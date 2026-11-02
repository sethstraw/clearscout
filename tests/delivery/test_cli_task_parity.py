"""CLI and HTTP parity for durable paid workflow requests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from tailorcv.application.tasks import TaskQueueService
from tailorcv.application.workspaces import WorkspaceService
from tailorcv.bootstrap.api import create_app
from tailorcv.bootstrap.cli import app
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import Employer, JobPosting
from tests.support.artifacts import runtime_storage

runner = CliRunner()


def test_cli_and_http_queue_the_same_tailoring_workflow(
    durable_database: Database, durable_database_url: str, tmp_path: Path
) -> None:
    workspace_id = str(
        WorkspaceService(durable_database, runtime_storage(durable_database))
        .create(slug="parity", name="Parity")
        .id
    )
    with durable_database.session() as session:
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
        job_id = str(job.id)

    result = runner.invoke(
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
            durable_database_url,
        ],
    )

    assert result.exit_code == 0, result.output
    # The HTTP entry point refuses to queue work no worker can run; the CLI does not.
    TaskQueueService(durable_database).renew_worker_presence(
        worker_id="parity-worker", ttl_seconds=3600
    )
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
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
