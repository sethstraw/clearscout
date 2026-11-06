"""Live whole-job match task loaded by the bespoke runner.

Sends one reviewed many-requirement posting through one shipped match call and
grades each requirement against its own blind label. Only
``scripts/eval.sh --live`` should launch it, with the job manifest that binds
the dataset; the approved sample limit is one row per posting.
"""

from __future__ import annotations

from pathlib import Path

from evals.inspect.product_questions import ProductQuestion
from evals.inspect.scorers.match_classification import job_classification_match
from evals.inspect.solvers.clearscout_live import clearscout_live_solver

TASK_NAME = "match-job-live"
# scripts/eval.sh reads this declaration to estimate the stage this run records.
STAGE = "match"
DATASET_PATH = Path(__file__).resolve().parents[2] / "golden" / "match_job.jsonl"
PRODUCT_QUESTION = ProductQuestion.REQUIREMENT_CLASSIFICATION


def solver():  # type: ignore[no-untyped-def]
    """Return the live solver callable the runner will invoke per sample."""
    return clearscout_live_solver()


def scorer():  # type: ignore[no-untyped-def]
    """Return the per-requirement scorer callable the runner will apply."""
    return job_classification_match()
