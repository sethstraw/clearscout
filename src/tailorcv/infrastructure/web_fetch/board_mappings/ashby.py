"""Ashby's posting record, which its board feed and single-posting read share unchanged."""

from __future__ import annotations

from typing import Any, cast

from tailorcv.domain.staged_facts import PostedDateGrade
from tailorcv.infrastructure.web_fetch.board_mappings.mapper import (
    BoardDeclaration,
    Posting,
    plain_description,
    posted_on,
    stated,
    stated_text,
)
from tailorcv.infrastructure.web_fetch.listing_facts import (
    ArrangementSignal,
    ashby_countries,
    ashby_places,
    location_display,
    remote_checked_arrangement,
)


def _job(posting: Posting) -> dict[str, Any]:
    """Ashby's API is JSON-only, so a posting record is always a job mapping."""
    return cast(dict[str, Any], posting.record)


def _places(posting: Posting) -> list[str]:
    """Return every place plus every country an Ashby posting states."""
    job = _job(posting)
    return [*ashby_places(job), *ashby_countries(job)]


def _location(posting: Posting) -> str:
    job = posting.record
    return location_display(stated_text(job, ("location",)), ashby_places(_job(posting)))


def _compensation(posting: Posting) -> str | None:
    """Return the pay range an Ashby posting publishes, or None."""
    compensation = stated(posting.record, ("compensation",))
    if not isinstance(compensation, dict):
        return None
    summary = str(compensation.get("compensationTierSummary") or "").strip()
    salary = _salary_range(compensation.get("summaryComponents"))
    if summary and salary:
        return f"{summary} (salary {salary})"
    return summary or salary or None


_INTERVALS = {"1 YEAR": "per year", "1 MONTH": "per month", "1 HOUR": "per hour"}


def _salary_range(components: Any) -> str:
    """Return the one salary component's currency, range and period, or "" when not exactly one."""
    if not isinstance(components, list):
        return ""
    salaries = [
        item
        for item in components
        if isinstance(item, dict) and item.get("compensationType") == "Salary"
    ]
    if len(salaries) != 1:
        return ""
    salary = salaries[0]
    currency, period = salary.get("currencyCode"), _INTERVALS.get(str(salary.get("interval")))
    low, high = salary.get("minValue"), salary.get("maxValue")
    if not (isinstance(currency, str) and currency and period):
        return ""
    if not all(isinstance(value, int | float) for value in (low, high)):
        return ""
    return f"{currency} {low:,.0f} - {high:,.0f} {period}"


def _description(posting: Posting) -> str:
    """Prefer the HTML body, which carries the full post; the plain one is often thin."""
    job = posting.record
    return str(stated(job, ("descriptionHtml",)) or stated(job, ("descriptionPlain",)) or "")


def _arrangement(posting: Posting) -> ArrangementSignal:
    return remote_checked_arrangement(
        stated(posting.record, ("workplaceType",)),
        _places(posting),
        plain_description(_description(posting)),
    )


ASHBY_MAPPING = BoardDeclaration(
    platform="ashby",
    title=("title",),
    url=("jobUrl",),
    requisition_id=("id",),
    places=_places,
    location=_location,
    description=_description,
    arrangement=_arrangement,
    employment_types=("employmentType",),
    compensation=_compensation,
    heading=(("title",), _location, ("employmentType",)),
    posted=posted_on(("publishedAt",), PostedDateGrade.FIRST_POSTED),
)
