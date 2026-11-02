"""Recognize the board platform a career address or its markup names."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from tailorcv.infrastructure.web_fetch.board_platform import BoardKind, BoardPlatform, host_match
from tailorcv.infrastructure.web_fetch.boards import BOARD_PATTERNS, ICIMS_SITE, JOB_URL_PATTERNS
from tailorcv.infrastructure.web_fetch.ingest.html_blocks import html_blocks
from tailorcv.infrastructure.web_fetch.platforms import PLATFORMS
from tailorcv.infrastructure.web_fetch.publisher_sources import REGISTERED_POSTING_LINK

# These stay automatic when found mid-chain: each names finite work whose URLs dedupe.
STRUCTURED_PAGE_CURSOR_REASONS = frozenset(
    {
        *(reason for platform in PLATFORMS for reason in platform.continuations),
        "sitemap_child_pending",
        "sitemap_posting_pending",
        REGISTERED_POSTING_LINK,
    }
)


BOARD_ADAPTER_REGISTRY: dict[str, BoardPlatform] = {
    platform.name: platform for platform in PLATFORMS
}


def identify_board(text: str, *, page_url: str = "") -> tuple[BoardKind, re.Match[str]] | None:
    """Return a supported board named in markup, without promoting posting links."""
    icims_host = host_match(ICIMS_SITE, page_url) is not None
    scripts = html_blocks(text, "script")
    talemetry_frontend = any(
        re.match(
            r"(?:(?:https?:)?//[^/]+)?/pack/talemetry_careersites/[^/?#]+\.js(?:[?#]|$)",
            script.attrs.get("src", ""),
        )
        is not None
        for script in scripts
    ) and any(
        not script.attrs.get("src")
        and re.search(r"CareerSite\.Path\.configure\s*\(\s*\{", script.inner) is not None
        for script in scripts
    )
    for platform, pattern in BOARD_PATTERNS:
        if platform == "successfactors" and (icims_host or talemetry_frontend):
            continue
        for match in pattern.finditer(text):
            address = re.match(r"""[^\s"'<>\\]+""", text[match.start() :])
            # Board patterns can match only the tenant prefix of a posting URL.
            if address is not None and any(
                host_match(job_pattern, address.group(0)) is not None
                for job_pattern in JOB_URL_PATTERNS
            ):
                continue
            return platform, match
    return None


def identify_board_url(url: str) -> tuple[BoardKind, re.Match[str]] | None:
    """Return the board platform a URL's own host serves, never one named in its path or query."""
    for platform, pattern in BOARD_PATTERNS:
        match = host_match(pattern, url)
        if match:
            return platform, match
    return None


@dataclass(frozen=True)
class BoardResolution:
    """What a career address named, and where that was recognized.

    ``platform`` is ``None`` when neither the address nor its fetched page
    names a board this fetcher can read. ``page`` carries the fetched markup so
    a caller that also wants declared postings does not fetch it twice.
    """

    platform: str | None
    match: re.Match[str] | None
    url: str
    page: str | None


@runtime_checkable
class BoardResolver(Protocol):
    """Network seam for identifying a board without reading its listings."""

    def resolve(self, url: str) -> BoardResolution:
        """Return what platform ``url`` (or its fetched page) names, if any."""
        ...
