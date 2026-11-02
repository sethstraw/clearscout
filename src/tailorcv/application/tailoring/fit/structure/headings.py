"""Heading shapes and the section kind a heading names."""

from __future__ import annotations

import re
import string

from tailorcv.application.tailoring.fit.lexicon import (
    BARE_HEADING_WORDS,
    BOILERPLATE_HEADINGS,
    BULLET,
    CANDIDATE_HEADINGS,
    HEADING_WORDS,
    MARKDOWN_HEADING,
    MENTION_PREFERENCE_MARKERS,
    MUST_MARKERS,
    OPTIONAL_HEADING,
    PAY_CONTEXT,
    PREFERRED_HEADINGS,
    REQUIREMENT_HEADINGS,
    RESPONSIBILITY_HEADINGS,
    TERMINAL,
    preference_cues,
)
from tailorcv.application.tailoring.fit.structure.reading import SectionKind
from tailorcv.domain.taxonomy import says
from tailorcv.shared.layout import RUNS_ON, unbalanced_brackets

# A line trailing off into the list under it leads it in, as a colon does.
ELLIPSIS = ("...", "…")


# A line that is only "or" joins the items either side of it and heads nothing.
BARE_OR = re.compile(r"^\W*or\W*$", re.IGNORECASE)


# A closing aside that is one stance and nothing else: "... we'd prioritize (nice to have)".
_CLOSING_ASIDE = re.compile(r"\((?P<aside>[^()]+)\)[:.]?$")


# A line closing on its stance heads the list under it at up to twice a bare heading's length.
_ASIDE_HEADING_WORDS = 2 * BARE_HEADING_WORDS


# Such a line speaks to the reader or for the employer, as "skills we'd prioritize" does; an
# item names a qualification, and qualification words open headings too, so they decide nothing.
_ADDRESSED = re.compile(r"\b(?:we|you)(?:['\u2019](?:d|ll|re|ve))?\b", re.IGNORECASE)


def _aside_stance(line: str) -> SectionKind | None:
    """Return the section kind a line's closing stance aside names, or nothing."""
    if (found := _CLOSING_ASIDE.search(line)) is None:
        return None
    aside = " ".join(found["aside"].casefold().split())
    if aside in PREFERRED_HEADINGS or OPTIONAL_HEADING.fullmatch(aside):
        return SectionKind.PREFERRED
    return SectionKind.REQUIREMENT if aside in MUST_MARKERS else None


def heads_by_stance(line: str, following: str) -> bool:
    """Return whether a line too long for a bare heading heads the list under it by its stance."""
    words = len(line.split())
    return (
        BARE_HEADING_WORDS < words <= _ASIDE_HEADING_WORDS
        and _aside_stance(line) is not None
        and _ADDRESSED.search(line) is not None
        and bool(BULLET.match(following))
    )


def heading_of(line: str, following: str) -> str | None:
    """Return the heading text of ``line``, or nothing when it is not a heading.

    Four shapes, because postings use them all: a Markdown heading, a short line
    ending in a colon (or an ellipsis over a list), a short unpunctuated line or
    question standing over a list, and a short line that is a section's name and
    little else. A bullet is never a heading, nor is half of a bracketed phrase
    wrapped across two lines.
    """
    if BULLET.match(line) or BARE_OR.match(line) or unbalanced_brackets(line):
        return None
    if heading := MARKDOWN_HEADING.match(line):
        return heading.group("text").strip().rstrip(":")
    words = len(line.split())
    if line.endswith(":") and words <= HEADING_WORDS:
        return line.rstrip(":").strip()
    if line.endswith(ELLIPSIS) and words <= HEADING_WORDS and BULLET.match(following):
        return line.rstrip(".…").strip()
    if heads_by_stance(line, following):
        return line.rstrip(":.").strip()
    bare = line.endswith("?") or not line.endswith(TERMINAL)
    if words <= BARE_HEADING_WORDS and bare and BULLET.match(following):
        return line
    if names_a_section(line):
        return line
    return None


SECTION_FRAGMENTS = (
    # Boilerplate fragments are specific and requirement fragments are generic, so
    # boilerplate is matched first; "Physical Requirements" holds no requirement.
    (BOILERPLATE_HEADINGS, SectionKind.BOILERPLATE),
    (PREFERRED_HEADINGS, SectionKind.PREFERRED),
    (REQUIREMENT_HEADINGS, SectionKind.REQUIREMENT),
    (RESPONSIBILITY_HEADINGS, SectionKind.RESPONSIBILITY),
)


# Words a section's name carries beside its fragment: "Minimum" or "Key", not a clause.
_NAME_WORDS_BEFORE = 2


NAME_WORDS_AFTER = 3


NAME_WORD = re.compile(r"[a-z0-9']+")


