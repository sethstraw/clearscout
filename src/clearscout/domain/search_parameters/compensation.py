"""What a role has to pay, and which period a stated figure could be quoted per."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from itertools import pairwise
from typing import Annotated, Literal, get_args

from pydantic import Field, model_validator

from clearscout.shared.models import StrictModel

type CompensationBasis = Literal["annual", "monthly", "daily", "hourly"]
BASES: tuple[CompensationBasis, ...] = get_args(CompensationBasis.__value__)
type CurrencyCode = Annotated[
    str,
    Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$"),
]

# Where a compensation band's stated numbers sit on the nought to hundred scale.
MINIMUM_STANDING = 60
TARGET_STANDING = 90

# Full-time equivalents. Used to test which period a figure could be quoted per;
# no comparison converts between periods.
ANNUAL_EQUIVALENT: dict[CompensationBasis, int] = {
    "annual": 1,
    "monthly": 12,
    "daily": 260,
    "hourly": 2080,
}
# How far outside the reader's own band a figure may sit and still be that period.
SCALE_MARGIN = 4


class CompensationBand(StrictModel):
    """What a role has to pay in one place, in one currency, on one basis.

    Minimum, target, and stretch separate a role somebody would take from one
    they want. All three are optional.
    """

    location: str = Field(min_length=1, max_length=80, description="A declared location key.")
    currency: CurrencyCode
    basis: CompensationBasis = "annual"
    minimum: Decimal | None = Field(default=None, ge=0)
    target: Decimal | None = Field(default=None, ge=0)
    stretch: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def ordered(self) -> CompensationBand:
        """Refuse a band whose numbers contradict each other."""
        stated = [
            (name, value)
            for name, value in (
                ("minimum", self.minimum),
                ("target", self.target),
                ("stretch", self.stretch),
            )
            if value is not None
        ]
        if not stated:
            raise ValueError("A compensation band states at least one of minimum, target, stretch.")
        for (first, low), (second, high) in pairwise(stated):
            if low > high:
                raise ValueError(f"{first} ({low}) is above {second} ({high}).")
        return self

    def standing(self, amount: Decimal) -> int:
        """Return how well ``amount`` meets this band, from 0 to 100.

        The three numbers anchor the scale: the minimum is pay worth taking, the
        target is pay worth wanting, and the stretch is the top of what was
        asked for. Pay between two anchors sits between their scores. Pay above a
        stated stretch scores as the target, since it can mean another level.
        """
        if self.above_stretch(amount):
            return TARGET_STANDING
        anchors = [
            (value, score)
            for value, score in (
                (self.minimum, MINIMUM_STANDING),
                (self.target, TARGET_STANDING),
                (self.stretch, 100),
            )
            if value is not None
        ]
        lowest, lowest_score = anchors[0]
        if amount >= anchors[-1][0]:
            return 100
        if amount <= lowest:
            return round(lowest_score * float(amount) / float(lowest)) if lowest else 0
        for (low, low_score), (high, high_score) in pairwise(anchors):
            if low <= amount <= high:
                share = float(amount - low) / float(high - low)
                return round(low_score + (high_score - low_score) * share)
        return 100

    def target_met(self, amount: Decimal) -> bool:
        """Return whether a stated target is at or below ``amount``."""
        return self.target is not None and amount >= self.target

    def above_stretch(self, amount: Decimal) -> bool:
        """Return whether ``amount`` is above a stated stretch."""
        return self.stretch is not None and amount > self.stretch


def basis_for(amount: Decimal, bands: Sequence[CompensationBand]) -> CompensationBasis | None:
    """Return the only period a figure of this size could be quoted per.

    Measured against ``bands``, and returns nothing unless exactly one period
    puts the figure in range.
    """
    scaled = [
        (band.minimum or band.target or band.stretch or Decimal(0)) * ANNUAL_EQUIVALENT[band.basis]
        for band in bands
    ]
    tops = [
        (band.stretch or band.target or band.minimum or Decimal(0)) * ANNUAL_EQUIVALENT[band.basis]
        for band in bands
    ]
    if not scaled or not min(scaled):
        return None
    low, high = min(scaled) / SCALE_MARGIN, max(tops) * SCALE_MARGIN
    fits = [basis for basis, factor in ANNUAL_EQUIVALENT.items() if low <= amount * factor <= high]
    return fits[0] if len(fits) == 1 else None
