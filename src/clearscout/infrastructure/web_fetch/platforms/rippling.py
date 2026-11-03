"""Rippling ATS boards: the paged job list joined to each job's detail."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import httpx

from clearscout.domain.identity import normalize_fetch_url
from clearscout.infrastructure.web_fetch.board_mappings import (
    RIPPLING_MAPPING,
    rippling_detail_url,
    rippling_job,
    rippling_jobs_url,
)
from clearscout.infrastructure.web_fetch.board_platform import HOST_START, BoardPlatform, host_match
from clearscout.infrastructure.web_fetch.board_reads import (
    held_reason,
    required_list_field,
    row_id,
    short_of_total,
    unreadable_continuation,
)
from clearscout.infrastructure.web_fetch.policy import HostHeldError

if TYPE_CHECKING:
    from clearscout.application.discovery.source_reading import SourceContinuationTarget, SourceRead
    from clearscout.infrastructure.web_fetch.board_platform import BoardTransport

RIPPLING_JOB = re.compile(
    HOST_START + r"ats\.rippling\.com/(?P<org>[A-Za-z0-9_-]+)/jobs/(?P<job>[0-9a-f-]{36})(?![\w-])",
    re.IGNORECASE,
)
RIPPLING_BOARD = re.compile(
    HOST_START + r"ats\.rippling\.com/(?P<org>[A-Za-z0-9_-]+)/jobs(?![\w-])", re.IGNORECASE
)

RIPPLING_MAX_PAGES = 100
_PENDING = "rippling_detail_rows_pending"


def _page_matches(
    payload: dict[str, Any], *, page: int, total: Any, pages: Any, page_size: Any
) -> bool:
    """Check a page's position and metadata against the advertised board window."""
    expected = {"page": page, "totalItems": total, "totalPages": pages}
    if any(
        type(payload.get(key)) is not int or payload.get(key) != value
        for key, value in expected.items()
    ):
        return False
    size = payload.get("pageSize")
    return size == page_size and ("pageSize" not in payload or (type(size) is int and size > 0))


def _page_rows(payload: Any) -> list[Any] | None:
    """Return an advertised page's rows, retaining malformed envelopes as a gap."""
    try:
        return required_list_field(payload, "items", "Rippling")
    except ValueError:
        return None


def _page_limit(total: Any, pages: Any) -> tuple[int, list[str]]:
    """Validate board totals and retain the existing page guard."""
    problems: list[str] = []
    if type(total) is not int or total < 0:
        problems.append("The Rippling board did not state a valid totalItems.")
    if type(pages) is not int or pages < 0 or (pages == 0 and total != 0):
        problems.append("The Rippling board did not state a valid totalPages.")
    elif pages > RIPPLING_MAX_PAGES:
        problems.append("The Rippling board exceeds the supported page window.")
    limit = min(pages, RIPPLING_MAX_PAGES) if type(pages) is int and pages > 0 else 1
    return limit, problems


def _later_page(fetcher: BoardTransport, org: str, page: int) -> tuple[Any, str]:
    """Classify a recoverable later-page interruption without handling cancellation."""
    try:
        return fetcher.get_json(rippling_jobs_url(org, page)), ""
    except HostHeldError as held:
        return None, held_reason(held)
    except httpx.TimeoutException:
        return None, "time_limit_reached" if fetcher.deadline_elapsed() else "fetch_failed"
    except (httpx.HTTPError, json.JSONDecodeError):
        return None, "fetch_failed"


