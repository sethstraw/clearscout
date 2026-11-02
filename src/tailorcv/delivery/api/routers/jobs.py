"""Jobs HTTP routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import Response

from tailorcv.application import job_search
from tailorcv.application.contracts import ApplicationEventInput, EvaluationCommit
from tailorcv.application.job_search.models import (
    ApplicationEventResponse,
    ClosingDateEditInput,
    EvidenceSourceResponse,
    FitReportResponse,
    JobArchiveBatchResponse,
    JobArchiveResponse,
    JobDetailResponse,
    JobListQuery,
    JobPageResponse,
    ManualJobReviewInput,
    PostingDiffResponse,
    PostingHintBody,
    PostingHintResponse,
    PostingObservationBodyResponse,
)
from tailorcv.application.tasks import TaskResponse
from tailorcv.delivery.api.constants import API_PREFIX
from tailorcv.delivery.api.dependencies import (
    ArtifactServiceDep,
    CorrelationId,
    DatabaseDep,
    IdempotencyKey,
    TaskQueueServiceDep,
    WorkflowServiceDep,
    WorkspaceId,
)
from tailorcv.delivery.api.models.base import FREE_MODE_RESPONSES
from tailorcv.delivery.api.models.jobs import (
    JobArchiveBatchBody,
    TailoringPreflightResponse,
)

router = APIRouter()


@router.put(
    f"{API_PREFIX}/jobs/{{job_posting_id}}/manual-review",
    response_model=JobDetailResponse,
    tags=["jobs"],
)
def save_manual_job_review(
    job_posting_id: str,
    body: ManualJobReviewInput,
    workspace_id: WorkspaceId,
    database: DatabaseDep,
    correlation_id: CorrelationId,
) -> JobDetailResponse:
    """Save a free human comparison judgment for a tracked job."""
    return job_search.save_manual_job_review(
        database, workspace_id, job_posting_id, body, correlation_id=correlation_id
    )


@router.put(
    f"{API_PREFIX}/jobs/{{job_posting_id}}/closing-date",
    response_model=JobDetailResponse,
    tags=["jobs"],
)
def set_closing_date(
    job_posting_id: str,
    body: ClosingDateEditInput,
    workspace_id: WorkspaceId,
    database: DatabaseDep,
    correlation_id: CorrelationId,
) -> JobDetailResponse:
    """Save or clear a personal closing date without provider work."""
    return job_search.set_closing_date(
        database, workspace_id, job_posting_id, body, correlation_id=correlation_id
    )


@router.post(
    f"{API_PREFIX}/jobs/archive",
    response_model=JobArchiveBatchResponse,
    tags=["jobs"],
)
def archive_jobs(
    body: JobArchiveBatchBody,
    correlation_id: CorrelationId,
    workspace_id: WorkspaceId,
    database: DatabaseDep,
) -> JobArchiveBatchResponse:
    """Atomically archive the exact tracked jobs selected by the person."""
    return job_search.archive_jobs(
        database,
        body.job_posting_ids,
        workspace_id,
        correlation_id=correlation_id,
    )


@router.get(f"{API_PREFIX}/jobs", response_model=JobPageResponse, tags=["jobs"])
def list_jobs(
    database: DatabaseDep,
    workspace_id: WorkspaceId,
    search: str = "",
    decision: Annotated[list[str] | None, Query()] = None,
    application_status: Annotated[list[str] | None, Query()] = None,
    posting_status: Annotated[list[str] | None, Query()] = None,
    sort: str = "recorded_score",
    descending: bool = True,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=250)] = 50,
    completeness: str = "",
    place: Annotated[list[str] | None, Query()] = None,
    employer: Annotated[list[str] | None, Query()] = None,
    include_archived: bool = False,
) -> JobPageResponse:
    """List and filter jobs in the durable ledger."""
    query = JobListQuery(
        workspace_id=workspace_id,
        search=search,
        decisions=tuple(decision or ()),
        application_statuses=tuple(application_status or ()),
        posting_statuses=tuple(posting_status or ()),
        sort=sort,
        descending=descending,
        offset=offset,
        limit=limit,
        completeness=completeness,
        places=tuple(place or ()),
        employers=tuple(employer or ()),
        include_archived=include_archived,
    )
    items, total = job_search.list_jobs(database, query)
    return JobPageResponse(
        items=items,
        total=total,
        offset=offset,
        limit=limit,
        employers=job_search.job_employer_facets(database, query),
    )


@router.get(
    f"{API_PREFIX}/jobs/{{job_posting_id}}",
    response_model=JobDetailResponse,
    tags=["jobs"],
)
def job_detail(
    job_posting_id: str,
    workspace_id: WorkspaceId,
    database: DatabaseDep,
    observation_offset: Annotated[int, Query(ge=0)] = 0,
    evaluation_offset: Annotated[int, Query(ge=0)] = 0,
    application_event_offset: Annotated[int, Query(ge=0)] = 0,
    history_limit: Annotated[int, Query(ge=1, le=250)] = 50,
) -> JobDetailResponse:
    """Read one job with bounded pages of durable history."""
    return job_search.job_detail(
        database,
        job_posting_id,
        workspace_id,
        observation_offset=observation_offset,
        evaluation_offset=evaluation_offset,
        application_event_offset=application_event_offset,
        history_limit=history_limit,
    )


@router.post(
    f"{API_PREFIX}/jobs/{{job_posting_id}}/evaluations/{{evaluation_id}}/review-currentness",
    response_model=JobDetailResponse,
    tags=["jobs"],
)
def review_evaluation_currentness(
    correlation_id: CorrelationId,
    job_posting_id: str,
    evaluation_id: str,
    workspace_id: WorkspaceId,
    database: DatabaseDep,
) -> JobDetailResponse:
    """Record a free review that deliberately retains a historical evaluation."""
    with database.session() as session:
        job_search.acknowledge_evaluation_currentness(
            session,
            workspace_id=workspace_id,
            job_posting_id=job_posting_id,
            evaluation_id=evaluation_id,
            correlation_id=correlation_id,
        )
    return job_search.job_detail(database, job_posting_id, workspace_id)


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/jobs/{{job_id}}/tailoring-preflight",
    response_model=TailoringPreflightResponse,
    tags=["jobs"],
)
def tailoring_preflight(
    workspace_id: str,
    job_id: str,
    service: WorkflowServiceDep,
    cover_letter: Annotated[bool, Query()] = True,
    approval_tailoring_run_id: Annotated[str | None, Query()] = None,
) -> TailoringPreflightResponse:
    """Check exact local package reuse without research or provider calls."""
    inputs = service.inputs(workspace_id, job_id)
    approval = (
        service.approval_manifest(workspace_id, approval_tailoring_run_id)
        if approval_tailoring_run_id is not None
        else None
    )
    return TailoringPreflightResponse.model_validate(
        service.tailoring_preflight(
            inputs,
            cover_letter=cover_letter,
            approval=approval,
        )
    )


@router.post(
    f"{API_PREFIX}/jobs/{{job_posting_id}}/archive",
    response_model=JobArchiveResponse,
    tags=["jobs"],
)
def archive_job(
    correlation_id: CorrelationId,
    job_posting_id: str,
    workspace_id: WorkspaceId,
    database: DatabaseDep,
) -> JobArchiveResponse:
    """Hide a job from the active ledger without deleting it."""
    return job_search.archive_job(
        database,
        job_posting_id,
        workspace_id,
        correlation_id=correlation_id,
    )


@router.post(
    f"{API_PREFIX}/jobs/{{job_posting_id}}/unarchive",
    response_model=JobArchiveResponse,
    tags=["jobs"],
)
def unarchive_job(
    correlation_id: CorrelationId,
    job_posting_id: str,
    workspace_id: WorkspaceId,
    database: DatabaseDep,
) -> JobArchiveResponse:
    """Restore an archived job to the active ledger."""
    return job_search.unarchive_job(
        database,
        job_posting_id,
        workspace_id,
        correlation_id=correlation_id,
    )


@router.get(
    f"{API_PREFIX}/evidence/{{evidence_id}}/source",
    response_model=EvidenceSourceResponse,
    tags=["jobs"],
)
def evidence_source(
    evidence_id: str,
    workspace_id: WorkspaceId,
    database: DatabaseDep,
    artifacts: ArtifactServiceDep,
) -> EvidenceSourceResponse:
    """Read the verified source behind one evidence record."""
    return job_search.evidence_source(database, artifacts, evidence_id, workspace_id)


@router.get(
    f"{API_PREFIX}/jobs/{{job_posting_id}}/fit-report",
    response_model=FitReportResponse,
    tags=["jobs"],
)
def fit_report(
    job_posting_id: str,
    workspace_id: WorkspaceId,
    database: DatabaseDep,
    artifacts: ArtifactServiceDep,
) -> FitReportResponse:
    """Read a job's verified stored fit report."""
    return job_search.fit_report(database, artifacts, job_posting_id, workspace_id)


