"""The source.continue task: read one bounded chunk and record what it found."""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Sequence
from threading import RLock
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from tailorcv.application.discovery.scan_staging import StagingBusyError, queue_absence_checks
from tailorcv.application.discovery.source_continuation.alignerr import selected_alignerr_pages
from tailorcv.application.discovery.source_continuation.chunk import (
    Chunk,
    ChunkAbandonedError,
    ChunkRecord,
    stalled_count,
)
from tailorcv.application.discovery.source_continuation.commands import SourceContinuationCommands
from tailorcv.application.discovery.source_continuation.coverage import (
    credit_listing_pages,
    retain_declared_read,
    retain_discovery_truncation,
    retain_paged_listing_counts,
    settle_drained_continuation,
)
from tailorcv.application.discovery.source_continuation.detail_checkpoints import (
    retain_detail_coverage,
)
from tailorcv.application.discovery.source_continuation.family import (
    current_scan_family_ids,
    fresh_root_task,
    is_current_in_session,
    next_pending,
)
from tailorcv.application.discovery.source_continuation.frontier_recording import chunk_listed_read
from tailorcv.application.discovery.source_continuation.frontier_refresh import (
    follow_up_target,
    refresh_classification_hold,
    refresh_declared_page_scope,
    refresh_declared_search_detail,
    update_deferred_frontier_row,
)
from tailorcv.application.discovery.source_continuation.limits import POSTING_DETAIL_COMPLETED
from tailorcv.application.discovery.source_continuation.phenom import (
    preserve_unreadable_phenom_retry,
)
from tailorcv.application.discovery.source_continuation.publisher_targets import (
    fresh_search_detail_retry_ids,
    publisher_read_root,
)
from tailorcv.application.discovery.source_continuation.rescreen import (
    completed_posting_reads,
    read_after,
    rescreen_cutoff,
)
from tailorcv.application.discovery.source_continuation.targets import (
    frontier_item,
    frontier_target,
    owner_source_id,
    retry_status,
)
from tailorcv.application.discovery.source_continuation.workday import (
    rebase_workday_listing_coverage,
)
from tailorcv.application.discovery.source_lifecycle import (
    employer_name_is_authoritative,
    source_read_address,
)
from tailorcv.application.discovery.source_reading import (
    SourceContinuationTarget,
    SourceDetailCoverage,
    SourceDetailSummary,
    SourceRead,
    SourceReadOptions,
)
from tailorcv.application.failures import ConflictError, NotFoundError, ValidationError
from tailorcv.application.tasks import TaskContext
from tailorcv.domain import SourceReadOutcome, utc_now
from tailorcv.domain.identity import normalize_fetch_url
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    Employer,
    Source,
    SourceScan,
    SourceScanContinuation,
    SourceScanFrontierItem,
)
from tailorcv.infrastructure.persistence.models.discovery import SourceScanMember
from tailorcv.infrastructure.web_fetch.board_reads import (
    NATIVE_LISTED_PLATFORMS,
    publisher_search_scope,
)

if TYPE_CHECKING:
    from tailorcv.application.discovery.funnel_audit import FunnelOutcome
    from tailorcv.application.discovery.scan_sweep import BoardSweep
    from tailorcv.application.discovery.source_reading import SourceContinuationReader


logger = logging.getLogger(__name__)


CONTINUATION_CHUNK_SIZE = 20


