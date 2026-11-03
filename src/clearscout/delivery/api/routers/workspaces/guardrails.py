"""Workspace anti-fabrication guardrail routes."""

from __future__ import annotations

from fastapi import APIRouter

from clearscout.application.tailoring.guardrail_preview import (
    GuardrailPreviewReport,
    preview_against_master_cv,
)
from clearscout.application.workspaces import (
    GuardrailSavedResponse,
    GuardrailStateResponse,
    GuardrailVersionResponse,
)
from clearscout.delivery.api.constants import API_PREFIX
from clearscout.delivery.api.dependencies import (
    CorrelationId,
    GuardrailServiceDep,
    MasterCvServiceDep,
)
from clearscout.delivery.api.models.workspaces import GuardrailBody

router = APIRouter()


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/guardrails",
    response_model=GuardrailStateResponse,
    tags=["workspaces"],
)
def read_guardrails(workspace_id: str, service: GuardrailServiceDep) -> GuardrailStateResponse:
    """Read the anti-fabrication rules in force for one workspace."""
    return service.describe(workspace_id)


@router.put(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/guardrails",
    response_model=GuardrailSavedResponse,
    tags=["workspaces"],
)
def write_guardrails(
    correlation_id: CorrelationId,
    workspace_id: str,
    body: GuardrailBody,
    service: GuardrailServiceDep,
) -> GuardrailSavedResponse:
    """Append one complete anti-fabrication rule version."""
    return service.save(
        workspace_id,
        body.rules,
        origin="api",
        correlation_id=correlation_id,
        expected_version=body.expected_version,
    )


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/guardrails/history",
    response_model=list[GuardrailVersionResponse],
    tags=["workspaces"],
)
def guardrail_history(
    workspace_id: str, service: GuardrailServiceDep
) -> list[GuardrailVersionResponse]:
    """List every anti-fabrication rule version, newest first."""
    return service.history(workspace_id)


@router.post(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/guardrails/preview",
    response_model=GuardrailPreviewReport,
    tags=["workspaces"],
)
def preview_guardrails(
    workspace_id: str,
    body: GuardrailBody,
    master_cv: MasterCvServiceDep,
) -> GuardrailPreviewReport:
    """Preview phrase matches and work-authorization precedence, writing nothing."""
    current = master_cv.read_version(workspace_id)
    return preview_against_master_cv(body.rules, current.content)
