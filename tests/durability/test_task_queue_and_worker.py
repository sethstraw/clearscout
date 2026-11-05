from __future__ import annotations

import hashlib
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from typing import Any, cast, get_args

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError

from clearscout.application import job_search
from clearscout.application.failures import ConflictError, NotFoundError, ValidationError
from clearscout.application.tasks import (
    TASK_SPECS,
    UNPAID,
    TaskContext,
    TaskQueueService,
    TaskSpec,
    TaskType,
    task_type_catalog,
    task_types,
    validate_task_payload,
)
from clearscout.application.tasks.events import TaskEventLog
from clearscout.application.tasks.leases import TaskLeases
from clearscout.application.workspaces import WorkspaceResponse, WorkspaceService
from clearscout.application.workspaces.models import ScanScheduleInput
from clearscout.application.workspaces.scan_schedule import ScanScheduleService
from clearscout.bootstrap.worker import ArtifactVerifier, Worker, _LeaseKeeper, default_handlers
from clearscout.domain import SourceKind, TaskStatus, utc_now
from clearscout.infrastructure import build
from clearscout.infrastructure.build import BuildIdentity, current_build
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    Artifact,
    BackgroundTask,
    PendingDiscovery,
    Source,
    SourceScan,
    SourceScanContinuation,
    SourceScanMember,
    WorkerPresence,
)
from tests.support.artifacts import artifact_service, runtime_storage
from tests.support.scenarios import (
    board_source,
    completed_earlier_scan,
    evaluation_commit,
    state_target_titles,
)


def _workspace(database: Database, *, root_path: str | None = None) -> WorkspaceResponse:
    return WorkspaceService(database, runtime_storage(database)).create(
        slug="tasks",
        name="Tasks",
        root_path=root_path,
    )


@pytest.fixture
def lease_ticks(monkeypatch: pytest.MonkeyPatch) -> Event:
    """Advance renewal loops through explicit events."""
    ticks = Event()
    original_start = _LeaseKeeper.start
    original_stop = _LeaseKeeper.stop

    def start(keeper: _LeaseKeeper) -> None:
        def wait(timeout: float | None = None) -> bool:
            ticks.wait()
            ticks.clear()
            return keeper._stop.is_set()

        monkeypatch.setattr(keeper._stop, "wait", wait)
        original_start(keeper)

    def stop(keeper: _LeaseKeeper) -> None:
        keeper._stop.set()
        ticks.set()
        original_stop(keeper)

    monkeypatch.setattr(_LeaseKeeper, "start", start)
    monkeypatch.setattr(_LeaseKeeper, "stop", stop)
    return ticks


def test_queue_lifecycle_idempotency_and_lease_recovery(durable_database: Database) -> None:
    workspace = _workspace(durable_database)
    workspace_id = str(workspace.id)
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="artifact.verify",
        payload={"artifact_id": "one"},
        idempotency_key="verify-one",
    )
    assert (
        queue.enqueue(
            workspace_id=workspace_id,
            task_type="artifact.verify",
            payload={"artifact_id": "one"},
            idempotency_key="verify-one",
        ).id
        == task.id
    )
    with pytest.raises(ConflictError, match="different payload"):
        queue.enqueue(
            workspace_id=workspace_id,
            task_type="artifact.verify",
            payload={"artifact_id": "two"},
            idempotency_key="verify-one",
        )
    with pytest.raises(ValidationError, match="Worker ID"):
        queue.claim_next(worker_id="", lease_seconds=5)

    claimed = queue.claim_next(worker_id="worker-a", lease_seconds=10)
    assert claimed is not None
    assert claimed.status == TaskStatus.RUNNING
    assert claimed.payload == {"artifact_id": "one"}
    heartbeat = queue.heartbeat(
        str(task.id), worker_id="worker-a", stage="hashing", progress={"files": 1}
    )
    assert heartbeat.progress == {"files": 1}
    with pytest.raises(ConflictError, match="no longer owns"):
        queue.heartbeat(str(task.id), worker_id="worker-b")
    with pytest.raises(ValidationError, match="finish only"):
        queue.finish(str(task.id), worker_id="worker-a", status=TaskStatus.RUNNING)

    with durable_database.session() as session:
        record = session.get(BackgroundTask, str(task.id))
        assert record is not None
        record.lease_expires_at = utc_now() - timedelta(seconds=1)
    recovered = queue.claim_next(worker_id="worker-b", lease_seconds=10)
    assert recovered is not None
    assert recovered.id == task.id
    assert recovered.attempt_count == 2
    completed = queue.finish(
        str(task.id),
        worker_id="worker-b",
        status=TaskStatus.COMPLETED,
        progress={"verified": 1},
    )
    assert completed.status == TaskStatus.COMPLETED
    assert queue.request_cancellation(str(task.id)).status == TaskStatus.COMPLETED
    assert [event.sequence for event in queue.events_after(str(task.id))] == [1, 2, 3, 4, 5]
    assert queue.events_after(str(task.id), 5) == []
    with pytest.raises(ValidationError, match="cannot be negative"):
        queue.events_after(str(task.id), -1)
    with pytest.raises(NotFoundError):
        queue.get("missing")


def test_a_worker_records_the_build_it_started_on_not_a_later_checkout(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, later = BuildIdentity("git:started", "1.0"), BuildIdentity("git:later", "1.0")
    current_build.cache_clear()
    monkeypatch.setattr(build, "identify_build", lambda *_args: started)
    Worker(durable_database, worker_id="build-worker", handlers={})
    monkeypatch.setattr(build, "identify_build", lambda *_args: later)
    try:
        assert current_build() == started
    finally:
        current_build.cache_clear()


@pytest.mark.parametrize("alive", [True, None, False])
def test_continuation_claim_waits_for_its_physical_reader_or_confirmed_process_exit(
    durable_database: Database, alive: bool | None
) -> None:
    workspace_id = str(_workspace(durable_database).id)
    queue = TaskQueueService(durable_database, reader_alive=lambda _pid, _start: alive)
    root = queue.enqueue(
        workspace_id=workspace_id,
        task_type="artifact.verify",
        payload={"artifact_id": "artifact-1"},
        idempotency_key="reader-owner",
    )
    claimed = queue.claim_next(worker_id="worker-a")
    assert claimed is not None
    now = utc_now()
    with durable_database.session() as session:
        source = Source(
            workspace_id=workspace_id,
            name="Acme",
            source_type=SourceKind.AGGREGATOR.value,
            url="https://example.test/jobs",
            priority=1,
        )
        session.add(source)
        session.flush()
        scan = SourceScan(
            source_id=source.id,
            scanned_on=now.date(),
            query_label="titles",
            status="incomplete",
            scan_started_at=now,
            scan_completed_at=now,
        )
        session.add(scan)
        session.flush()
        member = SourceScanMember(
            workspace_id=workspace_id,
            root_task_id=str(root.id),
            source_id=source.id,
            ordinal=0,
            state="continuing",
            active_task_id=str(root.id),
            active_attempt_count=claimed.attempt_count,
            active_process_id=41,
            active_process_started_at="original",
            generation=1,
        )
        session.add(member)
        session.flush()
        continuation = SourceScanContinuation(
            workspace_id=workspace_id,
            source_scan_id=scan.id,
            generation=1,
            handoff_scopes=[],
            batch_member_id=member.id,
        )
        session.add(continuation)
        session.flush()
        continuation_id, member_id = continuation.id, member.id
    child = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.continue",
        payload={"continuation_id": continuation_id, "generation": 1},
        idempotency_key="reader-followup",
    )
    first = queue.claim_lane(worker_id="worker-a", scope="continuation")
    if alive is False:
        assert first is not None and first.task.id == child.id
        return
    assert first is None
    assert queue.get(str(child.id)).status == TaskStatus.QUEUED
    with durable_database.session() as session:
        held = session.get(SourceScanMember, member_id)
        assert held is not None
        held.active_task_id = None
        held.read_completed_at = now
    after_release = queue.claim_lane(worker_id="worker-a", scope="continuation")
    assert after_release is not None and after_release.task.id == child.id


