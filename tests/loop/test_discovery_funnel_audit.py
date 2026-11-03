"""Discovery funnel paths and task-history bounds."""

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import event, func, select, update
from sqlalchemy.orm import Session

from clearscout.application.discovery import board_scan
from clearscout.application.discovery.board_scan import SourceScanService
from clearscout.application.discovery.funnel_audit import (
    FunnelAuditBuilder,
    FunnelCandidateKind,
    FunnelOutcome,
    FunnelReason,
    FunnelStage,
    bulk_staging_refusal_outcome,
)
from clearscout.application.discovery.screening import (
    BulkStagingRefusal,
    DiscoveryScreening,
    ScreeningCode,
    ScreeningDimension,
    ScreeningFinding,
    ScreeningStanding,
)
from clearscout.application.discovery.source_continuation import (
    SourceContinuationHandler,
    continuation_task_ids,
)
from clearscout.application.discovery.source_reading import (
    SourceContinuationTarget,
    SourceListing,
    SourceRead,
)
from clearscout.application.tasks import TaskQueueService
from clearscout.bootstrap.worker import (
    Worker,
    _LeaseKeeper,
    _WorkerTaskContext,
    default_handlers,
)
from clearscout.domain import SourceReadOutcome, new_id, utc_now
from clearscout.domain.identity import normalize_url, url_sha256
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    BackgroundTask,
    PendingDiscovery,
    SourceScan,
    SourceScanContinuation,
    SourceScanMember,
    SourceScanOutcome,
)
from clearscout.infrastructure.web_fetch.listing_facts import published_listing_facts
from tests.support.artifacts import runtime_storage
from tests.support.doubles import ScriptedBoards, UnexpectedPostingFetcher
from tests.support.scenarios import (
    board_source,
    dismiss_pending_discovery,
    pending_discovery,
    scan_workspace,
    seed_scan_continuation,
    source_listing,
    state_single_market,
    state_target_titles,
)


def _scan_service(database: Database) -> SourceScanService:
    return SourceScanService(
        database,
        storage=runtime_storage(database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )


def _outcome(url: str, stages: tuple[FunnelStage, ...], reason: FunnelReason) -> FunnelOutcome:
    return FunnelOutcome(
        kind=FunnelCandidateKind.BOARD_LISTING,
        title="Distributed Systems Steward",
        url=url,
        stages=stages,
        reason=reason,
    )


def test_impossible_funnel_paths_are_not_expressible() -> None:
    with pytest.raises(ValidationError, match="supported path"):
        _outcome(
            "https://jobs.example.test/1",
            (FunnelStage.PROPOSED, FunnelStage.STAGED, FunnelStage.REJECTED),
            FunnelReason.STAGED,
        )


def test_bounded_detail_keeps_full_counts_and_states_what_was_omitted() -> None:
    audit = FunnelAuditBuilder(limit=1)
    path = (
        FunnelStage.PROPOSED,
        FunnelStage.FETCHED,
        FunnelStage.READ,
        FunnelStage.SCREENED,
        FunnelStage.STAGED,
    )
    audit.record(_outcome("https://jobs.example.test/1", path, FunnelReason.STAGED))
    audit.record(_outcome("https://jobs.example.test/2", path, FunnelReason.STAGED))

    result = audit.build()

    assert len(result.outcomes) == 1
    assert result.total_outcomes == 2
    assert result.omitted_outcomes == 1
    assert result.omission_reason
    assert result.counts.staged == 2
    assert result.reason_counts == {"staged": 2}


def test_outcome_sink_receives_every_candidate_even_when_progress_detail_is_bounded() -> None:
    recorded: list[FunnelOutcome] = []
    audit = FunnelAuditBuilder(limit=1, on_record=recorded.append)
    path = (FunnelStage.PROPOSED, FunnelStage.REJECTED)
    for index in range(3):
        audit.record(
            _outcome(
                f"https://jobs.example.test/{index}",
                path,
                FunnelReason.INSUFFICIENT_TITLE_EVIDENCE,
            )
        )

    result = audit.build()

    assert len(result.outcomes) == 1
    assert [item.url for item in recorded] == [
        "https://jobs.example.test/0",
        "https://jobs.example.test/1",
        "https://jobs.example.test/2",
    ]


def test_bounded_detail_keeps_later_staged_outcomes_visible() -> None:
    audit = FunnelAuditBuilder(limit=2)
    rejected_path = (FunnelStage.PROPOSED, FunnelStage.REJECTED)
    staged_path = (
        FunnelStage.PROPOSED,
        FunnelStage.FETCHED,
        FunnelStage.READ,
        FunnelStage.SCREENED,
        FunnelStage.STAGED,
    )
    audit.record(
        _outcome(
            "https://jobs.example.test/rejected-1",
            rejected_path,
            FunnelReason.INSUFFICIENT_TITLE_EVIDENCE,
        )
    )
    audit.record(
        _outcome(
            "https://jobs.example.test/rejected-2",
            rejected_path,
            FunnelReason.INSUFFICIENT_TITLE_EVIDENCE,
        )
    )
    audit.record(
        _outcome(
            "https://jobs.example.test/staged",
            staged_path,
            FunnelReason.STAGED,
        )
    )

    result = audit.build()

    assert [outcome.url for outcome in result.outcomes] == [
        "https://jobs.example.test/rejected-1",
        "https://jobs.example.test/staged",
    ]
    assert result.total_outcomes == 3
    assert result.omitted_outcomes == 1
    assert result.counts.staged == 1
    assert result.counts.rejected == 2


def test_listing_outcome_retains_decisive_screening_findings() -> None:
    audit = FunnelAuditBuilder()
    screening = DiscoveryScreening(
        (
            ScreeningFinding(
                ScreeningDimension.TITLE,
                ScreeningStanding.REFUSE,
                ScreeningCode.TITLE_MISMATCH,
                "Data Platform Manager",
            ),
            ScreeningFinding(
                ScreeningDimension.LOCATION,
                ScreeningStanding.MATCH,
                ScreeningCode.LOCATION_MATCH,
                "Northport",
            ),
        )
    )

    audit.record_listing(
        _outcome(
            "https://jobs.example.test/filtered",
            (FunnelStage.PROPOSED,),
            FunnelReason.STAGED,
        ),
        source="Synthetic board",
        terminal=FunnelStage.STAGED,
        reason=FunnelReason.STAGED,
        screening=screening,
    )

    assert audit.build().outcomes[0].screening_findings[0].model_dump() == {
        "dimension": "title",
        "code": "title_mismatch",
        "subject": "Data Platform Manager",
    }


def test_source_scan_keeps_large_unsupported_cohort_out_of_pending_but_in_ledger(
    durable_database: Database,
) -> None:
    """No-loss uses the durable ledger without flooding the paid-review queue."""
    workspace_id = scan_workspace(durable_database)
    board_source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    unsupported_count = 400
    boards = ScriptedBoards(
        {
            "Acme": [
                source_listing(
                    f"Payroll Specialist {index}",
                    f"https://boards.greenhouse.io/acme/jobs/payroll-{index}",
                )
                for index in range(unsupported_count)
            ]
        }
    )
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={},
        idempotency_key="scan-unsupported-ledger",
    )
    worker = Worker(
        durable_database,
        worker_id="unsupported-ledger-worker",
        handlers=default_handlers(durable_database, fetcher=boards),
    )

    assert worker.run_once()

    finished = queue.get(str(task.id))
    assert finished.status == "completed", (finished.error_category, finished.error_message)
    assert finished.progress["new_discoveries"] == 0
    with durable_database.session() as session:
        assert session.scalar(select(func.count()).select_from(PendingDiscovery)) == 0
        outcomes = session.scalars(
            select(SourceScanOutcome).where(
                SourceScanOutcome.task_id == str(task.id),
                SourceScanOutcome.candidate_kind == "board_listing",
            )
        ).all()
    assert len(outcomes) == unsupported_count
    assert {outcome.reason for outcome in outcomes} == {"insufficient_title_evidence"}
    assert {outcome.terminal_stage for outcome in outcomes} == {"screened"}
    assert {outcome.candidate_kind for outcome in outcomes} == {"board_listing"}
    assert all(
        outcome.url.startswith("https://boards.greenhouse.io/acme/jobs/") for outcome in outcomes
    )


