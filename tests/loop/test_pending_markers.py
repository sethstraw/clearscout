"""Favorite and keep markers hold a discovery family against every unconfirmed dismissal."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from tailorcv.application.discovery.board_scan import SourceScanService
from tailorcv.application.discovery.dispositions import (
    DiscoveryDispositionService,
    dismissed_pending_ids,
)
from tailorcv.application.discovery.pending import pending_closing_date, set_pending_markers
from tailorcv.application.discovery.relevance_review_queries import RelevanceReviewQueries
from tailorcv.application.discovery.source_reading import SourceListing
from tailorcv.application.failures import ConflictError, NotFoundError
from tailorcv.application.job_search.models import (
    ClosingDateEditInput,
    DispositionApply,
    DispositionSelection,
)
from tailorcv.application.tasks import TaskQueueService
from tailorcv.application.workspaces import SearchParametersService
from tailorcv.bootstrap.worker import Worker, default_handlers
from tailorcv.domain import PendingDiscoveryReadiness, TaskStatus, new_id
from tailorcv.domain.identity import url_sha256
from tailorcv.domain.search_parameters import JobSearchParameters, Location, Seniority
from tailorcv.domain.staged_facts import StagedListingFacts
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    AuditEvent,
    BackgroundTask,
    PendingDiscovery,
    PendingDiscoveryDispositionEvent,
)
from tailorcv.infrastructure.web_fetch.listing_facts import published_listing_facts
from tests.support.artifacts import runtime_storage
from tests.support.doubles import ScriptedBoards, UnexpectedPostingFetcher
from tests.support.scenarios import (
    append_relevance_review,
    board_source,
    judged_relevance_review,
    record_relevance_review,
    scan_workspace,
    titles,
    workspace_with_retained_postings,
)

_LONDON = "London, United Kingdom"


def _workspace(database: Database, slug: str = "markers") -> str:
    workspace_id = scan_workspace(database, slug=slug)
    SearchParametersService(database).save(
        workspace_id,
        JobSearchParameters(
            seniority=Seniority(
                target_titles=titles("Data Platform Lead"), excluded_titles=["Retail"]
            ),
            locations=[Location(key="us", country="United States")],
        ),
    )
    return workspace_id


def _row(
    database: Database,
    workspace_id: str,
    title: str = "Data Platform Lead",
    *,
    variant_of: str | None = None,
    location: str = "Remote",
    facts: StagedListingFacts | None = None,
) -> str:
    pending_id = new_id()
    with database.session() as session:
        session.add(
            PendingDiscovery(
                id=pending_id,
                workspace_id=workspace_id,
                fingerprint=pending_id,
                employer="Northwind Fabrication",
                title=title,
                location=location,
                staged_facts=(facts or StagedListingFacts()).as_json(),
                discovery_url=f"https://jobs.example.test/{pending_id}",
                canonical_url=f"https://jobs.example.test/{pending_id}",
                reason="Synthetic scan",
                first_seen_on=date(2026, 1, 1),
                regional_variant_of=variant_of,
            )
        )
    return pending_id


def _mark(
    database: Database,
    workspace_id: str,
    pending_id: str,
    marker: str,
    note: str | None = None,
) -> None:
    with database.session() as session:
        set_pending_markers(
            session,
            workspace_id,
            pending_id,
            favorite=marker in {"favorite", "both"},
            kept=marker in {"keep", "both"},
            keep_note=note,
        )


def _service(database: Database, boards: ScriptedBoards | None = None) -> SourceScanService:
    return SourceScanService(
        database,
        storage=runtime_storage(database),
        fetcher=boards or ScriptedBoards({}),
        posting_fetcher=UnexpectedPostingFetcher(),
    )


def test_a_personal_closing_date_edit_changes_only_the_family_head_and_audits_its_reset(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = _workspace(durable_database)
    head = _row(durable_database, workspace_id)
    copy = _row(durable_database, workspace_id, variant_of=head)
    with durable_database.session() as session:
        session.get_one(PendingDiscovery, head).reason = "Closing date: February 15."
    _mark(durable_database, workspace_id, head, "keep", note="Call the recruiter.")
    times = iter([datetime(2026, 1, 2, tzinfo=UTC), datetime(2026, 1, 3, tzinfo=UTC)])
    monkeypatch.setattr("tailorcv.application.discovery.pending.utc_now", lambda: next(times))
    service = _service(durable_database)

    edited = service.set_pending_closing_date(
        workspace_id, copy, ClosingDateEditInput(closing_date_override=date(2026, 2, 1))
    )
    assert edited.id == head
    with durable_database.session() as session:
        row = session.get_one(PendingDiscovery, head)
        assert row.closing_date_override == date(2026, 2, 1)
        assert row.closing_date_override_updated_at == datetime(2026, 1, 2, tzinfo=UTC)
        assert row.kept and row.keep_note == "Call the recruiter."
        assert row.archived_at is None and row.withdrawn_at is None
        assert row.content_status == "not_yet_read"
        assert row.reason == "Closing date: February 15."
        assert pending_closing_date(session, workspace_id, copy) == (
            date(2026, 2, 1),
            datetime(2026, 1, 2, tzinfo=UTC),
        )
        assert session.get_one(PendingDiscovery, copy).closing_date_override is None
        assert session.scalar(select(func.count()).select_from(BackgroundTask)) == 0

    reset = service.set_pending_closing_date(
        workspace_id, head, ClosingDateEditInput(closing_date_override=None)
    )
    assert reset.id == head
    with durable_database.session() as session:
        row = session.get_one(PendingDiscovery, head)
        assert row.closing_date_override is None
        assert row.closing_date_override_updated_at == datetime(2026, 1, 3, tzinfo=UTC)
        assert pending_closing_date(session, workspace_id, copy) == (
            None,
            datetime(2026, 1, 3, tzinfo=UTC),
        )
        events = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.action == "pending_discovery.closing_date_changed")
            .order_by(AuditEvent.created_at)
        ).all()
        assert [event.entity_id for event in events] == [head, head]
        assert [event.details for event in events] == [
            {"old_closing_date_override": None, "closing_date_override": "2026-02-01"},
            {"old_closing_date_override": "2026-02-01", "closing_date_override": None},
        ]


@pytest.mark.parametrize(
    ("source_date", "source_day", "target_date", "target_day", "expected_date", "expected_day"),
    [
        (date(2026, 2, 1), 2, None, None, date(2026, 2, 1), 2),
        (date(2026, 2, 1), 3, date(2026, 2, 2), 2, date(2026, 2, 1), 3),
        (date(2026, 2, 1), 2, date(2026, 2, 2), 3, date(2026, 2, 2), 3),
        (date(2026, 2, 1), 2, date(2026, 2, 2), 2, date(2026, 2, 2), 2),
        (None, 3, date(2026, 2, 2), 2, None, 3),
        (date(2026, 2, 1), 2, None, 3, None, 3),
    ],
)
def test_a_duplicate_merge_preserves_the_latest_explicit_date_edit_and_destination_ties(
    durable_database: Database,
    source_date: date | None,
    source_day: int,
    target_date: date | None,
    target_day: int | None,
    expected_date: date | None,
    expected_day: int,
) -> None:
    workspace_id = _workspace(durable_database)
    original = _row(durable_database, workspace_id)
    copy = _row(durable_database, workspace_id)
    with durable_database.session() as session:
        source = session.get_one(PendingDiscovery, copy)
        source.closing_date_override = source_date
        source.closing_date_override_updated_at = datetime(2026, 1, source_day, tzinfo=UTC)
        target = session.get_one(PendingDiscovery, original)
        target.closing_date_override = target_date
        target.closing_date_override_updated_at = (
            datetime(2026, 1, target_day, tzinfo=UTC) if target_day else None
        )
    selection = DispositionSelection.model_validate(
        {
            "action": "dismiss",
            "selection_mode": "explicit",
            "category": "duplicate_pending",
            "duplicate_of_pending_id": original,
            "pending_discovery_ids": [copy],
        }
    )

    _apply(DiscoveryDispositionService(durable_database), workspace_id, selection)

    with durable_database.session() as session:
        target = session.get_one(PendingDiscovery, original)
        assert target.closing_date_override == expected_date
        assert target.closing_date_override_updated_at == datetime(
            2026, 1, expected_day, tzinfo=UTC
        )
        source = session.get_one(PendingDiscovery, copy)
        assert source.closing_date_override is None
        assert source.closing_date_override_updated_at is None


def test_a_withdrawn_head_hands_its_explicit_date_reset_to_the_new_family_head(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    head = _row(durable_database, workspace_id)
    copy = _row(durable_database, workspace_id, variant_of=head)
    with durable_database.session() as session:
        source = session.get_one(PendingDiscovery, head)
        source.closing_date_override_updated_at = datetime(2026, 1, 3, tzinfo=UTC)
        target = session.get_one(PendingDiscovery, copy)
        target.closing_date_override = date(2026, 2, 1)
        target.closing_date_override_updated_at = datetime(2026, 1, 2, tzinfo=UTC)

    _service(durable_database).staging.checks.withdraw_pending_discovery(
        workspace_id,
        head,
        note="The publisher check found this copy gone.",
        actor="worker",
        origin="test",
        correlation_id=None,
    )

    with durable_database.session() as session:
        target = session.get_one(PendingDiscovery, copy)
        assert target.regional_variant_of is None
        assert target.closing_date_override is None
        assert target.closing_date_override_updated_at == datetime(2026, 1, 3, tzinfo=UTC)
        source = session.get_one(PendingDiscovery, head)
        assert source.closing_date_override_updated_at is None


def test_marker_edits_and_confirmed_dismissal_leave_personal_dates_and_legacy_text_alone(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    head = _row(durable_database, workspace_id)
    copy = _row(durable_database, workspace_id, variant_of=head)
    stamp = datetime(2026, 1, 2, tzinfo=UTC)
    with durable_database.session() as session:
        row = session.get_one(PendingDiscovery, copy)
        row.closing_date_override = date(2026, 2, 1)
        row.closing_date_override_updated_at = stamp
        row.reason = "Closing date: February 1."
    _mark(durable_database, workspace_id, head, "keep", note="Wait for the recruiter.")
    _apply(
        DiscoveryDispositionService(durable_database),
        workspace_id,
        _explicit_dismiss([head], confirmed=True),
    )

    with durable_database.session() as session:
        row = session.get_one(PendingDiscovery, copy)
        assert row.closing_date_override == date(2026, 2, 1)
        assert row.closing_date_override_updated_at == stamp
        assert row.reason == "Closing date: February 1."
        assert not row.favorite and not row.kept


def _listed(database: Database, workspace_id: str) -> list[str]:
    return [item.id for item in _service(database).list_pending_discoveries(workspace_id)]


def _dismissed(database: Database, workspace_id: str) -> set[str]:
    with database.session() as session:
        return dismissed_pending_ids(session, workspace_id)


def _apply(
    service: DiscoveryDispositionService, workspace_id: str, selection: DispositionSelection
) -> list[str]:
    preview = service.preview(workspace_id, selection)
    applied = service.apply(
        workspace_id,
        DispositionApply.model_validate(
            {**selection.model_dump(), "snapshot_sha256": preview.snapshot_sha256}
        ),
        actor="local_user",
        origin="api",
        correlation_id="markers",
    )
    return sorted(event.pending_discovery_id for event in applied)


def _explicit_dismiss(pending_ids: list[str], *, confirmed: bool = False) -> DispositionSelection:
    return DispositionSelection.model_validate(
        {
            "action": "dismiss",
            "selection_mode": "explicit",
            "category": "out_of_scope_title",
            "pending_discovery_ids": pending_ids,
            "dismiss_marked": confirmed,
        }
    )


def test_a_bulk_rule_dismissal_skips_marked_families_and_names_their_heads(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    favorite = _row(durable_database, workspace_id, "Retail Display Coordinator")
    kept_head = _row(durable_database, workspace_id, "Retail Floor Planner")
    _row(durable_database, workspace_id, "Retail Floor Planner", variant_of=kept_head)
    plain = _row(durable_database, workspace_id, "Retail Stock Lead")
    _mark(durable_database, workspace_id, favorite, "favorite")
    _mark(durable_database, workspace_id, kept_head, "keep")
    service = DiscoveryDispositionService(durable_database)
    selection = DispositionSelection.model_validate(
        {
            "action": "dismiss",
            "selection_mode": "rule",
            "category": "title_excluded",
            "reason": "Outside the accepted title policy",
            "pending_discovery_ids": [favorite, kept_head, plain],
        }
    )

    preview = service.preview(workspace_id, selection)
    applied = _apply(service, workspace_id, selection)

    assert preview.skipped_marked_ids == sorted([favorite, kept_head])
    assert [target.id for target in preview.targets] == [plain]
    assert applied == [plain]
    assert _dismissed(durable_database, workspace_id) == {plain}
    assert set(_listed(durable_database, workspace_id)) == {favorite, kept_head}


@pytest.mark.parametrize("marker", ["favorite", "keep"])
def test_an_unconfirmed_single_dismissal_of_a_marked_row_is_refused(
    durable_database: Database, marker: str
) -> None:
    workspace_id = _workspace(durable_database)
    pending_id = _row(durable_database, workspace_id)
    _mark(durable_database, workspace_id, pending_id, marker)
    service = DiscoveryDispositionService(durable_database)
    selection = _explicit_dismiss([pending_id])

    with pytest.raises(ConflictError, match="favorite or kept"):
        service.preview(workspace_id, selection)
    with pytest.raises(ConflictError, match="favorite or kept"):
        service.apply(
            workspace_id,
            DispositionApply.model_validate(
                {**selection.model_dump(), "snapshot_sha256": "0" * 64}
            ),
            actor="local_user",
            origin="api",
            correlation_id=None,
        )
    assert _dismissed(durable_database, workspace_id) == set()


def test_a_confirmed_dismissal_reaches_a_marked_family_and_clears_its_markers(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    head = _row(durable_database, workspace_id)
    variant = _row(durable_database, workspace_id, variant_of=head)
    _mark(durable_database, workspace_id, head, "both", note="Recruiter asked me to wait.")
    service = DiscoveryDispositionService(durable_database)

    applied = _apply(service, workspace_id, _explicit_dismiss([head], confirmed=True))

    assert applied == sorted([head, variant])
    assert _listed(durable_database, workspace_id) == []
    with durable_database.session() as session:
        rows = session.scalars(
            select(PendingDiscovery).where(PendingDiscovery.workspace_id == workspace_id)
        ).all()
        assert {(row.favorite, row.kept, row.keep_note) for row in rows} == {(False, False, None)}


@pytest.mark.parametrize("marker", ["favorite", "keep"])
@pytest.mark.parametrize(
    "origin",
    ["evaluation.refusal", "source.scan.body_screen", "discovery.relevance_review.refusal"],
)
def test_a_program_refusal_never_dismisses_a_marked_family(
    durable_database: Database, marker: str, origin: str
) -> None:
    workspace_id = _workspace(durable_database)
    head = _row(durable_database, workspace_id)
    variant = _row(durable_database, workspace_id, variant_of=head)
    _mark(durable_database, workspace_id, variant, marker)

    with durable_database.session() as session:
        events = [
            DiscoveryDispositionService(durable_database).dismiss_refusal_in_session(
                session,
                workspace_id,
                pending_id,
                category="location_outside",
                reason="The posting is outside the searched markets.",
                search_parameter_set_id=None,
                actor="worker",
                origin=origin,
                correlation_id="task-1",
            )
            for pending_id in (head, variant)
        ]

    assert events == [[], []]
    assert _dismissed(durable_database, workspace_id) == set()


def test_a_no_requirements_evaluation_never_dismisses_a_kept_row(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    pending_id = _row(durable_database, workspace_id)
    _mark(durable_database, workspace_id, pending_id, "keep")

    with durable_database.session() as session:
        events = DiscoveryDispositionService(durable_database).dismiss_no_requirements_in_session(
            session,
            workspace_id,
            pending_id,
            content_sha256="a" * 64,
            actor="worker",
            origin="evaluation.body_screen",
            correlation_id="evaluate-1",
        )

    assert events == []
    assert _listed(durable_database, workspace_id) == [pending_id]


def _greenhouse_listing(url: str, location: str) -> SourceListing:
    return SourceListing(
        employer="Acme",
        title="Data Platform Lead",
        url=url,
        location=location,
        facts=published_listing_facts("greenhouse", locations=location),
    )


def test_a_board_scan_holdout_never_dismisses_a_favorite(durable_database: Database) -> None:
    workspace_id = _workspace(durable_database)
    source_id = board_source(
        durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme"
    )
    url = "https://boards.greenhouse.io/acme/jobs/marked"
    service = _service(
        durable_database, ScriptedBoards({"Acme": [_greenhouse_listing(url, _LONDON)]})
    )
    service.stage_listings(
        workspace_id,
        [_greenhouse_listing(url, "Remote, United States")],
        date(2026, 8, 25),
        reason="Seeded.",
        source_id=source_id,
    )
    with durable_database.session() as session:
        pending_id = session.scalars(select(PendingDiscovery.id)).one()
    _mark(durable_database, workspace_id, pending_id, "favorite")

    result = service.scan(workspace_id)

    with durable_database.session() as session:
        assert session.scalars(select(PendingDiscoveryDispositionEvent)).all() == []
    assert _listed(durable_database, workspace_id) == [pending_id]
    reasons = [item["reason"] for item in result["funnel"]["outcomes"] if item["url"] == url]
    assert reasons == ["already_known"]


def test_a_queue_rescreen_never_dismisses_a_kept_row(durable_database: Database) -> None:
    workspace_id = _workspace(durable_database)
    pending_id = _row(
        durable_database,
        workspace_id,
        location=_LONDON,
        facts=published_listing_facts("greenhouse", locations=_LONDON),
    )
    _mark(durable_database, workspace_id, pending_id, "keep")
    queue = TaskQueueService(durable_database)
    task_id = queue.enqueue(
        workspace_id=workspace_id,
        task_type="discovery.rescreen_queue",
        payload={},
        idempotency_key=new_id(),
    ).id
    worker = Worker(
        durable_database,
        worker_id="rescreen-worker",
        handlers=default_handlers(
            durable_database, artifact_root=runtime_storage(durable_database)
        ),
    )

    assert worker.run_once()

    task = queue.get(task_id)
    assert task.status is TaskStatus.COMPLETED, task.error_message
    assert task.progress["held_out"] == 0
    assert _dismissed(durable_database, workspace_id) == set()


def test_a_marked_row_that_closes_stays_listed_as_closed(durable_database: Database) -> None:
    workspace_id = _workspace(durable_database)
    kept = _row(durable_database, workspace_id)
    plain = _row(durable_database, workspace_id)
    _mark(durable_database, workspace_id, kept, "keep")
    service = _service(durable_database)

    for pending_id in (kept, plain):
        service.staging.checks.withdraw_pending_discovery(
            workspace_id,
            pending_id,
            note="The publisher check found this posting gone.",
            actor="worker",
            origin="test",
            correlation_id=None,
        )

    listed = service.list_pending_discoveries(workspace_id)
    assert [(item.id, item.kept) for item in listed] == [(kept, True)]
    assert listed[0].withdrawn_at is not None
    assert listed[0].readiness is PendingDiscoveryReadiness.WITHDRAWN
    page = service.pending_discovery_page(workspace_id)
    assert page.ready.items == []
    assert [(item.id, item.readiness) for item in page.attention.items] == [
        (kept, PendingDiscoveryReadiness.WITHDRAWN)
    ]


@pytest.mark.parametrize(("copy_archived", "listed_heads"), [(False, 1), (True, 0)])
def test_a_closed_family_stays_listed_while_an_unarchived_copy_is_kept(
    durable_database: Database, copy_archived: bool, listed_heads: int
) -> None:
    workspace_id = _workspace(durable_database)
    head = _row(durable_database, workspace_id)
    variant = _row(durable_database, workspace_id, variant_of=head)
    service = _service(durable_database)
    for pending_id in (variant, head):
        service.staging.checks.withdraw_pending_discovery(
            workspace_id,
            pending_id,
            note="The publisher check found this posting gone.",
            actor="worker",
            origin="test",
            correlation_id=None,
        )
    with durable_database.session() as session:
        copy = session.get(PendingDiscovery, variant)
        assert copy is not None
        assert copy.regional_variant_of == head
        copy.kept = True
        copy.archived_at = datetime.now(UTC) if copy_archived else None

    assert [item.id for item in service.list_pending_discoveries(workspace_id)] == (
        [head] * listed_heads
    )
    page = service.pending_discovery_page(workspace_id)
    assert [item.id for item in page.attention.items] == [head] * listed_heads


def test_a_closed_kept_row_with_a_retained_body_never_takes_a_ready_slot(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, [kept, plain] = workspace_with_retained_postings(
        durable_database,
        tmp_path,
        [("Data Engineer", "Data Engineer\n\nBuild ledgers."), ("Analytics Engineer", "Model.")],
        slug="closed-kept",
    )
    _mark(durable_database, workspace_id, kept, "keep")
    service = _service(durable_database)
    service.staging.checks.withdraw_pending_discovery(
        workspace_id,
        kept,
        note="The publisher check found this posting gone.",
        actor="worker",
        origin="test",
        correlation_id=None,
    )

    cohorts = service.list_pending_discoveries(workspace_id, ready_limit=1, attention_limit=1)

    assert [(item.id, item.readiness) for item in cohorts] == [
        (plain, PendingDiscoveryReadiness.READY),
        (kept, PendingDiscoveryReadiness.WITHDRAWN),
    ]


def test_favorites_then_kept_rows_lead_the_queue_in_both_reads(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    plain = _row(durable_database, workspace_id, "Data Platform Lead")
    kept = _row(durable_database, workspace_id, "Office Coordinator")
    favorite = _row(durable_database, workspace_id, "Warehouse Associate")
    both = _row(durable_database, workspace_id, "Retail Stock Lead")
    _mark(durable_database, workspace_id, kept, "keep")
    _mark(durable_database, workspace_id, favorite, "favorite")
    _mark(durable_database, workspace_id, both, "both")
    service = _service(durable_database)

    listed = _listed(durable_database, workspace_id)
    page = service.pending_discovery_page(workspace_id)

    assert listed[2:] == [kept, plain]
    assert set(listed[:2]) == {favorite, both}
    paged = [item.id for item in page.attention.items]
    assert paged[2:] == [kept, plain]
    assert set(paged[:2]) == {favorite, both}


def test_an_open_favorite_leads_a_closed_one_in_both_reads(durable_database: Database) -> None:
    workspace_id = _workspace(durable_database)
    closed = _row(durable_database, workspace_id, "Data Platform Lead")
    opened = _row(durable_database, workspace_id, "Retail Stock Lead")
    for pending_id in (closed, opened):
        _mark(durable_database, workspace_id, pending_id, "favorite")
    service = _service(durable_database)
    service.staging.checks.withdraw_pending_discovery(
        workspace_id,
        closed,
        note="The publisher check found this posting gone.",
        actor="worker",
        origin="test",
        correlation_id=None,
    )

    page = service.pending_discovery_page(workspace_id)

    assert _listed(durable_database, workspace_id) == [opened, closed]
    assert [item.id for item in page.attention.items] == [opened, closed]


def test_a_confirmed_duplicate_dismissal_hands_its_markers_to_the_original(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    original_head = _row(durable_database, workspace_id, "Data Platform Lead")
    original = _row(durable_database, workspace_id, "Data Platform Lead", variant_of=original_head)
    copy = _row(durable_database, workspace_id, "Data Platform Lead")
    _mark(durable_database, workspace_id, copy, "keep", note="Recruiter asked me to wait.")
    selection = DispositionSelection.model_validate(
        {
            "action": "dismiss",
            "selection_mode": "explicit",
            "category": "duplicate_pending",
            "duplicate_of_pending_id": original,
            "pending_discovery_ids": [copy],
            "dismiss_marked": True,
        }
    )

    applied = _apply(DiscoveryDispositionService(durable_database), workspace_id, selection)

    assert applied == [copy]
    with durable_database.session() as session:
        rows = {
            row.id: (row.kept, row.keep_note)
            for row in session.scalars(
                select(PendingDiscovery).where(PendingDiscovery.workspace_id == workspace_id)
            )
        }
    assert rows == {
        original_head: (True, "Recruiter asked me to wait."),
        original: (False, None),
        copy: (False, None),
    }


def test_editing_a_keep_note_leaves_the_stored_review_out_of_date_without_running_one(
    durable_database: Database,
) -> None:
    # The recorded review saves its own first parameter version.
    workspace_id = scan_workspace(durable_database, slug="keep-note")
    pending_id = _row(durable_database, workspace_id)
    record_relevance_review(durable_database, workspace_id, pending_id)
    queries = _service(durable_database).queries

    def stale() -> list[str]:
        review = queries.get_pending_discovery(workspace_id, pending_id).relevance_review
        assert review is not None
        return list(review.out_of_date_because)

    def tasks() -> int:
        with durable_database.session() as session:
            return int(session.scalar(select(func.count()).select_from(BackgroundTask)) or 0)

    before = tasks()
    _mark(durable_database, workspace_id, pending_id, "keep")
    assert "keep_note" not in stale()

    _mark(durable_database, workspace_id, pending_id, "keep", note="Team lead wrote back.")

    assert "keep_note" in stale()
    assert tasks() == before


def test_a_marker_on_a_copy_lifts_to_the_head_the_queue_lists(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    head = _row(durable_database, workspace_id)
    variant = _row(durable_database, workspace_id, variant_of=head)
    with durable_database.session() as session:
        copy = session.get(PendingDiscovery, variant)
        assert copy is not None
        copy.kept = True
        copy.keep_note = "Marked before it folded."

    [listed] = _service(durable_database).list_pending_discoveries(workspace_id)

    assert (listed.id, listed.kept, listed.keep_note) == (
        head,
        True,
        "Marked before it folded.",
    )
    with pytest.raises(ConflictError, match="favorite or kept"):
        DiscoveryDispositionService(durable_database).preview(
            workspace_id, _explicit_dismiss([head])
        )


def test_a_marker_set_through_a_copy_is_held_by_the_head(durable_database: Database) -> None:
    workspace_id = _workspace(durable_database)
    head = _row(durable_database, workspace_id)
    variant = _row(durable_database, workspace_id, variant_of=head)

    _mark(durable_database, workspace_id, variant, "favorite")

    with durable_database.session() as session:
        rows = {
            row.id: row.favorite
            for row in session.scalars(
                select(PendingDiscovery).where(PendingDiscovery.workspace_id == workspace_id)
            )
        }
    assert rows == {head: True, variant: False}


def test_a_withdrawn_favorite_head_hands_its_markers_to_the_copy_that_heads_the_family(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    head = _row(durable_database, workspace_id)
    variant = _row(durable_database, workspace_id, variant_of=head)
    _mark(durable_database, workspace_id, head, "favorite")
    service = _service(durable_database)
    service.staging.checks.withdraw_pending_discovery(
        workspace_id,
        head,
        note="The publisher check found this copy gone.",
        actor="worker",
        origin="test",
        correlation_id=None,
    )

    with durable_database.session() as session:
        events = DiscoveryDispositionService(durable_database).dismiss_refusal_in_session(
            session,
            workspace_id,
            variant,
            category="location_outside",
            reason="The posting is outside the searched markets.",
            search_parameter_set_id=None,
            actor="worker",
            origin="evaluation.refusal",
            correlation_id="task-1",
        )

    assert events == []
    assert _dismissed(durable_database, workspace_id) == set()
    listed = service.list_pending_discoveries(workspace_id)
    assert [(item.id, item.favorite, item.withdrawn_at) for item in listed] == [
        (variant, True, None)
    ]
    with durable_database.session() as session:
        closed = session.get(PendingDiscovery, head)
        assert closed is not None
        assert (closed.favorite, closed.kept, closed.regional_variant_of) == (False, False, None)


def test_a_withdrawn_favorite_head_stays_listed_when_only_a_dismissed_copy_survives(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    board = board_source(
        durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme"
    )
    head_url = "https://boards.greenhouse.io/acme/jobs/platform-lead-east"
    copy_url = "https://boards.greenhouse.io/acme/jobs/platform-lead-west"
    ids: dict[str, str] = {}
    with durable_database.session() as session:
        for url, seen in ((head_url, date(2026, 8, 24)), (copy_url, date(2026, 8, 25))):
            row = PendingDiscovery(
                id=new_id(),
                workspace_id=workspace_id,
                fingerprint=url_sha256(url),
                employer="Acme",
                title="Data Platform Lead",
                location="Remote",
                requisition_id="R-100",
                discovery_url=url,
                canonical_url=url,
                reason="Synthetic scan",
                first_seen_on=seen,
                source_id=board,
            )
            session.add(row)
            ids[url] = row.id
    head, copy = ids[head_url], ids[copy_url]
    _apply(DiscoveryDispositionService(durable_database), workspace_id, _explicit_dismiss([copy]))
    service = _service(durable_database)
    # A rescan folds the dismissed copy under the live head.
    service.stage_listings(
        workspace_id,
        [
            SourceListing(
                employer="Acme",
                title="Data Platform Lead",
                url=copy_url,
                location="Remote",
                requisition_id="R-100",
                facts=published_listing_facts("greenhouse", locations="Remote"),
            )
        ],
        date(2026, 8, 26),
        reason="Seeded.",
        source_id=board,
    )
    with durable_database.session() as session:
        folded = session.get(PendingDiscovery, copy)
        assert folded is not None
        assert folded.regional_variant_of == head
    _mark(durable_database, workspace_id, head, "favorite")

    service.staging.checks.withdraw_pending_discovery(
        workspace_id,
        head,
        note="The publisher check found this copy gone.",
        actor="worker",
        origin="test",
        correlation_id=None,
    )

    listed = service.list_pending_discoveries(workspace_id)
    assert [(item.id, item.favorite) for item in listed] == [(head, True)]
    with durable_database.session() as session:
        dismissed = session.get(PendingDiscovery, copy)
        assert dismissed is not None
        assert (dismissed.favorite, dismissed.regional_variant_of) == (False, None)


@pytest.mark.parametrize("copy_note", [None, "Copy note from the hiring manager."])
def test_two_marked_heads_merge_their_markers_when_a_family_refolds(
    durable_database: Database, copy_note: str | None
) -> None:
    workspace_id = _workspace(durable_database)
    head = _row(durable_database, workspace_id)
    variant = _row(durable_database, workspace_id, variant_of=head)
    _mark(durable_database, workspace_id, head, "both", note="Head note from the recruiter.")
    with durable_database.session() as session:
        copy = session.get_one(PendingDiscovery, variant)
        copy.kept = True
        copy.keep_note = copy_note
    _service(durable_database).staging.checks.withdraw_pending_discovery(
        workspace_id,
        head,
        note="The publisher check found this copy gone.",
        actor="worker",
        origin="test",
        correlation_id=None,
    )

    with durable_database.session() as session:
        rows = {
            row.id: (row.favorite, row.kept, row.keep_note)
            for row in session.scalars(
                select(PendingDiscovery).where(PendingDiscovery.workspace_id == workspace_id)
            )
        }
    assert rows == {
        head: (False, False, None),
        variant: (True, True, copy_note or "Head note from the recruiter."),
    }


def test_a_marked_variant_under_an_archived_head_is_not_found(durable_database: Database) -> None:
    workspace_id = _workspace(durable_database)
    head = _row(durable_database, workspace_id)
    variant = _row(durable_database, workspace_id, variant_of=head)
    with durable_database.session() as session:
        archived = session.get(PendingDiscovery, head)
        assert archived is not None
        archived.archived_at = datetime(2026, 8, 1, tzinfo=UTC)

    with pytest.raises(NotFoundError):
        _mark(durable_database, workspace_id, variant, "favorite")


def test_a_bulk_dismissal_of_only_marked_rows_points_at_their_own_rows(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    favorite = _row(durable_database, workspace_id, "Retail Display Coordinator")
    kept = _row(durable_database, workspace_id, "Retail Floor Planner")
    _mark(durable_database, workspace_id, favorite, "favorite")
    _mark(durable_database, workspace_id, kept, "keep")
    selection = DispositionSelection.model_validate(
        {
            "action": "dismiss",
            "selection_mode": "rule",
            "category": "title_excluded",
            "reason": "Outside the accepted title policy",
            "pending_discovery_ids": [favorite, kept],
        }
    )

    with pytest.raises(ConflictError, match="from its own row"):
        DiscoveryDispositionService(durable_database).preview(workspace_id, selection)


def _stale_review_reasons(database: Database, workspace_id: str, pending_id: str) -> list[str]:
    review = (
        _service(database).queries.get_pending_discovery(workspace_id, pending_id).relevance_review
    )
    assert review is not None
    return list(review.out_of_date_because)


def test_a_note_lifted_from_a_copy_onto_the_head_leaves_a_review_sent_that_note_current(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database, slug="lifted-note")
    head = _row(durable_database, workspace_id)
    variant = _row(durable_database, workspace_id, variant_of=head)
    with durable_database.session() as session:
        copy = session.get(PendingDiscovery, variant)
        assert copy is not None
        copy.kept = True
        copy.keep_note = "Marked before it folded."
    record_relevance_review(
        durable_database, workspace_id, head, keep_note="Marked before it folded."
    )

    _mark(durable_database, workspace_id, head, "keep", note="Marked before it folded.")

    assert "keep_note" not in _stale_review_reasons(durable_database, workspace_id, head)


def test_a_confirmed_dismissal_that_drops_a_note_leaves_the_review_out_of_date(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database, slug="dropped-note")
    pending_id = _row(durable_database, workspace_id)
    record_relevance_review(
        durable_database, workspace_id, pending_id, keep_note="Waiting on the hiring manager."
    )
    with durable_database.session() as session:
        row = session.get(PendingDiscovery, pending_id)
        assert row is not None
        row.kept = True
        row.keep_note = "Waiting on the hiring manager."
    assert "keep_note" not in _stale_review_reasons(durable_database, workspace_id, pending_id)

    _apply(
        DiscoveryDispositionService(durable_database),
        workspace_id,
        _explicit_dismiss([pending_id], confirmed=True),
    )

    assert "keep_note" in _stale_review_reasons(durable_database, workspace_id, pending_id)


def test_a_kept_row_that_folds_under_a_live_head_hands_the_head_its_note(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    board = board_source(
        durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme"
    )
    head_url = "https://boards.greenhouse.io/acme/jobs/platform-lead-east"
    kept_url = "https://boards.greenhouse.io/acme/jobs/platform-lead-west"
    ids: dict[str, str] = {}
    with durable_database.session() as session:
        for url, employer, seen in (
            (head_url, "Acme", date(2026, 8, 24)),
            (kept_url, "Acme Operations", date(2026, 8, 25)),
        ):
            row = PendingDiscovery(
                id=new_id(),
                workspace_id=workspace_id,
                fingerprint=url_sha256(url),
                employer=employer,
                title="Data Platform Lead",
                location="Remote",
                requisition_id="R-100",
                discovery_url=url,
                canonical_url=url,
                reason="Synthetic scan",
                first_seen_on=seen,
                source_id=board,
            )
            session.add(row)
            ids[url] = row.id
    head, kept = ids[head_url], ids[kept_url]
    _mark(durable_database, workspace_id, kept, "keep", note="Hiring manager replied.")

    _service(durable_database).stage_listings(
        workspace_id,
        [
            SourceListing(
                employer="Acme Operations",
                title="Data Platform Lead",
                url=kept_url,
                location="Remote",
                requisition_id="R-100",
                facts=published_listing_facts("greenhouse", locations="Remote"),
            )
        ],
        date(2026, 8, 26),
        reason="Seeded.",
        source_id=board,
    )

    with durable_database.session() as session:
        rows = {
            row.id: (row.regional_variant_of, row.kept, row.keep_note)
            for row in session.scalars(
                select(PendingDiscovery).where(PendingDiscovery.workspace_id == workspace_id)
            )
        }
    assert rows == {
        head: (None, True, "Hiring manager replied."),
        kept: (head, False, None),
    }


_REVIEWED_BODY = "Data Engineer\n\nNorthwind Fabrication runs streaming pipelines for its plants."


def _reviewed_rows(database: Database, tmp_path: Path, slug: str) -> tuple[str, str, str]:
    workspace_id, (first, second) = workspace_with_retained_postings(
        database,
        tmp_path,
        [("Data Engineer", _REVIEWED_BODY), ("Data Engineer, Plants", _REVIEWED_BODY)],
        slug=slug,
    )
    return workspace_id, first, second


def _review(database: Database, workspace_id: str, pending_id: str, note: str | None) -> None:
    append_relevance_review(
        database,
        workspace_id,
        pending_id,
        body=_REVIEWED_BODY,
        review=judged_relevance_review(pending_id),
        keep_note=note,
    )


def _staleness(database: Database, workspace_id: str, *pending_ids: str) -> list[list[str]]:
    with database.session() as session:
        current = RelevanceReviewQueries(database).current_for_pending_ids(
            session, workspace_id, list(pending_ids)
        )
    return [list(current[pending_id].out_of_date_because) for pending_id in pending_ids]


def test_a_duplicate_dismissal_and_its_restore_leave_both_reviews_out_of_date_when_the_note_moves(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, original, copy = _reviewed_rows(durable_database, tmp_path, "moved-note")
    _mark(durable_database, workspace_id, copy, "keep", note="Recruiter asked me to wait.")
    _review(durable_database, workspace_id, copy, "Recruiter asked me to wait.")
    _review(durable_database, workspace_id, original, None)
    assert _staleness(durable_database, workspace_id, copy, original) == [[], []]
    selection = DispositionSelection.model_validate(
        {
            "action": "dismiss",
            "selection_mode": "explicit",
            "category": "duplicate_pending",
            "duplicate_of_pending_id": original,
            "pending_discovery_ids": [copy],
            "dismiss_marked": True,
        }
    )

    assert _apply(DiscoveryDispositionService(durable_database), workspace_id, selection) == [copy]

    assert _staleness(durable_database, workspace_id, copy, original) == [
        ["keep_note"],
        ["keep_note"],
    ]
    restore = DispositionSelection.model_validate(
        {
            "action": "restore",
            "selection_mode": "explicit",
            "category": "duplicate_pending",
            "reason": "These are two separate openings.",
            "pending_discovery_ids": [copy],
        }
    )

    assert _apply(DiscoveryDispositionService(durable_database), workspace_id, restore) == [copy]

    assert _staleness(durable_database, workspace_id, copy, original) == [
        ["keep_note"],
        ["keep_note"],
    ]


def test_a_changed_head_note_leaves_a_folded_members_review_out_of_date(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, head, member = _reviewed_rows(durable_database, tmp_path, "folded-note")
    with durable_database.session() as session:
        folded = session.get_one(PendingDiscovery, member)
        folded.regional_variant_of = head
    _mark(durable_database, workspace_id, head, "keep", note="The plant rotation suits me.")
    _review(durable_database, workspace_id, member, "The plant rotation suits me.")
    assert _staleness(durable_database, workspace_id, member) == [[]]

    _mark(durable_database, workspace_id, head, "keep", note="Only the day shift.")

    assert _staleness(durable_database, workspace_id, member) == [["keep_note"]]


def test_archiving_the_head_that_carries_the_note_leaves_a_folded_members_review_out_of_date(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, head, member = _reviewed_rows(durable_database, tmp_path, "archived-note")
    with durable_database.session() as session:
        folded = session.get_one(PendingDiscovery, member)
        folded.regional_variant_of = head
    _mark(durable_database, workspace_id, head, "keep", note="The plant rotation suits me.")
    _review(durable_database, workspace_id, member, "The plant rotation suits me.")
    assert _staleness(durable_database, workspace_id, member) == [[]]

    with durable_database.session() as session:
        session.get_one(PendingDiscovery, head).archived_at = datetime(2026, 8, 1, tzinfo=UTC)

    assert _staleness(durable_database, workspace_id, member) == [["keep_note"]]


def test_a_refold_that_makes_a_copy_the_head_leaves_a_review_sent_the_old_head_note_out_of_date(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, head, member = _reviewed_rows(durable_database, tmp_path, "refolded-note")
    with durable_database.session() as session:
        session.get_one(PendingDiscovery, member).regional_variant_of = head
    _mark(durable_database, workspace_id, head, "keep", note="Head note from the recruiter.")
    with durable_database.session() as session:
        folded = session.get_one(PendingDiscovery, member)
        folded.kept = True
        folded.keep_note = "Copy note from the hiring manager."
    _review(durable_database, workspace_id, member, "Head note from the recruiter.")
    assert _staleness(durable_database, workspace_id, member) == [[]]

    _service(durable_database).staging.checks.withdraw_pending_discovery(
        workspace_id,
        head,
        note="The publisher check found this copy gone.",
        actor="worker",
        origin="test",
        correlation_id=None,
    )

    assert _staleness(durable_database, workspace_id, member) == [["keep_note"]]
