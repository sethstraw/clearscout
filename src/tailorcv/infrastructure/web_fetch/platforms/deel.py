"""Deel's own careers page, whose Next flight data lists the postings it hosts on jobs.deel.com."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from tailorcv.infrastructure.web_fetch.board_mappings.mapper import stated, stated_text
from tailorcv.infrastructure.web_fetch.board_mappings.page_posting import page_posting_declaration
from tailorcv.infrastructure.web_fetch.board_platform import HOST_START, BoardPlatform
from tailorcv.infrastructure.web_fetch.board_reads import (
    MAX_API_RESPONSE_BYTES,
    job_page_rows,
    listed_page_continuations,
    listed_page_read,
    page_markup,
)

if TYPE_CHECKING:
    from tailorcv.application.discovery.source_reading import SourceRead
    from tailorcv.infrastructure.web_fetch.board_platform import BoardTransport

# The page is the employer's own site, so its markup names the board rather than its URL.
DEEL_CAREERS = re.compile(
    r"""<link\b[^>]*\brel=["']canonical["'][^>]*\bhref=["']https://www\.deel\.com/careers/?["']""",
    re.IGNORECASE,
)
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# jobs.deel.com robots refuses each posting's /application page, never its overview.
DEEL_JOB = re.compile(
    HOST_START + rf"jobs\.deel\.com/deel/job-details/(?P<job>{_UUID})/overview(?![\w-])",
    re.IGNORECASE,
)
_ROOT = "https://www.deel.com/careers/"
_FLIGHT = re.compile(r'self\.__next_f\.push\(\[1,("(?:.*?)")\]\)</script>', re.DOTALL)
_LISTING = '"__component":"product.career-job-listing","jobs":'
_MAPPING = page_posting_declaration(
    "deel",
    row_places=True,
    # The overview's JobPosting states no pay; the list's tier summary is the published range.
    compensation=lambda posting: (
        stated_text(posting.record, ("row", "pay"))
        or stated(posting.record, ("detail", "compensation"))
        or None
    ),
)


def _text(value: Any) -> str:
    """Return a flight string as text: "$$" escapes a literal "$", any other "$" is a reference."""
    if not isinstance(value, str):
        return ""
    if value.startswith("$$"):
        return value[1:].strip()
    return "" if value.startswith("$") else value.strip()


def _openings(page: str) -> list[dict[str, Any]]:
    """Return each listed posting's id, overview address, places and any published pay."""
    flight = "".join(json.loads(chunk) for chunk in _FLIGHT.findall(page))
    start = flight.find(_LISTING)
    if start < 0:
        raise ValueError("Deel careers page streamed no job listing")
    jobs, _end = json.JSONDecoder().raw_decode(flight, start + len(_LISTING))
    if not isinstance(jobs, list):
        raise ValueError("Deel careers job listing was not a list")
    rows = []
    for job in jobs:
        posting = job.get("attributes") if isinstance(job, dict) else None
        if not isinstance(posting, dict) or posting.get("is_listed") is not True:
            continue
        identity = _text(posting.get("ashby_id"))
        if re.fullmatch(_UUID, identity) is None:
            raise ValueError("Deel careers job listing named a posting without its id")
        shown = posting.get("should_display_compensation_on_Job_board") is True
        rows.append(
            {
                "id": identity,
                "url": f"https://jobs.deel.com/deel/job-details/{identity}/overview",
                "places": [
                    place for item in posting.get("all_locations") or () if (place := _text(item))
                ],
                "pay": _text(posting.get("compensation_tier_summary")) if shown else "",
            }
        )
    return rows


def _read(fetcher: BoardTransport, employer: str, _groups: dict[str, str], url: str) -> SourceRead:
    """Read every posting the careers page lists and each overview page's JobPosting."""
    if urlsplit(url).hostname != "www.deel.com":
        raise ValueError("A Deel careers canonical link was served from another host")
    page = page_markup(fetcher.get_text(_ROOT, max_bytes=MAX_API_RESPONSE_BYTES))
    return listed_page_read(
        fetcher,
        employer,
        _openings(page),
        mapping=_MAPPING,
        unread_note="The Deel careers page listed no openings.",
        source_url=url,
    )


PLATFORM = BoardPlatform(
    name="deel",
    label="Deel careers",
    board_patterns=(DEEL_CAREERS,),
    adapter=_read,
    continuations=listed_page_continuations(_MAPPING, job_page_rows(DEEL_JOB)),
)
