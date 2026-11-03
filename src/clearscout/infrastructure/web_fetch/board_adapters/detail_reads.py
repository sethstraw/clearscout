"""Bounded detail reads joined to the stable row identities a board lists."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from clearscout.application.discovery.source_reading import (
    MAX_SOURCE_READ_ATTEMPTS,
    DetailFailureReason,
    SourceContinuationTarget,
    SourceDetailCheckpoint,
    SourceDetailCoverage,
    SourceDetailSummary,
    SourceListing,
    SourceRead,
)
from clearscout.domain import SourceReadOutcome
from clearscout.domain.identity import is_board_uuid, normalize_fetch_url, requisition_key
from clearscout.infrastructure.web_fetch.board_adapters.read_scope import SOURCE_READ_OPTIONS
from clearscout.infrastructure.web_fetch.board_adapters.transport import BoardHttpTransport
from clearscout.infrastructure.web_fetch.board_mappings import BoardDeclaration, Posting
from clearscout.infrastructure.web_fetch.board_mappings.mapper import stated_text
from clearscout.infrastructure.web_fetch.board_platform import host_match
from clearscout.infrastructure.web_fetch.board_reads import (
    board_listings,
    held_reason,
    page_continuation_target,
    read_result,
)
from clearscout.infrastructure.web_fetch.board_reads import row_id as stated_row_id
from clearscout.infrastructure.web_fetch.platforms.dover import DOVER_JOB
from clearscout.infrastructure.web_fetch.policy import (
    FetchPolicyError,
    HostHeldError,
    RequestDeadlineExceeded,
    RobotsDeniedError,
)

_DETAIL_OFFSET = "detail_offset"


_DETAIL_CURSOR = "detail_checkpoint"


@dataclass(frozen=True)
class _DetailRead:
    """One physical detail request's record, counts, and stopping policy."""

    record: Mapping[str, Any] | None
    detail: SourceDetailSummary
    stop_reason: Literal["time_limit_reached", "host_held"] | None = None
    robots_refused: bool = False

    @classmethod
    def failed(cls, reason: DetailFailureReason, example: str) -> _DetailRead:
        """Count one completed unsuccessful request."""
        return cls(
            record=None,
            detail=SourceDetailSummary(
                attempted=1,
                failed=1,
                failure_reasons=((reason, 1),),
                failure_examples=(example,),
            ),
        )


