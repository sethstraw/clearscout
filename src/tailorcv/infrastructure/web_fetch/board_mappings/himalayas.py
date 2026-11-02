"""Himalayas' posting record, as its bounded newest-openings aggregator feed states it."""

from __future__ import annotations

from typing import Any

from tailorcv.infrastructure.web_fetch.board_mappings.mapper import (
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


HIMALAYAS_MAPPING = BoardDeclaration(
    platform="himalayas",
    title=("title",),
    url=("applicationLink",),
    requisition_id=("guid",),
    employer=_employer,
    places=("locationRestrictions",),
    location=_location,
)
