"""One scan sweep: which boards it reads, what it admits or dismisses, and what it withdraws."""

from __future__ import annotations

import contextlib
import hashlib
import html
import json
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, ClassVar, cast

import httpx
import pytest
from sqlalchemy import select

from clearscout.application.discovery import scan_sweep
from clearscout.application.discovery.board_scan import SourceScanHandler, SourceScanService
from clearscout.application.discovery.content_resolution import (
    CurrentPostingContentReader,
    PostingContentResolutionService,
    ResolvedPostingContent,
    usable_current_evidence,
)
from clearscout.application.discovery.pending import reset_protected_ids
from clearscout.application.discovery.posting_fetch import FetchedPosting
from clearscout.application.discovery.posting_text import publisher_closing_date
from clearscout.application.discovery.scan_queries.history import QUERY_LABEL
from clearscout.application.discovery.scan_sweep import (
    SCAN_LANES,
    SOURCE_STAGE_LIMIT,
)
from clearscout.application.discovery.scan_sweep import sweep as sweep_module
from clearscout.application.discovery.scan_sweep.models import ScanInputs
from clearscout.application.discovery.source_reading import (
    PostingCheck,
    SourceContinuationTarget,
    SourceDetailCheckpoint,
    SourceListing,
    SourceRead,
    SourceReadOptions,
)
from clearscout.application.failures import (
    ConflictError,
    NotFoundError,
    UnavailableError,
)
from clearscout.application.tasks import TaskQueueService
from clearscout.application.workspaces import SearchParametersService
from clearscout.bootstrap.worker import TaskCancelledError, Worker, default_handlers
from clearscout.domain import (
    PostingContentAccessStatus,
    PostingContentExtractionMethod,
    PostingIdentityStatus,
    PostingStatus,
    SearchProposalState,
    SourceKind,
    SourceReadOutcome,
    new_id,
    utc_now,
)
from clearscout.domain.artifacts import PostingSource
from clearscout.domain.identity import normalize_text, normalize_url
from clearscout.domain.records import PostingReadIntent
from clearscout.domain.search_parameters import (
    Acceptance,
    Arrangement,
    CountryNames,
    EmployerArchetype,
    Employers,
    JobSearchParameters,
    Location,
    Seniority,
)
from clearscout.domain.staged_facts import ListingIdentityTrust, StagedListingFacts
from clearscout.infrastructure.artifacts.store import file_uri_path
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    Artifact,
    AuditEvent,
    CandidateProfile,
    DerivedSearchTitle,
    Employer,
    JobPosting,
    MasterCvVersion,
    ModelCall,
    PendingDiscovery,
    PendingDiscoveryDispositionEvent,
    PendingPostingContent,
    PostingIdentifier,
    PostingObservation,
    PostingUrl,
    Source,
    SourceScan,
    SourceScanFrontierItem,
    SourceScanOutcome,
)
from clearscout.infrastructure.web_fetch.board_adapters import BoardApiFetcher
from clearscout.infrastructure.web_fetch.board_mappings.dayforce import DAYFORCE_MAPPING
from clearscout.infrastructure.web_fetch.board_mappings.greenhouse import GREENHOUSE_MAPPING
from clearscout.infrastructure.web_fetch.board_mappings.mapper import source_listings
from clearscout.infrastructure.web_fetch.ingest.url import workday_posting_detail
from clearscout.infrastructure.web_fetch.listing_facts import (
    published_listing_facts,
)
from clearscout.infrastructure.web_fetch.policy import (
    DestinationUnresolvableError,
    InvalidFetchUrlError,
    PoliteHttpClient,
    RobotsDeniedError,
    RobotsUnavailableError,
)
from clearscout.infrastructure.web_fetch.posting_resolution import (
    DeterministicPostingContentResolver,
)
from clearscout.infrastructure.web_fetch.publisher_sources import PublisherSourceFetcher
from clearscout.infrastructure.web_fetch.rendered import RenderRuntimeUnavailableError
from clearscout.shared.errors import IngestError
from tests.support.artifacts import runtime_storage
from tests.support.doubles import (
    ScriptedBoards,
    ScriptedPage,
    UnexpectedContentResolver,
    UnexpectedPostingFetcher,
    declaring_page,
)
from tests.support.http import paylocity_board_page, paylocity_page_job, policy_client
from tests.support.scenarios import (
    board_source as _source,
)
from tests.support.scenarios import (
    dismiss_pending_discovery,
    mark_reset_archived,
    pending_posting_content,
    state_single_market,
    titles,
    tracked_board_posting,
)
from tests.support.scenarios import (
    scan_workspace as _workspace,
)
from tests.support.scenarios import (
    seed_pending_discovery as _seed_pending,
)
from tests.support.scenarios import (
    source_listing as _listing,
)
from tests.support.scenarios import (
    state_target_titles as _state_titles,
)


def _accept_title(database: Database, workspace_id: str, title: str) -> None:
    """Record one accepted search-vocabulary title against the workspace's current CV version."""
    with database.session() as session:
        profile = CandidateProfile(workspace_id=workspace_id, display_name="Candidate")
        session.add(profile)
        session.flush()
        version = MasterCvVersion(
            candidate_profile_id=profile.id,
            content_sha256="0" * 64,
            source_uri="file:///master.md",
            effective_at=utc_now(),
            byte_size=1,
        )
        session.add(version)
        session.flush()
        session.add(
            DerivedSearchTitle(
                id=new_id(),
                workspace_id=workspace_id,
                master_cv_version_id=version.id,
                title=title,
                normalized_title=normalize_text(title),
                reason="Accepted for this scan.",
                evidence_ids=[],
                state=SearchProposalState.ACCEPTED.value,
                position=0,
            )
        )


@pytest.mark.parametrize("complete", [True, False])
def test_scripted_board_reader_accepts_the_protocol_employer_keyword(complete: bool) -> None:
    reader = ScriptedBoards({"Globex": []}, complete=complete)

    result = reader.listings(employer="Globex", url="https://publisher.example/jobs")

    assert result.listings == []
    assert result.complete is complete
    assert result.raw_count == 0
    assert result.outcome is (
        SourceReadOutcome.ATS_COMPLETE if complete else SourceReadOutcome.CRAWL_CONTINUATION
    )


def test_scan_stages_new_listings_from_derived_employer_boards(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            seniority=Seniority(target_titles=titles("data"), excluded_titles=["intern"])
        ),
    )
    boards = ScriptedBoards(
        {
            "Acme": [
                _listing("Data Platform Lead", "https://boards.greenhouse.io/acme/jobs/1"),
                _listing("Data Intern", "https://boards.greenhouse.io/acme/jobs/2"),
                _listing("Sales Director", "https://boards.greenhouse.io/acme/jobs/3"),
            ],
        }
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=boards,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    result = service.scan(workspace_id)

    assert result["new_discoveries"] == 1
    assert [item["title"] for item in result["discoveries"]] == ["Data Platform Lead"]
    assert "manual" not in result
    with durable_database.session() as session:
        pending = session.scalars(select(PendingDiscovery)).all()
        assert sorted(item.title for item in pending) == ["Data Platform Lead"]
        scans = session.scalars(select(SourceScan)).all()
        assert [
            (item.status, item.read_count, item.result_count, item.new_count) for item in scans
        ] == [("completed", 3, 1, 1)]

    projected = {item.title: item for item in service.list_pending_discoveries(workspace_id)}
    assert list(projected) == ["Data Platform Lead"]
    assert any(
        outcome["title"] == "Sales Director" and outcome["reason"] == "insufficient_title_evidence"
        for outcome in result["funnel"]["outcomes"]
    )
    assert any(
        outcome["title"] == "Data Intern" and outcome["reason"] == "insufficient_title_evidence"
        for outcome in result["funnel"]["outcomes"]
    )

    # A second scan the same day re-reads the board and stages nothing twice.
    repeat = service.scan(workspace_id)
    assert repeat["new_discoveries"] == 0
    with durable_database.session() as session:
        assert len(session.scalars(select(SourceScan)).all()) == 1
    assert len(session.scalars(select(PendingDiscovery)).all()) == 1


@pytest.mark.parametrize("platform", ["greenhouse", "workday"])
def test_domain_title_review_reads_the_body_without_a_configured_role_noun(
    durable_database: Database,
    platform: str,
) -> None:
    workspace_id = _workspace(durable_database)
    board = (
        "https://acme.wd1.myworkdayjobs.com/Careers"
        if platform == "workday"
        else "https://boards.greenhouse.io/acme"
    )
    _source(durable_database, workspace_id, "Acme", board)
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            seniority=Seniority(
                target_titles=titles("Service Manager"),
                strong_domain_tokens=["Clinical Content"],
            )
        ),
    )
    posting_url = board + "/job/Clinical-Content-Advisor_R-10"
    listing = SourceListing(
        employer="Acme",
        title="Clinical Content Advisor",
        url=posting_url,
        facts=published_listing_facts(platform, locations="Remote, Canada"),
    )
    reads = _RecordingReads()
    details: list[str] = []

    def detail(url: str) -> PostingCheck:
        details.append(url)
        return PostingCheck(
            status="readable",
            note="Synthetic detail",
            platform=platform,
            location="Remote, Canada",
            facts=published_listing_facts(platform, locations="Remote, Canada"),
            text="Own the data platform remotely in Canada.",
            source=PostingSource(
                publisher=platform, record="{}", description_paths=("description",)
            ),
        )

    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": [listing]}),
        posting_fetcher=UnexpectedPostingFetcher(),
        workday_detail_checker=detail,
        content_resolution=PostingContentResolutionService(
            durable_database, reads, storage=runtime_storage(durable_database)
        ),
    )
    result = service.scan(workspace_id)
    assert result["new_discoveries"] == 1
    assert reads.urls == ([posting_url] if platform != "workday" else [])
    assert details == ([posting_url] if platform == "workday" else [])
    pending = service.list_pending_discoveries(workspace_id)
    assert [row.title for row in pending] == [listing.title]
    assert any(
        finding.code == "title_domain_review" and finding.standing == "unknown"
        for finding in pending[0].screening_findings
    )


@pytest.mark.parametrize("platform", ["greenhouse", "workday"])
@pytest.mark.parametrize("context", ["publisher", "registered_source"])
def test_employer_domain_title_review_reads_the_body_and_retains_its_reason(
    durable_database: Database, platform: str, context: str
) -> None:
    workspace_id = _workspace(durable_database)
    board = (
        "https://acme.wd1.myworkdayjobs.com/Careers"
        if platform == "workday"
        else "https://boards.greenhouse.io/acme"
    )
    _source(durable_database, workspace_id, "Acme", board)
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            seniority=Seniority(
                target_titles=titles("Service Manager"), specialist_ic_role_nouns=["Data Trust"]
            ),
            employers=Employers(
                archetypes=[EmployerArchetype(name="Knowledge publisher", employer_names=["Acme"])]
            ),
        ),
    )
    posting_url = board + "/job/Data-Trust-Advisor_R-10"
    trust = (
        ListingIdentityTrust.PUBLISHER
        if context == "publisher"
        else ListingIdentityTrust.UNVERIFIED_HINT
    )
    listing = SourceListing(
        employer="Acme" if context == "publisher" else "Globex",
        title="Data Trust Advisor",
        url=posting_url,
        facts=published_listing_facts(platform, identity_trust=trust, locations="Remote, Canada"),
    )
    reads = _RecordingReads()
    details: list[str] = []

    def detail(url: str) -> PostingCheck:
        details.append(url)
        return PostingCheck(
            status="readable",
            note="Synthetic detail",
            platform=platform,
            facts=listing.facts,
            text="Own the data platform remotely in Canada.",
            source=PostingSource(
                publisher=platform, record="{}", description_paths=("description",)
            ),
        )

    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": [listing]}),
        posting_fetcher=UnexpectedPostingFetcher(),
        workday_detail_checker=detail,
        content_resolution=PostingContentResolutionService(
            durable_database, reads, storage=runtime_storage(durable_database)
        ),
    )
    result = service.scan(workspace_id)
    assert result["new_discoveries"] == 1
    assert reads.urls == ([posting_url] if platform != "workday" else [])
    assert details == ([posting_url] if platform == "workday" else [])
    pending = service.list_pending_discoveries(workspace_id)
    finding = next(item for item in pending[0].screening_findings if item.dimension == "title")
    assert finding.code == "title_domain_review"
    assert finding.standing == "unknown"
    assert "Knowledge publisher" in finding.subject
    assert "Acme" in finding.subject
    assert "data trust" in finding.subject
    assert pending[0].employer == listing.employer


@pytest.mark.parametrize(
    ("body", "category"),
    [
        (
            b"Requirements\n- Active RN license required\n- Five years of experience",
            "condition_excluded",
        ),
        (b"Applicants must be a resident of France.", "location_residence_required"),
    ],
)
def test_employer_domain_title_review_still_refuses_a_published_body_requirement(
    durable_database: Database, body: bytes, category: str
) -> None:
    workspace_id = _workspace(durable_database)
    board = "https://boards.greenhouse.io/acme"
    _source(durable_database, workspace_id, "Acme", board)
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            locations=[Location(key="home", country="Canada")],
            countries=[CountryNames(country="Canada")],
            seniority=Seniority(
                target_titles=titles("Service Manager"), specialist_ic_role_nouns=["Writer"]
            ),
            employers=Employers(
                archetypes=[EmployerArchetype(name="Knowledge publisher", employer_names=["Acme"])]
            ),
            excluded_conditions=[("RN license", "Not held")],
        ),
    )
    posting_url = board + "/jobs/requirements"
    listing = SourceListing(
        employer="Acme",
        title="Writer",
        url=posting_url,
        location="Remote, Canada",
        facts=published_listing_facts("greenhouse", locations="Remote, Canada"),
    )

    class BodyReads(_RecordingReads):
        def resolve(self, url: str, **kwargs: Any) -> ResolvedPostingContent:
            return replace(super().resolve(url, **kwargs), content=body)

    reads = BodyReads()
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": [listing]}),
        posting_fetcher=UnexpectedPostingFetcher(),
        content_resolution=PostingContentResolutionService(
            durable_database, reads, storage=runtime_storage(durable_database)
        ),
    )
    service.scan(workspace_id)

    assert reads.urls == [posting_url]
    assert service.list_pending_discoveries(workspace_id) == []
    with durable_database.session() as session:
        event = session.scalar(select(PendingDiscoveryDispositionEvent))
        assert event is not None
        assert event.category == category


def _board_outcomes(result: dict[str, Any]) -> list[str]:
    return [
        outcome["reason"]
        for outcome in result["funnel"]["outcomes"]
        if outcome["kind"] == "board_lead"
    ]


def test_an_empty_board_reports_no_open_positions(durable_database: Database) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    _state_titles(durable_database, workspace_id, "data")

    result = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": []}),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    assert _board_outcomes(result) == ["no_open_positions"]


