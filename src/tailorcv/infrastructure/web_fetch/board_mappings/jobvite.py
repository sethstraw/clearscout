"""Jobvite's server-rendered opening lists joined with each page's declared JobPosting."""

from __future__ import annotations

import re
from typing import Any

from tailorcv.infrastructure.web_fetch.board_mappings.mapper import Posting, stated, stated_text
from tailorcv.infrastructure.web_fetch.board_mappings.page_posting import (
    page_posting_declaration,
)
from tailorcv.infrastructure.web_fetch.ingest import strip_tags
from tailorcv.infrastructure.web_fetch.ingest.html_blocks import HtmlBlock, html_blocks

_OPENING = re.compile(
    r"""href\s*=\s*["'](?:https?://jobs\.jobvite\.com)?/(?P<tenant>[A-Za-z0-9_-]+)"""
    r"""/job/(?P<job>[A-Za-z0-9]+)["']""",
    re.IGNORECASE,
)
# A category or all-jobs page lists openings the paged table does not.
_LIST_PAGE = re.compile(r"""href\s*=\s*["'][^"']*/jobs/(?:alljobs|category)(?![\w-])""")
_PAGE_LINK = re.compile(r"""href\s*=\s*["'][^"']*/jobs\?(?:[^"']*?&(?:amp;)?)?p=(?P<page>\d+)""")


def jobvite_board_url(tenant: str, page: int = 0) -> str:
    """Return one zero-indexed server-rendered page of a Jobvite tenant's openings."""
    base = f"https://jobs.jobvite.com/{tenant}/jobs"
    return f"{base}?p={page}" if page else base


def _text(markup: str) -> str:
    return " ".join(strip_tags(markup).split())


def _opening_entries(page: str) -> list[tuple[HtmlBlock, str]]:
    entries = [
        (row, "td")
        for table in html_blocks(page, "table", class_name="jv-job-list")
        for row in html_blocks(table.inner, "tr")
    ]
    entries.extend(
        (anchor, "div")
        for container in html_blocks(page, "div", class_name="jv-job-list")
        for collection in html_blocks(container.inner, "ul")
        for row in html_blocks(collection.inner, "li", class_name="row")
        for anchor in html_blocks(row.inner, "a", class_name="flex-row")
    )
    return sorted(entries, key=lambda entry: page.find(entry[0].inner))


def jobvite_openings(page: str, tenant: str) -> list[dict[str, str]]:
    """Read this tenant's openings from owned table and list containers."""
    rows: list[dict[str, str]] = []
    for row, tag in _opening_entries(page):
        cells = html_blocks(row.inner, tag)
        name = next((cell for cell in cells if cell.has_class("jv-job-list-name")), None)
        if name is None:
            continue
        anchors = html_blocks(name.inner, "a") if tag == "td" else [row]
        link = _OPENING.fullmatch(f'href="{anchors[0].attrs.get("href", "")}"') if anchors else None
        if link is None or link.group("tenant").casefold() != tenant.casefold():
            continue
        where = next((cell for cell in cells if cell.has_class("jv-job-list-location")), None)
        place = "" if where is None or "jv-meta" in where.inner else _text(where.inner)
        job = link.group("job")
        rows.append(
            {
                "id": job,
                "url": f"https://jobs.jobvite.com/{tenant}/job/{job}",
                "title": _text(name.inner),
                "place": place,
            }
        )
    return rows


def jobvite_more_pages(page: str) -> bool:
    """Return whether the page links a category or all-jobs list the paged table does not cover."""
    return _LIST_PAGE.search(page) is not None


def jobvite_linked_pages(page: str) -> set[int]:
    """Return the table page numbers a list page links with ``?p=N``."""
    return {int(link.group("page")) for link in _PAGE_LINK.finditer(page)}


def _places(posting: Posting) -> Any:
    """The list cell's one place is more exact than the page's markup, which may name a country."""
    place = stated_text(posting.record, ("row", "place"))
    return [place] if place else stated(posting.record, ("detail", "places"))


def _locations_complete(posting: Posting) -> bool:
    if stated_text(posting.record, ("row", "place")):
        return True
    return stated(posting.record, ("detail", "places_complete")) is True


JOBVITE_MAPPING = page_posting_declaration(
    "jobvite", places=_places, locations_complete=_locations_complete
)
