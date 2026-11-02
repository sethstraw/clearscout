"""Tasks HTTP routes."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from tailorcv.application.failures import ValidationError
from tailorcv.application.tasks import (
    TaskBillingResponse,
    TaskResponse,
    TaskTypeResponse,
    task_type_catalog,
)
from tailorcv.application.tasks.models import EvaluationPreflightResponse
from tailorcv.application.tasks.payloads import DiscoveryEvaluateBatchPayload
from tailorcv.delivery.api.constants import API_PREFIX
from tailorcv.delivery.api.dependencies import (
    CorrelationId,
    EvaluationWorkflowServiceDep,
    IdempotencyKey,
    SpendServiceDep,
    TaskQueueServiceDep,
    WorkspaceId,
)
from tailorcv.delivery.api.models.tasks import TaskCreate
from tailorcv.domain import TASK_STATUSES, TaskStatus

router = APIRouter()

# One page of task history per read; the stream resumes from the last sequence.
EVENT_PAGE = 250


@router.post(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/evaluation-preflight",
    response_model=EvaluationPreflightResponse,
    tags=["tasks"],
)
def evaluation_preflight(
    workspace_id: str,
    body: DiscoveryEvaluateBatchPayload,
    service: EvaluationWorkflowServiceDep,
) -> EvaluationPreflightResponse:
    """Estimate one or several evaluations without reading publishers or queuing work."""
    return service.preflight(workspace_id, body)


@router.get(
    f"{API_PREFIX}/tasks",
    response_model=list[TaskResponse],
    tags=["tasks"],
)
def list_tasks(
    service: TaskQueueServiceDep,
    workspace_id: WorkspaceId,
    status: Annotated[list[str] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=250)] = 50,
) -> list[TaskResponse]:
    """List durable tasks for a workspace."""
    statuses = tuple(status or ())
    if any(item not in TASK_STATUSES for item in statuses):
        raise ValidationError("Task status filter contains an unsupported value.")
    return service.list_tasks(workspace_id, statuses=statuses, limit=limit)


@router.post(
    f"{API_PREFIX}/tasks",
    response_model=TaskResponse,
    status_code=202,
    tags=["tasks"],
)
def enqueue_task(
    correlation_id: CorrelationId,
    body: TaskCreate,
    service: TaskQueueServiceDep,
    idempotency_key: IdempotencyKey,
) -> TaskResponse:
    """Enqueue an idempotent registered task."""
    service.require_worker()
    return service.enqueue(
        workspace_id=body.workspace_id,
        task_type=body.task_type,
        payload=body.payload,
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
    )


# Declared before /tasks/{task_id} so the literal segment wins the match.
@router.get(
    f"{API_PREFIX}/tasks/types",
    response_model=list[TaskTypeResponse],
    tags=["tasks"],
)
def list_task_types() -> list[TaskTypeResponse]:
    """List every registered task type and the charge running it may incur."""
    return list(task_type_catalog())


@router.get(
    f"{API_PREFIX}/tasks/{{task_id}}",
    response_model=TaskResponse,
    tags=["tasks"],
)
def task_detail(
    task_id: str,
    workspace_id: WorkspaceId,
    service: TaskQueueServiceDep,
) -> TaskResponse:
    """Read one durable task."""
    return service.get(task_id, workspace_id)


@router.get(
    f"{API_PREFIX}/tasks/{{task_id}}/billing",
    response_model=TaskBillingResponse,
    tags=["tasks"],
)
def task_billing(
    task_id: str,
    workspace_id: WorkspaceId,
    service: SpendServiceDep,
) -> TaskBillingResponse:
    """Read recorded model-call billing for one durable task."""
    return service.task_billing(workspace_id, task_id)


@router.post(
    f"{API_PREFIX}/tasks/{{task_id}}/cancel",
    response_model=TaskResponse,
    tags=["tasks"],
)
def cancel_task(
    correlation_id: CorrelationId,
    task_id: str,
    workspace_id: WorkspaceId,
    service: TaskQueueServiceDep,
) -> TaskResponse:
    """Request cancellation of a durable task."""
    return service.request_cancellation(
        task_id,
        workspace_id=workspace_id,
        correlation_id=correlation_id,
    )


@router.post(
    f"{API_PREFIX}/tasks/{{task_id}}/resume",
    response_model=TaskResponse,
    status_code=202,
    tags=["tasks"],
)
def resume_source_batch(
    correlation_id: CorrelationId,
    task_id: str,
    workspace_id: WorkspaceId,
    idempotency_key: IdempotencyKey,
    service: TaskQueueServiceDep,
) -> TaskResponse:
    """Resume unfinished source work as a new batch, preserving collected results."""
    service.require_worker()
    return service.resume_source_batch(
        workspace_id,
        task_id,
        idempotency_key,
        correlation_id=correlation_id,
    )


@router.post(
    f"{API_PREFIX}/tasks/{{task_id}}/retry",
    response_model=TaskResponse,
    status_code=202,
    tags=["tasks"],
)
def retry_task(
    correlation_id: CorrelationId,
    task_id: str,
    workspace_id: WorkspaceId,
    service: TaskQueueServiceDep,
) -> TaskResponse:
    """Requeue a failed or cancelled task."""
    service.require_worker()
    return service.retry(
        task_id,
        workspace_id=workspace_id,
        correlation_id=correlation_id,
    )


@router.get(
    f"{API_PREFIX}/tasks/{{task_id}}/events",
    response_class=StreamingResponse,
    tags=["tasks"],
)
async def task_events(
    request: Request,
    task_id: str,
    workspace_id: WorkspaceId,
    service: TaskQueueServiceDep,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Stream durable task events until the task stops."""
    await run_in_threadpool(service.get, task_id, workspace_id)
    sequence = event_sequence(last_event_id)

    async def stream() -> AsyncIterator[str]:
        nonlocal sequence
        while not await request.is_disconnected():
            events = await run_in_threadpool(
                partial(
                    service.events_after,
                    task_id,
                    sequence,
                    workspace_id=workspace_id,
                    limit=EVENT_PAGE,
                )
            )
            for event in events:
                sequence = event.sequence
                payload = json.dumps(
                    event.model_dump(mode="json"), separators=(",", ":"), default=str
                )
                yield f"id: {sequence}\nevent: {event.event_type}\ndata: {payload}\n\n"
            # A full page means more is waiting, and a finished task would
            # otherwise end the stream with its backlog undelivered.
            if len(events) == EVENT_PAGE:
                continue
            state = await run_in_threadpool(service.get, task_id, workspace_id)
            if state.status in {
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }:
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},
    )


def event_sequence(value: str | None) -> int:
    """Parse a non-negative SSE event cursor."""
    if value is None or not value.strip():
        return 0
    try:
        sequence = int(value)
    except ValueError as error:
        raise ValidationError("Last-Event-ID must be a non-negative integer.") from error
    if sequence < 0:
        raise ValidationError("Last-Event-ID must be a non-negative integer.")
    return sequence
