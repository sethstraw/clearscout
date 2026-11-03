"""Grade a document attempt by what the shipped deterministic verifier found.

No scorer here decides whether a line is good. ``verify.check`` and
``verify.check_letter`` already decide that, and these scorers only read their
findings: a scripted sample declares the checks its planted defect must fail, and
a live sample passes when the product's own gate is clear. The critic family
records the shipped verdict instead of inventing a quality threshold.
"""

from __future__ import annotations

from typing import Any

from clearscout.application.tailoring.stages.verify import FUNCTION_WORDS, doubted_span
from clearscout.domain.identity import normalize_text
from clearscout.shared.results import Outcome

_CRITIC_OUTCOMES = frozenset(outcome.value for outcome in Outcome)


def _about(claim: str, line: str) -> bool:
    """Return whether a doubt's words lie inside the line or contain it, sharing its content."""
    span, folded_line = normalize_text(doubted_span(claim)), normalize_text(line)
    # A lone term such as a tool name also appears in lines nobody doubted.
    shared = set(span.split()) & set(folded_line.split()) - FUNCTION_WORDS
    return len(shared) >= 2 and (span in folded_line or folded_line in span)


def _by_outcome(solver_output: dict[str, Any], outcome: Outcome) -> list[str]:
    found = solver_output.get("findings")
    if not isinstance(found, list) or not found:
        raise ValueError("solver returned no verifier findings")
    return sorted(
        str(item.get("check"))
        for item in found
        if isinstance(item, dict) and item.get("outcome") == outcome.value
    )


def _detail(solver_output: dict[str, Any], checks: list[str]) -> str:
    found = solver_output.get("findings")
    rows = found if isinstance(found, list) else []
    details = {
        str(item.get("check")): str(item.get("detail", ""))
        for item in rows
        if isinstance(item, dict)
    }
    return "; ".join(f"{check}: {details.get(check, '')}" for check in checks)


def _declared(sample: dict[str, Any], key: str, *, required: bool) -> list[str] | None:
    expect = sample.get("expect")
    if not isinstance(expect, dict) or key not in expect:
        if required:
            raise ValueError(f"a scripted document sample must declare expect.{key}")
        return None
    declared = expect[key]
    if not isinstance(declared, list) or any(not isinstance(item, str) for item in declared):
        raise ValueError(f"expect.{key} must be a list of check names")
    return sorted(declared)


def _score(failed: list[str], warned: list[str], solver_output: dict[str, Any]) -> dict[str, Any]:
    return {
        "failed_checks": failed,
        "warned_checks": warned,
        # "detail" is the failed-check detail the runner's readers already expect;
        # a warned check needs its own key or its reason reaches no one.
        "checks": {
            "failed": failed,
            "warned": warned,
            "detail": _detail(solver_output, failed),
            "warned_detail": _detail(solver_output, warned),
        },
    }


def verifier_contract() -> Any:
    """Return the scorer for a canned answer whose failing checks the sample declares.

    ``expect.warning_checks`` is compared only when a sample states it, which is
    how a case about a check that warns rather than fails asserts anything.
    """

    def score(sample: dict[str, Any], solver_output: dict[str, Any]) -> dict[str, Any]:
        expected = _declared(sample, "failing_checks", required=True) or []
        expected_warnings = _declared(sample, "warning_checks", required=False)
        failed = _by_outcome(solver_output, Outcome.FAIL)
        warned = _by_outcome(solver_output, Outcome.WARN)
        explanation = f"expected failures {expected or ['none']}, got {failed or ['none']}"
        if failed:
            explanation += f"; {_detail(solver_output, failed)}"
        matched = failed == expected and (expected_warnings is None or warned == expected_warnings)
        if expected_warnings is not None and warned != expected_warnings:
            explanation += f"; expected warnings {expected_warnings}, got {warned}"
        return {
            **_score(failed, warned, solver_output),
            "value": matched,
            "metadata": {
                "expected_failing_checks": expected,
                "expected_warning_checks": expected_warnings,
            },
            "explanation": explanation,
        }

    return score


def verifier_result() -> Any:
    """Return the scorer for a live attempt, which passes when the shipped gate is clear."""

    def score(sample: dict[str, Any], solver_output: dict[str, Any]) -> dict[str, Any]:
        del sample
        failed = _by_outcome(solver_output, Outcome.FAIL)
        warned = _by_outcome(solver_output, Outcome.WARN)
        explanation = (
            f"{len(failed)} check(s) failed: {_detail(solver_output, failed)}"
            if failed
            else "every shipped check passed"
        )
        return {
            **_score(failed, warned, solver_output),
            "value": not failed,
            "metadata": {},
            "explanation": explanation,
        }

    return score


