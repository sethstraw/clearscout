"""One optional file, and a working default for everything in it.

Resolution order is flag, then environment, then file, then default, and
``clearscout doctor`` reports which of those won.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from clearscout.application.tailoring.guardrails import Guardrails
from clearscout.domain.policy import LearningLoopPolicy
from clearscout.domain.search_parameters import JobSearchParameters
from clearscout.domain.taxonomy import Vocabulary
from clearscout.shared.costs import ServiceTier
from clearscout.shared.costs.prices import PriceOverride
from clearscout.shared.errors import ConfigError

CONFIG_FILENAME = "clearscout.toml"
APP = "clearscout"


class ProviderName(StrEnum):
    """The two supported model providers."""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"


DEFAULT_PROVIDER = ProviderName.OPENAI


class Tier(StrEnum):
    """Which class of work a stage is doing; configuration maps each tier to a model."""

    EXTRACTION = "extraction"
    DRAFTING = "drafting"


class Effort(StrEnum):
    """How much thinking a provider should spend on a request.

    Both vendors expose the same ladder under different names: Anthropic as
    ``output_config.effort`` and OpenAI as ``reasoning.effort``.
    """

    # Provider effort vocabularies differ at the lowest level.
    NONE = "none"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


# Providers whose adapter runs a background web-search response a campaign can poll.
BACKGROUND_WEB_SEARCH_PROVIDERS = frozenset({ProviderName.OPENAI})
ANTHROPIC_EFFORT = frozenset({Effort.LOW, Effort.MEDIUM, Effort.HIGH, Effort.XHIGH, Effort.MAX})

# What a model accepts is narrower than what its provider offers. gpt-5.6-terra
# answers a `minimal` request with a 400 naming the rest of the ladder.
MODEL_EFFORT: dict[str, frozenset[Effort]] = {
    "gpt-5.6-terra": frozenset(
        {Effort.NONE, Effort.LOW, Effort.MEDIUM, Effort.HIGH, Effort.XHIGH, Effort.MAX}
    ),
    # `none` and `minimal` are unverified for this model.
    "gpt-5.6-luna": frozenset({Effort.LOW, Effort.MEDIUM, Effort.HIGH, Effort.XHIGH, Effort.MAX}),
    "gpt-6-sol": frozenset(
        {Effort.NONE, Effort.LOW, Effort.MEDIUM, Effort.HIGH, Effort.XHIGH, Effort.MAX}
    ),
    "gpt-6-luna": frozenset(
        {Effort.NONE, Effort.LOW, Effort.MEDIUM, Effort.HIGH, Effort.XHIGH, Effort.MAX}
    ),
    "gpt-6.1-sol": frozenset({Effort.LOW, Effort.MEDIUM, Effort.HIGH, Effort.XHIGH, Effort.MAX}),
}

# The discounted tier is served more slowly, and OpenAI's own guidance is that
# the ten minute SDK default is likely too short for it.
FLEX_TIMEOUT_FLOOR = 900.0


class Verbosity(StrEnum):
    """How expansive a response should be."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ImageDetail(StrEnum):
    """How much of an image a vendor reads before answering.

    ``original`` and ``auto`` send the image as it is; ``low`` and ``high`` let
    the vendor downscale it first.
    """

    LOW = "low"
    HIGH = "high"
    ORIGINAL = "original"
    AUTO = "auto"


class CriticStatistic(StrEnum):
    """How the critic's draws merge into one score; ``minimum`` takes each axis's harshest draw."""

    MEDIAN = "median"
    MINIMUM = "minimum"


class ModelChoice(BaseModel):
    """Which model answers a request, and how hard it should think.

    Anthropic publishes neither a verbosity nor an image-detail control, and
    only the OpenAI adapter renders those two, or a service tier.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str
    effort: Effort | None = None
    verbosity: Verbosity | None = None
    image_detail: ImageDetail | None = None
    service_tier: ServiceTier = ServiceTier.STANDARD


class TierModels(BaseModel):
    """The model choice used for each tier under one provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    extraction: ModelChoice
    drafting: ModelChoice


