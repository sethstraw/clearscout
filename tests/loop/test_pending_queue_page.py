"""Bounded reads behind one page of the pending discovery queue."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from clearscout.application.discovery.board_scan import SourceScanService
from clearscout.application.discovery.content_resolution import (
    PostingContentResolutionService,
    ResolvedPostingContent,
)
from clearscout.application.discovery.pending import set_pending_markers
from clearscout.application.discovery.posting_text import publisher_closing_date
from clearscout.application.discovery.relevance_disputes import RelevanceDisputeService
from clearscout.application.discovery.relevance_review_rejections import (
    RelevanceReviewItemFailure,
    RelevanceReviewRejectionService,
)
from clearscout.application.discovery.scan_queries import (
    PENDING_PAGE_MAX_LIMIT,
    PendingQueueFilters,
    SourceScanQueries,
    projection,
)
from clearscout.application.discovery.screening import (
    ConstraintConfidence,
    DiscoveryScreening,
    ScreeningCode,
    ScreeningDimension,
    ScreeningFinding,
    ScreeningStanding,
    screen_discovery,
)
from clearscout.application.discovery.source_reading import SourceListing
from clearscout.application.failures import ConflictError, NotFoundError, ValidationError
from clearscout.application.job_search.evaluation_targets import remember_posting_url
from clearscout.application.job_search.models import (
    PendingDiscoveryPageResponse,
    PendingDiscoveryResponse,
)
from clearscout.application.tasks import TaskQueueService
from clearscout.application.workspaces import SearchParametersService
from clearscout.application.workspaces.records.access import audit_columns
from clearscout.domain import (
    EvaluationDecision,
    PendingDiscoveryReadiness,
    PostingContentAccessStatus,
    PostingContentExtractionMethod,
    PostingIdentityStatus,
    PostingStatus,
    new_id,
    utc_now,
)
from clearscout.domain.identity import requisition_key
from clearscout.domain.search_parameters import (
    Acceptance,
    Arrangement,
    Cluster,
    Employers,
    Focus,
    JobSearchParameters,
    Location,
    Seniority,
)
from clearscout.domain.staged_facts import PublishedFact, StagedListingFacts
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    Employer,
    JobEvaluation,
    JobPosting,
    PendingDiscovery,
    PendingPostingContent,
    PostingIdentifier,
    PostingRefusal,
    RelevanceReview,
)
from tests.support.artifacts import runtime_storage
from tests.support.doubles import ScriptedBoards, UnexpectedPostingFetcher
from tests.support.scenarios import (
    board_source,
    employer_board,
    pending_posting_content,
    record_relevance_review,
    scan_workspace,
    seed_pending_discoveries,
    seed_pending_discovery,
    seed_unread_row,
    source_listing,
    state_target_titles,
    titles,
)


@pytest.mark.parametrize(
    ("staged_value", "staged_day", "current_value", "expected"),
    [
        ("2026-10-19", 8, "2026-10-21", "2026-10-21"),
        ("2026-10-19", 10, "2026-10-21", "2026-10-19"),
        ("", 10, "2026-10-21", None),
        ("2026-10-19", 8, None, "2026-10-19"),
        (None, 8, "", None),
    ],
)
def test_pending_dates_use_the_latest_observation_without_changing_the_detail_read_time(
    durable_database: Database,
    staged_value: str | None,
    staged_day: int,
    current_value: str | None,
    expected: str | None,
) -> None:
    workspace_id = scan_workspace(durable_database)
    pending_id = _pending(durable_database, workspace_id, "Acme", date(2026, 10, 8))
    read_at = datetime(2026, 10, 9, 12, tzinfo=UTC)
    url = "https://example.test/jobs/date"
    with durable_database.session() as session:
        row = session.get_one(PendingDiscovery, pending_id)
        row.closing_date_override = date(2026, 10, 23)
        row.closing_date_override_updated_at = read_at
        row.staged_facts = StagedListingFacts(
            closing_date=publisher_closing_date(
                "",
                valid_through=staged_value,
                source_url=url,
                observed_at=datetime(2026, 10, staged_day, 12, tzinfo=UTC),
            )
            if staged_value is not None
            else None
        ).as_json()
        content = pending_posting_content(
            workspace_id,
            pending_id,
            url,
            fetched_at=read_at,
            publisher_closing_date=publisher_closing_date(
                "", valid_through=current_value, source_url=url, observed_at=read_at
            ).as_json()
            if current_value is not None
            else None,
        )
        session.add(content)
        session.flush()
        row.current_content_id = content.id
    projected = SourceScanQueries(durable_database).get_pending_discovery(workspace_id, pending_id)
    assert projected.publisher_closing_date is not None
    assert projected.publisher_closing_date.normalized_value == expected
    assert projected.closing_date_override == date(2026, 10, 23)
    assert projected.current_content_observed_at == read_at


def test_whole_lane_comes_back_in_one_page_above_the_former_bound(
    durable_database: Database,
) -> None:
    """A lane of more than 250 rows is returned whole when the caller asks for all of it."""
    workspace_id = scan_workspace(durable_database)
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    now = utc_now()
    seed_pending_discoveries(
        durable_database,
        workspace_id,
        [f"Globex {index}" for index in range(260)],
        created_at=now,
    )

    page = SourceScanQueries(durable_database).pending_discovery_page(
        workspace_id, limit=PENDING_PAGE_MAX_LIMIT
    )

    assert page.attention.total == 260
    assert len(page.attention.items) == 260
    assert page.attention.has_more is False


def _identify_by_requisition(session: Session, posting: JobPosting, row: PendingDiscovery) -> None:
    session.add(
        PostingIdentifier(
            workspace_id=posting.workspace_id,
            job_posting_id=posting.id,
            identifier_type="requisition",
            identifier_value=requisition_key(row.employer, row.requisition_id),
        )
    )


def _identify_by_url(session: Session, posting: JobPosting, row: PendingDiscovery) -> None:
    row.requisition_id = ""
    remember_posting_url(
        session,
        posting,
        url=row.canonical_url,
        source_id=None,
        url_type="canonical",
        make_primary=True,
        actor="local_user",
        origin="test",
        correlation_id=None,
    )


@pytest.mark.parametrize(
    "identify", [_identify_by_requisition, _identify_by_url], ids=["requisition", "url"]
)
def test_a_page_row_carries_the_time_of_the_evaluation_it_already_names(
    durable_database: Database,
    identify: Callable[[Session, JobPosting, PendingDiscovery], None],
) -> None:
    """The row says when its posting was evaluated, not only which evaluation that was."""
    workspace_id = scan_workspace(durable_database)
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    now = utc_now()
    pending_id = seed_pending_discovery(
        durable_database, workspace_id, employer="Globex", created_at=now
    )
    evaluated_at = now - timedelta(days=2)
    with durable_database.session() as session:
        employer = Employer(workspace_id=workspace_id, name="Globex", normalized_name="globex")
        session.add(employer)
        session.flush()
        posting = JobPosting(
            workspace_id=workspace_id,
            employer_id=employer.id,
            title="Data Platform Lead",
            normalized_title="data platform lead",
            first_seen_at=now,
        )
        session.add(posting)
        session.flush()
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        identify(session, posting, pending)
        session.add(
            JobEvaluation(
                job_posting_id=posting.id,
                version_number=1,
                decision=EvaluationDecision.APPLY_NOW.value,
                rationale="Synthetic evaluation.",
                payload_sha256="b" * 64,
                evaluated_at=evaluated_at,
            )
        )

    page = SourceScanQueries(durable_database).pending_discovery_page(workspace_id)

    [row] = [item for item in page.attention.items if item.id == pending_id]
    assert row.evaluated_at == evaluated_at


def _read_verified_bodies(
    database: Database, workspace_id: str, pending_ids: list[str], tmp_path: Path
) -> None:
    """Retain a verified publisher body for each row."""
    now = utc_now()

    class ReadableResolver:
        def resolve(
            self, url: str, *, employer: str, title: str, **_kwargs: Any
        ) -> ResolvedPostingContent:
            return ResolvedPostingContent(
                requested_url=url,
                final_url=url,
                content_status=PostingContentAccessStatus.READABLE,
                identity_status=PostingIdentityStatus.VERIFIED,
                posting_status=PostingStatus.ACTIVE,
                extraction_method=PostingContentExtractionMethod.STATIC_HTML,
                fetched_at=now,
                content=f"{title}\nFull publisher posting body.".encode(),
                http_status=200,
                publisher_markup_present=False,
                identity_evidence={"heading": title, "employer": employer},
                note="Verified publisher body.",
            )

    PostingContentResolutionService(
        database,
        ReadableResolver(),
        storage=runtime_storage(database, tmp_path),
    ).resolve_automatic(workspace_id, pending_ids, origin="test")


def test_readable_refusals_have_their_own_attention_state_across_both_queue_pages(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _refusing_workspace(durable_database)
    source_id = employer_board(durable_database, workspace_id, "Northwind")
    now = utc_now()
    refused_ids = [
        seed_pending_discovery(
            durable_database,
            workspace_id,
            employer=f"Northwind {index}",
            source_id=source_id,
            created_at=now,
        )
        for index in range(7)
    ]
    allowed_ids = seed_pending_discoveries(
        durable_database, workspace_id, [f"Globex {index}" for index in range(116)], created_at=now
    )
    unread_ids = [
        seed_pending_discovery(
            durable_database,
            workspace_id,
            employer=f"Northwind unread {index}",
            source_id=source_id,
            created_at=now,
        )
        for index in range(53)
    ]
    _read_verified_bodies(durable_database, workspace_id, [*refused_ids, *allowed_ids], tmp_path)
    queries = SourceScanQueries(durable_database)
    first = queries.pending_discovery_page(workspace_id)

    assert first.queue_total == 176
    assert first.ready.total == 116
    assert first.attention.total == 60
    assert len(first.ready.items) == len(first.attention.items) == 50
    assert first.ready.has_more and first.attention.has_more
    readiness = {facet.value: facet for facet in first.facets.readiness}
    assert readiness[PendingDiscoveryReadiness.READY].holding == 116
    refused = readiness[PendingDiscoveryReadiness.SCREENING_REFUSED]
    assert refused.holding == refused.count == 7
    assert readiness[PendingDiscoveryReadiness.NO_BODY].holding == 53
    assert sum(facet.holding for facet in first.facets.readiness) == first.queue_total
    constraints = {facet.value: facet.count for facet in first.facets.constraint}
    assert constraints["not_excluded"] == 116
    assert constraints[ConstraintConfidence.EXCLUDED] == 60

    second = queries.pending_discovery_page(
        workspace_id,
        ready_offset=50,
        attention_offset=50,
        snapshot_before=first.snapshot_before,
        expected_snapshot_sha256=first.snapshot_sha256,
    )
    third = queries.pending_discovery_page(
        workspace_id,
        ready_offset=100,
        attention_offset=100,
        snapshot_before=first.snapshot_before,
        expected_snapshot_sha256=first.snapshot_sha256,
    )
    ready_items = [item for page in (first, second, third) for item in page.ready.items]
    attention_items = [item for page in (first, second, third) for item in page.attention.items]
    assert len(ready_items) == len({item.id for item in ready_items}) == 116
    assert {item.id for item in ready_items} == set(allowed_ids)
    assert len(attention_items) == len({item.id for item in attention_items}) == 60
    assert {item.id for item in attention_items} == set(refused_ids + unread_ids)
    assert not third.ready.has_more and not second.attention.has_more
    assert third.attention.items == []
    assert _page_ids(_filtered(durable_database, workspace_id, constraint="not_excluded")) <= set(
        allowed_ids
    )
    filtered = _filtered(
        durable_database, workspace_id, readiness=PendingDiscoveryReadiness.SCREENING_REFUSED
    )
    assert filtered.ready.total == 0
    assert {item.id for item in filtered.attention.items} == set(refused_ids)


def test_a_readable_refusal_keeps_its_body_and_reason_in_every_queue_read(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _refusing_workspace(durable_database)
    pending_id = seed_pending_discovery(
        durable_database,
        workspace_id,
        employer="Northwind",
        source_id=employer_board(durable_database, workspace_id, "Northwind"),
        created_at=utc_now(),
    )
    _read_verified_bodies(durable_database, workspace_id, [pending_id], tmp_path)
    queries = SourceScanQueries(durable_database)
    page = queries.pending_discovery_page(workspace_id)

    assert page.ready.total == 0
    detail = queries.get_pending_discovery(workspace_id, pending_id)
    listed = queries.list_pending_discoveries(workspace_id)
    for item in [*page.attention.items, *listed, detail]:
        assert item.readiness == "screening_refused"
        assert item.screening_standing is ScreeningStanding.REFUSE
        assert item.constraint_confidence is ConstraintConfidence.EXCLUDED
        assert "The employer is excluded by the current settings." in item.recommendation_because
        assert item.current_content_id == item.retained_content_id == detail.current_content_id
        assert item.retained_content_artifact_id == detail.retained_content_artifact_id
        assert item.retained_content_artifact_id is not None
        assert item.content_status is PostingContentAccessStatus.READABLE
        assert not item.favorite and not item.kept
    with durable_database.session() as session:
        row = session.get_one(PendingDiscovery, pending_id)
        content = session.get_one(PendingPostingContent, row.current_content_id)
        assert row.withdrawn_at is None
        assert content.identity_status == PostingIdentityStatus.VERIFIED
        assert content.posting_status == PostingStatus.ACTIVE

    unread_id = seed_pending_discovery(
        durable_database, workspace_id, employer="Globex", created_at=utc_now()
    )
    cohort = queries.list_pending_discoveries(workspace_id, ready_limit=1, attention_limit=1)
    assert [item.id for item in cohort] == [unread_id]


@pytest.mark.parametrize("marker", ["favorite", "kept"])
def test_a_marked_readable_refusal_bypasses_filters_but_stays_in_attention(
    durable_database: Database, tmp_path: Path, marker: str
) -> None:
    workspace_id = _refusing_workspace(durable_database)
    pending_id = seed_pending_discovery(
        durable_database,
        workspace_id,
        employer="Northwind",
        source_id=employer_board(durable_database, workspace_id, "Northwind"),
        created_at=utc_now(),
    )
    _read_verified_bodies(durable_database, workspace_id, [pending_id], tmp_path)
    with durable_database.session() as session:
        set_pending_markers(
            session,
            workspace_id,
            pending_id,
            favorite=marker == "favorite",
            kept=marker == "kept",
            keep_note=None,
        )
    page = _filtered(
        durable_database,
        workspace_id,
        readiness=PendingDiscoveryReadiness.READY,
        constraint="not_excluded",
        **{marker: True},
    )

    assert page.ready.total == 0
    assert [item.id for item in page.attention.items] == [pending_id]
    assert page.attention.items[0].readiness == "screening_refused"
    readiness = {facet.value: facet for facet in page.facets.readiness}
    assert readiness[PendingDiscoveryReadiness.READY].holding == 0
    assert readiness[PendingDiscoveryReadiness.SCREENING_REFUSED].holding == 1
    assert readiness[PendingDiscoveryReadiness.READY].count == 1


@pytest.mark.parametrize(
    "status",
    [
        status
        for status in PostingContentAccessStatus
        if status is not PostingContentAccessStatus.READABLE
    ],
)
def test_a_screening_refusal_keeps_its_content_failure_as_the_attention_state(
    durable_database: Database, status: PostingContentAccessStatus
) -> None:
    workspace_id = _refusing_workspace(durable_database)
    pending_id = seed_pending_discovery(
        durable_database,
        workspace_id,
        employer="Northwind",
        source_id=employer_board(durable_database, workspace_id, "Northwind"),
        created_at=utc_now(),
        content_status=status,
    )
    page = SourceScanQueries(durable_database).pending_discovery_page(workspace_id)

    assert page.ready.total == 0
    assert [item.id for item in page.attention.items] == [pending_id]
    item = page.attention.items[0]
    assert item.screening_standing is ScreeningStanding.REFUSE
    assert item.readiness == (
        PendingDiscoveryReadiness.NO_BODY
        if status is PostingContentAccessStatus.NOT_YET_READ
        else PendingDiscoveryReadiness(status.value)
    )
    refused = next(
        facet
        for facet in page.facets.readiness
        if facet.value is PendingDiscoveryReadiness.SCREENING_REFUSED
    )
    assert refused.holding == refused.count == 0


def test_a_screening_change_that_moves_a_readable_row_restarts_the_queue_snapshot(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = scan_workspace(durable_database)
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    pending_id = seed_pending_discovery(
        durable_database,
        workspace_id,
        employer="Northwind",
        source_id=employer_board(durable_database, workspace_id, "Northwind"),
        created_at=utc_now(),
    )
    _read_verified_bodies(durable_database, workspace_id, [pending_id], tmp_path)
    queries = SourceScanQueries(durable_database)
    first = queries.pending_discovery_page(workspace_id, limit=1)
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            seniority=Seniority(target_titles=titles("Data Platform Lead")),
            employers=Employers(excluded_employers=["Northwind"]),
        ),
    )

    with pytest.raises(ConflictError, match="restart from the first page"):
        queries.pending_discovery_page(
            workspace_id,
            limit=1,
            ready_offset=1,
            expected_snapshot_sha256=first.snapshot_sha256,
            snapshot_before=first.snapshot_before,
        )


def test_readiness_cohorts_list_ready_rows_first_each_within_its_own_bound(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = scan_workspace(durable_database)
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    now = utc_now()
    attention_ids = [
        seed_pending_discovery(
            durable_database, workspace_id, employer=f"Alpha {index}", created_at=now
        )
        for index in range(3)
    ]
    ready_ids = [
        seed_pending_discovery(
            durable_database, workspace_id, employer=f"Zeta {index}", created_at=now
        )
        for index in range(3)
    ]
    _read_verified_bodies(durable_database, workspace_id, ready_ids, tmp_path)

    listed = SourceScanQueries(durable_database).list_pending_discoveries(
        workspace_id, ready_limit=2, attention_limit=1
    )

    assert [item.id for item in listed] == [*ready_ids[:2], attention_ids[0]]
    assert [item.retained_content_id is not None for item in listed] == [True, True, False]


def test_a_ready_row_names_the_artifact_that_holds_its_retained_body(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = scan_workspace(durable_database)
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    now = utc_now()
    ready_id = seed_pending_discovery(
        durable_database, workspace_id, employer="Gamma", created_at=now
    )
    unread_id = seed_pending_discovery(
        durable_database, workspace_id, employer="Alpha", created_at=now
    )
    _read_verified_bodies(durable_database, workspace_id, [ready_id], tmp_path)

    listed = {
        item.id: item
        for item in SourceScanQueries(durable_database).list_pending_discoveries(workspace_id)
    }

    with durable_database.session() as session:
        retained = session.get(PendingPostingContent, listed[ready_id].retained_content_id)
        assert retained is not None and retained.artifact_id is not None
        artifact_id = retained.artifact_id
    assert listed[ready_id].retained_content_artifact_id == artifact_id
    assert listed[unread_id].retained_content_artifact_id is None


def test_each_row_names_the_countries_its_location_states_or_suggests(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    places = {"Berlin, Germany": ["Germany"], "Boston": ["United States"], "Remote": []}
    ids = {
        seed_unread_row(
            durable_database,
            workspace_id,
            employer=f"Example {index}",
            title="Data Platform Lead",
            url=f"https://example.test/jobs/{index}",
            location=location,
        ): location
        for index, location in enumerate(places)
    }

    listed = SourceScanQueries(durable_database).list_pending_discoveries(workspace_id)

    assert {ids[item.id]: item.countries for item in listed} == places


def test_closed_history_names_the_withdrawn_regions_of_a_withdrawn_opening(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    withdrawn_at = utc_now()
    root_id, variant_id = (
        seed_pending_discovery(
            durable_database,
            workspace_id,
            employer="Globex",
            created_at=withdrawn_at,
            withdrawn_at=withdrawn_at,
        )
        for _ in range(2)
    )
    with durable_database.session() as session:
        variant = session.get(PendingDiscovery, variant_id)
        assert variant is not None
        variant.regional_variant_of = root_id
        variant.location = "Northtown"

    [closed] = SourceScanQueries(durable_database).list_pending_discoveries(
        workspace_id, closed_only=True
    )

    assert closed.id == root_id
    assert closed.also_in == ["Northtown"]


def test_one_page_compares_rows_against_every_root_once_for_both_lanes(
    durable_database: Database, tmp_path: Path
) -> None:
    """Ready and attention lanes share one duplicate-suggestion scan rather than one apiece."""
    workspace_id = scan_workspace(durable_database)
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    now = utc_now()
    ready_ids = [
        seed_pending_discovery(
            durable_database, workspace_id, employer=f"Globex {index}", created_at=now
        )
        for index in range(3)
    ]
    for index in range(3):
        seed_pending_discovery(
            durable_database, workspace_id, employer=f"Initech {index}", created_at=now
        )
    _read_verified_bodies(durable_database, workspace_id, ready_ids, tmp_path)

    statements: list[str] = []

    def count_statement(*args: object) -> None:
        statements.append(str(args[2]))

    event.listen(durable_database.engine, "before_cursor_execute", count_statement)
    try:
        page = SourceScanQueries(durable_database).pending_discovery_page(workspace_id, limit=2)
    finally:
        event.remove(durable_database.engine, "before_cursor_execute", count_statement)

    assert len(page.ready.items) == 2
    assert len(page.attention.items) == 2
    # Comparing against every root is the only read joining sources to relevance reviews.
    root_scans = [
        statement
        for statement in statements
        if "relevance_reviews" in statement and "sources" in statement
    ]
    assert len(root_scans) == 1


def test_a_skip_phrase_counts_only_the_rows_still_visible_in_the_queue(
    durable_database: Database,
) -> None:
    """A withdrawn row is already out of the queue, so the collateral count leaves it out."""
    workspace_id = scan_workspace(durable_database)
    now = utc_now()
    seed_pending_discovery(
        durable_database, workspace_id, employer="Globex", created_at=now, title="Delivery Lead"
    )
    seed_pending_discovery(
        durable_database, workspace_id, employer="Initech", created_at=now, title="delivery lead"
    )
    seed_pending_discovery(
        durable_database,
        workspace_id,
        employer="Initrode",
        created_at=now,
        title="Delivery Lead",
        withdrawn_at=now,
    )

    collateral = SourceScanQueries(durable_database).title_exclusion_collateral(
        workspace_id, ["Delivery Lead"]
    )

    assert collateral.queue_count == 2
    assert [(row.phrase, row.matched_count) for row in collateral.phrases] == [("Delivery Lead", 2)]


def test_a_skip_phrase_counts_a_whole_word_rather_than_a_longer_word_holding_it(
    durable_database: Database,
) -> None:
    """The count follows the shipped exclusion rule, which matches on word boundaries."""
    workspace_id = scan_workspace(durable_database)
    now = utc_now()
    seed_pending_discovery(
        durable_database, workspace_id, employer="Globex", created_at=now, title="Delivery Lead"
    )
    seed_pending_discovery(
        durable_database,
        workspace_id,
        employer="Initech",
        created_at=now,
        title="Leadership Development Partner",
    )

    collateral = SourceScanQueries(durable_database).title_exclusion_collateral(
        workspace_id, ["Lead"]
    )

    assert collateral.queue_count == 2
    assert [(row.phrase, row.matched_count) for row in collateral.phrases] == [("Lead", 1)]


def _excluded_board_row(database: Database, *, posting_employer: str) -> tuple[str, str]:
    """Stage a row read from a board registered to Globex, an excluded employer."""
    workspace_id = scan_workspace(database)
    SearchParametersService(database).save(
        workspace_id,
        JobSearchParameters(
            seniority=Seniority(target_titles=titles("Data Platform Lead")),
            employers=Employers(excluded_employers=["Globex"]),
        ),
    )
    board = employer_board(database, workspace_id, "Globex")
    pending_id = seed_pending_discovery(
        database, workspace_id, employer=posting_employer, created_at=utc_now(), source_id=board
    )
    return workspace_id, pending_id


def _page_row(
    queries: SourceScanQueries, workspace_id: str, pending_id: str
) -> PendingDiscoveryResponse:
    page = queries.pending_discovery_page(workspace_id)
    return next(item for item in page.attention.items if item.id == pending_id)


def _listed_row(
    queries: SourceScanQueries, workspace_id: str, pending_id: str
) -> PendingDiscoveryResponse:
    return next(
        item for item in queries.list_pending_discoveries(workspace_id) if item.id == pending_id
    )


def _one_row(
    queries: SourceScanQueries, workspace_id: str, pending_id: str
) -> PendingDiscoveryResponse:
    return queries.get_pending_discovery(workspace_id, pending_id)


@pytest.mark.parametrize("read_row", [_page_row, _listed_row, _one_row])
def test_a_row_on_an_excluded_employers_board_is_refused_and_names_that_board(
    durable_database: Database,
    read_row: Callable[[SourceScanQueries, str, str], PendingDiscoveryResponse],
) -> None:
    workspace_id, pending_id = _excluded_board_row(durable_database, posting_employer="Initech")

    row = read_row(SourceScanQueries(durable_database), workspace_id, pending_id)

    assert row.employer == "Initech"
    assert row.board_employer == "Globex"
    assert row.screening_standing is ScreeningStanding.REFUSE
    assert {
        (finding.code, finding.subject)
        for finding in row.screening_findings
        if finding.dimension is ScreeningDimension.EMPLOYER
    } == {(ScreeningCode.EMPLOYER_EXCLUDED, "Globex")}


def test_a_row_naming_its_boards_own_employer_carries_no_board_note(
    durable_database: Database,
) -> None:
    workspace_id, pending_id = _excluded_board_row(durable_database, posting_employer="GLOBEX")

    row = SourceScanQueries(durable_database).get_pending_discovery(workspace_id, pending_id)

    assert row.board_employer is None


def _reviewed_and_rejected_row(database: Database) -> tuple[str, str]:
    """Stage one visible discovery carrying both a stored review and a rejection mark."""
    workspace_id = scan_workspace(database)
    pending_id = seed_pending_discovery(
        database, workspace_id, employer="Globex", created_at=utc_now()
    )
    # The recorded review brings its own parameter set, so the workspace states no
    # target titles of its own here.
    record_relevance_review(database, workspace_id, pending_id)
    task_id = (
        TaskQueueService(database)
        .enqueue(
            workspace_id=workspace_id,
            task_type="discovery.relevance_review",
            payload={"pending_discovery_ids": [pending_id]},
            idempotency_key=f"queue-read-paths-{pending_id}",
        )
        .id
    )
    RelevanceReviewRejectionService(database).record(
        workspace_id=workspace_id,
        task_id=task_id,
        rejected=[
            RelevanceReviewItemFailure(
                pending_id, "missing_result", "Nothing came back for this discovery."
            )
        ],
    )
    return workspace_id, pending_id


@pytest.mark.parametrize("read_row", [_page_row, _listed_row, _one_row])
def test_every_read_path_carries_the_stored_review_and_the_rejection_mark(
    durable_database: Database,
    read_row: Callable[[SourceScanQueries, str, str], PendingDiscoveryResponse],
) -> None:
    """No caller can miss that a paid review of this row recorded nothing usable."""
    workspace_id, pending_id = _reviewed_and_rejected_row(durable_database)

    row = read_row(SourceScanQueries(durable_database), workspace_id, pending_id)

    assert row.relevance_review is not None
    assert row.relevance_review.recommendation == "unlikely"
    assert row.relevance_review_rejection is not None
    assert row.relevance_review_rejection.failure_code == "missing_result"


def test_the_list_resolves_the_relevance_facts_once_for_the_whole_page(
    durable_database: Database,
) -> None:
    """The route the SPA, the command line and the metrics surface share cannot scale per row."""
    workspace_id = scan_workspace(durable_database)
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    now = utc_now()
    for index in range(3):
        seed_pending_discovery(
            durable_database, workspace_id, employer=f"Globex {index}", created_at=now
        )

    statements: list[str] = []

    def count_statement(*args: object) -> None:
        statements.append(str(args[2]))

    event.listen(durable_database.engine, "before_cursor_execute", count_statement)
    try:
        listed = SourceScanQueries(durable_database).list_pending_discoveries(workspace_id)
    finally:
        event.remove(durable_database.engine, "before_cursor_execute", count_statement)

    assert len(listed) == 3
    assert sum("relevance_reviews" in statement for statement in statements) == 1
    assert sum("relevance_review_rejections" in statement for statement in statements) == 1


def _pending(
    database: Database,
    workspace_id: str,
    employer: str,
    first_seen_on: date,
    *,
    title: str = "Data Platform Lead",
) -> str:
    record_id = new_id()
    with database.session() as session:
        session.add(
            PendingDiscovery(
                id=record_id,
                workspace_id=workspace_id,
                fingerprint=new_id(),
                employer=employer,
                title=title,
                location="Remote",
                requisition_id="REQ-1",
                discovery_url=f"https://boards.greenhouse.io/{employer.lower()}/jobs/1",
                canonical_url=f"https://boards.greenhouse.io/{employer.lower()}/jobs/1",
                reason="Board scan matched the title filter.",
                first_seen_on=first_seen_on,
            )
        )
    return record_id


def _archive(database: Database, model: type[Any], record_id: str) -> None:
    with database.session() as session:
        record = session.get(model, record_id)
        assert record is not None
        record.archived_at = utc_now()


def test_the_review_queue_is_ordered_by_when_it_was_first_seen(
    durable_database: Database,
) -> None:
    """A discovery is not a job yet, so the queue says why it is still waiting."""
    workspace_id = scan_workspace(durable_database)
    today = utc_now().date()
    _pending(durable_database, workspace_id, "Alpha", today + timedelta(days=1))
    _pending(durable_database, workspace_id, "Initech", today)
    _pending(durable_database, workspace_id, "Globex", today, title="Staff Data Engineer")
    archived = _pending(durable_database, workspace_id, "Retired", today - timedelta(days=1))
    _archive(durable_database, PendingDiscovery, archived)

    queue = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).list_pending_discoveries(workspace_id)

    # Order by discovery time and employer; omit archived discoveries.
    assert [item.employer for item in queue] == ["Globex", "Initech", "Alpha"]
    first = queue[0]
    assert first.title == "Staff Data Engineer"
    assert first.location == "Remote"
    assert first.discovery_url == "https://boards.greenhouse.io/globex/jobs/1"
    assert first.canonical_url == "https://boards.greenhouse.io/globex/jobs/1"
    assert first.reason == "Board scan matched the title filter."
    assert first.first_seen_on == today


def test_a_scan_leaves_its_discoveries_in_the_review_queue(durable_database: Database) -> None:
    """The queue the SPA reads is the one the scan writes."""
    workspace_id = scan_workspace(durable_database)
    board_source(
        durable_database, workspace_id, "Globex Board", "https://boards.greenhouse.io/globex"
    )
    state_target_titles(durable_database, workspace_id, "data")
    boards = ScriptedBoards(
        {
            "Globex Board": [
                source_listing(
                    "Data Platform Lead",
                    "https://boards.greenhouse.io/globex/jobs/1",
                    employer="Globex",
                )
            ]
        }
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=boards,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.scan(workspace_id)

    queue = service.list_pending_discoveries(workspace_id)
    assert [(item.employer, item.title) for item in queue] == [("Globex", "Data Platform Lead")]
    assert queue[0].reason == (
        "Board scan of Globex Board retained a published listing for review."
    )


def test_the_review_queue_leads_with_the_discovery_worth_reading_first(
    durable_database: Database,
) -> None:
    """Both were staged by one sweep on one day, so only the rank separates them."""
    workspace_id = scan_workspace(durable_database)
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            seniority=Seniority(target_titles=titles("Data Platform Lead")),
            locations=[Location(key="ca", country="Canada")],
        ),
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.stage_listings(
        workspace_id,
        [
            source_listing(
                "Warehouse Supervisor", "https://boards.example.test/1", employer="Initech"
            ),
            source_listing(
                "Senior Data Platform Lead",
                "https://boards.example.test/2",
                employer="Globex",
                location="Toronto, Canada",
            ),
        ],
        date(2026, 8, 12),
        reason="staged together",
    )

    queue = service.list_pending_discoveries(workspace_id)

    assert [item.title for item in queue] == [
        "Senior Data Platform Lead",
        "Warehouse Supervisor",
    ]
    assert "the title answers Data Platform Lead" in queue[0].ranked_because
    assert queue[0].matched_target_title == "Data Platform Lead"
    assert queue[0].recommendation == "investigate"
    assert "does not resolve" in queue[0].recommendation_because
    assert queue[1].ranked_because == []
    assert queue[1].matched_target_title is None
    assert queue[1].recommendation == "investigate"
    assert queue[1].recommendation_because == "The title does not match a current target title."


def test_a_specialist_title_says_which_title_words_keep_it_for_review(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            seniority=Seniority(
                target_titles=titles("Logistics Planner"), specialist_ic_role_nouns=["Analyst"]
            ),
            focus=Focus(domains=[Cluster(name="data", terms=["clinical data"])]),
        ),
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.stage_listings(
        workspace_id,
        [source_listing("Clinical Data Analyst", "https://boards.example.test/1")],
        date(2026, 8, 12),
        reason="staged",
    )

    (queued,) = service.list_pending_discoveries(workspace_id)

    assert queued.recommendation_because == (
        "The title states analyst and clinical data, so it is kept for relevance review."
    )


def test_a_detail_read_says_the_posting_kept_a_bare_specialist_title(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            seniority=Seniority(
                target_titles=titles("Logistics Planner"), specialist_ic_role_nouns=["Analyst"]
            ),
            focus=Focus(domains=[Cluster(name="data", terms=["clinical data"])]),
        ),
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    stated = PublishedFact(values=("clinical data",), provenance="posting.summary", complete=True)
    service.stage_listings(
        workspace_id,
        [
            SourceListing(
                employer="Example",
                title="Analyst, Acceleration Team",
                url="https://boards.example.test/1",
                location="Remote",
                facts=StagedListingFacts(focus_terms=stated),
            )
        ],
        date(2026, 8, 12),
        reason="staged",
    )
    (queued,) = service.list_pending_discoveries(workspace_id)

    detail = SourceScanQueries(durable_database).get_pending_discovery(workspace_id, queued.id)

    assert detail.recommendation_because == (
        "The posting states clinical data, so this analyst title is kept for relevance review."
    )


def test_a_row_is_ranked_on_every_market_the_publisher_stated(
    durable_database: Database,
) -> None:
    """The stored display names one place, so ranking reads the retained set."""
    workspace_id = scan_workspace(durable_database)
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(locations=[Location(key="ca", country="Canada")]),
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.stage_listings(
        workspace_id,
        [
            SourceListing(
                employer="Globex",
                title="Data Platform Lead",
                url="https://boards.example.test/1",
                location="New York, United States",
                facts=StagedListingFacts(
                    locations=PublishedFact(
                        values=("New York, United States", "Toronto, Canada"),
                        provenance="greenhouse.offices",
                        complete=True,
                    )
                ),
            )
        ],
        date(2026, 8, 12),
        reason="staged together",
    )

    queue = service.list_pending_discoveries(workspace_id)

    assert queue[0].location == "New York, United States"
    assert queue[0].ranked_because == ["you may work in Canada already"]
    assert queue[0].rank == 30


def test_a_row_a_setting_already_refuses_sorts_behind_a_weaker_open_question(
    durable_database: Database,
) -> None:
    """The first read a person gets outranks the score, so a refused row cannot lead."""
    workspace_id = scan_workspace(durable_database)
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            seniority=Seniority(target_titles=titles("Data Platform Lead")),
            locations=[Location(key="ca", country="Canada")],
            employers=Employers(excluded_employers=["Globex"]),
        ),
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.stage_listings(
        workspace_id,
        [
            source_listing(
                "Senior Data Platform Lead",
                "https://boards.example.test/high-rank",
                employer="Globex",
                location="Toronto, Canada",
            ),
            source_listing(
                "Warehouse Supervisor", "https://boards.example.test/1", employer="Initech"
            ),
        ],
        date(2026, 8, 12),
        reason="staged from a board",
    )

    queue = service.list_pending_discoveries(workspace_id)

    assert [(item.title, item.recommendation) for item in queue] == [
        ("Warehouse Supervisor", "investigate"),
        ("Senior Data Platform Lead", "unlikely"),
    ]
    assert queue[1].rank > queue[0].rank


def test_the_queue_names_a_board_scan_a_campaign_and_a_persons_own_link(
    durable_database: Database,
) -> None:
    """A source, a campaign staging reason, or the person's own-link reason names the row.

    A posting the person added stays theirs after a tracked board lists it.
    """
    workspace_id = scan_workspace(durable_database)
    source_id = board_source(
        durable_database, workspace_id, "Globex Board", "https://boards.example.test/globex"
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.stage_listings(
        workspace_id,
        [
            source_listing(
                "Data Platform Lead",
                "https://boards.example.test/globex/1",
                employer="Globex",
            )
        ],
        date(2026, 8, 12),
        reason="staged from a board",
        source_id=source_id,
    )
    service.stage_found_posting(
        workspace_id,
        "https://jobs.lever.co/acme/0b6f3c2e-5a41-4d8e-9c7a-2f1e8d4b6a10",
        date(2026, 8, 13),
        title="Warehouse Supervisor",
    )
    service.stage_found_posting(
        workspace_id,
        "https://jobs.lever.co/initech/1f0a3c52-9d41-4b7e-8c6a-2b5d7e9f0a31",
        date(2026, 8, 14),
        title="Fleet Coordinator",
        origin="search.guided_campaign",
    )
    service.stage_listings(
        workspace_id,
        [
            source_listing(
                "Warehouse Supervisor",
                "https://jobs.lever.co/acme/0b6f3c2e-5a41-4d8e-9c7a-2f1e8d4b6a10",
                employer="Acme",
            )
        ],
        date(2026, 8, 15),
        reason="staged from a board",
        source_id=source_id,
    )

    queue = service.list_pending_discoveries(workspace_id)
    provenance = {item.title: item.staging_provenance for item in queue}

    assert provenance == {
        "Data Platform Lead": "board_scan",
        "Warehouse Supervisor": "person_added",
        "Fleet Coordinator": "guided_campaign",
    }


def test_the_queue_names_the_campaign_that_took_a_row_off_a_board_it_read(
    durable_database: Database,
) -> None:
    """The campaign's own board-listing reason names it even with no source and no record."""
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.staging.stage_found_posting(
        workspace_id,
        "https://jobs.lever.co/globex/6c1d4b90-7a2e-4f13-9d8c-0e5b7a2f6c31",
        date(2026, 8, 15),
        title="Fleet Coordinator",
        origin="search.guided_campaign",
        from_board_read=True,
    )

    queue = service.list_pending_discoveries(workspace_id)

    assert [item.staging_provenance for item in queue] == ["guided_campaign"]


