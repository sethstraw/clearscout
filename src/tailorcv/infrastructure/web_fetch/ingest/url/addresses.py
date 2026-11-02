"""Posting addresses: canonical forms, identifiers and redirects off a posting."""

from __future__ import annotations

import re
from urllib.parse import unquote, urlparse, urlsplit

from tailorcv.domain.identity import (
    PHRASE_CONNECTORS,
    mentions_in_order,
    normalize_text,
    normalize_url,
)
from tailorcv.domain.records import PageClassification
from tailorcv.infrastructure.web_fetch.board_platform import host_match
from tailorcv.infrastructure.web_fetch.boards import (
    JOB_URL_PATTERNS,
    canonicalise_source_url,
)
from tailorcv.infrastructure.web_fetch.ingest.careers import inspect_publisher_page
from tailorcv.infrastructure.web_fetch.ingest.job_posting_markup import (
    JobPostingMarkup,
)
from tailorcv.infrastructure.web_fetch.ingest.url.page_text import PageText


def canonical_posting_address(url: str, landed: str, html: str) -> str:
    """Return the page's canonical address when it names this posting elsewhere on its host."""
    canonical = inspect_publisher_page(landed, html).canonical_url
    if not canonical or normalize_url(canonical) == normalize_url(landed):
        return ""
    if urlparse(canonical).hostname != urlparse(landed).hostname:
        return ""
    identifiers = _path_identifiers(url)
    path = unquote(urlparse(canonical).path)
    if not identifiers or not all(_carries_token(path, token) for token in identifiers):
        return ""
    return canonical


# Shorter than this, a number in a path is a page of results rather than a posting.
_SHORTEST_POSTING_IDENTIFIER = 4


# A slug ending in one of these names a list of postings, whatever else the path carries.
LISTING_WORDS = frozenset(
    {"jobs", "job", "careers", "openings", "positions", "vacancies", "roles", "results"}
)


def _path_spells_the_title(title: str, path: str) -> bool:
    """Return whether one path segment spells a title in a path that carries an identifier.

    A category page spells its subject too, with a listing word or a place beside
    it, so only an identifier separates one posting's address from a category's.
    """
    wanted = [word for word in normalize_text(title).split() if word not in PHRASE_CONNECTORS]
    if len(wanted) < 2:
        return False
    segments = [normalize_text(segment) for segment in path.split("/") if segment]
    spelling = [segment for segment in segments if mentions_in_order(segment, " ".join(wanted))]
    if not any(segment.split()[-1] not in LISTING_WORDS - set(wanted) for segment in spelling):
        return False
    return any(
        _is_posting_identifier(word) and word not in wanted
        for segment in segments
        for word in segment.split()
    )


def _is_posting_identifier(word: str) -> bool:
    """Return whether one path token is long enough and digit-bearing to identify a posting."""
    return len(word) >= _SHORTEST_POSTING_IDENTIFIER and re.search(r"\d", word) is not None


_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.IGNORECASE)


def _path_identifiers(url: str) -> list[str]:
    """Return the posting identifiers a URL's path carries: its uuids, then its other tokens."""
    path = unquote(urlparse(url).path)
    tokens = re.split(r"[^A-Za-z0-9]+", _UUID.sub(" ", path))
    return _UUID.findall(path) + [token for token in tokens if _is_posting_identifier(token)]


def _carries_token(path: str, token: str) -> bool:
    """Return whether a path carries a token whole rather than inside a longer run."""
    # Bounded on both sides, because a short requisition number is a digit run
    # inside most paths: "7" would otherwise name the posting at /jobs/67890.
    bounded = rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])"
    return re.search(bounded, path, re.IGNORECASE) is not None


def names_the_posting(declared: JobPostingMarkup, url: str, landed: str) -> bool:
    """Return whether one declared block identifies the posting that was requested."""
    # A declared url claims the posting's address, so a different one names another
    # posting; an identifier claims nothing about the address, so a path that does
    # not carry it is no evidence, and the title rule still answers.
    if declared.url:
        return normalize_url(declared.url) == normalize_url(url)
    path = unquote(urlparse(url).path)
    if declared.identifier and _carries_token(path, declared.identifier):
        return True
    # A read that landed elsewhere shows another page, whatever its path spells.
    if unquote(urlparse(landed).path).rstrip("/") != path.rstrip("/"):
        return False
    return _path_spells_the_title(declared.title, path)


def left_the_posting(url: str, landed: str, declined: list[str]) -> str:
    """Explain a posting URL that ended up somewhere that is not a posting.

    Boards answer a closed posting with a redirect to the index rather than a
    404, and that page extracts cleanly into the wrong document.
    """
    return "\n".join(
        [
            f"{url} redirected to {landed}, which is a job board rather than a posting.",
            *declined,
            "  The posting may have closed, or the board may be refusing automated reads.",
            "  Open it in a browser, save it as a PDF or screenshot it, and pass that path.",
        ]
    )


def landed_on_its_own_listing(
    url: str, landed: str, page: PageText, classification: PageClassification | None
) -> bool:
    """Return whether a posting address reached its own site's listing, which no longer names it.

    An ordinary site may redirect for its own reasons, so only a same-site address
    that reads as a listing and dropped every identifier the requested path carried counts.
    """
    if classification is not PageClassification.COLLECTION or page.publisher_markup_present:
        return False
    if urlsplit(normalize_url(url)).hostname != urlsplit(normalize_url(landed)).hostname:
        return False
    path = unquote(urlsplit(landed).path)
    last = next((segment for segment in reversed(path.split("/")) if segment), "")
    words = re.findall(r"[a-z0-9]+", last.casefold())
    # A posting page with a similar-jobs rail classifies as a collection too, and its slug
    # may carry a listing word ("search-relevance-engineer") among its own.
    if not words or not set(words) <= LISTING_WORDS | {"search"}:
        return False
    identifiers = _posting_identifiers(url)
    return bool(identifiers) and not _keeps_an_identifier(landed, identifiers)


def _posting_identifiers(url: str) -> list[str]:
    """Return the identifiers a posting's path carries that its board's own address does not.

    A tenant named with a digit ("acme24") is on the board root a closure lands on too.
    """
    path = unquote(urlsplit(url).path)
    board = unquote(urlsplit(canonicalise_source_url(url)).path)
    identifiers = _path_identifiers(url)
    if board == path:
        return identifiers
    return [token for token in identifiers if not _carries_token(board, token)]


def _keeps_an_identifier(landed: str, identifiers: list[str]) -> bool:
    """Return whether a landed address carries any identifier in its path, query, or fragment."""
    reached = urlsplit(landed)
    kept = f"{unquote(reached.path)}?{unquote(reached.query)}#{unquote(reached.fragment)}"
    return any(_carries_token(kept, token) for token in identifiers)


def redirected_off_the_posting(requested: str, landed: str) -> bool:
    """Return whether a recognized posting URL ended up somewhere that no longer names it.

    Only asked of URLs whose shape is known, so an ordinary careers page that
    redirects for its own reasons is never second-guessed.
    """
    if not any(
        host_match(pattern, requested) and not host_match(pattern, landed)
        for pattern in JOB_URL_PATTERNS
    ):
        return False
    # A slug is the only identifier some vendors put in a posting's address.
    slug = unquote(urlsplit(requested).path).rstrip("/").rpartition("/")[2]
    identifiers = _posting_identifiers(requested) or ([slug] if slug else [])
    return not _keeps_an_identifier(landed, identifiers)
