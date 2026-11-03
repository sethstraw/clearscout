"""One pass over a workspace's due boards: read, screen, stage, and record each."""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, Literal

import httpx
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from clearscout.application.discovery.content_resolution import (
    CurrentPostingContentReader,
    PostingContentResolutionService,
    PrefetchedContent,
)
from clearscout.application.discovery.dispositions import (
    DiscoveryDispositionService,
)
from clearscout.application.discovery.funnel_audit import (
    DiscoveryFunnelAudit,
    FunnelAuditBuilder,
    FunnelOutcome,
)
from clearscout.application.discovery.scan_queries import SourceScanQueries
from clearscout.application.discovery.scan_staging import (
    DiscoveryStaging,
    StagedListings,
    WithdrawingRead,
    queue_absence_checks,
    shield_progress,
)
from clearscout.application.discovery.scan_sweep.content_evidence import StagedContentEvidence
from clearscout.application.discovery.scan_sweep.listing_audit import (
    alignerr_metadata,
    audit_alignerr_metadata,
    audit_listings,
    prepare_alignerr_enumeration,
    record_source,
)
from clearscout.application.discovery.scan_sweep.listing_screen import (
    SOURCE_STAGE_LIMIT,
    body_screened_listings,
    screen_listings,
)
from clearscout.application.discovery.scan_sweep.models import (
    BoardReadOutcome,
    DueBoards,
    LaneRead,
    ProcessedRead,
    ScanInputs,
    SweepBoard,
)
from clearscout.application.discovery.scan_sweep.outcome import (
    openings_reason,
    source_failure_outcome,
    unreachable_network,
)
from clearscout.application.discovery.scan_sweep.recording import ScanReadContext, ScanRecorder
from clearscout.application.discovery.scan_sweep.rescreen import (
    PendingQueueRescreen,
    screening_snapshot,
)
from clearscout.application.discovery.scan_sweep.workday import enrich_workday_candidates
from clearscout.application.discovery.screening import (
    StructuredHoldout,
)
from clearscout.application.discovery.source_continuation import (
    SourceContinuationCommands,
    fresh_root_task,
    recent_root_posting_reads,
)
from clearscout.application.discovery.source_lifecycle import (
    SourceCatalog,
    employer_name_is_authoritative,
    source_read_address,
)
from clearscout.application.discovery.source_reading import (
    PostingCheck,
    SourceContinuationReader,
    SourceDetailSummary,
    SourceListing,
    SourceRead,
    SourceReader,
    SourceReadOptions,
)
from clearscout.application.failures import NotFoundError, UnavailableError, ValidationError
from clearscout.application.tasks import TaskContext
from clearscout.application.tasks.commands import TaskCommands
from clearscout.application.tasks.events import TaskEventLog
from clearscout.application.workspaces import SearchParametersService
from clearscout.domain import (
    new_id,
    utc_now,
)
from clearscout.domain.identity import url_host
from clearscout.domain.search_parameters import JobSearchParameters
from clearscout.domain.staged_facts import vouched_identity
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    BackgroundTask,
    Employer,
    Source,
    SourceScanContinuation,
    Workspace,
)
from clearscout.infrastructure.persistence.models.discovery import SourceScanMember
from clearscout.infrastructure.web_fetch.board_adapters import identify_board, identify_board_url
from clearscout.infrastructure.web_fetch.ingest.url import (
    guarded_posting_reads,
)
from clearscout.infrastructure.web_fetch.policy import FetchPolicyError

__all__ = ["SCAN_LANES", "SOURCE_STAGE_LIMIT", "BoardSweep"]

logger = logging.getLogger(__name__)


# Boards read at once. Small on purpose: this is one person's job search, and
# the politeness client still serializes whatever shares a host.
SCAN_LANES = 4


def _ordered_scan_rows(rows: list[Any], considered: dict[str, int]) -> list[Any]:
    """Order scan rows by the source order used for this sweep."""
    return sorted(rows, key=lambda row: considered.get(row["source"], len(considered)))


@dataclass
class _BoardOutcomes:
    """Hold one board's funnel outcomes and hand them to the sink when the board is recorded."""

    sink: Callable[[Sequence[FunnelOutcome]], None] | None
    held: list[FunnelOutcome] = field(default_factory=list)
    audit: FunnelAuditBuilder = field(default_factory=FunnelAuditBuilder)

    def record(self, outcome: FunnelOutcome) -> None:
        """Hold one outcome of the board being recorded."""
        self.held.append(outcome)
        self.audit.record(outcome)

    @contextmanager
    def board(self) -> Iterator[None]:
        """Deliver what the board recorded, including the rows before a raised error."""
        self.audit = FunnelAuditBuilder()
        try:
            yield
        except BaseException:
            try:
                self.deliver()
            except Exception:
                # The board's own error is the one the sweep reports.
                logger.exception("source_scan_outcomes_unrecorded")
            raise
        self.deliver()

    def deliver(self) -> None:
        """Hand the held outcomes to the sink in one call."""
        held, self.held = self.held, []
        if self.sink is not None and held:
            self.sink(tuple(held))