def test_the_queue_admits_it_cannot_name_what_staged_a_row_with_no_stated_origin(
    durable_database: Database,
) -> None:
    """A reason no staging path writes leaves the queue with nothing to name."""
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.stage_listings(
        workspace_id,
        [
            source_listing(
                "Fleet Coordinator", "https://boards.example.test/globex/9", employer="Globex"
            )
        ],
        date(2026, 8, 15),
        reason="staged before the queue recorded origins",
    )

    queue = service.list_pending_discoveries(workspace_id)

    assert [item.staging_provenance for item in queue] == ["unrecorded"]


def test_reading_pending_discoveries_for_an_unknown_workspace_is_a_not_found(
    durable_database: Database,
) -> None:
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    with pytest.raises(NotFoundError, match="Unknown workspace"):
        service.list_pending_discoveries("no-such-workspace")


def test_the_review_queue_returns_no_more_than_it_was_asked_for(
    durable_database: Database,
) -> None:
    """The queue is append-only until somebody answers it, so the read is bounded."""
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.stage_listings(
        workspace_id,
        [
            SourceListing(
                employer="Globex",
                title=f"Data Engineer {index}",
                url=f"https://boards.greenhouse.io/globex/jobs/{index}",
            )
            for index in range(8)
        ],
        date(2026, 1, 2),
        reason="Staged for the bound.",
    )

    assert len(service.list_pending_discoveries(workspace_id, limit=3)) == 3
    assert len(service.list_pending_discoveries(workspace_id)) == 8
    with pytest.raises(ValidationError, match="between 1 and 250"):
        service.list_pending_discoveries(workspace_id, limit=0)


