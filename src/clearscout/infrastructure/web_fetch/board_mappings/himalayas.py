"""Himalayas' posting record, as its bounded newest-openings aggregator feed states it."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from clearscout.domain.staged_facts import (
    LISTED_UNTIL,
    ClosingDateStatement,
    PublisherClosingDate,
)
from clearscout.infrastructure.web_fetch.board_mappings.mapper import (
    BoardDeclaration,
    Posting,
    stated,
    stated_text,
)


def _joined_places(restrictions: Any) -> str:
    if isinstance(restrictions, list):
        return ", ".join(str(item).strip() for item in restrictions if str(item).strip())
    return str(restrictions or "").strip()


def _location(posting: Posting) -> str:
    return _joined_places(stated(posting.record, ("locationRestrictions",)))


def _stated_employer(posting: Posting) -> str:
    """Recover a listing's employer when Himalayas serves the placeholder.

    Himalayas' jobs API returns companyName=='name' for every job when
    limit>1, so the name falls back to a readable form of companySlug.
    """
    record = posting.record
    name = stated_text(record, ("companyName",))
    if name and name != "name":
        return name
    slug = stated_text(record, ("companySlug",))
    if not slug:
        return ""
    return " ".join(part for part in slug.replace("_", "-").split("-") if part).title()


def _employer(posting: Posting) -> str:
    return _stated_employer(posting) or posting.employer


def _listed_until(posting: Posting) -> PublisherClosingDate | None:
    """Read ``expiryDate``, the Unix second Himalayas states it stops listing the job."""
    stated_at = stated(posting.record, ("expiryDate",))
    if isinstance(stated_at, bool) or not isinstance(stated_at, (int, float)):
        return None
    try:
        expiry = datetime.fromtimestamp(stated_at, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
    statement = ClosingDateStatement(
        wording=f"Listed until {expiry:%Y-%m-%d %H:%M} UTC",
        basis=LISTED_UNTIL,
        normalized_value=expiry.isoformat(),
        precision="timestamp",
        timezone="+00:00",
    )
    return PublisherClosingDate(statements=(statement,))


HIMALAYAS_MAPPING = BoardDeclaration(
    platform="himalayas",
    title=("title",),
    url=("applicationLink",),
    requisition_id=("guid",),
    employer=_employer,
    places=("locationRestrictions",),
    location=_location,
    closing=_listed_until,
)
