"""Grade the requirements a jobspec extraction read out of a posting against the case's own.

An extracted requirement matches an expected one when either quote contains the other
after span normalization and the shorter carries at least half the longer's content
words; a quote recalls only the expected span it mostly states, and an expected span
marked ``whole`` must be quoted entire. A quote the posting does not contain fails the
row outright, and so does any recall or precision miss, an importance disagreement on a
matched requirement, or a quote word that no requirement text citing it carries.
Responsibilities the extraction reads out of duty lists are not counted against precision.
A case that labels its gates also fails on a labelled gate missed or read with another
kind or blocking, a gate quote the posting lacks, or an unlabelled gate that blocks.
"""

from __future__ import annotations

import re
from typing import Any

from clearscout.application.tailoring.elicit.vocabulary import FUNCTION_WORDS
from clearscout.application.tailoring.stages.jobspec import Importance
from clearscout.application.tailoring.stages.spans import normalize
from clearscout.domain.requirements import GATE_KINDS

_WORD = re.compile(r"[a-z0-9+#]+")
# The importance field carries these, so a requirement text may drop them.
_STANCE_WORDS = frozenset(
    {
        "must",
        "plus",
        "love",
        "required",
        "require",
        "preferred",
        "prefer",
        "bonus",
        "ideally",
        "nice",
        "essential",
        "minimum",
    }
)
_OVERLAP_FLOOR = 0.5

MISSED = "missed"
MATCHED = "matched"
IMPORTANCE_MISMATCH = "importance_mismatch"
FALSE_POSITIVE = "false_positive"
BOILERPLATE = "boilerplate"
NOT_IN_POSTING = "not_in_posting"
RESPONSIBILITY = "responsibility"
KIND_MISMATCH = "kind_mismatch"
BLOCKING_MISMATCH = "blocking_mismatch"
UNLABELED_GATE = "unlabeled"