@pytest.mark.parametrize(("complete", "raw_count"), [(True, 1), (False, 1), (False, 2)])
def test_a_board_whose_listings_all_miss_the_search_reports_no_matching_listings(
    durable_database: Database, complete: bool, raw_count: int
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    _state_titles(durable_database, workspace_id, "data")
    boards = ScriptedBoards(
        {
            "Acme": SourceRead(
                listings=[_listing("Sales Director", "https://boards.greenhouse.io/acme/jobs/1")],
                complete=complete,
                raw_count=raw_count,
                outcome=(
                    SourceReadOutcome.ATS_COMPLETE
                    if complete
                    else SourceReadOutcome.CRAWL_CONTINUATION
                ),
                skip_reasons={"js_shell": 1} if raw_count == 2 else {},
            )
        }
    )

    result = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=boards,
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    assert _board_outcomes(result) == ["no_matching_listings"]


def test_an_incomplete_raw_board_without_readable_listings_keeps_its_unread_reason(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    _state_titles(durable_database, workspace_id, "data")
    read = SourceRead(
        listings=[],
        complete=False,
        raw_count=2,
        outcome=SourceReadOutcome.CRAWL_CONTINUATION,
        skip_reasons={"posting_identity_not_established": 1, "js_shell": 1},
        note="The publisher feed stated no total for its empty list.",
    )
    handler = SourceScanHandler(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": read}),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={},
        idempotency_key="unread-board",
    )
    worker = Worker(
        durable_database,
        worker_id="unread-board-worker",
        handlers={"source.scan": handler},
    )

    assert worker.run_once()

    finished = queue.get(str(task.id))
    assert finished.status == "completed", (finished.error_category, finished.error_message)
    assert _board_outcomes(finished.progress) == ["no_readable_board"]
    [record] = handler.service.list_scan_history(workspace_id).items
    assert record.task_id == str(task.id)
    assert (record.status, record.read_outcome, record.complete) == (
        "incomplete",
        SourceReadOutcome.CRAWL_CONTINUATION,
        False,
    )
    assert (record.raw_count, record.read_count, record.result_count) == (2, 0, 0)
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert scan.id == record.id
        assert scan.source_id == source_id
        assert scan.skip_reasons == read.skip_reasons
        outcome = session.scalars(
            select(SourceScanOutcome).where(SourceScanOutcome.candidate_kind == "board_lead")
        ).one()
        assert outcome.reason == "no_readable_board"
        assert outcome.detail == read.note
        assert outcome.task_id == str(task.id)
        assert outcome.source_scan_id == record.id
        assert session.scalars(select(PendingDiscovery)).all() == []


def test_an_incomplete_zero_row_read_does_not_claim_the_board_is_empty(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    _state_titles(durable_database, workspace_id, "data")
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": []}, complete=False),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    result = service.scan(workspace_id)

    assert _board_outcomes(result) == ["no_readable_board"]
    with durable_database.session() as session:
        scan = session.scalar(select(SourceScan))
        assert scan is not None and scan.status == "incomplete"


def test_a_board_whose_matches_are_already_staged_reports_no_new_openings(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    _state_titles(durable_database, workspace_id, "data")
    boards = ScriptedBoards(
        {"Acme": [_listing("Data Platform Lead", "https://boards.greenhouse.io/acme/jobs/1")]}
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=boards,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.scan(workspace_id)

    result = service.scan(workspace_id)

    assert _board_outcomes(result) == ["no_new_openings"]


def _two_board_workspace(database: Database) -> tuple[str, str, str]:
    """Return a workspace stating one title and its two enabled employer boards."""
    workspace_id = _workspace(database)
    acme_id = _source(database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    globex_id = _source(database, workspace_id, "Globex", "https://boards.greenhouse.io/globex")
    _state_titles(database, workspace_id, "data")
    return workspace_id, acme_id, globex_id


def test_a_scan_of_one_named_source_reads_only_that_board(
    durable_database: Database,
) -> None:
    workspace_id, acme_id, _ = _two_board_workspace(durable_database)
    boards = ScriptedBoards(
        {
            "Acme": [_listing("Data Platform Lead", "https://boards.greenhouse.io/acme/jobs/1")],
            "Globex": [
                _listing("Data Platform Lead", "https://boards.greenhouse.io/globex/jobs/1")
            ],
        }
    )
    result = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=boards,
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id, source_ids=[acme_id])

    assert boards.requested == ["Acme"]
    assert result["sources_considered"] == 1
    with durable_database.session() as session:
        assert [item.source_id for item in session.scalars(select(SourceScan))] == [acme_id]


def test_an_overlapping_scan_reports_the_busy_source_and_reads_other_selected_sources(
    durable_database: Database,
) -> None:
    workspace_id, acme_id, globex_id = _two_board_workspace(durable_database)
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    requested: list[str] = []

    class HeldBoard:
        def listings(
            self,
            employer: str,
            url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            with lock:
                requested.append(employer)
                first = requested.count("Acme") == 1
            if employer == "Acme" and first:
                entered.set()
                release.wait()
            return SourceRead(
                listings=[], raw_count=0, complete=True, outcome=SourceReadOutcome.ATS_COMPLETE
            )

    def scan(selection: list[str]) -> dict[str, Any]:
        return SourceScanService(
            durable_database,
            storage=runtime_storage(durable_database),
            fetcher=HeldBoard(),
            posting_fetcher=UnexpectedPostingFetcher(),
        ).scan(workspace_id, source_ids=selection)

    with ThreadPoolExecutor(max_workers=1) as pool:
        first_scan = pool.submit(scan, [acme_id])
        entered.wait()
        try:
            second_scan = scan([acme_id, globex_id])
        finally:
            release.set()
        first_scan.result()

    assert requested.count("Acme") == 1
    assert requested.count("Globex") == 1
    assert second_scan["already_in_progress"] == [{"source_id": acme_id, "source": "Acme"}]
    assert [row["source"] for row in second_scan["scanned"]] == ["Globex"]


def test_a_cancelled_batch_keeps_its_source_reserved_until_the_reader_exits(
    durable_database: Database,
) -> None:
    workspace_id, acme_id, globex_id = _two_board_workspace(durable_database)
    entered = threading.Event()
    release = threading.Event()
    requested: list[str] = []
    lock = threading.Lock()

    class HeldBoard:
        def listings(
            self,
            employer: str,
            url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            with lock:
                requested.append(employer)
                first = requested.count("Acme") == 1
            if employer == "Acme" and first:
                entered.set()
                release.wait()
            return SourceRead(
                listings=[], raw_count=0, complete=True, outcome=SourceReadOutcome.ATS_COMPLETE
            )

    queue = TaskQueueService(durable_database)
    root = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [acme_id]},
        idempotency_key="held",
    )
    board = HeldBoard()
    worker = Worker(
        durable_database,
        worker_id="held-reader",
        handlers=default_handlers(durable_database, fetcher=board),
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(worker.run_once)
        entered.wait()
        try:
            queue.request_cancellation(str(root.id))
            with pytest.raises(ConflictError, match="still in progress|already in progress"):
                queue.commands.resume_source_batch(workspace_id, str(root.id), "too-early")
            result = SourceScanService(
                durable_database,
                storage=runtime_storage(durable_database),
                fetcher=board,
                posting_fetcher=UnexpectedPostingFetcher(),
            ).scan(workspace_id, source_ids=[acme_id, globex_id])
            assert result["already_in_progress"] == [{"source_id": acme_id, "source": "Acme"}]
            assert requested.count("Acme") == 1
            assert requested.count("Globex") == 1
        finally:
            release.set()
        assert running.result()
    assert queue.get(str(root.id)).status == "cancelled"
    resumed = queue.commands.resume_source_batch(workspace_id, str(root.id), "after-stop")
    assert resumed.status == "queued"


def test_cancellation_stops_a_queued_lane_before_its_initial_publisher_read(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id, acme_id, globex_id = _two_board_workspace(durable_database)
    entered = threading.Event()
    release = threading.Event()
    queued_entered = threading.Event()
    requested: list[str] = []
    original_lane = scan_sweep.BoardSweep._read_in_lane
    original_completed = as_completed

    def read_lane(
        sweep: scan_sweep.BoardSweep,
        board: scan_sweep.SweepBoard,
        inputs: ScanInputs,
        halted: Callable[[], bool],
    ) -> scan_sweep.BoardReadOutcome | None:
        if board.source.id == globex_id:
            queued_entered.set()
        return original_lane(sweep, board, inputs, halted)

    def completed(
        futures: set[Future[scan_sweep.BoardReadOutcome | None]],
    ) -> Iterator[Future[scan_sweep.BoardReadOutcome | None]]:
        for future in original_completed(futures):
            queued_entered.wait()
            yield future

    class HeldBoard:
        def listings(
            self,
            employer: str,
            url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            requested.append(employer)
            if employer == "Acme":
                entered.set()
                release.wait()
            return SourceRead(
                listings=[], raw_count=0, complete=True, outcome=SourceReadOutcome.ATS_COMPLETE
            )

    monkeypatch.setattr(sweep_module, "SCAN_LANES", 1)
    monkeypatch.setattr(scan_sweep.BoardSweep, "_read_in_lane", read_lane)
    monkeypatch.setattr(sweep_module, "as_completed", completed)
    queue = TaskQueueService(durable_database)
    root = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [acme_id, globex_id]},
        idempotency_key="held-before-queued-lane",
    )
    worker = Worker(
        durable_database,
        worker_id="queued-reader",
        handlers=default_handlers(durable_database, fetcher=HeldBoard()),
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(worker.run_once)
        entered.wait()
        queue.request_cancellation(str(root.id))
        release.set()
        assert running.result()
    assert requested == ["Acme"]
    assert queue.get(str(root.id)).status == "cancelled"


def test_a_scan_with_no_named_source_reads_every_enabled_board(
    durable_database: Database,
) -> None:
    workspace_id, _, _ = _two_board_workspace(durable_database)
    boards = ScriptedBoards({"Acme": [], "Globex": []})
    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=boards,
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id, source_ids=[])

    assert sorted(boards.requested) == ["Acme", "Globex"]


def test_an_unscanned_board_keeps_an_opening_its_publisher_dropped(
    durable_database: Database,
) -> None:
    """Absence means withdrawn only for a board this run actually read."""
    workspace_id, acme_id, _ = _two_board_workspace(durable_database)
    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards(
            {
                "Acme": [
                    _listing(
                        "Data Platform Lead", "https://boards.greenhouse.io/acme/jobs/1", "Acme"
                    )
                ],
                "Globex": [
                    _listing(
                        "Data Platform Lead", "https://boards.greenhouse.io/globex/jobs/1", "Globex"
                    )
                ],
            }
        ),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    pulled = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": [], "Globex": []}),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id, source_ids=[acme_id])

    assert [item["employer"] for item in pulled["withdrawn"]] == ["Acme"]
    queued = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).list_pending_discoveries(workspace_id)
    assert [(item.employer, item.withdrawn_at) for item in queued] == [("Globex", None)]


def test_a_scan_refuses_a_source_this_workspace_does_not_have(
    durable_database: Database,
) -> None:
    workspace_id, _, _ = _two_board_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({}),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    with pytest.raises(NotFoundError, match="Unknown source"):
        service.scan(workspace_id, source_ids=["missing-source"])


def test_a_scan_that_names_a_disabled_source_reads_it(durable_database: Database) -> None:
    """Naming a board is the decision to read it, whatever the recurring sweep covers."""
    workspace_id = _workspace(durable_database)
    _state_titles(durable_database, workspace_id, "data")
    disabled_id = _source(
        durable_database,
        workspace_id,
        "Globex",
        "https://boards.greenhouse.io/globex",
        enabled=False,
    )
    boards = ScriptedBoards(
        {"Globex": [_listing("Data Platform Lead", "https://boards.greenhouse.io/globex/jobs/1")]}
    )

    result = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=boards,
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id, source_ids=[disabled_id])

    assert boards.requested == ["Globex"]
    assert result["sources_considered"] == 1
    assert [item["title"] for item in result["discoveries"]] == ["Data Platform Lead"]


def test_a_scan_naming_nothing_leaves_out_a_disabled_source(durable_database: Database) -> None:
    """The recurring sweep still reads only the boards the workspace enabled."""
    workspace_id = _workspace(durable_database)
    _state_titles(durable_database, workspace_id, "data")
    _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    _source(
        durable_database,
        workspace_id,
        "Globex",
        "https://boards.greenhouse.io/globex",
        enabled=False,
    )
    boards = ScriptedBoards({"Acme": [], "Globex": []})

    result = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=boards,
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    assert boards.requested == ["Acme"]
    assert result["sources_considered"] == 1


def test_scan_uses_retained_publisher_bodies_for_recoverable_admission(
    durable_database: Database,
) -> None:
    """Only explicit body-backed conflicts leave the initial admission lane."""
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            locations=[
                Location(key="canada_remote", country="Canada"),
                Location(key="us_remote", country="United States"),
            ],
            countries=[CountryNames(country="Canada"), CountryNames(country="United States")],
            seniority=Seniority(
                target_titles=titles("Data Engineer"), excluded_titles=["Sales Executive"]
            ),
        ),
    )
    listings = [
        _listing("Data Engineer", "https://boards.greenhouse.io/acme/jobs/foreign"),
        _listing("Data Engineer", "https://boards.greenhouse.io/acme/jobs/us-residence"),
        _listing("Sales Executive", "https://boards.greenhouse.io/acme/jobs/excluded"),
        _listing("Data Engineer", "https://boards.greenhouse.io/acme/jobs/generic"),
        _listing("Data Engineer", "https://boards.greenhouse.io/acme/jobs/unknown", location=""),
        _listing(
            "Data Engineer", "https://boards.greenhouse.io/acme/jobs/viable", location="Canada"
        ),
        _listing("Data Engineer", "https://boards.greenhouse.io/acme/jobs/authorization"),
        _listing("Data Engineer", "https://boards.greenhouse.io/acme/jobs/us-remote"),
        _listing("Data Engineer", "https://boards.greenhouse.io/acme/jobs/us-salary"),
        _listing("Data Engineer", "https://boards.greenhouse.io/acme/jobs/no-residence-rule"),
        _listing("Data Engineer", "https://boards.greenhouse.io/acme/jobs/residence-alternative"),
    ]
    listings = [
        listings[0],
        replace(
            listings[1],
            location="Remote - United States",
            facts=published_listing_facts("greenhouse", locations="Remote - United States"),
            published_description=(
                "Location: Remote - United States\n"
                "Applicants must be a resident of the United States."
            ),
        ),
        replace(listings[2], published_description="Lead the commercial sales organization."),
        replace(listings[3], published_description="Build generic software systems."),
        replace(listings[4], published_description="Build clinical data tools."),
        replace(listings[5], published_description="Location: Toronto, Ontario, Canada"),
        replace(
            listings[6],
            location="Amsterdam, Netherlands",
            facts=published_listing_facts("greenhouse", locations="Amsterdam, Netherlands"),
            published_description="Applicants must be authorized to work in the United States.",
        ),
        replace(listings[7], published_description="Location: US Remote"),
        replace(listings[8], published_description="Pay range for US remote employees."),
        replace(
            listings[9],
            published_description="Residence: No requirement to live in the United States.",
        ),
        replace(
            listings[10],
            published_description="Residence: Must live in the United States or Canada.",
        ),
    ]

    class ForeignBodyResolver:
        calls = 0

        def resolve(self, url: str, **_kwargs: Any) -> ResolvedPostingContent:
            assert url.endswith("/foreign")
            self.calls += 1
            return ResolvedPostingContent(
                requested_url=url,
                final_url=url,
                content_status=PostingContentAccessStatus.READABLE,
                identity_status=PostingIdentityStatus.VERIFIED,
                posting_status=PostingStatus.ACTIVE,
                extraction_method=PostingContentExtractionMethod.STATIC_HTML,
                fetched_at=utc_now(),
                content=(
                    b"Location: Amsterdam, Netherlands"
                    if self.calls == 1
                    else b"Location: Toronto, Ontario, Canada"
                ),
                http_status=200,
                publisher_markup_present=True,
                identity_evidence={"publisher_declaration": True},
                note="Verified publisher body.",
            )

    content_resolution = PostingContentResolutionService(
        durable_database, ForeignBodyResolver(), storage=runtime_storage(durable_database)
    )
    handler = SourceScanHandler(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": listings}),
        posting_fetcher=UnexpectedPostingFetcher(),
        content_resolution=content_resolution,
    )
    service = handler.service
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={},
        idempotency_key="body-backed-initial-admission",
    )
    handlers = default_handlers(durable_database, fetcher=ScriptedBoards({"Acme": listings}))
    handlers["source.scan"] = handler
    worker = Worker(durable_database, worker_id="body-backed-initial-admission", handlers=handlers)

    assert worker.run_once()
    finished = queue.get(str(task.id))
    assert finished.status == "completed", (finished.error_category, finished.error_message)

    pending = service.list_pending_discoveries(workspace_id)
    visible_urls = {item.canonical_url for item in pending}
    # us-residence names the United States, which this person's own saved,
    # actionable "us_remote" location already authorizes.
    assert visible_urls == {
        "https://boards.greenhouse.io/acme/jobs/generic",
        "https://boards.greenhouse.io/acme/jobs/unknown",
        "https://boards.greenhouse.io/acme/jobs/viable",
        "https://boards.greenhouse.io/acme/jobs/us-remote",
        "https://boards.greenhouse.io/acme/jobs/us-salary",
        "https://boards.greenhouse.io/acme/jobs/no-residence-rule",
        "https://boards.greenhouse.io/acme/jobs/residence-alternative",
        "https://boards.greenhouse.io/acme/jobs/us-residence",
    }
    with durable_database.session() as session:
        dismissals = list(session.scalars(select(PendingDiscoveryDispositionEvent)))
        outcomes = list(
            session.scalars(
                select(SourceScanOutcome).where(
                    SourceScanOutcome.task_id == str(task.id),
                    SourceScanOutcome.candidate_kind == "board_listing",
                )
            )
        )
        scan = session.scalars(select(SourceScan)).one()
    assert {item.category for item in dismissals} == {"location_outside"}
    assert all("restorable" in item.reason for item in dismissals)
    assert {finding["code"] for item in dismissals for finding in item.screening["findings"]} == {
        "location_outside",
    }
    excluded = {
        outcome.url: outcome for outcome in outcomes if outcome.reason == "hard_constraint_excluded"
    }
    assert set(excluded) == {
        "https://boards.greenhouse.io/acme/jobs/foreign",
        "https://boards.greenhouse.io/acme/jobs/authorization",
    }
    assert {outcome.terminal_stage for outcome in excluded.values()} == {"screened"}
    assert {
        finding["code"] for outcome in excluded.values() for finding in outcome.screening_findings
    } == {
        "location_outside",
    }
    assert all(outcome.origin == "source.scan" for outcome in excluded.values())
    assert all(outcome.correlation_id == str(task.id) for outcome in excluded.values())
    assert all(outcome.source_scan_id == scan.id for outcome in excluded.values())
    assert all(
        outcome.normalized_url == normalize_url(outcome.url) for outcome in excluded.values()
    )
    assert all(
        sum(
            outcome.task_id == str(task.id)
            and outcome.source_name == candidate.source_name
            and outcome.candidate_kind == candidate.candidate_kind
            and outcome.normalized_url == candidate.normalized_url
            for outcome in outcomes
        )
        == 1
        for candidate in outcomes
    )
    with durable_database.session() as session:
        foreign_pending_id = session.scalar(
            select(PendingDiscovery.id).where(
                PendingDiscovery.canonical_url == "https://boards.greenhouse.io/acme/jobs/foreign"
            )
        )
    assert foreign_pending_id is not None
    refreshed = content_resolution.resolve_automatic(
        workspace_id,
        [foreign_pending_id],
        origin="source.continue",
    )
    assert refreshed["attempted"] == 1
    assert "https://boards.greenhouse.io/acme/jobs/foreign" in {
        item.canonical_url for item in service.list_pending_discoveries(workspace_id)
    }
    with durable_database.session() as session:
        foreign_history = list(
            session.scalars(
                select(PendingDiscoveryDispositionEvent)
                .where(PendingDiscoveryDispositionEvent.pending_discovery_id == foreign_pending_id)
                .order_by(PendingDiscoveryDispositionEvent.created_at)
            )
        )
    assert [event.action for event in foreign_history] == ["dismiss", "restore"]
    assert foreign_history[1].supersedes_event_id == foreign_history[0].id


def test_scan_dismisses_structured_hybrid_and_same_country_residence_bodies(
    durable_database: Database,
) -> None:
    """A hybrid attendance body is dismissed, and a same-country rule leaves its rows visible."""
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            locations=[
                Location(
                    key="us_remote",
                    country="United States",
                    arrangement=Arrangement(
                        remote=Acceptance.PREFERRED,
                        hybrid=Acceptance.REFUSED,
                        on_site=Acceptance.REFUSED,
                        maximum_office_days=0,
                    ),
                ),
            ],
            countries=[CountryNames(country="United States")],
            seniority=Seniority(target_titles=titles("Data Engineer")),
        ),
    )
    hybrid_body = (
        b"Location: Indianapolis, IN, United States\n"
        b"Hybrid: Eight days a month, commuting to our closest office."
    )
    residence_body = (
        b"This is a remote role. "
        b"To be eligible for this position, you must reside in the same country."
    )
    listings = [
        replace(
            _listing("Data Engineer", "https://boards.greenhouse.io/acme/jobs/indianapolis"),
            facts=published_listing_facts(
                "greenhouse", locations="Indianapolis, IN, United States"
            ),
        ),
        replace(
            _listing("Data Engineer", "https://boards.greenhouse.io/acme/jobs/houston"),
            location="Houston, TX, United States of America",
            facts=published_listing_facts(
                "greenhouse", locations="Houston, TX, United States of America"
            ),
        ),
        replace(
            _listing("Data Engineer", "https://boards.greenhouse.io/acme/jobs/unresolved"),
            location="Multiple Locations",
            facts=published_listing_facts("greenhouse", locations="Multiple Locations"),
        ),
    ]

    class BodyResolver:
        def resolve(self, url: str, **_kwargs: Any) -> ResolvedPostingContent:
            body = {
                "https://boards.greenhouse.io/acme/jobs/indianapolis": hybrid_body,
                "https://boards.greenhouse.io/acme/jobs/houston": residence_body,
                "https://boards.greenhouse.io/acme/jobs/unresolved": residence_body,
            }[url]
            return ResolvedPostingContent(
                requested_url=url,
                final_url=url,
                content_status=PostingContentAccessStatus.READABLE,
                identity_status=PostingIdentityStatus.VERIFIED,
                posting_status=PostingStatus.ACTIVE,
                extraction_method=PostingContentExtractionMethod.STATIC_HTML,
                fetched_at=utc_now(),
                content=body,
                http_status=200,
                publisher_markup_present=True,
                identity_evidence={"publisher_declaration": True},
                note="Verified publisher body.",
            )

    content_resolution = PostingContentResolutionService(
        durable_database, BodyResolver(), storage=runtime_storage(durable_database)
    )
    handler = SourceScanHandler(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": listings}),
        posting_fetcher=UnexpectedPostingFetcher(),
        content_resolution=content_resolution,
    )
    service = handler.service
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={},
        idempotency_key="hybrid-and-residence-admission",
    )
    handlers = default_handlers(durable_database, fetcher=ScriptedBoards({"Acme": listings}))
    handlers["source.scan"] = handler
    worker = Worker(durable_database, worker_id="hybrid-and-residence-admission", handlers=handlers)

    assert worker.run_once()
    finished = queue.get(str(task.id))
    assert finished.status == "completed", (finished.error_category, finished.error_message)

    visible_urls = {item.canonical_url for item in service.list_pending_discoveries(workspace_id)}
    assert "https://boards.greenhouse.io/acme/jobs/indianapolis" not in visible_urls
    # A same-country rule names no country, so it states no residence requirement.
    assert "https://boards.greenhouse.io/acme/jobs/houston" in visible_urls
    assert "https://boards.greenhouse.io/acme/jobs/unresolved" in visible_urls

    with durable_database.session() as session:
        dismissals: dict[str, str] = dict(
            session.execute(
                select(PendingDiscovery.canonical_url, PendingDiscoveryDispositionEvent.category)
                .join(
                    PendingDiscoveryDispositionEvent,
                    PendingDiscoveryDispositionEvent.pending_discovery_id == PendingDiscovery.id,
                )
                .where(PendingDiscovery.workspace_id == workspace_id)
            )
            .tuples()
            .all()
        )
    assert dismissals == {
        "https://boards.greenhouse.io/acme/jobs/indianapolis": "arrangement_refused"
    }


