"""Worker presence and atomic task lease lifecycle."""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import ColumnElement, bindparam, case, func, or_, select, update
from sqlalchemy.orm import InstrumentedAttribute, Session, aliased

from clearscout.application.failures import ConflictError, NotFoundError, ValidationError
from clearscout.application.tasks.events import TaskEventLog
from clearscout.application.tasks.models import ClaimedTask, TaskResponse
from clearscout.application.tasks.records import claimed_task_response, task_response
from clearscout.domain import TaskStatus, utc_now
from clearscout.domain.identity import url_host
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    BackgroundTask,
    Source,
    SourceScan,
    SourceScanContinuation,
    SourceScanMember,
    WorkerPresence,
)

DEFAULT_TASK_LEASE_SECONDS = 60
CONTINUATION_YIELD_LIMIT = timedelta(minutes=5)
CONTINUATION_TASK_TYPE = "source.continue"

# "continuation" claims only board continuations; "other" claims everything else.
ClaimScope = Literal["any", "continuation", "other"]


@dataclass(frozen=True)
class LaneClaim:
    """A leased task and, for a board continuation, the host its board is read from."""

    task: ClaimedTask
    host: str | None


def _board_host(url: str | None) -> str | None:
    """Return the host key the fetch client locks for this URL, or None without one."""
    return url_host(url or "") or None


