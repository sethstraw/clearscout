"""Phenom career sites, named by page markup and read through their search widget."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

import httpx

from tailorcv.application.discovery.source_reading import SourceDetailCheckpoint
from tailorcv.domain import SourceReadOutcome
from tailorcv.domain.identity import normalize_fetch_url
from tailorcv.infrastructure.web_fetch.board_mappings import PHENOM_MAPPING
from tailorcv.infrastructure.web_fetch.board_platform import BoardPlatform
from tailorcv.infrastructure.web_fetch.board_reads import (
    MAX_API_RESPONSE_BYTES,
    PageAnswer,
    board_listings,
    continued_board_page,
    deferred_page,
    page_continuation_target,
    page_markup,
    paged_board_read,
    unreadable_continuation,
    with_query,
)
from tailorcv.infrastructure.web_fetch.ingest.careers import inspect_publisher_page
from tailorcv.infrastructure.web_fetch.policy import FetchPolicyError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from tailorcv.application.discovery.source_reading import (
        SourceContinuationTarget,
        SourceListing,
        SourceRead,
    )
    from tailorcv.infrastructure.web_fetch.board_platform import BoardTransport

# Phenom Career Connect sets one page-scoped object, and the site reference the
# widget API needs is a field inside it.
PHENOM_BOARD = re.compile(
    r"phApp\.ddo\s*=[\s\S]{0,20000}?\"refNum\"\s*:\s*\"(?P<ref>[A-Za-z0-9._-]{2,64})\""
)

# The widget honors a size of 100, so a board of a few hundred openings reads
# inside one task, which narrows the window for the listing order to drift.
PHENOM_PAGE = 100
PHENOM_MAX_PAGES = 100
_URL_MAP = re.compile(r"phApp\.urlMap\s*=\s*")
_PENDING = "phenom_page_pending"


def _search_body(reference: str, offset: int, size: int) -> dict[str, Any]:
    return {
        "ddoKey": "refineSearch",
        "refNum": reference,
        "siteType": "external",
        "lang": "en_us",
        "deviceType": "desktop",
        "pageName": "search-results",
        "from": offset,
        "size": size,
        "jobs": True,
        "counts": True,
        "keywords": "",
        "all_fields": [],
    }


def _new_listing_progress(
    checkpoint: SourceDetailCheckpoint,
    listings: list[SourceListing],
    records: int,
    *,
    bounded_next: bool,
) -> bool:
    known = dict(checkpoint.listing_urls)
    page = {listing.requisition_id: listing.url for listing in listings if listing.requisition_id}
    digest = hashlib.sha256(json.dumps(sorted(page.items())).encode()).hexdigest()
    return (
        len(page) == len(listings)
        and all(identity not in known or known[identity] == url for identity, url in page.items())
        and (
            bool(page.keys() - known.keys())
            or (
                bool(page)
                and records == len(page)
                and bounded_next
                and digest not in checkpoint.page_fingerprints
            )
        )
    )


def _page_checkpoint(
    checkpoint: SourceDetailCheckpoint, listings: list[SourceListing], records: int, total: int
) -> SourceDetailCheckpoint:
    membership = dict(checkpoint.listing_urls)
    page = {listing.requisition_id: listing.url for listing in listings if listing.requisition_id}
    digest = hashlib.sha256(json.dumps(sorted(page.items())).encode()).hexdigest()
    unstable = (
        checkpoint.listing_unstable
        or records != len(page)
        or bool(set(page) & membership.keys())
        or (bool(records) and digest in checkpoint.page_fingerprints)
        or (checkpoint.listing_total is not None and checkpoint.listing_total != total)
    )
    membership.update(page)
    return replace(
        checkpoint,
        listing_ids=tuple(membership),
        listing_urls=tuple(membership.items()),
        listing_total=total,
        listing_unstable=unstable,
        page_fingerprints=(*checkpoint.page_fingerprints, digest),
    )


def _checkpoint_read(
    read: SourceRead,
    checkpoint: SourceDetailCheckpoint,
    recovery: SourceContinuationTarget,
    *,
    initial: bool = False,
    allow_unstable_progress: bool = False,
) -> SourceRead:
    if read.outcome is SourceReadOutcome.NO_SUPPORTED_REPRESENTATION:
        return read
    listings = list(
        {listing.requisition_id or listing.url: listing for listing in read.listings}.values()
    )
    missing = read.skip_reasons.get("missing_required_posting_identity", 0)
    raw_count = max(len(checkpoint.listing_ids), len(listings), read.raw_count if missing else 0)
    certified = (
        initial
        and read.complete
        and not checkpoint.listing_unstable
        and checkpoint.listing_total == len(checkpoint.listing_ids)
    )
    if (
        not read.deferred_targets
        and not certified
        and (
            checkpoint.listing_total != len(checkpoint.listing_ids)
            or (checkpoint.listing_ids and not checkpoint.listing_total)
        )
    ):
        checkpoint = replace(checkpoint, listing_unstable=True)
    deferred = read.deferred_targets
    reasons = dict(read.skip_reasons)
    note = read.note
    if checkpoint.listing_unstable:
        if not missing:
            reasons["phenom_listing_unstable"] = 1
        if not deferred:
            deferred = (recovery,)
            allow_unstable_progress = False
        note = " ".join(
            part
            for part in (
                note,
                "The publisher listing identities or total changed; scan again to verify coverage.",
            )
            if part
        )
    deferred = tuple(
        replace(
            target,
            detail_checkpoint=checkpoint,
            automatic_eligible=target.automatic_eligible
            and (bool(missing) or not checkpoint.listing_unstable or allow_unstable_progress),
        )
        for target in deferred
    )
    return replace(
        read,
        listings=listings,
        raw_count=raw_count,
        complete=certified,
        outcome=SourceReadOutcome.ATS_COMPLETE
        if certified
        else SourceReadOutcome.CRAWL_CONTINUATION
        if deferred
        else SourceReadOutcome.NO_SUPPORTED_REPRESENTATION
        if missing
        else SourceReadOutcome.PUBLISHER_LINKED_PAGES,
        deferred_targets=deferred,
        exhausted_outcome=None,
        skip_reasons=reasons,
        note=note,
    )


def _initial_read(
    fetcher: BoardTransport,
    employer: str,
    source_url: str,
    endpoint: str,
    reference: str,
    route: dict[str, str],
) -> tuple[SourceRead, SourceDetailCheckpoint, bool]:
    checkpoint = SourceDetailCheckpoint(
        platform=f"search:phenom_page:{reference}", board_url=source_url
    )
    observed: list[SourceListing] = []
    record_count = 0
    progress = False
    stalled = False

    def answered(payload: Any) -> PageAnswer:
        nonlocal checkpoint, record_count, progress, stalled
        mapped, records, total = _answered(employer, route, payload)
        progress = _new_listing_progress(
            checkpoint,
            mapped,
            records,
            bounded_next=records >= PHENOM_PAGE
            and (len(checkpoint.page_fingerprints) + 1) * PHENOM_PAGE < total,
        )
        stalled = bool(checkpoint.listing_urls) and bool(mapped) and not progress
        checkpoint = _page_checkpoint(checkpoint, mapped, records, total)
        observed.extend(mapped)
        record_count += records
        return mapped, records, total

    def pages() -> Iterable[tuple[Callable[[], Any], SourceContinuationTarget]]:
        for offset in range(0, PHENOM_MAX_PAGES * PHENOM_PAGE, PHENOM_PAGE):
            yield (
                partial(fetcher.post_json, endpoint, _search_body(reference, offset, PHENOM_PAGE)),
                _cursor(endpoint, source_url, offset, PHENOM_PAGE, reference, route),
            )
            if stalled:
                return

    ceiling = _cursor(
        endpoint, source_url, PHENOM_MAX_PAGES * PHENOM_PAGE, PHENOM_PAGE, reference, route
    )
    try:
        read = paged_board_read(
            fetcher,
            pages(),
            answered,
            ceiling,
        )
    except (FetchPolicyError, httpx.HTTPError):
        if not observed:
            raise
        cursor = _cursor(
            endpoint,
            source_url,
            len(checkpoint.page_fingerprints) * PHENOM_PAGE,
            PHENOM_PAGE,
            reference,
            route,
        )
        read = replace(
            deferred_page(
                observed,
                record_count,
                cursor,
                listing_pages=len(checkpoint.page_fingerprints),
            ),
            note="A publisher search page could not be read; retained listings remain available.",
        )
    if stalled and read.deferred_targets:
        read = replace(
            read,
            deferred_targets=(
                _cursor(
                    endpoint,
                    source_url,
                    len(checkpoint.page_fingerprints) * PHENOM_PAGE,
                    PHENOM_PAGE,
                    reference,
                    route,
                ),
            ),
        )
    return read, checkpoint, progress


def _read(fetcher: BoardTransport, employer: str, groups: dict[str, str], url: str) -> SourceRead:
    """Read one Phenom career site through the widget its search page reads.

    The site reports how many openings it holds, so the loop is bounded by
    what it says it has, and a site larger than the page ceiling comes back
    marked incomplete rather than looking like a small board that answered
    everything.
    """
    host, reference = urlsplit(url).netloc, groups.get("ref", "")
    endpoint = f"https://{host}/widgets?ddoKey=refineSearch"
    source_url = url
    route = _job_route(fetcher, url)
    read, checkpoint, progress = _initial_read(
        fetcher, employer, source_url, endpoint, reference, route
    )
    mapped = _mapped_read(read)
    if mapped is not read and read.deferred_targets:
        retry_route = {**route, "identity_records": str(read.raw_count)}
        retry = _cursor(endpoint, source_url, 0, PHENOM_PAGE, reference, retry_route)
        deferred: tuple[SourceContinuationTarget, ...] = (retry,)
        note = mapped.note
        if read.skip_reasons.get("host_held"):
            deferred = (
                *read.deferred_targets,
                replace(retry, automatic_eligible=False),
            )
            note = (
                f"{mapped.note} {read.note} "
                "Scan again is required to verify complete board coverage after identity recovery."
            )
        mapped = replace(
            mapped,
            outcome=SourceReadOutcome.CRAWL_CONTINUATION,
            deferred_targets=deferred,
            exhausted_outcome=None,
            note=note,
        )
    recovery = _cursor(
        endpoint,
        source_url,
        len(checkpoint.page_fingerprints) * PHENOM_PAGE,
        PHENOM_PAGE,
        reference,
        route,
    )
    return _checkpoint_read(
        mapped, checkpoint, recovery, initial=True, allow_unstable_progress=progress
    )


def _continued_checkpoint(
    fetcher: BoardTransport, target: SourceContinuationTarget, reference: str, offset: int
) -> SourceDetailCheckpoint | None:
    checkpoint = target.detail_checkpoint
    cursor, source = urlsplit(target.url), urlsplit(target.source_url)
    if (cursor.scheme, cursor.netloc) != (
        source.scheme,
        source.netloc,
    ) or cursor.path != "/widgets":
        return None
    if checkpoint is not None:
        if checkpoint.completed:
            return None
        if checkpoint.platform != f"search:phenom_page:{reference}" or normalize_fetch_url(
            checkpoint.board_url
        ) != normalize_fetch_url(target.source_url):
            return None
        return checkpoint
    page = page_markup(fetcher.get_text(target.source_url, max_bytes=MAX_API_RESPONSE_BYTES))
    declared = PHENOM_BOARD.search(page)
    if declared is None or declared.group("ref") != reference:
        return None
    return SourceDetailCheckpoint(
        platform=f"search:phenom_page:{reference}",
        board_url=target.source_url,
        listing_unstable=offset != 0,
    )


def _continue(
    fetcher: BoardTransport, employer: str, target: SourceContinuationTarget
) -> SourceRead:
    """Read one retained Phenom page and carry its next offset."""
    parsed = urlsplit(target.url)
    query = parse_qs(parsed.query)
    try:
        offset = int(query.get("offset", [""])[0])
        limit = int(query.get("limit", [str(PHENOM_PAGE)])[0])
        reference = query.get("ref", [""])[0]
    except ValueError:
        return replace(unreadable_continuation(target, "invalid_phenom_cursor"), raw_count=0)
    if offset < 0 or limit <= 0 or not reference:
        return replace(unreadable_continuation(target, "invalid_phenom_cursor"), raw_count=0)
    endpoint = with_query(
        urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")),
        ddoKey="refineSearch",
    )
    retained = _continued_checkpoint(fetcher, target, reference, offset)
    if retained is None:
        return replace(unreadable_continuation(target, "phenom_scope_mismatch"), raw_count=0)
    checkpoint = retained
    progress = False
    route = {key: query.get(key, [""])[0] for key in ("job_route", "route_base")}
    try:
        same_host = not route["route_base"] or urlsplit(route["route_base"]).netloc == parsed.netloc
    except ValueError:
        same_host = False
    if not same_host:
        route = {}
    if not route.get("job_route") or not route.get("route_base"):
        route = _job_route(fetcher, target.source_url)

    def answered(payload: Any) -> PageAnswer:
        nonlocal checkpoint, progress
        mapped, records, total = _answered(employer, route, payload)
        progress = _new_listing_progress(
            checkpoint,
            mapped,
            records,
            bounded_next=records >= limit and offset + records < total,
        )
        checkpoint = _page_checkpoint(checkpoint, mapped, records, total)
        return mapped, records, total

    read = continued_board_page(
        target,
        partial(fetcher.post_json, endpoint, _search_body(reference, offset, limit)),
        answered,
        lambda records, total: (
            replace(
                _cursor(endpoint, target.source_url, offset + records, limit, reference, route),
                automatic_eligible=target.automatic_eligible,
            )
            if records and records >= limit and (not total or offset + records < total)
            else None
        ),
        pending=_PENDING,
        retain_record_counts=True,
    )
    if read.skip_reasons.get("host_held") or read.skip_reasons.get("fetch_failed"):
        return replace(read, raw_count=0)
    identity_records = query.get("identity_records", [""])[0]
    if identity_records.isdigit() and not read.raw_count:
        return replace(
            read,
            outcome=SourceReadOutcome.NO_SUPPORTED_REPRESENTATION,
            deferred_targets=(),
            note=(
                "TailorCV could not read a title and posting link from any of "
                f"the board's {identity_records} records. Try adding an individual posting link."
            ),
        )
    recovery = _cursor(endpoint, target.source_url, offset + limit, limit, reference, route)
    progress = (
        bool(retained.page_fingerprints)
        and bool(retained.listing_urls)
        and target.automatic_eligible
        and progress
    )
    return _checkpoint_read(
        _mapped_read(read), checkpoint, recovery, allow_unstable_progress=progress
    )


def _job_route(fetcher: BoardTransport, url: str) -> dict[str, str]:
    """Read the declared job route and the career page directory it belongs to."""
    try:
        page = fetcher.get_text(url, max_bytes=MAX_API_RESPONSE_BYTES)
    except (FetchPolicyError, httpx.HTTPError):
        return {}
    markup = page_markup(page)
    assigned = _URL_MAP.search(markup)
    if assigned is None:
        return {}
    try:
        routes, _end = json.JSONDecoder().raw_decode(markup, assigned.end())
    except json.JSONDecodeError:
        return {}
    route = routes.get("job") if isinstance(routes, dict) else None
    if not isinstance(route, str):
        return {}
    try:
        first_line = page.split("\n", 1)[0]
        landed = first_line if first_line.startswith(("https://", "http://")) else url
        canonical = inspect_publisher_page(landed, markup).canonical_url
        if not canonical:
            return {}
        parsed = urlsplit(canonical)
        source = urlsplit(url)
        if any(
            (candidate.scheme, candidate.netloc) != (source.scheme, source.netloc)
            for candidate in (urlsplit(landed), parsed)
        ):
            return {}
        if not parsed.path or parsed.path.endswith("/"):
            base_path = parsed.path or "/"
        else:
            page_routes = sorted(
                (
                    value
                    for key, value in routes.items()
                    if key != "job"
                    and isinstance(value, str)
                    and re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", value)
                    and parsed.path.endswith("/" + value)
                ),
                key=len,
                reverse=True,
            )
            if not page_routes:
                return {}
            base_path = parsed.path[: -len(page_routes[0])]
        return {
            "job_route": route,
            "route_base": urlunsplit((parsed.scheme, parsed.netloc, base_path, "", "")),
        }
    except ValueError:
        return {}


def _mapped_read(read: SourceRead) -> SourceRead:
    """Retain usable Phenom postings without claiming coverage of missing identities."""
    missing = read.skip_reasons.get("missing_required_posting_identity", 0)
    if not missing:
        return read
    scope = (
        f"any of the board's {read.raw_count} records"
        if missing == read.raw_count
        else f"{missing} of the board's {read.raw_count} records"
    )
    return replace(
        read,
        complete=False,
        outcome=SourceReadOutcome.NO_SUPPORTED_REPRESENTATION,
        deferred_targets=(),
        exhausted_outcome=None,
        note=(
            f"TailorCV could not read a title and posting link from {scope}. "
            "Try adding an individual posting link."
        ),
    )


def _answered(employer: str, route: dict[str, str], payload: Any) -> PageAnswer:
    """Return one search page's listings, its record count, and the total the site states."""
    answered = payload.get("refineSearch") or {}
    data = answered.get("data") or {}
    jobs = data.get("jobs") or []
    total = int(data.get("totalHits") or answered.get("totalHits") or 0)
    return board_listings(PHENOM_MAPPING, jobs, employer, route), len(jobs), total


def _cursor(
    endpoint: str, source_url: str, offset: int, limit: int, reference: str, route: dict[str, str]
) -> SourceContinuationTarget:
    return page_continuation_target(
        with_query(
            endpoint, ddoKey="refineSearch", offset=offset, limit=limit, ref=reference, **route
        ),
        source_url=source_url,
        label=f"Phenom page at offset {offset}",
        reason=_PENDING,
    )


PLATFORM = BoardPlatform(
    name="phenom",
    board_patterns=(PHENOM_BOARD,),
    adapter=_read,
    single_tenant=False,
    continuations={_PENDING: _continue},
)
