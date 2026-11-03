"""Shopify's own careers page, whose React Router stream lists every posting it hires for."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from clearscout.infrastructure.web_fetch.board_mappings.page_posting import page_posting_declaration
from clearscout.infrastructure.web_fetch.board_platform import HOST_START, BoardPlatform
from clearscout.infrastructure.web_fetch.board_reads import (
    MAX_API_RESPONSE_BYTES,
    job_page_rows,
    listed_page_continuations,
    listed_page_read,
    page_markup,
    required_list_field,
)

if TYPE_CHECKING:
    from clearscout.application.discovery.source_reading import SourceRead
    from clearscout.infrastructure.web_fetch.board_platform import BoardTransport

# The page is the employer's own site, so its markup names the board rather than its URL.
SHOPIFY_CAREERS = re.compile(
    r"""<link\b[^>]*\brel=["']canonical["'][^>]*\bhref=["']https://www\.shopify\.com/careers/?["']""",
    re.IGNORECASE,
)
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# The route resolves a posting by its id; the slug before it is decoration.
SHOPIFY_JOB = re.compile(
    HOST_START + rf"www\.shopify\.com/careers/[a-z0-9-]*_(?P<job>{_UUID})(?![\w-])",
    re.IGNORECASE,
)
_ROOT = "https://www.shopify.com/careers"
_STREAM = re.compile(r'streamController\.enqueue\((".*?")\);</script>', re.DOTALL)
_MAPPING = page_posting_declaration("shopify")


def _container(raw: Any) -> Any:
    """Return an empty container for a stored object or array, or a scalar as itself."""
    if isinstance(raw, dict):
        return {}
    # A list led by a string is a typed value, such as a date or a promise.
    if isinstance(raw, list) and not (raw and isinstance(raw[0], str)):
        return []
    return raw


def _value(values: list[Any], index: Any, memo: dict[int, Any]) -> Any:
    """Return the value stored at ``index``; a container's entries are indexes too."""
    # A negative index stands for null, undefined or a similar constant.
    if type(index) is not int or not 0 <= index < len(values):
        return None
    if index in memo:
        return memo[index]
    raw = values[index]
    decoded = memo[index] = _container(raw)
    if isinstance(decoded, dict):
        for key, item in raw.items():
            name = _value(values, int(key.removeprefix("_")), memo)
            decoded[str(name)] = _value(values, item, memo)
    elif decoded is not raw:
        decoded.extend(_value(values, item, memo) for item in raw)
    return decoded


def _streamed(page: str) -> Any:
    """Decode the page's turbo-stream: a flat array whose objects map "_{key index}" to values."""
    found = _STREAM.search(page)
    if found is None:
        raise ValueError("Shopify careers page streamed no route data")
    values = json.loads(json.loads(found.group(1)))
    if not isinstance(values, list) or not values:
        raise ValueError("Shopify careers stream was not a value table")
    try:
        return _value(values, 0, {})
    except RecursionError as exc:
        raise ValueError("Shopify careers stream nested too deeply") from exc


def _route(page: str, suffix: str) -> dict[str, Any]:
    """Return the one route loader whose key ends with ``suffix``."""
    root = _streamed(page)
    loaders = root.get("loaderData") if isinstance(root, dict) else None
    routes = [
        data
        for key, data in (loaders.items() if isinstance(loaders, dict) else ())
        if key.endswith(suffix) and isinstance(data, dict)
    ]
    if len(routes) != 1:
        raise ValueError(f"Shopify careers stream did not carry one {suffix} loader")
    return routes[0]


def _openings(page: str) -> list[dict[str, str]]:
    """Return each listed posting's id and its page address."""
    rows = []
    for entry in required_list_field(_route(page, "/careers"), "jobPostingsWithJobs", "Shopify"):
        posting = entry.get("jobPosting") if isinstance(entry, dict) else None
        if not isinstance(posting, dict) or posting.get("isListed") is not True:
            continue
        identity, title = str(posting.get("id") or ""), str(posting.get("title") or "")
        if re.fullmatch(_UUID, identity) is None or not title:
            raise ValueError("Shopify careers stream listed a posting without an id and title")
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        rows.append({"id": identity, "url": f"{_ROOT}/{slug}_{identity}"})
    return rows


def _fields(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _country(place: dict[str, Any]) -> str:
    postal = _fields(_fields(place.get("address")).get("postalAddress"))
    return str(postal.get("addressCountry") or "").strip()


def _place_names(places: list[dict[str, Any]]) -> dict[str, str]:
    """Return each location's shown name, a region-wide one naming the countries filed under it.

    A remote "Americas" location sits beside offices in Canada and the United States
    under an "Americas" hierarchy, and its own country is the region's name.
    """
    names = {
        str(place.get("id")): str(place.get("externalName") or place.get("name") or "").strip()
        for place in places
    }
    for region in (place for place in places if place.get("type") == "LocationHierarchy"):
        title = str(region.get("name") or "").strip()
        members = [place for place in places if place.get("parentLocationId") == region.get("id")]
        countries = sorted({c for place in members if (c := _country(place)) and c != title})
        for place in members:
            if countries and str(place.get("name") or "").strip() == title:
                names[str(place.get("id"))] = f"{title} ({', '.join(countries)})"
    return names


def _page_posting(page: str) -> Mapping[str, Any] | None:
    """Return the posting a job page's own route loader states, or None without one."""
    try:
        data = _route(page, "/careers/$posting")
    except ValueError:
        return None
    posting = data.get("jobPosting")
    if not isinstance(posting, dict):
        return None
    names = _place_names(
        [place for place in data.get("locations") or () if isinstance(place, dict)]
    )
    ids = _fields(posting.get("locationIds"))
    wanted = [ids.get("primaryLocationId"), *(ids.get("secondaryLocationIds") or ())]
    places = [name for item in wanted if item and (name := names.get(str(item)))]
    pay = _fields(posting.get("compensation"))
    shown = _fields(pay.get("displaySettings")).get("shouldShowOnJobBoard") is True
    employment = str(posting.get("employmentType") or "")
    return {
        "title": str(posting.get("title") or ""),
        "description": str(posting.get("descriptionHtml") or ""),
        "employer": "",
        "places": list(dict.fromkeys(places)),
        "places_complete": bool(places) and len(places) == len([item for item in wanted if item]),
        "employment_types": [employment] if employment else [],
        "compensation": (str(pay.get("compensationTierSummary") or "") or None) if shown else None,
        "listed": posting.get("isListed"),
    }


def _withdrawn(record: Mapping[str, Any]) -> bool:
    return record.get("listed") is False


def _read(fetcher: BoardTransport, employer: str, _groups: dict[str, str], url: str) -> SourceRead:
    """Read every posting the careers page lists and each posting page's body."""
    if urlsplit(url).hostname != "www.shopify.com":
        raise ValueError("A Shopify careers canonical link was served from another host")
    page = page_markup(fetcher.get_text(_ROOT, max_bytes=MAX_API_RESPONSE_BYTES))
    return listed_page_read(
        fetcher,
        employer,
        _openings(page),
        mapping=_MAPPING,
        unread_note="The Shopify careers page listed no openings.",
        source_url=url,
        detail=_page_posting,
        withdrawn=_withdrawn,
    )


PLATFORM = BoardPlatform(
    name="shopify",
    label="Shopify careers",
    board_patterns=(SHOPIFY_CAREERS,),
    adapter=_read,
    continuations=listed_page_continuations(
        _MAPPING, job_page_rows(SHOPIFY_JOB), detail=_page_posting, withdrawn=_withdrawn
    ),
)
