"""Typed read models for versioned scoring policies."""

from datetime import datetime

from clearscout.domain.records import ScoreComponent
from clearscout.shared.models import StrictModel


class ScoreWeightResponse(StrictModel):
    """One component weight inside a policy."""

    component: ScoreComponent
    weight: float


class ScoreWeightSetResponse(StrictModel):
    """One versioned scoring policy."""

    id: str
    name: str
    version: int
    effective_at: datetime
    is_shipped_default: bool
    is_active: bool
    weights: list[ScoreWeightResponse]
