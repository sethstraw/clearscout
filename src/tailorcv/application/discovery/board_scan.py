"""One way in to board scanning, over the collaborators that do the work.

`SourceScanService` is a facade over the sweep, the source catalog, the
review-queue reads, and listing staging, and the single surface the API, the
CLI, the worker, and guided search bind to.
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Iterable, Sequence
from datetime import date, datetime
from functools import partial
from threading import Lock
from typing import Any, Literal, Protocol

import httpx
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session, aliased

from tailorcv.application.discovery.content_resolution import (
    CurrentPostingContentReader,
    PostingContentResolutionService,
)
from tailorcv.application.discovery.funnel_audit import (
    FunnelCandidateKind,
    FunnelOutcome,
    FunnelReason,
    FunnelStage,
)
from tailorcv.application.discovery.hints import PendingDiscoveryHintsService
from tailorcv.application.discovery.pending import (
    reset_pending_queue,
    set_pending_closing_date,
    set_pending_markers,
)
from tailorcv.application.discovery.posting_fetch import PostingFetcher
from tailorcv.application.discovery.posting_identity import BoardHolder
from tailorcv.application.discovery.posting_refresh import PostingRefreshService
from tailorcv.application.discovery.scan_queries import (
    PendingQueueFilters,
    ScanOutcomeFilters,
    SourceScanQueries,
    kept_scan_id_statement,
    registered_board_holder,
    require_workspace_in,
)
from tailorcv.application.discovery.scan_staging import DiscoveryStaging, StagedListings
from tailorcv.application.discovery.scan_sweep import BoardSweep, pending_row_failure
from tailorcv.application.discovery.screened_out_titles import (
    list_screened_out_titles,
    mark_screened_out_titles_reviewed,
    record_screened_out_titles,
)
from tailorcv.application.discovery.source_continuation import (
    continuation_task_ids,
    is_current_in_session,
)
from tailorcv.application.discovery.source_lifecycle import SourceCatalog
from tailorcv.application.discovery.source_reading import (
    PostingCheck,
    SourceDetailCheckpoint,
    SourceListing,
    SourceReader,
)
from tailorcv.application.failures import (
    PublisherUnverifiedRefreshError,
    UnsupportedPublisherRefreshError,
    ValidationError,
)
from tailorcv.application.job_search.models import (
    ClosingDateEditInput,
    FoundPostingAddResponse,
    FoundPostingCheckResponse,
    FoundPostingCreatedResponse,
    FoundPostingRequeuedResponse,
    PendingClosureReconcileRequest,
    PendingClosureReconcileResponse,
    PendingDiscoveryChangesResponse,
    PendingDiscoveryPageResponse,
    PendingDiscoveryResponse,
    PendingMarkersRequest,
    PendingQueueResetResponse,
    PostingRefusalResponse,
    ScreenedOutTitlePageResponse,
    ScreenedOutTitleReviewResponse,
    SourceListResponse,
    SourcePreviewResponse,
    SourceResponse,
    SourceScanHistoryPageResponse,
    SourceScanHistoryStatus,
    SourceScanOutcomePageResponse,
    SourceScanOutcomeResponse,
    SourceTerms,
    StagedListingFactsResponse,
    TitleExclusionCollateralResponse,
)
from tailorcv.application.tasks import (
    SourceScanPayload,
    TaskContext,
    TaskQueueService,
    TaskResponse,
)
from tailorcv.application.tasks.leases import CONTINUATION_TASK_TYPE
from tailorcv.application.tasks.payloads import (
    DiscoveryRefreshBatchPayload,
    DiscoveryRescreenQueuePayload,
)
from tailorcv.application.workspaces import SearchParametersService
from tailorcv.application.workspaces.parameter_models import SearchParametersSavedResponse
from tailorcv.domain import (
    PostingContentAccessStatus,
    SourceKind,
    new_id,
)
from tailorcv.domain.identity import normalize_url
from tailorcv.domain.records import PostingReadIntent, PostingRefreshOutcome
from tailorcv.domain.search_parameters import TargetTitle
from tailorcv.infrastructure.artifacts.runtime import RuntimeStorage
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    BackgroundTask,
    PendingDiscovery,
    Source,
    SourceScan,
    SourceScanContinuation,
    SourceScanFrontierItem,
    SourceScanOutcome,
)
from tailorcv.infrastructure.web_fetch.board_adapters import BoardApiFetcher, BoardResolver
from tailorcv.infrastructure.web_fetch.ingest import url as url_ingest
from tailorcv.infrastructure.web_fetch.ingest.url import check_posting, workday_posting_detail
from tailorcv.infrastructure.web_fetch.policy import FetchPolicyError, PoliteHttpClient
from tailorcv.shared.errors import TailorcvError

_DEAD_LINK_STATUSES = ("404", "410")
# Ledger families memoized at once, well above the worker's concurrent recording passes.
_LEDGER_SCOPE_CACHE_SIZE = 64


def _link_is_dead(note: str) -> bool:
    """A stored link that 404s or 410s is gone, not momentarily unreadable."""
    return any(f"HTTP {code}" in note for code in _DEAD_LINK_STATUSES)


def _refresh_failure_message(employer: str, title: str, note: str) -> str:
    """Name the row a failed refresh belongs to, and flag a dead link as re-addable."""
    employer = employer.strip()
    row = ", ".join(part for part in (employer, title.strip()) if part)
    prefix = f"{row}: " if row else ""
    if employer and _link_is_dead(note):
        return (
            f"{prefix}{note} This link is dead, but {employer} may still be advertising the "
            f"posting on its own board; re-add it from there."
        )
    return f"{prefix}{note}"


def _known_posting_checker(
    url: str,
    *,
    client: PoliteHttpClient | None = None,
    intent: PostingReadIntent = PostingReadIntent.CRAWL,
) -> PostingCheck:
    """Check one posting's publisher facts; a stored row's check reads as a crawl."""
    return check_posting(url, client=client, intent=intent)


class PostingChecker(Protocol):
    """Check one posting's publisher facts, reading as the intent names."""

    def __call__(
        self, url: str, /, *, intent: PostingReadIntent = PostingReadIntent.CRAWL
    ) -> PostingCheck:
        """Return the publisher's facts for one posting address."""
        ...


