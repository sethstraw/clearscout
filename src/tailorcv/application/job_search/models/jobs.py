"""Job list queries, summaries, evaluations, evidence and scoring."""

from datetime import date, datetime
from typing import Literal

from pydantic import ConfigDict, Field, field_validator

from tailorcv.application.contracts import PublisherClosingDateResponse
from tailorcv.domain import (
    ApplicationEventType,
    ApplicationStatus,
    EvaluationDecision,
    EvidenceClassification,
    EvidenceDirection,
    EvidenceSourceKind,
    EvidenceStrength,
    FactorType,
    FindingState,
    PostingStatus,
    ScoreComponent,
)
from tailorcv.domain.records import (
    RobotsReadBasis,
)
from tailorcv.domain.search_parameters import CompensationBasis, CurrencyCode
from tailorcv.shared.models import StrictModel


class JobListQuery(StrictModel):
    """Frozen filter and pagination command for the durable job ledger."""

    model_config = ConfigDict(frozen=True)

    workspace_id: str
    search: str = ""
    decisions: tuple[str, ...] = ()
    application_statuses: tuple[str, ...] = ()
    posting_statuses: tuple[str, ...] = ()
    sort: str = "recorded_score"
    descending: bool = True
    offset: int = 0
    limit: int = 50
    completeness: str = ""
    places: tuple[str, ...] = ()
    employers: tuple[str, ...] = ()
    include_archived: bool = False


class JobArchiveResponse(StrictModel):
    """Reversible job visibility state; no child record is deleted."""

    job_posting_id: str
    workspace_id: str
    archived: bool
    archived_at: datetime | None
    changed: bool


class JobArchiveBatchResponse(StrictModel):
    """Atomic result for archiving a caller-named set of tracked jobs."""

    job_posting_ids: list[str]
    requested_count: int
    archived_count: int
    already_archived_count: int


class PostingHintBody(StrictModel):
    """A tailoring-prompt hint that is never evidence; None or an empty string clears it."""

    hint: str | None = None


class PostingHintResponse(StrictModel):
    """The current posting hint after a set-hint call."""

    job_posting_id: str
    workspace_id: str
    hint: str | None
    changed: bool


class CompletenessCheckResponse(StrictModel):
    """One computed completeness requirement and its repair destination."""

    code: str
    label: str
    complete: bool
    action: str


class CompletenessResponse(StrictModel):
    """Computed job completeness and the repair route for each gap."""

    status: Literal["complete", "incomplete"]
    missing: list[str]
    checks: list[CompletenessCheckResponse]


class CompensationComponentResponse(StrictModel):
    """Original-currency compensation component."""

    component_type: str
    amount_min: float | None
    amount_max: float | None
    annualized_min: float | None
    annualized_max: float | None
    currency: CurrencyCode
    basis: CompensationBasis
    geography: str
    description: str


class EligibilityRecordResponse(StrictModel):
    """Geographic and employment eligibility facts."""

    workspace_location_key: str
    region_rule: str
    arrangement: str
    details: str


class EvaluationInputResponse(StrictModel):
    """Recorded and current identities for one input to an immutable assessment."""

    name: Literal["cv", "search_parameters", "posting", "prompts", "build"]
    recorded: str | None
    current: str | None
    status: Literal["current", "changed", "unknown"]


class EvaluationReviewResponse(StrictModel):
    """Whether a recorded evaluation needs a human decision about changed inputs."""

    status: Literal["current", "needs_review", "reviewed", "unknown"]
    reason: Literal["posting_changed", "criteria_changed"] | None = None
    reviewed_at: datetime | None = None


class JobSummaryResponse(StrictModel):
    """Filterable job ledger row."""

    job_posting_id: str
    employer: str
    title: str
    first_seen_at: datetime
    evaluation_id: str | None
    evaluation_version: int | None
    evaluation_inputs: list[EvaluationInputResponse] = Field(default_factory=list)
    evaluation_review: EvaluationReviewResponse = Field(
        default_factory=lambda: EvaluationReviewResponse(status="unknown")
    )
    decision: EvaluationDecision | None
    rationale: str | None
    scope_summary: str | None
    qualifications_summary: str | None
    evaluated_at: datetime | None
    manual_priority: Literal["high", "medium", "low"] | None = None
    manual_fit: Literal["strong", "possible", "weak"] | None = None
    manual_note: str = ""
    manual_reviewed_at: datetime | None = None
    recorded_fit_score: int | None = None
    recorded_compensation_score: int | None = None
    recorded_overall_score: int | None = None
    recorded_compensation: str = ""
    recorded_work_model: str = ""
    recorded_eligibility: str = ""
    next_action: str = ""
    recorded_deadline: str = ""
    recorded_source: str = ""
    publisher_closing_date: PublisherClosingDateResponse | None = None
    closing_date_override: date | None = None
    fit_score: int | None
    compensation_score: int | None
    compensation_standing_source: Literal["compared", "extracted"] | None = None
    eligibility_score: int | None
    evidence_score: int | None
    opportunity_score: int | None
    overall_score: int | None
    overall_score_lower_bound: int | None
    unscored_weighted_component_count: int
    application_status: ApplicationStatus
    application_id: str | None
    application_event_count: int
    archived_at: datetime | None = None
    place: str | None = None
    # Read from place by the workspace's PlaceReader.
    countries: list[str] = Field(default_factory=list)
    posting_status: PostingStatus | None = None
    last_verified_at: datetime | None = None
    score_policy_name: str | None = None
    score_policy_version: int | None = None
    completeness: CompletenessResponse
    compensation_components: list[CompensationComponentResponse]
    eligibility_records: list[EligibilityRecordResponse]


