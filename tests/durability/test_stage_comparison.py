"""Stepping one rung down the effort ladder, per provider."""

from __future__ import annotations

from decimal import Decimal

import pytest
from evals.stage_comparison import (
    ComparisonResult,
    Side,
    comparisons,
    offered,
    one_level_lower,
)

from clearscout.application.config import Effort, ModelChoice, ProviderName, Settings, Stage


def test_anthropic_is_not_offered_the_two_rungs_it_rejects() -> None:
    assert offered(ProviderName.ANTHROPIC)[0] is Effort.LOW
    assert Effort.MINIMAL not in offered(ProviderName.ANTHROPIC)
    assert offered(ProviderName.OPENAI)[0] is Effort.NONE


@pytest.mark.parametrize(
    ("provider", "effort", "expected"),
    [
        (ProviderName.OPENAI, Effort.LOW, Effort.MINIMAL),
        (ProviderName.OPENAI, Effort.HIGH, Effort.MEDIUM),
        (ProviderName.OPENAI, Effort.NONE, None),
        # Anthropic's floor is low, so lowering it would build a refused request.
        (ProviderName.ANTHROPIC, Effort.LOW, None),
        (ProviderName.ANTHROPIC, Effort.MEDIUM, Effort.LOW),
    ],
)
def test_one_level_lower_skips_rungs_a_provider_rejects(
    provider: ProviderName, effort: Effort, expected: Effort | None
) -> None:
    assert one_level_lower(effort, provider) is expected


def test_a_stage_with_no_effort_has_nothing_to_lower() -> None:
    assert one_level_lower(None, ProviderName.OPENAI) is None


def test_every_stage_above_the_floor_is_paired_with_a_cheaper_setting_of_its_model() -> None:
    """Only the effort moves, so the comparison is of one variable."""
    found = comparisons(Settings(provider=ProviderName.OPENAI))

    assert [item.stage for item in found] == list(Stage)
    for item in found:
        # The default OpenAI model has no effort below low.
        if item.current.effort is Effort.LOW:
            assert not item.comparable, f"{item.stage.value} found a rung below low"
            continue
        assert item.comparable, f"{item.stage.value} had no rung below it"
        assert item.lower is not None
        assert item.lower.model == item.current.model
        assert item.lower.effort is not item.current.effort


def test_a_stage_already_at_the_floor_is_reported_rather_than_skipped() -> None:
    """A stage nothing can be lowered for is a finding, not an absent row."""
    settings = Settings(provider=ProviderName.ANTHROPIC)
    lowered = settings.model_copy(
        update={
            "stages": {
                **settings.stages,
                Stage.JOBSPEC: settings.stages[Stage.JOBSPEC].model_copy(
                    update={"effort": Effort.LOW}
                ),
            }
        }
    )

    jobspec = next(item for item in comparisons(lowered) if item.stage is Stage.JOBSPEC)

    assert not jobspec.comparable
    assert jobspec.lower is None


def test_a_comparison_reports_both_sides_and_names_no_winner() -> None:
    result = ComparisonResult(
        stage=Stage.CRITIC,
        current=Side(choice=ModelChoice(model="m", effort=Effort.HIGH), dollars=Decimal("0.30")),
        lower=Side(choice=ModelChoice(model="m", effort=Effort.MEDIUM), dollars=Decimal("0.10")),
    )

    assert result.cheaper_by() == Decimal("0.20")
    assert not hasattr(result, "winner")


def test_an_unpriced_side_leaves_the_saving_unknown() -> None:
    result = ComparisonResult(
        stage=Stage.CRITIC,
        current=Side(choice=ModelChoice(model="m", effort=Effort.HIGH)),
        lower=Side(choice=ModelChoice(model="m", effort=Effort.MEDIUM), dollars=Decimal("0.10")),
    )

    assert result.cheaper_by() is None


def test_two_models_answering_leaves_the_saving_unknown() -> None:
    """A saving across two models is attributed to an effort that did not cause it."""
    result = ComparisonResult(
        stage=Stage.CRITIC,
        current=Side(
            choice=ModelChoice(model="m", effort=Effort.HIGH),
            dollars=Decimal("0.30"),
            served="m-2026-08-01",
        ),
        lower=Side(
            choice=ModelChoice(model="m", effort=Effort.MEDIUM),
            dollars=Decimal("0.10"),
            served="m-2026-02-16",
        ),
    )

    assert not result.one_model_answered
    assert result.cheaper_by() is None


def test_one_model_answering_both_sides_still_reports_the_saving() -> None:
    result = ComparisonResult(
        stage=Stage.CRITIC,
        current=Side(
            choice=ModelChoice(model="m", effort=Effort.HIGH),
            dollars=Decimal("0.30"),
            served="m-2026-02-16",
        ),
        lower=Side(
            choice=ModelChoice(model="m", effort=Effort.MEDIUM),
            dollars=Decimal("0.10"),
            served="m-2026-02-16",
        ),
    )

    assert result.cheaper_by() == Decimal("0.20")