def test_scan_dismisses_a_bare_in_office_cadence_body(durable_database: Database) -> None:
    """A body's bare "in office" cadence, not only "in the office", still reaches the hold-out."""
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            locations=[
                Location(
                    key="us_remote",
                    country="United States",
                    arrangement=Arrangement(
                        remote=Acceptance.PREFERRED,
                        hybrid=Acceptance.REFUSED,
                        on_site=Acceptance.REFUSED,
                        maximum_office_days=0,
                    ),
                ),
            ],
            countries=[CountryNames(country="United States")],
            seniority=Seniority(target_titles=titles("Data Engineer")),
        ),
    )
    url = "https://boards.greenhouse.io/acme/jobs/indianapolis"
    body = (
        b"Location: Indianapolis, IN, United States\n"
        b"This team follows a hybrid model requiring 3 days per week in office."
    )
    listings = [
        replace(
            _listing("Data Engineer", url),
            facts=published_listing_facts(
                "greenhouse", locations="Indianapolis, IN, United States"
            ),
        ),
    ]

    class BodyResolver:
        def resolve(self, url: str, **_kwargs: Any) -> ResolvedPostingContent:
            return ResolvedPostingContent(
                requested_url=url,
                final_url=url,
                content_status=PostingContentAccessStatus.READABLE,
                identity_status=PostingIdentityStatus.VERIFIED,
                posting_status=PostingStatus.ACTIVE,
                extraction_method=PostingContentExtractionMethod.STATIC_HTML,
                fetched_at=utc_now(),
                content=body,
                http_status=200,
                publisher_markup_present=True,
                identity_evidence={"publisher_declaration": True},
                note="Verified publisher body.",
            )

    content_resolution = PostingContentResolutionService(
        durable_database, BodyResolver(), storage=runtime_storage(durable_database)
    )
    handler = SourceScanHandler(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": listings}),
        posting_fetcher=UnexpectedPostingFetcher(),
        content_resolution=content_resolution,
    )
    service = handler.service
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={},
        idempotency_key="bare-in-office-admission",
    )
    handlers = default_handlers(durable_database, fetcher=ScriptedBoards({"Acme": listings}))
    handlers["source.scan"] = handler
    worker = Worker(durable_database, worker_id="bare-in-office-admission", handlers=handlers)

    assert worker.run_once()
    finished = queue.get(str(task.id))
    assert finished.status == "completed", (finished.error_category, finished.error_message)

    visible_urls = {item.canonical_url for item in service.list_pending_discoveries(workspace_id)}
    assert url not in visible_urls

    with durable_database.session() as session:
        events = list(
            session.scalars(
                select(PendingDiscoveryDispositionEvent)
                .join(
                    PendingDiscovery,
                    PendingDiscoveryDispositionEvent.pending_discovery_id == PendingDiscovery.id,
                )
                .where(PendingDiscovery.canonical_url == url)
            )
        )
    assert [event.category for event in events] == ["arrangement_refused"]
    assert "restorable" in events[0].reason
    assert "arrangement_refused" in {finding["code"] for finding in events[0].screening["findings"]}


def _single_body_scan(durable_database: Database, body: bytes, idempotency_key: str) -> set[str]:
    """Stage one listing, resolve its body, run the scan, and return visible urls."""
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    state_single_market(
        durable_database, workspace_id, "Data Engineer", key="home", country="Canada"
    )
    url = "https://boards.greenhouse.io/acme/jobs/residence"
    listings = [
        replace(
            _listing("Data Engineer", url),
            location="Toronto, ON, Canada",
            facts=published_listing_facts("greenhouse", locations="Toronto, ON, Canada"),
        )
    ]

    class BodyResolver:
        def resolve(self, url: str, **_kwargs: Any) -> ResolvedPostingContent:
            return ResolvedPostingContent(
                requested_url=url,
                final_url=url,
                content_status=PostingContentAccessStatus.READABLE,
                identity_status=PostingIdentityStatus.VERIFIED,
                posting_status=PostingStatus.ACTIVE,
                extraction_method=PostingContentExtractionMethod.STATIC_HTML,
                fetched_at=utc_now(),
                content=body,
                http_status=200,
                publisher_markup_present=True,
                identity_evidence={"publisher_declaration": True},
                note="Verified publisher body.",
            )

    content_resolution = PostingContentResolutionService(
        durable_database, BodyResolver(), storage=runtime_storage(durable_database)
    )
    handler = SourceScanHandler(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": listings}),
        posting_fetcher=UnexpectedPostingFetcher(),
        content_resolution=content_resolution,
    )
    service = handler.service
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={},
        idempotency_key=idempotency_key,
    )
    handlers = default_handlers(durable_database, fetcher=ScriptedBoards({"Acme": listings}))
    handlers["source.scan"] = handler
    worker = Worker(durable_database, worker_id=idempotency_key, handlers=handlers)
    assert worker.run_once()
    finished = queue.get(str(task.id))
    assert finished.status == "completed", (finished.error_category, finished.error_message)
    return {item.canonical_url for item in service.list_pending_discoveries(workspace_id)}


def test_a_body_requiring_residence_where_the_person_has_no_place_leaves_the_queue(
    durable_database: Database,
) -> None:
    """A body requiring residence where the person declared no place leaves the queue."""
    body = b"Applicants must be a resident of France. Employees must reside in Germany."
    visible = _single_body_scan(durable_database, body, "residence-refuse-and-refuse")
    assert "https://boards.greenhouse.io/acme/jobs/residence" not in visible


def test_a_body_location_line_never_resolves_a_staged_multi_location_summary(
    durable_database: Database,
) -> None:
    """A board's "3 Locations" summary stays incomplete after one body Location line.

    A single body-read location is not proof every one of the staged, still
    unresolved locations agrees, so it must never settle a precise arrangement
    refusal on its own.
    """
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            locations=[
                Location(
                    key="us_remote",
                    country="United States",
                    arrangement=Arrangement(
                        remote=Acceptance.PREFERRED,
                        hybrid=Acceptance.REFUSED,
                        on_site=Acceptance.REFUSED,
                        maximum_office_days=0,
                    ),
                ),
            ],
            countries=[CountryNames(country="United States")],
            seniority=Seniority(target_titles=titles("Data Engineer")),
        ),
    )
    url = "https://boards.greenhouse.io/acme/jobs/multi-location"
    listings = [
        replace(
            _listing("Data Engineer", url),
            location="3 Locations",
            facts=published_listing_facts("greenhouse", locations="3 Locations"),
        )
    ]
    body = (
        b"Location: New York, NY, United States\n"
        b"Hybrid: Five days a month, commuting to our closest office."
    )

    class BodyResolver:
        def resolve(self, url: str, **_kwargs: Any) -> ResolvedPostingContent:
            return ResolvedPostingContent(
                requested_url=url,
                final_url=url,
                content_status=PostingContentAccessStatus.READABLE,
                identity_status=PostingIdentityStatus.VERIFIED,
                posting_status=PostingStatus.ACTIVE,
                extraction_method=PostingContentExtractionMethod.STATIC_HTML,
                fetched_at=utc_now(),
                content=body,
                http_status=200,
                publisher_markup_present=True,
                identity_evidence={"publisher_declaration": True},
                note="Verified publisher body.",
            )

    content_resolution = PostingContentResolutionService(
        durable_database, BodyResolver(), storage=runtime_storage(durable_database)
    )
    handler = SourceScanHandler(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": listings}),
        posting_fetcher=UnexpectedPostingFetcher(),
        content_resolution=content_resolution,
    )
    service = handler.service
    queue = TaskQueueService(durable_database)
    idempotency_key = "multi-location-body-line"
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={},
        idempotency_key=idempotency_key,
    )
    handlers = default_handlers(durable_database, fetcher=ScriptedBoards({"Acme": listings}))
    handlers["source.scan"] = handler
    worker = Worker(durable_database, worker_id=idempotency_key, handlers=handlers)
    assert worker.run_once()
    finished = queue.get(str(task.id))
    assert finished.status == "completed", (finished.error_category, finished.error_message)

    visible_urls = {item.canonical_url for item in service.list_pending_discoveries(workspace_id)}
    assert url in visible_urls
    with durable_database.session() as session:
        dismissals = dict(
            session.execute(
                select(PendingDiscovery.canonical_url, PendingDiscoveryDispositionEvent.category)
                .join(
                    PendingDiscoveryDispositionEvent,
                    PendingDiscoveryDispositionEvent.pending_discovery_id == PendingDiscovery.id,
                )
                .where(PendingDiscovery.workspace_id == workspace_id)
            )
            .tuples()
            .all()
        )
    assert dismissals == {}


def test_scan_never_dismisses_a_body_with_a_remote_offer_anywhere(
    durable_database: Database,
) -> None:
    """A remote offer anywhere in the body blocks the attendance dismissal."""
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            locations=[
                Location(
                    key="us_remote",
                    country="United States",
                    arrangement=Arrangement(
                        remote=Acceptance.PREFERRED,
                        hybrid=Acceptance.REFUSED,
                        on_site=Acceptance.REFUSED,
                        maximum_office_days=0,
                    ),
                ),
            ],
            countries=[CountryNames(country="United States")],
            seniority=Seniority(target_titles=titles("Data Engineer")),
        ),
    )
    body = (
        b"Location: New York, NY, United States\n"
        b"This role is fully remote for most candidates. "
        b"Candidates near New York may choose a hybrid role."
    )
    listings = [
        replace(
            _listing("Data Engineer", "https://boards.greenhouse.io/acme/jobs/nyc"),
            facts=published_listing_facts("greenhouse", locations="New York, NY, United States"),
        ),
    ]

    class BodyResolver:
        def resolve(self, url: str, **_kwargs: Any) -> ResolvedPostingContent:
            return ResolvedPostingContent(
                requested_url=url,
                final_url=url,
                content_status=PostingContentAccessStatus.READABLE,
                identity_status=PostingIdentityStatus.VERIFIED,
                posting_status=PostingStatus.ACTIVE,
                extraction_method=PostingContentExtractionMethod.STATIC_HTML,
                fetched_at=utc_now(),
                content=body,
                http_status=200,
                publisher_markup_present=True,
                identity_evidence={"publisher_declaration": True},
                note="Verified publisher body.",
            )

    content_resolution = PostingContentResolutionService(
        durable_database, BodyResolver(), storage=runtime_storage(durable_database)
    )
    handler = SourceScanHandler(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": listings}),
        posting_fetcher=UnexpectedPostingFetcher(),
        content_resolution=content_resolution,
    )
    service = handler.service
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={},
        idempotency_key="remote-offer-blocks-dismissal",
    )
    handlers = default_handlers(durable_database, fetcher=ScriptedBoards({"Acme": listings}))
    handlers["source.scan"] = handler
    worker = Worker(durable_database, worker_id="remote-offer-blocks-dismissal", handlers=handlers)

    assert worker.run_once()
    finished = queue.get(str(task.id))
    assert finished.status == "completed", (finished.error_category, finished.error_message)

    visible_urls = {item.canonical_url for item in service.list_pending_discoveries(workspace_id)}
    assert "https://boards.greenhouse.io/acme/jobs/nyc" in visible_urls


