"""Read one stored posting body, and compare two of them.

A posting is kept as an append-only series of observations, so "what does this
job say" is always a question about one observation and "what changed" is a
question about two. Neither writes.
"""

from __future__ import annotations

from difflib import unified_diff
from typing import TYPE_CHECKING

from sqlalchemy import select

from tailorcv.application.documents.rendered_text import rendered_text
from tailorcv.application.failures import NotFoundError, ValidationError
from tailorcv.application.job_search._support import _observation, _readable_artifact
from tailorcv.application.job_search.models import (
    PostingDiffResponse,
    PostingObservationBodyResponse,
)
from tailorcv.infrastructure.persistence.models import JobPosting, PostingObservation, PostingUrl

if TYPE_CHECKING:
    from tailorcv.application.documents.evidence_artifacts import EvidenceArtifactService
    from tailorcv.infrastructure.persistence.database import Database

# The facts a refresh re-reads, so a diff can say what moved without diffing the
# whole body to find out.
COMPARED_FACTS = (
    "posting_status",
    "location",
    "residence_rule",
    "employment_type",
    "payroll_arrangement",
)


def posting_observation(
    database: Database,
    artifacts: EvidenceArtifactService,
    observation_id: str,
    workspace_id: str,
) -> PostingObservationBodyResponse:
    """Read one complete posting body through workspace and hash boundaries."""
    with database.session() as session:
        observation = session.scalar(
            select(PostingObservation)
            .join(JobPosting, JobPosting.id == PostingObservation.job_posting_id)
            .where(
                PostingObservation.id == observation_id,
                JobPosting.workspace_id == workspace_id,
                JobPosting.archived_at.is_(None),
            )
        )
        if observation is None:
            raise NotFoundError(f"Posting observation {observation_id!r} does not exist.")
        if observation.body_artifact_id is None:
            raise NotFoundError("This observation has no recoverable posting body.")
        artifact, body = _readable_artifact(
            artifacts,
            session,
            observation.body_artifact_id,
            workspace_id,
            missing="The posting body artifact is unavailable in this workspace.",
            unreadable="The posting body is not readable UTF-8 text.",
        )
        source_url = session.scalar(
            select(PostingUrl.url)
            .where(PostingUrl.job_posting_id == observation.job_posting_id)
            .order_by(PostingUrl.is_primary.desc(), PostingUrl.created_at, PostingUrl.id)
            .limit(1)
        )
        discovery_url = session.scalar(
            select(PostingUrl.url)
            .where(
                PostingUrl.job_posting_id == observation.job_posting_id,
                PostingUrl.url_type == "discovery",
            )
            .order_by(PostingUrl.created_at, PostingUrl.id)
            .limit(1)
        )
        return PostingObservationBodyResponse.model_validate(
            {
                **_observation(observation),
                "job_posting_id": observation.job_posting_id,
                "workspace_id": workspace_id,
                "source_url": source_url or "",
                "discovery_url": discovery_url,
                "logical_type": artifact.logical_type,
                "mime_type": artifact.mime_type,
                "sha256": artifact.sha256,
                "body": body,
                "rendered_text": rendered_text(body, artifact.mime_type),
            }
        )


def posting_diff(
    database: Database,
    artifacts: EvidenceArtifactService,
    job_posting_id: str,
    workspace_id: str,
    from_observation_id: str,
    to_observation_id: str,
) -> PostingDiffResponse:
    """Compare two verified observations of the same workspace-scoped posting."""
    before = posting_observation(database, artifacts, from_observation_id, workspace_id)
    after = posting_observation(database, artifacts, to_observation_id, workspace_id)
    if before.job_posting_id != job_posting_id or after.job_posting_id != job_posting_id:
        raise ValidationError("Both observations must belong to the requested job.")
    changed = [
        {"field": field, "before": getattr(before, field), "after": getattr(after, field)}
        for field in COMPARED_FACTS
        if getattr(before, field) != getattr(after, field)
    ]
    return PostingDiffResponse.model_validate(
        {
            "job_posting_id": job_posting_id,
            "from_observation_id": from_observation_id,
            "to_observation_id": to_observation_id,
            "changed_facts": changed,
            "body_diff": "".join(
                unified_diff(
                    before.body.splitlines(keepends=True),
                    after.body.splitlines(keepends=True),
                    fromfile=from_observation_id,
                    tofile=to_observation_id,
                )
            ),
        }
    )
