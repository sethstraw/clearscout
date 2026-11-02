"""Orchestration of the whole-queue discovery batch handlers.

The per-row evaluation and refresh are covered by their own tests; these pin the
batch loop: one shared spend ceiling stops a paid sweep with a partial result,
and a per-row failure is recorded without stopping the sweep.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, Event
from typing import Any, cast

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from tailorcv.application.discovery.board_scan import (
    DiscoveryRefreshBatchHandler,
    SourceScanService,
)
from tailorcv.application.discovery.content_resolution import (
    PostingContentResolutionService,
    PostingResolutionPreflight,
    ResolvedPostingContent,
)
from tailorcv.application.discovery.source_reading import PostingCheck
from tailorcv.application.failures import (
    DeadLinkRefreshError,
    PublisherUnverifiedRefreshError,
    UnsupportedPublisherRefreshError,
    ValidationError,
)
from tailorcv.application.tailoring.workflows.evaluations import (
    DiscoveryEvaluateBatchHandler,
    EvaluationHandler,
    EvaluationWorkflowService,
)
from tailorcv.domain import (
    PostingContentAccessStatus,
    PostingContentExtractionMethod,
    PostingIdentityStatus,
    PostingStatus,
    utc_now,
)
from tailorcv.domain.identity import normalize_url
from tailorcv.domain.records import PostingReadIntent
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    AuditEvent,
    PendingDiscovery,
    PostingUrl,
    Source,
)
from tailorcv.infrastructure.web_fetch.ingest.url import check_posting
from tailorcv.shared.errors import ProviderError, QuotaError
from tests.support.artifacts import runtime_storage
from tests.support.doubles import UnexpectedPostingFetcher
from tests.support.http import (
    DAYFORCE_POSTING_URL,
    CountingFeedClient,
    dayforce_feed_row,
    policy_client,
)
from tests.support.scenarios import (
    board_source,
    dismiss_pending_discovery,
    employer_board,
    pending_discovery,
    scan_workspace,
    seed_unread_row,
    tracked_board_posting,
)


@dataclass
class _Task:
    workspace_id: str
    created_at: datetime


@dataclass
class _Queue:
    task: _Task

    def get(self, task_id: str) -> _Task:
        assert task_id == "task-1"
        return self.task


@dataclass
class _Context:
    task_id: str = "task-1"
    worker_id: str = "worker-1"
    queue: _Queue = field(
        default_factory=lambda: _Queue(_Task("workspace", datetime(2026, 9, 3, tzinfo=UTC)))
    )
    stages: list[str] = field(default_factory=list)
    payloads: list[dict[str, Any]] = field(default_factory=list)
    cancelled: Event = field(default_factory=Event)
    busy_stage: str | None = None

    def progress(self, stage: str, payload: dict[str, Any]) -> None:
        if stage == self.busy_stage:
            raise OperationalError("UPDATE background_tasks", {}, Exception("database is locked"))
        self.stages.append(stage)
        self.payloads.append(payload)

    def cancellation_requested(self) -> bool:
        return self.cancelled.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise _CancelledError


class _CancelledError(Exception):
    """Stands in for the worker's cooperative cancellation signal."""


class _Evaluator:
    """Stands in for EvaluationHandler, scripting one outcome per target id.

    A callable outcome runs on the lane thread with the row's context, then its return
    value is scripted. ``same_posting`` maps a row to the earlier row naming its posting.
    """

    def __init__(
        self,
        outcomes: dict[
            str,
            dict[str, Any] | Exception | Callable[[Any], dict[str, Any] | Exception],
        ],
        same_posting: dict[str, str] | None = None,
    ) -> None:
        self.outcomes = outcomes
        self.same_posting = same_posting or {}
        self.seen: list[str] = []

    def posting_groups(self, workspace_id: str, pending_discovery_ids: list[str]) -> dict[str, str]:
        return {row: self.same_posting.get(row, row) for row in pending_discovery_ids}

    def evaluate_target(
        self,
        context: Any,
        workspace_id: str,
        *,
        created_at: datetime,
        pending_discovery_id: str | None,
        job_posting_id: str | None,
    ) -> dict[str, Any]:
        target_id = pending_discovery_id if pending_discovery_id is not None else job_posting_id
        assert target_id is not None
        self.seen.append(target_id)
        outcome = self.outcomes[target_id]
        if callable(outcome):
            outcome = outcome(context)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_evaluate_batch_retains_other_results_after_a_provider_error() -> None:
    evaluator = _Evaluator(
        {
            "a": {"decision": "apply_now"},
            "b": ProviderError("provider disconnected"),
            "c": {"decision": "investigate"},
        }
    )
    handler = DiscoveryEvaluateBatchHandler(evaluator)  # type: ignore[arg-type]

    result = handler(_Context(), {"pending_discovery_ids": ["a", "b", "c"]})  # type: ignore[arg-type]

    assert result["evaluated"] == 2
    assert result["failed"] == 1
    assert result["selected"] == 3
    assert evaluator.seen == ["a", "b", "c"]


@pytest.mark.parametrize("lanes", [1, 2])
def test_evaluate_batch_records_a_row_failure_and_keeps_going(lanes: int) -> None:
    evaluator = _Evaluator(
        {
            "a": ValidationError("bad row"),
            "b": {"decision": "investigate"},
        }
    )
    handler = DiscoveryEvaluateBatchHandler(evaluator, lanes=lanes)  # type: ignore[arg-type]

    result = handler(_Context(), {"pending_discovery_ids": ["a", "b"]})  # type: ignore[arg-type]

    assert result["evaluated"] == 1
    assert result["failed"] == 1
    assert result["failures"][0]["pending_discovery_id"] == "a"
    assert sorted(evaluator.seen) == ["a", "b"]