class ClosingDateEditInput(StrictModel):
    """A personal tracking date, or an explicit reset to publisher information."""

    closing_date_override: date | None

    @field_validator("closing_date_override", mode="before")
    @classmethod
    def date_only(cls, value: object) -> date | None:
        """Reject timestamps and noncanonical date strings."""
        if value is None or type(value) is date:
            return value
        if isinstance(value, str):
            try:
                parsed = date.fromisoformat(value)
            except ValueError:
                pass
            else:
                if value == parsed.isoformat():
                    return parsed
        raise ValueError("Enter a date as YYYY-MM-DD, or clear it to use the publisher date.")


class ManualJobReviewInput(StrictModel):
    """Editable recorded facts and judgments, separate from model evaluation."""

    priority: Literal["high", "medium", "low"] | None = None
    fit: Literal["strong", "possible", "weak"] | None = None
    note: str = Field(default="", max_length=2000)
    recorded_fit_score: int | None = Field(default=None, ge=0, le=100)
    recorded_compensation_score: int | None = Field(default=None, ge=0, le=100)
    recorded_overall_score: int | None = Field(default=None, ge=0, le=100)
    recorded_compensation: str = Field(default="", max_length=500)
    recorded_work_model: str = Field(default="", max_length=500)
    recorded_eligibility: str = Field(default="", max_length=500)
    next_action: str = Field(default="", max_length=500)
    recorded_deadline: str = Field(default="", max_length=200)
    recorded_source: str = Field(default="", max_length=500)


class ObservationResponse(StrictModel):
    """Append-only posting observation."""

    id: str
    observed_at: datetime
    posting_status: PostingStatus
    location: str
    residence_rule: str
    employment_type: str
    payroll_arrangement: str
    body_artifact_id: str | None
    extraction_method: str | None
    fetched_at: datetime | None
    http_status: int | None
    robots_basis: RobotsReadBasis | None
    publisher_closing_date: PublisherClosingDateResponse | None = None


class EvidenceScoreLinkResponse(StrictModel):
    """One score component relationship carried by an evidence record."""

    evidence_id: str
    component: ScoreComponent | None = None
    direction: EvidenceDirection


class EvidenceResponse(StrictModel):
    """Verbatim evidence with an optional deep link into a posting observation."""

    id: str
    text: str
    classification: EvidenceClassification
    artifact_id: str | None
    char_start: int | None
    char_end: int | None
    locator: str
    source_kind: EvidenceSourceKind
    master_cv_entry_id: str | None
    requirement_id: str | None
    strength: EvidenceStrength | None
    observation_id: str | None
    score_links: list[EvidenceScoreLinkResponse]


class FactorResponse(StrictModel):
    """One ordered evaluation pro, con, gap, or risk."""

    type: FactorType
    position: int
    text: str
    evidence_ids: list[str]


class ScorePolicyResponse(StrictModel):
    """Versioned score policy retained with an evaluation."""

    id: str
    name: str
    version: int


class ScoreArithmeticResponse(StrictModel):
    """One reconstructable row of score arithmetic and its evidence paths."""

    component: ScoreComponent
    score: int | None
    weight: float | None
    product: float | None
    finding_state: FindingState
    reason: str
    standing_source: Literal["compared", "extracted"] | None
    evidence_links: list[EvidenceScoreLinkResponse]


class UnsupportedResponse(StrictModel):
    """One score or factor that currently lacks evidence."""

    kind: Literal["score", "factor"]
    id: str
    label: str
    reason: str


class ApplicationEventResponse(StrictModel):
    """One immutable application history event."""

    id: str
    application_id: str
    status: ApplicationStatus
    event_type: ApplicationEventType
    occurred_at: datetime
    created_at: datetime
    notes: str
    artifact_uri: str
    corrects_event_id: str | None
    reverses_event_id: str | None


class EvaluationVersionResponse(StrictModel):
    """One immutable evaluation snapshot in a posting's supersession chain."""

    id: str
    version_number: int
    evaluation_inputs: list[EvaluationInputResponse] = Field(default_factory=list)
    supersedes_evaluation_id: str | None
    observation_id: str | None
    master_cv_version_id: str | None
    score_weight_set_id: str | None
    fit_report_artifact_id: str | None
    decision: EvaluationDecision
    rationale: str
    scope_summary: str
    qualifications_summary: str
    evaluated_at: datetime
    source_timestamp: str | None
    fit_score: int | None
    compensation_score: int | None
    compensation_standing_source: Literal["compared", "extracted"] | None = None
    eligibility_score: int | None
    evidence_score: int | None
    opportunity_score: int | None
    overall_score: int | None
    is_current: bool
