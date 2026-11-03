"""Turn job description text into a structured, checkable specification.

Two things matter here and neither is the prose. The exact keyword phrases,
because coverage is scored deterministically later and a paraphrase cannot be
scored; and the hard gates, which decide whether applying is worth an afternoon
and so lead the report.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from clearscout.application.config import Stage
from clearscout.application.prompts import flatten, render
from clearscout.application.tailoring.stages.spans import UNPLACED, Span, locate, normalize
from clearscout.domain.requirements import GateKind, is_boilerplate_requirement
from clearscout.infrastructure.providers.client import Client
from clearscout.infrastructure.providers.plan import RequestPlan, Segment
from clearscout.infrastructure.web_fetch.ingest import JDText
from clearscout.shared.errors import NoRequirementsError


class Importance(StrEnum):
    """How much weight a requirement carries in selection."""

    MUST_HAVE = "must_have"
    NICE_TO_HAVE = "nice_to_have"
    RESPONSIBILITY = "responsibility"


WEIGHTS = {
    Importance.MUST_HAVE: 3.0,
    Importance.NICE_TO_HAVE: 1.5,
    Importance.RESPONSIBILITY: 1.0,
}


class Dimension(StrEnum):
    """Whether a requirement is a hard skill or a behavioral disposition.

    A hard requirement is answered by concrete evidence: a technology, a
    credential, a scale, a minimum year count. A behavioral requirement is
    answered by a described action pattern that would earn the trait a
    behavioral-anchor reviewer looks for.
    """

    HARD = "hard"
    BEHAVIORAL = "behavioral"


# The docstring below is provider-sent, so it stays as written. Its "never acted
# on automatically" holds but for one grounded case: the evaluation workflow
# refuses a verified blocking gate matching a candidate-declared excluded condition.
class RawHardGate(BaseModel):
    """A stated condition that a candidate either meets or does not.

    ``blocking`` is the model's reading of whether the posting treats the gate
    as absolute. It is advisory to a human, never acted on automatically; the
    tool does not decide not to apply on someone's behalf.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: GateKind
    text: str
    blocking: bool = False
    quote_or_span: str = ""
    parent_group: str = ""


class HardGate(BaseModel):
    """One stated condition, carrying whether the posting was found to state it."""

    model_config = ConfigDict(frozen=True)

    kind: GateKind
    text: str
    blocking: bool = False
    quote_or_span: str = ""
    parent_group: str = ""
    source_span: Span = UNPLACED

    @property
    def verified(self) -> bool:
        """Return whether this gate's quote was located in the posting body."""
        return self.source_span.verified

    @property
    def verified_blocking(self) -> bool:
        """Return whether the posting was found to state this gate as absolute."""
        return self.blocking and self.verified


class RawCoreOccupation(BaseModel):
    """The occupation the posting primarily is, as the extraction model named it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    quote_or_span: str = ""


class CoreOccupation(BaseModel):
    """The posting's core occupation, carrying whether the posting states it."""

    model_config = ConfigDict(frozen=True)

    text: str
    quote_or_span: str = ""
    source_span: Span = UNPLACED

    @property
    def verified(self) -> bool:
        """Return whether this label's quote was located in the posting body."""
        return self.source_span.verified


class RawBoundaryMatch(BaseModel):
    """The numbered stated boundary the posting's core occupation falls under."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    quote_or_span: str
    boundary: int


class RawRequirement(BaseModel):
    """One requirement as the extraction model reported it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    quote_or_span: str = ""
    importance: Importance
    dimension: Dimension = Dimension.HARD
    parent_group: str = ""


class Requirement(BaseModel):
    """One requirement with the stable identifier the rest of the run uses.

    Identifiers are assigned in Python rather than asked for, because a model
    asked to invent them will eventually reuse or skip one.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    text: str
    importance: Importance
    dimension: Dimension = Dimension.HARD
    quote_or_span: str = ""
    parent_group: str = ""
    source_span: Span = UNPLACED

    @property
    def weight(self) -> float:
        """Return the selection weight this requirement's importance implies."""
        return WEIGHTS[self.importance]

    @property
    def verified(self) -> bool:
        """Return whether this requirement's quote was located in the posting body."""
        return self.source_span.verified


