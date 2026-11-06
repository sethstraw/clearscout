"""Every stage's inference settings, and the tier they are seeded from."""

from __future__ import annotations

import pytest
from evals.stage_comparison import one_level_lower

from clearscout.application.config import (
    DEFAULT_STAGES,
    FLEX_TIMEOUT_FLOOR,
    Effort,
    ImageDetail,
    ModelChoice,
    ProviderName,
    Settings,
    Stage,
    Tier,
    Verbosity,
)
from clearscout.shared.costs import ServiceTier
from clearscout.shared.errors import ConfigError

# These values are in the cache key, so moving one invalidates every recorded
# answer and the cassette under it.
SHIPPED = {
    Stage.INGEST: (Tier.EXTRACTION, 8000),
    Stage.STRUCTURE: (Tier.DRAFTING, 18000),
    Stage.JOBSPEC: (Tier.EXTRACTION, 16000),
    Stage.MATCH: (Tier.EXTRACTION, 16000),
    Stage.DRAFT: (Tier.DRAFTING, 12000),
    Stage.COVER: (Tier.DRAFTING, 8000),
    Stage.CRITIC: (Tier.DRAFTING, 12000),
    Stage.GUIDED_CAMPAIGN: (Tier.EXTRACTION, 6000),
    Stage.SEARCH_VOCABULARY: (Tier.EXTRACTION, 6000),
    Stage.RELEVANCE_REVIEW: (Tier.EXTRACTION, 16000),
    Stage.POSTING_RECOVERY: (Tier.EXTRACTION, 2000),
}


@pytest.mark.parametrize("stage", list(Stage))
def test_the_policy_reproduces_what_each_stage_already_sent(stage: Stage) -> None:
    setting = Settings().policy_for(stage)

    assert (setting.tier, setting.max_tokens) == SHIPPED[stage]


def test_every_stage_that_can_be_called_has_a_policy() -> None:
    assert set(DEFAULT_STAGES) == set(Stage)


def test_the_discounted_tier_waits_longer_without_shortening_a_stated_wait() -> None:
    """Flex is served more slowly, and a longer stated timeout still wins."""
    shipped = Settings()
    patient = Settings(request_timeout=1800.0)

    assert shipped.timeout_for() == shipped.request_timeout
    assert shipped.timeout_for(ServiceTier.STANDARD) == shipped.request_timeout
    assert shipped.timeout_for(ServiceTier.FLEX) > shipped.request_timeout
    assert shipped.timeout_for(ServiceTier.FLEX) == FLEX_TIMEOUT_FLOOR
    assert patient.timeout_for(ServiceTier.FLEX) == 1800.0


def test_a_stage_defaults_to_the_standard_tier_and_anthropic_refuses_the_other() -> None:
    assert Settings().choice_for_stage(Stage.MATCH).service_tier is ServiceTier.STANDARD

    openai = Settings(provider=ProviderName.OPENAI)
    assert openai.choice_for_stage(Stage.MATCH, ServiceTier.FLEX).service_tier is ServiceTier.FLEX

    anthropic = Settings(provider=ProviderName.ANTHROPIC)
    with pytest.raises(ConfigError, match="serves no 'flex' tier"):
        anthropic.choice_for_stage(Stage.MATCH, ServiceTier.FLEX)


def test_queue_work_asks_for_the_discount_only_where_it_is_served() -> None:
    """Asking Anthropic for flex is a refusal, so unattended work must not ask it."""
    openai = Settings(provider=ProviderName.OPENAI)
    anthropic = Settings(provider=ProviderName.ANTHROPIC)

    assert openai.unattended_tier() is ServiceTier.FLEX
    assert anthropic.unattended_tier() is ServiceTier.STANDARD
    assert (
        anthropic.choice_for_stage(Stage.MATCH, anthropic.unattended_tier()).service_tier
        is ServiceTier.STANDARD
    )


def test_only_the_stage_that_sends_images_states_a_detail_level() -> None:
    """Only ingest sends images, so only ingest carries an image detail level."""
    settings = Settings()

    assert settings.policy_for(Stage.INGEST).image_detail is ImageDetail.HIGH
    assert [stage for stage in Stage if settings.policy_for(stage).image_detail is not None] == [
        Stage.INGEST
    ]


@pytest.mark.parametrize("provider", list(ProviderName))
def test_only_the_match_stage_states_its_own_effort(provider: ProviderName) -> None:
    """Match reasons at medium on either provider; every other stage takes its tier's effort."""
    settings = Settings(provider=provider)

    assert settings.choice_for_stage(Stage.MATCH).effort is Effort.MEDIUM
    assert [stage for stage in Stage if settings.policy_for(stage).effort is not None] == [
        Stage.MATCH
    ]


