"""What a chunk's read leaves on its scan, and settling the scan once the frontier drains."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from tailorcv.application.discovery.scan_outcome import settle_drained_scan
from tailorcv.application.discovery.scan_staging import (
    AbsenceCheck,
    WithdrawingRead,
    withdraw_absent_in_session,
)
from tailorcv.application.discovery.source_continuation.alignerr import (
    alignerr_body_count,
    alignerr_listing_coverage,
    alignerr_read_membership,
)
from tailorcv.application.discovery.source_continuation.chunk import Chunk
from tailorcv.application.discovery.source_continuation.detail_checkpoints import (
    cumulative_detail_read_count,
)
from tailorcv.application.discovery.source_continuation.family import current_scan_family_ids
from tailorcv.application.discovery.source_continuation.limits import CARRIED_TRUNCATION_REASONS
from tailorcv.application.discovery.source_continuation.phenom import (
    phenom_listing_coverage,
    retain_phenom_read,
)
from tailorcv.application.discovery.source_continuation.publisher import (
    publisher_listing_coverage,
    retain_publisher_search_read,
)
from tailorcv.application.discovery.source_continuation.targets import (
    completed_frontier_urls,
    continuation_frontier_counts,
    person_held_frontier_urls,
)
from tailorcv.application.discovery.source_lifecycle import source_read_address
from tailorcv.application.discovery.source_reading import SourceDetailCoverage, SourceRead
from tailorcv.application.failures import ConflictError
from tailorcv.domain.identity import normalize_fetch_url
from tailorcv.infrastructure.persistence.models import (
    Source,
    SourceScan,
    SourceScanContinuation,
    SourceScanFrontierItem,
    SourceScanOutcome,
)
from tailorcv.infrastructure.persistence.models.discovery import SourceScanMember
from tailorcv.infrastructure.web_fetch.board_adapters import identify_board_url
from tailorcv.infrastructure.web_fetch.board_reads import NATIVE_LISTED_PLATFORMS
from tailorcv.infrastructure.web_fetch.boards import board_read_scope
from tailorcv.infrastructure.web_fetch.platforms.workday import workday_site_root

MANUAL_RECOVERY_NOTE = (
    "Automatic continuation reads are finished; the remaining links need a manual "
    "continuation or a later scan."
)


def retain_declared_read(
    session: Session,
    scan_id: str,
    items: list[SourceScanFrontierItem],
    read: SourceRead,
) -> None:
    """Retain declared diagnostics and body counts within current membership."""
    scan = session.get(SourceScan, scan_id)
    if scan is None:
        return
    retain_phenom_read(session, scan, items, read)
    publisher_retained = retain_publisher_search_read(session, scan, items, read)
    native_retained = publisher_retained and all(
        str((item.detail_checkpoint or {}).get("platform", "")).removeprefix("listed:")
        in NATIVE_LISTED_PLATFORMS
        for item in items
    )
    detail_count = (
        None if native_retained else cumulative_detail_read_count(session, scan, items, read)
    )
    if detail_count is not None:
        scan.raw_count = max(scan.raw_count or 0, read.raw_count)
        scan.read_count = detail_count
    membership = alignerr_read_membership(session, scan, items, read)
    if membership is not None:
        scan.raw_count = max(scan.raw_count or 0, read.raw_count)
        scan.read_count = alignerr_body_count(
            session, items[0].continuation_id, (url for _item, url in membership)
        )
    diagnostics = {
        reason: count
        for reason, count in read.skip_reasons.items()
        if reason
        in {
            "publisher_listing_total_mismatch",
            "publisher_listing_membership_missing",
            "publisher_listing_changed",
            "invalid_publisher_search_scope",
        }
    }
    if diagnostics:
        scan.skip_reasons = {**scan.skip_reasons, **diagnostics}
        if read.note and read.note not in scan.notes:
            scan.notes = " ".join(part for part in (scan.notes, read.note) if part)


def retain_discovery_truncation(
    session: Session, chunk: Chunk, source_scan_id: str, read_reasons: dict[str, int]
) -> None:
    """Mark the scan's discovery truncated and carry the read's truncation reasons onto it."""
    if chunk.write_guard is not None:
        chunk.write_guard(session)
    scan = session.get(SourceScan, source_scan_id)
    if scan is not None:
        scan.discovery_truncated = True
        carried = {
            reason: count
            for reason, count in read_reasons.items()
            if reason in CARRIED_TRUNCATION_REASONS
        }
        if carried:
            scan.skip_reasons = {**scan.skip_reasons, **carried}


def credit_listing_pages(
    session: Session, chunk: Chunk, source_scan_id: str, listing_pages: int
) -> None:
    """Add a chunk's listing pages to the scan it extends."""
    if chunk.write_guard is not None:
        chunk.write_guard(session)
    scan = session.get(SourceScan, source_scan_id)
    if scan is not None:
        scan.listing_pages = (scan.listing_pages or 0) + listing_pages


