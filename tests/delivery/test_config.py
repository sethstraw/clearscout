"""Settings resolution, and the promise that no configuration is required."""

from __future__ import annotations

from pathlib import Path

import pytest

from tailorcv.application.config import (
    DEFAULT_PROVIDER,
    Effort,
    ProviderName,
    Settings,
    Tier,
    config_locations,
    find_config,
    load_settings,
)
from tailorcv.application.tailoring.guardrails import Guardrails
from tailorcv.shared.errors import ConfigError
from tests.support.suite import SUITE_PROVIDER


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "tailorcv.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_a_run_needs_no_configuration_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero config is a supported way to run, not an error to report."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "tailorcv.application.config.user_config_dir", lambda app: str(tmp_path / "none")
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "nohome"))
    settings = load_settings()
    assert settings.pages == 2
    assert settings.provider is DEFAULT_PROVIDER
    assert settings.source is None


def test_the_default_provider_is_the_only_one_validated_against_a_live_api() -> None:
    """The Anthropic adapter is exercised offline only, so OpenAI is the default."""
    assert Settings().provider is ProviderName.OPENAI


def test_the_suite_exercises_the_default_provider() -> None:
    """Most tests set a key for whichever provider a run picks, so drift stops testing it."""
    assert SUITE_PROVIDER is Settings().provider


def test_the_master_cv_is_discovered_when_it_is_not_configured(
    tmp_path: Path, master_cv: Path
) -> None:
    (tmp_path / "cv").mkdir()
    copied = tmp_path / "cv" / "CV_Master.md"
    copied.write_text(master_cv.read_text(encoding="utf-8"), encoding="utf-8")
    assert Settings().master_cv(start=tmp_path) == copied


def test_an_explicit_cv_beats_discovery(tmp_path: Path, master_cv: Path) -> None:
    assert Settings().master_cv(master_cv, start=tmp_path) == master_cv


def test_a_flag_beats_the_file(tmp_path: Path, master_cv: Path) -> None:
    config = write_config(tmp_path, f'cv = "{master_cv}"\npages = 3\n')
    assert load_settings(config, overrides={"pages": 1}).pages == 1


def test_an_unset_flag_does_not_shadow_the_file(tmp_path: Path, master_cv: Path) -> None:
    config = write_config(tmp_path, f'cv = "{master_cv}"\npages = 3\n')
    assert load_settings(config, overrides={"pages": None}).pages == 3


