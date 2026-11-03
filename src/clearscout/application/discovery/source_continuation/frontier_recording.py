"""A scan's targets written into its frontier, with listing receipts and listed bodies."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from clearscout.application.discovery.source_continuation.chunk import Chunk
from clearscout.application.discovery.source_continuation.frontier_refresh import (
    refresh_classification_hold,
    refresh_declared_page_scope,
    refresh_declared_search_detail,
    refresh_reemitted_cursor,
)
from clearscout.application.discovery.source_continuation.limits import (
    CARRIED_ORIGIN,
    POSTING_DETAIL_COMPLETED,
)
from clearscout.application.discovery.source_continuation.publisher_targets import (
    publisher_search_row_target,
)
from clearscout.application.discovery.source_continuation.rescreen import recent_posting_reads
from clearscout.application.discovery.source_continuation.targets import (
    LISTED_POSTING_SUFFIX,
    ListedPostingCredit,
    continuation_frontier_counts,
    frontier_item,
    listed_posting,
    read_page_item,
    targets_by_url,
)
from clearscout.application.discovery.source_lifecycle import source_read_address
from clearscout.application.discovery.source_reading import (
    SourceContinuationTarget,
    SourceDetailCheckpoint,
    SourceRead,
)
from clearscout.domain import utc_now
from clearscout.domain.identity import normalize_fetch_url
from clearscout.infrastructure.persistence.models import Source, SourceScanFrontierItem
from clearscout.infrastructure.web_fetch.board_adapters import identify_board_url
from clearscout.infrastructure.web_fetch.board_reads import (
    publisher_search_scope,
    scope_owned_by_root,
)
from clearscout.infrastructure.web_fetch.boards import board_read_scope
from clearscout.infrastructure.web_fetch.platforms.workday import workday_site_root


def _merge_native_listing_receipt(
    row: SourceScanFrontierItem,
    receipt: SourceDetailCheckpoint,
    correlation_id: str | None,
) -> SourceDetailCheckpoint:
    """Union native observations without inheriting another scan family's bodies."""
    if (
        not receipt.platform.startswith("listed:")
        or row.correlation_id != correlation_id
        or not row.detail_checkpoint
    ):
        return receipt
    prior = SourceDetailCheckpoint.from_json(row.detail_checkpoint)
    if prior.platform != receipt.platform:
        return receipt
    membership = dict((*prior.listing_urls, *receipt.listing_urls))
    completed = dict((*prior.completed, *receipt.completed))
    protected = dict((*prior.protected, *receipt.protected))
    return replace(
        receipt,
        listing_ids=tuple(membership),
        listing_urls=tuple(membership.items()),
        completed=tuple(
            (identity, url)
            for identity, url in completed.items()
            if membership.get(identity) == url
        ),
        protected=tuple(
            (identity, url)
            for identity, url in protected.items()
            if membership.get(identity) == url and identity not in completed
        ),
    )


def _untried_carried_retry(row: SourceScanFrontierItem) -> bool:
    """Return whether a row is a carried retry no read has attempted yet."""
    return row.origin == CARRIED_ORIGIN and row.attempt_count == 0


def _record_native_completed_rows(
    session: Session,
    continuation_id: str,
    source_url: str,
    receipt: SourceDetailCheckpoint,
    correlation_id: str | None,
    rows: dict[str, SourceScanFrontierItem],
    read_now: frozenset[tuple[str, str]],
) -> None:
    """Retain original full bodies, settling a failed row or a carried retry read just now."""
    if not receipt.platform.startswith("listed:"):
        return
    for identity, url in receipt.completed:
        normalized_posting = normalize_fetch_url(url)
        posting = rows.get(normalized_posting)
        if posting is not None and (
            (
                posting.status == "pending"
                and not (_untried_carried_retry(posting) and (identity, url) in read_now)
            )
            or (posting.status == "completed" and posting.detail_checkpoint is not None)
        ):
            continue
        if posting is None:
            posting = read_page_item(
                continuation_id, normalized_posting, url, correlation_id=correlation_id
            )
            session.add(posting)
            rows[normalized_posting] = posting
        elif posting.status in {"failed", "pending"}:
            posting.status = "completed"
            posting.last_error = ""
            posting.completed_at = utc_now()
        posting.source_url = source_url
        posting.correlation_id = correlation_id
        posting.deferred_reason = POSTING_DETAIL_COMPLETED
        posting.detail_checkpoint = SourceDetailCheckpoint(
            platform=receipt.platform.removeprefix("listed:"),
            board_url=url,
            listing_ids=(identity,),
            listing_urls=((identity, url),),
            completed=((identity, url),),
        ).as_json()


