"""Actual-spend reconciliation and price-data HTTP routes."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import Field, model_validator

from clearscout.application.workspaces.spend_reconciliation import OpenAiActualSpendResponse
from clearscout.delivery.api.constants import API_PREFIX
from clearscout.delivery.api.dependencies import (
    PriceCatalogDep,
    SpendReconciliationServiceDep,
)
from clearscout.shared.costs.prices import PriceRefreshResponse, refresh_prices
from clearscout.shared.models import StrictModel

router = APIRouter()


class ReconciliationWindowBody(StrictModel):
    """The window to reconcile, as Unix seconds: inclusive start, exclusive end."""

    start_time: int = Field(ge=0)
    end_time: int = Field(ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> ReconciliationWindowBody:
        """Refuse an empty or backwards window before it reaches the provider."""
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time.")
        return self


@router.post(
    f"{API_PREFIX}/billing/openai/reconciliation",
    response_model=OpenAiActualSpendResponse,
    tags=["billing"],
    summary="Refresh OpenAI's actual billed cost and compare it with the recorded ledger",
)
def reconcile_openai_spend(
    body: ReconciliationWindowBody,
    service: SpendReconciliationServiceDep,
) -> OpenAiActualSpendResponse:
    """Pull organization-wide billed cost beside recorded cost; a free read, never per task."""
    return service.reconcile(body.start_time, body.end_time)


@router.post(
    f"{API_PREFIX}/billing/prices/refresh",
    response_model=PriceRefreshResponse,
    tags=["billing"],
    summary="Fetch published model prices and report which rates moved",
)
def refresh_model_prices(catalog: PriceCatalogDep) -> PriceRefreshResponse:
    """Re-derive the shipped models' rates from published prices; a failed fetch keeps the old."""
    return refresh_prices(catalog)
