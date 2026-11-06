"""Publisher search pages read in slices, with detail reads and recovery."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

import httpx

from clearscout.application.discovery.source_reading import (
    SourceContinuationTarget,
    SourceDetailCheckpoint,
    SourceDetailCoverage,
    SourceDetailSummary,
    SourceListing,
    SourceRead,
)
from clearscout.domain import SourceReadOutcome
from clearscout.domain.artifacts import PostingSource
from clearscout.domain.identity import normalize_fetch_url
from clearscout.domain.staged_facts import StagedListingFacts
from clearscout.infrastructure.web_fetch.board_mappings import BoardDeclaration
from clearscout.infrastructure.web_fetch.board_mappings.mapper import listing_summary
from clearscout.infrastructure.web_fetch.board_reads.continuations import (
    held_reason,
    page_continuation_target,
    retry_continuation,
    unreadable_continuation,
)
from clearscout.infrastructure.web_fetch.board_reads.page_fingerprints import (
    page_fingerprint,
    retained_page_fingerprints,
    with_page_fingerprints,
    with_query,
)
from clearscout.infrastructure.web_fetch.board_reads.payloads import distinct_by_id, row_id
from clearscout.infrastructure.web_fetch.board_reads.results import (
    BoardDeadlineReachedError,
    board_listings,
    robots_short,
)
from clearscout.infrastructure.web_fetch.board_reads.scope import publisher_search_scope
from clearscout.infrastructure.web_fetch.policy import FetchPolicyError, HostHeldError

if TYPE_CHECKING:
    from clearscout.infrastructure.web_fetch.board_platform import BoardTransport


@dataclass(frozen=True)
class PublisherSearchPage:
    """A declared search page and its explicit next-page boundary."""

    rows: list[dict[str, Any]]
    next_page: int | None
    total: int | None = None


@dataclass
class _SearchSlice:
    source_url: str
    reason: str
    target: SourceContinuationTarget | None
    enumeration_only: bool = False
    listing_urls: dict[str, str] = field(default_factory=dict)
    completed: dict[str, str] = field(default_factory=dict)
    listings: dict[str, SourceListing] = field(default_factory=dict)
    identities: set[str] = field(default_factory=set)
    deferred: list[SourceContinuationTarget] = field(default_factory=list)
    protected: list[str] = field(default_factory=list)
    reasons: dict[str, int] = field(default_factory=dict)
    detail: SourceDetailSummary = field(default_factory=SourceDetailSummary)
    fingerprints: list[str] = field(default_factory=list)
    fetched_pages: int = 0
    total: int | None = None
    unstable: bool = False
    pending: dict[str, dict[str, Any]] = field(default_factory=dict)
    detail_phase: Callable[[list[dict[str, Any]]], None] | None = None
    first_page: int = 0
    listing_pass: int = 1
    # Identities listed by the current relisted pass; empty on the first pass.
    pass_ids: set[str] = field(default_factory=set)
    listed_titles: bool = False
    # Title-only listings whose stated title the read's screen rules out, keyed by URL.
    set_aside: dict[str, SourceListing] = field(default_factory=dict)

    def listed(self) -> set[str]:
        """Return the identities the current listing pass has seen."""
        return self.pass_ids if self.listing_pass > 1 else self.identities

    def list_page(self, page: PublisherSearchPage) -> None:
        """Record one page's new identities; read row-carried bodies and hold the rest."""
        listed = distinct_by_id(page.rows)
        rows = [
            row
            for row in listed
            if row_id(row) not in self.identities
            or self.listing_urls.get(row_id(row)) != str(row["url"])
        ]
        for row in rows:
            item, url = str(row["id"]), str(row["url"])
            if self.listing_urls.get(item, url) != url:
                self.completed.pop(item, None)
                self.unstable |= item in self.listed()
            self.identities.add(item)
            self.listing_urls[item] = url
        if self.listing_pass > 1:
            self.pass_ids.update(row_id(row) for row in listed)
        now = [row for row in rows if self.enumeration_only or _row_carried(row)]
        held = {row_id(row) for row in rows} - {row_id(row) for row in now}
        self.pending.update((row_id(row), row) for row in rows if row_id(row) in held)
        if now and self.detail_phase is not None:
            self.detail_phase(now)

    def settle(self) -> None:
        """Run the detail phase over every listed row still awaiting its body."""
        rows, self.pending = list(self.pending.values()), {}
        if rows and self.detail_phase is not None:
            self.detail_phase(rows)

    def cursor(self, page: int) -> SourceContinuationTarget:
        # Each relisted pass gets its own address so the frontier never treats it as read.
        address = (
            with_query(self.source_url, __reader_page=page, __reader_pass=self.listing_pass)
            if self.listing_pass > 1
            else with_query(self.source_url, __reader_page=page)
        )
        target = page_continuation_target(
            address,
            source_url=self.source_url,
            label=f"Publisher search page {page}",
            reason=self.reason,
        )
        target = with_page_fingerprints(target, self.fingerprints)
        if target.detail_checkpoint is None:
            raise ValueError("Publisher search cursor lost its checkpoint.")
        return replace(
            target,
            detail_checkpoint=replace(
                target.detail_checkpoint,
                completed=tuple(self.completed.items()) if not self.enumeration_only else (),
                protected=tuple(
                    (item, url) for item, url in self.listing_urls.items() if url in self.protected
                ),
                listing_ids=tuple(sorted(self.identities)),
                listing_urls=tuple(sorted(self.listing_urls.items())),
                listing_total=self.total,
                listing_unstable=self.unstable,
                listing_pass=self.listing_pass,
                pass_listing_ids=tuple(sorted(self.pass_ids)),
            ),
        )

    def hold(self, page: int, reason: str, *, automatic: bool = True) -> SourceRead:
        """Settle listed rows, then retain the page cursor this read stopped at."""
        self.settle()
        self.reasons[reason] = self.reasons.get(reason, 0) + 1
        self.deferred.append(replace(self.cursor(page), automatic_eligible=automatic))
        return self.result(exhausted=False, held=True)

    def finish(self, page: int) -> SourceRead:
        """Settle listed rows and close the listing against its declared total."""
        self.settle()
        mismatch = self.total is not None and self.total > 0 and self.total != len(self.listed())
        if (mismatch or self.unstable) and self.relists():
            return self.relist(mismatch)
        if mismatch:
            if self.enumeration_only:
                read = self.hold(page, "publisher_listing_total_mismatch", automatic=False)
            else:
                self.reasons["publisher_listing_total_mismatch"] = 1
                read = self.result(exhausted=False)
            return replace(read, discovery_truncated=True, note=self.count_note())
        if self.enumeration_only and set(self.listing_urls) != self.identities:
            return self.hold(page, "publisher_listing_membership_missing", automatic=False)
        if self.listing_pass > 1 and not self.unstable:
            self.keep_pass_membership()
        return self.result(exhausted=True)

    def count_note(self) -> str:
        """State the publisher's total beside the identities this pass retained."""
        return (
            f"The publisher reports {self.total} listings, but this read retained "
            f"{len(self.listed())} distinct identities. "
            "Review the publisher search before retrying; coverage is incomplete."
        )

    def relists(self) -> bool:
        """Whether a listing stitched across tasks may be read again from its first page."""
        return self.target is not None and not self.enumeration_only and self.listing_pass < 3

    def relist(self, mismatch: bool) -> SourceRead:
        """Retain a fresh pass from the first page; only a third pass waits for a person."""
        note = (
            self.count_note()
            if mismatch
            else "The publisher listing changed during two listing passes. "
            "Read the search again from its first page to verify listings."
        )
        reason = "publisher_listing_total_mismatch" if mismatch else "publisher_listing_changed"
        self.fingerprints, self.total, self.unstable = [], None, False
        self.listing_pass, self.pass_ids = self.listing_pass + 1, set()
        automatic = self.listing_pass == 2
        if not automatic:
            self.reasons[reason] = 1
        self.deferred.append(replace(self.cursor(self.first_page), automatic_eligible=automatic))
        read = self.result(exhausted=False)
        return read if automatic else replace(read, note=note)

    def keep_pass_membership(self) -> None:
        """Narrow retained membership to the identities this relisted pass listed."""
        self.identities &= self.pass_ids
        self.listing_urls = {
            item: url for item, url in self.listing_urls.items() if item in self.pass_ids
        }
        self.completed = {
            item: url for item, url in self.completed.items() if item in self.pass_ids
        }
        current = set(self.listing_urls.values())
        self.protected = [url for url in self.protected if url in current]

    def _defer_unresolved(
        self, rows: list[dict[str, Any]], read: SourceRead, platform: str
    ) -> None:
        """Retain each listed row the detail read left unresolved as its own target."""
        checkpoints = [
            target.detail_checkpoint
            for target in read.deferred_targets
            if target.detail_checkpoint is not None
        ]
        failed_attempts = {
            item: attempts
            for checkpoint in checkpoints
            for item, attempts in checkpoint.failed_attempts
        }
        resolved = set(self.protected) | (set() if self.enumeration_only else set(self.listings))
        facts = {listing.requisition_id: listing.facts for listing in read.listings}
        # A robots-refused row leaves the detail checkpoint's pending set and is only counted.
        unresolved = {item for checkpoint in checkpoints for item in checkpoint.pending_ids}
        refused = "robots_disallowed" in read.skip_reasons
        for row in rows:
            item, url = str(row["id"]), str(row["url"])
            if url in resolved or (refused and item not in unresolved):
                continue
            attempts = failed_attempts.get(item, 0)
            self.deferred.append(
                replace(
                    page_continuation_target(
                        url,
                        source_url=self.source_url,
                        label=str(row.get("title") or item)[:300],
                        reason=platform + "_search_detail",
                    ),
                    automatic_eligible=not attempts,
                    detail_checkpoint=SourceDetailCheckpoint(
                        platform=platform,
                        board_url=url,
                        pending_ids=(item,),
                        listing_ids=(item,),
                        listing_urls=((item, url),),
                        failed_attempts=((item, attempts),) if attempts else (),
                        listing_facts=((item, facts[item]),) if item in facts else (),
                    ),
                )
            )

    def unscreened(
        self,
        fetcher: BoardTransport,
        mapping: BoardDeclaration,
        rows: list[dict[str, Any]],
        employer: str,
    ) -> list[dict[str, Any]]:
        """Keep rows whose listed title is ruled out as title-only listings; return the rest."""
        # A detail that declares role aliases could add a title the listed one lacks.
        if not self.listed_titles or mapping.role_aliases is not None:
            return rows
        aside = [
            row
            for row in rows
            if not _row_carried(row)
            and fetcher.rules_out_listed_title(str(row["url"]), str(row.get("title") or ""))
        ]
        if not aside:
            return rows
        self.set_aside.update(
            (listing.url, listing)
            for listing in _enumerated_search_read(mapping, aside, employer).listings
        )
        ruled_out = {row_id(row) for row in aside}
        return [row for row in rows if row_id(row) not in ruled_out]

    def consume(self, rows: list[dict[str, Any]], read: SourceRead, platform: str) -> None:
        """Apply one detail read and retain each unresolved listed row as its own target."""
        self.detail = self.detail.combined(read.detail)
        self.protected.extend(read.protected_posting_urls)
        for reason, count in read.skip_reasons.items():
            self.reasons[reason] = self.reasons.get(reason, 0) + count
        self.listings.update(
            (listing.url, listing)
            for listing in read.listings
            if self.enumeration_only or listing.published_description
        )
        self._defer_unresolved(rows, read, platform)
        proof = set(read.detail_read_urls or ())
        if not self.enumeration_only:
            self.completed.update(
                (listing.requisition_id, listing.url)
                for listing in read.listings
                if listing.published_description
                and listing.url in proof
                and self.listing_urls.get(listing.requisition_id) == listing.url
            )

    def result(self, *, exhausted: bool, held: bool = False) -> SourceRead:
        complete = exhausted and not self.deferred and not self.unstable and self.target is None
        # A held cursor carries instability to the finishing task, which relists instead.
        changed = self.unstable and not (
            held and not self.enumeration_only and self.listing_pass < 3
        )
        if changed:
            self.reasons["publisher_listing_changed"] = 1
        coverage = (
            SourceDetailCoverage(
                platform=self.reason.removesuffix("_search_page"),
                board_url=self.source_url,
                posting_urls=tuple(self.listing_urls.values()),
            )
            if self.enumeration_only and exhausted and not self.unstable
            else None
        )
        listing_ids = tuple(self.listing_urls)
        listing_urls = tuple(self.listing_urls.items())
        pass_ids = tuple(sorted(self.pass_ids))
        completed = tuple(self.completed.items()) if not self.enumeration_only else ()
        protected = tuple(
            (item, url) for item, url in self.listing_urls.items() if url in self.protected
        )
        listing: dict[str, Any] = {
            "completed": completed,
            "protected": protected,
            "page_fingerprints": tuple(self.fingerprints),
            "listing_ids": listing_ids,
            "listing_urls": listing_urls,
            "pass_listing_ids": pass_ids,
        }
        deferred = tuple(
            replace(
                target,
                detail_checkpoint=replace(
                    target.detail_checkpoint,
                    listing_total=self.total,
                    listing_unstable=self.unstable,
                    listing_pass=self.listing_pass,
                    # A detail row keeps its own pair; the cursor and receipt hold the listing.
                    **(
                        {}
                        if target.deferred_reason.endswith("_search_detail")
                        and not self.enumeration_only
                        else listing
                    ),
                ),
            )
            if (coverage is not None or not self.enumeration_only)
            and target.detail_checkpoint is not None
            else target
            for target in self.deferred
        )
        set_aside = [
            listing
            for url, listing in self.set_aside.items()
            if url not in self.listings
            and self.listing_urls.get(listing.requisition_id) == listing.url
        ]
        return robots_short(
            SourceRead(
                listings=[*self.listings.values(), *set_aside],
                complete=complete,
                raw_count=max(len(self.identities), len(self.listings)),
                outcome=SourceReadOutcome.ATS_COMPLETE
                if complete
                else SourceReadOutcome.CRAWL_CONTINUATION
                if self.deferred
                else SourceReadOutcome.PUBLISHER_LINKED_PAGES,
                skip_reasons=self.reasons,
                deferred_targets=deferred,
                protected_posting_urls=tuple(dict.fromkeys(self.protected)),
                detail=self.detail,
                detail_read_urls=tuple(self.completed.values())
                if not self.enumeration_only
                else None,
                attempted_target_urls=(self.target.url,) if self.target else None,
                discovery_truncated=changed,
                listing_coverage=coverage,
                listing_pages=self.fetched_pages,
                publisher_listing_receipts=(
                    SourceDetailCheckpoint(
                        platform="search:" + self.reason,
                        board_url=self.source_url,
                        completed=completed,
                        protected=protected,
                        listing_ids=listing_ids,
                        listing_urls=listing_urls,
                        page_fingerprints=tuple(self.fingerprints),
                        listing_total=self.total,
                        listing_unstable=self.unstable,
                        listing_pass=self.listing_pass,
                        pass_listing_ids=pass_ids,
                    ),
                )
                if not self.enumeration_only and self.fingerprints
                else (),
                exhausted_outcome=SourceReadOutcome.ATS_COMPLETE
                if self.deferred and exhausted and not self.unstable
                else None,
                note="Read publisher search identities without verifying full posting bodies."
                if self.enumeration_only
                else "Read publisher search identities and their available full descriptions.",
            )
        )


