"""Anonymous scenario builders. Every name, employer, and address is invented."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tailorcv import __version__
from tailorcv.application import job_search, prompts
from tailorcv.application.contracts import (
    CompensationInput,
    EligibilityInput,
    EvaluationCommit,
    EvaluationScores,
    PostingBodyInput,
    ScoreComponentInput,
    ScoreEvidenceInput,
)
from tailorcv.application.discovery import posting_identity as posting
from tailorcv.application.discovery.content_resolution import (
    PostingContentResolutionService,
    ResolvedPostingContent,
)
from tailorcv.application.discovery.relevance_review import current_review_contract
from tailorcv.application.discovery.relevance_review_records import (
    RelevanceReviewContract,
    RelevanceReviewPostingInput,
    RelevanceReviewRecorder,
    ValidatedRelevanceReview,
    current_review_in,
    keep_note_digest,
)
from tailorcv.application.discovery.source_continuation import POSTING_DETAIL_COMPLETED
from tailorcv.application.discovery.source_reading import SourceListing
from tailorcv.application.documents import DocumentService
from tailorcv.application.documents.evidence_artifacts import EvidenceArtifactService
from tailorcv.application.job_search.models import JobDetailResponse, RelevanceReviewCitation
from tailorcv.application.master_cv import cv
from tailorcv.application.master_cv.service import MasterCvService
from tailorcv.application.master_cv.versioning import current_version_id
from tailorcv.application.tailoring import runs
from tailorcv.application.tailoring.workflows import TailorOutput, WorkflowService
from tailorcv.application.tasks import TaskQueueService
from tailorcv.application.workspaces import SearchParametersService, WorkspaceService
from tailorcv.application.workspaces.parameters import parameter_snapshot_in
from tailorcv.domain import (
    EvaluationDecision,
    EvidenceClassification,
    EvidenceSourceKind,
    EvidenceStrength,
    PostingContentAccessStatus,
    PostingContentExtractionMethod,
    PostingIdentityStatus,
    PostingStatus,
    ScoreComponent,
    SourceReadOutcome,
    TailoringOutcome,
    new_id,
    utc_now,
)
from tailorcv.domain.identity import normalize_text, normalize_url, url_sha256
from tailorcv.domain.search_parameters import (
    CompensationBand,
    CountryNames,
    JobSearchParameters,
    Lane,
    Location,
    Seniority,
    TargetTitle,
)
from tailorcv.domain.staged_facts import PublishedFact, StagedListingFacts
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    Application,
    AuditEvent,
    BackgroundTask,
    CandidateProfile,
    Employer,
    JobEvaluation,
    JobPosting,
    JobSearchParameterSet,
    MasterCvVersion,
    PendingDiscovery,
    PendingDiscoveryDispositionEvent,
    PendingPostingContent,
    PostingObservation,
    PostingUrl,
    RelevanceReview,
    Source,
    SourceScan,
    SourceScanContinuation,
    SourceScanFrontierItem,
    SourceScanMember,
    SourceScanOutcome,
    TailoringRun,
)
from tailorcv.infrastructure.web_fetch.company_research import ResearchSource, fingerprint
from tailorcv.infrastructure.web_fetch.posting_resolution import DeterministicPostingContentResolver
from tailorcv.shared.costs import Ledger
from tests.support.artifacts import artifact_service, runtime_storage
from tests.support.runners import serialized_artifacts

# The three origins that write a structured hold-out; the program makes them, never the person.
HOLDOUT_ORIGINS = (
    "source.scan.structured_holdout",
    "source.continue.structured_holdout",
    "discovery.rescreen_queue.structured_holdout",
)


def dismiss_pending_discovery(database: Database, workspace_id: str, pending_id: str) -> None:
    """Record a human dismissal, the disposition that hides a row from Pending."""
    with database.session() as session:
        session.add(
            PendingDiscoveryDispositionEvent(
                id=new_id(),
                workspace_id=workspace_id,
                pending_discovery_id=pending_id,
                action="dismiss",
                category="human_review",
                reason="An earlier, unrelated human dismissal.",
                selection_mode="explicit",
                snapshot_sha256="dismiss",
                created_by="local_user",
                origin="test",
            )
        )


def record_relevance_review(
    database: Database,
    workspace_id: str,
    pending_id: str,
    *,
    body_sha256: str = "e" * 64,
    keep_note: str | None = None,
) -> None:
    """Record one paid relevance review against a discovery, with its own CV and parameters."""
    now = datetime(2026, 8, 29, tzinfo=UTC)
    with database.session() as session:
        candidate = CandidateProfile(workspace_id=workspace_id, display_name="Candidate")
        session.add(candidate)
        session.flush()
        version = MasterCvVersion(
            candidate_profile_id=candidate.id,
            content_sha256="c" * 64,
            source_uri="file:///synthetic/master.md",
            effective_at=now,
        )
        task = BackgroundTask(
            workspace_id=workspace_id,
            task_type="discovery.relevance_review",
            status="completed",
            payload={},
            payload_sha256="0" * 64,
            idempotency_key=f"relevance-review-{pending_id}",
        )
        parameters = JobSearchParameterSet(
            workspace_id=workspace_id, version=1, content_sha256="b" * 64, effective_at=now
        )
        session.add_all([version, task, parameters])
        session.flush()
        session.add(
            RelevanceReview(
                workspace_id=workspace_id,
                pending_discovery_id=pending_id,
                task_id=task.id,
                master_cv_version_id=version.id,
                search_parameter_set_id=parameters.id,
                recommendation="unlikely",
                summary="The posting centres on field sales rather than operations.",
                core_work_alignment="absent",
                core_qualification_alignment="absent",
                hard_condition="passes",
                decisive_basis="core_gap",
                posting_citations=[],
                master_cv_evidence_ids=[],
                unresolved_questions=[],
                fetched_at=now,
                source_url_sha256="d" * 64,
                body_sha256=body_sha256,
                search_context_sha256="f" * 64,
                reviewed_at=now,
                keep_note_sha256=keep_note_digest(keep_note),
            )
        )


def titles(*names: str, lane: Lane | None = None) -> list[TargetTitle]:
    """Build target titles, all on one track or on none."""
    return [TargetTitle(title=name, lane=lane) for name in names]


def home_market_search(*target_titles: str) -> JobSearchParameters:
    """Search parameters saving one home market, searched under the given target titles."""
    # Ruritania is in the name table but not the saved places, so a posting
    # there resolves to a country and still reads as outside.
    return JobSearchParameters(
        locations=[Location(key="home", country="Freedonia")],
        countries=[
            CountryNames(country="Freedonia"),
            CountryNames(country="Ruritania"),
        ],
        seniority=Seniority(target_titles=titles(*target_titles)),
    )


def canadian_pay_with_a_us_place() -> JobSearchParameters:
    """A person whose only pay band is in CAD, for Toronto, who also accepts US work."""
    return JobSearchParameters(
        locations=[
            Location(key="toronto", country="Canada"),
            Location(key="us", country="United States"),
        ],
        countries=[CountryNames(country="Canada"), CountryNames(country="United States")],
        compensation=[
            CompensationBand(location="toronto", currency="CAD", minimum=Decimal(100000))
        ],
    )


def published_location_facts(location: str) -> StagedListingFacts:
    """Staged facts stating one location the publisher supplied and declared complete."""
    return StagedListingFacts(
        locations=PublishedFact(values=(location,), provenance="fictional.location", complete=True)
    )


# A small synthetic target list: two data-product tracks, one governance
# track, one logistics-data track.
DATA_PRODUCT_TARGET_TITLES = (
    "Senior Data Product Manager",
    "Associate Director, Data Governance",
    "Technical Product Manager, Import Compliance",
    "Principal Technical Product Manager, Data Platform",
    "Senior Logistics Data Product Manager",
    "Associate Director, Logistics Data Strategy",
)


def data_product_search(minimum: str = "") -> JobSearchParameters:
    """Search parameters over the data-product titles, banded by an optional seniority minimum."""
    return JobSearchParameters(
        locations=[Location(key="home", country="Freedonia")],
        countries=[CountryNames(country="Freedonia")],
        seniority=Seniority(
            target_titles=titles(*DATA_PRODUCT_TARGET_TITLES),
            minimum=minimum,
        ),
    )


def scan_workspace(database: Database, *, slug: str = "scan") -> str:
    """Create the workspace a board scan and its pending queue are read against."""
    return str(
        WorkspaceService(database, runtime_storage(database))
        .create(slug=slug, name=slug.capitalize())
        .id
    )


def state_target_titles(database: Database, workspace_id: str, *names: str) -> None:
    """State the titles a scan searches under, which an employer board needs before it stages."""
    SearchParametersService(database).save(
        workspace_id,
        JobSearchParameters(seniority=Seniority(target_titles=titles(*names))),
    )


def store_parameters_without(
    database: Database, workspace_id: str, parameters: JobSearchParameters, field: str
) -> str:
    """Append a parameter version stored before ``field`` existed, hashed as it was then.

    Returns the new version's id.
    """
    document = parameters.model_dump(mode="json")
    del document[field]
    content = json.dumps(document, sort_keys=True, separators=(",", ":"))
    with database.session() as session:
        previous = parameter_snapshot_in(session, workspace_id)
        row = JobSearchParameterSet(
            workspace_id=workspace_id,
            version=previous.version + 1,
            content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            effective_at=datetime.now(UTC),
            supersedes_id=previous.parameter_set_id,
            document=document,
            created_by="local_user",
            origin="tailorcv",
        )
        session.add(row)
        session.flush()
        return str(row.id)


def state_single_market(
    database: Database, workspace_id: str, *names: str, key: str, country: str
) -> None:
    """Save one searched market, named by its key and country, and the titles searched in it."""
    SearchParametersService(database).save(
        workspace_id,
        JobSearchParameters(
            locations=[Location(key=key, country=country)],
            countries=[CountryNames(country=country)],
            seniority=Seniority(target_titles=titles(*names)),
        ),
    )


def employer_board(database: Database, workspace_id: str, employer: str) -> str:
    """Register one enabled board source to a named employer and return the source id."""
    source_id = new_id()
    slug = normalize_text(employer).replace(" ", "-")
    with database.session() as session:
        registered = Employer(
            workspace_id=workspace_id,
            name=employer,
            normalized_name=normalize_text(employer),
            careers_url=f"https://{slug}.example.test/careers",
        )
        session.add(registered)
        session.flush()
        session.add(
            Source(
                id=source_id,
                workspace_id=workspace_id,
                employer_id=registered.id,
                name=f"{employer} careers",
                source_type="employer_board",
                url=f"https://{slug}.example.test/careers",
                priority=1,
                enabled=True,
            )
        )
    return source_id


def board_source(database: Database, workspace_id: str, name: str, url: str, **overrides) -> str:
    """Register one source at a stated address, where `employer_board` invents the address."""
    source_id = new_id()
    with database.session() as session:
        employer = Employer(
            workspace_id=workspace_id,
            name=name,
            normalized_name=normalize_text(name),
            careers_url=url,
        )
        session.add(employer)
        session.flush()
        session.add(
            Source(
                id=source_id,
                workspace_id=workspace_id,
                employer_id=employer.id,
                name=name,
                source_type=overrides.get("source_type", "employer_board"),
                url=url,
                priority=overrides.get("priority", 1),
                enabled=overrides.get("enabled", True),
                origin=overrides.get("origin", "tailorcv"),
                resolved_platform=overrides.get("resolved_platform"),
                resolved_url=overrides.get("resolved_url"),
            )
        )
    return source_id


def tracked_board_posting(
    database: Database,
    workspace_id: str,
    source_id: str,
    *,
    title: str,
    url: str,
    location: str = "",
    closed: bool = False,
) -> str:
    """Track one posting a board source supplied, at its board address and last-read place."""
    with database.session() as session:
        source = session.get(Source, source_id)
        assert source is not None and source.employer_id is not None
        job = JobPosting(
            workspace_id=workspace_id,
            employer_id=source.employer_id,
            title=title,
            normalized_title=normalize_text(title),
            first_seen_at=utc_now(),
        )
        session.add(job)
        session.flush()
        session.add(
            PostingUrl(
                job_posting_id=job.id,
                source_id=source_id,
                url_type="discovery",
                url=url,
                normalized_url=normalize_url(url),
                is_primary=True,
            )
        )
        session.add(
            PostingObservation(
                job_posting_id=job.id,
                observed_at=utc_now(),
                posting_status=PostingStatus.CLOSED if closed else PostingStatus.ACTIVE,
                location=location,
            )
        )
        return str(job.id)


def seed_unread_row(
    database: Database,
    workspace_id: str,
    *,
    employer: str,
    title: str,
    url: str,
    location: str = "",
    source_id: str | None = None,
) -> str:
    """Insert one pending row at a stated address that no read has yet made evaluable."""
    row = pending_discovery(
        workspace_id, employer=employer, created_at=utc_now(), title=title, source_id=source_id
    )
    row.location = location
    row.requisition_id = ""
    row.discovery_url = url
    row.canonical_url = ""
    with database.session() as session:
        session.add(row)
    return str(row.id)


def source_listing(
    title: str, url: str, employer: str = "Example", location: str = "Remote"
) -> SourceListing:
    """Build one board listing, the unit a scan stages into the pending queue."""
    return SourceListing(employer=employer, title=title, url=url, location=location)


def seed_pending_discovery(
    database: Database,
    workspace_id: str,
    *,
    employer: str,
    created_at: datetime,
    content_status: PostingContentAccessStatus = PostingContentAccessStatus.NOT_YET_READ,
    reason: str = "Board scan matched the title filter.",
    source_id: str | None = None,
    withdrawn_at: datetime | None = None,
    title: str = "Data Platform Lead",
) -> str:
    """Insert one pending discovery with an explicit age, readiness, and reason."""
    row = pending_discovery(
        workspace_id,
        employer=employer,
        created_at=created_at,
        content_status=content_status,
        reason=reason,
        source_id=source_id,
        withdrawn_at=withdrawn_at,
        title=title,
    )
    record_id = str(row.id)
    with database.session() as session:
        session.add(row)
    return record_id


def pending_discovery(
    workspace_id: str,
    *,
    employer: str,
    created_at: datetime,
    content_status: PostingContentAccessStatus = PostingContentAccessStatus.NOT_YET_READ,
    reason: str = "Board scan matched the title filter.",
    source_id: str | None = None,
    withdrawn_at: datetime | None = None,
    title: str = "Data Platform Lead",
) -> PendingDiscovery:
    """Build one unsaved pending discovery with an explicit age, readiness, and reason."""
    return PendingDiscovery(
        id=new_id(),
        workspace_id=workspace_id,
        fingerprint=new_id(),
        employer=employer,
        title=title,
        location="Remote",
        requisition_id="REQ-1",
        discovery_url=f"https://boards.greenhouse.io/{employer.lower()}/jobs/1",
        canonical_url=f"https://boards.greenhouse.io/{employer.lower()}/jobs/1",
        content_status=content_status,
        reason=reason,
        first_seen_on=created_at.date(),
        created_at=created_at,
        source_id=source_id,
        withdrawn_at=withdrawn_at,
    )


def seed_pending_discoveries(
    database: Database,
    workspace_id: str,
    employers: Sequence[str],
    *,
    created_at: datetime,
) -> list[str]:
    """Insert many pending discoveries in one transaction, returning their ids in order.

    A queue bound is reached with hundreds of rows, and one session per row turns
    that into hundreds of commits competing for the same SQLite writer.
    """
    rows = [
        pending_discovery(workspace_id, employer=employer, created_at=created_at)
        for employer in employers
    ]
    record_ids = [str(row.id) for row in rows]
    with database.session() as session:
        session.add_all(rows)
    return record_ids


def pending_posting_content(
    workspace_id: str,
    pending_discovery_id: str,
    requested_url: str,
    **overrides: object,
) -> PendingPostingContent:
    """Build one unsaved posting-content row, read successfully just now unless overridden."""
    values: dict[str, object] = {
        "workspace_id": workspace_id,
        "pending_discovery_id": pending_discovery_id,
        "requested_url": requested_url,
        # Content resolution digests the URL text as given, not its normalized form,
        # and the stored-name lookup matches on that exact digest.
        "requested_url_sha256": hashlib.sha256(requested_url.encode("utf-8")).hexdigest(),
        "content_status": PostingContentAccessStatus.READABLE,
        "fetched_at": utc_now(),
    }
    values.update(overrides)
    return PendingPostingContent(**values)


def seed_scan_continuation(
    session: Session,
    workspace_id: str,
    source_scan_id: str,
    rows: Sequence[dict[str, object]],
    *,
    origin: str = "tailorcv",
    correlation_id: str | None = None,
) -> str:
    """Add a continuation and its hand-placed frontier rows to an open session.

    Each row states at least ``url`` and overrides any frontier-item field.
    ``correlation_id`` names the scan task whose read opened the continuation.
    Returns the continuation id.
    """
    continuation_id = new_id()
    session.add(
        SourceScanContinuation(
            id=continuation_id,
            workspace_id=workspace_id,
            source_scan_id=source_scan_id,
            handoff_scopes=[],
            correlation_id=correlation_id,
        )
    )
    session.flush()
    seed_frontier_rows(session, continuation_id, rows, origin=origin)
    return continuation_id


def seed_frontier_rows(
    session: Session,
    continuation_id: str,
    rows: Sequence[dict[str, object]],
    *,
    origin: str = "tailorcv",
) -> None:
    """Add hand-placed frontier rows to an existing continuation; each row states ``url``."""
    for row in rows:
        values: dict[str, object] = {
            "id": new_id(),
            "continuation_id": continuation_id,
            "label": "Role",
            "label_source": "visible",
            "source_url": "https://publisher.example/careers",
            "structural_context": "main",
            "relations": ["publisher-content", "substantive-label"],
            "automatic_eligible": True,
            "deferred_reason": "",
            "status": "pending",
            "attempt_count": 0,
            "last_error": "",
            "origin": origin,
        }
        values.update(row)
        values.setdefault("normalized_url", values["url"])
        if values["status"] == "completed":
            values.setdefault("completed_at", utc_now())
        session.add(SourceScanFrontierItem(**values))


def completed_earlier_scan(
    database: Database, workspace_id: str, source_id: str, urls: tuple[str, ...], *, day: int = -1
) -> str:
    """Seed a finished prior scan whose continuation already completed each posting URL."""
    scan_id = new_id()
    now = utc_now() + timedelta(days=day)
    with database.session() as session:
        session.add(
            SourceScan(
                id=scan_id,
                source_id=source_id,
                scanned_on=now.date(),
                query_label="title-filter",
                status="completed_empty",
                read_outcome=SourceReadOutcome.PUBLISHER_INDEX,
                raw_count=len(urls),
                read_count=0,
                skip_reasons={},
                result_count=0,
                new_count=0,
                notes="",
                scan_started_at=now,
                scan_completed_at=now,
                duration_ms=0,
            )
        )
        seed_scan_continuation(
            session,
            workspace_id,
            scan_id,
            [
                {
                    "url": url,
                    "source_url": url,
                    "status": "completed",
                    "deferred_reason": POSTING_DETAIL_COMPLETED,
                    "attempt_count": 1,
                }
                for url in urls
            ],
        )
    return scan_id


def same_day_rescans_with_follow_ups(database: Database) -> tuple[str, str, str]:
    """Seed two scan tasks, hours apart, that rescan one source on one day.

    Each task's continuation generation records its own outcomes: the first
    task's titles start with "Alpha", the second's with "Bravo". Both share
    the day's scan row, which the second task last wrote. Returns the
    workspace id and the first and second scan task ids.
    """
    workspace_id = scan_workspace(database, slug="same-day-rescans")
    source_id = board_source(database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(database, workspace_id, "Data Platform Lead")
    queue = TaskQueueService(database)
    started = utc_now() - timedelta(hours=5)

    def task(task_type: str, key: str, payload: dict[str, Any], parent: str | None) -> str:
        queued = queue.enqueue(
            workspace_id=workspace_id,
            task_type=task_type,
            payload=payload,
            idempotency_key=key,
            actor="worker",
            origin="source.scan",
            correlation_id=parent,
        )
        with database.session() as session:
            stored = session.get(BackgroundTask, str(queued.id))
            assert stored is not None
            stored.status = "completed"
            stored.completed_at = utc_now()
        return str(queued.id)

    first = task("source.scan", "first-scan", {}, None)
    scan_id = new_id()
    continuation_id = new_id()
    with database.session() as session:
        member = session.scalars(
            select(SourceScanMember).where(SourceScanMember.root_task_id == first)
        ).one()
        session.add(
            SourceScan(
                id=scan_id,
                source_id=source_id,
                scanned_on=started.date(),
                query_label="title-filter",
                status="incomplete",
                read_outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                raw_count=1,
                read_count=1,
                skip_reasons={},
                result_count=1,
                new_count=1,
                notes="",
                scan_started_at=started,
                scan_completed_at=started,
                duration_ms=0,
                correlation_id=first,
            )
        )
        session.flush()
        session.add(
            SourceScanContinuation(
                id=continuation_id,
                workspace_id=workspace_id,
                source_scan_id=scan_id,
                generation=1,
                handoff_scopes=[],
                origin="source.scan",
                correlation_id=first,
                batch_member_id=member.id,
            )
        )
        member.source_scan_id = scan_id
        member.continuation_id = continuation_id
        member.generation = 1
        member.state = "continuing"
    # A continuation task's correlation id names the task that enqueued it.
    first_follow_up = task(
        "source.continue", "first-1", {"continuation_id": continuation_id, "generation": 1}, first
    )
    first_chained = task(
        "source.continue",
        "first-2",
        {"continuation_id": continuation_id, "generation": 1},
        first_follow_up,
    )
    with database.session() as session:
        member = session.scalars(
            select(SourceScanMember).where(SourceScanMember.root_task_id == first)
        ).one()
        member.state = "completed"
        member.released_at = utc_now()
    second = task("source.scan", "second-scan", {}, None)
    with database.session() as session:
        member = session.scalars(
            select(SourceScanMember).where(SourceScanMember.root_task_id == second)
        ).one()
        continuation = session.get(SourceScanContinuation, continuation_id)
        assert continuation is not None
        continuation.generation = 2
        continuation.batch_member_id = member.id
        continuation.correlation_id = second
        member.source_scan_id = scan_id
        member.continuation_id = continuation_id
        member.generation = 2
        member.state = "continuing"
    second_follow_up = task(
        "source.continue", "second-1", {"continuation_id": continuation_id, "generation": 2}, second
    )
    created = {
        first: started,
        first_follow_up: started + timedelta(minutes=5),
        first_chained: started + timedelta(minutes=10),
        second: started + timedelta(hours=4),
        second_follow_up: started + timedelta(hours=4, minutes=5),
    }
    outcomes = (
        (first, "source.scan", "Alpha Scan Lead", "staged", "staged"),
        (
            first_follow_up,
            "source.continue",
            "Alpha Follow-up Lead",
            "rejected",
            "insufficient_title_evidence",
        ),
        (
            first_chained,
            "source.continue",
            "Alpha Chained Lead",
            "rejected",
            "hard_constraint_excluded",
        ),
        (second, "source.scan", "Bravo Scan Lead", "staged", "staged"),
        (
            second_follow_up,
            "source.continue",
            "Bravo Follow-up Lead",
            "rejected",
            "insufficient_title_evidence",
        ),
    )
    with database.session() as session:
        for task_id, at in created.items():
            stored = session.get(BackgroundTask, task_id)
            assert stored is not None
            stored.created_at = at
            stored.status = "completed"
            for member in session.scalars(
                select(SourceScanMember).where(SourceScanMember.root_task_id == task_id)
            ):
                member.state = "completed"
                member.released_at = at + timedelta(minutes=1)
        scan = session.get(SourceScan, scan_id)
        assert scan is not None
        scan.scan_started_at = created[second]
        scan.scan_completed_at = created[second]
        scan.correlation_id = second
        session.flush()
        for task_id, origin, title, stage, reason in outcomes:
            url = f"https://jobs.example/globex/{title.lower().replace(' ', '-')}"
            session.add(
                SourceScanOutcome(
                    id=new_id(),
                    workspace_id=workspace_id,
                    task_id=task_id,
                    source_scan_id=scan_id,
                    source_name="Globex",
                    candidate_kind="board_listing",
                    url=url,
                    normalized_url=url,
                    employer="Globex",
                    title=title,
                    location="Remote",
                    terminal_stage=stage,
                    reason=reason,
                    created_by="worker",
                    origin=origin,
                    correlation_id=task_id,
                    created_at=created[task_id] + timedelta(minutes=1),
                )
            )
    return workspace_id, first, second


def mark_reset_archived(
    database: Database,
    workspace_id: str,
    pending_id: str,
    *,
    withdrawn: bool = False,
) -> None:
    """Archive a pending row the way a queue reset does, audit event included."""
    reset_at = utc_now()
    with database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        pending.archived_at = reset_at
        pending.withdrawn_at = reset_at if withdrawn else None
        session.add(
            AuditEvent(
                id=new_id(),
                workspace_id=workspace_id,
                action="pending_discovery.queue_reset",
                entity_type="pending_discovery",
                entity_id=pending_id,
                details={"discovery_url": pending.discovery_url},
                created_at=reset_at,
                created_by="local_user",
                origin="pending_queue.reset",
            )
        )


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@dataclass(frozen=True)
class RetainedPosting:
    """One publisher body to retain, with the listing facts its row was staged with."""

    title: str
    body: str
    url: str = ""
    location: str = ""
    requisition_id: str = ""
    places: tuple[str, ...] = ()
    arrangements: tuple[str, ...] = ()
    first_seen_on: date = date(2026, 9, 1)


def workspace_with_retained_postings(
    database: Database,
    tmp_path: Path,
    postings: Sequence[tuple[str, str] | RetainedPosting],
    *,
    parameters: JobSearchParameters | None = None,
    slug: str = "retained",
    employer: str = "Globex",
) -> tuple[str, list[str]]:
    """Build a workspace with a master CV and one retained publisher body per posting.

    ``postings`` pairs a title with its body, or states the rest of the listing
    through ``RetainedPosting``. Returns the workspace id and the
    pending-discovery ids in the order they were given.
    """
    root = tmp_path / f"{slug}-root"
    root.mkdir()
    workspace = WorkspaceService(database, runtime_storage(database, tmp_path)).create(
        slug=slug, name=slug.replace("-", " ").capitalize(), root_path=str(root)
    )
    workspace_id = str(workspace.id)
    SearchParametersService(database).save(workspace_id, parameters or JobSearchParameters())
    MasterCvService(database, runtime_storage(database, tmp_path)).save_edit(
        workspace_id, (FIXTURES / "master_cv.md").read_text(encoding="utf-8")
    )
    content_resolution = PostingContentResolutionService(
        database,
        DeterministicPostingContentResolver(),
        storage=runtime_storage(database, tmp_path),
    )
    pending_ids: list[str] = []
    for item in postings:
        spec = item if isinstance(item, RetainedPosting) else RetainedPosting(*item)
        url = spec.url or f"https://example.test/jobs/{spec.title.lower().replace(' ', '-')}"
        with database.session() as session:
            pending = PendingDiscovery(
                workspace_id=workspace_id,
                fingerprint=url_sha256(url),
                employer=employer,
                title=spec.title,
                location=spec.location,
                requisition_id=spec.requisition_id,
                staged_facts=_staged_facts(spec),
                discovery_url=url,
                reason="Publisher-listed role",
                first_seen_on=spec.first_seen_on,
            )
            session.add(pending)
            session.flush()
            pending_id = pending.id
        content_resolution.store_prefetched_result(
            workspace_id,
            pending_id,
            ResolvedPostingContent(
                requested_url=url,
                final_url=url,
                content_status=PostingContentAccessStatus.READABLE,
                identity_status=PostingIdentityStatus.VERIFIED,
                posting_status=PostingStatus.ACTIVE,
                extraction_method=PostingContentExtractionMethod.STATIC_HTML,
                fetched_at=utc_now(),
                content=spec.body.encode("utf-8"),
                http_status=200,
                publisher_markup_present=True,
                identity_evidence={"heading": spec.title},
                note="Verified publisher body.",
            ),
            None,
            origin="test",
        )
        pending_ids.append(pending_id)
    return workspace_id, pending_ids


def workspace_with_relevance_review(
    database: Database,
    tmp_path: Path,
    *,
    recommendation: str = "unlikely",
    slug: str = "relevance",
) -> tuple[str, str, str]:
    """Build a workspace holding one retained posting and one stored relevance review.

    Returns the workspace id, the pending-discovery id, and the review id.
    """
    body = "Data Engineer\n\nGlobex builds streaming pipelines for commerce ledgers."
    workspace_id, (pending_id,) = workspace_with_retained_postings(
        database, tmp_path, [("Data Engineer", body)], slug=slug
    )
    append_relevance_review(
        database,
        workspace_id,
        pending_id,
        body=body,
        review=judged_relevance_review(pending_id, recommendation=recommendation),
    )
    with database.session() as session:
        review = current_review_in(session, workspace_id, pending_id)
        assert review is not None
        review_id = review.id
    return workspace_id, pending_id, review_id


def judged_relevance_review(
    pending_id: str, *, recommendation: str = "unlikely"
) -> ValidatedRelevanceReview:
    """Build a judged core-gap review citing the posting's opening title span."""
    return ValidatedRelevanceReview(
        pending_discovery_id=pending_id,
        outcome="judged",
        recommendation=recommendation,  # type: ignore[arg-type]
        summary="The model judged this posting off the stated search.",
        core_work_alignment="absent",
        core_qualification_alignment="absent",
        hard_condition="passes",
        decisive_basis="core_gap",
        posting_citations=[
            RelevanceReviewCitation(
                span_id=f"{pending_id}:span-0001",
                text="Data Engineer",
                char_start=0,
                char_end=13,
            )
        ],
        master_cv_evidence_ids=[],
        unresolved_questions=[],
    )


