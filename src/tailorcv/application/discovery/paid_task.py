"""Provider, ledger, tier, and billing shared by the paid discovery task handlers.

Acquiring the provider and guaranteeing the billing are one context manager
because a call this run makes must be filed against the workspace even when the
task raises afterwards.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from tailorcv.application.config import Settings
from tailorcv.application.workspaces.spend import guarantee_billed
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.providers.base import Provider, provider_for
from tailorcv.infrastructure.providers.cache import ResponseCache
from tailorcv.infrastructure.providers.client import Client
from tailorcv.infrastructure.providers.credentials import resolve
from tailorcv.shared.costs import Ledger
from tailorcv.shared.costs.prices import ledger_for


@dataclass(frozen=True)
class BilledRun:
    """One workspace's paid path: a client builder and its ledger."""

    build_client: Callable[[], Client]
    ledger: Ledger


@contextmanager
def billed_run(
    database: Database,
    *,
    workspace_id: str,
    task_id: str,
    settings: Settings,
    origin: str,
    provider: Provider | None = None,
) -> Iterator[BilledRun]:
    """Arm the paid path for one task and file its calls on the way out.

    Response caching is off: a paid re-read exists to observe what the posting
    says now, and a cached answer would report the earlier read instead.
    """
    service_tier = settings.unattended_tier()
    resolved = provider or provider_for(
        settings.provider, resolve(settings.provider).key, settings.timeout_for(service_tier)
    )
    ledger = ledger_for(settings.prices)
    with (
        guarantee_billed(
            database,
            workspace_id=workspace_id,
            task_id=task_id,
            ledger=ledger,
            provider=settings.provider,
            origin=origin,
        ),
        ResponseCache(enabled=False) as cache,
    ):

        def build_client() -> Client:
            return Client(
                provider=resolved,
                settings=settings,
                cache=cache,
                ledger=ledger,
                service_tier=service_tier,
            )

        yield BilledRun(build_client=build_client, ledger=ledger)
