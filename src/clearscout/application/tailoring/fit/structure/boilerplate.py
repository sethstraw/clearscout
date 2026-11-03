"""The employer's legal, benefits, and pay language."""

from __future__ import annotations

from clearscout.application.tailoring.fit.lexicon import (
    BOILERPLATE_LINES,
    BOILERPLATE_SUBJECTS,
    BULLET,
    PAY_CONTEXT,
    PAY_PERIODS,
    QUALIFICATION_CUE,
    QUOTED_PAY,
    TRAILING_PAY,
)
from clearscout.application.tailoring.fit.structure.reading import Block, SectionKind


def content_lines(blocks: tuple[Block, ...]) -> tuple[str, ...]:
    """Return every line of the posting that is not the employer's boilerplate."""
    return tuple(
        line
        for block in blocks
        if block.kind is not SectionKind.BOILERPLATE
        for line in block.lines
        if not is_boilerplate(line)
    )


def states_pay(line: str) -> bool:
    """Return whether a line quotes a pay figure, named by a pay word or a pay period."""
    if not (QUOTED_PAY.search(line) or TRAILING_PAY.search(line)):
        return False
    return bool(PAY_CONTEXT.search(line)) or any(
        period.search(line) for _basis, period in PAY_PERIODS
    )


# "Five years of", "Experience with", "Strong knowledge of": the words a qualification opens on.
_QUALIFICATION_OPENING = 3


def is_boilerplate(line: str) -> bool:
    """Return whether a line is the employer's legal or benefits language."""
    folded = line.casefold()
    if any(marker in folded for marker in BOILERPLATE_LINES):
        return True
    if not any(marker in folded for marker in BOILERPLATE_SUBJECTS):
        return False
    # A qualification opens the line it is the subject of; pay wording stays pay.
    text = found.group("text") if (found := BULLET.match(line)) else line
    opening = " ".join(text.split()[:_QUALIFICATION_OPENING])
    return PAY_CONTEXT.search(line) is not None or not names_a_qualification(opening)


def names_a_qualification(text: str) -> bool:
    """Return whether text names something a person holds: years, a degree, a skill."""
    folded = text.casefold()
    return any(cue in folded for cue in QUALIFICATION_CUE)
