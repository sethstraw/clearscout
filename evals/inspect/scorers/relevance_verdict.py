"""Grade a relevance review on its recommendation and its four classification axes.

Citations are not scored on their own: the schema closes CV evidence ids over the
inventory, and local validation refuses a posting span the batch never sent or
another posting owns. That rejection fails the mark.
"""

from __future__ import annotations

from typing import Any

from evals.inspect.relevance_review_dataset import RelevanceReviewLabel
from evals.support.relevance import AXES

# `_validate_one` nulls the recommendation and all four axes when the model
# answers this outcome, so the product states that decision through the outcome
# and a row expecting it is scored on the outcome instead.
INSUFFICIENT_OUTCOME = "insufficient_posting_text"
INSUFFICIENT_RECOMMENDATION = "insufficient_evidence"


def _observed_recommendation(solver_output: dict[str, Any]) -> str:
    stated = str(solver_output.get("completion") or "").strip()
    if solver_output.get("outcome") == INSUFFICIENT_OUTCOME:
        return INSUFFICIENT_RECOMMENDATION
    return stated


def _axis_comparison(
    label: RelevanceReviewLabel, solver_output: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    observed = solver_output.get("axes")
    observed = observed if isinstance(observed, dict) else {}
    return {
        name: {
            "expected": getattr(label.expected_axes, name),
            "observed": observed.get(name),
            "matches": getattr(label.expected_axes, name) == observed.get(name),
        }
        for name in AXES
    }


def recommendation_match() -> Any:
    """Return the scorer for an independently reviewed fit recommendation."""

    def score(sample: dict[str, Any], solver_output: dict[str, Any]) -> dict[str, Any]:
        label = RelevanceReviewLabel.from_row(sample)
        expected = label.expected_recommendation
        observed = _observed_recommendation(solver_output)
        failure = solver_output.get("validation_failure")
        matches = observed == expected
        nulled = solver_output.get("outcome") == INSUFFICIENT_OUTCOME
        axes_applicable = failure is None and not nulled
        axes = _axis_comparison(label, solver_output) if axes_applicable else {}
        # The two labeller passes split over a low-confidence row, so its axis
        # reference is one adjudication rather than an agreed label.
        axes_scored = label.axes_confidence == "high" if axes_applicable else None
        differed = [name for name, row in axes.items() if not row["matches"]]
        explanation = f"expected={expected} got={observed}"
        if failure:
            explanation += (
                f"; validation rejected the result: {failure.get('reason')}"
                "; the discarded answer states no axes"
            )
        elif nulled:
            explanation += "; the program nulls the axes for this outcome"
        if differed:
            explanation += "; axes differed: " + ", ".join(differed)
        if axes and not axes_scored:
            explanation += "; the labellers split on these axes, so they are recorded, not scored"
        return {
            "value": matches and failure is None and not (axes_scored and differed),
            "metadata": {
                "expected": expected,
                "recommendation_matches": matches,
                "axes": axes,
                "axes_matched": (
                    sum(1 for row in axes.values() if row["matches"]) if axes_applicable else None
                ),
                "axes_scored": axes_scored,
                "axes_confidence": label.axes_confidence,
                "provenance": (
                    label.provenance.score_metadata() if label.provenance is not None else None
                ),
            },
            "explanation": explanation,
        }

    # The runner checks every row's label with this before it calls a solver.
    score.validate_sample = RelevanceReviewLabel.from_row  # type: ignore[attr-defined]
    return score
