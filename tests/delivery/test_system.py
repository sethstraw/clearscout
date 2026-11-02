"""System routes: health reporting and serving a version that changed under the server."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tailorcv.application.tasks import TaskQueueService
from tailorcv.bootstrap.api import create_app
from tailorcv.infrastructure.persistence.database import Database


def test_health_is_read_only_when_database_is_missing(tmp_path: Path) -> None:
    database_path = tmp_path / "missing.sqlite3"
    with TestClient(
        create_app(f"sqlite+pysqlite:///{database_path}"), base_url="http://localhost"
    ) as client:
        response = client.get("/api/v1/health", headers={"X-Correlation-ID": "health-1"})
        assert response.status_code == 200
        assert response.json()["status"] == "degraded"
        assert response.json()["database_exists"] is False
        assert response.headers["x-correlation-id"] == "health-1"
        assert response.headers["cache-control"] == "no-store"
    assert not database_path.exists()


def test_health_reports_whether_a_worker_is_available(
    durable_database_url: str, tmp_path: Path
) -> None:
    database = Database(durable_database_url)
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"),
            base_url="http://localhost",
        ) as client:
            idle = client.get("/api/v1/health").json()
            assert idle["worker_available"] is False
            assert idle["status"] == "degraded"
            TaskQueueService(database).renew_worker_presence(worker_id="worker-a", ttl_seconds=3600)
            running = client.get("/api/v1/health").json()
            assert running["worker_available"] is True
            assert running["status"] == "ok"
    finally:
        database.dispose()


def test_an_upgrade_while_the_server_runs_does_not_need_a_restart(tmp_path: Path) -> None:
    """The first-run order is serve, then discover the schema needs upgrading."""
    from tailorcv.infrastructure.persistence.migration import upgrade

    database_url = f"sqlite+pysqlite:///{tmp_path / 'later.sqlite3'}"
    with TestClient(create_app(database_url), base_url="http://localhost") as client:
        before = client.get("/api/v1/health")
        assert before.json()["schema_current"] is False
        assert client.get("/api/v1/workspaces").status_code == 503

        upgrade(database_url)

        after = client.get("/api/v1/health")
        assert after.json()["schema_current"] is True
        assert client.get("/api/v1/workspaces").status_code == 200
