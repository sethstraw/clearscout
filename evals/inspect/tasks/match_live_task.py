"""Live match task loaded by the bespoke runner.

Only ``scripts/eval.sh --live`` should launch this task. The solver refuses to
run without a per-run approval token and an available credential. This is the
only mode that tests prompt-quality regressions and the only one that spends.
"""

from __future__ import annotations

from pathlib import Path

from evals.inspect.product_questions import ProductQuestion
from evals.inspect.scorers.match_classification import classification_match
from evals.inspect.solvers.clearscout_live import clearscout_live_solver

TASK_NAME = "match-live"
# scripts/eval.sh reads this declaration to estimate the stage this run records.
STAGE = "match"
DATASET_PATH = Path(__file__).resolve().parents[2] / "golden" / "match.jsonl"
PRODUCT_QUESTION = ProductQuestion.REQUIREMENT_CLASSIFICATION


def solver():  # type: ignore[no-untyped-def]
    """Return the live solver callable the runner will invoke per sample."""
    return clearscout_live_solver()


def scorer():  # type: ignore[no-untyped-def]
    """Return the classification scorer callable the runner will apply."""
    return classification_match()
