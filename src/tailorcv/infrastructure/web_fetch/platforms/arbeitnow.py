"""Arbeitnow, an aggregator whose bounded read takes the newest openings across employers."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from tailorcv.infrastructure.web_fetch.board_mappings import ARBEITNOW_MAPPING
from tailorcv.infrastructure.web_fetch.board_platform import HOST_END, HOST_START, BoardPlatform
from tailorcv.infrastructure.web_fetch.board_reads import (
    AGGREGATOR_LISTINGS,
    board_listings,
    bounded,
)

if TYPE_CHECKING:
    from tailorcv.application.discovery.source_reading import SourceListing, SourceRead
    from tailorcv.infrastructure.web_fetch.board_platform import BoardTransport

# One address lists many employers, so it captures no tenant.
ARBEITNOW_BOARD = re.compile(HOST_START + r"arbeitnow\.com" + HOST_END)

ARBEITNOW_PAGE = 100
ARBEITNOW_MAX_PAGES = AGGREGATOR_LISTINGS // ARBEITNOW_PAGE


def _read(fetcher: BoardTransport, employer: str, _groups: dict[str, str], _url: str) -> SourceRead:
    """Read the newest openings a second aggregator lists, across every employer.

    Paging stops where the payload states no next page, and at
    ``AGGREGATOR_LISTINGS`` regardless. The read is never complete.
    """
    listings: list[SourceListing] = []
    raw_count = 0
    for page in range(1, ARBEITNOW_MAX_PAGES + 1):
        payload = fetcher.get_json(f"https://www.arbeitnow.com/api/job-board-api?page={page}")
        jobs = payload.get("data") or []
        raw_count += len(jobs)
        listings.extend(board_listings(ARBEITNOW_MAPPING, jobs, employer))
        if len(listings) >= AGGREGATOR_LISTINGS:
            break
        if not (payload.get("links") or {}).get("next"):
            break
    return bounded(listings, raw_count=raw_count)


PLATFORM = BoardPlatform(
    name="arbeitnow",
    board_patterns=(ARBEITNOW_BOARD,),
    adapter=_read,
    single_tenant=False,
)
