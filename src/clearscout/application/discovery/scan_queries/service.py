"""The scan read facade the services and routes share."""

from __future__ import annotations

from collections.abc import Generator, Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from clearscout.application.discovery.content_resolution import CurrentPostingContentReader
from clearscout.application.discovery.pending import (
    dismissed_pending_discovery_ids,
    listed_pending_discovery_filters,
    visible_pending_discovery_filters,
)
from clearscout.application.discovery.proposals import accepted_titles
from clearscout.application.discovery.scan_queries.history import (
    ScanHistoryQueries,
    frontier_status_counts,
    latest_scan_ids_in,
    scan_follow_up_states,
)
from clearscout.application.discovery.scan_queries.outcomes import (
    ScanOutcomeFilters,
    ScanOutcomeQueries,
)
from clearscout.application.discovery.scan_queries.pending_rows import PendingRowQueries
from clearscout.application.discovery.scan_queries.queue import PendingQueueFilters
from clearscout.application.discovery.scan_queries.queue_page import PendingQueueQueries
from clearscout.application.discovery.scan_queries.workspace import require_workspace_in
from clearscout.application.failures import ValidationError
from clearscout.application.job_search.models import (
    PendingDiscoveryChangesFollowUpCounts,
    PendingDiscoveryChangesResponse,
    PendingDiscoveryChangesVisibleCounts,
    PendingDiscoveryPageResponse,
    PendingDiscoveryResponse,
    PostingRefusalResponse,
    SourceScanHistoryPageResponse,
    SourceScanHistoryStatus,
    SourceScanOutcomePageResponse,
    SourceScanOutcomeResponse,
    TitleExclusionCollateralResponse,
    TitleExclusionCollateralRowResponse,
)
from clearscout.domain.search_parameters import TargetTitle
from clearscout.domain.search_parameters.roles import title_excluded
from clearscout.domain.staged_facts import StagedListingFacts
from clearscout.infrastructure.artifacts.runtime import RuntimeStorage
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    BackgroundTask,
    JobSearchParameterSet,
    PendingDiscovery,
    PostingRefusal,
    PostingUrl,
)


