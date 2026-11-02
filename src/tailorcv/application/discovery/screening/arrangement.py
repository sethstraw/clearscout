"""Work arrangement reading and findings."""

from __future__ import annotations

import re
from typing import Literal

from tailorcv.application.discovery.screening.arrangement_terms import (
    ARRANGEMENT_TOKEN,
    PERIOD_LIMITED,
    ArrangementCategory,
    arrangement_value,
    token_category,
)
from tailorcv.application.discovery.screening.findings import (
    ScreeningCode,
    ScreeningDimension,
    ScreeningFinding,
    ScreeningStanding,
)
from tailorcv.application.discovery.screening.place_signals import (
    may_mean_accepting_place,
    places_refusing,
    refusal_place_trust,
)
from tailorcv.domain.identity import url_path_segments
from tailorcv.domain.search_parameters import (
    Acceptance,
    Arrangement,
    JobSearchParameters,
)
from tailorcv.domain.staged_facts import PublishedFact
from tailorcv.shared.text import sanitize


def arrangement_categories(fact: PublishedFact) -> tuple[ArrangementCategory, ...]:
    """Classify every publisher arrangement with the screening vocabulary."""
    categories: list[ArrangementCategory] = []
    for value in fact.values:
        category: ArrangementCategory
        if PERIOD_LIMITED.search(sanitize(value)):
            category = "period_limited"
        elif (reading := arrangement_value(value)) is None:
            category = "unknown"
        elif reading[0] == "remote":
            category = "remote"
        elif reading[0] == "hybrid":
            category = "hybrid"
        else:
            category = "on_site"
        if category not in categories:
            categories.append(category)
    return tuple(categories or ["unknown"])


ArrangementSource = Literal["published", "inferred_from_url", "inferred_from_location", "missing"]


_COUNTRY_TOKENS = re.compile(
    r"\b("
    r"United States of America|United States|USA|US|"
    r"Canada|CAN|CA|"
    r"United Kingdom|UK|GB|GBR"
    r")\b"
)


_WORKDAY_COUNTRY_DASH_CITY = re.compile(r"\b[A-Z]{3}\s*-\s*[A-Z][a-z]+")


def _looks_like_specific_city(location: str) -> bool:
    """Detect a city-plus-region-plus-country string, not a bare country label."""
    if not location:
        return False
    has_structure = ("," in location) or bool(_WORKDAY_COUNTRY_DASH_CITY.search(location))
    if not has_structure:
        return False
    return bool(_COUNTRY_TOKENS.search(location))


# For observability, not policy. Only infers when publisher, extraction,
# URL, and body all lack signal.
def resolve_arrangement_categories(
    fact: PublishedFact, location: str, discovery_url: str
) -> tuple[tuple[ArrangementCategory, ...], ArrangementSource]:
    """Classify arrangement with a recall-first fallback and expose the source."""
    existing = arrangement_categories(fact)
    if existing != ("unknown",):
        return existing, "published"
    slug = ""
    if discovery_url:
        slug = "/".join(url_path_segments(discovery_url)).replace("-", " ").replace("_", " ")
        slug = slug.lower()
    url_match = ARRANGEMENT_TOKEN.search(slug) if slug else None
    if url_match is not None:
        return (token_category(url_match.group(1)),), "inferred_from_url"
    location_match = ARRANGEMENT_TOKEN.search(sanitize(location)) if location else None
    if location_match is not None:
        return (token_category(location_match.group(1)),), "inferred_from_location"
    if location and _looks_like_specific_city(location):
        return ("on_site",), "inferred_from_location"
    return ("unknown",), "missing"


def _accepted_arrangements(
    readings: tuple[tuple[str, int | None], ...], arrangement: Arrangement
) -> tuple[list[tuple[str, int | None]], ScreeningFinding | None]:
    """Select explicitly accepted options or explain why selection is unresolved."""
    accepted = [
        (name, days)
        for name, days in readings
        if arrangement.acceptance_of(name) in (Acceptance.PREFERRED, Acceptance.ACCEPTABLE)
    ]
    if accepted:
        return accepted, None
    if not arrangement.stated:
        return list(readings), None
    if any(arrangement.acceptance_of(name) is None for name, _ in readings):
        return [], ScreeningFinding(
            ScreeningDimension.ARRANGEMENT,
            ScreeningStanding.UNKNOWN,
            ScreeningCode.ARRANGEMENT_UNKNOWN,
        )
    return [], ScreeningFinding(
        ScreeningDimension.ARRANGEMENT,
        ScreeningStanding.REFUSE,
        ScreeningCode.ARRANGEMENT_REFUSED,
        ", ".join(name for name, _ in readings),
    )


