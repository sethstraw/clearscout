"""Task queue routes: request validation, status mapping, and response shape."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tailorcv.application.config import Settings
from tailorcv.application.tasks import UNPAID, TaskQueueService, task_types
from tailorcv.application.workspaces import WorkspaceService
from tailorcv.bootstrap.api import create_app
from tailorcv.delivery.api.routers import tasks as tasks_router
from tailorcv.domain import TaskStatus, utc_now
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import PendingDiscovery
from tests.support.artifacts import runtime_storage
from tests.support.doubles import MemoryStore
from tests.support.scenarios import board_source, scan_workspace, state_target_titles
from tests.support.suite import SUITE_PROVIDER


def test_evaluation_preflight_is_available_without_a_worker_or_provider_key(
    durable_database_url: str, tmp_path: Path
) -> None:
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
    ) as client:
        workspace = client.post(
            "/api/v1/workspaces", json={"slug": "preflight", "name": "Preflight"}
        ).json()["id"]
        response = client.post(
            f"/api/v1/workspaces/{workspace}/evaluation-preflight",
            json={"job_posting_ids": ["missing-job"]},
        )
        tasks = client.get("/api/v1/tasks", params={"workspace_id": workspace}).json()
        schema = client.get("/api/v1/openapi.json")

    assert response.status_code == 200
    result = response.json()
    assert result["advisory"] is True
    assert result["currency"] == "USD"
    assert result["expected_cost_usd"] is None
    assert result["items"][0]["expected_unavailable_reason"]
    assert tasks == []
    assert schema.status_code == 200
    assert "/api/v1/workspaces/{workspace_id}/evaluation-preflight" in schema.json()["paths"]


def test_evaluation_preflight_rejects_a_selection_without_targets(
    durable_database_url: str, tmp_path: Path
) -> None:
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
    ) as client:
        response = client.post("/api/v1/workspaces/missing/evaluation-preflight", json={})
    assert response.status_code == 422


def test_resuming_a_cancelled_source_batch_creates_new_work_and_preserves_the_old_task(
    durable_database_url: str, tmp_path: Path
) -> None:
    database = Database(durable_database_url)
    workspace_id = scan_workspace(database)
    state_target_titles(database, workspace_id, "data")
    source_id = board_source(database, workspace_id, "Acme", "https://example.test/jobs")
    queue = TaskQueueService(database)
    queue.renew_worker_presence(worker_id="worker-a")
    original = queue.enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={"source_ids": [source_id]},
        idempotency_key="scan-original",
    )
    queue.request_cancellation(str(original.id), workspace_id=workspace_id)
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
    ) as client:
        response = client.post(
            f"/api/v1/tasks/{original.id}/resume",
            params={"workspace_id": workspace_id},
            headers={"Idempotency-Key": "resume-once"},
        )
        replay = client.post(
            f"/api/v1/tasks/{original.id}/resume",
            params={"workspace_id": workspace_id},
            headers={"Idempotency-Key": "resume-once"},
        )
        unavailable = client.post(
            "/api/v1/tasks/unknown/resume",
            params={"workspace_id": workspace_id},
            headers={"Idempotency-Key": "missing-batch"},
        )
    assert response.status_code == 202
    assert response.json()["id"] != str(original.id)
    assert response.json()["task_type"] == "source.scan"
    assert response.json()["status"] == "queued"
    assert response.json()["source_resume_from_task_id"] == str(original.id)
    assert response.json()["source_ids"] == [source_id]
    assert "payload" not in response.json()
    assert "result" not in response.json()
    assert replay.status_code == 202
    assert replay.json()["id"] == response.json()["id"]
    assert unavailable.status_code == 404
    assert queue.get(str(original.id)).status == TaskStatus.CANCELLED
    assert queue.get(str(original.id)).source_resume_from_task_id is None
    database.dispose()


def test_queueing_is_refused_while_no_worker_is_running(
    durable_database_url: str, tmp_path: Path
) -> None:
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
    ) as client:
        workspace = client.post("/api/v1/workspaces", json={"slug": "idle", "name": "Idle"})
        assert workspace.status_code == 201
        refused = client.post(
            "/api/v1/tasks",
            json={
                "workspace_id": workspace.json()["id"],
                "task_type": "artifact.verify",
                "payload": {"artifact_id": "one"},
            },
            headers={"Idempotency-Key": "idle-verify"},
        )
        listed = client.get("/api/v1/tasks", params={"workspace_id": workspace.json()["id"]}).json()

    assert refused.status_code == 503
    assert refused.json()["detail"] == (
        "The worker is not running. Start it with 'tailorcv worker' and queue the work again."
    )
    assert listed == []


def test_a_paid_task_without_a_provider_key_is_refused_as_free_mode(
    durable_database_url: str, tmp_path: Path
) -> None:
    database = Database(durable_database_url)
    workspace_id = scan_workspace(database)
    TaskQueueService(database).renew_worker_presence(worker_id="worker-a")
    database.dispose()
    with TestClient(
        create_app(
            durable_database_url,
            tmp_path / "artifacts",
            key_store=MemoryStore(),
            settings=Settings(provider=SUITE_PROVIDER),
        ),
        base_url="http://localhost",
    ) as client:
        response = client.post(
            "/api/v1/tasks",
            json={
                "workspace_id": workspace_id,
                "task_type": "workflow.tailor",
                "payload": {"job_posting_id": "synthetic-posting"},
            },
            headers={"Idempotency-Key": "free-mode-tailor"},
        )
        listed = client.get("/api/v1/tasks", params={"workspace_id": workspace_id}).json()
        documented = client.get("/api/v1/openapi.json").json()

    assert response.status_code == 409
    body = response.json()
    assert body["type"] == "urn:tailorcv:problem:free-mode"
    assert "\n" not in body["detail"]
    assert body["hint"] == "Add an API key in Settings > Provider."
    assert listed == []
    assert "409" in documented["paths"]["/api/v1/tasks"]["post"]["responses"]


@pytest.mark.parametrize("task_type", ["posting.refresh", "discovery.evaluate"])
def test_an_archived_job_is_explained_without_queueing_work(
    durable_database_url: str, tmp_path: Path, task_type: str
) -> None:
    from tailorcv.infrastructure.persistence.models import JobPosting
    from tests.support.scenarios import tracked_board_posting

    database = Database(durable_database_url)
    workspace_id = scan_workspace(database)
    source_id = board_source(database, workspace_id, "Acme", "https://example.test/jobs")
    job_id = tracked_board_posting(
        database,
        workspace_id,
        source_id,
        title="Data Platform Lead",
        url="https://example.test/jobs/1",
    )
    with database.session() as session:
        job = session.get(JobPosting, job_id)
        assert job is not None
        job.archived_at = utc_now()
    queue = TaskQueueService(database)
    queue.renew_worker_presence(worker_id="worker-a")
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
            json={
                "workspace_id": workspace_id,
                "task_type": task_type,
                "payload": {"job_posting_id": job_id},
            },
            headers={"Idempotency-Key": "archived-refusal"},
        )
        listed = client.get("/api/v1/tasks", params={"workspace_id": workspace_id})
    assert response.status_code == 409
    assert response.json()["detail"] == (
        "This job is archived. Choose an active tracked job before queueing work."
    )
    assert listed.status_code == 200 and listed.json() == []
    with database.session() as session:
        retained = session.get(JobPosting, job_id)
        assert retained is not None and retained.archived_at is not None
    database.dispose()


def test_the_task_type_catalog_is_served_without_a_workspace(
    durable_database_url: str, tmp_path: Path
) -> None:
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"),
        base_url="http://localhost",
    ) as client:
        # The literal segment has to beat /tasks/{task_id}, which would otherwise
        # answer 404 for a task called "types".
        response = client.get("/api/v1/tasks/types", headers={"X-Correlation-ID": "types-1"})
        assert response.status_code == 200
        served = {entry["task_type"]: entry for entry in response.json()}
        assert tuple(served) == task_types()
        assert served["search.guided_campaign"]["paid"] is True
        assert "web-content-retrieval charge" in served["search.guided_campaign"]["cost"]
        assert served["posting.refresh"]["paid"] is False
        assert served["posting.refresh"]["cost"] == UNPAID


def test_a_finished_task_streams_a_backlog_larger_than_one_page(
    durable_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bounded read plus a terminal check could end a stream mid-history.

    The stream stops when the task reaches a terminal status, so a page-sized
    read has to keep going while a full page comes back.
    """
    monkeypatch.setattr(tasks_router, "EVENT_PAGE", 2)
    database = Database(durable_database_url)
    try:
        workspace = WorkspaceService(database, runtime_storage(database)).create(
            slug="stream", name="Stream", root_path=str(tmp_path / "workspace")
        )
        workspace_id = str(workspace.id)
        queue = TaskQueueService(database)
        task = queue.enqueue(
            workspace_id=workspace_id,
            task_type="artifact.verify",
            payload={"artifact_id": "one"},
            idempotency_key="verify-one",
        )
        queue.claim_next(worker_id="worker-a", lease_seconds=10)
        queue.heartbeat(str(task.id), worker_id="worker-a", progress={"step": "reading"})
        queue.finish(str(task.id), worker_id="worker-a", status=TaskStatus.COMPLETED)
    finally:
        database.dispose()

    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"),
        base_url="http://localhost",
    ) as client:
        streamed = client.get(
            f"/api/v1/tasks/{task.id}/events", params={"workspace_id": workspace_id}
        )

    assert streamed.status_code == 200
    for expected in ("task.queued", "task.started", "task.progress", "task.completed"):
        assert f"event: {expected}" in streamed.text


