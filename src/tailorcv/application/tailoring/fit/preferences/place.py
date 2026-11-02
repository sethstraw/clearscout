"""Place and work-arrangement readings of a posting against the saved search."""

from __future__ import annotations

import re

from tailorcv.application.tailoring.fit.lexicon import (
    HYBRID_PLACE,
    LOCATION_LABEL,
    OFFICE_PRESENCE,
    REMOTE_LISTING_WORDS,
    office_days,
    outside_market,
    refuses_remote,
)
from tailorcv.application.tailoring.fit.structure import (
    Answer,
    Block,
    SectionKind,
    content_lines,
    posting_title,
    quote_line,
    unasked_answer,
)
from tailorcv.domain.search_parameters import (
    OUTSIDE_DECLARED,
    Acceptance,
    Arrangement,
    JobSearchParameters,
    Location,
    WorkEligibility,
)


def place_fit(
    posting: str,
    blocks: tuple[Block, ...],
    wanted: JobSearchParameters,
    listing: str = "",
    *,
    remote: bool = False,
) -> tuple[Answer[str], str | None]:
    """Say which declared place a posting is in, and return that place's key.

    Matched against the strings this person wrote; a remote posting naming none
    of them may also be placed by the country its listed city is in. Two declared
    places in one posting is a conflict rather than a pick. ``listing`` is the
    location the board listed the posting under, and ``remote`` says the posting
    offers remote work.
    """
    question = "Where you could work this"
    if not wanted.locations:
        return unasked_answer(question), None
    text, matched = _place_text(posting, blocks, wanted, listing)
    if not matched and remote:
        placed = _remote_place(posting, blocks, wanted, listing, question)
        if placed is not None:
            return placed
    if not matched:
        listed = ", ".join(location.key for location in wanted.locations)
        # The empty residence rule keeps this out of every downstream rule that
        # asks where the reader may work.
        return (
            Answer(
                question,
                f": {OUTSIDE_DECLARED}",
                note=f"the posting names none of your places ({listed})",
            ),
            None,
        )
    place = matched[0]
    if len({location.key for location in matched}) > 1:
        resolved = _most_specific(matched, text)
        if resolved is None:
            return _several(question, matched, text)
        place = resolved
    evidence = tuple(
        quote_line(line)
        for line in text.splitlines()
        if any(n.casefold() in line.casefold() for n in place.names())
    )[:1]
    verdict = f"{place.key}: {place.eligibility.value}"
    return Answer(question, verdict, evidence=evidence), place.key


def _remote_place(
    posting: str,
    blocks: tuple[Block, ...],
    wanted: JobSearchParameters,
    listing: str,
    question: str,
) -> tuple[Answer[str], str | None] | None:
    """Answer a remote posting no declared place names, or None to read it as outside."""
    # Page chrome and office lists outside the content blocks name cities that
    # place nothing, so a city is read only from the listing.
    lines = (posting_title(posting), *content_lines(blocks), listing)
    if wanted.place_reader.read("\n".join(outside_market(line) for line in lines)).explicit:
        return None
    inferred = set(wanted.place_reader.read(listing).inferred)
    # A region ("EMEA") or unresolved city left in the listing still names somewhere.
    if not inferred and not REMOTE_LISTING_WORDS.sub("", listing).strip(" -,/()|:.%"):
        return (
            Answer(
                question,
                note="the posting offers remote work and names no country, so which of "
                "your markets it hires in stays open",
            ),
            None,
        )
    if len(inferred) != 1:
        return None
    country = next(iter(inferred))
    matched = wanted.matching_locations(country)
    if len(matched) != 1 and (resolved := _most_specific(matched, country)) is not None:
        matched = (resolved,)
    if len(matched) != 1:
        return None
    place = matched[0]
    # A suggested country never refuses a posting, so an ineligible match reads as outside.
    if place.eligibility is WorkEligibility.INELIGIBLE:
        return None
    return (
        Answer(
            question,
            f"{place.key}: {place.eligibility.value}",
            evidence=(quote_line(f"listing location: {listing}"),),
            note=f"the listed city is in {country}",
        ),
        place.key,
    )


