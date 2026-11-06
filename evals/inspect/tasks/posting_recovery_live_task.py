"""Live posting-recovery task loaded by the bespoke runner.

Only ``scripts/eval.sh --live`` should launch this task. Each case sends one cited
hosted search for a posting identity and scores the validated candidates against the
posting's known address. This is defect discovery on a small n: read each miss.
"""

from __future__ import annotations

from pathlib import Path

from evals.inspect.product_questions import ProductQuestion
from evals.inspect.scorers.posting_recovery import recovered_posting
from evals.inspect.solvers.web_stage_live import posting_recovery_live_solver

TASK_NAME = "posting-recovery-live"
# scripts/eval.sh reads this declaration to estimate the stage this run records.
STAGE = "posting_recovery"
DATASET_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "posting_recovery_cases.jsonl"
PRODUCT_QUESTION = ProductQuestion.POSTING_SOURCE_RECOVERY


def solver():  # type: ignore[no-untyped-def]
    """Return the live solver callable the runner will invoke per sample."""
    return posting_recovery_live_solver(DATASET_PATH)


def scorer():  # type: ignore[no-untyped-def]
    """Return the recovered-posting scorer callable the runner will apply."""
    return recovered_posting()
