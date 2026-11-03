"""JazzHR tenant boards: the server-rendered opening list joined to each posting page's markup."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from clearscout.infrastructure.web_fetch.board_mappings import (
    JAZZHR_MAPPING,
    jazzhr_board_url,
    jazzhr_openings,
)
from clearscout.infrastructure.web_fetch.board_mappings.jazzhr import jazzhr_page_posting
from clearscout.infrastructure.web_fetch.board_platform import (
    HOST_END,
    HOST_START,
    BoardPlatform,
    host_match,
)
from clearscout.infrastructure.web_fetch.board_reads import (
    MAX_API_RESPONSE_BYTES,
    job_page_rows,
    job_page_tenant,
    listed_page_continuations,
    listed_page_read,
    page_markup,
    unreadable_continuation,
)

if TYPE_CHECKING:
    from clearscout.application.discovery.source_reading import SourceContinuationTarget, SourceRead
    from clearscout.infrastructure.web_fetch.board_platform import BoardTransport

# JazzHR serves its own app and API from these labels, so neither is a tenant.
_JAZZHR_TENANT = HOST_START + r"(?P<tenant>(?!(?:www|app|api)\.)[a-z0-9-]+)\.applytojob\.com"
JAZZHR_BOARD = re.compile(_JAZZHR_TENANT + HOST_END, re.IGNORECASE)
JAZZHR_JOB = re.compile(
    _JAZZHR_TENANT + r"/apply/(?P<job>[A-Za-z0-9]{10})(?=[/?#\"'\s<]|$)", re.IGNORECASE
)
_PENDING = "jazzhr_detail_rows_pending"


def _read(
    fetcher: BoardTransport,
    employer: str,
    groups: dict[str, str],
    url: str,
    *,
    target: SourceContinuationTarget | None = None,
) -> SourceRead:
    """Read every opening a JazzHR board page links and each posting page's declared body."""
    tenant = groups.get("tenant", "").casefold()
    page = page_markup(fetcher.get_text(jazzhr_board_url(tenant), max_bytes=MAX_API_RESPONSE_BYTES))
    return listed_page_read(
        fetcher,
        employer,
        jazzhr_openings(page, tenant),
        mapping=JAZZHR_MAPPING,
        unread_note="The JazzHR board page linked no openings.",
        source_url=url,
        groups={"tenant": tenant},
        detail=jazzhr_page_posting,
        detail_resume=(jazzhr_board_url(tenant), _PENDING),
        resume_target=target,
    )


def _continue(
    fetcher: BoardTransport, employer: str, target: SourceContinuationTarget
) -> SourceRead:
    """Re-list the board page and resume its unresolved stable opening identities."""
    match = host_match(JAZZHR_BOARD, target.url)
    if match is None:
        return unreadable_continuation(target, "invalid_jazzhr_cursor")
    return _read(fetcher, employer, match.groupdict(default=""), target.url, target=target)


PLATFORM = BoardPlatform(
    name="jazzhr",
    label="JazzHR",
    board_patterns=(JAZZHR_BOARD,),
    adapter=_read,
    job_patterns=(JAZZHR_JOB,),
    source_roots=((JAZZHR_BOARD, lambda _url, match: jazzhr_board_url(match.group("tenant"))),),
    employer_slug=True,
    continuations={
        **listed_page_continuations(
            JAZZHR_MAPPING, job_page_rows(JAZZHR_JOB), groups=job_page_tenant(JAZZHR_JOB)
        ),
        _PENDING: _continue,
    },
)