def test_a_scan_resolves_its_ledger_family_once_for_the_whole_pass(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dedupe across a scan and its continuations costs one resolution, not one per row."""
    workspace_id = scan_workspace(durable_database)
    board_source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    listing_count = 12
    boards = ScriptedBoards(
        {
            "Acme": [
                source_listing(
                    f"Payroll Specialist {index}",
                    f"https://boards.greenhouse.io/acme/jobs/payroll-{index}",
                )
                for index in range(listing_count)
            ]
        }
    )
    resolved_for = _counted_family_resolutions(monkeypatch)
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={},
        idempotency_key="scan-ledger-family-once",
    )
    worker = Worker(
        durable_database,
        worker_id="ledger-family-worker",
        handlers=default_handlers(durable_database, fetcher=boards),
    )

    assert worker.run_once()

    finished = queue.get(str(task.id))
    assert finished.status == "completed", (finished.error_category, finished.error_message)
    with durable_database.session() as session:
        recorded = session.scalar(
            select(func.count())
            .select_from(SourceScanOutcome)
            .where(
                SourceScanOutcome.task_id == str(task.id),
                SourceScanOutcome.candidate_kind == "board_listing",
            )
        )
    assert recorded == listing_count
    assert resolved_for == [str(task.id)]


def test_a_scan_commits_each_boards_outcomes_in_one_transaction(
    durable_database: Database,
) -> None:
    """A board's funnel rows land in one commit, however many listings it answered."""
    workspace_id = scan_workspace(durable_database)
    board_source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    board_source(durable_database, workspace_id, "Globex", "https://boards.greenhouse.io/globex")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    boards = ScriptedBoards(
        {
            name: [
                source_listing(
                    f"Payroll Specialist {index}",
                    f"https://boards.greenhouse.io/{name.lower()}/jobs/payroll-{index}",
                )
                for index in range(count)
            ]
            for name, count in (("Acme", 5), ("Globex", 3))
        }
    )
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={},
        idempotency_key="scan-one-commit-per-board",
    )
    worker = Worker(
        durable_database,
        worker_id="one-commit-per-board-worker",
        handlers=default_handlers(durable_database, fetcher=boards),
    )
    inserting: set[int] = set()
    outcome_commits: list[int] = []

    def flushed(session: Session, _context: object) -> None:
        if any(isinstance(row, SourceScanOutcome) for row in session.new):
            inserting.add(id(session))

    def committed(session: Session) -> None:
        if id(session) in inserting:
            inserting.discard(id(session))
            outcome_commits.append(id(session))

    event.listen(Session, "after_flush", flushed)
    event.listen(Session, "after_commit", committed)
    try:
        assert worker.run_once()
    finally:
        event.remove(Session, "after_flush", flushed)
        event.remove(Session, "after_commit", committed)

    finished = queue.get(str(task.id))
    assert finished.status == "completed", (finished.error_category, finished.error_message)
    with durable_database.session() as session:
        per_source = dict(
            session.execute(
                select(SourceScanOutcome.source_name, func.count())
                .where(
                    SourceScanOutcome.task_id == str(task.id),
                    SourceScanOutcome.candidate_kind == "board_listing",
                )
                .group_by(SourceScanOutcome.source_name)
            )
            .tuples()
            .all()
        )
    assert per_source == {"Acme": 5, "Globex": 3}
    assert len(outcome_commits) == 2


