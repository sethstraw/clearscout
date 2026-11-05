"""Commands that create, cancel, and retry durable tasks."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from clearscout.application.failures import (
    FREE_MODE_REASON,
    ConflictError,
    FreeModeError,
    NotFoundError,
    UnavailableError,
    ValidationError,
)
from clearscout.application.tasks.events import TaskEventLog
from clearscout.application.tasks.leases import drop_member_board_results
from clearscout.application.tasks.models import TaskResponse
from clearscout.application.tasks.payloads import SourceScanPayload
from clearscout.application.tasks.records import (
    payload_digest,
    task_pending_discovery_ids,
    task_response,
)
from clearscout.application.tasks.registry import task_is_paid
from clearscout.application.workspaces.parameters import parameters_in
from clearscout.domain import TASK_TYPES, TaskStatus, new_id, utc_now
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    Artifact,
    BackgroundTask,
    Employer,
    JobPosting,
    PendingDiscovery,
    Source,
    SourceScan,
    SourceScanContinuation,
    SourceScanFrontierItem,
    Workspace,
)
from clearscout.infrastructure.persistence.models.discovery import SourceScanMember

_TASK_TYPES = frozenset(TASK_TYPES)
_TERMINAL_STATUSES = frozenset(
    {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}
)


def _require_active_pending_targets(
    session: Session, workspace_id: str, task_type: str, payload: dict[str, Any]
) -> None:
    pending_ids = task_pending_discovery_ids(task_type, payload)
    if not pending_ids:
        return
    active_pending_ids = set(
        session.scalars(
            select(PendingDiscovery.id).where(
                PendingDiscovery.workspace_id == workspace_id,
                PendingDiscovery.id.in_(pending_ids),
                PendingDiscovery.archived_at.is_(None),
            )
        )
    )
    if any(pending_id not in active_pending_ids for pending_id in pending_ids):
        raise NotFoundError("A selected pending discovery no longer exists in the active queue.")


def _link_render_job(
    session: Session, workspace_id: str, task_type: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Name the job a rendered document is filed under, so the job's work lists it."""
    artifact_id = payload.get("artifact_id")
    if task_type != "document.render" or not isinstance(artifact_id, str):
        return payload
    artifact = session.get(Artifact, artifact_id)
    filed_under = (
        artifact.related_entity_id
        if artifact is not None
        and artifact.workspace_id == workspace_id
        and artifact.related_entity_type == "job_posting"
        else None
    )
    named = payload.get("job_posting_id")
    if named is not None and named != filed_under:
        raise ValidationError("This document is not filed under the job the render names.")
    return payload if filed_under is None else {**payload, "job_posting_id": filed_under}


