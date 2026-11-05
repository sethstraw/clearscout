"""Frontier rows a new scan carries forward from the source's earlier scans."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime

from sqlalchemy import delete, null, or_, select, update
from sqlalchemy.orm import Session

from clearscout.application.discovery.source_continuation.limits import (
    CARRIED_ORIGIN,
    HELD_UNTIL_REEMITTED,
    POSTING_DETAIL_COMPLETED,
)
from clearscout.application.discovery.source_continuation.publisher_targets import (
    failed_publisher_search_detail,
    publisher_search_row_target,
    row_publisher_scope,
)
from clearscout.application.discovery.source_lifecycle import source_read_address
from clearscout.application.discovery.source_reading import SourceDetailCheckpoint
from clearscout.domain import new_id
from clearscout.infrastructure.persistence.models import (
    Source,
    SourceScan,
    SourceScanContinuation,
    SourceScanFrontierItem,
)
from clearscout.infrastructure.persistence.models.discovery import (
    release_unnamed_listing_memberships,
)
from clearscout.infrastructure.web_fetch.board_reads import scope_owned_by_root


def carried_frontier_item(
    continuation_id: str,
    row: SourceScanFrontierItem,
    *,
    correlation_id: str | None,
    relisted: frozenset[str],
) -> SourceScanFrontierItem:
    """Clone prior frontier state, giving a failed row its cross-scan retry as pending.

    A failed listed posting page retries when a later day's read lists it again; a failed
    search detail keeps its spent attempts until a fresh-details scan.
    """
    platform = str((row.detail_checkpoint or {}).get("platform", ""))
    if (
        (
            row.deferred_reason == "alignerr_search_page"
            and platform == "search:alignerr_search_page"
        )
        or (
            row.deferred_reason in {"phenom_page_pending", "host_held"}
            and platform.startswith("search:phenom_page:")
        )
        or (
            row.deferred_reason.endswith(("_search_page", "_search_detail"))
            and row.detail_checkpoint is not None
        )
    ):
        correlation_id = row.correlation_id
    relisted_page = (
        row.status == "failed"
        and row.deferred_reason.endswith("_posting_pending")
        and row.normalized_url in relisted
    )
    preserved_detail = failed_publisher_search_detail(row) and not relisted_page
    retrying = row.status == "failed" and not preserved_detail
    return SourceScanFrontierItem(
        id=new_id(),
        continuation_id=continuation_id,
        url=row.url,
        normalized_url=row.normalized_url,
        label=row.label,
        label_source=row.label_source,
        source_url=row.source_url,
        structural_context=row.structural_context,
        relations=list(row.relations),
        depth=row.depth,
        automatic_eligible=relisted_page or (not preserved_detail and row.automatic_eligible),
        deferred_reason=row.deferred_reason,
        detail_checkpoint=row.detail_checkpoint,
        status="pending" if retrying else row.status,
        attempt_count=0 if retrying else row.attempt_count,
        last_error="" if retrying else row.last_error,
        completed_at=None if retrying else row.completed_at,
        created_by="worker",
        origin=CARRIED_ORIGIN if retrying else row.origin,
        correlation_id=correlation_id,
    )


def _restart_reused_frontier(
    session: Session, continuation_id: str, emitted: set[str] | None
) -> None:
    """Keep completed posting reads and live pending rows, retry failed rows once, drop the rest."""
    rows = session.scalars(
        select(SourceScanFrontierItem).where(
            SourceScanFrontierItem.continuation_id == continuation_id
        )
    ).all()
    for row in rows:
        if failed_publisher_search_detail(row):
            row.automatic_eligible = False
            continue
        if row.status == "pending" and not held_row_lapsed(row, emitted):
            continue
        if row.status == "completed":
            target = publisher_search_row_target(row, reopen=True)
            if row.deferred_reason == POSTING_DETAIL_COMPLETED or (
                target is not None and target.deferred_reason.endswith("_search_detail")
            ):
                continue
        if (
            row.status == "failed"
            and row.origin != CARRIED_ORIGIN
            and not held_row_lapsed(row, emitted)
        ):
            row.status = "pending"
            row.attempt_count = 0
            row.last_error = ""
            row.origin = CARRIED_ORIGIN
            continue
        session.delete(row)


def reopen_stale_completed_postings(
    session: Session,
    continuation_id: str,
    cutoff: datetime,
    *,
    source: Source,
    correlation_id: str | None,
) -> None:
    """Return this continuation's posting reads completed before the cutoff to pending."""
    root, _ = source_read_address(source.url, source.resolved_url)
    for row in session.scalars(
        select(SourceScanFrontierItem).where(
            SourceScanFrontierItem.continuation_id == continuation_id,
            SourceScanFrontierItem.status == "completed",
            SourceScanFrontierItem.deferred_reason.in_((POSTING_DETAIL_COMPLETED, "")),
            or_(
                SourceScanFrontierItem.completed_at.is_(None),
                SourceScanFrontierItem.completed_at <= cutoff,
            ),
        )
    ):
        target = publisher_search_row_target(row, reopen=True)
        if target is not None and not scope_owned_by_root(target.source_url, root):
            target = None
        if row.deferred_reason != POSTING_DETAIL_COMPLETED and (
            target is None or not target.deferred_reason.endswith("_search_detail")
        ):
            continue
        row.status = "pending"
        row.attempt_count = 0
        row.completed_at = None
        row.deferred_reason = target.deferred_reason if target is not None else ""
        if target is not None and target.detail_checkpoint is not None:
            row.detail_checkpoint = target.detail_checkpoint.as_json()
            row.correlation_id = correlation_id
        # One known posting URL is finite work, so it re-reads without a manual step.
        row.automatic_eligible = True


