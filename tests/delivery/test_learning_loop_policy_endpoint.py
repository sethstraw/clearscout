"""The learning-loop policy endpoint exposes versioned workspace controls."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tailorcv.application.workspaces import WorkspaceService
from tailorcv.bootstrap.api import create_app
from tailorcv.infrastructure.persistence.database import Database
from tests.support.artifacts import runtime_storage


def test_endpoint_enables_paid_dispute_refresh(durable_database_url: str, tmp_path: Path) -> None:
    database = Database(durable_database_url)
    try:
        workspace = WorkspaceService(database, runtime_storage(database)).create(
            slug="policy", name="Policy"
        )
        workspace_id = str(workspace.id)
    finally:
        database.dispose()

    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
    ) as client:
        current = client.get(f"/api/v1/workspaces/{workspace_id}/learning-loop-policy")
        assert current.status_code == 200
        body = current.json()
        assert body["version"] == 0
        assert body["policy"]["enable_dispute_reextraction"] is False
        assert body["policy"]["enable_dispute_rematch"] is False

        policy = {**body["policy"], "enable_dispute_rematch": True}
        saved = client.put(
            f"/api/v1/workspaces/{workspace_id}/learning-loop-policy",
            json={"expected_version": body["version"], "policy": policy},
        )

    assert saved.status_code == 200
    saved_body = saved.json()
    assert saved_body["version"] == 1
    assert saved_body["policy"]["enable_dispute_reextraction"] is False
    assert saved_body["policy"]["enable_dispute_rematch"] is True