def test_ready_and_attention_rows_have_independent_queue_bounds(
    durable_database: Database, tmp_path: Any
) -> None:
    """Unread recovery work cannot hide the complete paid-review page."""
    workspace_id = scan_workspace(durable_database)
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    now = utc_now()
    seed_pending_discoveries(
        durable_database,
        workspace_id,
        [f"Attention {index:03d}" for index in range(253)],
        created_at=now,
    )
    ready_ids = [
        seed_pending_discovery(
            durable_database,
            workspace_id,
            employer=f"Zeta Ready {index}",
            created_at=now,
        )
        for index in range(3)
    ]

    class ReadableResolver:
        def resolve(
            self, url: str, *, employer: str, title: str, **_kwargs: Any
        ) -> ResolvedPostingContent:
            return ResolvedPostingContent(
                requested_url=url,
                final_url=url,
                content_status=PostingContentAccessStatus.READABLE,
                identity_status=PostingIdentityStatus.VERIFIED,
                posting_status=PostingStatus.ACTIVE,
                extraction_method=PostingContentExtractionMethod.STATIC_HTML,
                fetched_at=now,
                content=f"{title}\nFull publisher posting body.".encode(),
                http_status=200,
                publisher_markup_present=False,
                identity_evidence={"heading": title, "employer": employer},
                note="Verified publisher body.",
            )

    PostingContentResolutionService(
        durable_database,
        ReadableResolver(),
        storage=runtime_storage(durable_database, tmp_path),
    ).resolve_automatic(workspace_id, ready_ids, origin="test")
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    legacy_page = service.list_pending_discoveries(workspace_id, limit=250)
    cohort_page = service.list_pending_discoveries(
        workspace_id,
        limit=250,
        ready_limit=250,
        attention_limit=250,
    )
    bounded_page = service.pending_discovery_page(workspace_id, limit=2)
    next_ready_page = service.pending_discovery_page(
        workspace_id,
        limit=2,
        ready_offset=2,
        expected_snapshot_sha256=bounded_page.snapshot_sha256,
        snapshot_before=bounded_page.snapshot_before,
    )

    assert all(item.retained_content_id is None for item in legacy_page)
    assert len(cohort_page) == 253
    assert sum(item.retained_content_id is not None for item in cohort_page) == 3
    assert sum(item.retained_content_id is None for item in cohort_page) == 250
    assert bounded_page.ready.total == 3
    assert len(bounded_page.ready.items) == 2
    assert bounded_page.ready.has_more is True
    assert bounded_page.attention.total == 253
    assert len(bounded_page.attention.items) == 2
    assert bounded_page.attention.has_more is True
    assert len(next_ready_page.ready.items) == 1
    assert next_ready_page.ready.has_more is False
    with pytest.raises(ValidationError, match="must be supplied together"):
        service.list_pending_discoveries(workspace_id, ready_limit=250)


