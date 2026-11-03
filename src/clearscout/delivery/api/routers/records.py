"""Records HTTP routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import Response

from clearscout.application.workspaces.records.models import (
    ApplicationOptionResponse,
    NoteResponse,
    SavedViewResponse,
    TagAssignmentResponse,
    TagResponse,
)
from clearscout.delivery.api.constants import API_PREFIX
from clearscout.delivery.api.dependencies import (
    CorrelationId,
    RecordsServiceDep,
    WorkspaceId,
)
from clearscout.delivery.api.models.records import (
    NoteBody,
    NoteUpdate,
    SavedViewBody,
    TagAssignmentBody,
    TagBody,
)

router = APIRouter()


@router.get(
    f"{API_PREFIX}/applications/options",
    response_model=list[ApplicationOptionResponse],
    tags=["workspace"],
)
def application_options(
    workspace_id: WorkspaceId,
    service: RecordsServiceDep,
) -> list[ApplicationOptionResponse]:
    """List applications available to attachment forms."""
    return service.applications.list_applications(workspace_id)


@router.get(f"{API_PREFIX}/notes", response_model=list[NoteResponse], tags=["workspace"])
def list_notes(
    workspace_id: WorkspaceId,
    service: RecordsServiceDep,
    search: str = "",
    entity_type: str | None = None,
    entity_id: str | None = None,
) -> list[NoteResponse]:
    """List active notes for a workspace."""
    return service.notes.list_notes(
        workspace_id, search=search, entity_type=entity_type, entity_id=entity_id
    )


@router.post(
    f"{API_PREFIX}/notes", response_model=NoteResponse, status_code=201, tags=["workspace"]
)
def create_note(
    correlation_id: CorrelationId,
    body: NoteBody,
    service: RecordsServiceDep,
) -> NoteResponse:
    """Create a non-evidence workspace note."""
    return service.notes.create_note(
        workspace_id=body.workspace_id,
        entity_type=body.entity_type,
        entity_id=body.entity_id,
        title=body.title,
        body=body.body,
        pinned=body.pinned,
        correlation_id=correlation_id,
    )


@router.put(f"{API_PREFIX}/notes/{{note_id}}", response_model=NoteResponse, tags=["workspace"])
def update_note(
    correlation_id: CorrelationId,
    note_id: str,
    body: NoteUpdate,
    service: RecordsServiceDep,
) -> NoteResponse:
    """Update a workspace note."""
    return service.notes.update_note(
        note_id,
        body.workspace_id,
        title=body.title,
        body=body.body,
        pinned=body.pinned,
        correlation_id=correlation_id,
    )


@router.delete(f"{API_PREFIX}/notes/{{note_id}}", response_model=NoteResponse, tags=["workspace"])
def archive_note(
    correlation_id: CorrelationId,
    note_id: str,
    workspace_id: WorkspaceId,
    service: RecordsServiceDep,
) -> NoteResponse:
    """Archive a workspace note."""
    return service.notes.archive_note(note_id, workspace_id, correlation_id=correlation_id)


@router.get(f"{API_PREFIX}/tags", response_model=list[TagResponse], tags=["workspace"])
def list_tags(
    workspace_id: WorkspaceId,
    service: RecordsServiceDep,
) -> list[TagResponse]:
    """List workspace tags and active assignments."""
    return service.tags.list_tags(workspace_id)


@router.post(f"{API_PREFIX}/tags", response_model=TagResponse, status_code=201, tags=["workspace"])
def create_tag(
    correlation_id: CorrelationId,
    body: TagBody,
    service: RecordsServiceDep,
) -> TagResponse:
    """Create a workspace tag."""
    return service.tags.create_tag(
        workspace_id=body.workspace_id,
        name=body.name,
        color=body.color,
        correlation_id=correlation_id,
    )


@router.put(f"{API_PREFIX}/tags/{{tag_id}}", response_model=TagResponse, tags=["workspace"])
def update_tag(
    correlation_id: CorrelationId,
    tag_id: str,
    body: TagBody,
    service: RecordsServiceDep,
) -> TagResponse:
    """Update a workspace tag."""
    return service.tags.update_tag(
        tag_id,
        body.workspace_id,
        name=body.name,
        color=body.color,
        correlation_id=correlation_id,
    )


@router.delete(f"{API_PREFIX}/tags/{{tag_id}}", response_model=TagResponse, tags=["workspace"])
def archive_tag(
    correlation_id: CorrelationId,
    tag_id: str,
    workspace_id: WorkspaceId,
    service: RecordsServiceDep,
) -> TagResponse:
    """Archive a workspace tag."""
    return service.tags.archive_tag(tag_id, workspace_id, correlation_id=correlation_id)


@router.post(
    f"{API_PREFIX}/tags/{{tag_id}}/assignments",
    response_model=TagAssignmentResponse,
    status_code=201,
    tags=["workspace"],
)
def assign_tag(
    correlation_id: CorrelationId,
    tag_id: str,
    body: TagAssignmentBody,
    service: RecordsServiceDep,
) -> TagAssignmentResponse:
    """Assign a tag to a workspace record."""
    return service.tags.assign_tag(
        tag_id=tag_id,
        workspace_id=body.workspace_id,
        entity_type=body.entity_type,
        entity_id=body.entity_id,
        correlation_id=correlation_id,
    )


@router.delete(
    f"{API_PREFIX}/tags/{{tag_id}}/assignments",
    response_class=Response,
    status_code=204,
    tags=["workspace"],
)
def unassign_tag(
    correlation_id: CorrelationId,
    tag_id: str,
    workspace_id: WorkspaceId,
    entity_type: Annotated[str, Query(min_length=1)],
    entity_id: Annotated[str, Query(min_length=1)],
    service: RecordsServiceDep,
) -> Response:
    """Remove a tag assignment from a workspace record."""
    service.tags.unassign_tag(
        tag_id=tag_id,
        workspace_id=workspace_id,
        entity_type=entity_type,
        entity_id=entity_id,
        correlation_id=correlation_id,
    )
    return Response(status_code=204)


@router.get(
    f"{API_PREFIX}/saved-views",
    response_model=list[SavedViewResponse],
    tags=["workspace"],
)
def list_saved_views(
    workspace_id: WorkspaceId,
    service: RecordsServiceDep,
    resource: str | None = None,
) -> list[SavedViewResponse]:
    """List saved job views for a workspace."""
    return service.saved_views.list_saved_views(workspace_id, resource=resource)


@router.post(
    f"{API_PREFIX}/saved-views",
    response_model=SavedViewResponse,
    status_code=201,
    tags=["workspace"],
)
def create_saved_view(
    correlation_id: CorrelationId,
    body: SavedViewBody,
    service: RecordsServiceDep,
) -> SavedViewResponse:
    """Create a saved job view."""
    return service.saved_views.save_view(
        workspace_id=body.workspace_id,
        view_id=None,
        resource=body.resource,
        name=body.name,
        configuration=body.configuration.model_dump(),
        correlation_id=correlation_id,
    )


@router.put(
    f"{API_PREFIX}/saved-views/{{view_id}}",
    response_model=SavedViewResponse,
    tags=["workspace"],
)
def update_saved_view(
    correlation_id: CorrelationId,
    view_id: str,
    body: SavedViewBody,
    service: RecordsServiceDep,
) -> SavedViewResponse:
    """Update a saved job view."""
    return service.saved_views.save_view(
        workspace_id=body.workspace_id,
        view_id=view_id,
        resource=body.resource,
        name=body.name,
        configuration=body.configuration.model_dump(),
        correlation_id=correlation_id,
    )


@router.delete(
    f"{API_PREFIX}/saved-views/{{view_id}}",
    response_model=SavedViewResponse,
    tags=["workspace"],
)
def archive_saved_view(
    correlation_id: CorrelationId,
    view_id: str,
    workspace_id: WorkspaceId,
    service: RecordsServiceDep,
) -> SavedViewResponse:
    """Archive a saved job view."""
    return service.saved_views.archive_saved_view(
        view_id, workspace_id, correlation_id=correlation_id
    )
