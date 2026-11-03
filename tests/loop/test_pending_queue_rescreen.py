"""Re-screening the active pending queue from its already-stored publisher facts."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import select

from clearscout.application.discovery.board_scan import (
    PendingQueueRescreenHandler,
    SourceScanService,
)
from clearscout.application.discovery.content_resolution import (
    CurrentPostingContent,
    CurrentPostingContentReader,
    PostingContentResolutionService,
    ResolvedPostingContent,
)
from clearscout.application.discovery.refusal_patterns import RefusedPosting, file_refusal
from clearscout.application.failures import UnavailableError
from clearscout.application.tasks import TaskQueueService
from clearscout.application.workspaces import SearchParametersService
from clearscout.bootstrap.worker import TaskCancelledError, Worker, default_handlers
from clearscout.domain import (
    PostingContentAccessStatus,
    PostingContentExtractionMethod,
    PostingIdentityStatus,
    PostingStatus,
    TaskStatus,
    new_id,
    utc_now,
)
from clearscout.domain.artifacts import PostingSource
from clearscout.domain.search_parameters import (
    Acceptance,
    Arrangement,
    CountryNames,
    JobSearchParameters,
    Location,
    Seniority,
)
from clearscout.domain.staged_facts import PublishedFact, StagedListingFacts
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    BackgroundTask,
    PendingDiscovery,
    PendingDiscoveryDispositionEvent,
)
from clearscout.infrastructure.web_fetch.listing_facts import published_listing_facts
from clearscout.infrastructure.web_fetch.posting_resolution import (
    DeterministicPostingContentResolver,
)
from tests.support.artifacts import runtime_storage
from tests.support.doubles import UnexpectedPostingFetcher
from tests.support.scenarios import (
    canadian_pay_with_a_us_place,
    dismiss_pending_discovery,
    scan_workspace,
    titles,
)

LONDON = published_listing_facts("greenhouse", locations="London, United Kingdom")


def _us_search(database: Database, workspace_id: str) -> None:
    SearchParametersService(database).save(
        workspace_id,
        JobSearchParameters(
            seniority=Seniority(target_titles=titles("Data Platform Lead")),
            locations=[Location(key="us", country="United States")],
        ),
    )


def _hybrid_holdout_parameters() -> JobSearchParameters:
    """Both saved remote-only markets refuse a precise hybrid arrangement."""
    refused = Arrangement(
        remote=Acceptance.PREFERRED,
        hybrid=Acceptance.REFUSED,
        on_site=Acceptance.REFUSED,
        maximum_office_days=0,
    )
    return JobSearchParameters(
        seniority=Seniority(target_titles=titles("Data Engineer")),
        locations=[
            Location(key="canada_remote", country="Canada", arrangement=refused),
            Location(key="us_remote", country="United States", arrangement=refused),
        ],
        countries=[CountryNames(country="Canada"), CountryNames(country="United States")],
        arrangement=Arrangement(
            remote=Acceptance.PREFERRED,
            hybrid=Acceptance.REFUSED,
            on_site=Acceptance.REFUSED,
            maximum_travel_percent=10,
            relocation=False,
        ),
    )


def _hybrid_facts(platform: str, *locations: str) -> StagedListingFacts:
    return StagedListingFacts(
        locations=PublishedFact(values=locations, provenance=f"{platform}.location", complete=True),
        arrangements=PublishedFact(
            values=("hybrid",), provenance=f"{platform}.arrangement", complete=True
        ),
    )


def _pending_row(
    database: Database,
    workspace_id: str,
    *,
    fingerprint: str,
    location: str,
    facts: StagedListingFacts,
    employer: str = "Acme",
    title: str = "Data Platform Lead",
    url: str = "",
    archived: bool = False,
) -> str:
    with database.session() as session:
        row = PendingDiscovery(
            workspace_id=workspace_id,
            fingerprint=fingerprint,
            employer=employer,
            title=title,
            location=location,
            staged_facts=facts.as_json(),
            discovery_url=url,
            canonical_url=url,
            reason="Staged for a rescreen test.",
            first_seen_on=date(2026, 8, 1),
            archived_at=utc_now() if archived else None,
        )
        session.add(row)
        session.flush()
        return row.id


def _london_row(database: Database, workspace_id: str, fingerprint: str) -> str:
    return _pending_row(
        database,
        workspace_id,
        fingerprint=fingerprint,
        location="London, United Kingdom",
        facts=LONDON,
    )


def _events(database: Database, pending_id: str) -> list[PendingDiscoveryDispositionEvent]:
    with database.session() as session:
        return list(
            session.scalars(
                select(PendingDiscoveryDispositionEvent)
                .where(PendingDiscoveryDispositionEvent.pending_discovery_id == pending_id)
                .order_by(PendingDiscoveryDispositionEvent.created_at)
            )
        )


def _store_body(
    database: Database,
    workspace_id: str,
    pending_id: str,
    url: str,
    body: bytes,
    *,
    origin: str = "test",
    source: PostingSource | None = None,
) -> None:
    PostingContentResolutionService(
        database,
        DeterministicPostingContentResolver(),
        storage=runtime_storage(database),
    ).store_prefetched_result(
        workspace_id,
        pending_id,
        ResolvedPostingContent(
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
            source=source,
        ),
        None,
        origin=origin,
    )


def _enqueue(database: Database, workspace_id: str) -> str:
    return (
        TaskQueueService(database)
        .enqueue(
            workspace_id=workspace_id,
            task_type="discovery.rescreen_queue",
            payload={},
            idempotency_key=new_id(),
        )
        .id
    )


def _run_through_worker(database: Database, workspace_id: str) -> dict[str, Any]:
    """Run the registered re-screen task the way the worker does and return its result."""
    task_id = _enqueue(database, workspace_id)
    worker = Worker(
        database,
        worker_id="rescreen-worker",
        handlers=default_handlers(database, artifact_root=runtime_storage(database)),
    )
    assert worker.run_once()
    task = TaskQueueService(database).get(task_id)
    assert task.status is TaskStatus.COMPLETED, task.error_message
    return task.progress


@dataclass
class _Context:
    queue: TaskQueueService
    task_id: str
    worker_id: str = "rescreen-worker"
    cancel_after_checks: int | None = None
    on_progress: Callable[[dict[str, Any]], None] | None = None
    stages: list[str] = field(default_factory=list)
    checks: int = 0

    def progress(self, stage: str, payload: dict[str, Any]) -> None:
        self.stages.append(stage)
        if self.on_progress is not None:
            self.on_progress(payload)

    def cancellation_requested(self) -> bool:
        return self.cancel_after_checks is not None and self.checks >= self.cancel_after_checks

    def raise_if_cancelled(self) -> None:
        if self.cancellation_requested():
            raise TaskCancelledError
        self.checks += 1


def _handler(database: Database) -> PendingQueueRescreenHandler:
    return PendingQueueRescreenHandler(
        SourceScanService(
            database,
            storage=runtime_storage(database),
            posting_fetcher=UnexpectedPostingFetcher(),
        )
    )


def _context(database: Database, workspace_id: str, **kwargs: Any) -> _Context:
    return _Context(TaskQueueService(database), _enqueue(database, workspace_id), **kwargs)


def test_the_task_holds_out_a_row_the_current_screening_now_refuses(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database, slug="rescreen")
    _us_search(durable_database, workspace_id)
    pending_id = _london_row(durable_database, workspace_id, "a" * 63 + "1")

    result = _run_through_worker(durable_database, workspace_id)

    assert (result["screened"], result["held_out"], result["held_out_ids"]) == (1, 1, [pending_id])
    [event] = _events(durable_database, pending_id)
    assert (event.action, event.category) == ("dismiss", "location_outside")
    assert event.origin == "discovery.rescreen_queue.structured_holdout"
    assert "restorable" in event.reason


def test_the_task_leaves_a_row_the_current_screening_still_accepts(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database, slug="rescreen")
    _us_search(durable_database, workspace_id)
    pending_id = _pending_row(
        durable_database,
        workspace_id,
        fingerprint="a" * 63 + "2",
        location="New York, NY, United States",
        facts=published_listing_facts("greenhouse", locations="New York, NY, United States"),
    )

    result = _run_through_worker(durable_database, workspace_id)

    assert (result["screened"], result["held_out"], result["unchanged"]) == (1, 0, 1)
    assert _events(durable_database, pending_id) == []


def test_the_task_leaves_a_row_a_paid_task_is_running_against(
    durable_database: Database,
) -> None:
    """A review running on a row must not lose its subject to an automatic hold-out."""
    workspace_id = scan_workspace(durable_database, slug="rescreen")
    _us_search(durable_database, workspace_id)
    pending_id = _london_row(durable_database, workspace_id, "a" * 63 + "9")
    with durable_database.session() as session:
        session.add(
            BackgroundTask(
                workspace_id=workspace_id,
                task_type="discovery.relevance_review",
                status=TaskStatus.RUNNING.value,
                payload={"pending_discovery_ids": [pending_id]},
                payload_sha256="c" * 64,
                idempotency_key=new_id(),
            )
        )

    result = _handler(durable_database)(_context(durable_database, workspace_id), {})

    assert (result["screened"], result["held_out"]) == (0, 0)
    assert (result["skipped_for_paid_task"], result["skipped_ids"]) == (1, [pending_id])
    assert _events(durable_database, pending_id) == []


def test_the_task_never_touches_or_restores_an_already_dismissed_row(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database, slug="rescreen")
    _us_search(durable_database, workspace_id)
    pending_id = _london_row(durable_database, workspace_id, "a" * 63 + "3")
    dismiss_pending_discovery(durable_database, workspace_id, pending_id)

    result = _run_through_worker(durable_database, workspace_id)

    assert (result["screened"], result["held_out"]) == (0, 0)
    [event] = _events(durable_database, pending_id)
    assert (event.action, event.category) == ("dismiss", "human_review")


def test_the_task_leaves_a_row_a_person_restored_restored(durable_database: Database) -> None:
    """A person's restore of an automatic hold-out outlasts every later re-screen."""
    workspace_id = scan_workspace(durable_database, slug="rescreen")
    _us_search(durable_database, workspace_id)
    pending_id = _london_row(durable_database, workspace_id, "e" * 64)
    with durable_database.session() as session:
        holdout = PendingDiscoveryDispositionEvent(
            id=new_id(),
            workspace_id=workspace_id,
            pending_discovery_id=pending_id,
            action="dismiss",
            category="location_outside",
            reason="Verified board facts meet a structured search hold-out.",
            selection_mode="rule",
            snapshot_sha256="holdout",
            created_by="worker",
            origin="source.scan.structured_holdout",
        )
        session.add(holdout)
        session.flush()
        session.add(
            PendingDiscoveryDispositionEvent(
                id=new_id(),
                workspace_id=workspace_id,
                pending_discovery_id=pending_id,
                action="restore",
                restore_provenance="person",
                category="",
                reason="I would relocate for this one.",
                selection_mode="explicit",
                snapshot_sha256="restore",
                supersedes_event_id=holdout.id,
                created_by="local_user",
                origin="api",
            )
        )

    result = _run_through_worker(durable_database, workspace_id)

    assert (result["held_out"], result["unchanged"]) == (0, 1)
    assert sorted(event.action for event in _events(durable_database, pending_id)) == [
        "dismiss",
        "restore",
    ]


