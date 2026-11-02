"""Read the figures posting text states: numbers, ranges, open-ended counts and units.

Every surface form is data defined once below and composed into standard-library
regular-expression fragments. ``FIGURE`` embeds in a caller's pattern, and
``read_figure`` turns the text it matched into a typed ``Figure``. Callers fold
their text with ``shared.text.sanitize`` first, so dash and tilde glyphs arrive
as ``-`` and ``~``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

_ONES = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)
_TENS = ("twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
NUMBER_WORDS: dict[str, int] = {word: value for value, word in enumerate(_ONES)} | {
    word: 20 + 10 * index for index, word in enumerate(_TENS)
}
# A letter suffix attaches to its digits ("120k", "1.5M"); a scale word follows a space.
MULTIPLIER_SUFFIXES: dict[str, int] = {"k": 1_000, "K": 1_000, "M": 1_000_000, "MM": 1_000_000}
# Only a thousands "k" may stand one space apart ("120 k"); a spaced "M" is "M&A".
SPACED_SUFFIXES = ("k", "K")
MULTIPLIER_WORDS: dict[str, int] = {"thousand": 1_000, "million": 1_000_000}
CURRENCY_SIGNS = "$\N{EURO SIGN}\N{POUND SIGN}\N{YEN SIGN}"
# A joiner between two numbers that makes them one range, read from its bottom.
RANGE_SEPARATORS = ("-", "~", "to", "through", "or")
# A mark after a number that leaves the figure open above it.
OPEN_MARKERS = ("+", "plus", "or more", "or above", "or greater")
UNITS: dict[str, tuple[str, ...]] = {
    "year": ("years", "year", "yrs", "yr"),
    "month": ("months", "month", "mos", "mo"),
    "week": ("weeks", "week", "wks", "wk"),
    "day": ("days", "day"),
    "hour": ("hours", "hour", "hrs", "hr"),
    "percent": ("percent", "per cent", "%"),
}


def _alternation(forms: tuple[str, ...] | list[str]) -> str:
    """Return a longest-first alternation of literal forms, spaces matching any run."""
    ordered = sorted(forms, key=len, reverse=True)
    return "|".join(re.escape(form).replace(r"\ ", r"\s+") for form in ordered)


def _words_fragment() -> str:
    tens = "|".join(_TENS)
    units = "|".join(_ONES[1:10])
    ones = _alternation(list(_ONES))
    return rf"(?i:\b(?:(?:{tens})(?:[- ](?:{units}))?|{ones})\b)"


_WORD_NUMBER = _words_fragment()
# Grouped thousands take a comma or a single space; a decimal follows only digits.
_DIGITS = (
    r"(?<![\w.,])(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?(?!\d|,\d)|\d{1,3}(?: \d{3})+(?!\d|[,.]\d)"
    r"|\d+(?:\.\d+)?(?!\d))"
)
# "five (5) years" states its figure twice; the digits in parentheses repeat it.
_ECHO = r"(?:\s*\(\s*\d{1,3}\s*\+?\s*\))?"
_SIGN = rf"(?:[{CURRENCY_SIGNS}]\s*)?"
_MULTIPLIER = (
    rf"(?:(?-i:{_alternation(list(MULTIPLIER_SUFFIXES))}|\s(?:{_alternation(SPACED_SUFFIXES)}))"
    r"(?![A-Za-z&])"
    rf"|\s*(?i:{_alternation(list(MULTIPLIER_WORDS))})\b)"
)
_SEPARATOR = (
    r"\s*(?:-\s*to\s*-|[-~]|(?i:"
    + "|".join(rf"\b{word}\b" for word in RANGE_SEPARATORS if word.isalpha())
    + r"))\s*"
)
_OPEN = (
    r"(?:\s*\+|\s*(?:-\s*)?(?i:"
    + "|".join(rf"\b{mark.replace(' ', r'\s+')}\b" for mark in OPEN_MARKERS if mark[0].isalpha())
    + r"))"
)


def _group(name: str, body: str, capture: bool) -> str:
    return f"(?P<{name}>{body})" if capture else f"(?:{body})"


def _number(capture: bool) -> str:
    return (
        _group("sign", _SIGN, capture)
        + "(?:"
        + _group("digits", _DIGITS, capture)
        + "|"
        + _group("word", _WORD_NUMBER, capture)
        + _group("echo", _ECHO, capture)
        + ")"
        + _group("multiplier", _MULTIPLIER, capture)
        + "?"
    )


def _figure(capture: bool) -> str:
    number = _number(False)
    return (
        rf"(?:(?i:between)\s+{_group('first', number, capture)}\s+(?i:and)\s+"
        + _group("last", number, capture)
        + "|"
        + _group("low", number, capture)
        + rf"(?:{_SEPARATOR}{_group('high', number, capture)})?"
        + _group("open", _OPEN, capture)
        + "?)"
    )


# A figure as a non-capturing fragment, safe to embed any number of times.
FIGURE = _figure(capture=False)
NUMBER = _number(capture=False)
_FIGURE_PARTS = re.compile(_figure(capture=True))
_NUMBER_PARTS = re.compile(_number(capture=True))


def unit(kind: str) -> str:
    """Return a fragment matching every written form of one unit in ``UNITS``."""
    forms = UNITS[kind]
    words = [form for form in forms if form[0].isalpha()]
    symbols = [form for form in forms if not form[0].isalpha()]
    parts = [rf"(?i:(?:{_alternation(words)})\b\.?)"] if words else []
    parts += [re.escape(symbol) for symbol in symbols]
    return "(?:" + "|".join(parts) + ")"


@dataclass(frozen=True)
class Figure:
    """One stated figure: a number, or a range from low to high, perhaps open above."""

    low: Decimal
    high: Decimal
    open: bool = False

    @property
    def floor(self) -> Decimal:
        """Return what a minimum gate reads: a range's bottom."""
        return self.low


