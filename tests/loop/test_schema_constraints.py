"""Prove the guarantee the whole design rests on.

The model can only cite identifiers that exist, because the response schema is
built over exactly the ones this parse produced. A refactor changing
``list[entry_literal]`` to ``list[str]`` would keep every other test green.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from tailorcv.application.master_cv.cv import CvInventory
from tailorcv.application.tailoring.guardrails import Guardrails
from tailorcv.application.tailoring.stages.draft import draft, draft_schema
from tailorcv.application.tailoring.stages.jobspec import JobSpec, requirement_vocabulary
from tailorcv.application.tailoring.stages.match import response_schema
from tailorcv.application.tailoring.stages.select import Plan
from tailorcv.infrastructure.providers.client import Client
from tailorcv.shared.errors import BudgetError


def test_matching_rejects_an_identifier_that_does_not_exist() -> None:
    schema = response_schema(("b-real0001",), ("r01",))
    with pytest.raises(ValidationError):
        schema.model_validate(
            {
                "matches": [
                    {
                        "requirement_id": "r01",
                        "entry_ids": ["b-fabricated"],
                        "classification": "DIRECT",
                        "strength": "strong",
                        "note": "",
                    }
                ]
            }
        )


def test_matching_accepts_an_identifier_that_does_exist() -> None:
    schema = response_schema(("b-real0001",), ("r01",))
    parsed = schema.model_validate(
        {
            "matches": [
                {
                    "requirement_id": "r01",
                    "entry_ids": ["b-real0001"],
                    "classification": "DIRECT",
                    "strength": "strong",
                    "note": "",
                }
            ]
        }
    )
    assert parsed.matches[0].entry_ids == ["b-real0001"]  # type: ignore[attr-defined]


def test_matching_rejects_a_requirement_that_was_never_extracted() -> None:
    schema = response_schema(("b-real0001",), ("r01",))
    with pytest.raises(ValidationError):
        schema.model_validate(
            {
                "matches": [
                    {
                        "requirement_id": "r99",
                        "entry_ids": [],
                        "classification": "GAP",
                        "strength": "none",
                        "note": "",
                    }
                ]
            }
        )


def test_drafting_cannot_cite_evidence_selection_discarded() -> None:
    """The drafting vocabulary is narrower than the matching one, deliberately."""
    schema = draft_schema(("b-selected",), ("acme",))
    with pytest.raises(ValidationError):
        schema.model_validate(
            {
                "summary": {"text": "Something.", "source_ids": ["b-unselected"]},
                "skills": [],
                "roles": [],
            }
        )


def test_drafting_cannot_invent_a_role() -> None:
    schema = draft_schema(("b-selected",), ("acme",))
    with pytest.raises(ValidationError):
        schema.model_validate(
            {
                "summary": {"text": "Something.", "source_ids": ["b-selected"]},
                "skills": [],
                "roles": [{"role_key": "some-other-employer", "context": None, "bullets": []}],
            }
        )


def test_an_empty_vocabulary_is_refused_before_pydantic_asserts(
    inventory: CvInventory,
    guardrails: Guardrails,
    client_factory: Callable[[dict[str, Any]], Client],
) -> None:
    """Literal[()] fails deep inside pydantic, and vanishes under python -O."""
    spec = JobSpec(
        title="Staff Data Platform Engineer",
        company="Globex",
        seniority=None,
        location=None,
        requirements=(),
        keywords=(),
        hard_gates=(),
        origin="test",
    )

    with pytest.raises(BudgetError, match="Nothing in the master CV matched"):
        draft(client_factory({}), inventory, spec, Plan(), guardrails)


def test_postings_of_different_lengths_send_one_schema() -> None:
    """The schema is part of the cached prefix, so a per-posting one is never reused."""
    entries = ("b-one",)
    schemas = [
        response_schema(entries, requirement_vocabulary(count)).model_json_schema()
        for count in (3, 20, 33, 64, 75, 128)
    ]
    beyond = response_schema(entries, requirement_vocabulary(129)).model_json_schema()

    assert all(schema == schemas[0] for schema in schemas)
    assert beyond != schemas[0]


def test_the_generated_schema_names_every_real_identifier() -> None:
    """The enum in the JSON schema is what constrains a provider's decoding."""
    schema = response_schema(("b-one", "b-two"), ("r01",))
    text = str(schema.model_json_schema())
    assert "b-one" in text
    assert "b-two" in text