@dataclass
class _DetailProgress:
    """Mutable stable identities for one bounded board-detail slice."""

    completed: dict[str, str]
    closed: set[str]
    failed: dict[str, int]
    pending: set[str]
    protected: dict[str, str]

    @classmethod
    def reconcile(
        cls,
        checkpoint: SourceDetailCheckpoint | None,
        *,
        platform: str,
        board_url: str,
        current: set[str],
        enumeration_complete: bool,
    ) -> _DetailProgress:
        """Reuse matching state and prune absence only from a complete enumeration."""
        if checkpoint is not None and (
            checkpoint.platform != platform
            or normalize_fetch_url(checkpoint.board_url) != normalize_fetch_url(board_url)
        ):
            checkpoint = None
        progress = cls(
            completed=dict(checkpoint.completed) if checkpoint else {},
            closed=set(checkpoint.closed_ids) if checkpoint else set(),
            failed=dict(checkpoint.failed_attempts) if checkpoint else {},
            pending=set(checkpoint.pending_ids) if checkpoint else set(),
            protected={
                item: url
                for item, url in checkpoint.protected
                if item not in current and not SOURCE_READ_OPTIONS.get().allows_posting(url)
            }
            if checkpoint
            else {},
        )
        if checkpoint:
            progress.pending |= {
                item for item, _url in checkpoint.protected if item not in progress.protected
            }
        if enumeration_complete:
            progress.completed = {
                item: url for item, url in progress.completed.items() if item in current
            }
            progress.closed &= current
            progress.failed = {
                item: attempts for item, attempts in progress.failed.items() if item in current
            }
            progress.pending &= current
            progress.protected = {
                item: url for item, url in progress.protected.items() if item in current
            }
        progress.pending |= current - set(progress.completed) - progress.closed
        progress.pending -= set(progress.protected)
        return progress

    def select(self, job_id: str, *, seen: set[str], manual_retry: bool) -> bool:
        """Select each unresolved identity once within its remaining attempt allowance."""
        if (
            job_id in seen
            or job_id in self.completed
            or job_id in self.closed
            or job_id in self.protected
        ):
            return False
        seen.add(job_id)
        return self.failed.get(job_id, 0) < MAX_SOURCE_READ_ATTEMPTS or manual_retry

    def record(
        self,
        job_id: str,
        record: Mapping[str, Any] | None,
        *,
        robots_refused: bool = False,
        row: Any,
        mapping: BoardDeclaration,
        employer: str,
        groups: dict[str, str] | None,
        record_key: str,
        withdrawn: Callable[[Mapping[str, Any]], bool],
    ) -> list[SourceListing]:
        """Apply one finished detail outcome without advancing unresolved identities."""
        if robots_refused:
            self.pending.discard(job_id)
            return []
        if record is None:
            self.failed[job_id] = self.failed.get(job_id, 0) + 1
            return []
        listings = []
        if withdrawn(record):
            self.closed.add(job_id)
        else:
            listings = board_listings(mapping, [{"row": row, record_key: record}], employer, groups)
            if not listings:
                self.failed[job_id] = self.failed.get(job_id, 0) + 1
                return []
            self.completed[job_id] = listings[0].url
        self.pending.discard(job_id)
        self.failed.pop(job_id, None)
        return listings

    def checkpoint(self, *, platform: str, board_url: str) -> SourceDetailCheckpoint:
        """Freeze the slice's identity state for the durable frontier."""
        return SourceDetailCheckpoint(
            platform=platform,
            board_url=board_url,
            completed=tuple(sorted(self.completed.items())),
            closed_ids=tuple(sorted(self.closed)),
            failed_attempts=tuple(sorted(self.failed.items())),
            pending_ids=tuple(sorted(self.pending)),
            protected=tuple(sorted(self.protected.items())),
        )


