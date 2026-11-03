"""Find, check, and store the API credential used for model requests.

clearscout uses provider API billing, not a Claude or ChatGPT subscription.
Resolution order is flag, then environment, then keyring. When none finds a key
and a person is at the terminal, onboarding takes the key without echoing it,
checks it against the provider, and stores it.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from clearscout.application.config import ProviderName
from clearscout.shared.errors import AuthError

KEYRING_SERVICE = "clearscout"

# Raise AuthError for unusable keys; `None` selects the configured default.
Validator = Callable[[ProviderName, str], None]


ENVIRONMENT_VARIABLES = {
    ProviderName.ANTHROPIC: "ANTHROPIC_API_KEY",
    ProviderName.OPENAI: "OPENAI_API_KEY",
}

# The organization admin key reads billing, not inference, so it is org-scoped
# and kept apart from the per-provider inference keys above.
OPENAI_ADMIN_ENV = "OPENAI_ADMIN_KEY"
OPENAI_ADMIN_KEYRING_USERNAME = "openai-admin"

CONSOLE_URLS = {
    ProviderName.ANTHROPIC: "https://console.anthropic.com/settings/keys",
    ProviderName.OPENAI: "https://platform.openai.com/api-keys",
}

WHY_A_KEY = (
    "clearscout needs its own API key. Neither provider lets a third-party tool\n"
    "charge model calls to a Claude or ChatGPT subscription, so a Console key is\n"
    "the supported route. API use is billed separately; review current provider\n"
    "pricing before a live run."
)


class Source(StrEnum):
    """Where a credential came from, for reporting without revealing the key."""

    FLAG = "command-line flag"
    ENVIRONMENT = "environment variable"
    KEYRING = "system keyring"
    PROMPT = "entered just now"


@dataclass(frozen=True)
class Credential:
    """An API key and where it was found.

    Nothing in this module renders the key; ``describe`` names only its source.
    """

    key: str
    provider: ProviderName
    source: Source

    def describe(self) -> str:
        """Return a human description of the credential, never the key itself."""
        return f"{self.provider.value} key from {self.source.value}"


class Prompter(Protocol):
    """The terminal interactions onboarding needs, as an interface."""

    def interactive(self) -> bool:
        """Return whether there is a person available to answer."""
        ...

    def notice(self, message: str) -> None:
        """Show an informational message."""
        ...

    def plain(self, message: str) -> None:
        """Write one unadorned line, on the same object the pipeline asks questions with."""
        ...

    def step(self, index: int, total: int, label: str) -> None:
        """Announce that stage ``index`` of ``total`` has started."""
        ...

    def confirm(self, question: str, default: bool = True) -> bool:
        """Ask a yes or no question."""
        ...

    def ask(self, question: str, default: str = "") -> str:
        """Ask for a value the user can see as they type."""
        ...

    def secret(self, question: str) -> str:
        """Ask for a value without echoing it. For credentials only."""
        ...

    def open_url(self, url: str) -> None:
        """Open a URL in the user's browser."""
        ...


class KeyStore(Protocol):
    """The slice of a credential store this tool uses."""

    def get(self, provider: ProviderName) -> str | None:
        """Return the stored key for ``provider``, if there is one."""
        ...

    def set(self, provider: ProviderName, key: str) -> None:
        """Store a key for ``provider``."""
        ...

    def forget(self, provider: ProviderName) -> None:
        """Remove any stored key for ``provider``."""
        ...


class KeyringStore:
    """The default store: the operating system's own credential manager.

    Every method swallows backend failures, so a machine with no keyring reads
    as a key that was not saved.
    """

    def get(self, provider: ProviderName) -> str | None:
        """Return the stored key, or ``None`` if there is none or no backend."""
        try:
            import keyring

            return keyring.get_password(KEYRING_SERVICE, provider.value)
        except Exception:  # noqa: BLE001 - keyring backends fail in many ways
            return None

    def set(self, provider: ProviderName, key: str) -> None:
        """Store a key, raising only when the caller can act on the failure."""
        try:
            import keyring

            keyring.set_password(KEYRING_SERVICE, provider.value, key)
        except Exception as error:
            raise AuthError(
                f"Could not save the key to your system keyring: {error}\n"
                f"Set {ENVIRONMENT_VARIABLES[provider]} in your shell instead."
            ) from error

    def forget(self, provider: ProviderName) -> None:
        """Remove a stored key, treating "there was none" as success."""
        try:
            import keyring

            keyring.delete_password(KEYRING_SERVICE, provider.value)
        except Exception:  # noqa: BLE001 - see class docstring
            return


