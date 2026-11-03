"""Zoho Recruit's careers page, which embeds every published opening as one JSON field."""

from __future__ import annotations

import html
import json
import re
from typing import Any

from clearscout.infrastructure.web_fetch.board_mappings.mapper import (
    BoardDeclaration,
    Posting,
    stated,
    stated_text,
)
from clearscout.infrastructure.web_fetch.listing_facts import ArrangementSignal

# The page's hidden ``jobs`` input; its value is the opening list, entity-escaped JSON.
_JOBS_INPUT = re.compile(r"""<input\b[^>]*\bid\s*=\s*["']jobs["'][^>]*>""", re.IGNORECASE)
_VALUE = re.compile(r'\bvalue\s*=\s*"([^"]*)"')


def zoho_board_url(host: str, portal: str) -> str:
    """Return the careers page of one Zoho Recruit portal."""
    return f"https://{host}/jobs/{portal}"


def zoho_openings(page: str) -> list[dict[str, Any]] | None:
    """Return the published openings the page embeds, or None when it embeds no list."""
    field = _JOBS_INPUT.search(page)
    value = _VALUE.search(field.group(0)) if field is not None else None
    if value is None:
        return None
    try:
        openings = json.loads(html.unescape(value.group(1)))
    except json.JSONDecodeError:
        return None
    if not isinstance(openings, list):
        return None
    return [job for job in openings if isinstance(job, dict) and job.get("Publish") is not False]


def _url(posting: Posting) -> str:
    job = stated_text(posting.record, ("id",))
    host, portal = posting.groups.get("host", ""), posting.groups.get("portal", "")
    return f"{zoho_board_url(host, portal)}/{job}" if host and portal and job else ""


def _places(posting: Posting) -> list[str]:
    """Return the one place the opening's city, state and country fields name together."""
    parts = (stated_text(posting.record, (key,)) for key in ("City", "State", "Country"))
    place = ", ".join(dict.fromkeys(part for part in parts if part))
    return [place] if place else []


def _arrangement(posting: Posting) -> ArrangementSignal:
    # An unset flag is every office opening's default, so only a set one states anything.
    remote = stated(posting.record, ("Remote_Job",)) is True
    return ArrangementSignal(value="remote" if remote else None, provenance_suffix="Remote_Job")


ZOHO_MAPPING = BoardDeclaration(
    platform="zoho",
    title=lambda posting: (
        stated_text(posting.record, ("Posting_Title",))
        or stated_text(posting.record, ("Job_Opening_Name",))
    ),
    url=_url,
    places=_places,
    # One city field cannot list the other places a multi-site opening is in.
    locations_complete=lambda _posting: False,
    requisition_id=("id",),
    employment_types=("Job_Type",),
    arrangement=_arrangement,
)