class SourceContinuationHandler:
    """Consume one bounded frontier chunk without losing partial progress."""

    def __init__(
        self,
        database: Database,
        reader: SourceContinuationReader,
        sweep: BoardSweep,
        outcome_recorder: Callable[[str, str, Sequence[FunnelOutcome]], None] | None = None,
        begin_recording_pass: Callable[[str], None] | None = None,
        *,
        time_limit_seconds: float = math.inf,
        clock: Callable[[], float] = time.monotonic,
        reader_alive: Callable[[int, str], bool | None] | None = None,
    ) -> None:
        """Bind the handler to its aggregate store, reader, and shared sweep policy.

        A chunk waits for its staging turn only until `time_limit_seconds` after it began.
        """
        self.database = database
        self.reader = reader
        self.sweep = sweep
        self.outcome_recorder = outcome_recorder
        self.begin_recording_pass = begin_recording_pass
        self.time_limit_seconds = time_limit_seconds
        self.clock = clock
        self.commands = SourceContinuationCommands(database, reader_alive=reader_alive)
        # Serializes a task thread's recording against the worker recovering that task.
        self._lock = RLock()
        self._running: dict[str, Chunk] = {}

    def _admitted_member(
        self,
        continuation_id: str,
        generation: int,
    ) -> SourceScanMember | None:
        with self.database.session() as session:
            continuation = session.get(SourceScanContinuation, continuation_id)
            if continuation is None or not is_current_in_session(session, continuation, generation):
                return None
            member = (
                session.get(SourceScanMember, continuation.batch_member_id)
                if continuation.batch_member_id is not None
                else None
            )
            if member is None:
                raise ValidationError("The source continuation has no admitted ownership.")
            return member

    def __call__(self, context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Consume one pending chunk and enqueue the next while work remains."""
        deadline = self.clock() + self.time_limit_seconds
        # A retry keeps its task id, so this invocation is the only boundary that
        # tells the recorder one attempt from the next.
        if self.begin_recording_pass is not None:
            self.begin_recording_pass(context.task_id)
        continuation_id = payload.get("continuation_id")
        if not isinstance(continuation_id, str) or not continuation_id:
            raise ValidationError("source.continue requires a continuation_id.")
        generation = payload.get("generation")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise ValidationError("source.continue requires a positive generation.")
        include_policy_deferred = payload.get("include_policy_deferred", False)
        if not isinstance(include_policy_deferred, bool):
            raise ValidationError("source.continue include_policy_deferred must be boolean.")
        no_progress_count = payload.get("no_progress_count", 0)
        if (
            not isinstance(no_progress_count, int)
            or isinstance(no_progress_count, bool)
            or no_progress_count < 0
        ):
            raise ValidationError(
                "source.continue no_progress_count must be a non-negative integer."
            )
        continuation, source, employer, items, current = self._load(
            continuation_id, generation, include_policy_deferred=include_policy_deferred
        )
        task = context.queue.get(context.task_id)
        if task.workspace_id != continuation.workspace_id:
            raise ValidationError("Source continuation does not belong to this task workspace.")
        if not current:
            return self._counts(continuation_id)
        member = self._admitted_member(continuation.id, generation)
        if member is None:
            return self._counts(continuation_id)
        attempt_count = getattr(context, "attempt_count", task.attempt_count)
        chunk = Chunk(
            continuation.id,
            continuation.workspace_id,
            generation,
            items,
            include_policy_deferred=include_policy_deferred,
            no_progress_count=no_progress_count,
            member_id=member.id,
            attempt_count=attempt_count,
            lock=self._lock,
            source_live=lambda: self.commands.batch_commands.source_read_is_live(
                member.id, context.task_id, attempt_count, generation
            ),
            write_guard=lambda session: self.commands.batch_commands.require_source_read_in_session(
                session, member.id, context.task_id, attempt_count, generation
            ),
        )
        try:
            claimed = self.commands.batch_commands.claim_source_read(
                member.id,
                context.task_id,
                attempt_count,
                process_id=getattr(context, "process_id", None),
                process_started_at=getattr(context, "process_started_at", None),
            )
        except OperationalError as exc:
            self._record_interruption(context, chunk, exc)
            raise
        if not claimed:
            raise ConflictError("This source is already in progress.")
        with self._lock:
            self._running[context.task_id] = chunk
        try:
            if not items:
                return self._counts(continuation_id)
            return self._consume(
                context, chunk, continuation, source, employer, lambda: deadline - self.clock()
            )
        except Exception as exc:
            self._record_interruption(context, chunk, exc)
            raise
        finally:
            with self._lock:
                self._running.pop(context.task_id, None)
            self.commands.batch_commands.release_source_read(
                member.id, context.task_id, attempt_count
            )

    def recover_abandoned(self, task_id: str) -> None:
        """Charge and re-arm the chunk of a task the worker abandoned at its time limit."""
        with self._lock:
            chunk = self._running.get(task_id)
            if chunk is None or chunk.recorded:
                return
            chunk.abandoned = True
            error = "The continuation task ran past its time limit and was abandoned."
            self._record_outcomes(
                chunk,
                ChunkRecord(
                    (),
                    (),
                    frozenset(),
                    task_id,
                    failures=dict.fromkeys((item.id for item in chunk.items), error),
                ),
            )

    def _consume(
        self,
        context: TaskContext,
        chunk: Chunk,
        continuation: SourceScanContinuation,
        source: Source,
        employer: str,
        remaining_seconds: Callable[[], float],
    ) -> dict[str, Any]:
        continuation_id, items = chunk.continuation_id, chunk.items
        context.progress(
            "reading_source_frontier",
            {
                **self._counts(continuation_id),
                "continuation_id": continuation_id,
                "count": len(items),
            },
        )
        context.raise_if_cancelled()
        root_url = publisher_read_root(
            items, source_read_address(source.url, source.resolved_url)[0]
        )
        handoff_scopes = tuple(
            (host, kind, detail) for host, kind, detail in continuation.handoff_scopes
        )
        with self.database.session() as session:
            member = session.get(SourceScanMember, continuation.batch_member_id)
            fresh_root = fresh_root_task(session, member.root_task_id if member else None)
            fresh_detail_retry_ids = fresh_search_detail_retry_ids(session, continuation, items)
        read, failures = self._read(
            employer,
            items,
            root_url=root_url,
            handoff_scopes=handoff_scopes,
            employer_is_authoritative=employer_name_is_authoritative(source.origin),
            manual_detail_retry=chunk.include_policy_deferred,
            fresh_detail_retry_ids=fresh_detail_retry_ids,
            options=SourceReadOptions(
                fresh_details=fresh_root is not None,
                protected_posting_urls=self.sweep.staging.protected_posting_urls(
                    chunk.workspace_id
                ),
            ),
        )
        context.raise_if_cancelled()
        if not self._is_current(continuation_id, chunk.generation):
            return self._counts(continuation_id)
        workspace_id = chunk.workspace_id
        staged: dict[str, Any] = {}
        detail = read.detail
        chunk.detail = detail
        chunk.protected_posting_urls = read.protected_posting_urls

        def record_detail(enriched: SourceDetailSummary) -> None:
            nonlocal detail
            detail = detail.combined(enriched)
            chunk.detail = detail

        def record_outcomes(outcomes: Sequence[FunnelOutcome]) -> None:
            if self.outcome_recorder is None:
                return
            # Held across the write so recovery cannot abandon the chunk between check and record.
            with chunk.lock:
                if not chunk.abandoned:
                    self.outcome_recorder(workspace_id, context.task_id, outcomes)

        def staging_wait() -> float:
            # The sweep asks for its wait before each staging write, so that is where it stops.
            chunk.require_live()
            return remaining_seconds()

        def require_source_read(session: Session) -> None:
            # Audit writes take the handler lock before SQLite; this guard already holds SQLite.
            if chunk.abandoned:
                raise ChunkAbandonedError
            if chunk.write_guard is not None:
                chunk.write_guard(session)

        deferred_targets = read.deferred_targets

        def record_read(enriched: SourceRead) -> None:
            nonlocal read, deferred_targets
            read = enriched
            deferred_targets = enriched.deferred_targets

        listing_coverage = None
        staging_succeeded = False
        try:
            chunk.require_live()
            staged = self.sweep.stage_continuation(
                workspace_id,
                source,
                employer,
                read,
                context,
                outcome_sink=record_outcomes,
                detail_sink=record_detail,
                read_sink=record_read,
                halted=lambda: not chunk.is_live() or context.cancellation_requested(),
                remaining_seconds=staging_wait,
                require_live=chunk.require_live,
                require_source_read=require_source_read,
            )
            listing_coverage = read.listing_coverage
            staging_succeeded = True
        except (OperationalError, StagingBusyError, ChunkAbandonedError):
            raise
        except Exception as exc:  # noqa: BLE001 - a staging defect is charged to its rows
            context.raise_if_cancelled()
            deferred_targets = ()
            unstaged = self._attempted_ids(
                items, {item.normalized_url for item in items}, read.attempted_target_urls
            )
            failures.update(dict.fromkeys(unstaged, _failure_text("staging", exc)))
        posting_urls = frozenset(
            normalized
            for listing in read.listings
            if (normalized := normalize_fetch_url(listing.url))
        )
        counts = self._record_once(
            chunk,
            ChunkRecord(
                deferred_targets,
                read.attempted_target_urls,
                posting_urls,
                context.task_id,
                failures=failures,
                detail=detail,
                detail_coverage=read.detail_coverage if not failures else None,
                recorded_read=read
                if staging_succeeded and (not failures or read.detail_read_urls is not None)
                else None,
                listing_coverage=(
                    listing_coverage if not failures and not read.discovery_truncated else None
                ),
                discovery_truncated=read.discovery_truncated,
                read_skip_reasons=read.skip_reasons,
                listing_pages=read.listing_pages,
                # A host that asked the reader to wait says nothing about the links themselves.
                count_no_progress="host_held" not in read.skip_reasons,
            ),
        )
        return {**counts, **staged}

    def _read(
        self,
        employer: str,
        items: list[SourceScanFrontierItem],
        *,
        root_url: str,
        handoff_scopes: tuple[tuple[str, str, str], ...],
        employer_is_authoritative: bool = False,
        manual_detail_retry: bool = False,
        fresh_detail_retry_ids: frozenset[str] = frozenset(),
        options: SourceReadOptions,
    ) -> tuple[SourceRead, dict[str, str]]:
        """Read one chunk; if it raises, read each target alone so only failing rows are charged."""
        try:
            read = self.reader.continue_listings(
                employer,
                tuple(
                    frontier_target(
                        i,
                        manual_detail_retry=manual_detail_retry or i.id in fresh_detail_retry_ids,
                    )
                    for i in items
                ),
                root_url=root_url,
                handoff_scopes=handoff_scopes,
                employer_is_authoritative=employer_is_authoritative,
                options=options,
            )
            read = preserve_unreadable_phenom_retry(items, read)
            return read, self._read_failures(items, read)
        except Exception as exc:  # noqa: BLE001 - reader failures are charged to their rows
            if len(items) == 1:
                return merged_read([]), {items[0].id: _failure_text("read", exc)}
        reads: list[tuple[SourceScanFrontierItem, SourceRead]] = []
        failures: dict[str, str] = {}
        for item in items:
            try:
                read = self.reader.continue_listings(
                    employer,
                    (
                        frontier_target(
                            item,
                            manual_detail_retry=manual_detail_retry
                            or item.id in fresh_detail_retry_ids,
                        ),
                    ),
                    root_url=root_url,
                    handoff_scopes=handoff_scopes,
                    employer_is_authoritative=employer_is_authoritative,
                    options=options,
                )
                read = preserve_unreadable_phenom_retry([item], read)
                reads.append((item, read))
                failures.update(self._read_failures([item], read))
            except Exception as exc:  # noqa: BLE001 - reader failures are charged to their rows
                failures[item.id] = _failure_text("read", exc)
        return merged_read(reads), failures

    def _read_failures(
        self, items: list[SourceScanFrontierItem], read: SourceRead
    ) -> dict[str, str]:
        """Charge definitive unreadable results only to frontier rows actually attempted."""
        if read.listings and read.outcome is not SourceReadOutcome.NO_SUPPORTED_REPRESENTATION:
            return {}
        attempted = self._attempted_ids(
            items, {item.normalized_url for item in items}, read.attempted_target_urls
        )
        prefix = "missing_required_posting_identity: "
        if read.outcome is SourceReadOutcome.NO_SUPPORTED_REPRESENTATION:
            return {
                item.id: (
                    prefix + read.note
                    if item.deferred_reason == "phenom_page_pending"
                    else read.note
                    or "The source did not provide a readable posting representation."
                )
                for item in items
                if item.id in attempted
            }
        if read.raw_count == 0 and read.outcome is SourceReadOutcome.PUBLISHER_LINKED_PAGES:
            return {
                item.id: item.last_error
                for item in items
                if item.id in attempted
                and item.deferred_reason == "phenom_page_pending"
                and item.last_error.startswith(prefix)
            }
        return {}

    def _record_interruption(self, context: TaskContext, chunk: Chunk, error: Exception) -> None:
        """Charge and re-arm a chunk whose task stopped; a user cancel stops the chain instead."""
        try:
            if context.cancellation_requested():
                return
            # A database or staging lock says nothing about the publisher links, so they are
            # not charged.
            locked = isinstance(error, (OperationalError, StagingBusyError))
            message = "The continuation task stopped before this read was recorded."
            charged = {} if locked else dict.fromkeys((item.id for item in chunk.items), message)
            self._record_once(
                chunk,
                ChunkRecord(
                    (),
                    (),
                    frozenset(),
                    context.task_id,
                    failures=charged,
                    count_no_progress=not locked,
                    detail=chunk.detail if chunk.is_live() else None,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - the original failure is the one to surface
            logger.warning(
                "source_continuation_rearm_failed",
                extra={
                    "continuation_id": chunk.continuation_id,
                    "task_id": context.task_id,
                    "error_type": type(exc).__name__,
                },
            )

    def _record_once(self, chunk: Chunk, record: ChunkRecord) -> dict[str, int]:
        """Record a chunk from its task thread unless the worker already recovered it."""
        with self._lock:
            if chunk.abandoned:
                return self._counts(chunk.continuation_id)
            counts = self._record_outcomes(chunk, record)
            chunk.recorded = True
            return counts

    def _load(
        self,
        continuation_id: str,
        generation: int,
        *,
        include_policy_deferred: bool,
    ) -> tuple[
        SourceScanContinuation,
        Source,
        str,
        list[SourceScanFrontierItem],
        bool,
    ]:
        with self.database.session() as session:
            continuation = session.get(SourceScanContinuation, continuation_id)
            if continuation is None:
                raise NotFoundError(f"Source continuation {continuation_id!r} does not exist.")
            scan = session.get(SourceScan, continuation.source_scan_id)
            source = session.get(Source, scan.source_id) if scan is not None else None
            if source is None or source.workspace_id != continuation.workspace_id:
                raise NotFoundError("Source continuation owner does not exist.")
            employer = ""
            if source.employer_id:
                employer = (
                    session.scalar(select(Employer.name).where(Employer.id == source.employer_id))
                    or ""
                )
            current = is_current_in_session(session, continuation, generation)
            statement = select(SourceScanFrontierItem).where(
                SourceScanFrontierItem.continuation_id == continuation_id,
                SourceScanFrontierItem.status == "pending",
            )
            if not include_policy_deferred:
                statement = statement.where(SourceScanFrontierItem.automatic_eligible.is_(True))
            page_statement = statement.where(
                SourceScanFrontierItem.deferred_reason.endswith("_search_page", autoescape=True)
            )
            if session.scalar(page_statement.limit(1)) is not None:
                statement = page_statement
            items = list(
                session.scalars(
                    statement.order_by(
                        SourceScanFrontierItem.created_at,
                        SourceScanFrontierItem.normalized_url,
                        SourceScanFrontierItem.id,
                    ).limit(CONTINUATION_CHUNK_SIZE)
                )
            )
            family = (
                current_scan_family_ids(session, continuation)
                if any(
                    str((item.detail_checkpoint or {}).get("platform", "")).startswith(
                        "search:phenom_page:"
                    )
                    for item in items
                )
                else frozenset()
            )
            session.expunge(continuation)
            session.expunge(source)
            for item in items:
                session.expunge(item)
                if (
                    item.deferred_reason in {"phenom_page_pending", "host_held"}
                    and str((item.detail_checkpoint or {}).get("platform", "")).startswith(
                        "search:phenom_page:"
                    )
                    and item.correlation_id not in family
                ):
                    item.detail_checkpoint = None
            return continuation, source, employer, items, current

    @staticmethod
    def _attempted_ids(
        selected: list[SourceScanFrontierItem],
        selected_urls: set[str],
        attempted_target_urls: tuple[str, ...] | None,
    ) -> set[str]:
        """Return the ids of selected rows that reached a GET attempt.

        An adapter that reports no attempt set counts every selected row;
        otherwise only the URLs the adapter actually visited count.
        """
        if attempted_target_urls is None:
            attempted_urls = selected_urls
        else:
            attempted_urls = {
                normalized
                for url in attempted_target_urls
                if (normalized := normalize_fetch_url(url))
            }
        return {item.id for item in selected if item.normalized_url in attempted_urls}

    def _retain_chunk_read_evidence(
        self,
        session: Session,
        continuation: SourceScanContinuation,
        chunk: Chunk,
        posting_urls: frozenset[str],
        deferred_targets: tuple[SourceContinuationTarget, ...],
        recorded_read: SourceRead | None,
        listing_coverage: SourceDetailCoverage | None,
    ) -> None:
        """Retain staged read evidence under the current chunk's write guard."""
        if recorded_read is None and listing_coverage is None:
            return
        if chunk.write_guard is not None:
            chunk.write_guard(session)
        if recorded_read is not None:
            retain_declared_read(session, continuation.source_scan_id, chunk.items, recorded_read)
        if listing_coverage is None:
            return
        if listing_coverage.platform == "alignerr":
            self.commands.retain_listing_coverage_in_session(
                session,
                continuation.id,
                listing_coverage,
                observed_urls=(*posting_urls, *chunk.protected_posting_urls),
                listing_identity_urls=tuple(
                    (listing.requisition_id, listing.url) for listing in recorded_read.listings
                )
                if recorded_read is not None
                else (),
                targets=(
                    *deferred_targets,
                    *selected_alignerr_pages(chunk.items, listing_coverage.board_url),
                ),
            )
        else:
            rebase_workday_listing_coverage(
                session, continuation, chunk, listing_coverage, posting_urls
            )
        session.flush()

    def _retain_chunk_detail_evidence(
        self,
        session: Session,
        continuation: SourceScanContinuation,
        chunk: Chunk,
        correlation_id: str,
        source_id: str,
        detail: SourceDetailSummary | None,
        detail_coverage: SourceDetailCoverage | None,
        recorded_read: SourceRead | None,
    ) -> None:
        """Retain guarded physical detail receipts independently of enumeration."""
        listed_read = chunk_listed_read(session, continuation.id, chunk, recorded_read)
        if detail_coverage is None and (
            (detail is None and listed_read is None) or chunk.member_id is None
        ):
            return
        if chunk.write_guard is not None:
            chunk.write_guard(session)
        if chunk.member_id is not None and (detail is not None or listed_read is not None):
            member = session.get(SourceScanMember, chunk.member_id)
            scan = session.get(SourceScan, continuation.source_scan_id)
            if member is not None and scan is not None:
                self.sweep.recording.record_detail_read_in_session(
                    scan,
                    member,
                    f"{correlation_id}:{chunk.attempt_count}:{chunk.generation}",
                    detail or SourceDetailSummary(),
                    listed_read=listed_read,
                )
        if detail_coverage is not None:
            retain_detail_coverage(session, continuation.source_scan_id, source_id, detail_coverage)

    def _record_outcomes(self, chunk: Chunk, record: ChunkRecord) -> dict[str, int]:
        continuation_id, generation = chunk.continuation_id, chunk.generation
        workspace_id, selected = chunk.workspace_id, chunk.items
        deferred = {
            normalize_fetch_url(target.url): target
            for target in record.deferred_targets
            if normalize_fetch_url(target.url)
        }
        selected_urls = {item.normalized_url for item in selected}
        attempted_ids = self._attempted_ids(selected, selected_urls, record.attempted_target_urls)
        attempted_ids |= record.failures.keys()
        with self.database.exclusive_session() as session:
            continuation = session.get(SourceScanContinuation, continuation_id)
            if continuation is None:
                raise NotFoundError(f"Source continuation {continuation_id!r} does not exist.")
            if not is_current_in_session(session, continuation, generation):
                return self._counts_in_session(session, continuation_id)
            if record.discovery_truncated:
                retain_discovery_truncation(
                    session, chunk, continuation.source_scan_id, record.read_skip_reasons or {}
                )
            if record.listing_pages:
                credit_listing_pages(
                    session, chunk, continuation.source_scan_id, record.listing_pages
                )
            source_id = owner_source_id(session, continuation.source_scan_id)
            self._update_frontier_rows(
                session,
                attempted_ids,
                deferred,
                record.posting_urls,
                record.failures,
                record.correlation_id,
                fresh_detail_retry_ids=fresh_search_detail_retry_ids(
                    session, continuation, selected
                ),
                unattempted_ids=frozenset(item.id for item in selected) - attempted_ids,
            )
            self._complete_protected_rows(session, chunk)
            self._append_new_targets(
                session,
                continuation_id=continuation_id,
                workspace_id=continuation.workspace_id,
                source_id=source_id,
                targets=deferred,
                attempted_urls=selected_urls,
                correlation_id=record.correlation_id,
            )
            session.flush()
            self._retain_chunk_detail_evidence(
                session,
                continuation,
                chunk,
                record.correlation_id,
                source_id,
                record.detail,
                record.detail_coverage,
                record.recorded_read,
            )
            self._retain_chunk_read_evidence(
                session,
                continuation,
                chunk,
                record.posting_urls,
                record.deferred_targets,
                record.recorded_read,
                record.listing_coverage,
            )
            stalled = stalled_count(session, chunk, attempted_ids, record.count_no_progress)
            counts = self._counts_in_session(session, continuation_id)
            next_row = next_pending(session, continuation_id, automatic_only=True)
            if next_row is not None:
                next_id, next_attempt_count = next_row
                self.commands.enqueue_in_session(
                    session,
                    workspace_id,
                    continuation_id,
                    generation,
                    f"auto:{next_id}:{next_attempt_count}:after:{record.correlation_id}",
                    record.correlation_id,
                    include_policy_deferred=False,
                    no_progress_count=stalled,
                    after_task_id=record.correlation_id,
                )
            else:
                scan = session.get(SourceScan, continuation.source_scan_id)
                if scan is not None:
                    retain_paged_listing_counts(session, continuation, scan, chunk)
                settle_drained_continuation(
                    session,
                    continuation,
                    record.correlation_id,
                    check_absent=queue_absence_checks(
                        self.sweep.batch_commands, continuation.workspace_id, record.correlation_id
                    ),
                )
            return counts

    @staticmethod
    def _complete_protected_rows(session: Session, chunk: Chunk) -> None:
        """Settle protected targets without recording a detail read or attempt."""
        protected = {normalize_fetch_url(url) for url in chunk.protected_posting_urls}
        for item in chunk.items:
            if item.normalized_url not in protected:
                continue
            row = session.get(SourceScanFrontierItem, item.id)
            if row is not None:
                row.status = "completed"
                row.deferred_reason = "protected_posting"
                row.last_error = ""

    @staticmethod
    def _update_frontier_rows(
        session: Session,
        attempted_ids: set[str],
        deferred: dict[str, SourceContinuationTarget],
        posting_urls: frozenset[str],
        failures: dict[str, str],
        correlation_id: str,
        *,
        fresh_detail_retry_ids: frozenset[str] = frozenset(),
        unattempted_ids: frozenset[str] = frozenset(),
    ) -> None:
        """Record attempted outcomes and narrow unattempted policy holds without charging them."""
        rows = session.scalars(
            select(SourceScanFrontierItem).where(
                SourceScanFrontierItem.id.in_(attempted_ids | unattempted_ids)
            )
        )
        now = utc_now()
        for row in rows:
            if row.id not in attempted_ids:
                target = deferred.get(row.normalized_url)
                if target is not None:
                    row.automatic_eligible = row.automatic_eligible and target.automatic_eligible
                continue
            row.attempt_count += 1
            if row.id in failures:
                if row.id in fresh_detail_retry_ids:
                    row.automatic_eligible = False
                    row.correlation_id = correlation_id
                row.status = retry_status(row)
                row.last_error = failures[row.id]
                continue
            if row.normalized_url in deferred:
                target = deferred[row.normalized_url]
                update_deferred_frontier_row(
                    row,
                    target,
                    correlation_id,
                    fresh_detail_retry=row.id in fresh_detail_retry_ids,
                )
                continue
            row.status = "completed"
            row.last_error = ""
            row.completed_at = now
            row.deferred_reason = (
                POSTING_DETAIL_COMPLETED if row.normalized_url in posting_urls else ""
            )

    @staticmethod
    def _append_new_targets(
        session: Session,
        *,
        continuation_id: str,
        workspace_id: str,
        source_id: str,
        targets: dict[str, SourceContinuationTarget],
        attempted_urls: set[str],
        correlation_id: str,
    ) -> None:
        """Append targets first surfaced by this read; cap unknown follow-up discoveries."""
        known = set(
            session.scalars(
                select(SourceScanFrontierItem.normalized_url).where(
                    SourceScanFrontierItem.continuation_id == continuation_id
                )
            )
        )
        completed = completed_posting_reads(
            session, source_id, exclude_continuation_id=continuation_id
        )
        recent = read_after(completed, rescreen_cutoff(session, workspace_id, continuation_id))
        for normalized, target in targets.items():
            if (
                (
                    target.deferred_reason
                    in {"alignerr_search_detail", "alignerr_search_page", "phenom_page_pending"}
                    or target.deferred_reason.endswith(("_search_detail", "_search_page"))
                )
                and normalized in known
                and normalized not in attempted_urls
            ):
                row = session.scalar(
                    select(SourceScanFrontierItem).where(
                        SourceScanFrontierItem.continuation_id == continuation_id,
                        SourceScanFrontierItem.normalized_url == normalized,
                    )
                )
                if row is not None:
                    refresh_declared_page_scope(row, target, correlation_id)
                    refresh_classification_hold(row, target)
                    refresh_declared_search_detail(session, source_id, row, target, correlation_id)
            if normalized in attempted_urls or normalized in known or normalized in recent:
                continue
            known.add(normalized)
            # A known posting due for re-screening is finite work, so it stays automatic.
            appended = target if normalized in completed else follow_up_target(target)
            session.add(
                frontier_item(
                    continuation_id,
                    normalized,
                    appended,
                    origin="source.continue",
                    correlation_id=correlation_id,
                )
            )

    def _counts(self, continuation_id: str) -> dict[str, int]:
        with self.database.session() as session:
            return self._counts_in_session(session, continuation_id)

    @staticmethod
    def _counts_in_session(session: Session, continuation_id: str) -> dict[str, int]:
        rows = session.execute(
            select(SourceScanFrontierItem.status, func.count())
            .where(SourceScanFrontierItem.continuation_id == continuation_id)
            .group_by(SourceScanFrontierItem.status)
        )
        counts = {str(status): int(count) for status, count in rows}
        projected = {state: counts.get(state, 0) for state in ("pending", "completed", "failed")}
        projected["policy_deferred"] = int(
            session.scalar(
                select(func.count())
                .select_from(SourceScanFrontierItem)
                .where(
                    SourceScanFrontierItem.continuation_id == continuation_id,
                    SourceScanFrontierItem.status == "pending",
                    SourceScanFrontierItem.automatic_eligible.is_(False),
                )
            )
            or 0
        )
        return projected

    def _is_current(self, continuation_id: str, generation: int) -> bool:
        with self.database.session() as session:
            continuation = session.get(SourceScanContinuation, continuation_id)
            return continuation is not None and is_current_in_session(
                session, continuation, generation
            )


def _failure_text(stage: str, error: Exception) -> str:
    text = " ".join(str(error).split()) or "no message"
    return f"Continuation {stage} raised {type(error).__name__}: {text}"[:500]


def _merged_native_body_urls(
    reads: list[tuple[SourceScanFrontierItem, SourceRead]],
) -> tuple[str, ...] | None:
    """Merge body proof only when every native target names one scope."""
    body_urls = reads[0][1].detail_read_urls if len(reads) == 1 else None
    if len(reads) > 1:
        native_proofs = [
            read.detail_read_urls
            for item, read in reads
            if read.detail_read_urls is not None
            and (
                scope := publisher_search_scope(
                    frontier_target(item), posting_urls=read.detail_read_urls
                )
            )
            is not None
            and scope[0] in NATIVE_LISTED_PLATFORMS
        ]
        scopes = {publisher_search_scope(frontier_target(item)) for item, _read in reads}
        if native_proofs and len(scopes) == 1 and None not in scopes:
            body_urls = tuple(dict.fromkeys(url for proof in native_proofs for url in proof))
    return body_urls


def merged_read(reads: list[tuple[SourceScanFrontierItem, SourceRead]]) -> SourceRead:
    """Combine single-target reads into the read their chunk would have returned."""
    listings = [listing for _item, read in reads for listing in read.listings]
    deferred = tuple(target for _item, read in reads for target in read.deferred_targets)
    skip_reasons: dict[str, int] = {}
    detail = SourceDetailSummary()
    coverage = reads[0][1].detail_coverage if len(reads) == 1 else None
    for _item, read in reads:
        detail = detail.combined(read.detail)
        for reason, count in read.skip_reasons.items():
            skip_reasons[reason] = skip_reasons.get(reason, 0) + count
    return SourceRead(
        listings=listings,
        complete=False,
        raw_count=max(len(listings), sum(read.raw_count for _item, read in reads)),
        outcome=(
            SourceReadOutcome.CRAWL_CONTINUATION
            if deferred
            else SourceReadOutcome.PUBLISHER_LINKED_PAGES
        ),
        skip_reasons=skip_reasons,
        note=" ".join(read.note for _item, read in reads if read.note),
        listing_coverage=reads[0][1].listing_coverage if len(reads) == 1 else None,
        exhausted_outcome=next(
            (read.exhausted_outcome for _item, read in reads if read.exhausted_outcome is not None),
            None,
        )
        if deferred
        else None,
        detail=detail,
        detail_coverage=coverage,
        detail_read_urls=_merged_native_body_urls(reads),
        publisher_listing_receipts=tuple(
            receipt
            for item, read in reads
            if item.deferred_reason.endswith(
                ("_search_page", "_sitemap_pending", "_detail_rows_pending")
            )
            for receipt in read.publisher_listing_receipts
        ),
        discovery_truncated=any(read.discovery_truncated for _item, read in reads),
        listing_pages=sum(read.listing_pages for _item, read in reads),
        deferred_targets=deferred,
        attempted_target_urls=tuple(
            url
            for item, read in reads
            for url in (
                (item.url,) if read.attempted_target_urls is None else read.attempted_target_urls
            )
        ),
        protected_posting_urls=tuple(
            url for _item, read in reads for url in read.protected_posting_urls
        ),
        closed_posting_urls=tuple(url for _item, read in reads for url in read.closed_posting_urls),
    )
