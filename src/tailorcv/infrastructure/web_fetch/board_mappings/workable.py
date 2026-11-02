"""Workable's public widget feed item."""

from __future__ import annotations

from tailorcv.infrastructure.web_fetch.board_mappings.mapper import (
    BoardDeclaration,
    Posting,
    stated,
    stated_text,
)
from tailorcv.infrastructure.web_fetch.listing_facts import ArrangementSignal


def _url(posting: Posting) -> str:
    """Prefer the canonical posting URL, else the direct application URL."""
    return stated_text(posting.record, ("url",)) or stated_text(
        posting.record, ("application_url",)
    )


def _place(record: object, region_key: str) -> str:
    return ", ".join(
        part
        for part in (
            stated_text(record, ("city",)),
            stated_text(record, (region_key,)),
            stated_text(record, ("country",)),
        )
        if part
    )


def _places(posting: Posting) -> list[str]:
    """Return each place a job's visible ``locations`` entries state, else its top-level one.

    When every entry is hidden, only the countries are kept, never a hidden city or region.
    """
    entries = stated(posting.record, ("locations",))
    if not isinstance(entries, list) or not entries:
        return [place] if (place := _place(posting.record, "state")) else []
    listed = [entry for entry in entries if isinstance(entry, dict)]
    visible = [entry for entry in listed if entry.get("hidden") is not True]
    if visible:
        stated_places = [_place(entry, "region") for entry in visible]
    else:
        stated_places = [stated_text(entry, ("country",)) for entry in listed]
        if not any(stated_places):
            stated_places = [stated_text(posting.record, ("country",))]
    return [place for place in dict.fromkeys(stated_places) if place]


def _arrangement(posting: Posting) -> ArrangementSignal:
    """Read ``telecommuting: true`` as remote; any other value states no arrangement."""
    if stated(posting.record, ("telecommuting",)) is not True:
        return ArrangementSignal()
    return ArrangementSignal(value="remote")


WORKABLE_MAPPING = BoardDeclaration(
    platform="workable",
    title=("title",),
    url=_url,
    places=_places,
    requisition_id=("shortcode",),
    arrangement=_arrangement,
    description=(("description",),),
)
