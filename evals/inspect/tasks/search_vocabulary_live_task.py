"""Live search-vocabulary task loaded by the bespoke runner.

Only ``scripts/eval.sh --live`` should launch this task. It proposes vocabulary for
each synthetic CV and parameter set and scores the raw titles and archetypes against
the case's labels. This is defect discovery on a small n: read proposals per case.
"""

from __future__ import annotations

from pathlib import Path

from evals.inspect.product_questions import ProductQuestion
from evals.inspect.scorers.search_vocabulary import proposal_usefulness
from evals.inspect.solvers.search_vocabulary_live import search_vocabulary_live_solver

TASK_NAME = "search-vocabulary-live"
# scripts/eval.sh reads this declaration to estimate the stage this run records.
STAGE = "search_vocabulary"
DATASET_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "search_vocabulary_cases.jsonl"
PRODUCT_QUESTION = ProductQuestion.SEARCH_VOCABULARY_PROPOSAL_USEFULNESS


def solver():  # type: ignore[no-untyped-def]
    """Return the live solver callable the runner will invoke per sample."""
    return search_vocabulary_live_solver(DATASET_PATH)


def scorer():  # type: ignore[no-untyped-def]
    """Return the proposal-usefulness scorer callable the runner will apply."""
    return proposal_usefulness()
