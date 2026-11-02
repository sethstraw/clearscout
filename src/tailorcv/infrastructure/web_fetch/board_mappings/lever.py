"""Lever's posting record, as its board feed and its single-posting endpoint both state it."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tailorcv.domain.countries import territory_name
from tailorcv.domain.staged_facts import PostedDateGrade
from tailorcv.infrastructure.web_fetch.board_mappings.mapper import (
    BoardDeclaration,
    Posting,
    pay_period,
    pay_range,
    plain_description,
    posted_on,
    stated,
    stated_text,
)
from tailorcv.infrastructure.web_fetch.listing_facts import (
    ArrangementSignal,
    location_display,
    remote_checked_arrangement,
    values,
)

_SUMMARY = ("categories", "location")


def _places(posting: Posting) -> list[str]:
    """Return every stated place, falling back to the one-line summary."""
    every = values(stated(posting.record, ("categories", "allLocations")))
    return list(every or values(stated_text(posting.record, _SUMMARY)))


def _located(posting: Posting) -> list[str]:
    country = territory_name(stated_text(posting.record, ("country",)))
    return [*_places(posting), country] if country else _places(posting)


def _location(posting: Posting) -> str:
    return location_display(stated_text(posting.record, _SUMMARY), _places(posting))


def _sections(posting: Posting) -> str:
    """Join every public description field, list sections included, as published markup."""
    record = posting.record
    sections: list[Any] = [
        stated(record, ("descriptionPlain",)) or stated(record, ("description",))
    ]
    for item in stated(record, ("lists",)) or []:
        sections.extend((stated(item, ("text",)), stated(item, ("content",))))
    sections.append(stated(record, ("additionalPlain",)))
    return "\n\n".join(str(section) for section in sections if section)


def _arrangement(posting: Posting) -> ArrangementSignal:
    # The title and summary are checked too: "(Remote)" in a title contradicts a hybrid value.
    checked = "\n\n".join(
        (
            stated_text(posting.record, ("text",)),
            stated_text(posting.record, _SUMMARY),
            plain_description(_sections(posting)),
        )
    )
    return remote_checked_arrangement(
        stated(posting.record, ("workplaceType",)), _places(posting), checked
    )


def _employer(posting: Posting) -> str:
    record = posting.record
    return (
        stated_text(record, ("company",))
        or stated_text(record, ("companyName",))
        or posting.employer
    )


def _compensation(posting: Posting) -> str | None:
    """Return the structured salary range; its interval reads like "per-year-salary"."""
    pay = stated(posting.record, ("salaryRange",))
    if not isinstance(pay, Mapping):
        return None
    return pay_range(
        pay.get("currency"), pay.get("min"), pay.get("max"), pay_period(pay.get("interval"))
    )


LEVER_MAPPING = BoardDeclaration(
    platform="lever",
    title=("text",),
    url=("hostedUrl",),
    requisition_id=("id",),
    employer=_employer,
    places=_located,
    location=_location,
    description=_sections,
    arrangement=_arrangement,
    employment_types=("categories", "commitment"),
    compensation=_compensation,
    heading=(("text",), _SUMMARY),
    posted=posted_on(("createdAt",), PostedDateGrade.FIRST_POSTED),
)
