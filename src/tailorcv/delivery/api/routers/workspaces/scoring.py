"""Workspace scoring-policy routes."""

from __future__ import annotations

from fastapi import APIRouter

from tailorcv.application.job_search.score_models import ScoreWeightSetResponse
from tailorcv.delivery.api.constants import API_PREFIX
from tailorcv.delivery.api.dependencies import ScorePolicyServiceDep
from tailorcv.delivery.api.models.jobs import ScoreWeightSetBody

router = APIRouter()


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/score-weight-sets",
    response_model=list[ScoreWeightSetResponse],
    tags=["workspaces"],
    summary="List the scoring policies this workspace can use",
)
def score_weight_sets(
    workspace_id: str, service: ScorePolicyServiceDep
) -> list[ScoreWeightSetResponse]:
    """List scoring policies for a workspace."""
    return service.list_score_weight_sets(workspace_id)


@router.post(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/score-weight-sets",
    response_model=ScoreWeightSetResponse,
    status_code=201,
    tags=["workspaces"],
    summary="Append a scoring policy",
)
def create_score_weight_set(
    workspace_id: str, body: ScoreWeightSetBody, service: ScorePolicyServiceDep
) -> ScoreWeightSetResponse:
    """Append and activate a scoring policy."""
    return service.create_score_weight_set(
        workspace_id,
        name=body.name,
        weights=body.weights.model_dump(),
    )
