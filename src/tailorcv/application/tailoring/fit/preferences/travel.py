"""Travel share and travel-cost readings."""

from __future__ import annotations

import re

from tailorcv.application.tailoring.fit.lexicon import (
    BENEFIT_HEADINGS,
    TRAVEL_BELOW,
    TRAVEL_COST,
    TRAVEL_PERCENT,
)
from tailorcv.application.tailoring.fit.preferences.clauses import cost_word_beside
from tailorcv.application.tailoring.fit.structure import (
    Answer,
    Block,
    SectionKind,
    quote_line,
    unasked_answer,
)
from tailorcv.domain.search_parameters import (
    JobSearchParameters,
)

# The top of a range whose low end a travel match read, as in "25% to 30%" or
# "30% - up to 75%"; "and" joins a range only after "between".
_RANGE_TOP = re.compile(
    r"\s*(?:%|per ?cent)?\s*(?P<join>-|to|and)\s*(?:up\s+to\s+)?(\d{1,3})\s*(?:%|per ?cent)",
    re.IGNORECASE,
)


_RANGE_BETWEEN = re.compile(r"\bbetween\s+$", re.IGNORECASE)


# A figure stated as a share of the travel, as in "30% of travel".
_SHARE_OF_TRAVEL = re.compile(
    r"\s*(?:%|per ?cent)\s+of\s+(?:the\s+|all\s+)?(?:travel|travelling|traveling)\b", re.IGNORECASE
)


# A top stays in the travel statement only when a clause end, a travel or time word, or
# a verb of that statement follows it; "10% - 90% telework" measures something else.
_RANGE_TOP_END = re.compile(
    r"\s*(?:$|[.,;:!?)]|(?:and|or)\b|(?:in\s+)?travel(?:l?ing)?\b"
    r"|of\s+(?:the\s+|your\s+)?(?:time|year|week)\b|per\s+year\b"
    r"|(?:annually|domestic|international|overnight|depending|based\s+on)\b"
    r"|(?:(?:may|will)\s+be\s+|is\s+)?(?:required|expected|needed)(?:\s+for\b|\s*(?:$|[.,;:!?)]))"
    r"|(?:may|is|will)\b[^.,;:!?]*?\btravel)",
    re.IGNORECASE,
)


# The low end of a range right before a figure, as in "10% - " or "10% to ".
_RANGE_BOTTOM_BEFORE = re.compile(
    r"\d{1,3}\s*(?:%|per ?cent)\s*(?:-|to)\s*(?:up\s+to\s+)?$", re.IGNORECASE
)


_RANGE_LOW = re.compile(
    r"(\d{1,3})\s*(?:%|per ?cent)?\s*(?P<join>-|to|and)\s*(?:up\s+to\s+)?$",
    re.IGNORECASE,
)


_PERCENT_SIGN = re.compile(r"\s*(?:%|per ?cent)", re.IGNORECASE)


def _lines_outside_benefits(blocks: tuple[Block, ...]) -> tuple[str, ...]:
    """Return every line not under a heading naming what the employer gives."""
    return tuple(
        line
        for block in blocks
        if not any(fragment in block.heading.casefold() for fragment in BENEFIT_HEADINGS)
        for line in block.lines
    )


def _stated_travel(
    lines: tuple[str, ...], *, limit: int | None = None
) -> tuple[int, bool, str, int, int] | None:
    """Return the decisive travel share, its bound, evidence, and low end."""
    shares: list[tuple[int, bool, str, int, int]] = []
    for line in lines:
        read_on_line = False
        for found in TRAVEL_PERCENT.finditer(line):
            if _travel_cost(line, found):
                continue
            group = 1 if found.group(1) is not None else 2
            # After a stated share, "50% of travel is international" splits that travel.
            if read_on_line and _SHARE_OF_TRAVEL.match(line, found.end(group)):
                continue
            share = _travel_share(line, found)
            if share is None:
                continue
            low, percent = share
            read_on_line = True
            below = TRAVEL_BELOW.search(line, 0, found.start(group)) is not None
            shares.append((percent, below, line, found.start(), low))
    return max(
        shares,
        key=lambda share: (
            limit is not None and not share[1] and share[4] > limit,
            share[0],
            share[4],
            not share[1],
        ),
        default=None,
    )


