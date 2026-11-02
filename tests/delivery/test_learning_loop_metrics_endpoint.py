"""The learning-loop-metrics endpoint returns the same snapshot the service computes."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from tailorcv.application.workspaces import WorkspaceService
from tailorcv.bootstrap.api import create_app
from tailorcv.domain import SearchProposalState, new_id
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import MasterCvProposal
from tests.support.artifacts import runtime_storage


def test_endpoint_returns_the_workspace_snapshot(durable_database_url: str, tmp_path: Path) -> None:
    database = Database(durable_database_url)
    try:
        workspace = WorkspaceService(database, runtime_storage(database)).create(
            slug="metrics", name="Metrics"
        )
        workspace_id = str(workspace.id)
        base = datetime(2026, 5, 1, tzinfo=UTC)
        with database.session() as session:
            session.add(
                MasterCvProposal(
                    id=new_id(),
                    workspace_id=workspace_id,
                    kind="add_missing",
                    requirement_key="req-endpoint",
                    requirement_text="X",
                    posting_count=3,
                    first_seen_at=base,
                    last_seen_at=base,
                    proposed_at=base,
                    state=SearchProposalState.ACCEPTED.value,
                    decided_at=base,
                )
            )
            session.commit()
    finally:
        database.dispose()

    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
    ) as client:
        response = client.get(
            f"/api/v1/workspaces/{workspace_id}/learning-loop-metrics",
        )

    assert response.status_code == 200
    body = response.json()
    assert body["workspace_id"] == workspace_id
    assert body["top_n"] == 10
    assert body["master_cv_proposal_acceptance"] == {"numerator": 1, "denominator": 1}
    signal_keys = {
        "reproposed_rejected_identities",
        "master_cv_proposal_acceptance",
        "pending_queue_missed_matches",
    }
    assert set(body) == {"workspace_id", "computed_at", "top_n", *signal_keys}
    for key in signal_keys - {"master_cv_proposal_acceptance"}:
        assert body[key] == {"numerator": 0, "denominator": 0}


def test_endpoint_returns_404_for_an_unknown_workspace(
    durable_database_url: str, tmp_path: Path
) -> None:
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
    ) as client:
        response = client.get("/api/v1/workspaces/does-not-exist/learning-loop-metrics")

    assert response.status_code == 404
