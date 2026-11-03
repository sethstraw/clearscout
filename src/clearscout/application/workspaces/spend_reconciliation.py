"""Reconcile OpenAI's actual billed cost against ClearScout's recorded cost.

OpenAI's organization billing is org-wide, and ClearScout's ledger is per-call
and per-workspace, so this compares the whole organization's billed cost in a
window against every OpenAI call ClearScout priced in this database over the same
window. It is an organization-level cross-check, not a per-task or per-workspace
reconciliation, and it infers no per-task mapping.
"""

from __future__ import annotations

from datetime import UTC, datetime

from clearscout.application.config import ProviderName
from clearscout.application.workspaces.spend import recorded_call_cost
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import ModelCall
from clearscout.infrastructure.providers.billing import (
    OpenAIBillingError,
    OrganizationBilling,
)
from clearscout.infrastructure.providers.credentials import OPENAI_ADMIN_ENV
from clearscout.shared.models import StrictModel

SCOPE_NOTE = (
    "OpenAI reports cost for the whole organization the admin key belongs to, which can "
    "include usage from other tools or projects that share the key. The recorded figure "
    "counts only the OpenAI model calls ClearScout priced in this database. This is an "
    "organization-level cross-check, not a per-task or per-workspace reconciliation."
)


class OpenAiActualSpendResponse(StrictModel):
    """OpenAI's actual billed cost beside ClearScout's recorded cost for a window."""

    available: bool
    unavailable_reason: str | None = None
    start_time: int
    end_time: int
    currency: str = "USD"
    openai_actual_usd: float | None = None
    recorded_usd: float | None = None
    recorded_call_count: int = 0
    unpriced_call_count: int = 0
    drift_usd: float | None = None
    scope_note: str = SCOPE_NOTE


class SpendReconciliationService:
    """Compare OpenAI's billed cost against the local ledger over a window."""

    def __init__(self, database: Database, billing: OrganizationBilling | None) -> None:
        """Bind the reader to one database and a billing source, ``None`` without an admin key."""
        self.database = database
        self.billing = billing

    def reconcile(self, start_time: int, end_time: int) -> OpenAiActualSpendResponse:
        """Return actual-vs-recorded OpenAI cost for a Unix-seconds window."""
        if self.billing is None:
            return OpenAiActualSpendResponse(
                available=False,
                unavailable_reason=(
                    "No OpenAI organization admin key is configured. Set the "
                    f"{OPENAI_ADMIN_ENV} environment variable, or store an admin key, to "
                    "read actual billed cost."
                ),
                start_time=start_time,
                end_time=end_time,
            )
        recorded, calls, unpriced = self._recorded(start_time, end_time)
        try:
            billed = self.billing.costs(start_time, end_time)
        except OpenAIBillingError as error:
            return OpenAiActualSpendResponse(
                available=False,
                unavailable_reason=str(error),
                start_time=start_time,
                end_time=end_time,
                recorded_usd=recorded,
                recorded_call_count=calls,
                unpriced_call_count=unpriced,
            )
        actual = float(billed.amount)
        drift = None if recorded is None else round(actual - recorded, 8)
        return OpenAiActualSpendResponse(
            available=True,
            start_time=start_time,
            end_time=end_time,
            currency=billed.currency,
            openai_actual_usd=actual,
            recorded_usd=recorded,
            recorded_call_count=calls,
            unpriced_call_count=unpriced,
            drift_usd=drift,
        )

    def _recorded(self, start_time: int, end_time: int) -> tuple[float | None, int, int]:
        """Return recorded OpenAI cost, call count, and unpriced count for the window.

        Uses the shared ledger honest-blank rule: a window holding any unpriced
        call withholds its total rather than understating it.
        """
        start_dt = datetime.fromtimestamp(start_time, tz=UTC)
        end_dt = datetime.fromtimestamp(end_time, tz=UTC)
        with self.database.session() as session:
            count, unpriced, recorded = recorded_call_cost(
                session,
                ModelCall.provider == ProviderName.OPENAI.value,
                ModelCall.created_at >= start_dt,
                ModelCall.created_at < end_dt,
            )
        return (None if recorded is None else float(recorded)), count, unpriced
