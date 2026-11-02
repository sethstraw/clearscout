"""Getro's job record, as a talent-network board page embeds it for each portfolio opening."""

from __future__ import annotations

from tailorcv.infrastructure.web_fetch.board_mappings.mapper import (
    BoardDeclaration,
    Posting,
    stated,
    stated_text,
)
from tailorcv.infrastructure.web_fetch.listing_facts import ArrangementSignal


def _places(posting: Posting) -> list[str]:
    entries = stated(posting.record, ("locations",))
    if not isinstance(entries, list):
        return []
    return [place for place in dict.fromkeys(str(item).strip() for item in entries) if place]


def _arrangement(posting: Posting) -> ArrangementSignal:
    """Read workMode: remote as remote; any other value states no arrangement."""
    if stated_text(posting.record, ("workMode",)).casefold() != "remote":
        return ArrangementSignal()
    return ArrangementSignal(value="remote", provenance_suffix="workMode")


# The network hosting the board is not the employer; each job names its portfolio company.
GETRO_MAPPING = BoardDeclaration(
    platform="getro",
    title=("title",),
    url=("url",),
    requisition_id=("id",),
    employer=("organization", "name"),
    places=_places,
    location=lambda posting: ", ".join(_places(posting)),
    arrangement=_arrangement,
    prefer_publisher_employer=True,
)
