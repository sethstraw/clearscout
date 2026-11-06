"""Prove the guarantee the whole design rests on.

The model can only cite identifiers that exist: matching closes its schema over
them, and drafting and letters, whose schemas stay constant so the cached prefix
is reused, are held to them by the program after the call.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from clearscout.application.master_cv.cv import CvInventory, Entry, EntryKind
from clearscout.application.tailoring.guardrails import Guardrails
from clearscout.application.tailoring.stages.draft import (
    CoverLetter,
    DraftedLine,
    DraftedRole,
    DraftedSkill,
    ResumeDraft,
    cover_letter,
    draft,
)
from clearscout.application.tailoring.stages.jobspec import JobSpec, requirement_vocabulary
from clearscout.application.tailoring.stages.match import response_schema
from clearscout.application.tailoring.stages.select import Plan
from clearscout.application.tailoring.stages.verify.checks import provenance
from clearscout.application.tailoring.stages.verify.letter import check_letter
from clearscout.infrastructure.providers.base import json_schema
from clearscout.infrastructure.providers.client import Client
from clearscout.shared.errors import BudgetError
from clearscout.shared.results import Outcome


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


def _first(inventory: CvInventory, kind: EntryKind) -> Entry:
    return next(entry for entry in inventory.entries if entry.kind is kind)


def _bullet_plan(inventory: CvInventory, **extra: Any) -> tuple[Entry, Plan]:
    bullet = _first(inventory, EntryKind.BULLET)
    role = bullet.role_key or ""
    plan = Plan(recent_roles=(role,), role_bullets={role: (bullet.id,)}, **extra)
    return bullet, plan


def test_drafting_cannot_cite_evidence_selection_discarded(inventory: CvInventory) -> None:
    """The drafting vocabulary is narrower than the matching one, deliberately."""
    _, plan = _bullet_plan(inventory)
    unselected = next(entry.id for entry in inventory.entries if entry.id not in plan.all_ids())
    written = ResumeDraft(summary=DraftedLine(text="Something.", source_ids=(unselected,)))

    assert provenance(written, plan).outcome is Outcome.FAIL
    assert unselected in provenance(written, plan).detail


def test_drafting_cannot_invent_a_role(inventory: CvInventory) -> None:
    bullet, plan = _bullet_plan(inventory)
    line = DraftedLine(text="Something.", source_ids=(bullet.id,))
    written = ResumeDraft(
        summary=line,
        roles=(DraftedRole(role_key="contoso-invented", bullets=(line,)),),
    )

    finding = provenance(written, plan)

    assert finding.outcome is Outcome.FAIL
    assert "contoso-invented" in finding.detail


def test_a_plan_with_no_recent_roles_offers_no_role_to_draft(inventory: CvInventory) -> None:
    bullet = _first(inventory, EntryKind.BULLET)
    line = DraftedLine(text="Something.", source_ids=(bullet.id,))
    plan = Plan(summary_ids=(bullet.id,))
    written = ResumeDraft(
        summary=line, roles=(DraftedRole(role_key=bullet.role_key or "", bullets=(line,)),)
    )

    finding = provenance(written, plan)

    assert finding.outcome is Outcome.FAIL
    assert "role(s) the evidence does not offer" in finding.detail


def test_a_skills_line_may_cite_only_skill_entries(inventory: CvInventory) -> None:
    skill = _first(inventory, EntryKind.SKILL)
    bullet, plan = _bullet_plan(inventory, skill_ids=(skill.id,))
    line = DraftedLine(text="Something.", source_ids=(bullet.id,))
    written = ResumeDraft(
        summary=line,
        skills=(DraftedSkill(label="Tools", text="SQL", source_ids=(bullet.id,)),),
    )

    finding = provenance(written, plan)

    assert finding.outcome is Outcome.FAIL
    assert bullet.id in finding.detail


def test_a_drafted_skills_line_keeps_a_bullet_it_cites_for_verification(
    inventory: CvInventory,
    guardrails: Guardrails,
    client_factory: Callable[[dict[str, Any]], Client],
) -> None:
    """Grouping under the CV's skill headings must not launder a non-skill citation."""
    skill = _first(inventory, EntryKind.SKILL)
    bullet, plan = _bullet_plan(inventory, skill_ids=(skill.id,))
    line = {"text": "Built things.", "source_ids": [bullet.id]}
    answer = {
        "summary": line,
        "skills": [{"text": "SQL", "source_ids": [skill.id, bullet.id]}],
        "roles": [],
    }

    written = draft(
        client_factory({"ConstrainedResumeDraft": lambda _: answer}),
        inventory,
        _SPEC,
        plan,
        guardrails,
    )
    finding = provenance(written, plan)

    assert finding.outcome is Outcome.FAIL
    assert bullet.id in finding.detail


