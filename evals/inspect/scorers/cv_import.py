"""Grade one CV import against the fields its corpus ``expected.toml`` names.

The shipped verifier already decides whether content was lost or invented; this
scorer compares its failing checks with the row's declaration, then checks the
fields the program copies verbatim downstream: the name, each role's employer,
title, dates and location in order, the extra section headings, and the dropped
lines. A dropped line the corpus keeps fails; one it drops but the model kept is
only reported.
"""

from __future__ import annotations

from typing import Any

from clearscout.shared.results import Outcome
from evals.support.cv_formats import cv_format, flat

# Each corpus key beside the extraction field that carries it.
ROLE_FIELDS = (
    ("employer", "organisation"),
    ("title", "title"),
    ("dates", "dates"),
    ("location", "location"),
)


def _text(field: Any) -> str | None:
    return str(field["text"]) if isinstance(field, dict) and "text" in field else None


def _failing_checks(output: dict[str, Any]) -> list[str]:
    found = output.get("findings")
    if not isinstance(found, list) or not found:
        raise ValueError("solver returned no import findings")
    return sorted(str(item["check"]) for item in found if item.get("outcome") == Outcome.FAIL.value)


def _role_failures(expected: list[dict[str, Any]], roles: list[dict[str, Any]]) -> list[str]:
    failed = [] if len(roles) == len(expected) else ["role count"]
    for number, (want, got) in enumerate(zip(expected, roles, strict=False), 1):
        failed += [
            f"role {number} {key}"
            for key, field in ROLE_FIELDS
            if key in want and _text(got.get(field)) != want[key]
        ]
    return failed


def _dropped(expected: list[str], output: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return the dropped lines the corpus keeps, and the corpus drops the model kept."""
    wanted = {flat(text) for text in expected}
    dropped = {flat(str(item["text"])) for item in output["dropped"]}
    return sorted(dropped - wanted), sorted(wanted - dropped)


def import_fidelity() -> Any:
    """Return the scorer for a live import of one corpus CV."""

    def score(sample: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
        _, expected = cv_format(str(sample["input"]["shape"]))
        declared = sorted(sample["expect"]["failing_checks"])
        failing = _failing_checks(output)
        extracted = output["extracted"]
        failed = [] if failing == declared else ["failing checks"]
        if _text(extracted.get("name")) != expected["name"]:
            failed.append("name")
        failed += _role_failures(expected["roles"], extracted.get("roles") or [])
        headings = [_text(item.get("heading")) for item in extracted.get("sections") or []]
        if headings != [item["heading"] for item in expected.get("sections", [])]:
            failed.append("section headings")
        lost, kept = _dropped(expected["dropped"], output)
        if lost:
            failed.append("dropped")
        return {
            "value": not failed,
            "failed": failed,
            "failed_checks": failing,
            "metadata": {
                "expected_failing_checks": declared,
                "roles": len(extracted.get("roles") or []),
                "expected_roles": len(expected["roles"]),
                "dropped_content": lost,
                "furniture_kept": kept,
            },
            "explanation": (
                f"failed {', '.join(failed)}; shipped checks failing {failing or ['none']}"
                if failed
                else "every corpus field survived and the shipped checks match"
            ),
        }

    # The live smoke refuses an output lacking any of these fields.
    score.required_fields = ("findings", "extracted", "dropped")  # type: ignore[attr-defined]
    return score
