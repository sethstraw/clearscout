"""The scan row, frontier, and detail receipts a board read persists."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tailorcv.application.discovery.funnel_audit import FunnelAuditBuilder, FunnelReason
from tailorcv.application.discovery.scan_outcome import (
    build_scan_status_note,
    exhausted_continuation_note,
    may_settle_drained_scan,
    resolve_exhausted_outcome,
)
from tailorcv.application.discovery.scan_queries import QUERY_LABEL
from tailorcv.application.discovery.scan_staging import (
    WithdrawingRead,
    queue_absence_checks,
    withdraw_absent_in_session,
)
from tailorcv.application.discovery.scan_sweep.listing_audit import record_source
from tailorcv.application.discovery.scan_sweep.models import BoardReadOutcome, SweepBoard
from tailorcv.application.discovery.scan_sweep.outcome import failed_read_reason
from tailorcv.application.discovery.source_continuation.commands import (
    FrontierPersistResult,
    SourceContinuationCommands,
)
from tailorcv.application.discovery.source_continuation.publisher import (
    drained_publisher_listing_coverage,
)
from tailorcv.application.discovery.source_continuation.targets import (
    ListedPostingCredit,
    completed_frontier_urls,
    person_held_frontier_urls,
)
from tailorcv.application.discovery.source_lifecycle import source_read_address
from tailorcv.application.discovery.source_reading import (
    SourceContinuationTarget,
    SourceDetailCheckpoint,
    SourceDetailCoverage,
    SourceDetailSummary,
)
from tailorcv.application.failures import ValidationError
from tailorcv.application.tasks.commands import TaskCommands
from tailorcv.domain import SourceReadOutcome, new_id, utc_now
from tailorcv.domain.identity import normalize_fetch_url
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import Source, SourceScan
from tailorcv.infrastructure.persistence.models.discovery import SourceScanMember
from tailorcv.infrastructure.web_fetch.board_adapters import restate_deadline_cut_note
from tailorcv.infrastructure.web_fetch.boards import board_read_scope


class ScanRecorder:
    """Persist each board read's scan row, continuation frontier, and detail receipts."""

    def __init__(
        self,
        database: Database,
        batch_commands: TaskCommands,
        continuations: SourceContinuationCommands | None,
    ) -> None:
        """Bind recording to the database, the batch member guard, and the frontier store."""
        self.database = database
        self.batch_commands = batch_commands
        self.continuations = continuations

    def record_omitted(
        self,
        audit: FunnelAuditBuilder,
        board: SweepBoard,
        reason: str,
        today: date,
        *,
        correlation_id: str | None,
        require_source_read: Callable[[Session], None] | None = None,
    ) -> dict[str, Any]:
        """Persist an omitted row for an enabled source the sweep does not touch."""
        now = utc_now()
        record_source(audit, board, FunnelReason.NO_READABLE_BOARD)
        self.record_scan(
            board.source,
            today,
            "omitted",
            0,
            0,
            f"reason: {reason}",
            started_at=now,
            completed_at=now,
            read_outcome=SourceReadOutcome.NO_SUPPORTED_REPRESENTATION,
            deferred_targets=(),
            correlation_id=correlation_id,
            require_source_read=require_source_read,
        )
        return {
            "source": board.source.name,
            "raw": 0,
            "open": 0,
            "skip_reasons": {},
            "complete": None,
            "matched": 0,
            "staged": 0,
            "new": 0,
            "withdrawn": 0,
            "detail": {"attempted": 0, "read": 0, "failed": 0, "capped": 0},
            "omitted_reason": reason,
        }

    def record_failed(
        self,
        audit: FunnelAuditBuilder,
        outcome: BoardReadOutcome,
        today: date,
        *,
        correlation_id: str | None,
        require_source_read: Callable[[Session], None] | None = None,
    ) -> dict[str, str]:
        """Persist a failed row for a board whose read raised, and audit why."""
        board = outcome.board
        source = board.source
        failure = outcome.failure or ""
        record_source(audit, board, failed_read_reason(outcome.failure_outcome), detail=failure)
        self.record_scan(
            source,
            today,
            "failed",
            0,
            0,
            " ".join(note for note in (board.address_note, failure) if note)[:500],
            started_at=outcome.started_at,
            completed_at=outcome.completed_at,
            read_outcome=outcome.failure_outcome or SourceReadOutcome.FETCH_FAILED,
            deferred_targets=(),
            correlation_id=correlation_id,
            require_source_read=require_source_read,
        )
        return {"source": source.name, "reason": failure}

    @staticmethod
    def _validated_scan_coverages(
        source: Source, context: ScanReadContext
    ) -> tuple[SourceDetailCoverage | None, SourceDetailCoverage | None]:
        """Keep detail and listing receipts only within the source's exact scope."""
        root = source_read_address(source.url, source.resolved_url)[0]
        coverage = context.detail_coverage
        if coverage is not None:
            scope = board_read_scope(root, coverage.platform)
            valid_alignerr = (
                coverage.platform == "alignerr"
                and normalize_fetch_url(root) == normalize_fetch_url(coverage.board_url)
                and not context.discovery_truncated
            )
            if not valid_alignerr and (
                scope is None or scope != board_read_scope(coverage.board_url, coverage.platform)
            ):
                coverage = None
        listing = context.listing_coverage
        if listing is not None and (
            listing.platform != "alignerr"
            or context.discovery_truncated
            or set(context.listing_urls) != set(listing.posting_urls)
            or normalize_fetch_url(root) != normalize_fetch_url(listing.board_url)
        ):
            listing = None
        return coverage, listing

    def _replace_scan_frontier(
        self,
        session: Session,
        source: Source,
        scan_id: str,
        targets: tuple[SourceContinuationTarget, ...],
        context: ScanReadContext,
        status: str,
        listing_coverage: SourceDetailCoverage | None,
        correlation_id: str | None,
    ) -> FrontierPersistResult:
        """Replace retained work and bind its counts to this read's listing scope."""
        if self.continuations is None:
            raise RuntimeError("Scan frontier replacement needs continuation commands.")
        frontier = self.continuations.replace_frontier_in_session(
            session,
            workspace_id=source.workspace_id,
            source_scan_id=scan_id,
            targets=targets,
            correlation_id=correlation_id,
            handoff_scopes=context.handoff_scopes,
            carry_forward=status in {"incomplete", "failed"}
            or context.complete is False
            or bool(listing_coverage),
            read_urls=context.attempted_target_urls,
            completed_workday_urls=context.completed_workday_urls,
            publisher_listing_receipts=context.publisher_listing_receipts,
        )
        if context.listing_identity_urls:
            self.continuations.retain_alignerr_body_count_in_session(
                session,
                frontier.continuation_id,
                context.listing_urls,
            )
        if listing_coverage is not None:
            refreshed = self.continuations.retain_listing_coverage_in_session(
                session,
                frontier.continuation_id,
                listing_coverage,
                observed_urls=context.listing_urls,
                listing_identity_urls=context.listing_identity_urls,
                targets=targets,
            )
            if refreshed is not None:
                frontier = replace(
                    frontier, pending_count=refreshed[0], unresolved_count=refreshed[1]
                )
        return frontier

    def record_scan(
        self,
        source: Source,
        today: date,
        status: str | None,
        result_count: int,
        new_count: int,
        notes: str | None,
        *,
        started_at: datetime,
        completed_at: datetime,
        read_outcome: SourceReadOutcome,
        deferred_targets: tuple[SourceContinuationTarget, ...],
        read_context: ScanReadContext | None = None,
        correlation_id: str | None = None,
        require_source_read: Callable[[Session], None] | None = None,
    ) -> RecordedScan:
        """Upsert one same-day scan row and its deferred frontier; the latest scan wins.

        ``status``/``notes`` are literal for a call with no ``read_context`` (an
        omitted or failed source); a successful read's status and note are built
        here from ``read_context``.
        """
        if deferred_targets and self.continuations is None:
            raise ValidationError(
                "The source returned continuation work without a configured continuation reader."
            )
        read_context = read_context or ScanReadContext()
        coverage, listing_coverage = self._validated_scan_coverages(source, read_context)
        exhausted_outcome = read_context.exhausted_outcome or (
            SourceReadOutcome.ATS_COMPLETE if listing_coverage is not None else None
        )
        raw_count, read_count, skip_reasons, complete, detail = (
            read_context.raw_count,
            read_context.read_count,
            read_context.skip_reasons,
            read_context.complete,
            read_context.detail,
        )
        if status is None or notes is None:
            status, notes = build_scan_status_note(
                read_outcome=read_outcome,
                raw_count=raw_count,
                read_count=read_count,
                skip_reasons=skip_reasons or {},
                matched=read_context.matched,
                staged=result_count,
                refused=read_context.refused,
                unfiltered=read_context.unfiltered,
                read_note=read_context.read_note,
                detail=detail or SourceDetailSummary(),
                complete=bool(complete),
            )
        detail_counts = _detail_field_counts(detail)
        fields: dict[str, Any] = {
            "status": status,
            "read_outcome": read_outcome,
            "raw_count": raw_count,
            "read_count": read_count,
            "skip_reasons": skip_reasons or {},
            "result_count": result_count,
            "new_count": new_count,
            "detail_attempted_count": detail_counts.attempted,
            "detail_read_count": detail_counts.read,
            "detail_failed_count": detail_counts.failed,
            "detail_interrupted_count": detail_counts.interrupted,
            "detail_capped_count": detail_counts.capped,
            "detail_failure_reasons": detail_counts.failure_reasons,
            "notes": notes,
            "complete": complete,
            "discovery_truncated": read_context.discovery_truncated,
            "listing_pages": read_context.listing_pages,
            "exhausted_outcome": exhausted_outcome,
            "detail_coverage": (coverage.as_json() if coverage is not None else None),
            "scan_started_at": started_at,
            "scan_completed_at": completed_at,
            "duration_ms": max(0, int((completed_at - started_at).total_seconds() * 1000)),
            "correlation_id": correlation_id,
        }
        final_status, final_outcome, final_notes, pending_count = status, read_outcome, notes, 0
        withdrawn: tuple[str, ...] = ()
        with self.database.exclusive_session() as session:
            if require_source_read is not None:
                require_source_read(session)
            member = session.scalar(
                select(SourceScanMember).where(
                    SourceScanMember.source_id == source.id,
                    SourceScanMember.root_task_id == correlation_id,
                )
            )
            if member is not None:
                self._record_root_detail_receipt(
                    session, member, fields, detail_counts, correlation_id
                )
            scan_id = self._upsert_scan_row(session, source.id, today, fields)
            if member is not None:
                member.source_scan_id = scan_id
            if self.continuations is not None:
                frontier = self._replace_scan_frontier(
                    session,
                    source,
                    scan_id,
                    deferred_targets,
                    read_context,
                    status,
                    listing_coverage,
                    correlation_id,
                )
                scan = session.get(SourceScan, scan_id)
                if scan is not None:
                    skip_reasons, read_note, final_notes = absorb_already_read_postings(
                        scan, frontier, skip_reasons or {}, (read_context.read_note, final_notes)
                    )
                    read_context = replace(read_context, read_note=read_note)
                    read_count = scan.read_count or 0
                pending_count = frontier.pending_count
                resolved = resolve_exhausted_outcome(
                    has_frontier=bool(deferred_targets) or listing_coverage is not None,
                    discovery_truncated=read_context.discovery_truncated,
                    exhausted_outcome=exhausted_outcome,
                    unresolved_count=frontier.unresolved_count,
                )
                if resolved is not None:
                    listing_coverage, raw_count, read_count = _certify_publisher_listing(
                        session,
                        scan,
                        frontier.continuation_id,
                        listing_coverage,
                        (raw_count, read_count),
                    )
                    settled = listing_coverage is not None or may_settle_drained_scan(
                        status, read_outcome, skip_reasons or {}
                    )
                    final_status, final_notes = build_scan_status_note(
                        read_outcome=resolved,
                        raw_count=raw_count,
                        read_count=read_count,
                        skip_reasons=skip_reasons or {},
                        matched=read_context.matched,
                        staged=result_count,
                        refused=read_context.refused,
                        unfiltered=read_context.unfiltered,
                        read_note=read_context.read_note,
                        detail=detail or SourceDetailSummary(),
                        complete=settled or bool(complete),
                        listing_complete=listing_coverage is not None,
                    )
                    final_notes = exhausted_continuation_note(final_notes)
                    final_outcome = resolved
                    withdrawn = self._apply_settled_scan(
                        session,
                        source,
                        scan_id,
                        today,
                        status=final_status,
                        outcome=final_outcome,
                        notes=final_notes,
                        settled=settled,
                        continuation_id=frontier.continuation_id,
                        read_context=read_context,
                        listing_coverage=listing_coverage,
                        correlation_id=correlation_id,
                    )
        return RecordedScan(
            status=final_status,
            read_outcome=final_outcome,
            note=final_notes,
            pending_count=pending_count,
            withdrawn=withdrawn,
        )

    def _record_root_detail_receipt(
        self,
        session: Session,
        member: SourceScanMember,
        fields: dict[str, Any],
        detail_counts: _DetailFieldCounts,
        correlation_id: str | None,
    ) -> None:
        """Preserve a resumed family's totals and record this root attempt once."""
        receipt = f"root:{correlation_id}:{member.active_attempt_count}"
        receipts = list(member.result.get("detail_receipts", []))
        prior_scan = (
            session.get(SourceScan, member.source_scan_id)
            if member.source_scan_id is not None
            else None
        )
        if receipts and prior_scan is not None:
            for name in (
                "detail_attempted_count",
                "detail_read_count",
                "detail_failed_count",
                "detail_interrupted_count",
                "detail_capped_count",
            ):
                previous = getattr(prior_scan, name)
                fields[name] = (
                    None
                    if previous is None
                    else previous + (0 if receipt in receipts else fields[name])
                )
            reasons = dict(prior_scan.detail_failure_reasons)
            if receipt not in receipts:
                for reason, count in detail_counts.failure_reasons.items():
                    reasons[reason] = reasons.get(reason, 0) + count
            fields["detail_failure_reasons"] = reasons
        if receipt not in receipts:
            member.result = {**member.result, "detail_receipts": [*receipts, receipt]}

    def record_detail_read_in_session(
        self,
        scan: SourceScan,
        member: SourceScanMember,
        receipt: str,
        detail: SourceDetailSummary,
        *,
        listed_read: ListedPostingCredit | None = None,
    ) -> None:
        """Record this owned attempt and its listed-posting credit once per receipt."""
        receipts = list(member.result.get("detail_receipts", []))
        if receipt in receipts:
            return
        scan.detail_attempted_count += detail.attempted
        scan.detail_read_count += detail.read
        scan.detail_failed_count += detail.failed
        if scan.detail_interrupted_count is not None:
            scan.detail_interrupted_count += detail.interrupted
        scan.detail_capped_count += detail.capped
        reasons = dict(scan.detail_failure_reasons)
        for reason, count in detail.failure_reasons:
            reasons[reason] = reasons.get(reason, 0) + count
        scan.detail_failure_reasons = reasons
        if listed_read is not None:
            _restate_deadline_cut(scan, listed_read)
        member.result = {**member.result, "detail_receipts": [*receipts, receipt]}

    @staticmethod
    def _upsert_scan_row(
        session: Session, source_id: str, today: date, fields: dict[str, Any]
    ) -> str:
        """Refresh this source's same-day scan row or insert it, and return its id."""
        existing = session.scalars(
            select(SourceScan).where(
                SourceScan.source_id == source_id,
                SourceScan.scanned_on == today,
                SourceScan.query_label == QUERY_LABEL,
            )
        ).first()
        if existing is not None:
            # Fresh counts win. Preserve id and created_at; refresh everything else.
            for name, value in fields.items():
                setattr(existing, name, value)
            return existing.id
        scan = SourceScan(
            id=new_id(),
            source_id=source_id,
            scanned_on=today,
            query_label=QUERY_LABEL,
            **fields,
        )
        session.add(scan)
        session.flush()
        return scan.id

    def _apply_settled_scan(
        self,
        session: Session,
        source: Source,
        scan_id: str,
        today: date,
        *,
        status: str,
        outcome: SourceReadOutcome,
        notes: str,
        settled: bool,
        continuation_id: str | None,
        read_context: ScanReadContext,
        listing_coverage: SourceDetailCoverage | None,
        correlation_id: str | None,
    ) -> tuple[str, ...]:
        """Write the relabeled scan, and withdraw what a settled whole list no longer carries."""
        row = session.get(SourceScan, scan_id)
        if row is None:
            return ()
        row.status, row.read_outcome, row.notes = status, outcome, notes
        if not settled or (continuation_id is None and listing_coverage is None):
            return ()
        row.complete = True
        return tuple(
            withdraw_absent_in_session(
                session,
                source.workspace_id,
                source.id,
                [
                    *(
                        listing_coverage.posting_urls
                        if listing_coverage is not None
                        else SourceDetailCoverage.from_json(row.detail_coverage).posting_urls
                        if row.detail_coverage is not None
                        else [
                            *read_context.listing_urls,
                            *read_context.attempted_target_urls,
                            *(
                                completed_frontier_urls(session, continuation_id)
                                if continuation_id
                                else ()
                            ),
                        ]
                    ),
                    *(
                        person_held_frontier_urls(session, continuation_id)
                        if continuation_id
                        else ()
                    ),
                ],
                today,
                correlation_id=correlation_id,
                read=WithdrawingRead(
                    row.raw_count or 0,
                    row.read_count or 0,
                    bool(row.complete),
                    row.listing_pages or 0,
                    started_at=row.scan_started_at,
                ),
                check_absent=queue_absence_checks(
                    self.batch_commands, source.workspace_id, correlation_id
                ),
            )
        )


