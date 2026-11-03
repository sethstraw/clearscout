"""Scripted-provider critic task loaded by the bespoke runner.

Runs the shipped ``clearscout.application.tailoring.stages.verify.critique`` against a
canned provider and records the outcome ``verify.critic_finding`` assigns that
verdict. The sample declares that outcome, so this catches a stale rubric template
or a change to the shipped floors; it does not test critic judgment.
"""

from __future__ import annotations

from pathlib import Path

from evals.inspect.product_questions import ProductQuestion
from evals.inspect.scorers.document_checks import critic_contract
from evals.inspect.solvers.document_scripted import critic_scripted_solver

TASK_NAME = "critic-scripted"
DATASET_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "critic_cases.jsonl"
PRODUCT_QUESTION = ProductQuestion.DRAFT_CRITIQUE


def solver():  # type: ignore[no-untyped-def]
    """Return the scripted solver callable the runner will invoke per sample."""
    return critic_scripted_solver()


def scorer():  # type: ignore[no-untyped-def]
    """Return the declared-outcome scorer callable the runner will apply."""
    return critic_contract()
