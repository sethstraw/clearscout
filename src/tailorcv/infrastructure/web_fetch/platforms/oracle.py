"""Oracle Recruiting Cloud career sites and the candidate requisition API."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from tailorcv.application.discovery.source_reading import SourceDetailCheckpoint
from tailorcv.infrastructure.web_fetch.board_mappings import ORACLE_LISTING_MAPPING
from tailorcv.infrastructure.web_fetch.board_platform import (
    BoardPlatform,
    board_query,
    match_groups,
)
from tailorcv.infrastructure.web_fetch.board_reads import (
    PARTITIONS_KEY,
    BoardDeadlineReachedError,
    FacetValue,
    ListingPartition,
    PartitionPlan,
    board_listings,
    continued_page,
    deferred_page,
    distinct_by_id,
    held_continuation,
    held_page,
    held_target,
    page_continuation_target,
    retry_continuation,
    short_of_total,
    split_partition,
    unreadable_continuation,
    whole,
)
from tailorcv.infrastructure.web_fetch.policy import FetchPolicyError, HostHeldError

if TYPE_CHECKING:
    from tailorcv.application.discovery.source_reading import (
        SourceContinuationTarget,
        SourceListing,
        SourceRead,
    )
    from tailorcv.infrastructure.web_fetch.board_platform import BoardTransport

ORACLE = re.compile(
    r"(?P<host>[a-z0-9-]+\.fa\.[a-z0-9]+\.oraclecloud\.com)/hcmUI/CandidateExperience/"
    r"(?P<lang>[a-z]{2}(?:-[A-Za-z]{2})?)/sites/(?P<site>[A-Za-z0-9._-]+)/job/(?P<job>\d+)"
)
# Oracle serves each tenant from its own Fusion host, and one host carries
# several career sites, so requests need the host, the locale, and the site.
ORACLE_BOARD = re.compile(
    r"(?P<host>[a-z0-9-]+\.fa\.[a-z0-9]+\.oraclecloud\.com)/hcmUI/CandidateExperience/"
    r"(?P<lang>[a-z]{2}(?:-[A-Za-z]{2})?)/sites/(?P<site>[A-Za-z0-9._-]+)"
)

ORACLE_PAGE = 25
ORACLE_MAX_PAGES = 100
# The finder answers no rows once offset plus limit passes this many.
ORACLE_WINDOW = 10_000
# The posting-date facet's ranges nest ("under 7 days" sits inside "under 30 days").
_POSTED = "selectedPostingDatesFacet"
_PENDING = "oracle_page_pending"
# A career page names its list page before the query that selects its facets.
_LIST_PAGE = re.compile(r"/(?:jobs|requisitions)(?=[/?])")
# A site several employers share narrows to one by a facet such as the organization.
# The posting-date facet stays the reader's own partition key, so it is never carried.
_CARRIED_FACET = re.compile(r"selected(?!PostingDatesFacet)\w+Facet=[\w.;-]+")
_SHORT_NOTE = "The site answered an empty page before reaching the total it states."
_UNION_NOTE = (
    "The site's posting-date slices did not add up to the total it states, "
    "so this read is not the whole board."
)

type _PageUrl = Callable[[str, int], str]


def _adapter_groups(match: re.Match[str]) -> dict[str, str]:
    """Return a board match's groups plus the facets its career page address selects."""
    page = _LIST_PAGE.match(match.string, match.end())
    query = board_query(match, page.end() if page else None)
    pairs = (f"{key}={value}" for key, value in parse_qsl(query))
    return {**match_groups(match), "facets": ",".join(filter(_CARRIED_FACET.fullmatch, pairs))}


def _root(_url: str, match: re.Match[str]) -> str:
    return (
        f"https://{match.group('host')}/hcmUI/CandidateExperience/"
        f"{match.group('lang')}/sites/{match.group('site')}"
    )


@dataclass
class _Pages:
    """What one run of Oracle pages read, and where the next unread page starts."""

    listings: list[SourceListing] = field(default_factory=list)
    raw_count: int = 0
    next_offset: int | None = None
    total: int = 0
    short: bool = False
    held: HostHeldError | None = None
    read_urls: list[str] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    # A board past the offset window reads one posting-date partition at a time.
    plan: PartitionPlan | None = None
    partitioned: bool = False
    unstable: bool = False
    certified: bool = False

    def posted(self) -> str:
        """Return the posting-date value of the partition being read, or empty."""
        return self.plan.queue[0].facets[0][1] if self.plan else ""


