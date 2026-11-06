"""Rippling's board list row joined with its single-job answer."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clearscout.domain.countries import territory_name
from clearscout.domain.staged_facts import PostedDateGrade
from clearscout.infrastructure.web_fetch.board_mappings.mapper import (
    BoardDeclaration,
    Posting,
    pay_range,
    posted_on,
    stated,
    stated_text,
)
from clearscout.infrastructure.web_fetch.listing_facts import (
    ArrangementSignal,
    extended_locations,
    remote_checked_arrangement,
    values,
)

_HOST = "https://ats.rippling.com"
# The list rows' workplaceType values; a code not listed here is unresolved.
_ARRANGEMENTS = {"REMOTE": "remote", "ON_SITE": "onsite", "HYBRID": "hybrid"}
_DESCRIPTION = (("detail", "description", "company"), ("detail", "description", "role"))


def rippling_jobs_url(org: str, page: int = 0) -> str:
    """Return the public board API address that lists one zero-indexed page of an org's jobs."""
    base = f"{_HOST}/api/v2/board/{org}/jobs"
    return f"{base}?page={page}" if page else base


def rippling_detail_url(org: str, job_id: str) -> str:
    """Return the single-job endpoint, the only Rippling answer that states a body."""
    return f"{rippling_jobs_url(org)}/{job_id}"


def rippling_job(payload: Any) -> Mapping[str, Any] | None:
    """Return a single-job answer that names its job, or None."""
    return payload if isinstance(payload, Mapping) and payload.get("name") else None


def _row_locations(posting: Posting) -> list[Mapping[str, Any]]:
    located = stated(posting.record, ("row", "locations"))
    return [place for place in located or [] if isinstance(place, Mapping)]


def _url(posting: Posting) -> str:
    stated_url = stated_text(posting.record, ("row", "url")) or stated_text(
        posting.record, ("detail", "url")
    )
    if stated_url:
        return stated_url
    job_id = stated_text(posting.record, ("row", "id"))
    org = posting.groups.get("org", "")
    return f"{_HOST}/{org}/jobs/{job_id}" if org and job_id else ""


def _places(posting: Posting) -> list[str]:
    """Return each list-row place and its country, else the places the detail states."""
    rows = _row_locations(posting)
    names = [stated_text(place, ("name",)) for place in rows]
    countries = [
        territory_name(stated_text(place, ("countryCode",))) or stated_text(place, ("country",))
        for place in rows
    ]
    if not any(names):
        names = list(values(stated(posting.record, ("detail", "workLocations"))))
    return list(extended_locations(names, countries))


def _locations_complete(posting: Posting) -> bool:
    """Read whether platform assembly retained every declared location row."""
    return stated(posting.record, ("row", "_tailorcv_rippling_locations_complete")) is not False


def _arrangement(posting: Posting) -> ArrangementSignal:
    """Read each list-row place's workplaceType; a detail alone states no arrangement."""
    stated_types = tuple(
        dict.fromkeys(
            kind
            for place in _row_locations(posting)
            if (kind := stated_text(place, ("workplaceType",)).upper())
        )
    )
    if not stated_types:
        if not _locations_complete(posting):
            return ArrangementSignal(
                unresolved=("Additional publisher locations could not be verified.",)
            )
        return ArrangementSignal()
    if any(kind not in _ARRANGEMENTS for kind in stated_types):
        return ArrangementSignal(
            unresolved=tuple(f"workplaceType: {kind}" for kind in stated_types)
        )
    arrangements = list(dict.fromkeys(_ARRANGEMENTS[kind] for kind in stated_types))
    body = [stated(posting.record, path) for path in _DESCRIPTION]
    signal = remote_checked_arrangement(arrangements, _places(posting), body)
    if not _locations_complete(posting):
        return ArrangementSignal(
            value=signal.value,
            unresolved=(
                *signal.unresolved,
                "Additional publisher locations could not be verified.",
            ),
            provenance_suffix=signal.provenance_suffix,
        )
    return signal


def _title(posting: Posting) -> str:
    return stated_text(posting.record, ("detail", "name")) or stated_text(
        posting.record, ("row", "name")
    )


def _requisition_id(posting: Posting) -> str:
    return stated_text(posting.record, ("row", "id")) or stated_text(
        posting.record, ("detail", "uuid")
    )


def _compensation(posting: Posting) -> list[str]:
    """Return every pay range the detail states, one per currency it is published in."""
    ranges = stated(posting.record, ("detail", "payRangeDetails"))
    return [
        written
        for pay in ranges or []
        if isinstance(pay, Mapping)
        and (written := pay_range(pay.get("currency"), pay.get("rangeStart"), pay.get("rangeEnd")))
    ]


RIPPLING_MAPPING = BoardDeclaration(
    platform="rippling",
    title=_title,
    url=_url,
    employer=("detail", "companyName"),
    places=_places,
    locations_complete=_locations_complete,
    requisition_id=_requisition_id,
    description=_DESCRIPTION,
    arrangement=_arrangement,
    # The label is a code such as SALARIED_FT; the id is the readable "Salaried, full-time".
    employment_types=("detail", "employmentType", "id"),
    compensation=_compensation,
    posted=posted_on(("detail", "createdOn"), PostedDateGrade.FIRST_POSTED),
)
