"""BambooHR career sites: the active-openings list joined to each opening's detail."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from clearscout.infrastructure.web_fetch.board_mappings import (
    BAMBOOHR_MAPPING,
    bamboohr_detail_url,
    bamboohr_opening,
    bamboohr_withdrawn,
)
from clearscout.infrastructure.web_fetch.board_platform import BoardPlatform, host_match
from clearscout.infrastructure.web_fetch.board_reads import (
    required_list_field,
    unreadable_continuation,
)

if TYPE_CHECKING:
    from clearscout.application.discovery.source_reading import SourceContinuationTarget, SourceRead
    from clearscout.infrastructure.web_fetch.board_platform import BoardTransport

BAMBOOHR_JOB = re.compile(
    r"(?P<tenant>[a-z0-9-]+)\.bamboohr\.com/careers/(?P<job>\d+)\b",
    re.IGNORECASE,
)
BAMBOOHR_BOARD = re.compile(r"(?P<tenant>[a-z0-9-]+)\.bamboohr\.com/careers\b", re.IGNORECASE)
_PENDING = "bamboohr_detail_rows_pending"


def _read(
    fetcher: BoardTransport,
    employer: str,
    groups: dict[str, str],
    _url: str,
    *,
    target: SourceContinuationTarget | None = None,
) -> SourceRead:
    """Read every active BambooHR opening and its complete detail body."""
    tenant = groups.get("tenant", "")
    payload = fetcher.get_json(f"https://{tenant}.bamboohr.com/careers/list")
    rows = required_list_field(payload, "result", "BambooHR")
    return fetcher.detailed_read(
        employer,
        rows,
        mapping=BAMBOOHR_MAPPING,
        detail=lambda job_id: bamboohr_opening(
            fetcher.get_json(bamboohr_detail_url(tenant, job_id))
        ),
        groups={"tenant": tenant},
        record_key="opening",
        withdrawn=bamboohr_withdrawn,
        missing_fails=True,
        resume=(f"https://{tenant}.bamboohr.com/careers", _PENDING),
        resume_target=target,
    )


def _continue(
    fetcher: BoardTransport, employer: str, target: SourceContinuationTarget
) -> SourceRead:
    """Re-list the career site and resume its unresolved stable opening identities."""
    match = host_match(BAMBOOHR_BOARD, target.url)
    if match is None:
        return unreadable_continuation(target, "invalid_bamboohr_cursor")
    return _read(fetcher, employer, match.groupdict(default=""), target.url, target=target)


PLATFORM = BoardPlatform(
    name="bamboohr",
    label="BambooHR",
    board_patterns=(BAMBOOHR_BOARD,),
    adapter=_read,
    job_patterns=(BAMBOOHR_JOB,),
    source_roots=(
        (BAMBOOHR_JOB, lambda _url, match: f"https://{match.group('tenant')}.bamboohr.com/careers"),
    ),
    employer_slug=True,
    posting_api=True,
    continuations={_PENDING: _continue},
)
