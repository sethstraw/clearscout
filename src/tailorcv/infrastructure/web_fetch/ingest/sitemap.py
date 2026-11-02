"""Discover job-posting URLs from a site's XML sitemaps.

Reads robots.txt for `Sitemap:` lines, falls back to the two conventional
sitemap locations (a Humi board's `/sitemap` in their place), expands a bounded
number of child sitemaps, and returns URLs whose path looks like a posting under
a conservative allowlist. Sitemaps a bound kept unread are returned so a caller
can resume from them.

Fetching is injected so the board fetcher's request seam and politeness policy
apply. `defusedxml` forbids DTDs and entity expansion in a hostile sitemap.
"""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from html.entities import name2codepoint
from urllib.parse import urlsplit

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError, fromstring

# Feeds written as HTML (WordPress's) carry named entities XML does not define.
_NAMED_ENTITY = re.compile(r"&([A-Za-z][A-Za-z0-9]*);")
_XML_ENTITIES = frozenset({"amp", "lt", "gt", "quot", "apos"})

# A posting URL carries one of these path segments followed by at least one more
# segment; the bare collection page (`/careers`, `/jobs`) is navigation.
_JOB_PATH_SEGMENTS = frozenset(
    {
        "job",
        "jobs",
        "career",
        "careers",
        "position",
        "positions",
        "opening",
        "openings",
        "posting",
        "postings",
        "vacancy",
        "vacancies",
        "role",
        "roles",
        "requisition",
        "requisitions",
        "opportunity",
        "opportunities",
    }
)

_DOCUMENT_EXTENSIONS = frozenset(
    {
        "csv",
        "doc",
        "docx",
        "gif",
        "jpeg",
        "jpg",
        "mp3",
        "mp4",
        "odt",
        "pdf",
        "png",
        "ppt",
        "pptx",
        "rtf",
        "svg",
        "webp",
        "xls",
        "xlsx",
        "zip",
    }
)


@dataclass(frozen=True)
class SitemapLimits:
    """Visible bounds for one sitemap discovery pass."""

    max_sitemap_fetches: int = 10
    max_child_sitemaps: int = 5
    # XML-only discovery fetches no posting pages here, so a generous bound
    # costs only one string per candidate.
    max_candidate_urls: int = 5000
    max_elapsed_seconds: float = 20.0


@dataclass(frozen=True)
class SitemapDiscovery:
    """Posting URLs found in one discovery pass, and what a bound left unread."""

    candidate_urls: tuple[str, ...]
    truncated: bool
    pending_sitemaps: tuple[str, ...] = ()
    non_posting_locs: int = 0


def discover_job_urls(
    fetch: Callable[[str], str | None], base_url: str, *, limits: SitemapLimits
) -> SitemapDiscovery:
    """Return posting URLs discovered from ``base_url``'s sitemaps, within ``limits``.

    ``fetch`` returns a URL's text, or None on any failure or non-200. Tries
    robots.txt for declared sitemaps first, then the two conventional locations.
    """
    origin = _origin(base_url)
    if not origin:
        return SitemapDiscovery((), False)
    deadline_at = time.monotonic() + limits.max_elapsed_seconds
    seeds = robots_sitemaps(fetch, origin)
    if not seeds and _humi_board(origin):
        seeds = [f"{origin}/sitemap"]
    elif not seeds:
        seeds = [f"{origin}/sitemap.xml", f"{origin}/sitemap_index.xml"]
    return _expand(fetch, seeds, limits, deadline_at)


def expand_sitemaps(
    fetch: Callable[[str], str | None], sitemap_urls: Iterable[str], *, limits: SitemapLimits
) -> SitemapDiscovery:
    """Resume discovery from sitemaps an earlier pass left unread."""
    return _expand(fetch, list(sitemap_urls), limits, time.monotonic() + limits.max_elapsed_seconds)


def _expand(
    fetch: Callable[[str], str | None],
    seeds: list[str],
    limits: SitemapLimits,
    deadline_at: float,
) -> SitemapDiscovery:
    """Read queued sitemaps within ``limits``; ``truncated`` marks any bound that cut work."""
    candidates: list[str] = []
    seen_candidates: set[str] = set()
    seen_sitemaps: set[str] = set()
    pending: list[str] = []
    queue: deque[str] = deque(seeds)
    fetches = 0
    children_expanded = 0
    non_posting = 0
    truncated = False
    while (
        queue
        and fetches < limits.max_sitemap_fetches
        and len(candidates) < limits.max_candidate_urls
    ):
        if time.monotonic() >= deadline_at:
            truncated = True
            break
        sitemap_url = queue.popleft()
        identity = sitemap_url.casefold()
        if identity in seen_sitemaps:
            continue
        seen_sitemaps.add(identity)
        text = fetch(sitemap_url)
        fetches += 1
        if text is None:
            continue
        is_index, locs = parse_sitemap(text)
        if is_index:
            children_expanded, dropped = _enqueue_children(
                locs, queue, seen_sitemaps, children_expanded, limits.max_child_sitemaps
            )
            pending.extend(dropped)
            truncated = truncated or bool(dropped)
            continue
        cut, skipped = _collect_job_urls(
            locs, candidates, seen_candidates, limits.max_candidate_urls
        )
        non_posting += skipped
        truncated = truncated or cut
    pending.extend(url for url in queue if url.casefold() not in seen_sitemaps)
    truncated = truncated or bool(queue)
    return SitemapDiscovery(
        tuple(candidates),
        truncated,
        pending_sitemaps=tuple(dict.fromkeys(pending)),
        non_posting_locs=non_posting,
    )


