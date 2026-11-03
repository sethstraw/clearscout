"""Discovery review and proposal routes."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import StringConstraints

from clearscout.application import job_search
from clearscout.application.discovery.assisted_content_resolution import (
    AssistedContentResolutionService,
)
from clearscout.application.discovery.dispositions import DiscoveryDispositionService
from clearscout.application.discovery.scan_queries import (
    MAX_COLLATERAL_PHRASES,
    PENDING_PAGE_MAX_LIMIT,
    PendingQueueFilters,
)
from clearscout.application.discovery.screening import ArrangementCategory
from clearscout.application.failures import ConflictError
from clearscout.application.job_search.models import (
    ClosingDateEditInput,
    DiscoveryRecommendation,
    DismissedDiscoveryResponse,
    DispositionApply,
    DispositionPreview,
    DispositionSelection,
    FoundPostingAddResponse,
    FoundPostingCheckResponse,
    JobDetailResponse,
    PendingClosureReconcileRequest,
    PendingClosureReconcileResponse,
    PendingConstraintRoute,
    PendingDiscoveryChangesResponse,
    PendingDiscoveryPageResponse,
    PendingDiscoveryResponse,
    PendingDiscoveryStagingProvenance,
    PendingDispositionResponse,
    PendingDuplicateCandidateResponse,
    PendingMarkersRequest,
    PendingPostingContentActionResponse,
    PendingQueueResetResponse,
    PendingRelevanceKey,
    PendingReviewStatus,
    PostingRefusalResponse,
    PostingResolutionPreflightRequest,
    ProposalRejectRequest,
    RefusalPatternProposalResponse,
    SearchArchetypeProposalInboxResponse,
    SearchArchetypeProposalResponse,
    SearchClusterProposalInboxResponse,
    SearchClusterProposalResponse,
    SearchTitleProposalInboxResponse,
    SearchTitleProposalResponse,
    TitleExclusionCollateralResponse,
)
from clearscout.application.tasks.models import PostingRecoveryApprovalManifest
from clearscout.application.workspaces import WorkspaceSettingsService
from clearscout.delivery.api.constants import API_PREFIX
from clearscout.delivery.api.dependencies import (
    ArtifactServiceDep,
    CorrelationId,
    DatabaseDep,
    PostingResolutionServiceDep,
    RefusalPatternProposalServiceDep,
    SearchArchetypeProposalServiceDep,
    SearchClusterProposalServiceDep,
    SearchTitleProposalServiceDep,
    SourceScanServiceDep,
)
from clearscout.delivery.api.models.workspaces import (
    FoundPostingCheck,
    FoundPostingCreate,
    PendingDiscoveryTitleUpdate,
    PendingPostingIdentityConfirm,
    PendingPostingTextAttach,
)
from clearscout.domain import PendingDiscoveryReadiness, utc_now

router = APIRouter()


@router.post(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/pending-discoveries/{{pending_id}}/track",
    response_model=JobDetailResponse,
    tags=["workspaces"],
)
def track_pending_discovery(
    workspace_id: str,
    pending_id: str,
    database: DatabaseDep,
    artifacts: ArtifactServiceDep,
    correlation_id: CorrelationId,
) -> JobDetailResponse:
    """Move a saved lead into the tracked ledger without running an evaluation."""
    return job_search.track_pending_discovery(
        database, artifacts, workspace_id, pending_id, correlation_id=correlation_id
    )


@router.post(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/posting-recovery-preflight",
    response_model=PostingRecoveryApprovalManifest,
    tags=["workspaces"],
    summary="Estimate assisted recovery for unreadable pending postings",
)
def posting_recovery_preflight(
    workspace_id: str,
    body: PostingResolutionPreflightRequest,
    service: PostingResolutionServiceDep,
    database: DatabaseDep,
) -> PostingRecoveryApprovalManifest:
    """Return the bounded estimate and exact manifest required by the paid task."""
    return AssistedContentResolutionService(database, service).preflight(
        workspace_id,
        body.pending_discovery_ids,
        WorkspaceSettingsService(database).current(workspace_id),
    )


@router.post(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/pending-discoveries/check",
    response_model=FoundPostingCheckResponse,
    tags=["workspaces"],
    summary="Check one posting through its publisher API",
)
def check_found_posting(
    workspace_id: str,
    body: FoundPostingCheck,
    service: SourceScanServiceDep,
) -> FoundPostingCheckResponse:
    """Read free publisher metadata without staging the posting or enqueueing work."""
    return service.check_found_posting(workspace_id, body.url)


@router.post(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/pending-dispositions/preview",
    response_model=DispositionPreview,
    tags=["workspaces"],
    summary="Preview exact pending discoveries a disposition would affect",
)
def preview_pending_disposition(
    workspace_id: str, body: DispositionSelection, database: DatabaseDep
) -> DispositionPreview:
    """Resolve primary discoveries and authoritative regional variants."""
    return DiscoveryDispositionService(database).preview(workspace_id, body)


@router.post(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/pending-dispositions/apply",
    response_model=list[PendingDispositionResponse],
    tags=["workspaces"],
    summary="Apply a confirmed pending-discovery disposition",
)
def apply_pending_disposition(
    workspace_id: str,
    body: DispositionApply,
    database: DatabaseDep,
    correlation_id: CorrelationId,
) -> list[PendingDispositionResponse]:
    """Append dismissal or restoration events without rewriting discoveries."""
    return DiscoveryDispositionService(database).apply(
        workspace_id,
        body,
        actor="local_user",
        origin="api",
        correlation_id=correlation_id,
    )


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/pending-discoveries/{{pending_id}}/dispositions",
    response_model=list[PendingDispositionResponse],
    tags=["workspaces"],
    summary="Read pending-discovery disposition history",
)
def pending_disposition_history(
    workspace_id: str, pending_id: str, database: DatabaseDep
) -> list[PendingDispositionResponse]:
    """Return the append-only history used to restore a dismissal."""
    return DiscoveryDispositionService(database).history(workspace_id, pending_id)


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/pending-dispositions/dismissed",
    response_model=list[DismissedDiscoveryResponse],
    tags=["workspaces"],
    summary="List dismissed discoveries available to restore",
)
def dismissed_pending_discoveries(
    workspace_id: str, database: DatabaseDep
) -> list[DismissedDiscoveryResponse]:
    """Enumerate latest dismissals without exposing superseded history as current."""
    return DiscoveryDispositionService(database).dismissed(workspace_id)


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/pending-dispositions/duplicate-candidates",
    response_model=list[PendingDuplicateCandidateResponse],
    tags=["workspaces"],
    summary="List pending discoveries available as duplicate targets",
)
def pending_duplicate_candidates(
    workspace_id: str, database: DatabaseDep
) -> list[PendingDuplicateCandidateResponse]:
    """Return active and dismissed roots without the bounded review-page limit."""
    return DiscoveryDispositionService(database).duplicate_candidates(workspace_id)


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/pending-discoveries",
    response_model=list[PendingDiscoveryResponse],
    tags=["workspaces"],
    summary="List discoveries awaiting enough evidence to evaluate",
)
def pending_discoveries(
    workspace_id: str,
    service: SourceScanServiceDep,
    limit: Annotated[int, Query(ge=1, le=250)] = 50,
    closed_only: bool = False,
    ready_limit: Annotated[int | None, Query(ge=1, le=250)] = None,
    attention_limit: Annotated[int | None, Query(ge=1, le=250)] = None,
) -> list[PendingDiscoveryResponse]:
    """List active discoveries or closed history, optionally paging readiness lanes."""
    return service.list_pending_discoveries(
        workspace_id,
        limit=limit,
        closed_only=closed_only,
        ready_limit=ready_limit,
        attention_limit=attention_limit,
    )


def _pending_queue_filters(
    readiness: PendingDiscoveryReadiness | None = None,
    relevance: PendingRelevanceKey | None = None,
    review_status: PendingReviewStatus | None = None,
    constraint: PendingConstraintRoute | None = None,
    category: DiscoveryRecommendation | None = None,
    arrangement: ArrangementCategory | None = None,
    provenance: PendingDiscoveryStagingProvenance | None = None,
    favorite: bool = False,
    kept: bool = False,
    search: Annotated[str, Query(max_length=200)] = "",
    employer: Annotated[
        list[Annotated[str, StringConstraints(max_length=200)]] | None, Query()
    ] = None,
) -> PendingQueueFilters:
    return PendingQueueFilters(
        readiness=readiness,
        relevance=relevance,
        review_status=review_status,
        constraint=constraint,
        category=category,
        arrangement=arrangement,
        provenance=provenance,
        favorite=favorite,
        kept=kept,
        search=search,
        employers=tuple(employer or ()),
    )


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/pending-discoveries/page",
    response_model=PendingDiscoveryPageResponse,
    tags=["workspaces"],
    summary="Page active discoveries by content readiness",
)
def pending_discovery_page(
    workspace_id: str,
    service: SourceScanServiceDep,
    filters: Annotated[PendingQueueFilters, Depends(_pending_queue_filters)],
    limit: Annotated[int, Query(ge=1, le=PENDING_PAGE_MAX_LIMIT)] = 50,
    ready_offset: Annotated[int, Query(ge=0)] = 0,
    attention_offset: Annotated[int, Query(ge=0)] = 0,
    expected_snapshot_sha256: Annotated[
        str | None, Query(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    ] = None,
    snapshot_before: Annotated[datetime | None, Query()] = None,
) -> PendingDiscoveryPageResponse:
    """Return stable, independently bounded ready and attention lanes of the filtered queue."""
    return service.pending_discovery_page(
        workspace_id,
        limit=limit,
        ready_offset=ready_offset,
        attention_offset=attention_offset,
        expected_snapshot_sha256=expected_snapshot_sha256,
        snapshot_before=snapshot_before,
        filters=filters,
    )


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/pending-discoveries/changes",
    response_model=PendingDiscoveryChangesResponse,
    tags=["workspaces"],
    summary="Count visible rows and publisher follow-up since a pending discovery snapshot",
)
def pending_discovery_changes(
    workspace_id: str,
    service: SourceScanServiceDep,
    snapshot_before: Annotated[datetime, Query()],
    filters: Annotated[PendingQueueFilters, Depends(_pending_queue_filters)],
) -> PendingDiscoveryChangesResponse:
    """Return counts only; a reviewer's own page load stays the source of ranked rows."""
    return service.pending_discovery_changes(
        workspace_id, snapshot_before=snapshot_before, filters=filters
    )


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/pending-discoveries/title-exclusion-collateral",
    response_model=TitleExclusionCollateralResponse,
    tags=["workspaces"],
    summary="Count queue rows each title-skip phrase would also hide",
)
def title_exclusion_collateral(
    workspace_id: str,
    service: SourceScanServiceDep,
    phrase: Annotated[
        list[Annotated[str, StringConstraints(max_length=120)]] | None,
        Query(max_length=MAX_COLLATERAL_PHRASES),
    ] = None,
) -> TitleExclusionCollateralResponse:
    """Report the collateral of a title-skip phrase against the queue as it stands."""
    return service.title_exclusion_collateral(workspace_id, phrase or [])