def _root_navigation_row(
    row: SourceScanFrontierItem, receipt: SourceDetailCheckpoint, root: str
) -> bool:
    """Whether a listed receipt at the source root lands on an unread navigation row."""
    return (
        receipt.platform.startswith("listed:")
        and row.normalized_url == root
        and row.status != "completed"
        and row.detail_checkpoint is None
    )


def record_publisher_listing_receipts(
    session: Session,
    continuation_id: str,
    source_id: str,
    receipts: tuple[SourceDetailCheckpoint, ...],
    correlation_id: str | None,
    rows: dict[str, SourceScanFrontierItem],
) -> None:
    """Retain scoped publisher enumeration independently of detail recovery."""
    source = session.get(Source, source_id)
    root = (
        normalize_fetch_url(source_read_address(source.url, source.resolved_url)[0])
        if source is not None
        else None
    )
    for incoming in receipts:
        receipt = incoming
        normalized = normalize_fetch_url(receipt.board_url)
        candidate = read_page_item(
            continuation_id, normalized, receipt.board_url, correlation_id=correlation_id
        )
        candidate.depth = 1
        if receipt.platform.startswith("listed:") and root is not None:
            candidate.source_url = root
        candidate.detail_checkpoint = receipt.as_json()
        target = publisher_search_row_target(candidate)
        scope = publisher_search_scope(target) if target is not None else None
        if (
            scope is None
            or scope[0] == "alignerr"
            or root is None
            or not scope_owned_by_root(scope[1], root)
        ):
            continue
        row = rows.get(normalized)
        read_now = frozenset(receipt.completed)
        if row is not None and _root_navigation_row(row, receipt, root):
            # A navigation link to the root names the very page this read fetched.
            row.status = "completed"
            row.deferred_reason = ""
            row.depth = 1
            row.automatic_eligible = True
            row.last_error = ""
            row.completed_at = utc_now()
            row.url = candidate.url
            row.label = candidate.label
            row.label_source = candidate.label_source
            row.source_url = candidate.source_url
            row.structural_context = candidate.structural_context
            row.relations = list(candidate.relations)
            row.correlation_id = correlation_id
            row.detail_checkpoint = receipt.as_json()
        elif row is not None:
            if row.status != "completed":
                continue
            receipt = _merge_native_listing_receipt(row, receipt, correlation_id)
            row.detail_checkpoint = receipt.as_json()
            row.source_url = candidate.source_url
            row.correlation_id = correlation_id
        else:
            session.add(candidate)
            rows[normalized] = candidate
        _record_native_completed_rows(
            session, continuation_id, candidate.source_url, receipt, correlation_id, rows, read_now
        )


def chunk_listed_read(
    session: Session, continuation_id: str, chunk: Chunk, read: SourceRead | None
) -> ListedPostingCredit | None:
    """Return the listed-posting credit of a chunk that read one or drained the frontier."""
    listed = [item.id for item in chunk.items if listed_posting(item)]
    absorbed = (
        session.scalar(
            select(func.count()).where(
                SourceScanFrontierItem.id.in_(listed),
                SourceScanFrontierItem.status == "completed",
                SourceScanFrontierItem.deferred_reason == POSTING_DETAIL_COMPLETED,
            )
        )
        if listed
        else 0
    ) or 0
    _pending, unresolved = continuation_frontier_counts(session, continuation_id)
    # A frontier that never held a posting body has no listed cut to drain.
    drained = unresolved == 0 and (
        absorbed > 0
        or session.scalar(
            select(SourceScanFrontierItem.id)
            .where(
                SourceScanFrontierItem.continuation_id == continuation_id,
                SourceScanFrontierItem.deferred_reason == POSTING_DETAIL_COMPLETED,
            )
            .limit(1)
        )
        is not None
    )
    if not absorbed and not drained:
        return None
    owed = session.scalar(
        select(func.count()).where(
            SourceScanFrontierItem.continuation_id == continuation_id,
            SourceScanFrontierItem.status.in_(("pending", "failed")),
            SourceScanFrontierItem.deferred_reason.endswith(LISTED_POSTING_SUFFIX),
        )
    )
    reader_counts = absorbed > 0 and (
        (read is not None and read.detail_read_urls is not None)
        or any(
            str((stored or {}).get("platform", "")).startswith("listed:")
            for stored in session.scalars(
                select(SourceScanFrontierItem.stored_checkpoint).where(
                    SourceScanFrontierItem.continuation_id == continuation_id,
                    SourceScanFrontierItem.stored_checkpoint.is_not(None),
                )
            )
        )
    )
    return ListedPostingCredit(absorbed, owed or 0, drained, reader_counts)


