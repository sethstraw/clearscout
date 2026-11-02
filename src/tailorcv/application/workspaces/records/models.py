"""Typed workspace-record read models and closed vocabularies."""

from datetime import datetime
from typing import Any, Literal

from tailorcv.shared.models import StrictModel

NoteEntityType = Literal["workspace", "job_posting", "application", "evaluation"]
TagEntityType = Literal["job_posting", "application"]
SavedViewResource = Literal["jobs"]


class SavedViewConfiguration(StrictModel):
    """Validated filters and sort persisted by a saved view."""

    filters: dict[str, Any] | list[Any]
    sort: dict[str, Any] | list[Any]


class ApplicationOptionResponse(StrictModel):
    """Active application choice for document attachment forms."""

    id: str
    job_posting_id: str
    title: str


class NoteResponse(StrictModel):
    """User-authored note, explicitly excluded from evidence."""

    id: str
    workspace_id: str
    entity_type: NoteEntityType
    entity_id: str | None
    title: str
    body: str
    pinned: bool
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None
    evidence_eligible: Literal[False]


class TagAssignmentResponse(StrictModel):
    """One active tag-to-record relationship."""

    id: str
    tag_id: str
    tag_name: str
    color: str
    entity_type: TagEntityType
    entity_id: str


class TagResponse(StrictModel):
    """Workspace tag with active assignments projected once."""

    id: str
    workspace_id: str
    name: str
    color: str
    created_at: datetime
    assignments: list[TagAssignmentResponse]


class SavedViewResponse(StrictModel):
    """Persisted local table configuration."""

    id: str
    workspace_id: str
    resource: SavedViewResource
    name: str
    configuration: SavedViewConfiguration
    created_at: datetime
    archived_at: datetime | None
