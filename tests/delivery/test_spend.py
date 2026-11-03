"""Workspace spend routes: what the recorded ledger publishes over HTTP."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from clearscout.application.workspaces.spend import record_calls
from clearscout.bootstrap.api import create_app
from clearscout.infrastructure.persistence.database import Database
from clearscout.shared.costs import CallManifest, Ledger, ModelPrice, Usage
from clearscout.shared.costs.prices import snapshot_label


def test_spend_reports_only_what_was_recorded(
    durable_database_url: str, durable_database: Database, tmp_path: Path
) -> None:
    """Spend is a report of recorded calls, not an estimate.

    A workspace that has never called a model has spent nothing, and saying so
    is different from having no idea. The interesting case is the other one: a
    call whose model had no configured price is counted but withheld from the
    total, because a missing price is not a zero price.
    """
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"),
        base_url="http://localhost",
    ) as client:
        workspace = client.post("/api/v1/workspaces", json={"slug": "spend", "name": "Spend"})
        workspace_id = workspace.json()["id"]

        empty = client.get(f"/api/v1/workspaces/{workspace_id}/spend")
        assert empty.status_code == 200
        assert empty.json()["call_count"] == 0
        assert empty.json()["total_usd"] == 0.0

        priced_ledger = Ledger(prices={"a-model-2026-08-15": ModelPrice(input=250000, output=0)})
        priced_ledger.record(
            CallManifest(
                stage="match",
                model="a-model",
                served="a-model-2026-08-15",
                usage=Usage(
                    input_tokens=1,
                    output_tokens=2,
                    cache_read_tokens=3,
                    cache_write_tokens=4,
                    reasoning_tokens=1,
                ),
                milliseconds=250,
                finish="stop",
            )
        )
        unpriced_ledger = Ledger()
        unpriced_ledger.record(
            CallManifest(stage="draft", model="an-unpriced-model", usage=Usage(input_tokens=1))
        )
        with durable_database.session() as session:
            record_calls(
                session,
                workspace_id=workspace_id,
                task_id=None,
                ledger=priced_ledger,
                provider="configured",
                origin="test",
            )
            record_calls(
                session,
                workspace_id=workspace_id,
                task_id=None,
                ledger=unpriced_ledger,
                provider="configured",
                origin="test",
            )

        summary = client.get(f"/api/v1/workspaces/{workspace_id}/spend").json()

    assert summary["call_count"] == 2
    assert summary["unpriced_calls"] == 1
    # Withheld, not understated as 0.25.
    assert summary["total_usd"] is None

    by_stage = {item["name"]: item for item in summary["by_stage"]}
    assert by_stage["match"]["amount_usd"] == 2.0
    assert by_stage["draft"]["amount_usd"] is None
    assert by_stage["draft"]["unpriced_calls"] == 1
    assert {item["stage"] for item in summary["recent"]} == {"match", "draft"}
    match_call = next(item for item in summary["recent"] if item["stage"] == "match")
    assert match_call == {
        "id": match_call["id"],
        "stage": "match",
        "model": "a-model-2026-08-15",
        "provider": "configured",
        "requested_model": "a-model",
        "served_model": "a-model-2026-08-15",
        "amount_usd": 2.0,
        "pricing_source": "configured",
        "price_snapshot": snapshot_label(),
        "cache_hit": False,
        "input_tokens": 1,
        "output_tokens": 2,
        "cache_read_tokens": 3,
        "cache_write_tokens": 4,
        "reasoning_tokens": 1,
        "duration_ms": 250,
        "completion_reason": "stop",
        "created_at": match_call["created_at"],
    }