class TaskCommands:
    """Change task lifecycle state and append the corresponding events."""

    def __init__(
        self,
        database: Database,
        events: TaskEventLog,
        *,
        reader_alive: Callable[[int, str], bool | None] | None = None,
        paid_work_ready: Callable[[], bool] | None = None,
    ) -> None:
        """Use the supplied transaction boundary and event writer.

        With ``paid_work_ready`` set, a paid task type is refused while it answers False.
        """
        self.database = database
        self.events = events
        self.reader_alive = reader_alive
        self.paid_work_ready = paid_work_ready

    def enqueue(
        self,
        *,
        workspace_id: str,
        task_type: str,
        payload: dict[str, Any],
        idempotency_key: str,
        actor: str = "local_user",
        origin: str = "api",
        correlation_id: str | None = None,
    ) -> TaskResponse:
        """Create one queued task or replay the identical existing request."""
        self._refuse_paid_work_without_key(task_type)
        with self.database.exclusive_session() as session:
            return self.enqueue_in_session(
                session,
                workspace_id=workspace_id,
                task_type=task_type,
                payload=payload,
                idempotency_key=idempotency_key,
                actor=actor,
                origin=origin,
                correlation_id=correlation_id,
            )

    def _refuse_paid_work_without_key(self, task_type: str) -> None:
        # Runs before any session opens, so the key lookup never holds the write lock.
        if (
            self.paid_work_ready is not None
            and task_is_paid(task_type.strip())
            and not self.paid_work_ready()
        ):
            raise FreeModeError(FREE_MODE_REASON)

    def enqueue_in_session(
        self,
        session: Session,
        *,
        workspace_id: str,
        task_type: str,
        payload: dict[str, Any],
        idempotency_key: str,
        actor: str = "local_user",
        origin: str = "api",
        correlation_id: str | None = None,
    ) -> TaskResponse:
        """Create or replay a task inside the caller's unit of work."""
        if not task_type.strip() or not idempotency_key.strip():
            raise ValidationError("Task type and idempotency key are required.")
        if task_type.strip() not in _TASK_TYPES:
            raise ValidationError(f"Unsupported task type {task_type.strip()!r}.")
        workspace = session.get(Workspace, workspace_id)
        if workspace is None or workspace.archived_at is not None:
            raise NotFoundError(f"Workspace {workspace_id!r} does not exist.")
        payload = _link_render_job(session, workspace_id, task_type.strip(), payload)
        digest = payload_digest(payload)
        job_posting_id = payload.get("job_posting_id")
        if isinstance(job_posting_id, str):
            job = session.scalar(
                select(JobPosting).where(
                    JobPosting.id == job_posting_id,
                    JobPosting.workspace_id == workspace_id,
                )
            )
            if job is None:
                raise NotFoundError(f"Job {job_posting_id!r} does not exist in this workspace.")
            if job.archived_at is not None:
                raise ConflictError(
                    "This job is archived. Choose an active tracked job before queueing work."
                )
        existing = session.scalar(
            select(BackgroundTask).where(
                BackgroundTask.workspace_id == workspace_id,
                BackgroundTask.task_type == task_type.strip(),
                BackgroundTask.idempotency_key == idempotency_key.strip(),
            )
        )
        if existing is not None:
            if existing.payload_sha256 != digest:
                raise ConflictError("This task idempotency key names a different payload.")
            return task_response(existing)
        if task_type.strip() == "source.scan":
            parameters = parameters_in(session, workspace_id)
            if not parameters.seniority.target_titles:
                raise ValidationError(
                    "Add at least one target title in search setup before scanning sources. "
                    "Individual posting links remain available."
                )
        _require_active_pending_targets(session, workspace_id, task_type.strip(), payload)
        task = BackgroundTask(
            workspace_id=workspace_id,
            task_type=task_type.strip(),
            status=TaskStatus.QUEUED.value,
            payload=payload,
            payload_sha256=digest,
            idempotency_key=idempotency_key.strip(),
            attempt_count=0,
            progress={},
            updated_at=utc_now(),
            created_by=actor,
            origin=origin,
            correlation_id=correlation_id,
        )
        session.add(task)
        session.flush()
        if task.task_type == "source.scan":
            self.bind_source_batch_in_session(session, task)
        elif task.task_type == "source.continue":
            self._admit_source_continuation(session, task)
        self.events.append(
            session,
            task,
            stage="queued",
            event_type="task.queued",
            payload={"task_type": task.task_type},
            actor=actor,
            origin=origin,
            correlation_id=correlation_id,
        )
        return task_response(task)

    def request_cancellation(
        self,
        task_id: str,
        *,
        workspace_id: str | None = None,
        actor: str = "local_user",
        origin: str = "api",
        correlation_id: str | None = None,
    ) -> TaskResponse:
        """Cancel owned source generations and request running readers to stop."""
        with self.database.exclusive_session() as session:
            task = session.get(BackgroundTask, task_id)
            if task is None or (workspace_id is not None and task.workspace_id != workspace_id):
                raise NotFoundError(f"Task {task_id!r} does not exist.")
            members = self._task_source_members(session, task)
            now = utc_now()
            owned = [task]
            for member in members:
                owned.extend(self._cancel_source_member(session, member, now))
            for target in {target.id: target for target in owned}.values():
                self._cancel_requested_task(
                    session,
                    target,
                    now,
                    cancel_terminal=target.id == task.id and bool(members),
                    actor=actor,
                    origin=origin,
                    correlation_id=correlation_id,
                )
            return task_response(task)

    def _task_source_members(
        self, session: Session, task: BackgroundTask
    ) -> list[SourceScanMember]:
        if task.task_type == "source.scan":
            return self.source_batch_members_in_session(session, task.id)
        if task.task_type != "source.continue":
            return []
        continuation = session.get(SourceScanContinuation, task.payload.get("continuation_id"))
        member = (
            session.get(SourceScanMember, continuation.batch_member_id)
            if continuation is not None and continuation.batch_member_id is not None
            else None
        )
        return (
            [member]
            if member is not None and member.generation == task.payload.get("generation")
            else []
        )

    def _cancel_source_member(
        self, session: Session, member: SourceScanMember, now: datetime
    ) -> list[BackgroundTask]:
        pending = (
            member.continuation_id is not None
            and session.scalar(
                select(SourceScanFrontierItem.id)
                .where(
                    SourceScanFrontierItem.continuation_id == member.continuation_id,
                    SourceScanFrontierItem.status.in_(["pending", "failed"]),
                )
                .limit(1)
            )
            is not None
        )
        if member.state == "already_in_progress" or (
            member.state == "completed" and member.released_at is not None and not pending
        ):
            return []
        member.cancellation_requested_at = now
        member.state = "cancelled"
        if member.active_task_id is None:
            member.released_at = now
        if member.continuation_id is None:
            return []
        return list(
            session.scalars(
                select(BackgroundTask).where(
                    BackgroundTask.workspace_id == member.workspace_id,
                    BackgroundTask.task_type == "source.continue",
                    BackgroundTask.payload["continuation_id"].as_string() == member.continuation_id,
                    BackgroundTask.payload["generation"].as_integer() == member.generation,
                    BackgroundTask.status.in_(["queued", "running"]),
                )
            )
        )

    def _cancel_requested_task(
        self,
        session: Session,
        task: BackgroundTask,
        now: datetime,
        *,
        cancel_terminal: bool,
        actor: str,
        origin: str,
        correlation_id: str | None,
    ) -> None:
        if task.status in _TERMINAL_STATUSES:
            if cancel_terminal:
                task.cancellation_requested_at = now
            return
        task.cancellation_requested_at = now
        task.updated_at = now
        event_type = "task.cancellation_requested"
        if task.status == TaskStatus.QUEUED.value:
            task.status = TaskStatus.CANCELLED.value
            task.completed_at = now
            event_type = "task.cancelled"
        self.events.append(
            session,
            task,
            stage=task.status,
            event_type=event_type,
            payload={},
            actor=actor,
            origin=origin,
            correlation_id=correlation_id,
        )

    def retry(
        self,
        task_id: str,
        *,
        workspace_id: str | None = None,
        actor: str = "local_user",
        origin: str = "api",
        correlation_id: str | None = None,
    ) -> TaskResponse:
        """Queue one idempotent retry of failed or canceled work."""
        with self.database.session() as session:
            task = session.get(BackgroundTask, task_id)
            if task is None or (workspace_id is not None and task.workspace_id != workspace_id):
                raise NotFoundError(f"Task {task_id!r} does not exist.")
            if task.status not in {TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}:
                raise ValidationError("Only failed or cancelled tasks can be retried.")
            if task.task_type == "source.continue" or (
                task.task_type == "source.scan"
                and task.payload.get("resume_from_task_id") is not None
            ):
                raise ValidationError(
                    "Resume the source batch or scan again to create a fresh source generation."
                )
            workspace_id = task.workspace_id
            task_type = task.task_type
            payload = dict(task.payload)
            idempotency_key = f"retry:{task.id}:{task.attempt_count}"
        return self.enqueue(
            workspace_id=workspace_id,
            task_type=task_type,
            payload=payload,
            idempotency_key=idempotency_key,
            actor=actor,
            origin=origin,
            correlation_id=correlation_id,
        )

    def source_batch_members_in_session(
        self, session: Session, root_task_id: str
    ) -> list[SourceScanMember]:
        """Return a batch's selected sources in their admission order."""
        return list(
            session.scalars(
                select(SourceScanMember)
                .where(SourceScanMember.root_task_id == root_task_id)
                .order_by(SourceScanMember.ordinal)
            )
        )

    def bind_source_batch_in_session(
        self, session: Session, task: BackgroundTask
    ) -> list[SourceScanMember]:
        """Snapshot source selection and reserve available sources atomically."""
        existing = self.source_batch_members_in_session(session, task.id)
        if existing:
            return existing
        payload = SourceScanPayload.model_validate(task.payload)
        prior_members = self._resume_prior_members(session, task, payload)
        ordered = self._selected_batch_sources(session, task, list(payload.source_ids))
        members = []
        for ordinal, source in enumerate(ordered):
            old = prior_members.get(source.id)
            member = self._reserve_source_member(session, task, source, ordinal, old)
            if old is not None:
                self._resume_member_frontier(session, task, member, old)
            members.append(member)
        session.flush()
        return members

    def _resume_prior_members(
        self, session: Session, task: BackgroundTask, payload: SourceScanPayload
    ) -> dict[str, SourceScanMember]:
        prior_members: dict[str, SourceScanMember] = {}
        selection = list(payload.source_ids)
        if payload.resume_from_task_id is not None:
            previous = session.get(BackgroundTask, payload.resume_from_task_id)
            if (
                previous is None
                or previous.workspace_id != task.workspace_id
                or previous.task_type != "source.scan"
            ):
                raise NotFoundError("The source batch does not exist in this workspace.")
            prior_members = {
                row.source_id: row for row in self._resumable_members(session, previous)
            }
            if not selection or any(source_id not in prior_members for source_id in selection):
                raise ValidationError("Resume must name sources from its original batch.")
        return prior_members

    def _selected_batch_sources(
        self, session: Session, task: BackgroundTask, selection: list[str]
    ) -> list[Source]:
        statement = (
            select(Source)
            .outerjoin(Employer, Employer.id == Source.employer_id)
            .where(
                Source.workspace_id == task.workspace_id,
                Source.archived_at.is_(None),
                or_(Source.employer_id.is_(None), Employer.archived_at.is_(None)),
            )
            .order_by(Source.priority, Source.name, Source.id)
        )
        if selection:
            statement = statement.where(Source.id.in_(selection))
        else:
            statement = statement.where(Source.enabled.is_(True))
        sources = list(session.scalars(statement))
        if selection and set(selection) != {source.id for source in sources}:
            raise NotFoundError(
                "Unknown source: a selected source does not exist in this workspace."
            )
        by_id = {source.id: source for source in sources}
        return [by_id[source_id] for source_id in selection] if selection else sources

    def _reserve_source_member(
        self,
        session: Session,
        task: BackgroundTask,
        source: Source,
        ordinal: int,
        old: SourceScanMember | None,
    ) -> SourceScanMember:
        if old is not None and old.released_at is None:
            old.released_at = utc_now()
            old.state = "failed"
            session.flush()
        holder = session.scalar(
            select(SourceScanMember).where(
                SourceScanMember.source_id == source.id, SourceScanMember.released_at.is_(None)
            )
        )
        stranded = holder is not None and self._stranded_chain(session, holder)
        if holder is not None and (stranded or self._dead_reader(holder)):
            holder.released_at = utc_now()
            holder.state = "failed"
            holder.error_category = "reader_lost"
            holder.error_message = (
                "The source chain stopped with no task left to continue it."
                if stranded
                else "The source reader's process stopped before recording its result."
            )
            session.flush()
            holder = None
        member = SourceScanMember(
            id=new_id(),
            workspace_id=task.workspace_id,
            root_task_id=task.id,
            source_id=source.id,
            ordinal=ordinal,
            state="already_in_progress" if holder is not None else "queued",
            released_at=utc_now() if holder is not None else None,
            created_by=task.created_by,
            origin=task.origin,
            correlation_id=task.id,
        )
        session.add(member)
        session.flush()
        return member

    def _resume_member_frontier(
        self,
        session: Session,
        task: BackgroundTask,
        member: SourceScanMember,
        old: SourceScanMember,
    ) -> None:
        if member.released_at is None and old.source_scan_id is not None:
            scan = session.get(SourceScan, old.source_scan_id)
            receipts = list(old.result.get("detail_receipts", []))
            if scan is not None and (
                scan.correlation_id != old.root_task_id
                and not any(
                    receipt.startswith(f"root:{scan.correlation_id}:") for receipt in receipts
                )
            ):
                raise ValidationError(
                    "A source was scanned again; scan its current state before resuming this batch."
                )
            member.source_scan_id = old.source_scan_id
            if receipts:
                member.result = {**member.result, "detail_receipts": receipts}
        if old.continuation_id is not None and member.released_at is None:
            continuation = session.get(SourceScanContinuation, old.continuation_id)
            if continuation is not None:
                if continuation.batch_member_id != old.id:
                    raise ValidationError(
                        "A source was scanned again; scan its current state "
                        "before resuming this batch."
                    )
                continuation.generation += 1
                continuation.batch_member_id = member.id
                continuation.correlation_id = task.id
                member.source_scan_id = continuation.source_scan_id
                member.continuation_id = continuation.id
                member.generation = continuation.generation
                for row in session.scalars(
                    select(SourceScanFrontierItem).where(
                        SourceScanFrontierItem.continuation_id == continuation.id,
                        SourceScanFrontierItem.status == "failed",
                    )
                ):
                    row.status = "pending"
                    row.automatic_eligible = False

    def _stranded_chain(self, session: Session, member: SourceScanMember) -> bool:
        if (
            member.state != "continuing"
            or member.active_task_id is not None
            or member.active_process_id is not None
        ):
            return False
        live = [TaskStatus.QUEUED.value, TaskStatus.RUNNING.value]
        successor = or_(
            and_(
                BackgroundTask.task_type == "source.scan", BackgroundTask.id == member.root_task_id
            ),
            and_(
                BackgroundTask.task_type == "source.continue",
                BackgroundTask.payload["continuation_id"].as_string() == member.continuation_id,
            ),
        )
        return (
            session.scalar(
                select(BackgroundTask.id)
                .where(
                    BackgroundTask.workspace_id == member.workspace_id,
                    BackgroundTask.status.in_(live),
                    successor,
                )
                .limit(1)
            )
            is None
        )

    def _dead_reader(self, member: SourceScanMember) -> bool:
        if (
            self.reader_alive is None
            or member.active_process_id is None
            or member.active_process_started_at is None
        ):
            return False
        return (
            self.reader_alive(member.active_process_id, member.active_process_started_at) is False
        )

    def _admit_source_continuation(self, session: Session, task: BackgroundTask) -> None:
        continuation = session.get(SourceScanContinuation, task.payload.get("continuation_id"))
        scan = session.get(SourceScan, continuation.source_scan_id) if continuation else None
        source = session.get(Source, scan.source_id) if scan else None
        if continuation is None or source is None or source.workspace_id != task.workspace_id:
            raise NotFoundError("The source continuation does not belong to this task workspace.")
        latest_scan_id = session.scalar(
            select(SourceScan.id)
            .where(SourceScan.source_id == source.id)
            .order_by(
                SourceScan.scanned_on.desc(),
                SourceScan.scan_completed_at.desc(),
                SourceScan.created_at.desc(),
                SourceScan.id.desc(),
            )
            .limit(1)
        )
        if latest_scan_id != continuation.source_scan_id:
            raise ValidationError("This scan is no longer its source's latest; scan again.")
        if continuation.generation != task.payload.get("generation"):
            raise ValidationError(
                "This source generation stopped; Resume the source batch or scan again."
            )
        if continuation.batch_member_id is not None:
            member = session.get(SourceScanMember, continuation.batch_member_id)
            if member is None or member.generation != continuation.generation:
                raise ValidationError("The source continuation has no admitted owner.")
            if member.released_at is not None or member.cancellation_requested_at is not None:
                raise ValidationError(
                    "This source generation stopped; Resume the source batch or scan again."
                )
            return
        self._reserve_continuation_member(session, task, continuation, source)

    def _reserve_continuation_member(
        self,
        session: Session,
        task: BackgroundTask,
        continuation: SourceScanContinuation,
        source: Source,
    ) -> None:
        holder = session.scalar(
            select(SourceScanMember).where(
                SourceScanMember.source_id == source.id, SourceScanMember.released_at.is_(None)
            )
        )
        if holder is not None:
            raise ConflictError("This source is already in progress.")
        member = SourceScanMember(
            id=new_id(),
            workspace_id=task.workspace_id,
            root_task_id=task.id,
            source_id=source.id,
            ordinal=0,
            state="queued",
            source_scan_id=continuation.source_scan_id,
            continuation_id=continuation.id,
            generation=continuation.generation,
            created_by=task.created_by,
            origin=task.origin,
            correlation_id=task.id,
        )
        session.add(member)
        session.flush()
        continuation.batch_member_id = member.id
        continuation.correlation_id = task.id

    def recover_dead_source_readers(self, reader_alive: Callable[[int, str], bool | None]) -> int:
        """Clear physical slots only when the owning process is confirmed dead."""
        recovered = 0
        with self.database.exclusive_session() as session:
            for member in session.scalars(
                select(SourceScanMember).where(SourceScanMember.active_task_id.is_not(None))
            ):
                if member.active_process_id is None or member.active_process_started_at is None:
                    continue
                if (
                    reader_alive(member.active_process_id, member.active_process_started_at)
                    is not False
                ):
                    continue
                member.active_task_id = None
                member.active_attempt_count = None
                member.active_process_id = None
                member.active_process_started_at = None
                member.read_completed_at = utc_now()
                if member.cancellation_requested_at is not None:
                    member.released_at = utc_now()
                    member.state = "cancelled"
                recovered += 1
        return recovered

    def source_read_is_live(
        self, member_id: str, task_id: str, attempt_count: int, generation: int | None = None
    ) -> bool:
        """Check cancellation, queue ownership, and the current source generation."""
        with self.database.session() as session:
            return self._source_read_is_live_in_session(
                session, member_id, task_id, attempt_count, generation
            )

    def _source_read_is_live_in_session(
        self,
        session: Session,
        member_id: str,
        task_id: str,
        attempt_count: int,
        generation: int | None,
    ) -> bool:
        member = session.get(SourceScanMember, member_id)
        task = session.get(BackgroundTask, task_id)
        if not (
            member is not None
            and task is not None
            and member.active_task_id == task_id
            and member.active_attempt_count == attempt_count
            and member.cancellation_requested_at is None
            and member.released_at is None
            and (generation is None or member.generation == generation)
            and task.status == TaskStatus.RUNNING.value
            and task.attempt_count == attempt_count
            and task.cancellation_requested_at is None
        ):
            return False
        if member.continuation_id is None:
            return True
        continuation = session.get(SourceScanContinuation, member.continuation_id)
        return (
            continuation is not None
            and continuation.batch_member_id == member.id
            and continuation.generation == member.generation
        )

    def require_source_read_in_session(
        self,
        session: Session,
        member_id: str,
        task_id: str,
        attempt_count: int,
        generation: int | None = None,
    ) -> None:
        """Reject a source write whose captured read owner is no longer current."""
        if not self._source_read_is_live_in_session(
            session, member_id, task_id, attempt_count, generation
        ):
            raise ConflictError("This source read no longer owns its task and generation.")

    def open_source_batch(
        self, workspace_id: str, source_ids: Sequence[str] | None, task_id: str | None = None
    ) -> tuple[str, list[SourceScanMember]]:
        """Load an admitted batch or open an internal synchronous source batch."""
        with self.database.exclusive_session() as session:
            if task_id is None:
                task = BackgroundTask(
                    id=new_id(),
                    workspace_id=workspace_id,
                    task_type="source.scan",
                    status=TaskStatus.RUNNING.value,
                    payload={"source_ids": list(source_ids or ())},
                    payload_sha256=payload_digest({"source_ids": list(source_ids or ())}),
                    idempotency_key=f"direct-source-scan:{new_id()}",
                    attempt_count=1,
                    progress={},
                    started_at=utc_now(),
                    updated_at=utc_now(),
                    created_by="local_user",
                    origin="direct",
                )
                session.add(task)
                session.flush()
                members = self.bind_source_batch_in_session(session, task)
            else:
                loaded = session.get(BackgroundTask, task_id)
                if loaded is None or loaded.workspace_id != workspace_id:
                    raise NotFoundError("The source batch does not exist in this workspace.")
                task = loaded
                if task.task_type != "source.scan":
                    raise ValidationError("The task is not a source scan batch.")
                members = self.source_batch_members_in_session(session, task.id)
            session.flush()
            return task.id, members

    def claim_source_read(
        self,
        member_id: str,
        task_id: str,
        attempt_count: int,
        *,
        process_id: int | None = None,
        process_started_at: str | None = None,
    ) -> bool:
        """Acquire one physical read slot without evicting a live or unknown reader."""
        with self.database.exclusive_session() as session:
            member = session.get(SourceScanMember, member_id)
            task = session.get(BackgroundTask, task_id)
            if (
                member is None
                or task is None
                or member.cancellation_requested_at is not None
                or member.released_at is not None
            ):
                return False
            if (
                task.status != TaskStatus.RUNNING.value
                or task.attempt_count != attempt_count
                or task.cancellation_requested_at is not None
            ):
                return False
            if member.active_task_id is not None and not self._dead_reader(member):
                return False
            member.active_task_id = task_id
            member.active_attempt_count = attempt_count
            member.active_process_id = process_id
            member.active_process_started_at = process_started_at
            member.read_started_at = utc_now()
            member.read_completed_at = None
            member.state = "scanning" if task.task_type == "source.scan" else "continuing"
            return True

    def release_source_read(
        self,
        member_id: str,
        task_id: str,
        attempt_count: int,
        *,
        result: dict[str, Any] | None = None,
        failed: bool = False,
    ) -> None:
        """Release only this attempt's physical slot and retain its completed work."""
        try:
            self._release_source_read(
                member_id, task_id, attempt_count, result=result, failed=failed
            )
        except OperationalError as error:
            raise UnavailableError(
                "The source reader stopped, but its reservation could not be saved. "
                "Restart the worker, then Resume the source batch or scan again."
            ) from error

    def _release_source_read(
        self,
        member_id: str,
        task_id: str,
        attempt_count: int,
        *,
        result: dict[str, Any] | None,
        failed: bool,
    ) -> None:
        with self.database.exclusive_session() as session:
            member = session.get(SourceScanMember, member_id)
            if (
                member is None
                or member.active_task_id != task_id
                or member.active_attempt_count != attempt_count
            ):
                return
            member.active_task_id = None
            member.active_attempt_count = None
            member.active_process_id = None
            member.active_process_started_at = None
            member.read_completed_at = utc_now()
            if result is not None:
                member.result = {**member.result, **result}
            if member.cancellation_requested_at is not None:
                member.state = "cancelled"
                member.released_at = utc_now()
                return
            children = (
                list(
                    session.scalars(
                        select(BackgroundTask).where(
                            BackgroundTask.workspace_id == member.workspace_id,
                            BackgroundTask.task_type == "source.continue",
                            BackgroundTask.status.in_(["queued", "running"]),
                            BackgroundTask.id != task_id,
                            BackgroundTask.payload["continuation_id"].as_string()
                            == member.continuation_id,
                            BackgroundTask.payload["generation"].as_integer() == member.generation,
                        )
                    )
                )
                if member.continuation_id is not None
                else []
            )
            frontier_failed = (
                member.continuation_id is not None
                and session.scalar(
                    select(SourceScanFrontierItem.id)
                    .where(
                        SourceScanFrontierItem.continuation_id == member.continuation_id,
                        SourceScanFrontierItem.status == "failed",
                    )
                    .limit(1)
                )
                is not None
            )
            if children:
                member.state = "continuing"
            else:
                member.state = "failed" if failed or frontier_failed else "completed"
                member.released_at = utc_now()
            if failed and result is not None:
                member.error_category = "source_read_failed"
                member.error_message = str(result.get("reason", "The source read failed."))

    def finish_source_batch(self, task_id: str, *, direct: bool, failed: bool = False) -> None:
        """Settle unread members while physical readers retain their reservations."""
        with self.database.exclusive_session() as session:
            task = session.get(BackgroundTask, task_id)
            if task is None:
                return
            for member in self.source_batch_members_in_session(session, task_id):
                if member.active_task_id is None and member.state == "queued":
                    member.state = (
                        "cancelled" if task.cancellation_requested_at is not None else "failed"
                    )
                    member.released_at = utc_now()
            if direct:
                task.status = TaskStatus.FAILED.value if failed else TaskStatus.COMPLETED.value
                task.completed_at = utc_now()
                drop_member_board_results(session, task_id)

    def _resumable_members(self, session: Session, task: BackgroundTask) -> list[SourceScanMember]:
        if task.status in {"queued", "running"}:
            raise ConflictError("This source batch is already in progress; resume after it stops.")
        members = self.source_batch_members_in_session(session, task.id)
        unfinished = []
        for member in members:
            if member.state == "already_in_progress":
                continue
            pending = (
                member.continuation_id is not None
                and session.scalar(
                    select(SourceScanFrontierItem.id)
                    .where(
                        SourceScanFrontierItem.continuation_id == member.continuation_id,
                        SourceScanFrontierItem.status.in_(["pending", "failed"]),
                    )
                    .limit(1)
                )
                is not None
            )
            if member.state == "completed" and not pending:
                continue
            if member.active_task_id is not None:
                raise ConflictError("A source reader is still in progress; resume after it stops.")
            if (
                member.continuation_id is not None
                and session.scalar(
                    select(BackgroundTask.id)
                    .where(
                        BackgroundTask.task_type == "source.continue",
                        BackgroundTask.status.in_(["queued", "running"]),
                        BackgroundTask.payload["continuation_id"].as_string()
                        == member.continuation_id,
                        BackgroundTask.payload["generation"].as_integer() == member.generation,
                    )
                    .limit(1)
                )
                is not None
            ):
                raise ConflictError(
                    "Follow-up reads are already in progress; resume after they stop."
                )
            unfinished.append(member)
        if not unfinished:
            raise ValidationError("This source batch has no unfinished work to resume.")
        return unfinished

    def resume_source_batch(
        self,
        workspace_id: str,
        task_id: str,
        idempotency_key: str,
        *,
        actor: str = "local_user",
        origin: str = "api",
        correlation_id: str | None = None,
    ) -> TaskResponse:
        """Create a new source batch retaining unfinished frontier work."""
        with self.database.exclusive_session() as session:
            existing = session.scalar(
                select(BackgroundTask).where(
                    BackgroundTask.workspace_id == workspace_id,
                    BackgroundTask.task_type == "source.scan",
                    BackgroundTask.idempotency_key == idempotency_key.strip(),
                )
            )
            if existing is not None:
                if existing.payload.get("resume_from_task_id") != task_id:
                    raise ConflictError("This task idempotency key names a different payload.")
                return task_response(existing)
            previous = session.get(BackgroundTask, task_id)
            if (
                previous is None
                or previous.workspace_id != workspace_id
                or previous.task_type != "source.scan"
            ):
                raise NotFoundError("The source batch does not exist in this workspace.")
            unfinished = self._resumable_members(session, previous)
            payload = {
                "source_ids": [member.source_id for member in unfinished],
                "resume_from_task_id": task_id,
            }
            return self.enqueue_in_session(
                session,
                workspace_id=workspace_id,
                task_type="source.scan",
                payload=payload,
                idempotency_key=idempotency_key,
                actor=actor,
                origin=origin,
                correlation_id=correlation_id,
            )
