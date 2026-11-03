"""The whole declared search, and the questions a posting is answered against."""

from __future__ import annotations

import re

from pydantic import Field, model_validator

from clearscout.domain.countries import CountryTable, table_for
from clearscout.domain.identity import written_mention_spans
from clearscout.domain.places import PlaceReader
from clearscout.domain.search_parameters.compensation import CompensationBand
from clearscout.domain.search_parameters.focus import ClusterKind, Focus
from clearscout.domain.search_parameters.places import (
    ARRANGEMENTS,
    Arrangement,
    CountryNames,
    Location,
    WorkingTimeZone,
)
from clearscout.domain.search_parameters.roles import Employers, Employment, Seniority
from clearscout.shared.models import StrictModel

# A capitalized name and a comma right before a region, as in "Red Hill, Northland".
_PLACE_BEFORE = re.compile(r"(?P<place>[A-Z][\w.'-]*(?:[ \t]+[A-Z][\w.'-]*)*)[ \t]*,\s*$")
_PLACE_REACH = 80


def _after_a_place(text: str, start: int) -> bool:
    """Return whether a place name, not an arrangement label, is written just before ``start``."""
    found = _PLACE_BEFORE.search(text, max(0, start - _PLACE_REACH), start)
    if found is None:
        return False
    written = found["place"].casefold().replace("-", " ")
    return not written.endswith(ARRANGEMENTS)


