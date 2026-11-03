"""What the commit contract refuses before a proposal can reach the store."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError as PydanticValidationError

from clearscout.application.contracts import (
    CompensationInput,
    EligibilityInput,
    EvaluationCommit,
    ScoreComponentInput,
)
from clearscout.application.failures import ValidationError
from clearscout.application.job_search._support import _assert_score_traceability
from clearscout.infrastructure.persistence.models import EvaluationScore
from tests.support.scenarios import evaluation_commit


def test_hard_gates_and_compensation_ranges_are_validated() -> None:
    with pytest.raises(PydanticValidationError, match="minimum cannot exceed"):
        CompensationInput(
            component_type="base",
            amount_min=Decimal(200),
            amount_max=Decimal(100),
            currency="USD",
            basis="annual",
        )
    with pytest.raises(PydanticValidationError, match="currency"):
        CompensationInput(component_type="base", currency="usd", basis="annual")
    with pytest.raises(PydanticValidationError, match="basis"):
        CompensationInput(
            component_type="base",
            currency="USD",
            basis="weekly",  # type: ignore[arg-type]
        )
    with pytest.raises(PydanticValidationError, match="active posting"):
        evaluation_commit("workspace", posting_status="closed")
    with pytest.raises(PydanticValidationError, match="names the residence rule"):
        evaluation_commit(
            "workspace",
            eligibility=EligibilityInput(workspace_location_key="   "),
        )
    invalid_importance = evaluation_commit("workspace").model_dump(mode="json")
    invalid_importance["fit_report"]["requirements"][0]["importance"] = "business-critical"
    with pytest.raises(PydanticValidationError, match="must_have"):
        EvaluationCommit.model_validate(invalid_importance)
    duplicate_id = evaluation_commit("workspace").model_dump(mode="json")
    duplicate_id["fit_report"]["hard_gates"][0]["requirement_id"] = duplicate_id["fit_report"][
        "requirements"
    ][0]["requirement_id"]
    with pytest.raises(PydanticValidationError, match="Requirement IDs must be unique"):
        EvaluationCommit.model_validate(duplicate_id)


def test_service_invariant_rejects_a_stated_score_without_a_link() -> None:
    score = EvaluationScore(
        id="score-1",
        evaluation_id="evaluation-1",
        component="fit",
        score=90,
        weight=Decimal("0.35"),
        finding_state="supported",
    )
    with pytest.raises(ValidationError, match="requires linked evidence"):
        _assert_score_traceability([score], [])


def test_commit_contract_rejects_a_stated_component_without_evidence() -> None:
    payload = evaluation_commit("workspace").model_dump(mode="python")
    for item in payload["score_evidence"]:
        item["components"] = [component for component in item["components"] if component != "fit"]
    with pytest.raises(PydanticValidationError, match="explicit evidence; missing: fit"):
        EvaluationCommit.model_validate(payload)


def test_commit_contract_rejects_a_report_reference_without_stored_evidence() -> None:
    payload = evaluation_commit("workspace").model_dump(mode="python")
    payload["fit_report"]["reading"][0]["evidence_keys"] = ["unknown-evidence"]

    with pytest.raises(PydanticValidationError, match="Evidence references unknown keys"):
        EvaluationCommit.model_validate(payload)


def test_conflicting_score_remains_distinct_from_not_stated() -> None:
    value = ScoreComponentInput(value=None, reason="Title and duties disagree.", conflict=True)
    assert value.finding_state == "conflict"
    assert ScoreComponentInput(value=None, reason="The source says nothing.").finding_state == (
        "not_stated"
    )
