"""Contract checks for the production task registry."""

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import func, select

from clearscout.application.failures import FreeModeError
from clearscout.application.tasks import TaskQueueService, task_types
from clearscout.bootstrap.api import create_app
from clearscout.delivery.api.models.tasks import TaskCreate
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import BackgroundTask, ModelCall
from clearscout.infrastructure.providers.credentials import find
from tests.support.doubles import MemoryStore
from tests.support.scenarios import board_source, scan_workspace, tracked_board_posting
from tests.support.suite import SUITE_PROVIDER


def test_openapi_task_response_exposes_the_production_vocabulary_as_an_enum(
    tmp_path: Path,
) -> None:
    schema = create_app(data_root=tmp_path).openapi()

    task_type_schema = schema["components"]["schemas"]["TaskResponse"]["properties"]["task_type"]
    assert task_type_schema["type"] == "string"
    assert task_type_schema["enum"] == list(task_types())


def test_a_paid_task_is_accepted_on_its_payload_alone() -> None:
    """Nothing about money is entered to enqueue a paid task."""
    request = TaskCreate.model_validate(
        {
            "workspace_id": "synthetic",
            "task_type": "workflow.tailor",
            "payload": {"job_posting_id": "synthetic-job"},
        }
    )

    assert request.payload == {
        "job_posting_id": "synthetic-job",
        "cover_letter": True,
        "regenerate": False,
        "approval_tailoring_run_id": None,
    }


def test_a_payload_carrying_a_spend_ceiling_is_refused() -> None:
    """The contract declares no spend ceiling, so a payload naming one is refused."""
    with pytest.raises(ValueError, match="Invalid workflow.tailor payload"):
        TaskCreate.model_validate(
            {
                "workspace_id": "synthetic",
                "task_type": "workflow.tailor",
                "payload": {"job_posting_id": "synthetic-job", "approved_cap_cents": 27},
            }
        )


def _free_mode_queue(database: Database, store: MemoryStore) -> TaskQueueService:
    return TaskQueueService(
        database, paid_work_ready=lambda: find(SUITE_PROVIDER, store=store) is not None
    )


def test_a_paid_task_is_refused_without_a_provider_key_and_nothing_is_stored(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    queue = _free_mode_queue(durable_database, MemoryStore())

    with pytest.raises(FreeModeError):
        queue.enqueue(
            workspace_id=workspace_id,
            task_type="workflow.tailor",
            payload={"job_posting_id": "synthetic-job"},
            idempotency_key="free-mode-tailor",
        )

    assert queue.list_tasks(workspace_id) == []
    with durable_database.session() as session:
        assert session.scalar(select(func.count()).select_from(BackgroundTask)) == 0
        assert session.scalar(select(func.count()).select_from(ModelCall)) == 0


def test_a_paid_task_is_queued_once_the_configured_provider_has_a_key(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    source_id = board_source(durable_database, workspace_id, "Acme", "https://example.test/jobs")
    job_id = tracked_board_posting(
        durable_database,
        workspace_id,
        source_id,
        title="Data Platform Lead",
        url="https://example.test/jobs/1",
    )
    queue = _free_mode_queue(durable_database, MemoryStore({SUITE_PROVIDER: "test-key-not-real"}))

    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="workflow.tailor",
        payload={"job_posting_id": job_id},
        idempotency_key="keyed-tailor",
    )

    assert task.status == "queued"
    assert [listed.id for listed in queue.list_tasks(workspace_id)] == [task.id]


def test_the_key_check_runs_before_the_write_lock_is_taken(durable_database: Database) -> None:
    workspace_id = scan_workspace(durable_database)
    path = durable_database.engine.url.database
    assert path is not None

    def write_lock_free() -> bool:
        probe = sqlite3.connect(path, timeout=0, isolation_level=None)
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.execute("ROLLBACK")
            return True
        except sqlite3.OperationalError:
            return False
        finally:
            probe.close()

    observed: list[bool] = []

    def ready() -> bool:
        observed.append(write_lock_free())
        return False

    with pytest.raises(FreeModeError):
        TaskQueueService(durable_database, paid_work_ready=ready).enqueue(
            workspace_id=workspace_id,
            task_type="workflow.tailor",
            payload={"job_posting_id": "synthetic-job"},
            idempotency_key="lock-order",
        )

    assert observed == [True]


def test_a_free_task_is_queued_without_a_provider_key(durable_database: Database) -> None:
    workspace_id = scan_workspace(durable_database)
    queue = _free_mode_queue(durable_database, MemoryStore())

    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="artifact.verify",
        payload={"artifact_id": "synthetic-artifact"},
        idempotency_key="free-verify",
    )

    assert task.status == "queued"


def test_a_source_scan_accepts_an_entire_large_selection() -> None:
    from clearscout.application.tasks.payloads import SourceScanPayload

    selected = [f"source-{index}" for index in range(301)]
    assert SourceScanPayload(source_ids=selected).source_ids == selected


