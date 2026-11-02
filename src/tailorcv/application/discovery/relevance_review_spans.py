"""Deterministic posting spans used by discovery relevance-review citations."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RelevanceReviewPostingSpan:
    """One application-owned citation identity and its exact source text."""

    id: str
    text: str
    char_start: int
    char_end: int


def posting_spans(pending_discovery_id: str, body: str) -> tuple[RelevanceReviewPostingSpan, ...]:
    """Split literal source paragraphs into stable, bounded citation spans."""
    pieces = [
        piece
        for match in re.finditer(
            r"\S(?:.*?\S)?(?=[^\S\r\n]*(?:\r?\n[^\S\r\n]*\r?\n|\s*\Z))",
            body,
            flags=re.DOTALL,
        )
        for piece in _bounded_pieces(body, match.start(), match.end())
    ]
    return tuple(
        RelevanceReviewPostingSpan(
            id=f"{pending_discovery_id}:span-{index:04d}",
            text=body[start:end],
            char_start=start,
            char_end=end,
        )
        for index, (start, end) in enumerate(pieces, start=1)
    )


def _bounded_pieces(
    body: str, start: int, end: int, *, ceiling: int = 800
) -> list[tuple[int, int]]:
    pieces: list[tuple[int, int]] = []
    while end - start > ceiling:
        split = body.rfind(" ", start, start + ceiling + 1)
        if split <= start:
            split = start + ceiling
        pieces.append((start, split))
        start = split
        while start < end and body[start].isspace():
            start += 1
    if start < end:
        pieces.append((start, end))
    return pieces