class AdminKeyStore(Protocol):
    """The one read this tool needs for the optional organization admin key."""

    def admin_key(self) -> str | None:
        """Return the stored organization admin key, if there is one."""
        ...


class KeyringAdminStore:
    """Read the organization admin key from the OS keyring, swallowing backend failures."""

    def admin_key(self) -> str | None:
        """Return the stored admin key, or ``None`` if there is none or no backend."""
        try:
            import keyring

            return keyring.get_password(KEYRING_SERVICE, OPENAI_ADMIN_KEYRING_USERNAME)
        except Exception:  # noqa: BLE001 - keyring backends fail in many ways
            return None


def find_admin_key(
    explicit: str | None = None, *, store: AdminKeyStore | None = None
) -> str | None:
    """Find the optional OpenAI organization admin key, or ``None`` when unset.

    Resolution is an explicit value, then the environment, then the keyring,
    with no onboarding. Billing reads are unavailable without it. The key is
    never logged.
    """
    if explicit:
        return explicit
    if environment := os.environ.get(OPENAI_ADMIN_ENV):
        return environment
    return (store or KeyringAdminStore()).admin_key()


def find(
    provider: ProviderName,
    explicit: str | None = None,
    store: KeyStore | None = None,
) -> Credential | None:
    """Look for a credential without asking anyone anything.

    Separate from :func:`resolve` because diagnostics need to report what is
    configured without triggering an onboarding prompt as a side effect.
    """
    if explicit:
        return Credential(explicit, provider, Source.FLAG)
    if environment := os.environ.get(ENVIRONMENT_VARIABLES[provider]):
        return Credential(environment, provider, Source.ENVIRONMENT)
    if stored := (store or KeyringStore()).get(provider):
        return Credential(stored, provider, Source.KEYRING)
    return None


def resolve(
    provider: ProviderName,
    explicit: str | None = None,
    *,
    prompter: Prompter | None = None,
    store: KeyStore | None = None,
    validator: Validator | None = None,
) -> Credential:
    """Return a usable credential, onboarding the user if that is what it takes.

    The interactive path runs only when nothing was found and someone is there
    to answer. Otherwise the failure is immediate and names every way to supply
    a key.
    """
    store = store or KeyringStore()
    if found := find(provider, explicit, store):
        return found
    if prompter is None or not prompter.interactive():
        raise AuthError(missing_key_message(provider))
    return onboard(provider, prompter=prompter, store=store, validator=validator)


def onboard(
    provider: ProviderName,
    *,
    prompter: Prompter,
    store: KeyStore | None = None,
    validator: Validator | None = None,
    attempts: int = 3,
) -> Credential:
    """Walk a person through creating and saving their first key.

    A key is validated before it is stored.
    """
    if attempts < 1:
        raise AuthError("At least one key-entry attempt is required.")

    store = store or KeyringStore()
    validate = validator or check_key
    url = CONSOLE_URLS[provider]

    prompter.notice(f"No {provider.value} API key found.\n\n{WHY_A_KEY}\n")
    if prompter.confirm(f"Open {url} now?"):
        prompter.open_url(url)

    for remaining in range(attempts, 0, -1):
        key = prompter.secret(f"Paste your {provider.value} API key").strip()
        if not key:
            raise AuthError("No key entered. " + missing_key_message(provider))
        try:
            validate(provider, key)
        except AuthError as error:
            if remaining == 1:
                raise
            prompter.notice(f"{error}\n")
            continue
        break

    prompter.notice("Key accepted.")
    prompter.notice("Saving it to your system keyring. Your OS may ask you to allow that.")
    try:
        store.set(provider, key)
        prompter.notice("Saved to your system keyring; future runs will not ask.\n")
    except AuthError as error:
        prompter.notice(f"{error}\n")
    return Credential(key, provider, Source.PROMPT)


def check_key(provider: ProviderName, key: str) -> None:
    """Confirm a key works by listing models, which costs nothing.

    A completion would also prove the key, but bills tokens.
    """
    from clearscout.infrastructure.providers.base import provider_for

    provider_for(provider, key).check_credentials()


def missing_key_message(provider: ProviderName) -> str:
    """Return the error text for a missing key, naming every way to supply one."""
    return (
        f"No API key for {provider.value}.\n"
        f"  - run 'clearscout auth set {provider.value}' to store one in your keyring\n"
        f"  - or export {ENVIRONMENT_VARIABLES[provider]}\n"
        f"  - or pass --api-key\n"
        f"Create a key at {CONSOLE_URLS[provider]}."
    )