def _number_value(text: str) -> tuple[Decimal, int | None] | None:
    """Return a number token's value and the multiplier it carries, if any."""
    parts = _NUMBER_PARTS.fullmatch(text.strip())
    if parts is None:
        return None
    if parts.group("digits"):
        try:
            value = Decimal(parts.group("digits").replace(",", "").replace(" ", ""))
        except InvalidOperation:
            return None
    else:
        pieces = re.split(r"[- ]", parts.group("word").casefold())
        value = Decimal(sum(NUMBER_WORDS[piece] for piece in pieces))
    suffix = (parts.group("multiplier") or "").strip()
    scale = MULTIPLIER_SUFFIXES.get(suffix) or MULTIPLIER_WORDS.get(suffix.casefold())
    return value, scale


def _scaled(value: Decimal, scale: int | None) -> Decimal:
    return value * scale if scale else value


def _range(low_text: str, high_text: str | None, open_above: bool) -> Figure | None:
    """Return the figure two number tokens state, sharing one side's multiplier."""
    low = _number_value(low_text)
    if low is None:
        return None
    if high_text is None:
        value = _scaled(*low)
        return Figure(value, value, open_above)
    high = _number_value(high_text)
    if high is None:
        return None
    (low_value, low_scale), (high_value, high_scale) = low, high
    # "95-130 thousand" and "$120k-150" scale both sides by the one multiplier stated.
    if low_scale is None and high_scale is not None and low_value <= high_value:
        low_scale = high_scale
    elif high_scale is None and low_scale is not None and low_value <= high_value:
        high_scale = low_scale
    bottom, top = sorted((_scaled(low_value, low_scale), _scaled(high_value, high_scale)))
    return Figure(bottom, top, open_above)


def _from_parts(parts: re.Match[str]) -> Figure | None:
    if parts.group("first") is not None:
        return _range(parts.group("first"), parts.group("last"), False)
    low, high = parts.group("low"), parts.group("high")
    # An echo such as "five (5+)" carries its own open mark.
    open_above = parts.group("open") is not None or "+" in low + (high or "")
    return _range(low, high, open_above)


def read_figure(text: str) -> Figure | None:
    """Return the figure ``text`` states when the whole of it is one figure."""
    parts = _FIGURE_PARTS.fullmatch(text.strip())
    return None if parts is None else _from_parts(parts)


def figures(text: str) -> Iterator[Figure]:
    """Yield every figure ``text`` states, in order."""
    for parts in _FIGURE_PARTS.finditer(text):
        if (figure := _from_parts(parts)) is not None:
            yield figure


# "two (2) days", "3-4 days", "3x a week", and "3 times per week" all state a count.
DAY_COUNT = rf"(?P<days>{FIGURE})(?:\s*(?:-\s*)?{unit('day')}|\s*x\b|\s+times\b)"
PER = r"(?:\s*/\s*|\s+(?:a|per|each)\s+)"
DAYS_PER_WEEK = re.compile(rf"{DAY_COUNT}(?:{PER}{unit('week')}|\s+weekly\b)", re.IGNORECASE)
DAYS_PER_MONTH = re.compile(rf"{DAY_COUNT}{PER}{unit('month')}", re.IGNORECASE)
WEEKS_PER_MONTH = 52 / 12


def stated_days(count: re.Match[str]) -> int:
    """Return the days a ``DAY_COUNT`` match states, a range's bottom, or -1."""
    figure = read_figure(count["days"])
    return -1 if figure is None else int(figure.floor)
