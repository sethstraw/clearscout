"""Greenhouse's job record, as its board listing and its single-job endpoint both state it."""

from __future__ import annotations

from tailorcv.domain.staged_facts import PostedDateGrade
from tailorcv.infrastructure.web_fetch.board_mappings.mapper import (
    BoardDeclaration,
    Posting,
    plain_description,
    posted_on,
    stated,
    stated_text,
)
from tailorcv.infrastructure.web_fetch.listing_facts import (
    ArrangementSignal,
    extended_locations,
    linkedin_arrangement_signal,
    location_display,
    remote_checked_arrangement,
    stated_place,
)
from tailorcv.shared.text import sanitize

_SUMMARY = ("location", "name")


def _names_the_employer(office: str, employer: str) -> bool:
    """Return whether an office name only repeats the employer's own name."""
    named, owner = office.casefold(), employer.casefold()
    return bool(owner) and (named.startswith(owner) or set(named.split()) <= set(owner.split()))


def _offices(posting: Posting) -> list[str]:
    """Return each office name that states a place; employers also name offices after themselves."""
    employer = stated_text(posting.record, ("company_name",)) or posting.employer
    return [
        name
        for office in stated(posting.record, ("offices",)) or []
        if (name := stated_text(office, ("name",))) and not _names_the_employer(name, employer)
    ]


def _places(posting: Posting) -> list[str] | str:
    """Return the places the job's summary states, its offices adding any the summary omits."""
    # Every entity ends in ";", so the summary is decoded before it is split into places.
    summary = sanitize(stated_text(posting.record, _SUMMARY))
    named = [part.strip() for part in stated_place(summary).split(";") if part.strip()]
    return list(extended_locations(named, _offices(posting))) or summary


def _location(posting: Posting) -> str:
    return location_display(sanitize(stated_text(posting.record, _SUMMARY)), _places(posting))


def _work_setting(posting: Posting) -> str:
    """Return the employer-defined "Work Setting" metadata value, or "" when not configured."""
    for entry in stated(posting.record, ("metadata",)) or []:
        if stated_text(entry, ("name",)) == "Work Setting":
            value = stated(entry, ("value",))
            return value.strip() if isinstance(value, str) else ""
    return ""


def _arrangement(posting: Posting) -> ArrangementSignal:
    """Read "Work Setting", else a #LI content tag; either is checked against a remote offer."""
    content = plain_description(stated(posting.record, ("content",)))
    setting = _work_setting(posting)
    if setting:
        return remote_checked_arrangement(setting, _places(posting), content)
    if not content:
        return ArrangementSignal()
    tagged = linkedin_arrangement_signal(content)
    if tagged.value is None:
        return tagged
    return remote_checked_arrangement(tagged.value, _places(posting), content)


GREENHOUSE_MAPPING = BoardDeclaration(
    platform="greenhouse",
    title=("title",),
    url=("absolute_url",),
    requisition_id=("id",),
    employer=("company_name",),
    places=_places,
    location=_location,
    description=(("content",),),
    arrangement=_arrangement,
    heading=(("title",), _SUMMARY),
    # updated_at moves on every edit; first_published stays put.
    posted=posted_on(("first_published",), PostedDateGrade.FIRST_POSTED),
)
