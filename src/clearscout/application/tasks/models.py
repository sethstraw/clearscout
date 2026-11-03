"""Typed read models and closed task vocabulary."""

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from clearscout.domain import TaskStatus, TaskType
from clearscout.shared.costs import ModelPrice
from clearscout.shared.costs.prices import PriceProvenance
from clearscout.shared.models import StrictModel


class TaskTypeResponse(StrictModel):
    """One registered task type and what running it costs the person."""

    task_type: TaskType
    paid: bool
    cost: str


class EvaluationCostStageResponse(StrictModel):
    """Configured stage rates and an advisory one-attempt token model."""

    stage: Literal["jobspec", "match"]
    provider: Literal["openai", "anthropic"]
    model: str
    requested_service_tier: Literal["standard", "flex"]
    price: ModelPrice | None
    provenance: PriceProvenance | None
    input_token_estimate: int = Field(ge=0)
    input_token_range_high: int = Field(ge=0)
    output_token_budget: int = Field(ge=0)
    expected_cost_usd: Decimal | None = Field(default=None, ge=0)
    range_low_usd: Decimal | None = Field(default=None, ge=0)
    range_high_usd: Decimal | None = Field(default=None, ge=0)
    expected_unavailable_reason: str | None = None
    range_unavailable_reason: str | None = None
    historical_output_samples: int = Field(default=0, ge=0)


class EvaluationCostItemResponse(StrictModel):
    """One selected evaluation's retained-input estimate or explicit unavailable reason."""

    target_kind: Literal["job", "pending_discovery"]
    target_id: str
    stages: list[EvaluationCostStageResponse] = Field(default_factory=list)
    expected_cost_usd: Decimal | None = Field(default=None, ge=0)
    range_low_usd: Decimal | None = Field(default=None, ge=0)
    range_high_usd: Decimal | None = Field(default=None, ge=0)
    expected_unavailable_reason: str | None = None
    range_unavailable_reason: str | None = None
    current_skip_reason: str | None = None
    retained_details_read_at: datetime | None = None


class EvaluationPreflightResponse(StrictModel):
    """Advisory costs from current rates; this read never queues or pays for evaluation."""

    workspace_id: str
    currency: Literal["USD"] = "USD"
    generated_at: datetime
    items: list[EvaluationCostItemResponse]
    expected_cost_usd: Decimal | None = Field(default=None, ge=0)
    range_low_usd: Decimal | None = Field(default=None, ge=0)
    range_high_usd: Decimal | None = Field(default=None, ge=0)
    expected_unavailable_reason: str | None = None
    range_unavailable_reason: str | None = None
    basis: str
    limitations: list[str]
    advisory: Literal[True] = True


class PostingRecoveryApprovalManifest(StrictModel):
    """Ordered, expiring bounded estimate for assisted posting recovery."""

    pending_discovery_ids: tuple[str, ...]
    snapshot_sha256: str
    provider: str
    model: str
    system_prompt_sha256: str
    user_prompt_sha256: str
    schema_sha256: str
    settings_sha256: str
    price_sha256: str
    estimate_cents: int
    issued_at: datetime
    expires_at: datetime


class TaskResponse(StrictModel):
    """Durable task state without private input or provider response bodies."""

    id: str
    workspace_id: str
    task_type: TaskType
    job_posting_ids: list[str]
    # The pending discoveries a task names, so a reload can tell which rows are in flight.
    pending_discovery_ids: list[str]
    # The sources a task names; empty on a scan that swept every enabled source.
    source_ids: list[str]
    source_resume_from_task_id: str | None = None
    status: TaskStatus
    attempt_count: int
    progress: dict[str, Any]
    cancellation_requested_at: datetime | None
    error_category: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    updated_at: datetime
    completed_at: datetime | None


class TaskBillingResponse(StrictModel):
    """Recorded model-call billing for one durable task."""

    task_id: str
    status: Literal["no_model_calls", "priced", "unpriced"]
    call_count: int = Field(ge=0)
    unpriced_call_count: int = Field(ge=0)
    actual_cost_usd: Decimal | None = Field(default=None, ge=0)
    # Calls the discounted tier refused that were then answered on the standard tier.
    resent_on_standard_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def require_consistent_billing_state(self) -> Self:
        """Keep the billing status consistent with its counts and amount."""
        if self.unpriced_call_count > self.call_count:
            raise ValueError("Unpriced model calls cannot exceed total model calls.")
        if self.resent_on_standard_count > self.call_count:
            raise ValueError("Resent model calls cannot exceed total model calls.")
        if self.status == "no_model_calls":
            if self.call_count or self.unpriced_call_count or self.actual_cost_usd != 0:
                raise ValueError("No-call billing must report zero calls and zero cost.")
        elif self.status == "priced":
            if not self.call_count or self.unpriced_call_count or self.actual_cost_usd is None:
                raise ValueError("Priced billing requires priced calls and a recorded cost.")
        elif (
            not self.call_count or not self.unpriced_call_count or self.actual_cost_usd is not None
        ):
            raise ValueError("Unpriced billing requires an unpriced call and no total cost.")
        return self


class ClaimedTask(TaskResponse):
    """A leased task with its raw payload, validated by the worker so a bad row fails one task."""

    payload: Any


class TaskEventResponse(StrictModel):
    """One durable event in a resumable task stream."""

    id: str
    task_id: str
    sequence: int
    stage: str
    event_type: str
    payload: dict[str, Any]
    created_at: datetime