@pytest.mark.parametrize("fresh", [False, True])
def test_a_source_scan_retains_only_an_explicit_fresh_details_request(fresh: bool) -> None:
    from clearscout.application.tasks import validate_task_payload
    from clearscout.application.tasks.payloads import SourceScanPayload

    payload = validate_task_payload("source.scan", {"fresh_details": fresh})

    assert SourceScanPayload.model_validate(payload).fresh_details is fresh
    assert payload == ({"source_ids": [], "fresh_details": True} if fresh else {"source_ids": []})


def test_an_ordinary_source_scan_omits_the_resume_selector_on_replay(
    durable_database: Database,
) -> None:
    from clearscout.application.tasks import TaskQueueService, validate_task_payload
    from tests.support.scenarios import scan_workspace, state_target_titles

    workspace_id = scan_workspace(durable_database)
    state_target_titles(durable_database, workspace_id, "Data Platform Lead")
    queue = TaskQueueService(durable_database)
    original = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": []},
        idempotency_key="ordinary-payload",
    )
    normalized = validate_task_payload("source.scan", {})
    assert (
        queue.enqueue(
            workspace_id=workspace_id,
            task_type="source.scan",
            payload=normalized,
            idempotency_key="ordinary-payload",
        ).id
        == original.id
    )
    assert normalized == {"source_ids": []}


def test_a_source_scan_requires_a_manually_saved_target_title(durable_database: Database) -> None:
    from clearscout.application.failures import ValidationError
    from clearscout.application.tasks import TaskQueueService
    from tests.support.scenarios import scan_workspace

    workspace_id = scan_workspace(durable_database)
    with pytest.raises(ValidationError, match="target title"):
        TaskQueueService(durable_database).enqueue(
            workspace_id=workspace_id,
            task_type="source.scan",
            payload={},
            idempotency_key="needs-titles",
        )


def test_overlapping_source_batches_report_busy_sources_and_keep_the_rest(
    durable_database: Database,
) -> None:
    from sqlalchemy import select

    from clearscout.application.tasks import TaskQueueService
    from clearscout.infrastructure.persistence.models.discovery import SourceScanMember
    from tests.support.scenarios import board_source, scan_workspace, state_target_titles

    workspace_id = scan_workspace(durable_database)
    state_target_titles(durable_database, workspace_id, "data")
    first = board_source(durable_database, workspace_id, "Acme", "https://boards.example.test/acme")
    second = board_source(
        durable_database, workspace_id, "Globex", "https://boards.example.test/globex"
    )
    queue = TaskQueueService(durable_database)
    queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [first]},
        idempotency_key="first",
    )
    batch = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [first, second]},
        idempotency_key="second",
    )
    with durable_database.session() as session:
        members = {
            row.source_id: row
            for row in session.scalars(
                select(SourceScanMember).where(SourceScanMember.root_task_id == str(batch.id))
            )
        }
    assert members[first].state == "already_in_progress"
    assert members[first].released_at is not None
    assert members[second].state == "queued"
    assert members[second].released_at is None


def test_a_source_batch_replay_keeps_its_payload_digest_and_selection(
    durable_database: Database,
) -> None:
    from sqlalchemy import select

    from clearscout.application.tasks import TaskQueueService
    from clearscout.application.tasks.records import payload_digest
    from clearscout.infrastructure.persistence.models import BackgroundTask
    from clearscout.infrastructure.persistence.models.discovery import SourceScanMember
    from tests.support.scenarios import board_source, scan_workspace, state_target_titles

    workspace_id = scan_workspace(durable_database)
    state_target_titles(durable_database, workspace_id, "data")
    first = board_source(durable_database, workspace_id, "Acme", "https://boards.example.test/acme")
    queue = TaskQueueService(durable_database)
    batch = queue.enqueue(
        workspace_id=workspace_id, task_type="source.scan", payload={}, idempotency_key="snapshot"
    )
    board_source(durable_database, workspace_id, "Globex", "https://boards.example.test/globex")
    replay = queue.enqueue(
        workspace_id=workspace_id, task_type="source.scan", payload={}, idempotency_key="snapshot"
    )
    assert replay.id == batch.id
    with durable_database.session() as session:
        task = session.get(BackgroundTask, str(batch.id))
        assert task is not None
        assert task.payload == {}
        assert task.payload_sha256 == payload_digest({})
        selected = list(
            session.scalars(
                select(SourceScanMember.source_id).where(
                    SourceScanMember.root_task_id == str(batch.id)
                )
            )
        )
    assert selected == [first]


@pytest.mark.parametrize("initial_source", [False, True])
def test_source_selection_survives_progress_updates(
    durable_database: Database, initial_source: bool
) -> None:
    from clearscout.application.tasks import TaskQueueService
    from tests.support.scenarios import board_source, scan_workspace, state_target_titles

    workspace_id = scan_workspace(durable_database)
    state_target_titles(durable_database, workspace_id, "data")
    selected = []
    if initial_source:
        selected.append(
            board_source(durable_database, workspace_id, "Acme", "https://boards.example.test/acme")
        )
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id, task_type="source.scan", payload={}, idempotency_key="progress"
    )
    claimed = queue.claim_next(worker_id="reader")
    assert claimed is not None and claimed.id == task.id
    queue.heartbeat(task.id, worker_id="reader", progress={"phase": "reading"})
    board_source(durable_database, workspace_id, "Globex", "https://boards.example.test/globex")

    batch_id, members = queue.commands.open_source_batch(workspace_id, None, str(task.id))

    assert batch_id == str(task.id)
    assert [member.source_id for member in members] == selected


