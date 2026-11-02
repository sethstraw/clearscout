"""The filtered review queue read as ranked lanes from one snapshot."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from tailorcv.application.discovery.pending import (
    campaign_staged_ids,
    dismissed_pending_discovery_ids,
    family_markers,
    listed_pending_discovery_filters,
    source_employer_names,
)
from tailorcv.application.discovery.scan_queries.pending_rows import PendingRowQueries
from tailorcv.application.discovery.scan_queries.projection import (
    duplicate_suggestions_in_session,
    ranked_discoveries,
    screened_projection,
)
from tailorcv.application.discovery.scan_queries.queue import (
    PendingQueueFilters,
    QueueContext,
    ScreenedRead,
    employer_facets,
    queue_facets,
    screened_queue,
    search_filters,
    snapshot_digest,
    validate_pending_page_request,
)
from tailorcv.application.discovery.scan_queries.workspace import require_workspace_in
from tailorcv.application.failures import ConflictError
from tailorcv.application.job_search.models import (
    PendingDiscoveryLanePageResponse,
    PendingDiscoveryPageResponse,
)
from tailorcv.domain import utc_now
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import PendingDiscovery, PendingPostingContent


class PendingQueueQueries:
    """Read the filtered review queue as ranked lanes from one snapshot."""

    def __init__(self, database: Database, rows: PendingRowQueries) -> None:
        """Bind the queue to the database and the row projection its lanes page through."""
        self.database = database
        self.rows = rows

    def pending_discovery_page(
        self,
        workspace_id: str,
        *,
        ready_offset: int = 0,
        attention_offset: int = 0,
        limit: int = 50,
        expected_snapshot_sha256: str | None = None,
        snapshot_before: datetime | None = None,
        filters: PendingQueueFilters | None = None,
    ) -> PendingDiscoveryPageResponse:
        """Return bounded lanes of the filtered queue from one snapshot while new rows arrive."""
        validate_pending_page_request(
            ready_offset,
            attention_offset,
            limit,
            expected_snapshot_sha256,
            snapshot_before,
        )
        filters = filters or PendingQueueFilters()
        snapshot_before = snapshot_before or utc_now()
        with self.database.session() as session:
            require_workspace_in(session, workspace_id)
            dismissed = dismissed_pending_discovery_ids(workspace_id)
            listed = (
                *listed_pending_discovery_filters(workspace_id, dismissed),
                PendingDiscovery.created_at <= snapshot_before,
            )
            queue_total = session.scalar(
                select(func.count()).select_from(PendingDiscovery).where(*listed)
            )
            read = self.screened_read(session, workspace_id, listed, filters)
            screened, context, projections_by_id = read.screened, read.context, read.projections
            content_by_pending = {row.id: content for row, content in read.candidates}
            candidate_by_id = {row.id: row for row, _content in read.candidates}
            searched, source_employers = read.searched, read.source_employers
            snapshot_sha256 = snapshot_digest(screened.lanes)
            if expected_snapshot_sha256 is not None and expected_snapshot_sha256 != snapshot_sha256:
                raise ConflictError(
                    "The pending discovery queue changed within this snapshot; "
                    "restart from the first page."
                )

            ready_ids = screened.lanes[True][ready_offset : ready_offset + limit]
            attention_ids = screened.lanes[False][attention_offset : attention_offset + limit]
            # Comparing a row against every root is the same work for both lanes.
            suggestions = duplicate_suggestions_in_session(
                session, workspace_id, (*ready_ids, *attention_ids)
            )
            place_reader = read.parameters.place_reader

            def page(
                is_ready: bool, offset: int, selected_ids: list[str]
            ) -> PendingDiscoveryLanePageResponse:
                lane = screened.lanes[is_ready]
                selected = [
                    (candidate_by_id[pending_id], content_by_pending[pending_id])
                    for pending_id in selected_ids
                ]
                items = self.rows.pending_responses_for_rows(
                    session,
                    workspace_id,
                    selected,
                    searched=searched,
                    projections_by_id=projections_by_id,
                    context=context,
                    suggestions=suggestions,
                    source_employers=source_employers,
                    place_reader=place_reader,
                )
                return PendingDiscoveryLanePageResponse(
                    items=items,
                    total=len(lane),
                    offset=offset,
                    limit=limit,
                    has_more=offset + len(items) < len(lane),
                )

            return PendingDiscoveryPageResponse(
                snapshot_sha256=snapshot_sha256,
                snapshot_before=snapshot_before,
                ready=page(True, ready_offset, ready_ids),
                attention=page(False, attention_offset, attention_ids),
                queue_total=queue_total or 0,
                facets=queue_facets(screened, filters),
                employers=employer_facets(screened),
            )

    def screened_read(
        self,
        session: Session,
        workspace_id: str,
        listed: Sequence[ColumnElement[bool]],
        filters: PendingQueueFilters,
    ) -> ScreenedRead:
        """Read, rank and screen the listed rows, splitting what the filters keep into lanes."""
        candidates = session.execute(
            select(PendingDiscovery, PendingPostingContent)
            .outerjoin(
                PendingPostingContent,
                PendingPostingContent.id == PendingDiscovery.current_content_id,
            )
            .where(*listed, *search_filters(filters.search))
        ).all()
        parameters, searched, missed_texts, missed_penalty = self.rows.pending_context(
            session, workspace_id
        )
        ranked = ranked_discoveries(
            [row for row, _content in candidates],
            parameters,
            searched,
            missed_texts,
            missed_penalty,
        )
        source_employers = source_employer_names(
            session, (row.source_id for row, _content in candidates)
        )
        candidate_ids = [row.id for row, _content in candidates]
        markers = family_markers(session, workspace_id, candidate_ids)
        reviews, rejections = self.rows.relevance_facts(
            session, workspace_id, candidate_ids, markers
        )
        context = QueueContext(
            markers=markers,
            reviews=reviews,
            rejections=rejections,
            campaign_staged=campaign_staged_ids(session, candidate_ids),
            cut_off=self.rows.cut_off_content_ids(
                session, workspace_id, (content for _row, content in candidates)
            ),
        )
        projections = {
            item[1].id: screened_projection(item, parameters, searched, source_employers)
            for item in ranked
        }
        content_by_pending = {row.id: content for row, content in candidates}
        return ScreenedRead(
            candidates=[(row, content) for row, content in candidates],
            parameters=parameters,
            searched=searched,
            source_employers=source_employers,
            context=context,
            projections=projections,
            screened=screened_queue(projections, content_by_pending, context, filters),
        )
