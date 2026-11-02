"""Build a ``Client`` for scripted or live evaluation runs.

Scripted mode uses the in-process ``ScriptedProvider`` from this package. Live
mode takes the caller's API key and constructs the official vendor SDK through
``provider_for``. Both modes share every other Client dependency (Settings, cache, ledger) so a
scripted run and a live run stay comparable.
"""

from __future__ import annotations

from pathlib import Path

from evals.support.scripted_provider import EVAL_PROVIDER, ScriptedProvider
from tailorcv.application.config import ProviderName, Settings
from tailorcv.infrastructure.providers.base import Provider, provider_for
from tailorcv.infrastructure.providers.cache import ResponseCache
from tailorcv.infrastructure.providers.client import Client
from tailorcv.shared.costs import Ledger
from tailorcv.shared.costs.prices import table as price_table


def _cache(cache_dir: Path | None) -> ResponseCache:
    """Return a disabled response cache; scoring must not silently cache."""
    return ResponseCache(enabled=False, directory=cache_dir)


def build_scripted_client(provider: ScriptedProvider, cache_dir: Path | None = None) -> Client:
    """Return a Client wired to an in-process scripted provider."""
    settings = Settings(provider=EVAL_PROVIDER)
    return Client(
        provider=provider,
        settings=settings,
        cache=_cache(cache_dir),
        ledger=Ledger(),
    )


def build_live_client(
    provider_name: ProviderName,
    api_key: str,
    *,
    cache_dir: Path | None = None,
    ledger: Ledger | None = None,
    settings: Settings | None = None,
) -> Client:
    """Return a Client wired to the real vendor SDK for one provider.

    A live run waits on nobody, so it asks for the tier queue work uses.
    """
    settings = settings if settings is not None else Settings(provider=provider_name)
    live_provider: Provider = provider_for(provider_name, api_key)
    return Client(
        provider=live_provider,
        settings=settings,
        cache=_cache(cache_dir),
        ledger=ledger if ledger is not None else Ledger(prices=price_table(settings.prices)),
        service_tier=settings.unattended_tier(),
    )
