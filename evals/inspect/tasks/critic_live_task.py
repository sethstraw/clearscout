"""Live critic task loaded by the bespoke runner.

Only ``scripts/eval.sh --live`` should launch this task. The solver refuses to
run without per-run approval and an available credential. Each sample's document
is assembled offline from its own canned draft, so the run measures the critic
rather than a fresh draft, and each sample is scored against the shipped outcome
it declares. A clean document and a document carrying a planted claim are both in
the set, so a critic that answered the same way every time would fail here.
"""

from __future__ import annotations

from pathlib import Path

from evals.inspect.product_questions import ProductQuestion
from evals.inspect.scorers.document_checks import critic_doubt_contract
from evals.inspect.solvers.document_live import document_live_solver
from tailorcv.application.config import Stage

TASK_NAME = "critic-live"
SCORE_KIND = "judged"
# scripts/eval.sh reads this declaration to estimate the stage this run records.
STAGE = "critic"
DATASET_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "critic_cases.jsonl"
PRODUCT_QUESTION = ProductQuestion.DRAFT_CRITIQUE


def solver():  # type: ignore[no-untyped-def]
    """Return the live solver callable the runner will invoke per sample."""
    return document_live_solver(Stage(STAGE), DATASET_PATH)


def scorer():  # type: ignore[no-untyped-def]
    """Return the scorer that judges whether the critic doubted what its sample plants."""
    return critic_doubt_contract()
