"""Pending discovery pages, facets, changes and queue actions."""

from datetime import date, datetime
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from tailorcv.application.contracts import PublisherClosingDateResponse
from tailorcv.application.discovery.screening import (
    ArrangementCategory,
    ArrangementSource,
    ConstraintConfidence,
    ScreeningStanding,
)
from tailorcv.application.job_search.models.posting_facts import (
    ScreeningFindingResponse,
    StagedListingFactsResponse,
)
from tailorcv.application.job_search.models.relevance_reviews import (
    RelevanceReviewRejectionResponse,
    RelevanceReviewResponse,
)
from tailorcv.application.job_search.models.vocabulary import (
    DiscoveryRecommendation,
    PendingConstraintRoute,
    PendingDiscoveryId,
    PendingDiscoveryStagingProvenance,
    PendingRelevanceKey,
    PendingReviewStatus,
)
from tailorcv.application.job_search.models.workspace import JobEmployerFacetResponse
from tailorcv.domain import (
    PendingDiscoveryReadiness,
    PostingContentAccessStatus,
    PostingContentExtractionMethod,
    PostingIdentityStatus,
    PostingStatus,
)
from tailorcv.domain.records import (
    DuplicateKeeperBasis,
    DuplicateSuggestionSignal,
    RobotsReadBasis,
)
from tailorcv.shared.models import StrictModel


# Computed on read by duplicate_suggestions_in_session. The separate manual
# picker is PendingDuplicateCandidateResponse.
class PendingDuplicateSuggestionResponse(StrictModel):
    """Another discovery a deterministic signal links to this one."""

    pending_discovery_id: str
    employer: str
    title: str
    location: str
    discovery_url: str
    dismissed: bool
    signal: DuplicateSuggestionSignal
    keeper_pending_discovery_id: str
    keeper_basis: DuplicateKeeperBasis


class PendingDiscoveryResponse(StrictModel):
    """One discovery that is not yet evaluable."""

    id: str
    employer: str
    title: str
    location: str
    discovery_url: str
    canonical_url: str
    content_status: PostingContentAccessStatus
    content_note: str
    readiness: PendingDiscoveryReadiness
    retained_content_id: str | None = None
    retained_content_artifact_id: str | None = None
    # Read from location by the workspace's PlaceReader, named and suggested alike.
    countries: list[str] = Field(default_factory=list)
    # Manual remediation echoes this back; it names the current observation even
    # when that observation is unusable, which retained_content_id never does.
    current_content_id: str | None = None
    current_posting_status: PostingStatus | None = None
    current_content_observed_at: datetime | None = None
    publisher_closing_date: PublisherClosingDateResponse | None = None
    closing_date_override: date | None = None
    current_identity_status: PostingIdentityStatus | None = None
    current_extraction_method: PostingContentExtractionMethod | None = None
    current_robots_basis: RobotsReadBasis | None = None
    observed_publisher_employer: str = ""
    observed_publisher_title: str = ""
    # The employer registered to the board the row was read from, set only when
    # the posting names a different employer.
    board_employer: str | None = None
    listing_artifact_id: str | None = None
    reason: str
    # Read from the campaign's own admission and capture records and from the
    # row's source. The stored reason text names a person for rows a campaign
    # staged, so it is never consulted here.
    staging_provenance: PendingDiscoveryStagingProvenance = "unrecorded"
    first_seen_on: date
    # The family's markers, lifted to the head the queue lists.
    favorite: bool = False
    kept: bool = False
    keep_note: str | None = None
    rank: int = 0
    ranked_because: list[str] = Field(default_factory=list)
    matched_target_title: str | None = None
    recommendation: DiscoveryRecommendation = "investigate"
    recommendation_because: str
    constraint_confidence: ConstraintConfidence
    screening_standing: ScreeningStanding
    screening_findings: list[ScreeningFindingResponse]
    staged_facts: StagedListingFactsResponse
    arrangement_categories: list[
        Literal["remote", "hybrid", "on_site", "period_limited", "unknown"]
    ]
    arrangement_categories_source: ArrangementSource
    also_in: list[str] = Field(default_factory=list)
    withdrawn_at: datetime | None = None
    # The current evaluated posting this discovery already names by requisition
    # or URL, so the SPA can hide the paid evaluation control on a duplicate.
    evaluated_job_id: str | None = None
    evaluated_evaluation_id: str | None = None
    evaluated_at: datetime | None = None
    evaluated_decision: str | None = None
    relevance_review: RelevanceReviewResponse | None = None
    # Set when the last paid review of this row was rejected locally, which is a
    # different state from a row no review has ever run against.
    relevance_review_rejection: RelevanceReviewRejectionResponse | None = None
    duplicate_suggestions: list[PendingDuplicateSuggestionResponse] = Field(default_factory=list)


class PendingDiscoveryLanePageResponse(StrictModel):
    """One stable, independently bounded pending-discovery lane."""

    items: list[PendingDiscoveryResponse]
    total: int
    offset: int
    limit: int
    has_more: bool


class PendingReadinessFacetResponse(StrictModel):
    """How many queue rows choosing one content readiness lists, and how many hold it."""

    value: PendingDiscoveryReadiness
    count: int = Field(ge=0)
    holding: int = Field(ge=0)


class PendingRelevanceFacetResponse(StrictModel):
    """How many queue rows read as one relevance key, and how many of those are out of date."""

    value: PendingRelevanceKey
    count: int = Field(ge=0)
    out_of_date: int = Field(ge=0)


class PendingReviewStatusFacetResponse(StrictModel):
    """How many reviewed queue rows hold one review status."""

    value: PendingReviewStatus
    count: int = Field(ge=0)


