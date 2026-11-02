"""Closed vocabularies and lexical readings for person-supplied discovery."""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import TYPE_CHECKING

from tailorcv.application.tailoring.changes import MIN_NEW_WORD, STOPWORDS
from tailorcv.application.tailoring.fit import Answer
from tailorcv.application.tailoring.stages.verify import NUMBER, WORD, bare_word
from tailorcv.domain.taxonomy import normalize, says

if TYPE_CHECKING:
    from .conversation import Reply

JOURNAL = "Discovery_Log.md"

# Use three probes to balance coverage and user burden.
MAX_PROBES = 3

# Answers below this threshold are incomplete.
SUBSTANTIVE_WORDS = 4

# Bound the preview around each proposed insertion.
CONTEXT_LINES = 3


class Source(StrEnum):
    """Where a piece of text came from. Exactly one of these may become a claim.

    The three that may not are named rather than merely excluded, so a refusal
    can say which one it caught.
    """

    PERSON = "the person answering"
    DRAFT = "generated resume text"
    MODEL = "a model suggestion"
    POSTING = "the job description"


class Ownership(StrEnum):
    """One rung of the ladder a claim about doing something can sit on."""

    EXPOSURE = "exposure"
    USE = "use"
    SUPPORT = "support"
    EVALUATION = "evaluation"
    CONFIGURATION = "configuration"
    IMPLEMENTATION = "implementation"
    OWNERSHIP = "ownership"
    LEADERSHIP = "leadership"
    DEPLOYMENT = "deployment"


# Order rungs from lowest to highest; ranks only prevent upward promotion.
LADDER: tuple[tuple[Ownership, tuple[str, ...]], ...] = (
    (
        Ownership.EXPOSURE,
        (
            "sat in on",
            "sat next to",
            "shadowed",
            "watched",
            "was shown",
            "saw a demo",
            "read about",
            "aware of",
            "exposed to",
            "familiar with",
            "observed",
        ),
    ),
    (
        Ownership.USE,
        ("i used", "used it", "used", "worked with", "wrote queries", "day to day", "consumed"),
    ),
    (
        Ownership.SUPPORT,
        (
            "supported",
            "on call",
            "helped",
            "assisted",
            "troubleshot",
            "answered tickets",
            "kept it running",
            "second line",
        ),
    ),
    (
        Ownership.EVALUATION,
        (
            "evaluated",
            "trialled",
            "trialed",
            "proof of concept",
            "spike",
            "benchmarked",
            "compared",
            "assessed",
            "piloted",
        ),
    ),
    (
        Ownership.CONFIGURATION,
        ("configured", "set up", "tuned", "administered", "provisioned", "wrote the config"),
    ),
    (
        Ownership.IMPLEMENTATION,
        ("built", "implemented", "wrote", "developed", "migrated", "integrated", "rebuilt"),
    ),
    (
        Ownership.OWNERSHIP,
        ("owned", "responsible for", "accountable for", "maintained", "on the hook for"),
    ),
    (
        Ownership.LEADERSHIP,
        ("led", "mentored", "managed", "directed", "coordinated", "ran the team", "supervised"),
    ),
    (
        Ownership.DEPLOYMENT,
        ("in production", "deployed", "shipped", "went live", "rolled out", "serving users"),
    ),
)

RANK = {level: index for index, (level, _) in enumerate(LADDER)}

# These answers close the question without a follow-up.
REFUSALS = (
    "no",
    "nope",
    "never",
    "not at all",
    "no experience",
    "none",
    "nothing",
    "havent",
    "have not",
    "haven t",
    "i have not",
    "i havent",
    "not really",
    "no i have not",
)

# These words carry no independent evidence.
_FUNCTION_TEXT = (
    "a an and as at be but by can did do for from had has have i if in is it its me my no not of "
    "on or our so that the them then they this to too us was we were what when who will with you "
    "your"
)
FUNCTION_WORDS = STOPWORDS | frozenset(_FUNCTION_TEXT.split())


