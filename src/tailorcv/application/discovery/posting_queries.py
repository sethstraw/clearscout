"""Small shared queries against a posting's observations and evaluations."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from tailorcv.infrastructure.persistence.models import EligibilityRecord, PostingObservation


def latest_observation_id(session: Session, job_posting_id: str) -> str | None:
    """Return the id of the most recent observation, or None when none exist."""
    return session.scalar(
        select(PostingObservation.id)
        .where(PostingObservation.job_posting_id == job_posting_id)
        .order_by(PostingObservation.observed_at.desc(), PostingObservation.id.desc())
        .limit(1)
    )


def same_body_eligibility(
    session: Session,
    job_posting_id: str,
    content_sha256: str,
    *,
    excluding_observation_id: str | None = None,
) -> list[EligibilityRecord]:
    """Return the eligibility records of the latest observation that read this exact body."""
    source_observation_id = session.scalar(
        select(EligibilityRecord.observation_id)
        .join(PostingObservation, PostingObservation.id == EligibilityRecord.observation_id)
        .where(
            PostingObservation.job_posting_id == job_posting_id,
            PostingObservation.content_sha256 == content_sha256,
            PostingObservation.id != excluding_observation_id,
        )
        .order_by(PostingObservation.observed_at.desc(), PostingObservation.id.desc())
        .limit(1)
    )
    if source_observation_id is None:
        return []
    return list(
        session.scalars(
            select(EligibilityRecord)
            .where(EligibilityRecord.observation_id == source_observation_id)
            .order_by(EligibilityRecord.id)
        )
    )
