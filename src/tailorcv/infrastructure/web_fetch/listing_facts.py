"""Publisher-stated listing facts shared by board and single-posting readers."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from tailorcv.application.discovery.source_reading import PostingCheck
from tailorcv.domain.arrangement_grammar import (
    OFFICE_ARRANGEMENT_WORDS,
    attendance_statement,
    plain,
    remote_offer_statement,
    sentences,
)
from tailorcv.domain.staged_facts import (
    FreshnessState,
    ListingIdentityTrust,
    PublishedFact,
    StagedListingFacts,
)
from tailorcv.shared.quantities import (
    NUMBER,
    read_figure,
)
from tailorcv.shared.text import sanitize

MARKET_COUNT = re.compile(
    rf"^\s*(?P<count>multiple|{NUMBER})\s+(?:locations?|offices)(?:\s+available)?"
    r"(?:\s*\([^()]*\))?\s*$"
    rf"|^\s*\+?\s*(?P<more>{NUMBER})\s+more(?:\s+(?:locations?|offices))?\s*$",
    re.IGNORECASE,
)
# "Multiple locations" states no count, so it reads as the fewest it can mean.
_MULTIPLE = 2


def market_count(location: str) -> int | None:
    """Return the stated count from an "N Locations" summary, or None."""
    match = MARKET_COUNT.match(sanitize(location))
    if match is None:
        return None
    stated = match.group("count") or match.group("more")
    if stated.casefold() == "multiple":
        return _MULTIPLE
    figure = read_figure(stated)
    return None if figure is None else int(figure.floor)


# "remote", "remotely", and "remote-first" count; a place like "Remoteville" does not.
REMOTE_PLACE = re.compile(r"\bremote(?:ly)?\b", re.IGNORECASE)
# Notes the body readers file as unresolved arrangement values; the body stays their evidence.
_REMOTE_OFFER_NOTE = "publisher body offers remote: "
_ATTENDANCE_NOTE = "publisher body states attendance: "
_TAG_CONFLICT_NOTE = "conflicting LinkedIn tags: "
_ROLE_CONFLICT_NOTE = "LinkedIn tag {tag} conflicts with role statement: "
_ROLE_CONFLICT_PREFIX = re.compile(
    re.escape(_ROLE_CONFLICT_NOTE).replace(re.escape("{tag}"), r"\S+")
)


def body_arrangement_note(value: str) -> bool:
    """Return whether an unresolved arrangement value is a note a body reader filed."""
    return value.startswith((_REMOTE_OFFER_NOTE, _ATTENDANCE_NOTE, _TAG_CONFLICT_NOTE)) or bool(
        _ROLE_CONFLICT_PREFIX.match(value)
    )


# A tenant label can wrap the word in its own text ("Hybrid (8 days/month)").
_ATTENDANCE_ARRANGEMENT = re.compile(
    rf"\b(?:hybrid|{OFFICE_ARRANGEMENT_WORDS}|office(?:[- ]based)?)\b", re.IGNORECASE
)

# LinkedIn Job Wrapping documents the hyphenated forms. Some publisher bodies
# omit that separator, so accept only those observed equivalent spellings too.
LINKEDIN_ARRANGEMENT_TAGS: tuple[tuple[str, str], ...] = (
    ("#li-remote", "remote"),
    ("#liremote", "remote"),
    ("#li-hybrid", "hybrid"),
    ("#lihybrid", "hybrid"),
    ("#li-onsite", "onsite"),
    ("#lionsite", "onsite"),
)
_LINKEDIN_TAG = r"#\s?li(?:[-_]|\s+-\s*)?(?:remote|hybrid|on-?site)"
_LINKEDIN_ARRANGEMENT_PATTERN = re.compile(
    rf"(?<![a-z0-9_-])(?P<tag>{_LINKEDIN_TAG})(?![a-z0-9_-])", re.IGNORECASE
)
_LINKEDIN_TAG_ARRANGEMENTS = dict(LINKEDIN_ARRANGEMENT_TAGS)
_LINKEDIN_ARRANGEMENT_TOKEN = re.compile(rf"{_LINKEDIN_TAG}[a-z0-9_-]*", re.IGNORECASE)


def _linkedin_arrangement(tag: str) -> str:
    """Return the arrangement a tag names, however its separators are written."""
    return _LINKEDIN_TAG_ARRANGEMENTS[re.sub(r"[\s_-]", "", tag.casefold())]


_ROLE_ARRANGEMENT_REFERENCE = re.compile(r"\b(?:this|the) (?:role|position|job)\b", re.IGNORECASE)
_ROLE_ARRANGEMENT_WORDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("remote", re.compile(r"\b(?:fully[- ]?)?remote\b", re.IGNORECASE)),
    ("hybrid", re.compile(r"\bhybrid\b", re.IGNORECASE)),
    ("onsite", re.compile(r"\bon[- ]?site\b", re.IGNORECASE)),
)


EXHAUSTIVE_LOCATION_PROVIDERS = frozenset(
    {
        "greenhouse",
        "lever",
        "ashby",
        "teamtailor",
        "himalayas",
        "dayforce",
        "workday",
    }
)

# An unmatched provenance here never proves a posting closed to a saved market.
NON_EXHAUSTIVE_LOCATION_PROVIDERS = frozenset(
    {
        "schema_org",
        "publisher_html",
        "smartrecruiters",
        "workable",
        "recruitee",
        "bamboohr",
        "gem",
        "telus",
        "ukg",
        "pinpoint",
        "paylocity",
        "personio",
        "comeet",
        "rippling",
        "dover",
        "adp",
        "breezy",
        "jazzhr",
        "jobvite",
        "hibob",
        "zoho",
        "avature",
        "successfactors",
        "phenom",
        "icims",
        "arbeitnow",
        "oracle",
        "publisher_index",
        "tbk",
        "alignerr",
        "cornerstone",
        "jobsyn",
        "darwinbox",
        "taleo",
        "fxrecruiter",
        "ycombinator",
        "eightfold",
        "jpaginate",
        "trinethire",
        "clearcompany",
        "getro",
        "taleobusiness",
        "peoplesoft",
        "shopify",
        "deel",
    }
)


def exhaustive_location_provider(provenance: str) -> bool:
    """Return whether a location fact's provenance names an exhaustive provider."""
    return provenance.removesuffix(".location") in EXHAUSTIVE_LOCATION_PROVIDERS


