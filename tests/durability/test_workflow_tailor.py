"""The tailor operation: its inputs, its command options, and the run it persists."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from clearscout.application.config import load_settings
from clearscout.application.documents import DocumentService
from clearscout.application.failures import NotFoundError, ValidationError
from clearscout.application.job_search.postings import RequirementRulingService
from clearscout.application.master_cv import MasterCvService
from clearscout.application.tailoring.guardrails import Guardrails
from clearscout.application.tailoring.pipeline import SerializedRunArtifacts, StoredMasterCv
from clearscout.application.tailoring.stages.jobspec import StoredPostingName
from clearscout.application.tailoring.stages.verify import KeywordCoverage, Report
from clearscout.application.tailoring.workflows import (
    PipelineWorkflowRunner,
    TailorOutput,
    WorkflowService,
)
from clearscout.application.tasks import TaskQueueService
from clearscout.application.workspaces import (
    GuardrailService,
    SearchParametersService,
    WorkspaceService,
)
from clearscout.bootstrap.api import create_app
from clearscout.bootstrap.worker import Worker, default_handlers
from clearscout.domain import ArtifactLogicalType, ResearchKind, TailoringOutcome
from clearscout.domain.identity import normalize_url
from clearscout.domain.rulings import Ruling
from clearscout.domain.search_parameters import JobSearchParameters, Location
from clearscout.domain.staged_facts import ListingIdentityTrust, StagedListingFacts
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    Artifact,
    CostRecord,
    Employer,
    JobEvaluation,
    JobPosting,
    MasterCvVersion,
    ModelCall,
    PendingDiscovery,
    PostingObservation,
    PostingRequirement,
    PostingUrl,
    ReviewProposal,
    TailoringRun,
)
from clearscout.infrastructure.web_fetch.company_research import (
    CompanyResearchFetcher,
    ResearchSource,
)
from clearscout.shared.costs import CallManifest, Ledger, ServiceTier, Usage
from clearscout.shared.results import Finding, Outcome
from tests.support.artifacts import runtime_storage
from tests.support.doubles import FakeRunResult
from tests.support.http import COMPANY_CAREERS_PAGE, policy_client
from tests.support.runners import ScriptedPipelineRunner, serialized_artifacts
from tests.support.scenarios import (
    FIXTURES,
    pending_posting_content,
    seed_pending_discovery,
    seed_reusable_tailoring_run,
    studio_workspace,
)

STUDIO_BODY = b"Acme needs a platform lead with Python experience."
STORED_CAREERS_TEXT = (
    "Acme builds freight scheduling software for regional carriers. Teams own a service "
    "from design through operation, publish their engineering handbook, and meet in "
    "person twice a year. Most roles are remote within the country, and every team "
    "keeps a public roadmap that candidates can read before they talk to us."
)


def _full_artifacts() -> SerializedRunArtifacts:
    return SerializedRunArtifacts(
        document=b"# Resume\n",
        report=b"# Report\n",
        manifest_json=b'{"inputs": {"prompt_suite": "test"}}\n',
        manifest={"inputs": {"prompt_suite": "test"}},
        cover_letter=b"# Letter\n",
        review_json=b'{"proposals": []}\n',
        review_markdown=b"# Changes\n",
        review={"proposals": []},
    )


def _minimal_artifacts() -> SerializedRunArtifacts:
    return SerializedRunArtifacts(
        document=b"# Resume\n",
        report=b"# Report\n",
        manifest_json=b"{}\n",
        manifest={},
        cover_letter=None,
        review_json=None,
        review_markdown=None,
        review={},
    )


def _expect_flex_tier_and_empty_approval(command: Any) -> None:
    assert command.service_tier is ServiceTier.FLEX
    assert command.approval is not None
    assert command.approval.proposals == ()


def _expect_nothing_further(command: Any) -> None:
    return None


@pytest.mark.parametrize(
    ("options", "expect", "findings", "build_artifacts", "outcome"),
    [
        (
            {
                "cover_letter": True,
                "force": False,
                "approval": {"schema": 1, "proposals": []},
                "service_tier": ServiceTier.FLEX,
                "stored_name": StoredPostingName(
                    title="Platform Product Lead", employer="Example Logistics"
                ),
            },
            _expect_flex_tier_and_empty_approval,
            (),
            _full_artifacts,
            TailoringOutcome.PASSED,
        ),
        (
            {"cover_letter": False, "force": True, "approval": None},
            _expect_nothing_further,
            (Finding("structure", Outcome.FAIL, "no H1"),),
            _minimal_artifacts,
            TailoringOutcome.CHECKS_FAILED,
        ),
    ],
)
def test_tailoring_passes_the_command_options_through_and_normalizes_the_run_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    options: dict[str, Any],
    expect: Any,
    findings: tuple[Finding, ...],
    build_artifacts: Any,
    outcome: TailoringOutcome,
) -> None:
    source = tmp_path / "posting.txt"
    master = tmp_path / "master.md"
    source.write_text("Synthetic posting", encoding="utf-8")
    master.write_text("# Synthetic Candidate\n", encoding="utf-8")
    settings = load_settings(start=tmp_path)
    ledger = Ledger()

    def fake_run(command: Any) -> FakeRunResult:
        assert command.materialize is False
        assert command.stored_name == options.get("stored_name")
        expect(command)
        return FakeRunResult(
            verification=Report(
                findings=findings,
                coverage=KeywordCoverage(present=(), missing=()),
            ),
            ledger=ledger,
            serialized=build_artifacts(),
        )

    monkeypatch.setattr("clearscout.application.tailoring.workflows.runner.pipeline.run", fake_run)

    tailored = PipelineWorkflowRunner().tailor(source, settings, master_cv=master, **options)

    assert tailored == TailorOutput(
        artifacts=build_artifacts(),
        outcome=outcome,
        ledger=ledger,
    )


def test_workflow_runner_consumes_the_serialized_package_without_reopening_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = Ledger()
    serialized = SerializedRunArtifacts(
        document=b"# Resume\n",
        report=b"# Report\n",
        manifest_json=b'{"run": {"outcome": "passed"}}\n',
        manifest={"run": {"outcome": "passed"}},
        cover_letter=None,
        review_json=None,
        review_markdown=None,
        review={},
    )
    monkeypatch.setattr(
        "clearscout.application.tailoring.workflows.runner.pipeline.run",
        lambda command: FakeRunResult(
            verification=Report(
                findings=(),
                coverage=KeywordCoverage(present=(), missing=()),
            ),
            ledger=ledger,
            serialized=serialized,
        ),
    )

    result = PipelineWorkflowRunner().tailor(
        tmp_path / "posting.txt",
        load_settings(start=tmp_path),
        master_cv=tmp_path / "master.md",
        cover_letter=False,
        force=False,
        approval=None,
    )

    assert result == TailorOutput(
        artifacts=SerializedRunArtifacts(
            document=b"# Resume\n",
            report=b"# Report\n",
            manifest_json=b'{"run": {"outcome": "passed"}}\n',
            manifest={"run": {"outcome": "passed"}},
            cover_letter=None,
            review_json=None,
            review_markdown=None,
            review={},
        ),
        outcome=TailoringOutcome.PASSED,
        ledger=ledger,
    )


def _posting_without_a_stored_body(
    service: WorkflowService,
    database: Database,
    workspace_id: str,
    job_id: str,
    master_version_id: str,
    tmp_path: Path,
) -> tuple[str, str]:
    with database.session() as session:
        employer = session.scalar(select(Employer).where(Employer.workspace_id == workspace_id))
        assert employer is not None
        job = JobPosting(
            workspace_id=workspace_id,
            employer_id=employer.id,
            title="No body",
            normalized_title="no body",
            first_seen_at=datetime.now(UTC),
        )
        session.add(job)
        session.flush()
        return workspace_id, job.id


def _posting_body_owned_by_another_workspace(
    service: WorkflowService,
    database: Database,
    workspace_id: str,
    job_id: str,
    master_version_id: str,
    tmp_path: Path,
) -> tuple[str, str]:
    _, no_body_job_id = _posting_without_a_stored_body(
        service, database, workspace_id, job_id, master_version_id, tmp_path
    )
    other_root = tmp_path / "other-workspace"
    other_root.mkdir()
    other = WorkspaceService(database, runtime_storage(database)).create(
        slug="other-workflow", name="Other workflow", root_path=str(other_root)
    )
    with database.session() as session:
        other_employer = Employer(workspace_id=str(other.id), name="Other", normalized_name="other")
        session.add(other_employer)
        session.flush()
        other_job = JobPosting(
            workspace_id=str(other.id),
            employer_id=other_employer.id,
            title="Other job",
            normalized_title="other job",
            first_seen_at=datetime.now(UTC),
        )
        session.add(other_job)
        session.flush()
        foreign_artifact = service.documents.store_generated(
            session,
            workspace_id=str(other.id),
            job_posting_id=other_job.id,
            tailoring_run_id=None,
            logical_type="posting.body.text",
            mime_type="text/plain",
            content=b"Foreign posting",
            provenance={},
        )
        session.add(
            PostingObservation(
                job_posting_id=no_body_job_id,
                observed_at=datetime.now(UTC),
                posting_status="active",
                body_artifact_id=foreign_artifact.id,
            )
        )
    return workspace_id, no_body_job_id


def _workspace_without_a_master_cv(
    service: WorkflowService,
    database: Database,
    workspace_id: str,
    job_id: str,
    master_version_id: str,
    tmp_path: Path,
) -> tuple[str, str]:
    empty_root = tmp_path / "empty-workspace"
    empty_root.mkdir()
    empty = WorkspaceService(database, runtime_storage(database)).create(
        slug="empty-workflow", name="Empty workflow", root_path=str(empty_root)
    )
    with database.session() as session:
        empty_employer = Employer(workspace_id=str(empty.id), name="Empty", normalized_name="empty")
        session.add(empty_employer)
        session.flush()
        empty_job = JobPosting(
            workspace_id=str(empty.id),
            employer_id=empty_employer.id,
            title="No master",
            normalized_title="no master",
            first_seen_at=datetime.now(UTC),
        )
        session.add(empty_job)
        session.flush()
        empty_posting = service.documents.store_generated(
            session,
            workspace_id=str(empty.id),
            job_posting_id=empty_job.id,
            tailoring_run_id=None,
            logical_type="posting.body.text",
            mime_type="text/plain",
            content=b"Stored posting",
            provenance={},
        )
        session.add(
            PostingObservation(
                job_posting_id=empty_job.id,
                observed_at=datetime.now(UTC),
                posting_status="active",
                body_artifact_id=empty_posting.id,
            )
        )
        return str(empty.id), empty_job.id


def _stored_posting_artifact_id(database: Database, job_id: str) -> str:
    with database.session() as session:
        observation = session.scalar(
            select(PostingObservation).where(PostingObservation.job_posting_id == job_id)
        )
        assert observation is not None and observation.body_artifact_id is not None
        return observation.body_artifact_id


def _posting_body_stored_outside_the_local_store(
    service: WorkflowService,
    database: Database,
    workspace_id: str,
    job_id: str,
    master_version_id: str,
    tmp_path: Path,
) -> tuple[str, str]:
    artifact_id = _stored_posting_artifact_id(database, job_id)
    with database.session() as session:
        stored = session.get(Artifact, artifact_id)
        assert stored is not None
        stored.storage_uri = "https://example.test/posting"
    return workspace_id, job_id


def _posting_body_failing_its_integrity_check(
    service: WorkflowService,
    database: Database,
    workspace_id: str,
    job_id: str,
    master_version_id: str,
    tmp_path: Path,
) -> tuple[str, str]:
    artifact_id = _stored_posting_artifact_id(database, job_id)
    with database.session() as session:
        stored = session.get(Artifact, artifact_id)
        assert stored is not None
        stored.sha256 = "0" * 64
    return workspace_id, job_id


def _master_version_stored_outside_the_local_store(
    service: WorkflowService,
    database: Database,
    workspace_id: str,
    job_id: str,
    master_version_id: str,
    tmp_path: Path,
) -> tuple[str, str]:
    with database.session() as session:
        master_version = session.get(MasterCvVersion, master_version_id)
        assert master_version is not None
        master_version.source_uri = "https://example.test/master"
    return workspace_id, job_id


def _master_version_failing_its_integrity_check(
    service: WorkflowService,
    database: Database,
    workspace_id: str,
    job_id: str,
    master_version_id: str,
    tmp_path: Path,
) -> tuple[str, str]:
    with database.session() as session:
        master_version = session.get(MasterCvVersion, master_version_id)
        assert master_version is not None
        master_version.content_sha256 = "0" * 64
    return workspace_id, job_id


@pytest.mark.parametrize(
    ("prepare", "message"),
    [
        (_posting_without_a_stored_body, "no stored posting body"),
        (_posting_body_owned_by_another_workspace, "posting artifact is unavailable"),
        (_workspace_without_a_master_cv, "no master CV"),
        (_posting_body_stored_outside_the_local_store, "supported local file"),
        (_posting_body_failing_its_integrity_check, "integrity check"),
        (_master_version_stored_outside_the_local_store, "Master CV version is not"),
        (_master_version_failing_its_integrity_check, "Master CV version failed"),
    ],
)
def test_workflow_inputs_refuse_a_posting_or_master_cv_they_cannot_vouch_for(
    durable_database: Database, tmp_path: Path, prepare: Any, message: str
) -> None:
    artifact_root = tmp_path / "artifacts"
    workspace_id, job_id, _, master_version_id = studio_workspace(
        durable_database, tmp_path / "workspace", artifact_root
    )
    service = WorkflowService(
        durable_database, runtime_storage(durable_database, artifact_root), ScriptedPipelineRunner()
    )
    target_workspace, target_job = prepare(
        service, durable_database, workspace_id, job_id, master_version_id, tmp_path
    )

    with pytest.raises(ValidationError, match=message):
        service.inputs(target_workspace, target_job)


def test_workflow_inputs_refuse_a_job_the_workspace_does_not_hold(
    durable_database: Database, tmp_path: Path
) -> None:
    artifact_root = tmp_path / "artifacts"
    workspace_id, _, _, _ = studio_workspace(
        durable_database, tmp_path / "workspace", artifact_root
    )
    service = WorkflowService(
        durable_database, runtime_storage(durable_database, artifact_root), ScriptedPipelineRunner()
    )

    with pytest.raises(NotFoundError, match="workflow job"):
        service.inputs(workspace_id, "missing-job")


def test_persisting_a_run_tolerates_malformed_review_proposals_and_reuses_stored_research(
    durable_database: Database, tmp_path: Path
) -> None:
    artifact_root = tmp_path / "artifacts"
    workspace_id, job_id, _, master_version_id = studio_workspace(
        durable_database, tmp_path / "workspace", artifact_root
    )
    service = WorkflowService(
        durable_database, runtime_storage(durable_database, artifact_root), ScriptedPipelineRunner()
    )
    queued = TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="workflow.tailor",
        payload={"job_posting_id": job_id},
        idempotency_key="malformed-review-persistence",
    )

    persisted = service.persist_tailoring(
        workspace_id=workspace_id,
        job_id=job_id,
        master_version_id=master_version_id,
        task_id=str(queued.id),
        output=TailorOutput(
            artifacts=serialized_artifacts(
                {"resume.markdown": ("text/markdown", b"No sources\n")},
                manifest={"inputs": "not-a-mapping"},
                review={
                    "proposals": [
                        "not-a-proposal",
                        {},
                        {
                            "id": "no-sources",
                            "text": "No sources",
                            "digest": "b" * 32,
                            "evidence_ids": "not-a-list",
                        },
                    ]
                },
            ),
            outcome=TailoringOutcome.PASSED,
            ledger=Ledger(),
        ),
        started_at=datetime.now(UTC),
        research=(
            ResearchSource(
                id="company.careers",
                kind="careers",
                url="https://example.test/careers",
                text=STORED_CAREERS_TEXT,
                method="scripted",
            ),
        ),
    )

    assert persisted["outcome"] == "passed"
    reused = service.company_research(service.inputs(workspace_id, job_id))
    assert next(item for item in reused if item.id == "company.careers").method == (
        "stored_research_artifact"
    )


POSTING_PAGE_URL = "https://example.test/careers/roles/platform-lead"
CAREERS_URL = "https://example.test/careers"
THIN_CAREERS_PAGE = "<html><body><main><h1>Careers | Acme</h1></main></body></html>"


def _studio_research(
    database: Database, tmp_path: Path, careers_url: str, serve: Callable[[str], httpx.Response]
) -> tuple[WorkflowService, str, str, str]:
    """Seed the studio posting at its own page, its employer's careers address, and a reader."""
    artifact_root = tmp_path / "artifacts"
    workspace_id, job_id, _, master_version_id = studio_workspace(
        database, tmp_path / "workspace", artifact_root
    )
    with database.session() as session:
        job = session.get(JobPosting, job_id)
        assert job is not None
        employer = session.get(Employer, job.employer_id)
        assert employer is not None
        employer.careers_url = careers_url
        session.add(
            PostingUrl(
                job_posting_id=job_id,
                url_type="canonical",
                url=POSTING_PAGE_URL,
                normalized_url=normalize_url(POSTING_PAGE_URL),
                is_primary=True,
            )
        )
    service = WorkflowService(
        database,
        runtime_storage(database, artifact_root),
        ScriptedPipelineRunner(),
        research_fetcher=CompanyResearchFetcher(client=policy_client(serve)),
    )
    return service, workspace_id, job_id, master_version_id


