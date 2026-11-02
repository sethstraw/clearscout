"""Replay-consistency task loaded by the bespoke runner.

Reads the private calibration snapshot named by ``--dataset`` and reports the
classification the workspace database already stores for each requirement. This measures replay
consistency, not prompt quality.
"""

from __future__ import annotations

from evals.inspect.product_questions import ProductQuestion
from evals.inspect.scorers.match_classification import stored_classification_consistency
from evals.inspect.solvers.tailorcv_replay import tailorcv_replay_solver

TASK_NAME = "match-replay"
DATASET_PATH = None
PRODUCT_QUESTION = ProductQuestion.REQUIREMENT_CLASSIFICATION


def solver():  # type: ignore[no-untyped-def]
    """Return the replay solver callable the runner will invoke per sample."""
    return tailorcv_replay_solver()


def scorer():  # type: ignore[no-untyped-def]
    """Return the classification scorer callable the runner will apply."""
    return stored_classification_consistency()
