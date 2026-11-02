"""The one write that changes what a tracked posting is called.

A rename leaves an append-only correction event carrying the old and new
title, so the person can see what the ledger called the posting before.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from tailorcv.domain.identity import normalize_text
from tailorcv.infrastructure.persistence.models import AuditEvent, JobPosting

# SQLite does not enforce the column's declared width, so the write does.
_TITLE_WIDTH = JobPosting.title.type.length or 500


def rename_posting(
    session: Session, job: JobPosting, title: str, *, audit: dict[str, Any], reason: str
) -> bool:
    """Set the posting's title and record it; a blank or unchanged title writes nothing."""
    wanted = title.strip()[:_TITLE_WIDTH]
    if not wanted or wanted == job.title:
        return False
    old = job.title
    job.title = wanted
    job.normalized_title = normalize_text(wanted)[:_TITLE_WIDTH]
    session.add(
        AuditEvent(
            workspace_id=job.workspace_id,
            action="posting.title_corrected",
            entity_type="job_posting",
            entity_id=job.id,
            details={"old": old, "new": wanted, "reason": reason},
            **audit,
        )
    )
    return True
