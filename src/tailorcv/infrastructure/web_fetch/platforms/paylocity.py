"""Paylocity recruiting boards: the board page's own job list joined to the V2 feed."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import httpx

from tailorcv.application.discovery.source_reading import SourceRead
from tailorcv.domain import SourceReadOutcome
from tailorcv.infrastructure.web_fetch.board_mappings import (
    PAYLOCITY_MAPPING,
    paylocity_board_page_url,
    paylocity_feed_url,
    paylocity_page_jobs,
)
from tailorcv.infrastructure.web_fetch.board_platform import BoardPlatform
from tailorcv.infrastructure.web_fetch.board_reads import (
    MAX_API_RESPONSE_BYTES,
    board_listings,
    page_markup,
    required_list_field,
    whole,
)

if TYPE_CHECKING:
    from tailorcv.infrastructure.web_fetch.board_platform import BoardTransport

PAYLOCITY_JOB = re.compile(
    r"recruiting\.paylocity\.com/recruiting/jobs/(?:Details|Apply)/\d+\b",
    re.IGNORECASE,
)
PAYLOCITY_BOARD = re.compile(
    r"recruiting\.paylocity\.com/recruiting/(?:v2/api/feed/jobs|jobs/All)/"
    r"(?P<tenant>[0-9a-f-]{36})(?:[/?&#\"'\s]|$)",
    re.IGNORECASE,
)


def _read(fetcher: BoardTransport, employer: str, groups: dict[str, str], _url: str) -> SourceRead:
    """Read a Paylocity board page's public openings and its feed's, with feed bodies.

    The V2 feed lists only the jobs a company syndicates, so the read is
    complete only when the board page states its own job list.
    """
    tenant = groups.get("tenant", "")
    page_jobs = _page_jobs(fetcher, tenant)
    try:
        feed_rows = required_list_field(
            fetcher.get_json(paylocity_feed_url(tenant)), "jobs", "Paylocity"
        )
    except (httpx.HTTPStatusError, ValueError):
        # A page that states its job list is the whole board without the feed.
        if page_jobs is None:
            raise
        feed_rows = []
    if page_jobs is None:
        return SourceRead(
            listings=board_listings(PAYLOCITY_MAPPING, feed_rows, employer),
            complete=False,
            raw_count=len(feed_rows),
            outcome=SourceReadOutcome.NO_SUPPORTED_REPRESENTATION,
            note=(
                "The Paylocity board page did not embed its job list, so only the jobs "
                "its feed syndicates were read."
            ),
        )
    feed = {
        str(row["jobId"]): row
        for row in feed_rows
        if isinstance(row, dict) and row.get("jobId") is not None
    }
    entries = [job for job in page_jobs if isinstance(job, dict)]
    public = [job for job in entries if job.get("IsInternal") is not True]
    # The page's Description is an excerpt cut mid-word, never a posting body.
    records = [
        {
            **{key: value for key, value in job.items() if key != "Description"},
            **feed.get(str(job.get("JobId")), {}),
        }
        for job in public
    ]
    on_page = {str(job.get("JobId")) for job in entries}
    records.extend(row for job_id, row in feed.items() if job_id not in on_page)
    read = whole(board_listings(PAYLOCITY_MAPPING, records, employer), raw_count=len(records))
    internal = len(entries) - len(public)
    if not internal:
        return read
    return replace(read, skip_reasons={**read.skip_reasons, "internal_only_posting": internal})


def _page_jobs(fetcher: BoardTransport, tenant: str) -> list[Any] | None:
    """Return the job list a tenant's board page embeds, or None when it states none."""
    try:
        page = fetcher.get_text(paylocity_board_page_url(tenant), max_bytes=MAX_API_RESPONSE_BYTES)
    except httpx.HTTPStatusError:
        return None
    return paylocity_page_jobs(page_markup(page))


PLATFORM = BoardPlatform(
    name="paylocity",
    board_patterns=(PAYLOCITY_BOARD,),
    adapter=_read,
    job_patterns=(PAYLOCITY_JOB,),
)
