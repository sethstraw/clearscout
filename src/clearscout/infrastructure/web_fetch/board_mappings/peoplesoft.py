"""PeopleSoft Candidate Gateway: the job grid a search page renders, and each posting page."""

from __future__ import annotations

import re
from collections.abc import Mapping
from html.parser import HTMLParser
from typing import Any

from clearscout.infrastructure.web_fetch.board_mappings.mapper import plain_description
from clearscout.infrastructure.web_fetch.board_mappings.page_posting import (
    page_posting_declaration,
)
from clearscout.infrastructure.web_fetch.ingest.html_blocks import html_blocks

# A grid cell's id names its field and its row, as in SCH_JOB_TITLE$3.
_CELL = re.compile(r"(?P<field>[A-Z][A-Z0-9_]*)\$(?P<row>\d+)")
_COUNT = re.compile(r"(?:<b>)?(?P<count>\d+)(?:</b>)?\s+(?:jobs|matches)\s+found", re.IGNORECASE)
# The grid's "next set" action; a classic grid drops it on its last set, a Fluid one keeps it.
PEOPLESOFT_NEXT_SET = "HRS_AGNT_RSLT_I$hdown$0"
# A classic row names its opening only in the link text, "Title - 79142".
_CLASSIC_LINK = re.compile(r"(?P<title>.*\S)\s+-\s+(?P<job>\d+)", re.DOTALL)
_ATTRIBUTE = re.compile(r"(?P<label>[A-Za-z][\w ]*?)\s*:\s*(?P<value>[^|]*?)\s*(?:\||$)")
_CONTENT_FRAME = "ptifrmtgtframe"


class _GatewayPage(HTMLParser):
    """The search form a gateway page posts back, and the content frame a portal wraps."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.action = ""
        self.fields: dict[str, str] = {}
        self.frame = ""
        self._in_form = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        stated = {key: value or "" for key, value in attrs}
        if tag == "form" and stated.get("name") == "win0":
            self._in_form, self.action = True, stated.get("action", "")
        elif tag == "input" and self._in_form and stated.get("type") == "hidden":
            if stated.get("name"):
                self.fields[stated["name"]] = stated.get("value", "")
        elif tag == "iframe" and stated.get("id") == _CONTENT_FRAME:
            self.frame = stated.get("src", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._in_form = False


def gateway_page(page: str) -> _GatewayPage:
    """Return the form action, hidden fields, and content frame one gateway page states."""
    parsed = _GatewayPage()
    parsed.feed(page)
    parsed.close()
    return parsed


def _by_id(page: str, tags: tuple[str, ...]) -> dict[str, str]:
    return {
        block.attrs["id"]: block.inner
        for tag in tags
        for block in html_blocks(page, tag)
        if block.attrs.get("id")
    }


def _text(markup: str | None) -> str:
    return " ".join(plain_description(markup or "").split())


def peoplesoft_openings(page: str) -> list[dict[str, Any]]:
    """Return each grid row's opening id, title, and place, in grid order."""
    rows: dict[int, dict[str, str]] = {}
    for identity, inner in _by_id(page, ("span", "a")).items():
        cell = _CELL.fullmatch(identity)
        if cell is not None:
            rows.setdefault(int(cell["row"]), {})[cell["field"]] = _text(inner)
    openings = []
    for _row, cells in sorted(rows.items()):
        job, title = cells.get("HRS_APP_JBSCH_I_HRS_JOB_OPENING_ID", ""), cells.get("SCH_JOB_TITLE")
        link = _CLASSIC_LINK.fullmatch(cells.get("POSTINGLINK", ""))
        if not job and link is not None:
            job, title = link["job"], link["title"]
        if not re.fullmatch(r"\d+", job) or not title:
            continue
        attributes = {
            found["label"].casefold(): found["value"]
            for found in _ATTRIBUTE.finditer(cells.get("JOBATTRIBUTES", ""))
        }
        place = cells.get("LOCATION") or attributes.get("location", "")
        openings.append({"id": job, "title": title, "places": [place] if place else []})
    return openings


def peoplesoft_total(page: str) -> int | None:
    """Return the "N jobs found" count the search states, or None when it states none."""
    found = _COUNT.search(page)
    return int(found["count"]) if found else None


def peoplesoft_posting(page: str) -> Mapping[str, Any] | None:
    """Return a posting page's title, place, schedule, and its titled description sections."""
    spans = _by_id(page, ("span", "div"))
    title = _text(spans.get("HRS_SCH_WRK2_POSTING_TITLE"))
    sections = []
    for number in range(100):
        body = spans.get(f"HRS_SCH_PSTDSC_DESCRLONG${number}")
        if body is None:
            break
        heading = _text(
            spans.get(f"HRS_SCH_PSTDSC_DESCR${number}")
            or spans.get(f"HRS_SCH_WRK_DESCR100${number}lbl")
        )
        if plain_description(body).strip():
            sections.append((f"<h2>{heading}</h2>" if heading else "") + body)
    if not title or not sections:
        return None
    place = _text(spans.get("HRS_SCH_WRK_HRS_DESCRLONG"))
    schedule = [
        value
        for name in ("HRS_SCH_WRK_HRS_FULL_PART_TIME", "HRS_SCH_WRK_HRS_REG_TEMP")
        if (value := _text(spans.get(name)))
    ]
    return {
        "title": title,
        "description": "\n".join(sections),
        "places": [place] if place else [],
        "places_complete": False,
        "employment_types": schedule,
    }


# The grid row's place is the one the search filters on, so it leads.
PEOPLESOFT_MAPPING = page_posting_declaration(
    "peoplesoft",
    places=lambda posting: (
        posting.record["row"].get("places") or posting.record["detail"].get("places", [])
    ),
)
