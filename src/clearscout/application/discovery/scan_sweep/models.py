"""The boards a sweep reads and the per-board results it passes between its parts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from clearscout.application.discovery.content_resolution import PrefetchedContent
from clearscout.application.discovery.screening import DiscoveryScreening, StructuredHoldout
from clearscout.application.discovery.source_reading import (
    SourceDetailSummary,
    SourceListing,
    SourceRead,
)
from clearscout.domain import SourceReadOutcome
from clearscout.domain.search_parameters import JobSearchParameters
from clearscout.infrastructure.persistence.models import Source


@dataclass(frozen=True)
class ScanInputs:
    """What every board in one sweep pass is screened against, fixed before any read."""

    workspace_id: str
    parameters: JobSearchParameters
    targets: list[str]
    stated: bool
    task_id: str | None


@dataclass(frozen=True)
class LaneRead:
    """A board's network work done in its read lane, ready for the caller to record."""

    listings: list[SourceListing]
    detail: SourceDetailSummary
    wanted: list[SourceListing]
    refused: list[tuple[SourceListing, StructuredHoldout]]
    unsupported: list[tuple[SourceListing, DiscoveryScreening]]
    prefetched: dict[str, PrefetchedContent]


@dataclass(frozen=True)
class BoardReadOutcome:
    """One board's read result, and the wall-clock brackets around the fetch."""

    board: SweepBoard
    read: SourceRead | None
    failure: str | None
    failure_outcome: SourceReadOutcome | None
    started_at: datetime
    completed_at: datetime
    operation_id: str = ""
    network_unreachable: bool = False
    lane: LaneRead | None = None


@dataclass(frozen=True)
class DueBoards:
    """Enabled sources the sweep will read this pass, and those it must skip."""

    readable: list[SweepBoard]
    omitted: list[tuple[SweepBoard, str]]


@dataclass(frozen=True)
class ProcessedRead:
    """One successful board's contribution back to the sweep totals."""

    scan_row: dict[str, Any]
    discoveries: list[dict[str, str]]
    withdrawn: list[dict[str, str]]
    new_count: int
    requeued_count: int
    # Rows this board's content reads found closed, which the absent-listing count never sees.
    withdrawn_on_read_count: int


@dataclass(frozen=True)
class SweepBoard:
    """One source the sweep reads, and the address it reads it at.

    ``employer`` is empty for an aggregator, which lists many employers and is
    bound to none of them.
    """

    source: Source
    employer: str
    url: str
    # Set when the stored resolution named another board and the source address was read.
    address_note: str = ""
    # True when a person named this board by hand, so the sweep keeps that name
    # instead of the one the board's own records state.
    employer_is_authoritative: bool = False
    fresh_details: bool = False