def ashby_places(job: dict[str, Any]) -> list[str]:
    """Return the places one Ashby posting states, secondary entries being objects."""
    stated = [str(job.get("location") or "").strip()]
    for entry in job.get("secondaryLocations") or []:
        place = entry.get("location") if isinstance(entry, dict) else entry
        stated.append(str(place or "").strip())
    return [place for place in dict.fromkeys(stated) if place]


def ashby_countries(job: dict[str, Any]) -> list[str]:
    """Return each country an Ashby posting's postal addresses name outright."""
    entries = [entry for entry in job.get("secondaryLocations") or [] if isinstance(entry, dict)]
    stated = [_ashby_postal_country(record.get("address")) for record in (job, *entries)]
    return [country for country in dict.fromkeys(stated) if country]


def _ashby_postal_country(address: Any) -> str:
    postal = address.get("postalAddress") if isinstance(address, dict) else None
    return str(postal.get("addressCountry") or "").strip() if isinstance(postal, dict) else ""


@dataclass(frozen=True)
class ArrangementSignal:
    """One publisher arrangement answer, or source text that leaves it unresolved."""

    value: str | None = None
    unresolved: tuple[str, ...] = ()
    # Trailing provenance segment after "<platform>."; names the field or phrase
    # an arrangement value was read from.
    provenance_suffix: str = "arrangement"


def stated_place(location: str) -> str:
    """Return the place a listing states, or empty when it only states a count."""
    return "" if market_count(location) is not None else location


def values(value: Any) -> tuple[str, ...]:
    """Normalize a publisher scalar or list without interpreting it."""
    items = value if isinstance(value, (list, tuple)) else [value]
    return tuple(str(item).strip() for item in items if str(item or "").strip())


