"""Credential resolution and the first-run onboarding flow."""

from __future__ import annotations

import pytest

from tailorcv.application.config import ProviderName
from tailorcv.infrastructure.providers.credentials import (
    CONSOLE_URLS,
    ENVIRONMENT_VARIABLES,
    Source,
    find,
    missing_key_message,
    onboard,
    resolve,
)
from tailorcv.shared.errors import AuthError
from tests.support.doubles import MemoryStore, ScriptedPrompter

ANTHROPIC = ProviderName.ANTHROPIC


@pytest.fixture(autouse=True)
def _no_ambient_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in ENVIRONMENT_VARIABLES.values():
        monkeypatch.delenv(variable, raising=False)


def accepts_anything(provider: ProviderName, key: str) -> None:
    return None


def rejects_short_keys(provider: ProviderName, key: str) -> None:
    if len(key) < 10:
        raise AuthError("that key was rejected: HTTP 401")


def test_an_explicit_key_wins() -> None:
    found = find(ANTHROPIC, "from-flag", MemoryStore({ANTHROPIC: "stored"}))
    assert found is not None
    assert (found.key, found.source) == ("from-flag", Source.FLAG)


def test_the_environment_beats_the_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    found = find(ANTHROPIC, store=MemoryStore({ANTHROPIC: "stored"}))
    assert found is not None
    assert (found.key, found.source) == ("from-env", Source.ENVIRONMENT)


def test_the_keyring_is_the_last_silent_source() -> None:
    found = find(ANTHROPIC, store=MemoryStore({ANTHROPIC: "stored"}))
    assert found is not None
    assert found.source is Source.KEYRING


def test_nothing_configured_finds_nothing() -> None:
    assert find(ANTHROPIC, store=MemoryStore()) is None


def test_a_credential_never_describes_itself_with_the_key() -> None:
    found = find(ANTHROPIC, "sk-secret-value", MemoryStore())
    assert found is not None
    assert "sk-secret-value" not in found.describe()


def test_a_non_interactive_run_fails_instead_of_blocking() -> None:
    prompter = ScriptedPrompter(interactive=False)
    with pytest.raises(AuthError, match="tailorcv auth set"):
        resolve(ANTHROPIC, prompter=prompter, store=MemoryStore())
    assert prompter.messages == []


def test_no_prompter_at_all_fails_the_same_way() -> None:
    with pytest.raises(AuthError, match=ENVIRONMENT_VARIABLES[ANTHROPIC]):
        resolve(ANTHROPIC, store=MemoryStore())


def test_the_missing_key_message_names_every_route() -> None:
    message = missing_key_message(ANTHROPIC)
    assert "tailorcv auth set" in message
    assert ENVIRONMENT_VARIABLES[ANTHROPIC] in message
    assert "--api-key" in message
    assert CONSOLE_URLS[ANTHROPIC] in message


def test_onboarding_explains_why_a_key_is_needed_before_asking() -> None:
    prompter = ScriptedPrompter(secrets=["a-valid-looking-key"])
    onboard(ANTHROPIC, prompter=prompter, store=MemoryStore(), validator=accepts_anything)
    transcript = prompter.transcript
    assert "subscription" in transcript
    assert transcript.index("subscription") < transcript.index("Paste your")


def test_onboarding_offers_the_right_console_page() -> None:
    prompter = ScriptedPrompter(secrets=["a-valid-looking-key"])
    onboard(ANTHROPIC, prompter=prompter, store=MemoryStore(), validator=accepts_anything)
    assert prompter.opened == [CONSOLE_URLS[ANTHROPIC]]


def test_declining_the_browser_still_lets_the_key_be_pasted() -> None:
    prompter = ScriptedPrompter(secrets=["a-valid-looking-key"], confirm=False)
    store = MemoryStore()
    onboard(ANTHROPIC, prompter=prompter, store=store, validator=accepts_anything)
    assert prompter.opened == []
    assert store.keys[ANTHROPIC] == "a-valid-looking-key"


def test_a_valid_key_is_stored_for_next_time() -> None:
    store = MemoryStore()
    credential = onboard(
        ANTHROPIC,
        prompter=ScriptedPrompter(secrets=["a-valid-looking-key"]),
        store=store,
        validator=accepts_anything,
    )
    assert store.keys[ANTHROPIC] == "a-valid-looking-key"
    assert credential.source is Source.PROMPT


def test_a_rejected_key_is_never_stored() -> None:
    store = MemoryStore()
    with pytest.raises(AuthError):
        onboard(
            ANTHROPIC,
            prompter=ScriptedPrompter(secrets=["short", "short", "short"]),
            store=store,
            validator=rejects_short_keys,
        )
    assert store.keys == {}


def test_a_typo_can_be_corrected_without_starting_over() -> None:
    store = MemoryStore()
    prompter = ScriptedPrompter(secrets=["short", "a-valid-looking-key"])
    onboard(ANTHROPIC, prompter=prompter, store=store, validator=rejects_short_keys)
    assert store.keys[ANTHROPIC] == "a-valid-looking-key"
    assert "rejected" in prompter.transcript


def test_an_empty_paste_stops_rather_than_looping() -> None:
    with pytest.raises(AuthError, match="No key entered"):
        onboard(
            ANTHROPIC,
            prompter=ScriptedPrompter(secrets=[]),
            store=MemoryStore(),
            validator=accepts_anything,
        )


def test_onboarding_refuses_zero_attempts_before_prompting() -> None:
    prompter = ScriptedPrompter()
    with pytest.raises(AuthError, match="At least one key-entry attempt"):
        onboard(
            ANTHROPIC,
            prompter=prompter,
            store=MemoryStore(),
            validator=accepts_anything,
            attempts=0,
        )
    assert prompter.messages == []


def test_a_keyring_that_cannot_save_does_not_lose_the_run() -> None:
    class BrokenStore(MemoryStore):
        def set(self, provider: ProviderName, key: str) -> None:
            raise AuthError("no keyring backend on this machine")

    prompter = ScriptedPrompter(secrets=["a-valid-looking-key"])
    credential = onboard(
        ANTHROPIC, prompter=prompter, store=BrokenStore(), validator=accepts_anything
    )
    assert credential.key == "a-valid-looking-key"
    assert "no keyring backend" in prompter.transcript


def test_resolve_onboards_only_when_nothing_was_found() -> None:
    prompter = ScriptedPrompter(secrets=["a-valid-looking-key"])
    credential = resolve(
        ANTHROPIC,
        prompter=prompter,
        store=MemoryStore({ANTHROPIC: "already-stored"}),
        validator=accepts_anything,
    )
    assert credential.key == "already-stored"
    assert prompter.messages == []