def append_relevance_review(
    database: Database,
    workspace_id: str,
    pending_id: str,
    *,
    body: str,
    review: ValidatedRelevanceReview,
    contract: RelevanceReviewContract | None = None,
    keep_note: str | None = None,
) -> None:
    """Append one review of the given body through the recorder a paid run uses."""
    with database.session() as session:
        version_id = current_version_id(session, workspace_id)
        parameter_set_id = parameter_snapshot_in(session, workspace_id).parameter_set_id
        pending = session.get_one(PendingDiscovery, pending_id)
        session.expunge(pending)
    assert version_id is not None and parameter_set_id is not None
    task = TaskQueueService(database).enqueue(
        workspace_id=workspace_id,
        task_type="discovery.relevance_review",
        payload={"pending_discovery_ids": [pending_id]},
        idempotency_key=f"relevance-review-{pending_id}-{review.outcome}",
    )
    RelevanceReviewRecorder(database).append(
        workspace_id=workspace_id,
        task_id=task.id,
        master_cv_version_id=version_id,
        search_parameter_set_id=parameter_set_id,
        search_context="Synthetic search context.",
        contract=contract or current_review_contract(),
        pending=[pending],
        inputs={
            pending_id: RelevanceReviewPostingInput(
                body=body,
                fetched_at=datetime(2026, 9, 1, tzinfo=UTC),
            )
        },
        recommendations=[review],
        notes={pending_id: keep_note} if keep_note else {},
    )