@router.post(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/pending-discoveries",
    response_model=FoundPostingAddResponse,
    tags=["workspaces"],
    summary="Add a posting you found yourself",
)
def add_found_posting(
    workspace_id: str,
    body: FoundPostingCreate,
    service: SourceScanServiceDep,
    correlation_id: CorrelationId,
) -> FoundPostingAddResponse:
    """Queue a posting from its link so evaluation can read it."""
    found = service.stage_found_posting(
        workspace_id,
        body.url,
        utc_now().date(),
        title=body.title,
        employer=body.employer,
        location=body.location,
        actor="local_user",
        origin="api",
        correlation_id=correlation_id,
        check_publisher_facts=body.check_publisher_facts,
    )
    if found is None:
        raise ConflictError("That posting is already staged or already tracked here.")
    return found


@router.put(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/pending-discoveries/{{pending_id}}/markers",
    response_model=PendingDiscoveryResponse,
    tags=["workspaces"],
    summary="Set a pending discovery's favorite and keep markers",
)
def set_pending_discovery_markers(
    workspace_id: str,
    pending_id: str,
    body: PendingMarkersRequest,
    service: SourceScanServiceDep,
    correlation_id: CorrelationId,
) -> PendingDiscoveryResponse:
    """Mark or unmark a discovery family; a marked family skips every unconfirmed dismissal."""
    return service.set_pending_markers(
        workspace_id, pending_id, body, correlation_id=correlation_id
    )


