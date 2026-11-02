"""Workable boards and the account widget API with details."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from tailorcv.infrastructure.web_fetch.board_mappings import WORKABLE_MAPPING
from tailorcv.infrastructure.web_fetch.board_platform import BoardPlatform
from tailorcv.infrastructure.web_fetch.board_reads import board_listings, whole

if TYPE_CHECKING:
    from tailorcv.application.discovery.source_reading import SourceRead
    from tailorcv.infrastructure.web_fetch.board_platform import BoardTransport

# A Workable job detail permalink, excluding the short /j/<id> link and the
# /api/... addresses WORKABLE_BOARD also recognizes as boards.
WORKABLE_JOB = re.compile(r"apply\.workable\.com/(?!j/|api/)(?P<org>[a-z0-9-]+)/j/[A-Za-z0-9]+")
WORKABLE_BOARD = re.compile(
    r"apply\.workable\.com/(?:api/v\d+/widget/accounts/)?(?!j/|api/)(?P<org>[a-z0-9-]+)"
)
# An embedding page names only the numeric account, which the widget API also reads.
WORKABLE_EMBED = re.compile(
    r"(?:\bwhr_embed\(\s*|(?<![\w.-])(?:www\.)?workable\.com/api/accounts/)(?P<org>\d+)\b"
)


def _read(fetcher: BoardTransport, employer: str, groups: dict[str, str], _url: str) -> SourceRead:
    org = groups.get("org", "")
    payload = fetcher.get_json(
        f"https://apply.workable.com/api/v1/widget/accounts/{org}?details=true"
    )
    jobs = payload.get("jobs", [])
    return whole(board_listings(WORKABLE_MAPPING, jobs, employer), raw_count=len(jobs))


PLATFORM = BoardPlatform(
    name="workable",
    board_patterns=(WORKABLE_BOARD, WORKABLE_EMBED),
    adapter=_read,
    job_patterns=(WORKABLE_JOB,),
    source_roots=(
        (WORKABLE_JOB, lambda _url, match: f"https://apply.workable.com/{match.group('org')}"),
    ),
    identity_rules=((WORKABLE_BOARD, lambda match: match.group("org").casefold()),),
    employer_slug=True,
)