def _scan_that_fails_after_screening(
    database: Database, monkeypatch: pytest.MonkeyPatch, key: str
) -> tuple[SourceScanService, _WorkerTaskContext]:
    """Seed one board whose scan raises after its listings were screened and audited."""
    workspace_id = scan_workspace(database)
    board = "https://boards.greenhouse.io/acme"
    board_source(database, workspace_id, "Acme", board)
    state_target_titles(database, workspace_id, "Data Platform Lead")
    service = SourceScanService(
        database,
        storage=runtime_storage(database),
        posting_fetcher=UnexpectedPostingFetcher(),
        fetcher=ScriptedBoards(
            {
                "Acme": [
                    source_listing(f"Payroll Specialist {n}", f"{board}/jobs/{n}") for n in range(3)
                ]
            }
        ),
    )

    def disk_full(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(service.staging, "withdraw_absent", disk_full)
    queue = TaskQueueService(database)
    task = queue.enqueue(
        workspace_id=workspace_id, task_type="source.scan", payload={}, idempotency_key=key
    )
    queue.claim_next(worker_id=key)
    keeper = _LeaseKeeper(queue, str(task.id), key, 20.0)
    return service, _WorkerTaskContext(queue, str(task.id), key, keeper)


def test_a_board_that_raises_keeps_the_outcomes_it_recorded_first(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, context = _scan_that_fails_after_screening(
        durable_database, monkeypatch, "board-raises-keeps-outcomes"
    )
    workspace_id = str(context.queue.get(context.task_id).workspace_id)

    with pytest.raises(OSError, match="disk full"):
        service.scan(workspace_id, context)

    with durable_database.session() as session:
        urls = session.scalars(
            select(SourceScanOutcome.url).where(
                SourceScanOutcome.task_id == context.task_id,
                SourceScanOutcome.candidate_kind == "board_listing",
            )
        ).all()
    assert sorted(urls) == [f"https://boards.greenhouse.io/acme/jobs/{n}" for n in range(3)]


def test_a_failing_outcome_write_does_not_replace_the_boards_own_error(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, context = _scan_that_fails_after_screening(
        durable_database, monkeypatch, "board-raises-sink-fails"
    )
    workspace_id = str(context.queue.get(context.task_id).workspace_id)

    def locked(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(service, "record_task_outcomes", locked)

    with pytest.raises(OSError, match="disk full"):
        service.scan(workspace_id, context)


def test_a_batch_stores_the_rows_recording_one_outcome_at_a_time_stores(
    durable_database: Database,
) -> None:
    """Dedup inside one batch keeps the first of each source, kind, and address."""
    workspace_id = scan_workspace(durable_database)
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    queue = TaskQueueService(durable_database)
    per_row_task, batch_task = (
        str(
            queue.enqueue(
                workspace_id=workspace_id,
                task_type="source.scan",
                payload={},
                idempotency_key=f"outcome-batch-{label}",
            ).id
        )
        for label in ("per-row", "batch")
    )
    board = "https://boards.greenhouse.io/acme/jobs"
    rejected = (FunnelStage.PROPOSED, FunnelStage.REJECTED)
    title_miss = FunnelReason.INSUFFICIENT_TITLE_EVIDENCE

    def listing(url: str, title: str = "Data Platform Lead", **fields: Any) -> FunnelOutcome:
        return FunnelOutcome(
            kind=fields.pop("kind", FunnelCandidateKind.BOARD_LISTING),
            title=title,
            url=url,
            stages=fields.pop("stages", rejected),
            reason=fields.pop("reason", title_miss),
            **fields,
        )

    outcomes = [
        listing(f"{board}/1", stages=_STAGED_PATH, reason=FunnelReason.STAGED),
        listing(f"{board}/2", detail="screened out"),
        listing(f"{board}/1", title="Repeated"),
        listing(f"{board}/1#apply", title="Same address"),
        listing(f"{board}/1", kind=FunnelCandidateKind.BOARD_LEAD),
        listing(f"{board}/1", source="Globex"),
        listing(f"{board}/2", source="Globex", title="Globex copy"),
        listing("", title="No address"),
    ]
    service = _scan_service(durable_database)

    per_row_added = [
        outcome
        for outcome in outcomes
        if _recorded_one(service, workspace_id, per_row_task, outcome)
    ]
    with durable_database.session() as session:
        batch_added = service.record_task_outcomes_in_session(
            session,
            workspace_id,
            batch_task,
            outcomes,
            origin="source.scan",
            default_source="Acme",
        )

    def stored(task_id: str) -> list[tuple[Any, ...]]:
        with durable_database.session() as session:
            rows = session.scalars(
                select(SourceScanOutcome).where(SourceScanOutcome.task_id == task_id)
            ).all()
            return sorted(
                (
                    row.source_name,
                    row.candidate_kind,
                    row.url,
                    row.normalized_url,
                    row.title,
                    row.terminal_stage,
                    row.reason,
                    row.detail,
                    row.source_scan_id,
                    row.origin,
                )
                for row in rows
            )

    assert list(batch_added) == per_row_added
    assert stored(batch_task) == stored(per_row_task)
    first = "Data Platform Lead"
    assert [(row[0], row[1], row[3], row[4]) for row in stored(batch_task)] == [
        ("Acme", "board_lead", normalize_url(f"{board}/1"), first),
        ("Acme", "board_listing", normalize_url(f"{board}/1"), first),
        ("Acme", "board_listing", normalize_url(f"{board}/2"), first),
        ("Globex", "board_listing", normalize_url(f"{board}/1"), first),
        ("Globex", "board_listing", normalize_url(f"{board}/2"), "Globex copy"),
    ]


def _recorded_one(
    service: SourceScanService, workspace_id: str, task_id: str, outcome: FunnelOutcome
) -> bool:
    """Record one outcome in its own session and say whether it added a row."""
    with service.database.session() as session:
        return bool(
            service.record_task_outcomes_in_session(
                session,
                workspace_id,
                task_id,
                (outcome,),
                origin="source.scan",
                default_source="Acme",
            )
        )


def test_scan_history_counts_screened_out_listings_until_they_reach_pending(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    board_source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    board_source(durable_database, workspace_id, "Globex", "https://boards.greenhouse.io/globex")
    state_single_market(
        durable_database, workspace_id, "Data Engineer", key="canada_remote", country="Canada"
    )
    acme = "https://boards.greenhouse.io/acme/jobs"
    foreign = replace(
        source_listing("Data Engineer", f"{acme}/foreign", location="Amsterdam, Netherlands"),
        facts=published_listing_facts("greenhouse", locations="Amsterdam, Netherlands"),
    )
    boards = ScriptedBoards(
        {
            "Acme": [
                source_listing("Data Engineer", f"{acme}/staged", location="Canada"),
                foreign,
                source_listing("Payroll Specialist", f"{acme}/payroll-1"),
                source_listing("Payroll Specialist", f"{acme}/payroll-2"),
            ],
            "Globex": [
                source_listing(
                    "Payroll Specialist", "https://boards.greenhouse.io/globex/jobs/payroll"
                )
            ],
        }
    )
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={},
        idempotency_key="scan-screened-out-count",
    )
    worker = Worker(
        durable_database,
        worker_id="screened-out-count-worker",
        handlers=default_handlers(durable_database, fetcher=boards),
    )
    assert worker.run_once()
    finished = queue.get(str(task.id))
    assert finished.status == "completed", (finished.error_category, finished.error_message)
    with durable_database.session() as session:
        ledger = (
            session.execute(
                select(SourceScanOutcome.url, SourceScanOutcome.reason).where(
                    SourceScanOutcome.task_id == str(task.id),
                    SourceScanOutcome.source_name == "Acme",
                )
            )
            .tuples()
            .all()
        )
    reasons = dict(ledger)
    assert reasons[f"{acme}/staged"] == "staged"
    assert reasons[f"{acme}/foreign"] == "hard_constraint_excluded"
    assert {url for url, reason in ledger if reason == "insufficient_title_evidence"} == {
        f"{acme}/payroll-1",
        f"{acme}/payroll-2",
    }
    service = _scan_service(durable_database)

    def counts() -> dict[str, int]:
        return {
            record.source_name: record.screened_out_count
            for record in service.list_scan_history(workspace_id).items
        }

    assert counts() == {"Acme": 3, "Globex": 1}
    assert service.stage_found_posting(workspace_id, f"{acme}/payroll-1", utc_now().date())
    assert counts() == {"Acme": 2, "Globex": 1}


_STAGED_PATH = (
    FunnelStage.PROPOSED,
    FunnelStage.FETCHED,
    FunnelStage.READ,
    FunnelStage.SCREENED,
    FunnelStage.STAGED,
)


def _scan_with_continuation(database: Database) -> tuple[str, str, str, str]:
    """Seed one board scan task, its scan row, and the continuation task that drains it."""
    workspace_id = scan_workspace(database)
    source_id = board_source(database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    state_target_titles(database, workspace_id, "Data Platform Lead")
    queue = TaskQueueService(database)
    scan_task = str(
        queue.enqueue(
            workspace_id=workspace_id,
            task_type="source.scan",
            payload={},
            idempotency_key="funnel-ledger-scan",
        ).id
    )
    scan_id = new_id()
    now = utc_now()
    with database.session() as session:
        member = session.scalars(
            select(SourceScanMember).where(SourceScanMember.root_task_id == scan_task)
        ).one()
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
                correlation_id=scan_task,
            )
        )
        continuation_id = seed_scan_continuation(
            session,
            workspace_id,
            scan_id,
            [{"url": "https://boards.greenhouse.io/acme/jobs?page=2"}],
            origin="source.scan",
            correlation_id=scan_task,
        )
        continuation = session.get(SourceScanContinuation, continuation_id)
        assert continuation is not None
        continuation.batch_member_id = member.id
        member.source_scan_id = scan_id
        member.continuation_id = continuation_id
        member.generation = continuation.generation
        member.state = "continuing"
    continuation_task = str(
        queue.enqueue(
            workspace_id=workspace_id,
            task_type="source.continue",
            payload={"continuation_id": continuation_id, "generation": 1},
            idempotency_key="funnel-ledger-continue",
            correlation_id=scan_task,
        ).id
    )
    return workspace_id, scan_task, continuation_task, scan_id


def test_a_posting_read_by_a_scan_and_its_continuation_counts_once(
    durable_database: Database,
) -> None:
    workspace_id, scan_task, continuation_task, _scan_id = _scan_with_continuation(durable_database)
    service = _scan_service(durable_database)
    url = "https://boards.greenhouse.io/acme/jobs/shared"

    service.record_task_outcomes(
        workspace_id,
        scan_task,
        (_outcome(url, _STAGED_PATH, FunnelReason.STAGED),),
        origin="source.scan",
        default_source="Acme",
    )
    service.record_task_outcomes(
        workspace_id,
        continuation_task,
        (_outcome(url, _STAGED_PATH, FunnelReason.STAGED),),
        origin="source.continue",
        default_source="Acme",
    )

    assert service.list_scan_outcomes(workspace_id, task_id=scan_task).total == 1


def test_the_screened_out_count_includes_rows_a_continuation_screened_out(
    durable_database: Database,
) -> None:
    workspace_id, scan_task, continuation_task, _scan_id = _scan_with_continuation(durable_database)
    service = _scan_service(durable_database)
    rejected = (FunnelStage.PROPOSED, FunnelStage.REJECTED)
    title_miss = FunnelReason.INSUFFICIENT_TITLE_EVIDENCE
    service.record_task_outcomes(
        workspace_id,
        scan_task,
        (_outcome("https://boards.greenhouse.io/acme/jobs/first", rejected, title_miss),),
        origin="source.scan",
        default_source="Acme",
    )
    service.record_task_outcomes(
        workspace_id,
        continuation_task,
        (_outcome("https://boards.greenhouse.io/acme/jobs/deferred", rejected, title_miss),),
        origin="source.continue",
        default_source="Acme",
    )

    [record] = service.list_scan_history(workspace_id).items

    assert record.screened_out_count == 2
    assert service.list_scan_outcomes(workspace_id, task_id=scan_task).total == 2


def test_scan_history_deduplicates_its_family_and_counts_only_its_requested_source(
    durable_database: Database,
) -> None:
    workspace_id, scan_task, continuation_task, _scan_id = _scan_with_continuation(durable_database)
    unrelated = TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={},
        idempotency_key="unrelated-history-task",
    )
    shared = "https://boards.greenhouse.io/acme/jobs/shared"
    with durable_database.session() as session:
        for task_id, source_name, url, normalized in (
            (scan_task, "Acme", f"{shared}?from=board", shared),
            (continuation_task, "Acme", f"{shared}?from=detail", shared),
            (continuation_task, "Acme", f"{shared}/other", f"{shared}/other"),
            (scan_task, "Globex", f"{shared}/foreign-source", f"{shared}/foreign-source"),
            (continuation_task, "Globex", f"{shared}/foreign-child", f"{shared}/foreign-child"),
            (str(unrelated.id), "Acme", f"{shared}/unrelated", f"{shared}/unrelated"),
        ):
            session.add(
                SourceScanOutcome(
                    workspace_id=workspace_id,
                    task_id=task_id,
                    source_name=source_name,
                    candidate_kind="board_listing",
                    url=url,
                    normalized_url=normalized,
                    terminal_stage="rejected",
                    reason="insufficient_title_evidence",
                    created_by="worker",
                    origin="test",
                )
            )

    [record] = _scan_service(durable_database).list_scan_history(workspace_id).items

    assert (record.task_id, record.source_name, record.screened_out_count) == (scan_task, "Acme", 2)


@pytest.mark.parametrize(
    "pending_state", ["visible", "dismissed", "archived", "withdrawn", "variant"]
)
def test_scan_history_excludes_only_fingerprints_visible_in_pending(
    durable_database: Database, pending_state: str
) -> None:
    workspace_id, scan_task, _continuation_task, _scan_id = _scan_with_continuation(
        durable_database
    )
    url = "https://boards.greenhouse.io/acme/jobs/pending"
    pending = pending_discovery(workspace_id, employer="Acme", created_at=utc_now())
    pending.fingerprint = url_sha256(url)
    pending.discovery_url = url
    if pending_state == "archived":
        pending.archived_at = utc_now()
    elif pending_state == "withdrawn":
        pending.withdrawn_at = utc_now()
    with durable_database.session() as session:
        if pending_state == "variant":
            head = pending_discovery(workspace_id, employer="Acme", created_at=utc_now())
            session.add(head)
            session.flush()
            pending.regional_variant_of = head.id
        session.add(pending)
    if pending_state == "dismissed":
        dismiss_pending_discovery(durable_database, workspace_id, pending.id)
    service = _scan_service(durable_database)
    service.record_task_outcomes(
        workspace_id,
        scan_task,
        tuple(
            _outcome(
                candidate,
                (FunnelStage.PROPOSED, FunnelStage.REJECTED),
                FunnelReason.INSUFFICIENT_TITLE_EVIDENCE,
            )
            for candidate in (url, f"{url}/other")
        ),
        origin="source.scan",
        default_source="Acme",
    )

    [record] = service.list_scan_history(workspace_id).items

    assert record.screened_out_count == (1 if pending_state == "visible" else 2)


def test_scan_history_matches_pending_with_the_raw_outcome_url_fingerprint(
    durable_database: Database,
) -> None:
    workspace_id, scan_task, _continuation_task, _scan_id = _scan_with_continuation(
        durable_database
    )
    raw_url = "HTTPS://boards.greenhouse.io/acme/jobs/matching/"
    pending = pending_discovery(workspace_id, employer="Acme", created_at=utc_now())
    pending.fingerprint = url_sha256(raw_url)
    with durable_database.session() as session:
        session.add(pending)
        for raw, normalized in (
            (raw_url, "https://boards.greenhouse.io/acme/jobs/different-ledger-identity"),
            ("https://boards.greenhouse.io/acme/jobs/other", normalize_url(raw_url)),
        ):
            session.add(
                SourceScanOutcome(
                    workspace_id=workspace_id,
                    task_id=scan_task,
                    source_name="Acme",
                    candidate_kind="board_listing",
                    url=raw,
                    normalized_url=normalized,
                    terminal_stage="rejected",
                    reason="insufficient_title_evidence",
                    created_by="worker",
                    origin="test",
                )
            )

    [record] = _scan_service(durable_database).list_scan_history(workspace_id).items

    assert record.screened_out_count == 1


def test_a_continuation_still_dedupes_its_own_rows_after_a_same_day_rescan(
    durable_database: Database,
) -> None:
    workspace_id, scan_task, continuation_task, scan_id = _scan_with_continuation(durable_database)
    queue = TaskQueueService(durable_database)
    queue.request_cancellation(scan_task)
    rescan = str(
        queue.enqueue(
            workspace_id=workspace_id,
            task_type="source.scan",
            payload={},
            idempotency_key="funnel-ledger-rescan",
        ).id
    )
    with durable_database.session() as session:
        member = session.scalars(
            select(SourceScanMember).where(SourceScanMember.root_task_id == rescan)
        ).one()
        continuation = session.scalars(
            select(SourceScanContinuation).where(SourceScanContinuation.source_scan_id == scan_id)
        ).one()
        continuation.correlation_id = rescan
        continuation.generation = 2
        continuation.batch_member_id = member.id
        member.source_scan_id = scan_id
        member.continuation_id = continuation.id
        member.generation = 2
        member.state = "continuing"
        session.execute(
            update(SourceScan).where(SourceScan.id == scan_id).values(correlation_id=rescan)
        )
    service = _scan_service(durable_database)
    url = "https://boards.greenhouse.io/acme/jobs/repeated"
    for _ in range(2):
        service.record_task_outcomes(
            workspace_id,
            continuation_task,
            (_outcome(url, _STAGED_PATH, FunnelReason.STAGED),),
            origin="source.continue",
            default_source="Acme",
        )

    with durable_database.session() as session:
        stored = session.scalar(
            select(func.count()).select_from(SourceScanOutcome).where(SourceScanOutcome.url == url)
        )
    assert stored == 1


def test_a_continuation_files_its_outcome_against_the_scan_it_continues(
    durable_database: Database,
) -> None:
    workspace_id, _scan_task, continuation_task, scan_id = _scan_with_continuation(durable_database)

    _scan_service(durable_database).record_task_outcomes(
        workspace_id,
        continuation_task,
        (
            _outcome(
                "https://boards.greenhouse.io/acme/jobs/deferred",
                _STAGED_PATH,
                FunnelReason.STAGED,
            ),
        ),
        origin="source.continue",
        default_source="Acme",
    )

    with durable_database.session() as session:
        stored = session.scalars(select(SourceScanOutcome)).one()
        assert stored.source_scan_id == scan_id


def _counted_family_resolutions(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the scan task each ledger family resolution starts from."""
    resolved_for: list[str] = []

    def counted(session: Session, workspace: str, scan_task_id: str) -> list[str]:
        resolved_for.append(scan_task_id)
        return continuation_task_ids(session, workspace, scan_task_id)

    # The scan service resolves the family through its own module attribute.
    monkeypatch.setattr(board_scan, "continuation_task_ids", counted)
    return resolved_for


def test_interleaved_recording_passes_each_resolve_their_ledger_family_once(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two slots recording on one service in turn keep each other's memoized family."""
    workspace_id, scan_task, continuation_task, scan_id = _scan_with_continuation(durable_database)
    service = _scan_service(durable_database)
    resolved_for = _counted_family_resolutions(monkeypatch)

    for index in range(2):
        for task, origin in ((scan_task, "source.scan"), (continuation_task, "source.continue")):
            service.record_task_outcomes(
                workspace_id,
                task,
                (
                    _outcome(
                        f"https://boards.greenhouse.io/acme/jobs/shared-{index}",
                        _STAGED_PATH,
                        FunnelReason.STAGED,
                    ),
                ),
                origin=origin,
                default_source="Acme",
            )

    assert resolved_for == [scan_task, scan_task]
    with durable_database.session() as session:
        stored = session.execute(
            select(SourceScanOutcome.url, SourceScanOutcome.source_scan_id).order_by(
                SourceScanOutcome.url
            )
        ).all()
    assert [tuple(row) for row in stored] == [
        ("https://boards.greenhouse.io/acme/jobs/shared-0", scan_id),
        ("https://boards.greenhouse.io/acme/jobs/shared-1", scan_id),
    ]


def test_a_recording_pass_boundary_leaves_another_tasks_memoized_family(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id, scan_task, continuation_task, _scan_id = _scan_with_continuation(durable_database)
    service = _scan_service(durable_database)
    resolved_for = _counted_family_resolutions(monkeypatch)

    def record(index: int) -> None:
        service.record_task_outcomes(
            workspace_id,
            scan_task,
            (
                _outcome(
                    f"https://boards.greenhouse.io/acme/jobs/scan-{index}",
                    _STAGED_PATH,
                    FunnelReason.STAGED,
                ),
            ),
            origin="source.scan",
            default_source="Acme",
        )

    record(1)
    service.begin_recording_pass(continuation_task)
    record(2)

    assert resolved_for == [scan_task]


def test_a_new_recording_pass_resolves_the_family_its_continuations_joined(
    durable_database: Database,
) -> None:
    """A new pass sees a chained continuation that joined after its first recording."""
    workspace_id, scan_task, continuation_task, scan_id = _scan_with_continuation(durable_database)
    board = "https://boards.greenhouse.io/acme"
    shared_url = f"{board}/jobs/payroll-2"
    fresh_url = f"{board}/jobs/payroll-3"
    service = _scan_service(durable_database)
    service.record_task_outcomes(
        workspace_id,
        scan_task,
        (_outcome(f"{board}/jobs/1", _STAGED_PATH, FunnelReason.STAGED),),
        origin="source.scan",
        default_source="Acme",
    )
    with durable_database.session() as session:
        continuation_id = session.scalar(
            select(SourceScanContinuation.id).where(
                SourceScanContinuation.source_scan_id == scan_id
            )
        )
    chained_task = str(
        TaskQueueService(durable_database)
        .enqueue(
            workspace_id=workspace_id,
            task_type="source.continue",
            payload={"continuation_id": continuation_id, "generation": 1},
            idempotency_key="scan-ledger-chained-continue",
            correlation_id=continuation_task,
        )
        .id
    )
    _scan_service(durable_database).record_task_outcomes(
        workspace_id,
        chained_task,
        (_outcome(shared_url, _STAGED_PATH, FunnelReason.STAGED),),
        origin="source.continue",
        default_source="Acme",
    )

    service.begin_recording_pass(scan_task)
    service.record_task_outcomes(
        workspace_id,
        scan_task,
        (
            _outcome(shared_url, _STAGED_PATH, FunnelReason.STAGED),
            _outcome(fresh_url, _STAGED_PATH, FunnelReason.STAGED),
        ),
        origin="source.scan",
        default_source="Acme",
    )

    with durable_database.session() as session:
        recorded = session.scalar(
            select(func.count())
            .select_from(SourceScanOutcome)
            .where(SourceScanOutcome.url == shared_url)
        )
        fresh = session.scalars(
            select(SourceScanOutcome).where(SourceScanOutcome.url == fresh_url)
        ).one()
    assert recorded == 1
    assert fresh.task_id == scan_task


class _PagedBoard:
    """Defer several publisher pages from the scan, then list one posting per page read."""

    def __init__(
        self,
        targets: tuple[SourceContinuationTarget, ...],
        pages: dict[str, SourceListing],
    ) -> None:
        self.targets = targets
        self.pages = pages

    def listings(
        self,
        _source_name: str,
        _url: str,
        *,
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        """Answer the scan with deferred pages and no listings of its own."""
        return SourceRead(
            listings=[],
            complete=False,
            raw_count=0,
            outcome=SourceReadOutcome.CRAWL_CONTINUATION,
            deferred_targets=self.targets,
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
        """Read the first target of the chunk, leaving the rest of the chunk unattempted."""
        return SourceRead(
            listings=[self.pages[targets[0].url]],
            complete=True,
            raw_count=1,
            outcome=SourceReadOutcome.ATS_COMPLETE,
            attempted_target_urls=(targets[0].url,),
        )


def test_a_retried_continuation_pass_resolves_the_family_its_siblings_joined(
    durable_database: Database,
) -> None:
    """A retry keeps the continuation's task id, so it must not reuse its own earlier family."""
    workspace_id = scan_workspace(durable_database)
    board = "https://boards.greenhouse.io/acme"
    board_source(durable_database, workspace_id, "Acme", board)
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    shared_url = f"{board}/jobs/payroll-3"
    pages = {
        f"{board}/jobs?page=2": source_listing("Payroll Specialist", f"{board}/jobs/payroll-2"),
        f"{board}/jobs?page=3": source_listing("Payroll Specialist", shared_url),
    }
    reader = _PagedBoard(
        tuple(
            SourceContinuationTarget(
                url=url,
                label="Published roles",
                label_source="visible",
                source_url=board,
                structural_context="main",
                relations=("publisher-content", "substantive-label"),
            )
            for url in pages
        ),
        pages,
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
        fetcher=reader,
    )
    queue = TaskQueueService(durable_database)
    worker_id = "continuation-retry-ledger-worker"

    def claim() -> tuple[_WorkerTaskContext, Any]:
        claimed = queue.claim_next(worker_id=worker_id)
        assert claimed is not None
        task_id = str(claimed.id)
        keeper = _LeaseKeeper(queue, task_id, worker_id, 20.0)
        return _WorkerTaskContext(queue, task_id, worker_id, keeper), claimed.payload

    queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={},
        idempotency_key="continuation-retry-ledger-scan",
    )
    scan_context, _scan_payload = claim()
    service.scan(workspace_id, scan_context)
    handler = SourceContinuationHandler(
        durable_database,
        reader,
        service.sweep,
        lambda ws, task_id, outcomes: service.record_task_outcomes(
            ws, task_id, outcomes, origin="source.continue"
        ),
        service.begin_recording_pass,
    )
    context, payload = claim()

    handler(context, payload)

    with durable_database.session() as session:
        sibling_task = session.scalar(
            select(BackgroundTask.id).where(
                BackgroundTask.task_type == "source.continue",
                BackgroundTask.id != context.task_id,
            )
        )
    assert sibling_task is not None
    # The sibling continuation runs in its own worker, so it leaves this service's memo alone.
    _scan_service(durable_database).record_task_outcomes(
        workspace_id,
        sibling_task,
        (_outcome(shared_url, _STAGED_PATH, FunnelReason.STAGED),),
        origin="source.continue",
        default_source="Acme",
    )

    handler(context, payload)

    with durable_database.session() as session:
        recorded = session.scalar(
            select(func.count())
            .select_from(SourceScanOutcome)
            .where(SourceScanOutcome.url == shared_url)
        )
    assert recorded == 1


def test_bulk_staging_refusal_outcome_states_the_seniority_band_and_location_fit() -> None:
    """SENIORITY_OUTSIDE covers above-band too, and one wording fits every location refusal."""
    _, seniority_detail = bulk_staging_refusal_outcome(BulkStagingRefusal.SENIORITY_OUTSIDE_BAND)
    assert "outside the configured seniority band" in seniority_detail

    _, location_detail = bulk_staging_refusal_outcome(
        BulkStagingRefusal.LOCATION_OUTSIDE_SAVED_PLACES
    )
    assert "does not fit the saved places or eligibility" in location_detail


def test_bulk_staging_refusal_outcome_defaults_a_none_refusal_to_insufficient_evidence() -> None:
    """A blank title never reaches a screening finding, so None needs its own default."""
    reason, _ = bulk_staging_refusal_outcome(None)
    assert reason is FunnelReason.INSUFFICIENT_TITLE_EVIDENCE


def test_an_enabled_source_with_no_address_is_recorded_as_an_unreadable_board(
    durable_database: Database,
) -> None:
    """A source the sweep cannot dispatch is filed with its own reason, not dropped."""
    workspace_id = scan_workspace(durable_database)
    board_source(durable_database, workspace_id, "Acme", "")
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={},
        idempotency_key="scan-no-readable-board",
    )
    worker = Worker(
        durable_database,
        worker_id="no-readable-board-worker",
        handlers=default_handlers(durable_database, fetcher=ScriptedBoards({})),
    )

    assert worker.run_once()

    finished = queue.get(str(task.id))
    assert finished.status == "completed", (finished.error_category, finished.error_message)
    assert finished.progress["funnel"]["reason_counts"] == {"no_readable_board": 1}


def _scan_task_on_day(
    database: Database, workspace_id: str, source_id: str, day: int, *, complete: bool
) -> tuple[str, str]:
    """Seed one board scan `day` days out and the task it records under; return both ids."""
    task_id = str(
        TaskQueueService(database)
        .enqueue(
            workspace_id=workspace_id,
            task_type="source.scan",
            payload={},
            idempotency_key=f"retention-{day}",
        )
        .id
    )
    scan_id = new_id()
    when = utc_now() + timedelta(days=day)
    with database.session() as session:
        session.add(
            SourceScan(
                id=scan_id,
                source_id=source_id,
                scanned_on=when.date(),
                query_label="title-filter",
                status="completed" if complete else "incomplete",
                read_outcome=SourceReadOutcome.PUBLISHER_INDEX,
                raw_count=0,
                read_count=0,
                skip_reasons={},
                result_count=0,
                new_count=0,
                notes="",
                complete=complete,
                scan_started_at=when,
                scan_completed_at=when,
                duration_ms=0,
                correlation_id=task_id,
            )
        )
    return task_id, scan_id


def test_a_repeat_outcome_outside_the_kept_scans_is_pruned_when_its_url_is_seen_again(
    durable_database: Database,
) -> None:
    """A URL keeps its first and latest sightings; kept scans keep every row."""
    workspace_id = scan_workspace(durable_database)
    board = "https://boards.greenhouse.io/acme/jobs"
    source_id = board_source(durable_database, workspace_id, "Acme", board)
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    service = _scan_service(durable_database)
    rejected = (FunnelStage.PROPOSED, FunnelStage.REJECTED)
    sightings = {
        (-3, True): ("a", "b", "c"),
        (-2, True): ("a", "b"),
        (-1, False): ("a", "b"),
        (0, False): ("a",),
    }
    scan_of: dict[int, str] = {}
    for (day, complete), names in sightings.items():
        task_id, scan_of[day] = _scan_task_on_day(
            durable_database, workspace_id, source_id, day, complete=complete
        )
        service.record_task_outcomes(
            workspace_id,
            task_id,
            [
                _outcome(f"{board}/{name}", rejected, FunnelReason.INSUFFICIENT_TITLE_EVIDENCE)
                for name in names
            ],
            origin="source.scan",
            default_source="Acme",
        )

    with durable_database.session() as session:
        kept = set(
            session.execute(
                select(SourceScanOutcome.url, SourceScanOutcome.source_scan_id)
            ).tuples()
        )
    expected = {
        ("a", -3),
        ("a", -2),
        ("a", 0),
        ("b", -3),
        ("b", -2),
        ("b", -1),
        ("c", -3),
    }
    assert kept == {(f"{board}/{name}", scan_of[day]) for name, day in expected}


def test_the_funnel_reason_vocabulary_is_the_closed_set_the_ledger_stores() -> None:
    """Every stored outcome names one of these, so a member cannot quietly leave the set."""
    assert set(FunnelReason) == {
        FunnelReason.STAGED,
        FunnelReason.ALREADY_KNOWN,
        FunnelReason.INCOMPLETE_POSTING,
        FunnelReason.DUPLICATE_URL,
        FunnelReason.DUPLICATE_LEAD,
        FunnelReason.EXCLUDED_EMPLOYER,
        FunnelReason.HARD_CONSTRAINT_EXCLUDED,
        FunnelReason.INSUFFICIENT_TITLE_EVIDENCE,
        FunnelReason.SENIORITY_OUTSIDE_BAND,
        FunnelReason.LOCATION_OUTSIDE_SAVED_PLACES,
        FunnelReason.STAGE_LIMIT_REACHED,
        FunnelReason.ROLE_FAMILY_REVIEW_LIMIT_REACHED,
        FunnelReason.NO_TITLE_CONFIGURED,
        FunnelReason.NO_OPEN_POSITIONS,
        FunnelReason.NO_MATCHING_LISTINGS,
        FunnelReason.NO_NEW_OPENINGS,
        FunnelReason.FETCH_REFUSED,
        FunnelReason.FETCH_FAILED,
        FunnelReason.NO_READABLE_BOARD,
    }