@router.put(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/pending-discoveries/{{pending_id}}/closing-date",
    response_model=PendingDiscoveryResponse,
    tags=["workspaces"],
    summary="Set a pending discovery's personal closing date",
)
def set_pending_discovery_closing_date(
    workspace_id: str,
    pending_id: str,
    body: ClosingDateEditInput,
    service: SourceScanServiceDep,
    correlation_id: CorrelationId,
) -> PendingDiscoveryResponse:
    """Save or reset a personal tracking date on the active discovery family."""
    return service.set_pending_closing_date(
        workspace_id, pending_id, body, correlation_id=correlation_id
    )


@router.post(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/pending-discoveries/reset",
    response_model=PendingQueueResetResponse,
    tags=["workspaces"],
    summary="Archive the whole active pending-discovery queue",
)
def reset_pending_discoveries(
    workspace_id: str,
    service: SourceScanServiceDep,
) -> PendingQueueResetResponse:
    """Clear every active discovery so a search can reprocess active jobs from scratch."""
    return service.reset_pending_discoveries(workspace_id)


@router.patch(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/pending-discoveries/{{pending_id}}",
    response_model=PendingDiscoveryResponse,
    tags=["workspaces"],
    summary="Correct the display title on one staged pending discovery",
)
def correct_pending_discovery_title(
    workspace_id: str,
    pending_id: str,
    body: PendingDiscoveryTitleUpdate,
    service: SourceScanServiceDep,
    correlation_id: CorrelationId,
) -> PendingDiscoveryResponse:
    """Overwrite the title hint on one pending row and append an audit event."""
    return service.correct_pending_discovery_title(
        workspace_id,
        pending_id,
        body.title,
        correlation_id=correlation_id,
    )


