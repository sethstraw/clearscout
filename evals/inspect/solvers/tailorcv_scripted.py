"""Scripted solver: drive the shipped match stage against a canned provider.

Assembles a synthetic ``CvInventory`` and a ``JobSpec`` from the sample and calls
the match stage through an in-process ``ScriptedProvider``. Catches a stale
prompt template or a closed-vocabulary regression; it measures nothing about
answer quality. A ``job_spec`` sample sends the whole posting through one call
and returns one outcome per requirement; a single-requirement sample returns its
``completion``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from evals.support.client import build_scripted_client
from evals.support.inventory import synthetic_guardrails, synthetic_inventory
from evals.support.jobspec import (
    numbered_job_spec,
    positional_requirement_ids,
    requirement_outcomes,
    sample_job_spec,
)
from evals.support.scripted_provider import ScriptedProvider
from tailorcv.application.master_cv.cv import CvInventory
from tailorcv.application.tailoring.stages.jobspec import JobSpec
from tailorcv.application.tailoring.stages.match import match

_DEFAULT_STRENGTH = "strong"
_MATCH_SCHEMA_KEY = "EvidenceMap"
_CLASSIFICATIONS = frozenset({"DIRECT", "TAILORING_ONLY", "TRANSFERABLE", "ASSERTED", "GAP"})


def _valid_classification(candidate: Any) -> str:
    """Return one canned classification, refusing an empty or out-of-vocabulary value."""
    if not isinstance(candidate, str) or not candidate.strip():
        raise ValueError("scripted classification must be a nonempty string")
    classification = candidate.strip().upper()
    if classification not in _CLASSIFICATIONS:
        raise ValueError(f"unsupported scripted classification: {classification}")
    return classification


def _requested_classification(sample: dict[str, Any]) -> str:
    metadata = sample.get("metadata") or {}
    candidate = metadata.get("scripted_classification") if isinstance(metadata, dict) else None
    if not isinstance(candidate, str) or not candidate.strip():
        raise ValueError("scripted sample is missing metadata.scripted_classification")
    return _valid_classification(candidate)


def _scripted_matches(sample: dict[str, Any], spec: JobSpec, multi: bool) -> dict[str, str]:
    """Return the canned classification for each requirement id in the spec."""
    if not multi:
        classification = _requested_classification(sample)
        return {requirement.id: classification for requirement in spec.requirements}
    metadata = sample.get("metadata") or {}
    requested = metadata.get("scripted_matches") if isinstance(metadata, dict) else None
    ids = {requirement.id for requirement in spec.requirements}
    if not isinstance(requested, dict) or set(requested) != ids:
        raise ValueError("scripted_matches must name every requirement id exactly once")
    return {key: _valid_classification(value) for key, value in requested.items()}


def _match_row_for(
    classification: str, requirement_id: str, inventory: CvInventory
) -> dict[str, Any]:
    first_entry = inventory.entries[0].id if inventory.entries else ""
    entry_ids: list[str] = []
    if classification not in {"GAP", "ASSERTED"} and first_entry:
        entry_ids = [first_entry]
    return {
        "requirement_id": requirement_id,
        "entry_ids": entry_ids,
        "classification": classification,
        "strength": "none" if classification in {"GAP", "ASSERTED"} else _DEFAULT_STRENGTH,
        "note": "scripted",
    }


def tailorcv_scripted_solver() -> Any:
    """Return the plain solver callable that runs the match stage."""
    inventory = synthetic_inventory()
    guardrails = synthetic_guardrails()

    def solve(sample: dict[str, Any]) -> dict[str, Any]:
        spec = sample_job_spec(sample, inventory)
        multi = "job_spec" in sample
        scripted_matches = _scripted_matches(sample, spec, multi)

        ids = positional_requirement_ids(len(spec.requirements))

        def script(_schema: type[BaseModel]) -> dict[str, Any]:
            return {
                "matches": [
                    _match_row_for(scripted_matches[requirement.id], identifier, inventory)
                    for identifier, requirement in zip(ids, spec.requirements, strict=True)
                ]
            }

        provider = ScriptedProvider(script={_MATCH_SCHEMA_KEY: script})
        client = build_scripted_client(provider)
        evidence = match(client, inventory, numbered_job_spec(spec), guardrails)
        if multi:
            return {
                "per_requirement": requirement_outcomes(spec, evidence),
                "metadata": {"scripted_requested": scripted_matches},
            }
        first_id = ids[0]
        result = evidence.for_requirement(first_id)
        # A row the run could not establish is reported incomplete, so the
        # scorer cannot credit an unanswered requirement as a judged gap.
        gap_detail: dict[str, Any] = {}
        if result is not None and result.gap_class is not None:
            gap_detail = {"gap_class": result.gap_class.value, "note": result.note}
        return {
            "completion": result.classification.value if result is not None else "GAP",
            "metadata": {
                "entry_keys": list(result.entry_ids) if result is not None else [],
                "outcome_complete": result is not None and not result.needs_review,
                **gap_detail,
                "scripted_requested": scripted_matches[first_id],
            },
        }

    return solve
