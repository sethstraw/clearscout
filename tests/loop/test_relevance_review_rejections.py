"""A paid review that recorded no verdict marks the discovery it could not review."""

import contextlib
import threading
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from clearscout.application.discovery.hints import PendingDiscoveryHintsService
from clearscout.application.discovery.relevance_review_rejections import (
    RelevanceReviewItemFailure,
    RelevanceReviewRejectionService,
)
from clearscout.application.discovery.scan_queries import SourceScanQueries
from clearscout.application.job_search.models import PendingDiscoveryResponse
from clearscout.application.tasks import TaskQueueService
from clearscout.domain import TaskStatus
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    AuditEvent,
    BackgroundTask,
    RelevanceReviewRejection,
)
from tests.support.scenarios import workspace_with_retained_postings

BODY = "Data Engineer\n\nGlobex builds streaming pipelines for commerce ledgers."


def _review_task(database: Database, workspace_id: str, pending_id: str, key: str) -> str:
    """Enqueue one relevance-review task over the given discovery."""
    return (
        TaskQueueService(database)
        .enqueue(
            workspace_id=workspace_id,
            task_type="discovery.relevance_review",
            payload={"pending_discovery_ids": [pending_id]},
            idempotency_key=key,
        )
        .id
    )


def _workspace(database: Database, tmp_path: Path, slug: str) -> tuple[str, str, str]:
    """Return a workspace, one retained pending discovery, and one review task."""
    workspace_id, (pending_id,) = workspace_with_retained_postings(
        database, tmp_path, [("Data Engineer", BODY)], slug=slug
    )
    return (
        workspace_id,
        pending_id,
        _review_task(database, workspace_id, pending_id, f"{slug}-review"),
    )


def _row(database: Database, workspace_id: str, pending_id: str) -> PendingDiscoveryResponse:
    return SourceScanQueries(database).get_pending_discovery(workspace_id, pending_id)


def test_a_rejected_review_marks_the_row_with_the_reason_it_recorded_nothing(
    durable_database: Database, tmp_path: Path
) -> None:
    """The queue can tell a row a paid review dropped from one never reviewed."""
    workspace_id, pending_id, task_id = _workspace(durable_database, tmp_path, "rejection-marked")
    unmarked = _row(durable_database, workspace_id, pending_id)

    RelevanceReviewRejectionService(durable_database).record(
        workspace_id=workspace_id,
        task_id=task_id,
        rejected=[
            RelevanceReviewItemFailure(
                pending_id,
                "invalid_cv_evidence",
                "The result cited evidence outside the selected master CV.",
            )
        ],
    )

    assert unmarked.relevance_review_rejection is None
    marked = _row(durable_database, workspace_id, pending_id).relevance_review_rejection
    assert marked is not None
    assert marked.failure_code == "invalid_cv_evidence"
    assert marked.detail == "The result cited evidence outside the selected master CV."
    assert marked.task_id == task_id


