"""Location findings from the posting's stated places."""

from __future__ import annotations

import re

from clearscout.application.discovery.screening.arrangement_terms import (
    accepts_all,
    arrangement_value,
)
from clearscout.application.discovery.screening.findings import (
    ScreeningCode,
    ScreeningDimension,
    ScreeningFinding,
    ScreeningStanding,
)
from clearscout.application.discovery.screening.place_signals import (
    read_place_signals,
    sibling_settles_code,
)
from clearscout.domain import countries
from clearscout.domain.search_parameters import (
    JobSearchParameters,
    Location,
    WorkEligibility,
)
from clearscout.domain.staged_facts import PublishedFact
from clearscout.infrastructure.web_fetch.listing_facts import (
    remote_label_place,
    remote_only_location,
)
from clearscout.shared.text import sanitize


def _outside_location_finding(
    values: tuple[str, ...], parameters: JobSearchParameters
) -> ScreeningFinding | None:
    """Refuse when a value reads outside the saved set and no value answers a saved one."""
    stated = {place.country.casefold() for place in parameters.locations}
    read = read_place_signals(values, parameters)
    spelled = {country for _value, reading in read for country in reading.explicit}
    named: set[str] = set()
    for _value, reading in read:
        countries = {country.casefold() for country in (*reading.explicit, *reading.inferred)}
        # A value clears the whole fact by naming a saved country outright, by
        # suggesting one while naming or suggesting nothing unsaved, or by resting
        # on a subdivision code no place claims, unless a value names that code's country.
        if (
            (reading.unverified_subdivision and not sibling_settles_code(reading, spelled))
            or any(country.casefold() in stated for country in reading.explicit)
            or (countries and countries <= stated)
        ):
            return None
        named.update(reading.outside(stated, inferred=True))
    if not named:
        return None
    return ScreeningFinding(
        ScreeningDimension.LOCATION,
        ScreeningStanding.REFUSE,
        ScreeningCode.LOCATION_OUTSIDE,
        ", ".join(sorted(named)),
    )


def _location_unknown_finding(
    signals: tuple[str, ...], parameters: JobSearchParameters
) -> ScreeningFinding:
    outside = _outside_location_finding(signals, parameters) if signals else None
    if outside is not None:
        return outside
    return ScreeningFinding(
        ScreeningDimension.LOCATION,
        ScreeningStanding.UNKNOWN,
        ScreeningCode.LOCATION_UNRESOLVED
        if any(not remote_only_location(signal) for signal in signals)
        else ScreeningCode.LOCATION_MISSING,
    )


# Strongest work eligibility first; an ineligible place refuses before this order applies.
_ELIGIBILITY_STRENGTH = (
    WorkEligibility.AUTHORIZED,
    WorkEligibility.SPONSORSHIP_REQUIRED,
    WorkEligibility.INELIGIBLE,
)


def _preferred_tie(
    tied: list[Location], arrangements: PublishedFact, parameters: JobSearchParameters
) -> Location:
    """Break an eligibility tie toward a place whose arrangement accepts every reading.

    Falls back to key order rather than list order, so which saved place a
    multi-location posting resolves to does not depend on the order a
    publisher happened to list its locations in.
    """
    readings = tuple(
        reading for value in arrangements.values if (reading := arrangement_value(value))
    )
    if readings:
        accepting = [
            place for place in tied if accepts_all(parameters.arrangement_for(place.key), readings)
        ]
        if accepting:
            return min(accepting, key=lambda place: place.key)
    return min(tied, key=lambda place: place.key)


