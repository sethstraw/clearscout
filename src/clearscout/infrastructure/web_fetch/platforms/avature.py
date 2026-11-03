"""Avature career portals, read from the JobDetail pages their published sitemap lists."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from clearscout.infrastructure.web_fetch.board_mappings import (
    AVATURE_MAPPING,
    avature_openings,
    avature_page_posting,
    avature_portal,
    avature_sitemap_url,
    avature_sitemaps,
)
from clearscout.infrastructure.web_fetch.board_platform import HOST_START, BoardPlatform
from clearscout.infrastructure.web_fetch.board_reads import (
    listed_page_continuations,
    listed_page_read,
    sitemap_locations,
)

if TYPE_CHECKING:
    from clearscout.application.discovery.source_reading import SourceContinuationTarget, SourceRead
    from clearscout.infrastructure.web_fetch.board_platform import BoardTransport

_AVATURE_PORTAL = (
    HOST_START
    + r"(?P<host>(?P<tenant>(?!www\.)[a-z0-9-]+)\.avature\.net)"
    + r"/(?P<portal>(?:[a-z]{2}_[A-Z]{2}/)?[A-Za-z][A-Za-z0-9_-]*)"
)
# A portal address ends at the portal or names one of its pages; other paths are site assets.
AVATURE_BOARD = re.compile(
    _AVATURE_PORTAL
    + r"(?:/?(?=[?#\"'\s<]|$)"
    + r"|/(?:SearchJobs|JobDetail|FolderDetail|Login|Register|SavedJobs|ApplicationMethods)"
    + r"(?=[/?#\"'\s<]|$))",
    re.IGNORECASE,
)
# A portal served from the employer's own domain still declares its portal path in a meta tag.
AVATURE_PAGE = re.compile(
    r"""<meta\s+name=["']avature\.portal\.urlPath["']\s+content=["']"""
    r"""(?P<url_path>[A-Za-z][A-Za-z0-9_-]*)["']"""
)
AVATURE_JOB = re.compile(
    _AVATURE_PORTAL + r"/JobDetail/[^/?#\"'\s<]+/(?P<job>\d+)(?=[/?#\"'\s<]|$)", re.IGNORECASE
)


def _portal_host(host: str, portal: str, pages: list[str], unread: tuple[str, ...]) -> str:
    """Return the host the portal's postings live on, which an avature.net index may move."""
    if avature_openings(pages, host, portal) or not host.endswith(".avature.net"):
        return host
    named = {
        urlsplit(address).netloc.casefold()
        for address in (*pages, *unread)
        if "/JobDetail/" in address or address.endswith(".xml")
    }
    return named.pop() if len(named) == 1 else host


def _read(fetcher: BoardTransport, employer: str, groups: dict[str, str], url: str) -> SourceRead:
    """Read every JobDetail page the portal's sitemap lists and what each page renders."""
    if groups.get("host"):
        host, portal = groups["host"].casefold(), groups.get("portal", "")
    else:
        found = avature_portal(url, groups.get("url_path", ""))
        if found is None:
            raise ValueError("Avature page names no portal")
        host, portal = found
    pages, unread, gone = sitemap_locations(
        fetcher,
        avature_sitemap_url(host, portal),
        "Avature",
        children=lambda children: avature_sitemaps(children, portal),
    )
    moved = _portal_host(host, portal, pages, unread)
    source_url = f"https://{moved}/{portal}"
    # A portal on its own domain is saved at the page it was found on, so continuations keep it.
    if not groups.get("host") and avature_portal(url) == (host, portal):
        source_url = url
    return listed_page_read(
        fetcher,
        employer,
        avature_openings(pages, moved, portal),
        mapping=AVATURE_MAPPING,
        unread_note="The Avature sitemap listed no JobDetail page.",
        source_url=source_url,
        detail=avature_page_posting,
        pending_sitemaps=unread,
        sitemap_listed=True,
        gone_sitemaps=gone,
        skip_recent=True,
    )


def _portal_openings(
    pages: list[str], target: SourceContinuationTarget
) -> list[dict[str, str]] | None:
    """Return the JobDetail rows among ``pages`` for the portal a continuation came from."""
    portal = avature_portal(target.source_url)
    if portal is None:
        return None
    return avature_openings(pages, *portal)


PLATFORM = BoardPlatform(
    name="avature",
    board_patterns=(AVATURE_BOARD, AVATURE_PAGE),
    adapter=_read,
    job_patterns=(AVATURE_JOB,),
    source_roots=(
        (
            AVATURE_BOARD,
            lambda _url, match: f"https://{match.group('host').casefold()}/{match.group('portal')}",
        ),
    ),
    employer_slug=True,
    continuations=listed_page_continuations(
        AVATURE_MAPPING, _portal_openings, detail=avature_page_posting, sitemaps=True
    ),
)