def _office_cadence_finding(
    accepted: list[tuple[str, int | None]], maximum: int | None
) -> ScreeningFinding | None:
    """Compare accepted options with an explicit weekly office limit."""
    if maximum is None or any(days is not None and days <= maximum for _, days in accepted):
        return None
    if any(days is None for _, days in accepted):
        standing, code = ScreeningStanding.UNKNOWN, ScreeningCode.ARRANGEMENT_UNKNOWN
    else:
        standing, code = ScreeningStanding.REFUSE, ScreeningCode.ARRANGEMENT_OFFICE_LIMIT
    return ScreeningFinding(ScreeningDimension.ARRANGEMENT, standing, code)


def _refused_arrangement_finding(
    parameters: JobSearchParameters,
    readings: tuple[tuple[str, int | None], ...],
    selection_finding: ScreeningFinding,
    *,
    identity_trusted: bool,
    location_fact: PublishedFact | None,
    location: str,
) -> ScreeningFinding:
    """Refuse an office arrangement unless its place may lie inside a saved place accepting it.

    Only a trusted, complete place may refuse; the subject names the refusing saved keys
    when a precise place settles them.
    """
    places, trusted = refusal_place_trust(location_fact, identity_trusted)
    if not trusted or may_mean_accepting_place(readings, places, location, parameters):
        return ScreeningFinding(
            ScreeningDimension.ARRANGEMENT,
            ScreeningStanding.UNKNOWN,
            ScreeningCode.ARRANGEMENT_REMOTE_UNCONFIRMED,
            selection_finding.subject,
        )
    return ScreeningFinding(
        ScreeningDimension.ARRANGEMENT,
        ScreeningStanding.REFUSE,
        ScreeningCode.ARRANGEMENT_REFUSED,
        places_refusing(readings, places, location, parameters) or selection_finding.subject,
    )


def arrangement_finding(
    fact: PublishedFact,
    parameters: JobSearchParameters,
    location_key: str | None,
    *,
    identity_trusted: bool = True,
    location_fact: PublishedFact | None = None,
    location: str = "",
) -> ScreeningFinding | None:
    """Screen complete arrangement values and refuse only explicit constraints."""
    arrangement = parameters.arrangement_for(location_key)
    if not arrangement.stated and arrangement.maximum_office_days is None:
        return None
    if not fact.complete:
        return ScreeningFinding(
            ScreeningDimension.ARRANGEMENT,
            ScreeningStanding.UNKNOWN,
            ScreeningCode.ARRANGEMENT_UNKNOWN,
        )
    readings = tuple(reading for value in fact.values if (reading := arrangement_value(value)))
    if not readings:
        return ScreeningFinding(
            ScreeningDimension.ARRANGEMENT,
            ScreeningStanding.UNKNOWN,
            ScreeningCode.ARRANGEMENT_UNKNOWN,
        )
    accepted, selection_finding = _accepted_arrangements(readings, arrangement)
    if selection_finding is not None:
        if selection_finding.code is ScreeningCode.ARRANGEMENT_REFUSED:
            return _refused_arrangement_finding(
                parameters,
                readings,
                selection_finding,
                identity_trusted=identity_trusted,
                location_fact=location_fact,
                location=location,
            )
        return selection_finding
    cadence_finding = _office_cadence_finding(accepted, arrangement.maximum_office_days)
    if cadence_finding is not None:
        return cadence_finding
    return ScreeningFinding(
        ScreeningDimension.ARRANGEMENT,
        ScreeningStanding.MATCH,
        ScreeningCode.ARRANGEMENT_MATCH,
        ", ".join(name for name, _ in accepted),
    )
