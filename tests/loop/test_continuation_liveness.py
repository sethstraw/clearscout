"""A continuation chain survives failures, rescans, and title decisions, then settles."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from threading import Event

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from clearscout.application.discovery.board_scan import SourceScanService
from clearscout.application.discovery.proposals import SearchTitleProposalService
from clearscout.application.discovery.scan_outcome import INCOMPLETE_READ_NOTE
from clearscout.application.discovery.scan_sweep import BoardSweep
from clearscout.application.discovery.source_continuation import (
    MANUAL_RECOVERY_NOTE,
    MAX_AUTOMATIC_ATTEMPTS,
    POSTING_DETAIL_COMPLETED,
    SourceContinuationCommands,
)
from clearscout.application.discovery.source_reading import (
    SourceContinuationTarget,
    SourceDetailSummary,
    SourceRead,
)
from clearscout.application.tasks import TaskQueueService
from clearscout.application.workspaces import WorkspaceService
from clearscout.bootstrap.worker import Worker, default_handlers
from clearscout.domain import SearchProposalState, SourceReadOutcome, TaskStatus, new_id, utc_now
from clearscout.domain.identity import normalize_text
from clearscout.domain.statements import derived_proposal_identity
from clearscout.domain.task_types import TaskType
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    BackgroundTask,
    CandidateProfile,
    DerivedSearchTitle,
    Employer,
    MasterCvVersion,
    Source,
    SourceScan,
    SourceScanFrontierItem,
)
from tests.support.artifacts import runtime_storage
from tests.support.doubles import UnexpectedPostingFetcher
from tests.support.scenarios import seed_scan_continuation

BOARD = "https://jobs.example/globex"


@pytest.mark.parametrize("task_type", ["source.scan", "source.continue"])
@pytest.mark.parametrize("boundary", ["new", "refresh", "holdout_refresh", "holdout_dismiss"])
def test_cancellation_while_staging_waits_prevents_source_writes(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch, task_type: str, boundary: str
) -> None:
    from collections.abc import Iterator
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager

    from clearscout.application.discovery.board_scan import SourceScanHandler
    from clearscout.application.discovery.scan_staging import DiscoveryStaging
    from clearscout.application.discovery.source_continuation import SourceContinuationHandler
    from clearscout.application.discovery.source_reading import SourceListing
    from clearscout.application.tasks import TaskHandler
    from clearscout.infrastructure.persistence.models import (
        PendingDiscovery,
        PendingDiscoveryDispositionEvent,
    )
    from clearscout.infrastructure.web_fetch.listing_facts import published_listing_facts
    from tests.support.scenarios import (
        published_location_facts,
        state_single_market,
        state_target_titles,
    )

    workspace_id, source_id = _workspace_with_source(durable_database)
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    held_out = boundary.startswith("holdout")
    if held_out:
        state_single_market(
            durable_database,
            workspace_id,
            "Data Platform Lead",
            key="us_remote",
            country="United States",
        )
    target = f"{BOARD}/retained"
    read = SourceRead(
        listings=[
            SourceListing(
                employer="Globex",
                title="Data Platform Lead",
                url=target,
                location="Amsterdam, Netherlands" if held_out else "Remote",
                facts=published_listing_facts("greenhouse", locations="Amsterdam, Netherlands")
                if held_out
                else published_location_facts("Remote"),
            )
        ],
        complete=True,
        raw_count=1,
        outcome=SourceReadOutcome.ATS_COMPLETE,
        attempted_target_urls=(target,),
        detail=SourceDetailSummary(attempted=2, read=1, interrupted=1, capped=3),
    )

    class Board(_DrainingBoard):
        def continue_listings(self, *args: object, **kwargs: object) -> SourceRead:
            return read

    board = Board(read)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=board,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    if boundary != "new":
        service.staging.stage_listings(
            workspace_id,
            [SourceListing(employer="Globex", title="Publisher prior title", url=target)],
            utc_now().date(),
            reason="Board scan matched the title filter.",
            source_id=source_id,
        )
    queue = TaskQueueService(durable_database)
    handler: TaskHandler
    if task_type == "source.continue":
        _scan(durable_database, workspace_id, _DrainingBoard(_deferring_read(_target(target))))
        [task] = _continue_tasks(durable_database)
        task_id = task.id
        handler = SourceContinuationHandler(durable_database, board, service.sweep)
    else:
        task_id = str(
            queue.enqueue(
                workspace_id=workspace_id,
                task_type=task_type,
                payload={"source_ids": [source_id]},
                idempotency_key="staging-root",
            ).id
        )
        handler = SourceScanHandler(
            durable_database,
            storage=runtime_storage(durable_database),
            fetcher=board,
            posting_fetcher=UnexpectedPostingFetcher(),
        )
    entered = Event()
    release = Event()
    original_turn = DiscoveryStaging.write_turn
    blocked_turn = {"new": 1, "refresh": 1, "holdout_refresh": 2, "holdout_dismiss": 3}[boundary]
    turns = 0

    @contextmanager
    def held_turn(staging: DiscoveryStaging, wait_seconds: float = float("inf")) -> Iterator[None]:
        nonlocal turns
        turns += 1
        if turns == blocked_turn:
            entered.set()
            release.wait()
        with original_turn(staging, wait_seconds):
            yield

    monkeypatch.setattr(DiscoveryStaging, "write_turn", held_turn)
    worker = Worker(durable_database, worker_id="staging-reader", handlers={task_type: handler})

    def run() -> bool:
        try:
            return worker.run_once()
        finally:
            entered.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(run)
        entered.wait()
        try:
            assert turns >= blocked_turn, queue.get(task_id).progress
            with durable_database.session() as session:
                before = [
                    (row.id, row.title, row.location, row.staged_facts, row.first_seen_on)
                    for row in session.scalars(select(PendingDiscovery))
                ]
                detail_before = [
                    (row.id, row.detail_attempted_count, row.detail_interrupted_count)
                    for row in session.scalars(select(SourceScan))
                ]
            queue.request_cancellation(task_id)
        finally:
            release.set()
        assert running.result()
    finished = queue.get(task_id)
    assert finished.status == TaskStatus.CANCELLED
    assert finished.error_category is None
    assert finished.error_message is None
    with durable_database.session() as session:
        assert [
            (row.id, row.title, row.location, row.staged_facts, row.first_seen_on)
            for row in session.scalars(select(PendingDiscovery))
        ] == before
        assert list(session.scalars(select(PendingDiscoveryDispositionEvent))) == []
        assert [
            (row.id, row.detail_attempted_count, row.detail_interrupted_count)
            for row in session.scalars(select(SourceScan))
        ] == detail_before


def _target(url: str, **changes: object) -> SourceContinuationTarget:
    target = SourceContinuationTarget(
        url=url,
        label="Published role",
        label_source="visible",
        source_url=f"{BOARD}/careers",
        structural_context="main",
        relations=("publisher-content", "substantive-label"),
    )
    return replace(target, **changes)  # type: ignore[arg-type]


def _deferring_read(
    *targets: SourceContinuationTarget,
    exhausted_outcome: SourceReadOutcome | None = None,
    discovery_truncated: bool = False,
    skip_reasons: dict[str, int] | None = None,
) -> SourceRead:
    return SourceRead(
        listings=[],
        complete=False,
        raw_count=0,
        outcome=SourceReadOutcome.CRAWL_CONTINUATION,
        skip_reasons=skip_reasons or {},
        deferred_targets=targets,
        discovery_truncated=discovery_truncated,
        exhausted_outcome=exhausted_outcome,
    )


def _finished_read(targets: tuple[SourceContinuationTarget, ...]) -> SourceRead:
    urls = tuple(target.url for target in targets)
    return SourceRead(
        listings=[],
        complete=False,
        raw_count=len(targets),
        outcome=SourceReadOutcome.PUBLISHER_LINKED_PAGES,
        attempted_target_urls=urls,
    )


def _workspace_with_source(database: Database) -> tuple[str, str]:
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database)).create(slug="chain", name="Chain").id
    )
    source_id = new_id()
    with database.session() as session:
        employer = Employer(
            workspace_id=workspace_id,
            name="Globex",
            normalized_name=normalize_text("Globex"),
            careers_url=BOARD,
        )
        session.add(employer)
        session.flush()
        session.add(
            Source(
                id=source_id,
                workspace_id=workspace_id,
                employer_id=employer.id,
                name="Globex",
                source_type="employer_board",
                url=BOARD,
                priority=1,
                enabled=True,
            )
        )
    return workspace_id, source_id


def _scan(database: Database, workspace_id: str, board: object) -> None:
    SourceScanService(
        database,
        storage=runtime_storage(database),
        fetcher=board,  # type: ignore[arg-type]
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)


def _drain(database: Database, board: object, limit: int = 12) -> None:
    worker = Worker(
        database,
        worker_id="chain-worker",
        handlers=default_handlers(database, fetcher=board),  # type: ignore[arg-type]
    )
    for _ in range(limit):
        if not worker.run_once():
            return
    raise AssertionError("the continuation chain did not drain")


def _rows_by_url(database: Database) -> dict[str, SourceScanFrontierItem]:
    with database.session() as session:
        rows = list(session.scalars(select(SourceScanFrontierItem)))
        for row in rows:
            session.expunge(row)
    return {row.url: row for row in rows}


def _only_scan(database: Database) -> SourceScan:
    with database.session() as session:
        [scan] = session.scalars(select(SourceScan)).all()
        session.expunge(scan)
    return scan


def test_a_target_whose_read_raises_is_charged_while_the_chain_reads_the_rest(
    durable_database: Database,
) -> None:
    workspace_id, _source_id = _workspace_with_source(durable_database)
    poison, good = f"{BOARD}/poison", f"{BOARD}/good"

    class RaisingBoard:
        def listings(
            self,
            _employer: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return _deferring_read(_target(poison), _target(good))

        def continue_listings(
            self,
            _employer: str,
            targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            if any(target.url == poison for target in targets):
                raise RuntimeError("publisher parser crashed")
            return _finished_read(targets)

    board = RaisingBoard()
    _scan(durable_database, workspace_id, board)
    _drain(durable_database, board)

    rows = _rows_by_url(durable_database)
    assert rows[good].status == "completed"
    assert rows[poison].status == "failed"
    assert rows[poison].attempt_count == MAX_AUTOMATIC_ATTEMPTS
    assert "publisher parser crashed" in rows[poison].last_error


def _continue_tasks(database: Database) -> list[BackgroundTask]:
    with database.session() as session:
        tasks = list(
            session.scalars(
                select(BackgroundTask)
                .where(BackgroundTask.task_type == "source.continue")
                .order_by(BackgroundTask.created_at)
            )
        )
        for task in tasks:
            session.expunge(task)
    return tasks


def test_a_continuation_cancelled_mid_read_stops_its_chain_without_charging_its_rows(
    durable_database: Database,
) -> None:
    workspace_id, _source_id = _workspace_with_source(durable_database)
    url = f"{BOARD}/role"
    queue = TaskQueueService(durable_database)

    class CancelledBoard:
        def listings(
            self,
            _employer: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return _deferring_read(_target(url))

        def continue_listings(
            self,
            _employer: str,
            targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            with durable_database.session() as session:
                running = session.scalar(
                    select(BackgroundTask.id).where(
                        BackgroundTask.status == TaskStatus.RUNNING.value
                    )
                )
            assert running is not None
            queue.request_cancellation(running)
            return _finished_read(targets)

    board = CancelledBoard()
    _scan(durable_database, workspace_id, board)
    Worker(
        durable_database,
        worker_id="cancel-worker",
        handlers=default_handlers(durable_database, fetcher=board),
    ).run_once()

    assert [task.status for task in _continue_tasks(durable_database)] == [
        TaskStatus.CANCELLED.value
    ]
    row = _rows_by_url(durable_database)[url]
    assert (row.status, row.attempt_count, row.automatic_eligible) == ("pending", 0, True)


def test_a_database_lock_while_staging_rearms_the_chain_without_charging_its_rows(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id, _source_id = _workspace_with_source(durable_database)
    url = f"{BOARD}/role"

    class ReadableBoard:
        def listings(
            self,
            _employer: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return _deferring_read(_target(url))

        def continue_listings(
            self,
            _employer: str,
            targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return _finished_read(targets)

    def locked(*_args: object, **_kwargs: object) -> dict[str, int]:
        raise OperationalError("INSERT", {}, Exception("database is locked"))

    board = ReadableBoard()
    _scan(durable_database, workspace_id, board)
    monkeypatch.setattr(BoardSweep, "stage_continuation", locked)
    Worker(
        durable_database,
        worker_id="locked-worker",
        handlers=default_handlers(durable_database, fetcher=board),
    ).run_once()

    tasks = _continue_tasks(durable_database)
    assert [task.status for task in tasks] == [TaskStatus.FAILED.value, TaskStatus.QUEUED.value]
    assert tasks[1].payload["no_progress_count"] == 0
    row = _rows_by_url(durable_database)[url]
    assert (row.status, row.attempt_count) == ("pending", 0)


def test_a_continuation_abandoned_at_its_time_limit_is_charged_once_and_rearmed_once(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from threading import Thread

    workspace_id, _source_id = _workspace_with_source(durable_database)
    url = f"{BOARD}/slow"
    release = Event()
    woke = Event()
    reader_entered = Event()
    reader_or_handler_done = Event()
    now = [0.0]
    original_join = Thread.join
    original_run = Thread.run

    def run_until_finished(thread: Thread) -> None:
        try:
            original_run(thread)
        finally:
            if thread.name.startswith("clearscout-task-"):
                reader_or_handler_done.set()

    def join_after_entry_or_completion(thread: Thread, timeout: float | None = None) -> None:
        if thread.name.startswith("clearscout-task-"):
            reader_or_handler_done.wait()
            original_join(thread, timeout=0)
        else:
            original_join(thread, timeout=timeout)

    monkeypatch.setattr(Thread, "run", run_until_finished)
    monkeypatch.setattr(Thread, "join", join_after_entry_or_completion)

    class SlowBoard:
        def listings(
            self,
            _employer: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return _deferring_read(_target(url))

        def continue_listings(
            self,
            _employer: str,
            targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            now[0] = 60.0
            reader_entered.set()
            reader_or_handler_done.set()
            release.wait()
            woke.set()
            return _finished_read(targets)

    board = SlowBoard()
    _scan(durable_database, workspace_id, board)
    worker = Worker(
        durable_database,
        worker_id="abandoning-worker",
        handlers=default_handlers(durable_database, fetcher=board),
        handler_time_limits={"source.continue": 0.1},
        clock=lambda: now[0],
    )

    def settled() -> tuple[list[tuple[str, str | None]], tuple[str, int]]:
        row = _rows_by_url(durable_database)[url]
        tasks = [(task.status, task.error_category) for task in _continue_tasks(durable_database)]
        return tasks, (row.status, row.attempt_count)

    expected = (
        [(TaskStatus.FAILED.value, "handler_time_limit"), (TaskStatus.QUEUED.value, None)],
        ("pending", 1),
    )
    try:
        assert worker.run_once()
        assert reader_entered.is_set()
        assert not woke.is_set()
        assert settled() == expected
    finally:
        release.set()
        abandoned_thread = worker._abandoned_thread
        if abandoned_thread is not None:
            original_join(abandoned_thread)
    assert abandoned_thread is not None
    assert woke.is_set()
    assert not abandoned_thread.is_alive()
    assert settled() == expected
    assert "time limit" in _rows_by_url(durable_database)[url].last_error


def test_a_same_day_rescan_rereads_board_pages_its_finished_chain_already_read(
    durable_database: Database,
) -> None:
    workspace_id, _source_id = _workspace_with_source(durable_database)
    page = f"{BOARD}/jobs?offset=20"

    class PagedBoard:
        def __init__(self) -> None:
            self.page_reads: list[tuple[str, ...]] = []

        def listings(
            self,
            _employer: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return _deferring_read(_target(page, deferred_reason="workday_page_pending"))

        def continue_listings(
            self,
            _employer: str,
            targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            self.page_reads.append(tuple(target.url for target in targets))
            return _finished_read(targets)

    board = PagedBoard()
    _scan(durable_database, workspace_id, board)
    _drain(durable_database, board)
    _scan(durable_database, workspace_id, board)
    _drain(durable_database, board)

    assert board.page_reads == [(page,), (page,)]


def test_accepting_a_title_rereads_a_posting_completed_within_the_rescreen_age(
    durable_database: Database,
) -> None:
    workspace_id, source_id = _workspace_with_source(durable_database)
    url = f"{BOARD}/posting-1"
    title_id = new_id()
    with durable_database.session() as session:
        profile = CandidateProfile(workspace_id=workspace_id, display_name="Candidate")
        session.add(profile)
        session.flush()
        version = MasterCvVersion(
            candidate_profile_id=profile.id,
            content_sha256="a" * 64,
            source_uri="file:///cv.md",
            effective_at=utc_now(),
            byte_size=1,
        )
        session.add(version)
        session.flush()
        session.add(
            DerivedSearchTitle(
                id=title_id,
                workspace_id=workspace_id,
                master_cv_version_id=version.id,
                title="Data Steward",
                normalized_title=normalize_text("Data Steward"),
                reason="proposed",
                evidence_ids=["evidence-1"],
                lane=None,
                position=0,
                state=SearchProposalState.PROPOSED.value,
                origin="test",
                correlation_id=None,
                carried_identity=derived_proposal_identity(normalize_text("Data Steward")),
            )
        )
    earlier_scan, rescan = (
        _scan_row(durable_database, source_id, -1),
        _scan_row(durable_database, source_id, 0),
    )
    with durable_database.session() as session:
        seed_scan_continuation(
            session,
            workspace_id,
            earlier_scan,
            [
                {
                    "url": url,
                    "source_url": f"{BOARD}/careers",
                    "deferred_reason": POSTING_DETAIL_COMPLETED,
                    "status": "completed",
                    "attempt_count": 1,
                    "completed_at": utc_now() - timedelta(hours=2),
                }
            ],
            origin="source.scan",
        )

    SearchTitleProposalService(durable_database).accept(workspace_id, title_id)
    with durable_database.session() as session:
        result = SourceContinuationCommands(durable_database).replace_frontier_in_session(
            session,
            workspace_id=workspace_id,
            source_scan_id=rescan,
            targets=(_target(url),),
            correlation_id=None,
            carry_forward=True,
            handoff_scopes=(),
        )

    assert result.pending_count == 1


def _scan_row(database: Database, source_id: str, day: int) -> str:
    scan_id = new_id()
    now = utc_now() + timedelta(days=day)
    with database.session() as session:
        session.add(
            SourceScan(
                id=scan_id,
                source_id=source_id,
                scanned_on=now.date(),
                query_label="title-filter",
                status="incomplete",
                read_outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                raw_count=0,
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
    return scan_id


class _DrainingBoard:
    """Defers the given first read and reads every continuation target to completion."""

    def __init__(self, first_read: SourceRead) -> None:
        self.first_read = first_read

    def listings(
        self,
        _employer: str,
        _url: str,
        *,
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        return self.first_read

    def continue_listings(
        self,
        _employer: str,
        targets: tuple[SourceContinuationTarget, ...],
        *,
        root_url: str = "",
        handoff_scopes: tuple[tuple[str, str, str], ...] = (),
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        return _finished_read(targets)


def _scan_and_drain(database: Database, first_read: SourceRead) -> SourceScan:
    workspace_id, _source_id = _workspace_with_source(database)
    board = _DrainingBoard(first_read)
    _scan(database, workspace_id, board)
    assert _only_scan(database).status == "incomplete"
    _drain(database, board)
    return _only_scan(database)


def test_a_drained_chain_settles_its_scan_under_the_outcome_its_adapter_declared(
    durable_database: Database,
) -> None:
    scan = _scan_and_drain(
        durable_database,
        _deferring_read(
            _target(f"{BOARD}/a"),
            _target(f"{BOARD}/b"),
            exhausted_outcome=SourceReadOutcome.PUBLISHER_INDEX,
        ),
    )

    assert (scan.status, scan.read_outcome) == ("completed_empty", "publisher_index")
    assert INCOMPLETE_READ_NOTE not in scan.notes
    assert scan.notes.endswith("this scan covers its discovered set.")
    assert scan.complete is True


def test_a_chain_leaving_only_manual_links_keeps_its_scan_incomplete_and_says_why(
    durable_database: Database,
) -> None:
    manual = _target(
        f"{BOARD}/manual", automatic_eligible=False, deferred_reason="low_priority_navigation"
    )
    scan = _scan_and_drain(
        durable_database,
        _deferring_read(
            _target(f"{BOARD}/a"), manual, exhausted_outcome=SourceReadOutcome.PUBLISHER_INDEX
        ),
    )

    assert scan.status == "incomplete"
    assert INCOMPLETE_READ_NOTE in scan.notes
    assert MANUAL_RECOVERY_NOTE in scan.notes


def test_a_drained_chain_whose_discovery_was_truncated_stays_incomplete(
    durable_database: Database,
) -> None:
    truncated = _scan_and_drain(
        durable_database,
        _deferring_read(
            _target(f"{BOARD}/a"),
            exhausted_outcome=SourceReadOutcome.PUBLISHER_INDEX,
            discovery_truncated=True,
        ),
    )
    assert (truncated.status, truncated.discovery_truncated) == ("incomplete", True)


def test_a_drained_chain_whose_own_read_hit_its_time_limit_stays_incomplete(
    durable_database: Database,
) -> None:
    scan = _scan_and_drain(
        durable_database,
        _deferring_read(
            _target(f"{BOARD}/a"),
            exhausted_outcome=SourceReadOutcome.PUBLISHER_INDEX,
            skip_reasons={"time_limit_reached": 1},
        ),
    )

    assert (scan.status, scan.read_outcome) == ("incomplete", "crawl_continuation")
    assert INCOMPLETE_READ_NOTE in scan.notes


def test_a_drained_chain_from_an_adapter_declaring_no_exhausted_outcome_stays_incomplete(
    durable_database: Database,
) -> None:
    scan = _scan_and_drain(durable_database, _deferring_read(_target(f"{BOARD}/a")))

    assert scan.status == "incomplete"
    assert MANUAL_RECOVERY_NOTE not in scan.notes


def test_a_same_day_rescan_keeps_pending_work_and_retries_a_failed_link_exactly_once(
    durable_database: Database,
) -> None:
    workspace_id, source_id = _workspace_with_source(durable_database)
    scan_id = _scan_row(durable_database, source_id, 0)
    pending, failed = f"{BOARD}/pending", f"{BOARD}/failed"
    commands = SourceContinuationCommands(durable_database)

    def rescan() -> dict[str, SourceScanFrontierItem]:
        with durable_database.session() as session:
            commands.replace_frontier_in_session(
                session,
                workspace_id=workspace_id,
                source_scan_id=scan_id,
                targets=(),
                correlation_id=None,
                carry_forward=True,
                handoff_scopes=(),
            )
        return _rows_by_url(durable_database)

    with durable_database.session() as session:
        commands.replace_frontier_in_session(
            session,
            workspace_id=workspace_id,
            source_scan_id=scan_id,
            targets=(_target(pending), _target(failed)),
            correlation_id=None,
            carry_forward=True,
            handoff_scopes=(),
        )
    with durable_database.session() as session:
        row = session.scalar(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.url == failed)
        )
        assert row is not None
        row.status, row.attempt_count = "failed", MAX_AUTOMATIC_ATTEMPTS

    first = rescan()
    assert (first[pending].status, first[pending].automatic_eligible) == ("pending", True)
    assert (first[failed].status, first[failed].attempt_count) == ("pending", 0)

    with durable_database.session() as session:
        row = session.get(SourceScanFrontierItem, first[failed].id)
        assert row is not None
        row.status, row.attempt_count = "failed", MAX_AUTOMATIC_ATTEMPTS

    second = rescan()
    assert second[pending].status == "pending"
    assert failed not in second


def test_cancelling_a_completed_batch_stops_its_children_and_preserves_the_frontier(
    durable_database: Database,
) -> None:
    from clearscout.infrastructure.persistence.models import SourceScanMember

    workspace_id, source_id = _workspace_with_source(durable_database)
    target = f"{BOARD}/retained"
    board = _DrainingBoard(_deferring_read(_target(target)))
    _scan(durable_database, workspace_id, board)
    queue = TaskQueueService(durable_database)
    unrelated = queue.enqueue(
        workspace_id=workspace_id,
        task_type="discovery.rescreen_queue",
        payload={},
        idempotency_key="unrelated",
    )
    with durable_database.session() as session:
        member = session.scalars(
            select(SourceScanMember).where(SourceScanMember.source_id == source_id)
        ).one()
        root_id = member.root_task_id
        original_rows = [
            (row.id, row.status, row.attempt_count)
            for row in session.scalars(select(SourceScanFrontierItem))
        ]
    queue.request_cancellation(root_id)
    assert all(
        task.status == TaskStatus.CANCELLED.value for task in _continue_tasks(durable_database)
    )
    assert queue.get(str(unrelated.id)).status == TaskStatus.QUEUED.value
    with durable_database.session() as session:
        assert [
            (row.id, row.status, row.attempt_count)
            for row in session.scalars(select(SourceScanFrontierItem))
        ] == original_rows
        member = session.scalars(
            select(SourceScanMember).where(SourceScanMember.root_task_id == root_id)
        ).one()
        assert member.state == "cancelled"
        assert member.released_at is not None


def test_retrying_a_cancelled_continuation_requires_resume_without_enqueuing_stale_work(
    durable_database: Database,
) -> None:
    from clearscout.application.failures import ValidationError

    workspace_id, _source_id = _workspace_with_source(durable_database)
    board = _DrainingBoard(_deferring_read(_target(f"{BOARD}/retained")))
    _scan(durable_database, workspace_id, board)
    queue = TaskQueueService(durable_database)
    [child] = _continue_tasks(durable_database)
    queue.request_cancellation(str(child.id))
    with pytest.raises(ValidationError, match="Resume the source batch or scan again"):
        queue.retry(str(child.id))
    assert [task.status for task in _continue_tasks(durable_database)] == [
        TaskStatus.CANCELLED.value
    ]


def test_first_manual_continuation_admits_ownership_before_competing_scan(
    durable_database: Database,
) -> None:
    from clearscout.infrastructure.persistence.models import (
        SourceScanContinuation,
        SourceScanMember,
    )
    from tests.support.scenarios import state_target_titles

    workspace_id, source_id = _workspace_with_source(durable_database)
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    scan_id = _scan_row(durable_database, source_id, 0)
    with durable_database.session() as session:
        continuation_id = seed_scan_continuation(
            session,
            workspace_id,
            scan_id,
            [{"url": f"{BOARD}/kept", "automatic_eligible": False}],
        )
    commands = SourceContinuationCommands(durable_database)
    first = commands.request_continuation(workspace_id, scan_id)
    queue = TaskQueueService(durable_database)
    competing = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [source_id]},
        idempotency_key="competing-scan",
    )
    with durable_database.session() as session:
        continuation = session.get(SourceScanContinuation, continuation_id)
        assert continuation is not None and continuation.batch_member_id is not None
        member = session.get(SourceScanMember, continuation.batch_member_id)
        assert member is not None
        assert member.root_task_id == first.id
        assert member.state == "queued" and member.released_at is None
        busy = session.scalars(
            select(SourceScanMember).where(SourceScanMember.root_task_id == competing.id)
        ).one()
        assert busy.state == "already_in_progress" and busy.released_at is not None
        [frontier] = session.scalars(select(SourceScanFrontierItem)).all()
        assert frontier.status == "pending" and frontier.attempt_count == 0
    queue.request_cancellation(str(first.id))
    resumed = commands.request_continuation(workspace_id, scan_id)
    assert resumed.id != first.id
    assert resumed.status == TaskStatus.QUEUED
    assert commands.request_continuation(workspace_id, scan_id).id == resumed.id


@pytest.mark.parametrize(
    ("successor", "process_id", "held_task", "task_type", "adopted"),
    [
        ("failed", None, False, "source.continue", True),
        ("queued", None, False, "source.continue", False),
        ("failed", 41, False, "source.continue", False),
        ("failed", None, True, "source.continue", False),
        ("running", None, False, "source.scan", False),
    ],
    ids=["stranded", "queued-successor", "live-process", "held-task", "running-root-scan"],
)
def test_a_continuing_holder_yields_its_source_only_when_nothing_can_continue_it(
    durable_database: Database,
    successor: str,
    process_id: int | None,
    held_task: bool,
    task_type: TaskType,
    adopted: bool,
) -> None:
    from clearscout.infrastructure.persistence.models import SourceScanMember
    from tests.support.scenarios import state_target_titles

    workspace_id, source_id = _workspace_with_source(durable_database)
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    scan_id = _scan_row(durable_database, source_id, 0)
    with durable_database.session() as session:
        seed_scan_continuation(session, workspace_id, scan_id, [{"url": f"{BOARD}/kept"}])
    first = SourceContinuationCommands(durable_database).request_continuation(workspace_id, scan_id)
    with durable_database.session() as session:
        holder = session.scalars(
            select(SourceScanMember).where(SourceScanMember.root_task_id == first.id)
        ).one()
        holder.state = "continuing"
        holder.active_process_id = process_id
        holder.active_process_started_at = "started" if process_id is not None else None
        holder.active_task_id = str(first.id) if held_task else None
        holder_id = holder.id
        task = session.get(BackgroundTask, str(first.id))
        assert task is not None
        task.status = successor
        task.task_type = task_type
    competing = TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [source_id]},
        idempotency_key="competing-scan",
    )
    with durable_database.session() as session:
        member = session.scalars(
            select(SourceScanMember).where(SourceScanMember.root_task_id == competing.id)
        ).one()
        old = session.get(SourceScanMember, holder_id)
        assert old is not None
        if adopted:
            assert member.state == "queued" and member.released_at is None
            assert old.released_at is not None and old.error_category == "reader_lost"
        else:
            assert member.state == "already_in_progress"
            assert old.released_at is None


def test_a_superseded_frontier_is_refused_before_task_admission(
    durable_database: Database,
) -> None:
    from clearscout.application.failures import ValidationError
    from clearscout.infrastructure.persistence.models import SourceScanMember

    workspace_id, source_id = _workspace_with_source(durable_database)
    old_scan = _scan_row(durable_database, source_id, -1)
    with durable_database.session() as session:
        continuation_id = seed_scan_continuation(
            session,
            workspace_id,
            old_scan,
            [{"url": f"{BOARD}/old"}],
        )
    _scan_row(durable_database, source_id, 0)
    with pytest.raises(ValidationError, match="latest"):
        TaskQueueService(durable_database).enqueue(
            workspace_id=workspace_id,
            task_type="source.continue",
            payload={"continuation_id": continuation_id, "generation": 1},
            idempotency_key="superseded-frontier",
        )
    with durable_database.session() as session:
        assert session.scalar(select(BackgroundTask.id)) is None
        assert session.scalar(select(SourceScanMember.id)) is None
        [frontier] = session.scalars(select(SourceScanFrontierItem)).all()
        assert frontier.status == "pending" and frontier.attempt_count == 0


def test_a_failed_read_release_preserves_ownership_until_restart_then_resume(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections.abc import Iterator
    from contextlib import contextmanager

    from sqlalchemy.orm import Session

    from clearscout.application.failures import ConflictError, UnavailableError
    from clearscout.infrastructure.persistence.models import SourceScanMember
    from tests.support.scenarios import state_target_titles

    workspace_id, source_id = _workspace_with_source(durable_database)
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    _scan(durable_database, workspace_id, _DrainingBoard(_deferring_read(_target(f"{BOARD}/kept"))))
    queue = TaskQueueService(durable_database)
    claimed = queue.claim_next(worker_id="reader")
    assert claimed is not None
    with durable_database.session() as session:
        member = session.scalars(
            select(SourceScanMember).where(SourceScanMember.source_id == source_id)
        ).one()
        root_id = member.root_task_id
        frontier_ids = list(session.scalars(select(SourceScanFrontierItem.id)))
    assert queue.commands.claim_source_read(
        member.id,
        claimed.id,
        claimed.attempt_count,
        process_id=41,
        process_started_at="original",
    )
    original_session = durable_database.exclusive_session

    @contextmanager
    def failed_commit() -> Iterator[Session]:
        with original_session() as session:
            yield session
            raise OperationalError("release unavailable", {}, RuntimeError("synthetic failure"))

    with monkeypatch.context() as release_failure:
        release_failure.setattr(durable_database, "exclusive_session", failed_commit)
        with pytest.raises(UnavailableError, match="Restart the worker.*Resume"):
            queue.commands.release_source_read(member.id, claimed.id, claimed.attempt_count)
    queue.finish(
        claimed.id,
        worker_id="reader",
        status=TaskStatus.FAILED,
        error_category="UnavailableError",
        error_message="Restart the worker, then Resume.",
        expected_attempt=claimed.attempt_count,
    )
    with pytest.raises(ConflictError, match="still in progress"):
        queue.resume_source_batch(workspace_id, root_id, "early")
    assert queue.commands.recover_dead_source_readers(lambda _pid, _start: True) == 0
    assert queue.commands.recover_dead_source_readers(lambda _pid, _start: False) == 1
    resumed = queue.resume_source_batch(workspace_id, root_id, "after-restart")
    with durable_database.session() as session:
        current = session.scalars(
            select(SourceScanMember).where(SourceScanMember.root_task_id == str(resumed.id))
        ).one()
        assert current.state == "queued"
        assert current.released_at is None
        assert current.continuation_id == member.continuation_id
        assert member.generation is not None
        assert current.generation == member.generation + 1
        assert list(session.scalars(select(SourceScanFrontierItem.id))) == frontier_ids


@pytest.mark.parametrize("remaining", ["none", "manual", "failed"])
def test_a_reader_dying_after_its_final_detail_releases_ownership_when_reclaimed(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch, remaining: str
) -> None:
    from typing import Any

    from clearscout.application.discovery.board_scan import SourceScanHandler
    from clearscout.application.discovery.source_continuation import SourceContinuationHandler
    from clearscout.application.discovery.source_reading import SourceListing
    from clearscout.application.tasks.leases import DEFAULT_TASK_LEASE_SECONDS
    from clearscout.infrastructure.persistence.models import (
        PendingDiscovery,
        SourceScanMember,
        SourceScanOutcome,
    )
    from tests.support.scenarios import published_location_facts, state_target_titles

    workspace_id, source_id = _workspace_with_source(durable_database)
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    url = f"{BOARD}/collected"
    targets = [_target(url)]
    if remaining != "none":
        targets.append(_target(f"{BOARD}/remaining", automatic_eligible=False))
    detail_reads: list[str] = []

    class Board:
        board_reads = 0

        def listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            self.board_reads += 1
            return _deferring_read(*targets)

        def continue_listings(
            self, employer: str, targets: tuple[SourceContinuationTarget, ...], **kwargs: Any
        ) -> SourceRead:
            detail_reads.extend(target.url for target in targets)
            return SourceRead(
                listings=[
                    SourceListing(
                        employer="Globex",
                        title="Data Platform Lead",
                        url=url,
                        location="Remote",
                        facts=published_location_facts("Remote"),
                    )
                ],
                complete=False,
                raw_count=1,
                outcome=SourceReadOutcome.PUBLISHER_LINKED_PAGES,
                attempted_target_urls=tuple(target.url for target in targets),
            )

    board = Board()
    _scan(durable_database, workspace_id, board)
    queue = TaskQueueService(durable_database)
    if remaining == "failed":
        with durable_database.session() as session:
            row = session.scalars(
                select(SourceScanFrontierItem).where(SourceScanFrontierItem.url == targets[-1].url)
            ).one()
            row.status = "failed"
            row.attempt_count = MAX_AUTOMATIC_ATTEMPTS
    scan_handler = SourceScanHandler(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=board,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    continuation_handler = SourceContinuationHandler(
        durable_database,
        board,
        scan_handler.service.sweep,
        lambda workspace, task_id, outcomes: scan_handler.service.record_task_outcomes(
            workspace, task_id, outcomes, origin="source.continue"
        ),
        scan_handler.service.begin_recording_pass,
    )
    worker = Worker(
        durable_database,
        worker_id="reader",
        handlers={"source.continue": continuation_handler, "source.scan": scan_handler},
        handler_time_limits={},
    )
    worker.queue = queue
    now = utc_now()
    monkeypatch.setattr("clearscout.application.tasks.leases.utc_now", lambda: now)
    monkeypatch.setattr(
        "clearscout.bootstrap.local_runtime.local_process_started_at", lambda _pid: "reader-start"
    )

    def crash_before_release(*args: Any, **kwargs: Any) -> None:
        raise SystemExit("reader died before releasing its source")

    with monkeypatch.context() as crash:
        crash.setattr(
            continuation_handler.commands.batch_commands,
            "release_source_read",
            crash_before_release,
        )
        with pytest.raises(SystemExit, match="before releasing"):
            worker.run_once()
    [task] = _continue_tasks(durable_database)
    assert task.status == TaskStatus.RUNNING.value
    with durable_database.session() as session:
        member = session.scalars(
            select(SourceScanMember).where(SourceScanMember.source_id == source_id)
        ).one()
        assert member.active_task_id == task.id and member.released_at is None
        frontier = [
            (row.id, row.status, row.attempt_count)
            for row in session.scalars(select(SourceScanFrontierItem))
        ]
        collected = list(session.scalars(select(PendingDiscovery.id)))
        outcomes = list(session.scalars(select(SourceScanOutcome.id)))
        assert len(collected) == 1 and len(outcomes) == 1
        assert _rows_by_url(durable_database)[url].status == "completed"

    assert queue.commands.recover_dead_source_readers(lambda _pid, _start: True) == 0
    assert queue.commands.recover_dead_source_readers(lambda _pid, _start: None) == 0
    assert queue.commands.recover_dead_source_readers(lambda _pid, _start: False) == 1
    now += timedelta(seconds=DEFAULT_TASK_LEASE_SECONDS + 1)
    assert worker.run_once()
    recovered = queue.get(task.id)
    assert recovered.status == TaskStatus.COMPLETED.value
    assert recovered.attempt_count == task.attempt_count + 1
    assert recovered.progress["pending"] == int(remaining == "manual")
    assert recovered.progress["failed"] == int(remaining == "failed")
    with durable_database.session() as session:
        released = session.get(SourceScanMember, member.id)
        assert released is not None
        assert released.active_task_id is None
        assert released.released_at is not None
        assert released.state == ("failed" if remaining == "failed" else "completed")
        assert [
            (row.id, row.status, row.attempt_count)
            for row in session.scalars(select(SourceScanFrontierItem))
        ] == frontier
        assert list(session.scalars(select(PendingDiscovery.id))) == collected
        assert list(session.scalars(select(SourceScanOutcome.id))) == outcomes
    fresh = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [source_id]},
        idempotency_key="fresh-after-reclaimed-reader",
    )
    with durable_database.session() as session:
        admitted = session.scalars(
            select(SourceScanMember).where(SourceScanMember.root_task_id == str(fresh.id))
        ).one()
        assert admitted.state == "queued" and admitted.released_at is None
    assert worker.run_once()
    assert queue.get(str(fresh.id)).status == TaskStatus.COMPLETED.value
    assert board.board_reads == 2
    assert detail_reads == [url]


@pytest.mark.parametrize("fresh", [False, True])
def test_resuming_a_cancelled_batch_reads_its_retained_frontier_without_rereading_the_board(
    durable_database: Database,
    fresh: bool,
) -> None:
    from clearscout.infrastructure.persistence.models import (
        SourceScanContinuation,
        SourceScanMember,
    )
    from tests.support.scenarios import state_target_titles

    workspace_id, source_id = _workspace_with_source(durable_database)
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    target = f"{BOARD}/retained"

    class ResumedBoard(_DrainingBoard):
        reads = 0

        def listings(
            self,
            employer: str,
            url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            self.reads += 1
            return super().listings(
                employer, url, employer_is_authoritative=employer_is_authoritative
            )

    board = ResumedBoard(_deferring_read(_target(target)))
    queue = TaskQueueService(durable_database)
    queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [source_id], "fresh_details": fresh},
        idempotency_key="resume-fresh",
    )
    assert Worker(
        durable_database,
        worker_id="chain-worker",
        handlers=default_handlers(durable_database, fetcher=board),
    ).run_once()
    with durable_database.session() as session:
        original = session.scalars(
            select(SourceScanMember).where(SourceScanMember.source_id == source_id)
        ).one()
        root_id = original.root_task_id
        old_generation = original.generation
        assert old_generation is not None
        frontier_ids = list(session.scalars(select(SourceScanFrontierItem.id)))
    queue.request_cancellation(root_id)
    resumed = queue.commands.resume_source_batch(workspace_id, root_id, "resume")
    assert resumed.id != root_id
    assert queue.commands.resume_source_batch(workspace_id, root_id, "resume").id == resumed.id
    with durable_database.session() as session:
        current = session.scalars(select(SourceScanContinuation)).one()
        assert current.generation == old_generation + 1
        assert list(session.scalars(select(SourceScanFrontierItem.id))) == frontier_ids
    _drain(durable_database, board)
    assert _rows_by_url(durable_database)[target].status == "completed"
    assert board.reads == 1
    assert [task.status for task in _continue_tasks(durable_database)] == [
        TaskStatus.CANCELLED.value,
        TaskStatus.COMPLETED.value,
    ]
    with durable_database.session() as session:
        roots = list(session.scalars(select(SourceScan).where(SourceScan.source_id == source_id)))
        assert len(roots) == 1
        original = session.scalars(
            select(SourceScanMember).where(SourceScanMember.root_task_id == root_id)
        ).one()
        assert original.state == "cancelled"


@pytest.mark.parametrize("owner", ["source", "employer"])
def test_a_retired_owner_stops_a_queued_fresh_continuation_before_any_read(
    durable_database: Database, owner: str
) -> None:
    from tests.support.scenarios import state_target_titles

    workspace_id, source_id = _workspace_with_source(durable_database)
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    target = f"{BOARD}/retained"

    class RetiredBoard(_DrainingBoard):
        continuation_reads = 0

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
            self.continuation_reads += 1
            return super().continue_listings(employer, targets, options=options)

    board = RetiredBoard(_deferring_read(_target(target)))
    TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [source_id], "fresh_details": True},
        idempotency_key="retired-owner",
    )
    worker = Worker(
        durable_database,
        worker_id="retired-owner",
        handlers=default_handlers(durable_database, fetcher=board),
    )
    assert worker.run_once()
    with durable_database.session() as session:
        source = session.get(Source, source_id)
        assert source is not None
        retired = source if owner == "source" else session.get(Employer, source.employer_id)
        assert retired is not None
        retired.archived_at = utc_now()
    assert worker.run_once()

    assert board.continuation_reads == 0
    row = _rows_by_url(durable_database)[target]
    assert row.status == "pending"
    assert row.attempt_count == 0
    assert row.completed_at is None