class BoardDetailReader(BoardHttpTransport):
    """Detail reads for a board's listed rows, resumable from a checkpoint."""

    def detailed_read(
        self,
        employer: str,
        rows: list[Any],
        *,
        mapping: BoardDeclaration,
        detail: Callable[[str], Mapping[str, Any] | None],
        groups: dict[str, str] | None = None,
        record_key: str = "detail",
        withdrawn: Callable[[Mapping[str, Any]], bool] = lambda _record: False,
        row_id: Callable[[Any], str] | None = None,
        missing_fails: bool = False,
        robots_skips: bool = False,
        resume: tuple[str, str] | None = None,
        resume_target: SourceContinuationTarget | None = None,
        enumeration_complete: bool = True,
        relist_required: bool = False,
        records_listed: bool = False,
        skip_recent: bool = False,
    ) -> SourceRead:
        """Join current stable row identities to bounded detail reads.

        ``robots_skips`` sets aside a row whose detail robots refuses instead of
        ending the read. ``records_listed`` rows carry their records, so no deadline cuts them.
        ``skip_recent`` leaves rows whose ``url`` was read recently pending and unrequested.
        """
        identify = row_id or stated_row_id
        board_url = resume[0] if resume else ""
        progress = _DetailProgress.reconcile(
            resume_target.detail_checkpoint if resume_target else None,
            platform=mapping.platform,
            board_url=board_url,
            current={identify(row) for row in rows} - {""},
            enumeration_complete=enumeration_complete,
        )
        read = self._read_detail_rows(
            employer,
            rows,
            progress=progress,
            mapping=mapping,
            detail=detail,
            groups=groups,
            record_key=record_key,
            withdrawn=withdrawn,
            identify=identify,
            missing_fails=missing_fails,
            robots_skips=robots_skips,
            enumeration_complete=enumeration_complete,
            manual_retry=bool(resume_target and resume_target.manual_detail_retry),
            deadline_elapsed=_never if records_listed else self.deadline_elapsed,
            recent=_recent_detail_rows(rows, identify) if skip_recent else frozenset(),
        )
        if resume is None:
            return read
        return _resumable(
            read,
            rows=len(rows),
            resume=resume,
            checkpoint=progress.checkpoint(platform=mapping.platform, board_url=board_url),
            enumeration_complete=enumeration_complete,
            resumed_url=resume_target.url if resume_target else None,
            relist_required=relist_required,
        )

    @staticmethod
    def _protected_detail_url(
        row: Any,
        job_id: str,
        mapping: BoardDeclaration,
        employer: str,
        groups: dict[str, str] | None,
    ) -> str:
        """Resolve a listed posting address without requesting its detail."""
        posting = Posting({"row": row}, employer=employer, groups=groups or {})
        if callable(mapping.url):
            url = mapping.url(posting)
        else:
            url = stated_text(posting.record, mapping.url) if mapping.url is not None else ""
        if url or mapping.platform != "dover" or not is_board_uuid(job_id):
            return url
        for retained in sorted(SOURCE_READ_OPTIONS.get().protected_posting_urls):
            match = host_match(DOVER_JOB, retained)
            if (
                match is not None
                and is_board_uuid(match["job"])
                and requisition_key("", match["job"]) == requisition_key("", job_id)
            ):
                return retained
        return ""

    def _protect_detail_rows(
        self,
        rows: list[Any],
        progress: _DetailProgress,
        mapping: BoardDeclaration,
        employer: str,
        groups: dict[str, str] | None,
        identify: Callable[[Any], str],
    ) -> int:
        """Set aside current protected identities before applying a read deadline."""
        protected: set[str] = set()
        for row in rows:
            job_id = identify(row)
            if not job_id:
                continue
            url = self._protected_detail_url(row, job_id, mapping, employer, groups)
            if url and not SOURCE_READ_OPTIONS.get().allows_posting(url):
                protected.add(job_id)
                progress.protected[job_id] = url
                progress.completed.pop(job_id, None)
                progress.closed.discard(job_id)
                progress.failed.pop(job_id, None)
                progress.pending.discard(job_id)
        return len(protected)

    @staticmethod
    def _protected_detail_read(
        read: SourceRead,
        progress: _DetailProgress,
        current_protected: int,
    ) -> SourceRead:
        """Report protected membership separately from physical detail attempts."""
        if not progress.protected:
            return read
        return replace(
            read,
            raw_count=read.raw_count + current_protected,
            skip_reasons={**read.skip_reasons, "protected_posting": len(progress.protected)},
            note=(
                f"Skipped {len(progress.protected)} protected postings without detail reads. "
                f"{read.note}"
            ).strip(),
            protected_posting_urls=tuple(progress.protected.values()),
        )

    def _read_detail_rows(
        self,
        employer: str,
        rows: list[Any],
        *,
        progress: _DetailProgress,
        mapping: BoardDeclaration,
        detail: Callable[[str], Mapping[str, Any] | None],
        groups: dict[str, str] | None,
        record_key: str,
        withdrawn: Callable[[Mapping[str, Any]], bool],
        identify: Callable[[Any], str],
        missing_fails: bool,
        robots_skips: bool,
        enumeration_complete: bool,
        manual_retry: bool,
        deadline_elapsed: Callable[[], bool],
        recent: frozenset[str] = frozenset(),
    ) -> SourceRead:
        """Read a bounded slice of unresolved rows and apply each finished detail.

        ``recent`` identities stay pending without a request and outside the deadline cut.
        """
        listings: list[SourceListing] = []
        summary = SourceDetailSummary()
        cut = reached = refused = 0
        seen: set[str] = set()
        stop_reason: Literal["time_limit_reached", "host_held"] | None = None
        current_protected = self._protect_detail_rows(
            rows, progress, mapping, employer, groups, identify
        )
        skipped = recent & progress.pending
        for position, row in enumerate(rows):
            job_id = identify(row)
            if not job_id:
                reached += 1
                continue
            if job_id in skipped or not progress.select(
                job_id, seen=seen, manual_retry=manual_retry
            ):
                continue
            if deadline_elapsed():
                cut = sum(
                    identify(item) in progress.pending - skipped
                    and identify(item) not in seen - {job_id}
                    for item in rows[position:]
                )
                stop_reason = "time_limit_reached"
                break
            example = str(row.get("url") or job_id) if isinstance(row, Mapping) else job_id
            result = self._detail_record(
                detail,
                job_id,
                platform=mapping.platform,
                missing_fails=missing_fails,
                robots_skips=robots_skips,
                example=example,
            )
            summary = summary.combined(result.detail)
            if result.stop_reason:
                cut = sum(identify(item) in progress.pending - skipped for item in rows[position:])
                stop_reason = result.stop_reason
                break
            reached += 1
            refused += result.robots_refused
            listings.extend(
                progress.record(
                    job_id,
                    result.record,
                    robots_refused=result.robots_refused,
                    row=row,
                    mapping=mapping,
                    employer=employer,
                    groups=groups,
                    record_key=record_key,
                    withdrawn=withdrawn,
                )
            )
        read = _detail_read_result(
            listings,
            summary=summary,
            rows=len(rows),
            reached=reached,
            cut=cut,
            platform=mapping.platform,
            stop_reason=stop_reason,
            complete=(
                enumeration_complete
                and not refused
                and not progress.pending
                and all(identify(row) for row in rows)
            ),
        )
        return self._protected_detail_read(
            _robots_skipped(read, refused), progress, current_protected
        )

    def _detail_record(
        self,
        detail: Callable[[str], Mapping[str, Any] | None],
        job_id: str,
        *,
        platform: str,
        missing_fails: bool,
        robots_skips: bool,
        example: str,
    ) -> _DetailRead:
        """Classify and count one physical detail request."""
        try:
            record = detail(job_id)
        except RobotsDeniedError:
            if not robots_skips:
                raise
            return _DetailRead(None, SourceDetailSummary(), robots_refused=True)
        except HostHeldError as held:
            return _DetailRead(None, SourceDetailSummary(), held_reason(held))
        except httpx.TimeoutException as exc:
            if self.deadline_elapsed():
                attempted = int(
                    not isinstance(exc, RequestDeadlineExceeded) or exc.request_attempted
                )
                return _DetailRead(
                    None,
                    SourceDetailSummary(attempted=attempted, interrupted=attempted),
                    "time_limit_reached",
                )
            return _DetailRead.failed("timeout", example)
        except httpx.HTTPStatusError:
            return _DetailRead.failed("http_status", example)
        except (FetchPolicyError, httpx.HTTPError):
            return _DetailRead.failed("other", example)
        if record is None and missing_fails:
            return _DetailRead.failed("parse", example)
        if record is None:
            raise ValueError(f"{platform} detail {job_id} did not contain a job")
        return _DetailRead(record, SourceDetailSummary(attempted=1, read=1))


