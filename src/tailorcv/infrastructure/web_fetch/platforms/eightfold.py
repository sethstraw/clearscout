"""Eightfold career sites, named by page markup and read through their public jobs API."""

from __future__ import annotations

import json
import re
from functools import partial
from html import unescape
from typing import TYPE_CHECKING, Any, NamedTuple
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from tailorcv.infrastructure.web_fetch.board_mappings.page_posting import page_posting_declaration
from tailorcv.infrastructure.web_fetch.board_platform import BoardPlatform
from tailorcv.infrastructure.web_fetch.board_reads import (
    MAX_API_RESPONSE_BYTES,
    PublisherSearchPage,
    page_markup,
    publisher_search_detail,
    publisher_search_read,
)
from tailorcv.infrastructure.web_fetch.ingest.html_blocks import html_blocks
from tailorcv.infrastructure.web_fetch.ingest.sitemap import robots_sitemaps
from tailorcv.infrastructure.web_fetch.policy import FetchPolicyError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from tailorcv.application.discovery.source_reading import SourceContinuationTarget, SourceRead
    from tailorcv.infrastructure.web_fetch.board_platform import BoardTransport

# Every Eightfold career page assigns its tenant group in an inline script.
EIGHTFOLD_BOARD = re.compile(
    r"""window\._EF_GROUP_ID\s*=\s*["'](?P<group>[A-Za-z0-9][A-Za-z0-9.-]{0,252})["']"""
)
_DOMAIN = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+")
_PAGE_SIZE = 10
_PENDING = "eightfold_search_page"
_MAPPING = page_posting_declaration("eightfold")


class _Tenant(NamedTuple):
    """The employer domain a career page declares, and whether it lists through PCSX."""

    domain: str
    pcsx: bool


# Career host to the tenant its page last declared, so a detail retry needs no page read.
_DOMAINS: dict[str, _Tenant] = {}


def _robots_domain(fetcher: BoardTransport, host: str) -> str | None:
    """Return the ``domain`` the host's robots.txt sitemap line declares, if one does."""

    def fetch(address: str) -> str | None:
        try:
            return page_markup(fetcher.get_text(address))
        except (FetchPolicyError, httpx.HTTPError):
            return None

    for sitemap in robots_sitemaps(fetch, f"https://{host}"):
        declared = parse_qs(urlsplit(sitemap).query).get("domain", [""])[0]
        if _DOMAIN.fullmatch(declared):
            return declared
    return None


def _declared_domain(fetcher: BoardTransport, url: str) -> _Tenant:
    """Return the tenant the career page's apply configuration or robots declares."""
    host = urlsplit(url).netloc
    response = fetcher.get_text(url, max_bytes=MAX_API_RESPONSE_BYTES)
    final, separator, _body = response.partition("\n")
    if separator and final.startswith("http") and urlsplit(final).netloc != host:
        raise ValueError("Eightfold career page redirected outside its host")
    page = page_markup(response)
    group = EIGHTFOLD_BOARD.search(page)
    if group is None:
        raise ValueError("Eightfold career page does not declare its tenant group")
    declared: object = None
    pcsx = False
    for block in html_blocks(page, "code"):
        if block.attrs.get("id") == "smartApplyData":
            settings = json.loads(unescape(block.inner))
            declared = settings.get("domain") if isinstance(settings, dict) else None
            break
    else:
        # A page with no apply configuration counts only when robots names its own group.
        robots = _robots_domain(fetcher, host)
        declared = robots if robots == group["group"] else None
        pcsx = True
    if isinstance(declared, str) and _DOMAIN.fullmatch(declared):
        _DOMAINS[host] = _Tenant(declared, pcsx)
        return _DOMAINS[host]
    raise ValueError("Eightfold career page does not declare its employer domain")


def _places(position: Mapping[str, Any]) -> list[str]:
    stated = position.get("locations")
    if isinstance(stated, list) and stated:
        return [str(place) for place in stated if str(place).strip()]
    return [str(position.get("location") or "")]


def _record(position: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "title": str(position.get("posting_name") or position.get("name") or ""),
        "description": str(position.get("job_description") or position.get("jobDescription") or ""),
        "employer": "",
        "places": _places(position),
        "places_complete": False,
        "employment_types": [],
        "compensation": None,
    }


def _address(tenant: _Tenant, host: str, position: Mapping[str, Any]) -> str | None:
    """Return the position's address on the career host, or None when it points elsewhere."""
    if tenant.pcsx:
        # PCSX lists a bare path, which the career host serves.
        parsed = urlsplit(str(position.get("positionUrl") or ""))
        if parsed.scheme or parsed.netloc or not parsed.path.startswith("/careers/job/"):
            return None
        return f"https://{host}{parsed.path}"
    address = str(position.get("canonicalPositionUrl") or "")
    parsed = urlsplit(address)
    if parsed.scheme != "https" or parsed.netloc != host or parsed.username:
        return None
    return address


def _rows(tenant: _Tenant, host: str, positions: list[Any]) -> list[dict[str, Any]]:
    """Return the positions whose address stays on the career host."""
    rows = []
    for position in positions:
        if not isinstance(position, dict):
            raise ValueError("Eightfold jobs page contains an invalid position")
        address = _address(tenant, host, position)
        if address is None:
            continue
        record = _record(position)
        rows.append(
            {
                "id": str(position.get("id") or ""),
                "url": address,
                "title": record["title"],
                "detail": record,
            }
        )
    return rows


