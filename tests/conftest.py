"""Shared fixtures. No test in this suite touches a network or a live API."""

from __future__ import annotations

import os
import socket
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck
from hypothesis import settings as hypothesis_settings

from clearscout.application.config import Settings
from clearscout.application.master_cv.cv import CvInventory, parse_cv
from clearscout.application.tailoring.guardrails import Guardrails
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.providers.cache import ResponseCache
from clearscout.infrastructure.providers.client import Client
from clearscout.infrastructure.providers.credentials import (
    ENVIRONMENT_VARIABLES,
    OPENAI_ADMIN_ENV,
    Credential,
    Source,
)
from clearscout.infrastructure.web_fetch.policy import PoliteHttpClient
from clearscout.shared.costs import Ledger
from tests.support.database import build_template, clone
from tests.support.doubles import ScriptedProvider
from tests.support.suite import FIXTURES, SUITE_PROVIDER

# Hypothesis: fast during dev iteration, thorough under HYPOTHESIS_PROFILE=ci.
hypothesis_settings.register_profile("fast", max_examples=10)
hypothesis_settings.register_profile(
    "ci",
    max_examples=100,
    suppress_health_check=[HealthCheck.too_slow],
)
hypothesis_settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "fast"))


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config: pytest.Config) -> None:
    """Run in one process when capture is off, because `-s` and workers disagree."""
    if config.option.capture == "no":
        config.option.numprocesses = 0


def _seal(patch: pytest.MonkeyPatch, documents: Path) -> None:
    """Cut the caller's scope off from the machine the suite happens to run on.

    Every field of `Settings` is settable from the environment, including nested
    ones, so without this the suite passes or fails according to whose laptop it
    is running on.
    """
    for name in list(os.environ):
        if name.startswith("CLEARSCOUT_"):
            patch.delenv(name, raising=False)
    # Read from the one mapping that knows them, so a third vendor cannot leave a
    # hole in the seal.
    for variable in (*ENVIRONMENT_VARIABLES.values(), OPENAI_ADMIN_ENV):
        patch.delenv(variable, raising=False)
    patch.setattr("keyring.get_password", lambda service, name: None)
    patch.setattr("keyring.set_password", lambda service, name, key: None)
    patch.setattr("keyring.delete_password", lambda service, name: None)
    # Without a data root, storage resolves to the real Documents workspace.
    patch.setattr(
        "clearscout.infrastructure.persistence.database.user_documents_path",
        lambda: documents,
    )

    # The seal covers the network, or a test naming a board URL would fetch it.
    # Loopback stays open for TestClient; anything else refuses, naming what tried.
    _connect = socket.socket.connect

    def _refuse(self: socket.socket, address: Any, *args: Any, **kwargs: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else address
        if host in {"127.0.0.1", "::1", "localhost"}:
            return _connect(self, address, *args, **kwargs)
        raise RuntimeError(
            f"A test tried to open a network connection to {host!r}. "
            "Tests never touch a live provider, keyring, job board, or renderer; "
            "inject a scripted fetcher instead."
        )

    patch.setattr(socket.socket, "connect", _refuse)


@pytest.fixture(scope="session", autouse=True)
def _sealed_session(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Seal the process before a module or session fixture opens databases or clients."""
    with pytest.MonkeyPatch.context() as patch:
        _seal(patch, tmp_path_factory.mktemp("sealed-session-documents"))
        yield


@pytest.fixture(autouse=True)
def _sealed_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Reseal per test, so one test's documents root is not another's."""
    _seal(monkeypatch, tmp_path / "sealed-documents")
    # Host pacing is process-wide, so a test starts with no earlier request to any host.
    monkeypatch.setattr(PoliteHttpClient, "_host_started_at", {})


@pytest.fixture
def master_cv() -> Path:
    """Return the synthetic master CV used by most tests."""
    return FIXTURES / "master_cv.md"


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Return one database at head, built once per session by the real migrations."""
    return build_template(tmp_path_factory.mktemp("template") / "template.sqlite3")


@pytest.fixture
def durable_database_url(tmp_path: Path, migrated_template: Path) -> str:
    """Return a private SQLite database at head, cloned from the session template."""
    return clone(migrated_template, tmp_path / "durable.sqlite3")


@pytest.fixture
def durable_database(durable_database_url: str) -> Iterator[Database]:
    """Return a migrated database and release its connection pool afterward."""
    database = Database(durable_database_url)
    yield database
    database.dispose()


@pytest.fixture
def refreshable_prices(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Let one test refresh the price table without repricing another test's calls."""
    from genai_prices.data_snapshot import set_custom_snapshot

    from clearscout.shared.costs import prices

    monkeypatch.setattr(prices, "PRICES", dict(prices.PRICES))
    monkeypatch.setattr(prices, "_in_force", replace(prices._in_force))
    yield
    set_custom_snapshot(None)


@pytest.fixture
def inventory(master_cv: Path) -> CvInventory:
    """Return the parsed synthetic master CV."""
    return parse_cv(master_cv)


@pytest.fixture
def settings(master_cv: Path, tmp_path: Path, guardrails: Guardrails) -> Settings:
    """Return settings pointing at the synthetic CV and a temporary output directory."""
    return Settings(cv=master_cv, output_dir=tmp_path / "runs", pages=2, rules=guardrails)


@pytest.fixture
def guardrails() -> Guardrails:
    """Return rules with one of each kind in force."""
    return Guardrails(
        protected=["Staff Systems Engineer - Platform"],
        required=["jane.doe@example.com"],
        forbidden=["Project Halyard"],
        work_authorization="Authorized to work in the US | No sponsorship required",
        distinctions=["Kafka is operational usage, not cluster administration."],
    )


@pytest.fixture
def credential() -> Credential:
    """Return a credential that no test should ever send anywhere."""
    return Credential("test-key", SUITE_PROVIDER, Source.ENVIRONMENT)


@pytest.fixture
def client_factory(
    settings: Settings, tmp_path: Path
) -> Iterator[Callable[[dict[str, Any]], Client]]:
    """Return a builder for a Client backed by a scripted provider and a temp cache.

    Each cache holds a sqlite connection, so they are closed on teardown.
    """
    opened: list[ResponseCache] = []

    def build(script: dict[str, Any]) -> Client:
        cache = ResponseCache(enabled=False, directory=tmp_path / "cache")
        opened.append(cache)
        return Client(
            provider=ScriptedProvider(script),
            settings=settings,
            cache=cache,
            ledger=Ledger(),
        )

    yield build
    for cache in opened:
        cache.close()
