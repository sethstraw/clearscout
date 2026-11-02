"""A vocabulary run proposes CV-grounded titles, archetypes, and clusters.

It touches no board, posting, or network: postings and leads are the guided
campaign's concern, not this one. The record the model is shown still crosses
the untrusted boundary on the way in, so a proposal citing evidence the CV does
not have is refused by the schema.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import event, select

from tailorcv.application import job_search
from tailorcv.application.discovery.board_scan import SourceScanService
from tailorcv.application.discovery.derived_vocabulary_rules import (
    ProposedArchetype,
    ProposedCluster,
    ProposedTitle,
    SearchTitle,
    searchable_archetypes,
    searchable_clusters,
    searchable_titles,
    searched_archetypes,
    searched_titles,
)
from tailorcv.application.discovery.posting_identity import posting_identity_keys, posting_url_key
from tailorcv.application.discovery.proposals import (
    SearchArchetypeProposalService,
    SearchClusterProposalService,
    SearchTitleProposalService,
    accepted_archetypes,
    accepted_clusters,
)
from tailorcv.application.discovery.ranking import rank
from tailorcv.application.discovery.relevance_review import RelevanceReviewService
from tailorcv.application.discovery.search_parameter_summary import search_summary
from tailorcv.application.discovery.search_planning_queries import SearchPlanningQueries
from tailorcv.application.discovery.search_vocabulary import (
    SearchVocabularyHandler,
    SearchVocabularyService,
    response_schema,
    system_prompt,
)
from tailorcv.application.discovery.source_reading import (
    SourceContinuationTarget,
    SourceListing,
    SourceRead,
)
from tailorcv.application.failures import NotFoundError, ValidationError
from tailorcv.application.master_cv import MasterCvService
from tailorcv.application.master_cv.cv import parse_cv
from tailorcv.application.tailoring.stages.context import citable_entries
from tailorcv.application.tasks import (
    SearchVocabularyPayload,
    TaskQueueService,
    validate_task_payload,
)
from tailorcv.application.workspaces import (
    SearchParametersService,
    WorkspaceService,
)
from tailorcv.application.workspaces.parameters import parameters_in
from tailorcv.bootstrap.worker import Worker, default_handlers
from tailorcv.domain import (
    PostingStatus,
    SearchProposalState,
    SourceReadOutcome,
    utc_now,
)
from tailorcv.domain.search_parameters import (
    LANES,
    MAX_ARCHETYPES,
    MAX_CLUSTERS,
    Acceptance,
    Arrangement,
    Cluster,
    ClusterKind,
    CompensationBand,
    EmployerArchetype,
    Employers,
    Employment,
    Focus,
    JobSearchParameters,
    Lane,
    Location,
    Seniority,
    TargetTitle,
    WorkEligibility,
    WorkingTimeZone,
)
from tailorcv.domain.search_parameters.roles import title_excluded
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    AuditEvent,
    Employer,
    JobPosting,
    ModelCall,
    PendingDiscovery,
    PostingObservation,
    PostingUrl,
    Source,
    WorkerPresence,
)
from tailorcv.infrastructure.providers.base import (
    json_schema,
)
from tailorcv.infrastructure.web_fetch.policy import fetch_refusal
from tailorcv.shared.costs import ServiceTier
from tests.support.artifacts import artifact_service, runtime_storage
from tests.support.doubles import ScriptedProvider, UnexpectedPostingFetcher
from tests.support.scenarios import evaluation_commit, pending_discovery
from tests.support.scenarios import titles as stated_titles


class ScriptedBoards:
    """A board reader whose answers are written down, and which counts requests."""

    def __init__(self, boards: dict[str, list[SourceListing] | SourceRead | Exception]) -> None:
        self.boards = boards
        self.requested: list[str] = []

    def listings(
        self,
        employer: str,
        url: str,
        *,
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        self.requested.append(url)
        answer = self.boards[url]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, SourceRead):
            return answer
        return SourceRead(
            listings=answer,
            complete=True,
            raw_count=len(answer),
            outcome=SourceReadOutcome.ATS_COMPLETE,
        )

    def continue_listings(
        self,
        employer: str,
        targets: tuple[SourceContinuationTarget, ...],
        *,
        root_url: str = "",
        handoff_scopes: tuple[tuple[str, str, str], ...] = (),
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        raise AssertionError("a search-vocabulary test must run continuation work explicitly")


def _vocabulary(
    *,
    titles: list[dict[str, Any]] | None = None,
    archetypes: list[dict[str, Any]] | None = None,
    capabilities: list[dict[str, Any]] | None = None,
    domains: list[dict[str, Any]] | None = None,
) -> ScriptedProvider:
    """A provider that answers with exactly these titles, archetypes, and clusters."""
    return ScriptedProvider(
        {
            "SearchVocabulary": lambda schema: {
                "titles": list(titles or []),
                "archetypes": list(archetypes or []),
                "capabilities": list(capabilities or []),
                "domains": list(domains or []),
            }
        }
    )


def _title(
    title: str, reason: str = "The record shows it.", *ids: str, lane: Lane | None = None
) -> dict[str, Any]:
    return {
        "title": title,
        "reason": reason,
        "evidence_ids": list(ids),
        "lane": None if lane is None else lane.value,
    }


def _archetype(name: str, because: str = "The record shows it.", *ids: str) -> dict[str, Any]:
    return {"name": name, "because": because, "evidence_ids": list(ids)}


def _cluster(name: str, terms: list[str], *ids: str) -> dict[str, Any]:
    return {"name": name, "terms": terms, "because": "", "evidence_ids": list(ids)}


def _workspace(
    database: Database,
    tmp_path: Path,
    master_cv: Path,
    content: bytes | None = None,
    settings_toml: str = "pages = 2\n",
) -> str:
    """A workspace with a stored master CV and a settings file of its own.

    The file is written before the workspace exists because it seeds the
    workspace once; editing it afterwards changes nothing. Spend limits are not
    part of that seed, and are saved through their own service.
    """
    root = tmp_path / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    # Keep the test independent of the user's home directory.
    (root / "tailorcv.toml").write_text(settings_toml, encoding="utf-8")
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database))
        .create(slug="guided", name="Guided", root_path=str(root))
        .id
    )
    SearchParametersService(database).save(
        workspace_id,
        JobSearchParameters(
            seniority=Seniority(target_titles=stated_titles("Staff Data Engineer"))
        ),
    )
    MasterCvService(database, runtime_storage(database, tmp_path / "artifacts")).import_source(
        workspace_id=workspace_id,
        filename="CV_Master.md",
        content=content if content is not None else master_cv.read_bytes(),
    )
    return workspace_id


def _entry_ids(master_cv: Path) -> list[str]:
    return [entry.id for entry in citable_entries(parse_cv(master_cv))]


def _service(
    database: Database, tmp_path: Path, provider: ScriptedProvider
) -> SearchVocabularyService:
    return SearchVocabularyService(
        database, storage=runtime_storage(database, tmp_path / "artifacts"), provider=provider
    )


def _queue(database: Database, workspace_id: str) -> list[PendingDiscovery]:
    with database.session() as session:
        return list(
            session.scalars(
                select(PendingDiscovery).where(PendingDiscovery.workspace_id == workspace_id)
            )
        )


@pytest.mark.parametrize("archived", [False, True])
def test_a_tracked_posting_whose_latest_observation_is_closed_can_be_rediscovered(
    durable_database: Database,
    tmp_path: Path,
    master_cv: Path,
    *,
    archived: bool,
) -> None:
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    job_search.commit_evaluation(
        durable_database,
        artifact_service(durable_database, tmp_path / "artifacts"),
        evaluation_commit(workspace_id),
    )
    with durable_database.session() as session:
        posting = session.scalar(select(JobPosting).where(JobPosting.workspace_id == workspace_id))
        assert posting is not None
        posting.archived_at = utc_now() if archived else None
        session.add(
            PostingObservation(
                job_posting_id=posting.id,
                observed_at=utc_now(),
                posting_status=PostingStatus.CLOSED,
            )
        )

    known = SearchPlanningQueries(
        durable_database, runtime_storage(durable_database, tmp_path / "runtime")
    ).known_identities(workspace_id)

    assert posting_url_key("https://example.test/jobs/42") not in known.posting_fingerprints


def test_a_tracked_posting_without_an_observation_remains_known_under_sql_null_semantics(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    url = "https://example.test/jobs/unobserved"
    with durable_database.session() as session:
        employer = Employer(
            workspace_id=workspace_id,
            name="Northwind Analytics",
            normalized_name="northwind analytics",
        )
        session.add(employer)
        session.flush()
        posting = JobPosting(
            workspace_id=workspace_id,
            employer_id=employer.id,
            title="Data Governance Lead",
            normalized_title="data governance lead",
            first_seen_at=utc_now(),
        )
        session.add(posting)
        session.flush()
        session.add(
            PostingUrl(
                job_posting_id=posting.id,
                url_type="canonical",
                url=url,
                normalized_url=url,
                is_primary=True,
            )
        )

    known = SearchPlanningQueries(
        durable_database, runtime_storage(durable_database, tmp_path / "runtime")
    ).known_identities(workspace_id)

    assert posting_url_key(url) in known.posting_fingerprints


def test_a_tracked_apply_url_is_known_under_the_key_the_detail_url_computes(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A campaign captures the detail URL for a posting whose stored URL kept '/apply'."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    detail = "https://jobs.lever.co/globex/9f2c1d3e"
    with durable_database.session() as session:
        employer = Employer(
            workspace_id=workspace_id,
            name="Globex Research",
            normalized_name="globex research",
        )
        session.add(employer)
        session.flush()
        posting = JobPosting(
            workspace_id=workspace_id,
            employer_id=employer.id,
            title="Clinical Data Manager",
            normalized_title="clinical data manager",
            first_seen_at=utc_now(),
        )
        session.add(posting)
        session.flush()
        session.add(
            PostingUrl(
                job_posting_id=posting.id,
                url_type="canonical",
                url=f"{detail}/apply",
                normalized_url=f"{detail}/apply",
                is_primary=True,
            )
        )

    known = SearchPlanningQueries(
        durable_database, runtime_storage(durable_database, tmp_path / "runtime")
    ).known_identities(workspace_id)

    assert posting_url_key(detail) in known.posting_fingerprints


_WORKDAY_LISTED = (
    "https://acme.wd1.myworkdayjobs.com/acme_careers/job/Canada-Remote/Product-Manager_R10001"
)


def _known_with_pending_url(
    database: Database, tmp_path: Path, master_cv: Path, url: str
) -> frozenset[str]:
    workspace_id = _workspace(database, tmp_path, master_cv)
    row = pending_discovery(workspace_id, employer="Acme Clinical", created_at=utc_now())
    row.discovery_url = row.canonical_url = url
    with database.session() as session:
        session.add(row)
    return (
        SearchPlanningQueries(database, runtime_storage(database, tmp_path / "runtime"))
        .known_identities(workspace_id)
        .posting_fingerprints
    )


@pytest.mark.parametrize(
    "variant",
    [
        "https://acme.wd1.myworkdayjobs.com/en-US/acme_careers/job/Canada-Remote/Product-Manager_R10001",
        "https://acme.wd1.myworkdayjobs.com/acme_careers/job/Product-Manager_R10001",
        "https://acme.wd1.myworkdayjobs.com/en-US/acme_careers/job/Product-Manager_R10001",
    ],
)
def test_a_pending_workday_requisition_is_known_under_its_locale_and_location_variants(
    durable_database: Database, tmp_path: Path, master_cv: Path, *, variant: str
) -> None:
    known = _known_with_pending_url(durable_database, tmp_path, master_cv, _WORKDAY_LISTED)

    assert not posting_identity_keys(variant).isdisjoint(known)


@pytest.mark.parametrize(
    "other",
    [
        "https://acme.wd1.myworkdayjobs.com/acme_careers/job/Canada-Remote/Product-Manager_R10002",
        "https://globex.wd5.myworkdayjobs.com/acme_careers/job/Canada-Remote/Product-Manager_R10001",
    ],
    ids=["another requisition", "another tenant"],
)
def test_a_different_workday_requisition_is_not_known_from_a_pending_one(
    durable_database: Database, tmp_path: Path, master_cv: Path, *, other: str
) -> None:
    known = _known_with_pending_url(durable_database, tmp_path, master_cv, _WORKDAY_LISTED)

    assert posting_identity_keys(other).isdisjoint(known)


def test_a_pending_careers_site_posting_is_known_under_another_locale(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    listed = "https://careers.example.com/en/careers/principal-data-engineer-500001/job/"
    known = _known_with_pending_url(durable_database, tmp_path, master_cv, listed)

    variant = listed.replace("/en/", "/jp/")
    assert not posting_identity_keys(variant).isdisjoint(known)


def test_a_tracked_workday_requisition_is_known_under_its_locale_variant(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    with durable_database.session() as session:
        employer = Employer(
            workspace_id=workspace_id, name="Acme Clinical", normalized_name="acme clinical"
        )
        session.add(employer)
        session.flush()
        posting = JobPosting(
            workspace_id=workspace_id,
            employer_id=employer.id,
            title="Product Manager",
            normalized_title="product manager",
            first_seen_at=utc_now(),
        )
        session.add(posting)
        session.flush()
        session.add(
            PostingUrl(
                job_posting_id=posting.id,
                url_type="canonical",
                url=_WORKDAY_LISTED,
                normalized_url=_WORKDAY_LISTED,
                is_primary=True,
            )
        )

    known = SearchPlanningQueries(
        durable_database, runtime_storage(durable_database, tmp_path / "runtime")
    ).known_identities(workspace_id)

    variant = "https://acme.wd1.myworkdayjobs.com/en-US/acme_careers/job/Product-Manager_R10001"
    assert not posting_identity_keys(variant).isdisjoint(known.posting_fingerprints)


def test_strict_matching_keeps_an_accepted_title_out_of_the_gate_too() -> None:
    """A board scan ignores accepted names under strict, and a guided run matches it."""
    accepted = [TargetTitle(title="Streaming Platform Engineer", lane=Lane.SPECIALIST)]
    stated = stated_titles("Head of Data")

    strict = JobSearchParameters(seniority=Seniority(target_titles=stated, title_matching="strict"))
    guided = JobSearchParameters(seniority=Seniority(target_titles=stated))

    assert searched_titles(strict, accepted, []) == [TargetTitle(title="Head of Data")]
    assert searched_titles(guided, accepted, []) == [TargetTitle(title="Head of Data"), *accepted]


def test_guided_title_vocabulary_interleaves_every_authorized_source() -> None:
    parameters = JobSearchParameters(
        seniority=Seniority(target_titles=stated_titles("Director", "Manager", "Lead"))
    )
    accepted = [TargetTitle(title="Principal", lane=Lane.SPECIALIST)]
    proposed = [
        SearchTitle(
            title="Practice Head",
            reason="The record supports this market name.",
            evidence_ids=("entry-1",),
            used=True,
            lane=Lane.LEADERSHIP,
        )
    ]

    assert [item.title for item in searched_titles(parameters, accepted, proposed)] == [
        "Director",
        "Principal",
        "Practice Head",
        "Manager",
        "Lead",
    ]


def test_search_vocabulary_progress_does_not_compete_with_its_business_transaction(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A later proposal heartbeat must not write beside the search transaction."""

    def fail_fast_on_a_competing_writer(dbapi_connection: Any, *_args: object) -> None:
        dbapi_connection.execute("PRAGMA busy_timeout=1")

    event.listen(durable_database.engine, "checkout", fail_fast_on_a_competing_writer)
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    provider = _vocabulary(
        titles=[
            {
                "title": "Data Platform Lead",
                "reason": "Supported scope",
                "evidence_ids": [_entry_ids(master_cv)[0]],
            }
        ]
    )
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="search.vocabulary",
        payload=validate_task_payload("search.vocabulary", {}),
        idempotency_key="guided-progress-transaction",
    )
    handlers = default_handlers(durable_database, artifact_root=tmp_path / "artifacts")
    handlers["search.vocabulary"] = SearchVocabularyHandler(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path / "artifacts"),
        provider=provider,
    )
    worker = Worker(durable_database, worker_id="guided-worker", handlers=handlers)

    assert worker.run_once()

    finished = queue.get(str(task.id))
    assert finished.error_category is None, finished.error_message
    assert finished.error_message is None
    assert finished.status == "completed"
    assert finished.progress["postings_recorded"] == 0
    assert finished.progress["search_parameter_set_id"]
    assert len(finished.progress["search_parameter_sha256"]) == 64
    events = queue.events_after(str(task.id))
    assert "proposing" in [item.stage for item in events]
    assert events[-1].event_type == "task.completed"
    with durable_database.session() as session:
        sources = list(session.scalars(select(Source).where(Source.workspace_id == workspace_id)))
        pending = list(
            session.scalars(
                select(PendingDiscovery).where(PendingDiscovery.workspace_id == workspace_id)
            )
        )
        completed = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.workspace_id == workspace_id,
                    AuditEvent.action == "search_vocabulary.completed",
                )
            )
        )
        presence = session.get(WorkerPresence, "guided-worker")
    assert sources == []
    assert pending == []
    assert len(provider.calls) == 1
    assert len(completed) == 1
    assert presence is not None


