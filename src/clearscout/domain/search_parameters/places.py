"""Where a person will work, on what terms, and what standing they hold there."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from clearscout.domain.identity import mentions
from clearscout.shared.models import StrictModel

# Keep preference names identical to the deterministic reading vocabulary.
ARRANGEMENTS: tuple[str, ...] = ("remote", "hybrid", "on site")


class Acceptance(StrEnum):
    """How a person feels about one option; no value means the question was never answered."""

    PREFERRED = "preferred"
    ACCEPTABLE = "acceptable"
    REFUSED = "refused"


class WorkEligibility(StrEnum):
    """Whether a person can take work in a place; the document held goes in ``basis``, ungated."""

    AUTHORIZED = "authorized"
    SPONSORSHIP_REQUIRED = "sponsorship required"
    INELIGIBLE = "ineligible"


class WorkingTimeZone(StrEnum):
    """North American working zones the deterministic fit reader compares."""

    PACIFIC = "Pacific"
    MOUNTAIN = "Mountain"
    CENTRAL = "Central"
    EASTERN = "Eastern"


# A posting somewhere the reader declared no standing for. Not a standing they
# hold, so it sits beside the vocabulary rather than inside it.
OUTSIDE_DECLARED = "outside your declared places"


class Arrangement(StrictModel):
    """Which working arrangements are wanted, tolerated, or refused.

    ``maximum_office_days`` is separate from the three acceptances because a
    posting can satisfy one and fail the other. It counts days per week.
    """

    remote: Acceptance | None = None
    hybrid: Acceptance | None = None
    on_site: Acceptance | None = None
    maximum_office_days: int | None = Field(
        default=None, ge=0, le=7, description="Office days per week this person will attend."
    )
    maximum_travel_percent: int | None = Field(default=None, ge=0, le=100)
    relocation: bool | None = None
    onsite_requires_paid_travel: bool | None = Field(
        default=None,
        description="Whether office attendance is acceptable only when the employer funds travel.",
    )

    def named(self) -> dict[str, Acceptance | None]:
        """Return the acceptances keyed by the arrangement names a reading uses."""
        return {"remote": self.remote, "hybrid": self.hybrid, "on site": self.on_site}

    @property
    def stated(self) -> bool:
        """Return whether any arrangement question was answered."""
        return any(value is not None for value in self.named().values())

    def acceptance_of(self, arrangement: str) -> Acceptance | None:
        """Return how this person feels about ``arrangement``, or ``None``."""
        return self.named().get(arrangement)

    def over(self, base: Arrangement) -> Arrangement:
        """Return this block layered on ``base``, field by field.

        Field by field rather than wholesale, because the common case is a
        location that differs in one respect and inherits the rest.
        """
        merged = base.model_dump()
        merged.update(
            {name: value for name, value in self.model_dump().items() if value is not None}
        )
        return Arrangement.model_validate(merged)


class Location(StrictModel):
    """One place a person is willing to work, and what their standing there is.

    ``key`` is the identifier the rest of the system uses, and the token written
    into a stored evaluation's residence rule, so an existing ledger keeps
    working by declaring its own tokens here.
    """

    key: str = Field(min_length=1, max_length=80)
    country: str = Field(min_length=1, max_length=120)
    region: str = Field(default="", max_length=160)
    metro: str = Field(default="", max_length=160)
    eligibility: WorkEligibility = WorkEligibility.AUTHORIZED
    basis: str = Field(
        default="",
        max_length=300,
        description="Why the person may work here, in their own words. Never inferred.",
    )
    aliases: list[str] = Field(
        default_factory=list,
        description=(
            "Publisher-specific names for this exact place. Common country spellings are built in."
        ),
    )
    resident: bool = Field(
        default=False,
        description=(
            "Whether the person lives here. A posting requiring residence elsewhere is refused "
            "unless the person will relocate."
        ),
    )
    # An unset location inherits the general preference.
    arrangement: Arrangement | None = None

    @property
    def actionable(self) -> bool:
        """Return whether an application here is worth making at all."""
        return self.eligibility is not WorkEligibility.INELIGIBLE

    def render(self) -> str:
        """Return the place as one line, finest granularity first."""
        parts = [part for part in (self.metro, self.region, self.country) if part]
        return ", ".join(parts)

    def names(self) -> tuple[str, ...]:
        """Return this place's fields and aliases; search parameters resolve country spellings."""
        written = (self.metro, self.region, self.country, *self.aliases)
        return tuple(dict.fromkeys(part.strip() for part in written if part.strip()))

    @property
    def granularity(self) -> int:
        """Return how specific this declaration is: metro, region, or country."""
        if self.metro:
            return 3
        if self.region:
            return 2
        return 1

    def named_tier(self, haystack: str) -> int | None:
        """Return how specifically ``haystack`` names this place, or None.

        An alias answers at this declaration's own granularity.
        """
        for tier, value in ((3, self.metro), (2, self.region), (1, self.country)):
            if value and mentions(haystack, value.casefold()):
                return tier
        if any(mentions(haystack, alias.casefold()) for alias in self.aliases):
            return self.granularity
        return None


class CountryNames(StrictModel):
    """One country a posting's place may name, and the spellings boards write."""

    country: str = Field(min_length=1, max_length=120)
    spellings: list[str] = Field(default_factory=list)