def search_page_number(target: SourceContinuationTarget, default: int) -> int:
    """Read the nonnegative internal page position of a retained search."""
    from urllib.parse import parse_qs

    value = int(parse_qs(urlsplit(target.url).query).get("__reader_page", [str(default)])[0])
    if value < 0:
        raise ValueError("Publisher search cursor must be nonnegative.")
    return value


def _search_slice(
    source_url: str, reason: str, target: SourceContinuationTarget | None
) -> _SearchSlice:
    from urllib.parse import parse_qs

    state = _SearchSlice(source_url, reason, target)
    state.fingerprints.extend(retained_page_fingerprints(target))
    if target is not None:
        query = parse_qs(urlsplit(target.url).query)
        total = query.get("__reader_total", [""])[0]
        state.total = int(total) if total else None
        state.unstable = query.get("__reader_unstable", ["0"])[0] == "1"
        if target.detail_checkpoint is not None:
            checkpoint = target.detail_checkpoint
            if checkpoint.platform != "search:" + reason or normalize_fetch_url(
                checkpoint.board_url
            ) != normalize_fetch_url(source_url):
                raise ValueError("Publisher search checkpoint does not match its declared source.")
            if checkpoint.listing_urls and publisher_search_scope(target) is None:
                raise ValueError("Publisher search checkpoint membership is outside its source.")
            state.listing_urls.update(checkpoint.listing_urls)
            if checkpoint.listing_urls:
                state.completed.update(checkpoint.completed)
                state.protected.extend(url for _item, url in checkpoint.protected)
            state.identities.update(target.detail_checkpoint.listing_ids)
            state.total = target.detail_checkpoint.listing_total
            state.unstable = target.detail_checkpoint.listing_unstable
            state.listing_pass = checkpoint.listing_pass
            state.pass_ids.update(checkpoint.pass_listing_ids)
    return state


