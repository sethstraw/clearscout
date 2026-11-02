"""The provider screen reports where a key is, and never what it is.

Every key value in this file is obviously fake, because a test that pastes
something key-shaped teaches the next reader to do the same.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tailorcv.application.config import DEFAULT_MODELS, ProviderName, Settings
from tailorcv.bootstrap.api import create_app
from tailorcv.infrastructure.providers.credentials import ENVIRONMENT_VARIABLES, KeyStore
from tailorcv.shared.errors import AuthError
from tests.support.doubles import MemoryStore
from tests.support.suite import SUITE_KEY_VARIABLE, SUITE_PROVIDER

FAKE_KEY = "test-key-not-real"


class BrokenKeyring:
    """A store that cannot write, the way a headless machine cannot.

    ``KeyringStore`` raises ``AuthError`` from ``set`` and swallows everything
    else, so this double reproduces both halves of that behavior.
    """

    def __init__(self, held: str | None = None) -> None:
        self.held = held

    def get(self, provider: ProviderName) -> str | None:
        del provider
        return self.held

    def set(self, provider: ProviderName, key: str) -> None:
        del provider, key
        raise AuthError(
            "Could not save the key to your system keyring: no recommended backend.\n"
            f"Set {SUITE_KEY_VARIABLE} in your shell instead."
        )

    def forget(self, provider: ProviderName) -> None:
        """Fail the way the real store fails: quietly."""
        del provider


def _client(tmp_path: Path, store: KeyStore, settings: Settings | None = None) -> TestClient:
    """Build a client over a database that was never created.

    The provider screen has to work during onboarding, which is before there is
    a workspace or a migrated database to scope it to.
    """
    return TestClient(
        create_app(
            f"sqlite+pysqlite:///{tmp_path / 'unused.sqlite3'}",
            key_store=store,
            settings=settings or Settings(),
        ),
        base_url="http://localhost",
    )


def test_no_key_anywhere_is_reported_as_absent(tmp_path: Path) -> None:
    with _client(tmp_path, MemoryStore()) as client:
        response = client.get("/api/v1/providers")
        assert response.status_code == 200
        listed = response.json()
        assert [item["provider"] for item in listed] == [item.value for item in ProviderName]
        for item in listed:
            provider = ProviderName(item["provider"])
            assert item["key_present"] is False
            assert item["key_source"] is None
            assert item["stored_in_keyring"] is False
            assert item["environment_variable"] == ENVIRONMENT_VARIABLES[provider]
            assert [tier["tier"] for tier in item["tiers"]] == ["extraction", "drafting"]
            assert [tier["model"] for tier in item["tiers"]] == [
                DEFAULT_MODELS[provider].extraction.model,
                DEFAULT_MODELS[provider].drafting.model,
            ]
            assert item["tiers"][1]["effort"] == DEFAULT_MODELS[provider].drafting.effort


def test_a_stored_key_is_present_and_never_returned(tmp_path: Path) -> None:
    store = MemoryStore()
    with _client(tmp_path, store) as client:
        stored = client.put(
            f"/api/v1/providers/{SUITE_PROVIDER.value}/key",
            json={"key": FAKE_KEY},
        )
        assert stored.status_code == 200
        assert stored.json()["key_present"] is True
        assert stored.json()["stored_in_keyring"] is True
        assert stored.json()["key_source"] == "system keyring"
        assert store.keys == {SUITE_PROVIDER: FAKE_KEY}

        listed = client.get("/api/v1/providers")
        held = next(item for item in listed.json() if item["provider"] == SUITE_PROVIDER.value)
        assert held["key_present"] is True
        assert held["key_source"] == "system keyring"
        # The other provider is unaffected by the write.
        assert all(
            item["key_present"] is False
            for item in listed.json()
            if item["provider"] != SUITE_PROVIDER.value
        )

        bodies = [stored.text, listed.text, client.get("/api/v1/openapi.json").text]
        assert all(FAKE_KEY not in body for body in bodies)


def test_the_key_reaches_no_response_body_or_documented_example(tmp_path: Path) -> None:
    store = MemoryStore()
    with _client(tmp_path, store) as client:
        seen = [
            client.put(f"/api/v1/providers/{SUITE_PROVIDER.value}/key", json={"key": FAKE_KEY}),
            client.get("/api/v1/providers"),
            client.put(f"/api/v1/providers/{SUITE_PROVIDER.value}/key", json={"key": "  "}),
            client.delete(f"/api/v1/providers/{SUITE_PROVIDER.value}/key"),
        ]
        assert all(FAKE_KEY not in response.text for response in seen)

        schema = client.get("/api/v1/openapi.json").json()
        request_body = schema["components"]["schemas"]["ProviderKeyBody"]
        assert "example" not in json.dumps(request_body)
        assert "key" not in schema["components"]["schemas"]["ProviderResponse"]["properties"]


def test_a_blank_key_is_refused(tmp_path: Path) -> None:
    store = MemoryStore()
    with _client(tmp_path, store) as client:
        for blank in ("", "   ", "\t\n"):
            refused = client.put(
                f"/api/v1/providers/{SUITE_PROVIDER.value}/key",
                json={"key": blank},
            )
            assert refused.status_code == 422
            assert "API key is required" in refused.json()["detail"]
        assert store.keys == {}


def test_deleting_removes_the_stored_key(tmp_path: Path) -> None:
    store = MemoryStore({SUITE_PROVIDER: FAKE_KEY})
    with _client(tmp_path, store) as client:
        removed = client.delete(f"/api/v1/providers/{SUITE_PROVIDER.value}/key")
        assert removed.status_code == 200
        assert removed.json()["key_present"] is False
        assert removed.json()["stored_in_keyring"] is False
        assert store.keys == {}
        assert all(item["key_present"] is False for item in client.get("/api/v1/providers").json())


def test_an_environment_key_is_reported_and_survives_a_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SUITE_KEY_VARIABLE, FAKE_KEY)
    store = MemoryStore()
    with _client(tmp_path, store) as client:
        listed = next(
            item
            for item in client.get("/api/v1/providers").json()
            if item["provider"] == SUITE_PROVIDER.value
        )
        assert listed["key_present"] is True
        assert listed["key_source"] == "environment variable"
        assert listed["stored_in_keyring"] is False

        removed = client.delete(f"/api/v1/providers/{SUITE_PROVIDER.value}/key")
        assert removed.status_code == 200
        # The API cannot unset a shell variable, and says the key is still there
        # rather than reporting a removal it did not perform.
        assert removed.json()["key_present"] is True
        assert removed.json()["key_source"] == "environment variable"
        assert removed.json()["stored_in_keyring"] is False
        assert FAKE_KEY not in removed.text


def test_a_keyring_that_cannot_write_is_reported_rather_than_swallowed(tmp_path: Path) -> None:
    with _client(tmp_path, BrokenKeyring()) as client:
        refused = client.put(
            f"/api/v1/providers/{SUITE_PROVIDER.value}/key",
            json={"key": FAKE_KEY},
        )
        assert refused.status_code == 503
        assert "Could not save the key" in refused.json()["detail"]
        assert SUITE_KEY_VARIABLE in refused.json()["detail"]
        assert FAKE_KEY not in refused.text


def test_a_keyring_that_cannot_forget_is_reported_rather_than_swallowed(tmp_path: Path) -> None:
    with _client(tmp_path, BrokenKeyring(held=FAKE_KEY)) as client:
        refused = client.delete(f"/api/v1/providers/{SUITE_PROVIDER.value}/key")
        assert refused.status_code == 503
        assert "still holds" in refused.json()["detail"]
        assert FAKE_KEY not in refused.text


def test_a_key_write_keeps_the_local_origin_boundary(tmp_path: Path) -> None:
    store = MemoryStore()
    with _client(tmp_path, store) as client:
        refused = client.put(
            f"/api/v1/providers/{SUITE_PROVIDER.value}/key",
            headers={"Host": "localhost:8765", "Origin": "https://evil.example"},
            json={"key": FAKE_KEY},
        )
        assert refused.status_code == 403
        assert refused.json()["title"] == "Cross-origin request refused"
        assert store.keys == {}


def test_an_unknown_provider_is_refused(tmp_path: Path) -> None:
    with _client(tmp_path, MemoryStore()) as client:
        assert client.get("/api/v1/providers").status_code == 200
        assert client.delete("/api/v1/providers/pretend/key").status_code == 422
