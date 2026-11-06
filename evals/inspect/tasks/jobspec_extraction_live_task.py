"""Live jobspec requirement-extraction task loaded by the bespoke runner.

Only ``scripts/eval.sh --live`` should launch this task. It extracts each synthetic
posting with no stated boundaries and scores the requirements against the case's
expected spans. This is defect discovery on a small n: read misses per case, not a rate.
"""

from __future__ import annotations

from pathlib import Path

from evals.inspect.product_questions import ProductQuestion
from evals.inspect.scorers.jobspec_extraction import requirement_extraction
from evals.inspect.solvers.jobspec_live import jobspec_extraction_live_solver

TASK_NAME = "jobspec-extraction-live"
# scripts/eval.sh reads this declaration to estimate the stage this run records.
STAGE = "jobspec"
DATASET_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "jobspec_extraction_cases.jsonl"
PRODUCT_QUESTION = ProductQuestion.POSTING_EXTRACTION_FIDELITY


def solver():  # type: ignore[no-untyped-def]
    """Return the live solver callable the runner will invoke per sample."""
    return jobspec_extraction_live_solver(DATASET_PATH)


def scorer():  # type: ignore[no-untyped-def]
    """Return the requirement-extraction scorer callable the runner will apply."""
    return requirement_extraction()