class JobSpecExtraction(BaseModel):
    """The schema the extraction model fills in."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    company: str | None = None
    seniority: str | None = None
    location: str | None = None
    requirements: list[RawRequirement] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    hard_gates: list[RawHardGate] = Field(default_factory=list)
    core_occupation: RawCoreOccupation | None = None
    boundary_match: RawBoundaryMatch | None = None


class JobSpec(BaseModel):
    """The finished specification, with identified requirements."""

    model_config = ConfigDict(frozen=True)

    title: str
    company: str | None
    seniority: str | None
    location: str | None
    requirements: tuple[Requirement, ...]
    keywords: tuple[str, ...]
    hard_gates: tuple[HardGate, ...]
    origin: str
    core_occupation: CoreOccupation | None = None
    # Copied from the search's own boundary text; the model supplies only its position.
    excluded_boundary: str | None = None
    excluded_boundary_quote: str = ""

    def requirement(self, requirement_id: str) -> Requirement | None:
        """Return one requirement by identifier."""
        return next((item for item in self.requirements if item.id == requirement_id), None)

    def slug(self) -> str:
        """Return a filesystem-safe stem for this posting's run directory."""
        parts = [part for part in (self.company, self.title) if part]
        text = "_".join(parts) or "posting"
        return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")[:80] or "posting"

    def named(self, stored: StoredPostingName | None) -> JobSpec:
        """Return this spec carrying the stored title and employer in place of extracted ones."""
        if stored is None:
            return self
        employer = stored.stated_employer
        return self.model_copy(
            update={
                "title": sanitize(stored.title, MAX_TITLE_LENGTH),
                "company": None if employer is None else sanitize(employer, MAX_TITLE_LENGTH),
            }
        )


@dataclass(frozen=True)
class StoredPostingName:
    """The title and employer name a workspace stores for one posting."""

    title: str
    employer: str
    # Set when the employer is only a title-cased board-address tenant the posting never spells.
    employer_from_address_only: bool = False

    @property
    def stated_employer(self) -> str | None:
        """Return the employer a package may name, or None when only the address names it."""
        return None if self.employer_from_address_only else self.employer


# The longest keyword phrase kept; the extraction prompt states the same limit.
MAX_KEYWORD_LENGTH = 60

MAX_TITLE_LENGTH = 120


def sanitize(value: str, limit: int | None = None) -> str:
    """Flatten posting-derived text to one line, cut to ``limit`` when one is given."""
    flat = flatten(value)
    return flat[:limit] if limit is not None else flat


def sanitize_keywords(keywords: list[str]) -> tuple[str, ...]:
    """Clean posting-derived phrases before they reach a later prompt.

    Bounding and stripping control characters closes the channel by which
    posting text could carry something shaped like an instruction into a later
    call. The title and the employer go through :func:`sanitize` instead.
    """
    cleaned: list[str] = []
    for keyword in keywords:
        flat = sanitize(keyword)
        if flat and len(flat) <= MAX_KEYWORD_LENGTH and flat not in cleaned:
            cleaned.append(flat)
    return tuple(cleaned)


def stated_keywords(keywords: tuple[str, ...], source: str) -> tuple[str, ...]:
    """Drop the keywords the posting does not contain, compared as source spans are."""
    folded = normalize(source)
    return tuple(keyword for keyword in keywords if normalize(keyword) in folded)


def system_prompt() -> str:
    """Return the extraction instructions, rendering the ``MAX_KEYWORD_LENGTH`` code enforces."""
    return render("jobspec_system", keyword_limit=str(MAX_KEYWORD_LENGTH))


def numbered_boundaries(boundaries: tuple[str, ...]) -> str:
    """Return the stated boundaries numbered from 1, as the extraction prompt shows them."""
    return "\n".join(f"{index}. {flatten(text)}" for index, text in enumerate(boundaries, 1))


