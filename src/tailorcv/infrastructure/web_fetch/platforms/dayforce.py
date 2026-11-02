"""Dayforce candidate portals and the complete anonymous job feed."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from tailorcv.application.discovery.source_reading import SourceRead
from tailorcv.domain import SourceReadOutcome
from tailorcv.infrastructure.web_fetch.board_mappings import DAYFORCE_MAPPING, dayforce_feed_url
from tailorcv.infrastructure.web_fetch.board_mappings.mapper import stated_text
from tailorcv.infrastructure.web_fetch.board_platform import BoardPlatform
from tailorcv.infrastructure.web_fetch.board_reads import board_listings

if TYPE_CHECKING:
    from tailorcv.infrastructure.web_fetch.board_platform import BoardTransport

DAYFORCE_JOB = re.compile(
    r"jobs\.dayforcehcm\.com/(?:[a-z]{2}-[A-Z]{2}/)?"
    r"(?P<namespace>[A-Za-z0-9_-]+)/(?P<site>[A-Za-z0-9_-]+)/jobs/"
    r"(?P<job>\d+)",
    re.IGNORECASE,
)
DAYFORCE_BOARD = re.compile(
    r"jobs\.dayforcehcm\.com/(?:[a-z]{2}-[A-Z]{2}/)?"
    r"(?P<namespace>[A-Za-z0-9_-]+)/(?P<site>[A-Za-z0-9_-]+)(?:/|$)",
    re.IGNORECASE,
)


def dayforce_posting_id(record: Any) -> str:
    """Return the posting id a feed row's detail address carries, or empty."""
    # A row's reference number is a requisition, which several rows can share.
    match = DAYFORCE_JOB.search(stated_text(record, ("JobDetailsUrl",)))
    return match.group("job") if match else ""


def _root(url: str, _match: re.Match[str]) -> str:
    return url[: url.casefold().find("/jobs/")]


def _read(fetcher: BoardTransport, employer: str, groups: dict[str, str], _url: str) -> SourceRead:
    """Read Dayforce's complete anonymous feed and collapse regional variants."""
    payload = fetcher.get_json(
        dayforce_feed_url(groups.get("namespace", ""), groups.get("site", ""))
    )
    if not isinstance(payload, list):
        raise ValueError("Dayforce feed did not return a list of postings")
    jobs = payload
    grouped: dict[str, list[dict[str, Any]]] = {}
    invalid_count = 0
    for raw in jobs:
        if not isinstance(raw, dict):
            invalid_count += 1
            continue
        url = str(raw.get("JobDetailsUrl") or "").strip()
        title = str(raw.get("Title") or "").strip()
        if not url or not title:
            invalid_count += 1
            continue
        posting_id = str(raw.get("ReferenceNumber") or "").strip()
        identity = f"posting:{posting_id}" if posting_id else f"url:{url.casefold()}"
        grouped.setdefault(identity, []).append(raw)
    listings = board_listings(
        DAYFORCE_MAPPING,
        ({**variants[0], "variants": variants} for variants in grouped.values()),
        employer,
    )
    collapsed = sum(len(variants) - 1 for variants in grouped.values())
    skip_reasons: dict[str, int] = {}
    if collapsed:
        skip_reasons["regional_variant_collapsed"] = collapsed
    if invalid_count:
        skip_reasons["missing_required_posting_identity"] = invalid_count
    return SourceRead(
        listings=listings,
        complete=invalid_count == 0,
        raw_count=len(jobs),
        skip_reasons=skip_reasons,
        outcome=(
            SourceReadOutcome.ATS_COMPLETE
            if invalid_count == 0
            else SourceReadOutcome.NO_SUPPORTED_REPRESENTATION
        ),
        note=(
            "Dayforce returned posting rows without a readable title or URL."
            if invalid_count
            else ""
        ),
    )


PLATFORM = BoardPlatform(
    name="dayforce",
    board_patterns=(DAYFORCE_BOARD,),
    adapter=_read,
    job_patterns=(DAYFORCE_JOB,),
    source_roots=((DAYFORCE_JOB, _root),),
    posting_api=True,
)
