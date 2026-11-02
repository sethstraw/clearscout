"""Workday career sites and the paged CXS listing API."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from tailorcv.application.discovery.source_reading import (
    MAX_SOURCE_READ_ATTEMPTS,
    DetailFailureReason,
    SourceDetailCheckpoint,
    SourceDetailCoverage,
    SourceDetailSummary,
)
from tailorcv.domain import SourceReadOutcome
from tailorcv.domain.staged_facts import StagedListingFacts
from tailorcv.infrastructure.web_fetch.board_mappings import WORKDAY_LISTING_MAPPING
from tailorcv.infrastructure.web_fetch.board_platform import (
    HOST_START,
    BoardPlatform,
    host_match,
    query_groups,
)
from tailorcv.infrastructure.web_fetch.board_reads import (
    PARTITIONS_KEY,
    BoardDeadlineReachedError,
    FacetCounts,
    FacetValue,
    ListingPartition,
    PartitionPlan,
    board_listings,
    continued_page,
    deferred_page,
    held_continuation,
    held_target,
    page_continuation_target,
    page_fingerprint,
    page_markup,
    partitioned_size,
    read_result,
    retained_page_fingerprints,
    retry_continuation,
    split_partition,
    unreadable_continuation,
    whole,
    with_page_fingerprints,
    with_query,
)
from tailorcv.infrastructure.web_fetch.listing_facts import workday_detail_facts
from tailorcv.infrastructure.web_fetch.policy import FetchPolicyError, HostHeldError

if TYPE_CHECKING:
    from tailorcv.application.discovery.source_reading import (
        PostingCheck,
        SourceContinuationTarget,
        SourceListing,
        SourceRead,
    )
    from tailorcv.infrastructure.web_fetch.board_platform import BoardTransport

WORKDAY = re.compile(
    HOST_START
    + r"(?P<host>(?P<shared_host>wd\d+\.myworkdaysite\.com)|(?:[\w-]+\.)*myworkdayjobs\.com)/"
    r"(?:(?P<locale>[A-Za-z]{2}-[A-Za-z]{2})/)?"
    r"(?(shared_host)recruiting/(?P<tenant>[A-Za-z0-9_-]+)/)"
    r"(?P<site>[A-Za-z0-9._-]+)/job/"
)
# Workday requests need both the tenant host and career-site path.
WORKDAY_BOARD = re.compile(
    HOST_START
    + r"(?P<host>(?P<shared_host>wd\d+\.myworkdaysite\.com)|[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com)/"
    r"(?:(?P<locale>[A-Za-z]{2}-[A-Za-z]{2})/)?"
    r"(?(shared_host)recruiting/(?P<tenant>[A-Za-z0-9_-]+)/)"
    r"""(?P<site>[A-Za-z0-9._-]+)(?=[/?#\s"'<>]|$)"""
)
# A tenant's CXS name can differ from its host label, as `acme_co` on `acme-co`.
_WORKDAY_STATED_TENANT = re.compile(r"""\btenant["']?\s*:\s*["']([A-Za-z0-9_-]+)["']""")
_WORKDAY_STATED_SITE = re.compile(r"""\bsiteId["']?\s*:\s*["']([A-Za-z0-9._-]+)["']""")

WORKDAY_PAGE = 20
WORKDAY_MAX_PAGES = 100
# Pages one continuation task reads from its cursor, inside the board read deadline.
WORKDAY_CONTINUATION_PAGES = 25
_PENDING = "workday_page_pending"
_DETAIL_PENDING = "workday_detail_pending"
# Facets tried first when a capped board is split; the CXS answer supplies the rest.
_SPLIT_FACETS = ("jobFamilyGroup", "workerSubType", "timeType")


def workday_posting_path(match: re.Match[str]) -> str:
    """Return a WORKDAY match's path after ``/job/``, ending at the posting's own segment."""
    # The posting segment carries the requisition's digits; a location before it
    # and an action such as applyManually after it carry none.
    path = match.string[match.end() :].partition("?")[0].partition("#")[0]
    segments = [segment for segment in path.split("/") if segment]
    numbered = [
        index for index, segment in enumerate(segments) if any(c.isdigit() for c in segment)
    ]
    return "/".join(segments[: numbered[-1] + 1]) if numbered else ""


def workday_stated_tenant(page: str, *, tenant: str = "", site: str = "") -> str:
    """Return the declared tenant when any requested tenant and site agree."""
    stated = _WORKDAY_STATED_TENANT.search(page)
    if stated is None or (tenant and stated.group(1) != tenant):
        return ""
    declared_site = _WORKDAY_STATED_SITE.search(page)
    if site and (declared_site is None or declared_site.group(1) != site):
        return ""
    return stated.group(1)


def workday_site_root(match: re.Match[str]) -> str:
    """Return the public career root, including shared-host tenant and locale."""
    groups = match.groupdict(default="")
    path = groups["site"]
    if groups.get("tenant"):
        path = f"recruiting/{groups['tenant']}/{path}"
    locale = f"{groups['locale']}/" if groups.get("locale") else ""
    return f"https://{groups['host']}/{locale}{path}"


def _posting_in_scope(url: str, root: str) -> bool:
    """Match a canonical posting path to its tenant, locale, and career site."""
    match = host_match(WORKDAY, url)
    if match is None or workday_site_root(match) != root.rstrip("/"):
        return False
    remainder = workday_posting_path(match)
    return bool(remainder) and urlsplit(url).path == f"{urlsplit(root).path}/job/{remainder}"


Search = tuple[tuple[str, str], ...]


def _search(query: str) -> Search:
    """Keep the query pairs that narrow a CXS search, dropping referral tracking."""
    return tuple(
        (key, value)
        for key, value in parse_qsl(query)
        if key.lower() != "source" and not key.lower().startswith("utm_")
    )


def _listing_body(offset: int, limit: int, search: Search = ()) -> dict[str, Any]:
    # Each kept query key is posted as an applied facet and `q` as the search
    # text; a key Workday does not filter on goes undetected.
    facets: dict[str, list[str]] = {}
    for key, value in search:
        if key != "q":
            facets.setdefault(key, []).append(value)
    text = " ".join(value for key, value in search if key == "q")
    return {"appliedFacets": facets, "limit": limit, "offset": offset, "searchText": text}


@dataclass
class _ListingMembership:
    """Workday page identities and instability retained across listing slices."""

    seen: set[str] = field(default_factory=set)
    fingerprints: list[str] = field(default_factory=list)
    total: int | None = None
    unstable: bool = False
    # A partitioned read unions windows toward a size no single page states.
    split: bool = False
    # Set while a partition re-reads rows already folded in before its split.
    overlaps: bool = False
    # A total stated once may be a first-page figure the board has since settled.
    restated: bool = False

    @classmethod
    def resume(
        cls, target: SourceContinuationTarget, *, root_url: str, split: bool = False
    ) -> _ListingMembership:
        checkpoint = target.detail_checkpoint
        if checkpoint is None or checkpoint.platform != "search:workday":
            return cls(fingerprints=list(retained_page_fingerprints(target)), split=split)
        return cls(
            seen=set(checkpoint.listing_ids),
            fingerprints=list(retained_page_fingerprints(target)),
            total=checkpoint.listing_total,
            unstable=checkpoint.listing_unstable
            or (split and checkpoint.listing_total is None)
            or checkpoint.board_url.rstrip("/") != root_url.rstrip("/")
            or any(not _posting_in_scope(url, root_url) for url in checkpoint.listing_ids),
            split=split,
        )

    def observe_total(self, total: int) -> None:
        if total > 0 and not self.split:
            self.restated |= self.total is not None
            self.unstable |= self.total is not None and self.total != total
            self.total = total

    def certifies_enumeration(self) -> bool:
        """Require stable identities matching the retained positive total."""
        return not self.unstable and self.total is not None and len(self.seen) == self.total

    def listing_coverage(self, root_url: str) -> SourceDetailCoverage | None:
        """Certify exact enumerated identities separately from readable bodies."""
        if not self.certifies_enumeration():
            return None
        return SourceDetailCoverage(
            platform="workday", board_url=root_url, posting_urls=tuple(sorted(self.seen))
        )

    def window_completion(self, *, ended: bool) -> bool | None:
        """Return no decision before the listing's observed end, or certify that end."""
        if not ended:
            return None
        complete = not self.unstable and (self.total is None or self.certifies_enumeration())
        self.unstable |= not complete
        return complete

    def ends(self, offset: int, fresh: list[SourceListing] | None, *, empty: bool) -> bool:
        """Report a window's end: an empty or repeated page, or a page adding no rows.

        Outside a split, a page adding no rows ends only past the stated total.
        """
        if empty or fresh is None:
            return True
        if self.split:
            return not fresh and not self.overlaps
        return not fresh and self.total is not None and offset >= self.total

    def fresh_rows(
        self, rows: list[SourceListing], raw_count: int, *, root_url: str
    ) -> list[SourceListing] | None:
        """Return a page's unseen rows, or None when the page repeats a retained one."""
        unique = {row.url: row for row in rows}
        self.unstable |= any(not _posting_in_scope(row.url, root_url) for row in rows)
        self.unstable |= len(unique) != raw_count
        fingerprint = page_fingerprint(row.url for row in rows)
        if fingerprint in self.fingerprints:
            # A publisher that answers past its end with an earlier page confirms
            # a stable window whose identities already match the stated total.
            self.unstable |= not self.split and not self.certifies_enumeration()
            return None
        self.unstable |= not self.overlaps and bool(self.seen.intersection(unique))
        fresh = [row for row in unique.values() if row.url not in self.seen]
        self.seen.update(row.url for row in fresh)
        self.unstable |= self.total is not None and len(self.seen) > self.total
        self.fingerprints.append(fingerprint)
        return fresh


def _listing_context(
    fetcher: BoardTransport, groups: dict[str, str], _url: str
) -> tuple[str, str, str, str]:
    host, site = groups["host"], groups["site"]
    locale = f"{groups['locale']}/" if groups.get("locale") else ""
    path = f"recruiting/{groups['tenant']}/{site}" if groups.get("tenant") else site
    match = host_match(WORKDAY_BOARD, f"https://{host}/{locale}{path}")
    if match is None:
        raise ValueError("Workday career URL has no site context")
    root = workday_site_root(match)
    tenant = groups.get("tenant") or host.split(".", maxsplit=1)[0]
    if groups.get("tenant") and not workday_stated_tenant(
        page_markup(fetcher.get_text(root)), tenant=tenant, site=site
    ):
        raise ValueError("Workday career page declares a different tenant or site")
    return host, site, root, f"https://{host}/wday/cxs/{tenant}/{site}/jobs"


@dataclass
class _Window:
    """The saved search, partition plan, and offset one CXS read pages through."""

    endpoint: str
    search: Search
    offset: int
    limit: int = WORKDAY_PAGE
    plan: PartitionPlan | None = None

    def partition(self) -> ListingPartition | None:
        return self.plan.queue[0] if self.plan else None

    def body(self) -> dict[str, Any]:
        partition = self.partition()
        applied = partition.facets if partition else ()
        return _listing_body(self.offset, self.limit, (*self.search, *applied))

    def url(self) -> str:
        """Return the durable address of the page ``body`` requests."""
        query = [*self.search, *([(PARTITIONS_KEY, self.plan.as_query())] if self.plan else [])]
        address = f"{self.endpoint}?{urlencode(query)}"
        return with_query(address, offset=self.offset, limit=self.limit)


@dataclass
class _Paging:
    """Fold answered CXS pages into one read, splitting a window paging cannot finish."""

    window: _Window
    membership: _ListingMembership
    root_url: str
    listings: list[SourceListing] = field(default_factory=list)
    raw_count: int = 0
    reason: str = ""
    duplicates: int = 0
    unidentified: int = 0
    # CXS states facets on a window's first page; a later short page splits by those.
    facets: FacetCounts = field(default_factory=dict)

    def take(
        self,
        postings: list[Any],
        total: int,
        facets: FacetCounts,
        mapped: Callable[[list[Any]], list[SourceListing]],
    ) -> bool | None:
        """Fold one page in; return whether the read certified at its end, or None to go on."""
        window, membership = self.window, self.membership
        partition = window.partition()
        if window.offset == 0 and self._first_page(partition, total, facets):
            return None
        membership.observe_total(total)
        membership.overlaps = window.plan is not None and window.plan.overlaps
        unstable = membership.unstable
        fresh = self._fold(postings, mapped)
        ended = membership.ends(window.offset, fresh, empty=not postings)
        if partition is not None:
            return self._partition_end(partition, ended=ended)
        return self._window_end(total, fresh, ended=ended, empty=not postings, unstable=unstable)

    def _first_page(
        self, partition: ListingPartition | None, total: int, facets: FacetCounts
    ) -> bool:
        """Check a window's first-page facets; return whether the window was split."""
        window, membership = self.window, self.membership
        self.facets = facets
        applied = [key for key, _ in (*window.search, *(partition.facets if partition else ()))]
        size = partitioned_size(facets, total, applied=applied)
        if partition is not None:
            size = max(size, partition.count)
        if size > total or (window.plan is not None and window.plan.resplit):
            if self._split(facets, size):
                return True
            membership.unstable = True
            if window.plan is not None:
                window.plan = replace(window.plan, resplit=False)
        elif partition is not None and total != partition.count:
            membership.unstable = True
        return False

    def _fold(
        self, postings: list[Any], mapped: Callable[[list[Any]], list[SourceListing]]
    ) -> list[SourceListing] | None:
        """Keep a page's unseen rows, or return None when the page repeats a retained one."""
        if not postings:
            return []
        rows = mapped(postings)
        overlaps = self.membership.overlaps
        fresh = self.membership.fresh_rows(rows, len(postings), root_url=self.root_url)
        if fresh is None:
            return None
        self.listings.extend(fresh)
        self.raw_count += len(postings)
        self.unidentified += len(postings) - len(rows)
        unique = len({row.url for row in rows})
        # A partition re-reading its split window repeats identities by design.
        self.duplicates += len(rows) - unique + (0 if overlaps else unique - len(fresh))
        return fresh

    def ended_skips(self) -> dict[str, int]:
        """Name the ending reason with the repeated and unidentified rows the pages served."""
        counts = {
            self.reason: 1,
            "duplicate_workday_identity": self.duplicates,
            "missing_required_posting_identity": self.unidentified,
        }
        return {reason: count for reason, count in counts.items() if count}

    def _partition_end(self, partition: ListingPartition, *, ended: bool) -> bool | None:
        """Page on within a partition, re-split one that ran dry, or move to the next."""
        window = self.window
        if not ended:
            window.offset += window.limit
            return None
        # A partition that runs dry early is re-read from its first page and split again.
        if window.offset < partition.count and not self.membership.unstable and window.plan:
            window.plan = replace(window.plan, overlaps=True, resplit=True)
            window.offset = 0
            return None
        return self._next_partition()

    def _window_end(
        self,
        total: int,
        fresh: list[SourceListing] | None,
        *,
        ended: bool,
        empty: bool,
        unstable: bool,
    ) -> bool | None:
        """Page on, split a root that ended short of its total, or certify its end."""
        window, membership = self.window, self.membership
        # CXS states a total on a window's first page only.
        stated = total or membership.total or 0
        short = (empty or fresh is None) and window.offset < stated
        if ended and short and not unstable and self._split(self.facets, stated):
            membership.unstable = False
            if window.plan is not None:
                window.plan = replace(window.plan, overlaps=True)
            return None
        complete = membership.window_completion(ended=ended)
        if complete is None:
            window.offset += window.limit
        elif not complete:
            self.reason = _ended_reason(fresh)
        return complete

    def _split(self, facets: FacetCounts, size: int) -> bool:
        """Replace the current window by its partitions under a facet summing to ``size``."""
        window = self.window
        current = window.partition() or ListingPartition((), size)
        ordered = {name: facets[name] for name in _SPLIT_FACETS if name in facets} | dict(facets)
        saved = {key for key, _ in window.search}
        parts = split_partition(
            current,
            {name: values for name, values in ordered.items() if name not in saved},
            size=size,
        )
        if parts is None:
            return False
        if window.plan is None:
            self.membership.split = True
            self.membership.total = size
            window.plan = PartitionPlan(parts)
        else:
            queue = (*parts, *window.plan.queue[1:])
            window.plan = replace(window.plan, queue=queue, resplit=False)
        window.offset = 0
        return True

    def _next_partition(self) -> bool | None:
        """Move to the next queued partition, or certify the union against the size."""
        window = self.window
        if window.plan is None:
            return self.membership.window_completion(ended=True)
        if len(window.plan.queue) > 1:
            window.plan = replace(window.plan, queue=window.plan.queue[1:], resplit=False)
            window.offset = 0
            return None
        complete = self.membership.certifies_enumeration()
        if not complete:
            self.reason = "incomplete_workday_listing"
        return complete

    def verified(
        self,
        fetcher: BoardTransport,
        post: Callable[[str, dict[str, Any]], tuple[str, Any]],
        mapped: Callable[[list[Any]], list[SourceListing]],
    ) -> bool:
        """Page an unstable unsplit listing once more; certify it when it lists exactly ``seen``."""
        window, membership = self.window, self.membership
        expected = len(membership.seen)
        if not self._verifiable(expected):
            return False
        listed: set[str] = set()
        pages_read: list[set[str]] = []
        for offset in range(0, expected + 2 * window.limit, window.limit):
            page = _Window(window.endpoint, window.search, offset, window.limit)
            try:
                _endpoint, payload = fetcher.paginated_request(
                    partial(post, page.endpoint, page.body()), self.listings, self.raw_count
                )
                postings, total = _answered(payload)
            except (BoardDeadlineReachedError, FetchPolicyError, httpx.HTTPError, ValueError):
                return False
            # CXS may state no total past the first page; a stated one must not move.
            if total != expected and (offset == 0 or total):
                return False
            if not postings:
                break
            urls = {row.url for row in mapped(postings)}
            # A board may repeat a whole earlier page past the end instead of answering empty.
            if len(urls) == len(postings) and offset >= expected and urls in pages_read:
                break
            if not self._fresh_page(urls, len(postings), listed):
                return False
            listed |= urls
            pages_read.append(urls)
        else:
            return False
        if listed != membership.seen:
            return False
        membership.unstable = False
        membership.total = expected
        return True

    def _fresh_page(self, urls: set[str], rows: int, listed: set[str]) -> bool:
        return len(urls) == rows and not (listed & urls or urls - self.membership.seen)

    def _verifiable(self, expected: int) -> bool:
        window, membership = self.window, self.membership
        # The pass reads every page plus the empty one that ends it.
        pages = -(-expected // window.limit) + 1
        return not (
            window.plan is not None
            or membership.split
            or expected == 0
            or pages > WORKDAY_CONTINUATION_PAGES
            or (membership.restated and membership.total != expected)
            or any(not _posting_in_scope(url, self.root_url) for url in membership.seen)
        )

    def cursor(self) -> SourceContinuationTarget:
        """Return the continuation that resumes this read at its current window."""
        membership = self.membership
        fingerprints = tuple(membership.fingerprints)
        cursor = with_page_fingerprints(
            page_continuation_target(
                self.window.url(),
                source_url=self.root_url,
                label=f"Workday page at offset {self.window.offset}",
                reason=_PENDING,
            ),
            fingerprints,
        )
        return replace(
            cursor,
            detail_checkpoint=SourceDetailCheckpoint(
                platform="search:workday",
                board_url=self.root_url,
                listing_ids=tuple(sorted(membership.seen)),
                page_fingerprints=fingerprints,
                listing_total=membership.total,
                listing_unstable=membership.unstable,
            ),
        )


def _facet_counts(payload: Any) -> FacetCounts:
    """Return the flat facets a CXS answer states, each value with its id and count."""
    counts: dict[str, tuple[FacetValue, ...]] = {}
    for facet in payload.get("facets") or [] if isinstance(payload, dict) else []:
        name = facet.get("facetParameter") if isinstance(facet, dict) else None
        values = facet.get("values") if isinstance(facet, dict) else None
        if not isinstance(name, str) or not isinstance(values, list):
            continue
        parsed = [
            FacetValue(value["id"], value["count"])
            for value in values
            if isinstance(value, dict)
            and isinstance(value.get("id"), str)
            and type(value.get("count")) is int
        ]
        if parsed and len(parsed) == len(values):
            counts[name] = tuple(parsed)
    return counts


def _read(fetcher: BoardTransport, employer: str, groups: dict[str, str], url: str) -> SourceRead:
    """Read aligned CXS pages within the advertised identity window."""
    host, site, root, endpoint = _listing_context(fetcher, groups, url)
    paging = _Paging(
        _Window(endpoint, _search(groups.get("board_query", "")), 0),
        _ListingMembership(),
        root,
    )
    mapped = partial(
        board_listings,
        WORKDAY_LISTING_MAPPING,
        employer=employer,
        groups={"host": host, "site": site, "root_url": root},
    )
    post = partial(_post_page, fetcher, host, site)
    fetched = 0
    for _page in range(WORKDAY_MAX_PAGES):
        window = paging.window
        try:
            window.endpoint, payload = fetcher.paginated_request(
                partial(post, window.endpoint, window.body()),
                paging.listings,
                paging.raw_count,
            )
            postings, total = _answered(payload)
        except BoardDeadlineReachedError:
            break
        except (FetchPolicyError, httpx.HTTPError, ValueError):
            if not paging.listings:
                raise
            break
        fetched += bool(postings)
        complete = paging.take(postings, total, _facet_counts(payload), mapped)
        if complete is False:
            complete = paging.verified(fetcher, post, mapped)
        if complete:
            # A split partition re-reads root rows by design, so only unidentified rows count.
            return whole(
                paging.listings,
                raw_count=len(paging.listings) + paging.unidentified,
                listing_pages=fetched,
            )
        if complete is not None:
            return replace(
                read_result(paging.listings, raw_count=paging.raw_count, complete=False),
                outcome=SourceReadOutcome.PUBLISHER_LINKED_PAGES,
                skip_reasons=paging.ended_skips(),
                discovery_truncated=True,
            )
    return replace(
        deferred_page(paging.listings, paging.raw_count, paging.cursor(), listing_pages=fetched),
        skip_reasons={},
        discovery_truncated=paging.membership.unstable,
    )


def _ended_reason(fresh: list[SourceListing] | None) -> str:
    """Name why an uncertified listing stopped at its observed end."""
    return "repeated_workday_page" if fresh is None else "incomplete_workday_listing"


def _post_page(
    fetcher: BoardTransport, host: str, site: str, endpoint: str, body: dict[str, Any]
) -> tuple[str, Any]:
    """POST one page, retrying a refused initial legacy tenant from its public root."""
    try:
        return endpoint, fetcher.post_json(endpoint, body)
    except httpx.HTTPStatusError:
        if body["offset"] or host.endswith(".myworkdaysite.com"):
            raise
        try:
            stated = workday_stated_tenant(page_markup(fetcher.get_text(f"https://{host}/{site}")))
        except (FetchPolicyError, httpx.HTTPError):
            stated = ""
        retry = f"https://{host}/wday/cxs/{stated}/{site}/jobs" if stated else endpoint
        if retry == endpoint:
            raise
        return retry, fetcher.post_json(retry, body)


def _continuation_root(
    target: SourceContinuationTarget, *, host: str, tenant: str, site: str
) -> str | None:
    """Resolve actual Workday scope independently from corporate provenance."""
    declared = host_match(WORKDAY_BOARD, target.source_url)
    checkpoint = target.detail_checkpoint
    retained = (
        host_match(WORKDAY_BOARD, checkpoint.board_url)
        if checkpoint is not None and checkpoint.platform == "search:workday"
        else None
    )
    if (
        declared is not None
        and retained is not None
        and workday_site_root(declared) != workday_site_root(retained)
    ):
        return None
    match = declared or retained
    shared_host = host.endswith(".myworkdaysite.com")
    if match is None:
        return None if shared_host else f"https://{host}/{site}"
    if (match.group("host"), match.group("site")) != (host, site) or (
        shared_host and match.group("tenant") != tenant
    ):
        return None
    return workday_site_root(match)


def _continue(
    fetcher: BoardTransport, employer: str, target: SourceContinuationTarget
) -> SourceRead:
    """Resume one scoped CXS cursor within the board deadline and page budget."""
    parsed = urlsplit(target.url)
    query = parse_qsl(parsed.query)
    paging = dict(query)
    search = tuple(
        (key, value) for key, value in query if key not in {"offset", "limit", PARTITIONS_KEY}
    )
    plan = PartitionPlan.from_query(paging[PARTITIONS_KEY]) if PARTITIONS_KEY in paging else None
    if PARTITIONS_KEY in paging and plan is None:
        return unreadable_continuation(target, "invalid_workday_cursor")
    try:
        offset = int(paging.get("offset", ""))
        limit = int(paging.get("limit", str(WORKDAY_PAGE)))
    except ValueError:
        return unreadable_continuation(target, "invalid_workday_cursor")
    path = [part for part in parsed.path.split("/") if part]
    if (
        offset < 0
        or limit <= 0
        or len(path) != 5
        or path[:2] != ["wday", "cxs"]
        or path[-1] != "jobs"
    ):
        return unreadable_continuation(target, "invalid_workday_cursor")
    tenant, site = path[2:4]
    root = _continuation_root(target, host=parsed.netloc, tenant=tenant, site=site)
    if root is None:
        return unreadable_continuation(target, "invalid_workday_cursor")
    shared_host = parsed.netloc.endswith(".myworkdaysite.com")
    if offset % limit:
        return replace(
            continued_page(target, [], (replace(target, automatic_eligible=False),)),
            skip_reasons={"incomplete_workday_listing": 1},
            discovery_truncated=True,
            attempted_target_urls=(),
        )
    window = _Window(
        urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")),
        search,
        offset,
        limit,
        plan,
    )
    post = partial(_post_page, fetcher, parsed.netloc, site)
    try:
        if shared_host and not workday_stated_tenant(
            page_markup(fetcher.get_text(root)), tenant=tenant, site=site
        ):
            return unreadable_continuation(target, "workday_scope_mismatch")
        window.endpoint, payload = post(window.endpoint, window.body())
        _answered(payload)
    except HostHeldError:
        return held_continuation(target)
    except (FetchPolicyError, httpx.HTTPError, ValueError):
        return retry_continuation(target, _PENDING)
    return _continued_pages(
        fetcher,
        target,
        _Paging(
            window,
            _ListingMembership.resume(target, root_url=root, split=plan is not None),
            root,
        ),
        post=post,
        first_page=payload,
        mapped=partial(
            board_listings,
            WORKDAY_LISTING_MAPPING,
            employer=employer,
            groups={"host": parsed.netloc, "site": site, "root_url": root},
        ),
    )


def _continued_pages(
    fetcher: BoardTransport,
    target: SourceContinuationTarget,
    paging: _Paging,
    *,
    post: Callable[[str, dict[str, Any]], tuple[str, Any]],
    first_page: Any,
    mapped: Callable[[list[Any]], list[SourceListing]],
) -> SourceRead:
    """Retain unique identities and stop within the advertised page window."""
    read_urls = [target.url]
    payload = first_page
    while True:
        postings, total = _answered(payload)
        complete = paging.take(postings, total, _facet_counts(payload), mapped)
        if complete is False:
            complete = paging.verified(fetcher, post, mapped)
        if complete is not None:
            ended = continued_page(
                target,
                paging.listings,
                (),
                read_urls=tuple(read_urls),
                listing_pages=len(read_urls),
            )
            if complete:
                return replace(
                    ended, listing_coverage=paging.membership.listing_coverage(paging.root_url)
                )
            return replace(ended, skip_reasons=paging.ended_skips(), discovery_truncated=True)
        if len(read_urls) >= WORKDAY_CONTINUATION_PAGES:
            break
        window = paging.window
        try:
            window.endpoint, payload = fetcher.paginated_request(
                partial(post, window.endpoint, window.body()),
                paging.listings,
                len(paging.listings),
            )
            _answered(payload)
        except (BoardDeadlineReachedError, FetchPolicyError, httpx.HTTPError, ValueError):
            break
        read_urls.append(window.url())
    return replace(
        continued_page(
            target,
            paging.listings,
            (paging.cursor(),),
            read_urls=tuple(read_urls),
            listing_pages=len(read_urls),
        ),
        discovery_truncated=paging.membership.unstable,
    )


def _detail_checkpoint(target: SourceContinuationTarget) -> SourceDetailCheckpoint | None:
    checkpoint = target.detail_checkpoint
    if (
        checkpoint is None
        or checkpoint.platform != "workday"
        or checkpoint.pending_ids != (target.url,)
        or target.url not in checkpoint.listing_ids
    ):
        return None
    root = checkpoint.board_url.rstrip("/")
    source = host_match(WORKDAY_BOARD, target.source_url)
    if source is not None and workday_site_root(source) != root:
        return None
    if any(not _posting_in_scope(url, root) for url in checkpoint.listing_ids):
        return None
    return checkpoint


def _failed_detail(
    target: SourceContinuationTarget,
    checkpoint: SourceDetailCheckpoint,
    reason: DetailFailureReason,
) -> SourceRead:
    failed = dict(checkpoint.failed_attempts)
    failed[target.url] = failed.get(target.url, 0) + 1
    cursor = replace(
        target,
        automatic_eligible=failed[target.url] < MAX_SOURCE_READ_ATTEMPTS,
        detail_checkpoint=replace(checkpoint, failed_attempts=tuple(sorted(failed.items()))),
        manual_detail_retry=False,
    )
    return replace(
        retry_continuation(cursor, _DETAIL_PENDING),
        detail=SourceDetailSummary(
            attempted=1,
            failed=1,
            failure_reasons=((reason, 1),),
            failure_examples=(target.url,),
        ),
    )


def _detail_listing(
    employer: str,
    target: SourceContinuationTarget,
    checkpoint: SourceDetailCheckpoint,
    checked: PostingCheck,
) -> SourceListing | None:
    if (
        checked.status != "readable"
        or checked.platform != "workday"
        or not checked.title.strip()
        or not checked.text.strip()
        or checked.source is None
        or not checked.facts.publisher_vouched
    ):
        return None
    root = checkpoint.board_url.rstrip("/")
    rows = board_listings(
        WORKDAY_LISTING_MAPPING,
        [
            {
                "title": checked.title,
                "externalPath": urlsplit(target.url).path.removeprefix(urlsplit(root).path),
            }
        ],
        employer,
        groups={"root_url": root},
    )
    if len(rows) != 1:
        return None
    original = dict(checkpoint.listing_facts).get(target.url, StagedListingFacts())
    return replace(
        rows[0],
        url=target.url,
        location=checked.location,
        facts=workday_detail_facts(original, checked),
        published_description=checked.text,
        source=checked.source,
    )


def _detail_timeout(
    fetcher: BoardTransport, target: SourceContinuationTarget, checkpoint: SourceDetailCheckpoint
) -> SourceRead:
    """Retain an interrupted body read without spending a failed attempt."""
    if fetcher.deadline_elapsed():
        return replace(
            retry_continuation(target, _DETAIL_PENDING),
            detail=SourceDetailSummary(attempted=1, interrupted=1),
        )
    return _failed_detail(target, checkpoint, "timeout")


def _unreadable_detail(
    fetcher: BoardTransport,
    target: SourceContinuationTarget,
    checkpoint: SourceDetailCheckpoint,
    checked: PostingCheck,
) -> SourceRead:
    """Retain the timeout classification carried by an unreadable CXS result."""
    if (
        checked.status == "unreadable"
        and checked.http_status is None
        and "timeout" in checked.note.casefold()
    ):
        return _detail_timeout(fetcher, target, checkpoint)
    return _failed_detail(
        target, checkpoint, "http_status" if checked.http_status is not None else "parse"
    )


def _continue_detail(
    fetcher: BoardTransport, employer: str, target: SourceContinuationTarget
) -> SourceRead:
    """Recover one exact owed Workday body without repeating board enumeration."""
    checkpoint = _detail_checkpoint(target)
    if checkpoint is None:
        return replace(
            unreadable_continuation(target, "invalid_workday_detail_cursor"),
            attempted_target_urls=(),
            discovery_truncated=True,
        )
    attempts = dict(checkpoint.failed_attempts).get(target.url, 0)
    if attempts >= MAX_SOURCE_READ_ATTEMPTS and not target.manual_detail_retry:
        return replace(
            retry_continuation(replace(target, automatic_eligible=False), _DETAIL_PENDING),
            attempted_target_urls=(),
        )
    try:
        checked = fetcher.workday_detail(target.url)
    except HostHeldError as held:
        return held_continuation(held_target(target, held))
    except BoardDeadlineReachedError:
        return replace(
            retry_continuation(target, _DETAIL_PENDING),
            attempted_target_urls=(),
        )
    except httpx.TimeoutException:
        return _detail_timeout(fetcher, target, checkpoint)
    except (FetchPolicyError, httpx.HTTPError, ValueError) as error:
        return _failed_detail(
            target,
            checkpoint,
            "http_status" if isinstance(error, httpx.HTTPStatusError) else "other",
        )
    listing = _detail_listing(employer, target, checkpoint, checked)
    if listing is None:
        return _unreadable_detail(fetcher, target, checkpoint, checked)
    membership = _ListingMembership(
        seen=set(checkpoint.listing_ids),
        total=checkpoint.listing_total,
        unstable=checkpoint.listing_unstable,
    )
    complete = membership.certifies_enumeration()
    return replace(
        continued_page(target, [listing], ()),
        complete=complete,
        outcome=SourceReadOutcome.ATS_COMPLETE
        if complete
        else SourceReadOutcome.PUBLISHER_LINKED_PAGES,
        detail=SourceDetailSummary(attempted=1, read=1),
        detail_coverage=SourceDetailCoverage(
            platform="workday", board_url=checkpoint.board_url, posting_urls=checkpoint.listing_ids
        )
        if complete
        else None,
        discovery_truncated=checkpoint.listing_unstable,
    )


def _answered(payload: Any) -> tuple[list[Any], int]:
    """Return one CXS page's postings and stated total, refusing a page of another shape."""
    if not isinstance(payload, dict):
        raise ValueError("Workday listing page is not a JSON object")
    postings = payload.get("jobPostings")
    if not isinstance(postings, list):
        raise ValueError("Workday listing page has no jobPostings list")
    try:
        return postings, int(payload.get("total") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("Workday listing page states a non-integer total") from exc


PLATFORM = BoardPlatform(
    name="workday",
    board_patterns=(WORKDAY_BOARD,),
    adapter=_read,
    job_patterns=(WORKDAY,),
    source_roots=((WORKDAY, lambda _url, m: workday_site_root(m)),),
    identity_rules=(
        (
            WORKDAY_BOARD,
            lambda match: (
                f"{match.group('host')}/recruiting/{match.group('tenant')}/{match.group('site')}"
                if match.group("tenant")
                else f"{match.group('host')}/{match.group('site')}"
            ).casefold(),
        ),
    ),
    posting_api=True,
    continuations={_PENDING: _continue, _DETAIL_PENDING: _continue_detail},
    adapter_groups=query_groups,
)
