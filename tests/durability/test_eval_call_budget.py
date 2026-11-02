"""A case declares the calls one draw of each stage makes, and nothing about resampling."""

from __future__ import annotations

import pytest
from evals.case import Case, Expectations, Responses, ScriptedSpec
from evals.scorers import CaseRun, score
from evals.support.inventory import synthetic_inventory

from tailorcv.shared.costs import Ledger


def _run(trials: int, critic_calls: int | None = None) -> CaseRun:
    case = Case(
        name="call-budget",
        proves="a declared call budget is read in draws rather than in billed calls",
        posting="synthetic",
        responses=Responses(jobspec=ScriptedSpec(title="Synthetic Role")),
        expect=Expectations(measured={"model_calls": 4}),
    )
    return CaseRun(
        case=case,
        inventory=synthetic_inventory(),
        ledger=Ledger(),
        calls=("jobspec", "match", "draft", *("critic",) * (critic_calls or trials)),
        critic_trials=trials,
    )


@pytest.mark.parametrize("trials", [1, 3, 5])
def test_a_declared_budget_survives_a_change_to_the_critic_trial_count(trials: int) -> None:
    """Hard-coding the trial count into a case file would rot the moment the knob moves."""
    measured = next(item for item in score(_run(trials)) if item.name == "model_calls")

    assert measured.ok
    assert measured.value == 3 + trials


def test_a_round_that_draws_fewer_critics_than_configured_fails_the_budget() -> None:
    """Rounding a part-drawn round up to a whole one would read as the declared budget."""
    measured = next(item for item in score(_run(3, critic_calls=2)) if item.name == "model_calls")

    assert not measured.ok