class BoardSweep:
    """Run one pass over a workspace's sources."""

    def __init__(
        self,
        database: Database,
        fetcher: SourceReader,
        *,
        parameters: SearchParametersService,
        queries: SourceScanQueries,
        catalog: SourceCatalog,
        staging: DiscoveryStaging,
        content_resolution: PostingContentResolutionService | None = None,
        current_content: CurrentPostingContentReader | None = None,
        posting_checker: Callable[[str], PostingCheck] | None = None,
        reader_alive: Callable[[int, str], bool | None] | None = None,
    ) -> None:
        """Bind one sweep to the durable database and the collaborators it drives."""
        self.database = database
        self.batch_commands = TaskCommands(
            database, TaskEventLog(database), reader_alive=reader_alive
        )
        self.fetcher = fetcher
        self.parameters = parameters
        self.queries = queries
        self.catalog = catalog
        self.staging = staging
        self.content_resolution = content_resolution
        self.posting_checker = posting_checker
        self.continuations = (
            SourceContinuationCommands(database)
            if isinstance(fetcher, SourceContinuationReader)
            else None
        )
        self.evidence = StagedContentEvidence(database, staging, content_resolution)
        self.recording = ScanRecorder(database, self.batch_commands, self.continuations)
        self.rescreen = PendingQueueRescreen(
            database, content_resolution, current_content, self._collect_targets
        )

    def scan(
        self,
        workspace_id: str,
        context: TaskContext | None = None,
        *,
        source_ids: Sequence[str] | None = None,
        board_outcome_sink: Callable[[Sequence[FunnelOutcome]], None] | None = None,
    ) -> dict[str, Any]:
        """Read admitted sources while other selected sources report already in progress."""
        batch_task_id, members = self.batch_commands.open_source_batch(
            workspace_id, source_ids, context.task_id if context is not None else None
        )
        task = self.batch_commands.database
        with task.session() as session:
            root = session.get(BackgroundTask, batch_task_id)
            if root is None:
                raise NotFoundError("The source batch no longer exists.")
            attempt_count = (
                getattr(context, "attempt_count", root.attempt_count)
                if context is not None
                else root.attempt_count
            )
            is_resume = root.payload.get("resume_from_task_id") is not None
            fresh_details = fresh_root_task(session, root.id) is not None
        names = self._batch_source_names(workspace_id, members)
        available: dict[str, SourceScanMember] = {}
        busy: list[dict[str, str]] = []
        resumed: list[dict[str, str]] = []
        recorded: list[dict[str, Any]] = []
        failed = True
        try:
            for member in members:
                disposition = self._prepare_source_member(
                    member, batch_task_id, attempt_count, context, is_resume=is_resume
                )
                if disposition == "read":
                    available[member.source_id] = member
                elif disposition == "recorded":
                    recorded.append(member.result["board_result"])
                else:
                    destination = resumed if disposition == "resumed" else busy
                    destination.append(
                        {"source_id": member.source_id, "source": names[member.source_id]}
                    )
            result = self._scan_batch(
                workspace_id,
                context,
                source_ids=list(available),
                board_outcome_sink=board_outcome_sink,
                batch_task_id=batch_task_id,
                members=available,
                attempt_count=attempt_count,
                fresh_details=fresh_details,
            )
            result = self._merge_scan_results(
                [*recorded, result],
                {name: position for position, name in enumerate(names.values())},
            )
            result["already_in_progress"] = busy
            result["resumed"] = resumed
            result["sources_considered"] += len(busy) + len(resumed)
            failed = False
            return result
        finally:
            for member in available.values():
                self.batch_commands.release_source_read(
                    member.id, batch_task_id, attempt_count, failed=failed
                )
            self.batch_commands.finish_source_batch(
                batch_task_id, direct=context is None, failed=failed
            )

    def _batch_source_names(
        self, workspace_id: str, members: list[SourceScanMember]
    ) -> dict[str, str]:
        if not members:
            return {}
        sources = self._due_boards(workspace_id, [member.source_id for member in members])
        return {
            board.source.id: board.source.name
            for board in [*sources.readable, *(board for board, _ in sources.omitted)]
        }

    def _prepare_source_member(
        self,
        member: SourceScanMember,
        task_id: str,
        attempt_count: int,
        context: TaskContext | None,
        *,
        is_resume: bool,
    ) -> Literal["read", "resumed", "busy", "recorded"]:
        if member.state == "already_in_progress":
            return "busy"
        if context is not None:
            context.raise_if_cancelled()
        if member.root_task_id == task_id and member.result.get("board_result") is not None:
            return "recorded"
        if is_resume and self._resume_source_member(member):
            return "resumed"
        claimed = self.batch_commands.claim_source_read(
            member.id,
            task_id,
            attempt_count,
            process_id=getattr(context, "process_id", None),
            process_started_at=getattr(context, "process_started_at", None),
        )
        return "read" if claimed else "busy"

    def _resume_source_member(self, member: SourceScanMember) -> bool:
        if member.continuation_id is None or self.continuations is None:
            return False
        with self.database.exclusive_session() as session:
            continuation = session.get(SourceScanContinuation, member.continuation_id)
            if continuation is None:
                return False
            follow_up = self.continuations.enqueue_pending_in_session(
                session, continuation, include_policy_deferred=True
            )
            member_row = session.get(SourceScanMember, member.id)
            if member_row is None:
                raise NotFoundError("The source batch member no longer exists.")
            if follow_up is not None:
                member_row.state = "continuing"
            return follow_up is not None

    def _source_write_guard(
        self, member: SourceScanMember, task_id: str, attempt_count: int
    ) -> Callable[[Session], None]:
        def require_source_read(session: Session) -> None:
            self.batch_commands.require_source_read_in_session(
                session, member.id, task_id, attempt_count
            )

        return require_source_read

    def _source_result(
        self,
        outcomes: _BoardOutcomes,
        *,
        processed: ProcessedRead | None = None,
        scanned: dict[str, Any] | None = None,
        failed: dict[str, str] | None = None,
        network_unreachable: bool = False,
    ) -> dict[str, Any]:
        """Bound one released board's contribution without losing its full counts."""
        return {
            "sources_considered": 1,
            "_sources_read": int(processed is not None or failed is not None),
            "_network_unreachable": int(network_unreachable),
            "scanned": [processed.scan_row] if processed else [scanned] if scanned else [],
            "failed": [failed] if failed else [],
            "discoveries": processed.discoveries[:100] if processed else [],
            "withdrawn": processed.withdrawn[:100] if processed else [],
            "new_discoveries": processed.new_count if processed else 0,
            "requeued_discoveries": processed.requeued_count if processed else 0,
            "withdrawn_on_read": processed.withdrawn_on_read_count if processed else 0,
            "funnel": outcomes.audit.build().model_dump(mode="json"),
        }

    def _merge_scan_results(
        self, results: list[dict[str, Any]], considered: dict[str, int]
    ) -> dict[str, Any]:
        """Merge released and freshly read boards without replaying their ledger writes."""
        merged: dict[str, Any] = {}
        for key in ("scanned", "failed", "discoveries", "withdrawn"):
            merged[key] = _ordered_scan_rows(
                [row for result in results for row in result[key]], considered
            )
        for key in (
            "sources_considered",
            "new_discoveries",
            "requeued_discoveries",
            "withdrawn_on_read",
        ):
            merged[key] = sum(result[key] for result in results)
        merged["funnel"] = self._merge_scan_funnels(results)
        sources_read = sum(result["_sources_read"] for result in results)
        unreachable = sum(result["_network_unreachable"] for result in results)
        if sources_read and unreachable == sources_read:
            raise UnavailableError(
                f"The scan failed: no source could be reached over the network "
                f"({merged['failed'][0]['reason']}). Check the connection and scan again."
            )
        merged["discoveries"] = merged["discoveries"][:100]
        merged["withdrawn"] = merged["withdrawn"][:100]
        merged["refresh_failed"] = [
            {"source": row["source"], **failure}
            for row in merged["scanned"]
            for failure in row.get("refresh_failed", [])
        ]
        return merged

    def _merge_scan_funnels(self, results: list[dict[str, Any]]) -> dict[str, Any]:
        """Retain bounded funnel detail with the complete per-source totals."""
        audits = [DiscoveryFunnelAudit.model_validate(result["funnel"]) for result in results]
        detail = FunnelAuditBuilder()
        reason_counts: dict[str, int] = {}
        counts = dict.fromkeys(audits[0].counts.model_dump(), 0)
        for audit in audits:
            for outcome in audit.outcomes:
                detail.record(outcome)
            for reason, count in audit.reason_counts.items():
                reason_counts[reason] = reason_counts.get(reason, 0) + count
            for stage, count in audit.counts.model_dump().items():
                counts[stage] += count
        funnel = detail.build().model_dump(mode="json")
        funnel["counts"] = counts
        funnel["reason_counts"] = dict(sorted(reason_counts.items()))
        funnel["total_outcomes"] = sum(audit.total_outcomes for audit in audits)
        funnel["omitted_outcomes"] = funnel["total_outcomes"] - len(funnel["outcomes"])
        if funnel["omitted_outcomes"] and not funnel["omission_reason"]:
            funnel["omission_reason"] = next(
                audit.omission_reason for audit in audits if audit.omission_reason
            )
        return funnel

    def _scan_batch(
        self,
        workspace_id: str,
        context: TaskContext | None = None,
        *,
        source_ids: Sequence[str] | None = None,
        board_outcome_sink: Callable[[Sequence[FunnelOutcome]], None] | None = None,
        batch_task_id: str,
        members: dict[str, SourceScanMember],
        attempt_count: int,
        fresh_details: bool = False,
    ) -> dict[str, Any]:
        """Sweep the selected sources, or every enabled one; report what each answered.

        The sink receives each board's funnel outcomes together, once per board.
        """
        parameters = self.parameters.current(workspace_id)
        targets = self._collect_targets(workspace_id, parameters)
        stated = any(term.strip() for term in targets)
        due_boards = (
            self._due_boards(workspace_id, source_ids)
            if source_ids
            else DueBoards(readable=[], omitted=[])
        )
        sources = [replace(board, fresh_details=fresh_details) for board in due_boards.readable]
        omitted_boards = due_boards.omitted
        today = utc_now().date()
        scanned: list[dict[str, Any]] = []
        failed: list[dict[str, str]] = []
        discoveries: list[dict[str, str]] = []
        withdrawn: list[dict[str, str]] = []
        board_outcomes = _BoardOutcomes(board_outcome_sink)
        audit = FunnelAuditBuilder(on_record=board_outcomes.record)
        new_total = 0
        requeued_total = 0
        withdrawn_on_read_total = 0
        due = list(sources)
        unreachable = 0
        correlation_id = batch_task_id
        for omitted_board, omit_reason in omitted_boards:
            with board_outcomes.board():
                scanned.append(
                    self.recording.record_omitted(
                        audit,
                        omitted_board,
                        omit_reason,
                        today,
                        correlation_id=correlation_id,
                        require_source_read=self._source_write_guard(
                            members[omitted_board.source.id], batch_task_id, attempt_count
                        ),
                    )
                )
            self.batch_commands.release_source_read(
                members[omitted_board.source.id].id,
                batch_task_id,
                attempt_count,
                result={
                    **scanned[-1],
                    "board_result": self._source_result(board_outcomes, scanned=scanned[-1]),
                },
            )
        inputs = ScanInputs(workspace_id, parameters, targets, stated, correlation_id)
        # Boards are read together and recorded one at a time: the politeness
        # client already serializes each host, and every database write staying
        # on this thread keeps one writer against SQLite.
        for position, outcome in enumerate(self._read(due, context, inputs), start=1):
            require_source_read = self._source_write_guard(
                members[outcome.board.source.id], batch_task_id, attempt_count
            )
            self._notify_progress(context, position, len(due), outcome.board.source.name)
            if outcome.failure is not None:
                unreachable += outcome.network_unreachable
                with board_outcomes.board():
                    failed.append(
                        self.recording.record_failed(
                            audit,
                            outcome,
                            today,
                            correlation_id=correlation_id,
                            require_source_read=require_source_read,
                        )
                    )
                self.batch_commands.release_source_read(
                    members[outcome.board.source.id].id,
                    batch_task_id,
                    attempt_count,
                    result={
                        **failed[-1],
                        "board_result": self._source_result(
                            board_outcomes,
                            failed=failed[-1],
                            network_unreachable=outcome.network_unreachable,
                        ),
                    },
                    failed=True,
                )
                continue
            if outcome.read is None:
                raise RuntimeError("a source read without a failure must include an outcome")
            with board_outcomes.board():
                result = self._process_successful_read(
                    workspace_id,
                    outcome,
                    outcome.read,
                    today,
                    stated,
                    audit,
                    context,
                    correlation_id=batch_task_id,
                    require_source_read=require_source_read,
                )
            self.batch_commands.release_source_read(
                members[outcome.board.source.id].id,
                batch_task_id,
                attempt_count,
                result={
                    **result.scan_row,
                    "board_result": self._source_result(board_outcomes, processed=result),
                },
            )
            scanned.append(result.scan_row)
            discoveries.extend(result.discoveries)
            withdrawn.extend(result.withdrawn)
            new_total += result.new_count
            requeued_total += result.requeued_count
            withdrawn_on_read_total += result.withdrawn_on_read_count
        # Consideration order stabilizes the discovery list; omitted sources go last.
        all_boards = [*sources, *(board for board, _ in omitted_boards)]
        considered = {board.source.name: position for position, board in enumerate(all_boards)}

        return {
            "_sources_read": len(due),
            "_network_unreachable": unreachable,
            "sources_considered": len(all_boards),
            "scanned": _ordered_scan_rows(scanned, considered),
            "failed": _ordered_scan_rows(failed, considered),
            "new_discoveries": new_total,
            "requeued_discoveries": requeued_total,
            "withdrawn": _ordered_scan_rows(withdrawn, considered)[:100],
            "withdrawn_on_read": withdrawn_on_read_total,
            "discoveries": _ordered_scan_rows(discoveries, considered)[:100],
            "refresh_failed": [
                {"source": row["source"], **failure}
                for row in _ordered_scan_rows(scanned, considered)
                for failure in row.get("refresh_failed", [])
            ],
            "funnel": audit.build().model_dump(mode="json"),
        }

    def stage_continuation(
        self,
        workspace_id: str,
        source: Source,
        employer: str,
        read: SourceRead,
        context: TaskContext,
        *,
        outcome_sink: Callable[[Sequence[FunnelOutcome]], None] | None = None,
        detail_sink: Callable[[SourceDetailSummary], None] | None = None,
        read_sink: Callable[[SourceRead], None] | None = None,
        halted: Callable[[], bool] | None = None,
        remaining_seconds: Callable[[], float] = lambda: math.inf,
        require_live: Callable[[], None] = lambda: None,
        require_source_read: Callable[[Session], None] | None = None,
    ) -> dict[str, Any]:
        """Apply the board-sweep screening and staging policy to a partial read.

        Each staging write waits for its turn no longer than `remaining_seconds` says,
        and `require_live` raises to stop the content writes that follow staging.
        """
        parameters = self.parameters.current(workspace_id)
        targets = self._collect_targets(workspace_id, parameters)
        stated = any(term.strip() for term in targets)
        if stated:
            options = SourceReadOptions(
                protected_posting_urls=self.staging.protected_posting_urls(workspace_id)
            )
            with guarded_posting_reads(options.allows_posting):
                read = self._enrich_workday_candidates(
                    read,
                    parameters,
                    targets,
                    halted,
                    source_url=source_read_address(source.url, source.resolved_url)[0],
                    source_employer=employer,
                    detail_sink=detail_sink,
                )
        listings = read.listings
        outcomes = _BoardOutcomes(outcome_sink)
        audit = FunnelAuditBuilder(on_record=outcomes.record)
        wanted, refused, unsupported = screen_listings(
            listings, parameters, targets, source_employer=employer
        )
        unfiltered = bool(employer) and not stated
        read = prepare_alignerr_enumeration(read, wanted, unfiltered=unfiltered)
        if read_sink is not None:
            read_sink(read)
        audit_alignerr_metadata(audit, source.name, listings)
        # Listed Alignerr identities are recorded before staging begins.
        outcomes.deliver()
        wanted = [item for item in wanted if not alignerr_metadata(item)]
        refused = [(item, reason) for item, reason in refused if not alignerr_metadata(item)]
        unsupported = [
            (item, reason) for item, reason in unsupported if not alignerr_metadata(item)
        ]
        new_candidates, refreshable, stage_omitted = self.staging.partition_scan_listings(
            workspace_id, wanted, new_limit=len(wanted)
        )
        eligible = [] if unfiltered else [*new_candidates, *refreshable]
        selected = eligible
        placed = self.staging.stage_listings(
            workspace_id,
            selected,
            utc_now().date(),
            reason=self._staging_reason(source.name, stated=stated),
            source_id=source.id,
            context=context,
            wait_seconds=remaining_seconds(),
            require_source_read=require_source_read,
        )
        resolvable = eligible
        require_live()
        self._retain_published_descriptions(
            workspace_id,
            resolvable,
            placed,
            context,
            origin="source.continue",
            require_source_read=require_source_read,
        )
        closed_ids = self.evidence.record_closed_postings(
            workspace_id,
            source,
            read,
            context,
            origin="source.continue",
            require_source_read=require_source_read,
        )
        require_live()
        resolution = self._resolve_staged(
            workspace_id,
            placed.row_ids(resolvable),
            context,
            origin="source.continue",
            require_source_read=require_source_read,
        )
        body_screened = body_screened_listings(
            self.database, workspace_id, resolvable, placed, origin="source.continue"
        )
        held_out = self._refresh_held_out(
            workspace_id,
            refused,
            source,
            stated=stated,
            unfiltered=unfiltered,
            context=context,
            wait_seconds=remaining_seconds(),
            require_source_read=require_source_read,
        )
        selected_refused = held_out.inserted
        held_out_pending_urls = self._dismiss_known_holdouts(
            workspace_id,
            refused,
            held_out,
            actor="worker",
            origin="source.continue.structured_holdout",
            correlation_id=context.task_id,
            wait_seconds=remaining_seconds(),
            require_source_read=require_source_read,
        )
        audit_listings(
            audit,
            source.name,
            wanted,
            eligible,
            stage_omitted,
            refused,
            selected_refused,
            held_out_pending_urls,
            unsupported,
            placed.queued,
            body_screened=body_screened,
            unfiltered=unfiltered,
        )
        outcomes.deliver()
        return {
            "read": sum(not alignerr_metadata(item) for item in listings),
            "matched": len(wanted),
            "no_title_match": len(unsupported),
            "staged": len(placed.queued),
            "refused": len(refused),
            "content_resolved": resolution["attempted"] + resolution["retained"],
            "withdrawn_on_read": len(closed_ids) + len(resolution["withdrawn_ids"]),
            "funnel": audit.build().model_dump(mode="json"),
        }

    def _collect_targets(self, workspace_id: str, parameters: JobSearchParameters) -> list[str]:
        """Merge stated titles with derived ones; a derived title is still stated."""
        targets = list(parameters.seniority.stated_titles)
        targets.extend(item.title for item in self.queries.derived_titles(workspace_id))
        return targets

    @staticmethod
    def _notify_progress(
        context: TaskContext | None, position: int, total: int, source_name: str
    ) -> None:
        if context is None:
            return
        context.raise_if_cancelled()
        context.progress(
            "scanning",
            {"current": position, "total": total, "source": source_name},
        )

    def _process_successful_read(
        self,
        workspace_id: str,
        outcome: BoardReadOutcome,
        read: SourceRead,
        today: date,
        stated: bool,
        audit: FunnelAuditBuilder,
        context: TaskContext | None,
        *,
        correlation_id: str | None = None,
        require_source_read: Callable[[Session], None] | None = None,
    ) -> ProcessedRead:
        """Stage the survivors its lane screened from one board, and record the outcome."""
        board = outcome.board
        source = board.source
        started_at = outcome.started_at
        completed_at = outcome.completed_at
        lane = outcome.lane
        if lane is None:
            raise RuntimeError("a successful board read must carry its lane's screening")
        listings, detail = lane.listings, lane.detail
        wanted, refused, unsupported = lane.wanted, lane.refused, lane.unsupported
        # An employer board with no stated title is read and reported, not
        # staged: every opening would match.
        unfiltered = bool(board.employer) and not stated
        audit_alignerr_metadata(audit, source.name, listings)
        wanted = [item for item in wanted if not alignerr_metadata(item)]
        refused = [(item, reason) for item, reason in refused if not alignerr_metadata(item)]
        unsupported = [
            (item, reason) for item, reason in unsupported if not alignerr_metadata(item)
        ]
        readable_count = sum(not alignerr_metadata(item) for item in listings)
        new_candidates, refreshable, stage_omitted = self.staging.partition_scan_listings(
            workspace_id, wanted, new_limit=len(wanted)
        )
        eligible = [] if unfiltered else [*new_candidates, *refreshable]
        staged = eligible
        placed = self.staging.stage_listings(
            workspace_id,
            staged,
            today,
            reason=self._staging_reason(source.name, stated=stated),
            source_id=source.id,
            context=context,
            require_source_read=require_source_read,
        )
        # A requeued row is queue work a person still has to decide, so it counts
        # as new here and is reported apart only in the progress payload.
        new_listings = placed.queued
        resolvable = eligible
        self._retain_published_descriptions(
            workspace_id,
            resolvable,
            placed,
            context,
            origin="source.scan",
            require_source_read=require_source_read,
        )
        closed_ids = self.evidence.record_closed_postings(
            workspace_id,
            source,
            read,
            context,
            origin="source.scan",
            require_source_read=require_source_read,
        )
        content_resolution = self._resolve_staged(
            workspace_id,
            placed.row_ids(resolvable),
            context,
            origin="source.scan",
            prefetched=lane.prefetched,
            require_source_read=require_source_read,
        )
        body_screened = body_screened_listings(
            self.database, workspace_id, resolvable, placed, origin="source.scan"
        )
        held_out = self._refresh_held_out(
            workspace_id,
            refused,
            source,
            stated=stated,
            unfiltered=unfiltered,
            context=context,
            require_source_read=require_source_read,
        )
        selected_refused = held_out.inserted
        held_out_pending_urls = self._dismiss_known_holdouts(
            workspace_id,
            refused,
            held_out,
            actor="worker",
            origin="source.scan.structured_holdout",
            correlation_id=context.task_id if context is not None else correlation_id,
            require_source_read=require_source_read,
        )
        audit_listings(
            audit,
            source.name,
            wanted,
            eligible,
            stage_omitted,
            refused,
            selected_refused,
            held_out_pending_urls,
            unsupported,
            new_listings,
            body_screened=body_screened,
            unfiltered=unfiltered,
        )
        read_note = " ".join(note for note in (board.address_note, read.note) if note)
        record_source(
            audit,
            board,
            openings_reason(read, wanted, new_listings),
            fetched=True,
            staged=bool(new_listings),
            detail=read_note,
        )
        gone = (
            self.staging.withdraw_absent(
                workspace_id,
                source.id,
                [*(item.url for item in listings), *read.protected_posting_urls],
                today,
                correlation_id=context.task_id if context is not None else correlation_id,
                require_source_read=require_source_read,
                read=WithdrawingRead(
                    read.raw_count,
                    readable_count,
                    read.complete,
                    read.listing_pages,
                    started_at=started_at,
                ),
                check_absent=queue_absence_checks(
                    self.batch_commands,
                    workspace_id,
                    context.task_id if context is not None else correlation_id,
                ),
            )
            if read.complete
            else []
        )
        merged_skips = dict(read.skip_reasons)
        recorded = self.recording.record_scan(
            source,
            today,
            None,
            len(staged),
            len(new_listings),
            None,
            started_at=started_at,
            completed_at=completed_at,
            read_outcome=read.outcome,
            deferred_targets=read.deferred_targets,
            correlation_id=context.task_id if context is not None else correlation_id,
            require_source_read=require_source_read,
            read_context=ScanReadContext(
                raw_count=read.raw_count,
                read_count=readable_count,
                skip_reasons=merged_skips,
                complete=read.complete,
                detail=detail,
                discovery_truncated=read.discovery_truncated,
                listing_pages=read.listing_pages,
                exhausted_outcome=read.exhausted_outcome,
                detail_coverage=read.detail_coverage,
                listing_coverage=read.listing_coverage,
                publisher_listing_receipts=read.publisher_listing_receipts,
                listing_identity_urls=tuple(
                    (item.requisition_id, item.url) for item in listings if alignerr_metadata(item)
                ),
                matched=len(wanted),
                refused=len(refused),
                unfiltered=unfiltered,
                handoff_scopes=read.handoff_scopes,
                read_note=read_note,
                attempted_target_urls=read.attempted_target_urls or (),
                completed_workday_urls=tuple(
                    item.url
                    for item in listings
                    if item.published_description.strip()
                    and item.source is not None
                    and item.source.publisher == "workday"
                    and item.facts.publisher_vouched
                    and (identity := identify_board_url(item.url)) is not None
                    and identity[0] == "workday"
                ),
                listing_urls=tuple(
                    dict.fromkeys([*(item.url for item in listings), *read.protected_posting_urls])
                ),
            ),
        )
        gone = [*gone, *recorded.withdrawn]
        withdrawn_rows = [
            {"source": source.name, "employer": board.employer, "title": title} for title in gone
        ]
        duration_ms = max(0, int((completed_at - started_at).total_seconds() * 1000))
        scan_row: dict[str, Any] = {
            "source": source.name,
            "raw": read.raw_count,
            "open": readable_count,
            "skip_reasons": read.skip_reasons,
            "complete": read.complete,
            "outcome": recorded.read_outcome.value,
            "note": recorded.note,
            "frontier_pending": recorded.pending_count,
            "matched": len(wanted),
            "refused": len(refused),
            "staged": len(staged),
            "new": len(new_listings),
            "requeued": len(placed.requeued),
            "withdrawn": len(gone),
            "duration_ms": duration_ms,
            "detail": {
                "attempted": detail.attempted,
                "read": detail.read,
                "failed": detail.failed,
                "capped": detail.capped,
            },
            "content_resolution": content_resolution,
            "refresh_failed": placed.refresh_failed,
        }
        logger.info(
            "source_scan",
            extra={
                "source_id": source.id,
                "operation_id": outcome.operation_id,
                "state": "finished",
                "host": url_host(board.url) or "unknown",
                "raw": read.raw_count,
                "read": readable_count,
                "matched": len(wanted),
                "refused": len(refused),
                "new": len(new_listings),
                "withdrawn": len(gone),
                "duration_ms": duration_ms,
                "detail_attempted": detail.attempted,
                "detail_failed": detail.failed,
            },
        )
        discovery_rows = [
            {
                "source": source.name,
                "employer": listing.employer,
                "title": listing.title,
                "location": listing.location,
                "url": listing.url,
            }
            for listing in new_listings
        ]
        return ProcessedRead(
            scan_row=scan_row,
            discoveries=discovery_rows,
            withdrawn=withdrawn_rows,
            new_count=len(new_listings),
            requeued_count=len(placed.requeued),
            withdrawn_on_read_count=len(closed_ids) + len(content_resolution["withdrawn_ids"]),
        )

    def _resolve_staged(
        self,
        workspace_id: str,
        ids: list[str],
        context: TaskContext | None,
        *,
        origin: str,
        prefetched: dict[str, PrefetchedContent] | None = None,
        require_source_read: Callable[[Session], None] | None = None,
    ) -> dict[str, Any]:
        """Persist free current content before a newly staged row reaches the queue."""
        if ids:
            visible = self._visible_addresses(workspace_id, ids)
            ids = [pending_id for pending_id in ids if pending_id in visible]
        if self.content_resolution is None or not ids:
            return {
                "selected": len(ids),
                "attempted": 0,
                "retained": 0,
                "outcomes": {},
                "evidence_ids": [],
                "withdrawn_ids": [],
            }
        options = SourceReadOptions(
            protected_posting_urls=self.staging.protected_posting_urls(workspace_id)
        )
        with guarded_posting_reads(options.allows_posting):
            return self.content_resolution.resolve_automatic(
                workspace_id,
                ids,
                shield_progress(context),
                origin=origin,
                prefetched=prefetched,
                require_source_read=require_source_read,
            )

    def _visible_addresses(self, workspace_id: str, ids: list[str]) -> dict[str, str]:
        """Map active, unprotected rows to their current read addresses."""
        return self.evidence.visible_addresses(workspace_id, ids)

    def _retain_published_descriptions(
        self,
        workspace_id: str,
        listings: list[SourceListing],
        placed: StagedListings,
        context: TaskContext | None,
        *,
        origin: str,
        require_source_read: Callable[[Session], None] | None = None,
    ) -> None:
        """Promote a board-published full description into current content evidence."""
        self.evidence.retain_published_descriptions(
            workspace_id,
            listings,
            placed,
            context,
            origin=origin,
            require_source_read=require_source_read,
        )

    def _enrich_workday_candidates(
        self,
        read: SourceRead,
        parameters: JobSearchParameters,
        targets: list[str],
        halted: Callable[[], bool] | None = None,
        *,
        source_url: str,
        source_employer: str = "",
        detail_sink: Callable[[SourceDetailSummary], None] | None = None,
    ) -> SourceRead:
        """Read eligible Workday details and retain each unresolved identity."""
        return enrich_workday_candidates(
            self.posting_checker,
            read,
            parameters,
            targets,
            halted,
            source_url=source_url,
            source_employer=source_employer,
            detail_sink=detail_sink,
        )

    def _refresh_held_out(
        self,
        workspace_id: str,
        refused: list[tuple[SourceListing, StructuredHoldout]],
        source: Source,
        *,
        stated: bool,
        unfiltered: bool,
        context: TaskContext | None,
        wait_seconds: float = math.inf,
        require_source_read: Callable[[Session], None] | None = None,
    ) -> StagedListings:
        """Refresh the tracked job or known row a held-out listing matches; never stage one.

        An unfiltered board refreshes nothing, so only a row staged at a listed URL is named.
        """
        listings = [item for item, _holdout in refused]
        if not listings:
            return StagedListings([], [])
        if unfiltered:
            return StagedListings(
                [], [], self.staging.row_ids_at_listed_urls(workspace_id, listings)
            )
        return self.staging.stage_listings(
            workspace_id,
            listings,
            utc_now().date(),
            reason=self._staging_reason(source.name, stated=stated),
            source_id=source.id,
            context=context,
            refresh_only=True,
            wait_seconds=wait_seconds,
            require_source_read=require_source_read,
        )

    def _dismiss_known_holdouts(
        self,
        workspace_id: str,
        refused: list[tuple[SourceListing, StructuredHoldout]],
        held_out: StagedListings,
        *,
        actor: str,
        origin: str,
        correlation_id: str | None,
        wait_seconds: float = math.inf,
        require_source_read: Callable[[Session], None] | None = None,
    ) -> set[str]:
        """Append a reversible hold-out only for a visible known pending row."""
        if not held_out.row_ids_by_url:
            return set()
        # Each hold-out rests on the row's markers and latest disposition read here.
        with (
            self.staging.write_turn(wait_seconds),
            self.database.exclusive_session() as session,
        ):
            if require_source_read is not None:
                require_source_read(session)
            dispositions = DiscoveryDispositionService(self.database)
            held_out_urls: set[str] = set()
            for listing, holdout in refused:
                pending_id = held_out.row_ids_by_url.get(listing.url)
                if pending_id is None:
                    continue
                dismissal = dispositions.dismiss_structured_holdout_in_session(
                    session,
                    workspace_id,
                    pending_id,
                    category=holdout.finding.code.value,
                    reason=(
                        "Verified board facts meet a structured search hold-out. "
                        "The dismissal remains individually restorable."
                    ),
                    actor=actor,
                    origin=origin,
                    correlation_id=correlation_id,
                    screening_override=screening_snapshot(holdout.screening),
                )
                # A marked, person-restored, or person-added row is skipped and stays open.
                if dismissal.held_out:
                    held_out_urls.add(listing.url.casefold())
            return held_out_urls

    def remember_if_readable(
        self,
        workspace_id: str,
        employer_name: str,
        careers_url: str,
        *,
        evidence_text: str = "",
        origin: str,
        correlation_id: str | None = None,
    ) -> bool:
        """Verify a supported board before allowing it into the recurring sweep."""
        if identify_board_url(careers_url) is None and identify_board(evidence_text) is None:
            return False
        try:
            self.fetcher.listings(employer_name, careers_url)
            self.catalog.remember_board(
                workspace_id,
                employer_name,
                careers_url,
                origin=origin,
                correlation_id=correlation_id,
            )
        except (httpx.HTTPError, FetchPolicyError, IntegrityError, ValueError):
            return False
        return True

    def _staging_reason(self, source_name: str, *, stated: bool) -> str:
        """State why a published board listing entered pending review."""
        if stated:
            return f"Board scan of {source_name} retained a published listing for review."
        return (
            f"Board scan of {source_name} retained a listing for review with no stated title "
            "filter."
        )

    def _read(
        self, due: list[SweepBoard], context: TaskContext | None, inputs: ScanInputs
    ) -> Iterator[BoardReadOutcome]:
        """Read the due boards a few at a time, yielding each as it answers.

        Each lane also does its board's detail and body reads, so boards on
        different hosts proceed together. One board's failure is its own: it is
        reported against that source and the rest are still read. Per-host
        politeness is the fetch client's, which holds a lock per host, so two
        boards on one host still take their turns.
        """
        if not due:
            return
        stopping = threading.Event()

        def halted() -> bool:
            # A lane polls the task's cancellation itself, between its posting reads.
            if not stopping.is_set() and context is not None and context.cancellation_requested():
                stopping.set()
            return stopping.is_set()

        with ThreadPoolExecutor(max_workers=min(SCAN_LANES, len(due))) as pool:
            pending = {pool.submit(self._read_in_lane, board, inputs, halted) for board in due}
            try:
                for finished in as_completed(pending):
                    # Freed once its board is recorded, rather than held to the sweep's end.
                    pending.discard(finished)
                    if context is not None:
                        context.raise_if_cancelled()
                    outcome = finished.result()
                    if outcome is not None:
                        yield outcome
            except BaseException:
                # Running lanes stop at their next read rather than finishing the board.
                stopping.set()
                pool.shutdown(cancel_futures=True)
                raise

    def _read_in_lane(
        self, board: SweepBoard, inputs: ScanInputs, halted: Callable[[], bool]
    ) -> BoardReadOutcome | None:
        """Read one board, then do the network work its recording needs, writing nothing."""
        if halted():
            return None
        outcome = self._listings(board, inputs.task_id)
        if outcome.read is None or halted():
            return outcome
        read = outcome.read
        if inputs.stated:
            options = SourceReadOptions(
                protected_posting_urls=self.staging.protected_posting_urls(inputs.workspace_id)
            )
            with guarded_posting_reads(options.allows_posting):
                read = self._enrich_workday_candidates(
                    read,
                    inputs.parameters,
                    inputs.targets,
                    halted,
                    source_url=board.url,
                    source_employer=board.employer,
                )
        listings, detail = read.listings, read.detail
        wanted, refused, unsupported = screen_listings(
            listings, inputs.parameters, inputs.targets, source_employer=board.employer
        )
        unfiltered = bool(board.employer) and not inputs.stated
        read = prepare_alignerr_enumeration(read, wanted, unfiltered=unfiltered)
        prefetched = (
            {}
            if unfiltered
            else self._prefetch_bodies(
                inputs, [item for item in wanted if not alignerr_metadata(item)], halted
            )
        )
        return replace(
            outcome,
            read=read,
            lane=LaneRead(listings, detail, wanted, refused, unsupported, prefetched),
        )

    def _prefetch_bodies(
        self, inputs: ScanInputs, wanted: list[SourceListing], halted: Callable[[], bool]
    ) -> dict[str, PrefetchedContent]:
        """Read the body of each wanted listing that will be staged as a new, bodiless row.

        Reading stops at the first raised error, as the caller's own reads would.
        """
        resolution = self.content_resolution
        if resolution is None or not wanted:
            return {}
        new, refreshable, _omitted = self.staging.partition_scan_listings(
            inputs.workspace_id, wanted, new_limit=len(wanted)
        )
        # Staging receives this order, and its requisition matching claims rows in order.
        unstaged = self.staging.unstaged_listings(inputs.workspace_id, [*new, *refreshable])
        options = SourceReadOptions(
            protected_posting_urls=self.staging.protected_posting_urls(inputs.workspace_id)
        )
        reads: dict[str, PrefetchedContent] = {}
        for listing in unstaged:
            if (
                listing.published_description.strip()
                or listing.url in reads
                or not options.allows_posting(listing.url)
            ):
                continue
            if halted():
                break
            title, employer = vouched_identity(
                listing.facts.publisher_vouched, listing.title, listing.employer
            )
            with guarded_posting_reads(options.allows_posting):
                read = resolution.prefetch(
                    inputs.workspace_id,
                    listing.url,
                    employer=employer,
                    title=title,
                    task_id=inputs.task_id,
                )
            reads[listing.url] = read
            if read.failure is not None:
                break
        return reads

    def _listings(self, board: SweepBoard, root_task_id: str | None = None) -> BoardReadOutcome:
        """Return one board's openings, or the reason reading it did not work."""
        started_at = utc_now()
        operation_id = new_id()
        logger.info(
            "source_scan",
            extra={
                "source_id": board.source.id,
                "operation_id": operation_id,
                "state": "started",
                "host": url_host(board.url) or "unknown",
            },
        )
        try:
            read = self.fetcher.listings(
                board.employer,
                board.url,
                employer_is_authoritative=board.employer_is_authoritative,
                options=SourceReadOptions(
                    fresh_details=board.fresh_details,
                    protected_posting_urls=self.staging.protected_posting_urls(
                        board.source.workspace_id
                    ),
                    recent_posting_urls=self._recent_posting_urls(board, root_task_id),
                ),
            )
            return BoardReadOutcome(
                board=board,
                read=read,
                failure=None,
                failure_outcome=None,
                started_at=started_at,
                completed_at=utc_now(),
                operation_id=operation_id,
            )
        except Exception as exc:  # noqa: BLE001 - a changed publisher payload can raise anything
            logger.warning(
                "source_scan",
                extra={
                    "source_id": board.source.id,
                    "operation_id": operation_id,
                    "state": "finished",
                    "host": url_host(board.url) or "unknown",
                    "outcome": "fetch_failed",
                    "error_type": type(exc).__name__,
                },
            )
            known = isinstance(exc, (httpx.HTTPError, FetchPolicyError, ValueError))
            return BoardReadOutcome(
                board=board,
                read=None,
                failure=str(exc) if known else f"{type(exc).__name__}: {exc}",
                failure_outcome=source_failure_outcome(exc),
                started_at=started_at,
                completed_at=utc_now(),
                operation_id=operation_id,
                network_unreachable=unreachable_network(exc),
            )

    def _recent_posting_urls(self, board: SweepBoard, root_task_id: str | None) -> frozenset[str]:
        """Return the posting URLs this board read recently enough that a read may skip them.

        A fresh root's creation time is the cutoff, so a fresh scan skips nothing read before it.
        """
        with self.database.session() as session:
            return recent_root_posting_reads(
                session, board.source.workspace_id, board.source.id, root_task_id
            )

    def _require_scannable(
        self, session: Session, workspace_id: str, source_ids: list[str]
    ) -> None:
        """Refuse a selection naming a source this workspace cannot read right now."""
        selected = {
            source.id: source
            for source in session.scalars(
                select(Source).where(Source.workspace_id == workspace_id, Source.id.in_(source_ids))
            )
        }
        for source_id in source_ids:
            source = selected.get(source_id)
            if source is None:
                raise NotFoundError(f"Unknown source {source_id!r}.")
            if source.archived_at is not None:
                raise ValidationError(f"Source {source.name!r} is retired and cannot be scanned.")
            employer = session.get(Employer, source.employer_id) if source.employer_id else None
            if employer is not None and employer.archived_at is not None:
                raise ValidationError(
                    f"Source {source.name!r} belongs to a retired employer and cannot be scanned."
                )

    def _due_boards(self, workspace_id: str, source_ids: Sequence[str] | None = None) -> DueBoards:
        """Return the sources this sweep reads and those it must skip with a reason."""
        selection = list(dict.fromkeys(source_ids or ()))
        with self.database.session() as session:
            if session.get(Workspace, workspace_id) is None:
                raise NotFoundError(f"Unknown workspace {workspace_id!r}.")
            if selection:
                self._require_scannable(session, workspace_id, selection)
            statement = (
                select(Source, Employer.name)
                .outerjoin(Employer, Employer.id == Source.employer_id)
                .where(
                    Source.workspace_id == workspace_id,
                    Source.archived_at.is_(None),
                    or_(Source.employer_id.is_(None), Employer.archived_at.is_(None)),
                )
                .order_by(Source.priority, Source.name)
            )
            if selection:
                statement = statement.where(Source.id.in_(selection))
            else:
                # An unselected sweep carries the recurring scope; a named selection
                # reads a source that scope leaves out.
                statement = statement.where(Source.enabled.is_(True))
            readable: list[SweepBoard] = []
            omitted: list[tuple[SweepBoard, str]] = []
            for source, employer in session.execute(statement):
                scan_url, address_note = source_read_address(source.url, source.resolved_url)
                board = SweepBoard(
                    source=source,
                    employer=employer or "",
                    url=scan_url,
                    address_note=address_note,
                    employer_is_authoritative=employer_name_is_authoritative(source.origin),
                )
                if scan_url:
                    readable.append(board)
                else:
                    # No address to read: the sweep cannot dispatch it, so persist
                    # an omitted row rather than dropping it silently.
                    omitted.append((board, "adapter_unavailable"))
            return DueBoards(readable=readable, omitted=omitted)
