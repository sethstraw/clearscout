"""Getro talent-network boards, read from the jobs state their Next page embeds."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from tailorcv.infrastructure.web_fetch.board_mappings import GETRO_MAPPING
from tailorcv.infrastructure.web_fetch.board_platform import HOST_END, HOST_START, BoardPlatform
from tailorcv.infrastructure.web_fetch.board_reads import (
    MAX_API_RESPONSE_BYTES,
    board_listings,
    page_markup,
    short_of_total,
    whole,
)
from tailorcv.infrastructure.web_fetch.ingest.html_blocks import html_blocks

if TYPE_CHECKING:
    from tailorcv.application.discovery.source_reading import SourceRead
    from tailorcv.infrastructure.web_fetch.board_platform import BoardTransport

GETRO_HOSTED = re.compile(
    HOST_START
    + r"(?P<network>(?!(?:api|www|cdn|app|jobs)\.)[a-z0-9-]+)\.getro\.com"
    + HOST_END
    + r"/jobs(?![\w-])",
    re.IGNORECASE,
)
# A custom-domain board states its network and its jobs list in __NEXT_DATA__; Next escapes
# "<" inside that JSON, so the state never runs past the script's first "<".
GETRO_STATE = re.compile(
    r'<script\b[^>]*\bid=["\']__NEXT_DATA__["\'][^>]*>'
    r'(?=[^<]*?"network"\s*:\s*\{[^{}<]*?"id"\s*:\s*"?(?P<network>\d+))'
    r'(?=[^<]*?"initialState"\s*:\s*\{[^<]*?"jobs"\s*:\s*\{[^{}\[\]<]*?"found"\s*:\s*\[)',
    re.IGNORECASE,
)


def _jobs_state(page: str) -> dict[str, Any] | None:
    """Return the page's ``initialState.jobs`` object, or None when it states none."""
    roots = [
        block for block in html_blocks(page, "script") if block.attrs.get("id") == "__NEXT_DATA__"
    ]
    if len(roots) != 1:
        return None
    try:
        data = json.loads(roots[0].inner)
    except json.JSONDecodeError:
        return None
    outer = data.get("props") if isinstance(data, dict) else None
    props = outer.get("pageProps") if isinstance(outer, dict) else None
    state = props.get("initialState") if isinstance(props, dict) else None
    jobs = state.get("jobs") if isinstance(state, dict) else None
    return jobs if isinstance(jobs, dict) else None


def _read(fetcher: BoardTransport, employer: str, _groups: dict[str, str], url: str) -> SourceRead:
    """Read the jobs a board page embeds; certify only a settled list matching its total."""
    page = page_markup(fetcher.get_text(url, max_bytes=MAX_API_RESPONSE_BYTES))
    state = _jobs_state(page) or {}
    found, total = state.get("found"), state.get("total")
    if not isinstance(found, list) or type(total) is not int or total < 0:
        return short_of_total(whole([]), "The Getro board page embedded no readable jobs list.")
    listings = board_listings(GETRO_MAPPING, found, employer)
    read = whole(listings, raw_count=len(found))
    settled = state.get("initialized") is True and state.get("isLazyLoading") is False
    if len(found) == total and settled:
        return read
    note = (
        f"The Getro board page states {total} jobs and embeds {len(found)}; "
        "a larger board ships only its first page."
        if len(found) != total
        else "The Getro board page did not state a settled jobs list."
    )
    # The stated total is the coverage measure; the unshipped jobs are not identity failures.
    return short_of_total(replace(read, raw_count=max(total, len(found))), note)


PLATFORM = BoardPlatform(
    name="getro",
    label="Getro",
    board_patterns=(GETRO_HOSTED, GETRO_STATE),
    adapter=_read,
    identity_rules=((GETRO_HOSTED, lambda match: match.group("network").casefold()),),
    single_tenant=False,
)