def extract(
    client: Client,
    jd: JDText,
    *,
    disputed_rulings: str = "",
    boundaries: tuple[str, ...] = (),
) -> JobSpec:
    """Extract a JobSpec from job description text with one extraction-tier call.

    ``disputed_rulings`` is the rendered dispute block; empty means no
    active disputes and the invariant preamble reads as a no-op.
    """
    policy = client.settings.policy_for(Stage.JOBSPEC)
    plan = RequestPlan(
        stage=Stage.JOBSPEC,
        instructions=Segment(system_prompt(), "jobspec_system"),
        task=Segment(
            render(
                "jobspec_user",
                jd_text=jd.text,
                stated_boundaries=numbered_boundaries(boundaries),
                disputed_rulings=disputed_rulings,
            ),
            "jobspec_user",
        ),
        max_tokens=policy.max_tokens,
    )
    return spec_from_extraction(client.complete(plan, JobSpecExtraction), jd, boundaries)


def spec_from_extraction(
    raw: JobSpecExtraction, jd: JDText, boundaries: tuple[str, ...] = ()
) -> JobSpec:
    """Return the spec one extraction answer yields for ``jd``, recorded or fresh."""
    kept_requirements = [
        item for item in raw.requirements if not is_boilerplate_requirement(item.text)
    ]
    kept_gates = [item for item in raw.hard_gates if not is_boilerplate_requirement(item.text)]
    requirements = tuple(
        _requirement(index, item, jd.text) for index, item in enumerate(kept_requirements, 1)
    )
    if not requirements:
        # Reject empty requirements so downstream support checks cannot pass vacuously.
        raise NoRequirementsError(
            f"No requirements could be read out of {jd.summary()}.\n"
            f"  It was understood as: {raw.title!r}\n"
            "  Every real posting states some, so the text that arrived is almost\n"
            "  certainly not the posting: a board index, a login wall, an expired\n"
            "  listing, or a page that builds itself with JavaScript.\n"
            "  Open the posting, save it as a PDF or screenshot it, and pass that path."
        )
    title = sanitize(raw.title, MAX_TITLE_LENGTH)
    occupation = _core_occupation(raw.core_occupation, jd.text)
    excluded = _excluded_boundary(raw.boundary_match, boundaries, jd.text, occupation, title)
    return JobSpec(
        title=title,
        company=sanitize(raw.company, MAX_TITLE_LENGTH) if raw.company else None,
        seniority=raw.seniority,
        location=raw.location,
        requirements=requirements,
        keywords=stated_keywords(sanitize_keywords(raw.keywords), jd.text),
        hard_gates=tuple(_gate(gate, jd.text) for gate in kept_gates),
        origin=jd.origin,
        core_occupation=occupation,
        excluded_boundary=None if excluded is None else excluded[0],
        excluded_boundary_quote="" if excluded is None else excluded[1],
    )


# A response schema is part of the provider's cached prefix, so every posting up to this
# many requirements gets one schema; a longer posting grows it in steps of the same size.
VOCABULARY_SIZE = 128


def _requirement_id(index: int) -> str:
    """Return the identifier this run gives the requirement at ``index``."""
    return f"r{index:02d}"


def requirement_vocabulary(count: int) -> tuple[str, ...]:
    """Return the identifiers a response schema may offer for ``count`` requirements."""
    size = max(VOCABULARY_SIZE, math.ceil(count / VOCABULARY_SIZE) * VOCABULARY_SIZE)
    return tuple(_requirement_id(index) for index in range(1, size + 1))


# Proficiency words that grade a requirement more strictly when an extraction drops them.
_DEGREE_WORDS = (
    r"deep|strong|solid|proven|demonstrated|extensive|significant|excellent"
    r"|advanced|expert|thorough|in-depth|hands-on"
)
_DEGREE_WORD = re.compile(rf"(?<![\w-])({_DEGREE_WORDS})[ \t]+$", re.IGNORECASE)
_QUOTED_DEGREE_WORD = re.compile(rf"\s*({_DEGREE_WORDS})[ \t]+", re.IGNORECASE)
_FIRST_WORD = re.compile(r"\s*([\w'-]+)")


