"""The lines a posting asks for something in, and how firmly."""

from __future__ import annotations

import re
from dataclasses import replace

from tailorcv.application.tailoring.fit.lexicon import (
    BULLET,
    CLEARANCE,
    CUE_FILLER,
    DEGREES,
    HELD_CREDENTIALS,
    MUST_MARKERS,
    PAY_CONTEXT,
    YEARS,
    clauses,
    offers_substitute,
    preference_cues,
)
from tailorcv.application.tailoring.fit.structure.boilerplate import (
    is_boilerplate,
    names_a_qualification,
    states_pay,
)
from tailorcv.application.tailoring.fit.structure.headings import BARE_OR
from tailorcv.application.tailoring.fit.structure.quotes import quote_line
from tailorcv.application.tailoring.fit.structure.reading import (
    Answer,
    Block,
    RequirementLine,
    SectionKind,
    Stance,
)
from tailorcv.application.tailoring.fit.structure.waivers import (
    blanked,
    preference_asides,
    unwaived,
)
from tailorcv.domain.requirements import WAIVED
from tailorcv.domain.taxonomy import SEPARATOR, says
from tailorcv.shared.text import sentence_pieces


def requirement_lines(blocks: tuple[Block, ...]) -> tuple[RequirementLine, ...]:
    """Return every line the posting asks for something in, and how it asks.

    A line inherits its block's stance and can override it: "SQL preferred" under
    "Requirements" is preferred, because the line is more specific than the
    heading. Outside a requirement block only a line stating its own stance
    counts. Where a block is a list, only its bullets are requirements. A bare
    "or" joins two requirements only where they sit either side of it in one
    block and share a stance.
    """
    found: list[RequirementLine] = []
    for block in blocks:
        # Indexes into ``found`` of the requirement just read, and of one a bare "or" follows.
        last: int | None = None
        linked: int | None = None
        for text in _judged_lines(block):
            if BARE_OR.match(text):
                linked, last = last, None
                continue
            line = None if is_boilerplate(text) or states_pay(text) else _stance_of(text, block)
            if line is None:
                linked = last = None
                continue
            if linked is not None and found[linked].stance is line.stance:
                found[linked] = replace(found[linked], or_next=True)
            found.append(line)
            linked, last = None, len(found) - 1
    return tuple(found)


def _list_items(lines: tuple[str, ...]) -> tuple[str, ...]:
    """Return a list's bullets, keeping a bare "or" between items and the rung right after one.

    A ladder may continue past its bullets in paragraphs a bare "or" line opens.
    """
    if not any(BULLET.match(line) for line in lines):
        return ()
    return tuple(
        match.group("text").strip() if (match := BULLET.match(line)) else line.strip()
        for position, line in enumerate(lines)
        if BULLET.match(line)
        or BARE_OR.match(line)
        or (position and BARE_OR.match(lines[position - 1]))
    )


def _judged_lines(block: Block) -> tuple[str, ...]:
    """Return the lines of a block judged for a stance: its bullets, or else its candidates."""
    if block.kind is SectionKind.BOILERPLATE:
        return ()
    bullets = _list_items(block.lines)
    # A bulleted duty list states duties, even where a duty uses a stance word;
    # a degree or an experience floor listed among them is still asked for.
    if block.kind is SectionKind.RESPONSIBILITY and bullets:
        return tuple(text for text in bullets if _names_a_credential(text))
    return bullets or _candidate_lines(block)


def _candidate_lines(block: Block) -> tuple[str, ...]:
    """Return a block's requirement candidates, splitting prose outside a requirement block.

    A prose scrape carries its requirements inside paragraphs under no heading or
    under a narrative one, so each sentence is judged on its own; a requirement or
    preferred block keeps its lines whole. Elsewhere a sentence must also read as a
    qualification, because a bare stance marker there is as likely to be pay,
    mission, or legal prose as a requirement.
    """
    if block.kind in (SectionKind.REQUIREMENT, SectionKind.PREFERRED):
        return block.lines
    return tuple(
        stripped
        for line in block.lines
        for sentence in sentence_pieces(line)
        if (stripped := sentence.strip()) and _reads_as_requirement(stripped)
    )


def _names_a_credential(text: str) -> bool:
    """Return whether text names a degree, a held credential, or years of experience."""
    return (
        any(says(text, phrases) for _, phrases in DEGREES)
        or bool(says(text, (*CLEARANCE, *HELD_CREDENTIALS)))
        or ("experience" in text.casefold() and YEARS.search(text) is not None)
    )


def _reads_as_requirement(sentence: str) -> bool:
    """Return whether a headingless prose sentence names a qualification, not boilerplate."""
    return PAY_CONTEXT.search(sentence) is None and names_a_qualification(sentence)


