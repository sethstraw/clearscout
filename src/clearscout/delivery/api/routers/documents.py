"""Documents HTTP routes."""

from __future__ import annotations

import mimetypes
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from clearscout.application.documents import MAX_UPLOAD_BYTES, serves_inline
from clearscout.application.documents.models import (
    ArtifactDiffResponse,
    ArtifactResponse,
    CitationCheckResponse,
    ReviewDecisionResponse,
    TailoringReviewResponse,
)
from clearscout.delivery.api.constants import API_PREFIX
from clearscout.delivery.api.dependencies import (
    ApprovalServiceDep,
    CorrelationId,
    DocumentServiceDep,
    WorkspaceId,
    bounded_body,
)
from clearscout.delivery.api.models.documents import (
    ApprovalDecisionBody,
    ArtifactRelabel,
    ArtifactRevert,
    CitationCheck,
    DocumentSave,
)
from clearscout.domain import ArtifactLogicalType

router = APIRouter()


@router.get(
    f"{API_PREFIX}/artifacts",
    response_model=list[ArtifactResponse],
    tags=["documents"],
)
def list_artifacts(
    service: DocumentServiceDep,
    workspace_id: WorkspaceId,
    entity_type: str | None = None,
    entity_id: str | None = None,
    include_archived: bool = False,
    include_related: bool = False,
) -> list[ArtifactResponse]:
    """List artifacts owned by a workspace."""
    return service.list_artifacts(
        workspace_id,
        entity_type=entity_type,
        entity_id=entity_id,
        include_archived=include_archived,
        include_related=include_related,
    )


@router.post(
    f"{API_PREFIX}/artifacts/upload",
    response_model=ArtifactResponse,
    status_code=201,
    tags=["documents"],
)
async def upload_artifact(
    correlation_id: CorrelationId,
    request: Request,
    service: DocumentServiceDep,
    workspace_id: WorkspaceId,
    entity_type: Annotated[str, Query(min_length=1)],
    entity_id: Annotated[str, Query(min_length=1)],
    logical_type: Annotated[ArtifactLogicalType, Query()],
    filename: Annotated[str, Query(min_length=1, max_length=240)],
    submission_role: Annotated[Literal["resume", "cover_letter", "other"] | None, Query()] = None,
) -> ArtifactResponse:
    """Upload and register an artifact through the intake boundary."""
    content = await bounded_body(request, MAX_UPLOAD_BYTES)
    return await run_in_threadpool(
        service.upload,
        workspace_id=workspace_id,
        entity_type=entity_type,
        entity_id=entity_id,
        logical_type=logical_type,
        filename=filename,
        mime_type=request.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0],
        content=content,
        submission_role=submission_role,
        correlation_id=correlation_id,
    )


@router.get(
    f"{API_PREFIX}/artifacts/{{artifact_id}}",
    response_model=ArtifactResponse,
    tags=["documents"],
)
def artifact_detail(
    artifact_id: str,
    workspace_id: WorkspaceId,
    service: DocumentServiceDep,
) -> ArtifactResponse:
    """Read safe metadata for one artifact."""
    return service.get(artifact_id, workspace_id)


@router.get(
    f"{API_PREFIX}/artifacts/{{artifact_id}}/content",
    response_class=Response,
    tags=["documents"],
)
def artifact_content(
    artifact_id: str,
    workspace_id: WorkspaceId,
    service: DocumentServiceDep,
    disposition: Literal["inline", "attachment"] = "inline",
) -> Response:
    """Read verified content for one artifact."""
    result = service.read(artifact_id, workspace_id)
    mime_type = result.artifact.mime_type.split(";", 1)[0].lower()
    safe_inline = serves_inline(mime_type)
    actual_disposition = "inline" if disposition == "inline" and safe_inline else "attachment"
    served_type = mime_type if safe_inline else "application/octet-stream"
    suffix = mimetypes.guess_extension(served_type, strict=False) or ".bin"
    raw_filename = f"{result.artifact.logical_type.replace('.', '-')}-{artifact_id}{suffix}"
    filename = "".join(
        character if character.isalnum() or character in "-_." else "-"
        for character in raw_filename
    )
    return Response(
        content=result.content,
        media_type=served_type,
        headers={"Content-Disposition": f'{actual_disposition}; filename="{filename}"'},
    )


@router.get(
    f"{API_PREFIX}/artifacts/{{artifact_id}}/versions",
    response_model=list[ArtifactResponse],
    tags=["documents"],
)
def artifact_versions(
    artifact_id: str,
    workspace_id: WorkspaceId,
    service: DocumentServiceDep,
) -> list[ArtifactResponse]:
    """List versions in an artifact family."""
    return service.versions(artifact_id, workspace_id)


