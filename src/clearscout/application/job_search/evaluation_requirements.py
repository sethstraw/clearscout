"""Fold a proposed hard gate onto the requirement identity it repeats."""

from __future__ import annotations

from dataclasses import dataclass

from clearscout.application.contracts import ReportRequirementInput
from clearscout.application.failures import ValidationError


def normalized_text(text: str) -> str:
    """Return one requirement's text, collapsed for identity comparison."""
    return " ".join(text.casefold().split())


def _gate_matches(
    hard_gates: list[ReportRequirementInput],
    requirements: list[ReportRequirementInput],
) -> dict[str, int]:
    """Return the first gate folded onto each requirement whose text it alone repeats."""
    identifiers = [item.requirement_id for item in [*requirements, *hard_gates]]
    if len(identifiers) != len(set(identifiers)):
        raise ValidationError("Requirement IDs must be unique across requirements and hard gates.")

    matches: dict[str, int] = {}
    for gate_index, gate in enumerate(hard_gates):
        candidates = [
            requirement
            for requirement in requirements
            if normalized_text(requirement.text) == normalized_text(gate.text)
        ]
        if len(candidates) == 1:
            matches.setdefault(candidates[0].requirement_id, gate_index)
    return matches


def _fold_gate_onto(
    requirement: ReportRequirementInput, gate: ReportRequirementInput
) -> ReportRequirementInput:
    gate_evidence = [key for key in gate.evidence_keys if key not in requirement.evidence_keys]
    return requirement.model_copy(
        update={
            "is_hard_gate": True,
            "gate_kind": gate.gate_kind,
            "gate_quote": gate.gate_quote,
            "gate_blocking": gate.gate_blocking,
            "evidence_keys": [*requirement.evidence_keys, *gate_evidence],
        }
    )


@dataclass(frozen=True)
class MergedRequirements:
    """The merged rows, and the row each stated id ended up on."""

    rows: list[ReportRequirementInput]
    # Keyed by every id the run stated, including a gate id the merge folded
    # away, because evidence anchored before the merge still cites it.
    carried_by: dict[str, str]


def merge_hard_gates_into_requirements(
    *,
    hard_gates: list[ReportRequirementInput],
    requirements: list[ReportRequirementInput],
) -> MergedRequirements:
    """Collapse a gate that repeats a requirement text into the requirement row.

    A hard gate is kept as its own row when its text repeats no requirement or
    more than one, and when an earlier gate already folded onto the requirement
    it repeats, since one line can state two gates of different kinds.
    """
    gate_matches = _gate_matches(hard_gates, requirements)
    matched_gate_indexes = set(gate_matches.values())
    merged: list[ReportRequirementInput] = []
    carried_by: dict[str, str] = {}
    for requirement in requirements:
        match = gate_matches.get(requirement.requirement_id)
        gate = hard_gates[match] if match is not None else None
        if gate is None:
            merged.append(requirement)
        else:
            merged.append(_fold_gate_onto(requirement, gate))
            carried_by[gate.requirement_id] = requirement.requirement_id
        carried_by[requirement.requirement_id] = requirement.requirement_id
    for gate_index, gate in enumerate(hard_gates):
        if gate_index in matched_gate_indexes:
            continue
        merged.append(gate.model_copy(update={"is_hard_gate": True}))
        carried_by[gate.requirement_id] = gate.requirement_id
    return MergedRequirements(rows=merged, carried_by=carried_by)
