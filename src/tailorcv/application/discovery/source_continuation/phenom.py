"""Phenom page checkpoints, retained reads, and listing coverage."""

from __future__ import annotations

from dataclasses import replace

from sqlalchemy import select
from sqlalchemy.orm import Session

from tailorcv.application.discovery.source_continuation.family import current_scan_family_ids
from tailorcv.application.discovery.source_lifecycle import source_read_address
from tailorcv.application.discovery.source_reading import (
    SourceDetailCheckpoint,
    SourceDetailCoverage,
    SourceRead,
)
from tailorcv.domain import SourceReadOutcome
from tailorcv.domain.identity import normalize_fetch_url
from tailorcv.infrastructure.persistence.models import (
    Source,
    SourceScan,
    SourceScanContinuation,
    SourceScanFrontierItem,
)


def preserve_unreadable_phenom_retry(
    items: list[SourceScanFrontierItem], read: SourceRead
) -> SourceRead:
    """Keep a Phenom page that still lacks posting identity charged as unreadable."""
    if (
        len(items) == 1
        and not read.listings
        and read.skip_reasons.get("phenom_listing_unstable")
        and items[0].deferred_reason == "phenom_page_pending"
        and items[0].last_error.startswith("missing_required_posting_identity: ")
    ):
        return replace(
            read,
            raw_count=0,
            outcome=SourceReadOutcome.NO_SUPPORTED_REPRESENTATION,
            deferred_targets=(),
            note=items[0].last_error,
        )
    return read


def _phenom_checkpoint(
    row: SourceScanFrontierItem, root: str, family: frozenset[str]
) -> SourceDetailCheckpoint | None:
    if (
        not (
            row.deferred_reason in {"phenom_page_pending", "host_held"}
            or (row.status == "completed" and not row.deferred_reason)
        )
        or row.correlation_id not in family
        or row.detail_checkpoint is None
        or not str(row.detail_checkpoint.get("platform", "")).startswith("search:phenom_page:")
    ):
        return None
    checkpoint = SourceDetailCheckpoint.from_json(row.detail_checkpoint)
    if (
        not checkpoint.platform.startswith("search:phenom_page:")
        or bool(checkpoint.completed)
        or normalize_fetch_url(checkpoint.board_url) != root
        or normalize_fetch_url(row.source_url) != root
    ):
        return None
    return checkpoint


def _phenom_read_checkpoint(
    items: list[SourceScanFrontierItem],
    read: SourceRead,
    root: str,
    family: frozenset[str],
) -> SourceDetailCheckpoint | None:
    selected = [
        checkpoint
        for item in items
        if (checkpoint := _phenom_checkpoint(item, root, family)) is not None
    ]
    incoming = [
        target.detail_checkpoint
        for target in read.deferred_targets
        if target.detail_checkpoint is not None
    ]
    if (
        not selected
        or len(selected) != len(items)
        or any(
            checkpoint.platform != selected[0].platform
            or normalize_fetch_url(checkpoint.board_url) != root
            for checkpoint in (*selected, *incoming)
        )
    ):
        return None
    if any(not listing.requisition_id for listing in read.listings):
        return None
    membership = dict(
        pair for checkpoint in (*selected, *incoming) for pair in checkpoint.listing_urls
    )
    membership.update((listing.requisition_id, listing.url) for listing in read.listings)
    unstable = any(checkpoint.listing_unstable for checkpoint in (*selected, *incoming)) or bool(
        read.skip_reasons.get("phenom_listing_unstable")
        or read.skip_reasons.get("missing_required_posting_identity")
    )
    latest = incoming[-1] if incoming else selected[-1]
    return replace(
        latest,
        listing_ids=tuple(membership),
        listing_urls=tuple(membership.items()),
        listing_unstable=unstable,
    )


def retain_phenom_read(
    session: Session, scan: SourceScan, items: list[SourceScanFrontierItem], read: SourceRead
) -> None:
    """Store a Phenom page read's checkpoint on its rows and recount the scan's membership."""
    if not items or any(
        not str((item.detail_checkpoint or {}).get("platform", "")).startswith(
            "search:phenom_page:"
        )
        for item in items
    ):
        return
    continuation = session.get(SourceScanContinuation, items[0].continuation_id)
    source = session.get(Source, scan.source_id)
    if continuation is None or source is None:
        return
    root_url, _ = source_read_address(source.url, source.resolved_url)
    root = normalize_fetch_url(root_url)
    family = current_scan_family_ids(session, continuation)
    checkpoint = _phenom_read_checkpoint(items, read, root, family)
    if checkpoint is None:
        return
    for item in items:
        stored = session.get(SourceScanFrontierItem, item.id)
        if stored is not None:
            stored.detail_checkpoint = checkpoint.as_json()
    rows = session.scalars(
        select(SourceScanFrontierItem).where(
            SourceScanFrontierItem.continuation_id == continuation.id
        )
    )
    membership = {
        item
        for row in rows
        if (retained := _phenom_checkpoint(row, root, family)) is not None
        and retained.platform == checkpoint.platform
        for item in retained.listing_ids
    }
    scan.raw_count = scan.read_count = len(membership)
    scan.exhausted_outcome = (
        SourceReadOutcome.PUBLISHER_INDEX
        if phenom_listing_coverage(session, continuation, scan) is not None
        else None
    )
    if read.note and read.note not in scan.notes:
        scan.notes = " ".join(part for part in (scan.notes, read.note) if part)


def phenom_listing_coverage(
    session: Session, continuation: SourceScanContinuation, scan: SourceScan
) -> SourceDetailCoverage | None:
    """Return the listing coverage the scan family's Phenom page checkpoints agree on."""
    source = session.get(Source, scan.source_id)
    if source is None:
        return None
    root_url, _ = source_read_address(source.url, source.resolved_url)
    root = normalize_fetch_url(root_url)
    family = current_scan_family_ids(session, continuation)
    rows = session.scalars(
        select(SourceScanFrontierItem).where(
            SourceScanFrontierItem.continuation_id == continuation.id
        )
    )
    checkpoints = [
        checkpoint
        for row in rows
        if (checkpoint := _phenom_checkpoint(row, root, family)) is not None
    ]
    if not checkpoints or len({checkpoint.platform for checkpoint in checkpoints}) != 1:
        return None
    membership = dict(pair for checkpoint in checkpoints for pair in checkpoint.listing_urls)
    totals = {checkpoint.listing_total for checkpoint in checkpoints}
    if (
        any(checkpoint.listing_unstable for checkpoint in checkpoints)
        or totals != {len(membership)}
        or len(membership) == 0
    ):
        return None
    return SourceDetailCoverage(
        platform="phenom", board_url=root_url, posting_urls=tuple(membership.values())
    )
