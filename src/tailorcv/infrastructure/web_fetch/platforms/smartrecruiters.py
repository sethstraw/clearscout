"""SmartRecruiters career pages and the offset-paged public postings API."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from tailorcv.application.discovery.source_reading import SourceRead
from tailorcv.infrastructure.web_fetch.board_mappings import SMARTRECRUITERS_MAPPING
from tailorcv.infrastructure.web_fetch.board_platform import BoardPlatform
from tailorcv.infrastructure.web_fetch.board_reads import (
    board_listings,
    continued_page,
    deferred_page,
    held_continuation,
    page_continuation_target,
    retry_continuation,
    unreadable_continuation,
    whole,
)
from tailorcv.infrastructure.web_fetch.policy import FetchPolicyError, HostHeldError

if TYPE_CHECKING:
    from tailorcv.application.discovery.source_reading import (
        SourceContinuationTarget,
        SourceListing,
    )
    from tailorcv.infrastructure.web_fetch.board_platform import BoardTransport

SMARTRECRUITERS_BOARD = re.compile(r"careers\.smartrecruiters\.com/(?P<org>[A-Za-z0-9_-]+)")
SMARTRECRUITERS_JOB = re.compile(
    r"(?:^|//)jobs\.smartrecruiters\.com/(?P<org>[A-Za-z0-9_-]+)/"
    r"(?P<job>\d+(?:-[A-Za-z0-9_-]+)?)(?=[/?#]|$)"
)
SMARTRECRUITERS_API = re.compile(r"api\.smartrecruiters\.com/v1/companies/(?P<org>[A-Za-z0-9_-]+)")
# A board listing's own `ref` names the posting by its API address rather than its page.
SMARTRECRUITERS_POSTING_REF = re.compile(
    r"api\.smartrecruiters\.com/v1/companies/(?P<org>[A-Za-z0-9_-]+)/postings/(?P<job>\d+)"
)

SMARTRECRUITERS_PAGE = 100
SMARTRECRUITERS_MAX_PAGES = 100
_PENDING = "smartrecruiters_page_pending"
# A grouped board pages its openings by department id or by a location name such as "Toronto, CA".
_GROUP_TYPES = frozenset({"department", "location"})
_GROUP_VALUE = re.compile(r"[\w][\w .,'()-]*")


def _read(fetcher: BoardTransport, employer: str, groups: dict[str, str], _url: str) -> SourceRead:
    """Page SmartRecruiters' postings by offset until its stated ``totalFound``."""
    org = groups.get("org", "")
    source_url = f"https://jobs.smartrecruiters.com/{org}"
    listings: list[SourceListing] = []
    raw_count = 0
    for page in range(SMARTRECRUITERS_MAX_PAGES):
        offset = raw_count
        page_url = _page_url(org, offset=offset)
        stop = fetcher.page_or_stop(
            page_url,
            fetcher.get_json,
            listings,
            raw_count,
            first_page=page == 0,
            cursor=_cursor(page_url, source_url, offset),
        )
        if isinstance(stop, SourceRead):
            return stop
        jobs = stop.get("content") or []
        raw_count += len(jobs)
        listings.extend(_listings(employer, org, jobs))
        if _exhausted(stop, len(jobs), raw_count, SMARTRECRUITERS_PAGE):
            return whole(listings, raw_count=raw_count, listing_pages=page + 1)
    return deferred_page(
        listings,
        raw_count,
        _cursor(_page_url(org, offset=raw_count), source_url, raw_count),
        listing_pages=SMARTRECRUITERS_MAX_PAGES,
    )


