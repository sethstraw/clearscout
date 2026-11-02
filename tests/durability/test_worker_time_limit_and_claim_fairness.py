"""A handler cannot hold the worker forever, and continuations share it fairly and politely."""

from __future__ import annotations

import threading
from datetime import timedelta
from threading import Barrier, Event, Lock
from typing import Any, cast
from urllib.parse import urlsplit

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from tailorcv.application.discovery.board_scan import SourceScanService
from tailorcv.application.discovery.content_resolution import PostingContentResolutionService
from tailorcv.application.discovery.funnel_audit import FunnelReason
from tailorcv.application.discovery.source_continuation import SourceContinuationHandler
from tailorcv.application.discovery.source_reading import (
    PostingCheck,
    SourceContinuationTarget,
    SourceListing,
    SourceRead,
)
from tailorcv.application.tasks import TaskContext, TaskQueueService
from tailorcv.application.tasks.leases import CONTINUATION_YIELD_LIMIT, DEFAULT_TASK_LEASE_SECONDS
from tailorcv.application.workspaces import WorkspaceService
from tailorcv.bootstrap.worker import Worker, default_handlers
from tailorcv.domain import SourceReadOutcome, TaskStatus, new_id, utc_now
from tailorcv.domain.artifacts import PostingSource
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    BackgroundTask,
    PendingDiscovery,
    PendingPostingContent,
    SourceScanContinuation,
    SourceScanFrontierItem,
    SourceScanOutcome,
)
from tailorcv.infrastructure.web_fetch.listing_facts import published_listing_facts
from tailorcv.infrastructure.web_fetch.posting_resolution import DeterministicPostingContentResolver
from tests.support.artifacts import runtime_storage
from tests.support.doubles import UnexpectedPostingFetcher
from tests.support.scenarios import board_source, completed_earlier_scan, state_target_titles


