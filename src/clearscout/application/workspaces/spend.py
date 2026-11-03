"""Provider usage and estimated cost of a workspace's model calls.

Writing a call and its cost record happens here in one function, because a call
written without its cost cannot be told apart from a free one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from clearscout.application.failures import NotFoundError
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    CostRecord,
    ModelCall,
    ModelCallEnvelope,
    ResponseCacheArchive,
    Workspace,
)
from clearscout.infrastructure.providers.cache import ResponseCache
from clearscout.shared.costs import Ledger

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from clearscout.application.job_search.models import (
        CostUnit,
        RunCostHistoryResponse,
        SpendSummaryResponse,
    )
    from clearscout.application.tasks.models import TaskBillingResponse


@contextmanager
def guarantee_billed(
    database: Database,
    *,
    workspace_id: str,
    task_id: str | None,
    ledger: Ledger,
    provider: str,
    origin: str,
    tailoring_run_id: str | None = None,
) -> Iterator[None]:
    """File every call in ``ledger`` on exit, in its own transaction."""
    try:
        yield
    finally:
        if ledger.entries:
            with database.session() as session:
                record_calls(
                    session,
                    workspace_id=workspace_id,
                    task_id=task_id,
                    ledger=ledger,
                    provider=provider,
                    origin=origin,
                    tailoring_run_id=tailoring_run_id,
                )


def record_calls(
    session: Session,
    *,
    workspace_id: str,
    task_id: str | None,
    ledger: Ledger,
    provider: str,
    origin: str,
    tailoring_run_id: str | None = None,
) -> list[ModelCall]:
    """Persist every call in one run's ledger with the cost it carried.

    ``provider`` is the run's configured provider and is used only for a call
    the ledger recorded without one, which is a call no adapter answered.

    Returns the rows created, in ledger order.
    """
    created: list[ModelCall] = []
    for sequence, entry in enumerate(ledger.entries, start=1):
        call = ModelCall(
            workspace_id=workspace_id,
            task_id=task_id,
            tailoring_run_id=tailoring_run_id,
            stage=entry.stage,
            provider=entry.provider or provider,
            requested_model=entry.model,
            served_model=entry.served or None,
            provider_response_id=entry.response_id or None,
            input_tokens=entry.usage.input_tokens,
            output_tokens=entry.usage.output_tokens,
            reasoning_tokens=entry.usage.reasoning_tokens,
            cache_read_tokens=entry.usage.cache_read_tokens,
            cache_write_tokens=entry.usage.cache_write_tokens,
            cache_hit=entry.cached,
            duration_ms=entry.milliseconds,
            completion_reason=entry.finish or None,
            effort=entry.effort or None,
            verbosity=entry.verbosity or None,
            image_detail=entry.image_detail or None,
            image_token_ceiling=entry.image_token_ceiling,
            service_tier=entry.service_tier or None,
            resent_on_standard=entry.resent_on_standard,
            code_revision=entry.revision or None,
            package_version=entry.package_version or None,
            prompt_names=list(entry.prompts),
            instructions_digest=entry.instructions_digest or None,
            evidence_digest=entry.evidence_digest or None,
            task_digest=entry.task_digest or None,
            schema_digest=entry.schema_digest or None,
            tool_policy=entry.tools or None,
            tool_version=entry.tool_version or None,
            search_policy=entry.search_policy,
            searches=list(entry.searches),
            search_request_count=entry.search_count,
            validated=entry.validated,
            cache_key=entry.cache_key or None,
            cached_at=entry.cached_at or None,
            response_digest=entry.response_digest or None,
            origin=origin,
        )
        session.add(call)
        session.flush()
        created.append(call)
        if entry.request_envelope is not None or entry.response_envelope is not None:
            session.add(
                ModelCallEnvelope(
                    model_call_id=call.id,
                    request_envelope=entry.request_envelope,
                    response_envelope=entry.response_envelope,
                    origin=origin,
                )
            )
        cost = CostRecord(
            model_call_id=call.id,
            amount=(
                Decimal(str(entry.dollars)).quantize(Decimal("0.00000001"), rounding=ROUND_CEILING)
                if entry.dollars is not None
                else None
            ),
            currency="USD",
            pricing_source=entry.pricing_source,
            price_snapshot=entry.price_snapshot or None,
            origin=origin,
        )
        session.add(cost)
        logger.info(
            "model_call_pricing_prepared",
            extra={
                "task_id": task_id,
                "origin": origin,
                "stage": entry.stage,
                "sequence": sequence,
                "requested_model": entry.model,
                "served_model": entry.served or None,
                "service_tier": entry.service_tier or None,
                "effort": entry.effort or None,
                "search_request_count": entry.search_count,
                "input_tokens": entry.usage.input_tokens,
                "output_tokens": entry.usage.output_tokens,
                "reasoning_tokens": entry.usage.reasoning_tokens,
                "cache_read_tokens": entry.usage.cache_read_tokens,
                "cache_write_tokens": entry.usage.cache_write_tokens,
                "duration_ms": entry.milliseconds,
                "validated": entry.validated,
                "amount_usd": str(entry.dollars) if entry.dollars is not None else None,
                "pricing_source": cost.pricing_source,
                "transaction_state": "prepared",
            },
        )
    return created


def archive_response_cache(
    session: Session, *, cache_directory: Path, origin: str = "clearscout"
) -> int:
    """Copy the response cache's surviving answers into the workspace, returning how many.

    The cache upserts on key, so an answer is lost the moment its request is
    made again. It is read through ``ResponseCache``, whose own connection sees
    answers that are still only in the write-ahead log.
    """
    if not cache_directory.exists():
        return 0
    held = set(
        session.execute(
            select(ResponseCacheArchive.cache_key, ResponseCacheArchive.stored_at)
        ).all()
    )
    with ResponseCache(enabled=True, directory=cache_directory) as cache:
        entries = list(cache.entries())
    archived = 0
    for key, entry in entries:
        if (key, entry.stored_at) in held:
            continue
        session.add(
            ResponseCacheArchive(
                cache_key=key,
                value=entry.value,
                stored_at=entry.stored_at,
                revision=entry.revision or None,
                package_version=entry.package_version or None,
                origin=origin,
            )
        )
        archived += 1
    return archived


def recorded_served_models(session: Session, workspace_id: str) -> set[str]:
    """Return every model name that answered one of this workspace's recorded calls."""
    rows = session.execute(
        select(func.distinct(func.coalesce(ModelCall.served_model, ModelCall.requested_model)))
        .select_from(ModelCall)
        .where(ModelCall.workspace_id == workspace_id)
    ).scalars()
    return {name for name in rows if name}


