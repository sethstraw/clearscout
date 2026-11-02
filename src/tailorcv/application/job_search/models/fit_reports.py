"""Job detail, fit reports, requirements and requirement gaps."""

from datetime import datetime
from typing import Literal

from pydantic import Field

from tailorcv.application.job_search.models.jobs import (
    ApplicationEventResponse,
    CompensationComponentResponse,
    EligibilityRecordResponse,
    EvaluationVersionResponse,
    EvidenceResponse,
    FactorResponse,
    JobSummaryResponse,
    ObservationResponse,
    ScoreArithmeticResponse,
    ScorePolicyResponse,
    UnsupportedResponse,
)
from tailorcv.domain import (
    ArtifactLogicalType,
    EvidenceSourceKind,
    SpanStatus,
)
from tailorcv.domain.requirements import GateKind, RequirementImportance
from tailorcv.shared.models import StrictModel


class OrphanedRulingResponse(StrictModel):
    """A ruling whose requirement dropped out of the current evaluation."""

    requirement_key: str
    ruling: Literal["disputed_fit", "disputed_extraction"]
    note: str | None
    requirement_text: str
    updated_at: datetime


RequirementDimension = Literal["hard", "behavioral"]


class PostingRequirementResponse(StrictModel):
    """One requirement as the current evaluation extracted it.

    Present on the job detail so the SPA renders the posting's own
    requirement text rather than reconstructing it from evidence spans,
    which are optional and may be absent for a valid requirement.
    """

    requirement_id: str
    requirement_key: str
    text: str
    importance: RequirementImportance
    position: int


class JobDetailResponse(JobSummaryResponse):
    """Job workspace including observations, evidence, factors, and history."""

    latest_observation: ObservationResponse | None
    observations: list[ObservationResponse]
    evidence: list[EvidenceResponse]
    requirements: list[PostingRequirementResponse] = Field(default_factory=list)
    factors: list[FactorResponse]
    score_policy: ScorePolicyResponse
    score_arithmetic: list[ScoreArithmeticResponse]
    unsupported: list[UnsupportedResponse]
    fit_report_available: bool
    observation_count: int
    evaluation_history: list[EvaluationVersionResponse]
    evaluation_count: int
    application_events: list[ApplicationEventResponse]
    compensation_components: list[CompensationComponentResponse]
    eligibility_records: list[EligibilityRecordResponse]
    posting_hint: str | None
    enable_dispute_reextraction: bool
    enable_dispute_rematch: bool
    disputed_fit_ruling_count: int
    disputed_extraction_ruling_count: int
    orphaned_rulings: list[OrphanedRulingResponse]
    orphaned_ruling_count: int


class EvidenceSourceResponse(StrictModel):
    """Verified source text and exact anchor behind one evidence record."""

    evidence_id: str
    artifact_id: str
    logical_type: ArtifactLogicalType
    mime_type: str
    sha256: str
    source_kind: EvidenceSourceKind
    locator: str
    char_start: int | None
    char_end: int | None
    body: str
    rendered_text: str


class ReportRequirementResponse(StrictModel):
    """One stored requirement or hard gate exposed to the job workspace."""

    requirement_id: str
    importance: RequirementImportance
    text: str
    evidence_keys: list[str]
    span_status: SpanStatus
    # False means the run settled nothing about this requirement either way.
    established: bool = True
    # True means the run settled the requirement as an outright gap: checked,
    # answered by nothing. Distinct from unestablished (never settled).
    unmet: bool = False
    requirement_key: str
    # `hard` is a skill/tool/scale/year requirement answered by concrete evidence.
    # `behavioral` is a disposition answered by a described action pattern.
    dimension: RequirementDimension = "hard"
    is_hard_gate: bool = False
    gate_kind: GateKind | None = None
    gate_quote: str | None = None
    gate_blocking: bool | None = None


class FitReportResponse(StrictModel):
    """Verified stored report and its structured provenance; no model runs at view time."""

    job_posting_id: str
    evaluation_id: str
    evaluation_version: int
    artifact_id: str
    sha256: str
    markdown: str
    requirements: list[ReportRequirementResponse]


RulingValue = Literal[
    "met",
    "not_applicable",
    "disputed_fit",
    "disputed_extraction",
    "reversed",
]


class RequirementGapSourceResponse(StrictModel):
    """One current evaluation that raised a requirement the master CV did not answer."""

    job_posting_id: str
    employer: str
    title: str
    importance: RequirementImportance
    evaluated_at: datetime
    requirement_key: str
    # A workspace ruling on this specific requirement identity, if any.
    ruling: RulingValue | None = None


class RequirementGapResponse(StrictModel):
    """One requirement text and every current evaluation that left it unanswered."""

    text: str
    raised_by: list[RequirementGapSourceResponse]


class RequirementGapSummaryResponse(StrictModel):
    """Whole cross-posting view of what the master CV could not answer.

    ``gap_count`` counts groups the stated ``grouping_rule`` produced; two
    postings share a group only when their texts match the rule.
    ``omitted_count`` is how many of those groups the bounded list left out.
    """

    evaluations_read: int
    gap_count: int
    omitted_count: int
    grouping_rule: str
    gaps: list[RequirementGapResponse]