def _search_membership(state: _SearchSlice, page: PublisherSearchPage) -> bool:
    ids = [str(row.get("id") or "") for row in page.rows]
    if any(not item or not row.get("url") for item, row in zip(ids, page.rows, strict=True)):
        raise ValueError("Publisher search rows must retain identity and detail address.")
    fingerprint = page_fingerprint(ids)
    # An empty closing page cannot hide a posting, so only pages with rows count.
    state.fetched_pages += bool(ids)
    if fingerprint in state.fingerprints:
        return False
    state.fingerprints.append(fingerprint)
    if state.listed().intersection(ids):
        state.unstable = True
    if page.total is not None and page.total > 0:
        state.unstable |= state.total is not None and state.total != page.total
        state.total = page.total
    return True


def _search_detail(
    detail: Callable[[dict[str, Any]], Mapping[str, Any] | None],
    indexed: dict[str, dict[str, Any]],
    item: str,
) -> Mapping[str, Any] | None:
    try:
        record = detail(indexed[item])
    except json.JSONDecodeError:
        return None
    if record is None:
        return None
    description = record.get("description")
    return record if isinstance(description, str) and description.strip() else None


def _row_carried(row: dict[str, Any]) -> bool:
    listed = row.get("detail")
    if not isinstance(listed, Mapping):
        return False
    description = listed.get("description")
    return isinstance(description, str) and bool(description.strip())