def _never() -> bool:
    return False


def _recent_detail_rows(rows: list[Any], identify: Callable[[Any], str]) -> frozenset[str]:
    """Return the identities of rows whose posting URL this read's options mark recent."""
    recent = SOURCE_READ_OPTIONS.get().recent_posting_urls
    if not recent:
        return frozenset()
    return frozenset(
        job_id
        for row in rows
        if isinstance(row, Mapping)
        and (job_id := identify(row))
        and normalize_fetch_url(str(row.get("url") or "")) in recent
    )


def _robots_skipped(read: SourceRead, refused: int) -> SourceRead:
    """Report the rows robots refused without reading them."""
    if not refused:
        return read
    return replace(
        read,
        skip_reasons={**read.skip_reasons, "robots_disallowed": refused},
        note=(
            f"Skipped {refused} postings robots.txt disallows without reading them. {read.note}"
        ).strip(),
    )


def _resumable(
    read: SourceRead,
    *,
    rows: int,
    resume: tuple[str, str],
    checkpoint: SourceDetailCheckpoint,
    enumeration_complete: bool,
    resumed_url: str | None,
    relist_required: bool = False,
) -> SourceRead:
    """Retain stable detail progress and certify resolved current membership."""
    url, reason = resume
    deferred: tuple[SourceContinuationTarget, ...] = ()
    if relist_required:
        checkpoint = replace(checkpoint, listing_unstable=True)
    if checkpoint.pending_ids or relist_required:
        failed = dict(checkpoint.failed_attempts)
        automatic = "host_held" not in read.skip_reasons and (
            relist_required
            or any(
                failed.get(item, 0) < MAX_SOURCE_READ_ATTEMPTS for item in checkpoint.pending_ids
            )
        )
        deferred = (
            replace(
                page_continuation_target(
                    _detail_cursor_url(url, checkpoint),
                    source_url=url,
                    label=f"{checkpoint.platform} pending detail reads",
                    reason=reason,
                ),
                automatic_eligible=automatic,
                detail_checkpoint=checkpoint,
            ),
        )
    coverage = None
    if enumeration_complete and read.complete:
        coverage = SourceDetailCoverage(
            platform=checkpoint.platform,
            board_url=url,
            posting_urls=tuple(
                dict.fromkeys(
                    value for _item, value in (*checkpoint.completed, *checkpoint.protected)
                )
            ),
        )
    return replace(
        read,
        raw_count=rows
        if enumeration_complete
        else max(
            rows,
            len(
                {item for item, _url in (*checkpoint.completed, *checkpoint.protected)}
                | set(checkpoint.closed_ids)
                | set(checkpoint.pending_ids)
            ),
        ),
        outcome=(
            SourceReadOutcome.CRAWL_CONTINUATION
            if deferred
            else SourceReadOutcome.ATS_COMPLETE
            if read.complete
            else SourceReadOutcome.NO_SUPPORTED_REPRESENTATION
        ),
        deferred_targets=deferred,
        exhausted_outcome=SourceReadOutcome.PUBLISHER_INDEX if deferred else None,
        attempted_target_urls=(resumed_url,) if resumed_url else None,
        detail_coverage=coverage,
        detail_read_urls=tuple(dict.fromkeys(url for _item, url in checkpoint.completed)),
    )


