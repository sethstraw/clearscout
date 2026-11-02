"""Recorded scan results and the search plan a finished campaign run advances."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from tailorcv.application.discovery.board_scan import SourceScanService
from tailorcv.application.discovery.scan_queries import SourceScanQueries
from tailorcv.application.discovery.source_reading import SourceRead
from tailorcv.domain import SourceReadOutcome, utc_now
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    BackgroundTask,
    PendingDiscovery,
    SourceScan,
)
from tests.support.artifacts import runtime_storage
from tests.support.doubles import ScriptedBoards, UnexpectedPostingFetcher
from tests.support.scenarios import (
    board_source,
    mark_reset_archived,
    scan_workspace,
    source_listing,
    state_target_titles,
)


def test_source_scan_adapter_skip_reasons_account_for_raw_minus_read_delta(
    durable_database: Database,
) -> None:
    """Adapter-layer drops must add up to the difference between raw and read counts."""
    workspace_id = scan_workspace(durable_database)
    board_source(durable_database, workspace_id, "Globex", "https://jobs.lever.co/globex")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    kept = [
        source_listing(
            "Data Platform Lead",
            f"https://jobs.lever.co/globex/{index}",
            employer="Globex",
        )
        for index in range(2)
    ]

    class AdapterDropsSome:
        def listings(
            self,
            _source_name: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=kept,
                complete=True,
                raw_count=5,
                outcome=SourceReadOutcome.ATS_COMPLETE,
                skip_reasons={"missing_required_posting_identity": 3},
            )

    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=AdapterDropsSome(),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    [record] = (
        SourceScanService(
            durable_database,
            storage=runtime_storage(durable_database),
            posting_fetcher=UnexpectedPostingFetcher(),
        )
        .list_scan_history(workspace_id)
        .items
    )
    assert (record.raw_count, record.read_count) == (5, 2)
    dumped = record.skip_reasons.model_dump()
    assert dumped["missing_required_posting_identity"] == 3
    # The adapter's two accounted-for buckets close the raw-to-read gap.
    assert (
        dumped["missing_required_posting_identity"] + dumped["bounded_result_cap"]
        == record.raw_count - record.read_count
    )


def test_scan_history_exposes_source_settings_and_recorded_results(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    board_source(
        durable_database,
        workspace_id,
        "Acme",
        "https://boards.greenhouse.io/acme",
        priority=3,
    )
    state_target_titles(durable_database, workspace_id, "data")
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards(
            {"Acme": [source_listing("Data Platform Lead", "https://example.test/1")]}
        ),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    service.scan(workspace_id)

    [record] = service.list_scan_history(workspace_id).items
    assert record.source_name == "Acme"
    assert record.employer == "Acme"
    assert record.source_url == "https://boards.greenhouse.io/acme"
    assert record.source_type == "employer_board"
    assert (record.priority, record.enabled) == (3, True)
    assert record.scanned_on == utc_now().date()
    assert (record.query_label, record.status) == ("title-filter", "completed")
    assert (record.result_count, record.new_count, record.notes) == (1, 1, "")


def test_a_rescan_counts_a_reset_archived_row_as_new_and_names_it_requeued(
    durable_database: Database,
) -> None:
    """A requeued row is queue work: it counts as new, and reports apart from an insert."""
    workspace_id = scan_workspace(durable_database)
    board_source(durable_database, workspace_id, "Globex", "https://boards.greenhouse.io/globex")
    state_target_titles(durable_database, workspace_id, "data")
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards(
            {"Globex": [source_listing("Data Platform Lead", "https://example.test/1")]}
        ),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.scan(workspace_id)
    with durable_database.session() as session:
        staged_id = session.scalar(select(PendingDiscovery.id))
    assert staged_id is not None
    mark_reset_archived(durable_database, workspace_id, staged_id)

    result = service.scan(workspace_id)

    [scan_row] = result["scanned"]
    assert (scan_row["new"], scan_row["requeued"]) == (1, 1)
    assert (result["new_discoveries"], result["requeued_discoveries"]) == (1, 1)
    with durable_database.session() as session:
        assert session.scalars(select(PendingDiscovery.id)).all() == [staged_id]
    assert service.list_scan_history(workspace_id).items[0].new_count == 1


def test_scan_history_lists_a_sources_latest_same_day_scan_before_the_one_it_superseded(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    source_id = board_source(
        durable_database, workspace_id, "Globex", "https://jobs.example/globex"
    )
    started = utc_now()
    # The superseded scan's id sorts first, so an id tiebreak alone lists it on top.
    board_scan = "00000000-0000-4000-8000-000000000000"
    guided_scan = "ffffffff-ffff-4fff-bfff-ffffffffffff"
    with durable_database.session() as session:
        for scan_id, query_label, completed in (
            (board_scan, "title-filter", started),
            (guided_scan, "not-a-board-scan", started + timedelta(minutes=5)),
        ):
            session.add(
                SourceScan(
                    id=scan_id,
                    source_id=source_id,
                    scanned_on=started.date(),
                    query_label=query_label,
                    status="completed",
                    read_outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                    raw_count=0,
                    read_count=0,
                    skip_reasons={},
                    result_count=0,
                    new_count=0,
                    notes="",
                    scan_started_at=started,
                    scan_completed_at=completed,
                    duration_ms=0,
                )
            )

    history = SourceScanQueries(durable_database).list_scan_history(workspace_id)

    assert [record.id for record in history.items] == [guided_scan, board_scan]


def test_scan_history_names_why_it_keeps_each_scan_per_kind_of_read(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    source_id = board_source(
        durable_database, workspace_id, "Globex", "https://jobs.example/globex"
    )
    started = utc_now()
    board_scan = "00000000-0000-4000-8000-000000000001"
    later_board_scan = "00000000-0000-4000-8000-000000000002"
    guided_scan = "00000000-0000-4000-8000-000000000003"
    with durable_database.session() as session:
        for scan_id, query_label, day, complete in (
            (board_scan, "title-filter", -1, True),
            (later_board_scan, "title-filter", 0, False),
            (guided_scan, "not-a-board-scan", 1, True),
        ):
            when = started + timedelta(days=day)
            session.add(
                SourceScan(
                    id=scan_id,
                    source_id=source_id,
                    scanned_on=when.date(),
                    query_label=query_label,
                    status="completed",
                    read_outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                    raw_count=0,
                    read_count=0,
                    skip_reasons={},
                    result_count=0,
                    new_count=0,
                    notes="",
                    complete=complete,
                    scan_started_at=when,
                    scan_completed_at=when,
                    duration_ms=0,
                )
            )

    history = SourceScanQueries(durable_database).list_scan_history(workspace_id)

    kept = {record.id: (record.kind, record.kept_as) for record in history.items}
    assert kept == {
        guided_scan: ("other", "latest_and_complete"),
        later_board_scan: ("title-filter", "latest"),
        board_scan: ("title-filter", "latest_complete"),
    }


def test_scan_history_lists_only_each_sources_latest_and_latest_complete_scan(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    source_id = board_source(
        durable_database, workspace_id, "Globex", "https://jobs.example/globex"
    )
    now = utc_now()
    scan_ids = {day: f"0000000{-day}-0000-4000-8000-000000000000" for day in (-3, -2, -1, 0)}
    with durable_database.session() as session:
        for day, complete in ((-3, True), (-2, True), (-1, False), (0, False)):
            when = now + timedelta(days=day)
            session.add(
                SourceScan(
                    id=scan_ids[day],
                    source_id=source_id,
                    scanned_on=when.date(),
                    query_label="title-filter",
                    status="completed" if complete else "incomplete",
                    read_outcome=SourceReadOutcome.PUBLISHER_INDEX,
                    raw_count=0,
                    read_count=0,
                    skip_reasons={},
                    result_count=0,
                    new_count=0,
                    notes="",
                    complete=complete,
                    scan_started_at=when,
                    scan_completed_at=when,
                    duration_ms=0,
                )
            )

    history = SourceScanQueries(durable_database).list_scan_history(workspace_id)

    assert [record.id for record in history.items] == [scan_ids[0], scan_ids[-2]]
    assert history.total == 2


def test_only_finished_campaign_runs_advance_the_search_plan(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    with durable_database.session() as session:
        for key, task_type, status in (
            ("campaign-done", "search.guided_campaign", "completed"),
            ("campaign-failed", "search.guided_campaign", "failed"),
            ("vocabulary-done", "search.vocabulary", "completed"),
        ):
            session.add(
                BackgroundTask(
                    workspace_id=workspace_id,
                    task_type=task_type,
                    status=status,
                    payload={},
                    payload_sha256="0" * 64,
                    idempotency_key=key,
                )
            )
        session.commit()

    runs = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({}),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).completed_guided_web_runs(workspace_id)

    assert runs == 1
