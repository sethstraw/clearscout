"""Taleo Business Edition career sections and the public RSS feed listing each one's postings."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode

from tailorcv.infrastructure.web_fetch.board_mappings import TALEOBUSINESS_MAPPING
from tailorcv.infrastructure.web_fetch.board_mappings.taleobusiness import (
    TALEOBUSINESS_NAMESPACE,
)
from tailorcv.infrastructure.web_fetch.board_platform import (
    HOST_START,
    BoardPlatform,
    board_query,
    match_groups,
)
from tailorcv.infrastructure.web_fetch.board_reads import (
    board_listings,
    required_feed_root,
    whole,
)

if TYPE_CHECKING:
    from tailorcv.application.discovery.source_reading import SourceRead
    from tailorcv.infrastructure.web_fetch.board_platform import BoardTransport

# A section lives under its data centre's path on that host and is named by the
# `org` and `cws` its query states, in either order.
TALEOBUSINESS_BOARD = re.compile(
    HOST_START
    + r"(?P<host>[a-z0-9-]+\.tbe\.taleo\.net)/(?P<dc>[a-z0-9]+)/ats/"
    + r"(?:careers/v2/jobSearch|careers/searchResults\.jsp|servlet/Rss)(?=\?)",
    re.IGNORECASE,
)


def _section(match: re.Match[str]) -> tuple[str, str]:
    """Return the org and career section a board address's query names."""
    query = dict(parse_qsl(board_query(match)))
    return query.get("org", ""), query.get("cws", "")


def _adapter_groups(match: re.Match[str]) -> dict[str, str]:
    """Return a board match's groups plus the org and section its query names."""
    org, cws = _section(match)
    return {**match_groups(match), "org": org, "cws": cws}


def _read(fetcher: BoardTransport, employer: str, groups: dict[str, str], _url: str) -> SourceRead:
    """Read every posting the section's feed lists, each with its HTML body."""
    org, cws = groups.get("org", ""), groups.get("cws", "")
    if not org or not cws:
        raise ValueError("Taleo Business Edition address did not name its org and section")
    feed = f"https://{groups['host']}/{groups['dc']}/ats/servlet/Rss?" + urlencode(
        {"org": org, "cws": cws}
    )
    root = required_feed_root(
        fetcher.get_feed(feed),
        "rss",
        "Taleo Business Edition",
        feed,
        namespace=TALEOBUSINESS_NAMESPACE,
    )
    items = root.findall("./channel/item")
    return whole(board_listings(TALEOBUSINESS_MAPPING, items, employer), raw_count=len(items))


def _identity(match: re.Match[str]) -> str:
    return "/".join((match.group("host"), *_section(match))).casefold()


PLATFORM = BoardPlatform(
    name="taleobusiness",
    label="Taleo Business Edition",
    board_patterns=(TALEOBUSINESS_BOARD,),
    adapter=_read,
    identity_rules=((TALEOBUSINESS_BOARD, _identity),),
    adapter_groups=_adapter_groups,
)
