"""Arrangement words and the office-day counts a posting states."""

from __future__ import annotations

import re
from typing import Literal

from tailorcv.domain.arrangement_grammar import OFFICE_ARRANGEMENT_WORDS
from tailorcv.domain.search_parameters import (
    Acceptance,
    Arrangement,
)
from tailorcv.shared.quantities import (
    DAYS_PER_MONTH,
    DAYS_PER_WEEK,
    FIGURE,
    PER,
    WEEKS_PER_MONTH,
    read_figure,
    stated_days,
    unit,
)
from tailorcv.shared.text import sanitize

# Each word a publisher names an arrangement with, and the name screening reads it as.
REMOTE_WORD = re.compile(
    r"\b(?:remote(?:ly)?|work(?:ing)?[- ]from[- ]home|wfh|telecommut\w*)\b", re.IGNORECASE
)


_ARRANGEMENT_WORDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("remote", REMOTE_WORD),
    ("hybrid", re.compile(r"\bhybrid\b", re.IGNORECASE)),
    (
        "on site",
        re.compile(rf"\b(?:{OFFICE_ARRANGEMENT_WORDS}|office[- ]based)\b", re.IGNORECASE),
    ),
)


# "Hybrid or Remote" and "Remote, Hybrid" offer both, so neither refuses a person.
_OFFER_JOINER = re.compile(r"\s*(?:or|and|/|&|,|\|)\s*(?:fully\s+)?", re.IGNORECASE)


# A comma label list ("Remote, Hybrid") is one label; a comma before prose starts evidence.
_LISTED_SECOND = re.compile(
    rf"\s*(?:fully\s+)?(?:remote|hybrid|{OFFICE_ARRANGEMENT_WORDS})\s*(?:,|$)",
    re.IGNORECASE,
)


# "Not Remote", "Non-Remote" and "Onsite (not remote)" name the arrangement they refuse.
_NEGATED_BEFORE = re.compile(r"\b(?:not|non|no)[\s-]+(?:fully[\s-]+)?$", re.IGNORECASE)


ArrangementCategory = Literal["remote", "hybrid", "on_site", "period_limited", "unknown"]


DAYS_PER_WORK_WEEK = 7


PERIOD_LIMITED = re.compile(
    r"\b(?:temporary|temporarily|interim|time[- ]limited|limited[- ]term|fixed[- ]term"
    rf"|until(?!\s+filled)|for\s+now|for\s+(?:the\s+first\s+)?(?:{FIGURE})\s*{unit('month')})\b",
    re.IGNORECASE,
)


def _offered_together(label: str, found: list[tuple[int, int, str]]) -> bool:
    """Return whether a remote word and a hybrid word are joined as two offers."""
    return any(
        _OFFER_JOINER.fullmatch(label, first_end, second_start)
        for _, first_end, first in found
        for second_start, _, second in found
        if first_end <= second_start and {first, second} == {"remote", "hybrid"}
    )


def _named_arrangement(label: str) -> str | None:
    """Return the one arrangement a label names; hybrid absorbs the office days it lists."""
    found = sorted(
        (match.start(), match.end(), name)
        for name, pattern in _ARRANGEMENT_WORDS
        for match in pattern.finditer(label)
        if not _NEGATED_BEFORE.search(label, 0, match.start())
    )
    named = {name for _, _, name in found}
    if "hybrid" in named:
        return None if _offered_together(label, found) else "hybrid"
    return next(iter(named)) if len(named) == 1 else None


def arrangement_value(value: str) -> tuple[str, int | None] | None:
    """Read only a named arrangement and an explicitly numeric weekly or monthly cadence.

    The name comes from the label before the first comma; a body-read value carries
    its evidence sentence after it, which may mention another arrangement.
    """
    text = sanitize(value)
    label, _, rest = text.partition(",")
    if _LISTED_SECOND.match(rest):
        label = text
    named = _named_arrangement(label)
    if named is None:
        return None
    days = _weekly_office_days(text)
    return named, days if days is not None else (0 if named == "remote" else None)


_FIGURE_SPAN = re.compile(FIGURE)


# A count's clause ends at punctuation or a conjunction: "4 days remote and 1 in office".
_COUNT_BREAK = re.compile(r"[,;:.!?](?=\s|$)|[()]|\s/\s|\b(?:and|but|while|with)\b", re.IGNORECASE)


# "our home office in Austin" is an office; "from home" and "at home" are not.
AWAY_FROM_OFFICE = re.compile(
    r"\b(?:remote(?:ly)?|(?:from|at)\s+home|home[\s-]based|wfh|telecommut\w*)\b", re.IGNORECASE
)


