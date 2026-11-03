"""Pay figures quoted on posting lines."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import NamedTuple

from clearscout.application.tailoring.fit.lexicon import (
    BARE_RANGE,
    BIWEEKLY_PAY,
    BIWEEKLY_PERIODS,
    COUNTRY_CURRENCY,
    CURRENCY_CODES,
    CURRENCY_MENTION,
    JURISDICTION_SPLIT,
    PAY_CONTEXT,
    PAY_PERIODS,
    QUOTED_PAY,
    SYMBOL_CURRENCIES,
    TRAILING_PAY,
    ZONE_HEADER,
    ZONE_LINE,
    country_hint,
)
from clearscout.application.tailoring.fit.preferences.clauses import clause_spans, cost_word_beside
from clearscout.application.tailoring.fit.preferences.pay_periods import (
    NEARBY,
    SALARY_FLOOR,
    basis_by_size,
    basis_in,
    figure_amount,
    figure_groups,
    names_sub_annual_period,
    period_owner,
)
from clearscout.application.tailoring.fit.structure import (
    Answer,
    Block,
    SectionKind,
    is_boilerplate,
    quote_line,
)
from clearscout.domain.search_parameters import (
    CompensationBasis,
    JobSearchParameters,
)

# A range the extractor found but could not compare (foreign currency, missing
# basis, no matching band) still scores at this neutral standing so the overall
# score can be computed and the range can be surfaced.
EXTRACTED_ONLY_STANDING = 50


# A pay word right before a figure labels it pay, whatever follows: "Pay range:".
_PAY_LABEL = re.compile(rf"(?:{PAY_CONTEXT.pattern})\W*$", re.IGNORECASE)


# Labels under which a currency range below HOURLY_CEILING with no period word is hourly.
_HOURLY_RANGE_LABEL = re.compile(
    r"\b(?:(?:base\s+)?pay|salary|compensation|budgeted)\s+range\b|\bsalary\s*:"
    r"|\b(?:minimum|maximum)\s+salary\s*:",
    re.IGNORECASE,
)


# "per session" or "per patient" names a unit other than time worked.
_PER_OTHER_UNIT = re.compile(r"\bper\s+(?!hour\b|hr\b)\w", re.IGNORECASE)


# PAY_PERIODS leaves "annualized" out, but on the range's own line it states a yearly period.
_ANNUALIZED = re.compile(r"\bannuali[sz]", re.IGNORECASE)


_MINIMUM_SALARY = re.compile(r"\bminimum\s+salary\s*:", re.IGNORECASE)


_MAXIMUM_SALARY = re.compile(r"\bmaximum\s+salary\s*:", re.IGNORECASE)


HOURLY_CEILING = Decimal(200)


# A bi-weekly word right after a figure is its period; ", paid bi-weekly" after an
# annual salary says only how often pay arrives.
_BIWEEKLY_AFTER = re.compile(
    rf"\s*(?:(?:{'|'.join(CURRENCY_CODES)})\s*)?(?:(?:per|a|paid)\s+)?(?:{BIWEEKLY_PAY.pattern})",
    re.IGNORECASE,
)


# A paycheck is never hourly-sized or annual-sized.
_BIWEEKLY_SIZES = ("monthly", "daily")


_OTHER_LOCATIONS_TIER = re.compile(r"\bother\b[^:\d]*?\blocations?\b", re.IGNORECASE)
_PRIMARY_TIER = re.compile(r"\bprimary\s+(?:work\s+)?location\b", re.IGNORECASE)
# "Primary Location: Austin, TX" with no figure states where the primary tier applies.
_PRIMARY_PLACE = re.compile(
    r"^\W*primary\s+(?:work\s+)?location\s*[:\-]\s*([^$\d]+?)\s*$", re.IGNORECASE
)
_SHIFT_DIFFERENTIAL = re.compile(r"\bshift[\s-]+differentials?\b", re.IGNORECASE)


def line_maximum(line: str, minimum: Decimal) -> Decimal:
    """Return the top of the ranges that start at ``minimum``, else the line's highest pay."""
    periods = _periods_a_year(line)
    quotes = _line_quotes(line)
    tops = [
        figure_amount(quote.maximum) * periods
        for quote in quotes
        if quote.maximum and figure_amount(quote.amount) * periods == minimum
    ]
    if tops:
        return max(minimum, *tops)
    highest = minimum
    for quote in quotes:
        for value in (quote.amount, quote.maximum):
            if value:
                highest = max(highest, figure_amount(value) * periods)
    return highest


