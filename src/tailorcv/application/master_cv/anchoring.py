"""Carry a stored evidence anchor onto an edited CV and report what drifted.

Each citation is a quote plus a character span, the W3C Web Annotation pair. An
anchor whose quote is gone is reported as not found rather than guessed at.

Materiality catches figures and the negations in ``_NEGATIONS`` and nothing
else: ownership inflation, seniority drift, and scope that widens with no number
attached all read as rewording, so a cosmetic verdict is not a clean bill of health.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from itertools import accumulate

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_WORD = re.compile(r"[a-z]+")
_NEGATIONS = ("not", "never", "no", "without", "unable", "ceased", "declined")

_Opcodes = Sequence[tuple[str, int, int, int, int]]
_Span = tuple[int, int]


@dataclass(frozen=True)
class Anchor:
    """A stored evidence quote and its optional end-exclusive span in its CV version."""

    quote: str
    char_start: int | None
    char_end: int | None


@dataclass(frozen=True)
class Drift:
    """Where an anchor landed in the edited CV, whether its text changed, and if that matters."""

    found: bool
    text: str
    changed: bool
    material: bool
    reason: str


def reanchor_many(old_text: str, new_text: str, anchors: Sequence[Anchor]) -> list[Drift]:
    """Locate many anchors while computing the document diff only once."""
    opcodes = (
        _opcodes(old_text, new_text)
        if any(_stored_span(old_text, item) for item in anchors)
        else ()
    )
    return [_reanchor(old_text, new_text, anchor, opcodes) for anchor in anchors]


def _reanchor(old_text: str, new_text: str, anchor: Anchor, opcodes: _Opcodes) -> Drift:
    span = _stored_span(old_text, anchor)
    if span is None:
        located = _locate_quote(new_text, anchor.quote, anchor.char_start)
    else:
        carried = _carry(opcodes, span)
        if carried is not None and not _rewritten(opcodes, span):
            located = carried
        else:
            # Prefer the quote; fall back to the span when the quote is gone, which is
            # the case where the evidence itself was edited.
            near = carried[0] if carried is not None else anchor.char_start
            located = _locate_quote(new_text, anchor.quote, near) or carried

    if located is None:
        return Drift(
            found=False,
            text="",
            changed=False,
            material=False,
            reason="The quote is gone from the CV, so this evidence has to be re-cited by hand.",
        )

    text = new_text[located[0] : located[1]]
    if text == anchor.quote:
        return Drift(
            found=True,
            text=text,
            changed=False,
            material=False,
            reason="The anchored text is unchanged.",
        )

    figures = _numbers(text) != _numbers(anchor.quote)
    negations = _negations(text) != _negations(anchor.quote)
    if figures or negations:
        return Drift(
            found=True,
            text=text,
            changed=True,
            material=True,
            reason=_material_reason(figures, negations),
        )
    return Drift(
        found=True,
        text=text,
        changed=True,
        material=False,
        reason="Wording changed, but every figure and negation survived it.",
    )


def _material_reason(figures: bool, negations: bool) -> str:
    if figures and negations:
        return "The figures and a negation both changed, so the claim needs review."
    if figures:
        return "The figures changed, so the claim needs review."
    return "A negation or hedge changed, so the claim needs review."


def _stored_span(old_text: str, anchor: Anchor) -> _Span | None:
    """Return the stored span, or None when it is absent or does not fit `old_text`."""
    start, end = anchor.char_start, anchor.char_end
    if start is None or end is None or not 0 <= start < end <= len(old_text):
        return None
    return start, end


def _opcodes(old_text: str, new_text: str) -> _Opcodes:
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    old_offsets = (0, *accumulate(map(len, old_lines)))
    new_offsets = (0, *accumulate(map(len, new_lines)))
    line_opcodes = SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes()
    opcodes: list[tuple[str, int, int, int, int]] = []
    for tag, i1, i2, j1, j2 in line_opcodes:
        old_start, old_end = old_offsets[i1], old_offsets[i2]
        new_start, new_end = new_offsets[j1], new_offsets[j2]
        if tag != "replace":
            opcodes.append((tag, old_start, old_end, new_start, new_end))
            continue
        refined = SequenceMatcher(
            None,
            old_text[old_start:old_end],
            new_text[new_start:new_end],
            autojunk=False,
        ).get_opcodes()
        opcodes.extend(
            (kind, old_start + a1, old_start + a2, new_start + b1, new_start + b2)
            for kind, a1, a2, b1, b2 in refined
        )
    return opcodes


def _carry(opcodes: _Opcodes, span: _Span) -> _Span | None:
    """Map a span forward through unchanged blocks only.

    A boundary that sits inside a replaced or deleted block did not survive the
    edit in any position we can name, so the whole span is refused rather than
    reported at a guessed offset.
    """
    start, end = span
    new_start: int | None = None
    new_end: int | None = None
    for tag, i1, i2, j1, _j2 in opcodes:
        if tag != "equal":
            continue
        if i1 <= start < i2:
            new_start = j1 + (start - i1)
        if i1 < end <= i2:
            new_end = j1 + (end - i1)
    if new_start is None or new_end is None:
        return None
    return new_start, new_end


def _rewritten(opcodes: _Opcodes, span: _Span) -> bool:
    """True when text inside the span was replaced or deleted, not merely shifted."""
    start, end = span
    return any(
        tag in ("replace", "delete") and i1 < end and start < i2
        for tag, i1, i2, _j1, _j2 in opcodes
    )


def _locate_quote(new_text: str, quote: str, near: int | None) -> _Span | None:
    """Find `quote` exactly, preferring the occurrence closest to `near`."""
    if not quote:
        return None
    hits: list[int] = []
    at = new_text.find(quote)
    while at != -1:
        hits.append(at)
        at = new_text.find(quote, at + 1)
    if not hits:
        return None
    if near is None:
        return hits[0], hits[0] + len(quote)
    best = min(hits, key=lambda hit: (abs(hit - near), hit))
    return best, best + len(quote)


def _numbers(text: str) -> tuple[str, ...]:
    # Grouping commas are dropped, because 1,400 and 1400 are the same figure.
    return tuple(match.group().replace(",", "") for match in _NUMBER.finditer(text))


def _negations(text: str) -> frozenset[str]:
    words = set(_WORD.findall(text.lower()))
    return frozenset(word for word in _NEGATIONS if word in words)
