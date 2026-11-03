"""Workspace learning-loop policy routes."""

from __future__ import annotations

from fastapi import APIRouter

from clearscout.application.workspaces import (
    LearningLoopPolicySavedResponse,
    LearningLoopPolicyStateResponse,
)
from clearscout.delivery.api.constants import API_PREFIX
from clearscout.delivery.api.dependencies import CorrelationId, LearningLoopPolicyServiceDep
from clearscout.delivery.api.models.workspaces import LearningLoopPolicyBody

router = APIRouter()


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/learning-loop-policy",
    response_model=LearningLoopPolicyStateResponse,
    tags=["workspaces"],
)
def read_learning_loop_policy(
    workspace_id: str, service: LearningLoopPolicyServiceDep
) -> LearningLoopPolicyStateResponse:
    """Read the learning-loop policy in force for one workspace."""
    return service.describe(workspace_id)


@router.put(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/learning-loop-policy",
    response_model=LearningLoopPolicySavedResponse,
    tags=["workspaces"],
)
def write_learning_loop_policy(
    correlation_id: CorrelationId,
    workspace_id: str,
    body: LearningLoopPolicyBody,
    service: LearningLoopPolicyServiceDep,
) -> LearningLoopPolicySavedResponse:
    """Append one complete learning-loop policy version."""
    return service.save(
        workspace_id,
        body.policy,
        origin="api",
        correlation_id=correlation_id,
        expected_version=body.expected_version,
    )