def _staged_facts(spec: RetainedPosting) -> dict[str, object]:
    """Stage the publisher's own places and arrangements, or leave the row without facts."""
    if not spec.places and not spec.arrangements:
        return {}
    return StagedListingFacts(
        locations=_published(spec.places), arrangements=_published(spec.arrangements)
    ).as_json()


def _published(values: tuple[str, ...]) -> PublishedFact:
    if not values:
        return PublishedFact()
    return PublishedFact(values=values, provenance="board", complete=True)


# Every case runs as though today were this instant, so a baseline moves because
# something changed rather than because a month passed.
NOW = datetime(2026, 1, 2, 12, tzinfo=UTC)


def evaluation_commit(workspace_id: str, **changes: object) -> EvaluationCommit:
    values: dict[str, object] = {
        "workspace_id": workspace_id,
        "employer": "Acme Health",
        "title": "Product Manager, Data Platform",
        "location": "Canada remote",
        "requisition_id": "REQ-42",
        "canonical_url": "https://example.test/jobs/42?utm_source=feed",
        "discovery_url": "https://example.test/jobs/42",
        "first_seen_at": NOW,
        "observed_at": NOW,
        "posting_status": "active",
        "residence_rule": "Canada residents",
        "employment_type": "full-time",
        "payroll_arrangement": "Canadian payroll",
        "decision": EvaluationDecision.APPLY_NOW,
        "rationale": "Strong product and data-platform evidence.",
        "scope_summary": "Own schemas, quality, governance, and roadmap.",
        "qualifications_summary": "Evidence covers the published core requirements.",
        "evaluated_at": NOW,
        "scores": EvaluationScores(
            fit=ScoreComponentInput(value=90),
            compensation=ScoreComponentInput(value=80),
            eligibility=ScoreComponentInput(value=100),
            evidence=ScoreComponentInput(value=80),
            opportunity=ScoreComponentInput(value=70),
        ),
        "posting_body": PostingBodyInput(
            logical_type="posting.body.text",
            content="Lead product and data platform governance. Canada remote role.",
            extraction_method="direct_text",
            fetched_at=NOW,
            http_status=200,
        ),
        "candidate_record": {
            "content": "Led a governed data platform.",
            "source_uri": "test://master-cv",
            "effective_at": NOW,
        },
        "score_evidence": [
            ScoreEvidenceInput(
                key="candidate-platform",
                source_kind=EvidenceSourceKind.CANDIDATE,
                text="data platform",
                char_start=15,
                char_end=28,
                components=[ScoreComponent.FIT, ScoreComponent.EVIDENCE],
                classification=EvidenceClassification.DIRECT,
                strength=EvidenceStrength.STRONG,
                requirement_id="REQ-1",
                master_cv_entry_id="cv-1",
            ),
            ScoreEvidenceInput(
                key="posting-location",
                source_kind=EvidenceSourceKind.POSTING,
                text="Canada remote",
                char_start=43,
                char_end=56,
                components=[
                    ScoreComponent.COMPENSATION,
                    ScoreComponent.ELIGIBILITY,
                    ScoreComponent.OPPORTUNITY,
                ],
                classification=EvidenceClassification.DIRECT,
                strength=EvidenceStrength.MODERATE,
                requirement_id="REQ-2",
            ),
        ],
        "fit_report": {
            "prompt_fingerprint": "test-suite-v1",
            "hard_gates": [
                {
                    "requirement_id": "gate-1",
                    "importance": "must_have",
                    "text": "Canada remote",
                    "evidence_keys": ["posting-location"],
                }
            ],
            "reading": [
                {
                    "question": "Where the work happens",
                    "answer": "remote",
                    "evidence_keys": ["posting-location"],
                }
            ],
            "requirements": [
                {
                    "requirement_id": "REQ-1",
                    "importance": "must_have",
                    "text": "Lead a data platform",
                    "evidence_keys": ["candidate-platform"],
                },
                {
                    "requirement_id": "REQ-2",
                    "importance": "must_have",
                    "text": "Canada remote",
                    "evidence_keys": ["posting-location"],
                },
            ],
            "coverage_present": 2,
            "coverage_total": 2,
            "checks": [{"check": "factual support", "result": "pass"}],
            "length_budget": "No derivative document was generated during evaluation.",
            "provenance": [],
        },
        "pros": [{"text": "Direct platform ownership.", "evidence_keys": ["candidate-platform"]}],
        "cons": [{"text": "Domain ramp-up."}],
        "gaps": [
            {
                "text": "No named warehouse product.",
                "evidence_keys": ["posting-location"],
                "direction": "limits",
            }
        ],
        "risks": [
            {
                "text": "Posting may close quickly.",
                "evidence_keys": ["posting-location"],
                "direction": "limits",
            }
        ],
        "compensation": [
            CompensationInput(
                component_type="base",
                amount_min=Decimal(150000),
                amount_max=Decimal(180000),
                currency="CAD",
                basis="annual",
                geography="Canada",
            )
        ],
        "eligibility": EligibilityInput(
            workspace_location_key="explicit_canada",
            arrangement="remote",
        ),
    }
    values.update(changes)
    if "fit_report" in changes and "score_evidence" not in changes:
        report = changes["fit_report"]
        assert isinstance(report, dict)
        requirement_ids: set[str] = set()
        for section in ("hard_gates", "requirements"):
            requirements = report.get(section, [])
            assert isinstance(requirements, list)
            for requirement in requirements:
                assert isinstance(requirement, dict)
                requirement_ids.add(str(requirement["requirement_id"]))
        evidence_rows = values["score_evidence"]
        assert isinstance(evidence_rows, list)
        values["score_evidence"] = [
            evidence
            if evidence.requirement_id is None or evidence.requirement_id in requirement_ids
            else evidence.model_copy(update={"requirement_id": None})
            for evidence in evidence_rows
            if isinstance(evidence, ScoreEvidenceInput)
        ]
    return EvaluationCommit.model_validate(values)


