"""One continuation batch's reads merged into a single read of the frontier."""

from __future__ import annotations

from dataclasses import replace

from tailorcv.application.discovery.source_reading import (
    SourceContinuationTarget,
    SourceDetailCheckpoint,
    SourceDetailCoverage,
    SourceDetailSummary,
    SourceRead,
)
from tailorcv.domain import SourceReadOutcome
from tailorcv.domain.identity import normalize_fetch_url
from tailorcv.infrastructure.web_fetch.board_platform import host_match
from tailorcv.infrastructure.web_fetch.board_reads import (
    NATIVE_LISTED_PLATFORMS,
    listed_posting_reason,
    publisher_search_scope,
)
from tailorcv.infrastructure.web_fetch.platforms import PLATFORMS
from tailorcv.infrastructure.web_fetch.platforms.workday import (
    WORKDAY,
    WORKDAY_BOARD,
    workday_site_root,
)


def finished_target_urls(read: SourceRead) -> set[str]:
    """Frontier URLs this read finished: requested, and not deferred back under the same URL."""
    deferred = {normalize_fetch_url(target.url) for target in read.deferred_targets}
    return {
        key
        for url in (*(read.attempted_target_urls or ()), *read.protected_posting_urls)
        if (key := normalize_fetch_url(url)) and key not in deferred
    }


def _continuation_summary(
    reads: list[SourceRead], unread: tuple[SourceContinuationTarget, ...]
) -> tuple[dict[str, int], SourceDetailSummary]:
    """Combine observed reasons and detail counts with the unread batch tail."""
    reasons: dict[str, int] = {}
    detail = SourceDetailSummary()
    for read in reads:
        for reason, count in read.skip_reasons.items():
            reasons[reason] = reasons.get(reason, 0) + count
        detail = detail.combined(read.detail)
    if unread:
        reasons["time_limit_reached"] = reasons.get("time_limit_reached", 0) + 1
        posting_reasons = {
            reason
            for platform in PLATFORMS
            if (reason := listed_posting_reason(platform.name)) in platform.continuations
        }
        detail = detail.combined(
            SourceDetailSummary(
                capped=sum(target.deferred_reason in posting_reasons for target in unread)
            )
        )
    return reasons, detail


def _workday_target_root(target: SourceContinuationTarget) -> str | None:
    """Read a continuation's verified board scope without corporate provenance."""
    checkpoint = target.detail_checkpoint
    if checkpoint is not None and checkpoint.platform in {"workday", "search:workday"}:
        return checkpoint.board_url.rstrip("/")
    match = host_match(WORKDAY_BOARD, target.source_url)
    return workday_site_root(match) if match is not None else None


def _workday_enumeration_scope_matches(
    coverage: SourceDetailCoverage, targets: tuple[SourceContinuationTarget, ...]
) -> bool:
    """Check every Workday enumeration identity and requested target belongs to its site."""
    root = host_match(WORKDAY_BOARD, coverage.board_url)
    if root is None or workday_site_root(root) != coverage.board_url:
        return False
    for url in coverage.posting_urls:
        match = host_match(WORKDAY, url)
        if match is None or workday_site_root(match) != coverage.board_url:
            return False
    return all(
        target.deferred_reason in {"workday_page_pending", "workday_detail_pending"}
        and _workday_target_root(target) == coverage.board_url
        for target in targets
    )


def _alignerr_enumeration_scope_matches(
    coverage: SourceDetailCoverage, targets: tuple[SourceContinuationTarget, ...]
) -> bool:
    """Match Alignerr enumeration evidence to the exact requested root and filter."""
    return all(
        normalize_fetch_url(target.source_url) == normalize_fetch_url(coverage.board_url)
        and target.deferred_reason == "alignerr_search_page"
        for target in targets
    )


def _merged_listing_coverage(
    targets: tuple[SourceContinuationTarget, ...], reads: list[SourceRead]
) -> SourceDetailCoverage | None:
    """Keep coherent enumeration evidence separate from readable body coverage."""
    coverages = [read.listing_coverage for read in reads if read.listing_coverage is not None]
    if not coverages or any(read.discovery_truncated for read in reads):
        return None
    coverage = coverages[0]
    if any(item != coverage for item in coverages):
        return None
    if coverage.platform == "alignerr":
        return coverage if _alignerr_enumeration_scope_matches(coverage, targets) else None
    if coverage.platform != "workday" or not _workday_enumeration_scope_matches(coverage, targets):
        return None
    membership = set(coverage.posting_urls)
    if any(
        url not in membership
        for read in reads
        for url in (*read.protected_posting_urls, *(listing.url for listing in read.listings))
    ):
        return None
    return coverage


