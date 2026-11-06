"""Grade the titles and employer archetypes one vocabulary call proposed against the case's own.

A title matches a gold label when both normalize to the same text, the rule the
program's ``already_stated`` check uses; an archetype matches by the program's
``same_employer_archetype``. Precision is taken over labeled proposals only; an
unlabeled proposal is reported as a warned check, not counted against the call.
Citation support asks whether each acceptable match cites an entry the label names,
and recall is reported without gating. A case may name claims the record does not
support; a reason or ``because`` stating one fails the call. Proposals citing only
role context, summary, or skill entries are counted, never failed. Clusters have no
labels: each is ruled on by ``searchable_clusters``, and one citing an id the CV
lacks, or left citing none, fails the call.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from clearscout.application.discovery.derived_vocabulary_rules import (
    ProposedArchetype,
    ProposedCluster,
    ProposedTitle,
    same_employer_archetype,
    searchable_archetypes,
    searchable_clusters,
    searchable_titles,
)
from clearscout.application.tailoring.stages.context import citable_entries
from clearscout.domain.identity import normalize_text
from clearscout.domain.search_parameters import ClusterKind, JobSearchParameters
from clearscout.domain.search_parameters.roles import title_excluded
from clearscout.domain.taxonomy import normalize
from evals.inspect.solvers.search_vocabulary_live import case_inventory, case_parameters

ACCEPTABLE = "acceptable"
UNACCEPTABLE = "unacceptable"
REPEATS_STATED = "repeats_stated"
EXCLUDED = "excluded"
UNLABELED = "unlabeled"
AXES = ("titles", "archetypes")
# Clusters carry no gold labels, so they are ruled on by the program and kept out of precision.
CLUSTER_AXES = {"capabilities": ClusterKind.CAPABILITY, "domains": ClusterKind.DOMAIN}
# Entry kinds a proposal is counted for when it cites nothing else.
CONTEXT_KINDS = frozenset({"role_context", "summary", "skill"})
_WORD = re.compile(r"[a-z0-9]+")
_SENTENCE_BREAK = re.compile(r"[.;:!?\n]")
_JOINERS = frozenset({"-", "\u2013"})
# "Co-led" and "self-directed" still claim the work; only an employer compound is excused.
_CLAIM_PREFIXES = frozenset({"co", "self"})

# Labels are hand rulings that can disagree at the margin, so precision gates below 1.0.
# An invented id fails on its own check, leaving only a real but non-supporting
# entry to fail citation support, so it gates higher.
PRECISION_THRESHOLD = 0.6
CITATION_SUPPORT_THRESHOLD = 0.8


def _names(item: dict[str, Any]) -> list[str]:
    return [str(item["name"]), *(str(alias) for alias in item.get("aliases", []))]


def _title_match(left: str, right: str) -> bool:
    return normalize(left) == normalize(right)


def _matcher(axis: str) -> Callable[[str, str], bool]:
    return _title_match if axis == "titles" else same_employer_archetype


def _stated(axis: str, parameters: JobSearchParameters) -> tuple[str, ...]:
    if axis == "titles":
        return parameters.seniority.stated_titles
    return tuple(item.name for item in parameters.employers.stated_archetypes)


def _labelled(axis: str, proposal: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    matches = _matcher(axis)
    return next(
        (item for item in items if any(matches(proposal, name) for name in _names(item))), None
    )


def gold_labels(sample: dict[str, Any]) -> dict[str, Any]:
    """Return a row's gold labels, refusing one its CV or parameters contradict."""
    case = sample.get("id")
    if not str(sample.get("why") or "").strip():
        raise ValueError(f"sample {case!r} needs a why")
    gold = sample.get("gold")
    if not isinstance(gold, dict):
        raise ValueError(f"sample {case!r} needs gold labels")
    known = {entry.id for entry in citable_entries(case_inventory(sample))}
    parameters = case_parameters(sample)
    excluded = [title for title in parameters.seniority.excluded_titles if title.strip()]
    for axis in AXES:
        stated = _stated(axis, parameters)
        labels = gold.get(axis)
        if not isinstance(labels, dict) or not labels.get(ACCEPTABLE):
            raise ValueError(f"sample {case!r} needs acceptable {axis}")
        for item in labels[ACCEPTABLE]:
            ids = item.get("evidence_ids")
            if not ids or not set(ids) <= known:
                raise ValueError(f"sample {case!r} cites an id its CV lacks: {item!r}")
            for name in _names(item):
                if _labelled(axis, name, labels.get(UNACCEPTABLE, [])) is not None:
                    raise ValueError(f"sample {case!r} labels {name!r} both ways")
                if any(_matcher(axis)(name, title) for title in stated) or (
                    axis == "titles" and title_excluded(name, excluded)
                ):
                    raise ValueError(f"sample {case!r} accepts a stated or excluded {name!r}")
        for item in labels.get(UNACCEPTABLE, []):
            if not str(item.get("why") or "").strip():
                raise ValueError(f"sample {case!r} rejects {item.get('name')!r} without a why")
    for claim in gold.get("unsupported_claims", []):
        terms = claim.get("terms")
        opening = claim.get("opening_terms", [])
        if (
            not isinstance(terms, list)
            or not terms
            or not isinstance(opening, list)
            or not all(isinstance(term, str) and term.strip() for term in [*terms, *opening])
            or not str(claim.get("why") or "").strip()
        ):
            raise ValueError(f"sample {case!r} names an unsupported claim without terms or a why")
    return gold


