"""Grade a solver classification against an independently reviewed label."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from evals.inspect.labeling import ReviewFamily
from evals.inspect.review_resolution import AcceptedLabel, accepted_labels, row_provenance

_LABEL_UNKNOWN = "UNKNOWN"
_EVIDENCE_LABELS = frozenset({"DIRECT", "TAILORING_ONLY", "TRANSFERABLE"})
_NON_EVIDENCE_LABELS = frozenset({"ASSERTED", "GAP"})


def _expected_labels(sample: dict[str, Any]) -> tuple[AcceptedLabel, ...]:
    """Return the classification and strength pairs the row accepts."""
    metadata = sample.get("metadata") or {}
    classification = next(
        (
            value
            for value in (
                sample.get("target"),
                sample.get("expected_classification"),
                metadata.get("expected_classification"),
            )
            if isinstance(value, list) or (isinstance(value, str) and value.strip())
        ),
        None,
    )
    return accepted_labels(classification, sample.get("expected_strength"))


def _reported(values: Sequence[str | None]) -> str | list[str | None] | None:
    return values[0] if len(values) == 1 else list(values)


def _entry_keys(metadata: dict[str, Any]) -> tuple[list[str], bool]:
    """Return normalized citation ids and whether their wire shape is valid."""
    value = metadata.get("entry_keys")
    if value is None:
        return [], True
    if not isinstance(value, (list, tuple)):
        return [], False
    if not all(isinstance(item, str) and item.strip() for item in value):
        return [], False
    return [item.strip() for item in value], True


def _gap_detail(metadata: dict[str, Any]) -> dict[str, Any]:
    """Return the product's own gap class and note, when the run set them."""
    detail: dict[str, Any] = {}
    for key in ("gap_class", "note"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            detail[key] = value.strip()
    return detail


def _score(accepted: tuple[AcceptedLabel, ...], solver_output: dict[str, Any]) -> dict[str, Any]:
    """Score classification membership and answer-side citations independently.

    Strength agreement is reported beside the pass mark and never moves it.
    """
    classes = [label or "" for label, _ in accepted]
    strengths = [grade for _, grade in accepted]
    answer = str(solver_output.get("completion") or "").strip().upper()
    raw_metadata = solver_output.get("metadata")
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    # Fail closed: an outcome not asserting completeness is not credited, so a
    # requirement the run never established cannot pass as a judged GAP.
    outcome_complete = bool(metadata.get("outcome_complete"))
    entry_keys, citation_shape_valid = _entry_keys(metadata)
    classification_matches = answer in classes and answer != _LABEL_UNKNOWN
    citation_valid: bool | None
    citation_problem = ""

    if not citation_shape_valid:
        citation_valid = False
        citation_problem = "entry_keys must be a list of non-empty strings"
    elif answer in _EVIDENCE_LABELS:
        citation_valid = bool(entry_keys)
        if not citation_valid:
            citation_problem = "no supporting entry was cited"
    elif answer in _NON_EVIDENCE_LABELS:
        citation_valid = not entry_keys
        if not citation_valid:
            citation_problem = "cited evidence is not permitted"
    elif answer == _LABEL_UNKNOWN:
        citation_valid = None if not entry_keys else False
        if entry_keys:
            citation_problem = "UNKNOWN may not cite evidence"
    else:
        citation_valid = False
        citation_problem = "answer is outside the closed classification vocabulary"

    problems = []
    if answer == _LABEL_UNKNOWN:
        problems.append("no stored classification")
    elif not classification_matches:
        problems.append("classification mismatch")
    if citation_problem:
        problems.append(citation_problem)
    if not outcome_complete:
        problems.append("run did not establish this requirement")
    explanation = f"expected={'|'.join(dict.fromkeys(classes))} got={answer}"
    if problems:
        explanation += "; " + "; ".join(problems)
    # A strength graded against another class's label would compare unlike judgments, and
    # ASSERTED and GAP force `none`, so an answer of either has no strength to compare.
    strength_matches: bool | None = None
    if any(strengths) and classification_matches and answer in _EVIDENCE_LABELS:
        answered = str(metadata.get("strength") or "").strip().lower()
        strength_matches = answered in {grade for label, grade in accepted if label == answer}
    return {
        "answer": answer,
        "metadata": {
            "expected": _reported(classes),
            "classification_matches": classification_matches,
            "citation_valid": citation_valid,
            "outcome_complete": outcome_complete,
            "expected_strength": _reported(strengths) if any(strengths) else None,
            "strength_matches": strength_matches,
            **_gap_detail(metadata),
        },
        "value": classification_matches and citation_valid is True and outcome_complete,
        "explanation": explanation,
    }


def classification_match() -> Any:
    """Return the scorer for an independently reviewed classification label."""

    def score(sample: dict[str, Any], solver_output: dict[str, Any]) -> dict[str, Any]:
        provenance = row_provenance(sample, ReviewFamily.MATCH)
        result = _score(_expected_labels(sample), solver_output)
        result["metadata"]["provenance"] = (
            provenance.score_metadata() if provenance is not None else None
        )
        return result

    # The runner checks every row's accepted labels with this before it calls a solver.
    score.validate_sample = _expected_labels  # type: ignore[attr-defined]
    return score


def scripted_classification_contract() -> Any:
    """Return the scorer for a canned provider's explicit response contract."""

    def score(sample: dict[str, Any], solver_output: dict[str, Any]) -> dict[str, Any]:
        metadata = sample.get("metadata")
        expected = metadata.get("scripted_classification") if isinstance(metadata, dict) else None
        if not isinstance(expected, str) or not expected.strip():
            raise ValueError("scripted sample is missing metadata.scripted_classification")
        return _score(accepted_labels(expected, None), solver_output)

    return score


def stored_classification_consistency() -> Any:
    """Return the scorer for a frozen prior ledger classification."""

    def score(sample: dict[str, Any], solver_output: dict[str, Any]) -> dict[str, Any]:
        expected = sample.get("current_classification")
        if not isinstance(expected, str) or not expected.strip():
            raise ValueError("replay sample is missing current_classification")
        return _score(accepted_labels(expected, None), solver_output)

    return score


def _coverage_failure(message: str, expected: dict[str, str]) -> dict[str, Any]:
    """Return a failing score for a whole-posting sample whose outcomes are incoherent."""
    total = len(expected)
    return {
        "value": False,
        "metadata": {"expected_requirements": total},
        "requirements": {"total": total, "passed": 0, "results": []},
        "explanation": message,
    }


def _score_per_requirement(
    expected: dict[str, str], solver_output: dict[str, Any]
) -> dict[str, Any]:
    """Grade a whole-posting sample by stable id, one outcome per requirement.

    Coverage is exact, and a requirement the run could not establish fails even
    when its label reads GAP, so a silent omission is never credited.
    """
    outcomes = solver_output.get("per_requirement")
    if not isinstance(outcomes, list) or not outcomes:
        return _coverage_failure("solver returned no per-requirement outcomes", expected)
    observed_ids = [str(item.get("requirement_id")) for item in outcomes]
    if len(observed_ids) != len(set(observed_ids)):
        return _coverage_failure("solver returned duplicate requirement ids", expected)
    if set(observed_ids) != {str(key) for key in expected}:
        return _coverage_failure(
            "requirement coverage mismatch: "
            f"expected {sorted(expected)}, got {sorted(observed_ids)}",
            expected,
        )
    results: list[dict[str, Any]] = []
    passed = 0
    for item in outcomes:
        requirement_id = str(item.get("requirement_id"))
        want = str(expected[requirement_id]).strip().upper()
        graded = _score(accepted_labels(want, None), item)
        requirement_ok = bool(graded["value"])
        if requirement_ok:
            passed += 1
        results.append(
            {
                "requirement_id": requirement_id,
                "value": requirement_ok,
                "expected": want,
                "classification_matches": graded["metadata"]["classification_matches"],
                "citation_valid": graded["metadata"]["citation_valid"],
                "outcome_complete": graded["metadata"]["outcome_complete"],
                "explanation": graded["explanation"],
            }
        )
    total = len(results)
    return {
        "value": passed == total,
        "metadata": {"expected_requirements": total},
        "requirements": {"total": total, "passed": passed, "results": results},
        "explanation": f"requirements {passed}/{total} passed",
    }


def per_requirement_scripted_contract() -> Any:
    """Return the per-requirement scorer for a canned whole-posting contract."""

    def expected(sample: dict[str, Any]) -> dict[str, str]:
        metadata = sample.get("metadata") or {}
        requested = metadata.get("scripted_matches") if isinstance(metadata, dict) else None
        if not isinstance(requested, dict) or not requested:
            raise ValueError("scripted multi sample is missing metadata.scripted_matches")
        return {str(key): str(value) for key, value in requested.items()}

    def score(sample: dict[str, Any], solver_output: dict[str, Any]) -> dict[str, Any]:
        return _score_per_requirement(expected(sample), solver_output)

    # The runner counts a truncated row's requirements from this, since it has no answer.
    score.expected_requirements = lambda sample: len(expected(sample))  # type: ignore[attr-defined]
    return score