def _periods_a_year(line: str) -> int:
    """Return how many of the line's stated pay periods fall in a year, when no basis names it."""
    quotes = _line_quotes(line)
    if quotes and all(_paid_biweekly(line, quote) for quote in quotes):
        return BIWEEKLY_PERIODS
    return 1


class _Quote(NamedTuple):
    """A figure or range a currency marks on a line, and where it is written."""

    currency: str
    amount: str
    maximum: str
    start: int
    end: int


def _line_quotes(line: str) -> list[_Quote]:
    """Return the pay figures a line marks with a currency, before or after them.

    A bi-weekly figure beside an annual-sized one is that salary's equivalent, so
    it is left out.
    """
    quotes = [
        _Quote(hit["code"] or hit["symbol"], hit["amount"], hit["maximum"] or "", *hit.span())
        for hit in _pay_figures(line, QUOTED_PAY)
    ]
    for hit in _pay_figures(line, TRAILING_PAY):
        if not any(quote.start <= hit.start() < quote.end for quote in quotes):
            quotes.append(_trailing_quote(line, hit))
    biweekly = [quote for quote in quotes if _paid_biweekly(line, quote)]
    if any(basis_by_size(quote.amount) == "annual" for quote in quotes if quote not in biweekly):
        return [quote for quote in quotes if quote not in biweekly]
    return quotes


def _trailing_quote(line: str, hit: re.Match[str]) -> _Quote:
    """Return the figure a trailing currency code marks, with the bare range it closes."""
    bottom = next(
        (found for found in BARE_RANGE.finditer(line) if found.end() == hit.end("amount")),
        None,
    )
    if bottom is None:
        return _Quote(hit["code"], hit["amount"], "", *hit.span())
    return _Quote(hit["code"], bottom["amount"], hit["amount"], bottom.start(), hit.end())


def _paid_biweekly(line: str, quote: _Quote) -> bool:
    """Return whether a figure of paycheck size is stated per bi-weekly period."""
    return (
        _BIWEEKLY_AFTER.match(line, quote.end) is not None
        and basis_by_size(quote.amount) in _BIWEEKLY_SIZES
    )


def _shift_differential_names(line: str, word: re.Match[str], figure: tuple[int, int]) -> bool:
    """Return whether a differential label directly attributes this figure."""
    if word.end() <= figure[0]:
        return (
            re.fullmatch(r"\s*(?:(?:of|pay)\s*)?:?\s*", line[word.end() : figure[0]], re.IGNORECASE)
            is not None
        )
    between = line[figure[1] : word.start()].strip()
    return not between or any(pattern.fullmatch(between) for _, pattern in PAY_PERIODS)


def _pay_figures(line: str, pattern: re.Pattern[str]) -> list[re.Match[str]]:
    """Return figures except those attributed to costs or shift differentials."""
    groups = figure_groups(line)
    clauses = clause_spans(line)
    differentials: set[tuple[int, int]] = set()
    for word in _SHIFT_DIFFERENTIAL.finditer(line):
        if groups:
            group = period_owner(groups, clauses, word.span())
            if _shift_differential_names(line, word, group):
                differentials.add(group)
    return [
        found
        for found in pattern.finditer(line)
        if not any(start <= found.start() < end for start, end in differentials)
        and (
            _PAY_LABEL.search(line, 0, found.start())
            or not cost_word_beside(line, found.start(), found.end())
        )
    ]


def extracted_only(
    question: str,
    currency: str,
    minimum: Decimal,
    maximum: Decimal,
    basis: CompensationBasis,
    line: str,
    *,
    note: str,
) -> tuple[Answer[str], int, bool, str]:
    """Return a neutral answer when the range is present but no band compares."""
    range_text = f"{minimum:,.0f}" if minimum == maximum else f"{minimum:,.0f}-{maximum:,.0f}"
    return (
        Answer(
            question,
            f"shown: {currency} {range_text} {basis}",
            evidence=(quote_line(line),),
            note=note,
        ),
        EXTRACTED_ONLY_STANDING,
        False,
        "",
    )


