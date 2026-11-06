"""An ADP Workforce Now career center's requisition list row joined with its detail answer."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from clearscout.domain.countries import territory_name
from clearscout.domain.staged_facts import PostedDateGrade
from clearscout.infrastructure.web_fetch.board_mappings.mapper import (
    BoardDeclaration,
    Posting,
    plain_description,
    posted_on,
    stated,
    stated_text,
)
from clearscout.infrastructure.web_fetch.listing_facts import extended_locations

_HOST = "https://workforcenow.adp.com"
_REQUISITIONS = f"{_HOST}/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"
_PAGE = f"{_HOST}/mascsr/default/mdf/recruitment/recruitment.html"
# The career page itself defaults to this language when its address names none.
_DEFAULT_LANG = "en_US"
_PAY = ("detail", "payGradeRange")


def adp_requisitions_url(
    cid: str, lang: str, *, top: int, skip: int, career_center: str = ""
) -> str:
    """Return one page of a career center's open requisitions; ``skip`` is 1-based."""
    # A skip of 0 answers with one requisition rather than the first page.
    locale = lang or _DEFAULT_LANG
    center = f"&ccId={career_center}" if career_center else ""
    return (
        f"{_REQUISITIONS}?cid={cid}{center}&lang={locale}&locale={locale}"
        f"&$top={top}&$skip={max(skip, 1)}"
    )


def adp_requisition_url(cid: str, item_id: str, lang: str = "", *, career_center: str = "") -> str:
    """Return the single-requisition endpoint, the only ADP answer that states a body."""
    locale = lang or _DEFAULT_LANG
    center = f"&ccId={career_center}" if career_center else ""
    return f"{_REQUISITIONS}/{item_id}?cid={cid}{center}&lang={locale}&locale={locale}"


def adp_requisition(payload: Any) -> Mapping[str, Any] | None:
    """Return a single-requisition answer that names its requisition, or None."""
    return payload if isinstance(payload, Mapping) and payload.get("requisitionTitle") else None


def adp_item_id(row: Any) -> str:
    """Return a list row's stated item id, or empty when it states none."""
    return stated_text(row, ("itemID",)) if isinstance(row, Mapping) else ""


def _item_id(posting: Posting) -> str:
    return stated_text(posting.record, ("detail", "itemID")) or adp_item_id(
        stated(posting.record, ("row",))
    )


def _url(posting: Posting) -> str:
    cid = posting.groups.get("tenant", "")
    item_id = _item_id(posting)
    if not cid or not item_id:
        return ""
    lang = posting.groups.get("lang") or _DEFAULT_LANG
    career_center = posting.groups.get("career_center", "")
    center = f"&ccId={career_center}" if career_center else ""
    return f"{_PAGE}?cid={cid}{center}&jobId={item_id}&lang={lang}"


def _places(posting: Posting) -> list[str]:
    """Return each requisition place as the career page names it, then its country."""
    located = stated(posting.record, ("detail", "requisitionLocations"))
    rows = [place for place in located or [] if isinstance(place, Mapping)]
    names = [
        stated_text(place, ("nameCode", "shortName"))
        or ", ".join(
            part
            for path in (
                ("address", "cityName"),
                ("address", "countrySubdivisionLevel1", "codeValue"),
            )
            if (part := stated_text(place, path))
        )
        for place in rows
    ]
    countries = [
        territory_name(stated_text(place, ("address", "countryCode"))) or "" for place in rows
    ]
    return list(extended_locations(names, countries))


def _rate(posting: Posting, rate: str) -> tuple[str, str]:
    """Return one stated pay rate's amount, written plainly, and its currency."""
    value = stated(posting.record, (*_PAY, rate, "amountValue"))
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return "", ""
    return f"{Decimal(str(value)).normalize():f}", stated_text(
        posting.record, (*_PAY, rate, "currencyCode")
    )


def _compensation(posting: Posting) -> str | None:
    """Write the stated pay grade range; ADP states no pay period beside it."""
    low, low_currency = _rate(posting, "minimumRate")
    high, high_currency = _rate(posting, "maximumRate")
    currency = low_currency or high_currency
    if not low or not currency or (high_currency and high_currency != currency):
        return None
    span = f"{low}-{high}" if high and high != low else low
    return f"{currency} {span}"


def _title(posting: Posting) -> str:
    return stated_text(posting.record, ("detail", "requisitionTitle")) or stated_text(
        posting.record, ("row", "requisitionTitle")
    )


def _requisition_id(posting: Posting) -> str:
    return stated_text(posting.record, ("detail", "clientRequisitionID")) or _item_id(posting)


def _employer(posting: Posting) -> str:
    """Read only the leading Company-labelled paragraph, or keep the configured name."""
    description = plain_description(stated(posting.record, ("detail", "requisitionDescription")))
    leading, _, _ = description.partition("\n\n")
    label, colon, name = leading.partition(":")
    company = " ".join(name.split()) if colon and label.strip().casefold() == "company" else ""
    return company or posting.employer


ADP_MAPPING = BoardDeclaration(
    platform="adp",
    title=_title,
    url=_url,
    places=_places,
    requisition_id=_requisition_id,
    employer=_employer,
    prefer_publisher_employer=True,
    description=(("detail", "requisitionDescription"),),
    employment_types=("detail", "workLevelCode", "shortName"),
    compensation=_compensation,
    posted=posted_on(("detail", "postDate"), PostedDateGrade.FIRST_POSTED),
)