@pytest.mark.parametrize("status", [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED])
def test_finishing_a_source_scan_task_drops_board_results_in_its_terminal_write(
    durable_database: Database, status: TaskStatus
) -> None:
    workspace_id = str(_workspace(durable_database).id)
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    queue = TaskQueueService(durable_database)
    roots = [
        queue.enqueue(
            workspace_id=workspace_id,
            task_type="source.scan",
            payload=validate_task_payload("source.scan", {}),
            idempotency_key=name,
        )
        for name in ("finishing-root", "other-root")
    ]
    claimed = queue.claim_next(worker_id="worker-a")
    assert claimed is not None and claimed.id == roots[0].id
    stored = {
        "board_result": {"new_discoveries": 1, "funnel": {"outcomes": []}},
        "detail_receipts": ["root:finishing-root:1"],
        "raw": {"listings": 1},
        "open": True,
    }
    with durable_database.session() as session:
        for ordinal, root in enumerate(roots):
            source = Source(
                workspace_id=workspace_id,
                name=f"Board {ordinal}",
                source_type=SourceKind.AGGREGATOR.value,
                url=f"https://example.test/jobs/{ordinal}",
                priority=1,
            )
            session.add(source)
            session.flush()
            session.add(
                SourceScanMember(
                    workspace_id=workspace_id,
                    root_task_id=str(root.id),
                    source_id=source.id,
                    ordinal=0,
                    state="completed",
                    result=stored,
                )
            )

    with pytest.raises(ConflictError, match="no longer owns"):
        queue.finish(str(roots[0].id), worker_id="worker-b", status=status)
    with durable_database.session() as session:
        members = list(session.scalars(select(SourceScanMember)))
        assert all(member.result == stored for member in members)

    finished = queue.finish(str(roots[0].id), worker_id="worker-a", status=status)

    assert finished.status == status
    with durable_database.session() as session:
        results = {
            member.root_task_id: member.result
            for member in session.scalars(select(SourceScanMember))
        }
    assert results[str(roots[0].id)] == {
        key: value for key, value in stored.items() if key != "board_result"
    }
    assert results[str(roots[1].id)] == stored


def test_claim_prioritizes_user_work_over_publisher_continuation(
    durable_database: Database,
) -> None:
    workspace_id = str(_workspace(durable_database).id)
    queue = TaskQueueService(durable_database)
    source_id = board_source(
        durable_database, workspace_id, "Globex", "https://jobs.example/globex"
    )
    completed_earlier_scan(durable_database, workspace_id, source_id, ())
    from sqlalchemy import select

    with durable_database.session() as session:
        continuation_id = session.scalars(select(SourceScanContinuation.id)).one()
    continuation = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.continue",
        payload={"continuation_id": continuation_id, "generation": 1},
        idempotency_key="continue-first",
    )
    user_task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="artifact.verify",
        payload={"artifact_id": "artifact-1"},
        idempotency_key="verify-second",
    )

    claimed = queue.claim_next(worker_id="worker-a")

    assert claimed is not None
    assert claimed.id == user_task.id
    assert queue.get(str(continuation.id)).status == TaskStatus.QUEUED


def test_worker_presence_expires_and_can_be_withdrawn(durable_database: Database) -> None:
    queue = TaskQueueService(durable_database)
    assert not queue.worker_available()

    queue.renew_worker_presence(worker_id="worker-a", ttl_seconds=10)
    assert queue.worker_available()

    with durable_database.session() as session:
        presence = session.get(WorkerPresence, "worker-a")
        assert presence is not None
        presence.available_until = utc_now() - timedelta(seconds=1)
    assert not queue.worker_available()

    queue.renew_worker_presence(worker_id="worker-a", ttl_seconds=10)
    queue.withdraw_worker_presence(worker_id="worker-a")
    assert not queue.worker_available()


def test_queue_facade_keeps_task_reads_and_event_history_workspace_scoped(
    durable_database: Database,
) -> None:
    workspace_id = str(_workspace(durable_database).id)
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="artifact.verify",
        payload={"artifact_id": "one"},
        idempotency_key="verify-one",
    )

    assert queue.get(str(task.id), workspace_id).id == task.id
    assert [item.id for item in queue.list_tasks(workspace_id)] == [task.id]
    events = queue.events_after(str(task.id), workspace_id=workspace_id)
    assert [item.event_type for item in events] == ["task.queued"]
    with pytest.raises(NotFoundError):
        queue.events_after(str(task.id), workspace_id="another-workspace")


def test_a_task_names_the_job_postings_its_payload_names(durable_database: Database) -> None:
    """A caller finds one job's work without being given the worker-only payload."""
    workspace_id = str(_workspace(durable_database).id)
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    artifacts = artifact_service(durable_database)
    first = job_search.commit_evaluation(
        durable_database, artifacts, evaluation_commit(workspace_id)
    ).job_posting_id
    second = job_search.commit_evaluation(
        durable_database,
        artifacts,
        evaluation_commit(
            workspace_id,
            title="Staff Data Engineer",
            canonical_url="https://example.test/jobs/43",
            discovery_url="https://example.test/jobs/43",
            requisition_id="REQ-43",
        ),
    ).job_posting_id
    queue = TaskQueueService(durable_database)
    one = queue.enqueue(
        workspace_id=workspace_id,
        task_type="posting.refresh",
        payload={"job_posting_id": first},
        idempotency_key="refresh-one",
    )
    several = queue.enqueue(
        workspace_id=workspace_id,
        task_type="workflow.batch",
        payload={"job_posting_ids": [first, second]},
        idempotency_key="batch-one",
    )
    none = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={},
        idempotency_key="scan-one",
    )

    listed = {item.id: item.job_posting_ids for item in queue.list_tasks(workspace_id)}

    assert listed[one.id] == [first]
    assert listed[several.id] == [first, second]
    assert listed[none.id] == []


def test_a_listed_task_names_the_pending_discoveries_its_payload_names(
    durable_database: Database,
) -> None:
    workspace_id = str(_workspace(durable_database).id)
    with durable_database.session() as session:
        for pending_id in ("pending-a", "pending-b", "pending-c"):
            session.add(
                PendingDiscovery(
                    id=pending_id,
                    workspace_id=workspace_id,
                    fingerprint=hashlib.sha256(pending_id.encode("ascii")).hexdigest(),
                    employer="Globex",
                    title="Data Platform Lead",
                    reason="Synthetic task target",
                    first_seen_on=utc_now().date(),
                )
            )
    queue = TaskQueueService(durable_database)
    batch = queue.enqueue(
        workspace_id=workspace_id,
        task_type="discovery.evaluate_batch",
        payload={"pending_discovery_ids": ["pending-a", "pending-b"]},
        idempotency_key="evaluate-batch-one",
    )
    single = queue.enqueue(
        workspace_id=workspace_id,
        task_type="discovery.evaluate",
        payload={"pending_discovery_id": "pending-c"},
        idempotency_key="evaluate-one",
    )

    listed = {item.id: item.pending_discovery_ids for item in queue.list_tasks(workspace_id)}

    assert listed[batch.id] == ["pending-a", "pending-b"]
    assert listed[single.id] == ["pending-c"]