@dataclass(frozen=True)
class CommittedEvaluation:
    """A fresh workspace holding exactly one committed evaluation."""

    workspace_id: str
    artifacts: EvidenceArtifactService
    proposal: EvaluationCommit
    detail: JobDetailResponse

    @property
    def job_posting_id(self) -> str:
        """The posting the ledger now lists for this workspace."""
        return self.detail.job_posting_id


def workspace_with_committed_evaluation(
    database: Database,
    artifact_root: Path,
    slug: str,
    *,
    correlation_id: str | None = None,
    **changes: object,
) -> CommittedEvaluation:
    """Create a workspace named for the case and commit one evaluation into it."""
    workspace = WorkspaceService(database, runtime_storage(database)).create(slug=slug, name=slug)
    artifacts = artifact_service(database, artifact_root / "artifacts")
    proposal = evaluation_commit(workspace.id, **changes)
    detail = job_search.commit_evaluation(
        database, artifacts, proposal, correlation_id=correlation_id
    )
    return CommittedEvaluation(workspace.id, artifacts, proposal, detail)


def seed_reusable_tailoring_run(
    database: Database,
    service: WorkflowService,
    workspace_id: str,
    job_id: str,
    master_version_id: str,
) -> dict[str, Any]:
    """Persist a completed run whose manifest matches this job's current inputs exactly."""
    inputs = service.inputs(workspace_id, job_id)
    research = (
        ResearchSource(
            id="company.posting",
            kind="posting",
            url=inputs.posting_url,
            text=inputs.posting_content.decode("utf-8"),
            method="stored_posting_artifact",
        ),
        ResearchSource(
            id="company.about",
            kind="about",
            url="https://example.test/about",
            text=(
                "The employer builds reliable public systems for city and county agencies. "
                "Small teams own each service from design through operation, publish their "
                "runbooks, and review every outage in the open. Most staff work remotely "
                "within the country and meet in person twice a year to plan the next release."
            ),
            method="same_host_about",
        ),
    )
    with prompts.overriding({key: text for key, (_, text) in inputs.prompt_overrides.items()}):
        prompt_suite = prompts.suite_fingerprint()
    manifest = {
        "posting": {
            "jd_hash": posting.content_hash(inputs.posting_content.decode("utf-8")),
        },
        "inputs": {
            "cv_hash": cv.content_hash(inputs.master_content.decode("utf-8")),
            "prompt_suite": prompt_suite,
            "settings_hash": runs.settings_hash(inputs.settings),
            "cover_letter": True,
            "research_hash": fingerprint(research),
            "trimmed": 0,
            "approval": "",
            "tailorcv": __version__,
        },
        "run": {"outcome": "passed"},
    }
    seed_task = TaskQueueService(database).enqueue(
        workspace_id=workspace_id,
        task_type="workflow.tailor",
        payload={"job_posting_id": job_id},
        idempotency_key="preflight-seed",
    )
    persisted = service.persist_tailoring(
        workspace_id=workspace_id,
        job_id=job_id,
        master_version_id=master_version_id,
        task_id=str(seed_task.id),
        output=TailorOutput(
            artifacts=serialized_artifacts(
                {
                    "resume.markdown": ("text/markdown", b"# Candidate\n"),
                    "cover_letter.markdown": (
                        "text/markdown",
                        b"Dear hiring team,\n",
                    ),
                },
                manifest=manifest,
                review={},
            ),
            outcome=TailoringOutcome.PASSED,
            ledger=Ledger(),
        ),
        started_at=datetime.now(UTC),
        research=research,
    )
    with database.session() as session:
        stored_seed = session.get(BackgroundTask, seed_task.id)
        assert stored_seed is not None
        stored_seed.status = "completed"
    return persisted


