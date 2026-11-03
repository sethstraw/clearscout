"""Relevance review recommendations, citations and rejections."""

from datetime import datetime

from pydantic import Field

from clearscout.application.job_search.models.vocabulary import (
    RelevanceReviewAnswerOutcome,
    RelevanceReviewCoreWorkAlignment,
    RelevanceReviewDecisiveBasis,
    RelevanceReviewHardCondition,
    RelevanceReviewQualificationAlignment,
    RelevanceReviewRejectionCode,
    RelevanceReviewStaleInput,
    RelevanceReviewVerdict,
)
from clearscout.shared.models import StrictModel


class RelevanceReviewRecommendation(StrictModel):
    """One semantic-review answer whose citations can be checked locally."""

    pending_discovery_id: str
    recommendation: RelevanceReviewVerdict
    summary: str = Field(min_length=1)
    outcome: RelevanceReviewAnswerOutcome = "judged"
    core_work_alignment: RelevanceReviewCoreWorkAlignment
    core_qualification_alignment: RelevanceReviewQualificationAlignment
    hard_condition: RelevanceReviewHardCondition
    # The listed exclusion condition a hard conflict rests on, verbatim; None on any other basis.
    matched_exclusion: str | None = None
    decisive_basis: RelevanceReviewDecisiveBasis
    posting_evidence_span_ids: list[str] = Field(min_length=1, max_length=5)
    master_cv_evidence_ids: list[str] = Field(default_factory=list, max_length=10)
    unresolved_questions: list[str] = Field(default_factory=list, max_length=8)


class RelevanceReviewBatchResponse(StrictModel):
    """The complete response to one bounded semantic-review call."""

    recommendations: list[RelevanceReviewRecommendation]


class RelevanceReviewCitation(StrictModel):
    """Application-resolved exact source span returned with a recommendation."""

    span_id: str
    text: str
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)


class RelevanceReviewResponse(StrictModel):
    """The current locally validated relevance review for one discovery."""

    id: str
    pending_discovery_id: str
    employer: str
    title: str
    outcome: RelevanceReviewAnswerOutcome
    recommendation: RelevanceReviewVerdict | None
    summary: str
    core_work_alignment: RelevanceReviewCoreWorkAlignment | None
    core_qualification_alignment: RelevanceReviewQualificationAlignment | None
    hard_condition: RelevanceReviewHardCondition | None
    # Null on every basis but hard_conflict, and on a hard_conflict stored without one.
    matched_exclusion: str | None
    decisive_basis: RelevanceReviewDecisiveBasis | None
    posting_citations: list[RelevanceReviewCitation]
    master_cv_evidence_ids: list[str]
    unresolved_questions: list[str]
    fetched_at: datetime
    reviewed_at: datetime
    task_id: str
    master_cv_version_id: str
    search_parameter_set_id: str
    # Empty while current.
    out_of_date_because: list[RelevanceReviewStaleInput]
    # Set when the workspace has disputed this exact review; the verdict above
    # stays as the model wrote it.
    disputed_at: datetime | None = None
    dispute_note: str | None = None


class RelevanceReviewRejectionResponse(StrictModel):
    """The paid review that recorded no verdict for one discovery, and why."""

    pending_discovery_id: str
    failure_code: RelevanceReviewRejectionCode
    detail: str
    rejected_at: datetime
    task_id: str