class Stage(StrEnum):
    """A call site whose inference settings can be chosen independently.

    Each value is the ``stage`` its request plan reports.
    """

    INGEST = "ingest"
    STRUCTURE = "structure"
    JOBSPEC = "jobspec"
    MATCH = "match"
    DRAFT = "draft"
    COVER = "cover"
    CRITIC = "critic"
    GUIDED_CAMPAIGN = "guided_campaign"
    SEARCH_VOCABULARY = "search_vocabulary"
    RELEVANCE_REVIEW = "relevance_review"
    POSTING_RECOVERY = "posting_recovery"


class StageSetting(BaseModel):
    """What one stage sends: its tier, its output ceiling, and its controls.

    ``tier`` supplies the model and effort until ``model`` states one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tier: Tier
    max_tokens: int = Field(gt=0)
    model: str = ""
    effort: Effort | None = None
    verbosity: Verbosity | None = None
    image_detail: ImageDetail | None = None


DEFAULT_STAGES: dict[Stage, StageSetting] = {
    Stage.INGEST: StageSetting(
        tier=Tier.EXTRACTION, max_tokens=8000, image_detail=ImageDetail.HIGH
    ),
    Stage.STRUCTURE: StageSetting(tier=Tier.DRAFTING, max_tokens=18000),
    Stage.JOBSPEC: StageSetting(tier=Tier.EXTRACTION, max_tokens=16000),
    # At low effort the model can answer without reasoning and understate a fit whose
    # qualifier the entry implies rather than names.
    Stage.MATCH: StageSetting(tier=Tier.EXTRACTION, max_tokens=8000, effort=Effort.MEDIUM),
    Stage.DRAFT: StageSetting(tier=Tier.DRAFTING, max_tokens=12000),
    Stage.COVER: StageSetting(tier=Tier.DRAFTING, max_tokens=8000),
    Stage.CRITIC: StageSetting(tier=Tier.DRAFTING, max_tokens=12000),
    # The capped campaign search plan stays below this response limit.
    Stage.GUIDED_CAMPAIGN: StageSetting(tier=Tier.EXTRACTION, max_tokens=6000),
    # The capped vocabulary proposal stays below this response limit.
    Stage.SEARCH_VOCABULARY: StageSetting(tier=Tier.EXTRACTION, max_tokens=6000),
    # An unused ceiling bills nothing; a truncated batch bills in full and is discarded.
    Stage.RELEVANCE_REVIEW: StageSetting(tier=Tier.EXTRACTION, max_tokens=16000),
    Stage.POSTING_RECOVERY: StageSetting(tier=Tier.EXTRACTION, max_tokens=2000),
}


DEFAULT_MODELS: dict[ProviderName, TierModels] = {
    ProviderName.ANTHROPIC: TierModels(
        extraction=ModelChoice(model="claude-sonnet-5", effort=Effort.LOW),
        drafting=ModelChoice(model="claude-sonnet-5", effort=Effort.HIGH),
    ),
    ProviderName.OPENAI: TierModels(
        extraction=ModelChoice(model="gpt-6.1-sol", effort=Effort.LOW),
        drafting=ModelChoice(model="gpt-6.1-sol", effort=Effort.MEDIUM),
    ),
}


class PostingSettings(BaseModel):
    """Per-workspace switches for how a posting is treated."""

    model_config = ConfigDict(extra="forbid")

    enable_hint: bool = True


class DiscoverySettings(BaseModel):
    """Per-workspace switches for what a guided search states."""

    model_config = ConfigDict(extra="forbid")

    max_carried_rejections: int = Field(default=40, ge=0)


class SectionNames(BaseModel):
    """The exact H2 headings the derivative emits.

    Configurable because heading text belongs to whatever document contract the
    Markdown will be rendered under, not to clearscout.
    """

    model_config = ConfigDict(extra="forbid")

    summary: str = "Summary"
    skills: str = "Core Skills"
    experience: str = "Professional Experience"
    education: str = "Education"
    publications: str = "Publications"


class Settings(BaseSettings):
    """Everything a run needs that is not the job description itself.

    Every field has a default, and the only one most people will set is ``cv``.
    Every field is settable from the environment under ``CLEARSCOUT_``, nested
    ones with a double underscore, so ``CLEARSCOUT_RULES__FORBIDDEN`` works.
    """

    model_config = SettingsConfigDict(
        extra="forbid",
        env_prefix="CLEARSCOUT_",
        env_nested_delimiter="__",
        nested_model_default_partial_update=True,
        toml_file=None,
        # Resolve paths once and preserve field validation after assignment.
        validate_assignment=True,
    )

    cv: Path | None = None
    output_dir: Path = Path("applications")
    provider: ProviderName = DEFAULT_PROVIDER
    pages: int = Field(default=2, ge=1, le=4)
    cache: bool = True

    rules: Guardrails = Field(default_factory=Guardrails)
    vocabulary: Vocabulary = Field(default_factory=Vocabulary)
    job_search: JobSearchParameters = Field(default_factory=JobSearchParameters)
    learning: LearningLoopPolicy = Field(default_factory=LearningLoopPolicy)
    postings: PostingSettings = Field(default_factory=PostingSettings)
    discovery: DiscoverySettings = Field(default_factory=DiscoverySettings)

    full_bullet_years: int = Field(default=15, ge=1)
    # No publication floor is imposed; publications share the page budget.
    min_publications: int = Field(default=0, ge=0)
    max_publications: int = Field(default=5, ge=0)
    # Match protected labels case-insensitively at line starts.
    header_drop: tuple[str, ...] = ()
    max_draft_attempts: int = Field(default=3, ge=1, le=5)
    # Each critic trial is a separate paid call on the same draft.
    critic_trials: int = Field(default=1, ge=1, le=5)
    # Which draw the five writing-quality scores are taken from once the trials are in.
    critic_score_statistic: CriticStatistic = CriticStatistic.MEDIAN
    # The bounds outside which a draft is sent to the critic.
    critic_coverage_floor: float = Field(default=0.6, ge=0.0, le=1.0)
    critic_transferable_ceiling: float = Field(default=0.5, ge=0.0, le=1.0)
    max_bullets_per_role: int = Field(default=9, ge=1, le=40)
    min_bullets_per_role: int = Field(default=2, ge=0, le=10)
    # Bound provider calls so retries cannot make a run unbounded.
    request_timeout: float = Field(default=300.0, gt=0)
    # Copy nested tier settings so batched runs do not share mutable values.
    models: dict[ProviderName, TierModels] = Field(
        default_factory=lambda: {
            name: tiers.model_copy(deep=True) for name, tiers in DEFAULT_MODELS.items()
        }
    )
    # Copy nested stage settings so batched runs do not share mutable values.
    stages: dict[Stage, StageSetting] = Field(
        default_factory=lambda: {
            stage: setting.model_copy(deep=True) for stage, setting in DEFAULT_STAGES.items()
        }
    )
    sections: SectionNames = Field(default_factory=SectionNames)
    prices: dict[str, PriceOverride] = Field(default_factory=dict)

    source: Path | None = Field(default=None, exclude=True)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,  # noqa: ARG003 - unused, but required
        file_secret_settings: PydanticBaseSettingsSource,  # noqa: ARG003 - unused, but required
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Return the sources highest priority first: flag, then environment, then file."""
        return (init_settings, env_settings, TomlConfigSettingsSource(settings_cls))

    def tiers_for(self, provider: ProviderName) -> TierModels:
        """Return the model choices configured for a provider, in force or not."""
        return self.models.get(provider) or DEFAULT_MODELS[provider]

    def policy_for(self, stage: Stage) -> StageSetting:
        """Return the inference settings this run uses for one stage."""
        return self.stages.get(stage) or DEFAULT_STAGES[stage]

    def choice_for(self, tier: Tier) -> ModelChoice:
        """Return the model and effort this run should use for ``tier``."""
        tiers = self.tiers_for(self.provider)
        choice = tiers.extraction if tier is Tier.EXTRACTION else tiers.drafting
        return self._offered(choice, f"the {tier.value} tier")

    def choice_for_stage(
        self, stage: Stage | str, service_tier: ServiceTier = ServiceTier.STANDARD
    ) -> ModelChoice:
        """Return the model and effort one stage sends, falling back to its tier.

        A plan is a dataclass, so its stage is coerced here rather than trusted.
        """
        stage = Stage(stage)
        setting = self.policy_for(stage)
        tiers = self.tiers_for(self.provider)
        seed = tiers.extraction if setting.tier is Tier.EXTRACTION else tiers.drafting
        choice = ModelChoice(
            model=setting.model or seed.model,
            effort=setting.effort if setting.effort is not None else seed.effort,
            verbosity=setting.verbosity if setting.verbosity is not None else seed.verbosity,
            image_detail=(
                setting.image_detail if setting.image_detail is not None else seed.image_detail
            ),
            service_tier=service_tier,
        )
        return self._offered(choice, f"the {stage.value} stage")

    def unattended_tier(self) -> ServiceTier:
        """Return the tier queue work asks for, which only OpenAI serves."""
        return ServiceTier.FLEX if self.provider is ProviderName.OPENAI else ServiceTier.STANDARD

    def timeout_for(self, service_tier: ServiceTier = ServiceTier.STANDARD) -> float:
        """Return the request timeout one tier needs, never below the stated one."""
        if service_tier is ServiceTier.FLEX:
            return max(self.request_timeout, FLEX_TIMEOUT_FLOOR)
        return self.request_timeout

    def _offered(self, choice: ModelChoice, subject: str) -> ModelChoice:
        """Refuse an effort or a service tier the chosen provider does not have.

        OpenAI adds ``none`` and ``minimal`` at the bottom of the ladder and
        Anthropic rejects both with a 400.
        """
        if (
            self.provider is ProviderName.ANTHROPIC
            and choice.service_tier is not ServiceTier.STANDARD
        ):
            raise ConfigError(
                f"Anthropic serves no {choice.service_tier.value!r} tier, "
                f"which is set for {subject}.\n"
                "  It is an OpenAI tier; run this against OpenAI instead."
            )
        if (
            self.provider is ProviderName.ANTHROPIC
            and choice.effort is not None
            and choice.effort not in ANTHROPIC_EFFORT
        ):
            allowed = ", ".join(sorted(effort.value for effort in ANTHROPIC_EFFORT))
            raise ConfigError(
                f"Anthropic has no effort level called {choice.effort.value!r}, "
                f"which is set for {subject}.\n"
                f"  Anthropic accepts: {allowed}\n"
                f"  {choice.effort.value!r} is an OpenAI level; set it under "
                "[models.openai] instead."
            )
        accepted = MODEL_EFFORT.get(choice.model)
        if choice.effort is not None and accepted is None and self.provider is ProviderName.OPENAI:
            recorded = ", ".join(sorted(MODEL_EFFORT))
            raise ConfigError(
                f"No effort levels are recorded for {choice.model}, and {subject} "
                f"states {choice.effort.value!r}.\n"
                "  A model that rejects a level answers with a billed error, so an "
                "unrecorded model may not state one.\n"
                f"  Leave the effort unset for that stage, or use: {recorded}"
            )
        if choice.effort is not None and accepted is not None and choice.effort not in accepted:
            allowed = ", ".join(sorted(effort.value for effort in accepted))
            raise ConfigError(
                f"{choice.model} has no effort level called {choice.effort.value!r}, "
                f"which is set for {subject}.\n"
                f"  It accepts: {allowed}"
            )
        return choice

    def master_cv(self, explicit: Path | None = None, start: Path | None = None) -> Path:
        """Return the master CV to read, discovering it when it was never set."""
        from clearscout.application.master_cv.discovery import resolve_master_cv

        return resolve_master_cv(explicit or self.cv, start)