_DEADLINE_CUT_NOTE = re.compile(
    r"(?P<lead>\s*)The read deadline cut off (?P<cut>\d+) of (?P<rows>\d+) "
    r"(?P<platform>\S+) detail reads\."
)


def deadline_cut_note(cut: int, rows: int, platform: str) -> str:
    """State how many of a slice's detail reads the read deadline cut off."""
    return f"The read deadline cut off {cut} of {rows} {platform} detail reads."


def restate_deadline_cut_note(note: str, cut: int) -> str:
    """Restate a note's deadline-cut sentence with ``cut`` reads, or drop it at zero."""

    def restated(match: re.Match[str]) -> str:
        if cut <= 0:
            return ""
        return match["lead"] + deadline_cut_note(cut, int(match["rows"]), match["platform"])

    return _DEADLINE_CUT_NOTE.sub(restated, note).strip()


def _detail_read_result(
    listings: list[SourceListing],
    *,
    summary: SourceDetailSummary,
    rows: int,
    reached: int,
    cut: int,
    platform: str,
    stop_reason: Literal["time_limit_reached", "host_held"] | None,
    complete: bool,
) -> SourceRead:
    """Describe physical detail outcomes and the unattempted tail of one slice."""
    read = read_result(
        listings,
        raw_count=max(reached, len(listings)),
        complete=complete,
        incomplete_reason=stop_reason,
    )
    notes = []
    if summary.failed:
        notes.append(f"{summary.failed} of {rows} {platform} detail reads failed.")
        read = replace(read, outcome=SourceReadOutcome.NO_SUPPORTED_REPRESENTATION)
    if stop_reason == "host_held":
        notes.append("The publisher requested a wait before the next detail read.")
    elif cut:
        notes.append(deadline_cut_note(cut, rows, platform))
    capped = 0 if stop_reason == "host_held" else max(0, cut - summary.interrupted)
    return replace(
        read,
        note=" ".join(notes),
        detail=summary.combined(SourceDetailSummary(capped=capped)),
    )


def _detail_cursor_url(url: str, checkpoint: SourceDetailCheckpoint) -> str:
    """Identify logical membership and progress independently of failed attempts."""
    state = checkpoint.as_json()
    state.pop("failed_attempts")
    fingerprint = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()[:24]
    parsed = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query)
        if key not in {_DETAIL_OFFSET, _DETAIL_CURSOR}
    ]
    return urlunsplit(parsed._replace(query=urlencode([*query, (_DETAIL_CURSOR, fingerprint)])))