def location_display(primary: str, published: Any) -> str:
    """Return the single place a listing displays, skipping a count summary.

    Every other published place stays in the locations fact.
    """
    kept = stated_place(primary)
    if kept:
        return kept
    real = (value for value in values(published) if market_count(value) is None)
    return next(real, "")


def extended_locations(stated: Sequence[str], additions: Sequence[str]) -> tuple[str, ...]:
    """Return stated places followed by every addition they do not already state."""
    return tuple(dict.fromkeys(value for value in (*stated, *additions) if value))


def workday_detail_facts(original: StagedListingFacts, checked: PostingCheck) -> StagedListingFacts:
    """Keep a place the board listing stated and the detail response did not repeat.

    A primary-only detail read for an "N Locations" board listing never
    resolves it; marking that complete would let a foreign primary hold the
    row out alone, so completeness requires N stated entries or N read values.
    """
    closing_date = checked.facts.closing_date
    if (closing_date is None or not closing_date.statements) and original.closing_date is not None:
        closing_date = original.closing_date
    values = extended_locations(checked.facts.locations.values, original.locations.values)
    stated_count = next(
        (
            count
            for value in original.locations.unresolved_values
            if (count := market_count(value)) is not None
        ),
        None,
    )
    if stated_count is not None and max(len(values), checked.location_entries) < stated_count:
        return replace(
            checked.facts,
            closing_date=closing_date,
            locations=replace(
                checked.facts.locations,
                values=values,
                unresolved_values=original.locations.unresolved_values,
                complete=False,
            ),
        )
    return replace(
        checked.facts,
        closing_date=closing_date,
        locations=replace(checked.facts.locations, values=values),
    )


def lever_arrangement_values(
    workplace_type: Any, locations: Any, description: str = ""
) -> tuple[list[str], list[str]]:
    """Separate a structured hybrid/on-site value from a publisher remote offer.

    A remote place alongside the structured value, or a remote offer stated in
    the body, both mean the structured value cannot be trusted as the whole
    answer, so it moves to ``unresolved_values`` for a body-backed decision.
    """
    arrangements = values(workplace_type)
    if not any(_ATTENDANCE_ARRANGEMENT.search(value) for value in arrangements):
        return list(arrangements), []
    remote_location = any(REMOTE_PLACE.search(value) for value in values(locations))
    offer = remote_offer_statement(description) if description else None
    if remote_location or offer is not None:
        evidence = offer or "a remote location listed alongside a hybrid/on-site value"
        return [], [*arrangements, f"{_REMOTE_OFFER_NOTE}{evidence}"]
    return list(arrangements), []


def remote_checked_arrangement(value: Any, locations: Any, content: Any) -> ArrangementSignal:
    """Return a structured arrangement, left unresolved when a remote offer contradicts it.

    ``content`` is the body text, or a list of its published sections.
    """
    sections = content if isinstance(content, (list, tuple)) else [content]
    text = "\n\n".join(section for section in sections if isinstance(section, str))
    accepted, unresolved = lever_arrangement_values(value, locations, text)
    if unresolved:
        return ArrangementSignal(unresolved=tuple(unresolved))
    # Several stated arrangements are not one answer, so none is taken as the fact.
    if len(accepted) > 1:
        return ArrangementSignal(unresolved=tuple(accepted))
    return ArrangementSignal(value=accepted[0] if accepted else None)


def linkedin_arrangement_signal(content: str) -> ArrangementSignal:
    """Read an exact LinkedIn tag without selecting contradictory publisher body text."""
    text = plain(content)
    tags = tuple(
        dict.fromkeys(
            (match.group("tag"), _linkedin_arrangement(match.group("tag")))
            for match in _LINKEDIN_ARRANGEMENT_PATTERN.finditer(text)
        )
    )
    if not tags:
        return ArrangementSignal()
    role_lines = tuple(
        line for line in text.splitlines() if _ROLE_ARRANGEMENT_REFERENCE.search(line)
    )
    role_arrangements = {
        arrangement
        for line in role_lines
        for sentence in sentences(line)
        for arrangement in _sentence_arrangements(sentence)
    }
    arrangements = {arrangement for _, arrangement in tags}
    if len(arrangements) > 1:
        if len(role_arrangements) == 1 and role_arrangements <= arrangements:
            return ArrangementSignal(value=next(iter(role_arrangements)))
        tags_text = ", ".join(tag for tag, _ in tags)
        return ArrangementSignal(unresolved=(f"{_TAG_CONFLICT_NOTE}{tags_text}",))
    tag, arrangement = tags[0]
    if role_arrangements and (len(role_arrangements) != 1 or arrangement not in role_arrangements):
        return ArrangementSignal(
            unresolved=(_ROLE_CONFLICT_NOTE.format(tag=tag) + _role_arrangement_quote(role_lines),)
        )
    return ArrangementSignal(value=arrangement)


