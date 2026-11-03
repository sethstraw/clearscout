"""Y Combinator company job pages, whose Inertia page props list the company's postings."""

from __future__ import annotations

import json
import re
import unicodedata
from functools import partial
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit

from clearscout.domain.records import PageClassification
from clearscout.infrastructure.web_fetch.board_mappings import page_posting
from clearscout.infrastructure.web_fetch.board_mappings.mapper import stated, stated_text
from clearscout.infrastructure.web_fetch.board_mappings.page_posting import page_posting_declaration
from clearscout.infrastructure.web_fetch.board_platform import BoardPlatform
from clearscout.infrastructure.web_fetch.board_reads import (
    MAX_API_RESPONSE_BYTES,
    PublisherSearchBoard,
    PublisherSearchPage,
    page_markup,
    publisher_search_read,
    required_list_field,
)
from clearscout.infrastructure.web_fetch.ingest.careers import inspect_publisher_page
from clearscout.infrastructure.web_fetch.ingest.html_blocks import html_blocks

if TYPE_CHECKING:
    from collections.abc import Mapping

    from clearscout.application.discovery.source_reading import SourceContinuationTarget, SourceRead
    from clearscout.infrastructure.web_fetch.board_platform import BoardTransport

YCOMBINATOR_BOARD = re.compile(
    r"https?://www\.ycombinator\.com/companies/(?P<company>[a-z0-9-]+)/jobs/?$", re.IGNORECASE
)
_HOST = "www.ycombinator.com"
_REASON = "ycombinator_search_page"
# Emoji joiners and presentation selectors are marks, not symbols, but belong to the badge.
_BADGE_MARKS = frozenset("️‍")
_MAPPING = page_posting_declaration(
    "ycombinator",
    row_places=True,
    # The list's salaryRange is display text such as "$120K - $160K"; the page's markup is exact.
    compensation=lambda posting: (
        stated(posting.record, ("detail", "compensation"))
        or stated_text(posting.record, ("row", "pay"))
        or None
    ),
)


def _title(value: object) -> str:
    """Drop a leading emoji badge; leading punctuation such as ".NET" is part of the title."""
    title = str(value or "").strip()
    start = 0
    while start < len(title) and (
        unicodedata.category(title[start]).startswith("S")
        or title[start] in _BADGE_MARKS
        or title[start].isspace()
    ):
        start += 1
    return title[start:]


def _page_props(page: str, source_url: str) -> dict[str, Any]:
    if inspect_publisher_page(source_url, page).classification is (
        PageClassification.ERROR_OR_CHALLENGE
    ):
        raise ValueError("YC jobs page is an error or challenge page")
    declared = [
        block.attrs["data-page"] for block in html_blocks(page, "div") if "data-page" in block.attrs
    ]
    if len(declared) != 1:
        raise ValueError("YC jobs page did not declare one page state")
    state = json.loads(declared[0])
    props = state.get("props") if isinstance(state, dict) else None
    if not isinstance(props, dict):
        raise ValueError("YC jobs page state did not declare its props")
    return props


def _row(entry: object, source_url: str) -> dict[str, Any]:
    if not isinstance(entry, dict) or type(entry.get("id")) is not int:
        raise ValueError("YC jobs page did not state a stable posting identity")
    address = urljoin(source_url, str(entry.get("url") or ""))
    parsed = urlsplit(address)
    root = urlsplit(source_url).path.rstrip("/") + "/"
    if parsed.netloc != _HOST or not parsed.path.startswith(root) or parsed.path == root:
        raise ValueError("YC jobs page did not state a posting address on its own board")
    location = str(entry.get("location") or "").strip()
    pay = entry.get("salaryRange")
    return {
        "id": str(entry["id"]),
        "url": address,
        "title": _title(entry.get("title")),
        "places": [location] if location else [],
        "pay": pay.strip() if isinstance(pay, str) else "",
    }


def _request(fetcher: BoardTransport, source_url: str, _page: int) -> PublisherSearchPage:
    response = fetcher.get_text(source_url, max_bytes=MAX_API_RESPONSE_BYTES)
    final = response.partition("\n")[0]
    # A renamed company redirects its old slug; postings then sit under the new one.
    board = final if YCOMBINATOR_BOARD.match(final) else source_url
    page = page_markup(response)
    postings = required_list_field(_page_props(page, board), "jobPostings", "YC jobs page")
    return PublisherSearchPage([_row(entry, board) for entry in postings], None)


def _detail(fetcher: BoardTransport, row: dict[str, Any]) -> Mapping[str, Any] | None:
    response = fetcher.get_text(str(row["url"]), max_bytes=MAX_API_RESPONSE_BYTES)
    final, separator, _body = response.partition("\n")
    if separator and final.startswith(("http://", "https://")) and urlsplit(final).netloc != _HOST:
        return None
    declared = page_posting(page_markup(response))
    return None if declared is None else {**declared, "title": _title(declared["title"])}


def _search(
    fetcher: BoardTransport,
    employer: str,
    url: str,
    target: SourceContinuationTarget | None = None,
) -> SourceRead:
    return publisher_search_read(
        fetcher,
        employer,
        source_url=url,
        reason=_REASON,
        first_page=0,
        request=partial(_request, fetcher, url),
        mapping=_MAPPING,
        detail=partial(_detail, fetcher),
        target=target,
    )


_BOARD = PublisherSearchBoard(_search, _MAPPING, lambda fetcher: partial(_detail, fetcher))

PLATFORM = BoardPlatform(
    name="ycombinator",
    board_patterns=(YCOMBINATOR_BOARD,),
    adapter=_BOARD.read,
    continuations={_REASON: _BOARD.resume, "ycombinator_search_detail": _BOARD.resume_detail},
    label="Y Combinator jobs",
)
