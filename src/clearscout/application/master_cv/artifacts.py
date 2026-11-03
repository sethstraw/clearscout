"""Coordinate immutable master-CV snapshots with their durable artifact records."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from clearscout.application.documents.evidence_artifacts import EvidenceArtifactService
from clearscout.application.failures import NotFoundError
from clearscout.infrastructure.persistence.models import Artifact, Workspace


class MasterCvArtifacts:
    """Own snapshot lookup and integrity-checked reads for master-CV versions."""

    def __init__(self, artifacts: EvidenceArtifactService) -> None:
        """Use the shared artifact service for immutable snapshot bytes."""
        self.artifacts = artifacts

    def content(self, session: Session, workspace_id: str, version_id: str) -> str:
        """Read a version's stored snapshot after proving its workspace ownership."""
        artifact = session.scalar(
            select(Artifact)
            .where(
                Artifact.workspace_id == workspace_id,
                Artifact.related_entity_type == "master_cv_version",
                Artifact.related_entity_id == version_id,
            )
            .order_by(Artifact.created_at.desc(), Artifact.id.desc())
        )
        if artifact is None:
            raise NotFoundError("This version has no stored snapshot to read.")
        workspace = session.get(Workspace, workspace_id)
        if workspace is None:
            raise NotFoundError(f"Workspace {workspace_id!r} does not exist.")
        return self.artifacts.read(artifact, workspace).decode("utf-8")