def _page_window(fetcher: BoardTransport, org: str) -> tuple[list[Any], Any, list[str], str, int]:
    """Read the advertised page window, returning its rows, gaps and fetched page count."""
    payload = fetcher.get_json(rippling_jobs_url(org))
    required_list_field(payload, "items", "Rippling")
    total = payload.get("totalItems")
    pages = payload.get("totalPages")
    page_size = payload.get("pageSize")
    limit, problems = _page_limit(total, pages)
    rows: list[Any] = []
    seen: set[tuple[str, ...]] = set()
    interruption = ""
    fetched = 1
    for page in range(limit):
        if page:
            payload, interruption = _later_page(fetcher, org, page)
            if interruption:
                problems.append(f"The Rippling board could not finish page {page}: {interruption}.")
                break
            fetched += 1
        page_rows = _page_rows(payload)
        if page_rows is None:
            problems.append(f"The Rippling board did not return its advertised page {page}.")
            continue
        if not _page_matches(payload, page=page, total=total, pages=pages, page_size=page_size):
            problems.append(f"The Rippling board returned inconsistent metadata on page {page}.")
        signature = tuple(sorted(json.dumps(row, sort_keys=True) for row in page_rows))
        if page_rows and signature in seen:
            problems.append(f"The Rippling board repeated a whole page at page {page}.")
        seen.add(signature)
        if not page_rows and (total != 0 or pages not in (0, 1)):
            problems.append(f"The Rippling board returned an empty advertised page at page {page}.")
        rows.extend(page_rows)
    return rows, total, problems, interruption, fetched


def _row_conflicts(original: dict[str, Any], row: dict[str, Any]) -> list[str]:
    """Name fields that disagree for one job outside its location declarations."""
    return sorted(
        key
        for key in original.keys() | row.keys()
        if key != "locations"
        and (key not in original or key not in row or original[key] != row[key])
    )


def _location_group(group: list[dict[str, Any]]) -> tuple[dict[str, Any], bool, list[str]]:
    """Merge consistent location variants for one job while retaining conflict diagnostics."""
    original = group[0]
    places: list[dict[str, Any]] = []
    signatures: set[tuple[str, ...]] = set()
    unlocated = False
    problems: list[str] = []
    for row in group:
        changed = _row_conflicts(original, row)
        if changed:
            problems.append(
                f"The Rippling job {row_id(original)} has conflicting fields: {', '.join(changed)}."
            )
            continue
        locations = row.get("locations")
        if locations is not None and (
            not isinstance(locations, list)
            or any(not isinstance(place, dict) for place in locations)
        ):
            problems.append(
                f"The Rippling job {row_id(original)} returned invalid location records."
            )
            continue
        unlocated = unlocated or not locations
        signatures.add(
            tuple(sorted(json.dumps(place, sort_keys=True) for place in locations or []))
        )
        for place in locations or []:
            if place not in places:
                places.append(place)
    merged = dict(original)
    # Stored raw.json artifacts carry this key under the former name, and they are immutable.
    merged["_tailorcv_rippling_locations_complete"] = not problems
    if "locations" in original or places:
        merged["locations"] = places
    return merged, not problems and not unlocated and len(signatures) > 1, problems


def _location_rows(rows: list[Any]) -> tuple[list[Any], bool, list[str]]:
    """Assemble stable job records and check each repeated ID's location-row proof."""
    kept: list[Any] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    positions: dict[str, int] = {}
    problems: list[str] = []
    for row in rows:
        job_id = row_id(row)
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not job_id:
            problems.append("The Rippling board returned a row without a valid job ID.")
            kept.append(row)
            continue
        if job_id not in grouped:
            grouped[job_id] = []
            positions[job_id] = len(kept)
            kept.append(row)
        grouped[job_id].append(row)
    repeated = False
    location_variants = True
    for job_id, group in grouped.items():
        merged, variants, row_problems = _location_group(group)
        kept[positions[job_id]] = merged
        problems.extend(row_problems)
        if len(group) > 1:
            repeated = True
            location_variants = location_variants and variants
    return kept, repeated and location_variants, problems


def _remap_partial_details(
    target: SourceContinuationTarget | None, board_url: str, rows: list[Any]
) -> SourceContinuationTarget | None:
    """Re-read affected resolved rows once after their partial enumeration is repaired."""
    checkpoint = target.detail_checkpoint if target else None
    if (
        target is None
        or checkpoint is None
        or not checkpoint.listing_unstable
        or checkpoint.platform != "rippling"
        or normalize_fetch_url(checkpoint.board_url) != normalize_fetch_url(board_url)
    ):
        return target
    current = {row_id(row) for row in rows}
    remap = {item for item, _url in checkpoint.completed if item in current}
    return replace(
        target,
        detail_checkpoint=replace(
            checkpoint,
            completed=tuple((item, url) for item, url in checkpoint.completed if item not in remap),
            pending_ids=tuple(sorted(set(checkpoint.pending_ids) | remap)),
            listing_unstable=False,
        ),
    )


