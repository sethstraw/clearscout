"""Alignerr page membership, body counts, and listing coverage."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from clearscout.application.discovery.source_continuation.family import continuation_task_ids
from clearscout.application.discovery.source_continuation.limits import POSTING_DETAIL_COMPLETED
from clearscout.application.discovery.source_continuation.targets import frontier_target
from clearscout.application.discovery.source_lifecycle import source_read_address
from clearscout.application.discovery.source_reading import (
    SourceContinuationTarget,
    SourceDetailCheckpoint,
    SourceDetailCoverage,
    SourceRead,
)
from clearscout.domain import SourceReadOutcome
from clearscout.domain.identity import normalize_fetch_url
from clearscout.infrastructure.persistence.models import (
    Source,
    SourceScan,
    SourceScanContinuation,
    SourceScanFrontierItem,
)


def alignerr_checkpoint_rows(
    rows: Iterable[SourceScanFrontierItem], root: str
) -> list[tuple[SourceScanFrontierItem, SourceDetailCheckpoint]]:
    """Read only checkpoints belonging to this Alignerr enumeration root."""
    return [
        (row, checkpoint)
        for row in rows
        if row.detail_checkpoint is not None
        and normalize_fetch_url(row.source_url) == root
        and (checkpoint := SourceDetailCheckpoint.from_json(row.detail_checkpoint)).platform
        in {"alignerr", "search:alignerr_search_page"}
    ]


def _current_alignerr_membership(
    checkpoints: Iterable[SourceDetailCheckpoint],
    targets: Iterable[SourceContinuationTarget],
    listed: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    """Combine current page receipts with incoming list identities."""
    membership: dict[str, str] = {}
    for checkpoint in checkpoints:
        if checkpoint.platform == "search:alignerr_search_page":
            membership.update(checkpoint.listing_urls)
    for target in targets:
        incoming = target.detail_checkpoint
        if incoming is None:
            continue
        if (
            incoming.listing_urls
            and not incoming.listing_unstable
            and incoming.listing_total == len(incoming.listing_ids)
        ):
            membership = dict(incoming.listing_urls)
            break
        membership.update(incoming.listing_urls)
    membership.update(listed)
    return tuple(membership.items())


def validated_alignerr_membership(
    coverage: SourceDetailCoverage,
    checkpoints: Iterable[SourceDetailCheckpoint],
    targets: tuple[SourceContinuationTarget, ...],
    listed: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...] | None:
    """Accept exact incoming membership without borrowing an older body receipt."""
    urls = {normalize_fetch_url(url) for url in coverage.posting_urls}
    if len(listed) == len(urls) and {normalize_fetch_url(url) for _item, url in listed} == urls:
        return listed
    membership = _current_alignerr_membership(checkpoints, targets, listed)
    if (
        len(membership) == len(urls)
        and {normalize_fetch_url(url) for _item, url in membership} == urls
    ):
        return membership
    return None


def rebase_alignerr_checkpoints(
    session: Session,
    checkpoints: list[tuple[SourceScanFrontierItem, SourceDetailCheckpoint]],
    membership: tuple[tuple[str, str], ...],
) -> None:
    """Bind every retained body obligation to the current exact enumeration."""
    identities = tuple(item for item, _url in membership)
    for row, checkpoint in checkpoints:
        if checkpoint.platform == "search:alignerr_search_page":
            session.delete(row)
        else:
            row.detail_checkpoint = replace(
                checkpoint,
                listing_ids=identities,
                listing_urls=membership,
                listing_total=len(identities),
                listing_unstable=False,
            ).as_json()


def alignerr_body_count(session: Session, continuation_id: str, posting_urls: Iterable[str]) -> int:
    """Count completed bodies only within the represented enumeration."""
    membership = {normalize_fetch_url(url) for url in posting_urls}
    return (
        session.scalar(
            select(func.count())
            .select_from(SourceScanFrontierItem)
            .where(
                SourceScanFrontierItem.continuation_id == continuation_id,
                SourceScanFrontierItem.status == "completed",
                SourceScanFrontierItem.deferred_reason == POSTING_DETAIL_COMPLETED,
                SourceScanFrontierItem.normalized_url.in_(membership),
            )
        )
        or 0
    )


def selected_alignerr_pages(
    items: list[SourceScanFrontierItem], board_url: str
) -> tuple[SourceContinuationTarget, ...]:
    """Retain mapped evidence from the page actually selected for this read."""
    root = normalize_fetch_url(board_url)
    targets = []
    for item in items:
        target = frontier_target(item)
        checkpoint = target.detail_checkpoint
        if (
            checkpoint is not None
            and checkpoint.platform == "search:alignerr_search_page"
            and normalize_fetch_url(item.source_url) == root
            and normalize_fetch_url(checkpoint.board_url) == root
            and checkpoint.listing_urls
            and not checkpoint.listing_unstable
        ):
            targets.append(target)
    return tuple(targets)


def _current_alignerr_page_membership(
    session: Session, continuation_id: str, board_url: str
) -> tuple[tuple[str, str], ...] | None:
    """Recover observed membership only from this scan family's page evidence."""
    continuation = session.get(SourceScanContinuation, continuation_id)
    if continuation is None or continuation.correlation_id is None:
        return None
    root_task = continuation.correlation_id
    task_ids = [root_task, *continuation_task_ids(session, continuation.workspace_id, root_task)]
    rows = session.scalars(
        select(SourceScanFrontierItem).where(
            SourceScanFrontierItem.continuation_id == continuation_id,
            SourceScanFrontierItem.correlation_id.in_(task_ids),
        )
    )
    root = normalize_fetch_url(board_url)
    maps = [
        checkpoint.listing_urls
        for _row, checkpoint in alignerr_checkpoint_rows(rows, root)
        if checkpoint.platform == "search:alignerr_search_page"
        and normalize_fetch_url(checkpoint.board_url) == root
        and checkpoint.listing_urls
    ]
    if not maps:
        return None
    return tuple(dict(pair for membership in maps for pair in membership).items())


