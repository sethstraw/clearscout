"""Jobvite tenant boards: the server-rendered opening table joined to each posting page's markup."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import httpx

from clearscout.infrastructure.web_fetch.board_mappings import (
    JOBVITE_MAPPING,
    jobvite_board_url,
    jobvite_linked_pages,
    jobvite_more_pages,
    jobvite_openings,
)
from clearscout.infrastructure.web_fetch.board_platform import HOST_START, BoardPlatform
from clearscout.infrastructure.web_fetch.board_reads import (
    MAX_API_RESPONSE_BYTES,
    job_page_rows,
    job_page_tenant,
    listed_page_continuations,
    listed_page_read,
    page_markup,
    short_of_total,
)
from clearscout.infrastructure.web_fetch.policy import FetchPolicyError

if TYPE_CHECKING:
    from clearscout.application.discovery.source_reading import SourceRead
    from clearscout.infrastructure.web_fetch.board_platform import BoardTransport

# The tenant is the first path segment; a legacy address puts "careers/" before it.
_JOBVITE_TENANT = (
    HOST_START
    + r"jobs\.jobvite\.com/(?:careers/)?"
    + r"(?P<tenant>(?!_|(?:careers|static|assets|api)(?![\w-]))[A-Za-z0-9_-]+)"
)
JOBVITE_BOARD = re.compile(_JOBVITE_TENANT + r"(?=[/?#\"'\s<]|$)", re.IGNORECASE)
JOBVITE_JOB = re.compile(
    _JOBVITE_TENANT + r"/job/(?P<job>[A-Za-z0-9]+)(?=[/?#\"'\s<]|$)", re.IGNORECASE
)


JOBVITE_MAX_PAGES = 20


def _pages(fetcher: BoardTransport, tenant: str) -> tuple[list[dict[str, str]], list[str]]:
    """Follow ``?p=N`` while each page links the next; return its rows and what cut it short."""
    rows: list[dict[str, str]] = []
    problems: list[str] = []
    for number in range(JOBVITE_MAX_PAGES):
        if number and fetcher.deadline_elapsed():
            problems.append(f"The Jobvite board's list page {number} was not read in time.")
            break
        address = jobvite_board_url(tenant, number)
        try:
            page = page_markup(fetcher.get_text(address, max_bytes=MAX_API_RESPONSE_BYTES))
        except (FetchPolicyError, httpx.HTTPError):
            # The first page failing fails the read; a later one only shortens it.
            if not number:
                raise
            problems.append(f"The Jobvite board's list page {number} could not be read.")
            break
        seen = {row["id"] for row in rows}
        found = jobvite_openings(page, tenant)
        rows.extend(found)
        if jobvite_more_pages(page):
            problems.append(
                "The Jobvite board links further pages of openings this read does not read."
            )
        if number and not any(row["id"] not in seen for row in found):
            problems.append(f"The Jobvite board's list page {number} listed no new openings.")
            break
        if number + 1 not in jobvite_linked_pages(page):
            break
    else:
        problems.append(f"The Jobvite board lists more than {JOBVITE_MAX_PAGES} pages.")
    return rows, list(dict.fromkeys(problems))


def _read(fetcher: BoardTransport, employer: str, groups: dict[str, str], url: str) -> SourceRead:
    """Read every opening the paged Jobvite table lists and each posting page's declared body."""
    tenant = groups.get("tenant", "").casefold()
    rows, problems = _pages(fetcher, tenant)
    read = listed_page_read(
        fetcher,
        employer,
        rows,
        mapping=JOBVITE_MAPPING,
        unread_note="The Jobvite board page listed no openings.",
        source_url=url,
        groups={"tenant": tenant},
        skip_recent=True,
    )
    return short_of_total(read, " ".join(problems)) if problems else read


PLATFORM = BoardPlatform(
    name="jobvite",
    board_patterns=(JOBVITE_BOARD,),
    adapter=_read,
    job_patterns=(JOBVITE_JOB,),
    source_roots=(
        (JOBVITE_BOARD, lambda _url, match: jobvite_board_url(match.group("tenant").casefold())),
    ),
    employer_slug=True,
    continuations=listed_page_continuations(
        JOBVITE_MAPPING, job_page_rows(JOBVITE_JOB), groups=job_page_tenant(JOBVITE_JOB)
    ),
)
