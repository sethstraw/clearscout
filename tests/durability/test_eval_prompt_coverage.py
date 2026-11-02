"""Every shipped prompt family carries a stated evaluation-coverage decision."""

from __future__ import annotations

import pytest
from evals.case import Case, Responses, ScriptedSpec
from evals.runner import (
    EVALUATED_PROMPTS,
    SCRIPTABLE_STAGES,
    UNEVALUATED_PROMPTS,
    EvalError,
    build_provider,
    check_prompt_coverage_is_declared,
)
from evals.support.inventory import synthetic_inventory

from tailorcv.application import prompts


def _families() -> set[str]:
    return {name.rsplit("_", 1)[0] for name in prompts.PROMPT_NAMES}


def test_every_shipped_prompt_family_is_declared_exactly_once() -> None:
    evaluated = set(EVALUATED_PROMPTS.values())
    unevaluated = set(UNEVALUATED_PROMPTS)

    assert evaluated | unevaluated == _families()
    assert not evaluated & unevaluated


def test_the_check_passes_on_the_suite_as_it_ships() -> None:
    check_prompt_coverage_is_declared()


def test_a_new_prompt_family_fails_until_somebody_decides_about_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        prompts, "PROMPT_NAMES", (*prompts.PROMPT_NAMES, "salary_negotiation_system")
    )

    with pytest.raises(EvalError) as raised:
        check_prompt_coverage_is_declared()

    assert "salary_negotiation" in str(raised.value)


def test_a_family_declared_both_ways_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(UNEVALUATED_PROMPTS, "match", "claimed by both maps")

    with pytest.raises(EvalError) as raised:
        check_prompt_coverage_is_declared()

    assert "both evaluated and not" in str(raised.value)


def _reasons_are_sentences() -> None:
    for family, reason in UNEVALUATED_PROMPTS.items():
        assert reason.strip().endswith("."), family
        assert len(reason.split()) >= 8, family


def test_a_stated_reason_must_read_as_a_sentence_a_reviewer_can_disagree_with(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fragment gives a reviewer nothing concrete to push back on."""
    monkeypatch.setitem(UNEVALUATED_PROMPTS, "salary_negotiation", "no case reaches this")

    with pytest.raises(AssertionError):
        _reasons_are_sentences()


def test_every_evaluated_stage_can_actually_be_scripted() -> None:
    """A stage with no builder raises, so declaring it evaluated fails rather than grades."""
    assert set(EVALUATED_PROMPTS) <= SCRIPTABLE_STAGES


def test_the_scriptable_stages_match_the_builders_the_provider_offers() -> None:
    """Probe the dispatcher on the real provider, not its source text.

    A stage the provider cannot build is refused by name; every scriptable
    stage dispatches to a builder instead. Renaming a registry key fails here
    even when the method it pointed at still exists, which reading the source
    text could not catch.
    """
    provider = build_provider(
        Case(
            name="registry-probe",
            proves="the payload dispatcher offers a builder for each scriptable stage",
            posting="synthetic",
            responses=Responses(jobspec=ScriptedSpec(title="Synthetic Role")),
        ),
        synthetic_inventory(),
    )

    dispatched: set[str] = set()
    for stage in (*SCRIPTABLE_STAGES, "salary_negotiation"):
        try:
            provider._payload(stage)
        except EvalError as error:
            if "scripts no response" in str(error):
                continue
        dispatched.add(stage)

    assert dispatched == SCRIPTABLE_STAGES


def test_a_stage_declared_evaluated_without_a_builder_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every shipped family has a case, so this fakes one that never will."""
    monkeypatch.setattr(
        prompts, "PROMPT_NAMES", (*prompts.PROMPT_NAMES, "salary_negotiation_system")
    )
    monkeypatch.setitem(EVALUATED_PROMPTS, "salary_negotiation", "salary_negotiation")

    with pytest.raises(EvalError) as raised:
        check_prompt_coverage_is_declared()

    assert "no case can script them" in str(raised.value)


def test_every_family_that_has_a_case_is_declared_evaluated_and_scriptable() -> None:
    """Every family a case reaches is declared evaluated and scriptable."""
    for stage, family in (
        ("cover", "cover_letter"),
        ("structure", "structure"),
        ("vision", "vision"),
        ("search_vocabulary", "search_vocabulary"),
        ("posting_recovery", "posting_recovery"),
        ("guided_campaign", "guided_campaign"),
    ):
        assert EVALUATED_PROMPTS[stage] == family
        assert stage in SCRIPTABLE_STAGES
        assert family not in UNEVALUATED_PROMPTS


def test_no_prompt_family_is_left_unevaluated() -> None:
    """Every shipped prompt family has a case; none is declared unevaluated."""
    assert UNEVALUATED_PROMPTS == {}
