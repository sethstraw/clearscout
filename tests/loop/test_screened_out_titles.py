"""Screened-out titles stay reviewable across scans, keyed by their normalized address."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import count

import pytest
from sqlalchemy import delete

from clearscout.application.discovery import screened_out_titles
from clearscout.application.discovery.board_scan import SourceScanService
from clearscout.application.failures import ValidationError
from clearscout.application.job_search.models import ScreenedOutTitleResponse
from clearscout.application.tasks import TaskQueueService
from clearscout.application.workspaces import SearchParametersService
from clearscout.bootstrap.worker import Worker, default_handlers
from clearscout.domain.search_parameters import JobSearchParameters, Seniority
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import ScreenedOutTitle
from tests.support.artifacts import runtime_storage
from tests.support.doubles import ScriptedBoards, UnexpectedPostingFetcher
from tests.support.scenarios import (
    board_source,
    scan_workspace,
    source_listing,
    state_target_titles,
    titles,
)

_BOARD = "https://boards.greenhouse.io/acme"
_SALES = f"{_BOARD}/jobs/3"
_FIRST = datetime(2025, 3, 1, 9, tzinfo=UTC)
_SECOND = datetime(2025, 3, 2, 9, tzinfo=UTC)
_SCANS = count()


def _boards() -> ScriptedBoards:
    return ScriptedBoards(
        {
            "Acme": [
                source_listing("Data Platform Lead", f"{_BOARD}/jobs/1", employer="Acme"),
                source_listing("Sales Director", _SALES, employer="Acme"),
            ]
        }
    )


def _scanner(database: Database) -> SourceScanService:
    return SourceScanService(
        database,
        storage=runtime_storage(database),
        fetcher=_boards(),
        posting_fetcher=UnexpectedPostingFetcher(),
    )


def _workspace(database: Database) -> str:
    workspace_id = scan_workspace(database)
    board_source(database, workspace_id, "Acme", _BOARD)
    state_target_titles(database, workspace_id, "data")
    return workspace_id


def _scan_at(
    monkeypatch: pytest.MonkeyPatch, service: SourceScanService, workspace_id: str, when: datetime
) -> None:
    """Run a scan as the worker does, since only a scan task records outcomes."""
    monkeypatch.setattr(screened_out_titles, "utc_now", lambda: when)
    queue = TaskQueueService(service.database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={},
        idempotency_key=f"screened-out-{next(_SCANS)}",
    )
    worker = Worker(
        service.database,
        worker_id="screened-out",
        handlers=default_handlers(service.database, fetcher=_boards()),
    )
    assert worker.run_once()
    finished = queue.get(str(task.id))
    assert finished.status == "completed", (finished.error_category, finished.error_message)


def _sales_row(service: SourceScanService, workspace_id: str) -> ScreenedOutTitleResponse:
    page = service.list_screened_out_titles(workspace_id)
    return next(item for item in page.items if item.url == _SALES)


def test_two_scans_of_one_screened_out_listing_keep_one_row_seen_first_and_last(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = _workspace(durable_database)
    service = _scanner(durable_database)

    _scan_at(monkeypatch, service, workspace_id, _FIRST)
    _scan_at(monkeypatch, service, workspace_id, _SECOND)

    page = service.list_screened_out_titles(workspace_id)
    assert [item.url for item in page.items] == [_SALES]
    (row,) = page.items
    assert (row.employer, row.title, row.source_name) == ("Acme", "Sales Director", "Acme")
    assert (row.title_code, row.first_seen_at, row.last_seen_at) == (
        "title_role_family_conflict",
        _FIRST,
        _SECOND,
    )
    assert (row.review_state, row.reviewed_at) == ("new", None)
    assert (page.all_count, page.needs_review_count) == (1, 1)


def test_a_reviewed_title_stays_reviewed_when_a_rescan_reads_the_same_finding(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = _workspace(durable_database)
    service = _scanner(durable_database)
    _scan_at(monkeypatch, service, workspace_id, _FIRST)
    marked = service.mark_screened_out_titles_reviewed(
        workspace_id, [_sales_row(service, workspace_id).id]
    )

    _scan_at(monkeypatch, service, workspace_id, _SECOND)

    assert marked.reviewed == 1
    assert _sales_row(service, workspace_id).review_state == "reviewed"
    page = service.list_screened_out_titles(workspace_id, needs_review=True)
    assert (page.items, page.all_count, page.needs_review_count) == ([], 1, 0)


def test_a_reviewed_title_needs_review_again_once_its_title_finding_changes(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = _workspace(durable_database)
    service = _scanner(durable_database)
    _scan_at(monkeypatch, service, workspace_id, _FIRST)
    service.mark_screened_out_titles_reviewed(workspace_id, [_sales_row(service, workspace_id).id])
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            seniority=Seniority(target_titles=titles("data"), excluded_titles=["sales"])
        ),
    )

    _scan_at(monkeypatch, service, workspace_id, _SECOND)

    row = _sales_row(service, workspace_id)
    assert (row.title_code, row.title_subject, row.review_state) == (
        "title_excluded",
        "sales",
        "changed",
    )
    page = service.list_screened_out_titles(workspace_id, needs_review=True)
    assert [item.url for item in page.items] == [_SALES]


def test_a_screened_out_listing_now_in_pending_leaves_the_view(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = _workspace(durable_database)
    service = _scanner(durable_database)
    _scan_at(monkeypatch, service, workspace_id, _FIRST)
    state_target_titles(durable_database, workspace_id, "data", "sales director")

    _scan_at(monkeypatch, service, workspace_id, _SECOND)

    page = service.list_screened_out_titles(workspace_id)
    assert (page.items, page.all_count, page.needs_review_count) == ([], 0, 0)


def test_first_seen_reaches_back_to_outcomes_recorded_before_the_row(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database carried from before the view still dates a title by its oldest outcome."""
    workspace_id = _workspace(durable_database)
    service = _scanner(durable_database)
    _scan_at(monkeypatch, service, workspace_id, _FIRST)
    with durable_database.session() as session:
        session.execute(delete(ScreenedOutTitle))

    _scan_at(monkeypatch, service, workspace_id, datetime(2099, 1, 1, tzinfo=UTC))

    row = _sales_row(service, workspace_id)
    assert row.first_seen_at < row.last_seen_at


def test_marking_another_workspaces_title_reviewed_is_refused(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = _workspace(durable_database)
    service = _scanner(durable_database)
    _scan_at(monkeypatch, service, workspace_id, _FIRST)
    other = scan_workspace(durable_database, slug="other")

    with pytest.raises(ValidationError):
        service.mark_screened_out_titles_reviewed(other, [_sales_row(service, workspace_id).id])
