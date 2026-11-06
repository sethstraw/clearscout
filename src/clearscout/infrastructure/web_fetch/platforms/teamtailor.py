"""Teamtailor tenant boards and the paged public full-body RSS feed."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from clearscout.infrastructure.web_fetch.board_mappings import TEAMTAILOR_MAPPING
from clearscout.infrastructure.web_fetch.board_mappings.teamtailor import TEAMTAILOR_NAMESPACE
from clearscout.infrastructure.web_fetch.board_platform import HOST_END, HOST_START, BoardPlatform
from clearscout.infrastructure.web_fetch.board_reads import (
    BoardDeadlineReachedError,
    board_listings,
    required_feed_root,
    whole,
)

if TYPE_CHECKING:
    from clearscout.application.discovery.source_reading import SourceListing, SourceRead
    from clearscout.infrastructure.web_fetch.board_platform import BoardTransport

# A tenant on a regional cluster keeps its region label, as in acme.na.teamtailor.com.
_TENANT = r"[a-z0-9-]+(?:\.[a-z]{2})?"
TEAMTAILOR_JOB = re.compile(
    rf"(?<![\w.-])(?P<tenant>{_TENANT})\.teamtailor\.com/jobs/(?P<job>\d+)(?:-|\b)",
    re.IGNORECASE,
)
TEAMTAILOR_BOARD = re.compile(
    HOST_START
    + rf"(?<!\.)(?P<tenant>(?!(?:www|app|api|assets|support)\.){_TENANT})\.teamtailor\.com"
    + HOST_END,
    re.IGNORECASE,
)
# A career site Teamtailor serves on the employer's own domain links Teamtailor's
# own site and publishes its feed on the page's host.
TEAMTAILOR_SITE = re.compile(HOST_START + r"www\.teamtailor\.com" + HOST_END, re.IGNORECASE)
# The same site loads its career-site bundle in the head, so it is found when the
# footer link falls past the bytes read for identification.
TEAMTAILOR_ASSETS = re.compile(
    HOST_START + r"[a-z0-9-]+\.teamtailor-cdn\.com/assets/careersite-", re.IGNORECASE
)

TEAMTAILOR_PAGE = 100


def _read(fetcher: BoardTransport, employer: str, groups: dict[str, str], url: str) -> SourceRead:
    """Read every page of Teamtailor's public full-body RSS feed."""
    tenant = groups.get("tenant", "")
    host = f"{tenant}.teamtailor.com" if tenant else urlsplit(url).netloc
    listings: list[SourceListing] = []
    raw_count = 0
    offset = 0
    pages = 0
    while True:
        if fetcher.deadline_elapsed():
            raise BoardDeadlineReachedError(listings, raw_count)
        feed = f"https://{host}/jobs.rss?per_page={TEAMTAILOR_PAGE}&offset={offset}"
        root = required_feed_root(
            fetcher.get_feed(feed), "rss", "Teamtailor", feed, namespace=TEAMTAILOR_NAMESPACE
        )
        items = root.findall("./channel/item")
        pages += bool(items)
        raw_count += len(items)
        listings.extend(board_listings(TEAMTAILOR_MAPPING, items, employer))
        if len(items) < TEAMTAILOR_PAGE:
            return whole(listings, raw_count=raw_count, listing_pages=pages)
        offset += len(items)


PLATFORM = BoardPlatform(
    name="teamtailor",
    board_patterns=(TEAMTAILOR_BOARD, TEAMTAILOR_SITE, TEAMTAILOR_ASSETS),
    adapter=_read,
    job_patterns=(TEAMTAILOR_JOB,),
    source_roots=(
        (TEAMTAILOR_JOB, lambda _url, match: f"https://{match.group('tenant')}.teamtailor.com"),
    ),
    employer_slug=True,
)