def config_locations(start: Path | None = None) -> list[Path]:
    """Return every place a configuration file is looked for, in order.

    The project directory first, because a configuration next to the CV is
    almost always the one meant. Then ``~/.config``, then the platform's own
    location.
    """
    root = start or Path.cwd()
    ordered = [
        root / CONFIG_FILENAME,
        Path.home() / ".config" / APP / CONFIG_FILENAME,
        Path(user_config_dir(APP)) / CONFIG_FILENAME,
    ]
    # Avoid duplicate paths when platform locations resolve identically.
    return list(dict.fromkeys(ordered))


def find_config(explicit: Path | None = None, start: Path | None = None) -> Path | None:
    """Locate the configuration file, or return ``None`` when there is none.

    No configuration is a supported way to run this tool, not an error.
    """
    if explicit is not None:
        path = explicit.expanduser()
        if not path.is_file():
            raise ConfigError(f"Configuration file not found: {path}")
        return path
    return next((path for path in config_locations(start) if path.is_file()), None)


def load_settings(
    config_path: Path | None = None,
    overrides: Mapping[str, object] | None = None,
    start: Path | None = None,
) -> Settings:
    """Build the effective settings for one run.

    ``overrides`` carries the command-line flags; a key whose value is ``None``
    is treated as absent so that an unset option does not shadow the file or the
    environment.
    """
    path = find_config(config_path, start)
    given: dict[str, Any] = {
        key: value for key, value in (overrides or {}).items() if value is not None
    }

    settings_cls = _bound_to(path)
    try:
        settings = settings_cls(**given)
    except ValidationError as error:
        raise ConfigError(_explain(path, error)) from error
    except tomllib.TOMLDecodeError as error:
        # TOML parsing is lazy, so report syntax errors while building settings.
        raise ConfigError(f"{path} is not valid TOML: {error}") from error

    settings.source = path
    _resolve_paths(settings, path.parent if path is not None else Path.cwd())
    if settings.max_publications < settings.min_publications:
        where = f"In {path}: " if path is not None else ""
        raise ConfigError(
            f"{where}max_publications ({settings.max_publications}) is below "
            f"min_publications ({settings.min_publications}). Raise one or lower the other."
        )
    return settings