def test_scan_reads_a_resolved_board_instead_of_the_marketing_careers_page(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    careers_url = "https://example.test/careers"
    resolved_url = "https://jobs.example.test/example"
    _source(
        durable_database,
        workspace_id,
        "Acme",
        careers_url,
        resolved_platform="dayforce",
        resolved_url=resolved_url,
    )
    boards = ScriptedBoards({"Acme": []})

    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=boards,
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    assert boards.requested_urls == [resolved_url]


def test_scan_reads_the_board_the_source_names_when_its_resolution_names_another(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    board_url = "https://job-boards.greenhouse.io/acmehealthcare"
    _source(
        durable_database,
        workspace_id,
        "Acme",
        board_url,
        resolved_platform="greenhouse",
        resolved_url="https://job-boards.greenhouse.io/acme",
    )
    boards = ScriptedBoards({"Acme": []})

    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=boards,
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    assert boards.requested_urls == [board_url]
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert "https://job-boards.greenhouse.io/acme " in scan.notes


def test_board_date_metadata_reaches_current_content_even_when_the_body_is_unchanged(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(seniority=Seniority(target_titles=titles("data"))),
    )
    url = "https://boards.greenhouse.io/acme/jobs/1"
    description = "Data Platform Lead\nOwn the data platform."
    base = _listing("Data Platform Lead", url)
    boards = ScriptedBoards({"Acme": []})

    class NeverResolve:
        def resolve(self, url: str, **kwargs: Any) -> ResolvedPostingContent:
            raise AssertionError("The board already supplied its full body.")

    storage = runtime_storage(durable_database)
    content_resolution = PostingContentResolutionService(
        durable_database, NeverResolve(), storage=storage
    )
    service = SourceScanService(
        durable_database,
        storage=storage,
        fetcher=boards,
        posting_fetcher=UnexpectedPostingFetcher(),
        content_resolution=content_resolution,
    )
    body_artifacts = []
    for day, value in ((8, "2026-10-01"), (9, "2026-10-21"), (10, "")):
        at = datetime(2026, 10, day, 12, tzinfo=UTC)
        date_fact = publisher_closing_date(
            description, valid_through=value, source_url=url, observed_at=at
        )
        boards.boards["Acme"] = [
            replace(
                base,
                published_description=description,
                facts=replace(base.facts, closing_date=date_fact),
                source=PostingSource(
                    publisher="page",
                    record=json.dumps({"validThrough": value, "description": description}),
                ),
            )
        ]
        service.scan(workspace_id)
        with durable_database.session() as session:
            pending = session.scalars(select(PendingDiscovery)).one()
            pending_id = pending.id
        current = CurrentPostingContentReader(durable_database, storage=storage).read(
            workspace_id, pending_id
        )
        assert current is not None and current.publisher_closing_date is not None
        assert current.publisher_closing_date.normalized_value == (value or None)
        assert current.publisher_closing_date.observed_at == at
        with durable_database.session() as session:
            content = session.get(PendingPostingContent, current.evidence_id)
            assert content is not None
            assert content.posting_status == PostingStatus.ACTIVE
            row = session.get(PendingDiscovery, pending_id)
            assert row is not None
            assert ("Closing date passed; verify availability." in row.content_note) == (day == 8)
            body_artifacts.append(content.artifact_id)
            if value:
                source_artifact = session.get(Artifact, current.publisher_closing_date.artifact_id)
                assert source_artifact is not None
                assert source_artifact.related_entity_id == content.id
    assert len(set(body_artifacts)) == 1


def test_scan_captures_the_full_board_description_for_an_unread_row(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(seniority=Seniority(target_titles=titles("data"))),
    )
    # Longer than the 4000-char screening summary, to prove the full text is kept.
    description = ("Own the data platform. " * 400).strip()
    assert len(description) > 4000
    listing = replace(
        _listing("Data Platform Lead", "https://boards.greenhouse.io/acme/jobs/1"),
        published_description=description,
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": [listing]}),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    assert service.scan(workspace_id)["new_discoveries"] == 1

    with durable_database.session() as session:
        pending = session.scalars(select(PendingDiscovery)).one()
        assert pending.content_status == "not_yet_read"
        assert pending.listing_artifact_id is not None
        artifact = session.get(Artifact, pending.listing_artifact_id)
        assert artifact is not None
        assert artifact.logical_type == "posting.listing.text"
        assert artifact.byte_size == len(description.encode("utf-8"))
        assert artifact.sha256 == hashlib.sha256(description.encode("utf-8")).hexdigest()
        first_artifact_id = pending.listing_artifact_id

    # A second scan the same day re-stages the row but never rewrites the capture.
    service.scan(workspace_id)
    with durable_database.session() as session:
        assert session.scalars(select(PendingDiscovery)).one().listing_artifact_id == (
            first_artifact_id
        )
        assert len(session.scalars(select(Artifact)).all()) == 1


@pytest.mark.parametrize("orphan_name", ["listing.txt", "raw.json", "description.html"])
def test_a_scan_captures_an_unlinked_listing_without_overwriting_orphaned_bytes(
    durable_database: Database,
    orphan_name: str,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    _state_titles(durable_database, workspace_id, "data")
    storage = runtime_storage(durable_database)
    record = {
        "id": 1,
        "title": "Data Platform Lead",
        "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
        "content": "<p>Own the data platform and its governance.</p>",
    }
    listing = source_listings(GREENHOUSE_MAPPING, [record], employer="Acme")[0]
    boards = ScriptedBoards({"Acme": [replace(listing, published_description="", source=None)]})
    service = SourceScanService(
        durable_database,
        storage=storage,
        fetcher=boards,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    assert service.scan(workspace_id)["new_discoveries"] == 1
    with durable_database.session() as session:
        pending = session.scalars(select(PendingDiscovery)).one()
        pending_id = pending.id
        assert pending.listing_artifact_id is None
        assert session.scalars(select(Artifact)).all() == []

    directory = storage.artifact_root / workspace_id / "pending-listing" / pending_id
    directory.mkdir(parents=True)
    assert listing.source is not None
    expected = {
        "listing.txt": listing.published_description.strip(),
        "raw.json": listing.source.record,
        "description.html": listing.source.description_markup,
    }
    orphaned = {**expected, orphan_name: "Earlier capture with different bytes."}
    for name, content in orphaned.items():
        (directory / name).write_text(content, encoding="utf-8")
    boards.boards["Acme"] = [listing]

    assert service.scan(workspace_id)["new_discoveries"] == 0
    service.scan(workspace_id)

    with durable_database.session() as session:
        pending = session.scalars(select(PendingDiscovery)).one()
        assert pending.id == pending_id
        assert pending.listing_artifact_id is not None
        captured = session.get(Artifact, pending.listing_artifact_id)
        assert captured is not None
        assert captured.logical_type == "posting.listing.text"
        artifacts = session.scalars(select(Artifact)).all()
        assert len(artifacts) == 3
        paths = [file_uri_path(artifact.storage_uri) for artifact in artifacts]
        assert all(artifact.integrity_status == "verified" for artifact in artifacts)
    assert all(path is not None for path in paths)
    captured_paths = [path for path in paths if path is not None]
    assert len({path.parent for path in captured_paths}) == 1
    assert {path.name: path.read_text(encoding="utf-8") for path in captured_paths} == expected
    assert {name: (directory / name).read_text(encoding="utf-8") for name in orphaned} == orphaned


def test_scan_stages_a_paylocity_page_only_opening_with_no_published_body(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    tenant = "0b7f2c1e-5a4d-4e8b-9c3f-7d6e5a4b3c2d"
    board = f"https://recruiting.paylocity.com/Recruiting/Jobs/All/{tenant}"
    _source(durable_database, workspace_id, "Examplehealth", board)
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(seniority=Seniority(target_titles=titles("data"))),
    )
    answers = {
        board: paylocity_board_page([paylocity_page_job(7001234, "Clinical Data Analyst")]),
        f"https://recruiting.paylocity.com/recruiting/v2/api/feed/jobs/{tenant}": json.dumps(
            {"displayName": tenant, "jobs": []}
        ),
    }
    fetcher = BoardApiFetcher(
        client=policy_client(
            lambda url: httpx.Response(200, request=httpx.Request("GET", url), text=answers[url])
        )
    )

    scanned = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=fetcher,
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    assert scanned["new_discoveries"] == 1
    with durable_database.session() as session:
        pending = session.scalars(select(PendingDiscovery)).one()
        assert pending.discovery_url == (
            "https://recruiting.paylocity.com/Recruiting/Jobs/Details/7001234"
        )
        assert pending.content_status == "not_yet_read"
        assert pending.listing_artifact_id is None
        assert session.scalars(select(Artifact)).all() == []


def test_scan_keeps_the_board_record_and_its_markup_beside_the_listing_capture(
    durable_database: Database,
) -> None:
    """A converter fix re-derives a captured listing offline only from what the sweep converted."""
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(seniority=Seniority(target_titles=titles("data"))),
    )
    content = html.escape("<p>" + "Own the data platform and its governance. " * 12 + "</p>")
    record = {
        "id": 1,
        "title": "Data Platform Lead",
        "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
        "content": content,
    }
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards(
            {"Acme": source_listings(GREENHOUSE_MAPPING, [record], employer="Acme")}
        ),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    service.scan(workspace_id)
    service.scan(workspace_id)

    with durable_database.session() as session:
        rows = session.scalars(select(Artifact)).all()
        files = {row.logical_type: file_uri_path(row.storage_uri) for row in rows}
    assert {kind: path.name for kind, path in files.items() if path is not None} == {
        "posting.listing.text": "listing.txt",
        "posting.source.json": "raw.json",
        "posting.body.html": "description.html",
    }
    assert len({path.parent for path in files.values() if path is not None}) == 1
    raw, markup = files["posting.source.json"], files["posting.body.html"]
    assert raw is not None and markup is not None
    assert json.loads(raw.read_bytes()) == record
    assert markup.read_text(encoding="utf-8") == content


def test_scan_reports_the_rows_its_own_content_reads_withdrew(
    durable_database: Database,
) -> None:
    """A staged row the scan's content read found closed is counted in the sweep result."""
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(seniority=Seniority(target_titles=titles("data"))),
    )
    listings = [
        _listing("Data Platform Lead", "https://boards.greenhouse.io/acme/jobs/closed"),
        _listing("Data Engineer", "https://boards.greenhouse.io/acme/jobs/open"),
    ]

    class ClosedForOneUrl:
        def resolve(self, url: str, **_kwargs: Any) -> ResolvedPostingContent:
            closed = url.endswith("/closed")
            return ResolvedPostingContent(
                requested_url=url,
                final_url=url,
                content_status=(
                    PostingContentAccessStatus.NOT_YET_READ
                    if closed
                    else PostingContentAccessStatus.READABLE
                ),
                identity_status=(
                    PostingIdentityStatus.NOT_YET_VERIFIED
                    if closed
                    else PostingIdentityStatus.VERIFIED
                ),
                posting_status=PostingStatus.CLOSED if closed else PostingStatus.ACTIVE,
                extraction_method=(None if closed else PostingContentExtractionMethod.STATIC_HTML),
                fetched_at=utc_now(),
                content=None if closed else b"Own the data platform. Remote in the United States.",
                http_status=404 if closed else 200,
                publisher_markup_present=not closed,
                identity_evidence={"closure": True} if closed else {"publisher_declaration": True},
                note=(
                    "The publisher reports that this posting is closed."
                    if closed
                    else "Verified publisher body."
                ),
            )

    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": listings}),
        posting_fetcher=UnexpectedPostingFetcher(),
        content_resolution=PostingContentResolutionService(
            durable_database, ClosedForOneUrl(), storage=runtime_storage(durable_database)
        ),
    )

    result = service.scan(workspace_id)

    assert result["withdrawn_on_read"] == 1
    [scan_row] = result["scanned"]
    assert scan_row["content_resolution"]["withdrawn_ids"] == [
        _pending_id_for(durable_database, "https://boards.greenhouse.io/acme/jobs/closed")
    ]
    # The board still lists both openings, so the absent-listing count stays zero.
    assert scan_row["withdrawn"] == 0


def test_an_unreadable_stored_body_does_not_end_the_scan_of_other_sources(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    _source(durable_database, workspace_id, "Globex", "https://boards.greenhouse.io/globex")
    _state_titles(durable_database, workspace_id, "data")
    acme_url = "https://boards.greenhouse.io/acme/jobs/1"
    globex_url = "https://boards.greenhouse.io/globex/jobs/2"
    boards = ScriptedBoards(
        {
            "Acme": [
                replace(
                    _listing("Data Platform Lead", acme_url),
                    published_description="Own the data platform.",
                )
            ],
            "Globex": [],
        }
    )
    storage = runtime_storage(durable_database)
    service = SourceScanService(
        durable_database,
        storage=storage,
        fetcher=boards,
        posting_fetcher=UnexpectedPostingFetcher(),
        content_resolution=PostingContentResolutionService(
            durable_database, UnexpectedContentResolver(), storage=storage
        ),
    )
    service.scan(workspace_id)
    pending_id = _pending_id_for(durable_database, acme_url)
    with durable_database.session() as session:
        row = session.get(PendingDiscovery, pending_id)
        assert row is not None
        evidence = session.get(PendingPostingContent, row.current_content_id)
        assert evidence is not None and evidence.artifact_id is not None
        artifact = session.get(Artifact, evidence.artifact_id)
        assert artifact is not None
        stored = file_uri_path(artifact.storage_uri)
    assert stored is not None
    stored.unlink()
    boards.boards["Acme"] = [
        replace(
            _listing("Data Platform Lead", acme_url),
            published_description="Own the data platform and its governance.",
        )
    ]
    boards.boards["Globex"] = [
        replace(_listing("Data Engineer", globex_url), published_description="Build pipelines.")
    ]

    result = service.scan(workspace_id)

    assert result["new_discoveries"] == 1
    assert _pending_id_for(durable_database, globex_url)
    with durable_database.session() as session:
        row = session.get(PendingDiscovery, pending_id)
        assert row is not None
        evidence = session.get(PendingPostingContent, row.current_content_id)
        assert evidence is not None and evidence.artifact_id is not None
        artifact = session.get(Artifact, evidence.artifact_id)
        assert artifact is not None
        stored = file_uri_path(artifact.storage_uri)
    assert stored is not None
    assert stored.read_text(encoding="utf-8") == "Own the data platform and its governance."


_WORKDAY_STAGED_URL = "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/Data-Engineer_R30001"
_GREENHOUSE_STAGED_URL = "https://boards.greenhouse.io/acme/jobs/30001"
_READ_ADDRESS = "https://careers.acme.example/jobs/data-engineer"


@pytest.mark.parametrize("repair_succeeds", [True, False])
def test_a_missing_body_recovery_keeps_other_sources_and_reports_its_outcome(
    durable_database: Database, repair_succeeds: bool
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    _source(durable_database, workspace_id, "Globex", "https://boards.greenhouse.io/globex")
    _state_titles(durable_database, workspace_id, "data")
    acme_url = "https://boards.greenhouse.io/acme/jobs/1"
    globex_url = "https://boards.greenhouse.io/globex/jobs/2"
    boards = ScriptedBoards(
        {
            "Acme": [
                replace(
                    _listing("Data Platform Lead", acme_url),
                    published_description="Own the data platform.",
                )
            ],
            "Globex": [],
        }
    )
    reads = _RecordingReads()

    class RecoveryResolver:
        def resolve(self, url: str, **kwargs: Any) -> ResolvedPostingContent:
            result = reads.resolve(url, **kwargs)
            if repair_succeeds:
                return result
            return replace(
                result,
                content=None,
                content_status=PostingContentAccessStatus.FETCH_FAILED,
                identity_status=PostingIdentityStatus.NOT_YET_VERIFIED,
                posting_status=None,
                extraction_method=None,
                note="Publisher request failed; retry the posting read.",
                http_status=503,
            )

    storage = runtime_storage(durable_database)
    resolution = PostingContentResolutionService(
        durable_database, RecoveryResolver(), storage=storage
    )
    service = SourceScanService(
        durable_database,
        storage=storage,
        fetcher=boards,
        posting_fetcher=UnexpectedPostingFetcher(),
        content_resolution=resolution,
    )
    service.scan(workspace_id)
    pending_id = _pending_id_for(durable_database, acme_url)
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        content = session.get(PendingPostingContent, pending.current_content_id)
        assert content is not None
        artifact = session.get(Artifact, content.artifact_id)
        assert artifact is not None
        stored = file_uri_path(artifact.storage_uri)
    assert stored is not None
    stored.unlink()
    boards.boards["Acme"] = [_listing("Data Platform Lead", acme_url)]
    boards.boards["Globex"] = [
        replace(_listing("Data Engineer", globex_url), published_description="Build pipelines.")
    ]

    result = service.scan(workspace_id)

    assert result["new_discoveries"] == 1
    assert _pending_id_for(durable_database, globex_url)
    assert reads.urls == [acme_url]
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        content = session.get(PendingPostingContent, pending.current_content_id)
        assert content is not None
        if repair_succeeds:
            assert content.content_status == PostingContentAccessStatus.READABLE.value
            assert "retained posting body" in pending.content_note.lower()
            assert "unavailable" in pending.content_note.lower()
            assert "unavailable" in content.identity_evidence["retained_body_recovery"].lower()
            assert resolution.current.read(workspace_id, pending_id) is not None
        else:
            assert content.content_status == PostingContentAccessStatus.FETCH_FAILED.value
            assert "retry" in pending.content_note.lower()
            assert "remains current" not in pending.content_note
            assert content.artifact_id is None


@pytest.mark.parametrize(
    ("staged_url", "read_address", "listed_url"),
    [
        (_GREENHOUSE_STAGED_URL, None, _GREENHOUSE_STAGED_URL),
        (
            _WORKDAY_STAGED_URL,
            None,
            "https://acme.wd5.myworkdayjobs.com/careers/job/Canada-Remote/Data-Engineer_R30001",
        ),
        (_GREENHOUSE_STAGED_URL, _READ_ADDRESS, _READ_ADDRESS),
    ],
    ids=["same address", "workday path variant", "re-pointed read address"],
)
def test_a_scan_listing_a_known_row_at_another_address_retains_and_screens_its_body(
    durable_database: Database, staged_url: str, read_address: str | None, listed_url: str
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.example.test/acme")
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            locations=[Location(key="us_remote", country="United States")],
            countries=[CountryNames(country="United States")],
            seniority=Seniority(target_titles=titles("Data Engineer")),
        ),
    )
    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": [_listing("Data Engineer", staged_url)]}),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)
    pending_id = _pending_id_for(durable_database, staged_url)
    if read_address is not None:
        with durable_database.session() as session:
            row = session.get(PendingDiscovery, pending_id)
            assert row is not None
            row.canonical_url = read_address
    listed = replace(
        _listing("Data Engineer", listed_url),
        published_description="Location: Amsterdam, Netherlands",
    )

    result = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": [listed]}),
        posting_fetcher=UnexpectedPostingFetcher(),
        content_resolution=PostingContentResolutionService(
            durable_database, UnexpectedContentResolver(), storage=runtime_storage(durable_database)
        ),
    ).scan(workspace_id)

    with durable_database.session() as session:
        [row] = session.scalars(select(PendingDiscovery)).all()
        dismissals = session.scalars(
            select(PendingDiscoveryDispositionEvent).where(
                PendingDiscoveryDispositionEvent.pending_discovery_id == pending_id
            )
        ).all()
        assert row.id == pending_id
        assert row.canonical_url == (read_address or staged_url)
        evidence = session.get(PendingPostingContent, row.current_content_id)
        assert evidence is not None and usable_current_evidence(row, evidence)
    assert [(item.origin, item.category) for item in dismissals] == [
        ("source.scan.body_screen", "location_outside")
    ]
    assert [
        outcome["reason"]
        for outcome in result["funnel"]["outcomes"]
        if outcome["url"] == listed_url
    ] == ["hard_constraint_excluded"]


def test_a_rescan_does_not_report_an_old_body_dismissal_its_unverified_read_never_judged(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.example.test/acme")
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            locations=[Location(key="us_remote", country="United States")],
            countries=[CountryNames(country="United States")],
            seniority=Seniority(target_titles=titles("Data Engineer")),
        ),
    )

    class ContradictedResolver:
        # Identity fails on this read, so the body screen never judges its body.
        def resolve(self, url: str, **_kwargs: object) -> ResolvedPostingContent:
            return ResolvedPostingContent(
                requested_url=url,
                final_url=url,
                content_status=PostingContentAccessStatus.READABLE,
                identity_status=PostingIdentityStatus.CONTRADICTED,
                posting_status=PostingStatus.ACTIVE,
                extraction_method=PostingContentExtractionMethod.STATIC_HTML,
                fetched_at=utc_now(),
                content=b"<html><body><p>Location: Ridgefield, CT</p></body></html>",
                http_status=200,
                publisher_markup_present=True,
                identity_evidence={"publisher_declaration": True},
                note="Publisher employer differs.",
            )

    def scan(listing: SourceListing, resolver: Any) -> dict[str, Any]:
        return SourceScanService(
            durable_database,
            storage=runtime_storage(durable_database),
            fetcher=ScriptedBoards({"Acme": [listing]}),
            posting_fetcher=UnexpectedPostingFetcher(),
            content_resolution=PostingContentResolutionService(
                durable_database, resolver, storage=runtime_storage(durable_database)
            ),
        ).scan(workspace_id)

    url = _GREENHOUSE_STAGED_URL
    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": [_listing("Data Engineer", url)]}),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)
    first = scan(
        replace(_listing("Data Engineer", url), published_description="Location: Bogota, Colombia"),
        UnexpectedContentResolver(),
    )
    assert [o["reason"] for o in first["funnel"]["outcomes"] if o["url"] == url] == [
        "hard_constraint_excluded"
    ]

    # The publisher read that followed failed identity on the same body, as a re-read
    # whose employer label differs from the board's does.
    PostingContentResolutionService(
        durable_database, UnexpectedContentResolver(), storage=runtime_storage(durable_database)
    ).store_prefetched_result(
        workspace_id,
        _pending_id_for(durable_database, url),
        ContradictedResolver().resolve(url),
        None,
        origin="source.scan",
    )

    result = scan(_listing("Data Engineer", url), ContradictedResolver())

    with durable_database.session() as session:
        row = session.get(PendingDiscovery, _pending_id_for(durable_database, url))
        assert row is not None
        current = session.get(PendingPostingContent, row.current_content_id)
        assert current is not None
        assert current.identity_status == PostingIdentityStatus.CONTRADICTED.value
    outcomes = [o for o in result["funnel"]["outcomes"] if o["url"] == url]
    assert outcomes
    assert all(o["reason"] != "hard_constraint_excluded" for o in outcomes), [
        (o["reason"], o.get("detail"), o["screening_findings"]) for o in outcomes
    ]
    assert all("Colombia" not in str(o["screening_findings"]) for o in outcomes)


def test_a_held_out_regional_sibling_leaves_the_wanted_row_of_its_requisition_alone(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://acme.wd5.myworkdayjobs.com/careers")
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            locations=[Location(key="us_remote", country="United States")],
            countries=[CountryNames(country="United States")],
            seniority=Seniority(target_titles=titles("Data Engineer")),
        ),
    )
    wanted_url = "https://acme.wd5.myworkdayjobs.com/careers/job/US-Remote/Data-Engineer_R30001"
    held_out_url = (
        "https://acme.wd5.myworkdayjobs.com/careers/job/Canada-Remote/Data-Engineer_R30001"
    )
    listings = [
        replace(
            _listing("Data Engineer", url),
            location=place,
            facts=published_listing_facts("workday", locations=place),
        )
        for url, place in (
            (wanted_url, "Remote - United States"),
            (held_out_url, "Toronto, Canada"),
        )
    ]
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": listings}),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    result = service.scan(workspace_id)
    service.scan(workspace_id)

    assert [
        (item.canonical_url, item.location)
        for item in service.list_pending_discoveries(workspace_id)
    ] == [(wanted_url, "Remote - United States")]
    with durable_database.session() as session:
        assert session.scalars(select(PendingDiscoveryDispositionEvent)).all() == []
    assert {outcome["url"]: outcome["reason"] for outcome in result["funnel"]["outcomes"]} == {
        wanted_url: "staged",
        held_out_url: "hard_constraint_excluded",
        "https://acme.wd5.myworkdayjobs.com/careers": "staged",
    }


def test_an_office_listing_at_a_remote_only_market_records_the_arrangement_that_refused_it(
    durable_database: Database,
) -> None:
    """The stored outcome names the refusal even when screening alone left the arrangement open."""
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    remote_only = Arrangement(
        remote=Acceptance.PREFERRED,
        hybrid=Acceptance.REFUSED,
        on_site=Acceptance.REFUSED,
        maximum_office_days=0,
    )
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            locations=[Location(key="us_remote", country="United States", arrangement=remote_only)],
            countries=[CountryNames(country="United States")],
            arrangement=remote_only,
            seniority=Seniority(target_titles=titles("Data Engineer")),
        ),
    )
    url = "https://boards.greenhouse.io/acme/jobs/denver-office"
    listing = replace(
        _listing("Data Engineer", url, "Acme", location="Denver, Colorado"),
        facts=published_listing_facts(
            "greenhouse", locations=["Denver, Colorado", "United States"]
        ),
        published_description="You will join the data platform team at our downtown office.",
    )
    handler = SourceScanHandler(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": [listing]}),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={},
        idempotency_key="arrangement-refusal-recorded",
    )
    handlers = default_handlers(durable_database, fetcher=ScriptedBoards({"Acme": [listing]}))
    handlers["source.scan"] = handler
    worker = Worker(durable_database, worker_id="arrangement-refusal-recorded", handlers=handlers)

    assert worker.run_once()
    finished = queue.get(str(task.id))
    assert finished.status == "completed", (finished.error_category, finished.error_message)
    with durable_database.session() as session:
        [outcome] = session.scalars(
            select(SourceScanOutcome).where(
                SourceScanOutcome.task_id == str(task.id), SourceScanOutcome.url == url
            )
        ).all()
    assert outcome.reason == "hard_constraint_excluded"
    assert [
        item["code"] for item in outcome.screening_findings if item["dimension"] == "arrangement"
    ] == ["arrangement_unknown", "arrangement_remote_only_sites"]


def _pending_id_for(database: Database, url: str) -> str:
    """Return the pending discovery staged for one canonical URL."""
    with database.session() as session:
        return session.scalars(
            select(PendingDiscovery.id).where(PendingDiscovery.canonical_url == url)
        ).one()


def test_an_employer_board_with_no_stated_title_is_read_but_stages_nothing(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    boards = ScriptedBoards(
        {
            "Acme": [
                _listing("Data Platform Lead", "https://boards.greenhouse.io/acme/jobs/1"),
                _listing("Sales Director", "https://boards.greenhouse.io/acme/jobs/2"),
            ]
        }
    )
    result = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=boards,
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    assert result["new_discoveries"] == 0
    assert [(item["open"], item["staged"]) for item in result["scanned"]] == [(2, 0)]
    with durable_database.session() as session:
        assert session.scalars(select(PendingDiscovery)).all() == []
        scan = session.scalars(select(SourceScan)).one()
        assert (scan.status, scan.read_count, scan.result_count) == ("completed", 2, 0)
        assert "no title is stated" in scan.notes
        assert scan.complete is True


def test_an_unfiltered_employer_board_stages_nothing_and_leaves_an_excluded_title_unmatched(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(seniority=Seniority(excluded_titles=["intern"])),
    )
    boards = ScriptedBoards(
        {
            "Acme": [
                _listing("Data Platform Lead", "https://boards.greenhouse.io/acme/jobs/1"),
                _listing("Data Intern", "https://boards.greenhouse.io/acme/jobs/2"),
            ]
        }
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=boards,
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    result = service.scan(workspace_id)

    assert result["new_discoveries"] == 0
    assert service.list_pending_discoveries(workspace_id) == []
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
    assert scan.notes.startswith("Read 1 openings and staged none")


def test_a_board_that_reads_zero_openings_records_completed_empty(
    durable_database: Database,
) -> None:
    """A silent zero-read is distinct from a completed scan that filtered listings out."""
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Initrode", "https://boards.greenhouse.io/initrode")
    _state_titles(durable_database, workspace_id, "Data Platform Lead")
    boards = ScriptedBoards({"Initrode": []})

    result = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=boards,
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    assert result["new_discoveries"] == 0
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert scan.status == "completed_empty"
        assert scan.read_count == 0
        assert scan.result_count == 0
        # The whole point of the sub-status is the note the operator reads:
        # it must explain that the platform was reached and the answer was empty.
        assert "The board API answered with zero openings" in scan.notes
        assert "not a fetch failure" in scan.notes


def test_a_nonempty_raw_board_that_parses_no_postings_records_the_skip_reason(
    durable_database: Database,
) -> None:
    """A parser failure must not look like a board that published no records."""
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://jobs.lever.co/globex")
    _state_titles(durable_database, workspace_id, "Data Platform Lead")

    class RawOnlyBoard:
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
                complete=True,
                raw_count=3,
                skip_reasons={"missing_required_posting_identity": 3},
                outcome=SourceReadOutcome.ATS_COMPLETE,
            )

    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=RawOnlyBoard(),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert (scan.raw_count, scan.read_count, scan.result_count) == (3, 0, 0)
        assert scan.skip_reasons == {"missing_required_posting_identity": 3}
        assert "returned 3 raw records" in scan.notes
        assert "none became usable postings" in scan.notes

    [record] = (
        SourceScanService(
            durable_database,
            storage=runtime_storage(durable_database),
            posting_fetcher=UnexpectedPostingFetcher(),
        )
        .list_scan_history(workspace_id)
        .items
    )
    assert (record.raw_count, record.read_count, record.result_count) == (3, 0, 0)
    dumped = record.skip_reasons.model_dump()
    assert dumped["missing_required_posting_identity"] == 3
    assert dumped["bounded_result_cap"] == 0
    # Every remaining screening dimension is present at zero.
    assert all(
        value == 0 for key, value in dumped.items() if key != "missing_required_posting_identity"
    )


def test_a_board_that_returns_openings_without_title_evidence_stays_out_of_pending(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    _state_titles(durable_database, workspace_id, "Data Platform Lead")
    boards = ScriptedBoards(
        {"Acme": [_listing("Sales Director", "https://boards.greenhouse.io/acme/jobs/1")]}
    )

    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=boards,
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert scan.status == "completed"
        assert scan.read_count == 1
        assert scan.result_count == 0
        assert session.scalars(select(PendingDiscovery)).all() == []


def test_a_publisher_crawl_page_without_a_posting_identity_stays_out_of_pending(
    durable_database: Database,
) -> None:
    """A probed site page that states no posting is read, not staged, however its heading reads."""
    workspace_id = _workspace(durable_database)
    host = "https://www.rivertonhealth.example"
    _source(durable_database, workspace_id, "Riverton Health", f"{host}/careers")
    _state_titles(durable_database, workspace_id, "Allied Health Programs")
    headings = ("Allied Health Programs", "Help Centre", "Site Map", "Privacy Statement")
    root = (
        "<main><ul>"
        + "".join(
            f'<li><a href="/info/page-{1040 + index}.aspx">{heading}</a></li>'
            for index, heading in enumerate(headings)
        )
        + "</ul></main>"
    )
    pages = {
        f"{host}/info/page-{1040 + index}.aspx": f"""
        <main><article><h1>{heading}</h1>
        <h2>What we publish</h2>
        <p>Riverton Health publishes plain-language information for residents across the
        region, including how to reach a program, what a referral needs, and where each
        clinic sits inside the wider network of community services it runs.</p>
        <h2>Finding a clinic</h2>
        <p>Use the directory to find a clinic near you and the hours that it keeps.</p>
        </article></main>
        """
        for index, heading in enumerate(headings)
    }
    publisher = PublisherSourceFetcher(
        client=policy_client(
            lambda url: httpx.Response(200, request=httpx.Request("GET", url), text=pages[url])
        )
    )

    class PublisherBoard:
        def listings(
            self,
            _source_name: str,
            url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return publisher.read("Riverton Health", url, root)

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

    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=PublisherBoard(),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert scan.result_count == 0
        assert session.scalars(select(PendingDiscovery)).all() == []


def test_strict_matching_still_searches_under_an_accepted_title(
    durable_database: Database,
) -> None:
    """Matching says how literally a title is read, not whether an accepted one counts."""
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    _accept_title(durable_database, workspace_id, "Data Platform Lead")
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(seniority=Seniority(title_matching="strict")),
    )
    boards = ScriptedBoards(
        {
            "Acme": [
                _listing("Senior Data Platform Lead", "https://boards.greenhouse.io/acme/jobs/1"),
                _listing("Sales Director", "https://boards.greenhouse.io/acme/jobs/2"),
            ]
        }
    )

    result = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=boards,
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    assert [item["title"] for item in result["discoveries"]] == [
        "Senior Data Platform Lead",
    ]


def test_a_board_larger_than_the_processing_chunk_retains_every_candidate(
    durable_database: Database,
) -> None:
    """Processing bounds must not silently turn into discovery loss."""
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            seniority=Seniority(target_titles=titles("data"), excluded_titles=["intern"])
        ),
    )
    oversized = SOURCE_STAGE_LIMIT + 25
    boards = ScriptedBoards(
        {
            "Acme": [
                _listing(
                    f"Data Engineer {index}", f"https://boards.greenhouse.io/acme/jobs/{index}"
                )
                for index in range(oversized)
            ]
            + [
                _listing("Data Intern", f"https://boards.greenhouse.io/acme/jobs/intern-{index}")
                for index in range(3)
            ]
        }
    )

    result = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=boards,
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    assert result["new_discoveries"] == oversized
    with durable_database.session() as session:
        assert len(session.scalars(select(PendingDiscovery)).all()) == oversized
        scan = session.scalars(select(SourceScan)).one()
        assert scan.read_count == oversized + 3
        assert "title_excluded" not in scan.skip_reasons
        assert "exceeded the per-source staging limit" not in scan.notes
    assert not any(
        outcome["reason"] == "stage_limit_reached" for outcome in result["funnel"]["outcomes"]
    )
    outcomes = result["funnel"]["outcomes"]
    insufficient = sum(outcome["reason"] == "insufficient_title_evidence" for outcome in outcomes)
    assert insufficient == 3


