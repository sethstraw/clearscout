"""Bounded employer-owned evidence for cover-letter drafting."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections.abc import Collection
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from tailorcv.domain import ResearchKind
from tailorcv.domain.identity import normalize_url
from tailorcv.domain.records import PageClassification
from tailorcv.infrastructure.web_fetch.boards import job_board_address
from tailorcv.infrastructure.web_fetch.ingest.careers import inspect_publisher_page
from tailorcv.infrastructure.web_fetch.ingest.job_posting_markup import job_postings
from tailorcv.infrastructure.web_fetch.ingest.readable import readable_text
from tailorcv.infrastructure.web_fetch.ingest.url import (
    PAGE_HEADERS,
    TIMEOUT,
    USER_AGENT,
)
from tailorcv.infrastructure.web_fetch.policy import FetchPolicyError, PoliteHttpClient

MAX_PAGE_BYTES = 400_000
MAX_SOURCE_CHARACTERS = 12_000
# At defaults trafilatura can choose a site menu over the page's own prose. Headers
# stay, because about pages often hold the company description there.
_SITE_CHROME = ["//nav", "//footer"]
# Shorter employer page text is a page title or a script notice, not an account of the company.
MIN_CAREERS_CHARACTERS = 240
# A careers landing page's prose and perks read as a posting or as chrome to the classifier.
_REFUSED_CAREERS_PAGES = frozenset(
    {PageClassification.ERROR_OR_CHALLENGE, PageClassification.JS_SHELL}
)
# A template field such as "{{ job.title }}" that the page served unrendered.
_UNRENDERED_PLACEHOLDER = re.compile(r"\{\{\s*[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\s*\}\}")


class CareersPageRefusedError(Exception):
    """The configured careers page cannot describe the company, so no research is taken from it."""


@dataclass(frozen=True)
class ResearchSource:
    """One quoted source whose identifier can support a letter paragraph."""

    id: str
    kind: ResearchKind
    url: str
    text: str
    method: str


def fingerprint(sources: tuple[ResearchSource, ...]) -> str:
    """Digest the exact research that can change a cover letter."""
    if not sources:
        return ""
    payload = json.dumps(
        [
            {key: value for key, value in asdict(item).items() if key != "method"}
            for item in sources
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def careers_source_refused(source: ResearchSource, posting_urls: Collection[str] = ()) -> bool:
    """Return whether a careers source's address or text cannot describe the company."""
    return _careers_address_refused(source.url, posting_urls) or research_text_refused(source.text)


def research_text_refused(text: str) -> bool:
    """Return whether employer page text is too thin or carries an unrendered placeholder."""
    stripped = text.strip()
    return (
        len(stripped) < MIN_CAREERS_CHARACTERS
        or _UNRENDERED_PLACEHOLDER.search(stripped) is not None
    )


def _careers_address_refused(url: str, posting_urls: Collection[str]) -> bool:
    """Return whether the address is this posting's own page or any job board address."""
    own = {normalize_url(item) for item in posting_urls if item.strip()}
    return normalize_url(url) in own or job_board_address(url)


def evidence_block(sources: tuple[ResearchSource, ...]) -> str:
    """Render sources as untrusted, identifier-bearing prompt evidence."""
    return "\n\n".join(
        f"Source ID: {item.id}\nKind: {item.kind}\nURL: {item.url}\n\n{item.text}"
        for item in sources
    )


class CompanyResearchFetcher:
    """Read one careers page and one same-host about page through shared policy."""

    def __init__(self, client: PoliteHttpClient | None = None) -> None:
        """Use the shared polite client or an injected test seam."""
        self.client = client or PoliteHttpClient(user_agent=USER_AGENT)

    def fetch(
        self, careers_url: str, *, posting_urls: Collection[str] = ()
    ) -> tuple[ResearchSource, ...]:
        """Return only successfully read employer-owned pages, or nothing when a read fails.

        Raises ``CareersPageRefusedError`` for a careers page that is one of
        ``posting_urls`` or a job board address, declares a posting, is a script wall,
        or has too little of its own text outside chrome; its links are not followed.
        """
        if not research_can_read(careers_url):
            return ()
        if _careers_address_refused(careers_url, posting_urls):
            raise CareersPageRefusedError(careers_url)
        careers_page = self._page(careers_url, source_id="company.careers", kind="careers")
        if careers_page is None:
            return ()
        careers, careers_html = careers_page
        evidence = inspect_publisher_page(careers_url, careers_html)
        if (
            careers_source_refused(careers, posting_urls)
            or evidence.classification in _REFUSED_CAREERS_PAGES
            or len(evidence.text_outside_chrome) < MIN_CAREERS_CHARACTERS
            or job_postings(careers_html)
        ):
            raise CareersPageRefusedError(careers_url)
        sources = [careers]
        about_url = _about_url(careers_url, careers_html)
        if about_url:
            about_page = self._page(about_url, source_id="company.about", kind="about")
            if about_page is not None and not research_text_refused(about_page[0].text):
                sources.append(about_page[0])
        return tuple(sources)

    def _page(
        self, url: str, *, source_id: str, kind: ResearchKind
    ) -> tuple[ResearchSource, str] | None:
        try:
            response = self.client.get(
                url,
                headers=PAGE_HEADERS,
                timeout=TIMEOUT,
                follow_redirects=False,
            )
            response.raise_for_status()
        except (httpx.HTTPError, FetchPolicyError):
            return None
        content_type = response.headers.get("content-type", "").casefold()
        if content_type and "html" not in content_type and not content_type.startswith("text/"):
            return None
        body = response.content[:MAX_PAGE_BYTES].decode(
            response.encoding or "utf-8", errors="replace"
        )
        extracted, _within_blocks = readable_text(body, prune_xpath=_SITE_CHROME)
        text = (extracted or "")[:MAX_SOURCE_CHARACTERS].strip()
        if not text:
            return None
        return ResearchSource(source_id, kind, url, text, "polite_html"), body


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href = ""
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "a":
            self._href = dict(attrs).get("href") or ""
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self._href:
            self.links.append((self._href, " ".join(self._text)))
            self._href = ""
            self._text = []


def _about_url(base_url: str, body: str) -> str:
    """Choose one same-host link explicitly labeled as an about page."""
    parser = _Links()
    parser.feed(body)
    base_host = (urlsplit(base_url).hostname or "").casefold()
    candidates: list[str] = []
    for href, label in parser.links:
        candidate = urljoin(base_url, href)
        parsed = urlsplit(candidate)
        signal = f"{parsed.path} {label}".casefold()
        if (
            parsed.hostname
            and parsed.hostname.casefold() == base_host
            and "about" in signal
            and research_can_read(candidate)
        ):
            candidates.append(candidate)
    return min(candidates, key=len) if candidates else ""


def research_can_read(url: str) -> bool:
    """Return whether an address is a public http(s) page research may request."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold()
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        return False
    if host == "localhost" or host.rpartition(".")[2] in {"local", "internal"}:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (address.is_private or address.is_loopback or address.is_link_local)
