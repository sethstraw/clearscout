"""Live two-attempt drafting task loaded by the bespoke runner.

Only ``scripts/eval.sh --live`` should launch this task. It writes resume attempt
one against the real provider and, when the shipped checks fail it, rewrites it
once through production's redraft loop. The score is the kept attempt's, and each
row also reports how attempt one scored. The critic is not asked, so only the
draft stage is paid.
"""

from __future__ import annotations

from pathlib import Path

from clearscout.application.config import Stage
from evals.inspect.product_questions import ProductQuestion
from evals.inspect.scorers.document_checks import verifier_result
from evals.inspect.solvers.document_live import document_live_solver

TASK_NAME = "draft-redraft-live"
# scripts/eval.sh reads this declaration to estimate the stage this run records.
STAGE = "draft"
DATASET_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "draft_cases.jsonl"
PRODUCT_QUESTION = ProductQuestion.FACTUAL_DRAFTING
DRAFT_ATTEMPTS = 2


def solver():  # type: ignore[no-untyped-def]
    """Return the live solver callable the runner will invoke per sample."""
    return document_live_solver(Stage(STAGE), DATASET_PATH, draft_attempts=DRAFT_ATTEMPTS)


def scorer():  # type: ignore[no-untyped-def]
    """Return the shipped-gate scorer callable the runner will apply."""
    return verifier_result(redrafting=True)
