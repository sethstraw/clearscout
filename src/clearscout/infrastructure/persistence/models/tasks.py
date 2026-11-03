"""The durable work queue one local worker leases from, and its progress.

Batch runs sit here rather than with tailoring because a batch is a unit of
queued work over many postings, not a drafting run over one.
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

from clearscout.domain import STORED_TASK_TYPES, TASK_STATUSES, TaskType, utc_now
from clearscout.infrastructure.persistence.models.base import AuditMixin, Base, IdMixin, _allowed
from clearscout.infrastructure.persistence.types import UTCDateTime


class BackgroundTask(IdMixin, AuditMixin, Base):
    """Persistent queue record leased by one local worker."""

    __tablename__ = "background_tasks"
    __table_args__ = (
        CheckConstraint(_allowed("status", TASK_STATUSES), name="ck_task_status"),
        CheckConstraint(_allowed("task_type", STORED_TASK_TYPES), name="ck_task_type"),
        UniqueConstraint("workspace_id", "task_type", "idempotency_key", name="uq_task_key"),
        UniqueConstraint("workspace_id", "id", name="uq_task_workspace_id"),
        Index("ix_task_claim", "status", "lease_expires_at", "created_at"),
        Index("ix_task_workspace_type_status", "workspace_id", "task_type", "status"),
        Index("ix_task_workspace_type_correlation", "workspace_id", "task_type", "correlation_id"),
        Index("ix_task_type_started", "task_type", "started_at"),
    )

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    task_type: Mapped[TaskType] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="queued")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    leased_by: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    progress: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    error_category: Mapped[str | None] = mapped_column(String(120))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class WorkerPresence(Base):
    """Expiring proof that a local background worker is polling."""

    __tablename__ = "worker_presence"

    worker_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    heartbeat_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    available_until: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class TaskEvent(IdMixin, AuditMixin, Base):
    """Append-only task progress streamed over SSE or polled."""

    __tablename__ = "task_events"
    __table_args__ = (UniqueConstraint("task_id", "sequence", name="uq_task_event"),)

    task_id: Mapped[str] = mapped_column(ForeignKey("background_tasks.id"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(String(120), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class BatchRun(IdMixin, AuditMixin, Base):
    """Immutable comparison of independently screened postings."""

    __tablename__ = "batch_runs"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("background_tasks.id"))
    result_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    result_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class BatchMember(IdMixin, AuditMixin, Base):
    """One job's place and user priority in an immutable comparison."""

    __tablename__ = "batch_members"
    __table_args__ = (UniqueConstraint("batch_run_id", "job_posting_id", name="uq_batch_member"),)

    batch_run_id: Mapped[str] = mapped_column(ForeignKey("batch_runs.id"), nullable=False)
    job_posting_id: Mapped[str] = mapped_column(ForeignKey("job_postings.id"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    priority: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
