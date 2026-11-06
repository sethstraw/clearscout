from __future__ import annotations

import hashlib
import html
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from threading import Barrier, Event
from typing import Any, cast

import httpx
import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from clearscout.application.discovery.content_resolution import (
    CUT_OFF_SKIP_REASON,
    LISTING_PAGE_NOTE,
    CurrentPostingContentReader,
    PostingContentResolutionService,
    PrefetchedContent,
    ResolvedPostingContent,
    pending_discovery_readiness,
    resolved_publisher_fetch,
    retained_text_cut_off,
)
from clearscout.application.discovery.dispositions import DiscoveryDispositionService
from clearscout.application.discovery.hints import PendingDiscoveryHintsService
from clearscout.application.discovery.pending import (
    latest_disposition_statement,
    reset_pending_queue,
    set_pending_markers,
    withdrawal_causes,
)
from clearscout.application.discovery.posting_fetch import FetchedPosting
from clearscout.application.discovery.posting_refresh import PostingRefreshService
from clearscout.application.discovery.posting_text import (
    publisher_closing_date,
    publisher_posted_date,
)
from clearscout.application.discovery.relevance_disputes import RelevanceDisputeService
from clearscout.application.discovery.relevance_review import current_review_contract
from clearscout.application.discovery.relevance_review_preparation import (
    RelevanceReviewPreparationService,
)
from clearscout.application.discovery.relevance_review_queries import RelevanceReviewQueries
from clearscout.application.discovery.relevance_review_records import ValidatedRelevanceReview
from clearscout.application.discovery.relevance_review_rejections import (
    RelevanceReviewItemFailure,
    RelevanceReviewRejectionService,
)
from clearscout.application.discovery.scan_queries import SourceScanQueries
from clearscout.application.discovery.scan_staging import DiscoveryStaging
from clearscout.application.discovery.scan_staging.publisher_facts import apply_publisher_check
from clearscout.application.discovery.source_reading import (
    PostingCheck,
    SourceListing,
    SourceReadOptions,
)
from clearscout.application.failures import ConflictError, NotFoundError, ValidationError
from clearscout.application.job_search.evaluation_targets import evaluated_jobs_for_pending
from clearscout.application.job_search.models import (
    PendingDiscoveryResponse,
    StagedListingFactsResponse,
)
from clearscout.application.tailoring import fit
from clearscout.application.tailoring.workflows import EvaluationWorkflowService
from clearscout.application.tasks import TaskQueueService
from clearscout.application.tasks.payloads import DiscoveryEvaluateBatchPayload
from clearscout.application.workspaces import SearchParametersService, WorkspaceService
from clearscout.application.workspaces.records.access import audit_columns
from clearscout.domain import (
    PendingDiscoveryReadiness,
    PendingWithdrawalCause,
    PostingContentAccessStatus,
    PostingContentExtractionMethod,
    PostingIdentityStatus,
    PostingStatus,
    utc_now,
)
from clearscout.domain.artifacts import PostingSource
from clearscout.domain.identity import url_sha256
from clearscout.domain.records import PostingReadIntent, RobotsReadBasis
from clearscout.domain.search_parameters import (
    Acceptance,
    Arrangement,
    CountryNames,
    JobSearchParameters,
    Location,
    Seniority,
)
from clearscout.domain.staged_facts import (
    ListingIdentityTrust,
    PostedDateGrade,
    PublishedFact,
    PublisherPostedDate,
    StagedListingFacts,
)
from clearscout.infrastructure.artifacts.store import file_uri_path
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    Artifact,
    AuditEvent,
    PendingDiscovery,
    PendingDiscoveryDispositionEvent,
    PendingPostingContent,
    RelevanceReview,
    RelevanceReviewDispute,
    Workspace,
)
from clearscout.infrastructure.web_fetch.ingest.url import guarded_posting_reads
from clearscout.infrastructure.web_fetch.posting_resolution import (
    DeterministicPostingContentResolver,
)
from clearscout.infrastructure.web_fetch.rendered import RenderRuntimeUnavailableError
from tests.support.application_forms import (
    FORM_PLATFORMS,
    US_STATE_OPTIONS,
    FormPlatform,
    form_source,
)
from tests.support.artifacts import artifact_service, runtime_storage, strand_promotion
from tests.support.doubles import UnexpectedPostingFetcher
from tests.support.http import DAYFORCE_POSTING_URL, dayforce_feed_row, policy_client
from tests.support.scenarios import (
    RetainedPosting,
    append_relevance_review,
    board_source,
    canadian_pay_with_a_us_place,
    dismiss_pending_discovery,
    judged_relevance_review,
    pending_discovery,
    pending_posting_content,
    published_location_facts,
    record_relevance_review,
    workspace_with_committed_evaluation,
    workspace_with_retained_postings,
)


class ScriptedResolver:
    def __init__(self, results: list[ResolvedPostingContent]) -> None:
        self.results = results
        self.calls: list[tuple[str, str, str]] = []
        self.intents: list[PostingReadIntent] = []

    def resolve(
        self,
        url: str,
        *,
        employer: str,
        title: str,
        intent: PostingReadIntent = PostingReadIntent.CRAWL,
        board_url: str = "",
    ) -> ResolvedPostingContent:
        self.calls.append((url, employer, title))
        self.intents.append(intent)
        return self.results.pop(0)


class _DeclaringStatic:
    def __init__(self, fetched: FetchedPosting) -> None:
        self.fetched = fetched

    def fetch(self, _url: str, **_kwargs: object) -> FetchedPosting:
        return self.fetched


def _add_pending(
    database: Database,
    workspace_id: str,
    *,
    employer: str,
    title: str,
    first_seen_on: date,
    url: str,
    source_id: str | None = None,
) -> str:
    """Add one persisted discovery for content-resolution scenarios."""
    with database.session() as session:
        pending = PendingDiscovery(
            workspace_id=workspace_id,
            fingerprint=url_sha256(url),
            employer=employer,
            title=title,
            discovery_url=url,
            reason="Publisher-listed role",
            first_seen_on=first_seen_on,
            source_id=source_id,
        )
        session.add(pending)
        session.flush()
        return pending.id


def _pending(database: Database, artifact_root: Path) -> tuple[str, str, str]:
    workspace = WorkspaceService(database, runtime_storage(database, artifact_root)).create(
        slug="content-resolution", name="Content resolution"
    )
    url = "https://example.test/jobs/operations-lead"
    return (
        workspace.id,
        _add_pending(
            database,
            workspace.id,
            employer="Acme",
            title="Operations Lead",
            first_seen_on=date(2026, 8, 28),
            url=url,
        ),
        url,
    )


def _readable(
    url: str,
    body: bytes = b"Operations Lead\nBuild reliable systems.",
    *,
    identity_status: PostingIdentityStatus = PostingIdentityStatus.VERIFIED,
    robots_basis: RobotsReadBasis | None = None,
) -> ResolvedPostingContent:
    return ResolvedPostingContent(
        requested_url=url,
        final_url=url,
        content_status=PostingContentAccessStatus.READABLE,
        identity_status=identity_status,
        posting_status=PostingStatus.ACTIVE,
        extraction_method=PostingContentExtractionMethod.STATIC_HTML,
        fetched_at=datetime(2026, 8, 28, tzinfo=UTC),
        content=body,
        http_status=200,
        publisher_markup_present=False,
        identity_evidence={"heading": "Operations Lead"},
        note="Verified from the publisher page.",
        robots_basis=robots_basis,
    )


def _resolve(
    database: Database,
    tmp_path: Path,
    workspace_id: str,
    pending_id: str,
    url: str,
    body: bytes,
    *,
    identity_status: PostingIdentityStatus = PostingIdentityStatus.VERIFIED,
    origin: str = "discovery.refresh_batch",
    source: PostingSource | None = None,
) -> None:
    """Resolve one pending row's content through the ordinary batch path."""
    read = replace(_readable(url, body, identity_status=identity_status), source=source)
    service = PostingContentResolutionService(
        database,
        ScriptedResolver([read]),
        storage=runtime_storage(database, tmp_path),
    )
    service.resolve_batch(
        workspace_id,
        [pending_id],
        service.preflight(workspace_id, [pending_id]).snapshot_sha256,
        origin=origin,
    )


def _relevance_review_count(database: Database, pending_id: str) -> int:
    with database.session() as session:
        return len(
            session.scalars(
                select(RelevanceReview.id).where(RelevanceReview.pending_discovery_id == pending_id)
            ).all()
        )