def _claim_words(text: str) -> list[tuple[str, bool, bool]]:
    """Return each word with whether it opens a sentence and whether a hyphen joins it leftward."""
    lowered = text.lower()
    words: list[tuple[str, bool, bool]] = []
    end = 0
    for found in _WORD.finditer(lowered):
        gap = lowered[end : found.start()]
        joined = gap in _JOINERS and bool(words) and words[-1][0] not in _CLAIM_PREFIXES
        words.append((found.group(), not words or bool(_SENTENCE_BREAK.search(gap)), joined))
        end = found.end()
    return words


def _states(words: list[tuple[str, bool, bool]], term: str, *, opening: bool) -> bool:
    target = normalize_text(term).split()
    return any(
        [word for word, _, _ in words[start : start + len(target)]] == target
        and not words[start][2]
        and (words[start][1] or not opening)
        for start in range(len(words) - len(target) + 1)
    )


def _unsupported_claim(text: str, claims: list[dict[str, Any]]) -> str:
    """Return why the record does not support a claim this text makes, empty when none."""
    # "Product-led AI" names an employer, so a term never starts inside a hyphen compound.
    # A present-tense verb is the candidate's only when it opens a sentence with no subject.
    words = _claim_words(text)
    return next(
        (
            str(claim["why"])
            for claim in claims
            if any(_states(words, term, opening=False) for term in claim["terms"])
            or any(_states(words, term, opening=True) for term in claim.get("opening_terms", []))
        ),
        "",
    )


def _program_refusals(
    sample: dict[str, Any], output: dict[str, Any], known: set[str]
) -> dict[str, list[str]]:
    """Return why the program's own rules would refuse each proposal, empty when kept."""
    parameters = case_parameters(sample)
    titles = searchable_titles(
        [ProposedTitle.model_validate(item) for item in output.get("titles") or []],
        parameters,
        known,
    )
    archetypes = searchable_archetypes(
        [ProposedArchetype.model_validate(item) for item in output.get("archetypes") or []],
        parameters,
        known,
    )
    refusals = {
        "titles": [item.refused_because for item in titles],
        "archetypes": [item.refused_because for item in archetypes],
    }
    for axis, kind in CLUSTER_AXES.items():
        clusters = searchable_clusters(
            [ProposedCluster.model_validate(item) for item in output.get(axis) or []],
            parameters,
            kind,
            known,
        )
        refusals[axis] = [item.refused_because for item in clusters]
    return refusals


