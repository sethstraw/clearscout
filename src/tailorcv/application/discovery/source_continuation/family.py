"""Which continuation is current, and the task family a scan's reads belong to."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from tailorcv.infrastructure.persistence.models import (
    BackgroundTask,
    Employer,
    Source,
    SourceScan,
    SourceScanContinuation,
    SourceScanFrontierItem,
)
from tailorcv.infrastructure.persistence.models.discovery import SourceScanMember

# A source's latest scan; same-day guided and board scans tie on scanned_on.
LATEST_SCAN_FIRST = (
    SourceScan.scanned_on.desc(),
    SourceScan.scan_completed_at.desc(),
    SourceScan.created_at.desc(),
    SourceScan.id.desc(),
)


def fresh_root_task(session: Session, root_task_id: str | None) -> BackgroundTask | None:
    """Find the explicit fresh scan retained by a batch and its resumes."""
    task = session.get(BackgroundTask, root_task_id) if root_task_id is not None else None
    workspace_id = task.workspace_id if task is not None else None
    visited: set[str] = set()
    while task is not None and task.id not in visited:
        if task.task_type != "source.scan" or task.workspace_id != workspace_id:
            return None
        if task.payload.get("fresh_details") is True:
            return task
        visited.add(task.id)
        previous_id = task.payload.get("resume_from_task_id")
        task = session.get(BackgroundTask, previous_id) if isinstance(previous_id, str) else None
    return None


def current_scan_family_ids(
    session: Session, continuation: SourceScanContinuation
) -> frozenset[str]:
    """Return the ids of every task in this continuation's scan family."""
    scan = session.get(SourceScan, continuation.source_scan_id)
    roots = {continuation.correlation_id}
    if scan is not None:
        roots.add(scan.correlation_id)
    ids = set(
        session.scalars(
            select(BackgroundTask.id).where(
                BackgroundTask.workspace_id == continuation.workspace_id,
                BackgroundTask.task_type == "source.continue",
                BackgroundTask.payload["continuation_id"].as_string() == continuation.id,
                BackgroundTask.payload["generation"].as_integer() == continuation.generation,
            )
        )
    )
    for root in roots:
        if root is not None:
            ids.add(root)
            ids.update(continuation_task_ids(session, continuation.workspace_id, root))
    return frozenset(ids)


def is_current_in_session(
    session: Session, continuation: SourceScanContinuation, generation: int
) -> bool:
    """Return whether a continuation generation is still its source's latest scan."""
    if continuation.generation != generation:
        return False
    if continuation.batch_member_id is not None:
        member = session.get(SourceScanMember, continuation.batch_member_id)
        if (
            member is None
            or member.generation != generation
            or member.cancellation_requested_at is not None
        ):
            return False
    scan = session.get(SourceScan, continuation.source_scan_id)
    if scan is None:
        return False
    source = session.get(Source, scan.source_id)
    if source is None or source.archived_at is not None:
        return False
    if source.employer_id:
        employer = session.get(Employer, source.employer_id)
        if employer is None or employer.archived_at is not None:
            return False
    latest_scan_id = session.scalar(
        select(SourceScan.id)
        .where(SourceScan.source_id == scan.source_id)
        .order_by(*LATEST_SCAN_FIRST)
        .limit(1)
    )
    return latest_scan_id == scan.id


def next_pending(
    session: Session, continuation_id: str, *, automatic_only: bool
) -> tuple[str, int] | None:
    """Return the next frontier identity and attempt number in stable order."""
    statement = select(SourceScanFrontierItem.id, SourceScanFrontierItem.attempt_count).where(
        SourceScanFrontierItem.continuation_id == continuation_id,
        SourceScanFrontierItem.status == "pending",
    )
    if automatic_only:
        statement = statement.where(SourceScanFrontierItem.automatic_eligible.is_(True))
    return (
        session.execute(
            statement.order_by(
                SourceScanFrontierItem.created_at,
                SourceScanFrontierItem.normalized_url,
                SourceScanFrontierItem.id,
            ).limit(1)
        )
        .tuples()
        .first()
    )


def continuation_task_ids(session: Session, workspace_id: str, scan_task_id: str) -> list[str]:
    """Return the continuation tasks that read the deferred links of one scan task."""
    # A same-day rescan reuses the scan row and advances its continuation, so a
    # scan task owns generations, not rows: the one it holds now and each one
    # whose first automatic task it enqueued.
    continuation_id = BackgroundTask.payload["continuation_id"].as_string()
    generation = BackgroundTask.payload["generation"].as_integer()
    continuation_tasks = (
        BackgroundTask.workspace_id == workspace_id,
        BackgroundTask.task_type == "source.continue",
    )
    owned = {
        *session.execute(
            select(SourceScanContinuation.id, SourceScanContinuation.generation).where(
                SourceScanContinuation.workspace_id == workspace_id,
                SourceScanContinuation.correlation_id == scan_task_id,
            )
        ).tuples(),
        *session.execute(
            select(continuation_id, generation).where(
                *continuation_tasks, BackgroundTask.correlation_id == scan_task_id
            )
        ).tuples(),
    }
    if not owned:
        return []
    rows = session.execute(
        select(BackgroundTask.id, continuation_id, generation)
        .where(*continuation_tasks, continuation_id.in_({owner for owner, _ in owned}))
        .order_by(BackgroundTask.created_at, BackgroundTask.id)
    ).tuples()
    return [task_id for task_id, owner, read in rows if (owner, read) in owned]
