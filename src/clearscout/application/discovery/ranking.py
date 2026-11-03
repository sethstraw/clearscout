"""Order a review queue from the title and place a discovery already carries."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from clearscout.application.discovery.titles import (
    best_title_answer,
    ladder_words,
    title_contains_phrase,
    title_words,
)
from clearscout.domain.search_parameters import (
    LEVELS,
    Cluster,
    ClusterKind,
    JobSearchParameters,
    Location,
    WorkEligibility,
)

TITLE_WEIGHT = 60
AUTHORIZED_WEIGHT = 30
SPONSORSHIP_WEIGHT = 10
CAPABILITY_WEIGHT = 25
DOMAIN_WEIGHT = 15
SENIORITY_WEIGHT = 10
# A boundary ranks a posting down rather than hiding it, so it outweighs one
# matched cluster and never outweighs the whole queue.
BOUNDARY_PENALTY = -30
# A country nobody stated ranks down the same way. A place the reader could not
# place at all is unknown rather than elsewhere, and keeps its zero.
ELSEWHERE_PENALTY = -30
DEFAULT_MISSED_REQUIREMENT_PENALTY = -5


class DiscoverySignal(StrEnum):
    """The signal a discovery's ranking matched on."""

    TITLE = "title"
    RESPONSIBILITY = "responsibility"
    DOMAIN_INTERSECTION = "domain_intersection"


@dataclass(frozen=True)
class Rank:
    """Where one discovery sits in the queue, and what put it there."""

    score: int
    because: tuple[str, ...]
    signal: DiscoverySignal | None = None


def attributed_signal(*, title: bool, capability: bool, domain: bool) -> DiscoverySignal | None:
    """Return the signal that surfaced a discovery, or None when none did.

    A matched title takes the attribution, so the other signals count only what
    title matching did not surface. A subject without a craft is not an
    intersection and stays unattributed.
    """
    if title:
        return DiscoverySignal.TITLE
    if capability and domain:
        return DiscoverySignal.DOMAIN_INTERSECTION
    if capability:
        return DiscoverySignal.RESPONSIBILITY
    return None


def _title_score(title: str, searched: list[str]) -> tuple[int, str]:
    """Score how well a title answers any of the names being searched under."""
    if not title_words(title) or not searched:
        return 0, ""
    answer = best_title_answer(title, searched)
    if answer is None:
        return 0, ""
    matched, best = answer
    return round(best * TITLE_WEIGHT), f"the title answers {matched}"


def _place_name(place: Location) -> str:
    """Name a place from the words its owner wrote, never from its internal key."""
    if place.metro:
        return place.metro
    if place.region:
        return f"{place.region}, {place.country}"
    return place.country


def _place_score(location: str, parameters: JobSearchParameters) -> tuple[int, str]:
    """Score where the work is against the places this person may take it."""
    matched = parameters.matching_locations(location)
    if not matched:
        stated = {place.country.casefold() for place in parameters.locations}
        named = parameters.place_reader.read(location).outside(stated)
        if len(named) == 1:
            return ELSEWHERE_PENALTY, f"{named[0]} is not one of your places"
        return 0, ""
    place = matched[0]
    if place.eligibility is WorkEligibility.AUTHORIZED:
        return AUTHORIZED_WEIGHT, f"you may work in {_place_name(place)} already"
    if place.eligibility is WorkEligibility.SPONSORSHIP_REQUIRED:
        return SPONSORSHIP_WEIGHT, f"{_place_name(place)} needs sponsorship"
    return 0, ""


def _cluster_score(
    title: str, clusters: tuple[Cluster, ...], weight: int, noun: str
) -> tuple[int, str]:
    """Score a title against the first cluster any of whose words it carries.

    One match is the whole signal: a title carrying four words of one cluster
    answers that cluster no more than a title carrying one, and scoring per word
    would let a long cluster outrank a precise one.
    """
    found = title_words(title)
    for cluster in clusters:
        if any(title_words(term) & found for term in cluster.matched_on):
            return weight, f"the {noun} is {cluster.name}"
    return 0, ""


def _boundary_score(title: str, boundaries: tuple[str, ...]) -> tuple[int, str]:
    """Rank down a title that states a refused boundary as a whole phrase, not a shared word."""
    for boundary in boundaries:
        if title_contains_phrase(title, boundary):
            return BOUNDARY_PENALTY, f"you ruled out {boundary}"
    return 0, ""


def _seniority_score(title: str, parameters: JobSearchParameters) -> tuple[int, str]:
    """Score an explicit title level against the person's stated band."""
    if not parameters.seniority.stated:
        return 0, ""
    ladder = ladder_words(title)
    found = [level for level in LEVELS if level in ladder]
    if not found:
        return 0, ""
    level = max(found, key=LEVELS.index)
    covered = parameters.seniority.covers(level)
    if covered is True:
        return SENIORITY_WEIGHT, f"the level is {level}"
    if covered is False:
        return -SENIORITY_WEIGHT, f"the level {level} is outside your stated band"
    return 0, ""


def _missed_requirement_score(
    title: str, location: str, missed_texts: frozenset[str], penalty: int
) -> tuple[int, str]:
    """Rank down a title matching a requirement text this workspace missed before.

    Bounded by how many distinct missed texts appear in the title or the
    residence line, so a discovery matching several missed requirements does not
    outweigh the whole queue.
    """
    if not missed_texts or penalty == 0:
        return 0, ""
    haystack = f"{title} {location}".casefold()
    matched = [text for text in missed_texts if text and text in haystack]
    if not matched:
        return 0, ""
    return penalty * len(matched), f"missed before: {matched[0]}"


def rank(
    title: str,
    location: str,
    parameters: JobSearchParameters,
    searched: list[str],
    *,
    missed_requirement_texts: frozenset[str] = frozenset(),
    missed_requirement_penalty: int = DEFAULT_MISSED_REQUIREMENT_PENALTY,
) -> Rank:
    """Return the standing of one discovery, and the reasons for it."""
    focus = parameters.focus
    answered = _title_score(title, searched)
    capability = _cluster_score(
        title, focus.ranked_clusters(ClusterKind.CAPABILITY), CAPABILITY_WEIGHT, "work"
    )
    domain = _cluster_score(
        title, focus.ranked_clusters(ClusterKind.DOMAIN), DOMAIN_WEIGHT, "subject"
    )
    scored = [
        answered,
        _place_score(location, parameters),
        capability,
        domain,
        _boundary_score(title, focus.stated_boundaries),
        _seniority_score(title, parameters),
        _missed_requirement_score(
            title, location, missed_requirement_texts, missed_requirement_penalty
        ),
    ]
    return Rank(
        score=sum(points for points, _ in scored),
        because=tuple(reason for _, reason in scored if reason),
        signal=attributed_signal(
            title=bool(answered[1]),
            capability=bool(capability[1]),
            domain=bool(domain[1]),
        ),
    )