def _scored_clusters(
    output: dict[str, Any], refusals: dict[str, list[str]], known: set[str]
) -> dict[str, Any]:
    """Return each proposed cluster with the program's ruling and any id the CV lacks."""
    rows = []
    for axis, kind in CLUSTER_AXES.items():
        for proposal, refused in zip(output.get(axis) or [], refusals[axis], strict=True):
            cited = [str(entry_id) for entry_id in proposal.get("evidence_ids") or []]
            rows.append(
                {
                    "kind": kind.value,
                    "name": str(proposal.get("name") or ""),
                    "evidence_ids": cited,
                    "unknown_evidence_ids": [
                        entry_id for entry_id in cited if entry_id not in known
                    ],
                    "program_refused_because": refused,
                }
            )
    # The program keeps a cluster whose other ids are real, so an unknown id is caught here.
    invalid = [
        row["name"]
        for row in rows
        if row["unknown_evidence_ids"] or row["program_refused_because"] == "missing_evidence"
    ]
    return {
        "proposals": rows,
        "program_refused": sum(bool(row["program_refused_because"]) for row in rows),
        "invalid_evidence": invalid,
    }


def _outcome(
    axis: str, name: str, labels: dict[str, Any], stated: tuple[str, ...], excluded: list[str]
) -> tuple[str, dict[str, Any] | None]:
    if any(_matcher(axis)(name, item) for item in stated):
        return REPEATS_STATED, None
    if axis == "titles" and title_excluded(name, excluded):
        return EXCLUDED, None
    if (item := _labelled(axis, name, labels[ACCEPTABLE])) is not None:
        return ACCEPTABLE, item
    if (item := _labelled(axis, name, labels.get(UNACCEPTABLE, []))) is not None:
        return UNACCEPTABLE, item
    return UNLABELED, None


def _scored_axis(
    axis: str,
    proposals: list[dict[str, Any]],
    labels: dict[str, Any],
    sample: dict[str, Any],
    refusals: list[str],
) -> dict[str, Any]:
    """Return one axis's per-proposal outcomes and counts."""
    parameters = case_parameters(sample)
    stated = _stated(axis, parameters)
    excluded = [title for title in parameters.seniority.excluded_titles if title.strip()]
    key, reason_key = ("title", "reason") if axis == "titles" else ("name", "because")
    claims = list(sample["gold"].get("unsupported_claims", []))
    kinds = {entry.id: entry.kind for entry in citable_entries(case_inventory(sample))}
    rows: list[dict[str, Any]] = []
    recalled: set[str] = set()
    for proposal, refused in zip(proposals, refusals, strict=True):
        name = str(proposal.get(key) or "")
        outcome, item = _outcome(axis, name, labels, stated, excluded)
        row: dict[str, Any] = {
            "name": name,
            "evidence_ids": list(proposal.get("evidence_ids") or []),
            "outcome": outcome,
            "program_refused_because": refused,
        }
        if claim := _unsupported_claim(str(proposal.get(reason_key) or ""), claims):
            row["unsupported_claim"] = claim
        if unknown := [entry_id for entry_id in row["evidence_ids"] if entry_id not in kinds]:
            row["unknown_evidence_ids"] = unknown
        if row["evidence_ids"] and all(
            kinds.get(entry_id) in CONTEXT_KINDS for entry_id in row["evidence_ids"]
        ):
            row["context_only_citation"] = True
        if item is not None:
            row["label"] = item["name"]
        if outcome == UNACCEPTABLE and item is not None:
            row["why"] = item["why"]
        if outcome == ACCEPTABLE and item is not None:
            recalled.add(str(item["name"]))
            row["citation_supported"] = bool(set(row["evidence_ids"]) & set(item["evidence_ids"]))
        rows.append(row)
    return {
        "proposals": rows,
        "acceptable": sum(row["outcome"] == ACCEPTABLE for row in rows),
        "unlabeled": sum(row["outcome"] == UNLABELED for row in rows),
        "supported": sum(bool(row.get("citation_supported")) for row in rows),
        "recalled": sorted(recalled),
        "missed": [item["name"] for item in labels[ACCEPTABLE] if item["name"] not in recalled],
        "program_refused": sum(bool(row["program_refused_because"]) for row in rows),
        "unsupported_claims": sum("unsupported_claim" in row for row in rows),
        "context_only_citations": sum("context_only_citation" in row for row in rows),
        "unknown_evidence": sum("unknown_evidence_ids" in row for row in rows),
    }


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


