"""Scripted-provider drafting task loaded by the bespoke runner.

Runs the shipped ``tailorcv.application.tailoring.stages.draft.draft`` against a canned
provider and scores attempt one through ``verify.check``. Catches a stale prompt
template, a closed-vocabulary regression, or a verifier that stopped catching a
planted defect; it does not test prompt quality.
"""

from __future__ import annotations

from pathlib import Path

from evals.inspect.product_questions import ProductQuestion
from evals.inspect.scorers.document_checks import verifier_contract
from evals.inspect.solvers.document_scripted import draft_scripted_solver

TASK_NAME = "draft-scripted"
DATASET_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "draft_cases.jsonl"
PRODUCT_QUESTION = ProductQuestion.FACTUAL_DRAFTING


def solver():  # type: ignore[no-untyped-def]
    """Return the scripted solver callable the runner will invoke per sample."""
    return draft_scripted_solver()


def scorer():  # type: ignore[no-untyped-def]
    """Return the declared-failure scorer callable the runner will apply."""
    return verifier_contract()