def test_a_rescan_refreshes_existing_rows_outside_the_new_discovery_limit(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    _state_titles(durable_database, workspace_id, "Data Engineer")
    total = SOURCE_STAGE_LIMIT + 1
    boards = ScriptedBoards(
        {
            "Acme": [
                _listing(
                    "Data Engineer",
                    f"https://boards.greenhouse.io/acme/jobs/{index}",
                    location="Remote",
                )
                for index in range(total)
            ]
        }
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=boards,
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    service.scan(workspace_id)
    boards.boards["Acme"] = [
        _listing(
            "Data Engineer",
            f"https://boards.greenhouse.io/acme/jobs/{index}",
            location="Northtown",
        )
        for index in range(total)
    ]

    result = service.scan(workspace_id)

    assert result["new_discoveries"] == 0
    queue = service.list_pending_discoveries(workspace_id, limit=250)
    assert len(queue) == total
    assert all(item.location == "Northtown" for item in queue)
    assert result["funnel"]["counts"]["rejected"] == 0
    assert not any(
        outcome["reason"] == "stage_limit_reached" for outcome in result["funnel"]["outcomes"]
    )


class _CountingBoards:
    """Record how many board reads overlap; a supplied hold can force real overlap."""

    def __init__(self, names: list[str], hold: Callable[[], None] = lambda: None) -> None:
        self.names = names
        self.guard = threading.Lock()
        self.inside = 0
        self.busiest = 0
        self.requested: list[str] = []
        self.hold = hold

    def listings(
        self,
        source_name: str,
        url: str,
        *,
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        with self.guard:
            self.inside += 1
            self.busiest = max(self.busiest, self.inside)
            self.requested.append(source_name)
        self.hold()
        with self.guard:
            self.inside -= 1
        if source_name == "Fails":
            raise httpx.ConnectError("the board did not answer")
        return SourceRead(
            listings=[
                _listing("Data Platform Lead", f"https://boards.example.test/{source_name}/1")
            ],
            complete=True,
            raw_count=1,
            outcome=SourceReadOutcome.ATS_COMPLETE,
        )


class _SweepLanes:
    """Hold the first read in each of the sweep's lanes until every lane is inside one.

    The party count is the sweep pool's own size. A lane that finishes before the
    meeting breaks the barrier and sets ``missed``, so waiters fail rather than
    hang. A read on the sweep's calling thread is counted but never held.
    """

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, missed: threading.Event | None = None
    ) -> None:
        self.guard = threading.Lock()
        self.inside = 0
        self.busiest = 0
        self.lanes = 0
        self.caller = threading.get_ident()
        self.released = threading.Event()
        self.missed = missed or threading.Event()
        self.barrier: threading.Barrier | None = None
        lanes = self

        class SizedPool(ThreadPoolExecutor):
            def __init__(self, max_workers: int, **kwargs: Any) -> None:
                super().__init__(max_workers, **kwargs)
                lanes.lanes = max_workers
                lanes.barrier = threading.Barrier(max_workers, action=lanes.released.set)

            def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
                return super().submit(lanes.run_lane, fn, *args, **kwargs)

        monkeypatch.setattr(
            "clearscout.application.discovery.scan_sweep.sweep.ThreadPoolExecutor", SizedPool
        )

    def run_lane(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run one submitted lane, breaking the meeting point if it ends before it."""
        try:
            return fn(*args, **kwargs)
        finally:
            if not self.released.is_set():
                if self.barrier is not None:
                    self.barrier.abort()
                self.missed.set()

    def hold(self, *_args: object) -> None:
        """Count one read in flight, waiting at the rendezvous if it opens a lane."""
        with self.guard:
            self.inside += 1
            self.busiest = max(self.busiest, self.inside)
        on_lane = threading.get_ident() != self.caller
        if on_lane and self.barrier is not None and not self.released.is_set():
            # A broken meeting leaves the overlap short, which the caller asserts.
            with contextlib.suppress(threading.BrokenBarrierError):
                self.barrier.wait()
        with self.guard:
            self.inside -= 1


def test_boards_are_read_a_few_at_a_time_rather_than_one_after_another(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boards are read concurrently, up to SCAN_LANES at once.

    The bound matters as much as the concurrency: this is one person's job
    search reading employer boards, not a crawler.
    """
    workspace_id = _workspace(durable_database)
    names = [f"Board {index:02d}" for index in range(SCAN_LANES * 2)]
    for name in names:
        _source(durable_database, workspace_id, name, f"https://boards.example.test/{name}")
    lanes = _SweepLanes(monkeypatch)
    boards = _CountingBoards(names, hold=lanes.hold)

    result = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=boards,
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    assert sorted(boards.requested) == sorted(names)
    assert boards.busiest == SCAN_LANES
    # Reported in the order the scan considered them, not the order they replied.
    assert [item["source"] for item in result["scanned"]] == names


class _ProbeLock:
    """A host lock that says when a second caller has to wait for it."""

    def __init__(self, contended: threading.Event) -> None:
        self._lock = threading.Lock()
        self._contended = contended

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if self._lock.locked():
            self._contended.set()
        return self._lock.acquire(blocking, timeout)

    def release(self) -> None:
        self._lock.release()


class _ProbeLocks(dict[str, Any]):
    """Hand out a probe lock for every host the client locks."""

    def __init__(self, contended: threading.Event) -> None:
        super().__init__()
        self.contended = contended

    def setdefault(self, key: str, default: Any = None) -> Any:
        return super().setdefault(key, _ProbeLock(self.contended))


def test_boards_on_one_host_take_turns_across_every_lane(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every lane reading a board on one host still sends that host one request at a time."""
    lane_count = 8
    monkeypatch.setattr(sweep_module, "SCAN_LANES", lane_count)
    workspace_id = _workspace(durable_database)
    slugs = [f"employer{index}" for index in range(lane_count)]
    for slug in slugs:
        _source(durable_database, workspace_id, slug, f"https://boards.greenhouse.io/{slug}")
    # Set when a second request waits on the host lock, gets past it, or a lane ends early.
    second_request = threading.Event()
    monkeypatch.setattr(PoliteHttpClient, "_host_locks", _ProbeLocks(second_request))
    lanes = _SweepLanes(monkeypatch, missed=second_request)
    guard = threading.Lock()
    inside = {"now": 0, "busiest": 0, "board_reads": 0}

    def answer(url: str) -> httpx.Response:
        with guard:
            inside["now"] += 1
            inside["busiest"] = max(inside["busiest"], inside["now"])
            inside["board_reads"] += 1
            first = inside["board_reads"] == 1
            if inside["now"] > 1:
                second_request.set()
        # Only a second lane can set the event, so a single lane does not wait.
        if first and lanes.lanes > 1:
            second_request.wait()
        with guard:
            inside["now"] -= 1
        slug = url.split("/boards/", 1)[1].split("/", 1)[0]
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={
                "jobs": [
                    {
                        "title": "Data Platform Lead",
                        "absolute_url": f"https://boards.greenhouse.io/{slug}/jobs/1",
                    }
                ]
            },
        )

    result = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=BoardApiFetcher(client=policy_client(answer)),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    assert result["failed"] == []
    assert [row["source"] for row in result["scanned"]] == slugs
    assert lanes.lanes == lane_count
    assert inside["board_reads"] == lane_count
    # A second lane reached the host while the first read held it, and had to wait.
    assert second_request.is_set()
    assert inside["busiest"] == 1


def test_one_board_that_fails_does_not_take_the_others_with_it(
    durable_database: Database,
) -> None:
    """Reads run concurrently, so a raised error has to stay with its source."""
    workspace_id = _workspace(durable_database)
    names = ["Fails", "Acme", "Globex"]
    for name in names:
        _source(durable_database, workspace_id, name, f"https://boards.example.test/{name}")
    boards = _CountingBoards(names)

    result = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=boards,
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    assert [item["source"] for item in result["failed"]] == ["Fails"]
    assert sorted(item["source"] for item in result["scanned"]) == ["Acme", "Globex"]
    with durable_database.session() as session:
        recorded = session.scalars(select(SourceScan)).all()
    assert sorted(item.status for item in recorded) == ["completed", "completed", "failed"]


@pytest.mark.parametrize(
    "refusal",
    [
        lambda url: IngestError(f"Only 70 characters of text came out of {url}"),
        RobotsDeniedError,
    ],
    ids=["thin-page", "robots-denied"],
)
def test_a_tracked_posting_that_cannot_be_reread_does_not_fail_the_scan(
    durable_database: Database, refusal: Callable[[str], Exception]
) -> None:
    workspace_id = _workspace(durable_database)
    tracked_url = "https://apply.example.test/j/ABC123"
    acme = _source(durable_database, workspace_id, "Acme", "https://apply.example.test/acme")
    _source(durable_database, workspace_id, "Globex", "https://boards.example.test/globex")
    _state_titles(durable_database, workspace_id, "data")
    job_id = tracked_board_posting(
        durable_database, workspace_id, acme, title="Data Platform Lead", url=tracked_url
    )
    boards = ScriptedBoards(
        {
            "Acme": [_listing("Data Platform Lead", tracked_url, employer="Acme")],
            "Globex": [],
        }
    )
    fetched: list[str] = []

    class ThinPageFetcher:
        def fetch(
            self,
            url: str,
            *,
            intent: PostingReadIntent = PostingReadIntent.CRAWL,
            board_url: str = "",
        ) -> FetchedPosting:
            fetched.append(url)
            raise refusal(url)

    result = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=boards,
        posting_fetcher=ThinPageFetcher(),
    ).scan(workspace_id)

    assert fetched == [tracked_url]
    assert sorted(item["source"] for item in result["scanned"]) == ["Acme", "Globex"]
    assert result["failed"] == []
    expected = refusal(tracked_url)
    assert result["refresh_failed"] == [
        {
            "source": "Acme",
            "job_posting_id": job_id,
            "error_category": type(expected).__name__,
            "error_message": str(expected),
        }
    ]


@pytest.mark.parametrize("supplied_date", [False, True])
def test_scan_refreshes_tracked_posting_from_its_full_publisher_body(
    durable_database: Database, supplied_date: bool
) -> None:
    workspace_id = _workspace(durable_database)
    url = "https://publisher.example/acme/jobs/data-lead"
    source_id = _source(
        durable_database, workspace_id, "Acme", "https://publisher.example/acme/jobs"
    )
    _state_titles(durable_database, workspace_id, "data")
    job_id = tracked_board_posting(
        durable_database, workspace_id, source_id, title="Data Platform Lead", url=url
    )
    body = "Lead data platform delivery and maintain clinical integrations. " * 12
    date = publisher_closing_date(
        "", valid_through="2099-12-01", source_url=url, observed_at=utc_now()
    )
    assert date is not None
    if not supplied_date:
        body += "\nApplications close December 1, 2099."
    markup = "<p>" + body + "</p>"
    listing = replace(
        _listing("Data Platform Lead", url, employer="Acme"),
        published_description=body,
        summary="A bounded summary must not become the retained body.",
        facts=StagedListingFacts(closing_date=date if supplied_date else None),
        source=PostingSource(
            publisher="darwinbox",
            record=json.dumps({"id": "data-lead", "description": markup}),
            description_markup=markup,
            description_paths=("description",),
        ),
    )
    reads: list[str] = []

    class ThinPageFetcher:
        def fetch(
            self,
            url: str,
            *,
            intent: PostingReadIntent = PostingReadIntent.CRAWL,
            board_url: str = "",
        ) -> FetchedPosting:
            reads.append(url)
            raise IngestError("The generic detail page contains only a metadata stub.")

    storage = runtime_storage(durable_database)
    service = SourceScanService(
        durable_database,
        storage=storage,
        fetcher=ScriptedBoards({"Acme": [listing]}),
        posting_fetcher=ThinPageFetcher(),
    )

    result = service.scan(workspace_id)

    assert result["refresh_failed"] == []
    assert reads == []
    with durable_database.session() as session:
        observations = list(
            session.scalars(
                select(PostingObservation)
                .where(PostingObservation.job_posting_id == job_id)
                .order_by(PostingObservation.observed_at)
            )
        )
        assert len(observations) == 2
        current = observations[-1]
        assert current.content_sha256 == hashlib.sha256(body.encode()).hexdigest()
        assert current.extraction_method == "static_structured"
        assert current.http_status is None and current.robots_basis is None
        assert current.publisher_closing_date is not None
        assert current.publisher_closing_date["normalized_value"] == "2099-12-01"
        artifacts = list(
            session.scalars(select(Artifact).where(Artifact.related_entity_id == current.id))
        )
        files = {row.logical_type: row for row in artifacts}
        assert current.body_artifact_id == files["posting.body.text"].id
        paths = {kind: file_uri_path(row.storage_uri) for kind, row in files.items()}
        contents: dict[str, str] = {}
        for kind, path in paths.items():
            assert path is not None
            contents[kind] = path.read_text()
        assert contents["posting.body.text"] == body
        assert contents["posting.body.html"] == markup
        assert json.loads(contents["posting.source.json"]) == {
            "id": "data-lead",
            "description": markup,
        }
        statement = current.publisher_closing_date["statements"][0]
        assert statement["artifact_id"] == (
            files["posting.source.json"].id if supplied_date else current.body_artifact_id
        )
        assert list(session.scalars(select(ModelCall))) == []
        assert list(session.scalars(select(PendingDiscovery))) == []


@pytest.mark.parametrize("evidence", ["summary_only", "no_source", "unverified_identity"])
def test_scan_fetches_tracked_postings_without_a_vouched_full_publisher_body(
    durable_database: Database, evidence: str
) -> None:
    workspace_id = _workspace(durable_database)
    url = "https://publisher.example/acme/jobs/data-lead"
    source_id = _source(
        durable_database, workspace_id, "Acme", "https://publisher.example/acme/jobs"
    )
    _state_titles(durable_database, workspace_id, "data")
    job_id = tracked_board_posting(
        durable_database, workspace_id, source_id, title="Data Platform Lead", url=url
    )
    listing = replace(
        _listing("Data Platform Lead", url, employer="Acme"),
        summary="A bounded summary is not the full publisher body.",
        published_description="" if evidence == "summary_only" else "Unvouched body.",
        source=None
        if evidence == "no_source"
        else PostingSource(publisher="darwinbox", record='{"id": "data-lead"}'),
        facts=StagedListingFacts(
            identity_trust=(
                ListingIdentityTrust.UNVERIFIED_HINT
                if evidence == "unverified_identity"
                else ListingIdentityTrust.PUBLISHER
            )
        ),
    )
    reads: list[str] = []
    body = "Lead data platform delivery and maintain clinical integrations. " * 12

    class FullPageFetcher:
        def fetch(
            self,
            url: str,
            *,
            intent: PostingReadIntent = PostingReadIntent.CRAWL,
            board_url: str = "",
        ) -> FetchedPosting:
            reads.append(url)
            return FetchedPosting(body, "active", "static_structured", utc_now(), final_url=url)

    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": [listing]}),
        posting_fetcher=FullPageFetcher(),
    )

    result = service.scan(workspace_id)

    assert result["refresh_failed"] == []
    assert reads == [url]
    with durable_database.session() as session:
        current = session.scalar(
            select(PostingObservation)
            .where(PostingObservation.job_posting_id == job_id)
            .order_by(PostingObservation.observed_at.desc())
        )
        assert current is not None
        assert current.content_sha256 == hashlib.sha256(body.encode()).hexdigest()
        assert list(session.scalars(select(ModelCall))) == []


def test_a_tracked_workable_short_link_refreshes_with_its_board_address(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    board_url = "https://apply.workable.com/acme/"
    posting_url = "https://apply.workable.com/j/AB12CD34EF"
    source = _source(durable_database, workspace_id, "Acme", board_url)
    _state_titles(durable_database, workspace_id, "data")
    tracked_board_posting(
        durable_database, workspace_id, source, title="Data Platform Lead", url=posting_url
    )
    boards = ScriptedBoards(
        {"Acme": [_listing("Data Platform Lead", posting_url, employer="Acme")]}
    )
    read_board_urls: list[str] = []

    class BoardAwareFetcher:
        def fetch(
            self,
            url: str,
            *,
            intent: PostingReadIntent = PostingReadIntent.CRAWL,
            board_url: str = "",
        ) -> FetchedPosting:
            read_board_urls.append(board_url)
            return FetchedPosting(
                text="Lead data platform delivery and maintain clinical integrations. " * 12,
                posting_status="active",
                method="Workable API",
                fetched_at=utc_now(),
                final_url=url,
            )

    result = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=boards,
        posting_fetcher=BoardAwareFetcher(),
    ).scan(workspace_id)

    assert read_board_urls == [board_url]
    assert result["refresh_failed"] == []


def _record_staging_threads(
    monkeypatch: pytest.MonkeyPatch, service: SourceScanService
) -> list[int]:
    """Return the thread of every staging write the service's sweep makes."""
    staged_on: list[int] = []
    stage = service.staging.stage_listings

    def recording(*args: Any, **kwargs: Any) -> Any:
        staged_on.append(threading.get_ident())
        return stage(*args, **kwargs)

    monkeypatch.setattr(service.staging, "stage_listings", recording)
    return staged_on


def test_each_boards_workday_detail_reads_run_in_its_own_lane(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two boards on different hosts read their details together; staging stays on one thread."""
    workspace_id = _workspace(durable_database)
    hosts = {"Globex": "globex.wd1.myworkdayjobs.com", "Initech": "initech.wd5.myworkdayjobs.com"}
    for name, host in hosts.items():
        _source(durable_database, workspace_id, name, f"https://{host}/Careers")
    _state_titles(durable_database, workspace_id, "Data Product Manager")
    boards = ScriptedBoards(
        {
            name: [
                SourceListing(
                    employer=name,
                    title="Data Product Manager",
                    url=f"https://{host}/en-US/Careers/job/Remote/Data-Product-Manager_R-1",
                    facts=published_listing_facts("workday", locations="2 Locations"),
                )
            ]
            for name, host in hosts.items()
        }
    )
    lanes = _SweepLanes(monkeypatch)

    def detail(url: str) -> PostingCheck:
        lanes.hold()
        return PostingCheck(
            status="readable",
            note="fixture",
            platform="workday",
            location="Remote, Canada",
            facts=published_listing_facts("workday", locations="Remote, Canada"),
            text="Own the data platform remotely in Canada.",
            source=PostingSource(
                publisher="workday", record="{}", description_paths=("description",)
            ),
        )

    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=boards,
        workday_detail_checker=detail,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    staged_on = _record_staging_threads(monkeypatch, service)

    result = service.scan(workspace_id)

    assert [row["detail"]["read"] for row in result["scanned"]] == [1, 1]
    assert lanes.busiest == 2
    assert staged_on == [threading.get_ident()] * 2


def test_each_boards_new_posting_bodies_are_read_in_its_own_lane(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Body reads for rows a board stages overlap across hosts and are stored by the caller."""
    workspace_id = _workspace(durable_database)
    urls = {
        "Acme": "https://boards.acme.example.test/jobs/1",
        "Globex": "https://careers.globex.example.test/jobs/1",
    }
    for name, url in urls.items():
        _source(durable_database, workspace_id, name, url.rsplit("/jobs", 1)[0])
    SearchParametersService(durable_database).save(
        workspace_id, JobSearchParameters(seniority=Seniority(target_titles=titles("data")))
    )
    lanes = _SweepLanes(monkeypatch)
    reads = _RecordingReads(on_read=lanes.hold)
    service = _body_reading_scan(
        durable_database,
        {name: [_listing("Data Platform Lead", url, employer=name)] for name, url in urls.items()},
        reads,
    )
    staged_on = _record_staging_threads(monkeypatch, service)

    result = service.scan(workspace_id)

    assert [row["content_resolution"]["attempted"] for row in result["scanned"]] == [1, 1]
    assert sorted(reads.urls) == sorted(urls.values())
    assert lanes.busiest == 2
    assert staged_on == [threading.get_ident()] * 2
    for url in urls.values():
        pending_id = _pending_id_for(durable_database, url)
        assert service.sweep.content_resolution is not None
        assert service.sweep.content_resolution.current.read(workspace_id, pending_id)


class _RecordingReads:
    """Answer every posting read with a readable body, recording each address read."""

    def __init__(self, on_read: Callable[[str], object] | None = None) -> None:
        self.urls: list[str] = []
        self.on_read = on_read

    def resolve(self, url: str, **_kwargs: Any) -> ResolvedPostingContent:
        self.urls.append(url)
        if self.on_read is not None:
            self.on_read(url)
        return ResolvedPostingContent(
            requested_url=url,
            final_url=url,
            content_status=PostingContentAccessStatus.READABLE,
            identity_status=PostingIdentityStatus.VERIFIED,
            posting_status=PostingStatus.ACTIVE,
            extraction_method=PostingContentExtractionMethod.STATIC_HTML,
            fetched_at=utc_now(),
            content=b"Own the data platform. Remote in the United States.",
            http_status=200,
            publisher_markup_present=True,
            identity_evidence={"publisher_declaration": True},
            note="Verified publisher body.",
        )


def _body_reading_scan(
    database: Database,
    listings: dict[str, list[SourceListing] | SourceRead],
    reads: _RecordingReads,
) -> SourceScanService:
    """A scan of these boards whose posting body reads go to ``reads``."""
    return SourceScanService(
        database,
        storage=runtime_storage(database),
        fetcher=ScriptedBoards(listings),
        posting_fetcher=UnexpectedPostingFetcher(),
        content_resolution=PostingContentResolutionService(
            database, reads, storage=runtime_storage(database)
        ),
    )


def test_a_rescan_reads_no_body_for_a_row_that_was_archived(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    url = "https://careers.globex.example.test/jobs/1"
    _source(durable_database, workspace_id, "Globex", "https://careers.globex.example.test")
    _state_titles(durable_database, workspace_id, "data")
    reads = _RecordingReads()
    service = _body_reading_scan(
        durable_database,
        {"Globex": [_listing("Data Platform Lead", url, employer="Globex")]},
        reads,
    )
    service.scan(workspace_id)
    with durable_database.session() as session:
        pending = session.scalar(select(PendingDiscovery))
        assert pending is not None
        pending.archived_at = utc_now()

    service.scan(workspace_id)

    assert reads.urls == [url]


@pytest.mark.parametrize("protection", ["manual", "retired_canonical"])
@pytest.mark.parametrize("published_body", [False, True])
def test_rediscovery_keeps_protected_content_and_resolves_an_eligible_sibling(
    durable_database: Database, protection: str, published_body: bool
) -> None:
    workspace_id = _workspace(durable_database)
    root = "https://careers.globex.example.test"
    source_id = _source(durable_database, workspace_id, "Globex", root)
    _state_titles(durable_database, workspace_id, "data")
    protected = _listing("Data Platform Lead", f"{root}/jobs/1", employer="Globex")
    canonical = f"{root}/jobs/retired"
    sibling = _listing("Data Platform Lead", f"{root}/jobs/2", employer="Globex")
    description = "Data Platform Lead. Own the data platform remotely. " * 12
    listed = replace(protected, published_description=description if published_body else "")
    requests: list[str] = []

    def response(url: str) -> httpx.Response:
        requests.append(url)
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            text=declaring_page(
                {
                    "@type": "JobPosting",
                    "title": sibling.title,
                    "hiringOrganization": {"name": sibling.employer},
                    "description": description,
                }
            ),
        )

    resolution = PostingContentResolutionService(
        durable_database,
        DeterministicPostingContentResolver(client=policy_client(response)),
        storage=runtime_storage(durable_database),
    )
    handler = SourceScanHandler(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Globex": [listed, sibling]}),
        posting_fetcher=UnexpectedPostingFetcher(),
        content_resolution=resolution,
    )
    prior = handler.service.staging.stage_listings(
        workspace_id, [protected], utc_now().date(), reason="Prior read", source_id=source_id
    )
    pending_id = prior.row_ids([protected])[0]
    with durable_database.session() as session:
        row = session.get(PendingDiscovery, pending_id)
        assert row is not None
        row.canonical_url = canonical
        evidence = pending_posting_content(
            workspace_id,
            pending_id,
            canonical,
            content_status=PostingContentAccessStatus.RENDER_REQUIRED,
            identity_status=PostingIdentityStatus.NOT_YET_VERIFIED,
            identity_evidence={"note": "Retained publisher rendering requirement."},
        )
        session.add(evidence)
        session.flush()
        previous_id = evidence.id
        row.current_content_id = previous_id
        row.content_status = PostingContentAccessStatus.RENDER_REQUIRED
        row.content_note = "Retained publisher rendering requirement."
    if protection == "manual":
        dismiss_pending_discovery(durable_database, workspace_id, pending_id)
    else:
        job_id = tracked_board_posting(
            durable_database, workspace_id, source_id, title=protected.title, url=canonical
        )
        with durable_database.session() as session:
            job = session.get(JobPosting, job_id)
            assert job is not None
            job.archived_at = utc_now()
    with durable_database.session() as session:
        previous_events = session.scalars(select(PendingDiscoveryDispositionEvent)).all()
        previous_event_ids = {event.id for event in previous_events}
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={},
        idempotency_key="protected-rediscovery",
    )
    worker = Worker(durable_database, worker_id="protected-scan", handlers={"source.scan": handler})

    assert worker.run_once()

    finished = queue.get(str(task.id))
    assert finished.status == "completed", (finished.error_category, finished.error_message)
    assert requests == [sibling.url]
    with durable_database.session() as session:
        row = session.get(PendingDiscovery, pending_id)
        assert row is not None
        assert row.current_content_id == previous_id
        assert row.content_status == PostingContentAccessStatus.RENDER_REQUIRED
        assert row.content_note == "Retained publisher rendering requirement."
        assert {
            event.id for event in session.scalars(select(PendingDiscoveryDispositionEvent))
        } == (previous_event_ids)
    sibling_id = _pending_id_for(durable_database, sibling.url)
    assert resolution.current.read(workspace_id, sibling_id) is not None
    assert finished.progress["scanned"][0]["content_resolution"]["attempted"] == 1


@pytest.mark.parametrize("known", [False, True])
def test_scan_body_reads_refuse_protected_redirects_and_keep_eligible_siblings(
    durable_database: Database, known: bool
) -> None:
    workspace_id = _workspace(durable_database)
    root = "https://careers.globex.example.test"
    source_id = _source(durable_database, workspace_id, "Globex", root)
    _state_titles(durable_database, workspace_id, "data")
    protected = _listing("Data Platform Lead", f"{root}/jobs/held", employer="Globex")
    listed = _listing("Data Platform Lead", f"{root}/jobs/1", employer="Globex")
    sibling = _listing("Data Platform Lead", f"{root}/jobs/2", employer="Globex")
    redirect_to = f"{protected.url}?utm_source=board"
    requests: list[str] = []

    def response(url: str) -> httpx.Response:
        requests.append(url)
        if url == listed.url:
            return httpx.Response(
                302, headers={"Location": redirect_to}, request=httpx.Request("GET", url)
            )
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            text=declaring_page(
                {
                    "@type": "JobPosting",
                    "title": sibling.title,
                    "hiringOrganization": {"name": sibling.employer},
                    "description": "Own the data platform remotely. " * 20,
                }
            ),
        )

    class NoBrowser:
        def fetch(self, _url: str, **_kwargs: object) -> object:
            raise RenderRuntimeUnavailableError("No browser in this test")

    resolver = DeterministicPostingContentResolver(
        client=policy_client(response), rendered=cast(Any, NoBrowser())
    )
    resolution = PostingContentResolutionService(
        durable_database, resolver, storage=runtime_storage(durable_database), automatic_lanes=2
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Globex": [listed, sibling]}),
        posting_fetcher=UnexpectedPostingFetcher(),
        content_resolution=resolution,
    )
    seeded = [protected, listed] if known else [protected]
    prior = service.staging.stage_listings(
        workspace_id, seeded, utc_now().date(), reason="Prior read", source_id=source_id
    )
    protected_id = prior.row_ids([protected])[0]
    dismiss_pending_discovery(durable_database, workspace_id, protected_id)

    result = service.scan(workspace_id)

    assert protected.url not in requests and redirect_to not in requests
    assert listed.url in requests and sibling.url in requests
    assert result["scanned"][0]["complete"] is True
    assert result["scanned"][0]["content_resolution"]["outcomes"].get("readable") == 1
    assert resolution.current.read(workspace_id, _pending_id_for(durable_database, sibling.url))
    with durable_database.session() as session:
        row = session.get(PendingDiscovery, protected_id)
        assert row is not None and row.current_content_id is None
        events = session.scalars(select(PendingDiscoveryDispositionEvent)).all()
        assert [(event.pending_discovery_id, event.action) for event in events] == [
            (protected_id, "dismiss")
        ]
    assert resolver.resolve(listed.url, employer="Globex", title=listed.title).content_status == (
        PostingContentAccessStatus.READABLE
    )
    assert redirect_to in requests


def test_a_scan_reads_no_body_for_a_posting_already_tracked(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = _workspace(durable_database)
    url = "https://careers.globex.example.test/jobs/1"
    source_id = _source(
        durable_database, workspace_id, "Globex", "https://careers.globex.example.test"
    )
    _state_titles(durable_database, workspace_id, "data")
    tracked_board_posting(
        durable_database, workspace_id, source_id, title="Data Platform Lead", url=url
    )
    reads = _RecordingReads()
    service = _body_reading_scan(
        durable_database,
        {"Globex": [_listing("Data Platform Lead", url, employer="Globex")]},
        reads,
    )
    # A tracked posting refreshes through the posting fetcher, not the body reader.
    monkeypatch.setattr(service.staging, "refresh_tracked", lambda *_args, **_kwargs: None)

    service.scan(workspace_id)

    assert reads.urls == []


@dataclass
class _CancellableContext:
    """A scan task whose cancellation the test requests partway through a board."""

    queue: TaskQueueService
    task_id: str = "scan-task"
    worker_id: str = "scan-worker"
    cancelled: threading.Event = field(default_factory=threading.Event)

    def progress(self, stage: str, payload: dict[str, Any]) -> None:
        return None

    def cancellation_requested(self) -> bool:
        return self.cancelled.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise TaskCancelledError

    def __post_init__(self) -> None:
        with self.queue.database.session() as session:
            workspace_id = session.scalar(select(Source.workspace_id).limit(1))
        assert workspace_id is not None
        if (
            not SearchParametersService(self.queue.database)
            .current(workspace_id)
            .seniority.target_titles
        ):
            _state_titles(self.queue.database, workspace_id, "Data Platform Lead")
        task = self.queue.enqueue(
            workspace_id=workspace_id, task_type="source.scan", payload={}, idempotency_key=new_id()
        )
        claimed = self.queue.claim_next(worker_id=self.worker_id)
        assert claimed is not None and claimed.id == task.id
        self.task_id = str(task.id)


def _workday_candidates(host: str, count: int) -> list[SourceListing]:
    return [
        SourceListing(
            employer="Globex",
            title="Data Product Manager",
            url=f"https://{host}/en-US/Careers/job/Remote/Data-Product-Manager_R-{index}",
            facts=published_listing_facts("workday", locations="2 Locations"),
        )
        for index in range(count)
    ]


def _readable_detail() -> PostingCheck:
    return PostingCheck(
        status="readable",
        note="fixture",
        platform="workday",
        location="Remote, Canada",
        facts=published_listing_facts("workday", locations="Remote, Canada"),
        text="Own the data platform remotely in Canada.",
        source=PostingSource(publisher="workday", record="{}", description_paths=("description",)),
    )


@pytest.mark.parametrize("missing", ["text", "source"])
def test_workday_readable_metadata_without_owned_body_remains_recoverable(
    durable_database: Database, missing: str
) -> None:
    workspace_id = _workspace(durable_database)
    host = "globex.wd1.myworkdayjobs.com"
    _source(durable_database, workspace_id, "Globex", f"https://{host}/en-US/Careers")
    _state_titles(durable_database, workspace_id, "Data Product Manager")
    listings = _workday_candidates(host, 1)

    class MetadataBoard(BoardApiFetcher):
        def listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            return SourceRead(
                listings=listings,
                complete=True,
                raw_count=1,
                outcome=SourceReadOutcome.ATS_COMPLETE,
            )

    checked = _readable_detail()
    checked = replace(checked, text="") if missing == "text" else replace(checked, source=None)
    result = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=MetadataBoard(
            client=policy_client(lambda url: httpx.Response(500, request=httpx.Request("GET", url)))
        ),
        workday_detail_checker=lambda url: checked,
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    assert result["scanned"][0]["complete"] is False
    with durable_database.session() as session:
        frontier = session.scalars(select(SourceScanFrontierItem)).one()
        assert frontier.url == listings[0].url
        assert frontier.deferred_reason == "workday_detail_pending"


def test_failed_workday_details_keep_every_recovery_identity_and_prevent_withdrawal(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    host = "globex.wd1.myworkdayjobs.com"
    board_url = f"https://{host}/Careers"
    source_id = _source(durable_database, workspace_id, "Globex", board_url)
    _state_titles(durable_database, workspace_id, "Data Product Manager")
    listings = _workday_candidates(host, 6)

    class RecoverableBoard(BoardApiFetcher):
        def listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            return SourceRead(
                listings=listings,
                complete=True,
                raw_count=len(listings),
                outcome=SourceReadOutcome.ATS_COMPLETE,
            )

    absent = _workday_candidates(host, 7)[-1]

    def detail(url: str) -> PostingCheck:
        if url == listings[0].url:
            return replace(_readable_detail(), text="Own the data platform remotely in Canada.")
        return PostingCheck(
            status="unreadable", note="Temporary detail refusal", platform="workday"
        )

    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=RecoverableBoard(),
        workday_detail_checker=detail,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    prior = service.staging.stage_listings(
        workspace_id, [absent], utc_now().date(), reason="Prior board read", source_id=source_id
    )

    result = service.scan(workspace_id)

    assert result["withdrawn"] == []
    assert result["scanned"][0]["complete"] is False
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        frontier = session.scalars(select(SourceScanFrontierItem)).all()
        absent_row = session.get(PendingDiscovery, prior.row_ids([absent])[0])
        assert absent_row is not None and absent_row.withdrawn_at is None
        assert scan.status == "incomplete"
        assert scan.read_outcome == SourceReadOutcome.CRAWL_CONTINUATION
        assert scan.detail_coverage is None
        assert {row.url for row in frontier} == {listing.url for listing in listings[1:]}
        assert all(row.deferred_reason == "workday_detail_pending" for row in frontier)
        checkpoints = [SourceDetailCheckpoint.from_json(row.detail_checkpoint) for row in frontier]
        assert [checkpoint.pending_ids for checkpoint in checkpoints] == [
            (row.url,) for row in frontier
        ]
        assert all(
            checkpoint.listing_ids == tuple(listing.url for listing in listings)
            for checkpoint in checkpoints
        )


def test_workday_listing_with_a_retained_complete_description_needs_no_detail_request(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    host = "globex.wd1.myworkdayjobs.com"
    _source(durable_database, workspace_id, "Globex", f"https://{host}/Careers")
    _state_titles(durable_database, workspace_id, "Data Product Manager")
    listing = replace(
        _workday_candidates(host, 1)[0],
        published_description="Own the data platform. Remote role in Canada.",
        facts=published_listing_facts("workday", locations="Remote, Canada"),
        source=PostingSource(publisher="workday", record="{}", description_paths=("description",)),
    )

    def detail(url: str) -> PostingCheck:
        pytest.fail(f"Completed description repeated its detail request: {url}")

    result = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Globex": [listing]}),
        workday_detail_checker=detail,
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    assert result["scanned"][0]["complete"] is True
    assert result["scanned"][0]["detail"]["attempted"] == 0


@pytest.mark.parametrize("continuation", [False, True])
@pytest.mark.parametrize("redirect_on_retry", [False, True])
def test_workday_detail_redirect_never_reads_a_protected_posting(
    durable_database: Database,
    continuation: bool,
    redirect_on_retry: bool,
) -> None:
    workspace_id = _workspace(durable_database)
    host = "globex.wd1.myworkdayjobs.com"
    root = f"https://{host}/en-US/Careers"
    source_id = _source(durable_database, workspace_id, "Globex", root)
    _state_titles(durable_database, workspace_id, "Data Product Manager")
    listed, protected = _workday_candidates(host, 2)
    requests: list[str] = []

    def response(url: str) -> httpx.Response:
        requests.append(url)
        if url == f"https://{host}/wday/cxs/globex/Careers/job/Remote/Data-Product-Manager_R-0":
            if redirect_on_retry and requests.count(url) == 1:
                return httpx.Response(503, request=httpx.Request("GET", url))
            return httpx.Response(
                302, headers={"Location": protected.url}, request=httpx.Request("GET", url)
            )
        assert url == protected.url
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={
                "jobPostingInfo": {
                    "title": "Data Product Manager",
                    "location": "Remote",
                    "jobDescription": "Own the data platform remotely.",
                    "jobReqId": "R-1",
                }
            },
        )

    client = policy_client(response)

    class RedirectBoard(BoardApiFetcher):
        def listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            return SourceRead(
                listings=[listed],
                complete=True,
                raw_count=1,
                outcome=SourceReadOutcome.ATS_COMPLETE,
            )

    board = RedirectBoard(client=client)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=board,
        workday_detail_checker=(
            (
                lambda url: PostingCheck(
                    status="unreadable", note="Initial refusal", platform="workday"
                )
            )
            if continuation
            else (lambda url: workday_posting_detail(url, client=client))
        ),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    prior = service.staging.stage_listings(
        workspace_id, [protected], utc_now().date(), reason="Prior read", source_id=source_id
    )
    with durable_database.session() as session:
        row = session.get(PendingDiscovery, prior.row_ids([protected])[0])
        assert row is not None
        session.add(
            PendingDiscoveryDispositionEvent(
                workspace_id=workspace_id,
                pending_discovery_id=row.id,
                action="dismiss",
                category="not_interested",
                reason="Dismissed by the person",
                selection_mode="explicit",
                snapshot_sha256="0" * 64,
                origin="person",
            )
        )
    result = service.scan(workspace_id)
    assert result["scanned"][0]["complete"] is False
    if continuation:
        with durable_database.session() as session:
            frontier = session.scalars(select(SourceScanFrontierItem)).one()
            assert frontier.detail_checkpoint is not None
            target = SourceContinuationTarget(
                url=frontier.url,
                label=frontier.label,
                label_source="visible",
                source_url=root,
                structural_context="main",
                relations=("publisher-content",),
                deferred_reason="workday_detail_pending",
                detail_checkpoint=SourceDetailCheckpoint.from_json(frontier.detail_checkpoint),
            )
        read = board.continue_listings(
            "Globex",
            (target,),
            root_url=root,
            handoff_scopes=(),
            options=SourceReadOptions(protected_posting_urls=frozenset({protected.url})),
        )
        assert not read.complete
        assert not read.listings
        assert {item.url for item in read.deferred_targets} == {listed.url}
    assert requests
    assert protected.url not in requests
    assert workday_posting_detail(listed.url, client=client).status == "readable"
    assert protected.url in requests


def test_cancelling_a_scan_stops_its_lane_before_the_next_detail_read(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    host = "globex.wd1.myworkdayjobs.com"
    _source(durable_database, workspace_id, "Globex", f"https://{host}/Careers")
    _state_titles(durable_database, workspace_id, "Data Product Manager")
    context = _CancellableContext(TaskQueueService(durable_database))
    detail_reads: list[str] = []

    def detail(url: str) -> PostingCheck:
        detail_reads.append(url)
        context.cancelled.set()
        return _readable_detail()

    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Globex": _workday_candidates(host, 3)}),
        workday_detail_checker=detail,
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    with pytest.raises(TaskCancelledError):
        service.scan(workspace_id, context)

    assert len(detail_reads) == 1


def test_cancelling_a_scan_stops_its_lane_before_the_next_body_read(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Globex", "https://careers.globex.example.test")
    _state_titles(durable_database, workspace_id, "data")
    context = _CancellableContext(TaskQueueService(durable_database))
    reads = _RecordingReads(on_read=lambda _url: context.cancelled.set())
    service = _body_reading_scan(
        durable_database,
        {
            "Globex": [
                _listing("Data Platform Lead", f"https://careers.globex.example.test/jobs/{n}")
                for n in range(3)
            ]
        },
        reads,
    )

    with pytest.raises(TaskCancelledError):
        service.scan(workspace_id, context)

    assert len(reads.urls) == 1


def test_a_failure_recording_one_board_stops_the_detail_reads_of_another(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = _workspace(durable_database)
    host = "globex.wd1.myworkdayjobs.com"
    _source(durable_database, workspace_id, "Acme", "https://boards.acme.example.test")
    _source(durable_database, workspace_id, "Globex", f"https://{host}/Careers")
    _state_titles(durable_database, workspace_id, "Data Product Manager")
    shutting_down = threading.Event()

    class ObservedPool(ThreadPoolExecutor):
        def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
            shutting_down.set()
            super().shutdown(wait, cancel_futures=cancel_futures)

    monkeypatch.setattr(
        "clearscout.application.discovery.scan_sweep.sweep.ThreadPoolExecutor", ObservedPool
    )
    detail_reads: list[str] = []
    detail_started = threading.Event()

    def detail(url: str) -> PostingCheck:
        detail_reads.append(url)
        detail_started.set()
        # The sweep stops its lanes before it shuts the pool down.
        shutting_down.wait()
        return _readable_detail()

    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards(
            {
                "Acme": [_listing("Office Manager", "https://boards.acme.example.test/jobs/1")],
                "Globex": _workday_candidates(host, 3),
            }
        ),
        workday_detail_checker=detail,
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    def disk_full(*_args: Any, **_kwargs: Any) -> Any:
        # Fail only once the other lane is inside its first detail read.
        detail_started.wait()
        raise OSError("disk full")

    monkeypatch.setattr(service.staging, "stage_listings", disk_full)

    with pytest.raises(OSError, match="disk full"):
        service.scan(workspace_id)

    assert len(detail_reads) == 1


class _MalformedBoards(_CountingBoards):
    """Raise the non-network errors a changed publisher payload produces."""

    def listings(
        self,
        source_name: str,
        url: str,
        *,
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        if source_name == "Bad Xml":
            raise SyntaxError("not well-formed (invalid token)")
        if source_name == "Bad Shape":
            raise AttributeError("'list' object has no attribute 'get'")
        return super().listings(source_name, url)


def test_a_malformed_board_payload_fails_only_that_source(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    names = ["Bad Xml", "Acme", "Bad Shape", "Globex"]
    for name in names:
        _source(durable_database, workspace_id, name, f"https://boards.example.test/{name}")

    result = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=_MalformedBoards(names),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    assert sorted(item["source"] for item in result["failed"]) == ["Bad Shape", "Bad Xml"]
    assert sorted(item["source"] for item in result["scanned"]) == ["Acme", "Globex"]
    with durable_database.session() as session:
        recorded = session.scalars(select(SourceScan)).all()
    assert sorted(item.status for item in recorded) == [
        "completed",
        "completed",
        "failed",
        "failed",
    ]


class _PublisherNamingBoards:
    """Map one feed record through its board declaration, as a board adapter does."""

    record: ClassVar[dict[str, str]] = {
        "JobDetailsUrl": "https://www.dayforcehcm.com/job/1",
        "Title": "Data Platform Lead",
        "CompanyName": "Globex",
    }

    def listings(
        self,
        employer: str,
        url: str,
        *,
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        """Return the one mapped listing this synthetic Dayforce feed states."""
        del url
        return SourceRead(
            listings=source_listings(
                DAYFORCE_MAPPING,
                [self.record],
                employer=employer,
                employer_is_authoritative=employer_is_authoritative,
            ),
            complete=True,
            raw_count=1,
            outcome=SourceReadOutcome.ATS_COMPLETE,
        )


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        pytest.param("api", "Northwind", id="hand-named"),
        pytest.param("evaluation", "Northwind", id="evaluation-named"),
        pytest.param("search.guided_campaign", "Globex", id="campaign-named"),
    ],
)
def test_only_a_person_answered_board_keeps_its_configured_employer_through_a_sweep(
    durable_database: Database, origin: str, expected: str
) -> None:
    """A payroll or legal entity the feed states may not silently rename a typed board."""
    workspace_id = _workspace(durable_database)
    _source(
        durable_database,
        workspace_id,
        "Northwind",
        "https://www.dayforcehcm.com/northwind",
        origin=origin,
    )
    _state_titles(durable_database, workspace_id, "data")
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=_PublisherNamingBoards(),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    service.scan(workspace_id)

    assert [item.employer for item in service.list_pending_discoveries(workspace_id)] == [expected]


def test_an_opening_the_board_stopped_listing_is_marked_withdrawn(
    durable_database: Database,
) -> None:
    """A confirmed missing opening leaves active triage but remains in closed history."""
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    _state_titles(durable_database, workspace_id, "data")
    both = [
        _listing("Data Platform Lead", "https://boards.greenhouse.io/acme/jobs/1", "Acme"),
        _listing("Data Platform Lead", "https://boards.greenhouse.io/acme/jobs/2", "Acme"),
    ]
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": both}),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.scan(workspace_id)

    pulled = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": [both[0]]}),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    assert [item["title"] for item in pulled["withdrawn"]] == ["Data Platform Lead"]
    queued = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).list_pending_discoveries(workspace_id)
    assert len(queued) == 1
    history = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).list_pending_discoveries(workspace_id, closed_only=True)
    assert [item.canonical_url for item in history] == ["https://boards.greenhouse.io/acme/jobs/2"]
    assert history[0].withdrawn_at is not None
    assert queued[0].withdrawn_at is None
    with durable_database.session() as session:
        event = session.scalar(
            select(AuditEvent).where(AuditEvent.action == "pending_discovery.withdrawn_absent")
        )
        assert event is not None
        assert event.entity_id == history[0].id
        assert {key: event.details[key] for key in ("raw_count", "read_count", "complete")} == {
            "raw_count": 1,
            "read_count": 1,
            "complete": True,
        }


def test_a_verified_active_body_keeps_an_absent_listing_visible(
    durable_database: Database,
) -> None:
    """A withdrawal defers to a verified, still-active body over board absence."""
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    _state_titles(durable_database, workspace_id, "data")
    listing = _listing("Data Platform Lead", "https://boards.greenhouse.io/acme/jobs/1", "Acme")
    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": [listing]}),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    with durable_database.session() as session:
        pending = session.scalar(select(PendingDiscovery))
        assert pending is not None
        pending_id = pending.id
        content = pending_posting_content(
            workspace_id,
            pending.id,
            pending.discovery_url,
            identity_status=PostingIdentityStatus.VERIFIED,
            posting_status=PostingStatus.ACTIVE,
        )
        session.add(content)
        session.flush()
        pending.current_content_id = content.id

    pulled = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": []}),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    assert pulled["withdrawn"] == []
    queued = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).list_pending_discoveries(workspace_id)
    assert [item.withdrawn_at for item in queued] == [None]
    with durable_database.session() as session:
        event = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "pending_discovery.absent_but_verified_active"
            )
        )
        assert event is not None
        assert event.entity_id == pending_id


def _seed_yesterdays_complete_scan(database: Database, source_id: str) -> datetime:
    """Insert a prior day's complete board read to anchor the withdrawal freshness bound."""
    completed_at = utc_now() - timedelta(days=1)
    with database.session() as session:
        session.add(
            SourceScan(
                id=new_id(),
                source_id=source_id,
                scanned_on=completed_at.date(),
                query_label=QUERY_LABEL,
                status="completed",
                read_outcome=SourceReadOutcome.PUBLISHER_INDEX,
                raw_count=0,
                read_count=0,
                result_count=0,
                new_count=0,
                complete=True,
                scan_started_at=completed_at,
                scan_completed_at=completed_at,
            )
        )
    return completed_at


def test_a_verified_active_body_older_than_the_last_complete_read_still_withdraws(
    durable_database: Database,
) -> None:
    """A body read before the source's last full board read does not outvote absence."""
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    _state_titles(durable_database, workspace_id, "data")
    yesterday = _seed_yesterdays_complete_scan(durable_database, source_id)
    listing = _listing("Data Platform Lead", "https://boards.greenhouse.io/acme/jobs/1", "Acme")
    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": [listing]}),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    with durable_database.session() as session:
        pending = session.scalar(select(PendingDiscovery))
        assert pending is not None
        content = pending_posting_content(
            workspace_id,
            pending.id,
            pending.discovery_url,
            identity_status=PostingIdentityStatus.VERIFIED,
            posting_status=PostingStatus.ACTIVE,
            fetched_at=yesterday - timedelta(days=1),
        )
        session.add(content)
        session.flush()
        pending.current_content_id = content.id

    pulled = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": []}),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    assert [item["title"] for item in pulled["withdrawn"]] == ["Data Platform Lead"]
    queued = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).list_pending_discoveries(workspace_id)
    assert queued == []
    with durable_database.session() as session:
        assert (
            session.scalar(
                select(AuditEvent).where(
                    AuditEvent.action == "pending_discovery.absent_but_verified_active"
                )
            )
            is None
        )


def test_two_consecutive_scans_write_one_verified_active_audit_event(
    durable_database: Database,
) -> None:
    """The transition event fires once; a repeated absence with the same body writes no more."""
    workspace_id = _workspace(durable_database)
    source_id = _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    _state_titles(durable_database, workspace_id, "data")
    _seed_yesterdays_complete_scan(durable_database, source_id)
    listing = _listing("Data Platform Lead", "https://boards.greenhouse.io/acme/jobs/1", "Acme")
    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": [listing]}),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    with durable_database.session() as session:
        pending = session.scalar(select(PendingDiscovery))
        assert pending is not None
        content = pending_posting_content(
            workspace_id,
            pending.id,
            pending.discovery_url,
            identity_status=PostingIdentityStatus.VERIFIED,
            posting_status=PostingStatus.ACTIVE,
        )
        session.add(content)
        session.flush()
        pending.current_content_id = content.id

    empty = ScriptedBoards({"Acme": []})
    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=empty,
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)
    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=empty,
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    with durable_database.session() as session:
        events = session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "pending_discovery.absent_but_verified_active"
            )
        ).all()
        assert len(events) == 1