def test_a_later_rejection_replaces_the_mark_the_row_already_carried(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, task_id = _workspace(durable_database, tmp_path, "rejection-replaced")
    service = RelevanceReviewRejectionService(durable_database)
    service.record(
        workspace_id=workspace_id,
        task_id=task_id,
        rejected=[RelevanceReviewItemFailure(pending_id, "missing_result", "Nothing came back.")],
    )

    service.record(
        workspace_id=workspace_id,
        task_id=task_id,
        rejected=[
            RelevanceReviewItemFailure(
                pending_id, "leading_excerpt_only", "The result cited only the leading excerpt."
            )
        ],
    )

    marked = _row(durable_database, workspace_id, pending_id).relevance_review_rejection
    assert marked is not None and marked.failure_code == "leading_excerpt_only"


def test_a_replaced_mark_names_the_task_that_replaced_it_in_its_audit_columns(
    durable_database: Database, tmp_path: Path
) -> None:
    """A mark whose audit provenance still names the first task cannot be traced."""
    workspace_id, pending_id, first_task = _workspace(
        durable_database, tmp_path, "rejection-correlated"
    )
    second_task = _review_task(durable_database, workspace_id, pending_id, "rejection-second")
    service = RelevanceReviewRejectionService(durable_database)
    service.record(
        workspace_id=workspace_id,
        task_id=first_task,
        rejected=[RelevanceReviewItemFailure(pending_id, "missing_result", "Nothing came back.")],
    )

    service.record(
        workspace_id=workspace_id,
        task_id=second_task,
        rejected=[RelevanceReviewItemFailure(pending_id, "duplicate_result", "Two answers.")],
    )

    with durable_database.session() as session:
        row = session.scalars(select(RelevanceReviewRejection)).one()
    assert (row.task_id, row.correlation_id) == (second_task, second_task)


def test_two_passes_marking_the_same_row_leave_one_mark_and_no_error(
    durable_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worker passes overlap, so the read and the write it depends on take one lock."""
    workspace_id, pending_id, task_id = _workspace(durable_database, tmp_path, "rejection-race")
    # Both passes read before either writes, the interleaving a plain session allows.
    gate = threading.Barrier(2, timeout=1)
    read_standing = RelevanceReviewRejectionService._standing

    def gated(session: Session, workspace: str, pending: str) -> RelevanceReviewRejection | None:
        row = read_standing(session, workspace, pending)
        with contextlib.suppress(threading.BrokenBarrierError):
            gate.wait()
        return row

    monkeypatch.setattr(RelevanceReviewRejectionService, "_standing", staticmethod(gated))
    failures: list[BaseException] = []

    def mark() -> None:
        try:
            RelevanceReviewRejectionService(durable_database).record(
                workspace_id=workspace_id,
                task_id=task_id,
                rejected=[
                    RelevanceReviewItemFailure(pending_id, "missing_result", "Nothing came back.")
                ],
            )
        except BaseException as error:  # noqa: BLE001
            failures.append(error)

    passes = [threading.Thread(target=mark) for _ in range(2)]
    for worker in passes:
        worker.start()
    for worker in passes:
        worker.join(timeout=60)

    assert [type(error).__name__ for error in failures] == []
    with durable_database.session() as session:
        assert len(session.scalars(select(RelevanceReviewRejection)).all()) == 1


def test_correcting_the_title_clears_the_mark_with_the_reviews_it_stood_beside(
    durable_database: Database, tmp_path: Path
) -> None:
    """A mark about a title the person replaced would misreport the row it sorts in."""
    workspace_id, pending_id, task_id = _workspace(durable_database, tmp_path, "rejection-retitled")
    RelevanceReviewRejectionService(durable_database).record(
        workspace_id=workspace_id,
        task_id=task_id,
        rejected=[RelevanceReviewItemFailure(pending_id, "missing_result", "Nothing came back.")],
    )
    with durable_database.session() as session:
        session.get(BackgroundTask, task_id).status = TaskStatus.COMPLETED  # type: ignore[union-attr]

    PendingDiscoveryHintsService(durable_database).correct_title(
        workspace_id, pending_id, "Staff Data Engineer"
    )

    assert _row(durable_database, workspace_id, pending_id).relevance_review_rejection is None
    with durable_database.session() as session:
        actions = session.scalars(
            select(AuditEvent.action).where(
                AuditEvent.workspace_id == workspace_id,
                AuditEvent.entity_type == "relevance_review_rejection",
            )
        ).all()
    assert list(actions) == [
        "relevance_review_rejection.marked",
        "relevance_review_rejection.cleared",
    ]


def test_the_queue_page_carries_the_mark_only_on_the_row_that_holds_it(
    durable_database: Database, tmp_path: Path
) -> None:
    """The page projection resolves marks per row, not only the single-row read."""
    workspace_id, pending_ids = workspace_with_retained_postings(
        durable_database,
        tmp_path,
        [("Data Engineer", BODY), ("Platform Engineer", BODY)],
        slug="rejection-page",
    )
    marked_id, unmarked_id = pending_ids
    task_id = _review_task(durable_database, workspace_id, marked_id, "rejection-page-review")
    RelevanceReviewRejectionService(durable_database).record(
        workspace_id=workspace_id,
        task_id=task_id,
        rejected=[
            RelevanceReviewItemFailure(marked_id, "duplicate_result", "Two answers came back.")
        ],
    )

    page = SourceScanQueries(durable_database).pending_discovery_page(workspace_id)

    marks = {
        item.id: item.relevance_review_rejection
        for item in (*page.ready.items, *page.attention.items)
    }
    marked = marks[marked_id]
    assert marks[unmarked_id] is None
    assert marked is not None
    assert marked.failure_code == "duplicate_result"


def test_an_accepted_review_clears_the_mark_without_any_retry(
    durable_database: Database, tmp_path: Path
) -> None:
    """Only a review the person paid for again clears the mark; nothing re-runs on its own."""
    workspace_id, pending_id, task_id = _workspace(durable_database, tmp_path, "rejection-cleared")
    service = RelevanceReviewRejectionService(durable_database)
    service.record(
        workspace_id=workspace_id,
        task_id=task_id,
        rejected=[RelevanceReviewItemFailure(pending_id, "missing_result", "Nothing came back.")],
    )
    assert _row(durable_database, workspace_id, pending_id).relevance_review_rejection is not None

    service.record(
        workspace_id=workspace_id, task_id=task_id, rejected=[], accepted_ids=[pending_id]
    )

    assert _row(durable_database, workspace_id, pending_id).relevance_review_rejection is None


def test_every_rejection_change_writes_an_audit_event(
    durable_database: Database, tmp_path: Path
) -> None:
    """A mark that lands with no audit trail cannot be recovered later."""
    workspace_id, pending_id, task_id = _workspace(durable_database, tmp_path, "rejection-audit")
    service = RelevanceReviewRejectionService(durable_database)

    service.record(
        workspace_id=workspace_id,
        task_id=task_id,
        rejected=[RelevanceReviewItemFailure(pending_id, "duplicate_result", "Two answers.")],
    )
    service.record(
        workspace_id=workspace_id, task_id=task_id, rejected=[], accepted_ids=[pending_id]
    )

    with durable_database.session() as session:
        events = session.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.workspace_id == workspace_id,
                AuditEvent.entity_type == "relevance_review_rejection",
            )
            .order_by(AuditEvent.created_at)
        ).all()
    assert [event.action for event in events] == [
        "relevance_review_rejection.marked",
        "relevance_review_rejection.cleared",
    ]