def test_api_task_lifecycle(durable_database_url: str, tmp_path: Path) -> None:
    database = Database(durable_database_url)
    try:
        TaskQueueService(database).renew_worker_presence(worker_id="worker-a", ttl_seconds=3600)
    finally:
        database.dispose()
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"),
        base_url="http://localhost",
    ) as client:
        workspace = client.post("/api/v1/workspaces", json={"slug": "tasks", "name": "Tasks"})
        assert workspace.status_code == 201
        workspace_id = workspace.json()["id"]

        task = client.post(
            "/api/v1/tasks",
            json={
                "workspace_id": workspace_id,
                "task_type": "artifact.verify",
                "payload": {"artifact_id": "missing"},
            },
            headers={"Idempotency-Key": "task-1"},
        )
        assert task.status_code == 202
        task_id = task.json()["id"]
        assert (
            client.post(
                f"/api/v1/tasks/{task_id}/cancel",
                params={"workspace_id": workspace_id},
            ).json()["status"]
            == "cancelled"
        )
        assert (
            client.get(
                f"/api/v1/tasks/{task_id}", params={"workspace_id": "other-workspace"}
            ).status_code
            == 404
        )
        retried = client.post(
            f"/api/v1/tasks/{task_id}/retry", params={"workspace_id": workspace_id}
        )
        assert retried.status_code == 202
        assert retried.json()["status"] == "queued"
        unsupported = client.post(
            "/api/v1/tasks",
            json={"workspace_id": workspace_id, "task_type": "unknown", "payload": {}},
            headers={"Idempotency-Key": "unsupported"},
        )
        assert unsupported.status_code == 422
        events = client.get(
            f"/api/v1/tasks/{task_id}/events", params={"workspace_id": workspace_id}
        )
        assert events.status_code == 200
        assert "event: task.queued" in events.text
        assert "event: task.cancelled" in events.text

        openapi = client.get("/api/v1/openapi.json")
        assert openapi.status_code == 200
        assert "/api/v1/tasks/{task_id}/retry" in openapi.json()["paths"]


