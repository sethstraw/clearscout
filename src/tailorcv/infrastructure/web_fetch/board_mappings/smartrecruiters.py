"""SmartRecruiters' posting record; only the single-posting detail states the ``jobAd`` body."""

from __future__ import annotations

from tailorcv.domain.staged_facts import PostedDateGrade
from tailorcv.infrastructure.web_fetch.board_mappings.mapper import (
    BoardDeclaration,
    Posting,
    posted_on,
    stated,
    stated_text,
)
from tailorcv.infrastructure.web_fetch.listing_facts import (
    ArrangementSignal,
    remote_checked_arrangement,
)


def _url(posting: Posting) -> str:
    """Prefer the publisher's own reference link, else the board's canonical address."""
    ref = stated_text(posting.record, ("ref",))
    if ref:
        return ref
    org = posting.groups.get("org", "")
    job_id = stated_text(posting.record, ("id",))
    return f"https://jobs.smartrecruiters.com/{org}/{job_id}"


def _places(posting: Posting) -> str:
    """Prefer the place SmartRecruiters writes out; its parts carry a bare country code."""
    return stated_text(posting.record, ("location", "fullLocation")) or ", ".join(
        part
        for part in (
            stated_text(posting.record, ("location", "city")),
            stated_text(posting.record, ("location", "region")),
            stated_text(posting.record, ("location", "country")),
        )
        if part
    )


def _requisition_id(posting: Posting) -> str:
    return stated_text(posting.record, ("refNumber",)) or stated_text(posting.record, ("id",))


# The role leads so the bounded screening summary is not spent on company copy.
_DESCRIPTION = (
    ("jobAd", "sections", "jobDescription", "text"),
    ("jobAd", "sections", "qualifications", "text"),
    ("jobAd", "sections", "additionalInformation", "text"),
    ("jobAd", "sections", "companyDescription", "text"),
)


def _arrangement(posting: Posting) -> ArrangementSignal:
    """Read the arrangement SmartRecruiters states as booleans on the posting location."""
    remote = stated(posting.record, ("location", "remote"))
    hybrid = stated(posting.record, ("location", "hybrid"))
    if remote is True:
        return ArrangementSignal(value="remote")
    if hybrid is True:
        value = "hybrid"
    # Both flags present and false is the board stating attendance, not silence.
    elif remote is False and hybrid is False:
        value = "onsite"
    else:
        return ArrangementSignal()
    body = [stated(posting.record, path) for path in _DESCRIPTION]
    return remote_checked_arrangement(value, _places(posting), body)


SMARTRECRUITERS_MAPPING = BoardDeclaration(
    platform="smartrecruiters",
    title=("name",),
    url=_url,
    employer=("company", "name"),
    places=_places,
    requisition_id=_requisition_id,
    employment_types=("typeOfEmployment", "label"),
    arrangement=_arrangement,
    description=_DESCRIPTION,
    heading=(("name",), _places),
    posted=posted_on(("releasedDate",), PostedDateGrade.FIRST_POSTED),
)
