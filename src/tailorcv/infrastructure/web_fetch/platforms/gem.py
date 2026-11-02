"""Gem job boards and the public post list, whose rows carry each post's whole body."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from tailorcv.infrastructure.web_fetch.board_mappings import GEM_MAPPING, gem_post_url
from tailorcv.infrastructure.web_fetch.board_platform import BoardPlatform, hosted_org
from tailorcv.infrastructure.web_fetch.board_reads import board_listings, whole

if TYPE_CHECKING:
    from tailorcv.application.discovery.source_reading import SourceRead
    from tailorcv.infrastructure.web_fetch.board_platform import BoardTransport

# A Gem post id is an opaque token, sometimes a uuid and sometimes URL-safe base64.
_GEM_ORG = hosted_org(r"[A-Za-z0-9_-]")
GEM_JOB = re.compile(r"jobs\.gem\.com/" + _GEM_ORG + r"/(?P<job>[A-Za-z0-9_-]+)(?=[/?#]|$)")
GEM_BOARD = re.compile(r"jobs\.gem\.com/" + _GEM_ORG)


def _read(fetcher: BoardTransport, employer: str, groups: dict[str, str], _url: str) -> SourceRead:
    """Read Gem's public board list, whose rows carry each post's whole body."""
    payload = fetcher.get_json(gem_post_url(groups.get("org", "")))
    if not isinstance(payload, list):
        raise ValueError("Gem board did not return a list of job posts")
    return whole(board_listings(GEM_MAPPING, payload, employer), raw_count=len(payload))


PLATFORM = BoardPlatform(
    name="gem",
    board_patterns=(GEM_BOARD,),
    adapter=_read,
    job_patterns=(GEM_JOB,),
    source_roots=((GEM_JOB, lambda _url, match: f"https://jobs.gem.com/{match.group('org')}"),),
    employer_slug=True,
    posting_api=True,
)