class _Clock:
    """A monotonic clock that moves only when the test advances it."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _workspace_id(database: Database) -> str:
    return str(
        WorkspaceService(database, runtime_storage(database)).create(slug="cap", name="Cap").id
    )


def _enqueue_verify(queue: TaskQueueService, workspace_id: str, key: str) -> str:
    return str(
        queue.enqueue(
            workspace_id=workspace_id,
            task_type="artifact.verify",
            payload={},
            idempotency_key=key,
        ).id
    )


def test_worker_fails_a_blocked_handler_at_its_time_limit_and_keeps_working(
    durable_database: Database,
) -> None:
    workspace_id = _workspace_id(durable_database)
    queue = TaskQueueService(durable_database)
    blocked = _enqueue_verify(queue, workspace_id, "blocked")
    clock = _Clock()
    release = Event()

    def block(context: TaskContext, payload: dict[str, object]) -> dict[str, object]:
        del context, payload
        clock.advance(60)
        release.wait(timeout=10)
        return {}

    worker = Worker(
        durable_database,
        worker_id="capped-worker",
        handlers={"artifact.verify": block},
        lease_heartbeat_seconds=0.01,
        handler_time_limits={"artifact.verify": 1},
        clock=clock,
    )
    try:
        assert worker.run_once()
    finally:
        release.set()

    failed = queue.get(blocked)
    assert failed.status == TaskStatus.FAILED
    assert failed.error_category == "handler_time_limit"


def test_a_handler_at_a_cancellation_point_stops_itself_at_the_time_limit(
    durable_database: Database,
) -> None:
    workspace_id = _workspace_id(durable_database)
    queue = TaskQueueService(durable_database)
    task_id = _enqueue_verify(queue, workspace_id, "cooperative")
    clock = _Clock()
    stopped = Event()

    def poll(context: TaskContext, payload: dict[str, object]) -> dict[str, object]:
        del payload
        # At the limit itself the grace heartbeat has not passed, so only the handler can stop.
        clock.advance(1)
        try:
            context.raise_if_cancelled()
        except Exception:
            stopped.set()
            raise
        return {}

    worker = Worker(
        durable_database,
        worker_id="cooperative-worker",
        handlers={"artifact.verify": poll},
        lease_heartbeat_seconds=0.01,
        handler_time_limits={"artifact.verify": 1},
        clock=clock,
    )
    assert worker.run_once()

    assert stopped.is_set()
    assert worker._abandoned_thread is None
    assert queue.get(task_id).error_category == "handler_time_limit"


def test_a_second_abandoned_handler_fails_its_task_and_stops_the_worker(
    durable_database: Database,
) -> None:
    workspace_id = _workspace_id(durable_database)
    queue = TaskQueueService(durable_database)
    first = _enqueue_verify(queue, workspace_id, "blocked-first")
    second = _enqueue_verify(queue, workspace_id, "blocked-second")
    clock = _Clock()
    release = Event()

    def block(context: TaskContext, payload: dict[str, object]) -> dict[str, object]:
        del context, payload
        clock.advance(60)
        release.wait(timeout=10)
        return {}

    worker = Worker(
        durable_database,
        worker_id="twice-capped-worker",
        handlers={"artifact.verify": block},
        lease_heartbeat_seconds=0.01,
        handler_time_limits={"artifact.verify": 1},
        clock=clock,
    )
    try:
        assert worker.run_once()
        with pytest.raises(SystemExit):
            worker.run_once()
    finally:
        release.set()

    assert [queue.get(task).error_category for task in (first, second)] == [
        "handler_time_limit",
        "handler_time_limit",
    ]


@pytest.mark.parametrize("prior_abandonment", [False, True])
def test_a_stale_abandoned_attempt_preserves_recovered_work_and_the_restart_rule(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch, prior_abandonment: bool
) -> None:
    workspace_id = _workspace_id(durable_database)
    queue = TaskQueueService(durable_database)
    first = (
        _enqueue_verify(queue, workspace_id, "earlier-abandonment") if prior_abandonment else None
    )
    task_id = _enqueue_verify(queue, workspace_id, "recovered-abandonment")
    clock = _Clock()
    release = Event()
    deadline_crossed = Event()
    stopped: list[Event] = []
    recovered: list[object] = []
    recovered_events: list[object] = []
    recovery_calls: list[str] = []
    original_join = threading.Thread.join

    def join_after_deadline(thread: threading.Thread, timeout: float | None = None) -> None:
        if thread.name.startswith("tailorcv-task-"):
            deadline_crossed.wait()
            original_join(thread, timeout=0)
        else:
            original_join(thread, timeout=timeout)

    monkeypatch.setattr(threading.Thread, "join", join_after_deadline)

    class Handler:
        def __call__(self, context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
            deadline_crossed.clear()
            ended = Event()
            stopped.append(ended)
            try:
                if context.task_id == task_id:
                    with durable_database.session() as session:
                        held = session.get(BackgroundTask, task_id)
                        assert held is not None
                        held.lease_expires_at = utc_now() - timedelta(seconds=1)
                    replacement = queue.claim_next(worker_id="reused-abandoned-worker")
                    assert replacement is not None and replacement.attempt_count == 2
                    recovered.append(queue.get(task_id))
                    recovered_events.extend(queue.events_after(task_id))
                clock.advance(60)
                deadline_crossed.set()
                release.wait()
                return {}
            finally:
                ended.set()

        def recover_abandoned(self, abandoned_id: str) -> None:
            recovery_calls.append(abandoned_id)
            if abandoned_id == task_id:
                queue.finish(task_id, worker_id="reused-abandoned-worker", status=TaskStatus.FAILED)

    worker = Worker(
        durable_database,
        worker_id="reused-abandoned-worker",
        handlers={"artifact.verify": Handler()},
        handler_time_limits={"artifact.verify": 1},
        clock=clock,
    )
    try:
        if prior_abandonment:
            assert worker.run_once()
            with pytest.raises(SystemExit, match="abandoned a second handler"):
                worker.run_once()
        else:
            assert worker.run_once()
    finally:
        release.set()
        for ended in stopped:
            ended.wait()

    assert recovery_calls == ([first] if first is not None else [])
    assert queue.get(task_id) == recovered[0]
    assert queue.events_after(task_id) == recovered_events


def test_a_time_limited_worker_refuses_a_heartbeat_of_half_the_lease() -> None:
    with pytest.raises(ValueError, match="half the task lease"):
        Worker(
            cast(Database, None),
            worker_id="slow-heartbeat",
            handlers={},
            lease_heartbeat_seconds=DEFAULT_TASK_LEASE_SECONDS / 2,
        )


def _aged_continuation(queue: TaskQueueService, database: Database, workspace_id: str) -> str:
    identity = new_id()
    task_id = _board_continuation(
        queue, database, workspace_id, f"Acme {identity}", f"https://example.test/{identity}/jobs"
    )
    with database.session() as session:
        waiting = session.get(BackgroundTask, task_id)
        assert waiting is not None
        waiting.created_at = utc_now() - CONTINUATION_YIELD_LIMIT - timedelta(minutes=1)
    return task_id


def _board_continuation(
    queue: TaskQueueService, database: Database, workspace_id: str, name: str, url: str
) -> str:
    """Queue a continuation task for a fresh board at `url`."""
    source_id = board_source(database, workspace_id, name, url)
    scan_id = completed_earlier_scan(database, workspace_id, source_id, ())
    with database.session() as session:
        continuation_id = session.scalar(
            select(SourceScanContinuation.id).where(
                SourceScanContinuation.source_scan_id == scan_id
            )
        )
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.continue",
        payload={"continuation_id": continuation_id, "generation": 1},
        idempotency_key=new_id(),
    )
    return str(task.id)


class _ReadLog:
    """Record when each continuation handler starts and ends, in order."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, str]] = []
        self._lock = Lock()

    def note(self, event: str, task_id: str) -> None:
        with self._lock:
            self.entries.append((event, task_id))


def test_continuations_for_boards_on_different_hosts_read_at_the_same_time(
    durable_database: Database,
) -> None:
    workspace_id = _workspace_id(durable_database)
    queue = TaskQueueService(durable_database)
    first = _board_continuation(
        queue, durable_database, workspace_id, "Initech", "https://initech.wd5.example/jobs"
    )
    second = _board_continuation(
        queue, durable_database, workspace_id, "Globex", "https://globex.wd1.example/jobs"
    )
    # Each read waits for the other, so the barrier passes only when both run at once.
    together = Barrier(2, timeout=10)

    def read(context: TaskContext, payload: dict[str, object]) -> dict[str, object]:
        del context, payload
        together.wait()
        return {}

    worker = Worker(
        durable_database,
        worker_id="slotted-worker",
        handlers={"source.continue": read},
        continuation_slots=2,
    )
    assert worker.dispatch()
    assert worker.dispatch()
    worker.wait_idle()

    assert [queue.get(task).status for task in (first, second)] == [
        TaskStatus.COMPLETED,
        TaskStatus.COMPLETED,
    ]


