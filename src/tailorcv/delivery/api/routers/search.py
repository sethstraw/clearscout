"""Search HTTP routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from tailorcv.application import prompts
from tailorcv.application.discovery import guided_campaign_report
from tailorcv.application.discovery.board_scan import queue_rescreen_for_saved_parameters
from tailorcv.application.discovery.guided_campaign import (
    DEFAULT_MAX_HOSTED_SEARCHES,
    DEFAULT_MAX_MODEL_CALLS,
)
from tailorcv.application.workspaces.parameter_models import (
    SearchParametersSavedResponse,
    SearchParametersStateResponse,
    SearchParameterVersionResponse,
)
from tailorcv.application.workspaces.prompt_library import overrides_in
from tailorcv.delivery.api.constants import API_PREFIX
from tailorcv.delivery.api.dependencies import (
    CorrelationId,
    DatabaseDep,
    SearchParametersServiceDep,
    TaskQueueServiceDep,
    WorkspaceId,
)
from tailorcv.delivery.api.models.search import (
    GuidedCampaignAbandonRequest,
    GuidedCampaignAdmittedPostingResponse,
    GuidedCampaignAttemptPreviewResponse,
    GuidedCampaignAttemptRowResponse,
    GuidedCampaignBoardAdmissionResponse,
    GuidedCampaignBoundsResponse,
    GuidedCampaignBranchPreviewResponse,
    GuidedCampaignBranchRowResponse,
    GuidedCampaignFindingResponse,
    GuidedCampaignNotStagedUrlResponse,
    GuidedCampaignPreviewResponse,
    GuidedCampaignReportResponse,
    SearchParameterCaps,
    SearchParameterOptions,
    SearchParametersBody,
)
from tailorcv.domain.search_parameters import (
    ARRANGEMENTS,
    BASES,
    EMPLOYMENT_TYPES,
    LANES,
    LEVELS,
    MAX_ARCHETYPES,
    MAX_CLUSTER_TERMS,
    MAX_CLUSTERS,
    MAX_TITLES,
    Acceptance,
    WorkEligibility,
)

router = APIRouter()


def _campaign_preview_response(
    preview: guided_campaign_report.CampaignPreview,
) -> GuidedCampaignPreviewResponse:
    """Render one compiled campaign preview as its wire response."""
    manifest = preview.manifest
    positions = {branch.key: position for position, branch in enumerate(manifest.branches, 1)}
    attempt_positions = {
        branch_key: attempt.position
        for attempt in manifest.attempts
        for branch_key in attempt.branch_keys
    }
    return GuidedCampaignPreviewResponse(
        manifest_sha256=manifest.sha256,
        cycle=preview.cycle,
        compiler_version=manifest.compiler_version,
        bounds=GuidedCampaignBoundsResponse(**manifest.bounds.document()),
        branches=[
            GuidedCampaignBranchPreviewResponse(
                position=positions[branch.key],
                key=branch.key,
                label=branch.label,
                kind=branch.kind,
                priority_family=branch.priority_family,
                protected=branch.protected,
                markets=list(branch.markets),
                floor_query=branch.floor_query,
                attempt_position=attempt_positions.get(branch.key),
                prior_outcome=preview.prior_outcomes.get(branch.key),
                last_searched_at=preview.last_searched_at.get(branch.key),
            )
            for branch in manifest.branches
        ],
        attempts=[
            GuidedCampaignAttemptPreviewResponse(
                position=attempt.position,
                branch_positions=[positions[key] for key in attempt.branch_keys],
                search_cap=attempt.search_cap,
            )
            for attempt in manifest.attempts
        ],
        provider=preview.provider,
        model=preview.model,
        supersedes_campaign_id=preview.supersedes_campaign_id,
        planned_search_fee_cents=preview.planned_search_fee_cents,
        estimated_cost_cents=preview.estimated_cost_cents,
        estimate_basis=preview.estimate_basis,
    )


def _campaign_report_response(
    report: guided_campaign_report.CampaignReport,
) -> GuidedCampaignReportResponse:
    """Render one campaign report as its wire response."""
    return GuidedCampaignReportResponse(
        campaign_id=report.campaign_id,
        manifest_sha256=report.manifest_sha256,
        cycle=report.cycle,
        status=report.status,
        max_model_calls=report.max_model_calls,
        max_hosted_searches=report.max_hosted_searches,
        model_calls_used=report.model_calls_used,
        hosted_searches_used=report.hosted_searches_used,
        cost_cents=report.cost_cents,
        known_posting_count=report.known_posting_count,
        registered_board_count=report.registered_board_count,
        findings=[
            GuidedCampaignFindingResponse(
                captured_url_id=item.captured_url_id,
                url=item.url,
                staged_basis=item.staged_basis,
                pending_discovery_id=item.pending_discovery_id,
                employer=item.employer,
                title=item.title,
                location=item.location,
                branch_labels=list(item.branch_labels),
                model_rationale=item.model_why,
            )
            for item in report.findings
        ],
        boards_admitted=[
            GuidedCampaignBoardAdmissionResponse(
                url=item.url,
                source_id=item.source_id,
                employer=item.employer,
                branch_labels=list(item.branch_labels),
                admitted_postings=[
                    GuidedCampaignAdmittedPostingResponse(
                        pending_discovery_id=posting.pending_discovery_id,
                        url=posting.url,
                        employer=posting.employer,
                        title=posting.title,
                        location=posting.location,
                    )
                    for posting in item.admitted_postings
                ],
            )
            for item in report.boards_admitted
        ],
        not_staged=[
            GuidedCampaignNotStagedUrlResponse(
                url=item.url, outcome=item.outcome, detail=item.detail
            )
            for item in report.not_staged
        ],
        branches=[
            GuidedCampaignBranchRowResponse(
                position=item.position,
                label=item.label,
                priority_family=item.priority_family,
                protected=item.protected,
                attempt_position=item.attempt_position,
                outcome=item.outcome,
                matched_queries=list(item.matched_queries),
            )
            for item in report.branches
        ],
        attempts=[
            GuidedCampaignAttemptRowResponse(
                position=item.position,
                state=item.state,
                hosted_search_count=item.hosted_search_count,
                page_action_count=item.page_action_count,
                finish=item.finish,
                captured_not_selected=item.captured_not_selected,
                cost_cents=item.cost_cents,
                error_category=item.error_category,
            )
            for item in report.attempts
        ],
        warnings=list(report.warnings),
        stranded_processing_attempts=report.stranded_processing_attempts,
    )


@router.get(
    f"{API_PREFIX}/guided-campaign/preview",
    response_model=GuidedCampaignPreviewResponse,
    tags=["job search"],
)
def guided_campaign_preview(
    workspace_id: WorkspaceId,
    database: DatabaseDep,
    # The module defaults are the starting point, not a ceiling: the approval
    # payload the runner honors bounds these the same way, at one and above.
    max_model_calls: Annotated[int, Query(ge=1)] = DEFAULT_MAX_MODEL_CALLS,
    max_hosted_searches: Annotated[int, Query(ge=1)] = DEFAULT_MAX_HOSTED_SEARCHES,
) -> GuidedCampaignPreviewResponse:
    """Preview the campaign the current inputs and bounds would approve. Writes nothing."""
    with database.session() as session:
        overrides = {key: text for key, (_, text) in overrides_in(session, workspace_id).items()}
        with prompts.overriding(overrides):
            preview = guided_campaign_report.build_campaign_preview(
                session,
                workspace_id,
                max_model_calls=max_model_calls,
                max_hosted_searches=max_hosted_searches,
            )
    return _campaign_preview_response(preview)


@router.get(
    f"{API_PREFIX}/guided-campaigns/latest",
    response_model=GuidedCampaignReportResponse | None,
    tags=["job search"],
)
def latest_guided_campaign_report(
    workspace_id: WorkspaceId,
    database: DatabaseDep,
) -> GuidedCampaignReportResponse | None:
    """Return the newest campaign's report, or null before any campaign."""
    with database.session() as session:
        report = guided_campaign_report.latest_campaign_report(session, workspace_id)
    return _campaign_report_response(report) if report is not None else None


