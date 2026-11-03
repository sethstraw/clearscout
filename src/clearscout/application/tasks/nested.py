"""Task contexts for a call nested inside another handler's task lease."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clearscout.application.tasks.protocols import TaskContext


class _NestedTaskContext:
    """Delegate the task identity and cancellation to the outer handler's context."""

    def __init__(self, inner: TaskContext) -> None:
        self._inner = inner
        self.queue = inner.queue
        self.task_id = inner.task_id
        self.worker_id = inner.worker_id

    def progress(self, stage: str, payload: dict[str, Any]) -> None:
        """Report nested progress through the outer context."""
        self._inner.progress(stage, payload)

    def cancellation_requested(self) -> bool:
        """Return whether the outer task was asked to cancel."""
        return self._inner.cancellation_requested()

    def raise_if_cancelled(self) -> None:
        """Stop at the outer task's cancellation point."""
        self._inner.raise_if_cancelled()


class ShieldedTaskContext(_NestedTaskContext):
    """Keep a nested call's progress out of an outer task whose payload has another shape."""

    def progress(self, stage: str, payload: dict[str, Any]) -> None:
        """Leave progress to the outer task and its lease keeper."""


class BatchStepContext(_NestedTaskContext):
    """Carry one batch item's step and the batch's tallies into every progress event."""

    def __init__(
        self, inner: TaskContext, *, current: int, total: int, so_far: Mapping[str, int]
    ) -> None:
        """Wrap the batch's context for item ``current`` of ``total``, after ``so_far``."""
        super().__init__(inner)
        self._step = {"current": current, "total": total, "so_far": dict(so_far)}

    def progress(self, stage: str, payload: dict[str, Any]) -> None:
        """Report the stage and its payload with the batch step and tallies added."""
        # Stored progress is replaced per event, so the step rides on each one. The
        # tallies sit under one key because item payloads use names such as "skipped".
        super().progress(stage, {**payload, **self._step})