@pytest.mark.parametrize(
    ("second_url", "slots"),
    [
        ("https://initech.wd5.example/other-jobs", 2),
        ("https://globex.wd1.example/jobs", 1),
    ],
    ids=["same-host", "slots-full"],
)
def test_a_continuation_waits_while_its_host_or_every_slot_is_busy(
    durable_database: Database, second_url: str, slots: int
) -> None:
    workspace_id = _workspace_id(durable_database)
    queue = TaskQueueService(durable_database)
    first = _board_continuation(
        queue, durable_database, workspace_id, "Initech", "https://initech.wd5.example/jobs"
    )
    second = _board_continuation(queue, durable_database, workspace_id, "Globex", second_url)
    log = _ReadLog()
    release = Event()

    def read(context: TaskContext, payload: dict[str, object]) -> dict[str, object]:
        del payload
        log.note("start", context.task_id)
        if context.task_id == first:
            release.wait(timeout=10)
        log.note("end", context.task_id)
        return {}

    worker = Worker(
        durable_database,
        worker_id="polite-worker",
        handlers={"source.continue": read},
        continuation_slots=slots,
    )
    assert worker.dispatch()
    waiting = worker.dispatch()
    held_status = queue.get(second).status
    release.set()
    worker.wait_idle()

    assert waiting is False
    assert held_status == TaskStatus.QUEUED
    assert log.entries == [("start", first), ("end", first), ("start", second), ("end", second)]
    assert queue.get(second).status == TaskStatus.COMPLETED


def test_a_slot_recovers_a_continuation_whose_lease_expired_under_a_dead_worker(
    durable_database: Database,
) -> None:
    workspace_id = _workspace_id(durable_database)
    queue = TaskQueueService(durable_database)
    task_id = _board_continuation(
        queue, durable_database, workspace_id, "Initech", "https://initech.wd5.example/jobs"
    )
    with durable_database.session() as session:
        stranded = session.get(BackgroundTask, task_id)
        assert stranded is not None
        stranded.status = TaskStatus.RUNNING.value
        stranded.attempt_count = 1
        stranded.leased_by = "dead-worker"
        stranded.lease_expires_at = utc_now() - timedelta(seconds=1)

    def read(context: TaskContext, payload: dict[str, object]) -> dict[str, object]:
        del payload
        context.progress("reading", {"completed": 1})
        return {"read": 1}

    worker = Worker(
        durable_database,
        worker_id="recovering-worker",
        handlers={"source.continue": read},
        continuation_slots=2,
    )
    assert worker.dispatch()
    worker.wait_idle()

    recovered = queue.get(task_id)
    assert recovered.status == TaskStatus.COMPLETED
    assert recovered.attempt_count == 2
    assert "task.recovered" in [event.event_type for event in queue.events_after(task_id)]


def test_a_continuation_cancelled_while_it_reads_in_a_slot_ends_cancelled(
    durable_database: Database,
) -> None:
    workspace_id = _workspace_id(durable_database)
    queue = TaskQueueService(durable_database)
    task_id = _board_continuation(
        queue, durable_database, workspace_id, "Initech", "https://initech.wd5.example/jobs"
    )
    release = Event()

    def read(context: TaskContext, payload: dict[str, object]) -> dict[str, object]:
        del payload
        release.wait(timeout=10)
        context.raise_if_cancelled()
        return {}

    worker = Worker(
        durable_database,
        worker_id="cancelling-worker",
        handlers={"source.continue": read},
        continuation_slots=2,
    )
    assert worker.dispatch()
    queue.request_cancellation(task_id)
    release.set()
    worker.wait_idle()

    assert queue.get(task_id).status == TaskStatus.CANCELLED


def test_user_work_runs_on_the_worker_while_every_continuation_slot_is_busy(
    durable_database: Database,
) -> None:
    workspace_id = _workspace_id(durable_database)
    queue = TaskQueueService(durable_database)
    reading = _board_continuation(
        queue, durable_database, workspace_id, "Initech", "https://initech.wd5.example/jobs"
    )
    release = Event()

    def read(context: TaskContext, payload: dict[str, object]) -> dict[str, object]:
        del context, payload
        release.wait(timeout=10)
        return {}

    def verify(context: TaskContext, payload: dict[str, object]) -> dict[str, object]:
        del context, payload
        return {"verified": True}

    worker = Worker(
        durable_database,
        worker_id="busy-worker",
        handlers={"source.continue": read, "artifact.verify": verify},
        continuation_slots=1,
    )
    try:
        assert worker.dispatch()
        user_work = _enqueue_verify(queue, workspace_id, "user-work")
        assert worker.dispatch()
        statuses = (queue.get(user_work).status, queue.get(reading).status)
    finally:
        release.set()
    worker.wait_idle()

    assert statuses == (TaskStatus.COMPLETED, TaskStatus.RUNNING)


def _claims_failing_once(
    worker: Worker, monkeypatch: pytest.MonkeyPatch, error: BaseException, before: Any = None
) -> None:
    """Make the first follow-on claim a slot makes raise `error`, after running `before`."""
    claim = worker.queue.claim_lane
    failed: list[bool] = []

    def failing_once(**options: Any) -> Any:
        if options["scope"] == "continuation" and not failed:
            failed.append(True)
            if before is not None:
                before()
            raise error
        return claim(**options)

    monkeypatch.setattr(worker.queue, "claim_lane", failing_once)


