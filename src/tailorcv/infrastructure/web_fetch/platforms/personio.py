"""Personio tenant boards on both country domains and the public XML feed."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from tailorcv.infrastructure.web_fetch.board_mappings import PERSONIO_MAPPING
from tailorcv.infrastructure.web_fetch.board_platform import BoardPlatform
from tailorcv.infrastructure.web_fetch.board_reads import (
    board_listings,
    required_feed_root,
    whole,
)

if TYPE_CHECKING:
    from tailorcv.application.discovery.source_reading import SourceRead
    from tailorcv.infrastructure.web_fetch.board_platform import BoardTransport

PERSONIO_JOB = re.compile(
    r"(?P<tenant>[a-z0-9-]+)\.jobs\.personio\.(?:com|de)/job/(?P<job>\d+)\b",
    re.IGNORECASE,
)
PERSONIO_BOARD = re.compile(
    r"(?P<tenant>[a-z0-9-]+)\.jobs\.personio\.(?P<tld>com|de)(?:/|$)",
    re.IGNORECASE,
)


def _root(url: str, match: re.Match[str]) -> str:
    tld = "de" if ".personio.de/" in url.casefold() else "com"
    return f"https://{match.group('tenant')}.jobs.personio.{tld}"


def _read(fetcher: BoardTransport, employer: str, groups: dict[str, str], _url: str) -> SourceRead:
    """Read a Personio tenant's enabled complete public XML feed."""
    tenant, tld = groups.get("tenant", ""), groups.get("tld", "com")
    feed = f"https://{tenant}.jobs.personio.{tld}/xml?language=en"
    root = required_feed_root(fetcher.get_feed(feed), "workzag-jobs", "Personio", feed)
    positions = root.findall("./position")
    listings = board_listings(
        PERSONIO_MAPPING, positions, employer, groups={"tenant": tenant, "tld": tld}
    )
    return whole(listings, raw_count=len(positions))


PLATFORM = BoardPlatform(
    name="personio",
    board_patterns=(PERSONIO_BOARD,),
    adapter=_read,
    job_patterns=(PERSONIO_JOB,),
    source_roots=((PERSONIO_JOB, _root),),
    employer_slug=True,
)
