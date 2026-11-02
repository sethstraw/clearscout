"""The words, figures, and verbs the verification checks read in drafted text."""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence

from tailorcv.application.tailoring.stages.context import PARTICIPATION
from tailorcv.shared.text import sentence_pieces

# Match pronouns case-sensitively; sentence-initial forms are handled separately.
PRONOUN = re.compile(
    r"\b(?:I'm|I've|I'd|I'll|[Mm]e|[Mm]y|[Mm]ine|[Mm]yself|[Ww]e|[Uu]s|[Oo]ur|[Oo]urs)\b"
)
BARE_I = re.compile(r"\bI\b")
NUMBER = re.compile(r"\d[\d,.]*")
# A figure is one word, so it neither splits at its separators nor lets its neighbours join.
WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#.-]*|\d+(?:[.,]\d+)*")

_ONES = (
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)
_TENS = ("twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
NUMBER_WORDS = {word: value for value, word in enumerate(_ONES, start=1)} | {
    word: (index + 2) * 10 for index, word in enumerate(_TENS)
}
SCALE_WORDS = frozenset(
    {"hundred", "hundreds", "thousand", "thousands", "million", "millions", "billion", "billions"}
)
# Longest first, so "twenty-five" is read whole and "nineteen" is never read as "nine".
_COUNTS = sorted(
    [f"{ten}[- ]{one}" for ten in _TENS for one in _ONES[:9]] + list(NUMBER_WORDS),
    key=len,
    reverse=True,
)
# Only a count this closed set spells exactly. A singular "a decade" is left out: it is
# routinely written for eleven or twelve years, so no exact reading of it exists.
WORD_SPAN = re.compile(
    rf"\b({'|'.join(_COUNTS)})[\s-]+(years?|months?|weeks?|days?|decades)\b", re.IGNORECASE
)

# Words two wordings of one claim may differ in. Negations carry the claim, so "without",
# "not" and "never" are content words and are left out of this set.
FUNCTION_WORDS = frozenset(
    {
        "a",
        "an",
        "are",
        "as",
        "at",
        "be",
        "been",
        "by",
        "for",
        "from",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "over",
        "that",
        "the",
        "these",
        "this",
        "those",
        "to",
        "was",
        "were",
        "which",
        "who",
        "whose",
        "with",
    }
)

# A qualifier the candidate's own evidence states and the line drops changes who
# did what. A negation counts only within three words of a role it denies, so
# "without downtime" and "not only" limit nobody.
_DENIED_ROLE = (
    r"own(?:ed|er|ership|ing)?|led|lead(?:ing)?(?!\s+to\b)|manag(?:e|ed|er|ing|ement)"
    r"|direct(?:ed|ing|ly)|direct\s+reports|supervis(?:e|ed|ing)|responsib\w*|authority"
)
QUALIFIER = re.compile(
    rf"\b(?:not(?!\s+only\b)|without)\s+(?:[\w-]+\s+){{0,2}}?(?:{_DENIED_ROLE})\b"
    rf"|\bassisted\b|{PARTICIPATION.pattern}",
    re.IGNORECASE,
)

# Verbs that claim ownership of work; a line opening with one its evidence never uses
# may be promoting a contribution into ownership.
OWNERSHIP_VERBS = frozenset(
    {
        "architected",
        "built",
        "championed",
        "directed",
        "drove",
        "headed",
        "led",
        "managed",
        "oversaw",
        "owned",
        "spearheaded",
    }
)

# The verb a letter claims after its first-person subject, as in "I built" or "I have led".
FIRST_PERSON_VERB = re.compile(r"\bI(?:['\u2019]ve|\s+ha(?:ve|d))?\s+([A-Za-z]+)")

# "Licensed" is a credential beside a profession or a licence, not in "licensed products".
LICENSED_CREDENTIAL = re.compile(
    r"\blicensed\s+(?:to\s+practi[cs]e|(?:[\w-]+\s+){0,2}?(?:pharmacists?|physicians?|nurses?"
    r"|practitioners?|clinicians?|psychologists?|therapists?|counsell?ors?|social\s+workers?"
    r"|dietitians?|dentists?|veterinarians?|engineers?|architects?|surveyors?|attorneys?"
    r"|lawyers?|accountants?|pilots?|electricians?|professionals?|licen[cs]es?|licensure))\b",
    re.IGNORECASE,
)

# Finite past forms a CV writes for work done. Nominalizations and bare forms are left
# out, because a term of art such as "validation" or "claim submission" reads as one.
CLAIM_VERBS = OWNERSHIP_VERBS | frozenset(
    {
        "analyzed",
        "audited",
        "built",
        "consolidated",
        "converted",
        "corrected",
        "created",
        "cut",
        "delivered",
        "deployed",
        "designed",
        "developed",
        "diagnosed",
        "doubled",
        "eliminated",
        "established",
        "expanded",
        "fixed",
        "founded",
        "grew",
        "implemented",
        "improved",
        "increased",
        "introduced",
        "launched",
        "maintained",
        "mentored",
        "migrated",
        "modernized",
        "moved",
        "negotiated",
        "optimized",
        "published",
        "ran",
        "reached",
        "rebuilt",
        "reconciled",
        "recovered",
        "reduced",
        "refactored",
        "removed",
        "reorganized",
        "replaced",
        "resolved",
        "restored",
        "rewrote",
        "saved",
        "scaled",
        "shipped",
        "simplified",
        "solved",
        "standardized",
        "streamlined",
        "taught",
        "tracked",
        "trained",
        "tuned",
        "unified",
        "upgraded",
        "used",
        "validated",
        "verified",
        "wrote",
    }
)


def bare_word(word: str) -> str:
    """Return one ``WORD`` token stripped of the dots and hyphens ``C++`` needs kept."""
    return word.casefold().strip(".-")


def figures_in(text: str) -> list[tuple[str, frozenset[str]]]:
    """Return each figure as the words that wrote it and every value those words state.

    Both sides of the comparison are split into sentences, so a word that
    suppresses a span reaches exactly as far in a citation as in a drafted line.
    """
    found: list[tuple[str, frozenset[str]]] = []
    for sentence in sentence_pieces(text):
        found += [(raw, frozenset({_number(raw)})) for raw in NUMBER.findall(sentence)]
        found += _word_spans(sentence)
    return found


def _word_spans(text: str) -> Iterator[tuple[str, frozenset[str]]]:
    """Yield each duration spelled in words in ``text``, with every value it states."""
    for match in WORD_SPAN.finditer(text):
        if not _part_of_a_larger_number(_prefix_words(text[: match.start()])):
            yield match.group(0), _span_readings(match.group(1), match.group(2))


def _prefix_words(prefix: str) -> list[str]:
    """Return the words before a span, split at the hyphens ``WORD`` keeps inside one token."""
    return [
        part for word in WORD.findall(prefix) for part in re.split(r"[- ]", bare_word(word)) if part
    ]


def _part_of_a_larger_number(before: Sequence[str]) -> bool:
    """Return whether the words before a count make it one part of a larger number.

    "one hundred and twenty days" would otherwise be read as twenty.
    """
    previous = before[-1] if before else ""
    if previous in SCALE_WORDS or previous in NUMBER_WORDS:
        return True
    return previous == "and" and len(before) > 1 and before[-2] in SCALE_WORDS


def _span_readings(count: str, unit: str) -> frozenset[str]:
    """Return every value a spelled span states, with a compound's value and decades as years."""
    value = sum(NUMBER_WORDS[part] for part in re.split(r"[- ]", count.casefold()))
    if unit.casefold() == "decades":
        return frozenset({str(value), str(value * 10)})
    return frozenset({str(value)})


def _number(raw: str) -> str:
    """Normalize a numeric token so 28.8 and 28.8 match but 288 does not."""
    return raw.strip(".,").replace(",", "")