def _search_detail_phase(
    fetcher: BoardTransport,
    employer: str,
    state: _SearchSlice,
    rows: list[dict[str, Any]],
    *,
    mapping: BoardDeclaration,
    detail: Callable[[dict[str, Any]], Mapping[str, Any] | None],
) -> None:
    if state.enumeration_only:
        read = _enumerated_search_read(mapping, rows, employer)
    else:
        rows = state.unscreened(fetcher, mapping, rows, employer)
        if not rows:
            return
        read = fetcher.detailed_read(
            employer,
            rows,
            mapping=mapping,
            detail=partial(_search_detail, detail, {row_id(row): row for row in rows}),
            missing_fails=True,
            robots_skips=True,
            resume=(state.source_url, mapping.platform + "_search_detail"),
            records_listed=all(_row_carried(row) for row in rows),
        )
    state.consume(rows, read, mapping.platform)


def _enumerated_search_read(
    mapping: BoardDeclaration,
    rows: list[dict[str, Any]],
    employer: str,
) -> SourceRead:
    """Map declared list facts without claiming a description or reading a detail."""
    indexed = {row_id(row): row for row in rows}
    listings = board_listings(
        mapping,
        [
            {"row": row, "detail": {"title": row.get("title") or row_id(row), "employer": employer}}
            for row in rows
        ],
        employer,
    )
    listings = [
        replace(
            listing,
            title=str(indexed[listing.requisition_id].get("title") or ""),
            summary=listing_summary(str(indexed[listing.requisition_id].get("summary") or "")),
            source=PostingSource(
                publisher=mapping.platform,
                record=json.dumps(indexed[listing.requisition_id], ensure_ascii=False),
            ),
        )
        for listing in listings
    ]
    return SourceRead(
        listings=listings,
        complete=False,
        raw_count=len(rows),
        outcome=SourceReadOutcome.PUBLISHER_LINKED_PAGES,
    )


