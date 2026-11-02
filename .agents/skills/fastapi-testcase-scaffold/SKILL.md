---
name: fastapi-testcase-scaffold
description: Scaffold a pytest test for a TailorCV FastAPI route handler. Use for an explicit API test request or a change under src/tailorcv/delivery/api/. Do not use for non-HTTP unit tests.
---

# TailorCV FastAPI testcase scaffold

Route tests assert the observable HTTP outcome of a real request. Mock outbound
HTTP, never the database. Match the surrounding test file's style before
adopting a new one.

## Locate

Find the route handler under `src/tailorcv/delivery/api/`. Note its path, HTTP
method, request model, response model, and every collaborator it calls, such as
repositories and provider clients.

List delivery tests with `rg --files tests/delivery` and find the test module
that owns the route: most routers have one named after them, such as
`test_tasks.py`, `test_sources.py`, `test_jobs.py`, or `test_search.py`. Extend
that module. Read a nearby test and reuse its fixtures and the `tests/support/`
builders it imports instead of writing local ones.

## Build the app and replace collaborators

There is no client fixture. Each API test builds the app with
`tailorcv.bootstrap.api.create_app` over the `durable_database_url` fixture
from `tests/conftest.py`, a migrated SQLite database cloned per test, and opens
`fastapi.testclient.TestClient` as a context manager. A collaborator the route
receives through a dependency is replaced with `app.dependency_overrides`. This
pair is from `tests/delivery/test_openai_billing.py`, where `FakeBilling` is a
double defined in that module:

```python
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tailorcv.application.config import Settings
from tailorcv.bootstrap.api import create_app
from tailorcv.delivery.api.dependencies import provide_openai_billing
from tests.support.doubles import MemoryStore


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
    assert response.json()["openai_actual_usd"] == pytest.approx(9.99)


def test_the_route_refuses_a_backwards_window(durable_database_url: str) -> None:
    with TestClient(_app(durable_database_url), base_url="http://localhost") as client:
        response = client.post(
            "/api/v1/billing/openai/reconciliation",
            json={"start_time": 100, "end_time": 100},
        )

    assert response.status_code == 422
```

## Mock outbound HTTP with a transport

A provider or job-board call the route makes goes through `httpx`. Hand the code
an `httpx.Client` over `httpx.MockTransport`; for a provider,
`tests/support/fake_provider.py` supplies `FakeProviderAPI` as the handler and
`tests/support/provider_clients.py` wraps the client in the official SDK.
`tests/delivery/test_end_to_end.py` shows how a pipeline test swaps the provider
factory with `monkeypatch`. The suite's network seal refuses any real socket, so
a call that escapes the transport fails loudly.

Do not mock the database. Use `durable_database_url`, or `durable_database` when
the test also seeds or reads rows directly.

## Assert outcomes, not steps

Assert the response body, the headers that matter, and any observable side
effect: a row created, a task queued, a file written. Do not assert that an
internal function was called.

Never use `SimpleNamespace` or an ad hoc double for a request or response
payload. Such doubles pass even when the code drops a field they lack. Use the
real Pydantic model or a builder that constructs it.

## Cover one happy path and one adversarial input

The adversarial case is the smallest input that validation or an authorization
check should reject. Assert the exact status code and the shape of the error
body.

## Verify

```sh
just pytest-log <name> <test_file>
```

Read the log it names once. Report the test file, the fixtures reused, and the
outbound URLs mocked.
