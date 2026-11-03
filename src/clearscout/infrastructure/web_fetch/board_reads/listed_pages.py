"""Boards whose listing pages link each posting's own page."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from functools import partial
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import httpx

from clearscout.application.discovery.source_reading import (
    DetailFailureReason,
    SourceContinuationTarget,
    SourceDetailCheckpoint,
    SourceDetailSummary,
    SourceRead,
)
from clearscout.domain import SourceReadOutcome
from clearscout.infrastructure.web_fetch.board_mappings import BoardDeclaration, page_posting
from clearscout.infrastructure.web_fetch.board_platform import host_match
from clearscout.infrastructure.web_fetch.board_reads.continuations import (
    answered_gone,
    continued_page,
    held_continuation,
    page_continuation_target,
    retry_continuation,
    unread_sitemap,
    unreadable_continuation,
)
from clearscout.infrastructure.web_fetch.board_reads.payloads import (
    MAX_API_RESPONSE_BYTES,
    distinct_by_id,
    page_markup,
    row_id,
)
from clearscout.infrastructure.web_fetch.board_reads.results import (
    board_listings,
    robots_short,
    short_of_total,
    whole,
)
from clearscout.infrastructure.web_fetch.board_reads.scope import (
    listed_posting_reason,
    listed_sitemap_reason,
    publisher_search_scope,
)
from clearscout.infrastructure.web_fetch.board_reads.sitemaps import (
    sitemap_child_targets,
    sitemap_locations,
)
from clearscout.infrastructure.web_fetch.policy import (
    FetchPolicyError,
    HostHeldError,
    RobotsDeniedError,
)

if TYPE_CHECKING:
    from clearscout.infrastructure.web_fetch.board_platform import BoardContinuation, BoardTransport


# The record a listed page stands for once its publisher answers that it is gone.
_GONE_PAGE: Mapping[str, Any] = MappingProxyType({})


@dataclass
class _ListedOutcomes:
    """The identities one listed read requested, and how each request ended."""

    observed: set[str] = field(default_factory=set)
    refused: set[str] = field(default_factory=set)
    closed: set[str] = field(default_factory=set)
    gone: set[str] = field(default_factory=set)
    answered: set[str] = field(default_factory=set)

    def unconfirmed_gone(self) -> set[str]:
        """Return the gone pages to keep owed: every page this read requested failed.

        A host turning a reader away can answer every page 404 or 410, so one
        page answering is what confirms the rest were removed.
        """
        if len(self.observed) > 1 and not self.answered:
            return set(self.gone)
        return set()


def _listed_detail(
    fetcher: BoardTransport,
    pages: dict[str, str],
    detail: Callable[[str], Mapping[str, Any] | None],
    withdrawn: Callable[[Mapping[str, Any]], bool],
    outcomes: _ListedOutcomes,
    identity: str,
    *,
    gone_closes: bool = False,
) -> Mapping[str, Any] | None:
    """Read one declared body, or a closed page, while retaining every spent failure attempt."""
    try:
        markup = fetcher.get_text(pages[identity], max_bytes=MAX_API_RESPONSE_BYTES)
    except HostHeldError:
        raise
    except RobotsDeniedError:
        outcomes.refused.add(identity)
        raise
    except httpx.TimeoutException:
        if not fetcher.deadline_elapsed():
            outcomes.observed.add(identity)
        raise
    except (FetchPolicyError, httpx.HTTPError) as error:
        outcomes.observed.add(identity)
        if gone_closes and answered_gone(error):
            outcomes.gone.add(identity)
            return _GONE_PAGE
        raise
    outcomes.observed.add(identity)
    outcomes.answered.add(identity)
    record = detail(page_markup(markup))
    if record is not None and withdrawn(record):
        outcomes.closed.add(identity)
        return record
    description = record.get("description") if record is not None else None
    return record if isinstance(description, str) and description.strip() else None


def _gone_counted(read: SourceRead, **counts: int) -> SourceRead:
    """Add each nonzero gone count to the read's skip reasons."""
    stated = {reason: count for reason, count in counts.items() if count}
    return replace(read, skip_reasons={**read.skip_reasons, **stated}) if stated else read