def _store_research(
    service: WorkflowService,
    ids: tuple[str, str, str],
    research: tuple[ResearchSource, ...],
) -> None:
    """Persist one passed run that stored ``research`` for the posting."""
    workspace_id, job_id, master_version_id = ids
    queued = TaskQueueService(service.database).enqueue(
        workspace_id=workspace_id,
        task_type="workflow.tailor",
        payload={"job_posting_id": job_id},
        idempotency_key="stored-research",
    )
    service.persist_tailoring(
        workspace_id=workspace_id,
        job_id=job_id,
        master_version_id=master_version_id,
        task_id=str(queued.id),
        output=TailorOutput(
            artifacts=serialized_artifacts(
                {"resume.markdown": ("text/markdown", b"# Resume\n")}, manifest={}, review={}
            ),
            outcome=TailoringOutcome.PASSED,
            ledger=Ledger(),
        ),
        started_at=datetime.now(UTC),
        research=research,
    )


def _stored(kind: ResearchKind, text: str, url: str = CAREERS_URL) -> ResearchSource:
    return ResearchSource(id=f"company.{kind}", kind=kind, url=url, text=text, method="polite_html")


def _answer(status: int, page: str = "") -> Callable[[str], httpx.Response]:
    return lambda url: httpx.Response(status, request=httpx.Request("GET", url), text=page)


