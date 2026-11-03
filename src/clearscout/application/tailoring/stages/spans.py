"""Find a quoted span in the source text it claims to have come from.

Both sides are compared in a normalized form, and the offsets returned are into
the original text. :class:`~clearscout.domain.SpanStatus` is domain vocabulary and
is used here rather than owned here.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from clearscout.domain import SpanStatus


class Span(BaseModel):
    """Where a quote sits in its source, or that it could not be placed there."""

    model_config = ConfigDict(frozen=True)

    status: SpanStatus = SpanStatus.NEEDS_REVIEW
    start: int = -1
    end: int = -1

    @property
    def verified(self) -> bool:
        """Return whether the quote was found in the source."""
        return self.status is SpanStatus.VERIFIED


UNPLACED = Span()


def normalize(text: str) -> str:
    """Collapse whitespace, drop it before punctuation, trim, and fold case."""
    return _folded(text)[0]


def _folded(text: str) -> tuple[str, tuple[int, ...]]:
    """Return the normalized text beside the source offset of each character.

    Case folding one character can yield several, so the map is many-to-one.
    Whitespace preceding a punctuation character is dropped, not collapsed.
    """
    characters: list[str] = []
    offsets: list[int] = []
    pending_space = False
    for index, character in enumerate(text):
        if character.isspace():
            pending_space = bool(characters)
            continue
        if pending_space:
            if character.isalnum():
                characters.append(" ")
                offsets.append(index)
            pending_space = False
        for folded in character.casefold():
            characters.append(folded)
            offsets.append(index)
    return "".join(characters), tuple(offsets)


def locate(quote: str, source: str) -> Span:
    """Return where a quote occurs in the source, or that it does not occur there."""
    needle = normalize(quote)
    if not needle:
        return UNPLACED
    haystack, offsets = _folded(source)
    position = haystack.find(needle)
    if position < 0:
        return UNPLACED
    return Span(
        status=SpanStatus.VERIFIED,
        start=offsets[position],
        end=offsets[position + len(needle) - 1] + 1,
    )
