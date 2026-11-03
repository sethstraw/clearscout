"""Estimate a live evaluation run's cost from the workspace's own model_calls history.

Usage: ``python -m evals.inspect.cost_estimate --data-root <path> --stage match
--provider openai [--model <id>] [--effort <level>] --limit N``.

Reads the most recent successful, non-cache-hit production calls for the named stage,
model, and effort, served on the tier a live run sends (flex on OpenAI), from
``cost_records`` joined to ``model_calls``, averages dollars per call, and
multiplies by the selected sample count (``--limit`` or the number of
``--ids``), ``--trials``, ``--arms``, and the calls one sample makes; the
relevance stage, which batches samples, prices the batch calls the ``--task``
run over that ``--split`` would send. Prints the sample size
the estimate is based on. If fewer than five samples exist, refuses to estimate
and exits with a non-zero code so the caller must pass an explicit approval.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.orm import Session

from clearscout.application.config import Effort, ProviderName, Stage
from clearscout.infrastructure.artifacts.runtime import RuntimeStorage
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models.tailoring import CostRecord, ModelCall
from clearscout.shared.costs import ServiceTier
from evals.inspect.runner import SPLITS, selected_samples
from evals.inspect.solvers.clearscout_live import live_settings
from evals.inspect.solvers.relevance_live import review_calls

MIN_SAMPLES = 5
# Every live eval solver bills under an origin with this prefix; production origins never carry it.
EVAL_ORIGIN_PREFIX = "eval."


def _served_on(service_tier: ServiceTier) -> ColumnElement[bool]:
    """Match calls served on ``service_tier``, in the spelling providers report."""
    # OpenAI reports "flex" or "default" and Anthropic "standard" or nothing, so
    # standard history is every call not served on flex.
    flex = ModelCall.service_tier == ServiceTier.FLEX.value
    if service_tier is ServiceTier.FLEX:
        return flex
    return or_(ModelCall.service_tier.is_(None), ~flex)


def _recent_costs(
    session: Session,
    stage: str,
    model: str,
    sample_size: int,
    service_tier: ServiceTier,
    effort: Effort | None,
) -> list[Decimal]:
    query = (
        select(CostRecord.amount)
        .join(ModelCall, CostRecord.model_call_id == ModelCall.id)
        .where(ModelCall.stage == stage)
        .where(ModelCall.requested_model == model)
        .where(~ModelCall.origin.startswith(EVAL_ORIGIN_PREFIX))
    )
    if effort is not None:
        query = query.where(ModelCall.effort == effort.value)
    rows = session.execute(
        query.where(_served_on(service_tier))
        .where(ModelCall.validated.is_(True))
        .where(ModelCall.cache_hit.is_(False))
        .where(CostRecord.amount.is_not(None))
        .where(CostRecord.currency == "USD")
        .order_by(ModelCall.created_at.desc())
        .limit(sample_size)
    ).all()
    return [Decimal(str(row[0])) for row in rows]


def estimate(
    data_root: Path,
    stage: str,
    model: str,
    limit: int | None = None,
    sample_size: int = 20,
    trials: int = 1,
    arms: int = 1,
    ids: Sequence[str] | None = None,
    calls_per_sample: int = 1,
    service_tier: ServiceTier = ServiceTier.STANDARD,
    effort: Effort | None = None,
    paid_calls: int | None = None,
) -> int:
    """Print an estimate and return an exit code: 0 grounded, 2 not grounded.

    A run of ``trials`` per sample pays for every repeat, and a run declaring
    several prompt arms pays for those trials once per arm, so the estimate is
    the per-call average times the samples, trials, arms and calls per sample. A
    run naming ``ids`` pays for exactly that many samples. A stage that batches
    samples passes ``paid_calls``, the calls the whole run sends, instead. An
    ``effort`` of None averages calls at every effort.
    """
    if trials < 1:
        raise ValueError("cost estimate trials must be at least 1")
    if arms < 1:
        raise ValueError("cost estimate arms must be at least 1")
    if ids is not None:
        if limit is not None:
            raise ValueError("cost estimate takes either a sample limit or sample ids")
        samples = len(ids)
    elif limit is None:
        raise ValueError("cost estimate requires a sample limit or sample ids")
    else:
        samples = limit
    storage = RuntimeStorage.resolve(data_root=data_root)
    database = Database(storage.database_url, require_existing=True)
    try:
        with database.session() as session:
            costs = _recent_costs(session, stage, model, sample_size, service_tier, effort)
    finally:
        database.dispose()

    effort_note = f", {effort.value} effort" if effort is not None else ""
    if len(costs) < MIN_SAMPLES:
        print(
            f"Refusing to estimate: only {len(costs)} historical '{stage}' calls on {model} "
            f"at the {service_tier.value} tier{effort_note} with cost. "
            f"Provide --i-approve $X.XX explicitly. This estimate is not history-grounded.",
            file=sys.stderr,
        )
        return 2

    average = sum(costs) / Decimal(len(costs))
    calls = paid_calls if paid_calls is not None else samples * trials * arms * calls_per_sample
    total = average * Decimal(calls)
    scope = f"--ids={','.join(ids)}" if ids is not None else f"--limit={limit}"
    if trials > 1:
        scope += f" --trials={trials}"
    if arms > 1:
        scope += f" --arms={arms}"
    if paid_calls is not None:
        scope += f" in {paid_calls} batched calls"
    elif calls_per_sample > 1:
        scope += f" at {calls_per_sample} calls per sample"
    print(
        f"last {len(costs)} {stage} calls on {model} averaged ${average:.4f}/call, "
        f"N={len(costs)}, {service_tier.value} tier{effort_note}; "
        f"estimated cost for {scope} is ${total:.2f}"
    )
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evals.inspect.cost_estimate",
        description="Estimate a live evaluation run from the workspace's own history.",
    )
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--provider", required=True, type=ProviderName)
    # Absent, the stage's shipped model for that provider is the one the run sends.
    parser.add_argument("--model", default="")
    parser.add_argument("--effort", type=Effort, default=None)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--limit", type=int)
    selection.add_argument("--ids", nargs="+", metavar="ID")
    parser.add_argument("--sample-size", type=int, default=20)
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--arms", type=int, default=1)
    # The relevance stage batches samples, so its price depends on which rows are selected.
    parser.add_argument("--task", default=None)
    parser.add_argument("--split", choices=SPLITS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    stage = Stage(args.stage)
    settings = live_settings(args.provider, stage, args.model, args.effort)
    choice = settings.choice_for_stage(stage)
    # The critic asks the same question critic_trials times for every draft it judges.
    calls_per_sample = settings.critic_trials if stage is Stage.CRITIC else 1
    paid_calls = None
    if stage is Stage.RELEVANCE_REVIEW:
        if args.task is None:
            raise SystemExit("the relevance_review estimate needs --task to price its batches")
        samples = selected_samples(args.task, limit=args.limit, split=args.split, ids=args.ids)
        paid_calls = review_calls(samples, args.trials, args.arms)
    return estimate(
        args.data_root,
        args.stage,
        choice.model,
        args.limit,
        args.sample_size,
        args.trials,
        args.arms,
        args.ids,
        calls_per_sample,
        settings.unattended_tier(),
        choice.effort,
        paid_calls,
    )


if __name__ == "__main__":
    raise SystemExit(main())
