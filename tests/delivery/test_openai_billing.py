"""The billing routes: the OpenAI actual-spend read and the price refresh.

No test here reaches a network: the billing client takes a getter seam that
answers from a fixture, and the reconciliation and refresh routes are handed a
fake billing source and a scripted price source. Every key value is obviously fake.
"""

from __future__ import annotations

import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clearscout.application.config import Settings
from clearscout.application.workspaces import WorkspaceService
from clearscout.application.workspaces.spend import record_calls
from clearscout.application.workspaces.spend_reconciliation import SpendReconciliationService
from clearscout.bootstrap.api import create_app
from clearscout.delivery.api.dependencies import provide_openai_billing, provide_price_catalog
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.providers.billing import (
    BilledCost,
    OpenAIBillingClient,
    OpenAIBillingError,
)
from clearscout.infrastructure.providers.credentials import (
    OPENAI_ADMIN_ENV,
    find_admin_key,
)
from clearscout.shared.costs import CallManifest, Ledger, ModelPrice, Usage
from tests.support.artifacts import runtime_storage
from tests.support.doubles import MemoryStore, ScriptedPriceCatalog


def _costs_page(value: float, *, next_page: str | None = None) -> dict[str, Any]:
    """Return one /organization/costs page shaped like the documented response."""
    return {
        "object": "page",
        "data": [
            {
                "object": "bucket",
                "start_time": 1730419200,
                "end_time": 1730505600,
                "results": [
                    {
                        "object": "organization.costs.result",
                        "amount": {"value": value, "currency": "usd"},
                        "line_item": None,
                        "project_id": None,
                    }
                ],
            }
        ],
        "has_more": next_page is not None,
        "next_page": next_page,
    }


class FakeBilling:
    """A billing source that answers with a fixed cost, so tests spend nothing."""

    def __init__(self, amount: Decimal, currency: str = "USD") -> None:
        self.amount = amount
        self.currency = currency
        self.windows: list[tuple[int, int]] = []

    def costs(self, start_time: int, end_time: int) -> BilledCost:
        self.windows.append((start_time, end_time))
        return BilledCost(
            start_time=start_time,
            end_time=end_time,
            amount=self.amount,
            currency=self.currency,
        )


def test_the_billing_client_sums_the_documented_costs_response() -> None:
    client = OpenAIBillingClient(
        "admin-key-not-real", getter=lambda path, params: _costs_page(0.06)
    )

    billed = client.costs(1730419200, 1730505600)

    assert billed.amount == Decimal("0.06")
    assert billed.currency == "USD"


def test_the_billing_client_pages_until_the_cursor_runs_out() -> None:
    pages = {None: _costs_page(1.25, next_page="cursor-2"), "cursor-2": _costs_page(0.75)}
    seen: list[dict[str, Any]] = []

    def getter(path: str, params: dict[str, Any]) -> dict[str, Any]:
        seen.append(params)
        return pages[params.get("page")]

    billed = OpenAIBillingClient("admin-key-not-real", getter=getter).costs(1, 2)

    # Both daily buckets are summed, and the second call carried the cursor.
    assert billed.amount == Decimal("2.00")
    assert seen[1]["page"] == "cursor-2"


def test_a_rejected_admin_key_is_reported_without_leaking_the_key() -> None:
    from openai import APIStatusError

    def getter(path: str, params: dict[str, Any]) -> dict[str, Any]:
        raise APIStatusError(
            "forbidden",
            response=httpx.Response(403, request=httpx.Request("GET", "https://api.openai.com")),
            body=None,
        )

    client = OpenAIBillingClient("admin-key-not-real", getter=getter)
    with pytest.raises(OpenAIBillingError, match="api.usage.read") as caught:
        client.costs(1, 2)
    assert "admin-key-not-real" not in str(caught.value)


def test_find_admin_key_prefers_flag_then_environment_then_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(OPENAI_ADMIN_ENV, raising=False)

    class Store:
        def admin_key(self) -> str | None:
            return "from-store"

    assert find_admin_key("from-flag", store=Store()) == "from-flag"
    assert find_admin_key(store=Store()) == "from-store"
    monkeypatch.setenv(OPENAI_ADMIN_ENV, "from-env")
    assert find_admin_key(store=Store()) == "from-env"


def test_find_admin_key_is_none_when_nothing_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(OPENAI_ADMIN_ENV, raising=False)

    class Empty:
        def admin_key(self) -> str | None:
            return None

    assert find_admin_key(store=Empty()) is None


def _seed_openai_call(
    database: Database, tmp_path: Path, *, slug: str, price: ModelPrice | None, input_tokens: int
) -> None:
    """Record one OpenAI model call so its cost lands in the window under test."""
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database))
        .create(slug=slug, name=slug.title(), root_path=str(tmp_path))
        .id
    )
    prices = {"gpt-5.6-terra": price} if price is not None else {}
    ledger = Ledger(prices=prices)
    ledger.record(CallManifest("match", "gpt-5.6-terra", Usage(input_tokens=input_tokens)))
    with database.session() as session:
        record_calls(
            session,
            workspace_id=workspace_id,
            task_id=None,
            ledger=ledger,
            provider="openai",
            origin="test",
        )


