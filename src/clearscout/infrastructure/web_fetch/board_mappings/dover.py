"""Dover's careers-page row joined with its application-portal job."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from clearscout.domain.countries import territory_name
from clearscout.infrastructure.web_fetch.board_mappings.mapper import (
    BoardDeclaration,
    Posting,
    pay_range,
    stated,
    stated_text,
)
from clearscout.infrastructure.web_fetch.listing_facts import ArrangementSignal, extended_locations

_HOST = "https://app.dover.io"
# Only REMOTE is a value the public API has been seen to state; any other is unresolved.
_ARRANGEMENTS = {"REMOTE": "remote"}


def dover_jobs_url(client: str) -> str:
    """Return the careers-page API address that lists a client's jobs."""
    return f"{_HOST}/api/v1/careers-page/{client}/jobs"


def dover_detail_url(job_id: str) -> str:
    """Return the application-portal job endpoint, the only Dover answer that states a body."""
    return f"{_HOST}/api/v1/inbound/application-portal-job/{job_id}"


def dover_job(payload: Any) -> Mapping[str, Any] | None:
    """Return an application-portal answer that titles its job, or None."""
    return payload if isinstance(payload, Mapping) and payload.get("title") else None


def dover_listed(row: Mapping[str, Any]) -> bool:
    """Return whether a careers-page row is a published opening rather than a sample."""
    return row.get("is_published") is not False and row.get("is_sample") is not True


def _title(posting: Posting) -> str:
    return stated_text(posting.record, ("detail", "title")) or stated_text(
        posting.record, ("row", "title")
    )


def _url(posting: Posting) -> str:
    company = stated_text(posting.record, ("detail", "client_name"))
    job_id = stated_text(posting.record, ("row", "id"))
    return f"{_HOST}/apply/{quote(company, safe='')}/{job_id}" if company and job_id else ""


def _places(posting: Posting) -> list[str]:
    """Return each row place and its country, else the one place the detail states."""
    options = [
        option
        for place in stated(posting.record, ("row", "locations")) or []
        if isinstance(option := stated(place, ("location_option",)), Mapping)
    ]
    names = [stated_text(option, ("display_name",)) for option in options]
    countries = [
        territory_name(country) or country
        for option in options
        if (country := stated_text(option, ("country",)))
    ]
    if not any(names):
        names = [stated_text(posting.record, ("detail", "location"))]
    return list(extended_locations(names, countries))


def _arrangement(posting: Posting) -> ArrangementSignal:
    """Read the row's workplace_type; a detail alone states no arrangement."""
    kind = stated_text(posting.record, ("row", "workplace_type")).upper()
    if not kind:
        return ArrangementSignal()
    value = _ARRANGEMENTS.get(kind)
    if value is None:
        return ArrangementSignal(unresolved=(f"workplace_type: {kind}",))
    return ArrangementSignal(value=value)


def _requisition_id(posting: Posting) -> str:
    return stated_text(posting.record, ("row", "id")) or stated_text(
        posting.record, ("detail", "id")
    )


def _compensation(posting: Posting) -> str | None:
    """Return the detail's pay bounds and currency; Dover states no pay period beside them."""
    pay = stated(posting.record, ("detail", "compensation"))
    if not isinstance(pay, Mapping):
        return None
    return pay_range(pay.get("currency_code"), pay.get("lower_bound"), pay.get("upper_bound"))


DOVER_MAPPING = BoardDeclaration(
    platform="dover",
    title=_title,
    url=_url,
    employer=("detail", "client_name"),
    places=_places,
    requisition_id=_requisition_id,
    description=(("detail", "user_provided_description"),),
    arrangement=_arrangement,
    compensation=_compensation,
)