def test_pending_pages_choose_the_strongest_rows_before_applying_the_limit(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    now = utc_now()
    older_ids = [
        seed_pending_discovery(
            durable_database,
            workspace_id,
            employer=f"Older {index}",
            created_at=now - timedelta(days=10 - index),
        )
        for index in range(2)
    ]
    strongest_id = seed_pending_discovery(
        durable_database,
        workspace_id,
        employer="Aster Systems",
        created_at=now,
    )
    with durable_database.session() as session:
        sales = session.get(PendingDiscovery, older_ids[0])
        accountant = session.get(PendingDiscovery, older_ids[1])
        strongest = session.get(PendingDiscovery, strongest_id)
        assert sales is not None
        assert accountant is not None
        assert strongest is not None
        sales.title = "Sales Director"
        accountant.title = "Payroll Accountant"
        publisher_facts = {
            "schema_version": 3,
            "identity_trust": "publisher",
            "role_aliases": {"values": [], "source_text": ""},
            "locations": {"values": [], "source_text": ""},
            "arrangements": {"values": [], "source_text": ""},
            "employment_types": {"values": [], "source_text": ""},
            "compensation": {"values": [], "source_text": ""},
            "freshness": "unknown",
        }
        sales.staged_facts = publisher_facts
        accountant.staged_facts = publisher_facts
        strongest.staged_facts = publisher_facts

    page = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).pending_discovery_page(workspace_id, limit=2)

    assert strongest_id in {item.id for item in page.attention.items}