def test_a_whole_scan_has_one_identity_and_a_named_board_another(
    durable_database: Database,
) -> None:
    workspace_id = str(_workspace(durable_database).id)
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    source_id = board_source(durable_database, workspace_id, "Acme", "https://jobs.example/acme")
    queue = TaskQueueService(durable_database)
    whole = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload=validate_task_payload("source.scan", {}),
        idempotency_key="scan-all",
    )
    one_board = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload=validate_task_payload("source.scan", {"source_ids": [source_id]}),
        idempotency_key="scan-source-a",
    )

    assert (
        queue.enqueue(
            workspace_id=workspace_id,
            task_type="source.scan",
            payload=validate_task_payload("source.scan", {"source_ids": []}),
            idempotency_key="scan-all",
        ).id
        == whole.id
    )
    assert one_board.id != whole.id
    assert queue.get(str(one_board.id)).source_ids == [source_id]
    assert queue.get(str(whole.id)).source_ids == []
    with pytest.raises(ConflictError, match="different payload"):
        queue.enqueue(
            workspace_id=workspace_id,
            task_type="source.scan",
            payload=validate_task_payload("source.scan", {"source_ids": [source_id]}),
            idempotency_key="scan-all",
        )


def test_the_database_rejects_an_unregistered_task_type(
    durable_database: Database,
) -> None:
    workspace_id = str(_workspace(durable_database).id)
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="artifact.verify",
        payload={"artifact_id": "one"},
        idempotency_key="verify-one",
    )
    with (
        pytest.raises(IntegrityError, match="ck_task_type"),
        durable_database.session() as session,
    ):
        session.execute(
            text("UPDATE background_tasks SET task_type = :task_type WHERE id = :task_id"),
            {"task_type": "workflow.screen", "task_id": str(task.id)},
        )


def test_the_stored_vocabulary_holds_a_task_type_the_queue_will_not_accept(
    durable_database: Database,
) -> None:
    """The CHECK is widened ahead of the handler, so the name is storable and unqueueable."""
    workspace_id = str(_workspace(durable_database).id)
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="artifact.verify",
        payload={"artifact_id": "two"},
        idempotency_key="verify-two",
    )

    with durable_database.session() as session:
        session.execute(
            text("UPDATE background_tasks SET task_type = :task_type WHERE id = :task_id"),
            {"task_type": "posting.resolve_batch", "task_id": str(task.id)},
        )

    with pytest.raises(ValidationError, match="Unsupported task type 'posting.resolve_batch'"):
        queue.enqueue(
            workspace_id=workspace_id,
            task_type="posting.resolve_batch",
            payload={},
            idempotency_key="resolve",
        )


def test_a_retired_task_type_is_omitted_from_reads_instead_of_failing_them(
    durable_database: Database,
) -> None:
    """Completed rows keep a retired name, and a typed read drops what it cannot validate."""
    workspace_id = str(_workspace(durable_database).id)
    queue = TaskQueueService(durable_database)
    retired = queue.enqueue(
        workspace_id=workspace_id,
        task_type="artifact.verify",
        payload={"artifact_id": "three"},
        idempotency_key="verify-three",
    )
    current = queue.enqueue(
        workspace_id=workspace_id,
        task_type="artifact.verify",
        payload={"artifact_id": "four"},
        idempotency_key="verify-four",
    )
    with durable_database.session() as session:
        session.execute(
            text("UPDATE background_tasks SET task_type = :task_type WHERE id = :task_id"),
            {"task_type": "posting.resolve_batch", "task_id": str(retired.id)},
        )

    assert [item.id for item in queue.list_tasks(workspace_id)] == [current.id]
    with pytest.raises(NotFoundError):
        queue.get(str(retired.id))


def test_a_second_worker_on_one_database_is_refused_rather_than_tolerated(
    durable_database: Database,
) -> None:
    """One worker per database is the contract, so the second one does not start.

    Two workers on one SQLite file duplicate claims and collide on event
    sequences, and nothing in the schema stops them.
    """
    queue = TaskQueueService(durable_database)
    queue.renew_worker_presence(worker_id="worker-a", ttl_seconds=10)

    with pytest.raises(ConflictError, match="one worker per database"):
        queue.renew_worker_presence(worker_id="worker-b", ttl_seconds=10)

    queue.renew_worker_presence(worker_id="worker-a", ttl_seconds=10)
    queue.withdraw_worker_presence(worker_id="worker-a")
    queue.renew_worker_presence(worker_id="worker-b", ttl_seconds=10)
    assert queue.worker_available()


def test_a_worker_takes_over_once_the_previous_presence_has_expired(
    durable_database: Database,
) -> None:
    """A worker that died without withdrawing must not lock the queue for good."""
    queue = TaskQueueService(durable_database)
    queue.renew_worker_presence(worker_id="worker-a", ttl_seconds=10)
    with durable_database.session() as session:
        presence = session.get(WorkerPresence, "worker-a")
        assert presence is not None
        presence.available_until = utc_now() - timedelta(seconds=1)

    queue.renew_worker_presence(worker_id="worker-b", ttl_seconds=10)

    assert queue.worker_available()


def test_a_claim_whose_task_moved_underneath_it_leases_nothing(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SQLite emits no row lock, so the claim has to check what it read is still true.

    `FOR UPDATE SKIP LOCKED` compiles away on SQLite, so two claims could read
    the same queued task and both lease it.
    """
    workspace_id = str(_workspace(durable_database).id)
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="artifact.verify",
        payload={"artifact_id": "one"},
        idempotency_key="verify-one",
    )
    reading = TaskLeases._claimable
    raced: list[str] = []

    def lose_the_race(session: Any, now: Any, **claim: Any) -> Any:
        candidate = reading(session, now, **claim)
        if not raced:
            raced.append("once")
            # The other worker commits between this read and the claim it leads to.
            TaskQueueService(durable_database).claim_next(worker_id="worker-a", lease_seconds=10)
        return candidate

    monkeypatch.setattr(TaskLeases, "_claimable", staticmethod(lose_the_race))

    assert queue.claim_next(worker_id="worker-b", lease_seconds=10) is None
    with durable_database.session() as session:
        held = session.get(BackgroundTask, str(task.id))
        assert held is not None
        assert (held.leased_by, held.attempt_count) == ("worker-a", 1)


def test_a_claim_whose_task_was_leased_before_it_loaded_the_row_leases_nothing(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = str(_workspace(durable_database).id)
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="artifact.verify",
        payload={"artifact_id": "one"},
        idempotency_key="verify-one",
    )
    loading = TaskLeases._loaded
    raced: list[str] = []

    def lose_the_race(session: Any, task_id: str) -> Any:
        if not raced:
            raced.append("once")
            # The other worker commits after the candidate id was chosen but before its row loads.
            TaskQueueService(durable_database).claim_next(worker_id="worker-a", lease_seconds=10)
        return loading(session, task_id)

    monkeypatch.setattr(TaskLeases, "_loaded", staticmethod(lose_the_race))

    assert queue.claim_next(worker_id="worker-b", lease_seconds=10) is None
    with durable_database.session() as session:
        held = session.get(BackgroundTask, str(task.id))
        assert held is not None
        assert (held.leased_by, held.attempt_count) == ("worker-a", 1)


def test_a_claim_whose_task_was_cancelled_before_it_loaded_the_row_leases_nothing(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = str(_workspace(durable_database).id)
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="artifact.verify",
        payload={"artifact_id": "one"},
        idempotency_key="verify-one",
    )
    loading = TaskLeases._loaded
    raced: list[str] = []

    def lose_the_race(session: Any, task_id: str) -> Any:
        if not raced:
            raced.append("once")
            # The API cancels after the candidate id was chosen but before its row loads.
            TaskQueueService(durable_database).request_cancellation(task_id)
        return loading(session, task_id)

    monkeypatch.setattr(TaskLeases, "_loaded", staticmethod(lose_the_race))

    assert queue.claim_next(worker_id="worker-b", lease_seconds=10) is None
    with durable_database.session() as session:
        held = session.get(BackgroundTask, str(task.id))
        assert held is not None
        assert (held.status, held.leased_by) == (TaskStatus.CANCELLED, None)


def test_an_event_sequence_another_writer_took_is_retried(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The API and the worker append to one task and can read the same sequence."""
    workspace_id = str(_workspace(durable_database).id)
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="artifact.verify",
        payload={"artifact_id": "one"},
        idempotency_key="verify-one",
    )
    taken = TaskEventLog._next_sequence
    seen: list[str] = []

    def stale_once(session: Any, task_id: str) -> int:
        seen.append(task_id)
        return 1 if len(seen) == 1 else int(taken(session, task_id))

    monkeypatch.setattr(TaskEventLog, "_next_sequence", staticmethod(stale_once))

    queue.request_cancellation(str(task.id))

    assert [item.sequence for item in queue.events_after(str(task.id))] == [1, 2]