class SourceScanService:
    """The board-scanning capability, presented as one surface."""

    def __init__(
        self,
        database: Database,
        *,
        storage: RuntimeStorage,
        posting_fetcher: PostingFetcher,
        fetcher: SourceReader | None = None,
        posting_checker: PostingChecker | None = None,
        workday_detail_checker: Callable[[str], PostingCheck] | None = None,
        content_resolution: PostingContentResolutionService | None = None,
        http_client: PoliteHttpClient | None = None,
        reader_alive: Callable[[int, str], bool | None] | None = None,
    ) -> None:
        """Bind the capability to one durable database and one network seam."""
        self.database = database
        # Keyed by (workspace, task), so concurrent slots' passes keep their own entries.
        self._ledger_scope_cache: dict[tuple[str, str], tuple[tuple[str, ...], tuple[str, ...]]]
        self._ledger_scope_cache = {}
        # A scan pass and continuation passes share this service across threads.
        self._ledger_scope_lock = Lock()
        reader = BoardApiFetcher(client=http_client) if fetcher is None else fetcher
        # The default readers share one client, and with it each host's robots and pacing.
        client = reader.client if isinstance(reader, BoardApiFetcher) else http_client
        self.posting_checker = posting_checker or partial(_known_posting_checker, client=client)
        self.fetcher = reader
        self.postings = PostingRefreshService(
            database,
            storage=storage,
            fetcher=posting_fetcher,
        )
        self.parameters = SearchParametersService(database)
        self.queries = SourceScanQueries(database, storage=storage)
        self.catalog = SourceCatalog(
            database,
            resolver=self.fetcher if isinstance(self.fetcher, BoardResolver) else None,
        )
        self.staging = DiscoveryStaging(database, self.postings)
        self.hints = PendingDiscoveryHintsService(database, queries=self.queries)
        self.sweep = BoardSweep(
            database,
            self.fetcher,
            parameters=self.parameters,
            queries=self.queries,
            catalog=self.catalog,
            staging=self.staging,
            content_resolution=content_resolution,
            # Always available, unlike content_resolution, which callers leave unset
            # whenever automatic network resolution must stay off during a scan.
            current_content=CurrentPostingContentReader(database, storage=storage),
            reader_alive=reader_alive,
            posting_checker=(
                workday_detail_checker
                or (partial(workday_posting_detail, client=client) if fetcher is None else None)
            ),
        )

    def scan(
        self,
        workspace_id: str,
        context: TaskContext | None = None,
        *,
        source_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Sweep the selected sources, or every enabled one, retaining each staged candidate."""
        # A retry keeps its task id, so only the pass boundary can tell one attempt
        # from the next; the family may have gained continuations in between.
        if context is not None:
            self.begin_recording_pass(context.task_id)
        try:
            return self.sweep.scan(
                workspace_id,
                context,
                source_ids=source_ids,
                board_outcome_sink=(
                    partial(
                        self.record_task_outcomes,
                        workspace_id,
                        context.task_id,
                        origin="source.scan",
                    )
                    if context is not None
                    else None
                ),
            )
        finally:
            if context is not None:
                self._attach_outcomes_to_source_scans(workspace_id, context.task_id)

    def reset_pending_discoveries(self, workspace_id: str) -> PendingQueueResetResponse:
        """Rebuild the pending queue around human choices and in-flight paid work."""
        self.queries.require_workspace(workspace_id)
        with self.database.exclusive_session() as session:
            result = reset_pending_queue(session, workspace_id)
        return PendingQueueResetResponse(
            archived_count=result.archived_count,
            kept_count=result.kept_count,
            rescreened_count=result.rescreened_count,
        )

    def set_pending_markers(
        self,
        workspace_id: str,
        pending_id: str,
        request: PendingMarkersRequest,
        *,
        actor: str = "local_user",
        origin: str = "api",
        correlation_id: str | None = None,
    ) -> PendingDiscoveryResponse:
        """Set a family's favorite and keep markers and return its head as the queue lists it."""
        self.queries.require_workspace(workspace_id)
        with self.database.exclusive_session() as session:
            head_id = set_pending_markers(
                session,
                workspace_id,
                pending_id,
                favorite=request.favorite,
                kept=request.kept,
                keep_note=request.keep_note,
                actor=actor,
                origin=origin,
                correlation_id=correlation_id,
            )
            session.flush()
            return self.queries.get_pending_discovery(workspace_id, head_id, session=session)

    def set_pending_closing_date(
        self,
        workspace_id: str,
        pending_id: str,
        request: ClosingDateEditInput,
        *,
        actor: str = "local_user",
        origin: str = "api",
        correlation_id: str | None = None,
    ) -> PendingDiscoveryResponse:
        """Set a personal closing date and return the discovery family's active head."""
        self.queries.require_workspace(workspace_id)
        with self.database.exclusive_session() as session:
            head_id = set_pending_closing_date(
                session,
                workspace_id,
                pending_id,
                request.closing_date_override,
                actor=actor,
                origin=origin,
                correlation_id=correlation_id,
            )
            session.flush()
            return self.queries.get_pending_discovery(workspace_id, head_id, session=session)

    @staticmethod
    def _continued_scan_ids(session: Session, task_id: str) -> tuple[str, ...]:
        """Return the scans whose deferred links this task reads; empty unless it continues one."""
        return tuple(
            session.scalars(
                select(SourceScanContinuation.source_scan_id)
                .join(
                    BackgroundTask,
                    BackgroundTask.payload["continuation_id"].as_string()
                    == SourceScanContinuation.id,
                )
                .where(
                    BackgroundTask.id == task_id,
                    BackgroundTask.task_type == CONTINUATION_TASK_TYPE,
                )
            )
        )

    def begin_recording_pass(self, task_id: str) -> None:
        """Drop this task's memoized ledger family so its pass resolves one of its own."""
        with self._ledger_scope_lock:
            for key in [key for key in self._ledger_scope_cache if key[1] == task_id]:
                del self._ledger_scope_cache[key]

    def _ledger_scope(
        self, session: Session, workspace_id: str, task_id: str
    ) -> tuple[tuple[str, ...], list[str]]:
        """Return the scans this task continues and the tasks sharing one ledger with it.

        One recording pass is one task, so a scan of several hundred candidates
        resolves this once and reuses it for every outcome it records.
        """
        key = (workspace_id, task_id)
        with self._ledger_scope_lock:
            cached = self._ledger_scope_cache.get(key)
        if cached is not None:
            return cached[0], list(cached[1])
        continued = self._continued_scan_ids(session, task_id)
        scan_task_id = task_id
        if continued:
            scan_task_id = (
                session.scalar(
                    select(SourceScan.correlation_id).where(SourceScan.id.in_(continued))
                )
                or task_id
            )
        # A same-day rescan takes over the scan row, so an earlier generation's task
        # still dedupes against its own rows after its scan stops owning it.
        ledger_task_ids = tuple(
            dict.fromkeys(
                (scan_task_id, task_id, *continuation_task_ids(session, workspace_id, scan_task_id))
            )
        )
        with self._ledger_scope_lock:
            self._ledger_scope_cache[key] = (continued, ledger_task_ids)
            while len(self._ledger_scope_cache) > _LEDGER_SCOPE_CACHE_SIZE:
                del self._ledger_scope_cache[next(iter(self._ledger_scope_cache))]
        return continued, list(ledger_task_ids)

    @staticmethod
    def _source_scan_ids(
        session: Session, workspace_id: str, task_id: str, continued: tuple[str, ...]
    ) -> dict[str, str | None]:
        """Map each unambiguous source label to the scan row for this task."""
        # A continuation task records under itself, so its scan is reached through
        # the continuation it drains rather than through the scan's correlation id.
        source_rows = session.execute(
            select(Source.name, SourceScan.id)
            .join(SourceScan, SourceScan.source_id == Source.id)
            .where(
                Source.workspace_id == workspace_id,
                or_(SourceScan.correlation_id == task_id, SourceScan.id.in_(continued)),
            )
        ).all()
        scan_ids: dict[str, str | None] = {}
        for source_name, scan_id in source_rows:
            scan_ids[source_name] = scan_id if source_name not in scan_ids else None
        return scan_ids

    def _attach_outcomes_to_source_scans(self, workspace_id: str, task_id: str) -> None:
        """Link already-recorded outcomes after their source scan rows are durable."""
        with self.database.session() as session:
            continued = self._continued_scan_ids(session, task_id)
            scan_ids = self._source_scan_ids(session, workspace_id, task_id, continued)
            outcomes = session.scalars(
                select(SourceScanOutcome).where(
                    SourceScanOutcome.workspace_id == workspace_id,
                    SourceScanOutcome.task_id == task_id,
                    SourceScanOutcome.source_scan_id.is_(None),
                )
            )
            for outcome in outcomes:
                source_scan_id = scan_ids.get(outcome.source_name)
                if source_scan_id is not None:
                    outcome.source_scan_id = source_scan_id

    def record_task_outcomes(
        self,
        workspace_id: str,
        task_id: str,
        outcomes: Iterable[FunnelOutcome],
        *,
        origin: str,
        default_source: str = "",
    ) -> None:
        """Persist task outcomes in one transaction while discovery is still progressing."""
        with self.database.session() as session:
            self.record_task_outcomes_in_session(
                session,
                workspace_id,
                task_id,
                outcomes,
                origin=origin,
                default_source=default_source,
            )

    @staticmethod
    def _current_outcome_continuation(
        session: Session, workspace_id: str, task_id: str, scan_id: str
    ) -> SourceScanContinuation | None:
        """Require an active uncancelled task in the owning current generation."""
        task = session.get(BackgroundTask, task_id)
        if (
            task is None
            or task.workspace_id != workspace_id
            or task.task_type != CONTINUATION_TASK_TYPE
            or task.status != "running"
            or task.cancellation_requested_at is not None
        ):
            return None
        continuation = session.get(SourceScanContinuation, task.payload.get("continuation_id"))
        generation = task.payload.get("generation")
        if (
            continuation is None
            or continuation.source_scan_id != scan_id
            or not isinstance(generation, int)
            or not is_current_in_session(session, continuation, generation)
        ):
            return None
        return continuation

    def _advance_metadata_outcome(
        self,
        session: Session,
        row: SourceScanOutcome,
        outcome: FunnelOutcome,
        *,
        workspace_id: str,
        task_id: str,
        scan_id: str | None,
        origin: str,
    ) -> bool:
        """Advance one listed identity only after its guarded readable body outcome."""
        if (
            scan_id is None
            or origin != "source.continue"
            or row.reason != FunnelReason.INCOMPLETE_POSTING.value
            or row.candidate_kind != FunnelCandidateKind.BOARD_LISTING.value
            or outcome.reason
            in {
                FunnelReason.INCOMPLETE_POSTING,
                FunnelReason.FETCH_FAILED,
                FunnelReason.FETCH_REFUSED,
                FunnelReason.NO_READABLE_BOARD,
            }
            or FunnelStage.READ not in outcome.stages
        ):
            return False
        scan = session.get(SourceScan, scan_id)
        if scan is None or (
            row.source_scan_id != scan_id
            and not (row.source_scan_id is None and row.task_id == scan.correlation_id)
        ):
            return False
        continuation = self._current_outcome_continuation(session, workspace_id, task_id, scan_id)
        if continuation is None:
            return False
        frontier = session.scalar(
            select(SourceScanFrontierItem).where(
                SourceScanFrontierItem.continuation_id == continuation.id,
                SourceScanFrontierItem.url == outcome.url,
            )
        )
        if frontier is None or frontier.detail_checkpoint is None:
            return False
        if SourceDetailCheckpoint.from_json(frontier.detail_checkpoint).platform != "alignerr":
            return False
        row.source_scan_id = scan_id
        row.employer, row.title, row.location = outcome.employer, outcome.title, outcome.location
        row.terminal_stage, row.reason = outcome.stages[-1].value, outcome.reason.value
        row.detail = outcome.detail[:1000]
        row.screening_findings = [
            finding.model_dump(mode="json") for finding in outcome.screening_findings
        ]
        row.origin, row.correlation_id = origin, task_id
        return True

    def record_task_outcomes_in_session(
        self,
        session: Session,
        workspace_id: str,
        task_id: str,
        outcomes: Iterable[FunnelOutcome],
        *,
        origin: str,
        default_source: str = "",
    ) -> tuple[FunnelOutcome, ...]:
        """Persist discovery outcomes in the caller's atomic transaction; return those added."""
        keyed = [
            (outcome, source_name, normalized_url)
            for outcome in outcomes
            if (source_name := outcome.source or default_source)
            and (normalized_url := normalize_url(outcome.url))
        ]
        if not keyed:
            return ()
        continued, ledger_task_ids = self._ledger_scope(session, workspace_id, task_id)
        scan_ids = self._source_scan_ids(session, workspace_id, task_id, continued)
        # One URL is one ledger row per source and kind across a scan and its
        # continuations, because the reader aggregates them into one funnel.
        recorded = {
            (row.source_name, row.candidate_kind, row.normalized_url): row
            for row in session.scalars(
                select(SourceScanOutcome).where(
                    SourceScanOutcome.workspace_id == workspace_id,
                    SourceScanOutcome.task_id.in_(ledger_task_ids),
                    SourceScanOutcome.source_name.in_({name for _, name, _ in keyed}),
                    SourceScanOutcome.candidate_kind.in_({item.kind.value for item, _, _ in keyed}),
                    SourceScanOutcome.normalized_url.in_({url for _, _, url in keyed}),
                )
            )
        }
        added: list[FunnelOutcome] = []
        newly: list[tuple[FunnelOutcome, str, str]] = []
        for outcome, source_name, normalized_url in keyed:
            key = (source_name, outcome.kind.value, normalized_url)
            if key in recorded:
                if self._advance_metadata_outcome(
                    session,
                    recorded[key],
                    outcome,
                    workspace_id=workspace_id,
                    task_id=task_id,
                    scan_id=scan_ids.get(source_name),
                    origin=origin,
                ):
                    newly.append((outcome, source_name, normalized_url))
                    added.append(outcome)
                continue
            newly.append((outcome, source_name, normalized_url))
            row = SourceScanOutcome(
                id=new_id(),
                workspace_id=workspace_id,
                task_id=task_id,
                source_scan_id=scan_ids.get(source_name),
                source_name=source_name,
                candidate_kind=outcome.kind.value,
                url=outcome.url,
                normalized_url=normalized_url,
                employer=outcome.employer,
                title=outcome.title,
                location=outcome.location,
                terminal_stage=outcome.stages[-1].value,
                reason=outcome.reason.value,
                detail=outcome.detail[:1000],
                screening_findings=[
                    finding.model_dump(mode="json") for finding in outcome.screening_findings
                ],
                created_by="worker",
                origin=origin,
                correlation_id=task_id,
            )
            session.add(row)
            recorded[key] = row
            added.append(outcome)
        record_screened_out_titles(session, workspace_id, newly)
        self._prune_repeated_outcomes(session, workspace_id, {url for _, _, url in newly})
        return tuple(added)

    @staticmethod
    def _prune_repeated_outcomes(
        session: Session, workspace_id: str, normalized_urls: set[str]
    ) -> None:
        """Delete these URLs' sightings that are neither first nor latest, outside kept scans."""
        if not normalized_urls:
            return
        session.flush()
        row, other = SourceScanOutcome, aliased(SourceScanOutcome)
        same_url = (other.workspace_id == row.workspace_id) & (
            other.normalized_url == row.normalized_url
        )
        earlier = (other.created_at < row.created_at) | (
            (other.created_at == row.created_at) & (other.id < row.id)
        )
        later = (other.created_at > row.created_at) | (
            (other.created_at == row.created_at) & (other.id > row.id)
        )
        session.execute(
            delete(row)
            .where(
                row.workspace_id == workspace_id,
                row.normalized_url.in_(normalized_urls),
                row.source_scan_id.is_not(None),
                row.source_scan_id.not_in(kept_scan_id_statement(workspace_id)),
                select(other.id).where(same_url, earlier).exists(),
                select(other.id).where(same_url, later).exists(),
            )
            .execution_options(synchronize_session="fetch")
        )

    def remember_if_readable(
        self,
        workspace_id: str,
        employer_name: str,
        board_url: str,
        *,
        evidence_text: str = "",
        origin: str,
        correlation_id: str | None = None,
    ) -> bool:
        """Verify a supported board before allowing it into the recurring sweep."""
        return self.sweep.remember_if_readable(
            workspace_id,
            employer_name,
            board_url,
            evidence_text=evidence_text,
            origin=origin,
            correlation_id=correlation_id,
        )

    def request_continuation(self, workspace_id: str, source_scan_id: str) -> TaskResponse:
        """Enqueue one bounded continuation read for a scan's deferred publisher links."""
        if self.sweep.continuations is None:
            raise ValidationError("This workspace has no configured continuation reader.")
        return self.sweep.continuations.request_continuation(workspace_id, source_scan_id)

    def stage_found_posting(
        self,
        workspace_id: str,
        url: str,
        today: date,
        *,
        title: str = "",
        employer: str = "",
        location: str = "",
        actor: str = "local_user",
        origin: str = "tailorcv",
        correlation_id: str | None = None,
        check_publisher_facts: bool = False,
        content_status: PostingContentAccessStatus | None = None,
        content_note: str | None = None,
    ) -> FoundPostingAddResponse | None:
        """Stage or requeue a posting and return its explicit outcome."""
        self.queries.require_workspace(workspace_id)
        checked = (
            self.posting_checker(url, intent=PostingReadIntent.SINGLE_POSTING)
            if check_publisher_facts
            else None
        )
        staged_status = content_status
        staged_note = content_note
        if checked is not None:
            staged_status = staged_status or PostingContentAccessStatus.NOT_YET_READ
            staged_note = staged_note if staged_note is not None else ""
        staged = self.staging.stage_found_posting(
            workspace_id,
            url,
            today,
            title=title,
            employer=employer,
            location=location,
            actor=actor,
            origin=origin,
            correlation_id=correlation_id,
            publisher_check=checked,
            content_status=staged_status,
            content_note=staged_note,
        )
        if staged is None:
            return None
        pending = self.queries.get_pending_discovery(workspace_id, staged.pending_discovery_id)
        response_check = self._check_response(checked) if checked is not None else None
        if staged.requeued:
            return FoundPostingRequeuedResponse(
                pending_discovery=pending, posting_check=response_check
            )
        return FoundPostingCreatedResponse(pending_discovery=pending, posting_check=response_check)

    def check_found_posting(self, workspace_id: str, url: str) -> FoundPostingCheckResponse:
        """Return one free publisher preflight without staging or enqueueing work."""
        self.queries.require_workspace(workspace_id)
        return self._check_response(
            self.posting_checker(url, intent=PostingReadIntent.SINGLE_POSTING)
        )

    def refresh_pending_discovery(
        self,
        workspace_id: str,
        pending_id: str,
        *,
        actor: str = "local_user",
        origin: str = "api",
        correlation_id: str | None = None,
    ) -> PendingDiscoveryResponse:
        """Refresh a pending row from free publisher metadata; a closed read withdraws it."""
        pending = self.queries.get_pending_discovery(workspace_id, pending_id)
        # A row a content read re-pointed is live or closed at the address its evidence reads.
        checked = self.posting_checker(pending.canonical_url or pending.discovery_url)
        if checked.status == "closed":
            self.staging.checks.withdraw_pending_discovery(
                workspace_id,
                pending_id,
                note=checked.note,
                actor=actor,
                origin=origin,
                correlation_id=correlation_id,
            )
            return self.queries.get_pending_discovery(workspace_id, pending_id)
        if checked.status == "unsupported":
            raise UnsupportedPublisherRefreshError(
                _refresh_failure_message(pending.employer, pending.title, checked.note)
            )
        if checked.status != "readable":
            message = _refresh_failure_message(pending.employer, pending.title, checked.note)
            if _link_is_dead(checked.note):
                raise ValidationError(message)
            raise PublisherUnverifiedRefreshError(message)
        self.staging.checks.refresh_pending_discovery(
            workspace_id,
            pending_id,
            checked,
            actor=actor,
            origin=origin,
            correlation_id=correlation_id,
        )
        return self.queries.get_pending_discovery(workspace_id, pending_id)

    def note_registered_board_holder(
        self, workspace_id: str, pending_id: str
    ) -> BoardHolder | None:
        """Name, in an unreadable row's content note, the stored board record holding its role."""
        with self.database.session() as session:
            holder = registered_board_holder(session, workspace_id, pending_id)
            pending = session.get(PendingDiscovery, pending_id)
            if holder is None or pending is None:
                return holder
            held = (
                f"tracked as job posting {holder.entity_id}"
                if holder.entity_type == "job_posting"
                else f"awaiting review as pending row {holder.entity_id}"
            )
            sentence = f"This role is {held} on the employer's registered board at {holder.url}."
            if sentence not in pending.content_note:
                pending.content_note = f"{pending.content_note} {sentence}".strip()
            return holder

    def reconcile_saved_closures(
        self,
        workspace_id: str,
        request: PendingClosureReconcileRequest,
        *,
        actor: str = "local_user",
        origin: str = "api",
        correlation_id: str | None = None,
    ) -> PendingClosureReconcileResponse:
        """Apply explicit current closure evidence without contacting a publisher."""
        return self.staging.checks.reconcile_saved_closures(
            workspace_id,
            request.pending_discovery_ids,
            actor=actor,
            origin=origin,
            correlation_id=correlation_id,
        )

    @staticmethod
    def _check_response(checked: PostingCheck) -> FoundPostingCheckResponse:
        # The manual-add preflight has no distinct "closed" wire state, so a closed
        # posting reads as unreadable there with the closure stated in the note.
        if checked.status == "closed":
            status: Literal["readable", "unreadable", "unsupported"] = "unreadable"
        else:
            status = checked.status
        return FoundPostingCheckResponse(
            status=status,
            note=checked.note,
            platform=checked.platform,
            title=checked.title,
            employer=checked.employer,
            location=checked.location,
            staged_facts=StagedListingFactsResponse.model_validate(checked.facts.as_json()),
        )

    def correct_pending_discovery_title(
        self,
        workspace_id: str,
        pending_id: str,
        title: str,
        *,
        actor: str = "local_user",
        origin: str = "api",
        correlation_id: str | None = None,
    ) -> PendingDiscoveryResponse:
        """Overwrite the title hint on one pending discovery.

        Employer and place come from the publisher when the posting is
        evaluated, so they are not correctable here. Every mutation appends a
        `pending_discovery.hint_corrected` audit event.
        """
        return self.hints.correct_title(
            workspace_id,
            pending_id,
            title,
            actor=actor,
            origin=origin,
            correlation_id=correlation_id,
        )

    def stage_listings(
        self,
        workspace_id: str,
        listings: list[SourceListing],
        today: date,
        *,
        reason: str,
        source_id: str | None = None,
        context: TaskContext | None = None,
    ) -> StagedListings:
        """Stage unseen listings and refresh tracked postings; report what was queued."""
        return self.staging.stage_listings(
            workspace_id,
            listings,
            today,
            reason=reason,
            source_id=source_id,
            context=context,
        )

    def stage_listings_in_session(
        self,
        session: Session,
        workspace_id: str,
        listings: list[SourceListing],
        today: date,
        *,
        reason: str,
        source_id: str | None = None,
        correlation_id: str | None = None,
        refreshes: list[tuple[str, SourceListing]],
        refresh_only: bool = False,
    ) -> StagedListings:
        """Stage listings inside the caller's transaction."""
        return self.staging.stage_listings_in_session(
            workspace_id,
            listings,
            today,
            reason=reason,
            source_id=source_id,
            correlation_id=correlation_id,
            session=session,
            refreshes=refreshes,
            refresh_only=refresh_only,
        )

    def refresh_tracked(
        self,
        workspace_id: str,
        refreshes: list[tuple[str, SourceListing]],
        context: TaskContext | None = None,
    ) -> None:
        """Refresh tracked jobs after their discovery transaction commits."""
        self.staging.refresh_tracked(workspace_id, refreshes, context)

    def list_sources(self, workspace_id: str) -> SourceListResponse:
        """List the board sources this workspace sweeps, with each one's last outcome."""
        return self.catalog.list_sources(workspace_id)

    def add_source(
        self,
        workspace_id: str,
        name: str,
        url: str,
        *,
        source_type: SourceKind = SourceKind.EMPLOYER_BOARD,
        terms: SourceTerms | None = None,
        enabled: bool = True,
        origin: str,
        correlation_id: str | None = None,
    ) -> SourceResponse:
        """Register one source by hand, kept even when its address names no known platform."""
        return self.catalog.add_source(
            workspace_id,
            name,
            url,
            source_type=source_type,
            terms=terms,
            enabled=enabled,
            origin=origin,
            correlation_id=correlation_id,
        )

    def update_source(
        self,
        workspace_id: str,
        source_id: str,
        *,
        name: str | None = None,
        url: str | None = None,
        careers_url: str | None = None,
        enabled: bool | None = None,
        priority: int | None = None,
        terms: SourceTerms | None = None,
        actor: str = "local_user",
        origin: str = "api",
        correlation_id: str | None = None,
    ) -> SourceResponse:
        """Change one source's name, address, company careers page, sweep state, or order."""
        return self.catalog.update_source(
            workspace_id,
            source_id,
            name=name,
            url=url,
            careers_url=careers_url,
            enabled=enabled,
            priority=priority,
            terms=terms,
            actor=actor,
            origin=origin,
            correlation_id=correlation_id,
        )

    def archive_source(self, workspace_id: str, source_id: str) -> None:
        """Stop sweeping one board, keeping the scans it has already recorded."""
        self.catalog.archive_source(workspace_id, source_id)

    def preview_url(
        self, workspace_id: str, url: str, source_type: SourceKind = SourceKind.EMPLOYER_BOARD
    ) -> SourcePreviewResponse:
        """Resolve one address without saving anything, before a source exists."""
        return self.catalog.preview_url(workspace_id, url, source_type)

    def resolve_platform(self, workspace_id: str, source_id: str) -> SourceResponse:
        """Re-run board resolution against one already-saved source address."""
        return self.catalog.resolve_platform(workspace_id, source_id)

    def remember_board(
        self,
        workspace_id: str,
        employer_name: str,
        board_url: str,
        *,
        enabled: bool = True,
        origin: str,
        correlation_id: str | None = None,
    ) -> str:
        """Persist one already-verified board as internal sweep state."""
        return self.catalog.remember_board(
            workspace_id,
            employer_name,
            board_url,
            enabled=enabled,
            origin=origin,
            correlation_id=correlation_id,
        )

    def remember_board_in_session(
        self,
        session: Session,
        workspace_id: str,
        employer_name: str,
        board_url: str,
        *,
        enabled: bool = True,
        origin: str,
        correlation_id: str | None = None,
    ) -> str:
        """Persist one board address inside the caller's transaction."""
        return self.catalog.remember_board_in_session(
            session,
            workspace_id,
            employer_name,
            board_url,
            enabled=enabled,
            origin=origin,
            correlation_id=correlation_id,
        )

    def remember_guided_board_in_session(
        self,
        session: Session,
        workspace_id: str,
        employer_name: str,
        board_url: str,
        *,
        enabled: bool,
        correlation_id: str | None = None,
    ) -> str | None:
        """Add a model-proposed source without changing a person-maintained source."""
        return self.catalog.remember_guided_board_in_session(
            session,
            workspace_id,
            employer_name,
            board_url,
            enabled=enabled,
            correlation_id=correlation_id,
        )

    def list_refusals(self, workspace_id: str, *, limit: int = 50) -> list[PostingRefusalResponse]:
        """Return the postings a stated condition refused, most recent first."""
        return self.queries.list_refusals(workspace_id, limit=limit)

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
        """Return a filtered, bounded page of recorded scans with source settings."""
        return self.queries.list_scan_history(
            workspace_id,
            offset=offset,
            limit=limit,
            source_id=source_id,
            status=status,
            latest_only=latest_only,
        )

    def list_scan_outcomes(
        self,
        workspace_id: str,
        *,
        task_id: str,
        filters: ScanOutcomeFilters | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> SourceScanOutcomePageResponse:
        """Return one task's queryable board-screening record and its coverage state."""
        return self.queries.list_scan_outcomes(
            workspace_id, task_id=task_id, filters=filters, offset=offset, limit=limit
        )

    def export_scan_outcomes(
        self,
        workspace_id: str,
        *,
        task_id: str,
        filters: ScanOutcomeFilters | None = None,
    ) -> Generator[SourceScanOutcomeResponse, None, None]:
        """Stream one task's filtered candidate ledger."""
        return self.queries.export_scan_outcomes(workspace_id, task_id=task_id, filters=filters)

    def list_screened_out_titles(
        self,
        workspace_id: str,
        *,
        needs_review: bool | None = None,
        search: str = "",
        source: str = "",
        offset: int = 0,
        limit: int = 100,
    ) -> ScreenedOutTitlePageResponse:
        """Return screened-out titles across every scan, with how many need review."""
        with self.database.session() as session:
            require_workspace_in(session, workspace_id)
            return list_screened_out_titles(
                session,
                workspace_id,
                needs_review=needs_review,
                search=search,
                source=source,
                offset=offset,
                limit=limit,
            )

    def mark_screened_out_titles_reviewed(
        self, workspace_id: str, ids: Sequence[str]
    ) -> ScreenedOutTitleReviewResponse:
        """Mark screened-out titles reviewed until their title finding changes."""
        with self.database.session() as session:
            require_workspace_in(session, workspace_id)
            return mark_screened_out_titles_reviewed(session, workspace_id, ids)

    def list_pending_discoveries(
        self,
        workspace_id: str,
        *,
        limit: int = 50,
        closed_only: bool = False,
        ready_limit: int | None = None,
        attention_limit: int | None = None,
    ) -> list[PendingDiscoveryResponse]:
        """Return the review queue, with closed history only when requested."""
        return self.queries.list_pending_discoveries(
            workspace_id,
            limit=limit,
            closed_only=closed_only,
            ready_limit=ready_limit,
            attention_limit=attention_limit,
        )

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
        """Return independently bounded ready and attention lanes of the filtered queue."""
        return self.queries.pending_discovery_page(
            workspace_id,
            ready_offset=ready_offset,
            attention_offset=attention_offset,
            limit=limit,
            expected_snapshot_sha256=expected_snapshot_sha256,
            snapshot_before=snapshot_before,
            filters=filters,
        )

    def pending_discovery_changes(
        self,
        workspace_id: str,
        *,
        snapshot_before: datetime,
        filters: PendingQueueFilters | None = None,
    ) -> PendingDiscoveryChangesResponse:
        """Return filtered newer-row and publisher follow-up counts since a page snapshot."""
        return self.queries.pending_discovery_changes(
            workspace_id, snapshot_before=snapshot_before, filters=filters
        )

    def title_exclusion_collateral(
        self, workspace_id: str, phrases: Sequence[str]
    ) -> TitleExclusionCollateralResponse:
        """Return how many current queue rows each title-skip phrase would also hide."""
        return self.queries.title_exclusion_collateral(workspace_id, phrases)

    def derived_titles(self, workspace_id: str) -> list[TargetTitle]:
        """Return suggestions allowed by the latest workspace decisions."""
        return self.queries.derived_titles(workspace_id)

    def derived_titles_in_session(self, session: Session, workspace_id: str) -> list[TargetTitle]:
        """Return workspace-effective accepted suggestions in the caller's transaction."""
        return self.queries.derived_titles_in_session(session, workspace_id)

    def completed_guided_web_runs(self, workspace_id: str) -> int:
        """Count finished hosted searches so the next plan starts where earlier ones did not."""
        return self.queries.completed_guided_web_runs(workspace_id)


class SourceScanHandler:
    """Worker adapter for one board scan."""

    def __init__(
        self,
        database: Database,
        *,
        storage: RuntimeStorage,
        posting_fetcher: PostingFetcher,
        content_resolution: PostingContentResolutionService | None = None,
        fetcher: SourceReader | None = None,
        workday_detail_checker: Callable[[str], PostingCheck] | None = None,
        http_client: PoliteHttpClient | None = None,
        reader_alive: Callable[[int, str], bool | None] | None = None,
    ) -> None:
        """Configure the scan service with its network seam."""
        self.service = SourceScanService(
            database,
            storage=storage,
            fetcher=fetcher,
            posting_fetcher=posting_fetcher,
            content_resolution=content_resolution,
            workday_detail_checker=workday_detail_checker,
            http_client=http_client,
            reader_alive=reader_alive,
        )

    def __call__(self, context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Sweep this task's derived employer boards with checkpoints."""
        value = SourceScanPayload.model_validate(payload)
        task = context.queue.get(context.task_id)
        return self.service.scan(str(task.workspace_id), context, source_ids=value.source_ids)


def queue_rescreen_for_saved_parameters(
    tasks: TaskQueueService, saved: SearchParametersSavedResponse, *, correlation_id: str | None
) -> None:
    """Queue the free whole-queue re-screen once for each new parameter version."""
    if saved.unchanged:
        return
    tasks.enqueue(
        workspace_id=saved.workspace_id,
        task_type="discovery.rescreen_queue",
        payload={},
        idempotency_key=f"search_parameters.saved:{saved.id}",
        origin="search_parameters.saved",
        correlation_id=correlation_id,
    )


class PendingQueueRescreenHandler:
    """Re-screen the whole active pending queue from stored facts, one row at a time."""

    def __init__(self, service: SourceScanService) -> None:
        """Reuse the scan service's sweep, which owns the screening rules."""
        self.service = service

    def __call__(self, context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Judge each visible row with per-row cancellation and progress."""
        DiscoveryRescreenQueuePayload.model_validate(payload)
        task = context.queue.get(context.task_id)
        return self.service.sweep.rescreen.rescreen_pending_queue(
            str(task.workspace_id),
            context,
            actor="worker",
            origin="discovery.rescreen_queue.structured_holdout",
        ).as_result()


# A read that fails on one row leaves the rest of the selection refreshable.
_ROW_FAILURES = (TailorcvError, FetchPolicyError, httpx.HTTPError)


def _count_closed_once(
    pending_id: str, closed: bool, withdrawn: list[str], *other_buckets: list[str]
) -> None:
    """Count a row the body read closed as withdrawn, and in no other bucket."""
    if not closed:
        return
    withdrawn.append(pending_id)
    for bucket in other_buckets:
        if pending_id in bucket:
            bucket.remove(pending_id)


class DiscoveryRefreshBatchHandler:
    """Refresh publisher facts, liveness, and posting text for selected discoveries."""

    def __init__(
        self, service: SourceScanService, content: PostingContentResolutionService
    ) -> None:
        """Reuse the free per-row publisher check and the deterministic content read."""
        self.service = service
        self.content = content

    def __call__(self, context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Refresh each selected row sequentially with per-row checkpoints."""
        value = DiscoveryRefreshBatchPayload.model_validate(payload)
        task = context.queue.get(context.task_id)
        workspace_id = str(task.workspace_id)
        refreshed: list[str] = []
        withdrawn: list[str] = []
        not_checkable: list[str] = []
        unverified: list[str] = []
        failures: list[dict[str, str]] = []
        content_failures: list[dict[str, str]] = []
        content_outcomes: dict[str, int] = {}
        content_kept = 0
        on_board: list[dict[str, str]] = []
        # A whole-site posting feed answers for every row on that site in this pass.
        with url_ingest.shared_posting_feeds():
            for position, pending_id in enumerate(value.pending_discovery_ids, start=1):
                context.raise_if_cancelled()
                context.progress(
                    "refreshing",
                    {
                        "current": position,
                        "total": len(value.pending_discovery_ids),
                        PostingRefreshOutcome.REFRESHED.value: len(refreshed),
                        PostingRefreshOutcome.WITHDRAWN_AS_CLOSED.value: len(withdrawn),
                        PostingRefreshOutcome.UNVERIFIED.value: len(unverified),
                        "pending_discovery_id": pending_id,
                    },
                )
                try:
                    result = self.service.refresh_pending_discovery(
                        workspace_id,
                        pending_id,
                        actor="worker",
                        origin="discovery.refresh_batch",
                        correlation_id=context.task_id,
                    )
                except UnsupportedPublisherRefreshError:
                    not_checkable.append(pending_id)
                except PublisherUnverifiedRefreshError:
                    # The publisher answered without confirming the row, so the stored
                    # facts stand and the body read still decides whether it is gone.
                    unverified.append(pending_id)
                except _ROW_FAILURES as error:
                    failures.append(pending_row_failure(pending_id, error))
                    continue
                else:
                    # A refresh that reads the publisher as closed withdraws the row, and a
                    # withdrawn row is no longer a content target.
                    if result.withdrawn_at is not None:
                        withdrawn.append(pending_id)
                        continue
                    refreshed.append(pending_id)
                try:
                    status, closed, kept = self._read_content(workspace_id, pending_id, context)
                except _ROW_FAILURES as error:
                    # The publisher check already placed this row in one outcome, so the
                    # read error is reported beside the content statuses, not as a second one.
                    content_failures.append(pending_row_failure(pending_id, error))
                else:
                    content_outcomes[status] = content_outcomes.get(status, 0) + 1
                    content_kept += int(kept)
                    _count_closed_once(
                        pending_id, closed, withdrawn, refreshed, not_checkable, unverified
                    )
                    if not closed and status != PostingContentAccessStatus.READABLE.value:
                        holder = self.service.note_registered_board_holder(workspace_id, pending_id)
                        if holder is not None:
                            on_board.append(
                                {
                                    "pending_discovery_id": pending_id,
                                    "holder_type": holder.entity_type,
                                    "holder_id": holder.entity_id,
                                    "holder_url": holder.url,
                                }
                            )
                context.raise_if_cancelled()
        readable = content_outcomes.get(PostingContentAccessStatus.READABLE.value, 0)
        buckets: dict[PostingRefreshOutcome, list[str]] = {
            PostingRefreshOutcome.REFRESHED: refreshed,
            PostingRefreshOutcome.WITHDRAWN_AS_CLOSED: withdrawn,
            PostingRefreshOutcome.NOT_CHECKABLE: not_checkable,
            PostingRefreshOutcome.UNVERIFIED: unverified,
        }
        # not_checkable and unverified state what the publisher check could tell, so they
        # are read with content_outcomes rather than as a verdict on the row.
        return {
            "selected": len(value.pending_discovery_ids),
            **{name.value: len(ids) for name, ids in buckets.items()},
            **{f"{name.value}_ids": ids for name, ids in buckets.items()},
            PostingRefreshOutcome.FAILED.value: len(failures),
            "failures": failures,
            "content_readable": readable,
            "content_unreadable": sum(content_outcomes.values()) - readable,
            "content_outcomes": content_outcomes,
            # Readable rows whose re-read failed and left the earlier body current.
            "content_kept": content_kept,
            "content_failures": content_failures,
            # A row here also sits in its refresh bucket; the holder is left as it is.
            "tracked_on_registered_board": len(on_board),
            "tracked_on_registered_board_holders": on_board,
        }

    def _read_content(
        self,
        workspace_id: str,
        pending_id: str,
        context: TaskContext,
    ) -> tuple[str, bool, bool]:
        """Re-read one row's posting body; return the status it shows, whether it closed or kept.

        A failed re-read that kept the row's readable body reports readable. The
        content read honors cancellation through the task context while this
        handler keeps reporting the one refresh stage a row belongs to.
        """
        preflight = self.content.preflight(workspace_id, [pending_id])
        outcome = self.content.resolve_batch(
            workspace_id,
            [pending_id],
            preflight.snapshot_sha256,
            context,
            origin="discovery.refresh_batch",
            report_progress=False,
        )
        if pending_id in outcome["kept_ids"]:
            return PostingContentAccessStatus.READABLE.value, False, True
        status = next(iter(outcome["outcomes"]), PostingContentAccessStatus.NOT_YET_READ.value)
        return status, pending_id in outcome["withdrawn_ids"], False
