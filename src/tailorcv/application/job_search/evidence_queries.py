"""Read the stored source behind a score, and the report written from it.

Both answer the same question from different ends: show me the exact bytes this
evaluation relied on. Neither writes, and both refuse anything the workspace does
not own, because an artifact identifier is not proof of a right to read it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from tailorcv.application.documents.rendered_text import rendered_text
from tailorcv.application.failures import NotFoundError
from tailorcv.application.job_search._support import _readable_artifact, _requirements
from tailorcv.application.job_search.models import EvidenceSourceResponse, FitReportResponse
from tailorcv.infrastructure.persistence.models import JobEvaluation, JobPosting, MatchEvidence

if TYPE_CHECKING:
    from tailorcv.application.documents.evidence_artifacts import EvidenceArtifactService
    from tailorcv.infrastructure.persistence.database import Database


def evidence_source(
    database: Database,
    artifacts: EvidenceArtifactService,
    evidence_id: str,
    workspace_id: str,
) -> EvidenceSourceResponse:
    """Read the verified source document behind one workspace-scoped evidence row."""
    with database.session() as session:
        evidence = session.scalar(
            select(MatchEvidence)
            .join(JobEvaluation, JobEvaluation.id == MatchEvidence.evaluation_id)
            .join(JobPosting, JobPosting.id == JobEvaluation.job_posting_id)
            .where(
                MatchEvidence.id == evidence_id,
                JobPosting.workspace_id == workspace_id,
                JobPosting.archived_at.is_(None),
            )
        )
        if evidence is None or evidence.artifact_id is None:
            raise NotFoundError(f"Evidence {evidence_id!r} has no recoverable source.")
        artifact, body = _readable_artifact(
            artifacts,
            session,
            evidence.artifact_id,
            workspace_id,
            missing="The evidence artifact is unavailable in this workspace.",
            unreadable="This evidence source is not readable UTF-8 text.",
        )
        return EvidenceSourceResponse.model_validate(
            {
                "evidence_id": evidence.id,
                "artifact_id": artifact.id,
                "logical_type": artifact.logical_type,
                "mime_type": artifact.mime_type,
                "sha256": artifact.sha256,
                "source_kind": evidence.source_kind,
                "locator": evidence.locator,
                "char_start": evidence.char_start,
                "char_end": evidence.char_end,
                "body": body,
                "rendered_text": rendered_text(body, artifact.mime_type),
            }
        )


def fit_report(
    database: Database,
    artifacts: EvidenceArtifactService,
    job_posting_id: str,
    workspace_id: str,
) -> FitReportResponse:
    """Return the verified, pre-generated Markdown report for one evaluation.

    The stored Markdown and its complete structured requirement list are served
    together.
    """
    with database.session() as session:
        evaluation = session.scalar(
            select(JobEvaluation)
            .join(JobPosting, JobPosting.id == JobEvaluation.job_posting_id)
            .where(
                JobEvaluation.job_posting_id == job_posting_id,
                JobPosting.workspace_id == workspace_id,
                JobPosting.archived_at.is_(None),
            )
            .order_by(JobEvaluation.version_number.desc(), JobEvaluation.id.desc())
            .limit(1)
        )
        if evaluation is None or evaluation.fit_report_artifact_id is None:
            raise NotFoundError("This evaluation has no recoverable fit report.")
        artifact, markdown = _readable_artifact(
            artifacts,
            session,
            evaluation.fit_report_artifact_id,
            workspace_id,
            missing="The fit report artifact is unavailable in this workspace.",
            unreadable="The fit report is not readable UTF-8 Markdown.",
        )
        payload = evaluation.fit_report_payload or {}
        requirements = _requirements(payload.get("requirements"))
        return FitReportResponse(
            job_posting_id=job_posting_id,
            evaluation_id=evaluation.id,
            evaluation_version=evaluation.version_number,
            artifact_id=artifact.id,
            sha256=artifact.sha256,
            markdown=markdown,
            requirements=requirements,
        )