class PendingConstraintFacetResponse(StrictModel):
    """How many queue rows choosing one constraint route lists."""

    value: PendingConstraintRoute
    count: int = Field(ge=0)


class PendingCategoryFacetResponse(StrictModel):
    """How many queue rows carry one screening recommendation."""

    value: DiscoveryRecommendation
    count: int = Field(ge=0)


class PendingArrangementFacetResponse(StrictModel):
    """How many queue rows state one work arrangement."""

    value: ArrangementCategory
    count: int = Field(ge=0)


class PendingProvenanceFacetResponse(StrictModel):
    """How many queue rows one origin put in the queue."""

    value: PendingDiscoveryStagingProvenance
    count: int = Field(ge=0)


class PendingMarkerFacetResponse(StrictModel):
    """How many queue rows carry each marker."""

    favorite: int = Field(ge=0)
    kept: int = Field(ge=0)


# Each dimension is counted under every filter except its own selection. Entries
# overlap where a row holds several values or a marker lists it under every
# readiness and route; `*_total` and readiness `holding` give the row counts.
class PendingDiscoveryFacetsResponse(StrictModel):
    """Per-value row counts behind each pending-queue filter."""

    readiness: list[PendingReadinessFacetResponse]
    relevance: list[PendingRelevanceFacetResponse]
    review_status: list[PendingReviewStatusFacetResponse]
    review_status_total: int = Field(ge=0)
    constraint: list[PendingConstraintFacetResponse]
    constraint_total: int = Field(ge=0)
    category: list[PendingCategoryFacetResponse]
    arrangement: list[PendingArrangementFacetResponse]
    arrangement_total: int = Field(ge=0)
    provenance: list[PendingProvenanceFacetResponse]
    markers: PendingMarkerFacetResponse


# Lane totals, has_more and the snapshot digest describe the filtered set;
# queue_total counts the same snapshot before any filter.
class PendingDiscoveryPageResponse(StrictModel):
    """Independent ready and attention pages for active discovery triage."""

    snapshot_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    snapshot_before: datetime
    ready: PendingDiscoveryLanePageResponse
    attention: PendingDiscoveryLanePageResponse
    queue_total: int = Field(ge=0)
    facets: PendingDiscoveryFacetsResponse
    # Counted with every filter except the employer selection itself.
    employers: list[JobEmployerFacetResponse]


class PendingDiscoveryChangesVisibleCounts(StrictModel):
    """Visible pending discoveries created after a snapshot, by readiness lane."""

    ready: int = Field(ge=0)
    attention: int = Field(ge=0)


class PendingDiscoveryChangesFollowUpCounts(StrictModel):
    """Publisher follow-up activity across the workspace's latest board scans."""

    active_sources: int = Field(ge=0)
    automatic_pending_count: int = Field(ge=0)
    frontier_completed_count: int = Field(ge=0)


class PendingDiscoveryChangesResponse(StrictModel):
    """What changed since a pending-discovery page snapshot was taken."""

    newer_visible: PendingDiscoveryChangesVisibleCounts
    follow_up: PendingDiscoveryChangesFollowUpCounts


class TitleExclusionCollateralRowResponse(StrictModel):
    """How many of the queue's current rows one title-skip phrase would also hide."""

    phrase: str
    matched_count: int = Field(ge=0)


# Counted with the shipped exclusion predicate in domain.search_parameters.roles,
# so the number agrees with what screening does.
class TitleExclusionCollateralResponse(StrictModel):
    """What a title-skip list would take out of the pending queue as it stands."""

    queue_count: int = Field(ge=0)
    phrases: list[TitleExclusionCollateralRowResponse]


class FoundPostingCheckResponse(StrictModel):
    """Free publisher metadata preflight for one manually supplied posting."""

    status: Literal["readable", "unreadable", "unsupported"]
    note: str
    platform: str = ""
    title: str = ""
    employer: str = ""
    location: str = ""
    staged_facts: StagedListingFactsResponse = Field(default_factory=StagedListingFactsResponse)


class FoundPostingCreatedResponse(StrictModel):
    """A manually supplied posting newly staged for review."""

    outcome: Literal["created"] = "created"
    pending_discovery: PendingDiscoveryResponse
    posting_check: FoundPostingCheckResponse | None = None


class FoundPostingRequeuedResponse(StrictModel):
    """A manually supplied posting restored to the review queue."""

    outcome: Literal["requeued"] = "requeued"
    pending_discovery: PendingDiscoveryResponse
    posting_check: FoundPostingCheckResponse | None = None


FoundPostingAddResponse = Annotated[
    FoundPostingCreatedResponse | FoundPostingRequeuedResponse,
    Field(discriminator="outcome"),
]


class PendingQueueResetResponse(StrictModel):
    """Counts from rebuilding the pending queue around human decisions."""

    archived_count: int
    kept_count: int
    rescreened_count: int


class PendingMarkersRequest(StrictModel):
    """A person's favorite and keep markers for one discovery family."""

    favorite: bool
    kept: bool
    keep_note: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def note_needs_keep(self) -> Self:
        """Hold the note to one line, and only on a kept row."""
        if self.keep_note is None:
            return self
        if "\n" in self.keep_note or "\r" in self.keep_note:
            raise ValueError("A keep note is one line.")
        if not self.kept and self.keep_note.strip():
            raise ValueError("A keep note needs the keep marker.")
        return self


class PendingPostingContentActionResponse(StrictModel):
    """Durable evidence created by one explicit pending-posting remediation."""

    pending_discovery_id: PendingDiscoveryId
    evidence_id: str = Field(min_length=1)
    readiness: Literal["ready"] = "ready"
