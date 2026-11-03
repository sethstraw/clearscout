"""HiBob's public careers job-ad feed, which states every posting with its full body."""

from __future__ import annotations

from clearscout.infrastructure.web_fetch.board_mappings.mapper import (
    BoardDeclaration,
    Posting,
    pay_period,
    salary_compensation,
    stated,
    stated_text,
)
from clearscout.infrastructure.web_fetch.ingest.job_posting_markup import BaseSalaryMarkup
from clearscout.infrastructure.web_fetch.listing_facts import (
    ArrangementSignal,
    extended_locations,
    remote_checked_arrangement,
)

_DESCRIPTION = (("description",), ("responsibilities",), ("requirements",), ("benefits",))
_ARRANGEMENTS = {"remote": "remote", "hybrid": "hybrid", "on_site": "onsite", "onsite": "onsite"}


def hibob_job_ads_url(tenant: str) -> str:
    """Return the feed one HiBob careers site reads its published job ads from."""
    return f"https://{tenant}.careers.hibob.com/api/job-ad"


def _url(posting: Posting) -> str:
    tenant, job = posting.groups.get("tenant", ""), stated_text(posting.record, ("id",))
    return f"https://{tenant}.careers.hibob.com/jobs/{job}" if tenant and job else ""


def _places(posting: Posting) -> list[str]:
    """Return the ad's site and the country it names."""
    site = stated_text(posting.record, ("site",))
    country = stated_text(posting.record, ("country",))
    return list(extended_locations([site], [country]))


def _arrangement(posting: Posting) -> ArrangementSignal:
    """Read the workspace type; one the vocabulary does not know is unresolved."""
    stated_value = stated_text(posting.record, ("workspaceTypeId",)).casefold()
    if not stated_value:
        return ArrangementSignal()
    value = _ARRANGEMENTS.get(stated_value)
    if value is None:
        return ArrangementSignal(unresolved=(f"workspaceType: {stated_value}",))
    body = [stated(posting.record, path) for path in _DESCRIPTION]
    return remote_checked_arrangement(value, _places(posting), body)


def _compensation(posting: Posting) -> str | None:
    """Return the pay-transparency range only when its floor, ceiling and period are stated."""
    return salary_compensation(
        BaseSalaryMarkup(
            currency=stated_text(posting.record, ("payTransparencySalaryCurrency",)),
            minimum=stated_text(posting.record, ("payTransparencyMinSalary",)),
            maximum=stated_text(posting.record, ("payTransparencyMaxSalary",)),
            unit=pay_period(stated_text(posting.record, ("payTransparencySalaryPayPeriod",))),
        )
    )


HIBOB_MAPPING = BoardDeclaration(
    platform="hibob",
    title=("title",),
    url=_url,
    places=_places,
    # One site field cannot list the other places a multi-site ad is open in.
    locations_complete=lambda _posting: False,
    requisition_id=("id",),
    employment_types=("employmentType",),
    compensation=_compensation,
    arrangement=_arrangement,
    description=_DESCRIPTION,
)