def test_evaluate_batch_reports_rows_in_selection_order_when_they_finish_out_of_order() -> None:
    later_row_done = Event()

    def first(_: Any) -> dict[str, Any]:
        later_row_done.wait()
        return {"pending_discovery_id": "a", "decision": "apply_now"}

    def second_failure(_: Any) -> Exception:
        return ValidationError("bad row b")

    def third(_: Any) -> dict[str, Any]:
        later_row_done.set()
        return {"pending_discovery_id": "c", "decision": "investigate"}

    evaluator = _Evaluator(
        {
            "a": first,
            "b": second_failure,
            "c": third,
            "d": ValidationError("bad row d"),
            "e": {"pending_discovery_id": "e", "skipped": "withdrawn"},
            "f": {"pending_discovery_id": "f", "skipped": "dismissed"},
        }
    )
    handler = DiscoveryEvaluateBatchHandler(evaluator, lanes=3)  # type: ignore[arg-type]

    result = handler(
        _Context(),  # type: ignore[arg-type]
        {"pending_discovery_ids": ["a", "b", "c", "d", "e", "f"]},
    )

    assert [row["pending_discovery_id"] for row in result["results"]] == ["a", "c"]
    assert [row["pending_discovery_id"] for row in result["failures"]] == ["b", "d"]
    assert [row["pending_discovery_id"] for row in result["skipped_rows"]] == ["e", "f"]


def test_evaluate_batch_starts_no_row_after_cancellation() -> None:
    context = _Context()

    def cancel_during_row(_: Any) -> dict[str, Any]:
        context.cancelled.set()
        return {"decision": "apply_now"}

    evaluator = _Evaluator({"a": cancel_during_row, "b": {"decision": "investigate"}})
    handler = DiscoveryEvaluateBatchHandler(evaluator, lanes=1)  # type: ignore[arg-type]

    with pytest.raises(_CancelledError):
        handler(context, {"pending_discovery_ids": ["a", "b"]})  # type: ignore[arg-type]

    assert evaluator.seen == ["a"]
    assert context.stages.count("evaluating") == 1


def test_evaluate_batch_lets_running_lanes_finish_after_cancellation_and_starts_no_row() -> None:
    context = _Context()
    finished: list[str] = []
    running = Event()

    def cancel_during_row(_: Any) -> dict[str, Any]:
        running.wait()
        context.cancelled.set()
        return {"decision": "apply_now"}

    def running_row(_: Any) -> dict[str, Any]:
        running.set()
        context.cancelled.wait()
        finished.append("b")
        return {"decision": "investigate"}

    evaluator = _Evaluator(
        {"a": cancel_during_row, "b": running_row, "c": {"decision": "investigate"}}
    )
    handler = DiscoveryEvaluateBatchHandler(evaluator, lanes=2)  # type: ignore[arg-type]

    with pytest.raises(_CancelledError):
        handler(context, {"pending_discovery_ids": ["a", "b", "c"]})  # type: ignore[arg-type]

    assert finished == ["b"]
    assert sorted(evaluator.seen) == ["a", "b"]
    assert context.stages.count("evaluating") == 2


def test_evaluate_batch_runs_rows_naming_one_posting_in_one_lane_in_selection_order() -> None:
    """A later row naming the same posting must see the earlier row's commit and not pay."""
    context = _Context()
    committed: dict[str, int] = {}
    paid: list[str] = []
    unrelated_started = Event()

    def ledger(row: str) -> Callable[[Any], dict[str, Any]]:
        def outcome(_: Any) -> dict[str, Any]:
            if row == "a":
                unrelated_started.wait()
            if "posting-1" in committed:
                return {"pending_discovery_id": row, "decision": "already_evaluated"}
            paid.append(row)
            committed["posting-1"] = len(context.stages)
            return {"pending_discovery_id": row, "decision": "apply_now"}

        return outcome

    def unrelated(_: Any) -> dict[str, Any]:
        unrelated_started.set()
        return {"pending_discovery_id": "c", "decision": "investigate"}

    evaluator = _Evaluator(
        {"a": ledger("a"), "b": ledger("b"), "c": unrelated},
        same_posting={"b": "a"},
    )
    handler = DiscoveryEvaluateBatchHandler(evaluator, lanes=4)  # type: ignore[arg-type]

    result = handler(context, {"pending_discovery_ids": ["a", "b", "c"]})  # type: ignore[arg-type]

    started = [
        payload["pending_discovery_id"]
        for stage, payload in zip(context.stages, context.payloads, strict=True)
        if stage == "evaluating"
    ]
    assert started.index("b") >= committed["posting-1"]
    assert paid == ["a"]
    assert [row["decision"] for row in result["results"]] == [
        "apply_now",
        "already_evaluated",
        "investigate",
    ]


def test_evaluate_batch_counts_a_row_as_current_only_once_an_earlier_row_finished() -> None:
    both_running = Barrier(2)

    def meet(_: Any) -> dict[str, Any]:
        both_running.wait()
        return {"decision": "investigate"}

    context = _Context()
    handler = DiscoveryEvaluateBatchHandler(_Evaluator({"a": meet, "b": meet}), lanes=2)  # type: ignore[arg-type]

    handler(context, {"pending_discovery_ids": ["a", "b"]})  # type: ignore[arg-type]

    assert [payload["current"] for payload in context.payloads] == [1, 1]


def test_evaluate_batch_keeps_evaluating_when_a_lane_progress_write_finds_sqlite_busy() -> None:
    context = _Context(busy_stage="posting_fetched")

    def report(_row_context: Any) -> dict[str, Any]:
        _row_context.progress("posting_fetched", {})
        return {"decision": "investigate"}

    evaluator = _Evaluator({"a": report, "b": {"decision": "apply_now"}})
    handler = DiscoveryEvaluateBatchHandler(evaluator, lanes=2)  # type: ignore[arg-type]

    result = handler(context, {"pending_discovery_ids": ["a", "b"]})  # type: ignore[arg-type]

    assert result["evaluated"] == 2
    assert result["failed"] == 0


