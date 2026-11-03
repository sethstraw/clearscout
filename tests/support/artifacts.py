"""Build the artifact store a job-search operation writes evidence through."""

from __future__ import annotations

from pathlib import Path

from clearscout.application.documents.evidence_artifacts import EvidenceArtifactService
from clearscout.infrastructure.artifacts.runtime import RuntimeStorage
from clearscout.infrastructure.artifacts.store import LocalArtifactStore, file_uri_path
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import Artifact


def runtime_storage(database: Database, artifact_root: Path | None = None) -> RuntimeStorage:
    """Resolve synthetic storage explicitly at the test setup boundary."""
    return RuntimeStorage.resolve(database.url, artifact_root=artifact_root)


def artifact_service(
    database: Database, artifact_root: Path | None = None
) -> EvidenceArtifactService:
    """Root the store where the test wants its bytes, beside the test database."""
    return EvidenceArtifactService(runtime_storage(database, artifact_root))


def strand_promotion(database: Database, artifact_root: Path, artifact_id: str) -> Path:
    """Put a promoted artifact back where a crash between stage and promotion leaves it.

    Returns the target path, which no longer exists.
    """
    store = LocalArtifactStore(artifact_root)
    with database.session() as session:
        artifact = session.get(Artifact, artifact_id)
        assert artifact is not None
        target = file_uri_path(artifact.storage_uri)
        assert target is not None
        relative = target.relative_to(store.root).as_posix()
        target.replace(store.staging_path(relative, artifact.sha256))
        artifact.integrity_status = "staged"
        artifact.last_verified_at = None
    return target


__all__ = ["artifact_service", "runtime_storage", "strand_promotion"]
