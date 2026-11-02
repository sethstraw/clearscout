from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, tzinfo
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar, NamedTuple

import pytest
from sqlalchemy import func, select

from tailorcv.application import job_search
from tailorcv.application.discovery.board_scan import SourceScanService
from tailorcv.application.discovery.content_resolution import (
    PostingContentResolutionService,
    ResolvedPostingContent,
)
from tailorcv.application.discovery.dispositions import DiscoveryDispositionService
from tailorcv.application.discovery.pending import set_pending_markers
from tailorcv.application.discovery.posting_fetch import FetchedPosting
from tailorcv.application.discovery.posting_text import publisher_closing_date
from tailorcv.application.discovery.source_reading import PostingCheck, SourceListing, SourceRead
from tailorcv.application.documents.evidence_artifacts import EvidenceArtifactService
from tailorcv.application.failures import ValidationError
from tailorcv.application.job_search.models import (
    DispositionApply,
    DispositionSelection,
    JobListQuery,
)
from tailorcv.application.master_cv import MasterCvService
from tailorcv.application.master_cv.cv import parse_cv
from tailorcv.application.tailoring import fit
from tailorcv.application.tailoring.stages.jobspec import (
    Importance,
    JobSpec,
    Requirement,
    StoredPostingName,
)
from tailorcv.application.tailoring.stages.match import Classification, EvidenceMap, Match, Strength
from tailorcv.application.tailoring.workflows import (
    EvaluationHandler,
    EvaluationOutput,
    EvaluationWorkflowService,
)
from tailorcv.application.tailoring.workflows import evaluations as evaluation_workflows
from tailorcv.application.tasks import TaskQueueService, task_types, validate_task_payload
from tailorcv.application.tasks.payloads import DiscoveryEvaluateBatchPayload
from tailorcv.application.workspaces import SearchParametersService
from tailorcv.bootstrap.worker import Worker, default_handlers
from tailorcv.domain import (
    EvidenceStatus,
    GapClass,
    PostingContentAccessStatus,
    PostingContentExtractionMethod,
    PostingIdentityStatus,
    PostingStatus,
    SourceReadOutcome,
    new_id,
)
from tailorcv.domain.artifacts import PostingSource
from tailorcv.domain.records import PostingReadIntent, RobotsReadBasis
from tailorcv.domain.search_parameters import (
    Acceptance,
    Arrangement,
    CountryNames,
    Employers,
    JobSearchParameters,
    Location,
    Seniority,
)
from tailorcv.domain.staged_facts import (
    ListingIdentityTrust,
    PublishedFact,
    PublisherClosingDate,
    StagedListingFacts,
)
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    Artifact,
    CostRecord,
    Employer,
    JobEvaluation,
    JobPosting,
    MasterCvVersion,
    ModelCall,
    PendingDiscovery,
    PendingDiscoveryDispositionEvent,
    PendingPostingContent,
    PostingObservation,
    PostingRefusal,
    Source,
    Workspace,
)
from tailorcv.infrastructure.web_fetch.ingest import JDText
from tailorcv.infrastructure.web_fetch.posting_resolution import DeterministicPostingContentResolver
from tailorcv.shared.costs import CallManifest, Ledger, ServiceTier, Usage
from tailorcv.shared.errors import IngestError, NoRequirementsError
from tests.support.artifacts import runtime_storage
from tests.support.doubles import UnexpectedPostingFetcher
from tests.support.runners import ScriptedPipelineRunner
from tests.support.scenarios import (
    HOLDOUT_ORIGINS,
    dismiss_pending_discovery,
    pending_posting_content,
    seed_pending_discovery,
    studio_workspace,
)


