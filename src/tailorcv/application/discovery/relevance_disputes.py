"""Workspace disputes of the relevance verdict recorded for one discovery.

The dispute is stored beside the review rather than in it, so
``relevance_reviews`` stays what the pipeline wrote. Every change writes an
``audit_events`` entry, so the current state is one row and the history stays
recoverable.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tailorcv.application.discovery.relevance_review_records import current_review_in
from tailorcv.application.failures import ConflictError, NotFoundError
from tailorcv.application.workspaces.records.access import audit_columns
from tailorcv.domain import utc_now
from tailorcv.domain.rulings import RelevanceDispute
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import AuditEvent, RelevanceReviewDispute


class RelevanceDisputeService:
    """Record and withdraw a workspace dispute of one discovery's relevance verdict."""

    def __init__(self, database: Database) -> None:
        """Bind the service to one durable database."""
        self.database = database

    def dispute(
        self,
        *,
        workspace_id: str,
        pending_discovery_id: str,
        relevance_review_id: str,
        note: str | None,
        audit: dict[str, Any],
    ) -> RelevanceReviewDispute:
        """Set aside the named review, replacing any dispute the discovery carries.

        Raises NotFoundError when the discovery has no review, and ConflictError
        when the named review is not the one in force, so a dispute never lands
        on a verdict the person did not read.
        """
        with self.database.session() as session:
            current = current_review_in(session, workspace_id, pending_discovery_id)
            if current is None:
                raise NotFoundError("That discovery has no relevance review to dispute.")
            if current.id != relevance_review_id:
                raise ConflictError(
                    "A newer relevance review replaced the one shown. Read it before disputing."
                )
            row = session.scalar(
                select(RelevanceReviewDispute).where(
                    RelevanceReviewDispute.workspace_id == workspace_id,
                    RelevanceReviewDispute.pending_discovery_id == pending_discovery_id,
                )
            )
            if row is None:
                row = RelevanceReviewDispute(
                    workspace_id=workspace_id,
                    pending_discovery_id=pending_discovery_id,
                    relevance_review_id=relevance_review_id,
                    disposition=RelevanceDispute.SET_ASIDE.value,
                    note=note,
                    updated_at=utc_now(),
                    **audit,
                )
                session.add(row)
            else:
                row.relevance_review_id = relevance_review_id
                row.disposition = RelevanceDispute.SET_ASIDE.value
                row.note = note
                row.updated_at = utc_now()
            session.flush()
            session.add(
                AuditEvent(
                    workspace_id=workspace_id,
                    action="relevance_dispute.set",
                    entity_type="relevance_review_dispute",
                    entity_id=row.id,
                    details={
                        "pending_discovery_id": pending_discovery_id,
                        "relevance_review_id": relevance_review_id,
                        "disposition": RelevanceDispute.SET_ASIDE.value,
                        "recommendation": current.recommendation,
                        "note": note,
                    },
                    **audit,
                )
            )
            session.flush()
            session.expunge(row)
            return row

    def clear(
        self,
        *,
        workspace_id: str,
        pending_discovery_id: str,
        audit: dict[str, Any],
    ) -> bool:
        """Withdraw the dispute on one discovery. Returns whether a row was cleared."""
        with self.database.session() as session:
            return self.clear_in_session(
                session,
                workspace_id,
                pending_discovery_id,
                actor=audit["created_by"],
                origin=audit["origin"],
                correlation_id=audit["correlation_id"],
            )

    def clear_in_session(
        self,
        session: Session,
        workspace_id: str,
        pending_discovery_id: str,
        *,
        actor: str,
        origin: str,
        correlation_id: str | None,
    ) -> bool:
        """Delete the dispute standing on one discovery and audit the clearing.

        Callers that delete a discovery's reviews clear its dispute first, since
        the dispute names one of those reviews. Returns whether a row was cleared.
        """
        row = session.scalar(
            select(RelevanceReviewDispute).where(
                RelevanceReviewDispute.workspace_id == workspace_id,
                RelevanceReviewDispute.pending_discovery_id == pending_discovery_id,
            )
        )
        if row is None:
            return False
        entity_id = row.id
        disputed_review_id = row.relevance_review_id
        session.delete(row)
        session.add(
            AuditEvent(
                workspace_id=workspace_id,
                action="relevance_dispute.cleared",
                entity_type="relevance_review_dispute",
                entity_id=entity_id,
                details={
                    "pending_discovery_id": pending_discovery_id,
                    "relevance_review_id": disputed_review_id,
                },
                **audit_columns(actor, origin, correlation_id),
            )
        )
        session.flush()
        return True
