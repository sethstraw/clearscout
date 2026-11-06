"""Live vision smoke loaded by the bespoke runner.

Only ``scripts/eval.sh --live`` should launch this task. It renders a synthetic
CV and a synthetic posting to PNG locally, reads each through production's
vision reader, and scores word recall against the source text.
"""

from __future__ import annotations

from pathlib import Path

from evals.inspect.product_questions import ProductQuestion
from evals.inspect.scorers.vision_transcript import token_recall
from evals.inspect.solvers.vision_live import vision_live_solver

TASK_NAME = "vision-live"
# scripts/eval.sh reads this declaration to estimate the stage this run records.
STAGE = "ingest"
DATASET_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "vision_cases.jsonl"
PRODUCT_QUESTION = ProductQuestion.SCREENSHOT_TRANSCRIPTION


def solver():  # type: ignore[no-untyped-def]
    """Return the live solver callable the runner will invoke per sample."""
    return vision_live_solver(DATASET_PATH)


def scorer():  # type: ignore[no-untyped-def]
    """Return the word-recall scorer callable the runner will apply."""
    return token_recall()
