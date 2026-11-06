"""ClearCompany's public careers API result, which carries each opening's body."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from clearscout.domain.staged_facts import PostedDateGrade, PublisherPostedDate
from clearscout.infrastructure.web_fetch.board_mappings.mapper import (
    BoardDeclaration,
    Posting,
    pay_period,
    pay_range,
    posted_on,
    stated,
    stated_text,
)
from clearscout.infrastructure.web_fetch.listing_facts import ArrangementSignal

_POSTED = posted_on(("postedDate",), PostedDateGrade.FIRST_POSTED)
# A requisition opens before its posting goes live, so the open date is no first-posted day.
_OPENED = posted_on(("openDate",), PostedDateGrade.BOARD_DATE)


def clearcompany_jobs_url(site: str, page: int, page_size: int) -> str:
    """Return one page of a ClearCompany career site's public job list."""
    return (
        f"https://careers-api.clearcompany.com/v1/{site}"
        f"?pageIndex={page}&pageSize={page_size}&source=CJB-0"
    )


def _title(posting: Posting) -> str:
    return stated_text(posting.record, ("positionTitle",)) or stated_text(
        posting.record, ("title",)
    )


def _url(posting: Posting) -> str:
    """Return the posting page the apply link sits under, without its tracking query."""
    link = urlsplit(stated_text(posting.record, ("applyLink",)))
    path = link.path.rstrip("/").removesuffix("/apply")
    return urlunsplit((link.scheme, link.netloc, path, "", "")) if link.netloc else ""


def _place(entry: Any) -> str:
    parts = (stated_text(entry, (key,)) for key in ("city", "subdivision", "country"))
    return ", ".join(part for part in parts if part)


def _flagged(posting: Posting, *keys: str) -> bool:
    """Return whether any location entry sets one of ``keys``; the API flags entries, not jobs."""
    entries = stated(posting.record, ("locations",))
    if not isinstance(entries, list):
        return False
    return any(stated(entry, (key,)) is True for entry in entries for key in keys)


def _remote(posting: Posting) -> bool:
    return _flagged(posting, "isRemote", "isNationwide")


def _places(posting: Posting) -> list[str]:
    """Return each located entry, or the flat location a remote or nationwide job states."""
    flat = stated_text(posting.record, ("location",))
    if _remote(posting) and flat:
        return [flat]
    entries = stated(posting.record, ("locations",))
    places = [_place(entry) for entry in entries] if isinstance(entries, list) else []
    return [place for place in dict.fromkeys([*places, *([] if places else [flat])]) if place]


def _arrangement(posting: Posting) -> ArrangementSignal:
    """Read a location entry's ``isRemote: true`` as remote; otherwise state no arrangement."""
    if not _flagged(posting, "isRemote"):
        return ArrangementSignal()
    return ArrangementSignal(value="remote")


def _compensation(posting: Posting) -> str | None:
    """Return the salary range the job states; ClearCompany states no currency beside it."""
    record = posting.record
    return pay_range(
        "",
        stated_text(record, ("salaryRangeLow",)),
        stated_text(record, ("salaryRangeHigh",)),
        pay_period(stated(record, ("salaryType",))),
    )


def _posted(posting: Posting) -> PublisherPostedDate | None:
    """Return the stated posting date, or the opening date when the job states none."""
    return _POSTED(posting) or _OPENED(posting)


CLEARCOMPANY_MAPPING = BoardDeclaration(
    platform="clearcompany",
    title=_title,
    url=_url,
    places=_places,
    requisition_id=("id",),
    arrangement=_arrangement,
    compensation=_compensation,
    description=(("description",),),
    posted=_posted,
)