def test_a_partial_board_read_withdraws_nothing(durable_database: Database) -> None:
    """Absence only means withdrawn when the read was the whole board."""
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    _state_titles(durable_database, workspace_id, "data")
    both = [
        _listing("Data Platform Lead", "https://boards.greenhouse.io/acme/jobs/1", "Acme"),
        _listing("Data Platform Lead", "https://boards.greenhouse.io/acme/jobs/2", "Acme"),
    ]
    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": both}),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    partial = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": [both[0]]}, complete=False),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    assert partial["withdrawn"] == []
    queued = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).list_pending_discoveries(workspace_id)
    assert [item.withdrawn_at for item in queued] == [None, None]


def test_a_posting_page_the_board_read_found_closed_is_recorded_closed(
    durable_database: Database,
) -> None:
    """A closed detail page withdraws its row on stored closure evidence, even in a partial read."""
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    _state_titles(durable_database, workspace_id, "data")
    kept, closed = (
        replace(
            _listing("Data Platform Lead", f"https://boards.greenhouse.io/acme/jobs/{job}", "Acme"),
            published_description="Own the data platform.",
        )
        for job in (1, 2)
    )
    resolution = PostingContentResolutionService(
        durable_database, UnexpectedContentResolver(), storage=runtime_storage(durable_database)
    )
    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": [kept, closed]}),
        posting_fetcher=UnexpectedPostingFetcher(),
        content_resolution=resolution,
    ).scan(workspace_id)

    result = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        content_resolution=resolution,
        fetcher=ScriptedBoards(
            {
                "Acme": SourceRead(
                    listings=[kept],
                    complete=False,
                    raw_count=2,
                    outcome=SourceReadOutcome.PUBLISHER_LINKED_PAGES,
                    closed_posting_urls=(closed.url,),
                )
            }
        ),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    assert result["withdrawn_on_read"] == 1

    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    assert [item.canonical_url for item in service.list_pending_discoveries(workspace_id)] == [
        kept.url
    ]
    history = service.list_pending_discoveries(workspace_id, closed_only=True)
    assert [item.canonical_url for item in history] == [closed.url]
    with durable_database.session() as session:
        row = session.get(PendingDiscovery, history[0].id)
        assert row is not None and row.current_content_id is not None
        evidence = session.get(PendingPostingContent, row.current_content_id)
        assert evidence is not None
        assert evidence.posting_status == PostingStatus.CLOSED
        assert reset_protected_ids(session, workspace_id, [row.id]) == {row.id}