def recorded_call_cost(
    session: Session, *predicates: ColumnElement[bool]
) -> tuple[int, int, Decimal | None]:
    """Return (call count, unpriced count, recorded total) for the matched calls.

    A call written without an amount had an unknown price; summing it as zero
    would understate the total, so any unpriced call withholds the total instead
    of understating it, on the ledger's honest-blank rule.
    """
    cost_by_call = (
        select(
            CostRecord.model_call_id.label("model_call_id"),
            func.sum(CostRecord.amount).label("amount"),
        )
        .group_by(CostRecord.model_call_id)
        .subquery()
    )
    call_count, amount, unpriced_count = session.execute(
        select(
            func.count(ModelCall.id),
            func.sum(cost_by_call.c.amount),
            func.count(ModelCall.id) - func.count(cost_by_call.c.amount),
        )
        .select_from(ModelCall)
        .join(cost_by_call, cost_by_call.c.model_call_id == ModelCall.id, isouter=True)
        .where(*predicates)
    ).one()
    count = int(call_count)
    unpriced = int(unpriced_count)
    recorded = None if unpriced else Decimal(str(amount or 0))
    return count, unpriced, recorded


class SpendService:
    """Read recorded model spend for one workspace."""

    def __init__(self, database: Database) -> None:
        """Bind the service to one database."""
        self.database = database

    def evaluation_output_tokens(
        self, workspace_id: str, stage: str, provider: str, model: str
    ) -> list[int]:
        """Read usable paid token samples for this exact configured evaluation stage."""
        with self.database.session() as session:
            return list(
                session.scalars(
                    select(ModelCall.output_tokens).where(
                        ModelCall.workspace_id == workspace_id,
                        ModelCall.stage == stage,
                        ModelCall.provider == provider,
                        ModelCall.requested_model == model,
                        ModelCall.cache_hit.is_(False),
                        ModelCall.validated.is_(True),
                        ModelCall.response_digest.is_not(None),
                        ModelCall.output_tokens > 0,
                    )
                )
            )

    def run_cost_history(self, workspace_id: str) -> list[RunCostHistoryResponse]:
        """Report the cost per unit of work for every paid task type."""
        from clearscout.application.job_search.models import RunCostHistoryResponse
        from clearscout.application.tasks.registry import TASK_SPECS

        with self.database.session() as session:
            history = per_unit_costs(session, workspace_id)
        return [
            RunCostHistoryResponse.model_validate(
                {
                    "task_type": spec.name,
                    "currency": "USD",
                    "unit": history[spec.name].unit,
                    "measured_runs": history[spec.name].measured_runs,
                    "unpriced_runs": history[spec.name].unpriced_runs,
                    "replayed_runs": history[spec.name].replayed_runs,
                    "per_unit_low": history[spec.name].low,
                    "per_unit_median": history[spec.name].median,
                    "per_unit_high": history[spec.name].high,
                }
            )
            for spec in TASK_SPECS
            if spec.paid
        ]

    def task_billing(self, workspace_id: str, task_id: str) -> TaskBillingResponse:
        """Return recorded model-call cost for one workspace-owned task."""
        from clearscout.application.tasks.models import TaskBillingResponse
        from clearscout.infrastructure.persistence.models import BackgroundTask

        with self.database.session() as session:
            task = session.get(BackgroundTask, task_id)
            if task is None or task.workspace_id != workspace_id:
                raise NotFoundError(f"Task {task_id!r} does not exist.")
            owned = (ModelCall.task_id == task_id, ModelCall.workspace_id == workspace_id)
            count, unpriced, actual = recorded_call_cost(session, *owned)
            resent = session.scalar(
                select(func.count(ModelCall.id)).where(
                    *owned, ModelCall.resent_on_standard.is_(True)
                )
            )
        status: Literal["no_model_calls", "priced", "unpriced"] = (
            "no_model_calls" if count == 0 else "unpriced" if unpriced else "priced"
        )
        return TaskBillingResponse(
            task_id=task_id,
            status=status,
            call_count=count,
            unpriced_call_count=unpriced,
            actual_cost_usd=actual,
            resent_on_standard_count=int(resent or 0),
        )

    def spend_summary(self, workspace_id: str, *, recent: int = 10) -> SpendSummaryResponse:
        """Report what this workspace has actually spent on model calls.

        A call with no cost record means the price was not configured for the
        model that served it, not that the call was free, so a total that would
        include one is withheld rather than understated.
        """
        with self.database.session() as session:
            workspace = session.get(Workspace, workspace_id)
            if workspace is None or workspace.archived_at is not None:
                raise NotFoundError(f"Workspace {workspace_id!r} does not exist.")

            rows = session.execute(
                select(
                    ModelCall.id,
                    ModelCall.stage,
                    ModelCall.provider,
                    ModelCall.requested_model,
                    ModelCall.served_model,
                    ModelCall.created_at,
                    ModelCall.cache_hit,
                    ModelCall.input_tokens,
                    ModelCall.output_tokens,
                    ModelCall.cache_read_tokens,
                    ModelCall.cache_write_tokens,
                    ModelCall.reasoning_tokens,
                    ModelCall.duration_ms,
                    ModelCall.completion_reason,
                    CostRecord.amount,
                    CostRecord.pricing_source,
                    CostRecord.price_snapshot,
                )
                .join(CostRecord, CostRecord.model_call_id == ModelCall.id, isouter=True)
                .where(ModelCall.workspace_id == workspace_id)
                .order_by(ModelCall.created_at.desc(), ModelCall.id.desc())
            ).all()

        # Imported here because the wire models pull the whole job-search
        # package, which reaches back into the discovery services that record
        # spend through this module.
        from clearscout.application.job_search.models import SpendSummaryResponse

        return SpendSummaryResponse.model_validate(
            {
                "currency": "USD",
                "call_count": len(rows),
                "unpriced_calls": sum(1 for row in rows if row.amount is None),
                "total_usd": _priced_total([row.amount for row in rows]),
                "by_stage": _grouped_spend(rows, lambda row: row.stage or "unknown"),
                "by_model": _grouped_spend(
                    rows, lambda row: row.served_model or row.requested_model or "unknown"
                ),
                "recent": [_recent_call(row) for row in rows[:recent]],
            }
        )