def test_the_environment_beats_the_file(
    tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write_config(tmp_path, f'cv = "{master_cv}"\nprovider = "anthropic"\n')
    monkeypatch.setenv("TAILORCV_PROVIDER", "openai")
    assert load_settings(config).provider is ProviderName.OPENAI


def test_the_cv_path_resolves_against_the_configuration_file(
    tmp_path: Path, master_cv: Path
) -> None:
    (tmp_path / "cv").mkdir()
    copied = tmp_path / "cv" / "Master.md"
    copied.write_text(master_cv.read_text(encoding="utf-8"), encoding="utf-8")
    config = write_config(tmp_path, 'cv = "cv/Master.md"\n')
    assert load_settings(config).cv == copied.resolve()


def test_output_lands_beside_the_user_not_beside_the_config(
    tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user-level config must not write resumes into ~/.config."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    config = write_config(tmp_path, f'cv = "{master_cv}"\noutput_dir = "applications"\n')
    assert load_settings(config).output_dir == (work / "applications").resolve()


def test_rules_live_in_the_same_file(tmp_path: Path, master_cv: Path) -> None:
    """Rules are a `[rules]` table in the settings file, not a second file."""
    config = write_config(
        tmp_path,
        f'cv = "{master_cv}"\n\n[rules]\nprotected = ["Staff Engineer - Platform"]\n'
        'distinctions = ["Terraform is production; Pulumi is familiarity."]\n',
    )
    settings = load_settings(config)
    assert settings.rules.protected == ["Staff Engineer - Platform"]
    assert "Terraform is production" in settings.rules.prompt_block()
    assert settings.rules.configured


def test_every_non_default_guardrail_counts_as_configured() -> None:
    assert Guardrails(required=["Exact wording"]).configured
    assert Guardrails(first_person=True).configured
    assert Guardrails(ascii_only=False).configured


def test_prices_ship_with_the_program(tmp_path: Path, master_cv: Path) -> None:
    """Nobody should have to type a price to see what a run cost."""
    from tailorcv.shared.costs.prices import table

    settings = load_settings(write_config(tmp_path, f'cv = "{master_cv}"\n'))
    assert settings.choice_for(Tier.DRAFTING).model in table(settings.prices)


def test_a_price_can_still_be_overridden(tmp_path: Path, master_cv: Path) -> None:
    from tailorcv.shared.costs.prices import table

    config = write_config(
        tmp_path,
        f'cv = "{master_cv}"\n\n[prices."claude-sonnet-5"]\ninput = 9.0\noutput = 9.0\n',
    )
    assert table(load_settings(config).prices)["claude-sonnet-5"].input == 9.0


def test_a_missing_master_cv_is_an_actionable_error(tmp_path: Path) -> None:
    config = write_config(tmp_path, 'cv = "nope.md"\n')
    with pytest.raises(ConfigError, match="Master CV not found"):
        load_settings(config).master_cv()


def test_an_unknown_key_names_itself_and_the_file(tmp_path: Path, master_cv: Path) -> None:
    config = write_config(tmp_path, f'cv = "{master_cv}"\npagez = 2\n')
    with pytest.raises(ConfigError) as caught:
        load_settings(config)
    assert "pagez" in str(caught.value)
    assert "unknown setting" in str(caught.value)
    assert str(config) in str(caught.value)


def test_malformed_toml_names_the_file(tmp_path: Path) -> None:
    config = write_config(tmp_path, "this is not toml = = =\n")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_settings(config)


def test_an_explicitly_named_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Configuration file not found"):
        find_config(tmp_path / "absent.toml")


def test_the_project_directory_is_searched_before_the_home_directory(tmp_path: Path) -> None:
    locations = config_locations(tmp_path)
    assert locations[0] == tmp_path / "tailorcv.toml"
    assert ".config" in str(locations[1])


def test_publication_bounds_are_validated(tmp_path: Path, master_cv: Path) -> None:
    config = write_config(
        tmp_path, f'cv = "{master_cv}"\nmin_publications = 5\nmax_publications = 2\n'
    )
    with pytest.raises(ConfigError, match="max_publications"):
        load_settings(config)


def test_tier_maps_to_a_model_and_an_effort(master_cv: Path) -> None:
    settings = Settings(cv=master_cv, provider=ProviderName.OPENAI)
    assert settings.choice_for(Tier.EXTRACTION).model == settings.choice_for(Tier.DRAFTING).model
    assert settings.choice_for(Tier.EXTRACTION).effort is Effort.LOW
    assert settings.choice_for(Tier.DRAFTING).effort is Effort.MEDIUM


def test_effort_is_configurable_per_tier(tmp_path: Path, master_cv: Path) -> None:
    """Written against Anthropic, which is why the file names it.

    A `[models.<provider>]` table only governs the provider in force, and this
    one is full of claude identifiers, so the provider belongs in the file
    beside them rather than being inherited from whatever the default happens
    to be that month.
    """
    body = "\n".join(
        [
            f'cv = "{master_cv}"',
            'provider = "anthropic"',
            "",
            "[models.anthropic.extraction]",
            'model = "claude-haiku-4-5-20251001"',
            'effort = "low"',
            "",
            "[models.anthropic.drafting]",
            'model = "claude-sonnet-5"',
            'effort = "max"',
            "",
        ]
    )
    settings = load_settings(write_config(tmp_path, body))
    assert settings.choice_for(Tier.EXTRACTION).effort is Effort.LOW
    assert settings.choice_for(Tier.DRAFTING).model == "claude-sonnet-5"


def test_a_vocabulary_table_reaches_the_deterministic_reading(
    tmp_path: Path, master_cv: Path
) -> None:
    """The `[vocabulary]` table as a settings file spells it, from file to lexicon."""
    from tailorcv.domain.taxonomy import extended

    body = "\n".join(
        [
            f'cv = "{master_cv}"',
            "",
            "[vocabulary]",
            'families = { data_architecture = ["canonical entity model"] }',
            'specialties = { master_reference_data = ["golden record service"] }',
            'aliases = { "master data management" = ["MDM"] }',
            "",
        ]
    )
    settings = load_settings(write_config(tmp_path, body))
    lexicon = extended(settings.vocabulary)
    family = lexicon.family("data_architecture")
    assert family is not None
    # Appended, not substituted: what shipped is still there.
    assert "canonical entity model" in family.terms
    assert "data governance" in family.terms
    assert lexicon.mentions("master data management", "we run MDM for two domains")


def test_a_vocabulary_key_that_names_nothing_is_refused_by_name(
    tmp_path: Path, master_cv: Path
) -> None:
    """A typo here would otherwise do nothing at all, quietly, for a year."""
    body = f'cv = "{master_cv}"\n\n[vocabulary]\nfamilies = {{ data_architcture = ["x"] }}\n'
    with pytest.raises(ConfigError, match="unknown family key"):
        load_settings(write_config(tmp_path, body))


def test_config_locations_are_not_reported_twice(tmp_path: Path) -> None:
    """On some platforms two of the three resolve to the same directory."""
    locations = config_locations(tmp_path)
    assert len(locations) == len(set(locations))
