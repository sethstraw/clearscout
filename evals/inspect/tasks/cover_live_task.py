"""Live cover-letter task loaded by the bespoke runner.

Only ``scripts/eval.sh --live`` should launch this task. The solver refuses to
run without per-run approval and an available credential. It writes one letter
against the real provider and scores it through ``verify.check_letter``, so every
failing check is one the product itself would have raised.
"""

from __future__ import annotations

from pathlib import Path

from evals.inspect.product_questions import ProductQuestion
from evals.inspect.scorers.document_checks import verifier_result
from evals.inspect.solvers.document_live import document_live_solver
from tailorcv.application.config import Stage

TASK_NAME = "cover-live"
# scripts/eval.sh reads this declaration to estimate the stage this run records.
STAGE = "cover"
DATASET_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "cover_cases.jsonl"
PRODUCT_QUESTION = ProductQuestion.FACTUAL_DRAFTING


def solver():  # type: ignore[no-untyped-def]
    """Return the live solver callable the runner will invoke per sample."""
    return document_live_solver(Stage(STAGE), DATASET_PATH)


def scorer():  # type: ignore[no-untyped-def]
    """Return the shipped-gate scorer callable the runner will apply."""
    return verifier_result()
