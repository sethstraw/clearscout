"""Closed vocabularies for pending discoveries and relevance reviews."""

from typing import Annotated, Literal

from pydantic import Field

from tailorcv.application.discovery.screening import (
    ConstraintConfidence,
)

DiscoveryRecommendation = Literal["review", "investigate", "unlikely"]


PendingDiscoveryStagingProvenance = Literal[
    "guided_campaign", "board_scan", "person_added", "unrecorded"
]


RelevanceReviewVerdict = Literal["review", "investigate", "unlikely", "insufficient_evidence"]


RelevanceReviewCoreWorkAlignment = Literal["direct", "transferable", "absent", "unresolved"]


RelevanceReviewQualificationAlignment = Literal[
    "established", "partially_established", "absent", "unresolved"
]


RelevanceReviewHardCondition = Literal["passes", "conflicts", "unresolved"]


RelevanceReviewDecisiveBasis = Literal[
    "core_match",
    "transferable_match",
    "core_gap",
    "hard_conflict",
    "logistics_unknown",
    "insufficient_posting",
]


# A non-verdict answer: the text was too thin to judge, so no class is recorded.
RelevanceReviewUnjudged = Literal["insufficient_posting_text"]


RelevanceReviewAnswerOutcome = Literal["judged", RelevanceReviewUnjudged]


# Why local validation rejected a billed provider answer, leaving no review.
RelevanceReviewRejectionCode = Literal[
    "missing_result",
    "duplicate_result",
    "wrong_posting_span",
    "invalid_cv_evidence",
    "inconsistent_verdict",
    "leading_excerpt_only",
]


# An input that changed since the review ran, in the order a reader names them.
RelevanceReviewStaleInput = Literal[
    "master_cv", "search_parameters", "review_instructions", "keep_note"
]


# How the queue reads a row's relevance: a verdict, a too-thin posting, a
# person's dispute, a paid call rejected locally, or no review at all.
PendingRelevanceKey = Literal[
    "not_reviewed", "disputed", "review_rejected", RelevanceReviewUnjudged, RelevanceReviewVerdict
]


# A stale review holds "out_of_date" and one state per changed input.
PendingReviewStatus = Literal[
    "out_of_date",
    "out_of_date_master_cv",
    "out_of_date_search_parameters",
    "out_of_date_review_instructions",
    "out_of_date_keep_note",
    "current",
]


PendingConstraintRoute = (
    Literal["not_excluded", "pursue_only", "needs_clarification", "excluded_only"]
    | ConstraintConfidence
)


PendingDiscoveryId = Annotated[str, Field(min_length=1)]
