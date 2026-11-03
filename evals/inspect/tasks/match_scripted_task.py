"""Scripted-provider match task loaded by the bespoke runner.

Runs the shipped ``clearscout.application.tailoring.stages.match.match`` against a canned in-process
provider. Catches template and closed-vocabulary regressions; does not test
prompt quality.
"""

from __future__ import annotations

from pathlib import Path

from evals.inspect.product_questions import ProductQuestion
from evals.inspect.scorers.match_classification import scripted_classification_contract
from evals.inspect.solvers.clearscout_scripted import clearscout_scripted_solver

TASK_NAME = "match-scripted"
DATASET_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "match_scripted.jsonl"
PRODUCT_QUESTION = ProductQuestion.REQUIREMENT_CLASSIFICATION


def solver():  # type: ignore[no-untyped-def]
    """Return the scripted solver callable the runner will invoke per sample."""
    return clearscout_scripted_solver()


def scorer():  # type: ignore[no-untyped-def]
    """Return the classification scorer callable the runner will apply."""
    return scripted_classification_contract()
