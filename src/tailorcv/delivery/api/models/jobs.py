"""Job ledger API contracts."""

from decimal import Decimal

from pydantic import Field, field_validator

from tailorcv.application.job_search.commands import MAX_JOB_ARCHIVE_BATCH
from tailorcv.application.job_search.models import (
    ApplicationEventResponse,
    ApplicationFunnelResponse,
    CompensationComponentResponse,
    CompletenessCheckResponse,
    CompletenessResponse,
    EligibilityRecordResponse,
    EvaluationVersionResponse,
    EvidenceResponse,
    EvidenceScoreLinkResponse,
    EvidenceSourceResponse,
    FactorResponse,
    FitReportResponse,
    JobArchiveBatchResponse,
    JobArchiveResponse,
    JobDetailResponse,
    JobPageResponse,
    JobSummaryResponse,
    ObservationResponse,
    PendingDiscoveryResponse,
    PostingDiffResponse,
    PostingFactDiffResponse,
    PostingObservationBodyResponse,
    ScoreArithmeticResponse,
    ScorePolicyResponse,
    SourceScanHistoryPageResponse,
    SourceScanResponse,
    SpendCallResponse,
    SpendGroupResponse,
    SpendSummaryResponse,
    UnsupportedResponse,
    WorkspaceSummaryResponse,
)
from tailorcv.application.job_search.score_models import ScoreWeightResponse, ScoreWeightSetResponse
from tailorcv.shared.models import StrictModel

__all__ = [
    "ApplicationEventResponse",
    "ApplicationFunnelResponse",
    "CompensationComponentResponse",
    "CompletenessCheckResponse",
    "CompletenessResponse",
    "EligibilityRecordResponse",
    "EvaluationVersionResponse",
    "EvidenceResponse",
    "EvidenceScoreLinkResponse",
    "EvidenceSourceResponse",
    "FactorResponse",
    "FitReportResponse",
    "JobArchiveBatchBody",
    "JobArchiveBatchResponse",
    "JobArchiveResponse",
    "JobDetailResponse",
    "JobPageResponse",
    "JobSummaryResponse",
    "ObservationResponse",
    "PendingDiscoveryResponse",
    "PostingDiffResponse",
    "PostingFactDiffResponse",
    "PostingObservationBodyResponse",
    "ScoreArithmeticResponse",
    "ScorePolicyResponse",
    "ScoreWeightResponse",
    "ScoreWeightSetBody",
    "ScoreWeightSetResponse",
    "ScoreWeightsInput",
    "SourceScanHistoryPageResponse",
    "SourceScanResponse",
    "SpendCallResponse",
    "SpendGroupResponse",
    "SpendSummaryResponse",
    "TailoringPreflightResponse",
    "UnsupportedResponse",
    "WorkspaceSummaryResponse",
]


class ScoreWeightsInput(StrictModel):
    """The five component weights in one scoring policy."""

    fit: Decimal
    compensation: Decimal
    eligibility: Decimal
    evidence: Decimal
    opportunity: Decimal


class JobArchiveBatchBody(StrictModel):
    """The exact tracked jobs the person selected for one atomic archive."""

    job_posting_ids: list[str] = Field(min_length=1, max_length=MAX_JOB_ARCHIVE_BATCH)

    @field_validator("job_posting_ids")
    @classmethod
    def unique_job_ids(cls, values: list[str]) -> list[str]:
        """Reject ambiguous or unusable selections before command dispatch."""
        if any(not value.strip() for value in values):
            raise ValueError("job_posting_ids cannot contain an empty id")
        if len(values) != len(set(values)):
            raise ValueError("job_posting_ids cannot contain duplicates")
        return values


class ScoreWeightSetBody(StrictModel):
    """A new policy to append. Weights must cover every component and sum to 1."""

    name: str
    weights: ScoreWeightsInput


class TailoringPreflightResponse(StrictModel):
    """Whether current workspace inputs exactly match a completed package."""

    reuse_available: bool
    tailoring_run_id: str | None
    artifact_ids: list[str]
    cost_usd: float | None
