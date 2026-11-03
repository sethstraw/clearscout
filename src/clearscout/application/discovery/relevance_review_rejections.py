"""Marks for paid relevance reviews that local validation refused to record.

The mark is stored beside the reviews rather than in them, so
``relevance_reviews`` stays what the pipeline accepted. Every change writes an
``audit_events`` entry, so the current state is one row and the history stays
recoverable. Nothing here re-runs a review: spending again is the workspace's
decision.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from clearscout.application.job_search.models import (
    RelevanceReviewRejectionCode,
    RelevanceReviewRejectionResponse,
)
from clearscout.application.workspaces.records.access import audit_columns
from clearscout.domain import utc_now
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import AuditEvent, RelevanceReviewRejection


@dataclass(frozen=True)
class RelevanceReviewItemFailure:
    """One provider answer rejected by deterministic local validation."""

    pending_discovery_id: str
    failure_code: RelevanceReviewRejectionCode
    detail: str


class RelevanceReviewRejectionService:
    """Mark the discoveries a paid review left without a verdict, and clear the mark."""

    def __init__(self, database: Database) -> None:
        """Bind the service to one durable database."""
        self.database = database

    def record(
        self,
        *,
        workspace_id: str,
        task_id: str,
        rejected: Sequence[RelevanceReviewItemFailure],
        accepted_ids: Sequence[str] = (),
    ) -> None:
        """Mark each rejected discovery and clear the mark on each accepted one."""
        if not rejected and not accepted_ids:
            return
        # Worker passes over one discovery overlap, and each mark is written from
        # what the same transaction just read.
        with self.database.exclusive_session() as session:
            for item in rejected:
                self._mark(session, workspace_id, task_id, item)
            for pending_discovery_id in accepted_ids:
                self.clear_in_session(session, workspace_id, pending_discovery_id)

    def _mark(
        self,
        session: Session,
        workspace_id: str,
        task_id: str,
        item: RelevanceReviewItemFailure,
    ) -> None:
        row = self._standing(session, workspace_id, item.pending_discovery_id)
        rejected_at = utc_now()
        if row is None:
            row = RelevanceReviewRejection(
                workspace_id=workspace_id,
                pending_discovery_id=item.pending_discovery_id,
                task_id=task_id,
                failure_code=item.failure_code,
                detail=item.detail,
                rejected_at=rejected_at,
                created_by="worker",
                origin="discovery.relevance_review",
                correlation_id=task_id,
            )
            session.add(row)
        else:
            row.task_id = task_id
            row.correlation_id = task_id
            row.failure_code = item.failure_code
            row.detail = item.detail
            row.rejected_at = rejected_at
        session.flush()
        session.add(
            AuditEvent(
                workspace_id=workspace_id,
                action="relevance_review_rejection.marked",
                entity_type="relevance_review_rejection",
                entity_id=row.id,
                details={
                    "pending_discovery_id": item.pending_discovery_id,
                    "failure_code": item.failure_code,
                    "detail": item.detail,
                    "task_id": task_id,
                },
                created_by="worker",
                origin="discovery.relevance_review",
                correlation_id=task_id,
            )
        )
        session.flush()

    def clear_in_session(
        self,
        session: Session,
        workspace_id: str,
        pending_discovery_id: str,
        *,
        actor: str = "worker",
        origin: str = "discovery.relevance_review",
        correlation_id: str | None = None,
    ) -> None:
        """Delete the mark standing against one discovery and audit the clearing.

        Callers that invalidate a row's review history clear the mark with it.
        The correlation defaults to the task whose rejection is being cleared.
        """
        row = self._standing(session, workspace_id, pending_discovery_id)
        if row is None:
            return
        entity_id = row.id
        task_id = row.task_id
        session.delete(row)
        session.add(
            AuditEvent(
                workspace_id=workspace_id,
                action="relevance_review_rejection.cleared",
                entity_type="relevance_review_rejection",
                entity_id=entity_id,
                details={"pending_discovery_id": pending_discovery_id, "task_id": task_id},
                **audit_columns(actor, origin, correlation_id or task_id),
            )
        )
        session.flush()

    @staticmethod
    def _standing(
        session: Session, workspace_id: str, pending_discovery_id: str
    ) -> RelevanceReviewRejection | None:
        return session.scalar(
            select(RelevanceReviewRejection).where(
                RelevanceReviewRejection.workspace_id == workspace_id,
                RelevanceReviewRejection.pending_discovery_id == pending_discovery_id,
            )
        )


def rejections_for_pending_ids(
    session: Session, workspace_id: str, pending_ids: Sequence[str]
) -> dict[str, RelevanceReviewRejectionResponse]:
    """Return the mark standing against each requested discovery, keyed by discovery."""
    if not pending_ids:
        return {}
    rows = session.scalars(
        select(RelevanceReviewRejection).where(
            RelevanceReviewRejection.workspace_id == workspace_id,
            RelevanceReviewRejection.pending_discovery_id.in_(pending_ids),
        )
    ).all()
    return {
        row.pending_discovery_id: RelevanceReviewRejectionResponse.model_validate(
            {
                "pending_discovery_id": row.pending_discovery_id,
                "failure_code": row.failure_code,
                "detail": row.detail,
                "rejected_at": row.rejected_at,
                "task_id": row.task_id,
            }
        )
        for row in rows
    }