def _publisher_search_start(
    source_url: str,
    reason: str,
    target: SourceContinuationTarget | None,
    page: int,
    enumeration_only: bool,
) -> tuple[_SearchSlice, int] | SourceRead:
    try:
        state = _search_slice(source_url, reason, target)
    except ValueError:
        if target is None:
            raise
        return replace(
            unreadable_continuation(target, "invalid_publisher_search_scope"),
            outcome=SourceReadOutcome.CRAWL_CONTINUATION,
            deferred_targets=(replace(target, automatic_eligible=False),),
            note="The retained search scope is unverified. "
            "Scan the source again using its Scan control to verify listings. "
            "Reading deferred links cannot verify this retained scope.",
        )
    if (
        enumeration_only
        and reason == "alignerr_search_page"
        and state.identities
        and not state.listing_urls
    ):
        state = _SearchSlice(source_url, reason, target)
        page = 0
    state.enumeration_only = enumeration_only
    if enumeration_only:
        state.completed.clear()
    elif state.identities and not state.listing_urls:
        read = state.hold(page, "publisher_listing_membership_missing", automatic=False)
        return replace(
            read,
            note="The retained search identities lack current posting addresses. "
            "Scan the source again using its Scan control to verify listings "
            "and full posting bodies. "
            "Reading deferred links cannot recover these missing addresses.",
        )
    return state, page


