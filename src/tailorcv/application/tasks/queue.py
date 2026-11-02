"""Public facade for durable task queue use cases."""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Set as AbstractSet
from typing import Any

from sqlalchemy.orm import Session

from tailorcv.application.failures import UnavailableError
from tailorcv.application.tasks.commands import TaskCommands
from tailorcv.application.tasks.events import TaskEventLog
from tailorcv.application.tasks.leases import (
    DEFAULT_TASK_LEASE_SECONDS,
    ClaimScope,
    LaneClaim,
    TaskLeases,
)
from tailorcv.application.tasks.models import ClaimedTask, TaskEventResponse, TaskResponse
from tailorcv.application.tasks.queries import TaskQueries
from tailorcv.domain import TaskStatus
from tailorcv.infrastructure.persistence.database import Database


class TaskQueueService:
    """Offer the stable queue boundary while specialists own each concern."""

    def __init__(
        self,
        database: Database,
        *,
        reader_alive: Callable[[int, str], bool | None] | None = None,
    ) -> None:
        """Compose commands, leases, events, and queries over one database."""
        self.database = database
        self.events = TaskEventLog(database)
        self.commands = TaskCommands(database, self.events)
        self.leases = TaskLeases(database, self.events)
        self.queries = TaskQueries(database)
        self.reader_alive = reader_alive

    def renew_worker_presence(self, *, worker_id: str, ttl_seconds: int = 120) -> None:
        """Make one polling worker visible until its next expected heartbeat."""
        self.leases.renew_worker_presence(worker_id=worker_id, ttl_seconds=ttl_seconds)

    def withdraw_worker_presence(self, *, worker_id: str) -> None:
        """Stop advertising a worker that is shutting down normally."""
        self.leases.withdraw_worker_presence(worker_id=worker_id)

    def worker_available(self) -> bool:
        """Return whether any worker heartbeat has not expired."""
        return self.leases.worker_available()

    def require_worker(self) -> None:
        """Refuse queueing that no running worker could claim."""
        if not self.leases.worker_available():
            raise UnavailableError(
                "The worker is not running. Start it with 'tailorcv worker' "
                "and queue the work again."
            )

    def enqueue(
        self,
        *,
        workspace_id: str,
        task_type: str,
        payload: dict[str, Any],
        idempotency_key: str,
        actor: str = "local_user",
        origin: str = "api",
        correlation_id: str | None = None,
    ) -> TaskResponse:
        """Create one queued task or replay the identical existing request."""
        return self.commands.enqueue(
            workspace_id=workspace_id,
            task_type=task_type,
            payload=payload,
            idempotency_key=idempotency_key,
            actor=actor,
            origin=origin,
            correlation_id=correlation_id,
        )

    def enqueue_in_session(
        self,
        session: Session,
        *,
        workspace_id: str,
        task_type: str,
        payload: dict[str, Any],
        idempotency_key: str,
        actor: str = "local_user",
        origin: str = "api",
        correlation_id: str | None = None,
    ) -> TaskResponse:
        """Create or replay one task in the caller's transaction."""
        return self.commands.enqueue_in_session(
            session,
            workspace_id=workspace_id,
            task_type=task_type,
            payload=payload,
            idempotency_key=idempotency_key,
            actor=actor,
            origin=origin,
            correlation_id=correlation_id,
        )

    def get(self, task_id: str, workspace_id: str | None = None) -> TaskResponse:
        """Return one task without exposing arbitrary provider payloads."""
        return self.queries.get(task_id, workspace_id)

    def list_tasks(
        self,
        workspace_id: str,
        *,
        statuses: tuple[str, ...] = (),
        limit: int = 50,
    ) -> list[TaskResponse]:
        """List recent tasks for one workspace."""
        return self.queries.list(workspace_id, statuses=statuses, limit=limit)

    def request_cancellation(
        self,
        task_id: str,
        *,
        workspace_id: str | None = None,
        actor: str = "local_user",
        origin: str = "api",
        correlation_id: str | None = None,
    ) -> TaskResponse:
        """Cancel queued work or request cooperative cancellation of running work."""
        return self.commands.request_cancellation(
            task_id,
            workspace_id=workspace_id,
            actor=actor,
            origin=origin,
            correlation_id=correlation_id,
        )

    def retry(
        self,
        task_id: str,
        *,
        workspace_id: str | None = None,
        actor: str = "local_user",
        origin: str = "api",
        correlation_id: str | None = None,
    ) -> TaskResponse:
        """Queue one idempotent retry of failed or canceled work."""
        return self.commands.retry(
            task_id,
            workspace_id=workspace_id,
            actor=actor,
            origin=origin,
            correlation_id=correlation_id,
        )

    def resume_source_batch(
        self,
        workspace_id: str,
        task_id: str,
        idempotency_key: str,
        *,
        actor: str = "local_user",
        origin: str = "api",
        correlation_id: str | None = None,
    ) -> TaskResponse:
        """Create a new batch for retained unfinished source work."""
        return self.commands.resume_source_batch(
            workspace_id,
            task_id,
            idempotency_key,
            actor=actor,
            origin=origin,
            correlation_id=correlation_id,
        )

    def claim_next(
        self, *, worker_id: str, lease_seconds: int = DEFAULT_TASK_LEASE_SECONDS
    ) -> ClaimedTask | None:
        """Lease queued or abandoned work to one worker."""
        if self.reader_alive is not None:
            self.commands.recover_dead_source_readers(self.reader_alive)
        return self.leases.claim_next(worker_id=worker_id, lease_seconds=lease_seconds)

    def claim_lane(
        self,
        *,
        worker_id: str,
        lease_seconds: int = DEFAULT_TASK_LEASE_SECONDS,
        scope: ClaimScope = "any",
        busy_hosts: AbstractSet[str] = frozenset(),
    ) -> LaneClaim | None:
        """Lease the oldest task in scope, skipping continuations whose board host is busy."""
        if self.reader_alive is not None:
            self.commands.recover_dead_source_readers(self.reader_alive)
        return self.leases.claim_lane(
            worker_id=worker_id, lease_seconds=lease_seconds, scope=scope, busy_hosts=busy_hosts
        )

    def heartbeat(
        self,
        task_id: str,
        *,
        worker_id: str,
        expected_attempt: int | None = None,
        progress: dict[str, Any] | None = None,
        stage: str = "running",
        lease_seconds: int = DEFAULT_TASK_LEASE_SECONDS,
    ) -> ClaimedTask:
        """Extend a live lease and optionally append visible progress."""
        return self.leases.heartbeat(
            task_id,
            worker_id=worker_id,
            expected_attempt=expected_attempt,
            progress=progress,
            stage=stage,
            lease_seconds=lease_seconds,
        )

    def finish(
        self,
        task_id: str,
        *,
        worker_id: str,
        expected_attempt: int | None = None,
        status: TaskStatus,
        progress: dict[str, Any] | None = None,
        error_category: str | None = None,
        error_message: str | None = None,
    ) -> TaskResponse:
        """Finish owned work as completed, failed, or cooperatively canceled."""
        return self.leases.finish(
            task_id,
            worker_id=worker_id,
            expected_attempt=expected_attempt,
            status=status,
            progress=progress,
            error_category=error_category,
            error_message=error_message,
        )

    def events_after(
        self,
        task_id: str,
        sequence: int = 0,
        *,
        workspace_id: str | None = None,
        limit: int = 250,
    ) -> list[TaskEventResponse]:
        """Return ordered events after a sequence for polling or resumable SSE."""
        return self.events.after(task_id, sequence, workspace_id=workspace_id, limit=limit)