# A gerund or a process noun: "gathering", "engineering", "synthesis", "analysis".
_ACTIVITY = re.compile(r"[a-z]+(?:ing|sis)")


_EVERY_FRAGMENT = tuple(fragment for fragments, _ in SECTION_FRAGMENTS for fragment in fragments)


# "What you need to know" or "What you need from us" tells the reader about the process or
# the employer; the lines under it still read on their own stated terms.
_ASKS_WHAT_TO_KNOW = re.compile(
    r"\bwhat\s+(?:do\s+)?you(?:['\u2019]ll|\s+will)?\s+need\s+(?:to\s+know|from\s+us)\b"
)


def kind_of(heading: str) -> SectionKind:
    """Return what a heading makes the block under it.

    Order matters: "Preferred qualifications" is a preferred block and contains
    the word every requirement block contains.
    """
    if OPTIONAL_HEADING.fullmatch(heading):
        return SectionKind.PREFERRED
    folded = _ASKS_WHAT_TO_KNOW.sub(" ", heading.casefold())
    named = next(
        (
            kind
            for fragments, kind in SECTION_FRAGMENTS
            if any(
                fragment in folded and _names_the_candidate(folded, fragment)
                for fragment in fragments
            )
        ),
        SectionKind.OTHER,
    )
    # A closing stance aside outranks the words before it: "Skills and experience (optional)".
    if named in (*ASKING_KINDS, SectionKind.OTHER) and (stated := _aside_stance(heading)):
        return stated
    return named


def _names_the_candidate(folded: str, fragment: str) -> bool:
    """Return whether a heading using a candidate-only fragment is about the candidate.

    "Our key strengths" describes the company and "What we look for in a vendor" a vendor.
    """
    if fragment not in CANDIDATE_HEADINGS:
        return True
    return (
        re.fullmatch(
            rf"\W*(?:your\s+)?{re.escape(fragment)}"
            r"(?:\s+in\s+(?:you|a\s+candidate|candidates|the\s+candidate))?\W*",
            folded,
        )
        is not None
    )


def names_a_section(line: str) -> bool:
    """Return whether a bare line is a section's name from the heading vocabulary.

    "Minimum Qualifications" names one, as does "Northwind Benefits & Perks", whose
    second name is no clause; "Degree preferred" states a stance on its subject, a
    figure or a pay word marks a pay line, and a longer line is prose.
    """
    if (
        not line[:1].isupper()
        or len(line.split()) > BARE_HEADING_WORDS
        or line.endswith(TERMINAL)
        or BULLET.match(line)
        or any(character.isdigit() or character == "$" for character in line)
    ):
        return False
    folded = line.casefold()
    if (section := section_word(folded)) is None:
        return False
    found, fragment, kind = section
    before = NAME_WORD.findall(folded[: found.start()])
    after = name_tail(folded[found.end() :], kind)
    # "Degree required" states a stance on a subject, and "Required to travel" or
    # "Would be great at planning" asks for something in a phrase.
    opening = NAME_WORD.findall(folded[found.end() :])[:1]
    phrase = bool(opening) and opening[0] in RUNS_ON and opening[0] != "and"
    stance = (
        says(fragment, MUST_MARKERS) or bool(preference_cues(fragment, MENTION_PREFERENCE_MARKERS))
    ) and (bool(before) or phrase)
    paid = kind is not SectionKind.BOILERPLATE and PAY_CONTEXT.search(line)
    return not (
        after is None
        or stance
        or paid
        or len(before) > _NAME_WORDS_BEFORE
        or after > (NAME_WORDS_AFTER if not before else 0)
    )


def section_word(folded: str) -> tuple[re.Match[str], str, SectionKind] | None:
    """Return the first heading-vocabulary word in a folded line, its fragment, and its kind."""
    for fragments, kind in SECTION_FRAGMENTS:
        for fragment in fragments:
            found = re.search(rf"\b{re.escape(fragment)}[\w']*", folded)
            if found and _names_the_candidate(folded, fragment):
                return found, fragment, kind
    return None


def name_tail(tail: str, kind: SectionKind) -> int | None:
    """Return how many words follow a section's fragment, or nothing when they open a phrase."""
    words = NAME_WORD.findall(tail)
    if kind is SectionKind.REQUIREMENT and words and _ACTIVITY.fullmatch(words[0]):
        return None
    return sum(1 for word in words if word != "and" and not word.startswith(_EVERY_FRAGMENT))


def presented_heading(heading: str) -> str:
    """Return a heading as it is reported, with one set in capitals folded to title case."""
    if heading.isupper():
        return string.capwords(heading.casefold())
    return heading


ASKING_KINDS = (SectionKind.REQUIREMENT, SectionKind.PREFERRED)