def test_research_never_reads_the_postings_own_page_as_its_careers_page(
    durable_database: Database, tmp_path: Path
) -> None:
    requested: list[str] = []

    def serve(url: str) -> httpx.Response:
        requested.append(url)
        return httpx.Response(200, request=httpx.Request("GET", url), text=COMPANY_CAREERS_PAGE)

    service, workspace_id, job_id, _ = _studio_research(
        durable_database, tmp_path, POSTING_PAGE_URL, serve
    )

    research = service.company_research(service.inputs(workspace_id, job_id))

    assert [item.kind for item in research] == ["posting"]
    assert POSTING_PAGE_URL not in requested


def test_research_reuses_nothing_stored_once_the_careers_page_is_read_and_refused(
    durable_database: Database, tmp_path: Path
) -> None:
    service, workspace_id, job_id, master_version_id = _studio_research(
        durable_database, tmp_path, CAREERS_URL, _answer(200, THIN_CAREERS_PAGE)
    )
    _store_research(
        service,
        (workspace_id, job_id, master_version_id),
        (
            _stored("careers", STORED_CAREERS_TEXT),
            _stored("about", STORED_CAREERS_TEXT, "https://example.test/about"),
        ),
    )

    research = service.company_research(service.inputs(workspace_id, job_id))

    assert [item.kind for item in research] == ["posting"]