def test_only_jobspec_and_relevance_review_ship_a_verbosity_and_it_is_low() -> None:
    """Every other stage keeps the vendor default verbosity."""
    settings = Settings(provider=ProviderName.OPENAI)

    assert settings.choice_for_stage(Stage.JOBSPEC).verbosity is Verbosity.LOW
    assert {
        stage: settings.policy_for(stage).verbosity
        for stage in Stage
        if settings.policy_for(stage).verbosity is not None
    } == {Stage.JOBSPEC: Verbosity.LOW, Stage.RELEVANCE_REVIEW: Verbosity.LOW}


def test_a_stage_takes_its_controls_from_its_own_setting() -> None:
    settings = Settings()
    louder = settings.model_copy(
        update={
            "stages": {
                **settings.stages,
                Stage.DRAFT: settings.stages[Stage.DRAFT].model_copy(
                    update={"verbosity": Verbosity.LOW}
                ),
            }
        }
    )

    assert louder.choice_for_stage(Stage.DRAFT).verbosity is Verbosity.LOW
    assert louder.choice_for_stage(Stage.COVER).verbosity is None


def test_a_model_that_rejects_an_effort_is_refused_before_the_call() -> None:
    """gpt-5.6-terra answers a `minimal` request with a 400 listing the rest of the ladder."""
    settings = Settings(provider=ProviderName.OPENAI)
    asking_for_minimal = settings.model_copy(
        update={
            "stages": {
                **settings.stages,
                Stage.JOBSPEC: settings.stages[Stage.JOBSPEC].model_copy(
                    update={"effort": Effort.MINIMAL}
                ),
            }
        }
    )

    with pytest.raises(ConfigError, match="minimal"):
        asking_for_minimal.choice_for_stage(Stage.JOBSPEC)


def test_a_model_with_no_recorded_efforts_is_refused_before_the_call() -> None:
    """Which levels a model accepts is learned from a billed error, so it is not guessed."""
    settings = Settings(provider=ProviderName.OPENAI)
    unrecorded = settings.model_copy(
        update={
            "models": {
                **settings.models,
                ProviderName.OPENAI: settings.models[ProviderName.OPENAI].model_copy(
                    update={"extraction": ModelChoice(model="gpt-5.6-sol", effort=Effort.LOW)}
                ),
            }
        }
    )

    with pytest.raises(ConfigError, match="No effort levels are recorded for gpt-5.6-sol"):
        unrecorded.choice_for_stage(Stage.JOBSPEC)


@pytest.mark.parametrize("model", ["gpt-6-sol", "gpt-6-luna"])
def test_a_gpt_6_model_takes_its_documented_ladder_and_refuses_minimal(model: str) -> None:
    """OpenAI documents none through max for both models, with no `minimal`."""
    settings = Settings(provider=ProviderName.OPENAI)

    def asking(effort: Effort) -> Settings:
        return settings.model_copy(
            update={
                "models": {
                    **settings.models,
                    ProviderName.OPENAI: settings.models[ProviderName.OPENAI].model_copy(
                        update={"extraction": ModelChoice(model=model, effort=effort)}
                    ),
                }
            }
        )

    assert [
        asking(effort).choice_for_stage(Stage.JOBSPEC).effort
        for effort in (Effort.NONE, Effort.LOW, Effort.MAX)
    ] == [Effort.NONE, Effort.LOW, Effort.MAX]
    with pytest.raises(ConfigError, match=f"{model} has no effort level called 'minimal'"):
        asking(Effort.MINIMAL).choice_for_stage(Stage.JOBSPEC)


def test_the_rung_below_low_is_the_one_the_model_accepts() -> None:
    """Stepping down skips a rung the model would refuse rather than proposing it."""
    assert one_level_lower(Effort.LOW, ProviderName.OPENAI, "gpt-5.6-terra") is Effort.NONE


def test_a_stage_setting_can_be_overridden_without_touching_the_others() -> None:
    settings = Settings()
    lowered = settings.model_copy(
        update={
            "stages": {
                **settings.stages,
                Stage.CRITIC: settings.stages[Stage.CRITIC].model_copy(update={"max_tokens": 4000}),
            }
        }
    )

    assert lowered.policy_for(Stage.CRITIC).max_tokens == 4000
    assert lowered.policy_for(Stage.DRAFT).max_tokens == 12000