def _request(fetcher: BoardTransport, url: str, pages: _Pages, offset: int) -> Any | None:
    """Fetch one page, or record where the read stopped and return None.

    Only a later page's failure carries a cursor; the first page's failure is raised.
    """
    try:
        payload = fetcher.paginated_request(
            partial(fetcher.get_json, url), pages.listings, pages.raw_count
        )
    except BoardDeadlineReachedError:
        pages.next_offset = offset
        return None
    except HostHeldError as held:
        if not pages.read_urls:
            raise
        pages.next_offset, pages.held = offset, held
        return None
    except (FetchPolicyError, httpx.HTTPError):
        if not pages.read_urls:
            raise
        pages.next_offset = offset
        return None
    pages.read_urls.append(url)
    return payload


def _pages(
    fetcher: BoardTransport,
    page_url: _PageUrl,
    mapped: Callable[[list[Any]], list[SourceListing]],
    pages: _Pages,
    *,
    offset: int,
    limit: int,
) -> _Pages:
    """Read pages from ``offset`` until an empty page, the stated total, or the deadline.

    Oracle answers short pages mid-board, so the offset steps by ``limit``. A
    first page stating more than the window splits the read by posting date.
    """
    for _page in range(ORACLE_MAX_PAGES):
        payload = _request(fetcher, page_url("", offset), pages, offset)
        if payload is None:
            return pages
        requisitions, stated = _answered(payload)
        if offset == 0 and stated > ORACLE_WINDOW and _split(pages, payload, stated):
            return _partitions(fetcher, page_url, mapped, pages, offset=0, limit=limit)
        # Past its window the API answers no rows and states a total of 0.
        pages.total = max(pages.total, stated)
        if not requisitions:
            pages.short = offset < pages.total
            return pages
        fresh = distinct_by_id(requisitions, "Id", pages.seen)
        pages.raw_count += len(fresh)
        pages.listings.extend(mapped(fresh))
        offset += limit
        if pages.total and offset >= pages.total:
            return pages
    pages.next_offset = offset
    return pages


def _split(pages: _Pages, payload: Any, stated: int) -> bool:
    """Plan one partition per posting-date value when the values sum to ``stated``."""
    parts = split_partition(
        ListingPartition((), stated), {_POSTED: _posting_dates(payload)}, size=stated, nested=True
    )
    if parts is None:
        return False
    pages.plan, pages.total, pages.partitioned = PartitionPlan(parts), stated, True
    return True


def _partitions(
    fetcher: BoardTransport,
    page_url: _PageUrl,
    mapped: Callable[[list[Any]], list[SourceListing]],
    pages: _Pages,
    *,
    offset: int,
    limit: int,
) -> _Pages:
    """Read the planned partitions, each by offset until it passes its count or a page is empty.

    Short and repeated pages do not end a partition; the union certifies only when
    it reaches the facet sum fixed at the split with no repeat and no unstable page.
    """
    for _page in range(ORACLE_MAX_PAGES - len(pages.read_urls)):
        plan = pages.plan
        if plan is None:
            break
        count = plan.queue[0].count
        requisitions = _partition_page(fetcher, page_url(pages.posted(), offset), pages, offset)
        if requisitions is None:
            return pages
        fresh = distinct_by_id(requisitions, "Id", pages.seen)
        pages.unstable |= len(fresh) != len(requisitions)
        pages.raw_count += len(fresh)
        pages.listings.extend(mapped(fresh))
        offset += limit
        if requisitions and offset < count and offset + limit <= ORACLE_WINDOW:
            continue
        pages.unstable |= offset < count
        if len(plan.queue) > 1:
            pages.plan, offset = PartitionPlan(plan.queue[1:]), 0
            continue
        pages.plan = None
        pages.certified = not pages.unstable and len(pages.seen) == pages.total
        return pages
    pages.next_offset = offset
    return pages


def _partition_page(
    fetcher: BoardTransport, url: str, pages: _Pages, offset: int
) -> list[Any] | None:
    """Fetch one partition page, refetching once when it states another count than the plan's.

    A page whose count still differs is used, and leaves the read unstable; a
    read already unstable cannot certify, so it skips the refetch.
    """
    count = pages.plan.queue[0].count if pages.plan else 0
    for attempt in range(2):
        payload = _request(fetcher, url, pages, offset)
        if payload is None:
            return None
        requisitions, stated = _answered(payload)
        if not requisitions or stated == count or pages.unstable:
            return requisitions
        pages.unstable |= attempt > 0
    return requisitions


