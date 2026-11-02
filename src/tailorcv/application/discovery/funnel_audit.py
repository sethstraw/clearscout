"""Typed, bounded outcomes for candidates that enter discovery."""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tailorcv.application.discovery.screening import BulkStagingRefusal, DiscoveryScreening

MAX_AUDIT_OUTCOMES = 500


class FunnelListing(Protocol):
    """The publisher fields both discovery paths already carry."""

    @property
    def employer(self) -> str:
        """Return the publisher-stated employer."""
        ...

    @property
    def title(self) -> str:
        """Return the publisher-stated title."""
        ...

    @property
    def location(self) -> str:
        """Return the publisher-stated location."""
        ...

    @property
    def url(self) -> str:
        """Return the posting URL."""
        ...


class FunnelCandidateKind(StrEnum):
    """The identity being audited."""

    DIRECT_POSTING = "direct_posting"
    BOARD_LEAD = "board_lead"
    BOARD_LISTING = "board_listing"


class FunnelStage(StrEnum):
    """One monotonic step reached by a candidate."""

    PROPOSED = "proposed"
    FETCHED = "fetched"
    READ = "read"
    SCREENED = "screened"
    STAGED = "staged"
    REJECTED = "rejected"


class FunnelReason(StrEnum):
    """The machine-readable terminal explanation."""

    STAGED = "staged"
    ALREADY_KNOWN = "already_known"
    INCOMPLETE_POSTING = "incomplete_posting"
    DUPLICATE_URL = "duplicate_url"
    DUPLICATE_LEAD = "duplicate_lead"
    EXCLUDED_EMPLOYER = "excluded_employer"
    HARD_CONSTRAINT_EXCLUDED = "hard_constraint_excluded"
    INSUFFICIENT_TITLE_EVIDENCE = "insufficient_title_evidence"
    SENIORITY_OUTSIDE_BAND = "seniority_outside_band"
    LOCATION_OUTSIDE_SAVED_PLACES = "location_outside_saved_places"
    STAGE_LIMIT_REACHED = "stage_limit_reached"
    ROLE_FAMILY_REVIEW_LIMIT_REACHED = "role_family_review_limit_reached"
    NO_TITLE_CONFIGURED = "no_title_configured"
    NO_OPEN_POSITIONS = "no_open_positions"
    NO_MATCHING_LISTINGS = "no_matching_listings"
    NO_NEW_OPENINGS = "no_new_openings"
    FETCH_REFUSED = "fetch_refused"
    FETCH_FAILED = "fetch_failed"
    NO_READABLE_BOARD = "no_readable_board"


_BULK_STAGING_REFUSAL_BLOCKERS: dict[BulkStagingRefusal, str] = {
    BulkStagingRefusal.INSUFFICIENT_TITLE_EVIDENCE: (
        "its title evidence is insufficient for automatic staging"
    ),
    BulkStagingRefusal.SENIORITY_OUTSIDE_BAND: (
        "its stated seniority is outside the configured seniority band"
    ),
    BulkStagingRefusal.LOCATION_OUTSIDE_SAVED_PLACES: (
        "its location does not fit the saved places or eligibility"
    ),
}


def bulk_staging_refusal_outcome(refusal: BulkStagingRefusal | None) -> tuple[FunnelReason, str]:
    """Return the funnel reason and detail for a refusal; a blank title passes None."""
    refusal = refusal or BulkStagingRefusal.INSUFFICIENT_TITLE_EVIDENCE
    reason = FunnelReason(refusal.value)
    detail = (
        f"The listing remains in the screened-candidate ledger because "
        f"{_BULK_STAGING_REFUSAL_BLOCKERS[refusal]}. It is recoverable and has not been rejected."
    )
    return reason, detail


_ALLOWED_PATHS = {
    (FunnelStage.PROPOSED,),
    (FunnelStage.PROPOSED, FunnelStage.REJECTED),
    (FunnelStage.PROPOSED, FunnelStage.STAGED),
    (FunnelStage.PROPOSED, FunnelStage.SCREENED, FunnelStage.REJECTED),
    (FunnelStage.PROPOSED, FunnelStage.SCREENED, FunnelStage.STAGED),
    (FunnelStage.PROPOSED, FunnelStage.FETCHED, FunnelStage.READ),
    (FunnelStage.PROPOSED, FunnelStage.FETCHED, FunnelStage.READ, FunnelStage.REJECTED),
    (FunnelStage.PROPOSED, FunnelStage.FETCHED, FunnelStage.READ, FunnelStage.SCREENED),
    (
        FunnelStage.PROPOSED,
        FunnelStage.FETCHED,
        FunnelStage.READ,
        FunnelStage.SCREENED,
        FunnelStage.REJECTED,
    ),
    (
        FunnelStage.PROPOSED,
        FunnelStage.FETCHED,
        FunnelStage.READ,
        FunnelStage.SCREENED,
        FunnelStage.STAGED,
    ),
}


class FunnelScreeningFinding(BaseModel):
    """One decisive screening fact retained with a funnel outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    dimension: str
    code: str
    subject: str = ""


class FunnelOutcome(BaseModel):
    """One candidate's valid path through the current discovery run."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: FunnelCandidateKind
    employer: str = ""
    title: str = ""
    location: str = ""
    url: str = ""
    source: str = ""
    stages: tuple[FunnelStage, ...]
    reason: FunnelReason
    detail: str = ""
    screening_findings: tuple[FunnelScreeningFinding, ...] = ()

    @model_validator(mode="after")
    def path_is_valid(self) -> FunnelOutcome:
        """Reject contradictory or out-of-order stage paths."""
        if self.stages not in _ALLOWED_PATHS:
            raise ValueError("funnel stages do not form a supported path")
        return self


