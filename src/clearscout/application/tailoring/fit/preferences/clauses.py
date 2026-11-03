"""Clause edges and cost words that tell pay from other figures on a line."""

from __future__ import annotations

import re

from clearscout.application.tailoring.fit.lexicon import (
    CURRENCY_CODES,
    PAY_CONTEXT,
    TRAVEL_COST,
)

_JOINING = r"(?:plus|with|and|including)"


# A figure's own words end at a clause break or a joining word, so the benefits in
# "Great benefits and" a salary name no figure.
_CLAUSE_END = re.compile(rf"[.:](?=\s|$)|;|\b{_JOINING}\b", re.IGNORECASE)


# How many words before a figure can name what it pays for: "401(k) match up to".
_WORDS_BEFORE = 5


# A pay word since the last amount or clause break owns the figure, as in "total cash
# pay, including base and bonus, is" a figure; "Level 3" is no amount, but "401(k)" is.
_PAY_SCOPE_END = re.compile(r"[.:](?=\s|$)|;|[$£€]\s*\d|\d{3,}")


# A cost named by the few words right after a figure, as in "per month car allowance";
# a currency code also ends the figure's own words.
_COST_AFTER = re.compile(
    rf"(?:[\s-]+(?!(?:{_JOINING}|{'|'.join(CURRENCY_CODES)})\b)[^\s,;:.()+&]+){{0,3}}?"
    rf"[\s-]+{TRAVEL_COST.pattern}",
    re.IGNORECASE,
)


# A cost joined to the figure by "of" or "up to" names it, even after "compensation
# includes": "a signing bonus of" a figure. A cost label ends in a colon.
_COST_NAMES = re.compile(
    rf"{TRAVEL_COST.pattern}(?:\s+(?:of\s+)?(?:up\s+to\s+)?|(?P<label>\s*:\s*))$", re.IGNORECASE
)


def cost_word_beside(line: str, start: int, end: int) -> bool:
    """Return whether the words attached to the figure at ``start:end`` name a cost.

    Those are the last few before it in its clause, with no pay word among them, and
    the few right after it.
    """
    before = " ".join(_CLAUSE_END.split(line[:start])[-1].split()[-_WORDS_BEFORE:])
    scope = _PAY_SCOPE_END.split(line[:start])[-1]
    named = _COST_NAMES.search(line, 0, start)
    # "Salary and benefits:" labels pay, so a label naming pay is no cost.
    if named and not (
        named["label"] and PAY_CONTEXT.search(_PAY_SCOPE_END.split(line[: named.start()])[-1])
    ):
        return True
    if TRAVEL_COST.search(before) and not PAY_CONTEXT.search(scope):
        return True
    return _COST_AFTER.match(line, end) is not None


def clause_spans(line: str) -> list[tuple[int, int]]:
    """Return the spans between a line's clause breaks and joining words."""
    edges = [0]
    for found in _CLAUSE_END.finditer(line):
        edges.extend(found.span())
    edges.append(len(line))
    return list(zip(edges[::2], edges[1::2], strict=True))
