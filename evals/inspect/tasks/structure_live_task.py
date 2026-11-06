"""Live CV import task loaded by the bespoke runner.

Only ``scripts/eval.sh --live`` should launch this task. It runs production's
import steps over the six synthetic CVs under ``tests/fixtures/cv_formats`` and
grades each against that shape's ``expected.toml``.
"""

from __future__ import annotations

from pathlib import Path

from evals.inspect.product_questions import ProductQuestion
from evals.inspect.scorers.cv_import import import_fidelity
from evals.inspect.solvers.structure_live import structure_live_solver

TASK_NAME = "structure-live"
# scripts/eval.sh reads this declaration to estimate the stage this run records.
STAGE = "structure"
DATASET_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "structure_cases.jsonl"
PRODUCT_QUESTION = ProductQuestion.CV_IMPORT_FIDELITY


def solver():  # type: ignore[no-untyped-def]
    """Return the live solver callable the runner will invoke per sample."""
    return structure_live_solver(DATASET_PATH)


def scorer():  # type: ignore[no-untyped-def]
    """Return the corpus-fidelity scorer callable the runner will apply."""
    return import_fidelity()
