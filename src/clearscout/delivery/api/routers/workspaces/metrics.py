"""Learning-loop signals for one workspace, read on demand."""

from __future__ import annotations

from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool

from clearscout.application.workspaces import LearningLoopMetricsResponse
from clearscout.delivery.api.constants import API_PREFIX
from clearscout.delivery.api.dependencies import LearningLoopMetricsServiceDep

router = APIRouter()


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/learning-loop-metrics",
    response_model=LearningLoopMetricsResponse,
    tags=["workspaces"],
    summary="Learning-loop signals for one workspace",
)
async def read_learning_loop_metrics(
    workspace_id: str,
    service: LearningLoopMetricsServiceDep,
) -> LearningLoopMetricsResponse:
    """Return one snapshot of the three learning-loop signals."""
    return await run_in_threadpool(service.read, workspace_id)
