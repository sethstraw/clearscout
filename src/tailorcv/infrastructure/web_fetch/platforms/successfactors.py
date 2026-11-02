"""SAP SuccessFactors career sites, named by their CDN and read from the host's sitemap."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from tailorcv.infrastructure.web_fetch.board_mappings import (
    SUCCESSFACTORS_MAPPING,
    successfactors_brand,
    successfactors_openings,
)
from tailorcv.infrastructure.web_fetch.board_mappings.successfactors import (
    successfactors_page_posting,
    successfactors_withdrawn,
)
from tailorcv.infrastructure.web_fetch.board_platform import BoardPlatform
from tailorcv.infrastructure.web_fetch.board_reads import (
    listed_page_continuations,
    listed_page_read,
    sitemap_locations,
)

if TYPE_CHECKING:
    from tailorcv.application.discovery.source_reading import SourceContinuationTarget, SourceRead
    from tailorcv.infrastructure.web_fetch.board_platform import BoardTransport

# A career site serves from the employer's own host and loads its assets from this CDN.
SUCCESSFACTORS_BOARD = re.compile(r"rmkcdn\.successfactors\.com/")


def _read(fetcher: BoardTransport, employer: str, _groups: dict[str, str], url: str) -> SourceRead:
    """Read the posting pages the host's sitemap lists under the address's brand.

    A site serving several brands lists every brand's postings in one sitemap,
    so the brand the address names keeps the read to its own board.
    """
    host = urlsplit(url).netloc
    pages, unread, gone = sitemap_locations(
        fetcher, f"https://{host}/sitemap.xml", "SuccessFactors"
    )
    return listed_page_read(
        fetcher,
        employer,
        successfactors_openings(pages, host, successfactors_brand(url)),
        mapping=SUCCESSFACTORS_MAPPING,
        unread_note="The SuccessFactors sitemap listed no posting for this board.",
        source_url=url,
        pending_sitemaps=unread,
        sitemap_listed=True,
        gone_sitemaps=gone,
        detail=successfactors_page_posting,
        withdrawn=successfactors_withdrawn,
        skip_recent=True,
    )


def _brand_openings(pages: list[str], target: SourceContinuationTarget) -> list[dict[str, str]]:
    """Return the posting rows among ``pages`` for the brand a continuation's site address names."""
    site = target.source_url
    return successfactors_openings(pages, urlsplit(site).netloc, successfactors_brand(site))


PLATFORM = BoardPlatform(
    name="successfactors",
    label="SAP SuccessFactors",
    board_patterns=(SUCCESSFACTORS_BOARD,),
    adapter=_read,
    single_tenant=False,
    continuations=listed_page_continuations(
        SUCCESSFACTORS_MAPPING,
        _brand_openings,
        sitemaps=True,
        detail=successfactors_page_posting,
        withdrawn=successfactors_withdrawn,
    ),
)