def quoted_pay(
    blocks: tuple[Block, ...],
    wanted: JobSearchParameters,
) -> tuple[str, Decimal, CompensationBasis | None, str, str] | None:
    """Return the currency, amount, pay period, reading line, and source line of a posting's pay.

    A figure pair carrying no currency counts only on a line naming pay. Boards
    that pay differently by country write one line each, so a line naming a
    country the reader declared is taken over one that names none, except that
    a Primary Location tier holds unless the reader is known to be elsewhere.
    """
    preferred_currencies = frozenset(band.currency for band in wanted.compensation)
    lines, section, candidates, hourly, source = _pay_pass(
        blocks, preferred_currencies, content_only=True
    )
    about_pay = _about_pay(candidates, section, blocks)
    if not about_pay:
        # A block headed "Compensation" is boilerplate to a requirement reading
        # and is exactly where the salary is written.
        every = _pay_pass(blocks, preferred_currencies, content_only=False)
        if every_about := _about_pay(every[2], every[1], blocks):
            (lines, section, candidates, hourly, source), about_pay = every, every_about
        elif not candidates:
            lines, section, candidates, hourly, source = every
    if not candidates:
        return None

    # A line or heading naming pay outranks a stray figure elsewhere in the posting.
    preferred = about_pay or candidates
    named = [item for item in preferred if wanted.country_named_in(item[1])]
    primary = [item for item in preferred if _PRIMARY_TIER.search(item[1])]
    if (
        primary
        and _same_country_tier(named, primary[0], lines)
        and not _reader_elsewhere_than_primary(lines, wanted)
    ):
        named = primary
    index, line, written, figure = (named or preferred)[0]
    currency = (
        SYMBOL_CURRENCIES.get(written, written)
        or _page_currency(lines)
        or COUNTRY_CURRENCY.get(country_hint("\n".join(lines)))
        or ""
    )
    if not currency:
        return None
    periods = _periods_a_year(line)
    basis: CompensationBasis | None = (
        "annual"
        if periods > 1
        else "hourly"
        if index in hourly
        else basis_in(lines, section, index, figure)
    )
    return currency, figure_amount(figure) * periods, basis, line, source[index]


def _same_country_tier(
    named: list[tuple[int, str, str, str]],
    primary: tuple[int, str, str, str],
    lines: tuple[str, ...],
) -> bool:
    """Return whether the country-named lines are tiers in the primary line's own country.

    Country decides it, not the written symbol: a bare "$" is shared across countries.
    """
    if not named:
        return True
    places = _primary_places(lines)
    primary_country = (
        country_hint(" ".join(places)) or country_hint(primary[1]) or country_hint("\n".join(lines))
    )
    primary_code = primary[2] if _is_currency_code(primary[2]) else ""

    def same(line: str, written: str) -> bool:
        if primary_code and _is_currency_code(written) and written != primary_code:
            return False
        line_country = country_hint(line)
        if _OTHER_LOCATIONS_TIER.search(line):
            return line_country in ("", primary_country)
        return bool(line_country) and line_country == primary_country

    return all(same(line, written) for _, line, written, _ in named)


def _primary_places(lines: tuple[str, ...]) -> list[str]:
    """Return the places a "Primary Location" pay line names."""
    return [match.group(1) for line in lines if (match := _PRIMARY_PLACE.search(line))]


def _is_currency_code(written: str) -> bool:
    """Return whether ``written`` is a three-letter currency code rather than a symbol."""
    return len(written) == 3 and written.isalpha()


def _reader_elsewhere_than_primary(lines: tuple[str, ...], wanted: JobSearchParameters) -> bool:
    """Return whether the posting names its primary location and no declared place lies in it.

    A declaration naming only a country says nothing about where in it the reader is.
    """
    places = _primary_places(lines)
    specific = [location for location in wanted.locations if location.metro or location.region]
    if not places or not specific:
        return False
    return not any(
        (location.named_tier(place.casefold()) or 0) > 1
        for place in places
        for location in specific
    )


