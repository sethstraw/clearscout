"""Hard gates collapse into the requirement row they repeat."""

from __future__ import annotations

from typing import Any

import pytest

from clearscout.application.contracts import ReportRequirementInput
from clearscout.application.failures import ValidationError
from clearscout.application.job_search.evaluation_requirements import (
    merge_hard_gates_into_requirements,
)


def _input(**overrides: Any) -> ReportRequirementInput:
    defaults: dict[str, Any] = {
        "requirement_id": "r01",
        "importance": "must_have",
        "text": "Ship the platform",
        "evidence_keys": [],
    }
    defaults.update(overrides)
    return ReportRequirementInput(**defaults)


def test_a_gate_repeating_a_requirement_text_collapses_into_one_row() -> None:
    requirement = _input(requirement_id="r01", text="Must be eligible to work in Canada")
    gate = _input(
        requirement_id="gate-1",
        text="Must be eligible to work in Canada",
        is_hard_gate=True,
        gate_kind="work_authorization",
        gate_blocking=True,
        evidence_keys=["hard-gate-1"],
    )

    merged = merge_hard_gates_into_requirements(hard_gates=[gate], requirements=[requirement]).rows

    assert len(merged) == 1
    row = merged[0]
    assert row.requirement_id == "r01"
    assert row.is_hard_gate is True
    assert row.gate_kind == "work_authorization"
    assert row.gate_blocking is True
    assert row.evidence_keys == ["hard-gate-1"]


def test_a_gate_whose_text_no_requirement_repeats_stays_its_own_row() -> None:
    requirement = _input(requirement_id="r01", text="Ship the platform")
    gate = _input(
        requirement_id="gate-1",
        text="Must be eligible to work in Canada",
        is_hard_gate=True,
        gate_kind="work_authorization",
    )

    merged = merge_hard_gates_into_requirements(hard_gates=[gate], requirements=[requirement]).rows

    assert [(row.requirement_id, row.is_hard_gate) for row in merged] == [
        ("r01", False),
        ("gate-1", True),
    ]


def test_the_hard_gate_collection_sets_the_canonical_gate_flag() -> None:
    gate = _input(
        requirement_id="gate-1",
        text="Must be eligible to work in Canada",
        is_hard_gate=False,
    )

    merged = merge_hard_gates_into_requirements(hard_gates=[gate], requirements=[]).rows

    assert merged[0].is_hard_gate is True


def test_a_gate_and_requirement_must_not_share_a_requirement_id() -> None:
    requirement = _input(requirement_id="REQ-2", text="Canada remote")
    gate = _input(
        requirement_id="REQ-2",
        text="Canada remote",
        is_hard_gate=True,
        gate_kind="work_authorization",
        gate_blocking=True,
    )

    with pytest.raises(ValidationError, match="Requirement IDs must be unique"):
        merge_hard_gates_into_requirements(hard_gates=[gate], requirements=[requirement])


def test_an_id_collision_with_different_text_is_also_refused() -> None:
    requirement = _input(requirement_id="REQ-2", text="Operate the data platform")
    gate = _input(
        requirement_id="REQ-2",
        text="Must be eligible to work in Canada",
        is_hard_gate=True,
        gate_kind="work_authorization",
    )

    with pytest.raises(ValidationError, match="Requirement IDs must be unique"):
        merge_hard_gates_into_requirements(hard_gates=[gate], requirements=[requirement])


def test_a_gate_repeating_two_requirements_stays_its_own_row() -> None:
    requirements = [
        _input(requirement_id="r01", text="Operate the data platform"),
        _input(requirement_id="r02", text="  operate   THE data platform "),
    ]
    gate = _input(requirement_id="gate-1", text="Operate the data platform")

    merged = merge_hard_gates_into_requirements(hard_gates=[gate], requirements=requirements)

    assert [(row.requirement_id, row.is_hard_gate) for row in merged.rows] == [
        ("r01", False),
        ("r02", False),
        ("gate-1", True),
    ]


def test_a_line_stating_two_gates_folds_the_first_and_keeps_the_second_as_its_own_row() -> None:
    line = "Bachelor's degree and 5+ years of data engineering experience"
    requirement = _input(requirement_id="r01", text=line)
    gates = [
        _input(requirement_id="gate-1", text=line, is_hard_gate=True, gate_kind="degree"),
        _input(
            requirement_id="gate-2",
            text=f"  {line.upper()} ",
            is_hard_gate=True,
            gate_kind="minimum_years",
        ),
    ]

    merged = merge_hard_gates_into_requirements(hard_gates=gates, requirements=[requirement])

    assert [(row.requirement_id, row.gate_kind) for row in merged.rows] == [
        ("r01", "degree"),
        ("gate-2", "minimum_years"),
    ]
    assert merged.carried_by == {"r01": "r01", "gate-1": "r01", "gate-2": "gate-2"}
