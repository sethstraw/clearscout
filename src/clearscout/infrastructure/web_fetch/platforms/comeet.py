"""Comeet careers pages and the public-token careers API."""

from __future__ import annotations

import contextlib
import json
import re
from typing import TYPE_CHECKING

from clearscout.infrastructure.web_fetch.board_mappings import COMEET_MAPPING
from clearscout.infrastructure.web_fetch.board_platform import HOST_START, BoardPlatform
from clearscout.infrastructure.web_fetch.board_reads import (
    MAX_API_RESPONSE_BYTES,
    board_listings,
    page_markup,
    whole,
)

if TYPE_CHECKING:
    from clearscout.application.discovery.source_reading import SourceRead
    from clearscout.infrastructure.web_fetch.board_platform import BoardTransport

COMEET_JOB = re.compile(
    HOST_START + r"(?:www\.)?comeet\.com?/jobs/[^/]+/(?P<company>[A-Za-z0-9.]+)/[^/]+/"
    r"(?P<job>[A-Za-z0-9.]+)",
    re.IGNORECASE,
)
COMEET_BOARD = re.compile(
    HOST_START + r"(?:www\.)?comeet\.co/careers-api/2\.0/company/"
    r"(?P<company>[A-Za-z0-9.]+)/positions\?[^#\s\"']*"
    r"\btoken=(?P<token>[A-Za-z0-9]+)",
    re.IGNORECASE,
)
# The careers page a person opens; it ends at the company uid, before any posting path.
COMEET_PAGE = re.compile(
    HOST_START + r"(?:www\.)?comeet\.com/jobs/(?P<slug>[A-Za-z0-9_-]+)"
    r"(?:/(?P<uid>[A-Za-z0-9.]+))?/?(?=[?#\"'\s<]|$)",
    re.IGNORECASE,
)
# An employer page embedding Comeet states both API keys in COMEET.init, or, through the
# WordPress plugin, in a comeet-named object (comeetvar) keyed comeet_uid and comeet_token.
COMEET_EMBED = re.compile(
    r"(?:(?<![\w.])COMEET\.init\(\s*|(?<![\w.])comeet\w*\s*=\s*)\{"
    r"(?=[^}]*?[\"'](?:company-uid|comeet_uid)[\"']\s*:\s*[\"'](?P<company>[A-Za-z0-9.]+)[\"'])"
    r"(?=[^}]*?[\"'](?:token|comeet_token)[\"']\s*:\s*[\"'](?P<token>[A-Za-z0-9]+)[\"'])"
)
_COMPANY_DATA = re.compile(r"\bCOMPANY_DATA\s*=\s*")


def _read(fetcher: BoardTransport, employer: str, groups: dict[str, str], _url: str) -> SourceRead:
    """Read Comeet's complete public-token careers API with details enabled."""
    company, token = groups.get("company", ""), groups.get("token", "")
    if not token:
        company, token = _page_key(fetcher, groups)
    payload = fetcher.get_json(
        f"https://www.comeet.co/careers-api/2.0/company/{company}/positions"
        f"?token={token}&details=true"
    )
    if not isinstance(payload, list):
        raise ValueError("Comeet feed did not return a list of positions")
    return whole(board_listings(COMEET_MAPPING, payload, employer), raw_count=len(payload))


def _page_key(fetcher: BoardTransport, groups: dict[str, str]) -> tuple[str, str]:
    """Return the company uid and API token a careers page states in its ``COMPANY_DATA``."""
    page = page_markup(
        fetcher.get_text(
            f"https://www.comeet.com/jobs/{groups.get('slug', '')}/{groups.get('uid') or ''}",
            max_bytes=MAX_API_RESPONSE_BYTES,
        )
    )
    assigned = _COMPANY_DATA.search(page)
    data: object = None
    if assigned is not None:
        with contextlib.suppress(json.JSONDecodeError):
            data, _end = json.JSONDecoder().raw_decode(page, assigned.end())
    if not isinstance(data, dict) or not data.get("company_uid") or not data.get("token"):
        raise ValueError("Comeet careers page did not state its company uid and token")
    return str(data["company_uid"]), str(data["token"])


PLATFORM = BoardPlatform(
    name="comeet",
    board_patterns=(COMEET_BOARD, COMEET_PAGE, COMEET_EMBED),
    adapter=_read,
    job_patterns=(COMEET_JOB,),
)
