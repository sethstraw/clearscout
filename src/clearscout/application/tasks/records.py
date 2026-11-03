"""Task response shaping and stable payload identity."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from clearscout.application.tasks.models import TaskEventResponse, TaskResponse
from clearscout.infrastructure.persistence.models import BackgroundTask, TaskEvent


def payload_digest(payload: dict[str, Any]) -> str:
    """Return the stable digest used by task idempotency checks."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def task_response(task: BackgroundTask) -> TaskResponse:
    """Shape one durable task for callers outside the queue."""
    values = task_values(task)
    return TaskResponse.model_validate(values)


def claimed_task_response(task: BackgroundTask) -> dict[str, Any]:
    """Shape one leased task with its worker-only payload."""
    return task_values(task, include_payload=True)


def _named_ids(payload: Any, single_key: str, several_key: str) -> list[str]:
    """Return the ids a task payload names under one key pair, in payload order."""
    if not isinstance(payload, dict):
        return []
    single = payload.get(single_key)
    if isinstance(single, str) and single:
        return [single]
    several = payload.get(several_key)
    if not isinstance(several, list):
        return []
    return [item for item in several if isinstance(item, str) and item]


def job_posting_ids(payload: Any) -> list[str]:
    """Return the job postings a task payload names, in the order it names them."""
    return _named_ids(payload, "job_posting_id", "job_posting_ids")


def source_ids(payload: Any) -> list[str]:
    """Return the sources a task payload names, in the order it names them."""
    # No registered payload carries a singular source_id, so only the plural key is read.
    return _named_ids(payload, "source_ids", "source_ids")


def pending_discovery_ids(payload: Any) -> list[str]:
    """Return the pending discoveries a task payload names, in the order it names them."""
    return _named_ids(payload, "pending_discovery_id", "pending_discovery_ids")


def task_pending_discovery_ids(task_type: str, payload: Any) -> list[str]:
    """Return pending targets from the task type's validated payload shape."""
    if task_type == "posting.recover_batch":
        from clearscout.application.tasks.payloads import PostingRecoverBatchPayload

        recovery = PostingRecoverBatchPayload.model_validate(payload)
        return list(recovery.approval.pending_discovery_ids)
    return pending_discovery_ids(payload)


def task_values(task: BackgroundTask, *, include_payload: bool = False) -> dict[str, Any]:
    """Collect durable task fields for a caller-specific response model."""
    result: dict[str, Any] = {
        "id": task.id,
        "workspace_id": task.workspace_id,
        "task_type": task.task_type,
        "job_posting_ids": job_posting_ids(task.payload),
        "pending_discovery_ids": task_pending_discovery_ids(task.task_type, task.payload),
        "source_ids": source_ids(task.payload),
        "source_resume_from_task_id": (
            task.payload.get("resume_from_task_id") if task.task_type == "source.scan" else None
        ),
        "status": task.status,
        "attempt_count": task.attempt_count,
        "progress": task.progress,
        "cancellation_requested_at": task.cancellation_requested_at,
        "error_category": task.error_category,
        "error_message": task.error_message,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "updated_at": task.updated_at,
        "completed_at": task.completed_at,
    }
    if include_payload:
        result["payload"] = task.payload
    return result


def event_response(event: TaskEvent) -> TaskEventResponse:
    """Shape one immutable task event."""
    return TaskEventResponse(
        id=event.id,
        task_id=event.task_id,
        sequence=event.sequence,
        stage=event.stage,
        event_type=event.event_type,
        payload=event.payload,
        created_at=event.created_at,
    )