def _enqueue_children(
    locs: list[str], queue: deque[str], seen_sitemaps: set[str], expanded: int, limit: int
) -> tuple[int, list[str]]:
    """Queue a sitemap index's not-yet-seen children up to the limit; return those dropped."""
    for index, loc in enumerate(locs):
        if expanded >= limit:
            return expanded, [
                remaining for remaining in locs[index:] if remaining.casefold() not in seen_sitemaps
            ]
        if loc.casefold() not in seen_sitemaps:
            queue.append(loc)
            expanded += 1
    return expanded, []


def _collect_job_urls(
    locs: list[str], candidates: list[str], seen: set[str], limit: int
) -> tuple[bool, int]:
    """Append the not-yet-seen job URLs among ``locs``, up to the candidate limit.

    Returns whether the limit cut off a job URL, and how many locs were not job-shaped.
    """
    non_posting = 0
    for index, loc in enumerate(locs):
        if len(candidates) >= limit:
            remaining = locs[index:]
            shaped = [url for url in remaining if _looks_like_job_url(url)]
            non_posting += len(remaining) - len(shaped)
            return any(url.casefold() not in seen for url in shaped), non_posting
        if not _looks_like_job_url(loc):
            non_posting += 1
        elif loc.casefold() not in seen:
            seen.add(loc.casefold())
            candidates.append(loc)
    return False, non_posting


def robots_sitemaps(fetch: Callable[[str], str | None], origin: str) -> list[str]:
    """Return the absolute sitemap URLs a site's robots.txt declares, if any."""
    text = fetch(f"{origin}/robots.txt")
    if text is None:
        return []
    found: list[str] = []
    for line in text.splitlines():
        label, separator, value = line.partition(":")
        if separator and label.strip().casefold() == "sitemap":
            target = value.strip()
            if target.startswith(("http://", "https://")):
                found.append(target)
    return found


def xml_entity_references(text: str) -> str:
    """Rewrite HTML named entities that XML leaves undefined, such as ``&nbsp;``, as numeric."""
    return _NAMED_ENTITY.sub(
        lambda match: (
            f"&#{name2codepoint[match[1]]};"
            if match[1] in name2codepoint and match[1] not in _XML_ENTITIES
            else match[0]
        ),
        text,
    )


def parse_sitemap(text: str) -> tuple[bool, list[str]]:
    """Return whether the document is a sitemap index and the <loc> URLs it lists.

    An RSS feed served at a sitemap address lists each item's <link> instead.
    """
    try:
        root = fromstring(text)
    except (ParseError, DefusedXmlException):
        try:
            root = fromstring(xml_entity_references(text))
        except (ParseError, DefusedXmlException):
            return False, []
    if root.tag == "rss":
        links = (item.findtext("link") or "" for item in root.iterfind("./channel/item"))
        return False, [link.strip() for link in links if link.strip()]
    is_index = _localname(root.tag) == "sitemapindex"
    locs = [
        (element.text or "").strip()
        for element in root.iter()
        if _localname(element.tag) == "loc" and (element.text or "").strip()
    ]
    return is_index, locs


def _looks_like_job_url(url: str) -> bool:
    if is_document_url(url):
        return False
    segments = [segment.casefold() for segment in urlsplit(url).path.split("/") if segment]
    if _humi_board(url):
        return len(segments) == 2 and segments[1].isdigit()
    return any(
        segment in _JOB_PATH_SEGMENTS and index < len(segments) - 1
        for index, segment in enumerate(segments)
    )


def is_document_url(url: str) -> bool:
    """Return whether a URL's last path segment names a document or media file."""
    last = urlsplit(url).path.rstrip("/").rpartition("/")[2]
    _stem, dot, extension = last.rpartition(".")
    return bool(dot) and extension.casefold() in _DOCUMENT_EXTENSIONS


def is_non_html_media_type(content_type: str) -> bool:
    """Return whether a response's declared media type cannot carry HTML markup."""
    media = content_type.partition(";")[0].strip().casefold()
    if not media:
        return False
    # Servers label HTML as text/plain often enough that no text type is trusted to exclude it.
    return not (media.startswith("text/") or media.endswith(("/xhtml+xml", "/xml", "+xml")))


def _humi_board(url: str) -> bool:
    """Return whether a URL is on a Humi board, which names a posting `/<team>/<id>`."""
    return (urlsplit(url).hostname or "").endswith(".applytojobs.ca")


def _localname(tag: object) -> str:
    """Return an XML tag's local name without its namespace."""
    return str(tag).rpartition("}")[2]


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"
