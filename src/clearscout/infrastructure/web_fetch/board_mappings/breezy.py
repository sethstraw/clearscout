"""Breezy HR's public position feed row joined with its posting page's declared body."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clearscout.domain.countries import territory_name
from clearscout.infrastructure.web_fetch.board_mappings.mapper import (
    BoardDeclaration,
    Posting,
    stated,
    stated_text,
)
from clearscout.infrastructure.web_fetch.ingest.job_posting_markup import job_postings
from clearscout.infrastructure.web_fetch.listing_facts import (
    ArrangementSignal,
    extended_locations,
    remote_checked_arrangement,
)

_ARRANGEMENTS = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "on_site": "onsite"}
_LOCATION = ("row", "location")
_DESCRIPTION = (("detail", "description"),)


def breezy_positions_url(tenant: str) -> str:
    """Return the public feed listing every published position of one Breezy tenant."""
    return f"https://{tenant}.breezy.hr/json"


def breezy_page_url(tenant: str, row: Any) -> str:
    """Return the posting page a feed row names, or the one its friendly id addresses."""
    stated_url = stated_text(row, ("url",))
    if stated_url:
        return stated_url
    friendly_id = stated_text(row, ("friendly_id",)) or stated_text(row, ("id",))
    return f"https://{tenant}.breezy.hr/p/{friendly_id}" if tenant and friendly_id else ""


def breezy_page_posting(page: str) -> Mapping[str, Any]:
    """Return the body a posting page declares; a page declaring none states no body."""
    declared = next(iter(job_postings(page)), None)
    return {"description": declared.description} if declared is not None else {}


def _url(posting: Posting) -> str:
    return breezy_page_url(posting.groups.get("tenant", ""), stated(posting.record, ("row",)))


def _places(posting: Posting) -> list[str]:
    """Return each place the row's locations list names, else its one location, then countries."""
    listed = stated(posting.record, ("row", "locations"))
    entries = [entry for entry in listed or [] if isinstance(entry, Mapping)] or [
        stated(posting.record, _LOCATION)
    ]
    countries = [
        territory_name(stated_text(entry, ("country", "id")))
        or stated_text(entry, ("country", "name"))
        for entry in entries
    ]
    names = [stated_text(entry, ("name",)) for entry in entries]
    return list(extended_locations(names, countries))


def _arrangement(posting: Posting) -> ArrangementSignal:
    """Read remote_details.value, else a set is_remote flag; an unknown value is unresolved."""
    stated_value = stated_text(posting.record, (*_LOCATION, "remote_details", "value")).casefold()
    if not stated_value:
        # An unset flag is the default every office posting carries, so it states nothing.
        if stated(posting.record, (*_LOCATION, "is_remote")) is not True:
            return ArrangementSignal()
        stated_value = "remote"
    value = _ARRANGEMENTS.get(stated_value)
    if value is None:
        return ArrangementSignal(unresolved=(f"remote_details: {stated_value}",))
    body = [stated(posting.record, path) for path in _DESCRIPTION]
    return remote_checked_arrangement(value, _places(posting), body)


BREEZY_MAPPING = BoardDeclaration(
    platform="breezy",
    title=("row", "name"),
    url=_url,
    employer=("row", "company", "name"),
    places=_places,
    requisition_id=("row", "id"),
    employment_types=("row", "type", "name"),
    compensation=("row", "salary"),
    arrangement=_arrangement,
    description=_DESCRIPTION,
)