def _continuation_raw_count(
    targets: tuple[SourceContinuationTarget, ...],
    reads: list[SourceRead],
    listing_count: int,
) -> int:
    """Retain accumulated enumeration counts for the adapters that return them."""
    if targets and all(target.deferred_reason == "alignerr_search_page" for target in targets):
        return max((read.raw_count for read in reads), default=0)
    if targets and all(target.deferred_reason == "phenom_page_pending" for target in targets):
        return sum(read.raw_count for read in reads)
    if len(reads) == 1 and reads[0].detail_read_urls is not None:
        return max(reads[0].raw_count, listing_count)
    return max(len(targets), listing_count)


def _continuation_detail_coverage(
    reads: list[SourceRead], *, stable_details: bool, has_remaining: bool
) -> SourceDetailCoverage | None:
    """Keep full-detail coverage only when the batch finishes one coherent scope."""
    scopes = {
        (read.detail_coverage.platform, read.detail_coverage.board_url)
        for read in reads
        if read.detail_coverage is not None
    }
    if (
        stable_details
        and reads
        and len(scopes) == 1
        and not has_remaining
        and all(read.detail_coverage == reads[0].detail_coverage for read in reads)
    ):
        return reads[0].detail_coverage
    return None


def _continuation_note(
    reads: list[SourceRead],
    reasons: dict[str, int],
    *,
    unreadable: bool,
    remaining_count: int,
    target_count: int,
) -> str:
    """Preserve explicit publisher diagnostics instead of generic frontier counts."""
    if (
        (unreadable and not remaining_count)
        or "publisher_listing_total_mismatch" in reasons
        or "publisher_listing_membership_missing" in reasons
        or "publisher_listing_changed" in reasons
        or "invalid_publisher_search_scope" in reasons
        or any(read.protected_posting_urls for read in reads)
    ):
        return " ".join(read.note for read in reads if read.note)
    if remaining_count:
        return f"{remaining_count} source URLs or board pages remain in the durable frontier."
    return f"Read {target_count} durable source-continuation targets."


def _publisher_search_body_receipt(
    target: SourceContinuationTarget, read: SourceRead, root_url: str
) -> tuple[tuple[str, str], tuple[str, ...]] | None:
    proof = read.detail_read_urls
    if proof is None:
        return None
    scope = publisher_search_scope(target, posting_urls=proof)
    if scope is None or scope[0] == "alignerr" or scope[1] != normalize_fetch_url(root_url):
        return None
    checkpoints = tuple(
        item.detail_checkpoint
        for item in (target, *read.deferred_targets)
        if item.detail_checkpoint is not None
        and publisher_search_scope(item, posting_urls=proof) == scope
    )
    known = {
        normalize_fetch_url(listing.url)
        for listing in read.listings
        if listing.published_description
    }
    known.update(
        normalize_fetch_url(url)
        for checkpoint in checkpoints
        for _item, url in checkpoint.completed
    )
    protected = {
        normalize_fetch_url(url)
        for checkpoint in checkpoints
        for _item, url in checkpoint.protected
    } | {normalize_fetch_url(url) for url in read.protected_posting_urls}
    normalized = {normalize_fetch_url(url) for url in proof}
    if not normalized <= known or normalized & protected:
        return None
    return scope, proof


def _publisher_search_body_urls(
    reads: list[tuple[SourceContinuationTarget, SourceRead]],
    root_url: str,
) -> tuple[str, ...] | None:
    receipts = [
        receipt
        for target, read in reads
        if (receipt := _publisher_search_body_receipt(target, read, root_url)) is not None
    ]
    if not receipts or len({scope for scope, _proof in receipts}) != 1:
        return None
    return tuple(dict.fromkeys(url for _scope, proof in receipts for url in proof))


def _protected_native_batch(
    targets: tuple[SourceContinuationTarget, ...],
    reads: list[SourceRead],
    root_url: str,
) -> bool:
    """Identify an owned native batch consisting entirely of protected postings."""
    return bool(targets) and all(
        target.url in {url for read in reads for url in read.protected_posting_urls}
        and (scope := publisher_search_scope(target)) is not None
        and scope[0] in NATIVE_LISTED_PLATFORMS
        and scope[1] == normalize_fetch_url(root_url)
        for target in targets
    )


