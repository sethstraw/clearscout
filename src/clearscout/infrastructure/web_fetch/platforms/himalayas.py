"""Himalayas, an aggregator whose bounded read takes the newest openings across employers."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from clearscout.infrastructure.web_fetch.board_mappings import HIMALAYAS_MAPPING
from clearscout.infrastructure.web_fetch.board_platform import HOST_START, BoardPlatform
from clearscout.infrastructure.web_fetch.board_reads import (
    AGGREGATOR_LISTINGS,
    board_listings,
    bounded,
)

if TYPE_CHECKING:
    from clearscout.application.discovery.source_reading import SourceListing, SourceRead
    from clearscout.infrastructure.web_fetch.board_platform import BoardTransport

# One address lists many employers, so it captures no tenant.
HIMALAYAS_BOARD = re.compile(HOST_START + r"himalayas\.app/jobs")

# The API caps a request at twenty.
HIMALAYAS_PAGE = 20
HIMALAYAS_MAX_PAGES = AGGREGATOR_LISTINGS // HIMALAYAS_PAGE


def _read(fetcher: BoardTransport, employer: str, _groups: dict[str, str], _url: str) -> SourceRead:
    """Read the newest openings one aggregator lists, across every employer.

    The read is never complete. It is bounded by ``AGGREGATOR_LISTINGS``
    rather than by what the site holds, so absence from it says nothing
    about whether an opening was withdrawn.
    """
    listings: list[SourceListing] = []
    raw_count = 0
    cursor = ""
    for _page in range(HIMALAYAS_MAX_PAGES):
        query = {"limit": HIMALAYAS_PAGE, **({"cursor": cursor} if cursor else {})}
        payload = fetcher.get_json(f"https://himalayas.app/jobs/api?{urlencode(query)}")
        jobs = payload.get("jobs") or []
        raw_count += len(jobs)
        listings.extend(board_listings(HIMALAYAS_MAPPING, jobs, employer))
        total = int(payload.get("totalCount") or 0)
        # The API deprecates offset paging; each page names the cursor of the next.
        cursor = str(payload.get("nextCursor") or "")
        if not cursor or len(jobs) < HIMALAYAS_PAGE:
            break
        if total and raw_count >= total:
            break
    return bounded(listings, raw_count=raw_count)


PLATFORM = BoardPlatform(
    name="himalayas",
    board_patterns=(HIMALAYAS_BOARD,),
    adapter=_read,
    single_tenant=False,
)
