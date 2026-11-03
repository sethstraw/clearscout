"""Place signals read from stated locations and the saved places they may mean."""

from __future__ import annotations

import re
from collections.abc import Sequence

from clearscout.application.discovery.screening.arrangement_terms import (
    ARRANGEMENT_TOKEN,
    accepts_all,
)
from clearscout.domain import countries
from clearscout.domain.identity import mentions, normalize_text
from clearscout.domain.places import PlaceReading
from clearscout.domain.search_parameters import (
    JobSearchParameters,
    Location,
)
from clearscout.domain.staged_facts import PublishedFact
from clearscout.infrastructure.web_fetch.listing_facts import (
    REMOTE_PLACE,
)


def _place_signal_values(location_fact: PublishedFact, location: str) -> tuple[str, ...] | None:
    """Return every distinct place value, or None when any of them reads as remote."""
    raw = [value.strip() for value in (*location_fact.values, location) if value.strip()]
    if any(REMOTE_PLACE.search(value) for value in raw):
        return None
    return tuple(dict.fromkeys(raw))


def read_place_signals(
    signals: tuple[str, ...], parameters: JobSearchParameters
) -> list[tuple[str, PlaceReading]]:
    """Pair each stated place value with the saved place reader's reading of it."""
    return [(value, parameters.place_reader.read(value)) for value in signals]


def _coarse_signals_are_safe(
    read: Sequence[tuple[str, PlaceReading]], precise_countries: set[str]
) -> bool:
    """Return whether every coarse (non-precise) value names only a country a precise value names.

    A bare country or region is otherwise unsafe to drop, since it can span
    several saved locations on its own.
    """
    for _value, reading in read:
        if reading.locality:
            continue
        named = {*reading.explicit, *reading.inferred}
        if not named or not named <= precise_countries:
            return False
    return True


def trusted_value_countries(reading: PlaceReading, explicit_countries: set[str]) -> set[str]:
    """Trust an inferred country guess only when a sibling value states it explicitly."""
    if reading.explicit:
        return {name.casefold() for name in reading.explicit}
    return {name.casefold() for name in reading.inferred} & explicit_countries


def _nearby_saved_places(in_country: Sequence[Location], region: str) -> Sequence[Location]:
    """Return saved places close enough to soften a refusal: same region, or a region-less metro."""
    if not region:
        return in_country
    return [
        place
        for place in in_country
        if place.region.casefold() == region.casefold() or (place.metro and not place.region)
    ]


def saved_places_near_site(
    in_country: Sequence[Location], value: str, parameters: JobSearchParameters
) -> list[Location]:
    """Return saved places a named site may sit near, including any whose words it spells."""
    region = parameters.place_reader.region_of(value)
    # An ambiguous city or a region that is also a city ("Quebec") leaves the gazetteer
    # unsettled, so only a region the value spells or a region-less metro counts.
    if not region:
        spelled = normalize_text(value)
        codes = re.findall(r"\b[A-Z]{2}\b", value)
        in_country = [
            place
            for place in in_country
            if (place.metro and not place.region)
            or (place.region and mentions(spelled, normalize_text(place.region)))
            or any(
                countries.subdivision_name(place.country, code) == place.region for code in codes
            )
        ]
    return [*_nearby_saved_places(in_country, region), *parameters.matching_locations(value)]


def _value_refusal_keys(
    value: str,
    reading: PlaceReading,
    readings: tuple[tuple[str, int | None], ...],
    parameters: JobSearchParameters,
    explicit_countries: set[str],
) -> frozenset[str] | None:
    """Return the saved-location keys one precise value refuses, or None to abandon the read."""
    countries = trusted_value_countries(reading, explicit_countries)
    if not countries:
        return None
    # A saved country-level location can already match here (a province name
    # is a recognized country spelling), so the subdivision softening below
    # must run regardless of whether markets_named found a candidate.
    matched = parameters.markets_named(value)
    in_country = [place for place in parameters.locations if place.country.casefold() in countries]
    nearby = _nearby_saved_places(in_country, parameters.place_reader.region_of(value))
    if any(accepts_all(parameters.arrangement_for(place.key), readings) for place in nearby):
        return None
    if not matched:
        matched = tuple(place for place in in_country if place.granularity <= 1)
    refuses_all = all(
        not accepts_all(parameters.arrangement_for(place.key), readings) for place in matched
    )
    if not matched or not refuses_all:
        return None
    return frozenset(place.key for place in matched)


