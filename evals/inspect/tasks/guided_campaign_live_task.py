"""Live guided-campaign task loaded by the bespoke runner.

Only ``scripts/eval.sh --live`` should launch this task. Each case compiles a campaign
from a synthetic parameter set, sends its named attempt as one background hosted-search
call, and scores the searches and picks by the checks the campaign runner applies.
"""

from __future__ import annotations

from pathlib import Path

from evals.inspect.product_questions import ProductQuestion
from evals.inspect.scorers.guided_campaign import campaign_attempt
from evals.inspect.solvers.web_stage_live import guided_campaign_live_solver

TASK_NAME = "guided-campaign-live"
# scripts/eval.sh reads this declaration to estimate the stage this run records.
STAGE = "guided_campaign"
DATASET_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "guided_campaign_cases.jsonl"
PRODUCT_QUESTION = ProductQuestion.CAMPAIGN_SEARCH_COVERAGE


def solver():  # type: ignore[no-untyped-def]
    """Return the live solver callable the runner will invoke per sample."""
    return guided_campaign_live_solver(DATASET_PATH)


def scorer():  # type: ignore[no-untyped-def]
    """Return the campaign-attempt scorer callable the runner will apply."""
    return campaign_attempt()
