"""Document facade and shared artifact rules."""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from tailorcv.application.documents.models import (
    ArtifactDiffResponse,
    ArtifactResponse,
    CitationCheckResponse,
)
from tailorcv.application.failures import NotFoundError, ValidationError
from tailorcv.application.workspaces.records.access import add_audit_event, require_owned_entity
from tailorcv.domain import ArtifactAuthorType, ArtifactLogicalType
from tailorcv.infrastructure.artifacts.runtime import RuntimeStorage
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    Application,
    Artifact,
    JobEvaluation,
    JobPosting,
    PostingObservation,
    TailoringRun,
)

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
EDITABLE_TYPES = frozenset({"text/markdown", "text/plain"})
GENERATED_TYPES = frozenset(
    {
        "screening.report.markdown",
        "evaluation.fit_report.markdown",
        "batch.report.markdown",
        "batch.record.json",
        "discovery.session.json",
        "tailoring.review.json",
        "tailoring.manifest.json",
        "posting.body.text",
        "master_cv.snapshot.markdown",
    }
)
INLINE_TYPES = frozenset({"application/pdf", "text/markdown", "text/plain"})
# SVG is markup that can carry script, so only raster images render inline.
SCRIPTABLE_IMAGE_TYPES = frozenset({"image/svg+xml"})
ENTITY_MODELS: dict[str, type[Any]] = {
    "job_posting": JobPosting,
    "application": Application,
    "evaluation": JobEvaluation,
}
JOB_SCOPED_ENTITIES: tuple[tuple[str, type[Any]], ...] = (
    ("job_evaluation", JobEvaluation),
    ("tailoring_run", TailoringRun),
    ("posting_observation", PostingObservation),
    ("application", Application),
)


def serves_inline(mime_type: str) -> bool:
    """Return whether a browser may render this media type in place."""
    base_type = mime_type.split(";", 1)[0].strip().lower()
    return base_type in INLINE_TYPES or (
        base_type.startswith("image/") and base_type not in SCRIPTABLE_IMAGE_TYPES
    )


def unified(before: str, after: str, from_label: str, to_label: str) -> str:
    """Return a deterministic unified diff between two versions of one text."""
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=from_label,
            tofile=to_label,
        )
    )


@dataclass(frozen=True)
class ArtifactContent:
    """Verified bytes together with the durable metadata that authorized the read."""

    artifact: Artifact
    content: bytes


