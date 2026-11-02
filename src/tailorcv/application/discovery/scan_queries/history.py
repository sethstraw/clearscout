"""Kept board scans, their follow-up state, and their screened-out counts."""

from __future__ import annotations

from typing import get_args

from pydantic import BaseModel
from sqlalchemy import ColumnElement, Select, and_, case, func, or_, select
from sqlalchemy.orm import Session

from tailorcv.application.discovery.funnel_audit import FunnelReason
from tailorcv.application.discovery.pending import (
    dismissed_pending_discovery_ids,
    visible_pending_discovery_filters,
)
from tailorcv.application.discovery.scan_queries.workspace import require_workspace_in
from tailorcv.application.discovery.source_continuation import (
    LATEST_SCAN_FIRST,
    continuation_task_ids,
)
from tailorcv.application.failures import ValidationError
from tailorcv.application.job_search.models import (
    SourceScanDetailFailureReasonsResponse,
    SourceScanFollowUpState,
    SourceScanHistoryPageResponse,
    SourceScanHistoryStatus,
    SourceScanKeptAs,
    SourceScanKind,
    SourceScanResponse,
    SourceScanSkipCountsResponse,
)
from tailorcv.domain.identity import url_sha256
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    BackgroundTask,
    Employer,
    PendingDiscovery,
    Source,
    SourceScan,
    SourceScanContinuation,
    SourceScanFrontierItem,
    SourceScanOutcome,
)

QUERY_LABEL = "title-filter"

SCREENED_OUT_REASONS = (
    FunnelReason.HARD_CONSTRAINT_EXCLUDED.value,
    FunnelReason.INSUFFICIENT_TITLE_EVIDENCE.value,
)


def _zeroed_ints(response_type: type[BaseModel], source: dict[str, int] | None) -> dict[str, int]:
    """Zero-fill missing keys for an all-int response model, refusing other shapes."""
    values = source or {}
    zeroed: dict[str, int] = {}
    for name, field in response_type.model_fields.items():
        if field.annotation is not int:
            raise TypeError(
                f"{response_type.__name__}.{name} must be annotated as int, "
                f"got {field.annotation!r}."
            )
        zeroed[name] = values.get(name, 0)
    return zeroed


def _kept_as(scan: SourceScan, latest_of_kind: bool) -> SourceScanKeptAs:
    """Name why history keeps a scan; a kept scan that is not the latest is the latest complete."""
    if not latest_of_kind:
        return "latest_complete"
    return "latest_and_complete" if scan.complete else "latest"


_SCAN_KINDS: dict[str, SourceScanKind] = {
    "title-filter": "title-filter",
    "guided-search": "guided-search",
}


def _scan_kind(query_label: str) -> SourceScanKind:
    """Map a stored query label onto the published kinds of read."""
    return _SCAN_KINDS.get(query_label, "other")


def latest_scan_ids_in(session: Session, workspace_id: str) -> frozenset[str]:
    """Return each source's most recent scan id."""
    return frozenset(session.scalars(_latest_scan_id_statement(workspace_id)))


def _latest_scan_id_statement(workspace_id: str, *, board_only: bool = False) -> Select[tuple[str]]:
    """Select each source's most recent scan id, optionally among board scans only."""
    scoped = select(
        SourceScan.id.label("scan_id"),
        func.row_number()
        .over(
            partition_by=SourceScan.source_id,
            order_by=LATEST_SCAN_FIRST,
        )
        .label("rank"),
    ).join(Source, Source.id == SourceScan.source_id)
    scoped = scoped.where(Source.workspace_id == workspace_id)
    if board_only:
        scoped = scoped.where(SourceScan.query_label == QUERY_LABEL)
    ranked = scoped.subquery()
    return select(ranked.c.scan_id).where(ranked.c.rank == 1)


def kept_scan_id_statement(workspace_id: str) -> Select[tuple[str]]:
    """Select each source's latest scan and latest complete scan, per kind of read."""
    kept = _kept_scan_statement(workspace_id).subquery()
    return select(kept.c.scan_id)