class _ObservedQuotaError(QuotaError):
    """Out-of-credit error that marks when the batch reads its account scope."""

    def __init__(self, message: str, read: Event) -> None:
        super().__init__(message)
        self._read = read

    @property
    def account_level(self) -> bool:  # type: ignore[override]
        self._read.set()
        return True


def test_evaluate_batch_lets_running_rows_finish_after_an_account_error_and_starts_no_more() -> (
    None
):
    account_read = Event()

    def running_row(_: Any) -> dict[str, Any]:
        account_read.wait()
        return {"pending_discovery_id": "b", "decision": "investigate"}

    evaluator = _Evaluator(
        {
            "a": _ObservedQuotaError("Your Example account is out of credit.", account_read),
            "b": running_row,
            "c": {"decision": "apply_now"},
        }
    )
    context = _Context()
    handler = DiscoveryEvaluateBatchHandler(evaluator, lanes=2)  # type: ignore[arg-type]

    with pytest.raises(QuotaError):
        handler(context, {"pending_discovery_ids": ["a", "b", "c"]})  # type: ignore[arg-type]

    assert sorted(evaluator.seen) == ["a", "b"]
    summary = context.payloads[-1]
    assert summary["evaluated"] == 1
    assert summary["failed"] == 1
    assert summary["results"] == [{"pending_discovery_id": "b", "decision": "investigate"}]
    assert summary["failures"][0]["pending_discovery_id"] == "a"


