"""Whole-posting scripted match task loaded by the bespoke runner.

Sends a multi-requirement ``JobSpec`` through one shipped
``clearscout.application.tailoring.stages.match.match`` call and scores each
requirement by its stable id. Catches template and closed-vocabulary regressions
across a many-requirement posting, including the r01..rNN identity mapping; it
does not test prompt quality.
"""

from __future__ import annotations

from pathlib import Path

from evals.inspect.product_questions import ProductQuestion
from evals.inspect.scorers.match_classification import per_requirement_scripted_contract
from evals.inspect.solvers.clearscout_scripted import clearscout_scripted_solver

TASK_NAME = "match-scripted-multi"
DATASET_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "match_scripted_multi.jsonl"
PRODUCT_QUESTION = ProductQuestion.REQUIREMENT_CLASSIFICATION


def solver():  # type: ignore[no-untyped-def]
    """Return the scripted solver callable the runner will invoke per sample."""
    return clearscout_scripted_solver()


def scorer():  # type: ignore[no-untyped-def]
    """Return the per-requirement scorer callable the runner will apply."""
    return per_requirement_scripted_contract()
