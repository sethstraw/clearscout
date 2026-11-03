"""Separate-process evidence for SQLite task queue coordination."""

from __future__ import annotations

import multiprocessing
import sqlite3
from datetime import timedelta
from multiprocessing.process import BaseProcess
from pathlib import Path
from queue import Empty

from clearscout.application.tasks import TaskQueueService
from clearscout.application.workspaces import WorkspaceService
from clearscout.domain import utc_now
from clearscout.infrastructure.persistence.database import Database, sqlite_path
from clearscout.infrastructure.persistence.models import BackgroundTask, WorkerPresence
from clearscout.infrastructure.persistence.types import UTCDateTime
from tests.support.artifacts import runtime_storage

_TIMEOUT_SECONDS = 15


def _claim_in_process(
    database_url: str,
    worker_id: str,
    start: multiprocessing.synchronize.Event,
    outcomes: multiprocessing.queues.Queue[tuple[str, str | None]],
) -> None:
    database = Database(database_url)
    try:
        if not start.wait(_TIMEOUT_SECONDS):
            raise RuntimeError("claim start signal timed out")
        claimed = TaskQueueService(database).claim_next(worker_id=worker_id, lease_seconds=10)
        outcomes.put(("ok", str(claimed.id) if claimed is not None else None))
    except Exception as error:  # noqa: BLE001 - return child failures to the parent assertion
        outcomes.put(("error", repr(error)))
    finally:
        database.dispose()


def _heartbeat_in_process(
    database_url: str,
    task_id: str,
    progress: int,
    start: multiprocessing.synchronize.Event,
    outcomes: multiprocessing.queues.Queue[tuple[str, str | None]],
) -> None:
    database = Database(database_url)
    try:
        if not start.wait(_TIMEOUT_SECONDS):
            raise RuntimeError("event start signal timed out")
        TaskQueueService(database).heartbeat(
            task_id,
            worker_id="worker-a",
            progress={"process": progress},
        )
        outcomes.put(("ok", None))
    except Exception as error:  # noqa: BLE001 - return child failures to the parent assertion
        outcomes.put(("error", repr(error)))
    finally:
        database.dispose()


def _claim_with_presence_then_wait(
    database_url: str,
    ready: multiprocessing.queues.Queue[tuple[str, str | None]],
    stop: multiprocessing.synchronize.Event,
) -> None:
    database = Database(database_url)
    try:
        queue = TaskQueueService(database)
        queue.renew_worker_presence(worker_id="worker-a", ttl_seconds=10)
        claimed = queue.claim_next(worker_id="worker-a", lease_seconds=10)
        ready.put(("ok", str(claimed.id) if claimed is not None else None))
        stop.wait(_TIMEOUT_SECONDS)
    except Exception as error:  # noqa: BLE001 - return child failures to the parent assertion
        ready.put(("error", repr(error)))
    finally:
        database.dispose()


def _recover_with_presence(
    database_url: str,
    outcomes: multiprocessing.queues.Queue[tuple[str, str | None]],
) -> None:
    database = Database(database_url)
    try:
        queue = TaskQueueService(database)
        queue.renew_worker_presence(worker_id="worker-b", ttl_seconds=10)
        claimed = queue.claim_next(worker_id="worker-b", lease_seconds=10)
        outcomes.put(("ok", str(claimed.id) if claimed is not None else None))
    except Exception as error:  # noqa: BLE001 - return child failures to the parent assertion
        outcomes.put(("error", repr(error)))
    finally:
        database.dispose()


def _hold_an_existing_wal(
    database_path: str,
    task_id: str,
    ready: multiprocessing.queues.Queue[tuple[str, str | None]],
    stop: multiprocessing.synchronize.Event,
) -> None:
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute(
            "UPDATE background_tasks SET updated_at = ? WHERE id = ?",
            (UTCDateTime.serialize(utc_now()), task_id),
        )
        connection.commit()
        ready.put(("ok", None))
        stop.wait(_TIMEOUT_SECONDS)
    except Exception as error:  # noqa: BLE001 - return child failures to the parent assertion
        ready.put(("error", repr(error)))
    finally:
        connection.close()


def _read_events_after_restart(
    database_url: str,
    task_id: str,
    outcomes: multiprocessing.queues.Queue[tuple[str, str | None]],
) -> None:
    database = Database(database_url)
    try:
        sequences = [event.sequence for event in TaskQueueService(database).events_after(task_id)]
        outcomes.put(("ok", ",".join(str(sequence) for sequence in sequences)))
    except Exception as error:  # noqa: BLE001 - return child failures to the parent assertion
        outcomes.put(("error", repr(error)))
    finally:
        database.dispose()


