"""Spend summaries and run cost history."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from tailorcv.domain import (
    TaskType,
)
from tailorcv.shared.models import StrictModel


class SpendGroupResponse(StrictModel):
    """Spend for one stage or one model."""

    name: str
    calls: int
    amount_usd: float | None
    unpriced_calls: int


class SpendCallResponse(StrictModel):
    """One recorded model call."""

    id: str
    stage: str
    model: str
    provider: str
    requested_model: str
    served_model: str | None
    amount_usd: float | None
    pricing_source: str | None
    # Null on a call recorded before the price data was named, which is unknown.
    price_snapshot: str | None
    # TailorCV's response cache: true means no provider call was made, so the
    # two prompt-cache token counts below are zero and nothing was billed.
    cache_hit: bool
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    reasoning_tokens: int
    duration_ms: int | None
    completion_reason: str | None
    created_at: datetime


class SpendSummaryResponse(StrictModel):
    """What this workspace has actually spent, never what it might spend."""

    currency: Literal["USD"]
    call_count: int
    unpriced_calls: int
    total_usd: float | None
    by_stage: list[SpendGroupResponse]
    by_model: list[SpendGroupResponse]
    recent: list[SpendCallResponse]


# The one vocabulary for what a paid task is priced in, shared by the wire
# response and by the per-unit map that fills it.
CostUnit = Literal["call", "posting", "attempt", "run"]


# The per-unit figures come from SpendService.run_cost_history, over completed
# runs only; a caller multiplies them by the units it is about to run.
class RunCostHistoryResponse(StrictModel):
    """Cost per unit of work for one kind of paid run."""

    task_type: TaskType
    currency: Literal["USD"]
    unit: CostUnit
    measured_runs: int
    unpriced_runs: int
    replayed_runs: int
    per_unit_low: Decimal | None
    per_unit_median: Decimal | None
    per_unit_high: Decimal | None