class _PostingFetcher:
    def __init__(self) -> None:
        self.intents_seen: list[PostingReadIntent] = []

    def fetch(
        self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL, board_url: str = ""
    ) -> FetchedPosting:
        self.intents_seen.append(intent)
        assert url == "https://boards.greenhouse.io/acme-freight/jobs/100"
        return FetchedPosting(
            text=(
                "Acme Freight seeks a Platform Director in Canada. "
                "Located in the Eastern time zone or one hour offset. "
                "Lead a governed data platform and its product roadmap."
            ),
            posting_status="active",
            method="direct_text",
            fetched_at=datetime(2026, 8, 10, 12, tzinfo=UTC),
            http_status=200,
            final_url=url,
            publisher_check=PostingCheck(
                status="readable",
                note="Fixture publisher response.",
                platform="greenhouse",
                employer="Acme Freight",
                title="Platform Director",
            ),
        )

    def listings(
        self,
        source_name: str,
        url: str,
        *,
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        assert source_name == "Acme Freight"
        assert url == "https://boards.greenhouse.io/acme-freight/jobs/100"
        return SourceRead(
            listings=[],
            complete=True,
            raw_count=0,
            outcome=SourceReadOutcome.ATS_COMPLETE,
        )


class _AnySourceFetcher(_PostingFetcher):
    """Answers the scenario posting whatever name its employer's source is stored under."""

    def listings(
        self,
        source_name: str,
        url: str,
        *,
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        del source_name
        return super().listings("Acme Freight", url)


class _TwoPostingFetcher(_PostingFetcher):
    """Answers the scenario posting and the second staged posting from one worker."""

    SECOND_URL = "https://jobs.lever.co/globex/second-posting"

    def fetch(
        self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL, board_url: str = ""
    ) -> FetchedPosting:
        del intent
        if url == self.SECOND_URL:
            return FetchedPosting(
                text=(
                    "Globex seeks a Platform Director in Canada. "
                    "Lead a governed data platform and its product roadmap."
                ),
                posting_status="active",
                method="direct_text",
                fetched_at=datetime(2026, 8, 10, 12, tzinfo=UTC),
                http_status=200,
                final_url=url,
                publisher_check=PostingCheck(
                    status="readable",
                    note="Fixture publisher response.",
                    platform="lever",
                    employer="Globex",
                    title="Platform Director",
                ),
            )
        return super().fetch(url)

    def listings(
        self,
        source_name: str,
        url: str,
        *,
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        if source_name == "Globex":
            return SourceRead(
                listings=[],
                complete=True,
                raw_count=0,
                outcome=SourceReadOutcome.ATS_COMPLETE,
            )
        return super().listings(source_name, url)


class _FailingPostingFetcher:
    def fetch(
        self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL, board_url: str = ""
    ) -> FetchedPosting:
        raise AssertionError(f"Stored verified content should prevent a fetch of {url}")

    def listings(
        self,
        source_name: str,
        url: str,
        *,
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        del source_name, url
        return SourceRead(
            listings=[],
            complete=True,
            raw_count=0,
            outcome=SourceReadOutcome.ATS_COMPLETE,
        )


class _ResolvedContent:
    def __init__(self, result: ResolvedPostingContent) -> None:
        self.result = result

    def resolve(
        self,
        url: str,
        *,
        employer: str = "",
        title: str = "",
        intent: PostingReadIntent = PostingReadIntent.CRAWL,
        board_url: str = "",
    ) -> ResolvedPostingContent:
        del url, employer, title, intent
        return self.result


class _EvaluationRunner(ScriptedPipelineRunner):
    evaluations = 0
    tiers: ClassVar[list[Any]] = []
    stored_names: ClassVar[list[StoredPostingName | None]] = []

    def evaluate(
        self,
        source: Path,
        settings: Any,
        *,
        master_cv: Path,
        service_tier: Any = None,
        ledger: Ledger | None = None,
        stored_name: StoredPostingName | None = None,
        listing_location: str = "",
        fresh: bool = False,
    ) -> EvaluationOutput:
        del fresh
        type(self).evaluations += 1
        type(self).tiers.append(service_tier)
        type(self).stored_names.append(stored_name)
        posting = source.read_text(encoding="utf-8")
        inventory = parse_cv(master_cv)
        entry_id = inventory.ids()[0]
        spec = JobSpec(
            title="Platform Director",
            company="Acme Freight",
            seniority="director",
            location="Canada",
            requirements=(
                Requirement(
                    id="R01",
                    text="Lead a governed data platform",
                    importance=Importance.MUST_HAVE,
                ),
            ),
            keywords=("data platform", "roadmap"),
            hard_gates=(),
            origin=str(source),
        )
        evidence = EvidenceMap(
            matches=(
                Match(
                    requirement_id="R01",
                    entry_ids=(entry_id,),
                    classification=Classification.DIRECT,
                    strength=Strength.STRONG,
                    note="Direct platform leadership evidence.",
                ),
            )
        )
        ledger = ledger or Ledger()
        ledger.record(CallManifest("jobspec", "model-a", Usage(input_tokens=20, output_tokens=8)))
        ledger.record(CallManifest("match", "model-a", Usage(input_tokens=30, output_tokens=10)))
        return EvaluationOutput(
            spec=spec,
            evidence=evidence,
            inventory=inventory,
            ledger=ledger,
            jd=JDText(text=posting, origin=str(source), method="direct_text"),
            reading=fit.read(
                posting,
                inventory,
                parameters=settings.job_search,
                vocabulary=settings.vocabulary,
                today=date(2026, 8, 10),
                listing_location=listing_location,
            ),
            outcome="pursue",
            reasons=("The must-have requirement has direct evidence.",),
            prompt_fingerprint="test-prompt-suite",
        )


class _IncompleteEvidenceRunner(_EvaluationRunner):
    def evaluate(
        self,
        source: Path,
        settings: Any,
        *,
        master_cv: Path,
        service_tier: Any = None,
        ledger: Ledger | None = None,
        stored_name: StoredPostingName | None = None,
        listing_location: str = "",
        fresh: bool = False,
    ) -> EvaluationOutput:
        output = super().evaluate(
            source,
            settings,
            master_cv=master_cv,
            service_tier=service_tier,
            ledger=ledger,
            stored_name=stored_name,
            listing_location=listing_location,
            fresh=fresh,
        )
        return replace(
            output,
            evidence=EvidenceMap(
                matches=(
                    Match(
                        requirement_id="R01",
                        classification=Classification.GAP,
                        strength=Strength.NONE,
                        note="The run did not establish this requirement.",
                        evidence_status=EvidenceStatus.UNRESOLVED,
                        gap_class=GapClass.INCOMPLETE_EVIDENCE,
                    ),
                ),
                unrecognized=("r14",),
            ),
        )


class _Scenario(NamedTuple):
    """A workspace with an imported master CV and one discovery waiting to be read."""

    workspace_id: str
    identifier_free_job_id: str
    pending_id: str
    artifact_root: Path
    queue: TaskQueueService
    worker: Worker


@pytest.fixture
def scenario(durable_database: Database, tmp_path: Path) -> _Scenario:
    artifact_root = tmp_path / "artifacts"
    workspace_id, identifier_free_job_id, _, master_version_id = studio_workspace(
        durable_database, tmp_path / "workspace", artifact_root
    )
    # The seed writes the version row directly; evaluation reads the one the
    # import path registered, so the raw row gives way to a real import.
    with durable_database.session() as session:
        version = session.get(MasterCvVersion, master_version_id)
        assert version is not None
        session.delete(version)
    MasterCvService(
        durable_database, runtime_storage(durable_database, artifact_root)
    ).import_source(
        workspace_id=workspace_id,
        filename="master.md",
        content=(tmp_path / "workspace" / "master.md").read_bytes(),
    )
    scans = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    scans.stage_listings(
        workspace_id,
        [
            SourceListing(
                employer="Acme Freight",
                title="Platform Director",
                location="Canada",
                requisition_id="REQ-100",
                url="https://boards.greenhouse.io/acme-freight/jobs/100",
            )
        ],
        date(2026, 8, 10),
        reason="guided search verified this posting",
    )
    return _Scenario(
        workspace_id=workspace_id,
        identifier_free_job_id=identifier_free_job_id,
        pending_id=scans.list_pending_discoveries(workspace_id)[0].id,
        artifact_root=artifact_root,
        queue=TaskQueueService(durable_database),
        worker=Worker(
            durable_database,
            worker_id="evaluate",
            handlers=default_handlers(
                durable_database,
                artifact_root=artifact_root,
                fetcher=_PostingFetcher(),
                workflow_runner=_EvaluationRunner(),
            ),
        ),
    )


def test_evaluation_preflight_does_not_read_a_person_dismissed_pending_posting(
    scenario: _Scenario, durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    dismiss_pending_discovery(durable_database, scenario.workspace_id, scenario.pending_id)
    service = EvaluationWorkflowService(
        durable_database,
        runtime_storage(durable_database, scenario.artifact_root),
        fetcher=UnexpectedPostingFetcher(),
    )

    def unexpected(*_args, **_kwargs):
        raise AssertionError("Preflight read a person-dismissed posting.")

    monkeypatch.setattr(service, "_pending_inputs", unexpected)
    response = service.preflight(
        scenario.workspace_id,
        DiscoveryEvaluateBatchPayload(pending_discovery_ids=[scenario.pending_id]),
    )
    assert response.expected_cost_usd == 0
    assert response.range_high_usd == 0
    assert response.items[0].current_skip_reason == "Person-dismissed posting."
    assert response.items[0].stages == []


def test_evaluation_preflight_explains_unread_pending_details_without_fetching(
    scenario: _Scenario, durable_database: Database
) -> None:
    service = EvaluationWorkflowService(
        durable_database,
        runtime_storage(durable_database, scenario.artifact_root),
        fetcher=UnexpectedPostingFetcher(),
    )
    response = service.preflight(
        scenario.workspace_id,
        DiscoveryEvaluateBatchPayload(pending_discovery_ids=[scenario.pending_id]),
    )
    assert response.range_high_usd is None
    assert response.items[0].range_unavailable_reason
    with durable_database.session() as session:
        assert session.scalar(select(func.count()).select_from(ModelCall)) == 0


def test_evaluation_preflight_prices_retained_job_without_history_or_refresh(
    scenario: _Scenario, durable_database: Database
) -> None:
    service = EvaluationWorkflowService(
        durable_database,
        runtime_storage(durable_database, scenario.artifact_root),
        fetcher=UnexpectedPostingFetcher(),
    )
    response = service.preflight(
        scenario.workspace_id,
        DiscoveryEvaluateBatchPayload(job_posting_ids=[scenario.identifier_free_job_id]),
    )
    assert response.currency == "USD"
    assert response.advisory is True
    assert response.expected_cost_usd is None
    assert response.expected_unavailable_reason
    assert response.range_low_usd == 0
    assert response.range_high_usd is not None and response.range_high_usd > 0
    assert {stage.stage for stage in response.items[0].stages} == {"jobspec", "match"}
    for stage in response.items[0].stages:
        assert stage.model and stage.price and stage.provenance
        catalog_priced = stage.provenance.source_kind == "bundled_catalog"
        assert (stage.provenance.table_date is not None) == catalog_priced
        assert bool(stage.provenance.table_date_unavailable_reason) != catalog_priced
    with durable_database.session() as session:
        assert session.scalar(select(func.count()).select_from(ModelCall)) == 0
        assert session.scalar(select(func.count()).select_from(CostRecord)) == 0


def test_evaluation_preflight_reprices_usable_output_samples_without_double_counting_reasoning(
    scenario: _Scenario, durable_database: Database
) -> None:
    service = EvaluationWorkflowService(
        durable_database,
        runtime_storage(durable_database, scenario.artifact_root),
        fetcher=UnexpectedPostingFetcher(),
    )
    selection = DiscoveryEvaluateBatchPayload(job_posting_ids=[scenario.identifier_free_job_id])
    initial = service.preflight(scenario.workspace_id, selection)
    with durable_database.session() as session:
        for stage in initial.items[0].stages:
            for cached, validated, digest, output in [
                (False, True, "a" * 64, 100),
                (True, True, None, 9000),
                (False, False, "b" * 64, 9000),
                (False, True, None, 9000),
            ]:
                session.add(
                    ModelCall(
                        id=new_id(),
                        workspace_id=scenario.workspace_id,
                        stage=stage.stage,
                        provider=stage.provider,
                        requested_model=stage.model,
                        output_tokens=output,
                        reasoning_tokens=50,
                        cache_hit=cached,
                        validated=validated,
                        response_digest=digest,
                    )
                )
    response = service.preflight(scenario.workspace_id, selection)
    assert response.expected_cost_usd is not None and response.expected_cost_usd > 0
    for stage in response.items[0].stages:
        assert stage.historical_output_samples == 1
        assert stage.price is not None
        price = stage.price.halved() if stage.requested_service_tier == "flex" else stage.price
        assert stage.expected_cost_usd == Decimal(
            str(price.cost(Usage(input_tokens=stage.input_token_estimate, output_tokens=100)))
        )


def test_evaluation_preflight_preserves_a_priced_row_when_another_target_is_missing(
    scenario: _Scenario, durable_database: Database
) -> None:
    service = EvaluationWorkflowService(
        durable_database,
        runtime_storage(durable_database, scenario.artifact_root),
        fetcher=UnexpectedPostingFetcher(),
    )
    response = service.preflight(
        scenario.workspace_id,
        DiscoveryEvaluateBatchPayload(
            job_posting_ids=[scenario.identifier_free_job_id, "missing-posting"]
        ),
    )
    assert len(response.items) == 2
    assert response.items[0].range_high_usd is not None
    assert response.items[1].range_high_usd is None
    assert response.items[1].range_unavailable_reason
    assert response.range_high_usd is None
    assert response.range_unavailable_reason


def test_evaluation_preflight_does_not_read_an_archived_jobs_artifacts(
    scenario: _Scenario, durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    with durable_database.session() as session:
        job = session.get(JobPosting, scenario.identifier_free_job_id)
        assert job is not None
        job.archived_at = datetime(2026, 10, 8, tzinfo=UTC)
    service = EvaluationWorkflowService(
        durable_database,
        runtime_storage(durable_database, scenario.artifact_root),
        fetcher=UnexpectedPostingFetcher(),
    )

    def unexpected(*_args, **_kwargs):
        raise AssertionError("Preflight read a retired posting's artifacts.")

    monkeypatch.setattr(service.workflows, "inputs", unexpected)
    response = service.preflight(
        scenario.workspace_id,
        DiscoveryEvaluateBatchPayload(job_posting_ids=[scenario.identifier_free_job_id]),
    )
    assert response.items[0].range_unavailable_reason
    assert response.range_high_usd is None


def _evaluate(
    scenario: _Scenario, key: str, *, worker: Worker | None = None, **payload: str | bool
) -> Any:
    """Queue one evaluation, run it, and return the finished task."""
    task = scenario.queue.enqueue(
        workspace_id=scenario.workspace_id,
        task_type="discovery.evaluate",
        payload={**payload},
        idempotency_key=key,
    )
    assert (worker or scenario.worker).run_once()
    finished = scenario.queue.get(str(task.id))
    assert finished.error_message is None, finished.error_message
    assert finished.status == "completed"
    return finished


def _evaluate_that_cannot_produce_a_refusal(
    scenario: _Scenario, durable_database: Database, key: str, worker: Worker
) -> None:
    """Run an evidence failure and prove it leaves the discovery reviewable."""
    task = scenario.queue.enqueue(
        workspace_id=scenario.workspace_id,
        task_type="discovery.evaluate",
        payload={"pending_discovery_id": scenario.pending_id},
        idempotency_key=key,
    )
    assert worker.run_once()
    finished = scenario.queue.get(str(task.id))
    assert finished.status == "failed"
    assert "retain" in (finished.error_message or "") or "unavailable" in (
        finished.error_message or ""
    )
    with durable_database.session() as session:
        assert session.scalar(select(PostingRefusal)) is None
        assert session.scalar(select(PendingDiscoveryDispositionEvent)) is None
        assert session.scalar(select(ModelCall)) is None
        pending = session.get(PendingDiscovery, scenario.pending_id)
        assert pending is not None and pending.archived_at is None


def _evaluate_with_unavailable_content(
    scenario: _Scenario, durable_database: Database, key: str, worker: Worker
) -> None:
    """Complete one content-unavailable evaluation as an explicit free skip."""
    task = scenario.queue.enqueue(
        workspace_id=scenario.workspace_id,
        task_type="discovery.evaluate",
        payload={"pending_discovery_id": scenario.pending_id},
        idempotency_key=key,
    )
    assert worker.run_once()
    finished = scenario.queue.get(str(task.id))
    assert finished.status == "completed"
    assert finished.progress["pending_discovery_id"] == scenario.pending_id
    assert finished.progress["skipped"] == "unreadable"
    with durable_database.session() as session:
        assert session.scalar(select(PostingRefusal)) is None
        assert session.scalar(select(PendingDiscoveryDispositionEvent)) is None
        assert session.scalar(select(ModelCall)) is None
        pending = session.get(PendingDiscovery, scenario.pending_id)
        assert pending is not None and pending.archived_at is None


def _second_pending(scenario: _Scenario, durable_database: Database) -> str:
    """Stage a second discovery beside the scenario's and return its id."""
    scans = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    scans.stage_listings(
        scenario.workspace_id,
        [
            SourceListing(
                employer="Globex",
                title="Platform Director",
                location="Canada",
                requisition_id="REQ-200",
                url=_TwoPostingFetcher.SECOND_URL,
            )
        ],
        date(2026, 8, 10),
        reason="guided search verified this posting",
    )
    return next(
        row.id
        for row in scans.list_pending_discoveries(scenario.workspace_id)
        if row.id != scenario.pending_id
    )


def _batch_worker(
    scenario: _Scenario,
    durable_database: Database,
    worker_id: str,
    runner: _EvaluationRunner | None = None,
) -> Worker:
    """Build a worker that can read either staged posting of a two-row batch."""
    return Worker(
        durable_database,
        worker_id=worker_id,
        handlers=default_handlers(
            durable_database,
            artifact_root=scenario.artifact_root,
            fetcher=_TwoPostingFetcher(),
            workflow_runner=runner or _EvaluationRunner(),
        ),
    )


def _job_count(database: Database, workspace_id: str) -> int:
    with database.session() as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(JobPosting)
                .where(JobPosting.workspace_id == workspace_id)
            )
            or 0
        )


def test_an_evaluation_task_names_exactly_one_subject() -> None:
    """A discovery or a job already in the ledger, never both and never neither."""
    assert "discovery.evaluate" in task_types()
    assert validate_task_payload("discovery.evaluate", {"pending_discovery_id": "pending-1"}) == {
        "pending_discovery_id": "pending-1",
        "job_posting_id": None,
        "fresh": False,
    }
    for payload in ({}, {"pending_discovery_id": "pending-1", "job_posting_id": "job-1"}):
        with pytest.raises(ValidationError, match="exactly one"):
            validate_task_payload("discovery.evaluate", payload)


def test_evaluating_a_discovery_reads_the_posting_it_already_holds(
    scenario: _Scenario, durable_database: Database
) -> None:
    """Evaluation fetches the row's own address, which robots governs as the person's read."""
    fetcher = _PostingFetcher()
    worker = Worker(
        durable_database,
        worker_id="evaluate-known-posting",
        handlers=default_handlers(
            durable_database,
            artifact_root=scenario.artifact_root,
            fetcher=fetcher,
            workflow_runner=_EvaluationRunner(),
        ),
    )
    _evaluate(
        scenario, "evaluate-known-posting", worker=worker, pending_discovery_id=scenario.pending_id
    )

    assert fetcher.intents_seen == [PostingReadIntent.CRAWL]


def test_evaluating_a_discovery_files_it_as_a_job_with_a_stored_fit_report(
    scenario: _Scenario, durable_database: Database
) -> None:
    finished = _evaluate(scenario, "evaluate-pending", pending_discovery_id=scenario.pending_id)
    job_id = str(finished.progress["job_posting_id"])
    assert finished.progress["evaluation_version"] == 1

    with durable_database.session() as session:
        discovery = session.get(PendingDiscovery, scenario.pending_id)
        assert discovery is not None and discovery.archived_at is not None, (
            "an evaluated discovery is no longer pending"
        )
        job = session.get(JobPosting, job_id)
        assert job is not None and job.title == "Platform Director"
        evaluation = session.scalar(
            select(JobEvaluation).where(JobEvaluation.job_posting_id == job_id)
        )
        assert evaluation is not None
        assert evaluation.fit_report_artifact_id is not None
        report = session.get(Artifact, evaluation.fit_report_artifact_id)
        assert report is not None and report.logical_type == "evaluation.fit_report.markdown"
        employer = session.scalar(
            select(Employer).where(
                Employer.workspace_id == scenario.workspace_id,
                Employer.normalized_name == "acme freight",
            )
        )
        assert employer is not None
        assert employer.careers_url is None, "a posting address is never the careers page"
        source = session.scalar(select(Source).where(Source.employer_id == employer.id))
        assert source is not None
        assert source.url == "https://job-boards.greenhouse.io/acme-freight"


def test_evaluation_promotes_the_board_without_replacing_the_employers_careers_page(
    scenario: _Scenario, durable_database: Database
) -> None:
    with durable_database.session() as session:
        session.add(
            Employer(
                workspace_id=scenario.workspace_id,
                name="Acme Freight",
                normalized_name="acme freight",
                careers_url="https://www.acme-freight.example/careers",
            )
        )

    _evaluate(scenario, "evaluate-known-employer", pending_discovery_id=scenario.pending_id)

    with durable_database.session() as session:
        employer = session.scalar(
            select(Employer).where(
                Employer.workspace_id == scenario.workspace_id,
                Employer.normalized_name == "acme freight",
            )
        )
        assert employer is not None
        assert employer.careers_url == "https://www.acme-freight.example/careers"
        source = session.scalar(select(Source).where(Source.employer_id == employer.id))
        assert source is not None and source.url == "https://job-boards.greenhouse.io/acme-freight"


def test_evaluation_reuses_verified_current_content_without_refetching(
    scenario: _Scenario, durable_database: Database
) -> None:
    url = "https://boards.greenhouse.io/acme-freight/jobs/100"
    body = (
        "Acme Freight seeks a Platform Director in Canada. "
        "Lead a governed data platform and its product roadmap."
    )
    result = ResolvedPostingContent(
        requested_url=url,
        final_url=url,
        content_status=PostingContentAccessStatus.READABLE,
        identity_status=PostingIdentityStatus.VERIFIED,
        posting_status=PostingStatus.ACTIVE,
        extraction_method=PostingContentExtractionMethod.STATIC_STRUCTURED,
        fetched_at=datetime(2026, 8, 28, tzinfo=UTC),
        content=body.encode(),
        http_status=200,
    )
    storage = runtime_storage(durable_database, scenario.artifact_root)
    resolution = PostingContentResolutionService(
        durable_database, _ResolvedContent(result), storage=storage
    )
    preflight = resolution.preflight(scenario.workspace_id, [scenario.pending_id])
    resolution.resolve_batch(
        scenario.workspace_id,
        [scenario.pending_id],
        preflight.snapshot_sha256,
        origin="discovery.refresh_batch",
    )
    worker = Worker(
        durable_database,
        worker_id="evaluate-stored-content",
        handlers=default_handlers(
            durable_database,
            artifact_root=scenario.artifact_root,
            fetcher=_FailingPostingFetcher(),
            workflow_runner=_EvaluationRunner(),
        ),
    )

    finished = _evaluate(
        scenario,
        "evaluate-stored-content",
        worker=worker,
        pending_discovery_id=scenario.pending_id,
    )

    assert finished.progress["decision"]


def test_evaluating_retained_content_carries_its_robots_basis_onto_the_observation(
    scenario: _Scenario, durable_database: Database
) -> None:
    """A single-posting-read basis on the retained evidence reaches the stored observation."""
    url = "https://boards.greenhouse.io/acme-freight/jobs/100"
    body = (
        "Acme Freight seeks a Platform Director in Canada. "
        "Lead a governed data platform and its product roadmap."
    )
    result = ResolvedPostingContent(
        requested_url=url,
        final_url=url,
        content_status=PostingContentAccessStatus.READABLE,
        identity_status=PostingIdentityStatus.VERIFIED,
        posting_status=PostingStatus.ACTIVE,
        extraction_method=PostingContentExtractionMethod.STATIC_STRUCTURED,
        fetched_at=datetime(2026, 8, 28, tzinfo=UTC),
        content=body.encode(),
        http_status=200,
        robots_basis=RobotsReadBasis.SINGLE_POSTING_READ,
    )
    storage = runtime_storage(durable_database, scenario.artifact_root)
    resolution = PostingContentResolutionService(
        durable_database, _ResolvedContent(result), storage=storage
    )
    preflight = resolution.preflight(scenario.workspace_id, [scenario.pending_id])
    resolution.resolve_batch(
        scenario.workspace_id,
        [scenario.pending_id],
        preflight.snapshot_sha256,
        origin="discovery.refresh_batch",
    )
    worker = Worker(
        durable_database,
        worker_id="evaluate-robots-basis",
        handlers=default_handlers(
            durable_database,
            artifact_root=scenario.artifact_root,
            fetcher=_FailingPostingFetcher(),
            workflow_runner=_EvaluationRunner(),
        ),
    )

    finished = _evaluate(
        scenario,
        "evaluate-robots-basis",
        worker=worker,
        pending_discovery_id=scenario.pending_id,
    )
    job_id = str(finished.progress["job_posting_id"])

    with durable_database.session() as session:
        observation = session.scalar(
            select(PostingObservation).where(PostingObservation.job_posting_id == job_id)
        )
        assert observation is not None
        assert observation.robots_basis == "single_posting_read"


class _AllowedPostingFetcher(_PostingFetcher):
    """A publisher read robots explicitly allowed."""

    def fetch(
        self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL, board_url: str = ""
    ) -> FetchedPosting:
        return replace(super().fetch(url, intent=intent), robots_basis=RobotsReadBasis.ALLOWED)


@pytest.mark.parametrize(
    "refresh", ["retained", "failed", "empty", "changed", "absent", "body_label"]
)
def test_evaluation_keeps_closing_provenance_until_a_readable_refresh_replaces_it(
    scenario: _Scenario,
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    refresh: str,
) -> None:
    url = "https://boards.greenhouse.io/acme-freight/jobs/100"
    observed_at = datetime(2026, 8, 1, 12, tzinfo=UTC)
    stamp = "2026-11-01T15:00:00-07:00"
    body = (
        "Acme Freight seeks a Platform Director in Canada. "
        "Lead a governed data platform and its product roadmap.\n"
        f"Closing date: {stamp}\n"
        "Apply through the Acme Freight careers page.\n"
    )
    source = PostingSource(
        publisher="greenhouse",
        record=f'{{"validThrough": "{stamp}"}}',
    )
    resolved = ResolvedPostingContent(
        requested_url=url,
        final_url=url,
        content_status=PostingContentAccessStatus.READABLE,
        identity_status=PostingIdentityStatus.VERIFIED,
        posting_status=PostingStatus.ACTIVE,
        extraction_method=PostingContentExtractionMethod.STATIC_STRUCTURED,
        fetched_at=observed_at,
        content=body.encode(),
        source=source,
        publisher_closing_date=publisher_closing_date(
            body, valid_through=stamp, source_url=url, observed_at=observed_at
        ),
    )
    resolution = PostingContentResolutionService(
        durable_database,
        _ResolvedContent(resolved),
        storage=runtime_storage(durable_database, scenario.artifact_root),
    )
    resolution.store_prefetched_result(
        scenario.workspace_id, scenario.pending_id, resolved, None, origin="evaluation-test"
    )
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, scenario.pending_id)
        assert pending is not None
        retained = session.get(PendingPostingContent, pending.current_content_id)
        assert retained is not None
        original = PublisherClosingDate.from_json(retained.publisher_closing_date)
        assert all(statement.artifact_id for statement in original.statements)
    worker = Worker(
        durable_database,
        worker_id="evaluate-closing-facts",
        handlers=default_handlers(
            durable_database,
            artifact_root=scenario.artifact_root,
            fetcher=_FailingPostingFetcher(),
            workflow_runner=_EvaluationRunner(),
        ),
    )
    first = _evaluate(
        scenario, "closing-first", worker=worker, pending_discovery_id=scenario.pending_id
    )
    job_id = str(first.progress["job_posting_id"])
    with durable_database.session() as session:
        first_observation = session.scalar(
            select(PostingObservation).where(PostingObservation.job_posting_id == job_id)
        )
        assert first_observation is not None
        assert first_observation.publisher_closing_date == original.as_json()

    clock = observed_at + timedelta(hours=1 if refresh == "retained" else 48)

    class ObservedDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> ObservedDatetime:
            return cls.fromtimestamp(clock.timestamp(), tz=tz)

    monkeypatch.setattr(evaluation_workflows, "datetime", ObservedDatetime)
    fresh_body = body.partition("\nClosing date:")[0]
    if refresh == "body_label":
        fresh_body += "\nClosing date: 2026-12-15\nApply through the Acme Freight careers page.\n"
    fresh_fact = (
        publisher_closing_date(
            fresh_body, valid_through="2026-12-15", source_url=url, observed_at=clock
        )
        if refresh == "changed"
        else None
    )
    refreshed = FetchedPosting(
        text=None if refresh == "empty" else fresh_body,
        posting_status="active",
        method="direct_text",
        fetched_at=clock,
        final_url=url,
        publisher_closing_date=fresh_fact,
    )
    fetcher = (
        _RefreshFailureFetcher()
        if refresh in {"failed", "retained"}
        else _ScriptedRefreshFetcher(refreshed)
    )
    if isinstance(fetcher, _ScriptedRefreshFetcher):
        fetcher.intents_seen.append(PostingReadIntent.SINGLE_POSTING)
    rerun_worker = Worker(
        durable_database,
        worker_id="evaluate-closing-facts",
        handlers=default_handlers(
            durable_database,
            artifact_root=scenario.artifact_root,
            fetcher=fetcher,
            workflow_runner=_EvaluationRunner(),
        ),
    )

    second = _evaluate(scenario, "closing-second", worker=rerun_worker, job_posting_id=job_id)

    with durable_database.session() as session:
        observation = session.scalar(
            select(PostingObservation)
            .join(JobEvaluation, JobEvaluation.observation_id == PostingObservation.id)
            .where(JobEvaluation.id == second.progress["evaluation_id"])
        )
        assert observation is not None
        assert observation.posting_status == "active"
        fact = PublisherClosingDate.from_json(observation.publisher_closing_date)
        if refresh in {"retained", "failed", "empty"}:
            assert observation.publisher_closing_date == original.as_json()
        else:
            assert fact.observed_at == clock
            assert fact.source_url == url
            assert fact.normalized_value == ("2026-12-15" if refresh != "absent" else None)
            assert fact.precision == ("date" if refresh != "absent" else None)
            assert fact.timezone is None
            assert len(fact.statements) == (0 if refresh == "absent" else 1)
            if refresh == "body_label":
                assert fact.artifact_id == observation.body_artifact_id
                assert fact.statements[0].artifact_id == observation.body_artifact_id
            elif refresh == "changed":
                assert fact.artifact_id is None
                assert fact.statements[0].artifact_id is None


def test_evaluating_a_freshly_fetched_posting_stores_its_allowed_robots_basis(
    scenario: _Scenario, durable_database: Database
) -> None:
    """A publisher fetch robots explicitly allowed carries that basis to the observation."""
    worker = Worker(
        durable_database,
        worker_id="evaluate-allowed-robots-basis",
        handlers=default_handlers(
            durable_database,
            artifact_root=scenario.artifact_root,
            fetcher=_AllowedPostingFetcher(),
            workflow_runner=_EvaluationRunner(),
        ),
    )
    finished = _evaluate(
        scenario,
        "evaluate-allowed-robots-basis",
        worker=worker,
        pending_discovery_id=scenario.pending_id,
    )
    job_id = str(finished.progress["job_posting_id"])

    with durable_database.session() as session:
        observation = session.scalar(
            select(PostingObservation).where(PostingObservation.job_posting_id == job_id)
        )
        assert observation is not None
        assert observation.robots_basis == "allowed"


class _StatedFactsPostingFetcher(_PostingFetcher):
    """A publisher read whose body states residence, employment type, and payroll."""

    def fetch(
        self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL, board_url: str = ""
    ) -> FetchedPosting:
        return replace(
            super().fetch(url, intent=intent),
            text=(
                "Acme Freight seeks a Platform Director in Canada. "
                "Applicants must reside in Canada. "
                "This is a full-time role paid as a W-2 employee. "
                "Lead a governed data platform and its product roadmap."
            ),
        )


def test_evaluating_a_discovery_records_the_facts_its_body_states(
    scenario: _Scenario, durable_database: Database
) -> None:
    worker = Worker(
        durable_database,
        worker_id="evaluate-stated-residence",
        handlers=default_handlers(
            durable_database,
            artifact_root=scenario.artifact_root,
            fetcher=_StatedFactsPostingFetcher(),
            workflow_runner=_EvaluationRunner(),
        ),
    )
    finished = _evaluate(
        scenario,
        "evaluate-stated-residence",
        worker=worker,
        pending_discovery_id=scenario.pending_id,
    )
    job_id = str(finished.progress["job_posting_id"])

    with durable_database.session() as session:
        observation = session.scalar(
            select(PostingObservation).where(PostingObservation.job_posting_id == job_id)
        )
        assert observation is not None
        assert (
            observation.location,
            observation.residence_rule,
            observation.employment_type,
            observation.payroll_arrangement,
        ) == ("Canada", "Applicants must reside in Canada.", "full-time", "W-2")


def test_the_job_detail_says_what_the_arrangement_was_read_from(
    scenario: _Scenario, durable_database: Database
) -> None:
    """A classification can contradict a posting's own label, so it carries its reason."""
    finished = _evaluate(scenario, "evaluate-arrangement", pending_discovery_id=scenario.pending_id)

    detail = job_search.job_detail(
        durable_database, str(finished.progress["job_posting_id"]), scenario.workspace_id
    )

    details = [record.details for record in detail.eligibility_records]
    assert any("Arrangement:" in text for text in details)
    assert any("Place:" in text for text in details)


def test_an_evaluation_files_the_calls_it_was_billed_for(
    scenario: _Scenario, durable_database: Database
) -> None:
    """A run the workspace paid for is spend the guard and the ledger can see."""
    finished = _evaluate(scenario, "evaluate-billing", pending_discovery_id=scenario.pending_id)

    with durable_database.session() as session:
        calls = session.scalars(
            select(ModelCall).where(ModelCall.task_id == str(finished.id))
        ).all()
        stages = sorted(call.stage for call in calls)
        costs = session.scalars(
            select(CostRecord).where(
                CostRecord.model_call_id.in_([call.id for call in calls] or [""])
            )
        ).all()

    assert stages == ["jobspec", "match"]
    assert len(costs) == len(calls)


def test_a_failed_commit_still_bills_the_calls_the_provider_already_ran(
    scenario: _Scenario,
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model calls bill whatever happens next; a rollback must not lose the ledger."""

    def _fail(*_args: Any, **_kwargs: Any) -> None:
        raise ValidationError("Simulated commit failure after paid calls ran.")

    monkeypatch.setattr(job_search, "commit_evaluation", _fail)

    task = scenario.queue.enqueue(
        workspace_id=scenario.workspace_id,
        task_type="discovery.evaluate",
        payload={"pending_discovery_id": scenario.pending_id},
        idempotency_key="evaluate-failing-commit",
    )
    assert scenario.worker.run_once()
    finished = scenario.queue.get(str(task.id))
    assert finished.status == "failed"

    with durable_database.session() as session:
        calls = session.scalars(
            select(ModelCall).where(ModelCall.task_id == str(finished.id))
        ).all()
        costs = session.scalars(
            select(CostRecord).where(
                CostRecord.model_call_id.in_([call.id for call in calls] or [""])
            )
        ).all()

    assert sorted(call.stage for call in calls) == ["jobspec", "match"]
    assert len(costs) == len(calls)


def test_a_runner_failure_still_bills_the_call_it_recorded(
    scenario: _Scenario, durable_database: Database
) -> None:
    class RecordingFailureRunner(_EvaluationRunner):
        def evaluate(
            self, *args: Any, ledger: Ledger | None = None, **kwargs: Any
        ) -> EvaluationOutput:
            del args, kwargs
            assert ledger is not None
            ledger.record(
                CallManifest("jobspec", "model-a", Usage(input_tokens=20, output_tokens=8))
            )
            raise ValidationError("The provider response could not be parsed.")

    worker = Worker(
        durable_database,
        worker_id="evaluate-runner-failure",
        handlers=default_handlers(
            durable_database,
            artifact_root=scenario.artifact_root,
            fetcher=_PostingFetcher(),
            workflow_runner=RecordingFailureRunner(),
        ),
    )
    task = scenario.queue.enqueue(
        workspace_id=scenario.workspace_id,
        task_type="discovery.evaluate",
        payload={"pending_discovery_id": scenario.pending_id},
        idempotency_key="evaluate-runner-failure",
    )

    assert worker.run_once()
    finished = scenario.queue.get(str(task.id))
    assert finished.status == "failed"
    with durable_database.session() as session:
        calls = session.scalars(select(ModelCall).where(ModelCall.task_id == str(task.id))).all()
        costs = session.scalars(
            select(CostRecord).where(CostRecord.model_call_id.in_([call.id for call in calls]))
        ).all()

    assert [call.stage for call in calls] == ["jobspec"]
    assert len(costs) == 1


def test_a_content_ingest_failure_is_skipped_after_billing_its_call(
    scenario: _Scenario, durable_database: Database
) -> None:
    class RecordingIngestFailureRunner(_EvaluationRunner):
        def evaluate(
            self, *args: Any, ledger: Ledger | None = None, **kwargs: Any
        ) -> EvaluationOutput:
            del args, kwargs
            assert ledger is not None
            ledger.record(
                CallManifest("jobspec", "model-a", Usage(input_tokens=20, output_tokens=8))
            )
            raise IngestError("The refreshed publisher body has no requirements.")

    worker = Worker(
        durable_database,
        worker_id="evaluate-ingest-failure",
        handlers=default_handlers(
            durable_database,
            artifact_root=scenario.artifact_root,
            fetcher=_PostingFetcher(),
            workflow_runner=RecordingIngestFailureRunner(),
        ),
    )
    task = scenario.queue.enqueue(
        workspace_id=scenario.workspace_id,
        task_type="discovery.evaluate",
        payload={"pending_discovery_id": scenario.pending_id},
        idempotency_key="evaluate-ingest-failure",
    )

    assert worker.run_once()
    finished = scenario.queue.get(str(task.id))
    assert finished.status == "completed"
    assert finished.progress["skipped"] == "unreadable"
    with durable_database.session() as session:
        calls = session.scalars(select(ModelCall).where(ModelCall.task_id == str(task.id))).all()
        costs = session.scalars(
            select(CostRecord).where(CostRecord.model_call_id.in_([call.id for call in calls]))
        ).all()

    assert [call.stage for call in calls] == ["jobspec"]
    assert len(costs) == 1


class _RaisingRunner(_EvaluationRunner):
    def __init__(self, error: IngestError, during: Callable[[], None] = lambda: None) -> None:
        super().__init__()
        self.error = error
        self.during = during

    def evaluate(self, *args: Any, **kwargs: Any) -> EvaluationOutput:
        del args, kwargs
        self.during()
        raise self.error


def _run_raising(scenario: _Scenario, database: Database, runner: _RaisingRunner, key: str) -> Any:
    worker = Worker(
        database,
        worker_id=key,
        handlers=default_handlers(
            database,
            artifact_root=scenario.artifact_root,
            fetcher=_PostingFetcher(),
            workflow_runner=runner,
        ),
    )
    return _evaluate(scenario, key, worker=worker, pending_discovery_id=scenario.pending_id)


@pytest.mark.parametrize(
    ("error", "dismissed"),
    [
        (
            NoRequirementsError("No requirements could be read out of the posting."),
            [("no_requirements", "evaluation.body_screen")],
        ),
        (IngestError("The refreshed publisher body could not be decoded."), []),
    ],
)
def test_only_a_posting_with_no_requirements_is_dismissed_from_pending(
    scenario: _Scenario,
    durable_database: Database,
    error: IngestError,
    dismissed: list[tuple[str, str]],
) -> None:
    finished = _run_raising(scenario, durable_database, _RaisingRunner(error), "no-requirements")

    assert finished.progress["skipped"] == "unreadable"
    rows = DiscoveryDispositionService(durable_database).dismissed(scenario.workspace_id)
    assert [(row.category, row.origin) for row in rows] == dismissed


def test_a_body_replaced_mid_run_is_not_dismissed_for_having_no_requirements(
    scenario: _Scenario, durable_database: Database
) -> None:
    url = "https://boards.greenhouse.io/acme-freight/jobs/100"
    replacement = ResolvedPostingContent(
        requested_url=url,
        final_url=url,
        content_status=PostingContentAccessStatus.READABLE,
        identity_status=PostingIdentityStatus.VERIFIED,
        posting_status=PostingStatus.ACTIVE,
        extraction_method=PostingContentExtractionMethod.STATIC_STRUCTURED,
        fetched_at=datetime(2026, 8, 29, tzinfo=UTC),
        content=b"Acme Freight seeks a Platform Director. Ten years of platform leadership.",
        http_status=200,
    )
    resolution = PostingContentResolutionService(
        durable_database,
        _ResolvedContent(replacement),
        storage=runtime_storage(durable_database, scenario.artifact_root),
    )

    def replace_body() -> None:
        preflight = resolution.preflight(scenario.workspace_id, [scenario.pending_id])
        resolution.resolve_batch(
            scenario.workspace_id,
            [scenario.pending_id],
            preflight.snapshot_sha256,
            origin="discovery.refresh_batch",
        )

    runner = _RaisingRunner(NoRequirementsError("No requirements."), during=replace_body)
    finished = _run_raising(scenario, durable_database, runner, "no-requirements-replaced")

    assert finished.progress["skipped"] == "unreadable"
    assert DiscoveryDispositionService(durable_database).dismissed(scenario.workspace_id) == []


def test_a_person_dismissal_mid_run_outlasts_no_requirements_and_a_later_body(
    scenario: _Scenario, durable_database: Database
) -> None:
    url = "https://boards.greenhouse.io/acme-freight/jobs/100"
    changed = ResolvedPostingContent(
        requested_url=url,
        final_url=url,
        content_status=PostingContentAccessStatus.READABLE,
        identity_status=PostingIdentityStatus.VERIFIED,
        posting_status=PostingStatus.ACTIVE,
        extraction_method=PostingContentExtractionMethod.STATIC_STRUCTURED,
        fetched_at=datetime(2026, 8, 30, tzinfo=UTC),
        content=b"Acme Freight seeks a Platform Director. Ten years of platform leadership.",
        http_status=200,
    )
    runner = _RaisingRunner(
        NoRequirementsError("No requirements."),
        during=lambda: dismiss_pending_discovery(
            durable_database, scenario.workspace_id, scenario.pending_id
        ),
    )
    _run_raising(scenario, durable_database, runner, "no-requirements-person")
    dispositions = DiscoveryDispositionService(durable_database)
    after_run = dispositions.history(scenario.workspace_id, scenario.pending_id)

    resolution = PostingContentResolutionService(
        durable_database,
        _ResolvedContent(changed),
        storage=runtime_storage(durable_database, scenario.artifact_root),
    )
    resolution.resolve_batch(
        scenario.workspace_id,
        [scenario.pending_id],
        resolution.preflight(scenario.workspace_id, [scenario.pending_id]).snapshot_sha256,
        origin="discovery.refresh_batch",
    )
    after_body = dispositions.history(scenario.workspace_id, scenario.pending_id)

    assert [(event.action, event.category) for event in after_run] == [("dismiss", "human_review")]
    assert [(event.action, event.category) for event in after_body] == [("dismiss", "human_review")]


def test_a_queued_evaluation_asks_for_the_discounted_tier(scenario: _Scenario) -> None:
    """Screening a posting is queue work, so the slower tier costs the user nothing."""
    _EvaluationRunner.tiers.clear()

    _evaluate(scenario, "evaluate-tier", pending_discovery_id=scenario.pending_id)

    assert _EvaluationRunner.tiers == [ServiceTier.FLEX]


def test_an_evaluation_names_a_tracked_posting_by_its_stored_title_and_employer(
    scenario: _Scenario,
) -> None:
    _EvaluationRunner.stored_names.clear()

    _evaluate(scenario, "evaluate-stored-name", job_posting_id=scenario.identifier_free_job_id)

    assert _EvaluationRunner.stored_names == [
        StoredPostingName(title="Platform Lead", employer="Acme")
    ]


@pytest.mark.parametrize(
    ("trust", "employer"),
    [
        (ListingIdentityTrust.UNVERIFIED_HINT, "Acme Freight"),
        (ListingIdentityTrust.PUBLISHER, "acme-freight"),
    ],
)
def test_an_unvouched_discovery_is_named_by_the_employer_its_publisher_stated(
    scenario: _Scenario,
    durable_database: Database,
    trust: ListingIdentityTrust,
    employer: str,
) -> None:
    """A vouched stored name stands; an unvouched one yields to the publisher, never to a model."""
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, scenario.pending_id)
        assert pending is not None
        pending.employer = "acme-freight"
        pending.staged_facts = StagedListingFacts(identity_trust=trust).as_json()
    _EvaluationRunner.stored_names.clear()
    worker = Worker(
        durable_database,
        worker_id="evaluate-name",
        handlers=default_handlers(
            durable_database,
            artifact_root=scenario.artifact_root,
            fetcher=_AnySourceFetcher(),
            workflow_runner=_EvaluationRunner(),
        ),
    )

    _evaluate(
        scenario,
        f"evaluate-name-{trust.value}",
        worker=worker,
        pending_discovery_id=scenario.pending_id,
    )

    assert _EvaluationRunner.stored_names == [
        StoredPostingName(title="Platform Director", employer=employer)
    ]


class _RespelledEmployerFetcher(_AnySourceFetcher):
    """Answers the scenario posting with its employer named as ``spelling``."""

    def __init__(self, spelling: str) -> None:
        super().__init__()
        self.spelling = spelling

    def fetch(
        self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL, board_url: str = ""
    ) -> FetchedPosting:
        fetched = super().fetch(url, intent=intent)
        assert fetched.text is not None
        return replace(fetched, text=fetched.text.replace("Acme Freight", self.spelling))


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        (
            "ACME Freight",
            StoredPostingName(title="Platform Director", employer="ACME Freight"),
        ),
        (
            "The company",
            StoredPostingName(
                title="Platform Director",
                employer="Acme Freight",
                employer_from_address_only=True,
            ),
        ),
    ],
)
def test_a_discovery_named_only_by_its_board_address_takes_the_posting_spelling_or_a_flag(
    scenario: _Scenario,
    durable_database: Database,
    spelling: str,
    expected: StoredPostingName,
) -> None:
    """A vouched name equal to the address's tenant is checked against the posting text."""
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, scenario.pending_id)
        assert pending is not None
        pending.employer = "Acme Freight"
        pending.staged_facts = StagedListingFacts(
            identity_trust=ListingIdentityTrust.PUBLISHER
        ).as_json()
    _EvaluationRunner.stored_names.clear()
    worker = Worker(
        durable_database,
        worker_id="evaluate-address-name",
        handlers=default_handlers(
            durable_database,
            artifact_root=scenario.artifact_root,
            fetcher=_RespelledEmployerFetcher(spelling),
            workflow_runner=_EvaluationRunner(),
        ),
    )

    _evaluate(
        scenario,
        f"evaluate-address-name-{expected.employer_from_address_only}",
        worker=worker,
        pending_discovery_id=scenario.pending_id,
    )

    assert _EvaluationRunner.stored_names == [expected]


def test_an_evaluation_reads_publisher_evidence_only_from_its_own_pending_posting(
    scenario: _Scenario, durable_database: Database
) -> None:
    """Another pending posting that later served the same body does not name this one."""
    other_url = "https://boards.greenhouse.io/globex-freight/jobs/1"
    other = seed_pending_discovery(
        durable_database,
        scenario.workspace_id,
        employer="globex-freight",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, scenario.pending_id)
        assert pending is not None
        stated = pending.employer
        body = _PostingFetcher().fetch(pending.canonical_url or pending.discovery_url).text
        assert body is not None
        pending.staged_facts = StagedListingFacts(
            identity_trust=ListingIdentityTrust.UNVERIFIED_HINT
        ).as_json()
        session.add(
            pending_posting_content(
                scenario.workspace_id,
                other,
                other_url,
                content_sha256=hashlib.sha256(body.encode()).hexdigest(),
                fetched_at=datetime(2026, 9, 30, tzinfo=UTC),
                identity_evidence={"publisher_employer": "Globex Freight"},
            )
        )
    _EvaluationRunner.stored_names.clear()
    worker = Worker(
        durable_database,
        worker_id="evaluate-own-evidence",
        handlers=default_handlers(
            durable_database,
            artifact_root=scenario.artifact_root,
            fetcher=_AnySourceFetcher(),
            workflow_runner=_EvaluationRunner(),
        ),
    )

    _evaluate(
        scenario, "evaluate-own-evidence", worker=worker, pending_discovery_id=scenario.pending_id
    )

    assert _EvaluationRunner.stored_names == [
        StoredPostingName(title="Platform Director", employer=stated)
    ]


def test_an_unestablished_requirement_survives_the_stored_fit_report_round_trip(
    scenario: _Scenario, durable_database: Database
) -> None:
    worker = Worker(
        durable_database,
        worker_id="evaluate-incomplete",
        handlers=default_handlers(
            durable_database,
            artifact_root=scenario.artifact_root,
            fetcher=_PostingFetcher(),
            workflow_runner=_IncompleteEvidenceRunner(),
        ),
    )

    finished = _evaluate(
        scenario,
        "evaluate-incomplete",
        worker=worker,
        pending_discovery_id=scenario.pending_id,
    )
    report = job_search.fit_report(
        durable_database,
        EvidenceArtifactService(runtime_storage(durable_database, scenario.artifact_root)),
        str(finished.progress["job_posting_id"]),
        scenario.workspace_id,
    )

    assert report.requirements[0].established is False


def test_a_discarded_identifier_is_countable_after_the_run(
    scenario: _Scenario, durable_database: Database
) -> None:
    """A model naming a requirement the posting does not have is a number, not a lost row."""
    worker = Worker(
        durable_database,
        worker_id="evaluate-discarded",
        handlers=default_handlers(
            durable_database,
            artifact_root=scenario.artifact_root,
            fetcher=_PostingFetcher(),
            workflow_runner=_IncompleteEvidenceRunner(),
        ),
    )

    finished = _evaluate(
        scenario,
        "evaluate-discarded",
        worker=worker,
        pending_discovery_id=scenario.pending_id,
    )

    with durable_database.session() as session:
        evaluation = session.scalar(
            select(JobEvaluation).where(
                JobEvaluation.job_posting_id == str(finished.progress["job_posting_id"])
            )
        )
        assert evaluation is not None
        payload = evaluation.fit_report_payload or {}
        assert payload["unrecognized_requirements"] == ["r14"]


def test_a_posting_a_stated_condition_refuses_costs_no_fit_analysis(
    scenario: _Scenario, durable_database: Database
) -> None:
    """A refusal decidable before any paid call costs no fit analysis.

    This employer is one the person asked to leave out, which is decidable from
    their own parameters and the posting alone. The evaluation never runs, and
    what is filed is a refusal rather than an evaluation with nothing in it.
    """
    parameters = SearchParametersService(durable_database)
    used = parameters.save(
        scenario.workspace_id,
        JobSearchParameters(employers=Employers(excluded_employers=["Acme Freight"])),
    )
    changed = JobSearchParameters(
        employers=Employers(excluded_employers=["Acme Freight"]),
        seniority=Seniority(minimum="senior"),
    )

    class ParameterChangingFetcher(_PostingFetcher):
        def fetch(
            self,
            url: str,
            *,
            intent: PostingReadIntent = PostingReadIntent.CRAWL,
            board_url: str = "",
        ) -> FetchedPosting:
            parameters.save(scenario.workspace_id, changed)
            return super().fetch(url, intent=intent)

    worker = Worker(
        durable_database,
        worker_id="evaluate-refusal-provenance",
        handlers=default_handlers(
            durable_database,
            artifact_root=scenario.artifact_root,
            fetcher=ParameterChangingFetcher(),
            workflow_runner=_EvaluationRunner(),
        ),
    )
    before = _EvaluationRunner.evaluations

    finished = _evaluate(
        scenario,
        "evaluate-refused",
        worker=worker,
        pending_discovery_id=scenario.pending_id,
    )

    assert _EvaluationRunner.evaluations == before, "a refused posting ran the pipeline"
    assert finished.progress["decision"] == "ineligible"
    assert finished.progress["refused_by"] == "employer"
    with durable_database.session() as session:
        refusal_row = session.scalar(select(PostingRefusal))
        assert refusal_row is not None
        assert refusal_row.condition == "employer"
        assert refusal_row.pending_discovery_id == scenario.pending_id
        assert refusal_row.search_parameter_set_id == used.id
        assert session.scalar(select(JobEvaluation)) is None, "a refusal is not an evaluation"
        discovery = session.get(PendingDiscovery, scenario.pending_id)
        assert discovery is not None and discovery.archived_at is None
        disposition = session.scalar(select(PendingDiscoveryDispositionEvent))
        assert disposition is not None
        assert disposition.search_parameter_set_id == used.id
        assert disposition.correlation_id == str(finished.id)
        pending_content = session.get(PendingPostingContent, discovery.current_content_id)
        assert pending_content is not None
        assert pending_content.origin == "evaluation.prefetched_fetch"
        assert pending_content.requested_url == "https://boards.greenhouse.io/acme-freight/jobs/100"
        artifact = session.get(Artifact, pending_content.artifact_id)
        workspace = session.get(Workspace, scenario.workspace_id)
        assert artifact is not None and workspace is not None
        assert EvidenceArtifactService(
            runtime_storage(durable_database, scenario.artifact_root)
        ).read(artifact, workspace) == (
            b"Acme Freight seeks a Platform Director in Canada. "
            b"Located in the Eastern time zone or one hour offset. "
            b"Lead a governed data platform and its product roadmap."
        )

    refusals = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).list_refusals(scenario.workspace_id)
    assert [(item.employer, item.condition) for item in refusals] == [("Acme Freight", "employer")]
    assert refusals[0].search_parameter_set_id == used.id
    assert refusals[0].search_parameter_version == 1
    assert parameters.describe(scenario.workspace_id).version == 2

    dispositions = DiscoveryDispositionService(durable_database)
    dismissed = dispositions.dismissed(scenario.workspace_id)
    assert [(item.pending_discovery_id, item.category, item.reason) for item in dismissed] == [
        (scenario.pending_id, "employer", "the employer is Acme Freight")
    ]
    selection = DispositionSelection.model_validate(
        {
            "action": "restore",
            "selection_mode": "explicit",
            "category": "employer",
            "reason": "Recheck the refusal under the current search rules.",
            "pending_discovery_ids": [scenario.pending_id],
        }
    )
    preview = dispositions.preview(scenario.workspace_id, selection)
    dispositions.apply(
        scenario.workspace_id,
        DispositionApply.model_validate(
            {**selection.model_dump(mode="json"), "snapshot_sha256": preview.snapshot_sha256}
        ),
        actor="local_user",
        origin="test",
        correlation_id=None,
    )
    assert dispositions.dismissed(scenario.workspace_id) == []
    assert [
        item.id
        for item in SourceScanService(
            durable_database,
            storage=runtime_storage(durable_database),
            posting_fetcher=UnexpectedPostingFetcher(),
        ).list_pending_discoveries(scenario.workspace_id)
    ] == [scenario.pending_id]


def test_an_unverifiable_publisher_read_cannot_refuse_or_dismiss_a_discovery(
    scenario: _Scenario, durable_database: Database
) -> None:
    """An in-memory body is not sufficient evidence for an automatic dismissal."""

    class UnverifiedFetcher(_PostingFetcher):
        verified = False

        def fetch(
            self,
            url: str,
            *,
            intent: PostingReadIntent = PostingReadIntent.CRAWL,
            board_url: str = "",
        ) -> FetchedPosting:
            result = super().fetch(url, intent=intent)
            return result if self.verified else replace(result, publisher_check=None)

    SearchParametersService(durable_database).save(
        scenario.workspace_id,
        JobSearchParameters(employers=Employers(excluded_employers=["Acme Freight"])),
    )
    fetcher = UnverifiedFetcher()
    worker = Worker(
        durable_database,
        worker_id="evaluate-unverified-publisher",
        handlers=default_handlers(
            durable_database,
            artifact_root=scenario.artifact_root,
            fetcher=fetcher,
            workflow_runner=_EvaluationRunner(),
        ),
    )
    before = _EvaluationRunner.evaluations

    _evaluate_with_unavailable_content(
        scenario, durable_database, "evaluate-unverified-publisher", worker
    )

    assert _EvaluationRunner.evaluations == before

    fetcher.verified = True
    recovered = _evaluate(
        scenario,
        "evaluate-after-verified-publisher",
        worker=worker,
        pending_discovery_id=scenario.pending_id,
    )
    assert recovered.progress["decision"] == "ineligible"
    repeated = _evaluate(
        scenario,
        "evaluate-verified-publisher-retry",
        worker=worker,
        pending_discovery_id=scenario.pending_id,
    )
    assert repeated.progress == recovered.progress
    with durable_database.session() as session:
        assert session.scalar(select(func.count()).select_from(PostingRefusal)) == 1
        assert (
            session.scalar(select(func.count()).select_from(PendingDiscoveryDispositionEvent)) == 1
        )


def _dismiss(
    scenario: _Scenario,
    database: Database,
    category: str,
    origin: str,
    pending_id: str | None = None,
) -> None:
    with database.session() as session:
        DiscoveryDispositionService(database).dismiss_refusal_in_session(
            session,
            scenario.workspace_id,
            pending_id or scenario.pending_id,
            category=category,
            reason="A stated condition refused this posting.",
            search_parameter_set_id=None,
            actor="worker",
            origin=origin,
            correlation_id=None,
        )


def _restore(
    scenario: _Scenario, database: Database, category: str, *, origin: str = "api"
) -> None:
    dispositions = DiscoveryDispositionService(database)
    restore = DispositionSelection.model_validate(
        {
            "action": "restore",
            "selection_mode": "explicit",
            "category": category,
            "reason": "Acme Freight is worth a full evaluation after review.",
            "pending_discovery_ids": [scenario.pending_id],
        }
    )
    preview = dispositions.preview(scenario.workspace_id, restore)
    dispositions.apply(
        scenario.workspace_id,
        DispositionApply.model_validate(
            {**restore.model_dump(mode="json"), "snapshot_sha256": preview.snapshot_sha256}
        ),
        actor="local_user",
        origin=origin,
        correlation_id=None,
    )


def _exclude_acme(scenario: _Scenario, database: Database) -> None:
    SearchParametersService(database).save(
        scenario.workspace_id,
        JobSearchParameters(employers=Employers(excluded_employers=["Acme Freight"])),
    )


def test_a_person_dismissal_after_queueing_skips_evaluation_without_spend(
    scenario: _Scenario, durable_database: Database
) -> None:
    """A person's dismissal after a task is queued must stop the paid run cold."""
    task = scenario.queue.enqueue(
        workspace_id=scenario.workspace_id,
        task_type="discovery.evaluate",
        payload={"pending_discovery_id": scenario.pending_id},
        idempotency_key="evaluate-then-dismissed",
    )
    dispositions = DiscoveryDispositionService(durable_database)
    selection = DispositionSelection.model_validate(
        {
            "action": "dismiss",
            "selection_mode": "explicit",
            "category": "other",
            "reason": "The person dismissed this after the task was queued.",
            "pending_discovery_ids": [scenario.pending_id],
        }
    )
    preview = dispositions.preview(scenario.workspace_id, selection)
    dispositions.apply(
        scenario.workspace_id,
        DispositionApply.model_validate(
            {**selection.model_dump(mode="json"), "snapshot_sha256": preview.snapshot_sha256}
        ),
        actor="local_user",
        origin="api",
        correlation_id=None,
    )
    before = _EvaluationRunner.evaluations

    assert scenario.worker.run_once()
    finished = scenario.queue.get(str(task.id))

    assert finished.status == "completed"
    assert finished.progress["skipped"] == "dismissed"
    assert _EvaluationRunner.evaluations == before
    with durable_database.session() as session:
        assert session.scalar(select(PostingRefusal)) is None
        events = session.scalars(
            select(PendingDiscoveryDispositionEvent).where(
                PendingDiscoveryDispositionEvent.pending_discovery_id == scenario.pending_id
            )
        ).all()
        assert len(events) == 1, "the run must not append a second dismissal on top"
    assert dispositions.dismissed(scenario.workspace_id)[0].category == "other"


class _UnexpectedRunner(ScriptedPipelineRunner):
    """Fails the run if a held-out discovery reaches the paid evaluation stage."""

    def evaluate(self, *args: Any, **kwargs: Any) -> EvaluationOutput:
        """Refuse the call a skipped row must never make."""
        del args, kwargs
        raise AssertionError("A row the program held out must never reach a provider.")


@pytest.mark.parametrize("origin", HOLDOUT_ORIGINS)
def test_a_program_holdout_skips_evaluation_without_spend_or_a_persons_name(
    scenario: _Scenario, durable_database: Database, origin: str
) -> None:
    """A hold-out the program made stops the paid run and is never called the person's."""
    dispositions = DiscoveryDispositionService(durable_database)
    with durable_database.session() as session:
        dispositions.dismiss_structured_holdout_in_session(
            session,
            scenario.workspace_id,
            scenario.pending_id,
            category="location_outside",
            reason=(
                "Verified board facts meet a structured search hold-out. "
                "The dismissal remains individually restorable."
            ),
            actor="worker",
            origin=origin,
            correlation_id=None,
            screening_override={"standing": "refuse", "findings": []},
        )
    worker = Worker(
        durable_database,
        worker_id="evaluate-holdout",
        handlers=default_handlers(
            durable_database,
            artifact_root=scenario.artifact_root,
            fetcher=_FailingPostingFetcher(),
            workflow_runner=_UnexpectedRunner(),
        ),
    )

    finished = _evaluate(
        scenario, f"evaluate-{origin}", worker=worker, pending_discovery_id=scenario.pending_id
    )

    assert finished.progress["skipped"] == "held_out"
    assert "disposition_event_id" not in finished.progress
    with durable_database.session() as session:
        assert session.scalar(select(ModelCall)) is None
        assert session.scalar(select(CostRecord)) is None
        assert session.scalar(select(PostingRefusal)) is None
        assert (
            session.scalar(select(func.count()).select_from(PendingDiscoveryDispositionEvent)) == 1
        )


def test_a_withdrawal_after_queueing_skips_evaluation_without_spend(
    scenario: _Scenario, durable_database: Database
) -> None:
    task = scenario.queue.enqueue(
        workspace_id=scenario.workspace_id,
        task_type="discovery.evaluate",
        payload={"pending_discovery_id": scenario.pending_id},
        idempotency_key="evaluate-then-withdrawn",
    )
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, scenario.pending_id)
        assert pending is not None
        pending.withdrawn_at = datetime(2026, 8, 11, tzinfo=UTC)
        session.commit()
    before = _EvaluationRunner.evaluations

    assert scenario.worker.run_once()
    finished = scenario.queue.get(str(task.id))

    assert finished.status == "completed"
    assert finished.progress == {
        "pending_discovery_id": scenario.pending_id,
        "skipped": "withdrawn",
    }
    assert _EvaluationRunner.evaluations == before
    with durable_database.session() as session:
        assert session.scalar(select(ModelCall)) is None
        assert session.scalar(select(CostRecord)) is None


def test_a_tracked_posting_archived_after_queueing_fails_its_batch_row_without_spend(
    scenario: _Scenario, durable_database: Database
) -> None:
    """An archived posting is refused before its paid stages, not at the commit after them."""
    first = _evaluate(scenario, "evaluate-pending", pending_discovery_id=scenario.pending_id)
    job_id = str(first.progress["job_posting_id"])
    task = scenario.queue.enqueue(
        workspace_id=scenario.workspace_id,
        task_type="discovery.evaluate_batch",
        payload={"job_posting_ids": [job_id]},
        idempotency_key="rerun-then-archived",
    )
    job_search.archive_job(durable_database, job_id, scenario.workspace_id)
    before = _EvaluationRunner.evaluations

    assert scenario.worker.run_once()
    finished = scenario.queue.get(str(task.id))

    assert finished.status == "completed"
    assert _EvaluationRunner.evaluations == before
    assert finished.progress["failures"] == [
        {
            "job_posting_id": job_id,
            "error_category": "NotFoundError",
            "error_message": "The workflow job does not exist in this workspace.",
        }
    ]
    with durable_database.session() as session:
        versions = session.scalars(
            select(JobEvaluation.version_number).where(JobEvaluation.job_posting_id == job_id)
        ).all()
    assert versions == [1]


def test_a_restored_gate_does_not_pass_a_later_person_dismissal(
    scenario: _Scenario, durable_database: Database
) -> None:
    SearchParametersService(durable_database).save(
        scenario.workspace_id,
        JobSearchParameters(
            seniority=Seniority(excluded_titles=["Director"]),
            employers=Employers(excluded_employers=["Acme Freight"]),
        ),
    )
    first = _evaluate(scenario, "evaluate-title-gate", pending_discovery_id=scenario.pending_id)
    assert first.progress["refused_by"] == "job family"
    _restore(scenario, durable_database, "job family")

    dispositions = DiscoveryDispositionService(durable_database)
    unrelated = DispositionSelection.model_validate(
        {
            "action": "dismiss",
            "selection_mode": "explicit",
            "category": "other",
            "reason": "The person dismissed this for an unrelated reason.",
            "pending_discovery_ids": [scenario.pending_id],
        }
    )
    preview = dispositions.preview(scenario.workspace_id, unrelated)
    dispositions.apply(
        scenario.workspace_id,
        DispositionApply.model_validate(
            {**unrelated.model_dump(mode="json"), "snapshot_sha256": preview.snapshot_sha256}
        ),
        actor="local_user",
        origin="api",
        correlation_id=None,
    )
    before = _EvaluationRunner.evaluations

    skipped = _evaluate(scenario, "evaluate-dismissed", pending_discovery_id=scenario.pending_id)

    assert skipped.progress["skipped"] == "dismissed"
    assert _EvaluationRunner.evaluations == before
    _restore(scenario, durable_database, "other")

    second = _evaluate(scenario, "evaluate-other-gate", pending_discovery_id=scenario.pending_id)

    assert second.progress["refused_by"] == "employer"
    _restore(scenario, durable_database, "employer")

    third = _evaluate(scenario, "evaluate-all-restored", pending_discovery_id=scenario.pending_id)

    assert _EvaluationRunner.evaluations == before + 1
    assert "refused_by" not in third.progress


def test_evaluate_batch_skips_a_dismissed_row_and_evaluates_the_rest(
    scenario: _Scenario, durable_database: Database
) -> None:
    """One dismissed row is skipped without spend; the other rows still evaluate."""
    second_pending_id = _second_pending(scenario, durable_database)
    worker = _batch_worker(scenario, durable_database, "evaluate-batch-skip")
    dispositions = DiscoveryDispositionService(durable_database)
    selection = DispositionSelection.model_validate(
        {
            "action": "dismiss",
            "selection_mode": "explicit",
            "category": "other",
            "reason": "The person dismissed this row before the batch ran.",
            "pending_discovery_ids": [scenario.pending_id],
        }
    )
    preview = dispositions.preview(scenario.workspace_id, selection)
    dispositions.apply(
        scenario.workspace_id,
        DispositionApply.model_validate(
            {**selection.model_dump(mode="json"), "snapshot_sha256": preview.snapshot_sha256}
        ),
        actor="local_user",
        origin="api",
        correlation_id=None,
    )
    before = _EvaluationRunner.evaluations

    task = scenario.queue.enqueue(
        workspace_id=scenario.workspace_id,
        task_type="discovery.evaluate_batch",
        payload={
            "pending_discovery_ids": [scenario.pending_id, second_pending_id],
        },
        idempotency_key="evaluate-batch-skip",
    )
    assert worker.run_once()
    finished = scenario.queue.get(str(task.id))

    assert finished.error_message is None, finished.error_message
    assert finished.progress["evaluated"] == 1
    assert finished.progress["skipped"] == 1
    assert finished.progress["skipped_rows"][0]["pending_discovery_id"] == scenario.pending_id
    assert _EvaluationRunner.evaluations == before + 1
    [dismissed] = [
        event.payload
        for event in scenario.queue.events_after(str(task.id), limit=250)
        if event.stage == "dismissed_skipped"
    ]
    assert dismissed["skipped"] == "dismissed"
    assert dismissed["so_far"] == {"evaluated": 0, "refused": 0, "skipped": 0, "failed": 0}


def _person_disposition(
    scenario: _Scenario, database: Database, action: str, pending_id: str
) -> None:
    dispositions = DiscoveryDispositionService(database)
    selection = DispositionSelection.model_validate(
        {
            "action": action,
            "selection_mode": "explicit",
            "category": "other",
            "reason": "The person reviewed this family head.",
            "pending_discovery_ids": [pending_id],
        }
    )
    preview = dispositions.preview(scenario.workspace_id, selection)
    dispositions.apply(
        scenario.workspace_id,
        DispositionApply.model_validate(
            {**selection.model_dump(mode="json"), "snapshot_sha256": preview.snapshot_sha256}
        ),
        actor="local_user",
        origin="api",
        correlation_id=None,
    )


@pytest.mark.parametrize(("head", "evaluated"), [("person", 0), ("restored", 1), ("program", 0)])
def test_evaluate_batch_skips_a_variant_only_while_its_family_head_is_dismissed(
    scenario: _Scenario, durable_database: Database, head: str, evaluated: int
) -> None:
    """A variant staged under a dismissed head is skipped before spend, not failed at commit."""
    head_id = _second_pending(scenario, durable_database)
    worker = _batch_worker(scenario, durable_database, f"evaluate-variant-{head}")
    if head == "program":
        _dismiss(scenario, durable_database, "employer", "source.scan.body_screen", head_id)
    else:
        _person_disposition(scenario, durable_database, "dismiss", head_id)
    if head == "restored":
        _person_disposition(scenario, durable_database, "restore", head_id)
    with durable_database.session() as session:
        session.get_one(PendingDiscovery, scenario.pending_id).regional_variant_of = head_id
    before = _EvaluationRunner.evaluations

    task = scenario.queue.enqueue(
        workspace_id=scenario.workspace_id,
        task_type="discovery.evaluate_batch",
        payload={"pending_discovery_ids": [scenario.pending_id]},
        idempotency_key=f"evaluate-variant-{head}",
    )
    assert worker.run_once()
    finished = scenario.queue.get(str(task.id))

    assert finished.error_message is None, finished.error_message
    assert finished.progress["failed"] == 0
    assert finished.progress["evaluated"] == evaluated
    assert finished.progress["skipped"] == 1 - evaluated
    assert _EvaluationRunner.evaluations == before + evaluated


def test_a_discovery_with_no_staged_employer_is_skipped_without_spend(
    scenario: _Scenario, durable_database: Database
) -> None:
    """The ledger copies a name the program read, so an unnamed row is not evaluated."""
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, scenario.pending_id)
        assert pending is not None
        pending.employer = ""
        session.commit()
    before = _EvaluationRunner.evaluations

    finished = _evaluate(scenario, "evaluate-unnamed", pending_discovery_id=scenario.pending_id)

    assert finished.progress["pending_discovery_id"] == scenario.pending_id
    assert finished.progress["skipped"] == "unnamed"
    assert "publisher" in str(finished.progress["detail"])
    assert _EvaluationRunner.evaluations == before
    with durable_database.session() as session:
        assert session.scalar(select(JobEvaluation)) is None
        assert session.scalar(select(ModelCall)) is None
        assert session.scalar(select(CostRecord)) is None
        pending = session.get(PendingDiscovery, scenario.pending_id)
        assert pending is not None and pending.archived_at is None


