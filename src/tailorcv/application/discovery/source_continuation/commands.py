"""Opening, replacing, and enqueueing a scan's continuation frontier."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from tailorcv.application.discovery.source_continuation.alignerr import (
    alignerr_body_count,
    alignerr_checkpoint_rows,
    rebase_alignerr_checkpoints,
    validated_alignerr_membership,
)
from tailorcv.application.discovery.source_continuation.detail_checkpoints import (
    replace_detail_checkpoints,
)
from tailorcv.application.discovery.source_continuation.family import (
    LATEST_SCAN_FIRST,
    is_current_in_session,
    next_pending,
)
from tailorcv.application.discovery.source_continuation.frontier_carry import (
    advance_generation,
    carried_frontier_item,
    latest_prior_rows,
    reopen_stale_completed_postings,
)
from tailorcv.application.discovery.source_continuation.frontier_recording import (
    record_frontier_targets,
)
from tailorcv.application.discovery.source_continuation.publisher_targets import (
    verified_search_members,
)
from tailorcv.application.discovery.source_continuation.rescreen import rescreen_cutoff
from tailorcv.application.discovery.source_continuation.targets import (
    continuation_frontier_counts,
    targets_by_url,
)
from tailorcv.application.discovery.source_lifecycle import source_read_address
from tailorcv.application.discovery.source_reading import (
    SourceContinuationTarget,
    SourceDetailCheckpoint,
    SourceDetailCoverage,
)
from tailorcv.application.failures import ConflictError, NotFoundError, ValidationError
from tailorcv.application.tasks import TaskQueueService, TaskResponse
from tailorcv.application.tasks.commands import TaskCommands
from tailorcv.application.tasks.records import task_response
from tailorcv.domain import SourceReadOutcome, new_id
from tailorcv.domain.identity import normalize_fetch_url
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    BackgroundTask,
    Source,
    SourceScan,
    SourceScanContinuation,
    SourceScanFrontierItem,
)
from tailorcv.infrastructure.persistence.models.discovery import SourceScanMember

# A scan that ends in one of these read nothing, so its empty frontier declares nothing absent.
_UNREAD_OUTCOMES = frozenset(
    {
        SourceReadOutcome.ACCESS_BLOCKED,
        SourceReadOutcome.RENDER_REQUIRED,
        SourceReadOutcome.SESSION_REQUIRED,
        SourceReadOutcome.POLICY_REFUSED,
        SourceReadOutcome.FETCH_FAILED,
    }
)


@dataclass(frozen=True)
class FrontierPersistResult:
    """What persisting one scan's deferred frontier actually wrote."""

    continuation_id: str | None
    # Rows still awaiting a read, whatever carried or dedup history put them
    # there; zero means nothing remains for this continuation to drain.
    pending_count: int
    # Pending plus failed; neither row was read.
    unresolved_count: int
    # Rows this call actually added, excluding dedup skips and carried rows.
    inserted_count: int
    # Deferred listed postings whose bodies a completed read already holds.
    absorbed_count: int = 0
    # Every deferred listed posting this call reconciled, read or not.
    listed_posting_count: int = 0


def stably_listed_urls(receipts: tuple[SourceDetailCheckpoint, ...]) -> frozenset[str]:
    """Return the posting URLs a stable native listing receipt names."""
    return frozenset(
        normalize_fetch_url(url)
        for receipt in receipts
        if receipt.platform.startswith("listed:") and not receipt.listing_unstable
        for _, url in receipt.listing_urls
    )


