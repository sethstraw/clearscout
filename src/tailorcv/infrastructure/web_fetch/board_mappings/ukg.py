"""UKG Pro Recruiting's opportunity, as its posting page embeds it."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from tailorcv.infrastructure.web_fetch.board_mappings.mapper import (
    BoardDeclaration,
    Posting,
    stated,
    stated_text,
)

# UKG's robots.txt refuses the board's search path, so a post is read only from its own page.
UKG_BOARD = re.compile(
    r"(?:^|//)(?P<host>recruiting\d*\.ultipro\.(?:com|ca)|[a-z0-9-]+\.rec\.pro\.ukg\.net)"
    r"/(?P<tenant>[A-Za-z0-9_-]+)/JobBoard/"
    r"(?P<board>[0-9a-f-]{36})(?=/|\?|#|$)",
    re.IGNORECASE,
)
UKG_OPPORTUNITY = re.compile(
    UKG_BOARD.pattern + r"/OpportunityDetail\?(?:[^#]*&)?opportunityId=(?P<job>[0-9a-f-]{36})",
    re.IGNORECASE,
)
_EMBEDDED = re.compile(r"CandidateOpportunityDetail\(\s*")


def ukg_opportunity_url(match: re.Match[str]) -> str:
    """Return the posting page an opportunity match names, without its other query fields."""
    return (
        f"https://{match.group('host')}/{match.group('tenant')}/JobBoard/{match.group('board')}"
        f"/OpportunityDetail?opportunityId={match.group('job')}"
    )


def ukg_opportunity(page: Any) -> Mapping[str, Any] | None:
    """Return the titled opportunity record a posting page embeds, or None."""
    if not isinstance(page, str) or (call := _EMBEDDED.search(page)) is None:
        return None
    try:
        record, _end = json.JSONDecoder().raw_decode(page, call.end())
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, Mapping) and stated_text(record, ("Title",)) else None


def ukg_withdrawn(record: Mapping[str, Any]) -> bool:
    """Return whether the page states the opportunity is closed."""
    return record.get("OpportunityIsClosed") is True


def _place(location: Any) -> str:
    """Render one location's address when the page shows it, else its name."""
    if not isinstance(location, Mapping):
        return ""
    address = [
        stated_text(location, ("Address", "City")),
        stated_text(location, ("Address", "State", "Code")),
        stated_text(location, ("Address", "Country", "Name")),
    ]
    if location.get("DisplayAddress") is True and address[0]:
        return ", ".join(part for part in address if part)
    return stated_text(location, ("LocalizedName",))


def _places(posting: Posting) -> list[str]:
    locations = stated(posting.record, ("Locations",))
    if not isinstance(locations, list):
        return []
    return [place for location in locations if (place := _place(location))]


def _compensation(posting: Posting) -> str | None:
    """Return the pay range only when the page shows it; a hidden range is still filled in."""
    if stated(posting.record, ("PayRangeVisible",)) is not True:
        return None
    low = stated_text(posting.record, ("PayRange", "PayRangeMinimum"))
    high = stated_text(posting.record, ("PayRange", "PayRangeMaximum"))
    if not low:
        return None
    span = f"{low}-{high}" if high and high != low else low
    currency = stated_text(posting.record, ("PayRangeCurrencyCode",))
    return f"{span} {currency}".strip()


UKG_MAPPING = BoardDeclaration(
    platform="ukg",
    title=("Title",),
    requisition_id=("RequisitionNumber",),
    places=_places,
    description=(("Description",),),
    compensation=_compensation,
)
