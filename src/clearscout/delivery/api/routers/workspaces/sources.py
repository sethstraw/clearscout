"""Workspace source registration and scan-history routes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from itertools import islice
from typing import Annotated

from anyio import CancelScope
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.types import Receive, Scope, Send

from clearscout.application.discovery.funnel_audit import (
    FunnelCandidateKind,
    FunnelReason,
    FunnelStage,
)
from clearscout.application.discovery.scan_queries import ScanOutcomeFilters
from clearscout.application.job_search.models import (
    ScreenedOutTitlePageResponse,
    ScreenedOutTitleReviewRequest,
    ScreenedOutTitleReviewResponse,
    SourceListResponse,
    SourcePreviewResponse,
    SourceResponse,
    SourceScanHistoryPageResponse,
    SourceScanHistoryStatus,
    SourceScanOutcomeDirection,
    SourceScanOutcomePageResponse,
    SourceScanOutcomeSort,
)
from clearscout.application.tasks import TaskResponse
from clearscout.application.workspaces.models import ScanScheduleInput, ScanScheduleResponse
from clearscout.delivery.api.constants import API_PREFIX
from clearscout.delivery.api.dependencies import (
    CorrelationId,
    ScanScheduleServiceDep,
    SourceScanServiceDep,
)
from clearscout.delivery.api.models.workspaces import SourceCreate, SourceEdit, SourcePreviewRequest

router = APIRouter()


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/scan-schedule",
    response_model=ScanScheduleResponse,
    tags=["workspaces"],
    summary="Read the opt-in local scan calendar and its next occurrence",
)
def scan_schedule(workspace_id: str, service: ScanScheduleServiceDep) -> ScanScheduleResponse:
    """Read the calendar without starting a scan."""
    return service.read(workspace_id)


@router.put(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/scan-schedule",
    response_model=ScanScheduleResponse,
    tags=["workspaces"],
    summary="Save or pause one free local scan schedule",
)
def save_scan_schedule(
    workspace_id: str,
    body: ScanScheduleInput,
    service: ScanScheduleServiceDep,
    correlation_id: CorrelationId,
) -> ScanScheduleResponse:
    """Save a calendar; scans run only while the local worker is running."""
    return service.save(workspace_id, body, origin="api", correlation_id=correlation_id)


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/sources",
    response_model=SourceListResponse,
    tags=["workspaces"],
    summary="List the employer boards this workspace sweeps",
)
def sources(workspace_id: str, service: SourceScanServiceDep) -> SourceListResponse:
    """List every board source with its address and last scan outcome."""
    return service.list_sources(workspace_id)


@router.post(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/sources",
    response_model=SourceResponse,
    tags=["workspaces"],
    summary="Register one source by hand",
)
def add_source(
    workspace_id: str,
    body: SourceCreate,
    correlation_id: CorrelationId,
    service: SourceScanServiceDep,
) -> SourceResponse:
    """Register a source, kept even when its address names no known platform.

    A career page that is refused or fails to load is answered with a problem, and
    nothing is saved.
    """
    return service.add_source(
        workspace_id,
        body.name,
        body.url,
        source_type=body.source_type,
        terms=body.terms,
        origin="api",
        correlation_id=correlation_id,
    )


@router.patch(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/sources/{{source_id}}",
    response_model=SourceResponse,
    tags=["workspaces"],
    summary="Change one board's name, address, careers page, order, or whether it is swept",
)
def edit_source(
    workspace_id: str,
    source_id: str,
    body: SourceEdit,
    service: SourceScanServiceDep,
    correlation_id: CorrelationId,
) -> SourceResponse:
    """Apply the stated changes to one board source and return what it now is."""
    return service.update_source(
        workspace_id,
        source_id,
        name=body.name,
        url=body.url,
        careers_url=body.careers_url,
        enabled=body.enabled,
        priority=body.priority,
        terms=body.terms,
        correlation_id=correlation_id,
    )


@router.delete(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/sources/{{source_id}}",
    status_code=204,
    tags=["workspaces"],
    summary="Stop sweeping one employer board",
)
def archive_source(workspace_id: str, source_id: str, service: SourceScanServiceDep) -> None:
    """Archive one board source, keeping the scans it has already recorded."""
    service.archive_source(workspace_id, source_id)


@router.post(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/sources/preview",
    response_model=SourcePreviewResponse,
    tags=["workspaces"],
    summary="Resolve an address before saving it as a source",
)
def preview_source(
    workspace_id: str,
    body: SourcePreviewRequest,
    service: SourceScanServiceDep,
) -> SourcePreviewResponse:
    """Report what board platform an address resolves to, without saving anything."""
    return service.preview_url(workspace_id, body.url, body.source_type)


@router.post(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/sources/{{source_id}}/revalidate",
    response_model=SourceResponse,
    tags=["workspaces"],
    summary="Re-run board resolution against one saved source",
)
def revalidate_source(
    workspace_id: str, source_id: str, service: SourceScanServiceDep
) -> SourceResponse:
    """Re-resolve one source's saved address and persist what was found."""
    return service.resolve_platform(workspace_id, source_id)


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/source-scans",
    response_model=SourceScanHistoryPageResponse,
    tags=["workspaces"],
    summary="List retained board scans and the source settings used",
)
def source_scan_history(
    workspace_id: str,
    service: SourceScanServiceDep,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=250)] = 50,
    source_id: Annotated[str, Query(max_length=36)] = "",
    status: SourceScanHistoryStatus | None = None,
    latest_only: bool = False,
) -> SourceScanHistoryPageResponse:
    """List board scan outcomes with the source priority in force."""
    return service.list_scan_history(
        workspace_id,
        offset=offset,
        limit=limit,
        source_id=source_id,
        status=status,
        latest_only=latest_only,
    )