def _gone_settled(
    read: SourceRead, outcomes: _ListedOutcomes, pages: dict[str, str]
) -> tuple[SourceRead, set[str]]:
    """Close the confirmed gone pages and count the unconfirmed ones as failed requests.

    Returns the read and every identity it closed.
    """
    owed = outcomes.unconfirmed_gone()
    closed = outcomes.closed | (outcomes.gone - owed)
    read = replace(
        read, closed_posting_urls=tuple(url for item, url in pages.items() if item in closed)
    )
    read = _gone_counted(read, posting_gone=len(outcomes.gone - owed))
    if not owed:
        return read, closed
    urls = tuple(url for item, url in pages.items() if item in owed)
    detail = replace(
        read.detail, attempted=read.detail.attempted - len(urls), read=read.detail.read - len(urls)
    ).combined(
        SourceDetailSummary(
            attempted=len(urls),
            failed=len(urls),
            failure_reasons=(("http_status", len(urls)),),
            failure_examples=urls,
        )
    )
    return replace(read, detail=detail), closed


def _listed_read_receipt(
    read: SourceRead,
    platform: str,
    source_url: str,
    pages: dict[str, str],
    completed: dict[str, str],
    protected: dict[str, str],
) -> SourceRead:
    """Attach exact native enumeration and published body evidence."""
    return replace(
        read,
        detail_read_urls=tuple(completed.values()),
        publisher_listing_receipts=(
            SourceDetailCheckpoint(
                platform="listed:" + platform,
                board_url=source_url,
                listing_ids=tuple(pages),
                listing_urls=tuple(pages.items()),
                completed=tuple(completed.items()),
                protected=tuple(protected.items()),
                listing_unstable=read.discovery_truncated,
            ),
        ),
    )


def _resumed_listed_read(
    read: SourceRead,
    resume_target: SourceContinuationTarget | None,
    platform: str,
    source_url: str,
    pages: dict[str, str],
    completed: dict[str, str],
    protected: dict[str, str],
) -> SourceRead:
    """Retain exact completed bodies and current membership across a native slice."""
    if resume_target is not None and resume_target.detail_checkpoint is not None:
        completed.update(
            (identity, url)
            for identity, url in resume_target.detail_checkpoint.completed
            if pages.get(identity) == url and url in (read.detail_read_urls or ())
        )
    for target in read.deferred_targets:
        if target.detail_checkpoint is not None:
            completed.update(
                (identity, url)
                for identity, url in target.detail_checkpoint.completed
                if pages.get(identity) == url
            )
    unstable = (
        platform == "jazzhr"
        and resume_target is not None
        and resume_target.detail_checkpoint is not None
        and publisher_search_scope(resume_target) is not None
        and (
            resume_target.detail_checkpoint.listing_unstable
            or dict(resume_target.detail_checkpoint.listing_urls) != pages
        )
    )
    if unstable:
        read = replace(
            read,
            complete=False,
            detail_coverage=None,
            exhausted_outcome=None,
            outcome=SourceReadOutcome.CRAWL_CONTINUATION
            if read.deferred_targets
            else SourceReadOutcome.PUBLISHER_LINKED_PAGES,
            discovery_truncated=True,
            skip_reasons={**read.skip_reasons, "publisher_listing_changed": 1},
            note=" ".join(
                part
                for part in (
                    read.note,
                    "The publisher's job list changed during this scan. "
                    "Scan again to check the current list.",
                )
                if part
            ),
        )
    receipt = _listed_read_receipt(read, platform, source_url, pages, completed, protected)
    return replace(
        receipt,
        deferred_targets=tuple(
            replace(
                target,
                detail_checkpoint=replace(
                    target.detail_checkpoint,
                    listing_ids=tuple(pages),
                    listing_urls=tuple(pages.items()),
                    listing_unstable=target.detail_checkpoint.listing_unstable or unstable,
                ),
            )
            if target.detail_checkpoint is not None
            else target
            for target in read.deferred_targets
        ),
    )