def _bound_to(path: Path | None) -> type[Settings]:
    """Return a Settings class whose TOML source reads ``path``.

    pydantic-settings resolves its file source from the class's configuration,
    so binding a discovered path means producing a subclass carrying it.
    """
    if path is None:
        return Settings
    bound = SettingsConfigDict(**{**Settings.model_config, "toml_file": path})
    return type("BoundSettings", (Settings,), {"model_config": bound})


def _explain(path: Path | None, error: ValidationError) -> str:
    """Turn a validation failure into something a person can act on.

    Pydantic's own rendering names the field but not the file.
    """
    where = str(path) if path is not None else "the command-line options"
    lines = [f"Invalid configuration in {where}:"]
    for problem in error.errors():
        location = ".".join(str(part) for part in problem["loc"]) or "(top level)"
        message = problem["msg"]
        if problem["type"] == "extra_forbidden":
            message = "unknown setting; check the spelling, or see README for the full list"
        lines.append(f"  {location}: {message}")
    return "\n".join(lines)


def _resolve_paths(settings: Settings, base: Path) -> None:
    """Resolve the CV against the config's ``base`` and the output directory against the cwd."""
    if settings.cv is not None:
        settings.cv = _resolve(settings.cv, base)
    settings.output_dir = _resolve(settings.output_dir, Path.cwd())


def _resolve(path: Path, base: Path) -> Path:
    expanded = path.expanduser()
    return expanded if expanded.is_absolute() else (base / expanded).resolve()