def _task(database: Database) -> str:
    workspace = WorkspaceService(database, runtime_storage(database)).create(
        slug="processes", name="Processes"
    )
    created = TaskQueueService(database).enqueue(
        workspace_id=str(workspace.id),
        task_type="artifact.verify",
        payload={"artifact_id": "process"},
        idempotency_key="process-task",
    )
    return str(created.id)


def _outcome(outcomes: multiprocessing.queues.Queue[tuple[str, str | None]]) -> str | None:
    try:
        status, detail = outcomes.get(timeout=_TIMEOUT_SECONDS)
    except Empty as error:
        raise AssertionError("child process did not report an outcome") from error
    assert status == "ok", detail
    return detail


def _join(process: BaseProcess) -> None:
    process.join(_TIMEOUT_SECONDS)
    assert process.exitcode == 0


def _join_terminated(process: BaseProcess) -> None:
    process.join(_TIMEOUT_SECONDS)
    assert process.exitcode not in {None, 0}


def test_separate_processes_claim_one_queued_task_once(durable_database: Database) -> None:
    task_id = _task(durable_database)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    outcomes = context.Queue()
    processes = [
        context.Process(
            target=_claim_in_process,
            args=(durable_database.url, worker_id, start, outcomes),
        )
        for worker_id in ("worker-a", "worker-b")
    ]
    for process in processes:
        process.start()
    start.set()

    claimed = [_outcome(outcomes), _outcome(outcomes)]
    for process in processes:
        _join(process)

    assert claimed.count(task_id) == 1
    assert claimed.count(None) == 1


def test_separate_processes_append_unique_task_event_sequences(durable_database: Database) -> None:
    task_id = _task(durable_database)
    queue = TaskQueueService(durable_database)
    assert queue.claim_next(worker_id="worker-a", lease_seconds=10) is not None
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    outcomes = context.Queue()
    processes = [
        context.Process(
            target=_heartbeat_in_process,
            args=(durable_database.url, task_id, progress, start, outcomes),
        )
        for progress in (1, 2)
    ]
    for process in processes:
        process.start()
    start.set()

    assert [_outcome(outcomes), _outcome(outcomes)] == [None, None]
    for process in processes:
        _join(process)

    assert [event.sequence for event in queue.events_after(task_id)] == [1, 2, 3, 4]


def test_worker_termination_recovers_presence_and_lease_in_a_second_process(
    durable_database: Database,
) -> None:
    task_id = _task(durable_database)
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    stop = context.Event()
    worker = context.Process(
        target=_claim_with_presence_then_wait,
        args=(durable_database.url, ready, stop),
    )
    worker.start()
    assert _outcome(ready) == task_id
    worker.terminate()
    _join_terminated(worker)

    with durable_database.session() as session:
        task = session.get(BackgroundTask, task_id)
        presence = session.get(WorkerPresence, "worker-a")
        assert task is not None and presence is not None
        task.lease_expires_at = utc_now() - timedelta(seconds=1)
        presence.available_until = utc_now() - timedelta(seconds=1)

    outcomes = context.Queue()
    recovered = context.Process(
        target=_recover_with_presence,
        args=(durable_database.url, outcomes),
    )
    recovered.start()
    assert _outcome(outcomes) == task_id
    _join(recovered)

    recovered_task = TaskQueueService(durable_database).get(task_id)
    assert recovered_task.attempt_count == 2
    with durable_database.session() as session:
        task = session.get(BackgroundTask, task_id)
        assert task is not None and task.leased_by == "worker-b"


def test_a_fresh_process_reads_a_database_while_an_existing_wal_is_open(
    durable_database: Database,
) -> None:
    task_id = _task(durable_database)
    database_path = sqlite_path(durable_database.url)
    assert database_path is not None
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    stop = context.Event()
    holder = context.Process(
        target=_hold_an_existing_wal,
        args=(str(database_path), task_id, ready, stop),
    )
    holder.start()
    assert _outcome(ready) is None
    assert Path(f"{database_path}-wal").is_file()

    outcomes = context.Queue()
    restarted = context.Process(
        target=_read_events_after_restart,
        args=(durable_database.url, task_id, outcomes),
    )
    restarted.start()
    assert _outcome(outcomes) == "1"
    _join(restarted)

    stop.set()
    _join(holder)