class SourceContinuationCommands:
    """Own durable frontier creation and bounded automatic or manual enqueueing."""

    def __init__(
        self, database: Database, *, reader_alive: Callable[[int, str], bool | None] | None = None
    ) -> None:
        """Bind frontier writes and task enqueueing to one database."""
        self.database = database
        self.queue = TaskQueueService(database)
        self.batch_commands = TaskCommands(database, self.queue.events, reader_alive=reader_alive)

    @staticmethod
    def _open_continuation(
        session: Session,
        continuation: SourceScanContinuation | None,
        *,
        workspace_id: str,
        source_scan_id: str,
        prior_rows: list[SourceScanFrontierItem],
        relisted: frozenset[str],
        correlation_id: str | None,
        handoff_scopes: tuple[tuple[str, str, str], ...],
        carry_forward: bool,
        emitted: set[str] | None,
    ) -> SourceScanContinuation:
        """Open this scan's continuation with its carried rows, or advance the existing one."""
        if continuation is not None:
            advance_generation(
                session,
                continuation,
                correlation_id,
                handoff_scopes,
                carry_forward=carry_forward,
                emitted=emitted,
            )
            return continuation
        opened = SourceScanContinuation(
            id=new_id(),
            workspace_id=workspace_id,
            source_scan_id=source_scan_id,
            generation=1,
            created_by="worker",
            origin="source.scan",
            correlation_id=correlation_id,
            handoff_scopes=[list(scope) for scope in handoff_scopes],
        )
        session.add(opened)
        session.flush()
        for row in prior_rows:
            session.add(
                carried_frontier_item(
                    opened.id, row, correlation_id=correlation_id, relisted=relisted
                )
            )
        return opened

    def replace_frontier_in_session(
        self,
        session: Session,
        *,
        workspace_id: str,
        source_scan_id: str,
        targets: tuple[SourceContinuationTarget, ...],
        correlation_id: str | None,
        carry_forward: bool,
        handoff_scopes: tuple[tuple[str, str, str], ...],
        read_urls: tuple[str, ...] = (),
        completed_workday_urls: tuple[str, ...] = (),
        publisher_listing_receipts: tuple[SourceDetailCheckpoint, ...] = (),
    ) -> FrontierPersistResult:
        """Reconcile new targets with recoverable work from the source's prior scan."""
        scan = session.get(SourceScan, source_scan_id)
        source = session.get(Source, scan.source_id) if scan is not None else None
        if source is None or source.workspace_id != workspace_id:
            raise ValidationError("Source continuation does not belong to the scan workspace.")
        continuation = session.scalar(
            select(SourceScanContinuation).where(
                SourceScanContinuation.source_scan_id == source_scan_id
            )
        )
        read_declared_nothing = scan is not None and (
            scan.status == "failed" or scan.read_outcome in _UNREAD_OUTCOMES
        )
        emitted = None if read_declared_nothing else set(targets_by_url(targets))
        prior_rows, prior_day = (
            latest_prior_rows(
                session,
                source.id,
                source_scan_id,
                emitted,
                verified_search_members(publisher_listing_receipts),
            )
            if carry_forward and continuation is None
            else ([], None)
        )
        if continuation is None and not targets and not prior_rows:
            return FrontierPersistResult(None, 0, 0, 0)
        continuation = self._open_continuation(
            session,
            continuation,
            workspace_id=workspace_id,
            source_scan_id=source_scan_id,
            prior_rows=prior_rows,
            relisted=(
                stably_listed_urls(publisher_listing_receipts)
                if scan is not None and prior_day is not None and prior_day < scan.scanned_on
                else frozenset()
            ),
            correlation_id=correlation_id,
            handoff_scopes=handoff_scopes,
            carry_forward=carry_forward,
            emitted=emitted,
        )
        member = session.scalar(
            select(SourceScanMember).where(
                SourceScanMember.source_id == source.id,
                SourceScanMember.root_task_id == correlation_id,
                SourceScanMember.released_at.is_(None),
            )
        )
        if member is not None:
            continuation.batch_member_id = member.id
            member.source_scan_id = source_scan_id
            member.continuation_id = continuation.id
            member.generation = continuation.generation
        cutoff = rescreen_cutoff(session, workspace_id, continuation.id)
        replace_detail_checkpoints(session, continuation.id, targets)
        reopen_stale_completed_postings(
            session, continuation.id, cutoff, source=source, correlation_id=correlation_id
        )
        session.flush()
        inserted_count, absorbed_count, listed_count = record_frontier_targets(
            session,
            continuation.id,
            source.id,
            targets=targets,
            read_urls=read_urls,
            completed_workday_urls=completed_workday_urls,
            publisher_listing_receipts=publisher_listing_receipts,
            cutoff=cutoff,
            correlation_id=correlation_id,
        )
        session.flush()
        next_row = next_pending(session, continuation.id, automatic_only=True)
        if next_row is not None:
            next_id, next_attempt_count = next_row
            self.enqueue_in_session(
                session,
                workspace_id,
                continuation.id,
                continuation.generation,
                f"auto:{next_id}:{next_attempt_count}",
                correlation_id,
                include_policy_deferred=False,
            )
        pending_count, unresolved_count = continuation_frontier_counts(session, continuation.id)
        return FrontierPersistResult(
            continuation.id,
            pending_count,
            unresolved_count,
            inserted_count,
            absorbed_count,
            listed_count,
        )

    @staticmethod
    def retain_listing_coverage_in_session(
        session: Session,
        continuation_id: str | None,
        coverage: SourceDetailCoverage,
        *,
        observed_urls: tuple[str, ...],
        listing_identity_urls: tuple[tuple[str, str], ...] = (),
        targets: tuple[SourceContinuationTarget, ...] = (),
    ) -> tuple[int, int] | None:
        """Rebase exact current Alignerr enumeration after the owning guarded write."""
        if continuation_id is None or coverage.platform != "alignerr":
            return None
        continuation = session.get(SourceScanContinuation, continuation_id)
        scan = session.get(SourceScan, continuation.source_scan_id) if continuation else None
        source = session.get(Source, scan.source_id) if scan else None
        if scan is None or source is None:
            return None
        root = normalize_fetch_url(source_read_address(source.url, source.resolved_url)[0])
        urls = {normalize_fetch_url(url) for url in coverage.posting_urls}
        if root != normalize_fetch_url(coverage.board_url):
            return None
        if not {normalize_fetch_url(url) for url in observed_urls} <= urls:
            return None
        rows = session.scalars(
            select(SourceScanFrontierItem).where(
                SourceScanFrontierItem.continuation_id == continuation_id
            )
        )
        checkpoints = alignerr_checkpoint_rows(rows, root)
        membership = validated_alignerr_membership(
            coverage,
            (),
            targets,
            listing_identity_urls,
        )
        if membership is None:
            return None
        rebase_alignerr_checkpoints(session, checkpoints, membership)
        scan.raw_count = len(membership)
        scan.read_count = alignerr_body_count(session, continuation_id, coverage.posting_urls)
        scan.discovery_truncated = False
        scan.exhausted_outcome = SourceReadOutcome.ATS_COMPLETE
        scan.skip_reasons = {
            reason: count
            for reason, count in scan.skip_reasons.items()
            if reason
            not in {
                "time_limit_reached",
                "publisher_search_request_failed",
                "publisher_listing_total_mismatch",
            }
        }
        session.flush()
        return continuation_frontier_counts(session, continuation_id)

    @staticmethod
    def retain_alignerr_body_count_in_session(
        session: Session,
        continuation_id: str | None,
        posting_urls: tuple[str, ...],
    ) -> int:
        """Retain physical body counts for the current represented listing scope."""
        if continuation_id is None:
            return 0
        continuation = session.get(SourceScanContinuation, continuation_id)
        scan = session.get(SourceScan, continuation.source_scan_id) if continuation else None
        if scan is None:
            return 0
        scan.read_count = alignerr_body_count(session, continuation_id, posting_urls)
        return scan.read_count

    def request_continuation(self, workspace_id: str, source_scan_id: str) -> TaskResponse:
        """Resume the latest scan's retained frontier under an actionable generation."""
        with self.database.exclusive_session() as session:
            scan = session.get(SourceScan, source_scan_id)
            source = session.get(Source, scan.source_id) if scan is not None else None
            if source is None or source.workspace_id != workspace_id:
                raise NotFoundError(f"Source scan {source_scan_id!r} does not exist.")
            continuation = session.scalar(
                select(SourceScanContinuation).where(
                    SourceScanContinuation.source_scan_id == source_scan_id
                )
            )
            if continuation is None:
                raise ValidationError("This scan has no deferred publisher links to read.")
            next_row = next_pending(session, continuation.id, automatic_only=False)
            if next_row is None:
                raise ValidationError("This scan has no deferred publisher links to read.")
            latest = session.scalar(
                select(SourceScan.id)
                .where(SourceScan.source_id == source.id)
                .order_by(*LATEST_SCAN_FIRST)
                .limit(1)
            )
            if latest != source_scan_id:
                raise ValidationError(
                    "This scan is no longer its source's latest; rescan before continuing."
                )
            member = (
                session.get(SourceScanMember, continuation.batch_member_id)
                if continuation.batch_member_id is not None
                else None
            )
            if (
                member is not None
                and member.active_task_id is not None
                and member.cancellation_requested_at is not None
            ):
                raise ConflictError(
                    "This source is already in progress; resume after its reader stops."
                )
            next_id, next_attempt = next_row
            new_owner = member is not None and (
                member.released_at is not None or member.cancellation_requested_at is not None
            )
            if new_owner:
                holder = session.scalar(
                    select(SourceScanMember).where(
                        SourceScanMember.source_id == source.id,
                        SourceScanMember.released_at.is_(None),
                    )
                )
                if holder is not None:
                    raise ConflictError("This source is already in progress.")
                continuation.generation += 1
                continuation.batch_member_id = None
            task = self.enqueue_in_session(
                session,
                workspace_id,
                continuation.id,
                continuation.generation,
                f"manual:{next_id}:{next_attempt}",
                continuation.correlation_id,
                include_policy_deferred=True,
            )
            if task is None:
                raise ValidationError(
                    "This source generation was cancelled; resume its retained work."
                )
            return task

    def enqueue_in_session(
        self,
        session: Session,
        workspace_id: str,
        continuation_id: str,
        generation: int,
        token: str,
        correlation_id: str | None,
        *,
        include_policy_deferred: bool,
        no_progress_count: int = 0,
        after_task_id: str | None = None,
    ) -> TaskResponse | None:
        """Enqueue the next chunk task for a current generation, or return the one queued."""
        continuation = session.get(SourceScanContinuation, continuation_id)
        if continuation is None or not is_current_in_session(session, continuation, generation):
            return None
        active = session.scalar(
            select(BackgroundTask)
            .where(
                BackgroundTask.workspace_id == workspace_id,
                BackgroundTask.task_type == "source.continue",
                BackgroundTask.status.in_(["queued", "running"]),
                BackgroundTask.payload["continuation_id"].as_string() == continuation_id,
                BackgroundTask.payload["generation"].as_integer() == generation,
                BackgroundTask.id != after_task_id,
            )
            .order_by(BackgroundTask.created_at)
            .limit(1)
        )
        if active is not None:
            return task_response(active)
        return self.queue.enqueue_in_session(
            session,
            workspace_id=workspace_id,
            task_type="source.continue",
            payload={
                "continuation_id": continuation_id,
                "generation": generation,
                "include_policy_deferred": include_policy_deferred,
                "no_progress_count": no_progress_count,
            },
            idempotency_key=(f"source-continuation:{continuation_id}:{generation}:{token}"),
            actor="worker",
            origin="source.scan",
            correlation_id=correlation_id,
        )

    def enqueue_pending_in_session(
        self,
        session: Session,
        continuation: SourceScanContinuation,
        *,
        include_policy_deferred: bool,
    ) -> TaskResponse | None:
        """Queue the next retained frontier under its admitted generation."""
        next_row = next_pending(
            session, continuation.id, automatic_only=not include_policy_deferred
        )
        if next_row is None:
            return None
        next_id, next_attempt = next_row
        return self.enqueue_in_session(
            session,
            continuation.workspace_id,
            continuation.id,
            continuation.generation,
            f"resume:{next_id}:{next_attempt}",
            continuation.correlation_id,
            include_policy_deferred=include_policy_deferred,
        )
