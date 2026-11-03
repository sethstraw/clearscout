"""One discovery task's candidate ledger, paged or streamed."""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.orm import Session

from clearscout.application.discovery.funnel_audit import (
    FunnelCandidateKind,
    FunnelReason,
    FunnelStage,
)
from clearscout.application.discovery.scan_queries.workspace import require_workspace_in
from clearscout.application.discovery.source_continuation.family import continuation_task_ids
from clearscout.application.failures import NotFoundError, ValidationError
from clearscout.application.job_search.models import (
    FunnelScreeningFindingResponse,
    SourceScanOutcomeCaptureState,
    SourceScanOutcomePageResponse,
    SourceScanOutcomeResponse,
)
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import BackgroundTask, SourceScanOutcome


@dataclass(frozen=True)
class ScanOutcomeFilters:
    """The ledger rows one request asks for, and the order it reads them in."""

    search: str = ""
    source: str = ""
    terminal_stage: FunnelStage | None = None
    reason: FunnelReason | None = None
    candidate_kind: FunnelCandidateKind | None = None
    sort: str = "created_at"
    direction: str = "desc"


class ScanOutcomeQueries:
    """Read one discovery task's candidate ledger as a page or a stream."""

    def __init__(self, database: Database) -> None:
        """Bind ledger reads to the application database."""
        self.database = database

    @staticmethod
    def _outcome_order(sort: str, direction: str) -> ColumnElement[object]:
        columns = {
            "employer": SourceScanOutcome.employer,
            "title": SourceScanOutcome.title,
            "location": SourceScanOutcome.location,
            "terminal_stage": SourceScanOutcome.terminal_stage,
            "reason": SourceScanOutcome.reason,
            "created_at": SourceScanOutcome.created_at,
        }
        column = columns.get(sort)
        if column is None:
            raise ValidationError("Source scan outcome sort is not supported.")
        if direction not in {"asc", "desc"}:
            raise ValidationError("Source scan outcome direction is not supported.")
        return column.asc() if direction == "asc" else column.desc()

    def _outcome_task(
        self, session: Session, workspace_id: str, task_id: str
    ) -> tuple[BackgroundTask, list[str]]:
        require_workspace_in(session, workspace_id)
        task = session.scalar(
            select(BackgroundTask).where(
                BackgroundTask.id == task_id,
                BackgroundTask.workspace_id == workspace_id,
                BackgroundTask.task_type.in_(
                    (
                        "source.scan",
                        "source.continue",
                        "search.guided_campaign",
                        "search.guided_campaign_retry",
                        "search.vocabulary",
                    )
                ),
            )
        )
        if task is None:
            raise NotFoundError(f"Discovery task '{task_id}' does not exist.")
        # A continuation owns the rows it read, so a scan's ledger is the whole
        # funnel only when it also reads its continuation tasks' rows. A chained
        # continue task carries its parent's id as correlation, so it reads only its own.
        ledger_task_ids = [task_id]
        if task.task_type != "source.continue":
            ledger_task_ids += continuation_task_ids(session, workspace_id, task_id)
        return task, ledger_task_ids

    @staticmethod
    def _outcome_response(row: SourceScanOutcome) -> SourceScanOutcomeResponse:
        return SourceScanOutcomeResponse(
            id=row.id,
            task_id=row.task_id,
            source_scan_id=row.source_scan_id,
            source_name=row.source_name,
            candidate_kind=FunnelCandidateKind(row.candidate_kind),
            url=row.url,
            employer=row.employer,
            title=row.title,
            location=row.location,
            terminal_stage=FunnelStage(row.terminal_stage),
            reason=FunnelReason(row.reason),
            detail=row.detail,
            screening_findings=[
                FunnelScreeningFindingResponse.model_validate(item)
                for item in row.screening_findings
            ],
            created_at=row.created_at,
        )

    def export_scan_outcomes(
        self,
        workspace_id: str,
        *,
        task_id: str,
        filters: ScanOutcomeFilters | None = None,
    ) -> Generator[SourceScanOutcomeResponse, None, None]:
        """Stream the filtered ledger in one bounded-memory read without page metadata."""
        filters = filters or ScanOutcomeFilters()
        ordered = self._outcome_order(filters.sort, filters.direction)
        with self.database.session() as session:
            _, task_ids = self._outcome_task(session, workspace_id, task_id)
            narrowed = self._narrowed_outcomes(workspace_id, task_ids, filters)
            rows = session.scalars(
                select(SourceScanOutcome)
                .where(*narrowed)
                .order_by(ordered, SourceScanOutcome.id)
                .execution_options(yield_per=250)
            )
            try:
                for row in rows:
                    yield self._outcome_response(row)
            finally:
                rows.close()

    def list_scan_outcomes(
        self,
        workspace_id: str,
        *,
        task_id: str,
        filters: ScanOutcomeFilters | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> SourceScanOutcomePageResponse:
        """Return one queryable task-scoped candidate audit ledger."""
        if offset < 0:
            raise ValidationError("Source scan outcome offset cannot be negative.")
        if limit < 1 or limit > 250:
            raise ValidationError("Source scan outcome limit must be between 1 and 250.")
        filters = filters or ScanOutcomeFilters()
        ordered = self._outcome_order(filters.sort, filters.direction)
        with self.database.session() as session:
            task, ledger_task_ids = self._outcome_task(session, workspace_id, task_id)
            narrowed = self._narrowed_outcomes(workspace_id, ledger_task_ids, filters)
            task_filter = (
                SourceScanOutcome.workspace_id == workspace_id,
                SourceScanOutcome.task_id.in_(ledger_task_ids),
            )
            recorded_outcomes = (
                session.scalar(
                    select(func.count()).select_from(SourceScanOutcome).where(*task_filter)
                )
                or 0
            )
            follow_up_outcomes = (
                session.scalar(
                    select(func.count())
                    .select_from(SourceScanOutcome)
                    .where(
                        SourceScanOutcome.workspace_id == workspace_id,
                        SourceScanOutcome.task_id.in_(ledger_task_ids[1:]),
                    )
                )
                or 0
            )
            task_origins = {
                "source.scan": ("source.scan", "operations.workspace_backfill"),
                "source.continue": ("source.continue",),
                "search.guided_campaign": ("search.guided_campaign",),
                # A retry pass writes under its own task id, but the admission path
                # stamps every row with the campaign origin.
                "search.guided_campaign_retry": ("search.guided_campaign",),
                "search.vocabulary": ("search.vocabulary",),
            }
            captured_outcomes = (
                session.scalar(
                    select(func.count())
                    .select_from(SourceScanOutcome)
                    .where(
                        *task_filter,
                        SourceScanOutcome.origin.in_(task_origins[task.task_type]),
                    )
                )
                or 0
            )
            reported_outcomes, reported_omitted_outcomes = self._funnel_totals(task.progress)
            recovered = (
                session.scalar(
                    select(func.count())
                    .select_from(SourceScanOutcome)
                    .where(
                        *task_filter,
                        SourceScanOutcome.origin == "operations.workspace_backfill",
                    )
                )
                or 0
            ) > 0
            total = session.scalar(
                select(func.count()).select_from(SourceScanOutcome).where(*narrowed)
            )
            rows = session.scalars(
                select(SourceScanOutcome)
                .where(*narrowed)
                .order_by(ordered, SourceScanOutcome.id)
                .offset(offset)
                .limit(limit)
            ).all()
            return SourceScanOutcomePageResponse(
                items=[self._outcome_response(row) for row in rows],
                total=total or 0,
                offset=offset,
                limit=limit,
                sources=list(
                    session.scalars(
                        select(SourceScanOutcome.source_name)
                        .where(*task_filter)
                        .distinct()
                        .order_by(SourceScanOutcome.source_name)
                    )
                ),
                terminal_stages=[
                    FunnelStage(value)
                    for value in session.scalars(
                        select(SourceScanOutcome.terminal_stage)
                        .where(*task_filter)
                        .distinct()
                        .order_by(SourceScanOutcome.terminal_stage)
                    )
                ],
                reasons=[
                    FunnelReason(value)
                    for value in session.scalars(
                        select(SourceScanOutcome.reason)
                        .where(*task_filter)
                        .distinct()
                        .order_by(SourceScanOutcome.reason)
                    )
                ],
                candidate_kinds=[
                    FunnelCandidateKind(value)
                    for value in session.scalars(
                        select(SourceScanOutcome.candidate_kind)
                        .where(*task_filter)
                        .distinct()
                        .order_by(SourceScanOutcome.candidate_kind)
                    )
                ],
                capture_state=self._capture_state(
                    task.status,
                    captured_outcomes,
                    reported_outcomes,
                    recovered=recovered,
                ),
                recorded_outcomes=recorded_outcomes,
                follow_up_outcomes=follow_up_outcomes,
                reported_outcomes=reported_outcomes,
                reported_omitted_outcomes=reported_omitted_outcomes,
            )

    @staticmethod
    def _narrowed_outcomes(
        workspace_id: str, ledger_task_ids: list[str], filters: ScanOutcomeFilters
    ) -> list[ColumnElement[bool]]:
        """Return the conditions selecting the ledger rows a request asked to see."""
        conditions: list[ColumnElement[bool]] = [
            SourceScanOutcome.workspace_id == workspace_id,
            SourceScanOutcome.task_id.in_(ledger_task_ids),
        ]
        if filters.source.strip():
            conditions.append(SourceScanOutcome.source_name == filters.source.strip())
        if filters.terminal_stage is not None:
            conditions.append(SourceScanOutcome.terminal_stage == filters.terminal_stage.value)
        if filters.reason is not None:
            conditions.append(SourceScanOutcome.reason == filters.reason.value)
        if filters.candidate_kind is not None:
            conditions.append(SourceScanOutcome.candidate_kind == filters.candidate_kind.value)
        if term := filters.search.strip():
            conditions.append(
                or_(
                    *(
                        column.icontains(term, autoescape=True)
                        for column in (
                            SourceScanOutcome.employer,
                            SourceScanOutcome.title,
                            SourceScanOutcome.location,
                            SourceScanOutcome.source_name,
                        )
                    )
                )
            )
        return conditions

    @staticmethod
    def _funnel_totals(progress: dict[str, object]) -> tuple[int | None, int | None]:
        """Return trustworthy full counts from a source.scan task's final payload."""
        funnel = progress.get("funnel")
        if not isinstance(funnel, dict):
            return None, None
        total = funnel.get("total_outcomes")
        omitted = funnel.get("omitted_outcomes")
        return (
            total if isinstance(total, int) and total >= 0 else None,
            omitted if isinstance(omitted, int) and omitted >= 0 else None,
        )

    @staticmethod
    def _capture_state(
        task_status: str,
        captured_outcomes: int,
        reported_outcomes: int | None,
        *,
        recovered: bool,
    ) -> SourceScanOutcomeCaptureState:
        """Describe coverage without treating a bounded task payload as a full ledger."""
        if task_status in {"queued", "running"}:
            return "in_progress"
        if reported_outcomes is None:
            return "unavailable"
        # A task payload reports only its own reads, so the rows its follow-up
        # tasks recorded stay out of both sides of this comparison.
        if recovered:
            return "recovered" if captured_outcomes == reported_outcomes else "recovered_partial"
        return "complete" if captured_outcomes == reported_outcomes else "partial"