@dataclass(frozen=True)
class _DetailFieldCounts:
    """Detail counts as the SourceScan row's flat columns store them."""

    attempted: int
    read: int
    failed: int
    interrupted: int
    capped: int
    failure_reasons: dict[str, int]


def _detail_field_counts(detail: SourceDetailSummary | None) -> _DetailFieldCounts:
    """Flatten an optional detail summary to its zeroed defaults."""
    if detail is None:
        return _DetailFieldCounts(0, 0, 0, 0, 0, {})
    return _DetailFieldCounts(
        detail.attempted,
        detail.read,
        detail.failed,
        detail.interrupted,
        detail.capped,
        dict(detail.failure_reasons),
    )


@dataclass(frozen=True)
class RecordedScan:
    """The status, outcome, note, and frontier state actually persisted for one scan."""

    status: str
    read_outcome: SourceReadOutcome
    note: str
    pending_count: int
    # Titles withdrawn because this scan settled as the source's whole list.
    withdrawn: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScanReadContext:
    """The underlying read's fields a successful-scan upsert notes and can reclassify from."""

    raw_count: int = 0
    read_count: int = 0
    skip_reasons: dict[str, int] | None = None
    complete: bool | None = None
    detail: SourceDetailSummary | None = None
    discovery_truncated: bool = False
    listing_pages: int = 0
    exhausted_outcome: SourceReadOutcome | None = None
    listing_coverage: SourceDetailCoverage | None = None
    publisher_listing_receipts: tuple[SourceDetailCheckpoint, ...] = ()
    listing_identity_urls: tuple[tuple[str, str], ...] = ()
    detail_coverage: SourceDetailCoverage | None = None
    matched: int = 0
    refused: int = 0
    unfiltered: bool = False
    read_note: str = ""
    handoff_scopes: tuple[tuple[str, str, str], ...] = ()
    attempted_target_urls: tuple[str, ...] = ()
    completed_workday_urls: tuple[str, ...] = ()
    listing_urls: tuple[str, ...] = ()