def test_evaluate_batch_skips_an_unnamed_row_and_evaluates_the_rest(
    scenario: _Scenario, durable_database: Database
) -> None:
    second_pending_id = _second_pending(scenario, durable_database)
    worker = _batch_worker(scenario, durable_database, "evaluate-batch-unnamed")
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, scenario.pending_id)
        assert pending is not None
        pending.employer = ""
        session.commit()
    before = _EvaluationRunner.evaluations

    task = scenario.queue.enqueue(
        workspace_id=scenario.workspace_id,
        task_type="discovery.evaluate_batch",
        payload={
            "pending_discovery_ids": [scenario.pending_id, second_pending_id],
        },
        idempotency_key="evaluate-batch-unnamed",
    )
    assert worker.run_once()
    finished = scenario.queue.get(str(task.id))

    assert finished.error_message is None, finished.error_message
    assert finished.progress["evaluated"] == 1
    assert finished.progress["skipped"] == 1
    assert finished.progress["skipped_rows"][0] == {
        "pending_discovery_id": scenario.pending_id,
        "skipped": "unnamed",
        "detail": finished.progress["skipped_rows"][0]["detail"],
    }
    assert finished.progress["results"][0]["decision"] is not None
    assert _EvaluationRunner.evaluations == before + 1


def test_a_persons_restore_of_a_refusal_lets_full_evaluation_run(
    scenario: _Scenario, durable_database: Database
) -> None:
    _exclude_acme(scenario, durable_database)
    refused = _evaluate(scenario, "evaluate-refused", pending_discovery_id=scenario.pending_id)
    assert refused.progress["refused_by"] == "employer"
    _restore(scenario, durable_database, "employer")
    before = _EvaluationRunner.evaluations

    finished = _evaluate(scenario, "evaluate-restored", pending_discovery_id=scenario.pending_id)

    assert _EvaluationRunner.evaluations == before + 1
    assert "refused_by" not in finished.progress
    with durable_database.session() as session:
        evaluation = session.get(JobEvaluation, str(finished.progress["evaluation_id"]))
        assert evaluation is not None
        assert evaluation.job_posting_id == finished.progress["job_posting_id"]
        assert session.scalar(select(func.count()).select_from(PostingRefusal)) == 1