class TaskLeases:
    """Coordinate one worker and its claimed task leases."""

    def __init__(self, database: Database, events: TaskEventLog) -> None:
        """Use the supplied transaction boundary and event writer."""
        self.database = database
        self.events = events

    def renew_worker_presence(self, *, worker_id: str, ttl_seconds: int = 120) -> None:
        """Make one polling worker visible until its next expected heartbeat."""
        worker_id = worker_id.strip()
        if not worker_id or ttl_seconds < 10 or ttl_seconds > 3600:
            raise ValidationError("Worker ID and a 10-3600 second presence are required.")
        now = utc_now()
        with self.database.exclusive_session() as session:
            self._only_worker(session, worker_id, now)
            presence = session.get(WorkerPresence, worker_id)
            if presence is None:
                session.add(
                    WorkerPresence(
                        worker_id=worker_id,
                        heartbeat_at=now,
                        available_until=now + timedelta(seconds=ttl_seconds),
                    )
                )
            else:
                presence.heartbeat_at = now
                presence.available_until = now + timedelta(seconds=ttl_seconds)

    def withdraw_worker_presence(self, *, worker_id: str) -> None:
        """Stop advertising a worker that is shutting down normally."""
        worker_id = worker_id.strip()
        if not worker_id:
            raise ValidationError("Worker ID is required.")
        with self.database.session() as session:
            presence = session.get(WorkerPresence, worker_id)
            if presence is not None:
                session.delete(presence)

    def worker_available(self) -> bool:
        """Return whether any worker heartbeat has not expired."""
        now = utc_now()
        with self.database.session() as session:
            worker_id = session.scalar(
                select(WorkerPresence.worker_id)
                .where(WorkerPresence.available_until >= now)
                .limit(1)
            )
            return worker_id is not None

    def claim_next(
        self, *, worker_id: str, lease_seconds: int = DEFAULT_TASK_LEASE_SECONDS
    ) -> ClaimedTask | None:
        """Lease queued or abandoned work to one worker."""
        claimed = self.claim_lane(worker_id=worker_id, lease_seconds=lease_seconds)
        return None if claimed is None else claimed.task

    def claim_lane(
        self,
        *,
        worker_id: str,
        lease_seconds: int = DEFAULT_TASK_LEASE_SECONDS,
        scope: ClaimScope = "any",
        busy_hosts: AbstractSet[str] = frozenset(),
    ) -> LaneClaim | None:
        """Lease the oldest task in scope, skipping continuations whose board host is busy."""
        if not worker_id.strip() or lease_seconds < 10 or lease_seconds > 3600:
            raise ValidationError("Worker ID and a 10-3600 second lease are required.")
        now = utc_now()
        with self.database.session() as session:
            self._only_worker(session, worker_id.strip(), now)
            found = self._claimable(session, now, scope=scope, busy_hosts=busy_hosts)
            if found is None:
                return None
            task, host = found
            # A cancel-requested running task is still recovered here, so it
            # spends an attempt increment; no attempt cap reads that count.
            recovered = task.status == TaskStatus.RUNNING.value
            taken = session.connection().execute(
                update(BackgroundTask)
                .where(
                    BackgroundTask.id == task.id,
                    # The row loads after the candidate query, so it may no longer be claimable.
                    TaskLeases._claim_condition(now),
                    BackgroundTask.status == task.status,
                    BackgroundTask.attempt_count == task.attempt_count,
                    BackgroundTask.leased_by == task.leased_by,
                    BackgroundTask.lease_expires_at == task.lease_expires_at,
                    BackgroundTask.cancellation_requested_at == task.cancellation_requested_at,
                )
                .values(
                    status=TaskStatus.RUNNING.value,
                    attempt_count=task.attempt_count + 1,
                    leased_by=worker_id.strip(),
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    heartbeat_at=now,
                    started_at=task.started_at or now,
                    updated_at=now,
                )
            )
            if taken.rowcount != 1:
                return None
            session.refresh(task)
            self.events.append(
                session,
                task,
                stage="running",
                event_type="task.recovered" if recovered else "task.started",
                payload={"attempt": task.attempt_count},
                actor=worker_id,
                origin="worker",
                correlation_id=task.correlation_id,
            )
            return LaneClaim(ClaimedTask.model_validate(claimed_task_response(task)), host)

    def heartbeat(
        self,
        task_id: str,
        *,
        worker_id: str,
        expected_attempt: int | None = None,
        progress: dict[str, Any] | None = None,
        stage: str = "running",
        lease_seconds: int = DEFAULT_TASK_LEASE_SECONDS,
    ) -> ClaimedTask:
        """Extend a live lease and optionally append visible progress."""
        with self.database.session() as session:
            task = self._owned_running(session, task_id, worker_id)
            now = utc_now()
            values: dict[str, Any] = {
                "heartbeat_at": now,
                "lease_expires_at": now + timedelta(seconds=lease_seconds),
                "updated_at": now,
            }
            if progress is not None:
                values["progress"] = progress
            changed = session.connection().execute(
                update(BackgroundTask)
                .where(
                    BackgroundTask.id == task_id,
                    BackgroundTask.status == TaskStatus.RUNNING.value,
                    BackgroundTask.leased_by == worker_id,
                    BackgroundTask.attempt_count
                    == (task.attempt_count if expected_attempt is None else expected_attempt),
                )
                .values(**values)
            )
            if changed.rowcount != 1:
                raise ConflictError("The worker no longer owns this task lease.")
            session.refresh(task)
            if progress is not None:
                self.events.append(
                    session,
                    task,
                    stage=stage,
                    event_type="task.progress",
                    payload=progress,
                    actor=worker_id,
                    origin="worker",
                    correlation_id=task.correlation_id,
                )
            return ClaimedTask.model_validate(claimed_task_response(task))

    def finish(
        self,
        task_id: str,
        *,
        worker_id: str,
        expected_attempt: int | None = None,
        status: TaskStatus,
        progress: dict[str, Any] | None = None,
        error_category: str | None = None,
        error_message: str | None = None,
    ) -> TaskResponse:
        """Finish owned work as completed, failed, or cooperatively canceled."""
        allowed = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
        if status not in allowed:
            raise ValidationError("A worker may finish only completed, failed, or cancelled.")
        with self.database.session() as session:
            task = self._owned_running(session, task_id, worker_id)
            now = utc_now()
            cancelled = BackgroundTask.cancellation_requested_at.is_not(None)
            changed = session.connection().execute(
                update(BackgroundTask)
                .where(
                    BackgroundTask.id == task_id,
                    BackgroundTask.status == TaskStatus.RUNNING.value,
                    BackgroundTask.leased_by == worker_id,
                    BackgroundTask.attempt_count
                    == (task.attempt_count if expected_attempt is None else expected_attempt),
                )
                .values(
                    status=case((cancelled, TaskStatus.CANCELLED.value), else_=status.value),
                    progress=case(
                        (cancelled, BackgroundTask.progress),
                        else_=bindparam(
                            "terminal_progress",
                            progress or task.progress,
                            type_=BackgroundTask.__table__.c.progress.type,
                        ),
                    ),
                    error_category=case((cancelled, None), else_=error_category),
                    error_message=case((cancelled, None), else_=error_message),
                    completed_at=now,
                    updated_at=now,
                    leased_by=None,
                    lease_expires_at=None,
                )
            )
            if changed.rowcount != 1:
                raise ConflictError("The worker no longer owns this task lease.")
            session.refresh(task)
            self.events.append(
                session,
                task,
                stage=task.status,
                event_type=f"task.{task.status}",
                payload={"error_category": task.error_category},
                actor=worker_id,
                origin="worker",
                correlation_id=task.correlation_id,
            )
            return task_response(task)

    @staticmethod
    def _only_worker(session: Session, worker_id: str, now: datetime) -> None:
        """Refuse a worker while another one is serving this database."""
        other = session.scalar(
            select(WorkerPresence)
            .where(
                WorkerPresence.worker_id != worker_id,
                WorkerPresence.available_until >= now,
            )
            .limit(1)
        )
        if other is not None:
            raise ConflictError(
                f"Worker {other.worker_id!r} is already serving this database until "
                f"{other.available_until.isoformat()}. ClearScout supports one worker "
                "per database; stop that worker, or wait for its presence to expire."
            )

    @staticmethod
    def _claimable(
        session: Session, now: datetime, *, scope: ClaimScope, busy_hosts: AbstractSet[str]
    ) -> tuple[BackgroundTask, str | None] | None:
        """Return the first task in claim order that the scope and busy hosts admit."""
        continuation = BackgroundTask.task_type == CONTINUATION_TASK_TYPE
        statement = (
            select(BackgroundTask.id, Source.resolved_url, Source.url)
            .outerjoin(
                SourceScanContinuation,
                continuation
                & (
                    BackgroundTask.payload["continuation_id"].as_string()
                    == SourceScanContinuation.id
                ),
            )
            .outerjoin(SourceScan, SourceScan.id == SourceScanContinuation.source_scan_id)
            .outerjoin(
                SourceScanMember,
                SourceScanMember.id == SourceScanContinuation.batch_member_id,
            )
            .outerjoin(Source, Source.id == SourceScan.source_id)
            .where(TaskLeases._claim_condition(now))
            .where(SourceScanMember.active_task_id.is_(None))
            .order_by(*TaskLeases._claim_order(now))
        )
        if scope == "continuation":
            statement = statement.where(continuation)
        elif scope == "other":
            statement = statement.where(~continuation)
        # The join matches only continuations, so any other task reads an empty address.
        address = func.lower(func.coalesce(func.nullif(Source.resolved_url, ""), Source.url, ""))
        prefiltered = statement
        for busy in busy_hosts:
            prefiltered = prefiltered.where(~TaskLeases._names_host(address, busy))
        for _ in range(len(busy_hosts) + 1):
            found = session.execute(prefiltered.limit(1)).first()
            if found is None:
                break
            task_id, resolved_url, url = found
            host = _board_host(resolved_url or url)
            if host not in busy_hosts:
                return TaskLeases._loaded(session, task_id), host
            prefiltered = prefiltered.where(BackgroundTask.id != task_id)
        # The SQL match only narrows: a busy host named in a query string, or an
        # authority it cannot read, would hide a free task, so every candidate is read here.
        for task_id, resolved_url, url in session.execute(statement):
            host = _board_host(resolved_url or url)
            if host not in busy_hosts:
                return TaskLeases._loaded(session, task_id), host
        return None

    @staticmethod
    def _loaded(session: Session, task_id: str) -> BackgroundTask:
        """Load the one task a claim chose from its id-only candidate rows."""
        task = session.get(BackgroundTask, task_id)
        if task is None:
            raise NotFoundError(f"Task {task_id!r} does not exist.")
        return task

    @staticmethod
    def _names_host(address: ColumnElement[str], host: str) -> ColumnElement[bool]:
        """Match a lowercased URL whose authority is `host`, with or without userinfo or port."""
        matches = [
            address.contains(f"{lead}{host}{tail}", autoescape=True)
            for lead in ("://", "@")
            for tail in ("/", ":", "?", "#")
        ]
        matches += [address.endswith(f"{lead}{host}", autoescape=True) for lead in ("://", "@")]
        return or_(*matches)

    @staticmethod
    def _claim_condition(now: datetime) -> ColumnElement[bool]:
        """Match queued work and running work whose lease has expired."""
        return or_(
            (
                (BackgroundTask.status == TaskStatus.QUEUED.value)
                & BackgroundTask.cancellation_requested_at.is_(None)
            ),
            (
                (BackgroundTask.status == TaskStatus.RUNNING.value)
                & (BackgroundTask.lease_expires_at < now)
            ),
        )

    @staticmethod
    def _claim_order(
        now: datetime,
    ) -> tuple[ColumnElement[Any], InstrumentedAttribute[datetime], InstrumentedAttribute[str]]:
        """Order the oldest user task first, letting publisher continuation wait only so long."""
        # A continuation yields to other work unless it has waited CONTINUATION_YIELD_LIMIT
        # and no continuation started within that window, so one aged chunk runs per window.
        window_start = now - CONTINUATION_YIELD_LIMIT
        earlier = aliased(BackgroundTask)
        recent_continuation = (
            select(earlier.id)
            .where(
                earlier.task_type == CONTINUATION_TASK_TYPE,
                earlier.started_at >= window_start,
            )
            .exists()
        )
        yielding = (BackgroundTask.task_type == CONTINUATION_TASK_TYPE) & (
            (BackgroundTask.created_at > window_start) | recent_continuation
        )
        return (case((yielding, 1), else_=0), BackgroundTask.created_at, BackgroundTask.id)

    @staticmethod
    def _owned_running(session: Session, task_id: str, worker_id: str) -> BackgroundTask:
        task = session.get(BackgroundTask, task_id)
        if task is None:
            raise NotFoundError(f"Task {task_id!r} does not exist.")
        if task.status != TaskStatus.RUNNING.value or task.leased_by != worker_id:
            raise ConflictError("The worker no longer owns this task lease.")
        return task
