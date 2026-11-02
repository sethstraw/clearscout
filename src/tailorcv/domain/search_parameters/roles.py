"""Which titles, which rung of the ladder, and which kind of employer."""

from __future__ import annotations

import functools
import re
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from tailorcv.shared.models import StrictModel

TITLE_NOISE = frozenset({"a", "an", "and", "at", "for", "in", "of", "on", "the", "to", "with"})
TITLE_TOKEN_EQUIVALENTS: dict[str, str] = {
    "management": "manager",
    "informaticist": "informatics",
}


# Screening one queue page folds each row's title against every configured
# title, so the same few hundred strings are folded tens of thousands of times.
@functools.lru_cache(maxsize=4096)
def title_word_sequence(title: str) -> tuple[str, ...]:
    """Normalize role words without losing their order or word boundaries."""
    return tuple(
        TITLE_TOKEN_EQUIVALENTS.get(word, word)
        for word in re.findall(r"[a-z0-9]+", title.casefold())
        if word not in TITLE_NOISE
    )


def _singular(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _is_word(token: str) -> bool:
    return token.isascii() and token.isalnum()


_SEGMENT_END_WORDS = frozenset({"or", "i", "ii", "iii", "iv", "v", "sr", "jr"})


def _ends_segment(token: str) -> bool:
    return (
        not _is_word(token)
        or token.isdigit()
        or token in TITLE_NOISE
        or token in _SEGMENT_END_WORDS
        or token in LEVELS
    )


# A title's "management" reads as "manager" only where it ends its phrase segment
# (punctuation, a noise, level, or numeral word, or the end); an exclusion's matches both.
@functools.lru_cache(maxsize=4096)
def _exclusion_words(text: str) -> tuple[str, ...]:
    tokens = re.findall(r"[a-z0-9]+|[^a-z0-9\s]", text.casefold())
    words: list[str] = []
    for index, word in enumerate(tokens):
        if not _is_word(word) or word in TITLE_NOISE:
            continue
        following = tokens[index + 1] if index + 1 < len(tokens) else ""
        if word == "management" and following and not _ends_segment(following):
            words.append(word)
            continue
        words.append(_singular(TITLE_TOKEN_EQUIVALENTS.get(word, word)))
    return tuple(words)


@functools.lru_cache(maxsize=4096)
def _exclusion_pattern(exclusion: str) -> str:
    words = re.findall(r"[a-z0-9]+", exclusion.casefold())
    return " ".join(
        "(?:management|manager)" if word == "management" else re.escape(_singular(folded))
        for word in words
        if word not in TITLE_NOISE
        for folded in (TITLE_TOKEN_EQUIVALENTS.get(word, word),)
    )


def title_excluded(title: str, exclusions: list[str]) -> str | None:
    """Match a contiguous configured occupation phrase in a published title."""
    normalized = " ".join(_exclusion_words(title))
    for exclusion in exclusions:
        pattern = _exclusion_pattern(exclusion)
        if not pattern:
            continue
        if re.search(rf"(?<!\w){pattern}(?!\w)", normalized):
            return exclusion
    return None


# Order levels from lowest to highest for range comparisons.
LEVELS: tuple[str, ...] = (
    "intern",
    "junior",
    "associate",
    "mid",
    "senior",
    "lead",
    "staff",
    "principal",
    "director",
    "executive",
)


class Lane(StrEnum):
    """Which of two tracks a title belongs to."""

    LEADERSHIP = "leadership"
    SPECIALIST = "specialist"


LANES: tuple[str, ...] = tuple(item.value for item in Lane)

# As many synthesized names as one guided search may put up for review; every
# stated title is still searched and screened, whatever their count.
MAX_TITLES = 8


class TargetTitle(StrictModel):
    """One title worth reading for, and the track it belongs to."""

    title: str = Field(max_length=120)
    lane: Lane | None = None


class Seniority(StrictModel):
    """The band of the ladder worth reading, and the titles worth reading it for."""

    minimum: str = Field(default="", max_length=40)
    maximum: str = Field(default="", max_length=40)
    target_titles: list[TargetTitle] = Field(default_factory=list)
    excluded_titles: list[str] = Field(default_factory=list)
    # Saved role phrases retain titles for review; domain evidence supplies context.
    specialist_ic_role_nouns: list[str] = Field(default_factory=list)
    strong_domain_tokens: list[str] = Field(default_factory=list)
    # Guided titles are preferences; strict titles are gates. Exclusions always apply.
    title_matching: Literal["guided", "strict"] = "guided"

    @field_validator("target_titles", mode="before")
    @classmethod
    def bare_titles_state_no_lane(cls, value: object) -> object:
        """Accept a plain title, which is a title whose lane nobody has stated."""
        if isinstance(value, list):
            return [{"title": item} if isinstance(item, str) else item for item in value]
        return value

    @property
    def tracked_titles(self) -> tuple[TargetTitle, ...]:
        """Return the titles worth reading for with their tracks, without the blanks."""
        return tuple(
            TargetTitle(title=item.title.strip(), lane=item.lane)
            for item in self.target_titles
            if item.title.strip()
        )

    @property
    def stated_titles(self) -> tuple[str, ...]:
        """Return the titles worth reading for, without the blank entries."""
        return tuple(item.title for item in self.tracked_titles)

    @model_validator(mode="after")
    def known_levels(self) -> Seniority:
        """Reject a level name the ladder does not carry, and an inverted band."""
        for field_name in ("minimum", "maximum"):
            value = getattr(self, field_name)
            if value and value not in LEVELS:
                raise ValueError(f"{field_name} must be one of: {', '.join(LEVELS)}; got {value!r}")
        if (
            self.minimum
            and self.maximum
            and LEVELS.index(self.minimum) > LEVELS.index(self.maximum)
        ):
            raise ValueError(f"minimum ({self.minimum}) sits above maximum ({self.maximum}).")
        return self

    @property
    def stated(self) -> bool:
        """Return whether any level bound was given."""
        return bool(self.minimum or self.maximum)

    def covers(self, level: str) -> bool | None:
        """Return whether ``level`` is inside the band, or ``None`` when unbounded."""
        if not self.stated or level not in LEVELS:
            return None
        position = LEVELS.index(level)
        if self.minimum and position < LEVELS.index(self.minimum):
            return False
        return not (self.maximum and position > LEVELS.index(self.maximum))

    def exceeds(self, level: str) -> bool | None:
        """Return whether ``level`` is above the ceiling, or ``None`` when unknowable."""
        if not self.maximum or level not in LEVELS:
            return None
        return LEVELS.index(level) > LEVELS.index(self.maximum)


class EmploymentType(StrEnum):
    """A commonly published engagement shape, offered as a discoverable choice.

    Not exhaustive: "fractional", "locum", and "casual" are real, but a save
    refuses a new value outside this set and keeps only one already on record.
    """

    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    TEMPORARY = "temporary"
    INTERNSHIP = "internship"
    SEASONAL = "seasonal"
    FIXED_TERM = "fixed_term"
    FREELANCE = "freelance"
    APPRENTICESHIP = "apprenticeship"


EMPLOYMENT_TYPES: tuple[str, ...] = tuple(item.value for item in EmploymentType)

# Accepted archetypes shape the context a search reads, not its paid fan-out.
MAX_ARCHETYPES = 6


class Employment(StrictModel):
    """Which engagement shapes are on the table, as free strings.

    ``EMPLOYMENT_TYPES`` is what a picker offers.
    """

    types: list[str] = Field(default_factory=list)
    excluded_types: list[str] = Field(default_factory=list)
    payroll_arrangements: list[str] = Field(default_factory=list)


class EmployerArchetype(StrictModel):
    """A work domain and the employers explicitly associated with it."""

    name: str = Field(max_length=120)
    because: str = Field(default="", max_length=300)
    employer_names: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def named_membership(self) -> EmployerArchetype:
        """Require a domain name when an employer association is supplied."""
        if any(name.strip() for name in self.employer_names) and not self.name.strip():
            raise ValueError("Name the employer type before associating employers.")
        return self

    @property
    def stated(self) -> bool:
        """Return whether this archetype names anything."""
        return bool(self.name.strip())


class Employers(StrictModel):
    """Which organisations are worth an application, and which are not."""

    industries: list[str] = Field(default_factory=list)
    excluded_industries: list[str] = Field(default_factory=list)
    excluded_employers: list[str] = Field(default_factory=list)
    sizes: list[str] = Field(default_factory=list)
    archetypes: list[EmployerArchetype] = Field(default_factory=list)

    @field_validator("archetypes", mode="before")
    @classmethod
    def bare_archetypes_state_no_reason(cls, value: object) -> object:
        """Accept a plain name, which is an archetype nobody explained."""
        if isinstance(value, list):
            return [{"name": item} if isinstance(item, str) else item for item in value]
        return value

    @property
    def stated_archetypes(self) -> tuple[EmployerArchetype, ...]:
        """Return the archetypes worth searching, without the blank entries."""
        return tuple(
            EmployerArchetype(
                name=item.name.strip(),
                because=item.because.strip(),
                employer_names=[name.strip() for name in item.employer_names if name.strip()],
            )
            for item in self.archetypes
            if item.stated
        )
