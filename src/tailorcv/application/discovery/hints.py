"""Correct a person's display hints on a staged pending discovery.

Only the title is correctable; employer and place come from the publisher when
the posting is evaluated. Every correction appends a
`pending_discovery.hint_corrected` audit event carrying both titles.
"""

from __future__ import annotations

from sqlalchemy import delete, select

from tailorcv.application.discovery.pending import TITLE_CORRECTED_ACTION, active_task_targets
from tailorcv.application.discovery.relevance_disputes import RelevanceDisputeService
from tailorcv.application.discovery.relevance_review_rejections import (
    RelevanceReviewRejectionService,
)
from tailorcv.application.discovery.scan_queries import SourceScanQueries
from tailorcv.application.failures import ConflictError, NotFoundError
from tailorcv.application.job_search.models import PendingDiscoveryResponse
from tailorcv.application.workspaces.records.access import audit_columns
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    AuditEvent,
    PendingDiscovery,
    RelevanceReview,
)


class PendingDiscoveryHintsService:
    """Own human corrections to the display hints on a pending discovery."""

    def __init__(self, database: Database, *, queries: SourceScanQueries | None = None) -> None:
        """Bind the capability to one durable database and one projector."""
        self.database = database
        self.queries = queries or SourceScanQueries(database)

    def correct_title(
        self,
        workspace_id: str,
        pending_id: str,
        title: str,
        *,
        actor: str = "local_user",
        origin: str = "api",
        correlation_id: str | None = None,
    ) -> PendingDiscoveryResponse:
        """Overwrite the pending discovery's title hint and append an audit event.

        A changed title clears the row's relevance reviews, any dispute of them,
        and any mark a rejected review left, which last only while the body and
        title they judged are unchanged.

        Raises NotFoundError when no row belongs to (workspace_id, pending_id).
        Raises ConflictError when the row is archived or when a paid discovery
        task is queued or running for it; a mid-flight extraction would
        otherwise overwrite the correction and read as ignored.
        """
        with self.database.session() as session:
            row = session.scalar(
                select(PendingDiscovery).where(
                    PendingDiscovery.workspace_id == workspace_id,
                    PendingDiscovery.id == pending_id,
                )
            )
            if row is None:
                raise NotFoundError(f"Pending discovery '{pending_id}' does not exist.")
            if row.archived_at is not None:
                raise ConflictError(
                    "This discovery has been archived; its title can no longer be corrected."
                )
            if active_task_targets(session, workspace_id, pending_id):
                raise ConflictError(
                    "A guided search or discovery evaluation is in flight for this row; "
                    "wait for it to finish before correcting the title."
                )
            old_title = row.title
            row.title = title
            if old_title != title:
                RelevanceDisputeService(self.database).clear_in_session(
                    session,
                    workspace_id,
                    pending_id,
                    actor=actor,
                    origin=origin,
                    correlation_id=correlation_id,
                )
                session.execute(
                    delete(RelevanceReview).where(
                        RelevanceReview.pending_discovery_id == pending_id
                    )
                )
                RelevanceReviewRejectionService(self.database).clear_in_session(
                    session,
                    workspace_id,
                    pending_id,
                    actor=actor,
                    origin=origin,
                    correlation_id=correlation_id,
                )
            session.add(
                AuditEvent(
                    workspace_id=workspace_id,
                    action=TITLE_CORRECTED_ACTION,
                    entity_type="pending_discovery",
                    entity_id=pending_id,
                    details={"old_title": old_title, "new_title": title},
                    **audit_columns(actor, origin, correlation_id),
                )
            )
            session.flush()
            return self.queries.get_pending_discovery(workspace_id, pending_id, session=session)