def test_a_favorite_family_passes_the_rule_gates_to_full_evaluation(
    scenario: _Scenario, durable_database: Database
) -> None:
    _exclude_acme(scenario, durable_database)
    with durable_database.exclusive_session() as session:
        set_pending_markers(
            session,
            scenario.workspace_id,
            scenario.pending_id,
            favorite=True,
            kept=False,
            keep_note=None,
        )
    before = _EvaluationRunner.evaluations

    finished = _evaluate(scenario, "evaluate-favorite", pending_discovery_id=scenario.pending_id)

    assert _EvaluationRunner.evaluations == before + 1
    assert "refused_by" not in finished.progress
    with durable_database.session() as session:
        assert session.scalar(select(func.count()).select_from(PostingRefusal)) == 0


def test_a_persons_restore_does_not_pass_a_closed_posting(
    scenario: _Scenario, durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dismiss(scenario, durable_database, "posting status", "evaluation.refusal")
    _restore(scenario, durable_database, "posting status")
    handlers = default_handlers(
        durable_database,
        artifact_root=scenario.artifact_root,
        fetcher=_PostingFetcher(),
        workflow_runner=_EvaluationRunner(),
    )
    handler = handlers["discovery.evaluate"]
    assert isinstance(handler, EvaluationHandler)
    read_inputs = handler.service.inputs

    def closed(*args: Any, **kwargs: Any) -> Any:
        return replace(read_inputs(*args, **kwargs), posting_status="closed")

    monkeypatch.setattr(handler.service, "inputs", closed)
    worker = Worker(durable_database, worker_id="evaluate-closed-restored", handlers=handlers)
    before = _EvaluationRunner.evaluations

    finished = _evaluate(
        scenario,
        "evaluate-closed-restored",
        worker=worker,
        pending_discovery_id=scenario.pending_id,
    )

    assert _EvaluationRunner.evaluations == before
    assert finished.progress["refused_by"] == "posting status"


def test_restoring_each_refusing_gate_in_turn_lets_full_evaluation_run(
    scenario: _Scenario, durable_database: Database
) -> None:
    SearchParametersService(durable_database).save(
        scenario.workspace_id,
        JobSearchParameters(
            seniority=Seniority(excluded_titles=["Director"]),
            employers=Employers(excluded_employers=["Acme Freight"]),
        ),
    )
    first = _evaluate(scenario, "evaluate-title-gate", pending_discovery_id=scenario.pending_id)
    assert first.progress["refused_by"] == "job family"
    _restore(scenario, durable_database, "job family")
    before = _EvaluationRunner.evaluations

    second = _evaluate(scenario, "evaluate-other-gate", pending_discovery_id=scenario.pending_id)

    assert _EvaluationRunner.evaluations == before
    assert second.progress["refused_by"] == "employer"
    dispositions = DiscoveryDispositionService(durable_database)
    assert [item.category for item in dispositions.dismissed(scenario.workspace_id)] == ["employer"]
    _restore(scenario, durable_database, "employer")

    third = _evaluate(scenario, "evaluate-both-restored", pending_discovery_id=scenario.pending_id)

    assert _EvaluationRunner.evaluations == before + 1
    assert "refused_by" not in third.progress
    assert dispositions.dismissed(scenario.workspace_id) == []
    with durable_database.session() as session:
        conditions = session.scalars(select(PostingRefusal.condition)).all()
    assert sorted(conditions) == ["employer", "job family"]


def test_an_automatic_body_screen_restore_does_not_unlock_evaluation(
    scenario: _Scenario, durable_database: Database
) -> None:
    _dismiss(scenario, durable_database, "employer", "source.scan.body_screen")
    _restore(scenario, durable_database, "employer", origin="source.scan.body_screen")
    _exclude_acme(scenario, durable_database)
    before = _EvaluationRunner.evaluations

    finished = _evaluate(
        scenario, "evaluate-body-restore", pending_discovery_id=scenario.pending_id
    )

    assert _EvaluationRunner.evaluations == before
    assert finished.progress["refused_by"] == "employer"


def test_a_content_persistence_failure_cannot_refuse_or_dismiss_a_discovery(
    scenario: _Scenario, durable_database: Database
) -> None:
    """A failure between fetch and durable evidence leaves the prospect pending."""

    class FailingContentResolution:
        def store_prefetched_result(self, *args: object, **kwargs: object) -> str:
            del args, kwargs
            raise ValidationError("Publisher body could not be retained.")

    handlers = default_handlers(
        durable_database,
        artifact_root=scenario.artifact_root,
        fetcher=_PostingFetcher(),
        workflow_runner=_EvaluationRunner(),
    )
    handlers["discovery.evaluate"] = EvaluationHandler(
        EvaluationWorkflowService(
            durable_database,
            runtime_storage(durable_database, scenario.artifact_root),
            fetcher=_PostingFetcher(),
            runner=_EvaluationRunner(),
            content_resolution=FailingContentResolution(),  # type: ignore[arg-type]
        )
    )
    worker = Worker(durable_database, worker_id="evaluate-persist-failure", handlers=handlers)
    before = _EvaluationRunner.evaluations

    _evaluate_that_cannot_produce_a_refusal(
        scenario, durable_database, "evaluate-persist-failure", worker
    )

    assert _EvaluationRunner.evaluations == before


def test_missing_rereadable_evidence_cannot_refuse_or_dismiss_a_discovery(
    scenario: _Scenario, durable_database: Database
) -> None:
    """A successful write call without readable evidence cannot decide eligibility."""

    class NoEvidenceContentResolution:
        def store_prefetched_result(self, *args: object, **kwargs: object) -> str:
            del args, kwargs
            return "unreadable-evidence"

    handlers = default_handlers(
        durable_database,
        artifact_root=scenario.artifact_root,
        fetcher=_PostingFetcher(),
        workflow_runner=_EvaluationRunner(),
    )
    handlers["discovery.evaluate"] = EvaluationHandler(
        EvaluationWorkflowService(
            durable_database,
            runtime_storage(durable_database, scenario.artifact_root),
            fetcher=_PostingFetcher(),
            runner=_EvaluationRunner(),
            content_resolution=NoEvidenceContentResolution(),  # type: ignore[arg-type]
        )
    )
    worker = Worker(durable_database, worker_id="evaluate-reread-failure", handlers=handlers)
    before = _EvaluationRunner.evaluations

    _evaluate_with_unavailable_content(
        scenario, durable_database, "evaluate-reread-failure", worker
    )

    assert _EvaluationRunner.evaluations == before


def test_a_saved_excluded_condition_does_not_refuse_a_posting_the_gates_accept(
    scenario: _Scenario, durable_database: Database
) -> None:
    """A stored refusal reason is text; text that lands in a title is no stated condition.

    "director" is in this posting's title and in nothing this person excluded,
    so the deterministic gates are what decide, and they let it through.
    """
    SearchParametersService(durable_database).save(
        scenario.workspace_id,
        JobSearchParameters(excluded_conditions=[("job family", "director")]),
    )
    before = _EvaluationRunner.evaluations

    finished = _evaluate(scenario, "evaluate-excluded", pending_discovery_id=scenario.pending_id)

    assert _EvaluationRunner.evaluations == before + 1
    assert "refused_by" not in finished.progress
    assert finished.progress["evaluation_version"] == 1
    with durable_database.session() as session:
        assert session.scalar(select(PostingRefusal)) is None


def test_re_enqueueing_one_discovery_evaluates_it_once(
    scenario: _Scenario, durable_database: Database
) -> None:
    """A retry after a dropped response must not file the same posting twice."""
    first = _evaluate(scenario, "evaluate-pending", pending_discovery_id=scenario.pending_id)
    before = _job_count(durable_database, scenario.workspace_id)

    replayed = scenario.queue.enqueue(
        workspace_id=scenario.workspace_id,
        task_type="discovery.evaluate",
        payload={"pending_discovery_id": scenario.pending_id},
        idempotency_key="evaluate-pending",
    )
    assert replayed.id == first.id
    assert _job_count(durable_database, scenario.workspace_id) == before


def test_a_rerun_supersedes_the_evaluation_rather_than_replacing_it(
    scenario: _Scenario, durable_database: Database
) -> None:
    """A decision made against an older reading stays readable after a newer one."""
    first = _evaluate(scenario, "evaluate-pending", pending_discovery_id=scenario.pending_id)
    job_id = str(first.progress["job_posting_id"])

    rerun = _evaluate(scenario, "rerun-evaluation", job_posting_id=job_id)
    assert rerun.progress["job_posting_id"] == job_id
    assert rerun.progress["evaluation_version"] == 2

    with durable_database.session() as session:
        history = session.scalars(
            select(JobEvaluation)
            .where(JobEvaluation.job_posting_id == job_id)
            .order_by(JobEvaluation.version_number)
        ).all()
    assert [item.version_number for item in history] == [1, 2]
    assert history[1].supersedes_evaluation_id == history[0].id


class _FreshnessProbe(_EvaluationRunner):
    """Record whether each evaluation asked to pass the response cache."""

    def __init__(self) -> None:
        super().__init__()
        self.fresh: list[bool] = []

    def evaluate(
        self,
        source: Path,
        settings: Any,
        *,
        master_cv: Path,
        service_tier: Any = None,
        ledger: Ledger | None = None,
        stored_name: StoredPostingName | None = None,
        listing_location: str = "",
        fresh: bool = False,
    ) -> EvaluationOutput:
        self.fresh.append(fresh)
        return super().evaluate(
            source,
            settings,
            master_cv=master_cv,
            service_tier=service_tier,
            ledger=ledger,
            stored_name=stored_name,
            listing_location=listing_location,
            fresh=fresh,
        )


def test_a_persons_fresh_rerun_passes_the_cache_and_a_batch_rerun_does_not(
    scenario: _Scenario, durable_database: Database
) -> None:
    probe = _FreshnessProbe()
    worker = _batch_worker(scenario, durable_database, "evaluate-fresh", probe)
    first = _evaluate(
        scenario, "evaluate-pending", worker=worker, pending_discovery_id=scenario.pending_id
    )
    job_id = str(first.progress["job_posting_id"])

    _evaluate(scenario, "rerun-fresh", worker=worker, job_posting_id=job_id, fresh=True)
    batch = scenario.queue.enqueue(
        workspace_id=scenario.workspace_id,
        task_type="discovery.evaluate_batch",
        payload={"job_posting_ids": [job_id]},
        idempotency_key="rerun-batch",
    )
    assert worker.run_once()

    assert scenario.queue.get(str(batch.id)).status == "completed"
    assert probe.fresh == [False, True, False]


def _read_as_closed(database: Database, job_id: str) -> None:
    """Append the closed reading a posting refresh records a day after the one before it."""
    with database.session() as session:
        latest = session.scalar(
            select(PostingObservation)
            .where(PostingObservation.job_posting_id == job_id)
            .order_by(PostingObservation.observed_at.desc())
            .limit(1)
        )
        assert latest is not None
        # Appended, not edited: observations are immutable at the database layer.
        session.add(
            PostingObservation(
                job_posting_id=job_id,
                observed_at=latest.observed_at + timedelta(days=1),
                posting_status="closed",
                body_artifact_id=latest.body_artifact_id,
                content_sha256=latest.content_sha256,
                origin="test",
            )
        )


def _canada_search(**changes: Any) -> JobSearchParameters:
    """Search parameters placing the scenario posting in the searched market."""
    return JobSearchParameters(
        locations=[Location(key="home", country="Canada")],
        countries=[CountryNames(country="Canada")],
        **changes,
    )


def _tracked_apply_now(scenario: _Scenario, database: Database) -> tuple[str, str]:
    """Evaluate the scenario discovery into a tracked apply-now job; return job and evaluation."""
    SearchParametersService(database).save(scenario.workspace_id, _canada_search())
    first = _evaluate(scenario, "evaluate-pending", pending_discovery_id=scenario.pending_id)
    job_id = str(first.progress["job_posting_id"])
    assert job_id in _apply_now_ids(database, scenario)
    return job_id, str(first.progress["evaluation_id"])


class _PlacelessPostingFetcher(_PostingFetcher):
    """Answers the scenario posting with a body that names no country."""

    def fetch(
        self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL, board_url: str = ""
    ) -> FetchedPosting:
        return replace(
            super().fetch(url, intent=intent),
            text=(
                "Acme Freight seeks a Platform Director. "
                "Lead a governed data platform and its product roadmap."
            ),
        )


def test_a_body_naming_no_country_is_placed_by_its_listing_location(
    scenario: _Scenario, durable_database: Database
) -> None:
    SearchParametersService(durable_database).save(scenario.workspace_id, _canada_search())
    worker = Worker(
        durable_database,
        worker_id="evaluate-placeless",
        handlers=default_handlers(
            durable_database,
            artifact_root=scenario.artifact_root,
            fetcher=_PlacelessPostingFetcher(),
            workflow_runner=_EvaluationRunner(),
        ),
    )

    finished = _evaluate(
        scenario, "evaluate-placeless", worker=worker, pending_discovery_id=scenario.pending_id
    )

    assert str(finished.progress["job_posting_id"]) in _apply_now_ids(durable_database, scenario)


def test_a_tracked_rerun_of_a_placeless_body_is_still_placed_by_its_listing_location(
    scenario: _Scenario, durable_database: Database
) -> None:
    SearchParametersService(durable_database).save(scenario.workspace_id, _canada_search())
    worker = Worker(
        durable_database,
        worker_id="evaluate-placeless-rerun",
        handlers=default_handlers(
            durable_database,
            artifact_root=scenario.artifact_root,
            fetcher=_PlacelessPostingFetcher(),
            workflow_runner=_EvaluationRunner(),
        ),
    )
    first = _evaluate(
        scenario,
        "evaluate-placeless-first",
        worker=worker,
        pending_discovery_id=scenario.pending_id,
    )
    job_id = str(first.progress["job_posting_id"])

    _evaluate(scenario, "evaluate-placeless-rerun", worker=worker, job_posting_id=job_id)

    assert job_id in _apply_now_ids(durable_database, scenario)


class _BodyPostingFetcher(_PostingFetcher):
    """Answers the scenario posting with the body a test names."""

    def __init__(self, body: str) -> None:
        super().__init__()
        self.body = body

    def fetch(
        self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL, board_url: str = ""
    ) -> FetchedPosting:
        return replace(super().fetch(url, intent=intent), text=self.body)


_REMOTE_ONLY = Arrangement(remote=Acceptance.PREFERRED, hybrid=Acceptance.REFUSED)


def _evaluate_listed(
    scenario: _Scenario,
    database: Database,
    *,
    body: str,
    location: str,
    staged: StagedListingFacts,
    parameters: JobSearchParameters,
) -> Any:
    """Evaluate the scenario discovery as listed under ``location`` with ``body`` as its page."""
    SearchParametersService(database).save(scenario.workspace_id, parameters)
    with database.session() as session:
        pending = session.get(PendingDiscovery, scenario.pending_id)
        assert pending is not None
        pending.location = location
        pending.staged_facts = staged.as_json()
    worker = Worker(
        database,
        worker_id="evaluate-listed",
        handlers=default_handlers(
            database,
            artifact_root=scenario.artifact_root,
            fetcher=_BodyPostingFetcher(body),
            workflow_runner=_EvaluationRunner(),
        ),
    )
    return _evaluate(
        scenario, "evaluate-listed", worker=worker, pending_discovery_id=scenario.pending_id
    )


@pytest.mark.parametrize(
    "staged",
    [
        StagedListingFacts(),
        StagedListingFacts(locations=PublishedFact(values=("Canada",))),
    ],
    ids=["no staged places", "incomplete staged places"],
)
def test_a_hybrid_body_under_incomplete_staged_places_is_refused_before_evaluation(
    scenario: _Scenario, durable_database: Database, staged: StagedListingFacts
) -> None:
    before = _EvaluationRunner.evaluations

    finished = _evaluate_listed(
        scenario,
        durable_database,
        body="Acme Freight seeks a Platform Director in Canada. This is a hybrid role, three "
        "days a week in our Toronto office.",
        location="Canada",
        staged=staged,
        parameters=_canada_search(arrangement=_REMOTE_ONLY),
    )

    assert finished.progress["refused_by"] == "arrangement"
    assert _EvaluationRunner.evaluations == before


def test_a_placeless_hybrid_body_is_read_against_its_listing_location_before_evaluation(
    scenario: _Scenario, durable_database: Database
) -> None:
    parameters = JobSearchParameters(
        locations=[
            Location(key="home", country="Canada"),
            Location(
                key="ywg",
                country="Canada",
                region="Manitoba",
                metro="Winnipeg",
                arrangement=Arrangement(hybrid=Acceptance.ACCEPTABLE),
            ),
        ],
        countries=[CountryNames(country="Canada")],
        arrangement=_REMOTE_ONLY,
    )
    before = _EvaluationRunner.evaluations

    finished = _evaluate_listed(
        scenario,
        durable_database,
        body="Acme Freight seeks a Platform Director. Work Model: Hybrid, two days a week in "
        "the office.",
        location="Winnipeg, Manitoba, Canada",
        staged=StagedListingFacts(
            locations=PublishedFact(values=("Winnipeg, Manitoba, Canada",), complete=True)
        ),
        parameters=parameters,
    )

    assert "refused_by" not in finished.progress
    assert _EvaluationRunner.evaluations == before + 1


def test_a_hybrid_body_under_a_fully_placed_office_listing_is_still_refused_before_evaluation(
    scenario: _Scenario, durable_database: Database
) -> None:
    before = _EvaluationRunner.evaluations

    finished = _evaluate_listed(
        scenario,
        durable_database,
        body="Acme Freight seeks a Platform Director in Canada. This is a hybrid role, three "
        "days a week in our Toronto office.",
        location="Canada",
        staged=StagedListingFacts(locations=PublishedFact(values=("Canada",), complete=True)),
        parameters=_canada_search(arrangement=_REMOTE_ONLY),
    )

    assert finished.progress["refused_by"] == "arrangement"
    assert _EvaluationRunner.evaluations == before
    with durable_database.session() as session:
        refusal_row = session.scalar(select(PostingRefusal))
        assert refusal_row is not None
        assert "This is a hybrid role" in refusal_row.reason


def _apply_now_ids(database: Database, scenario: _Scenario) -> list[str]:
    """Return the jobs the ledger's Apply Now list shows."""
    listed, _ = job_search.list_jobs(
        database, JobListQuery(workspace_id=scenario.workspace_id, decisions=("apply_now",))
    )
    return [item.job_posting_id for item in listed]


def _place(database: Database, scenario: _Scenario, job_id: str) -> str | None:
    """Return the place the ledger list shows for one job."""
    listed, _ = job_search.list_jobs(database, JobListQuery(workspace_id=scenario.workspace_id))
    return next(item.place for item in listed if item.job_posting_id == job_id)


def _batch_progress(scenario: _Scenario, key: str, **payload: list[str]) -> dict[str, Any]:
    """Run one evaluation batch and return its finished summary."""
    task = scenario.queue.enqueue(
        workspace_id=scenario.workspace_id,
        task_type="discovery.evaluate_batch",
        payload=payload,
        idempotency_key=key,
    )
    assert scenario.worker.run_once()
    finished = scenario.queue.get(str(task.id))
    assert finished.error_message is None, finished.error_message
    assert finished.progress["failed"] == 0, finished.progress["failures"]
    return dict(finished.progress)


def _rerun_in_batch(scenario: _Scenario, job_id: str, key: str) -> dict[str, Any]:
    """Re-evaluate one tracked posting through the ledger's batch task; return its row."""
    [result] = _batch_progress(scenario, key, job_posting_ids=[job_id])["results"]
    return dict(result)


def _rerun_alone(scenario: _Scenario, job_id: str, key: str) -> dict[str, Any]:
    """Re-evaluate one tracked posting the way the job page's rerun button does."""
    return dict(_evaluate(scenario, key, job_posting_id=job_id).progress)


_RERUNS = pytest.mark.parametrize("rerun", [_rerun_in_batch, _rerun_alone])


def _assert_refusal_supersedes(
    database: Database,
    scenario: _Scenario,
    result: dict[str, Any],
    *,
    job_id: str,
    previous_id: str,
    decision: str,
    place: str | None,
) -> None:
    """The refusal is filed, versioned over the old decision, and read as current."""
    with database.session() as session:
        refusal_row = session.scalar(
            select(PostingRefusal).where(PostingRefusal.job_posting_id == job_id)
        )
        assert refusal_row is not None and refusal_row.condition == result["refused_by"]
        evaluation = session.get(JobEvaluation, str(result["evaluation_id"]))
        assert evaluation is not None
        assert evaluation.version_number == result["evaluation_version"] == 2
        assert evaluation.supersedes_evaluation_id == previous_id
        assert evaluation.decision == result["decision"] == decision
        assert refusal_row.reason in evaluation.rationale
        assert evaluation.search_parameter_set_id == refusal_row.search_parameter_set_id
    assert job_id not in _apply_now_ids(database, scenario)
    assert _place(database, scenario, job_id) == place
    detail = job_search.job_detail(database, job_id, scenario.workspace_id)
    assert detail.decision is not None
    assert detail.decision.value == decision
    assert detail.evaluation_review.status == "current"
    report = job_search.fit_report(
        database,
        EvidenceArtifactService(runtime_storage(database, scenario.artifact_root)),
        job_id,
        scenario.workspace_id,
    ).markdown
    assert {item.name: item.status for item in detail.evaluation_inputs}["prompts"] == "unknown"
    assert [(item.type, item.text) for item in detail.factors if item.type == "pro"] == [
        ("pro", "The stored posting body is kept with this rule decision.")
    ]
    assert [item.evidence_ids for item in detail.factors if item.type == "con"] == [[]]
    assert (
        "## Hard gates\n\nNot read: a rule refused this posting before its requirements "
        "were extracted."
    ) in report


@_RERUNS
def test_a_tracked_posting_a_stated_condition_refuses_leaves_apply_now(
    scenario: _Scenario,
    durable_database: Database,
    rerun: Callable[[_Scenario, str, str], dict[str, Any]],
) -> None:
    """A rerun refused by the person's current rules supersedes the old decision for free."""
    job_id, previous_id = _tracked_apply_now(scenario, durable_database)
    place = _place(durable_database, scenario, job_id)
    SearchParametersService(durable_database).save(
        scenario.workspace_id,
        _canada_search(seniority=Seniority(excluded_titles=["Director"])),
    )
    before = _EvaluationRunner.evaluations

    result = rerun(scenario, job_id, "rerun-refused")

    assert _EvaluationRunner.evaluations == before, "a refused posting ran the pipeline"
    assert result["refused_by"] == "job family"
    _assert_refusal_supersedes(
        durable_database,
        scenario,
        result,
        job_id=job_id,
        previous_id=previous_id,
        decision="ineligible",
        place=place,
    )


@_RERUNS
def test_a_tracked_posting_read_as_closed_leaves_apply_now_as_closed_evidence(
    scenario: _Scenario,
    durable_database: Database,
    rerun: Callable[[_Scenario, str, str], dict[str, Any]],
) -> None:
    """A closed reading answers a rerun before any paid call and supersedes the old decision."""
    job_id, previous_id = _tracked_apply_now(scenario, durable_database)
    place = _place(durable_database, scenario, job_id)
    _read_as_closed(durable_database, job_id)
    before = _EvaluationRunner.evaluations

    result = rerun(scenario, job_id, "rerun-closed")

    assert _EvaluationRunner.evaluations == before, "a closed posting ran the pipeline"
    assert result["refused_by"] == "posting status"
    assert "closed" in result["reason"]
    _assert_refusal_supersedes(
        durable_database,
        scenario,
        result,
        job_id=job_id,
        previous_id=previous_id,
        decision="closed_evidence",
        place=place,
    )


def test_a_repeated_refusal_of_a_tracked_posting_files_one_refusal(
    scenario: _Scenario, durable_database: Database
) -> None:
    """The learning loop counts a posting once however often the same rule refuses it."""
    job_id, _ = _tracked_apply_now(scenario, durable_database)
    SearchParametersService(durable_database).save(
        scenario.workspace_id,
        _canada_search(seniority=Seniority(excluded_titles=["Director"])),
    )

    first = _rerun_in_batch(scenario, job_id, "rerun-refused")
    second = _rerun_alone(scenario, job_id, "rerun-refused-again")

    assert first["refused_by"] == second["refused_by"] == "job family"
    with durable_database.session() as session:
        filed = session.scalars(
            select(PostingRefusal).where(PostingRefusal.job_posting_id == job_id)
        ).all()
    assert len(filed) == 1


def test_a_batch_summary_counts_rule_refusals_apart_from_full_evaluations(
    scenario: _Scenario, durable_database: Database
) -> None:
    """Every selected row lands in exactly one of evaluated, refused, skipped, or failed."""
    second_pending_id = _second_pending(scenario, durable_database)
    scenario = scenario._replace(
        worker=_batch_worker(scenario, durable_database, "evaluate-batch-refused")
    )
    _exclude_acme(scenario, durable_database)
    before = _EvaluationRunner.evaluations

    progress = _batch_progress(
        scenario,
        "evaluate-batch-refused",
        pending_discovery_ids=[scenario.pending_id, second_pending_id],
    )

    assert _EvaluationRunner.evaluations == before + 1
    assert {key: progress[key] for key in ("selected", "evaluated", "refused", "skipped")} == {
        "selected": 2,
        "evaluated": 1,
        "refused": 1,
        "skipped": 0,
    }


class _ProgressProbeRunner(_EvaluationRunner):
    """Read the batch task's stored progress at the moment each row's provider call runs."""

    def __init__(self, queue: TaskQueueService) -> None:
        super().__init__()
        self.queue = queue
        self.task_id = ""
        self.seen: list[dict[str, Any]] = []

    def evaluate(
        self,
        source: Path,
        settings: Any,
        *,
        master_cv: Path,
        service_tier: Any = None,
        ledger: Ledger | None = None,
        stored_name: StoredPostingName | None = None,
        listing_location: str = "",
        fresh: bool = False,
    ) -> EvaluationOutput:
        self.seen.append(dict(self.queue.get(self.task_id).progress))
        return super().evaluate(
            source,
            settings,
            master_cv=master_cv,
            service_tier=service_tier,
            ledger=ledger,
            stored_name=stored_name,
            listing_location=listing_location,
            fresh=fresh,
        )


def test_a_batch_row_being_evaluated_keeps_its_step_in_the_stored_progress(
    scenario: _Scenario, durable_database: Database
) -> None:
    """The Tasks page reads the step from stored progress while a row's nested stages run."""
    second_pending_id = _second_pending(scenario, durable_database)
    probe = _ProgressProbeRunner(scenario.queue)
    worker = _batch_worker(scenario, durable_database, "evaluate-batch-step", probe)
    task = scenario.queue.enqueue(
        workspace_id=scenario.workspace_id,
        task_type="discovery.evaluate_batch",
        payload={"pending_discovery_ids": [scenario.pending_id, second_pending_id]},
        idempotency_key="evaluate-batch-step",
    )
    probe.task_id = str(task.id)

    assert worker.run_once()

    # Parallel lanes share one stored progress slot, so a snapshot may hold another
    # row's stage; each row's own id is checked through the event log below.
    assert len(probe.seen) == 2
    assert all(seen.get("total") == 2 and seen.get("current") in (1, 2) for seen in probe.seen)
    events = scenario.queue.events_after(str(task.id), limit=250)
    steps = [event.payload for event in events if event.stage == "evaluating"]
    assert {str(step.get("pending_discovery_id")) for step in steps} == {
        scenario.pending_id,
        second_pending_id,
    }
    verified = [event.payload for event in events if event.stage == "inputs_verified"]
    committed = [event.payload for event in events if event.stage == "evaluation_committed"]
    assert sorted(payload["pending_discovery_id"] for payload in verified) == sorted(
        [scenario.pending_id, second_pending_id]
    )
    assert len(committed) == 2
    assert all(payload["evaluation_id"] and payload["decision"] for payload in committed)


class _ManualAddFetcher:
    """Fetcher for a manually-added lever.co URL used by the manual-add test."""

    url = "https://jobs.lever.co/globex/0b6f3c2e-5a41-4d8e-9c7a-2f1e8d4b6a10"

    def fetch(
        self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL, board_url: str = ""
    ) -> FetchedPosting:
        assert url == self.url
        return FetchedPosting(
            text=(
                "Globex seeks a Platform Director in Canada. "
                "Lead a governed data platform and its product roadmap."
            ),
            posting_status="active",
            method="direct_text",
            fetched_at=datetime(2026, 8, 10, 12, tzinfo=UTC),
            http_status=200,
            final_url=url,
            publisher_check=PostingCheck(
                status="readable",
                note="Fixture publisher response.",
                platform="lever",
                employer="Globex",
                title="Platform Director",
            ),
        )

    def listings(
        self,
        source_name: str,
        url: str,
        *,
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        # A board read only reaches here when evaluation is promoting the URL,
        # which a row staged from a single posting URL must not do.
        return SourceRead(
            listings=[],
            complete=True,
            raw_count=0,
            outcome=SourceReadOutcome.ATS_COMPLETE,
        )


def _evaluate_found_posting(
    durable_database: Database, tmp_path: Path, *, origin: str, worker_id: str
) -> str:
    """Stage the fixture posting under one staging origin, evaluate it, return the workspace."""
    artifact_root = tmp_path / "artifacts"
    workspace_id, _, _, master_version_id = studio_workspace(
        durable_database, tmp_path / "workspace", artifact_root
    )
    with durable_database.session() as session:
        version = session.get(MasterCvVersion, master_version_id)
        assert version is not None
        session.delete(version)
    MasterCvService(
        durable_database, runtime_storage(durable_database, artifact_root)
    ).import_source(
        workspace_id=workspace_id,
        filename="master.md",
        content=(tmp_path / "workspace" / "master.md").read_bytes(),
    )
    scans = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    staged = scans.stage_found_posting(
        workspace_id,
        _ManualAddFetcher.url,
        date(2026, 8, 10),
        title="Platform Director",
        origin=origin,
    )
    assert staged is not None
    pending_id = scans.list_pending_discoveries(workspace_id)[0].id
    queue = TaskQueueService(durable_database)
    worker = Worker(
        durable_database,
        worker_id=worker_id,
        handlers=default_handlers(
            durable_database,
            artifact_root=artifact_root,
            fetcher=_ManualAddFetcher(),
            workflow_runner=_EvaluationRunner(),
        ),
    )
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="discovery.evaluate",
        payload={"pending_discovery_id": pending_id},
        idempotency_key=worker_id,
    )
    assert worker.run_once()
    finished = queue.get(str(task.id))
    assert finished.error_message is None, finished.error_message
    return workspace_id


def test_evaluating_a_manually_added_posting_does_not_register_a_source(
    durable_database: Database, tmp_path: Path
) -> None:
    """A manual add stays a one-off discovery; evaluation must not promote its URL."""
    workspace_id = _evaluate_found_posting(
        durable_database, tmp_path, origin="tailorcv", worker_id="evaluate-manual-add"
    )

    with durable_database.session() as session:
        sources = session.scalars(select(Source).where(Source.workspace_id == workspace_id)).all()
        assert sources == [], "manual-add evaluation must not register any recurring source"


def test_evaluating_a_campaign_staged_posting_does_not_register_a_source(
    durable_database: Database, tmp_path: Path
) -> None:
    """A campaign proposes one posting URL with no board read behind it, so nothing is promoted."""
    workspace_id = _evaluate_found_posting(
        durable_database,
        tmp_path,
        origin="search.guided_campaign",
        worker_id="evaluate-campaign-add",
    )

    with durable_database.session() as session:
        sources = session.scalars(select(Source).where(Source.workspace_id == workspace_id)).all()
        assert sources == [], "campaign-staged evaluation must not register any recurring source"


def test_an_unverified_employer_hint_does_not_refuse_before_evaluation(
    scenario: _Scenario, durable_database: Database
) -> None:
    """Only a publisher-vouched employer may refuse a posting before evaluation."""
    SearchParametersService(durable_database).save(
        scenario.workspace_id,
        JobSearchParameters(employers=Employers(excluded_employers=["Acme Freight", "Globex"])),
    )
    scans = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    # A manual add keeps its employer as an unverified hint: nothing checked the publisher.
    staged = scans.stage_found_posting(
        scenario.workspace_id,
        _ManualAddFetcher.url,
        date(2026, 8, 10),
        title="Platform Director",
        employer="Globex",
    )
    assert staged is not None
    hinted_id = next(
        row.id
        for row in scans.list_pending_discoveries(scenario.workspace_id)
        if row.id != scenario.pending_id
    )

    class _BothPostingsFetcher(_PostingFetcher):
        def fetch(
            self,
            url: str,
            *,
            intent: PostingReadIntent = PostingReadIntent.CRAWL,
            board_url: str = "",
        ) -> FetchedPosting:
            if url == _ManualAddFetcher.url:
                return _ManualAddFetcher().fetch(url, intent=intent)
            return super().fetch(url, intent=intent)

    worker = Worker(
        durable_database,
        worker_id="evaluate-employer-identity",
        handlers=default_handlers(
            durable_database,
            artifact_root=scenario.artifact_root,
            fetcher=_BothPostingsFetcher(),
            workflow_runner=_EvaluationRunner(),
        ),
    )
    vouched = _evaluate(
        scenario,
        "evaluate-vouched-employer",
        worker=worker,
        pending_discovery_id=scenario.pending_id,
    )
    assert vouched.progress["refused_by"] == "employer"
    before = _EvaluationRunner.evaluations

    finished = _evaluate(
        scenario, "evaluate-hinted-employer", worker=worker, pending_discovery_id=hinted_id
    )

    assert "refused_by" not in finished.progress
    assert _EvaluationRunner.evaluations == before + 1


def test_a_job_that_never_came_from_a_discovery_can_still_be_evaluated(
    scenario: _Scenario, durable_database: Database
) -> None:
    """The ledger holds postings that arrived by other routes, and they score too."""
    before = _job_count(durable_database, scenario.workspace_id)

    finished = _evaluate(
        scenario, "evaluate-existing-job", job_posting_id=scenario.identifier_free_job_id
    )

    assert finished.progress["job_posting_id"] == scenario.identifier_free_job_id
    assert _job_count(durable_database, scenario.workspace_id) == before, (
        "evaluating a job already in the ledger must not file a second one"
    )


def test_a_duplicate_discovery_short_circuits_before_the_provider_runs(
    scenario: _Scenario, durable_database: Database
) -> None:
    """A pending that names an evaluated posting's requisition or URL must not spend."""
    first = _evaluate(scenario, "evaluate-pending", pending_discovery_id=scenario.pending_id)
    first_job_id = str(first.progress["job_posting_id"])
    first_evaluation_id = str(first.progress["evaluation_id"])
    jobs_before = _job_count(durable_database, scenario.workspace_id)
    runs_before = _EvaluationRunner.evaluations

    duplicate_pending_id = new_id()
    with durable_database.session() as session:
        session.add(
            PendingDiscovery(
                id=duplicate_pending_id,
                workspace_id=scenario.workspace_id,
                fingerprint="duplicate-fingerprint",
                employer="Acme Freight",
                title="Platform Director",
                location="Canada",
                staged_facts={},
                requisition_id="REQ-100",
                discovery_url="https://boards.greenhouse.io/acme-freight/jobs/100",
                canonical_url="https://boards.greenhouse.io/acme-freight/jobs/100",
                reason="board rescan restaged an already-evaluated posting",
                first_seen_on=date(2026, 8, 11),
            )
        )

    finished = _evaluate(scenario, "evaluate-duplicate", pending_discovery_id=duplicate_pending_id)

    assert finished.progress == {
        "decision": "already_evaluated",
        "pending_discovery_id": duplicate_pending_id,
        "job_posting_id": first_job_id,
        "evaluation_id": first_evaluation_id,
        "existing_decision": finished.progress["existing_decision"],
    }
    assert _EvaluationRunner.evaluations == runs_before, "duplicate ran the provider"
    assert _job_count(durable_database, scenario.workspace_id) == jobs_before
    with durable_database.session() as session:
        assert session.scalar(select(PostingRefusal)) is None
        row = session.get(PendingDiscovery, duplicate_pending_id)
        assert row is not None and row.archived_at is None, (
            "a duplicate stays in the queue for explicit reconciliation"
        )


class _WrappedLeverFetcher(_FailingPostingFetcher):
    """Answers a Lever posting and the employer careers page that wraps it."""

    JOB: ClassVar[str] = "3c1d5e7f-2a4b-4c6d-8e0f-9a1b3c5d7e9f"
    LEVER_URL: ClassVar[str] = f"https://jobs.lever.co/acme-freight/{JOB}"
    CAREERS_URL: ClassVar[str] = (
        f"https://careers.acme-freight.example/job/{JOB}/platform-director/"
    )

    def fetch(
        self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL, board_url: str = ""
    ) -> FetchedPosting:
        del intent
        wrapper = url == self.CAREERS_URL
        page = f'<main><h1>Platform Director</h1><a href="{self.LEVER_URL}/apply">Apply</a></main>'
        return FetchedPosting(
            text=(
                "Acme Freight seeks a Platform Director in Canada. "
                "Lead a governed data platform and its product roadmap."
            ),
            posting_status="active",
            method="direct_text",
            fetched_at=datetime(2026, 8, 10, 12, tzinfo=UTC),
            http_status=200,
            final_url=url,
            publisher_check=PostingCheck(
                status="readable",
                note="Fixture publisher response.",
                platform="schema_org" if wrapper else "lever",
                employer="Acme Freight",
                title="Platform Director",
            ),
            source=PostingSource(publisher="page", page_html=page) if wrapper else None,
        )


def test_a_careers_page_wrapping_an_evaluated_lever_posting_short_circuits_before_the_provider(
    scenario: _Scenario, durable_database: Database
) -> None:
    """A careers page listed after the Lever posting is evaluated joins that job, not the queue."""
    fetcher = _WrappedLeverFetcher
    worker = Worker(
        durable_database,
        worker_id="evaluate-wrapped-lever",
        handlers=default_handlers(
            durable_database,
            artifact_root=scenario.artifact_root,
            fetcher=fetcher(),
            workflow_runner=_EvaluationRunner(),
        ),
    )
    scans = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=fetcher(),
    )

    def stage(url: str, requisition_id: str) -> None:
        scans.stage_listings(
            scenario.workspace_id,
            [
                SourceListing(
                    employer="Acme Freight",
                    title="Platform Director",
                    location="Canada",
                    requisition_id=requisition_id,
                    url=url,
                )
            ],
            date(2026, 8, 11),
            reason="guided search verified this posting",
        )

    stage(fetcher.LEVER_URL, fetcher.JOB)
    lever_id = next(
        row.id
        for row in scans.list_pending_discoveries(scenario.workspace_id)
        if row.discovery_url == fetcher.LEVER_URL
    )
    _evaluate(scenario, "evaluate-lever", worker=worker, pending_discovery_id=lever_id)
    runs_before = _EvaluationRunner.evaluations
    jobs_before = _job_count(durable_database, scenario.workspace_id)

    stage(fetcher.CAREERS_URL, "")

    visible = [row.discovery_url for row in scans.list_pending_discoveries(scenario.workspace_id)]
    assert fetcher.CAREERS_URL not in visible
    assert _EvaluationRunner.evaluations == runs_before, "the wrapper ran the provider"
    assert _job_count(durable_database, scenario.workspace_id) == jobs_before