def _travel_cost(line: str, found: re.Match[str]) -> bool:
    """Return whether a travel match states a share of a cost or a payment, not of the time."""
    return TRAVEL_COST.search(found.group()) is not None or cost_word_beside(
        line, found.start(), found.end()
    )


def _travel_share(line: str, found: re.Match[str]) -> tuple[int, int] | None:
    """Return the low and high shares one travel match states."""
    group = 1 if found.group(1) is not None else 2
    if found.group(group) is None:
        return None
    if group == 2 and not _top_stays_travel(line, found.start(group), found.end(group)):
        return None
    percent = int(found.group(group))
    if percent > 100:
        return None
    if (top := _range_top(line, found.start(group), found.end(group))) is not None:
        return min(percent, top), max(percent, top)
    low = _RANGE_LOW.search(line, 0, found.start(group))
    if low is not None and (
        low["join"].casefold() != "and" or _RANGE_BETWEEN.search(line, 0, low.start()) is not None
    ):
        return min(int(low[1]), percent), max(int(low[1]), percent)
    return percent, percent


def _range_top(line: str, start: int, end: int) -> int | None:
    """Return the top of a range whose low end is the figure at ``start:end``, if one joins it."""
    top = _RANGE_TOP.match(line, end)
    if top is None or int(top.group(2)) > 100:
        return None
    if top["join"].casefold() == "and" and _RANGE_BETWEEN.search(line, 0, start) is None:
        return None
    if _RANGE_TOP_END.match(line, top.end()) is None:
        return None
    return int(top.group(2))


def _top_stays_travel(line: str, start: int, end: int) -> bool:
    """Return whether the figure at ``start:end`` is a travel share if it tops a range.

    A top the range reader refused is not read again from a travel word further on.
    """
    if _RANGE_BOTTOM_BEFORE.search(line, 0, start) is None:
        return True
    sign = _PERCENT_SIGN.match(line, end)
    return sign is not None and _RANGE_TOP_END.match(line, sign.end()) is not None


def travel_fit(
    blocks: tuple[Block, ...], wanted: JobSearchParameters, place: str | None = None
) -> Answer[str]:
    """Say whether the travel a posting states is inside what this person accepts."""
    question = "Travel against your limit"
    limit = wanted.arrangement_for(place).maximum_travel_percent
    if limit is None:
        return unasked_answer(question)
    stated = _stated_travel(_lines_outside_benefits(blocks), limit=limit)
    if stated is None:
        return Answer(question, note="the posting states no travel share to compare")
    percent, below, line, start, low = stated
    evidence = (quote_line(line, start),)
    if below and percent > limit:
        return Answer(
            question,
            evidence=evidence,
            note=f"the posting says only that travel stays below {percent} per cent",
        )
    # A requirements list asking for travel inside the limit disputes a higher
    # share stated elsewhere, so the posting is held for review.
    required = _stated_travel(
        tuple(
            line
            for block in blocks
            if block.kind is SectionKind.REQUIREMENT
            for line in block.lines
        )
    )
    if percent > limit and required is not None and required[0] <= limit:
        return Answer(
            question,
            evidence=(*evidence, quote_line(required[2], required[3])),
            note=f"the posting states {percent} per cent and requires {required[0]}",
            conflict=True,
        )
    if low <= limit < percent:
        return Answer(
            question,
            evidence=evidence,
            note=f"the posting states a travel range of {low}-{percent} per cent "
            f"against your {limit}",
            conflict=True,
        )
    verdict = "inside" if percent <= limit else "over"
    bound = "below " if below else ""
    return Answer(
        question, f"{bound}{percent} per cent against your {limit}: {verdict}", evidence=evidence
    )