def _search_detail_delisted(
    row: SourceScanFrontierItem, verified: Mapping[tuple[str, str], frozenset[str]]
) -> bool:
    """Return whether a verified listing for this detail's scope names neither its id nor URL."""
    if not row.deferred_reason.endswith("_search_detail") or not verified:
        return False
    scope = row_publisher_scope(row)
    if scope is None or scope not in verified:
        return False
    pending = SourceDetailCheckpoint.from_json(row.detail_checkpoint).pending_ids
    return not {*pending, row.normalized_url} & verified[scope]


def held_row_lapsed(
    row: SourceScanFrontierItem,
    emitted: set[str] | None,
    verified: Mapping[tuple[str, str], frozenset[str]] | None = None,
) -> bool:
    """Return whether a held manual row went undeclared by the fresh read.

    `emitted` is None when the read failed or was blocked, so it says nothing about the source.
    A pending search page cursor the fresh read did not re-emit lapses too, and a failed one
    lapses on any fresh read, which declares its own cursor for the same listing. A search
    detail lapses when `verified` holds its scope's membership and that omits it.
    """
    if emitted is None:
        return False
    if row.status in {"pending", "failed"} and _search_detail_delisted(row, verified or {}):
        return True
    if row.status == "failed" and row.deferred_reason.endswith("_search_page"):
        return row.deferred_reason != "alignerr_search_page"
    if row.status != "pending" or row.normalized_url in emitted:
        return False
    if row.deferred_reason.endswith("_search_page"):
        return row.deferred_reason != "alignerr_search_page"
    return not row.automatic_eligible and row.deferred_reason in HELD_UNTIL_REEMITTED


def _carried_prior_rows(
    session: Session,
    prior_continuation_id: str,
    emitted: set[str] | None,
    verified: Mapping[tuple[str, str], frozenset[str]],
) -> list[SourceScanFrontierItem]:
    """Return prior frontier rows still pending or eligible for one carry-forward retry."""
    rows = session.scalars(
        select(SourceScanFrontierItem).where(
            SourceScanFrontierItem.continuation_id == prior_continuation_id,
            SourceScanFrontierItem.status.in_(("pending", "failed")),
        )
    )
    return [
        row
        for row in rows
        if not held_row_lapsed(row, emitted, verified)
        and (
            row.status == "pending"
            or row.origin != CARRIED_ORIGIN
            or failed_publisher_search_detail(row)
        )
    ]


def latest_prior_rows(
    session: Session,
    source_id: str,
    source_scan_id: str,
    emitted: set[str] | None,
    verified: Mapping[tuple[str, str], frozenset[str]],
) -> tuple[list[SourceScanFrontierItem], date | None]:
    """Return carryable rows from the source's newest other scan's continuation, and its day."""
    # Only the newest other scan carries; when it left no continuation, nothing older does.
    prior = session.execute(
        select(SourceScanContinuation.id, SourceScan.scanned_on)
        .select_from(SourceScan)
        .outerjoin(SourceScanContinuation, SourceScanContinuation.source_scan_id == SourceScan.id)
        .where(SourceScan.source_id == source_id, SourceScan.id != source_scan_id)
        .order_by(SourceScan.scan_started_at.desc(), SourceScan.created_at.desc())
        .limit(1)
    ).first()
    if prior is None or prior.id is None:
        return [], None
    return _carried_prior_rows(session, prior.id, emitted, verified), prior.scanned_on


def release_superseded_checkpoints(session: Session, source_id: str, source_scan_id: str) -> None:
    """Drop checkpoints and memberships from every other scan's frontier of this source."""
    superseded = (
        select(SourceScanContinuation.id)
        .join(SourceScan, SourceScan.id == SourceScanContinuation.source_scan_id)
        .where(SourceScan.source_id == source_id, SourceScan.id != source_scan_id)
    )
    session.execute(
        update(SourceScanFrontierItem)
        .where(
            SourceScanFrontierItem.continuation_id.in_(superseded),
            or_(
                SourceScanFrontierItem.stored_checkpoint.is_not(None),
                SourceScanFrontierItem.listing_membership_sha256.is_not(None),
            ),
        )
        .values(stored_checkpoint=null(), listing_membership_sha256=None)
        .execution_options(synchronize_session="fetch")
    )
    # The after_flush release only runs when ORM frontier rows flush, which a Core UPDATE skips.
    release_unnamed_listing_memberships(session)


def advance_generation(
    session: Session,
    continuation: SourceScanContinuation,
    correlation_id: str | None,
    handoff_scopes: tuple[tuple[str, str, str], ...],
    *,
    carry_forward: bool,
    emitted: set[str] | None,
) -> None:
    """Start a rescan's generation, restarting or clearing the existing frontier."""
    continuation.generation += 1
    continuation.correlation_id = correlation_id
    continuation.handoff_scopes = [list(scope) for scope in handoff_scopes]
    if carry_forward:
        _restart_reused_frontier(session, continuation.id, emitted)
        return
    session.execute(
        delete(SourceScanFrontierItem).where(
            SourceScanFrontierItem.continuation_id == continuation.id
        )
    )
