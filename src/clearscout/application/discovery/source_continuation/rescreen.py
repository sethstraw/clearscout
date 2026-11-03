"""Completed posting reads, and the cutoff after which a traversal reads one again."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from clearscout.application.discovery.source_continuation.family import fresh_root_task
from clearscout.application.discovery.source_continuation.limits import POSTING_DETAIL_COMPLETED
from clearscout.domain import SearchProposalState, utc_now
from clearscout.infrastructure.persistence.models import (
    DerivedSearchTitle,
    JobSearchParameterSet,
    SourceScan,
    SourceScanContinuation,
    SourceScanFrontierItem,
)
from clearscout.infrastructure.persistence.models.discovery import SourceScanMember

# A completed posting read older than this, or older than the current search
# parameters, is read again.
RESCREEN_COMPLETED_AFTER = timedelta(days=7)


def completed_posting_reads(
    session: Session, source_id: str, *, exclude_continuation_id: str | None = None
) -> dict[str, datetime | None]:
    """Return each posting-detail URL this source completed, with its latest read time."""
    statement = (
        select(
            SourceScanFrontierItem.normalized_url,
            func.max(SourceScanFrontierItem.completed_at),
        )
        .join(
            SourceScanContinuation,
            SourceScanContinuation.id == SourceScanFrontierItem.continuation_id,
        )
        .join(SourceScan, SourceScan.id == SourceScanContinuation.source_scan_id)
        .where(
            SourceScan.source_id == source_id,
            SourceScanFrontierItem.status == "completed",
            SourceScanFrontierItem.deferred_reason == POSTING_DETAIL_COMPLETED,
            *(
                (SourceScanFrontierItem.continuation_id != exclude_continuation_id,)
                if exclude_continuation_id is not None
                else ()
            ),
        )
        .group_by(SourceScanFrontierItem.normalized_url)
    )
    return dict(session.execute(statement).tuples().all())


def read_after(reads: dict[str, datetime | None], cutoff: datetime) -> dict[str, datetime]:
    """Return the URLs whose latest completed read is after the cutoff, with that read time."""
    return {url: latest for url, latest in reads.items() if latest is not None and latest > cutoff}


def recent_posting_reads(
    session: Session,
    source_id: str,
    cutoff: datetime,
    *,
    exclude_continuation_id: str | None = None,
) -> dict[str, datetime]:
    """Return the posting URLs this source completed reading after ``cutoff``.

    A failed read in a frontier created after a URL's latest completion ends that credit.
    """
    recent = read_after(
        completed_posting_reads(
            session, source_id, exclude_continuation_id=exclude_continuation_id
        ),
        cutoff,
    )
    failed = session.execute(
        select(SourceScanFrontierItem.normalized_url, func.max(SourceScanFrontierItem.created_at))
        .join(
            SourceScanContinuation,
            SourceScanContinuation.id == SourceScanFrontierItem.continuation_id,
        )
        .join(SourceScan, SourceScan.id == SourceScanContinuation.source_scan_id)
        .where(
            SourceScan.source_id == source_id,
            SourceScanFrontierItem.status == "failed",
            SourceScanFrontierItem.normalized_url.in_(list(recent)),
        )
        .group_by(SourceScanFrontierItem.normalized_url)
    )
    for url, failed_at in failed.tuples():
        if failed_at > recent[url]:
            del recent[url]
    return recent


def rescreen_cutoff(
    session: Session,
    workspace_id: str,
    continuation_id: str | None = None,
    *,
    root_task_id: str | None = None,
) -> datetime:
    """Return the cutoff for completed posting reads this traversal must repeat."""
    current = session.scalars(
        select(JobSearchParameterSet.effective_at)
        .where(JobSearchParameterSet.workspace_id == workspace_id)
        .order_by(JobSearchParameterSet.version.desc())
        .limit(1)
    ).first()
    # An accepted title widens screening without writing a new parameter set.
    accepted = session.scalar(
        select(func.max(DerivedSearchTitle.decided_at)).where(
            DerivedSearchTitle.workspace_id == workspace_id,
            DerivedSearchTitle.state == SearchProposalState.ACCEPTED.value,
        )
    )
    root_task_id = root_task_id or (
        session.scalar(
            select(SourceScanMember.root_task_id)
            .join(
                SourceScanContinuation,
                SourceScanContinuation.batch_member_id == SourceScanMember.id,
            )
            .where(SourceScanContinuation.id == continuation_id)
        )
        if continuation_id is not None
        else None
    )
    fresh_root = fresh_root_task(session, root_task_id)
    return max(
        instant
        for instant in (
            utc_now() - RESCREEN_COMPLETED_AFTER,
            current,
            accepted,
            fresh_root.created_at if fresh_root is not None else None,
        )
        if instant is not None
    )


def recent_root_posting_reads(
    session: Session, workspace_id: str, source_id: str, root_task_id: str | None
) -> frozenset[str]:
    """Return the posting URLs this source read recently enough for the root's scan to skip."""
    cutoff = rescreen_cutoff(session, workspace_id, root_task_id=root_task_id)
    return frozenset(recent_posting_reads(session, source_id, cutoff))