def test_worker_startup_runs_one_calendar_catch_up_before_dispatch(
    durable_database: Database,
) -> None:
    workspace = _workspace(durable_database)
    state_target_titles(durable_database, workspace.id, "Operations Analyst")
    board_source(durable_database, workspace.id, "Acme", "https://jobs.acme.test/")
    now = datetime(2026, 10, 8, 12, tzinfo=UTC)
    schedule = ScanScheduleService(durable_database, clock=lambda: now)
    schedule.save(
        workspace.id,
        ScanScheduleInput(expected_revision=0, timezone="UTC", local_time="09:00"),
    )
    now = datetime(2026, 10, 12, 10, tzinfo=UTC)
    observed: list[str] = []

    def read(context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        assert payload["source_ids"] == []
        observed.append(context.task_id)
        worker.request_stop()
        return {"scanned": []}

    worker = Worker(
        durable_database,
        worker_id="calendar-worker",
        handlers={"source.scan": read},
        wall_clock=lambda: now,
        sleep=lambda _seconds: worker.request_stop(),
    )
    worker.run_forever()
    with durable_database.session() as session:
        task = session.scalars(select(BackgroundTask)).one()
        assert task.task_type == "source.scan"
        assert task.status == "completed"
        assert observed == [task.id]
    state = schedule.read(workspace.id).state
    assert state is not None
    assert state.last_due_at == datetime(2026, 10, 9, 9, tzinfo=UTC)
    assert state.next_run_at == datetime(2026, 10, 13, 9, tzinfo=UTC)
    assert not worker.queue.worker_available()


def test_a_refused_second_worker_does_not_claim_a_due_calendar(
    durable_database: Database,
) -> None:
    workspace = _workspace(durable_database)
    state_target_titles(durable_database, workspace.id, "Operations Analyst")
    board_source(durable_database, workspace.id, "Acme", "https://jobs.acme.test/")
    now = datetime(2026, 10, 8, 12, tzinfo=UTC)
    schedule = ScanScheduleService(durable_database, clock=lambda: now)
    saved = schedule.save(workspace.id, ScanScheduleInput(expected_revision=0, timezone="UTC"))
    now = datetime(2026, 10, 12, 10, tzinfo=UTC)
    queue = TaskQueueService(durable_database)
    queue.renew_worker_presence(worker_id="first-calendar-worker")
    worker = Worker(
        durable_database, worker_id="second-calendar-worker", handlers={}, wall_clock=lambda: now
    )
    with pytest.raises(ConflictError):
        worker.run_forever()
    assert schedule.read(workspace.id) == saved
    with durable_database.session() as session:
        assert list(session.scalars(select(BackgroundTask))) == []
    queue.withdraw_worker_presence(worker_id="first-calendar-worker")


def test_a_locked_presence_renewal_is_retried_on_the_next_beat(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = TaskQueueService(durable_database)
    worker = Worker(durable_database, worker_id="worker-a", handlers={}, sleep=lambda _s: None)
    original_renew = worker.queue.renew_worker_presence
    renewals = 0

    def locked_once(**kwargs: Any) -> object:
        nonlocal renewals
        renewals += 1
        if renewals == 1:
            raise OperationalError("renew presence", {}, RuntimeError("database is locked"))
        return original_renew(**kwargs)

    def stop_after_presence_is_visible() -> bool:
        assert queue.worker_available()
        raise KeyboardInterrupt

    monkeypatch.setattr(worker.queue, "renew_worker_presence", locked_once)
    monkeypatch.setattr(worker, "dispatch", stop_after_presence_is_visible)
    with pytest.raises(KeyboardInterrupt):
        worker.run_forever(poll_seconds=0.1)
    assert renewals == 2
    assert not queue.worker_available()


def test_task_enqueue_refuses_an_archived_or_cross_workspace_job(
    durable_database: Database,
) -> None:
    workspace = _workspace(durable_database)
    workspace_id = str(workspace.id)
    committed = job_search.commit_evaluation(
        durable_database,
        artifact_service(durable_database),
        evaluation_commit(workspace_id),
    )
    job_id = committed.job_posting_id
    job_search.archive_job(durable_database, job_id, workspace_id)

    with pytest.raises(ConflictError, match="This job is archived"):
        TaskQueueService(durable_database).enqueue(
            workspace_id=workspace_id,
            task_type="posting.refresh",
            payload={"job_posting_id": job_id},
            idempotency_key="archived-job",
        )
    other = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="other-tasks", name="Other Tasks"
    )
    job_search.unarchive_job(durable_database, job_id, workspace_id)
    with pytest.raises(NotFoundError, match="does not exist in this workspace"):
        TaskQueueService(durable_database).enqueue(
            workspace_id=str(other.id),
            task_type="posting.refresh",
            payload={"job_posting_id": job_id},
            idempotency_key="cross-workspace-job",
        )


def test_queued_and_running_cancellation_are_append_only(durable_database: Database) -> None:
    workspace_id = _workspace(durable_database).id
    queue = TaskQueueService(durable_database)
    queued = queue.enqueue(
        workspace_id=workspace_id,
        task_type="artifact.verify",
        payload={},
        idempotency_key="queued-cancel",
    )
    assert queue.request_cancellation(str(queued.id)).status == TaskStatus.CANCELLED
    assert queue.claim_next(worker_id="worker") is None

    running = queue.enqueue(
        workspace_id=workspace_id,
        task_type="artifact.verify",
        payload={},
        idempotency_key="running-cancel",
    )
    assert queue.claim_next(worker_id="worker") is not None
    requested = queue.request_cancellation(str(running.id))
    assert requested.status == TaskStatus.RUNNING
    assert requested.cancellation_requested_at is not None

    with durable_database.session() as session:
        abandoned = session.get(BackgroundTask, str(running.id))
        assert abandoned is not None
        abandoned.lease_expires_at = utc_now() - timedelta(seconds=1)

    handler_calls: list[str] = []

    def must_not_run(context: object, payload: dict[str, object]) -> dict[str, object]:
        del context, payload
        handler_calls.append("called")
        return {}

    recovery_worker = Worker(
        durable_database,
        worker_id="recovery-worker",
        handlers={"artifact.verify": must_not_run},
    )
    assert recovery_worker.run_once()
    assert handler_calls == []

    recovered = queue.get(str(running.id))
    assert recovered.status == TaskStatus.CANCELLED
    assert recovered.attempt_count == 2
    assert recovered.completed_at is not None
    with durable_database.session() as session:
        finished = session.get(BackgroundTask, str(running.id))
        assert finished is not None
        assert finished.leased_by is None
        assert finished.lease_expires_at is None
    event_types = [event.event_type for event in queue.events_after(str(running.id))]
    assert event_types.count("task.recovered") == 1
    assert event_types.count("task.cancelled") == 1


@pytest.mark.parametrize("status", [TaskStatus.COMPLETED, TaskStatus.FAILED])
@pytest.mark.parametrize("cancellation_timing", ["before_finish", "after_read"])
def test_a_requested_cancellation_wins_over_the_handlers_terminal_outcome(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    status: TaskStatus,
    cancellation_timing: str,
) -> None:
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=_workspace(durable_database).id,
        task_type="artifact.verify",
        payload={},
        idempotency_key="terminal-cancellation",
    )
    task_id = str(task.id)
    claimed = queue.claim_next(worker_id="worker")
    assert claimed is not None
    queue.heartbeat(task_id, worker_id="worker", progress={"retained": 1})
    if cancellation_timing == "before_finish":
        queue.request_cancellation(task_id)
    else:
        reading = TaskLeases._owned_running

        def cancel_after_read(session: Any, task_id: str, worker_id: str) -> BackgroundTask:
            held = reading(session, task_id, worker_id)
            queue.request_cancellation(task_id)
            assert held.cancellation_requested_at is None
            return held

        monkeypatch.setattr(TaskLeases, "_owned_running", staticmethod(cancel_after_read))
    finished = queue.finish(
        task_id,
        worker_id="worker",
        expected_attempt=claimed.attempt_count,
        status=status,
        progress={"replacement": 1},
        error_category="ConflictError",
        error_message="The source read lost its generation.",
    )
    assert finished.status == TaskStatus.CANCELLED
    assert finished.error_category is None
    assert finished.error_message is None
    assert finished.progress == {"retained": 1}
    assert finished.completed_at is not None
    assert queue.get(task_id) == finished
    terminal = queue.events_after(task_id)[-1]
    assert terminal.stage == finished.status.value
    assert terminal.event_type == "task.cancelled"
    assert terminal.payload == {"error_category": None}
    with durable_database.session() as session:
        stored = session.get(BackgroundTask, task_id)
        assert stored is not None
        assert stored.leased_by is None
        assert stored.lease_expires_at is None