def _read(fetcher: BoardTransport, employer: str, groups: dict[str, str], _url: str) -> SourceRead:
    """Read one Oracle Recruiting Cloud career site through its candidate API.

    The read is whole only once its offset reaches the stated total, an empty
    page ends a board that states none, or its partitions add up to the total.
    """
    host, site = groups.get("host", ""), groups.get("site", "")
    endpoint = f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    locale = groups.get("lang", "") or "en"
    source_url = f"https://{host}/hcmUI/CandidateExperience/{locale}/sites/{site}"
    page_url = _page_urls(endpoint, site, ORACLE_PAGE, groups.get("facets", ""))
    pages = _pages(
        fetcher,
        page_url,
        partial(_listings, employer, host, locale, site),
        _Pages(),
        offset=0,
        limit=ORACLE_PAGE,
    )
    if pages.next_offset is not None:
        cursor = _cursor(page_url(pages.posted(), pages.next_offset), source_url, pages)
        fetched = len(pages.read_urls)
        if pages.held is None:
            return deferred_page(pages.listings, pages.raw_count, cursor, listing_pages=fetched)
        cursor = held_target(cursor, pages.held)
        return held_page(
            deferred_page(pages.listings, pages.raw_count, cursor, listing_pages=fetched),
            pages.held,
        )
    read = whole(pages.listings, raw_count=pages.raw_count, listing_pages=len(pages.read_urls))
    if pages.partitioned and not pages.certified:
        note = _UNION_NOTE
    elif pages.short:
        note = _SHORT_NOTE
    else:
        return read
    return replace(
        short_of_total(read, note),
        skip_reasons={"incomplete_oracle_listing": 1},
        discovery_truncated=True,
    )


def _resumed(target: SourceContinuationTarget, posted: str) -> _Pages | None:
    """Rebuild the pages state a cursor carries, or None when its plan does not match."""
    checkpoint = target.detail_checkpoint
    total = (checkpoint.listing_total or 0) if checkpoint is not None else 0
    retained = parse_qs(urlsplit(target.url).query).get(PARTITIONS_KEY)
    if retained is None:
        return None if posted else _Pages(total=total)
    plan = PartitionPlan.from_query(retained[0])
    if plan is None or checkpoint is None or checkpoint.platform != "oracle":
        return None
    if plan.queue[0].facets != ((_POSTED, posted),) or not total:
        return None
    return _Pages(
        total=total,
        seen=set(checkpoint.listing_ids),
        plan=plan,
        partitioned=True,
        unstable=checkpoint.listing_unstable,
    )


def _continue(
    fetcher: BoardTransport, employer: str, target: SourceContinuationTarget
) -> SourceRead:
    """Read retained Oracle pages within the board deadline and carry the next offset."""
    cursor = _parsed_cursor(target.url)
    source = urlsplit(target.source_url)
    source_parts = [part for part in source.path.split("/") if part]
    try:
        locale = source_parts[source_parts.index("CandidateExperience") + 1]
        site = source_parts[source_parts.index("sites") + 1]
    except (ValueError, IndexError):
        return unreadable_continuation(target, "invalid_oracle_cursor")
    if cursor is None or cursor[0] != site or cursor[3] <= 0:
        return unreadable_continuation(target, "invalid_oracle_cursor")
    _site, facets, posted, limit, offset = cursor
    state = _resumed(target, posted)
    if state is None:
        return unreadable_continuation(target, "invalid_oracle_cursor")
    target_parts = urlsplit(target.url)
    endpoint = urlunsplit((target_parts.scheme, target_parts.netloc, target_parts.path, "", ""))
    page_url = _page_urls(endpoint, site, limit, facets)
    read_pages = _partitions if state.plan is not None else _pages
    try:
        pages = read_pages(
            fetcher,
            page_url,
            partial(_listings, employer, source.netloc, locale, site),
            state,
            offset=offset,
            limit=limit,
        )
    except HostHeldError:
        return held_continuation(target)
    except (FetchPolicyError, httpx.HTTPError):
        return retry_continuation(target, _PENDING)
    return _continued(target, pages, page_url)


