"""Live relevance-review task loaded by the bespoke runner.

Only ``scripts/eval.sh --live`` should launch this task. The solver refuses to
run without per-run approval and an available credential. It reviews the selected
postings in the batches production would send through the shipped stage and
scores each recommendation against the frozen reviewed label.
"""

from __future__ import annotations

from pathlib import Path

from evals.inspect.product_questions import ProductQuestion
from evals.inspect.scorers.relevance_verdict import recommendation_match
from evals.inspect.solvers.relevance_live import relevance_live_solver

TASK_NAME = "relevance-review-live"
# scripts/eval.sh reads this declaration to estimate the stage this run records.
STAGE = "relevance_review"
DATASET_PATH = Path(__file__).resolve().parents[2] / "golden" / "relevance_review.jsonl"
PRODUCT_QUESTION = ProductQuestion.FIT_RECOMMENDATION


def solver():  # type: ignore[no-untyped-def]
    """Return the live solver the runner hands each repeat's batch of samples."""
    return relevance_live_solver(DATASET_PATH)


def scorer():  # type: ignore[no-untyped-def]
    """Return the recommendation scorer callable the runner will apply."""
    return recommendation_match()