def test_the_task_never_touches_an_archived_row(durable_database: Database) -> None:
    workspace_id = scan_workspace(durable_database, slug="rescreen")
    _us_search(durable_database, workspace_id)
    pending_id = _pending_row(
        durable_database,
        workspace_id,
        fingerprint="a" * 63 + "4",
        location="London, United Kingdom",
        facts=LONDON,
        archived=True,
    )

    result = _run_through_worker(durable_database, workspace_id)

    assert (result["screened"], result["held_out"]) == (0, 0)
    assert _events(durable_database, pending_id) == []


def test_the_task_never_holds_out_on_an_empty_retained_body(durable_database: Database) -> None:
    """A row with no retained content has no body, so the arrangement rule cannot refuse it."""
    workspace_id = scan_workspace(durable_database, slug="rescreen")
    SearchParametersService(durable_database).save(workspace_id, _hybrid_holdout_parameters())
    pending_id = _pending_row(
        durable_database,
        workspace_id,
        fingerprint="a" * 63 + "5",
        location="New York, NY",
        facts=_hybrid_facts("lever", "New York, NY"),
        title="Data Engineer",
    )

    result = _run_through_worker(durable_database, workspace_id)

    assert (result["screened"], result["held_out"]) == (1, 0)
    assert _events(durable_database, pending_id) == []


