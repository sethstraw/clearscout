"""Promote immutable document bytes."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from clearscout.application.documents.evidence_artifacts import EvidenceArtifactService
from clearscout.domain import ArtifactAuthorType, ArtifactLogicalType
from clearscout.infrastructure.artifacts.runtime import RuntimeStorage
from clearscout.infrastructure.persistence.models import Artifact


class DocumentArtifactPromotion:
    """Own storage promotion for document artifact records."""

    def __init__(self, storage: RuntimeStorage) -> None:
        """Configure the artifact store rooted in the resolved runtime storage."""
        self.storage = storage
        self.evidence_store = EvidenceArtifactService(storage)

    def store(
        self,
        session: Session,
        *,
        workspace_id: str,
        logical_type: ArtifactLogicalType,
        mime_type: str,
        content: bytes,
        relative_path: str,
        related_entity_type: str | None,
        related_entity_id: str | None,
        tailoring_run_id: str | None,
        family_id: str,
        version: int,
        supersedes: str | None,
        author_type: ArtifactAuthorType,
        provenance: dict[str, Any],
        correlation_id: str | None,
    ) -> Artifact:
        """Stage bytes in the current transaction and retain their version lineage."""
        artifact = self.evidence_store.store(
            session,
            workspace_id=workspace_id,
            logical_type=logical_type,
            content=content,
            relative_path=relative_path,
            mime_type=mime_type,
            related_entity_type=related_entity_type,
            related_entity_id=related_entity_id,
            provenance=provenance,
            audit={
                "created_by": "local_user",
                "origin": "clearscout",
                "correlation_id": correlation_id,
            },
        )
        artifact.tailoring_run_id = tailoring_run_id
        artifact.artifact_family_id = family_id
        artifact.version_number = version
        artifact.supersedes_artifact_id = supersedes
        artifact.author_type = author_type
        session.flush()
        return artifact
