"""Preference asides and waived phrases, blanked in place."""

from __future__ import annotations

import re

from clearscout.application.tailoring.fit.lexicon import (
    MUST_MARKERS,
    names_beyond,
    phrase_breaks,
    preference_cues,
)
from clearscout.domain.requirements import WAIVED
from clearscout.domain.taxonomy import says

ASIDE = re.compile(r"\([^()]*\)")


def preference_asides(text: str) -> list[tuple[int, int]]:
    """Return the spans of preference asides that name something, on a line that requires.

    "(Doctorate preferred)" and "(5+ years is preferred)" qualify something beside the
    requirement; a bare "(preferred)" is the line's own stance and is not returned.
    """
    spans = [
        found.span()
        for found in ASIDE.finditer(text)
        if (cues := preference_cues(found.group())) and names_beyond(found.group(), cues)
    ]
    return spans if spans and says(unwaived(blanked(text, spans)), MUST_MARKERS) else []


def blanked(text: str, spans: list[tuple[int, int]]) -> str:
    """Return ``text`` with each span blanked, keeping every position where it was."""
    for start, end in spans:
        text = text[:start] + " " * (end - start) + text[end:]
    return text


def waived_spans(text: str) -> list[tuple[int, int]]:
    """Return the span of each aside and phrase a waiver sits in.

    "5+ years of experience, CPA not required" waives only " CPA not required".
    """
    asides = [found.span() for found in ASIDE.finditer(text) if WAIVED.search(found.group())]
    outside = blanked(text, asides)
    ends = [end for _, end in phrase_breaks(outside)]
    phrases = zip([0, *ends], [*ends, len(text)], strict=True)
    return asides + [
        (start, end) for start, end in phrases if WAIVED.search(outside[start:end].strip())
    ]


def unwaived(text: str) -> str:
    """Return ``text`` with each aside and phrase a waiver sits in blanked."""
    return blanked(text, waived_spans(text))