@pytest.mark.parametrize("lanes", [0, 9])
def test_evaluate_batch_refuses_a_lane_count_outside_one_to_eight(lanes: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 8"):
        DiscoveryEvaluateBatchHandler(_Evaluator({}), lanes=lanes)  # type: ignore[arg-type]


def test_evaluate_batch_stops_at_an_out_of_credit_account_and_keeps_the_row_failure() -> None:
    """No later row can be paid for, so the sweep stops and reports what it did record."""
    evaluator = _Evaluator(
        {
            "a": QuotaError("Your Example account is out of credit.\nNothing was billed."),
            "b": {"decision": "investigate"},
        }
    )
    context = _Context()
    handler = DiscoveryEvaluateBatchHandler(evaluator)  # type: ignore[arg-type]

    with pytest.raises(QuotaError):
        handler(context, {"pending_discovery_ids": ["a", "b"]})  # type: ignore[arg-type]

    assert evaluator.seen == ["a"]
    assert context.payloads[-1]["failed"] == 1
    assert context.payloads[-1]["failures"] == [
        {
            "pending_discovery_id": "a",
            "error_category": "QuotaError",
            "error_message": "Your Example account is out of credit.\nNothing was billed.",
        }
    ]


class _Refreshed:
    """Stand in for the returned pending row; a withdrawal sets ``withdrawn_at``."""

    def __init__(self, *, withdrawn: bool) -> None:
        self.withdrawn_at = "2026-09-10T00:00:00Z" if withdrawn else None


class _ContentResolution:
    """Stand in for the content service, scripting one read outcome per row."""

    def __init__(
        self,
        outcomes: dict[str, str | Exception] | None = None,
        closed: set[str] | None = None,
    ) -> None:
        self.outcomes = outcomes or {}
        self.closed = closed or set()
        self.seen: list[str] = []

    def preflight(self, workspace_id: str, ids: list[str]) -> PostingResolutionPreflight:
        del workspace_id, ids
        return PostingResolutionPreflight(targets=(), snapshot_sha256="sha")

    def resolve_batch(
        self,
        workspace_id: str,
        pending_discovery_ids: list[str],
        snapshot_sha256: str,
        context: Any = None,
        *,
        origin: str,
        correlation_id: str | None = None,
        report_progress: bool = True,
    ) -> dict[str, Any]:
        del workspace_id, snapshot_sha256, origin, correlation_id
        if context is not None:
            context.raise_if_cancelled()
        assert report_progress is False, "the refresh handler reports its own stage"
        pending_id = pending_discovery_ids[0]
        self.seen.append(pending_id)
        outcome = self.outcomes.get(pending_id, "readable")
        if isinstance(outcome, Exception):
            raise outcome
        return {
            "selected": 1,
            "completed": 1,
            "outcomes": {outcome: 1},
            "evidence_ids": ["e-1"],
            "withdrawn_ids": [pending_id] if pending_id in self.closed else [],
            "kept_ids": [],
        }


_DAYFORCE_URL = DAYFORCE_POSTING_URL.format(reference="12345")
_OTHER_DAYFORCE_URL = DAYFORCE_POSTING_URL.format(reference="67890")


class _CheckingRefreshService:
    """Stand in for the refresh service, reading each row through the publisher check."""

    def __init__(self, client: Any, urls: dict[str, str]) -> None:
        self.client = client
        self.urls = urls
        self.checked: list[str] = []
        self.employers: list[str] = []

    def refresh_pending_discovery(
        self,
        workspace_id: str,
        pending_id: str,
        *,
        actor: str,
        origin: str,
        correlation_id: str | None,
    ) -> Any:
        del workspace_id, actor, origin, correlation_id
        checked = check_posting(
            self.urls[pending_id],
            client=cast(Any, self.client),
            intent=PostingReadIntent.SINGLE_POSTING,
        )
        self.checked.append(checked.title)
        self.employers.append(checked.employer)
        return _Refreshed(withdrawn=False)


class _RefreshService:
    def __init__(
        self,
        failures: set[str],
        withdrawn: set[str] | None = None,
        unsupported: set[str] | None = None,
        unverified: set[str] | None = None,
        dead: set[str] | None = None,
    ) -> None:
        self.failures = failures
        self.dead = dead or set()
        self.withdrawn = withdrawn or set()
        self.unsupported = unsupported or set()
        self.unverified = unverified or set()
        self.seen: list[str] = []

    def refresh_pending_discovery(
        self,
        workspace_id: str,
        pending_id: str,
        *,
        actor: str,
        origin: str,
        correlation_id: str | None,
    ) -> Any:
        self.seen.append(pending_id)
        if pending_id in self.unsupported:
            raise UnsupportedPublisherRefreshError("no free reader covers this publisher")
        if pending_id in self.unverified:
            raise PublisherUnverifiedRefreshError("Workday returned HTTP 403 (S22).")
        if pending_id in self.dead:
            raise DeadLinkRefreshError("Acme returned HTTP 404. This link is dead.")
        if pending_id in self.failures:
            raise ValidationError("cannot refresh")
        return _Refreshed(withdrawn=pending_id in self.withdrawn)

    def note_registered_board_holder(self, workspace_id: str, pending_id: str) -> None:
        del workspace_id, pending_id


def test_refresh_batch_reads_withdraws_and_records_a_failure() -> None:
    service = _RefreshService(failures={"b"}, withdrawn={"c"})
    content = _ContentResolution()
    handler = DiscoveryRefreshBatchHandler(service, content)  # type: ignore[arg-type]

    result = handler(_Context(), {"pending_discovery_ids": ["a", "b", "c"]})  # type: ignore[arg-type]

    assert result["refreshed"] == 1
    assert result["refreshed_ids"] == ["a"]
    assert result["withdrawn_as_closed"] == 1
    assert result["withdrawn_as_closed_ids"] == ["c"]
    assert result["failed"] == 1
    assert service.seen == ["a", "b", "c"]
    # A row whose publisher check failed or withdrew it is not a content target.
    assert content.seen == ["a"]


def test_refresh_batch_marks_only_a_dead_link_failure_conclusive() -> None:
    service = _RefreshService(failures={"b"}, dead={"a"})
    handler = DiscoveryRefreshBatchHandler(service, _ContentResolution())  # type: ignore[arg-type]

    result = handler(_Context(), {"pending_discovery_ids": ["a", "b"]})  # type: ignore[arg-type]

    assert result["failed"] == 2
    assert {f["pending_discovery_id"]: f["conclusive"] for f in result["failures"]} == {
        "a": True,
        "b": False,
    }


def test_refresh_batch_counts_an_unsupported_publisher_apart_from_a_failure() -> None:
    """A row on a publisher the free check does not cover is not a read failure."""
    service = _RefreshService(failures={"b"}, unsupported={"a"})
    handler = DiscoveryRefreshBatchHandler(service, _ContentResolution())  # type: ignore[arg-type]

    result = handler(_Context(), {"pending_discovery_ids": ["a", "b"]})  # type: ignore[arg-type]

    assert result["not_checkable"] == 1
    assert result["not_checkable_ids"] == ["a"]
    assert result["failed"] == 1
    assert result["failures"][0]["pending_discovery_id"] == "b"


def test_refresh_batch_reads_the_posting_text_of_every_checked_row() -> None:
    """One refresh re-reads the publisher's facts and the body behind them."""
    service = _RefreshService(failures=set(), unsupported={"b"})
    content = _ContentResolution({"b": "fetch_failed"})
    handler = DiscoveryRefreshBatchHandler(service, content)  # type: ignore[arg-type]

    result = handler(_Context(), {"pending_discovery_ids": ["a", "b"]})  # type: ignore[arg-type]

    assert content.seen == ["a", "b"]
    assert result["content_readable"] == 1
    assert result["content_unreadable"] == 1
    assert result["content_outcomes"] == {"readable": 1, "fetch_failed": 1}


def test_refresh_batch_counts_a_publisher_refusal_as_unverified_not_a_failure() -> None:
    """A publisher that answers without confirming the row has not failed to be read."""
    service = _RefreshService(failures={"b"}, unverified={"a"})
    handler = DiscoveryRefreshBatchHandler(service, _ContentResolution())  # type: ignore[arg-type]

    result = handler(_Context(), {"pending_discovery_ids": ["a", "b"]})  # type: ignore[arg-type]

    assert result["unverified"] == 1
    assert result["unverified_ids"] == ["a"]
    assert result["failed"] == 1
    assert result["failures"][0]["pending_discovery_id"] == "b"


def test_refresh_batch_reads_the_posting_text_of_a_row_the_publisher_refused() -> None:
    """An unconfirmed check is not a verdict, so the body read still decides the row."""
    service = _RefreshService(failures=set(), unverified={"a"})
    content = _ContentResolution({"a": "readable"})
    handler = DiscoveryRefreshBatchHandler(service, content)  # type: ignore[arg-type]

    result = handler(_Context(), {"pending_discovery_ids": ["a", "b"]})  # type: ignore[arg-type]

    assert content.seen == ["a", "b"]
    assert result["content_readable"] == 2


def test_refresh_batch_counts_a_closure_the_body_read_found_as_a_withdrawal() -> None:
    """A row the publisher check could not judge is still withdrawn when its body reads closed."""
    service = _RefreshService(failures=set(), unsupported={"a"}, unverified={"d"})
    content = _ContentResolution(
        {"a": "not_yet_read", "b": "not_yet_read", "d": "not_yet_read"}, closed={"a", "b", "d"}
    )
    handler = DiscoveryRefreshBatchHandler(service, content)  # type: ignore[arg-type]

    result = handler(_Context(), {"pending_discovery_ids": ["a", "b", "c", "d"]})  # type: ignore[arg-type]

    assert result["withdrawn_as_closed_ids"] == ["a", "b", "d"]
    assert result["withdrawn_as_closed"] == 3
    assert result["not_checkable_ids"] == []
    assert result["unverified_ids"] == []
    assert result["refreshed_ids"] == ["c"]


def test_refresh_batch_puts_every_selected_row_in_exactly_one_outcome() -> None:
    """The counts add up to the selection, so no row is double-counted or dropped."""
    selected = ["a", "b", "c", "d", "e"]
    service = _RefreshService(failures={"b"}, withdrawn={"c"}, unsupported={"d"}, unverified={"e"})
    content = _ContentResolution(
        {
            "a": ValidationError("content changed"),
            "d": ValidationError("content changed"),
            "e": "readable",
        }
    )
    handler = DiscoveryRefreshBatchHandler(service, content)  # type: ignore[arg-type]

    result = handler(_Context(), {"pending_discovery_ids": selected})  # type: ignore[arg-type]

    landed = [
        *result["refreshed_ids"],
        *result["withdrawn_as_closed_ids"],
        *result["not_checkable_ids"],
        *result["unverified_ids"],
        *[failure["pending_discovery_id"] for failure in result["failures"]],
    ]
    assert sorted(landed) == selected
    assert result["selected"] == len(selected)
    # The read errors on rows the publisher already placed are reported, not re-bucketed.
    assert [failure["pending_discovery_id"] for failure in result["content_failures"]] == ["a", "d"]


def test_refresh_batch_refreshes_a_row_whose_publisher_disallows_crawling() -> None:
    """The row's address is already in hand, so its check is a person's read, not a crawl."""
    rows = [dayforce_feed_row("12345", "Clinical Data Manager")]
    client = policy_client(
        lambda url: httpx.Response(200, request=httpx.Request("GET", url), json=rows),
        robots="User-agent: *\nDisallow: /\n",
    )
    service = _CheckingRefreshService(client, {"a": _DAYFORCE_URL})
    handler = DiscoveryRefreshBatchHandler(service, _ContentResolution())  # type: ignore[arg-type]

    result = handler(_Context(), {"pending_discovery_ids": ["a"]})  # type: ignore[arg-type]

    assert result["refreshed_ids"] == ["a"]
    assert service.employers == ["Acme Health"]


def test_refresh_batch_reads_one_site_feed_once_for_every_row_on_it() -> None:
    """Two rows on a feed-only publisher share the site read their pass already made."""
    client = CountingFeedClient(
        [
            dayforce_feed_row("12345", "Clinical Data Manager"),
            dayforce_feed_row("67890", "Staff Data Engineer"),
        ]
    )
    service = _CheckingRefreshService(client, {"a": _DAYFORCE_URL, "b": _OTHER_DAYFORCE_URL})
    handler = DiscoveryRefreshBatchHandler(service, _ContentResolution())  # type: ignore[arg-type]

    handler(_Context(), {"pending_discovery_ids": ["a", "b"]})  # type: ignore[arg-type]

    assert service.checked == ["Clinical Data Manager", "Staff Data Engineer"]
    assert len(client.requested) == 1


def test_refresh_batch_records_a_content_read_failure_and_keeps_going() -> None:
    """A failed body read leaves the row's publisher outcome and reports the read error."""
    service = _RefreshService(failures=set())
    content = _ContentResolution({"a": ValidationError("content changed")})
    handler = DiscoveryRefreshBatchHandler(service, content)  # type: ignore[arg-type]

    result = handler(_Context(), {"pending_discovery_ids": ["a", "b"]})  # type: ignore[arg-type]

    assert result["refreshed"] == 2
    assert result["failed"] == 0
    assert result["content_failures"][0]["pending_discovery_id"] == "a"
    assert result["content_failures"][0]["error_message"] == "content changed"
    assert result["content_readable"] == 1


_DEAD_URL = "https://listings.example.test/view/operations-lead-4821"
_BOARD_URL = "https://globex.example.test/careers/jobs/operations-lead"


class _DeadLinkResolver:
    """Answer posting reads as dead links, or as readable bodies at the named addresses."""

    def __init__(self, readable: frozenset[str] = frozenset()) -> None:
        self.readable = readable
        self.urls: list[str] = []

    def resolve(
        self,
        url: str,
        *,
        employer: str,
        title: str,
        intent: PostingReadIntent = PostingReadIntent.CRAWL,
        board_url: str = "",
    ) -> ResolvedPostingContent:
        del employer, title, intent
        self.urls.append(url)
        if url in self.readable:
            return ResolvedPostingContent(
                requested_url=url,
                final_url=url,
                content_status=PostingContentAccessStatus.READABLE,
                identity_status=PostingIdentityStatus.VERIFIED,
                posting_status=PostingStatus.ACTIVE,
                extraction_method=PostingContentExtractionMethod.STATIC_STRUCTURED,
                fetched_at=utc_now(),
                content=b"Operations Lead\nResponsibilities\nRun the regional operations team.",
                http_status=200,
            )
        return ResolvedPostingContent(
            requested_url=url,
            final_url=None,
            content_status=PostingContentAccessStatus.FETCH_FAILED,
            identity_status=PostingIdentityStatus.NOT_YET_VERIFIED,
            posting_status=None,
            extraction_method=None,
            fetched_at=utc_now(),
            http_status=404,
            note="The listing host returned HTTP 404.",
        )


_UNSUPPORTED = PostingCheck(status="unsupported", note="No free reader covers this listing host.")


def _scan_service(
    database: Database, tmp_path: Path, check: PostingCheck = _UNSUPPORTED
) -> SourceScanService:
    return SourceScanService(
        database,
        storage=runtime_storage(database, tmp_path),
        posting_fetcher=UnexpectedPostingFetcher(),
        posting_checker=lambda _url, **_: check,
    )


def _read_ready(database: Database, tmp_path: Path, workspace_id: str, pending_id: str) -> None:
    """Retain a verified active body for one row at its own address."""
    with database.session() as session:
        row = session.get(PendingDiscovery, pending_id)
        assert row is not None
        url = row.canonical_url or row.discovery_url
    content = PostingContentResolutionService(
        database,
        _DeadLinkResolver(readable=frozenset({url})),
        storage=runtime_storage(database, tmp_path),
    )
    snapshot = content.preflight(workspace_id, [pending_id]).snapshot_sha256
    content.resolve_batch(workspace_id, [pending_id], snapshot, origin="discovery.refresh_batch")


def _refresh_for_real(
    database: Database,
    tmp_path: Path,
    workspace_id: str,
    pending_id: str,
    check: PostingCheck = _UNSUPPORTED,
) -> tuple[dict[str, Any], _DeadLinkResolver]:
    resolver = _DeadLinkResolver()
    content = PostingContentResolutionService(
        database, resolver, storage=runtime_storage(database, tmp_path)
    )
    handler = DiscoveryRefreshBatchHandler(_scan_service(database, tmp_path, check), content)
    context = _Context(queue=_Queue(_Task(workspace_id, datetime(2026, 9, 25, tzinfo=UTC))))
    result = handler(context, {"pending_discovery_ids": [pending_id]})  # type: ignore[arg-type]
    return result, resolver


def _row(database: Database, pending_id: str) -> PendingDiscovery:
    with database.session() as session:
        row = session.get(PendingDiscovery, pending_id)
        assert row is not None
        session.expunge(row)
        return row


def test_refresh_names_the_tracked_board_posting_that_holds_an_unreadable_rows_role(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = scan_workspace(durable_database, slug="board-held")
    board = employer_board(durable_database, workspace_id, "Globex")
    job_id = tracked_board_posting(
        durable_database,
        workspace_id,
        board,
        title="Operations Lead",
        url=_BOARD_URL,
        location="Austin, Texas",
    )
    pending_id = seed_unread_row(
        durable_database,
        workspace_id,
        employer="Globex",
        title="Operations Lead (Austin, TX)",
        url=_DEAD_URL,
    )

    result, resolver = _refresh_for_real(durable_database, tmp_path, workspace_id, pending_id)

    assert result["tracked_on_registered_board_holders"] == [
        {
            "pending_discovery_id": pending_id,
            "holder_type": "job_posting",
            "holder_id": job_id,
            "holder_url": _BOARD_URL,
        }
    ]
    # Only the row's own address was read; the board posting was found from what is stored.
    assert resolver.urls == [_DEAD_URL]
    row = _row(durable_database, pending_id)
    assert row.canonical_url == ""
    assert job_id in row.content_note
    assert row.content_status == PostingContentAccessStatus.FETCH_FAILED


_ASHBY_JOB = "2f0c6a1e-5b7d-4c3e-9a8f-1d2e3f4a5b6c"


@pytest.mark.parametrize(
    ("tracked_url", "held"),
    [
        (f"https://jobs.ashbyhq.com/globex/{_ASHBY_JOB}", True),
        (f"https://jobs.ashbyhq.com/GLOBEX/{_ASHBY_JOB}/", True),
        (f"https://jobs.ashbyhq.com/initech/{_ASHBY_JOB}", False),
    ],
)
def test_refresh_names_a_posting_tracked_on_the_board_before_the_board_was_registered(
    durable_database: Database, tmp_path: Path, tracked_url: str, held: bool
) -> None:
    workspace_id = scan_workspace(durable_database, slug="board-unattributed")
    board = board_source(
        durable_database, workspace_id, "Globex", "https://jobs.ashbyhq.com/globex"
    )
    job_id = tracked_board_posting(
        durable_database, workspace_id, board, title="Operations Lead", url=tracked_url
    )
    with durable_database.session() as session:
        session.execute(
            update(PostingUrl).where(PostingUrl.job_posting_id == job_id).values(source_id=None)
        )
    pending_id = seed_unread_row(
        durable_database, workspace_id, employer="Globex", title="Operations Lead", url=_DEAD_URL
    )

    result, _resolver = _refresh_for_real(durable_database, tmp_path, workspace_id, pending_id)

    expected = [
        {
            "pending_discovery_id": pending_id,
            "holder_type": "job_posting",
            "holder_id": job_id,
            "holder_url": tracked_url,
        }
    ]
    assert result["tracked_on_registered_board_holders"] == (expected if held else [])


def test_one_tracked_job_at_two_board_addresses_is_still_the_one_holder(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = scan_workspace(durable_database, slug="board-two-addresses")
    board = board_source(
        durable_database, workspace_id, "Globex", "https://jobs.ashbyhq.com/globex"
    )
    board_url = f"https://jobs.ashbyhq.com/globex/{_ASHBY_JOB}"
    job_id = tracked_board_posting(
        durable_database, workspace_id, board, title="Operations Lead", url=board_url
    )
    with durable_database.session() as session:
        variant = f"{board_url}/application"
        session.add(
            PostingUrl(
                job_posting_id=job_id,
                source_id=None,
                url_type="canonical",
                url=variant,
                normalized_url=normalize_url(variant),
                is_primary=False,
            )
        )
    pending_id = seed_unread_row(
        durable_database, workspace_id, employer="Globex", title="Operations Lead", url=_DEAD_URL
    )

    result, _resolver = _refresh_for_real(durable_database, tmp_path, workspace_id, pending_id)

    assert result["tracked_on_registered_board_holders"] == [
        {
            "pending_discovery_id": pending_id,
            "holder_type": "job_posting",
            "holder_id": job_id,
            "holder_url": board_url,
        }
    ]


def test_refresh_refuses_a_board_posting_that_states_another_place(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = scan_workspace(durable_database, slug="board-elsewhere")
    board = employer_board(durable_database, workspace_id, "Globex")
    tracked_board_posting(
        durable_database,
        workspace_id,
        board,
        title="Operations Lead",
        url=_BOARD_URL,
        location="Denver, Colorado",
    )
    pending_id = seed_unread_row(
        durable_database,
        workspace_id,
        employer="Globex",
        title="Operations Lead (Austin, TX)",
        url=_DEAD_URL,
    )

    result, _resolver = _refresh_for_real(durable_database, tmp_path, workspace_id, pending_id)

    assert result["tracked_on_registered_board_holders"] == []
    assert _row(durable_database, pending_id).canonical_url == ""


def test_refresh_refuses_a_role_two_of_the_employers_boards_hold(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = scan_workspace(durable_database, slug="board-twice")
    board = employer_board(durable_database, workspace_id, "Globex")
    with durable_database.session() as session:
        first = session.get(Source, board)
        assert first is not None
        second = Source(
            workspace_id=workspace_id,
            employer_id=first.employer_id,
            name="Globex campus careers",
            source_type="employer_board",
            url="https://campus.globex.example.test/careers",
            priority=2,
            enabled=True,
        )
        session.add(second)
        session.flush()
        second_id = second.id
    for source_id, url in ((board, _BOARD_URL), (second_id, f"{_BOARD_URL}-campus")):
        tracked_board_posting(
            durable_database, workspace_id, source_id, title="Operations Lead", url=url
        )
    pending_id = seed_unread_row(
        durable_database, workspace_id, employer="Globex", title="Operations Lead", url=_DEAD_URL
    )

    result, _resolver = _refresh_for_real(durable_database, tmp_path, workspace_id, pending_id)

    assert result["tracked_on_registered_board_holders"] == []
    assert _row(durable_database, pending_id).canonical_url == ""


def test_refresh_reports_a_live_board_row_holding_the_role_and_leaves_both_rows(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = scan_workspace(durable_database, slug="board-pending")
    board = employer_board(durable_database, workspace_id, "Globex")
    board_row = seed_unread_row(
        durable_database,
        workspace_id,
        employer="Globex",
        title="Operations Lead",
        url=_BOARD_URL,
        location="Austin, TX",
        source_id=board,
    )
    _read_ready(durable_database, tmp_path, workspace_id, board_row)
    pending_id = seed_unread_row(
        durable_database,
        workspace_id,
        employer="Globex",
        title="Operations Lead (Remote)",
        url=_DEAD_URL,
    )

    result, _resolver = _refresh_for_real(durable_database, tmp_path, workspace_id, pending_id)

    assert result["tracked_on_registered_board_holders"] == [
        {
            "pending_discovery_id": pending_id,
            "holder_type": "pending_discovery",
            "holder_id": board_row,
            "holder_url": _BOARD_URL,
        }
    ]
    held = _row(durable_database, board_row)
    assert (held.withdrawn_at, held.archived_at, held.canonical_url) == (None, None, "")
    assert _row(durable_database, pending_id).canonical_url == ""


def test_refresh_keeps_a_team_parenthetical_as_part_of_the_title(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = scan_workspace(durable_database, slug="board-team")
    board = employer_board(durable_database, workspace_id, "Globex")
    tracked_board_posting(
        durable_database, workspace_id, board, title="Account Executive", url=_BOARD_URL
    )
    pending_id = seed_unread_row(
        durable_database,
        workspace_id,
        employer="Globex",
        title="Account Executive (Enterprise)",
        url=_DEAD_URL,
    )

    result, _resolver = _refresh_for_real(durable_database, tmp_path, workspace_id, pending_id)

    assert result["tracked_on_registered_board_holders"] == []


def test_refresh_does_not_take_a_board_posting_stating_no_place_for_a_placed_row(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = scan_workspace(durable_database, slug="board-unplaced")
    board = employer_board(durable_database, workspace_id, "Globex")
    tracked_board_posting(
        durable_database, workspace_id, board, title="Operations Lead", url=_BOARD_URL
    )
    pending_id = seed_unread_row(
        durable_database,
        workspace_id,
        employer="Globex",
        title="Operations Lead (Austin, TX)",
        url=_DEAD_URL,
    )

    result, _resolver = _refresh_for_real(durable_database, tmp_path, workspace_id, pending_id)

    assert result["tracked_on_registered_board_holders"] == []


def test_refresh_does_not_let_two_unreadable_board_twins_hold_each_other(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = scan_workspace(durable_database, slug="board-twins")
    board = employer_board(durable_database, workspace_id, "Globex")
    twin = seed_unread_row(
        durable_database,
        workspace_id,
        employer="Globex",
        title="Operations Lead",
        url=_BOARD_URL,
        source_id=board,
    )
    pending_id = seed_unread_row(
        durable_database,
        workspace_id,
        employer="Globex",
        title="Operations Lead",
        url=_DEAD_URL,
        source_id=board,
    )

    first, _resolver = _refresh_for_real(durable_database, tmp_path, workspace_id, pending_id)
    second, _resolver = _refresh_for_real(durable_database, tmp_path, workspace_id, twin)

    assert first["tracked_on_registered_board_holders"] == []
    assert second["tracked_on_registered_board_holders"] == []


def test_refresh_does_not_name_a_dismissed_board_row_as_the_holder(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = scan_workspace(durable_database, slug="board-dismissed")
    board = employer_board(durable_database, workspace_id, "Globex")
    dismissed = seed_unread_row(
        durable_database,
        workspace_id,
        employer="Globex",
        title="Operations Lead",
        url=_BOARD_URL,
        source_id=board,
    )
    _read_ready(durable_database, tmp_path, workspace_id, dismissed)
    dismiss_pending_discovery(durable_database, workspace_id, dismissed)
    pending_id = seed_unread_row(
        durable_database, workspace_id, employer="Globex", title="Operations Lead", url=_DEAD_URL
    )

    result, _resolver = _refresh_for_real(durable_database, tmp_path, workspace_id, pending_id)

    assert result["tracked_on_registered_board_holders"] == []


def test_a_row_with_usable_evidence_is_not_noted_as_held_on_the_board(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = scan_workspace(durable_database, slug="board-ready")
    board = employer_board(durable_database, workspace_id, "Globex")
    tracked_board_posting(
        durable_database, workspace_id, board, title="Operations Lead", url=_BOARD_URL
    )
    pending_id = seed_unread_row(
        durable_database, workspace_id, employer="Globex", title="Operations Lead", url=_DEAD_URL
    )
    _read_ready(durable_database, tmp_path, workspace_id, pending_id)
    note = _row(durable_database, pending_id).content_note

    holder = _scan_service(durable_database, tmp_path).note_registered_board_holder(
        workspace_id, pending_id
    )

    assert holder is None
    assert _row(durable_database, pending_id).content_note == note


def test_refresh_counts_a_row_whose_failed_reread_kept_its_body_as_readable(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = scan_workspace(durable_database, slug="refresh-kept")
    pending_id = seed_unread_row(
        durable_database, workspace_id, employer="Globex", title="Operations Lead", url=_DEAD_URL
    )
    _read_ready(durable_database, tmp_path, workspace_id, pending_id)

    result, resolver = _refresh_for_real(durable_database, tmp_path, workspace_id, pending_id)

    assert resolver.urls == [_DEAD_URL]
    assert _row(durable_database, pending_id).content_status == PostingContentAccessStatus.READABLE
    assert result["content_outcomes"] == {"readable": 1}
    assert (result["content_readable"], result["content_unreadable"]) == (1, 0)
    assert result["content_kept"] == 1
    assert result["tracked_on_registered_board_holders"] == []


def test_refresh_counts_a_closed_read_of_an_already_withdrawn_row_as_closed(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = scan_workspace(durable_database, slug="refresh-closed-twice")
    pending_id = seed_unread_row(
        durable_database, workspace_id, employer="Globex", title="Operations Lead", url=_DEAD_URL
    )
    closed = PostingCheck(status="closed", note="The publisher lists this job as closed.")
    _refresh_for_real(durable_database, tmp_path, workspace_id, pending_id, closed)
    withdrawn_at = _row(durable_database, pending_id).withdrawn_at
    assert withdrawn_at is not None

    result, resolver = _refresh_for_real(
        durable_database, tmp_path, workspace_id, pending_id, closed
    )

    assert result["failures"] == []
    assert result["withdrawn_as_closed_ids"] == [pending_id]
    assert resolver.urls == []
    assert _row(durable_database, pending_id).withdrawn_at == withdrawn_at
    with durable_database.session() as session:
        withdrawals = session.scalars(
            select(AuditEvent.id).where(
                AuditEvent.entity_id == pending_id,
                AuditEvent.action == "pending_discovery.withdrawn_on_refresh",
            )
        ).all()
    assert len(withdrawals) == 1


def test_evaluate_batch_refuses_duplicate_rows() -> None:
    handler = DiscoveryEvaluateBatchHandler(_Evaluator({}))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unique"):
        handler(_Context(), {"pending_discovery_ids": ["a", "a"]})  # type: ignore[arg-type]


def test_evaluate_batch_reevaluates_tracked_postings() -> None:
    evaluator = _Evaluator(
        {
            "job-1": {"decision": "apply_now"},
            "job-2": {"decision": "investigate"},
        }
    )
    handler = DiscoveryEvaluateBatchHandler(evaluator)  # type: ignore[arg-type]

    result = handler(_Context(), {"job_posting_ids": ["job-1", "job-2"]})  # type: ignore[arg-type]

    assert result["selected"] == 2
    assert result["evaluated"] == 2
    assert result["failed"] == 0
    assert evaluator.seen == ["job-1", "job-2"]


def test_evaluate_batch_keys_a_posting_failure_by_job_posting_id() -> None:
    evaluator = _Evaluator(
        {
            "job-1": ValidationError("bad posting"),
            "job-2": {"decision": "investigate"},
        }
    )
    handler = DiscoveryEvaluateBatchHandler(evaluator)  # type: ignore[arg-type]

    result = handler(_Context(), {"job_posting_ids": ["job-1", "job-2"]})  # type: ignore[arg-type]

    assert result["evaluated"] == 1
    assert result["failed"] == 1
    assert result["failures"][0]["job_posting_id"] == "job-1"
    assert "pending_discovery_id" not in result["failures"][0]


def test_evaluate_batch_refuses_two_target_kinds() -> None:
    handler = DiscoveryEvaluateBatchHandler(_Evaluator({}))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="exactly one target kind"):
        handler(
            _Context(),  # type: ignore[arg-type]
            {"pending_discovery_ids": ["a"], "job_posting_ids": ["job-1"]},
        )


def test_evaluate_batch_refuses_no_target() -> None:
    handler = DiscoveryEvaluateBatchHandler(_Evaluator({}))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="exactly one target kind"):
        handler(_Context(), {})  # type: ignore[arg-type]


def test_rows_naming_one_posting_by_requisition_or_address_share_a_group(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = scan_workspace(durable_database, slug="same-posting")
    stated = [
        ("Globex", "REQ-7", "https://jobs.example.com/globex/7"),
        ("globex", "req-7", "https://jobs.example.com/globex/7-copy"),
        ("Acme Freight", "", "https://jobs.example.com/acme/40"),
        ("Acme Freight", "", "https://www.jobs.example.com/acme/40/"),
        ("Globex", "REQ-8", "https://jobs.example.com/globex/8"),
        ("Acme Freight", "REQ-7", "https://jobs.example.com/acme/7"),
    ]
    ids: list[str] = []
    with durable_database.session() as session:
        for employer, requisition_id, url in stated:
            row = pending_discovery(workspace_id, employer=employer, created_at=datetime.now(UTC))
            row.requisition_id = requisition_id
            row.discovery_url = url
            row.canonical_url = ""
            session.add(row)
            ids.append(str(row.id))
    service = EvaluationWorkflowService(
        durable_database,
        runtime_storage(durable_database, tmp_path),
        fetcher=UnexpectedPostingFetcher(),
    )

    groups = EvaluationHandler(service).posting_groups(workspace_id, ids)

    first, second, third, fourth, fifth, sixth = ids
    assert groups == {
        first: first,
        second: first,
        third: third,
        fourth: third,
        fifth: fifth,
        sixth: sixth,
    }
