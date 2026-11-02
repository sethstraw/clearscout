"""Dayforce's anonymous feed, collapsed to one posting with every regional variant."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tailorcv.domain.countries import territory_name
from tailorcv.infrastructure.web_fetch.board_mappings.mapper import (
    BoardDeclaration,
    Posting,
    pay_range,
    stated,
    stated_text,
)
from tailorcv.infrastructure.web_fetch.listing_facts import ArrangementSignal


def dayforce_feed_url(namespace: str, site: str) -> str:
    """Return the anonymous feed stating every posting on one Dayforce site."""
    return f"https://www.dayforcehcm.com/api/{namespace}/v1/jobfeeds?internalJobBoardCode={site}"


def _variant_place(variant: Any) -> str:
    """Render one regional variant's place the way Dayforce's feed states it."""
    if not isinstance(variant, Mapping):
        return ""
    country_code = stated_text(variant, ("Country",))
    country = territory_name(country_code) or country_code
    return ", ".join(
        part
        for part in (stated_text(variant, ("City",)), stated_text(variant, ("State",)), country)
        if part
    )


def _places(posting: Posting) -> list[str]:
    """Return every regional variant's place, deduplicated in feed order."""
    variants = stated(posting.record, ("variants",)) or []
    return list(dict.fromkeys(place for item in variants if (place := _variant_place(item))))


def _places_complete(posting: Posting) -> bool:
    """Return False when every regional variant restates one street address and postal code."""
    # Dayforce restates one office once per advertised region, pairing its city with each region.
    offices = {
        (stated_text(item, ("AddressLine1",)), stated_text(item, ("PostalCode",)))
        for item in stated(posting.record, ("variants",)) or []
    }
    if len(offices) != 1 or len(_places(posting)) < 2:
        return True
    street, postal_code = offices.pop()
    return not (street and postal_code)


def _requisition_id(posting: Posting) -> str:
    return stated_text(posting.record, ("ParentRequisitionCode",)) or stated_text(
        posting.record, ("ReferenceNumber",)
    )


def _employer(posting: Posting) -> str:
    return (
        stated_text(posting.record, ("CompanyName",))
        or stated_text(posting.record, ("ClientSiteName",))
        or posting.employer
    )


def _arrangement(posting: Posting) -> ArrangementSignal:
    """Read IsVirtualLocation on every regional variant; a disagreement stays unresolved."""
    flags = {
        stated(item, ("IsVirtualLocation",))
        for item in stated(posting.record, ("variants",)) or []
        if isinstance(item, Mapping)
    }
    if True not in flags:
        return ArrangementSignal()
    if flags != {True}:
        return ArrangementSignal(unresolved=("IsVirtualLocation: set on some regional variants",))
    return ArrangementSignal(value="remote", provenance_suffix="IsVirtualLocation")


def _compensation(posting: Posting) -> str | None:
    """Return the hiring-rate range; the feed states no currency or period beside it."""
    record = posting.record
    return pay_range("", stated(record, ("MinHiringRate",)), stated(record, ("MaxHiringRate",)))


DAYFORCE_MAPPING = BoardDeclaration(
    platform="dayforce",
    title=("Title",),
    url=("JobDetailsUrl",),
    employer=_employer,
    places=_places,
    locations_complete=_places_complete,
    requisition_id=_requisition_id,
    description=(("Description",),),
    arrangement=_arrangement,
    compensation=_compensation,
)