def _continued(target: SourceContinuationTarget, pages: _Pages, page_url: _PageUrl) -> SourceRead:
    """Wrap one continuation run, carrying its cursor or marking an uncertified end."""
    deferred: tuple[SourceContinuationTarget, ...] = ()
    if pages.next_offset is not None:
        after = _cursor(page_url(pages.posted(), pages.next_offset), target.source_url, pages)
        deferred = (after if pages.held is None else held_target(after, pages.held),)
    # The first page read is the cursor's own page, which a partition plan addresses.
    read_urls = (target.url, *pages.read_urls[1:])
    read = continued_page(
        target, pages.listings, deferred, read_urls=read_urls, listing_pages=len(read_urls)
    )
    if pages.held is not None:
        return held_page(read, pages.held)
    if pages.short or (pages.partitioned and not deferred and not pages.certified):
        return replace(
            read, skip_reasons={"incomplete_oracle_listing": 1}, discovery_truncated=True
        )
    return read


def _answered(payload: Any) -> tuple[list[Any], int]:
    """Return one response page's requisitions and the total the site states."""
    items = payload.get("items") or []
    answered = items[0] if items else {}
    return answered.get("requisitionList") or [], int(answered.get("TotalJobsCount") or 0)


def _posting_dates(payload: Any) -> tuple[FacetValue, ...]:
    """Return the posting-date facet values one response page states, with their counts."""
    items = payload.get("items") or []
    stated = (items[0] if items else {}).get("postingDatesFacet") or []
    values = [
        FacetValue(str(value["Id"]), value["TotalCount"])
        for value in stated
        if isinstance(value, dict)
        and value.get("Id") is not None
        and type(value.get("TotalCount")) is int
    ]
    return tuple(values) if len(values) == len(stated) else ()


def _cursor(page_url: str, source_url: str, pages: _Pages) -> SourceContinuationTarget:
    """Name the next unread page, carrying the total and any partition plan."""
    if pages.plan is not None:
        page_url = f"{page_url}&{urlencode({PARTITIONS_KEY: pages.plan.as_query()})}"
    target = page_continuation_target(
        page_url,
        source_url=source_url,
        label=f"Oracle page at offset {pages.next_offset}",
        reason=_PENDING,
    )
    if not pages.total:
        return target
    checkpoint = SourceDetailCheckpoint(
        platform="oracle",
        board_url=source_url,
        listing_total=pages.total,
        listing_ids=tuple(sorted(pages.seen)) if pages.plan is not None else (),
        listing_unstable=pages.unstable,
    )
    return replace(target, detail_checkpoint=checkpoint)


def _page_urls(endpoint: str, site: str, limit: int, facets: str = "") -> _PageUrl:
    """Return the page address builder for one site, taking a posting-date value and offset."""
    return lambda posted, offset: _page_url(
        endpoint, site=site, limit=limit, offset=offset, posted=posted, facets=facets
    )


def _page_url(
    endpoint: str, *, site: str, limit: int, offset: int, posted: str = "", facets: str = ""
) -> str:
    """Build the Oracle finder URL without losing its structured separators."""
    carried = f"{facets}," if facets else ""
    facet = f"{_POSTED}={posted}," if posted else ""
    return (
        f"{endpoint}?onlyData=true&expand=requisitionList"
        f"&finder=findReqs;siteNumber={site},{carried}{facet}limit={limit},offset={offset}"
    )


_CURSOR = re.compile(
    rf"findReqs;siteNumber=([^,]+),((?:{_CARRIED_FACET.pattern},)*)"
    rf"(?:{_POSTED}=([^,]+),)?limit=([0-9]+),offset=([0-9]+)"
)


def _parsed_cursor(url: str) -> tuple[str, str, str, int, int] | None:
    """Parse a retained finder cursor into site, carried facets, date value, limit, offset."""
    finder = parse_qs(urlsplit(url).query).get("finder", [""])[0]
    matched = _CURSOR.fullmatch(finder)
    if matched is None:
        return None
    site, facets, posted, limit, offset = matched.groups()
    return site, facets.rstrip(","), posted or "", int(limit), int(offset)


def _listings(
    employer: str, host: str, locale: str, site: str, jobs: list[Any]
) -> list[SourceListing]:
    """Map the identified requisitions from one Oracle response page."""
    return board_listings(
        ORACLE_LISTING_MAPPING,
        jobs,
        employer,
        groups={"host": host, "locale": locale, "site": site},
    )


PLATFORM = BoardPlatform(
    name="oracle",
    board_patterns=(ORACLE_BOARD,),
    adapter=_read,
    job_patterns=(ORACLE,),
    source_roots=((ORACLE, _root),),
    identity_rules=(
        (
            ORACLE_BOARD,
            lambda match: f"{match.group('host').casefold()}/{match.group('site').casefold()}",
        ),
    ),
    posting_api=True,
    continuations={_PENDING: _continue},
    adapter_groups=_adapter_groups,
)
