"""Grade the stated boundary a jobspec extraction fired against the case's expected one.

An expected boundary of null means the posting must survive. A fired boundary on
such a case is a false positive, a missed one a false negative, and a different
number a wrong boundary.
"""

from __future__ import annotations

from typing import Any

FALSE_POSITIVE = "false_positive"
FALSE_NEGATIVE = "false_negative"
WRONG_BOUNDARY = "wrong_boundary"


def expected_boundary(sample: dict[str, Any]) -> int | None:
    """Return a row's expected boundary number, refusing a malformed label."""
    if "expected_boundary" not in sample or not str(sample.get("why") or "").strip():
        raise ValueError(f"sample {sample.get('id')!r} needs expected_boundary and why")
    expected = sample["expected_boundary"]
    if expected is not None and (
        isinstance(expected, bool) or not isinstance(expected, int) or expected < 1
    ):
        raise ValueError(f"sample {sample.get('id')!r} expected_boundary must be null or >= 1")
    return expected


def boundary_error(expected: int | None, observed: int | None) -> str | None:
    """Name how an observed boundary missed the expected one, or None when it matched."""
    if observed == expected:
        return None
    if expected is None:
        return FALSE_POSITIVE
    if observed is None:
        return FALSE_NEGATIVE
    return WRONG_BOUNDARY


def boundary_match() -> Any:
    """Return the scorer for the boundary one extraction fired."""

    def score(sample: dict[str, Any], solver_output: dict[str, Any]) -> dict[str, Any]:
        expected = expected_boundary(sample)
        observed = solver_output.get("boundary")
        observed = observed if isinstance(observed, int) else None
        error = boundary_error(expected, observed)
        return {
            "value": error is None,
            "metadata": {
                "expected_boundary": expected,
                "observed_boundary": observed,
                "boundary_error": error,
            },
            "explanation": f"expected={expected or 'none'} got={observed or 'none'}",
        }

    # The runner checks every row's label with this before it calls a solver.
    score.validate_sample = expected_boundary  # type: ignore[attr-defined]
    return score