def _pay_pass(
    blocks: tuple[Block, ...], preferred_currencies: frozenset[str], *, content_only: bool
) -> tuple[
    tuple[str, ...],
    tuple[int, ...],
    list[tuple[int, str, str, str]],
    dict[int, frozenset[str]],
    tuple[str, ...],
]:
    """Return one pass's lines, blocks, pay candidates, hourly ranges, and unfolded lines."""
    source, section = _sections(blocks, content_only=content_only)
    source, section = _expand_jurisdiction_lines(source, section)
    lines, section = _fold_zone_lines(source, section, blocks)
    lines = _fold_minimum_maximum_lines(lines)
    hourly = _hourly_ranges(lines, section, blocks)
    return lines, section, _pay_candidates(lines, preferred_currencies, hourly), hourly, source


def _fold_minimum_maximum_lines(lines: tuple[str, ...]) -> tuple[str, ...]:
    """Append the range a "Minimum Salary" line and the "Maximum Salary" line after it state.

    Only figures below HOURLY_CEILING fold. The appended text is for reading only; evidence
    quotes the unfolded line.
    """
    folded = list(lines)
    for index in range(len(lines) - 1):
        low = QUOTED_PAY.search(lines[index]) if _MINIMUM_SALARY.search(lines[index]) else None
        high = (
            QUOTED_PAY.search(lines[index + 1])
            if _MAXIMUM_SALARY.search(lines[index + 1])
            else None
        )
        if low is None or high is None or low["maximum"] or high["maximum"]:
            continue
        if max(figure_amount(low["amount"]), figure_amount(high["amount"])) < HOURLY_CEILING:
            folded[index] = f"{lines[index]} (range: {low[0]} - {high['amount']})"
    return tuple(folded)


def _hourly_ranges(
    lines: tuple[str, ...], section: tuple[int, ...], blocks: tuple[Block, ...]
) -> dict[int, frozenset[str]]:
    """Map each line quoting a labelled range below HOURLY_CEILING to that range's two figures.

    The label may be on the line before the range, alone on the line before it, or the
    block's heading; a period word anywhere in the block, or "annualized" on the line, vetoes.
    """
    periodic: set[int] = {
        section[position]
        for position, text in enumerate(lines)
        if any(pattern.search(text) for _, pattern in PAY_PERIODS)
    }
    found: dict[int, frozenset[str]] = {}
    for index, line in enumerate(lines):
        if section[index] in periodic or _ANNUALIZED.search(line):
            continue
        label = _hourly_range_label(lines, section, blocks, index)
        if label is None or _PER_OTHER_UNIT.search(line) or _PER_OTHER_UNIT.search(label[0]):
            continue
        quote = _hourly_range(line, label[1])
        if quote is not None:
            found[index] = frozenset({quote["amount"], quote["maximum"]})
    return found


def _hourly_range_label(
    lines: tuple[str, ...], section: tuple[int, ...], blocks: tuple[Block, ...], index: int
) -> tuple[str, int] | None:
    """Return the pay label over a line and where on the line a range may start after it.

    The label is on the line itself, a bare line before it, or its heading.
    """
    line = lines[index]
    if hit := _HOURLY_RANGE_LABEL.search(line):
        return line, hit.end()
    first = index == 0 or section[index - 1] != section[index]
    label = blocks[section[index]].heading if first else lines[index - 1]
    if _HOURLY_RANGE_LABEL.search(label) and not re.search(r"\d", label):
        return label, 0
    return None


def _hourly_range(line: str, start: int) -> re.Match[str] | None:
    """Return the first currency range after ``start`` with both ends in (0, HOURLY_CEILING)."""
    quote = next(
        (hit for hit in QUOTED_PAY.finditer(line) if hit["maximum"] and hit.start() >= start),
        None,
    )
    # A sentence break between the label and the range means the range is about something else.
    if quote is None or re.search(r"[.;]\s", line[start : quote.start()]):
        return None
    if _MAGNITUDE_AFTER.match(line, quote.end()):
        return None
    ends = [figure_amount(quote[group]) for group in ("amount", "maximum")]
    return quote if all(0 < end < HOURLY_CEILING for end in ends) else None