def test_the_task_holds_out_an_arrangement_refusal_when_the_body_has_no_remote_offer(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database, slug="rescreen")
    SearchParametersService(durable_database).save(workspace_id, _hybrid_holdout_parameters())
    url = "https://boards.lever.co/acme/jobs/hybrid-nyc"
    pending_id = _pending_row(
        durable_database,
        workspace_id,
        fingerprint="a" * 63 + "6",
        location="New York, NY",
        facts=_hybrid_facts("lever", "New York, NY"),
        title="Data Engineer",
        url=url,
    )
    _store_body(
        durable_database,
        workspace_id,
        pending_id,
        url,
        b"This role is fully on-site, no remote option.",
    )

    result = _run_through_worker(durable_database, workspace_id)

    assert (result["screened"], result["held_out"]) == (1, 1)
    [event] = _events(durable_database, pending_id)
    assert event.category == "arrangement_refused"


def test_the_task_a_remote_offer_in_the_retained_body_blocks_the_arrangement_holdout(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database, slug="rescreen")
    SearchParametersService(durable_database).save(workspace_id, _hybrid_holdout_parameters())
    url = "https://boards.lever.co/acme/jobs/hybrid-nyc-remote-offer"
    pending_id = _pending_row(
        durable_database,
        workspace_id,
        fingerprint="a" * 63 + "7",
        location="New York, NY",
        facts=_hybrid_facts("lever", "New York, NY"),
        title="Data Engineer",
        url=url,
    )
    _store_body(durable_database, workspace_id, pending_id, url, b"This role is hybrid. #LI-remote")

    result = _run_through_worker(durable_database, workspace_id)

    assert (result["screened"], result["held_out"]) == (1, 0)
    assert _events(durable_database, pending_id) == []


