"""The pay period a quoted figure states or implies."""

from __future__ import annotations

import re
from decimal import Decimal

from tailorcv.application.tailoring.fit.lexicon import (
    BARE_RANGE,
    PAY_CONTEXT,
    PAY_PERIODS,
    QUOTED_PAY,
    RANGE_SEPARATOR,
    TRAILING_PAY,
)
from tailorcv.application.tailoring.fit.preferences.clauses import clause_spans
from tailorcv.domain.search_parameters import (
    CompensationBasis,
)
from tailorcv.shared.text import sentence_breaks

# How many lines either side of a figure may state its pay period.
NEARBY = 2


# The smallest figure a currency-less range may be read as pay, which keeps
# "2 to 3 years" out.
SALARY_FLOOR = Decimal(1000)


# The figure each sub-annual period stays below: no one is paid six figures an hour.
_PERIOD_CEILINGS: dict[CompensationBasis, Decimal] = {
    "hourly": Decimal(1_000),
    "daily": Decimal(10_000),
    "monthly": Decimal(100_000),
}


_PAY_RANGE_JOIN = re.compile(rf"\s*{RANGE_SEPARATOR}\s*")


def figure_amount(written: str) -> Decimal:
    """Return a written figure as a number, dropping its grouping.

    A trailing ``k`` or ``K`` denotes thousands, so ``150K`` reads as 150000.
    """
    text = written.replace(",", "").replace(" ", "")
    if text.endswith(("k", "K")):
        return Decimal(text[:-1]) * Decimal(1000)
    return Decimal(text)


def basis_in(
    lines: tuple[str, ...], section: tuple[int, ...], index: int, figure: str
) -> CompensationBasis | None:
    """Return the pay period this posting states for the figure on ``index``.

    Period words attach only to the picked line itself or to nearby lines that
    also name pay-sized figures. Unrelated stipend or story lines cannot
    restamp the basis. Figure size settles it when nothing else applies.
    """
    picked = lines[index] if index < len(lines) else ""
    period = _figure_period(picked, figure)
    if period is not None:
        return period
    amount = figure_amount(figure)
    window_lines = lines[max(index - NEARBY, 0) : index + NEARBY + 1]
    neighbours = [line for line in window_lines if line != picked and _line_is_about_pay(line)]
    period = _pay_period_in(tuple(neighbours), amount)
    if period is not None:
        return period
    if index < len(section):
        owner = section[index]
        rest = tuple(
            line
            for position, line in enumerate(lines)
            if section[position] == owner and line != picked and _line_is_about_pay(line)
        )
        period = _pay_period_in(rest, amount)
        if period is not None:
            return period
    return basis_by_size(figure)


def _figure_period(line: str, figure: str) -> CompensationBasis | None:
    """Return the period word on ``line`` nearest the figure written ``figure``.

    Only a period a figure that size can be quoted per counts. A word in another
    sentence counts only when the figure's size settles nothing.
    """
    groups = figure_groups(line)
    written = re.search(rf"(?<![\d,.]){re.escape(figure)}(?!\d)", line)
    at = written.start() if written is not None else -1
    mine = next((group for group in groups if group[0] <= at < group[1]), None)
    amount = figure_amount(figure)
    if mine is None:
        return _pay_period_in((line,), amount)
    breaks = sentence_breaks(line)
    opens = max((end for _, end in breaks if end <= at), default=0)
    closes = min((start for start, _ in breaks if start >= at), default=len(line))
    clauses = clause_spans(line)
    owned = [
        (not opens <= word.start() < closes, _gap(mine, word.span()), basis)
        for basis, pattern in PAY_PERIODS
        if _period_fits(basis, amount)
        for word in pattern.finditer(line)
        if period_owner(groups, clauses, word.span()) == mine
    ]
    if not owned:
        return None
    elsewhere, _, basis = min(owned)
    return None if elsewhere and basis_by_size(figure) is not None else basis


def period_owner(
    groups: list[tuple[int, int]], clauses: list[tuple[int, int]], word: tuple[int, int]
) -> tuple[int, int]:
    """Return the figure a period word belongs to: the nearest in its clause, else on the line."""
    clause = next((span for span in clauses if span[0] <= word[0] <= span[1]), word)
    inside = [group for group in groups if clause[0] <= group[0] < clause[1]]
    return min(inside or groups, key=lambda group: _gap(group, word))


def _period_fits(basis: CompensationBasis, amount: Decimal) -> bool:
    """Return whether a figure this size can be quoted per ``basis``."""
    if basis == "annual":
        return amount >= SALARY_FLOOR
    return amount < _PERIOD_CEILINGS[basis]


def figure_groups(line: str) -> list[tuple[int, int]]:
    """Return figure spans, merging overlaps and ranges that repeat their currency."""
    spans = sorted(
        found.span()
        for pattern in (QUOTED_PAY, TRAILING_PAY, BARE_RANGE)
        for found in pattern.finditer(line)
    )
    groups: list[tuple[int, int]] = []
    for start, end in spans:
        if groups and (
            start < groups[-1][1] or _PAY_RANGE_JOIN.fullmatch(line[groups[-1][1] : start])
        ):
            groups[-1] = (groups[-1][0], max(end, groups[-1][1]))
        else:
            groups.append((start, end))
    return groups


def _gap(one: tuple[int, int], other: tuple[int, int]) -> int:
    """Return how many characters lie between two spans, zero when they touch."""
    return max(one[0] - other[1], other[0] - one[1], 0)


def _pay_period_in(lines: tuple[str, ...], amount: Decimal) -> CompensationBasis | None:
    """Return the first period ``lines`` state that a figure of ``amount`` can be quoted per."""
    for candidate, pattern in PAY_PERIODS:
        if _period_fits(candidate, amount) and any(pattern.search(line) for line in lines):
            return candidate
    return None


def basis_by_size(figure: str) -> CompensationBasis | None:
    """Return the only pay period a figure this size could plausibly be quoted per."""
    amount = figure_amount(figure)
    if amount >= Decimal(50_000):
        return "annual"
    if amount >= Decimal(5_000):
        return "monthly"
    if amount >= Decimal(500):
        return "daily"
    return None


def _line_is_about_pay(line: str) -> bool:
    """Return whether a line carries base-pay evidence a period word may attach to.

    Stipends and story numbers below SALARY_FLOOR do not carry the picked
    figure's basis, so their period words never propagate.
    """
    if PAY_CONTEXT.search(line):
        bare = BARE_RANGE.search(line)
        if bare is not None and figure_amount(bare.group("amount")) >= SALARY_FLOOR:
            return True
    for match in QUOTED_PAY.finditer(line):
        if figure_amount(match.group("amount")) >= SALARY_FLOOR:
            return True
    for match in TRAILING_PAY.finditer(line):
        if figure_amount(match.group("amount")) >= SALARY_FLOOR:
            return True
    return False


def names_sub_annual_period(line: str, figure: str) -> bool:
    """Return whether a stated sub-annual period belongs to this figure."""
    return _figure_period(line, figure) in ("hourly", "daily", "monthly")
