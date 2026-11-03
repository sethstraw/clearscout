"""Recruitee tenant boards and the public offers API."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from clearscout.infrastructure.web_fetch.board_mappings import RECRUITEE_MAPPING
from clearscout.infrastructure.web_fetch.board_platform import HOST_END, BoardPlatform
from clearscout.infrastructure.web_fetch.board_reads import board_listings, whole

if TYPE_CHECKING:
    from clearscout.application.discovery.source_reading import SourceRead
    from clearscout.infrastructure.web_fetch.board_platform import BoardTransport

RECRUITEE_JOB = re.compile(
    r"(?P<org>[a-z0-9-]+)\.recruitee\.com/(?:l/[a-z]{2}(?:-[a-z]{2})?/)?o/[A-Za-z0-9-]+",
    re.IGNORECASE,
)
RECRUITEE_BOARD = re.compile(r"(?P<org>[a-z0-9-]+)\.recruitee\.com" + HOST_END)


def _read(fetcher: BoardTransport, employer: str, groups: dict[str, str], _url: str) -> SourceRead:
    payload = fetcher.get_json(f"https://{groups.get('org', '')}.recruitee.com/api/offers/")
    jobs = payload.get("offers", [])
    return whole(board_listings(RECRUITEE_MAPPING, jobs, employer), raw_count=len(jobs))


PLATFORM = BoardPlatform(
    name="recruitee",
    board_patterns=(RECRUITEE_BOARD,),
    adapter=_read,
    job_patterns=(RECRUITEE_JOB,),
    source_roots=(
        (RECRUITEE_JOB, lambda _url, match: f"https://{match.group('org')}.recruitee.com"),
    ),
    identity_rules=((RECRUITEE_BOARD, lambda match: match.group("org").casefold()),),
    employer_slug=True,
)