@router.get(
    f"{API_PREFIX}/guided-campaigns/{{campaign_id}}",
    response_model=GuidedCampaignReportResponse,
    tags=["job search"],
)
def guided_campaign_report_by_id(
    campaign_id: str,
    workspace_id: WorkspaceId,
    database: DatabaseDep,
) -> GuidedCampaignReportResponse:
    """Return one campaign's report by id, once its cycle is no longer the latest."""
    with database.session() as session:
        report = guided_campaign_report.campaign_report(session, workspace_id, campaign_id)
    return _campaign_report_response(report)


@router.post(
    f"{API_PREFIX}/guided-campaigns/{{campaign_id}}/attempts/{{position}}/abandon",
    response_model=GuidedCampaignReportResponse,
    tags=["job search"],
)
def abandon_guided_campaign_attempt(
    campaign_id: str,
    position: int,
    body: GuidedCampaignAbandonRequest,
    database: DatabaseDep,
) -> GuidedCampaignReportResponse:
    """Abandon an uncertain attempt so the campaign can finish without it."""
    with database.session() as session:
        report = guided_campaign_report.abandon_uncertain_attempt(
            session, body.workspace_id, campaign_id, position
        )
    return _campaign_report_response(report)


@router.get(
    f"{API_PREFIX}/job-search-parameters/options",
    response_model=SearchParameterOptions,
    tags=["job search"],
)
def job_search_parameter_options() -> SearchParameterOptions:
    """Publish the vocabularies a parameters form renders as choices."""
    return SearchParameterOptions(
        arrangements=list(ARRANGEMENTS),
        levels=list(LEVELS),
        bases=list(BASES),
        eligibility=[item.value for item in WorkEligibility],
        acceptance=[item.value for item in Acceptance],
        lanes=list(LANES),
        employment_types=list(EMPLOYMENT_TYPES),
        caps=SearchParameterCaps(
            titles=MAX_TITLES,
            archetypes=MAX_ARCHETYPES,
            capabilities=MAX_CLUSTERS,
            domains=MAX_CLUSTERS,
            cluster_terms=MAX_CLUSTER_TERMS,
        ),
    )