@router.post(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/source-scans/{{source_scan_id}}/continue",
    response_model=TaskResponse,
    status_code=202,
    tags=["workspaces"],
    summary="Read one bounded batch of a scan's deferred publisher links",
)
def continue_source_scan(
    workspace_id: str, source_scan_id: str, service: SourceScanServiceDep
) -> TaskResponse:
    """Enqueue exactly one continuation task for a scan's current generation."""
    return service.request_continuation(workspace_id, source_scan_id)


def _scan_outcome_filters(
    search: str = "",
    source: str = "",
    terminal_stage: FunnelStage | None = None,
    reason: FunnelReason | None = None,
    candidate_kind: FunnelCandidateKind | None = None,
    sort: SourceScanOutcomeSort = "created_at",
    direction: SourceScanOutcomeDirection = "desc",
) -> ScanOutcomeFilters:
    return ScanOutcomeFilters(
        search=search,
        source=source,
        terminal_stage=terminal_stage,
        reason=reason,
        candidate_kind=candidate_kind,
        sort=sort,
        direction=direction,
    )


# Paging is a dependency so its query parameters follow the filters in the schema.
def _scan_outcome_page(
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=250)] = 100,
) -> tuple[int, int]:
    return offset, limit


class _ScanOutcomeExportResponse(StreamingResponse):
    def __init__(self, content: AsyncIterator[str], close: Callable[[], None]) -> None:
        super().__init__(content, media_type="application/x-ndjson")
        self._close_records = close

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with CancelScope(shield=True):
                await run_in_threadpool(self._close_records)


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/source-scan-outcomes/export",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"application/x-ndjson": {"schema": {"type": "string"}}},
            "description": "Each line is one SourceScanOutcomeResponse JSON object.",
        }
    },
    tags=["workspaces"],
    summary="Stream the complete filtered candidate ledger without page metadata",
)
async def export_source_scan_outcomes(
    workspace_id: str,
    task_id: Annotated[str, Query(min_length=1)],
    service: SourceScanServiceDep,
    filters: Annotated[ScanOutcomeFilters, Depends(_scan_outcome_filters)],
) -> StreamingResponse:
    """Export typed ledger records with the same filters and task scope as the list."""
    records = service.export_scan_outcomes(workspace_id, task_id=task_id, filters=filters)

    def chunk() -> str:
        return "".join(row.model_dump_json() + "\n" for row in islice(records, 250))

    try:
        first = await run_in_threadpool(chunk)
    except BaseException:
        with CancelScope(shield=True):
            await run_in_threadpool(records.close)
        raise

    async def stream() -> AsyncIterator[str]:
        try:
            part = first
            while part:
                yield part
                part = await run_in_threadpool(chunk)
        finally:
            with CancelScope(shield=True):
                await run_in_threadpool(records.close)

    return _ScanOutcomeExportResponse(stream(), records.close)


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/source-scan-outcomes",
    response_model=SourceScanOutcomePageResponse,
    tags=["workspaces"],
    summary="List the body-free candidate ledger for one discovery task",
)
def source_scan_outcomes(
    workspace_id: str,
    task_id: Annotated[str, Query(min_length=1)],
    service: SourceScanServiceDep,
    filters: Annotated[ScanOutcomeFilters, Depends(_scan_outcome_filters)],
    page: Annotated[tuple[int, int], Depends(_scan_outcome_page)],
) -> SourceScanOutcomePageResponse:
    """List every durably retained candidate decision for one discovery task."""
    offset, limit = page
    return service.list_scan_outcomes(
        workspace_id, task_id=task_id, filters=filters, offset=offset, limit=limit
    )


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/screened-out-titles",
    response_model=ScreenedOutTitlePageResponse,
    tags=["workspaces"],
    summary="List screened-out titles across every scan, one row per address",
)
def screened_out_titles(
    workspace_id: str,
    service: SourceScanServiceDep,
    needs_review: bool | None = None,
    search: str = "",
    source: str = "",
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=250)] = 100,
) -> ScreenedOutTitlePageResponse:
    """List screened-out titles with first and last sighting and review state."""
    return service.list_screened_out_titles(
        workspace_id,
        needs_review=needs_review,
        search=search,
        source=source,
        offset=offset,
        limit=limit,
    )


@router.post(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/screened-out-titles/review",
    response_model=ScreenedOutTitleReviewResponse,
    tags=["workspaces"],
    summary="Mark screened-out titles reviewed until their title finding changes",
)
def review_screened_out_titles(
    workspace_id: str,
    request: ScreenedOutTitleReviewRequest,
    service: SourceScanServiceDep,
) -> ScreenedOutTitleReviewResponse:
    """Mark the named screened-out titles reviewed against their current finding."""
    return service.mark_screened_out_titles_reviewed(workspace_id, request.ids)