class Angle(StrEnum):
    """What a question is asking about. Five of them, broad ones first.

    The order is the behaviour: the widest question that could land is asked
    first, and the narrow ones only once something has.
    """

    DIRECT = "direct experience"
    SUPPORTING = "indirect or supporting work"
    ADJACENT = "adjacent tools and domains"
    LEADERSHIP = "leadership and stakeholders"
    SCALE = "scale, production status, and outcomes"


# One broad angle per probe in the MAX_PROBES budget, asked until evidence is found.
BROAD = (Angle.DIRECT, Angle.SUPPORTING, Angle.ADJACENT)
# Ask follow-ups only for uncovered details.
NARROW = (Angle.LEADERSHIP, Angle.SCALE)

QUESTIONS: dict[Angle, str] = {
    Angle.DIRECT: ("Have you worked on {subject} yourself? Say what you did, in your own words."),
    Angle.SUPPORTING: (
        "Did you support or contribute to work on {subject} that somebody else led?"
    ),
    Angle.ADJACENT: (
        "Have you used anything close to {subject}? "
        "Name the closest thing and what you did with it."
    ),
    Angle.LEADERSHIP: (
        "On that {subject} work, did you lead anyone or answer to anyone outside your own team?"
    ),
    Angle.SCALE: (
        "On that {subject} work: how large, how often, and was it in production? "
        "Only what you can state as fact."
    ),
}


class Point(StrEnum):
    """One of the eight things a person confirms before material is evidence.

    Eight separate acknowledgements rather than one, because "yes" to a printed
    block is a decision about the block's first line.
    """

    WORDING = "the exact factual wording"
    METRICS = "the metrics, including that there are none"
    OWNERSHIP = "the ownership level"
    SCOPE = "the scope"
    CATEGORY = "the evidence category"
    GAPS = "the job gaps it relates to"
    PROVENANCE = "that its source is user-confirmed discovery"
    AMBIGUITY = "what remains ambiguous"


REQUIRED = frozenset(Point)


def reads_as(text: str) -> Answer[Ownership]:
    """Return the highest rung of the ladder ``text`` evidences, or nothing.

    An :class:`Answer` rather than a value, because a reading that always
    produced a level would produce one for "yes". It is lexical: it reads the
    highest rung any phrase uses, so an answer saying somebody else owned the
    thing reads as ownership, which is why the level is confirmed explicitly.
    """
    found = [(RANK[level], level, phrases) for level, phrases in LADDER if says(text, phrases)]
    if not found:
        return Answer(
            "Ownership level",
            note="nothing in the wording says what the person did, only that something happened",
        )
    _, level, phrases = max(found)
    return Answer("Ownership level", level, evidence=says(text, phrases))


def figures(text: str) -> tuple[str, ...]:
    """Return the numeric tokens in ``text``, normalized, in reading order."""
    return tuple(dict.fromkeys(raw.strip(".,").replace(",", "") for raw in NUMBER.findall(text)))


def unsourced_words(claim: str, replies: Sequence[Reply]) -> tuple[str, ...]:
    """Return the claim's significant words that appear in no answer.

    The second barrier around fabrication, and the one that does not depend on
    anybody labeling anything honestly. Short words and function words are
    exempt: they carry no evidence, and refusing a claim for containing "with"
    would make the rule unusable.
    """
    known = {bare_word(word) for reply in replies for word in WORD.findall(reply.text)}
    missing: list[str] = []
    for word in WORD.findall(claim):
        folded = bare_word(word)
        if len(folded) < MIN_NEW_WORD or folded in FUNCTION_WORDS or folded in known:
            continue
        if folded not in missing:
            missing.append(folded)
    return tuple(missing)


def _is_refusal(text: str) -> bool:
    """Return whether an answer is a complete "no".

    Narrow: reading a long, hedged answer as a refusal would throw away the
    thing the person was trying to say.
    """
    stripped = normalize(text).strip()
    if not stripped:
        return True
    return any(stripped == normalize(phrase).strip() for phrase in REFUSALS)