def _continue(
    fetcher: BoardTransport, employer: str, target: SourceContinuationTarget
) -> SourceRead:
    """Read one retained SmartRecruiters page and carry its next offset."""
    parsed = urlsplit(target.url)
    path = [part for part in parsed.path.split("/") if part]
    query = parse_qs(parsed.query)
    try:
        offset = int(query.get("offset", [""])[0])
        limit = int(query.get("limit", [str(SMARTRECRUITERS_PAGE)])[0])
    except ValueError:
        return unreadable_continuation(target, "invalid_smartrecruiters_cursor")
    if len(path) != 4 or path[:2] != ["v1", "companies"] or path[3] != "postings":
        return unreadable_continuation(target, "invalid_smartrecruiters_cursor")
    org = path[2]
    try:
        payload = fetcher.get_json(target.url)
    except HostHeldError:
        return held_continuation(target)
    except (FetchPolicyError, httpx.HTTPError):
        return retry_continuation(target, _PENDING)
    jobs = payload.get("content") or []
    next_offset = offset + len(jobs)
    deferred = (
        ()
        if _exhausted(payload, len(jobs), next_offset, limit)
        else (
            _cursor(
                _page_url(org, offset=next_offset, limit=limit), target.source_url, next_offset
            ),
        )
    )
    return continued_page(target, _listings(employer, org, jobs), deferred, listing_pages=1)


def _cursor(page_url: str, source_url: str, offset: int) -> SourceContinuationTarget:
    return page_continuation_target(
        page_url,
        source_url=source_url,
        label=f"SmartRecruiters page at offset {offset}",
        reason=_PENDING,
    )


def _page_url(org: str, *, offset: int, limit: int = SMARTRECRUITERS_PAGE) -> str:
    return (
        f"https://api.smartrecruiters.com/v1/companies/{org}/postings?limit={limit}&offset={offset}"
    )


def _exhausted(payload: Any, page_rows: int, read_rows: int, limit: int) -> bool:
    """A stated total ends paging only once reached or on an empty page; else a short page."""
    total = int(payload.get("totalFound") or 0)
    if total:
        return read_rows >= total or page_rows == 0
    return page_rows < limit


def _listings(employer: str, org: str, jobs: list[Any]) -> list[SourceListing]:
    """Map one SmartRecruiters page; ``org`` builds the posting URL when no ref is given."""
    return board_listings(SMARTRECRUITERS_MAPPING, jobs, employer, groups={"org": org})


def _publisher_paging_addresses(
    url: str, org: str, filters: dict[str, str], tag: str, attrs: dict[str, str]
) -> set[str]:
    from urllib.parse import urlencode, urljoin

    source = urlsplit(url)
    endpoint = urljoin(url, attrs.get("data-more", ""))
    target = urlsplit(endpoint)
    expected = f"/{org}/api/" + ("groups" if tag == "div" else "more")
    if target.netloc != source.netloc or target.path != expected or target.query:
        return set()
    try:
        current = int(attrs.get("data-page", ""))
        total = int(attrs.get("data-groups-pages", "")) if tag == "div" else None
    except ValueError:
        return set()
    if current < 0:
        return set()
    if tag == "div":
        pages = range(current + 1, total) if total is not None else ()
        return {
            endpoint + "?" + urlencode(sorted({**filters, "page": str(position)}.items()))
            for position in pages
        }
    value = unquote(attrs.get("data-value", ""))
    if attrs.get("data-type") in _GROUP_TYPES and _GROUP_VALUE.fullmatch(value):
        values = {
            **filters,
            "page": str(current + 1),
            "type": attrs["data-type"],
            "value": value,
        }
        return {endpoint + "?" + urlencode(sorted(values.items()))}
    return set()


def _publisher_next_page(url: str, filters: dict[str, str], page: str) -> str | None:
    """Return the no-script pager's "Next page" address when it only advances ``page``."""
    from urllib.parse import parse_qsl, urljoin

    from tailorcv.infrastructure.web_fetch.ingest.html_blocks import html_blocks

    source = urlsplit(url)
    for pager in html_blocks(page, "li", class_name="sr-nojs"):
        for anchor in html_blocks(pager.inner, "a"):
            if re.sub(r"<[^>]*>|\s+", " ", anchor.inner).strip() != "Next page":
                continue
            target = urlsplit(urljoin(url, anchor.attrs.get("href", "")))
            query = dict(parse_qsl(target.query, keep_blank_values=True))
            position = query.pop("page", "")
            if query.get("search") == "":
                del query["search"]
            if (
                target.scheme == "https"
                and target.netloc == source.netloc
                and target.path.rstrip("/") == source.path.rstrip("/")
                and query == filters
                and position.isdigit()
            ):
                # The publisher's own spelling, so its plain pager links dedupe against it.
                return target._replace(fragment="").geturl()
    return None