def _requires_in_own_clause(text: str) -> bool:
    """Return whether a clause of the line requires something and names no preference."""
    return any(
        says(unwaived(clause), MUST_MARKERS) and not preference_cues(clause)
        for clause in clauses(text)
    )


# Words that label the opening rather than ask for anything: "Entry level position".
_OPENING_LABEL = frozenset(
    ("and", "or", "entry", "level", "position", "role", "job", "opening", "this", "the")
)


def _names_something(text: str) -> bool:
    """Return whether ``text`` has a word beyond a cue's grammar, a join or an opening label."""
    return bool(set(SEPARATOR.split(text.casefold())) - {""} - CUE_FILLER - _OPENING_LABEL)


def _has_uncued_clause(text: str) -> bool:
    """Return whether a clause of the line names something with no preference cue.

    Under a requirement heading, "5+ years of experience; a CPA would be beneficial"
    keeps the heading's stance for its first clause.
    """
    return any(
        _names_something(unwaived(clause))
        and not preference_cues(clause)
        and not offers_substitute(clause)
        for clause in clauses(text)
    )


# A capitalised place name, or "the state of hire": case-sensitive inside an ignore-case pattern.
_PLACE = r"(?:the\s+state\s+of\s+\w+|(?-i:[A-Z][\w.]*(?:\s+[A-Z][\w.]*)?))"


# "Current RN license in good standing": a holder's adjective opening a phrase the licence
# heads, followed by nothing or by one tail that only says where or by whom it is held.
_HELD_LICENCE = re.compile(
    r"(?:an?\s+)?(?:active|current|valid|unrestricted|unencumbered)\s+(?:[\w'&/.-]+\s+){0,5}?"
    r"(?:licen[cs]e|licensure|registration|certification)s?"
    r"(?:\s+(?:in\s+good\s+standing"
    rf"|in\s+{_PLACE}(?:\s+or\s+{_PLACE})?"
    r"|from\s+(?:an?|the)\s+(?:[\w'&-]+\s+){0,5}?(?:board|state|province|agency)))?\.?",
    re.IGNORECASE,
)


_CONDITIONED = re.compile(
    r"\b(?:only|if|unless|except|lieu|waive[ds]?|option(?:al)?|preferred)\b", re.IGNORECASE
)


def _stance_of(text: str, block: Block) -> RequirementLine | None:
    """Return how firmly one line asks for something, or nothing when it does not."""
    judged = blanked(text, preference_asides(text))
    if (
        (preferred := preference_cues(judged))
        and not _requires_in_own_clause(judged)
        and not (block.kind is SectionKind.REQUIREMENT and _has_uncued_clause(judged))
    ):
        return RequirementLine(
            quote_line(text), Stance.PREFERRED, f"the line says {preferred[0]!r}", text
        )
    if block.kind is SectionKind.PREFERRED:
        return RequirementLine(
            quote_line(text), Stance.PREFERRED, f"under the heading {block.heading!r}", text
        )
    stated = unwaived(text)
    # A line whose every phrase waives what it names asks for nothing.
    if not _names_something(stated):
        return None
    must = says(stated, MUST_MARKERS)
    if block.kind is SectionKind.REQUIREMENT:
        reason = f"the line says {must[0]!r}" if must else f"under the heading {block.heading!r}"
        return RequirementLine(quote_line(text), Stance.MUST_HAVE, reason, text)
    if must:
        return RequirementLine(
            quote_line(text), Stance.MUST_HAVE, f"the line says {must[0]!r}", text
        )
    # Matched on the written line, since a waived sentence is blanked from ``stated``.
    if (
        _HELD_LICENCE.fullmatch(text.strip())
        and not WAIVED.search(text)
        and not _CONDITIONED.search(text)
    ):
        return RequirementLine(
            quote_line(text), Stance.MUST_HAVE, "the line names a licence to hold", text, bare=True
        )
    return None


def requirement_sections(blocks: tuple[Block, ...]) -> Answer[str]:
    """Return which blocks were read as the posting's requirement sections."""
    # A block resumed after a run-in label repeats its heading.
    named = list(
        dict.fromkeys(
            block.heading
            for block in blocks
            if block.kind in (SectionKind.REQUIREMENT, SectionKind.PREFERRED) and block.heading
        )
    )
    if not named:
        return Answer(
            "Requirement sections",
            note=(
                "no heading names a requirement or preferred section, so requirements "
                "were read only from lines that state their own stance"
            ),
        )
    return Answer("Requirement sections", ", ".join(named), evidence=tuple(named))


def counted_at_stance(requirements: tuple[RequirementLine, ...], stance: Stance) -> Answer[int]:
    """Return how many lines the posting states at one stance, or that it states none."""
    lines = [line for line in requirements if line.stance is stance]
    question = f"Lines stated as {stance.value}"
    if not lines:
        return Answer(question, note=f"no line is stated as {stance.value}")
    return Answer(question, len(lines), evidence=tuple(line.text for line in lines[:3]))
