"""Teamtailor's posting record, as its public per-tenant RSS feed states it."""

from __future__ import annotations

from xml.etree.ElementTree import Element

from clearscout.domain.staged_facts import PostedDateGrade
from clearscout.infrastructure.web_fetch.board_mappings.mapper import (
    BoardDeclaration,
    Posting,
    posted_on,
    stated,
    stated_text,
)
from clearscout.infrastructure.web_fetch.listing_facts import (
    ArrangementSignal,
    remote_checked_arrangement,
)

# Teamtailor publishes each posting's locations under this custom RSS namespace.
TEAMTAILOR_NAMESPACE = "https://teamtailor.com/locations"


def _places(posting: Posting) -> list[str]:
    """Return every place a posting's namespaced <locations> block names."""
    record = posting.record
    if not isinstance(record, Element):
        return []
    return [
        text
        for node in record.findall(
            f"{{{TEAMTAILOR_NAMESPACE}}}locations/{{{TEAMTAILOR_NAMESPACE}}}location/"
            f"{{{TEAMTAILOR_NAMESPACE}}}name"
        )
        if (text := str(node.text or "").strip())
    ]


_ARRANGEMENTS = {"hybrid": "hybrid", "fully": "remote"}


def _arrangement(posting: Posting) -> ArrangementSignal:
    """Read the arrangement the feed states in each item's remoteStatus element."""
    status = stated_text(posting.record, ("remoteStatus",)).casefold()
    # "none" is the unset default, which region-wide roles carry too; it states no attendance.
    if status in {"", "none"}:
        return ArrangementSignal()
    value = _ARRANGEMENTS.get(status)
    if value is None:
        return ArrangementSignal(unresolved=(f"remoteStatus: {status}",))
    return remote_checked_arrangement(
        value, _places(posting), stated(posting.record, ("description",))
    )


TEAMTAILOR_MAPPING = BoardDeclaration(
    platform="teamtailor",
    title=("title",),
    url=("link",),
    requisition_id=("guid",),
    places=_places,
    arrangement=_arrangement,
    description=(("description",),),
    posted=posted_on(("pubDate",), PostedDateGrade.FIRST_POSTED),
)