def test_a_source_batch_refuses_a_source_outside_its_workspace(durable_database: Database) -> None:
    from clearscout.application.failures import NotFoundError
    from clearscout.application.tasks import TaskQueueService
    from tests.support.scenarios import scan_workspace, state_target_titles

    workspace_id = scan_workspace(durable_database)
    state_target_titles(durable_database, workspace_id, "data")
    with pytest.raises(NotFoundError, match="source"):
        TaskQueueService(durable_database).enqueue(
            workspace_id=workspace_id,
            task_type="source.scan",
            payload={"source_ids": ["unknown"]},
            idempotency_key="unknown",
        )


def test_resume_refuses_a_batch_that_is_still_in_progress_without_creating_a_task(
    durable_database: Database,
) -> None:
    from sqlalchemy import func, select

    from clearscout.application.failures import ConflictError
    from clearscout.application.tasks import TaskQueueService
    from clearscout.infrastructure.persistence.models import BackgroundTask
    from tests.support.scenarios import board_source, scan_workspace, state_target_titles

    workspace_id = scan_workspace(durable_database)
    state_target_titles(durable_database, workspace_id, "data")
    board_source(durable_database, workspace_id, "Acme", "https://boards.example.test/acme")
    queue = TaskQueueService(durable_database)
    original = queue.enqueue(
        workspace_id=workspace_id, task_type="source.scan", payload={}, idempotency_key="original"
    )
    with pytest.raises(ConflictError, match="already in progress"):
        queue.commands.resume_source_batch(workspace_id, str(original.id), "invalid-resume")
    with durable_database.session() as session:
        assert session.scalar(select(func.count()).select_from(BackgroundTask)) == 1


@pytest.mark.parametrize("alive", [True, None])
def test_recovery_keeps_a_source_reserved_when_its_reader_is_alive_or_unknown(
    durable_database: Database, alive: bool | None
) -> None:
    from sqlalchemy import select

    from clearscout.application.tasks import TaskQueueService
    from clearscout.infrastructure.persistence.models import SourceScanMember
    from tests.support.scenarios import board_source, scan_workspace, state_target_titles

    workspace_id = scan_workspace(durable_database)
    state_target_titles(durable_database, workspace_id, "data")
    source_id = board_source(
        durable_database, workspace_id, "Acme", "https://boards.example.test/acme"
    )
    queue = TaskQueueService(durable_database)
    original = queue.enqueue(
        workspace_id=workspace_id, task_type="source.scan", payload={}, idempotency_key="original"
    )
    claimed = queue.claim_next(worker_id="reader")
    assert claimed is not None
    with durable_database.session() as session:
        member = session.scalars(
            select(SourceScanMember).where(SourceScanMember.root_task_id == str(original.id))
        ).one()
    assert queue.commands.claim_source_read(
        member.id,
        str(original.id),
        claimed.attempt_count,
        process_id=12345,
        process_started_at="synthetic-start",
    )
    assert queue.commands.recover_dead_source_readers(lambda _pid, _start: alive) == 0
    another = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [source_id]},
        idempotency_key="another",
    )
    with durable_database.session() as session:
        other = session.scalars(
            select(SourceScanMember).where(SourceScanMember.root_task_id == str(another.id))
        ).one()
        assert other.state == "already_in_progress"


def test_a_confirmed_dead_reader_can_recover_without_releasing_a_live_generation(
    durable_database: Database,
) -> None:
    from sqlalchemy import select

    from clearscout.application.tasks import TaskQueueService
    from clearscout.infrastructure.persistence.models import SourceScanMember
    from tests.support.scenarios import board_source, scan_workspace, state_target_titles

    workspace_id = scan_workspace(durable_database)
    state_target_titles(durable_database, workspace_id, "data")
    board_source(durable_database, workspace_id, "Acme", "https://boards.example.test/acme")
    queue = TaskQueueService(durable_database)
    original = queue.enqueue(
        workspace_id=workspace_id, task_type="source.scan", payload={}, idempotency_key="original"
    )
    claimed = queue.claim_next(worker_id="reader")
    assert claimed is not None
    with durable_database.session() as session:
        member = session.scalars(
            select(SourceScanMember).where(SourceScanMember.root_task_id == str(original.id))
        ).one()
    assert queue.commands.claim_source_read(
        member.id,
        str(original.id),
        claimed.attempt_count,
        process_id=12345,
        process_started_at="synthetic-start",
    )
    assert queue.commands.recover_dead_source_readers(lambda _pid, _start: False) == 1
    with durable_database.session() as session:
        recovered = session.get(SourceScanMember, member.id)
        assert recovered is not None
        assert recovered.active_task_id is None
        assert recovered.released_at is None
    assert queue.commands.claim_source_read(
        member.id,
        str(original.id),
        claimed.attempt_count,
        process_id=54321,
        process_started_at="replacement-start",
    )