def _kept_scan_statement(workspace_id: str) -> Select[tuple[str, bool]]:
    """Select each kept scan id with whether it is the latest of its source and kind."""
    kind = (SourceScan.source_id, SourceScan.query_label)
    ranked = (
        select(
            SourceScan.id.label("scan_id"),
            SourceScan.complete.label("complete"),
            func.row_number().over(partition_by=kind, order_by=LATEST_SCAN_FIRST).label("latest"),
            func.row_number()
            .over(partition_by=(*kind, SourceScan.complete), order_by=LATEST_SCAN_FIRST)
            .label("latest_alike"),
        )
        .join(Source, Source.id == SourceScan.source_id)
        .where(Source.workspace_id == workspace_id)
        .subquery()
    )
    return select(ranked.c.scan_id, (ranked.c.latest == 1).label("latest_of_kind")).where(
        (ranked.c.latest == 1) | (ranked.c.complete.is_(True) & (ranked.c.latest_alike == 1))
    )


def frontier_status_counts(
    session: Session, scan_ids: list[str]
) -> tuple[dict[tuple[str, str], int], dict[str, int]]:
    """Group frontier rows by scan and status, and by scan for automatic-eligible pending."""
    if not scan_ids:
        return {}, {}
    rows = session.execute(
        select(
            SourceScanContinuation.source_scan_id,
            SourceScanFrontierItem.status,
            SourceScanFrontierItem.automatic_eligible,
            func.count(),
        )
        .join(
            SourceScanFrontierItem,
            SourceScanFrontierItem.continuation_id == SourceScanContinuation.id,
        )
        .where(SourceScanContinuation.source_scan_id.in_(scan_ids))
        .group_by(
            SourceScanContinuation.source_scan_id,
            SourceScanFrontierItem.status,
            SourceScanFrontierItem.automatic_eligible,
        )
    )
    status_counts: dict[tuple[str, str], int] = {}
    automatic_pending_counts: dict[str, int] = {}
    for scan_id, status, automatic_eligible, count in rows:
        status_counts[(scan_id, status)] = status_counts.get((scan_id, status), 0) + count
        if status == "pending" and automatic_eligible:
            automatic_pending_counts[scan_id] = automatic_pending_counts.get(scan_id, 0) + count
    return status_counts, automatic_pending_counts


def scan_follow_up_states(
    session: Session,
    workspace_id: str,
    scan_ids: list[str],
    latest_scan_ids: frozenset[str],
) -> dict[str, SourceScanFollowUpState]:
    """Derive each scan's follow-up state from its continuation, frontier, and task queue."""
    if not scan_ids:
        return {}
    continuation_by_scan = dict(
        session.execute(
            select(SourceScanContinuation.source_scan_id, SourceScanContinuation.id).where(
                SourceScanContinuation.source_scan_id.in_(scan_ids)
            )
        )
        .tuples()
        .all()
    )
    states: dict[str, SourceScanFollowUpState] = {
        scan_id: "none" for scan_id in scan_ids if scan_id not in continuation_by_scan
    }
    if not continuation_by_scan:
        return states
    continuation_ids = list(continuation_by_scan.values())
    pending_by_continuation = dict(
        session.execute(
            select(SourceScanFrontierItem.continuation_id, func.count())
            .where(
                SourceScanFrontierItem.continuation_id.in_(continuation_ids),
                SourceScanFrontierItem.status == "pending",
            )
            .group_by(SourceScanFrontierItem.continuation_id)
        )
        .tuples()
        .all()
    )
    active_continuation_ids = set(
        session.scalars(
            select(BackgroundTask.payload["continuation_id"].as_string()).where(
                BackgroundTask.workspace_id == workspace_id,
                BackgroundTask.task_type == "source.continue",
                BackgroundTask.status.in_(("queued", "running")),
                BackgroundTask.payload["continuation_id"].as_string().in_(continuation_ids),
            )
        )
    )
    for scan_id, continuation_id in continuation_by_scan.items():
        if scan_id not in latest_scan_ids:
            states[scan_id] = "superseded"
        elif continuation_id in active_continuation_ids:
            states[scan_id] = "active"
        elif pending_by_continuation.get(continuation_id, 0) > 0:
            states[scan_id] = "waiting"
        else:
            states[scan_id] = "complete"
    return states