@pytest.mark.parametrize("status", [TaskStatus.COMPLETED, TaskStatus.FAILED])
def test_an_unrequested_terminal_outcome_retains_its_status_and_error(
    durable_database: Database, status: TaskStatus
) -> None:
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=_workspace(durable_database).id,
        task_type="artifact.verify",
        payload={},
        idempotency_key="terminal-outcome",
    )
    task_id = str(task.id)
    claimed = queue.claim_next(worker_id="worker")
    assert claimed is not None
    category = "ConflictError" if status == TaskStatus.FAILED else None
    message = "The source read lost its generation." if category else None
    finished = queue.finish(
        task_id,
        worker_id="worker",
        expected_attempt=claimed.attempt_count,
        status=status,
        progress={"finished": 1},
        error_category=category,
        error_message=message,
    )
    assert finished.status == status
    assert finished.error_category == category
    assert finished.error_message == message
    assert finished.progress == {"finished": 1}
    assert queue.get(task_id) == finished
    terminal = queue.events_after(task_id)[-1]
    assert terminal.stage == status.value
    assert terminal.event_type == f"task.{status.value}"
    assert terminal.payload == {"error_category": category}


@pytest.mark.parametrize("stale_ownership", ["worker", "attempt"])
def test_a_requested_cancellation_does_not_allow_a_stale_worker_to_finish(
    durable_database: Database, stale_ownership: str
) -> None:
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=_workspace(durable_database).id,
        task_type="artifact.verify",
        payload={},
        idempotency_key="stale-terminal-cancellation",
    )
    task_id = str(task.id)
    claimed = queue.claim_next(worker_id="worker")
    assert claimed is not None
    requested = queue.request_cancellation(task_id)
    events = queue.events_after(task_id)
    with pytest.raises(ConflictError, match="no longer owns"):
        queue.finish(
            task_id,
            worker_id="stale-worker" if stale_ownership == "worker" else "worker",
            expected_attempt=claimed.attempt_count + (stale_ownership == "attempt"),
            status=TaskStatus.COMPLETED,
        )
    assert queue.get(task_id) == requested
    assert queue.events_after(task_id) == events


def test_failed_or_cancelled_task_retry_is_idempotent(durable_database: Database) -> None:
    workspace_id = _workspace(durable_database).id
    queue = TaskQueueService(durable_database)
    original = queue.enqueue(
        workspace_id=workspace_id,
        task_type="artifact.verify",
        payload={"artifact_id": "one"},
        idempotency_key="retry-source",
    )
    queue.request_cancellation(str(original.id))
    retried = queue.retry(str(original.id))
    assert retried.status == TaskStatus.QUEUED
    assert queue.retry(str(original.id)).id == retried.id
    with pytest.raises(ValidationError, match="failed or cancelled"):
        queue.retry(str(retried.id))


def test_worker_persists_success_failure_and_cooperative_cancellation(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database).id
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    queue = TaskQueueService(durable_database)

    def success(context: object, payload: dict[str, object]) -> dict[str, object]:
        del context
        return {"seen": payload["value"]}

    success_task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="artifact.verify",
        payload={"value": 7},
        idempotency_key="success",
    )
    worker = Worker(durable_database, worker_id="worker", handlers={"artifact.verify": success})
    assert worker.run_once()
    assert queue.get(str(success_task.id)).progress == {"seen": 7}
    assert not worker.run_once()

    # A registered task this worker was not built with, which is the only way an
    # unsupported type reaches a worker, since the queue rejects unknown names.
    unsupported = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={},
        idempotency_key="unsupported",
    )
    assert worker.run_once()
    unsupported_state = queue.get(str(unsupported.id))
    assert unsupported_state.error_category == "unsupported_task_type"

    def fail(context: object, payload: dict[str, object]) -> dict[str, object]:
        del context, payload
        raise RuntimeError("bounded failure\nwithout a traceback")

    failed = queue.enqueue(
        workspace_id=workspace_id,
        task_type="document.render",
        payload={},
        idempotency_key="failed",
    )
    failing_worker = Worker(
        durable_database,
        worker_id="failing",
        handlers={"document.render": fail},
    )
    assert failing_worker.run_once()
    failed_state = queue.get(str(failed.id))
    assert failed_state.error_category == "RuntimeError"
    assert failed_state.error_message == "bounded failure without a traceback"

    def cancel(context: object, payload: dict[str, object]) -> dict[str, object]:
        from clearscout.bootstrap.worker import TaskContext

        del payload
        assert isinstance(context, TaskContext)
        context.queue.request_cancellation(context.task_id)
        context.raise_if_cancelled()
        return {}

    cancelled = queue.enqueue(
        workspace_id=workspace_id,
        task_type="posting.refresh",
        payload={},
        idempotency_key="cancelled",
    )
    cancelling_worker = Worker(
        durable_database,
        worker_id="cancelling",
        handlers={"posting.refresh": cancel},
    )
    assert cancelling_worker.run_once()
    assert queue.get(str(cancelled.id)).status == TaskStatus.CANCELLED