# "$95 - $125 thousand" or "USD 80 - 100 k" scales the figures, so they are not hourly.
_MAGNITUDE_AFTER = re.compile(r"\s*(?:k|thousand|m|mm|million|b|bn|billion)\b", re.IGNORECASE)


def _about_pay(
    candidates: list[tuple[int, str, str, str]], section: tuple[int, ...], blocks: tuple[Block, ...]
) -> list[tuple[int, str, str, str]]:
    """Return the candidates on a line, or under a heading, that names pay."""
    return [
        item
        for item in candidates
        if PAY_CONTEXT.search(item[1]) or PAY_CONTEXT.search(blocks[section[item[0]]].heading)
    ]


def _expand_jurisdiction_lines(
    lines: tuple[str, ...], section: tuple[int, ...]
) -> tuple[tuple[str, ...], tuple[int, ...]]:
    """Split "If performed in X, ..." stanzas concatenated onto one line."""
    expanded: list[str] = []
    owners: list[int] = []
    for index, line in enumerate(lines):
        parts = [part.strip() for part in JURISDICTION_SPLIT.split(line) if part.strip()]
        owner = section[index] if index < len(section) else 0
        expanded.extend(parts if len(parts) > 1 else [line])
        owners.extend([owner] * (max(1, len(parts))))
    return tuple(expanded), tuple(owners)


def _fold_zone_lines(
    lines: tuple[str, ...], section: tuple[int, ...], blocks: tuple[Block, ...]
) -> tuple[tuple[str, ...], tuple[int, ...]]:
    """Widen the first zone-row line in each zone table to span all zones.

    The aggregate range is appended so QUOTED_PAY sees both endpoints; it is for
    reading only, and evidence quotes the unfolded line.
    """
    # A zone header ending in a colon heads its own block rather than being a line of one.
    headed = frozenset(
        index
        for index, owner in enumerate(section)
        if (index == 0 or section[index - 1] != owner) and ZONE_HEADER.search(blocks[owner].heading)
    )
    runs = _zone_runs(lines, headed)
    if not runs:
        return lines, section
    rewritten = list(lines)
    for start, run in runs:
        widened = _zone_widened_line(rewritten[start], run)
        if widened is not None:
            rewritten[start] = widened
    return tuple(rewritten), section


def _zone_runs(lines: tuple[str, ...], headed: frozenset[int]) -> list[tuple[int, list[str]]]:
    """Return each geographic-zone table found in ``lines`` as (start, run)."""
    runs: list[tuple[int, list[str]]] = []
    header_seen = False
    start = -1
    run: list[str] = []
    for index, line in enumerate(lines):
        if index in headed:
            if run:
                runs.append((start, run))
            header_seen, start, run = True, -1, []
        if ZONE_HEADER.search(line):
            if run:
                runs.append((start, run))
            header_seen, start, run = True, -1, []
            continue
        if header_seen and ZONE_LINE.match(line):
            if start < 0:
                start = index
            run.append(line)
            continue
        if run:
            runs.append((start, run))
            header_seen, start, run = False, -1, []
    if run:
        runs.append((start, run))
    return runs


def _zone_widened_line(anchor: str, run: list[str]) -> str | None:
    """Return ``anchor`` with an aggregate min-max appended, or ``None`` if not needed."""
    if len(run) < 2:
        return None
    amounts: list[Decimal] = []
    for line in run:
        for hit in QUOTED_PAY.finditer(line):
            amounts.append(figure_amount(hit.group("amount")))
            if hit.group("maximum"):
                amounts.append(figure_amount(hit.group("maximum")))
    if len(amounts) < 2:
        return None
    low, high = min(amounts), max(amounts)
    return f"{anchor} (all zones: ${low:,.0f} - ${high:,.0f})"


def _sections(
    blocks: tuple[Block, ...], *, content_only: bool = False
) -> tuple[tuple[str, ...], tuple[int, ...]]:
    """Return every line and which block each one came from."""
    lines: list[str] = []
    owners: list[int] = []
    for position, block in enumerate(blocks):
        for line in block.lines:
            if content_only and (block.kind is SectionKind.BOILERPLATE or is_boilerplate(line)):
                continue
            lines.append(line)
            owners.append(position)
    return tuple(lines), tuple(owners)


