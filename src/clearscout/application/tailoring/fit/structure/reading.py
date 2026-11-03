"""Reading types: answers, stances, blocks, requirement lines, and disagreements."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import StrEnum
from typing import Any, Generic, Literal, TypeVar

from clearscout.application.contracts import CompensationInput
from clearscout.application.tailoring.fit.lexicon import NOT_STATED

ValueT = TypeVar("ValueT")


@dataclass(frozen=True)
class Answer(Generic[ValueT]):
    """One derived answer, or an explicit refusal to give one.

    ``value`` is ``None`` when the text does not support a conclusion, and
    ``note`` then says what was looked for. ``conflict`` marks the second reason
    for having no value: not that the posting said nothing, but that what it said
    does not add up.
    """

    question: str
    value: ValueT | None = None
    evidence: tuple[str, ...] = ()
    note: str = ""
    conflict: bool = False

    @property
    def stated(self) -> bool:
        """Return whether the text answered this question at all."""
        return self.value is not None

    def render(self) -> str:
        """Return the answer as it is printed, including the abstention."""
        return NOT_STATED if self.value is None else str(self.value)

    def because(self) -> str:
        """Return what the answer was read from, or why nothing was concluded."""
        return "; ".join(self.evidence) if self.evidence else self.note


class Stance(StrEnum):
    """How firmly a posting asks for one thing, in its own words."""

    MUST_HAVE = "must-have"
    PREFERRED = "preferred"


class SectionKind(StrEnum):
    """What a block of a posting is, judged by its heading."""

    REQUIREMENT = "requirement"
    PREFERRED = "preferred"
    RESPONSIBILITY = "responsibility"
    BOILERPLATE = "boilerplate"
    OTHER = "other"


@dataclass(frozen=True)
class Block:
    """One headed block of a posting, and what its heading makes it."""

    heading: str
    kind: SectionKind
    lines: tuple[str, ...]


@dataclass(frozen=True)
class RequirementLine:
    """One thing the posting asks for, how firmly, and the wording that says so."""

    text: str
    stance: Stance
    reason: str
    # ``text`` is the printable quote; readers match against the whole line.
    whole: str
    # A bare "or" line between this line and the next makes the next an alternative.
    or_next: bool = False
    # Must-have only because it names a licence to hold, with no marker or heading saying so.
    bare: bool = False


# Every Answer field of Reading, so a Disagreement naming one is checked by mypy.
AnswerName = Literal[
    "role_family",
    "specialty",
    "orientation",
    "seniority",
    "years",
    "education",
    "location",
    "authorization",
    "sections",
    "must_have",
    "preferred",
    "record_families",
    "record_years",
    "place_fit",
    "arrangement_fit",
    "travel_fit",
    "time_zone_fit",
    "level_fit",
    "residence_requirement",
    "pay_fit",
]


@dataclass(frozen=True)
class Disagreement:
    """One place the model's reading and the rules' reading do not match."""

    subject: str
    model_says: str
    rules_say: str
    detail: str = ""
    # The Reading answer the rules' side comes from; the row cites that answer's evidence.
    answer: AnswerName | None = None
    # Index into the spec's hard gates of the gate the row quotes; it cites that gate's anchor.
    gate: int | None = None
    # Posting lines the row quotes when no reading answer or gate backs it.
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class Reading:
    """What the rules read out of one posting and one master CV.

    Every field is an :class:`Answer`, so "not stated" is a value this object
    holds rather than a string a caller has to remember to print.
    """

    role_family: Answer[str]
    specialty: Answer[str]
    orientation: Answer[str]
    seniority: Answer[str]
    years: Answer[int]
    education: Answer[str]
    location: Answer[str]
    authorization: Answer[str]
    sections: Answer[str]
    must_have: Answer[int]
    preferred: Answer[int]
    record_families: Answer[str]
    record_years: Answer[int]
    # Each abstains twice: once when the posting says nothing, again when the
    # reader has expressed no preference.
    place_fit: Answer[str] = field(
        default_factory=lambda: unasked_answer("Where you could work this")
    )
    arrangement_fit: Answer[str] = field(
        default_factory=lambda: unasked_answer("Arrangement you want")
    )
    travel_fit: Answer[str] = field(
        default_factory=lambda: unasked_answer("Travel against your limit")
    )
    time_zone_fit: Answer[str] = field(
        default_factory=lambda: unasked_answer("Time zone you work in")
    )
    level_fit: Answer[str] = field(
        default_factory=lambda: unasked_answer("Level you are reading for")
    )
    residence_requirement: Answer[str] = field(
        default_factory=lambda: unasked_answer("Residence the posting requires")
    )
    pay_fit: Answer[str] = field(default_factory=lambda: unasked_answer("Pay against your band"))
    pay_standing: int | None = None
    pay_target_met: bool = False
    # The "<currency> <basis>" of the stretch the posting's pay floor is above, else empty.
    pay_above_stretch: str = ""
    pay_extractions: tuple[CompensationInput, ...] = field(default_factory=tuple)
    requirements: tuple[RequirementLine, ...] = field(default_factory=tuple)

    def named(self) -> tuple[tuple[str, Answer[Any]], ...]:
        """Return every answer with the field name it is stored under.

        Derived from the fields rather than listed again, so an answer added to
        this class reaches every reader of it without a second list.
        """
        return tuple(
            (item.name, value)
            for item in fields(self)
            if isinstance(value := getattr(self, item.name), Answer)
        )

    def answers(self) -> tuple[Answer[Any], ...]:
        """Return every question this reading answered or declined to answer."""
        return tuple(answer for _, answer in self.named())

    @property
    def abstentions(self) -> int:
        """Return how many questions the text did not support an answer to."""
        return sum(1 for answer in self.answers() if not answer.stated)


def unasked_answer(question: str) -> Answer[str]:
    """Return the answer for a question nobody configured this run to ask."""
    return Answer(question, note="no job-search parameters are set for this question")
