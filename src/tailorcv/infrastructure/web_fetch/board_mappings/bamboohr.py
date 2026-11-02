"""BambooHR's list row joined with its detail job opening."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tailorcv.infrastructure.web_fetch.board_mappings.mapper import (
    BoardDeclaration,
    Path,
    Posting,
    stated,
    stated_text,
)
from tailorcv.infrastructure.web_fetch.listing_facts import ArrangementSignal


def bamboohr_detail_url(tenant: str, job_id: str) -> str:
    """Return the detail endpoint, the only BambooHR answer that states a posting's body."""
    return f"https://{tenant}.bamboohr.com/careers/{job_id}/detail"


def bamboohr_opening(payload: Any) -> Mapping[str, Any] | None:
    """Return the job opening a detail answer carries, or None when it carries none."""
    result = payload.get("result") if isinstance(payload, Mapping) else None
    opening = result.get("jobOpening") if isinstance(result, Mapping) else None
    return opening if isinstance(opening, Mapping) else None


def bamboohr_withdrawn(opening: Mapping[str, Any]) -> bool:
    """Return whether the opening states a status other than open; an absent one proves nothing."""
    status = stated_text(opening, ("jobOpeningStatus",)).casefold()
    return bool(status) and status != "open"


def _title(posting: Posting) -> str:
    return stated_text(posting.record, ("opening", "jobOpeningName")) or stated_text(
        posting.record, ("row", "jobOpeningName")
    )


def _url(posting: Posting) -> str:
    share_url = stated_text(posting.record, ("opening", "jobOpeningShareUrl"))
    if share_url:
        return share_url
    tenant = posting.groups.get("tenant", "")
    job_id = stated_text(posting.record, ("row", "id"))
    return f"https://{tenant}.bamboohr.com/careers/{job_id}"


def _place(record: Any, path: Path) -> str:
    """Render one BambooHR location object's city/state/country, without a remote label."""
    location = stated(record, path)
    if not isinstance(location, Mapping):
        return ""
    return ", ".join(
        stated_text(location, (field,))
        for field in ("city", "state", "addressCountry")
        if stated_text(location, (field,))
    )


def _places(posting: Posting) -> str:
    return _place(posting.record, ("opening", "location")) or _place(
        posting.record, ("row", "location")
    )


def _arrangement(posting: Posting) -> ArrangementSignal:
    """Read BambooHR's isRemote boolean as its own arrangement signal, not a place suffix."""
    opening_remote = stated(posting.record, ("opening", "isRemote"))
    row_remote = stated(posting.record, ("row", "isRemote"))
    return ArrangementSignal(value="remote" if True in (opening_remote, row_remote) else None)


def _compensation(posting: Posting) -> str | None:
    """Return the opening's pay as the publisher wrote it; BambooHR states it only as text."""
    return stated_text(posting.record, ("opening", "compensation")) or None


BAMBOOHR_MAPPING = BoardDeclaration(
    platform="bamboohr",
    title=_title,
    url=_url,
    places=_places,
    requisition_id=("row", "id"),
    description=(("opening", "description"),),
    arrangement=_arrangement,
    compensation=_compensation,
)
