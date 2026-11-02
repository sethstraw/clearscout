"""Frontier rows built from continuation targets, and targets rebuilt from rows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tailorcv.application.discovery.source_continuation.limits import MAX_AUTOMATIC_ATTEMPTS
from tailorcv.application.discovery.source_reading import (
    ContinuationLabelSource,
    ContinuationRelation,
    ContinuationStructuralContext,
    SourceContinuationTarget,
    SourceDetailCheckpoint,
)
from tailorcv.application.failures import NotFoundError, ValidationError
from tailorcv.domain import new_id, utc_now
from tailorcv.domain.identity import normalize_fetch_url
from tailorcv.infrastructure.persistence.models import SourceScan, SourceScanFrontierItem


def frontier_item(
    continuation_id: str,
    normalized_url: str,
    target: SourceContinuationTarget,
    *,
    origin: str,
    correlation_id: str | None,
) -> SourceScanFrontierItem:
    """Build one durable frontier row from retained publisher-link evidence."""
    return SourceScanFrontierItem(
        id=new_id(),
        continuation_id=continuation_id,
        url=target.url,
        normalized_url=normalized_url,
        label=target.label,
        label_source=target.label_source,
        source_url=target.source_url,
        structural_context=target.structural_context,
        relations=list(target.relations),
        depth=target.depth,
        automatic_eligible=target.automatic_eligible,
        deferred_reason=target.deferred_reason,
        detail_checkpoint=(
            target.detail_checkpoint.as_json() if target.detail_checkpoint is not None else None
        ),
        status="pending",
        attempt_count=0,
        last_error="",
        created_by="worker",
        origin=origin,
        correlation_id=correlation_id,
    )


def read_page_item(
    continuation_id: str, normalized_url: str, url: str, *, correlation_id: str | None
) -> SourceScanFrontierItem:
    """Record a page the scan's own read already fetched, completed and needing no retry.

    The blank deferred reason keeps it out of the posting-detail rescreen and out
    of the next scan's carry-forward, so it only ever answers what the traversal read.
    """
    return SourceScanFrontierItem(
        id=new_id(),
        continuation_id=continuation_id,
        url=url,
        normalized_url=normalized_url,
        label="Read during the source scan",
        label_source="none",
        source_url=url,
        structural_context="main",
        relations=["publisher-content"],
        depth=1,
        automatic_eligible=True,
        deferred_reason="",
        status="completed",
        attempt_count=1,
        last_error="",
        completed_at=utc_now(),
        created_by="worker",
        origin="source.scan",
        correlation_id=correlation_id,
    )


LISTED_POSTING_SUFFIX = "_posting_pending"


def listed_posting(target: SourceContinuationTarget | SourceScanFrontierItem) -> bool:
    """Whether a listed-page read deferred this target as one posting body."""
    return target.deferred_reason.endswith(LISTED_POSTING_SUFFIX)


@dataclass(frozen=True)
class ListedPostingCredit:
    """Listed postings a read holds bodies for, those still owed, and whether none is owed."""

    absorbed: int
    owed: int
    drained: bool
    # A native listing receipt, or a reader that declares its detail reads, counts
    # bodies against its own receipts instead.
    reader_counts_bodies: bool = False


def continuation_frontier_counts(session: Session, continuation_id: str) -> tuple[int, int]:
    """Return one continuation's pending count and its pending-plus-failed count."""
    rows = session.execute(
        select(SourceScanFrontierItem.status, func.count())
        .where(SourceScanFrontierItem.continuation_id == continuation_id)
        .group_by(SourceScanFrontierItem.status)
    )
    counts = {str(status): int(count) for status, count in rows}
    pending = counts.get("pending", 0)
    return pending, pending + counts.get("failed", 0)


def owner_source_id(session: Session, source_scan_id: str) -> str:
    """Return the source id owning this scan, raising if the scan is gone."""
    scan = session.get(SourceScan, source_scan_id)
    if scan is None:
        raise NotFoundError("Source continuation owner scan does not exist.")
    return scan.source_id


def targets_by_url(
    targets: tuple[SourceContinuationTarget, ...],
) -> dict[str, SourceContinuationTarget]:
    """Key targets by normalized URL, keeping the first of each."""
    normalized_targets: dict[str, SourceContinuationTarget] = {}
    for target in targets:
        normalized = normalize_fetch_url(target.url)
        if not normalized:
            raise ValidationError("Source continuation target cannot be normalized.")
        normalized_targets.setdefault(normalized, target)
    return normalized_targets


def frontier_target(
    item: SourceScanFrontierItem, *, manual_detail_retry: bool = False
) -> SourceContinuationTarget:
    """Rebuild the application target from one durable frontier row."""
    return SourceContinuationTarget(
        url=item.url,
        label=item.label,
        label_source=cast(ContinuationLabelSource, item.label_source),
        source_url=item.source_url,
        structural_context=cast(ContinuationStructuralContext, item.structural_context),
        relations=cast(tuple[ContinuationRelation, ...], tuple(item.relations)),
        depth=item.depth,
        automatic_eligible=item.automatic_eligible,
        deferred_reason=item.deferred_reason,
        detail_checkpoint=(
            SourceDetailCheckpoint.from_json(item.detail_checkpoint)
            if item.detail_checkpoint is not None
            else None
        ),
        manual_detail_retry=manual_detail_retry,
    )


def retry_status(row: SourceScanFrontierItem) -> str:
    """Keep an unread row pending until an automatic row spends its attempts."""
    if not row.automatic_eligible or row.attempt_count < MAX_AUTOMATIC_ATTEMPTS:
        return "pending"
    return "failed"


def completed_frontier_urls(session: Session, continuation_id: str) -> list[str]:
    """Return every URL this continuation's frontier records as read.

    A page fetched here that declared no posting is present, not absent, so a
    withdrawal counts these alongside the candidates a read actually listed.
    """
    return list(
        session.scalars(
            select(SourceScanFrontierItem.url).where(
                SourceScanFrontierItem.continuation_id == continuation_id,
                SourceScanFrontierItem.status == "completed",
            )
        )
    )