def publisher_search_read(
    fetcher: BoardTransport,
    employer: str,
    *,
    source_url: str,
    reason: str,
    first_page: int,
    request: Callable[[int], PublisherSearchPage],
    mapping: BoardDeclaration,
    detail: Callable[[dict[str, Any]], Mapping[str, Any] | None],
    target: SourceContinuationTarget | None = None,
    enumeration_only: bool = False,
    listed_titles: bool = False,
) -> SourceRead:
    """List declared pages to explicit exhaustion, then read the listed details.

    ``listed_titles`` says each row's ``title`` is its posting's title, so the read's
    title screen may set a row aside without reading its detail.
    """
    page = search_page_number(target, first_page) if target is not None else first_page
    resumed = _publisher_search_start(source_url, reason, target, page, enumeration_only)
    if isinstance(resumed, SourceRead):
        return resumed
    state, page = resumed
    state.first_page = first_page
    state.listed_titles = listed_titles
    state.detail_phase = partial(
        _search_detail_phase, fetcher, employer, state, mapping=mapping, detail=detail
    )
    while not fetcher.deadline_elapsed():
        answer = _requested_search_page(fetcher, state, page, request)
        if isinstance(answer, SourceRead):
            return answer
        if answer is None:
            return state.hold(page, "repeated_publisher_page", automatic=False)
        state.list_page(answer)
        if not answer.rows or answer.next_page is None:
            return state.finish(page)
        if answer.next_page <= page:
            return state.hold(page, "invalid_publisher_page_cursor", automatic=False)
        page = answer.next_page
    return state.hold(page, "time_limit_reached")


def _requested_search_page(
    fetcher: BoardTransport,
    state: _SearchSlice,
    page: int,
    request: Callable[[int], PublisherSearchPage],
) -> PublisherSearchPage | SourceRead | None:
    """Request one page; None means the publisher repeated a page already listed."""
    try:
        answer: PublisherSearchPage = fetcher.paginated_request(
            partial(request, page), list(state.listings.values()), len(state.identities)
        )
        return answer if _search_membership(state, answer) else None
    except BoardDeadlineReachedError:
        return state.hold(page, "time_limit_reached")
    except HostHeldError as held:
        return state.hold(page, held_reason(held), automatic=held.paced)
    except (FetchPolicyError, httpx.HTTPError, ValueError):
        if not state.identities and state.target is None:
            raise
        return state.hold(page, "publisher_search_request_failed", automatic=False)


