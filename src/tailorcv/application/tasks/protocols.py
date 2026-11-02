"""Application contracts for durable task execution."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from tailorcv.application.tasks.queue import TaskQueueService


@runtime_checkable
class TaskContext(Protocol):
    """Progress and cancellation boundary supplied to task handlers."""

    queue: TaskQueueService
    task_id: str
    worker_id: str

    def progress(self, stage: str, payload: dict[str, Any]) -> None:
        """Persist visible progress and extend the task lease."""
        ...

    def cancellation_requested(self) -> bool:
        """Return whether cooperative cancellation was requested."""
        ...

    def raise_if_cancelled(self) -> None:
        """Stop at a handler-defined cancellation point."""
        ...


class TaskHandler(Protocol):
    """Execute one registered durable task type."""

    def __call__(self, context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Execute one claimed payload and return final progress metadata."""
        ...
