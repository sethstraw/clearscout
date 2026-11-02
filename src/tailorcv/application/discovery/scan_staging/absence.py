"""Withdrawing the rows a complete read no longer lists, and checking first-time misses."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from tailorcv.application.discovery.pending import release_variants_of_withdrawn
from tailorcv.application.discovery.scan_staging.matching import listed_row_ids
from tailorcv.application.workspaces.records.access import audit_columns
from tailorcv.domain import PostingIdentityStatus, PostingStatus, utc_now
from tailorcv.domain.staged_facts import STAGED_FACTS_VERSIONS, FreshnessState, StagedListingFacts
from tailorcv.infrastructure.persistence.models import (
    AuditEvent,
    BackgroundTask,
    PendingDiscovery,
    PendingPostingContent,
    SourceScan,
)

if TYPE_CHECKING:
    from tailorcv.application.tasks.commands import TaskCommands


@dataclass(frozen=True)
class WithdrawingRead:
    """The counts of the read whose coverage withdraws absent rows."""

    raw_count: int
    read_count: int
    complete: bool
    listing_pages: int = 0
    started_at: datetime | None = None

    @property
    def multi_page(self) -> bool:
        """Return whether the read spanned more than one listing page."""
        return self.listing_pages > 1

    def prior_mark(self, absent_since: datetime | None) -> bool:
        """Return whether an absence mark came from a read before this one."""
        if absent_since is None:
            return False
        return self.started_at is None or absent_since < self.started_at

    def as_details(self) -> dict[str, int | bool]:
        """Return the counts as audit event details."""
        return {
            "raw_count": self.raw_count,
            "read_count": self.read_count,
            "complete": self.complete,
            "listing_pages": self.listing_pages,
        }


# Receives, in the withdrawing transaction, the rows a multi-page read missed for the first time.
type AbsenceCheck = Callable[[Session, list[str]], None]


ABSENCE_CHECK_BATCH = 250


def queue_absence_checks(
    commands: TaskCommands, workspace_id: str, correlation_id: str | None
) -> AbsenceCheck:
    """Queue a free publisher and body refresh for each row a multi-page read missed."""

    def queue(session: Session, pending_ids: list[str]) -> None:
        ordered = sorted(pending_ids)
        # Each marking gets its own key, so a later re-mark of the same rows queues afresh.
        marking = f"{correlation_id or 'none'}:{uuid4().hex}"
        for start in range(0, len(ordered), ABSENCE_CHECK_BATCH):
            batch = ordered[start : start + ABSENCE_CHECK_BATCH]
            digest = hashlib.sha256("\n".join(batch).encode()).hexdigest()[:16]
            commands.enqueue_in_session(
                session,
                workspace_id=workspace_id,
                task_type="discovery.refresh_batch",
                payload={"pending_discovery_ids": batch},
                idempotency_key=f"absence-check:{marking}:{digest}",
                actor="worker",
                origin="source.scan",
                correlation_id=correlation_id,
            )

    return queue


def _pending_absence_checks(session: Session, workspace_id: str) -> set[str]:
    """Return the rows whose queued or running absence check has not settled."""
    payloads = session.scalars(
        select(BackgroundTask.payload).where(
            BackgroundTask.workspace_id == workspace_id,
            BackgroundTask.task_type == "discovery.refresh_batch",
            BackgroundTask.idempotency_key.like("absence-check:%"),
            BackgroundTask.status.in_(["queued", "running"]),
        )
    )
    return {str(item) for payload in payloads for item in payload["pending_discovery_ids"]}


def _verified_active_since(
    session: Session, row: PendingDiscovery, since: datetime | None
) -> PendingPostingContent | None:
    """Return the row's current content when it is verified active and newer than ``since``."""
    if not row.current_content_id:
        return None
    evidence = session.get(PendingPostingContent, row.current_content_id)
    if (
        evidence is None
        or evidence.workspace_id != row.workspace_id
        or evidence.pending_discovery_id != row.id
        or evidence.posting_status != PostingStatus.ACTIVE
        or evidence.identity_status != PostingIdentityStatus.VERIFIED
    ):
        return None
    return evidence if since is None or evidence.fetched_at > since else None


def _record_absent_but_verified_active(
    session: Session,
    row: PendingDiscovery,
    evidence: PendingPostingContent,
    source_id: str,
    correlation_id: str | None,
) -> None:
    """Append one event per evidence read that kept an absent row visible."""
    action = "pending_discovery.absent_but_verified_active"
    recorded = session.scalars(
        select(AuditEvent).where(
            AuditEvent.workspace_id == row.workspace_id,
            AuditEvent.action == action,
            AuditEvent.entity_id == row.id,
        )
    )
    if any(event.details.get("evidence_id") == evidence.id for event in recorded):
        return
    session.add(
        AuditEvent(
            workspace_id=row.workspace_id,
            action=action,
            entity_type="pending_discovery",
            entity_id=row.id,
            details={
                "discovery_url": row.discovery_url,
                "source_id": source_id,
                "evidence_id": evidence.id,
            },
            **audit_columns("worker", "source.scan", correlation_id),
        )
    )


