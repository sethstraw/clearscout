"""Typed read models for the learning-loop metrics dashboard.

The service returns one snapshot per workspace, carrying three measurable
signals: whether the loop still re-proposes rejected identities, how often
master-CV proposals land, and whether the top of the pending queue is still
surfacing missed-requirement work.
"""

from datetime import datetime

from clearscout.shared.models import StrictModel


class LearningLoopSignal(StrictModel):
    """One measured signal as an integer numerator over an integer denominator."""

    numerator: int
    denominator: int


class LearningLoopMetricsResponse(StrictModel):
    """One snapshot of the three learning-loop signals for one workspace."""

    workspace_id: str
    computed_at: datetime
    top_n: int
    reproposed_rejected_identities: LearningLoopSignal
    master_cv_proposal_acceptance: LearningLoopSignal
    pending_queue_missed_matches: LearningLoopSignal
