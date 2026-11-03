"""Paylocity's public V2 feed item and the job entry its board page embeds.

The feed names fields in camelCase and the page in PascalCase, so one record may
carry both: the feed's where the feed lists the job, the page's otherwise.
"""

from __future__ import annotations

import json
import re
from typing import Any

from clearscout.infrastructure.web_fetch.board_mappings.mapper import (
    BoardDeclaration,
    Posting,
    stated,
    stated_text,
)
from clearscout.infrastructure.web_fetch.listing_facts import (
    ArrangementSignal,
    remote_checked_arrangement,
)

_HOST = "https://recruiting.paylocity.com"
_PAGE_DATA = re.compile(r"window\.pageData\s*=\s*")


def paylocity_board_page_url(tenant: str) -> str:
    """Return the company's own board page, which embeds every public opening."""
    return f"{_HOST}/Recruiting/Jobs/All/{tenant}"


def paylocity_feed_url(tenant: str) -> str:
    """Return the V2 syndication feed, which carries full bodies for the jobs it lists."""
    return f"{_HOST}/recruiting/v2/api/feed/jobs/{tenant}"


def paylocity_page_jobs(markup: str) -> list[Any] | None:
    """Return the ``pageData.Jobs`` list a board page assigns, or None when it states none."""
    assigned = _PAGE_DATA.search(markup)
    if assigned is None:
        return None
    try:
        page_data, _end = json.JSONDecoder().raw_decode(markup, assigned.end())
    except json.JSONDecodeError:
        return None
    jobs = page_data.get("Jobs") if isinstance(page_data, dict) else None
    return jobs if isinstance(jobs, list) else None


def _text(posting: Posting, *paths: tuple[str, ...]) -> str:
    return next((text for path in paths if (text := stated_text(posting.record, path))), "")


def _title(posting: Posting) -> str:
    return _text(posting, ("title",), ("JobTitle",))


def _url(posting: Posting) -> str:
    # Identity is the address, so a job keeps one whether or not the feed lists it.
    job_id = _text(posting, ("JobId",), ("jobId",))
    if job_id:
        return f"{_HOST}/Recruiting/Jobs/Details/{job_id}"
    return _text(posting, ("displayUrl",), ("applyUrl",))


def _places(posting: Posting) -> str:
    """Render Paylocity's structured location the way the board's own feed does."""
    displayed = _text(posting, ("jobLocation", "locationDisplayName"), ("LocationName",))
    geographic = ", ".join(
        part
        for part in (
            _text(posting, ("jobLocation", "city"), ("JobLocation", "City")),
            _text(posting, ("jobLocation", "state"), ("JobLocation", "State")),
        )
        if part
    )
    return "; ".join(dict.fromkeys(part for part in (displayed, geographic) if part))


def _arrangement(posting: Posting) -> ArrangementSignal:
    """Read the page's remote flag; a false flag leaves the arrangement unstated."""
    if stated(posting.record, ("IsRemote",)) is not True:
        return ArrangementSignal()
    body = [stated(posting.record, ("description",)), stated(posting.record, ("requirements",))]
    return remote_checked_arrangement("remote", _places(posting), body)


def _requisition_id(posting: Posting) -> str:
    return _text(posting, ("jobCode",), ("jobId",), ("JobId",))


PAYLOCITY_MAPPING = BoardDeclaration(
    platform="paylocity",
    title=_title,
    url=_url,
    employer=("companyName",),
    places=_places,
    requisition_id=_requisition_id,
    description=(("description",), ("requirements",)),
    arrangement=_arrangement,
)
