from __future__ import annotations

from typing import Any

import pytest

from tailorcv.infrastructure.web_fetch.board_mappings.mapper import Posting, map_posting
from tailorcv.infrastructure.web_fetch.board_mappings.page_posting import page_posting_declaration
from tailorcv.infrastructure.web_fetch.board_reads import page_posting_detail

_ROW = {"id": "42", "url": "https://jobs.acme.example/postings/42", "places": ["Leeds, UK"]}
_PAGE = """<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@type":"JobPosting","title":"Lab Analyst",
"description":"<p>Run assays.</p>","datePosted":"2026-10-01",
"hiringOrganization":{"@type":"Organization","name":"Acme"}%s}
</script></head><body></body></html>"""
_PLACE = (
    ',"jobLocation":{"@type":"Place","address":{"@type":"PostalAddress",'
    '"addressLocality":"Boston","addressRegion":"MA","addressCountry":"US"}}'
)


class _Pages:
    def __init__(self, page: str) -> None:
        self.page = page
        self.requested: list[str] = []

    def get_text(self, url: str, *, max_bytes: int) -> str:
        self.requested.append(url)
        return f"{url}\n{self.page}"


def _location(detail: dict[str, Any] | None, *, row_places: bool) -> str:
    declaration = page_posting_declaration("acme", row_places=row_places)
    return map_posting(declaration, Posting({"row": _ROW, "detail": detail or {}})).location


@pytest.mark.parametrize(("place", "expected"), [(_PLACE, "Boston"), ("", "Leeds, UK")])
def test_row_places_fill_in_only_when_the_page_states_none(place: str, expected: str) -> None:
    pages = _Pages(_PAGE % place)
    detail = page_posting_detail(pages)(dict(_ROW))  # type: ignore[arg-type]

    assert pages.requested == [_ROW["url"]]
    assert detail is not None
    assert detail["title"] == "Lab Analyst"
    assert expected in _location(dict(detail), row_places=True)


def test_page_places_alone_are_the_default() -> None:
    assert _location({"places": []}, row_places=False) == ""