class JobSearchParameters(StrictModel):
    """Everything about the search that is not about the career record.

    Every field is optional and every default is empty, so this object's zero
    value is "no opinions expressed" and every reading abstains.
    """

    locations: list[Location] = Field(default_factory=list)
    working_time_zone: WorkingTimeZone | None = Field(
        default=None,
        description=(
            "The time zone the person normally works from. Posting time-zone requirements "
            "are compared with this value; locations do not imply it."
        ),
    )
    working_day_shift_hours: int = Field(
        default=0,
        ge=0,
        le=12,
        description=(
            "Hours the person can shift their working day. Added to a posting's own "
            "time-zone tolerance."
        ),
    )
    countries: list[CountryNames] = Field(
        default_factory=list,
        description="Countries a posting's place may name. Empty uses the shipped table.",
    )
    arrangement: Arrangement = Field(default_factory=Arrangement)
    compensation: list[CompensationBand] = Field(default_factory=list)
    seniority: Seniority = Field(default_factory=Seniority)
    employment: Employment = Field(default_factory=Employment)
    employers: Employers = Field(default_factory=Employers)
    focus: Focus = Field(default_factory=Focus)
    score_weight_set: str = Field(
        default="",
        max_length=160,
        description="Name of the scoring policy this search uses. Empty means the default.",
    )
    opportunity_thesis: str = Field(
        default="",
        max_length=4000,
        description=(
            "Free-text statement of the target opportunity shape; model prompts read it as the "
            "person's stated preference, not evidence."
        ),
    )
    excluded_conditions: list[tuple[str, str]] = Field(
        default_factory=list,
        description=(
            "(condition, normalized_reason) pairs the relevance review applies as stated "
            "hard-exclusion conditions against posting text."
        ),
    )

    @model_validator(mode="after")
    def coherent(self) -> JobSearchParameters:
        """Refuse duplicate location keys and bands pointing at nothing.

        A band whose location key names no declared location fails silently
        otherwise: it simply never matches anything.
        """
        keys = [location.key for location in self.locations]
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            raise ValueError(f"Duplicate location keys: {', '.join(duplicates)}")
        known = set(keys)
        unknown = sorted(
            {band.location for band in self.compensation if band.location not in known}
        )
        if unknown:
            listed = ", ".join(sorted(known)) or "none"
            raise ValueError(
                f"Compensation bands name locations that are not declared: {', '.join(unknown)}.\n"
                f"  Declared location keys: {listed}"
            )
        return self

    @property
    def configured(self) -> bool:
        """Return whether anybody has expressed an opinion here at all."""
        return bool(
            self.locations
            or self.working_time_zone is not None
            or self.working_day_shift_hours > 0
            or self.compensation
            or self.arrangement.stated
            or self.arrangement.maximum_office_days is not None
            or self.arrangement.maximum_travel_percent is not None
            or self.arrangement.relocation is not None
            or self.arrangement.onsite_requires_paid_travel is not None
            or self.seniority.stated
            or self.seniority.target_titles
            or self.seniority.excluded_titles
            or self.employment.types
            or self.employment.excluded_types
            or self.employment.payroll_arrangements
            or self.employers.industries
            or self.employers.excluded_industries
            or self.employers.excluded_employers
            or self.employers.sizes
            or self.employers.stated_archetypes
            or self.focus.stated_clusters(ClusterKind.CAPABILITY)
            or self.focus.stated_clusters(ClusterKind.DOMAIN)
            or self.focus.stated_boundaries
            or self.excluded_conditions
            or self.opportunity_thesis.strip()
        )

    def location(self, key: str) -> Location | None:
        """Return the declared location with this key, or ``None``."""
        return next((item for item in self.locations if item.key == key), None)

    def bands_for(self, key: str) -> tuple[CompensationBand, ...]:
        """Return every band declared for one location."""
        return tuple(band for band in self.compensation if band.location == key)

    def arrangement_for(self, key: str | None) -> Arrangement:
        """Return a location's arrangement over the general one; ``None`` gives the general one."""
        place = self.location(key) if key else None
        if place is None or place.arrangement is None:
            return self.arrangement
        return place.arrangement.over(self.arrangement)

    def matching_locations(self, text: str) -> tuple[Location, ...]:
        """Return declared locations whose words appear whole in ``text``, most specific tier."""
        named = self._named_place_tiers(text)
        precise = [(tier, location) for tier, location in named if tier == location.granularity]
        return self._most_specific(precise or named)

    def markets_named(self, text: str) -> tuple[Location, ...]:
        """Return places ``text`` names at their own granularity, not a country over finer ones."""
        named = self._named_place_tiers(text)
        return self._most_specific(
            [(tier, location) for tier, location in named if tier == location.granularity]
        )

    def metros_named_by_region(self, text: str) -> tuple[Location, ...]:
        """Return the declared metros ``text`` names only by the region they sit in.

        A region written after a place anywhere, as in "Easttown, Northland", names
        another city in it, so the metro does not count.
        """
        return tuple(
            location
            for tier, location in self._named_place_tiers(text)
            if tier == 2
            and location.granularity == 3
            and not any(
                _after_a_place(text, start)
                for start, _ in written_mention_spans(text, location.region.strip())
            )
        )

    def _named_place_tiers(self, text: str) -> list[tuple[int, Location]]:
        haystack = text.casefold()
        table = self.country_table
        named_countries = {country.casefold() for country in PlaceReader(table).read(text).explicit}
        return [
            (tier, location)
            for location in self.locations
            if (tier := self._place_tier(table, location, haystack, named_countries)) is not None
        ]

    @staticmethod
    def _most_specific(tiered: list[tuple[int, Location]]) -> tuple[Location, ...]:
        if not tiered:
            return ()
        most_specific = max(tier for tier, _ in tiered)
        return tuple(location for tier, location in tiered if tier == most_specific)

    @property
    def stated_countries(self) -> dict[str, tuple[str, ...]]:
        """Return the countries this person named, keyed by the name to report."""
        return {
            item.country.strip(): tuple(
                spelling.strip() for spelling in item.spellings if spelling.strip()
            )
            for item in self.countries
            if item.country.strip()
        }

    def country_named_in(self, text: str) -> str:
        """Return the declared country ``text`` names, if it names exactly one.

        Any of a place's own words answers, including a metro or an alias.
        """
        haystack = text.casefold()
        table = self.country_table
        named_countries = {country.casefold() for country in PlaceReader(table).read(text).explicit}
        named = {
            location.country
            for location in self.locations
            if self._place_tier(table, location, haystack, named_countries) is not None
        }
        return next(iter(named)) if len(named) == 1 else ""

    @property
    def country_table(self) -> CountryTable:
        """Return the shipped country spellings extended by this person's own."""
        return table_for(self.stated_countries)

    @property
    def place_reader(self) -> PlaceReader:
        """Return a place reader over this person's country table."""
        return PlaceReader(self.country_table)

    @staticmethod
    def _place_tier(
        table: CountryTable, location: Location, haystack: str, named_countries: set[str]
    ) -> int | None:
        """Return how specifically ``haystack`` names ``location``, or None.

        The table reads the whole string, so when it knows the declared country
        and names others instead, a bare country word was a street or a
        building. A declared spelling the table cannot place keeps its own match.
        """
        tier = location.named_tier(haystack)
        known = table.named_in(location.country).casefold()
        if tier == 1 and known and named_countries and known not in named_countries:
            return None
        if tier is None and known and known in named_countries:
            return 1
        return tier

    def currency_in(self, country: str) -> str:
        """Return the one currency the declared bands use in ``country``."""
        if not country:
            return ""
        keys = {
            location.key
            for location in self.locations
            if location.country.casefold() == country.casefold()
        }
        currencies = {band.currency for band in self.compensation if band.location in keys}
        return next(iter(currencies)) if len(currencies) == 1 else ""

    @property
    def actionable_locations(self) -> tuple[str, ...]:
        """Return the keys an application is worth making in; empty means unknown, not nowhere."""
        return tuple(item.key for item in self.locations if item.actionable)