def _listed_pending_targets(
    platform: str,
    source_url: str,
    pending: dict[str, str],
    observed: set[str],
    pending_sitemaps: tuple[str, ...],
    *,
    host_held: bool,
) -> tuple[SourceContinuationTarget, ...]:
    """Keep unresolved original postings and sitemap pages actionable."""
    deferred = (
        *(
            replace(
                page_continuation_target(
                    url,
                    source_url=source_url,
                    label="Listed posting",
                    reason=listed_posting_reason(platform),
                ),
                detail_checkpoint=SourceDetailCheckpoint(
                    platform=platform,
                    board_url=url,
                    pending_ids=(identity,),
                    listing_ids=(identity,),
                    listing_urls=((identity, url),),
                    failed_attempts=((identity, 1),) if identity in observed else (),
                ),
            )
            for identity, url in pending.items()
        ),
        *(
            replace(
                target,
                detail_checkpoint=SourceDetailCheckpoint(
                    platform="listed:" + platform,
                    board_url=target.url,
                ),
            )
            for target in sitemap_child_targets(
                pending_sitemaps,
                source_url=source_url,
                reason=listed_sitemap_reason(platform),
            )
        ),
    )
    return (
        tuple(replace(target, automatic_eligible=False) for target in deferred)
        if host_held
        else deferred
    )


def _listed_resume_target(
    target: SourceContinuationTarget | None,
    pages: dict[str, str],
    platform: str,
) -> SourceContinuationTarget | None:
    """Keep a changed original JazzHR URL owed instead of reusing another body."""
    if target is None or platform != "jazzhr" or publisher_search_scope(target) is None:
        return target
    checkpoint = target.detail_checkpoint
    if checkpoint is None:
        return target
    changed = {
        identity
        for identity, url in checkpoint.completed
        if identity in pages and pages[identity] != url
    }
    if not changed:
        return target
    return replace(
        target,
        detail_checkpoint=replace(
            checkpoint,
            completed=tuple(pair for pair in checkpoint.completed if pair[0] not in changed),
            pending_ids=tuple(sorted(set(checkpoint.pending_ids) | changed)),
        ),
    )


