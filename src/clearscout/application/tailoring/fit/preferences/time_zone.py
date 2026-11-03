"""Time-zone readings against the zones a person wants."""

from __future__ import annotations

import re

from clearscout.application.tailoring.fit.lexicon import (
    REQUIRED_TIME_ZONE,
    TIME_ZONE_OFFSETS,
    TIME_ZONE_REQUIRED,
    TIME_ZONE_TOLERANCE,
    WISH_OPENER,
    clause_breaks,
    phrase_breaks,
    preference_cues,
)
from clearscout.application.tailoring.fit.structure import (
    Answer,
    Block,
    content_lines,
    quote_line,
    unasked_answer,
)
from clearscout.domain.search_parameters import (
    JobSearchParameters,
)
from clearscout.shared.quantities import read_figure

_WITH = re.compile(r"\bwith\b", re.IGNORECASE)


def _wished_zone(line: str, start: int, end: int) -> bool:
    """Return whether a cue in the zone's own phrase, or opening its clause, wishes for it."""
    phrases = phrase_breaks(line)
    opens = max((stop for _, stop in phrases if stop <= start), default=0)
    closes = min((begin for begin, _ in phrases if begin >= end), default=len(line))
    # "Eastern time zone with Spanish a plus": a "with" before a cue opens a phrase of its own.
    for found in _WITH.finditer(line, end, closes):
        if preference_cues(line[found.start() : closes]):
            closes = found.start()
            break
    phrase = line[opens:closes]
    clause = max((stop for _, stop in clause_breaks(line) if stop <= start), default=0)
    opening = line[clause:start]
    return bool(preference_cues(phrase)) or WISH_OPENER.match(opening) is not None


def _required_zone(blocks: tuple[Block, ...]) -> tuple[str, int, str] | None:
    """Return the zone a posting requires, the hours either side it allows, and the line."""
    for line in content_lines(blocks):
        if TIME_ZONE_REQUIRED.search(line) is None:
            continue
        found = next(
            (
                named
                for named in REQUIRED_TIME_ZONE.finditer(line)
                if not _wished_zone(line, *named.span())
            ),
            None,
        )
        if found is None:
            continue
        zone = (found.group("zone") or found.group("zone_after") or "").casefold()
        if zone not in TIME_ZONE_OFFSETS:
            continue
        allowed = TIME_ZONE_TOLERANCE.search(line)
        figure = read_figure(allowed["hours"]) if allowed is not None else None
        # A tolerance is a ceiling, so a range reads its top.
        return zone, int(figure.high) if figure is not None else 0, line
    return None


def time_zone_fit(blocks: tuple[Block, ...], wanted: JobSearchParameters) -> Answer[str]:
    """Say whether this person's working zone reaches the one a posting requires."""
    question = "Time zone you work in"
    mine = wanted.working_time_zone.value.casefold() if wanted.working_time_zone else ""
    if mine not in TIME_ZONE_OFFSETS:
        return unasked_answer(question)
    required = _required_zone(blocks)
    if required is None:
        return Answer(question, note="the posting requires no time zone")
    zone, tolerance, line = required
    distance = abs(TIME_ZONE_OFFSETS[mine] - TIME_ZONE_OFFSETS[zone])
    shift = wanted.working_day_shift_hours
    verdict = "reaches" if distance <= tolerance + shift else "outside"
    counted = f" plus your {shift}-hour shift" if shift else ""
    return Answer(
        question,
        f"{mine.title()} is {_hours(distance)} from {zone.title()} against a tolerance of "
        f"{_hours(tolerance)}{counted}: {verdict}",
        evidence=(quote_line(line),),
    )


def _hours(count: int) -> str:
    """Return a count of hours with its unit."""
    return f"{count} hour" if count == 1 else f"{count} hours"