def _place_text(
    posting: str, blocks: tuple[Block, ...], wanted: JobSearchParameters, listing: str = ""
) -> tuple[str, tuple[Location, ...]]:
    """Return the text a posting's place is read from, and the declared places it names."""
    # Boards often encode the market in the title.
    text = "\n".join((posting_title(posting), *content_lines(blocks)))
    matched = wanted.matching_locations(text)
    if not matched and listing:
        text = f"listing location: {listing}"
        matched = wanted.matching_locations(text)
    if not matched:
        # Employers state the hiring place in the sections a requirement reading skips:
        # the about-us blurb and the pay paragraph both name the country.
        text = "\n".join(outside_market(line) for line in posting.splitlines())
        matched = wanted.matching_locations(text)
    return text, matched


def _several(
    question: str, matched: tuple[Location, ...], text: str
) -> tuple[Answer[str], str | None]:
    """Answer a posting open to several declared places.

    Returns no key, and states a standing only when every candidate place
    carries the same one.
    """
    named = sorted({location.key for location in matched})
    standings = {location.eligibility for location in matched}
    if len(standings) > 1:
        return (
            Answer(
                question,
                note=f"the posting names more than one of your places: {', '.join(named)}",
                conflict=True,
            ),
            None,
        )
    evidence = tuple(
        quote_line(line)
        for line in text.splitlines()
        for location in matched
        if any(name.casefold() in line.casefold() for name in location.names())
    )[:1]
    verdict = f"{' or '.join(named)}: {next(iter(standings)).value}"
    return Answer(question, verdict, evidence=evidence), None


def _most_specific(matched: tuple[Location, ...], text: str) -> Location | None:
    """Return the one declared place a posting names, or None when it names several.

    Declared places nest: a country match also matches every region and metro
    inside it. Places in different countries do not nest.
    """
    haystack = text.casefold()
    tiers = {location.key: location.named_tier(haystack) for location in matched}
    named = [location for location in matched if tiers[location.key] is not None]
    if not named or len({location.country.casefold() for location in named}) > 1:
        return None
    finest = max(tier for location in named if (tier := tiers[location.key]) is not None)
    candidates = {
        location.key: location
        for location in named
        if tiers[location.key] == finest and location.granularity == finest
    }
    return next(iter(candidates.values())) if len(candidates) == 1 else None


def arrangement_fit(
    location: Answer[str],
    posting: str,
    blocks: tuple[Block, ...],
    wanted: JobSearchParameters,
    place: str | None = None,
    *,
    arrangement_lines: tuple[str, ...],
) -> Answer[str]:
    """Say whether the stated arrangement is one this person wants.

    Refuses to guess in both directions: a posting that named no arrangement is
    not on-site, and a person who answered none of the three questions has not
    refused any of them. ``place`` selects the arrangement that applies, and a
    posting in a place nobody declared falls back to the general one.
    ``arrangement_lines`` are the lines ``location`` was read from.
    """
    question = "Arrangement you want"
    arrangement = wanted.arrangement_for(place)
    if not arrangement.stated:
        return unasked_answer(question)
    if location.value is None and not location.conflict:
        ruled_out = next((line for line in content_lines(blocks) if refuses_remote(line)), None)
        if ruled_out is not None and all(
            arrangement.acceptance_of(name) is Acceptance.REFUSED for name in ("hybrid", "on site")
        ):
            evidence = (quote_line(ruled_out),)
            return Answer(
                question,
                Acceptance.REFUSED.value,
                evidence=evidence,
                note="the posting rules out remote work, the one arrangement you accept: "
                + _quoted(evidence),
            )
    if location.value is None:
        because = location.because() or "no evidence"
        return Answer(
            question,
            note=f"the posting states no arrangement to compare ({because})",
            conflict=location.conflict,
        )
    acceptance = arrangement.acceptance_of(location.value)
    if acceptance is None:
        return Answer(
            question, note=f"the posting is {location.value} and you have not said either way"
        )
    if _refused_hybrid_unplaced(acceptance, location, blocks, arrangement_lines):
        return Answer(
            question,
            evidence=location.evidence,
            note="the posting is hybrid but names no office place or office days: "
            + _quoted(location.evidence),
        )
    if acceptance is Acceptance.REFUSED and (
        metro := _accepting_metro(location.value, posting, blocks, wanted, place)
    ):
        return Answer(
            question,
            evidence=location.evidence,
            note=f"the posting is {location.value} and names only {metro.region}, "
            f"where your {metro.key} place accepts it",
        )
    # The arrangement gate records this note as its refusal reason.
    note = (
        f"the posting is {location.value}, which you refuse: {_quoted(location.evidence)}"
        if acceptance is Acceptance.REFUSED
        else ""
    )
    # A remote reading from anything but a "Location:" label offers remote work, so an
    # office cadence beside it is the other option, not this role's limit.
    offered = location.value == "remote" and not any(
        LOCATION_LABEL.match(quote) for quote in location.evidence[:1]
    )
    days = None if offered else _office_days(blocks)
    limit = arrangement.maximum_office_days
    if days is not None and limit is not None and days > limit:
        return Answer(
            question,
            f"{acceptance.value}, but {days} office days against your {limit}",
            evidence=location.evidence,
            note=note,
        )
    return Answer(question, acceptance.value, evidence=location.evidence, note=note)


