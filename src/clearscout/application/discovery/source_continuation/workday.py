"""Workday listing coverage rebased onto the chunk that read it."""

from __future__ import annotations

from dataclasses import replace

from sqlalchemy.orm import Session

from clearscout.application.discovery.source_continuation.chunk import Chunk
from clearscout.application.discovery.source_continuation.family import select_frontier_rows
from clearscout.application.discovery.source_continuation.limits import WORKDAY_TRUNCATION_REASONS
from clearscout.application.discovery.source_lifecycle import source_read_address
from clearscout.application.discovery.source_reading import (
    SourceDetailCheckpoint,
    SourceDetailCoverage,
)
from clearscout.domain.identity import normalize_fetch_url
from clearscout.infrastructure.persistence.models import (
    Source,
    SourceScan,
    SourceScanContinuation,
    SourceScanFrontierItem,
)
from clearscout.infrastructure.web_fetch.board_adapters import identify_board_url
from clearscout.infrastructure.web_fetch.boards import board_read_scope
from clearscout.infrastructure.web_fetch.platforms.workday import workday_site_root


def _workday_listing_scope(
    coverage: SourceDetailCoverage,
    items: list[SourceScanFrontierItem],
    protected_posting_urls: tuple[str, ...],
    posting_urls: frozenset[str],
) -> tuple[tuple[str, str], ...] | None:
    """Require exact Workday membership anchored by this chunk's declared board."""
    if coverage.platform != "workday":
        return None
    scope = board_read_scope(coverage.board_url, "workday")
    membership = {normalize_fetch_url(url) for url in coverage.posting_urls}
    observed = posting_urls | {normalize_fetch_url(url) for url in protected_posting_urls}
    if scope is None or not observed <= membership:
        return None
    for url in coverage.posting_urls:
        identity = identify_board_url(url)
        if identity is None or identity[0] != "workday":
            return None
        if board_read_scope(workday_site_root(identity[1]), "workday") != scope:
            return None
    for item in items:
        if item.detail_checkpoint is None:
            continue
        checkpoint = SourceDetailCheckpoint.from_json(item.detail_checkpoint)
        if (
            checkpoint.platform in {"workday", "search:workday"}
            and board_read_scope(checkpoint.board_url, "workday") == scope
        ):
            return scope
    return None


def rebase_workday_listing_coverage(
    session: Session,
    continuation: SourceScanContinuation,
    chunk: Chunk,
    coverage: SourceDetailCoverage,
    posting_urls: frozenset[str],
) -> None:
    """Replace obsolete paging holds and retain each still-listed owed body's metadata."""
    scope = _workday_listing_scope(
        coverage, chunk.items, chunk.protected_posting_urls, posting_urls
    )
    if scope is None:
        return
    if chunk.write_guard is not None:
        chunk.write_guard(session)
    scan = session.get(SourceScan, continuation.source_scan_id)
    source = session.get(Source, scan.source_id) if scan is not None else None
    if source is None or scan is None:
        return
    root_url, _ = source_read_address(source.url, source.resolved_url)
    source_scope = board_read_scope(root_url, "workday")
    if source_scope is not None and source_scope != scope:
        return
    for row in session.scalars(
        select_frontier_rows().where(
            SourceScanFrontierItem.continuation_id == continuation.id,
            SourceScanFrontierItem.detail_checkpoint.is_not(None),
        )
    ):
        if row.detail_checkpoint is None:
            continue
        checkpoint = SourceDetailCheckpoint.from_json(row.detail_checkpoint)
        if board_read_scope(checkpoint.board_url, "workday") != scope:
            continue
        if (
            checkpoint.platform == "search:workday"
            and row.deferred_reason == "workday_page_pending"
        ):
            session.delete(row)
        elif (
            checkpoint.platform == "workday"
            and row.status in {"pending", "failed"}
            and row.url in coverage.posting_urls
        ):
            row.detail_checkpoint = replace(
                checkpoint,
                listing_ids=coverage.posting_urls,
                listing_total=len(coverage.posting_urls),
                listing_unstable=False,
            ).as_json()
    scan.discovery_truncated = False
    scan.skip_reasons = {
        reason: count
        for reason, count in scan.skip_reasons.items()
        if reason not in WORKDAY_TRUNCATION_REASONS
    }
