"""Pair each stage's current inference setting with the next one down.

Choosing what to run, not running it: a paid comparison is approved separately
and executes through the production compiler and the official adapters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from tailorcv.application.config import (
    ANTHROPIC_EFFORT,
    MODEL_EFFORT,
    Effort,
    ModelChoice,
    ProviderName,
    Settings,
    Stage,
)
from tailorcv.shared.costs import Usage

# Lowest rung first, which is the order a comparison steps down.
LADDER: tuple[Effort, ...] = (
    Effort.NONE,
    Effort.MINIMAL,
    Effort.LOW,
    Effort.MEDIUM,
    Effort.HIGH,
    Effort.XHIGH,
    Effort.MAX,
)


def offered(provider: ProviderName, model: str = "") -> tuple[Effort, ...]:
    """Return the rungs one model accepts, lowest first."""
    accepted = MODEL_EFFORT.get(model)
    if accepted is not None:
        return tuple(rung for rung in LADDER if rung in accepted)
    if provider is ProviderName.ANTHROPIC:
        return tuple(rung for rung in LADDER if rung in ANTHROPIC_EFFORT)
    return LADDER


def one_level_lower(
    effort: Effort | None, provider: ProviderName, model: str = ""
) -> Effort | None:
    """Return the next rung down, or nothing when there is none to step to.

    Rungs the model does not accept are skipped, so lowering never produces a
    request it would refuse.
    """
    if effort is None:
        return None
    rungs = offered(provider, model)
    if effort not in rungs:
        return None
    position = rungs.index(effort)
    return rungs[position - 1] if position else None


@dataclass(frozen=True)
class StageComparison:
    """One stage's current setting and the cheaper one to measure it against."""

    stage: Stage
    current: ModelChoice
    lower: ModelChoice | None

    @property
    def comparable(self) -> bool:
        """Return whether there is a cheaper setting to run at all."""
        return self.lower is not None


def comparisons(settings: Settings) -> tuple[StageComparison, ...]:
    """Return one comparison per stage, in the order the stages are declared."""
    provider = settings.provider
    found: list[StageComparison] = []
    for stage in Stage:
        current = settings.choice_for_stage(stage)
        lower = one_level_lower(current.effort, provider, current.model)
        found.append(
            StageComparison(
                stage=stage,
                current=current,
                lower=None if lower is None else current.model_copy(update={"effort": lower}),
            )
        )
    return tuple(found)


@dataclass(frozen=True)
class Side:
    """What one setting cost and returned on one case.

    ``served`` is the model the provider says answered, empty when the run did
    not report one.
    """

    choice: ModelChoice
    usage: Usage = field(default_factory=Usage)
    dollars: Decimal | None = None
    defects: tuple[str, ...] = ()
    served: str = ""


@dataclass(frozen=True)
class ComparisonResult:
    """Both sides of one stage's comparison, reported without a verdict.

    No winner is named here. Which setting to keep is a decision about quality
    against cost, and it is recorded where the approval that paid for it is.
    """

    stage: Stage
    current: Side
    lower: Side

    @property
    def one_model_answered(self) -> bool:
        """Return whether one model answered both sides, which a valid effort pair needs."""
        served = {side.served for side in (self.current, self.lower) if side.served}
        return len(served) < 2

    def cheaper_by(self) -> Decimal | None:
        """Return what the lower setting saved.

        Nothing is returned when either side is unpriced, or when the two sides
        were answered by different models.
        """
        if self.current.dollars is None or self.lower.dollars is None:
            return None
        if not self.one_model_answered:
            return None
        return self.current.dollars - self.lower.dollars
