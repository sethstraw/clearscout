"""TriNet Hire company boards: the rendered job table joined to each posting page's body."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from clearscout.infrastructure.web_fetch.board_mappings import (
    TRINETHIRE_MAPPING,
    trinethire_board_url,
    trinethire_openings,
    trinethire_page_posting,
    trinethire_paged,
)
from clearscout.infrastructure.web_fetch.board_platform import HOST_START, BoardPlatform, host_match
from clearscout.infrastructure.web_fetch.board_reads import (
    MAX_API_RESPONSE_BYTES,
    job_page_rows,
    listed_page_continuations,
    listed_page_read,
    page_markup,
    short_of_total,
    whole,
)

if TYPE_CHECKING:
    from clearscout.application.discovery.source_reading import SourceContinuationTarget, SourceRead
    from clearscout.infrastructure.web_fetch.board_platform import BoardTransport

# A company path is its numeric account id, a hyphen, and its slug.
_COMPANY = HOST_START + r"app\.trinethire\.com/companies/(?P<company>\d+)-(?P<org>[a-z0-9-]+)/jobs"
TRINETHIRE_JOB = re.compile(_COMPANY + r"/(?P<job>\d+)(?![\w])", re.IGNORECASE)
# An employer page frames the same table at /jobs/embed.
TRINETHIRE_BOARD = re.compile(_COMPANY + r"(?:/embed)?(?![\w-])", re.IGNORECASE)


def _company_path(groups: dict[str, str]) -> str:
    return f"{groups.get('company', '')}-{groups.get('org', '')}".casefold()


def _posting_groups(target: SourceContinuationTarget) -> dict[str, str]:
    match = host_match(TRINETHIRE_JOB, target.url)
    return {"org": _company_path(match.groupdict())} if match is not None else {}


def _read(fetcher: BoardTransport, employer: str, groups: dict[str, str], url: str) -> SourceRead:
    """Read every opening the company's job table lists and each posting page's body."""
    company = _company_path(groups)
    board_url = trinethire_board_url(company)
    page = page_markup(fetcher.get_text(board_url, max_bytes=MAX_API_RESPONSE_BYTES))
    parsed = trinethire_openings(page, groups.get("company", ""))
    if parsed is None:
        return short_of_total(whole([]), "The TriNet Hire board page rendered no job table.")
    rows, unread = parsed
    paged = trinethire_paged(page, groups.get("company", ""))
    if not rows and not unread and not paged:
        return whole([])
    read = listed_page_read(
        fetcher,
        employer,
        rows,
        mapping=TRINETHIRE_MAPPING,
        unread_note="The TriNet Hire job table linked no opening of this company.",
        source_url=url,
        groups={"org": company},
        detail=trinethire_page_posting,
        skip_recent=True,
    )
    if unread:
        read = short_of_total(
            read, f"{unread} TriNet Hire job rows linked no opening of this company."
        )
    if paged:
        read = short_of_total(read, "The TriNet Hire board page shows paging past this table.")
    return read


PLATFORM = BoardPlatform(
    name="trinethire",
    label="TriNet Hire",
    board_patterns=(TRINETHIRE_BOARD,),
    adapter=_read,
    job_patterns=(TRINETHIRE_JOB,),
    source_roots=(
        (
            TRINETHIRE_JOB,
            lambda _url, match: trinethire_board_url(_company_path(match.groupdict())),
        ),
        (
            TRINETHIRE_BOARD,
            lambda _url, match: trinethire_board_url(_company_path(match.groupdict())),
        ),
    ),
    identity_rules=((TRINETHIRE_BOARD, lambda match: match.group("company")),),
    continuations=listed_page_continuations(
        TRINETHIRE_MAPPING,
        job_page_rows(TRINETHIRE_JOB),
        detail=trinethire_page_posting,
        groups=_posting_groups,
    ),
)