@pytest.mark.parametrize("handler_outcome", ["completed", "failed", "unsupported"])
@pytest.mark.parametrize("cancel_before_finish", [False, True])
def test_worker_terminal_log_matches_persisted_outcome_after_late_cancellation(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    handler_outcome: str,
    cancel_before_finish: bool,
) -> None:
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=_workspace(durable_database).id,
        task_type="artifact.verify",
        payload={},
        idempotency_key="logged-terminal-outcome",
    )

    def handler(context: object, payload: dict[str, object]) -> dict[str, object]:
        del context, payload
        if handler_outcome == "failed":
            raise RuntimeError("Synthetic handler failure")
        return {"retained": True}

    worker = Worker(
        durable_database,
        worker_id="terminal-log-worker",
        handlers={} if handler_outcome == "unsupported" else {"artifact.verify": handler},
    )
    finish = worker.queue.finish

    def cancel_then_finish(*args: Any, **kwargs: Any) -> Any:
        if cancel_before_finish:
            queue.request_cancellation(str(task.id))
        return finish(*args, **kwargs)

    monkeypatch.setattr(worker.queue, "finish", cancel_then_finish)
    with caplog.at_level("INFO", logger="clearscout.bootstrap.worker"):
        assert worker.run_once()
    persisted = queue.get(str(task.id))
    expected = (
        "cancelled"
        if cancel_before_finish
        else ("failed" if handler_outcome == "unsupported" else handler_outcome)
    )
    assert persisted.status.value == expected
    assert queue.events_after(str(task.id))[-1].stage == expected
    logged = [
        record
        for record in caplog.records
        if record.msg == "task_execution" and getattr(record, "state", None) == "finished"
    ]
    assert len(logged) == 1
    assert getattr(logged[0], "status", None) == persisted.status.value
    assert getattr(logged[0], "error_type", "missing") == persisted.error_category


def test_worker_renews_the_lease_while_a_handler_is_blocked(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch, lease_ticks: Event
) -> None:
    workspace_id = _workspace(durable_database).id
    queue = TaskQueueService(durable_database)
    started = Event()
    release = Event()

    def block(context: object, payload: dict[str, object]) -> dict[str, object]:
        del context, payload
        started.set()
        release.wait()
        return {"finished": True}

    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="artifact.verify",
        payload={},
        idempotency_key="blocked-handler",
    )
    worker = Worker(
        durable_database,
        worker_id="blocking-worker",
        handlers={"artifact.verify": block},
        lease_heartbeat_seconds=0.01,
    )
    first_renewed = Event()
    second_renewed = Event()
    original_heartbeat = worker.queue.heartbeat

    def record_heartbeat(*args: object, **kwargs: object) -> object:
        result = cast(Any, original_heartbeat)(*args, **kwargs)
        (first_renewed if not first_renewed.is_set() else second_renewed).set()
        return result

    monkeypatch.setattr(worker.queue, "heartbeat", record_heartbeat)
    thread = Thread(target=worker.run_once)
    thread.start()
    started.wait()
    lease_ticks.set()
    first_renewed.wait()
    with durable_database.session() as session:
        running = session.get(BackgroundTask, task.id)
        assert running is not None
        first_heartbeat = running.heartbeat_at
    lease_ticks.set()
    second_renewed.wait()
    with durable_database.session() as session:
        renewed = session.get(BackgroundTask, task.id)
        assert renewed is not None
        assert renewed.heartbeat_at != first_heartbeat

    assert queue.worker_available()
    assert queue.get(str(task.id)).status == TaskStatus.RUNNING
    release.set()
    thread.join()
    assert not thread.is_alive()
    assert queue.get(str(task.id)).status == TaskStatus.COMPLETED


def test_worker_fails_a_task_when_lease_renewal_dies(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch, lease_ticks: Event
) -> None:
    workspace_id = _workspace(durable_database).id
    queue = TaskQueueService(durable_database)
    renewal_failed = Event()
    handler_calls = 0

    def handler(context: object, payload: object) -> dict[str, bool]:
        nonlocal handler_calls
        del context, payload
        handler_calls += 1
        lease_ticks.set()
        renewal_failed.wait()
        return {"finished": True}

    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="artifact.verify",
        payload={},
        idempotency_key="dead-lease-renewal",
    )
    worker = Worker(
        durable_database,
        worker_id="dead-lease-worker",
        handlers={"artifact.verify": handler},
        lease_heartbeat_seconds=0.01,
    )

    def fail_renewal(*args: object, **kwargs: object) -> None:
        del args, kwargs
        renewal_failed.set()
        raise ConflictError("lease ownership was lost")

    monkeypatch.setattr(worker.queue, "heartbeat", fail_renewal)

    assert worker.run_once()
    failed = queue.get(str(task.id))
    assert handler_calls == 1
    assert failed.status == TaskStatus.FAILED
    assert failed.error_category == "lease_renewal_died"


def test_worker_stops_lease_renewal_when_a_handler_interrupts(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch, lease_ticks: Event
) -> None:
    workspace_id = _workspace(durable_database).id
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="artifact.verify",
        payload={},
        idempotency_key="interrupted-handler",
    )
    first_renewal = Event()
    renewal_calls = 0

    def interrupt(context: object, payload: object) -> dict[str, bool]:
        del context, payload
        lease_ticks.set()
        first_renewal.wait()
        raise KeyboardInterrupt

    worker = Worker(
        durable_database,
        worker_id="interrupted-worker",
        handlers={"artifact.verify": interrupt},
        lease_heartbeat_seconds=0.01,
    )
    original_heartbeat = worker.queue.heartbeat

    def record_renewal(*args: object, **kwargs: object) -> object:
        nonlocal renewal_calls
        renewal_calls += 1
        first_renewal.set()
        return cast(Any, original_heartbeat)(*args, **kwargs)

    monkeypatch.setattr(worker.queue, "heartbeat", record_renewal)
    stopped_threads: list[Thread] = []
    original_stop = _LeaseKeeper.stop

    def record_stop(self: _LeaseKeeper) -> None:
        original_stop(self)
        stopped_threads.append(self._thread)

    monkeypatch.setattr(_LeaseKeeper, "stop", record_stop)

    with pytest.raises(KeyboardInterrupt):
        worker.run_once()
    assert renewal_calls >= 1
    # The renewal thread must be joined, not merely signaled, before the interrupt propagates.
    assert stopped_threads and not stopped_threads[0].is_alive()
    assert queue.get(str(task.id)).status == TaskStatus.RUNNING


def test_worker_contains_a_lost_ownership_terminal_write(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = _workspace(durable_database).id
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="artifact.verify",
        payload={},
        idempotency_key="lost-terminal-ownership",
    )
    worker = Worker(
        durable_database,
        worker_id="ownership-worker",
        handlers={"artifact.verify": lambda context, payload: {"finished": True}},
    )
    finish_calls = 0

    def lost_finish(*args: object, **kwargs: object) -> object:
        nonlocal finish_calls
        finish_calls += 1
        raise ConflictError("The worker no longer owns this task.")

    monkeypatch.setattr(worker.queue, "finish", lost_finish)

    assert worker.run_once()
    assert finish_calls == 1
    assert queue.get(str(task.id)).status == TaskStatus.RUNNING


def test_worker_retries_a_transient_terminal_write_without_replaying_work(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = _workspace(durable_database).id
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="artifact.verify",
        payload={},
        idempotency_key="transient-terminal-write",
    )
    handler_calls = 0

    def handler(context: object, payload: object) -> dict[str, bool]:
        nonlocal handler_calls
        handler_calls += 1
        return {"finished": True}

    worker = Worker(
        durable_database,
        worker_id="terminal-retry-worker",
        handlers={"artifact.verify": handler},
        sleep=lambda _seconds: None,
    )
    original_finish = worker.queue.finish
    finish_calls = 0

    def transient_finish(*args: object, **kwargs: object) -> object:
        nonlocal finish_calls
        finish_calls += 1
        if finish_calls < 3:
            raise OperationalError("finish task", {}, RuntimeError("database is busy"))
        return cast(Any, original_finish)(*args, **kwargs)

    monkeypatch.setattr(worker.queue, "finish", transient_finish)

    assert worker.run_once()
    assert finish_calls == 3
    assert handler_calls == 1
    assert queue.get(str(task.id)).status == TaskStatus.COMPLETED
    assert not worker.run_once()


