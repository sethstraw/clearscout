"""Pinpoint tenant boards and the complete public postings feed."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from tailorcv.infrastructure.web_fetch.board_mappings import PINPOINT_MAPPING
from tailorcv.infrastructure.web_fetch.board_platform import HOST_END, BoardPlatform
from tailorcv.infrastructure.web_fetch.board_reads import (
    board_listings,
    required_list_field,
    whole,
)

if TYPE_CHECKING:
    from tailorcv.application.discovery.source_reading import SourceRead
    from tailorcv.infrastructure.web_fetch.board_platform import BoardTransport

PINPOINT_JOB = re.compile(
    r"(?P<tenant>[a-z0-9-]+)\.pinpointhq\.com/(?:[a-z]{2}(?:-[A-Z]{2})?/)?"
    r"(?:postings|jobs)/(?P<job>[A-Za-z0-9-]+)",
    re.IGNORECASE,
)
PINPOINT_BOARD = re.compile(
    r"(?P<tenant>[a-z0-9-]+)\.pinpointhq\.com" + HOST_END + r"(?:/(?:[a-z]{2}(?:-[A-Z]{2})?))?",
    re.IGNORECASE,
)


def _read(fetcher: BoardTransport, employer: str, groups: dict[str, str], _url: str) -> SourceRead:
    """Read Pinpoint's complete public postings feed, including every body section."""
    payload = fetcher.get_json(f"https://{groups.get('tenant', '')}.pinpointhq.com/postings.json")
    rows = required_list_field(payload, "data", "Pinpoint")
    return whole(board_listings(PINPOINT_MAPPING, rows, employer), raw_count=len(rows))


PLATFORM = BoardPlatform(
    name="pinpoint",
    board_patterns=(PINPOINT_BOARD,),
    adapter=_read,
    job_patterns=(PINPOINT_JOB,),
    source_roots=(
        (PINPOINT_JOB, lambda _url, match: f"https://{match.group('tenant')}.pinpointhq.com"),
    ),
    employer_slug=True,
)
