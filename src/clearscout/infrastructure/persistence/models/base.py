"""The one declarative base and the column mixins every table shares."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from clearscout.domain import new_id, utc_now
from clearscout.infrastructure.persistence.types import UTCDateTime


def _allowed(column: str, values: tuple[str, ...]) -> str:
    """Return a deterministic SQL check expression for a closed vocabulary."""
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


class Base(DeclarativeBase):
    """Declarative base for every migration-managed table."""


class IdMixin:
    """Collision-safe identifier used for every new durable record."""

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)


class AuditMixin:
    """Write provenance present on mutable and immutable records alike."""

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="local_user")
    origin: Mapped[str] = mapped_column(String(120), nullable=False, default="clearscout")
    correlation_id: Mapped[str | None] = mapped_column(String(120))


class SoftArchiveMixin:
    """Recoverable removal for legitimately mutable records."""

    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