_AT_OFFICE = re.compile(rf"\b(?:office|{OFFICE_ARRANGEMENT_WORDS})\b", re.IGNORECASE)


# "up to 3 days" is a ceiling: it says nothing of the fewest days a person attends.
_CEILING = re.compile(r"\bup\s+to\s*$", re.IGNORECASE)


_WEEKLY = re.compile(rf"{PER}{unit('week')}|\bweekly\b", re.IGNORECASE)


# A figure states office days only with a day unit or the office right after it: "1 in
# office" does, "Building 5 office" and "1 office visit per quarter" do not.
# "Floor 3 onsite" and "team of 5 in office" number a place or people, not days.
_NUMBERED_NOUN = re.compile(
    r"\b(?:floor|building|suite|level|room|team\s+of)\s*#?\s*$", re.IGNORECASE
)


_OFFICE_DAY_COUNT = re.compile(
    rf"(?:{FIGURE})(?:\s*-?\s*{unit('day')}|\s+(?:in[\s-]?)?office\s+{unit('day')}"
    rf"|\s+(?:in\s+(?:the\s+|our\s+)?office|{OFFICE_ARRANGEMENT_WORDS})(?=\s*(?:[,;.)]|$|and\b)))",
    re.IGNORECASE,
)


def _weekly_office_days(value: str) -> int | None:
    """Return the stated office days per week, a range's bottom, converting a monthly count.

    A count its clause places at home or remote is not an office count, and a count
    the clause places in the office outranks one that names no place.
    """
    office: list[int | None] = []
    unplaced: list[int | None] = []
    for found in _FIGURE_SPAN.finditer(value):
        before, after = _count_clause(value, found)
        clause = before + after
        if AWAY_FROM_OFFICE.search(clause):
            continue
        at_office = _AT_OFFICE.search(clause) is not None
        days = _counted_days(value, found, at_office=at_office)
        if days is None:
            continue
        (office if at_office else unplaced).append(None if _CEILING.search(before) else days)
    chosen = office or unplaced
    return chosen[0] if chosen else None


def _count_clause(value: str, found: re.Match[str]) -> tuple[str, str]:
    """Return the text of a count's own clause before and after the figure."""
    breaks = list(_COUNT_BREAK.finditer(value, 0, found.start()))
    start = breaks[-1].end() if breaks else 0
    following = _COUNT_BREAK.search(value, found.end())
    end = following.start() if following else len(value)
    return value[start : found.start()], value[found.end() : end]


def _counted_days(value: str, found: re.Match[str], *, at_office: bool) -> int | None:
    """Return the weekly office days one figure states, or None when it states none."""
    if (weekly := DAYS_PER_WEEK.match(value, found.start())) is not None:
        days = stated_days(weekly)
    elif (monthly := DAYS_PER_MONTH.match(value, found.start())) is not None:
        # Rounded down: this count meets a limit that can refuse, so a borderline month passes.
        days = int(stated_days(monthly) / WEEKS_PER_MONTH) if stated_days(monthly) >= 0 else -1
    elif (
        at_office
        and _OFFICE_DAY_COUNT.match(value, found.start())
        and not _NUMBERED_NOUN.search(value, 0, found.start())
        and _WEEKLY.search(value)
    ):
        # "2 days onsite and 3 days remote per week" states its period once for both counts.
        figure = read_figure(found.group())
        days = -1 if figure is None else int(figure.floor)
    else:
        return None
    return days if 0 <= days <= DAYS_PER_WORK_WEEK else None


ARRANGEMENT_TOKEN = re.compile(
    rf"\b(remote|wfh|hybrid|{OFFICE_ARRANGEMENT_WORDS})\b", re.IGNORECASE
)


_ARRANGEMENT_TOKEN_TO_CATEGORY: dict[str, ArrangementCategory] = {
    "remote": "remote",
    "wfh": "remote",
    "hybrid": "hybrid",
    "onsite": "on_site",
    "inoffice": "on_site",
    "inperson": "on_site",
}


def token_category(token: str) -> ArrangementCategory:
    """Return the category one matched arrangement token names."""
    return _ARRANGEMENT_TOKEN_TO_CATEGORY[re.sub(r"[\s-]", "", token.casefold())]


def accepts_all(arrangement: Arrangement, readings: tuple[tuple[str, int | None], ...]) -> bool:
    """Return whether the saved arrangement accepts every arrangement the posting offers."""
    return all(
        arrangement.acceptance_of(name) in (Acceptance.PREFERRED, Acceptance.ACCEPTABLE)
        for name, _ in readings
    )