def test_the_task_holds_out_an_office_row_whose_retained_body_offers_no_remote_work(
    durable_database: Database,
) -> None:
    """A US city with no stated arrangement at a remote-only market is held out."""
    workspace_id = scan_workspace(durable_database, slug="rescreen")
    SearchParametersService(durable_database).save(workspace_id, _hybrid_holdout_parameters())
    url = "https://boards.lever.co/acme/jobs/denver-office"
    pending_id = _pending_row(
        durable_database,
        workspace_id,
        fingerprint="a" * 63 + "8",
        location="Denver, Colorado",
        facts=StagedListingFacts(
            locations=PublishedFact(
                values=("Denver, Colorado", "United States"),
                provenance="lever.location",
                complete=True,
            )
        ),
        title="Data Engineer",
        url=url,
    )
    _store_body(
        durable_database, workspace_id, pending_id, url, b"Join the data team at our Denver office."
    )

    result = _run_through_worker(durable_database, workspace_id)

    assert (result["screened"], result["held_out"]) == (1, 1)
    [event] = _events(durable_database, pending_id)
    assert event.category == "arrangement_remote_only_sites"


def test_the_task_saves_the_pay_a_retained_body_states_on_a_row_staged_without_it(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database, slug="rescreen")
    _us_search(durable_database, workspace_id)
    url = "https://boards.greenhouse.io/acme/jobs/data-platform-lead"
    facts = published_listing_facts("greenhouse", locations="New York, NY, United States")
    pending_id = _pending_row(
        durable_database,
        workspace_id,
        fingerprint="a" * 63 + "a",
        location="New York, NY, United States",
        facts=facts,
        url=url,
    )
    _store_body(
        durable_database,
        workspace_id,
        pending_id,
        url,
        b"Data Platform Lead\n\nCompensation: USD 140,000 - 170,000 per year.\n\n"
        b"Own the warehouse ingestion platform and its on-call rotation.\n",
    )
    # Staged facts that state no pay, beside a retained body that does.
    with durable_database.session() as session:
        session.get_one(PendingDiscovery, pending_id).staged_facts = facts.as_json()

    _run_through_worker(durable_database, workspace_id)

    with durable_database.session() as session:
        row = session.get_one(PendingDiscovery, pending_id)
        pay = StagedListingFacts.from_json(row.staged_facts).compensation
    assert pay.values == ("USD 140000-170000 per year",)


