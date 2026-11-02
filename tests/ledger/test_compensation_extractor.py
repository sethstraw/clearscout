"""Compensation extractor regression suite."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tailorcv.application.master_cv.cv import parse_cv
from tailorcv.application.tailoring.fit import read
from tailorcv.application.tailoring.fit.preferences import EXTRACTED_ONLY_STANDING
from tailorcv.domain.search_parameters import (
    CompensationBand,
    CompensationBasis,
    CountryNames,
    JobSearchParameters,
    Location,
    WorkEligibility,
)
from tests.support.suite import FIXTURES

FIXTURE = FIXTURES / "master_cv.md"


def _inventory():
    return parse_cv(FIXTURE)


def _posting(*paragraphs: str, title: str = "Senior Product Analyst") -> str:
    return "\n".join((f"# {title}", "", *paragraphs))


def _dual_currency_parameters() -> JobSearchParameters:
    return JobSearchParameters(
        locations=[
            Location(
                key="us_remote",
                country="United States",
                aliases=["Remote - US", "Remote, United States"],
                eligibility=WorkEligibility.AUTHORIZED,
            ),
            Location(
                key="canada_remote",
                country="Canada",
                aliases=["Remote - Canada"],
                eligibility=WorkEligibility.AUTHORIZED,
            ),
        ],
        compensation=[
            CompensationBand(
                location="us_remote",
                currency="USD",
                basis="annual",
                minimum=Decimal(120000),
                target=Decimal(170000),
                stretch=Decimal(210000),
            ),
            CompensationBand(
                location="canada_remote",
                currency="CAD",
                basis="annual",
                minimum=Decimal(140000),
                target=Decimal(190000),
                stretch=Decimal(230000),
            ),
        ],
    )


def test_us_target_range_resolves_usd() -> None:
    reading = read(
        _posting(
            "Fully remote.",
            "The US target range of our base salary is $200,000 - $250,000.",
        ),
        _inventory(),
        parameters=_dual_currency_parameters(),
    )

    assert reading.pay_fit.stated
    assert reading.pay_fit.value is not None
    assert "USD" in reading.pay_fit.value
    assert not reading.pay_fit.value.startswith("shown: ")


def test_zone_table_aggregates_to_widest_range() -> None:
    parameters = JobSearchParameters(
        locations=[
            Location(
                key="us_remote",
                country="United States",
                aliases=["Remote, USA"],
                eligibility=WorkEligibility.AUTHORIZED,
            )
        ],
        compensation=[
            CompensationBand(
                location="us_remote",
                currency="USD",
                basis="annual",
                minimum=Decimal(150000),
                target=Decimal(210000),
                stretch=Decimal(270000),
            )
        ],
    )
    reading = read(
        _posting(
            "Remote, USA.",
            "Salary ranges, by geographic zone:",
            "Zone 1: $200,000 - $250,000",
            "Zone 2: $180,000 - $225,000",
            "Zone 3: $160,000 - $200,000",
        ),
        _inventory(),
        parameters=parameters,
    )

    assert reading.pay_fit.stated
    assert reading.pay_fit.value is not None
    assert "USD" in reading.pay_fit.value
    assert reading.pay_fit.evidence
    quoted = reading.pay_fit.evidence[0]
    assert quoted == "Zone 1: $200,000 - $250,000"


def test_per_jurisdiction_stanzas_split_on_one_line() -> None:
    parameters = _dual_currency_parameters()
    reading = read(
        _posting(
            "Remote in the United States.",
            "If performed in Illinois, the base pay range is $124,200 - $230,800."
            "If performed in Chicago, IL, the base pay range is $130,200 - $241,800.",
        ),
        _inventory(),
        parameters=parameters,
    )

    assert reading.pay_fit.stated
    assert reading.pay_fit.value is not None
    assert reading.pay_fit.evidence
    quoted = reading.pay_fit.evidence[0]
    assert "124,200" in quoted or "130,200" in quoted


def test_symbol_on_min_only_range() -> None:
    parameters = _dual_currency_parameters()
    reading = read(
        _posting(
            "Remote in the United States.",
            "Pay Range: $150,000 - 210,000",
        ),
        _inventory(),
        parameters=parameters,
    )

    assert reading.pay_fit.stated
    assert reading.pay_fit.value is not None
    assert "USD" in reading.pay_fit.value


def test_bare_pay_range_with_us_context() -> None:
    parameters = _dual_currency_parameters()
    reading = read(
        _posting(
            "Remote (US ONLY).",
            "Compensation Range",
            "$130,000 - $160,000 based on location.",
        ),
        _inventory(),
        parameters=parameters,
    )

    assert reading.pay_fit.stated
    assert reading.pay_fit.value is not None
    assert "USD" in reading.pay_fit.value


def test_monthly_stipend_does_not_contaminate_annual_basis() -> None:
    parameters = _dual_currency_parameters()
    reading = read(
        _posting(
            "Remote in the United States.",
            "Competitive salary: $215,000 - $245,000.",
            "Equity compensation package.",
            "Monthly $100 work from home expense stipend.",
        ),
        _inventory(),
        parameters=parameters,
    )

    assert reading.pay_fit.stated
    assert reading.pay_fit.value is not None
    assert "USD" in reading.pay_fit.value
    assert "annual" in reading.pay_fit.value
    assert "monthly" not in reading.pay_fit.value


def test_foreign_currency_shown_but_not_compared() -> None:
    parameters = _dual_currency_parameters()
    reading = read(
        _posting(
            "Remote across Europe.",
            "Base pay range: EUR 110,500 - 184,000 annually.",
        ),
        _inventory(),
        parameters=parameters,
    )

    assert reading.pay_fit.stated
    assert reading.pay_fit.value is not None
    assert reading.pay_fit.value.startswith("shown: EUR ")
    assert reading.pay_standing == EXTRACTED_ONLY_STANDING
    assert "add a EUR band" in reading.pay_fit.note


def test_user_story_dollar_amount_is_not_pay() -> None:
    parameters = _dual_currency_parameters()
    reading = read(
        _posting(
            "Remote in the United States.",
            "Our platform serves 40,000 users. We've raised $9M.",
            "Compensation range: $150,000 - $180,000.",
        ),
        _inventory(),
        parameters=parameters,
    )

    assert reading.pay_fit.stated
    assert reading.pay_fit.value is not None
    assert reading.pay_fit.evidence
    quoted = reading.pay_fit.evidence[0]
    assert "150,000" in quoted
    assert "9M" not in quoted


def _one_us_band(basis: CompensationBasis, minimum: int = 40) -> JobSearchParameters:
    return JobSearchParameters(
        locations=[
            Location(
                key="us_remote",
                country="United States",
                aliases=["Remote - US"],
                eligibility=WorkEligibility.AUTHORIZED,
            )
        ],
        compensation=[
            CompensationBand(
                location="us_remote", currency="USD", basis=basis, minimum=Decimal(minimum)
            )
        ],
    )


@pytest.mark.parametrize("heading", ["## New York pay range", "New York pay range:"])
def test_a_range_under_a_pay_heading_beats_an_earlier_benefit_amount(heading: str) -> None:
    reading = read(
        _posting(
            "Remote in the United States.",
            "",
            "## Benefits",
            "- 401(k) with company match (up to $3,000 annually)",
            "- Medical, dental, and vision coverage",
            "",
            heading,
            "$170,000-$250,000 USD",
        ),
        _inventory(),
        parameters=_dual_currency_parameters(),
    )

    assert reading.pay_fit.value == "at or above your USD annual floor"
    assert "170,000" in reading.pay_fit.evidence[0]


@pytest.mark.parametrize(
    "heading", ["Pay range", "PAY RANGE", "Salary", "Compensation", "Base Pay", "base pay"]
)
def test_a_figure_under_a_pay_heading_beats_one_under_no_pay_heading(heading: str) -> None:
    reading = read(
        _posting(
            "Remote in the United States.",
            "Relocation support of $5,000 is available for movers.",
            "",
            f"## {heading}",
            "$150,000 - $180,000",
        ),
        _inventory(),
        parameters=_dual_currency_parameters(),
    )

    assert "150,000" in reading.pay_fit.evidence[0]


@pytest.mark.parametrize(
    "line",
    [
        "401(k) with company match (up to $3,000 annually)",
        "401(k) match up to $3,000",
        "Annual learning stipend of $2,000",
        "An annual learning budget of USD 1,000",
        "- Meal vouchers: EUR 8 per day",
        "Lead smaller accounts (GBP 50,000-80,000 revenue) with support from the team",
        "A $2,000 stipend for home office equipment",
        "Signing bonus of $10,000",
        "Home office allowance of $1,500",
        "Tuition reimbursement up to $5,250 per year",
        "Wellness benefit of $1,200 each year",
        "Compensation includes a $2,000 learning stipend",
        "Compensation includes a 401(k) match up to $5,000",
        "Compensation includes a generous match up to $5,000",
        "Our compensation package includes a signing bonus of $10,000",
        "Pay includes an annual bonus of up to $15,000",
        "Meal allowance: EUR 8 per day",
    ],
)
def test_a_benefit_amount_is_never_pay(line: str) -> None:
    reading = read(
        _posting("Remote in the United States.", "", "## Perks", f"- {line}"),
        _inventory(),
        parameters=_dual_currency_parameters(),
    )

    assert reading.pay_fit.note == "the posting states no pay to compare"


@pytest.mark.parametrize(
    "line",
    [
        "Base salary: $150,000 - $180,000 plus an annual bonus of up to $20,000",
        "Signing bonus of $10,000. Base salary: $150,000 - $180,000",
        "$150,000 - $180,000 per year plus benefits",
        "$150,000 - $180,000 plus commission",
        "$150,000 - $180,000 + equity + bonus",
    ],
)
def test_a_bonus_beside_the_salary_on_one_line_is_not_the_pay(line: str) -> None:
    reading = read(
        _posting("Remote in the United States.", line),
        _inventory(),
        parameters=_dual_currency_parameters(),
    )

    assert reading.pay_fit.value == "at or above your USD annual floor"


@pytest.mark.parametrize(
    ("line", "shown"),
    [
        ("Pay: $3,148.20 bi-weekly", "shown: USD 81,853 annual"),
        ("Pay: $3,148.20 biweekly", "shown: USD 81,853 annual"),
        ("Pay: $3,148.20 paid every two weeks", "shown: USD 81,853 annual"),
        ("Pay: $3,000 - $3,500 bi-weekly", "shown: USD 78,000-91,000 annual"),
        ("Pay: $8,500 per month", "shown: USD 8,500 monthly"),
        ("Pay: $8,500 monthly", "shown: USD 8,500 monthly"),
        ("Pay: $80,000 per year, paid bi-weekly", "shown: USD 80,000 annual"),
        ("Pay: $3,200 per bi-weekly pay period", "shown: USD 83,200 annual"),
    ],
)
def test_a_stated_period_is_annualised_or_kept_with_its_period(line: str, shown: str) -> None:
    reading = read(
        _posting("Remote in the United States.", line),
        _inventory(),
        parameters=_one_us_band("hourly"),
    )

    assert reading.pay_fit.value == shown


@pytest.mark.parametrize(
    "line",
    [
        "Pay: $55 per hour",
        "Pay: $55 - $65 per hour",
        "Pay: $55 per hour, paid biweekly",
        "Pay: $3,500 bi-weekly, or $42 per hour",
        *(
            f"{lead}{figure}{unit}"
            for lead in ("Pay: ", "")
            for figure in ("$48.50 - $56.00", "USD 52", "48.50-56.00 USD", "US$48.50 to US$56.00")
            for unit in (
                " per hour",
                " per hr",
                " per Hr",
                " PER HOUR",
                " Per Hour",
                "/hr",
                "/HR",
                "/hour",
                " / hour",
                " / hr",
                " an hour",
                " hourly",
            )
        ),
    ],
)
def test_an_hourly_figure_is_kept_with_its_period(line: str) -> None:
    reading = read(
        _posting("Remote in the United States.", line),
        _inventory(),
        parameters=_one_us_band("hourly"),
    )

    assert reading.pay_fit.value == "at or above your USD hourly floor"


@pytest.mark.parametrize(
    ("lines", "currency", "low", "high"),
    [
        (
            (
                "- Minimum Salary: $31.64",
                "- Maximum Salary: $54.36",
                "- Hours per Shift: 7.75",
                "- FTE: 1.00",
            ),
            "USD",
            "31.64",
            "54.36",
        ),
        (("Minimum salary: USD 31.64", "Maximum salary: USD 54.36"), "USD", "31.64", "54.36"),
        (("Budgeted Range", "$68.60-$76.97 CAD"), "CAD", "68.60", "76.97"),
        (("BUDGETED RANGE:", "CAD 68.60 \u2013 76.97"), "CAD", "68.60", "76.97"),
        (("Salary : $64.12 - $74.93", "FTE: 0.8"), "USD", "64.12", "74.93"),
        (("Salary: USD 64.12-74.93",), "USD", "64.12", "74.93"),
        (("The salary range for this position is $20.70 to $31.04",), "USD", "20.70", "31.04"),
        (("Base Pay Range for this position $17.33 - $28.88",), "USD", "17.33", "28.88"),
        (("base pay range: $17.33\u2013$28.88",), "USD", "17.33", "28.88"),
        (
            ("Pay range based on experience and location: $36.15-$48.88",),
            "USD",
            "36.15",
            "48.88",
        ),
        (("Pay Range: $36 to $48",), "USD", "36", "48"),
        (("Compensation range: $19.46 - $28.55",), "USD", "19.46", "28.55"),
        # A country-only reader takes the Primary tier over one naming the country.
        (
            (
                "Primary Location Base Pay Range: $53 - $72",
                "Other US Location(s) Base Pay Range: $45 - $61",
            ),
            "USD",
            "53",
            "72",
        ),
    ],
)
def test_a_labelled_range_under_two_hundred_reads_hourly(
    lines: tuple[str, ...], currency: str, low: str, high: str
) -> None:
    reading = read(
        _posting("Remote in the United States.", "", "## About the role", *lines),
        _inventory(),
        parameters=_one_us_band("hourly", minimum=15),
    )

    [pay] = reading.pay_extractions
    assert (pay.currency, pay.amount_min, pay.amount_max, pay.basis) == (
        currency,
        Decimal(low),
        Decimal(high),
        "hourly",
    )


@pytest.mark.parametrize(
    ("primary_place", "metro", "low", "high"),
    [
        ("Austin, TX", "Denver", "45", "61"),
        ("Austin, TX", "Austin", "53", "72"),
        ("Austin, TX", "", "53", "72"),
        (None, "Denver", "53", "72"),
    ],
)
def test_the_primary_tier_holds_unless_the_reader_is_elsewhere(
    primary_place: str | None, metro: str, low: str, high: str
) -> None:
    stated = (f"Primary Location: {primary_place}",) if primary_place else ()
    parameters = JobSearchParameters(
        locations=[
            Location(
                key="us_home",
                country="United States",
                metro=metro,
                eligibility=WorkEligibility.AUTHORIZED,
            )
        ],
        compensation=[
            CompensationBand(
                location="us_home", currency="USD", basis="hourly", minimum=Decimal(15)
            )
        ],
    )
    reading = read(
        _posting(
            "Hybrid in the United States.",
            *stated,
            "",
            "## About the role",
            "Primary Location Base Pay Range: $53 - $72",
            "Other US Location(s) Base Pay Range: $45 - $61",
        ),
        _inventory(),
        parameters=parameters,
    )

    [pay] = reading.pay_extractions
    assert (pay.amount_min, pay.amount_max) == (Decimal(low), Decimal(high))


@pytest.mark.parametrize(
    ("country", "currency", "tier", "low", "high"),
    [
        ("Canada", "CAD", "Canada Base Pay Range: CAD 60 - 80", "60", "80"),
        ("United Kingdom", "GBP", "United Kingdom Base Pay Range: GBP 50 - 70", "50", "70"),
        ("Canada", "CAD", "Canada Base Pay Range: $60 - $80", "60", "80"),
        ("Canada", "CAD", "Other Locations in Canada Base Pay Range: CAD 60 - 80", "60", "80"),
    ],
)
def test_a_tier_for_another_country_beats_the_primary_tier(
    country: str, currency: str, tier: str, low: str, high: str
) -> None:
    parameters = JobSearchParameters(
        locations=[Location(key="home", country=country, eligibility=WorkEligibility.AUTHORIZED)],
        compensation=[
            CompensationBand(
                location="home", currency=currency, basis="hourly", minimum=Decimal(15)
            )
        ],
    )
    reading = read(
        _posting(
            f"Hybrid in the United States or {country}.",
            "",
            "## About the role",
            "Primary Location Base Pay Range: $53 - $72",
            tier,
        ),
        _inventory(),
        parameters=parameters,
    )

    [pay] = reading.pay_extractions
    assert (pay.currency, pay.amount_min, pay.amount_max) == (currency, Decimal(low), Decimal(high))


@pytest.mark.parametrize(
    "lines",
    [
        ("Compensation: $20-$30 per patient session", "20-30 hours per week"),
        ("Pay range: $20 - $30 per session",),
        ("Salary range: $60 - $90 per visit",),
        ("Pay range: $40 - $55 per case",),
        ("Pay range: $55 - $80 per 15-minute consult",),
        ("Salary range: $95 - $125 thousand",),
        ("Salary range: USD 80 - 100 k",),
        ("Salary: $3 - $5 million in ARR responsibility",),
    ],
)
def test_a_labelled_range_per_other_unit_or_scaled_is_not_hourly(lines: tuple[str, ...]) -> None:
    reading = read(
        _posting("Remote in the United States.", *lines),
        _inventory(),
        parameters=_one_us_band("hourly", minimum=15),
    )

    assert [pay.basis for pay in reading.pay_extractions if pay.basis == "hourly"] == []


@pytest.mark.parametrize(
    "line",
    [
        "Compensation range $19.46 - $28.55 hourly rate",
        "$22.00 - $25.00 / hour",
    ],
)
def test_a_range_stating_its_hourly_period_still_reads_hourly(line: str) -> None:
    reading = read(
        _posting("Remote in the United States.", line),
        _inventory(),
        parameters=_one_us_band("hourly", minimum=15),
    )

    assert reading.pay_fit.value == "at or above your USD hourly floor"


@pytest.mark.parametrize(
    "line",
    [
        "A $250 sign-on bonus for new hires",
        "A $150 stipend for home office equipment",
        "Pay range: $300 - $450",
        "Requires 3-5 years of experience",
        "Join a team of $20 - $30 donors",
        "The potential base pay range for this role is $0.00 - $0.00",
        "The potential base pay range for this role, when annualized, is $85 - $120",
        "Salary range is discussed at interview. Gift cards worth $25 - $50 for referrals.",
        "Gift cards worth $25 - $50; see the salary range at interview",
    ],
)
def test_an_unlabelled_or_large_small_figure_is_still_no_pay(line: str) -> None:
    reading = read(
        _posting("Remote in the United States.", line),
        _inventory(),
        parameters=_one_us_band("hourly", minimum=15),
    )

    assert reading.pay_extractions == ()


def test_a_small_range_before_the_salary_range_leaves_it_annual() -> None:
    reading = read(
        _posting(
            "Remote in the United States.", "Parking $50 - $100; salary range $80,000 - $100,000"
        ),
        _inventory(),
        parameters=_one_us_band("annual"),
    )

    [pay] = reading.pay_extractions
    assert (pay.amount_min, pay.amount_max, pay.basis) == (
        Decimal(80000),
        Decimal(100000),
        "annual",
    )


@pytest.mark.parametrize(
    "line",
    [
        "Compensation: $400 wellness stipend; you report to an HR director",
        "Pay: $45 - $55 for an HR coordinator",
        "Compensation: $400 wellness stipend, paid per HR policy",
    ],
)
def test_an_hr_role_name_is_no_hourly_period(line: str) -> None:
    reading = read(
        _posting("Remote in the United States.", line),
        _inventory(),
        parameters=_one_us_band("hourly"),
    )

    assert reading.pay_fit.value is None


@pytest.mark.parametrize(
    ("currency", "differential", "first"),
    [
        (currency, differential, first)
        for currency in ("$", "USD ")
        for differential in (
            "shift differential of {currency}2.75 per hour",
            "{currency}2.75 per hour shift-differential",
            "Shift differential pay: {currency}2.75/hour",
        )
        for first in (False, True)
    ],
)
def test_a_shift_differential_does_not_supply_the_base_pay_period(
    currency: str, differential: str, first: bool
) -> None:
    base = f"Starting pay is {currency}23.50"
    supplement = differential.format(currency=currency)
    line = f"{supplement}, plus {base}" if first else f"{base}, plus {supplement}"
    reading = read(
        _posting("Remote in the United States.", line),
        _inventory(),
        parameters=_one_us_band("hourly"),
    )

    assert reading.pay_extractions == ()
    assert not reading.pay_fit.stated


@pytest.mark.parametrize(
    ("currency", "differential", "amount", "first", "maximum", "separator"),
    [
        (currency, differential, amount, first, maximum, separator)
        for currency in ("$", "USD ")
        for differential in (
            "shift differential of {amount} per hour",
            "{amount} per hour shift-differential",
            "Shift differential pay: {amount}/hour",
        )
        for amount in ("$2.75", "$42.00")
        for first in (False, True)
        for maximum in (None, "35.75")
        for separator in ("-", "to", "\u2013", "\u2014")
        if maximum is not None or separator == "-"
    ],
)
def test_a_shift_differential_does_not_change_explicit_base_pay(
    currency: str,
    differential: str,
    amount: str,
    first: bool,
    maximum: str | None,
    separator: str,
) -> None:
    base = f"Base pay: {currency}23.50"
    if maximum is not None:
        base += f" {separator} {currency}{maximum}"
    base += " per hour"
    supplement = differential.format(amount=amount)
    line = f"{supplement}, plus {base}" if first else f"{base}, plus {supplement}"
    reading = read(
        _posting("Remote in the United States.", line),
        _inventory(),
        parameters=_one_us_band("hourly"),
    )

    (stored,) = reading.pay_extractions
    assert (stored.amount_min, stored.amount_max, stored.currency, stored.basis) == (
        Decimal("23.50"),
        Decimal(maximum or "23.50"),
        "USD",
        "hourly",
    )
    assert reading.pay_fit.value == "below your USD hourly floor"


@pytest.mark.parametrize(
    "line",
    [
        differential.format(amount=amount)
        for amount in ("$2.75", "2.75 USD")
        for differential in (
            "shift differential of {amount} per hour",
            "{amount} per hour shift-differential",
            "Shift differential pay: {amount}/hour",
        )
    ],
)
def test_a_shift_differential_alone_is_not_base_pay(line: str) -> None:
    reading = read(
        _posting("Remote in the United States.", line),
        _inventory(),
        parameters=_one_us_band("hourly"),
    )

    assert reading.pay_extractions == ()
    assert not reading.pay_fit.stated


@pytest.mark.parametrize(
    "line",
    [
        "Base pay: $23.50 per hour; shift differential may apply.",
        "Base pay: $23.50 per hour, plus a shift differential based on schedule.",
        "A shift differential may apply; base pay: $23.50 per hour.",
    ],
)
def test_an_unpriced_shift_differential_keeps_the_explicit_base_pay(line: str) -> None:
    reading = read(
        _posting("Remote in the United States.", line),
        _inventory(),
        parameters=_one_us_band("hourly"),
    )

    (stored,) = reading.pay_extractions
    assert (stored.amount_min, stored.amount_max, stored.currency, stored.basis) == (
        Decimal("23.50"),
        Decimal("23.50"),
        "USD",
        "hourly",
    )
    assert reading.pay_fit.value == "below your USD hourly floor"


@pytest.mark.parametrize("decimal_separator", [".", ". "])
def test_a_full_posting_with_only_ambiguous_base_pay_keeps_pay_unknown(
    decimal_separator: str,
) -> None:
    posting = _posting(
        "Remote in the United States.",
        "The role works an evening schedule.",
        f"Starting pay is ${'23.50'.replace('.', decimal_separator)}, plus a "
        f"shift differential of ${'2.75'.replace('.', decimal_separator)} per hour.",
        "Build and maintain product reports for customer teams.",
        "Collaborate with colleagues on review and planning.",
        "## Compensation",
        f"Base pay range: ${'23.50'.replace('.', decimal_separator)} - "
        f"${'35.75'.replace('.', decimal_separator)}",
        "",
        "The range reflects hourly or annual pay depending on the position.",
        "Benefits include health coverage and paid leave.",
    )
    reading = read(posting, _inventory(), parameters=_one_us_band("hourly"))

    assert reading.pay_extractions == ()
    assert not reading.pay_fit.stated


@pytest.mark.parametrize(
    ("line", "shown"),
    [
        (
            "Salary Range $91,600 — $154,200 USD Benefits to support your health and "
            "wellbeing include medical coverage.",
            "shown: USD 91,600-154,200 annual",
        ),
        (
            "Compensation: $29,200.00 - $48,900.00 USD This role is eligible for Bonus.",
            "shown: USD 29,200-48,900 ",
        ),
        (
            "The annual base pay (or hourly rate of compensation) for this position ranges "
            "from $ 142,171.20 - $213,256.80 USD Annual Our positions offer eligibility for "
            "incentives such as short-term bonuses.",
            "shown: USD 142,171-213,257 annual",
        ),
        (
            "Bay Area: $164,000 — $222,000 USD All Other US Locations: $143,000 — $193,000 USD",
            "shown: USD 143,000-193,000 annual",
        ),
        (
            "$100,000+ total compensation at target. A $750 per month car allowance is "
            "provided. The potential base pay range for this role is $55,000-60,000 annually.",
            "shown: USD 55,000-60,000 annual",
        ),
    ],
)
def test_a_benefit_word_in_a_later_sentence_leaves_the_salary_read(line: str, shown: str) -> None:
    reading = read(
        _posting("Remote in the United States.", line),
        _inventory(),
        parameters=_one_us_band("hourly"),
    )

    assert reading.pay_fit.value is not None
    assert reading.pay_fit.value.startswith(shown)


@pytest.mark.parametrize(
    ("line", "low", "high"),
    [
        ("Salary: $80,000, paid bi-weekly", 80_000, 80_000),
        ("Salary range: $80,000 - $95,000. Employees are paid bi-weekly.", 80_000, 95_000),
        ("Salary Range $91,600 - $154,200 Benefits to support your wellbeing", 91_600, 154_200),
        ("Pay range: $120,000 - $150,000 Medical benefits start day one.", 120_000, 150_000),
        ("Great benefits and $120,000 - $150,000 annually", 120_000, 150_000),
        (
            "Target cash compensation for this role, including base salary and bonus, "
            "is $140,000 - $175,000 annually",
            140_000,
            175_000,
        ),
        (
            "Compensation for Level 3, including bonus target, is $150,000 annually",
            150_000,
            150_000,
        ),
    ],
)
def test_a_salary_beside_a_pay_frequency_or_benefits_is_stored_as_written(
    line: str, low: int, high: int
) -> None:
    reading = read(
        _posting("Remote in the United States.", line),
        _inventory(),
        parameters=_dual_currency_parameters(),
    )

    (stored,) = reading.pay_extractions
    assert (stored.amount_min, stored.amount_max, stored.basis) == (
        Decimal(low),
        Decimal(high),
        "annual",
    )


@pytest.mark.parametrize(
    ("line", "low", "high", "basis"),
    [
        ("Salary $95,000 or $3,653.85 biweekly", 95_000, 95_000, "annual"),
        ("Salary: $3,653.85 bi-weekly or $95,000 per year", 95_000, 95_000, "annual"),
        (
            "$3,148.20 to $4,126.82 bi-weekly ($82,168 - $107,710/year)",
            82_168,
            107_710,
            "annual",
        ),
        (
            "Pay: $3,148.20 - $4,126.82 biweekly, or $82,168 - $107,710 per year",
            82_168,
            107_710,
            "annual",
        ),
        (
            "Base salary USD 90,000 - 110,000 annually, plus a monthly expense budget of USD 750",
            90_000,
            110_000,
            "annual",
        ),
        (
            "Base salary USD 7,500 - 9,000 per month, plus an annual learning stipend of USD 1,000",
            7_500,
            9_000,
            "monthly",
        ),
        ("Salary: $7,500 - $9,000 monthly. Eligible for an annual bonus.", 7_500, 9_000, "monthly"),
        (
            "Annual base salary: $90,000 - $110,000, plus a $750 monthly phone allowance",
            90_000,
            110_000,
            "annual",
        ),
        ("The hourly range for this role is between $28.00 and $34.00", 28, 34, "hourly"),
        (
            "The hourly range for this role is between $28.00 and $34.00, reviewed annually",
            28,
            34,
            "hourly",
        ),
        ("Salary: 120,000 - 150,000 USD", 120_000, 150_000, "annual"),
        ("Salary: 120,000 USD - 150,000 USD", 120_000, 150_000, "annual"),
        ("Salary: USD 120,000 - 150,000", 120_000, 150_000, "annual"),
        ("$120,000 - $150,000 USD", 120_000, 150_000, "annual"),
    ],
)
def test_each_figure_is_stored_with_the_period_and_currency_written_beside_it(
    line: str, low: int, high: int, basis: CompensationBasis
) -> None:
    reading = read(
        _posting("Remote in the United States.", line),
        _inventory(),
        parameters=_dual_currency_parameters(),
    )

    (stored,) = reading.pay_extractions
    assert (stored.amount_min, stored.amount_max, stored.currency, stored.basis) == (
        Decimal(low),
        Decimal(high),
        "USD",
        basis,
    )


@pytest.mark.parametrize(
    ("line", "floor", "verdict"),
    [
        ("Salary: 120,000 - 150,000 USD", 130_000, "below your USD annual floor"),
        ("Salary: USD 120,000 - 150,000", 130_000, "below your USD annual floor"),
        (
            "$3,148.20 to $4,126.82 bi-weekly ($82,168 - $107,710/year)",
            80_000,
            "at or above your USD annual floor",
        ),
        ("Salary $95,000 or $3,653.85 biweekly", 95_001, "below your USD annual floor"),
    ],
)
def test_the_floor_is_compared_with_the_annual_figure_the_line_states(
    line: str, floor: int, verdict: str
) -> None:
    reading = read(
        _posting("Remote in the United States.", line),
        _inventory(),
        parameters=_one_us_band("annual", floor),
    )

    assert reading.pay_fit.value == verdict


@pytest.mark.parametrize(
    "paragraphs",
    [
        (
            "The starting annual base pay for this role is listed below.",
            "Remote US: $150,000 - $185,000 USD. We expect you to use AI in your daily work.",
        ),
        (
            "The starting annual base pay for this role is listed below.",
            "Remote US: $150,000 - $185,000 USD. You will work with sales on a day-to-day basis.",
        ),
        (
            "The annual base pay for this position ranges from $195,000 to $293,000. Hourly and "
            "salaried non-exempt employees will also be paid overtime.",
        ),
        (
            "The annual base pay (or hourly rate of compensation) for this position ranges from "
            "$116,710 to $175,064.",
        ),
    ],
)
def test_a_period_no_salary_of_that_size_could_have_is_not_its_period(
    paragraphs: tuple[str, ...],
) -> None:
    reading = read(
        _posting("Remote in the United States.", *paragraphs),
        _inventory(),
        parameters=_one_us_band("annual", 100_000),
    )

    assert reading.pay_fit.value == "at or above your USD annual floor"


@pytest.mark.parametrize(
    "line", ["Rate: $28, paid bi-weekly", "Pay: $25.50 - $30.00, paid biweekly"]
)
def test_an_hourly_rate_paid_bi_weekly_is_never_read_as_annual_pay(line: str) -> None:
    reading = read(
        _posting("Remote in the United States.", line),
        _inventory(),
        parameters=_dual_currency_parameters(),
    )

    assert reading.pay_fit.value is None
    assert reading.pay_extractions == ()


def test_a_zone_table_stores_the_top_of_every_zone() -> None:
    reading = read(
        _posting(
            "Remote in the United States.",
            "Salary ranges, by geographic zone:",
            "Zone 1: $160,000 - $200,000",
            "Zone 2: $180,000 - $225,000",
            "Zone 3: $200,000 - $250,000",
        ),
        _inventory(),
        parameters=_dual_currency_parameters(),
    )

    (stored,) = reading.pay_extractions
    assert (stored.amount_min, stored.amount_max) == (Decimal(160_000), Decimal(250_000))
    assert stored.description == "Zone 1: $160,000 - $200,000"


def test_a_minimum_maximum_pair_stores_and_quotes_the_source_line() -> None:
    reading = read(
        _posting(
            "Remote in the United States.",
            "",
            "## About the role",
            "- Minimum Salary: $31.64",
            "- Maximum Salary: $54.36",
        ),
        _inventory(),
        parameters=_one_us_band("hourly", minimum=15),
    )

    [pay] = reading.pay_extractions
    assert (pay.amount_min, pay.amount_max, pay.basis) == (
        Decimal("31.64"),
        Decimal("54.36"),
        "hourly",
    )
    assert pay.description is not None
    assert "Minimum Salary: $31.64" in pay.description
    assert "(range:" not in pay.description
    assert all("(range:" not in quote for quote in reading.pay_fit.evidence)


def test_a_sign_on_bonus_in_the_title_is_not_the_pay() -> None:
    reading = read(
        _posting(
            "Remote in the United States.",
            "Pay: $80,000 - $120,000 per year",
            title="Care Coordinator - $5k SIGN ON BONUS",
        ),
        _inventory(),
        parameters=_one_us_band("hourly"),
    )

    assert reading.pay_fit.value == "shown: USD 80,000-120,000 annual"


_LISTINGS = {
    "us": "Austin, Texas, United States",
    "canada": "Toronto, Ontario, Canada",
    "unplaced": "",
}


def _toronto_and_us_bands(*currencies: str) -> JobSearchParameters:
    """Toronto and US places, with a band in each named currency for its own country."""
    homes = {"CAD": "toronto", "USD": "us"}
    return JobSearchParameters(
        locations=[
            Location(key="toronto", country="Canada"),
            Location(key="us", country="United States"),
        ],
        countries=[CountryNames(country="Canada"), CountryNames(country="United States")],
        compensation=[
            CompensationBand(location=homes[currency], currency=currency, minimum=Decimal(100000))
            for currency in currencies
        ],
    )


def _dollar_reading(pay: str, listing: str, *bands: str) -> tuple[tuple[str, ...], str | None]:
    reading = read(
        _posting(f"Compensation: {pay} per year.", "Own the ingestion platform."),
        parameters=_toronto_and_us_bands(*bands),
        listing_location=_LISTINGS[listing],
    )
    return tuple(stored.currency for stored in reading.pay_extractions), reading.pay_fit.value


@pytest.mark.parametrize("bands", [("CAD",), ("USD",), ("CAD", "USD")])
@pytest.mark.parametrize("listing", list(_LISTINGS))
@pytest.mark.parametrize(
    ("pay", "currency"),
    [
        ("$120,000 - $150,000 USD", "USD"),
        ("$120,000 - $150,000 CAD", "CAD"),
        ("C$120,000 - C$150,000", "CAD"),
        ("US$120,000 - US$150,000", "USD"),
    ],
)
def test_a_dollar_the_posting_marks_keeps_its_currency_wherever_it_is_listed(
    pay: str, currency: str, listing: str, bands: tuple[str, ...]
) -> None:
    stored, verdict = _dollar_reading(pay, listing, *bands)

    assert stored == (currency,)
    assert verdict is not None
    assert currency in verdict


@pytest.mark.parametrize("bands", [("CAD",), ("USD",), ("CAD", "USD")])
@pytest.mark.parametrize(("listing", "currency"), [("us", "USD"), ("canada", "CAD")])
def test_a_bare_dollar_takes_the_listed_country_before_the_persons_bands(
    listing: str, currency: str, bands: tuple[str, ...]
) -> None:
    stored, verdict = _dollar_reading("$120,000 - $150,000", listing, *bands)

    assert stored == (currency,)
    assert verdict is not None
    assert currency in verdict


@pytest.mark.parametrize(
    ("lines", "listing"),
    [
        (("Compensation: US$120,000 - US$150,000 per year.",), ""),
        (("Remote (US only).", "Compensation: $120,000 - $150,000 per year."), ""),
        (("Compensation: $120,000 - $150,000 per year.",), _LISTINGS["us"]),
    ],
    ids=["us-prefix", "us-hint-in-body", "us-listing"],
)
def test_a_us_signal_beats_cad_bands_when_the_person_declared_no_us_place(
    lines: tuple[str, ...], listing: str
) -> None:
    parameters = JobSearchParameters(
        locations=[Location(key="toronto", country="Canada")],
        countries=[CountryNames(country="Canada")],
        compensation=[
            CompensationBand(location="toronto", currency="CAD", minimum=Decimal(100000))
        ],
    )

    reading = read(_posting(*lines), parameters=parameters, listing_location=listing)

    assert [stored.currency for stored in reading.pay_extractions] == ["USD"]
    assert reading.pay_fit.value == "shown: USD 120,000-150,000 annual"


@pytest.mark.parametrize(
    ("bands", "stored"),
    [(("CAD",), ("CAD",)), (("USD",), ("USD",)), (("CAD", "USD"), ())],
)
def test_a_bare_dollar_on_an_unplaced_posting_takes_the_one_currency_the_bands_use(
    bands: tuple[str, ...], stored: tuple[str, ...]
) -> None:
    """With no country anywhere in the posting, only a single-currency band settles it."""
    assert _dollar_reading("$120,000 - $150,000", "unplaced", *bands)[0] == stored