@router.get(
    f"{API_PREFIX}/jobs/{{job_posting_id}}/fit-report/export",
    response_class=Response,
    tags=["jobs"],
)
def export_fit_report(
    job_posting_id: str,
    workspace_id: WorkspaceId,
    database: DatabaseDep,
    artifacts: ArtifactServiceDep,
) -> Response:
    """Download a job's verified fit report."""
    report = job_search.fit_report(database, artifacts, job_posting_id, workspace_id)
    return Response(
        content=report.markdown,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="fit-report-{job_posting_id}.md"'},
    )


@router.get(
    f"{API_PREFIX}/posting-observations/{{observation_id}}",
    response_model=PostingObservationBodyResponse,
    tags=["jobs"],
)
def posting_observation(
    observation_id: str,
    workspace_id: WorkspaceId,
    database: DatabaseDep,
    artifacts: ArtifactServiceDep,
) -> PostingObservationBodyResponse:
    """Read one verified posting observation."""
    return job_search.posting_observation(database, artifacts, observation_id, workspace_id)


@router.get(
    f"{API_PREFIX}/jobs/{{job_posting_id}}/posting-diff",
    response_model=PostingDiffResponse,
    tags=["jobs"],
)
def posting_diff(
    job_posting_id: str,
    workspace_id: WorkspaceId,
    from_observation_id: Annotated[str, Query(alias="from", min_length=1)],
    to_observation_id: Annotated[str, Query(alias="to", min_length=1)],
    database: DatabaseDep,
    artifacts: ArtifactServiceDep,
) -> PostingDiffResponse:
    """Compare two observations of one posting."""
    return job_search.posting_diff(
        database,
        artifacts,
        job_posting_id,
        workspace_id,
        from_observation_id,
        to_observation_id,
    )


