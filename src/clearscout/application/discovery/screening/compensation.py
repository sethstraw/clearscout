"""Stated pay findings against the saved compensation floor."""

from __future__ import annotations

import re
from decimal import Decimal

from clearscout.application.discovery.screening.findings import (
    ScreeningCode,
    ScreeningDimension,
    ScreeningFinding,
    ScreeningStanding,
)
from clearscout.domain.search_parameters import (
    JobSearchParameters,
)
from clearscout.domain.staged_facts import PublishedFact
from clearscout.shared.quantities import (
    figures,
)
from clearscout.shared.text import sanitize

COMPENSATION_CURRENCY = re.compile(r"\b([A-Z]{3})\b")


COMPENSATION_BASES = {
    "annual": "annual",
    "annually": "annual",
    "annum": "annual",
    "year": "annual",
    "yearly": "annual",
    "yr": "annual",
    "monthly": "monthly",
    "month": "monthly",
    "daily": "daily",
    "day": "daily",
    "hourly": "hourly",
    "hour": "hourly",
    "hr": "hourly",
}


def _compensation_value(value: str) -> tuple[str, str, Decimal] | None:
    """Read an explicit currency, period, and maximum amount from one value."""
    text = sanitize(value)
    currencies = set(COMPENSATION_CURRENCY.findall(text))
    bases = {
        basis
        for token, basis in COMPENSATION_BASES.items()
        if re.search(rf"\b{token}\b", text, re.IGNORECASE)
    }
    amounts = [figure.high for figure in figures(text)]
    if len(currencies) != 1 or len(bases) != 1 or not amounts:
        return None
    return next(iter(currencies)), next(iter(bases)), max(amounts)


def compensation_finding(
    fact: PublishedFact, parameters: JobSearchParameters, location_key: str | None
) -> ScreeningFinding | None:
    """Screen a published maximum against a band minimum, or show stated pay when none has one."""
    if not parameters.compensation:
        return None
    if not any(
        band.minimum is not None
        for band in (
            parameters.bands_for(location_key) if location_key else parameters.compensation
        )
    ):
        return _stated_pay_finding(fact, parameters, location_key)
    stated = "; ".join(fact.values)
    unknown = ScreeningFinding(
        ScreeningDimension.COMPENSATION,
        ScreeningStanding.UNKNOWN,
        ScreeningCode.COMPENSATION_UNKNOWN,
        stated,
    )
    if not fact.complete or location_key is None:
        return unknown
    parsed = tuple(_compensation_value(value) for value in fact.values)
    if not parsed or any(value is None for value in parsed):
        return unknown
    bands = parameters.bands_for(location_key)
    comparisons = [
        maximum >= band.minimum
        for parsed_value in parsed
        if parsed_value is not None
        for currency, basis, maximum in (parsed_value,)
        for band in bands
        if band.minimum is not None and band.currency == currency and band.basis == basis
    ]
    if not comparisons:
        standing, code = ScreeningStanding.UNKNOWN, ScreeningCode.COMPENSATION_UNKNOWN
    elif any(comparisons):
        standing, code = ScreeningStanding.MATCH, ScreeningCode.COMPENSATION_MATCH
    else:
        standing, code = ScreeningStanding.REFUSE, ScreeningCode.COMPENSATION_BELOW_MINIMUM
    return ScreeningFinding(ScreeningDimension.COMPENSATION, standing, code, stated)


def _stated_pay_finding(
    fact: PublishedFact, parameters: JobSearchParameters, location_key: str | None
) -> ScreeningFinding | None:
    """Show stated pay and whether its top reaches the place's target; refuse nothing."""
    if not fact.values:
        return None
    parsed = tuple(_compensation_value(value) for value in fact.values)
    reached = [
        band.target_met(value[2])
        for band in (parameters.bands_for(location_key) if location_key else ())
        if band.target is not None
        for value in parsed
        if value is not None and (band.currency, band.basis) == value[:2]
    ]
    code = (
        ScreeningCode.COMPENSATION_STATED
        if not reached
        else ScreeningCode.COMPENSATION_TARGET_MET
        if any(reached)
        else ScreeningCode.COMPENSATION_BELOW_TARGET
    )
    return ScreeningFinding(
        ScreeningDimension.COMPENSATION, ScreeningStanding.MATCH, code, "; ".join(fact.values)
    )