class SourceScanQueries:
    """Own bounded reads over durable board-scan records."""

    def __init__(self, database: Database, *, storage: RuntimeStorage | None = None) -> None:
        """Bind all scan reads to the application database and, optionally, retained bodies."""
        self.database = database
        self.content_reader = (
            CurrentPostingContentReader(database, storage=storage) if storage is not None else None
        )
        self.rows = PendingRowQueries(database, self.content_reader)
        self.queue = PendingQueueQueries(database, self.rows)
        self.history = ScanHistoryQueries(database)
        self.outcomes = ScanOutcomeQueries(database)

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
        return self.queue.pending_discovery_page(
            workspace_id,
            ready_offset=ready_offset,
            attention_offset=attention_offset,
            limit=limit,
            expected_snapshot_sha256=expected_snapshot_sha256,
            snapshot_before=snapshot_before,
            filters=filters,
        )

    def list_pending_discoveries(
        self,
        workspace_id: str,
        *,
        limit: int = 50,
        closed_only: bool = False,
        ready_limit: int | None = None,
        attention_limit: int | None = None,
    ) -> list[PendingDiscoveryResponse]:
        """Return one active page, the newest closed postings, or bounded readiness cohorts."""
        return self.rows.list_pending_discoveries(
            workspace_id,
            limit=limit,
            closed_only=closed_only,
            ready_limit=ready_limit,
            attention_limit=attention_limit,
        )

    def get_pending_discovery(
        self,
        workspace_id: str,
        pending_id: str,
        *,
        session: Session | None = None,
    ) -> PendingDiscoveryResponse:
        """Project one pending discovery; a caller in a write transaction passes its session."""
        return self.rows.get_pending_discovery(workspace_id, pending_id, session=session)

    def list_scan_history(
        self,
        workspace_id: str,
        *,
        offset: int = 0,
        limit: int = 50,
        source_id: str = "",
        status: SourceScanHistoryStatus | None = None,
        latest_only: bool = False,
    ) -> SourceScanHistoryPageResponse:
        """Return a filtered page of kept scans; latest_only keeps each source's last board scan."""
        return self.history.list_scan_history(
            workspace_id,
            offset=offset,
            limit=limit,
            source_id=source_id,
            status=status,
            latest_only=latest_only,
        )

    def export_scan_outcomes(
        self,
        workspace_id: str,
        *,
        task_id: str,
        filters: ScanOutcomeFilters | None = None,
    ) -> Generator[SourceScanOutcomeResponse, None, None]:
        """Stream the filtered ledger in one bounded-memory read without page metadata."""
        return self.outcomes.export_scan_outcomes(workspace_id, task_id=task_id, filters=filters)

    def list_scan_outcomes(
        self,
        workspace_id: str,
        *,
        task_id: str,
        filters: ScanOutcomeFilters | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> SourceScanOutcomePageResponse:
        """Return one queryable task-scoped candidate audit ledger."""
        return self.outcomes.list_scan_outcomes(
            workspace_id, task_id=task_id, filters=filters, offset=offset, limit=limit
        )

    def pending_discovery_changes(
        self,
        workspace_id: str,
        *,
        snapshot_before: datetime,
        filters: PendingQueueFilters | None = None,
    ) -> PendingDiscoveryChangesResponse:
        """Count rows newer than a snapshot that the filtered queue would list, and follow-up."""
        with self.database.session() as session:
            require_workspace_in(session, workspace_id)
            dismissed = dismissed_pending_discovery_ids(workspace_id)
            newer = (
                *listed_pending_discovery_filters(workspace_id, dismissed),
                PendingDiscovery.created_at > snapshot_before,
            )
            # A poll usually finds nothing newer, and the screen's context read is its cost.
            any_newer = session.scalar(select(PendingDiscovery.id).where(*newer).limit(1))
            lanes = (
                self.queue.screened_read(
                    session, workspace_id, newer, filters or PendingQueueFilters()
                ).screened.lanes
                if any_newer is not None
                else {True: [], False: []}
            )
            latest_scan_ids = latest_scan_ids_in(session, workspace_id)
            follow_up_states = scan_follow_up_states(
                session, workspace_id, sorted(latest_scan_ids), latest_scan_ids
            )
            active_scan_ids = [
                scan_id for scan_id, state in follow_up_states.items() if state == "active"
            ]
            status_counts, automatic_pending_counts = frontier_status_counts(
                session, active_scan_ids
            )
            return PendingDiscoveryChangesResponse(
                newer_visible=PendingDiscoveryChangesVisibleCounts(
                    ready=len(lanes[True]), attention=len(lanes[False])
                ),
                follow_up=PendingDiscoveryChangesFollowUpCounts(
                    active_sources=len(active_scan_ids),
                    automatic_pending_count=sum(automatic_pending_counts.values()),
                    frontier_completed_count=sum(
                        status_counts.get((scan_id, "completed"), 0) for scan_id in active_scan_ids
                    ),
                ),
            )

    def title_exclusion_collateral(
        self, workspace_id: str, phrases: Sequence[str]
    ) -> TitleExclusionCollateralResponse:
        """Count the visible queue rows each title-skip phrase would also hide.

        Judged with the shipped exclusion predicate over the row's copied title
        and its publisher-evidenced aliases, the same candidates screening reads.
        """
        with self.database.session() as session:
            require_workspace_in(session, workspace_id)
            dismissed = dismissed_pending_discovery_ids(workspace_id)
            rows = session.execute(
                select(PendingDiscovery.title, PendingDiscovery.staged_facts).where(
                    *visible_pending_discovery_filters(workspace_id, dismissed)
                )
            ).all()
            candidates = [
                (title, *StagedListingFacts.from_json(staged).role_aliases.values)
                for title, staged in rows
            ]
            wanted = list(dict.fromkeys(phrase for phrase in phrases if phrase.strip()))
            return TitleExclusionCollateralResponse(
                queue_count=len(rows),
                phrases=[
                    TitleExclusionCollateralRowResponse(
                        phrase=phrase,
                        matched_count=sum(
                            1
                            for names in candidates
                            if any(title_excluded(name, [phrase]) is not None for name in names)
                        ),
                    )
                    for phrase in wanted
                ],
            )

    def list_refusals(self, workspace_id: str, *, limit: int = 50) -> list[PostingRefusalResponse]:
        """Return historical condition-based refusals, including archived targets."""
        if limit < 1 or limit > 250:
            raise ValidationError("Posting refusal limit must be between 1 and 250.")
        with self.database.session() as session:
            require_workspace_in(session, workspace_id)
            posting_url = (
                select(PostingUrl.url)
                .where(PostingUrl.job_posting_id == PostingRefusal.job_posting_id)
                .order_by(PostingUrl.is_primary.desc(), PostingUrl.created_at, PostingUrl.id)
                .limit(1)
                .correlate(PostingRefusal)
                .scalar_subquery()
            )
            rows = session.execute(
                select(
                    PostingRefusal,
                    JobSearchParameterSet.version,
                    func.coalesce(
                        posting_url,
                        func.nullif(PendingDiscovery.canonical_url, ""),
                        func.nullif(PendingDiscovery.discovery_url, ""),
                    ),
                )
                .outerjoin(
                    JobSearchParameterSet,
                    JobSearchParameterSet.id == PostingRefusal.search_parameter_set_id,
                )
                .outerjoin(
                    PendingDiscovery, PendingDiscovery.id == PostingRefusal.pending_discovery_id
                )
                .where(PostingRefusal.workspace_id == workspace_id)
                .order_by(PostingRefusal.refused_at.desc(), PostingRefusal.id)
                .limit(limit)
            ).all()
            return [
                PostingRefusalResponse(
                    id=refusal.id,
                    job_posting_id=refusal.job_posting_id,
                    pending_discovery_id=refusal.pending_discovery_id,
                    posting_url=url,
                    employer=refusal.employer,
                    title=refusal.title,
                    condition=refusal.condition,
                    reason=refusal.reason,
                    refused_at=refusal.refused_at,
                    search_parameter_set_id=refusal.search_parameter_set_id,
                    search_parameter_version=version,
                )
                for refusal, version, url in rows
            ]

    def derived_titles(self, workspace_id: str) -> list[TargetTitle]:
        """Return title suggestions allowed by the latest workspace decisions."""
        with self.database.session() as session:
            return self.derived_titles_in_session(session, workspace_id)

    def derived_titles_in_session(self, session: Session, workspace_id: str) -> list[TargetTitle]:
        """Return workspace-effective accepted titles in the caller's transaction."""
        return list(accepted_titles(session, workspace_id))

    def completed_guided_web_runs(self, workspace_id: str) -> int:
        """Count hosted searches completed under the current parameter version."""
        with self.database.session() as session:
            effective_at = session.scalar(
                select(func.max(JobSearchParameterSet.effective_at)).where(
                    JobSearchParameterSet.workspace_id == workspace_id
                )
            )
            conditions = [
                BackgroundTask.workspace_id == workspace_id,
                BackgroundTask.task_type == "search.guided_campaign",
                BackgroundTask.status == "completed",
            ]
            if effective_at is not None:
                conditions.append(BackgroundTask.created_at >= effective_at)
            count = session.scalar(
                select(func.count()).select_from(BackgroundTask).where(*conditions)
            )
            return int(count or 0)

    def require_workspace(self, workspace_id: str) -> None:
        """Refuse work addressed to a workspace that does not exist."""
        with self.database.session() as session:
            require_workspace_in(session, workspace_id)
