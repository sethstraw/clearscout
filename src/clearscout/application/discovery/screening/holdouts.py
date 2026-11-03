"""Structured holdouts that keep a listing out of automatic staging."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace

from clearscout.application.discovery.screening.arrangement_terms import (
    AWAY_FROM_OFFICE,
    REMOTE_WORD,
    arrangement_value,
)
from clearscout.application.discovery.screening.findings import (
    GENERIC_US_REMOTE,
    DiscoveryScreening,
    ScreeningCode,
    ScreeningDimension,
    ScreeningFinding,
    ScreeningStanding,
)
from clearscout.application.discovery.screening.location import stated_places
from clearscout.application.discovery.screening.place_signals import (
    UNLISTED_PLACES,
    names_site,
    placed_by_reading_or_sibling,
    read_place_signals,
    refusal_place_trust,
    saved_places_near_site,
    trusted_value_countries,
)
from clearscout.domain import countries
from clearscout.domain.arrangement_grammar import remote_offer_statement
from clearscout.domain.search_parameters import (
    Acceptance,
    JobSearchParameters,
    Location,
)
from clearscout.domain.staged_facts import StagedListingFacts
from clearscout.infrastructure.web_fetch.listing_facts import (
    body_arrangement_note,
    exhaustive_location_provider,
)
from clearscout.shared.text import sanitize


def structured_location_holdout(
    facts: StagedListingFacts,
    location: str,
    screening: DiscoveryScreening,
    parameters: JobSearchParameters,
) -> bool:
    """Return whether every published location value and the display name only unsaved countries."""
    if not parameters.locations:
        return False
    if not facts.publisher_vouched:
        return False
    if not screening.location_fact_complete:
        return False
    if not any(finding.code is ScreeningCode.LOCATION_OUTSIDE for finding in screening.findings):
        return False
    fact = facts.locations
    if fact.unresolved_values or not fact.values:
        return False
    if not exhaustive_location_provider(fact.provenance):
        return False
    signals = tuple(
        dict.fromkeys(value.strip() for value in (*fact.values, location) if value.strip())
    )
    if not signals:
        return False
    table = parameters.country_table
    saved = {
        (table.named_in(place.country) or place.country).casefold()
        for place in parameters.locations
    }
    spelled = {name for value in signals for name in parameters.place_reader.read(value).explicit}
    return all(_signal_is_foreign(value, parameters, saved, spelled) for value in signals)


def _signal_is_foreign(
    value: str, parameters: JobSearchParameters, saved: set[str], spelled: set[str]
) -> bool:
    reading = parameters.place_reader.read(value)
    if not placed_by_reading_or_sibling(reading, spelled):
        return False
    if any(name in countries.PUBLISHER_REGION_NAMES for name in reading.explicit):
        return False
    if GENERIC_US_REMOTE.fullmatch(sanitize(value)):
        return False
    sovereigns = {
        countries.territory_sovereign(name) or name
        for name in (*reading.explicit, *reading.inferred, *reading.unverified_code_countries)
    }
    if any(name.casefold() in saved for name in sovereigns):
        return False
    return not parameters.matching_locations(value)


# Board values that place the work nowhere in particular: "<State> - Virtual",
# "USA - Nationwide", "Anywhere in the US", "Telework", "Distributed", "Flexible".
_UNPLACED_WORK = re.compile(
    r"\b(?:virtual|telework\w*|distributed|flexible|anywhere|nationwide)\b", re.IGNORECASE
)


_UNCONFIRMED_ARRANGEMENT = (
    ScreeningCode.ARRANGEMENT_UNKNOWN,
    ScreeningCode.ARRANGEMENT_REMOTE_UNCONFIRMED,
)


def _means_remote(value: str) -> bool:
    """Return whether a board value names remote, away-from-office, or unplaced work."""
    return any(pattern.search(value) for pattern in (REMOTE_WORD, AWAY_FROM_OFFICE, _UNPLACED_WORK))


def _remote_evidence(facts: StagedListingFacts, location: str, description: str) -> bool:
    """Return whether a place, a board arrangement value, or the body says remote."""
    if any(_means_remote(value) for value in stated_places(facts.locations, location)):
        return True
    arrangement = facts.arrangements
    for value in (*arrangement.values, *arrangement.unresolved_values):
        if body_arrangement_note(value):
            continue
        reading = arrangement_value(value)
        if reading is None and _means_remote(value.partition(",")[0]):
            return True
        if reading is not None and reading[0] == "remote":
            return True
    return remote_offer_statement(description) is not None


def _remote_only(place: Location, parameters: JobSearchParameters) -> bool:
    arrangement = parameters.arrangement_for(place.key)
    return arrangement.hybrid is Acceptance.REFUSED and arrangement.on_site is Acceptance.REFUSED


def _sites_only_remote(
    facts: StagedListingFacts, location: str, parameters: JobSearchParameters
) -> bool:
    """Return whether the row names a saved-market site and every place it may mean is remote-only.

    Saved places carry no distance, so a named site may also mean any saved
    place in its region; a bare country or region names no site and must mean
    only remote-only places unless a named site already covers it. A value
    naming no place is ignored unless it says the list is incomplete.
    """
    places, trusted = refusal_place_trust(facts.locations, facts.publisher_vouched)
    if not trusted:
        return False
    read = read_place_signals(stated_places(places, location), parameters)
    sites = {value for value, _reading in read if names_site(value, parameters)}
    site_countries = {
        name.casefold() for value, reading in read if value in sites for name in reading.countries
    }
    explicit = {name.casefold() for _, reading in read for name in reading.explicit}
    held = False
    for value, reading in read:
        countries = trusted_value_countries(reading, explicit)
        if not countries and reading.names_no_place and not UNLISTED_PLACES.search(value):
            continue
        if not countries:
            return False
        candidates = _saved_places_meant(
            value,
            countries,
            site=value in sites,
            site_countries=site_countries,
            parameters=parameters,
        )
        if candidates is None:
            continue
        if not candidates or not all(_remote_only(place, parameters) for place in candidates):
            return False
        held = held or value in sites
    return held


def _saved_places_meant(
    value: str,
    countries: set[str],
    *,
    site: bool,
    site_countries: set[str],
    parameters: JobSearchParameters,
) -> Sequence[Location] | None:
    """Return the saved places one place value may mean, or None when it adds nothing to judge."""
    in_country = [place for place in parameters.locations if place.country.casefold() in countries]
    if not in_country:
        return None
    if site:
        return [
            *saved_places_near_site(in_country, value, parameters),
            *(
                parameters.markets_named(value)
                or [place for place in in_country if place.granularity <= 1]
            ),
        ]
    if countries <= site_countries:
        return None
    return in_country


def structured_arrangement_holdout(
    facts: StagedListingFacts,
    location: str,
    screening: DiscoveryScreening,
    parameters: JobSearchParameters,
    description: str = "",
) -> bool:
    """Return whether an office arrangement at a remote-only market may hold this listing out.

    Requires publisher identity, a complete exhaustive location fact, and a
    non-empty body: a board with no description (Workday's sweep) cannot rule
    out a remote offer. An ``ARRANGEMENT_REFUSED`` finding over a complete
    arrangement fact holds unless the body offers remote. An unknown or
    unconfirmed arrangement holds only with no remote evidence at all and a
    named site whose every possible saved place refuses hybrid and on-site.
    """
    if not facts.publisher_vouched:
        return False
    if not screening.location_fact_complete:
        return False
    if not exhaustive_location_provider(facts.locations.provenance):
        return False
    if not description.strip():
        return False
    finding = next(
        (item for item in screening.findings if item.dimension is ScreeningDimension.ARRANGEMENT),
        None,
    )
    if finding is None:
        return False
    if finding.code is ScreeningCode.ARRANGEMENT_REFUSED:
        arrangement = facts.arrangements
        return (
            arrangement.complete
            and not arrangement.unresolved_values
            and remote_offer_statement(description) is None
        )
    if finding.code not in _UNCONFIRMED_ARRANGEMENT:
        return False
    return office_only_at_remote_only_sites(facts, location, parameters, description)


def office_only_at_remote_only_sites(
    facts: StagedListingFacts,
    location: str,
    parameters: JobSearchParameters,
    description: str,
    *,
    title: str = "",
) -> bool:
    """Return whether nothing says remote and every named site is a place accepted only remotely."""
    if _means_remote(title):
        return False
    return not _remote_evidence(facts, location, description) and _sites_only_remote(
        facts, location, parameters
    )


@dataclass(frozen=True)
class StructuredHoldout:
    """The finding a structured hold-out refused on, and the screening that records it."""

    finding: ScreeningFinding
    screening: DiscoveryScreening


def structured_holdout(
    facts: StagedListingFacts,
    location: str,
    screening: DiscoveryScreening,
    parameters: JobSearchParameters,
    description: str = "",
) -> StructuredHoldout | None:
    """Return the structured hold-out that keeps this listing out of Pending, if any.

    Location is checked first, so a listing both rules hold out is reported as
    ``LOCATION_OUTSIDE``. A refusal screening did not already record is added
    after its dimension's own finding, which stays.
    """
    if structured_location_holdout(facts, location, screening, parameters):
        code, dimension = ScreeningCode.LOCATION_OUTSIDE, ScreeningDimension.LOCATION
    elif structured_arrangement_holdout(facts, location, screening, parameters, description):
        dimension = ScreeningDimension.ARRANGEMENT
        refused = any(item.code is ScreeningCode.ARRANGEMENT_REFUSED for item in screening.findings)
        code = (
            ScreeningCode.ARRANGEMENT_REFUSED
            if refused
            else ScreeningCode.ARRANGEMENT_REMOTE_ONLY_SITES
        )
    else:
        return None
    recorded = next((item for item in screening.findings if item.code is code), None)
    if recorded is not None:
        return StructuredHoldout(recorded, screening)
    finding = ScreeningFinding(dimension, ScreeningStanding.REFUSE, code)
    findings = list(screening.findings)
    position = next(
        (index + 1 for index, item in enumerate(findings) if item.dimension is dimension),
        len(findings),
    )
    findings.insert(position, finding)
    return StructuredHoldout(finding, replace(screening, findings=tuple(findings)))
