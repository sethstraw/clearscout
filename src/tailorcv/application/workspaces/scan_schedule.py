"""Workspace-owned local calendar for free source scans."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import ValidationError as ModelValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from tailorcv.application.failures import ConflictError, NotFoundError, ValidationError
from tailorcv.application.tasks import TaskQueueService
from tailorcv.application.workspaces.models import (
    ScanScheduleConfiguration,
    ScanScheduleInput,
    ScanScheduleResponse,
    ScanScheduleState,
)
from tailorcv.application.workspaces.parameters import parameters_in
from tailorcv.application.workspaces.records.access import add_audit_event, require_workspace
from tailorcv.domain import utc_now
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import Employer, Source, Workspace


def _next_occurrence(configuration: ScanScheduleConfiguration, after: datetime) -> datetime:
    zone = ZoneInfo(configuration.timezone)
    local_date = after.astimezone(zone).date()
    hour, minute = (int(part) for part in configuration.local_time.split(":"))
    while True:
        if configuration.frequency == "daily" or local_date.weekday() == configuration.weekday:
            local = datetime.combine(local_date, datetime.min.time()).replace(
                hour=hour, minute=minute, tzinfo=zone, fold=0
            )
            candidate = local.astimezone(UTC)
            resolved = candidate.astimezone(zone)
            # A partial gap may cross midnight; a whole skipped date has a day-long shift.
            gap = resolved.replace(tzinfo=None) - local.replace(tzinfo=None)
            if gap < timedelta(days=1) and candidate > after:
                return candidate
        local_date += timedelta(days=1)


def _state(workspace: Workspace) -> ScanScheduleState | None:
    if workspace.scan_schedule is None:
        return None
    try:
        return ScanScheduleState.model_validate(workspace.scan_schedule)
    except ModelValidationError:
        return None


def _source_ids(
    session: Session, workspace_id: str, configuration: ScanScheduleConfiguration
) -> list[str]:
    statement = (
        select(Source.id)
        .outerjoin(Employer, Employer.id == Source.employer_id)
        .where(
            Source.workspace_id == workspace_id,
            Source.archived_at.is_(None),
            or_(Source.employer_id.is_(None), Employer.archived_at.is_(None)),
        )
        .order_by(Source.priority, Source.name, Source.id)
    )
    if configuration.scope == "selected":
        available = set(session.scalars(statement.where(Source.id.in_(configuration.source_ids))))
        return [source_id for source_id in configuration.source_ids if source_id in available]
    return list(session.scalars(statement.where(Source.enabled.is_(True))))


def _response(session: Session, workspace: Workspace) -> ScanScheduleResponse:
    state = _state(workspace)
    unavailable: list[str] = []
    if state is not None and state.configuration.scope == "selected":
        available = set(_source_ids(session, workspace.id, state.configuration))
        unavailable = [
            source_id for source_id in state.configuration.source_ids if source_id not in available
        ]
    return ScanScheduleResponse(
        workspace_id=workspace.id, state=state, unavailable_source_ids=unavailable
    )


def _require_titles(session: Session, workspace_id: str) -> None:
    if not parameters_in(session, workspace_id).seniority.target_titles:
        raise ValidationError(
            "Add at least one target title in search setup before scheduling source scans."
        )


class ScanScheduleService:
    """Save and claim workspace scan calendar occurrences."""

    def __init__(
        self,
        database: Database,
        *,
        queue: TaskQueueService | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """Use the supplied queue, transaction boundary, and aware clock."""
        self.database = database
        self.queue = queue or TaskQueueService(database)
        self.clock = clock

    def read(self, workspace_id: str) -> ScanScheduleResponse:
        """Read the saved calendar while retaining unavailable source selections."""
        with self.database.session() as session:
            return _response(session, require_workspace(session, workspace_id))

    def save(
        self,
        workspace_id: str,
        document: ScanScheduleInput,
        *,
        origin: str = "api",
        correlation_id: str | None = None,
    ) -> ScanScheduleResponse:
        """Replace one revision and schedule its first future local occurrence."""
        now = self._now()
        configuration = ScanScheduleConfiguration.model_validate(
            document.model_dump(exclude={"expected_revision"})
        )
        with self.database.exclusive_session() as session:
            workspace = require_workspace(session, workspace_id)
            current = _state(workspace)
            revision = current.revision if current is not None else 0
            if document.expected_revision != revision:
                raise ConflictError(
                    "The scan schedule changed. Reload it before saving your draft."
                )
            if configuration.scope == "selected":
                available = set(_source_ids(session, workspace_id, configuration))
                previous = set(current.configuration.source_ids) if current is not None else set()
                if set(configuration.source_ids) - available - previous:
                    raise NotFoundError(
                        "A selected source does not exist in this active workspace."
                    )
            if not configuration.paused:
                _require_titles(session, workspace_id)
            state = ScanScheduleState(
                configuration=configuration,
                revision=revision + 1,
                next_run_at=None if configuration.paused else _next_occurrence(configuration, now),
                last_due_at=current.last_due_at if current else None,
                last_task_id=current.last_task_id if current else None,
                last_error=current.last_error if current else None,
            )
            workspace.scan_schedule = state.model_dump(mode="json")
            add_audit_event(
                session,
                workspace_id=workspace_id,
                action="scan_schedule.saved",
                entity_type="workspace",
                entity_id=workspace_id,
                details={
                    "revision": state.revision,
                    "configuration": configuration.model_dump(mode="json"),
                },
                origin=origin,
                correlation_id=correlation_id,
            )
            return _response(session, workspace)

    def tick(self) -> list[str]:
        """Claim at most one overdue free scan per active workspace."""
        now = self._now()
        with self.database.session() as session:
            workspaces = session.scalars(
                select(Workspace)
                .where(Workspace.archived_at.is_(None), Workspace.scan_schedule.is_not(None))
                .order_by(Workspace.id)
            )
            candidates = [
                workspace.id for workspace in workspaces if self._due(_state(workspace), now)
            ]
        task_ids = []
        for workspace_id in candidates:
            task_id = self._claim(workspace_id)
            if task_id is not None:
                task_ids.append(task_id)
        return task_ids

    def _now(self) -> datetime:
        now = self.clock()
        if now.utcoffset() is None:
            raise ValidationError("The scan schedule clock must include a timezone.")
        return now.astimezone(UTC)

    @staticmethod
    def _due(state: ScanScheduleState | None, now: datetime) -> bool:
        return (
            state is not None
            and not state.configuration.paused
            and state.next_run_at is not None
            and state.next_run_at <= now
        )

    def _claim(self, workspace_id: str) -> str | None:
        with self.database.exclusive_session() as session:
            workspace = session.get(Workspace, workspace_id)
            if workspace is None or workspace.archived_at is not None:
                return None
            state = _state(workspace)
            now = self._now()
            if state is None or state.next_run_at is None or not self._due(state, now):
                return None
            due = state.next_run_at
            task_id = None
            error = None
            try:
                _require_titles(session, workspace_id)
                source_ids = _source_ids(session, workspace_id, state.configuration)
                if not source_ids:
                    raise ValidationError(
                        "No sources in the saved scan scope are currently available."
                    )
                with session.begin_nested():
                    task = self.queue.enqueue_in_session(
                        session,
                        workspace_id=workspace_id,
                        task_type="source.scan",
                        payload={
                            "source_ids": source_ids
                            if state.configuration.scope == "selected"
                            else []
                        },
                        idempotency_key=f"scan_schedule:{workspace_id}:{state.revision}:{due.isoformat()}",
                        origin="scan_schedule",
                    )
                task_id = task.id
            except (ValidationError, NotFoundError, ConflictError) as exc:
                error = str(exc)
            claimed = state.model_copy(
                update={
                    "next_run_at": _next_occurrence(state.configuration, max(now, due)),
                    "last_due_at": due,
                    "last_task_id": task_id,
                    "last_error": error,
                }
            )
            workspace.scan_schedule = claimed.model_dump(mode="json")
            add_audit_event(
                session,
                workspace_id=workspace_id,
                action="scan_schedule.claimed",
                entity_type="workspace",
                entity_id=workspace_id,
                details={
                    "revision": state.revision,
                    "due_at": due.isoformat(),
                    "task_id": task_id,
                    "error": error,
                },
                origin="scan_schedule",
                correlation_id=task_id,
            )
            return task_id