def expected_gates(sample: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Return a row's labelled gates, or None when it labels none, refusing a bad label."""
    case = sample.get("id")
    gates = sample.get("gates")
    if gates is None:
        return None
    if not isinstance(gates, list):
        raise ValueError(f"sample {case!r} needs a gates list")
    posting = normalize(str(sample.get("input") or ""))
    for gate in gates:
        if (
            not isinstance(gate, dict)
            or gate.get("kind") not in GATE_KINDS
            or not isinstance(gate.get("blocking"), bool)
        ):
            raise ValueError(f"sample {case!r} has a gate without a kind and blocking: {gate!r}")
        if not gate.get("span") or normalize(str(gate["span"])) not in posting:
            raise ValueError(f"sample {case!r} labels a gate its posting lacks: {gate!r}")
    return gates


def expected_requirements(sample: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a row's expected requirements, refusing a label the posting cannot support."""
    case = sample.get("id")
    expected_gates(sample)
    if not str(sample.get("why") or "").strip():
        raise ValueError(f"sample {case!r} needs a why")
    expected = sample.get("expected")
    forbidden = sample.get("forbidden", [])
    if not isinstance(expected, list) or not isinstance(forbidden, list):
        raise ValueError(f"sample {case!r} needs expected and forbidden lists")
    posting = normalize(str(sample.get("input") or ""))
    for item in expected:
        if not isinstance(item, dict) or item.get("importance") not in {
            Importance.MUST_HAVE.value,
            Importance.NICE_TO_HAVE.value,
        }:
            raise ValueError(f"sample {case!r} has an expected item without a stance")
        if normalize(str(item.get("span") or "")) not in posting or not item.get("span"):
            raise ValueError(f"sample {case!r} expects a span its posting lacks: {item!r}")
    for span in forbidden:
        if not isinstance(span, str) or not span or normalize(span) not in posting:
            raise ValueError(f"sample {case!r} forbids a span its posting lacks: {span!r}")
    return expected


def _content_words(text: str) -> set[str]:
    words = set()
    for word in _WORD.findall(normalize(text).lower()):
        stem = word
        for suffix in ("ing", "ed", "s"):
            if len(word) > len(suffix) + 3 and word.endswith(suffix):
                stem = word[: -len(suffix)]
                break
        if len(stem) > 1 and stem not in FUNCTION_WORDS | _STANCE_WORDS:
            words.add(stem)
    return words


def _overlaps(quote: str, span: str) -> bool:
    if not quote or not (quote in span or span in quote):
        return False
    shorter, longer = sorted((_content_words(quote), _content_words(span)), key=len)
    return len(shorter) >= _OVERLAP_FLOOR * len(longer)


def _matches(quote: str, item: dict[str, Any]) -> bool:
    span = normalize(str(item["span"]))
    return span in quote if item.get("whole") else _overlaps(quote, span)


def _share(quote: str, item: dict[str, Any]) -> float:
    span = _content_words(str(item["span"]))
    return len(span & _content_words(quote)) / max(len(_content_words(quote)), 1)


def _text_fidelity(extracted: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return each quote whose content words no requirement text sharing it carries."""
    texts: dict[str, tuple[str, set[str]]] = {}
    for requirement in extracted:
        quote = str(requirement.get("quote") or "")
        if quote.strip():
            _, carried = texts.setdefault(normalize(quote), (quote, set()))
            carried.update(_content_words(str(requirement.get("text") or "")))
    return [
        {"quote": quote, "missing": sorted(_content_words(quote) - carried)}
        for quote, carried in texts.values()
        if _content_words(quote) - carried
    ]


def _extracted_outcome(
    requirement: dict[str, Any], posting: str, expected: list[dict[str, Any]], forbidden: list[str]
) -> tuple[str, list[int]]:
    """Return how one extracted requirement scored and the expected items it matched."""
    quote = normalize(str(requirement.get("quote") or requirement.get("text") or ""))
    if quote not in posting or not quote:
        return NOT_IN_POSTING, []
    if any(_overlaps(quote, normalize(span)) for span in forbidden):
        return BOILERPLATE, []
    hits = [index for index, item in enumerate(expected) if _matches(quote, item)]
    if hits:
        # A quote bundling several expected spans recalls only the one it mostly states.
        return MATCHED, [max(hits, key=lambda index: _share(quote, expected[index]))]
    if requirement.get("importance") == Importance.RESPONSIBILITY.value:
        return RESPONSIBILITY, []
    return FALSE_POSITIVE, []


def _gate_outcome(found: list[dict[str, Any]], label: dict[str, Any]) -> str:
    if not found:
        return MISSED
    if sorted({str(gate.get("kind")) for gate in found}) != [label["kind"]]:
        return KIND_MISMATCH
    if sorted({bool(gate.get("blocking")) for gate in found}) != [label["blocking"]]:
        return BLOCKING_MISMATCH
    return MATCHED


def _scored_gates(
    extracted: list[dict[str, Any]], labels: list[dict[str, Any]], posting: str
) -> dict[str, Any]:
    """Return each gate's outcome, and whether every labelled gate agreed on kind and blocking."""
    matched_by: dict[int, list[dict[str, Any]]] = {}
    extracted_rows = []
    for gate in extracted:
        quote = normalize(str(gate.get("quote") or gate.get("text") or ""))
        hits = [index for index, label in enumerate(labels) if quote and _matches(quote, label)]
        if not quote or quote not in posting:
            outcome = NOT_IN_POSTING
        elif hits:
            outcome = MATCHED
            best = max(hits, key=lambda index: _share(quote, labels[index]))
            matched_by.setdefault(best, []).append(gate)
        else:
            outcome = UNLABELED_GATE
        extracted_rows.append({**gate, "outcome": outcome})
    expected_rows = [
        {**label, "outcome": _gate_outcome(matched_by.get(index, []), label)}
        for index, label in enumerate(labels)
    ]
    # An unlabelled gate is a reading the labels did not foresee.
    # One that blocks can skip a posting, so it fails the row.
    unlabelled_blocking = sum(
        row["outcome"] == UNLABELED_GATE and bool(row.get("blocking")) for row in extracted_rows
    )
    not_in_posting = sum(row["outcome"] == NOT_IN_POSTING for row in extracted_rows)
    agreed = sum(row["outcome"] == MATCHED for row in expected_rows)
    return {
        "passed": agreed == len(labels) and not not_in_posting and not unlabelled_blocking,
        "agreed": agreed,
        "not_in_posting": not_in_posting,
        "unlabelled_blocking": unlabelled_blocking,
        "expected": expected_rows,
        "extracted": extracted_rows,
    }


def requirement_extraction() -> Any:
    """Return the scorer for the requirements one extraction read out of its posting."""

    def score(sample: dict[str, Any], solver_output: dict[str, Any]) -> dict[str, Any]:
        expected = expected_requirements(sample)
        forbidden = [str(span) for span in sample.get("forbidden", [])]
        posting = normalize(str(sample["input"]))
        extracted = list(solver_output.get("requirements") or [])
        matched_by: dict[int, list[dict[str, Any]]] = {}
        extracted_rows = []
        for requirement in extracted:
            outcome, hits = _extracted_outcome(requirement, posting, expected, forbidden)
            for index in hits:
                matched_by.setdefault(index, []).append(requirement)
            extracted_rows.append({**requirement, "outcome": outcome})
        expected_rows = []
        for index, item in enumerate(expected):
            found = matched_by.get(index, [])
            importances = sorted({str(match.get("importance")) for match in found})
            outcome = (
                MISSED
                if not found
                else MATCHED
                if importances == [item["importance"]]
                else IMPORTANCE_MISMATCH
            )
            expected_rows.append({**item, "outcome": outcome, "extracted_importance": importances})
        outcomes = [row["outcome"] for row in extracted_rows]
        scored = [outcome for outcome in outcomes if outcome != RESPONSIBILITY]
        true_positive = outcomes.count(MATCHED)
        recalled = sum(row["outcome"] != MISSED for row in expected_rows)
        agreed = sum(row["outcome"] == MATCHED for row in expected_rows)
        precision = true_positive / len(scored) if scored else 1.0
        recall = recalled / len(expected) if expected else 1.0
        importance_agreement = agreed / recalled if recalled else 1.0
        not_in_posting = outcomes.count(NOT_IN_POSTING)
        fidelity = _text_fidelity(extracted)
        passed = not_in_posting == 0 and precision == 1.0 and recall == 1.0 and not fidelity
        result: dict[str, Any] = {
            "value": passed and importance_agreement == 1.0,
            "requirements": {"total": len(expected), "passed": agreed},
            "metadata": {
                "precision": precision,
                "recall": recall,
                "importance_agreement": importance_agreement,
                "not_in_posting": not_in_posting,
                "boilerplate": outcomes.count(BOILERPLATE),
                "text_fidelity": fidelity,
                "expected": expected_rows,
                "extracted": extracted_rows,
            },
            "explanation": (
                f"precision={precision:.2f} recall={recall:.2f} "
                f"importance={importance_agreement:.2f} not_in_posting={not_in_posting} "
                f"text_fidelity_misses={len(fidelity)}"
            ),
        }
        if (labels := expected_gates(sample)) is not None:
            gates = _scored_gates(list(solver_output.get("hard_gates") or []), labels, posting)
            result["value"] = result["value"] and gates["passed"]
            result["metadata"]["gates"] = gates
            result["explanation"] += (
                f" gates={gates['agreed']}/{len(labels)} "
                f"unlabelled_blocking={gates['unlabelled_blocking']}"
            )
        return result

    # The runner checks every row's label with this before it calls a solver.
    score.validate_sample = expected_requirements  # type: ignore[attr-defined]
    # The live smoke refuses an output lacking any of these fields.
    score.required_fields = ("requirements", "hard_gates")  # type: ignore[attr-defined]
    return score
