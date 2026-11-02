"""Create document versions and change mutable document metadata."""

from __future__ import annotations

import mimetypes
from typing import Any, Literal

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from tailorcv.application.documents.approvals import _record_overrides
from tailorcv.application.documents.artifacts import (
    EDITABLE_TYPES,
    GENERATED_TYPES,
    MAX_UPLOAD_BYTES,
    _artifact_entity,
    _artifact_record,
    _audit_artifact,
    _broken_citations,
    _decode,
    _document_artifact,
    _safe_filename,
)
from tailorcv.application.documents.evidence_artifacts import artifact_suffix
from tailorcv.application.documents.models import ArtifactResponse
from tailorcv.application.documents.promotion import DocumentArtifactPromotion
from tailorcv.application.documents.queries import DocumentQueries
from tailorcv.application.failures import ConflictError, ValidationError
from tailorcv.application.workspaces.records.access import require_workspace
from tailorcv.domain import ArtifactAuthorType, ArtifactLogicalType, new_id, utc_now
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import Artifact


class DocumentVersionCommands:
    """Own document mutations and their database transaction boundaries."""

    def __init__(
        self,
        database: Database,
        promotion: DocumentArtifactPromotion,
        queries: DocumentQueries,
    ) -> None:
        """Configure document commands with storage promotion and verified reads."""
        self.database = database
        self.promotion = promotion
        self.queries = queries

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
        """Store a user-supplied file under the private root and index its exact hash."""
        if not content:
            raise ValidationError("An uploaded artifact cannot be empty.")
        if len(content) > MAX_UPLOAD_BYTES:
            raise ValidationError(f"Uploaded artifacts are limited to {MAX_UPLOAD_BYTES} bytes.")
        safe_name = _safe_filename(filename)
        supplied_type = mime_type.strip().lower()
        if supplied_type == "text/html":
            supplied_type = "application/octet-stream"
        media_type = (
            supplied_type or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
        )
        if submission_role is not None and (
            entity_type != "application" or logical_type != "user.attachment"
        ):
            raise ValidationError("Submitted files must be attachments to an application.")
        family = new_id()
        with self.database.session() as session:
            require_workspace(session, workspace_id)
            _artifact_entity(session, workspace_id, entity_type, entity_id)
            artifact = self.promotion.store(
                session,
                workspace_id=workspace_id,
                logical_type=logical_type,
                mime_type=media_type,
                content=content,
                relative_path=f"{workspace_id}/uploads/{family}/v0001-{safe_name}",
                related_entity_type=entity_type,
                related_entity_id=entity_id,
                tailoring_run_id=None,
                family_id=family,
                version=1,
                supersedes=None,
                author_type="human",
                provenance={
                    "source": "user_upload",
                    "filename": safe_name,
                    **({"submission_role": submission_role} if submission_role else {}),
                },
                correlation_id=correlation_id,
            )
            _audit_artifact(session, artifact, "artifact.uploaded", correlation_id)
            return _artifact_record(artifact)

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
        """Store a generated version without replacing any earlier bytes."""
        query = select(Artifact).where(
            Artifact.workspace_id == workspace_id,
            Artifact.related_entity_type == "job_posting",
            Artifact.related_entity_id == job_posting_id,
            Artifact.logical_type == logical_type,
            Artifact.artifact_family_id.is_not(None),
        )
        source_id = provenance.get("derived_from_artifact_id")
        if source_id is not None:
            query = query.where(_derived_from_family(session, str(source_id)))
        latest = session.scalar(
            query.order_by(Artifact.version_number.desc(), Artifact.created_at.desc())
        )
        family, version = _next_version(session, latest) if latest is not None else (new_id(), 1)
        return self.promotion.store(
            session,
            workspace_id=workspace_id,
            logical_type=logical_type,
            mime_type=mime_type,
            content=content,
            relative_path=(
                f"{workspace_id}/documents/{family}/"
                f"v{version:04d}{artifact_suffix(logical_type, mime_type)}"
            ),
            related_entity_type="job_posting",
            related_entity_id=job_posting_id,
            tailoring_run_id=tailoring_run_id,
            family_id=family,
            version=version,
            supersedes=latest.id if latest is not None else None,
            author_type=author_type,
            provenance=provenance,
            correlation_id=correlation_id,
        )

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
        """Save a new version and record every citation break as an approval."""
        if author_type not in {"human", "model"}:
            raise ValidationError("Document versions must be attributed to human or model.")
        encoded = content.encode("utf-8")
        if not encoded:
            raise ValidationError("A document version cannot be empty.")
        with self.database.session() as session:
            base = _document_artifact(session, artifact_id, workspace_id)
            if base.logical_type in GENERATED_TYPES:
                raise ValidationError(
                    f"{base.logical_type} is a record of a run and is not rewritten. "
                    "Re-run the work instead."
                )
            if base.mime_type not in EDITABLE_TYPES:
                raise ValidationError("Only Markdown and plain-text artifacts are editable.")
            workspace = require_workspace(session, workspace_id)
            original = _decode(self.promotion.evidence_store.read(base, workspace), base.id)
            broken = _broken_citations(base, original, content)
            if broken and not override_reason.strip():
                raise ConflictError(
                    "This edit changes evidence-backed lines. Supply an override reason to save it."
                )
            if broken and base.tailoring_run_id is None:
                raise ConflictError(
                    "Citation-breaking edits require the originating tailoring run's review record."
                )
            family, version = _next_version(session, base)
            provenance = dict(base.provenance or {})
            provenance.pop("submission_role", None)
            provenance.update(
                {
                    "source": "editor_save",
                    "base_artifact_id": base.id,
                    "citation_overrides": len(broken),
                }
            )
            saved = self.promotion.store(
                session,
                workspace_id=workspace_id,
                logical_type=base.logical_type,
                mime_type=base.mime_type,
                content=encoded,
                relative_path=(
                    f"{workspace_id}/documents/{family}/"
                    f"v{version:04d}{artifact_suffix(base.logical_type, base.mime_type)}"
                ),
                related_entity_type=base.related_entity_type,
                related_entity_id=base.related_entity_id,
                tailoring_run_id=base.tailoring_run_id,
                family_id=family,
                version=version,
                supersedes=base.id,
                author_type=author_type,
                provenance=provenance,
                correlation_id=correlation_id,
            )
            if broken:
                _record_overrides(
                    session,
                    base=base,
                    saved=saved,
                    broken=broken,
                    reason=override_reason.strip(),
                    correlation_id=correlation_id,
                )
            _audit_artifact(session, saved, "artifact.version_saved", correlation_id)
            return _artifact_record(saved)

    def revert(
        self,
        *,
        current_artifact_id: str,
        target_artifact_id: str,
        workspace_id: str,
        correlation_id: str | None = None,
    ) -> ArtifactResponse:
        """Append the selected stored bytes as a new human version."""
        target = self.queries.read(target_artifact_id, workspace_id)
        current = self.queries.get(current_artifact_id, workspace_id)
        if current.artifact_family_id != target.artifact.artifact_family_id:
            raise ValidationError("A revert target must belong to the same document family.")
        return self.save_version(
            artifact_id=current_artifact_id,
            workspace_id=workspace_id,
            content=_decode(target.content, target.artifact.id),
            author_type="human",
            override_reason=(
                f"Reverted to artifact {target.artifact.id}; "
                "citation changes retain that version's text."
            ),
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
        """Change user-facing metadata while leaving immutable bytes untouched."""
        with self.database.session() as session:
            artifact = _document_artifact(session, artifact_id, workspace_id)
            artifact.logical_type = logical_type
            _audit_artifact(session, artifact, "artifact.relabeled", correlation_id)
            return _artifact_record(artifact)

    def archive(
        self,
        artifact_id: str,
        workspace_id: str,
        *,
        correlation_id: str | None = None,
    ) -> ArtifactResponse:
        """Soft archive an artifact while retaining its immutable history."""
        with self.database.session() as session:
            artifact = _document_artifact(session, artifact_id, workspace_id)
            artifact.archived_at = utc_now()
            _audit_artifact(session, artifact, "artifact.archived", correlation_id)
            return _artifact_record(artifact)


def _derived_from_family(session: Session, source_id: str) -> ColumnElement[bool]:
    """Match files derived from any version of the source artifact's document family."""
    derived_from: ColumnElement[str] = Artifact.provenance["derived_from_artifact_id"].as_string()
    source = session.get(Artifact, source_id)
    if source is None or source.artifact_family_id is None:
        return derived_from == source_id
    return derived_from.in_(
        select(Artifact.id).where(
            Artifact.workspace_id == source.workspace_id,
            Artifact.artifact_family_id == source.artifact_family_id,
        )
    )


def _next_version(session: Session, base: Artifact) -> tuple[str, int]:
    """Return the next immutable version in one artifact family."""
    family = base.artifact_family_id or base.id
    highest = session.scalar(
        select(func.max(Artifact.version_number)).where(
            Artifact.workspace_id == base.workspace_id,
            Artifact.artifact_family_id == family,
        )
    )
    return family, int(highest or 0) + 1