def _screened_out_counts(
    session: Session, workspace_id: str, task_sources: set[tuple[str, str]]
) -> dict[tuple[str, str], int]:
    """Count distinct restorable screened-out listings per task and source."""
    if not task_sources:
        return {}
    # The same task set the outcome page reads: a continuation's rows count for its scan.
    scan_task_of: dict[str, str] = {}
    for scan_task_id in {task_id for task_id, _source in task_sources}:
        scan_task_of[scan_task_id] = scan_task_id
        for continuation_task in continuation_task_ids(session, workspace_id, scan_task_id):
            scan_task_of.setdefault(continuation_task, scan_task_id)
    family_tasks: dict[str, list[str]] = {}
    for task_id, scan_task_id in scan_task_of.items():
        family_tasks.setdefault(scan_task_id, []).append(task_id)
    requested_sources: dict[str, set[str]] = {}
    for scan_task_id, source_name in task_sources:
        requested_sources.setdefault(scan_task_id, set()).add(source_name)
    requested_pairs = or_(
        *(
            and_(
                SourceScanOutcome.task_id.in_(family_tasks[scan_task_id]),
                SourceScanOutcome.source_name.in_(sources),
            )
            for scan_task_id, sources in requested_sources.items()
        )
    )
    conditions = (
        SourceScanOutcome.workspace_id == workspace_id,
        SourceScanOutcome.task_id.in_(scan_task_of),
        SourceScanOutcome.source_name.in_({source for _task_id, source in task_sources}),
        SourceScanOutcome.reason.in_(SCREENED_OUT_REASONS),
        requested_pairs,
    )
    # Pending rows carry no ledger link; the shared URL fingerprint is the join.
    in_pending = set(
        session.scalars(
            select(PendingDiscovery.fingerprint).where(
                *visible_pending_discovery_filters(
                    workspace_id,
                    dismissed_pending_discovery_ids(workspace_id),
                )
            )
        )
    )
    excluded_urls = []
    if in_pending:
        excluded_urls = [
            url
            for url in session.scalars(select(SourceScanOutcome.url).distinct().where(*conditions))
            if url_sha256(url) in in_pending
        ]
    scan_task = case(scan_task_of, value=SourceScanOutcome.task_id)
    counts = select(
        scan_task,
        SourceScanOutcome.source_name,
        func.count(func.distinct(SourceScanOutcome.normalized_url)),
    ).where(*conditions)
    if excluded_urls:
        counts = counts.where(SourceScanOutcome.url.not_in(excluded_urls))
    rows = session.execute(counts.group_by(scan_task, SourceScanOutcome.source_name))
    return {(task_id, source_name): count for task_id, source_name, count in rows}