@pytest.mark.parametrize(
    ("stored", "reused"),
    [
        pytest.param(
            (
                _stored("careers", STORED_CAREERS_TEXT),
                _stored("about", STORED_CAREERS_TEXT, "https://example.test/about"),
            ),
            ["careers", "about"],
            id="clean-pages",
        ),
        pytest.param(
            (_stored("careers", STORED_CAREERS_TEXT + " Open roles: {{ job.title }}"),),
            [],
            id="careers-with-an-unrendered-placeholder",
        ),
        pytest.param(
            (_stored("careers", STORED_CAREERS_TEXT, POSTING_PAGE_URL),),
            [],
            id="careers-at-the-postings-own-address",
        ),
        pytest.param(
            (
                _stored("careers", STORED_CAREERS_TEXT),
                _stored("about", "About Acme", "https://example.test/about"),
            ),
            ["careers"],
            id="thin-about-page",
        ),
    ],
)
def test_research_reuses_only_clean_stored_pages_when_the_careers_read_fails(
    durable_database: Database,
    tmp_path: Path,
    stored: tuple[ResearchSource, ...],
    reused: list[str],
) -> None:
    service, workspace_id, job_id, master_version_id = _studio_research(
        durable_database, tmp_path, CAREERS_URL, _answer(404)
    )
    _store_research(service, (workspace_id, job_id, master_version_id), stored)

    research = service.company_research(service.inputs(workspace_id, job_id))

    assert [item.kind for item in research] == ["posting", *reused]
    assert all(item.method == "stored_research_artifact" for item in research[1:])