def studio_workspace(
    database: Database, root: Path, artifact_root: Path
) -> tuple[str, str, str, str]:
    """Seed one workspace with an employer, a posting, a CV version, and an application.

    Returns the workspace, job posting, application, and master-CV version
    identifiers the tailoring surfaces are addressed by.
    """
    root.mkdir()
    master = root / "master.md"
    content = (FIXTURES / "master_cv.md").read_bytes()
    master.write_bytes(content)
    workspace = WorkspaceService(database, runtime_storage(database)).create(
        slug="studio", name="Studio", root_path=str(root)
    )
    workspace_id = str(workspace.id)
    documents = DocumentService(database, runtime_storage(database, artifact_root))
    with database.session() as session:
        employer = Employer(
            workspace_id=workspace_id,
            name="Acme",
            normalized_name="acme",
        )
        session.add(employer)
        session.flush()
        job = JobPosting(
            workspace_id=workspace_id,
            employer_id=employer.id,
            title="Platform Lead",
            normalized_title="platform lead",
            first_seen_at=datetime.now(UTC),
        )
        session.add(job)
        session.flush()
        candidate = CandidateProfile(
            workspace_id=workspace_id,
            display_name="Candidate",
        )
        session.add(candidate)
        session.flush()
        version = MasterCvVersion(
            candidate_profile_id=candidate.id,
            content_sha256=hashlib.sha256(content).hexdigest(),
            source_uri=master.as_uri(),
            effective_at=datetime.now(UTC),
            byte_size=len(content),
        )
        session.add(version)
        session.flush()
        posting = documents.store_generated(
            session,
            workspace_id=workspace_id,
            job_posting_id=job.id,
            tailoring_run_id=None,
            logical_type="posting.body.text",
            mime_type="text/plain",
            content=b"Acme needs a platform lead with Python experience.",
            provenance={"source": "test"},
            author_type="system",
        )
        session.add(
            PostingObservation(
                job_posting_id=job.id,
                observed_at=datetime.now(UTC),
                posting_status="active",
                body_artifact_id=posting.id,
            )
        )
        application = Application(workspace_id=workspace_id, job_posting_id=job.id)
        session.add(application)
        session.flush()
        return workspace_id, job.id, application.id, version.id


