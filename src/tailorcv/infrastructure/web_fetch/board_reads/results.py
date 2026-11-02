"""Board read results: whole, bounded, and short-of-total reads of mapped listings."""

from __future__ import annotations

from collections.abc import Iterable
from contextvars import ContextVar
from dataclasses import replace
from typing import Any

from tailorcv.application.discovery.source_reading import (
    SourceContinuationTarget,
    SourceListing,
    SourceRead,
)
from tailorcv.domain import SourceReadOutcome
from tailorcv.infrastructure.web_fetch.board_mappings import BoardDeclaration, source_listings

# An aggregator read is deliberately bounded: it takes this many of the newest
# listings rather than the whole market, and never reports itself complete.
AGGREGATOR_LISTINGS = 200


# Set for the whole of one read, because every page of a paginated board maps
# its records under the same configured employer.
HAND_NAMED_EMPLOYER: ContextVar[bool] = ContextVar("tailorcv_hand_named_employer", default=False)


class BoardDeadlineReachedError(Exception):
    """A paginated board exhausted its owning operation deadline."""

    def __init__(self, listings: list[SourceListing], raw_count: int) -> None:
        """Keep the listings read before the deadline and how many records they came from."""
        super().__init__("Board read exceeded its operation time limit.")
        self.listings = listings
        self.raw_count = raw_count


def board_listings(
    mapping: BoardDeclaration,
    records: Iterable[Any],
    employer: str,
    groups: dict[str, str] | None = None,
) -> list[SourceListing]:
    """Map board records under the read's employer and its hand-named setting."""
    return source_listings(
        mapping,
        records,
        employer=employer,
        employer_is_authoritative=HAND_NAMED_EMPLOYER.get(),
        groups=groups,
    )


def read_result(
    listings: list[SourceListing],
    *,
    raw_count: int,
    complete: bool,
    incomplete_reason: str | None = None,
    exhausted_outcome: SourceReadOutcome | None = None,
    deferred_targets: tuple[SourceContinuationTarget, ...] = (),
    listing_pages: int = 0,
) -> SourceRead:
    """Retain raw and parsed counts plus the reason records were skipped."""
    skipped = raw_count - len(listings)
    skip_reasons = {"missing_required_posting_identity": skipped} if skipped else {}
    if incomplete_reason is not None:
        skip_reasons[incomplete_reason] = 1
    return SourceRead(
        listings=listings,
        complete=complete,
        raw_count=raw_count,
        skip_reasons=skip_reasons,
        outcome=(
            SourceReadOutcome.ATS_COMPLETE if complete else SourceReadOutcome.CRAWL_CONTINUATION
        ),
        deferred_targets=deferred_targets,
        exhausted_outcome=exhausted_outcome,
        listing_pages=listing_pages,
    )


def whole(
    listings: list[SourceListing], *, raw_count: int | None = None, listing_pages: int = 1
) -> SourceRead:
    """Wrap an API answer that returns every posting the board has."""
    return read_result(
        listings,
        raw_count=len(listings) if raw_count is None else raw_count,
        complete=True,
        listing_pages=listing_pages,
    )


def bounded(
    listings: list[SourceListing], *, raw_count: int | None = None, listing_pages: int = 1
) -> SourceRead:
    """Wrap an aggregator answer, capped at the openings one read may take.

    The cap counts openings rather than pages, so an operator answering with a
    larger page than requested cannot widen the read.
    """
    kept = listings[:AGGREGATOR_LISTINGS]
    reported = len(listings) if raw_count is None else raw_count
    missing_identity = reported - len(listings)
    bounded_away = len(listings) - len(kept)
    return SourceRead(
        listings=kept,
        complete=False,
        raw_count=reported,
        outcome=SourceReadOutcome.CRAWL_CONTINUATION,
        skip_reasons={
            reason: count
            for reason, count in (
                ("missing_required_posting_identity", missing_identity),
                ("bounded_result_cap", bounded_away),
            )
            if count
        },
        listing_pages=listing_pages,
    )


def short_of_total(read: SourceRead, note: str) -> SourceRead:
    """Mark a read incomplete because the board stated more openings than it returned."""
    return replace(
        read,
        complete=False,
        outcome=(
            SourceReadOutcome.CRAWL_CONTINUATION
            if read.deferred_targets
            else SourceReadOutcome.NO_SUPPORTED_REPRESENTATION
        ),
        detail_coverage=None,
        note=_joined(read.note, note),
    )


def _joined(*notes: str) -> str:
    """Join the non-empty notes one read collected, in order."""
    return " ".join(note for note in notes if note)


def robots_short(read: SourceRead) -> SourceRead:
    """Keep a read that skipped a robots-refused posting from settling as the whole board."""
    if "robots_disallowed" not in read.skip_reasons:
        return read
    return replace(
        read,
        complete=False,
        detail_coverage=None,
        exhausted_outcome=None,
        outcome=SourceReadOutcome.CRAWL_CONTINUATION
        if read.deferred_targets
        else SourceReadOutcome.PUBLISHER_LINKED_PAGES,
    )