def publisher_search_detail(
    fetcher: BoardTransport,
    employer: str,
    target: SourceContinuationTarget,
    *,
    mapping: BoardDeclaration,
    detail: Callable[[dict[str, Any]], Mapping[str, Any] | None],
    row: dict[str, Any] | None = None,
) -> SourceRead:
    """Resume one exact failed detail with its publisher's retained stable identity."""
    checkpoint = target.detail_checkpoint
    if (
        checkpoint is None
        or checkpoint.platform != mapping.platform
        or len(checkpoint.pending_ids) != 1
        or (checkpoint.listing_urls and publisher_search_scope(target) is None)
    ):
        return unreadable_continuation(target, "invalid_publisher_detail_identity")
    row = row or {
        "id": checkpoint.pending_ids[0],
        "url": checkpoint.board_url,
        "title": target.label,
        "places": list(
            dict(checkpoint.listing_facts)
            .get(checkpoint.pending_ids[0], StagedListingFacts())
            .locations.values
        ),
    }
    read = fetcher.detailed_read(
        employer,
        [row],
        mapping=mapping,
        detail=partial(_search_detail, detail, {row_id(row): row}),
        missing_fails=True,
        resume=(checkpoint.board_url, target.deferred_reason),
        resume_target=target,
    )
    original = dict(checkpoint.listing_facts).get(checkpoint.pending_ids[0])
    if original is not None:
        read = replace(
            read,
            listings=[
                replace(
                    listing,
                    facts=replace(
                        listing.facts,
                        locations=original.locations
                        if not listing.facts.locations.values
                        else listing.facts.locations,
                    ),
                )
                for listing in read.listings
            ],
        )
    deferred = tuple(
        replace(
            item,
            url=target.url,
            source_url=target.source_url,
            automatic_eligible=target.automatic_eligible
            and (item.automatic_eligible or target.manual_detail_retry)
            and not read.detail.failed
            and "host_held" not in read.skip_reasons,
            detail_checkpoint=replace(
                item.detail_checkpoint,
                listing_facts=checkpoint.listing_facts,
                listing_ids=tuple(
                    identity
                    for identity in checkpoint.listing_ids
                    if identity in checkpoint.pending_ids
                ),
                listing_urls=tuple(
                    pair for pair in checkpoint.listing_urls if pair[0] in checkpoint.pending_ids
                ),
                listing_total=checkpoint.listing_total,
                listing_unstable=checkpoint.listing_unstable,
            )
            if item.detail_checkpoint
            else None,
        )
        for item in read.deferred_targets
    )
    return replace(
        read,
        complete=False,
        outcome=SourceReadOutcome.CRAWL_CONTINUATION
        if deferred
        else SourceReadOutcome.PUBLISHER_LINKED_PAGES,
        deferred_targets=deferred,
        detail_coverage=None,
        raw_count=max(read.raw_count, len(checkpoint.listing_ids)),
        detail_read_urls=tuple(
            dict.fromkeys(
                (
                    *(url for _item, url in checkpoint.completed),
                    *(read.detail_read_urls or ()),
                )
            )
        )
        if checkpoint.listing_urls
        else read.detail_read_urls,
        attempted_target_urls=(target.url,) if read.detail.attempted else (),
    )


def _recovery_identity(target: SourceContinuationTarget, platform: str) -> tuple[str | None, str]:
    checkpoint = target.detail_checkpoint
    if (
        checkpoint is None
        or checkpoint.platform != platform
        or len(checkpoint.pending_ids) != 1
        or checkpoint.board_url != target.url
    ):
        return None, "invalid_publisher_detail_identity"
    wanted = checkpoint.pending_ids[0]
    if checkpoint.listing_urls:
        if dict(checkpoint.listing_urls).get(wanted) != target.url:
            return None, "publisher_detail_identity_changed"
        if publisher_search_scope(target) is None:
            return None, "invalid_publisher_detail_identity"
    return wanted, ""


def _recovery_page_boundary(answer: PublisherSearchPage, page: int) -> int | str:
    if not answer.rows or answer.next_page is None:
        return "publisher_detail_identity_unavailable"
    if answer.next_page <= page:
        return "invalid_publisher_page_cursor"
    return answer.next_page


def publisher_search_recovery(
    fetcher: BoardTransport,
    employer: str,
    target: SourceContinuationTarget,
    *,
    first_page: int,
    request: Callable[[int], PublisherSearchPage],
    mapping: BoardDeclaration,
    detail: Callable[[dict[str, Any]], Mapping[str, Any] | None],
) -> SourceRead:
    """Locate one retained identity in its public search without reading other details."""
    wanted, refusal = _recovery_identity(target, mapping.platform)
    if wanted is None:
        return unreadable_continuation(target, refusal)
    page = first_page
    seen: set[str] = set()
    while not fetcher.deadline_elapsed():
        try:
            answer = fetcher.paginated_request(partial(request, page), [], 0)
        except (BoardDeadlineReachedError, FetchPolicyError, httpx.HTTPError, ValueError):
            return retry_continuation(target, target.deferred_reason)
        fingerprint = page_fingerprint(str(row["id"]) for row in answer.rows)
        if fingerprint in seen:
            return unreadable_continuation(target, "repeated_publisher_page")
        seen.add(fingerprint)
        row = next((row for row in answer.rows if str(row["id"]) == wanted), None)
        if row is not None:
            if row["url"] != target.url:
                return unreadable_continuation(target, "publisher_detail_identity_changed")
            return publisher_search_detail(
                fetcher, employer, target, mapping=mapping, detail=detail, row=row
            )
        boundary = _recovery_page_boundary(answer, page)
        if isinstance(boundary, str):
            return unreadable_continuation(target, boundary)
        page = boundary
    return retry_continuation(target, target.deferred_reason)


