"""Master-CV version and impact routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request
from starlette.concurrency import run_in_threadpool

from tailorcv.application import job_search
from tailorcv.application.job_search.models import RequirementGapSummaryResponse
from tailorcv.application.master_cv import MAX_CV_BYTES
from tailorcv.application.master_cv.proposal_aggregation import (
    AggregationReport,
    refresh_master_cv_proposals,
)
from tailorcv.application.master_cv.proposal_models import MasterCvProposalResponse
from tailorcv.application.workspaces import (
    MasterCvContentResponse,
    MasterCvDiffResponse,
    MasterCvEditResponse,
    MasterCvImpactResponse,
    MasterCvImportResponse,
    MasterCvRereadEstimateResponse,
    MasterCvVersionResponse,
)
from tailorcv.delivery.api.constants import API_PREFIX
from tailorcv.delivery.api.dependencies import (
    CorrelationId,
    DatabaseDep,
    MasterCvProposalServiceDep,
    MasterCvServiceDep,
    bounded_body,
)
from tailorcv.delivery.api.models.base import FREE_MODE_RESPONSES
from tailorcv.delivery.api.models.workspaces import MasterCvEdit

router = APIRouter()


@router.post(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/master-cv/import",
    response_model=MasterCvImportResponse,
    status_code=201,
    responses=FREE_MODE_RESPONSES,
    tags=["workspaces"],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        }
    },
)
async def import_master_cv(
    correlation_id: CorrelationId,
    workspace_id: str,
    request: Request,
    service: MasterCvServiceDep,
    filename: Annotated[str, Query(min_length=1, max_length=240)],
    replace: bool = False,
    confirm_reread: bool = False,
) -> MasterCvImportResponse:
    """Upload, standardize when confirmed, and version one master CV."""
    content = await bounded_body(request, MAX_CV_BYTES)
    return await run_in_threadpool(
        service.import_source,
        workspace_id=workspace_id,
        filename=filename,
        content=content,
        mime_type=request.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0],
        replace=replace,
        confirm_reread=confirm_reread,
        correlation_id=correlation_id,
    )


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/master-cv",
    response_model=MasterCvContentResponse,
    tags=["workspaces"],
    summary="Read the stored master CV",
)
def read_master_cv(
    workspace_id: str, service: MasterCvServiceDep, version_id: str = ""
) -> MasterCvContentResponse:
    """Read one stored master-CV version."""
    return service.read_version(workspace_id, version_id)


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/master-cv/versions",
    response_model=list[MasterCvVersionResponse],
    tags=["workspaces"],
    summary="List stored master CV versions",
)
def master_cv_versions(
    workspace_id: str, service: MasterCvServiceDep
) -> list[MasterCvVersionResponse]:
    """List stored master-CV versions."""
    return service.versions(workspace_id)


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/master-cv/requirement-gaps",
    response_model=RequirementGapSummaryResponse,
    tags=["workspaces"],
    summary="List requirements no evaluation could answer from the master CV",
)
def master_cv_requirement_gaps(
    workspace_id: str,
    database: DatabaseDep,
    min_raised: Annotated[int, Query(ge=1, le=1000)] = 1,
    importance: Annotated[list[str] | None, Query()] = None,
) -> RequirementGapSummaryResponse:
    """Group unanswered requirements across every posting's current evaluation."""
    return job_search.requirement_gaps(
        database, workspace_id, min_raised=min_raised, importance=importance
    )


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/master-cv/diff",
    response_model=MasterCvDiffResponse,
    tags=["workspaces"],
    summary="Compare two stored master CV versions",
)
def master_cv_diff(
    workspace_id: str,
    from_version: Annotated[str, Query(min_length=1)],
    to_version: Annotated[str, Query(min_length=1)],
    service: MasterCvServiceDep,
) -> MasterCvDiffResponse:
    """Compare two stored master-CV versions."""
    return MasterCvDiffResponse(
        from_version=from_version,
        to_version=to_version,
        diff=service.diff_versions(workspace_id, from_version, to_version),
    )


@router.post(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/master-cv/impact",
    response_model=MasterCvImpactResponse,
    tags=["workspaces"],
    summary="Report which committed citations an edit would contradict",
)
def master_cv_impact(
    workspace_id: str, body: MasterCvEdit, service: MasterCvServiceDep
) -> MasterCvImpactResponse:
    """Preview citation drift from a proposed master-CV edit."""
    return service.impact(workspace_id, body.content)


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/master-cv/reread-estimate",
    response_model=MasterCvRereadEstimateResponse,
    tags=["workspaces"],
    summary="Report what past model re-reads of a CV cost",
)
def master_cv_reread_estimate(
    workspace_id: str, service: MasterCvServiceDep
) -> MasterCvRereadEstimateResponse:
    """Average the recorded cost of past model re-reads of a CV."""
    return service.reread_estimate(workspace_id)


@router.post(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/master-cv",
    response_model=MasterCvEditResponse,
    status_code=201,
    responses=FREE_MODE_RESPONSES,
    tags=["workspaces"],
    summary="Save an edited master CV as a new version",
)
def save_master_cv(
    correlation_id: CorrelationId,
    workspace_id: str,
    body: MasterCvEdit,
    service: MasterCvServiceDep,
) -> MasterCvEditResponse:
    """Save a validated master-CV edit as a new version."""
    return service.save_edit(
        workspace_id,
        body.content,
        confirm_reread=body.confirm_reread,
        correlation_id=correlation_id,
    )


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/master-cv-proposals",
    response_model=list[MasterCvProposalResponse],
    tags=["workspaces"],
    summary="List aggregated master-CV proposals for this workspace",
)
def list_master_cv_proposals(
    workspace_id: str, service: MasterCvProposalServiceDep
) -> list[MasterCvProposalResponse]:
    """Return every master-CV proposal, including accepted and rejected."""
    return service.list(workspace_id)


@router.post(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/master-cv-proposals/refresh",
    response_model=AggregationReport,
    tags=["workspaces"],
    summary="Run both aggregations and insert any proposals that meet the thresholds",
)
def refresh_master_cv_proposals_endpoint(
    workspace_id: str, database: DatabaseDep
) -> AggregationReport:
    """Aggregate stored rulings and requirements into proposals."""
    return refresh_master_cv_proposals(database, workspace_id)


@router.post(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/master-cv-proposals/{{proposal_id}}/accept",
    response_model=MasterCvProposalResponse,
    tags=["workspaces"],
    summary="Accept a master-CV proposal",
)
def accept_master_cv_proposal(
    workspace_id: str, proposal_id: str, service: MasterCvProposalServiceDep
) -> MasterCvProposalResponse:
    """Rule this proposal accepted."""
    return service.accept(workspace_id, proposal_id)


@router.post(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/master-cv-proposals/{{proposal_id}}/reject",
    response_model=MasterCvProposalResponse,
    tags=["workspaces"],
    summary="Reject a master-CV proposal",
)
def reject_master_cv_proposal(
    workspace_id: str, proposal_id: str, service: MasterCvProposalServiceDep
) -> MasterCvProposalResponse:
    """Rule this proposal rejected."""
    return service.reject(workspace_id, proposal_id)