def test_artifact_verifier_never_rewrites_indexed_content(
    durable_database: Database, tmp_path: Path
) -> None:
    source = tmp_path / "workspace" / "resume.md"
    source.parent.mkdir()
    source.write_bytes(b"evidence")
    workspace_id = _workspace(durable_database, root_path=str(source.parent)).id
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    with durable_database.session() as session:
        artifact = Artifact(
            workspace_id=workspace_id,
            logical_type="resume.markdown",
            mime_type="text/markdown",
            storage_uri=source.resolve().as_uri(),
            byte_size=source.stat().st_size,
            sha256=expected,
            provenance={"source": "test"},
        )
        session.add(artifact)
        session.flush()
        artifact_id = artifact.id

    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="artifact.verify",
        payload={"artifact_id": artifact_id},
        idempotency_key="verify",
    )
    before = source.read_bytes()
    worker = Worker(
        durable_database,
        worker_id="verifier",
        handlers={"artifact.verify": ArtifactVerifier(durable_database, tmp_path / "store")},
    )
    assert worker.run_once()
    state = queue.get(str(task.id))
    assert state.progress["integrity_status"] == "verified"
    assert source.read_bytes() == before

    source.write_bytes(b"changed")
    second = queue.enqueue(
        workspace_id=workspace_id,
        task_type="artifact.verify",
        payload={"artifact_id": artifact_id},
        idempotency_key="verify-changed",
    )
    assert worker.run_once()
    assert queue.get(str(second.id)).progress["integrity_status"] == "changed"


def test_a_rejected_row_survives_a_transient_terminal_write(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = _workspace(durable_database).id
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="artifact.verify",
        payload={"artifact_id": "one"},
        idempotency_key="corrupt-transient",
    )
    with durable_database.session() as session:
        record = session.get(BackgroundTask, str(task.id))
        assert record is not None
        record.payload = cast(dict[str, Any], ["not", "an", "object"])

    worker = Worker(
        durable_database,
        worker_id="worker",
        handlers={"artifact.verify": lambda context, payload: {}},
        sleep=lambda _seconds: None,
    )
    original_finish = worker.queue.finish
    finish_calls = 0

    def transient_finish(*args: object, **kwargs: object) -> object:
        nonlocal finish_calls
        finish_calls += 1
        if finish_calls < 3:
            raise OperationalError("finish task", {}, RuntimeError("database is busy"))
        return cast(Any, original_finish)(*args, **kwargs)

    monkeypatch.setattr(worker.queue, "finish", transient_finish)

    assert worker.run_once()
    assert finish_calls == 3
    state = queue.get(str(task.id))
    assert state.status == TaskStatus.FAILED
    assert state.error_category == "invalid_payload"


def test_an_unsupported_task_type_survives_a_transient_terminal_write(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = _workspace(durable_database).id
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="artifact.verify",
        payload={"artifact_id": "one"},
        idempotency_key="unsupported-transient",
    )

    worker = Worker(durable_database, worker_id="worker", handlers={}, sleep=lambda _seconds: None)
    original_finish = worker.queue.finish
    finish_calls = 0

    def transient_finish(*args: object, **kwargs: object) -> object:
        nonlocal finish_calls
        finish_calls += 1
        if finish_calls < 3:
            raise OperationalError("finish task", {}, RuntimeError("database is busy"))
        return cast(Any, original_finish)(*args, **kwargs)

    monkeypatch.setattr(worker.queue, "finish", transient_finish)

    assert worker.run_once()
    assert finish_calls == 3
    state = queue.get(str(task.id))
    assert state.status == TaskStatus.FAILED
    assert state.error_category == "unsupported_task_type"


def test_the_task_vocabulary_and_the_handler_registry_name_the_same_tasks() -> None:
    assert get_args(TaskType) == task_types()


def test_the_task_catalog_does_not_construct_the_worker_handler_graph() -> None:
    assert [field.name for field in fields(TaskSpec)] == [
        "name",
        "payload_model",
        "paid",
        "cost",
    ]


def test_the_worker_composes_one_handler_for_every_registered_task(
    durable_database: Database,
) -> None:
    handlers = default_handlers(durable_database)
    assert tuple(handlers) == task_types()
    assert {name: type(handler).__name__ for name, handler in handlers.items()} == {
        "artifact.verify": "ArtifactVerifier",
        "posting.refresh": "PostingRefreshHandler",
        "posting.refresh_batch": "PostingRefreshBatchHandler",
        "posting.refresh_with_reextraction": "PostingRefreshWithReextractionHandler",
        "posting.refresh_with_rematch": "PostingRefreshWithRematchHandler",
        "posting.recover_batch": "AssistedContentResolutionHandler",
        "discovery.evaluate": "EvaluationHandler",
        "discovery.evaluate_batch": "DiscoveryEvaluateBatchHandler",
        "discovery.refresh_batch": "DiscoveryRefreshBatchHandler",
        "discovery.rescreen_queue": "PendingQueueRescreenHandler",
        "discovery.relevance_review": "RelevanceReviewHandler",
        "workflow.tailor": "TailoringHandler",
        "workflow.approve": "TailoringHandler",
        "workflow.batch": "BatchHandler",
        "workflow.discovery": "DiscoveryHandler",
        "document.render": "DocumentRenderHandler",
        "source.scan": "SourceScanHandler",
        "source.continue": "SourceContinuationHandler",
        "search.guided_campaign": "GuidedCampaignHandler",
        "search.guided_campaign_retry": "GuidedCampaignRetryHandler",
        "search.vocabulary": "SearchVocabularyHandler",
    }
    workflow_services = [
        cast(Any, handlers[name]).service
        for name in (
            "workflow.tailor",
            "workflow.approve",
            "workflow.batch",
            "workflow.discovery",
        )
    ]
    assert all(service is workflow_services[0] for service in workflow_services)


def test_the_composed_continuation_handler_starts_each_pass_with_a_clean_ledger_scope(
    durable_database: Database,
) -> None:
    """A continuation sharing the scan service must reset its memo, or a retry double counts."""
    handlers = default_handlers(durable_database)
    scan_service = cast(Any, handlers["source.scan"]).service

    boundary = cast(Any, handlers["source.continue"]).begin_recording_pass

    assert boundary is not None
    assert boundary == scan_service.begin_recording_pass


def test_the_production_handler_graph_wires_a_live_workday_detail_checker(
    durable_database: Database,
) -> None:
    """source.scan and source.continue share one BoardSweep, so wiring either checks both."""
    handlers = default_handlers(durable_database)
    scan = cast(Any, handlers["source.scan"])
    assert scan.service.sweep.posting_checker is not None
    continuation = cast(Any, handlers["source.continue"])
    assert continuation.sweep is scan.service.sweep


def test_the_production_handler_graph_reads_the_web_through_one_polite_client(
    durable_database: Database,
) -> None:
    """One client holds each host's robots cache and pacing, so every reader must share it."""
    handlers = default_handlers(durable_database)
    scan = cast(Any, handlers["source.scan"]).service
    posting_fetcher = cast(Any, handlers["posting.refresh"]).service.fetcher

    shared = scan.fetcher.client
    assert scan.sweep.posting_checker.keywords["client"] is shared
    assert posting_fetcher.client is shared
    assert scan.sweep.content_resolution.resolver.client is shared
    for task_type in ("discovery.evaluate", "posting.refresh_with_rematch"):
        workflows = cast(Any, handlers[task_type]).service.workflows
        assert workflows.research_fetcher.client is shared


