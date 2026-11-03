"""Dover careers pages: the published job list joined to each job's detail."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import TYPE_CHECKING

from clearscout.infrastructure.web_fetch.board_mappings import (
    DOVER_MAPPING,
    dover_detail_url,
    dover_job,
    dover_jobs_url,
    dover_listed,
)
from clearscout.infrastructure.web_fetch.board_platform import HOST_START, BoardPlatform, host_match
from clearscout.infrastructure.web_fetch.board_reads import (
    required_list_field,
    short_of_total,
    unreadable_continuation,
)

if TYPE_CHECKING:
    from clearscout.application.discovery.source_reading import SourceContinuationTarget, SourceRead
    from clearscout.infrastructure.web_fetch.board_platform import BoardTransport

# The company segment is display text; the job uuid alone names the posting.
DOVER_JOB = re.compile(
    HOST_START
    + r"app\.dover\.(?:io|com)/apply/(?P<company>[^/?#\s\"']+)/(?P<job>[0-9a-f-]{36})(?![\w-])",
    re.IGNORECASE,
)
# Employer sites embed this careers page, often with ?embed=1.
DOVER_BOARD = re.compile(
    HOST_START + r"app\.dover\.(?:io|com)/dover/careers/(?P<tenant>[0-9a-f-]{36})(?![\w-])",
    re.IGNORECASE,
)
_PENDING = "dover_detail_rows_pending"


def _read(
    fetcher: BoardTransport,
    employer: str,
    groups: dict[str, str],
    _url: str,
    *,
    target: SourceContinuationTarget | None = None,
) -> SourceRead:
    """Read a Dover careers page's published jobs and each one's body from its detail.

    The page states a ``count``, so a page answering with fewer rows is not the whole board.
    """
    payload = fetcher.get_json(dover_jobs_url(groups.get("tenant", "")))
    rows = required_list_field(payload, "results", "Dover")
    listed = [row for row in rows if not isinstance(row, dict) or dover_listed(row)]
    count = payload.get("count")
    enumeration_complete = not isinstance(count, int) or count <= len(rows)
    read = fetcher.detailed_read(
        employer,
        listed,
        mapping=DOVER_MAPPING,
        detail=lambda job_id: dover_job(fetcher.get_json(dover_detail_url(job_id))),
        missing_fails=True,
        resume=(f"https://app.dover.io/dover/careers/{groups.get('tenant', '')}", _PENDING),
        resume_target=target,
        enumeration_complete=enumeration_complete,
    )
    if unlisted := len(rows) - len(listed):
        read = replace(
            read,
            skip_reasons={**read.skip_reasons, "sample_or_unpublished_posting": unlisted},
        )
    if enumeration_complete:
        return read
    return short_of_total(
        read, f"The Dover careers page states {count} jobs and returned {len(rows)}."
    )


def _continue(
    fetcher: BoardTransport, employer: str, target: SourceContinuationTarget
) -> SourceRead:
    """Re-list the careers page and resume its unresolved stable detail identities."""
    match = host_match(DOVER_BOARD, target.url)
    if match is None:
        return unreadable_continuation(target, "invalid_dover_cursor")
    return _read(fetcher, employer, match.groupdict(default=""), target.url, target=target)


PLATFORM = BoardPlatform(
    name="dover",
    board_patterns=(DOVER_BOARD,),
    adapter=_read,
    job_patterns=(DOVER_JOB,),
    continuations={_PENDING: _continue},
)