def test_pending_page_keeps_new_discoveries_out_of_an_active_paging_snapshot(
    durable_database: Database,
) -> None:
    """New scan rows do not restart a reader traversing an existing snapshot."""
    workspace_id = scan_workspace(durable_database)
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    now = utc_now()
    for index in range(3):
        seed_pending_discovery(
            durable_database,
            workspace_id,
            employer=f"Existing {index}",
            created_at=now - timedelta(days=index + 1),
        )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    first = service.pending_discovery_page(workspace_id, limit=2)
    arriving_id = seed_pending_discovery(
        durable_database,
        workspace_id,
        employer="Northwind Research",
        created_at=utc_now(),
    )

    second = service.pending_discovery_page(
        workspace_id,
        limit=2,
        attention_offset=2,
        expected_snapshot_sha256=first.snapshot_sha256,
        snapshot_before=first.snapshot_before,
    )
    assert second.snapshot_sha256 == first.snapshot_sha256
    assert second.snapshot_before == first.snapshot_before
    assert {item.id for item in second.attention.items}.isdisjoint({arriving_id})
    with pytest.raises(ValidationError, match="snapshot and boundary are required"):
        service.pending_discovery_page(workspace_id, limit=2, attention_offset=2)


def _filtered(
    database: Database, workspace_id: str, **filters: Any
) -> PendingDiscoveryPageResponse:
    return SourceScanQueries(database).pending_discovery_page(
        workspace_id, filters=PendingQueueFilters(**filters)
    )