def test_only_the_tasks_that_can_reach_a_paid_model_are_marked_as_spending() -> None:
    # Completeness against the registry is a type. Which tasks can spend money is
    # not, and claiming one can when it cannot is a false claim about a bill.
    assert sorted(spec.name for spec in TASK_SPECS if spec.paid) == [
        "discovery.evaluate",
        "discovery.evaluate_batch",
        "discovery.relevance_review",
        "posting.recover_batch",
        "posting.refresh_with_reextraction",
        "posting.refresh_with_rematch",
        "search.guided_campaign",
        "search.vocabulary",
        "workflow.approve",
        "workflow.batch",
        "workflow.tailor",
    ]


def test_the_priced_search_and_the_tailoring_reuse_condition_are_stated() -> None:
    costs = {spec.name: spec.cost for spec in TASK_SPECS}
    assert "Provider charges may include model tokens" in costs["search.guided_campaign"]
    assert "web-content-retrieval charge" in costs["search.guided_campaign"]
    assert "Costs are estimates and do not stop an approved task" in costs["search.guided_campaign"]
    assert "matching completed package" in costs["workflow.tailor"]
    assert "unless regeneration is requested" in costs["workflow.tailor"]


def test_every_registered_task_states_whether_it_can_reach_a_paid_provider() -> None:
    paid = {spec.name for spec in TASK_SPECS if spec.paid}
    assert paid and paid != {spec.name for spec in TASK_SPECS}
    for spec in TASK_SPECS:
        if spec.paid:
            assert spec.cost != UNPAID
            assert "charge" in spec.cost
        else:
            assert spec.cost == UNPAID


def test_the_cost_catalog_answers_for_every_registered_task() -> None:
    catalog = task_type_catalog()
    assert tuple(entry.task_type for entry in catalog) == task_types()
    assert {(entry.paid, entry.cost) for entry in catalog} == {
        (spec.paid, spec.cost) for spec in TASK_SPECS
    }


def test_recovery_leaves_a_renewed_task_with_its_original_owner(durable_database, monkeypatch):
    workspace_id = str(_workspace(durable_database).id)
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="artifact.verify",
        payload={"artifact_id": "one"},
        idempotency_key="renewed",
    )
    queue.claim_next(worker_id="original", lease_seconds=10)
    with durable_database.session() as session:
        held = session.get(BackgroundTask, str(task.id))
        held.lease_expires_at = utc_now() - timedelta(seconds=1)
    reading = TaskLeases._claimable

    def renew_after_read(session, now, **claim):
        candidate = reading(session, now, **claim)
        queue.heartbeat(str(task.id), worker_id="original", lease_seconds=3600)
        return candidate

    monkeypatch.setattr(TaskLeases, "_claimable", staticmethod(renew_after_read))
    assert queue.claim_next(worker_id="recovery") is None
    assert queue.get(str(task.id)).attempt_count == 1


@pytest.mark.parametrize("operation", ["heartbeat", "finish"])
@pytest.mark.parametrize("recovery_worker", ["original", "replacement"])
def test_a_stale_lease_write_preserves_the_task_recovered_after_its_read(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    recovery_worker: str,
) -> None:
    workspace_id = str(_workspace(durable_database).id)
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="artifact.verify",
        payload={},
        idempotency_key="stale-write",
    )
    claimed = queue.claim_next(worker_id="original", lease_seconds=10)
    assert claimed is not None
    reading = TaskLeases._owned_running
    recovered_events: list[object] = []
    recovered: list[object] = []

    def recover_after_read(session: Any, task_id: str, worker_id: str) -> Any:
        held = reading(session, task_id, worker_id)
        with durable_database.session() as competing:
            stale = competing.get(BackgroundTask, task_id)
            assert stale is not None
            stale.lease_expires_at = utc_now() - timedelta(seconds=1)
        replacement = TaskQueueService(durable_database)
        assert replacement.claim_next(worker_id=recovery_worker) is not None
        recovered.append(replacement.get(task_id))
        recovered_events.extend(replacement.events_after(task_id))
        return held

    monkeypatch.setattr(TaskLeases, "_owned_running", staticmethod(recover_after_read))
    write = queue.heartbeat if operation == "heartbeat" else queue.finish
    arguments: dict[str, Any] = {"worker_id": "original", "progress": {"stale": True}}
    if operation == "finish":
        arguments["status"] = TaskStatus.COMPLETED
    with pytest.raises(ConflictError, match="no longer owns"):
        write(str(task.id), **arguments)

    assert queue.get(str(task.id)) == recovered[0]
    assert queue.events_after(str(task.id)) == recovered_events


@pytest.mark.parametrize("operation", ["progress", "finish", "renewal"])
def test_a_worker_does_not_write_into_a_later_attempt_with_the_same_worker_id(
    durable_database: Database,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    lease_ticks: Event,
    operation: str,
) -> None:
    workspace_id = str(_workspace(durable_database).id)
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="artifact.verify",
        payload={},
        idempotency_key="reused-worker-id",
    )
    recovered: list[object] = []
    recovered_events: list[object] = []
    renewal_finished = Event()

    def handler(context: Any, payload: object) -> dict[str, bool]:
        with durable_database.session() as session:
            held = session.get(BackgroundTask, str(task.id))
            assert held is not None
            held.lease_expires_at = utc_now() - timedelta(seconds=1)
        replacement = queue.claim_next(worker_id="same-worker")
        assert replacement is not None and replacement.attempt_count == 2
        recovered.append(queue.get(str(task.id)))
        recovered_events.extend(queue.events_after(str(task.id)))
        if operation == "progress":
            context.progress("stale", {"stale": True})
        elif operation == "renewal":
            lease_ticks.set()
            renewal_finished.wait()
        return {"stale": True}

    worker = Worker(
        durable_database,
        worker_id="same-worker",
        handlers={"artifact.verify": handler},
    )
    original_heartbeat = worker.queue.heartbeat

    def heartbeat(*args: Any, **kwargs: Any) -> object:
        try:
            return original_heartbeat(*args, **kwargs)
        finally:
            renewal_finished.set()

    monkeypatch.setattr(worker.queue, "heartbeat", heartbeat)
    with caplog.at_level("INFO", logger="clearscout.bootstrap.worker"):
        assert worker.run_once()

    assert queue.get(str(task.id)) == recovered[0]
    assert queue.events_after(str(task.id)) == recovered_events
    terminal = [
        record
        for record in caplog.records
        if record.msg == "task_execution" and getattr(record, "state", None) == "finished"
    ]
    assert len(terminal) == 1
    assert getattr(terminal[0], "status", None) == "ownership_lost"


def test_simultaneous_first_announcements_leave_exactly_one_live_worker(
    durable_database, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import select

    checked = Event()
    release = Event()
    original = TaskLeases._only_worker

    def pause_first(session, worker_id, now):
        original(session, worker_id, now)
        if worker_id == "first":
            checked.set()
            assert release.wait(5)

    monkeypatch.setattr(TaskLeases, "_only_worker", staticmethod(pause_first))

    attempting = Event()

    def announce(worker_id):
        if worker_id == "second":
            attempting.set()
        try:
            TaskQueueService(durable_database).renew_worker_presence(
                worker_id=worker_id, ttl_seconds=3600
            )
            return True
        except ConflictError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(announce, "first")
        assert checked.wait(5)
        second = pool.submit(announce, "second")
        # The first attempt still holds the exclusive session, so the second contends
        # for it from the moment it starts.
        assert attempting.wait(5)
        release.set()
        assert sum([first.result(), second.result()]) == 1
    with durable_database.session() as session:
        assert (
            len(
                session.scalars(
                    select(WorkerPresence).where(WorkerPresence.available_until >= utc_now())
                ).all()
            )
            == 1
        )