def publisher_search_or_recovery(
    fetcher: BoardTransport,
    employer: str,
    *,
    source_url: str,
    reason: str,
    first_page: int,
    request: Callable[[int], PublisherSearchPage],
    mapping: BoardDeclaration,
    detail: Callable[[dict[str, Any]], Mapping[str, Any] | None],
    target: SourceContinuationTarget | None = None,
    recovery: SourceContinuationTarget | None = None,
) -> SourceRead:
    """Recover one retained detail when ``recovery`` is given, else read or resume the search."""
    if recovery is not None:
        return publisher_search_recovery(
            fetcher,
            employer,
            recovery,
            first_page=first_page,
            request=request,
            mapping=mapping,
            detail=detail,
        )
    return publisher_search_read(
        fetcher,
        employer,
        source_url=source_url,
        reason=reason,
        first_page=first_page,
        request=request,
        mapping=mapping,
        detail=detail,
        target=target,
    )


class PublisherSearch(Protocol):
    """Read a board's publisher search from its source address, or resume it at ``target``."""

    def __call__(
        self,
        fetcher: BoardTransport,
        employer: str,
        source_url: str,
        target: SourceContinuationTarget | None = None,
        /,
    ) -> SourceRead:
        """Return the pages read, and the cursor for any page left to read."""
        ...


@dataclass(frozen=True)
class PublisherSearchBoard:
    """A board read only through its publisher search, with page and detail continuations."""

    search: PublisherSearch
    mapping: BoardDeclaration
    detail: Callable[[BoardTransport], Callable[[dict[str, Any]], Mapping[str, Any] | None]]

    def read(
        self, fetcher: BoardTransport, employer: str, _groups: dict[str, str], url: str, /
    ) -> SourceRead:
        """Read the search from the address the board was recognized at."""
        return self.search(fetcher, employer, url)

    def resume(
        self, fetcher: BoardTransport, employer: str, target: SourceContinuationTarget, /
    ) -> SourceRead:
        """Resume the search at a retained page cursor."""
        return self.search(fetcher, employer, target.source_url, target)

    def resume_detail(
        self, fetcher: BoardTransport, employer: str, target: SourceContinuationTarget, /
    ) -> SourceRead:
        """Retry the one posting detail a search read left pending."""
        return publisher_search_detail(
            fetcher, employer, target, mapping=self.mapping, detail=self.detail(fetcher)
        )


class RecoverableSearch(Protocol):
    """Read a board's search, resume it at ``target``, or recover one detail at ``recovery``."""

    def __call__(
        self,
        fetcher: BoardTransport,
        employer: str,
        groups: dict[str, str],
        url: str,
        /,
        *,
        target: SourceContinuationTarget | None = None,
        recovery: SourceContinuationTarget | None = None,
    ) -> SourceRead:
        """Return what the search or the recovery read."""
        ...


@dataclass(frozen=True)
class RecoverableSearchBoard:
    """Continuations for a board whose own adapter both pages its search and recovers details.

    ``groups`` rebuilds the adapter's match groups from a retained target's source address.
    """

    read: RecoverableSearch
    groups: Callable[[SourceContinuationTarget], dict[str, str]]

    def resume(
        self, fetcher: BoardTransport, employer: str, target: SourceContinuationTarget, /
    ) -> SourceRead:
        """Reload the board's search configuration, then resume at a retained page cursor."""
        return self.read(fetcher, employer, self.groups(target), target.source_url, target=target)

    def resume_detail(
        self, fetcher: BoardTransport, employer: str, target: SourceContinuationTarget, /
    ) -> SourceRead:
        """Reload the board's search configuration, then recover one retained detail."""
        return self.read(fetcher, employer, self.groups(target), target.source_url, recovery=target)
