"""Quoting posting lines and naming the role a posting is for."""

from __future__ import annotations

import re

from clearscout.application.tailoring.fit.lexicon import (
    BLURB_CUES,
    LABELLED_TITLE,
    MARKDOWN_HEADING,
    QUOTE,
)
from clearscout.domain.taxonomy import SEPARATOR, says


def quote_line(text: str, anchor: int | str | None = None) -> str:
    """Return one line of source text, flattened and short enough to print.

    A long line keeps its opening, or the window holding ``anchor``: an index into
    ``text``, or a phrase it contains.
    """
    flat = " ".join(text.split())
    if len(flat) <= QUOTE:
        return flat
    if isinstance(anchor, str):
        # Found as ``says`` matches it, so "remote first" finds "remote-first".
        words = [word for word in SEPARATOR.split(anchor.casefold()) if word]
        spelled = "[^a-z0-9+]+".join(map(re.escape, words))
        found = re.search(rf"(?<![a-z0-9+]){spelled}(?![a-z0-9+])", text, re.IGNORECASE)
        anchor = found.start() if found else -1
    at = -1 if anchor is None or anchor < 0 else len(" ".join(text[:anchor].split()))
    start = min(at - (QUOTE - 6) // 2, len(flat) - (QUOTE - 3))
    if start <= 0:
        return flat[: QUOTE - 3] + "..."
    if start + QUOTE - 3 == len(flat):
        return "..." + flat[start:]
    return "..." + flat[start : start + QUOTE - 6] + "..."


def unshortened(quote: str) -> str:
    """Return a ``quote_line`` quote without the ellipses that mark it shortened."""
    return quote.removeprefix("...").removesuffix("...")


def first_saying(lines: tuple[str, ...], phrases: tuple[str, ...]) -> tuple[str, str] | None:
    """Return the first (phrase, line) pair where a line uses one of ``phrases``."""
    for line in lines:
        found = says(line, phrases)
        if found:
            return found[0], line
    return None


def posting_title(posting: str) -> str:
    """Return the posting's own name for the role, as best the text gives it.

    The first non-empty line that is not company prose, less any Markdown heading
    marker and any "Job title:" label. A scrape that opens with a mission
    statement ("At X, we believe ...") skips it rather than reporting it as the role.
    """
    for raw in posting.splitlines():
        line = raw.strip()
        if not line:
            continue
        if heading := MARKDOWN_HEADING.match(line):
            line = heading.group("text").strip()
        if labelled := LABELLED_TITLE.match(line):
            line = labelled.group("text").strip()
        if _company_blurb(line):
            continue
        return line
    return ""


def _company_blurb(line: str) -> bool:
    """Return whether a line opens with company prose rather than the role title."""
    folded = line.casefold()
    if folded.startswith("at ") and "," in line[:60]:
        return True
    return any(cue in folded for cue in BLURB_CUES)
