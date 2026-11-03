"""Sources, source scans and scan outcomes."""

from datetime import date, datetime
from typing import Literal

from pydantic import Field

from clearscout.application.discovery.funnel_audit import (
    FunnelCandidateKind,
    FunnelReason,
    FunnelStage,
)
from clearscout.application.job_search.models.posting_facts import FunnelScreeningFindingResponse
from clearscout.domain import (
    SourceKind,
    SourceReadOutcome,
)
from clearscout.infrastructure.web_fetch.board_platform import BoardKind
from clearscout.shared.models import StrictModel


# For observability, not policy.
class SourceScanSkipCountsResponse(StrictModel):
    """Known reasons that raw board records did not become usable postings."""

    missing_required_posting_identity: int = Field(default=0, ge=0)
    bounded_result_cap: int = Field(default=0, ge=0)
    title_excluded: int = Field(default=0, ge=0)
    title_mismatch: int = Field(default=0, ge=0)
    title_role_family_conflict: int = Field(default=0, ge=0)
    title_role_family_review: int = Field(default=0, ge=0)
    employer_excluded: int = Field(default=0, ge=0)
    seniority_outside: int = Field(default=0, ge=0)
    location_outside: int = Field(default=0, ge=0)
    location_ineligible: int = Field(default=0, ge=0)
    employment_excluded: int = Field(default=0, ge=0)
    employment_outside: int = Field(default=0, ge=0)
    arrangement_refused: int = Field(default=0, ge=0)
    arrangement_office_limit: int = Field(default=0, ge=0)
    compensation_below_minimum: int = Field(default=0, ge=0)
    protected_posting: int = Field(default=0, ge=0)
    incomplete_workday_listing: int = Field(default=0, ge=0)
    repeated_workday_page: int = Field(default=0, ge=0)
    duplicate_workday_identity: int = Field(default=0, ge=0)
    incomplete_oracle_listing: int = Field(default=0, ge=0)


class SourceScanDetailFailureReasonsResponse(StrictModel):
    """Typed counts of failed posting-detail reads."""

    timeout: int = Field(default=0, ge=0)
    http_status: int = Field(default=0, ge=0)
    parse: int = Field(default=0, ge=0)
    other: int = Field(default=0, ge=0)


# none: no continuation; superseded: a newer scan of the same source exists;
# active: a queued or running source.continue task owns it; waiting: pending
# frontier rows remain with no active task; complete: no pending rows remain.
SourceScanFollowUpState = Literal["active", "waiting", "complete", "superseded", "none"]


# Why history keeps a scan: the newest read of its source and kind, the newest
# complete one, or both at once.
SourceScanKeptAs = Literal["latest", "latest_complete", "latest_and_complete"]


# The stored kinds of read; other covers any label outside this set.
SourceScanKind = Literal["title-filter", "guided-search", "other"]


SourceScanHistoryStatus = Literal["completed", "completed_empty", "incomplete", "failed", "omitted"]


# The statuses of a completed read, whether or not it listed anything.
READ_SCAN_STATUSES: tuple[SourceScanHistoryStatus, ...] = ("completed", "completed_empty")


class SourceScanResponse(StrictModel):
    """One retained board scan with the source settings that governed it."""

    id: str
    # Present only when this scan is tied to a source.scan task in this workspace.
    task_id: str | None
    source_name: str
    # Empty when the source lists many employers and is bound to none.
    employer: str | None
    source_url: str
    source_type: SourceKind
    priority: int
    enabled: bool
    scanned_on: date
    query_label: str
    kind: SourceScanKind
    status: SourceScanHistoryStatus
    read_outcome: SourceReadOutcome
    frontier_pending_count: int = Field(ge=0)
    frontier_completed_count: int = Field(ge=0)
    frontier_failed_count: int = Field(ge=0)
    automatic_pending_count: int = Field(ge=0)
    follow_up_state: SourceScanFollowUpState
    kept_as: SourceScanKeptAs
    # Distinct held-out and title-gated ledger listings for this task and source,
    # less any now visible in Pending. Zero when the scan has no task.
    screened_out_count: int = Field(ge=0)
    # Raw platform records and the usable postings parsed from them.
    raw_count: int
    read_count: int
    skip_reasons: SourceScanSkipCountsResponse
    result_count: int
    new_count: int
    detail_attempted_count: int
    detail_read_count: int
    detail_failed_count: int
    detail_interrupted_count: int | None = Field(default=None, ge=0)
    detail_capped_count: int
    detail_failure_reasons: SourceScanDetailFailureReasonsResponse
    notes: str
    # Null where the scan predates this field, which is not the same as bounded.
    complete: bool | None
    scan_started_at: datetime
    scan_completed_at: datetime
    duration_ms: int


