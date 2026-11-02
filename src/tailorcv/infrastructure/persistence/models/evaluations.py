"""The immutable evaluation ledger and the application history beneath it.

Nothing here is edited. A changed judgment supersedes its predecessor, and a
changed application state is another event, so what was decided and when stays
recoverable. Every score and factor links to the evidence that carried it.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from tailorcv.domain import (
    APPLICATION_STATUSES,
    EVALUATION_DECISIONS,
    SCORE_COMPONENTS,
)
from tailorcv.domain.requirements import (
    GATE_KINDS,
    REQUIREMENT_IMPORTANCES,
    GateKind,
    RequirementImportance,
)
from tailorcv.domain.search_parameters import BASES
from tailorcv.infrastructure.persistence.models.base import (
    AuditMixin,
    Base,
    IdMixin,
    SoftArchiveMixin,
    _allowed,
)
from tailorcv.infrastructure.persistence.types import UTCDateTime


class JobEvaluation(IdMixin, AuditMixin, Base):
    """One immutable version in a posting's append-only evaluation history."""

    __tablename__ = "job_evaluations"
    __table_args__ = (
        UniqueConstraint("job_posting_id", "version_number", name="uq_job_evaluation_version"),
        UniqueConstraint("supersedes_evaluation_id", name="uq_evaluation_supersedes"),
        CheckConstraint("version_number >= 1", name="ck_evaluation_version_positive"),
        CheckConstraint(
            "(version_number = 1 AND supersedes_evaluation_id IS NULL) OR "
            "(version_number > 1 AND supersedes_evaluation_id IS NOT NULL)",
            name="ck_evaluation_version_chain",
        ),
        CheckConstraint(_allowed("decision", EVALUATION_DECISIONS), name="ck_evaluation_decision"),
        Index("ix_evaluation_job_time", "job_posting_id", "evaluated_at"),
        Index("ix_evaluation_job_version", "job_posting_id", "version_number"),
    )

    job_posting_id: Mapped[str] = mapped_column(ForeignKey("job_postings.id"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    supersedes_evaluation_id: Mapped[str | None] = mapped_column(ForeignKey("job_evaluations.id"))
    observation_id: Mapped[str | None] = mapped_column(ForeignKey("posting_observations.id"))
    master_cv_version_id: Mapped[str | None] = mapped_column(ForeignKey("master_cv_versions.id"))
    score_weight_set_id: Mapped[str | None] = mapped_column(ForeignKey("score_weight_sets.id"))
    # Null states that the run's search parameters were not recorded, never that
    # none were in force.
    search_parameter_set_id: Mapped[str | None] = mapped_column(
        ForeignKey("job_search_parameter_sets.id")
    )
    fit_report_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    fit_report_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    decision: Mapped[str] = mapped_column(String(40), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    scope_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    qualifications_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    source_timestamp: Mapped[str | None] = mapped_column(String(80))


class EvaluationCurrentnessReview(IdMixin, AuditMixin, Base):
    """A person's retained decision about one exact currentness snapshot."""

    __tablename__ = "evaluation_currentness_reviews"
    __table_args__ = (
        UniqueConstraint(
            "evaluation_id", "snapshot_sha256", name="uq_evaluation_currentness_review_snapshot"
        ),
        Index("ix_evaluation_currentness_review_evaluation", "evaluation_id", "reviewed_at"),
    )

    evaluation_id: Mapped[str] = mapped_column(ForeignKey("job_evaluations.id"), nullable=False)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    posting_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    posting_status: Mapped[str] = mapped_column(String(24), nullable=False)
    master_cv_version_id: Mapped[str] = mapped_column(String(36), nullable=False)
    search_parameter_set_id: Mapped[str] = mapped_column(String(36), nullable=False)
    reviewed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class EvaluationScore(IdMixin, AuditMixin, Base):
    """One named component score, kept separate from the decision gate."""

    __tablename__ = "evaluation_scores"
    __table_args__ = (
        UniqueConstraint("evaluation_id", "component", name="uq_evaluation_score"),
        CheckConstraint(
            _allowed("component", SCORE_COMPONENTS), name="ck_evaluation_score_component"
        ),
        CheckConstraint(
            "score IS NULL OR score BETWEEN 0 AND 100", name="ck_evaluation_score_range"
        ),
        CheckConstraint(
            "(score IS NOT NULL AND finding_state = 'supported' "
            "AND no_evidence_reason IS NULL) OR "
            "(score IS NULL AND finding_state IN ('not_stated', 'conflict') "
            "AND no_evidence_reason IS NOT NULL)",
            name="ck_evaluation_score_finding",
        ),
        CheckConstraint(
            "standing_source IS NULL OR standing_source IN ('compared', 'extracted')",
            name="ck_evaluation_score_standing_source",
        ),
    )

    evaluation_id: Mapped[str] = mapped_column(ForeignKey("job_evaluations.id"), nullable=False)
    component: Mapped[str] = mapped_column(String(80), nullable=False)
    score: Mapped[int | None] = mapped_column(Integer)
    weight: Mapped[Decimal | None] = mapped_column(Numeric(7, 6))
    finding_state: Mapped[str] = mapped_column(String(24), nullable=False, default="supported")
    no_evidence_reason: Mapped[str | None] = mapped_column(Text)
    standing_source: Mapped[str | None] = mapped_column(String(24))


class MatchEvidence(IdMixin, AuditMixin, Base):
    """Candidate evidence attached to an immutable evaluation."""

    __tablename__ = "match_evidence"
    __table_args__ = (
        CheckConstraint(
            "(char_start IS NULL AND char_end IS NULL) OR "
            "(char_start >= 0 AND char_end > char_start)",
            name="ck_match_evidence_span",
        ),
        CheckConstraint(
            "artifact_id IS NOT NULL AND ("
            "(char_start IS NOT NULL AND char_end IS NOT NULL) OR "
            "(length(trim(evidence_text)) > 0 AND length(trim(locator)) > 0))",
            name="ck_match_evidence_anchor",
        ),
        CheckConstraint(
            "classification IN ('DIRECT', 'TRANSFERABLE', 'GAP', 'TAILORING_ONLY', 'ASSERTED')",
            name="ck_match_evidence_classification",
        ),
        CheckConstraint(
            "strength IS NULL OR strength IN ('strong', 'moderate', 'weak', 'none')",
            name="ck_match_evidence_strength",
        ),
        CheckConstraint(
            "source_kind IN ('posting', 'candidate')",
            name="ck_match_evidence_source_kind",
        ),
        CheckConstraint(
            "(requirement_id IS NULL AND requirement_key IS NULL) OR "
            "(requirement_id IS NOT NULL AND requirement_key IS NOT NULL)",
            name="ck_match_evidence_requirement_identity",
        ),
    )

    evaluation_id: Mapped[str] = mapped_column(ForeignKey("job_evaluations.id"), nullable=False)
    artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    master_cv_entry_id: Mapped[str | None] = mapped_column(String(160))
    # The cited entry's durable identity, absent where the version it cites stores
    # no entries and on evidence quoting the posting.
    master_cv_entry_key: Mapped[str | None] = mapped_column(String(36))
    requirement_id: Mapped[str | None] = mapped_column(String(160))
    # Both identities are absent on evidence anchored to the whole evaluation
    # rather than to one requirement.
    requirement_key: Mapped[str | None] = mapped_column(String(36))
    evidence_text: Mapped[str] = mapped_column(Text, nullable=False)
    char_start: Mapped[int | None] = mapped_column(Integer)
    char_end: Mapped[int | None] = mapped_column(Integer)
    locator: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # No default satisfies the check constraints above.
    source_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    strength: Mapped[str | None] = mapped_column(String(40))
    classification: Mapped[str] = mapped_column(String(80), nullable=False)


class PostingRequirement(IdMixin, AuditMixin, Base):
    """One requirement as a posting evaluation stated it.

    ``requirement_id`` is the positional label the run assigns (``r01``, ``r02``).
    ``requirement_key`` is the identity a citation stores; it is scoped to one
    posting and survives a rewording across re-evaluations of that posting.
    """

    __tablename__ = "posting_requirements"
    __table_args__ = (
        UniqueConstraint(
            "job_evaluation_id", "requirement_id", name="uq_posting_requirement_identity"
        ),
        UniqueConstraint("job_evaluation_id", "requirement_key", name="uq_posting_requirement_key"),
        Index("ix_posting_requirement_posting_key", "job_posting_id", "requirement_key"),
        Index(
            "ix_posting_requirement_gate",
            "job_evaluation_id",
            "is_hard_gate",
            sqlite_where="is_hard_gate = 1",
        ),
        CheckConstraint(
            _allowed("importance", REQUIREMENT_IMPORTANCES),
            name="ck_posting_requirement_importance",
        ),
        CheckConstraint(
            f"gate_kind IS NULL OR {_allowed('gate_kind', GATE_KINDS)}",
            name="ck_posting_requirement_gate_kind",
        ),
    )

    job_posting_id: Mapped[str] = mapped_column(ForeignKey("job_postings.id"), nullable=False)
    job_evaluation_id: Mapped[str] = mapped_column(ForeignKey("job_evaluations.id"), nullable=False)
    requirement_id: Mapped[str] = mapped_column(String(64), nullable=False)
    requirement_key: Mapped[str] = mapped_column(String(36), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    importance: Mapped[RequirementImportance] = mapped_column(String(40), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    is_hard_gate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    gate_kind: Mapped[GateKind | None] = mapped_column(String(80), nullable=True)
    gate_quote: Mapped[str | None] = mapped_column(Text, nullable=True)
    gate_blocking: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class EvaluationScoreEvidence(IdMixin, AuditMixin, Base):
    """Explicit evidence relationship to one immutable component score."""

    __tablename__ = "evaluation_score_evidence"
    __table_args__ = (
        UniqueConstraint("evaluation_score_id", "match_evidence_id", name="uq_score_evidence"),
        CheckConstraint(
            "direction IN ('supports', 'contradicts', 'limits')",
            name="ck_score_evidence_direction",
        ),
    )

    evaluation_score_id: Mapped[str] = mapped_column(
        ForeignKey("evaluation_scores.id"), nullable=False
    )
    match_evidence_id: Mapped[str] = mapped_column(ForeignKey("match_evidence.id"), nullable=False)
    direction: Mapped[str] = mapped_column(String(20), nullable=False, default="supports")


class EvaluationFactorEvidence(IdMixin, AuditMixin, Base):
    """Explicit evidence supporting or limiting an evaluation factor."""

    __tablename__ = "evaluation_factor_evidence"
    __table_args__ = (
        UniqueConstraint("evaluation_factor_id", "match_evidence_id", name="uq_factor_evidence"),
        CheckConstraint(
            "direction IN ('supports', 'contradicts', 'limits')",
            name="ck_factor_evidence_direction",
        ),
    )

    evaluation_factor_id: Mapped[str] = mapped_column(
        ForeignKey("evaluation_factors.id"), nullable=False
    )
    match_evidence_id: Mapped[str] = mapped_column(ForeignKey("match_evidence.id"), nullable=False)
    direction: Mapped[str] = mapped_column(String(20), nullable=False, default="supports")


class EvaluationFactor(IdMixin, AuditMixin, Base):
    """One pro, con, gap, or recruiter risk in document order."""

    __tablename__ = "evaluation_factors"
    __table_args__ = (
        CheckConstraint("factor_type IN ('pro', 'con', 'gap', 'risk')", name="ck_factor_type"),
        UniqueConstraint("evaluation_id", "factor_type", "position", name="uq_factor_position"),
    )

    evaluation_id: Mapped[str] = mapped_column(ForeignKey("job_evaluations.id"), nullable=False)
    factor_type: Mapped[str] = mapped_column(String(20), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)


class CompensationComponent(IdMixin, AuditMixin, Base):
    """Original-currency compensation component without false equivalence."""

    __tablename__ = "compensation_components"
    __table_args__ = (
        CheckConstraint(
            "length(currency) = 3 AND currency NOT GLOB '*[^A-Z]*'",
            name="ck_compensation_currency",
        ),
        CheckConstraint(_allowed("basis", BASES), name="ck_compensation_basis"),
    )

    job_posting_id: Mapped[str] = mapped_column(ForeignKey("job_postings.id"), nullable=False)
    observation_id: Mapped[str | None] = mapped_column(ForeignKey("posting_observations.id"))
    component_type: Mapped[str] = mapped_column(String(80), nullable=False)
    amount_min: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    amount_max: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    annualized_min: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    annualized_max: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    basis: Mapped[str] = mapped_column(String(12), nullable=False)
    geography: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")


class EligibilityRecord(IdMixin, AuditMixin, Base):
    """Geographic and employment eligibility as observed, never inferred later."""

    __tablename__ = "eligibility_records"

    job_posting_id: Mapped[str] = mapped_column(ForeignKey("job_postings.id"), nullable=False)
    observation_id: Mapped[str | None] = mapped_column(ForeignKey("posting_observations.id"))
    workspace_location_key: Mapped[str] = mapped_column(String(80), nullable=False)
    region_rule: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    arrangement: Mapped[str] = mapped_column(Text, nullable=False, default="")
    details: Mapped[str] = mapped_column(Text, nullable=False, default="")


class Application(IdMixin, AuditMixin, SoftArchiveMixin, Base):
    """Stable application identity whose status is derived from events."""

    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("workspace_id", "job_posting_id", name="uq_application_job"),
    )

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    job_posting_id: Mapped[str] = mapped_column(ForeignKey("job_postings.id"), nullable=False)


class ApplicationEvent(IdMixin, AuditMixin, Base):
    """Append-only state change, correction, or reversal."""

    __tablename__ = "application_events"
    __table_args__ = (
        CheckConstraint(_allowed("status", APPLICATION_STATUSES), name="ck_application_status"),
        CheckConstraint("event_type IN ('status', 'correction', 'reversal')", name="ck_event_type"),
        UniqueConstraint("application_id", "idempotency_key", name="uq_application_event_key"),
        Index("ix_application_event_time", "application_id", "occurred_at", "created_at"),
    )

    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    event_type: Mapped[str] = mapped_column(String(24), nullable=False, default="status")
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    source_timestamp: Mapped[str | None] = mapped_column(String(80))
    corrects_event_id: Mapped[str | None] = mapped_column(ForeignKey("application_events.id"))
    reverses_event_id: Mapped[str | None] = mapped_column(ForeignKey("application_events.id"))
    artifact_uri: Mapped[str] = mapped_column(Text, nullable=False, default="")
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