def restore_leading_qualifier(text: str, quote: str, source: str) -> tuple[str, str]:
    """Return ``text`` and ``quote`` with the degree word the body puts right before the text.

    The word is the one the located quote opens with, or else the one the body line puts
    right before the quote; the text takes it only when its own first word follows it.
    """
    span = locate(quote, source)
    found = _leading_degree(quote, source, span.start) if span.verified else None
    text_first = _FIRST_WORD.match(text)
    if found is None or text_first is None:
        return text, quote
    word, follower, restored_quote = found
    if text_first.group(1).casefold() != follower.casefold():
        return text, quote
    lead = word[0].upper() if text_first.group(1)[0].isupper() else word[0].lower()
    return f"{lead}{word[1:]} {follower}{text[text_first.end() :]}", restored_quote


def _leading_degree(quote: str, source: str, start: int) -> tuple[str, str, str] | None:
    """Return a placed quote's leading degree word, the word it grades, and the quote with it."""
    if quoted := _QUOTED_DEGREE_WORD.match(quote):
        follower = _FIRST_WORD.match(quote, quoted.end())
        return (quoted.group(1), follower.group(1), quote) if follower else None
    degree = _DEGREE_WORD.search(source[:start].rsplit("\n", 1)[-1])
    placed = _FIRST_WORD.match(source, start)
    quote_first = _FIRST_WORD.match(quote)
    if degree is None or placed is None or quote_first is None:
        return None
    if quote_first.group(1).casefold() != placed.group(1).casefold():
        return None
    return degree.group(1), placed.group(1), f"{degree.group(1)} {quote.lstrip()}"


def _requirement(index: int, raw: RawRequirement, source: str) -> Requirement:
    """Identify one extracted requirement and place its quote in the posting."""
    text, quote = restore_leading_qualifier(raw.text, raw.quote_or_span or raw.text, source)
    return Requirement(
        id=_requirement_id(index),
        text=text,
        importance=raw.importance,
        dimension=raw.dimension,
        quote_or_span=quote,
        parent_group=raw.parent_group or "requirements",
        source_span=locate(quote, source),
    )


def _gate(raw: RawHardGate, source: str) -> HardGate:
    """Place one extracted gate's quote in the posting that is said to state it."""
    quote = raw.quote_or_span or raw.text
    return HardGate(
        kind=raw.kind,
        text=raw.text,
        blocking=raw.blocking,
        quote_or_span=quote,
        parent_group=raw.parent_group or "hard_gates",
        source_span=locate(quote, source),
    )


def _core_occupation(raw: RawCoreOccupation | None, source: str) -> CoreOccupation | None:
    """Place the core-occupation label's quote in the posting that states it."""
    if raw is None or not raw.text.strip():
        return None
    quote = raw.quote_or_span or raw.text
    return CoreOccupation(text=raw.text, quote_or_span=quote, source_span=locate(quote, source))


def _excluded_boundary(
    raw: RawBoundaryMatch | None,
    boundaries: tuple[str, ...],
    source: str,
    occupation: CoreOccupation | None,
    title: str,
) -> tuple[str, str] | None:
    """Return the boundary the model placed the posting under and the posting text citing it.

    The quote must sit on the verified core-occupation span or inside the title, so a
    word found in a preferred-skills line cannot rule a posting out.
    """
    if raw is None or not 1 <= raw.boundary <= len(boundaries):
        return None
    if occupation is None or not occupation.verified or not raw.quote_or_span.strip():
        return None
    span = locate(raw.quote_or_span, source)
    if not span.verified:
        return None
    held = occupation.source_span
    on_occupation = span.start < held.end and held.start < span.end
    if not on_occupation and normalize(raw.quote_or_span) not in normalize(title):
        return None
    return boundaries[raw.boundary - 1], source[span.start : span.end]
