"""Continuation targets and the reads that defer, hold, or retry them."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal

import httpx

from tailorcv.application.discovery.source_reading import (
    SourceContinuationTarget,
    SourceListing,
    SourceRead,
)
from tailorcv.domain import SourceReadOutcome
from tailorcv.infrastructure.web_fetch.board_reads.results import (
    BoardDeadlineReachedError,
    read_result,
    whole,
)
from tailorcv.infrastructure.web_fetch.policy import FetchPolicyError, HostHeldError

if TYPE_CHECKING:
    from tailorcv.infrastructure.web_fetch.board_platform import BoardTransport


# One answered page: its mapped listings, the records it held, and the total the
# board states, 0 when it states none.
type PageAnswer = tuple[list[SourceListing], int, int]


def page_continuation_target(
    url: str, *, source_url: str, label: str, reason: str
) -> SourceContinuationTarget:
    """Build one automatic page or exact-posting continuation target."""
    return SourceContinuationTarget(
        url=url,
        label=label,
        label_source="none",
        source_url=source_url,
        structural_context="other",
        relations=("same-origin", "publisher-content"),
        automatic_eligible=True,
        deferred_reason=reason,
    )


def deferred_page(
    listings: list[SourceListing],
    raw_count: int,
    cursor: SourceContinuationTarget,
    *,
    listing_pages: int,
) -> SourceRead:
    """Return the incomplete read whose remaining pages resume at ``cursor``."""
    return read_result(
        listings,
        raw_count=raw_count,
        complete=False,
        exhausted_outcome=SourceReadOutcome.PUBLISHER_INDEX,
        deferred_targets=(cursor,),
        listing_pages=listing_pages,
    )


def continued_page(
    target: SourceContinuationTarget,
    listings: list[SourceListing],
    deferred: tuple[SourceContinuationTarget, ...],
    *,
    read_urls: tuple[str, ...] = (),
    listing_pages: int = 0,
) -> SourceRead:
    """Wrap a successful structured continuation, naming every page URL it read in ``read_urls``."""
    return SourceRead(
        listings=listings,
        complete=False,
        raw_count=max(1, len(listings)),
        outcome=(
            SourceReadOutcome.CRAWL_CONTINUATION
            if deferred
            else SourceReadOutcome.PUBLISHER_LINKED_PAGES
        ),
        deferred_targets=deferred,
        attempted_target_urls=read_urls or (target.url,),
        listing_pages=listing_pages,
    )


def retry_continuation(target: SourceContinuationTarget, reason: str) -> SourceRead:
    """Keep a transiently unreadable structured page in the durable frontier."""
    return SourceRead(
        listings=[],
        complete=False,
        raw_count=1,
        outcome=SourceReadOutcome.CRAWL_CONTINUATION,
        skip_reasons={"fetch_failed": 1},
        deferred_targets=(replace(target, deferred_reason=reason),),
        attempted_target_urls=(target.url,),
    )


def held_continuation(target: SourceContinuationTarget) -> SourceRead:
    """Keep a target whose host asked for a longer wait; no request was made, so no attempt."""
    return SourceRead(
        listings=[],
        complete=False,
        raw_count=1,
        outcome=SourceReadOutcome.CRAWL_CONTINUATION,
        skip_reasons={"host_held": 1},
        deferred_targets=(target,),
        attempted_target_urls=(),
    )


def answered_gone(error: Exception) -> bool:
    """Return whether a request failed with the publisher saying the address is gone."""
    return isinstance(error, httpx.HTTPStatusError) and error.response.status_code in (404, 410)


def gone_sitemap(target: SourceContinuationTarget) -> SourceRead:
    """Finish an unread child sitemap the publisher has since removed."""
    return replace(continued_page(target, [], ()), skip_reasons={"sitemap_child_gone": 1})


def unread_sitemap(target: SourceContinuationTarget, error: Exception) -> SourceRead:
    """Finish a child sitemap whose request failed when it is gone, or keep it owed."""
    if answered_gone(error):
        return gone_sitemap(target)
    return retry_continuation(target, target.deferred_reason)


def held_reason(held: HostHeldError) -> Literal["time_limit_reached", "host_held"]:
    """Name a hold the way its cursor resumes: on its own when paced, by hand otherwise."""
    return "time_limit_reached" if held.paced else "host_held"


def held_target(target: SourceContinuationTarget, held: HostHeldError) -> SourceContinuationTarget:
    """Keep ``target`` automatic only when the hold is paced."""
    return replace(target, automatic_eligible=held.paced and target.automatic_eligible)


def held_page(read: SourceRead, held: HostHeldError) -> SourceRead:
    """Mark a read cut by an unpaced hold so its cursor waits for a manual resume."""
    if held.paced:
        return read
    return replace(
        read,
        skip_reasons={**read.skip_reasons, "host_held": 1},
        note=(
            "The publisher requested a wait before the next page read. "
            "After the wait, use Read deferred publisher links in source history, "
            "or scan the source again."
        ),
    )


def unreadable_continuation(target: SourceContinuationTarget, reason: str) -> SourceRead:
    """Finish an invalid internal cursor without issuing an unsafe request."""
    return SourceRead(
        listings=[],
        complete=False,
        raw_count=1,
        outcome=SourceReadOutcome.PUBLISHER_LINKED_PAGES,
        skip_reasons={reason: 1},
        attempted_target_urls=(target.url,),
    )


def paged_board_read(
    fetcher: BoardTransport,
    pages: Iterable[tuple[Callable[[], Any], SourceContinuationTarget]],
    answered: Callable[[Any], PageAnswer],
    ceiling: SourceContinuationTarget,
) -> SourceRead:
    """Read a board page by page until a page is empty or the stated total is reached.

    Each page carries the cursor that resumes at it, so the deadline defers the
    unread page; a board still answering when ``pages`` runs out resumes at ``ceiling``.
    """
    listings: list[SourceListing] = []
    raw_count = 0
    fetched = 0
    for request, cursor in pages:
        try:
            payload = fetcher.paginated_request(request, listings, raw_count)
        except BoardDeadlineReachedError:
            return deferred_page(listings, raw_count, cursor, listing_pages=fetched)
        except HostHeldError as held:
            return held_page(
                deferred_page(
                    listings, raw_count, held_target(cursor, held), listing_pages=fetched
                ),
                held,
            )
        mapped, records, total = answered(payload)
        fetched += bool(records)
        raw_count += records
        listings.extend(mapped)
        if not records or (total and raw_count >= total):
            return whole(listings, raw_count=raw_count, listing_pages=fetched)
    return deferred_page(listings, raw_count, ceiling, listing_pages=fetched)


def continued_board_page(
    target: SourceContinuationTarget,
    request: Callable[[], Any],
    answered: Callable[[Any], PageAnswer],
    following: Callable[[int, int], SourceContinuationTarget | None],
    *,
    pending: str,
    retain_record_counts: bool = False,
) -> SourceRead:
    """Read one retained page of a paged board and carry the cursor ``following`` names.

    ``following`` takes the page's record count and stated total, and names no
    cursor once the board has nothing after this page.
    """
    try:
        payload = request()
    except HostHeldError:
        return held_continuation(target)
    except (FetchPolicyError, httpx.HTTPError):
        return retry_continuation(target, pending)
    listings, records, total = answered(payload)
    after = following(records, total)
    read = continued_page(target, listings, () if after is None else (after,), listing_pages=1)
    if not retain_record_counts:
        return read
    skipped = records - len(listings)
    return replace(
        read,
        raw_count=records,
        skip_reasons={"missing_required_posting_identity": skipped} if skipped else {},
    )
