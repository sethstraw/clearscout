"""Workday detail reads: one fetch per listing, its failure bucket, and the certificate."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

import httpx

from tailorcv.application.discovery.screening import (
    screen_discovery_title,
    summary_may_retain_title,
    supports_title_expansion,
)
from tailorcv.application.discovery.source_reading import (
    DETAIL_FAILURE_EXAMPLE_LIMIT,
    DetailFailureReason,
    PostingCheck,
    SourceContinuationTarget,
    SourceDetailCheckpoint,
    SourceDetailCoverage,
    SourceDetailSummary,
    SourceListing,
    SourceRead,
)
from tailorcv.domain import SourceReadOutcome
from tailorcv.domain.search_parameters import JobSearchParameters
from tailorcv.infrastructure.web_fetch.board_adapters import identify_board_url
from tailorcv.infrastructure.web_fetch.board_mappings.mapper import listing_summary
from tailorcv.infrastructure.web_fetch.boards import board_read_scope
from tailorcv.infrastructure.web_fetch.ingest.url import posting_read_allowed, shared_posting_feeds
from tailorcv.infrastructure.web_fetch.listing_facts import workday_detail_facts
from tailorcv.infrastructure.web_fetch.platforms.workday import workday_site_root


@dataclass
class WorkdayEnrichmentState:
    """Accumulate counters and enriched listings across one Workday detail sweep."""

    enriched: list[SourceListing] = field(default_factory=list)
    attempted: int = 0
    read: int = 0
    failed: int = 0
    failure_counts: dict[DetailFailureReason, int] = field(
        default_factory=lambda: {"timeout": 0, "http_status": 0, "parse": 0, "other": 0}
    )
    failure_examples: list[str] = field(default_factory=list)
    pending: list[tuple[SourceListing, bool]] = field(default_factory=list)
    completed: list[tuple[str, str]] = field(default_factory=list)

    def record_failure(self, url: str, classified: DetailFailureReason) -> None:
        """Track one detail-fetch failure and its example URL."""
        self.failed += 1
        self.failure_counts[classified] += 1
        if len(self.failure_examples) < DETAIL_FAILURE_EXAMPLE_LIMIT:
            self.failure_examples.append(url)

    def summary(self) -> SourceDetailSummary:
        """Return the sweep summary shape callers expect."""
        return SourceDetailSummary(
            attempted=self.attempted,
            read=self.read,
            failed=self.failed,
            capped=sum(not failed for _, failed in self.pending),
            failure_reasons=tuple(
                (reason, count) for reason, count in self.failure_counts.items() if count > 0
            ),
            failure_examples=tuple(self.failure_examples),
        )


def workday_enumeration_coverage(read: SourceRead) -> SourceDetailCoverage | None:
    """Accept a stable certificate only when its exact membership owns this read's listings."""
    coverage = read.listing_coverage
    if coverage is None or coverage.platform != "workday" or read.discovery_truncated:
        return None
    scope = board_read_scope(coverage.board_url, "workday")
    observed = {*(item.url for item in read.listings), *read.protected_posting_urls}
    if scope is None or not observed <= set(coverage.posting_urls):
        return None
    for url in coverage.posting_urls:
        identity = identify_board_url(url)
        if identity is None or identity[0] != "workday":
            return None
        if board_read_scope(workday_site_root(identity[1]), "workday") != scope:
            return None
    return coverage


def detail_read_one(
    checker: Callable[[str], PostingCheck],
    listing: SourceListing,
    state: WorkdayEnrichmentState,
) -> None:
    """Attempt one detail fetch and record its outcome on the shared state."""
    state.attempted += 1
    try:
        checked = checker(listing.url)
    except Exception as exc:  # noqa: BLE001 - observability of the detail-fetch failure
        state.record_failure(listing.url, _classify_exception(exc))
        state.pending.append((listing, True))
        state.enriched.append(incomplete_workday_listing(listing))
        return
    if (
        checked.status != "readable"
        or checked.platform != "workday"
        or not checked.text.strip()
        or checked.source is None
    ):
        state.record_failure(
            listing.url,
            "parse" if checked.status == "readable" else _classify_detail_failure(checked),
        )
        state.pending.append((listing, True))
        state.enriched.append(incomplete_workday_listing(listing))
        return
    state.read += 1
    state.completed.append((listing.url, listing.url))
    state.enriched.append(
        replace(
            listing,
            location=checked.location or listing.location,
            facts=workday_detail_facts(listing.facts, checked),
            # The board sweep never carries a body; only the detail read does,
            # and the arrangement hold-out's remote-offer check needs it.
            published_description=checked.text or listing.published_description,
            summary=listing.summary or listing_summary(checked.text),
            source=checked.source,
        )
    )


# Bucket rendered PostingCheck.note strings into the DetailFailureReason vocabulary.
_NOTE_KEYWORDS: tuple[tuple[tuple[str, ...], DetailFailureReason], ...] = (
    (("timeout", "timed out"), "timeout"),
    (("http", "status", " 4", " 5", "connect", "network"), "http_status"),
    (("parse", "unexpected", "no posting", "returned a record"), "parse"),
)