def _continuation_listing_receipts(
    publisher_reads: list[tuple[SourceContinuationTarget, SourceRead]],
    root_url: str,
) -> tuple[SourceDetailCheckpoint, ...]:
    """Keep publisher page receipts validated against the exact batch root."""
    return tuple(
        receipt
        for target, read in publisher_reads
        if target.deferred_reason.endswith(
            ("_search_page", "_sitemap_pending", "_detail_rows_pending")
        )
        and (scope := publisher_search_scope(target)) is not None
        and scope[1] == normalize_fetch_url(root_url)
        for receipt in read.publisher_listing_receipts
        if publisher_search_scope(replace(target, detail_checkpoint=receipt)) == scope
    )


def merged_continuation(
    targets: tuple[SourceContinuationTarget, ...],
    reads: list[SourceRead],
    unread: tuple[SourceContinuationTarget, ...],
    *,
    publisher_reads: list[tuple[SourceContinuationTarget, SourceRead]],
    root_url: str,
) -> SourceRead:
    """Merge one batch's reads, keeping deferred only what no read in the batch finished."""
    reasons, detail = _continuation_summary(reads, unread)
    finished_urls = set().union(*(finished_target_urls(read) for read in reads))
    # One page names the next as pending, so consecutive pages in one batch would re-defer
    # each other. A pointer asking for a different kind of read of that URL still stands.
    finished = {
        (key, target.deferred_reason)
        for target in targets
        if (key := normalize_fetch_url(target.url)) in finished_urls
    }
    remaining = tuple(
        target
        for target in (*(item for read in reads for item in read.deferred_targets), *unread)
        if target.detail_checkpoint is not None
        or (normalize_fetch_url(target.url), target.deferred_reason) not in finished
    )
    listings = [listing for read in reads for listing in read.listings]
    phenom_pages = bool(targets) and all(
        target.deferred_reason == "phenom_page_pending" for target in targets
    )
    body_urls = _publisher_search_body_urls(publisher_reads, root_url)
    if body_urls is None and _protected_native_batch(targets, reads, root_url):
        body_urls = ()
    listing_receipts = _continuation_listing_receipts(publisher_reads, root_url)
    raw_count = max(_continuation_raw_count(targets, reads, len(listings)), len(body_urls or ()))
    raw_count = max(
        raw_count,
        len(
            {
                (receipt.platform, normalize_fetch_url(receipt.board_url), identity)
                for receipt in listing_receipts
                for identity in receipt.listing_ids
            }
        ),
    )
    stable_details = bool(targets) and all(
        target.deferred_reason.endswith("_detail_rows_pending")
        or target.deferred_reason == "workday_detail_pending"
        for target in targets
    )
    coverage = _continuation_detail_coverage(
        reads, stable_details=stable_details, has_remaining=bool(remaining)
    )
    unreadable = (phenom_pages or stable_details) and any(
        read.outcome is SourceReadOutcome.NO_SUPPORTED_REPRESENTATION for read in reads
    )
    return SourceRead(
        listings=listings,
        complete=coverage is not None,
        raw_count=raw_count,
        outcome=(
            SourceReadOutcome.CRAWL_CONTINUATION
            if remaining
            else SourceReadOutcome.ATS_COMPLETE
            if coverage is not None
            else SourceReadOutcome.NO_SUPPORTED_REPRESENTATION
            if unreadable
            else SourceReadOutcome.PUBLISHER_LINKED_PAGES
        ),
        skip_reasons=reasons,
        note=_continuation_note(
            reads,
            reasons,
            unreadable=unreadable,
            remaining_count=len(remaining),
            target_count=len(targets),
        ),
        deferred_targets=remaining,
        attempted_target_urls=tuple(
            url for read in reads for url in read.attempted_target_urls or ()
        ),
        detail=detail,
        detail_coverage=coverage,
        detail_read_urls=reads[0].detail_read_urls
        if stable_details and len(reads) == 1
        else body_urls,
        listing_coverage=_merged_listing_coverage(targets, reads),
        publisher_listing_receipts=listing_receipts,
        exhausted_outcome=next(
            (read.exhausted_outcome for read in reads if read.exhausted_outcome is not None), None
        )
        if remaining
        else None,
        protected_posting_urls=tuple(url for read in reads for url in read.protected_posting_urls),
        closed_posting_urls=tuple(url for read in reads for url in read.closed_posting_urls),
        discovery_truncated=any(read.discovery_truncated for read in reads),
        listing_pages=sum(read.listing_pages for read in reads),
    )
