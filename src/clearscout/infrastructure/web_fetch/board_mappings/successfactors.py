"""SAP SuccessFactors career sites: sitemap-listed posting pages that declare their JobPosting."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from typing import Any
from urllib.parse import urlsplit

from clearscout.application.discovery.posting_text import month_first_day
from clearscout.infrastructure.web_fetch.board_mappings.page_posting import (
    og_titles,
    page_posting,
    page_posting_declaration,
)
from clearscout.infrastructure.web_fetch.ingest import strip_tags
from clearscout.infrastructure.web_fetch.ingest.html_blocks import html_blocks
from clearscout.infrastructure.web_fetch.ingest.job_posting_markup import microdata_date_posted
from clearscout.infrastructure.web_fetch.ingest.readable import closed_page_notice

# A posting page is /<brand>/job/<slug>/<id>/ or, outside any brand, /job/<slug>/<id>/.
_POSTING_PATH = re.compile(r"/(?:(?P<brand>[^/]+)/)?job/[^/]+/(?P<job>\d+)/?")
# First path segments that are site pages rather than a brand's board.
_SITE_PAGES = frozenset({"", "job", "search", "go", "content", "talentcommunity", "viewalljobs"})


def successfactors_brand(url: str) -> str:
    """Return the brand a career site address names in its first path segment, or ""."""
    first = urlsplit(url).path.strip("/").split("/", 1)[0]
    return "" if first.casefold() in _SITE_PAGES else first


def successfactors_openings(pages: list[str], host: str, brand: str) -> list[dict[str, str]]:
    """Return one row per posting page on ``host`` under ``brand`` (or under none, when "")."""
    rows: list[dict[str, str]] = []
    for page in pages:
        parts = urlsplit(page)
        posting = _POSTING_PATH.fullmatch(parts.path)
        if parts.netloc.casefold() != host.casefold() or posting is None:
            continue
        if (posting.group("brand") or "").casefold() != brand.casefold():
            continue
        rows.append({"id": posting.group("job"), "url": page})
    return rows


_GENERIC_ROLES = frozenset({"careers", "jobs", "job search", "job opportunities", "opportunities"})
_POSTING_TYPES = frozenset({"http://schema.org/JobPosting", "https://schema.org/JobPosting"})


def _itemprop_spans(markup: str, name: str) -> list[str]:
    return [
        block.inner
        for block in html_blocks(markup, "span")
        if name in block.attrs.get("itemprop", "").split()
    ]


def _itemprop_posting(page: str, shell: str) -> Mapping[str, Any] | None:
    """Read the page's one itemprop title, confirmed by og:title, and the shell's itemprop body."""
    titles = _itemprop_spans(page, "title")
    if len(titles) != 1:
        return None
    role = strip_tags(titles[0]).strip()
    if not role or role.casefold() in _GENERIC_ROLES or og_titles(page) != [role]:
        return None
    bodies = [body for body in _itemprop_spans(shell, "description") if strip_tags(body).strip()]
    if not bodies:
        return None
    return {"title": role, "description": "".join(bodies), "places": [], "places_complete": False}


def _unlabelled_posting(page: str, shell: str) -> Mapping[str, Any] | None:
    """Read one owned description when the publisher's two role titles agree."""
    heads = html_blocks(page, "head")
    if len(heads) != 1:
        return None
    titles = html_blocks(heads[0].inner, "title")
    if len(titles) != 1:
        return None
    title = re.fullmatch(r"(.+) Job Details \| (.+)", strip_tags(titles[0].inner).strip())
    if title is None or og_titles(page) != [title.group(1)]:
        return None
    role = title.group(1).strip()
    if role.casefold() in _GENERIC_ROLES:
        return None
    descriptions = [
        block
        for block in html_blocks(shell, "span")
        if "description" in block.attrs.get("itemprop", "").split()
    ]
    if len(descriptions) != 1:
        return None
    description = descriptions[0]
    if description.attrs.get(
        "data-careersite-propertyid"
    ) != "description" or not description.has_class("rtltextaligneligible"):
        return None
    bodies = html_blocks(description.inner, "span", class_name="jobdescription")
    if len(bodies) != 1 or not strip_tags(bodies[0].inner).strip():
        return None
    return {
        "title": role,
        "description": bodies[0].inner,
        "places": [],
        "places_complete": False,
    }