@pytest.mark.parametrize(
    "outcome",
    [TailoringOutcome.PASSED, TailoringOutcome.CHECKS_FAILED],
)
def test_persist_tailoring_preserves_normalized_run_and_artifacts(
    durable_database: Database,
    tmp_path: Path,
    outcome: TailoringOutcome,
) -> None:
    artifact_root = tmp_path / "artifacts"
    workspace_id, job_id, _, master_version_id = studio_workspace(
        durable_database, tmp_path / "workspace", artifact_root
    )
    service = WorkflowService(
        durable_database,
        runtime_storage(durable_database, artifact_root),
        ScriptedPipelineRunner(),
    )
    task = TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="workflow.tailor",
        payload={"job_posting_id": job_id},
        idempotency_key=f"normalized-persistence-{outcome}",
    )
    review = {
        "proposals": [
            {
                "id": "line-1",
                "slot": "experience",
                "text": "Supported line",
                "digest": "a" * 32,
                "evidence_ids": ["b-r1a"],
            }
        ]
    }
    manifest = {"inputs": {"prompt_suite": "synthetic-suite"}, "run": {"outcome": outcome}}
    files: dict[ArtifactLogicalType, tuple[str, bytes]] = {
        "resume.markdown": ("text/markdown", b"# Candidate\n- Supported line\n"),
        "tailoring.report.markdown": ("text/markdown", b"# Tailoring report\n"),
        "tailoring.manifest.json": (
            "application/json",
            (json.dumps(manifest, sort_keys=True) + "\n").encode(),
        ),
        "cover_letter.markdown": ("text/markdown", b"Supported line\n"),
        "tailoring.review.json": (
            "application/json",
            (json.dumps(review, sort_keys=True) + "\n").encode(),
        ),
        "tailoring.review.markdown": ("text/markdown", b"# Proposed changes\n"),
    }
    ledger = Ledger(
        entries=[
            CallManifest(
                stage="draft",
                model="requested-model",
                served="served-model",
                usage=Usage(input_tokens=10, output_tokens=5),
                dollars=1.25,
            )
        ]
    )

    persisted = service.persist_tailoring(
        workspace_id=workspace_id,
        job_id=job_id,
        master_version_id=master_version_id,
        task_id=str(task.id),
        output=TailorOutput(
            artifacts=serialized_artifacts(files, manifest=manifest, review=review),
            outcome=outcome,
            ledger=ledger,
        ),
        started_at=datetime.now(UTC),
    )

    run_id = str(persisted["tailoring_run_id"])
    with durable_database.session() as session:
        run = session.get(TailoringRun, run_id)
        assert run is not None
        assert run.manifest == manifest
        assert run.outcome == outcome
        artifacts = session.scalars(
            select(Artifact).where(Artifact.tailoring_run_id == run_id)
        ).all()
        proposals = session.scalars(
            select(ReviewProposal).where(ReviewProposal.tailoring_run_id == run_id)
        ).all()
        calls = session.scalars(select(ModelCall).where(ModelCall.tailoring_run_id == run_id)).all()
        costs = session.scalars(
            select(CostRecord).where(CostRecord.model_call_id == calls[0].id)
        ).all()

    assert {artifact.logical_type for artifact in artifacts} == set(files)
    assert set(persisted["artifact_ids"]) == {artifact.id for artifact in artifacts}
    for artifact in artifacts:
        mime_type, content = files[artifact.logical_type]
        assert artifact.mime_type == mime_type
        assert service.documents.read(artifact.id, workspace_id).content == content
        citations = artifact.provenance.get("line_citations")
        if artifact.logical_type in {"resume.markdown", "cover_letter.markdown"}:
            assert citations == [
                {
                    "line_number": 2 if artifact.logical_type == "resume.markdown" else 1,
                    "text": "- Supported line"
                    if artifact.logical_type == "resume.markdown"
                    else "Supported line",
                    "source_ids": ["b-r1a"],
                    "proposal_id": proposals[0].id,
                    "digest": "a" * 32,
                }
            ]
        else:
            assert citations is None
    assert len(proposals) == 1
    assert proposals[0].source_ids == ["b-r1a"]
    assert len(calls) == 1
    assert calls[0].served_model == "served-model"
    assert len(costs) == 1
    assert float(costs[0].amount or 0) == 1.25
    assert persisted["cost_usd"] == 1.25
    assert persisted["served_models"] == {"draft": "served-model"}


def test_tailoring_preflight_reuses_an_exact_run_before_research_or_provider_work(
    durable_database: Database, tmp_path: Path
) -> None:
    artifact_root = tmp_path / "artifacts"
    workspace_id, job_id, _, master_version_id = studio_workspace(
        durable_database, tmp_path / "workspace", artifact_root
    )
    service = WorkflowService(
        durable_database, runtime_storage(durable_database, artifact_root), ScriptedPipelineRunner()
    )
    inputs = service.inputs(workspace_id, job_id)
    unavailable = service.tailoring_preflight(inputs, cover_letter=True, approval=None)
    assert unavailable["reuse_available"] is False
    assert unavailable["cost_usd"] is None

    persisted = seed_reusable_tailoring_run(
        durable_database, service, workspace_id, job_id, master_version_id
    )
    preflight = service.tailoring_preflight(inputs, cover_letter=True, approval=None)

    assert preflight == {
        "reuse_available": True,
        "tailoring_run_id": persisted["tailoring_run_id"],
        "artifact_ids": persisted["artifact_ids"],
        "cost_usd": 0,
    }
    assert service.tailoring_preflight(inputs, cover_letter=False, approval=None) == {
        "reuse_available": False,
        "tailoring_run_id": None,
        "artifact_ids": [],
        "cost_usd": None,
    }
    assert (
        service.tailoring_preflight(
            inputs,
            cover_letter=True,
            approval={"schema": 1, "digest": "different-review", "proposals": []},
        )["reuse_available"]
        is False
    )
    with TestClient(
        create_app(durable_database.url, artifact_root), base_url="http://localhost"
    ) as client:
        response = client.get(
            f"/api/v1/workspaces/{workspace_id}/jobs/{job_id}/tailoring-preflight",
            params={"cover_letter": "true"},
        )
    assert response.status_code == 200
    assert response.json() == preflight