def _priced_total(amounts: Sequence[Decimal | None]) -> float | None:
    """Sum, or withhold the total when any price was never configured."""
    priced: list[Decimal] = []
    for amount in amounts:
        if amount is None:
            return None
        priced.append(amount)
    return float(sum(priced, Decimal(0)))


def _grouped_spend(rows: Sequence[Any], key: Callable[[Any], str]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        grouped.setdefault(key(row), []).append(row)
    return sorted(
        (
            {
                "name": name,
                "calls": len(items),
                "amount_usd": _priced_total([item.amount for item in items]),
                "unpriced_calls": sum(1 for item in items if item.amount is None),
            }
            for name, items in grouped.items()
        ),
        key=lambda item: (item["amount_usd"] is None, -(item["amount_usd"] or 0)),
    )


def _recent_call(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "stage": row.stage or "unknown",
        "model": row.served_model or row.requested_model or "unknown",
        "provider": row.provider,
        "requested_model": row.requested_model,
        "served_model": row.served_model,
        "amount_usd": float(row.amount) if row.amount is not None else None,
        "pricing_source": row.pricing_source,
        "price_snapshot": row.price_snapshot,
        "cache_hit": bool(row.cache_hit),
        "input_tokens": row.input_tokens,
        "output_tokens": row.output_tokens,
        "cache_read_tokens": row.cache_read_tokens,
        "cache_write_tokens": row.cache_write_tokens,
        "reasoning_tokens": row.reasoning_tokens,
        "duration_ms": row.duration_ms,
        "completion_reason": row.completion_reason,
        "created_at": row.created_at,
    }


@dataclass(frozen=True)
class PerUnitCosts:
    """One paid task type's cost per unit of work across its completed runs."""

    unit: CostUnit
    measured_runs: int
    unpriced_runs: int
    replayed_runs: int
    low: Decimal | None
    median: Decimal | None
    high: Decimal | None


# The unit a task is priced in. A review or a vocabulary run settles near the
# same figure whatever it carried, so both are priced by the calls they made.
_UNIT_NOUNS: dict[str, CostUnit] = {
    "discovery.evaluate_batch": "posting",
    "discovery.relevance_review": "call",
    "search.guided_campaign": "attempt",
    "search.vocabulary": "call",
}
_UNIT_PAYLOAD_KEYS: dict[str, tuple[str, ...]] = {
    "discovery.evaluate_batch": ("pending_discovery_ids", "job_posting_ids"),
}


def per_unit_costs(
    session: Session,
    workspace_id: str,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, PerUnitCosts]:
    """Return every paid task type's cost per unit of work over its completed runs.

    ``provider`` and ``model`` narrow the sample to calls a matching run would
    make, so a rate is not carried over from a model priced differently.

    A run that carries a call nobody priced is counted and left out of the
    sample, because averaging it in as zero would report the work as cheaper.

    A run whose every call was replayed from the response cache paid no provider
    and is left out of the sample as well: it says the work was not done again,
    not what doing it costs.
    """
    from clearscout.application.tasks.registry import TASK_SPECS
    from clearscout.infrastructure.persistence.models import BackgroundTask

    matched = [ModelCall.workspace_id == workspace_id, BackgroundTask.status == "completed"]
    if provider is not None:
        matched.append(ModelCall.provider == provider)
    if model is not None:
        matched.append(func.coalesce(ModelCall.served_model, ModelCall.requested_model) == model)
    rows = session.execute(
        select(
            BackgroundTask.id,
            BackgroundTask.task_type,
            BackgroundTask.payload,
            func.sum(CostRecord.amount),
            func.count(ModelCall.id) - func.count(CostRecord.amount),
            func.count(func.distinct(ModelCall.id)),
            func.count(
                func.distinct(case((ModelCall.cache_hit.is_(True), None), else_=ModelCall.id))
            ),
        )
        .select_from(ModelCall)
        .join(BackgroundTask, BackgroundTask.id == ModelCall.task_id)
        .join(CostRecord, CostRecord.model_call_id == ModelCall.id, isouter=True)
        .where(*matched)
        .group_by(BackgroundTask.id)
    ).all()
    attempts = _campaign_attempts_by_task(session, [row[0] for row in rows])
    measured: dict[str, list[Decimal]] = {}
    unpriced: dict[str, int] = {}
    replayed: dict[str, int] = {}
    for task_id, task_type, payload, amount, unpriced_calls, calls, billed_calls in rows:
        if unpriced_calls or amount is None:
            unpriced[task_type] = unpriced.get(task_type, 0) + 1
            continue
        if not billed_calls:
            replayed[task_type] = replayed.get(task_type, 0) + 1
            continue
        units = _unit_count(task_type, payload, int(calls), attempts.get(task_id, 0))
        per_unit = (Decimal(str(amount)) / units).quantize(Decimal("0.00000001"))
        measured.setdefault(task_type, []).append(per_unit)
    history: dict[str, PerUnitCosts] = {}
    for spec in TASK_SPECS:
        if not spec.paid:
            continue
        priced = sorted(measured.get(spec.name, []))
        history[spec.name] = PerUnitCosts(
            unit=_UNIT_NOUNS.get(spec.name, "run"),
            measured_runs=len(priced),
            unpriced_runs=unpriced.get(spec.name, 0),
            replayed_runs=replayed.get(spec.name, 0),
            low=priced[0] if priced else None,
            median=_median(priced),
            high=priced[-1] if priced else None,
        )
    return history


def _campaign_attempts_by_task(session: Session, task_ids: Sequence[str]) -> dict[str, int]:
    """Return how many recorded campaign attempts each task dispatched."""
    from clearscout.infrastructure.persistence.models import GuidedCampaignAttempt

    if not task_ids:
        return {}
    rows = session.execute(
        select(ModelCall.task_id, func.count(func.distinct(GuidedCampaignAttempt.id)))
        .select_from(GuidedCampaignAttempt)
        .join(ModelCall, ModelCall.id == GuidedCampaignAttempt.model_call_id)
        .where(ModelCall.task_id.in_(task_ids))
        .group_by(ModelCall.task_id)
    ).all()
    return {task_id: int(count) for task_id, count in rows if task_id is not None}


def _unit_count(
    task_type: str, payload: dict[str, Any], model_calls: int, campaign_attempts: int
) -> int:
    """Return the units of work one finished task did, defaulting to the run itself."""
    if task_type == "search.guided_campaign":
        # A campaign with no attempt rows wrote one model call per attempt, so its calls
        # stand in for them.
        return max(campaign_attempts or model_calls, 1)
    if _UNIT_NOUNS.get(task_type) == "call":
        return max(model_calls, 1)
    selected = sum(len(payload.get(key) or []) for key in _UNIT_PAYLOAD_KEYS.get(task_type, ()))
    return max(selected, 1)


def _median(amounts: list[Decimal]) -> Decimal | None:
    """Return the middle value, averaging the middle pair when there is no middle."""
    if not amounts:
        return None
    middle = len(amounts) // 2
    if len(amounts) % 2:
        return amounts[middle]
    return (amounts[middle - 1] + amounts[middle]) / 2
