"""Pinpoint's public postings feed item."""

from __future__ import annotations

from clearscout.infrastructure.web_fetch.board_mappings.mapper import (
    BoardDeclaration,
    Posting,
    pay_period,
    pay_range,
    stated,
    stated_text,
)
from clearscout.infrastructure.web_fetch.listing_facts import (
    ArrangementSignal,
    remote_checked_arrangement,
)

_ARRANGEMENTS = {"onsite": "onsite", "hybrid": "hybrid", "remote": "remote"}
_PLACE = ("location", "name")
_DESCRIPTION = (
    ("description",),
    ("key_responsibilities",),
    ("skills_knowledge_expertise",),
    ("benefits",),
)


def _arrangement(posting: Posting) -> ArrangementSignal:
    """Read the arrangement Pinpoint states as workplace_type; an unknown value is unresolved."""
    stated_type = stated_text(posting.record, ("workplace_type",)).casefold()
    if not stated_type:
        return ArrangementSignal()
    value = _ARRANGEMENTS.get(stated_type)
    if value is None:
        return ArrangementSignal(unresolved=(f"workplace_type: {stated_type}",))
    body = [stated(posting.record, path) for path in _DESCRIPTION]
    return remote_checked_arrangement(value, stated(posting.record, _PLACE), body)


def _compensation(posting: Posting) -> str | None:
    """Return the stated range only when the posting shows it; a hidden range is still filled in."""
    record = posting.record
    if stated(record, ("compensation_visible",)) is not True:
        return None
    return pay_range(
        stated(record, ("compensation_currency",)),
        stated(record, ("compensation_minimum",)),
        stated(record, ("compensation_maximum",)),
        pay_period(stated(record, ("compensation_frequency",))),
    )


def _requisition_id(posting: Posting) -> str:
    return stated_text(posting.record, ("job", "requisition_id")) or stated_text(
        posting.record, ("id",)
    )


PINPOINT_MAPPING = BoardDeclaration(
    platform="pinpoint",
    title=("title",),
    url=("url",),
    places=_PLACE,
    requisition_id=_requisition_id,
    employment_types=("employment_type_text",),
    compensation=_compensation,
    arrangement=_arrangement,
    description=_DESCRIPTION,
)
