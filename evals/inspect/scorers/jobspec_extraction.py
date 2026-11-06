"""Grade the requirements a jobspec extraction read out of a posting against the case's own.

An extracted requirement matches an expected one when either quote contains the other
after span normalization and the shorter carries at least half the longer's content
words; a quote recalls only the expected span it mostly states, and an expected span
marked ``whole`` must be quoted entire. A quote the posting does not contain fails the
row outright, and so does any recall or precision miss, an importance disagreement on a
matched requirement, or a quote word that no requirement text citing it carries.
Responsibilities the extraction reads out of duty lists are not counted against precision.
"""

from __future__ import annotations

import re
from typing import Any

from clearscout.application.tailoring.elicit.vocabulary import FUNCTION_WORDS
from clearscout.application.tailoring.stages.jobspec import Importance
from clearscout.application.tailoring.stages.spans import normalize

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


def expected_requirements(sample: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a row's expected requirements, refusing a label the posting cannot support."""
    case = sample.get("id")
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
        return {
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

    # The runner checks every row's label with this before it calls a solver.
    score.validate_sample = expected_requirements  # type: ignore[attr-defined]
    return score
