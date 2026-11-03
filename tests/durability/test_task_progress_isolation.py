"""What a nested call's progress does to the outer task it runs inside.

A source scan shields its nested posting refreshes, whose payload describes a
different job's work. A batch item's nested call keeps reporting, and carries
the batch step and tallies so the stored progress still says where the batch is.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from clearscout.application.tasks import (
    BatchStepContext,
    ShieldedTaskContext,
    TaskContext,
    TaskQueueService,
)
from clearscout.application.tasks.models import ClaimedTask
from clearscout.bootstrap.worker import (
    LeaseRenewalError,
    TaskCancelledError,
    _LeaseKeeper,
    _WorkerTaskContext,
)
from clearscout.infrastructure.persistence.database import Database
from tests.support.scenarios import scan_workspace as _workspace
from tests.support.scenarios import state_target_titles


def _claimed(
    database: Database, key: str
) -> tuple[TaskQueueService, ClaimedTask, _WorkerTaskContext]:
    """Lease one task to a worker and return the context its handler would receive."""
    workspace_id = _workspace(database)
    state_target_titles(database, workspace_id, "Data Platform Lead")
    queue = TaskQueueService(database)
    queue.renew_worker_presence(worker_id="w1", ttl_seconds=3600)
    enqueued = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={},
        idempotency_key=key,
    )
    claimed = queue.claim_next(worker_id="w1")
    assert claimed is not None and claimed.id == enqueued.id
    keeper = _LeaseKeeper(queue, enqueued.id, "w1", 20.0)
    return queue, claimed, _WorkerTaskContext(queue, enqueued.id, "w1", keeper)


def test_nested_posting_refresh_within_source_scan_leaves_scan_progress_empty(
    durable_database: Database,
) -> None:
    queue, claimed, outer = _claimed(durable_database, "scan-1")
    nested = ShieldedTaskContext(outer)
    before_events = queue.events_after(claimed.id, sequence=0, limit=100)

    nested.progress("fetching", {"job_posting_id": "abc", "url": "https://example.com/job"})
    nested.progress(
        "persisting",
        {"job_posting_id": "abc", "content_unchanged": True, "model_calls_made": 0},
    )

    reloaded = queue.get(claimed.id)
    assert reloaded.progress == {}
    assert reloaded.updated_at == claimed.updated_at
    assert queue.events_after(claimed.id, sequence=0, limit=100) == before_events


def test_a_batch_items_nested_stage_is_stored_with_the_batch_step_and_tallies(
    durable_database: Database,
) -> None:
    queue, claimed, outer = _claimed(durable_database, "batch-step-1")
    nested = BatchStepContext(outer, current=3, total=7, so_far={"evaluated": 1, "skipped": 1})

    nested.progress("dismissed_skipped", {"pending_discovery_id": "abc", "skipped": "dismissed"})

    expected = {
        "pending_discovery_id": "abc",
        "skipped": "dismissed",
        "current": 3,
        "total": 7,
        "so_far": {"evaluated": 1, "skipped": 1},
    }
    assert queue.get(claimed.id).progress == expected
    last = queue.events_after(claimed.id, sequence=0, limit=100)[-1]
    assert (last.stage, last.payload) == ("dismissed_skipped", expected)


def test_progress_after_a_renewal_failure_of_any_type_stops_the_handler_unrecorded(
    durable_database: Database,
) -> None:
    queue, claimed, context = _claimed(durable_database, "renewal-failure")
    # The renewal thread records whatever its heartbeat raised.
    context.lease_keeper.failure = ValueError("renewal heartbeat failed")

    with pytest.raises(LeaseRenewalError) as raised:
        context.progress("reading", {"completed": 1})

    assert isinstance(raised.value.__cause__, ValueError)
    assert queue.get(claimed.id).progress == {}


@pytest.mark.parametrize(
    "wrap",
    [
        ShieldedTaskContext,
        lambda outer: BatchStepContext(outer, current=1, total=2, so_far={}),
    ],
    ids=["shield", "batch-step"],
)
def test_a_nested_call_stops_at_the_outer_tasks_cancellation(
    durable_database: Database, wrap: Callable[[TaskContext], TaskContext]
) -> None:
    queue, claimed, outer = _claimed(durable_database, "nested-cancel")
    nested = wrap(outer)

    assert nested.task_id == claimed.id
    assert nested.cancellation_requested() is False
    nested.raise_if_cancelled()
    queue.request_cancellation(claimed.id)
    assert nested.cancellation_requested() is True
    with pytest.raises(TaskCancelledError):
        nested.raise_if_cancelled()