def successfactors_withdrawn(record: Mapping[str, Any]) -> bool:
    """Report whether a detail record is a posting page that says the job has closed."""
    return record.get("closed") is True


def successfactors_page_posting(page: str) -> Mapping[str, Any] | None:
    """Read declared posting facts or an owned SuccessFactors detail template."""
    declared = page_posting(page)
    if declared is not None:
        labelled = "" if declared.get("posted") else _labelled_posted(page)
        return {**declared, "posted": labelled} if labelled else declared
    posting = _template_posting(page)
    if posting is None or "title" not in posting:
        return posting
    posted = microdata_date_posted(page) or _labelled_posted(page)
    return {**posting, "posted": posted} if posted else posting


_POSTED_LABELS = frozenset({"posting start date", "date posted", "posted date"})


def _labelled_posted(page: str) -> str:
    """Return the one labelled posted date as ISO, or "" when absent, several, or ambiguous."""
    stated: set[str] = set()
    for row in html_blocks(page, "div"):
        labels = html_blocks(row.inner, "span", class_name="joblayouttoken-label")
        values = html_blocks(row.inner, "span", class_name="rtltextaligneligible")
        if len(labels) != 1 or len(values) != 1:
            continue
        label = strip_tags(labels[0].inner).strip().rstrip(":").strip().casefold()
        if label in _POSTED_LABELS:
            stated.add(_posted_day(strip_tags(values[0].inner).strip(), values[0].attrs))
    return stated.pop() if len(stated) == 1 else ""


def _posted_day(value: str, attrs: Mapping[str, str]) -> str:
    # Day and month order is only known for a US-English span; other numeric dates stay unread.
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value).isoformat()
        lang = (attrs.get("lang") or attrs.get("xml:lang") or "").casefold()
        return month_first_day(value) if lang == "en-us" else ""
    except ValueError:
        return ""


def _template_posting(page: str) -> Mapping[str, Any] | None:
    """Read the jobDisplayShell template, or a closed notice when the page has no shell."""
    shells = html_blocks(page, "div", class_name="jobDisplayShell")
    if not shells:
        bodies = html_blocks(page, "body")
        text = strip_tags(bodies[0].inner if len(bodies) == 1 else page)
        return {"closed": True} if closed_page_notice(page, text) else None
    if len(shells) != 1 or "itemscope" not in shells[0].attrs:
        return None
    itemtype = shells[0].attrs.get("itemtype")
    if itemtype is not None and itemtype not in _POSTING_TYPES:
        return None
    shell = shells[0].inner
    if not html_blocks(shell, "span", class_name="joblayouttoken-label"):
        return _unlabelled_posting(page, shell) or _itemprop_posting(page, shell)
    fields = _labelled_fields(shell)
    if fields is None:
        return None
    title = strip_tags(fields.get("title", ""))
    description = fields.get("description", "")
    if not title or not strip_tags(description):
        return _itemprop_posting(page, shell)
    location = strip_tags(fields.get("location", ""))
    return {
        "title": title,
        "description": description,
        "places": [location] if location else [],
        "places_complete": False,
    }


def _labelled_fields(shell: str) -> dict[str, str] | None:
    """Return each labelled token's value by casefolded label, or None on an ambiguous token."""
    fields: dict[str, str] = {}
    for token in html_blocks(shell, "div", class_name="joblayouttoken"):
        labels = html_blocks(token.inner, "span", class_name="joblayouttoken-label")
        if not labels:
            continue
        values = html_blocks(token.inner, "span", class_name="rtltextaligneligible")
        if len(labels) != 1 or len(values) != 1:
            return None
        label = strip_tags(labels[0].inner).strip().rstrip(":").strip().casefold()
        if label in fields:
            return None
        fields[label] = values[0].inner
    return fields


SUCCESSFACTORS_MAPPING = page_posting_declaration("successfactors")
