"""A board whose list names each opening and whose posting page declares its JobPosting.

The detail record is what the posting page's markup declares, so every platform
of this shape shares one declaration and overrides only the fields its list
row states better than the page does.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from html.parser import HTMLParser
from typing import Any

from tailorcv.domain.staged_facts import PostedDateGrade
from tailorcv.infrastructure.web_fetch.board_mappings.mapper import (
    BoardDeclaration,
    posted_on,
    salary_compensation,
    stated,
)
from tailorcv.infrastructure.web_fetch.ingest.html_blocks import html_blocks
from tailorcv.infrastructure.web_fetch.ingest.job_posting_markup import job_postings


def page_posting(page: str) -> Mapping[str, Any] | None:
    """Return what a posting page's JobPosting markup declares, or None when it declares none."""
    declared = next(iter(job_postings(page)), None)
    if declared is None:
        return None
    return {
        "title": declared.title,
        "description": declared.description,
        "employer": declared.employer,
        "places": list(declared.locations),
        "places_complete": declared.locations_complete,
        "employment_types": list(declared.employment_types),
        "compensation": salary_compensation(declared.base_salary),
        "posted": declared.date_posted,
    }


class _OpenGraphTitles(HTMLParser):
    """Collect every Open Graph title declaration in a publisher head."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.titles: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "meta" and values.get("property") == "og:title":
            self.titles.append(values.get("content") or "")


def og_titles(page: str) -> list[str] | None:
    """Return the Open Graph titles of the page's one head, or None without exactly one head."""
    heads = html_blocks(page, "head")
    if len(heads) != 1:
        return None
    metadata = _OpenGraphTitles()
    metadata.feed(heads[0].inner)
    metadata.close()
    return metadata.titles


def _detail_or_row_places(posting: Any) -> Any:
    return posting.record["detail"].get("places") or posting.record["row"].get("places", [])


def page_posting_declaration(
    platform: str, *, row_places: bool = False, **fields: Any
) -> BoardDeclaration:
    """Return the declaration joining a list row (``id``, ``url``) to its page's posting.

    ``row_places`` falls back to the row's places when the page states none; ``fields``
    replaces any default.
    """
    declaration = BoardDeclaration(
        platform=platform,
        title=("detail", "title"),
        url=("row", "url"),
        employer=("detail", "employer"),
        places=_detail_or_row_places if row_places else ("detail", "places"),
        locations_complete=lambda posting: (
            stated(posting.record, ("detail", "places_complete")) is True
        ),
        requisition_id=("row", "id"),
        employment_types=("detail", "employment_types"),
        compensation=("detail", "compensation"),
        description=(("detail", "description"),),
        posted=posted_on(("detail", "posted"), PostedDateGrade.BOARD_DATE, basis="datePosted"),
    )
    return replace(declaration, **fields)