def test_the_task_reads_a_us_listings_placeless_dollar_as_usd_beside_only_cad_bands(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database, slug="rescreen")
    SearchParametersService(durable_database).save(workspace_id, canadian_pay_with_a_us_place())
    url = "https://boards.greenhouse.io/acme/jobs/data-platform-lead-austin"
    location = "Austin, Texas, United States"
    facts = published_listing_facts("greenhouse", locations=location)
    pending_id = _pending_row(
        durable_database,
        workspace_id,
        fingerprint="a" * 63 + "c",
        location=location,
        facts=facts,
        url=url,
    )
    _store_body(
        durable_database,
        workspace_id,
        pending_id,
        url,
        b"Data Platform Lead\n\nCompensation: $120,000 - $150,000 per year.\n\n"
        b"Own the warehouse ingestion platform and its on-call rotation.\n",
    )
    # Staged facts that state no pay, beside a retained body that does.
    with durable_database.session() as session:
        session.get_one(PendingDiscovery, pending_id).staged_facts = facts.as_json()

    _run_through_worker(durable_database, workspace_id)

    with durable_database.session() as session:
        row = session.get_one(PendingDiscovery, pending_id)
        pay = StagedListingFacts.from_json(row.staged_facts).compensation
    assert pay.values == ("USD 120000-150000 per year",)


class _RereadAfterReading(CurrentPostingContentReader):
    """Hands the task one body, then a read of the posting stores a newer one."""

    def __init__(self, database: Database, url: str, body: bytes) -> None:
        super().__init__(database, storage=runtime_storage(database))
        self.url = url
        self.body = body

    def read(self, workspace_id: str, pending_id: str) -> CurrentPostingContent | None:
        current = super().read(workspace_id, pending_id)
        _store_body(self.database, workspace_id, pending_id, self.url, self.body)
        return current


def test_the_task_leaves_pay_a_newer_body_saved_while_it_read_an_older_one(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database, slug="rescreen")
    _us_search(durable_database, workspace_id)
    url = "https://boards.greenhouse.io/acme/jobs/data-platform-lead-reread"
    pending_id = _pending_row(
        durable_database,
        workspace_id,
        fingerprint="a" * 63 + "b",
        location="New York, NY, United States",
        facts=published_listing_facts("greenhouse", locations="New York, NY, United States"),
        url=url,
    )
    body = (
        b"Data Platform Lead\n\nCompensation: USD 140,000 - 170,000 per year.\n\n"
        b"Own the warehouse ingestion platform and its on-call rotation.\n"
    )
    _store_body(durable_database, workspace_id, pending_id, url, body)
    handler = _handler(durable_database)
    handler.service.sweep.rescreen.current_content = _RereadAfterReading(
        durable_database, url, body.replace(b"140,000 - 170,000", b"150,000 - 180,000")
    )

    handler(_context(durable_database, workspace_id), {})

    with durable_database.session() as session:
        row = session.get_one(PendingDiscovery, pending_id)
        pay = StagedListingFacts.from_json(row.staged_facts).compensation
    assert pay.values == ("USD 150000-180000 per year",)


def test_cancelling_the_task_leaves_the_rows_it_has_not_reached_untouched(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database, slug="rescreen")
    _us_search(durable_database, workspace_id)
    pending_ids = [
        _london_row(durable_database, workspace_id, "b" * 63 + str(index)) for index in range(3)
    ]
    context = _context(durable_database, workspace_id, cancel_after_checks=1)

    with pytest.raises(TaskCancelledError):
        _handler(durable_database)(context, {})

    touched = [pending_id for pending_id in pending_ids if _events(durable_database, pending_id)]
    assert len(touched) == 1
    assert context.stages == ["rescreening"]