def test_workflow_inputs_use_database_owned_workspace_settings(
    durable_database: Database, tmp_path: Path
) -> None:
    root = tmp_path / "workspace"
    workspace_id, job_id, _, _ = studio_workspace(durable_database, root, tmp_path / "artifacts")
    GuardrailService(durable_database).save(
        workspace_id,
        Guardrails(forbidden=["database rule"]),
        expected_version=0,
    )
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(locations=[Location(key="database", country="Atlantis")]),
        expected_version=0,
    )
    (root / "clearscout.toml").write_text('[rules]\nforbidden = ["file rule"]\n', encoding="utf-8")

    inputs = WorkflowService(
        durable_database, runtime_storage(durable_database, tmp_path / "artifacts")
    ).inputs(workspace_id, job_id)

    assert inputs.settings.rules.forbidden == ["database rule"]
    assert inputs.settings.job_search.locations[0].key == "database"


def test_approval_manifest_refuses_missing_and_malformed_reviews(
    durable_database: Database, tmp_path: Path
) -> None:
    artifact_root = tmp_path / "artifacts"
    workspace_id, job_id, _, master_version_id = studio_workspace(
        durable_database, tmp_path / "workspace", artifact_root
    )
    workflow = WorkflowService(
        durable_database, runtime_storage(durable_database, artifact_root), ScriptedPipelineRunner()
    )
    documents = DocumentService(durable_database, runtime_storage(durable_database, artifact_root))
    with pytest.raises(NotFoundError, match="does not exist"):
        workflow.approval_manifest(workspace_id, "missing")

    tasks = [
        TaskQueueService(durable_database).enqueue(
            workspace_id=workspace_id,
            task_type="workflow.tailor",
            payload={"job_posting_id": job_id},
            idempotency_key=f"invalid-review-{position}",
        )
        for position in range(4)
    ]
    run_ids: list[str] = []
    with durable_database.session() as session:
        for position, task in enumerate(tasks):
            run = TailoringRun(
                workspace_id=workspace_id,
                job_posting_id=job_id,
                master_cv_version_id=master_version_id,
                task_id=task.id,
                outcome="passed",
                manifest={},
            )
            session.add(run)
            session.flush()
            run_ids.append(run.id)
            if position == 0:
                continue
            contents = {
                1: b"\xff",
                2: b'{"wrong": []}\n',
                3: b'{"proposals": [{"id": "unknown"}]}\n',
            }
            documents.store_generated(
                session,
                workspace_id=workspace_id,
                job_posting_id=job_id,
                tailoring_run_id=run.id,
                logical_type="tailoring.review.json",
                mime_type="application/json",
                content=contents[position],
                provenance={"source": "test"},
            )
    with pytest.raises(ValidationError, match="no stored review"):
        workflow.approval_manifest(workspace_id, run_ids[0])
    with pytest.raises(ValidationError, match="not valid review JSON"):
        workflow.approval_manifest(workspace_id, run_ids[1])
    with pytest.raises(ValidationError, match="unsupported shape"):
        workflow.approval_manifest(workspace_id, run_ids[2])
    with pytest.raises(ValidationError, match="do not cover"):
        workflow.approval_manifest(workspace_id, run_ids[3])


@pytest.mark.parametrize("failure", ["cancel_after_result", "stage_failure", "manifest_failure"])
def test_tailoring_keeps_billed_calls_when_cancelled_or_a_later_stage_fails(
    durable_database, tmp_path, failure
):
    workspace_id, job_id, _, _ = studio_workspace(
        durable_database, tmp_path / "workspace", tmp_path / "artifacts"
    )
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="workflow.tailor",
        payload={"job_posting_id": job_id},
        idempotency_key=failure,
    )

    class InterruptedRunner(ScriptedPipelineRunner):
        def tailor(self, *args, **kwargs):
            result = super().tailor(*args, **kwargs)
            if failure == "stage_failure":
                if "ledger" in kwargs:
                    kwargs["ledger"].entries.extend(result.ledger.entries)
                raise RuntimeError("later stage failed")
            if failure == "manifest_failure":
                from dataclasses import replace

                return replace(
                    result,
                    artifacts=replace(result.artifacts, manifest={"inputs": {"invalid": object()}}),
                )
            queue.request_cancellation(str(task.id))
            return result

    worker = Worker(
        durable_database,
        worker_id="billing",
        handlers=default_handlers(
            durable_database,
            artifact_root=tmp_path / "artifacts",
            workflow_runner=InterruptedRunner(),
        ),
    )
    assert worker.run_once()
    assert queue.get(str(task.id)).status == (
        "cancelled" if failure == "cancel_after_result" else "failed"
    )
    with durable_database.session() as session:
        assert (
            len(session.scalars(select(ModelCall).where(ModelCall.task_id == task.id)).all()) == 1
        )
        assert len(session.scalars(select(CostRecord)).all()) == 1
        if failure == "cancel_after_result":
            assert (
                session.scalar(select(TailoringRun).where(TailoringRun.task_id == task.id))
                is not None
            )


