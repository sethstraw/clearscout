"""Read models for durable task records."""

from __future__ import annotations

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select

from clearscout.application.failures import NotFoundError, ValidationError
from clearscout.application.tasks.models import TaskResponse
from clearscout.application.tasks.records import task_response
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import BackgroundTask, Workspace


def _within_contract(task: BackgroundTask) -> TaskResponse | None:
    """Return one task, or None when storage holds a type the contract dropped."""
    try:
        return task_response(task)
    except PydanticValidationError:
        return None


class TaskQueries:
    """Read tasks without exposing their worker-only payloads."""

    def __init__(self, database: Database) -> None:
        """Use the supplied explicit transaction boundary."""
        self.database = database

    def get(self, task_id: str, workspace_id: str | None = None) -> TaskResponse:
        """Return one task owned by the optional workspace."""
        with self.database.session() as session:
            task = session.get(BackgroundTask, task_id)
            if task is None or (workspace_id is not None and task.workspace_id != workspace_id):
                raise NotFoundError(f"Task {task_id!r} does not exist.")
            response = _within_contract(task)
            if response is None:
                raise NotFoundError(f"Task {task_id!r} does not exist.")
            return response

    def list(
        self,
        workspace_id: str,
        *,
        statuses: tuple[str, ...] = (),
        limit: int = 50,
    ) -> list[TaskResponse]:
        """List one bounded page of recent workspace tasks."""
        if limit < 1 or limit > 250:
            raise ValidationError("Task list limit must be between 1 and 250.")
        with self.database.session() as session:
            workspace = session.get(Workspace, workspace_id)
            if workspace is None or workspace.archived_at is not None:
                raise NotFoundError(f"Workspace {workspace_id!r} does not exist.")
            statement = select(BackgroundTask).where(BackgroundTask.workspace_id == workspace_id)
            if statuses:
                statement = statement.where(BackgroundTask.status.in_(statuses))
            tasks = session.scalars(
                statement.order_by(BackgroundTask.created_at.desc()).limit(limit)
            ).all()
            return [response for task in tasks if (response := _within_contract(task)) is not None]
