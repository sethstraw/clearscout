"""Read work authorization and residence requirements from a posting."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from itertools import pairwise

from clearscout.application.tailoring.fit.lexicon import (
    AUTHORIZED,
    CLEARANCE,
    NO_SPONSORSHIP,
    PAY_CONTEXT,
    REQUIRED_TIME_ZONE,
    SPONSORSHIP,
    TIME_ZONE_MENTION,
    WORK_AUTHORIZATION_ALTERNATIVE,
    answered_field,
    local_residence_required,
    residence_required,
    states_nearness,
)
from clearscout.application.tailoring.fit.structure import (
    Answer,
    Block,
    content_lines,
    first_saying,
    quote_line,
    unasked_answer,
)
from clearscout.domain.countries import (
    DEFAULT_NAMES,
    SUBDIVISION_CODES,
    subdivision_name,
    subdivision_names,
)
from clearscout.domain.places import indexed_places
from clearscout.domain.search_parameters import JobSearchParameters, Location
from clearscout.shared.text import sentence_pieces

# A state whose name plus "City" is a city in it: "New York, NY" names that city.
_CITY_STATE_CODES = {
    name: code
    for country in DEFAULT_NAMES
    for code in SUBDIVISION_CODES
    if (name := subdivision_name(country, code))
    and any(admin == code for _, admin in indexed_places(f"{name} City"))
}


def read_authorization(blocks: tuple[Block, ...]) -> Answer[str]:
    """Return what the posting says about the right to work, or that it says nothing."""
    question = "Work authorization"
    lines = tuple(line for block in blocks for line in block.lines)
    for phrases, verdict in (
        (NO_SPONSORSHIP, "sponsorship is not offered"),
        (SPONSORSHIP, "sponsorship is offered"),
        (AUTHORIZED, "you must already be authorized to work"),
        (CLEARANCE, "a security clearance is required"),
    ):
        if found := first_saying(lines, phrases):
            return Answer(question, verdict, evidence=(quote_line(found[1], found[0]),))
    return Answer(question, note="the posting says nothing about sponsorship or authorization")


def read_residence_requirement(
    blocks: tuple[Block, ...], wanted: JobSearchParameters, listing_location: str = ""
) -> Answer[str]:
    """Read a stated requirement to live somewhere this person declared no place.

    Refuse-safe, parallel to the credential gate: fires only on a residence
    requirement whose country the person declared no eligible place in, or, for a
    person who marked where they live, no home or place they will relocate to.
    A requirement naming no maintained country abstains rather than refusing; one
    to live near the posting is read against the listing's own country.
    """
    question = "Residence the posting requires"
    if not wanted.locations:
        return unasked_answer(question)
    can_work = _eligible_countries(wanted)
    if not can_work:
        return Answer(
            question, note="you have declared no place a residence requirement could meet"
        )
    lives = resident_countries(wanted)
    if refused := _unsatisfied_residence(blocks, wanted, can_work) or (
        lives and _unsatisfied_residence(blocks, wanted, lives, where_lived=True)
    ):
        named, sentence, start = refused
        return Answer(
            question,
            f"the posting requires residing in {min(named)}",
            evidence=(quote_line(sentence, start),),
        )
    place = wanted.place_reader.read(listing_location)
    if lives and (named := place.outside(lives)):
        local = _local_residence(blocks, wanted, place.countries)
        if local is not None:
            near = "in"
            if _names_locality(wanted, listing_location):
                near = f"near {listing_location}, in"
            return Answer(
                question,
                f"the posting requires residing {near} {min(named)}",
                evidence=(quote_line(*local),),
            )
    clock = (TIME_ZONE_MENTION, REQUIRED_TIME_ZONE)
    listed = next(
        (
            (clause, start)
            for _, clause, start in _required_clauses(blocks, clock, residence_required)
            if _lists_workable_states(clause, wanted)
        ),
        None,
    )
    if listed is not None:
        return Answer(
            question,
            note="the posting lists states to live in, in a country you will work remotely in",
            evidence=(quote_line(*listed),),
        )
    return Answer(question, note="no stated residence requirement rules this person out")


def _lists_workable_states(clause: str, wanted: JobSearchParameters) -> bool:
    """Return whether a clause names two or more states of a workable country and no country."""
    if states_nearness(clause):
        return False
    actionable = [place for place in wanted.locations if place.actionable]
    for country in _canonical_countries(wanted, actionable):
        rest = clause
        named = 0
        # A state counts only where it ends a list item, so "Kansas City" names no state,
        # and "New York, NY" names the city.
        for state in sorted(subdivision_names(country), key=len, reverse=True):
            if code := _CITY_STATE_CODES.get(state):
                city = rf"\b{re.escape(state)},\s*(?-i:{code})\b"
                rest = re.sub(city, " ", rest, flags=re.IGNORECASE)
            item_end = rf"\b{re.escape(state)}\b(?=\s*(?:[,.;:)]|\b(?:or|and)\b|$))"
            rest, found = re.subn(item_end, " ", rest, flags=re.IGNORECASE)
            named += found > 0
        if named >= 2:
            return not wanted.place_reader.read(rest).countries
    return False


def _local_residence(
    blocks: tuple[Block, ...], wanted: JobSearchParameters, listing: tuple[str, ...]
) -> tuple[str, int] | None:
    """Return the first clause requiring residence near the posting naming no other country."""
    aside = (TIME_ZONE_MENTION, REQUIRED_TIME_ZONE, PAY_CONTEXT, WORK_AUTHORIZATION_ALTERNATIVE)
    for sentence, clause, required in _required_clauses(blocks, aside, local_residence_required):
        if local_residence_required(sentence) is not None and set(
            wanted.place_reader.read(clause).countries
        ) <= set(listing):
            return clause, required
    return None


def _names_locality(wanted: JobSearchParameters, listing_location: str) -> bool:
    """Return whether a listing names more than a country or a remote label."""
    rest = listing_location.casefold()
    for (start, end), _ in sorted(wanted.country_table.spans_in(rest), reverse=True):
        rest = rest[:start] + rest[end:]
    return bool(re.sub(r"\bremote\b|[\W_]", "", rest))


def _eligible_countries(wanted: JobSearchParameters) -> set[str]:
    """Normalize actionable locations using the same country table as posting text."""
    return _countries(wanted, [place for place in wanted.locations if place.actionable])


def resident_countries(wanted: JobSearchParameters) -> set[str]:
    """Return the countries this person lives in or will move to; none without a marked home."""
    homes = [place for place in wanted.locations if place.resident]
    if not homes:
        return set()
    moves = [
        place for place in wanted.locations if wanted.arrangement_for(place.key).relocation is True
    ]
    return _countries(wanted, homes + moves)


def _countries(wanted: JobSearchParameters, places: list[Location]) -> set[str]:
    return {name.casefold() for name in _canonical_countries(wanted, places)}


def _canonical_countries(wanted: JobSearchParameters, places: list[Location]) -> set[str]:
    """Return the maintained names of the countries ``places`` were saved under."""
    table = wanted.country_table
    return {name for place in places for name in table.names_in(place.country) or (place.country,)}


def _unsatisfied_residence(
    blocks: tuple[Block, ...],
    wanted: JobSearchParameters,
    meeting: set[str],
    *,
    where_lived: bool = False,
) -> tuple[tuple[str, ...], str, int] | None:
    """Return the first residence requirement naming only countries outside ``meeting``.

    A clause about a time zone is always skipped, and with ``where_lived`` so is one
    about pay or one accepting work authorization or citizenship instead: none of
    them says where to live. A sentence naming any country in ``meeting`` abstains.
    """
    reader = wanted.place_reader
    clock = (TIME_ZONE_MENTION, REQUIRED_TIME_ZONE)
    aside = (*clock, PAY_CONTEXT, WORK_AUTHORIZATION_ALTERNATIVE) if where_lived else clock
    for sentence, clause, required in _required_clauses(blocks, aside, residence_required):
        if where_lived and _lists_workable_states(clause, wanted):
            continue
        named = reader.read(clause).outside(meeting)
        if named and reader.read(sentence).outside(meeting):
            return named, clause, required
    return None


def _required_clauses(
    blocks: tuple[Block, ...],
    aside: tuple[re.Pattern[str], ...],
    required: Callable[[str], int | None],
) -> Iterator[tuple[str, str, int]]:
    """Yield each sentence, clause, and offset where ``required`` finds a requirement."""
    lines = content_lines(blocks)
    for line, following in pairwise((*lines, "")):
        if answered_field(line, following):
            continue
        for sentence in sentence_pieces(line):
            # Semicolons only: a comma before "and" can sit inside the list of countries.
            for clause in sentence.split(";"):
                found = required(clause)
                if found is not None and not any(pattern.search(clause) for pattern in aside):
                    yield sentence, clause, found