def _classify_exception(exc: BaseException) -> DetailFailureReason:
    """Bucket a raised detail-fetch exception into the observability vocabulary."""
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.HTTPStatusError | httpx.HTTPError):
        return "http_status"
    if isinstance(exc, ValueError | KeyError | TypeError):
        return "parse"
    return "other"


def _classify_detail_failure(check: Any) -> DetailFailureReason:
    """Bucket one PostingCheck failure into the observability vocabulary."""
    note = str(getattr(check, "note", "")).lower()
    for markers, classification in _NOTE_KEYWORDS:
        if any(marker in note for marker in markers):
            return classification
    return "other"


def incomplete_workday_listing(listing: SourceListing) -> SourceListing:
    """Keep list-view facts but do not claim the unread detail made them complete."""
    return replace(
        listing,
        facts=replace(
            listing.facts,
            role_aliases=replace(listing.facts.role_aliases, complete=False),
            locations=replace(listing.facts.locations, complete=False),
        ),
    )


def enrich_workday_candidates(
    checker: Callable[[str], PostingCheck] | None,
    read: SourceRead,
    parameters: JobSearchParameters,
    targets: list[str],
    halted: Callable[[], bool] | None = None,
    *,
    source_url: str,
    source_employer: str = "",
    detail_sink: Callable[[SourceDetailSummary], None] | None = None,
) -> SourceRead:
    """Read eligible Workday details and retain each unresolved identity."""
    if checker is None:
        return read
    state = WorkdayEnrichmentState()
    protected: list[str] = []
    with shared_posting_feeds():
        for listing in read.listings:
            if not workday_candidate(listing, parameters, targets, source_employer=source_employer):
                state.enriched.append(listing)
                continue
            if listing.published_description.strip() and listing.source is not None:
                state.enriched.append(listing)
                state.completed.append((listing.url, listing.url))
                continue
            if not posting_read_allowed(listing.url):
                state.enriched.append(incomplete_workday_listing(listing))
                protected.append(listing.url)
                continue
            if halted is not None and halted():
                state.enriched.append(incomplete_workday_listing(listing))
                state.pending.append((listing, False))
                continue
            detail_read_one(checker, listing, state)
    enrichment = state.summary()
    if detail_sink is not None:
        detail_sink(enrichment)
    detail = read.detail.combined(enrichment)
    protected_urls = tuple(dict.fromkeys((*read.protected_posting_urls, *protected)))
    skip_reasons = dict(read.skip_reasons)
    if protected:
        skip_reasons["protected_posting"] = skip_reasons.get("protected_posting", 0) + len(
            protected
        )
    if not state.pending:
        return replace(
            read,
            listings=state.enriched,
            detail=detail,
            protected_posting_urls=protected_urls,
            skip_reasons=skip_reasons,
        )
    listing_ids = tuple(dict.fromkeys((*(item.url for item in read.listings), *protected_urls)))
    coverage = read.detail_coverage or workday_enumeration_coverage(read)
    if coverage is not None:
        listing_ids = coverage.posting_urls
    enumeration_complete = read.complete or coverage is not None
    deferred = tuple(
        SourceContinuationTarget(
            url=listing.url,
            label=listing.title[:300],
            label_source="visible",
            source_url=source_url,
            structural_context="main",
            relations=("publisher-content", "substantive-label"),
            deferred_reason="workday_detail_pending",
            detail_checkpoint=SourceDetailCheckpoint(
                platform="workday",
                board_url=workday_site_root(identity[1]),
                completed=tuple(dict.fromkeys(state.completed)),
                protected=tuple((url, url) for url in protected_urls),
                failed_attempts=((listing.url, 1),) if failed else (),
                pending_ids=(listing.url,),
                listing_ids=listing_ids,
                listing_total=len(listing_ids) if enumeration_complete else None,
                listing_facts=((listing.url, listing.facts),),
            ),
        )
        for listing, failed in state.pending
        if (identity := identify_board_url(listing.url)) is not None
    )
    return replace(
        read,
        listings=state.enriched,
        complete=False,
        outcome=SourceReadOutcome.CRAWL_CONTINUATION,
        deferred_targets=(*read.deferred_targets, *deferred),
        protected_posting_urls=protected_urls,
        skip_reasons=skip_reasons,
        exhausted_outcome=read.exhausted_outcome or (read.outcome if read.complete else None),
        detail=detail,
        detail_coverage=None,
    )


def workday_candidate(
    listing: SourceListing,
    parameters: JobSearchParameters,
    targets: list[str],
    *,
    source_employer: str = "",
) -> bool:
    """Return whether this listing warrants a detail read."""
    identified = identify_board_url(listing.url)
    if identified is None or identified[0] != "workday":
        return False
    title = screen_discovery_title(
        listing.title,
        listing.facts.role_aliases,
        parameters,
        targets,
        employer=listing.employer if listing.facts.publisher_vouched else "",
        source_employer=source_employer,
    )
    return supports_title_expansion(title, include_bounded_review=True) or summary_may_retain_title(
        title, listing.title, parameters
    )