def test_a_slot_failure_stops_every_slot_claiming_and_surfaces_through_dispatch(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = _workspace_id(durable_database)
    queue = TaskQueueService(durable_database)
    first = _board_continuation(
        queue, durable_database, workspace_id, "Initech", "https://initech.wd5.example/jobs"
    )
    second = _board_continuation(
        queue, durable_database, workspace_id, "Globex", "https://globex.wd1.example/jobs"
    )
    third = _board_continuation(
        queue, durable_database, workspace_id, "Umbrella", "https://umbrella.wd3.example/jobs"
    )
    second_started = Event()

    def read(context: TaskContext, payload: dict[str, object]) -> dict[str, object]:
        del payload
        if context.task_id == first:
            second_started.wait(timeout=10)
        elif context.task_id == second:
            second_started.set()
            # The first slot fails on its next claim; this one finishes only after that.
            for thread in threading.enumerate():
                if thread.name == f"tailorcv-slot-{first}":
                    thread.join(timeout=10)
        return {}

    worker = Worker(
        durable_database,
        worker_id="failing-slot-worker",
        handlers={"source.continue": read},
        continuation_slots=2,
    )
    _claims_failing_once(worker, monkeypatch, RuntimeError("claim broke"))
    assert worker.dispatch()
    assert worker.dispatch()

    with pytest.raises(RuntimeError, match="claim broke"):
        worker.wait_idle()
    with pytest.raises(RuntimeError, match="claim broke"):
        worker.dispatch()
    assert [queue.get(task).status for task in (first, second, third)] == [
        TaskStatus.COMPLETED,
        TaskStatus.COMPLETED,
        TaskStatus.QUEUED,
    ]


def test_a_slot_failure_is_logged_even_when_the_drain_suppresses_it(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    workspace_id = _workspace_id(durable_database)
    queue = TaskQueueService(durable_database)
    _board_continuation(
        queue, durable_database, workspace_id, "Initech", "https://initech.wd5.example/jobs"
    )

    def read(context: TaskContext, payload: dict[str, object]) -> dict[str, object]:
        del context, payload
        return {}

    worker = Worker(
        durable_database,
        worker_id="logged-slot-worker",
        handlers={"source.continue": read},
        continuation_slots=1,
    )
    _claims_failing_once(worker, monkeypatch, RuntimeError("claim broke"))
    assert worker.dispatch()
    caplog.set_level("ERROR", logger="tailorcv.bootstrap.worker")
    with pytest.raises(RuntimeError, match="claim broke"):
        worker.wait_idle()

    failures = [r for r in caplog.records if r.getMessage() == "continuation_slot_failed"]
    assert [getattr(r, "error_type", None) for r in failures] == ["RuntimeError"]


def test_a_database_lock_on_a_slots_next_claim_leaves_the_work_for_the_worker(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = _workspace_id(durable_database)
    queue = TaskQueueService(durable_database)
    first = _board_continuation(
        queue, durable_database, workspace_id, "Initech", "https://initech.wd5.example/jobs"
    )
    second = _board_continuation(
        queue, durable_database, workspace_id, "Globex", "https://globex.wd1.example/jobs"
    )

    def read(context: TaskContext, payload: dict[str, object]) -> dict[str, object]:
        del context, payload
        return {}

    worker = Worker(
        durable_database,
        worker_id="locked-claim-worker",
        handlers={"source.continue": read},
        continuation_slots=1,
    )
    _claims_failing_once(
        worker, monkeypatch, OperationalError("UPDATE", {}, Exception("database is locked"))
    )
    assert worker.dispatch()
    worker.wait_idle()
    held = queue.get(second).status
    assert worker.dispatch()
    worker.wait_idle()

    assert held == TaskStatus.QUEUED
    assert [queue.get(task).status for task in (first, second)] == [
        TaskStatus.COMPLETED,
        TaskStatus.COMPLETED,
    ]


def test_a_slot_whose_next_claim_raises_leaves_a_host_another_lane_took_busy(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = _workspace_id(durable_database)
    queue = TaskQueueService(durable_database)
    _board_continuation(
        queue, durable_database, workspace_id, "Initech", "https://initech.wd5.example/jobs"
    )

    def read(context: TaskContext, payload: dict[str, object]) -> dict[str, object]:
        del context, payload
        return {}

    worker = Worker(
        durable_database,
        worker_id="host-keeping-worker",
        handlers={"source.continue": read},
        continuation_slots=2,
    )
    # Stands in for another lane claiming the freed host before this claim fails.
    _claims_failing_once(
        worker,
        monkeypatch,
        RuntimeError("claim broke"),
        before=lambda: worker._busy_hosts.add("initech.wd5.example"),
    )
    assert worker.dispatch()
    with pytest.raises(RuntimeError, match="claim broke"):
        worker.wait_idle()

    assert worker._busy_hosts == {"initech.wd5.example"}


def test_stopping_the_worker_drains_its_slots_before_withdrawing_its_presence(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = _workspace_id(durable_database)
    queue = TaskQueueService(durable_database)
    reading = _board_continuation(
        queue, durable_database, workspace_id, "Initech", "https://initech.wd5.example/jobs"
    )
    waiting = _board_continuation(
        queue, durable_database, workspace_id, "Globex", "https://globex.wd1.example/jobs"
    )
    stop = Event()
    draining = Event()
    present_while_draining: list[bool] = []

    def read(context: TaskContext, payload: dict[str, object]) -> dict[str, object]:
        del context, payload
        stop.set()
        present_while_draining.append(draining.wait(timeout=10) and queue.worker_available())
        return {}

    def interrupted(_seconds: float) -> None:
        stop.wait(timeout=10)
        raise KeyboardInterrupt

    worker = Worker(
        durable_database,
        worker_id="draining-worker",
        handlers={"source.continue": read},
        continuation_slots=1,
        sleep=interrupted,
    )
    wait_idle = worker.wait_idle

    def drain() -> None:
        draining.set()
        wait_idle()

    monkeypatch.setattr(worker, "wait_idle", drain)
    with pytest.raises(KeyboardInterrupt):
        worker.run_forever(poll_seconds=0.1)

    assert present_while_draining == [True]
    assert [queue.get(task).status for task in (reading, waiting)] == [
        TaskStatus.COMPLETED,
        TaskStatus.QUEUED,
    ]
    assert not queue.worker_available()


def test_stop_request_finishes_an_inline_task_before_the_worker_exits(
    durable_database: Database,
) -> None:
    workspace_id = _workspace_id(durable_database)
    queue = TaskQueueService(durable_database)
    task_id = _enqueue_verify(queue, workspace_id, "inline-stop")
    running: Worker

    def finish(context: TaskContext, payload: dict[str, object]) -> dict[str, object]:
        del context, payload
        running.request_stop()
        return {"saved": True}

    running = Worker(
        durable_database,
        worker_id="inline-stop-worker",
        handlers={"artifact.verify": finish},
        sleep=lambda _seconds: (_ for _ in ()).throw(RuntimeError("worker kept polling")),
    )
    running.run_forever(poll_seconds=0.1)

    assert queue.get(task_id).status == TaskStatus.COMPLETED
    assert not queue.worker_available()


def test_a_database_lock_on_the_dispatchers_claim_keeps_the_worker_polling(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = _workspace_id(durable_database)
    queue = TaskQueueService(durable_database)
    task_id = _enqueue_verify(queue, workspace_id, "after-locked-claim")
    running: Worker

    def finish(context: TaskContext, payload: dict[str, object]) -> dict[str, object]:
        del context, payload
        running.request_stop()
        return {"saved": True}

    running = Worker(
        durable_database,
        worker_id="locked-dispatch-worker",
        handlers={"artifact.verify": finish},
        sleep=lambda _seconds: None,
    )
    claim = running.queue.claim_lane
    failed: list[bool] = []

    def locked_once(**options: Any) -> Any:
        if not failed:
            failed.append(True)
            raise OperationalError("UPDATE", {}, Exception("database is locked"))
        return claim(**options)

    monkeypatch.setattr(running.queue, "claim_lane", locked_once)
    running.run_forever(poll_seconds=0.1)

    assert failed == [True]
    assert queue.get(task_id).status == TaskStatus.COMPLETED


def test_an_interrupt_during_an_inline_task_stops_its_handler_at_the_next_progress_point(
    durable_database: Database,
) -> None:
    workspace_id = _workspace_id(durable_database)
    queue = TaskQueueService(durable_database)
    task_id = _enqueue_verify(queue, workspace_id, "interrupted-inline")
    interrupted = Event()
    resumed = Event()
    reached: list[str] = []
    dispatcher = threading.current_thread()

    def clock() -> float:
        # Stands in for Ctrl-C reaching the worker thread while it waits on the handler.
        if threading.current_thread() is dispatcher and interrupted.is_set():
            raise KeyboardInterrupt
        return 0.0

    def draft(context: TaskContext, payload: dict[str, object]) -> dict[str, object]:
        del payload
        context.progress("drafting", {"completed": 1})
        interrupted.set()
        resumed.wait(timeout=10)
        try:
            context.progress("critiquing", {"completed": 2})
        except Exception as exc:
            reached.append(type(exc).__name__)
            raise
        reached.append("completed")
        return {}

    worker = Worker(
        durable_database,
        worker_id="interrupted-inline-worker",
        handlers={"artifact.verify": draft},
        lease_heartbeat_seconds=0.01,
        clock=clock,
    )
    with pytest.raises(KeyboardInterrupt):
        worker.run_once()
    resumed.set()
    for thread in threading.enumerate():
        if thread.name == f"tailorcv-task-{task_id}":
            thread.join(timeout=10)

    assert reached == ["LeaseRenewalError"]
    interrupted_task = queue.get(task_id)
    assert (interrupted_task.status, interrupted_task.progress) == (
        TaskStatus.RUNNING,
        {"completed": 1},
    )


def _staging_target(board_url: str) -> SourceContinuationTarget:
    return SourceContinuationTarget(
        url=f"{board_url}/page-2",
        label="Published role",
        label_source="visible",
        source_url=board_url,
        structural_context="main",
        relations=("publisher-content", "substantive-label"),
    )


class _WorkdayOpeningBoards:
    """Defer one page per board, whose continuation lists one Workday opening."""

    def listings(
        self,
        _employer: str,
        url: str,
        *,
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        return SourceRead(
            listings=[],
            complete=False,
            raw_count=0,
            outcome=SourceReadOutcome.CRAWL_CONTINUATION,
            deferred_targets=(_staging_target(url),),
        )

    published_description = ""

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
        tenant = (urlsplit(root_url).hostname or "").split(".")[-2]
        opening = SourceListing(
            employer=employer,
            title="Data Product Manager",
            url=(
                f"https://{tenant}.wd1.myworkdayjobs.com/en-US/Careers/job/Canada/"
                "Data-Product-Manager_R-1"
            ),
            facts=published_listing_facts("workday", locations="Canada"),
            published_description=self.published_description,
        )
        return SourceRead(
            listings=[opening], complete=True, raw_count=1, outcome=SourceReadOutcome.ATS_COMPLETE
        )


def _readable_detail(_url: str) -> PostingCheck:
    return PostingCheck(
        status="readable",
        note="fixture",
        text="Own the data platform and analytics roadmap in Canada.",
        source=PostingSource(publisher="workday", record="{}", description_paths=("description",)),
        platform="workday",
        location="Toronto, Canada",
        facts=published_listing_facts("workday", locations="Toronto, Canada"),
    )


def _staging_worker(
    database: Database,
    detail: Any,
    *,
    boards: tuple[tuple[str, str], ...],
    slots: int = 2,
    board: _WorkdayOpeningBoards | None = None,
    content_resolution: PostingContentResolutionService | None = None,
    **handler_options: Any,
) -> tuple[SourceScanService, Worker]:
    """Scan boards whose continuations stage Workday openings, and a worker to run them."""
    workspace_id = _workspace_id(database)
    state_target_titles(database, workspace_id, "Data Product Manager")
    for name, url in boards:
        board_source(database, workspace_id, name, url)
    board = board or _WorkdayOpeningBoards()
    service = SourceScanService(
        database,
        storage=runtime_storage(database),
        fetcher=board,
        posting_fetcher=UnexpectedPostingFetcher(),
        workday_detail_checker=detail,
        content_resolution=content_resolution,
    )
    service.scan(workspace_id)
    handlers = default_handlers(database, fetcher=board)
    handlers["source.continue"] = SourceContinuationHandler(
        database,
        board,
        service.sweep,
        lambda workspace, task_id, outcomes: service.record_task_outcomes(
            workspace, task_id, outcomes, origin="source.continue"
        ),
        service.begin_recording_pass,
        **handler_options,
    )
    return service, Worker(
        database, worker_id="staging-worker", handlers=handlers, continuation_slots=slots
    )


def test_a_chunk_reads_while_another_stages_and_their_staging_writes_take_turns(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    writing = Event()
    release = Event()
    read_while_staging = Event()
    readers_started = Event()
    guard = Lock()
    details: list[str] = []
    writes: list[str] = []

    def detail(url: str) -> PostingCheck:
        with guard:
            details.append(url)
            later = len(details) > 1
        if later:
            readers_started.set()
            if writing.wait(timeout=10):
                read_while_staging.set()
        else:
            readers_started.wait(timeout=10)
        return _readable_detail(url)

    service, worker = _staging_worker(
        durable_database,
        detail,
        boards=(
            ("Globex", "https://jobs.globex.example/careers"),
            ("Initech", "https://jobs.initech.example/careers"),
        ),
    )
    stage = service.staging.stage_listings_in_session

    def observed(*args: Any, **options: Any) -> Any:
        with guard:
            writes.append("start")
            first = len(writes) == 1
        if first:
            writing.set()
            release.wait(timeout=10)
        staged = stage(*args, **options)
        with guard:
            writes.append("end")
        return staged

    monkeypatch.setattr(service.staging, "stage_listings_in_session", observed)
    original_task_ids = {task.id for task in _continue_tasks(durable_database)}
    assert len(original_task_ids) == 2
    assert worker.dispatch()
    assert worker.dispatch()
    try:
        read = read_while_staging.wait(timeout=10)
        with guard:
            during = list(writes)
    finally:
        release.set()
    worker.wait_idle()

    assert read
    assert during == ["start"]
    assert writes == ["start", "end", "start", "end"]
    tasks = _continue_tasks(durable_database)
    assert {task.id for task in tasks} == original_task_ids
    assert [task.status for task in tasks] == [TaskStatus.COMPLETED, TaskStatus.COMPLETED]
    with durable_database.session() as session:
        frontier = [row.status for row in session.scalars(select(SourceScanFrontierItem))]
        staged = session.scalars(select(PendingDiscovery.employer)).all()
        outcomes = session.execute(
            select(SourceScanOutcome.employer, SourceScanOutcome.reason).where(
                SourceScanOutcome.task_id.in_(original_task_ids)
            )
        ).all()
    assert frontier == ["completed", "completed"]
    assert sorted(staged) == ["Globex", "Initech"]
    assert sorted(outcomes) == [("Globex", FunnelReason.STAGED), ("Initech", FunnelReason.STAGED)]


def test_a_source_read_guard_holding_the_sql_writer_does_not_wait_for_the_handler_lock(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, worker = _staging_worker(
        durable_database,
        _readable_detail,
        boards=(("Globex", "https://jobs.globex.example/careers"),),
    )
    handler = cast(SourceContinuationHandler, worker.handlers["source.continue"])
    inner_lock = handler._lock
    checking_guard = Event()
    lock_held = Event()
    release = Event()
    failures: list[str] = []
    stage = service.sweep.stage_continuation
    guard_thread: threading.Thread | None = None

    class GuardLock:
        def __enter__(self) -> bool:
            if checking_guard.is_set() and threading.current_thread() is guard_thread:
                raise AssertionError("The SQL source-read guard acquired the handler lock.")
            return bool(inner_lock.acquire())

        def __exit__(self, *_exc: object) -> None:
            inner_lock.release()

    monkeypatch.setattr(handler, "_lock", GuardLock())

    def hold_handler_lock() -> None:
        with handler._lock:
            lock_held.set()
            release.wait()

    def guarded_stage(*args: Any, **options: Any) -> Any:
        nonlocal guard_thread
        guard_thread = threading.current_thread()
        holder = threading.Thread(target=hold_handler_lock)
        try:
            with durable_database.exclusive_session() as session:
                holder.start()
                assert lock_held.wait(timeout=10)
                checking_guard.set()
                try:
                    options["require_source_read"](session)
                except AssertionError as exc:
                    failures.append(str(exc))
                    raise
                finally:
                    checking_guard.clear()
        finally:
            release.set()
            holder.join(timeout=10)
        assert not holder.is_alive()
        return stage(*args, **options)

    monkeypatch.setattr(service.sweep, "stage_continuation", guarded_stage)
    task_id = _continue_tasks(durable_database)[0].id
    assert worker.run_once()

    assert failures == []
    assert [(task.id, task.status) for task in _continue_tasks(durable_database)] == [
        (task_id, TaskStatus.COMPLETED)
    ]
    with durable_database.session() as session:
        assert session.scalars(select(SourceScanFrontierItem.status)).all() == ["completed"]
        assert session.scalars(select(PendingDiscovery.employer)).all() == ["Globex"]
        assert session.scalars(
            select(SourceScanOutcome.reason).where(SourceScanOutcome.task_id == task_id)
        ).all() == [FunnelReason.STAGED]


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


def test_a_chunk_that_misses_the_staging_turn_by_its_time_limit_is_rearmed_uncharged(
    durable_database: Database,
) -> None:
    clock = _Clock()

    def slow_detail(url: str) -> PostingCheck:
        clock.advance(120)
        return _readable_detail(url)

    service, worker = _staging_worker(
        durable_database,
        slow_detail,
        boards=(("Globex", "https://jobs.globex.example/careers"),),
        time_limit_seconds=60,
        clock=clock,
    )
    with service.staging.write_turn():
        assert worker.run_once()

    tasks = _continue_tasks(durable_database)
    assert [(task.status, task.error_category) for task in tasks] == [
        (TaskStatus.FAILED.value, "StagingBusyError"),
        (TaskStatus.QUEUED.value, None),
    ]
    assert tasks[1].payload["no_progress_count"] == 0
    with durable_database.session() as session:
        rows = [
            (row.status, row.attempt_count)
            for row in session.scalars(select(SourceScanFrontierItem))
        ]
    assert rows == [("pending", 0)]


def test_a_chunk_abandoned_before_its_staging_turn_stages_nothing(
    durable_database: Database,
) -> None:
    handler: SourceContinuationHandler

    def abandoned_detail(url: str) -> PostingCheck:
        # Stands in for the worker abandoning the chunk while its detail read is in flight.
        handler.recover_abandoned(_continue_tasks(durable_database)[0].id)
        return _readable_detail(url)

    _service, worker = _staging_worker(
        durable_database,
        abandoned_detail,
        boards=(("Globex", "https://jobs.globex.example/careers"),),
    )
    handler = cast(SourceContinuationHandler, worker.handlers["source.continue"])
    assert worker.run_once()

    with durable_database.session() as session:
        staged = session.scalars(select(PendingDiscovery.employer)).all()
        frontier = [
            (row.status, row.attempt_count)
            for row in session.scalars(select(SourceScanFrontierItem))
        ]
    assert staged == []
    assert frontier == [("pending", 1)]


def test_a_chunk_abandoned_during_its_staging_write_stores_no_content_after_it(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler: SourceContinuationHandler
    board = _WorkdayOpeningBoards()
    board.published_description = "Own the data product roadmap for the payments platform."
    content_resolution = PostingContentResolutionService(
        durable_database,
        DeterministicPostingContentResolver(),
        storage=runtime_storage(durable_database),
    )
    service, worker = _staging_worker(
        durable_database,
        _readable_detail,
        boards=(("Globex", "https://jobs.globex.example/careers"),),
        board=board,
        content_resolution=content_resolution,
    )
    handler = cast(SourceContinuationHandler, worker.handlers["source.continue"])
    stage = service.staging.stage_listings

    def stage_then_abandon(*args: Any, **options: Any) -> Any:
        staged = stage(*args, **options)
        handler.recover_abandoned(_continue_tasks(durable_database)[0].id)
        return staged

    monkeypatch.setattr(service.staging, "stage_listings", stage_then_abandon)
    assert worker.run_once()

    with durable_database.session() as session:
        staged_rows = session.scalars(select(PendingDiscovery)).all()
        evidence = session.scalars(select(PendingPostingContent)).all()
        assert [row.current_content_id for row in staged_rows] == [None]
    assert evidence == []


def test_a_chunk_abandoned_after_its_retained_descriptions_resolves_no_content(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler: SourceContinuationHandler
    content_resolution = PostingContentResolutionService(
        durable_database,
        DeterministicPostingContentResolver(),
        storage=runtime_storage(durable_database),
    )
    service, worker = _staging_worker(
        durable_database,
        _readable_detail,
        boards=(("Globex", "https://jobs.globex.example/careers"),),
        content_resolution=content_resolution,
    )
    handler = cast(SourceContinuationHandler, worker.handlers["source.continue"])
    retain = service.sweep._retain_published_descriptions
    retained_ids: list[str] = []

    def unexpected_resolution(*args: Any, **options: Any) -> Any:
        pytest.fail("Abandoned chunk resolved additional posting content.")

    monkeypatch.setattr(content_resolution, "resolve_automatic", unexpected_resolution)

    def retain_then_abandon(*args: Any, **options: Any) -> None:
        retain(*args, **options)
        with durable_database.session() as session:
            retained_ids.extend(session.scalars(select(PendingPostingContent.id)))
        handler.recover_abandoned(_continue_tasks(durable_database)[0].id)

    monkeypatch.setattr(service.sweep, "_retain_published_descriptions", retain_then_abandon)
    assert worker.run_once()

    with durable_database.session() as session:
        staged_rows = session.scalars(select(PendingDiscovery)).all()
        evidence = session.scalars(select(PendingPostingContent)).all()
    assert len(staged_rows) == 1
    assert len(retained_ids) == 1
    assert [row.id for row in evidence] == retained_ids


class _ProbedLock:
    """Delegate to a lock, telling `contended` when the probed thread finds it held."""

    def __init__(self, inner: Any, contended: Event, log: list[str]) -> None:
        self.inner = inner
        self.contended = contended
        self.log = log
        self.probed: threading.Thread | None = None

    def __enter__(self) -> bool:
        if threading.current_thread() is self.probed and not self.inner.acquire(blocking=False):
            self.contended.set()
            return bool(self.inner.acquire())
        if threading.current_thread() is not self.probed:
            return bool(self.inner.acquire())
        return True

    def __exit__(self, *_exc: object) -> None:
        if threading.current_thread() is self.probed:
            self.log.append("recovered")
            self.contended.set()
        self.inner.release()


def test_an_outcome_recorded_while_its_chunk_is_recovered_lands_before_the_recovery(
    durable_database: Database,
) -> None:
    _service, worker = _staging_worker(
        durable_database,
        _readable_detail,
        boards=(("Globex", "https://jobs.globex.example/careers"),),
    )
    handler = cast(SourceContinuationHandler, worker.handlers["source.continue"])
    record = handler.outcome_recorder
    assert record is not None
    proceed = Event()
    log: list[str] = []
    lock = _ProbedLock(handler._lock, proceed, log)
    handler._lock = cast(Any, lock)

    def recover() -> None:
        handler.recover_abandoned(_continue_tasks(durable_database)[0].id)

    def recording_during_recovery(workspace_id: str, task_id: str, outcome: Any) -> None:
        if lock.probed is None:
            lock.probed = threading.Thread(target=recover)
            lock.probed.start()
            assert proceed.wait(timeout=10)
            log.append("recorded")
        record(workspace_id, task_id, outcome)

    handler.outcome_recorder = recording_during_recovery
    assert worker.run_once()
    assert lock.probed is not None
    lock.probed.join(timeout=10)

    assert not lock.probed.is_alive()
    assert log == ["recorded", "recovered"]


def test_a_worker_refuses_a_continuation_slot_count_outside_its_bounds() -> None:
    with pytest.raises(ValueError, match="slots"):
        Worker(cast(Database, None), worker_id="no-slots", handlers={}, continuation_slots=0)


def test_one_aged_continuation_per_window_is_claimed_before_newer_user_work(
    durable_database: Database,
) -> None:
    workspace_id = _workspace_id(durable_database)
    queue = TaskQueueService(durable_database)
    first = _aged_continuation(queue, durable_database, workspace_id)
    _aged_continuation(queue, durable_database, workspace_id)
    user_work = _enqueue_verify(queue, workspace_id, "user-work")

    claims = [queue.claim_next(worker_id="fair-worker") for _ in range(2)]

    assert [claim.id if claim else None for claim in claims] == [first, user_work]


def test_a_continuation_claim_loads_one_task_past_every_continuation_on_a_busy_host(
    durable_database: Database,
) -> None:
    workspace_id = _workspace_id(durable_database)
    queue = TaskQueueService(durable_database)
    for name in ("Initech", "Initrode", "Initex"):
        _board_continuation(
            queue,
            durable_database,
            workspace_id,
            name,
            f"https://INITECH.wd5.example:8443/{name.casefold()}?page=1",
        )
    free = _board_continuation(
        queue, durable_database, workspace_id, "Globex", "https://globex.wd1.example/jobs"
    )
    loaded: list[str] = []

    def note(task: BackgroundTask, _context: object) -> None:
        loaded.append(task.id)

    event.listen(BackgroundTask, "load", note)
    try:
        claimed = queue.claim_lane(
            worker_id="bounded-worker",
            scope="continuation",
            busy_hosts=frozenset({"initech.wd5.example"}),
        )
    finally:
        event.remove(BackgroundTask, "load", note)

    assert claimed is not None
    assert (claimed.task.id, claimed.host) == (free, "globex.wd1.example")
    assert loaded == [free]


def test_a_busy_host_the_sql_match_misses_does_not_hold_back_a_free_host(
    durable_database: Database,
) -> None:
    workspace_id = _workspace_id(durable_database)
    queue = TaskQueueService(durable_database)
    # A bracketed IPv6 authority passes the SQL match, so only the host check after it holds.
    held = _board_continuation(
        queue, durable_database, workspace_id, "Loopback", "https://[::1]:8443/jobs"
    )
    free = _board_continuation(
        queue, durable_database, workspace_id, "Globex", "https://globex.wd1.example/jobs"
    )
    with durable_database.session() as session:
        first = session.get(BackgroundTask, held)
        assert first is not None
        first.created_at = utc_now() - timedelta(minutes=1)

    claimed = queue.claim_lane(
        worker_id="backstop-worker",
        scope="continuation",
        busy_hosts=frozenset({"::1"}),
    )

    assert claimed is not None
    assert claimed.task.id == free


def test_a_claim_past_every_sql_miss_loads_only_the_task_it_claims(
    durable_database: Database,
) -> None:
    workspace_id = _workspace_id(durable_database)
    queue = TaskQueueService(durable_database)
    held = [
        _board_continuation(
            queue, durable_database, workspace_id, name, f"https://[::1]:8443/{name.lower()}"
        )
        for name in ("Loopback", "Loopbridge", "Loopcrest")
    ]
    free = _board_continuation(
        queue, durable_database, workspace_id, "Globex", "https://globex.wd1.example/jobs"
    )
    with durable_database.session() as session:
        for index, task_id in enumerate(held):
            row = session.get(BackgroundTask, task_id)
            assert row is not None
            row.created_at = utc_now() - timedelta(minutes=10 - index)
    loaded: list[str] = []

    def note(task: BackgroundTask, _context: object) -> None:
        loaded.append(task.id)

    event.listen(BackgroundTask, "load", note)
    try:
        claimed = queue.claim_lane(
            worker_id="backstop-worker", scope="continuation", busy_hosts=frozenset({"::1"})
        )
    finally:
        event.remove(BackgroundTask, "load", note)

    assert claimed is not None
    assert claimed.task.id == free
    assert loaded == [free]


def test_a_busy_host_named_only_in_a_query_string_does_not_hold_back_its_board(
    durable_database: Database,
) -> None:
    workspace_id = _workspace_id(durable_database)
    queue = TaskQueueService(durable_database)
    free = _board_continuation(
        queue,
        durable_database,
        workspace_id,
        "Globex",
        "https://globex.wd1.example/jobs?from=https://initech.wd5.example/x",
    )

    claimed = queue.claim_lane(
        worker_id="backstop-worker",
        scope="continuation",
        busy_hosts=frozenset({"initech.wd5.example"}),
    )

    assert claimed is not None
    assert claimed.task.id == free
    assert claimed.host == "globex.wd1.example"
