"""TBK careers indexes whose root declares an anonymous paginated search."""

from __future__ import annotations

import json
import re
from functools import partial
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

from tailorcv.infrastructure.web_fetch.board_mappings.page_posting import page_posting_declaration
from tailorcv.infrastructure.web_fetch.board_platform import BoardPlatform
from tailorcv.infrastructure.web_fetch.board_reads import (
    MAX_API_RESPONSE_BYTES,
    PublisherSearchBoard,
    PublisherSearchPage,
    page_markup,
    page_posting_detail,
    publisher_search_read,
    required_list_field,
)
from tailorcv.infrastructure.web_fetch.ingest.html_blocks import html_blocks

if TYPE_CHECKING:
    from tailorcv.application.discovery.source_reading import SourceContinuationTarget, SourceRead
    from tailorcv.infrastructure.web_fetch.board_platform import BoardTransport

TBK_CONFIG = re.compile(r'\bTBK_APP_CONTEXT\s*=\s*\{\s*"api_map"\s*:')
_REASON = "tbk_search_page"
_MAPPING = page_posting_declaration("tbk", row_places=True)


def _assignment(page: str, name: str) -> dict[str, Any]:
    matches = []
    for block in html_blocks(page, "script"):
        found = re.search(rf"\b{re.escape(name)}\s*=\s*", block.inner)
        if found is not None:
            value, _end = json.JSONDecoder().raw_decode(block.inner, found.end())
            matches.append(value)
    if len(matches) != 1 or not isinstance(matches[0], dict):
        raise ValueError(f"TBK root did not declare one {name} object")
    return matches[0]


def _config(fetcher: BoardTransport, source_url: str) -> tuple[str, int, int, dict[str, str]]:
    page = page_markup(fetcher.get_text(source_url, max_bytes=MAX_API_RESPONSE_BYTES))
    base = str(_assignment(page, "TBK_GUTENBERG").get("apiBase") or "")
    parsed, source = urlsplit(base), urlsplit(source_url)
    if (
        parsed.scheme != source.scheme
        or parsed.netloc != source.netloc
        or parsed.path.rstrip("/") != "/app/themes/tbk-ba3e-theme/api"
        or parsed.query
        or parsed.fragment
        or parsed.username
    ):
        raise ValueError("TBK search API base did not belong to this root")
    routes = required_list_field(_assignment(page, "TBK_APP_CONTEXT"), "api_map", "TBK")
    search = [
        route
        for route in routes
        if isinstance(route, dict) and route.get("function") == "getFilteredJobs"
    ]
    if len(search) != 1 or search[0] != {
        "function": "getFilteredJobs",
        "method": "GET",
        "route": "/wp/career-search",
        "anonymous": True,
    }:
        raise ValueError("TBK root did not declare the anonymous career-search GET")
    indexes = [
        block
        for block in html_blocks(page, "div")
        if block.attrs.get("data-component") == "CareersIndex"
    ]
    if len(indexes) != 1:
        raise ValueError("TBK root did not declare one careers index")
    hydration = _assignment(page, "TBK_HYDRATE_COMPONENTS")
    props = hydration.get(indexes[0].attrs.get("data-cid", ""))
    if not isinstance(props, dict):
        raise ValueError("TBK careers index did not declare its search settings")
    per_page = props.get("perPage", 5)
    if type(per_page) is not int or per_page <= 0:
        raise ValueError("TBK careers index did not declare a positive page size")
    filters = dict(parse_qsl(source.query, keep_blank_values=True))
    filters.update(parse_qsl(source.fragment.removeprefix("!"), keep_blank_values=True))
    current = int(filters.pop("currentPage", "1"))
    if current < 1:
        raise ValueError("TBK careers index did not name a positive page")
    filters.pop("loadingMore", None)
    filters.pop("perPage", None)
    return base.rstrip("/") + search[0]["route"], per_page, current, filters


def _request(
    fetcher: BoardTransport,
    endpoint: str,
    per_page: int,
    filters: dict[str, str],
    page: int,
) -> PublisherSearchPage:
    query = {
        **filters,
        "currentPage": str(page),
        "perPage": str(per_page),
        "loadingMore": str(int(page > 1)),
    }
    payload = fetcher.get_json(endpoint + "?" + urlencode(query))
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise ValueError("TBK career-search did not report a successful response")
    data = payload.get("data")
    cards = required_list_field(data, "cards", "TBK career-search")
    rows = []
    for card in cards:
        if not isinstance(card, dict) or not card.get("ID"):
            raise ValueError("TBK career-search did not state a stable card identity")
        link = card.get("careerLink")
        if not isinstance(link, dict) or not link.get("href"):
            raise ValueError("TBK career-search did not state a posting address")
        rows.append(
            {
                "id": str(card["ID"]),
                "url": urljoin(endpoint, str(link["href"])),
                "title": card.get("careerTitle", ""),
                "places": [
                    place["name"]
                    for place in card.get("careerLocation", [])
                    if isinstance(place, dict) and place.get("name")
                ],
            }
        )
    total = data.get("total") if isinstance(data, dict) else None
    return PublisherSearchPage(
        rows, page + 1 if cards else None, total if type(total) is int and total >= 0 else None
    )


def _search(
    fetcher: BoardTransport,
    employer: str,
    source_url: str,
    target: SourceContinuationTarget | None = None,
) -> SourceRead:
    endpoint, per_page, current, filters = _config(fetcher, source_url)
    return publisher_search_read(
        fetcher,
        employer,
        source_url=source_url,
        reason=_REASON,
        first_page=current,
        request=partial(_request, fetcher, endpoint, per_page, filters),
        mapping=_MAPPING,
        detail=page_posting_detail(fetcher),
        target=target,
    )


_BOARD = PublisherSearchBoard(_search, _MAPPING, page_posting_detail)

PLATFORM = BoardPlatform(
    name="tbk",
    board_patterns=(TBK_CONFIG,),
    adapter=_BOARD.read,
    continuations={_REASON: _BOARD.resume, "tbk_search_detail": _BOARD.resume_detail},
    label="TBK careers",
)
