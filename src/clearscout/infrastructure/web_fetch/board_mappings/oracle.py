"""Oracle Recruiting Cloud's requisition record, as its listing and its detail endpoint state it.

``ORACLE_LISTING_MAPPING`` reads one ``requisitionList`` item; ``Posting.groups``
must carry ``host``, ``locale``, and ``site`` so it can build the posting's
candidate-experience address. ``ORACLE_DETAIL_MAPPING`` reads one
``recruitingCEJobRequisitionDetails`` item. A stated legal employer takes precedence
over the registered board name. Only the detail states the full description,
so the listing retains no body.
"""

from __future__ import annotations

from clearscout.domain.staged_facts import PostedDateGrade
from clearscout.infrastructure.web_fetch.board_mappings.mapper import (
    BoardDeclaration,
    Posting,
    posted_on,
    stated,
    stated_text,
)
from clearscout.infrastructure.web_fetch.listing_facts import (
    ArrangementSignal,
    extended_locations,
    remote_checked_arrangement,
)

# Oracle's WorkplaceTypeCode values; a code not listed here is unresolved.
_ARRANGEMENTS = {
    "REMOTE": "remote",
    "ONSITE": "onsite",
    "ON_SITE": "onsite",
    "HYBRID": "hybrid",
}


_PLACE = ("PrimaryLocation",)
_DESCRIPTION = (
    ("ExternalDescriptionStr",),
    ("ExternalResponsibilitiesStr",),
    ("ExternalQualificationsStr",),
)


def _arrangement(posting: Posting) -> ArrangementSignal:
    code = stated_text(posting.record, ("WorkplaceTypeCode",)).upper()
    if not code:
        return ArrangementSignal()
    value = _ARRANGEMENTS.get(code)
    if value is None:
        return ArrangementSignal(unresolved=(f"unrecognized WorkplaceTypeCode: {code}",))
    body = [stated(posting.record, path) for path in _DESCRIPTION]
    return remote_checked_arrangement(value, stated(posting.record, _PLACE), body)


def _detail_places(posting: Posting) -> list[str]:
    """Return the primary location, then each secondary location the detail's expansion names."""
    secondary = [
        stated_text(entry, ("Name",))
        for entry in stated(posting.record, ("secondaryLocations",)) or []
    ]
    return list(extended_locations([stated_text(posting.record, _PLACE)], secondary))


def _employer(posting: Posting) -> str:
    company = stated(posting.record, ("LegalEmployer",))
    return company.strip() if isinstance(company, str) else ""


def _listing_url(posting: Posting) -> str:
    requisition = stated_text(posting.record, ("Id",))
    if not requisition:
        return ""
    host = posting.groups.get("host", "")
    locale = posting.groups.get("locale", "")
    site = posting.groups.get("site", "")
    return f"https://{host}/hcmUI/CandidateExperience/{locale}/sites/{site}/job/{requisition}"


ORACLE_LISTING_MAPPING = BoardDeclaration(
    platform="oracle",
    title=("Title",),
    employer=_employer,
    prefer_publisher_employer=True,
    url=_listing_url,
    location=_PLACE,
    places=_PLACE,
    requisition_id=("Id",),
    arrangement=_arrangement,
)

ORACLE_DETAIL_MAPPING = BoardDeclaration(
    platform="oracle",
    title=("Title",),
    employer=_employer,
    prefer_publisher_employer=True,
    location=_PLACE,
    places=_detail_places,
    requisition_id=("Id",),
    description=_DESCRIPTION,
    arrangement=_arrangement,
    heading=(("Title",), ("PrimaryLocation",), ("WorkplaceType",)),
    posted=posted_on(("ExternalPostedStartDate",), PostedDateGrade.FIRST_POSTED),
)