@router.post(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/pending-discoveries/{{pending_id}}/refresh",
    response_model=PendingDiscoveryResponse,
    tags=["workspaces"],
    summary="Refresh one pending discovery from its publisher",
)
def refresh_pending_discovery(
    workspace_id: str,
    pending_id: str,
    service: SourceScanServiceDep,
    correlation_id: CorrelationId,
) -> PendingDiscoveryResponse:
    """Replace mutable staged facts with current free publisher metadata."""
    return service.refresh_pending_discovery(
        workspace_id,
        pending_id,
        correlation_id=correlation_id,
    )


@router.post(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/pending-discoveries/{{pending_id}}/content",
    response_model=PendingPostingContentActionResponse,
    tags=["workspaces"],
    summary="Attach user-obtained text to an unread pending posting",
)
def attach_pending_posting_content(
    workspace_id: str,
    pending_id: str,
    body: PendingPostingTextAttach,
    service: PostingResolutionServiceDep,
    correlation_id: CorrelationId,
) -> PendingPostingContentActionResponse:
    """Store attested text as current evidence without reading a policy-gated URL."""
    evidence_id = service.store_user_supplied_text(
        workspace_id,
        pending_id,
        body.content,
        attested_current_exact_posting=body.attested_current_exact_posting,
        expected_current_content_id=body.expected_current_content_id,
        expected_url=body.expected_url,
        correlation_id=correlation_id,
    )
    return PendingPostingContentActionResponse(
        pending_discovery_id=pending_id,
        evidence_id=evidence_id,
    )


