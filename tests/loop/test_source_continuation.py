"""Bounded frontier budget, eligibility narrowing, and cross-scan dedupe."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from socket import AF_INET, SOCK_STREAM
from typing import Any, ClassVar, cast
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from clearscout.application.discovery.board_scan import SourceScanHandler, SourceScanService
from clearscout.application.discovery.content_resolution import (
    PostingContentResolutionService,
    ResolvedPostingContent,
)
from clearscout.application.discovery.funnel_audit import FunnelReason
from clearscout.application.discovery.posting_refresh import PostingRefreshService
from clearscout.application.discovery.scan_queries import ScanOutcomeFilters, SourceScanQueries
from clearscout.application.discovery.scan_queries.history import QUERY_LABEL
from clearscout.application.discovery.scan_staging import DiscoveryStaging, StagingBusyError
from clearscout.application.discovery.scan_sweep import listing_audit
from clearscout.application.discovery.scan_sweep import sweep as sweep_module
from clearscout.application.discovery.scan_sweep.recording import ScanReadContext, ScanRecorder
from clearscout.application.discovery.source_continuation import (
    CONTINUATION_CHUNK_SIZE,
    DISCOVERED_DURING_FOLLOW_UP,
    LATEST_SCAN_FIRST,
    MANUAL_RECOVERY_NOTE,
    MAX_AUTOMATIC_ATTEMPTS,
    MAX_NO_PROGRESS_TASKS,
    POSTING_DETAIL_COMPLETED,
    RESCREEN_COMPLETED_AFTER,
    FrontierPersistResult,
    SourceContinuationCommands,
    SourceContinuationHandler,
    continuation_task_ids,
    recent_root_posting_reads,
)
from clearscout.application.discovery.source_continuation import handler as continuation_handler
from clearscout.application.discovery.source_continuation.family import select_frontier_rows
from clearscout.application.discovery.source_continuation.frontier_carry import (
    carried_frontier_item,
)
from clearscout.application.discovery.source_continuation.publisher_targets import (
    publisher_search_row_target,
)
from clearscout.application.discovery.source_continuation.rescreen import recent_posting_reads
from clearscout.application.discovery.source_continuation.targets import (
    frontier_item,
    frontier_target,
    read_page_item,
)
from clearscout.application.discovery.source_reading import (
    PostingCheck,
    SourceContinuationTarget,
    SourceDetailCheckpoint,
    SourceDetailCoverage,
    SourceDetailSummary,
    SourceListing,
    SourceRead,
    SourceReader,
    SourceReadOptions,
)
from clearscout.application.failures import ValidationError
from clearscout.application.tasks import TaskQueueService
from clearscout.application.tasks.commands import TaskCommands
from clearscout.application.workspaces import SearchParametersService, WorkspaceService
from clearscout.bootstrap.worker import Worker, default_handlers
from clearscout.domain import (
    PostingContentAccessStatus,
    PostingStatus,
    SourceReadOutcome,
    new_id,
    utc_now,
)
from clearscout.domain.artifacts import PostingSource
from clearscout.domain.identity import normalize_fetch_url
from clearscout.domain.search_parameters import (
    CountryNames,
    JobSearchParameters,
    Location,
    Seniority,
)
from clearscout.domain.staged_facts import PublishedFact, StagedListingFacts
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    AuditEvent,
    BackgroundTask,
    JobSearchParameterSet,
    PendingDiscovery,
    PendingDiscoveryDispositionEvent,
    PendingPostingContent,
    Source,
    SourceListingMembership,
    SourceScan,
    SourceScanContinuation,
    SourceScanFrontierItem,
    SourceScanMember,
    SourceScanOutcome,
)
from clearscout.infrastructure.web_fetch import board_reads
from clearscout.infrastructure.web_fetch.board_adapters import BoardApiFetcher, deadline_cut_note
from clearscout.infrastructure.web_fetch.board_mappings.bamboohr import BAMBOOHR_MAPPING
from clearscout.infrastructure.web_fetch.board_mappings.dayforce import DAYFORCE_MAPPING
from clearscout.infrastructure.web_fetch.board_mappings.mapper import source_listings
from clearscout.infrastructure.web_fetch.boards import board_identity, board_read_scope
from clearscout.infrastructure.web_fetch.listing_facts import published_listing_facts
from clearscout.infrastructure.web_fetch.platforms import oracle, phenom, workday
from clearscout.infrastructure.web_fetch.policy import HostHeldError, PoliteHttpClient
from clearscout.infrastructure.web_fetch.publisher_sources import (
    PublisherReadLimits,
    PublisherSourceFetcher,
)
from tests.loop.test_board_mappings_declared_search import _SearchSite
from tests.loop.test_board_mappings_feeds import (
    _ADP_BOARD,
    _adp_requisition,
    _CheckpointAdp,
    _jazzhr_posting_page,
)
from tests.loop.test_board_mappings_listed_pages import (
    _ICIMS_ANALYST,
    _ICIMS_ENGINEER,
    _ICIMS_ROBOTS,
    _ICIMS_ROOT,
    _icims_pages,
)
from tests.loop.test_board_paging import _ORACLE_SITE, _facet_board, _OracleBoard, _PhenomWindow
from tests.loop.test_publisher_sources import _Client as _ScriptedPublisherClient
from tests.loop.test_taleo_reader import _TaleoSite
from tests.support.artifacts import runtime_storage
from tests.support.doubles import (
    ScriptedBoards,
    ScriptedPage,
    UnexpectedPostingFetcher,
    declaring_page,
)
from tests.support.http import policy_client, sitemap_urlset
from tests.support.scenarios import board_source as _source
from tests.support.scenarios import (
    completed_earlier_scan,
    dismiss_pending_discovery,
    same_day_rescans_with_follow_ups,
    seed_frontier_rows,
    seed_scan_continuation,
    source_listing,
    state_single_market,
    state_target_titles,
    titles,
)
from tests.support.scenarios import scan_workspace as _workspace


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("fail_page", [None, 1])
@pytest.mark.parametrize("failed_detail", [False, True])
def test_alignerr_commits_membership_before_bodies_and_settles_only_readable_details(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    fail_page: int | None,
    failed_detail: bool,
    cancel: bool,
) -> None:
    workspace_id = _workspace(durable_database)
    site = _SearchSite("alignerr", scope="?category=Research&search=data")
    source_id = _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    if failed_detail:
        site.failed_details = {2}
    original = site.answer
    observed: list[str] = []
    cancelled = False
    if cancel:
        monkeypatch.setattr(continuation_handler, "CONTINUATION_CHUNK_SIZE", 1)

    def answer(url: str, **kwargs: Any) -> httpx.Response:
        nonlocal cancelled
        if "/jobs/role-" in url:
            with durable_database.session() as session:
                scan = session.scalars(select(SourceScan)).one()
                frontier = session.scalars(select(SourceScanFrontierItem)).all()
                assert scan.raw_count == 3
                assert {row.url for row in frontier} >= {
                    site.detail_url(number) for number in (1, 2, 3)
                }
                outcomes = session.scalars(select(SourceScanOutcome)).all()
                assert {row.url for row in outcomes} >= {
                    site.detail_url(number) for number in (1, 2, 3)
                }
                assert not any(
                    row.automatic_eligible
                    and row.deferred_reason == "alignerr_search_page"
                    and row.status == "pending"
                    for row in frontier
                )
            observed.append(url)
            if cancel and not cancelled and len(observed) == 2:
                with durable_database.session() as session:
                    task_id = session.scalars(
                        select(BackgroundTask.id).where(BackgroundTask.status == "running")
                    ).one()
                TaskQueueService(durable_database).request_cancellation(
                    task_id, workspace_id=workspace_id
                )
                cancelled = True
        response = original(url)
        if (
            fail_page is not None
            and urlsplit(url).path == "/api/jobs"
            and not observed
            and len(site.api_queries) == 1
        ):
            site.stopped = True
        return response

    site.fetcher.client.getter = answer
    handler = SourceScanHandler(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=site.fetcher,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service = handler.service
    TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id, task_type="source.scan", payload={}, idempotency_key=new_id()
    )
    root_worker = Worker(
        durable_database, worker_id="alignerr-root", handlers={"source.scan": handler}
    )
    assert root_worker.run_once()
    assert observed == []
    with durable_database.session() as session:
        assert not session.scalars(select(PendingDiscovery)).all()
        scan = session.scalars(select(SourceScan)).one()
        assert scan.source_id == source_id
        assert scan.status == "incomplete"
        assert scan.read_count == 0
        outcomes = session.scalars(select(SourceScanOutcome)).all()
        assert all(
            row.reason == FunnelReason.INCOMPLETE_POSTING.value
            for row in outcomes
            if row.candidate_kind == "board_listing"
        )
    site.stopped = False
    worker = Worker(
        durable_database,
        worker_id="alignerr-root",
        handlers={
            "source.continue": SourceContinuationHandler(
                durable_database,
                site.fetcher,
                service.sweep,
                lambda workspace, task, outcomes: service.record_task_outcomes(
                    workspace, task, outcomes, origin="source.continue"
                ),
            )
        },
    )
    for _ in range(8):
        if not worker.run_once():
            break
        if cancelled:
            with durable_database.session() as session:
                scan_id = session.scalars(select(SourceScan.id)).one()
                stopped = session.scalars(
                    select(BackgroundTask).where(BackgroundTask.status == "cancelled")
                ).all()
            if stopped and service.sweep.continuations is not None:
                service.sweep.continuations.request_continuation(workspace_id, scan_id)
                cancelled = False
    with durable_database.session() as session:
        tasks = session.scalars(select(BackgroundTask)).all()
        assert all(task.status != "failed" for task in tasks), [
            (task.status, task.error_message) for task in tasks
        ]
        if cancel:
            assert any(task.status == "cancelled" for task in tasks)
            assert observed.count(site.detail_url(2)) == 2
    assert observed.count(site.detail_url(1)) == 1
    assert set(observed) == {site.detail_url(number) for number in (1, 2, 3)}
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert scan.raw_count == 3
        assert scan.read_count == (2 if failed_detail else 3)
        assert scan.complete is not failed_detail
        assert scan.detail_coverage is None
        pending = session.scalars(select(PendingDiscovery)).all()
        assert {row.discovery_url for row in pending} == {
            site.detail_url(number) for number in ((1, 3) if failed_detail else (1, 2, 3))
        }
        if failed_detail:
            row = session.scalars(
                select(SourceScanFrontierItem).where(
                    SourceScanFrontierItem.url == site.detail_url(2)
                )
            ).one()
            assert not row.automatic_eligible
            checkpoint = SourceDetailCheckpoint.from_json(row.detail_checkpoint)
            assert checkpoint.pending_ids == ("2",)
            assert checkpoint.failed_attempts == (("2", 1),)
            assert checkpoint.listing_facts


@pytest.mark.parametrize("recording", ["committed", "staging_failed", "superseded", "cancelled"])
def test_declared_total_gap_diagnostic_survives_only_a_committed_continuation(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    recording: str,
) -> None:
    workspace_id = _workspace(durable_database)
    site = _SearchSite("tbk")
    site.totals = [4, 4, 4]
    site.stop_after_pages = 1
    _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    handler = SourceScanHandler(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=site.fetcher,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service = handler.service
    TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id, task_type="source.scan", payload={}, idempotency_key=new_id()
    )
    worker = Worker(durable_database, worker_id="declared-gap", handlers={"source.scan": handler})
    assert worker.run_once()
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        original_notes = scan.notes
        assert "publisher_listing_total_mismatch" not in scan.skip_reasons
    site.stopped = False
    original = site.answer

    def answer(url: str, **kwargs: Any) -> httpx.Response:
        response = original(url)
        if recording == "superseded" and urlsplit(url).path.endswith("/career-search"):
            with durable_database.session() as session:
                continuation = session.scalars(select(SourceScanContinuation)).one()
                continuation.generation += 1
        elif recording == "cancelled" and urlsplit(url).path.endswith("/career-search"):
            with durable_database.session() as session:
                task_id = session.scalars(
                    select(BackgroundTask.id).where(BackgroundTask.status == "running")
                ).one()
            TaskQueueService(durable_database).request_cancellation(
                task_id, workspace_id=workspace_id
            )
        return response

    site.fetcher.client.getter = answer
    if recording == "staging_failed":

        def stage_failed(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("Synthetic staging failure")

        monkeypatch.setattr(service.sweep, "stage_continuation", stage_failed)
    worker = Worker(
        durable_database,
        worker_id="declared-gap",
        handlers={
            "source.continue": SourceContinuationHandler(
                durable_database, site.fetcher, service.sweep
            )
        },
    )
    assert worker.run_once()
    while recording == "committed" and worker.run_once():
        pass
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert not scan.complete
        if recording == "committed":
            assert scan.skip_reasons["publisher_listing_total_mismatch"] == 1
            assert "publisher reports 4 listings" in scan.notes
            assert "3 distinct identities" in scan.notes
            rows = session.scalars(select(SourceScanFrontierItem)).all()
            assert [
                row.automatic_eligible
                for row in rows
                if row.deferred_reason == "tbk_search_page" and row.status == "pending"
            ] == [False]
            assert {row.requisition_id for row in session.scalars(select(PendingDiscovery))} == {
                "1",
                "2",
                "3",
            }
        else:
            assert "publisher_listing_total_mismatch" not in scan.skip_reasons
            assert scan.notes == original_notes
            if recording == "cancelled":
                assert (
                    session.scalar(
                        select(BackgroundTask).where(BackgroundTask.status == "cancelled")
                    )
                    is not None
                )


def test_a_relisted_page_cursor_is_queued_beside_the_same_page_read_in_the_first_pass(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    site = _SearchSite("tbk")
    site.stop_after_pages = 1
    _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    worker = _enqueue_scan(durable_database, workspace_id, site.fetcher)
    assert worker.run_once()
    site.stopped = False
    site.totals = [4, 4, 4]
    site.stop_after_pages = len(site.api_queries) + 3
    for _ in range(6):
        if site.stopped or not worker.run_once():
            break

    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        pending = [
            (parse_qs(urlsplit(row.url).query), row.automatic_eligible)
            for row in session.scalars(select(SourceScanFrontierItem))
            if row.deferred_reason == "tbk_search_page" and row.status == "pending"
        ]
    assert pending == [({"__reader_page": ["2"], "__reader_pass": ["2"]}, True)]
    assert "publisher_listing_total_mismatch" not in scan.skip_reasons
    assert not scan.discovery_truncated


def test_instability_seen_before_a_deadline_hold_is_relisted_without_truncating_the_scan(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    site = _SearchSite("tbk")
    site.stop_after_pages = 1
    _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    worker = _enqueue_scan(durable_database, workspace_id, site.fetcher)
    assert worker.run_once()
    site.pages, site.totals = [[5, 1], [2, 3], []], [4, 4, 4]
    site.stopped = False
    site.stop_after_pages = len(site.api_queries) + 1
    assert worker.run_once()
    site.stopped = False
    while worker.run_once():
        pass

    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        relists = [
            row.automatic_eligible
            for row in session.scalars(select(SourceScanFrontierItem))
            if parse_qs(urlsplit(row.url).query).get("__reader_pass") == ["2"]
        ]
        assert not scan.discovery_truncated
        assert "publisher_listing_changed" not in scan.skip_reasons
    assert relists == [True]


@pytest.mark.parametrize("movement", ["added_before", "removed_before_added_after", "swapped"])
def test_a_search_that_visibly_moves_across_a_listing_cut_is_relisted_from_its_first_page(
    durable_database: Database, movement: str
) -> None:
    workspace_id = _workspace(durable_database)
    site = _SearchSite("tbk")
    site.stop_after_pages = 1
    _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    worker = _enqueue_scan(durable_database, workspace_id, site.fetcher)
    assert worker.run_once()
    site.stopped = False
    site.pages, site.totals = {
        "added_before": ([[4, 1], [2, 3], []], [4, 4, 4]),
        "removed_before_added_after": ([[2, 3], [4], []], [3, 3, 3]),
        "swapped": ([[1, 3], [2], []], [3, 3, 3]),
    }[movement]
    while worker.run_once():
        pass
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        pages = [
            (parse_qs(urlsplit(row.url).query).get("__reader_pass"), row.status)
            for row in session.scalars(select(SourceScanFrontierItem))
            if "__reader_page" in parse_qs(urlsplit(row.url).query)
        ]
        assert not scan.complete
        assert "publisher_listing_changed" not in scan.skip_reasons
        assert "publisher_listing_total_mismatch" not in scan.skip_reasons
        assert not scan.discovery_truncated
    relisted = movement != "removed_before_added_after"
    assert [relist for relist, _status in pages if relist is not None] == (
        [["2"]] if relisted else []
    )
    assert all(status != "pending" for _relist, status in pages)


def test_a_search_page_cursor_loads_ahead_of_older_detail_rows(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = _workspace(durable_database)
    site = _SearchSite("tbk")
    site.stop_after_pages = 1
    _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    worker = _enqueue_scan(durable_database, workspace_id, site.fetcher)
    assert worker.run_once()
    with durable_database.session() as session:
        rows = session.scalars(select(SourceScanFrontierItem)).all()
        page = next(row for row in rows if row.deferred_reason == "tbk_search_page")
        assert any(row.deferred_reason == "tbk_search_detail" for row in rows)
        page.created_at = max(row.created_at for row in rows) + timedelta(seconds=1)
    monkeypatch.setattr(continuation_handler, "CONTINUATION_CHUNK_SIZE", 1)
    site.stopped = False
    site.requested.clear()

    assert worker.run_once()

    assert any("/career-search" in url for url in site.requested)


def test_a_fresh_scan_lapses_a_pending_page_cursor_of_the_same_search(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    site = _SearchSite("tbk")
    site.stop_after_pages = 1
    _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    worker = _enqueue_scan(durable_database, workspace_id, site.fetcher)
    assert worker.run_once()
    with durable_database.session() as session:
        page = session.scalars(
            select(SourceScanFrontierItem).where(
                SourceScanFrontierItem.deferred_reason == "tbk_search_page"
            )
        ).one()
        assert page.automatic_eligible
        page_id = page.id
        queued = session.scalars(
            select(BackgroundTask.id).where(BackgroundTask.status == "queued")
        ).all()
    for task_id in queued:
        TaskQueueService(durable_database).request_cancellation(task_id)
    site.stopped = False
    site.stop_after_pages = len(site.api_queries) + 2
    TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id, task_type="source.scan", payload={}, idempotency_key=new_id()
    )

    assert worker.run_once()

    with durable_database.session() as session:
        stored = session.get(SourceScanFrontierItem, page_id)
        assert stored is None or stored.status != "pending"
        pending = [
            row.url
            for row in session.scalars(select(SourceScanFrontierItem))
            if row.deferred_reason == "tbk_search_page" and row.status == "pending"
        ]
        assert [parse_qs(urlsplit(url).query)["__reader_page"] for url in pending] == [["3"]]


def test_a_fresh_cursor_at_a_manually_held_page_replaces_the_hold(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    site = _SearchSite("tbk")
    site.fail_page = 1
    _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    worker = _enqueue_scan(durable_database, workspace_id, site.fetcher)
    assert worker.run_once()
    with durable_database.session() as session:
        held = session.scalars(
            select(SourceScanFrontierItem).where(
                SourceScanFrontierItem.deferred_reason == "tbk_search_page"
            )
        ).one()
        assert not held.automatic_eligible
        queued = session.scalars(
            select(BackgroundTask.id).where(BackgroundTask.status == "queued")
        ).all()
    for task_id in queued:
        TaskQueueService(durable_database).request_cancellation(task_id)
    site.fail_page = None
    site.stop_after_pages = len(site.api_queries) + 1
    TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id, task_type="source.scan", payload={}, idempotency_key=new_id()
    )

    assert worker.run_once()

    with durable_database.session() as session:
        pending = [
            (parse_qs(urlsplit(row.url).query)["__reader_page"], row.automatic_eligible)
            for row in session.scalars(select(SourceScanFrontierItem))
            if row.deferred_reason == "tbk_search_page" and row.status == "pending"
        ]
    assert pending == [(["2"], True)]


def test_a_fresh_scan_does_not_revive_a_failed_page_cursor_beside_its_own(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    site = _SearchSite("tbk")
    site.stop_after_pages = 1
    _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    worker = _enqueue_scan(durable_database, workspace_id, site.fetcher)
    assert worker.run_once()
    with durable_database.session() as session:
        page = session.scalars(
            select(SourceScanFrontierItem).where(
                SourceScanFrontierItem.deferred_reason == "tbk_search_page"
            )
        ).one()
        page.status = "failed"
        queued = session.scalars(
            select(BackgroundTask.id).where(BackgroundTask.status == "queued")
        ).all()
    for task_id in queued:
        TaskQueueService(durable_database).request_cancellation(task_id)
    site.stopped = False
    site.stop_after_pages = len(site.api_queries) + 2
    TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id, task_type="source.scan", payload={}, idempotency_key=new_id()
    )

    assert worker.run_once()

    with durable_database.session() as session:
        pending = [
            parse_qs(urlsplit(row.url).query)["__reader_page"]
            for row in session.scalars(select(SourceScanFrontierItem))
            if row.deferred_reason == "tbk_search_page" and row.status == "pending"
        ]
    assert pending == [["3"]]


@pytest.mark.parametrize("observed", [False, True])
def test_partial_alignerr_rescan_keeps_manual_failures_and_refreshes_only_observed_facts(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    observed: bool,
) -> None:
    workspace_id = _workspace(durable_database)
    site = _SearchSite("alignerr")
    _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    site.failed_details = {2}
    worker = _enqueue_scan(durable_database, workspace_id, site.fetcher)
    assert worker.run_once()
    assert worker.run_once()
    with durable_database.session() as session:
        failed = session.scalars(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.url == site.detail_url(2))
        ).one()
        checkpoint = SourceDetailCheckpoint.from_json(failed.detail_checkpoint)
        attempts = failed.attempt_count
        assert checkpoint.failed_attempts == (("2", 1),)
        assert not failed.automatic_eligible
    original = site.row

    def row(number: int) -> dict[str, Any]:
        value = original(number)
        value["location"] = "Remote, United States"
        return value

    monkeypatch.setattr(site, "row", row)
    site.pages = [[1, 2] if observed else [1], []]
    site.totals = [3, 3]
    TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id, task_type="source.scan", payload={}, idempotency_key=new_id()
    )
    assert worker.run_once()
    with durable_database.session() as session:
        failed = session.scalars(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.url == site.detail_url(2))
        ).one()
        refreshed = SourceDetailCheckpoint.from_json(failed.detail_checkpoint)
        assert failed.attempt_count == attempts
        assert not failed.automatic_eligible
        assert refreshed.failed_attempts == checkpoint.failed_attempts
        expected = (
            ("Remote, United States",)
            if observed
            else checkpoint.listing_facts[0][1].locations.values
        )
        assert refreshed.listing_facts[0][1].locations.values == expected
        scan = session.scalars(select(SourceScan)).one()
        assert not scan.complete
        assert all(row.withdrawn_at is None for row in session.scalars(select(PendingDiscovery)))
    assert site.requested.count(site.detail_url(2)) == 1


def test_alignerr_membership_keeps_ineligible_and_protected_ids_without_body_coverage(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = _workspace(durable_database)
    site = _SearchSite("alignerr")
    _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    original = site.row

    def row(number: int) -> dict[str, Any]:
        value = original(number)
        if number == 2:
            value["title"] = "Security Guard"
        elif number == 3:
            value["title"] = ""
        return value

    monkeypatch.setattr(site, "row", row)
    protected = source_listing(employer="Acme", title="Data Analyst 1", url=site.detail_url(1))
    staging = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).staging
    staging.stage_listings(
        workspace_id, [protected], utc_now().date(), reason="Synthetic retained opening"
    )
    with durable_database.session() as session:
        pending = session.scalars(select(PendingDiscovery)).one()
        protected_id = pending.id
    dismiss_pending_discovery(durable_database, workspace_id, protected_id)
    worker = _enqueue_scan(durable_database, workspace_id, site.fetcher)
    assert worker.run_once()
    assert not worker.run_once()
    assert site.detail_reads == 0
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert scan.complete
        assert scan.status == "completed"
        assert scan.raw_count == 3
        assert scan.read_count == 0
        assert scan.detail_coverage is None
        outcomes = session.scalars(
            select(SourceScanOutcome).where(SourceScanOutcome.candidate_kind == "board_listing")
        ).all()
        assert {row.url for row in outcomes} == {site.detail_url(number) for number in (1, 2, 3)}
        assert all(row.reason == FunnelReason.INCOMPLETE_POSTING.value for row in outcomes)
        pending = session.scalars(select(PendingDiscovery)).one()
        assert pending.id == protected_id
        assert pending.withdrawn_at is None


@pytest.mark.parametrize(
    "certificate", ["foreign", "different_filter", "missing_member", "truncated", "staging_failed"]
)
def test_invalid_alignerr_enumeration_cannot_settle_or_withdraw(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    certificate: str,
) -> None:
    workspace_id = _workspace(durable_database)
    site = _SearchSite("alignerr", scope="?search=data")
    _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    original = site.answer

    def answer(url: str, **kwargs: Any) -> httpx.Response:
        response = original(url)
        if urlsplit(url).path == "/api/jobs" and len(site.api_queries) == 1:
            site.stopped = True
        return response

    site.fetcher.client.getter = answer
    handler = SourceScanHandler(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=site.fetcher,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service = handler.service
    TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id, task_type="source.scan", payload={}, idempotency_key=new_id()
    )
    worker = Worker(
        durable_database, worker_id="alignerr-invalid", handlers={"source.scan": handler}
    )
    assert worker.run_once()
    site.stopped = False
    continued = site.fetcher.continue_listings

    def continuation(*args: Any, **kwargs: Any) -> SourceRead:
        read = continued(*args, **kwargs)
        coverage = read.listing_coverage
        if coverage is None:
            return read
        if certificate == "foreign":
            coverage = replace(coverage, board_url="https://globex.example/jobs?search=data")
        elif certificate == "different_filter":
            coverage = replace(coverage, board_url="https://acme.example/jobs?search=other")
        elif certificate == "missing_member":
            coverage = replace(coverage, posting_urls=coverage.posting_urls[:2])
        return replace(
            read, listing_coverage=coverage, discovery_truncated=certificate == "truncated"
        )

    monkeypatch.setattr(site.fetcher, "continue_listings", continuation)
    if certificate == "staging_failed":
        stage = service.sweep.stage_continuation

        def staging(*args: Any, **kwargs: Any) -> dict[str, Any]:
            if kwargs.get("read_sink") and args[3].listing_coverage is not None:
                raise RuntimeError("Synthetic page staging failure")
            return stage(*args, **kwargs)

        monkeypatch.setattr(service.sweep, "stage_continuation", staging)
    worker = Worker(
        durable_database,
        worker_id="alignerr-invalid",
        handlers={
            "source.continue": SourceContinuationHandler(
                durable_database, site.fetcher, service.sweep
            )
        },
    )
    for _ in range(5):
        if not worker.run_once():
            break
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert not scan.complete
        assert scan.detail_coverage is None
        assert all(row.withdrawn_at is None for row in session.scalars(select(PendingDiscovery)))


@pytest.mark.parametrize("new_member", [False, True])
def test_complete_alignerr_rescan_uses_only_current_membership_for_withdrawal(
    durable_database: Database,
    new_member: bool,
) -> None:
    workspace_id = _workspace(durable_database)
    site = _SearchSite("alignerr")
    _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    worker = _enqueue_scan(durable_database, workspace_id, site.fetcher)
    while worker.run_once():
        pass
    with durable_database.session() as session:
        assert session.scalars(select(SourceScan)).one().complete
        assert {row.discovery_url for row in session.scalars(select(PendingDiscovery))} == {
            site.detail_url(number) for number in (1, 2, 3)
        }
    site.pages = [[2, 3], [4], []] if new_member else [[2, 3], []]
    site.totals = [3, 3, 3] if new_member else [2, 2]
    TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id, task_type="source.scan", payload={}, idempotency_key=new_id()
    )
    while worker.run_once():
        pass
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert scan.complete, (
            scan.status,
            scan.skip_reasons,
            [
                (row.url, row.status, row.deferred_reason, row.last_error)
                for row in session.scalars(select(SourceScanFrontierItem))
            ],
            [(row.status, row.error_message) for row in session.scalars(select(BackgroundTask))],
        )
        assert scan.raw_count == scan.read_count == (3 if new_member else 2)
        assert scan.detail_coverage is None
        assert scan.listing_pages == (2 if new_member else 1)
        missed = session.scalars(
            select(PendingDiscovery).where(PendingDiscovery.discovery_url == site.detail_url(1))
        ).one()
        assert (missed.withdrawn_at is None) is new_member
        assert (missed.absent_since is not None) is new_member
    if new_member:
        TaskQueueService(durable_database).enqueue(
            workspace_id=workspace_id, task_type="source.scan", payload={}, idempotency_key=new_id()
        )
        while worker.run_once():
            pass
    with durable_database.session() as session:
        pending = {row.discovery_url: row for row in session.scalars(select(PendingDiscovery))}
        assert pending[site.detail_url(1)].withdrawn_at is not None
        assert {url for url, row in pending.items() if row.withdrawn_at is None} == {
            site.detail_url(number) for number in ((2, 3, 4) if new_member else (2, 3))
        }


def test_a_one_listing_page_alignerr_read_withdraws_on_its_first_miss(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    site = _SearchSite("alignerr")
    site.pages = [[1, 2, 3]]
    site.totals = [3]
    source_id = _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    stale = site.detail_url(9)
    _stage_known_rows(durable_database, workspace_id, source_id, (stale,))
    worker = _enqueue_scan(durable_database, workspace_id, site.fetcher)
    while worker.run_once():
        pass
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert scan.complete
        assert scan.listing_pages == 1
    assert _withdrawn_urls(durable_database) == {stale}


def test_a_one_page_search_closed_by_a_continuation_counts_one_listing_page(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    site = _SearchSite("alignerr")
    site.pages, site.totals = [[1, 2], []], [2, 2]
    site.stop_after_pages = 1
    source_id = _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    stale = site.detail_url(9)
    _stage_known_rows(durable_database, workspace_id, source_id, (stale,))
    worker = _enqueue_scan(durable_database, workspace_id, site.fetcher)
    assert worker.run_once()
    with durable_database.session() as session:
        assert not session.scalars(select(SourceScan)).one().complete
    site.stopped = False
    while worker.run_once():
        pass
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert scan.complete
        assert scan.listing_pages == 1
    assert _withdrawn_urls(durable_database) == {stale}


@pytest.mark.parametrize("failed_detail", [False, True])
def test_alignerr_body_results_advance_the_retained_metadata_outcome(
    durable_database: Database,
    failed_detail: bool,
) -> None:
    workspace_id = _workspace(durable_database)
    site = _SearchSite("alignerr")
    _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    if failed_detail:
        site.failed_details = {2}
    worker = _enqueue_scan(durable_database, workspace_id, site.fetcher)
    assert worker.run_once()
    with durable_database.session() as session:
        initial = session.scalars(
            select(SourceScanOutcome).where(SourceScanOutcome.candidate_kind == "board_listing")
        ).all()
        assert all(row.reason == FunnelReason.INCOMPLETE_POSTING.value for row in initial)
    while worker.run_once():
        pass
    with durable_database.session() as session:
        outcomes = session.scalars(
            select(SourceScanOutcome).where(SourceScanOutcome.candidate_kind == "board_listing")
        ).all()
        assert len(outcomes) == 3
        by_url = {row.url: row for row in outcomes}
        for number in (1, 2, 3):
            row = by_url[site.detail_url(number)]
            if failed_detail and number == 2:
                assert row.reason == FunnelReason.INCOMPLETE_POSTING.value
            else:
                assert row.reason == FunnelReason.STAGED.value
                assert "body is unverified" not in row.detail
        pending = session.scalars(select(PendingDiscovery)).all()
        assert {row.discovery_url for row in pending} == {
            site.detail_url(number) for number in ((1, 3) if failed_detail else (1, 2, 3))
        }


@pytest.mark.parametrize("recording", ["staging_failed", "superseded", "cancelled"])
def test_alignerr_uncommitted_body_cannot_advance_the_metadata_outcome(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    recording: str,
) -> None:
    workspace_id = _workspace(durable_database)
    site = _SearchSite("alignerr")
    _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    monkeypatch.setattr(continuation_handler, "CONTINUATION_CHUNK_SIZE", 1)
    handlers = default_handlers(durable_database, fetcher=site.fetcher)
    service = cast(SourceScanHandler, handlers["source.scan"]).service
    TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id, task_type="source.scan", payload={}, idempotency_key=new_id()
    )
    worker = Worker(durable_database, worker_id="guarded-body", handlers=handlers)
    assert worker.run_once()
    assert worker.run_once()
    original = site.answer
    attempted: list[str] = []

    def answer(url: str, **kwargs: Any) -> httpx.Response:
        response = original(url)
        if url == site.detail_url(2):
            attempted.append(url)
            if recording == "superseded":
                with durable_database.session() as session:
                    continuation = session.scalars(select(SourceScanContinuation)).one()
                    continuation.generation += 1
            elif recording == "cancelled":
                with durable_database.session() as session:
                    task_id = session.scalars(
                        select(BackgroundTask.id).where(BackgroundTask.status == "running")
                    ).one()
                TaskQueueService(durable_database).request_cancellation(
                    task_id, workspace_id=workspace_id
                )
        return response

    site.fetcher.client.getter = answer
    if recording == "staging_failed":

        def stage_failed(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("Synthetic staging failure")

        monkeypatch.setattr(service.sweep, "stage_continuation", stage_failed)
    assert worker.run_once()
    assert attempted == [site.detail_url(2)]
    with durable_database.session() as session:
        outcomes = session.scalars(
            select(SourceScanOutcome).where(SourceScanOutcome.candidate_kind == "board_listing")
        ).all()
        by_url = {row.url: row for row in outcomes}
        assert len(by_url) == 3
        assert by_url[site.detail_url(1)].reason == FunnelReason.STAGED.value
        for number in (2, 3):
            assert by_url[site.detail_url(number)].reason == FunnelReason.INCOMPLETE_POSTING.value
        pending = session.scalars(select(PendingDiscovery)).all()
        assert {row.discovery_url for row in pending} == {site.detail_url(1)}
        assert not session.scalars(select(SourceScan)).one().complete
        if recording == "cancelled":
            assert (
                session.scalar(select(BackgroundTask).where(BackgroundTask.status == "cancelled"))
                is not None
            )


def test_checkpoint_enumeration_urls_round_trip_without_completing_bodies() -> None:
    checkpoint = SourceDetailCheckpoint(
        platform="search:alignerr_search_page",
        board_url="https://acme.example/jobs",
        listing_ids=("1",),
        listing_urls=(("1", "https://acme.example/jobs/role-1"),),
    )
    assert checkpoint.completed == ()
    assert SourceDetailCheckpoint.from_json(checkpoint.as_json()) == checkpoint
    legacy = checkpoint.as_json()
    del legacy["listing_urls"]
    assert SourceDetailCheckpoint.from_json(legacy).listing_urls == ()
    with pytest.raises(ValueError):
        replace(checkpoint, listing_urls=(("2", "https://acme.example/jobs/role-2"),))
    with pytest.raises(ValueError):
        replace(checkpoint, listing_urls=(("1", "relative"),))


@pytest.mark.parametrize("failed_detail", [False, True])
@pytest.mark.parametrize("unstable", [False, True])
def test_legacy_alignerr_page_restarts_its_scope_without_losing_manual_details(
    durable_database: Database,
    failed_detail: bool,
    unstable: bool,
) -> None:
    workspace_id = _workspace(durable_database)
    site = _SearchSite("alignerr")
    _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    original = site.answer

    def answer(url: str, **kwargs: Any) -> httpx.Response:
        response = original(url)
        if urlsplit(url).path == "/api/jobs" and len(site.api_queries) == 1:
            site.stopped = True
        return response

    site.fetcher.client.getter = answer
    if failed_detail:
        site.failed_details = {2}
    worker = _enqueue_scan(durable_database, workspace_id, site.fetcher)
    assert worker.run_once()
    with durable_database.session() as session:
        page = session.scalars(
            select(SourceScanFrontierItem).where(
                SourceScanFrontierItem.deferred_reason == "alignerr_search_page"
            )
        ).one()
        checkpoint = SourceDetailCheckpoint.from_json(page.detail_checkpoint)
        if unstable:
            checkpoint = replace(checkpoint, listing_unstable=True)
            assert "__reader_unstable" not in page.url
            page.url += "&__reader_unstable=1"
        legacy = checkpoint.as_json()
        del legacy["listing_urls"]
        page.detail_checkpoint = legacy
        if failed_detail:
            detail = session.scalars(
                select(SourceScanFrontierItem).where(
                    SourceScanFrontierItem.url == site.detail_url(2)
                )
            ).one()
            detail.detail_checkpoint = replace(
                SourceDetailCheckpoint.from_json(detail.detail_checkpoint),
                failed_attempts=(("2", 2),),
            ).as_json()
            detail.attempt_count = 2
            detail.automatic_eligible = False
            detail.last_error = "Synthetic retained detail failure"
    site.stopped = False
    resumed_at = len(site.api_queries)
    while worker.run_once():
        pass
    assert site.api_queries[resumed_at]["offset"] == ["0"]
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert scan.raw_count == 3
        assert scan.read_count == (2 if failed_detail else 3)
        assert scan.complete is not failed_detail
        outcomes = session.scalars(
            select(SourceScanOutcome).where(SourceScanOutcome.candidate_kind == "board_listing")
        ).all()
        assert {row.url for row in outcomes} == {site.detail_url(n) for n in (1, 2, 3)}
        assert all(
            not row.last_error or "enumeration URLs" not in row.last_error
            for row in session.scalars(select(SourceScanFrontierItem))
        )
        pending = session.scalars(select(PendingDiscovery)).all()
        assert {row.discovery_url for row in pending} == {
            site.detail_url(n) for n in ((1, 3) if failed_detail else (1, 2, 3))
        }
        if failed_detail:
            failed = session.scalars(
                select(SourceScanFrontierItem).where(
                    SourceScanFrontierItem.url == site.detail_url(2)
                )
            ).one()
            checkpoint = SourceDetailCheckpoint.from_json(failed.detail_checkpoint)
            assert checkpoint.failed_attempts == (("2", 2),)
            assert site.detail_url(2) not in site.requested
            assert not failed.automatic_eligible


def test_incomplete_alignerr_membership_still_counts_successful_bodies(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    site = _SearchSite("alignerr")
    site.totals = [4, 4, 4]
    _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    worker = _enqueue_scan(durable_database, workspace_id, site.fetcher)
    while worker.run_once():
        pass
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert not scan.complete
        assert scan.raw_count == scan.read_count == 3
        assert scan.skip_reasons["publisher_listing_total_mismatch"] == 1
        pending = session.scalars(select(PendingDiscovery)).all()
        assert {row.discovery_url for row in pending} == {site.detail_url(n) for n in (1, 2, 3)}
        outcomes = session.scalars(
            select(SourceScanOutcome).where(SourceScanOutcome.candidate_kind == "board_listing")
        ).all()
        assert {row.url for row in outcomes} == {site.detail_url(n) for n in (1, 2, 3)}
        assert all(row.reason == FunnelReason.STAGED.value for row in outcomes)


@pytest.mark.parametrize("new_member", [False, True])
@pytest.mark.parametrize("prior_partial", [False, True])
def test_partial_alignerr_rescan_counts_only_current_observed_bodies(
    durable_database: Database,
    new_member: bool,
    prior_partial: bool,
) -> None:
    workspace_id = _workspace(durable_database)
    site = _SearchSite("alignerr")
    if prior_partial:
        site.totals = [4, 4, 4]
    _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    worker = _enqueue_scan(durable_database, workspace_id, site.fetcher)
    while worker.run_once():
        pass
    site.pages = [[3, 2], [4], []] if new_member else [[3, 2], []]
    site.totals = [4, 4, 4] if new_member else [3, 3]
    TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id, task_type="source.scan", payload={}, idempotency_key=new_id()
    )
    while worker.run_once():
        pass
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert not scan.complete
        assert scan.raw_count == scan.read_count == (3 if new_member else 2)
        assert scan.detail_coverage is None
        assert scan.skip_reasons["publisher_listing_total_mismatch"] == 1
        pending = {row.discovery_url: row for row in session.scalars(select(PendingDiscovery))}
        assert all(row.withdrawn_at is None for row in pending.values())
        assert set(pending) == {
            site.detail_url(n) for n in ((1, 2, 3, 4) if new_member else (1, 2, 3))
        }


def test_next_day_alignerr_carry_does_not_certify_old_page_membership_as_current(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    site = _SearchSite("alignerr")
    site.totals = [4, 4, 4]
    _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    worker = _enqueue_scan(durable_database, workspace_id, site.fetcher)
    while worker.run_once():
        pass
    with durable_database.session() as session:
        prior_scan = session.scalars(select(SourceScan)).one()
        prior_scan.scanned_on -= timedelta(days=1)
        prior_page = session.scalars(
            select(SourceScanFrontierItem).where(
                SourceScanFrontierItem.deferred_reason == "alignerr_search_page"
            )
        ).one()
        prior_correlation = prior_page.correlation_id
        prior_url = prior_page.url
        prior_map = SourceDetailCheckpoint.from_json(prior_page.detail_checkpoint).listing_urls
        prior_page.automatic_eligible = False
    site.pages = [[3, 2], []]
    site.totals = [3, 3]
    task = TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id, task_type="source.scan", payload={}, idempotency_key=new_id()
    )
    assert worker.run_once()
    with durable_database.session() as session:
        scans = session.scalars(select(SourceScan).order_by(SourceScan.scanned_on)).all()
        assert len(scans) == 2
        continuation = session.scalars(
            select(SourceScanContinuation).where(
                SourceScanContinuation.source_scan_id == scans[-1].id
            )
        ).one()
        carried = session.scalars(
            select(SourceScanFrontierItem).where(
                SourceScanFrontierItem.continuation_id == continuation.id,
                SourceScanFrontierItem.url == prior_url,
            )
        ).one()
        assert continuation.correlation_id == task.id
        assert carried.correlation_id == prior_correlation
        assert SourceDetailCheckpoint.from_json(carried.detail_checkpoint).listing_urls == prior_map
        assert not carried.automatic_eligible
        assert not scans[-1].complete
        assert scans[-1].raw_count == 2
        assert all(row.withdrawn_at is None for row in session.scalars(select(PendingDiscovery)))


@pytest.mark.parametrize("initial_gap", [0, 2])
@pytest.mark.parametrize("reordered", [False, True])
def test_terminal_partial_workday_scan_counts_distinct_current_listings(
    durable_database: Database, initial_gap: int, reordered: bool
) -> None:
    workspace_id = _workspace(durable_database)
    root = "https://globex.wd1.myworkdayjobs.com/en-US/Careers"
    _source(durable_database, workspace_id, "Globex", root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    listings = [
        source_listing("Production Technician", f"{root}/job/Remote/Technician_R-{index}", "Globex")
        for index in range(4)
    ]
    page = SourceContinuationTarget(
        url=f"{root}?offset=2",
        label="More openings",
        label_source="visible",
        source_url=root,
        structural_context="main",
        relations=("publisher-content",),
        deferred_reason="workday_page_pending",
        detail_checkpoint=SourceDetailCheckpoint(
            platform="search:workday",
            board_url=root,
            listing_ids=tuple(item.url for item in listings[:2]),
            listing_total=50,
            listing_unstable=True,
        ),
    )

    class PartialWorkday(_DeferringBoard):
        def listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            return SourceRead(
                listings=listings[:2],
                complete=False,
                raw_count=2 + initial_gap,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=(page,),
                exhausted_outcome=SourceReadOutcome.ATS_COMPLETE,
                discovery_truncated=True,
            )

        def continue_listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            later = [listings[1], *listings[2:], listings[2]]
            return SourceRead(
                listings=list(reversed(later)) if reordered else later,
                complete=False,
                raw_count=4,
                outcome=SourceReadOutcome.PUBLISHER_LINKED_PAGES,
                attempted_target_urls=(page.url,),
                discovery_truncated=True,
            )

    board = PartialWorkday(page, listings)
    worker = _enqueue_scan(durable_database, workspace_id, board)
    assert worker.run_once()
    with durable_database.session() as session:
        member = session.scalars(select(SourceScanMember)).one()
        receipt = dict(member.result)
        assert receipt["raw"] == 2 + initial_gap
        assert receipt["open"] == 2
    assert worker.run_once()
    assert not worker.run_once()
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert scan.read_count == 4
        assert scan.raw_count == 4 + initial_gap
        assert not scan.complete
        assert scan.discovery_truncated
        assert scan.detail_coverage is None
        assert scan.detail_attempted_count == scan.detail_read_count == 0
        retained = session.scalars(select(SourceScanMember)).one().result
        assert retained["raw"] == receipt["raw"]
        assert retained["open"] == receipt["open"]
        assert "board_result" not in retained
        outcomes = session.scalars(
            select(SourceScanOutcome).where(SourceScanOutcome.candidate_kind == "board_listing")
        ).all()
        assert {row.normalized_url for row in outcomes} == {item.url for item in listings}
        assert all(row.reason == "insufficient_title_evidence" for row in outcomes)


def test_terminal_partial_oracle_scan_counts_listings_across_continuation_cursors(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    host = "https://example.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites"
    root = f"{host}/CX_1"
    _source(durable_database, workspace_id, "Globex", root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    listings = [
        source_listing("Production Technician", f"{root}/job/{100 + index}", "Globex")
        for index in range(6)
    ]
    foreign = [
        source_listing("Production Technician", f"{host}/CX_2/job/900", "Globex"),
        source_listing(
            "Production Technician",
            "https://initech.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/901",
            "Globex",
        ),
    ]

    def cursor(offset: int) -> SourceContinuationTarget:
        return SourceContinuationTarget(
            url=f"{root}?offset={offset}",
            label=f"Oracle page at offset {offset}",
            label_source="visible",
            source_url=root,
            structural_context="main",
            relations=("publisher-content",),
            deferred_reason="oracle_page_pending",
            detail_checkpoint=SourceDetailCheckpoint(
                platform="oracle", board_url=root, listing_total=50, listing_unstable=True
            ),
        )

    pages = {
        cursor(2).url: (listings[2:4], (cursor(4),)),
        cursor(4).url: ([*listings[4:], *foreign], ()),
    }

    class PartialOracle(_DeferringBoard):
        def listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            return SourceRead(
                listings=listings[:2],
                complete=False,
                raw_count=2,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=(cursor(2),),
                exhausted_outcome=SourceReadOutcome.ATS_COMPLETE,
                discovery_truncated=True,
            )

        def continue_listings(
            self, employer: str, targets: tuple[SourceContinuationTarget, ...], **kwargs: Any
        ) -> SourceRead:
            read, deferred = pages[targets[0].url]
            return SourceRead(
                listings=list(read),
                complete=False,
                raw_count=len(read),
                outcome=(
                    SourceReadOutcome.CRAWL_CONTINUATION
                    if deferred
                    else SourceReadOutcome.PUBLISHER_LINKED_PAGES
                ),
                attempted_target_urls=(targets[0].url,),
                deferred_targets=deferred,
                discovery_truncated=True,
            )

    worker = _enqueue_scan(durable_database, workspace_id, PartialOracle(cursor(2), listings))
    while worker.run_once():
        pass
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert (scan.read_count, scan.raw_count) == (6, 6)
        outcomes = session.scalars(
            select(SourceScanOutcome).where(SourceScanOutcome.candidate_kind == "board_listing")
        ).all()
        assert {row.normalized_url for row in outcomes} == {
            item.url for item in [*listings, *foreign]
        }


@pytest.mark.parametrize("next_day", [False, True])
def test_terminal_workday_listing_counts_exclude_prior_roots_and_carried_checkpoints(
    durable_database: Database, next_day: bool
) -> None:
    workspace_id = _workspace(durable_database)
    root = "https://globex.wd1.myworkdayjobs.com/en-US/Careers"
    _source(durable_database, workspace_id, "Globex", root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    listings = [
        source_listing("Production Technician", f"{root}/job/Remote/Technician_R-{index}", "Globex")
        for index in range(5)
    ]
    checkpoint = SourceDetailCheckpoint(
        platform="search:workday",
        board_url=root,
        listing_ids=tuple(item.url for item in listings),
        listing_total=50,
        listing_unstable=True,
    )
    page = SourceContinuationTarget(
        url=f"{root}?offset=2",
        label="More openings",
        label_source="visible",
        source_url=root,
        structural_context="main",
        relations=("publisher-content",),
        deferred_reason="workday_page_pending",
        detail_checkpoint=checkpoint,
    )
    held = replace(page, url=f"{root}?offset=40", automatic_eligible=False)

    class RescannedWorkday(_DeferringBoard):
        fresh = False

        def listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            return SourceRead(
                listings=listings[2:4] if self.fresh else listings[:2],
                complete=False,
                raw_count=2,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=(page,) if self.fresh else (page, held),
                exhausted_outcome=SourceReadOutcome.ATS_COMPLETE,
                discovery_truncated=True,
            )

        def continue_listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            return SourceRead(
                listings=listings[3:] if self.fresh else listings[:2],
                complete=False,
                raw_count=2,
                outcome=SourceReadOutcome.PUBLISHER_LINKED_PAGES,
                attempted_target_urls=(page.url,),
                discovery_truncated=True,
            )

    board = RescannedWorkday(page, listings)
    worker = _enqueue_scan(durable_database, workspace_id, board)
    assert worker.run_once()
    assert worker.run_once()
    assert not worker.run_once()
    with durable_database.session() as session:
        old_scan = session.scalars(select(SourceScan)).one()
        old_root = old_scan.correlation_id
        if next_day:
            old_scan.scanned_on -= timedelta(days=1)
    board.fresh = True
    worker = _enqueue_scan(durable_database, workspace_id, board)
    assert worker.run_once()
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan).order_by(*LATEST_SCAN_FIRST)).first()
        assert scan is not None
        assert scan.correlation_id != old_root
        continuation = session.scalars(
            select(SourceScanContinuation).where(SourceScanContinuation.source_scan_id == scan.id)
        ).one()
        carried = session.scalars(
            select(SourceScanFrontierItem).where(
                SourceScanFrontierItem.continuation_id == continuation.id,
                SourceScanFrontierItem.url == held.url,
            )
        ).one()
        assert (
            SourceDetailCheckpoint.from_json(carried.detail_checkpoint).listing_ids
            == checkpoint.listing_ids
        )
        if next_day:
            assert carried.correlation_id == scan.correlation_id
        carried.status = "failed"
        carried.attempt_count = 3
        carried.last_error = "Retained manual recovery failure"
    assert worker.run_once()
    assert not worker.run_once()
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan).order_by(*LATEST_SCAN_FIRST)).first()
        assert scan is not None
        assert scan.read_count == scan.raw_count == 3
        assert not scan.complete
        assert scan.discovery_truncated
        assert scan.detail_attempted_count == 0
        retained = session.scalars(
            select(SourceScanFrontierItem).where(
                SourceScanFrontierItem.continuation_id == continuation.id,
                SourceScanFrontierItem.url == held.url,
            )
        ).one()
        assert retained.status == "failed"
        assert retained.attempt_count == 3
        assert retained.last_error == "Retained manual recovery failure"


@pytest.mark.parametrize("detail_only", [False, True])
def test_terminal_workday_counts_observed_metadata_beside_protected_and_foreign_urls(
    durable_database: Database, detail_only: bool
) -> None:
    workspace_id = _workspace(durable_database)
    root = "https://globex.wd1.myworkdayjobs.com/en-US/Careers"
    source_id = _source(durable_database, workspace_id, "Globex", root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    listings = [
        source_listing("Production Technician", f"{root}/job/Remote/Technician_R-{index}", "Globex")
        for index in range(3)
    ]
    _stage_known_rows(durable_database, workspace_id, source_id, (listings[1].url, listings[2].url))
    with durable_database.session() as session:
        protected_ids = list(session.scalars(select(PendingDiscovery.id)))
    for pending_id in protected_ids:
        dismiss_pending_discovery(durable_database, workspace_id, pending_id)
    target = SourceContinuationTarget(
        url=listings[1].url if detail_only else f"{root}?offset=1",
        label="More openings",
        label_source="visible",
        source_url=root,
        structural_context="main",
        relations=("publisher-content",),
        deferred_reason="workday_detail_pending" if detail_only else "workday_page_pending",
        detail_checkpoint=SourceDetailCheckpoint(
            platform="workday" if detail_only else "search:workday",
            board_url=root,
            pending_ids=(listings[1].url,) if detail_only else (),
            listing_ids=tuple(item.url for item in listings),
            listing_unstable=True,
        ),
    )
    protected = replace(
        target,
        url=listings[2].url,
        deferred_reason="workday_detail_pending",
        detail_checkpoint=SourceDetailCheckpoint(
            platform="workday", board_url=root, pending_ids=(listings[2].url,)
        ),
    )
    foreign = [
        replace(listings[1], url=listings[1].url.replace("Careers", "Other")),
        replace(listings[1], url=listings[1].url.replace("en-US", "fr-FR")),
        replace(listings[1], url=listings[1].url.replace("globex.wd1", "acme.wd1")),
    ]

    class ProtectedWorkday(_DeferringBoard):
        def listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            return SourceRead(
                listings=listings[:1],
                complete=False,
                raw_count=1,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=(target, protected),
                discovery_truncated=True,
            )

        def continue_listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            assert {listings[1].url, listings[2].url} <= set(
                kwargs["options"].protected_posting_urls
            )
            return SourceRead(
                listings=[listings[1], *foreign],
                complete=False,
                raw_count=4,
                outcome=SourceReadOutcome.PUBLISHER_LINKED_PAGES,
                attempted_target_urls=(target.url,),
                protected_posting_urls=(listings[2].url,),
                discovery_truncated=True,
            )

    worker = _enqueue_scan(durable_database, workspace_id, ProtectedWorkday(target, listings))
    assert worker.run_once()
    assert worker.run_once()
    assert not worker.run_once()
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert scan.read_count == scan.raw_count == 2
        assert not scan.complete
        assert scan.detail_attempted_count == scan.detail_read_count == 0
        protected_row = session.scalars(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.url == listings[2].url)
        ).one()
        assert protected_row.status == "completed"
        assert protected_row.attempt_count == 0
        assert protected_row.deferred_reason == "protected_posting"
        observed = set(session.scalars(select(SourceScanOutcome.url)))
        assert listings[1].url in observed
        assert listings[2].url not in observed


def test_terminal_workday_listing_count_keeps_observations_when_totals_move(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workday, "WORKDAY_PAGE", 2)
    monkeypatch.setattr(workday, "WORKDAY_MAX_PAGES", 1)
    workspace_id = _workspace(durable_database)
    root = "https://globex.wd1.myworkdayjobs.com/en-US/Careers"
    _source(durable_database, workspace_id, "Globex", root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")

    class MovingWorkday(_WorkdayPages):
        def post(self, url: str, *, json: dict[str, object], **kwargs: object) -> httpx.Response:
            offset = int(str(json["offset"]))
            self.requested.append(f"{url}?limit=2&offset={offset}")
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={
                    "total": 4 if offset == 0 else 6,
                    "jobPostings": [
                        {
                            "title": "Production Technician",
                            "externalPath": f"/job/Remote/Technician_R-{index}",
                        }
                        for index in ([0, 1] if offset == 0 else [2, 3])
                    ],
                },
            )

    site = MovingWorkday()
    worker = _enqueue_scan(durable_database, workspace_id, BoardApiFetcher(client=site.client()))
    assert worker.run_once()
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert scan.read_count == scan.raw_count == 2
    assert worker.run_once()
    assert not worker.run_once()
    assert len(site.requested) == 3
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert scan.read_count == scan.raw_count == 4
        assert not scan.complete
        assert scan.discovery_truncated
        assert scan.detail_attempted_count == 0
        assert (
            session.scalars(
                select(SourceScanFrontierItem).where(SourceScanFrontierItem.status == "pending")
            ).all()
            == []
        )


@pytest.mark.parametrize("interruption", ["cancel", "source_loss", "stale_generation"])
def test_terminal_workday_listing_counts_obey_the_source_write_guard(
    durable_database: Database, interruption: str
) -> None:
    workspace_id = _workspace(durable_database)
    root = "https://globex.wd1.myworkdayjobs.com/en-US/Careers"
    _source(durable_database, workspace_id, "Globex", root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    listings = [
        source_listing("Production Technician", f"{root}/job/Remote/Technician_R-{index}", "Globex")
        for index in range(3)
    ]
    page = SourceContinuationTarget(
        url=f"{root}?offset=1",
        label="More openings",
        label_source="visible",
        source_url=root,
        structural_context="main",
        relations=("publisher-content",),
        deferred_reason="workday_page_pending",
    )

    class InterruptedWorkday(_DeferringBoard):
        def listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            return super().listings(*args, **kwargs)

        def continue_listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            read = super().continue_listings(*args, **kwargs)
            with durable_database.session() as session:
                task = session.scalars(
                    select(BackgroundTask).where(
                        BackgroundTask.task_type == "source.continue",
                        BackgroundTask.status == "running",
                    )
                ).one()
                task_id = task.id
                if interruption == "source_loss":
                    member = session.scalars(select(SourceScanMember)).one()
                    member.active_attempt_count = 999
                elif interruption == "stale_generation":
                    continuation = session.scalars(select(SourceScanContinuation)).one()
                    continuation.generation += 1
            if interruption == "cancel":
                TaskQueueService(durable_database).request_cancellation(
                    task_id, workspace_id=workspace_id
                )
            return read

    worker = _enqueue_scan(durable_database, workspace_id, InterruptedWorkday(page, listings))
    assert worker.run_once()
    assert worker.run_once()
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert scan.read_count == scan.raw_count == 1
        assert not scan.complete
        assert set(
            session.scalars(
                select(SourceScanOutcome.url).where(
                    SourceScanOutcome.candidate_kind == "board_listing"
                )
            )
        ) == {listings[0].url}


@pytest.mark.parametrize("lost_owner", ["attempt", "task"])
def test_workday_terminal_interruption_keeps_counts_after_earlier_listing_progress(
    durable_database: Database, lost_owner: str
) -> None:
    workspace_id = _workspace(durable_database)
    root = "https://globex.wd1.myworkdayjobs.com/en-US/Careers"
    _source(durable_database, workspace_id, "Globex", root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    listings = [
        source_listing("Production Technician", f"{root}/job/Remote/Technician_R-{index}", "Globex")
        for index in range(3)
    ]
    first = SourceContinuationTarget(
        url=f"{root}?offset=1",
        label="More openings",
        label_source="visible",
        source_url=root,
        structural_context="main",
        relations=("publisher-content",),
        deferred_reason="workday_page_pending",
    )
    final = replace(first, url=f"{root}?offset=3")

    class InterruptedAfterProgress(_DeferringBoard):
        failures = 0

        def listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            return super().listings(*args, **kwargs)

        def continue_listings(
            self, employer: str, targets: tuple[SourceContinuationTarget, ...], **kwargs: Any
        ) -> SourceRead:
            if targets[0].url == first.url:
                return SourceRead(
                    listings=listings[1:],
                    complete=False,
                    raw_count=2,
                    outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                    attempted_target_urls=(first.url,),
                    deferred_targets=(final,),
                    discovery_truncated=True,
                )
            self.failures += 1
            if self.failures == MAX_AUTOMATIC_ATTEMPTS:
                with durable_database.session() as session:
                    member = session.scalars(select(SourceScanMember)).one()
                    if lost_owner == "attempt":
                        member.active_attempt_count = 999
                    else:
                        member.active_task_id = member.root_task_id
            raise RuntimeError("Synthetic final page failure")

    board = InterruptedAfterProgress(first, listings)
    worker = _enqueue_scan(durable_database, workspace_id, board)
    assert worker.run_once()
    assert worker.run_once()
    for _ in range(MAX_AUTOMATIC_ATTEMPTS - 1):
        assert worker.run_once()
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert scan.read_count == scan.raw_count == 1
        observed = set(
            session.scalars(
                select(SourceScanOutcome.url).where(
                    SourceScanOutcome.candidate_kind == "board_listing"
                )
            )
        )
        assert observed == {listing.url for listing in listings}
    assert worker.run_once()
    assert board.failures == MAX_AUTOMATIC_ATTEMPTS
    assert not worker.run_once()
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert scan.read_count == scan.raw_count == 1
        assert not scan.complete
        assert scan.detail_attempted_count == scan.detail_read_count == 0
        failed = session.scalars(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.url == final.url)
        ).one()
        assert failed.status == "failed"
        assert failed.attempt_count == MAX_AUTOMATIC_ATTEMPTS
        assert failed.last_error == "The continuation task stopped before this read was recorded."
        assert failed.automatic_eligible
        completed = session.scalars(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.url == first.url)
        ).one()
        assert completed.status == "completed"


@pytest.mark.parametrize(
    "missing",
    ["member", "receipt", "root", "source", "scan", "continuation", "root_outcomes"],
)
@pytest.mark.parametrize("certified", [False, True])
def test_terminal_workday_scan_without_current_root_evidence_retains_initial_counts(
    durable_database: Database, missing: str, certified: bool
) -> None:
    workspace_id = _workspace(durable_database)
    root = "https://globex.wd1.myworkdayjobs.com/en-US/Careers"
    _source(durable_database, workspace_id, "Globex", root)
    other_source_id = _source(
        durable_database, workspace_id, "Acme", "https://acme.example/careers"
    )
    with durable_database.session() as session:
        other_source = session.get(Source, other_source_id)
        assert other_source is not None
        other_source.archived_at = utc_now()
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    listings = [
        source_listing("Production Technician", f"{root}/job/Remote/Technician_R-{index}", "Globex")
        for index in range(4)
    ]
    page = SourceContinuationTarget(
        url=f"{root}?offset=1",
        label="More openings",
        label_source="visible",
        source_url=root,
        structural_context="main",
        relations=("publisher-content",),
        deferred_reason="workday_page_pending",
    )

    class UnlinkedWorkday(_DeferringBoard):
        def listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            return replace(
                super().listings(*args, **kwargs),
                exhausted_outcome=SourceReadOutcome.ATS_COMPLETE,
            )

        def continue_listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            read = super().continue_listings(*args, **kwargs)
            with durable_database.session() as session:
                member = session.scalars(select(SourceScanMember)).one()
                continuation = session.scalars(select(SourceScanContinuation)).one()
                if missing == "member":
                    continuation.batch_member_id = None
                elif missing == "receipt":
                    member.result = {}
                elif missing == "root":
                    continuation.correlation_id = None
                elif missing == "source":
                    member.source_id = other_source_id
                elif missing == "scan":
                    member.source_scan_id = None
                elif missing == "continuation":
                    member.continuation_id = None
                elif missing == "root_outcomes":
                    for outcome in session.scalars(
                        select(SourceScanOutcome).where(
                            SourceScanOutcome.task_id == member.root_task_id
                        )
                    ):
                        session.delete(outcome)
            return replace(
                read,
                complete=certified,
                discovery_truncated=not certified,
                outcome=SourceReadOutcome.ATS_COMPLETE
                if certified
                else SourceReadOutcome.PUBLISHER_LINKED_PAGES,
            )

    worker = _enqueue_scan(durable_database, workspace_id, UnlinkedWorkday(page, listings))
    assert worker.run_once()
    assert worker.run_once()
    with durable_database.session() as session:
        scan = session.scalars(
            select(SourceScan).where(SourceScan.correlation_id.is_not(None))
        ).one()
        assert scan.read_count == scan.raw_count == 1
        assert scan.complete is (certified and missing != "member")
        assert scan.detail_attempted_count == 0


@pytest.mark.parametrize("retry_failure", [False, True])
@pytest.mark.parametrize("corporate_root", [False, True])
@pytest.mark.parametrize("certified", [False, True])
def test_workday_detail_recovery_retains_successes_and_settles_only_after_every_owed_url(
    durable_database: Database, retry_failure: bool, corporate_root: bool, certified: bool
) -> None:
    workspace_id = _workspace(durable_database)
    root = "https://globex.wd1.myworkdayjobs.com/en-US/Careers"
    source_url = "https://globex.example/careers" if corporate_root else root
    _source(durable_database, workspace_id, "Globex", source_url)
    state_target_titles(durable_database, workspace_id, "Data Product Manager")
    listings = [
        SourceListing(
            employer="Globex",
            title="Data Product Manager",
            url=f"{root}/job/Remote/Data-Product-Manager_R-{index}",
            facts=published_listing_facts("workday", locations="2 Locations"),
        )
        for index in range(3)
    ]
    reads: list[str] = []
    recovered: list[str] = []

    def checked(url: str) -> PostingCheck:
        return PostingCheck(
            status="readable",
            note="Published complete detail",
            platform="workday",
            employer="Globex",
            title="Data Product Manager",
            location="Remote, Canada",
            facts=published_listing_facts("workday", locations="Remote, Canada"),
            text="Own the data platform. Remote role in Canada.",
            source=PostingSource(
                publisher="workday", record="{}", description_paths=("description",)
            ),
            location_entries=1,
        )

    class RecoverableBoard(BoardApiFetcher):
        def listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            return SourceRead(
                listings=listings,
                complete=certified,
                raw_count=3,
                outcome=SourceReadOutcome.ATS_COMPLETE
                if certified
                else SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=() if certified else (old_page,),
                discovery_truncated=not certified,
                exhausted_outcome=None if certified else SourceReadOutcome.ATS_COMPLETE,
            )

        def workday_detail(self, url: str) -> PostingCheck:
            recovered.append(url)
            if retry_failure and url == listings[2].url and recovered.count(url) == 1:
                return PostingCheck(
                    status="unreadable", note="Temporary refusal", platform="workday"
                )
            return checked(url)

    def initial(url: str) -> PostingCheck:
        reads.append(url)
        if url == listings[0].url:
            return checked(url)
        return PostingCheck(status="unreadable", note="Temporary refusal", platform="workday")

    board = RecoverableBoard(
        client=policy_client(lambda url: httpx.Response(500, request=httpx.Request("GET", url)))
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=board,
        workday_detail_checker=initial,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    with durable_database.session() as session:
        source = session.scalars(select(Source)).one()
        session.expunge(source)
    now = utc_now()
    old_page = SourceContinuationTarget(
        url=f"{root}?offset=20",
        label="More board pages",
        label_source="visible",
        source_url=source_url,
        structural_context="main",
        relations=("publisher-content",),
        automatic_eligible=False,
        deferred_reason="workday_page_pending",
        detail_checkpoint=SourceDetailCheckpoint(
            platform="search:workday",
            board_url=root,
            listing_unstable=True,
        ),
    )
    service.sweep.recording.record_scan(
        source,
        now.date(),
        "incomplete",
        0,
        0,
        "Unstable enumeration",
        started_at=now,
        completed_at=now,
        read_outcome=SourceReadOutcome.CRAWL_CONTINUATION,
        deferred_targets=(old_page,),
        read_context=ScanReadContext(
            complete=False,
            discovery_truncated=True,
            exhausted_outcome=SourceReadOutcome.ATS_COMPLETE,
        ),
    )
    service.scan(workspace_id)
    with durable_database.session() as session:
        frontier = session.scalars(select(SourceScanFrontierItem)).all()
        assert all(row.source_url == source_url for row in frontier)
        assert all(
            SourceDetailCheckpoint.from_json(row.detail_checkpoint).board_url == root
            for row in frontier
        )
        assert {row.url for row in frontier} == {
            *(listing.url for listing in listings[1:]),
            *((old_page.url,) if not certified else ()),
        }
    worker = Worker(
        durable_database,
        worker_id="workday-recovery",
        handlers={
            "source.continue": SourceContinuationHandler(durable_database, board, service.sweep)
        },
    )

    assert worker.run_once()
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        remaining = session.scalars(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.status == "pending")
        ).all()
        assert scan.complete is (certified and not retry_failure)
        assert {row.url for row in remaining} == {
            *((listings[2].url,) if retry_failure else ()),
            *((old_page.url,) if not certified else ()),
        }
    if retry_failure:
        assert worker.run_once()
    assert not worker.run_once()
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        rows = session.scalars(select(SourceScanFrontierItem)).all()
        assert scan.complete is certified
        assert scan.read_outcome == (
            SourceReadOutcome.ATS_COMPLETE if certified else SourceReadOutcome.CRAWL_CONTINUATION
        )
        assert all(row.status == "completed" for row in rows if row.url != old_page.url)
        if certified and not corporate_root:
            assert scan.detail_coverage is not None
            assert scan.detail_coverage["posting_urls"] == [listing.url for listing in listings]
        if not certified:
            assert scan.detail_coverage is None
        pending = session.scalars(select(PendingDiscovery)).all()
        assert len(pending) == 3
        assert all(not row.staged_facts["locations"]["complete"] for row in pending)
    assert reads == [listing.url for listing in listings]
    assert recovered.count(listings[0].url) == 0
    assert recovered.count(listings[1].url) == 1
    assert recovered.count(listings[2].url) == (2 if retry_failure else 1)


@pytest.mark.parametrize(
    "certificate",
    ["certified", "missing", "foreign", "truncated", "protected_omitted", "observed_omitted"],
)
@pytest.mark.parametrize("corporate_root", [False, True])
def test_later_workday_enumeration_rebases_owed_details_before_global_completion(
    durable_database: Database,
    certificate: str,
    corporate_root: bool,
) -> None:
    workspace_id = _workspace(durable_database)
    root = "https://globex.wd1.myworkdayjobs.com/en-US/Careers"
    source_url = "https://globex.example/careers" if corporate_root else root
    source_id = _source(durable_database, workspace_id, "Globex", source_url)
    state_target_titles(durable_database, workspace_id, "Data Product Manager")
    listings = [
        SourceListing(
            employer="Globex",
            title="Data Product Manager",
            url=f"{root}/job/Remote/Data-Product-Manager_R-{index}",
            facts=published_listing_facts("workday", locations="2 Locations"),
        )
        for index in range(2)
    ]

    def page(offset: int, *, automatic: bool) -> SourceContinuationTarget:
        return SourceContinuationTarget(
            url=f"{root}?offset={offset}",
            label="More openings",
            label_source="visible",
            source_url=source_url,
            structural_context="main",
            relations=("publisher-content",),
            deferred_reason="workday_page_pending",
            automatic_eligible=automatic,
            detail_checkpoint=SourceDetailCheckpoint(
                platform="search:workday", board_url=root, listing_unstable=not automatic
            ),
        )

    old_page, fresh_page = page(80, automatic=False), page(20, automatic=True)
    recovered: list[str] = []

    class EnumeratedBoard(BoardApiFetcher):
        def listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            return SourceRead(
                listings=[listings[0]],
                complete=False,
                raw_count=1,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=(fresh_page,),
                discovery_truncated=True,
                exhausted_outcome=SourceReadOutcome.ATS_COMPLETE,
                skip_reasons={
                    "repeated_workday_page": 1,
                    "duplicate_workday_identity": 2,
                    "incomplete_oracle_listing": 1,
                    "protected_posting": 1,
                },
            )

        def continue_listings(
            self, employer: str, targets: tuple[SourceContinuationTarget, ...], **kwargs: Any
        ) -> SourceRead:
            if any(target.url == fresh_page.url for target in targets):
                coverage_root = (
                    root if certificate != "foreign" else root.replace("Careers", "Other")
                )
                coverage = (
                    None
                    if certificate == "missing"
                    else SourceDetailCoverage(
                        platform="workday",
                        board_url=coverage_root,
                        posting_urls=tuple(
                            item.url.replace(root, coverage_root)
                            for item in (
                                listings[:1] if certificate == "observed_omitted" else listings
                            )
                        ),
                    )
                )
                return SourceRead(
                    listings=[listings[1]],
                    complete=False,
                    raw_count=1,
                    outcome=SourceReadOutcome.PUBLISHER_LINKED_PAGES,
                    attempted_target_urls=(fresh_page.url,),
                    listing_coverage=coverage,
                    discovery_truncated=certificate == "truncated",
                    protected_posting_urls=(f"{root}/job/Remote/Protected_R-9",)
                    if certificate == "protected_omitted"
                    else (),
                )
            return super().continue_listings(employer, targets, **kwargs)

        def workday_detail(self, url: str) -> PostingCheck:
            recovered.append(url)
            if url == listings[1].url and recovered.count(url) == 1:
                return PostingCheck(
                    status="unreadable", note="Temporary refusal", platform="workday"
                )
            return PostingCheck(
                status="readable",
                note="Published complete detail",
                platform="workday",
                title="Data Product Manager",
                text="Own the data platform remotely.",
                facts=published_listing_facts("workday", locations="Remote, Canada"),
                source=PostingSource(
                    publisher="workday", record="{}", description_paths=("description",)
                ),
            )

    board = EnumeratedBoard(
        client=policy_client(lambda url: httpx.Response(500, request=httpx.Request("GET", url)))
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=board,
        workday_detail_checker=lambda url: PostingCheck(
            status="unreadable", note="Initial refusal", platform="workday"
        ),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    with durable_database.session() as session:
        source = session.get(Source, source_id)
        assert source is not None
        session.expunge(source)
    now = utc_now()
    service.sweep.recording.record_scan(
        source,
        now.date(),
        "incomplete",
        0,
        0,
        "Old unstable page hold",
        started_at=now,
        completed_at=now,
        read_outcome=SourceReadOutcome.CRAWL_CONTINUATION,
        deferred_targets=(old_page,),
        read_context=ScanReadContext(
            complete=False,
            discovery_truncated=True,
            exhausted_outcome=SourceReadOutcome.ATS_COMPLETE,
        ),
    )
    service.scan(workspace_id)
    worker = Worker(
        durable_database,
        worker_id="later-workday-enumeration",
        handlers={
            "source.continue": SourceContinuationHandler(durable_database, board, service.sweep)
        },
    )
    assert worker.run_once()
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        frontier = session.scalars(select(SourceScanFrontierItem)).all()
        assert not scan.complete
        assert scan.detail_coverage is None
        assert scan.discovery_truncated is (certificate != "certified")
        assert ("repeated_workday_page" in scan.skip_reasons) is (certificate != "certified")
        assert ("duplicate_workday_identity" in scan.skip_reasons) is (certificate != "certified")
        assert scan.skip_reasons.get("incomplete_oracle_listing") == 1
        assert scan.skip_reasons.get("protected_posting") == 1
        assert any(row.url == old_page.url for row in frontier) is (certificate != "certified")
        owed = [row for row in frontier if row.deferred_reason == "workday_detail_pending"]
        assert {row.url for row in owed} == {item.url for item in listings}
        for row in owed:
            checkpoint = SourceDetailCheckpoint.from_json(row.detail_checkpoint)
            assert checkpoint.pending_ids == (row.url,)
            assert checkpoint.listing_facts[0][1].locations.unresolved_values == ("2 Locations",)
            if certificate == "certified":
                assert checkpoint.listing_ids == tuple(item.url for item in listings)
                assert checkpoint.listing_total == 2
                assert checkpoint.failed_attempts == ((row.url, 1),)
    assert worker.run_once()
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert not scan.complete
        remaining = session.scalars(
            select(SourceScanFrontierItem).where(
                SourceScanFrontierItem.status == "pending",
                SourceScanFrontierItem.deferred_reason == "workday_detail_pending",
            )
        ).all()
        assert {row.url for row in remaining} == {listings[1].url}
    assert worker.run_once()
    assert not worker.run_once()
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert scan.complete is (certificate == "certified")
        assert scan.read_outcome == (
            SourceReadOutcome.ATS_COMPLETE
            if certificate == "certified"
            else SourceReadOutcome.CRAWL_CONTINUATION
        )
    assert recovered.count(listings[0].url) == 1
    assert recovered.count(listings[1].url) == 2


def test_failed_staging_with_no_attempts_cannot_retire_workday_enumeration_holds(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = _workspace(durable_database)
    root = "https://globex.wd1.myworkdayjobs.com/en-US/Careers"
    source_id = _source(durable_database, workspace_id, "Globex", root)
    scan_id = _scan_row(
        durable_database, source_id, 0, exhausted_outcome=SourceReadOutcome.ATS_COMPLETE
    )
    checkpoint = SourceDetailCheckpoint(
        platform="search:workday", board_url=root, listing_unstable=True
    )
    urls = (f"{root}?offset=20", f"{root}?offset=80")
    continuation_id = _seed_continuation(
        durable_database,
        workspace_id,
        scan_id,
        [
            {
                "url": url,
                "source_url": root,
                "deferred_reason": "workday_page_pending",
                "automatic_eligible": index == 0,
                "detail_checkpoint": checkpoint.as_json(),
            }
            for index, url in enumerate(urls)
        ],
    )
    with durable_database.session() as session:
        scan = session.get(SourceScan, scan_id)
        assert scan is not None
        scan.discovery_truncated = True

    class EnumeratedBoard(BoardApiFetcher):
        def continue_listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            return SourceRead(
                listings=[],
                complete=False,
                raw_count=0,
                outcome=SourceReadOutcome.PUBLISHER_LINKED_PAGES,
                attempted_target_urls=(),
                listing_coverage=SourceDetailCoverage(
                    platform="workday", board_url=root, posting_urls=()
                ),
            )

    board = EnumeratedBoard(
        client=policy_client(lambda url: httpx.Response(500, request=httpx.Request("GET", url)))
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=board,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    handler = SourceContinuationHandler(durable_database, board, service.sweep)

    def failed_staging(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Synthetic staging failure before receipt")

    monkeypatch.setattr(service.sweep, "stage_continuation", failed_staging)
    queue = TaskQueueService(durable_database)
    queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.continue",
        payload={"continuation_id": continuation_id, "generation": 1},
        idempotency_key="failed-empty-workday-staging",
    )
    worker = Worker(
        durable_database,
        worker_id="failed-empty-workday-staging",
        handlers={"source.continue": handler},
    )
    assert worker.run_once()
    with durable_database.session() as session:
        scan = session.get(SourceScan, scan_id)
        assert scan is not None and not scan.complete and scan.discovery_truncated
        assert set(session.scalars(select(SourceScanFrontierItem.url))) == set(urls)


@pytest.mark.parametrize("a_succeeds", [False, True])
def test_partial_workday_rescan_keeps_owed_details_omitted_from_the_fresh_list(
    durable_database: Database,
    a_succeeds: bool,
) -> None:
    workspace_id = _workspace(durable_database)
    root = "https://globex.wd1.myworkdayjobs.com/en-US/Careers"
    source_id = _source(durable_database, workspace_id, "Globex", root)
    state_target_titles(durable_database, workspace_id, "Data Product Manager")
    listings = [
        SourceListing(
            employer="Globex",
            title="Data Product Manager",
            url=f"{root}/job/Remote/Data-Product-Manager_R-{index}",
            facts=published_listing_facts("workday", locations="2 Locations"),
        )
        for index in range(2)
    ]

    class PartialBoard(BoardApiFetcher):
        current: SourceRead

        def listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            return self.current

    board = PartialBoard(
        client=policy_client(lambda url: httpx.Response(500, request=httpx.Request("GET", url)))
    )
    board.current = SourceRead(
        listings=listings,
        complete=True,
        raw_count=2,
        outcome=SourceReadOutcome.ATS_COMPLETE,
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=board,
        workday_detail_checker=lambda url: PostingCheck(
            status="unreadable", note="Temporary detail refusal", platform="workday"
        ),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    with durable_database.session() as session:
        source = session.get(Source, source_id)
        assert source is not None
        session.expunge(source)
    now = utc_now()
    old_targets = tuple(
        SourceContinuationTarget(
            url=listing.url,
            label=listing.title,
            label_source="visible",
            source_url=root,
            structural_context="main",
            relations=("publisher-content",),
            deferred_reason="workday_detail_pending",
            automatic_eligible=False,
            detail_checkpoint=SourceDetailCheckpoint(
                platform="workday",
                board_url=root,
                pending_ids=(listing.url,),
                listing_ids=tuple(item.url for item in listings),
                listing_total=2,
                failed_attempts=((listing.url, 1),),
                listing_facts=((listing.url, listing.facts),),
            ),
        )
        for listing in listings
    )
    service.sweep.recording.record_scan(
        source,
        now.date(),
        "incomplete",
        0,
        0,
        "Owed details",
        started_at=now,
        completed_at=now,
        read_outcome=SourceReadOutcome.CRAWL_CONTINUATION,
        deferred_targets=old_targets,
        read_context=ScanReadContext(
            complete=False, exhausted_outcome=SourceReadOutcome.ATS_COMPLETE
        ),
    )
    board.current = SourceRead(
        listings=[listings[0]],
        complete=False,
        raw_count=1,
        outcome=SourceReadOutcome.PUBLISHER_LINKED_PAGES,
        discovery_truncated=True,
    )

    refreshed: list[str] = []

    def refreshed_detail(url: str) -> PostingCheck:
        refreshed.append(url)
        assert url == listings[0].url
        if not a_succeeds:
            return PostingCheck(
                status="unreadable", note="Temporary detail refusal", platform="workday"
            )
        return PostingCheck(
            status="readable",
            note="Published complete detail",
            platform="workday",
            title="Data Product Manager",
            text="Own the data product roadmap and analytics strategy.",
            source=PostingSource(
                publisher="workday", record="{}", description_paths=("description",)
            ),
            facts=published_listing_facts("workday", locations="Remote"),
        )

    service.sweep.posting_checker = refreshed_detail
    result = service.scan(workspace_id)
    assert result["withdrawn"] == []
    assert refreshed == [listings[0].url]
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        frontier = session.scalars(select(SourceScanFrontierItem)).all()
        assert not scan.complete
        assert scan.detail_coverage is None
        assert {row.url for row in frontier} == {listing.url for listing in listings}
        rows = {row.url: row for row in frontier}
        assert rows[listings[1].url].status == "pending"
        assert rows[listings[1].url].deferred_reason == "workday_detail_pending"
        observed = rows[listings[0].url]
        assert observed.status == ("completed" if a_succeeds else "pending")
        if a_succeeds:
            assert observed.deferred_reason == "posting_detail_completed"
        else:
            assert observed.deferred_reason == "workday_detail_pending"
            checkpoint = SourceDetailCheckpoint.from_json(observed.detail_checkpoint)
            assert checkpoint.listing_ids == (listings[0].url,)
            assert checkpoint.listing_total is None


def test_workday_details_first_seen_during_continuation_remain_owed_after_failure(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    root = "https://globex.wd1.myworkdayjobs.com/en-US/Careers"
    _source(durable_database, workspace_id, "Globex", root)
    state_target_titles(durable_database, workspace_id, "Data Product Manager")
    target = replace(
        _continuation(f"{root}?page=2"),
        source_url=root,
        deferred_reason="workday_page_pending",
    )
    listing = SourceListing(
        employer="Globex",
        title="Data Product Manager",
        url=f"{root}/job/Remote/Data-Product-Manager_R-1",
        facts=published_listing_facts("workday", locations="2 Locations"),
    )

    class ContinuedBoard(BoardApiFetcher):
        def listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            return SourceRead(
                listings=[],
                complete=False,
                raw_count=0,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=(target,),
                exhausted_outcome=SourceReadOutcome.ATS_COMPLETE,
            )

        def continue_listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            return SourceRead(
                listings=[listing],
                complete=True,
                raw_count=1,
                outcome=SourceReadOutcome.ATS_COMPLETE,
            )

    board = ContinuedBoard(
        client=policy_client(lambda url: httpx.Response(500, request=httpx.Request("GET", url)))
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=board,
        workday_detail_checker=lambda url: PostingCheck(
            status="unreadable", note="Temporary detail refusal", platform="workday"
        ),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.scan(workspace_id)
    worker = Worker(
        durable_database,
        worker_id="continued-workday-detail",
        handlers={
            "source.continue": SourceContinuationHandler(durable_database, board, service.sweep)
        },
    )

    assert worker.run_once()

    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        pending = session.scalars(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.status == "pending")
        ).all()
        assert not scan.complete
        assert scan.detail_coverage is None
        assert [row.url for row in pending] == [listing.url]
        assert pending[0].deferred_reason == "workday_detail_pending"
        assert pending[0].automatic_eligible
        checkpoint = SourceDetailCheckpoint.from_json(pending[0].detail_checkpoint)
        assert checkpoint.pending_ids == (listing.url,)
        assert checkpoint.listing_facts == ((listing.url, listing.facts),)


def test_checkpoint_listing_facts_round_trip_and_reject_unowned_identity() -> None:
    url = "https://globex.wd1.myworkdayjobs.com/en-US/Careers/job/Data_R-1"
    facts = published_listing_facts("workday", locations="3 Locations")
    checkpoint = SourceDetailCheckpoint(
        platform="workday",
        board_url="https://globex.wd1.myworkdayjobs.com/en-US/Careers",
        pending_ids=(url,),
        listing_facts=((url, facts),),
    )
    assert SourceDetailCheckpoint.from_json(checkpoint.as_json()) == checkpoint
    with pytest.raises(ValueError, match="unique"):
        replace(checkpoint, listing_facts=((url, facts), (url, facts)))
    with pytest.raises(ValueError, match="pending detail identities"):
        replace(checkpoint, pending_ids=())


def test_detail_checkpoint_survives_durable_frontier_and_carry_forward() -> None:
    checkpoint = SourceDetailCheckpoint(
        platform="jazzhr",
        board_url="https://publisher.example/careers",
        completed=(("read-role", "https://publisher.example/jobs/read-role"),),
        failed_attempts=(("retry-role", 2),),
        pending_ids=("retry-role", "new-role"),
    )
    target = replace(
        _continuation("https://publisher.example/careers?clearscout_detail_cursor=remaining"),
        detail_checkpoint=checkpoint,
    )
    row = frontier_item("continuation", target.url, target, origin="scan", correlation_id=None)
    restored = frontier_target(row)
    assert restored.detail_checkpoint == checkpoint
    carried = carried_frontier_item(
        "next-continuation", row, correlation_id=None, relisted=frozenset()
    )
    assert frontier_target(carried).detail_checkpoint == checkpoint


def test_proven_current_details_settle_a_prior_deadline_cut() -> None:
    from clearscout.application.discovery.scan_outcome import settle_drained_scan

    coverage = SourceDetailCoverage(
        platform="jazzhr",
        board_url="https://publisher.example/careers",
        posting_urls=("https://publisher.example/jobs/current-role",),
    )
    scan = SourceScan(
        status="incomplete",
        read_outcome=SourceReadOutcome.CRAWL_CONTINUATION,
        skip_reasons={"time_limit_reached": 1},
        discovery_truncated=False,
        exhausted_outcome=SourceReadOutcome.ATS_COMPLETE,
        detail_coverage=coverage.as_json(),
        complete=False,
        raw_count=1,
        read_count=1,
        result_count=1,
        notes="",
    )
    settle_drained_scan(scan, unresolved_count=1)
    assert (scan.complete, scan.status) == (False, "incomplete")
    assert settle_drained_scan(scan, unresolved_count=0)
    assert scan.complete
    assert scan.read_outcome is SourceReadOutcome.ATS_COMPLETE


def test_a_deadline_cut_without_current_detail_coverage_cannot_settle() -> None:
    from clearscout.application.discovery.scan_outcome import settle_drained_scan

    scan = SourceScan(
        status="incomplete",
        read_outcome=SourceReadOutcome.CRAWL_CONTINUATION,
        skip_reasons={"time_limit_reached": 1},
        discovery_truncated=False,
        exhausted_outcome=SourceReadOutcome.ATS_COMPLETE,
        detail_coverage=None,
        complete=False,
    )
    assert not settle_drained_scan(scan, unresolved_count=0)
    assert not scan.complete


@pytest.mark.parametrize(
    ("saved", "observed", "platform", "same_scope"),
    [
        (
            "https://app.dover.com/dover/careers/7c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f",
            "https://app.dover.io/dover/careers/7c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f",
            "dover",
            True,
        ),
        (
            "https://one.example/careers",
            "https://two.example/careers",
            "jazzhr",
            False,
        ),
        (
            "https://one.applytojob.com/apply",
            "https://two.applytojob.com/apply",
            "jazzhr",
            False,
        ),
    ],
)
def test_current_detail_coverage_names_the_same_publisher_board(
    saved: str, observed: str, platform: str, same_scope: bool
) -> None:
    scope = board_read_scope(saved, platform)
    assert (scope is not None and scope == board_read_scope(observed, platform)) is same_scope


def test_adp_coverage_ignores_tracking_fields_but_preserves_locale_and_tenant() -> None:
    root = "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
    tenant = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    center = "19000101_000001"
    saved = f"{root}?cid={tenant}&ccId={center}&type=MP&lang=en_CA"
    coverage = f"{root}?cid={tenant}&ccId={center}&lang=en_CA"
    scope = board_read_scope(saved, "adp")
    assert scope is not None
    assert scope == board_read_scope(coverage, "adp")
    assert scope != board_read_scope(coverage.replace(f"&ccId={center}", ""), "adp")
    assert scope != board_read_scope(coverage.replace(center, f"{center}_2"), "adp")
    assert scope != board_read_scope(coverage.replace("en_CA", "fr_CA"), "adp")
    assert scope != board_read_scope(
        coverage.replace(tenant, "11111111-2222-4333-8444-555555555555"), "adp"
    )


def _continuation(url: str, label: str = "Published role") -> SourceContinuationTarget:
    return SourceContinuationTarget(
        url=url,
        label=label,
        label_source="visible",
        source_url="https://publisher.example/careers",
        structural_context="main",
        relations=("publisher-content", "substantive-label"),
    )


def _scan_row(
    database: Database,
    source_id: str,
    day: int,
    *,
    query_label: str = "title-filter",
    later_by: timedelta = timedelta(),
    status: str = "incomplete",
    read_outcome: SourceReadOutcome = SourceReadOutcome.CRAWL_CONTINUATION,
    exhausted_outcome: SourceReadOutcome | None = None,
) -> str:
    """Create a scan `day` days out; scans are unique per source, day, and query label."""
    scan_id = new_id()
    started = utc_now() + timedelta(days=day)
    scanned_on = started.date()
    now = started + later_by
    with database.session() as session:
        session.add(
            SourceScan(
                id=scan_id,
                source_id=source_id,
                scanned_on=scanned_on,
                query_label=query_label,
                status=status,
                read_outcome=read_outcome,
                raw_count=0,
                read_count=0,
                skip_reasons={},
                result_count=0,
                new_count=0,
                notes="",
                exhausted_outcome=exhausted_outcome,
                scan_started_at=now,
                scan_completed_at=now,
                duration_ms=0,
            )
        )
    return scan_id


def _seed_continuation(
    database: Database, workspace_id: str, scan_id: str, rows: list[dict[str, object]]
) -> str:
    """Build a continuation with hand-placed frontier rows for one scan."""
    with database.session() as session:
        return seed_scan_continuation(session, workspace_id, scan_id, rows, origin="source.scan")


def _rows(database: Database, continuation_id: str) -> list[SourceScanFrontierItem]:
    with database.session() as session:
        rows = list(
            session.scalars(
                select(SourceScanFrontierItem)
                .where(SourceScanFrontierItem.continuation_id == continuation_id)
                .order_by(SourceScanFrontierItem.normalized_url)
            )
        )
        for row in rows:
            session.expunge(row)
        return rows


def test_follow_up_discoveries_do_not_extend_the_automatic_chain(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    seeds = (
        _continuation("https://jobs.example/globex/seed-1"),
        _continuation("https://jobs.example/globex/seed-2"),
    )

    class ChainBoard:
        continue_calls = 0

        def listings(
            self,
            _source_name: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=[],
                complete=False,
                raw_count=0,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=seeds,
            )

        def continue_listings(
            self,
            _employer: str,
            targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            self.continue_calls += 1
            urls = tuple(target.url for target in targets)
            discovered = _continuation("https://jobs.example/globex/found-1")
            return SourceRead(
                listings=[],
                complete=False,
                raw_count=len(targets),
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=(discovered,),
                attempted_target_urls=urls,
            )

    board = ChainBoard()
    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=board,
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    worker = Worker(
        durable_database,
        worker_id="follow-up-budget-worker",
        handlers=default_handlers(durable_database, fetcher=board),
    )
    for _ in range(5):
        if not worker.run_once():
            break

    with durable_database.session() as session:
        rows = session.scalars(select(SourceScanFrontierItem)).all()
        continue_tasks = session.scalars(
            select(BackgroundTask).where(BackgroundTask.task_type == "source.continue")
        ).all()

    by_url = {row.url: row for row in rows}
    assert by_url["https://jobs.example/globex/seed-1"].status == "completed"
    assert by_url["https://jobs.example/globex/seed-2"].status == "completed"
    found = by_url["https://jobs.example/globex/found-1"]
    assert found.status == "pending"
    assert found.automatic_eligible is False
    assert found.deferred_reason == DISCOVERED_DURING_FOLLOW_UP
    assert len(continue_tasks) == 1
    assert board.continue_calls == 1


def test_a_follow_up_rediscovering_a_posting_due_for_rescreen_keeps_it_automatic(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    stale_url = "https://jobs.example/globex/read-long-ago"
    unknown_url = "https://jobs.example/globex/never-read"
    _seed_continuation(
        durable_database,
        workspace_id,
        _scan_row(durable_database, source_id, day=-10),
        [
            {
                "url": stale_url,
                "status": "completed",
                "deferred_reason": POSTING_DETAIL_COMPLETED,
                "completed_at": utc_now() - RESCREEN_COMPLETED_AFTER - timedelta(days=1),
            }
        ],
    )
    seed = _continuation("https://jobs.example/globex/seed-1")

    class RediscoveringBoard:
        def listings(
            self,
            _source_name: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=[],
                complete=False,
                raw_count=0,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=(seed,),
            )

        def continue_listings(
            self,
            _employer: str,
            targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=[],
                complete=False,
                raw_count=len(targets),
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=(_continuation(stale_url), _continuation(unknown_url)),
                attempted_target_urls=tuple(target.url for target in targets),
            )

    board = RediscoveringBoard()
    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=board,
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)
    Worker(
        durable_database,
        worker_id="rescreen-follow-up-worker",
        handlers=default_handlers(durable_database, fetcher=board),
    ).run_once()

    with durable_database.session() as session:
        appended = {
            row.url: row
            for row in session.scalars(
                select(SourceScanFrontierItem).where(
                    SourceScanFrontierItem.origin == "source.continue"
                )
            )
        }
    assert appended[stale_url].automatic_eligible is True
    assert appended[stale_url].deferred_reason != DISCOVERED_DURING_FOLLOW_UP
    assert appended[unknown_url].automatic_eligible is False
    assert appended[unknown_url].deferred_reason == DISCOVERED_DURING_FOLLOW_UP


def test_a_same_day_rescan_reopens_its_own_reads_made_before_the_parameters_changed(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    url = "https://jobs.example/globex/posting-1"
    scan_id = _scan_row(durable_database, source_id, day=0)
    continuation_id = _seed_continuation(
        durable_database,
        workspace_id,
        scan_id,
        [
            {
                "url": url,
                "status": "completed",
                "deferred_reason": POSTING_DETAIL_COMPLETED,
                "completed_at": utc_now() - timedelta(hours=2),
                "automatic_eligible": False,
            }
        ],
    )
    with durable_database.session() as session:
        session.add(
            JobSearchParameterSet(
                workspace_id=workspace_id,
                version=1,
                content_sha256="d" * 64,
                effective_at=utc_now(),
            )
        )

    with durable_database.session() as session:
        result = SourceContinuationCommands(durable_database).replace_frontier_in_session(
            session,
            workspace_id=workspace_id,
            source_scan_id=scan_id,
            targets=(_continuation(url),),
            correlation_id=None,
            carry_forward=True,
            handoff_scopes=(),
        )
    assert result.continuation_id == continuation_id
    [row] = _rows(durable_database, continuation_id)
    assert (row.status, row.completed_at, row.automatic_eligible) == ("pending", None, True)
    assert result.pending_count == 1
    with durable_database.session() as session:
        continue_tasks = session.scalars(
            select(BackgroundTask).where(BackgroundTask.task_type == "source.continue")
        ).all()
    assert len(continue_tasks) == 1


def test_a_page_cursor_discovered_during_follow_up_stays_automatic_and_is_read(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    seed_url = "https://jobs.example/globex/seed"
    cursor_url = "https://jobs.example/globex/page-2"

    class CursorBoard:
        def __init__(self) -> None:
            self.cursor_reads: list[tuple[str, ...]] = []

        def listings(
            self,
            _source_name: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=[],
                complete=False,
                raw_count=0,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=(_continuation(seed_url),),
            )

        def continue_listings(
            self,
            _employer: str,
            targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            urls = tuple(target.url for target in targets)
            if urls == (seed_url,):
                cursor = replace(_continuation(cursor_url), deferred_reason="workday_page_pending")
                return SourceRead(
                    listings=[],
                    complete=False,
                    raw_count=1,
                    outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                    deferred_targets=(cursor,),
                    attempted_target_urls=urls,
                )
            self.cursor_reads.append(urls)
            return SourceRead(
                listings=[],
                complete=True,
                raw_count=len(targets),
                outcome=SourceReadOutcome.ATS_COMPLETE,
                attempted_target_urls=urls,
            )

    board = CursorBoard()
    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=board,
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    worker = Worker(
        durable_database,
        worker_id="cursor-follow-up-worker",
        handlers=default_handlers(durable_database, fetcher=board),
    )
    for _ in range(5):
        if not worker.run_once():
            break

    assert board.cursor_reads == [(cursor_url,)]
    with durable_database.session() as session:
        cursor_row = session.scalar(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.url == cursor_url)
        )
    assert cursor_row is not None
    assert cursor_row.automatic_eligible is True
    assert cursor_row.status == "completed"


_PAGED_ROOT = "https://careers.smartrecruiters.com/Acme"
_PAGE_TWO = _PAGED_ROOT + "?search=&page=2"
_REGISTERED_JOB = "https://jobs.smartrecruiters.com/Acme/744000012345678-clinical-analyst"
_PATH_WORD_JOB = "https://careers.smartrecruiters.com/Acme/jobs/clinical-data-manager"


def _paged_publisher_follow_up(
    database: Database,
) -> tuple[dict[str, SourceScanFrontierItem], list[str]]:
    """Read a publisher board's second page in a continuation, one page per task."""
    workspace_id = _workspace(database)
    _source(database, workspace_id, "Acme", _PAGED_ROOT)
    body = "Responsibilities include clinical data analysis and study documentation. " * 8
    page_two = (
        '<div class="openings-body js-openings"><section class="openings-section opening">'
        '<ul class="opening-jobs js-openings-load">'
        + "".join(
            f'<li class="opening-job job"><a href="{url}">{title}</a></li>'
            for url, title in (
                (_REGISTERED_JOB, "Clinical Analyst"),
                (_REGISTERED_JOB.replace("678-", "679-"), "Clinical Analyst II"),
                (_PATH_WORD_JOB, "Clinical Data Manager"),
                (_PATH_WORD_JOB + "-ii", "Clinical Data Manager II"),
            )
        )
        + "</ul></section></div>"
    )
    calls: list[str] = []

    def handler(url: str) -> httpx.Response:
        calls.append(url)
        if url == _PAGE_TWO:
            text = page_two
        else:
            title = "Clinical Analyst" if url == _REGISTERED_JOB else "Clinical Data Manager"
            record = {"@type": "JobPosting", "title": title, "url": url, "description": body}
            text = f'<script type="application/ld+json">{json.dumps(record)}</script>'
        return httpx.Response(200, text=text, request=httpx.Request("GET", url))

    publishers = PublisherSourceFetcher(
        client=policy_client(handler), limits=PublisherReadLimits(max_pages=1)
    )
    identity = board_identity(_PAGED_ROOT)
    assert identity is not None
    scopes = (("jobs.smartrecruiters.com", "board", f"{identity[0]}:{identity[1]}"),)

    class PagedPublisherBoard:
        def listings(
            self,
            _source_name: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=[],
                complete=False,
                raw_count=0,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=(replace(_continuation(_PAGE_TWO), source_url=_PAGED_ROOT),),
                handoff_scopes=scopes,
            )

        def continue_listings(
            self,
            employer: str,
            targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return publishers.continue_listings(
                employer, targets, root_url=root_url, handoff_scopes=handoff_scopes
            )

    board = PagedPublisherBoard()
    SourceScanService(
        database,
        storage=runtime_storage(database),
        fetcher=board,
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)
    worker = Worker(
        database,
        worker_id="paged-publisher-worker",
        handlers=default_handlers(database, fetcher=board),
    )
    for _ in range(5):
        if not worker.run_once():
            break
    with database.session() as session:
        rows = {row.url: row for row in session.scalars(select(SourceScanFrontierItem))}
        for row in rows.values():
            session.expunge(row)
    return rows, calls


def test_a_registered_posting_found_mid_chain_stays_automatic_and_is_read(
    durable_database: Database,
) -> None:
    rows, calls = _paged_publisher_follow_up(durable_database)

    posting = rows[_REGISTERED_JOB]
    assert posting.automatic_eligible is True
    assert posting.status == "completed"
    assert calls.count(_REGISTERED_JOB) == 1


def test_a_path_word_posting_found_mid_chain_waits_for_a_manual_read(
    durable_database: Database,
) -> None:
    rows, calls = _paged_publisher_follow_up(durable_database)
    posting = rows[_PATH_WORD_JOB]
    assert (posting.status, posting.automatic_eligible) == ("pending", False)
    assert posting.deferred_reason == DISCOVERED_DURING_FOLLOW_UP
    assert _PATH_WORD_JOB not in calls


def test_a_manual_row_stays_non_automatic_after_a_retryable_failure(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    url = "https://jobs.example/globex/manual"
    manual_target = replace(
        _continuation(url), automatic_eligible=False, deferred_reason="low_priority_navigation"
    )

    class ManualBoard:
        def listings(
            self,
            _source_name: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=[],
                complete=False,
                raw_count=0,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=(manual_target,),
            )

        def continue_listings(
            self,
            _employer: str,
            targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            urls = tuple(target.url for target in targets)
            retried = replace(
                targets[0], automatic_eligible=True, deferred_reason="retryable_error"
            )
            return SourceRead(
                listings=[],
                complete=False,
                raw_count=1,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=(retried,),
                attempted_target_urls=urls,
            )

    board = ManualBoard()
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=board,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.scan(workspace_id)
    with durable_database.session() as session:
        scan_id = session.scalar(select(SourceScan.id))
    assert scan_id is not None
    service.request_continuation(workspace_id, scan_id)

    worker = Worker(
        durable_database,
        worker_id="manual-retry-worker",
        handlers=default_handlers(durable_database, fetcher=board),
    )
    assert worker.run_once()

    with durable_database.session() as session:
        row = session.scalar(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.url == url)
        )
    assert row is not None
    assert row.automatic_eligible is False
    assert row.status == "pending"


def test_follow_up_does_not_refetch_a_posting_completed_in_an_earlier_continuation(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    url = "https://jobs.example/globex/posting-1"
    scan_a = _scan_row(durable_database, source_id, day=1)
    _seed_continuation(
        durable_database,
        workspace_id,
        scan_a,
        [{"url": url, "status": "completed", "deferred_reason": POSTING_DETAIL_COMPLETED}],
    )

    scan_b = _scan_row(durable_database, source_id, day=2)
    commands = SourceContinuationCommands(durable_database)
    with durable_database.session() as session:
        result = commands.replace_frontier_in_session(
            session,
            workspace_id=workspace_id,
            source_scan_id=scan_b,
            targets=(_continuation(url),),
            correlation_id=None,
            carry_forward=True,
            handoff_scopes=(),
        )
    assert result.continuation_id is not None
    assert result.pending_count == 0
    rows = _rows(durable_database, result.continuation_id)
    assert [(row.url, row.status) for row in rows] == [(url, "completed")]


def test_a_rebuilt_frontier_replaces_the_stored_handoff_scopes(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    scan = _scan_row(durable_database, source_id, day=1)
    commands = SourceContinuationCommands(durable_database)
    for scopes in ((("old.example", "host", ""),), (("www.jobs.example", "host", ""),)):
        with durable_database.session() as session:
            result = commands.replace_frontier_in_session(
                session,
                workspace_id=workspace_id,
                source_scan_id=scan,
                targets=(_continuation("https://jobs.example/globex/posting-1"),),
                correlation_id=None,
                carry_forward=False,
                handoff_scopes=scopes,
            )

    with durable_database.session() as session:
        stored = session.get(SourceScanContinuation, result.continuation_id)
        assert stored is not None
        assert stored.handoff_scopes == [["www.jobs.example", "host", ""]]


@pytest.mark.parametrize("next_day", [False, True])
def test_a_fresh_board_checkpoint_replaces_prior_detail_work_without_duplicate_reads(
    durable_database: Database, next_day: bool
) -> None:
    root = "https://globex.applytojob.com/apply"
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", root)
    first_scan = _scan_row(durable_database, source_id, day=1)
    commands = SourceContinuationCommands(durable_database)
    for index in range(2):
        scan = _scan_row(durable_database, source_id, day=2) if index and next_day else first_scan
        target = replace(
            _continuation(f"{root}?detail_checkpoint=slice-{index}"),
            automatic_eligible=False,
            detail_checkpoint=SourceDetailCheckpoint(
                platform="jazzhr", board_url=root, pending_ids=(f"role-{index}",)
            ),
        )
        with durable_database.session() as session:
            result = commands.replace_frontier_in_session(
                session,
                workspace_id=workspace_id,
                source_scan_id=scan,
                targets=(target,),
                correlation_id=None,
                carry_forward=True,
                handoff_scopes=(),
            )
    assert result.unresolved_count == 1
    with durable_database.session() as session:
        rows = session.scalars(
            select(SourceScanFrontierItem).where(
                SourceScanFrontierItem.continuation_id == result.continuation_id
            )
        ).all()
        assert [frontier_target(row).detail_checkpoint for row in rows] == [
            target.detail_checkpoint
        ]


@pytest.mark.parametrize("next_day", [False, True])
def test_fresh_checkpoint_replacement_preserves_url_only_and_unrelated_frontier_work(
    durable_database: Database, next_day: bool
) -> None:
    root = "https://globex.applytojob.com/apply"
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", root)
    first_scan = _scan_row(durable_database, source_id, day=1)
    legacy = replace(_continuation(f"{root}/legacy-role"), automatic_eligible=False)
    old = replace(
        _continuation(f"{root}?detail_checkpoint=old"),
        automatic_eligible=False,
        detail_checkpoint=SourceDetailCheckpoint(
            platform="jazzhr", board_url=root, pending_ids=("old-role",)
        ),
    )
    unrelated_root = "https://acme.applytojob.com/apply"
    unrelated = replace(
        _continuation(f"{unrelated_root}?detail_checkpoint=other"),
        automatic_eligible=False,
        detail_checkpoint=SourceDetailCheckpoint(
            platform="jazzhr", board_url=unrelated_root, pending_ids=("other-role",)
        ),
    )
    independent = replace(
        _continuation("https://acme.example/jobs/retained-role"),
        automatic_eligible=False,
        deferred_reason="alignerr_search_detail",
        detail_checkpoint=SourceDetailCheckpoint(
            platform="alignerr",
            board_url="https://acme.example/jobs/retained-role",
            pending_ids=("retained-id",),
        ),
    )
    initial = _persist_frontier(
        durable_database, workspace_id, first_scan, (legacy, old, unrelated, independent)
    )
    assert initial.continuation_id is not None
    with durable_database.session() as session:
        stored_null = session.scalar(
            select(SourceScanFrontierItem).where(
                SourceScanFrontierItem.continuation_id == initial.continuation_id,
                SourceScanFrontierItem.url == legacy.url,
                SourceScanFrontierItem.detail_checkpoint.is_not(None),
            )
        )
        assert stored_null is not None
        assert stored_null.detail_checkpoint is None
    fresh = replace(
        old,
        url=f"{root}?detail_checkpoint=fresh",
        detail_checkpoint=SourceDetailCheckpoint(
            platform="jazzhr", board_url=root, pending_ids=("fresh-role",)
        ),
    )
    next_scan = _scan_row(durable_database, source_id, day=2) if next_day else first_scan

    result = _persist_frontier(durable_database, workspace_id, next_scan, (fresh,))

    assert result.continuation_id is not None
    assert result.unresolved_count == 4
    rows = {row.url: row for row in _rows(durable_database, result.continuation_id)}
    assert set(rows) == {legacy.url, fresh.url, unrelated.url, independent.url}
    assert fresh.detail_checkpoint is not None
    assert unrelated.detail_checkpoint is not None
    assert independent.detail_checkpoint is not None
    assert rows[legacy.url].detail_checkpoint is None
    assert rows[fresh.url].detail_checkpoint == fresh.detail_checkpoint.as_json()
    assert rows[unrelated.url].detail_checkpoint == unrelated.detail_checkpoint.as_json()
    assert rows[independent.url].detail_checkpoint == independent.detail_checkpoint.as_json()


@pytest.mark.parametrize(
    "invalid_checkpoint",
    [{}, {"platform": "jazzhr", "board_url": "not-an-address"}],
)
def test_fresh_checkpoint_replacement_refuses_a_malformed_non_null_checkpoint(
    durable_database: Database, invalid_checkpoint: dict[str, str]
) -> None:
    root = "https://globex.applytojob.com/apply"
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", root)
    scan_id = _scan_row(durable_database, source_id, day=1)
    target = replace(
        _continuation(f"{root}?detail_checkpoint=old"),
        automatic_eligible=False,
        detail_checkpoint=SourceDetailCheckpoint(
            platform="jazzhr", board_url=root, pending_ids=("old-role",)
        ),
    )
    initial = _persist_frontier(durable_database, workspace_id, scan_id, (target,))
    assert initial.continuation_id is not None
    with durable_database.session() as session:
        stored = session.scalar(
            select(SourceScanFrontierItem).where(
                SourceScanFrontierItem.continuation_id == initial.continuation_id
            )
        )
        assert stored is not None
        stored.detail_checkpoint = invalid_checkpoint

    with pytest.raises(ValueError):
        _persist_frontier(durable_database, workspace_id, scan_id, (target,))

    assert (
        _rows(durable_database, initial.continuation_id)[0].detail_checkpoint == invalid_checkpoint
    )


@pytest.mark.parametrize(
    "reason", ["classification_ambiguous", "classification_error_or_challenge"]
)
@pytest.mark.parametrize("automatic", [True, False])
@pytest.mark.parametrize("attempts", [0, 1, 2, MAX_AUTOMATIC_ATTEMPTS])
def test_fresh_detail_declarations_refresh_classification_holds_without_resetting_history(
    durable_database: Database, reason: str, automatic: bool, attempts: int
) -> None:
    root = "https://acme.fxrecruiter.com/jobs"
    url = f"{root}/role-1"
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Acme", root)
    scan_id = _scan_row(durable_database, source_id, day=1)
    error = f"Publisher read deferred: {reason}." if attempts else ""
    eligible = automatic and attempts < MAX_AUTOMATIC_ATTEMPTS
    continuation_id = _seed_continuation(
        durable_database,
        workspace_id,
        scan_id,
        [
            {
                "url": f"{url}/",
                "normalized_url": url,
                "label": "Earlier role label",
                "automatic_eligible": False,
                "deferred_reason": reason,
                "correlation_id": "retained-correlation",
                "attempt_count": attempts,
                "last_error": error,
            },
            {
                "url": f"{root}/unrelated",
                "label": "Unrelated held role",
                "automatic_eligible": False,
                "deferred_reason": reason,
            },
        ],
    )
    before = {row.normalized_url: row for row in _rows(durable_database, continuation_id)}
    checkpoint = SourceDetailCheckpoint(
        platform="fxrecruiter",
        board_url=url,
        pending_ids=("published-role-1",),
        failed_attempts=() if automatic else (("published-role-1", 1),),
    )
    fresh = SourceContinuationTarget(
        url=url,
        label="Declared publisher role",
        label_source="title",
        source_url=root,
        structural_context="list",
        relations=("same-origin", "publisher-content", "substantive-label"),
        depth=2,
        automatic_eligible=automatic,
        deferred_reason="fxrecruiter_search_detail",
        detail_checkpoint=checkpoint,
    )

    result = _persist_frontier(durable_database, workspace_id, scan_id, (fresh,))

    assert result.continuation_id == continuation_id
    assert (result.pending_count, result.unresolved_count, result.inserted_count) == (2, 2, 0)
    after = {row.normalized_url: row for row in _rows(durable_database, continuation_id)}
    row = after[url]
    assert row.id == before[url].id
    assert (row.url, row.label, row.label_source) == (fresh.url, fresh.label, fresh.label_source)
    assert (row.source_url, row.structural_context, row.relations, row.depth) == (
        fresh.source_url,
        fresh.structural_context,
        list(fresh.relations),
        fresh.depth,
    )
    assert row.automatic_eligible is eligible
    assert row.deferred_reason == fresh.deferred_reason
    assert row.detail_checkpoint == checkpoint.as_json()
    assert (row.status, row.attempt_count, row.last_error) == ("pending", attempts, error)
    assert (row.origin, row.correlation_id, row.created_at) == (
        before[url].origin,
        before[url].correlation_id,
        before[url].created_at,
    )
    unrelated = after[f"{root}/unrelated"]
    assert unrelated.id == before[unrelated.url].id
    assert unrelated.label == "Unrelated held role"
    assert unrelated.deferred_reason == reason
    assert unrelated.automatic_eligible is False
    assert unrelated.detail_checkpoint is None
    with durable_database.session() as session:
        continuation = session.get(SourceScanContinuation, continuation_id)
        assert continuation is not None
        assert continuation.generation == 2
        tasks = list(
            session.scalars(
                select(BackgroundTask).where(BackgroundTask.task_type == "source.continue")
            )
        )
        assert len(tasks) == int(eligible)
        if eligible:
            assert tasks[0].payload == {
                "continuation_id": continuation_id,
                "generation": 2,
                "include_policy_deferred": False,
                "no_progress_count": 0,
            }
            assert tasks[0].idempotency_key == (
                f"source-continuation:{continuation_id}:2:auto:{row.id}:{attempts}"
            )


@pytest.mark.parametrize(
    "case",
    [
        "ordinary_html",
        "attempted",
        "mismatched_classification",
        "invalid_deferral_text",
        "transport_error",
        "failed_url_only",
        "failed_detail",
        "completed",
        "other_manual_hold",
        "malformed_checkpoint",
    ],
)
def test_fresh_detail_declarations_preserve_existing_recovery_boundaries(
    durable_database: Database, case: str
) -> None:
    root = "https://acme.fxrecruiter.com/jobs"
    url = f"{root}/role-1"
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Acme", root)
    scan_id = _scan_row(durable_database, source_id, day=1)
    held: dict[str, object] = {
        "url": url,
        "label": "Retained role label",
        "automatic_eligible": False,
        "deferred_reason": "classification_ambiguous",
    }
    if case == "attempted":
        held.update(attempt_count=1, last_error="Retained read failure")
    elif case == "mismatched_classification":
        held.update(
            attempt_count=1,
            last_error="Publisher read deferred: classification_error_or_challenge.",
        )
    elif case == "invalid_deferral_text":
        held.update(
            attempt_count=1,
            last_error="Publisher read deferred: classification_ambiguous",
        )
    elif case == "transport_error":
        held.update(attempt_count=1, last_error="Read failed: HTTP 503 Service Unavailable")
    elif case == "failed_url_only":
        held.update(status="failed", attempt_count=2, last_error="Retained read failure")
    elif case == "failed_detail":
        held["detail_checkpoint"] = SourceDetailCheckpoint(
            platform="fxrecruiter",
            board_url=url,
            pending_ids=("retained-publisher-id",),
            failed_attempts=(("retained-publisher-id", 1),),
        ).as_json()
    elif case == "completed":
        held.update(status="completed", deferred_reason=POSTING_DETAIL_COMPLETED)
    elif case == "other_manual_hold":
        held["deferred_reason"] = "posting_identity_not_established"
    elif case == "malformed_checkpoint":
        held["detail_checkpoint"] = {}
    continuation_id = _seed_continuation(durable_database, workspace_id, scan_id, [held])
    [before] = _rows(durable_database, continuation_id)
    fresh = replace(
        _continuation(url, "Fresh role label"),
        source_url=root,
        deferred_reason="" if case == "ordinary_html" else "fxrecruiter_search_detail",
        detail_checkpoint=(
            None
            if case == "ordinary_html"
            else SourceDetailCheckpoint(
                platform="fxrecruiter", board_url=url, pending_ids=("fresh-publisher-id",)
            )
        ),
    )

    result = _persist_frontier(durable_database, workspace_id, scan_id, (fresh,))

    assert result.continuation_id == continuation_id
    assert result.inserted_count == 0
    [row] = _rows(durable_database, continuation_id)
    assert row.id == before.id
    assert (row.url, row.label, row.label_source, row.source_url) == (
        before.url,
        before.label,
        before.label_source,
        before.source_url,
    )
    assert (row.structural_context, row.relations, row.depth) == (
        before.structural_context,
        before.relations,
        before.depth,
    )
    assert row.automatic_eligible is False
    assert row.deferred_reason == before.deferred_reason
    assert row.detail_checkpoint == before.detail_checkpoint
    if case == "failed_url_only":
        assert (row.status, row.attempt_count, row.last_error) == ("pending", 0, "")
        assert row.origin == "source.scan.carry_forward"
    else:
        assert (row.status, row.attempt_count, row.last_error) == (
            before.status,
            before.attempt_count,
            before.last_error,
        )
    with durable_database.session() as session:
        assert (
            list(
                session.scalars(
                    select(BackgroundTask).where(BackgroundTask.task_type == "source.continue")
                )
            )
            == []
        )


def _requeued_urls(
    database: Database, workspace_id: str, source_id: str, url: str, completed_at: datetime
) -> list[str]:
    """Seed one completed posting read, then return what a next-day scan re-queues."""
    scan_a = _scan_row(database, source_id, day=1)
    _seed_continuation(
        database,
        workspace_id,
        scan_a,
        [
            {
                "url": url,
                "status": "completed",
                "deferred_reason": POSTING_DETAIL_COMPLETED,
                "completed_at": completed_at,
            }
        ],
    )
    scan_b = _scan_row(database, source_id, day=2)
    with database.session() as session:
        result = SourceContinuationCommands(database).replace_frontier_in_session(
            session,
            workspace_id=workspace_id,
            source_scan_id=scan_b,
            targets=(_continuation(url),),
            correlation_id=None,
            carry_forward=True,
            handoff_scopes=(),
        )
    assert result.continuation_id is not None
    return [row.url for row in _rows(database, result.continuation_id) if row.status == "pending"]


def test_a_posting_read_longer_ago_than_the_rescreen_age_is_read_again(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    url = "https://jobs.example/globex/posting-1"
    read_at = utc_now() - RESCREEN_COMPLETED_AFTER - timedelta(days=1)

    assert _requeued_urls(durable_database, workspace_id, source_id, url, read_at) == [url]


def test_a_posting_read_before_the_search_parameters_changed_is_read_again(
    durable_database: Database,
) -> None:
    read_at = utc_now() - timedelta(hours=2)
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    url = "https://jobs.example/globex/posting-1"
    with durable_database.session() as session:
        session.add(
            JobSearchParameterSet(
                workspace_id=workspace_id,
                version=1,
                content_sha256="c" * 64,
                effective_at=utc_now(),
            )
        )

    assert _requeued_urls(durable_database, workspace_id, source_id, url, read_at) == [url]


def test_a_recent_posting_read_under_the_current_parameters_is_not_read_again(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    url = "https://jobs.example/globex/posting-1"

    assert _requeued_urls(durable_database, workspace_id, source_id, url, utc_now()) == []


@pytest.mark.parametrize("fresh", [False, True])
def test_a_queued_fresh_scan_rereads_recent_details(
    durable_database: Database, fresh: bool
) -> None:
    workspace_id = _workspace(durable_database)
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    url = "https://jobs.example/globex/recent"
    _seed_continuation(
        durable_database,
        workspace_id,
        _scan_row(durable_database, source_id, day=-1),
        [
            {
                "url": url,
                "status": "completed",
                "deferred_reason": POSTING_DETAIL_COMPLETED,
                "completed_at": utc_now(),
            }
        ],
    )

    queue = TaskQueueService(durable_database)
    root = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [source_id], "fresh_details": fresh},
        idempotency_key="fresh-details",
    )
    board = _DrainingBoard("https://jobs.example/globex/root", url, (url,))
    worker = Worker(
        durable_database,
        worker_id="fresh-details",
        handlers=default_handlers(durable_database, fetcher=board),
    )
    for _ in range(4):
        if not worker.run_once():
            break
    else:
        raise AssertionError("the source batch did not drain")

    assert board.continue_calls == int(fresh)
    with durable_database.session() as session:
        member = session.scalars(
            select(SourceScanMember).where(SourceScanMember.root_task_id == str(root.id))
        ).one()
        assert member.state == "completed"
        saved_root = session.get(BackgroundTask, str(root.id))
        assert saved_root is not None and saved_root.status == "completed"
        pending = session.scalar(
            select(PendingDiscovery).where(
                PendingDiscovery.workspace_id == workspace_id, PendingDiscovery.discovery_url == url
            )
        )
        assert (pending is not None) is fresh


@pytest.mark.parametrize("fresh", [False, True])
def test_a_fresh_follow_up_rereads_old_family_details_without_repeating_its_own_successes(
    durable_database: Database, fresh: bool
) -> None:
    workspace_id = _workspace(durable_database)
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    known_url = "https://jobs.example/globex/recent"
    seed_url = "https://jobs.example/globex/first"
    _seed_continuation(
        durable_database,
        workspace_id,
        _scan_row(durable_database, source_id, day=-1),
        [
            {
                "url": known_url,
                "status": "completed",
                "deferred_reason": POSTING_DETAIL_COMPLETED,
                "completed_at": utc_now(),
            }
        ],
    )

    class RediscoveringBoard(_DrainingBoard):
        def __init__(self) -> None:
            super().__init__("https://jobs.example/globex/root", seed_url, (seed_url,))
            self.targets: list[str] = []

        def continue_listings(
            self,
            employer: str,
            targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            self.targets.extend(target.url for target in targets)
            return SourceRead(
                listings=[_posting(target.url) for target in targets],
                complete=False,
                raw_count=len(targets),
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                attempted_target_urls=tuple(target.url for target in targets),
                deferred_targets=(
                    _continuation(known_url if targets[0].url == seed_url else seed_url),
                ),
            )

    root = TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [source_id], "fresh_details": fresh},
        idempotency_key="fresh-follow-up",
    )
    board = RediscoveringBoard()
    worker = Worker(
        durable_database,
        worker_id="fresh-follow-up",
        handlers=default_handlers(durable_database, fetcher=board),
    )
    for _ in range(5):
        if not worker.run_once():
            break
    else:
        raise AssertionError("the source batch did not drain")

    assert board.targets == ([seed_url, known_url] if fresh else [seed_url])
    with durable_database.session() as session:
        member = session.scalars(
            select(SourceScanMember).where(SourceScanMember.root_task_id == str(root.id))
        ).one()
        rows = list(
            session.scalars(
                select(SourceScanFrontierItem).where(
                    SourceScanFrontierItem.continuation_id == member.continuation_id,
                    SourceScanFrontierItem.status == "completed",
                )
            )
        )
        assert {row.url for row in rows} >= set(board.targets)
        assert len([row for row in rows if row.url == seed_url]) == 1


@pytest.mark.parametrize("protection", ["person_dismissed", "retired_tracked"])
def test_a_fresh_continuation_keeps_protected_postings_and_their_content_read_time(
    durable_database: Database, protection: str
) -> None:
    from clearscout.application.discovery.source_reading import SourceReadOptions
    from clearscout.infrastructure.persistence.models import JobPosting, PendingPostingContent
    from tests.support.scenarios import (
        dismiss_pending_discovery,
        pending_posting_content,
        tracked_board_posting,
    )

    workspace_id = _workspace(durable_database)
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    board_url = "https://jobs.example/globex"
    source_id = _source(durable_database, workspace_id, "Globex", board_url)
    url = f"{board_url}/protected"
    _stage_known_rows(durable_database, workspace_id, source_id, (url,))
    job_id = None
    if protection == "retired_tracked":
        job_id = tracked_board_posting(
            durable_database, workspace_id, source_id, title="Data Platform Lead", url=url
        )
    observed_at = utc_now() - timedelta(days=1)
    with durable_database.session() as session:
        pending = session.scalars(select(PendingDiscovery)).one()
        pending_id = pending.id
        evidence = pending_posting_content(workspace_id, pending_id, url, fetched_at=observed_at)
        session.add(evidence)
        session.flush()
        evidence_id = evidence.id
        pending.current_content_id = evidence_id
        pending.content_status = PostingContentAccessStatus.READABLE
        if job_id is not None:
            job = session.get(JobPosting, job_id)
            assert job is not None
            job.archived_at = utc_now()
    if protection == "person_dismissed":
        dismiss_pending_discovery(durable_database, workspace_id, pending_id)

    class ProtectedBoard(_DrainingBoard):
        def __init__(self) -> None:
            super().__init__(f"{board_url}/root", url, (url,))
            self.allowed: list[bool] = []
            self.detail_reader = BoardApiFetcher(
                client=cast(PoliteHttpClient, _PublisherClient({}))
            )

        def listings(
            self,
            employer: str,
            url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            self.allowed.append(cast(SourceReadOptions, options).allows_posting(self.continued))
            return super().listings(employer, url, options=options)

        def continue_listings(
            self,
            employer: str,
            targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return self.detail_reader.continue_listings(
                employer,
                targets,
                root_url=root_url,
                handoff_scopes=handoff_scopes,
                options=cast(SourceReadOptions, options),
            )

    root = TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [source_id], "fresh_details": True},
        idempotency_key="protected-fresh",
    )
    board = ProtectedBoard()
    worker = Worker(
        durable_database,
        worker_id="protected-fresh",
        handlers=default_handlers(durable_database, fetcher=board),
    )
    assert worker.run_once()
    assert worker.run_once()
    assert not worker.run_once()

    assert board.allowed == [False]
    with durable_database.session() as session:
        member = session.scalars(
            select(SourceScanMember).where(SourceScanMember.root_task_id == str(root.id))
        ).one()
        frontier = session.scalars(
            select(SourceScanFrontierItem).where(
                SourceScanFrontierItem.continuation_id == member.continuation_id,
                SourceScanFrontierItem.url == url,
            )
        ).one()
        assert frontier.status == "completed"
        assert frontier.deferred_reason == "protected_posting"
        assert frontier.attempt_count == 0
        assert frontier.completed_at is None
        retained = session.get(PendingDiscovery, pending_id)
        assert retained is not None
        assert retained.current_content_id == evidence_id
        retained_evidence = session.get(PendingPostingContent, evidence_id)
        assert retained_evidence is not None and retained_evidence.fetched_at == observed_at
        assert (
            session.scalar(
                select(PendingPostingContent.id).where(
                    PendingPostingContent.pending_discovery_id == pending_id,
                    PendingPostingContent.id != evidence_id,
                )
            )
            is None
        )
        if job_id is not None:
            job = session.get(JobPosting, job_id)
            assert job is not None and job.archived_at is not None
        else:
            disposition = session.scalars(
                select(PendingDiscoveryDispositionEvent).where(
                    PendingDiscoveryDispositionEvent.pending_discovery_id == pending_id
                )
            ).one()
            assert disposition.action == "dismiss"
            assert disposition.origin == "test"


@pytest.mark.parametrize("fresh", [True, False], ids=["fresh", "ordinary"])
def test_a_fresh_continuation_reads_a_held_rows_content_again_and_stages_its_posted_date(
    durable_database: Database, fresh: bool
) -> None:
    """Content held from before a fresh scan is read again where its continuation stages it."""
    from clearscout.application.discovery.posting_text import publisher_posted_date
    from clearscout.domain import PostingContentExtractionMethod, PostingIdentityStatus
    from clearscout.domain.staged_facts import PostedDateGrade, StagedListingFacts

    workspace_id = _workspace(durable_database)
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    board_url = "https://jobs.example/globex"
    source_id = _source(durable_database, workspace_id, "Globex", board_url)
    listed, held = f"{board_url}/listed", f"{board_url}/held"
    _stage_known_rows(durable_database, workspace_id, source_id, (held,))

    class DatingResolver:
        def __init__(self, fetched_at: datetime, dated: bool) -> None:
            self.fetched_at, self.dated = fetched_at, dated
            self.urls: list[str] = []

        def resolve(self, url: str, **_kwargs: Any) -> ResolvedPostingContent:
            self.urls.append(url)
            return ResolvedPostingContent(
                requested_url=url,
                final_url=url,
                content_status=PostingContentAccessStatus.READABLE,
                identity_status=PostingIdentityStatus.VERIFIED,
                posting_status=PostingStatus.ACTIVE,
                extraction_method=PostingContentExtractionMethod.STATIC_HTML,
                fetched_at=self.fetched_at,
                content=b"Data Platform Lead\nBuild the clinical data platform.",
                http_status=200,
                publisher_markup_present=True,
                identity_evidence={"publisher_declaration": True},
                note="Verified publisher body.",
                publisher_posted_date=publisher_posted_date(
                    "2026-08-20",
                    basis="greenhouse.first_published",
                    grade=PostedDateGrade.FIRST_POSTED,
                )
                if self.dated
                else None,
            )

    storage = runtime_storage(durable_database)
    with durable_database.session() as session:
        held_id = session.scalars(
            select(PendingDiscovery.id).where(PendingDiscovery.discovery_url == held)
        ).one()
    PostingContentResolutionService(
        durable_database,
        DatingResolver(utc_now() - timedelta(days=1), dated=False),
        storage=storage,
    ).resolve_automatic(workspace_id, [held_id], origin="source.scan")

    resolver = DatingResolver(utc_now(), dated=True)
    content = PostingContentResolutionService(durable_database, resolver, storage=storage)
    board = _DrainingBoard(listed, held, (held,))
    scan_handler = SourceScanHandler(
        durable_database,
        storage=storage,
        fetcher=board,
        posting_fetcher=UnexpectedPostingFetcher(),
        content_resolution=content,
    )
    worker = Worker(
        durable_database,
        worker_id="held-continuation",
        handlers={
            "source.scan": scan_handler,
            "source.continue": SourceContinuationHandler(
                durable_database,
                board,
                scan_handler.service.sweep,
                lambda workspace_id, task_id, outcomes: scan_handler.service.record_task_outcomes(
                    workspace_id, task_id, outcomes, origin="source.continue"
                ),
                scan_handler.service.begin_recording_pass,
            ),
        },
    )
    TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [source_id], "fresh_details": fresh},
        idempotency_key="held-continuation",
    )
    for _ in range(4):
        if not worker.run_once():
            break
    else:
        raise AssertionError("the source batch did not drain")

    assert board.continue_calls == 1
    assert resolver.urls.count(held) == int(fresh)
    with durable_database.session() as session:
        row = session.get_one(PendingDiscovery, held_id)
        posted = StagedListingFacts.from_json(row.staged_facts).posted_date
    assert (posted.earliest.normalized_value if posted else None) == (
        "2026-08-20" if fresh else None
    )


@pytest.mark.parametrize("mode", ["reuse", "fresh", "failed_fresh"])
def test_only_a_successful_fresh_detail_read_replaces_the_retained_body_and_read_time(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    import json

    from clearscout.application.discovery.content_resolution import CurrentPostingContentReader
    from clearscout.application.discovery.source_reading import SourceReadOptions
    from clearscout.application.workspaces import service as workspace_service
    from clearscout.application.workspaces import versioned
    from clearscout.domain import (
        PostingContentExtractionMethod,
        PostingIdentityStatus,
        PostingStatus,
    )
    from clearscout.infrastructure.persistence.models import PendingPostingContent
    from tests.support.doubles import UnexpectedContentResolver

    read_at = utc_now() - timedelta(days=1)
    with monkeypatch.context() as past:
        past.setattr(workspace_service, "utc_now", lambda: read_at - timedelta(days=1))
        past.setattr(versioned, "utc_now", lambda: read_at - timedelta(days=1))
        workspace_id = _workspace(durable_database)
        state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    board_url = "https://jobs.example/globex"
    source_id = _source(durable_database, workspace_id, "Globex", board_url)
    url = f"{board_url}/retained"
    _stage_known_rows(durable_database, workspace_id, source_id, (url,))
    with durable_database.session() as session:
        pending_id = session.scalars(select(PendingDiscovery.id)).one()
    storage = runtime_storage(durable_database)
    content = PostingContentResolutionService(
        durable_database, UnexpectedContentResolver(), storage=storage
    )
    content.store_prefetched_result(
        workspace_id,
        pending_id,
        ResolvedPostingContent(
            requested_url=url,
            final_url=url,
            content_status=PostingContentAccessStatus.READABLE,
            identity_status=PostingIdentityStatus.VERIFIED,
            posting_status=PostingStatus.ACTIVE,
            extraction_method=PostingContentExtractionMethod.STATIC_STRUCTURED,
            fetched_at=read_at,
            content=b"Own the data platform.",
            http_status=200,
            publisher_markup_present=True,
        ),
        None,
        origin="test",
    )
    with durable_database.session() as session:
        retained_pending = session.get(PendingDiscovery, pending_id)
        assert retained_pending is not None
        retained_id = retained_pending.current_content_id
    _seed_continuation(
        durable_database,
        workspace_id,
        _scan_row(durable_database, source_id, day=-1),
        [
            {
                "url": url,
                "status": "completed",
                "deferred_reason": POSTING_DETAIL_COMPLETED,
                "completed_at": read_at,
            }
        ],
    )
    changed_body = (
        "Own the data platform and its governance. Build reliable data pipelines, "
        "maintain data quality checks, document operational requirements, and work "
        "with product teams to deliver accurate reporting. Lead platform planning "
        "and incident reviews, improve observability, and mentor data engineers. "
        "This role supports a distributed team and welcomes remote work. "
        "Review changes with engineering partners, maintain platform documentation, "
        "and communicate delivery progress and operational risks to stakeholders."
    )
    markup = (
        '<script type="application/ld+json">'
        + json.dumps(
            {
                "@context": "https://schema.org",
                "@type": "JobPosting",
                "title": "Data Platform Lead",
                "description": changed_body,
                "url": url,
                "hiringOrganization": {"@type": "Organization", "name": "Globex"},
                "jobLocationType": "TELECOMMUTE",
            }
        )
        + "</script>"
    )

    class DetailClient(_PublisherClient):
        def __init__(self) -> None:
            super().__init__({url: markup})
            self.reads: list[str] = []

        def get(self, url: str, **kwargs: Any) -> httpx.Response:
            self.reads.append(url)
            if mode == "failed_fresh":
                raise httpx.ConnectError(
                    "Synthetic offline posting", request=httpx.Request("GET", url)
                )
            return super().get(url, **kwargs)

    client = DetailClient()

    class DeferredBoard(_DrainingBoard):
        def __init__(self) -> None:
            super().__init__(f"{board_url}/root", url, (url,))
            self.detail_reader = BoardApiFetcher(client=cast(PoliteHttpClient, client))
            self.last_read: SourceRead | None = None

        def listings(
            self,
            employer: str,
            address: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=[],
                complete=False,
                raw_count=1,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=(_continuation(url),),
                attempted_target_urls=(),
            )

        def continue_listings(
            self,
            employer: str,
            targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            self.last_read = self.detail_reader.continue_listings(
                employer,
                targets,
                root_url=root_url,
                handoff_scopes=handoff_scopes,
                options=cast(SourceReadOptions, options),
            )
            return self.last_read

    root = TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [source_id], "fresh_details": mode != "reuse"},
        idempotency_key="fresh-content",
    )
    board = DeferredBoard()
    scan_handler = SourceScanHandler(
        durable_database,
        storage=storage,
        fetcher=board,
        posting_fetcher=UnexpectedPostingFetcher(),
        content_resolution=content,
    )
    worker = Worker(
        durable_database,
        worker_id="fresh-content",
        handlers={
            "source.scan": scan_handler,
            "source.continue": SourceContinuationHandler(
                durable_database, board, scan_handler.service.sweep
            ),
        },
    )
    for _ in range(6):
        if not worker.run_once():
            break
    else:
        raise AssertionError("the source batch did not drain")

    current = CurrentPostingContentReader(durable_database, storage=storage).read(
        workspace_id, pending_id
    )
    assert current is not None
    with durable_database.session() as session:
        evidence = session.get(PendingPostingContent, current.evidence_id)
        assert evidence is not None
        if mode == "fresh":
            assert client.reads == [url]
            assert board.last_read is not None
            assert [item.published_description for item in board.last_read.listings] == [
                changed_body
            ]
            assert current.body == changed_body.encode()
            assert evidence.id != retained_id
            assert evidence.fetched_at > read_at
        else:
            assert client.reads == ([] if mode == "reuse" else [url] * MAX_AUTOMATIC_ATTEMPTS)
            assert current.body == b"Own the data platform."
            assert evidence.id == retained_id
            assert evidence.fetched_at == read_at
        saved_root = session.get(BackgroundTask, str(root.id))
        assert saved_root is not None and saved_root.status == "completed"


def test_a_new_scan_rereads_a_workday_page_cursor_completed_earlier(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    url = "https://jobs.example/globex/page-3"
    scan_a = _scan_row(durable_database, source_id, day=1)
    _seed_continuation(
        durable_database,
        workspace_id,
        scan_a,
        [{"url": url, "status": "completed", "deferred_reason": ""}],
    )

    scan_b = _scan_row(durable_database, source_id, day=2)
    commands = SourceContinuationCommands(durable_database)
    cursor_target = replace(_continuation(url), deferred_reason="workday_page_pending")
    with durable_database.session() as session:
        result = commands.replace_frontier_in_session(
            session,
            workspace_id=workspace_id,
            source_scan_id=scan_b,
            targets=(cursor_target,),
            correlation_id=None,
            carry_forward=True,
            handoff_scopes=(),
        )
    assert result.continuation_id is not None
    [row] = _rows(durable_database, result.continuation_id)
    assert row.url == url
    assert row.status == "pending"
    assert row.automatic_eligible is True


def test_carry_forward_clones_pending_rows_and_skips_a_completed_row(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    done_url = "https://jobs.example/globex/done"
    todo_url = "https://jobs.example/globex/todo"
    scan_a = _scan_row(durable_database, source_id, day=1)
    _seed_continuation(
        durable_database,
        workspace_id,
        scan_a,
        [
            {"url": done_url, "status": "completed"},
            {"url": todo_url, "status": "pending"},
        ],
    )

    scan_b = _scan_row(durable_database, source_id, day=2)
    commands = SourceContinuationCommands(durable_database)
    with durable_database.session() as session:
        result = commands.replace_frontier_in_session(
            session,
            workspace_id=workspace_id,
            source_scan_id=scan_b,
            targets=(),
            correlation_id=None,
            carry_forward=True,
            handoff_scopes=(),
        )
    assert result.continuation_id is not None
    rows = _rows(durable_database, result.continuation_id)
    assert [row.url for row in rows] == [todo_url]
    assert rows[0].status == "pending"


_HELD_URL = "https://jobs.example/globex/culture"


def _held_row(reason: str = "posting_identity_not_established") -> dict[str, object]:
    """A non-automatic pending row whose posting identity the crawl never established."""
    return {"url": _HELD_URL, "automatic_eligible": False, "deferred_reason": reason}


def _persist_frontier(
    database: Database,
    workspace_id: str,
    scan_id: str,
    targets: tuple[SourceContinuationTarget, ...] = (),
) -> FrontierPersistResult:
    with database.session() as session:
        return SourceContinuationCommands(database).replace_frontier_in_session(
            session,
            workspace_id=workspace_id,
            source_scan_id=scan_id,
            targets=targets,
            correlation_id=None,
            carry_forward=True,
            handoff_scopes=(),
        )


_HELD_REASONS = [
    "posting_identity_not_established",
    "shape_probe_pending",
    "low_priority_navigation",
]


@pytest.mark.parametrize("reason", _HELD_REASONS)
def test_a_held_row_the_rescan_no_longer_declares_is_dropped_so_the_source_settles(
    durable_database: Database, reason: str
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    scan_a = _scan_row(durable_database, source_id, day=1)
    _seed_continuation(durable_database, workspace_id, scan_a, [_held_row(reason)])

    scan_b = _scan_row(durable_database, source_id, day=2)
    result = _persist_frontier(durable_database, workspace_id, scan_b)

    assert result.continuation_id is None
    assert result.unresolved_count == 0


@pytest.mark.parametrize("reason", _HELD_REASONS)
def test_a_same_day_rescan_that_no_longer_declares_a_held_row_drops_it(
    durable_database: Database, reason: str
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    scan_id = _scan_row(durable_database, source_id, day=1)
    held = replace(_continuation(_HELD_URL), automatic_eligible=False, deferred_reason=reason)
    first = _persist_frontier(durable_database, workspace_id, scan_id, (held,))

    rescan = _persist_frontier(durable_database, workspace_id, scan_id)

    assert rescan.continuation_id == first.continuation_id
    assert rescan.unresolved_count == 0
    assert first.continuation_id is not None
    assert _rows(durable_database, first.continuation_id) == []


@pytest.mark.parametrize(
    ("status", "read_outcome"),
    [
        ("failed", SourceReadOutcome.CRAWL_CONTINUATION),
        ("incomplete", SourceReadOutcome.ACCESS_BLOCKED),
    ],
)
def test_a_failed_or_blocked_rescan_keeps_the_held_rows_it_could_not_redeclare(
    durable_database: Database, status: str, read_outcome: SourceReadOutcome
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    scan_a = _scan_row(durable_database, source_id, day=1)
    _seed_continuation(durable_database, workspace_id, scan_a, [_held_row()])

    scan_b = _scan_row(durable_database, source_id, day=2, status=status, read_outcome=read_outcome)
    result = _persist_frontier(durable_database, workspace_id, scan_b)

    assert result.continuation_id is not None
    [row] = _rows(durable_database, result.continuation_id)
    assert (row.url, row.status) == (_HELD_URL, "pending")


def test_a_rescan_carries_nothing_past_a_newer_scan_that_left_no_continuation(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    scan_a = _scan_row(durable_database, source_id, day=1)
    _seed_continuation(durable_database, workspace_id, scan_a, [_held_row()])
    _scan_row(
        durable_database,
        source_id,
        day=2,
        status="completed",
        read_outcome=SourceReadOutcome.ATS_COMPLETE,
    )

    scan_c = _scan_row(durable_database, source_id, day=3, status="failed")
    result = _persist_frontier(durable_database, workspace_id, scan_c)

    assert result.continuation_id is None
    assert result.unresolved_count == 0


@pytest.mark.parametrize("reason", _HELD_REASONS)
def test_a_held_row_the_rescan_declares_again_is_carried(
    durable_database: Database, reason: str
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    scan_a = _scan_row(durable_database, source_id, day=1)
    _seed_continuation(
        durable_database,
        workspace_id,
        scan_a,
        [{**_held_row(reason), "label": "Label from the earlier scan"}],
    )

    scan_b = _scan_row(durable_database, source_id, day=2)
    held = replace(_continuation(_HELD_URL), automatic_eligible=False, deferred_reason=reason)
    result = _persist_frontier(durable_database, workspace_id, scan_b, (held,))

    assert result.continuation_id is not None
    [row] = _rows(durable_database, result.continuation_id)
    assert (row.url, row.status) == (_HELD_URL, "pending")
    assert row.label == "Label from the earlier scan"


def test_a_row_failed_in_an_earlier_scan_is_retried_once_in_a_new_scan(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    url = "https://jobs.example/globex/flaky"
    scan_a = _scan_row(durable_database, source_id, day=1)
    _seed_continuation(
        durable_database,
        workspace_id,
        scan_a,
        [{"url": url, "status": "failed", "attempt_count": 3, "origin": "source.scan"}],
    )

    scan_b = _scan_row(durable_database, source_id, day=2)
    commands = SourceContinuationCommands(durable_database)
    with durable_database.session() as session:
        result_b = commands.replace_frontier_in_session(
            session,
            workspace_id=workspace_id,
            source_scan_id=scan_b,
            targets=(),
            correlation_id=None,
            carry_forward=True,
            handoff_scopes=(),
        )
    assert result_b.continuation_id is not None
    [row] = _rows(durable_database, result_b.continuation_id)
    assert row.url == url
    assert row.status == "pending"
    assert row.attempt_count == 0

    with durable_database.session() as session:
        stored = session.get(SourceScanFrontierItem, row.id)
        assert stored is not None
        stored.status = "failed"

    scan_c = _scan_row(durable_database, source_id, day=3)
    with durable_database.session() as session:
        result_c = commands.replace_frontier_in_session(
            session,
            workspace_id=workspace_id,
            source_scan_id=scan_c,
            targets=(),
            correlation_id=None,
            carry_forward=True,
            handoff_scopes=(),
        )
    assert result_c.continuation_id is None


# A later day's read that lists the spent page again retries it; otherwise it keeps its failure.
@pytest.mark.parametrize("carry", ["later_day", "same_day", "delisted"])
@pytest.mark.parametrize("read_today", [True, False])
def test_a_carried_failed_listed_posting_settles_only_when_todays_read_completes_it(
    durable_database: Database, read_today: bool, carry: str
) -> None:
    workspace_id = _workspace(durable_database)
    root = "https://jobs.jobvite.com/acme/jobs"
    identity = "oAb12cDe"
    url = f"https://jobs.jobvite.com/acme/job/{identity}"
    source_id = _source(durable_database, workspace_id, "Acme", root)
    scan_a = _scan_row(durable_database, source_id, day=1)
    _seed_continuation(
        durable_database,
        workspace_id,
        scan_a,
        [
            {
                "url": url,
                "source_url": root,
                "status": "failed",
                "attempt_count": 3,
                "automatic_eligible": False,
                "deferred_reason": "jobvite_posting_pending",
                "detail_checkpoint": SourceDetailCheckpoint(
                    platform="jobvite",
                    board_url=url,
                    pending_ids=(identity,),
                    listing_ids=(identity,),
                    listing_urls=((identity, url),),
                    failed_attempts=((identity, 3),),
                ).as_json(),
            }
        ],
    )

    scan_b = (
        _scan_row(durable_database, source_id, day=2)
        if carry != "same_day"
        else _scan_row(durable_database, source_id, day=1, query_label="second-filter")
    )
    other = "oZz98yXw"
    listed_ids = (other,) if carry == "delisted" else (identity,)
    listed = tuple((each, f"https://jobs.jobvite.com/acme/job/{each}") for each in listed_ids)
    with durable_database.session() as session:
        result = SourceContinuationCommands(durable_database).replace_frontier_in_session(
            session,
            workspace_id=workspace_id,
            source_scan_id=scan_b,
            targets=(),
            correlation_id=None,
            carry_forward=True,
            handoff_scopes=(),
            publisher_listing_receipts=(
                SourceDetailCheckpoint(
                    platform="listed:jobvite",
                    board_url=root,
                    listing_ids=listed_ids,
                    listing_urls=listed,
                    completed=listed if read_today else (),
                ),
            ),
        )

    assert result.continuation_id is not None
    [posting] = [row for row in _rows(durable_database, result.continuation_id) if row.url == url]
    retried = carry == "later_day"
    settled = read_today and carry != "delisted"
    assert posting.status == ("completed" if settled else "pending" if retried else "failed")
    assert result.unresolved_count == (0 if settled else 1)
    if not settled:
        assert posting.attempt_count == (0 if retried else 3)
        assert posting.automatic_eligible is retried


@pytest.mark.parametrize(
    ("platform", "root", "posting"),
    [
        ("jobvite", "https://jobs.jobvite.com/acme/jobs", "https://jobs.jobvite.com/acme/job/{}"),
        (
            "workday",
            "https://acme.wd1.myworkdayjobs.com/External",
            "https://acme.wd1.myworkdayjobs.com/External/job/Remote/Analyst_R{}",
        ),
    ],
)
def test_a_same_day_rescan_that_defers_a_posting_keeps_the_mornings_completed_reads(
    durable_database: Database, platform: str, root: str, posting: str
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Acme", root)
    scan_id = _scan_row(durable_database, source_id, day=0)
    read, deferred = posting.format("1001"), posting.format("1002")
    continuation_id = _seed_continuation(
        durable_database,
        workspace_id,
        scan_id,
        [
            {
                "url": read,
                "source_url": root,
                "status": "completed",
                "deferred_reason": POSTING_DETAIL_COMPLETED,
                "detail_checkpoint": SourceDetailCheckpoint(
                    platform=platform,
                    board_url=read,
                    listing_ids=("1001",),
                    listing_urls=(("1001", read),),
                ).as_json(),
            }
        ],
    )

    with durable_database.session() as session:
        SourceContinuationCommands(durable_database).replace_frontier_in_session(
            session,
            workspace_id=workspace_id,
            source_scan_id=scan_id,
            targets=(
                replace(
                    _continuation(deferred),
                    source_url=root,
                    deferred_reason=f"{platform}_posting_pending",
                    detail_checkpoint=SourceDetailCheckpoint(
                        platform=platform,
                        board_url=deferred,
                        pending_ids=("1002",),
                        listing_ids=("1002",),
                        listing_urls=(("1002", deferred),),
                        # A certified Workday total supersedes every checkpoint of its board.
                        listing_total=1,
                    ),
                ),
            ),
            correlation_id=None,
            carry_forward=True,
            handoff_scopes=(),
        )

    kept = {row.url: row.status for row in _rows(durable_database, continuation_id)}
    assert kept == {read: "completed", deferred: "pending"}


# Only a verified listing that omits the failed detail drops it; any other read keeps it owed.
@pytest.mark.parametrize("today", ["delisted", "listed", "partial", "unstable"])
def test_a_carried_failed_search_detail_lapses_when_a_verified_listing_drops_it(
    durable_database: Database, today: str
) -> None:
    workspace_id = _workspace(durable_database)
    root = "https://careers.acme.example/careers"
    source_id = _source(durable_database, workspace_id, "Acme", root)
    postings = {each: f"https://careers.acme.example/careers/job/{each}" for each in "ABC"}
    failed = postings["C"]
    _seed_continuation(
        durable_database,
        workspace_id,
        _scan_row(durable_database, source_id, day=1),
        [
            {
                "url": failed,
                "source_url": root,
                "automatic_eligible": False,
                "attempt_count": 1,
                "deferred_reason": "eightfold_search_detail",
                "detail_checkpoint": SourceDetailCheckpoint(
                    platform="eightfold",
                    board_url=failed,
                    pending_ids=("C",),
                    listing_ids=("C",),
                    listing_urls=(("C", failed),),
                    failed_attempts=(("C", 1),),
                ).as_json(),
            }
        ],
    )

    listed = tuple(postings.items()) if today == "listed" else tuple(postings.items())[:2]
    with durable_database.session() as session:
        result = SourceContinuationCommands(durable_database).replace_frontier_in_session(
            session,
            workspace_id=workspace_id,
            source_scan_id=_scan_row(durable_database, source_id, day=2),
            targets=(),
            correlation_id=None,
            carry_forward=True,
            handoff_scopes=(),
            publisher_listing_receipts=(
                SourceDetailCheckpoint(
                    platform="search:eightfold_search_page",
                    board_url=root,
                    listing_ids=tuple(item for item, _url in listed),
                    listing_urls=listed,
                    completed=listed[:2],
                    listing_total=3 if today == "partial" else len(listed),
                    listing_unstable=today == "unstable",
                    page_fingerprints=(hashlib.sha256(b"page-1").hexdigest(),),
                ),
            ),
        )

    carried = (
        [row for row in _rows(durable_database, result.continuation_id) if row.url == failed]
        if result.continuation_id is not None
        else []
    )
    if today == "delisted":
        assert carried == []
        assert result.unresolved_count == 0
        return
    [detail] = carried
    assert (detail.status, detail.automatic_eligible, detail.attempt_count) == ("pending", False, 1)
    assert result.unresolved_count == 1


def test_a_row_carried_while_pending_keeps_its_retry_after_a_later_failure(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    url = "https://jobs.example/globex/unread"
    scan_a = _scan_row(durable_database, source_id, day=1)
    _seed_continuation(durable_database, workspace_id, scan_a, [{"url": url}])
    commands = SourceContinuationCommands(durable_database)

    def rescan(day: int) -> str | None:
        scan_id = _scan_row(durable_database, source_id, day=day)
        with durable_database.session() as session:
            return commands.replace_frontier_in_session(
                session,
                workspace_id=workspace_id,
                source_scan_id=scan_id,
                targets=(),
                correlation_id=None,
                carry_forward=True,
                handoff_scopes=(),
            ).continuation_id

    def fail_every_row(continuation_id: str) -> None:
        with durable_database.session() as session:
            for row in session.scalars(
                select(SourceScanFrontierItem).where(
                    SourceScanFrontierItem.continuation_id == continuation_id
                )
            ):
                row.status = "failed"
                row.attempt_count = 3
        for task in TaskQueueService(durable_database).list_tasks(workspace_id):
            if task.task_type == "source.continue" and task.status in {"queued", "running"}:
                TaskQueueService(durable_database).request_cancellation(str(task.id))

    continuation_b = rescan(day=2)
    assert continuation_b is not None
    [carried] = _rows(durable_database, continuation_b)
    assert carried.status == "pending"
    fail_every_row(continuation_b)

    continuation_c = rescan(day=3)
    assert continuation_c is not None
    [retried] = _rows(durable_database, continuation_c)
    assert retried.url == url
    assert retried.status == "pending"
    assert retried.attempt_count == 0
    fail_every_row(continuation_c)

    assert rescan(day=4) is None


def test_follow_up_state_and_continuation_agree_on_same_day_guided_and_board_scans(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    board_scan = _scan_row(durable_database, source_id, day=0)
    guided_scan = _scan_row(
        durable_database,
        source_id,
        day=0,
        query_label="not-a-board-scan",
        later_by=timedelta(minutes=5),
    )
    _seed_continuation(
        durable_database, workspace_id, board_scan, [{"url": "https://jobs.example/globex/a"}]
    )
    guided_continuation = _seed_continuation(
        durable_database, workspace_id, guided_scan, [{"url": "https://jobs.example/globex/b"}]
    )

    history = SourceScanQueries(durable_database).list_scan_history(workspace_id)
    states = {item.id: item.follow_up_state for item in history.items}
    assert states == {board_scan: "superseded", guided_scan: "waiting"}

    commands = SourceContinuationCommands(durable_database)
    with pytest.raises(ValidationError, match="no longer its source's latest"):
        commands.request_continuation(workspace_id, board_scan)
    commands.request_continuation(workspace_id, guided_scan)
    with durable_database.session() as session:
        enqueued = {
            task.payload["continuation_id"]
            for task in session.scalars(
                select(BackgroundTask).where(BackgroundTask.task_type == "source.continue")
            )
        }
    assert enqueued == {guided_continuation}


def _posting(url: str) -> SourceListing:
    return SourceListing(employer="Globex", title="Data Platform Lead", url=url, location="Remote")


def _stage_known_rows(
    database: Database, workspace_id: str, source_id: str, urls: tuple[str, ...]
) -> None:
    """Leave the queue holding the rows an earlier scan of this source staged."""
    DiscoveryStaging(
        database,
        PostingRefreshService(
            database, storage=runtime_storage(database), fetcher=UnexpectedPostingFetcher()
        ),
    ).stage_listings(
        workspace_id,
        [_posting(url) for url in urls],
        (utc_now() - timedelta(days=1)).date(),
        reason="Board scan matched the title filter.",
        source_id=source_id,
    )


def _missed_once(database: Database, *urls: str) -> None:
    """Record an earlier multi-page miss of these rows and the completed check that read them."""
    with database.session() as session:
        _mark_checked_absent(
            session,
            session.scalars(
                select(PendingDiscovery).where(PendingDiscovery.discovery_url.in_(urls))
            ).all(),
        )


def _mark_checked_absent(session: Any, rows: Sequence[PendingDiscovery]) -> None:
    """Mark rows absent a day ago, with a completed refresh batch that read each one."""
    for row in rows:
        row.absent_since = utc_now() - timedelta(days=1)
    session.add(
        BackgroundTask(
            workspace_id=rows[0].workspace_id,
            task_type="discovery.refresh_batch",
            status="completed",
            payload={"pending_discovery_ids": [row.id for row in rows]},
            payload_sha256="0" * 64,
            idempotency_key=new_id(),
        )
    )


@pytest.mark.parametrize(("members", "protected_count"), [(39, 5), (21, 0)])
@pytest.mark.parametrize("interruption", [None, "cancel", "source_loss", "stale_generation"])
def test_detail_continuations_count_all_successful_bodies_once(
    durable_database: Database, members: int, protected_count: int, interruption: str | None
) -> None:
    workspace_id = _workspace(durable_database)
    root = "https://acme.applytojob.com/apply"
    source_id = _source(durable_database, workspace_id, "Acme", root)
    state_target_titles(durable_database, workspace_id, "Clinical Data Analyst")
    urls = [f"{root}/role{index:06d}/Clinical-Data-Analyst" for index in range(members)]
    protected_ids: list[str] = []
    if protected_count:
        _stage_known_rows(durable_database, workspace_id, source_id, tuple(urls[:protected_count]))
        with durable_database.session() as session:
            protected_ids = list(session.scalars(select(PendingDiscovery.id)))
        for pending_id in protected_ids:
            dismiss_pending_discovery(durable_database, workspace_id, pending_id)
    requested: list[str] = []
    window_reads = [0]
    interrupted: list[SourceRead] = []
    before_pending: set[str] = set()

    class ReadWindowJazz(BoardApiFetcher):
        def continue_listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            window_reads[0] = 0
            read = super().continue_listings(*args, **kwargs)
            if interruption is not None:
                assert read.detail_read_urls is not None and len(read.detail_read_urls) > 14
                interrupted.append(read)
                with durable_database.session() as session:
                    task = session.scalars(
                        select(BackgroundTask).where(
                            BackgroundTask.task_type == "source.continue",
                            BackgroundTask.status == "running",
                        )
                    ).one()
                    task_id = task.id
                    if interruption == "source_loss":
                        member = session.scalars(
                            select(SourceScanMember).where(SourceScanMember.source_id == source_id)
                        ).one()
                        member.active_attempt_count = 999
                    elif interruption == "stale_generation":
                        continuation = session.scalars(select(SourceScanContinuation)).one()
                        continuation.generation += 1
                if interruption == "cancel":
                    TaskQueueService(durable_database).request_cancellation(
                        task_id, workspace_id=workspace_id
                    )
            return read

        @staticmethod
        def deadline_elapsed() -> bool:
            return window_reads[0] >= 14

        def get_text(self, url: str, *, max_bytes: int = 0) -> str:
            if url == root:
                window_reads[0] = 0
                return (
                    "<main>"
                    + "".join(f'<a href="{address}">Clinical Data Analyst</a>' for address in urls)
                    + "</main>"
                )
            assert url in urls
            window_reads[0] += 1
            requested.append(url)
            return _jazzhr_posting_page("Clinical Data Analyst", "Springfield")

    board = ReadWindowJazz()
    worker = _enqueue_scan(durable_database, workspace_id, board)
    first_count: int | None = None
    for _ in range(12):
        if not worker.run_once():
            break
        with durable_database.session() as session:
            scan = session.scalar(select(SourceScan).where(SourceScan.source_id == source_id))
            if scan is not None and first_count is None:
                first_count = scan.read_count
                before_pending = set(session.scalars(select(PendingDiscovery.id)))
        if interrupted:
            break
    else:
        raise AssertionError("The controlled detail slices did not drain.")

    assert first_count == 14
    if interruption is not None:
        assert len(interrupted) == 1
        assert requested == urls[protected_count : protected_count + 28]
        with durable_database.session() as session:
            scan = session.scalars(
                select(SourceScan).where(SourceScan.source_id == source_id)
            ).one()
            assert scan.read_count == 14
            assert not scan.complete
            assert set(session.scalars(select(PendingDiscovery.id))) == before_pending
            if interruption == "cancel":
                task = session.scalars(
                    select(BackgroundTask).where(BackgroundTask.task_type == "source.continue")
                ).one()
                assert task.status == "cancelled"
        return
    assert requested == urls[protected_count:]
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan).where(SourceScan.source_id == source_id)).one()
        assert scan.raw_count == members
        assert scan.read_count == members - protected_count
        assert scan.detail_read_count == members - protected_count
        assert scan.complete is True
        assert scan.detail_coverage is not None
        assert len(scan.detail_coverage["posting_urls"]) == members
        retained = list(session.scalars(select(PendingDiscovery)))
        assert {row.discovery_url for row in retained} == set(urls)
        assert all(task.status == "completed" for task in session.scalars(select(BackgroundTask)))


def test_partial_detail_enumeration_retains_successful_body_counts_without_completion(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Acme", _ADP_BOARD)
    state_target_titles(durable_database, workspace_id, "Clinical Data Analyst")

    class PartialAdp(_CheckpointAdp):
        continued: SourceRead | None = None

        def continue_listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            self.rows = self.rows[2:]
            self.total = 5
            self.seconds = 0
            self.continued = super().continue_listings(*args, **kwargs)
            return self.continued

    board = PartialAdp(monkeypatch)
    board.rows = [
        _adp_requisition(itemID=f"920000000000{index}_1", requisitionTitle="Clinical Data Analyst")
        for index in range(4)
    ]
    worker = _enqueue_scan(durable_database, workspace_id, board)
    for _ in range(10):
        if not worker.run_once() or board.continued is not None:
            break
    else:
        raise AssertionError("The partial enumeration did not drain.")

    assert board.requested == [f"920000000000{index}_1" for index in range(4)]
    assert board.continued is not None
    assert board.continued.detail_read_urls is not None
    assert len(board.continued.detail_read_urls) == 4
    assert board.continued.deferred_targets == ()
    assert board.continued.detail_coverage is None
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan).where(SourceScan.source_id == source_id)).one()
        assert scan.read_count == scan.raw_count == 4
        assert scan.detail_read_count == 4
        assert not scan.complete
        assert scan.status == "incomplete"


@pytest.mark.parametrize(
    "invalid", ["forged_url", "foreign_board", "mixed_board", "historical_only", "metadata_only"]
)
def test_unsubstantiated_detail_evidence_does_not_change_the_scan_count(
    durable_database: Database, invalid: str
) -> None:
    workspace_id = _workspace(durable_database)
    root = "https://acme.applytojob.com/apply"
    other = "https://globex.applytojob.com/apply"
    source_id = _source(durable_database, workspace_id, "Acme", root)
    urls = tuple(f"{root}/role{index:06d}/Data-Analyst" for index in range(3))
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    checkpoint = SourceDetailCheckpoint(
        platform="jazzhr",
        board_url=root,
        completed=tuple((str(index), url) for index, url in enumerate(urls)),
        pending_ids=("unread",),
    )
    incoming: list[SourceRead] = []

    class EvidenceBoard:
        def listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            return SourceRead(
                listings=[source_listing("Data Analyst", urls[0], employer="Acme")],
                complete=False,
                raw_count=4,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=(
                    replace(
                        _continuation(root),
                        source_url=root,
                        deferred_reason="jazzhr_detail_rows_pending",
                        detail_checkpoint=checkpoint,
                    ),
                ),
            )

        def continue_listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            listings = [source_listing("Data Analyst", url, employer="Acme") for url in urls[1:]]
            coverage: SourceDetailCoverage | None = SourceDetailCoverage(
                platform="jazzhr",
                board_url=other if invalid == "foreign_board" else root,
                posting_urls=urls,
            )
            proof: tuple[str, ...] | None = urls
            deferred: tuple[SourceContinuationTarget, ...] = ()
            if invalid == "forged_url":
                proof = (*urls[:2], f"{other}/unknown/Data-Analyst")
            elif invalid == "mixed_board":
                deferred = (
                    replace(
                        _continuation(other),
                        source_url=other,
                        automatic_eligible=False,
                        deferred_reason="jazzhr_detail_rows_pending",
                        detail_checkpoint=SourceDetailCheckpoint(
                            platform="jazzhr", board_url=other, pending_ids=("foreign",)
                        ),
                    ),
                )
                coverage = None
            elif invalid == "historical_only":
                listings = []
                coverage = None
            elif invalid == "metadata_only":
                proof = None
            read = SourceRead(
                listings=listings,
                complete=coverage is not None,
                raw_count=4,
                outcome=(
                    SourceReadOutcome.CRAWL_CONTINUATION
                    if deferred
                    else SourceReadOutcome.ATS_COMPLETE
                    if coverage is not None
                    else SourceReadOutcome.PUBLISHER_LINKED_PAGES
                ),
                deferred_targets=deferred,
                detail_read_urls=proof,
                detail_coverage=coverage,
            )
            incoming.append(read)
            return read

    worker = _enqueue_scan(durable_database, workspace_id, EvidenceBoard())
    for _ in range(8):
        if not worker.run_once():
            break
    else:
        raise AssertionError("The scoped detail evidence did not drain.")

    assert len(incoming) == 1
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan).where(SourceScan.source_id == source_id)).one()
        assert scan.read_count == 1
        assert scan.raw_count == 4


@pytest.mark.parametrize("total", [4, 5])
def test_phenom_followups_count_current_listing_identities_without_claiming_bodies(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch, total: int
) -> None:
    monkeypatch.setattr(phenom, "PHENOM_PAGE", 2)
    monkeypatch.setattr(phenom, "PHENOM_MAX_PAGES", 1)
    workspace_id = _workspace(durable_database)
    board = _PhenomWindow(
        {
            offset: list(range(offset + 1, min(offset + 3, total + 1)))
            for offset in range(0, total, 2)
        },
        total=total,
    )
    source_id = _source(durable_database, workspace_id, "Acme", board.root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    worker = _enqueue_scan(durable_database, workspace_id, board)
    initial: tuple[int | None, int | None] | None = None
    for _ in range(12):
        if not worker.run_once():
            break
        with durable_database.session() as session:
            scan = session.scalar(select(SourceScan).where(SourceScan.source_id == source_id))
            if scan is not None and initial is None:
                initial = scan.raw_count, scan.read_count
    else:
        raise AssertionError("The bounded Phenom listing tasks did not drain.")

    assert initial == (2, 2)
    assert board.requests == list(range(0, total, 2))
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan).where(SourceScan.source_id == source_id)).one()
        assert scan.raw_count == scan.read_count == total
        assert scan.detail_read_count == 0
        assert scan.detail_coverage is None
        assert scan.complete
        checkpoints = [
            SourceDetailCheckpoint.from_json(row.detail_checkpoint)
            for row in session.scalars(select(SourceScanFrontierItem))
            if row.detail_checkpoint is not None
        ]
        assert checkpoints
        assert max(len(checkpoint.listing_ids) for checkpoint in checkpoints) == total
        assert all(checkpoint.completed == () for checkpoint in checkpoints)
        assert all(
            row.current_content_id is None for row in session.scalars(select(PendingDiscovery))
        )
        assert {row.discovery_url for row in session.scalars(select(PendingDiscovery))} == {
            board.root + f"job/{item}/data-analyst" for item in range(1, total + 1)
        }


@pytest.mark.parametrize(
    "scope", ["current", "old_root", "other_widget", "other_board", "body_completed"]
)
@pytest.mark.parametrize("branch_status", ["pending", "completed"])
def test_phenom_counts_all_current_branches_without_merging_their_page_receipts(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch, scope: str, branch_status: str
) -> None:
    monkeypatch.setattr(phenom, "PHENOM_PAGE", 2)
    monkeypatch.setattr(phenom, "PHENOM_MAX_PAGES", 1)
    workspace_id = _workspace(durable_database)
    board = _PhenomWindow({0: [1, 2], 2: [3, 4]}, total=4)
    source_id = _source(durable_database, workspace_id, "Acme", board.root)
    retained_url = board.root + "job/100/data-analyst"
    _stage_known_rows(durable_database, workspace_id, source_id, (retained_url,))
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    worker = _enqueue_scan(durable_database, workspace_id, board)
    assert worker.run_once()
    with durable_database.session() as session:
        cursor = session.scalars(select(SourceScanFrontierItem)).one()
        checkpoint = SourceDetailCheckpoint.from_json(cursor.detail_checkpoint)
        branch = replace(
            checkpoint,
            platform="search:phenom_page:OTHER" if scope == "other_widget" else checkpoint.platform,
            board_url=board.root + "other/" if scope == "other_board" else board.root,
            listing_ids=("REQ-1", "REQ-99"),
            listing_urls=(
                ("REQ-1", board.root + "job/1/data-analyst"),
                ("REQ-99", board.root + "job/99/data-analyst"),
            ),
            completed=(("REQ-99", board.root + "job/99/data-analyst"),)
            if scope == "body_completed"
            else (),
            listing_unstable=True,
        )
        branch_url = cursor.url + "&branch=retained"
        session.add(
            SourceScanFrontierItem(
                id=new_id(),
                continuation_id=cursor.continuation_id,
                url=branch_url,
                normalized_url=branch_url,
                label="Retained publisher page",
                label_source=cursor.label_source,
                source_url=branch.board_url,
                structural_context=cursor.structural_context,
                relations=cursor.relations,
                depth=cursor.depth,
                automatic_eligible=False,
                deferred_reason="phenom_page_pending",
                detail_checkpoint=branch.as_json(),
                status=branch_status,
                attempt_count=0,
                last_error="",
                correlation_id="earlier-root" if scope == "old_root" else cursor.correlation_id,
                origin=cursor.origin,
            )
        )
    assert worker.run_once()
    assert board.requests == [0, 2]
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert scan.raw_count == scan.read_count == (5 if scope == "current" else 4)
        assert scan.detail_read_count == 0
        assert scan.detail_coverage is None
        assert scan.complete == (
            branch_status == "completed" and scope in {"old_root", "other_board", "body_completed"}
        )
        receipts = [
            SourceDetailCheckpoint.from_json(row.detail_checkpoint)
            for row in session.scalars(select(SourceScanFrontierItem))
            if row.detail_checkpoint is not None
        ]
        assert any(
            set(receipt.listing_ids) == {"REQ-1", "REQ-2", "REQ-3", "REQ-4"} for receipt in receipts
        )
        assert branch in receipts
        branch_row = session.scalars(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.url == branch_url)
        ).one()
        assert not branch_row.automatic_eligible
        assert branch_row.status == branch_status
        if scope == "current":
            retained = session.scalars(
                select(PendingDiscovery).where(PendingDiscovery.discovery_url == retained_url)
            ).one()
            assert retained.withdrawn_at is None


def test_phenom_old_root_checkpoint_is_excluded_before_a_carried_page_read(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(phenom, "PHENOM_PAGE", 2)
    monkeypatch.setattr(phenom, "PHENOM_MAX_PAGES", 1)
    workspace_id = _workspace(durable_database)
    board = _PhenomWindow({0: [1, 2], 2: [3, 4]}, total=4)
    source_id = _source(durable_database, workspace_id, "Acme", board.root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    worker = _enqueue_scan(durable_database, workspace_id, board)
    assert worker.run_once()
    with durable_database.session() as session:
        row = session.scalars(select(SourceScanFrontierItem)).one()
        original = SourceDetailCheckpoint.from_json(row.detail_checkpoint)
        old = replace(
            original,
            listing_ids=("REQ-99",),
            listing_urls=(("REQ-99", board.root + "job/99/data-analyst"),),
        )
        row.detail_checkpoint = old.as_json()
        row.correlation_id = "earlier-root"
        row_id = row.id
    assert worker.run_once()
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan).where(SourceScan.source_id == source_id)).one()
        assert not scan.complete
        assert scan.raw_count == scan.read_count == 2
        stored = session.get(SourceScanFrontierItem, row_id)
        assert stored is not None
        assert stored.correlation_id == "earlier-root"
        assert SourceDetailCheckpoint.from_json(stored.detail_checkpoint) == old
        fresh = session.scalars(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.status == "pending")
        ).one()
        assert set(SourceDetailCheckpoint.from_json(fresh.detail_checkpoint).listing_ids) == {
            "REQ-3",
            "REQ-4",
        }
        assert all(
            pending.withdrawn_at is None for pending in session.scalars(select(PendingDiscovery))
        )
        assert {pending.discovery_url for pending in session.scalars(select(PendingDiscovery))} == {
            board.root + f"job/{item}/data-analyst" for item in (1, 2, 3, 4)
        }
    assert board.requests == [0, 2]


def test_phenom_terminal_membership_withdraws_only_current_absence(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(phenom, "PHENOM_PAGE", 2)
    monkeypatch.setattr(phenom, "PHENOM_MAX_PAGES", 1)
    workspace_id = _workspace(durable_database)
    board = _PhenomWindow({0: [1, 2], 2: [3, 4]}, total=4)
    source_id = _source(durable_database, workspace_id, "Acme", board.root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    gone = board.root + "job/99/data-analyst"
    _stage_known_rows(durable_database, workspace_id, source_id, (gone,))
    worker = _enqueue_scan(durable_database, workspace_id, board)
    assert worker.run_once()
    with durable_database.session() as session:
        continuation = session.scalars(select(SourceScanContinuation)).one()
        session.add(
            SourceScanFrontierItem(
                id=new_id(),
                continuation_id=continuation.id,
                url=gone,
                normalized_url=gone,
                source_url=board.root,
                label="Data Analyst",
                label_source="visible",
                structural_context="main",
                relations=["publisher-content"],
                status="completed",
                deferred_reason="posting_detail_completed",
                completed_at=utc_now(),
                correlation_id="earlier-root",
            )
        )
    _missed_once(durable_database, gone)
    assert worker.run_once()
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert scan.complete
        assert scan.raw_count == scan.read_count == 4
        assert scan.detail_coverage is None
        pending = {row.discovery_url: row for row in session.scalars(select(PendingDiscovery))}
        assert pending[gone].withdrawn_at is not None
        assert {url for url, row in pending.items() if row.withdrawn_at is None} == {
            board.root + f"job/{item}/data-analyst" for item in (1, 2, 3, 4)
        }
    assert board.requests == [0, 2]


@pytest.mark.parametrize("interruption", ["cancel", "source_loss", "stale_generation"])
def test_phenom_listing_count_updates_obey_the_source_write_guard(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    monkeypatch.setattr(phenom, "PHENOM_PAGE", 2)
    monkeypatch.setattr(phenom, "PHENOM_MAX_PAGES", 1)
    workspace_id = _workspace(durable_database)
    callbacks: list[str] = []

    class InterruptedWindow(_PhenomWindow):
        def continue_listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            read = super().continue_listings(*args, **kwargs)
            callbacks.append(interruption)
            with durable_database.session() as session:
                task = session.scalars(
                    select(BackgroundTask).where(
                        BackgroundTask.task_type == "source.continue",
                        BackgroundTask.status == "running",
                    )
                ).one()
                task_id = task.id
                if interruption == "source_loss":
                    member = session.scalars(select(SourceScanMember)).one()
                    member.active_attempt_count = 999
                elif interruption == "stale_generation":
                    continuation = session.scalars(select(SourceScanContinuation)).one()
                    continuation.generation += 1
            if interruption == "cancel":
                TaskQueueService(durable_database).request_cancellation(
                    task_id, workspace_id=workspace_id
                )
            return read

    board = InterruptedWindow({0: [1, 2], 2: [3, 4]}, total=4)
    _source(durable_database, workspace_id, "Acme", board.root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    worker = _enqueue_scan(durable_database, workspace_id, board)
    assert worker.run_once()
    with durable_database.session() as session:
        before = set(session.scalars(select(PendingDiscovery.id)))
    assert worker.run_once()
    assert callbacks == [interruption]
    assert board.requests == [0, 2]
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert scan.raw_count == scan.read_count == 2
        assert not scan.complete
        assert set(session.scalars(select(PendingDiscovery.id))) == before
        assert (
            max(
                len(SourceDetailCheckpoint.from_json(row.detail_checkpoint).listing_ids)
                for row in session.scalars(select(SourceScanFrontierItem))
                if row.detail_checkpoint is not None
            )
            == 2
        )
        if interruption == "cancel":
            task = session.scalars(
                select(BackgroundTask).where(BackgroundTask.task_type == "source.continue")
            ).one()
            assert task.status == "cancelled"


@pytest.mark.parametrize("new_member", [False, True])
def test_phenom_rescan_reconciles_current_membership_without_historical_counts(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch, new_member: bool
) -> None:
    monkeypatch.setattr(phenom, "PHENOM_PAGE", 2)
    monkeypatch.setattr(phenom, "PHENOM_MAX_PAGES", 1)
    workspace_id = _workspace(durable_database)
    board = _PhenomWindow({0: [1, 2], 2: [3, 4]}, total=4)
    _source(durable_database, workspace_id, "Acme", board.root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    worker = _enqueue_scan(durable_database, workspace_id, board)
    for _ in range(8):
        if not worker.run_once():
            break
    with durable_database.session() as session:
        assert session.scalars(select(SourceScan)).one().complete
    board.pages = {0: [3, 2], 2: [5]} if new_member else {0: [3, 2]}
    board.total = 3 if new_member else 2
    _missed_once(durable_database, *(board.root + f"job/{item}/data-analyst" for item in (1, 4)))
    TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id, task_type="source.scan", payload={}, idempotency_key=new_id()
    )
    for _ in range(8):
        if not worker.run_once():
            break
    else:
        raise AssertionError("The current listing rescan did not drain.")
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert scan.complete
        assert scan.raw_count == scan.read_count == board.total
        assert scan.detail_read_count == 0
        assert scan.detail_coverage is None
        pending = {row.discovery_url: row for row in session.scalars(select(PendingDiscovery))}
        expected = (2, 3, 5) if new_member else (2, 3)
        assert {url for url, row in pending.items() if row.withdrawn_at is None} == {
            board.root + f"job/{item}/data-analyst" for item in expected
        }
    assert board.requests == ([0, 2, 0, 2] if new_member else [0, 2, 0])


@pytest.mark.parametrize(
    "interruption",
    [
        None,
        "cancel",
        "source_loss",
        "stale_generation",
        "staging",
        "old_family",
        "old_root",
        "missing_checkpoint",
        "missing_root_receipt",
    ],
)
def test_native_listed_continuations_count_only_owned_committed_bodies_and_keep_failed_recovery(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    interruption: str | None,
) -> None:
    root = "https://jobs.jobvite.com/acme/jobs"
    identities = ("oAb12cDe", "oFg34hIj", "oKl56mNo", "oPq78rSt")
    urls = tuple(f"https://jobs.jobvite.com/acme/job/{identity}" for identity in identities)
    board_page = (
        '<table class="jv-job-list"><tbody>'
        + "".join(
            f'<tr><td class="jv-job-list-name"><a href="/acme/job/{identity}">'
            'Clinical Analyst</a></td><td class="jv-job-list-location">United States</td></tr>'
            for identity in identities
        )
        + "</tbody></table>"
    )
    failed = {urls[3]}
    redirected = urls[1] + "/clinical-analyst"
    interrupted = False

    def answer(url: str) -> httpx.Response:
        if url in failed:
            return httpx.Response(503, request=httpx.Request("GET", url))
        if url == urls[1]:
            return httpx.Response(
                302, headers={"Location": redirected}, request=httpx.Request("GET", url)
            )
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            text=board_page
            if url == root
            else declaring_page(
                {
                    "@context": "https://schema.org",
                    "@type": "JobPosting",
                    "title": "Clinical Analyst",
                    "description": "Analyze clinical data for patient programs.",
                    "jobLocation": {
                        "@type": "Place",
                        "address": {"@type": "PostalAddress", "addressCountry": "US"},
                    },
                }
            ),
        )

    defaults = SourceReadOptions()
    stopped = [False]

    class ListedPages(BoardApiFetcher):
        @staticmethod
        def deadline_elapsed() -> bool:
            return stopped[0]

        def get_text(self, url: str, *, max_bytes: int = 0) -> str:
            page = super().get_text(url, max_bytes=max_bytes)
            if url == urls[0]:
                stopped[0] = True
            return page

        def continue_listings(
            self,
            employer: str,
            targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str,
            handoff_scopes: tuple[tuple[str, str, str], ...],
            employer_is_authoritative: bool = False,
            options: SourceReadOptions = defaults,
        ) -> SourceRead:
            nonlocal interrupted
            stopped[0] = False
            read = super().continue_listings(
                employer,
                targets,
                root_url=root_url,
                handoff_scopes=handoff_scopes,
                employer_is_authoritative=employer_is_authoritative,
                options=replace(
                    options, protected_posting_urls=options.protected_posting_urls | {urls[2]}
                ),
            )
            if not interrupted and interruption in {"cancel", "source_loss", "stale_generation"}:
                interrupted = True
                with durable_database.session() as session:
                    task = session.scalars(
                        select(BackgroundTask).where(
                            BackgroundTask.task_type == "source.continue",
                            BackgroundTask.status == "running",
                        )
                    ).one()
                    task_id = task.id
                    if interruption == "source_loss":
                        member = session.scalars(
                            select(SourceScanMember).where(SourceScanMember.source_id == source_id)
                        ).one()
                        member.active_attempt_count = 999
                    elif interruption == "stale_generation":
                        continuation = session.scalars(select(SourceScanContinuation)).one()
                        continuation.generation += 1
                if interruption == "cancel":
                    TaskQueueService(durable_database).request_cancellation(
                        task_id, workspace_id=workspace_id
                    )
            return read

    fetcher = ListedPages(client=policy_client(answer))
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Acme", root)
    state_target_titles(durable_database, workspace_id, "Clinical Analyst")
    worker = _enqueue_scan(durable_database, workspace_id, fetcher)
    assert worker.run_once()
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan).where(SourceScan.source_id == source_id)).one()
        scan_id = scan.id
        assert (scan.raw_count, scan.read_count) == (4, 1), scan.notes
        pending = session.scalars(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.status == "pending")
        ).all()
        assert {row.url for row in pending} == set(urls[1:])
        row_changes = {
            "old_family": ("correlation_id", "unrelated-old-family"),
            "missing_checkpoint": ("detail_checkpoint", None),
        }
        if interruption in row_changes:
            attribute, value = row_changes[interruption]
            for row in pending:
                setattr(row, attribute, value)
        elif interruption == "old_root":
            source = session.get(Source, source_id)
            assert source is not None
            source.url = "https://jobs.jobvite.com/initech/jobs"
        elif interruption == "missing_root_receipt":
            receipt = session.scalars(
                select(SourceScanFrontierItem).where(SourceScanFrontierItem.url == root)
            ).one()
            receipt.detail_checkpoint = None
        before_pending = set(session.scalars(select(PendingDiscovery.id)))
    if interruption == "staging":
        handler = cast(SourceContinuationHandler, worker.handlers["source.continue"])

        def failed_staging(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("Synthetic retained artifact could not be staged")

        monkeypatch.setattr(handler.sweep, "stage_continuation", failed_staging)
    assert worker.run_once()
    with durable_database.session() as session:
        stored = session.get(SourceScan, scan_id)
        assert stored is not None
        if interruption is not None:
            assert (stored.raw_count, stored.read_count) == (4, 1)
            assert stored.detail_coverage is None
            if interruption in {"cancel", "source_loss", "stale_generation", "staging"}:
                assert set(session.scalars(select(PendingDiscovery.id))) == before_pending
            return
        assert (stored.raw_count, stored.read_count) == (4, 2)
        root_receipt = session.scalars(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.url == root)
        ).one()
        assert SourceDetailCheckpoint.from_json(root_receipt.detail_checkpoint).protected == (
            (identities[2], urls[2]),
        )
        owed = session.scalars(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.url == urls[3])
        ).one()
        assert owed.status == "pending" and owed.attempt_count == 1
        assert SourceDetailCheckpoint.from_json(owed.detail_checkpoint).pending_ids == (
            identities[3],
        )
        assert all(row.withdrawn_at is None for row in session.scalars(select(PendingDiscovery)))
    failed.clear()
    assert worker.run_once()
    with durable_database.session() as session:
        stored = session.get(SourceScan, scan_id)
        assert stored is not None
        assert (stored.raw_count, stored.read_count) == (4, 3)
        receipts = [
            SourceDetailCheckpoint.from_json(row.detail_checkpoint)
            for row in session.scalars(select(SourceScanFrontierItem))
            if row.detail_checkpoint
        ]
        assert dict(pair for receipt in receipts for pair in receipt.completed) == {
            identities[index]: urls[index] for index in (0, 1, 3)
        }
    assert worker.run_once() is False
    with durable_database.session() as session:
        stored = session.get(SourceScan, scan_id)
        assert stored is not None and stored.read_count == 3


@pytest.mark.parametrize("changed_url", [False, True])
def test_native_jazzhr_relisting_retains_prior_bodies_and_reads_current_original_urls(
    durable_database: Database,
    changed_url: bool,
) -> None:
    root = "https://acme.applytojob.com/apply"
    earlier = root + "/Before0001/Clinical-Analyst"
    pending = root + "/During0002/Clinical-Analyst"
    later = root + (
        "/Before0001/Alternate-Analyst" if changed_url else "/Later00003/Clinical-Analyst"
    )
    current = [earlier, pending]
    requested: list[str] = []
    stopped = [False]

    class RelistingJazz(BoardApiFetcher):
        def continue_listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            stopped[0] = False
            return super().continue_listings(*args, **kwargs)

        @staticmethod
        def deadline_elapsed() -> bool:
            return stopped[0]

        def get_text(self, url: str, *, max_bytes: int = 0) -> str:
            if url == root:
                stopped[0] = False
                return (
                    "<main>"
                    + "".join(f'<a href="{address}">Clinical Analyst</a>' for address in current)
                    + "</main>"
                )
            assert url in {earlier, pending, later}
            requested.append(url)
            if url == earlier:
                stopped[0] = True
            return _jazzhr_posting_page("Clinical Analyst", "Springfield")

    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", root)
    state_target_titles(durable_database, workspace_id, "Clinical Analyst")
    worker = _enqueue_scan(durable_database, workspace_id, RelistingJazz())
    assert worker.run_once()
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert (scan.raw_count, scan.read_count) == (2, 1)
    current[:] = [pending, later]
    assert worker.run_once()
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert (scan.raw_count, scan.read_count) == ((2, 1) if changed_url else (3, 3))
        assert scan.complete is False
        assert scan.detail_coverage is None
        assert scan.skip_reasons["publisher_listing_changed"] == 1
        assert (
            "The publisher's job list changed during this scan. "
            "Scan again to check the current list." in scan.notes
        )
        assert all(row.withdrawn_at is None for row in session.scalars(select(PendingDiscovery)))
        receipts = [
            SourceDetailCheckpoint.from_json(row.detail_checkpoint)
            for row in session.scalars(select(SourceScanFrontierItem))
            if row.detail_checkpoint
        ]
        assert ("Before0001", earlier) in {
            pair for receipt in receipts for pair in receipt.completed
        }
        owed = {
            row.url
            for row in session.scalars(select(SourceScanFrontierItem))
            if row.status == "pending"
        }
        assert later in requested or later in owed
    assert requested == [earlier, pending, later]


@pytest.mark.parametrize("conflicting", [False, True])
def test_native_listed_sitemap_arrivals_union_history_and_reject_conflicting_urls(
    durable_database: Database,
    conflicting: bool,
) -> None:
    host = "https://careers.acme.example"
    root = host + "/Health/"
    leaves = tuple(host + "/sitemap-" + name + ".xml" for name in ("initial", "later", "last"))
    urls = tuple(
        host + f"/Health/job/Clinical-Analyst/{identity}/" for identity in (1001, 1002, 1003)
    )
    failed = set(leaves[1:])
    alternate = urls[1].replace("Clinical-Analyst", "Alternate-Analyst") if conflicting else urls[1]
    pages = {
        root: '<html><script src="https://rmkcdn.successfactors.com/assets/site.js"></script></html>',
        host + "/sitemap.xml": '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(f"<sitemap><loc>{leaf}</loc></sitemap>" for leaf in leaves)
        + "</sitemapindex>",
        leaves[0]: sitemap_urlset(urls[0]),
        leaves[1]: sitemap_urlset(urls[1]),
        leaves[2]: sitemap_urlset(urls[2], alternate),
        **{
            url: declaring_page(
                {
                    "@context": "https://schema.org",
                    "@type": "JobPosting",
                    "title": "Clinical Analyst",
                    "description": "Analyze clinical data for patient programs.",
                    "jobLocation": {
                        "@type": "Place",
                        "address": {"@type": "PostalAddress", "addressCountry": "US"},
                    },
                }
            )
            for url in (*urls, alternate)
        },
    }

    def answer(url: str) -> httpx.Response:
        return httpx.Response(
            503 if url in failed else 200, request=httpx.Request("GET", url), text=pages[url]
        )

    fetcher = BoardApiFetcher(client=policy_client(answer))
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", root)
    state_target_titles(durable_database, workspace_id, "Clinical Analyst")
    worker = _enqueue_scan(durable_database, workspace_id, fetcher)
    assert worker.run_once()
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert (scan.raw_count, scan.read_count) == (1, 1)
        assert scan.complete is False
    failed.clear()
    assert worker.run_once()
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert (scan.raw_count, scan.read_count) == (3, 1)
        assert scan.complete is False
    assert worker.run_once()
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert (scan.raw_count, scan.read_count) == (3, 2 if conflicting else 3)
        assert scan.detail_coverage is None
        assert {row.discovery_url for row in session.scalars(select(PendingDiscovery))} == {
            *urls,
            alternate,
        }
        assert all(row.withdrawn_at is None for row in session.scalars(select(PendingDiscovery)))
        receipts = [
            SourceDetailCheckpoint.from_json(row.detail_checkpoint)
            for row in session.scalars(select(SourceScanFrontierItem))
            if row.detail_checkpoint
        ]
        assert {identity for receipt in receipts for identity in receipt.listing_ids} == {
            "1001",
            "1002",
            "1003",
        }
    assert worker.run_once() is False


@pytest.mark.parametrize(
    "saved_root", ["https://careers.acme.example/", "https://careers.acme.example"]
)
def test_native_listed_receipt_settles_a_carried_pending_navigation_row_at_the_root(
    durable_database: Database,
    saved_root: str,
) -> None:
    host = "https://careers.acme.example"
    urls = tuple(host + f"/job/Clinical-Analyst/{identity}/" for identity in (1001, 1002))
    other = host + "/about/"
    failed = {urls[1]}
    fail_once: set[str] = set()
    pages = {
        host + "/": '<html><script src="https://rmkcdn.successfactors.com/assets/site.js"></script>'
        "</html>",
        host + "/sitemap.xml": sitemap_urlset(*urls),
        **{
            url: declaring_page(
                {
                    "@context": "https://schema.org",
                    "@type": "JobPosting",
                    "title": "Clinical Analyst",
                    "description": "Analyze clinical data for patient programs.",
                    "jobLocation": {
                        "@type": "Place",
                        "address": {"@type": "PostalAddress", "addressCountry": "US"},
                    },
                }
            )
            for url in urls
        },
    }

    def answer(url: str) -> httpx.Response:
        page = pages.get(url if url in pages else url + "/")
        refused = url in failed or url in fail_once
        fail_once.discard(url)
        return httpx.Response(
            503 if refused else 200 if page is not None else 404,
            request=httpx.Request("GET", url),
            text=page or "",
        )

    fetcher = BoardApiFetcher(client=policy_client(answer))
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", saved_root)
    state_target_titles(durable_database, workspace_id, "Clinical Analyst")
    worker = _enqueue_scan(durable_database, workspace_id, fetcher)
    while worker.run_once():
        pass
    with durable_database.session() as session:
        prior = session.scalars(select(SourceScan)).one()
        assert prior.read_count == 1
        prior.scanned_on -= timedelta(days=1)
        root_url = normalize_fetch_url(saved_root)
        receipt_row = session.scalars(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.normalized_url == root_url)
        ).one()
        continuation_id = receipt_row.continuation_id
        session.delete(receipt_row)
        session.flush()
        # The Careers link a posting page offers, and an unrelated navigation link.
        for url, label in ((saved_root, "Careers"), (other, "About")):
            session.add(
                SourceScanFrontierItem(
                    continuation_id=continuation_id,
                    url=url,
                    normalized_url=normalize_fetch_url(url),
                    label=label,
                    label_source="visible",
                    structural_context="list",
                    relations=["substantive-label"],
                    status="pending",
                    deferred_reason="discovered_during_follow_up",
                    automatic_eligible=False,
                    detail_checkpoint=None,
                    depth=2,
                    source_url=urls[0],
                )
            )
    failed.clear()
    fail_once.add(urls[1])
    TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id, task_type="source.scan", payload={}, idempotency_key=new_id()
    )
    while worker.run_once():
        pass
    with durable_database.session() as session:
        today = session.scalars(select(SourceScan).order_by(SourceScan.scanned_on)).all()[-1]
        assert (today.raw_count, today.read_count) == (2, 2)
        continuation = session.scalars(
            select(SourceScanContinuation).where(SourceScanContinuation.source_scan_id == today.id)
        ).one()
        rows = {
            row.normalized_url: row
            for row in session.scalars(
                select(SourceScanFrontierItem).where(
                    SourceScanFrontierItem.continuation_id == continuation.id
                )
            )
        }
        settled = rows[root_url]
        assert settled.status == "completed"
        assert settled.detail_checkpoint is not None
        assert (
            SourceDetailCheckpoint.from_json(settled.detail_checkpoint).platform
            == "listed:successfactors"
        )
        assert rows[normalize_fetch_url(other)].status == "pending"


def test_hydrated_search_continuation_counts_current_full_bodies_despite_a_listing_gap(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    site = _TaleoSite()
    site.totals = [4, 4, 4]
    site.stop_after_pages = 1
    source_id = _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Clinical Analyst")
    worker = _enqueue_scan(durable_database, workspace_id, site.fetcher)
    assert worker.run_once()
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert (scan.raw_count, scan.read_count) == (2, 0)
        assert not scan.complete
    site.stopped = False
    while worker.run_once():
        pass
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan).where(SourceScan.source_id == source_id)).one()
        assert scan.raw_count == scan.read_count == 3
        assert scan.detail_read_count == 3
        assert not scan.complete
        assert scan.detail_coverage is None
        assert scan.skip_reasons["publisher_listing_total_mismatch"] == 1
        assert "reports 4 listings" in scan.notes and "3 distinct identities" in scan.notes
        assert {row.discovery_url for row in session.scalars(select(PendingDiscovery))} == {
            site.detail_url(number) for number in (1, 2, 3)
        }
        assert all(row.withdrawn_at is None for row in session.scalars(select(PendingDiscovery)))
        pending = [
            (parse_qs(urlsplit(row.url).query)["__reader_pass"], row.automatic_eligible)
            for row in session.scalars(
                select(SourceScanFrontierItem).where(SourceScanFrontierItem.status == "pending")
            )
        ]
        assert pending == [(["3"], False)]
    assert site.detail_reads == 3
    assert [body["pageNo"] for _, body, _ in site.posts] == [1, 2, 1, 2]


def test_exact_hydrated_search_detail_augments_its_current_body_receipt(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    site = _TaleoSite()
    site.failed = {2}
    site.totals = [4, 4, 4]
    _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Clinical Analyst")
    worker = _enqueue_scan(durable_database, workspace_id, site.fetcher)
    assert worker.run_once()
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        scan_id = scan.id
        assert scan.raw_count == 3 and scan.read_count == 2
        assert scan.detail_read_count == 2
        failed = session.scalars(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.url == site.detail_url(2))
        ).one()
        assert not failed.automatic_eligible
        assert SourceDetailCheckpoint.from_json(failed.detail_checkpoint).failed_attempts == (
            ("2", 1),
        )
    site.failed.clear()
    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=site.fetcher,
        posting_fetcher=UnexpectedPostingFetcher(),
    ).request_continuation(workspace_id, scan_id)
    assert worker.run_once()
    with durable_database.session() as session:
        stored = session.get(SourceScan, scan_id)
        assert stored is not None
        assert stored.raw_count == stored.read_count == 3
        assert stored.detail_read_count == 3
        assert not stored.complete
        assert stored.detail_coverage is None
        recovered = session.scalars(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.url == site.detail_url(2))
        ).one()
        assert recovered.status == "completed"
        assert all(row.withdrawn_at is None for row in session.scalars(select(PendingDiscovery)))
    assert site.detail_reads == 4


def test_failed_search_detail_retry_preserves_its_latest_attempt_receipt(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    site = _TaleoSite()
    site.failed = {2}
    site.totals = [4, 4, 4]
    _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Clinical Analyst")
    worker = _enqueue_scan(durable_database, workspace_id, site.fetcher)
    assert worker.run_once()
    with durable_database.session() as session:
        scan_id = session.scalars(select(SourceScan.id)).one()
    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=site.fetcher,
        posting_fetcher=UnexpectedPostingFetcher(),
    ).request_continuation(workspace_id, scan_id)
    assert worker.run_once()
    with durable_database.session() as session:
        scan = session.get(SourceScan, scan_id)
        assert scan is not None and scan.raw_count == 3 and scan.read_count == 2
        failed = session.scalars(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.url == site.detail_url(2))
        ).one()
        assert SourceDetailCheckpoint.from_json(failed.detail_checkpoint).failed_attempts == (
            ("2", 2),
        )
        assert failed.status == "pending" and not failed.automatic_eligible
    assert site.detail_reads == 4


@pytest.mark.parametrize("prior_attempts", [2, 3])
def test_current_declared_search_detail_refreshes_generic_routing_without_resetting_history(
    durable_database: Database,
    prior_attempts: int,
) -> None:
    workspace_id = _workspace(durable_database)
    site = _TaleoSite()
    source_id = _source(durable_database, workspace_id, "Acme", site.root)
    scan_id = _scan_row(durable_database, source_id, day=0)
    url = site.detail_url(2)
    old = SourceDetailCheckpoint(
        platform="taleo",
        board_url=url,
        pending_ids=("2",),
        failed_attempts=(("2", prior_attempts),),
    )
    continuation_id = _seed_continuation(
        durable_database,
        workspace_id,
        scan_id,
        [
            {
                "url": url,
                "source_url": site.root,
                "deferred_reason": "",
                "automatic_eligible": False,
                "attempt_count": 2,
                "last_error": "Retained body failure",
                "detail_checkpoint": old.as_json(),
            }
        ],
    )
    [before] = _rows(durable_database, continuation_id)
    target = replace(
        _continuation(url),
        source_url=site.root,
        deferred_reason="taleo_search_detail",
        detail_checkpoint=SourceDetailCheckpoint(
            platform="taleo",
            board_url=url,
            pending_ids=("2",),
            listing_ids=("1", "2"),
            listing_urls=(("1", site.detail_url(1)), ("2", url)),
            completed=(("1", site.detail_url(1)),),
        ),
    )
    result = _persist_frontier(durable_database, workspace_id, scan_id, (target,))
    assert result.inserted_count == 0
    [row] = _rows(durable_database, continuation_id)
    assert row.id == before.id
    assert row.deferred_reason == "taleo_search_detail"
    refreshed = SourceDetailCheckpoint.from_json(row.detail_checkpoint)
    assert refreshed.pending_ids == ("2",)
    assert target.detail_checkpoint is not None
    assert refreshed.listing_urls == target.detail_checkpoint.listing_urls
    assert refreshed.failed_attempts == (("2", prior_attempts),)
    assert (row.status, row.attempt_count, row.last_error, row.automatic_eligible) == (
        "pending",
        2,
        "Retained body failure",
        False,
    )


@pytest.mark.parametrize(
    ("fresh", "prior_attempts", "interrupt_continuation", "guard"),
    [
        (fresh, attempts, interrupted, "")
        for fresh in (False, True)
        for attempts in (1, 2, MAX_AUTOMATIC_ATTEMPTS)
        for interrupted in (False, True)
    ]
    + [
        (True, MAX_AUTOMATIC_ATTEMPTS, False, guard)
        for guard in ("protected", "cancelled", "stale_generation")
    ],
)
def test_a_fresh_declared_detail_gets_one_automatic_retry_without_resetting_its_history(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    fresh: bool,
    prior_attempts: int,
    interrupt_continuation: bool,
    guard: str,
) -> None:
    workspace_id = _workspace(durable_database)
    site = _TaleoSite()
    site.pages = [[1], []]
    site.totals = [1, 1]
    source_id = _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Clinical Analyst")
    detail_url = site.detail_url(1)
    category_url = site.origin + "/categories/research"
    old_checkpoint = SourceDetailCheckpoint(
        platform="taleo",
        board_url=detail_url,
        pending_ids=("1",),
        listing_ids=("1",),
        listing_urls=(("1", detail_url),),
        failed_attempts=(("1", prior_attempts),),
    )
    _seed_continuation(
        durable_database,
        workspace_id,
        _scan_row(durable_database, source_id, day=-1),
        [
            {
                "url": detail_url,
                "source_url": site.root,
                "deferred_reason": "taleo_search_detail",
                "automatic_eligible": False,
                "attempt_count": prior_attempts,
                "last_error": "Retained body failure",
                "correlation_id": "prior-root",
                "detail_checkpoint": old_checkpoint.as_json(),
            },
            {
                "url": category_url,
                "source_url": site.root,
                "deferred_reason": "classification_ambiguous",
                "automatic_eligible": False,
                "attempt_count": 2,
                "last_error": "Retained category failure",
                "correlation_id": "prior-root",
            },
        ],
    )
    monkeypatch.setattr(site.fetcher, "deadline_elapsed", lambda: bool(site.posts))
    root = TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [source_id], "fresh_details": fresh},
        idempotency_key=new_id(),
    )
    worker = Worker(
        durable_database,
        worker_id="declared-detail-retry",
        handlers=default_handlers(durable_database, fetcher=site.fetcher),
    )

    assert worker.run_once()
    assert site.detail_reads == 0
    with durable_database.session() as session:
        member = session.scalars(
            select(SourceScanMember).where(SourceScanMember.root_task_id == str(root.id))
        ).one()
        continuation_id = member.continuation_id
    assert continuation_id is not None
    rows = {row.url: row for row in _rows(durable_database, continuation_id)}
    detail = rows[detail_url]
    assert detail.automatic_eligible is fresh
    assert detail.attempt_count == prior_attempts
    assert detail.last_error == "Retained body failure"
    assert SourceDetailCheckpoint.from_json(detail.detail_checkpoint).failed_attempts == (
        ("1", prior_attempts),
    )
    category = rows[category_url]
    assert (category.automatic_eligible, category.attempt_count, category.last_error) == (
        False,
        2,
        "Retained category failure",
    )
    if not fresh:
        assert not worker.run_once()
        assert site.detail_reads == 0
        return
    monkeypatch.setattr(site.fetcher, "deadline_elapsed", lambda: site.stopped)
    if guard:
        if guard == "protected":
            _stage_known_rows(durable_database, workspace_id, source_id, (detail_url,))
            with durable_database.session() as session:
                pending_id = session.scalars(select(PendingDiscovery.id)).one()
            dismiss_pending_discovery(durable_database, workspace_id, pending_id)
        elif guard == "cancelled":
            with durable_database.session() as session:
                queued_id = session.scalars(
                    select(BackgroundTask.id).where(
                        BackgroundTask.task_type == "source.continue",
                        BackgroundTask.status == "queued",
                    )
                ).one()
            TaskQueueService(durable_database).request_cancellation(
                queued_id, workspace_id=workspace_id
            )
        else:
            with durable_database.session() as session:
                continuation = session.get(SourceScanContinuation, continuation_id)
                assert continuation is not None
                continuation.generation += 1
        assert worker.run_once() is (guard != "cancelled")
        assert not worker.run_once()
        detail = next(
            row for row in _rows(durable_database, continuation_id) if row.url == detail_url
        )
        assert detail.attempt_count == prior_attempts
        assert site.detail_reads == 0 and detail_url not in site.requests
        if guard == "protected":
            assert detail.status == "completed" and detail.deferred_reason == "protected_posting"
        else:
            assert detail.status == "pending"
            assert SourceDetailCheckpoint.from_json(detail.detail_checkpoint).failed_attempts == (
                ("1", prior_attempts),
            )
        assert category_url not in site.requests
        return
    if interrupt_continuation:
        site.stopped = True
        assert worker.run_once()
        detail = next(
            row for row in _rows(durable_database, continuation_id) if row.url == detail_url
        )
        assert detail.automatic_eligible
        assert detail.attempt_count == prior_attempts
        assert SourceDetailCheckpoint.from_json(detail.detail_checkpoint).failed_attempts == (
            ("1", prior_attempts),
        )
        assert site.detail_reads == 0
        site.stopped = False

    assert worker.run_once()
    assert not worker.run_once()

    rows = {row.url: row for row in _rows(durable_database, continuation_id)}
    detail = rows[detail_url]
    assert detail.status == "completed"
    assert detail.attempt_count == prior_attempts + 1
    assert SourceDetailCheckpoint.from_json(detail.detail_checkpoint).completed == (
        ("1", detail_url),
    )
    assert site.requests.count(detail_url) == 1
    assert category_url not in site.requests
    category = rows[category_url]
    assert (
        category.status,
        category.automatic_eligible,
        category.attempt_count,
        category.last_error,
    ) == ("pending", False, 2, "Retained category failure")
    with durable_database.session() as session:
        captured = session.scalars(select(PendingDiscovery)).one()
        assert captured.discovery_url == detail_url
        assert captured.listing_artifact_id is not None


def test_a_later_fresh_search_page_reopens_only_its_declared_historical_detail(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = _workspace(durable_database)
    site = _TaleoSite()
    source_id = _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Clinical Analyst")
    url = site.detail_url(3)
    _seed_continuation(
        durable_database,
        workspace_id,
        _scan_row(durable_database, source_id, day=-1),
        [
            {
                "url": url,
                "source_url": site.root,
                "deferred_reason": "taleo_search_detail",
                "automatic_eligible": False,
                "attempt_count": 2,
                "last_error": "Retained body failure",
                "correlation_id": "prior-root",
                "detail_checkpoint": SourceDetailCheckpoint(
                    platform="taleo",
                    board_url=url,
                    pending_ids=("3",),
                    listing_ids=("3",),
                    listing_urls=(("3", url),),
                    failed_attempts=(("3", 2),),
                ).as_json(),
            }
        ],
    )
    site.stop_after_pages = 1
    root = TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [source_id], "fresh_details": True},
        idempotency_key=new_id(),
    )
    worker = Worker(
        durable_database,
        worker_id="later-declared-detail",
        handlers=default_handlers(durable_database, fetcher=site.fetcher),
    )

    assert worker.run_once()
    with durable_database.session() as session:
        member = session.scalars(
            select(SourceScanMember).where(SourceScanMember.root_task_id == str(root.id))
        ).one()
        continuation_id = member.continuation_id
    assert continuation_id is not None
    [held] = [row for row in _rows(durable_database, continuation_id) if row.url == url]
    assert held.automatic_eligible is False
    site.stopped = False
    monkeypatch.setattr(site.fetcher, "deadline_elapsed", lambda: len(site.posts) > 1)

    assert worker.run_once()

    [declared] = [row for row in _rows(durable_database, continuation_id) if row.url == url]
    assert declared.automatic_eligible is True
    assert declared.attempt_count == 2
    assert SourceDetailCheckpoint.from_json(declared.detail_checkpoint).failed_attempts == (
        ("3", 2),
    )
    assert site.requests.count(url) == 0
    monkeypatch.setattr(site.fetcher, "deadline_elapsed", lambda: False)

    assert worker.run_once()
    assert not worker.run_once()

    [completed] = [row for row in _rows(durable_database, continuation_id) if row.url == url]
    assert completed.status == "completed" and completed.attempt_count == 3
    assert site.requests.count(url) == 1


@pytest.mark.parametrize("day", [-1, 0])
@pytest.mark.parametrize("carried", [False, True])
@pytest.mark.parametrize("mode", ["fresh", "nonfresh", "omitted", "protected"])
def test_a_new_fresh_root_retries_its_exact_prior_failed_publisher_detail(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    day: int,
    carried: bool,
    mode: str,
) -> None:
    from clearscout.domain.identity import normalize_fetch_url

    workspace_id = _workspace(durable_database)
    site = _TaleoSite()
    site.pages = [[2] if mode == "omitted" else [1], []]
    site.totals = [1, 1]
    if mode == "protected":
        site.pages = [[1], [2], []]
        site.totals = [2, 2, 2]
        site.page_sizes = [1]
    source_id = _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Clinical Analyst")
    url = site.detail_url(1)
    checkpoint = SourceDetailCheckpoint(
        platform="taleo",
        board_url=url,
        pending_ids=("1",),
        listing_ids=("1",),
        listing_urls=(("1", url),),
        failed_attempts=(("1", 2),),
    )
    prior_continuation_id = _seed_continuation(
        durable_database,
        workspace_id,
        _scan_row(durable_database, source_id, day=day),
        [
            {
                "url": url,
                "normalized_url": normalize_fetch_url(url),
                "source_url": site.root,
                "deferred_reason": "taleo_search_detail",
                "status": "failed",
                "origin": "source.scan.carry_forward" if carried else "source.scan",
                "automatic_eligible": True,
                "attempt_count": MAX_AUTOMATIC_ATTEMPTS,
                "last_error": "Retained detail failure",
                "correlation_id": "prior-root",
                "detail_checkpoint": checkpoint.as_json(),
            }
        ],
    )
    [seeded] = _rows(durable_database, prior_continuation_id)
    assert seeded.url == url and seeded.normalized_url == normalize_fetch_url(url)
    assert seeded.status == "failed" and seeded.attempt_count == MAX_AUTOMATIC_ATTEMPTS
    seeded_id = seeded.id
    if mode == "protected":
        _stage_known_rows(durable_database, workspace_id, source_id, (url,))
        with durable_database.session() as session:
            pending_id = session.scalars(select(PendingDiscovery.id)).one()
        dismiss_pending_discovery(durable_database, workspace_id, pending_id)
    monkeypatch.setattr(site.fetcher, "deadline_elapsed", lambda: bool(site.posts))
    queue = TaskQueueService(durable_database)
    root = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [source_id], "fresh_details": mode != "nonfresh"},
        idempotency_key=new_id(),
    )
    worker = Worker(
        durable_database,
        worker_id="fresh-failed-detail",
        handlers=default_handlers(durable_database, fetcher=site.fetcher),
    )

    assert worker.run_once()

    with durable_database.session() as session:
        member = session.scalars(
            select(SourceScanMember).where(SourceScanMember.root_task_id == str(root.id))
        ).one()
        continuation_id = member.continuation_id
    assert continuation_id is not None
    if day == 0:
        assert continuation_id == prior_continuation_id
    carried_rows = [row for row in _rows(durable_database, continuation_id) if row.url == url]
    if mode == "omitted" and day == -1:
        # A later day's verified listing that drops the detail lets it lapse.
        assert carried_rows == []
        monkeypatch.setattr(site.fetcher, "deadline_elapsed", lambda: False)
        while worker.run_once():
            pass
        assert site.requests.count(url) == 0
        scan = _board_scan_row(durable_database)
        assert (scan.status, scan.complete) == ("completed", True)
        return
    [row] = carried_rows
    if day == 0:
        assert row.id == seeded_id
    assert row.normalized_url == normalize_fetch_url(url)
    if mode != "fresh":
        assert row.status == "failed" and not row.automatic_eligible
        assert row.attempt_count == MAX_AUTOMATIC_ATTEMPTS
        assert row.last_error == "Retained detail failure"
        assert SourceDetailCheckpoint.from_json(row.detail_checkpoint).failed_attempts == (
            ("1", 2),
        )
        assert site.requests.count(url) == 0
        return
    assert row.status == "pending" and row.automatic_eligible
    assert row.attempt_count == MAX_AUTOMATIC_ATTEMPTS
    assert row.last_error == "Retained detail failure"
    assert SourceDetailCheckpoint.from_json(row.detail_checkpoint).failed_attempts == (
        ("1", MAX_AUTOMATIC_ATTEMPTS),
    )
    assert site.detail_reads == 0
    monkeypatch.setattr(site.fetcher, "deadline_elapsed", lambda: False)

    assert worker.run_once()
    assert not worker.run_once()

    [row] = [row for row in _rows(durable_database, continuation_id) if row.url == url]
    assert row.status == "completed"
    assert row.attempt_count == MAX_AUTOMATIC_ATTEMPTS + 1
    assert site.requests.count(url) == 1


def test_a_pacing_deadline_keeps_a_fresh_detail_pending_without_spending_history(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clearscout.domain.identity import normalize_fetch_url
    from tests.loop.test_fxrecruiter_reader import _FeedSite

    workspace_id = _workspace(durable_database)
    site = _FeedSite()
    source_id = _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    url = site.url(1)
    original = site.fetcher.client.getter
    assert original is not None

    def answer(address: str, **kwargs: Any) -> httpx.Response:
        if address == site.origin + "/feeds/jobs-rss":
            site.stopped = True
            return httpx.Response(
                200,
                request=httpx.Request("GET", address),
                text=(
                    f"<rss><channel><item><title>Data Analyst 1</title><link>{url}</link>"
                    "</item></channel></rss>"
                ),
            )
        return original(address, **kwargs)

    site.fetcher.client.getter = answer
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.board_adapters.transport.time.monotonic",
        lambda: site.now,
    )
    prior_id = _seed_continuation(
        durable_database,
        workspace_id,
        _scan_row(durable_database, source_id, day=0),
        [
            {
                "url": url,
                "normalized_url": normalize_fetch_url(url),
                "source_url": site.root,
                "deferred_reason": "fxrecruiter_search_detail",
                "automatic_eligible": True,
                "attempt_count": 2,
                "last_error": "Retained body failure",
                "correlation_id": "prior-root",
                "detail_checkpoint": SourceDetailCheckpoint(
                    platform="fxrecruiter",
                    board_url=url,
                    pending_ids=("1",),
                    listing_ids=("1",),
                    listing_urls=(("1", url),),
                    failed_attempts=(("1", 2),),
                ).as_json(),
            }
        ],
    )
    queue = TaskQueueService(durable_database)
    queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [source_id], "fresh_details": True},
        idempotency_key=new_id(),
    )
    worker = Worker(
        durable_database,
        worker_id="pacing-detail-history",
        handlers=default_handlers(durable_database, fetcher=site.fetcher),
    )
    assert worker.run_once()
    assert site.detail_reads == 0
    site.stopped = False
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.board_adapters.read_scope.MAX_PAGE_READ_SECONDS", 1.0
    )

    assert worker.run_once()

    [retained] = [row for row in _rows(durable_database, prior_id) if row.url == url]
    assert site.detail_reads == 0 and url not in site.requested
    assert retained.status == "pending" and retained.automatic_eligible
    assert retained.attempt_count == 2
    assert retained.last_error == "Retained body failure"
    assert SourceDetailCheckpoint.from_json(retained.detail_checkpoint).failed_attempts == (
        ("1", 2),
    )
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.board_adapters.read_scope.MAX_PAGE_READ_SECONDS", 30.0
    )

    assert worker.run_once()
    assert not worker.run_once()

    [completed] = [row for row in _rows(durable_database, prior_id) if row.url == url]
    assert completed.status == "completed" and completed.attempt_count == 3
    assert site.requested.count(url) == 1


def test_a_complete_sweep_keeps_an_off_target_listed_row_unread_and_listed(
    durable_database: Database,
) -> None:
    from tests.loop.test_fxrecruiter_reader import _FeedSite

    workspace_id = _workspace(durable_database)
    site = _FeedSite()
    site.titles = {2: "Warehouse Forklift Operator"}
    source_id = _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    _stage_known_rows(durable_database, workspace_id, source_id, (site.url(2),))
    with durable_database.session() as session:
        known_id = session.scalars(select(PendingDiscovery.id)).one()

    worker = _enqueue_scan(durable_database, workspace_id, site.fetcher)
    for _ in range(4):
        if not worker.run_once():
            break

    assert site.url(2) not in site.requested
    assert site.detail_reads == 2
    with durable_database.session() as session:
        known = session.get(PendingDiscovery, known_id)
        assert known is not None
        assert (known.absent_since, known.withdrawn_at) == (None, None)
        assert not session.scalars(select(SourceScanFrontierItem)).all()


def test_a_relisted_search_continuation_reads_no_off_target_detail_and_counts_it(
    durable_database: Database,
) -> None:
    from tests.loop.test_fxrecruiter_reader import _FeedSite

    workspace_id = _workspace(durable_database)
    site = _FeedSite()
    site.titles = {2: "Warehouse Forklift Operator"}
    _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    original = site.fetcher.client.getter
    assert original is not None

    def stop_after_root(address: str, **kwargs: Any) -> httpx.Response:
        site.stopped = site.stopped or address == site.root
        return original(address, **kwargs)

    site.fetcher.client.getter = stop_after_root
    worker = _enqueue_scan(durable_database, workspace_id, site.fetcher)
    assert worker.run_once()
    with durable_database.session() as session:
        [cursor] = session.scalars(select(SourceScanFrontierItem)).all()
        assert cursor.deferred_reason == "fxrecruiter_search_page"
    site.fetcher.client.getter = original
    site.stopped = False

    for _ in range(4):
        if not worker.run_once():
            break

    assert site.url(1) in site.requested
    assert site.url(2) not in site.requested
    with durable_database.session() as session:
        assert normalize_fetch_url(site.url(2)) not in set(
            session.scalars(select(SourceScanFrontierItem.normalized_url))
        )
        tasks = session.scalars(
            select(BackgroundTask).where(BackgroundTask.task_type == "source.continue")
        ).all()
        counts = {(task.progress["matched"], task.progress["no_title_match"]) for task in tasks}
    assert counts == {(2, 1)}


def test_a_fresh_detail_host_hold_keeps_history_and_stops_the_automatic_chain(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clearscout.domain.identity import normalize_fetch_url
    from tests.loop.test_fxrecruiter_reader import _FeedSite

    workspace_id = _workspace(durable_database)
    site = _FeedSite()
    source_id = _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    url = site.url(1)
    feed_url = site.origin + "/feeds/jobs-rss"
    hold_url = site.origin + "/publisher-hold"

    def answer(address: str) -> httpx.Response:
        if address == hold_url:
            return httpx.Response(
                429, request=httpx.Request("GET", address), headers={"Retry-After": "120"}
            )
        if address == feed_url:
            site.stopped = True
            return httpx.Response(
                200,
                request=httpx.Request("GET", address),
                text=(
                    f"<rss><channel><item><title>Data Analyst 1</title><link>{url}</link>"
                    "</item></channel></rss>"
                ),
            )
        return site.answer(address)

    site.fetcher = BoardApiFetcher(client=policy_client(answer))
    _seed_continuation(
        durable_database,
        workspace_id,
        _scan_row(durable_database, source_id, day=-1),
        [
            {
                "url": url,
                "normalized_url": normalize_fetch_url(url),
                "source_url": site.root,
                "deferred_reason": "fxrecruiter_search_detail",
                "automatic_eligible": False,
                "attempt_count": MAX_AUTOMATIC_ATTEMPTS,
                "last_error": "Retained body failure",
                "correlation_id": "prior-root",
                "detail_checkpoint": SourceDetailCheckpoint(
                    platform="fxrecruiter",
                    board_url=url,
                    pending_ids=("1",),
                    listing_ids=("1",),
                    listing_urls=(("1", url),),
                    failed_attempts=(("1", MAX_AUTOMATIC_ATTEMPTS),),
                ).as_json(),
            }
        ],
    )
    monkeypatch.setattr(site.fetcher, "deadline_elapsed", lambda: site.stopped)
    queue = TaskQueueService(durable_database)
    root = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [source_id], "fresh_details": True},
        idempotency_key=new_id(),
    )
    worker = Worker(
        durable_database,
        worker_id="held-fresh-detail",
        handlers=default_handlers(durable_database, fetcher=site.fetcher),
    )
    assert worker.run_once()
    with durable_database.session() as session:
        member = session.scalars(
            select(SourceScanMember).where(SourceScanMember.root_task_id == str(root.id))
        ).one()
        continuation_id = member.continuation_id
    assert continuation_id is not None
    assert site.fetcher.client.get(hold_url).status_code == 429
    site.stopped = False

    assert worker.run_once()
    assert not worker.run_once()

    [held] = [row for row in _rows(durable_database, continuation_id) if row.url == url]
    assert held.status == "pending" and not held.automatic_eligible
    assert held.attempt_count == MAX_AUTOMATIC_ATTEMPTS
    assert held.last_error == "Retained body failure"
    assert SourceDetailCheckpoint.from_json(held.detail_checkpoint).failed_attempts == (
        ("1", MAX_AUTOMATIC_ATTEMPTS),
    )
    assert url not in site.requested and site.detail_reads == 0
    site.fetcher.client.close()
    site.fetcher.client = policy_client(answer)
    site.stopped = False
    monkeypatch.setattr(site.fetcher, "deadline_elapsed", lambda: site.stopped)
    queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [source_id], "fresh_details": True},
        idempotency_key=new_id(),
    )
    assert worker.run_once()
    site.stopped = False

    assert worker.run_once()
    assert not worker.run_once()

    [completed] = [row for row in _rows(durable_database, continuation_id) if row.url == url]
    assert completed.status == "completed"
    assert completed.attempt_count == MAX_AUTOMATIC_ATTEMPTS + 1
    assert site.requested.count(url) == 1


def test_a_taleo_context_host_hold_does_not_spend_a_fresh_detail_attempt(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clearscout.domain.identity import normalize_fetch_url

    workspace_id = _workspace(durable_database)
    site = _TaleoSite()
    site.pages = [[1], []]
    site.totals = [1, 1]
    source_id = _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Clinical Analyst")
    url = site.detail_url(1)
    _seed_continuation(
        durable_database,
        workspace_id,
        _scan_row(durable_database, source_id, day=-1),
        [
            {
                "url": url,
                "normalized_url": normalize_fetch_url(url),
                "source_url": site.root,
                "deferred_reason": "taleo_search_detail",
                "automatic_eligible": False,
                "attempt_count": MAX_AUTOMATIC_ATTEMPTS,
                "last_error": "Retained body failure",
                "correlation_id": "prior-root",
                "detail_checkpoint": SourceDetailCheckpoint(
                    platform="taleo",
                    board_url=url,
                    pending_ids=("1",),
                    listing_ids=("1",),
                    listing_urls=(("1", url),),
                    failed_attempts=(("1", MAX_AUTOMATIC_ATTEMPTS),),
                ).as_json(),
            }
        ],
    )
    monkeypatch.setattr(site.fetcher, "deadline_elapsed", lambda: bool(site.posts))
    queue = TaskQueueService(durable_database)
    root = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [source_id], "fresh_details": True},
        idempotency_key=new_id(),
    )
    worker = Worker(
        durable_database,
        worker_id="held-taleo-context",
        handlers=default_handlers(durable_database, fetcher=site.fetcher),
    )
    assert worker.run_once()
    with durable_database.session() as session:
        member = session.scalars(
            select(SourceScanMember).where(SourceScanMember.root_task_id == str(root.id))
        ).one()
        continuation_id = member.continuation_id
    assert continuation_id is not None
    [admitted] = [row for row in _rows(durable_database, continuation_id) if row.url == url]
    assert admitted.automatic_eligible
    hold_url = site.origin + "/publisher-hold"

    def get(address: str, **kwargs: Any) -> httpx.Response:
        if address == hold_url:
            return httpx.Response(
                429, request=httpx.Request("GET", address), headers={"Retry-After": "120"}
            )
        return site.get(address, **kwargs)

    site.fetcher.client.getter = get
    assert site.fetcher.client.get(hold_url).status_code == 429
    monkeypatch.setattr(site.fetcher, "deadline_elapsed", lambda: False)

    assert worker.run_once()
    assert not worker.run_once()

    [held] = [row for row in _rows(durable_database, continuation_id) if row.url == url]
    assert held.status == "pending" and not held.automatic_eligible
    assert held.attempt_count == MAX_AUTOMATIC_ATTEMPTS
    assert held.last_error == "Retained body failure"
    assert SourceDetailCheckpoint.from_json(held.detail_checkpoint).failed_attempts == (
        ("1", MAX_AUTOMATIC_ATTEMPTS),
    )
    assert url not in site.requests and site.detail_reads == 0
    site.fetcher.client.close()
    site.fetcher.client = site.new_client()
    site.posts.clear()
    monkeypatch.setattr(site.fetcher, "deadline_elapsed", lambda: bool(site.posts))
    queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [source_id], "fresh_details": True},
        idempotency_key=new_id(),
    )
    assert worker.run_once()
    [readmitted] = [row for row in _rows(durable_database, continuation_id) if row.url == url]
    assert readmitted.automatic_eligible and readmitted.attempt_count == MAX_AUTOMATIC_ATTEMPTS
    monkeypatch.setattr(site.fetcher, "deadline_elapsed", lambda: False)

    assert worker.run_once()
    assert not worker.run_once()

    [completed] = [row for row in _rows(durable_database, continuation_id) if row.url == url]
    assert completed.status == "completed"
    assert completed.attempt_count == MAX_AUTOMATIC_ATTEMPTS + 1
    assert site.requests.count(url) == 1


@pytest.mark.parametrize("prior_failed", [False, True])
@pytest.mark.parametrize("failure", ["body", "transport"])
def test_a_fresh_detail_failure_cannot_reopen_on_same_root_page_reemission(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch, failure: str, prior_failed: bool
) -> None:
    workspace_id = _workspace(durable_database)
    site = _TaleoSite()
    site.pages = [[1], [2], []]
    site.page_sizes = [1]
    site.totals = [2, 2, 2]
    source_id = _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Clinical Analyst")
    url = site.detail_url(1)
    prior_attempts = MAX_AUTOMATIC_ATTEMPTS if prior_failed else 2
    _seed_continuation(
        durable_database,
        workspace_id,
        _scan_row(durable_database, source_id, day=-1),
        [
            {
                "url": url,
                "source_url": site.root,
                "deferred_reason": "taleo_search_detail",
                "automatic_eligible": False,
                "attempt_count": prior_attempts,
                "status": "failed" if prior_failed else "pending",
                "last_error": "Retained body failure",
                "correlation_id": "prior-root",
                "detail_checkpoint": SourceDetailCheckpoint(
                    platform="taleo",
                    board_url=url,
                    pending_ids=("1",),
                    listing_ids=("1",),
                    listing_urls=(("1", url),),
                    failed_attempts=(("1", 2),),
                ).as_json(),
            }
        ],
    )
    monkeypatch.setattr(continuation_handler, "CONTINUATION_CHUNK_SIZE", 1)
    original_listings = site.fetcher.listings
    original_continuation = site.fetcher.continue_listings
    root_reads: list[SourceRead] = []

    def listings(*args: Any, **kwargs: Any) -> SourceRead:
        read = original_listings(*args, **kwargs)
        root_reads.append(read)
        return read

    def continued(
        employer: str, targets: tuple[SourceContinuationTarget, ...], **kwargs: Any
    ) -> SourceRead:
        if targets[0].deferred_reason == "taleo_search_page":
            declaration = next(
                target for target in root_reads[-1].deferred_targets if target.url == url
            )
            return SourceRead(
                listings=[],
                complete=False,
                raw_count=1,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                attempted_target_urls=(targets[0].url,),
                deferred_targets=(declaration,),
            )
        return original_continuation(employer, targets, **kwargs)

    def get(address: str, **kwargs: Any) -> httpx.Response:
        if failure == "transport" and address == url and 1 in site.failed:
            site.requests.append(address)
            raise httpx.ConnectError(
                "Synthetic connection failure", request=httpx.Request("GET", address)
            )
        return site.get(address, **kwargs)

    monkeypatch.setattr(site.fetcher, "listings", listings)
    monkeypatch.setattr(site.fetcher, "continue_listings", continued)
    monkeypatch.setattr(site.fetcher, "deadline_elapsed", lambda: bool(site.posts))
    site.fetcher.client.getter = get
    queue = TaskQueueService(durable_database)
    root = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [source_id], "fresh_details": True},
        idempotency_key=new_id(),
    )
    worker = Worker(
        durable_database,
        worker_id="once-per-fresh-root",
        handlers=default_handlers(durable_database, fetcher=site.fetcher),
    )

    assert worker.run_once()
    site.failed = {1}
    monkeypatch.setattr(site.fetcher, "deadline_elapsed", lambda: False)
    assert worker.run_once()
    assert worker.run_once()
    assert not worker.run_once()

    assert site.requests.count(url) == 1
    with durable_database.session() as session:
        member = session.scalars(
            select(SourceScanMember).where(SourceScanMember.root_task_id == str(root.id))
        ).one()
        continuation_id = member.continuation_id
    assert continuation_id is not None
    [held] = [row for row in _rows(durable_database, continuation_id) if row.url == url]
    assert not held.automatic_eligible and held.attempt_count == prior_attempts + 1
    if failure == "body":
        assert SourceDetailCheckpoint.from_json(held.detail_checkpoint).failed_attempts == (
            ("1", prior_attempts + 1),
        )

    site.failed.clear()
    site.posts.clear()
    monkeypatch.setattr(site.fetcher, "deadline_elapsed", lambda: bool(site.posts))
    queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [source_id], "fresh_details": True},
        idempotency_key=new_id(),
    )

    assert worker.run_once()

    [readmitted] = [row for row in _rows(durable_database, continuation_id) if row.url == url]
    assert readmitted.automatic_eligible
    assert readmitted.attempt_count == prior_attempts + 1
    assert SourceDetailCheckpoint.from_json(readmitted.detail_checkpoint).failed_attempts == (
        ("1", prior_attempts + 1),
    )
    monkeypatch.setattr(site.fetcher, "deadline_elapsed", lambda: False)
    assert worker.run_once()
    assert worker.run_once()
    assert not worker.run_once()
    assert site.requests.count(url) == 2
    [completed] = [row for row in _rows(durable_database, continuation_id) if row.url == url]
    assert completed.status == "completed" and completed.attempt_count == prior_attempts + 2


@pytest.mark.parametrize(
    "mismatch", ["stable_id", "board_url", "platform", "source_root", "closed", "protected"]
)
def test_a_fresh_search_declaration_keeps_unowned_historical_details_manual(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch, mismatch: str
) -> None:
    workspace_id = _workspace(durable_database)
    site = _TaleoSite()
    site.pages = [[1], []]
    site.totals = [1, 1]
    source_id = _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Clinical Analyst")
    url = site.detail_url(1)
    job_id = "2" if mismatch == "stable_id" else "1"
    checkpoint = SourceDetailCheckpoint(
        platform="fxrecruiter" if mismatch == "platform" else "taleo",
        board_url=site.detail_url(2) if mismatch == "board_url" else url,
        pending_ids=() if mismatch in {"closed", "protected"} else (job_id,),
        listing_ids=(job_id,),
        listing_urls=((job_id, url),),
        failed_attempts=() if mismatch in {"closed", "protected"} else ((job_id, 2),),
        closed_ids=(job_id,) if mismatch == "closed" else (),
        protected=((job_id, url),) if mismatch == "protected" else (),
    )
    _seed_continuation(
        durable_database,
        workspace_id,
        _scan_row(durable_database, source_id, day=-1),
        [
            {
                "url": url,
                "source_url": site.root.replace("/external/", "/other/")
                if mismatch == "source_root"
                else site.root,
                "deferred_reason": "taleo_search_detail",
                "automatic_eligible": False,
                "attempt_count": 2,
                "last_error": "Retained body failure",
                "correlation_id": "prior-root",
                "detail_checkpoint": checkpoint.as_json(),
            }
        ],
    )
    monkeypatch.setattr(site.fetcher, "deadline_elapsed", lambda: bool(site.posts))
    TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [source_id], "fresh_details": True},
        idempotency_key=new_id(),
    )
    worker = Worker(
        durable_database,
        worker_id="unowned-declaration",
        handlers=default_handlers(durable_database, fetcher=site.fetcher),
    )

    assert worker.run_once()
    assert not worker.run_once()

    assert url not in site.requests
    with durable_database.session() as session:
        held = session.scalars(
            select(SourceScanFrontierItem)
            .where(SourceScanFrontierItem.url == url)
            .order_by(SourceScanFrontierItem.created_at.desc())
        ).first()
        assert held is not None and not held.automatic_eligible and held.attempt_count == 2


@pytest.mark.parametrize("historical_reason", ["", POSTING_DETAIL_COMPLETED])
@pytest.mark.parametrize("missing_own_body", [False, True])
@pytest.mark.parametrize("kind", ["taleo", "fxrecruiter"])
@pytest.mark.parametrize("failed_reopen", [False, True])
def test_fresh_carried_search_details_keep_their_owner_and_only_count_current_bodies(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    historical_reason: str,
    failed_reopen: bool,
    missing_own_body: bool,
) -> None:
    from clearscout.domain.identity import normalize_fetch_url
    from tests.loop.test_fxrecruiter_reader import _FeedSite

    workspace_id = _workspace(durable_database)
    site = _TaleoSite() if kind == "taleo" else _FeedSite()
    detail_url = site.detail_url if isinstance(site, _TaleoSite) else site.url
    source_id = _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Clinical Analyst", "Data Analyst")
    old_url, new_url, generic_url = (detail_url(number) for number in (1, 2, 3))
    old_checkpoint = SourceDetailCheckpoint(
        platform=kind,
        board_url=old_url,
        listing_ids=("1", "99"),
        listing_urls=(("1", old_url), ("99", detail_url(99))),
        completed=(("99", detail_url(99)),)
        if missing_own_body
        else (("1", old_url), ("99", detail_url(99))),
        pending_ids=("1",) if missing_own_body else (),
        listing_total=4,
    )
    _seed_continuation(
        durable_database,
        workspace_id,
        _scan_row(durable_database, source_id, day=0, status="completed"),
        [
            {
                "url": old_url,
                "normalized_url": normalize_fetch_url(old_url),
                "source_url": site.root,
                "status": "completed",
                "deferred_reason": historical_reason,
                "completed_at": utc_now() - timedelta(hours=1),
                "correlation_id": "historical-root",
                "detail_checkpoint": old_checkpoint.as_json(),
            }
        ],
    )
    new_target = replace(
        _continuation(new_url),
        source_url=site.root,
        deferred_reason=kind + "_search_detail",
        detail_checkpoint=SourceDetailCheckpoint(
            platform=kind,
            board_url=new_url,
            pending_ids=("2",),
            listing_ids=("2",),
            listing_urls=(("2", new_url),),
            listing_total=4,
        ),
    )
    generic_target = replace(_continuation(generic_url), source_url=site.root)
    first = SourceRead(
        listings=[],
        complete=False,
        raw_count=3,
        outcome=SourceReadOutcome.CRAWL_CONTINUATION,
        deferred_targets=(new_target, generic_target),
    )
    monkeypatch.setattr(site.fetcher, "listings", lambda *_args, **_kwargs: first)

    def generic_read(*_args: Any, **_kwargs: Any) -> SourceRead:
        return SourceRead(
            listings=[
                replace(
                    source_listing("Clinical Analyst 3", generic_url, "Acme"),
                    requisition_id="3",
                    published_description="A generic description without a scoped receipt.",
                )
            ],
            complete=False,
            raw_count=1,
            outcome=SourceReadOutcome.PUBLISHER_LINKED_PAGES,
            attempted_target_urls=(generic_url,),
        )

    monkeypatch.setattr(site.fetcher.publishers, "continue_listings", generic_read)
    if failed_reopen:
        site.failed = {1}
    root = TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [source_id], "fresh_details": True},
        idempotency_key=new_id(),
    )
    worker = Worker(
        durable_database,
        worker_id="fresh-search-owner",
        handlers=default_handlers(durable_database, fetcher=site.fetcher),
    )
    assert worker.run_once()
    with durable_database.session() as session:
        scan = session.scalars(
            select(SourceScan).where(SourceScan.correlation_id == str(root.id))
        ).one()
        scan_id = scan.id
        assert scan.read_count == 0
        row = session.scalars(
            select(SourceScanFrontierItem).where(
                SourceScanFrontierItem.url == old_url,
                SourceScanFrontierItem.status == "pending",
            )
        ).one()
        reopened = SourceDetailCheckpoint.from_json(row.detail_checkpoint)
        assert row.deferred_reason == kind + "_search_detail"
        assert row.correlation_id == str(root.id)
        assert reopened.pending_ids == ("1",)
        assert reopened.completed == ()
    assert worker.run_once()
    with durable_database.session() as session:
        stored_scan = session.get(SourceScan, scan_id)
        assert stored_scan is not None and stored_scan.read_count == (1 if failed_reopen else 2)
        assert stored_scan.raw_count == 3
        assert not stored_scan.complete and stored_scan.detail_coverage is None
        rows = session.scalars(
            select(SourceScanFrontierItem).where(
                SourceScanFrontierItem.correlation_id.in_(
                    [*continuation_task_ids(session, workspace_id, str(root.id)), str(root.id)]
                ),
                SourceScanFrontierItem.url.in_((old_url, new_url)),
            )
        ).all()
        bodies = {
            pair
            for row in rows
            for pair in SourceDetailCheckpoint.from_json(row.detail_checkpoint).completed
        }
        assert bodies == ({("2", new_url)} if failed_reopen else {("1", old_url), ("2", new_url)})
        if failed_reopen:
            failed = next(row for row in rows if row.url == old_url)
            assert failed.status == "pending" and not failed.automatic_eligible
            assert SourceDetailCheckpoint.from_json(failed.detail_checkpoint).failed_attempts == (
                ("1", 1),
            )
    if failed_reopen:
        site.failed.clear()
        SourceScanService(
            durable_database,
            storage=runtime_storage(durable_database),
            fetcher=site.fetcher,
            posting_fetcher=UnexpectedPostingFetcher(),
        ).request_continuation(workspace_id, scan_id)
        assert worker.run_once()
        with durable_database.session() as session:
            resumed_scan = session.get(SourceScan, scan_id)
            assert (
                resumed_scan is not None
                and resumed_scan.raw_count == 3
                and resumed_scan.read_count == 2
                and not resumed_scan.complete
            )
    assert site.detail_reads == (3 if failed_reopen else 2)


@pytest.mark.parametrize("generic_mixed", [False, True])
@pytest.mark.parametrize("historical_reason", ["", POSTING_DETAIL_COMPLETED])
@pytest.mark.parametrize("missing_own_body", [False, True])
@pytest.mark.parametrize("kind", ["taleo", "fxrecruiter"])
@pytest.mark.parametrize("failed_reopen", [False, True])
def test_fresh_listing_windows_exclude_carried_recovery_membership(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    generic_mixed: bool,
    kind: str,
    historical_reason: str,
    failed_reopen: bool,
    missing_own_body: bool,
) -> None:
    from clearscout.domain.identity import normalize_fetch_url
    from tests.loop.test_fxrecruiter_reader import _FeedSite

    workspace_id = _workspace(durable_database)
    site = _TaleoSite() if kind == "taleo" else _FeedSite()
    detail_url = site.detail_url if isinstance(site, _TaleoSite) else site.url
    own = 3 if kind == "taleo" else 99
    historical_id = "99" if kind == "taleo" else "98"
    old_url = detail_url(own)
    source_id = _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Clinical Analyst", "Data Analyst")
    _seed_continuation(
        durable_database,
        workspace_id,
        _scan_row(durable_database, source_id, day=0, status="completed"),
        [
            {
                "url": old_url,
                "normalized_url": normalize_fetch_url(old_url),
                "source_url": site.root,
                "status": "completed",
                "deferred_reason": historical_reason,
                "completed_at": utc_now() - timedelta(hours=1),
                "correlation_id": "historical-root",
                "detail_checkpoint": SourceDetailCheckpoint(
                    platform=kind,
                    board_url=old_url,
                    listing_ids=(str(own), historical_id),
                    listing_urls=(
                        (str(own), old_url),
                        (historical_id, detail_url(int(historical_id))),
                    ),
                    completed=((historical_id, detail_url(int(historical_id))),)
                    if missing_own_body
                    else ((str(own), old_url), (historical_id, detail_url(int(historical_id)))),
                    pending_ids=(str(own),) if missing_own_body else (),
                    listing_total=5,
                ).as_json(),
            }
        ],
    )
    if generic_mixed:
        generic_url = detail_url(5)
        original = site.fetcher.listings

        def listings(*args: Any, **kwargs: Any) -> SourceRead:
            read = original(*args, **kwargs)
            return replace(
                read,
                deferred_targets=(
                    *read.deferred_targets,
                    replace(_continuation(generic_url), source_url=site.root),
                ),
            )

        def generic_read(*_args: Any, **_kwargs: Any) -> SourceRead:
            return SourceRead(
                listings=[
                    replace(
                        source_listing("Data Analyst 5", generic_url, "Acme"),
                        requisition_id="5",
                        published_description="A generic description without a scoped receipt.",
                    )
                ],
                complete=False,
                raw_count=1,
                outcome=SourceReadOutcome.PUBLISHER_LINKED_PAGES,
                attempted_target_urls=(generic_url,),
            )

        monkeypatch.setattr(site.fetcher, "listings", listings)
        monkeypatch.setattr(site.fetcher.publishers, "continue_listings", generic_read)
    site.interrupt_after = 2
    if failed_reopen:
        site.failed = {own}
    root = TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [source_id], "fresh_details": True},
        idempotency_key=new_id(),
    )
    worker = Worker(
        durable_database,
        worker_id="fresh-listing-membership",
        handlers=default_handlers(durable_database, fetcher=site.fetcher),
    )
    assert worker.run_once()
    with durable_database.session() as session:
        scan = session.scalars(
            select(SourceScan).where(SourceScan.correlation_id == str(root.id))
        ).one()
        scan_id = scan.id
        assert scan.read_count == 2
    site.stopped = False
    assert worker.run_once()
    with durable_database.session() as session:
        stored = session.get(SourceScan, scan_id)
        assert stored is not None
        assert stored.raw_count == (4 if kind == "fxrecruiter" and not failed_reopen else 3)
        assert stored.read_count == (
            2
            if kind == "taleo" and failed_reopen
            else 4
            if kind == "fxrecruiter" and not failed_reopen
            else 3
        )
        assert stored.detail_coverage is None
        historical = session.scalars(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.url == old_url)
        ).one()
        if failed_reopen:
            assert historical.status == "pending" and not historical.automatic_eligible
            assert SourceDetailCheckpoint.from_json(
                historical.detail_checkpoint
            ).failed_attempts == ((str(own), 1),)
        if kind == "fxrecruiter":
            assert not stored.complete
            checkpoint = SourceDetailCheckpoint.from_json(historical.detail_checkpoint)
            assert checkpoint.listing_urls == ((str(own), old_url),)
        if not failed_reopen:
            assert (
                session.scalar(
                    select(SourceScanOutcome.id).where(
                        SourceScanOutcome.source_scan_id == scan_id,
                        SourceScanOutcome.url == old_url,
                    )
                )
                is not None
            )
        assert all(row.withdrawn_at is None for row in session.scalars(select(PendingDiscovery)))
    if failed_reopen:
        site.failed.clear()
        SourceScanService(
            durable_database,
            storage=runtime_storage(durable_database),
            fetcher=site.fetcher,
            posting_fetcher=UnexpectedPostingFetcher(),
        ).request_continuation(workspace_id, scan_id)
        assert worker.run_once()
        with durable_database.session() as session:
            recovered = session.get(SourceScan, scan_id)
            assert recovered is not None
            assert (
                recovered.raw_count == recovered.read_count == (4 if kind == "fxrecruiter" else 3)
            )
    requests = site.requests if isinstance(site, _TaleoSite) else site.requested
    assert detail_url(int(historical_id)) not in requests


@pytest.mark.parametrize("generic_mixed", [False, True])
def test_terminal_search_page_keeps_protected_enumeration_without_claiming_bodies(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    generic_mixed: bool,
) -> None:
    workspace_id = _workspace(durable_database)
    site = _TaleoSite()
    source_id = _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Clinical Analyst")
    staging = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).staging
    staging.stage_listings(
        workspace_id,
        [source_listing("Clinical Analyst 3", site.detail_url(3), "Acme")],
        utc_now().date(),
        reason="Synthetic retained opening",
    )
    with durable_database.session() as session:
        protected_id = session.scalars(select(PendingDiscovery.id)).one()
    dismiss_pending_discovery(durable_database, workspace_id, protected_id)
    if generic_mixed:
        original = site.fetcher.listings
        generic_url = site.detail_url(5)

        def listings(*args: Any, **kwargs: Any) -> SourceRead:
            read = original(*args, **kwargs)
            return replace(
                read,
                deferred_targets=(
                    *read.deferred_targets,
                    replace(_continuation(generic_url), source_url=site.root),
                ),
            )

        monkeypatch.setattr(site.fetcher, "listings", listings)
        monkeypatch.setattr(
            site.fetcher.publishers,
            "continue_listings",
            lambda *_args, **_kwargs: SourceRead(
                listings=[source_listing("Clinical Analyst 5", generic_url, "Acme")],
                complete=False,
                raw_count=1,
                outcome=SourceReadOutcome.PUBLISHER_LINKED_PAGES,
                attempted_target_urls=(generic_url,),
            ),
        )
    site.stop_after_pages = 1
    worker = _enqueue_scan(durable_database, workspace_id, site.fetcher)
    assert worker.run_once()
    site.stopped = False
    while worker.run_once():
        pass
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan).where(SourceScan.source_id == source_id)).one()
        assert scan.raw_count == 3 and scan.read_count == 2
        assert scan.detail_coverage is None
        protected = session.get(PendingDiscovery, protected_id)
        assert protected is not None and protected.withdrawn_at is None
    assert site.detail_url(3) not in site.requests


def test_empty_search_page_history_cannot_promote_an_old_failed_detail_map(
    durable_database: Database,
) -> None:
    from clearscout.infrastructure.web_fetch.board_reads import page_fingerprint

    workspace_id = _workspace(durable_database)
    site = _TaleoSite()
    source_id = _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Clinical Analyst")
    staging = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).staging
    staging.stage_listings(
        workspace_id,
        [
            source_listing(f"Clinical Analyst {number}", site.detail_url(number), "Acme")
            for number in (1, 2, 3)
        ],
        utc_now().date(),
        reason="Synthetic retained openings",
    )
    with durable_database.session() as session:
        protected_ids = tuple(session.scalars(select(PendingDiscovery.id)))
    for pending_id in protected_ids:
        dismiss_pending_discovery(durable_database, workspace_id, pending_id)
    old_url = site.detail_url(99)
    checkpoint = SourceDetailCheckpoint(
        platform="taleo",
        board_url=old_url,
        listing_ids=("99", "98"),
        listing_urls=(("99", old_url), ("98", site.detail_url(98))),
        completed=(("99", old_url), ("98", site.detail_url(98))),
        page_fingerprints=(page_fingerprint(["99", "98"]),),
        listing_total=2,
    )
    _seed_continuation(
        durable_database,
        workspace_id,
        _scan_row(durable_database, source_id, day=0, status="completed"),
        [
            {
                "url": old_url,
                "source_url": site.root,
                "status": "completed",
                "deferred_reason": POSTING_DETAIL_COMPLETED,
                "completed_at": utc_now() - timedelta(hours=1),
                "correlation_id": "historical-root",
                "detail_checkpoint": checkpoint.as_json(),
            }
        ],
    )
    site.stopped = True
    site.failed = {99}
    root = TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [source_id], "fresh_details": True},
        idempotency_key=new_id(),
    )
    worker = Worker(
        durable_database,
        worker_id="empty-search-page",
        handlers=default_handlers(durable_database, fetcher=site.fetcher),
    )
    assert worker.run_once()
    site.stopped = False
    while worker.run_once():
        pass
    with durable_database.session() as session:
        scan = session.scalars(
            select(SourceScan).where(SourceScan.correlation_id == str(root.id))
        ).one()
        assert scan.raw_count == 3 and scan.read_count == 0
        assert not scan.complete and scan.detail_coverage is None
        old = session.scalars(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.url == old_url)
        ).one()
        retained = SourceDetailCheckpoint.from_json(old.detail_checkpoint)
        assert old.status == "pending" and not old.automatic_eligible
        assert retained.listing_urls == (("99", old_url),)
        assert retained.failed_attempts == (("99", 1),)
        assert all(row.withdrawn_at is None for row in session.scalars(select(PendingDiscovery)))
    assert site.detail_url(98) not in site.requests
    assert not {site.detail_url(number) for number in (1, 2, 3)} & set(site.requests)


@pytest.mark.parametrize("failed_current", [False, True])
def test_exhausted_feed_enumeration_keeps_protected_ids_beside_positive_historical_recovery(
    durable_database: Database,
    failed_current: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clearscout.domain.identity import normalize_fetch_url
    from tests.loop.test_fxrecruiter_reader import _FeedSite
    from tests.support.doubles import UnexpectedContentResolver

    workspace_id = _workspace(durable_database)
    site = _FeedSite()
    source_id = _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    staging = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).staging
    staging.stage_listings(
        workspace_id,
        [source_listing("Data Analyst 3", site.url(3), "Acme")],
        utc_now().date(),
        reason="Synthetic retained opening",
    )
    with durable_database.session() as session:
        protected_id = session.scalars(select(PendingDiscovery.id)).one()
    dismiss_pending_discovery(durable_database, workspace_id, protected_id)
    old_url = site.url(99)
    staging.stage_listings(
        workspace_id,
        [source_listing("Data Analyst 99", old_url, "Acme")],
        (utc_now() - timedelta(days=1)).date(),
        reason="Synthetic retained historical opening",
        source_id=source_id,
    )
    with durable_database.session() as session:
        historical_pending_id = session.scalars(
            select(PendingDiscovery.id).where(PendingDiscovery.discovery_url == old_url)
        ).one()
    _seed_continuation(
        durable_database,
        workspace_id,
        _scan_row(durable_database, source_id, day=0, status="completed"),
        [
            {
                "url": old_url,
                "normalized_url": normalize_fetch_url(old_url),
                "source_url": site.root,
                "status": "completed",
                "deferred_reason": POSTING_DETAIL_COMPLETED,
                "completed_at": utc_now() - timedelta(hours=1),
                "correlation_id": "historical-root",
                "detail_checkpoint": SourceDetailCheckpoint(
                    platform="fxrecruiter",
                    board_url=old_url,
                    listing_ids=("99", "98"),
                    listing_urls=(("99", old_url), ("98", site.url(98))),
                    completed=(("99", old_url), ("98", site.url(98))),
                ).as_json(),
            }
        ],
    )
    if not failed_current:
        original = site.fetcher.listings

        def listings(*args: Any, **kwargs: Any) -> SourceRead:
            read = original(*args, **kwargs)
            assert read.complete
            return replace(
                read,
                complete=False,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                exhausted_outcome=SourceReadOutcome.ATS_COMPLETE,
                deferred_targets=(
                    replace(
                        _continuation(old_url),
                        source_url=site.root,
                        deferred_reason="fxrecruiter_search_detail",
                        detail_checkpoint=SourceDetailCheckpoint(
                            platform="fxrecruiter",
                            board_url=old_url,
                            pending_ids=("99",),
                            listing_ids=("99", "98"),
                            listing_urls=(("99", old_url), ("98", site.url(98))),
                        ),
                    ),
                ),
            )

        monkeypatch.setattr(site.fetcher, "listings", listings)
    site.failed = {2} if failed_current else set()
    root = TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [source_id], "fresh_details": True},
        idempotency_key=new_id(),
    )
    storage = runtime_storage(durable_database)
    content = PostingContentResolutionService(
        durable_database, UnexpectedContentResolver(), storage=storage
    )
    scan_handler = SourceScanHandler(
        durable_database,
        storage=storage,
        fetcher=site.fetcher,
        posting_fetcher=UnexpectedPostingFetcher(),
        content_resolution=content,
    )
    worker = Worker(
        durable_database,
        worker_id="feed-root-membership",
        handlers={
            "source.scan": scan_handler,
            "source.continue": SourceContinuationHandler(
                durable_database,
                site.fetcher,
                scan_handler.service.sweep,
                lambda workspace_id, task_id, outcomes: scan_handler.service.record_task_outcomes(
                    workspace_id, task_id, outcomes, origin="source.continue"
                ),
                scan_handler.service.begin_recording_pass,
            ),
        },
    )
    assert worker.run_once()
    with durable_database.session() as session:
        task = session.get(BackgroundTask, root.id)
        assert task is not None and task.status == "completed", (
            task.error_message if task is not None else "missing root task"
        )
    assert worker.run_once()
    with durable_database.session() as session:
        scan = session.scalars(
            select(SourceScan).where(SourceScan.correlation_id == str(root.id))
        ).one()
        assert scan.raw_count == 4 and scan.read_count == (2 if failed_current else 3)
        assert not scan.complete and scan.detail_coverage is None
        if not failed_current:
            assert scan.discovery_truncated and scan.exhausted_outcome is None
            assert scan.skip_reasons["publisher_listing_changed"] == 1
        retained = session.get(PendingDiscovery, historical_pending_id)
        assert retained is not None and retained.withdrawn_at is None
        assert retained.current_content_id is not None
        assert (
            session.scalar(
                select(SourceScanOutcome.id).where(
                    SourceScanOutcome.source_scan_id == scan.id,
                    SourceScanOutcome.url == old_url,
                )
            )
            is not None
        )
        protected = session.get(PendingDiscovery, protected_id)
        assert protected is not None and protected.withdrawn_at is None
        assert all(row.withdrawn_at is None for row in session.scalars(select(PendingDiscovery)))
    assert site.url(3) not in site.requested
    assert site.url(98) not in site.requested


@pytest.mark.parametrize("historical_reason", ["", POSTING_DETAIL_COMPLETED])
@pytest.mark.parametrize("invalid", ["platform", "foreign", "root", "ambiguous", "board"])
def test_stale_search_receipts_without_an_exact_owner_keep_generic_recovery(
    durable_database: Database,
    invalid: str,
    historical_reason: str,
) -> None:
    workspace_id = _workspace(durable_database)
    site = _TaleoSite()
    source_id = _source(durable_database, workspace_id, "Acme", site.root)
    url = site.detail_url(1)
    checkpoint = SourceDetailCheckpoint(
        platform="taleo",
        board_url=url,
        listing_ids=("1",),
        listing_urls=(("1", url),),
        completed=(("1", url),),
    )
    if invalid == "platform":
        checkpoint = replace(checkpoint, platform="unknown")
    elif invalid == "foreign":
        checkpoint = replace(
            checkpoint,
            listing_ids=("1", "2"),
            listing_urls=(("1", url), ("2", site.detail_url(2).replace("acme.", "other."))),
        )
    elif invalid == "ambiguous":
        checkpoint = replace(
            checkpoint,
            listing_ids=("1", "2"),
            listing_urls=(("1", url), ("2", url)),
            completed=(("1", url),),
        )
    elif invalid == "board":
        checkpoint = replace(checkpoint, board_url=site.detail_url(2))
    scan_id = _scan_row(durable_database, source_id, day=0)
    continuation_id = _seed_continuation(
        durable_database,
        workspace_id,
        scan_id,
        [
            {
                "url": url,
                "source_url": site.root,
                "status": "completed",
                "deferred_reason": historical_reason,
                "completed_at": utc_now() - timedelta(days=10),
                "detail_checkpoint": checkpoint.as_json(),
            }
        ],
    )
    if invalid == "root":
        with durable_database.session() as session:
            source = session.get(Source, source_id)
            assert source is not None
            source.url = site.root.replace("portal=1001", "portal=1002")
    _persist_frontier(durable_database, workspace_id, scan_id)
    rows = _rows(durable_database, continuation_id)
    if historical_reason:
        [row] = rows
        assert row.status == "pending" and row.deferred_reason == ""
    else:
        assert all(row.status == "completed" and row.deferred_reason == "" for row in rows)


def test_mixed_historical_search_failure_cannot_certify_current_body_counts(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = _workspace(durable_database)
    site = _TaleoSite()
    site.failed = {1}
    source_id = _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Clinical Analyst")
    old_url, new_url, stale_url = (site.detail_url(number) for number in (1, 2, 99))
    _seed_continuation(
        durable_database,
        workspace_id,
        _scan_row(durable_database, source_id, day=0),
        [
            {
                "url": old_url,
                "source_url": site.root,
                "status": "pending",
                "deferred_reason": "taleo_search_detail",
                "automatic_eligible": True,
                "correlation_id": "historical-root",
                "detail_checkpoint": SourceDetailCheckpoint(
                    platform="taleo",
                    board_url=old_url,
                    pending_ids=("1",),
                    listing_ids=("1", "99"),
                    listing_urls=(("1", old_url), ("99", stale_url)),
                    completed=(("99", stale_url),),
                    listing_total=4,
                ).as_json(),
            }
        ],
    )
    target = replace(
        _continuation(new_url),
        source_url=site.root,
        deferred_reason="taleo_search_detail",
        detail_checkpoint=SourceDetailCheckpoint(
            platform="taleo",
            board_url=new_url,
            pending_ids=("2",),
            listing_ids=("2",),
            listing_urls=(("2", new_url),),
            listing_total=4,
        ),
    )
    first = SourceRead(
        listings=[],
        complete=False,
        raw_count=3,
        outcome=SourceReadOutcome.CRAWL_CONTINUATION,
        deferred_targets=(target,),
    )
    monkeypatch.setattr(site.fetcher, "listings", lambda *_args, **_kwargs: first)
    root = TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [source_id], "fresh_details": True},
        idempotency_key=new_id(),
    )
    worker = Worker(
        durable_database,
        worker_id="mixed-search-family",
        handlers=default_handlers(durable_database, fetcher=site.fetcher),
    )
    assert worker.run_once()
    assert worker.run_once()
    with durable_database.session() as session:
        scan = session.scalars(
            select(SourceScan).where(SourceScan.correlation_id == str(root.id))
        ).one()
        assert scan.read_count == 1
        assert not scan.complete and scan.detail_coverage is None
        current = session.scalars(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.url == new_url)
        ).one()
        checkpoint = SourceDetailCheckpoint.from_json(current.detail_checkpoint)
        assert checkpoint.completed == (("2", new_url),)
        historical = session.scalars(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.url == old_url)
        ).one()
        assert historical.status == "pending" and not historical.automatic_eligible
        assert SourceDetailCheckpoint.from_json(historical.detail_checkpoint).failed_attempts == (
            ("1", 1),
        )


def test_fresh_search_detail_retry_counts_its_current_declared_scan_family(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    site = _TaleoSite()
    site.failed = {2}
    site.totals = [4, 4, 4]
    _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Clinical Analyst")
    worker = _enqueue_scan(durable_database, workspace_id, site.fetcher)
    assert worker.run_once()
    with durable_database.session() as session:
        prior = session.scalars(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.url == site.detail_url(2))
        ).one()
        prior_id, prior_correlation = prior.id, prior.correlation_id
    TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id, task_type="source.scan", payload={}, idempotency_key=new_id()
    )
    assert worker.run_once()
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        scan_id = scan.id
        assert scan.read_count == 2 and scan.raw_count == 3
        row = session.get(SourceScanFrontierItem, prior_id)
        assert row is not None
        assert not row.automatic_eligible
    site.failed.clear()
    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=site.fetcher,
        posting_fetcher=UnexpectedPostingFetcher(),
    ).request_continuation(workspace_id, scan_id)
    assert worker.run_once()
    with durable_database.session() as session:
        recovered_scan = session.get(SourceScan, scan_id)
        assert recovered_scan is not None
        assert recovered_scan.raw_count == recovered_scan.read_count == 3
        assert not recovered_scan.complete and recovered_scan.detail_coverage is None
        row = session.get(SourceScanFrontierItem, prior_id)
        assert row is not None and row.status == "completed"
        assert row.correlation_id != prior_correlation
        assert row.correlation_id == recovered_scan.correlation_id
        assert all(row.withdrawn_at is None for row in session.scalars(select(PendingDiscovery)))


@pytest.mark.parametrize("invalid_scope", [False, True])
def test_untrusted_search_continuation_retains_actionable_scan_history(
    durable_database: Database, invalid_scope: bool
) -> None:
    workspace_id = _workspace(durable_database)
    site = _TaleoSite()
    site.stop_after_pages = 1
    _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Clinical Analyst")
    worker = _enqueue_scan(durable_database, workspace_id, site.fetcher)
    assert worker.run_once()
    with durable_database.session() as session:
        row = session.scalars(
            select(SourceScanFrontierItem).where(
                SourceScanFrontierItem.deferred_reason == "taleo_search_page"
            )
        ).one()
        checkpoint = SourceDetailCheckpoint.from_json(row.detail_checkpoint)
        row.detail_checkpoint = (
            replace(checkpoint, board_url=site.root.replace("acme.", "other."))
            if invalid_scope
            else replace(checkpoint, listing_urls=(), completed=())
        ).as_json()
    before = len(site.posts), site.detail_reads
    site.stopped = False
    assert worker.run_once()
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        reason = (
            "invalid_publisher_search_scope"
            if invalid_scope
            else "publisher_listing_membership_missing"
        )
        assert scan.skip_reasons.get(reason) == 1
        assert "Scan the source again" in scan.notes
        assert not scan.read_count and not scan.complete
        row = session.scalars(
            select(SourceScanFrontierItem).where(
                SourceScanFrontierItem.status == "pending",
                SourceScanFrontierItem.deferred_reason == "taleo_search_page",
            )
        ).one()
        assert not row.automatic_eligible
    assert (len(site.posts), site.detail_reads) == before


def test_fresh_search_declaration_refreshes_a_legacy_page_hold_without_erasing_history(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    site = _TaleoSite()
    site.stop_after_pages = 1
    _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Clinical Analyst")
    worker = _enqueue_scan(durable_database, workspace_id, site.fetcher)
    assert worker.run_once()
    with durable_database.session() as session:
        row = session.scalars(
            select(SourceScanFrontierItem).where(
                SourceScanFrontierItem.deferred_reason == "taleo_search_page"
            )
        ).one()
        checkpoint = SourceDetailCheckpoint.from_json(row.detail_checkpoint)
        row.detail_checkpoint = replace(checkpoint, listing_urls=(), completed=()).as_json()
        row.correlation_id = "earlier-root"
        row.automatic_eligible = False
        row.attempt_count = 2
        row.last_error = "Retained publisher hold"
        row_id = row.id
        queued = session.scalars(
            select(BackgroundTask.id).where(BackgroundTask.status == "queued")
        ).all()
    for task_id in queued:
        TaskQueueService(durable_database).request_cancellation(task_id)
    site.stopped = False
    site.stop_after_pages = len(site.posts) + 1
    TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id, task_type="source.scan", payload={}, idempotency_key=new_id()
    )
    assert worker.run_once()
    with durable_database.session() as session:
        stored = session.get(SourceScanFrontierItem, row_id)
        assert stored is not None
        current = SourceDetailCheckpoint.from_json(stored.detail_checkpoint)
        assert dict(current.listing_urls) == {
            str(number): site.detail_url(number) for number in (1, 2)
        }
        assert current.completed == ()
        assert stored.correlation_id != "earlier-root"
        assert (stored.attempt_count, stored.last_error, stored.automatic_eligible) == (
            2,
            "Retained publisher hold",
            True,
        )
        scan = session.scalars(select(SourceScan)).one()
        scan_id = scan.id
    site.stopped = False
    while worker.run_once():
        pass
    with durable_database.session() as session:
        final_scan = session.get(SourceScan, scan_id)
        assert final_scan is not None and final_scan.read_count == 3
        stored = session.get(SourceScanFrontierItem, row_id)
        assert stored is not None and stored.status == "completed"


@pytest.mark.parametrize("completed_first", [False, True])
def test_search_body_count_does_not_pick_an_unobserved_conflicting_branch_by_row_order(
    durable_database: Database, completed_first: bool
) -> None:
    from clearscout.domain.identity import normalize_fetch_url

    workspace_id = _workspace(durable_database)
    site = _TaleoSite()
    site.interrupt_after = 2
    site.totals = [4, 4, 4]
    _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Clinical Analyst")
    worker = _enqueue_scan(durable_database, workspace_id, site.fetcher)
    assert worker.run_once()
    first_url, second_url = site.detail_url(4), site.detail_url(4) + "&revision=2"
    old, new = (second_url, first_url) if completed_first else (first_url, second_url)
    with durable_database.session() as session:
        continuation = session.scalars(select(SourceScanContinuation)).one()
        scan = session.scalars(select(SourceScan)).one()
        for complete in (True, False) if completed_first else (False, True):
            url = old if complete else new
            checkpoint = SourceDetailCheckpoint(
                platform="taleo",
                board_url=url,
                listing_ids=("4",),
                listing_urls=(("4", url),),
                pending_ids=() if complete else ("4",),
                completed=(("4", url),) if complete else (),
            )
            session.add(
                SourceScanFrontierItem(
                    id=new_id(),
                    continuation_id=continuation.id,
                    url=url,
                    normalized_url=normalize_fetch_url(url),
                    source_url=site.root,
                    label="Clinical Analyst 4",
                    label_source="visible",
                    structural_context="main",
                    relations=["publisher-content"],
                    status="completed" if complete else "pending",
                    deferred_reason=POSTING_DETAIL_COMPLETED if complete else "taleo_search_detail",
                    detail_checkpoint=checkpoint.as_json(),
                    automatic_eligible=False,
                    completed_at=utc_now() if complete else None,
                    correlation_id=scan.correlation_id,
                )
            )
    with durable_database.session() as session:
        rows = list(session.scalars(select(SourceScanFrontierItem)))
        completed_row = next(row for row in rows if row.deferred_reason == POSTING_DETAIL_COMPLETED)
        assert publisher_search_row_target(completed_row) is not None
    site.stopped = False
    assert worker.run_once()
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert scan.raw_count == 4
        assert scan.read_count == 3
        assert not scan.complete and scan.detail_coverage is None
        assert all(row.withdrawn_at is None for row in session.scalars(select(PendingDiscovery)))


@pytest.mark.parametrize("interruption", ["cancel", "source_loss", "stale_generation", "old_root"])
def test_hydrated_search_body_receipts_obey_the_owning_chunk_guard(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    workspace_id = _workspace(durable_database)
    site = _TaleoSite()
    site.stop_after_pages = 1
    _source(durable_database, workspace_id, "Acme", site.root)
    state_target_titles(durable_database, workspace_id, "Clinical Analyst")
    worker = _enqueue_scan(durable_database, workspace_id, site.fetcher)
    assert worker.run_once()
    with durable_database.session() as session:
        before = set(session.scalars(select(PendingDiscovery.id)))
        if interruption == "old_root":
            page = session.scalars(
                select(SourceScanFrontierItem).where(
                    SourceScanFrontierItem.deferred_reason == "taleo_search_page"
                )
            ).one()
            page.correlation_id = "unrelated-old-root"
    callbacks: list[str] = []
    readers = site.fetcher._continuation_readers()
    original = readers["taleo_search_page"]

    def guarded(employer: str, target: SourceContinuationTarget) -> SourceRead:
        read = original(employer, target)
        callbacks.append(interruption)
        with durable_database.session() as session:
            task = session.scalars(
                select(BackgroundTask).where(
                    BackgroundTask.task_type == "source.continue",
                    BackgroundTask.status == "running",
                )
            ).one()
            task_id = task.id
            if interruption == "source_loss":
                member = session.scalars(select(SourceScanMember)).one()
                member.active_attempt_count = 999
            elif interruption == "stale_generation":
                continuation = session.scalars(select(SourceScanContinuation)).one()
                continuation.generation += 1
        if interruption == "cancel":
            TaskQueueService(durable_database).request_cancellation(
                task_id, workspace_id=workspace_id
            )
        return read

    monkeypatch.setattr(
        site.fetcher, "_continuation_readers", lambda: {**readers, "taleo_search_page": guarded}
    )
    site.stopped = False
    assert worker.run_once()
    assert callbacks == [interruption]
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert (scan.raw_count, scan.read_count) == (2, 0)
        assert scan.detail_coverage is None
        if interruption != "old_root":
            assert set(session.scalars(select(PendingDiscovery.id))) == before
        if interruption == "cancel":
            task = session.scalars(
                select(BackgroundTask).where(BackgroundTask.task_type == "source.continue")
            ).one()
            assert task.status == "cancelled"


def _enqueue_scan(database: Database, workspace_id: str, board: SourceReader) -> Worker:
    """Queue the board scan as its own task so its candidate audit is recorded."""
    if not SearchParametersService(database).current(workspace_id).seniority.target_titles:
        state_target_titles(database, workspace_id, "Operations Analyst")
    TaskQueueService(database).enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={},
        idempotency_key=new_id(),
    )
    return Worker(
        database, worker_id="drain-worker", handlers=default_handlers(database, fetcher=board)
    )


def _board_scan_row(database: Database) -> SourceScan:
    with database.session() as session:
        scan = session.scalars(
            select(SourceScan)
            .where(SourceScan.query_label == "title-filter")
            .order_by(*LATEST_SCAN_FIRST)
        ).first()
        assert scan is not None
        session.expunge(scan)
        return scan


def _withdrawn_urls(database: Database) -> set[str]:
    with database.session() as session:
        return set(
            session.scalars(
                select(PendingDiscovery.discovery_url).where(
                    PendingDiscovery.withdrawn_at.is_not(None)
                )
            )
        )


class _DrainingBoard:
    """Defer part of the candidate set on the first read and finish it on continuation."""

    def __init__(
        self,
        listed: str,
        continued: str,
        deferred: tuple[str, ...],
        read_pages: tuple[str, ...] = (),
        detail: SourceDetailSummary | None = None,
        continued_detail: SourceDetailSummary | None = None,
        skip_reasons: dict[str, int] | None = None,
        held: tuple[str, ...] = (),
        held_reason: str = "stable_http_410",
    ) -> None:
        self.listed = listed
        self.continued = continued
        self.skip_reasons = skip_reasons or {}
        self.deferred = deferred
        self.held = held
        self.held_reason = held_reason
        self.read_pages = read_pages
        self.discovery_truncated = False
        self.detail = detail or SourceDetailSummary()
        self.continued_detail = continued_detail or SourceDetailSummary()
        self.reads = 0
        self.continue_calls = 0

    def listings(
        self,
        _source_name: str,
        _url: str,
        *,
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        self.reads += 1
        return SourceRead(
            listings=[_posting(self.listed)],
            complete=False,
            raw_count=1 + len(self.deferred) + len(self.held) + len(self.read_pages),
            outcome=SourceReadOutcome.CRAWL_CONTINUATION,
            deferred_targets=(
                *(_continuation(url) for url in self.deferred),
                *(
                    replace(
                        _continuation(url),
                        automatic_eligible=False,
                        deferred_reason=self.held_reason,
                    )
                    for url in self.held
                ),
            ),
            attempted_target_urls=(self.listed, *self.read_pages),
            discovery_truncated=self.discovery_truncated,
            exhausted_outcome=(
                SourceReadOutcome.PUBLISHER_INDEX if self.deferred or self.held else None
            ),
            detail=self.detail,
            skip_reasons=self.skip_reasons,
        )

    def continue_listings(
        self,
        _employer: str,
        targets: tuple[SourceContinuationTarget, ...],
        *,
        root_url: str = "",
        handoff_scopes: tuple[tuple[str, str, str], ...] = (),
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        self.continue_calls += 1
        return SourceRead(
            listings=[_posting(self.continued)],
            complete=False,
            raw_count=len(targets),
            outcome=SourceReadOutcome.PUBLISHER_LINKED_PAGES,
            attempted_target_urls=tuple(target.url for target in targets),
            detail=self.continued_detail,
        )


def _assert_recorded_detail_counts(
    database: Database,
    expected: tuple[int, int, int, int | None, int],
    reasons: dict[str, int],
) -> None:
    scan = _board_scan_row(database)
    assert (
        scan.detail_attempted_count,
        scan.detail_read_count,
        scan.detail_failed_count,
        scan.detail_interrupted_count,
        scan.detail_capped_count,
    ) == expected
    assert scan.detail_failure_reasons == reasons


def test_a_paged_host_hold_keeps_results_and_other_sources_then_resumes(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(phenom, "PHENOM_PAGE", 10)
    workspace_id = _workspace(durable_database)
    board_url = "https://careers.acme.test/global/en/home"
    held_source = _source(durable_database, workspace_id, "Acme", board_url)
    other_source = _source(
        durable_database, workspace_id, "Globex", "https://careers.globex.test/global/en/home"
    )
    gone = "https://careers.acme.test/global/en/job/gone"
    _stage_known_rows(durable_database, workspace_id, held_source, (gone,))

    class HeldPages(ScriptedPage):
        held = True

        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            offset = int(body["from"])
            is_held_source = "careers.acme.test" in url
            if self.held and is_held_source and offset == 20:
                raise HostHeldError(url, ready_at=100)
            total = 30 if is_held_source else 1
            host = "careers.acme.test" if is_held_source else "careers.globex.test"
            jobs = [
                {
                    "jobId": f"REQ-{index}",
                    "title": f"Operations Analyst {index}",
                    "applyUrl": f"https://{host}/global/en/job/{index}",
                }
                for index in range(offset, min(offset + 10, total))
            ]
            return {"refineSearch": {"totalHits": total, "data": {"jobs": jobs}}}

    board = HeldPages('<script>phApp.ddo = {"refNum":"ACMEUS"};</script>')
    worker = _enqueue_scan(durable_database, workspace_id, board)
    assert worker.run_once()
    assert not worker.run_once()
    with durable_database.session() as session:
        scans = {scan.source_id: scan for scan in session.scalars(select(SourceScan))}
        held_scan = scans[held_source]
        scan_id = held_scan.id
        assert held_scan.raw_count == held_scan.read_count == 20
        assert not held_scan.complete
        assert held_scan.status == "incomplete"
        assert held_scan.skip_reasons == {"host_held": 1}
        assert "publisher requested a wait" in held_scan.notes
        assert scans[other_source].complete
        assert scans[other_source].raw_count == 1
        assert (
            session.scalar(
                select(PendingDiscovery).where(
                    PendingDiscovery.discovery_url == "https://careers.globex.test/global/en/job/0"
                )
            )
            is not None
        )
        frontier = session.scalars(select(SourceScanFrontierItem)).one()
        assert frontier.status == "pending"
        assert not frontier.automatic_eligible
        assert "offset=20" in frontier.url
    assert _withdrawn_urls(durable_database) == set()

    board.held = False
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=board,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.request_continuation(workspace_id, scan_id)
    assert worker.run_once()
    with durable_database.session() as session:
        scan = session.get(SourceScan, scan_id)
        assert scan is not None
        assert scan.complete
        assert scan.raw_count == scan.read_count == 30
        assert scan.listing_pages == 3
        check = session.scalars(
            select(BackgroundTask).where(BackgroundTask.status == "queued")
        ).one()
        gone_id = session.scalars(
            select(PendingDiscovery.id).where(PendingDiscovery.discovery_url == gone)
        ).one()
        assert check.task_type == "discovery.refresh_batch"
        assert check.payload == {"pending_discovery_ids": [gone_id]}
        frontier = session.scalars(select(SourceScanFrontierItem)).one()
        assert frontier.status == "completed"
        urls = set(session.scalars(select(PendingDiscovery.discovery_url)))
        assert all(
            f"https://careers.acme.test/global/en/job/{index}" in urls for index in range(30)
        )
    assert _withdrawn_urls(durable_database) == set()


def test_a_held_detail_keeps_manual_recovery_without_an_automatic_retry_chain(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(durable_database, workspace_id, "Operations Analyst")

    class HeldDetails:
        continued = 0

        def listings(
            self,
            employer: str,
            url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            def held(_job_id: str) -> Mapping[str, Any] | None:
                raise HostHeldError(url, ready_at=100)

            return BoardApiFetcher().detailed_read(
                employer,
                [{"id": "REQ-100"}],
                mapping=BAMBOOHR_MAPPING,
                detail=held,
                resume=(url, "bamboohr_detail_rows_pending"),
            )

        def continue_listings(
            self,
            _employer: str,
            targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            self.continued += 1
            return SourceRead(
                listings=[],
                complete=False,
                raw_count=0,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                skip_reasons={"host_held": 1},
                deferred_targets=targets,
                attempted_target_urls=(),
            )

    board = HeldDetails()
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=board,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.scan(workspace_id)
    _assert_recorded_detail_counts(durable_database, (0, 0, 0, 0, 0), {})
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        scan_id = scan.id
        frontier = session.scalars(select(SourceScanFrontierItem)).one()
        assert frontier.status == "pending"
        assert not frontier.automatic_eligible
        assert (
            list(
                session.scalars(
                    select(BackgroundTask).where(BackgroundTask.task_type == "source.continue")
                )
            )
            == []
        )
    service.request_continuation(workspace_id, scan_id)
    worker = Worker(
        durable_database,
        worker_id="held-detail-worker",
        handlers=default_handlers(durable_database, fetcher=board),
    )
    assert worker.run_once()
    assert not worker.run_once()
    assert board.continued == 1
    with durable_database.session() as session:
        frontier = session.scalars(select(SourceScanFrontierItem)).one()
        assert frontier.status == "pending"
        assert frontier.attempt_count == 0
    _assert_recorded_detail_counts(durable_database, (0, 0, 0, 0, 0), {})


@pytest.mark.parametrize("repeat_receipt", [False, True], ids=["once", "repeated-write"])
def test_source_detail_counts_retain_the_root_and_sum_each_continuation_chunk(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    repeat_receipt: bool,
) -> None:
    monkeypatch.setattr(continuation_handler, "CONTINUATION_CHUNK_SIZE", 1)
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(durable_database, workspace_id, "Operations Analyst")
    urls = tuple(f"https://jobs.example/globex/detail-{index}" for index in range(3))
    board = _DrainingBoard(
        "https://jobs.example/globex/listed",
        urls[0],
        urls,
        detail=SourceDetailSummary(
            attempted=3,
            read=1,
            failed=1,
            interrupted=1,
            capped=2,
            failure_reasons=(("timeout", 1),),
        ),
        continued_detail=SourceDetailSummary(
            attempted=4,
            read=2,
            failed=1,
            interrupted=1,
            capped=3,
            failure_reasons=(("http_status", 1),),
        ),
    )
    if repeat_receipt:
        original = ScanRecorder.record_detail_read_in_session

        def write_twice(
            self: ScanRecorder,
            scan: SourceScan,
            member: SourceScanMember,
            receipt: str,
            detail: SourceDetailSummary,
            **kwargs: Any,
        ) -> None:
            original(self, scan, member, receipt, detail, **kwargs)
            original(self, scan, member, receipt, detail, **kwargs)

        monkeypatch.setattr(ScanRecorder, "record_detail_read_in_session", write_twice)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=board,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.scan(workspace_id)
    _assert_recorded_detail_counts(durable_database, (3, 1, 1, 1, 2), {"timeout": 1})
    worker = Worker(
        durable_database,
        worker_id="detail-accounting-worker",
        handlers=default_handlers(durable_database, fetcher=board),
    )
    for completed in range(1, 4):
        assert worker.run_once()
        _assert_recorded_detail_counts(
            durable_database,
            (3 + 4 * completed, 1 + 2 * completed, 1 + completed, 1 + completed, 2 + 3 * completed),
            {"timeout": 1, "http_status": completed},
        )
    assert (board.reads, board.continue_calls) == (1, 3)


@pytest.mark.parametrize("action", ["resume", "scan-again"])
def test_resume_preserves_detail_counts_while_a_fresh_scan_starts_a_new_family(
    durable_database: Database,
    action: str,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    listed = "https://jobs.example/globex/listed"
    continued = "https://jobs.example/globex/continued"
    board = _DrainingBoard(
        listed,
        continued,
        (continued,),
        detail=SourceDetailSummary(
            attempted=3,
            read=1,
            failed=1,
            interrupted=1,
            capped=5,
            failure_reasons=(("timeout", 1),),
        ),
        continued_detail=SourceDetailSummary(attempted=1, read=1),
    )
    worker = _enqueue_scan(durable_database, workspace_id, board)
    assert worker.run_once()
    _assert_recorded_detail_counts(durable_database, (3, 1, 1, 1, 5), {"timeout": 1})
    queue = TaskQueueService(durable_database)
    with durable_database.session() as session:
        member = session.scalars(select(SourceScanMember)).one()
        root_id = member.root_task_id
    queue.request_cancellation(root_id)
    if action == "resume":
        queue.resume_source_batch(workspace_id, root_id, "resume-recorded-details")
    else:
        board.detail = SourceDetailSummary(attempted=2, read=2, capped=1)
        queue.enqueue(
            workspace_id=workspace_id,
            task_type="source.scan",
            payload={},
            idempotency_key="scan-again-recorded-details",
        )
    for _ in range(4):
        if not worker.run_once():
            break
    if action == "resume":
        _assert_recorded_detail_counts(durable_database, (4, 2, 1, 1, 5), {"timeout": 1})
        assert board.reads == 1
    else:
        _assert_recorded_detail_counts(durable_database, (3, 3, 0, 0, 1), {})
        assert board.reads == 2
    assert board.continue_calls == 1


def test_continuing_a_historical_scan_retains_unknown_interruptions_and_new_attempts(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    target = "https://jobs.example/globex/continued"
    board = _DrainingBoard(
        "https://jobs.example/globex/listed",
        target,
        (target,),
        continued_detail=SourceDetailSummary(attempted=1, interrupted=1),
    )
    worker = _enqueue_scan(durable_database, workspace_id, board)
    assert worker.run_once()
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        scan.detail_interrupted_count = None
        member = session.scalars(select(SourceScanMember)).one()
        root_id = member.root_task_id
    queue = TaskQueueService(durable_database)
    queue.request_cancellation(root_id)
    queue.resume_source_batch(workspace_id, root_id, "resume-unknown-interruptions")
    for _ in range(4):
        if not worker.run_once():
            break
    _assert_recorded_detail_counts(durable_database, (1, 0, 0, None, 0), {})
    with durable_database.session() as session:
        tasks = list(
            session.scalars(
                select(BackgroundTask)
                .where(BackgroundTask.task_type == "source.continue")
                .order_by(BackgroundTask.created_at)
            )
        )
        frontier = session.scalars(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.normalized_url == target)
        ).one()
    assert tasks[-1].status == "completed"
    assert frontier.status == "completed"
    assert board.continue_calls == 1


def test_completed_detail_metrics_survive_a_staging_lock_and_count_a_real_retry(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(durable_database, workspace_id, "Operations Analyst")
    target = "https://jobs.example/globex/continued"
    board = _DrainingBoard(
        "https://jobs.example/globex/listed",
        target,
        (target,),
        detail=SourceDetailSummary(attempted=1, read=1),
        continued_detail=SourceDetailSummary(attempted=2, read=1, interrupted=1, capped=3),
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=board,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.scan(workspace_id)
    handlers = default_handlers(durable_database, fetcher=board)
    handler = cast(SourceContinuationHandler, handlers["source.continue"])
    worker = Worker(durable_database, worker_id="detail-staging-lock-worker", handlers=handlers)

    def staging_busy(*_args: Any, **_kwargs: Any) -> Any:
        raise StagingBusyError("synthetic staging turn is busy")

    with monkeypatch.context() as staging_failure:
        staging_failure.setattr(handler.sweep.staging, "stage_listings", staging_busy)
        assert worker.run_once()
    _assert_recorded_detail_counts(durable_database, (3, 2, 0, 1, 3), {})
    with durable_database.session() as session:
        task = session.scalars(
            select(BackgroundTask)
            .where(BackgroundTask.task_type == "source.continue")
            .order_by(BackgroundTask.created_at)
        ).first()
        assert task is not None
        completed_task_id = task.id
        row = session.scalars(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.normalized_url == target)
        ).one()
    assert row.status == "pending"
    assert row.attempt_count == 0
    handler.recover_abandoned(completed_task_id)
    handler.recover_abandoned(completed_task_id)
    _assert_recorded_detail_counts(durable_database, (3, 2, 0, 1, 3), {})
    assert worker.run_once()
    _assert_recorded_detail_counts(durable_database, (5, 3, 0, 2, 6), {})
    assert board.continue_calls == 2


def test_a_locked_read_claim_leaves_the_continuation_one_live_successor(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(durable_database, workspace_id, "Operations Analyst")
    target = "https://jobs.example/globex/continued"
    board = _DrainingBoard("https://jobs.example/globex/listed", target, (target,))
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=board,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.scan(workspace_id)
    handlers = default_handlers(durable_database, fetcher=board)
    handler = cast(SourceContinuationHandler, handlers["source.continue"])
    worker = Worker(durable_database, worker_id="claim-lock-worker", handlers=handlers)

    def claim_locked(*_args: Any, **_kwargs: Any) -> bool:
        raise OperationalError("BEGIN IMMEDIATE", {}, Exception("database is locked"))

    with monkeypatch.context() as claim_failure:
        claim_failure.setattr(handler.commands.batch_commands, "claim_source_read", claim_locked)
        assert worker.run_once()
    with durable_database.session() as session:
        statuses = [
            task.status
            for task in session.scalars(
                select(BackgroundTask)
                .where(BackgroundTask.task_type == "source.continue")
                .order_by(BackgroundTask.created_at)
            )
        ]
        row = session.scalars(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.normalized_url == target)
        ).one()
    assert statuses.count("queued") == 1
    assert row.status == "pending"
    assert row.attempt_count == 0
    assert board.continue_calls == 0
    assert worker.run_once()
    assert board.continue_calls == 1


@pytest.mark.parametrize("fresh_before_resume", [False, True], ids=["resume", "superseded"])
def test_a_root_read_committed_before_its_result_is_saved_keeps_its_detail_family(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    fresh_before_resume: bool,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    board = _DrainingBoard(
        "https://jobs.example/globex/listed",
        "https://jobs.example/globex/unused",
        (),
        detail=SourceDetailSummary(
            attempted=3,
            read=1,
            failed=1,
            interrupted=1,
            capped=5,
            failure_reasons=(("timeout", 1),),
        ),
    )
    worker = _enqueue_scan(durable_database, workspace_id, board)
    original = TaskCommands.release_source_read

    def crash_before_result(
        self: TaskCommands,
        member_id: str,
        task_id: str,
        attempt_count: int,
        *,
        result: dict[str, Any] | None = None,
        failed: bool = False,
    ) -> None:
        if result is not None and "board_result" in result:
            raise RuntimeError("synthetic interruption after the scan committed")
        original(self, member_id, task_id, attempt_count, result=result, failed=failed)

    with monkeypatch.context() as interrupted:
        interrupted.setattr(TaskCommands, "release_source_read", crash_before_result)
        assert worker.run_once()
    _assert_recorded_detail_counts(durable_database, (3, 1, 1, 1, 5), {"timeout": 1})
    with durable_database.session() as session:
        member = session.scalars(select(SourceScanMember)).one()
        root_id = member.root_task_id
        assert member.source_scan_id is not None
        assert "board_result" not in member.result
        assert list(session.scalars(select(SourceScanContinuation))) == []
    queue = TaskQueueService(durable_database)
    assert queue.get(root_id).status == "failed"
    board.detail = SourceDetailSummary(attempted=2, read=2, capped=1)
    if fresh_before_resume:
        queue.enqueue(
            workspace_id=workspace_id,
            task_type="source.scan",
            payload={},
            idempotency_key="replace-crashed-detail-family",
        )
        assert worker.run_once()
        _assert_recorded_detail_counts(durable_database, (2, 2, 0, 0, 1), {})
        with pytest.raises(ValidationError, match="scanned again"):
            queue.resume_source_batch(workspace_id, root_id, "superseded-detail-family")
        _assert_recorded_detail_counts(durable_database, (2, 2, 0, 0, 1), {})
    else:
        queue.resume_source_batch(workspace_id, root_id, "resume-crashed-detail-family")
        assert worker.run_once()
        _assert_recorded_detail_counts(durable_database, (5, 3, 1, 1, 6), {"timeout": 1})
    assert (board.reads, board.continue_calls) == (2, 0)


def test_a_drained_traversal_withdraws_the_openings_it_no_longer_lists(
    durable_database: Database,
) -> None:
    """Absence across a whole drained traversal is evidence a single bounded read never had."""
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    listed = "https://jobs.example/globex/listed"
    continued = "https://jobs.example/globex/continued"
    blank = "https://jobs.example/globex/blank"
    first_blank = "https://jobs.example/globex/first-read-blank"
    gone = "https://jobs.example/globex/gone"
    _stage_known_rows(
        durable_database, workspace_id, source_id, (listed, continued, blank, first_blank, gone)
    )

    worker = _enqueue_scan(
        durable_database,
        workspace_id,
        _DrainingBoard(listed, continued, (continued, blank), (first_blank,)),
    )
    for _ in range(6):
        if not worker.run_once():
            break

    assert _withdrawn_urls(durable_database) == {gone}
    scan = _board_scan_row(durable_database)
    assert scan.complete is True
    assert scan.read_outcome == SourceReadOutcome.PUBLISHER_INDEX.value
    with durable_database.session() as session:
        pulled_id = session.scalars(
            select(PendingDiscovery.id).where(PendingDiscovery.discovery_url == gone)
        ).one()
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "pending_discovery.withdrawn_absent")
        ).all()
    assert [event.entity_id for event in events] == [pulled_id]
    assert _withdrawing_read(events[0]) == (4, 1, True)


@pytest.mark.parametrize("refusal", ["board_api_access_refused", "board_api_policy_refused"])
def test_a_drained_traversal_after_a_refused_board_api_withdraws_nothing(
    durable_database: Database, refusal: str
) -> None:
    """A publisher page read in place of a refused board API cannot speak for the board."""
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    listed = "https://jobs.example/globex/listed"
    continued = "https://jobs.example/globex/continued"
    gone = "https://jobs.example/globex/gone"
    _stage_known_rows(durable_database, workspace_id, source_id, (listed, continued, gone))

    worker = _enqueue_scan(
        durable_database,
        workspace_id,
        _DrainingBoard(listed, continued, (continued,), skip_reasons={refusal: 1}),
    )
    for _ in range(6):
        if not worker.run_once():
            break

    assert _withdrawn_urls(durable_database) == set()
    scan = _board_scan_row(durable_database)
    assert not scan.complete
    assert scan.status == "incomplete"


@pytest.mark.parametrize("reason", ["stable_http_404", "stable_http_410"])
@pytest.mark.parametrize("continued", [True, False], ids=["after-continuation", "at-record"])
def test_a_traversal_leaving_only_person_held_links_certifies_and_keeps_their_openings(
    durable_database: Database, continued: bool, reason: str
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    listed = "https://jobs.example/globex/listed"
    continued_url = "https://jobs.example/globex/continued"
    held = "https://jobs.example/globex/held"
    gone = "https://jobs.example/globex/gone"
    _stage_known_rows(durable_database, workspace_id, source_id, (listed, held, gone))

    worker = _enqueue_scan(
        durable_database,
        workspace_id,
        _DrainingBoard(
            listed,
            continued_url,
            (continued_url,) if continued else (),
            held=(held,),
            held_reason=reason,
        ),
    )
    for _ in range(6):
        if not worker.run_once():
            break

    scan = _board_scan_row(durable_database)
    assert scan.complete is True
    assert MANUAL_RECOVERY_NOTE not in scan.notes
    assert _withdrawn_urls(durable_database) == {gone}
    with durable_database.session() as session:
        row = session.scalars(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.url == held)
        ).one()
        assert (row.status, row.automatic_eligible) == ("pending", False)


@pytest.mark.parametrize("status", [404, 410])
def test_a_publisher_gone_answer_read_through_traversal_certifies_the_drained_scan(
    durable_database: Database, status: int
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    listed = "https://jobs.example/globex/listed"
    held = "https://jobs.example/globex/roles/held"
    gone = "https://jobs.example/globex/gone"
    _stage_known_rows(durable_database, workspace_id, source_id, (listed, held, gone))
    request = httpx.Request("GET", held)
    failure = httpx.HTTPStatusError(
        "gone", request=request, response=httpx.Response(status, request=request)
    )
    publisher_read = PublisherSourceFetcher(
        client=cast(Any, _ScriptedPublisherClient({held: failure}))
    ).read(
        "Globex",
        "https://jobs.example/globex",
        '<main><a href="/globex/roles/held">Held Role</a></main>',
    )

    class GoneLinkBoard(_DrainingBoard):
        def listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            return replace(
                super().listings(*args, **kwargs),
                deferred_targets=publisher_read.deferred_targets,
                exhausted_outcome=SourceReadOutcome.PUBLISHER_INDEX,
            )

    worker = _enqueue_scan(
        durable_database, workspace_id, GoneLinkBoard(listed, f"{listed}/unused", ())
    )
    for _ in range(6):
        if not worker.run_once():
            break

    scan = _board_scan_row(durable_database)
    assert scan.complete is True
    assert _withdrawn_urls(durable_database) == {gone}
    with durable_database.session() as session:
        row = session.scalars(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.url == held)
        ).one()
        assert (row.status, row.automatic_eligible, row.deferred_reason) == (
            "pending",
            False,
            f"stable_http_{status}",
        )


@pytest.mark.parametrize(
    "reason",
    ["stable_http_401", "stable_http_403", "policy_refused", "invalid_publisher_response"],
)
@pytest.mark.parametrize("continued", [True, False], ids=["after-continuation", "at-record"])
def test_a_collection_link_held_unread_keeps_the_scan_open_and_its_postings_staged(
    durable_database: Database, continued: bool, reason: str
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    listed = "https://jobs.example/globex/listed"
    continued_url = "https://jobs.example/globex/continued"
    collection = "https://jobs.example/globex/jobs/pharmacy"
    child = "https://jobs.example/globex/jobs/pharmacy/clinical-pharmacist"
    _stage_known_rows(durable_database, workspace_id, source_id, (listed, child))

    for _scan in range(2):
        worker = _enqueue_scan(
            durable_database,
            workspace_id,
            _DrainingBoard(
                listed,
                continued_url,
                (continued_url,) if continued else (),
                held=(collection,),
                held_reason=reason,
            ),
        )
        for _ in range(6):
            if not worker.run_once():
                break

        assert _withdrawn_urls(durable_database) == set()
    with durable_database.session() as session:
        scans = session.scalars(select(SourceScan)).all()
    assert scans and not any(scan.complete for scan in scans)


class _FailingContinuationBoard(_DrainingBoard):
    """Defer one automatic link whose every continuation read fails."""

    def continue_listings(
        self,
        _employer: str,
        targets: tuple[SourceContinuationTarget, ...],
        *,
        root_url: str = "",
        handoff_scopes: tuple[tuple[str, str, str], ...] = (),
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        self.continue_calls += 1
        raise RuntimeError("synthetic publisher outage")


def test_a_failed_link_a_retry_can_resolve_still_blocks_certification(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    listed = "https://jobs.example/globex/listed"
    failing = "https://jobs.example/globex/failing"
    held = "https://jobs.example/globex/held"
    gone = "https://jobs.example/globex/gone"
    _stage_known_rows(durable_database, workspace_id, source_id, (listed, gone))

    board = _FailingContinuationBoard(listed, failing, (failing,), held=(held,))
    worker = _enqueue_scan(durable_database, workspace_id, board)
    for _ in range(12):
        if not worker.run_once():
            break

    assert board.continue_calls == MAX_AUTOMATIC_ATTEMPTS
    with durable_database.session() as session:
        statuses = {row.url: row.status for row in session.scalars(select(SourceScanFrontierItem))}
    assert (statuses[failing], statuses[held]) == ("failed", "pending")
    scan = _board_scan_row(durable_database)
    assert not scan.complete
    assert scan.status == "incomplete"
    assert MANUAL_RECOVERY_NOTE in scan.notes
    assert _withdrawn_urls(durable_database) == set()


def test_a_drained_listed_traversal_keeps_a_posting_robots_disallows(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Acme", _ICIMS_ROOT)
    state_target_titles(durable_database, workspace_id, "Data Engineer")
    _stage_known_rows(durable_database, workspace_id, source_id, (_ICIMS_ANALYST,))
    pages = _icims_pages()
    engineer_reads: list[str] = []

    def answer(url: str) -> httpx.Response:
        request = httpx.Request("GET", url)
        if url == _ICIMS_ENGINEER:
            engineer_reads.append(url)
            if len(engineer_reads) == 1:
                return httpx.Response(503, request=request)
        if url not in pages:
            return httpx.Response(404, request=request)
        return httpx.Response(200, request=request, text=pages[url])

    robots = _ICIMS_ROBOTS + "Disallow: /jobs/7001/\n"
    board = BoardApiFetcher(client=policy_client(answer, robots=robots))
    worker = _enqueue_scan(durable_database, workspace_id, board)
    for _ in range(8):
        if not worker.run_once():
            break

    assert len(engineer_reads) == 2
    assert _ICIMS_ANALYST not in _withdrawn_urls(durable_database)
    assert _board_scan_row(durable_database).complete is False


def test_a_continued_icims_chunk_of_protected_postings_proves_an_empty_body_set() -> None:
    pages = _icims_pages()
    requested: list[str] = []
    stopped = [False]

    def answer(url: str) -> httpx.Response:
        requested.append(url)
        stopped[0] |= url == _ICIMS_ANALYST
        if url not in pages:
            return httpx.Response(404, request=httpx.Request("GET", url))
        return httpx.Response(200, request=httpx.Request("GET", url), text=pages[url])

    class Board(BoardApiFetcher):
        @staticmethod
        def deadline_elapsed() -> bool:
            return stopped[0]

    board = Board(client=policy_client(answer, robots=_ICIMS_ROBOTS))
    [target] = board.listings("Acme", _ICIMS_ROOT).deferred_targets
    assert target.url == _ICIMS_ENGINEER
    stopped[0] = False
    requested.clear()

    read = board.continue_listings(
        "Acme",
        (target,),
        root_url=_ICIMS_ROOT,
        handoff_scopes=(),
        options=SourceReadOptions(protected_posting_urls=frozenset({_ICIMS_ENGINEER})),
    )

    assert _ICIMS_ENGINEER not in requested
    assert read.protected_posting_urls == (_ICIMS_ENGINEER,)
    assert read.detail_read_urls == ()


@pytest.mark.parametrize("cut", ["failed_body", "time_limit"])
def test_a_drained_icims_listing_counts_continued_bodies_and_certifies_its_listing(
    durable_database: Database, cut: str
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Acme", _ICIMS_ROOT)
    state_target_titles(durable_database, workspace_id, "Data Engineer")
    unlisted = "https://careers-acme.icims.com/jobs/7009/job?in_iframe=1"
    _stage_known_rows(durable_database, workspace_id, source_id, (_ICIMS_ANALYST, unlisted))
    pages = _icims_pages()
    engineer_reads: list[str] = []

    stopped = [False]

    def answer(url: str) -> httpx.Response:
        request = httpx.Request("GET", url)
        if url == _ICIMS_ANALYST and cut == "time_limit" and not engineer_reads:
            stopped[0] = True
        if url == _ICIMS_ENGINEER:
            engineer_reads.append(url)
            if len(engineer_reads) == 1 and cut == "failed_body":
                return httpx.Response(503, request=request)
        if url not in pages:
            return httpx.Response(404, request=request)
        return httpx.Response(200, request=request, text=pages[url])

    class Board(BoardApiFetcher):
        @staticmethod
        def deadline_elapsed() -> bool:
            return stopped[0]

        def continue_listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            stopped[0] = False
            return super().continue_listings(*args, **kwargs)

    board = Board(client=policy_client(answer, robots=_ICIMS_ROBOTS))
    worker = _enqueue_scan(durable_database, workspace_id, board)
    for _ in range(8):
        if not worker.run_once():
            break

    scan = _board_scan_row(durable_database)
    assert len(engineer_reads) == (2 if cut == "failed_body" else 1)
    assert (scan.raw_count, scan.read_count) == (2, 2)
    assert scan.complete is True
    assert scan.status != "incomplete"
    assert _withdrawn_urls(durable_database) == {unlisted}


def _drain_icims_scan(
    database: Database,
    workspace_id: str,
    requested: list[str],
    *,
    failing: frozenset[str] = frozenset(),
    fail_once: set[str] | None = None,
    payload: dict[str, Any] | None = None,
) -> SourceScan:
    """Queue one iCIMS scan task, drain it, and return the stored scan row.

    A ``fail_once`` page answers 503 to its first request only.
    """
    pages = _icims_pages()
    once = fail_once if fail_once is not None else set()

    def answer(url: str) -> httpx.Response:
        requested.append(url)
        request = httpx.Request("GET", url)
        if url in failing or url in once:
            once.discard(url)
            return httpx.Response(503, request=request)
        if url not in pages:
            return httpx.Response(404, request=request)
        return httpx.Response(200, request=request, text=pages[url])

    board = BoardApiFetcher(client=policy_client(answer, robots=_ICIMS_ROBOTS))
    TaskQueueService(database).enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload=payload or {},
        idempotency_key=new_id(),
    )
    worker = Worker(
        database, worker_id="drain-worker", handlers=default_handlers(database, fetcher=board)
    )
    for _ in range(10):
        if not worker.run_once():
            break
    return _board_scan_row(database)


@pytest.mark.parametrize("fresh", [False, True], ids=["rescan", "fresh-details"])
def test_a_same_day_icims_rescan_credits_recent_bodies_without_reading_them_again(
    durable_database: Database, fresh: bool
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", _ICIMS_ROOT)
    state_target_titles(durable_database, workspace_id, "Data Engineer")
    morning: list[str] = []
    # A clean read leaves no frontier, so the morning defers one body to a continuation.
    assert (
        _drain_icims_scan(
            durable_database, workspace_id, morning, fail_once={_ICIMS_ENGINEER}
        ).complete
        is True
    )
    assert morning.count(_ICIMS_ENGINEER) == 2

    requested: list[str] = []
    if fresh:
        scan = _drain_icims_scan(
            durable_database, workspace_id, requested, payload={"fresh_details": True}
        )
        assert {_ICIMS_ANALYST, _ICIMS_ENGINEER} <= set(requested)
    else:
        scan = _drain_icims_scan(durable_database, workspace_id, requested)
        assert _ICIMS_ANALYST not in requested
        assert _ICIMS_ENGINEER not in requested
    assert (scan.raw_count, scan.read_count, scan.detail_capped_count) == (2, 2, 0)
    assert "time_limit_reached" not in scan.skip_reasons
    assert scan.complete is True


def test_a_rescan_reads_again_a_posting_whose_last_body_read_failed(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", _ICIMS_ROOT)
    state_target_titles(durable_database, workspace_id, "Data Engineer")
    failing = frozenset({_ICIMS_ENGINEER})
    _drain_icims_scan(durable_database, workspace_id, [], failing=failing)

    requested: list[str] = []
    scan = _drain_icims_scan(durable_database, workspace_id, requested, failing=failing)

    assert _ICIMS_ANALYST not in requested
    assert _ICIMS_ENGINEER in requested
    assert scan.read_count == 1
    assert scan.complete is False


def test_a_newer_failed_body_read_ends_an_older_completed_reads_skip_credit(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Acme", _ICIMS_ROOT)
    state_target_titles(durable_database, workspace_id, "Data Engineer")
    completed_earlier_scan(
        durable_database, workspace_id, source_id, (_ICIMS_ANALYST, _ICIMS_ENGINEER), day=-2
    )
    later_scan = completed_earlier_scan(durable_database, workspace_id, source_id, ())
    with durable_database.session() as session:
        later = session.scalars(
            select(SourceScanContinuation).where(
                SourceScanContinuation.source_scan_id == later_scan
            )
        ).one()
        seed_frontier_rows(
            session,
            later.id,
            [
                {
                    "url": _ICIMS_ENGINEER,
                    "status": "failed",
                    "attempt_count": 1,
                    "last_error": "The publisher answered HTTP 503.",
                }
            ],
        )

    with durable_database.session() as session:
        recent = recent_root_posting_reads(session, workspace_id, source_id, None)

    assert _ICIMS_ANALYST in recent
    assert _ICIMS_ENGINEER not in recent


def test_final_detail_membership_withdraws_a_row_removed_between_slices(
    durable_database: Database,
) -> None:
    root = "https://globex.applytojob.com/apply"
    earlier = f"{root}/Earlier123"
    current = f"{root}/Current123"
    removed = f"{root}/Removed123"
    cursor = f"{root}?clearscout_detail_cursor=remaining"
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", root)
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    _stage_known_rows(durable_database, workspace_id, source_id, (earlier, current, removed))

    class ChangingBoard(_DrainingBoard):
        def listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            read = super().listings(*args, **kwargs)
            return replace(read, skip_reasons={"time_limit_reached": 1})

        def continue_listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            read = super().continue_listings(*args, **kwargs)
            return replace(
                read,
                detail_coverage=SourceDetailCoverage(
                    platform="jazzhr", board_url=root, posting_urls=(current,)
                ),
            )

    worker = _enqueue_scan(
        durable_database, workspace_id, ChangingBoard(earlier, current, (cursor,))
    )
    assert worker.run_once()
    assert not _withdrawn_urls(durable_database)
    assert worker.run_once()
    assert _withdrawn_urls(durable_database) == {earlier, removed}
    assert _board_scan_row(durable_database).complete is True


def test_failed_staging_cannot_advance_a_detail_checkpoint(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = "https://globex.applytojob.com/apply"
    initial_cursor = f"{root}?detail_checkpoint=initial"
    next_cursor = f"{root}?detail_checkpoint=next"
    earlier = f"{root}/Earlier123"
    continued = f"{root}/Current123"
    checkpoint = SourceDetailCheckpoint(
        platform="jazzhr",
        board_url=root,
        completed=(("earlier", earlier),),
        pending_ids=("current", "remaining"),
    )
    initial_target = replace(_continuation(initial_cursor), detail_checkpoint=checkpoint)
    successor = replace(
        _continuation(next_cursor),
        detail_checkpoint=replace(
            checkpoint,
            completed=(*checkpoint.completed, ("current", continued)),
            pending_ids=("remaining",),
        ),
    )

    class AdvancingBoard(_DrainingBoard):
        def listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            return replace(super().listings(*args, **kwargs), deferred_targets=(initial_target,))

        def continue_listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            assert args[1][0].detail_checkpoint == checkpoint
            return replace(
                super().continue_listings(*args, **kwargs),
                deferred_targets=(successor,),
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                exhausted_outcome=SourceReadOutcome.PUBLISHER_INDEX,
            )

    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", root)
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    worker = _enqueue_scan(
        durable_database, workspace_id, AdvancingBoard(earlier, continued, (initial_cursor,))
    )
    assert worker.run_once()
    handler = cast(SourceContinuationHandler, worker.handlers["source.continue"])

    def failed_staging(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Synthetic retained artifact could not be staged")

    with monkeypatch.context() as failure:
        failure.setattr(handler.sweep, "stage_continuation", failed_staging)
        assert worker.run_once()
    with durable_database.session() as session:
        rows = session.scalars(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.status == "pending")
        ).all()
        assert [row.url for row in rows] == [initial_cursor]
        assert frontier_target(rows[0]).detail_checkpoint == checkpoint
        assert "staging" in rows[0].last_error
    assert worker.run_once()
    assert _board_scan_row(durable_database).complete is False
    with durable_database.session() as session:
        assert (
            session.scalar(
                select(PendingDiscovery.id).where(PendingDiscovery.discovery_url == continued)
            )
            is not None
        )


@pytest.mark.parametrize("stated_total", [0, 20], ids=["unknown-total", "positive-total"])
@pytest.mark.parametrize("empty_on_retry", [False, True], ids=["same-records", "empty-retry"])
@pytest.mark.parametrize("mixed", [False, True], ids=["unmapped-page", "mixed-page"])
@pytest.mark.parametrize(
    "first_page_readable", [False, True], ids=["initial-unreadable", "later-unreadable"]
)
def test_unreadable_phenom_pages_remain_failed_without_withdrawing_and_recover_on_rescan(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    stated_total: int,
    first_page_readable: bool,
    empty_on_retry: bool,
    mixed: bool,
) -> None:
    monkeypatch.setattr(phenom, "PHENOM_PAGE", 10)
    monkeypatch.setattr(phenom, "PHENOM_MAX_PAGES", 1)
    workspace_id = _workspace(durable_database)
    board_url = "https://careers.acme.test/global/en/home"
    source_id = _source(durable_database, workspace_id, "Acme", board_url)
    gone = "https://careers.acme.test/global/en/job/gone"
    _stage_known_rows(durable_database, workspace_id, source_id, (gone,))

    class ScriptedPhenom(ScriptedPage):
        recovered = False

        def __init__(self, page: str) -> None:
            super().__init__(page)
            self.unreadable_requests: dict[int, int] = {}

        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            offset = int(body["from"])
            readable = self.recovered or (first_page_readable and offset == 0)
            attempts = self.unreadable_requests.get(offset, 0)
            if not readable:
                self.unreadable_requests[offset] = attempts + 1
            jobs = [
                {
                    "jobId": f"REQ-{index}",
                    "title": f"Operations Analyst {index}",
                    **(
                        {"applyUrl": f"https://careers.acme.test/global/en/job/{index}"}
                        if readable or (mixed and index == offset)
                        else {}
                    ),
                }
                for index in range(offset, min(offset + 10, 20))
            ]
            if empty_on_retry and not readable and attempts:
                jobs = []
            return {"refineSearch": {"totalHits": stated_total, "data": {"jobs": jobs}}}

    board = ScriptedPhenom('<script>phApp.ddo = {"refNum":"ACMEUS"};</script>')
    worker = _enqueue_scan(durable_database, workspace_id, board)
    for _ in range(7):
        if not worker.run_once():
            break

    scan = _board_scan_row(durable_database)
    assert scan.complete is False
    assert scan.status == "incomplete"
    assert _withdrawn_urls(durable_database) == set()
    with durable_database.session() as session:
        failed = session.scalars(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.status == "failed")
        ).all()
        assert len(failed) == 1
        assert failed[0].attempt_count == MAX_AUTOMATIC_ATTEMPTS
        assert "posting link" in failed[0].last_error
        if mixed:
            readable_index = 0 if not first_page_readable else 10
            kept = session.scalar(
                select(PendingDiscovery).where(
                    PendingDiscovery.discovery_url
                    == f"https://careers.acme.test/global/en/job/{readable_index}"
                )
            )
            assert kept is not None
            assert kept.withdrawn_at is None
    if not first_page_readable:
        assert scan.exhausted_outcome is None

    board.recovered = True
    _missed_once(durable_database, gone)
    worker = _enqueue_scan(durable_database, workspace_id, board)
    for _ in range(7):
        if not worker.run_once():
            break

    assert _board_scan_row(durable_database).complete is (stated_total > 0)
    assert _withdrawn_urls(durable_database) == ({gone} if stated_total else set())
    if not stated_total:
        with durable_database.session() as session:
            pending = session.scalars(
                select(SourceScanFrontierItem).where(SourceScanFrontierItem.status == "pending")
            ).all()
            assert pending and all(not row.automatic_eligible for row in pending)


def test_the_continuation_task_that_drains_the_frontier_records_no_handoff(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    listed = "https://jobs.example/globex/listed"
    continued = "https://jobs.example/globex/continued"
    worker = _enqueue_scan(
        durable_database, workspace_id, _DrainingBoard(listed, continued, (continued,))
    )
    for _ in range(4):
        if not worker.run_once():
            break

    with durable_database.session() as session:
        [task] = session.scalars(
            select(BackgroundTask).where(BackgroundTask.task_type == "source.continue")
        ).all()
        counts = dict(task.progress)
    assert (task.status, counts["pending"]) == ("completed", 0)


class _ProgressWatchingBoard(_DrainingBoard):
    """Record the running continuation task's stored progress while its links are read."""

    def __init__(self, database: Database, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.database = database
        self.seen: list[dict[str, Any]] = []

    def continue_listings(
        self,
        _employer: str,
        targets: tuple[SourceContinuationTarget, ...],
        *,
        root_url: str = "",
        handoff_scopes: tuple[tuple[str, str, str], ...] = (),
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        with self.database.session() as session:
            [running] = session.scalars(
                select(BackgroundTask).where(BackgroundTask.task_type == "source.continue")
            ).all()
            self.seen.append(dict(running.progress))
        return super().continue_listings(
            _employer,
            targets,
            root_url=root_url,
            handoff_scopes=handoff_scopes,
            employer_is_authoritative=employer_is_authoritative,
        )


def test_a_running_continuation_shows_the_links_read_and_queued_for_its_source(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    continued = "https://jobs.example/globex/continued"
    board = _ProgressWatchingBoard(
        durable_database,
        "https://jobs.example/globex/listed",
        continued,
        (continued,),
        read_pages=("https://jobs.example/globex/page/1", "https://jobs.example/globex/page/2"),
    )
    worker = _enqueue_scan(durable_database, workspace_id, board)
    for _ in range(4):
        if not worker.run_once():
            break

    # The scan read the listed posting and both pages, and left one link queued.
    [progress] = board.seen
    assert (progress["completed"], progress["pending"], progress["count"]) == (3, 1, 1)


class _ScreenedBatchBoard(_DrainingBoard):
    """Return one title match, one title miss, and one market refusal on continuation."""

    def continue_listings(
        self,
        _employer: str,
        targets: tuple[SourceContinuationTarget, ...],
        *,
        root_url: str = "",
        handoff_scopes: tuple[tuple[str, str, str], ...] = (),
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        return SourceRead(
            listings=[
                replace(_posting(self.continued), location="Toronto, Ontario, Canada"),
                SourceListing(
                    employer="Globex",
                    title="Pastry Chef",
                    url="https://jobs.example/globex/pastry-chef",
                    location="Toronto, Ontario, Canada",
                ),
                replace(
                    _posting("https://jobs.example/globex/london"),
                    location="London, United Kingdom",
                    facts=StagedListingFacts(
                        locations=PublishedFact(
                            values=("London, United Kingdom",),
                            provenance="lever.location",
                            complete=True,
                        )
                    ),
                ),
            ],
            complete=False,
            raw_count=3,
            outcome=SourceReadOutcome.PUBLISHER_LINKED_PAGES,
            attempted_target_urls=tuple(target.url for target in targets),
        )


def test_a_continuation_batch_reports_every_opening_it_read_in_one_bucket(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_single_market(
        durable_database, workspace_id, "Data Platform Lead", key="canada", country="Canada"
    )
    listed = "https://jobs.example/globex/listed"
    continued = "https://jobs.example/globex/continued"
    worker = _enqueue_scan(
        durable_database, workspace_id, _ScreenedBatchBoard(listed, continued, (continued,))
    )
    for _ in range(4):
        if not worker.run_once():
            break

    with durable_database.session() as session:
        [task] = session.scalars(
            select(BackgroundTask).where(BackgroundTask.task_type == "source.continue")
        ).all()
        counts = dict(task.progress)
    buckets = (counts["matched"], counts["no_title_match"], counts["refused"])
    assert (counts["read"], buckets) == (3, (1, 1, 1))


class _PublisherNamingBoard:
    """Defer one page, then map its record through a board declaration on continuation."""

    record: ClassVar[dict[str, str]] = {
        "JobDetailsUrl": "https://www.dayforcehcm.com/job/1",
        "Title": "Data Platform Lead",
        "CompanyName": "Globex",
    }

    def __init__(self, deferred: str) -> None:
        self.deferred = deferred

    def listings(
        self,
        _source_name: str,
        _url: str,
        *,
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        """Report the whole candidate set as deferred so the continuation does the reading."""
        return SourceRead(
            listings=[],
            complete=False,
            raw_count=1,
            outcome=SourceReadOutcome.CRAWL_CONTINUATION,
            deferred_targets=(_continuation(self.deferred),),
        )

    def continue_listings(
        self,
        employer: str,
        targets: tuple[SourceContinuationTarget, ...],
        *,
        root_url: str = "",
        handoff_scopes: tuple[tuple[str, str, str], ...] = (),
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        """Return the one mapped listing this synthetic Dayforce page states."""
        return SourceRead(
            listings=source_listings(
                DAYFORCE_MAPPING,
                [self.record],
                employer=employer,
                employer_is_authoritative=employer_is_authoritative,
            ),
            complete=False,
            raw_count=1,
            outcome=SourceReadOutcome.PUBLISHER_LINKED_PAGES,
            attempted_target_urls=tuple(target.url for target in targets),
        )


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        pytest.param("api", "Northwind", id="hand-named"),
        pytest.param("search.guided_campaign", "Globex", id="campaign-named"),
    ],
)
def test_only_a_hand_named_board_keeps_its_configured_employer_on_a_continuation_page(
    durable_database: Database, origin: str, expected: str
) -> None:
    """A later page of a typed board is renamed as silently as its first page was."""
    workspace_id = _workspace(durable_database)
    _source(
        durable_database,
        workspace_id,
        "Northwind",
        "https://www.dayforcehcm.com/northwind",
        origin=origin,
    )
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")

    worker = _enqueue_scan(
        durable_database,
        workspace_id,
        _PublisherNamingBoard("https://www.dayforcehcm.com/northwind/page-2"),
    )
    for _ in range(4):
        if not worker.run_once():
            break

    with durable_database.session() as session:
        employers = session.scalars(select(PendingDiscovery.employer)).all()
    assert list(employers) == [expected]


class _AlreadyDrainedBoard:
    """Defer only targets a prior continuation already read, so the scan drains as it records."""

    def __init__(self, listed: str, deferred: str, read_pages: tuple[str, ...] = ()) -> None:
        self.listed = listed
        self.deferred = deferred
        self.read_pages = read_pages

    def listings(
        self,
        _source_name: str,
        _url: str,
        *,
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        return SourceRead(
            listings=[_posting(self.listed)],
            complete=False,
            raw_count=2 + len(self.read_pages),
            outcome=SourceReadOutcome.CRAWL_CONTINUATION,
            deferred_targets=(_continuation(self.deferred),),
            attempted_target_urls=(self.listed, *self.read_pages),
            exhausted_outcome=SourceReadOutcome.PUBLISHER_INDEX,
        )

    def continue_listings(
        self,
        _employer: str,
        targets: tuple[SourceContinuationTarget, ...],
        *,
        root_url: str = "",
        handoff_scopes: tuple[tuple[str, str, str], ...] = (),
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        raise AssertionError("a frontier drained at scan time needs no continuation read")


def test_a_frontier_already_drained_when_the_scan_records_withdraws_what_it_no_longer_lists(
    durable_database: Database,
) -> None:
    """A scan whose every deferred target was already read is complete without a continuation."""
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    listed = "https://jobs.example/globex/listed"
    deferred = "https://jobs.example/globex/already-read"
    blank = "https://jobs.example/globex/blank"
    gone = "https://jobs.example/globex/gone"
    _seed_continuation(
        durable_database,
        workspace_id,
        _scan_row(durable_database, source_id, day=-1),
        [{"url": deferred, "status": "completed", "deferred_reason": POSTING_DETAIL_COMPLETED}],
    )
    _stage_known_rows(durable_database, workspace_id, source_id, (listed, deferred, blank, gone))

    worker = _enqueue_scan(
        durable_database, workspace_id, _AlreadyDrainedBoard(listed, deferred, (blank,))
    )
    for _ in range(6):
        if not worker.run_once():
            break

    assert _withdrawn_urls(durable_database) == {gone}
    scan = _board_scan_row(durable_database)
    assert scan.complete is True
    assert scan.read_outcome == SourceReadOutcome.PUBLISHER_INDEX.value


class _DeadlineCutBoard:
    """Read one listed posting body and defer the rest, `capped` of them cut by the deadline."""

    def __init__(
        self, listed: str, deferred: tuple[str, ...], capped: int, cursors: tuple[str, ...] = ()
    ) -> None:
        self.listed = listed
        self.deferred = deferred
        self.capped = capped
        self.cursors = cursors

    def listings(
        self,
        _source_name: str,
        _url: str,
        *,
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        rows = 1 + len(self.deferred)
        return SourceRead(
            listings=[_posting(self.listed)],
            complete=False,
            raw_count=rows,
            outcome=SourceReadOutcome.CRAWL_CONTINUATION,
            deferred_targets=(
                *(
                    replace(_continuation(url), deferred_reason="successfactors_posting_pending")
                    for url in self.deferred
                ),
                *(
                    replace(
                        _continuation(url),
                        deferred_reason="successfactors_search_page",
                        automatic_eligible=False,
                    )
                    for url in self.cursors
                ),
            ),
            attempted_target_urls=(self.listed,),
            exhausted_outcome=SourceReadOutcome.PUBLISHER_INDEX,
            note=deadline_cut_note(self.capped, rows, "successfactors") if self.capped else "",
            detail=SourceDetailSummary(attempted=1, read=1, capped=self.capped),
            skip_reasons={"time_limit_reached": 1} if self.capped else {},
        )

    def continue_listings(
        self,
        _employer: str,
        targets: tuple[SourceContinuationTarget, ...],
        *,
        root_url: str = "",
        handoff_scopes: tuple[tuple[str, str, str], ...] = (),
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        return SourceRead(
            listings=[_posting(target.url) for target in targets],
            complete=False,
            raw_count=len(targets),
            outcome=SourceReadOutcome.PUBLISHER_LINKED_PAGES,
            attempted_target_urls=tuple(target.url for target in targets),
        )


def _run_scan(database: Database, workspace_id: str, board: SourceReader) -> tuple[SourceScan, str]:
    """Scan once and return the stored row with the note the scan result reported."""
    result = SourceScanService(
        database,
        storage=runtime_storage(database),
        fetcher=board,
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)
    return _board_scan_row(database), result["scanned"][0]["note"]


@pytest.mark.parametrize(
    ("deferred", "board_capped", "already_read", "capped", "read_count"),
    [
        (("cut-0", "cut-1", "cut-2"), 3, ("cut-0", "cut-1", "cut-2"), 0, 4),
        (("cut-0", "cut-1", "cut-2"), 3, ("cut-0", "cut-1"), 1, 3),
        # A failed row defers beside the cut ones, so its prior read leaves cut-1 owed.
        (("cut-0", "cut-1", "failed-0"), 2, ("cut-0", "failed-0"), 1, 3),
        # Nothing was cut, yet bodies a completed read holds still count as read.
        (("cut-0", "cut-1", "cut-2"), 0, ("cut-0", "cut-1", "cut-2"), 0, 4),
    ],
)
def test_deadline_cut_postings_another_continuation_already_read_are_not_left_capped(
    durable_database: Database,
    deferred: tuple[str, ...],
    board_capped: int,
    already_read: tuple[str, ...],
    capped: int,
    read_count: int,
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    base = "https://jobs.example/globex/"
    _seed_continuation(
        durable_database,
        workspace_id,
        _scan_row(durable_database, source_id, day=-1),
        [
            {"url": base + name, "status": "completed", "deferred_reason": POSTING_DETAIL_COMPLETED}
            for name in already_read
        ],
    )

    scan, reported_note = _run_scan(
        durable_database,
        workspace_id,
        _DeadlineCutBoard(base + "listed", tuple(base + name for name in deferred), board_capped),
    )

    settled = capped == 0
    assert (scan.detail_capped_count, scan.read_count, scan.detail_read_count) == (
        capped,
        read_count,
        1,
    )
    assert ("time_limit_reached" in scan.skip_reasons) is not settled
    expected_note = "" if settled else deadline_cut_note(capped, 4, "successfactors")
    assert scan.notes.startswith(expected_note)
    assert reported_note.startswith(expected_note)
    assert ("read deadline cut off" in reported_note) is not settled
    assert ("read deadline cut off" in scan.notes) is not settled
    assert scan.complete is settled


def test_a_pending_search_cursor_keeps_the_deadline_claim_after_every_cut_posting_is_credited(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    base = "https://jobs.example/globex/"
    cut = tuple(f"{base}cut-{index}" for index in range(3))
    _seed_continuation(
        durable_database,
        workspace_id,
        _scan_row(durable_database, source_id, day=-1),
        [
            {"url": url, "status": "completed", "deferred_reason": POSTING_DETAIL_COMPLETED}
            for url in cut
        ],
    )

    scan, _ = _run_scan(
        durable_database,
        workspace_id,
        _DeadlineCutBoard(base + "listed", cut, 3, cursors=(base + "search?page=2",)),
    )

    assert (scan.detail_capped_count, scan.read_count) == (0, 4)
    assert "time_limit_reached" in scan.skip_reasons
    assert scan.complete is False


def test_a_resumed_root_that_records_again_after_an_absorb_keeps_physical_detail_counts(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    base = "https://jobs.example/globex/"
    cut = tuple(f"{base}cut-{index}" for index in range(3))
    _seed_continuation(
        durable_database,
        workspace_id,
        _scan_row(durable_database, source_id, day=-1),
        [
            {"url": url, "status": "completed", "deferred_reason": POSTING_DETAIL_COMPLETED}
            for url in cut
        ],
    )
    worker = _enqueue_scan(
        durable_database, workspace_id, _DeadlineCutBoard(base + "listed", cut, 3)
    )
    original = TaskCommands.release_source_read

    def crash_before_result(
        self: TaskCommands,
        member_id: str,
        task_id: str,
        attempt_count: int,
        *,
        result: dict[str, Any] | None = None,
        failed: bool = False,
    ) -> None:
        if result is not None and "board_result" in result:
            raise RuntimeError("synthetic interruption after the scan committed")
        original(self, member_id, task_id, attempt_count, result=result, failed=failed)

    with monkeypatch.context() as interrupted:
        interrupted.setattr(TaskCommands, "release_source_read", crash_before_result)
        assert worker.run_once()
    with durable_database.session() as session:
        root_id = session.scalars(select(SourceScanMember)).one().root_task_id
    TaskQueueService(durable_database).resume_source_batch(
        workspace_id, root_id, "resume-after-absorb"
    )
    assert worker.run_once()

    scan = _board_scan_row(durable_database)
    assert (scan.detail_attempted_count, scan.detail_read_count, scan.detail_capped_count) == (
        2,
        2,
        0,
    )
    assert scan.read_count == 4
    assert "time_limit_reached" not in scan.skip_reasons


def test_a_pending_search_cursor_keeps_the_deadline_cut_after_every_posting_is_credited(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    base = "https://jobs.example/globex/"
    cut = tuple(f"{base}cut-{index}" for index in range(2))
    _seed_continuation(
        durable_database,
        workspace_id,
        _scan_row(durable_database, source_id, day=-1),
        [
            {"url": url, "status": "completed", "deferred_reason": POSTING_DETAIL_COMPLETED}
            for url in cut
        ],
    )

    scan, _ = _run_scan(
        durable_database,
        workspace_id,
        _DeadlineCutBoard(base + "listed", cut, 2, cursors=(base + "search?page=2",)),
    )

    assert (scan.detail_capped_count, scan.read_count) == (0, 3)
    assert "time_limit_reached" in scan.skip_reasons
    assert scan.complete is False


def test_a_failed_prior_read_of_a_cut_posting_is_not_credited(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    base = "https://jobs.example/globex/"
    _seed_continuation(
        durable_database,
        workspace_id,
        _scan_row(durable_database, source_id, day=-1),
        [
            {
                "url": base + "cut-0",
                "status": "failed",
                "deferred_reason": "successfactors_posting_pending",
            }
        ],
    )

    scan, _ = _run_scan(
        durable_database, workspace_id, _DeadlineCutBoard(base + "listed", (base + "cut-0",), 1)
    )

    assert (scan.detail_capped_count, scan.read_count) == (1, 1)
    assert "time_limit_reached" in scan.skip_reasons


@pytest.mark.parametrize("repeat_receipt", [False, True], ids=["once", "replayed-chunk"])
def test_a_continuation_that_reads_every_deadline_cut_posting_settles_without_a_rescan(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch, repeat_receipt: bool
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    base = "https://jobs.example/globex/"
    board = _DeadlineCutBoard(base + "listed", tuple(f"{base}cut-{i}" for i in range(3)), 3)
    if repeat_receipt:
        original = ScanRecorder.record_detail_read_in_session

        def write_twice(self: ScanRecorder, *args: Any, **kwargs: Any) -> None:
            original(self, *args, **kwargs)
            original(self, *args, **kwargs)

        monkeypatch.setattr(ScanRecorder, "record_detail_read_in_session", write_twice)
    worker = _enqueue_scan(durable_database, workspace_id, board)
    for _ in range(6):
        if not worker.run_once():
            break

    scan = _board_scan_row(durable_database)
    assert scan.detail_capped_count == 0
    assert scan.read_count == 4
    assert "time_limit_reached" not in scan.skip_reasons
    assert "read deadline cut off" not in scan.notes
    assert scan.complete is True


def _withdrawing_read(event: AuditEvent) -> tuple[int, int, bool]:
    """Return the read counts a withdrawn_absent audit event carries."""
    details = event.details
    return details["raw_count"], details["read_count"], details["complete"]


def test_a_root_read_that_settles_its_frontier_withdraws_with_its_read_counts(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    base = "https://jobs.example/globex/"
    cut = tuple(f"{base}cut-{i}" for i in range(3))
    _stage_known_rows(durable_database, workspace_id, source_id, (base + "gone",))
    _seed_continuation(
        durable_database,
        workspace_id,
        _scan_row(durable_database, source_id, day=-1),
        [
            {"url": url, "status": "completed", "deferred_reason": POSTING_DETAIL_COMPLETED}
            for url in cut
        ],
    )

    scan, _ = _run_scan(durable_database, workspace_id, _DeadlineCutBoard(base + "listed", cut, 3))

    assert scan.complete is True
    assert _withdrawn_urls(durable_database) == {base + "gone"}
    with durable_database.session() as session:
        event = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "pending_discovery.withdrawn_absent")
        ).one()
    assert _withdrawing_read(event) == (4, 4, True)


class _PartlyBodilessBoard(_DeadlineCutBoard):
    """Return no body for `bodiless` targets; the root also defers a `sitemap` child."""

    def __init__(self, *args: Any, bodiless: frozenset[str], sitemap: str = "") -> None:
        super().__init__(*args)
        self.bodiless = bodiless
        self.sitemap = sitemap

    def listings(
        self,
        _source_name: str,
        _url: str,
        *,
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        read = super().listings(_source_name, _url)
        if not self.sitemap:
            return read
        child = replace(_continuation(self.sitemap), deferred_reason="sitemap_child_pending")
        return replace(
            read, raw_count=read.raw_count + 1, deferred_targets=(*read.deferred_targets, child)
        )

    def continue_listings(
        self,
        _employer: str,
        targets: tuple[SourceContinuationTarget, ...],
        *,
        root_url: str = "",
        handoff_scopes: tuple[tuple[str, str, str], ...] = (),
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        postings = [t for t in targets if t.url not in self.bodiless and t.url != self.sitemap]
        return SourceRead(
            listings=[_posting(target.url) for target in postings],
            complete=False,
            raw_count=len(postings),
            outcome=SourceReadOutcome.PUBLISHER_LINKED_PAGES,
            attempted_target_urls=tuple(target.url for target in targets),
        )


@pytest.mark.parametrize("repeat_receipt", [False, True], ids=["once", "replayed-chunk"])
def test_a_continuation_chunk_credits_only_the_cut_postings_it_read_a_body_for(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch, repeat_receipt: bool
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    base = "https://jobs.example/globex/"
    cut = tuple(f"{base}cut-{i}" for i in range(3))
    board = _PartlyBodilessBoard(base + "listed", cut, 3, bodiless=frozenset({cut[2]}))
    if repeat_receipt:
        original = ScanRecorder.record_detail_read_in_session

        def write_twice(self: ScanRecorder, *args: Any, **kwargs: Any) -> None:
            original(self, *args, **kwargs)
            original(self, *args, **kwargs)

        monkeypatch.setattr(ScanRecorder, "record_detail_read_in_session", write_twice)
    worker = _enqueue_scan(durable_database, workspace_id, board)
    for _ in range(6):
        if not worker.run_once():
            break

    scan = _board_scan_row(durable_database)
    assert (scan.detail_capped_count, scan.read_count) == (0, 3)
    assert "time_limit_reached" not in scan.skip_reasons
    assert scan.complete is True


def test_a_last_chunk_that_reads_only_a_cursor_still_settles_the_deadline_cut(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    base = "https://jobs.example/globex/"
    # Twenty postings fill the first chunk, so the sitemap child is read alone after them.
    cut = tuple(f"{base}cut-{i:02d}" for i in range(20))
    board = _PartlyBodilessBoard(
        base + "listed", cut, 20, bodiless=frozenset(), sitemap=base + "zz-sitemap.xml"
    )
    worker = _enqueue_scan(durable_database, workspace_id, board)
    for _ in range(6):
        if not worker.run_once():
            break

    scan = _board_scan_row(durable_database)
    with durable_database.session() as session:
        cursor_status = session.scalar(
            select(SourceScanFrontierItem.status).where(
                SourceScanFrontierItem.normalized_url.endswith("zz-sitemap.xml")
            )
        )
    assert cursor_status == "completed"
    assert scan.detail_capped_count == 0
    assert "time_limit_reached" not in scan.skip_reasons
    assert scan.complete is True


class _TimedOutSitemapBoard(_PartlyBodilessBoard):
    """Defer only a sitemap child and report the root read as out of time with nothing capped."""

    def listings(
        self,
        _source_name: str,
        _url: str,
        *,
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        return SourceRead(
            listings=[_posting(self.listed)],
            complete=False,
            raw_count=2,
            outcome=SourceReadOutcome.CRAWL_CONTINUATION,
            deferred_targets=(
                replace(_continuation(self.sitemap), deferred_reason="sitemap_child_pending"),
            ),
            attempted_target_urls=(self.listed,),
            exhausted_outcome=SourceReadOutcome.PUBLISHER_INDEX,
            detail=SourceDetailSummary(attempted=1, read=1, capped=0),
            skip_reasons={"time_limit_reached": 1},
        )


def test_a_drained_frontier_that_never_held_a_posting_body_keeps_its_time_limit(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    base = "https://jobs.example/globex/"
    board = _TimedOutSitemapBoard(
        base + "listed", (), 0, bodiless=frozenset(), sitemap=base + "sitemap.xml"
    )
    worker = _enqueue_scan(durable_database, workspace_id, board)
    for _ in range(6):
        if not worker.run_once():
            break

    scan = _board_scan_row(durable_database)
    with durable_database.session() as session:
        statuses = set(session.scalars(select(SourceScanFrontierItem.status)))
    assert statuses == {"completed"}
    assert scan.detail_capped_count == 0
    assert "time_limit_reached" in scan.skip_reasons
    assert scan.complete is False


def test_a_same_day_rescan_credits_postings_its_own_continuation_already_read(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    base = "https://jobs.example/globex/"
    board = _DeadlineCutBoard(base + "listed", tuple(f"{base}cut-{i}" for i in range(3)), 3)
    worker = _enqueue_scan(durable_database, workspace_id, board)
    for _ in range(6):
        if not worker.run_once():
            break

    scan, _ = _run_scan(durable_database, workspace_id, board)

    assert (scan.detail_capped_count, scan.read_count, scan.detail_read_count) == (0, 4, 1)
    assert "time_limit_reached" not in scan.skip_reasons
    assert "read deadline cut off" not in scan.notes
    assert scan.complete is True


def test_a_failed_scan_whose_frontier_drains_withdraws_nothing_and_stays_incomplete(
    durable_database: Database,
) -> None:
    """A read that failed never covered the source, whatever its frontier later resolves to."""
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    read = "https://jobs.example/globex/read"
    gone = "https://jobs.example/globex/gone"
    _stage_known_rows(durable_database, workspace_id, source_id, (read, gone))
    scan_id = _scan_row(
        durable_database,
        source_id,
        day=0,
        status="failed",
        exhausted_outcome=SourceReadOutcome.PUBLISHER_INDEX,
    )
    continuation_id = _seed_continuation(durable_database, workspace_id, scan_id, [{"url": read}])

    class ReadingBoard:
        def listings(
            self,
            _source_name: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            raise AssertionError("this test drives the continuation, never a fresh board read")

        def continue_listings(
            self,
            _employer: str,
            targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=[_posting(read)],
                complete=False,
                raw_count=len(targets),
                outcome=SourceReadOutcome.PUBLISHER_LINKED_PAGES,
                attempted_target_urls=tuple(target.url for target in targets),
            )

    SourceContinuationCommands(durable_database).request_continuation(workspace_id, scan_id)
    worker = Worker(
        durable_database,
        worker_id="failed-scan-worker",
        handlers=default_handlers(durable_database, fetcher=ReadingBoard()),
    )
    for _ in range(4):
        if not worker.run_once():
            break

    assert [row.status for row in _rows(durable_database, continuation_id)] == ["completed"]
    assert _withdrawn_urls(durable_database) == set()
    with durable_database.session() as session:
        scan = session.get(SourceScan, scan_id)
        assert scan is not None
        assert (scan.status, scan.complete) == ("failed", None)


def test_a_trailing_slash_does_not_make_a_listed_row_look_absent(
    durable_database: Database,
) -> None:
    """The present set is keyed on the posting fingerprint, never on stored URL text."""
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    listed = "https://jobs.example/globex/listed"
    continued = "https://jobs.example/globex/continued"
    gone = "https://jobs.example/globex/gone"
    _stage_known_rows(
        durable_database, workspace_id, source_id, (f"{listed}/", f"{continued}/", gone)
    )

    worker = _enqueue_scan(
        durable_database, workspace_id, _DrainingBoard(listed, continued, (continued,))
    )
    for _ in range(6):
        if not worker.run_once():
            break

    assert _withdrawn_urls(durable_database) == {gone}


def test_a_truncated_discovery_withdraws_nothing_even_once_its_frontier_drains(
    durable_database: Database,
) -> None:
    """A traversal that never saw its whole candidate set cannot speak for absence."""
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    listed = "https://jobs.example/globex/listed"
    continued = "https://jobs.example/globex/continued"
    gone = "https://jobs.example/globex/gone"
    _stage_known_rows(durable_database, workspace_id, source_id, (listed, continued, gone))
    board = _DrainingBoard(listed, continued, (continued,))
    board.discovery_truncated = True

    worker = _enqueue_scan(durable_database, workspace_id, board)
    for _ in range(6):
        if not worker.run_once():
            break

    assert _withdrawn_urls(durable_database) == set()
    scan = _board_scan_row(durable_database)
    assert scan.complete is False
    assert scan.read_outcome == SourceReadOutcome.CRAWL_CONTINUATION.value


@pytest.mark.parametrize("fallback_merge", [False, True])
def test_instability_discovered_during_continuation_survives_durable_exhaustion(
    durable_database: Database, fallback_merge: bool
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    listed = "https://jobs.example/globex/listed"
    continued = "https://jobs.example/globex/continued"
    other_page = "https://jobs.example/globex/page/other"
    final_page = "https://jobs.example/globex/page/final"
    omitted = "https://jobs.example/globex/omitted"
    _stage_known_rows(durable_database, workspace_id, source_id, (listed, continued, omitted))

    class ChangingBoard(_DrainingBoard):
        final_reads = 0

        def continue_listings(
            self,
            _employer: str,
            targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            if fallback_merge and len(targets) > 1:
                raise ValueError("Read these publisher pages individually")
            urls = tuple(target.url for target in targets)
            unstable = continued in urls
            if final_page in urls:
                self.final_reads += 1
            return SourceRead(
                listings=[_posting(continued)] if unstable else [],
                complete=False,
                raw_count=len(targets),
                outcome=SourceReadOutcome.CRAWL_CONTINUATION
                if unstable
                else SourceReadOutcome.PUBLISHER_LINKED_PAGES,
                deferred_targets=(
                    replace(_continuation(final_page), deferred_reason="workday_page_pending"),
                )
                if unstable
                else (),
                attempted_target_urls=urls,
                discovery_truncated=unstable,
            )

    board = ChangingBoard(listed, continued, (continued, other_page))
    worker = _enqueue_scan(durable_database, workspace_id, cast(SourceReader, board))
    assert worker.run_once()
    initial = _board_scan_row(durable_database)
    assert initial.complete is False
    assert initial.discovery_truncated is False
    for _ in range(6):
        if not worker.run_once():
            break

    assert board.final_reads == 1
    with durable_database.session() as session:
        states = set(session.scalars(select(SourceScanFrontierItem.status)))
        retained = session.scalars(
            select(PendingDiscovery).where(PendingDiscovery.discovery_url == omitted)
        ).one()
        assert retained.withdrawn_at is None
    assert states == {"completed"}
    assert _withdrawn_urls(durable_database) == set()
    scan = _board_scan_row(durable_database)
    assert scan.discovery_truncated is True
    assert scan.complete is False
    assert scan.read_outcome == SourceReadOutcome.CRAWL_CONTINUATION.value


@pytest.mark.parametrize(
    "carried",
    [
        {"incomplete_workday_listing": 1},
        {"incomplete_workday_listing": 1, "duplicate_workday_identity": 3},
        {"incomplete_oracle_listing": 1},
    ],
)
def test_a_continuation_that_truncates_discovery_names_its_reason_on_the_scan(
    durable_database: Database, carried: dict[str, int]
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    listed = "https://jobs.example/globex/listed"
    continued = "https://jobs.example/globex/continued"
    _stage_known_rows(durable_database, workspace_id, source_id, (listed, continued))

    class UnstableBoard(_DrainingBoard):
        def continue_listings(
            self, _employer: str, targets: tuple[SourceContinuationTarget, ...], **_: Any
        ) -> SourceRead:
            return SourceRead(
                listings=[_posting(continued)],
                complete=False,
                raw_count=len(targets),
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                attempted_target_urls=tuple(target.url for target in targets),
                discovery_truncated=True,
                skip_reasons={**carried, "listing_screened_out": 2},
            )

    board = UnstableBoard(listed, continued, (continued,), skip_reasons={"protected_posting": 1})
    worker = _enqueue_scan(durable_database, workspace_id, cast(SourceReader, board))
    for _ in range(6):
        if not worker.run_once():
            break

    scan = _board_scan_row(durable_database)
    assert scan.complete is False
    assert scan.discovery_truncated is True
    assert scan.skip_reasons == {"protected_posting": 1, **carried}


def test_a_carried_truncation_reason_does_not_hold_a_drained_scan() -> None:
    from clearscout.application.discovery.scan_outcome import settle_drained_scan

    scan = SourceScan(
        status="incomplete",
        read_outcome=SourceReadOutcome.CRAWL_CONTINUATION,
        skip_reasons={"incomplete_workday_listing": 1, "repeated_workday_page": 1},
        discovery_truncated=False,
        exhausted_outcome=SourceReadOutcome.ATS_COMPLETE,
        detail_coverage=None,
        complete=False,
        raw_count=1,
        read_count=1,
        result_count=1,
        notes="",
    )
    assert settle_drained_scan(scan, unresolved_count=0)
    assert scan.complete


def test_a_bounded_first_read_that_defers_the_rest_withdraws_nothing(
    durable_database: Database,
) -> None:
    """The bounded first read of a large traversal is not the publisher's whole list."""
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    listed = "https://jobs.example/globex/listed"
    continued = "https://jobs.example/globex/continued"
    gone = "https://jobs.example/globex/gone"
    _stage_known_rows(durable_database, workspace_id, source_id, (listed, continued, gone))

    worker = _enqueue_scan(
        durable_database, workspace_id, _DrainingBoard(listed, continued, (continued,))
    )
    assert worker.run_once()

    assert _withdrawn_urls(durable_database) == set()
    scan = _board_scan_row(durable_database)
    assert scan.complete is False
    assert scan.status == "incomplete"


def test_a_publisher_index_read_may_declare_itself_complete() -> None:
    """A drained traversal settles as a publisher index, so that outcome may claim completeness."""
    read = SourceRead(
        listings=[], complete=True, raw_count=0, outcome=SourceReadOutcome.PUBLISHER_INDEX
    )

    assert (read.complete, read.outcome) == (True, SourceReadOutcome.PUBLISHER_INDEX)


_WORKDAY_API = "https://globex.wd1.myworkdayjobs.com/wday/cxs/globex/Careers/jobs"
_WORKDAY_SITE = "https://globex.wd1.myworkdayjobs.com/Careers"
_WORKDAY_PAGE = 20
_WORKDAY_TOTAL = 900


def _workday_page_url(offset: int) -> str:
    """The frontier URL for the Workday page at ``offset``, as the adapter writes it."""
    return f"{_WORKDAY_API}?limit={_WORKDAY_PAGE}&offset={offset}"


class _WorkdayPages:
    """Answer Workday page POSTs from memory, recording each page URL requested.

    Workday carries its offset in a POST body, so the recorded URL is rebuilt
    from that body in the same form the frontier holds.
    """

    def __init__(self, *, failing_offset: int | None = None) -> None:
        self.requested: list[str] = []
        self.failing_offset = failing_offset

    def get(self, url: str, **_kwargs: object) -> httpx.Response:
        """Answer the robots read the outbound policy makes before any page."""
        return httpx.Response(200, text="", request=httpx.Request("GET", url))

    def post(self, url: str, *, json: dict[str, object], **_kwargs: object) -> httpx.Response:
        """Answer one page addressed by the offset in its request body."""
        offset, limit = int(str(json["offset"])), int(str(json["limit"]))
        page_url = f"{url}?limit={limit}&offset={offset}"
        self.requested.append(page_url)
        if offset == self.failing_offset:
            return httpx.Response(500, request=httpx.Request("POST", page_url))
        postings = [
            {"title": f"Role {offset + index}", "externalPath": f"/job/Role-{offset + index}"}
            for index in range(limit)
        ]
        return httpx.Response(
            200,
            request=httpx.Request("POST", page_url),
            json={"total": _WORKDAY_TOTAL, "jobPostings": postings},
        )

    def client(self) -> PoliteHttpClient:
        """Wire these pages behind the outbound policy the production fetcher uses."""
        return PoliteHttpClient(
            user_agent="clearscout/test",
            sleeper=lambda _seconds: None,
            jitter=lambda _low, _high: 0.0,
            getter=self.get,
            poster=self.post,
            resolver=lambda _host, port: [(AF_INET, SOCK_STREAM, 6, "", ("93.184.216.34", port))],
        )


def test_board_pages_read_in_one_continuation_task_complete_instead_of_returning_to_pending(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each page names the next as pending, including pages this same batch just read."""
    monkeypatch.setattr(workday, "WORKDAY_CONTINUATION_PAGES", 3)
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", _WORKDAY_SITE)
    scan_id = _scan_row(durable_database, source_id, day=0)
    seeded = tuple(_workday_page_url(offset) for offset in (400, 420, 440))
    continuation_id = _seed_continuation(
        durable_database,
        workspace_id,
        scan_id,
        [{"url": url, "deferred_reason": "workday_page_pending"} for url in seeded],
    )
    server = _WorkdayPages()

    SourceContinuationCommands(durable_database).request_continuation(workspace_id, scan_id)
    assert Worker(
        durable_database,
        worker_id="workday-page-batch-worker",
        handlers=default_handlers(
            durable_database, fetcher=BoardApiFetcher(client=server.client())
        ),
    ).run_once()

    rows = _rows(durable_database, continuation_id)
    by_url = {row.url: row for row in rows}
    assert [(by_url[url].status, by_url[url].attempt_count) for url in seeded] == [
        ("completed", 1)
    ] * 3
    assert [row.url for row in rows if row.status == "pending"] == [_workday_page_url(460)]
    assert server.requested == list(seeded)
    with durable_database.session() as session:
        [task] = session.scalars(
            select(BackgroundTask).where(
                BackgroundTask.task_type == "source.continue",
                BackgroundTask.status == "completed",
            )
        ).all()
        counts = dict(task.progress)
        follow_ups = session.scalars(
            select(BackgroundTask.id).where(
                BackgroundTask.task_type == "source.continue",
                BackgroundTask.status == "queued",
            )
        ).all()
    assert (counts["completed"], counts["pending"], counts["failed"]) == (3, 1, 0)
    assert len(follow_ups) == 1


def test_a_workday_page_that_keeps_failing_mid_chain_spends_its_own_attempts(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", _WORKDAY_SITE)
    scan_id = _scan_row(durable_database, source_id, day=0)
    continuation_id = _seed_continuation(
        durable_database,
        workspace_id,
        scan_id,
        [{"url": _workday_page_url(400), "deferred_reason": "workday_page_pending"}],
    )
    server = _WorkdayPages(failing_offset=440)
    worker = Worker(
        durable_database,
        worker_id="workday-failing-page-worker",
        handlers=default_handlers(
            durable_database, fetcher=BoardApiFetcher(client=server.client())
        ),
    )

    SourceContinuationCommands(durable_database).request_continuation(workspace_id, scan_id)
    for _ in range(4):
        assert worker.run_once()

    rows = {row.url: row for row in _rows(durable_database, continuation_id)}
    assert set(rows) == {_workday_page_url(400), _workday_page_url(440)}
    assert (rows[_workday_page_url(400)].status, rows[_workday_page_url(400)].attempt_count) == (
        "completed",
        1,
    )
    assert (rows[_workday_page_url(440)].status, rows[_workday_page_url(440)].attempt_count) == (
        "failed",
        3,
    )


class _FamilyPages(_WorkdayPages):
    """Answer Workday POSTs narrowed by the applied job family, recording each family read."""

    families: ClassVar[dict[str, int]] = {"fam-a": 30, "fam-b": 10}

    def post(self, url: str, *, json: dict[str, object], **_kwargs: object) -> httpx.Response:
        """Answer one family page addressed by its body."""
        offset, limit = int(str(json["offset"])), int(str(json["limit"]))
        applied = cast("dict[str, list[str]]", json["appliedFacets"])
        [family] = applied["jobFamilyGroup"]
        self.requested.append(f"{family}@{offset}")
        postings = [
            {"title": f"Role {index}", "externalPath": f"/job/Role-{family}-{index}"}
            for index in range(offset, min(offset + limit, self.families[family]))
        ]
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"total": self.families[family], "jobPostings": postings},
        )


def test_a_partitioned_workday_chain_credits_each_family_page_row_once(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workday, "WORKDAY_CONTINUATION_PAGES", 1)
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", _WORKDAY_SITE)
    scan_id = _scan_row(durable_database, source_id, day=0)
    plan = board_reads.PartitionPlan(
        (
            board_reads.ListingPartition((("jobFamilyGroup", "fam-a"),), 30),
            board_reads.ListingPartition((("jobFamilyGroup", "fam-b"),), 10),
        )
    )
    seeded = board_reads.with_query(
        f"{_WORKDAY_API}?{urlencode({board_reads.PARTITIONS_KEY: plan.as_query()})}",
        offset=0,
        limit=_WORKDAY_PAGE,
    )
    continuation_id = _seed_continuation(
        durable_database,
        workspace_id,
        scan_id,
        [{"url": seeded, "deferred_reason": "workday_page_pending"}],
    )
    server = _FamilyPages()
    worker = Worker(
        durable_database,
        worker_id="workday-family-chain-worker",
        handlers=default_handlers(
            durable_database, fetcher=BoardApiFetcher(client=server.client())
        ),
    )

    SourceContinuationCommands(durable_database).request_continuation(workspace_id, scan_id)
    for _ in range(5):
        assert worker.run_once()

    rows = _rows(durable_database, continuation_id)
    assert server.requested == ["fam-a@0", "fam-a@20", "fam-a@40", "fam-b@0", "fam-b@20"]
    assert len(rows) == 5
    assert [(row.status, row.attempt_count) for row in rows] == [("completed", 1)] * 5
    assert seeded in {row.url for row in rows}


def test_a_complete_read_may_not_keep_a_deferred_frontier() -> None:
    with pytest.raises(ValueError, match="cannot retain continuation targets"):
        SourceRead(
            listings=[],
            complete=True,
            raw_count=0,
            outcome=SourceReadOutcome.PUBLISHER_INDEX,
            deferred_targets=(_continuation("https://jobs.example/globex/pending"),),
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"url": "not a URL"}, "absolute HTTP URL"),
        ({"source_url": ""}, "source URL must contain"),
        ({"relations": ()}, "nonempty and unique"),
        ({"depth": 0}, "depth must be positive"),
        (
            {"relations": ("publisher-content", "publisher-content")},
            "nonempty and unique",
        ),
    ],
)
def test_source_continuation_target_refuses_invalid_provenance(
    overrides: dict[str, Any], message: str
) -> None:
    values: dict[str, Any] = {
        "url": "https://publisher.example/jobs/1",
        "label": "Published role",
        "label_source": "visible",
        "source_url": "https://publisher.example/careers",
        "structural_context": "main",
        "relations": ("publisher-content", "substantive-label"),
    }
    values.update(overrides)
    with pytest.raises(ValueError, match=message):
        SourceContinuationTarget(**values)


def test_source_read_retains_an_unbounded_typed_deferred_frontier() -> None:
    frontier = tuple(
        _continuation(f"https://publisher.example/jobs/{index}") for index in range(1001)
    )
    read = SourceRead(
        listings=[],
        complete=False,
        raw_count=0,
        outcome=SourceReadOutcome.CRAWL_CONTINUATION,
        deferred_targets=frontier,
    )

    assert read.deferred_targets == frontier


@pytest.mark.parametrize(
    "evidence",
    [
        ("https://publisher.example/jobs/1", "https://publisher.example/jobs/1"),
        ("",),
        ("not-a-url",),
        ("https://publisher.example/jobs/1", "https://publisher.example/jobs/2"),
    ],
)
def test_source_read_refuses_invalid_cumulative_detail_evidence(evidence: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        SourceRead(
            listings=[],
            complete=False,
            raw_count=1,
            outcome=SourceReadOutcome.PUBLISHER_LINKED_PAGES,
            detail_read_urls=evidence,
        )


@pytest.mark.parametrize(
    "arguments",
    [
        {
            "listings": [
                SourceListing(
                    employer="Acme",
                    title="Role",
                    url="https://publisher.example/jobs/1",
                )
            ],
            "complete": True,
            "raw_count": 0,
            "outcome": SourceReadOutcome.ATS_COMPLETE,
        },
        {
            "listings": [],
            "complete": True,
            "raw_count": 0,
            "outcome": SourceReadOutcome.CRAWL_CONTINUATION,
        },
        {
            "listings": [],
            "complete": False,
            "raw_count": 0,
            "outcome": SourceReadOutcome.ATS_COMPLETE,
        },
        {
            "listings": [],
            "complete": True,
            "raw_count": 1,
            "outcome": SourceReadOutcome.ATS_COMPLETE,
            "skip_reasons": {"missing_identity": 0},
        },
        {
            "listings": [],
            "complete": False,
            "raw_count": 0,
            "outcome": SourceReadOutcome.FETCH_FAILED,
            "deferred_targets": (_continuation("https://publisher.example/jobs/1"),),
        },
    ],
)
def test_source_read_refuses_contradictory_scan_evidence(arguments: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        SourceRead(**arguments)


def test_source_scan_persists_the_exact_deferred_read_frontier(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.lever.co/globex")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    frontier = (
        replace(_continuation("https://jobs.lever.co/globex?skip=3"), depth=3),
        replace(
            _continuation("https://jobs.lever.co/globex?skip=6"),
            depth=4,
            automatic_eligible=False,
            deferred_reason="low_priority_navigation",
        ),
    )

    class ContinuedBoard:
        def listings(
            self,
            _source_name: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=[],
                complete=False,
                raw_count=0,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=frontier,
            )

        def continue_listings(
            self,
            _employer: str,
            _targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=[],
                complete=False,
                raw_count=0,
                outcome=SourceReadOutcome.ATS_COMPLETE,
            )

    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ContinuedBoard(),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    [record] = (
        SourceScanService(
            durable_database,
            storage=runtime_storage(durable_database),
            posting_fetcher=UnexpectedPostingFetcher(),
        )
        .list_scan_history(workspace_id)
        .items
    )
    assert record.status == "incomplete"
    assert record.read_outcome is SourceReadOutcome.CRAWL_CONTINUATION
    assert record.frontier_pending_count == 2
    assert record.frontier_completed_count == 0
    assert record.frontier_failed_count == 0
    with durable_database.session() as session:
        rows = session.scalars(
            select(SourceScanFrontierItem).order_by(SourceScanFrontierItem.normalized_url)
        ).all()
    assert [(row.depth, row.automatic_eligible, row.deferred_reason) for row in rows] == [
        (3, True, ""),
        (4, False, "low_priority_navigation"),
    ]


def test_a_fresh_scan_whose_frontier_is_fully_already_completed_is_not_left_incomplete(
    durable_database: Database,
) -> None:
    """Every deferred target was already read; the scan must not stall forever."""
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    url = "https://jobs.example/globex/posting-1"
    earlier_scan = completed_earlier_scan(durable_database, workspace_id, source_id, (url,))

    class ContinuedBoard:
        def listings(
            self,
            _source_name: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=[],
                complete=False,
                raw_count=0,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=(_continuation(url),),
                exhausted_outcome=SourceReadOutcome.PUBLISHER_INDEX,
            )

        def continue_listings(
            self,
            _employer: str,
            _targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=[], complete=False, raw_count=0, outcome=SourceReadOutcome.ATS_COMPLETE
            )

    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ContinuedBoard(),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan).where(SourceScan.id != earlier_scan)).one()
        # Nothing remains to drain, so the scan reads like a complete publisher
        # index rather than sitting "incomplete" with no way to advance.
        assert scan.status == "completed_empty"
        assert scan.read_outcome == SourceReadOutcome.PUBLISHER_INDEX.value
        continuation = session.scalar(
            select(SourceScanContinuation).where(SourceScanContinuation.source_scan_id == scan.id)
        )
        assert continuation is not None
        continuation_id = continuation.id
    with durable_database.session() as session:
        rows = session.scalars(
            select(SourceScanFrontierItem).where(
                SourceScanFrontierItem.continuation_id == continuation_id
            )
        ).all()
    # The target dedups away as already read, so it is held as coverage, never queued.
    assert [(row.url, row.status) for row in rows] == [(url, "completed")]


def test_a_mixed_frontier_persists_only_the_new_targets_and_stays_a_continuation(
    durable_database: Database,
) -> None:
    """A partially-drained frontier still defers only what is genuinely new."""
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    done_url = "https://jobs.example/globex/posting-done"
    new_url = "https://jobs.example/globex/posting-new"
    earlier_scan = completed_earlier_scan(durable_database, workspace_id, source_id, (done_url,))

    class ContinuedBoard:
        def listings(
            self,
            _source_name: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=[],
                complete=False,
                raw_count=0,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=(_continuation(done_url), _continuation(new_url)),
                exhausted_outcome=SourceReadOutcome.PUBLISHER_INDEX,
            )

        def continue_listings(
            self,
            _employer: str,
            _targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=[], complete=False, raw_count=0, outcome=SourceReadOutcome.ATS_COMPLETE
            )

    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ContinuedBoard(),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan).where(SourceScan.id != earlier_scan)).one()
        assert scan.status == "incomplete"
        assert scan.read_outcome == SourceReadOutcome.CRAWL_CONTINUATION.value
        continuation = session.scalar(
            select(SourceScanContinuation).where(SourceScanContinuation.source_scan_id == scan.id)
        )
        assert continuation is not None
        continuation_id = continuation.id
    with durable_database.session() as session:
        rows = session.scalars(
            select(SourceScanFrontierItem)
            .where(SourceScanFrontierItem.continuation_id == continuation_id)
            .order_by(SourceScanFrontierItem.normalized_url)
        ).all()
    assert [row.url for row in rows if row.status == "pending"] == [new_url]


def test_a_same_day_rescan_reclassifies_once_its_carried_row_completes(
    durable_database: Database,
) -> None:
    """A same-day rescan must read pending frontier state, not total frontier rows.

    The carried completed row keeps the continuation's row count above zero even
    once nothing remains pending; only the pending count tells the two apart.
    """
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    url = "https://jobs.example/globex/posting-1"

    class ContinuedBoard:
        def listings(
            self,
            _source_name: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=[],
                complete=False,
                raw_count=0,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=(_continuation(url),),
                exhausted_outcome=SourceReadOutcome.PUBLISHER_INDEX,
            )

        def continue_listings(
            self,
            _employer: str,
            _targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=[], complete=False, raw_count=0, outcome=SourceReadOutcome.ATS_COMPLETE
            )

    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ContinuedBoard(),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.scan(workspace_id)

    with durable_database.session() as session:
        scan_id = session.scalar(select(SourceScan.id).where(SourceScan.query_label == QUERY_LABEL))
        row = session.scalars(select(SourceScanFrontierItem)).one()
        assert scan_id is not None
        # A completed automatic continuation task would have left the row this way.
        row.status = "completed"
        row.deferred_reason = POSTING_DETAIL_COMPLETED
        row.completed_at = utc_now()
        for task in session.scalars(
            select(BackgroundTask).where(BackgroundTask.task_type == "source.continue")
        ):
            task.status = "completed"
            task.completed_at = utc_now()
        for member in session.scalars(select(SourceScanMember)):
            member.state = "completed"
            member.released_at = utc_now()

    service.scan(workspace_id)

    with durable_database.session() as session:
        scan = session.get(SourceScan, scan_id)
        assert scan is not None
        assert scan.status == "completed_empty"
        assert scan.read_outcome == SourceReadOutcome.PUBLISHER_INDEX.value


def test_a_same_day_rescan_with_an_exhausted_failed_retry_stays_incomplete(
    durable_database: Database,
) -> None:
    """A row that exhausted its retries and failed is unresolved, not exhausted.

    It is already known to this continuation, so the rediscovered target is
    skipped and nothing is pending; a scan must still not call that drained.
    """
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    url = "https://jobs.example/globex/posting-1"

    class ContinuedBoard:
        def listings(
            self,
            _source_name: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=[],
                complete=False,
                raw_count=0,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=(_continuation(url),),
                exhausted_outcome=SourceReadOutcome.PUBLISHER_INDEX,
            )

        def continue_listings(
            self,
            _employer: str,
            _targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=[], complete=False, raw_count=0, outcome=SourceReadOutcome.ATS_COMPLETE
            )

    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ContinuedBoard(),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.scan(workspace_id)

    with durable_database.session() as session:
        scan_id = session.scalar(select(SourceScan.id).where(SourceScan.query_label == QUERY_LABEL))
        row = session.scalars(select(SourceScanFrontierItem)).one()
        assert scan_id is not None
        # Exhausted its automatic retries: terminally failed, not completed.
        row.status = "failed"
        row.attempt_count = MAX_AUTOMATIC_ATTEMPTS

    service.scan(workspace_id)

    with durable_database.session() as session:
        scan = session.get(SourceScan, scan_id)
        assert scan is not None
        assert scan.status == "incomplete"
        assert scan.read_outcome == SourceReadOutcome.CRAWL_CONTINUATION.value


def test_a_board_scan_writes_the_frontier_and_enqueues_automatic_continuation(
    durable_database: Database,
) -> None:
    """An eligible saved publisher link starts recovery without another user action."""
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    frontier = (_continuation("https://jobs.example/globex/first"),)

    class ContinuedBoard:
        def listings(
            self,
            _source_name: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=[],
                complete=False,
                raw_count=0,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=frontier,
            )

        def continue_listings(
            self,
            _employer: str,
            _targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            raise AssertionError("this test inspects the queued task without running it")

    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ContinuedBoard(),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    with durable_database.session() as session:
        pending = session.scalars(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.status == "pending")
        ).all()
        tasks = session.scalars(
            select(BackgroundTask).where(BackgroundTask.task_type == "source.continue")
        ).all()
    assert [item.url for item in pending] == [frontier[0].url]
    assert len(tasks) == 1
    assert tasks[0].payload["include_policy_deferred"] is False


def test_request_continuation_enqueues_the_keyed_bounded_task(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    frontier = (
        replace(
            _continuation("https://jobs.example/globex/first"),
            automatic_eligible=False,
            deferred_reason="low_priority_navigation",
        ),
    )

    class ContinuedBoard:
        def listings(
            self,
            _source_name: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=[],
                complete=False,
                raw_count=0,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=frontier,
            )

        def continue_listings(
            self,
            employer: str,
            targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=[SourceListing(employer, "Role", targets[0].url)],
                complete=True,
                raw_count=1,
                outcome=SourceReadOutcome.ATS_COMPLETE,
            )

    board = ContinuedBoard()
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=board,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.scan(workspace_id)
    with durable_database.session() as session:
        scan_id = session.scalar(select(SourceScan.id))
        continuation = session.scalars(select(SourceScanContinuation)).one()
        [item] = session.scalars(select(SourceScanFrontierItem)).all()
    assert scan_id is not None

    task = service.request_continuation(workspace_id, scan_id)

    with durable_database.session() as session:
        row = session.get(BackgroundTask, task.id)
        assert row is not None
        assert row.task_type == "source.continue"
        assert row.payload == {
            "continuation_id": continuation.id,
            "generation": continuation.generation + 1,
            "include_policy_deferred": True,
            "no_progress_count": 0,
        }
        assert row.idempotency_key == (
            f"source-continuation:{continuation.id}:{continuation.generation + 1}"
            f":manual:{item.id}:0"
        )

    # A repeat request against the same unfinished chain replays the task rather
    # than double-reading, because an equal idempotency key returns the same row.
    replayed = service.request_continuation(workspace_id, scan_id)
    assert replayed.id == task.id

    worker = Worker(
        durable_database,
        worker_id="explicit-continuation-worker",
        handlers=default_handlers(durable_database, fetcher=board),
    )
    assert worker.run_once()
    with durable_database.session() as session:
        [completed] = session.scalars(select(SourceScanFrontierItem)).all()
    assert completed.status == "completed"


def test_manual_continuation_reads_one_chunk_without_propagating_the_override(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    frontier = tuple(
        replace(
            _continuation(f"https://jobs.example/globex/{number}"),
            automatic_eligible=False,
            deferred_reason="low_priority_navigation",
        )
        for number in range(CONTINUATION_CHUNK_SIZE + 1)
    )

    class DeferredBoard:
        attempted = 0

        def listings(
            self,
            _source_name: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=[],
                complete=False,
                raw_count=0,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=frontier,
            )

        def continue_listings(
            self,
            _employer: str,
            targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            self.attempted += len(targets)
            return SourceRead(
                listings=[],
                complete=True,
                raw_count=len(targets),
                outcome=SourceReadOutcome.ATS_COMPLETE,
                attempted_target_urls=tuple(target.url for target in targets),
            )

    board = DeferredBoard()
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=board,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.scan(workspace_id)
    with durable_database.session() as session:
        scan_id = session.scalar(select(SourceScan.id))
    assert scan_id is not None
    task = service.request_continuation(workspace_id, scan_id)
    worker = Worker(
        durable_database,
        worker_id="manual-one-chunk-worker",
        handlers=default_handlers(durable_database, fetcher=board),
    )
    assert worker.run_once()
    completed_task = TaskQueueService(durable_database).get(task.id)
    assert completed_task.status == "completed", completed_task.error_message

    with durable_database.session() as session:
        tasks = session.scalars(
            select(BackgroundTask).where(BackgroundTask.task_type == "source.continue")
        ).all()
        pending = session.scalars(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.status == "pending")
        ).all()
    assert board.attempted == CONTINUATION_CHUNK_SIZE
    assert len(tasks) == 1
    assert len(pending) == 1
    assert pending[0].automatic_eligible is False


def test_request_continuation_refuses_a_scan_with_no_deferred_links(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")

    class CompleteBoard:
        def listings(
            self,
            _source_name: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=[], complete=True, raw_count=0, outcome=SourceReadOutcome.ATS_COMPLETE
            )

        def continue_listings(
            self,
            _employer: str,
            _targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            raise AssertionError("a scan with nothing deferred must not continue")

    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=CompleteBoard(),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.scan(workspace_id)
    with durable_database.session() as session:
        scan_id = session.scalar(select(SourceScan.id))
    assert scan_id is not None

    with pytest.raises(ValidationError, match="no deferred publisher links"):
        service.request_continuation(workspace_id, scan_id)


def test_request_continuation_refuses_a_scan_whose_frontier_is_exhausted(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    scan_id = new_id()
    now = utc_now()
    with durable_database.session() as session:
        session.add(
            SourceScan(
                id=scan_id,
                source_id=source_id,
                scanned_on=now.date(),
                query_label="title-filter",
                status="incomplete",
                read_outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                raw_count=0,
                read_count=0,
                skip_reasons={},
                result_count=0,
                new_count=0,
                notes="",
                scan_started_at=now,
                scan_completed_at=now,
                duration_ms=0,
            )
        )
        seed_scan_continuation(
            session,
            workspace_id,
            scan_id,
            [
                {
                    "url": "https://jobs.example/globex/first",
                    "source_url": "https://jobs.example/globex",
                    "status": "completed",
                    "attempt_count": 1,
                }
            ],
        )

    commands = SourceContinuationCommands(durable_database)
    with pytest.raises(ValidationError, match="no deferred publisher links"):
        commands.request_continuation(workspace_id, scan_id)


def test_request_continuation_refuses_a_scan_superseded_by_a_newer_scan(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    target = _continuation("https://jobs.example/globex/first")

    class ContinuedBoard:
        calls = 0

        def listings(
            self,
            _source_name: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            self.calls += 1
            if self.calls == 1:
                return SourceRead(
                    listings=[],
                    complete=False,
                    raw_count=0,
                    outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                    deferred_targets=(target,),
                )
            return SourceRead(
                listings=[], complete=True, raw_count=0, outcome=SourceReadOutcome.ATS_COMPLETE
            )

        def continue_listings(
            self,
            _employer: str,
            _targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            raise AssertionError("a superseded scan must not continue")

    board = ContinuedBoard()
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=board,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.scan(workspace_id)
    with durable_database.session() as session:
        stale_scan_id = session.scalar(select(SourceScan.id))
        source = session.scalars(select(Source)).one()
        session.expunge(source)
    assert stale_scan_id is not None

    # A later day's scan creates a new SourceScan row for the same source,
    # so the frontier tied to the earlier one is no longer its source's latest.
    tomorrow = utc_now() + timedelta(days=1)
    service.sweep.recording.record_scan(
        source,
        tomorrow.date(),
        "completed",
        0,
        0,
        "",
        started_at=tomorrow,
        completed_at=tomorrow,
        read_outcome=SourceReadOutcome.ATS_COMPLETE,
        deferred_targets=(),
    )

    with pytest.raises(ValidationError, match="no longer its source's latest"):
        service.request_continuation(workspace_id, stale_scan_id)


def test_a_reclaimed_continuation_of_a_superseded_scan_reads_nothing(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    target = _continuation("https://jobs.example/globex/first")

    class ContinuedBoard:
        continued: ClassVar[list[str]] = []

        def listings(
            self,
            _source_name: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=[],
                complete=False,
                raw_count=0,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=(target,),
            )

        def continue_listings(
            self,
            _employer: str,
            targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            self.continued.extend(item.url for item in targets)
            return SourceRead(
                listings=[], complete=True, raw_count=0, outcome=SourceReadOutcome.ATS_COMPLETE
            )

    board = ContinuedBoard()
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=board,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.scan(workspace_id)
    queue = TaskQueueService(durable_database)
    claimed = queue.claim_next(worker_id="lost-worker")
    assert claimed is not None and claimed.task_type == "source.continue"
    with durable_database.session() as session:
        source = session.scalars(select(Source)).one()
        session.expunge(source)
        task = session.get(BackgroundTask, claimed.id)
        assert task is not None
        task.lease_expires_at = utc_now() - timedelta(seconds=1)

    tomorrow = utc_now() + timedelta(days=1)
    service.sweep.recording.record_scan(
        source,
        tomorrow.date(),
        "completed",
        0,
        0,
        "",
        started_at=tomorrow,
        completed_at=tomorrow,
        read_outcome=SourceReadOutcome.ATS_COMPLETE,
        deferred_targets=(),
    )
    worker = Worker(
        durable_database,
        worker_id="reclaiming-worker",
        handlers={
            "source.continue": SourceContinuationHandler(durable_database, board, service.sweep)
        },
    )

    assert worker.run_once()
    assert board.continued == []
    with durable_database.session() as session:
        statuses = session.scalars(select(SourceScanFrontierItem.status)).all()
    assert statuses == ["pending"]


def test_a_continuation_keeps_chaining_past_the_old_aggregate_read_cap(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    monkeypatch.setattr(continuation_handler, "CONTINUATION_CHUNK_SIZE", 1)
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    frontier = tuple(_continuation(f"https://jobs.example/globex/{n}") for n in range(3))

    class ContinuedBoard:
        def listings(
            self,
            _source_name: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=[],
                complete=False,
                raw_count=0,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=frontier,
            )

        def continue_listings(
            self,
            employer: str,
            targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=[SourceListing(employer, "Role", targets[0].url)],
                complete=False,
                raw_count=len(targets),
                outcome=SourceReadOutcome.PUBLISHER_LINKED_PAGES,
            )

    board = ContinuedBoard()
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=board,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.scan(workspace_id)
    worker = Worker(
        durable_database,
        worker_id="budget-worker",
        handlers=default_handlers(durable_database, fetcher=board),
    )
    for _ in range(6):
        if not worker.run_once():
            break

    with durable_database.session() as session:
        completed = session.scalars(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.status == "completed")
        ).all()
        pending = session.scalars(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.status == "pending")
        ).all()
        continue_tasks = session.scalars(
            select(BackgroundTask).where(BackgroundTask.task_type == "source.continue")
        ).all()
    assert len(completed) == 3
    assert len(pending) == 0
    assert len(continue_tasks) == 3


def test_continuation_does_not_spend_a_retry_on_an_unvisited_frontier_url(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    monkeypatch.setattr(continuation_handler, "CONTINUATION_CHUNK_SIZE", 2)
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    frontier = (
        _continuation("https://jobs.example/globex/first"),
        _continuation("https://jobs.example/globex/second"),
    )

    class DeadlineBoard:
        def listings(
            self,
            _source_name: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=[],
                complete=False,
                raw_count=0,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=frontier,
            )

        def continue_listings(
            self,
            _employer: str,
            targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=[],
                complete=False,
                raw_count=len(targets),
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=targets,
                attempted_target_urls=(targets[0].url,),
            )

    board = DeadlineBoard()
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=board,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.scan(workspace_id)
    with durable_database.session() as session:
        scan_id = session.scalar(select(SourceScan.id))
    assert scan_id is not None
    worker = Worker(
        durable_database,
        worker_id="deadline-continuation-worker",
        handlers=default_handlers(durable_database, fetcher=board),
    )
    assert worker.run_once()

    with durable_database.session() as session:
        rows = session.scalars(
            select(SourceScanFrontierItem).order_by(SourceScanFrontierItem.normalized_url)
        ).all()
    assert [row.attempt_count for row in rows] == [1, 0]
    assert [row.status for row in rows] == ["pending", "pending"]


def test_zero_attempt_continuation_retries_then_routes_to_manual_recovery(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    targets = (
        _continuation("https://jobs.example/globex/first"),
        _continuation("https://jobs.example/globex/second"),
    )
    monkeypatch.setattr(
        "clearscout.application.discovery.source_continuation.handler.CONTINUATION_CHUNK_SIZE", 1
    )

    class StalledBoard:
        def listings(
            self,
            _source_name: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=[],
                complete=False,
                raw_count=0,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=targets,
            )

        def continue_listings(
            self,
            _employer: str,
            _targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=[],
                complete=False,
                raw_count=0,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                attempted_target_urls=(),
            )

    board = StalledBoard()
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=board,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.scan(workspace_id)
    worker = Worker(
        durable_database,
        worker_id="stalled-continuation-worker",
        handlers=default_handlers(durable_database, fetcher=board),
    )
    for _ in range(MAX_NO_PROGRESS_TASKS * len(targets)):
        assert worker.run_once()

    with durable_database.session() as session:
        rows = session.scalars(
            select(SourceScanFrontierItem).order_by(SourceScanFrontierItem.normalized_url)
        ).all()
        tasks = session.scalars(
            select(BackgroundTask).where(BackgroundTask.task_type == "source.continue")
        ).all()
    assert [row.status for row in rows] == ["pending", "pending"]
    assert [row.attempt_count for row in rows] == [0, 0]
    assert [row.automatic_eligible for row in rows] == [False, False]
    assert [row.deferred_reason for row in rows] == [
        "automatic_no_progress",
        "automatic_no_progress",
    ]
    assert len(tasks) == MAX_NO_PROGRESS_TASKS * len(targets)


def test_same_day_rescan_unions_the_frontier_and_supersedes_its_task(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    first = _continuation("https://jobs.example/globex/first")
    second = _continuation("https://jobs.example/globex/second")
    replacement = _continuation("https://jobs.example/globex/replacement")

    class ContinuedBoard:
        calls = 0

        def listings(
            self,
            _source_name: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            raise AssertionError("the test records the already-read scan directly")

        def continue_listings(
            self,
            _employer: str,
            _targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            self.calls += 1
            raise AssertionError("a superseded task must not perform publisher I/O")

    board = ContinuedBoard()
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=board,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    queue = TaskQueueService(durable_database)
    state_target_titles(durable_database, workspace_id, "Operations Analyst")
    first_root = queue.enqueue(
        workspace_id=workspace_id, task_type="source.scan", payload={}, idempotency_key="first-root"
    )
    now = utc_now()
    with durable_database.session() as session:
        source = session.scalars(select(Source)).one()
        session.expunge(source)
    service.sweep.recording.record_scan(
        source,
        now.date(),
        "incomplete",
        0,
        0,
        "first frontier",
        started_at=now,
        completed_at=now,
        read_outcome=SourceReadOutcome.CRAWL_CONTINUATION,
        deferred_targets=(first, second),
        correlation_id=str(first_root.id),
    )
    with durable_database.session() as session:
        scan_id = session.scalar(select(SourceScan.id))
    assert scan_id is not None
    queue.request_cancellation(str(first_root.id))
    second_root = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={},
        idempotency_key="second-root",
    )
    service.sweep.recording.record_scan(
        source,
        now.date(),
        "incomplete",
        0,
        0,
        "replacement frontier",
        started_at=now,
        completed_at=now,
        read_outcome=SourceReadOutcome.CRAWL_CONTINUATION,
        deferred_targets=(second, replacement),
        correlation_id=str(second_root.id),
    )

    with durable_database.session() as session:
        continuation = session.scalars(select(SourceScanContinuation)).one()
        items = session.scalars(
            select(SourceScanFrontierItem).order_by(SourceScanFrontierItem.normalized_url)
        ).all()
        tasks = session.scalars(
            select(BackgroundTask)
            .where(BackgroundTask.task_type == "source.continue")
            .order_by(BackgroundTask.created_at)
        ).all()
    assert continuation.generation == 2
    assert [item.normalized_url for item in items] == sorted(
        [first.url, second.url, replacement.url]
    )
    assert [task.payload["generation"] for task in tasks] == [1, 2]
    assert len({task.idempotency_key for task in tasks}) == 2

    assert [task.status for task in tasks] == ["cancelled", "queued"]
    assert board.calls == 0


def test_a_manual_continuation_stays_with_its_scan_after_a_same_day_rescan(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    deferred = replace(
        _continuation("https://jobs.example/globex/first"),
        automatic_eligible=False,
        deferred_reason="low_priority_navigation",
    )
    state_target_titles(durable_database, workspace_id, "Operations Analyst")
    queue = TaskQueueService(durable_database)
    first_task = str(
        queue.enqueue(
            workspace_id=workspace_id,
            task_type="source.scan",
            payload={},
            idempotency_key="first-scan",
        ).id
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    now = utc_now()
    with durable_database.session() as session:
        source = session.scalars(select(Source)).one()
        session.expunge(source)

    def record(correlation_id: str) -> None:
        service.sweep.recording.record_scan(
            source,
            now.date(),
            "incomplete",
            0,
            0,
            "deferred frontier",
            started_at=now,
            completed_at=now,
            read_outcome=SourceReadOutcome.CRAWL_CONTINUATION,
            deferred_targets=(deferred,),
            correlation_id=correlation_id,
        )

    record(first_task)
    with durable_database.session() as session:
        scan_id = session.scalar(select(SourceScan.id))
    assert scan_id is not None
    manual = service.request_continuation(workspace_id, scan_id)
    queue.request_cancellation(first_task)
    second_task = str(
        queue.enqueue(
            workspace_id=workspace_id,
            task_type="source.scan",
            payload={},
            idempotency_key="second-scan",
        ).id
    )
    record(second_task)

    with durable_database.session() as session:
        assert continuation_task_ids(session, workspace_id, first_task) == [str(manual.id)]
        assert continuation_task_ids(session, workspace_id, second_task) == []


@pytest.mark.parametrize(
    ("first", "second", "attempted", "automatic"),
    [
        ("icims_page_pending", "icims_page_pending", False, True),
        ("icims_page_pending", "icims_page_pending", True, False),
        ("icims_page_pending", "low_priority_navigation", False, False),
        ("low_priority_navigation", "low_priority_navigation", False, False),
    ],
    ids=["untried-cursor", "attempted", "other-reason", "not-a-cursor"],
)
def test_a_same_day_rescan_makes_only_an_untried_manual_page_cursor_automatic(
    durable_database: Database, first: str, second: str, attempted: bool, automatic: bool
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    cursor = _continuation("https://jobs.example/globex/api/jobs?page=6&limit=20")
    state_target_titles(durable_database, workspace_id, "Operations Analyst")
    queue = TaskQueueService(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    now = utc_now()
    with durable_database.session() as session:
        source = session.scalars(select(Source)).one()
        session.expunge(source)

    def record(key: str, reason: str, *, eligible: bool) -> str:
        task = str(
            queue.enqueue(
                workspace_id=workspace_id, task_type="source.scan", payload={}, idempotency_key=key
            ).id
        )
        service.sweep.recording.record_scan(
            source,
            now.date(),
            "incomplete",
            0,
            0,
            "page cursor",
            started_at=now,
            completed_at=now,
            read_outcome=SourceReadOutcome.CRAWL_CONTINUATION,
            deferred_targets=(
                replace(cursor, deferred_reason=reason, automatic_eligible=eligible),
            ),
            correlation_id=task,
        )
        return task

    first_task = record("first-scan", first, eligible=False)
    if attempted:
        with durable_database.session() as session:
            session.scalars(select(SourceScanFrontierItem)).one().attempt_count = 1
    queue.request_cancellation(first_task)
    second_task = record("second-scan", second, eligible=True)

    with durable_database.session() as session:
        row = session.scalars(select(SourceScanFrontierItem)).one()
        assert row.automatic_eligible is automatic
        queued = continuation_task_ids(session, workspace_id, second_task)
        assert len(queued) == (1 if automatic else 0)


def test_complete_same_day_rescan_clears_the_prior_frontier(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    target = _continuation("https://jobs.example/globex/first")

    class ContinuedBoard:
        def listings(
            self,
            _source_name: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            raise AssertionError("the test records the already-read scan directly")

        def continue_listings(
            self,
            _employer: str,
            _targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            raise AssertionError("a cleared frontier must not perform publisher I/O")

    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ContinuedBoard(),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    now = utc_now()
    with durable_database.session() as session:
        source = session.scalars(select(Source)).one()
        session.expunge(source)
    service.sweep.recording.record_scan(
        source,
        now.date(),
        "incomplete",
        0,
        0,
        "frontier",
        started_at=now,
        completed_at=now,
        read_outcome=SourceReadOutcome.CRAWL_CONTINUATION,
        deferred_targets=(target,),
    )
    service.sweep.recording.record_scan(
        source,
        now.date(),
        "completed",
        0,
        0,
        "complete",
        started_at=now,
        completed_at=now,
        read_outcome=SourceReadOutcome.ATS_COMPLETE,
        deferred_targets=(),
    )

    [record] = service.list_scan_history(workspace_id).items
    assert record.status == "completed"
    assert record.frontier_pending_count == 0
    assert record.frontier_completed_count == 0
    assert record.frontier_failed_count == 0


def test_next_day_failure_carries_the_prior_frontier_to_the_latest_scan(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    target = _continuation("https://jobs.example/globex/first")

    class UnusedBoard:
        def listings(
            self,
            _source_name: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            raise AssertionError("the test records scans directly")

        def continue_listings(
            self,
            _employer: str,
            _targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            raise AssertionError("the test does not run continuation work")

    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=UnusedBoard(),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    now = utc_now()
    with durable_database.session() as session:
        source = session.scalars(select(Source)).one()
        session.expunge(source)
    service.sweep.recording.record_scan(
        source,
        now.date(),
        "incomplete",
        0,
        0,
        "frontier",
        started_at=now,
        completed_at=now,
        read_outcome=SourceReadOutcome.CRAWL_CONTINUATION,
        deferred_targets=(target,),
    )
    with durable_database.session() as session:
        first_task = session.scalars(
            select(BackgroundTask).where(BackgroundTask.task_type == "source.continue")
        ).one()
    TaskQueueService(durable_database).request_cancellation(first_task.id)
    tomorrow = now + timedelta(days=1)
    service.sweep.recording.record_scan(
        source,
        tomorrow.date(),
        "failed",
        0,
        0,
        "temporary failure",
        started_at=tomorrow,
        completed_at=tomorrow,
        read_outcome=SourceReadOutcome.FETCH_FAILED,
        deferred_targets=(),
    )

    with durable_database.session() as session:
        scans = list(session.scalars(select(SourceScan).order_by(SourceScan.scanned_on)))
        latest = scans[-1]
        continuation = session.scalar(
            select(SourceScanContinuation).where(SourceScanContinuation.source_scan_id == latest.id)
        )
        assert continuation is not None
        rows = list(
            session.scalars(
                select(SourceScanFrontierItem).where(
                    SourceScanFrontierItem.continuation_id == continuation.id
                )
            )
        )
    assert [row.normalized_url for row in rows] == [target.url]
    assert rows[0].status == "pending"


@pytest.mark.parametrize("complete", [False, True])
def test_only_a_partial_completed_read_carries_the_prior_frontier(
    durable_database: Database, complete: bool
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    target = _continuation("https://jobs.example/globex/first")

    class UnusedBoard:
        def listings(
            self,
            _source_name: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            raise AssertionError("the test records scans directly")

        def continue_listings(
            self,
            _employer: str,
            _targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            raise AssertionError("the test does not run continuation work")

    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=UnusedBoard(),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    now = utc_now()
    with durable_database.session() as session:
        source = session.scalars(select(Source)).one()
        session.expunge(source)
    service.sweep.recording.record_scan(
        source,
        now.date(),
        "incomplete",
        0,
        0,
        "frontier",
        started_at=now,
        completed_at=now,
        read_outcome=SourceReadOutcome.CRAWL_CONTINUATION,
        deferred_targets=(target,),
    )
    with durable_database.session() as session:
        first_task = session.scalars(
            select(BackgroundTask).where(BackgroundTask.task_type == "source.continue")
        ).one()
    TaskQueueService(durable_database).request_cancellation(first_task.id)
    tomorrow = now + timedelta(days=1)
    service.sweep.recording.record_scan(
        source,
        tomorrow.date(),
        None,
        0,
        0,
        None,
        started_at=tomorrow,
        completed_at=tomorrow,
        read_outcome=SourceReadOutcome.PUBLISHER_INDEX,
        deferred_targets=(),
        read_context=ScanReadContext(raw_count=2, read_count=2, complete=complete),
    )

    with durable_database.session() as session:
        latest = session.scalars(select(SourceScan).order_by(SourceScan.scanned_on)).all()[-1]
        carried = list(
            session.scalars(
                select(SourceScanFrontierItem.normalized_url)
                .join(
                    SourceScanContinuation,
                    SourceScanContinuation.id == SourceScanFrontierItem.continuation_id,
                )
                .where(
                    SourceScanContinuation.source_scan_id == latest.id,
                    SourceScanFrontierItem.status == "pending",
                )
            )
        )
    assert carried == ([] if complete else [target.url])


def test_source_scan_refuses_a_frontier_without_a_continuation_executor(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")

    class ReaderOnly:
        def listings(
            self,
            _source_name: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=[],
                complete=False,
                raw_count=0,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=(_continuation("https://jobs.example/globex?page=2"),),
            )

    with pytest.raises(ValidationError, match="without a configured continuation reader"):
        SourceScanService(
            durable_database,
            storage=runtime_storage(durable_database),
            fetcher=ReaderOnly(),
            posting_fetcher=UnexpectedPostingFetcher(),
        ).scan(workspace_id)

    with durable_database.session() as session:
        assert session.scalar(select(SourceScan.id)) is None


def test_unreadable_frontier_gets_three_distinct_automatic_attempts(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    target = _continuation("https://jobs.example/globex/lead", label="Data Platform Lead")

    class RetryingBoard:
        def listings(
            self,
            _source_name: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=[],
                complete=False,
                raw_count=0,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=(target,),
            )

        def continue_listings(
            self,
            employer: str,
            targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            assert targets == (target,)
            return SourceRead(
                listings=[
                    SourceListing(
                        employer=employer,
                        title=target.label,
                        url=target.url,
                        content_status=PostingContentAccessStatus.FETCH_FAILED,
                    )
                ],
                complete=False,
                raw_count=1,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=(target,),
            )

    board = RetryingBoard()
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=board,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.scan(workspace_id)
    with durable_database.session() as session:
        scan_id = session.scalar(select(SourceScan.id))
    assert scan_id is not None
    worker = Worker(
        durable_database,
        worker_id="source-continuation-worker",
        handlers=default_handlers(durable_database, fetcher=board),
    )

    assert worker.run_once()
    assert worker.run_once()
    assert worker.run_once()
    assert not worker.run_once()

    with durable_database.session() as session:
        [item] = session.scalars(select(SourceScanFrontierItem)).all()
        tasks = session.scalars(
            select(BackgroundTask).where(BackgroundTask.task_type == "source.continue")
        ).all()
        assert (item.status, item.attempt_count) == ("failed", 3)
        assert len(tasks) == 3
        assert len({task.idempotency_key for task in tasks}) == 3


def test_a_continuation_preserves_its_refusals_and_keeps_the_viable_row(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            locations=[Location(key="canada", country="Canada")],
            countries=[CountryNames(country="Canada")],
            seniority=Seniority(target_titles=titles("data"), excluded_titles=["intern"]),
        ),
    )
    target = _continuation("https://jobs.example/globex/recovered", label="Recovered page")
    viable = replace(
        source_listing("Data Platform Lead", "https://jobs.example/globex/data-platform-lead"),
        published_description="Own the data platform and its publisher-backed roadmap.",
    )
    refused = replace(
        source_listing("Data Intern", "https://jobs.example/globex/data-intern"),
        published_description="Location: London, United Kingdom",
    )

    class ContinuingBoard:
        def listings(
            self,
            _source_name: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=[],
                complete=False,
                raw_count=0,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=(target,),
            )

        def continue_listings(
            self,
            employer: str,
            targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            assert targets == (target,)
            return SourceRead(
                listings=[viable, refused],
                complete=True,
                raw_count=2,
                outcome=SourceReadOutcome.ATS_COMPLETE,
            )

    board = ContinuingBoard()

    class UnusedResolver:
        def resolve(self, *_args: Any, **_kwargs: Any) -> ResolvedPostingContent:
            raise AssertionError("continuation retained publisher bodies must not fetch again")

    content_resolution = PostingContentResolutionService(
        durable_database, UnusedResolver(), storage=runtime_storage(durable_database)
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=board,
        posting_fetcher=UnexpectedPostingFetcher(),
        content_resolution=content_resolution,
    )
    service.scan(workspace_id)
    with durable_database.session() as session:
        scan_id = session.scalar(select(SourceScan.id))
    assert scan_id is not None
    handlers = default_handlers(durable_database, fetcher=board)
    handlers["source.continue"] = SourceContinuationHandler(
        durable_database,
        board,
        service.sweep,
        lambda workspace_id, task_id, outcomes: service.record_task_outcomes(
            workspace_id, task_id, outcomes, origin="source.continue"
        ),
    )
    worker = Worker(
        durable_database,
        worker_id="source-continuation-refusal-worker",
        handlers=handlers,
    )

    assert worker.run_once()

    pending = {row.title: row for row in service.list_pending_discoveries(workspace_id)}
    assert set(pending) == {"Data Platform Lead"}
    assert pending["Data Platform Lead"].screening_standing != "refuse"
    with durable_database.session() as session:
        outcomes = list(
            session.scalars(
                select(SourceScanOutcome).where(SourceScanOutcome.workspace_id == workspace_id)
            )
        )
        dismissals = list(session.scalars(select(PendingDiscoveryDispositionEvent)))
    assert {outcome.title for outcome in outcomes} == {"Data Platform Lead", "Data Intern"}
    by_title = {outcome.title: outcome for outcome in outcomes}
    assert by_title["Data Platform Lead"].reason == "staged"
    assert by_title["Data Platform Lead"].terminal_stage == "staged"
    assert by_title["Data Intern"].reason == "insufficient_title_evidence"
    assert by_title["Data Intern"].terminal_stage == "screened"
    assert {outcome.origin for outcome in outcomes} == {"source.continue"}
    assert all(outcome.correlation_id == outcome.task_id for outcome in outcomes)
    assert dismissals == []


def _recording_continuation(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch, *, roles: int
) -> tuple[str, Worker, SourceContinuationHandler, list[int]]:
    """Seed a deferred page of `roles` engineers and `roles` interns, wired as production is."""
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            locations=[Location(key="canada", country="Canada")],
            countries=[CountryNames(country="Canada")],
            seniority=Seniority(target_titles=titles("engineer"), excluded_titles=["intern"]),
        ),
    )
    target = _continuation("https://jobs.example/globex/page-2", label="Page 2")
    listings = [
        replace(
            source_listing(f"{title} {index}", f"https://jobs.example/globex/{slug}-{index}"),
            published_description="Build the platform from Toronto, Canada.",
        )
        for index in range(roles)
        for title, slug in (("Platform Engineer", "engineer"), ("Platform Intern", "intern"))
    ]

    class PagedBoard:
        def listings(self, *_args: Any, **_options: Any) -> SourceRead:
            return SourceRead(
                listings=[],
                complete=False,
                raw_count=0,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=(target,),
            )

        def continue_listings(self, *_args: Any, **_options: Any) -> SourceRead:
            return SourceRead(
                listings=listings,
                complete=True,
                raw_count=len(listings),
                outcome=SourceReadOutcome.ATS_COMPLETE,
            )

    board = PagedBoard()
    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=board,
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)
    sizes: list[int] = []
    record = SourceScanService.record_task_outcomes

    def counted(service: SourceScanService, *args: Any, **options: Any) -> None:
        outcomes = tuple(args[2])
        if options.get("origin") == "source.continue":
            sizes.append(len(outcomes))
        record(service, args[0], args[1], outcomes, **options)

    monkeypatch.setattr(SourceScanService, "record_task_outcomes", counted)
    handlers = default_handlers(durable_database, fetcher=board)
    handler = cast(SourceContinuationHandler, handlers["source.continue"])
    worker = Worker(durable_database, worker_id="outcome-batch-worker", handlers=handlers)
    return workspace_id, worker, handler, sizes


def test_a_continuation_records_every_outcome_in_one_transaction(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id, worker, _handler, sizes = _recording_continuation(
        durable_database, monkeypatch, roles=4
    )

    assert worker.run_once()

    with durable_database.session() as session:
        rows = session.scalars(
            select(SourceScanOutcome).where(
                SourceScanOutcome.workspace_id == workspace_id,
                SourceScanOutcome.origin == "source.continue",
            )
        ).all()
    assert sizes == [8]
    assert len(rows) == 8
    assert {row.reason for row in rows} == {"staged", "insufficient_title_evidence"}


def test_an_abandoned_continuation_records_no_outcome(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id, worker, handler, sizes = _recording_continuation(
        durable_database, monkeypatch, roles=4
    )
    audit = listing_audit.audit_listings

    def abandon_then_audit(*args: Any, **options: Any) -> None:
        with durable_database.session() as session:
            task_id = session.scalar(
                select(BackgroundTask.id).where(BackgroundTask.task_type == "source.continue")
            )
        assert task_id is not None
        handler.recover_abandoned(task_id)
        audit(*args, **options)

    monkeypatch.setattr(sweep_module, "audit_listings", abandon_then_audit)
    assert worker.run_once()

    with durable_database.session() as session:
        rows = session.scalars(
            select(SourceScanOutcome).where(
                SourceScanOutcome.workspace_id == workspace_id,
                SourceScanOutcome.origin == "source.continue",
            )
        ).all()
    assert sizes == []
    assert rows == []


def test_continuation_task_cannot_process_another_workspaces_frontier(
    durable_database: Database,
) -> None:
    owner_workspace = _workspace(durable_database)
    other_workspace = str(
        WorkspaceService(durable_database, runtime_storage(durable_database))
        .create(slug="other-scan", name="Other scan")
        .id
    )
    _source(durable_database, owner_workspace, "Globex", "https://jobs.example/globex")
    target = _continuation("https://jobs.example/globex/lead")

    class CountingBoard:
        calls = 0

        def listings(
            self,
            _source_name: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return SourceRead(
                listings=[],
                complete=False,
                raw_count=0,
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=(target,),
            )

        def continue_listings(
            self,
            _employer: str,
            _targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            self.calls += 1
            raise AssertionError("cross-workspace task must fail before publisher I/O")

    board = CountingBoard()
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=board,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.scan(owner_workspace)
    queue = TaskQueueService(durable_database)
    with durable_database.session() as session:
        scan_id = session.scalar(select(SourceScan.id))
        continuation_id = session.scalar(select(SourceScanContinuation.id))
    assert continuation_id is not None
    assert scan_id is not None
    with durable_database.session() as session:
        correct_task_id = session.scalar(
            select(BackgroundTask.id).where(BackgroundTask.task_type == "source.continue")
        )
    assert correct_task_id is not None
    queue.request_cancellation(correct_task_id)
    from clearscout.application.failures import NotFoundError

    with pytest.raises(NotFoundError, match="does not belong"):
        queue.enqueue(
            workspace_id=other_workspace,
            task_type="source.continue",
            payload={"continuation_id": continuation_id, "generation": 1},
            idempotency_key="cross-workspace-continuation",
        )
    assert board.calls == 0
    with durable_database.session() as session:
        assert (
            session.scalar(
                select(BackgroundTask.id).where(BackgroundTask.workspace_id == other_workspace)
            )
            is None
        )


class _PublisherClient:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages

    def get(self, url: str, **_kwargs: Any) -> httpx.Response:
        return httpx.Response(200, text=self.pages[url], request=httpx.Request("GET", url))


def test_board_scan_continuation_admits_only_same_host_frontier(
    durable_database: Database,
) -> None:
    """A publisher hop keeps same-host roles and refuses every off-host anchor."""
    workspace_id = _workspace(durable_database)
    host = "https://careers.globex.example"
    _source(durable_database, workspace_id, "Globex", f"{host}/jobs")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    root = f'<main><p>Our openings.</p><a href="{host}/index">Openings</a></main>'
    index = (
        "<main><ul>"
        '<li><a href="/roles/data-lead">Data Platform Lead</a></li>'
        '<li><a href="/roles/ml-lead">ML Platform Lead</a></li>'
        '<li><a href="https://aggregator.example/globex">Aggregator</a></li>'
        '<li><a href="https://press.example/news">Press</a></li>'
        "</ul></main>"
    )
    publisher = PublisherSourceFetcher(
        client=cast(PoliteHttpClient, _PublisherClient({f"{host}/index": index})),
        limits=PublisherReadLimits(max_pages=1),
    )

    class PublisherBoard:
        last_read: SourceRead | None = None

        def listings(
            self,
            _source_name: str,
            url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            self.last_read = publisher.read("Globex", url, root)
            return self.last_read

        def continue_listings(
            self,
            employer: str,
            targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return publisher.continue_listings(
                employer, targets, root_url=root_url, handoff_scopes=handoff_scopes
            )

    board = PublisherBoard()
    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=board,
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    with durable_database.session() as session:
        frontier_urls = list(session.scalars(select(SourceScanFrontierItem.normalized_url)))
    assert frontier_urls
    assert all(url.startswith(host) for url in frontier_urls)
    assert any("data-lead" in url for url in frontier_urls)
    assert any("ml-lead" in url for url in frontier_urls)
    assert board.last_read is not None
    assert board.last_read.skip_reasons.get("off_host_link", 0) >= 1


class _DeferringBoard:
    """Defer one publisher link on the first read and list two roles on the follow-up."""

    def __init__(self, target: SourceContinuationTarget, listings: list[SourceListing]) -> None:
        self.target = target
        self.listings_read = listings

    def listings(
        self,
        _source_name: str,
        _url: str,
        *,
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        return SourceRead(
            listings=[self.listings_read[0]],
            complete=False,
            raw_count=1,
            outcome=SourceReadOutcome.CRAWL_CONTINUATION,
            deferred_targets=(self.target,),
        )

    def continue_listings(
        self,
        employer: str,
        targets: tuple[SourceContinuationTarget, ...],
        *,
        root_url: str = "",
        handoff_scopes: tuple[tuple[str, str, str], ...] = (),
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        return SourceRead(
            listings=self.listings_read[1:],
            complete=True,
            raw_count=len(self.listings_read) - 1,
            outcome=SourceReadOutcome.ATS_COMPLETE,
        )


def test_a_continued_posting_page_found_closed_withdraws_its_row(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    state_target_titles(durable_database, workspace_id, "Data Analyst")
    kept, closed = (
        replace(
            source_listing("Data Analyst", f"https://jobs.example/globex/{slug}"),
            published_description="Analyse clinical data.",
        )
        for slug in ("analyst-1", "analyst-2")
    )

    class ClosingBoard(_DeferringBoard):
        def continue_listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            return SourceRead(
                listings=[],
                complete=False,
                raw_count=1,
                outcome=SourceReadOutcome.PUBLISHER_LINKED_PAGES,
                attempted_target_urls=(self.target.url,),
                closed_posting_urls=(closed.url,),
            )

    class UnusedResolver:
        def resolve(self, *_args: Any, **_kwargs: Any) -> ResolvedPostingContent:
            raise AssertionError("a retained publisher body needs no fetch")

    resolution = PostingContentResolutionService(
        durable_database, UnusedResolver(), storage=runtime_storage(durable_database)
    )
    board = ClosingBoard(_continuation("https://jobs.example/globex/page-2"), [kept])
    services = [
        SourceScanService(
            durable_database,
            storage=runtime_storage(durable_database),
            fetcher=fetcher,
            posting_fetcher=UnexpectedPostingFetcher(),
            content_resolution=resolution,
        )
        for fetcher in (ScriptedBoards({"Globex": [kept, closed]}, complete=False), board)
    ]
    for service in services:
        service.scan(workspace_id)
    handlers = default_handlers(durable_database, fetcher=board)
    handlers["source.continue"] = SourceContinuationHandler(
        durable_database, board, services[1].sweep
    )
    worker = Worker(durable_database, worker_id="closed-continuation", handlers=handlers)
    while worker.run_once():
        pass

    with durable_database.session() as session:
        rows = {row.discovery_url: row for row in session.scalars(select(PendingDiscovery))}
        assert rows[kept.url].withdrawn_at is None
        assert rows[closed.url].withdrawn_at is not None
        evidence = session.get(PendingPostingContent, rows[closed.url].current_content_id)
        assert evidence is not None
        assert evidence.posting_status == PostingStatus.CLOSED
        [task] = session.scalars(
            select(BackgroundTask).where(BackgroundTask.task_type == "source.continue")
        ).all()
        assert task.progress["withdrawn_on_read"] == 1


def _continued_scan(durable_database: Database) -> tuple[str, str, str]:
    """Run one scan that defers a link and the continuation that reads it."""
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.example/globex")
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            locations=[Location(key="canada", country="Canada")],
            countries=[CountryNames(country="Canada")],
            seniority=Seniority(target_titles=titles("data"), excluded_titles=["intern"]),
        ),
    )
    board = _DeferringBoard(
        _continuation("https://jobs.example/globex/page-2", label="More roles"),
        [
            replace(
                source_listing("Data Platform Lead", "https://jobs.example/globex/data-lead"),
                published_description="Own the data platform.",
            ),
            replace(
                source_listing("Data Quality Lead", "https://jobs.example/globex/data-quality"),
                published_description="Own data quality.",
            ),
            replace(
                source_listing("Data Intern", "https://jobs.example/globex/data-intern"),
                published_description="Location: London, United Kingdom",
            ),
        ],
    )
    queue = TaskQueueService(durable_database)
    scan_task = str(
        queue.enqueue(
            workspace_id=workspace_id,
            task_type="source.scan",
            payload={},
            idempotency_key="scan-continuation-ledger",
        ).id
    )
    worker = Worker(
        durable_database,
        worker_id="continuation-ledger-worker",
        handlers=default_handlers(durable_database, fetcher=board),
    )
    while worker.run_once():
        pass
    assert queue.get(scan_task).status == "completed"
    with durable_database.session() as session:
        continuation_task = session.scalar(
            select(BackgroundTask.id).where(BackgroundTask.task_type == "source.continue")
        )
    assert continuation_task is not None
    return workspace_id, scan_task, continuation_task


def test_a_continuation_owns_its_rows_without_shrinking_the_scan_ledger(
    durable_database: Database,
) -> None:
    workspace_id, scan_task, continuation_task = _continued_scan(durable_database)
    queries = SourceScanQueries(durable_database)

    with durable_database.session() as session:
        by_task: dict[str, list[str]] = {}
        for outcome in session.scalars(select(SourceScanOutcome)):
            by_task.setdefault(outcome.task_id, []).append(outcome.title)
    scan_page = queries.list_scan_outcomes(workspace_id, task_id=scan_task)
    follow_up_page = queries.list_scan_outcomes(workspace_id, task_id=continuation_task)

    assert set(by_task[continuation_task]) == {"Data Quality Lead", "Data Intern"}
    assert "Data Platform Lead" in by_task[scan_task]
    assert scan_page.total == sum(len(rows) for rows in by_task.values())
    assert scan_page.follow_up_outcomes == 2
    assert {"Data Platform Lead", "Data Quality Lead", "Data Intern"} <= {
        item.title for item in scan_page.items
    }
    assert follow_up_page.total == 2
    assert follow_up_page.follow_up_outcomes == 0
    assert follow_up_page.capture_state == "complete"
    assert follow_up_page.reported_outcomes == 2
    assert scan_page.capture_state == "complete"
    assert scan_page.recorded_outcomes - scan_page.follow_up_outcomes == scan_page.reported_outcomes


def test_each_same_day_scan_task_reads_back_only_its_own_outcomes(
    durable_database: Database,
) -> None:
    workspace_id, first, second = same_day_rescans_with_follow_ups(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    first_page = service.list_scan_outcomes(workspace_id, task_id=first)
    second_page = service.list_scan_outcomes(workspace_id, task_id=second)

    assert {item.title for item in first_page.items} == {
        "Alpha Scan Lead",
        "Alpha Follow-up Lead",
        "Alpha Chained Lead",
    }
    assert first_page.follow_up_outcomes == 2
    assert {item.title for item in second_page.items} == {"Bravo Scan Lead", "Bravo Follow-up Lead"}
    assert second_page.recorded_outcomes == 2
    assert second_page.reasons == [FunnelReason.INSUFFICIENT_TITLE_EVIDENCE, FunnelReason.STAGED]


def test_a_continue_task_with_a_chained_successor_reads_back_only_its_own_outcomes(
    durable_database: Database,
) -> None:
    workspace_id, _, _ = same_day_rescans_with_follow_ups(durable_database)
    with durable_database.session() as session:
        first_follow_up = session.scalar(
            select(BackgroundTask.id).where(BackgroundTask.idempotency_key == "first-1")
        )
    assert first_follow_up is not None

    page = SourceScanQueries(durable_database).list_scan_outcomes(
        workspace_id, task_id=first_follow_up
    )

    assert {item.title for item in page.items} == {"Alpha Follow-up Lead"}
    assert page.follow_up_outcomes == 0


@pytest.mark.parametrize(
    ("narrowing", "expected"),
    [
        ({"search": "follow-up"}, {"Alpha Follow-up Lead"}),
        ({"reason": FunnelReason.HARD_CONSTRAINT_EXCLUDED}, {"Alpha Chained Lead"}),
        ({"source": "Globex"}, {"Alpha Scan Lead", "Alpha Follow-up Lead", "Alpha Chained Lead"}),
    ],
)
def test_narrowing_a_same_day_scan_task_stays_within_its_own_outcomes(
    durable_database: Database, narrowing: dict[str, Any], expected: set[str]
) -> None:
    workspace_id, first, _second = same_day_rescans_with_follow_ups(durable_database)

    page = SourceScanQueries(durable_database).list_scan_outcomes(
        workspace_id, task_id=first, filters=ScanOutcomeFilters(**narrowing)
    )

    assert {item.title for item in page.items} == expected


def test_a_rescanned_row_counts_only_its_latest_task_s_screened_out_listings(
    durable_database: Database,
) -> None:
    workspace_id, _first, second = same_day_rescans_with_follow_ups(durable_database)

    history = SourceScanQueries(durable_database).list_scan_history(workspace_id)

    assert [(item.task_id, item.screened_out_count) for item in history.items] == [(second, 1)]


@pytest.mark.parametrize("saved", ["", "/", "/jobs/search/", "/jobs"])
def test_a_jpaginate_source_counts_every_continued_posting_wherever_it_was_saved(
    durable_database: Database, saved: str
) -> None:
    from tests.loop.test_jpaginate_reader import _Portal

    class Portal(_Portal):
        def answer(self, url: str) -> httpx.Response:
            if urlsplit(url).path == "/jobs":
                url = self.origin + "/"
            return super().answer(url)

    workspace_id = _workspace(durable_database)
    site = Portal([[1, 2], [3, 4], [5, 6]])
    site.stop_after_detail = 4
    source_id = _source(durable_database, workspace_id, "Example", site.origin + saved)
    state_target_titles(durable_database, workspace_id, "Clinical Data Analyst")
    root = TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [source_id]},
        idempotency_key=new_id(),
    )
    worker = Worker(
        durable_database,
        worker_id="jpaginate-root-source",
        handlers=default_handlers(durable_database, fetcher=site.fetcher),
    )
    assert worker.run_once()
    site.stopped = False
    while worker.run_once():
        pass

    with durable_database.session() as session:
        scan = session.scalars(
            select(SourceScan).where(SourceScan.correlation_id == str(root.id))
        ).one()
        assert scan.raw_count == scan.read_count == 6
        assert scan.complete is True
        assert scan.status != "incomplete"


def test_a_same_day_listed_rescan_counts_bodies_its_earlier_task_completed(
    durable_database: Database,
) -> None:
    root = "https://jobs.jobvite.com/acme/jobs"
    identities = ("oAb12cDe", "oFg34hIj", "oKl56mNo", "oPq78rSt")
    urls = tuple(f"https://jobs.jobvite.com/acme/job/{identity}" for identity in identities)
    board_page = (
        '<table class="jv-job-list"><tbody>'
        + "".join(
            f'<tr><td class="jv-job-list-name"><a href="/acme/job/{identity}">'
            'Clinical Analyst</a></td><td class="jv-job-list-location">United States</td></tr>'
            for identity in identities
        )
        + "</tbody></table>"
    )
    posting = declaring_page(
        {
            "@context": "https://schema.org",
            "@type": "JobPosting",
            "title": "Clinical Analyst",
            "description": "Analyze clinical data for patient programs.",
            "jobLocation": {
                "@type": "Place",
                "address": {"@type": "PostalAddress", "addressCountry": "US"},
            },
        }
    )
    stop_after: list[str] = [urls[0]]
    requested: list[str] = []
    stopped = [False]

    def answer(url: str) -> httpx.Response:
        if url != root:
            requested.append(url)
            stopped[0] |= url in stop_after
        return httpx.Response(
            200, request=httpx.Request("GET", url), text=board_page if url == root else posting
        )

    class ListedPages(BoardApiFetcher):
        @staticmethod
        def deadline_elapsed() -> bool:
            return stopped[0]

    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", root)
    state_target_titles(durable_database, workspace_id, "Clinical Analyst")
    board = ListedPages(client=policy_client(answer))
    worker = _enqueue_scan(durable_database, workspace_id, board)
    assert worker.run_once()
    stopped[0], stop_after[:] = False, [urls[1]]
    assert worker.run_once()
    TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id, task_type="source.scan", payload={}, idempotency_key=new_id()
    )
    stopped[0], stop_after[:] = False, [urls[2]]
    requested.clear()
    assert worker.run_once()
    stopped[0], stop_after[:] = False, []
    while worker.run_once():
        stopped[0] = False

    scan = _board_scan_row(durable_database)
    assert (scan.raw_count, scan.read_count) == (4, 4)
    assert scan.complete is True


def test_a_same_day_rescan_counts_bodies_its_earlier_task_completed(
    durable_database: Database,
) -> None:
    from tests.loop.test_jpaginate_reader import _Portal

    workspace_id = _workspace(durable_database)
    site = _Portal([[1, 2], [3, 4], [5, 6]])
    site.stop_after_detail = 2
    _source(durable_database, workspace_id, "Example", site.origin + "/")
    state_target_titles(durable_database, workspace_id, "Clinical Data Analyst")
    worker = _enqueue_scan(durable_database, workspace_id, site.fetcher)
    assert worker.run_once()
    site.stopped, site.stop_after_detail = False, 4
    assert worker.run_once()
    TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id, task_type="source.scan", payload={}, idempotency_key=new_id()
    )
    site.stopped, site.stop_after_detail = False, 5
    assert worker.run_once()
    site.stopped, site.stop_after_detail = False, None
    while worker.run_once():
        pass

    scan = _board_scan_row(durable_database)
    assert scan.raw_count == scan.read_count == 6
    assert scan.complete is True


_LARGE_LISTING = 200


def _stored_listing_pairs(database: Database) -> int:
    """Count the completed, protected and membership pairs every frontier row stores."""
    with database.session() as session:
        checkpoints = [
            SourceDetailCheckpoint.from_json(row.detail_checkpoint)
            for row in session.scalars(select(SourceScanFrontierItem))
            if row.detail_checkpoint is not None
        ]
    return sum(
        len(checkpoint.completed) + len(checkpoint.protected) + len(checkpoint.listing_urls)
        for checkpoint in checkpoints
    )


# A page stop leaves a cursor, so the later detail stop defers bodies from a continue task;
# without one, the later stop interrupts the retries of bodies the scan task deferred.
# A scan whose first task holds a page cursor stays uncertified on this portal.
@pytest.mark.parametrize(
    ("page_stop", "detail_stops", "certified"),
    [(None, (100, 150), True), (5, (None, 150), False)],
)
def test_a_publisher_search_frontier_stores_listing_pairs_linear_in_the_listing(
    durable_database: Database,
    page_stop: int | None,
    detail_stops: tuple[int | None, int],
    certified: bool,
) -> None:
    stored = _drain_large_portal_scan(durable_database, page_stop, detail_stops)

    scan = _board_scan_row(durable_database)
    assert scan.raw_count == scan.read_count == _LARGE_LISTING
    assert scan.complete is certified
    assert max(stored) <= 4 * _LARGE_LISTING, stored


def _drain_large_portal_scan(
    database: Database,
    page_stop: int | None,
    detail_stops: tuple[int | None, int],
    alter_after_scan: Any = None,
) -> list[int]:
    """Scan a large portal to a drained frontier; return the pairs stored after each task."""
    from tests.loop.test_jpaginate_reader import _Portal

    workspace_id = _workspace(database)
    site = _Portal([list(range(job, job + 20)) for job in range(1, _LARGE_LISTING + 1, 20)])
    site.stop_after_page, site.stop_after_detail = page_stop, detail_stops[0]
    _source(database, workspace_id, "Example", site.origin + "/")
    state_target_titles(database, workspace_id, "Clinical Data Analyst")
    worker = _enqueue_scan(database, workspace_id, site.fetcher)
    assert worker.run_once()
    if alter_after_scan is not None:
        with database.session() as session:
            alter_after_scan(session)
    stored = [_stored_listing_pairs(database)]
    site.stopped, site.stop_after_detail = False, detail_stops[1]
    while worker.run_once():
        site.stopped = False
        stored.append(_stored_listing_pairs(database))
    return stored


def _stamp_listing_on_detail_rows(session: Any) -> None:
    """Give every pending search-detail row the whole listing, as older builds stored it."""
    rows = session.scalars(select(SourceScanFrontierItem)).all()
    listing = next(
        SourceDetailCheckpoint.from_json(row.detail_checkpoint)
        for row in rows
        if row.detail_checkpoint is not None
        and str(row.detail_checkpoint["platform"]).startswith("search:")
    )
    for row in rows:
        if row.deferred_reason.endswith("_search_detail") and row.detail_checkpoint is not None:
            row.detail_checkpoint = replace(
                SourceDetailCheckpoint.from_json(row.detail_checkpoint),
                listing_ids=listing.listing_ids,
                listing_urls=listing.listing_urls,
                completed=listing.completed,
                protected=listing.protected,
            ).as_json()


def test_a_retried_detail_row_drops_the_listing_an_older_checkpoint_carried(
    durable_database: Database,
) -> None:
    stored = _drain_large_portal_scan(
        durable_database, None, (100, 150), alter_after_scan=_stamp_listing_on_detail_rows
    )

    scan = _board_scan_row(durable_database)
    assert scan.raw_count == scan.read_count == _LARGE_LISTING
    assert scan.complete is True
    assert stored[0] > _LARGE_LISTING**2 // 4
    assert stored[-1] <= 4 * _LARGE_LISTING, stored


def _adopted_jpaginate_rescan(
    database: Database,
    second_stop: int,
    alter_carried: Any = None,
    alter_after_root: Any = None,
) -> tuple[str, str, str]:
    """Drain an uncertified first scan, then rescan the same day under a new root task."""
    from tests.loop.test_jpaginate_reader import _Portal

    workspace_id = _workspace(database)
    site = _Portal([[1, 2], [3, 4], [5, 6]])
    site.total = 7
    site.stop_after_detail = 4
    source_id = _source(database, workspace_id, "Example", site.origin + "/")
    state_target_titles(database, workspace_id, "Clinical Data Analyst")
    unlisted = site.job_url(99)
    _stage_known_rows(database, workspace_id, source_id, (unlisted,))
    worker = _enqueue_scan(database, workspace_id, site.fetcher)
    assert worker.run_once()
    site.stopped = False
    while worker.run_once():
        pass
    first = _board_scan_row(database).correlation_id
    assert first is not None
    if alter_carried is not None:
        with database.session() as session:
            alter_carried(session, site)
    site.total, site.stop_after_detail = 6, second_stop
    second = TaskQueueService(database).enqueue(
        workspace_id=workspace_id, task_type="source.scan", payload={}, idempotency_key=new_id()
    )
    assert worker.run_once()
    if alter_after_root is not None:
        with database.session() as session:
            alter_after_root(session, site)
    site.stopped = False
    while worker.run_once():
        pass
    with database.session() as session:
        member = session.scalars(
            select(SourceScanMember).where(SourceScanMember.root_task_id == str(second.id))
        ).one()
        assert member.state != "already_in_progress"
    return first, str(second.id), unlisted


# A stop after three bodies leaves a continue task; after four, nothing is pending at root time.
def _premark_unlisted(session: Any, site: Any) -> None:
    _mark_checked_absent(
        session,
        session.scalars(
            select(PendingDiscovery).where(PendingDiscovery.discovery_url == site.job_url(99))
        ).all(),
    )


@pytest.mark.parametrize("premark", [False, True])
@pytest.mark.parametrize("second_stop", [3, 4])
def test_an_adopted_same_day_rescan_certifies_from_bodies_its_earlier_task_completed(
    durable_database: Database, second_stop: int, premark: bool
) -> None:
    first, second, unlisted = _adopted_jpaginate_rescan(
        durable_database, second_stop, _premark_unlisted if premark else None
    )

    with durable_database.session() as session:
        rows = session.scalars(select(SourceScanFrontierItem)).all()
        assert {row.correlation_id for row in rows} == {first, second}
        assert all(
            row.correlation_id == first
            for row in rows
            if row.deferred_reason == POSTING_DETAIL_COMPLETED
            and row.status == "completed"
            and row.completed_at is not None
            and SourceDetailCheckpoint.from_json(row.detail_checkpoint).listing_total == 7
        )
    scan = _board_scan_row(durable_database)
    assert scan.raw_count == scan.read_count == 6
    assert scan.complete is True
    assert scan.listing_pages > 1
    assert _withdrawn_urls(durable_database) == ({unlisted} if premark else set())
    with durable_database.session() as session:
        absent_since = session.scalars(
            select(PendingDiscovery.absent_since).where(PendingDiscovery.discovery_url == unlisted)
        ).one()
        assert (absent_since is None) is premark


def _drop_carried_body(session: Any, site: Any) -> None:
    for row in session.scalars(select(SourceScanFrontierItem)):
        checkpoint = SourceDetailCheckpoint.from_json(row.detail_checkpoint)
        completed = tuple(pair for pair in checkpoint.completed if pair[0] != "6")
        row.detail_checkpoint = replace(checkpoint, completed=completed).as_json()


def _move_carried_body(session: Any, site: Any) -> None:
    for row in session.scalars(select(SourceScanFrontierItem)):
        checkpoint = SourceDetailCheckpoint.from_json(row.detail_checkpoint)
        completed = tuple(
            (identity, site.job_url(66) if identity == "6" else url)
            for identity, url in checkpoint.completed
        )
        row.detail_checkpoint = replace(checkpoint, completed=completed).as_json()


def _age_carried_body(session: Any, site: Any) -> None:
    # A day-old body stays inside the rescreen window, so only the scan date can refuse it.
    _append_parameters(session, utc_now() - timedelta(days=2))
    for row in session.scalars(select(SourceScanFrontierItem)):
        if row.completed_at is not None:
            row.completed_at -= timedelta(days=1)


def _age_carried_bodies_but_one(session: Any, site: Any) -> None:
    # One same-day body row must not vouch for siblings whose own rows are a day old.
    _append_parameters(session, utc_now() - timedelta(days=2))
    fresh = site.job_url(6)
    for row in session.scalars(select(SourceScanFrontierItem)):
        if row.completed_at is not None and row.normalized_url != normalize_fetch_url(fresh):
            row.completed_at -= timedelta(days=1)


@pytest.mark.parametrize("second_stop", [3, 4])
@pytest.mark.parametrize(
    "alter",
    [_drop_carried_body, _move_carried_body, _age_carried_body, _age_carried_bodies_but_one],
)
def test_an_adopted_rescan_without_a_current_same_day_body_for_each_identity_stays_incomplete(
    durable_database: Database, second_stop: int, alter: Any
) -> None:
    _adopted_jpaginate_rescan(durable_database, second_stop, alter)

    scan = _board_scan_row(durable_database)
    assert scan.complete is False
    assert _withdrawn_urls(durable_database) == set()


def _supersede_parameters(session: Any, site: Any) -> None:
    _append_parameters(session, utc_now())


def _append_parameters(session: Any, effective_at: datetime) -> None:
    """Append the latest parameters again as a new version effective at `effective_at`."""
    latest = session.scalars(
        select(JobSearchParameterSet).order_by(JobSearchParameterSet.version.desc()).limit(1)
    ).one()
    session.add(
        JobSearchParameterSet(
            workspace_id=latest.workspace_id,
            version=latest.version + 1,
            content_sha256=latest.content_sha256,
            effective_at=effective_at,
            supersedes_id=latest.id,
            document=latest.document,
        )
    )


# The rescan's restart reads again what predates new parameters, so they land after it restarts.
def test_an_adopted_rescan_refuses_same_day_bodies_completed_before_the_parameters_changed(
    durable_database: Database,
) -> None:
    _adopted_jpaginate_rescan(durable_database, 3, alter_after_root=_supersede_parameters)

    scan = _board_scan_row(durable_database)
    assert scan.complete is False
    assert _withdrawn_urls(durable_database) == set()


def _add_unscoped_body(session: Any, url: str) -> None:
    # The body a recent read supplied lands as a bare completed row beside the page receipt.
    page = session.scalars(
        select(SourceScanFrontierItem).where(SourceScanFrontierItem.deferred_reason == "")
    ).first()
    assert page is not None
    session.add(
        read_page_item(
            page.continuation_id,
            normalize_fetch_url(url),
            url,
            correlation_id=page.correlation_id,
        )
    )


def _unscope_member_body(session: Any, site: Any) -> None:
    _add_unscoped_body(session, site.job_url(1))


def _unscope_stranger_body(session: Any, site: Any) -> None:
    _add_unscoped_body(session, site.job_url(77))


# A recent read leaves a member body's row with no scope; only a non-member one refuses.
@pytest.mark.parametrize(
    ("alter", "certified"), [(_unscope_member_body, True), (_unscope_stranger_body, False)]
)
def test_an_adopted_rescan_certifies_past_an_unscoped_row_only_when_it_is_a_member(
    durable_database: Database, alter: Any, certified: bool
) -> None:
    _adopted_jpaginate_rescan(durable_database, 3, alter_after_root=alter)

    assert _board_scan_row(durable_database).complete is certified


@pytest.mark.parametrize(("declared", "certified"), [(6, True), (7, False)])
def test_a_time_limited_jpaginate_drain_certifies_only_a_listing_that_meets_its_total(
    durable_database: Database, declared: int, certified: bool
) -> None:
    from tests.loop.test_jpaginate_reader import _Portal

    workspace_id = _workspace(durable_database)
    site = _Portal([[1, 2], [3, 4], [5, 6]])
    site.total = declared
    site.stop_after_detail = 4
    source_id = _source(durable_database, workspace_id, "Example", site.origin + "/")
    state_target_titles(durable_database, workspace_id, "Clinical Data Analyst")
    unlisted = site.job_url(99)
    _stage_known_rows(durable_database, workspace_id, source_id, (unlisted,))
    _missed_once(durable_database, unlisted)
    worker = _enqueue_scan(durable_database, workspace_id, site.fetcher)
    assert worker.run_once()
    site.stopped = False
    while worker.run_once():
        pass

    scan = _board_scan_row(durable_database)
    assert scan.raw_count == scan.read_count == 6
    assert scan.complete is certified
    assert (scan.status == "incomplete") is not certified
    assert _withdrawn_urls(durable_database) == ({unlisted} if certified else set())


def test_a_drained_jpaginate_listing_missing_one_body_receipt_stays_incomplete(
    durable_database: Database,
) -> None:
    from tests.loop.test_jpaginate_reader import _Portal

    workspace_id = _workspace(durable_database)
    site = _Portal([[1, 2], [3, 4], [5, 6]])
    site.total = 6
    site.stop_after_detail = 4
    source_id = _source(durable_database, workspace_id, "Example", site.origin + "/")
    state_target_titles(durable_database, workspace_id, "Clinical Data Analyst")
    unlisted = site.job_url(99)
    _stage_known_rows(durable_database, workspace_id, source_id, (unlisted,))
    worker = _enqueue_scan(durable_database, workspace_id, site.fetcher)
    assert worker.run_once()
    with durable_database.session() as session:
        for row in session.scalars(select(SourceScanFrontierItem)):
            checkpoint = SourceDetailCheckpoint.from_json(row.detail_checkpoint)
            completed = tuple(pair for pair in checkpoint.completed if pair[0] != "1")
            row.detail_checkpoint = replace(checkpoint, completed=completed).as_json()
    site.stopped = False
    while worker.run_once():
        pass

    with durable_database.session() as session:
        rows = session.scalars(select(SourceScanFrontierItem)).all()
        assert {row.status for row in rows} == {"completed"}
        assert all(
            ("1", site.job_url(1))
            not in SourceDetailCheckpoint.from_json(row.detail_checkpoint).completed
            for row in rows
        )
    scan = _board_scan_row(durable_database)
    assert scan.complete is False
    assert scan.status == "incomplete"
    assert _withdrawn_urls(durable_database) == set()


@pytest.mark.parametrize(
    ("served", "complete", "offsets"),
    [(200, True, ["50", "100", "150"]), (100, False, ["50", "100"])],
)
def test_an_oracle_chain_credits_each_page_cursor_once_and_ends_at_the_kept_total(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    served: int,
    complete: bool,
    offsets: list[str],
) -> None:
    monkeypatch.setattr(oracle, "ORACLE_MAX_PAGES", 2)
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", _ORACLE_SITE)
    board = _OracleBoard({at: list(range(at, at + 25)) for at in range(0, served, 25)}, 200)

    class Reader:
        def listings(self, employer: str, url: str, **kwargs: Any) -> SourceRead:
            return board.listings(employer, url, **kwargs)

        def continue_listings(self, employer: str, targets: Any, **kwargs: Any) -> SourceRead:
            return board.continue_listings(employer, targets, **kwargs)

    worker = _enqueue_scan(durable_database, workspace_id, cast(SourceReader, Reader()))

    for _run in range(40):
        if not worker.run_once():
            break

    with durable_database.session() as session:
        rows = session.scalars(select(SourceScanFrontierItem)).all()
        assert sorted(
            (row.url.split("offset=")[1], row.status, row.attempt_count) for row in rows
        ) == sorted((offset, "completed", 1) for offset in offsets)
    scan = _board_scan_row(durable_database)
    assert scan.complete is complete
    assert scan.discovery_truncated is not complete


@pytest.mark.parametrize(("hidden", "complete"), [({}, True), ({"31": 4}, False)])
def test_an_oracle_chain_past_the_offset_window_certifies_only_a_union_matching_its_facets(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    hidden: dict[str, int],
    complete: bool,
) -> None:
    monkeypatch.setattr(oracle, "ORACLE_WINDOW", 100)
    monkeypatch.setattr(oracle, "ORACLE_MAX_PAGES", 2)
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", _ORACLE_SITE)
    board = _facet_board(hidden=hidden)

    class Reader:
        def listings(self, employer: str, url: str, **kwargs: Any) -> SourceRead:
            return board.listings(employer, url, **kwargs)

        def continue_listings(self, employer: str, targets: Any, **kwargs: Any) -> SourceRead:
            return board.continue_listings(employer, targets, **kwargs)

    worker = _enqueue_scan(durable_database, workspace_id, cast(SourceReader, Reader()))

    for _run in range(40):
        if not worker.run_once():
            break

    assert board.requests[-1] == ("31", 50 if hidden else 25)
    assert len(set(board.requests)) == len(board.requests)
    scan = _board_scan_row(durable_database)
    assert scan.complete is complete
    assert scan.discovery_truncated is not complete


def _jobvite_checkpoint(url: str, *ids: str, pending: bool) -> dict[str, object]:
    return SourceDetailCheckpoint(
        platform="jobvite",
        board_url=url,
        pending_ids=(ids[0],) if pending else (),
        listing_ids=ids,
        listing_urls=tuple((each, f"https://jobs.jobvite.com/acme/job/{each}") for each in ids),
    ).as_json()


def _superseded_scans(database: Database) -> tuple[str, str, str, str, str]:
    """Seed two earlier scans holding checkpoints; return ids and three posting URLs."""
    workspace_id = _workspace(database)
    root = "https://jobs.jobvite.com/acme/jobs"
    source_id = _source(database, workspace_id, "Acme", root)
    oldest = "https://jobs.jobvite.com/acme/job/a1"
    older = "https://jobs.jobvite.com/acme/job/b1"
    owed = "https://jobs.jobvite.com/acme/job/b2"
    completed = {
        "source_url": root,
        "status": "completed",
        "deferred_reason": POSTING_DETAIL_COMPLETED,
    }
    _seed_continuation(
        database,
        workspace_id,
        _scan_row(database, source_id, day=-2),
        [
            {
                **completed,
                "url": oldest,
                "detail_checkpoint": _jobvite_checkpoint(oldest, "a1", "a2", pending=False),
            }
        ],
    )
    _seed_continuation(
        database,
        workspace_id,
        _scan_row(database, source_id, day=-1),
        [
            {
                **completed,
                "url": older,
                "detail_checkpoint": _jobvite_checkpoint(older, "b1", "b2", pending=False),
            },
            {
                "url": owed,
                "source_url": root,
                "deferred_reason": "jobvite_posting_pending",
                "detail_checkpoint": _jobvite_checkpoint(owed, "b2", "b1", pending=True),
            },
        ],
    )
    return workspace_id, source_id, oldest, older, owed


def _earlier_frontier(
    database: Database, source_id: str, scan_id: str
) -> list[tuple[object, str | None]]:
    with database.session() as session:
        return [
            (row.stored_checkpoint, row.listing_membership_sha256)
            for row in session.scalars(
                select(SourceScanFrontierItem)
                .join(SourceScanContinuation)
                .join(SourceScan)
                .where(SourceScan.source_id == source_id, SourceScan.id != scan_id)
            )
        ]


def _membership_digests(database: Database) -> set[str]:
    with database.session() as session:
        return set(session.scalars(select(SourceListingMembership.sha256)))


def test_a_newer_scan_of_a_source_releases_the_checkpoints_its_earlier_scans_left(
    durable_database: Database,
) -> None:
    workspace_id, source_id, oldest, older, owed = _superseded_scans(durable_database)
    scan_id = _scan_row(durable_database, source_id, day=0)

    with durable_database.session() as session:
        result = SourceContinuationCommands(durable_database).replace_frontier_in_session(
            session,
            workspace_id=workspace_id,
            source_scan_id=scan_id,
            targets=(),
            correlation_id=None,
            carry_forward=True,
            handoff_scopes=(),
        )

    assert result.continuation_id is not None
    [carried] = [row for row in _rows(durable_database, result.continuation_id) if row.url == owed]
    assert carried.status == "pending"
    assert carried.detail_checkpoint is not None
    assert carried.detail_checkpoint["listing_ids"] == ["b2", "b1"]
    assert _earlier_frontier(durable_database, source_id, scan_id) == [(None, None)] * 3
    assert _membership_digests(durable_database) == {carried.listing_membership_sha256}
    with durable_database.session() as session:
        reads = recent_posting_reads(session, source_id, utc_now() - timedelta(days=30))
    assert {oldest, older} <= set(reads)


def test_a_newer_scan_with_nothing_to_carry_still_releases_earlier_checkpoints(
    durable_database: Database,
) -> None:
    workspace_id, source_id, _, _, _ = _superseded_scans(durable_database)
    scan_id = _scan_row(durable_database, source_id, day=0)

    with durable_database.session() as session:
        result = SourceContinuationCommands(durable_database).replace_frontier_in_session(
            session,
            workspace_id=workspace_id,
            source_scan_id=scan_id,
            targets=(),
            correlation_id=None,
            carry_forward=False,
            handoff_scopes=(),
        )

    assert result.continuation_id is None
    assert _earlier_frontier(durable_database, source_id, scan_id) == [(None, None)] * 3
    assert _membership_digests(durable_database) == set()


def test_a_same_day_rescan_keeps_its_completed_posting_checkpoints(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    root = "https://jobs.jobvite.com/acme/jobs"
    source_id = _source(durable_database, workspace_id, "Acme", root)
    scan_id = _scan_row(durable_database, source_id, day=0)
    read = "https://jobs.jobvite.com/acme/job/c1"
    continuation_id = _seed_continuation(
        durable_database,
        workspace_id,
        scan_id,
        [
            {
                "url": read,
                "source_url": root,
                "status": "completed",
                "deferred_reason": POSTING_DETAIL_COMPLETED,
                "detail_checkpoint": _jobvite_checkpoint(read, "c1", "c2", pending=False),
            }
        ],
    )

    with durable_database.session() as session:
        SourceContinuationCommands(durable_database).replace_frontier_in_session(
            session,
            workspace_id=workspace_id,
            source_scan_id=scan_id,
            targets=(),
            correlation_id=None,
            carry_forward=True,
            handoff_scopes=(),
        )

    [kept] = _rows(durable_database, continuation_id)
    assert kept.status == "completed"
    assert kept.listing_membership_sha256 in _membership_digests(durable_database)
    assert kept.detail_checkpoint is not None
    assert kept.detail_checkpoint["listing_ids"] == ["c1", "c2"]


def test_a_whole_continuation_load_reads_a_shared_membership_apart_from_its_rows(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    root = "https://jobs.jobvite.com/acme/jobs"
    source_id = _source(durable_database, workspace_id, "Acme", root)
    scan_id = _scan_row(durable_database, source_id, day=0)
    ids = ("d1", "d2", "d3", "d4")
    continuation_id = _seed_continuation(
        durable_database,
        workspace_id,
        scan_id,
        [
            {
                "url": f"https://jobs.jobvite.com/acme/job/{each}",
                "source_url": root,
                "status": "completed",
                "deferred_reason": POSTING_DETAIL_COMPLETED,
                "detail_checkpoint": _jobvite_checkpoint(
                    f"https://jobs.jobvite.com/acme/job/{each}", *ids, pending=False
                ),
            }
            for each in ids
        ],
    )
    membership_reads: list[str] = []

    def record(_conn: object, _cursor: object, statement: str, *_args: object) -> None:
        if statement.startswith("SELECT") and "source_listing_memberships" in statement:
            membership_reads.append(statement)

    event.listen(durable_database.engine, "before_cursor_execute", record)
    try:
        with durable_database.exclusive_session() as session:
            SourceContinuationCommands(durable_database).replace_frontier_in_session(
                session,
                workspace_id=workspace_id,
                source_scan_id=scan_id,
                targets=(),
                correlation_id=None,
                carry_forward=True,
                handoff_scopes=(),
            )
    finally:
        event.remove(durable_database.engine, "before_cursor_execute", record)

    assert membership_reads
    assert not [each for each in membership_reads if "source_scan_frontier_items" in each]
    assert {row.listing_membership_sha256 for row in _rows(durable_database, continuation_id)} == (
        _membership_digests(durable_database)
    )


def test_selected_frontier_rows_read_their_shared_membership_once(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    root = "https://jobs.jobvite.com/acme/jobs"
    source_id = _source(durable_database, workspace_id, "Acme", root)
    scan_id = _scan_row(durable_database, source_id, day=0)
    ids = ("d1", "d2", "d3")
    continuation_id = _seed_continuation(
        durable_database,
        workspace_id,
        scan_id,
        [
            {
                "url": f"https://jobs.jobvite.com/acme/job/{each}",
                "source_url": root,
                "status": "completed",
                "deferred_reason": POSTING_DETAIL_COMPLETED,
                "detail_checkpoint": _jobvite_checkpoint(
                    f"https://jobs.jobvite.com/acme/job/{each}", *ids, pending=False
                ),
            }
            for each in ids
        ],
    )
    membership_reads: list[str] = []

    def record(_conn: object, _cursor: object, statement: str, *_args: object) -> None:
        if statement.startswith("SELECT") and "source_listing_memberships" in statement:
            membership_reads.append(statement)

    event.listen(durable_database.engine, "before_cursor_execute", record)
    try:
        with durable_database.exclusive_session() as session:
            rows = session.scalars(
                select_frontier_rows().where(
                    SourceScanFrontierItem.continuation_id == continuation_id
                )
            ).all()
            checkpoints = [row.detail_checkpoint for row in rows]
    finally:
        event.remove(durable_database.engine, "before_cursor_execute", record)

    assert len(membership_reads) == 1
    assert "source_scan_frontier_items" not in membership_reads[0]
    assert len(checkpoints) == len(ids)
    assert all(each is not None and each["listing_ids"] == list(ids) for each in checkpoints)
