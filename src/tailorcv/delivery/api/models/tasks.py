"""Durable task API contracts."""

from typing import Any

from pydantic import model_validator

from tailorcv.application.failures import ValidationError
from tailorcv.application.tasks import TaskResponse, TaskType, validate_task_payload
from tailorcv.shared.models import StrictModel


class TaskCreate(StrictModel):
    """A bounded task request accepted by the production worker registry."""

    workspace_id: str
    task_type: TaskType
    payload: dict[str, Any]

    @model_validator(mode="after")
    def registered_payload(self) -> "TaskCreate":
        """Validate task name and payload through the shared production registry."""
        try:
            self.payload = validate_task_payload(self.task_type, dict(self.payload))
        except ValidationError as exc:
            raise ValueError(str(exc)) from exc
        return self


__all__ = ["TaskCreate", "TaskResponse"]
