"""Recognize a job board platform and read its openings through platform feeds and APIs.

When a career URL names no supported platform, the page is fetched once and up
to ``DETECT_BYTES`` of its markup is searched for a board identifier. A page
that names none is read itself: declared posting markup first, then visible
collections or sitemap discovery. Some platforms serve the career site from the
employer's own domain and are named by that markup, so an adapter is told the
address the page came from.

A recognized board is read by its platform's adapter from the registry in
``platforms``; this fetcher owns the transport seams, deadline, and paging
guards those adapters share. Aggregator reads are bounded and never complete,
so nothing they omit is treated as withdrawn.
"""

from clearscout.infrastructure.web_fetch.board_adapters.detail_reads import (
    deadline_cut_note,
    restate_deadline_cut_note,
)
from clearscout.infrastructure.web_fetch.board_adapters.fetcher import (
    SITEMAP_POSTING_FETCHES,
    BoardApiFetcher,
)
from clearscout.infrastructure.web_fetch.board_adapters.identify import (
    BOARD_ADAPTER_REGISTRY,
    STRUCTURED_PAGE_CURSOR_REASONS,
    BoardResolution,
    BoardResolver,
    identify_board,
    identify_board_url,
)
from clearscout.infrastructure.web_fetch.board_adapters.transport import (
    DETECT_BYTES,
)

__all__ = [
    "BOARD_ADAPTER_REGISTRY",
    "DETECT_BYTES",
    "SITEMAP_POSTING_FETCHES",
    "STRUCTURED_PAGE_CURSOR_REASONS",
    "BoardApiFetcher",
    "BoardResolution",
    "BoardResolver",
    "deadline_cut_note",
    "identify_board",
    "identify_board_url",
    "restate_deadline_cut_note",
]