def test_clarification_groups_count_all_content_states_and_page_without_overlap(
    durable_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Presentation groups retain each uncertain route without promoting marked exclusions."""
    workspace_id = scan_workspace(durable_database)
    routes = {
        "Globex": (ScreeningDimension.TITLE, ScreeningCode.TITLE_UNVERIFIED),
        "Initech": (ScreeningDimension.LOCATION, ScreeningCode.LOCATION_MISSING),
        "Umbrella": (ScreeningDimension.COMPENSATION, ScreeningCode.COMPENSATION_UNKNOWN),
    }

    def screening(employer: str, *_args: Any, **_kwargs: Any) -> DiscoveryScreening:
        if employer in routes:
            dimension, code = routes[employer]
            return DiscoveryScreening(
                (ScreeningFinding(dimension, ScreeningStanding.UNKNOWN, code),)
            )
        if employer == "Northwind":
            return DiscoveryScreening(
                (
                    ScreeningFinding(
                        ScreeningDimension.EMPLOYER,
                        ScreeningStanding.REFUSE,
                        ScreeningCode.EMPLOYER_EXCLUDED,
                    ),
                )
            )
        return DiscoveryScreening(())

    monkeypatch.setattr(projection, "screen_discovery", screening)
    now = utc_now()
    uncertain = [
        seed_pending_discovery(durable_database, workspace_id, employer=employer, created_at=now)
        for employer in routes
        for _index in range(23)
    ]
    clear = seed_pending_discovery(durable_database, workspace_id, employer="Acme", created_at=now)
    excluded = seed_pending_discovery(
        durable_database, workspace_id, employer="Northwind", created_at=now
    )
    _read_verified_bodies(durable_database, workspace_id, [uncertain[0], clear], tmp_path)
    with durable_database.session() as session:
        for pending_id in (uncertain[1], excluded):
            set_pending_markers(
                session, workspace_id, pending_id, favorite=True, kept=False, keep_note=None
            )

    queries = SourceScanQueries(durable_database)
    first = queries.pending_discovery_page(
        workspace_id, filters=PendingQueueFilters(constraint="needs_clarification"), limit=50
    )
    assert (first.ready.total, first.attention.total) == (1, 68)
    assert first.ready.items[0].id == uncertain[0]
    assert {item.value: item.count for item in first.facets.readiness}[
        PendingDiscoveryReadiness.READY
    ] == 1
    assert {item.value: item.count for item in first.facets.constraint}["needs_clarification"] == 69
    assert len(first.attention.items) == 50
    second = queries.pending_discovery_page(
        workspace_id,
        filters=PendingQueueFilters(constraint="needs_clarification"),
        limit=50,
        ready_offset=1,
        attention_offset=50,
        expected_snapshot_sha256=first.snapshot_sha256,
        snapshot_before=first.snapshot_before,
    )
    assert not (_page_ids(first) & _page_ids(second))
    assert _page_ids(first) | _page_ids(second) == set(uncertain)
    strict = _filtered(durable_database, workspace_id, constraint="pursue_only")
    assert _page_ids(strict) == {clear}
    ready = _filtered(
        durable_database,
        workspace_id,
        constraint="needs_clarification",
        readiness=PendingDiscoveryReadiness.READY,
    )
    assert _page_ids(ready) == {uncertain[0]}
    assert {item.value: item.count for item in ready.facets.constraint}["needs_clarification"] == 1
    for advertised in ready.facets.constraint:
        destination = _filtered(
            durable_database,
            workspace_id,
            constraint=advertised.value,
            readiness=PendingDiscoveryReadiness.READY,
        )
        assert advertised.count == destination.ready.total + destination.attention.total


def _page_ids(page: PendingDiscoveryPageResponse) -> set[str]:
    return {item.id for item in (*page.ready.items, *page.attention.items)}


def _refusing_workspace(database: Database) -> str:
    """A workspace refusing the Northwind and Acme boards, whose rows route as excluded."""
    workspace_id = scan_workspace(database)
    SearchParametersService(database).save(
        workspace_id,
        JobSearchParameters(
            seniority=Seniority(target_titles=titles("Data Platform Lead")),
            employers=Employers(excluded_employers=["Northwind", "Acme"]),
        ),
    )
    return workspace_id


def test_the_screened_out_group_does_not_count_unrelated_saved_picks(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _refusing_workspace(durable_database)
    source_id = employer_board(durable_database, workspace_id, "Northwind")
    refused = seed_pending_discovery(
        durable_database,
        workspace_id,
        employer="Northwind",
        source_id=source_id,
        created_at=utc_now(),
    )
    saved = seed_pending_discovery(
        durable_database, workspace_id, employer="Globex", created_at=utc_now()
    )
    _read_verified_bodies(durable_database, workspace_id, [refused, saved], tmp_path)
    with durable_database.session() as session:
        set_pending_markers(session, workspace_id, saved, favorite=True, kept=False, keep_note=None)
    page = SourceScanQueries(durable_database).pending_discovery_page(
        workspace_id, filters=PendingQueueFilters(constraint="excluded_only")
    )
    assert _page_ids(page) == {refused}
    assert page.ready.total == 0
    assert page.attention.total == 1
    assert next(f.count for f in page.facets.constraint if f.value == "excluded_only") == 1


@pytest.mark.parametrize("arrangement", ["hybrid", "onsite"])
def test_a_geographic_match_names_the_posting_place_without_claiming_remote_work(
    durable_database: Database, tmp_path: Path, arrangement: str
) -> None:
    workspace_id = scan_workspace(durable_database)
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            seniority=Seniority(target_titles=titles("Data Platform Lead")),
            locations=[Location(key="us_remote", country="United States")],
            arrangement=Arrangement(
                remote=Acceptance.PREFERRED, hybrid=Acceptance.REFUSED, on_site=Acceptance.REFUSED
            ),
        ),
    )
    pending_id = seed_pending_discovery(
        durable_database, workspace_id, employer="Globex", created_at=utc_now()
    )
    _read_verified_bodies(durable_database, workspace_id, [pending_id], tmp_path)
    with durable_database.session() as session:
        row = session.get_one(PendingDiscovery, pending_id)
        row.location = "Chicago, Illinois, United States"
        row.staged_facts = StagedListingFacts(
            locations=PublishedFact(
                values=(row.location,), provenance="fictional.location", complete=True
            ),
            arrangements=PublishedFact(
                values=(arrangement,), provenance="fictional.arrangement", complete=True
            ),
        ).as_json()
    page = SourceScanQueries(durable_database).pending_discovery_page(workspace_id, limit=50)
    [response] = page.attention.items
    location = next(
        f for f in response.screening_findings if f.dimension is ScreeningDimension.LOCATION
    )
    work = next(
        f for f in response.screening_findings if f.dimension is ScreeningDimension.ARRANGEMENT
    )
    assert location.standing is ScreeningStanding.MATCH
    assert (
        location.subject
        == "Chicago, Illinois, United States (geography only; work arrangement checked separately)"
    )
    assert work.standing is ScreeningStanding.REFUSE
    assert response.screening_standing is ScreeningStanding.REFUSE
    assert response.retained_content_id is not None


def test_search_narrows_employer_title_and_place_before_paging(
    durable_database: Database,
) -> None:
    """Totals and paging describe the matching rows, not the whole queue."""
    workspace_id = scan_workspace(durable_database)
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    now = utc_now()
    by_employer = seed_pending_discovery(
        durable_database, workspace_id, employer="Globex Analytics", created_at=now
    )
    by_title = seed_pending_discovery(
        durable_database, workspace_id, employer="Initech", created_at=now, title="Globex Lead"
    )
    by_place = seed_unread_row(
        durable_database,
        workspace_id,
        employer="Alpha",
        title="Data Platform Lead",
        url="https://jobs.example.test/alpha/1",
        location="Globex Park, Ohio",
    )
    seed_pending_discovery(durable_database, workspace_id, employer="Northwind", created_at=now)
    queries = SourceScanQueries(durable_database)
    filters = PendingQueueFilters(search="  gLOBEX ")

    first = queries.pending_discovery_page(workspace_id, limit=2, filters=filters)
    second = queries.pending_discovery_page(
        workspace_id,
        limit=2,
        attention_offset=2,
        expected_snapshot_sha256=first.snapshot_sha256,
        snapshot_before=first.snapshot_before,
        filters=filters,
    )

    assert first.queue_total == 4
    assert (first.attention.total, first.attention.has_more) == (3, True)
    assert _page_ids(first) | _page_ids(second) == {by_employer, by_title, by_place}


@pytest.mark.parametrize(
    ("search", "wanted", "unwanted"),
    [("100%", "100% Remote Co", "1000 Remote Co"), ("a_b", "a_b Labs", "axb Labs")],
)
def test_search_matches_like_wildcards_literally(
    durable_database: Database, search: str, wanted: str, unwanted: str
) -> None:
    workspace_id = scan_workspace(durable_database)
    now = utc_now()
    wanted_id = seed_pending_discovery(
        durable_database, workspace_id, employer=wanted, created_at=now
    )
    seed_pending_discovery(durable_database, workspace_id, employer=unwanted, created_at=now)

    page = _filtered(durable_database, workspace_id, search=search)

    assert _page_ids(page) == {wanted_id}


def test_the_employer_selection_matches_any_spelling_and_its_counts_ignore_it(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    now = utc_now()
    globex = [
        seed_pending_discovery(durable_database, workspace_id, employer=name, created_at=now)
        for name in ("Globex", "Globex", "GLOBEX ")
    ]
    seed_pending_discovery(durable_database, workspace_id, employer="Initech", created_at=now)

    page = _filtered(durable_database, workspace_id, employers=("globex",))
    searched = _filtered(durable_database, workspace_id, employers=("Initech",), search="glob")

    assert _page_ids(page) == set(globex)
    assert [(item.name, item.count) for item in page.employers] == [
        ("Globex", 3),
        ("Initech", 1),
    ]
    assert _page_ids(searched) == set()
    assert [(item.name, item.count) for item in searched.employers] == [("Globex", 3)]


def test_a_marked_row_skips_readiness_and_route_but_not_search_or_employer(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _refusing_workspace(durable_database)
    now = utc_now()
    ready = seed_pending_discovery(
        durable_database, workspace_id, employer="Globex", created_at=now
    )
    _read_verified_bodies(durable_database, workspace_id, [ready], tmp_path)
    unread_favorite = seed_pending_discovery(
        durable_database, workspace_id, employer="Initech", created_at=now
    )
    refused_kept = seed_pending_discovery(
        durable_database,
        workspace_id,
        employer="Northwind",
        created_at=now,
        source_id=employer_board(durable_database, workspace_id, "Northwind"),
    )
    seed_pending_discovery(durable_database, workspace_id, employer="Alpha", created_at=now)
    refused = seed_pending_discovery(
        durable_database,
        workspace_id,
        employer="Acme",
        created_at=now,
        source_id=employer_board(durable_database, workspace_id, "Acme"),
    )
    with durable_database.session() as session:
        set_pending_markers(
            session, workspace_id, unread_favorite, favorite=True, kept=False, keep_note=None
        )
        set_pending_markers(
            session, workspace_id, refused_kept, favorite=False, kept=True, keep_note=None
        )
    narrowing = {"readiness": PendingDiscoveryReadiness.READY, "constraint": "not_excluded"}

    page = _filtered(durable_database, workspace_id, **narrowing)
    searched = _filtered(durable_database, workspace_id, **narrowing, search="northwind")
    by_employer = _filtered(durable_database, workspace_id, **narrowing, employers=("Alpha",))
    routed = _page_ids(_filtered(durable_database, workspace_id, constraint="not_excluded"))

    assert refused_kept in routed
    assert refused not in routed
    assert [item.id for item in page.ready.items] == [ready]
    assert {item.id for item in page.attention.items} == {unread_favorite, refused_kept}
    assert _page_ids(searched) == {refused_kept}
    assert _page_ids(by_employer) == set()


def test_marker_chips_list_any_pressed_marker(durable_database: Database) -> None:
    workspace_id = scan_workspace(durable_database)
    now = utc_now()
    favorite, kept, _plain = (
        seed_pending_discovery(durable_database, workspace_id, employer=name, created_at=now)
        for name in ("Globex", "Initech", "Northwind")
    )
    with durable_database.session() as session:
        set_pending_markers(
            session, workspace_id, favorite, favorite=True, kept=False, keep_note=None
        )
        set_pending_markers(session, workspace_id, kept, favorite=False, kept=True, keep_note=None)

    assert _page_ids(_filtered(durable_database, workspace_id, favorite=True)) == {favorite}
    assert _page_ids(_filtered(durable_database, workspace_id, kept=True)) == {kept}
    both = _filtered(durable_database, workspace_id, favorite=True, kept=True)
    assert _page_ids(both) == {favorite, kept}
    assert (both.facets.markers.favorite, both.facets.markers.kept) == (1, 1)


def test_relevance_and_review_status_read_the_whole_queue(durable_database: Database) -> None:
    workspace_id = scan_workspace(durable_database)
    now = utc_now()
    reviewed = seed_pending_discovery(
        durable_database, workspace_id, employer="Globex", created_at=now
    )
    record_relevance_review(durable_database, workspace_id, reviewed)
    unreviewed = {
        seed_pending_discovery(durable_database, workspace_id, employer=name, created_at=now)
        for name in ("Initech", "Northwind", "Alpha")
    }
    [row] = _filtered(durable_database, workspace_id, relevance="unlikely").attention.items
    assert row.id == reviewed
    assert row.relevance_review is not None
    stale = row.relevance_review.out_of_date_because
    assert stale

    not_reviewed = _filtered(durable_database, workspace_id, relevance="not_reviewed")
    out_of_date = _filtered(durable_database, workspace_id, review_status="out_of_date")
    by_input = _filtered(durable_database, workspace_id, review_status=f"out_of_date_{stale[0]}")
    current = _filtered(durable_database, workspace_id, review_status="current")

    assert _page_ids(not_reviewed) == unreviewed
    assert _page_ids(out_of_date) == {reviewed}
    assert _page_ids(by_input) == {reviewed}
    assert _page_ids(current) == set()
    counts = {item.value: (item.count, item.out_of_date) for item in not_reviewed.facets.relevance}
    assert counts["unlikely"] == (1, 1)
    assert counts["not_reviewed"] == (3, 0)
    statuses = {item.value: item.count for item in current.facets.review_status}
    assert (statuses["out_of_date"], statuses["current"]) == (1, 0)
    assert current.facets.review_status_total == 4


def test_the_keep_note_review_status_follows_the_note_the_review_was_sent(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    reviewed = seed_pending_discovery(
        durable_database, workspace_id, employer="Globex", created_at=utc_now()
    )
    record_relevance_review(durable_database, workspace_id, reviewed, keep_note="Sent note.")

    def by_note_status(note: str) -> set[str]:
        with durable_database.session() as session:
            set_pending_markers(
                session, workspace_id, reviewed, favorite=False, kept=True, keep_note=note
            )
        return _page_ids(
            _filtered(durable_database, workspace_id, review_status="out_of_date_keep_note")
        )

    assert by_note_status("Sent note.") == set()
    assert by_note_status("Edited after the review.") == {reviewed}


def test_category_arrangement_and_origin_narrow_the_queue(durable_database: Database) -> None:
    workspace_id = _refusing_workspace(durable_database)
    now = utc_now()
    board = employer_board(durable_database, workspace_id, "Globex")
    scanned = seed_pending_discovery(
        durable_database, workspace_id, employer="Globex", created_at=now, source_id=board
    )
    refused = seed_pending_discovery(
        durable_database,
        workspace_id,
        employer="Northwind",
        created_at=now,
        source_id=employer_board(durable_database, workspace_id, "Northwind"),
    )
    on_site = seed_unread_row(
        durable_database,
        workspace_id,
        employer="Alpha",
        title="Data Platform Lead",
        url="https://jobs.example.test/alpha/on-site-1",
        location="Springfield, Ohio",
    )

    by_category = _filtered(durable_database, workspace_id, category="unlikely")
    by_origin = _filtered(durable_database, workspace_id, provenance="board_scan")
    remote = _filtered(durable_database, workspace_id, arrangement="remote")

    assert _page_ids(by_category) == {refused}
    assert _page_ids(by_origin) == {scanned, refused}
    assert on_site not in _page_ids(remote)
    assert {scanned, refused} <= _page_ids(remote)
    arrangement = {item.value: item.count for item in remote.facets.arrangement}
    assert remote.facets.arrangement_total == 3
    assert arrangement["remote"] == 2


def test_each_filter_count_ignores_its_own_selection(
    durable_database: Database, tmp_path: Path
) -> None:
    """The readiness counts behind the attention summary survive the default ready view."""
    workspace_id = _refusing_workspace(durable_database)
    now = utc_now()
    ready = seed_pending_discovery(
        durable_database, workspace_id, employer="Globex", created_at=now
    )
    _read_verified_bodies(durable_database, workspace_id, [ready], tmp_path)
    for name in ("Initech", "Alpha"):
        seed_pending_discovery(durable_database, workspace_id, employer=name, created_at=now)
    seed_pending_discovery(
        durable_database,
        workspace_id,
        employer="Acme",
        created_at=now,
        source_id=employer_board(durable_database, workspace_id, "Acme"),
    )

    page = _filtered(
        durable_database,
        workspace_id,
        readiness=PendingDiscoveryReadiness.READY,
        constraint="not_excluded",
    )

    assert page.queue_total == 4
    readiness = {item.value: item.count for item in page.facets.readiness}
    assert readiness[PendingDiscoveryReadiness.READY] == 1
    assert readiness[PendingDiscoveryReadiness.NO_BODY] == 2
    constraint = {item.value: item.count for item in page.facets.constraint}
    assert constraint[ConstraintConfidence.EXCLUDED] == 0
    assert (constraint["not_excluded"], page.facets.constraint_total) == (1, 1)


def test_a_readiness_or_route_count_is_the_number_of_rows_that_choice_lists(
    durable_database: Database, tmp_path: Path
) -> None:
    """A favorite or kept row passes readiness and route, so every such count includes it."""
    workspace_id = _refusing_workspace(durable_database)
    now = utc_now()
    ready = seed_pending_discovery(
        durable_database, workspace_id, employer="Globex", created_at=now
    )
    _read_verified_bodies(durable_database, workspace_id, [ready], tmp_path)
    unread_favorite = seed_pending_discovery(
        durable_database, workspace_id, employer="Initech", created_at=now
    )
    refused_kept = seed_pending_discovery(
        durable_database,
        workspace_id,
        employer="Northwind",
        created_at=now,
        source_id=employer_board(durable_database, workspace_id, "Northwind"),
    )
    seed_pending_discovery(durable_database, workspace_id, employer="Alpha", created_at=now)
    seed_pending_discovery(
        durable_database,
        workspace_id,
        employer="Acme",
        created_at=now,
        source_id=employer_board(durable_database, workspace_id, "Acme"),
    )
    with durable_database.session() as session:
        set_pending_markers(
            session, workspace_id, unread_favorite, favorite=True, kept=False, keep_note=None
        )
        set_pending_markers(
            session, workspace_id, refused_kept, favorite=False, kept=True, keep_note=None
        )

    facets = _filtered(durable_database, workspace_id).facets
    readiness = {item.value: item.count for item in facets.readiness}
    holding = {item.value: item.holding for item in facets.readiness}
    routes = {item.value: item.count for item in facets.constraint}
    listed = PendingDiscoveryReadiness.READY

    assert (readiness[listed], holding[listed], sum(holding.values())) == (3, 1, 5)
    assert (routes["not_excluded"], routes[ConstraintConfidence.EXCLUDED]) == (4, 3)
    assert facets.constraint_total == 5
    for value, count in readiness.items():
        assert len(_page_ids(_filtered(durable_database, workspace_id, readiness=value))) == count
    for route, count in routes.items():
        assert len(_page_ids(_filtered(durable_database, workspace_id, constraint=route))) == count


def test_the_excluded_route_lists_only_the_rows_a_rule_refuses(
    durable_database: Database,
) -> None:
    workspace_id = _refusing_workspace(durable_database)
    now = utc_now()
    refused = seed_pending_discovery(
        durable_database,
        workspace_id,
        employer="Acme",
        created_at=now,
        source_id=employer_board(durable_database, workspace_id, "Acme"),
    )
    seed_pending_discovery(durable_database, workspace_id, employer="Globex", created_at=now)

    page = _filtered(durable_database, workspace_id, constraint=ConstraintConfidence.EXCLUDED)

    assert _page_ids(page) == {refused}


def test_search_matches_an_accented_capital_from_a_lower_case_term(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    now = utc_now()
    accented = seed_pending_discovery(
        durable_database, workspace_id, employer="Élan Systems", created_at=now
    )
    seed_pending_discovery(durable_database, workspace_id, employer="Initech", created_at=now)

    page = _filtered(durable_database, workspace_id, search="élan")

    assert _page_ids(page) == {accented}


def test_a_head_lists_under_the_marker_its_family_member_carries(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    now = utc_now()
    head = seed_pending_discovery(durable_database, workspace_id, employer="Globex", created_at=now)
    member = seed_pending_discovery(
        durable_database, workspace_id, employer="Globex", created_at=now
    )
    seed_pending_discovery(durable_database, workspace_id, employer="Initech", created_at=now)
    with durable_database.session() as session:
        variant = session.get(PendingDiscovery, member)
        assert variant is not None
        variant.regional_variant_of = head
        variant.favorite = True

    page = _filtered(durable_database, workspace_id, favorite=True)

    assert [(item.id, item.favorite) for item in page.attention.items] == [(head, True)]
    assert page.facets.markers.favorite == 1


def test_relevance_lists_a_rejected_paid_review_and_a_disputed_verdict_apart(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    now = utc_now()
    rejected, disputed, _plain = (
        seed_pending_discovery(durable_database, workspace_id, employer=name, created_at=now)
        for name in ("Globex", "Initech", "Northwind")
    )
    task_id = (
        TaskQueueService(durable_database)
        .enqueue(
            workspace_id=workspace_id,
            task_type="discovery.relevance_review",
            payload={"pending_discovery_ids": [rejected]},
            idempotency_key=f"queue-relevance-keys-{rejected}",
        )
        .id
    )
    RelevanceReviewRejectionService(durable_database).record(
        workspace_id=workspace_id,
        task_id=task_id,
        rejected=[
            RelevanceReviewItemFailure(
                rejected, "missing_result", "Nothing came back for this discovery."
            )
        ],
    )
    record_relevance_review(durable_database, workspace_id, disputed)
    with durable_database.session() as session:
        review_id = session.scalars(
            select(RelevanceReview.id).where(RelevanceReview.pending_discovery_id == disputed)
        ).one()
    RelevanceDisputeService(durable_database).dispute(
        workspace_id=workspace_id,
        pending_discovery_id=disputed,
        relevance_review_id=review_id,
        note="The posting names the ledger work my CV records.",
        audit=audit_columns("local_user", "test", None),
    )

    by_rejection = _filtered(durable_database, workspace_id, relevance="review_rejected")
    by_dispute = _filtered(durable_database, workspace_id, relevance="disputed")

    assert _page_ids(by_rejection) == {rejected}
    assert _page_ids(by_dispute) == {disputed}
    counts = {item.value: item.count for item in by_dispute.facets.relevance}
    assert (counts["review_rejected"], counts["disputed"], counts["unlikely"]) == (1, 1, 0)


def test_the_origin_filter_lists_a_guided_campaigns_rows(durable_database: Database) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.stage_found_posting(
        workspace_id,
        "https://jobs.lever.co/acme/0b6f3c2e-5a41-4d8e-9c7a-2f1e8d4b6a10",
        date(2026, 8, 13),
        title="Warehouse Supervisor",
    )
    service.stage_found_posting(
        workspace_id,
        "https://jobs.lever.co/initech/1f0a3c52-9d41-4b7e-8c6a-2b5d7e9f0a31",
        date(2026, 8, 14),
        title="Fleet Coordinator",
        origin="search.guided_campaign",
    )

    page = _filtered(durable_database, workspace_id, provenance="guided_campaign")

    assert [item.title for item in (*page.ready.items, *page.attention.items)] == [
        "Fleet Coordinator"
    ]


def test_a_row_leaving_the_filtered_set_restarts_paging(durable_database: Database) -> None:
    workspace_id = scan_workspace(durable_database)
    now = utc_now()
    pending_ids = [
        seed_pending_discovery(durable_database, workspace_id, employer=name, created_at=now)
        for name in ("Globex", "Initech", "Northwind")
    ]
    queries = SourceScanQueries(durable_database)
    filters = PendingQueueFilters(relevance="not_reviewed")
    first = queries.pending_discovery_page(workspace_id, limit=2, filters=filters)

    record_relevance_review(durable_database, workspace_id, pending_ids[0])

    with pytest.raises(ConflictError, match="restart from the first page"):
        queries.pending_discovery_page(
            workspace_id,
            limit=2,
            attention_offset=2,
            expected_snapshot_sha256=first.snapshot_sha256,
            snapshot_before=first.snapshot_before,
            filters=filters,
        )


def test_pending_page_builds_ranking_context_once(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ready and attention lanes share one coherent ranking context."""
    workspace_id = scan_workspace(durable_database)
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    seed_pending_discovery(
        durable_database,
        workspace_id,
        employer="Globex",
        created_at=utc_now(),
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    calls = 0
    original = service.queries.rows.pending_context

    def counted(session: Any, current_workspace_id: str) -> Any:
        nonlocal calls
        calls += 1
        return original(session, current_workspace_id)

    monkeypatch.setattr(service.queries.rows, "pending_context", counted)

    service.pending_discovery_page(workspace_id, limit=2)

    assert calls == 1


def test_pending_page_screens_each_candidate_once(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Projecting the bounded lanes reuses the screening that ordered them."""
    workspace_id = scan_workspace(durable_database)
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    candidate_count = 7
    for index in range(candidate_count):
        seed_pending_discovery(
            durable_database,
            workspace_id,
            employer=f"Candidate {index}",
            created_at=utc_now(),
        )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    calls = 0
    original = screen_discovery

    def counted(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(projection, "screen_discovery", counted)

    page = service.pending_discovery_page(workspace_id, limit=2)

    assert len(page.attention.items) == 2
    assert calls == candidate_count


def test_review_queue_query_count_does_not_grow_with_retained_content_rows(
    durable_database: Database,
) -> None:
    """One page loads current content in bulk instead of once per response row."""
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.stage_listings(
        workspace_id,
        [
            SourceListing(
                employer="Globex",
                title=f"Data Engineer {index}",
                url=f"https://boards.greenhouse.io/globex/jobs/query-{index}",
            )
            for index in range(8)
        ],
        date(2026, 1, 2),
        reason="Staged for one bounded query.",
    )
    with durable_database.session() as session:
        pending = session.scalars(select(PendingDiscovery)).all()
        for row in pending:
            content = pending_posting_content(
                workspace_id,
                row.id,
                row.canonical_url,
                content_status=PostingContentAccessStatus.POLICY_REFUSED,
            )
            session.add(content)
            session.flush()
            row.current_content_id = content.id

    def query_count(limit: int) -> int:
        statements: list[str] = []

        def count_statement(*args: object) -> None:
            statements.append(str(args[2]))

        event.listen(durable_database.engine, "before_cursor_execute", count_statement)
        try:
            assert len(service.list_pending_discoveries(workspace_id, limit=limit)) == limit
        finally:
            event.remove(durable_database.engine, "before_cursor_execute", count_statement)
        return len(statements)

    assert query_count(8) == query_count(1)


def test_the_refusal_list_returns_no_more_than_it_was_asked_for(
    durable_database: Database,
) -> None:
    """Refusals are never pruned, so this read grows for as long as the workspace does."""
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    with durable_database.session() as session:
        for index in range(6):
            session.add(
                PostingRefusal(
                    id=new_id(),
                    workspace_id=workspace_id,
                    employer=f"Globex {index}",
                    title="Data Engineer",
                    condition="residence",
                    reason="Outside every stated place.",
                    refused_at=utc_now(),
                )
            )

    assert len(service.list_refusals(workspace_id, limit=2)) == 2
    assert len(service.list_refusals(workspace_id)) == 6
    with pytest.raises(ValidationError, match="between 1 and 250"):
        service.list_refusals(workspace_id, limit=251)


def test_refusal_history_identifies_archived_targets_with_the_same_title(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    now = utc_now()
    pending_id = seed_pending_discovery(
        durable_database, workspace_id, employer="Globex", created_at=now
    )
    with durable_database.session() as session:
        employer = Employer(workspace_id=workspace_id, name="Globex", normalized_name="globex")
        session.add(employer)
        session.flush()
        job = JobPosting(
            workspace_id=workspace_id,
            employer_id=employer.id,
            title="Data Platform Lead",
            normalized_title="data platform lead",
            first_seen_at=now,
            archived_at=now,
        )
        session.add(job)
        session.flush()
        job_id = job.id
        job_url = "https://boards.greenhouse.io/globex/jobs/old"
        remember_posting_url(
            session,
            job,
            url=job_url,
            source_id=None,
            url_type="employer",
            make_primary=True,
            actor="test",
            origin="test",
            correlation_id=None,
        )
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        pending.archived_at = now
        pending.canonical_url = "https://boards.greenhouse.io/globex/jobs/new"
        for target in ({"job_posting_id": job_id}, {"pending_discovery_id": pending_id}, {}):
            session.add(
                PostingRefusal(
                    workspace_id=workspace_id,
                    employer="Globex",
                    title="Data Platform Lead",
                    condition="posting_status",
                    reason="Posting stated closed.",
                    refused_at=now,
                    **target,
                )
            )

    refusals = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).list_refusals(workspace_id)

    assert {
        (row.job_posting_id, row.pending_discovery_id, row.posting_url) for row in refusals
    } == {
        (job_id, None, job_url),
        (None, pending_id, "https://boards.greenhouse.io/globex/jobs/new"),
        (None, None, None),
    }


def test_scan_response_helpers_refuse_a_non_int_field() -> None:
    """The int-zero helper must fail loudly if a response field grows a richer shape."""
    from pydantic import BaseModel

    from clearscout.application.discovery.scan_queries.history import _zeroed_ints

    class Mixed(BaseModel):
        good: int = 0
        bad: str = ""

    with pytest.raises(TypeError, match="Mixed.bad"):
        _zeroed_ints(Mixed, {"good": 1})