def listed_page_read(
    fetcher: BoardTransport,
    employer: str,
    rows: list[Any],
    *,
    mapping: BoardDeclaration,
    unread_note: str,
    source_url: str,
    groups: dict[str, str] | None = None,
    detail: Callable[[str], Mapping[str, Any] | None] = page_posting,
    withdrawn: Callable[[Mapping[str, Any]], bool] = lambda _record: False,
    pending_sitemaps: tuple[str, ...] = (),
    detail_resume: tuple[str, str] | None = None,
    resume_target: SourceContinuationTarget | None = None,
    sitemap_listed: bool = False,
    gone_sitemaps: int = 0,
    skip_recent: bool = False,
) -> SourceRead:
    """Join each list row (``id``, ``url``) to what its posting page declares.

    A list naming no opening and leaving no sitemap unread reads as unreadable
    rather than as an empty board. Rows the deadline cuts off and
    ``pending_sitemaps`` become continuation targets under the reasons
    ``listed_page_continuations`` registers for ``mapping.platform``.
    ``sitemap_listed`` rows came from a sitemap, which outlives its postings,
    so a page answering 404 or 410 is closed rather than failed, unless the
    read requested several pages and none answered. ``gone_sitemaps`` counts
    the child sitemaps the read found removed. ``skip_recent`` defers rows read
    recently as pending postings without requesting them.
    """
    distinct = distinct_by_id(rows)
    if not distinct and not pending_sitemaps:
        return _gone_counted(
            short_of_total(whole([]), unread_note), sitemap_child_gone=gone_sitemaps
        )
    pages = {row_id(row): str(row.get("url") or "") for row in distinct}
    resume_target = _listed_resume_target(resume_target, pages, mapping.platform)
    outcomes = _ListedOutcomes()

    read = fetcher.detailed_read(
        employer,
        distinct,
        mapping=mapping,
        detail=partial(
            _listed_detail, fetcher, pages, detail, withdrawn, outcomes, gone_closes=sitemap_listed
        ),
        groups=groups,
        withdrawn=lambda record: record is _GONE_PAGE or withdrawn(record),
        missing_fails=True,
        robots_skips=True,
        resume=detail_resume,
        resume_target=resume_target,
        skip_recent=skip_recent and detail_resume is None,
    )
    read, closed = _gone_settled(read, outcomes, pages)
    read = _gone_counted(replace(read, raw_count=len(distinct)), sitemap_child_gone=gone_sitemaps)
    refused, observed = outcomes.refused, outcomes.observed
    completed = {
        listing.requisition_id: listing.url
        for listing in read.listings
        if listing.published_description and pages.get(listing.requisition_id) == listing.url
    }
    read = replace(
        read,
        listings=[
            listing
            for listing in read.listings
            if completed.get(listing.requisition_id) == listing.url
        ],
    )
    protected = {
        identity: url for identity, url in pages.items() if url in read.protected_posting_urls
    }
    if detail_resume is not None:
        root = (
            resume_target.source_url
            if resume_target is not None and publisher_search_scope(resume_target) is not None
            else source_url
        )
        return robots_short(
            _resumed_listed_read(
                read, resume_target, mapping.platform, root, pages, completed, protected
            )
        )
    # A page declaring no posting fails only its own row while the others stage.
    if read.listings and read.outcome is SourceReadOutcome.NO_SUPPORTED_REPRESENTATION:
        read = replace(read, outcome=SourceReadOutcome.CRAWL_CONTINUATION)
    pending = {
        identity: url
        for identity, url in pages.items()
        if identity not in completed
        and identity not in protected
        and identity not in refused
        and identity not in closed
    }
    deferred = _listed_pending_targets(
        mapping.platform,
        source_url,
        pending,
        observed,
        pending_sitemaps,
        host_held="host_held" in read.skip_reasons,
    )
    read = _listed_read_receipt(read, mapping.platform, source_url, pages, completed, protected)
    if not deferred:
        return robots_short(read)
    return robots_short(
        replace(
            read,
            complete=False,
            outcome=SourceReadOutcome.CRAWL_CONTINUATION,
            deferred_targets=deferred,
            exhausted_outcome=SourceReadOutcome.PUBLISHER_INDEX,
        )
    )


# The rows among listed pages that belong to the board a target came from, or
# None when the target names no such board.
type ListedRows = Callable[[list[str], SourceContinuationTarget], list[dict[str, str]] | None]


def job_page_rows(job: re.Pattern[str]) -> ListedRows:
    """Return the rows for pages a platform's job pattern names, keyed by its ``job`` group."""

    def rows(pages: list[str], _target: SourceContinuationTarget) -> list[dict[str, str]]:
        return [
            {"id": match.group("job"), "url": page}
            for page in pages
            if (match := host_match(job, page)) is not None
        ]

    return rows


def job_page_tenant(job: re.Pattern[str]) -> Callable[[SourceContinuationTarget], dict[str, str]]:
    """Return the tenant group a posting continuation's own address names."""

    def groups(target: SourceContinuationTarget) -> dict[str, str]:
        match = host_match(job, target.url)
        return {"tenant": match.group("tenant").casefold()} if match is not None else {}

    return groups