def test_an_opening_the_board_lists_again_is_no_longer_withdrawn(
    durable_database: Database,
) -> None:
    """The projection tracks the board, so a reposted opening comes back."""
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    _state_titles(durable_database, workspace_id, "data")
    listing = _listing("Data Platform Lead", "https://boards.greenhouse.io/acme/jobs/1", "Acme")
    for board in (ScriptedBoards({"Acme": [listing]}), ScriptedBoards({"Acme": []})):
        SourceScanService(
            durable_database,
            storage=runtime_storage(durable_database),
            fetcher=board,
            posting_fetcher=UnexpectedPostingFetcher(),
        ).scan(workspace_id)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    assert service.list_pending_discoveries(workspace_id) == []
    assert service.list_pending_discoveries(workspace_id, closed_only=True)[0].withdrawn_at
    context = _CancellableContext(TaskQueueService(durable_database))

    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": [listing]}),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id, context)

    assert service.list_pending_discoveries(workspace_id)[0].withdrawn_at is None
    with durable_database.session() as session:
        event = session.scalar(
            select(AuditEvent).where(AuditEvent.action == "pending_discovery.relisted")
        )
        assert event is not None
        assert event.correlation_id == context.task_id


def test_a_board_only_withdraws_the_openings_it_staged_itself(
    durable_database: Database,
) -> None:
    """A complete read speaks for the source that made it and for no other.

    A provider web search stages an opening under the address it found, which is
    not the address the board lists it at. Judging that row against the board's
    addresses withdraws an opening the very same read still carries.
    """
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).stage_listings(
        workspace_id,
        [_listing("Data Platform Lead", "https://acme.example/careers/1", "Acme")],
        date(2026, 8, 16),
        reason="Provider-hosted web search returned this public posting URL.",
    )
    board = [_listing("Data Platform Lead", "https://boards.greenhouse.io/acme/jobs/1", "Acme")]

    result = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": board}),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    assert result["withdrawn"] == []
    with durable_database.session() as session:
        pulled = session.scalars(
            select(PendingDiscovery.canonical_url).where(PendingDiscovery.withdrawn_at.is_not(None))
        ).all()
    assert list(pulled) == []


