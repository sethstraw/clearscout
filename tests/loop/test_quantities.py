"""The shared quantity reader reads every written form of a figure to one typed value."""

from __future__ import annotations

import re
from collections.abc import Callable
from decimal import Decimal
from itertools import product

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tailorcv.shared.quantities import (
    FIGURE,
    NUMBER_WORDS,
    UNITS,
    Figure,
    figures,
    read_figure,
    unit,
)
from tailorcv.shared.text import sanitize

_WORDS = {value: word for word, value in NUMBER_WORDS.items()}


def _spelled(value: int) -> str:
    """Return the words a writer spells ``value`` with, for 0 to 99."""
    if value in _WORDS:
        return _WORDS[value]
    return f"{_WORDS[value - value % 10]}-{_WORDS[value % 10]}"


_STYLES: dict[str, Callable[[int], str]] = {
    "digits": str,
    "word": _spelled,
    "capitalised word": lambda value: _spelled(value).capitalize(),
    "word with its digits": lambda value: f"{_spelled(value)} ({value})",
}
_SEPARATORS = (
    "-",
    " - ",
    "\u2013",
    " \u2014 ",
    "~",
    "\u301c",
    "\uff5e",
    " to ",
    " TO ",
    " through ",
    " or ",
    "-to-",
)
_OPEN_MARKS = ("+", " +", " or more", " or above", " or greater", " plus", "-plus", " Or More")
_PAIRS = ((1, 2), (3, 5), (12, 15), (20, 25))


def _read(text: str) -> Figure | None:
    return read_figure(sanitize(text))


@pytest.mark.parametrize(
    ("pair", "low_style", "high_style", "separator"),
    list(product(_PAIRS, _STYLES, _STYLES, _SEPARATORS)),
)
def test_a_range_reads_its_bottom_however_it_is_joined(
    pair: tuple[int, int], low_style: str, high_style: str, separator: str
) -> None:
    low, high = pair
    text = _STYLES[low_style](low) + separator + _STYLES[high_style](high)
    figure = _read(text)
    assert figure == Figure(Decimal(low), Decimal(high))
    assert figure.floor == low


@pytest.mark.parametrize(("pair", "style"), list(product(_PAIRS, _STYLES)))
def test_between_and_reads_as_a_range(pair: tuple[int, int], style: str) -> None:
    low, high = pair
    text = f"between {_STYLES[style](low)} and {_STYLES[style](high)}"
    assert _read(text) == Figure(Decimal(low), Decimal(high))


@pytest.mark.parametrize(
    ("value", "style", "mark"), list(product((1, 5, 10, 25), _STYLES, _OPEN_MARKS))
)
def test_an_open_mark_leaves_the_figure_open_above(value: int, style: str, mark: str) -> None:
    assert _read(_STYLES[style](value) + mark) == Figure(Decimal(value), Decimal(value), open=True)


_THOUSANDS = {
    "comma": lambda thousands: f"{thousands},000",
    "space": lambda thousands: f"{thousands} 000",
    "no-break space": lambda thousands: f"{thousands}\u00a0000",
    "narrow space": lambda thousands: f"{thousands}\u202f000",
    "thousand": lambda thousands: f"{thousands} thousand",
    "Thousand": lambda thousands: f"{thousands} Thousand",
    "k": lambda thousands: f"{thousands}k",
    "K": lambda thousands: f"{thousands}K",
    "spaced k": lambda thousands: f"{thousands} k",
    "spaced K": lambda thousands: f"{thousands} K",
}
_SIGNS = ("", "$", "\u00a3", "\u20ac", "\uffe1")


@pytest.mark.parametrize(
    ("thousands", "form", "sign"), list(product((95, 125, 160), _THOUSANDS, _SIGNS))
)
def test_every_thousands_form_reads_the_same_amount(thousands: int, form: str, sign: str) -> None:
    value = Decimal(thousands * 1000)
    assert _read(sign + _THOUSANDS[form](thousands)) == Figure(value, value)