def _group_next_page(url: str, org: str, page: str) -> str | None:
    """Return a group fragment's following page while this one lists any posting."""
    from urllib.parse import parse_qsl, urlencode

    from tailorcv.infrastructure.web_fetch.ingest.html_blocks import html_blocks

    source = urlsplit(url)
    query = dict(parse_qsl(source.query))
    posting = re.compile(rf"smartrecruiters\.com/{re.escape(org)}/\d")
    if (
        source.path.rstrip("/") != f"/{org}/api/more"
        or set(query) != {"page", "type", "value"}
        or query["type"] not in _GROUP_TYPES
        or not query["page"].isdigit()
        or not _GROUP_VALUE.fullmatch(query["value"])
        or not any(posting.search(a.attrs.get("href", "")) for a in html_blocks(page, "a"))
    ):
        return None
    following = {**query, "page": str(int(query["page"]) + 1)}
    return source._replace(query=urlencode(sorted(following.items())), fragment="").geturl()


def _numbered_pages(url: str, org: str, filters: dict[str, str], page: str) -> list[str]:
    """Return the addresses a numbered pager for this page should carry."""
    if org == urlsplit(url).path.strip("/"):
        following = _publisher_next_page(url, filters, page)
        return [following] if following else []
    following = _group_next_page(url, org, page)
    # The current page beside its next reads as one listing, even holding a single posting.
    return [url, following] if following else []


def publisher_collection_markup(url: str, page: str) -> str:
    """Expose only this publisher's declared group, department, and next-page GET links."""
    from html import escape
    from urllib.parse import parse_qsl

    from tailorcv.infrastructure.web_fetch.ingest.html_blocks import html_blocks

    source = urlsplit(url)
    match = re.fullmatch(r"/(?P<org>[A-Za-z0-9_-]+)(?:/api/(?:groups|more))?/?", source.path)
    if source.hostname != "careers.smartrecruiters.com" or match is None:
        return page
    org = match["org"]
    filters = {
        key: value for key, value in parse_qsl(source.query) if key not in {"page", "type", "value"}
    }
    addresses: set[str] = set()
    for tag, class_name in (("div", "js-openings-load"), ("a", "js-more")):
        for block in html_blocks(page, tag, class_name=class_name):
            addresses.update(_publisher_paging_addresses(url, org, filters, tag, block.attrs))
    links = "".join(
        f'<li><a href="{escape(address, quote=True)}">View jobs</a></li>'
        for address in sorted(addresses)
    )
    numbered = _numbered_pages(url, org, filters, page)
    # Leading the page, the pager marking claims this address before the plain "Next page"
    # anchor does, and lets the crawler follow the chain past its gateway depth.
    pager = "".join(
        f'<li><a class="page-numbers" href="{escape(address, quote=True)}">View jobs</a></li>'
        for address in numbered
    )
    pager = f'<ul class="pagination">{pager}</ul>' if pager else ""
    return pager + page + ("<ul>" + links + "</ul>" if links else "")


PLATFORM = BoardPlatform(
    name="smartrecruiters",
    label="SmartRecruiters",
    board_patterns=(SMARTRECRUITERS_BOARD, SMARTRECRUITERS_API),
    adapter=_read,
    job_patterns=(SMARTRECRUITERS_JOB,),
    # Unanchored board patterns also match a detail URL's tenant segment.
    source_roots=(
        (
            SMARTRECRUITERS_BOARD,
            lambda _url, match: f"https://careers.smartrecruiters.com/{match.group('org')}",
        ),
    ),
    identity_rules=(
        (SMARTRECRUITERS_BOARD, lambda match: match.group("org").casefold()),
        (SMARTRECRUITERS_JOB, lambda match: match.group("org").casefold()),
    ),
    posting_api=True,
    continuations={_PENDING: _continue},
)