def test_a_source_can_name_an_aggregator_rather_than_one_employer(
    durable_database: Database,
) -> None:
    """An aggregator lists many employers, so its source names none of them.

    Each staged row keeps the employer its own posting states rather than the
    name of the site the posting was read from.
    """
    workspace_id = _workspace(durable_database)
    fetcher = ScriptedPage(
        declaring_page(
            {
                "@type": "JobPosting",
                "title": "Platform Engineer",
                "url": "https://digest.example/jobs/1",
                "hiringOrganization": {"name": "Acme"},
            },
            {
                "@type": "JobPosting",
                "title": "Data Platform Lead",
                "url": "https://digest.example/jobs/2",
                "hiringOrganization": {"name": "Globex"},
            },
        )
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=fetcher,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.add_source(
        workspace_id,
        "Openings Digest",
        "https://digest.example/jobs",
        source_type=SourceKind.AGGREGATOR,
        origin="manual",
    )

    result = service.scan(workspace_id)

    assert [item["source"] for item in result["scanned"]] == ["Openings Digest"]
    queued = service.list_pending_discoveries(workspace_id)
    assert sorted(item.employer for item in queued) == ["Acme", "Globex"]


@pytest.mark.parametrize(
    ("completed_kind", "listing_count"),
    [("completed", 1), ("completed", 501), ("failed", 1), ("continuing", 1), ("omitted", 1)],
)
@pytest.mark.parametrize("unfinished", [False, True])
def test_a_reclaimed_scan_keeps_released_results_and_reads_only_unfinished_sources(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    completed_kind: str,
    listing_count: int,
    unfinished: bool,
) -> None:
    from clearscout.infrastructure.persistence.models import BackgroundTask, SourceScanMember

    monkeypatch.setattr(
        "clearscout.bootstrap.local_runtime.local_process_started_at", lambda _pid: "reader-start"
    )
    workspace_id, acme_id, globex_id = _two_board_workspace(durable_database)
    if completed_kind == "omitted":
        with durable_database.session() as session:
            source = session.get(Source, acme_id)
            assert source is not None
            source.url = ""
    stale_id = _seed_pending(
        durable_database,
        workspace_id,
        employer="Acme",
        created_at=utc_now() - timedelta(days=1),
        source_id=acme_id,
        title="Old Data Platform Lead",
    )
    acme_url = "https://boards.greenhouse.io/acme/jobs/10"
    globex_url = "https://boards.greenhouse.io/globex/jobs/2"
    target = SourceContinuationTarget(
        url="https://boards.greenhouse.io/acme/jobs/next",
        label="Data Platform Lead",
        label_source="visible",
        source_url="https://boards.greenhouse.io/acme",
        structural_context="main",
        relations=("publisher-content", "substantive-label"),
    )

    class RecoverableBoards:
        def __init__(self, boards: dict[str, list[SourceListing] | SourceRead]) -> None:
            self.scripted = ScriptedBoards(boards)
            self.requested = self.scripted.requested

        def listings(
            self,
            employer: str,
            url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            if employer == "Acme" and completed_kind == "failed":
                self.requested.append(employer)
                raise httpx.ConnectError("publisher could not be resolved")
            return self.scripted.listings(
                employer, url, employer_is_authoritative=employer_is_authoritative
            )

        def continue_listings(
            self, employer: str, targets: tuple[SourceContinuationTarget, ...], **kwargs: Any
        ) -> SourceRead:
            raise AssertionError("Root recovery must leave continuation work to its child.")

    board = RecoverableBoards(
        {
            "Acme": SourceRead(
                listings=[
                    _listing("Data Platform Lead", f"{acme_url}{index}")
                    for index in range(listing_count)
                ],
                raw_count=listing_count,
                complete=completed_kind != "continuing",
                outcome=(
                    SourceReadOutcome.CRAWL_CONTINUATION
                    if completed_kind == "continuing"
                    else SourceReadOutcome.ATS_COMPLETE
                ),
                deferred_targets=(target,) if completed_kind == "continuing" else (),
            ),
            "Globex": [_listing("Data Platform Lead", globex_url)],
        }
    )
    handler = SourceScanHandler(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=board,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [acme_id, globex_id] if unfinished else [acme_id]},
        idempotency_key="recover-released-source",
    )
    worker = Worker(
        durable_database,
        worker_id="recover-released-source",
        handlers={"source.scan": handler},
        handler_time_limits={},
    )
    commands = handler.service.sweep.batch_commands
    release_source_read = commands.release_source_read
    read_boards = handler.service.sweep._read

    def read_serially(
        boards: list[scan_sweep.SweepBoard],
        context: Any,
        inputs: ScanInputs,
    ) -> Iterator[scan_sweep.BoardReadOutcome]:
        for selected in boards:
            yield from read_boards([selected], context, inputs)

    def crash_after_release(*args: Any, **kwargs: Any) -> None:
        if kwargs.get("result") is None:
            return
        release_source_read(*args, **kwargs)
        raise SystemExit("reader died after releasing its source")

    with monkeypatch.context() as crash:
        crash.setattr(handler.service.sweep, "_read", read_serially)
        crash.setattr(commands, "release_source_read", crash_after_release)
        crash.setattr(commands, "finish_source_batch", lambda *args, **kwargs: None)
        with pytest.raises(SystemExit, match="after releasing"):
            worker.run_once()
    with durable_database.session() as session:
        member = session.scalars(
            select(SourceScanMember).where(SourceScanMember.source_id == acme_id)
        ).one()
        assert member.active_task_id is None
        assert member.state == ("completed" if completed_kind == "omitted" else completed_kind)
        stored_result = member.result
        # Stored members outlive a code change; resume merges them under these keys.
        assert {"_sources_read", "_network_unreachable"} <= stored_result["board_result"].keys()
        assert stored_result["board_result"]["new_discoveries"] == listing_count * int(
            completed_kind in {"completed", "continuing"}
        )
        scans = list(session.scalars(select(SourceScan.id)))
        pending = list(session.scalars(select(PendingDiscovery.id)))
        outcomes = list(session.scalars(select(SourceScanOutcome.id)))
        assert len(scans) == 1
        root = session.get(BackgroundTask, str(task.id))
        assert root is not None and root.status == "running"
        root.lease_expires_at = utc_now() - timedelta(seconds=1)
        children = [
            (child.id, child.status, child.attempt_count)
            for child in session.scalars(
                select(BackgroundTask).where(BackgroundTask.task_type == "source.continue")
            )
        ]
        if unfinished:
            held = session.scalars(
                select(SourceScanMember).where(SourceScanMember.source_id == globex_id)
            ).one()
            assert held.active_task_id == str(task.id)
            assert held.active_process_started_at == "reader-start"
    queue.commands.recover_dead_source_readers(lambda _pid, _start: False)
    assert worker.run_once()
    recovered = queue.get(str(task.id))
    assert recovered.attempt_count == 2
    assert recovered.status == (
        "failed" if completed_kind == "failed" and not unfinished else "completed"
    )
    expected_new = listing_count * int(completed_kind in {"completed", "continuing"}) + int(
        unfinished
    )
    if recovered.status == "completed":
        assert recovered.progress["new_discoveries"] == expected_new
        assert recovered.progress["already_in_progress"] == []
        assert recovered.progress["sources_considered"] == 1 + int(unfinished)
        assert len(recovered.progress["discoveries"]) == min(100, expected_new)
        assert len(recovered.progress["failed"]) == int(completed_kind == "failed")
        assert len(recovered.progress["scanned"]) == int(completed_kind != "failed") + int(
            unfinished
        )
        assert len(recovered.progress["withdrawn"]) == int(completed_kind == "completed")
        assert recovered.progress["funnel"]["total_outcomes"] == (
            (listing_count + 1 if completed_kind in {"completed", "continuing"} else 1)
            + 2 * int(unfinished)
        )
        funnel = recovered.progress["funnel"]
        assert len(funnel["outcomes"]) == min(500, funnel["total_outcomes"])
        assert funnel["omitted_outcomes"] == max(0, funnel["total_outcomes"] - 500)
        assert funnel["counts"]["proposed"] == funnel["total_outcomes"]
        assert sum(funnel["reason_counts"].values()) == funnel["total_outcomes"]
    assert board.requested == (
        ([] if completed_kind == "omitted" else ["Acme"]) + (["Globex"] if unfinished else [])
    )
    with durable_database.session() as session:
        assert set(scans) <= set(session.scalars(select(SourceScan.id)))
        assert set(pending) <= set(session.scalars(select(PendingDiscovery.id)))
        assert set(outcomes) <= set(session.scalars(select(SourceScanOutcome.id)))
        assert len(list(session.scalars(select(SourceScan)))) == 1 + int(unfinished)
        assert len(list(session.scalars(select(PendingDiscovery)))) == 1 + expected_new
        assert len(list(session.scalars(select(SourceScanOutcome)))) == len(outcomes) + 2 * int(
            unfinished
        )
        saved = session.get(SourceScanMember, member.id)
        assert saved is not None and saved.result == stored_result
        old = session.get(PendingDiscovery, stale_id)
        assert old is not None
        assert (old.withdrawn_at is not None) == (completed_kind == "completed")
        assert [
            (child.id, child.status, child.attempt_count)
            for child in session.scalars(
                select(BackgroundTask).where(BackgroundTask.task_type == "source.continue")
            )
        ] == children


def test_scan_runs_through_the_worker_with_progress(durable_database: Database) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    _state_titles(durable_database, workspace_id, "data")
    boards = ScriptedBoards(
        {"Acme": [_listing("Data Platform Lead", "https://boards.greenhouse.io/acme/jobs/1")]}
    )
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={},
        idempotency_key="scan-1",
    )
    worker = Worker(
        durable_database,
        worker_id="scan-worker",
        handlers=default_handlers(durable_database, fetcher=boards),
    )
    assert worker.run_once()
    finished = queue.get(str(task.id))
    assert finished.status == "completed", (finished.error_category, finished.error_message)
    assert finished.progress["new_discoveries"] == 1
    assert finished.progress["discoveries"][0]["employer"] == "Example"
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert scan.correlation_id == str(task.id)
        outcome = session.scalars(
            select(SourceScanOutcome).where(SourceScanOutcome.candidate_kind == "board_listing")
        ).one()
        assert outcome.task_id == str(task.id)
        assert outcome.source_scan_id == scan.id
        assert outcome.employer == "Example"
        assert outcome.title == "Data Platform Lead"
        assert outcome.reason == "staged"
        assert outcome.terminal_stage == "staged"


def test_an_unsupported_page_remains_an_observable_incomplete_scan(
    durable_database: Database,
) -> None:
    """An enabled source the scanner cannot read still leaves a scan row.

    The history is the only durable record, so a source passed over without a
    row would show no failure, no skip, and no zero.
    """
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://www.linkedin.com/jobs")
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards(
            {
                "Acme": SourceRead(
                    listings=[],
                    complete=False,
                    raw_count=0,
                    outcome=SourceReadOutcome.NO_SUPPORTED_REPRESENTATION,
                    note="The publisher page exposed no supported representation.",
                )
            }
        ),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    result = service.scan(workspace_id)

    assert "manual" not in result
    assert [(item["source"], item["outcome"]) for item in result["scanned"]] == [
        ("Acme", "no_supported_representation")
    ]
    with durable_database.session() as session:
        recorded = session.scalars(select(SourceScan)).all()
    assert [(item.status, item.read_outcome, item.result_count) for item in recorded] == [
        ("incomplete", SourceReadOutcome.NO_SUPPORTED_REPRESENTATION, 0)
    ]
    assert "no supported representation" in recorded[0].notes


def test_a_scheduled_policy_refusal_remains_durable_and_never_looks_empty(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(
        durable_database,
        workspace_id,
        "Acme Health",
        "https://jobs.dayforcehcm.com/en-US/acme/ACMECANDIDATEPORTAL",
    )

    class RefusedBoards:
        def listings(
            self,
            _source_name: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            raise RobotsDeniedError("https://www.dayforcehcm.com/api/acme/v1/jobfeeds")

    result = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=RefusedBoards(),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    assert result["new_discoveries"] == 0
    with durable_database.session() as session:
        source = session.get(Source, source_id)
        assert source is not None
        assert source.enabled is True
        scan = session.scalars(select(SourceScan)).one()
        assert scan.status == "failed"
        assert scan.read_outcome == SourceReadOutcome.POLICY_REFUSED.value
        assert scan.raw_count == 0
        assert scan.result_count == 0
        assert scan.complete is None


@pytest.mark.parametrize("failure_type", [InvalidFetchUrlError, RobotsUnavailableError])
def test_a_policy_infrastructure_failure_remains_retryable_not_policy_refused(
    durable_database: Database,
    failure_type: type[Exception],
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme Health", "https://careers.example.test")

    class UnavailableBoards:
        def listings(
            self,
            _source_name: str,
            _url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            raise failure_type("temporary policy infrastructure failure")

    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=UnavailableBoards(),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
    assert scan.status == "failed"
    assert scan.read_outcome == SourceReadOutcome.FETCH_FAILED.value


def _access_blocked(url: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", url)
    return httpx.HTTPStatusError(
        "forbidden", request=request, response=httpx.Response(403, request=request)
    )


@pytest.mark.parametrize(
    ("raiser", "expected_reason"),
    [
        (RobotsDeniedError, "fetch_refused"),
        (_access_blocked, "fetch_refused"),
        (lambda url: InvalidFetchUrlError("not a fetchable url"), "fetch_failed"),
    ],
)
def test_a_failed_read_maps_standing_refusals_apart_from_fetch_failures(
    durable_database: Database,
    raiser: Callable[[str], Exception],
    expected_reason: str,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme Health", "https://careers.example.test")

    class RaisingBoards:
        def listings(
            self,
            _source_name: str,
            url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            raise raiser(url)

    result = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=RaisingBoards(),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    board_outcomes = [
        outcome for outcome in result["funnel"]["outcomes"] if outcome["kind"] == "board_lead"
    ]
    assert [outcome["reason"] for outcome in board_outcomes] == [expected_reason]


def _unresolvable(url: str) -> DestinationUnresolvableError:
    try:
        raise OSError(8, f"nodename nor servname provided for {url}")
    except OSError as exc:
        error = DestinationUnresolvableError(
            "The destination could not be resolved to a public network address."
        )
        error.__cause__ = exc
        return error


def test_a_sweep_whose_every_read_failed_on_the_network_fails_the_task(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme Health", "https://careers.example.test")
    _source(durable_database, workspace_id, "Globex", "https://jobs.lever.co/globex")

    class OfflineBoards:
        def listings(
            self,
            source_name: str,
            url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            if source_name == "Globex":
                raise httpx.ConnectError("connection refused")
            raise _unresolvable(url)

    with pytest.raises(UnavailableError, match="no source could be reached"):
        SourceScanService(
            durable_database,
            storage=runtime_storage(durable_database),
            fetcher=OfflineBoards(),
            posting_fetcher=UnexpectedPostingFetcher(),
        ).scan(workspace_id)

    with durable_database.session() as session:
        statuses = session.scalars(select(SourceScan.status)).all()
    assert statuses == ["failed", "failed"]


def test_a_sweep_that_reached_any_publisher_still_completes(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme Health", "https://careers.example.test")
    _source(durable_database, workspace_id, "Globex", "https://jobs.lever.co/globex")

    class HalfOfflineBoards:
        def listings(
            self,
            source_name: str,
            url: str,
            *,
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            if source_name == "Globex":
                request = httpx.Request("GET", url)
                raise httpx.HTTPStatusError(
                    "not found", request=request, response=httpx.Response(404, request=request)
                )
            raise _unresolvable(url)

    result = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=HalfOfflineBoards(),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    assert [item["source"] for item in result["failed"]] == ["Acme Health", "Globex"]


def test_a_board_scan_refreshes_a_tracked_requisition_instead_of_staging_a_duplicate(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = _workspace(durable_database)
    with durable_database.session() as session:
        employer = Employer(
            workspace_id=workspace_id,
            name="Globex",
            normalized_name="globex",
        )
        session.add(employer)
        session.flush()
        job = JobPosting(
            workspace_id=workspace_id,
            employer_id=employer.id,
            title="Platform Lead",
            normalized_title="platform lead",
            first_seen_at=utc_now(),
        )
        session.add(job)
        session.flush()
        session.add(
            PostingIdentifier(
                workspace_id=workspace_id,
                job_posting_id=job.id,
                identifier_type="requisition",
                identifier_value="globex|req 42",
            )
        )
        session.add(
            PostingUrl(
                job_posting_id=job.id,
                url_type="canonical",
                url="https://jobs.example.test/old-platform-lead",
                normalized_url="https://jobs.example.test/old-platform-lead",
                is_primary=True,
            )
        )

    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    refreshed: list[tuple[str, SourceListing]] = []
    monkeypatch.setattr(
        service.staging,
        "refresh_tracked",
        lambda _workspace_id, items, _context=None, *, board_url="", require_source_read=None: (
            refreshed.extend(items)
        ),
    )

    staged = service.stage_listings(
        workspace_id,
        [
            SourceListing(
                "Globex",
                "Platform Lead",
                "https://jobs.example.test/new-platform-lead",
                "Remote",
                "REQ-42",
            )
        ],
        date(2026, 8, 25),
        reason="Board scan.",
    )

    assert staged.queued == []
    assert refreshed == [
        (
            job.id,
            SourceListing(
                "Globex",
                "Platform Lead",
                "https://jobs.example.test/new-platform-lead",
                "Remote",
                "REQ-42",
            ),
        )
    ]
    with durable_database.session() as session:
        assert session.scalars(select(PendingDiscovery)).all() == []
        urls = {
            row.normalized_url: row
            for row in session.scalars(
                select(PostingUrl).where(PostingUrl.job_posting_id == job.id)
            )
        }
        assert urls["https://jobs.example.test/new-platform-lead"].is_primary is True
        assert urls["https://jobs.example.test/old-platform-lead"].is_primary is False
        assert (
            session.scalar(
                select(PostingIdentifier.id).where(
                    PostingIdentifier.workspace_id == workspace_id,
                    PostingIdentifier.identifier_type == "url",
                    PostingIdentifier.identifier_value
                    == "https://jobs.example.test/new-platform-lead",
                )
            )
            is not None
        )


def test_a_board_scan_stages_a_requisition_matching_an_archived_job(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = _workspace(durable_database)
    with durable_database.session() as session:
        employer = Employer(
            workspace_id=workspace_id,
            name="Globex",
            normalized_name="globex",
        )
        session.add(employer)
        session.flush()
        job = JobPosting(
            workspace_id=workspace_id,
            employer_id=employer.id,
            title="Archived Platform Lead",
            normalized_title="archived platform lead",
            first_seen_at=utc_now(),
            archived_at=utc_now(),
        )
        session.add(job)
        session.flush()
        session.add(
            PostingIdentifier(
                workspace_id=workspace_id,
                job_posting_id=job.id,
                identifier_type="requisition",
                identifier_value="globex|req 43",
            )
        )

    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    refreshed: list[tuple[str, SourceListing]] = []
    monkeypatch.setattr(
        service.staging,
        "refresh_tracked",
        lambda _workspace_id, items, _context=None, *, board_url="", require_source_read=None: (
            refreshed.extend(items)
        ),
    )
    listing = SourceListing(
        "Globex",
        "Reopened Platform Lead",
        "https://jobs.example.test/reopened-platform-lead",
        "Remote",
        "REQ-43",
    )

    staged = service.stage_listings(
        workspace_id,
        [listing],
        date(2026, 8, 25),
        reason="Board scan.",
    )

    assert staged.inserted == [listing]
    assert refreshed == []
    with durable_database.session() as session:
        assert session.scalar(select(PendingDiscovery.discovery_url)) == listing.url


def test_a_consumed_reset_marker_does_not_requeue_a_later_evaluation_archive(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    url = "https://jobs.example.test/evaluated-opening"
    staged = service.stage_found_posting(workspace_id, url, utc_now().date())
    assert staged is not None
    mark_reset_archived(durable_database, workspace_id, staged.pending_discovery.id)
    assert service.stage_found_posting(workspace_id, url, utc_now().date()) is not None
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, staged.pending_discovery.id)
        assert pending is not None
        reset_event = session.scalar(
            select(AuditEvent).where(
                AuditEvent.entity_id == pending.id,
                AuditEvent.action == "pending_discovery.queue_reset",
            )
        )
        assert reset_event is not None
        pending.archived_at = reset_event.created_at

    assert service.stage_found_posting(workspace_id, url, utc_now().date()) is None
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, staged.pending_discovery.id)
        assert pending is not None
        assert pending.archived_at is not None


def test_a_paid_task_cannot_be_enqueued_for_a_reset_archived_pending_row(
    durable_database: Database,
) -> None:
    from clearscout.application.tasks.payloads import DiscoveryEvaluateBatchPayload

    workspace_id = _workspace(durable_database)
    source_id = _source(
        durable_database,
        workspace_id,
        "Globex",
        "https://boards.greenhouse.io/globex",
    )
    pending_id = _seed_pending(
        durable_database,
        workspace_id,
        employer="Globex",
        created_at=utc_now(),
        source_id=source_id,
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    assert service.reset_pending_discoveries(workspace_id).archived_count == 1

    with pytest.raises(NotFoundError, match="no longer exists in the active queue"):
        TaskQueueService(durable_database).enqueue(
            workspace_id=workspace_id,
            task_type="discovery.evaluate_batch",
            payload=DiscoveryEvaluateBatchPayload(pending_discovery_ids=[pending_id]).model_dump(),
            idempotency_key="archived-reset-target",
        )


def test_strong_matches_precede_an_unbounded_role_family_review_lane(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    target = "Senior Clinical Data Product Manager"
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(seniority=Seniority(target_titles=titles(target))),
    )
    family = [
        _listing(
            f"Product Manager Operations {index}",
            f"https://boards.greenhouse.io/acme/jobs/family-{index}",
        )
        for index in range(40)
    ]
    exact = [
        _listing(target, f"https://boards.greenhouse.io/acme/jobs/exact-{index}")
        for index in range(3)
    ]

    result = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": family + exact}),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    assert result["new_discoveries"] == 43
    assert [item["title"] for item in result["discoveries"][:3]] == [target] * 3
    assert (
        sum(
            outcome["reason"] == "role_family_review_limit_reached"
            for outcome in result["funnel"]["outcomes"]
        )
        == 0
    )
