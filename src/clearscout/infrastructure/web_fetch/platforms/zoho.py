"""Zoho Recruit career portals, read from the opening list their careers page embeds."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from clearscout.infrastructure.web_fetch.board_mappings import (
    ZOHO_MAPPING,
    zoho_board_url,
    zoho_openings,
)
from clearscout.infrastructure.web_fetch.board_platform import HOST_END, HOST_START, BoardPlatform
from clearscout.infrastructure.web_fetch.board_reads import (
    MAX_API_RESPONSE_BYTES,
    board_listings,
    page_markup,
    short_of_total,
    whole,
)

if TYPE_CHECKING:
    from clearscout.application.discovery.source_reading import SourceRead
    from clearscout.infrastructure.web_fetch.board_platform import BoardTransport

_ZOHO_HOST = (
    HOST_START
    + r"(?P<host>(?P<tenant>(?!www\.)[a-z0-9-]+)\.zohorecruit\.(?:com\.au|com|eu|in|jp|ca))"
)
# A portal is the segment after /jobs/; an address naming only the host reads the default one.
ZOHO_BOARD = re.compile(
    _ZOHO_HOST + r"(?:/jobs/(?P<portal>[A-Za-z0-9_-]+))?" + HOST_END, re.IGNORECASE
)
ZOHO_JOB = re.compile(
    _ZOHO_HOST + r"/jobs/(?P<portal>[A-Za-z0-9_-]+)/(?P<job>\d{6,})(?=[/?#\"'\s<]|$)",
    re.IGNORECASE,
)
_DEFAULT_PORTAL = "Careers"


def _root(_url: str, match: re.Match[str]) -> str:
    return zoho_board_url(match.group("host").casefold(), match.group("portal") or _DEFAULT_PORTAL)


def _read(fetcher: BoardTransport, employer: str, groups: dict[str, str], _url: str) -> SourceRead:
    """Read every published opening the portal's careers page embeds.

    The list states title, place and schedule; a posting page declares no
    JobPosting, so bodies are left to a later posting read.
    """
    host = groups.get("host", "").casefold()
    portal = groups.get("portal") or _DEFAULT_PORTAL
    page = page_markup(
        fetcher.get_text(zoho_board_url(host, portal), max_bytes=MAX_API_RESPONSE_BYTES)
    )
    openings = zoho_openings(page)
    if openings is None:
        return short_of_total(whole([]), "The Zoho Recruit careers page embedded no opening list.")
    listings = board_listings(ZOHO_MAPPING, openings, employer, {"host": host, "portal": portal})
    return whole(listings, raw_count=len(openings))


PLATFORM = BoardPlatform(
    name="zoho",
    label="Zoho Recruit",
    board_patterns=(ZOHO_BOARD,),
    adapter=_read,
    job_patterns=(ZOHO_JOB,),
    source_roots=((ZOHO_BOARD, _root),),
    employer_slug=True,
)