def test_a_row_that_leaves_the_queue_mid_task_counts_as_vanished(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database, slug="rescreen")
    _us_search(durable_database, workspace_id)
    pending_id = _london_row(durable_database, workspace_id, "c" * 64)

    def archive_the_row_being_screened(payload: dict[str, Any]) -> None:
        with durable_database.session() as session:
            row = session.get(PendingDiscovery, payload["pending_discovery_id"])
            assert row is not None
            row.archived_at = utc_now()

    context = _context(durable_database, workspace_id, on_progress=archive_the_row_being_screened)
    result = _handler(durable_database)(context, {})

    assert (result["vanished"], result["vanished_ids"]) == (1, [pending_id])
    assert (result["screened"], result["failed"]) == (0, 0)
    assert _events(durable_database, pending_id) == []


class _UnavailableFor(CurrentPostingContentReader):
    """Retained evidence one row cannot currently supply."""

    def __init__(self, database: Database, pending_id: str) -> None:
        super().__init__(database, storage=runtime_storage(database))
        self.pending_id = pending_id

    def read(self, workspace_id: str, pending_id: str) -> CurrentPostingContent | None:
        if pending_id == self.pending_id:
            raise UnavailableError("The retained evidence store is offline.")
        return super().read(workspace_id, pending_id)


def test_a_row_failure_is_recorded_and_the_rest_of_the_queue_is_still_screened(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database, slug="rescreen")
    _us_search(durable_database, workspace_id)
    failing = _london_row(durable_database, workspace_id, "d" * 63 + "1")
    held = _london_row(durable_database, workspace_id, "d" * 63 + "2")
    handler = _handler(durable_database)
    handler.service.sweep.rescreen.current_content = _UnavailableFor(durable_database, failing)

    result = handler(_context(durable_database, workspace_id), {})

    assert result["failed"] == 1
    assert result["failures"] == [
        {
            "pending_discovery_id": failing,
            "error_category": "UnavailableError",
            "error_message": "The retained evidence store is offline.",
        }
    ]
    assert (result["held_out"], result["held_out_ids"]) == (1, [held])
    assert _events(durable_database, failing) == []


def test_a_person_dismissal_landing_mid_task_is_never_overridden(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database, slug="rescreen")
    _us_search(durable_database, workspace_id)
    pending_id = _london_row(durable_database, workspace_id, "f" * 64)

    def person_dismisses_the_row_being_screened(payload: dict[str, Any]) -> None:
        dismiss_pending_discovery(durable_database, workspace_id, payload["pending_discovery_id"])

    context = _context(
        durable_database, workspace_id, on_progress=person_dismisses_the_row_being_screened
    )
    result = _handler(durable_database)(context, {})

    assert (result["held_out"], result["vanished_ids"]) == (0, [pending_id])
    [event] = _events(durable_database, pending_id)
    assert (event.action, event.category, event.created_by) == (
        "dismiss",
        "human_review",
        "local_user",
    )


def test_no_other_writer_can_land_between_a_row_read_and_its_hold_out(
    durable_database: Database,
) -> None:
    """A person's write attempted inside the row's transaction waits for the hold-out."""
    workspace_id = scan_workspace(durable_database, slug="rescreen")
    _us_search(durable_database, workspace_id)
    pending_id = _london_row(durable_database, workspace_id, "9" * 64)
    database_path = durable_database.engine.url.database
    assert database_path is not None
    attempts: list[str] = []

    def person_writes_before_the_hold_out(
        conn: Any, cursor: Any, statement: str, *args: Any
    ) -> None:
        if attempts or not statement.startswith("INSERT INTO pending_discovery_disposition"):
            return
        person = sqlite3.connect(database_path, timeout=0)
        try:
            person.execute("BEGIN IMMEDIATE")
            person.rollback()
            attempts.append("written")
        except sqlite3.OperationalError:
            attempts.append("refused")
        finally:
            person.close()

    sa_event.listen(
        durable_database.engine, "before_cursor_execute", person_writes_before_the_hold_out
    )
    try:
        result = _handler(durable_database)(_context(durable_database, workspace_id), {})
    finally:
        sa_event.remove(
            durable_database.engine, "before_cursor_execute", person_writes_before_the_hold_out
        )

    assert attempts == ["refused"]
    assert (result["held_out_ids"], result["failed"]) == ([pending_id], 0)


