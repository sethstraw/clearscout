"""Append and read immutable task event histories."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from clearscout.application.failures import ConflictError, NotFoundError, ValidationError
from clearscout.application.tasks.models import TaskEventResponse
from clearscout.application.tasks.records import event_response
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import BackgroundTask, TaskEvent

_SEQUENCE_ATTEMPTS = 3


class TaskEventLog:
    """Append and page one task's durable event history."""

    def __init__(self, database: Database) -> None:
        """Use the supplied explicit transaction boundary."""
        self.database = database

    def append(
        self,
        session: Session,
        task: BackgroundTask,
        *,
        stage: str,
        event_type: str,
        payload: dict[str, Any],
        actor: str,
        origin: str,
        correlation_id: str | None,
    ) -> TaskEvent:
        """Append one event without losing a competing writer's sequence."""
        for _ in range(_SEQUENCE_ATTEMPTS):
            event = TaskEvent(
                task_id=task.id,
                sequence=self._next_sequence(session, task.id),
                stage=stage,
                event_type=event_type,
                payload=payload,
                created_by=actor,
                origin=origin,
                correlation_id=correlation_id,
            )
            try:
                with session.begin_nested():
                    session.add(event)
            except IntegrityError:
                continue
            return event
        raise ConflictError(f"Task {task.id!r} is receiving events faster than they can be filed.")

    def after(
        self,
        task_id: str,
        sequence: int = 0,
        *,
        workspace_id: str | None = None,
        limit: int = 250,
    ) -> list[TaskEventResponse]:
        """Return one bounded page of events after a known sequence."""
        if sequence < 0:
            raise ValidationError("Task event sequence cannot be negative.")
        if limit < 1 or limit > 500:
            raise ValidationError("Task event limit must be between 1 and 500.")
        with self.database.session() as session:
            task = session.get(BackgroundTask, task_id)
            if task is None or (workspace_id is not None and task.workspace_id != workspace_id):
                raise NotFoundError(f"Task {task_id!r} does not exist.")
            events = session.scalars(
                select(TaskEvent)
                .where(TaskEvent.task_id == task_id, TaskEvent.sequence > sequence)
                .order_by(TaskEvent.sequence)
                .limit(limit)
            ).all()
            return [event_response(event) for event in events]

    @staticmethod
    def _next_sequence(session: Session, task_id: str) -> int:
        """Return the position one more event would take in a task's history."""
        highest = session.scalar(
            select(func.coalesce(func.max(TaskEvent.sequence), 0)).where(
                TaskEvent.task_id == task_id
            )
        )
        return int(highest or 0) + 1
