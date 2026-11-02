"""Workspace record API contracts."""

from pydantic import Field

from tailorcv.application.workspaces.records.models import (
    ApplicationOptionResponse,
    NoteEntityType,
    NoteResponse,
    SavedViewConfiguration,
    SavedViewResource,
    SavedViewResponse,
    TagAssignmentResponse,
    TagEntityType,
    TagResponse,
)
from tailorcv.shared.models import StrictModel


class NoteBody(StrictModel):
    """User-authored Markdown note; never evidence."""

    workspace_id: str = Field(min_length=1)
    entity_type: NoteEntityType
    entity_id: str | None = None
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1)
    pinned: bool = False


class NoteUpdate(StrictModel):
    """Editable fields of an existing note."""

    workspace_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1)
    pinned: bool = False


class TagBody(StrictModel):
    """Colored workspace tag."""

    workspace_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=120)
    color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")


class TagAssignmentBody(StrictModel):
    """Tag relationship to a job or application."""

    workspace_id: str = Field(min_length=1)
    entity_type: TagEntityType
    entity_id: str = Field(min_length=1)


class SavedViewBody(StrictModel):
    """Persisted table filters and sort configuration."""

    workspace_id: str = Field(min_length=1)
    resource: SavedViewResource
    name: str = Field(min_length=1, max_length=240)
    configuration: SavedViewConfiguration


__all__ = [
    "ApplicationOptionResponse",
    "NoteBody",
    "NoteResponse",
    "NoteUpdate",
    "SavedViewBody",
    "SavedViewConfiguration",
    "SavedViewResponse",
    "TagAssignmentBody",
    "TagAssignmentResponse",
    "TagBody",
    "TagResponse",
]