def test_a_queued_vocabulary_run_asks_for_the_unattended_service_tier(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    provider = _vocabulary()
    queue = TaskQueueService(durable_database)
    queue.enqueue(
        workspace_id=workspace_id,
        task_type="search.vocabulary",
        payload=validate_task_payload("search.vocabulary", {}),
        idempotency_key="guided-unattended-tier",
    )
    handlers = default_handlers(durable_database, artifact_root=tmp_path / "artifacts")
    handlers["search.vocabulary"] = SearchVocabularyHandler(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path / "artifacts"),
        provider=provider,
    )

    assert Worker(durable_database, worker_id="tier-worker", handlers=handlers).run_once()

    assert [choice.service_tier for choice in provider.choices] == [ServiceTier.FLEX]


def test_the_registry_builds_the_handler_without_reaching_for_a_key(
    durable_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Constructing the production handler must not resolve a credential.

    The keyring is touched when a search runs, not when a worker starts, or a
    machine with no key configured could not start a worker at all.
    """

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("building a handler must not resolve a credential")

    monkeypatch.setattr("tailorcv.application.discovery.search_vocabulary.resolve", refuse)
    handlers = default_handlers(durable_database, artifact_root=tmp_path / "artifacts")
    assert "search.vocabulary" in handlers


def test_the_payload_takes_no_fields() -> None:
    """A vocabulary run reads no request fields at all."""
    assert validate_task_payload("search.vocabulary", {}) == {}
    for payload in ({"results_per_call": 3}, {"mode": "web"}):
        with pytest.raises(ValidationError, match="Invalid search.vocabulary payload"):
            validate_task_payload("search.vocabulary", payload)


def test_an_unstated_preference_is_not_reported_as_one() -> None:
    """Empty means no opinion, so the model is told nothing rather than a default."""
    assert search_summary(JobSearchParameters()) == "This person has stated no search preferences."
    stated = search_summary(
        JobSearchParameters(
            seniority=Seniority(target_titles=stated_titles("data platform"), minimum="senior"),
            locations=[Location(key="ca", country="Canada", metro="Toronto")],
        )
    )
    assert "Titles worth looking for: data platform" in stated
    assert "Places this person may work:" in stated
    assert "Toronto, Canada" in stated
    assert "Seniority band: senior" in stated
    assert "Titles to skip" not in stated


def test_a_title_holding_a_comma_stays_one_entry_in_the_summary() -> None:
    stated = search_summary(
        JobSearchParameters(
            seniority=Seniority(
                target_titles=stated_titles("Director, Regulated Archives", "Records Custodian"),
                excluded_titles=["Records Clerk, Archives", "Intern"],
            )
        )
    ).splitlines()

    assert "Titles worth looking for: Director, Regulated Archives; Records Custodian" in stated
    assert "Titles to skip: Records Clerk, Archives; Intern" in stated


def test_excluded_conditions_reach_the_summary_and_skip_blank_conditions() -> None:
    """Stated hard-exclusion conditions surface for the review; blank ones do not."""
    stated = search_summary(
        JobSearchParameters(
            excluded_conditions=[
                ("requires active security clearance", "no clearance"),
                ("", "reason without a condition"),
            ]
        )
    )

    assert (
        "Hard exclusion conditions the model must honor: "
        "requires active security clearance (reason: no clearance)" in stated
    )
    assert "reason without a condition" not in stated


def test_the_search_states_the_kinds_of_employer_worth_looking_at() -> None:
    """The search names kinds of employer, not only industries and sizes.

    An industry is what an employer sells and a size is how many people it
    employs; neither says what the work there looks like, so neither widens a
    search beyond the names somebody already thought of.
    """
    stated = search_summary(
        JobSearchParameters(
            employers=Employers(
                industries=["health"],
                archetypes=[
                    EmployerArchetype(
                        name="terminology and drug-information vendors",
                        because="the record is built on coded clinical vocabularies",
                    ),
                    EmployerArchetype(name="biomedical knowledge-graph companies"),
                ],
            )
        )
    )

    assert "Industries of interest: health" in stated
    assert (
        "Kinds of organisation worth looking at: terminology and drug-information vendors "
        "(the record is built on coded clinical vocabularies), "
        "biomedical knowledge-graph companies" in stated
    )


def test_the_search_states_the_work_and_the_subject_separately() -> None:
    """A capability is the craft and a domain is what it is about.

    The terms go with the name, because they are what a posting is read
    against and a name alone leaves the model to guess the group.
    """
    stated = search_summary(
        JobSearchParameters(
            focus=Focus(
                capabilities=[Cluster(name="data platform engineering", terms=["pipeline", "etl"])],
                domains=[Cluster(name="clinical terminology")],
                boundaries=["people management"],
            )
        )
    )

    assert "Kinds of work this person does: data platform engineering (pipeline, etl)" in stated
    assert "Subjects this work is about: clinical terminology" in stated
    assert "Work this person does not want:\n- people management" in stated


def test_each_unwanted_work_boundary_is_stated_on_its_own_line() -> None:
    """A boundary that is itself a list stays one item, apart from its neighbours."""
    stated = search_summary(
        JobSearchParameters(
            focus=Focus(
                boundaries=[
                    "payer strategy, procurement, contracting, and rebates",
                    "people management",
                ]
            )
        )
    )

    assert (
        "Work this person does not want:\n"
        "- payer strategy, procurement, contracting, and rebates\n"
        "- people management"
    ) in stated


def test_the_summary_leads_with_the_opportunity_thesis_when_set() -> None:
    """The thesis is one person's preference and precedes every evidence line."""
    stated = search_summary(
        JobSearchParameters(opportunity_thesis="Head of platform at a mid-market health payer.")
    )
    first_line = stated.splitlines()[0]

    assert first_line == (
        "Opportunity thesis (person-stated preference, not evidence): "
        "Head of platform at a mid-market health payer."
    )


def test_an_unstated_opportunity_thesis_is_not_reported_as_one() -> None:
    """Empty means no opinion, as with every other parameter."""
    stated = search_summary(JobSearchParameters(opportunity_thesis="   "))

    assert "Opportunity thesis" not in stated


def test_an_opportunity_thesis_alone_counts_as_configured() -> None:
    """Even one stated preference means the workspace has said something."""
    assert JobSearchParameters(opportunity_thesis="a mandate line").configured


def test_every_retained_employment_and_employer_preference_reaches_search_vocabulary() -> None:
    stated = search_summary(
        JobSearchParameters(
            employment=Employment(
                types=["permanent"],
                excluded_types=["contract"],
                payroll_arrangements=["employee payroll"],
            ),
            employers=Employers(
                industries=["public services"],
                excluded_industries=["tobacco"],
                sizes=["enterprise"],
            ),
        )
    )

    assert "Engagement types: permanent" in stated
    assert "Engagement types refused: contract" in stated
    assert "Payroll arrangements accepted: employee payroll" in stated
    assert "Industries of interest: public services" in stated
    assert "Industries to leave out: tobacco" in stated
    assert "Employer sizes worth considering: enterprise" in stated


def test_an_unstated_focus_is_not_reported_as_one() -> None:
    """An empty field is no opinion, which is what every other parameter means."""
    stated = search_summary(JobSearchParameters())

    assert "Kinds of work this person does" not in stated
    assert "Subjects this work is about" not in stated
    assert "Work this person does not want" not in stated


def test_an_archetype_nobody_named_is_not_an_instruction() -> None:
    """An empty field is no opinion, which is what every other parameter means."""
    stated = search_summary(
        JobSearchParameters(
            employers=Employers.model_validate({"archetypes": ["  ", "peer employers"]})
        )
    )

    assert "Kinds of organisation worth looking at: peer employers" in stated


def test_search_vocabulary_summary_includes_per_location_facts() -> None:
    """Per-location constraints and compensation are passed as one search instruction."""
    stated = search_summary(
        JobSearchParameters(
            locations=[
                Location(
                    key="ca",
                    country="Canada",
                    metro="Toronto",
                    eligibility=WorkEligibility.AUTHORIZED,
                    basis="Canadian citizen",
                    arrangement=Arrangement(
                        remote=Acceptance.PREFERRED, on_site=Acceptance.REFUSED
                    ),
                )
            ],
            compensation=[
                CompensationBand(
                    location="ca",
                    currency="CAD",
                    basis="annual",
                    minimum=Decimal(115000),
                    target=Decimal(145000),
                    stretch=Decimal(175000),
                )
            ],
            arrangement=Arrangement(remote=Acceptance.ACCEPTABLE),
        )
    )
    assert "eligibility authorized" in stated
    assert "basis Canadian citizen" in stated
    assert "arrangement remote preferred" in stated
    assert "refused on site;" in stated
    assert "on site refused" not in stated
    assert "compensation minimum 115000" in stated
    assert "target 145000" in stated
    assert "stretch 175000" in stated
    assert "Working arrangements accepted" not in stated


def test_global_arrangement_limits_reach_the_recruiter_without_a_location_row() -> None:
    stated = search_summary(
        JobSearchParameters(
            arrangement=Arrangement(
                remote=Acceptance.PREFERRED,
                maximum_office_days=1,
                maximum_travel_percent=10,
                onsite_requires_paid_travel=True,
                relocation=False,
            )
        )
    )

    assert "Working arrangements accepted: remote" in stated
    assert "Maximum office days a week: 1" in stated
    assert "Maximum travel: 10%" in stated
    assert "Office attendance only with employer-funded travel" in stated
    assert "Relocation: no" in stated


def test_working_time_zone_reaches_vocabulary_and_relevance_review_search_context() -> None:
    stated = search_summary(JobSearchParameters(working_time_zone=WorkingTimeZone.PACIFIC))

    assert "Usual working time zone: Pacific" in stated


def test_the_hours_a_person_can_shift_reach_search_context_only_when_stated() -> None:
    shifted = search_summary(
        JobSearchParameters(working_time_zone=WorkingTimeZone.PACIFIC, working_day_shift_hours=2)
    )
    unshifted = search_summary(JobSearchParameters(working_time_zone=WorkingTimeZone.PACIFIC))

    assert "Hours this person can shift their working day: 2" in shifted
    assert "shift their working day" not in unshifted


def test_the_record_drives_the_search_but_not_what_the_person_will_accept() -> None:
    """The record says what the work is. The parameters say what is acceptable.

    The record drives role families, domains, and employer archetypes, not only
    titles. It does not drive eligibility, pay, or arrangement: a career record is not
    evidence of any of those, and `search_summary` states all three.
    """
    prompt = system_prompt(
        remaining_titles=8,
        remaining_archetypes=4,
        remaining_capabilities=4,
        remaining_domains=4,
    )

    assert "role families" in prompt
    assert "kinds of organisation that buy it" in prompt
    assert "search parameters, not the record" in prompt
    assert "not evidence of any of those" in prompt


def _proposed_once(durable_database: Database, tmp_path: Path, master_cv: Path) -> str:
    """Run one search that proposes a single evidence-backed title."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(seniority=Seniority(target_titles=stated_titles("data platform lead"))),
    )
    cited = _entry_ids(master_cv)[0]
    provider = _vocabulary(
        titles=[_title("Streaming Platform Engineer", "Owned streaming ingestion.", cited)]
    )
    _service(durable_database, tmp_path, provider).search(workspace_id, SearchVocabularyPayload())
    return workspace_id


def test_a_rejected_title_is_not_offered_again_by_a_later_search(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace_id = _proposed_once(durable_database, tmp_path, master_cv)
    inbox = SearchTitleProposalService(durable_database)
    [offered] = inbox.list(workspace_id)
    inbox.reject(workspace_id, offered.id)

    cited = _entry_ids(master_cv)[0]
    provider = _vocabulary(
        titles=[_title("Streaming Platform Engineer", "Owned streaming ingestion.", cited)]
    )
    _service(durable_database, tmp_path, provider).search(workspace_id, SearchVocabularyPayload())

    ruled = inbox.list(workspace_id)
    assert [(item.title, item.state) for item in ruled] == [
        ("Streaming Platform Engineer", SearchProposalState.REJECTED)
    ]
    assert (
        SourceScanService(
            durable_database,
            storage=runtime_storage(durable_database),
            fetcher=ScriptedBoards({}),
            posting_fetcher=UnexpectedPostingFetcher(),
        ).derived_titles(workspace_id)
        == []
    )


def test_rejected_titles_do_not_use_up_the_proposal_budget(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    cited = _entry_ids(master_cv)[0]
    filled = _vocabulary(
        titles=[_title(f"Platform Engineer {index}", "Ran it.", cited) for index in range(8)]
    )
    _service(durable_database, tmp_path, filled).search(workspace_id, SearchVocabularyPayload())
    inbox = SearchTitleProposalService(durable_database)
    for item in inbox.list(workspace_id):
        inbox.reject(workspace_id, item.id)

    later = _vocabulary(titles=[_title("Streaming Platform Engineer", "Owned ingestion.", cited)])
    _service(durable_database, tmp_path, later).search(workspace_id, SearchVocabularyPayload())

    assert [
        item.title
        for item in inbox.list(workspace_id)
        if item.state is SearchProposalState.PROPOSED
    ] == ["Streaming Platform Engineer"]


def test_a_ruling_on_a_search_title_can_be_reversed(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace_id = _proposed_once(durable_database, tmp_path, master_cv)
    inbox = SearchTitleProposalService(durable_database)
    [offered] = inbox.list(workspace_id)

    accepted = inbox.accept(workspace_id, offered.id)
    rejected = inbox.reject(workspace_id, offered.id)

    assert accepted.decided_at is not None
    assert rejected.state is SearchProposalState.REJECTED
    assert (
        SourceScanService(
            durable_database,
            storage=runtime_storage(durable_database),
            fetcher=ScriptedBoards({}),
            posting_fetcher=UnexpectedPostingFetcher(),
        ).derived_titles(workspace_id)
        == []
    )


def test_ruling_on_a_title_from_another_workspace_is_a_not_found(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace_id = _proposed_once(durable_database, tmp_path, master_cv)
    inbox = SearchTitleProposalService(durable_database)
    [offered] = inbox.list(workspace_id)

    with pytest.raises(NotFoundError, match="does not exist in this workspace"):
        inbox.accept("some-other-workspace", offered.id)


def test_reading_search_title_proposals_for_an_unknown_workspace_is_a_not_found(
    durable_database: Database,
) -> None:
    with pytest.raises(NotFoundError, match="Unknown workspace"):
        SearchTitleProposalService(durable_database).list("no-such-workspace")


def test_a_synthesized_title_that_restates_a_refusal_never_reaches_the_search() -> None:
    """The prompt asks for this and the program does not depend on the answer.

    Matched on whole words, so a refusal of "intern" refuses "Data Intern" and
    leaves "Internal Auditor" alone rather than reading one word inside another.
    """
    parameters = JobSearchParameters(
        seniority=Seniority(target_titles=stated_titles("data"), excluded_titles=["intern"])
    )
    decided = searchable_titles(
        [
            ProposedTitle(title="Data Intern", reason="Same work, junior."),
            ProposedTitle(title="INTERN, data platform", reason="Same again."),
            ProposedTitle(
                title="Internal Auditor", reason="Unrelated word.", evidence_ids=["b-cited"]
            ),
        ],
        parameters,
        {"b-cited"},
    )

    assert [(item.title, item.used, item.refused_because) for item in decided] == [
        ("Data Intern", False, "excluded_title"),
        ("INTERN, data platform", False, "excluded_title"),
        ("Internal Auditor", True, ""),
    ]


@pytest.mark.parametrize(
    ("excluded", "synthesized"),
    [("Clinical Trials Management", "Clinical Trials Manager"), ("Intern", "Data Interns")],
)
def test_a_synthesized_title_is_refused_exactly_as_screening_refuses_it(
    excluded: str, synthesized: str
) -> None:
    parameters = JobSearchParameters(
        seniority=Seniority(target_titles=stated_titles("data"), excluded_titles=[excluded])
    )

    [decided] = searchable_titles(
        [ProposedTitle(title=synthesized, reason="Close.", evidence_ids=["b-cited"])],
        parameters,
        {"b-cited"},
    )

    assert decided.refused_because == "excluded_title"
    assert title_excluded(synthesized, [excluded]) == excluded


def test_a_title_may_cite_only_entries_the_record_actually_has(master_cv: Path) -> None:
    """The vocabulary is closed by the schema and closed again on the way back.

    A constrained decoder cannot emit an identifier outside the set, so the
    second pass is for the provider that somehow does.
    """
    ids = _entry_ids(master_cv)
    schema = json_schema(response_schema(tuple(ids)))
    assert _cited_vocabulary(schema, "ConstrainedSearchTitle")["enum"] == ids

    decided = searchable_titles(
        [ProposedTitle(title="Analytics Engineer", reason="Why.", evidence_ids=[ids[0], "b-nope"])],
        JobSearchParameters(),
        set(ids),
    )
    assert decided[0].evidence_ids == (ids[0],)


def test_an_entry_the_schema_cannot_cite_is_not_evidence(master_cv: Path, tmp_path: Path) -> None:
    """A section the record holds but the schema withholds leaves a proposal uncited."""
    cv = tmp_path / "cv_with_an_unknown_section.md"
    cv.write_text(f"{master_cv.read_text()}\n\n## Hobbies\n\n- Long distance running.\n")
    inventory = parse_cv(cv)
    citable = {entry.id for entry in citable_entries(inventory)}
    withheld = sorted({entry.id for entry in inventory.entries} - citable)
    assert withheld, "the fixture must hold an entry the schema cannot cite"

    decided = searchable_titles(
        [ProposedTitle(title="Analytics Engineer", reason="Why.", evidence_ids=withheld)],
        JobSearchParameters(),
        citable,
    )

    assert decided[0].used is False
    assert decided[0].refused_because == "missing_evidence"


def test_an_archetype_citing_no_known_entry_is_refused_for_missing_evidence() -> None:
    decided = searchable_archetypes(
        [ProposedArchetype(name="registry operators", because="Why.", evidence_ids=["not-known"])],
        JobSearchParameters(),
        {"entry-1"},
    )
    assert decided[0].used is False
    assert decided[0].refused_because == "missing_evidence"


def test_a_cluster_citing_no_known_entry_is_refused_for_missing_evidence() -> None:
    decided = searchable_clusters(
        [
            ProposedCluster(
                name="graph engineering",
                terms=["cypher"],
                because="Why.",
                evidence_ids=["not-known"],
            )
        ],
        JobSearchParameters(),
        ClusterKind.CAPABILITY,
        {"entry-1"},
    )
    assert decided[0].used is False
    assert decided[0].refused_because == "missing_evidence"


def test_the_prompt_states_the_remaining_capacity_the_inbox_can_hold() -> None:
    """A nearly-full inbox is told the truth about what a run may still add."""
    text = " ".join(
        system_prompt(
            remaining_titles=3,
            remaining_archetypes=2,
            remaining_capabilities=1,
            remaining_domains=4,
        ).split()
    )
    assert "at most 3 titles" in text
    assert "at most 2 of them" in text
    assert "at most 1 to `capabilities`" in text
    assert "at most 4 to `domains`" in text


def test_the_prompt_keeps_independent_cluster_capacity() -> None:
    """A full capability inbox does not suppress domain proposals that still fit."""
    text = " ".join(
        system_prompt(
            remaining_titles=0,
            remaining_archetypes=0,
            remaining_capabilities=0,
            remaining_domains=2,
        ).split()
    )
    assert "propose no titles at all" in text
    assert "propose none" in text
    assert "at most 0 to `capabilities`" in text
    assert "at most 2 to `domains`" in text
    assert "does not prevent proposals for the other kind" in text


def test_the_prompt_names_every_track_a_title_may_be_given() -> None:
    """A vocabulary the schema accepts and the instructions never name is a guess."""
    text = system_prompt(
        remaining_titles=8,
        remaining_archetypes=4,
        remaining_capabilities=4,
        remaining_domains=4,
    )

    assert [lane for lane in LANES if lane not in text] == []


def test_a_track_crosses_as_a_nullable_enum_both_vendors_accept() -> None:
    """Optional is emulated with null, and Anthropic rejects `allOf` beside a `$ref`."""
    schema = json_schema(response_schema(("entry-1",)))

    lane = schema["$defs"]["ConstrainedSearchTitle"]["properties"]["lane"]
    assert lane["anyOf"] == [{"$ref": "#/$defs/Lane"}, {"type": "null"}]
    assert schema["$defs"]["Lane"]["enum"] == list(LANES)
    assert "allOf" not in json.dumps(schema)


def _cited_vocabulary(schema: dict[str, Any], model: str) -> dict[str, Any]:
    """Return one constrained model's evidence vocabulary, following a shared reference."""
    items: dict[str, Any] = schema["$defs"][model]["properties"]["evidence_ids"]["items"]
    reference = items.get("$ref")
    if reference is None:
        return items
    shared: dict[str, Any] = schema["$defs"][reference.rsplit("/", 1)[1]]
    return shared


def test_the_record_vocabulary_is_emitted_once_however_many_fields_cite_it() -> None:
    """Two citing fields share one entry vocabulary rather than each inlining it.

    A long record makes the entry vocabulary the term that approaches OpenAI's
    1000-value and 120,000-character ceilings, so the second citing field has to
    reference the first one's vocabulary rather than inline its own copy.
    """
    schema = json_schema(response_schema(("entry-1", "entry-2")))

    title_ids = schema["$defs"]["ConstrainedSearchTitle"]["properties"]["evidence_ids"]["items"]
    archetype_ids = schema["$defs"]["ConstrainedEmployerArchetype"]["properties"]["evidence_ids"][
        "items"
    ]
    assert "$ref" in title_ids
    assert title_ids == archetype_ids
    assert _cited_vocabulary(schema, "ConstrainedSearchTitle")["enum"] == ["entry-1", "entry-2"]
    assert json.dumps(schema).count('"entry-1"') == 1


def test_a_kind_of_employer_the_search_proposed_waits_for_a_ruling(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A proposed kind of employer waits for the person's ruling.

    A kind of organization is how a search reaches past the names somebody
    already wrote down, and the model classifies while the person decides, so a
    proposal is stated to nobody until it is accepted.
    """
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    cited = _entry_ids(master_cv)[0]
    provider = _vocabulary(
        archetypes=[
            _archetype(
                "terminology and drug-information vendors",
                "The record is built on coded clinical vocabularies.",
                cited,
            )
        ]
    )

    result = _service(durable_database, tmp_path, provider).search(
        workspace_id, SearchVocabularyPayload()
    )

    assert result["archetypes"] == [
        {
            "name": "terminology and drug-information vendors",
            "because": "The record is built on coded clinical vocabularies.",
            "evidence_ids": [cited],
            "used": True,
            "refused_because": "",
        }
    ]
    [offered] = SearchArchetypeProposalService(durable_database).list(workspace_id)
    assert offered.name == "terminology and drug-information vendors"
    assert offered.evidence_ids == [cited]
    assert offered.state is SearchProposalState.PROPOSED
    assert offered.decided_at is None
    assert accepted_archetypes(durable_database, workspace_id) == ()


def test_vocabulary_schema_refuses_an_employer_kind_without_cv_evidence(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A kind said to come from the record and citing none of it is not evidence."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    provider = _vocabulary(archetypes=[_archetype("late-stage health startups", "They hire this.")])

    with pytest.raises(PydanticValidationError, match="evidence_ids"):
        _service(durable_database, tmp_path, provider).search(
            workspace_id, SearchVocabularyPayload()
        )
    assert SearchArchetypeProposalService(durable_database).list(workspace_id) == []


def test_a_kind_may_cite_only_entries_the_record_actually_has(master_cv: Path) -> None:
    """The vocabulary is closed by the schema and closed again on the way back."""
    ids = _entry_ids(master_cv)
    schema = json_schema(response_schema(tuple(ids)))
    assert _cited_vocabulary(schema, "ConstrainedEmployerArchetype")["enum"] == ids

    decided = searchable_archetypes(
        [
            ProposedArchetype(
                name="registry operators", because="Why.", evidence_ids=[ids[0], "b-nope"]
            )
        ],
        JobSearchParameters(),
        set(ids),
    )
    assert decided[0].evidence_ids == (ids[0],)


def test_a_kind_the_search_already_states_is_not_offered_back(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A restatement costs an inbox slot and widens nothing."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            employers=Employers(archetypes=[EmployerArchetype(name="Peer Employers")])
        ),
    )
    cited = _entry_ids(master_cv)[0]
    provider = _vocabulary(
        archetypes=[
            _archetype("peer employers", "Same work, different logo.", cited),
            _archetype("registry operators", "The record runs one.", cited),
            _archetype("Registry Operators", "Said twice.", cited),
        ]
    )

    result = _service(durable_database, tmp_path, provider).search(
        workspace_id, SearchVocabularyPayload()
    )

    assert [item["refused_because"] for item in result["archetypes"]] == [
        "already_stated",
        "",
        "duplicate",
    ]
    assert [
        item.name for item in SearchArchetypeProposalService(durable_database).list(workspace_id)
    ] == ["registry operators"]


def test_a_rejected_kind_of_employer_is_not_offered_again_by_a_later_search(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A ruling holds, so the same kind does not come back as a fresh decision."""
    workspace_id, cited = _archetype_proposed_once(durable_database, tmp_path, master_cv)
    inbox = SearchArchetypeProposalService(durable_database)
    [offered] = inbox.list(workspace_id)
    inbox.reject(workspace_id, offered.id)

    again = _vocabulary(archetypes=[_archetype("hospital systems", "Proposed again.", cited)])
    _service(durable_database, tmp_path, again).search(workspace_id, SearchVocabularyPayload())

    assert [(item.name, item.state) for item in inbox.list(workspace_id)] == [
        ("hospital systems", SearchProposalState.REJECTED)
    ]
    assert accepted_archetypes(durable_database, workspace_id) == ()


def test_a_ruling_on_a_kind_of_employer_can_be_reversed(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace_id, _ = _archetype_proposed_once(durable_database, tmp_path, master_cv)
    inbox = SearchArchetypeProposalService(durable_database)
    [offered] = inbox.list(workspace_id)

    accepted = inbox.accept(workspace_id, offered.id)
    rejected = inbox.reject(workspace_id, offered.id)

    assert accepted.decided_at is not None
    assert rejected.state is SearchProposalState.REJECTED
    assert accepted_archetypes(durable_database, workspace_id) == ()


def test_the_inbox_holds_no_more_kinds_than_one_person_rules_on(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """One answer is capped by the schema; what accumulates across runs is not.

    A rejected kind does not spend a slot, so a queue filled by one run can
    still be worked down and refilled by the next.
    """
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    cited = _entry_ids(master_cv)[0]
    limit = MAX_ARCHETYPES
    filled = _vocabulary(
        archetypes=[
            _archetype(f"employer kind {index}", "The record shows it.", cited)
            for index in range(limit)
        ]
    )
    _service(durable_database, tmp_path, filled).search(workspace_id, SearchVocabularyPayload())
    over = _vocabulary(archetypes=[_archetype("one kind too many", "The record shows it.", cited)])
    _service(durable_database, tmp_path, over).search(workspace_id, SearchVocabularyPayload())

    inbox = SearchArchetypeProposalService(durable_database)
    stored = inbox.list(workspace_id)
    assert [item.name for item in stored] == [f"employer kind {index}" for index in range(limit)]
    for item in stored:
        inbox.reject(workspace_id, item.id)

    later = _vocabulary(archetypes=[_archetype("one more kind", "The record shows it.", cited)])
    _service(durable_database, tmp_path, later).search(workspace_id, SearchVocabularyPayload())

    assert [
        item.name for item in inbox.list(workspace_id) if item.state is SearchProposalState.PROPOSED
    ] == ["one more kind"]


def test_the_inbox_reads_in_the_order_the_search_proposed(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """One run writes one created_at, so without a stored place the order is a UUID's.

    The model puts what it is most confident about first, and a queue that
    reshuffles between reads throws that away and moves rows under the cursor.
    """
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    cited = _entry_ids(master_cv)[0]
    offered = ["registry operators", "hospital systems", "terminology vendors"]
    provider = _vocabulary(
        archetypes=[_archetype(name, "The record shows it.", cited) for name in offered]
    )

    _service(durable_database, tmp_path, provider).search(workspace_id, SearchVocabularyPayload())

    inbox = SearchArchetypeProposalService(durable_database)
    assert [item.name for item in inbox.list(workspace_id)] == offered
    assert [item.name for item in inbox.list(workspace_id)] == offered


def test_a_capability_the_search_proposed_waits_for_a_ruling(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A cluster carries the words a posting is read against, not just a name."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    cited = _entry_ids(master_cv)[0]
    provider = _vocabulary(
        capabilities=[_cluster("data platform engineering", ["pipeline", "etl"], cited)],
        domains=[_cluster("clinical terminology", ["snomed"], cited)],
    )

    result = _service(durable_database, tmp_path, provider).search(
        workspace_id, SearchVocabularyPayload()
    )

    assert [(item["kind"], item["name"], item["terms"]) for item in result["clusters"]] == [
        ("capability", "data platform engineering", ["pipeline", "etl"]),
        ("domain", "clinical terminology", ["snomed"]),
    ]
    offered = SearchClusterProposalService(durable_database).list(workspace_id)
    assert [(item.kind, item.name) for item in offered] == [
        (ClusterKind.CAPABILITY, "data platform engineering"),
        (ClusterKind.DOMAIN, "clinical terminology"),
    ]
    assert all(item.state is SearchProposalState.PROPOSED for item in offered)
    with durable_database.session() as session:
        assert accepted_clusters(session, workspace_id) == ()


def test_accepting_a_capability_within_the_cap_adds_it_to_the_list_and_ranks_it(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """Four stated leave room, so an accepted fifth ranks against a matching title."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    cited = _entry_ids(master_cv)[0]
    service = SearchParametersService(durable_database)
    service.save(
        workspace_id,
        JobSearchParameters(
            focus=Focus(
                capabilities=[Cluster(name=f"stated capability {index}") for index in range(4)]
            )
        ),
    )
    versions_before = len(service.history(workspace_id))
    proposing = _vocabulary(capabilities=[_cluster("graph engineering", ["cypher"], cited)])
    _service(durable_database, tmp_path, proposing).search(workspace_id, SearchVocabularyPayload())
    inbox = SearchClusterProposalService(durable_database)
    [offered] = inbox.list(workspace_id)
    inbox.accept(workspace_id, offered.id)

    with durable_database.session() as session:
        parameters = parameters_in(session, workspace_id)
    stated = [item.name for item in parameters.focus.stated_clusters(ClusterKind.CAPABILITY)]
    assert stated[4] == "graph engineering"
    ranked = rank("Senior Cypher Engineer", "", parameters, [])
    assert "the work is graph engineering" in ranked.because
    assert len(service.history(workspace_id)) == versions_before + 1


def test_accepting_a_capability_past_the_cap_adds_it_but_does_not_rank_it(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """Five stated fill the ranked slots, so an accepted sixth is listed but never ranked."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    cited = _entry_ids(master_cv)[0]
    service = SearchParametersService(durable_database)
    service.save(
        workspace_id,
        JobSearchParameters(
            focus=Focus(
                capabilities=[
                    Cluster(name=f"stated capability {index}") for index in range(MAX_CLUSTERS)
                ]
            )
        ),
    )
    proposing = _vocabulary(capabilities=[_cluster("graph engineering", ["cypher"], cited)])
    _service(durable_database, tmp_path, proposing).search(workspace_id, SearchVocabularyPayload())
    inbox = SearchClusterProposalService(durable_database)
    [offered] = inbox.list(workspace_id)
    inbox.accept(workspace_id, offered.id)

    with durable_database.session() as session:
        parameters = parameters_in(session, workspace_id)
    stated = [item.name for item in parameters.focus.stated_clusters(ClusterKind.CAPABILITY)]
    assert stated[MAX_CLUSTERS] == "graph engineering"
    ranked = rank("Senior Cypher Engineer", "", parameters, [])
    assert "the work is graph engineering" not in ranked.because


def test_accepting_the_same_capability_twice_neither_duplicates_it_nor_reversions(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A second accept of a name already stated writes no duplicate and no new version."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    cited = _entry_ids(master_cv)[0]
    service = SearchParametersService(durable_database)
    proposing = _vocabulary(capabilities=[_cluster("graph engineering", ["cypher"], cited)])
    _service(durable_database, tmp_path, proposing).search(workspace_id, SearchVocabularyPayload())
    inbox = SearchClusterProposalService(durable_database)
    [offered] = inbox.list(workspace_id)
    versions_before = len(service.history(workspace_id))
    inbox.accept(workspace_id, offered.id)
    after_first = len(service.history(workspace_id))
    inbox.accept(workspace_id, offered.id)
    after_second = len(service.history(workspace_id))

    with durable_database.session() as session:
        parameters = parameters_in(session, workspace_id)
    stated = parameters.focus.stated_clusters(ClusterKind.CAPABILITY)
    assert [item.name for item in stated] == ["graph engineering"]
    assert after_first == versions_before + 1
    assert after_second == after_first


def test_withdrawing_approval_removes_the_capability_from_the_stated_list(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """Withdrawing approval retracts the cluster an accept added, so it no longer ranks."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    cited = _entry_ids(master_cv)[0]
    service = SearchParametersService(durable_database)
    proposing = _vocabulary(capabilities=[_cluster("graph engineering", ["cypher"], cited)])
    _service(durable_database, tmp_path, proposing).search(workspace_id, SearchVocabularyPayload())
    inbox = SearchClusterProposalService(durable_database)
    [offered] = inbox.list(workspace_id)
    inbox.accept(workspace_id, offered.id)
    versions_after_accept = len(service.history(workspace_id))
    inbox.reject(workspace_id, offered.id)

    with durable_database.session() as session:
        parameters = parameters_in(session, workspace_id)
    stated = [item.name for item in parameters.focus.stated_clusters(ClusterKind.CAPABILITY)]
    assert "graph engineering" not in stated
    assert len(service.history(workspace_id)) == versions_after_accept + 1


def test_vocabulary_schema_refuses_a_capability_without_cv_evidence(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A cluster said to come from the record and citing none of it is not evidence."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    provider = _vocabulary(capabilities=[_cluster("wishful thinking", ["hope"])])

    with pytest.raises(PydanticValidationError, match="evidence_ids"):
        _service(durable_database, tmp_path, provider).search(
            workspace_id, SearchVocabularyPayload()
        )
    assert SearchClusterProposalService(durable_database).list(workspace_id) == []


def test_each_kind_of_cluster_carries_its_own_inbox_budget(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A record deep in one subject should not spend the capability queue on it."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    cited = _entry_ids(master_cv)[0]
    limit = MAX_CLUSTERS
    provider = _vocabulary(
        capabilities=[_cluster(f"capability {index}", ["term"], cited) for index in range(limit)],
        domains=[_cluster(f"domain {index}", ["term"], cited) for index in range(limit)],
    )

    _service(durable_database, tmp_path, provider).search(workspace_id, SearchVocabularyPayload())

    stored = SearchClusterProposalService(durable_database).list(workspace_id)
    assert sum(item.kind is ClusterKind.CAPABILITY for item in stored) == limit
    assert sum(item.kind is ClusterKind.DOMAIN for item in stored) == limit


def test_a_kind_the_person_wrote_outranks_the_same_kind_a_search_proposed() -> None:
    """Their reason for it is the one the search is told, not the model's."""
    parameters = JobSearchParameters(
        employers=Employers(
            archetypes=[EmployerArchetype(name="peer employers", because="I have worked at two.")]
        )
    )
    allowed = [
        EmployerArchetype(name="Peer Employers", because="The model's reading."),
        EmployerArchetype(name="registry operators", because="The record runs one."),
    ]

    assert searched_archetypes(parameters, allowed) == [
        EmployerArchetype(name="peer employers", because="I have worked at two."),
        EmployerArchetype(name="registry operators", because="The record runs one."),
    ]


def test_ruling_on_a_kind_of_employer_from_another_workspace_is_a_not_found(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace_id, _ = _archetype_proposed_once(durable_database, tmp_path, master_cv)
    inbox = SearchArchetypeProposalService(durable_database)
    [offered] = inbox.list(workspace_id)

    with pytest.raises(NotFoundError, match="does not exist in this workspace"):
        inbox.accept("some-other-workspace", offered.id)


def test_reading_archetype_proposals_for_an_unknown_workspace_is_a_not_found(
    durable_database: Database,
) -> None:
    with pytest.raises(NotFoundError, match="Unknown workspace"):
        SearchArchetypeProposalService(durable_database).list("no-such-workspace")


def test_a_workspace_with_no_master_cv_has_no_kinds_to_rule_on(
    durable_database: Database, tmp_path: Path
) -> None:
    """Every proposal cites a CV version, so with no version there is nothing to show."""
    root = tmp_path / "empty"
    root.mkdir()
    workspace_id = str(
        WorkspaceService(durable_database, runtime_storage(durable_database))
        .create(slug="e", name="E", root_path=str(root))
        .id
    )

    assert SearchArchetypeProposalService(durable_database).list(workspace_id) == []
    assert accepted_archetypes(durable_database, workspace_id) == ()


def _archetype_proposed_once(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> tuple[str, str]:
    """Run one search that proposes a single evidence-backed kind of employer."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    cited = _entry_ids(master_cv)[0]
    provider = _vocabulary(
        archetypes=[_archetype("hospital systems", "The record is clinical.", cited)]
    )
    _service(durable_database, tmp_path, provider).search(workspace_id, SearchVocabularyPayload())
    return workspace_id, cited


@pytest.mark.parametrize(
    ("url", "refusal"),
    [
        ("https://boards.greenhouse.io/acme", ""),
        ("http://example.test/jobs", ""),
        ("ftp://example.test/jobs", "not_an_http_url"),
        ("https://[::1]/jobs", "not_a_hostname"),
        ("https://service.internal/jobs", "local_hostname"),
        ("not a url at all", "not_an_http_url"),
    ],
)
def test_an_address_is_judged_before_it_is_opened(url: str, refusal: str) -> None:
    assert fetch_refusal(url) == refusal


def test_vocabulary_suggestions_use_one_text_call_and_no_retrieval(
    durable_database: Database, tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    evidence_id = _entry_ids(master_cv)[0]
    provider = _vocabulary(
        titles=[
            {
                "title": "Data Platform Lead",
                "reason": "Supported work",
                "evidence_ids": [evidence_id],
            }
        ]
    )
    service = _service(durable_database, tmp_path, provider)
    planning = service.queries.planning_inputs(workspace_id)

    def unexpected(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Vocabulary must not retrieve or review postings")

    monkeypatch.setattr(RelevanceReviewService, "review_candidates", unexpected)
    result = service.search(workspace_id, SearchVocabularyPayload())
    assert "No postings or new employer boards" in result["capability_note"]
    assert result["search_parameter_set_id"] == planning.search_parameter_set_id
    assert result["search_parameter_sha256"] == planning.search_parameter_sha256
    assert len(provider.calls) == 1
    assert provider.calls[0][1].search is None
    assert provider.web_search_calls == 0
    assert result["postings"] == []
    assert result["leads"] == []
    assert SearchTitleProposalService(durable_database).list(workspace_id)[0].evidence_ids == [
        evidence_id
    ]


@pytest.mark.parametrize("field", ["titles", "archetypes", "capabilities", "domains"])
@pytest.mark.parametrize("citations", [None, [], ["invented"]])
def test_vocabulary_schema_refuses_ungrounded_items(
    field: str, citations: list[str] | None
) -> None:
    from pydantic import ValidationError as SchemaError

    item = (
        {"title": "Role", "reason": "Why", "evidence_ids": citations}
        if field == "titles"
        else {"name": "Group", "because": "Why", "evidence_ids": citations}
    )
    if citations is None:
        item.pop("evidence_ids")
    with pytest.raises(SchemaError):
        response_schema(("exp.synthetic",)).model_validate({field: [item]})


def test_vocabulary_schema_refuses_all_model_urls() -> None:
    """A vocabulary run proposes no postings or leads, so a model-written URL is refused."""
    schema = response_schema(("exp.synthetic",))
    for extra_field in ("postings", "leads"):
        with pytest.raises(PydanticValidationError):
            schema.model_validate({"titles": [], extra_field: [{"url": "https://evil.example/x"}]})


def test_vocabulary_requires_a_citable_record() -> None:
    with pytest.raises(ValidationError, match="citable CV"):
        response_schema(())


def test_durable_vocabulary_task_uses_its_cap_without_web_or_candidate_review(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    evidence_id = _entry_ids(master_cv)[0]
    provider = _vocabulary(
        titles=[
            {
                "title": "Data Platform Lead",
                "reason": "Supported scope",
                "evidence_ids": [evidence_id],
            }
        ]
    )
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="search.vocabulary",
        payload=validate_task_payload("search.vocabulary", {}),
        idempotency_key="vocabulary-cap",
    )
    handlers = default_handlers(durable_database, artifact_root=tmp_path / "artifacts")
    handlers["search.vocabulary"] = SearchVocabularyHandler(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path / "artifacts"),
        provider=provider,
    )
    assert Worker(durable_database, worker_id="vocabulary-worker", handlers=handlers).run_once()
    finished = queue.get(str(task.id))
    assert finished.status == "completed", finished.error_message
    assert len(provider.calls) == 1
    assert provider.web_search_calls == 0
    assert provider.calls[0][1].search is None
    assert _queue(durable_database, workspace_id) == []
    with durable_database.session() as session:
        assert session.scalar(select(ModelCall.task_id)) == str(task.id)