class DocumentService:
    """Expose document commands, queries, and promotion through one stable port."""

    def __init__(self, database: Database, storage: RuntimeStorage) -> None:
        """Compose collaborators around one resolved local artifact root."""
        from tailorcv.application.documents.commands import DocumentVersionCommands
        from tailorcv.application.documents.promotion import DocumentArtifactPromotion
        from tailorcv.application.documents.queries import DocumentQueries

        self.storage = storage
        self.root = storage.artifact_root
        self.promotion = DocumentArtifactPromotion(storage)
        self.evidence_store = self.promotion.evidence_store
        self.queries = DocumentQueries(database, self.promotion)
        self.commands = DocumentVersionCommands(database, self.promotion, self.queries)

    def list_artifacts(
        self,
        workspace_id: str,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        include_archived: bool = False,
        include_related: bool = False,
    ) -> list[ArtifactResponse]:
        """List workspace artifacts and optional related records."""
        return self.queries.list_artifacts(
            workspace_id,
            entity_type=entity_type,
            entity_id=entity_id,
            include_archived=include_archived,
            include_related=include_related,
        )

    def get(self, artifact_id: str, workspace_id: str) -> ArtifactResponse:
        """Return one artifact metadata record."""
        return self.queries.get(artifact_id, workspace_id)

    def read(self, artifact_id: str, workspace_id: str) -> ArtifactContent:
        """Read verified bytes for one workspace artifact."""
        return self.queries.read(artifact_id, workspace_id)

    def upload(
        self,
        *,
        workspace_id: str,
        entity_type: str,
        entity_id: str,
        logical_type: ArtifactLogicalType,
        filename: str,
        mime_type: str,
        content: bytes,
        submission_role: Literal["resume", "cover_letter", "other"] | None = None,
        correlation_id: str | None = None,
    ) -> ArtifactResponse:
        """Store one user-supplied artifact."""
        return self.commands.upload(
            workspace_id=workspace_id,
            entity_type=entity_type,
            entity_id=entity_id,
            logical_type=logical_type,
            filename=filename,
            mime_type=mime_type,
            content=content,
            submission_role=submission_role,
            correlation_id=correlation_id,
        )

    def store_generated(
        self,
        session: Session,
        *,
        workspace_id: str,
        job_posting_id: str,
        tailoring_run_id: str | None,
        logical_type: ArtifactLogicalType,
        mime_type: str,
        content: bytes,
        provenance: dict[str, Any],
        author_type: ArtifactAuthorType = "model",
        correlation_id: str | None = None,
    ) -> Artifact:
        """Store one generated immutable artifact version in a caller transaction."""
        return self.commands.store_generated(
            session,
            workspace_id=workspace_id,
            job_posting_id=job_posting_id,
            tailoring_run_id=tailoring_run_id,
            logical_type=logical_type,
            mime_type=mime_type,
            content=content,
            provenance=provenance,
            author_type=author_type,
            correlation_id=correlation_id,
        )

    def versions(self, artifact_id: str, workspace_id: str) -> list[ArtifactResponse]:
        """Return immutable history for one document family."""
        return self.queries.versions(artifact_id, workspace_id)

    def diff(self, artifact_id: str, other_id: str, workspace_id: str) -> ArtifactDiffResponse:
        """Return a deterministic text diff between two artifacts."""
        return self.queries.diff(artifact_id, other_id, workspace_id)

    def citation_check(
        self, artifact_id: str, workspace_id: str, content: str
    ) -> CitationCheckResponse:
        """Report evidence-backed lines changed by an edit."""
        return self.queries.citation_check(artifact_id, workspace_id, content)

    def save_version(
        self,
        *,
        artifact_id: str,
        workspace_id: str,
        content: str,
        author_type: ArtifactAuthorType,
        override_reason: str = "",
        correlation_id: str | None = None,
    ) -> ArtifactResponse:
        """Append an editor-authored document version."""
        return self.commands.save_version(
            artifact_id=artifact_id,
            workspace_id=workspace_id,
            content=content,
            author_type=author_type,
            override_reason=override_reason,
            correlation_id=correlation_id,
        )

    def revert(
        self,
        *,
        current_artifact_id: str,
        target_artifact_id: str,
        workspace_id: str,
        correlation_id: str | None = None,
    ) -> ArtifactResponse:
        """Append a selected stored version as the newest version."""
        return self.commands.revert(
            current_artifact_id=current_artifact_id,
            target_artifact_id=target_artifact_id,
            workspace_id=workspace_id,
            correlation_id=correlation_id,
        )

    def relabel(
        self,
        artifact_id: str,
        workspace_id: str,
        logical_type: ArtifactLogicalType,
        *,
        correlation_id: str | None = None,
    ) -> ArtifactResponse:
        """Change an artifact's logical type."""
        return self.commands.relabel(
            artifact_id,
            workspace_id,
            logical_type,
            correlation_id=correlation_id,
        )

    def archive(
        self,
        artifact_id: str,
        workspace_id: str,
        *,
        correlation_id: str | None = None,
    ) -> ArtifactResponse:
        """Soft archive one artifact."""
        return self.commands.archive(
            artifact_id,
            workspace_id,
            correlation_id=correlation_id,
        )


def _artifact_entity(session: Session, workspace_id: str, entity_type: str, entity_id: str) -> Any:
    if entity_type not in ENTITY_MODELS:
        raise ValidationError(f"Artifacts cannot be attached to {entity_type!r}.")
    return require_owned_entity(session, workspace_id, entity_type, entity_id)


def _audit_artifact(
    session: Session, artifact: Artifact, action: str, correlation_id: str | None
) -> None:
    # The artifact carries the origin of the run that produced it, so its audit
    # trail records the same one rather than the column default.
    add_audit_event(
        session,
        workspace_id=artifact.workspace_id,
        action=action,
        entity_type="artifact",
        entity_id=artifact.id,
        details={"sha256": artifact.sha256, "logical_type": artifact.logical_type},
        origin=artifact.origin,
        correlation_id=correlation_id,
    )


def _document_artifact(
    session: Session,
    artifact_id: str,
    workspace_id: str,
    *,
    include_archived: bool = False,
) -> Artifact:
    artifact = session.get(Artifact, artifact_id)
    if (
        artifact is None
        or artifact.workspace_id != workspace_id
        or (artifact.archived_at is not None and not include_archived)
    ):
        raise NotFoundError(f"Artifact {artifact_id!r} does not exist in this workspace.")
    _assert_artifact_parent_active(session, artifact, workspace_id)
    return artifact


def _job_scope_id(entity_type: str, entity: Any) -> str | None:
    if entity_type == "job_posting":
        return str(entity.id)
    if entity_type == "application":
        return str(entity.job_posting_id)
    return None