def test_a_letter_cannot_cite_evidence_its_prompt_never_showed(inventory: CvInventory) -> None:
    """Education is copied onto the resume, so the letter's evidence leaves it out."""
    education = _first(inventory, EntryKind.EDUCATION)
    _, plan = _bullet_plan(inventory, education_ids=(education.id,))
    letter = CoverLetter(
        greeting="Dear hiring team,",
        paragraphs=(DraftedLine(text="I studied the field.", source_ids=(education.id,)),),
        closing="Sincerely,",
    )

    report = check_letter(letter=letter, plan=plan, inventory=inventory, guardrails=Guardrails())
    finding = next(item for item in report.findings if item.check == "letter provenance")

    assert finding.outcome is Outcome.FAIL
    assert education.id in finding.detail


def _sent_schema(
    client_factory: Callable[[dict[str, Any]], Client], stage: str, answer: dict[str, Any]
) -> tuple[dict[str, Any], Client]:
    seen: dict[str, Any] = {}

    def respond(schema: type[BaseModel]) -> dict[str, Any]:
        seen[stage] = json_schema(schema)
        return answer

    return seen, client_factory({stage: respond})


def test_two_drafts_from_different_evidence_send_one_schema(
    inventory: CvInventory,
    guardrails: Guardrails,
    client_factory: Callable[[dict[str, Any]], Client],
) -> None:
    """The schema precedes the cached prefix, so a per-run one is never reused."""
    bullets = [entry for entry in inventory.entries if entry.kind is EntryKind.BULLET][:2]
    sent = []
    for bullet in bullets:
        role = bullet.role_key or ""
        plan = Plan(recent_roles=(role,), role_bullets={role: (bullet.id,)})
        answer = {"summary": {"text": "Built things.", "source_ids": [bullet.id]}, "roles": []}
        seen, client = _sent_schema(client_factory, "ConstrainedResumeDraft", answer)
        draft(client, inventory, _SPEC, plan, guardrails)
        sent.append(seen["ConstrainedResumeDraft"])

    assert bullets[0].id != bullets[1].id
    assert sent[0] == sent[1]


def test_two_letters_from_different_evidence_send_one_schema(
    inventory: CvInventory,
    guardrails: Guardrails,
    client_factory: Callable[[dict[str, Any]], Client],
) -> None:
    bullets = [entry for entry in inventory.entries if entry.kind is EntryKind.BULLET][:2]
    sent = []
    for bullet in bullets:
        role = bullet.role_key or ""
        plan = Plan(recent_roles=(role,), role_bullets={role: (bullet.id,)})
        answer = {"paragraphs": [{"text": "I built things.", "source_ids": [bullet.id]}]}
        seen, client = _sent_schema(client_factory, "ConstrainedCoverLetter", answer)
        cover_letter(client, inventory, _SPEC, plan, guardrails)
        sent.append(seen["ConstrainedCoverLetter"])

    assert sent[0] == sent[1]


_SPEC = JobSpec(
    title="Staff Data Platform Engineer",
    company="Globex",
    seniority=None,
    location=None,
    requirements=(),
    keywords=(),
    hard_gates=(),
    origin="test",
)


def test_an_empty_vocabulary_is_refused_before_pydantic_asserts(
    inventory: CvInventory,
    guardrails: Guardrails,
    client_factory: Callable[[dict[str, Any]], Client],
) -> None:
    """A plan that selected nothing is refused before any call is paid for."""
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
