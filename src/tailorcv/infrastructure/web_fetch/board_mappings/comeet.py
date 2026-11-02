"""Comeet's posting record, as its public token-scoped careers API states it."""

from __future__ import annotations

import re
from typing import Any

from tailorcv.infrastructure.web_fetch.board_mappings.mapper import (
    BoardDeclaration,
    Posting,
    stated,
    stated_text,
)
from tailorcv.infrastructure.web_fetch.listing_facts import (
    ArrangementSignal,
    remote_checked_arrangement,
)

_PLACE = ("location", "name")
_ARRANGEMENTS = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite"}


def _url(posting: Posting) -> str:
    record = posting.record
    return stated_text(record, ("url_active_page",)) or stated_text(
        record, ("url_comeet_hosted_page",)
    )


def _requisition_id(posting: Posting) -> str:
    record = posting.record
    return stated_text(record, ("internal_use_custom_id",)) or stated_text(record, ("uid",))


def _description(posting: Posting) -> str:
    """Join every detail section's stated value, in feed order."""
    details = stated(posting.record, ("details",))
    items: list[Any] = details if isinstance(details, list) else []
    sections = [
        str(item.get("value")) for item in items if isinstance(item, dict) and item.get("value")
    ]
    return "\n\n".join(sections)


def _arrangement(posting: Posting) -> ArrangementSignal:
    """Read workplace_type, else a set location is_remote; an unknown type is unresolved."""
    stated_type = stated_text(posting.record, ("workplace_type",))
    if not stated_type:
        # An unset flag is every office posting's default, so it states nothing.
        if stated(posting.record, ("location", "is_remote")) is not True:
            return ArrangementSignal()
        stated_type = "remote"
    value = _ARRANGEMENTS.get(re.sub(r"[\s_-]", "", stated_type.casefold()))
    if value is None:
        return ArrangementSignal(unresolved=(f"workplace_type: {stated_type}",))
    return remote_checked_arrangement(value, stated(posting.record, _PLACE), _description(posting))


COMEET_MAPPING = BoardDeclaration(
    platform="comeet",
    title=("name",),
    url=_url,
    requisition_id=_requisition_id,
    employer=("company_name",),
    places=_PLACE,
    description=_description,
    arrangement=_arrangement,
)
