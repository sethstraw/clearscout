"""Career portals whose search pages server-render job rows under a declared jPaginate count."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit

from tailorcv.domain.records import PageClassification
from tailorcv.infrastructure.web_fetch.board_mappings import page_posting
from tailorcv.infrastructure.web_fetch.board_mappings.page_posting import page_posting_declaration
from tailorcv.infrastructure.web_fetch.board_platform import BoardPlatform
from tailorcv.infrastructure.web_fetch.board_reads import (
    MAX_API_RESPONSE_BYTES,
    PublisherSearchBoard,
    PublisherSearchPage,
    page_continuation_target,
    page_markup,
    publisher_search_read,
    retry_continuation,
    search_page_number,
    with_query,
)
from tailorcv.infrastructure.web_fetch.ingest import strip_tags
from tailorcv.infrastructure.web_fetch.ingest.careers import inspect_publisher_page
from tailorcv.infrastructure.web_fetch.ingest.html_blocks import HtmlBlock, html_blocks

if TYPE_CHECKING:
    from collections.abc import Mapping

    from tailorcv.application.discovery.source_reading import SourceContinuationTarget, SourceRead
    from tailorcv.infrastructure.web_fetch.board_platform import BoardTransport

# Anchored at the start so a page without the markers costs three linear scans, not one per link.
JPAGINATE_SEARCH = re.compile(
    r"""\A(?=.*?["'(]/job_list\.js\b)"""
    r"""(?=.*?\bclass=["'](?:[^"']*\s)?job_list_row[\s"'])"""
    r"""(?=.*?href=["'](?:https?://[^/"'\s]+)?/jobs/search/?["'])""",
    re.DOTALL | re.IGNORECASE,
)
_SEARCH_PATH = re.compile(r"/jobs/search/(?P<search>[0-9]+)/?")
# A title slug may carry percent-encoded punctuation such as an en dash; the last number is the id.
_JOB_PATH = re.compile(r"/jobs/[^/]+-(?P<id>[0-9]+)/?")
_PAGE_COUNT = re.compile(r"(?P<count>[0-9]+)(?:\.0+)?")
_REASON = "jpaginate_search_page"
_MAPPING = page_posting_declaration("jpaginate", places=("row", "places"))


@dataclass(frozen=True)
class _Search:
    """One visit's search id, its declared page count, and its first page's markup."""

    origin: str
    search_id: str
    pages: int | None
    first: str


def _search_root(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}/jobs/search/"


def _scope(url: str) -> str:
    # A saved search root reaches the reader as the numbered search it redirects to.
    path, root = urlsplit(url).path, _search_root(url)
    return root if path == urlsplit(root).path or _SEARCH_PATH.fullmatch(path) else url


def _same_origin(address: str, origin: str) -> bool:
    parsed, source = urlsplit(address), urlsplit(origin)
    return (parsed.scheme, parsed.netloc) == (source.scheme, source.netloc) and not parsed.username


def _ghost_value(page: str, element_id: str) -> int | None:
    values = [
        strip_tags(block.inner).strip()
        for block in html_blocks(page, "div")
        if block.attrs.get("id") == element_id
    ]
    found = _PAGE_COUNT.fullmatch(values[0]) if len(values) == 1 else None
    return int(found["count"]) if found is not None and int(found["count"]) >= 1 else None


def _job_row(anchor: HtmlBlock, origin: str, places: list[str]) -> dict[str, Any] | None:
    address = urljoin(origin, anchor.attrs.get("href", ""))
    identity = _JOB_PATH.fullmatch(urlsplit(address).path)
    if identity is None or not _same_origin(address, origin):
        return None
    return {
        "id": identity["id"],
        "url": address,
        "title": " ".join(strip_tags(anchor.inner).split()),
        "places": places,
    }


def _listed_row(block: str, origin: str) -> dict[str, Any] | None:
    stated = (
        " ".join(strip_tags(span.inner).split())
        for span in html_blocks(block, "span", class_name="location")
    )
    places = [place for place in stated if place]
    return next(
        (
            row
            for anchor in html_blocks(block, "a")
            if (row := _job_row(anchor, origin, places)) is not None
        ),
        None,
    )


def _rows(page: str, origin: str) -> list[dict[str, Any]]:
    listed = (
        _listed_row(block.inner, origin)
        for block in html_blocks(page, "div", class_name="job_list_row")
    )
    return [row for row in listed if row is not None]


def _resolve(fetcher: BoardTransport, source_url: str) -> _Search:
    response = fetcher.get_text(source_url, max_bytes=MAX_API_RESPONSE_BYTES)
    final = response.partition("\n")[0]
    found = _SEARCH_PATH.fullmatch(urlsplit(final).path)
    if found is None or not _same_origin(final, source_url):
        raise ValueError("jPaginate job search did not open a numbered search on its own host")
    page = page_markup(response)
    return _Search(source_url, found["search"], _ghost_value(page, "jPaginateNumPages"), page)


def _request(fetcher: BoardTransport, search: _Search, page: int) -> PublisherSearchPage:
    if search.pages is None or page < 1 or page > search.pages:
        raise ValueError("jPaginate search page lies outside the declared page count")
    markup = search.first
    if page > 1:
        address = urljoin(search.origin, f"/jobs/search/{search.search_id}/page{page}")
        response = fetcher.get_text(address, max_bytes=MAX_API_RESPONSE_BYTES)
        if urlsplit(response.partition("\n")[0]).path != urlsplit(address).path:
            raise ValueError("jPaginate search page redirected away from its numbered page")
        markup = page_markup(response)
    if (
        _ghost_value(markup, "jPaginateCurrPage") != page
        or _ghost_value(markup, "jPaginateNumPages") != search.pages
    ):
        raise ValueError("jPaginate search answered a different page than the one requested")
    rows = _rows(markup, search.origin)
    if not rows:
        raise ValueError("jPaginate search page inside the declared count held no rows")
    return PublisherSearchPage(
        rows, page + 1 if page < search.pages else None, _declared_total(markup)
    )


def _declared_total(page: str) -> int | None:
    stated = [
        strip_tags(block.inner).strip().replace(",", "")
        for block in html_blocks(page, "span", class_name="total_results")
    ]
    return int(stated[0]) if len(stated) == 1 and stated[0].isdigit() else None


def _detail(fetcher: BoardTransport, row: dict[str, Any]) -> Mapping[str, Any] | None:
    address = str(row["url"])
    response = fetcher.get_text(address, max_bytes=MAX_API_RESPONSE_BYTES)
    if urlsplit(response.partition("\n")[0]).path != urlsplit(address).path:
        return None
    page = page_markup(response)
    # A short posting beside a similar-jobs list reads as a collection to the generic
    # classifier, so only a challenge page is refused; the row already names the posting.
    if (
        inspect_publisher_page(address, page).classification
        is PageClassification.ERROR_OR_CHALLENGE
    ):
        return None
    declared = page_posting(page)
    if declared is not None:
        return declared
    # The qualifications block sits inside the description block when both are present.
    body = next(
        (
            block.inner
            for name in ("job_description", "job_qualifications")
            for block in html_blocks(page, "div", class_name=name)
            if strip_tags(block.inner).strip()
        ),
        "",
    )
    headings = [
        " ".join(strip_tags(block.inner).split())
        for block in html_blocks(page, "h1", class_name="title")
    ]
    title = next((heading for heading in headings if heading), str(row.get("title") or ""))
    if not title or not body:
        return None
    return {
        "title": title,
        "description": body,
        "employer": "",
        "places": [],
        "places_complete": False,
        "employment_types": [],
        "compensation": None,
    }


def _search(
    fetcher: BoardTransport,
    employer: str,
    url: str,
    target: SourceContinuationTarget | None = None,
) -> SourceRead:
    # A search id lives for one visit, so every read resolves a fresh one and keeps only the page.
    search = _resolve(fetcher, _search_root(url))
    page = search_page_number(target, 1) if target is not None else 1
    if search.pages is None:
        cursor = target or page_continuation_target(
            with_query(url, __reader_page=page),
            source_url=_scope(url),
            label=f"Publisher search page {page}",
            reason=_REASON,
        )
        held = retry_continuation(replace(cursor, automatic_eligible=False), _REASON)
        return replace(held, skip_reasons={"publisher_search_request_failed": 1})
    return publisher_search_read(
        fetcher,
        employer,
        source_url=_scope(url),
        reason=_REASON,
        first_page=1,
        request=partial(_request, fetcher, search),
        mapping=_MAPPING,
        detail=partial(_detail, fetcher),
        target=target,
    )


_BOARD = PublisherSearchBoard(_search, _MAPPING, lambda fetcher: partial(_detail, fetcher))

PLATFORM = BoardPlatform(
    name="jpaginate",
    board_patterns=(JPAGINATE_SEARCH,),
    adapter=_BOARD.read,
    continuations={_REASON: _BOARD.resume, "jpaginate_search_detail": _BOARD.resume_detail},
    label="jPaginate job search",
)