def listing_outcome(
    listing: FunnelListing,
    *,
    source: str,
    terminal: FunnelStage | None,
    reason: FunnelReason,
    detail: str = "",
    screening: DiscoveryScreening | None = None,
    screening_findings: tuple[FunnelScreeningFinding, ...] | None = None,
) -> FunnelOutcome:
    """Map listing primitives into the shared typed contract."""
    stages = (
        FunnelStage.PROPOSED,
        FunnelStage.FETCHED,
        FunnelStage.READ,
        FunnelStage.SCREENED,
    )
    findings = screening_findings
    if findings is None:
        findings = (
            tuple(
                FunnelScreeningFinding(
                    dimension=finding.dimension,
                    code=finding.code,
                    subject=finding.subject,
                )
                for finding in screening.findings
                if finding.standing != "match"
            )
            if screening is not None
            else ()
        )
    return FunnelOutcome(
        kind=FunnelCandidateKind.BOARD_LISTING,
        employer=listing.employer,
        title=listing.title,
        location=listing.location,
        url=listing.url,
        source=source,
        stages=(*stages, terminal) if terminal is not None else stages,
        reason=reason,
        detail=detail,
        screening_findings=findings,
    )


class FunnelCounts(BaseModel):
    """Full counts including omitted detail rows."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    proposed: int
    read: int
    fetched: int
    screened: int
    staged: int
    rejected: int


class DiscoveryFunnelAudit(BaseModel):
    """Bounded detail with explicit full totals and omissions."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    outcomes: tuple[FunnelOutcome, ...]
    counts: FunnelCounts
    total_outcomes: int
    omitted_outcomes: int
    omission_reason: str = ""
    reason_counts: dict[str, int] = Field(default_factory=dict)


class FunnelAuditBuilder:
    """Accumulate full totals while bounding task and event payload size."""

    def __init__(
        self,
        limit: int = MAX_AUDIT_OUTCOMES,
        *,
        on_record: Callable[[FunnelOutcome], None] | None = None,
    ) -> None:
        """Start an audit with a positive detail bound."""
        if limit < 1:
            raise ValueError("funnel audit limit must be positive")
        self._limit = limit
        self._outcomes: list[FunnelOutcome] = []
        self._counts = dict.fromkeys(FunnelStage, 0)
        self._total = 0
        self._reason_counts: dict[str, int] = {}
        self._on_record = on_record

    def record(self, outcome: FunnelOutcome) -> None:
        """Count every outcome while retaining staged detail within the bound."""
        if self._on_record is not None:
            self._on_record(outcome)
        self._total += 1
        self._reason_counts[outcome.reason.value] = (
            self._reason_counts.get(outcome.reason.value, 0) + 1
        )
        for stage in outcome.stages:
            self._counts[stage] += 1
        if len(self._outcomes) < self._limit:
            self._outcomes.append(outcome)
            return
        if FunnelStage.STAGED not in outcome.stages:
            return
        replaceable = next(
            (
                index
                for index in range(len(self._outcomes) - 1, -1, -1)
                if FunnelStage.STAGED not in self._outcomes[index].stages
            ),
            None,
        )
        if replaceable is not None:
            self._outcomes.pop(replaceable)
            self._outcomes.append(outcome)

    def record_listing(
        self,
        listing: FunnelListing,
        *,
        source: str,
        terminal: FunnelStage | None,
        reason: FunnelReason,
        detail: str = "",
        screening: DiscoveryScreening | None = None,
        screening_findings: tuple[FunnelScreeningFinding, ...] | None = None,
    ) -> None:
        """Count one board listing through the shared typed contract."""
        self.record(
            listing_outcome(
                listing,
                source=source,
                terminal=terminal,
                reason=reason,
                detail=detail,
                screening=screening,
                screening_findings=screening_findings,
            )
        )

    def record_lead(
        self,
        *,
        employer: str,
        url: str,
        source: str = "",
        fetched: bool,
        staged: bool,
        reason: FunnelReason,
        detail: str = "",
    ) -> None:
        """Record one board address without duplicating its transition policy."""
        stages = (
            (
                FunnelStage.PROPOSED,
                FunnelStage.FETCHED,
                FunnelStage.READ,
                FunnelStage.SCREENED,
                FunnelStage.STAGED,
            )
            if staged
            else (FunnelStage.PROPOSED, FunnelStage.FETCHED, FunnelStage.READ)
            if fetched
            else (FunnelStage.PROPOSED, FunnelStage.REJECTED)
        )
        self.record(
            FunnelOutcome(
                kind=FunnelCandidateKind.BOARD_LEAD,
                employer=employer,
                url=url,
                source=source,
                stages=stages,
                reason=reason,
                detail=detail,
            )
        )

    def build(self) -> DiscoveryFunnelAudit:
        """Freeze the bounded audit for durable task history."""
        omitted = self._total - len(self._outcomes)
        return DiscoveryFunnelAudit(
            outcomes=tuple(self._outcomes),
            counts=FunnelCounts(
                proposed=self._counts[FunnelStage.PROPOSED],
                read=self._counts[FunnelStage.READ],
                fetched=self._counts[FunnelStage.FETCHED],
                screened=self._counts[FunnelStage.SCREENED],
                staged=self._counts[FunnelStage.STAGED],
                rejected=self._counts[FunnelStage.REJECTED],
            ),
            total_outcomes=self._total,
            omitted_outcomes=omitted,
            omission_reason=(
                f"At most {self._limit} detailed outcomes are retained; "
                "staged outcomes replace earlier non-staged detail when the limit is full."
            )
            if omitted
            else "",
            reason_counts=dict(sorted(self._reason_counts.items())),
        )