def _matched_places_finding(
    matched: tuple[Location, ...],
    arrangements: PublishedFact,
    parameters: JobSearchParameters,
) -> ScreeningFinding:
    """Resolve named places: an ineligible one refuses, else the strongest match wins."""
    ineligible = next(
        (place for place in matched if place.eligibility is WorkEligibility.INELIGIBLE), None
    )
    if ineligible is not None:
        return ScreeningFinding(
            ScreeningDimension.LOCATION,
            ScreeningStanding.REFUSE,
            ScreeningCode.LOCATION_INELIGIBLE,
            ineligible.key,
        )
    strength = min(_ELIGIBILITY_STRENGTH.index(place.eligibility) for place in matched)
    tied = [
        place for place in matched if _ELIGIBILITY_STRENGTH.index(place.eligibility) == strength
    ]
    winner = tied[0] if len(tied) == 1 else _preferred_tie(tied, arrangements, parameters)
    return ScreeningFinding(
        ScreeningDimension.LOCATION,
        ScreeningStanding.MATCH,
        ScreeningCode.LOCATION_MATCH,
        winner.key,
    )


# Workday's "ST - Town" value; a spaced dash keeps "IN-OFFICE" and "ON-SITE" out.
_LEADING_STATE = re.compile(r"\s*(?P<code>[A-Z]{2})\s+-\s+(?P<rest>\S.*)")


def _remote_state(value: str, parameters: JobSearchParameters) -> str:
    """Return a "Remote-MO" or "IL - Town" value with its US state code spelled out.

    A code that also spells a saved country ("Remote-DE" beside Germany) stays as written.
    """
    code, rest = remote_label_place(value), ""
    if not code and (leading := _LEADING_STATE.fullmatch(sanitize(value))):
        code, rest = leading["code"], f"{leading['rest']}, "
    state = countries.subdivision_name("United States", code)
    namesake = countries.territory_name(code)
    saved = {place.country.casefold() for place in parameters.locations}
    if not state or (namesake and namesake.casefold() in saved):
        return value
    return f"{rest}{state}"


def stated_places(fact: PublishedFact, location: str) -> tuple[str, ...]:
    """Return each distinct place string a row carries, resolved or not, in stated order."""
    return tuple(
        dict.fromkeys(
            value for value in (*fact.values, *fact.unresolved_values, location.strip()) if value
        )
    )


def place_finding(
    location: str,
    fact: PublishedFact,
    parameters: JobSearchParameters,
    arrangements: PublishedFact,
) -> ScreeningFinding:
    """Screen only a location the staged board fact can identify confidently."""
    if not parameters.locations:
        return ScreeningFinding(
            ScreeningDimension.LOCATION,
            ScreeningStanding.UNKNOWN,
            ScreeningCode.LOCATION_UNRESTRICTED,
        )
    signals = stated_places(fact, location)
    if fact.unresolved_values:
        return ScreeningFinding(
            ScreeningDimension.LOCATION,
            ScreeningStanding.UNKNOWN,
            ScreeningCode.LOCATION_UNRESOLVED,
        )
    if not fact.complete:
        return _location_unknown_finding(signals, parameters)
    values = tuple(
        value
        for value in fact.values or ((location.strip(),) if location.strip() else ())
        if not remote_only_location(value)
    )
    if not values:
        return ScreeningFinding(
            ScreeningDimension.LOCATION,
            ScreeningStanding.UNKNOWN,
            ScreeningCode.LOCATION_MISSING,
        )
    # A generic "Remote" label must not turn an explicit foreign market in
    # the same publisher record into a Canadian remote match.
    outside = _outside_location_finding(signals, parameters)
    if outside is not None:
        return outside
    values = tuple(_remote_state(value, parameters) for value in values)
    matched = tuple(
        {
            place.key: place for value in values for place in parameters.matching_locations(value)
        }.values()
    )
    if len(matched) == 1:
        return _matched_places_finding(matched, arrangements, parameters)
    # Two or more places named at their own granularity are a genuine multi-market
    # posting and match at the strongest; a coarse name that only spans finer saved
    # policies stays unresolved because it cannot choose between them.
    precise = tuple(
        {place.key: place for value in values for place in parameters.markets_named(value)}.values()
    )
    if len(precise) >= 2:
        return _matched_places_finding(precise, arrangements, parameters)
    return ScreeningFinding(
        ScreeningDimension.LOCATION,
        ScreeningStanding.UNKNOWN,
        ScreeningCode.LOCATION_UNRESOLVED,
    )