def _mark_verified_current(row: PendingDiscovery) -> None:
    """Set a kept row's staged freshness to verified current."""
    stored_facts = row.staged_facts
    if stored_facts and stored_facts.get("schema_version") not in STAGED_FACTS_VERSIONS:
        # This reader returns a blank record for a document it cannot read, so an
        # older one takes the freshness key in place and keeps its stored facts.
        row.staged_facts = {**stored_facts, "freshness": FreshnessState.VERIFIED_CURRENT.value}
        return
    row.staged_facts = replace(
        StagedListingFacts.from_json(stored_facts),
        freshness=FreshnessState.VERIFIED_CURRENT,
    ).as_json()


def _withdraw_absent_row(
    session: Session,
    row: PendingDiscovery,
    source_id: str,
    now: datetime,
    read: WithdrawingRead | None,
    correlation_id: str | None,
) -> None:
    """Withdraw one row a complete read missed and record why."""
    row.absent_since = None
    row.withdrawn_at = now
    session.add(
        AuditEvent(
            workspace_id=row.workspace_id,
            action="pending_discovery.withdrawn_absent",
            entity_type="pending_discovery",
            entity_id=row.id,
            details={
                "discovery_url": row.discovery_url,
                "source_id": source_id,
                **(read.as_details() if read is not None else {}),
            },
            **audit_columns("worker", "source.scan", correlation_id),
        )
    )


def withdraw_absent_in_session(
    session: Session,
    workspace_id: str,
    source_id: str,
    present_urls: Iterable[str],
    today: date,
    *,
    correlation_id: str | None = None,
    read: WithdrawingRead | None = None,
    check_absent: AbsenceCheck | None = None,
) -> list[str]:
    """Withdraw absent rows inside the caller's transaction.

    Only ever called once a read or a drained traversal covers the whole
    source, because absence from a partial one says nothing. ``present_urls``
    is everything that coverage listed or fetched. A read speaks for the
    source that made it, so a row another source staged is left alone even
    when it names the same employer at a different address. The row stays in
    the queue carrying the date it went, so a person sees that it was pulled
    rather than finding it silently gone. Content verified active after the
    last complete read from a prior day keeps a row. A multi-page read only
    marks a row it misses for the first time and hands it to ``check_absent``;
    the next complete read that also misses it withdraws it.
    """
    now = utc_now()
    gone: list[str] = []
    withdrawn: list[str] = []
    previous_complete_read_at = session.scalar(
        select(SourceScan.scan_completed_at)
        .where(
            SourceScan.source_id == source_id,
            SourceScan.complete.is_(True),
            SourceScan.scanned_on < today,
        )
        .order_by(SourceScan.scanned_on.desc(), SourceScan.scan_completed_at.desc())
        .limit(1)
    )
    staged = session.scalars(
        select(PendingDiscovery).where(
            PendingDiscovery.workspace_id == workspace_id,
            PendingDiscovery.source_id == source_id,
            PendingDiscovery.archived_at.is_(None),
        )
    ).all()
    listed = listed_row_ids(staged, list(present_urls))
    unchecked: list[str] = []
    checking: set[str] | None = None
    for row in staged:
        if row.id in listed:
            row.absent_since = None
            continue
        evidence = _verified_active_since(session, row, previous_complete_read_at)
        if evidence is not None:
            row.absent_since = None
            _record_absent_but_verified_active(session, row, evidence, source_id, correlation_id)
            _mark_verified_current(row)
            continue
        first_paged_miss = (
            read is not None and read.multi_page and not read.prior_mark(row.absent_since)
        )
        if checking is None and read is not None and read.multi_page:
            checking = _pending_absence_checks(session, workspace_id)
        if row.withdrawn_at is None and (first_paged_miss or row.id in (checking or ())):
            if row.absent_since is None:
                row.absent_since = now
                unchecked.append(row.id)
            continue
        if row.withdrawn_at is None:
            _withdraw_absent_row(session, row, source_id, now, read, correlation_id)
            gone.append(row.title)
            withdrawn.append(row.id)
    if unchecked and check_absent is not None:
        check_absent(session, unchecked)
    # Released after the loop, so a copy this read also withdrew stays in its family.
    release_variants_of_withdrawn(
        session,
        workspace_id,
        withdrawn,
        actor="worker",
        origin="source.scan",
        correlation_id=correlation_id,
    )
    return gone