def places_refusing(
    readings: tuple[tuple[str, int | None], ...],
    location_fact: PublishedFact,
    location: str,
    parameters: JobSearchParameters,
) -> str | None:
    """Return the refusing saved-location key(s), only when every precise value refuses.

    Each precise value is judged on its own against saved locations in the
    same gazetteer subdivision, falling back to its whole country only when
    no subdivision resolves; a value with no candidate leaves the read
    undecided rather than guessed.
    """
    signals = _place_signal_values(location_fact, location)
    if not signals:
        return None
    read = read_place_signals(signals, parameters)
    precise = [(value, reading) for value, reading in read if reading.locality]
    if not precise:
        return None
    precise_countries = {
        name for _, reading in precise for name in (*reading.explicit, *reading.inferred)
    }
    if not _coarse_signals_are_safe(read, precise_countries):
        return None
    explicit_countries = {name.casefold() for _, reading in read for name in reading.explicit}
    refusing_keys: set[str] = set()
    for value, reading in precise:
        matched = _value_refusal_keys(value, reading, readings, parameters, explicit_countries)
        if matched is None:
            return None
        refusing_keys.update(matched)
    return ", ".join(sorted(refusing_keys))


def refusal_place_trust(
    location_fact: PublishedFact | None, identity_trusted: bool
) -> tuple[PublishedFact, bool]:
    """Return the location fact to judge and whether it may refuse by place."""
    places = location_fact or PublishedFact()
    trusted = identity_trusted and places.complete and not places.unresolved_values
    return places, trusted


def may_mean_accepting_place(
    readings: tuple[tuple[str, int | None], ...],
    location_fact: PublishedFact,
    location: str,
    parameters: JobSearchParameters,
) -> bool:
    """Return whether a stated place may lie inside a saved place accepting every reading.

    A remote value, an unlisted-places summary, an incomplete list, a named site
    resolving no country, or no value resolving a country keeps the row open; a bare
    country or region a named site already places adds nothing.
    """
    signals = _place_signal_values(location_fact, location)
    if signals is None or any(UNLISTED_PLACES.search(value) for value in signals):
        return True
    if not location_fact.complete and (location_fact.values or location_fact.unresolved_values):
        return True
    read = read_place_signals(signals, parameters)
    site_countries = {
        name.casefold()
        for value, reading in read
        if names_site(value, parameters)
        for name in reading.countries
    }
    if not site_countries and (
        not any(reading.countries for _value, reading in read)
        or any(not reading.countries and names_site(value, parameters) for value, reading in read)
    ):
        return True
    return any(
        _value_may_accept(value, reading, site_countries, readings, parameters)
        for value, reading in read
    )


def _value_may_accept(
    value: str,
    reading: PlaceReading,
    site_countries: set[str],
    readings: tuple[tuple[str, int | None], ...],
    parameters: JobSearchParameters,
) -> bool:
    """Return whether one stated place may lie inside a saved place accepting every reading."""
    countries = {name.casefold() for name in reading.countries}
    site = names_site(value, parameters)
    if not site and countries and countries <= site_countries:
        return False
    in_country = [place for place in parameters.locations if place.country.casefold() in countries]
    nearby = (
        saved_places_near_site(in_country, value, parameters)
        if site
        else [*in_country, *parameters.matching_locations(value)]
    )
    return any(accepts_all(parameters.arrangement_for(place.key), readings) for place in nearby)


def sibling_settles_code(reading: PlaceReading, spelled: set[str]) -> bool:
    """Return whether a known city's unverified code spells only countries a sibling names."""
    # "Hyderabad - TS - IN" beside "India" is India's code; a town no gazetteer
    # row names ("Fictionville, IN") may still be in Indiana.
    codes = set(reading.unverified_code_countries)
    return reading.locality and bool(codes) and codes <= spelled


def placed_by_reading_or_sibling(reading: PlaceReading, spelled: set[str]) -> bool:
    """Return whether a value names its country, or a sibling spells every country it rests on."""
    if reading.unverified_subdivision:
        return sibling_settles_code(reading, spelled) and set(reading.inferred) <= spelled
    return bool(reading.explicit) or (bool(reading.inferred) and set(reading.inferred) <= spelled)


_WORD = re.compile(r"[^\W\d_]{2,}")


# "Any city, MO" or "Various" stands in for a place the board left unnamed.
_PLACEHOLDER_PLACE = re.compile(
    r"\b(?:any|multiple|various|several|all)\s+(?:cit(?:y|ies)|locations?|places?|towns?|sites?)\b"
    r"|^\s*various\b",
    re.IGNORECASE,
)


def names_site(value: str, parameters: JobSearchParameters) -> bool:
    """Return whether a place value names a place below its country and region.

    Country-table spans cover subdivision names too, so a bare "Washington" or
    "Ontario" leaves no word, while "Seattle, Washington" leaves "Seattle".
    """
    if _PLACEHOLDER_PLACE.search(value):
        return False
    rest = value
    for (start, end), _country in parameters.country_table.spans_in(value):
        rest = rest[:start] + " " * (end - start) + rest[end:]
    return bool(_WORD.search(ARRANGEMENT_TOKEN.sub(" ", rest)))


# "Multiple Locations", "Various Locations", "2 Locations": the unlisted places may be any.
UNLISTED_PLACES = re.compile(
    r"\b(?:multiple|various|several|many|other|\d+)\s+(?:locations?|offices|sites)\b",
    re.IGNORECASE,
)
