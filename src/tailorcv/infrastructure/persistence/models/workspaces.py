"""Workspace persistence and versioned decision inputs."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
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

from tailorcv.infrastructure.persistence.models.base import (
    AuditMixin,
    Base,
    IdMixin,
    SoftArchiveMixin,
)
from tailorcv.infrastructure.persistence.types import UTCDateTime


class Workspace(IdMixin, AuditMixin, SoftArchiveMixin, Base):
    """One local job-search project and its allowed storage boundary."""

    __tablename__ = "workspaces"
    __table_args__ = (UniqueConstraint("slug", name="uq_workspaces_slug"),)

    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    root_path: Mapped[str | None] = mapped_column(Text)
    settings_seed: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default="{}"
    )
    scan_schedule: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))


class CandidateProfile(IdMixin, AuditMixin, SoftArchiveMixin, Base):
    """Mutable candidate profile metadata; CV evidence remains versioned."""

    __tablename__ = "candidate_profiles"
    __table_args__ = (UniqueConstraint("workspace_id", name="uq_candidate_workspace"),)

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    display_name: Mapped[str] = mapped_column(String(240), nullable=False)


class MasterCvVersion(IdMixin, AuditMixin, Base):
    """Immutable identity and hash of one master-CV version."""

    __tablename__ = "master_cv_versions"
    __table_args__ = (
        UniqueConstraint("candidate_profile_id", "content_sha256", name="uq_cv_content"),
    )

    candidate_profile_id: Mapped[str] = mapped_column(
        ForeignKey("candidate_profiles.id"), nullable=False
    )
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_uri: Mapped[str] = mapped_column(Text, nullable=False)
    effective_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    byte_size: Mapped[int | None] = mapped_column(Integer)


class MasterCvEntry(IdMixin, AuditMixin, Base):
    """One citable entry as one master-CV version stated it.

    ``entry_id`` is the parser's content address over the entry's scope and text.
    ``entry_key`` is the identity a citation stores, and it survives a rewording.
    """

    __tablename__ = "master_cv_entries"
    __table_args__ = (
        UniqueConstraint("master_cv_version_id", "entry_id", name="uq_cv_entry_identity"),
        UniqueConstraint("master_cv_version_id", "entry_key", name="uq_cv_entry_key"),
        Index("ix_cv_entry_version_position", "master_cv_version_id", "position"),
        Index("ix_cv_entry_key", "entry_key"),
    )

    master_cv_version_id: Mapped[str] = mapped_column(
        ForeignKey("master_cv_versions.id"), nullable=False
    )
    entry_id: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_key: Mapped[str] = mapped_column(String(36), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    section: Mapped[str] = mapped_column(Text, nullable=False)
    role_key: Mapped[str | None] = mapped_column(Text)
    group: Mapped[str | None] = mapped_column(Text)
    label: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class ScoreWeightSet(IdMixin, AuditMixin, Base):
    """Immutable named scoring policy retained with every evaluation that used it."""

    __tablename__ = "score_weight_sets"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", "version", name="uq_score_weight_version"),
        UniqueConstraint("content_sha256", name="uq_score_weight_digest"),
    )

    workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspaces.id"))
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class JobSearchParameterSet(IdMixin, AuditMixin, Base):
    """One immutable version of a workspace's search parameters.

    Versioned by supersession, so a decision stays auditable against the
    parameters in force when it was made. The parameters are one JSON document,
    read and written whole and validated by one pydantic model.
    """

    __tablename__ = "job_search_parameter_sets"
    __table_args__ = (
        UniqueConstraint("workspace_id", "version", name="uq_job_search_parameter_version"),
        UniqueConstraint("workspace_id", "id", name="uq_job_search_parameter_workspace_id"),
    )

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    supersedes_id: Mapped[str | None] = mapped_column(
        ForeignKey("job_search_parameter_sets.id"), nullable=True
    )
    document: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class GuardrailVersion(IdMixin, AuditMixin, Base):
    """One immutable version of a workspace's anti-fabrication rules."""

    __tablename__ = "guardrail_versions"
    __table_args__ = (UniqueConstraint("workspace_id", "version", name="uq_guardrail_version"),)

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    supersedes_id: Mapped[str | None] = mapped_column(
        ForeignKey("guardrail_versions.id"), nullable=True
    )
    document: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class LearningLoopPolicyVersion(IdMixin, AuditMixin, Base):
    """One immutable version of a workspace's learning-loop policy."""

    __tablename__ = "learning_loop_policy_versions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "version", name="uq_learning_loop_policy_version"),
    )

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    supersedes_id: Mapped[str | None] = mapped_column(
        ForeignKey("learning_loop_policy_versions.id"), nullable=True
    )
    document: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ScoreWeightComponent(IdMixin, AuditMixin, Base):
    """One immutable component weight belonging to a versioned score policy."""

    __tablename__ = "score_weight_components"
    __table_args__ = (
        UniqueConstraint("weight_set_id", "component", name="uq_score_weight_component"),
        CheckConstraint("weight >= 0 AND weight <= 1", name="ck_score_weight_range"),
    )

    weight_set_id: Mapped[str] = mapped_column(ForeignKey("score_weight_sets.id"), nullable=False)
    component: Mapped[str] = mapped_column(String(80), nullable=False)
    weight: Mapped[Decimal] = mapped_column(Numeric(7, 6), nullable=False)


class PromptTemplate(IdMixin, AuditMixin, SoftArchiveMixin, Base):
    """Mutable identity for a prompt whose versions are immutable.

    One row per prompt a workspace has actually edited. A prompt nobody has
    touched has no row and resolves to the file that ships with the package.
    Archiving the row restores that file while keeping version history.
    """

    __tablename__ = "prompt_templates"
    __table_args__ = (UniqueConstraint("workspace_id", "key", name="uq_prompt_template"),)

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    key: Mapped[str] = mapped_column(String(160), nullable=False)


class PromptVersion(IdMixin, AuditMixin, Base):
    """Immutable whole-file snapshot of one overridden prompt.

    The two digest columns carry a ``sha256:`` prefix where every other digest
    column in this package is bare hex. ``base_digest`` is the packaged text
    this copy was forked from.
    """

    __tablename__ = "prompt_versions"
    __table_args__ = (
        UniqueConstraint("prompt_template_id", "version_number", name="uq_prompt_version"),
        # Reverts append a version with repeated content and remain queryable.
        Index("ix_prompt_version_digest", "prompt_template_id", "content_digest"),
        CheckConstraint("version_number >= 1", name="ck_prompt_version_positive"),
    )

    prompt_template_id: Mapped[str] = mapped_column(
        ForeignKey("prompt_templates.id"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    base_digest: Mapped[str] = mapped_column(String(80), nullable=False)


class RequirementRuling(IdMixin, AuditMixin, Base):
    """A workspace's judgment on one posting requirement.

    Mutable state: setting a ruling replaces the row and writes an
    ``audit_events`` entry, so the current state is one row and the history is
    recoverable through the audit log. The ruling is keyed by
    ``requirement_key``, the durable identity that carries across
    re-evaluations of one posting, so a ruling survives a supersession.
    """

    __tablename__ = "requirement_rulings"
    __table_args__ = (
        UniqueConstraint("workspace_id", "requirement_key", name="uq_workspace_requirement_ruling"),
        CheckConstraint(
            "ruling IN "
            "('met', 'not_applicable', 'disputed_fit', 'disputed_extraction', 'reversed')",
            name="ck_requirement_ruling_vocab",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    requirement_key: Mapped[str] = mapped_column(String(36), nullable=False)
    ruling: Mapped[str] = mapped_column(String(24), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