def alignerr_read_membership(
    session: Session, scan: SourceScan, items: list[SourceScanFrontierItem], read: SourceRead
) -> tuple[tuple[str, str], ...] | None:
    """Recover the current accumulated page scope or retained body scope."""
    checkpoints = [
        SourceDetailCheckpoint.from_json(item.detail_checkpoint)
        for item in items
        if item.detail_checkpoint is not None
    ]
    if not checkpoints or any(
        checkpoint.platform not in {"alignerr", "search:alignerr_search_page"}
        for checkpoint in checkpoints
    ):
        return None
    if any(checkpoint.platform == "search:alignerr_search_page" for checkpoint in checkpoints):
        return _current_alignerr_membership(
            checkpoints,
            read.deferred_targets,
            tuple((listing.requisition_id, listing.url) for listing in read.listings),
        )
    observed = _current_alignerr_page_membership(
        session, items[0].continuation_id, items[0].source_url
    )
    if observed is not None:
        return observed
    if scan.exhausted_outcome == SourceReadOutcome.ATS_COMPLETE and not scan.discovery_truncated:
        return checkpoints[0].listing_urls
    return ()


def alignerr_listing_coverage(
    session: Session,
    continuation: SourceScanContinuation,
    scan: SourceScan,
) -> SourceDetailCoverage | None:
    """Recover stable enumeration membership separately from completed body evidence."""
    if scan.exhausted_outcome != SourceReadOutcome.ATS_COMPLETE or scan.discovery_truncated:
        return None
    source = session.get(Source, scan.source_id)
    if source is None:
        return None
    root = normalize_fetch_url(source_read_address(source.url, source.resolved_url)[0])
    for row in session.scalars(
        select(SourceScanFrontierItem).where(
            SourceScanFrontierItem.continuation_id == continuation.id
        )
    ):
        if row.detail_checkpoint is None or normalize_fetch_url(row.source_url) != root:
            continue
        checkpoint = SourceDetailCheckpoint.from_json(row.detail_checkpoint)
        if (
            checkpoint.platform == "alignerr"
            and checkpoint.listing_urls
            and checkpoint.listing_total == len(checkpoint.listing_ids)
            and not checkpoint.listing_unstable
        ):
            return SourceDetailCoverage(
                platform="alignerr",
                board_url=row.source_url,
                posting_urls=tuple(dict(checkpoint.listing_urls).values()),
            )
    return None