def test_reconciliation_reports_actual_beside_recorded_and_the_drift(
    durable_database: Database, tmp_path: Path
) -> None:
    # A dollar-per-input-token price makes the recorded total exactly the tokens.
    dollar_per_token = ModelPrice(input=1_000_000.0, output=0.0)
    _seed_openai_call(durable_database, tmp_path, slug="a", price=dollar_per_token, input_tokens=2)
    _seed_openai_call(durable_database, tmp_path, slug="b", price=dollar_per_token, input_tokens=3)
    now = int(time.time())
    service = SpendReconciliationService(durable_database, FakeBilling(Decimal("6.50")))

    report = service.reconcile(now - 3600, now + 3600)

    assert report.available is True
    assert report.recorded_usd == pytest.approx(5.0)
    assert report.recorded_call_count == 2
    assert report.unpriced_call_count == 0
    assert report.openai_actual_usd == pytest.approx(6.5)
    assert report.drift_usd == pytest.approx(1.5)
    assert "organization-level" in report.scope_note


def test_reconciliation_withholds_the_recorded_total_when_a_call_is_unpriced(
    durable_database: Database, tmp_path: Path
) -> None:
    _seed_openai_call(durable_database, tmp_path, slug="c", price=None, input_tokens=4)
    now = int(time.time())
    service = SpendReconciliationService(durable_database, FakeBilling(Decimal("2.00")))

    report = service.reconcile(now - 3600, now + 3600)

    # An unpriced call has no honest total, so recorded and drift are blank.
    assert report.recorded_usd is None
    assert report.unpriced_call_count == 1
    assert report.drift_usd is None
    assert report.openai_actual_usd == pytest.approx(2.0)


def test_reconciliation_is_unavailable_when_no_admin_key_is_configured(
    durable_database: Database,
) -> None:
    now = int(time.time())
    report = SpendReconciliationService(durable_database, None).reconcile(now - 3600, now + 3600)

    assert report.available is False
    assert OPENAI_ADMIN_ENV in (report.unavailable_reason or "")
    assert report.openai_actual_usd is None


def _app(database_url: str) -> FastAPI:
    """Build the API over a migrated database and a memory key store."""
    return create_app(database_url, key_store=MemoryStore(), settings=Settings())


def test_the_route_returns_the_reconciliation_report(durable_database_url: str) -> None:
    app = _app(durable_database_url)
    app.dependency_overrides[provide_openai_billing] = lambda: FakeBilling(Decimal("9.99"))
    with TestClient(app, base_url="http://localhost") as client:
        response = client.post(
            "/api/v1/billing/openai/reconciliation",
            json={"start_time": 1730419200, "end_time": 1730505600},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["openai_actual_usd"] == pytest.approx(9.99)
    assert body["recorded_call_count"] == 0
    assert "organization-level" in body["scope_note"]


def test_the_route_reports_unavailable_when_no_admin_key_is_configured(
    durable_database_url: str,
) -> None:
    app = _app(durable_database_url)
    app.dependency_overrides[provide_openai_billing] = lambda: None
    with TestClient(app, base_url="http://localhost") as client:
        response = client.post(
            "/api/v1/billing/openai/reconciliation",
            json={"start_time": 1730419200, "end_time": 1730505600},
        )

    assert response.status_code == 200
    assert response.json()["available"] is False


def test_the_price_refresh_route_reports_the_moves_and_the_snapshot_in_force(
    durable_database_url: str, refreshable_prices: None
) -> None:
    app = _app(durable_database_url)
    app.dependency_overrides[provide_price_catalog] = lambda: ScriptedPriceCatalog(
        rates={"gpt-5.6-terra": 9.0}
    )
    with TestClient(app, base_url="http://localhost") as client:
        response = client.post("/api/v1/billing/prices/refresh")

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["snapshot"] != body["previous_snapshot"]
    assert [change["model"] for change in body["changed"]] == ["gpt-5.6-terra"]
    assert body["changed"][0]["input_usd_per_million"] == pytest.approx(9.0)


def test_the_price_refresh_route_reports_a_source_it_could_not_read(
    durable_database_url: str, refreshable_prices: None
) -> None:
    app = _app(durable_database_url)
    app.dependency_overrides[provide_price_catalog] = lambda: ScriptedPriceCatalog(
        error=TimeoutError("connect timed out")
    )
    with TestClient(app, base_url="http://localhost") as client:
        response = client.post("/api/v1/billing/prices/refresh")

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert "could not be read" in body["unavailable_reason"]
    assert body["changed"] == []
    assert body["snapshot"] == body["previous_snapshot"]


def test_the_route_refuses_a_backwards_window(durable_database_url: str) -> None:
    with TestClient(_app(durable_database_url), base_url="http://localhost") as client:
        response = client.post(
            "/api/v1/billing/openai/reconciliation",
            json={"start_time": 100, "end_time": 100},
        )

    assert response.status_code == 422