def test_zero_planning_estimate_does_not_prevent_paid_tailoring(durable_database, tmp_path):
    from pydantic import BaseModel

    from clearscout.application.config import ProviderName, Stage
    from clearscout.infrastructure.providers.base import CallRecord
    from clearscout.infrastructure.providers.cache import ResponseCache
    from clearscout.infrastructure.providers.client import Client
    from clearscout.shared.costs import ModelPrice
    from tests.support.plans import plan_for

    class Answer(BaseModel):
        value: str

    class BoundedAnswer:
        name = ProviderName.OPENAI
        calls = 0

        def available_models(self) -> frozenset[str] | None:
            return None

        def check_credentials(self) -> None:
            return None

        def complete(self, plan, schema, choice):
            self.calls += 1
            return Answer(value="ok"), CallRecord(
                usage=Usage(input_tokens=1000), model=choice.model
            )

    provider = BoundedAnswer()

    class CapRunner(ScriptedPipelineRunner):
        def tailor(self, source, settings, **kwargs):
            model = settings.choice_for_stage(Stage.DRAFT).model
            ledger = Ledger(prices={model: ModelPrice(input=100, output=100)})
            with ResponseCache(enabled=False) as cache:
                client = Client(
                    provider=provider,
                    settings=settings,
                    ledger=ledger,
                    cache=cache,
                )
                client.complete(plan_for(stage=Stage.DRAFT, max_tokens=1000), Answer)
            return super().tailor(source, settings, **kwargs)

    workspace_id, job_id, _, _ = studio_workspace(
        durable_database, tmp_path / "workspace", tmp_path / "artifacts"
    )
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="workflow.tailor",
        payload={"job_posting_id": job_id},
        idempotency_key="zero-cap",
    )
    worker = Worker(
        durable_database,
        worker_id="zero-cap",
        handlers=default_handlers(
            durable_database, artifact_root=tmp_path / "artifacts", workflow_runner=CapRunner()
        ),
    )
    assert worker.run_once()
    assert queue.get(str(task.id)).status == "completed"
    assert provider.calls == 1


def test_a_queued_tailoring_run_asks_for_the_unattended_service_tier(
    durable_database: Database, tmp_path: Path
) -> None:
    tiers: list[object] = []

    class TierRunner(ScriptedPipelineRunner):
        def tailor(self, source, settings, **kwargs):
            tiers.append(kwargs.get("service_tier"))
            return super().tailor(source, settings, **kwargs)

    workspace_id, job_id, _, _ = studio_workspace(
        durable_database, tmp_path / "workspace", tmp_path / "artifacts"
    )
    TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="workflow.tailor",
        payload={"job_posting_id": job_id},
        idempotency_key="unattended-tier",
    )
    worker = Worker(
        durable_database,
        worker_id="unattended-tier",
        handlers=default_handlers(
            durable_database, artifact_root=tmp_path / "artifacts", workflow_runner=TierRunner()
        ),
    )

    assert worker.run_once()
    assert tiers == [ServiceTier.FLEX]


def _stated_requirements(database: Database, job_id: str, texts: dict[str, str]) -> None:
    """Record one evaluation of ``job_id`` stating each requirement text under its key."""
    with database.session() as session:
        evaluation = JobEvaluation(
            job_posting_id=job_id,
            decision="apply_now",
            rationale="Fits.",
            payload_sha256="c" * 64,
            evaluated_at=datetime.now(UTC),
        )
        session.add(evaluation)
        session.flush()
        for position, (key, text) in enumerate(texts.items()):
            session.add(
                PostingRequirement(
                    job_posting_id=job_id,
                    job_evaluation_id=evaluation.id,
                    requirement_id=f"r{position + 1:02d}",
                    requirement_key=key,
                    text=text,
                    importance="must_have",
                    position=position,
                    is_hard_gate=False,
                    created_by="test",
                    origin="test_fixture",
                )
            )


def test_a_tailoring_run_carries_only_the_disputes_ruled_on_its_own_posting(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, job_id, _, _ = studio_workspace(
        durable_database, tmp_path / "workspace", tmp_path / "artifacts"
    )
    with durable_database.session() as session:
        job = session.get(JobPosting, job_id)
        assert job is not None
        other = JobPosting(
            workspace_id=workspace_id,
            employer_id=job.employer_id,
            title="Data Lead",
            normalized_title="data lead",
            first_seen_at=datetime.now(UTC),
        )
        session.add(other)
        session.flush()
        other_id = other.id
    _stated_requirements(durable_database, job_id, {"rk-own": "Operate container clusters"})
    _stated_requirements(
        durable_database,
        other_id,
        {"rk-other": "Speak fluent Portuguese", "rk-other-fit": "Ten years of Scala"},
    )
    rulings = RequirementRulingService(durable_database)
    audit = {"created_by": "test", "origin": "test"}
    for key, ruling in (
        ("rk-own", Ruling.DISPUTED_FIT),
        ("rk-other", Ruling.DISPUTED_EXTRACTION),
        ("rk-other-fit", Ruling.DISPUTED_FIT),
    ):
        rulings.set(
            workspace_id=workspace_id, requirement_key=key, ruling=ruling, note=None, audit=audit
        )
    TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="workflow.tailor",
        payload={"job_posting_id": job_id},
        idempotency_key="own-disputes",
    )
    runner = ScriptedPipelineRunner()
    worker = Worker(
        durable_database,
        worker_id="own-disputes",
        handlers=default_handlers(
            durable_database, artifact_root=tmp_path / "artifacts", workflow_runner=runner
        ),
    )

    assert worker.run_once()
    assert runner.last_active_disputes == "- rk-own: Operate container clusters"


def test_a_queued_tailoring_run_names_the_posting_by_its_stored_title_and_employer(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, job_id, _, _ = studio_workspace(
        durable_database, tmp_path / "workspace", tmp_path / "artifacts"
    )
    TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="workflow.tailor",
        payload={"job_posting_id": job_id},
        idempotency_key="stored-name",
    )
    runner = ScriptedPipelineRunner()
    worker = Worker(
        durable_database,
        worker_id="stored-name",
        handlers=default_handlers(
            durable_database, artifact_root=tmp_path / "artifacts", workflow_runner=runner
        ),
    )

    assert worker.run_once()
    assert runner.last_stored_name == StoredPostingName(title="Platform Lead", employer="Acme")


