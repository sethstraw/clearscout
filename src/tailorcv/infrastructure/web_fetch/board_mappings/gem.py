"""Gem's job post, which its board list and single-post endpoint state alike."""

from __future__ import annotations

from tailorcv.infrastructure.web_fetch.board_mappings.mapper import (
    BoardDeclaration,
    Posting,
    plain_description,
    stated,
    stated_text,
)
from tailorcv.infrastructure.web_fetch.listing_facts import (
    ArrangementSignal,
    extended_locations,
    location_display,
    remote_checked_arrangement,
)

_LOCATION = ("location", "name")
_ARRANGEMENTS = {"remote": "remote", "hybrid": "hybrid", "in_office": "onsite"}


def gem_post_url(org: str, post_id: str = "") -> str:
    """Return the public board API address for an org's posts, or for one post."""
    return f"https://api.gem.com/job_board/v0/{org}/job_posts/{post_id}"


def _places(posting: Posting) -> list[str]:
    """Return the post's location, then each office location it does not already name."""
    offices = [
        stated_text(office, ("location", "name"))
        for office in stated(posting.record, ("offices",)) or []
    ]
    return list(extended_locations([stated_text(posting.record, _LOCATION)], offices))


def _location(posting: Posting) -> str:
    return location_display(stated_text(posting.record, _LOCATION), _places(posting))


def _arrangement(posting: Posting) -> ArrangementSignal:
    """Read the location_type enum, checked against a remote offer in the body."""
    stated_type = stated_text(posting.record, ("location_type",)).casefold()
    if not stated_type:
        return ArrangementSignal()
    value = _ARRANGEMENTS.get(stated_type)
    if value is None:
        return ArrangementSignal(unresolved=(f"location_type: {stated_type}",))
    return remote_checked_arrangement(
        value, _places(posting), plain_description(stated(posting.record, ("content",)))
    )


GEM_MAPPING = BoardDeclaration(
    platform="gem",
    title=("title",),
    url=("absolute_url",),
    requisition_id=("id",),
    places=_places,
    location=_location,
    description=(("content",),),
    arrangement=_arrangement,
    employment_types=("employment_type",),
)
