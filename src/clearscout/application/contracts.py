"""Provider-neutral typed inputs for durable job-search use cases."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field, model_validator

from clearscout.domain import (
    ApplicationEventType,
    ApplicationStatus,
    EvaluationDecision,
    EvidenceClassification,
    EvidenceDirection,
    EvidenceSourceKind,
    EvidenceStrength,
    FindingState,
    PostingStatus,
    ScoreComponent,
    SpanStatus,
)
from clearscout.domain.records import RobotsReadBasis
from clearscout.domain.requirements import GateKind, RequirementImportance
from clearscout.domain.search_parameters import CompensationBasis, CurrencyCode
from clearscout.shared.models import StrictModel

SCORE_COMPONENTS = ("fit", "compensation", "eligibility", "evidence", "opportunity")


def score_weight_digest(weights: dict[str, str]) -> str:
    """Content-address component weights as their authored strings, so 0.20 and 0.2 differ."""
    return hashlib.sha256(
        json.dumps(weights, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ScoreComponentInput(StrictModel):
    """One score or an explicit, reasoned abstention."""

    value: int | None = Field(default=None, ge=0, le=100)
    reason: str = ""
    conflict: bool = False
    standing_source: Literal["compared", "extracted"] | None = None

    @model_validator(mode="after")
    def explain_abstention(self) -> ScoreComponentInput:
        """Keep missing information distinct from conflicting information."""
        if self.value is None and not self.reason.strip():
            raise ValueError("A null score requires a reason.")
        if self.value is not None and (self.reason.strip() or self.conflict):
            raise ValueError("A stated score cannot also be an abstention or conflict.")
        if self.value is None and self.standing_source is not None:
            raise ValueError("An abstention cannot carry a standing source.")
        return self

    @property
    def finding_state(self) -> FindingState:
        """Return the durable Answer-style state for this component."""
        if self.value is not None:
            return FindingState.SUPPORTED
        return FindingState.CONFLICT if self.conflict else FindingState.NOT_STATED


class EvaluationScores(StrictModel):
    """Five evidence-gated component findings; weights live in durable configuration."""

    fit: ScoreComponentInput
    compensation: ScoreComponentInput
    eligibility: ScoreComponentInput
    evidence: ScoreComponentInput
    opportunity: ScoreComponentInput

    def named(self) -> dict[str, ScoreComponentInput]:
        """Return components in the stable published order."""
        return {name: getattr(self, name) for name in SCORE_COMPONENTS}

    def overall(self, weights: dict[str, Decimal]) -> int | None:
        """Compute the composite only when every weighted component is stated.

        A component carrying zero weight is not part of the composite, so its
        abstention cannot suppress one.
        """
        counted = {
            name: item for name, item in self.named().items() if weights.get(name, Decimal(0)) > 0
        }
        if any(item.value is None for item in counted.values()):
            return None
        return round(
            sum(Decimal(item.value or 0) * weights[name] for name, item in counted.items())
        )


class CompensationInput(StrictModel):
    """One original-currency compensation component."""

    component_type: str = Field(min_length=1, max_length=80)
    amount_min: Decimal | None = None
    amount_max: Decimal | None = None
    currency: CurrencyCode
    basis: CompensationBasis
    geography: str = Field(default="", max_length=300)
    description: str = ""

    @model_validator(mode="after")
    def ordered_range(self) -> CompensationInput:
        """Refuse a range whose published maximum is below its minimum."""
        if (
            self.amount_min is not None
            and self.amount_max is not None
            and self.amount_min > self.amount_max
        ):
            raise ValueError("Compensation minimum cannot exceed its maximum.")
        return self


class EligibilityInput(StrictModel):
    """Geographic and employment facts observed for a posting."""

    workspace_location_key: str = Field(min_length=1, max_length=80)
    region_rule: str = Field(default="", max_length=160)
    arrangement: str = ""
    details: str = ""


class ClosingDateStatementResponse(StrictModel):
    """One publisher closing statement with retained date precision."""

    wording: str
    basis: str
    normalized_value: str | None = None
    precision: Literal["date", "timestamp"] | None = None
    timezone: str | None = None
    clarification: str = ""
    artifact_id: str | None = None


class PublisherClosingDateResponse(StrictModel):
    """Publisher dates and provenance, separate from posting closure."""

    statements: list[ClosingDateStatementResponse] = Field(default_factory=list)
    source_url: str = ""
    artifact_id: str | None = None
    observed_at: datetime | None = None
    normalized_value: str | None = None
    precision: Literal["date", "timestamp"] | None = None
    timezone: str | None = None
    needs_clarification: bool = False


class PostingBodyInput(StrictModel):
    """Original posting bytes or text stored as durable evidence."""

    logical_type: Literal[
        "posting.body.text",
        "posting.body.html",
        "posting.source.pdf",
        "posting.source.image",
    ]
    content: str = Field(min_length=1)
    encoding: Literal["utf-8", "base64"] = "utf-8"
    mime_type: str = Field(default="", max_length=160)
    extracted_text: str | None = None
    extraction_method: str = Field(min_length=1, max_length=80)
    fetched_at: datetime | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    robots_basis: RobotsReadBasis | None = None
    publisher_closing_date: PublisherClosingDateResponse | None = None

    @model_validator(mode="after")
    def binary_source_has_text(self) -> PostingBodyInput:
        """Require a readable extraction alongside PDF or image source bytes."""
        if self.logical_type in {
            "posting.source.pdf",
            "posting.source.image",
        } and (not self.extracted_text or not self.extracted_text.strip()):
            raise ValueError("PDF and image posting sources require extracted_text.")
        return self


class CandidateRecordInput(StrictModel):
    """Exact master-CV text snapshot used by this evaluation."""

    content: str = Field(min_length=1)
    source_uri: str = Field(min_length=1)
    effective_at: datetime


class ScoreEvidenceInput(StrictModel):
    """A stable, verbatim source anchor and every score it informs."""

    key: str = Field(min_length=1, max_length=160)
    source_kind: EvidenceSourceKind
    text: str = Field(min_length=1)
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, gt=0)
    locator: str = ""
    master_cv_entry_id: str | None = Field(default=None, max_length=160)
    components: list[ScoreComponent] = Field(default_factory=list)
    direction: EvidenceDirection = EvidenceDirection.SUPPORTS
    classification: EvidenceClassification
    strength: EvidenceStrength = EvidenceStrength.MODERATE
    requirement_id: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def valid_span(self) -> ScoreEvidenceInput:
        """Require an offset span or a quote with a meaningful source locator."""
        one_offset = (self.char_start is None) != (self.char_end is None)
        if one_offset:
            raise ValueError("Evidence offsets must be supplied together.")
        if self.char_start is not None and self.char_end is not None:
            if self.char_end <= self.char_start:
                raise ValueError("Evidence char_end must be greater than char_start.")
        elif not self.locator.strip():
            raise ValueError("Evidence without offsets requires a locator.")
        if (
            self.classification in {EvidenceClassification.GAP, EvidenceClassification.ASSERTED}
            and self.strength != EvidenceStrength.NONE
        ):
            raise ValueError(f"{self.classification} evidence must use strength 'none'.")
        if (
            self.classification not in {EvidenceClassification.GAP, EvidenceClassification.ASSERTED}
            and self.strength == EvidenceStrength.NONE
        ):
            raise ValueError("Supported match classifications require a non-zero strength.")
        return self


class EvaluationFactorInput(StrictModel):
    """One decision factor and the evidence keys that support or limit it."""

    text: str = Field(min_length=1)
    evidence_keys: list[str] = Field(default_factory=list)
    direction: EvidenceDirection = EvidenceDirection.SUPPORTS


class ReportReadingInput(StrictModel):
    """One deterministic Answer-shaped report finding."""

    question: str = Field(min_length=1)
    answer: str | None = None
    evidence_keys: list[str] = Field(default_factory=list)
    note: str = ""
    conflict: bool = False

    @model_validator(mode="after")
    def explain_missing_answer(self) -> ReportReadingInput:
        """Require every abstention to say why the source did not answer."""
        if self.answer is None and not self.note.strip():
            raise ValueError("A report abstention requires a note.")
        return self


class ReportDisagreementInput(StrictModel):
    """One unresolved model-versus-rules disagreement."""

    subject: str = Field(min_length=1)
    model_says: str = Field(min_length=1)
    rules_say: str = Field(min_length=1)
    evidence_keys: list[str] = Field(min_length=1)


class ReportRequirementInput(StrictModel):
    """One extracted requirement and its evidence map.

    ``span_status`` is about the posting's own wording; ``established`` is about
    whether the run settled the requirement against the record.
    """

    requirement_id: str = Field(min_length=1, max_length=160)
    importance: RequirementImportance
    text: str = Field(min_length=1)
    evidence_keys: list[str] = Field(default_factory=list)
    span_status: SpanStatus = SpanStatus.NEEDS_REVIEW
    established: bool = True
    # A settled requirement can still be unmet: an outright GAP is established
    # (the record was checked) yet answered by nothing.
    unmet: bool = False
    dimension: str = Field(default="hard", max_length=20)
    is_hard_gate: bool = False
    gate_kind: GateKind | None = None
    gate_quote: str | None = None
    gate_blocking: bool | None = None


class ReportCheckInput(StrictModel):
    """One deterministic or critic verification finding."""

    check: str = Field(min_length=1)
    result: str = Field(min_length=1, max_length=80)
    detail: str = ""


class ReportProvenanceInput(StrictModel):
    """One derivative line bound to its source evidence."""

    derivative_line: str = Field(min_length=1)
    evidence_keys: list[str] = Field(min_length=1)


class ReportCostInput(StrictModel):
    """One provider-neutral cost line recorded at evaluation time."""

    stage: str = Field(min_length=1, max_length=120)
    model: str = Field(default="", max_length=160)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    amount: Decimal | None = Field(default=None, ge=0)
    currency: str = Field(default="USD", min_length=1, max_length=12)


# A rule refusal answers before any prompt runs, so its report names no prompt suite.
RULE_REFUSAL_PROMPT_FINGERPRINT = "none (refused by rule)"


class FitReportInput(StrictModel):
    """Structured findings from which the durable Markdown report is rendered once."""

    prompt_fingerprint: str = Field(min_length=1, max_length=160)
    search_parameter_set_id: str | None = None
    code_revision: str | None = None
    hard_gates: list[ReportRequirementInput] = Field(default_factory=list)
    reading: list[ReportReadingInput] = Field(min_length=1)
    disagreements: list[ReportDisagreementInput] = Field(default_factory=list)
    requirements: list[ReportRequirementInput] = Field(min_length=1)
    coverage_present: int = Field(ge=0)
    coverage_total: int = Field(ge=0)
    changed_since_previous: list[str] = Field(default_factory=list)
    unrecognized_requirements: list[str] = Field(default_factory=list)
    checks: list[ReportCheckInput] = Field(min_length=1)
    critic_scores: dict[str, int] = Field(default_factory=dict)
    length_budget: str = Field(min_length=1)
    provenance: list[ReportProvenanceInput] = Field(default_factory=list)
    costs: list[ReportCostInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_coverage(self) -> FitReportInput:
        """Reject impossible coverage and out-of-range critic values."""
        if self.coverage_present > self.coverage_total:
            raise ValueError("Coverage present cannot exceed coverage total.")
        if any(value < 0 or value > 5 for value in self.critic_scores.values()):
            raise ValueError("Critic scores must be between 0 and 5.")
        identifiers = [item.requirement_id for item in [*self.requirements, *self.hard_gates]]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Requirement IDs must be unique across requirements and hard gates.")
        return self


class EvaluationCommit(StrictModel):
    """Complete proposed evaluation committed as one immutable transaction."""

    workspace_id: str
    supersedes_evaluation_id: str | None = None
    employer: str = Field(min_length=1, max_length=300)
    title: str = Field(min_length=1, max_length=500)
    location: str = Field(default="", max_length=500)
    requisition_id: str = Field(default="", max_length=300)
    canonical_url: str = ""
    discovery_url: str = ""
    first_seen_at: datetime
    observed_at: datetime
    posting_status: PostingStatus
    residence_rule: str = ""
    employment_type: str = Field(default="", max_length=160)
    payroll_arrangement: str = ""
    decision: EvaluationDecision
    rationale: str = Field(min_length=1)
    scope_summary: str = ""
    qualifications_summary: str = ""
    evaluated_at: datetime
    score_weight_set_id: str | None = None
    scores: EvaluationScores
    pros: list[EvaluationFactorInput] = Field(min_length=1)
    cons: list[EvaluationFactorInput] = Field(min_length=1)
    gaps: list[EvaluationFactorInput] = Field(default_factory=list)
    risks: list[EvaluationFactorInput] = Field(default_factory=list)
    compensation: list[CompensationInput] = Field(default_factory=list)
    eligibility: EligibilityInput
    source_name: str = ""
    source_timestamp: str | None = None
    posting_body: PostingBodyInput
    candidate_record: CandidateRecordInput
    score_evidence: list[ScoreEvidenceInput] = Field(min_length=1)
    fit_report: FitReportInput

    @model_validator(mode="after")
    def enforce_hard_gates(self) -> EvaluationCommit:
        """Reject an evaluation that breaks a durable evidence rule."""
        _validate_actionable_evaluation(self)
        _validate_score_evidence_coverage(self)
        _validate_unique_evidence_keys(self)
        _validate_evidence_references(self)
        return self

    def digest(self) -> str:
        """Bind idempotency to findings, excluding the concurrency precondition."""
        payload = json.dumps(
            self.model_dump(mode="json", exclude={"supersedes_evaluation_id"}),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_actionable_evaluation(proposal: EvaluationCommit) -> None:
    """Require an actionable proposal to name an active, eligible posting."""
    actionable = {
        EvaluationDecision.APPLY_NOW,
        EvaluationDecision.PREMIUM_TARGET,
    }
    if proposal.decision in actionable and proposal.posting_status != "active":
        raise ValueError("An actionable evaluation requires an active posting.")
    if proposal.decision in actionable and not proposal.eligibility.workspace_location_key.strip():
        raise ValueError(
            "An actionable evaluation names the residence rule it relied on; "
            "eligibility.workspace_location_key is empty."
        )


def _validate_score_evidence_coverage(proposal: EvaluationCommit) -> None:
    """Require evidence for every stated or conflicting score."""
    expected = {
        component
        for component, score in proposal.scores.named().items()
        if score.value is not None or score.conflict
    }
    covered = {component for item in proposal.score_evidence for component in item.components}
    missing = sorted(expected - covered)
    if missing:
        raise ValueError(
            "Every stated or conflicting score requires explicit evidence; missing: "
            + ", ".join(missing)
        )


def _validate_unique_evidence_keys(proposal: EvaluationCommit) -> None:
    """Reject duplicate evidence keys within a proposed evaluation."""
    if len({item.key for item in proposal.score_evidence}) != len(proposal.score_evidence):
        raise ValueError("Evidence keys must be unique within an evaluation.")


def _validate_evidence_references(proposal: EvaluationCommit) -> None:
    """Reject findings and report lines that cite no stored evidence."""
    known = {item.key for item in proposal.score_evidence}
    references = [
        *(
            key
            for group in (proposal.pros, proposal.cons, proposal.gaps, proposal.risks)
            for factor in group
            for key in factor.evidence_keys
        ),
        *(key for item in proposal.fit_report.reading for key in item.evidence_keys),
        *(key for item in proposal.fit_report.disagreements for key in item.evidence_keys),
        *(key for item in proposal.fit_report.hard_gates for key in item.evidence_keys),
        *(key for item in proposal.fit_report.requirements for key in item.evidence_keys),
        *(key for item in proposal.fit_report.provenance for key in item.evidence_keys),
    ]
    unknown = sorted(set(references) - known)
    if unknown:
        raise ValueError("Evidence references unknown keys: " + ", ".join(unknown))


class ApplicationEventInput(StrictModel):
    """Append-only application status, correction, or reversal."""

    status: ApplicationStatus
    event_type: ApplicationEventType = ApplicationEventType.STATUS
    occurred_at: datetime
    notes: str = ""
    artifact_uri: str = ""
    corrects_event_id: str | None = None
    reverses_event_id: str | None = None

    @model_validator(mode="after")
    def correction_reference(self) -> ApplicationEventInput:
        """Require the recorded event that a correction or reversal addresses."""
        if self.event_type is ApplicationEventType.CORRECTION and not self.corrects_event_id:
            raise ValueError("A correction must identify the event it corrects.")
        if self.event_type is ApplicationEventType.REVERSAL and not self.reverses_event_id:
            raise ValueError("A reversal must identify the event it reverses.")
        return self

    def digest(self) -> str:
        """Return a stable digest for idempotency conflict checks."""
        data: dict[str, Any] = self.model_dump(mode="json")
        payload = json.dumps(data, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
