"""Search proposals, posting refusals and refusal patterns."""

from datetime import datetime
from typing import Literal

from pydantic import Field

from clearscout.domain import (
    ProposalRejectionReason,
    SearchProposalState,
)
from clearscout.domain.search_parameters import ClusterKind, Lane
from clearscout.shared.models import StrictModel


class ProposalRejectRequest(StrictModel):
    """Reject body carrying an optional structured reason; an empty body still rejects."""

    reason: ProposalRejectionReason | None = None


type SearchProposalCapacityKind = Literal["title", "employer_archetype", "capability", "domain"]


class SearchProposalCapacityResponse(StrictModel):
    """The workspace-effective review capacity for one proposal kind."""

    kind: SearchProposalCapacityKind
    active_count: int = Field(ge=0)
    limit: int = Field(ge=0)
    remaining: int = Field(ge=0)


class SearchTitleProposalResponse(StrictModel):
    """One title a search proposed, and whether the user has ruled on it."""

    id: str
    title: str
    reason: str
    evidence_ids: list[str]
    lane: Lane | None
    origin: str
    correlation_id: str | None
    master_cv_version_id: str
    is_current_master_cv_version: bool
    state: SearchProposalState
    proposed_at: datetime
    decided_at: datetime | None
    rejection_reason: ProposalRejectionReason | None = None


class SearchClusterProposalResponse(StrictModel):
    """One capability or domain a search proposed, and how the user ruled on it."""

    id: str
    kind: ClusterKind
    name: str
    terms: list[str]
    because: str
    evidence_ids: list[str]
    origin: str
    correlation_id: str | None
    master_cv_version_id: str
    is_current_master_cv_version: bool
    state: SearchProposalState
    proposed_at: datetime
    decided_at: datetime | None
    rejection_reason: ProposalRejectionReason | None = None


class SearchArchetypeProposalResponse(StrictModel):
    """One kind of employer a search proposed, and whether the user has ruled on it."""

    id: str
    name: str
    because: str
    evidence_ids: list[str]
    origin: str
    correlation_id: str | None
    master_cv_version_id: str
    is_current_master_cv_version: bool
    state: SearchProposalState
    proposed_at: datetime
    decided_at: datetime | None
    rejection_reason: ProposalRejectionReason | None = None


class SearchTitleProposalInboxResponse(StrictModel):
    """Workspace-effective title proposals and the limit that governs them."""

    items: list[SearchTitleProposalResponse]
    capacities: list[SearchProposalCapacityResponse]


class SearchArchetypeProposalInboxResponse(StrictModel):
    """Workspace-effective employer proposals and the limit that governs them."""

    items: list[SearchArchetypeProposalResponse]
    capacities: list[SearchProposalCapacityResponse]


class SearchClusterProposalInboxResponse(StrictModel):
    """Workspace-effective cluster proposals and the per-kind limits that govern them."""

    items: list[SearchClusterProposalResponse]
    capacities: list[SearchProposalCapacityResponse]


class PostingRefusalResponse(StrictModel):
    """One posting a stated condition refused, and the condition that did it."""

    id: str
    job_posting_id: str | None
    pending_discovery_id: str | None
    posting_url: str | None
    employer: str
    title: str
    condition: str
    reason: str
    refused_at: datetime
    search_parameter_set_id: str | None
    search_parameter_version: int | None


class RefusedPostingResponse(StrictModel):
    """One posting a refusal pattern still refuses."""

    employer: str
    title: str


class RefusalPatternProposalResponse(StrictModel):
    """A recurring refusal offered for acceptance as a stated exclusion."""

    id: str
    condition: str
    normalized_reason: str
    reason_examples: list[str]
    refused_postings: list[RefusedPostingResponse]
    refusal_count: int
    first_seen_at: datetime
    last_seen_at: datetime
    state: SearchProposalState
    proposed_at: datetime
    decided_at: datetime | None