def listed_page_continuations(
    mapping: BoardDeclaration,
    rows: ListedRows,
    *,
    detail: Callable[[str], Mapping[str, Any] | None] = page_posting,
    groups: Callable[[SourceContinuationTarget], dict[str, str] | None] = lambda _target: None,
    withdrawn: Callable[[Mapping[str, Any]], bool] = lambda _record: False,
    sitemaps: bool = False,
) -> dict[str, BoardContinuation]:
    """Return the readers resuming what ``listed_page_read`` deferred for ``mapping.platform``.

    A posting page is read through the platform's own ``detail`` and mapping; a
    child sitemap is filtered by ``rows`` exactly as the first read's sitemap was.
    With ``sitemaps``, a posting page or child sitemap answering 404 or 410 is gone.
    """
    platform = mapping.platform

    def posting(
        fetcher: BoardTransport, employer: str, target: SourceContinuationTarget
    ) -> SourceRead:
        found = rows([target.url], target)
        if not found:
            return unreadable_continuation(target, f"invalid_{platform}_cursor")
        return _listed_posting_read(
            fetcher,
            employer,
            target,
            mapping,
            found[0],
            groups(target),
            detail,
            withdrawn,
            gone_closes=sitemaps,
        )

    def sitemap(
        fetcher: BoardTransport, _employer: str, target: SourceContinuationTarget
    ) -> SourceRead:
        try:
            pages, unread, gone = sitemap_locations(fetcher, target.url, platform)
        except HostHeldError:
            return held_continuation(target)
        except (FetchPolicyError, httpx.HTTPError) as error:
            return unread_sitemap(target, error)
        except ValueError:
            return unreadable_continuation(target, f"invalid_{platform}_sitemap")
        found = rows(pages, target)
        if found is None:
            return unreadable_continuation(target, f"invalid_{platform}_cursor")
        distinct = distinct_by_id(found)
        membership = tuple((row_id(row), str(row["url"])) for row in distinct)
        deferred = (
            *(
                replace(
                    page_continuation_target(
                        row["url"],
                        source_url=target.source_url,
                        label="Listed posting",
                        reason=listed_posting_reason(platform),
                    ),
                    detail_checkpoint=SourceDetailCheckpoint(
                        platform=platform,
                        board_url=row["url"],
                        pending_ids=(row_id(row),),
                        listing_ids=(row_id(row),),
                        listing_urls=((row_id(row), row["url"]),),
                    ),
                )
                for row in distinct
            ),
            *sitemap_child_targets(
                unread, source_url=target.source_url, reason=listed_sitemap_reason(platform)
            ),
        )
        deferred = tuple(
            replace(
                item,
                detail_checkpoint=SourceDetailCheckpoint(
                    platform="listed:" + platform,
                    board_url=item.url,
                ),
            )
            if item.deferred_reason == listed_sitemap_reason(platform)
            else item
            for item in deferred
        )
        continued = _gone_counted(continued_page(target, [], deferred), sitemap_child_gone=gone)
        return replace(
            continued,
            raw_count=len(distinct),
            detail_read_urls=(),
            publisher_listing_receipts=(
                SourceDetailCheckpoint(
                    platform="listed:" + platform,
                    board_url=target.url,
                    listing_ids=tuple(identity for identity, _url in membership),
                    listing_urls=membership,
                ),
            ),
        )

    readers: dict[str, BoardContinuation] = {listed_posting_reason(platform): posting}
    if sitemaps:
        readers[listed_sitemap_reason(platform)] = sitemap
    return readers


def _closed_posting(target: SourceContinuationTarget) -> SourceRead:
    """Finish one listed posting its publisher has closed."""
    return replace(
        continued_page(target, [], ()),
        detail=SourceDetailSummary(attempted=1, read=1),
        closed_posting_urls=(target.url,),
    )


def _failed_posting(target: SourceContinuationTarget, reason: DetailFailureReason) -> SourceRead:
    """Keep one listed posting owed after a failed request, counting the failure."""
    return replace(
        retry_continuation(target, target.deferred_reason),
        detail=SourceDetailSummary(
            attempted=1, failed=1, failure_reasons=((reason, 1),), failure_examples=(target.url,)
        ),
    )


