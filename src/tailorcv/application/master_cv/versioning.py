"""Select the immutable master-CV version in force for a workspace."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from tailorcv.infrastructure.persistence.models import CandidateProfile, MasterCvVersion


def current_version_id(session: Session, workspace_id: str) -> str | None:
    """Return the newest master-CV version id for one workspace."""
    return session.scalar(
        select(MasterCvVersion.id)
        .join(CandidateProfile, CandidateProfile.id == MasterCvVersion.candidate_profile_id)
        .where(CandidateProfile.workspace_id == workspace_id)
        .order_by(MasterCvVersion.effective_at.desc(), MasterCvVersion.id.desc())
        .limit(1)
    )