@pytest.mark.parametrize(
    ("form", "separator"), list(product(_THOUSANDS, ("-", " - ", "\u2013", " to ")))
)
def test_a_pay_range_reads_both_ends_in_thousands(form: str, separator: str) -> None:
    text = _THOUSANDS[form](125) + separator + _THOUSANDS[form](160)
    assert _read(text) == Figure(Decimal(125000), Decimal(160000))


@pytest.mark.parametrize(
    ("text", "low", "high"),
    [
        ("95-130 thousand", 95000, 130000),
        ("$120-150k", 120000, 150000),
        ("$120k-150", 120000, 150000),
        ("1.5M", 1500000, 1500000),
        ("1-2 million", 1000000, 2000000),
    ],
)
def test_one_stated_multiplier_scales_both_ends(text: str, low: int, high: int) -> None:
    assert _read(text) == Figure(Decimal(low), Decimal(high))


_UNIT_FIGURES = {
    "3": Figure(Decimal(3), Decimal(3)),
    "3-5": Figure(Decimal(3), Decimal(5)),
    "three to five": Figure(Decimal(3), Decimal(5)),
    "3+": Figure(Decimal(3), Decimal(3), open=True),
}


@pytest.mark.parametrize(
    ("kind", "form", "figure"),
    [
        (kind, form, figure)
        for kind, forms in UNITS.items()
        for form, figure in product(forms, _UNIT_FIGURES)
    ],
)
def test_every_unit_form_follows_a_figure(kind: str, form: str, figure: str) -> None:
    pattern = re.compile(rf"(?P<figure>{FIGURE})\s*{unit(kind)}")
    found = pattern.fullmatch(f"{figure} {form}") or pattern.fullmatch(f"{figure}{form}")
    assert found is not None
    assert read_figure(found["figure"]) == _UNIT_FIGURES[figure]


@pytest.mark.parametrize(
    "text",
    [
        "in the office 3-4 days a week",
        "Pay: USD 125 000 - 160 000 per year.",
        "We need three to five years of SQL.",
    ],
)
def test_a_figure_inside_a_sentence_reads_as_it_does_alone(text: str) -> None:
    alone = {
        "in the office 3-4 days a week": "3-4",
        "Pay: USD 125 000 - 160 000 per year.": "125 000 - 160 000",
        "We need three to five years of SQL.": "three to five",
    }[text]
    assert next(figures(text)) == read_figure(alone)


@pytest.mark.parametrize(
    "text", ["", "remote", "USD per year", "9am", "Q3", "H1B", "W-2", "3,4", "one-off"]
)
def test_text_that_states_no_whole_figure_reads_none(text: str) -> None:
    assert read_figure(text) is None


@pytest.mark.parametrize(
    ("text", "value"),
    [("5 km", 5), ("5 Kafka clusters", 5), ("3 kids", 3), ("5 M&A deals", 5), ("2 MM", 2)],
)
def test_a_spaced_letter_that_begins_a_word_multiplies_nothing(text: str, value: int) -> None:
    assert next(figures(text)) == Figure(Decimal(value), Decimal(value))


_RANGES = st.tuples(st.integers(0, 99), st.integers(0, 99)).map(sorted)


@given(
    _RANGES,
    st.sampled_from(sorted(_STYLES)),
    st.sampled_from(sorted(_STYLES)),
    st.sampled_from(_SEPARATORS),
)
def test_a_variant_reads_the_same_as_its_canonical_form(
    pair: list[int], low_style: str, high_style: str, separator: str
) -> None:
    low, high = pair
    variant = _STYLES[low_style](low) + separator + _STYLES[high_style](high)
    assert _read(variant) == Figure(Decimal(low), Decimal(high))


@given(st.text(alphabet="bcdfghjlmpqrstvwxyzBCDFG ,.;:-~()/", max_size=40))
def test_text_with_no_digit_or_number_word_states_no_figure(text: str) -> None:
    assert list(figures(sanitize(text))) == []
