"""CLI and HTTP parity for durable paid workflow requests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from typer.testing import CliRunner, Result

from tailorcv.application.tasks import TaskQueueService
from tailorcv.application.workspaces import WorkspaceService
from tailorcv.bootstrap.api import create_app
from tailorcv.bootstrap.cli import app
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import BackgroundTask, Employer, JobPosting
from tests.support.artifacts import runtime_storage
from tests.support.doubles import MemoryStore
from tests.support.suite import SUITE_KEY_VARIABLE, SUITE_PROVIDER

runner = CliRunner()


def _tracked_job(database: Database) -> tuple[str, str]:
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database))
        .create(slug="parity", name="Parity")
        .id
    )
    with database.session() as session:
        employer = Employer(workspace_id=workspace_id, name="Example", normalized_name="example")
        session.add(employer)
        session.flush()
        job = JobPosting(
            workspace_id=workspace_id,
            employer_id=employer.id,
            title="Platform Engineer",
            normalized_title="platform engineer",
            first_seen_at=datetime.now(UTC),
        )
        session.add(job)
        session.flush()
        return workspace_id, str(job.id)


def _queue_tailoring(workspace_id: str, job_id: str, database_url: str) -> Result:
    return runner.invoke(
        app,
        [
            "tasks",
            "tailor",
            "--workspace-id",
            workspace_id,
            "--job-posting-id",
            job_id,
            "--idempotency-key",
            "parity-tailor",
            "--database-url",
            database_url,
        ],
    )


def test_the_cli_refuses_paid_tailoring_without_a_provider_key(
    durable_database: Database, durable_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id, job_id = _tracked_job(durable_database)
    monkeypatch.setenv("TAILORCV_PROVIDER", SUITE_PROVIDER.value)

    result = _queue_tailoring(workspace_id, job_id, durable_database_url)

    assert result.exit_code != 0
    assert "No provider API key is set" in result.output
    with durable_database.session() as session:
        assert session.scalar(select(func.count()).select_from(BackgroundTask)) == 0


def test_cli_and_http_queue_the_same_tailoring_workflow(
    durable_database: Database,
    durable_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id, job_id = _tracked_job(durable_database)
    monkeypatch.setenv("TAILORCV_PROVIDER", SUITE_PROVIDER.value)
    monkeypatch.setenv(SUITE_KEY_VARIABLE, "test-key-not-real")

    result = _queue_tailoring(workspace_id, job_id, durable_database_url)

    assert result.exit_code == 0, result.output
    # Both entry points refuse paid work without a key; only HTTP also needs a running worker.
    TaskQueueService(durable_database).renew_worker_presence(
        worker_id="parity-worker", ttl_seconds=3600
    )
    with TestClient(
        create_app(
            durable_database_url,
            tmp_path / "artifacts",
            key_store=MemoryStore({SUITE_PROVIDER: "test-key-not-real"}),
        ),
        base_url="http://localhost",
    ) as client:
        response = client.post(
            "/api/v1/tasks",
            headers={"Idempotency-Key": "parity-tailor"},
            json={
                "workspace_id": workspace_id,
                "task_type": "workflow.tailor",
                "payload": {
                    "job_posting_id": job_id,
                    "cover_letter": True,
                    "regenerate": False,
                },
            },
        )

    assert response.status_code == 202
    task_id = response.json()["id"]
    assert task_id in result.output
    task = TaskQueueService(durable_database).get(task_id, workspace_id)
    assert (task.task_type, task.status, task.attempt_count) == ("workflow.tailor", "queued", 0)
