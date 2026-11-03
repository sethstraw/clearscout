"""What the user writes alongside the ledger, and which is never evidence.

Notes, tags, and saved views are ordinary mutable metadata. Nothing here may be
cited by an evaluation or a draft; that is the whole reason it lives apart from
the artifact store.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from clearscout.domain import utc_now
from clearscout.infrastructure.persistence.models.base import (
    AuditMixin,
    Base,
    IdMixin,
    SoftArchiveMixin,
)
from clearscout.infrastructure.persistence.types import UTCDateTime


class Note(IdMixin, AuditMixin, SoftArchiveMixin, Base):
    """Mutable user-authored note with ordinary CRUD and archiving."""

    __tablename__ = "notes"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(36))
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )


class Tag(IdMixin, AuditMixin, SoftArchiveMixin, Base):
    """Mutable workspace tag."""

    __tablename__ = "tags"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_tag_name"),)

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    color: Mapped[str] = mapped_column(String(32), nullable=False, default="")


class TagAssignment(IdMixin, AuditMixin, Base):
    """Tag on mutable metadata, never a bulk edit of an evaluation."""

    __tablename__ = "tag_assignments"
    __table_args__ = (
        UniqueConstraint("tag_id", "entity_type", "entity_id", name="uq_tag_assignment"),
    )

    tag_id: Mapped[str] = mapped_column(ForeignKey("tags.id"), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False)


class SavedView(IdMixin, AuditMixin, SoftArchiveMixin, Base):
    """User-editable filter, sort, and column configuration."""

    __tablename__ = "saved_views"
    __table_args__ = (UniqueConstraint("workspace_id", "resource", "name", name="uq_saved_view"),)

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    resource: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