@router.get(
    f"{API_PREFIX}/artifacts/{{artifact_id}}/diff/{{other_id}}",
    response_model=ArtifactDiffResponse,
    tags=["documents"],
)
def artifact_diff(
    artifact_id: str,
    other_id: str,
    workspace_id: WorkspaceId,
    service: DocumentServiceDep,
) -> ArtifactDiffResponse:
    """Compare two verified text artifacts."""
    return service.diff(artifact_id, other_id, workspace_id)


@router.post(
    f"{API_PREFIX}/artifacts/{{artifact_id}}/citation-check",
    response_model=CitationCheckResponse,
    tags=["documents"],
)
def check_artifact_citations(
    artifact_id: str,
    body: CitationCheck,
    service: DocumentServiceDep,
) -> CitationCheckResponse:
    """Check an unsaved edit against retained citations."""
    return service.citation_check(artifact_id, body.workspace_id, body.content)


@router.post(
    f"{API_PREFIX}/artifacts/{{artifact_id}}/versions",
    response_model=ArtifactResponse,
    status_code=201,
    tags=["documents"],
)
def save_artifact_version(
    correlation_id: CorrelationId,
    artifact_id: str,
    body: DocumentSave,
    service: DocumentServiceDep,
) -> ArtifactResponse:
    """Save an edited artifact as a new immutable version."""
    return service.save_version(
        artifact_id=artifact_id,
        workspace_id=body.workspace_id,
        content=body.content,
        author_type=body.author_type,
        override_reason=body.override_reason,
        correlation_id=correlation_id,
    )


@router.post(
    f"{API_PREFIX}/artifacts/{{artifact_id}}/revert",
    response_model=ArtifactResponse,
    status_code=201,
    tags=["documents"],
)
def revert_artifact(
    correlation_id: CorrelationId,
    artifact_id: str,
    body: ArtifactRevert,
    service: DocumentServiceDep,
) -> ArtifactResponse:
    """Append an older artifact's bytes as the current version."""
    return service.revert(
        current_artifact_id=artifact_id,
        target_artifact_id=body.target_artifact_id,
        workspace_id=body.workspace_id,
        correlation_id=correlation_id,
    )


@router.patch(
    f"{API_PREFIX}/artifacts/{{artifact_id}}",
    response_model=ArtifactResponse,
    tags=["documents"],
)
def relabel_artifact(
    correlation_id: CorrelationId,
    artifact_id: str,
    body: ArtifactRelabel,
    service: DocumentServiceDep,
) -> ArtifactResponse:
    """Change an artifact's mutable logical label."""
    return service.relabel(
        artifact_id,
        body.workspace_id,
        body.logical_type,
        correlation_id=correlation_id,
    )


@router.delete(
    f"{API_PREFIX}/artifacts/{{artifact_id}}",
    response_model=ArtifactResponse,
    tags=["documents"],
)
def archive_artifact(
    correlation_id: CorrelationId,
    artifact_id: str,
    workspace_id: WorkspaceId,
    service: DocumentServiceDep,
) -> ArtifactResponse:
    """Archive an artifact without deleting its bytes."""
    return service.archive(
        artifact_id,
        workspace_id,
        correlation_id=correlation_id,
    )


@router.get(
    f"{API_PREFIX}/tailoring-runs/{{run_id}}/review",
    response_model=TailoringReviewResponse,
    tags=["documents"],
)
def tailoring_review(
    run_id: str,
    workspace_id: WorkspaceId,
    service: ApprovalServiceDep,
) -> TailoringReviewResponse:
    """List review proposals for a tailoring run."""
    return service.review(run_id, workspace_id)


@router.post(
    f"{API_PREFIX}/review-proposals/{{proposal_id}}/decisions",
    response_model=ReviewDecisionResponse,
    tags=["documents"],
)
def decide_review_proposal(
    correlation_id: CorrelationId,
    proposal_id: str,
    body: ApprovalDecisionBody,
    service: ApprovalServiceDep,
) -> ReviewDecisionResponse:
    """Record a human decision on a review proposal."""
    return service.decide(
        proposal_id,
        body.workspace_id,
        proposal_digest=body.proposal_digest,
        decision=body.decision,
        replacement_text=body.replacement_text,
        replacement_source_ids=body.replacement_source_ids,
        correlation_id=correlation_id,
    )