def fit_report(*requirements: dict[str, object]) -> dict[str, object]:
    """Return a fit_report payload carrying the caller's requirements."""
    hard_gates = []
    if any(requirement.get("text") == "Canada remote" for requirement in requirements):
        hard_gates = [
            {
                "requirement_id": "gate-1",
                "importance": "must_have",
                "text": "Canada remote",
                "evidence_keys": [],
            }
        ]
    return {
        "prompt_fingerprint": "test-suite-v1",
        "hard_gates": hard_gates,
        "reading": [
            {
                "question": "Where the work happens",
                "answer": "remote",
                "evidence_keys": ["posting-location"],
            }
        ],
        "requirements": list(requirements),
        "coverage_present": len(requirements),
        "coverage_total": len(requirements),
        "checks": [{"check": "factual support", "result": "pass"}],
        "length_budget": "No derivative document was generated during evaluation.",
        "provenance": [],
    }


def seed_application_fit_report(
    database: Database,
    documents: DocumentService,
    *,
    workspace_id: str,
    job_id: str,
    master_version_id: str,
    task_id: str,
) -> str:
    """Create a run and its fit report through the application document seam."""
    with database.session() as session:
        run = TailoringRun(
            workspace_id=workspace_id,
            job_posting_id=job_id,
            master_cv_version_id=master_version_id,
            task_id=task_id,
            outcome="passed",
            manifest={},
        )
        session.add(run)
        evaluation = JobEvaluation(
            job_posting_id=job_id,
            decision="apply_now",
            rationale="Fits.",
            payload_sha256="b" * 64,
            evaluated_at=datetime.now(UTC),
        )
        session.add(evaluation)
        session.flush()
        fit_report = documents.evidence_store.store(
            session,
            workspace_id=workspace_id,
            logical_type="evaluation.fit_report.markdown",
            content=b"# Fit\n",
            relative_path=f"{workspace_id}/evaluations/{evaluation.id}/fit-report.md",
            mime_type="text/markdown",
            related_entity_type="job_evaluation",
            related_entity_id=evaluation.id,
            provenance={"source": "test"},
            audit={},
        )
        return fit_report.id


