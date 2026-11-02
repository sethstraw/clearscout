"""Read document metadata, bytes, history, and evidence-aware comparisons."""

from __future__ import annotations

from sqlalchemy import and_, or_, select

from tailorcv.application.documents.artifacts import (
    EDITABLE_TYPES,
    ENTITY_MODELS,
    ArtifactContent,
    _artifact_entity,
    _artifact_record,
    _artifact_records,
    _broken_citations,
    _decode,
    _document_artifact,
    _job_scope_clause,
    _job_scope_id,
    unified,
)
from tailorcv.application.documents.models import (
    ArtifactDiffResponse,
    ArtifactResponse,
    CitationCheckResponse,
)
from tailorcv.application.documents.promotion import DocumentArtifactPromotion
from tailorcv.application.failures import ValidationError
from tailorcv.application.workspaces.records.access import active_parent_clauses, require_workspace
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import Artifact


class DocumentQueries:
    """Own document reads without creating or changing artifact records."""

    def __init__(self, database: Database, promotion: DocumentArtifactPromotion) -> None:
        """Configure read access to persisted artifacts and their verified bytes."""
        self.database = database
        self.promotion = promotion

    def list_artifacts(
        self,
        workspace_id: str,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        include_archived: bool = False,
        include_related: bool = False,
    ) -> list[ArtifactResponse]:
        """List files after proving any requested relationship belongs to the workspace."""
        with self.database.session() as session:
            require_workspace(session, workspace_id)
            if (entity_type is None) != (entity_id is None):
                raise ValidationError(
                    "Artifact entity_type and entity_id must be supplied together."
                )
            scope = None
            if entity_type is not None and entity_id is not None:
                entity = _artifact_entity(session, workspace_id, entity_type, entity_id)
                job_posting_id = _job_scope_id(entity_type, entity) if include_related else None
                scope = (
                    _job_scope_clause(job_posting_id)
                    if job_posting_id is not None
                    else and_(
                        Artifact.related_entity_type == entity_type,
                        Artifact.related_entity_id == entity_id,
                    )
                )
            query = select(Artifact).where(Artifact.workspace_id == workspace_id)
            known = tuple(ENTITY_MODELS)
            query = query.where(
                or_(
                    Artifact.related_entity_type.is_(None),
                    ~Artifact.related_entity_type.in_(known),
                    *active_parent_clauses(
                        Artifact.related_entity_type,
                        Artifact.related_entity_id,
                        workspace_id,
                        known,
                    ),
                )
            )
            if scope is not None:
                query = query.where(scope)
            if not include_archived:
                query = query.where(Artifact.archived_at.is_(None))
            artifacts = session.scalars(
                query.order_by(Artifact.created_at.desc(), Artifact.id.desc())
            ).all()
            return _artifact_records(artifacts)

    def get(self, artifact_id: str, workspace_id: str) -> ArtifactResponse:
        """Return one artifact metadata record after workspace ownership checks."""
        with self.database.session() as session:
            artifact = _document_artifact(session, artifact_id, workspace_id, include_archived=True)
            return _artifact_record(artifact)

    def read(self, artifact_id: str, workspace_id: str) -> ArtifactContent:
        """Read and verify only the bytes named by an indexed workspace artifact."""
        with self.database.session() as session:
            artifact = _document_artifact(session, artifact_id, workspace_id)
            workspace = require_workspace(session, workspace_id)
            content = self.promotion.evidence_store.read(artifact, workspace)
            session.expunge(artifact)
            return ArtifactContent(artifact=artifact, content=content)

    def versions(self, artifact_id: str, workspace_id: str) -> list[ArtifactResponse]:
        """Return one document family's full immutable history."""
        with self.database.session() as session:
            artifact = _document_artifact(session, artifact_id, workspace_id, include_archived=True)
            if artifact.artifact_family_id is None:
                return [_artifact_record(artifact)]
            versions = session.scalars(
                select(Artifact)
                .where(
                    Artifact.workspace_id == workspace_id,
                    Artifact.artifact_family_id == artifact.artifact_family_id,
                )
                .order_by(Artifact.version_number.desc())
            ).all()
            return _artifact_records(versions)

    def diff(self, artifact_id: str, other_id: str, workspace_id: str) -> ArtifactDiffResponse:
        """Return a deterministic text diff between two verified workspace artifacts."""
        first = self.read(artifact_id, workspace_id)
        second = self.read(other_id, workspace_id)
        if (
            first.artifact.mime_type not in EDITABLE_TYPES
            or second.artifact.mime_type not in EDITABLE_TYPES
        ):
            raise ValidationError("Version diff is available only for Markdown and plain text.")
        return ArtifactDiffResponse(
            from_artifact_id=artifact_id,
            to_artifact_id=other_id,
            diff=unified(
                _decode(first.content, first.artifact.id),
                _decode(second.content, second.artifact.id),
                f"v{first.artifact.version_number or 0}",
                f"v{second.artifact.version_number or 0}",
            ),
        )

    def citation_check(
        self, artifact_id: str, workspace_id: str, content: str
    ) -> CitationCheckResponse:
        """Name evidence-backed lines whose exact drafted text an edit changes."""
        current = self.read(artifact_id, workspace_id)
        if current.artifact.mime_type not in EDITABLE_TYPES:
            raise ValidationError("Citation checks are available only for Markdown and plain text.")
        broken = _broken_citations(
            current.artifact, _decode(current.content, current.artifact.id), content
        )
        return CitationCheckResponse(
            artifact_id=artifact_id,
            broken=broken,
            requires_override=bool(broken),
        )