@router.post(
    f"{API_PREFIX}/jobs/{{job_posting_id}}/reevaluate",
    response_model=TaskResponse,
    status_code=202,
    responses=FREE_MODE_RESPONSES,
    tags=["jobs"],
)
def reevaluate_job(
    correlation_id: CorrelationId,
    job_posting_id: str,
    workspace_id: WorkspaceId,
    service: TaskQueueServiceDep,
    idempotency_key: IdempotencyKey,
) -> TaskResponse:
    """Schedule a re-evaluation of one tracked posting against the stored observation.

    Runs through the same paid evaluation workflow used for a fresh discovery, so
    provider charges may apply.
    """
    service.require_worker()
    return service.enqueue(
        workspace_id=workspace_id,
        task_type="discovery.evaluate",
        payload={"job_posting_id": job_posting_id, "fresh": True},
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
    )


@router.post(
    f"{API_PREFIX}/evaluations",
    response_model=JobDetailResponse,
    status_code=201,
    tags=["jobs"],
)
def commit_evaluation(
    correlation_id: CorrelationId,
    body: EvaluationCommit,
    database: DatabaseDep,
    artifacts: ArtifactServiceDep,
) -> JobDetailResponse:
    """Commit one validated evaluation transaction."""
    return job_search.commit_evaluation(
        database,
        artifacts,
        body,
        correlation_id=correlation_id,
    )


@router.post(
    f"{API_PREFIX}/jobs/{{job_posting_id}}/application-events",
    response_model=ApplicationEventResponse,
    status_code=201,
    tags=["applications"],
)
def append_application_event(
    correlation_id: CorrelationId,
    job_posting_id: str,
    workspace_id: WorkspaceId,
    body: ApplicationEventInput,
    database: DatabaseDep,
    idempotency_key: IdempotencyKey,
) -> ApplicationEventResponse:
    """Append an idempotent application history event."""
    return job_search.append_application_event(
        database,
        job_posting_id,
        body,
        workspace_id=workspace_id,
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
    )


@router.put(
    f"{API_PREFIX}/jobs/{{job_posting_id}}/hint",
    response_model=PostingHintResponse,
    tags=["jobs"],
)
def set_posting_hint(
    correlation_id: CorrelationId,
    job_posting_id: str,
    workspace_id: WorkspaceId,
    body: PostingHintBody,
    database: DatabaseDep,
) -> PostingHintResponse:
    """Set or clear the user-supplied tailoring hint for one posting."""
    return job_search.set_posting_hint(
        database,
        job_posting_id,
        workspace_id,
        body.hint,
        correlation_id=correlation_id,
    )