def _traversal_read_urls(session: Session, scan_id: str, continuation_id: str) -> list[str]:
    """Return every URL this scan's traversal listed or fetched, across all of its reads."""
    listed = session.scalars(
        select(SourceScanOutcome.url).where(SourceScanOutcome.source_scan_id == scan_id)
    )
    return [*listed, *completed_frontier_urls(session, continuation_id)]


def _paged_listing_scope(url: str) -> tuple[str, tuple[tuple[str, str], ...] | None] | None:
    """Return the platform and site scope of a Workday or Oracle root or posting URL."""
    identity = identify_board_url(url)
    if identity is None:
        return None
    if identity[0] == "workday":
        return "workday", board_read_scope(workday_site_root(identity[1]), "workday")
    if identity[0] == "oracle":
        return "oracle", board_read_scope(url, "oracle")
    return None


def retain_paged_listing_counts(
    session: Session,
    continuation: SourceScanContinuation,
    scan: SourceScan,
    chunk: Chunk,
) -> None:
    """Project observed paged-board listing counts from the current scan's ledger."""
    if chunk.abandoned or chunk.write_guard is None:
        return
    try:
        chunk.write_guard(session)
    except ConflictError:
        return
    member = session.get(SourceScanMember, continuation.batch_member_id)
    source = session.get(Source, scan.source_id)
    if (
        member is None
        or source is None
        or source.workspace_id != continuation.workspace_id
        or member.workspace_id != continuation.workspace_id
        or member.source_id != scan.source_id
        or member.source_scan_id != scan.id
        or member.continuation_id != continuation.id
        or member.generation != continuation.generation
        or member.root_task_id != scan.correlation_id
        or member.root_task_id != continuation.correlation_id
        or member.cancellation_requested_at is not None
    ):
        return
    root_url, _ = source_read_address(source.url, source.resolved_url)
    scope = _paged_listing_scope(root_url)
    if scope is None or scope[1] is None:
        return
    raw, opened = member.result.get("raw"), member.result.get("open")
    if type(raw) is not int or type(opened) is not int or raw < opened or opened < 0:
        return
    family = current_scan_family_ids(session, continuation)
    rows = session.execute(
        select(
            SourceScanOutcome.task_id,
            SourceScanOutcome.url,
            SourceScanOutcome.normalized_url,
        )
        .where(
            SourceScanOutcome.workspace_id == continuation.workspace_id,
            SourceScanOutcome.source_scan_id == scan.id,
            SourceScanOutcome.task_id.in_(family),
            SourceScanOutcome.candidate_kind == "board_listing",
        )
        .distinct()
    )
    observed: set[str] = set()
    initial: set[str] = set()
    for task_id, url, normalized in rows:
        if _paged_listing_scope(url) != scope or normalize_fetch_url(url) != normalized:
            continue
        observed.add(normalized)
        if task_id == member.root_task_id:
            initial.add(normalized)
    if len(initial) < opened or len(observed) < (scan.read_count or 0):
        return
    scan.read_count = len(observed)
    # The initial receipt retains unmapped rows; later receipts retain only URLs.
    scan.raw_count = max(scan.raw_count or 0, len(observed) + raw - opened)


def settle_drained_continuation(
    session: Session,
    continuation: SourceScanContinuation,
    correlation_id: str,
    *,
    check_absent: AbsenceCheck | None = None,
) -> None:
    """Settle the owning scan once nothing remains to read automatically, or say why not."""
    scan = session.get(SourceScan, continuation.source_scan_id)
    if scan is None:
        return
    _pending, unresolved = continuation_frontier_counts(session, continuation.id)
    listing_coverage = (
        (
            alignerr_listing_coverage(session, continuation, scan)
            or phenom_listing_coverage(session, continuation, scan)
            or publisher_listing_coverage(session, continuation, scan)
        )
        if not unresolved
        else None
    )
    if not settle_drained_scan(
        scan, unresolved_count=unresolved, listing_complete=listing_coverage is not None
    ):
        return
    if unresolved:
        if MANUAL_RECOVERY_NOTE not in scan.notes:
            scan.notes = " ".join(part for part in (scan.notes, MANUAL_RECOVERY_NOTE) if part)
        return
    # The candidate audit that names what the first read listed is written only
    # under a task, so a scan with no task cannot answer what the traversal saw.
    if not scan.complete or scan.correlation_id is None:
        return
    withdraw_absent_in_session(
        session,
        continuation.workspace_id,
        scan.source_id,
        [
            *(
                listing_coverage.posting_urls
                if listing_coverage is not None
                else SourceDetailCoverage.from_json(scan.detail_coverage).posting_urls
                if scan.detail_coverage is not None
                else _traversal_read_urls(session, scan.id, continuation.id)
            ),
            *person_held_frontier_urls(session, continuation.id),
        ],
        scan.scanned_on,
        correlation_id=correlation_id,
        read=WithdrawingRead(
            scan.raw_count or 0,
            scan.read_count or 0,
            bool(scan.complete),
            scan.listing_pages or 0,
            started_at=scan.scan_started_at,
        ),
        check_absent=check_absent,
    )