def populated_workspace(client: TestClient, slug: str, employer: str) -> dict[str, str]:
    """One workspace holding every record kind a screen can show.

    Built through the shipped API rather than by inserting rows, so the scenario
    cannot drift from what the application would have written.
    """
    workspace_id = client.post(
        "/api/v1/workspaces", json={"slug": slug, "name": slug.title()}
    ).json()["id"]
    evaluation = client.post(
        "/api/v1/evaluations",
        json=evaluation_commit(workspace_id, employer=employer).model_dump(mode="json"),
    )
    assert evaluation.status_code == 201, evaluation.text
    job_id = evaluation.json()["job_posting_id"]
    note = client.post(
        "/api/v1/notes",
        json={
            "workspace_id": workspace_id,
            "entity_type": "job_posting",
            "entity_id": job_id,
            "title": f"Call notes for {employer}",
            "body": f"A private note about {employer}.",
        },
    )
    assert note.status_code == 201, note.text
    tag = client.post(
        "/api/v1/tags",
        json={"workspace_id": workspace_id, "name": f"{employer} shortlist", "color": "#336699"},
    )
    assert tag.status_code == 201, tag.text
    view = client.post(
        "/api/v1/saved-views",
        json={
            "workspace_id": workspace_id,
            "resource": "jobs",
            "name": f"{employer} shortlist",
            "configuration": {"filters": {"search": employer}, "sort": {"field": "overall"}},
        },
    )
    assert view.status_code == 201, view.text
    task = client.post(
        "/api/v1/tasks",
        json={
            "workspace_id": workspace_id,
            "task_type": "artifact.verify",
            "payload": {"artifact_id": f"{slug}-artifact"},
        },
        headers={"Idempotency-Key": f"{slug}-verify"},
    )
    assert task.status_code == 202, task.text
    return {
        "workspace": workspace_id,
        "job": job_id,
        "task": task.json()["id"],
        "slug": slug,
    }