def _sentence_arrangements(sentence: str) -> set[str]:
    """Return the arrangements one role sentence names; a hybrid one's office days stay hybrid."""
    named = {name for name, pattern in _ROLE_ARRANGEMENT_WORDS if pattern.search(sentence)}
    return {"hybrid"} if "hybrid" in named else named


def _role_arrangement_quote(role_lines: tuple[str, ...]) -> str:
    """Quote the sentences that place the role, since a collapsed body is one line."""
    named = [
        sentence
        for line in role_lines
        for sentence in sentences(line)
        if any(pattern.search(sentence) for _, pattern in _ROLE_ARRANGEMENT_WORDS)
    ]
    quoted = [s for s in named if _ROLE_ARRANGEMENT_REFERENCE.search(s)] or named
    return " ".join(quoted)


def without_linkedin_arrangement_tags(content: str) -> str:
    """Remove LinkedIn tag lookalikes before ordinary phrase inference."""
    return _LINKEDIN_ARRANGEMENT_TOKEN.sub("", content)


# A location value's own trailing word can state attendance: "Palo Alto -
# Hybrid", "NY office". Anchored at the end, so a place name like "Remote
# Oaks" never reads as an arrangement.
_LOCATION_ARRANGEMENT_WORD = re.compile(
    rf"\b(hybrid|remote(?:[\s-]+office)?|work[\s-]+from[\s-]+home|{OFFICE_ARRANGEMENT_WORDS}|office)"
    r"\)?[\s.]*$",
    re.IGNORECASE,
)
_REMOTE_LABEL = r"(?:remote|work[\s-]+from[\s-]+home|wfh)"
# A remote word leading the label needs punctuation after it, so "Remote Oaks" stays a place.
_LOCATION_LEADING_REMOTE = re.compile(rf"^\s*{_REMOTE_LABEL}\s*[-,:(]", re.IGNORECASE)
_LOCATION_REMOTE_ONLY = re.compile(
    rf"^\s*{_REMOTE_LABEL}(?:\s+(?:nationwide|location))?[\s.]*$", re.IGNORECASE
)
# "Home Office" is an ATS value for working from home, so its trailing "office"
# must not read as onsite. It is left unstated rather than mapped to remote,
# which would assert an arrangement the publisher did not name.
_LOCATION_HOME_OFFICE = re.compile(r"\b(?:home|virtual)[\s-]+office\)?[\s.]*$", re.IGNORECASE)
_LOCATION_ARRANGEMENT_NAMES: dict[str, str] = {
    "hybrid": "hybrid",
    "remote": "remote",
    "remoteoffice": "remote",
    "workfromhome": "remote",
    "onsite": "onsite",
    "inoffice": "onsite",
    "inperson": "onsite",
    "office": "onsite",
}


def remote_only_location(location: str) -> bool:
    """Return whether a location value states remote work and no place at all."""
    return _LOCATION_REMOTE_ONLY.match(sanitize(location)) is not None


def remote_label_place(location: str) -> str:
    """Return the place a leading remote label is attached to ("Remote-MO" gives "MO")."""
    text = sanitize(location)
    found = _LOCATION_LEADING_REMOTE.match(text)
    return text[found.end() :].strip(" )") if found else ""


