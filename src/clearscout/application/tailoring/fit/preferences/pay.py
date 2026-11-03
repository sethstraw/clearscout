"""Pay fit: a posting's pay compared with the person's bands."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from clearscout.application.contracts import CompensationInput
from clearscout.application.tailoring.fit.lexicon import (
    COUNTRY_CURRENCY,
    DOLLAR_PREFIX,
    DOLLAR_PREFIX_CURRENCIES,
    country_hint,
)
from clearscout.application.tailoring.fit.preferences.pay_lines import (
    dollar_currency,
    extracted_only,
    line_maximum,
    quoted_pay,
)
from clearscout.application.tailoring.fit.structure import (
    Answer,
    Block,
    content_lines,
    quote_line,
    unasked_answer,
)
from clearscout.domain.search_parameters import (
    CompensationBand,
    CompensationBasis,
    JobSearchParameters,
    basis_for,
)


def _compatible_pay_bands(
    bands: Sequence[CompensationBand],
    currency: str,
    amount: Decimal,
    basis: CompensationBasis | None,
) -> tuple[list[CompensationBand], CompensationBasis | None, list[CompensationBand]]:
    """Return same-currency bands, the resolved basis, and matching bands."""
    same_currency = [band for band in bands if band.currency == currency]
    resolved_basis = basis if basis is not None else basis_for(amount, same_currency)
    matching = [band for band in same_currency if band.basis == resolved_basis]
    return same_currency, resolved_basis, matching


def pay_fit(
    blocks: tuple[Block, ...],
    wanted: JobSearchParameters,
    place: str | None = None,
    *,
    listing: str,
) -> tuple[Answer[str], int | None, bool, str]:
    """Compare pay, and say where in the band it falls, from 0 to 100."""
    question = "Pay against your band"
    if not wanted.compensation:
        return unasked_answer(question), None, False, ""
    quoted = quoted_pay(blocks, wanted)
    if quoted is None:
        return Answer(question, note="the posting states no pay to compare"), None, False, ""
    currency, amount, basis, line, source = quoted
    maximum = line_maximum(line, amount)
    if currency == "$":
        resolved = _resolved_dollar_currency(blocks, wanted, place, line, listing)
        if not resolved:
            return (
                Answer(
                    question,
                    note="the posting quotes a bare dollar sign and names no country "
                    "beside it, so which dollar it is stays open",
                ),
                None,
                False,
                "",
            )
        currency = resolved
    return _compare_pay_to_bands(
        wanted,
        place,
        question=question,
        currency=currency,
        amount=amount,
        maximum=maximum,
        basis=basis,
        line=source,
    )


def _resolved_dollar_currency(
    blocks: tuple[Block, ...],
    wanted: JobSearchParameters,
    place: str | None,
    line: str,
    listing: str,
) -> str | None:
    """Return which dollar a bare sign means, from the posting before the person's bands."""
    marker = DOLLAR_PREFIX.search(line)
    if marker is not None:
        return DOLLAR_PREFIX_CURRENCIES[marker.group(1)]
    located = wanted.location(place) if place is not None else None
    return (
        _country_currency(wanted, wanted.country_named_in(line))
        or _country_currency(wanted, country_hint(line))
        or (_country_currency(wanted, located.country) if located is not None else "")
        or _country_currency(wanted, wanted.country_named_in(listing))
        or _country_currency(wanted, country_hint(listing))
        or _country_currency(wanted, country_hint("\n".join(content_lines(blocks))))
        or dollar_currency(wanted, place)
    )


def _country_currency(wanted: JobSearchParameters, country: str) -> str:
    """Return the currency a country pays in, else the one the person's bands use there."""
    return COUNTRY_CURRENCY.get(country_hint(country), "") or wanted.currency_in(country)


def _compare_pay_to_bands(
    wanted: JobSearchParameters,
    place: str | None,
    *,
    question: str,
    currency: str,
    amount: Decimal,
    maximum: Decimal,
    basis: CompensationBasis | None,
    line: str,
) -> tuple[Answer[str], int | None, bool, str]:
    bands = wanted.bands_for(place) if place is not None else wanted.compensation
    if not bands:
        return extracted_only(
            question,
            currency,
            amount,
            maximum,
            basis or "annual",
            line,
            note=f"no band declared for {place}",
        )
    same_currency, basis, matching = _compatible_pay_bands(bands, currency, amount, basis)
    if basis is None:
        return extracted_only(
            question,
            currency,
            amount,
            maximum,
            "annual",
            line,
            note="no pay period stated; annual assumed",
        )
    if not same_currency:
        listed = ", ".join(sorted({band.currency for band in bands}))
        return extracted_only(
            question,
            currency,
            amount,
            maximum,
            basis,
            line,
            note=f"your bands are in {listed}; add a {currency} band to compare",
        )

    if not matching:
        listed = ", ".join(sorted({band.basis for band in same_currency}))
        return extracted_only(
            question,
            currency,
            amount,
            maximum,
            basis,
            line,
            note=f"your {currency} bands are {listed}; add a {currency} {basis} band to compare",
        )

    stated = [band for band in matching if band.minimum is not None]
    if not stated:
        return extracted_only(
            question,
            currency,
            amount,
            maximum,
            basis,
            line,
            note=f"your {currency} {basis} bands state no minimum",
        )
    band = min(stated, key=lambda item: item.minimum or Decimal(0))
    standing = band.standing(amount)
    floor = band.minimum or Decimal(0)
    verdict = "at or above" if amount >= floor else "below"
    above_stretch = f"{currency} {basis}" if band.above_stretch(amount) else ""
    note = f"the posting states this {currency} {basis} pay range"
    if above_stretch:
        note += f", and its floor is above your {above_stretch} stretch figure"
    return (
        Answer(
            question,
            f"{verdict} your {currency} {basis} floor",
            evidence=(quote_line(line),),
            note=note,
        ),
        standing,
        band.target_met(amount),
        above_stretch,
    )


def pay_extractions(
    blocks: tuple[Block, ...],
    wanted: JobSearchParameters,
    place: str | None,
    geography: str,
    *,
    listing: str,
) -> tuple[CompensationInput, ...]:
    """Return the compensation range the posting quotes, for persistence.

    Runs the same picker as :func:`pay_fit` so the stored range and the fit
    verdict cite the same line. Emits nothing when the picker resolves nothing.
    """
    quoted = quoted_pay(blocks, wanted)
    if quoted is None:
        return ()
    currency, minimum, basis, line, source = quoted
    if currency == "$":
        resolved = _resolved_dollar_currency(blocks, wanted, place, line, listing)
        if not resolved:
            return ()
        currency = resolved
    maximum = line_maximum(line, minimum)
    return (
        CompensationInput(
            component_type="base",
            amount_min=minimum,
            amount_max=maximum,
            currency=currency,
            basis=basis or "annual",
            geography=geography[:300],
            description=quote_line(source),
        ),
    )