def _publisher_evidence(
    database: Database, workspace_id: str, slug: str, evidence: dict[str, str], fetched_at: datetime
) -> str:
    """Stage one pending posting whose read of the studio body recorded this publisher evidence."""
    pending_id = seed_pending_discovery(
        database, workspace_id, employer=slug, created_at=fetched_at
    )
    url = f"https://boards.greenhouse.io/{slug}/jobs/1"
    with database.session() as session:
        session.add(
            pending_posting_content(
                workspace_id,
                pending_id,
                url,
                content_sha256=hashlib.sha256(STUDIO_BODY).hexdigest(),
                fetched_at=fetched_at,
                identity_evidence=evidence,
            )
        )
    return pending_id


def _tailor_studio(
    database: Database,
    tmp_path: Path,
    evidence: dict[str, str],
    *,
    vouched: bool = False,
    elsewhere: dict[str, str] | None = None,
) -> ScriptedPipelineRunner:
    """Queue a tailoring run on the studio posting, its body recording this publisher evidence.

    ``elsewhere`` is evidence a later read of the same body recorded under another posting's URL.
    """
    workspace_id, job_id, _, _ = studio_workspace(
        database, tmp_path / "workspace", tmp_path / "artifacts"
    )
    url = "https://boards.greenhouse.io/acme-freight/jobs/1"
    with database.session() as session:
        job = session.get(JobPosting, job_id)
        assert job is not None
        employer = session.get(Employer, job.employer_id)
        assert employer is not None
        employer.name = "acme-freight"
        session.add(
            PostingUrl(
                job_posting_id=job_id,
                url_type="canonical",
                url=url,
                normalized_url=normalize_url(url),
                is_primary=True,
            )
        )
    pending_id = _publisher_evidence(
        database, workspace_id, "acme-freight", evidence, datetime(2026, 9, 1, tzinfo=UTC)
    )
    if vouched:
        with database.session() as session:
            pending = session.get(PendingDiscovery, pending_id)
            assert pending is not None
            pending.staged_facts = StagedListingFacts(
                identity_trust=ListingIdentityTrust.PUBLISHER
            ).as_json()
    if elsewhere is not None:
        _publisher_evidence(
            database, workspace_id, "globex-freight", elsewhere, datetime(2026, 9, 2, tzinfo=UTC)
        )
    TaskQueueService(database).enqueue(
        workspace_id=workspace_id,
        task_type="workflow.tailor",
        payload={"job_posting_id": job_id},
        idempotency_key="publisher-name",
    )
    runner = ScriptedPipelineRunner()
    worker = Worker(
        database,
        worker_id="publisher-name",
        handlers=default_handlers(
            database, artifact_root=tmp_path / "artifacts", workflow_runner=runner
        ),
    )
    assert worker.run_once()
    return runner


@pytest.mark.parametrize(
    ("evidence", "employer"),
    [
        ({"publisher_employer": "Acme Freight", "publisher_title": "Staff Lead"}, "Acme Freight"),
        ({"heading": "Platform Lead"}, "acme-freight"),
    ],
)
def test_tailoring_names_the_employer_the_publisher_stated_for_this_body(
    durable_database: Database, tmp_path: Path, evidence: dict[str, str], employer: str
) -> None:
    """A slug-like stored employer gives way to the publisher's name, never to a model's."""
    runner = _tailor_studio(durable_database, tmp_path, evidence)

    assert runner.last_stored_name == StoredPostingName(title="Platform Lead", employer=employer)


def test_tailoring_reads_publisher_evidence_only_from_this_postings_urls(
    durable_database: Database, tmp_path: Path
) -> None:
    """Another posting that served the same body later names its own employer, not this one."""
    runner = _tailor_studio(
        durable_database,
        tmp_path,
        {"publisher_employer": "Acme Freight"},
        elsewhere={"publisher_employer": "Globex Freight"},
    )

    assert runner.last_stored_name == StoredPostingName(
        title="Platform Lead", employer="Acme Freight"
    )


def test_a_publisher_vouched_employer_stands_in_tailoring_as_it_does_in_evaluation(
    durable_database: Database, tmp_path: Path
) -> None:
    runner = _tailor_studio(
        durable_database, tmp_path, {"publisher_employer": "Acme Freight"}, vouched=True
    )

    assert runner.last_stored_name == StoredPostingName(
        title="Platform Lead", employer="acme-freight"
    )


def test_a_queued_tailoring_run_names_the_master_cv_by_version_and_uploaded_file(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, job_id, _, _ = studio_workspace(
        durable_database, tmp_path / "workspace", tmp_path / "artifacts"
    )
    content = (FIXTURES / "master_cv.md").read_bytes() + b"\n"
    imported = MasterCvService(durable_database, runtime_storage(durable_database)).import_source(
        workspace_id=workspace_id, filename="ana_ferreira_cv.md", content=content, replace=True
    )
    TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="workflow.tailor",
        payload={"job_posting_id": job_id},
        idempotency_key="stored-cv",
    )
    runner = ScriptedPipelineRunner()
    worker = Worker(
        durable_database,
        worker_id="stored-cv",
        handlers=default_handlers(
            durable_database, artifact_root=tmp_path / "artifacts", workflow_runner=runner
        ),
    )

    assert worker.run_once()
    assert runner.last_stored_cv == StoredMasterCv(
        version_id=imported.master_cv_version_id, file_name="ana_ferreira_cv.md"
    )