def test_replacing_current_content_with_a_different_body_drops_its_relevance_review(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    service = PostingContentResolutionService(
        durable_database,
        ScriptedResolver(
            [_readable(url), _readable(url, b"Operations Lead\nRun regional logistics.")]
        ),
        storage=runtime_storage(durable_database, tmp_path),
    )
    service.resolve_batch(
        workspace_id,
        [pending_id],
        service.preflight(workspace_id, [pending_id]).snapshot_sha256,
        origin="discovery.refresh_batch",
    )
    record_relevance_review(durable_database, workspace_id, pending_id)
    assert _relevance_review_count(durable_database, pending_id) == 1

    service.resolve_batch(
        workspace_id,
        [pending_id],
        service.preflight(workspace_id, [pending_id]).snapshot_sha256,
        origin="discovery.refresh_batch",
    )

    assert _relevance_review_count(durable_database, pending_id) == 0


def test_new_words_clear_a_disputed_review_with_its_dispute_and_audit_the_clearing(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    _resolve(durable_database, tmp_path, workspace_id, pending_id, url, b"Operations Lead\nShip.")
    record_relevance_review(durable_database, workspace_id, pending_id)
    with durable_database.session() as session:
        review_id = session.scalars(
            select(RelevanceReview.id).where(RelevanceReview.pending_discovery_id == pending_id)
        ).one()
    RelevanceDisputeService(durable_database).dispute(
        workspace_id=workspace_id,
        pending_discovery_id=pending_id,
        relevance_review_id=review_id,
        note=None,
        audit=audit_columns("local_user", "test", None),
    )

    _resolve(
        durable_database, tmp_path, workspace_id, pending_id, url, b"Operations Lead\nPlan routes."
    )

    assert _relevance_review_count(durable_database, pending_id) == 0
    with durable_database.session() as session:
        assert session.scalars(select(RelevanceReviewDispute.id)).all() == []
        cleared = session.scalars(
            select(AuditEvent.details).where(AuditEvent.action == "relevance_dispute.cleared")
        ).all()
    assert cleared == [{"pending_discovery_id": pending_id, "relevance_review_id": review_id}]


def test_a_reread_that_only_relays_the_body_keeps_its_relevance_review(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    body = b"Operations Lead\nBuild reliable systems."
    _resolve(durable_database, tmp_path, workspace_id, pending_id, url, body)
    record_relevance_review(durable_database, workspace_id, pending_id)

    _resolve(
        durable_database,
        tmp_path,
        workspace_id,
        pending_id,
        url,
        b"Operations  Lead\n\n\nBuild  reliable systems. ",
    )

    assert _relevance_review_count(durable_database, pending_id) == 1


def _current_body(database: Database, tmp_path: Path, workspace_id: str, pending_id: str) -> bytes:
    with database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        current = session.get(PendingPostingContent, pending.current_content_id)
        assert current is not None
        artifact = session.get(Artifact, current.artifact_id)
        workspace = session.get(Workspace, workspace_id)
        assert artifact is not None and workspace is not None
        body = artifact_service(database, tmp_path).read(artifact, workspace)
        assert current.content_sha256 == hashlib.sha256(body).hexdigest()
        return body


def test_a_reread_that_lays_a_flat_body_out_in_lines_replaces_it(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    _resolve(durable_database, tmp_path, workspace_id, pending_id, url, b"Operations Lead Ship.")
    lined = b"Operations Lead\n\nShip."

    _resolve(durable_database, tmp_path, workspace_id, pending_id, url, lined)

    assert _current_body(durable_database, tmp_path, workspace_id, pending_id) == lined


def test_a_reread_that_lays_the_same_words_out_in_lines_keeps_its_relevance_review(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    _resolve(durable_database, tmp_path, workspace_id, pending_id, url, b"Operations Lead Ship.")
    record_relevance_review(durable_database, workspace_id, pending_id)

    _resolve(
        durable_database, tmp_path, workspace_id, pending_id, url, b"Operations Lead\n\n- Ship."
    )

    assert _relevance_review_count(durable_database, pending_id) == 1


def test_a_body_stranded_before_promotion_keeps_its_review_after_reconcile(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    _resolve(durable_database, tmp_path, workspace_id, pending_id, url, b"Operations Lead Ship.")
    record_relevance_review(durable_database, workspace_id, pending_id)
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        content = session.get(PendingPostingContent, pending.current_content_id)
        assert content is not None and content.artifact_id is not None
        artifact_id = content.artifact_id
    strand_promotion(durable_database, tmp_path, artifact_id)

    artifact_service(durable_database, tmp_path).reconcile(durable_database)
    _resolve(
        durable_database, tmp_path, workspace_id, pending_id, url, b"Operations Lead\n\n- Ship."
    )

    assert _relevance_review_count(durable_database, pending_id) == 1
    with durable_database.session() as session:
        artifact = session.get(Artifact, artifact_id)
        assert artifact is not None and artifact.integrity_status == "verified"


def _fail(
    database: Database,
    tmp_path: Path,
    workspace_id: str,
    pending_id: str,
    url: str,
    **changes: Any,
) -> None:
    """Record one reread that reached no body through the ordinary batch path."""
    failed = replace(
        _readable(url),
        **{
            "content": None,
            "content_status": PostingContentAccessStatus.FETCH_FAILED,
            "identity_status": PostingIdentityStatus.NOT_YET_VERIFIED,
            "posting_status": None,
            "extraction_method": None,
            "http_status": 503,
            **changes,
        },
    )
    service = PostingContentResolutionService(
        database, ScriptedResolver([failed]), storage=runtime_storage(database, tmp_path)
    )
    service.resolve_batch(
        workspace_id,
        [pending_id],
        service.preflight(workspace_id, [pending_id]).snapshot_sha256,
        origin="discovery.refresh_batch",
    )


def test_a_reread_that_fails_without_a_body_keeps_its_relevance_review(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    _resolve(durable_database, tmp_path, workspace_id, pending_id, url, b"Operations Lead\nShip.")
    record_relevance_review(durable_database, workspace_id, pending_id)

    _fail(durable_database, tmp_path, workspace_id, pending_id, url)

    assert _relevance_review_count(durable_database, pending_id) == 1


def test_a_failed_reread_keeps_the_readable_body_current_and_retains_what_it_saw(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    _resolve(durable_database, tmp_path, workspace_id, pending_id, url, b"Operations Lead\nShip.")
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        readable_id = pending.current_content_id

    error = {"error": "The publisher returned HTTP 503."}
    _fail(durable_database, tmp_path, workspace_id, pending_id, url, identity_evidence=error)

    storage = runtime_storage(durable_database, tmp_path)
    current = CurrentPostingContentReader(durable_database, storage=storage).read(
        workspace_id, pending_id
    )
    assert current is not None and current.body == b"Operations Lead\nShip."
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        assert pending.current_content_id == readable_id
        assert pending.content_status == PostingContentAccessStatus.READABLE
        assert pending.content_note.endswith("The retained posting body remains current.")
        failed = session.scalars(
            select(PendingPostingContent).where(
                PendingPostingContent.pending_discovery_id == pending_id,
                PendingPostingContent.content_status == PostingContentAccessStatus.FETCH_FAILED,
            )
        ).one()
        assert (failed.http_status, failed.final_url, failed.artifact_id) == (503, url, None)
        assert failed.identity_evidence == error


def test_a_reread_landing_on_another_page_replaces_the_readable_body(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    _resolve(durable_database, tmp_path, workspace_id, pending_id, url, b"Operations Lead\nShip.")

    _fail(
        durable_database,
        tmp_path,
        workspace_id,
        pending_id,
        url,
        content_status=PostingContentAccessStatus.RENDER_REQUIRED,
        identity_status=PostingIdentityStatus.CONTRADICTED,
        final_url="https://example.test/careers",
        http_status=200,
    )

    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        assert pending.content_status == PostingContentAccessStatus.RENDER_REQUIRED


def test_the_same_words_flattened_after_a_failed_reread_keep_the_layout_and_review(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    lined = b"Operations Lead\n\nShip."
    _resolve(durable_database, tmp_path, workspace_id, pending_id, url, lined)
    record_relevance_review(durable_database, workspace_id, pending_id)
    _fail(durable_database, tmp_path, workspace_id, pending_id, url)

    _resolve(durable_database, tmp_path, workspace_id, pending_id, url, b"Operations  Lead Ship.")

    assert _relevance_review_count(durable_database, pending_id) == 1
    assert _current_body(durable_database, tmp_path, workspace_id, pending_id) == lined


def test_a_reread_that_flattens_a_lined_body_keeps_the_stored_bytes(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    lined = b"Operations Lead\n\nShip."
    _resolve(durable_database, tmp_path, workspace_id, pending_id, url, lined)

    _resolve(durable_database, tmp_path, workspace_id, pending_id, url, b"Operations  Lead Ship.")

    assert _current_body(durable_database, tmp_path, workspace_id, pending_id) == lined


@pytest.mark.parametrize(
    "relaid", [b"Operations Lead\nShip.", b"Operations  Lead\n\nShip."], ids=["same", "relaid"]
)
def test_a_reread_over_a_lost_stored_body_retains_the_fresh_bytes(
    durable_database: Database, tmp_path: Path, relaid: bytes
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    _resolve(durable_database, tmp_path, workspace_id, pending_id, url, b"Operations Lead\nShip.")
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        lost = session.get(PendingPostingContent, pending.current_content_id)
        assert lost is not None
        artifact = session.get(Artifact, lost.artifact_id)
        assert artifact is not None
        stored = file_uri_path(artifact.storage_uri)
        assert stored is not None
    stored.unlink()

    _resolve(durable_database, tmp_path, workspace_id, pending_id, url, relaid)

    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        current = session.get(PendingPostingContent, pending.current_content_id)
        assert current is not None
        assert current.content_sha256 == hashlib.sha256(relaid).hexdigest()
        artifact = session.get(Artifact, current.artifact_id)
        workspace = session.get(Workspace, workspace_id)
        assert artifact is not None and workspace is not None
        assert artifact_service(durable_database, tmp_path).read(artifact, workspace) == relaid


def test_replacing_current_content_with_a_different_body_clears_its_rejected_review_mark(
    durable_database: Database, tmp_path: Path
) -> None:
    """A mark about a body the publisher replaced would keep sorting the row by it."""
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    service = PostingContentResolutionService(
        durable_database,
        ScriptedResolver(
            [_readable(url), _readable(url, b"Operations Lead\nRun regional logistics.")]
        ),
        storage=runtime_storage(durable_database, tmp_path),
    )
    service.resolve_batch(
        workspace_id,
        [pending_id],
        service.preflight(workspace_id, [pending_id]).snapshot_sha256,
        origin="discovery.refresh_batch",
    )
    task = TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="discovery.relevance_review",
        payload={"pending_discovery_ids": [pending_id]},
        idempotency_key="content-resolution-rejection",
    )
    RelevanceReviewRejectionService(durable_database).record(
        workspace_id=workspace_id,
        task_id=task.id,
        rejected=[RelevanceReviewItemFailure(pending_id, "missing_result", "Nothing came back.")],
    )

    service.resolve_batch(
        workspace_id,
        [pending_id],
        service.preflight(workspace_id, [pending_id]).snapshot_sha256,
        origin="discovery.refresh_batch",
    )

    row = SourceScanQueries(durable_database).get_pending_discovery(workspace_id, pending_id)
    assert row.relevance_review_rejection is None


_EMPTY_BODY = b"Operations Lead\nSign in to see this posting."


def _dismissed_for_no_requirements(database: Database, tmp_path: Path) -> tuple[str, str, str]:
    """Retain one body and dismiss it the way an empty-requirements evaluation does."""
    workspace_id, pending_id, url = _pending(database, tmp_path)
    _resolve(database, tmp_path, workspace_id, pending_id, url, _EMPTY_BODY)
    with database.session() as session:
        DiscoveryDispositionService(database).dismiss_no_requirements_in_session(
            session,
            workspace_id,
            pending_id,
            content_sha256=hashlib.sha256(_EMPTY_BODY).hexdigest(),
            actor="worker",
            origin="evaluation.body_screen",
            correlation_id="evaluate-1",
        )
    return workspace_id, pending_id, url


def _latest_disposition(
    database: Database, workspace_id: str, pending_id: str
) -> tuple[str, str | None, str]:
    with database.session() as session:
        event = session.scalars(latest_disposition_statement(workspace_id, [pending_id])).one()
        return event.action, event.restore_provenance, event.category


@pytest.mark.parametrize("origin", ["discovery.refresh_batch", "source.scan"])
def test_storing_the_same_body_leaves_a_no_requirements_dismissal_standing(
    durable_database: Database, tmp_path: Path, origin: str
) -> None:
    workspace_id, pending_id, url = _dismissed_for_no_requirements(durable_database, tmp_path)

    _resolve(durable_database, tmp_path, workspace_id, pending_id, url, _EMPTY_BODY, origin=origin)

    assert _latest_disposition(durable_database, workspace_id, pending_id) == (
        "dismiss",
        None,
        "no_requirements",
    )


def test_a_changed_body_restores_a_no_requirements_dismissal_for_audit(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _dismissed_for_no_requirements(durable_database, tmp_path)

    _resolve(
        durable_database,
        tmp_path,
        workspace_id,
        pending_id,
        url,
        b"Operations Lead\nFive years running regional logistics is required.",
    )

    assert _latest_disposition(durable_database, workspace_id, pending_id) == (
        "restore",
        "audit",
        "no_requirements",
    )


def _canada_home_with_us_remote(
    *, us_remote: Arrangement | None = None, elsewhere: tuple[Location, ...] = ()
) -> JobSearchParameters:
    """A person living in Canada and authorized for US remote work they will not move for."""
    locations = [
        Location(key="toronto", country="Canada", resident=True),
        Location(key="us_remote", country="United States", arrangement=us_remote),
        *elsewhere,
    ]
    return JobSearchParameters(
        locations=locations,
        countries=[CountryNames(country=place.country) for place in locations],
    )


def _body_screened(
    database: Database,
    tmp_path: Path,
    parameters: JobSearchParameters,
    rule: str,
    *,
    location: str = "Remote - United States",
    label: str | None = None,
    source: PostingSource | None = None,
    staged: StagedListingFacts | None = None,
) -> tuple[str, str] | None:
    """Read one body stating ``rule`` in a scan and return its dismissal, if any.

    The body's Location line states ``label``, else the row's location; an empty one is left out.
    """
    workspace_id, pending_id, url = _pending(database, tmp_path)
    SearchParametersService(database).save(workspace_id, parameters)
    with database.session() as session:
        row = session.get_one(PendingDiscovery, pending_id)
        row.location = location
        if staged is None:
            staged = published_location_facts(location) if location else StagedListingFacts()
        row.staged_facts = staged.as_json()
    stated = location if label is None else label
    line = f"Location: {stated}\n" if stated else ""
    body = (
        f"Operations Lead\n{line}\n"
        f"{rule}\n\nRun the regional carrier desk and own the weekly service review.\n"
    )
    _resolve(
        database,
        tmp_path,
        workspace_id,
        pending_id,
        url,
        body.encode(),
        origin="source.scan",
        source=source,
    )
    with database.session() as session:
        event = session.scalars(
            latest_disposition_statement(workspace_id, [pending_id])
        ).one_or_none()
        return None if event is None else (event.category, event.reason)


@pytest.mark.parametrize(
    "rule",
    [
        "Candidates must reside in the United States.",
        "Must reside in the United States within one of the states listed below: Texas, Ohio.",
    ],
)
def test_a_body_requiring_residence_where_the_person_neither_lives_nor_moves_is_dismissed(
    durable_database: Database, tmp_path: Path, rule: str
) -> None:
    """Authorization to work remotely in a country does not meet a rule to live there."""
    screened = _body_screened(durable_database, tmp_path, _canada_home_with_us_remote(), rule)

    assert screened is not None
    assert screened[0] == "location_residence_required"
    assert rule in screened[1]


_STATE_LIST = (
    "You must reside in one of the following states: Arizona, California, Colorado, "
    "Florida, Georgia, Illinois, Massachusetts."
)


@pytest.mark.parametrize(
    "rule",
    [
        _STATE_LIST,
        "Candidates must reside in Texas or Oklahoma.",
        "Candidates must reside in Kansas or Missouri.",
        "Candidates must reside in New York, New Jersey or Connecticut.",
        "Candidates must reside in Virginia, West Virginia or North Carolina.",
        "Candidates must reside in Washington, DC, Maryland or Virginia.",
        "Candidates must reside in Virginia, DC, or Maryland.",
        "Candidates must reside in Maryland, Virginia, DC.",
        "Candidates must reside in Texas, CA, or Florida.",
        "Candidates must reside in Oregon, WA, Idaho.",
        "Candidates must reside in Georgia, GA, or Florida.",
        "Candidates must reside in Oregon, OR, Washington, WA, or Idaho, ID.",
        "Candidates must reside in Maine, ME or New Hampshire, NH.",
    ],
)
def test_a_list_of_states_in_a_country_the_person_works_remotely_in_leaves_the_row(
    durable_database: Database, tmp_path: Path, rule: str
) -> None:
    screened = _body_screened(durable_database, tmp_path, _canada_home_with_us_remote(), rule)

    assert screened is None


def test_a_list_of_states_reads_a_country_saved_under_another_name() -> None:
    locations = [
        Location(key="toronto", country="Canada", resident=True),
        Location(key="us_remote", country="USA"),
    ]
    parameters = JobSearchParameters(
        locations=locations,
        countries=[CountryNames(country=place.country) for place in locations],
    )

    reading = fit.residence_requirement(f"Operations Lead\n\n{_STATE_LIST}\n", parameters)

    assert reading.value is None
    assert "states" in reading.note


@pytest.mark.parametrize(
    "rule",
    [
        "Candidates must reside within 50 miles of New York or Washington.",
        "Candidates must reside within commuting distance of our New York or Washington offices.",
        "Must reside in Texas (Texas residency required).",
        "Candidates must reside in Kansas City, Missouri.",
        "Candidates must reside in New York City or Washington, DC.",
        "Candidates must reside in New York, NY or Washington, DC.",
        "Candidates must reside in New York, NY or Washington, D.C.",
    ],
)
def test_nearness_or_one_repeated_state_is_not_a_state_list(
    durable_database: Database, tmp_path: Path, rule: str
) -> None:
    screened = _body_screened(durable_database, tmp_path, _canada_home_with_us_remote(), rule)

    assert screened is not None
    assert screened[0] == "location_residence_required"


def test_a_list_of_states_leaves_the_row_while_the_reading_records_it() -> None:
    reading = fit.residence_requirement(
        f"Operations Lead\n\n{_STATE_LIST}\n", _canada_home_with_us_remote(), ""
    )

    assert reading.value is None
    assert "states" in reading.note
    assert any("Arizona" in quote for quote in reading.evidence)


@pytest.mark.parametrize("platform", FORM_PLATFORMS)
def test_a_required_us_state_question_refuses_a_body_listing_states(
    durable_database: Database, tmp_path: Path, platform: FormPlatform
) -> None:
    screened = _body_screened(
        durable_database,
        tmp_path,
        _canada_home_with_us_remote(),
        _STATE_LIST,
        source=form_source(platform, _RESIDENCE_LABEL, US_STATE_OPTIONS),
    )

    assert screened is not None
    assert screened[0] == "location_residence_required"
    assert _RESIDENCE_LABEL in screened[1]


def test_a_body_residence_rule_reads_every_declared_move_as_relevance_review_does(
    durable_database: Database, tmp_path: Path
) -> None:
    """The listing's own place sets attendance, not which other places the person moves to."""
    parameters = _canada_home_with_us_remote(
        us_remote=Arrangement(relocation=True),
        elsewhere=(Location(key="uk", country="United Kingdom"),),
    )
    rule = "Candidates must reside in the United Kingdom."

    screened = _body_screened(
        durable_database, tmp_path, parameters, rule, location="Austin, Texas, United States"
    )

    assert screened is not None
    assert screened[0] == "location_residence_required"
    assert rule in screened[1]


@pytest.mark.parametrize(
    ("location", "refused"),
    [("Austin, Texas, United States", True), ("Kingsmere, Ontario, Canada", False)],
)
def test_a_body_requiring_living_near_the_listing_reads_the_listing_country(
    durable_database: Database, tmp_path: Path, location: str, refused: bool
) -> None:
    """The body screen reads the listing as the fit reader given that listing does."""
    rule = "Candidates must reside locally."
    parameters = _canada_home_with_us_remote()

    screened = _body_screened(durable_database, tmp_path, parameters, rule, location=location)

    if refused:
        assert screened is not None
        assert screened[0] == "location_residence_required"
        assert f"the posting requires residing near {location}, in United States" in screened[1]
        assert rule in screened[1]
    else:
        assert screened is None


@pytest.mark.parametrize(
    ("country", "location"),
    [("Canada", "Toronto, Ontario, Canada"), ("Germany", "Berlin, Germany")],
)
def test_a_body_requiring_a_us_time_zone_leaves_a_person_outside_the_us_in_the_queue(
    durable_database: Database, tmp_path: Path, country: str, location: str
) -> None:
    """A time zone names a clock, not a country the candidate must live in."""
    parameters = JobSearchParameters(
        locations=[Location(key="home", country=country)],
        countries=[CountryNames(country=country)],
    )

    screened = _body_screened(
        durable_database,
        tmp_path,
        parameters,
        "You must be located in US time zones.",
        location=location,
    )

    assert screened is None


_PLACELESS_BODY = (
    b"Operations Lead\nRun the regional carrier desk and own the weekly service review."
)


@pytest.mark.parametrize(
    ("place", "body", "action"),
    [
        ("Lindenfeld, Germ, Ba", _PLACELESS_BODY, "dismiss"),
        ("Lindenfeld, Germ, Ba", b"Operations Lead\nOwn the weekly carrier review.", "dismiss"),
        ("Toronto, Ontario, Canada", _PLACELESS_BODY, "dismiss"),
        ("Toronto, Ontario, Canada", b"Operations Lead\nOwn the weekly carrier review.", "restore"),
        ("", b"Operations Lead\nLocation: Toronto, Ontario, Canada", "restore"),
    ],
)
def test_a_body_screen_restores_a_location_dismissal_only_for_new_words_placed_inside(
    durable_database: Database, tmp_path: Path, place: str, body: bytes, action: str
) -> None:
    """An unread place or an unchanged body is no evidence the stated conflict went away."""
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    SearchParametersService(durable_database).save(workspace_id, _canada_home_with_us_remote())
    with durable_database.session() as session:
        row = session.get_one(PendingDiscovery, pending_id)
        row.location = "Lindenfeld, Germany"
        row.staged_facts = published_location_facts(row.location).as_json()
    _resolve(
        durable_database,
        tmp_path,
        workspace_id,
        pending_id,
        url,
        _PLACELESS_BODY,
        origin="source.scan",
    )
    with durable_database.session() as session:
        row = session.get_one(PendingDiscovery, pending_id)
        row.location = place
        staged = published_location_facts(place) if place else StagedListingFacts()
        row.staged_facts = staged.as_json()

    _resolve(
        durable_database, tmp_path, workspace_id, pending_id, url, body, origin="source.continue"
    )

    assert _latest_disposition(durable_database, workspace_id, pending_id)[::2] == (
        action,
        "location_outside",
    )


def test_a_source_read_restores_a_location_dismissal_once_its_words_differ_from_the_judged_body(
    durable_database: Database, tmp_path: Path
) -> None:
    """A refresh between the dismissal and the source read does not stand in for the judgment."""
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    SearchParametersService(durable_database).save(workspace_id, _canada_home_with_us_remote())
    with durable_database.session() as session:
        row = session.get_one(PendingDiscovery, pending_id)
        row.location = ""
        row.staged_facts = StagedListingFacts().as_json()
    judged = b"Operations Lead\nLocation: Lindenfeld, Germany"
    _resolve(
        durable_database, tmp_path, workspace_id, pending_id, url, judged, origin="source.scan"
    )
    assert _latest_disposition(durable_database, workspace_id, pending_id)[::2] == (
        "dismiss",
        "location_outside",
    )
    placed_inside = b"Operations Lead\nLocation: Toronto, Ontario, Canada"
    for origin in ("discovery.refresh_batch", "source.continue"):
        _resolve(
            durable_database, tmp_path, workspace_id, pending_id, url, placed_inside, origin=origin
        )

    assert _latest_disposition(durable_database, workspace_id, pending_id)[::2] == (
        "restore",
        "location_outside",
    )


def test_a_placeless_row_whose_body_places_the_role_outside_every_country_is_dismissed(
    durable_database: Database, tmp_path: Path
) -> None:
    screened = _body_screened(
        durable_database,
        tmp_path,
        _canada_home_with_us_remote(),
        "This role is based in Warsaw, Poland.",
        location="",
    )

    assert screened is not None
    assert screened[0] == "location_outside"
    assert "Warsaw, Poland" in screened[1]


def test_a_placeless_row_whose_body_places_the_role_at_a_facility_abroad_is_dismissed(
    durable_database: Database, tmp_path: Path
) -> None:
    screened = _body_screened(
        durable_database,
        tmp_path,
        _canada_home_with_us_remote(),
        "This is a permanent role based in our Global Capability Centre in Warsaw, Poland"
        " and hybrid working with the expectation for some office presence.",
        location="",
    )

    assert screened is not None
    assert screened[0] == "location_outside"
    assert "Warsaw, Poland" in screened[1]


@pytest.mark.parametrize(("location", "label"), [("", "Remote"), ("Remote", "")])
def test_a_role_place_sentence_leaves_a_row_whose_listing_or_label_states_a_location(
    durable_database: Database, tmp_path: Path, location: str, label: str
) -> None:
    screened = _body_screened(
        durable_database,
        tmp_path,
        _canada_home_with_us_remote(),
        "This role is based in Warsaw, Poland.",
        location=location,
        label=label,
    )

    assert screened is None


def test_a_role_place_sentence_leaves_a_row_only_its_staged_locations_place(
    durable_database: Database, tmp_path: Path
) -> None:
    screened = _body_screened(
        durable_database,
        tmp_path,
        _canada_home_with_us_remote(),
        "This role is based in Warsaw, Poland.",
        location="",
        staged=published_location_facts("Remote - United States"),
    )

    assert screened is None


def test_a_placeless_row_whose_body_places_the_role_in_a_saved_country_stays(
    durable_database: Database, tmp_path: Path
) -> None:
    screened = _body_screened(
        durable_database,
        tmp_path,
        _canada_home_with_us_remote(),
        "This role is based in Ridgeton, Ontario, Canada.",
        location="",
    )

    assert screened is None


_RESIDENCE_LABEL = "Which state do you currently reside in?"


@pytest.mark.parametrize("platform", FORM_PLATFORMS)
def test_a_required_us_state_question_with_no_other_answer_is_a_residence_rule(
    durable_database: Database, tmp_path: Path, platform: FormPlatform
) -> None:
    """A form that only accepts a US state of residence requires living in the US."""
    screened = _body_screened(
        durable_database,
        tmp_path,
        _canada_home_with_us_remote(),
        "Remote work is welcome.",
        source=form_source(platform, _RESIDENCE_LABEL, US_STATE_OPTIONS),
    )

    assert screened is not None
    assert screened[0] == "location_residence_required"
    assert _RESIDENCE_LABEL in screened[1]


@pytest.mark.parametrize("platform", FORM_PLATFORMS)
@pytest.mark.parametrize(
    ("options", "required"),
    [((*US_STATE_OPTIONS, "I reside outside the United States"), True), (US_STATE_OPTIONS, False)],
)
def test_a_state_question_a_person_outside_the_us_can_answer_leaves_the_row(
    durable_database: Database,
    tmp_path: Path,
    platform: FormPlatform,
    options: tuple[str, ...],
    required: bool,
) -> None:
    screened = _body_screened(
        durable_database,
        tmp_path,
        _canada_home_with_us_remote(),
        "Remote work is welcome.",
        source=form_source(platform, _RESIDENCE_LABEL, options, required=required),
    )

    assert screened is None


_PAID_BODY = (
    b"Operations Lead\nLocation: Remote - United States\n\n"
    b"Compensation: The base salary range is $120,000 - $150,000 per year.\n\n"
    b"Run the regional carrier desk and own the weekly service review.\n"
)


_PLACELESS_PAID_BODY = (
    b"Operations Lead\n\n"
    b"Compensation: The base salary range is $120,000 - $150,000 per year.\n\n"
    b"Run the regional carrier desk and own the weekly service review.\n"
)


def _staged_pay(database: Database, pending_id: str) -> tuple[tuple[str, ...], str]:
    with database.session() as session:
        row = session.get_one(PendingDiscovery, pending_id)
        pay = StagedListingFacts.from_json(row.staged_facts).compensation
        return pay.values, pay.provenance


def _us_row_with_pay(
    database: Database, tmp_path: Path, listing_pay: tuple[str, ...] = ()
) -> tuple[str, str, str]:
    workspace_id, pending_id, url = _pending(database, tmp_path)
    SearchParametersService(database).save(
        workspace_id,
        JobSearchParameters(
            locations=[Location(key="home", country="United States")],
            countries=[CountryNames(country="United States")],
        ),
    )
    staged = published_location_facts("Remote - United States")
    with database.session() as session:
        row = session.get_one(PendingDiscovery, pending_id)
        row.location = "Remote - United States"
        row.staged_facts = replace(
            staged,
            compensation=replace(
                staged.compensation, values=listing_pay, provenance="greenhouse.compensation"
            ),
        ).as_json()
    return workspace_id, pending_id, url


def test_a_body_read_saves_the_pay_its_posting_states_when_the_listing_states_none(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _us_row_with_pay(durable_database, tmp_path)

    _resolve(durable_database, tmp_path, workspace_id, pending_id, url, _PAID_BODY)

    assert _staged_pay(durable_database, pending_id) == (
        ("USD 120000-150000 per year",),
        "publisher_body.compensation",
    )


def test_a_body_read_keeps_the_pay_the_listing_states(
    durable_database: Database, tmp_path: Path
) -> None:
    listed = ("USD 125000-155000 per year",)
    workspace_id, pending_id, url = _us_row_with_pay(durable_database, tmp_path, listed)

    _resolve(durable_database, tmp_path, workspace_id, pending_id, url, _PAID_BODY)

    assert _staged_pay(durable_database, pending_id) == (listed, "greenhouse.compensation")


def test_a_us_listing_reads_a_placeless_dollar_as_usd_beside_only_cad_bands(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    SearchParametersService(durable_database).save(workspace_id, canadian_pay_with_a_us_place())
    with durable_database.session() as session:
        row = session.get_one(PendingDiscovery, pending_id)
        row.location = "Austin, Texas, United States"
        row.staged_facts = published_location_facts(row.location).as_json()

    _resolve(durable_database, tmp_path, workspace_id, pending_id, url, _PLACELESS_PAID_BODY)

    assert _staged_pay(durable_database, pending_id) == (
        ("USD 120000-150000 per year",),
        "publisher_body.compensation",
    )


@pytest.mark.parametrize(
    ("reread", "pay"),
    [
        (
            _PAID_BODY.replace(b"$120,000 - $150,000", b"$130,000 - $160,000"),
            (("USD 130000-160000 per year",), "publisher_body.compensation"),
        ),
        (
            b"Operations Lead\nLocation: Remote - United States\n\n"
            b"Run the regional carrier desk and own the weekly service review.\n",
            ((), ""),
        ),
    ],
    ids=["new pay", "no pay"],
)
def test_a_reread_body_replaces_the_pay_an_earlier_body_saved(
    durable_database: Database, tmp_path: Path, reread: bytes, pay: tuple[tuple[str, ...], str]
) -> None:
    workspace_id, pending_id, url = _us_row_with_pay(durable_database, tmp_path)
    _resolve(durable_database, tmp_path, workspace_id, pending_id, url, _PAID_BODY)

    _resolve(durable_database, tmp_path, workspace_id, pending_id, url, reread)

    assert _staged_pay(durable_database, pending_id) == pay


def test_confirming_a_contradicted_body_saves_the_pay_it_states(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _us_row_with_pay(durable_database, tmp_path)
    contradicted = replace(
        _readable(url, _PAID_BODY),
        identity_status=PostingIdentityStatus.CONTRADICTED,
        identity_evidence={"publisher_employer": "Acme Holdings"},
    )
    service = PostingContentResolutionService(
        durable_database,
        ScriptedResolver([contradicted]),
        storage=runtime_storage(durable_database, tmp_path),
    )
    service.resolve_batch(
        workspace_id,
        [pending_id],
        service.preflight(workspace_id, [pending_id]).snapshot_sha256,
        origin="discovery.refresh_batch",
    )
    with durable_database.session() as session:
        current_content_id = session.get_one(PendingDiscovery, pending_id).current_content_id
    assert current_content_id is not None

    service.confirm_current_identity(
        workspace_id,
        pending_id,
        attested_same_posting=True,
        expected_current_content_id=current_content_id,
        expected_url=url,
        correlation_id="request-pay",
    )

    assert _staged_pay(durable_database, pending_id) == (
        ("USD 120000-150000 per year",),
        "publisher_body.compensation",
    )


def test_the_same_words_laid_out_anew_restore_a_no_requirements_dismissal(
    durable_database: Database, tmp_path: Path
) -> None:
    """New lines can show the requirements the flat body hid, so the dismissal reopens."""
    workspace_id, pending_id, url = _dismissed_for_no_requirements(durable_database, tmp_path)

    _resolve(
        durable_database,
        tmp_path,
        workspace_id,
        pending_id,
        url,
        b"Operations Lead\n\n- Sign in to see this posting.",
    )

    assert _latest_disposition(durable_database, workspace_id, pending_id) == (
        "restore",
        "audit",
        "no_requirements",
    )


def test_current_content_carries_the_stored_robots_basis(
    durable_database: Database, tmp_path: Path
) -> None:
    """A single-posting-read basis on the stored evidence reaches the current projection."""
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    service = PostingContentResolutionService(
        durable_database,
        ScriptedResolver([_readable(url, robots_basis=RobotsReadBasis.SINGLE_POSTING_READ)]),
        storage=runtime_storage(durable_database, tmp_path),
    )
    service.resolve_batch(
        workspace_id,
        [pending_id],
        service.preflight(workspace_id, [pending_id]).snapshot_sha256,
        origin="discovery.refresh_batch",
    )

    current = CurrentPostingContentReader(
        durable_database, storage=runtime_storage(durable_database, tmp_path)
    ).read(workspace_id, pending_id)

    assert current is not None
    assert current.robots_basis == RobotsReadBasis.SINGLE_POSTING_READ


def test_current_content_has_no_robots_basis_when_none_was_stored(
    durable_database: Database, tmp_path: Path
) -> None:
    """An ordinary crawl read leaves the current projection's robots basis unset."""
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    service = PostingContentResolutionService(
        durable_database,
        ScriptedResolver([_readable(url)]),
        storage=runtime_storage(durable_database, tmp_path),
    )
    service.resolve_batch(
        workspace_id,
        [pending_id],
        service.preflight(workspace_id, [pending_id]).snapshot_sha256,
        origin="discovery.refresh_batch",
    )

    current = CurrentPostingContentReader(
        durable_database, storage=runtime_storage(durable_database, tmp_path)
    ).read(workspace_id, pending_id)

    assert current is not None
    assert current.robots_basis is None


def test_confirming_identity_of_the_same_body_keeps_its_relevance_review(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    ambiguous = replace(_readable(url), identity_status=PostingIdentityStatus.AMBIGUOUS)
    service = PostingContentResolutionService(
        durable_database,
        ScriptedResolver([ambiguous]),
        storage=runtime_storage(durable_database, tmp_path),
    )
    service.resolve_batch(
        workspace_id,
        [pending_id],
        service.preflight(workspace_id, [pending_id]).snapshot_sha256,
        origin="discovery.refresh_batch",
    )
    [target] = service.preflight(workspace_id, [pending_id]).targets
    assert target.current_content_id is not None
    record_relevance_review(durable_database, workspace_id, pending_id)

    service.confirm_current_identity(
        workspace_id,
        pending_id,
        attested_same_posting=True,
        expected_current_content_id=target.current_content_id,
        expected_url=url,
        correlation_id="request-confirm",
    )

    assert _relevance_review_count(durable_database, pending_id) == 1


def test_resolution_retains_body_and_projects_current_evidence(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    resolver = ScriptedResolver([_readable(url)])
    service = PostingContentResolutionService(
        durable_database, resolver, storage=runtime_storage(durable_database, tmp_path)
    )

    preflight = service.preflight(workspace_id, [pending_id])
    result = service.resolve_batch(
        workspace_id, [pending_id], preflight.snapshot_sha256, origin="discovery.refresh_batch"
    )

    assert result["outcomes"] == {"readable": 1}
    assert resolver.calls == [(url, "", "")]
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        evidence = session.get(PendingPostingContent, pending.current_content_id)
        assert evidence is not None
        assert evidence.identity_status == PostingIdentityStatus.VERIFIED
        artifact = session.get(Artifact, evidence.artifact_id)
        workspace = session.get(Workspace, workspace_id)
        assert artifact is not None and workspace is not None
        assert service.artifacts.read(artifact, workspace) == (
            b"Operations Lead\nBuild reliable systems."
        )


_SEARCH_RESULT_CARDS = (
    ("Director of Scheduling Platform Engineering", "Northwind Regional Freight"),
    ("Senior Manager, Depot Timetable Systems", "Globex Freight North"),
    ("Principal Engineer, Fleet Planning Services", "Initrode Logistics"),
    ("Head of Transit Data Platform Delivery", "Acme Rail Partners"),
    ("Lead Architect, Journey Planning Platform", "Initech Transit Group"),
    ("Staff Engineer, Depot Operations Tooling", "Northwind Freight Works"),
    ("Director, Timetable Information Systems", "Globex Regional Transit"),
)
# Whole-page extraction runs each card's title, the same title again, and its
# employer together, so every card is one distinct line and no line repeats.
_SEARCH_RESULTS_BODY = "\n".join(
    [
        "Jobs",
        "Companies",
        "1,000+ Director Scheduling Platform jobs in Calderfield",
        *(f"{title}{title}{employer}" for title, employer in _SEARCH_RESULT_CARDS),
        "About",
        "Terms",
    ]
).encode("utf-8")


def test_a_stored_search_results_page_is_not_left_standing_as_a_verified_posting(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    lines = _SEARCH_RESULTS_BODY.decode("utf-8").splitlines()
    assert len(set(lines)) == len(lines)

    _resolve(durable_database, tmp_path, workspace_id, pending_id, url, _SEARCH_RESULTS_BODY)

    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        evidence = session.get(PendingPostingContent, pending.current_content_id)
        assert evidence is not None
        assert evidence.identity_status == PostingIdentityStatus.CONTRADICTED
        assert (
            pending_discovery_readiness(pending, evidence)
            is PendingDiscoveryReadiness.IDENTITY_CONTRADICTED
        )
    reader = CurrentPostingContentReader(
        durable_database, storage=runtime_storage(durable_database, tmp_path)
    )
    assert reader.read(workspace_id, pending_id) is None


_REPEATING_POSTING_BODY = "\n".join(
    [
        "# Director of Scheduling Platform Engineering",
        "Location: Calderfield, Canada",
        "Northwind Regional Freight runs the regional depot network and is hiring a "
        "director to own the scheduling platform end to end.",
        "You will own the scheduling platform roadmap. You will own the scheduling "
        "platform roadmap, and you will own its delivery.",
        "Requirements: ten years of platform engineering, five of them leading teams.",
        "We are an equal opportunity employer, and we welcome applicants from every "
        "background in every market we serve.",
        "Employment type: Full-time. Compensation is reviewed annually.",
    ]
).encode("utf-8")


def test_a_posting_that_repeats_a_long_phrase_is_still_a_verified_posting(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)

    _resolve(durable_database, tmp_path, workspace_id, pending_id, url, _REPEATING_POSTING_BODY)

    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        evidence = session.get(PendingPostingContent, pending.current_content_id)
        assert evidence is not None
        assert evidence.identity_status == PostingIdentityStatus.VERIFIED
        assert pending_discovery_readiness(pending, evidence) is PendingDiscoveryReadiness.READY


def test_a_person_cannot_attest_that_a_search_results_page_is_their_posting(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    service = PostingContentResolutionService(
        durable_database,
        ScriptedResolver([_readable(url, _SEARCH_RESULTS_BODY)]),
        storage=runtime_storage(durable_database, tmp_path),
    )
    service.resolve_batch(
        workspace_id,
        [pending_id],
        service.preflight(workspace_id, [pending_id]).snapshot_sha256,
        origin="discovery.refresh_batch",
    )
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        contradicted_id = pending.current_content_id
    assert contradicted_id is not None

    with pytest.raises(ConflictError, match="lists many postings"):
        service.confirm_current_identity(
            workspace_id,
            pending_id,
            attested_same_posting=True,
            expected_current_content_id=contradicted_id,
            expected_url=url,
            correlation_id="request-2",
        )

    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        assert pending.current_content_id == contradicted_id
        evidence = session.get(PendingPostingContent, contradicted_id)
        assert evidence is not None
        assert evidence.identity_status == PostingIdentityStatus.CONTRADICTED
        assert pending.content_note == LISTING_PAGE_NOTE


_OPERATIONS_NODE = {
    "@type": "JobPosting",
    "title": "Operations Lead",
    "hiringOrganization": {"name": "Acme"},
    "jobLocationType": "TELECOMMUTE",
    "applicantLocationRequirements": {"@type": "Country", "name": "Canada"},
    "description": "<p>" + "Run the regional operations desk. " * 12 + "</p>",
}
_DECLARING_PAGE = (
    '<html><head><script type="application/ld+json">'
    f"{json.dumps(_OPERATIONS_NODE)}</script></head>"
    "<body><main><h1>Operations Lead</h1></main></body></html>"
)
_DUTIES = "<p>" + "Run the regional operations desk and report on service levels. " * 8 + "</p>"
_QUALIFICATIONS = "<ul><li>" + "Five years leading a regional operations team. " * 8 + "</li></ul>"
_ASHBY_JOB = {
    "id": "3c9a7e14-8b2d-4f6a-a1c5-7d0e9b2f4c81",
    "title": "Operations Lead",
    "location": "Springfield",
    "descriptionHtml": _DUTIES,
}
_ORACLE_REQUISITION = {
    "Id": "2020001",
    "Title": "Operations Lead",
    "PrimaryLocation": "Springfield",
    "ExternalDescriptionStr": _DUTIES,
    "ExternalQualificationsStr": _QUALIFICATIONS,
}
_DAYFORCE_ROW = {**dayforce_feed_row("12345", "Operations Lead"), "Description": _DUTIES}
_MAPPINGS = "clearscout.infrastructure.web_fetch.board_mappings"
# Each board's posting URL, the API address it answers on, the answer, the one
# per-posting record in it, and the description markup the body converts.
_BOARD_READS = [
    pytest.param(
        "https://job-boards.greenhouse.io/acme/jobs/999",
        "boards-api.greenhouse.io/v1/boards/acme/jobs/999",
        {"id": 999, "title": "Operations Lead", "content": html.escape(_DUTIES)},
        {"id": 999, "title": "Operations Lead", "content": html.escape(_DUTIES)},
        html.escape(_DUTIES),
        ["content"],
        id="greenhouse",
    ),
    pytest.param(
        "https://jobs.lever.co/acme/5e2d8b90-1f4c-4a7e-b3d6-8c9a0e1f2b47",
        "api.lever.co/v0/postings/acme/5e2d8b90-1f4c-4a7e-b3d6-8c9a0e1f2b47",
        {
            "text": "Operations Lead",
            "description": _DUTIES,
            "lists": [{"text": "Qualifications", "content": _QUALIFICATIONS}],
        },
        {
            "text": "Operations Lead",
            "description": _DUTIES,
            "lists": [{"text": "Qualifications", "content": _QUALIFICATIONS}],
        },
        f"{_DUTIES}\n\nQualifications\n\n{_QUALIFICATIONS}",
        [f"{_MAPPINGS}.lever._sections"],
        id="lever",
    ),
    pytest.param(
        "https://jobs.ashbyhq.com/acme/3c9a7e14-8b2d-4f6a-a1c5-7d0e9b2f4c81",
        "api.ashbyhq.com/posting-api/job-board/acme",
        {"jobs": [_ASHBY_JOB]},
        _ASHBY_JOB,
        _DUTIES,
        [f"{_MAPPINGS}.ashby._description"],
        id="ashby",
    ),
    pytest.param(
        "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/R-123",
        "/wday/cxs/acme/careers/job/R-123",
        {"jobPostingInfo": {"title": "Operations Lead", "jobDescription": _DUTIES}},
        {"jobPostingInfo": {"title": "Operations Lead", "jobDescription": _DUTIES}},
        _DUTIES,
        [f"{_MAPPINGS}.workday._detail_description"],
        id="workday",
    ),
    pytest.param(
        "https://acme.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_4/job/2020001",
        "recruitingCEJobRequisitionDetails",
        {"items": [_ORACLE_REQUISITION], "count": 1},
        _ORACLE_REQUISITION,
        f"{_DUTIES}\n\n{_QUALIFICATIONS}",
        ["ExternalDescriptionStr", "ExternalQualificationsStr"],
        id="oracle",
    ),
    pytest.param(
        "https://jobs.smartrecruiters.com/Acme/743999000111-operations-lead",
        "api.smartrecruiters.com/v1/companies/Acme/postings/743999000111",
        {
            "name": "Operations Lead",
            "jobAd": {
                "sections": {
                    "jobDescription": {"text": _DUTIES},
                    "qualifications": {"text": _QUALIFICATIONS},
                }
            },
        },
        {
            "name": "Operations Lead",
            "jobAd": {
                "sections": {
                    "jobDescription": {"text": _DUTIES},
                    "qualifications": {"text": _QUALIFICATIONS},
                }
            },
        },
        f"{_DUTIES}\n\n{_QUALIFICATIONS}",
        ["jobAd.sections.jobDescription.text", "jobAd.sections.qualifications.text"],
        id="smartrecruiters",
    ),
    pytest.param(
        DAYFORCE_POSTING_URL.format(reference="12345"),
        "/v1/jobfeeds",
        [_DAYFORCE_ROW],
        {**_DAYFORCE_ROW, "variants": [_DAYFORCE_ROW]},
        _DUTIES,
        ["Description"],
        id="dayforce",
    ),
]


class _NeverRendered:
    def fetch(self, url: str, **_kwargs: object) -> object:
        raise AssertionError(f"a static read answered, so {url} is never rendered")


def _retained_files(
    database: Database, service: PostingContentResolutionService, workspace_id: str, pending_id: str
) -> dict[str, tuple[Path, bytes, dict[str, Any]]]:
    """Return each artifact the current evidence retained, by type: its file, bytes, provenance."""
    with database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        workspace = session.get(Workspace, workspace_id)
        assert pending is not None and workspace is not None
        rows = session.scalars(
            select(Artifact).where(
                Artifact.related_entity_type == "pending_posting_content",
                Artifact.related_entity_id == pending.current_content_id,
            )
        ).all()
        files: dict[str, tuple[Path, bytes, dict[str, Any]]] = {}
        for row in rows:
            path = file_uri_path(row.storage_uri)
            assert path is not None
            files[row.logical_type] = (path, service.artifacts.read(row, workspace), row.provenance)
        return files


def _resolve_through(
    database: Database, tmp_path: Path, url: str, resolver: Any
) -> tuple[PostingContentResolutionService, str, str]:
    workspace = WorkspaceService(database, runtime_storage(database, tmp_path)).create(
        slug="raw-retention", name="Raw retention"
    )
    pending_id = _add_pending(
        database,
        workspace.id,
        employer="Acme",
        title="Operations Lead",
        first_seen_on=date(2026, 8, 28),
        url=url,
    )
    service = PostingContentResolutionService(
        database, resolver, storage=runtime_storage(database, tmp_path)
    )
    service.resolve_batch(
        workspace.id,
        [pending_id],
        service.preflight(workspace.id, [pending_id]).snapshot_sha256,
        origin="discovery.refresh_batch",
    )
    return service, workspace.id, pending_id


@pytest.mark.parametrize(("url", "api", "answer", "record", "markup", "paths"), _BOARD_READS)
def test_a_board_read_keeps_its_record_and_description_markup_beside_the_body(
    durable_database: Database,
    tmp_path: Path,
    request: pytest.FixtureRequest,
    url: str,
    api: str,
    answer: Any,
    record: dict[str, Any],
    markup: str,
    paths: list[str],
) -> None:
    """A converter fix re-derives the body offline only from what the read converted."""

    def serve(requested: str) -> httpx.Response:
        request = httpx.Request("GET", requested)
        if api in requested:
            return httpx.Response(200, request=request, json=answer)
        return httpx.Response(404, request=request)

    resolver = DeterministicPostingContentResolver(
        client=policy_client(serve), rendered=cast(Any, _NeverRendered())
    )
    service, workspace_id, pending_id = _resolve_through(durable_database, tmp_path, url, resolver)

    files = _retained_files(durable_database, service, workspace_id, pending_id)
    assert set(files) == {"posting.body.text", "posting.source.json", "posting.body.html"}
    assert {path.name for path, _content, _provenance in files.values()} == {
        "body.txt",
        "raw.json",
        "description.html",
    }
    assert len({path.parent for path, _content, _provenance in files.values()}) == 1
    assert json.loads(files["posting.source.json"][1]) == record
    assert files["posting.body.html"][1].decode("utf-8") == markup
    assert files["posting.source.json"][2]["description_paths"] == paths
    assert files["posting.source.json"][2]["publisher"] == cast(Any, request.node).callspec.id


def test_a_declaring_page_keeps_its_node_its_description_and_the_page_beside_the_body(
    durable_database: Database, tmp_path: Path
) -> None:
    """Interpretation flattens the node, so only the retained node answers a typing question."""
    url = "https://example.test/jobs/operations-lead"
    resolver = DeterministicPostingContentResolver(
        client=policy_client(
            lambda requested: httpx.Response(
                200, request=httpx.Request("GET", requested), text=_DECLARING_PAGE
            )
        ),
        rendered=cast(Any, _NeverRendered()),
    )
    service, workspace_id, pending_id = _resolve_through(durable_database, tmp_path, url, resolver)

    files = _retained_files(durable_database, service, workspace_id, pending_id)
    assert {logical_type: path.name for logical_type, (path, _c, _p) in files.items()} == {
        "posting.body.text": "body.txt",
        "posting.source.json": "raw.json",
        "posting.body.html": "description.html",
        "posting.source.html": "page.html",
    }
    assert len({path.parent for path, _content, _provenance in files.values()}) == 1
    assert json.loads(files["posting.source.json"][1]) == _OPERATIONS_NODE
    assert files["posting.body.html"][1].decode("utf-8") == _OPERATIONS_NODE["description"]
    assert files["posting.source.html"][1].decode("utf-8") == _DECLARING_PAGE
    assert files["posting.body.html"][2]["description_paths"] == ["description"]


class _NoBrowser:
    def fetch(self, _url: str, **_kwargs: object) -> object:
        raise RenderRuntimeUnavailableError("no browser in this test")


_LISTED_JOB = "4012345"
_LISTED_POSTING = f"https://www.initech.example/careers/job/{_LISTED_JOB}?gh_jid={_LISTED_JOB}"


_LISTED_SINGLE_JOB = f"boards-api.greenhouse.io/v1/boards/initech-health/jobs/{_LISTED_JOB}"
_LISTED_BOARD_LISTING = "boards-api.greenhouse.io/v1/boards/initech-health/jobs?"


def _resolve_listed_posting(
    database: Database,
    tmp_path: Path,
    source_url: str | None,
    *,
    resolved_url: str | None = None,
    open_on_board: bool = True,
) -> tuple[PendingDiscovery, PendingPostingContent, list[str]]:
    """Read one hosted Greenhouse posting whose page hides its token; return its row and read."""
    requested: list[str] = []

    def serve(target: str) -> httpx.Response:
        requested.append(target)
        request = httpx.Request("GET", target)
        if _LISTED_SINGLE_JOB in target and open_on_board:
            job = {"id": int(_LISTED_JOB), "title": "Staff Data Engineer", "content": _DUTIES}
            return httpx.Response(200, request=request, json=job)
        if _LISTED_BOARD_LISTING in target:
            others = {"jobs": [{"id": 4099999, "title": "Registered Nurse"}]}
            return httpx.Response(200, request=request, json=others)
        if "boards-api.greenhouse.io" in target:
            return httpx.Response(404, request=request, json={"error": "Job not found"})
        return httpx.Response(200, request=request, text="<html><body></body></html>")

    workspace = WorkspaceService(database, runtime_storage(database, tmp_path))
    workspace_id = workspace.create(slug="listed-board", name="Listed board").id
    source_id = (
        board_source(
            database, workspace_id, "Initech Health", source_url, resolved_url=resolved_url
        )
        if source_url
        else None
    )
    pending_id = _add_pending(
        database,
        workspace_id,
        employer="Initech Health",
        title="Staff Data Engineer",
        first_seen_on=date(2026, 8, 28),
        url=_LISTED_POSTING,
        source_id=source_id,
    )
    resolver = DeterministicPostingContentResolver(
        client=policy_client(serve), rendered=cast(Any, _NoBrowser())
    )
    service = PostingContentResolutionService(
        database, resolver, storage=runtime_storage(database, tmp_path)
    )
    service.resolve_batch(
        workspace_id,
        [pending_id],
        service.preflight(workspace_id, [pending_id]).snapshot_sha256,
        origin="discovery.refresh_batch",
    )
    with database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None and pending.current_content_id is not None
        evidence = session.get(PendingPostingContent, pending.current_content_id)
        assert evidence is not None
        session.expunge_all()
    return pending, evidence, requested


@pytest.mark.parametrize(
    ("source_url", "verified"),
    [
        ("https://boards.greenhouse.io/initech-health", True),
        (None, False),
        ("https://jobs.lever.co/initech-health", False),
    ],
    ids=["greenhouse board", "no source", "lever board"],
)
def test_a_hosted_greenhouse_posting_is_verified_through_the_board_its_row_was_listed_on(
    durable_database: Database, tmp_path: Path, source_url: str | None, verified: bool
) -> None:
    """A careers page that hides its board token is read through the row's own source."""
    _pending_row, evidence, requested = _resolve_listed_posting(
        durable_database, tmp_path, source_url
    )
    read = (evidence.content_status, evidence.identity_status)
    assert (read == (PostingContentAccessStatus.READABLE, PostingIdentityStatus.VERIFIED)) is (
        verified
    )
    assert any("/boards/initech-health/" in target for target in requested) is verified


def test_a_listed_board_scanned_at_its_resolved_address_names_the_token(
    durable_database: Database, tmp_path: Path
) -> None:
    _pending_row, evidence, _requested = _resolve_listed_posting(
        durable_database,
        tmp_path,
        "https://www.initech.example/careers",
        resolved_url="https://boards.greenhouse.io/initech-health",
    )
    assert (evidence.content_status, evidence.identity_status) == (
        PostingContentAccessStatus.READABLE,
        PostingIdentityStatus.VERIFIED,
    )


def test_a_hosted_posting_its_listing_board_no_longer_holds_withdraws_the_row(
    durable_database: Database, tmp_path: Path
) -> None:
    """The board that listed the row omitting it is a delisting, unlike a guessed board."""
    pending, evidence, _requested = _resolve_listed_posting(
        durable_database,
        tmp_path,
        "https://boards.greenhouse.io/initech-health",
        open_on_board=False,
    )
    assert evidence.posting_status == PostingStatus.CLOSED
    assert pending.withdrawn_at is not None


def test_date_metadata_changes_and_removal_survive_body_reuse_but_failed_reads_do_not_erase_it(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    body = b"Operations Lead\nBuild reliable systems."
    reads = []
    for day, value in ((8, "2026-10-19"), (9, "2026-10-21"), (10, "")):
        at = datetime(2026, 10, day, 12, tzinfo=UTC)
        source = PostingSource(
            publisher="page",
            record=json.dumps({"validThrough": value, "description": body.decode()}),
        )
        reads.append(
            replace(
                _readable(url, body),
                fetched_at=at,
                source=source,
                publisher_closing_date=publisher_closing_date(
                    body.decode(), valid_through=value, source_url=url, observed_at=at
                ),
            )
        )
    service = PostingContentResolutionService(
        durable_database,
        ScriptedResolver(list(reads)),
        storage=runtime_storage(durable_database, tmp_path),
    )
    reader = CurrentPostingContentReader(
        durable_database, storage=runtime_storage(durable_database, tmp_path)
    )
    content_ids = []
    for index, expected in enumerate(("2026-10-19", "2026-10-21", None)):
        service.resolve_batch(
            workspace_id,
            [pending_id],
            service.preflight(workspace_id, [pending_id]).snapshot_sha256,
            origin="discovery.refresh_batch",
        )
        current = reader.read(workspace_id, pending_id)
        assert current is not None and current.publisher_closing_date is not None
        fact = current.publisher_closing_date
        assert fact.normalized_value == expected
        assert fact.observed_at == reads[index].fetched_at
        content_ids.append(current.evidence_id)
        if expected is not None:
            with durable_database.session() as session:
                source_artifact = session.get(Artifact, fact.statements[0].artifact_id)
                workspace = session.get(Workspace, workspace_id)
                assert source_artifact is not None and workspace is not None
                assert source_artifact.logical_type == "posting.source.json"
                assert source_artifact.related_entity_id == current.evidence_id
                assert (
                    json.loads(service.artifacts.read(source_artifact, workspace))["validThrough"]
                    == expected
                )
        if index == 1:
            _fail(durable_database, tmp_path, workspace_id, pending_id, url)
            after_failure = reader.read(workspace_id, pending_id)
            assert after_failure is not None
            assert after_failure.evidence_id == current.evidence_id
            assert after_failure.publisher_closing_date == fact
    with durable_database.session() as session:
        records = [session.get(PendingPostingContent, item) for item in content_ids]
        assert len({item.artifact_id for item in records if item is not None}) == 1
        assert records[0] is not None and records[0].publisher_closing_date is not None
        assert records[0].publisher_closing_date["normalized_value"] == "2026-10-19"


def test_explicit_closure_preserves_the_last_publisher_date(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    at = datetime(2026, 10, 8, 12, tzinfo=UTC)
    fact = publisher_closing_date("Applications close: 2026-10-19", source_url=url, observed_at=at)
    closed = ResolvedPostingContent(
        requested_url=url,
        final_url=url,
        content_status=PostingContentAccessStatus.NOT_YET_READ,
        identity_status=PostingIdentityStatus.NOT_YET_VERIFIED,
        posting_status=PostingStatus.CLOSED,
        extraction_method=None,
        fetched_at=datetime(2026, 10, 9, 12, tzinfo=UTC),
        http_status=404,
        note="The publisher reports that this posting is closed.",
    )
    service = PostingContentResolutionService(
        durable_database,
        ScriptedResolver([replace(_readable(url), publisher_closing_date=fact), closed]),
        storage=runtime_storage(durable_database, tmp_path),
    )
    for _ in range(2):
        service.resolve_batch(
            workspace_id,
            [pending_id],
            service.preflight(workspace_id, [pending_id]).snapshot_sha256,
            origin="discovery.refresh_batch",
        )
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None and pending.withdrawn_at == closed.fetched_at
        evidence = session.get(PendingPostingContent, pending.current_content_id)
        assert evidence is not None and evidence.publisher_closing_date is not None
        assert evidence.publisher_closing_date["normalized_value"] == "2026-10-19"
        assert evidence.publisher_closing_date["observed_at"] == fact.as_json()["observed_at"]


def test_a_reread_of_the_same_words_stands_on_the_body_and_source_already_kept(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    source = PostingSource(publisher="page", record=json.dumps(_OPERATIONS_NODE))
    lined = b"Operations Lead\n\n- Ship."
    reads = [
        replace(_readable(url, lined), source=source),
        replace(_readable(url, b"Operations Lead Ship."), source=source),
    ]
    service = PostingContentResolutionService(
        durable_database,
        ScriptedResolver(reads),
        storage=runtime_storage(durable_database, tmp_path),
    )
    for _ in range(len(reads)):
        service.resolve_batch(
            workspace_id,
            [pending_id],
            service.preflight(workspace_id, [pending_id]).snapshot_sha256,
            origin="discovery.refresh_batch",
        )

    with durable_database.session() as session:
        bodies = session.scalars(
            select(PendingPostingContent.artifact_id).where(
                PendingPostingContent.pending_discovery_id == pending_id
            )
        ).all()
        kinds = session.scalars(
            select(Artifact.logical_type).where(Artifact.workspace_id == workspace_id)
        ).all()
    assert len(bodies) == 2 and len(set(bodies)) == 1
    assert sorted(kinds) == ["posting.body.text", "posting.source.json"]


def test_a_read_that_stores_no_body_keeps_none_of_its_source(
    durable_database: Database, tmp_path: Path
) -> None:
    url = "https://example.test/jobs/operations-lead"
    closed = ResolvedPostingContent(
        requested_url=url,
        final_url=url,
        content_status=PostingContentAccessStatus.NOT_YET_READ,
        identity_status=PostingIdentityStatus.NOT_YET_VERIFIED,
        posting_status=PostingStatus.CLOSED,
        extraction_method=None,
        fetched_at=datetime(2026, 8, 28, tzinfo=UTC),
        note="The publisher reports that this posting is closed.",
        source=PostingSource(
            publisher="page",
            record=json.dumps(_OPERATIONS_NODE),
            description_markup=str(_OPERATIONS_NODE["description"]),
            description_paths=("description",),
            page_html=_DECLARING_PAGE,
        ),
    )
    service, workspace_id, pending_id = _resolve_through(
        durable_database, tmp_path, url, ScriptedResolver([closed])
    )

    assert _retained_files(durable_database, service, workspace_id, pending_id) == {}


def test_resolve_batch_reads_as_a_crawl_and_stores_its_robots_basis(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    resolver = ScriptedResolver([_readable(url, robots_basis=RobotsReadBasis.SINGLE_POSTING_READ)])
    service = PostingContentResolutionService(
        durable_database, resolver, storage=runtime_storage(durable_database, tmp_path)
    )

    preflight = service.preflight(workspace_id, [pending_id])
    service.resolve_batch(
        workspace_id, [pending_id], preflight.snapshot_sha256, origin="discovery.refresh_batch"
    )

    assert resolver.intents == [PostingReadIntent.CRAWL]
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        evidence = session.get(PendingPostingContent, pending.current_content_id)
        assert evidence is not None
        assert evidence.robots_basis == RobotsReadBasis.SINGLE_POSTING_READ


def test_resolve_batch_reads_every_row_on_one_host_as_a_crawl(
    durable_database: Database, tmp_path: Path
) -> None:
    """Rows nobody just added read under robots, however many share a host."""
    workspace_id, first_id, first_url = _pending(durable_database, tmp_path)
    pending_ids = [first_id]
    urls = [first_url]
    with durable_database.session() as session:
        for index in range(5):
            row = PendingDiscovery(
                workspace_id=workspace_id,
                fingerprint=f"{index}" * 64,
                employer="Acme",
                title="Operations Lead",
                discovery_url=f"https://example.test/jobs/role-{index}",
                reason="Publisher-listed role",
                first_seen_on=date(2026, 8, 28),
            )
            session.add(row)
            session.flush()
            pending_ids.append(row.id)
            urls.append(row.discovery_url)
    resolver = ScriptedResolver(
        [_readable(url, robots_basis=RobotsReadBasis.SINGLE_POSTING_READ) for url in urls]
    )
    service = PostingContentResolutionService(
        durable_database, resolver, storage=runtime_storage(durable_database, tmp_path)
    )
    preflight = service.preflight(workspace_id, pending_ids)

    service.resolve_batch(
        workspace_id, pending_ids, preflight.snapshot_sha256, origin="discovery.refresh_batch"
    )

    assert resolver.intents == [PostingReadIntent.CRAWL] * len(pending_ids)


def _staging(database: Database, artifact_root: Path) -> DiscoveryStaging:
    return DiscoveryStaging(
        database,
        PostingRefreshService(
            database,
            storage=runtime_storage(database, artifact_root),
            fetcher=UnexpectedPostingFetcher(),
        ),
    )


def _person_added(database: Database, artifact_root: Path) -> tuple[str, str, str]:
    """Stage one link the way the add-a-posting route does."""
    workspace = WorkspaceService(database, runtime_storage(database, artifact_root)).create(
        slug="person-added", name="Person added"
    )
    url = "https://example.test/jobs/operations-lead"
    staged = _staging(database, artifact_root).stage_found_posting(
        workspace.id, url, date(2026, 8, 28), title="Operations Lead", origin="api"
    )
    assert staged is not None
    return workspace.id, staged.pending_discovery_id, url


def _read_once(
    database: Database,
    artifact_root: Path,
    workspace_id: str,
    pending_id: str,
    resolver: ScriptedResolver,
) -> None:
    service = PostingContentResolutionService(
        database, resolver, storage=runtime_storage(database, artifact_root)
    )
    service.resolve_batch(
        workspace_id,
        [pending_id],
        service.preflight(workspace_id, [pending_id]).snapshot_sha256,
        origin="discovery.refresh_batch",
    )


def test_the_first_read_after_a_persons_add_is_a_single_posting_read(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _person_added(durable_database, tmp_path)
    resolver = ScriptedResolver([_readable(url)])

    _read_once(durable_database, tmp_path, workspace_id, pending_id, resolver)

    assert resolver.intents == [PostingReadIntent.SINGLE_POSTING]


def test_a_person_added_row_a_board_relists_before_its_first_read_is_a_single_posting_read(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _person_added(durable_database, tmp_path)
    source_id = board_source(durable_database, workspace_id, "Acme", "https://example.test/careers")
    _staging(durable_database, tmp_path).stage_listings(
        workspace_id,
        [SourceListing("Acme", "Operations Lead", url, location="Remote")],
        date(2026, 8, 29),
        reason="Board scan matched the title filter.",
        source_id=source_id,
        refresh_only=True,
    )
    with durable_database.session() as session:
        relisted = session.get(PendingDiscovery, pending_id)
        assert relisted is not None
        assert relisted.source_id == source_id
    resolver = ScriptedResolver([_readable(url)])

    _read_once(durable_database, tmp_path, workspace_id, pending_id, resolver)

    assert resolver.intents == [PostingReadIntent.SINGLE_POSTING]


def test_a_second_refresh_of_a_person_added_row_is_a_crawl(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _person_added(durable_database, tmp_path)
    resolver = ScriptedResolver([_readable(url), _readable(url, b"Operations Lead\nStill open.")])

    _read_once(durable_database, tmp_path, workspace_id, pending_id, resolver)
    _read_once(durable_database, tmp_path, workspace_id, pending_id, resolver)

    assert resolver.intents == [PostingReadIntent.SINGLE_POSTING, PostingReadIntent.CRAWL]


def test_a_scan_found_row_reads_as_a_crawl(durable_database: Database, tmp_path: Path) -> None:
    """Guard: only a row the person's add staged takes the single-posting read."""
    workspace_id, _pending_id, _url = _pending(durable_database, tmp_path)
    url = "https://example.test/jobs/platform-lead"
    source_id = board_source(durable_database, workspace_id, "Acme", "https://example.test/careers")
    pending_id = _add_pending(
        durable_database,
        workspace_id,
        employer="Acme",
        title="Platform Lead",
        first_seen_on=date(2026, 8, 28),
        url=url,
        source_id=source_id,
    )
    resolver = ScriptedResolver([_readable(url)])

    _read_once(durable_database, tmp_path, workspace_id, pending_id, resolver)

    assert resolver.intents == [PostingReadIntent.CRAWL]


def test_user_supplied_text_is_attested_sanitized_and_immediately_usable(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    service = PostingContentResolutionService(
        durable_database, ScriptedResolver([]), storage=runtime_storage(durable_database, tmp_path)
    )
    body = "Operations Lead\n" + ("Build reliable systems across teams and regions. " * 10)

    evidence_id = service.store_user_supplied_text(
        workspace_id,
        pending_id,
        body,
        attested_current_exact_posting=True,
        expected_current_content_id=None,
        expected_url=url,
        correlation_id="request-1",
    )

    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        evidence = session.get(PendingPostingContent, evidence_id)
        assert pending is not None and evidence is not None
        assert pending.current_content_id == evidence_id
        assert evidence.created_by == "local_user"
        assert evidence.correlation_id == "request-1"
        assert evidence.extraction_method == PostingContentExtractionMethod.USER_SUPPLIED_TEXT
        assert pending_discovery_readiness(pending, evidence) is PendingDiscoveryReadiness.READY
    assert service.current.read(workspace_id, pending_id) is not None


@pytest.mark.parametrize("body", ["x" * 399, "x" * 120_001])
def test_user_supplied_text_obeys_the_normal_posting_body_bounds(
    durable_database: Database, tmp_path: Path, body: str
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    service = PostingContentResolutionService(
        durable_database, ScriptedResolver([]), storage=runtime_storage(durable_database, tmp_path)
    )

    with pytest.raises(ValidationError, match="posting text"):
        service.store_user_supplied_text(
            workspace_id,
            pending_id,
            body,
            attested_current_exact_posting=True,
            expected_current_content_id=None,
            expected_url=url,
            correlation_id="request-bounds",
        )


def test_user_supplied_text_refuses_an_identity_that_changed_after_review(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, _url = _pending(durable_database, tmp_path)
    service = PostingContentResolutionService(
        durable_database, ScriptedResolver([]), storage=runtime_storage(durable_database, tmp_path)
    )

    with pytest.raises(ConflictError, match="changed after"):
        service.store_user_supplied_text(
            workspace_id,
            pending_id,
            "Current responsibilities and qualifications. " * 12,
            attested_current_exact_posting=True,
            expected_current_content_id=None,
            expected_url="https://example.test/jobs/a-different-role",
            correlation_id="request-drift",
        )


def test_user_can_confirm_retained_body_after_publisher_identity_conflict(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    contradicted = replace(
        _readable(url),
        identity_status=PostingIdentityStatus.CONTRADICTED,
        identity_evidence={
            "publisher_employer": "Acme Holdings",
            "publisher_title": "Operations Lead",
        },
    )
    service = PostingContentResolutionService(
        durable_database,
        ScriptedResolver([contradicted]),
        storage=runtime_storage(durable_database, tmp_path),
    )
    preview = service.preflight(workspace_id, [pending_id])
    service.resolve_batch(
        workspace_id, [pending_id], preview.snapshot_sha256, origin="discovery.refresh_batch"
    )
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        current_content_id = pending.current_content_id
    assert current_content_id is not None

    evidence_id = service.confirm_current_identity(
        workspace_id,
        pending_id,
        attested_same_posting=True,
        expected_current_content_id=current_content_id,
        expected_url=url,
        correlation_id="request-2",
    )

    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        evidence = session.get(PendingPostingContent, evidence_id)
        assert pending is not None and evidence is not None
        assert evidence.identity_status == PostingIdentityStatus.VERIFIED
        assert evidence.identity_evidence["prior_identity_status"] == "contradicted"
        assert evidence.identity_evidence["user_attested_same_posting"] is True
        assert pending_discovery_readiness(pending, evidence) is PendingDiscoveryReadiness.READY
    assert service.current.read(workspace_id, pending_id) is not None


@pytest.mark.parametrize("outcome", ["not_found", "ambiguous"])
def test_bodiless_assisted_attempt_keeps_the_earlier_session_required_status(
    durable_database: Database, tmp_path: Path, outcome: str
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    session_required = ResolvedPostingContent(
        requested_url=url,
        final_url=url,
        content_status=PostingContentAccessStatus.SESSION_REQUIRED,
        identity_status=PostingIdentityStatus.NOT_YET_VERIFIED,
        posting_status=None,
        extraction_method=None,
        fetched_at=datetime(2026, 8, 28, tzinfo=UTC),
        http_status=401,
        note="The publisher requires a signed-in session.",
    )
    service = PostingContentResolutionService(
        durable_database,
        ScriptedResolver([session_required]),
        storage=runtime_storage(durable_database, tmp_path),
    )
    service.resolve_batch(
        workspace_id,
        [pending_id],
        service.preflight(workspace_id, [pending_id]).snapshot_sha256,
        origin="discovery.refresh_batch",
    )
    target = service.preflight(workspace_id, [pending_id]).targets[0]

    evidence_id = service.store_assisted_failure(
        workspace_id,
        target,
        outcome=outcome,
        detail="Hosted search returned no cited exact publisher candidate.",
        candidate_url=None,
        context=None,
    )

    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        evidence = session.get(PendingPostingContent, evidence_id)
        assert pending is not None and evidence is not None
        assert pending.current_content_id == evidence_id
        assert pending.content_status == PostingContentAccessStatus.SESSION_REQUIRED
        assert evidence.content_status == PostingContentAccessStatus.SESSION_REQUIRED
        assert pending_discovery_readiness(pending, evidence) is (
            PendingDiscoveryReadiness.SESSION_REQUIRED
        )


@pytest.mark.parametrize("outcome", ["not_found", "ambiguous"])
def test_failed_assisted_attempt_leaves_a_readable_contradicted_body_current(
    durable_database: Database, tmp_path: Path, outcome: str
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    contradicted = replace(
        _readable(url),
        identity_status=PostingIdentityStatus.CONTRADICTED,
        identity_evidence={"publisher_employer": "Globex", "publisher_title": "Operations Lead"},
    )
    service = PostingContentResolutionService(
        durable_database,
        ScriptedResolver([contradicted]),
        storage=runtime_storage(durable_database, tmp_path),
    )
    service.resolve_batch(
        workspace_id,
        [pending_id],
        service.preflight(workspace_id, [pending_id]).snapshot_sha256,
        origin="discovery.refresh_batch",
    )
    target = service.preflight(workspace_id, [pending_id]).targets[0]
    detail = "Hosted search returned no cited exact publisher candidate."

    attempt_evidence_id = service.store_assisted_failure(
        workspace_id,
        target,
        outcome=outcome,
        detail=detail,
        candidate_url=None,
        context=None,
    )

    evidence_id = target.current_content_id
    assert evidence_id is not None
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        evidence = session.get(PendingPostingContent, evidence_id)
        attempt = session.get(PendingPostingContent, attempt_evidence_id)
        assert pending is not None and evidence is not None and attempt is not None
        assert pending.current_content_id == evidence_id
        assert pending.content_status == PostingContentAccessStatus.READABLE
        assert pending.content_note == f"{detail} The retained posting body remains current."
        assert pending_discovery_readiness(pending, evidence) is (
            PendingDiscoveryReadiness.IDENTITY_CONTRADICTED
        )
        assert attempt.supersedes_content_id is None
        assert attempt.artifact_id is None
        assert attempt.identity_evidence == {"method": "assisted_recovery", "outcome": outcome}
    service.confirm_current_identity(
        workspace_id,
        pending_id,
        attested_same_posting=True,
        expected_current_content_id=evidence_id,
        expected_url=url,
        correlation_id="request-after-attempt",
    )
    assert service.current.read(workspace_id, pending_id) is not None


def test_unsuccessful_assisted_recovery_is_durable_on_the_pending_row(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, _url = _pending(durable_database, tmp_path)
    service = PostingContentResolutionService(
        durable_database, ScriptedResolver([]), storage=runtime_storage(durable_database, tmp_path)
    )
    target = service.preflight(workspace_id, [pending_id]).targets[0]

    evidence_id = service.store_assisted_failure(
        workspace_id,
        target,
        outcome="not_found",
        detail="Hosted search returned no cited exact publisher candidate.",
        candidate_url=None,
        context=None,
    )

    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        evidence = session.get(PendingPostingContent, evidence_id)
        assert pending is not None and evidence is not None
        assert pending.current_content_id == evidence_id
        assert pending.content_status == PostingContentAccessStatus.FETCH_FAILED
        assert pending.content_note == "Hosted search returned no cited exact publisher candidate."
        assert evidence.identity_evidence == {
            "method": "assisted_recovery",
            "outcome": "not_found",
        }


def test_closed_resolution_withdraws_the_active_queue_but_retains_its_evidence(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    closed_at = datetime(2026, 9, 9, tzinfo=UTC)
    closed = ResolvedPostingContent(
        requested_url=url,
        final_url=url,
        content_status=PostingContentAccessStatus.NOT_YET_READ,
        identity_status=PostingIdentityStatus.NOT_YET_VERIFIED,
        posting_status=PostingStatus.CLOSED,
        extraction_method=None,
        fetched_at=closed_at,
        http_status=404,
        identity_evidence={"closure": True},
        note="The publisher reports that this posting is closed.",
    )
    service = PostingContentResolutionService(
        durable_database,
        ScriptedResolver([closed]),
        storage=runtime_storage(durable_database, tmp_path),
    )

    preflight = service.preflight(workspace_id, [pending_id])
    result = service.resolve_batch(
        workspace_id, [pending_id], preflight.snapshot_sha256, origin="discovery.refresh_batch"
    )

    assert result["outcomes"] == {"not_yet_read": 1}
    assert result["withdrawn_ids"] == [pending_id]
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        assert pending.withdrawn_at == closed_at
        evidence = session.get(PendingPostingContent, pending.current_content_id)
        assert evidence is not None
        assert evidence.posting_status is not None
        assert PostingStatus(evidence.posting_status) is PostingStatus.CLOSED
        audit = session.scalars(
            select(AuditEvent).where(
                AuditEvent.entity_id == pending_id,
                AuditEvent.action == "pending_discovery.withdrawn_on_content_read",
            )
        ).one()
        assert audit.details["evidence_id"] == evidence.id
        assert audit.details["note"] == closed.note
        assert withdrawal_causes(session, workspace_id, [pending]) == {
            pending_id: PendingWithdrawalCause.CONTENT_READ_CLOSED
        }
    with pytest.raises(NotFoundError, match="unavailable"):
        service.preflight(workspace_id, [pending_id])


@pytest.mark.parametrize("head_dismissed", [False, True])
def test_a_read_that_withdraws_a_head_releases_its_live_copy_unless_the_head_was_dismissed(
    durable_database: Database, tmp_path: Path, head_dismissed: bool
) -> None:
    workspace_id, head_id, url = _pending(durable_database, tmp_path)
    copy_id = _add_pending(
        durable_database,
        workspace_id,
        employer="Acme",
        title="Operations Lead",
        first_seen_on=date(2026, 8, 29),
        url="https://example.test/jobs/operations-lead/framed",
    )
    with durable_database.session() as session:
        copy = session.get(PendingDiscovery, copy_id)
        assert copy is not None
        copy.regional_variant_of = head_id
    service = PostingContentResolutionService(
        durable_database,
        ScriptedResolver(
            [
                ResolvedPostingContent(
                    requested_url=url,
                    final_url=url,
                    content_status=PostingContentAccessStatus.NOT_YET_READ,
                    identity_status=PostingIdentityStatus.NOT_YET_VERIFIED,
                    posting_status=PostingStatus.CLOSED,
                    extraction_method=None,
                    fetched_at=datetime(2026, 9, 9, tzinfo=UTC),
                    http_status=404,
                    identity_evidence={"closure": True},
                    note="The publisher reports that this posting is closed.",
                )
            ]
        ),
        storage=runtime_storage(durable_database, tmp_path),
    )
    preflight = service.preflight(workspace_id, [head_id])
    if head_dismissed:
        dismiss_pending_discovery(durable_database, workspace_id, head_id)

    service.resolve_batch(
        workspace_id, [head_id], preflight.snapshot_sha256, origin="discovery.refresh_batch"
    )

    with durable_database.session() as session:
        copy = session.get(PendingDiscovery, copy_id)
        assert copy is not None
        assert copy.regional_variant_of == (head_id if head_dismissed else None)
        released = session.scalars(
            select(AuditEvent).where(
                AuditEvent.entity_id == copy_id,
                AuditEvent.action == "pending_discovery.regional_variant_released",
            )
        ).all()
        assert [event.details["released_from_head"] for event in released] == (
            [] if head_dismissed else [head_id]
        )
    page = SourceScanQueries(durable_database).pending_discovery_page(workspace_id)
    visible_ids = {item.id for item in page.ready.items}
    visible_ids |= {item.id for item in page.attention.items}
    assert (copy_id in visible_ids) is not head_dismissed


def test_queue_reset_leaves_a_withdrawal_the_stored_closed_evidence_backs(
    durable_database: Database, tmp_path: Path
) -> None:
    """A reset must not clear a withdrawal that the row's own stored evidence set."""
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    closed_at = datetime(2026, 9, 9, tzinfo=UTC)
    closed = ResolvedPostingContent(
        requested_url=url,
        final_url=url,
        content_status=PostingContentAccessStatus.NOT_YET_READ,
        identity_status=PostingIdentityStatus.NOT_YET_VERIFIED,
        posting_status=PostingStatus.CLOSED,
        extraction_method=None,
        fetched_at=closed_at,
        http_status=404,
        identity_evidence={"closure": True},
        note="The publisher reports that this posting is closed.",
    )
    service = PostingContentResolutionService(
        durable_database,
        ScriptedResolver([closed]),
        storage=runtime_storage(durable_database, tmp_path),
    )
    preflight = service.preflight(workspace_id, [pending_id])
    service.resolve_batch(
        workspace_id, [pending_id], preflight.snapshot_sha256, origin="discovery.refresh_batch"
    )

    with durable_database.session() as session:
        reset_pending_queue(session, workspace_id)

    with durable_database.session() as session:
        row = session.get(PendingDiscovery, pending_id)
        assert row is not None
        assert row.withdrawn_at == closed_at
        assert row.archived_at is None
        reset_events = session.scalars(
            select(AuditEvent).where(
                AuditEvent.entity_id == pending_id,
                AuditEvent.action == "pending_discovery.withdrawal_reset",
            )
        ).all()
        assert reset_events == []

    page = SourceScanQueries(durable_database).pending_discovery_page(workspace_id)
    visible_ids = {item.id for item in page.ready.items}
    visible_ids |= {item.id for item in page.attention.items}
    assert pending_id not in visible_ids


def test_queue_reset_clears_a_withdrawal_with_no_closed_evidence(
    durable_database: Database, tmp_path: Path
) -> None:
    """The closure guard must not over-protect a sourceless withdrawal with no closed body."""
    workspace_id, pending_id, _url = _pending(durable_database, tmp_path)
    staging = DiscoveryStaging(
        durable_database,
        PostingRefreshService(
            durable_database,
            storage=runtime_storage(durable_database, tmp_path),
            fetcher=UnexpectedPostingFetcher(),
        ),
    )
    staging.checks.withdraw_pending_discovery(
        workspace_id,
        pending_id,
        note="The publisher check found this posting gone.",
        actor="test",
        origin="test",
        correlation_id=None,
    )

    with durable_database.session() as session:
        reset_pending_queue(session, workspace_id)

    with durable_database.session() as session:
        row = session.get(PendingDiscovery, pending_id)
        assert row is not None
        assert row.withdrawn_at is None


def test_automatic_resolution_reports_the_row_it_withdrew(
    durable_database: Database, tmp_path: Path
) -> None:
    """An automatic pass names the rows it closed, so its caller can report them."""
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    closed = ResolvedPostingContent(
        requested_url=url,
        final_url=url,
        content_status=PostingContentAccessStatus.NOT_YET_READ,
        identity_status=PostingIdentityStatus.NOT_YET_VERIFIED,
        posting_status=PostingStatus.CLOSED,
        extraction_method=None,
        fetched_at=datetime(2026, 9, 9, tzinfo=UTC),
        http_status=404,
        identity_evidence={"closure": True},
        note="The publisher reports that this posting is closed.",
    )
    service = PostingContentResolutionService(
        durable_database,
        ScriptedResolver([closed]),
        storage=runtime_storage(durable_database, tmp_path),
    )

    result = service.resolve_automatic(workspace_id, [pending_id], origin="source.scan")

    assert result["withdrawn_ids"] == [pending_id]
    with durable_database.session() as session:
        row = session.get(PendingDiscovery, pending_id)
        assert row is not None and row.withdrawn_at is not None


class _WithdrawingResolver(ScriptedResolver):
    """Withdraws its row while the read is in flight, as a complete board read does."""

    def __init__(
        self,
        results: list[ResolvedPostingContent],
        database: Database,
        pending_id: str,
    ) -> None:
        super().__init__(results)
        self._database = database
        self._pending_id = pending_id

    def resolve(
        self,
        url: str,
        *,
        employer: str,
        title: str,
        intent: PostingReadIntent = PostingReadIntent.CRAWL,
        board_url: str = "",
    ) -> ResolvedPostingContent:
        with self._database.session() as session:
            row = session.get(PendingDiscovery, self._pending_id)
            assert row is not None
            row.withdrawn_at = datetime(2026, 9, 9, tzinfo=UTC)
        return super().resolve(url, employer=employer, title=title, intent=intent)


def test_automatic_resolution_reports_the_row_it_relisted(
    durable_database: Database, tmp_path: Path
) -> None:
    """An automatic pass names the rows it returned to the queue, as it names the closed ones."""
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    service = PostingContentResolutionService(
        durable_database,
        _WithdrawingResolver([_readable(url)], durable_database, pending_id),
        storage=runtime_storage(durable_database, tmp_path),
    )

    result = service.resolve_automatic(workspace_id, [pending_id], origin="source.scan")

    assert result["relisted_ids"] == [pending_id]
    assert result["withdrawn_ids"] == []
    with durable_database.session() as session:
        row = session.get(PendingDiscovery, pending_id)
        assert row is not None
        assert row.withdrawn_at is None


def test_a_new_body_that_relists_a_withdrawn_row_restores_its_no_requirements_dismissal(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _dismissed_for_no_requirements(durable_database, tmp_path)
    service = PostingContentResolutionService(
        durable_database,
        _WithdrawingResolver(
            [
                _readable(
                    url, b"Operations Lead\nFive years running regional logistics is required."
                )
            ],
            durable_database,
            pending_id,
        ),
        storage=runtime_storage(durable_database, tmp_path),
    )

    service.resolve_batch(
        workspace_id,
        [pending_id],
        service.preflight(workspace_id, [pending_id]).snapshot_sha256,
        origin="discovery.refresh_batch",
    )

    assert _latest_disposition(durable_database, workspace_id, pending_id) == (
        "restore",
        "audit",
        "no_requirements",
    )


@pytest.mark.parametrize("prefetched", [False, True])
def test_source_ownership_loss_preserves_pending_content_without_late_mutations(
    durable_database: Database, tmp_path: Path, prefetched: bool
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    result = _readable(url)
    service = PostingContentResolutionService(
        durable_database,
        ScriptedResolver([result]),
        storage=runtime_storage(durable_database, tmp_path),
    )

    def cancelled_source(session: Session) -> None:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        pending.content_note = "This transaction must roll back."
        raise ConflictError("The source read is no longer owned.")

    if prefetched:
        with pytest.raises(ConflictError, match="source read is no longer owned"):
            service.store_prefetched_result(
                workspace_id,
                pending_id,
                result,
                None,
                origin="source.scan",
                require_source_read=cancelled_source,
            )
    else:
        with pytest.raises(ConflictError, match="source read is no longer owned"):
            service.resolve_automatic(
                workspace_id,
                [pending_id],
                origin="source.scan",
                require_source_read=cancelled_source,
            )

    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        assert pending.current_content_id is None
        assert pending.content_note != "This transaction must roll back."
        assert list(session.scalars(select(PendingPostingContent))) == []
        assert list(session.scalars(select(Artifact))) == []
        assert list(session.scalars(latest_disposition_statement(workspace_id, [pending_id]))) == []


def test_automatic_resolution_persists_once_and_reuses_current_content(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    resolver = ScriptedResolver([_readable(url)])
    service = PostingContentResolutionService(
        durable_database, resolver, storage=runtime_storage(durable_database, tmp_path)
    )

    first = service.resolve_automatic(
        workspace_id,
        [pending_id],
        origin="search.guided_campaign",
    )
    second = service.resolve_automatic(
        workspace_id,
        [pending_id],
        origin="source.scan",
    )

    assert first["attempted"] == 1
    assert first["outcomes"] == {"readable": 1}
    assert second == {
        "selected": 1,
        "attempted": 0,
        "retained": 1,
        "outcomes": {},
        "evidence_ids": [],
        "withdrawn_ids": [],
        "relisted_ids": [],
    }
    assert resolver.calls == [(url, "", "")]
    with durable_database.session() as session:
        contents = list(
            session.scalars(
                select(PendingPostingContent).where(
                    PendingPostingContent.pending_discovery_id == pending_id
                )
            )
        )
    assert len(contents) == 1


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_automatic_resolution_repairs_an_unavailable_retained_body(
    durable_database: Database, tmp_path: Path, damage: str
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    body = b"Operations Lead\nBuild reliable systems."
    _resolve(durable_database, tmp_path, workspace_id, pending_id, url, body)
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        previous = session.get(PendingPostingContent, pending.current_content_id)
        assert previous is not None
        previous_id = previous.id
        artifact = session.get(Artifact, previous.artifact_id)
        assert artifact is not None
        path = file_uri_path(artifact.storage_uri)
        assert path is not None
    if damage == "missing":
        path.unlink()
    elif damage == "corrupt":
        path.write_bytes(b"Corrupt retained bytes.")
    resolver = ScriptedResolver([_readable(url, body)])
    storage = runtime_storage(durable_database, tmp_path)
    service = PostingContentResolutionService(durable_database, resolver, storage=storage)

    report = service.resolve_automatic(workspace_id, [pending_id], origin="source.scan")

    assert report["attempted"] == 1
    assert report["retained"] == 0
    assert report["outcomes"] == {"readable": 1}
    assert resolver.intents == [PostingReadIntent.CRAWL]
    current = CurrentPostingContentReader(durable_database, storage=storage).read(
        workspace_id, pending_id
    )
    assert current is not None and current.body == body
    assert current.evidence_id != previous_id
    assert report["evidence_ids"] == [current.evidence_id]
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        repaired = session.get(PendingPostingContent, current.evidence_id)
        previous = session.get(PendingPostingContent, previous_id)
        assert pending is not None and repaired is not None and previous is not None
        recovery = repaired.identity_evidence["retained_body_recovery"]
        assert "retained posting body is unavailable" in recovery
        assert "Recovered from fresh posting content." in recovery
        assert recovery in pending.content_note
        assert repaired.identity_evidence["heading"] == "Operations Lead"
        assert repaired.supersedes_content_id == previous_id
        assert previous.identity_evidence == {"heading": "Operations Lead"}


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
@pytest.mark.parametrize("automatic", [True, False], ids=["automatic", "batch"])
def test_a_failed_repair_records_unreadable_content_and_preserves_the_known_date(
    durable_database: Database, tmp_path: Path, damage: str, automatic: bool
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    body = b"Operations Lead\nBuild reliable systems."
    observed_at = datetime(2026, 10, 8, 12, tzinfo=UTC)
    original = replace(
        _readable(url, body),
        fetched_at=observed_at,
        source=PostingSource(
            publisher="page",
            record=json.dumps({"validThrough": "2026-10-19", "description": body.decode()}),
        ),
        publisher_closing_date=publisher_closing_date(
            body.decode(),
            valid_through="2026-10-19",
            source_url=url,
            observed_at=observed_at,
        ),
    )
    failure_note = "The publisher returned HTTP 503."
    failed_at = datetime(2026, 10, 9, 12, tzinfo=UTC)
    failed = replace(
        _readable(url),
        content=None,
        content_status=PostingContentAccessStatus.FETCH_FAILED,
        identity_status=PostingIdentityStatus.NOT_YET_VERIFIED,
        posting_status=None,
        extraction_method=None,
        fetched_at=failed_at,
        http_status=503,
        identity_evidence={"error": failure_note},
        note=failure_note,
    )
    resolver = ScriptedResolver([original, failed])
    storage = runtime_storage(durable_database, tmp_path)
    service = PostingContentResolutionService(durable_database, resolver, storage=storage)
    service.resolve_batch(
        workspace_id,
        [pending_id],
        service.preflight(workspace_id, [pending_id]).snapshot_sha256,
        origin="discovery.refresh_batch",
    )
    override = date(2026, 10, 22)
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        pending.closing_date_override = override
        pending.closing_date_override_updated_at = observed_at
        previous = session.get(PendingPostingContent, pending.current_content_id)
        assert previous is not None
        previous_id = previous.id
        previous_date = previous.publisher_closing_date
        assert previous_date is not None
        artifact = session.get(Artifact, previous.artifact_id)
        assert artifact is not None
        path = file_uri_path(artifact.storage_uri)
        assert path is not None
    if damage == "missing":
        path.unlink()
    elif damage == "corrupt":
        path.write_bytes(b"Corrupt retained bytes.")

    if automatic:
        report = service.resolve_automatic(workspace_id, [pending_id], origin="source.scan")
        assert report["attempted"] == 1
        assert report["retained"] == 0
        assert report["outcomes"] == {"fetch_failed": 1}
    else:
        service.resolve_batch(
            workspace_id,
            [pending_id],
            service.preflight(workspace_id, [pending_id]).snapshot_sha256,
            origin="discovery.refresh_batch",
        )

    assert (
        CurrentPostingContentReader(durable_database, storage=storage).read(
            workspace_id, pending_id
        )
        is None
    )
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        current = session.get(PendingPostingContent, pending.current_content_id)
        previous = session.get(PendingPostingContent, previous_id)
        assert current is not None and previous is not None
        assert current.id != previous_id
        assert current.supersedes_content_id == previous_id
        assert pending.content_status == PostingContentAccessStatus.FETCH_FAILED
        assert (
            pending_discovery_readiness(pending, current) == PendingDiscoveryReadiness.FETCH_FAILED
        )
        assert failure_note in pending.content_note
        assert "retained posting body is unavailable" in pending.content_note
        assert "Retry" in pending.content_note
        assert "retained posting body remains current" not in pending.content_note
        assert current.artifact_id is None
        assert current.identity_evidence["error"] == failure_note
        assert (
            "retained posting body is unavailable"
            in current.identity_evidence["retained_body_recovery"]
        )
        assert current.fetched_at == failed_at
        assert current.publisher_closing_date == previous_date
        assert previous.publisher_closing_date == previous_date
        assert previous.fetched_at == observed_at
        assert pending.closing_date_override == override
        assert pending.closing_date_override_updated_at == observed_at


@pytest.mark.parametrize("automatic", [True, False], ids=["automatic", "batch"])
def test_a_failed_repair_replaces_content_whose_digest_disagrees_with_its_intact_artifact(
    durable_database: Database, tmp_path: Path, automatic: bool
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    body = b"Operations Lead\nBuild reliable systems."
    _resolve(durable_database, tmp_path, workspace_id, pending_id, url, body)
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        previous = session.get(PendingPostingContent, pending.current_content_id)
        assert previous is not None
        damaged = pending_posting_content(
            workspace_id,
            pending_id,
            url,
            artifact_id=previous.artifact_id,
            final_url=url,
            final_url_sha256=previous.final_url_sha256,
            content_sha256="0" * 64,
            identity_status=PostingIdentityStatus.VERIFIED,
            posting_status=PostingStatus.ACTIVE,
            extraction_method=PostingContentExtractionMethod.STATIC_HTML,
            fetched_at=previous.fetched_at,
            publisher_closing_date=previous.publisher_closing_date,
            supersedes_content_id=previous.id,
        )
        session.add(damaged)
        session.flush()
        damaged_id = damaged.id
        pending.current_content_id = damaged_id
    storage = runtime_storage(durable_database, tmp_path)
    with pytest.raises(ValidationError, match="digest does not match"):
        CurrentPostingContentReader(durable_database, storage=storage).read(
            workspace_id, pending_id
        )
    failed = replace(
        _readable(url),
        content=None,
        content_status=PostingContentAccessStatus.FETCH_FAILED,
        identity_status=PostingIdentityStatus.NOT_YET_VERIFIED,
        posting_status=None,
        extraction_method=None,
        http_status=503,
        identity_evidence={"error": "The publisher returned HTTP 503."},
        note="The publisher returned HTTP 503.",
    )
    service = PostingContentResolutionService(
        durable_database, ScriptedResolver([failed]), storage=storage
    )

    if automatic:
        report = service.resolve_automatic(workspace_id, [pending_id], origin="source.scan")
        assert report["attempted"] == 1
        assert report["retained"] == 0
        assert report["outcomes"] == {"fetch_failed": 1}
    else:
        service.resolve_batch(
            workspace_id,
            [pending_id],
            service.preflight(workspace_id, [pending_id]).snapshot_sha256,
            origin="discovery.refresh_batch",
        )

    assert (
        CurrentPostingContentReader(durable_database, storage=storage).read(
            workspace_id, pending_id
        )
        is None
    )
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        current = session.get(PendingPostingContent, pending.current_content_id)
        damaged_evidence = session.get(PendingPostingContent, damaged_id)
        assert current is not None and damaged_evidence is not None
        assert current.supersedes_content_id == damaged_id
        assert (
            pending_discovery_readiness(pending, current) == PendingDiscoveryReadiness.FETCH_FAILED
        )
        assert pending.content_status == PostingContentAccessStatus.FETCH_FAILED
        assert "retained posting body remains current" not in pending.content_note
        assert "retained posting body is unavailable" in pending.content_note
        assert "Retry" in pending.content_note
        assert current.identity_evidence["error"] == "The publisher returned HTTP 503."
        assert (
            "retained posting body is unavailable"
            in current.identity_evidence["retained_body_recovery"]
        )
        assert damaged_evidence.content_sha256 == "0" * 64
        artifact = session.get(Artifact, damaged_evidence.artifact_id)
        workspace = session.get(Workspace, workspace_id)
        assert artifact is not None and workspace is not None
        assert service.artifacts.read(artifact, workspace) == body


def test_automatic_resolution_replays_a_prefetched_read_failure_without_reading_again(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    resolver = ScriptedResolver([_readable(url)])
    service = PostingContentResolutionService(
        durable_database, resolver, storage=runtime_storage(durable_database, tmp_path)
    )
    prefetched = PrefetchedContent(url, "", "", failure=OSError("synthetic prefetch failure"))

    with pytest.raises(OSError, match="synthetic prefetch failure"):
        service.resolve_automatic(
            workspace_id, [pending_id], origin="source.scan", prefetched={url: prefetched}
        )

    assert resolver.calls == []
    assert service.current.read(workspace_id, pending_id) is None


def test_automatic_resolution_reads_live_when_a_prefetch_asked_for_another_identity(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    resolver = ScriptedResolver([_readable(url, b"Operations Lead\nThe live publisher body.")])
    service = PostingContentResolutionService(
        durable_database, resolver, storage=runtime_storage(durable_database, tmp_path)
    )
    prefetched = PrefetchedContent(
        url, "Acme", "Platform Lead", result=_readable(url, b"Platform Lead\nA stale body.")
    )

    result = service.resolve_automatic(
        workspace_id, [pending_id], origin="source.scan", prefetched={url: prefetched}
    )

    assert result["outcomes"] == {"readable": 1}
    assert resolver.calls == [(url, "", "")]
    assert _current_body(durable_database, tmp_path, workspace_id, pending_id) == (
        b"Operations Lead\nThe live publisher body."
    )


@pytest.mark.parametrize("automatic_lanes", [1, 2])
def test_automatic_resolution_carries_protection_into_each_lane_and_restores_it(
    durable_database: Database, tmp_path: Path, automatic_lanes: int
) -> None:
    workspace_id, redirect_id, redirect_url = _pending(durable_database, tmp_path)
    protected_url = "https://example.test/jobs/protected"
    sibling_url = "https://other.example.test/jobs/operations-lead"
    sibling_id = _add_pending(
        durable_database,
        workspace_id,
        employer="Acme",
        title="Operations Lead",
        first_seen_on=date(2026, 8, 28),
        url=sibling_url,
    )
    requests: list[str] = []

    def response(url: str) -> httpx.Response:
        requests.append(url)
        if url == redirect_url:
            return httpx.Response(
                302, headers={"Location": protected_url}, request=httpx.Request("GET", url)
            )
        return httpx.Response(200, request=httpx.Request("GET", url), text=_DECLARING_PAGE)

    resolver = DeterministicPostingContentResolver(
        client=policy_client(response), rendered=cast(Any, _NoBrowser())
    )
    service = PostingContentResolutionService(
        durable_database,
        resolver,
        storage=runtime_storage(durable_database, tmp_path),
        automatic_lanes=automatic_lanes,
    )
    options = SourceReadOptions(protected_posting_urls=frozenset({protected_url}))

    with guarded_posting_reads(options.allows_posting):
        result = service.resolve_automatic(
            workspace_id, [redirect_id, sibling_id], origin="source.scan"
        )

    assert protected_url not in requests
    assert redirect_url in requests and sibling_url in requests
    assert result["attempted"] == 2
    assert result["outcomes"].get("readable") == 1
    assert len(result["evidence_ids"]) == 2
    assert service.current.read(workspace_id, sibling_id) is not None
    assert resolver.resolve(
        redirect_url, employer="Acme", title="Operations Lead"
    ).content_status == (PostingContentAccessStatus.READABLE)
    assert protected_url in requests


def test_automatic_resolution_overlaps_a_bounded_group_of_independent_reads(
    durable_database: Database,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    workspace_id, first_id, first_url = _pending(durable_database, tmp_path)
    second_url = "https://other.example.test/jobs/platform-lead"
    with durable_database.session() as session:
        second = PendingDiscovery(
            workspace_id=workspace_id,
            fingerprint="b" * 64,
            employer="Example",
            title="Platform Lead",
            discovery_url=second_url,
            reason="Publisher-listed role",
            first_seen_on=date(2026, 8, 28),
        )
        session.add(second)
        session.flush()
        second_id = second.id
    rendezvous = Barrier(2)

    class OverlapResolver:
        def resolve(
            self,
            url: str,
            *,
            employer: str,
            title: str,
            intent: PostingReadIntent = PostingReadIntent.CRAWL,
            board_url: str = "",
        ) -> ResolvedPostingContent:
            rendezvous.wait()
            return _readable(url, f"{title}\nVerified publisher body.".encode())

    service = PostingContentResolutionService(
        durable_database,
        OverlapResolver(),
        storage=runtime_storage(durable_database, tmp_path),
        automatic_lanes=2,
    )

    with caplog.at_level(
        "INFO", logger="clearscout.application.discovery.content_resolution.reads"
    ):
        result = service.resolve_automatic(
            workspace_id,
            [first_id, second_id],
            origin="search.guided_campaign",
        )

    assert result["attempted"] == 2
    assert result["outcomes"] == {"readable": 2}
    assert len(result["evidence_ids"]) == 2
    assert first_url != second_url
    finished = [
        record
        for record in caplog.records
        if record.getMessage() == "posting_content_read"
        and getattr(record, "state", None) == "finished"
    ]
    assert {getattr(record, "workspace_id", None) for record in finished} == {workspace_id}
    assert {getattr(record, "url_sha256", None) for record in finished} == {
        url_sha256(first_url),
        url_sha256(second_url),
    }
    assert {getattr(record, "pending_discovery_id", None) for record in finished} == {
        first_id,
        second_id,
    }


def test_automatic_resolution_refills_a_fast_lane_before_a_slow_read_finishes(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, first_id, first_url = _pending(durable_database, tmp_path)
    second_url = "https://fast.example.test/jobs/platform-lead"
    third_url = "https://next.example.test/jobs/analytics-lead"
    with durable_database.session() as session:
        second = PendingDiscovery(
            workspace_id=workspace_id,
            fingerprint="b" * 64,
            employer="Example",
            title="Platform Lead",
            discovery_url=second_url,
            reason="Publisher-listed role",
            first_seen_on=date(2026, 8, 28),
        )
        third = PendingDiscovery(
            workspace_id=workspace_id,
            fingerprint="c" * 64,
            employer="Example",
            title="Analytics Lead",
            discovery_url=third_url,
            reason="Publisher-listed role",
            first_seen_on=date(2026, 8, 28),
        )
        session.add_all([second, third])
        session.flush()
        second_id = second.id
        third_id = third.id
    third_started = Event()

    class UnevenResolver:
        def resolve(
            self,
            url: str,
            *,
            employer: str,
            title: str,
            intent: PostingReadIntent = PostingReadIntent.CRAWL,
            board_url: str = "",
        ) -> ResolvedPostingContent:
            # The slow read ends only once a refilled lane has started the third.
            if url == first_url:
                third_started.wait()
            elif url == third_url:
                third_started.set()
            return _readable(url, f"{title}\nVerified publisher body.".encode())

    service = PostingContentResolutionService(
        durable_database,
        UnevenResolver(),
        storage=runtime_storage(durable_database, tmp_path),
        automatic_lanes=2,
    )
    result = service.resolve_automatic(
        workspace_id, [first_id, second_id, third_id], origin="source.scan"
    )

    assert result["attempted"] == 3
    assert result["outcomes"] == {"readable": 3}


def test_batch_resolution_refills_a_fast_lane_before_a_slow_read_finishes(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, first_id, first_url = _pending(durable_database, tmp_path)
    second_url = "https://fast.example.test/jobs/platform-lead"
    third_url = "https://next.example.test/jobs/analytics-lead"
    with durable_database.session() as session:
        second = PendingDiscovery(
            workspace_id=workspace_id,
            fingerprint="d" * 64,
            employer="Example",
            title="Platform Lead",
            discovery_url=second_url,
            reason="Publisher-listed role",
            first_seen_on=date(2026, 8, 28),
        )
        third = PendingDiscovery(
            workspace_id=workspace_id,
            fingerprint="e" * 64,
            employer="Example",
            title="Analytics Lead",
            discovery_url=third_url,
            reason="Publisher-listed role",
            first_seen_on=date(2026, 8, 28),
        )
        session.add_all([second, third])
        session.flush()
        second_id = second.id
        third_id = third.id
    third_started = Event()

    class UnevenResolver:
        def resolve(
            self,
            url: str,
            *,
            employer: str,
            title: str,
            intent: PostingReadIntent = PostingReadIntent.CRAWL,
            board_url: str = "",
        ) -> ResolvedPostingContent:
            # The slow read ends only once a refilled lane has started the third.
            if url == first_url:
                third_started.wait()
            elif url == third_url:
                third_started.set()
            return _readable(url, f"{title}\nVerified publisher body.".encode())

    service = PostingContentResolutionService(
        durable_database,
        UnevenResolver(),
        storage=runtime_storage(durable_database, tmp_path),
        automatic_lanes=2,
    )
    snapshot = service.preflight(workspace_id, [first_id, second_id, third_id]).snapshot_sha256
    result = service.resolve_batch(
        workspace_id,
        [first_id, second_id, third_id],
        snapshot,
        origin="discovery.refresh_batch",
    )

    assert result["completed"] == 3
    assert result["outcomes"] == {"readable": 3}


def test_automatic_resolution_retains_a_successful_sibling_when_one_read_fails(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, first_id, first_url = _pending(durable_database, tmp_path)
    second_url = "https://failure.example.test/jobs/platform-lead"
    with durable_database.session() as session:
        second = PendingDiscovery(
            workspace_id=workspace_id,
            fingerprint="c" * 64,
            employer="Example",
            title="Platform Lead",
            discovery_url=second_url,
            reason="Publisher-listed role",
            first_seen_on=date(2026, 8, 28),
        )
        session.add(second)
        session.flush()
        second_id = second.id
    rendezvous = Barrier(2)

    class PartialFailureResolver:
        def resolve(
            self,
            url: str,
            *,
            employer: str,
            title: str,
            intent: PostingReadIntent = PostingReadIntent.CRAWL,
            board_url: str = "",
        ) -> ResolvedPostingContent:
            rendezvous.wait()
            if url == second_url:
                raise OSError("synthetic read failure")
            return _readable(url, f"{title}\nVerified publisher body.".encode())

    service = PostingContentResolutionService(
        durable_database,
        PartialFailureResolver(),
        storage=runtime_storage(durable_database, tmp_path),
        automatic_lanes=2,
    )

    with pytest.raises(OSError, match="synthetic read failure"):
        service.resolve_automatic(
            workspace_id,
            [first_id, second_id],
            origin="search.guided_campaign",
        )

    assert service.current.read(workspace_id, first_id) is not None
    assert service.current.read(workspace_id, second_id) is None
    assert first_url != second_url


def _repoint_events(database: Database, pending_id: str) -> list[AuditEvent]:
    with database.session() as session:
        events = session.scalars(
            select(AuditEvent).where(
                AuditEvent.entity_id == pending_id,
                AuditEvent.action == "pending_discovery.repointed_on_content_read",
            )
        ).all()
        session.expunge_all()
        return list(events)


def test_an_assisted_read_that_does_not_verify_the_posting_keeps_the_rows_address(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, original_url = _pending(durable_database, tmp_path)
    candidate_url = "https://careers.example.test/openings/operations-lead"
    service = PostingContentResolutionService(
        durable_database,
        ScriptedResolver([]),
        storage=runtime_storage(durable_database, tmp_path),
    )
    [target] = service.preflight(workspace_id, [pending_id]).targets

    stored = service.store_assisted_result(
        workspace_id,
        target,
        _readable(candidate_url, identity_status=PostingIdentityStatus.CONTRADICTED),
        None,
    )

    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        evidence = session.get(PendingPostingContent, stored.id)
        assert pending is not None and evidence is not None
        assert pending.current_content_id == evidence.id
        assert evidence.requested_url == candidate_url
        assert (pending.canonical_url or pending.discovery_url) == original_url
        assert pending_discovery_readiness(pending, evidence) is (
            PendingDiscoveryReadiness.URL_MISMATCH
        )
    assert _repoint_events(durable_database, pending_id) == []


def test_an_ordinary_read_at_the_rows_own_address_writes_no_repoint(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)

    _resolve(durable_database, tmp_path, workspace_id, pending_id, url, b"Operations Lead\nRun it.")

    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        evidence = session.get(PendingPostingContent, pending.current_content_id)
        assert pending_discovery_readiness(pending, evidence) is PendingDiscoveryReadiness.READY
        assert pending.canonical_url == ""
    assert _repoint_events(durable_database, pending_id) == []


def test_an_aggregator_listing_keeps_the_apply_address_it_supplied(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database, tmp_path))
    workspace_id = workspace.create(slug="aggregator", name="Aggregator").id
    apply_url = "https://himalayas.app/companies/acme/jobs/operations-lead"
    source_id = board_source(
        durable_database,
        workspace_id,
        "Himalayas",
        "https://himalayas.app/jobs",
        source_type="aggregator",
    )
    pending_id = _add_pending(
        durable_database,
        workspace_id,
        employer="Acme",
        title="Operations Lead",
        first_seen_on=date(2026, 8, 28),
        url=apply_url,
        source_id=source_id,
    )
    service = PostingContentResolutionService(
        durable_database, ScriptedResolver([]), storage=runtime_storage(durable_database, tmp_path)
    )
    [target] = service.preflight(workspace_id, [pending_id]).targets

    service.store_assisted_result(
        workspace_id, target, _readable("https://careers.example.test/jobs/ops"), None
    )

    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        assert (pending.canonical_url or pending.discovery_url) == apply_url
    assert _repoint_events(durable_database, pending_id) == []


def _no_busy_wait(dbapi_connection: Any, _record: object) -> None:
    dbapi_connection.execute("PRAGMA busy_timeout=0")


class _RepointDuringStore(ScriptedResolver):
    """Commit a re-point from a second connection when the store loads the row it checks."""

    def __init__(self, read: ResolvedPostingContent, repoint: Callable[[], object]) -> None:
        super().__init__([read])
        self.repoint = repoint
        self.refused: list[OperationalError] = []

    def resolve(self, url: str, **kwargs: Any) -> ResolvedPostingContent:
        event.listen(PendingDiscovery, "load", self._on_load, once=True)
        return super().resolve(url, **kwargs)

    def _on_load(self, _row: PendingDiscovery, _context: object) -> None:
        try:
            self.repoint()
        except OperationalError as error:
            self.refused.append(error)


def test_a_repoint_cannot_commit_between_a_stores_check_and_its_write(
    durable_database: Database, durable_database_url: str, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    other = Database(durable_database_url)
    # Refuse at once rather than wait, so a held write lock shows as a refusal.
    event.listen(other.engine, "connect", _no_busy_wait)
    recovery = PostingContentResolutionService(
        other, ScriptedResolver([]), storage=runtime_storage(other, tmp_path)
    )
    [target] = recovery.preflight(workspace_id, [pending_id]).targets
    resolver = _RepointDuringStore(
        _readable(url),
        lambda: recovery.store_assisted_result(
            workspace_id, target, _readable("https://careers.example.test/jobs/ops"), None
        ),
    )
    refresh = PostingContentResolutionService(
        durable_database, resolver, storage=runtime_storage(durable_database, tmp_path)
    )
    try:
        refresh.resolve_batch(
            workspace_id,
            [pending_id],
            refresh.preflight(workspace_id, [pending_id]).snapshot_sha256,
            origin="discovery.refresh_batch",
        )
    finally:
        other.dispose()

    assert len(resolver.refused) == 1
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        evidence = session.get(PendingPostingContent, pending.current_content_id)
        assert pending.canonical_url == ""
        assert pending_discovery_readiness(pending, evidence) is PendingDiscoveryReadiness.READY


def test_resolution_rejects_changed_snapshot_without_fetching(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    resolver = ScriptedResolver([_readable(url)])
    service = PostingContentResolutionService(
        durable_database, resolver, storage=runtime_storage(durable_database, tmp_path)
    )
    preflight = service.preflight(workspace_id, [pending_id])
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        pending.content_status = PostingContentAccessStatus.RENDER_REQUIRED

    with pytest.raises(ConflictError, match="changed after resolution preflight"):
        service.resolve_batch(
            workspace_id, [pending_id], preflight.snapshot_sha256, origin="discovery.refresh_batch"
        )

    assert resolver.calls == []


def _senior_data_engineer(database: Database, artifact_root: Path) -> tuple[str, str, str]:
    workspace_id, pending_id, url = _pending(database, artifact_root)
    with database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        pending.title = "Senior Data Engineer"
        pending.staged_facts = StagedListingFacts(
            identity_trust=ListingIdentityTrust.PUBLISHER
        ).as_json()
    return workspace_id, pending_id, url


@pytest.mark.parametrize(
    ("corrected", "expected_title"), [(False, "Senior Data Engineer"), (True, "")]
)
def test_a_hand_corrected_title_is_not_presented_as_publisher_identity(
    durable_database: Database, tmp_path: Path, corrected: bool, expected_title: str
) -> None:
    workspace_id, pending_id, url = _senior_data_engineer(durable_database, tmp_path)
    if corrected:
        PendingDiscoveryHintsService(durable_database).correct_title(
            workspace_id, pending_id, "Data Engineer II"
        )
    resolver = ScriptedResolver([_readable(url)])
    service = PostingContentResolutionService(
        durable_database, resolver, storage=runtime_storage(durable_database, tmp_path)
    )

    service.resolve_batch(
        workspace_id,
        [pending_id],
        service.preflight(workspace_id, [pending_id]).snapshot_sha256,
        origin="discovery.refresh_batch",
    )

    assert resolver.calls == [(url, "Acme", expected_title)]


def test_publisher_declaration_verifies_a_row_whose_title_a_person_corrected(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, url = _senior_data_engineer(durable_database, tmp_path)
    PendingDiscoveryHintsService(durable_database).correct_title(
        workspace_id, pending_id, "Data Engineer II"
    )
    resolver = DeterministicPostingContentResolver.__new__(DeterministicPostingContentResolver)
    resolver.static = cast(
        Any,
        _DeclaringStatic(
            FetchedPosting(
                text="Senior Data Engineer\n" + "Build reliable data pipelines. " * 30,
                posting_status="active",
                method="Greenhouse API",
                fetched_at=datetime(2026, 8, 28, tzinfo=UTC),
                title="Senior Data Engineer",
                employer="Acme",
                final_url=url,
            )
        ),
    )
    resolver.rendered = cast(Any, None)
    resolver.client = cast(Any, None)
    service = PostingContentResolutionService(
        durable_database, resolver, storage=runtime_storage(durable_database, tmp_path)
    )

    service.resolve_batch(
        workspace_id,
        [pending_id],
        service.preflight(workspace_id, [pending_id]).snapshot_sha256,
        origin="discovery.refresh_batch",
    )

    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None and pending.current_content_id is not None
        evidence = session.get(PendingPostingContent, pending.current_content_id)
        assert evidence is not None
        assert evidence.identity_status == PostingIdentityStatus.VERIFIED


def _greenhouse_check(wording: str, title: str = "Operations Lead") -> PostingCheck:
    return PostingCheck(
        status="readable",
        note="",
        platform="greenhouse",
        title=title,
        employer="Acme",
        facts=StagedListingFacts(
            posted_date=publisher_posted_date(
                wording, basis="greenhouse.first_published", grade=PostedDateGrade.FIRST_POSTED
            )
        ),
        text=f"{title}\n" + "Build reliable systems. " * 30,
    )


def _staged_posted(database: Database, pending_id: str) -> PublisherPostedDate | None:
    with database.session() as session:
        row = session.get_one(PendingDiscovery, pending_id)
        return StagedListingFacts.from_json(row.staged_facts).posted_date


def _read_detail(
    database: Database,
    tmp_path: Path,
    target: tuple[str, str, str],
    wording: str,
    *,
    on: date = date(2026, 8, 28),
    title: str = "Operations Lead",
    final_url: str = "",
    refresh_check_first: bool = False,
) -> PublisherPostedDate | None:
    """Read one row's detail page; a refresh pass first applies the same publisher check."""
    workspace_id, pending_id, url = target
    check = _greenhouse_check(wording, title)
    if refresh_check_first:
        with database.session() as session:
            apply_publisher_check(session.get_one(PendingDiscovery, pending_id), check, read_on=on)
    resolver = DeterministicPostingContentResolver.__new__(DeterministicPostingContentResolver)
    resolver.static = cast(
        Any,
        _DeclaringStatic(
            FetchedPosting(
                text=check.text,
                posting_status="active",
                method="Greenhouse API",
                fetched_at=datetime(on.year, on.month, on.day, 9, tzinfo=UTC),
                title=title,
                employer="Acme",
                final_url=final_url or url,
                publisher_check=check,
            )
        ),
    )
    resolver.rendered = cast(Any, None)
    resolver.client = cast(Any, None)
    service = PostingContentResolutionService(
        database, resolver, storage=runtime_storage(database, tmp_path)
    )
    service.resolve_batch(
        workspace_id,
        [pending_id],
        service.preflight(workspace_id, [pending_id]).snapshot_sha256,
        origin="discovery.refresh_batch",
    )
    return _staged_posted(database, pending_id)


def test_a_detail_read_stages_the_posted_date_its_publisher_states(
    durable_database: Database, tmp_path: Path
) -> None:
    target = _pending(durable_database, tmp_path)

    posted = _read_detail(durable_database, tmp_path, target, "2026-08-20")

    assert posted is not None
    assert posted.earliest.normalized_value == "2026-08-20"
    assert posted.earliest.basis == "greenhouse.first_published"
    assert posted.earliest.read_on == date(2026, 8, 28)


def test_a_detail_read_keeps_the_earlier_posted_date_a_row_already_holds(
    durable_database: Database, tmp_path: Path
) -> None:
    target = _pending(durable_database, tmp_path)
    prior = publisher_posted_date(
        "2026-08-10", basis="greenhouse.first_published", grade=PostedDateGrade.FIRST_POSTED
    )
    with durable_database.session() as session:
        row = session.get_one(PendingDiscovery, target[1])
        row.staged_facts = StagedListingFacts(posted_date=prior).as_json()

    posted = _read_detail(durable_database, tmp_path, target, "2026-08-20")

    assert posted is not None
    assert posted.earliest.normalized_value == "2026-08-10"
    assert posted.latest.normalized_value == "2026-08-20"
    assert posted.reads == 2


def test_a_refresh_pass_counts_its_check_and_detail_read_as_one_posted_date_read(
    durable_database: Database, tmp_path: Path
) -> None:
    target = _pending(durable_database, tmp_path)
    _read_detail(durable_database, tmp_path, target, "2026-08-20", on=date(2026, 8, 27))

    posted = _read_detail(
        durable_database, tmp_path, target, "2026-08-20", refresh_check_first=True
    )

    assert posted is not None
    assert (posted.reads, posted.changes) == (2, 0)


def test_a_date_that_rolls_across_refresh_passes_needs_clarification(
    durable_database: Database, tmp_path: Path
) -> None:
    target = _pending(durable_database, tmp_path)
    posted = None
    for day in (1, 2, 3, 4):
        posted = _read_detail(
            durable_database,
            tmp_path,
            target,
            f"2026-09-0{day}",
            on=date(2026, 9, day),
            refresh_check_first=True,
        )

    assert posted is not None
    assert posted.needs_clarification
    assert posted.day is None


def test_a_redirect_to_a_posting_not_verified_as_the_row_donates_no_posted_date(
    durable_database: Database, tmp_path: Path
) -> None:
    target = _pending(durable_database, tmp_path)

    posted = _read_detail(
        durable_database,
        tmp_path,
        target,
        "2026-08-20",
        title="Different Role",
        final_url="https://example.test/jobs/different-role",
    )

    with durable_database.session() as session:
        row = session.get_one(PendingDiscovery, target[1])
        evidence = session.get_one(PendingPostingContent, row.current_content_id)
        assert evidence.identity_status != PostingIdentityStatus.VERIFIED
    assert posted is None


def test_a_retained_publisher_probe_carries_the_posted_date_its_check_states() -> None:
    url = "https://example.test/jobs/operations-lead"
    check = _greenhouse_check("2026-08-20")
    fetched = FetchedPosting(
        text=check.text,
        posting_status="active",
        method="Greenhouse API",
        fetched_at=datetime(2026, 8, 28, tzinfo=UTC),
        title=check.title,
        employer="Acme",
        final_url=url,
        publisher_check=check,
    )

    result = resolved_publisher_fetch(url, fetched)

    assert result is not None
    assert result.publisher_posted_date == check.facts.posted_date


@pytest.mark.parametrize(
    ("reread_before", "read_again"),
    [
        (datetime(2026, 9, 2, tzinfo=UTC), True),
        (datetime(2026, 8, 28, tzinfo=UTC), False),
        (None, False),
    ],
    ids=["fetched-before-cutoff", "fetched-at-cutoff", "no-cutoff"],
)
def test_an_automatic_pass_reads_held_content_again_only_when_fetched_before_its_cutoff(
    durable_database: Database,
    tmp_path: Path,
    reread_before: datetime | None,
    read_again: bool,
) -> None:
    """A fresh scan reads content held from before it began, and nothing read since."""
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    _resolve(durable_database, tmp_path, workspace_id, pending_id, url, _PLACELESS_BODY)
    resolver = ScriptedResolver(
        [
            replace(
                _readable(url, _PLACELESS_BODY),
                fetched_at=datetime(2026, 9, 2, 9, tzinfo=UTC),
                publisher_posted_date=publisher_posted_date(
                    "2026-08-20",
                    basis="greenhouse.first_published",
                    grade=PostedDateGrade.FIRST_POSTED,
                ),
            )
        ]
    )
    service = PostingContentResolutionService(
        durable_database, resolver, storage=runtime_storage(durable_database, tmp_path)
    )

    report = service.resolve_automatic(
        workspace_id, [pending_id], origin="source.scan", reread_before=reread_before
    )

    posted = _staged_posted(durable_database, pending_id)
    assert (report["attempted"], report["retained"]) == ((1, 0) if read_again else (0, 1))
    assert (posted.earliest.normalized_value if posted else None) == (
        "2026-08-20" if read_again else None
    )


def test_a_bodyless_reread_under_a_cutoff_is_not_repeated_by_a_later_pass(
    durable_database: Database, tmp_path: Path
) -> None:
    """A fresh scan's blocked re-read counts as its read, and the held body stays current."""
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    _resolve(durable_database, tmp_path, workspace_id, pending_id, url, _PLACELESS_BODY)
    with durable_database.session() as session:
        held_content_id = session.get_one(PendingDiscovery, pending_id).current_content_id
    blocked = ResolvedPostingContent(
        requested_url=url,
        final_url=None,
        content_status=PostingContentAccessStatus.ACCESS_BLOCKED,
        identity_status=PostingIdentityStatus.NOT_YET_VERIFIED,
        posting_status=None,
        extraction_method=None,
        fetched_at=datetime(2026, 9, 2, 9, tzinfo=UTC),
        http_status=403,
        note="The publisher refused the read.",
    )
    cutoff = datetime(2026, 9, 2, tzinfo=UTC)
    storage = runtime_storage(durable_database, tmp_path)
    first = PostingContentResolutionService(
        durable_database, ScriptedResolver([blocked]), storage=storage
    ).resolve_automatic(workspace_id, [pending_id], origin="source.scan", reread_before=cutoff)
    later = ScriptedResolver([_readable(url)])

    second = PostingContentResolutionService(
        durable_database, later, storage=storage
    ).resolve_automatic(workspace_id, [pending_id], origin="source.continue", reread_before=cutoff)

    assert first["attempted"] == 1
    assert (second["attempted"], second["retained"], later.calls) == (0, 1, [])
    with durable_database.session() as session:
        assert session.get_one(PendingDiscovery, pending_id).current_content_id == held_content_id


class _ScanTask:
    """The task context of a later scan pass, which reports nothing back."""

    queue = cast(TaskQueueService, None)
    worker_id = "worker-1"

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id

    def progress(self, stage: str, payload: dict[str, Any]) -> None:
        pass

    def cancellation_requested(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        pass


@pytest.mark.parametrize(
    ("reread_before", "read_again"),
    [(None, False), (datetime(2026, 9, 2, tzinfo=UTC), True)],
    ids=["ordinary-rescan", "fresh-scan"],
)
def test_a_body_dismissed_row_is_read_again_only_under_a_fresh_scan_cutoff(
    durable_database: Database,
    tmp_path: Path,
    reread_before: datetime | None,
    read_again: bool,
) -> None:
    """A later scan pass keeps a body-dismissed row's held body unless a fresh scan began."""
    screened = _body_screened(
        durable_database,
        tmp_path,
        _canada_home_with_us_remote(),
        "Candidates must reside in the United States.",
    )
    assert screened is not None
    assert screened[0] == "location_residence_required"
    with durable_database.session() as session:
        row = session.scalars(select(PendingDiscovery)).one()
        workspace_id, pending_id = row.workspace_id, row.id
    resolver = ScriptedResolver([_readable("https://example.test/jobs/operations-lead")])

    report = PostingContentResolutionService(
        durable_database, resolver, storage=runtime_storage(durable_database, tmp_path)
    ).resolve_automatic(
        workspace_id,
        [pending_id],
        _ScanTask("scan-2"),
        origin="source.scan",
        reread_before=reread_before,
    )

    assert (report["attempted"], len(resolver.calls)) == ((1, 1) if read_again else (0, 0))


@pytest.mark.parametrize("field", ["employer", "title"])
def test_resolution_binds_presented_identity_fields_in_snapshot(
    durable_database: Database, tmp_path: Path, field: str
) -> None:
    workspace_id, pending_id, url = _pending(durable_database, tmp_path)
    resolver = ScriptedResolver([_readable(url)])
    service = PostingContentResolutionService(
        durable_database, resolver, storage=runtime_storage(durable_database, tmp_path)
    )
    preflight = service.preflight(workspace_id, [pending_id])
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        setattr(pending, field, f"Changed {field}")

    with pytest.raises(ConflictError, match="changed after resolution preflight"):
        service.resolve_batch(
            workspace_id, [pending_id], preflight.snapshot_sha256, origin="discovery.refresh_batch"
        )

    assert resolver.calls == []


@pytest.mark.parametrize(
    "result",
    [
        ResolvedPostingContent(
            requested_url="https://example.test/jobs/operations-lead",
            final_url=None,
            content_status=PostingContentAccessStatus.READABLE,
            identity_status=PostingIdentityStatus.VERIFIED,
            posting_status=PostingStatus.ACTIVE,
            extraction_method=PostingContentExtractionMethod.STATIC_HTML,
            fetched_at=datetime(2026, 8, 28, tzinfo=UTC),
        ),
        ResolvedPostingContent(
            requested_url="https://example.test/jobs/operations-lead",
            final_url="https://example.test/jobs/operations-lead",
            content_status=PostingContentAccessStatus.READABLE,
            identity_status=PostingIdentityStatus.VERIFIED,
            posting_status=PostingStatus.ACTIVE,
            extraction_method=PostingContentExtractionMethod.STATIC_HTML,
            fetched_at=datetime(2026, 8, 28),
            content=b"body",
        ),
        ResolvedPostingContent(
            requested_url="https://example.test/jobs/operations-lead",
            final_url="https://example.test/jobs/operations-lead",
            content_status=PostingContentAccessStatus.READABLE,
            identity_status=PostingIdentityStatus.VERIFIED,
            posting_status=PostingStatus.CLOSED,
            extraction_method=PostingContentExtractionMethod.STATIC_HTML,
            fetched_at=datetime(2026, 8, 28, tzinfo=UTC),
            content=b"body",
        ),
        ResolvedPostingContent(
            requested_url="https://example.test/jobs/operations-lead",
            final_url=None,
            content_status=PostingContentAccessStatus.ACCESS_BLOCKED,
            identity_status=PostingIdentityStatus.NOT_YET_VERIFIED,
            posting_status=PostingStatus.ACTIVE,
            extraction_method=None,
            fetched_at=datetime(2026, 8, 28, tzinfo=UTC),
        ),
        ResolvedPostingContent(
            requested_url="https://wrong.example/jobs/operations-lead",
            final_url=None,
            content_status=PostingContentAccessStatus.ACCESS_BLOCKED,
            identity_status=PostingIdentityStatus.NOT_YET_VERIFIED,
            posting_status=None,
            extraction_method=None,
            fetched_at=datetime(2026, 8, 28, tzinfo=UTC),
        ),
    ],
)
def test_resolution_rejects_malformed_effect_results_before_persistence(
    durable_database: Database, tmp_path: Path, result: ResolvedPostingContent
) -> None:
    workspace_id, pending_id, _url = _pending(durable_database, tmp_path)
    service = PostingContentResolutionService(
        durable_database,
        ScriptedResolver([result]),
        storage=runtime_storage(durable_database, tmp_path),
    )
    preflight = service.preflight(workspace_id, [pending_id])

    with pytest.raises(ValidationError):
        service.resolve_batch(
            workspace_id, [pending_id], preflight.snapshot_sha256, origin="discovery.refresh_batch"
        )

    with durable_database.session() as session:
        assert session.scalar(select(PendingPostingContent.id)) is None


def test_pending_discovery_response_rejects_an_unknown_readiness_value() -> None:
    """The wire model's readiness field is closed, not a free string."""
    fields: dict[str, Any] = {
        "id": "pending-1",
        "employer": "Acme",
        "title": "Engineer",
        "location": "Remote",
        "discovery_url": "https://example.com/job",
        "canonical_url": "https://example.com/job",
        "content_status": PostingContentAccessStatus.NOT_YET_READ,
        "content_note": "",
        "reason": "matched",
        "recommendation_because": "matched",
        "constraint_confidence": "pursue",
        "screening_standing": "match",
        "screening_findings": [],
        "arrangement_categories": ["unknown"],
        "arrangement_categories_source": "missing",
        "first_seen_on": date(2026, 1, 1),
        "staged_facts": StagedListingFactsResponse(),
    }

    PendingDiscoveryResponse(readiness=PendingDiscoveryReadiness.NO_BODY, **fields)

    with pytest.raises(PydanticValidationError):
        PendingDiscoveryResponse(readiness=cast(Any, "not_a_real_readiness_state"), **fields)


def test_a_different_body_digest_does_not_link(durable_database: Database, tmp_path: Path) -> None:
    workspace_id, older_id, older_url = _pending(durable_database, tmp_path)
    newer_url = "https://example.test/jobs/operations-lead-remote"
    newer_id = _add_pending(
        durable_database,
        workspace_id,
        employer="Acme",
        title="Operations Lead",
        first_seen_on=date(2026, 8, 29),
        url=newer_url,
    )
    _resolve(
        durable_database,
        tmp_path,
        workspace_id,
        older_id,
        older_url,
        b"Operations Lead\nBody A has different requirements for this role. " * 10,
    )

    _resolve(
        durable_database,
        tmp_path,
        workspace_id,
        newer_id,
        newer_url,
        b"Operations Lead\nBody B has different requirements for this role. " * 10,
    )

    with durable_database.session() as session:
        newer = session.get(PendingDiscovery, newer_id)
        assert newer is not None
        assert newer.regional_variant_of is None


def test_a_different_title_with_the_same_body_does_not_link(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, older_id, older_url = _pending(durable_database, tmp_path)
    newer_url = "https://example.test/jobs/platform-lead"
    newer_id = _add_pending(
        durable_database,
        workspace_id,
        employer="Acme",
        title="Platform Lead",
        first_seen_on=date(2026, 8, 29),
        url=newer_url,
    )
    body = b"Shared regional advertisement text with enough role detail to compare. " * 10
    _resolve(durable_database, tmp_path, workspace_id, older_id, older_url, body)

    _resolve(durable_database, tmp_path, workspace_id, newer_id, newer_url, body)

    with durable_database.session() as session:
        newer = session.get(PendingDiscovery, newer_id)
        assert newer is not None
        assert newer.regional_variant_of is None


def test_a_different_employer_with_the_same_body_does_not_link(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, older_id, older_url = _pending(durable_database, tmp_path)
    newer_url = "https://other.example.test/jobs/operations-lead"
    newer_id = _add_pending(
        durable_database,
        workspace_id,
        employer="Globex",
        title="Operations Lead",
        first_seen_on=date(2026, 8, 29),
        url=newer_url,
    )
    body = b"Shared regional advertisement text with enough role detail to compare. " * 10
    _resolve(durable_database, tmp_path, workspace_id, older_id, older_url, body)

    _resolve(durable_database, tmp_path, workspace_id, newer_id, newer_url, body)

    with durable_database.session() as session:
        newer = session.get(PendingDiscovery, newer_id)
        assert newer is not None
        assert newer.regional_variant_of is None


def test_a_row_already_carrying_regional_variant_of_is_not_relinked(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, primary_id, primary_url = _pending(durable_database, tmp_path)
    other_url = "https://example.test/jobs/operations-lead-other"
    other_id = _add_pending(
        durable_database,
        workspace_id,
        employer="Acme",
        title="Operations Lead",
        first_seen_on=date(2026, 8, 25),
        url=other_url,
    )
    matching_url = "https://example.test/jobs/operations-lead-matching"
    matching_id = _add_pending(
        durable_database,
        workspace_id,
        employer="Acme",
        title="Operations Lead",
        first_seen_on=date(2026, 8, 30),
        url=matching_url,
    )
    with durable_database.session() as session:
        matching = session.get(PendingDiscovery, matching_id)
        assert matching is not None
        matching.regional_variant_of = other_id

    body = b"Operations Lead\nBuild reliable systems for clinical product teams. " * 10
    _resolve(durable_database, tmp_path, workspace_id, primary_id, primary_url, body)

    _resolve(durable_database, tmp_path, workspace_id, matching_id, matching_url, body)

    with durable_database.session() as session:
        matching = session.get(PendingDiscovery, matching_id)
        assert matching is not None
        assert matching.regional_variant_of == other_id


def test_a_row_whose_candidate_primary_is_itself_a_variant_does_not_link_to_it(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, primary_id, primary_url = _pending(durable_database, tmp_path)
    body = b"Operations Lead\nBuild reliable systems for clinical product teams. " * 10
    _resolve(durable_database, tmp_path, workspace_id, primary_id, primary_url, body)

    variant_url = "https://example.test/jobs/operations-lead-variant"
    variant_id = _add_pending(
        durable_database,
        workspace_id,
        employer="Acme",
        title="Operations Lead",
        first_seen_on=date(2026, 8, 29),
        url=variant_url,
    )
    _resolve(durable_database, tmp_path, workspace_id, variant_id, variant_url, body)
    with durable_database.session() as session:
        variant = session.get(PendingDiscovery, variant_id)
        assert variant is not None
        assert variant.regional_variant_of == primary_id
        primary = session.get(PendingDiscovery, primary_id)
        assert primary is not None
        primary.archived_at = utc_now()

    latest_url = "https://example.test/jobs/operations-lead-latest"
    latest_id = _add_pending(
        durable_database,
        workspace_id,
        employer="Acme",
        title="Operations Lead",
        first_seen_on=date(2026, 8, 30),
        url=latest_url,
    )

    _resolve(durable_database, tmp_path, workspace_id, latest_id, latest_url, body)

    with durable_database.session() as session:
        latest = session.get(PendingDiscovery, latest_id)
        assert latest is not None
        assert latest.regional_variant_of is None


def test_a_withdrawn_body_match_leaves_an_active_discovery_in_pending(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, withdrawn_id, withdrawn_url = _pending(durable_database, tmp_path)
    active_url = "https://example.test/jobs/operations-lead-active"
    active_id = _add_pending(
        durable_database,
        workspace_id,
        employer="Acme",
        title="Operations Lead",
        first_seen_on=date(2026, 8, 29),
        url=active_url,
    )
    body = b"Operations Lead\nBuild reliable systems for clinical product teams. " * 10
    _resolve(durable_database, tmp_path, workspace_id, withdrawn_id, withdrawn_url, body)
    with durable_database.session() as session:
        withdrawn = session.get(PendingDiscovery, withdrawn_id)
        assert withdrawn is not None
        withdrawn.withdrawn_at = utc_now()

    _resolve(durable_database, tmp_path, workspace_id, active_id, active_url, body)

    with durable_database.session() as session:
        active = session.get(PendingDiscovery, active_id)
        assert active is not None
        assert active.regional_variant_of is None
    visible = SourceScanQueries(durable_database).list_pending_discoveries(workspace_id)
    assert {row.id for row in visible} == {active_id}


def test_a_primary_with_a_regional_variant_is_not_relinked_to_a_body_match(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, primary_id, primary_url = _pending(durable_database, tmp_path)
    variant_id = _add_pending(
        durable_database,
        workspace_id,
        employer="Acme",
        title="Operations Lead",
        first_seen_on=date(2026, 8, 29),
        url="https://example.test/jobs/operations-lead-variant",
    )
    earlier_url = "https://example.test/jobs/operations-lead-earlier"
    earlier_id = _add_pending(
        durable_database,
        workspace_id,
        employer="Acme",
        title="Operations Lead",
        first_seen_on=date(2026, 8, 27),
        url=earlier_url,
    )
    with durable_database.session() as session:
        variant = session.get(PendingDiscovery, variant_id)
        assert variant is not None
        variant.regional_variant_of = primary_id

    body = b"Operations Lead\nBuild reliable systems for clinical product teams. " * 10
    _resolve(durable_database, tmp_path, workspace_id, earlier_id, earlier_url, body)
    _resolve(durable_database, tmp_path, workspace_id, primary_id, primary_url, body)

    with durable_database.session() as session:
        primary = session.get(PendingDiscovery, primary_id)
        variant = session.get(PendingDiscovery, variant_id)
        assert primary is not None and variant is not None
        assert primary.regional_variant_of is None
        assert variant.regional_variant_of == primary_id
    assert {
        row.id for row in SourceScanQueries(durable_database).list_pending_discoveries(workspace_id)
    } == {earlier_id, primary_id}


def test_an_unverified_body_does_not_link_regional_variants(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, primary_id, primary_url = _pending(durable_database, tmp_path)
    sibling_url = "https://example.test/jobs/operations-lead-unverified"
    sibling_id = _add_pending(
        durable_database,
        workspace_id,
        employer="Acme",
        title="Operations Lead",
        first_seen_on=date(2026, 8, 29),
        url=sibling_url,
    )
    body = b"Operations Lead\nBuild reliable systems for clinical product teams. " * 10
    _resolve(durable_database, tmp_path, workspace_id, primary_id, primary_url, body)
    _resolve(
        durable_database,
        tmp_path,
        workspace_id,
        sibling_id,
        sibling_url,
        body,
        identity_status=PostingIdentityStatus.AMBIGUOUS,
    )

    with durable_database.session() as session:
        sibling = session.get(PendingDiscovery, sibling_id)
        assert sibling is not None
        assert sibling.regional_variant_of is None


def test_an_unverified_body_match_cannot_become_a_regional_primary(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, older_id, older_url = _pending(durable_database, tmp_path)
    newer_url = "https://example.test/jobs/operations-lead-verified"
    newer_id = _add_pending(
        durable_database,
        workspace_id,
        employer="Acme",
        title="Operations Lead",
        first_seen_on=date(2026, 8, 29),
        url=newer_url,
    )
    body = b"Operations Lead\nBuild reliable systems for clinical product teams. " * 10
    _resolve(
        durable_database,
        tmp_path,
        workspace_id,
        older_id,
        older_url,
        body,
        identity_status=PostingIdentityStatus.AMBIGUOUS,
    )
    _resolve(durable_database, tmp_path, workspace_id, newer_id, newer_url, body)

    with durable_database.session() as session:
        newer = session.get(PendingDiscovery, newer_id)
        assert newer is not None
        assert newer.regional_variant_of is None
    visible = SourceScanQueries(durable_database).list_pending_discoveries(workspace_id)
    assert {row.id for row in visible} == {older_id, newer_id}


def test_a_short_body_does_not_link_regional_variants(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, primary_id, primary_url = _pending(durable_database, tmp_path)
    sibling_url = "https://example.test/jobs/operations-lead-short"
    sibling_id = _add_pending(
        durable_database,
        workspace_id,
        employer="Acme",
        title="Operations Lead",
        first_seen_on=date(2026, 8, 29),
        url=sibling_url,
    )
    body = b"Operations Lead\nShort duplicate body."
    _resolve(durable_database, tmp_path, workspace_id, primary_id, primary_url, body)
    _resolve(durable_database, tmp_path, workspace_id, sibling_id, sibling_url, body)

    with durable_database.session() as session:
        sibling = session.get(PendingDiscovery, sibling_id)
        assert sibling is not None
        assert sibling.regional_variant_of is None


def test_a_pair_collapses_under_the_older_row_when_the_newer_is_read_again(
    durable_database: Database, tmp_path: Path
) -> None:
    """A row links only to a copy that sorts before it, so read order cannot pick the head."""
    workspace_id, older_id, older_url = _pending(durable_database, tmp_path)
    newer_url = "https://example.test/jobs/operations-lead-newer"
    newer_id = _add_pending(
        durable_database,
        workspace_id,
        employer="Acme",
        title="Operations Lead",
        first_seen_on=date(2026, 8, 29),
        url=newer_url,
    )
    body = b"Operations Lead\nBuild reliable systems for clinical product teams. " * 10
    _resolve(durable_database, tmp_path, workspace_id, newer_id, newer_url, body)
    _resolve(durable_database, tmp_path, workspace_id, older_id, older_url, body)

    visible = SourceScanQueries(durable_database).list_pending_discoveries(workspace_id)
    assert {row.id for row in visible} == {older_id, newer_id}

    _resolve(durable_database, tmp_path, workspace_id, newer_id, newer_url, body)

    visible = SourceScanQueries(durable_database).list_pending_discoveries(workspace_id)
    assert {row.id for row in visible} == {older_id}


_ROLE_INTRO = (
    "Globex runs the clinical operations platform that keeps trial sites, monitors, "
    "and sponsors working from one record. The team owns scheduling, site readiness, "
    "and the reporting that every side audits against at the end of a study."
)
_ROLE_DUTIES = (
    "You will own the operating cadence for a portfolio of sites, keep escalations "
    "moving to a decision, and work with engineering on the tooling that removes the "
    "manual steps this team repeats every single week of a study."
)
_SENIORITY_SENTENCE = "This opening sits at the principal level and reports to the director."
_ONSITE_SENTENCE = "Hybrid: three days a week, commuting to our closest office."

_HOME = JobSearchParameters(
    locations=[Location(key="home", country="Freedonia")],
    countries=[CountryNames(country="Freedonia"), CountryNames(country="Ruritania")],
)
_ABROAD = JobSearchParameters(
    locations=[Location(key="away", country="Ruritania")],
    countries=[CountryNames(country="Freedonia"), CountryNames(country="Ruritania")],
)
_REMOTE_ONLY = JobSearchParameters(
    locations=[Location(key="home", country="Freedonia", metro="Northport")],
    countries=[CountryNames(country="Freedonia")],
    arrangement=Arrangement(
        remote=Acceptance.PREFERRED, hybrid=Acceptance.REFUSED, on_site=Acceptance.REFUSED
    ),
)


def _city_posting(
    title: str,
    place: str,
    slug: str,
    *,
    first_seen_on: date,
    requisition_id: str = "",
    extra: str = "",
) -> RetainedPosting:
    """Build one city's copy of an opening, the city stated in the body and the listing."""
    body = f"{title}\n\nLocation: {place}\n\n{_ROLE_INTRO}\n\n{_ROLE_DUTIES}"
    return RetainedPosting(
        title=title,
        body=f"{body}\n\n{extra}" if extra else body,
        url=f"https://example.test/jobs/{slug}",
        location=place,
        requisition_id=requisition_id,
        places=(place,),
        first_seen_on=first_seen_on,
    )


def _family_heads(database: Database, workspace_id: str) -> dict[str, str | None]:
    """Return every row in one workspace with the regional-family head it hangs under."""
    with database.session() as session:
        return {
            row.id: row.regional_variant_of
            for row in session.scalars(
                select(PendingDiscovery).where(PendingDiscovery.workspace_id == workspace_id)
            )
        }


def _preparation_outcomes(
    database: Database, tmp_path: Path, workspace_id: str, ids: list[str]
) -> dict[str, str]:
    """Return each selected row's preparation outcome, the decision that spends a paid review."""
    _prepared, outcomes = RelevanceReviewPreparationService(
        database,
        storage=runtime_storage(database, tmp_path),
        fetcher=UnexpectedPostingFetcher(),
    ).prepare(workspace_id, ids, contract=current_review_contract())
    return {item.pending_discovery_id: item.outcome for item in outcomes}


def test_one_opening_advertised_per_city_stands_as_one_row(
    durable_database: Database, tmp_path: Path
) -> None:
    """Three copies of one opening differ only in their own city, so they fold into one row."""
    workspace_id, (north, remote, south) = workspace_with_retained_postings(
        durable_database,
        tmp_path,
        [
            _city_posting(
                "Operations Lead",
                "Northport, Freedonia",
                "ops-north",
                first_seen_on=date(2026, 9, 1),
            ),
            _city_posting(
                "Operations Lead", "Remote, Freedonia", "ops-remote", first_seen_on=date(2026, 9, 2)
            ),
            _city_posting(
                "Operations Lead",
                "Southport, Freedonia",
                "ops-south",
                first_seen_on=date(2026, 9, 3),
            ),
        ],
        parameters=_HOME,
        slug="city-fold",
    )

    visible = SourceScanQueries(durable_database).list_pending_discoveries(workspace_id)
    assert {row.id for row in visible} == {north}
    assert _family_heads(durable_database, workspace_id) == {
        north: None,
        remote: north,
        south: north,
    }


def test_two_copies_with_different_requisition_ids_do_not_fold(
    durable_database: Database, tmp_path: Path
) -> None:
    """A board that states a requisition id has already said which opening each copy is."""
    workspace_id, (first, second) = workspace_with_retained_postings(
        durable_database,
        tmp_path,
        [
            _city_posting(
                "Platform Engineer",
                "Northport, Freedonia",
                "platform-1001",
                first_seen_on=date(2026, 9, 1),
                requisition_id="REQ-1001",
            ),
            _city_posting(
                "Platform Engineer",
                "Northport, Freedonia",
                "platform-1002",
                first_seen_on=date(2026, 9, 2),
                requisition_id="REQ-1002",
            ),
        ],
        parameters=_HOME,
        slug="requisition-guard",
    )

    visible = SourceScanQueries(durable_database).list_pending_discoveries(workspace_id)
    assert {row.id for row in visible} == {first, second}
    assert _family_heads(durable_database, workspace_id) == {first: None, second: None}


_LEVER_JOB = "3c1d5e7f-2a4b-4c6d-8e0f-9a1b3c5d7e9f"
_LEVER_URL = f"https://jobs.lever.co/acme/{_LEVER_JOB}"
_CAREERS_URL = f"https://careers.acme.example/job/{_LEVER_JOB}/operations-lead/"
_NOT_THIS_LEVER_POSTING = [
    pytest.param(
        "https://jobs.lever.co/acme/5e6f7a8b-9c0d-4e1f-8a2b-3c4d5e6f7a8b/apply",
        id="another-lever-posting",
    ),
    pytest.param("https://careers.acme.example/apply", id="no-board-link"),
]


def _careers_page_row(database: Database, tmp_path: Path, workspace_id: str, apply_url: str) -> str:
    """Stage the careers-site copy of the opening and read its page, which links ``apply_url``."""
    pending_id = _add_pending(
        database,
        workspace_id,
        employer="Acme",
        title="Operations Lead",
        first_seen_on=date(2026, 8, 28),
        url=_CAREERS_URL,
    )
    page = f'<main><h1>Operations Lead</h1><a href="{apply_url}">Apply now</a></main>'
    service = PostingContentResolutionService(
        database,
        ScriptedResolver(
            [
                replace(
                    _readable(_CAREERS_URL),
                    source=PostingSource(publisher="page", page_html=page),
                )
            ]
        ),
        storage=runtime_storage(database, tmp_path),
    )
    service.resolve_batch(
        workspace_id,
        [pending_id],
        service.preflight(workspace_id, [pending_id]).snapshot_sha256,
        origin="discovery.refresh_batch",
    )
    return pending_id


def _stage_lever_listing(database: Database, tmp_path: Path, workspace_id: str) -> None:
    DiscoveryStaging(
        database,
        PostingRefreshService(
            database,
            storage=runtime_storage(database, tmp_path),
            fetcher=UnexpectedPostingFetcher(),
        ),
    ).stage_listings(
        workspace_id,
        [
            SourceListing(
                employer="Acme",
                title="Operations Lead",
                url=_LEVER_URL,
                requisition_id=_LEVER_JOB,
            )
        ],
        date(2026, 8, 29),
        reason="Board scan matched the title filter.",
    )


def _lever_row_head(database: Database, workspace_id: str) -> str | None:
    with database.session() as session:
        return session.scalar(
            select(PendingDiscovery.regional_variant_of).where(
                PendingDiscovery.workspace_id == workspace_id,
                PendingDiscovery.discovery_url == _LEVER_URL,
            )
        )


def test_a_lever_listing_folds_under_the_careers_page_that_links_it(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, _pending_id, _url = _pending(durable_database, tmp_path)
    careers_id = _careers_page_row(durable_database, tmp_path, workspace_id, f"{_LEVER_URL}/apply")

    _stage_lever_listing(durable_database, tmp_path, workspace_id)

    assert _lever_row_head(durable_database, workspace_id) == careers_id


@pytest.mark.parametrize("apply_url", _NOT_THIS_LEVER_POSTING)
def test_a_lever_listing_folds_under_a_careers_page_naming_its_uuid_whatever_the_page_links(
    durable_database: Database, tmp_path: Path, apply_url: str
) -> None:
    workspace_id, _pending_id, _url = _pending(durable_database, tmp_path)
    careers_id = _careers_page_row(durable_database, tmp_path, workspace_id, apply_url)

    _stage_lever_listing(durable_database, tmp_path, workspace_id)

    assert _lever_row_head(durable_database, workspace_id) == careers_id


def test_a_careers_page_that_links_a_tracked_lever_posting_names_that_posting(
    durable_database: Database, tmp_path: Path
) -> None:
    committed = workspace_with_committed_evaluation(
        durable_database,
        tmp_path,
        "lever-wrapper",
        employer="Acme",
        title="Operations Lead",
        requisition_id=_LEVER_JOB,
        canonical_url=_LEVER_URL,
        discovery_url=_LEVER_URL,
    )
    careers_id = _careers_page_row(
        durable_database, tmp_path, committed.workspace_id, f"{_LEVER_URL}/apply"
    )

    with durable_database.session() as session:
        careers = session.get(PendingDiscovery, careers_id)
        assert careers is not None
        tracked = evaluated_jobs_for_pending(session, committed.workspace_id, [careers])

    assert tracked[careers_id].job_posting_id == committed.job_posting_id


def test_two_copies_differing_only_by_a_linkedin_recruiter_tag_fold(
    durable_database: Database, tmp_path: Path
) -> None:
    """The recruiter tag rides on the copy a person applies through, not on the opening."""
    workspace_id, (remote, remote_us) = workspace_with_retained_postings(
        durable_database,
        tmp_path,
        [
            _city_posting(
                "Operations Lead",
                "Northport, Freedonia",
                "ops-li-remote",
                first_seen_on=date(2026, 9, 1),
                extra="#LI-Remote",
            ),
            _city_posting(
                "Operations Lead",
                "Northport, Freedonia",
                "ops-li-remote-us",
                first_seen_on=date(2026, 9, 2),
                extra="#LI-RemoteUS",
            ),
        ],
        parameters=_HOME,
        slug="recruiter-tag",
    )

    visible = SourceScanQueries(durable_database).list_pending_discoveries(workspace_id)
    assert {row.id for row in visible} == {remote}
    assert _family_heads(durable_database, workspace_id) == {remote: None, remote_us: remote}


def test_two_copies_whose_bodies_differ_beyond_the_city_do_not_fold(
    durable_database: Database, tmp_path: Path
) -> None:
    """One extra sentence of role text is a different opening, not another city of this one."""
    workspace_id, (plain, senior) = workspace_with_retained_postings(
        durable_database,
        tmp_path,
        [
            _city_posting(
                "Data Engineer", "Northport, Freedonia", "data-1", first_seen_on=date(2026, 9, 1)
            ),
            _city_posting(
                "Data Engineer",
                "Northport, Freedonia",
                "data-2",
                first_seen_on=date(2026, 9, 2),
                extra=_SENIORITY_SENTENCE,
            ),
        ],
        parameters=_HOME,
        slug="differing-body",
    )

    visible = SourceScanQueries(durable_database).list_pending_discoveries(workspace_id)
    assert {row.id for row in visible} == {plain, senior}
    assert _family_heads(durable_database, workspace_id) == {plain: None, senior: None}


def test_the_copy_the_person_accepts_heads_the_family(
    durable_database: Database, tmp_path: Path
) -> None:
    """A copy in a place the search refuses folds under the copy it accepts, not the other way."""
    outside = _city_posting(
        "Clinical Analyst",
        "Southport, Ruritania",
        "analyst-ruritania",
        first_seen_on=date(2026, 9, 1),
    )
    workspace_id, (refused, accepted) = workspace_with_retained_postings(
        durable_database,
        tmp_path,
        [
            outside,
            _city_posting(
                "Clinical Analyst",
                "Northport, Freedonia",
                "analyst-freedonia",
                first_seen_on=date(2026, 9, 5),
            ),
        ],
        parameters=_HOME,
        slug="accepted-head",
    )

    # The refused copy was read first, so it folds on its next body read.
    _resolve(
        durable_database,
        tmp_path,
        workspace_id,
        refused,
        outside.url,
        outside.body.encode("utf-8"),
    )

    visible = SourceScanQueries(durable_database).list_pending_discoveries(workspace_id)
    assert {row.id for row in visible} == {accepted}
    assert _family_heads(durable_database, workspace_id) == {accepted: None, refused: accepted}


def test_a_kept_copy_that_folds_on_its_body_hands_the_head_its_note(
    durable_database: Database, tmp_path: Path
) -> None:
    outside = _city_posting(
        "Clinical Analyst",
        "Southport, Ruritania",
        "kept-ruritania",
        first_seen_on=date(2026, 9, 1),
    )
    inside = _city_posting(
        "Clinical Analyst",
        "Northport, Freedonia",
        "kept-freedonia",
        first_seen_on=date(2026, 9, 5),
    )
    workspace_id, (kept, accepted) = workspace_with_retained_postings(
        durable_database,
        tmp_path,
        [outside, inside],
        parameters=_HOME,
        slug="kept-body-fold",
    )
    with durable_database.session() as session:
        set_pending_markers(
            session,
            workspace_id,
            kept,
            favorite=False,
            kept=True,
            keep_note="The team confirmed remote work.",
        )
    append_relevance_review(
        durable_database,
        workspace_id,
        accepted,
        body=inside.body,
        review=judged_relevance_review(accepted),
    )

    _resolve(
        durable_database,
        tmp_path,
        workspace_id,
        kept,
        outside.url,
        outside.body.encode("utf-8"),
    )

    visible = SourceScanQueries(durable_database).list_pending_discoveries(workspace_id)
    assert [(row.id, row.kept, row.keep_note) for row in visible] == [
        (accepted, True, "The team confirmed remote work.")
    ]
    with durable_database.session() as session:
        copy = session.get_one(PendingDiscovery, kept)
        assert (copy.regional_variant_of, copy.kept, copy.keep_note) == (accepted, False, None)
        review = RelevanceReviewQueries(durable_database).current_for_pending_ids(
            session, workspace_id, [accepted]
        )[accepted]
    assert review.out_of_date_because == ["keep_note"]


def test_a_copy_its_own_body_refuses_does_not_head_the_family(
    durable_database: Database, tmp_path: Path
) -> None:
    """The attendance a body states refuses a copy its staged facts alone let through."""
    place = "Northport, Freedonia"
    body = (
        f"Operations Lead\n\nLocation: {place}\n\n{_ONSITE_SENTENCE}\n\n"
        f"{_ROLE_INTRO}\n\n{_ROLE_DUTIES}"
    )
    workspace_id, (open_to_remote, onsite) = workspace_with_retained_postings(
        durable_database,
        tmp_path,
        [
            RetainedPosting(
                title="Operations Lead",
                body=body,
                url="https://example.test/jobs/ops-remote-offer",
                location=place,
                places=(place,),
                arrangements=("remote",),
                first_seen_on=date(2026, 9, 2),
            ),
            RetainedPosting(
                title="Operations Lead",
                body=body,
                url="https://example.test/jobs/ops-onsite",
                location=place,
                places=(place,),
                first_seen_on=date(2026, 9, 1),
            ),
        ],
        parameters=_REMOTE_ONLY,
        slug="body-refused-head",
    )

    visible = SourceScanQueries(durable_database).list_pending_discoveries(workspace_id)
    assert {row.id for row in visible} == {open_to_remote}
    assert _family_heads(durable_database, workspace_id) == {
        open_to_remote: None,
        onsite: open_to_remote,
    }


def test_confirming_a_contradicted_body_folds_it_under_its_family_head(
    durable_database: Database, tmp_path: Path
) -> None:
    """A person's identity confirmation is the read that lets a copy join its family."""
    workspace_id, head, head_url = _pending(durable_database, tmp_path)
    sibling_url = "https://example.test/jobs/operations-lead-confirmed"
    sibling = _add_pending(
        durable_database,
        workspace_id,
        employer="Acme",
        title="Operations Lead",
        first_seen_on=date(2026, 8, 29),
        url=sibling_url,
    )
    body = b"Operations Lead\nBuild reliable systems for clinical product teams. " * 10
    _resolve(durable_database, tmp_path, workspace_id, head, head_url, body)
    _resolve(
        durable_database,
        tmp_path,
        workspace_id,
        sibling,
        sibling_url,
        body,
        identity_status=PostingIdentityStatus.CONTRADICTED,
    )
    assert _family_heads(durable_database, workspace_id) == {head: None, sibling: None}
    with durable_database.session() as session:
        row = session.get(PendingDiscovery, sibling)
        assert row is not None
        current_content_id = row.current_content_id
    assert current_content_id is not None

    PostingContentResolutionService(
        durable_database, ScriptedResolver([]), storage=runtime_storage(durable_database, tmp_path)
    ).confirm_current_identity(
        workspace_id,
        sibling,
        attested_same_posting=True,
        expected_current_content_id=current_content_id,
        expected_url=sibling_url,
        correlation_id="request-confirm",
    )

    assert _family_heads(durable_database, workspace_id) == {head: None, sibling: head}


def test_a_regional_family_reaches_a_paid_relevance_review_once(
    durable_database: Database, tmp_path: Path
) -> None:
    """Every city copy is selected, and only the head is prepared for the paid review."""
    workspace_id, (north, remote, south) = workspace_with_retained_postings(
        durable_database,
        tmp_path,
        [
            _city_posting(
                "Operations Lead",
                "Northport, Freedonia",
                "ops-north",
                first_seen_on=date(2026, 9, 1),
            ),
            _city_posting(
                "Operations Lead", "Remote, Freedonia", "ops-remote", first_seen_on=date(2026, 9, 2)
            ),
            _city_posting(
                "Operations Lead",
                "Southport, Freedonia",
                "ops-south",
                first_seen_on=date(2026, 9, 3),
            ),
        ],
        parameters=_HOME,
        slug="reviewed-once",
    )

    outcomes = _preparation_outcomes(
        durable_database, tmp_path, workspace_id, [north, remote, south]
    )

    assert outcomes == {
        north: "readable",
        remote: "regional_variant",
        south: "regional_variant",
    }


def test_a_narrowed_search_prepares_the_family_copy_it_still_accepts(
    durable_database: Database, tmp_path: Path
) -> None:
    """A head the person's new places refuse no longer speaks for the copies under it."""
    workspace_id, (head, variant) = workspace_with_retained_postings(
        durable_database,
        tmp_path,
        [
            _city_posting(
                "Clinical Analyst",
                "Northport, Freedonia",
                "analyst-freedonia",
                first_seen_on=date(2026, 9, 1),
            ),
            _city_posting(
                "Clinical Analyst",
                "Southport, Ruritania",
                "analyst-ruritania",
                first_seen_on=date(2026, 9, 2),
            ),
        ],
        parameters=_HOME,
        slug="stale-head",
    )
    assert _family_heads(durable_database, workspace_id) == {head: None, variant: head}

    SearchParametersService(durable_database).save(workspace_id, _ABROAD)

    outcomes = _preparation_outcomes(durable_database, tmp_path, workspace_id, [head, variant])

    assert outcomes == {head: "deterministic_refuse", variant: "readable"}


def test_a_dismissed_body_match_leaves_an_active_discovery_in_pending(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, dismissed_id, dismissed_url = _pending(durable_database, tmp_path)
    active_url = "https://example.test/jobs/operations-lead-active"
    active_id = _add_pending(
        durable_database,
        workspace_id,
        employer="Acme",
        title="Operations Lead",
        first_seen_on=date(2026, 8, 29),
        url=active_url,
    )
    body = b"Operations Lead\nBuild reliable systems for clinical product teams. " * 10
    _resolve(durable_database, tmp_path, workspace_id, dismissed_id, dismissed_url, body)
    dismiss_pending_discovery(durable_database, workspace_id, dismissed_id)

    _resolve(durable_database, tmp_path, workspace_id, active_id, active_url, body)

    with durable_database.session() as session:
        active = session.get(PendingDiscovery, active_id)
        assert active is not None
        assert active.regional_variant_of is None
    visible = SourceScanQueries(durable_database).list_pending_discoveries(workspace_id)
    assert {row.id for row in visible} == {active_id}


def _readiness_inputs(
    *, requested_url: str = "", evidence_overrides: dict[str, object] | None = None
) -> tuple[PendingDiscovery, PendingPostingContent]:
    """Build one pending row and the evidence that is ready to evaluate unless overridden."""
    pending = pending_discovery(
        "workspace-readiness",
        employer="Northwind",
        created_at=datetime(2026, 8, 28, tzinfo=UTC),
        title="Operations Lead",
    )
    values: dict[str, object] = {
        "content_status": PostingContentAccessStatus.READABLE,
        "identity_status": PostingIdentityStatus.VERIFIED,
        "posting_status": PostingStatus.ACTIVE,
        "artifact_id": "artifact-readiness",
        "extraction_method": PostingContentExtractionMethod.STATIC_HTML,
        "content_sha256": "a" * 64,
    }
    values.update(evidence_overrides or {})
    evidence = pending_posting_content(
        pending.workspace_id,
        pending.id,
        requested_url or pending.canonical_url,
        **values,
    )
    return pending, evidence


def test_evidence_read_for_another_url_is_never_this_rows_readiness() -> None:
    """Otherwise a body read for a different posting would present as ready to evaluate."""
    pending, evidence = _readiness_inputs(requested_url="https://example.test/jobs/another-role")

    assert pending_discovery_readiness(pending, evidence) is PendingDiscoveryReadiness.URL_MISMATCH


def test_a_readable_body_nobody_verified_is_not_ready_to_evaluate() -> None:
    """An unattempted identity is its own answer, not a contradiction or an ambiguity."""
    pending, evidence = _readiness_inputs(
        evidence_overrides={"identity_status": PostingIdentityStatus.NOT_YET_VERIFIED}
    )

    assert pending_discovery_readiness(pending, evidence) is (
        PendingDiscoveryReadiness.IDENTITY_NOT_VERIFIED
    )


@pytest.mark.parametrize(
    "missing",
    [
        {"posting_status": None},
        {"posting_status": PostingStatus.CLOSED},
        {"artifact_id": None},
        {"extraction_method": None},
        {"content_sha256": None},
    ],
)
def test_verified_evidence_missing_any_required_part_is_incomplete(
    missing: dict[str, object],
) -> None:
    """Each part is required before evaluation, so any one of them absent reads the same."""
    pending, evidence = _readiness_inputs(evidence_overrides=missing)

    assert pending_discovery_readiness(pending, evidence) is (
        PendingDiscoveryReadiness.EVIDENCE_INCOMPLETE
    )


def test_the_readiness_vocabulary_is_the_closed_set_the_queue_may_report() -> None:
    """The SPA branches on these, so a member cannot quietly leave the set."""
    assert set(PendingDiscoveryReadiness) == {
        PendingDiscoveryReadiness.READY,
        PendingDiscoveryReadiness.SCREENING_REFUSED,
        PendingDiscoveryReadiness.NO_BODY,
        PendingDiscoveryReadiness.ACCESS_BLOCKED,
        PendingDiscoveryReadiness.FETCH_FAILED,
        PendingDiscoveryReadiness.RENDER_REQUIRED,
        PendingDiscoveryReadiness.SESSION_REQUIRED,
        PendingDiscoveryReadiness.POLICY_REFUSED,
        PendingDiscoveryReadiness.IDENTITY_NOT_VERIFIED,
        PendingDiscoveryReadiness.IDENTITY_AMBIGUOUS,
        PendingDiscoveryReadiness.IDENTITY_CONTRADICTED,
        PendingDiscoveryReadiness.URL_MISMATCH,
        PendingDiscoveryReadiness.EVIDENCE_INCOMPLETE,
        PendingDiscoveryReadiness.PARTIAL_TEXT,
        PendingDiscoveryReadiness.WITHDRAWN,
    }


def _named_by_read(
    database: Database,
    tmp_path: Path,
    *,
    url: str,
    employer: str,
    title: str,
    evidence: dict[str, Any],
    body: bytes = b"Operations Lead\nBuild reliable systems.",
    identity_status: PostingIdentityStatus = PostingIdentityStatus.VERIFIED,
    page_html: str = "",
    trust: ListingIdentityTrust = ListingIdentityTrust.PUBLISHER,
) -> tuple[str, str]:
    """Store one read of a row and return the employer and title it then carries."""
    workspace = WorkspaceService(database, runtime_storage(database, tmp_path)).create(
        slug="read-names", name="Read names"
    )
    pending_id = _add_pending(
        database,
        workspace.id,
        employer=employer,
        title=title,
        first_seen_on=date(2026, 8, 28),
        url=url,
    )
    with database.session() as session:
        row = session.get(PendingDiscovery, pending_id)
        assert row is not None
        row.staged_facts = StagedListingFacts(identity_trust=trust).as_json()
    read = replace(
        _readable(url, body, identity_status=identity_status),
        identity_evidence=evidence,
        source=PostingSource(publisher="page", page_html=page_html) if page_html else None,
    )
    service = PostingContentResolutionService(
        database, ScriptedResolver([read]), storage=runtime_storage(database, tmp_path)
    )
    service.resolve_batch(
        workspace.id,
        [pending_id],
        service.preflight(workspace.id, [pending_id]).snapshot_sha256,
        origin="discovery.refresh_batch",
    )
    with database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        return pending.employer, pending.title


def test_a_verified_read_names_a_row_added_without_a_title_or_employer(
    durable_database: Database, tmp_path: Path
) -> None:
    named = _named_by_read(
        durable_database,
        tmp_path,
        url="https://careers.example.test/jobs/operations-lead",
        employer="",
        title="",
        evidence={"publisher_title": "Operations Lead", "publisher_employer": "Globex"},
    )

    assert named == ("Globex", "Operations Lead")


def test_a_read_leaves_the_names_a_row_already_carries(
    durable_database: Database, tmp_path: Path
) -> None:
    named = _named_by_read(
        durable_database,
        tmp_path,
        url="https://careers.example.test/jobs/operations-lead",
        employer="Acme",
        title="Operations Lead, Platform",
        evidence={"publisher_title": "Operations Lead", "publisher_employer": "Globex"},
    )

    assert named == ("Acme", "Operations Lead, Platform")


def test_a_read_naming_only_its_own_host_names_no_employer(
    durable_database: Database, tmp_path: Path
) -> None:
    named = _named_by_read(
        durable_database,
        tmp_path,
        url="https://www.jobs-example.test/job/operations-lead",
        employer="",
        title="",
        evidence={"publisher_title": "Operations Lead", "publisher_employer": "jobs-example.test"},
    )

    assert named == ("", "Operations Lead")


@pytest.mark.parametrize(
    "identity_status", [PostingIdentityStatus.CONTRADICTED, PostingIdentityStatus.AMBIGUOUS]
)
def test_an_unverified_read_names_nothing(
    durable_database: Database, tmp_path: Path, identity_status: PostingIdentityStatus
) -> None:
    named = _named_by_read(
        durable_database,
        tmp_path,
        url="https://careers.example.test/jobs/operations-lead",
        employer="",
        title="",
        evidence={"publisher_title": "Operations Lead", "publisher_employer": "Globex"},
        identity_status=identity_status,
    )

    assert named == ("", "")


def test_a_read_leaves_a_name_only_the_board_address_gives_to_the_refresh(
    durable_database: Database, tmp_path: Path
) -> None:
    named = _named_by_read(
        durable_database,
        tmp_path,
        url="https://northwindhealth.bamboohr.com/careers/37",
        employer="",
        title="Operations Lead",
        evidence={"publisher_title": "Operations Lead", "publisher_employer": ""},
    )

    assert named == ("", "Operations Lead")


def test_a_read_names_a_board_tenant_as_its_posting_spells_it(
    durable_database: Database, tmp_path: Path
) -> None:
    named = _named_by_read(
        durable_database,
        tmp_path,
        url="https://northwindhealth.bamboohr.com/careers/37",
        employer="",
        title="Operations Lead",
        evidence={"publisher_title": "Operations Lead", "publisher_employer": ""},
        body=b"Operations Lead\nNorthwind Health builds reliable systems.",
    )

    assert named == ("Northwind Health", "Operations Lead")


_ACME_HEALTH_PAGE = "<html><head><title>Operations Lead | Acme Health</title></head></html>"


@pytest.mark.parametrize(
    ("trust", "employer"),
    [
        (ListingIdentityTrust.UNVERIFIED_HINT, "Acme Health"),
        (ListingIdentityTrust.PUBLISHER, ""),
    ],
)
def test_a_read_names_a_hinted_row_from_its_own_sites_page_title(
    durable_database: Database, tmp_path: Path, trust: ListingIdentityTrust, employer: str
) -> None:
    named = _named_by_read(
        durable_database,
        tmp_path,
        url="https://careers.acmehealth.test/jobs/operations-lead",
        employer="",
        title="Operations Lead",
        evidence={"publisher_title": "Operations Lead", "publisher_employer": ""},
        page_html=_ACME_HEALTH_PAGE,
        trust=trust,
    )

    assert named == (employer, "Operations Lead")


@pytest.mark.parametrize(
    ("provenance", "expected"),
    [
        ("source.registered_employer", ""),
        ("source.employer_hint", ""),
        ("successfactors.employer", "Acme Pharmaceuticals"),
    ],
)
def test_a_re_read_expects_only_an_employer_the_publisher_stated(
    durable_database: Database, tmp_path: Path, provenance: str, expected: str
) -> None:
    from clearscout.infrastructure.web_fetch.posting_resolution import _publisher_identity

    url = "https://jobs.acme.test/job/Ridgefield-Principal-Scientist/1435902733/"
    title = "Principal Scientist"
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database, tmp_path))
    workspace_id = workspace.create(slug="tenant-label", name="Tenant label").id
    pending_id = _add_pending(
        durable_database,
        workspace_id,
        employer="Acme Pharmaceuticals",
        title=title,
        first_seen_on=date(2026, 8, 28),
        url=url,
    )
    with durable_database.session() as session:
        row = session.get(PendingDiscovery, pending_id)
        assert row is not None
        row.staged_facts = StagedListingFacts(
            identity_trust=ListingIdentityTrust.PUBLISHER,
            employer=PublishedFact(
                values=("Acme Pharmaceuticals",), provenance=provenance, complete=True
            ),
        ).as_json()

    class TenantLabelResolver:
        # The publisher's markup names its tenant code, as SuccessFactors pages do.
        def __init__(self) -> None:
            self.employers: list[str] = []

        def resolve(self, url: str, *, employer: str, title: str, **_: object) -> Any:
            self.employers.append(employer)
            status = _publisher_identity(
                title, employer, "Principal Scientist", "AcmePRD", url, url
            )
            return _readable(url, identity_status=status)

    resolver = TenantLabelResolver()
    PostingContentResolutionService(
        durable_database, resolver, storage=runtime_storage(durable_database, tmp_path)
    ).resolve_automatic(workspace_id, [pending_id], origin="source.scan")

    assert resolver.employers == [expected]
    with durable_database.session() as session:
        row = session.get(PendingDiscovery, pending_id)
        assert row is not None
        content = session.get(PendingPostingContent, row.current_content_id)
        assert content is not None
        stated = provenance == "successfactors.employer"
        assert content.identity_status == (
            PostingIdentityStatus.CONTRADICTED.value
            if stated
            else PostingIdentityStatus.VERIFIED.value
        )


_FILLER = "Northwind Labs runs assay programs for clinical partners across three sites. " * 20


def _ending(total: int, last_line: str) -> str:
    """Return a body of exactly ``total`` characters whose last line is ``last_line``."""
    return f"{_FILLER[: total - len(last_line) - 1]}\n{last_line}"


_CUT_MID_WORD = _FILLER[:498] + "lab"
_CUT_ON_BULLET = _ending(480, "- Partner with laboratory operations")
_FULL_BODY = _ending(1100, "Apply through the Northwind Labs careers page.")


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        (_CUT_MID_WORD, True),
        (_CUT_ON_BULLET, True),
        (_ending(300, "Apply through our careers page."), False),
        (_ending(1100, "- Partner with laboratory operations"), False),
        (_ending(1013, "https://careers.northwind.example/jobs/42"), False),
        (_ending(500, 'Our motto: "Measure twice"'), False),
        (_ending(500, "Hybrid schedule (two days on site)"), False),
        (_ending(500, "- Partner with laboratory operations") + "\n\n  \n", True),
        (_ending(500, "Apply through our careers page.") + "\n\n  \n", False),
        (_ending(500, "Requirements:"), False),
        (_ending(500, "Benefits include:  "), False),
    ],
    ids=[
        "501-mid-word",
        "480-bullet",
        "300-period",
        "1100-bullet",
        "1013-url",
        "closing-quote",
        "closing-bracket",
        "trailing-blanks-bullet",
        "trailing-blanks-period",
        "colon",
        "colon-trailing-spaces",
    ],
)
def test_a_short_body_without_terminal_punctuation_reads_as_cut_off(
    text: str, flagged: bool
) -> None:
    assert retained_text_cut_off(text.encode("utf-8"), "static_html", frozenset()) is flagged


def _queue_readiness(
    database: Database, tmp_path: Path, workspace_id: str
) -> dict[str, PendingDiscoveryReadiness]:
    queries = SourceScanQueries(database, storage=runtime_storage(database, tmp_path))
    return {row.id: row.readiness for row in queries.list_pending_discoveries(workspace_id)}


def test_a_cut_off_retained_body_lists_as_partial_text_without_leaving_the_queue(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, (cut, bullet, full) = workspace_with_retained_postings(
        durable_database,
        tmp_path,
        [
            ("Assay Lead", _CUT_MID_WORD),
            ("Assay Scientist", _CUT_ON_BULLET),
            ("Assay Manager", _FULL_BODY),
        ],
        slug="cut-off-queue",
        employer="Northwind Labs",
    )

    assert _queue_readiness(durable_database, tmp_path, workspace_id) == {
        cut: PendingDiscoveryReadiness.PARTIAL_TEXT,
        bullet: PendingDiscoveryReadiness.PARTIAL_TEXT,
        full: PendingDiscoveryReadiness.READY,
    }
    single = SourceScanQueries(
        durable_database, storage=runtime_storage(durable_database, tmp_path)
    ).get_pending_discovery(workspace_id, cut)
    assert single.readiness is PendingDiscoveryReadiness.PARTIAL_TEXT
    with durable_database.session() as session:
        assert session.scalar(select(PendingDiscoveryDispositionEvent)) is None


def _too_short_review(pending_id: str) -> ValidatedRelevanceReview:
    return ValidatedRelevanceReview(
        pending_discovery_id=pending_id,
        outcome="insufficient_posting_text",
        recommendation=None,
        summary="The supplied text is a listing excerpt.",
        core_work_alignment=None,
        core_qualification_alignment=None,
        hard_condition=None,
        decisive_basis=None,
        posting_citations=[],
        master_cv_evidence_ids=[],
        unresolved_questions=[],
    )


def test_a_review_that_found_this_exact_body_too_short_lists_it_as_partial_text(
    durable_database: Database, tmp_path: Path
) -> None:
    other_body = _ending(1100, "Apply through the Northwind Labs hiring portal.")
    workspace_id, (same, different) = workspace_with_retained_postings(
        durable_database,
        tmp_path,
        [("Assay Lead", _FULL_BODY), ("Assay Manager", other_body)],
        slug="too-short-review",
        employer="Northwind Labs",
    )
    append_relevance_review(
        durable_database,
        workspace_id,
        same,
        body=_FULL_BODY,
        review=_too_short_review(same),
    )
    append_relevance_review(
        durable_database,
        workspace_id,
        different,
        body=_ending(1100, "An older excerpt of this posting."),
        review=_too_short_review(different),
    )

    assert _queue_readiness(durable_database, tmp_path, workspace_id) == {
        same: PendingDiscoveryReadiness.PARTIAL_TEXT,
        different: PendingDiscoveryReadiness.READY,
    }


def test_text_a_person_attached_is_never_read_as_cut_off(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, [pending_id] = workspace_with_retained_postings(
        durable_database,
        tmp_path,
        [("Assay Scientist", _CUT_MID_WORD)],
        slug="attached-cut-off",
        employer="Northwind Labs",
    )
    with durable_database.session() as session:
        pending = session.get_one(PendingDiscovery, pending_id)
        current_id, url = pending.current_content_id, pending.discovery_url
    service = PostingContentResolutionService(
        durable_database,
        DeterministicPostingContentResolver(),
        storage=runtime_storage(durable_database, tmp_path),
    )
    service.store_user_supplied_text(
        workspace_id,
        pending_id,
        _CUT_ON_BULLET,
        attested_current_exact_posting=True,
        expected_current_content_id=current_id,
        expected_url=url,
        correlation_id=None,
    )
    append_relevance_review(
        durable_database,
        workspace_id,
        pending_id,
        body=_CUT_ON_BULLET,
        review=_too_short_review(pending_id),
    )

    assert _queue_readiness(durable_database, tmp_path, workspace_id) == {
        pending_id: PendingDiscoveryReadiness.READY
    }
    assert not retained_text_cut_off(
        _CUT_ON_BULLET.encode("utf-8"),
        PostingContentExtractionMethod.USER_SUPPLIED_TEXT.value,
        frozenset(),
    )


def test_a_long_body_a_review_found_too_short_lists_and_evaluates_as_partial_text(
    durable_database: Database, tmp_path: Path
) -> None:
    long_body = (_FILLER * 3)[:3400] + "\nApply through the Northwind Labs careers page."
    workspace_id, (reviewed, sibling) = workspace_with_retained_postings(
        durable_database,
        tmp_path,
        [("Assay Lead", long_body), ("Assay Lead II", long_body)],
        slug="long-too-short",
        employer="Northwind Labs",
    )
    append_relevance_review(
        durable_database,
        workspace_id,
        reviewed,
        body=long_body,
        review=_too_short_review(reviewed),
    )

    assert _queue_readiness(durable_database, tmp_path, workspace_id) == {
        reviewed: PendingDiscoveryReadiness.PARTIAL_TEXT,
        sibling: PendingDiscoveryReadiness.PARTIAL_TEXT,
    }
    preflight = EvaluationWorkflowService(
        durable_database,
        runtime_storage(durable_database, tmp_path),
        fetcher=UnexpectedPostingFetcher(),
    ).preflight(
        workspace_id, DiscoveryEvaluateBatchPayload(pending_discovery_ids=[reviewed, sibling])
    )
    assert [item.current_skip_reason for item in preflight.items] == [CUT_OFF_SKIP_REASON] * 2


def test_a_long_body_its_reviewed_row_has_moved_past_still_lists_as_partial_text(
    durable_database: Database, tmp_path: Path
) -> None:
    long_body = (_FILLER * 3)[:3400] + "\nApply through the Northwind Labs careers page."
    workspace_id, (reviewed, sibling) = workspace_with_retained_postings(
        durable_database,
        tmp_path,
        [("Assay Lead", long_body), ("Assay Lead II", long_body)],
        slug="moved-too-short",
        employer="Northwind Labs",
    )
    PostingContentResolutionService(
        durable_database,
        DeterministicPostingContentResolver(),
        storage=runtime_storage(durable_database, tmp_path),
    ).store_prefetched_result(
        workspace_id,
        reviewed,
        _readable(
            "https://example.test/jobs/assay-lead",
            (long_body + "\nUpdated benefits apply from the first day.").encode("utf-8"),
        ),
        None,
        origin="test",
    )
    append_relevance_review(
        durable_database,
        workspace_id,
        reviewed,
        body=long_body,
        review=_too_short_review(reviewed),
    )

    assert _queue_readiness(durable_database, tmp_path, workspace_id)[sibling] == (
        PendingDiscoveryReadiness.PARTIAL_TEXT
    )
    preflight = EvaluationWorkflowService(
        durable_database,
        runtime_storage(durable_database, tmp_path),
        fetcher=UnexpectedPostingFetcher(),
    ).preflight(workspace_id, DiscoveryEvaluateBatchPayload(pending_discovery_ids=[sibling]))
    assert [item.current_skip_reason for item in preflight.items] == [CUT_OFF_SKIP_REASON]


@pytest.mark.parametrize(
    ("too_short_at", "judged_at", "expected"),
    [
        (datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 2, tzinfo=UTC), "ready"),
        (datetime(2026, 9, 2, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC), "partial_text"),
    ],
    ids=["judged-later", "too-short-later"],
)
def test_only_the_latest_review_of_a_body_can_mark_it_cut_off(
    durable_database: Database,
    tmp_path: Path,
    too_short_at: datetime,
    judged_at: datetime,
    expected: str,
) -> None:
    long_body = (_FILLER * 3)[:3400] + "\nApply through the Northwind Labs careers page."
    workspace_id, [pending_id] = workspace_with_retained_postings(
        durable_database,
        tmp_path,
        [("Assay Lead", long_body)],
        slug="superseded-too-short",
        employer="Northwind Labs",
    )
    append_relevance_review(
        durable_database,
        workspace_id,
        pending_id,
        body=long_body,
        review=_too_short_review(pending_id),
    )
    append_relevance_review(
        durable_database,
        workspace_id,
        pending_id,
        body=long_body,
        review=judged_relevance_review(pending_id, recommendation="investigate"),
    )
    with durable_database.session() as session:
        for review in session.scalars(select(RelevanceReview)):
            review.reviewed_at = too_short_at if review.recommendation is None else judged_at

    assert _queue_readiness(durable_database, tmp_path, workspace_id) == {
        pending_id: PendingDiscoveryReadiness(expected)
    }


def test_a_screening_refusal_outranks_cut_off_text_in_the_queue(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, [pending_id] = workspace_with_retained_postings(
        durable_database,
        tmp_path,
        [("Assay Director", _CUT_MID_WORD)],
        parameters=JobSearchParameters(seniority=Seniority(excluded_titles=["Director"])),
        slug="refused-cut-off",
        employer="Northwind Labs",
    )

    with durable_database.session() as session:
        pending = session.get_one(PendingDiscovery, pending_id)
        pending.staged_facts = StagedListingFacts(
            identity_trust=ListingIdentityTrust.PUBLISHER
        ).as_json()
    assert _queue_readiness(durable_database, tmp_path, workspace_id) == {
        pending_id: PendingDiscoveryReadiness.SCREENING_REFUSED
    }