@router.post(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/pending-discoveries/{{pending_id}}/confirm-identity",
    response_model=PendingPostingContentActionResponse,
    tags=["workspaces"],
    summary="Confirm retained publisher evidence identifies a pending posting",
)
def confirm_pending_posting_identity(
    workspace_id: str,
    pending_id: str,
    body: PendingPostingIdentityConfirm,
    service: PostingResolutionServiceDep,
    correlation_id: CorrelationId,
) -> PendingPostingContentActionResponse:
    """Append a user identity attestation without modifying the retained body."""
    evidence_id = service.confirm_current_identity(
        workspace_id,
        pending_id,
        attested_same_posting=body.attested_same_posting,
        expected_current_content_id=body.expected_current_content_id,
        expected_url=body.expected_url,
        correlation_id=correlation_id,
    )
    return PendingPostingContentActionResponse(
        pending_discovery_id=pending_id,
        evidence_id=evidence_id,
    )


@router.post(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/pending-discoveries/reconcile-closures",
    response_model=PendingClosureReconcileResponse,
    tags=["workspaces"],
    summary="Apply current saved publisher closure evidence to pending discoveries",
)
def reconcile_pending_discovery_closures(
    workspace_id: str,
    body: PendingClosureReconcileRequest,
    service: SourceScanServiceDep,
    correlation_id: CorrelationId,
) -> PendingClosureReconcileResponse:
    """Move only explicitly closed saved postings out of active triage without a publisher read."""
    return service.reconcile_saved_closures(
        workspace_id,
        body,
        correlation_id=correlation_id,
    )


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/search-title-proposals",
    response_model=SearchTitleProposalInboxResponse,
    tags=["workspaces"],
    summary="List titles a search proposed and how the user ruled on each",
)
def search_title_proposals(
    workspace_id: str, service: SearchTitleProposalServiceDep
) -> SearchTitleProposalInboxResponse:
    """List workspace-effective title proposals with provenance and capacity."""
    return service.inbox(workspace_id)


@router.post(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/search-title-proposals/{{proposal_id}}/accept",
    response_model=SearchTitleProposalResponse,
    tags=["workspaces"],
    summary="Allow searches to look under a proposed title",
)
def accept_search_title(
    workspace_id: str, proposal_id: str, service: SearchTitleProposalServiceDep
) -> SearchTitleProposalResponse:
    """Accept one proposed search title."""
    return service.accept(workspace_id, proposal_id)


@router.post(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/search-title-proposals/{{proposal_id}}/reject",
    response_model=SearchTitleProposalResponse,
    tags=["workspaces"],
    summary="Refuse a proposed title, now and when it is proposed again",
)
def reject_search_title(
    workspace_id: str,
    proposal_id: str,
    service: SearchTitleProposalServiceDep,
    body: ProposalRejectRequest | None = None,
) -> SearchTitleProposalResponse:
    """Reject one proposed search title, optionally naming the reason."""
    return service.reject(workspace_id, proposal_id, body.reason if body else None)


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/search-archetype-proposals",
    response_model=SearchArchetypeProposalInboxResponse,
    tags=["workspaces"],
    summary="List employer archetypes a search proposed and how the user ruled on each",
)
def search_archetype_proposals(
    workspace_id: str, service: SearchArchetypeProposalServiceDep
) -> SearchArchetypeProposalInboxResponse:
    """List workspace-effective employer proposals with provenance and capacity."""
    return service.inbox(workspace_id)


@router.post(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/search-archetype-proposals/{{proposal_id}}/accept",
    response_model=SearchArchetypeProposalResponse,
    tags=["workspaces"],
    summary="State a proposed kind of employer to every later search",
)
def accept_search_archetype(
    workspace_id: str, proposal_id: str, service: SearchArchetypeProposalServiceDep
) -> SearchArchetypeProposalResponse:
    """Accept one proposed employer archetype."""
    return service.accept(workspace_id, proposal_id)