def test_api_preserves_the_completed_already_evaluated_outcome(
    durable_database_url: str, tmp_path: Path
) -> None:
    database = Database(durable_database_url)
    try:
        workspace = WorkspaceService(database, runtime_storage(database)).create(
            slug="already-evaluated",
            name="Already evaluated",
            root_path=str(tmp_path / "workspace"),
        )
        with database.session() as session:
            session.add(
                PendingDiscovery(
                    id="pending-race",
                    workspace_id=str(workspace.id),
                    fingerprint="0" * 64,
                    employer="Globex",
                    title="Data Platform Lead",
                    reason="Synthetic API task target",
                    first_seen_on=utc_now().date(),
                )
            )
        queue = TaskQueueService(database)
        task = queue.enqueue(
            workspace_id=str(workspace.id),
            task_type="discovery.evaluate",
            payload={"pending_discovery_id": "pending-race"},
            idempotency_key="evaluate-race",
        )
        queue.claim_next(worker_id="worker-a", lease_seconds=10)
        outcome = {
            "decision": "already_evaluated",
            "pending_discovery_id": "pending-race",
            "job_posting_id": "job-existing",
            "evaluation_id": "evaluation-existing",
            "existing_decision": "apply_now",
        }
        queue.finish(
            str(task.id),
            worker_id="worker-a",
            status=TaskStatus.COMPLETED,
            progress=outcome,
        )
    finally:
        database.dispose()

    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
    ) as client:
        response = client.get(
            f"/api/v1/tasks/{task.id}", params={"workspace_id": str(workspace.id)}
        )

    assert response.status_code == 200
    assert response.json()["progress"] == outcome


@pytest.mark.parametrize("idempotency_key", [None, "", "   "])
def test_task_post_without_a_usable_idempotency_key_names_the_header(
    idempotency_key: str | None, durable_database_url: str, tmp_path: Path
) -> None:
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
    ) as client:
        workspace = client.post("/api/v1/workspaces", json={"slug": "idem", "name": "Idem"})
        assert workspace.status_code == 201
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key is not None else {}
        response = client.post(
            "/api/v1/tasks",
            json={
                "workspace_id": workspace.json()["id"],
                "task_type": "search.guided_campaign",
                "payload": {},
            },
            headers=headers,
        )
    assert response.status_code == 400
    body = response.json()
    assert "Idempotency-Key" in body["detail"]
    assert "Idempotency-Key" in body["hint"]
    assert body["type"].endswith("missing-idempotency-key")


def test_paid_task_route_refuses_a_payload_carrying_a_spend_ceiling(
    durable_database_url: str, tmp_path: Path
) -> None:
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
    ) as client:
        workspace = client.post("/api/v1/workspaces", json={"slug": "caps", "name": "Caps"})
        workspace_id = workspace.json()["id"]
        payload: dict[str, Any] = {
            "job_posting_id": "synthetic-posting",
            "approved_cap_cents": 12,
        }
        response = client.post(
            "/api/v1/tasks",
            json={
                "workspace_id": workspace_id,
                "task_type": "workflow.tailor",
                "payload": payload,
            },
            headers={"Idempotency-Key": "invalid-cap"},
        )
        assert response.status_code == 422
        assert response.json()["type"] == "urn:tailorcv:problem:request-validation"
        assert client.get("/api/v1/tasks", params={"workspace_id": workspace_id}).json() == []