def _verdict_outcome(solver_output: dict[str, Any]) -> str:
    outcome = str(solver_output.get("completion") or "")
    if outcome not in _CRITIC_OUTCOMES:
        raise ValueError(f"critic solver returned an unknown shipped outcome: {outcome!r}")
    return outcome


def _verdict_summary(solver_output: dict[str, Any]) -> dict[str, Any]:
    verdict = solver_output.get("verdict")
    if not isinstance(verdict, dict):
        raise ValueError("critic solver returned no verdict")
    finding = solver_output.get("finding")
    return {
        "verdict": verdict,
        "finding_detail": str(finding.get("detail", "")) if isinstance(finding, dict) else "",
    }


def critic_contract() -> Any:
    """Return the scorer for a canned verdict whose shipped outcome the sample declares."""

    def score(sample: dict[str, Any], solver_output: dict[str, Any]) -> dict[str, Any]:
        expect = sample.get("expect")
        expected = expect.get("critic_outcome") if isinstance(expect, dict) else None
        if expected not in _CRITIC_OUTCOMES:
            raise ValueError("a scripted critic sample must declare expect.critic_outcome")
        observed = _verdict_outcome(solver_output)
        summary = _verdict_summary(solver_output)
        return {
            "value": observed == expected,
            "critic": {"outcome": observed, **summary},
            "metadata": {"expected_critic_outcome": expected},
            "explanation": f"expected {expected}, got {observed}; {summary['finding_detail']}",
        }

    return score


def critic_doubt_contract() -> Any:
    """Return the scorer for whether a live critic doubted a claim, as the case constructs it.

    Whether a document carries a planted claim is a fact of how the case was
    built. The six quality axes are a rubric judgment, so a clean document can
    score ``warn`` on an ordinary axis without the critic being wrong. Scoring
    the doubt keeps a run measuring what the fixture actually controls, and
    penalizes a critic that doubts everything and one that doubts nothing alike.
    The shipped outcome is compared with the declared one beside the pass mark.
    """

    def score(sample: dict[str, Any], solver_output: dict[str, Any]) -> dict[str, Any]:
        declared, expected_outcome, planted = _declared_doubt(sample)
        outcome = _verdict_outcome(solver_output)
        summary = _verdict_summary(solver_output)
        claims = summary["verdict"].get("unsupported_claims") or []
        # A planted case is caught only by a doubt about its planted line.
        doubted = any(
            planted is None or _about(str(claim.get("claim", "")), planted["line"])
            for claim in claims
        )
        boundaries = [str(claim.get("boundary", "")) for claim in claims]
        return {
            "value": doubted == declared,
            "critic": {"outcome": outcome, **summary},
            "metadata": {
                "expected_unsupported_claim": declared,
                "observed_boundaries": boundaries,
                "expected_critic_outcome": expected_outcome,
                "outcome_matches": outcome == expected_outcome,
            },
            "explanation": (
                f"expected doubt={declared}, got doubt={doubted}"
                f"{' via ' + ', '.join(boundaries) if boundaries else ''}"
            ),
        }

    # The runner checks every row's declarations with this before it calls a solver.
    score.validate_sample = _declared_doubt  # type: ignore[attr-defined]
    return score


def _declared_doubt(sample: dict[str, Any]) -> tuple[bool, str, dict[str, Any] | None]:
    """Return a live critic row's declared doubt, outcome, and planted line, or refuse it."""
    expect = sample.get("expect")
    declared = expect.get("unsupported_claim") if isinstance(expect, dict) else None
    if not isinstance(declared, bool):
        raise ValueError("a live critic sample must declare expect.unsupported_claim")
    expected_outcome = expect.get("critic_outcome") if isinstance(expect, dict) else None
    if expected_outcome not in _CRITIC_OUTCOMES:
        raise ValueError("a live critic sample must declare expect.critic_outcome")
    metadata = sample.get("metadata")
    planted = metadata.get("planted") if isinstance(metadata, dict) else None
    if planted is not None and not (
        isinstance(planted, dict) and isinstance(planted.get("line"), str)
    ):
        raise ValueError("a live critic sample's metadata.planted must name its line")
    return declared, str(expected_outcome), planted
