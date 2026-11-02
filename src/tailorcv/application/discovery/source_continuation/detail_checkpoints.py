"""Detail checkpoints a read leaves on its rows, and the detail counts they add up to."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from tailorcv.application.discovery.source_continuation.limits import POSTING_DETAIL_COMPLETED
from tailorcv.application.discovery.source_lifecycle import source_read_address
from tailorcv.application.discovery.source_reading import (
    SourceContinuationTarget,
    SourceDetailCheckpoint,
    SourceDetailCoverage,
    SourceRead,
)
from tailorcv.domain.identity import normalize_fetch_url
from tailorcv.infrastructure.persistence.models import Source, SourceScan, SourceScanFrontierItem
from tailorcv.infrastructure.web_fetch.boards import board_read_scope


def _detail_checkpoint_superseded(
    row: SourceScanFrontierItem,
    checkpoint: SourceDetailCheckpoint,
    scopes: set[tuple[str, tuple[tuple[str, str], ...] | None]],
    certified_workday: set[tuple[tuple[str, str], ...] | None],
    workday_targets: dict[str, tuple[tuple[str, str], ...] | None],
) -> bool:
    """Match fresh enumeration or an exact observed detail to its retained checkpoint."""
    # A completed posting body is coverage, not a board read a fresh one replaces.
    if row.status == "completed" and row.deferred_reason == POSTING_DETAIL_COMPLETED:
        return False
    platform = checkpoint.platform
    scope = board_read_scope(
        checkpoint.board_url, "workday" if platform == "search:workday" else platform
    )
    if scope is None:
        return False
    if platform == "search:workday":
        return row.deferred_reason == "workday_page_pending" and scope in certified_workday
    if platform == "workday":
        return scope in certified_workday or workday_targets.get(row.normalized_url) == scope
    return (platform, scope) in scopes


def replace_detail_checkpoints(
    session: Session, continuation_id: str, targets: tuple[SourceContinuationTarget, ...]
) -> None:
    """A fresh board read replaces its prior checkpoint instead of creating two reads."""
    checkpoints = [
        target.detail_checkpoint
        for target in targets
        if target.detail_checkpoint and not target.deferred_reason.endswith("_search_detail")
    ]
    scopes = {
        (checkpoint.platform, board_read_scope(checkpoint.board_url, checkpoint.platform))
        for checkpoint in checkpoints
        if checkpoint.platform != "workday"
    }
    certified_workday = {
        board_read_scope(checkpoint.board_url, "workday")
        for checkpoint in checkpoints
        if checkpoint.platform == "workday"
        and checkpoint.listing_total is not None
        and checkpoint.listing_total == len(checkpoint.listing_ids)
        and not checkpoint.listing_unstable
    }
    workday_targets = {
        normalize_fetch_url(target.url): board_read_scope(
            target.detail_checkpoint.board_url, "workday"
        )
        for target in targets
        if target.detail_checkpoint is not None and target.detail_checkpoint.platform == "workday"
    }
    if not scopes and not workday_targets:
        return
    rows = session.scalars(
        select(SourceScanFrontierItem).where(
            SourceScanFrontierItem.continuation_id == continuation_id,
            SourceScanFrontierItem.detail_checkpoint.is_not(None),
        )
    )
    for row in rows:
        # JSON null passes SQL IS NOT NULL and loads as None.
        if row.detail_checkpoint is None:
            continue
        checkpoint = SourceDetailCheckpoint.from_json(row.detail_checkpoint)
        if _detail_checkpoint_superseded(
            row, checkpoint, scopes, certified_workday, workday_targets
        ):
            session.delete(row)


def detail_count_checkpoints(
    items: list[SourceScanFrontierItem], read: SourceRead
) -> tuple[SourceDetailCheckpoint, ...]:
    """Return the checkpoints the chunk's rows held and the read's new targets carry."""
    selected = tuple(
        SourceDetailCheckpoint.from_json(item.detail_checkpoint)
        for item in items
        if item.detail_checkpoint is not None
    )
    incoming = tuple(
        target.detail_checkpoint
        for target in read.deferred_targets
        if target.detail_checkpoint is not None
    )
    return (*selected, *incoming)


def _detail_count_scope(
    session: Session, scan: SourceScan, items: list[SourceScanFrontierItem], read: SourceRead
) -> tuple[str, str] | None:
    if not items or any(
        not item.deferred_reason.endswith("_detail_rows_pending") for item in items
    ):
        return None
    checkpoints = detail_count_checkpoints(items, read)
    scopes = {
        (checkpoint.platform, normalize_fetch_url(checkpoint.board_url))
        for checkpoint in checkpoints
    }
    coverage = read.detail_coverage
    if coverage is not None:
        scopes.add((coverage.platform, normalize_fetch_url(coverage.board_url)))
    if len(scopes) != 1:
        return None
    platform, board_url = next(iter(scopes))
    if any(item.deferred_reason != f"{platform}_detail_rows_pending" for item in items):
        return None
    source = session.get(Source, scan.source_id)
    if source is None:
        return None
    root_url, _ = source_read_address(source.url, source.resolved_url)
    scope = board_read_scope(root_url, platform)
    if scope is None or scope != board_read_scope(board_url, platform):
        return None
    if any(board_read_scope(item.source_url, platform) != scope for item in items):
        return None
    return platform, board_url


def cumulative_detail_read_count(
    session: Session, scan: SourceScan, items: list[SourceScanFrontierItem], read: SourceRead
) -> int | None:
    """Count the detail bodies a read proves, or None when this scan cannot own the proof."""
    if read.detail_read_urls is None or _detail_count_scope(session, scan, items, read) is None:
        return None
    proof = {normalize_fetch_url(url) for url in read.detail_read_urls}
    known = {normalize_fetch_url(listing.url) for listing in read.listings}
    known.update(
        normalize_fetch_url(url)
        for checkpoint in detail_count_checkpoints(items, read)
        for _item, url in checkpoint.completed
    )
    if not proof <= known or proof & {
        normalize_fetch_url(url) for url in read.protected_posting_urls
    }:
        return None
    if read.detail_coverage is not None:
        if not proof <= {normalize_fetch_url(url) for url in read.detail_coverage.posting_urls}:
            return None
    elif proof and not read.deferred_targets and not read.listings:
        return None
    return len(proof)


def retain_detail_coverage(
    session: Session, scan_id: str, source_id: str, coverage: SourceDetailCoverage
) -> None:
    """Retain complete membership only for the source's actual recognized board scope."""
    source = session.get(Source, source_id)
    scan = session.get(SourceScan, scan_id)
    if source is None or scan is None:
        return
    root_url, _ = source_read_address(source.url, source.resolved_url)
    scope = board_read_scope(root_url, coverage.platform)
    if scope is not None and scope == board_read_scope(coverage.board_url, coverage.platform):
        scan.detail_coverage = coverage.as_json()