class _MultiPassFetcher:
    """First call returns the stored body; later calls return a refreshed body."""

    URL: ClassVar[str] = "https://boards.greenhouse.io/acme-freight/jobs/100"
    FRESH_METHOD: ClassVar[str] = "fresh_direct_text"

    def __init__(self) -> None:
        self.calls = 0

    def fetch(
        self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL, board_url: str = ""
    ) -> FetchedPosting:
        assert url == self.URL
        self.calls += 1
        if self.calls == 1:
            return FetchedPosting(
                text=(
                    "Acme Freight seeks a Platform Director in Canada. "
                    "Lead a governed data platform and its product roadmap."
                ),
                posting_status="active",
                method="direct_text",
                fetched_at=datetime(2026, 8, 10, 12, tzinfo=UTC),
                http_status=200,
                final_url=url,
                publisher_check=PostingCheck(
                    status="readable",
                    note="Fixture publisher response.",
                    platform="greenhouse",
                    employer="Acme Freight",
                    title="Platform Director",
                ),
            )
        return FetchedPosting(
            text=(
                "Acme Freight seeks a Platform Director II in Canada. "
                "Refined posting body with an updated summary."
            ),
            posting_status="active",
            method=self.FRESH_METHOD,
            fetched_at=datetime(2026, 8, 11, 12, tzinfo=UTC),
            http_status=200,
        )

    def listings(
        self,
        source_name: str,
        url: str,
        *,
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        assert source_name == "Acme Freight"
        assert url == self.URL
        return SourceRead(
            listings=[],
            complete=True,
            raw_count=0,
            outcome=SourceReadOutcome.ATS_COMPLETE,
        )


class _RefreshFailureFetcher(_PostingFetcher):
    """Fail a rerun refresh after the standard fetcher stores the initial body."""

    def fetch(
        self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL, board_url: str = ""
    ) -> FetchedPosting:
        del url, intent
        raise OSError("synthetic refresh failure")


def test_a_rerun_refetches_a_stale_active_posting_and_uses_the_fresh_body(
    durable_database: Database, tmp_path: Path
) -> None:
    """An ingest-layer fix reaches a rerun without waiting for a new discovery."""
    artifact_root = tmp_path / "artifacts"
    workspace_id, _, _, master_version_id = studio_workspace(
        durable_database, tmp_path / "workspace", artifact_root
    )
    with durable_database.session() as session:
        version = session.get(MasterCvVersion, master_version_id)
        assert version is not None
        session.delete(version)
    MasterCvService(
        durable_database, runtime_storage(durable_database, artifact_root)
    ).import_source(
        workspace_id=workspace_id,
        filename="master.md",
        content=(tmp_path / "workspace" / "master.md").read_bytes(),
    )
    scans = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    scans.stage_listings(
        workspace_id,
        [
            SourceListing(
                employer="Acme Freight",
                title="Platform Director",
                location="Canada",
                requisition_id="REQ-100",
                url=_MultiPassFetcher.URL,
            )
        ],
        date(2026, 8, 10),
        reason="guided search verified this posting",
    )
    pending_id = scans.list_pending_discoveries(workspace_id)[0].id
    fetcher = _MultiPassFetcher()
    queue = TaskQueueService(durable_database)
    worker = Worker(
        durable_database,
        worker_id="evaluate-refetch",
        handlers=default_handlers(
            durable_database,
            artifact_root=artifact_root,
            fetcher=fetcher,
            workflow_runner=_EvaluationRunner(),
        ),
    )
    first = queue.enqueue(
        workspace_id=workspace_id,
        task_type="discovery.evaluate",
        payload={"pending_discovery_id": pending_id},
        idempotency_key="refetch-first",
    )
    assert worker.run_once()
    finished = queue.get(str(first.id))
    assert finished.error_message is None, finished.error_message
    job_id = str(finished.progress["job_posting_id"])

    rerun = queue.enqueue(
        workspace_id=workspace_id,
        task_type="discovery.evaluate",
        payload={"job_posting_id": job_id},
        idempotency_key="refetch-rerun",
    )
    assert worker.run_once()
    rerun_finished = queue.get(str(rerun.id))
    assert rerun_finished.error_message is None, rerun_finished.error_message

    assert fetcher.calls == 2, "a rerun with a stale active observation refetches"
    with durable_database.session() as session:
        observations = session.scalars(
            select(PostingObservation)
            .where(PostingObservation.job_posting_id == job_id)
            .order_by(PostingObservation.observed_at.desc(), PostingObservation.id.desc())
        ).all()
        assert observations[0].extraction_method == _MultiPassFetcher.FRESH_METHOD, (
            "the newest observation records the fresh fetch's extraction method"
        )


def test_a_stale_rerun_records_a_refresh_failure_and_uses_its_stored_body(
    scenario: _Scenario, durable_database: Database
) -> None:
    """A failed stale refresh remains visible while evaluation continues from stored evidence."""
    first = _evaluate(scenario, "refresh-failure-first", pending_discovery_id=scenario.pending_id)
    job_id = str(first.progress["job_posting_id"])
    worker = Worker(
        durable_database,
        worker_id="evaluate",
        handlers=default_handlers(
            durable_database,
            artifact_root=scenario.artifact_root,
            fetcher=_RefreshFailureFetcher(),
            workflow_runner=_EvaluationRunner(),
        ),
    )
    finished = _evaluate(
        scenario,
        "refresh-failure-rerun",
        worker=worker,
        job_posting_id=job_id,
    )
    assert finished.progress["job_posting_id"] == job_id
    # The completed result keeps what the transient failure event said.
    assert finished.progress["refresh_fallback"] == "stored_posting_text"
    assert "refresh_fallback" not in first.progress
    events = scenario.queue.events_after(str(finished.id), workspace_id=scenario.workspace_id)
    failure = next(event for event in events if event.stage == "posting_refresh_failed")
    assert failure.payload == {
        "refresh_fallback": "stored_posting_text",
        "host": "boards.greenhouse.io",
        "error_type": "OSError",
    }


class _ScriptedRefreshFetcher(_PostingFetcher):
    """Answer the first read with the scenario body and every rerun read with ``refreshed``."""

    def __init__(self, refreshed: FetchedPosting) -> None:
        super().__init__()
        self.refreshed = refreshed

    def fetch(
        self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL, board_url: str = ""
    ) -> FetchedPosting:
        if not self.intents_seen:
            return super().fetch(url, intent=intent)
        self.intents_seen.append(intent)
        return self.refreshed


def _stale_rerun(
    scenario: _Scenario, durable_database: Database, refreshed: FetchedPosting
) -> tuple[Any, str]:
    """Evaluate the scenario discovery, then rerun its posting against ``refreshed``."""
    worker = Worker(
        durable_database,
        worker_id="evaluate",
        handlers=default_handlers(
            durable_database,
            artifact_root=scenario.artifact_root,
            fetcher=_ScriptedRefreshFetcher(refreshed),
            workflow_runner=_EvaluationRunner(),
        ),
    )
    first = _evaluate(
        scenario, "refresh-first", worker=worker, pending_discovery_id=scenario.pending_id
    )
    job_id = str(first.progress["job_posting_id"])
    return _evaluate(scenario, "refresh-rerun", worker=worker, job_posting_id=job_id), job_id


def test_a_stale_reruns_reread_is_a_crawl(scenario: _Scenario, durable_database: Database) -> None:
    """Guard: a rerun re-reads a stored address, which no person just entered."""
    fetcher = _ScriptedRefreshFetcher(
        FetchedPosting(
            text=None,
            posting_status="closed",
            method="closure_signal",
            fetched_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
            http_status=404,
        )
    )
    worker = Worker(
        durable_database,
        worker_id="evaluate",
        handlers=default_handlers(
            durable_database,
            artifact_root=scenario.artifact_root,
            fetcher=fetcher,
            workflow_runner=_EvaluationRunner(),
        ),
    )
    first = _evaluate(
        scenario, "crawl-first", worker=worker, pending_discovery_id=scenario.pending_id
    )
    reads_before_rerun = len(fetcher.intents_seen)

    _evaluate(
        scenario, "crawl-rerun", worker=worker, job_posting_id=str(first.progress["job_posting_id"])
    )

    assert fetcher.intents_seen[reads_before_rerun:] == [PostingReadIntent.CRAWL]


def test_a_stale_rerun_whose_reread_says_closed_is_refused_as_closed_without_a_model_call(
    scenario: _Scenario, durable_database: Database
) -> None:
    """A fresh closure reading refuses the rerun rather than evaluating the stored body as open."""
    closed = FetchedPosting(
        text=None,
        posting_status="closed",
        method="closure_signal",
        fetched_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
        http_status=404,
    )
    finished, job_id = _stale_rerun(scenario, durable_database, closed)

    assert finished.progress["decision"] == "closed_evidence"
    assert finished.progress["refused_by"] == "posting status"
    assert "refresh_fallback" not in finished.progress
    with durable_database.session() as session:
        assert (
            session.scalar(select(ModelCall).where(ModelCall.task_id == str(finished.id))) is None
        )
        newest, prior = session.scalars(
            select(PostingObservation)
            .where(PostingObservation.job_posting_id == job_id)
            .order_by(PostingObservation.observed_at.desc(), PostingObservation.id.desc())
        ).all()
        assert newest.posting_status == "closed"
        assert newest.extraction_method == "closure_signal"
        assert newest.http_status == 404
        assert newest.observed_at.replace(tzinfo=UTC) == closed.fetched_at
        assert newest.content_sha256 == prior.content_sha256


@pytest.mark.parametrize(
    ("refreshed", "reason"),
    [
        (
            FetchedPosting(
                text="",
                posting_status="active",
                method="direct_text",
                fetched_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
            ),
            "empty_body",
        ),
        (
            FetchedPosting(
                text=" \n\t ",
                posting_status="active",
                method="direct_text",
                fetched_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
            ),
            "empty_body",
        ),
        (
            FetchedPosting(
                text=None,
                posting_status="unverified",
                method="publisher_refused",
                fetched_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
                http_status=403,
                verification_note="The publisher answered 403 Forbidden.",
            ),
            "publisher_refused",
        ),
        (
            FetchedPosting(
                text=None,
                posting_status="unverified",
                method="destination_unresolvable",
                fetched_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
            ),
            "destination_unresolvable",
        ),
    ],
    ids=["empty-active-body", "whitespace-active-body", "publisher-refused", "unresolved-host"],
)
def test_a_stale_rerun_whose_reread_has_no_body_records_the_stored_text_fallback(
    scenario: _Scenario, durable_database: Database, refreshed: FetchedPosting, reason: str
) -> None:
    """A bodiless re-read leaves the rerun on stored text and says so, like a failed read."""
    finished, job_id = _stale_rerun(scenario, durable_database, refreshed)

    assert finished.progress["job_posting_id"] == job_id
    assert finished.progress["refresh_fallback"] == "stored_posting_text"
    events = scenario.queue.events_after(str(finished.id), workspace_id=scenario.workspace_id)
    failure = next(event for event in events if event.stage == "posting_refresh_failed")
    assert failure.payload == {
        "refresh_fallback": "stored_posting_text",
        "host": "boards.greenhouse.io",
        "reason": reason,
    }


# A listing excerpt an aggregator cut off mid-word at about 500 characters.
_CUT_OFF_POSTING = (
    "Acme Freight seeks a Platform Director in Canada. "
    + "Lead a governed data platform and its product roadmap with the analytics team. " * 6
)[:498] + "pla"
_CUT_OFF_SKIP = "The retained text appears cut off, so no evaluation was run."


class _CutOffPostingFetcher(_PostingFetcher):
    def fetch(
        self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL, board_url: str = ""
    ) -> FetchedPosting:
        return replace(super().fetch(url, intent=intent), text=_CUT_OFF_POSTING)


class _BoardOnlyFetcher(_PostingFetcher):
    """Answers the board verification after a commit and refuses a posting read."""

    def fetch(
        self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL, board_url: str = ""
    ) -> FetchedPosting:
        raise AssertionError(f"Attached text must not be re-read from {url}.")


def _assert_nothing_filed(database: Database, pending_id: str | None = None) -> None:
    with database.session() as session:
        assert session.scalar(select(PostingRefusal)) is None
        assert session.scalar(select(PendingDiscoveryDispositionEvent)) is None
        assert session.scalar(select(ModelCall)) is None
        if pending_id is not None:
            assert session.scalar(select(JobEvaluation)) is None
            pending = session.get(PendingDiscovery, pending_id)
            assert pending is not None and pending.archived_at is None


def test_a_cut_off_discovery_body_skips_evaluation_and_prices_at_zero(
    scenario: _Scenario, durable_database: Database
) -> None:
    worker = Worker(
        durable_database,
        worker_id="evaluate-cut-off",
        handlers=default_handlers(
            durable_database,
            artifact_root=scenario.artifact_root,
            fetcher=_CutOffPostingFetcher(),
            workflow_runner=_EvaluationRunner(),
        ),
    )
    before = _EvaluationRunner.evaluations

    finished = _evaluate(
        scenario, "evaluate-cut-off", worker=worker, pending_discovery_id=scenario.pending_id
    )

    assert _EvaluationRunner.evaluations == before
    assert finished.progress["skipped"] == "partial_text"
    assert finished.progress["detail"] == _CUT_OFF_SKIP
    _assert_nothing_filed(durable_database, scenario.pending_id)
    preflight = EvaluationWorkflowService(
        durable_database,
        runtime_storage(durable_database, scenario.artifact_root),
        fetcher=UnexpectedPostingFetcher(),
    ).preflight(
        scenario.workspace_id,
        DiscoveryEvaluateBatchPayload(pending_discovery_ids=[scenario.pending_id]),
    )
    [item] = preflight.items
    assert (item.expected_cost_usd, item.range_low_usd, item.range_high_usd) == (
        Decimal(0),
        Decimal(0),
        Decimal(0),
    )
    assert item.current_skip_reason == _CUT_OFF_SKIP
    assert item.stages == []


def test_a_tracked_posting_whose_reread_is_cut_off_skips_its_rerun(
    scenario: _Scenario, durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _evaluate(scenario, "evaluate-full-job", pending_discovery_id=scenario.pending_id)
    job_id = str(first.progress["job_posting_id"])
    worker = Worker(
        durable_database,
        worker_id="evaluate",
        handlers=default_handlers(
            durable_database,
            artifact_root=scenario.artifact_root,
            fetcher=_CutOffPostingFetcher(),
            workflow_runner=_EvaluationRunner(),
        ),
    )
    later = datetime.now(UTC) + timedelta(days=30)

    # A stale reading makes the rerun re-read the publisher through the fetcher.
    class LaterDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> LaterDatetime:
            return cls.fromtimestamp(later.timestamp(), tz=tz)

    monkeypatch.setattr(evaluation_workflows, "datetime", LaterDatetime)
    filed = (JobEvaluation, ModelCall, PostingRefusal, PendingDiscoveryDispositionEvent)

    def counts() -> list[int | None]:
        with durable_database.session() as session:
            return [session.scalar(select(func.count()).select_from(m)) for m in filed]

    counts_before = counts()
    before = _EvaluationRunner.evaluations

    finished = _evaluate(scenario, "evaluate-cut-off-job", worker=worker, job_posting_id=job_id)

    assert _EvaluationRunner.evaluations == before
    assert finished.progress["skipped"] == "partial_text"
    assert finished.progress["job_posting_id"] == job_id
    assert counts() == counts_before


def test_a_cut_off_body_the_screen_refuses_records_the_refusal(
    scenario: _Scenario, durable_database: Database
) -> None:
    SearchParametersService(durable_database).save(
        scenario.workspace_id,
        JobSearchParameters(seniority=Seniority(excluded_titles=["Director"])),
    )
    worker = Worker(
        durable_database,
        worker_id="evaluate-cut-off-refused",
        handlers=default_handlers(
            durable_database,
            artifact_root=scenario.artifact_root,
            fetcher=_CutOffPostingFetcher(),
            workflow_runner=_EvaluationRunner(),
        ),
    )

    finished = _evaluate(
        scenario,
        "evaluate-cut-off-refused",
        worker=worker,
        pending_discovery_id=scenario.pending_id,
    )

    assert finished.progress["refused_by"] == "job family"
    assert "skipped" not in finished.progress
    preflight = EvaluationWorkflowService(
        durable_database,
        runtime_storage(durable_database, scenario.artifact_root),
        fetcher=UnexpectedPostingFetcher(),
    ).preflight(
        scenario.workspace_id,
        DiscoveryEvaluateBatchPayload(pending_discovery_ids=[scenario.pending_id]),
    )
    [item] = preflight.items
    assert item.expected_cost_usd == Decimal(0)
    assert (item.current_skip_reason or "").startswith("Screening refuses this posting:")


def test_cut_off_text_a_person_attached_is_evaluated_as_a_discovery_and_as_a_tracked_job(
    scenario: _Scenario, durable_database: Database
) -> None:
    storage = runtime_storage(durable_database, scenario.artifact_root)
    with durable_database.session() as session:
        pending = session.get_one(PendingDiscovery, scenario.pending_id)
        current_id, url = pending.current_content_id, pending.discovery_url
    PostingContentResolutionService(
        durable_database, DeterministicPostingContentResolver(), storage=storage
    ).store_user_supplied_text(
        scenario.workspace_id,
        scenario.pending_id,
        _CUT_OFF_POSTING,
        attested_current_exact_posting=True,
        expected_current_content_id=current_id,
        expected_url=url,
        correlation_id=None,
    )
    worker = Worker(
        durable_database,
        worker_id="evaluate-attached",
        handlers=default_handlers(
            durable_database,
            artifact_root=scenario.artifact_root,
            fetcher=_BoardOnlyFetcher(),
            workflow_runner=_EvaluationRunner(),
        ),
    )
    before = _EvaluationRunner.evaluations

    discovery = _evaluate(
        scenario, "evaluate-attached", worker=worker, pending_discovery_id=scenario.pending_id
    )
    tracked = _evaluate(
        scenario,
        "evaluate-attached-job",
        worker=worker,
        job_posting_id=discovery.progress["job_posting_id"],
    )

    assert "skipped" not in discovery.progress
    assert "skipped" not in tracked.progress
    assert tracked.progress["evaluation_version"] == 2
    assert _EvaluationRunner.evaluations == before + 2