@router.post(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/search-archetype-proposals/{{proposal_id}}/reject",
    response_model=SearchArchetypeProposalResponse,
    tags=["workspaces"],
    summary="Refuse a proposed kind of employer, now and when it is proposed again",
)
def reject_search_archetype(
    workspace_id: str,
    proposal_id: str,
    service: SearchArchetypeProposalServiceDep,
    body: ProposalRejectRequest | None = None,
) -> SearchArchetypeProposalResponse:
    """Reject one proposed employer archetype, optionally naming the reason."""
    return service.reject(workspace_id, proposal_id, body.reason if body else None)


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/search-cluster-proposals",
    response_model=SearchClusterProposalInboxResponse,
    tags=["workspaces"],
    summary="List capabilities and domains a search proposed, and the rulings on them",
)
def search_cluster_proposals(
    workspace_id: str, service: SearchClusterProposalServiceDep
) -> SearchClusterProposalInboxResponse:
    """List workspace-effective cluster proposals with provenance and capacity."""
    return service.inbox(workspace_id)


@router.post(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/search-cluster-proposals/{{proposal_id}}/accept",
    response_model=SearchClusterProposalResponse,
    tags=["workspaces"],
    summary="Rank and search by a proposed capability or domain",
)
def accept_search_cluster(
    workspace_id: str, proposal_id: str, service: SearchClusterProposalServiceDep
) -> SearchClusterProposalResponse:
    """Accept one proposed capability or domain."""
    return service.accept(workspace_id, proposal_id)


@router.post(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/search-cluster-proposals/{{proposal_id}}/reject",
    response_model=SearchClusterProposalResponse,
    tags=["workspaces"],
    summary="Refuse a proposed capability or domain, now and when it returns",
)
def reject_search_cluster(
    workspace_id: str,
    proposal_id: str,
    service: SearchClusterProposalServiceDep,
    body: ProposalRejectRequest | None = None,
) -> SearchClusterProposalResponse:
    """Reject one proposed capability or domain, optionally naming the reason."""
    return service.reject(workspace_id, proposal_id, body.reason if body else None)


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/posting-refusals",
    response_model=list[PostingRefusalResponse],
    tags=["workspaces"],
    summary="List postings a stated condition refused before evaluation",
)
def posting_refusals(
    workspace_id: str,
    service: SourceScanServiceDep,
    limit: Annotated[int, Query(ge=1, le=250)] = 50,
) -> list[PostingRefusalResponse]:
    """List postings refused without a fit analysis, and what refused them."""
    return service.list_refusals(workspace_id, limit=limit)


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/refusal-pattern-proposals",
    response_model=list[RefusalPatternProposalResponse],
    tags=["workspaces"],
    summary="List proposed hard-gate exclusions from recurring refusals",
)
def refusal_pattern_proposals(
    workspace_id: str, service: RefusalPatternProposalServiceDep
) -> list[RefusalPatternProposalResponse]:
    """List refusal-pattern proposals for a workspace."""
    return service.list(workspace_id)


@router.post(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/refusal-pattern-proposals/{{proposal_id}}/accept",
    response_model=RefusalPatternProposalResponse,
    tags=["workspaces"],
    summary="Settle a refusal pattern so it stops being proposed",
)
def accept_refusal_pattern(
    workspace_id: str, proposal_id: str, service: RefusalPatternProposalServiceDep
) -> RefusalPatternProposalResponse:
    """Settle one refusal pattern so later aggregation stops proposing it."""
    return service.accept(workspace_id, proposal_id)


@router.post(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/refusal-pattern-proposals/{{proposal_id}}/reject",
    response_model=RefusalPatternProposalResponse,
    tags=["workspaces"],
    summary="Reject a refusal-pattern proposal",
)
def reject_refusal_pattern(
    workspace_id: str, proposal_id: str, service: RefusalPatternProposalServiceDep
) -> RefusalPatternProposalResponse:
    """Reject one refusal-pattern proposal."""
    return service.reject(workspace_id, proposal_id)