def _latest(database: Database, pending_id: str) -> PendingDiscoveryDispositionEvent:
    return _events(database, pending_id)[-1]


def test_a_holdout_the_current_rules_no_longer_make_returns_to_the_queue(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database, slug="rescreen")
    _us_search(durable_database, workspace_id)
    pending_id = _london_row(durable_database, workspace_id, "b" * 63 + "1")
    _run_through_worker(durable_database, workspace_id)
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            seniority=Seniority(target_titles=titles("Data Platform Lead")),
            locations=[
                Location(key="us", country="United States"),
                Location(key="uk", country="United Kingdom"),
            ],
        ),
    )

    result = _run_through_worker(durable_database, workspace_id)

    assert result["restored_ids"] == [pending_id]
    latest = _latest(durable_database, pending_id)
    assert (latest.action, latest.origin) == ("restore", "discovery.rescreen_queue.restore")


def _travel_search(limit: int) -> JobSearchParameters:
    return JobSearchParameters(
        seniority=Seniority(target_titles=titles("Data Platform Lead")),
        locations=[Location(key="us", country="United States")],
        arrangement=Arrangement(maximum_travel_percent=limit),
    )


def test_a_rule_refusal_a_raised_limit_no_longer_makes_returns_to_the_queue(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database, slug="rescreen")
    SearchParametersService(durable_database).save(workspace_id, _travel_search(15))
    url = "https://boards.greenhouse.io/acme/jobs/travel"
    pending_id = _pending_row(
        durable_database,
        workspace_id,
        fingerprint="b" * 63 + "2",
        location="New York, NY, United States",
        facts=published_listing_facts("greenhouse", locations="New York, NY, United States"),
        url=url,
    )
    _store_body(
        durable_database,
        workspace_id,
        pending_id,
        url,
        b"Data Platform Lead\n\nThis role requires travel up to 20% of the time.\n",
    )
    file_refusal(
        durable_database,
        RefusedPosting(workspace_id, "Acme", "Data Platform Lead", pending_discovery_id=pending_id),
        condition="travel",
        reason="20 per cent against your 15",
        task_id=None,
        origin="evaluation.refusal",
        refresh=False,
    )
    _run_through_worker(durable_database, workspace_id)
    assert _latest(durable_database, pending_id).action == "dismiss"
    SearchParametersService(durable_database).save(workspace_id, _travel_search(25))

    result = _run_through_worker(durable_database, workspace_id)

    assert result["restored_ids"] == [pending_id]
    assert _latest(durable_database, pending_id).action == "restore"


@pytest.mark.parametrize(
    ("place", "countries", "restored"),
    [
        ("Lindenfeld, Germ, Ba", ("United States",), False),
        ("Lindenfeld, Germ, Ba", ("United States", "Germany"), False),
        ("Lindenfeld, Germany", ("United States", "Germany"), True),
    ],
)
def test_a_rescreen_restores_a_body_screen_location_dismissal_only_once_the_place_reads_inside(
    durable_database: Database, place: str, countries: tuple[str, ...], restored: bool
) -> None:
    """A place the rescreen cannot read is no evidence the posting moved inside the boundary."""
    workspace_id = scan_workspace(durable_database, slug="rescreen")
    _us_search(durable_database, workspace_id)
    url = "https://boards.greenhouse.io/acme/jobs/abroad"
    pending_id = _pending_row(
        durable_database,
        workspace_id,
        fingerprint="b" * 63 + "5",
        location="Lindenfeld, Germany",
        facts=published_listing_facts("greenhouse", locations="Lindenfeld, Germany"),
        url=url,
    )
    _store_body(
        durable_database,
        workspace_id,
        pending_id,
        url,
        b"Data Platform Lead\n\nRun the regional carrier desk.\n",
        origin="source.scan",
    )
    assert _latest(durable_database, pending_id).category == "location_outside"
    with durable_database.session() as session:
        row = session.get_one(PendingDiscovery, pending_id)
        row.location = place
        row.staged_facts = published_listing_facts("greenhouse", locations=place).as_json()
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            seniority=Seniority(target_titles=titles("Data Platform Lead")),
            locations=[Location(key=country.lower(), country=country) for country in countries],
        ),
    )

    result = _run_through_worker(durable_database, workspace_id)

    assert result["restored_ids"] == ([pending_id] if restored else [])


