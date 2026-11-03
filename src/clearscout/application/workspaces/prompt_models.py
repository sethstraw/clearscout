"""Typed prompt-library read models."""

from typing import Literal

from clearscout.shared.models import StrictModel


class PromptVariableResponse(StrictModel):
    """One placeholder a prompt uses, and what the program puts in it."""

    name: str
    handling: Literal["block", "inline", "written"]


class PromptStateResponse(StrictModel):
    """Where one prompt's current text came from and its packaged-default state."""

    key: str
    workspace_id: str
    origin: Literal["packaged", "override"]
    version: int
    content_sha256: str
    packaged_sha256: str
    base_sha256: str
    upgrade: Literal["current", "adopted", "collision"]
    variables: list[PromptVariableResponse]
    updated_at: str | None
    updated_by: str


class PromptDetailResponse(PromptStateResponse):
    """One resolved prompt beside the packaged text it may be replacing."""

    content: str
    packaged_content: str


class PromptVersionResponse(StrictModel):
    """One immutable saved version of an overridden prompt."""

    id: str
    key: str
    version: int
    content_sha256: str
    base_sha256: str
    created_at: str
    created_by: str
    origin: str
    restores_version: int | None


class PromptDiffResponse(StrictModel):
    """A unified diff between any two versions of one prompt."""

    key: str
    from_version: int
    to_version: int
    diff: str