def absorb_already_read_postings(
    scan: SourceScan,
    frontier: FrontierPersistResult,
    skip_reasons: dict[str, int],
    notes: tuple[str, str],
) -> tuple[dict[str, int], str, str]:
    """Credit deferred listed postings a completed read holds against the deadline cut.

    Detail counts stay physical requests; only the listing read and the cap move.
    """
    read_note, note = notes
    absorbed = frontier.absorbed_count
    capped = scan.detail_capped_count or 0
    remaining = _restate_deadline_cut(
        scan,
        ListedPostingCredit(
            absorbed,
            # Deferred postings include failed and interrupted rows, so only the
            # unread remainder bounds what the deadline can still have cut.
            owed=max(0, frontier.listed_posting_count - absorbed),
            drained=frontier.listed_posting_count > 0 and frontier.unresolved_count == 0,
        ),
    )
    if remaining != capped:
        read_note = restate_deadline_cut_note(read_note, remaining)
        note = restate_deadline_cut_note(note, remaining)
    if "time_limit_reached" not in scan.skip_reasons:
        skip_reasons = {k: v for k, v in skip_reasons.items() if k != "time_limit_reached"}
    return skip_reasons, read_note, note


def _restate_deadline_cut(scan: SourceScan, credit: ListedPostingCredit) -> int:
    """Count listed bodies read against the scan's deadline cut, and return what is left.

    ``owed`` bounds the cut to listed postings still unread; once ``drained`` says no
    listed target of any kind is owed, an emptied cut ends the deadline's claim.
    """
    remaining = scan.detail_capped_count or 0
    if credit.absorbed > 0:
        if not credit.reader_counts_bodies:
            scan.read_count = min(scan.raw_count or 0, (scan.read_count or 0) + credit.absorbed)
        if credit.owed < remaining:
            remaining = scan.detail_capped_count = credit.owed
            scan.notes = restate_deadline_cut_note(scan.notes or "", remaining)
    if credit.drained and not remaining and "time_limit_reached" in scan.skip_reasons:
        scan.skip_reasons = {
            key: count for key, count in scan.skip_reasons.items() if key != "time_limit_reached"
        }
    return remaining


def _certify_publisher_listing(
    session: Session,
    scan: SourceScan | None,
    continuation_id: str | None,
    coverage: SourceDetailCoverage | None,
    counts: tuple[int, int],
) -> tuple[SourceDetailCoverage | None, int, int]:
    """Return the reader's coverage, else a drained publisher listing's, with the counts to show."""
    if coverage is not None or scan is None or continuation_id is None:
        return coverage, *counts
    certified = drained_publisher_listing_coverage(session, continuation_id, scan)
    if certified is None:
        return None, *counts
    return certified, scan.raw_count or 0, scan.read_count or 0