def _refused_hybrid_unplaced(
    acceptance: Acceptance,
    location: Answer[str],
    blocks: tuple[Block, ...],
    arrangement_lines: tuple[str, ...],
) -> bool:
    """Return whether a refused hybrid reading names no office place or office days.

    Such a "hybrid" may describe the work rather than attendance.
    """
    if acceptance is not Acceptance.REFUSED or location.value != "hybrid":
        return False
    lines = (*(block.heading for block in blocks), *content_lines(blocks), *arrangement_lines)
    if _stated_office_days(lines) is not None:
        return False
    boilerplate = {
        line
        for block in blocks
        if block.kind is SectionKind.BOILERPLATE
        for line in (block.heading, *block.lines)
    }
    # Compensation scope names locations without placing the role there; an explicit
    # hybrid-role statement in the same section still carries its own placement.
    return not any(
        HYBRID_PLACE.search(line)
        for line in lines
        if line not in boilerplate or re.search(r"\bhybrid\b", line, re.IGNORECASE)
    )


def _quoted(evidence: tuple[str, ...]) -> str:
    """Return the posting clauses an answer rests on, each in quotation marks."""
    return "; ".join(f'"{quote}"' for quote in evidence)


def _accepting_metro(
    arrangement: str,
    posting: str,
    blocks: tuple[Block, ...],
    wanted: JobSearchParameters,
    place: str | None,
) -> Location | None:
    """Return a declared metro accepting ``arrangement`` that the posting names only by region.

    The job may be in that metro or elsewhere in the region, so neither place decides.
    """
    chosen = wanted.location(place) if place else None
    text, _ = _place_text(posting, blocks, wanted)
    days = _office_days(blocks)
    return next(
        (
            metro
            for metro in wanted.metros_named_by_region(text)
            if (chosen is None or metro.country.casefold() == chosen.country.casefold())
            and _accepts_within(wanted.arrangement_for(metro.key), arrangement, days)
        ),
        None,
    )


def _accepts_within(accepting: Arrangement, arrangement: str, days: int | None) -> bool:
    """Return whether ``accepting`` takes ``arrangement`` at the weekly office days stated."""
    limit = accepting.maximum_office_days
    return accepting.acceptance_of(arrangement) in (
        Acceptance.PREFERRED,
        Acceptance.ACCEPTABLE,
    ) and (days is None or limit is None or days <= limit)


def _office_days(blocks: tuple[Block, ...]) -> int | None:
    """Return the weekly office days a posting states, when it states them."""
    return _stated_office_days(content_lines(blocks))


def _stated_office_days(lines: tuple[str, ...]) -> int | None:
    """Return the weekly office days these lines state, when they state them."""
    posting = "\n".join(lines)
    for line in lines:
        if OFFICE_PRESENCE.search(line) is None:
            continue
        if (days := office_days(line, posting=posting)) is not None:
            return days
    return None