def _pay_candidates(
    lines: tuple[str, ...],
    preferred_currencies: frozenset[str] = frozenset(),
    hourly: dict[int, frozenset[str]] | None = None,
) -> list[tuple[int, str, str, str]]:
    """Return every figure in ``lines`` that could be the pay this posting quotes.

    ``preferred_currencies`` breaks ties on lines that name more than one
    currency, so a cross-currency note quoting the user's currency wins.
    """
    found: list[tuple[int, str, str, str]] = []
    hourly = hourly or {}
    for index, line in enumerate(lines):
        quoted = _line_quoted_pay(line, preferred_currencies, hourly.get(index, frozenset()))
        if quoted is not None:
            currency, amount = quoted
            found.append((index, line, currency, amount))
            continue
        bare = _line_bare_pay(lines, index, line)
        if bare is not None:
            found.append((index, line, "", bare))
    return found


def _line_quoted_pay(
    line: str, preferred_currencies: frozenset[str], hourly: frozenset[str] = frozenset()
) -> tuple[str, str] | None:
    """Return the currency and lowest pay-sized amount on a line that names its currency.

    A figure below SALARY_FLOOR needs its own stated sub-annual period, or to be in ``hourly``.
    """
    written = [
        (quote.currency, quote.amount)
        for quote in _line_quotes(line)
        if quote.amount in hourly
        or figure_amount(quote.amount) >= SALARY_FLOOR
        or names_sub_annual_period(line, quote.amount)
    ]
    trailing = [
        (item.group("code"), item.group("amount"))
        for item in _pay_figures(line, TRAILING_PAY)
        if item.group("amount") in hourly
        or figure_amount(item.group("amount")) >= SALARY_FLOOR
        or names_sub_annual_period(line, item.group("amount"))
    ]
    if not written:
        return None
    written, trailing = _narrow_to_preferred(written, trailing, preferred_currencies)
    lowest = min(written, key=lambda pair: figure_amount(pair[1]))
    currency = trailing[0][0] if trailing else lowest[0]
    return currency, lowest[1]


def _narrow_to_preferred(
    written: list[tuple[str, str]],
    trailing: list[tuple[str, str]],
    preferred_currencies: frozenset[str],
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Keep only pairs in the preferred currency set when a line names more than one."""
    if not preferred_currencies:
        return written, trailing
    # A bare "$" is whichever dollar the line's code names, so it is no second currency.
    resolved = {SYMBOL_CURRENCIES.get(pair[0], pair[0]) for pair in written} - {"$"}
    if len(resolved) <= 1:
        return written, trailing
    narrowed = [
        pair for pair in written if SYMBOL_CURRENCIES.get(pair[0], pair[0]) in preferred_currencies
    ]
    if not narrowed:
        return written, trailing
    return narrowed, [pair for pair in trailing if pair in narrowed]


def _line_bare_pay(lines: tuple[str, ...], index: int, line: str) -> str | None:
    """Return the amount of a currency-less range only when the line is about pay."""
    if not _about_pay_near(lines, index):
        return None
    bare = BARE_RANGE.search(line)
    if bare is None:
        return None
    if figure_amount(bare.group("amount")) < SALARY_FLOOR:
        return None
    return bare.group("amount")


def _about_pay_near(lines: tuple[str, ...], index: int) -> bool:
    """Return whether a line within ``NEARBY`` lines of this one names pay."""
    window = lines[max(index - NEARBY, 0) : index + NEARBY + 1]
    return any(PAY_CONTEXT.search(line) for line in window)


def dollar_currency(wanted: JobSearchParameters, place: str | None) -> str | None:
    """Return which dollar a bare sign means, from the band for this place."""
    bands = [band for band in wanted.compensation if place is None or band.location == place]
    currencies = {band.currency for band in bands}
    return next(iter(currencies)) if len(currencies) == 1 else None


def _page_currency(lines: tuple[str, ...]) -> str:
    """Return the one currency code the posting writes, if it writes exactly one."""
    written = {found for line in lines for found in CURRENCY_MENTION.findall(line)}
    return next(iter(written)) if len(written) == 1 else ""
