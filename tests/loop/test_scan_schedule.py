"""Workspace calendar claims and source selection without clock waits."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

import pytest
from sqlalchemy import event, select

from tailorcv.application.failures import ConflictError, NotFoundError, ValidationError
from tailorcv.application.tasks import TaskQueueService
from tailorcv.application.workspaces import SearchParametersService
from tailorcv.application.workspaces.models import ScanScheduleInput
from tailorcv.application.workspaces.scan_schedule import ScanScheduleService
from tailorcv.domain.search_parameters import JobSearchParameters
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import BackgroundTask, Employer, Source, Workspace
from tailorcv.infrastructure.persistence.models.discovery import SourceScanMember
from tests.support.scenarios import board_source, scan_workspace, state_target_titles


@pytest.fixture
def calendar(durable_database: Database):
    workspace_id = scan_workspace(durable_database)
    state_target_titles(durable_database, workspace_id, "Product Manager")
    source_id = board_source(
        durable_database, workspace_id, "Acme", "https://acme.example.test/careers"
    )
    clock = [datetime(2026, 1, 5, 8, tzinfo=UTC)]
    service = ScanScheduleService(durable_database, clock=lambda: clock[0])
    return service, workspace_id, source_id, clock


@pytest.mark.parametrize(
    ("now", "options", "expected"),
    [
        ("2026-01-05T08:00:00+00:00", {}, "2026-01-05T09:00:00+00:00"),
        ("2026-01-05T09:00:00+00:00", {}, "2026-01-06T09:00:00+00:00"),
        (
            "2026-01-05T09:00:00+00:00",
            {"frequency": "weekly", "weekday": 0},
            "2026-01-12T09:00:00+00:00",
        ),
        (
            "2026-03-08T06:00:00+00:00",
            {"timezone": "America/New_York", "local_time": "02:30"},
            "2026-03-08T07:30:00+00:00",
        ),
        (
            "2026-03-08T06:00:00+00:00",
            {
                "timezone": "America/New_York",
                "local_time": "02:30",
                "frequency": "weekly",
                "weekday": 6,
            },
            "2026-03-08T07:30:00+00:00",
        ),
        (
            "2026-11-01T04:00:00+00:00",
            {"timezone": "America/New_York", "local_time": "01:30"},
            "2026-11-01T05:30:00+00:00",
        ),
        (
            "2026-11-01T06:00:00+00:00",
            {
                "timezone": "America/New_York",
                "local_time": "01:30",
                "frequency": "weekly",
                "weekday": 6,
            },
            "2026-11-08T06:30:00+00:00",
        ),
        (
            "2011-12-29T20:00:00+00:00",
            {"timezone": "Pacific/Apia"},
            "2011-12-30T19:00:00+00:00",
        ),
    ],
)
def test_saved_calendar_uses_the_first_future_local_occurrence(calendar, now, options, expected):
    service, workspace_id, _, clock = calendar
    clock[0] = datetime.fromisoformat(now)
    document = ScanScheduleInput.model_validate(
        {"expected_revision": 0, "timezone": "UTC", **options}
    )

    saved = service.save(workspace_id, document)

    assert saved.state is not None
    assert saved.state.next_run_at == datetime.fromisoformat(expected)
    assert saved.state.revision == 1


def test_absent_calendar_is_opt_in_and_stale_save_preserves_the_saved_calendar(calendar):
    service, workspace_id, _, _ = calendar
    assert service.read(workspace_id).state is None
    assert service.tick() == []
    original = service.save(workspace_id, ScanScheduleInput(expected_revision=0, timezone="UTC"))
    with pytest.raises(ConflictError):
        service.save(
            workspace_id, ScanScheduleInput(expected_revision=0, timezone="UTC", local_time="10:00")
        )
    assert service.read(workspace_id) == original


@pytest.mark.parametrize("frequency", ["daily", "weekly"])
def test_midnight_crossing_gap_runs_and_returns_to_the_saved_calendar(calendar, frequency):
    service, workspace_id, _, clock = calendar
    clock[0] = datetime(2026, 3, 28, 20, tzinfo=UTC)
    saved = service.save(
        workspace_id,
        ScanScheduleInput(
            expected_revision=0,
            timezone="America/Nuuk",
            local_time="23:30",
            frequency=frequency,
            weekday=5,
        ),
    )
    assert saved.state is not None
    due = datetime(2026, 3, 29, 1, 30, tzinfo=UTC)
    assert saved.state.next_run_at == due
    clock[0] = due
    assert len(service.tick()) == 1
    state = service.read(workspace_id).state
    assert state is not None
    assert state.last_due_at == due
    assert state.next_run_at == datetime(
        2026,
        3 if frequency == "daily" else 4,
        30 if frequency == "daily" else 5,
        0,
        30,
        tzinfo=UTC,
    )


def test_startup_claims_one_missed_occurrence_and_advances_past_now(calendar, durable_database):
    service, workspace_id, source_id, clock = calendar
    service.save(workspace_id, ScanScheduleInput(expected_revision=0, timezone="UTC"))
    clock[0] = datetime(2026, 1, 9, 10, tzinfo=UTC)
    task_ids = service.tick()
    assert len(task_ids) == 1
    assert service.tick() == []
    state = service.read(workspace_id).state
    assert state is not None
    assert state.last_due_at == datetime(2026, 1, 5, 9, tzinfo=UTC)
    assert state.next_run_at == datetime(2026, 1, 10, 9, tzinfo=UTC)
    assert state.last_task_id == task_ids[0]
    with durable_database.session() as session:
        task = session.get(BackgroundTask, task_ids[0])
        assert task is not None
        assert task.task_type == "source.scan"
        assert task.payload == {"source_ids": []}
        assert task.idempotency_key == (f"scan_schedule:{workspace_id}:1:2026-01-05T09:00:00+00:00")
        member = session.scalar(
            select(SourceScanMember).where(SourceScanMember.root_task_id == task.id)
        )
        assert member is not None and member.source_id == source_id


def test_concurrent_ticks_claim_one_occurrence(calendar, durable_database):
    service, workspace_id, _, clock = calendar
    service.save(workspace_id, ScanScheduleInput(expected_revision=0, timezone="UTC"))
    clock[0] = datetime(2026, 1, 5, 10, tzinfo=UTC)
    ready = Barrier(2)

    def synchronize(_connection, _cursor, statement, _parameters, _context, _many):
        if statement == "BEGIN IMMEDIATE":
            ready.wait()

    event.listen(durable_database.engine, "before_cursor_execute", synchronize)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _: ScanScheduleService(durable_database, clock=lambda: clock[0]).tick(),
                    range(2),
                )
            )
    finally:
        event.remove(durable_database.engine, "before_cursor_execute", synchronize)
    assert sorted(len(result) for result in results) == [0, 1]
    assert len(TaskQueueService(durable_database).list_tasks(workspace_id)) == 1


def test_pause_and_resume_skip_elapsed_occurrences_but_preserve_history(calendar):
    service, workspace_id, _, clock = calendar
    service.save(workspace_id, ScanScheduleInput(expected_revision=0, timezone="UTC"))
    clock[0] = datetime(2026, 1, 5, 10, tzinfo=UTC)
    task_id = service.tick()[0]
    saved = service.save(
        workspace_id, ScanScheduleInput(expected_revision=1, timezone="UTC", paused=True)
    )
    assert saved.state is not None and saved.state.next_run_at is None
    clock[0] = datetime(2026, 1, 12, 10, tzinfo=UTC)
    assert ScanScheduleService(service.database, clock=lambda: clock[0]).tick() == []
    resumed = service.save(workspace_id, ScanScheduleInput(expected_revision=2, timezone="UTC"))
    assert resumed.state is not None
    assert resumed.state.next_run_at == datetime(2026, 1, 13, 9, tzinfo=UTC)
    assert resumed.state.last_task_id == task_id
    assert service.tick() == []


def test_backward_clock_does_not_replay_and_forward_clock_claims_once(calendar):
    service, workspace_id, _, clock = calendar
    service.save(workspace_id, ScanScheduleInput(expected_revision=0, timezone="UTC"))
    clock[0] = datetime(2026, 1, 5, 10, tzinfo=UTC)
    assert len(service.tick()) == 1
    clock[0] = datetime(2026, 1, 4, 10, tzinfo=UTC)
    assert service.tick() == []
    clock[0] = datetime(2026, 2, 1, 10, tzinfo=UTC)
    assert len(service.tick()) == 1
    assert service.tick() == []


def test_active_save_requires_manual_titles_but_paused_configuration_does_not(calendar):
    service, workspace_id, _, _ = calendar
    SearchParametersService(service.database).save(workspace_id, JobSearchParameters())
    with pytest.raises(ValidationError, match="target title"):
        service.save(workspace_id, ScanScheduleInput(expected_revision=0, timezone="UTC"))
    saved = service.save(
        workspace_id, ScanScheduleInput(expected_revision=0, timezone="UTC", paused=True)
    )
    assert saved.state is not None and saved.state.next_run_at is None


def test_missing_titles_at_due_time_are_visible_and_do_not_queue_paid_work(calendar):
    service, workspace_id, _, clock = calendar
    service.save(workspace_id, ScanScheduleInput(expected_revision=0, timezone="UTC"))
    SearchParametersService(service.database).save(workspace_id, JobSearchParameters())
    clock[0] = datetime(2026, 1, 5, 10, tzinfo=UTC)
    assert service.tick() == []
    state = service.read(workspace_id).state
    assert state is not None and state.last_error is not None
    assert "target title" in state.last_error
    assert state.last_due_at == datetime(2026, 1, 5, 9, tzinfo=UTC)
    assert state.next_run_at == datetime(2026, 1, 6, 9, tzinfo=UTC)
    assert TaskQueueService(service.database).list_tasks(workspace_id) == []


def test_saved_selection_includes_disabled_sources_but_rejects_other_workspaces(calendar):
    service, workspace_id, source_id, _ = calendar
    with service.database.session() as session:
        session.get(Source, source_id).enabled = False
    saved = service.save(
        workspace_id,
        ScanScheduleInput(
            expected_revision=0, timezone="UTC", scope="selected", source_ids=[source_id]
        ),
    )
    assert saved.unavailable_source_ids == []
    other = scan_workspace(service.database, slug="other")
    foreign = board_source(service.database, other, "Example", "https://example.test/careers")
    with pytest.raises(NotFoundError):
        service.save(
            workspace_id,
            ScanScheduleInput(
                expected_revision=1, timezone="UTC", scope="selected", source_ids=[foreign]
            ),
        )


def test_empty_saved_selection_cannot_become_all_enabled():
    with pytest.raises(ValueError, match="at least one source"):
        ScanScheduleInput(expected_revision=0, timezone="UTC", scope="selected")


@pytest.mark.parametrize("unavailable", ["source", "employer", "missing"])
def test_unavailable_selection_remains_visible_and_does_not_scan_other_sources(
    calendar, unavailable
):
    service, workspace_id, source_id, clock = calendar
    service.save(
        workspace_id,
        ScanScheduleInput(
            expected_revision=0, timezone="UTC", scope="selected", source_ids=[source_id]
        ),
    )
    board_source(service.database, workspace_id, "Example", "https://example.test/careers")
    with service.database.session() as session:
        source = session.get(Source, source_id)
        if unavailable == "source":
            source.archived_at = clock[0]
        elif unavailable == "employer":
            session.get(Employer, source.employer_id).archived_at = clock[0]
        else:
            session.delete(source)
    response = service.read(workspace_id)
    assert response.unavailable_source_ids == [source_id]
    assert response.state.configuration.source_ids == [source_id]
    clock[0] = datetime(2026, 1, 5, 10, tzinfo=UTC)
    assert service.tick() == []
    state = service.read(workspace_id).state
    assert state.last_error is not None
    assert state.next_run_at == datetime(2026, 1, 6, 9, tzinfo=UTC)
    assert TaskQueueService(service.database).list_tasks(workspace_id) == []


@pytest.mark.parametrize("unavailable", ["source", "employer", "missing"])
def test_unavailable_saved_selection_can_be_paused_without_changing_its_scope(
    calendar, unavailable
):
    service, workspace_id, source_id, clock = calendar
    saved = service.save(
        workspace_id,
        ScanScheduleInput(
            expected_revision=0, timezone="UTC", scope="selected", source_ids=[source_id]
        ),
    )
    with service.database.session() as session:
        source = session.get(Source, source_id)
        if unavailable == "source":
            source.archived_at = clock[0]
        elif unavailable == "employer":
            session.get(Employer, source.employer_id).archived_at = clock[0]
        else:
            session.delete(source)
    paused = service.save(
        workspace_id,
        ScanScheduleInput(**saved.state.configuration.model_dump(), expected_revision=1).model_copy(
            update={"paused": True}
        ),
    )
    assert paused.state.configuration.source_ids == [source_id]
    assert paused.state.configuration.paused
    assert paused.state.next_run_at is None
    assert paused.unavailable_source_ids == [source_id]
    with pytest.raises(NotFoundError):
        service.save(
            workspace_id,
            ScanScheduleInput(
                expected_revision=2,
                timezone="UTC",
                scope="selected",
                source_ids=[source_id, "unknown-source"],
                paused=True,
            ),
        )
    assert service.read(workspace_id) == paused


def test_due_selection_uses_only_remaining_active_sources_even_when_disabled(calendar):
    service, workspace_id, source_id, clock = calendar
    second = board_source(
        service.database, workspace_id, "Example", "https://example.test/careers", enabled=False
    )
    service.save(
        workspace_id,
        ScanScheduleInput(
            expected_revision=0, timezone="UTC", scope="selected", source_ids=[source_id, second]
        ),
    )
    with service.database.session() as session:
        session.get(Source, source_id).archived_at = clock[0]
    clock[0] = datetime(2026, 1, 5, 10, tzinfo=UTC)
    task_id = service.tick()[0]
    with service.database.session() as session:
        assert session.get(BackgroundTask, task_id).payload == {"source_ids": [second]}
    assert service.read(workspace_id).state.configuration.source_ids == [source_id, second]


def test_all_enabled_scope_resolves_current_sources_at_due_time(calendar):
    service, workspace_id, source_id, clock = calendar
    service.save(workspace_id, ScanScheduleInput(expected_revision=0, timezone="UTC"))
    second = board_source(service.database, workspace_id, "Example", "https://example.test/careers")
    with service.database.session() as session:
        session.get(Source, source_id).enabled = False
    clock[0] = datetime(2026, 1, 5, 10, tzinfo=UTC)
    task_id = service.tick()[0]
    with service.database.session() as session:
        members = session.scalars(
            select(SourceScanMember).where(SourceScanMember.root_task_id == task_id)
        ).all()
        assert [member.source_id for member in members] == [second]


def test_all_enabled_without_eligible_sources_records_a_failed_occurrence(calendar):
    service, workspace_id, source_id, clock = calendar
    service.save(workspace_id, ScanScheduleInput(expected_revision=0, timezone="UTC"))
    with service.database.session() as session:
        session.get(Source, source_id).enabled = False
    clock[0] = datetime(2026, 1, 5, 10, tzinfo=UTC)
    assert service.tick() == []
    state = service.read(workspace_id).state
    assert state.last_error is not None
    assert state.next_run_at == datetime(2026, 1, 6, 9, tzinfo=UTC)


def test_busy_source_is_skipped_while_another_selected_source_is_admitted(calendar):
    service, workspace_id, source_id, clock = calendar
    second = board_source(service.database, workspace_id, "Example", "https://example.test/careers")
    TaskQueueService(service.database).enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [source_id]},
        idempotency_key="manual",
    )
    service.save(
        workspace_id,
        ScanScheduleInput(
            expected_revision=0, timezone="UTC", scope="selected", source_ids=[source_id, second]
        ),
    )
    clock[0] = datetime(2026, 1, 5, 10, tzinfo=UTC)
    task_id = service.tick()[0]
    with service.database.session() as session:
        members = session.scalars(
            select(SourceScanMember).where(SourceScanMember.root_task_id == task_id)
        ).all()
        assert {member.source_id: member.state for member in members} == {
            source_id: "already_in_progress",
            second: "queued",
        }


def test_enqueue_failure_rolls_back_task_and_due_claim_together(calendar):
    service, workspace_id, _, clock = calendar
    service.save(workspace_id, ScanScheduleInput(expected_revision=0, timezone="UTC"))
    original = service.read(workspace_id)
    clock[0] = datetime(2026, 1, 5, 10, tzinfo=UTC)

    class FailingQueue(TaskQueueService):
        def enqueue_in_session(self, session, **kwargs):
            super().enqueue_in_session(session, **kwargs)
            raise RuntimeError("synthetic queue failure")

    failing = ScanScheduleService(
        service.database, queue=FailingQueue(service.database), clock=lambda: clock[0]
    )
    with pytest.raises(RuntimeError, match="synthetic queue failure"):
        failing.tick()
    assert service.read(workspace_id) == original
    assert TaskQueueService(service.database).list_tasks(workspace_id) == []


def test_archived_workspace_is_not_scheduled_or_read(calendar):
    service, workspace_id, _, clock = calendar
    service.save(workspace_id, ScanScheduleInput(expected_revision=0, timezone="UTC"))
    with service.database.session() as session:
        session.get(Workspace, workspace_id).archived_at = clock[0]
    clock[0] = datetime(2026, 1, 5, 10, tzinfo=UTC)
    assert service.tick() == []
    with pytest.raises(NotFoundError):
        service.read(workspace_id)


def test_naive_clock_is_rejected_before_saving(calendar):
    service, workspace_id, _, clock = calendar
    clock[0] = datetime(2026, 1, 5, 8)
    with pytest.raises(ValidationError, match="timezone"):
        service.save(workspace_id, ScanScheduleInput(expected_revision=0, timezone="UTC"))
