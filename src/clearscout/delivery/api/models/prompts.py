"""Prompt-library API contracts."""

from pydantic import Field

from clearscout.application.workspaces.prompt_models import (
    PromptDetailResponse,
    PromptDiffResponse,
    PromptStateResponse,
    PromptVariableResponse,
    PromptVersionResponse,
)
from clearscout.shared.models import StrictModel


class PromptSave(StrictModel):
    """Replacement text for one prompt, whole rather than merged."""

    workspace_id: str = Field(min_length=1)
    content: str = Field(min_length=1, max_length=200_000)
    expected_version: int | None = Field(default=None, ge=0)


class PromptRevert(StrictModel):
    """The earlier version whose text is to be appended as a new one."""

    workspace_id: str = Field(min_length=1)
    version: int = Field(ge=1)


class PromptReset(StrictModel):
    """A request to let the packaged prompt answer again."""

    workspace_id: str = Field(min_length=1)


__all__ = [
    "PromptDetailResponse",
    "PromptDiffResponse",
    "PromptReset",
    "PromptRevert",
    "PromptSave",
    "PromptStateResponse",
    "PromptVariableResponse",
    "PromptVersionResponse",
]