def _unanswered_posting(
    target: SourceContinuationTarget, error: httpx.HTTPError, *, gone_closes: bool
) -> SourceRead:
    """Close a posting its publisher answered gone, or keep it owed after another failure."""
    if gone_closes and answered_gone(error):
        return replace(_closed_posting(target), skip_reasons={"posting_gone": 1})
    return _failed_posting(
        target, "http_status" if isinstance(error, httpx.HTTPStatusError) else "other"
    )


def owed_gone_postings(
    batch: list[tuple[SourceContinuationTarget, SourceRead]],
) -> list[tuple[SourceContinuationTarget, SourceRead]]:
    """Keep a batch's gone postings owed when every posting page it requested failed.

    A host turning a reader away can answer every page 404 or 410, so one
    posting page answering is what confirms the rest were removed.
    """
    requested = [read for _target, read in batch if read.detail.attempted]
    answered = any(
        (read.detail.read and "posting_gone" not in read.skip_reasons)
        or "posting_page_declared_no_posting" in read.skip_reasons
        for read in requested
    )
    if len(requested) < 2 or answered:
        return batch
    return [
        (target, _failed_posting(target, "http_status"))
        if "posting_gone" in read.skip_reasons
        else (target, read)
        for target, read in batch
    ]


def _listed_posting_read(
    fetcher: BoardTransport,
    employer: str,
    target: SourceContinuationTarget,
    mapping: BoardDeclaration,
    row: Mapping[str, Any],
    groups: dict[str, str] | None,
    detail: Callable[[str], Mapping[str, Any] | None],
    withdrawn: Callable[[Mapping[str, Any]], bool],
    *,
    gone_closes: bool = False,
) -> SourceRead:
    """Read one declared posting and retain its actual detail outcome."""
    if fetcher.deadline_elapsed():
        return replace(
            retry_continuation(target, target.deferred_reason),
            detail=SourceDetailSummary(capped=1),
        )
    try:
        page = fetcher.get_text(target.url, max_bytes=MAX_API_RESPONSE_BYTES)
    except HostHeldError:
        return held_continuation(target)
    except httpx.TimeoutException:
        interrupted = fetcher.deadline_elapsed()
        return replace(
            retry_continuation(target, target.deferred_reason),
            detail=SourceDetailSummary(
                attempted=1,
                failed=int(not interrupted),
                interrupted=int(interrupted),
                failure_reasons=() if interrupted else (("timeout", 1),),
                failure_examples=() if interrupted else (target.url,),
            ),
        )
    except httpx.HTTPError as error:
        return _unanswered_posting(target, error, gone_closes=gone_closes)
    except RobotsDeniedError:
        return unreadable_continuation(target, "robots_disallowed")
    except FetchPolicyError:
        return retry_continuation(target, target.deferred_reason)
    record = detail(page_markup(page))
    if record is not None and withdrawn(record):
        return _closed_posting(target)
    listings = (
        board_listings(mapping, [{"row": row, "detail": record}], employer, groups)
        if record is not None
        else []
    )
    if not any(
        listing.published_description
        and listing.requisition_id == row_id(row)
        and listing.url == target.url
        for listing in listings
    ):
        return replace(
            retry_continuation(target, target.deferred_reason),
            skip_reasons={"posting_page_declared_no_posting": 1},
            detail=SourceDetailSummary(
                attempted=1,
                failed=1,
                failure_reasons=(("parse", 1),),
                failure_examples=(target.url,),
            ),
        )
    proof = (
        tuple(
            listing.url
            for listing in listings
            if listing.published_description
            and listing.requisition_id == row_id(row)
            and listing.url == target.url
        )
        if publisher_search_scope(target) is not None
        else None
    )
    return replace(
        continued_page(target, listings, ()),
        detail=SourceDetailSummary(attempted=1, read=1),
        detail_read_urls=proof,
    )
