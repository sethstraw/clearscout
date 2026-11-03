"""Where every citable byte came from, and what happened to it.

An artifact row is metadata only; the bytes live in runtime storage. Both
tables here exist so a claim can be traced back to a file and a file back to
the write that produced it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from clearscout.domain import (
    ARTIFACT_AUTHOR_TYPES,
    ARTIFACT_INTEGRITY_STATUSES,
    ARTIFACT_LOGICAL_TYPES,
    ArtifactAuthorType,
    ArtifactIntegrityStatus,
    ArtifactLogicalType,
)
from clearscout.infrastructure.persistence.models.base import (
    AuditMixin,
    Base,
    IdMixin,
    SoftArchiveMixin,
    _allowed,
)
from clearscout.infrastructure.persistence.types import UTCDateTime


class Artifact(IdMixin, AuditMixin, SoftArchiveMixin, Base):
    """Metadata for a stored file; never the file blob."""

    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "storage_uri", "sha256", name="uq_artifact_version"),
        UniqueConstraint(
            "workspace_id",
            "artifact_family_id",
            "version_number",
            name="uq_artifact_family_version",
        ),
        Index("ix_artifact_type", "workspace_id", "logical_type"),
        Index("ix_artifact_family", "workspace_id", "artifact_family_id", "version_number"),
        CheckConstraint(
            _allowed("logical_type", ARTIFACT_LOGICAL_TYPES), name="ck_artifact_logical_type"
        ),
        CheckConstraint(
            _allowed("author_type", ARTIFACT_AUTHOR_TYPES), name="ck_artifact_author_type"
        ),
        CheckConstraint(
            _allowed("integrity_status", ARTIFACT_INTEGRITY_STATUSES),
            name="ck_artifact_integrity_status",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    logical_type: Mapped[ArtifactLogicalType] = mapped_column(String(100), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(160), nullable=False)
    storage_uri: Mapped[str] = mapped_column(Text, nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    related_entity_type: Mapped[str | None] = mapped_column(String(80))
    related_entity_id: Mapped[str | None] = mapped_column(String(36))
    tailoring_run_id: Mapped[str | None] = mapped_column(ForeignKey("tailoring_runs.id"))
    artifact_family_id: Mapped[str | None] = mapped_column(String(36))
    version_number: Mapped[int | None] = mapped_column(Integer)
    supersedes_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    author_type: Mapped[ArtifactAuthorType] = mapped_column(
        String(24), nullable=False, default="system"
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    integrity_status: Mapped[ArtifactIntegrityStatus] = mapped_column(
        String(40), nullable=False, default="unverified"
    )


class AuditEvent(IdMixin, AuditMixin, Base):
    """Append-only record of important writes and import decisions."""

    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_workspace_time", "workspace_id", "created_at"),)

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(36))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
