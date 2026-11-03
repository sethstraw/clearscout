"""Recruitee's public offers feed item."""

from __future__ import annotations

from collections.abc import Mapping

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


def _url(posting: Posting) -> str:
    """Prefer the careers-site URL, else the direct apply URL."""
    return stated_text(posting.record, ("careers_url",)) or stated_text(
        posting.record, ("careers_apply_url",)
    )


def _place(entry: Mapping[str, object]) -> str:
    named = stated_text(entry, ("name",))
    parts = (stated_text(entry, (key,)) for key in ("city", "state", "country"))
    return named or ", ".join(part for part in parts if part)


def _places(posting: Posting) -> list[str]:
    """Return every location entry the offer states, else its one-line location."""
    entries = stated(posting.record, ("locations",))
    places = [_place(entry) for entry in entries or [] if isinstance(entry, Mapping)]
    flat = stated_text(posting.record, ("location",))
    return [place for place in dict.fromkeys([*places, *([] if any(places) else [flat])]) if place]


def _location(posting: Posting) -> str:
    return stated_text(posting.record, ("location",)) or next(iter(_places(posting)), "")


def _arrangement(posting: Posting) -> ArrangementSignal:
    """Read the remote and hybrid flags; an offer setting neither states no arrangement."""
    flagged = [key for key in ("remote", "hybrid") if stated(posting.record, (key,)) is True]
    if not flagged:
        return ArrangementSignal()
    return remote_checked_arrangement(flagged, _places(posting), "")


def _compensation(posting: Posting) -> str | None:
    pay = stated(posting.record, ("salary",))
    if not isinstance(pay, Mapping):
        return None
    return pay_range(
        pay.get("currency"), pay.get("min"), pay.get("max"), pay_period(pay.get("period"))
    )


RECRUITEE_MAPPING = BoardDeclaration(
    platform="recruitee",
    title=("title",),
    url=_url,
    location=_location,
    places=_places,
    requisition_id=("id",),
    arrangement=_arrangement,
    compensation=_compensation,
)
