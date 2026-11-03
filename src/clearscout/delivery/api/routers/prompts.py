"""Prompts HTTP routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from clearscout.application.workspaces.prompt_models import (
    PromptDetailResponse,
    PromptDiffResponse,
    PromptStateResponse,
    PromptVersionResponse,
)
from clearscout.delivery.api.constants import API_PREFIX
from clearscout.delivery.api.dependencies import (
    CorrelationId,
    PromptLibraryServiceDep,
    WorkspaceId,
)
from clearscout.delivery.api.models.prompts import (
    PromptReset,
    PromptRevert,
    PromptSave,
)

router = APIRouter()


@router.get(
    f"{API_PREFIX}/prompts",
    response_model=list[PromptStateResponse],
    tags=["prompts"],
)
def list_prompts(
    workspace_id: WorkspaceId,
    service: PromptLibraryServiceDep,
) -> list[PromptStateResponse]:
    """Report every prompt in the suite and whether it is packaged or overridden."""
    return service.list_prompts(workspace_id)


@router.get(
    f"{API_PREFIX}/prompts/{{key}}",
    response_model=PromptDetailResponse,
    tags=["prompts"],
)
def read_prompt(
    key: str,
    workspace_id: WorkspaceId,
    service: PromptLibraryServiceDep,
) -> PromptDetailResponse:
    """Read one resolved prompt and its packaged default."""
    return service.read(workspace_id, key)


@router.put(
    f"{API_PREFIX}/prompts/{{key}}",
    response_model=PromptDetailResponse,
    tags=["prompts"],
)
def write_prompt(
    correlation_id: CorrelationId,
    key: str,
    body: PromptSave,
    service: PromptLibraryServiceDep,
) -> PromptDetailResponse:
    """Save a complete prompt override."""
    return service.save(
        body.workspace_id,
        key,
        body.content,
        expected_version=body.expected_version,
        origin="api",
        correlation_id=correlation_id,
    )


@router.get(
    f"{API_PREFIX}/prompts/{{key}}/versions",
    response_model=list[PromptVersionResponse],
    tags=["prompts"],
)
def prompt_versions(
    key: str,
    workspace_id: WorkspaceId,
    service: PromptLibraryServiceDep,
) -> list[PromptVersionResponse]:
    """List immutable versions of a prompt override."""
    return service.versions(workspace_id, key)


@router.get(
    f"{API_PREFIX}/prompts/{{key}}/diff",
    response_model=PromptDiffResponse,
    tags=["prompts"],
)
def prompt_diff(
    key: str,
    workspace_id: WorkspaceId,
    from_version: Annotated[int, Query(ge=0)],
    to_version: Annotated[int, Query(ge=0)],
    service: PromptLibraryServiceDep,
) -> PromptDiffResponse:
    """Compare any two versions, where zero on either side is the packaged file."""
    return service.diff(workspace_id, key, from_version=from_version, to_version=to_version)


@router.post(
    f"{API_PREFIX}/prompts/{{key}}/revert",
    response_model=PromptDetailResponse,
    tags=["prompts"],
)
def revert_prompt(
    correlation_id: CorrelationId,
    key: str,
    body: PromptRevert,
    service: PromptLibraryServiceDep,
) -> PromptDetailResponse:
    """Append the named version's text as a new version rather than moving a pointer."""
    return service.revert(
        body.workspace_id,
        key,
        body.version,
        origin="api",
        correlation_id=correlation_id,
    )


@router.post(
    f"{API_PREFIX}/prompts/{{key}}/reset",
    response_model=PromptDetailResponse,
    tags=["prompts"],
)
def reset_prompt(
    key: str,
    body: PromptReset,
    service: PromptLibraryServiceDep,
) -> PromptDetailResponse:
    """Archive the override so the packaged prompt answers again."""
    return service.reset(body.workspace_id, key)
