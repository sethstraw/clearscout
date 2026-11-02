"""Workspace recorded-spend routes."""

from __future__ import annotations

from fastapi import APIRouter

from tailorcv.application.job_search.models import RunCostHistoryResponse, SpendSummaryResponse
from tailorcv.delivery.api.constants import API_PREFIX
from tailorcv.delivery.api.dependencies import SpendServiceDep

router = APIRouter()


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/spend-history",
    response_model=list[RunCostHistoryResponse],
    tags=["workspaces"],
    summary="Read what each kind of paid run has cost this workspace",
)
def read_run_cost_history(
    workspace_id: str, service: SpendServiceDep
) -> list[RunCostHistoryResponse]:
    """Report the estimated cost per unit of work for each paid task type."""
    return service.run_cost_history(workspace_id)


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/spend",
    response_model=SpendSummaryResponse,
    tags=["workspaces"],
    summary="Read recorded model spend for this workspace",
)
def workspace_spend(workspace_id: str, service: SpendServiceDep) -> SpendSummaryResponse:
    """Summarize recorded model spend for a workspace."""
    return service.spend_summary(workspace_id)