def _job_scope_clause(job_posting_id: str) -> Any:
    related = [
        and_(
            Artifact.related_entity_type == entity_type,
            Artifact.related_entity_id.in_(
                select(model.id).where(model.job_posting_id == job_posting_id)
            ),
        )
        for entity_type, model in JOB_SCOPED_ENTITIES
    ]
    return or_(
        and_(
            Artifact.related_entity_type == "job_posting",
            Artifact.related_entity_id == job_posting_id,
        ),
        *related,
    )


def _assert_artifact_parent_active(session: Session, artifact: Artifact, workspace_id: str) -> None:
    if artifact.related_entity_type in ENTITY_MODELS and artifact.related_entity_id is not None:
        _artifact_entity(
            session,
            workspace_id,
            artifact.related_entity_type,
            artifact.related_entity_id,
        )


def _artifact_record(artifact: Artifact) -> ArtifactResponse:
    """Return safe artifact metadata without its filesystem URI."""
    provenance = dict(artifact.provenance or {})
    payload: dict[str, Any] = {
        "id": artifact.id,
        "workspace_id": artifact.workspace_id,
        "logical_type": artifact.logical_type,
        "mime_type": artifact.mime_type,
        "byte_size": artifact.byte_size,
        "sha256": artifact.sha256,
        "integrity_status": artifact.integrity_status,
        "last_verified_at": artifact.last_verified_at,
        "created_at": artifact.created_at,
        "created_by": artifact.created_by,
        "provenance": provenance,
        "related_entity_type": artifact.related_entity_type,
        "related_entity_id": artifact.related_entity_id,
        "tailoring_run_id": artifact.tailoring_run_id,
        "artifact_family_id": artifact.artifact_family_id,
        "version_number": artifact.version_number,
        "supersedes_artifact_id": artifact.supersedes_artifact_id,
        "author_type": artifact.author_type,
        "archived_at": artifact.archived_at,
        "inline_viewable": serves_inline(artifact.mime_type),
        "editable": artifact.logical_type not in GENERATED_TYPES
        and artifact.mime_type in EDITABLE_TYPES,
    }
    try:
        return ArtifactResponse.model_validate(payload)
    except PydanticValidationError as exc:
        raise ValidationError(
            f"Artifact {artifact.id!r} has invalid persisted metadata: "
            f"{_invalid_values(payload, exc)}."
        ) from exc


def _invalid_values(payload: dict[str, Any], exc: PydanticValidationError) -> str:
    names = sorted({str(error["loc"][0]) for error in exc.errors() if error["loc"]})
    stored = [f"{name} {payload[name]!r}" for name in names if name in payload]
    return ", ".join(stored) or "a value"


def _artifact_records(artifacts: Sequence[Artifact]) -> list[ArtifactResponse]:
    """Return every artifact under the current persisted contract."""
    return [_artifact_record(item) for item in artifacts]


def _broken_citation(raw: Any, before: list[str], after: list[str]) -> dict[str, Any] | None:
    """Report how one cited line broke, or None when it is intact or uncitable."""
    if not isinstance(raw, dict):
        return None
    line_number = raw.get("line_number")
    if not isinstance(line_number, int) or line_number < 1:
        return None
    expected = str(raw.get("text", ""))
    source_ids = raw.get("source_ids", [])
    if not isinstance(source_ids, list) or not source_ids:
        return None
    index = line_number - 1
    actual_before = before[index] if index < len(before) else expected
    replacement = after[index] if index < len(after) else ""
    if actual_before == replacement and expected == actual_before:
        return None
    return {
        "line_number": line_number,
        "text": expected or actual_before,
        "replacement": replacement,
        "source_ids": [str(item) for item in source_ids],
        "proposal_id": raw.get("proposal_id"),
    }


def _broken_citations(artifact: Artifact, original: str, edited: str) -> list[dict[str, Any]]:
    citations = artifact.provenance.get("line_citations", []) if artifact.provenance else []
    if not isinstance(citations, list):
        return []
    before = original.splitlines()
    after = edited.splitlines()
    verdicts = (_broken_citation(raw, before, after) for raw in citations)
    return [verdict for verdict in verdicts if verdict is not None]


def _decode(content: bytes, artifact_id: str) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"Artifact {artifact_id!r} is not valid UTF-8 text.") from exc


def _safe_filename(value: str) -> str:
    name = Path(value).name.strip().replace("\x00", "")
    if not name or name in {".", ".."}:
        return "upload.bin"
    return "".join(
        character if character.isalnum() or character in "._-" else "_" for character in name
    )[:180]
