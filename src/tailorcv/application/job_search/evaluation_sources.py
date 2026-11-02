"""Store the two source texts an evaluation's evidence spans address.

The posting body and the master CV are persisted before any evidence row cites
them, so a stored span always addresses bytes that exist. Both write through the
artifact store inside the transaction the caller opened.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from tailorcv.application.contracts import EvaluationCommit
from tailorcv.application.documents.evidence_artifacts import (
    EvidenceArtifactService,
    artifact_suffix,
)
from tailorcv.application.failures import NotFoundError
from tailorcv.application.job_search.posting_bodies import (
    posting_body_text,
    posting_mime_type,
    posting_source_bytes,
)
from tailorcv.application.master_cv import register_master_cv
from tailorcv.application.workspaces.records.access import audit_columns
from tailorcv.infrastructure.persistence.models import Artifact, MasterCvVersion, Workspace


def _posting_artifacts(
    artifacts: EvidenceArtifactService,
    session: Session,
    proposal: EvaluationCommit,
    *,
    observation_id: str,
    actor: str,
    origin: str,
    correlation_id: str | None,
) -> Artifact:
    """Store original posting bytes plus readable extracted text when needed.

    A PDF or an image is kept as it arrived and again as the text an evidence
    span can address. Text and HTML are stored once when the readable body is
    byte-identical to what arrived.
    """
    value = proposal.posting_body
    source = posting_source_bytes(value)
    body_text = posting_body_text(value, source)
    mime_type = posting_mime_type(value)
    audit = audit_columns(actor, origin, correlation_id)
    body_is_source = body_text.encode("utf-8") == source
    source_artifact: Artifact | None = None
    if not body_is_source:
        source_artifact = artifacts.store(
            session,
            workspace_id=proposal.workspace_id,
            logical_type=value.logical_type,
            content=source,
            relative_path=(
                f"{proposal.workspace_id}/postings/{observation_id}/source"
                f"{artifact_suffix(value.logical_type, mime_type)}"
            ),
            mime_type=mime_type,
            related_entity_type="posting_observation",
            related_entity_id=observation_id,
            provenance={"source": "evaluation_commit", "role": "original"},
            audit=audit,
        )
    logical_type = value.logical_type if body_is_source else "posting.body.text"
    body_bytes = source if body_is_source else body_text.encode("utf-8")
    return artifacts.store(
        session,
        workspace_id=proposal.workspace_id,
        logical_type=logical_type,
        content=body_bytes,
        relative_path=(
            f"{proposal.workspace_id}/postings/{observation_id}/body{artifact_suffix(logical_type)}"
        ),
        mime_type=mime_type if body_is_source else "text/plain",
        related_entity_type="posting_observation",
        related_entity_id=observation_id,
        provenance={
            "source": "evaluation_commit",
            "role": "readable_body",
            "source_artifact_id": source_artifact.id if source_artifact else None,
        },
        audit=audit,
    )


def _candidate_record(
    artifacts: EvidenceArtifactService,
    session: Session,
    proposal: EvaluationCommit,
    *,
    actor: str,
    origin: str,
    correlation_id: str | None,
) -> tuple[MasterCvVersion, Artifact]:
    """Persist or reuse the exact master-CV text that evidence spans address."""
    content = proposal.candidate_record.content.encode("utf-8")
    workspace = session.get(Workspace, proposal.workspace_id)
    if workspace is None:
        raise NotFoundError(f"Workspace {proposal.workspace_id!r} does not exist.")
    return register_master_cv(
        session,
        artifacts,
        workspace=workspace,
        content=content,
        inventory=None,
        original_source_uri=proposal.candidate_record.source_uri,
        actor=actor,
        origin=origin,
        correlation_id=correlation_id,
        effective_at=proposal.candidate_record.effective_at,
    )