def location_stated_arrangement(location: str) -> str | None:
    """Return the arrangement a location value states as its trailing or leading words."""
    trimmed = sanitize(location).rstrip()
    if _LOCATION_HOME_OFFICE.search(trimmed):
        return None
    match = _LOCATION_ARRANGEMENT_WORD.search(trimmed)
    if match is not None:
        return _LOCATION_ARRANGEMENT_NAMES[re.sub(r"[\s-]", "", match.group(1).casefold())]
    if remote_only_location(trimmed) or _LOCATION_LEADING_REMOTE.match(trimmed):
        return "remote"
    return None


def _body_contradiction(arrangements: tuple[str, ...], description: str) -> str | None:
    """Return the body statement that contradicts a location-stated arrangement, or None."""
    if "remote" in arrangements:
        tag = linkedin_arrangement_signal(description)
        if tag.unresolved:
            return tag.unresolved[0]
        if tag.value is not None and tag.value not in arrangements:
            return f"{_ATTENDANCE_NOTE}LinkedIn {tag.value} tag"
        # A capped cadence refuses nobody, yet it still puts a remote suffix in doubt.
        text = without_linkedin_arrangement_tags(description)
        found = attendance_statement(text, capped=False)
        return None if found is None else f"{_ATTENDANCE_NOTE}{found[1]}"
    offer = remote_offer_statement(description)
    return None if offer is None else f"{_REMOTE_OFFER_NOTE}{offer}"


def published_listing_facts(
    platform: str,
    *,
    identity_trust: ListingIdentityTrust = ListingIdentityTrust.PUBLISHER,
    locations: Any = None,
    description: str = "",
    arrangements: Any = None,
    arrangements_unresolved: Any = None,
    employment_types: Any = None,
    compensation: Any = None,
    locations_complete: bool = True,
    arrangement_provenance: str = "arrangement",
    role_aliases: Any = None,
    freshness: FreshnessState = FreshnessState.UNKNOWN,
) -> StagedListingFacts:
    """Build facts only from fields published in one structured listing response."""
    location_values = values(locations)
    arrangement_values = values(arrangements)
    unresolved_arrangements = values(arrangements_unresolved)
    from_location: tuple[str, ...] = ()
    partial_from_location: tuple[str, ...] = ()
    if not arrangement_values and not unresolved_arrangements:
        stated = [location_stated_arrangement(value) for value in location_values]
        named = tuple(dict.fromkeys(value for value in stated if value is not None))
        # Only every location speaking for itself settles the listing. When some
        # stay silent, the named ones are unresolved so a partly described
        # multi-location listing cannot be hard-gated on one location's suffix.
        if named and all(value is not None for value in stated):
            from_location = named
        else:
            partial_from_location = named
        # A location suffix the posting's own body contradicts settles nothing.
        if from_location and (against := _body_contradiction(from_location, description)):
            from_location, partial_from_location = (), (*from_location, against)
    return StagedListingFacts(
        identity_trust=identity_trust,
        role_aliases=PublishedFact(
            values=values(role_aliases),
            provenance=f"{platform}.job_description" if role_aliases is not None else "",
            complete=role_aliases is not None,
        ),
        freshness=freshness,
        locations=PublishedFact(
            values=tuple(value for value in location_values if market_count(value) is None),
            unresolved_values=tuple(
                value for value in location_values if market_count(value) is not None
            ),
            provenance=f"{platform}.location",
            complete=locations_complete
            and bool(location_values)
            and not any(market_count(value) is not None for value in location_values),
        ),
        arrangements=PublishedFact(
            values=from_location or arrangement_values,
            unresolved_values=unresolved_arrangements or partial_from_location,
            provenance=(
                f"{platform}.location_phrase.arrangement"
                if from_location or partial_from_location
                else f"{platform}.{arrangement_provenance}"
            ),
            complete=bool(from_location)
            or (
                arrangements is not None
                and not unresolved_arrangements
                and not partial_from_location
            ),
        ),
        employment_types=PublishedFact(
            values=values(employment_types),
            provenance=f"{platform}.employment_type",
            complete=employment_types is not None,
        ),
        compensation=PublishedFact(
            values=values(compensation),
            provenance=f"{platform}.compensation",
            complete=compensation is not None,
        ),
    )