class SourceTerms(StrictModel):
    """The condition a source's published terms attach to reading it; empty when unrecorded."""

    url: str = Field(default="", max_length=2000)
    condition: str = Field(default="", max_length=2000)


class SourceResponse(StrictModel):
    """One source, and what the last scan of it managed to read."""

    id: str
    name: str
    # Empty when the source lists many employers and is bound to none.
    employer: str | None
    url: str
    # The employer's own site that company research reads; empty for an
    # aggregator and until a person or a non-board address supplies it.
    careers_url: str | None
    source_type: SourceKind
    # Empty when the address names no known platform; a career page that fronts
    # one is identified during the scan rather than here.
    platform: BoardKind | None
    priority: int
    enabled: bool
    terms: SourceTerms
    last_scanned_on: date | None
    last_status: Literal["completed", "completed_empty", "incomplete", "failed", "omitted"] | None
    # Empty until a scan of this source has been recorded.
    last_read_outcome: SourceReadOutcome | None
    last_notes: str
    # Counted over every retained scan of this source. A source with scans and
    # no staged posting has been read and has never put a row in the queue.
    scan_count: int = Field(ge=0)
    staged_count: int = Field(ge=0)
    # Only completed reads; a failed or partial read is not evidence the board
    # lists nothing.
    read_scan_count: int = Field(ge=0)
    incomplete_scan_count: int = Field(ge=0)
    # Omitted scans count in scan_count but never attempted a read.
    failed_scan_count: int = Field(ge=0)
    first_read_on: date | None
    # The last scan whose board reported any raw record, before screening.
    # Empty when no retained scan has ever listed an opening.
    last_listed_on: date | None


class SourceListResponse(StrictModel):
    """Every board source this workspace holds, archived ones excluded."""

    items: list[SourceResponse]


class SourcePreviewResponse(StrictModel):
    """What resolving one address found, before or after it is saved as a source."""

    resolved_url: str
    # Empty when the address, and its fetched page, name no known platform.
    platform: BoardKind | None
    note: str


class SourceScanHistoryPageResponse(StrictModel):
    """One stable page of retained board scan history."""

    items: list[SourceScanResponse]
    total: int
    offset: int
    limit: int


SourceScanOutcomeSort = Literal[
    "employer", "title", "location", "terminal_stage", "reason", "created_at"
]


SourceScanOutcomeDirection = Literal["asc", "desc"]


SourceScanOutcomeCaptureState = Literal[
    "complete",
    "in_progress",
    "partial",
    "recovered",
    "recovered_partial",
    "unavailable",
]


class SourceScanOutcomeResponse(StrictModel):
    """One body-free candidate decision retained for a completed board scan."""

    id: str
    task_id: str
    source_scan_id: str | None
    source_name: str
    candidate_kind: FunnelCandidateKind
    url: str
    employer: str
    title: str
    location: str
    terminal_stage: FunnelStage
    reason: FunnelReason
    detail: str
    screening_findings: list[FunnelScreeningFindingResponse]
    created_at: datetime


class SourceScanOutcomePageResponse(StrictModel):
    """One filtered, stable page of a completed scan's candidate ledger."""

    items: list[SourceScanOutcomeResponse]
    total: int
    offset: int
    limit: int
    sources: list[str]
    terminal_stages: list[FunnelStage]
    reasons: list[FunnelReason]
    candidate_kinds: list[FunnelCandidateKind]
    capture_state: SourceScanOutcomeCaptureState
    recorded_outcomes: int = Field(ge=0)
    follow_up_outcomes: int = Field(default=0, ge=0)
    reported_outcomes: int | None = Field(default=None, ge=0)
    reported_omitted_outcomes: int | None = Field(default=None, ge=0)