@router.get(
    f"{API_PREFIX}/job-search-parameters",
    response_model=SearchParametersStateResponse,
    tags=["job search"],
)
def read_job_search_parameters(
    workspace_id: WorkspaceId,
    service: SearchParametersServiceDep,
) -> SearchParametersStateResponse:
    """Read the current job-search parameters."""
    return service.describe(workspace_id)


@router.put(
    f"{API_PREFIX}/job-search-parameters",
    response_model=SearchParametersSavedResponse,
    tags=["job search"],
)
def write_job_search_parameters(
    correlation_id: CorrelationId,
    body: SearchParametersBody,
    service: SearchParametersServiceDep,
    tasks: TaskQueueServiceDep,
) -> SearchParametersSavedResponse:
    """Save a complete job-search parameter version and queue a re-screen against it."""
    saved = service.save(
        body.workspace_id,
        body.parameters,
        origin="api",
        correlation_id=correlation_id,
        expected_version=body.expected_version,
    )
    queue_rescreen_for_saved_parameters(tasks, saved, correlation_id=correlation_id)
    return saved


@router.get(
    f"{API_PREFIX}/job-search-parameters/history",
    response_model=list[SearchParameterVersionResponse],
    tags=["job search"],
)
def job_search_parameter_history(
    workspace_id: WorkspaceId,
    service: SearchParametersServiceDep,
) -> list[SearchParameterVersionResponse]:
    """List job-search parameter history."""
    return service.history(workspace_id)
