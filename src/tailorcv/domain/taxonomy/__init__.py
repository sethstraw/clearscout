"""Typed operations over the shipped role-family vocabulary."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tailorcv.domain.taxonomy.data import FAMILIES, Family, Specialty

SEPARATOR = re.compile(r"[^a-z0-9+]+")


def normalize(text: str) -> str:
    """Reduce text to the spaced, lowercase form phrases are matched against."""
    return f" {SEPARATOR.sub(' ', text.casefold()).strip()} "


FAMILY_KEYS: frozenset[str] = frozenset(family.key for family in FAMILIES)
SPECIALTY_KEYS: frozenset[str] = frozenset(
    specialty.key for family in FAMILIES for specialty in family.specialties
)


class Vocabulary(BaseModel):
    """Installation-specific additions to the shipped vocabulary."""

    model_config = ConfigDict(extra="forbid")

    families: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Extra evidence phrases for a role family, keyed by family.",
    )
    specialties: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Extra evidence phrases for a specialty, keyed by specialty.",
    )
    aliases: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Other spellings of a phrase, keyed by the phrase they stand for.",
    )

    @field_validator("families")
    @classmethod
    def _known_families(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        """Reject a family key that names nothing, listing the ones that do."""
        return _known(value, FAMILY_KEYS, "family")

    @field_validator("specialties")
    @classmethod
    def _known_specialties(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        """Reject a specialty key that names nothing, listing the ones that do."""
        return _known(value, SPECIALTY_KEYS, "specialty")


def _known(value: dict[str, list[str]], allowed: frozenset[str], kind: str) -> dict[str, list[str]]:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(
            f"unknown {kind} key(s): {', '.join(unknown)}. "
            f"The {kind} keys are: {', '.join(sorted(allowed))}"
        )
    return value


@dataclass(frozen=True)
class Taxonomy:
    """The families and phrase aliases in force for one run."""

    families: tuple[Family, ...] = FAMILIES
    aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def family(self, key: str) -> Family | None:
        """Return the family with ``key``, or nothing when there is no such family."""
        return next((item for item in self.families if item.key == key), None)

    def mentions(self, phrase: str, text: str) -> bool:
        """Return whether text uses a phrase or one of its declared aliases."""
        haystack = normalize(text)
        return any(
            f" {normalize(spelling).strip()} " in haystack
            for spelling in (phrase, *self.aliases.get(phrase, ()))
        )

    def present(self, phrases: tuple[str, ...], text: str) -> tuple[str, ...]:
        """Return the phrases text uses, in declaration order."""
        return tuple(phrase for phrase in phrases if self.mentions(phrase, text))


def extended(vocabulary: Vocabulary | None = None) -> Taxonomy:
    """Return the shipped taxonomy with one installation's additions folded in."""
    if vocabulary is None or not (
        vocabulary.families or vocabulary.specialties or vocabulary.aliases
    ):
        return Taxonomy()
    families = tuple(_with_additions(family, vocabulary) for family in FAMILIES)
    aliases = {
        phrase: tuple(spellings) for phrase, spellings in vocabulary.aliases.items() if spellings
    }
    return Taxonomy(families=families, aliases=aliases)


def _with_additions(family: Family, vocabulary: Vocabulary) -> Family:
    extra = tuple(vocabulary.families.get(family.key, ()))
    specialties = tuple(
        Specialty(
            key=specialty.key,
            label=specialty.label,
            terms=(*specialty.terms, *vocabulary.specialties.get(specialty.key, ())),
        )
        for specialty in family.specialties
    )
    if not extra and specialties == family.specialties:
        return family
    return Family(
        key=family.key,
        label=family.label,
        sector=family.sector,
        titles=family.titles,
        terms=(*family.terms, *extra),
        specialties=specialties,
    )


def says(text: str, phrases: tuple[str, ...]) -> tuple[str, ...]:
    """Return which phrases appear in text as whole phrases."""
    haystack = normalize(text)
    return tuple(phrase for phrase in phrases if f" {normalize(phrase).strip()} " in haystack)


__all__ = [
    "FAMILIES",
    "FAMILY_KEYS",
    "SPECIALTY_KEYS",
    "Family",
    "Specialty",
    "Taxonomy",
    "Vocabulary",
    "extended",
    "normalize",
]
