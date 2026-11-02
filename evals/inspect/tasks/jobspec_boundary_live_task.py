"""Live jobspec boundary task loaded by the bespoke runner.

Only ``scripts/eval.sh --live`` should launch this task. It extracts each synthetic
posting against a synthetic numbered boundary list and scores the boundary that
would skip it. This is defect discovery on a small n: run it with ``--trials`` and
read hits per case, not a rate.
"""

from __future__ import annotations

from pathlib import Path

from evals.inspect.product_questions import ProductQuestion
from evals.inspect.scorers.jobspec_boundary import boundary_match
from evals.inspect.solvers.jobspec_live import jobspec_boundary_live_solver

TASK_NAME = "jobspec-boundary-live"
# scripts/eval.sh reads this declaration to estimate the stage this run records.
STAGE = "jobspec"
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
DATASET_PATH = FIXTURES / "jobspec_boundary_cases.jsonl"
BOUNDARIES_PATH = FIXTURES / "jobspec_boundaries.json"
PRODUCT_QUESTION = ProductQuestion.POSTING_EXTRACTION_FIDELITY


def solver():  # type: ignore[no-untyped-def]
    """Return the live solver callable the runner will invoke per sample."""
    return jobspec_boundary_live_solver(DATASET_PATH, BOUNDARIES_PATH)


def scorer():  # type: ignore[no-untyped-def]
    """Return the boundary scorer callable the runner will apply."""
    return boundary_match()