def _jobs(fetcher: BoardTransport, host: str, domain: str, start: int) -> dict[str, Any]:
    query = urlencode({"domain": domain, "start": start, "num": _PAGE_SIZE})
    payload = fetcher.get_json(f"https://{host}/api/apply/v2/jobs?{query}")
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("positions"), list)
        or not isinstance(payload.get("count"), int)
    ):
        raise ValueError("Eightfold jobs page has no positions and count")
    return payload


def _short(payload: dict[str, Any], start: int) -> bool:
    returned = len(payload["positions"])
    return returned < _PAGE_SIZE and start + returned < payload["count"]


def _reports_error(error: object) -> bool:
    # A clean PCSX answer still carries an error object whose message and body are empty.
    if isinstance(error, dict):
        return bool(error.get("message") or error.get("body"))
    return isinstance(error, str) and bool(error.strip())


def _pcsx_data(fetcher: BoardTransport, url: str) -> dict[str, Any]:
    payload = fetcher.get_json(url)
    if not isinstance(payload, dict):
        raise ValueError("Eightfold PCSX answer has no data")
    # PCSX can answer HTTP 200 with its own failure status and an empty data block.
    if payload.get("status", 200) != 200 or _reports_error(payload.get("error")):
        raise ValueError("Eightfold PCSX answer reports an error")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("Eightfold PCSX answer has no data")
    return data


def _search(
    fetcher: BoardTransport, host: str, tenant: _Tenant, start: int
) -> tuple[list[Any], Any]:
    if tenant.pcsx:
        query = urlencode({"domain": tenant.domain, "query": "", "location": "", "start": start})
        data = _pcsx_data(fetcher, f"https://{host}/api/pcsx/search?{query}")
        positions, total = data.get("positions"), data.get("count")
        if not isinstance(positions, list) or type(total) is not int:
            raise ValueError("Eightfold PCSX search has no positions and count")
        return positions, total
    payload = _jobs(fetcher, host, tenant.domain, start)
    # A short window before the stated count is a truncated answer that one retry usually fills.
    if _short(payload, start):
        payload = _jobs(fetcher, host, tenant.domain, start)
    if _short(payload, start):
        raise ValueError("Eightfold jobs page stayed truncated before its stated count")
    return payload["positions"], payload["count"]


def _request(
    fetcher: BoardTransport, host: str, tenant: _Tenant, start: int
) -> PublisherSearchPage:
    positions, total = _search(fetcher, host, tenant, start)
    following = start + len(positions)
    return PublisherSearchPage(
        _rows(tenant, host, positions),
        following if positions and following < total else None,
        total,
    )


def _position(fetcher: BoardTransport, host: str, tenant: _Tenant, identity: str) -> Any:
    if tenant.pcsx:
        query = urlencode({"position_id": identity, "domain": tenant.domain, "hl": "en"})
        return _pcsx_data(fetcher, f"https://{host}/api/pcsx/position_details?{query}")
    query = urlencode({"domain": tenant.domain})
    return fetcher.get_json(f"https://{host}/api/apply/v2/jobs/{identity}?{query}")


def _detail(
    fetcher: BoardTransport, host: str, tenant: _Tenant, row: dict[str, Any]
) -> Mapping[str, Any] | None:
    listed = row.get("detail")
    if isinstance(listed, dict) and listed.get("description"):
        return listed
    identity = str(row["id"])
    try:
        position = _position(fetcher, host, tenant, identity)
    except ValueError:
        return None
    if not isinstance(position, dict) or str(position.get("id") or "") != identity:
        return None
    return _record(position)


def _read(
    fetcher: BoardTransport,
    employer: str,
    _groups: dict[str, str],
    url: str,
    *,
    target: SourceContinuationTarget | None = None,
) -> SourceRead:
    """Read every position the career page's employer domain lists, ten at a time."""
    host = urlsplit(url).netloc
    tenant = _declared_domain(fetcher, url)
    return publisher_search_read(
        fetcher,
        employer,
        source_url=url,
        reason=_PENDING,
        first_page=0,
        request=partial(_request, fetcher, host, tenant),
        mapping=_MAPPING,
        detail=partial(_detail, fetcher, host, tenant),
        target=target,
    )


def _continue(
    fetcher: BoardTransport, employer: str, target: SourceContinuationTarget
) -> SourceRead:
    return _read(fetcher, employer, {}, target.source_url, target=target)


def _continue_detail(
    fetcher: BoardTransport, employer: str, target: SourceContinuationTarget
) -> SourceRead:
    host = urlsplit(target.source_url).netloc
    tenant = _DOMAINS.get(host) or _declared_domain(fetcher, target.source_url)
    return publisher_search_detail(
        fetcher, employer, target, mapping=_MAPPING, detail=partial(_detail, fetcher, host, tenant)
    )


PLATFORM = BoardPlatform(
    name="eightfold",
    board_patterns=(EIGHTFOLD_BOARD,),
    adapter=_read,
    continuations={_PENDING: _continue, "eightfold_search_detail": _continue_detail},
)
