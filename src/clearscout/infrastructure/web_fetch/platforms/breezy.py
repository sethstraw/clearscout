"""Breezy HR tenant boards: the position feed joined to each posting page's declared body."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from clearscout.infrastructure.web_fetch.board_mappings import (
    BREEZY_MAPPING,
    breezy_page_posting,
    breezy_page_url,
    breezy_positions_url,
)
from clearscout.infrastructure.web_fetch.board_platform import (
    HOST_END,
    HOST_START,
    BoardPlatform,
    host_match,
)
from clearscout.infrastructure.web_fetch.board_reads import (
    MAX_API_RESPONSE_BYTES,
    distinct_by_id,
    page_markup,
    row_id,
    unreadable_continuation,
)

if TYPE_CHECKING:
    from clearscout.application.discovery.source_reading import SourceContinuationTarget, SourceRead
    from clearscout.infrastructure.web_fetch.board_platform import BoardTransport

# Breezy serves its own app, API and assets from these labels, so none is a tenant.
_BREEZY_TENANT = HOST_START + r"(?P<tenant>(?!(?:www|app|api|assets)\.)[a-z0-9-]+)\.breezy\.hr"
BREEZY_JOB = re.compile(_BREEZY_TENANT + r"/p/(?P<job>[a-z0-9]+)(?![a-z0-9_])", re.IGNORECASE)
BREEZY_BOARD = re.compile(_BREEZY_TENANT + HOST_END, re.IGNORECASE)
_PENDING = "breezy_detail_rows_pending"


def _read(
    fetcher: BoardTransport,
    employer: str,
    groups: dict[str, str],
    _url: str,
    *,
    target: SourceContinuationTarget | None = None,
) -> SourceRead:
    """Read a Breezy tenant's complete position feed and each posting page's declared body."""
    tenant = groups.get("tenant", "")
    rows = fetcher.get_json(breezy_positions_url(tenant))
    if not isinstance(rows, list):
        raise ValueError("Breezy feed did not return a list of positions")
    distinct = distinct_by_id(rows)
    pages = {row_id(row): breezy_page_url(tenant, row) for row in distinct}
    return fetcher.detailed_read(
        employer,
        distinct,
        mapping=BREEZY_MAPPING,
        detail=lambda job_id: breezy_page_posting(
            page_markup(fetcher.get_text(pages[job_id], max_bytes=MAX_API_RESPONSE_BYTES))
        ),
        groups={"tenant": tenant},
        missing_fails=True,
        resume=(f"https://{tenant}.breezy.hr", _PENDING),
        resume_target=target,
    )


def _continue(
    fetcher: BoardTransport, employer: str, target: SourceContinuationTarget
) -> SourceRead:
    """Re-list the position feed and resume its unresolved stable detail identities."""
    match = host_match(BREEZY_BOARD, target.url)
    if match is None:
        return unreadable_continuation(target, "invalid_breezy_cursor")
    return _read(fetcher, employer, match.groupdict(default=""), target.url, target=target)


PLATFORM = BoardPlatform(
    name="breezy",
    label="Breezy HR",
    board_patterns=(BREEZY_BOARD,),
    adapter=_read,
    job_patterns=(BREEZY_JOB,),
    source_roots=((BREEZY_JOB, lambda _url, match: f"https://{match.group('tenant')}.breezy.hr"),),
    employer_slug=True,
    continuations={_PENDING: _continue},
)
