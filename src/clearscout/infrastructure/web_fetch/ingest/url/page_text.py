"""Readable text from a posting page."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from clearscout.application.discovery.source_reading import PostingCheck
from clearscout.domain import utc_now
from clearscout.domain.artifacts import PostingSource
from clearscout.infrastructure.web_fetch.board_mappings import (
    plain_description,
)
from clearscout.infrastructure.web_fetch.board_mappings.schema_org import declared_check
from clearscout.infrastructure.web_fetch.board_platform import host_match
from clearscout.infrastructure.web_fetch.board_slugs import title_from_url_slug
from clearscout.infrastructure.web_fetch.ingest.careers import inspect_publisher_page
from clearscout.infrastructure.web_fetch.ingest.job_posting_markup import (
    JobPostingMarkup,
    job_postings,
)
from clearscout.infrastructure.web_fetch.ingest.readable import closed_page_notice, readable_text
from clearscout.infrastructure.web_fetch.ingest.text import (
    MINIMUM_USEFUL_CHARACTERS,
    require_enough_text,
    strip_tags,
)
from clearscout.infrastructure.web_fetch.ingest.url.results import (
    PAGE_WITH_PUBLISHER_MARKUP_METHOD,
    PUBLISHER_MARKUP_METHOD,
    UrlPostingText,
)
from clearscout.infrastructure.web_fetch.ingest.url.transport import (
    LINKEDIN_JOB_VIEW,
    LINKEDIN_RELATED_LISTINGS,
)
from clearscout.shared.errors import IngestError


@dataclass(frozen=True)
class PageText:
    """Readable text from one posting page, with how it was read and what the page stated."""

    text: str
    method: str
    valid_through: str
    publisher_markup_present: bool
    title: str = ""
    employer: str = ""
    publisher_check: PostingCheck | None = None
    # The block the fields above were copied from, beside how many the page declared.
    declared: JobPostingMarkup | None = None
    declared_count: int = 0
    bounded_markup: str = ""


def from_html(url: str, body: str) -> UrlPostingText:
    """Extract one already-fetched publisher page without another network read."""
    page = read_page_text(body, url)
    require_enough_text(page.text, url)
    return UrlPostingText(
        text=with_url_slug_title(page.text, url),
        origin=url,
        method=page.method,
        valid_through=page.valid_through,
        publisher_markup_present=page.publisher_markup_present,
        title=page.title,
        employer=page.employer,
        publisher_check=page.publisher_check,
        source=page_source(page, body),
    )


def with_url_slug_title(body: str, url: str) -> str:
    """Prepend a URL-derived title when the board hides the header from the body.

    iCIMS serves postings in an iframe that strips the title the URL path still names.
    """
    derived = title_from_url_slug(url)
    if not derived:
        return body
    return f"Title: {derived}\n\n{body}"


def read_page_text(body: str, page_url: str) -> PageText:
    """Prefer the description the page declares about itself, and say so.

    A `JobPosting` block is the publisher stating what the posting says, so it
    beats recovering the same words from the rendered page. It is used only when
    it carries a usable body: plenty of pages declare the markup for the title
    and dates and leave the description to the page.
    """
    postings = job_postings(body)
    read_at = utc_now()
    for posting in postings:
        declared = plain_description(posting.description)
        if len(declared) >= MINIMUM_USEFUL_CHARACTERS:
            return PageText(
                declared,
                PUBLISHER_MARKUP_METHOD,
                posting.valid_through,
                True,
                posting.title,
                posting.employer,
                declared_check(posting, page_url=page_url, read_at=read_at),
                posting,
                len(postings),
            )
    evidence = inspect_publisher_page(page_url, body)
    bounded = evidence.posting_body_markup
    text = _preview_text(bounded) if bounded else _extract(body, page_url)
    notice = closed_page_notice(body, text)
    if notice:
        raise IngestError(notice)
    if postings:
        return PageText(
            text,
            PAGE_WITH_PUBLISHER_MARKUP_METHOD,
            postings[0].valid_through,
            True,
            postings[0].title,
            postings[0].employer,
            declared_check(postings[0], page_url=page_url, read_at=read_at),
            postings[0],
            len(postings),
            bounded,
        )
    return PageText(
        text,
        "page extraction",
        "",
        False,
        title=evidence.title if bounded else "",
        bounded_markup=bounded,
    )


def page_source(page: PageText, html: str) -> PostingSource:
    """Return the page a body was read from, with the declared node and markup it used."""
    declared = page.declared
    used = declared if page.method == PUBLISHER_MARKUP_METHOD else None
    return PostingSource(
        publisher="page",
        record=declared.node if declared is not None else "",
        description_markup=used.description if used is not None else page.bounded_markup,
        description_paths=(
            used.description_paths
            if used is not None
            else ("div.job-preview-details",)
            if page.bounded_markup
            else ()
        ),
        page_html=html,
    )


def _extract(body: str, page_url: str = "") -> str:
    """Pull the readable article out of an HTML page."""
    linkedin = host_match(LINKEDIN_JOB_VIEW, page_url) is not None
    text, within_blocks = readable_text(
        body, prune_xpath=LINKEDIN_RELATED_LISTINGS if linkedin else None
    )
    if not within_blocks:
        raise IngestError("The page states its posting only outside its readable blocks.")
    # strip_tags already ends in the layout pass.
    return text if text is not None else strip_tags(body)


def _preview_text(markup: str) -> str:
    """Read every block of a bounded posting preview without its application controls."""
    from lxml.html import fromstring, tostring  # type: ignore[import-untyped]

    detail = fromstring(markup)
    for element in detail.xpath(".//a | .//form | .//nav | .//header | .//footer"):
        if element.tag == "a":
            path = urlparse(element.get("href", "")).path.casefold()
            if "/apply/" not in path and not path.rstrip("/").endswith("/jobs/all"):
                continue
        element.drop_tree()
    return strip_tags(tostring(detail, encoding="unicode"))