def test_a_rescreen_never_restores_a_dismissal_a_person_made(durable_database: Database) -> None:
    workspace_id = scan_workspace(durable_database, slug="rescreen")
    _us_search(durable_database, workspace_id)
    pending_id = _pending_row(
        durable_database,
        workspace_id,
        fingerprint="b" * 63 + "3",
        location="New York, NY, United States",
        facts=published_listing_facts("greenhouse", locations="New York, NY, United States"),
    )
    dismiss_pending_discovery(durable_database, workspace_id, pending_id)

    result = _run_through_worker(durable_database, workspace_id)

    assert result["restored_ids"] == []
    [event] = _events(durable_database, pending_id)
    assert (event.action, event.category) == ("dismiss", "human_review")


def _form_search(*, us_home: bool) -> JobSearchParameters:
    locations = [
        Location(key="toronto", country="Canada", resident=not us_home),
        Location(key="us_remote", country="United States", resident=us_home),
    ]
    return JobSearchParameters(
        locations=locations,
        countries=[CountryNames(country=place.country) for place in locations],
    )


def test_a_form_residence_dismissal_stands_until_the_person_lives_in_the_us(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database, slug="rescreen")
    SearchParametersService(durable_database).save(workspace_id, _form_search(us_home=False))
    url = "https://boards.greenhouse.io/acme/jobs/form"
    pending_id = _pending_row(
        durable_database,
        workspace_id,
        fingerprint="b" * 63 + "4",
        location="Remote - United States",
        facts=published_listing_facts("greenhouse", locations="Remote - United States"),
        url=url,
    )
    question = {
        "required": True,
        "label": "Which state do you reside in?",
        "fields": [{"name": "q", "values": [{"label": "Ohio"}, {"label": "Texas"}]}],
    }
    _store_body(
        durable_database,
        workspace_id,
        pending_id,
        url,
        b"Data Platform Lead\n\nRemote work is welcome.\n",
        origin="source.scan",
        source=PostingSource(publisher="greenhouse", record=json.dumps({"questions": [question]})),
    )
    assert _latest(durable_database, pending_id).category == "location_residence_required"

    kept = _run_through_worker(durable_database, workspace_id)
    SearchParametersService(durable_database).save(workspace_id, _form_search(us_home=True))
    restored = _run_through_worker(durable_database, workspace_id)

    assert kept["restored_ids"] == []
    assert restored["restored_ids"] == [pending_id]


def test_an_arrangement_holdout_with_no_retained_body_stays_held_out(
    durable_database: Database,
) -> None:
    """Without the body it was judged on, an arrangement hold-out cannot be judged again."""
    workspace_id = scan_workspace(durable_database, slug="rescreen")
    SearchParametersService(durable_database).save(workspace_id, _hybrid_holdout_parameters())
    pending_id = _pending_row(
        durable_database,
        workspace_id,
        fingerprint="b" * 63 + "5",
        location="New York, NY",
        facts=_hybrid_facts("lever", "New York, NY"),
        title="Data Engineer",
    )
    with durable_database.session() as session:
        session.add(
            PendingDiscoveryDispositionEvent(
                id=new_id(),
                workspace_id=workspace_id,
                pending_discovery_id=pending_id,
                action="dismiss",
                category="arrangement_refused",
                reason="Verified board facts meet a structured search hold-out.",
                selection_mode="rule",
                snapshot_sha256="holdout",
                created_by="worker",
                origin="source.scan.structured_holdout",
            )
        )

    result = _run_through_worker(durable_database, workspace_id)

    assert result["restored_ids"] == []