def _absorbed_listed_posting(
    target: SourceContinuationTarget, row: SourceScanFrontierItem | None, *, read_recently: bool
) -> bool:
    """Whether a deferred listed posting's body is already held by a completed read."""
    if not listed_posting(target):
        return False
    if row is None:
        return read_recently
    return row.status == "completed" and row.deferred_reason == POSTING_DETAIL_COMPLETED


def _recent_body_item(
    continuation_id: str,
    normalized: str,
    target: SourceContinuationTarget,
    read_at: datetime,
    correlation_id: str | None,
) -> SourceScanFrontierItem:
    """Record a target a recent read already supplied, as a held body when it is a posting."""
    item = read_page_item(continuation_id, normalized, target.url, correlation_id=correlation_id)
    checkpoint = target.detail_checkpoint
    if not listed_posting(target) or checkpoint is None or not checkpoint.listing_urls:
        return item
    # A listing certification counts only completed pairs, so the held body names its own.
    item.source_url = target.source_url or item.source_url
    item.deferred_reason = POSTING_DETAIL_COMPLETED
    # The original read time keeps the rescreen window from renewing on every skip.
    item.completed_at = read_at
    item.detail_checkpoint = replace(
        checkpoint, pending_ids=(), completed=checkpoint.listing_urls
    ).as_json()
    return item


def record_frontier_targets(
    session: Session,
    continuation_id: str,
    source_id: str,
    *,
    targets: tuple[SourceContinuationTarget, ...],
    read_urls: tuple[str, ...],
    completed_workday_urls: tuple[str, ...],
    cutoff: datetime,
    correlation_id: str | None,
    publisher_listing_receipts: tuple[SourceDetailCheckpoint, ...] = (),
) -> tuple[int, int, int]:
    """Reconcile declared detail work, queue new targets, and retain reads as coverage.

    Returns the inserted count, the deferred listed postings already read, and
    all deferred listed postings.
    """
    rows = {
        row.normalized_url: row
        for row in session.scalars(
            select(SourceScanFrontierItem).where(
                SourceScanFrontierItem.continuation_id == continuation_id
            )
        )
    }
    held = set(rows)
    deferred = targets_by_url(targets)
    for url in completed_workday_urls:
        normalized = normalize_fetch_url(url)
        row = rows.get(normalized)
        identity = identify_board_url(url)
        scope = (
            board_read_scope(workday_site_root(identity[1]), "workday")
            if identity is not None and identity[0] == "workday"
            else None
        )
        if (
            row is None
            or normalized in deferred
            or scope is None
            or row.deferred_reason != "workday_detail_pending"
            or row.detail_checkpoint is None
        ):
            continue
        checkpoint = SourceDetailCheckpoint.from_json(row.detail_checkpoint)
        if checkpoint.platform != "workday" or scope != board_read_scope(
            checkpoint.board_url, "workday"
        ):
            continue
        row.status = "completed"
        row.deferred_reason = POSTING_DETAIL_COMPLETED
        row.completed_at = utc_now()
        row.last_error = ""
        row.detail_checkpoint = None
    recent = recent_posting_reads(
        session, source_id, cutoff, exclude_continuation_id=continuation_id
    )
    inserted_count = absorbed_count = listed_count = 0
    for normalized, target in deferred.items():
        listed_count += listed_posting(target)
        row = rows.get(normalized)
        if row is not None:
            refresh_declared_page_scope(row, target, correlation_id)
            refresh_classification_hold(row, target)
            refresh_declared_search_detail(session, source_id, row, target, correlation_id)
            refresh_reemitted_cursor(row, target, correlation_id)
            absorbed_count += _absorbed_listed_posting(target, row, read_recently=False)
            continue
        held.add(normalized)
        # A recent read already supplied this URL, so it is coverage this
        # traversal holds rather than work it still owes.
        if normalized in recent:
            absorbed_count += _absorbed_listed_posting(target, None, read_recently=True)
            session.add(
                _recent_body_item(
                    continuation_id, normalized, target, recent[normalized], correlation_id
                )
            )
            continue
        session.add(
            frontier_item(
                continuation_id,
                normalized,
                target,
                origin="source.scan",
                correlation_id=correlation_id,
            )
        )
        inserted_count += 1
    record_publisher_listing_receipts(
        session, continuation_id, source_id, publisher_listing_receipts, correlation_id, rows
    )
    held.update(rows)
    for url in read_urls:
        normalized = normalize_fetch_url(url)
        if not normalized or normalized in held:
            continue
        held.add(normalized)
        session.add(read_page_item(continuation_id, normalized, url, correlation_id=correlation_id))
    return inserted_count, absorbed_count, listed_count