def _partial_location_facts(read: SourceRead, rows: list[Any]) -> SourceRead:
    """Preserve known places while leaving omitted publisher locations explicitly unresolved."""
    partial = {
        row_id(row)
        for row in rows
        if isinstance(row, dict) and row.get("_tailorcv_rippling_locations_complete") is False
    }
    reason = "Additional publisher locations could not be verified."
    listings = []
    for listing in read.listings:
        retained = listing
        if listing.requisition_id in partial:
            locations = listing.facts.locations
            retained = replace(
                listing,
                facts=replace(
                    listing.facts,
                    locations=replace(
                        locations,
                        complete=False,
                        unresolved_values=tuple(
                            dict.fromkeys((*locations.unresolved_values, reason))
                        ),
                    ),
                ),
            )
        listings.append(retained)
    return replace(read, listings=listings)


def _interrupted_window(read: SourceRead, reason: str) -> SourceRead:
    """Keep a failed page's re-list cursor and distinguish a host hold from an attempt."""
    targets = tuple(
        replace(target, automatic_eligible=False) if reason == "host_held" else target
        for target in read.deferred_targets
    )
    return replace(
        read,
        skip_reasons={**read.skip_reasons, reason: 1},
        deferred_targets=targets,
    )


def _read(
    fetcher: BoardTransport,
    employer: str,
    groups: dict[str, str],
    _url: str,
    *,
    target: SourceContinuationTarget | None = None,
) -> SourceRead:
    """Read each listed job once, retaining all of its publisher location variants."""
    org = groups.get("org", "")
    board_url = f"https://ats.rippling.com/{org}/jobs"
    rows, total, page_problems, interruption, fetched = _page_window(fetcher, org)
    distinct, location_variants, row_problems = _location_rows(rows)
    found = len({row_id(row) for row in distinct} - {""})
    problems = page_problems + row_problems
    enumeration_complete = not problems and (
        found == total or (location_variants and len(rows) == total)
    )
    if enumeration_complete:
        target = _remap_partial_details(target, board_url, distinct)
    else:
        for row in distinct:
            if isinstance(row, dict):
                row["_tailorcv_rippling_locations_complete"] = False
    read = fetcher.detailed_read(
        employer,
        distinct,
        mapping=RIPPLING_MAPPING,
        detail=lambda job_id: rippling_job(fetcher.get_json(rippling_detail_url(org, job_id))),
        groups={"org": org},
        missing_fails=True,
        resume=(board_url, _PENDING),
        resume_target=target,
        enumeration_complete=enumeration_complete,
        relist_required=bool(interruption),
    )
    # A detail continuation relists the same window, so only the root read counts its pages.
    read = replace(
        _partial_location_facts(read, distinct), listing_pages=fetched if target is None else 0
    )
    if interruption:
        read = _interrupted_window(read, interruption)
    if enumeration_complete:
        return read
    return short_of_total(
        read,
        " ".join(
            dict.fromkeys(
                [
                    f"The Rippling board states {total} items and returned "
                    f"{len(rows)} rows for {found} distinct jobs.",
                    *problems,
                ]
            )
        ),
    )


def _continue(
    fetcher: BoardTransport, employer: str, target: SourceContinuationTarget
) -> SourceRead:
    """Re-list the board and resume its unresolved stable detail identities."""
    match = host_match(RIPPLING_BOARD, target.url)
    if match is None:
        return unreadable_continuation(target, "invalid_rippling_cursor")
    return _read(fetcher, employer, match.groupdict(default=""), target.url, target=target)


PLATFORM = BoardPlatform(
    name="rippling",
    board_patterns=(RIPPLING_BOARD,),
    adapter=_read,
    job_patterns=(RIPPLING_JOB,),
    source_roots=(
        (RIPPLING_JOB, lambda _url, match: f"https://ats.rippling.com/{match.group('org')}/jobs"),
    ),
    employer_slug=True,
    continuations={_PENDING: _continue},
)