def proposal_usefulness() -> Any:
    """Return the scorer for the titles and archetypes one vocabulary call proposed."""

    def score(sample: dict[str, Any], solver_output: dict[str, Any]) -> dict[str, Any]:
        gold = gold_labels(sample)
        known = {entry.id for entry in citable_entries(case_inventory(sample))}
        refusals = _program_refusals(sample, solver_output, known)
        axes = {
            axis: _scored_axis(
                axis, list(solver_output.get(axis) or []), gold[axis], sample, refusals[axis]
            )
            for axis in AXES
        }
        proposed = sum(len(axes[axis]["proposals"]) for axis in AXES)
        acceptable = sum(axes[axis]["acceptable"] for axis in AXES)
        supported = sum(axes[axis]["supported"] for axis in AXES)
        gold_total = sum(len(gold[axis][ACCEPTABLE]) for axis in AXES)
        recalled = sum(len(axes[axis]["recalled"]) for axis in AXES)
        unlabeled = sum(axes[axis]["unlabeled"] for axis in AXES)
        labeled = proposed - unlabeled
        precision = acceptable / labeled if labeled else None
        citation_support = _ratio(supported, acceptable)
        recall = _ratio(recalled, gold_total)
        refused = sum(axes[axis]["program_refused"] for axis in AXES)
        overclaimed = sum(axes[axis]["unsupported_claims"] for axis in AXES)
        clusters = _scored_clusters(solver_output, refusals, known)
        failed = [
            name
            for name, failing in (
                ("no_proposals", proposed == 0),
                ("no_labeled_proposals", proposed > 0 and precision is None),
                ("precision", precision is not None and precision < PRECISION_THRESHOLD),
                ("citation_support", citation_support < CITATION_SUPPORT_THRESHOLD),
                ("unsupported_claim", overclaimed > 0),
                ("cluster_evidence", bool(clusters["invalid_evidence"])),
                ("unknown_evidence", any(axes[axis]["unknown_evidence"] for axis in AXES)),
            )
            if failing
        ]
        shown = "n/a" if precision is None else f"{precision:.2f}"
        return {
            "value": not failed,
            "checks": {
                "failed": failed,
                "warned": ["unlabeled_proposals"] if unlabeled else [],
            },
            "metadata": {
                "precision": precision,
                "precision_denominator": "labeled proposals",
                "unlabeled": unlabeled,
                "unlabeled_share": _ratio(unlabeled, proposed) if proposed else 0.0,
                "recall": recall,
                "citation_support": citation_support,
                "citation_support_denominator": "acceptable matches",
                "program_refused": refused,
                "unsupported_claims": overclaimed,
                "context_only_citations": sum(
                    axes[axis]["context_only_citations"] for axis in AXES
                ),
                "clusters_proposed": len(clusters["proposals"]),
                "clusters": clusters,
                **axes,
            },
            "explanation": (
                f"precision={shown} recall={recall:.2f} unlabeled={unlabeled}/{proposed} "
                f"citation_support={citation_support:.2f} program_refused={refused}"
            ),
        }

    # The runner checks every row's label with this before it calls a solver.
    score.validate_sample = gold_labels  # type: ignore[attr-defined]
    # The live smoke refuses an output lacking any of these fields.
    score.required_fields = (*AXES, "capabilities", "domains")  # type: ignore[attr-defined]
    return score
