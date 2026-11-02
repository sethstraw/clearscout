"""Typed read models for workspace learning-loop policy versions."""

from datetime import datetime

from tailorcv.domain.policy import LearningLoopPolicy
from tailorcv.shared.models import StrictModel


class LearningLoopPolicyStateResponse(StrictModel):
    """Current learning-loop policy version, or the empty default when none was saved."""

    workspace_id: str
    version: int
    content_sha256: str
    effective_at: datetime | None
    policy: LearningLoopPolicy


class LearningLoopPolicySavedResponse(StrictModel):
    """One append-only learning-loop policy version after a write."""

    id: str
    workspace_id: str
    version: int
    content_sha256: str
    effective_at: datetime
    supersedes_id: str | None
    unchanged: bool
    policy: LearningLoopPolicy