class ScanHistoryQueries:
    """Page kept board scans with their follow-up state and screened-out counts."""

    def __init__(self, database: Database) -> None:
        """Bind scan-history reads to the application database."""
        self.database = database

    def list_scan_history(
        self,
        workspace_id: str,
        *,
        offset: int = 0,
        limit: int = 50,
        source_id: str = "",
        status: SourceScanHistoryStatus | None = None,
        latest_only: bool = False,
    ) -> SourceScanHistoryPageResponse:
        """Return a filtered page of kept scans; latest_only keeps each source's last board scan."""
        if offset < 0:
            raise ValidationError("Source scan offset cannot be negative.")
        if limit < 1 or limit > 250:
            raise ValidationError("Source scan limit must be between 1 and 250.")
        with self.database.session() as session:
            require_workspace_in(session, workspace_id)
            scan_filter: list[ColumnElement[bool]] = [
                Source.workspace_id == workspace_id,
                SourceScan.status.in_(get_args(SourceScanHistoryStatus)),
                SourceScan.id.in_(kept_scan_id_statement(workspace_id)),
            ]
            if source_id:
                scan_filter.append(SourceScan.source_id == source_id)
            if status is not None:
                scan_filter.append(SourceScan.status == status)
            if latest_only:
                scan_filter.append(
                    SourceScan.id.in_(_latest_scan_id_statement(workspace_id, board_only=True))
                )
            total = session.scalar(
                select(func.count())
                .select_from(SourceScan)
                .join(Source, Source.id == SourceScan.source_id)
                .where(*scan_filter)
            )
            scan_task_id = (
                select(BackgroundTask.id)
                .where(
                    BackgroundTask.id == SourceScan.correlation_id,
                    BackgroundTask.workspace_id == workspace_id,
                    BackgroundTask.task_type == "source.scan",
                )
                .scalar_subquery()
                .label("task_id")
            )
            rows = session.execute(
                select(SourceScan, Source, Employer.name, scan_task_id)
                .join(Source, Source.id == SourceScan.source_id)
                .outerjoin(Employer, Employer.id == Source.employer_id)
                .where(*scan_filter)
                .order_by(SourceScan.scanned_on.desc(), Source.priority, *LATEST_SCAN_FIRST[1:])
                .offset(offset)
                .limit(limit)
            ).all()
            scan_ids = [scan.id for scan, _source, _employer, _task_id in rows]
            frontier_counts, automatic_pending_counts = frontier_status_counts(session, scan_ids)
            follow_up_states = scan_follow_up_states(
                session, workspace_id, scan_ids, latest_scan_ids_in(session, workspace_id)
            )
            latest_of_kind: dict[str, bool] = dict(
                session.execute(_kept_scan_statement(workspace_id)).tuples().all()
            )
            screened_out_counts = _screened_out_counts(
                session,
                workspace_id,
                {(task_id, source.name) for _scan, source, _employer, task_id in rows if task_id},
            )
            return SourceScanHistoryPageResponse(
                items=[
                    SourceScanResponse(
                        id=scan.id,
                        task_id=task_id,
                        source_name=source.name,
                        employer=employer,
                        source_url=source.url,
                        source_type=source.source_type,
                        priority=source.priority,
                        enabled=source.enabled,
                        scanned_on=scan.scanned_on,
                        query_label=scan.query_label,
                        status=scan.status,
                        read_outcome=scan.read_outcome,
                        frontier_pending_count=frontier_counts.get((scan.id, "pending"), 0),
                        frontier_completed_count=frontier_counts.get((scan.id, "completed"), 0),
                        frontier_failed_count=frontier_counts.get((scan.id, "failed"), 0),
                        automatic_pending_count=automatic_pending_counts.get(scan.id, 0),
                        follow_up_state=follow_up_states.get(scan.id, "none"),
                        kept_as=_kept_as(scan, latest_of_kind.get(scan.id, False)),
                        kind=_scan_kind(scan.query_label),
                        screened_out_count=(
                            screened_out_counts.get((task_id, source.name), 0) if task_id else 0
                        ),
                        raw_count=scan.raw_count,
                        read_count=scan.read_count,
                        skip_reasons=SourceScanSkipCountsResponse.model_validate(
                            _zeroed_ints(SourceScanSkipCountsResponse, scan.skip_reasons)
                        ),
                        result_count=scan.result_count,
                        new_count=scan.new_count,
                        detail_attempted_count=scan.detail_attempted_count,
                        detail_read_count=scan.detail_read_count,
                        detail_failed_count=scan.detail_failed_count,
                        detail_interrupted_count=scan.detail_interrupted_count,
                        detail_capped_count=scan.detail_capped_count,
                        detail_failure_reasons=SourceScanDetailFailureReasonsResponse.model_validate(
                            _zeroed_ints(
                                SourceScanDetailFailureReasonsResponse,
                                scan.detail_failure_reasons,
                            )
                        ),
                        notes=scan.notes,
                        complete=scan.complete,
                        scan_started_at=scan.scan_started_at,
                        scan_completed_at=scan.scan_completed_at,
                        duration_ms=scan.duration_ms,
                    )
                    for scan, source, employer, task_id in rows
                ],
                total=total or 0,
                offset=offset,
                limit=limit,
            )
