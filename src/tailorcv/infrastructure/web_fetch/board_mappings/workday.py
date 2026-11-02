"""Workday's posting record, as its CXS board listing and its posting-detail endpoint state it.

The board listing (one ``jobPostings`` item) and the posting detail
(``jobPostingInfo``) are different response shapes, so they get separate
declarations: ``WORKDAY_LISTING_MAPPING`` and ``WORKDAY_DETAIL_MAPPING``.

``WORKDAY_LISTING_MAPPING`` expects ``Posting.groups`` to carry ``host`` and
``site`` so it can build the posting's address from the record's relative
``externalPath``. ``WORKDAY_DETAIL_MAPPING`` expects ``Posting.groups`` to
carry ``remainder``: the URL-encoded path segment after ``/job/`` in the
request that was answered, used only to decide whether an acronym alias the
description defines is one this posting names itself by.
"""

from __future__ import annotations

import re
from typing import Any, cast
from urllib.parse import unquote

from tailorcv.domain.countries import DEFAULT_TABLE, SUBDIVISION_CODES, territory_name
from tailorcv.domain.places import PlaceReader, indexed_places
from tailorcv.domain.staged_facts import PostedDateGrade
from tailorcv.infrastructure.web_fetch.board_mappings.mapper import (
    BoardDeclaration,
    Posting,
    plain_description,
    posted_on,
    stated,
    stated_text,
)
from tailorcv.infrastructure.web_fetch.listing_facts import (
    ArrangementSignal,
    extended_locations,
    location_display,
)
from tailorcv.infrastructure.web_fetch.workday_arrangement import workday_arrangement_facts

_WORKDAY_PREFIX_COUNTRY = re.compile(r"^(?P<country>[A-Z]{2,3})---")
_WORKDAY_SUFFIX_COUNTRY = re.compile(r"-(?P<country>[A-Z]{2})$")
_PLACES = PlaceReader(DEFAULT_TABLE)


def workday_country_from_path(path: str) -> str | None:
    """Return a country only when Workday encodes it in a structured job path."""
    after_job = path.split("/job/", 1)[-1].lstrip("/")
    location_segment = unquote(after_job.split("/", 1)[0])
    prefix = _WORKDAY_PREFIX_COUNTRY.match(location_segment)
    if prefix:
        # "USA---Atlanta-GA" names its country up front; the trailing token is a state.
        code = prefix.group("country")
        if len(code) == 3:
            return DEFAULT_TABLE.named_in(code) or None
        if code not in SUBDIVISION_CODES:
            return territory_name(code)
        # A state or province code names its country when the town verifies under it, and
        # the namesake ISO country only when the town is indexed there and under no such state.
        town = location_segment[prefix.end() :].partition("---")[0]
        reading = _PLACES.read(f"{code} - {town}")
        if len(reading.explicit) == 1:
            return reading.explicit[0]
        places = indexed_places(town)
        if any(admin == code for _, admin in places):
            return None
        return territory_name(code) if any(country == code for country, _ in places) else None
    suffix = _WORKDAY_SUFFIX_COUNTRY.search(location_segment)
    if not suffix:
        return None
    code = suffix.group("country")
    # A state suffix ("Nashville-TN") is not a country, so Germany-DE and India-IN are lost too.
    if code in {"CA", "US"} or code in SUBDIVISION_CODES:
        return None
    return territory_name(code)


def _workday_requisition(path: str) -> str:
    """Return the requisition number a Workday posting path ends with, if any."""
    tail = path.rsplit("/", maxsplit=1)[-1]
    _, separator, requisition = tail.rpartition("_")
    return requisition if separator else ""


def _listing_path(posting: Posting) -> str:
    return stated_text(posting.record, ("externalPath",))


def _listing_places(posting: Posting) -> list[str]:
    """Return the listing's stated place plus any country its path encodes."""
    location = stated_text(posting.record, ("locationsText",))
    country = workday_country_from_path(_listing_path(posting))
    return [value for value in (location, country) if value]


def _listing_url(posting: Posting) -> str:
    path = _listing_path(posting)
    if not path:
        return ""
    host = posting.groups.get("host", "")
    site = posting.groups.get("site", "")
    root = posting.groups.get("root_url") or f"https://{host}/{site}"
    return f"{root}{path}"


def _listing_requisition_id(posting: Posting) -> str:
    return _workday_requisition(_listing_path(posting))


def _listing_arrangement(posting: Posting) -> ArrangementSignal:
    job = cast(dict[str, Any], posting.record)
    return workday_arrangement_facts(job, job, _listing_places(posting))


WORKDAY_LISTING_MAPPING = BoardDeclaration(
    platform="workday",
    title=("title",),
    url=_listing_url,
    location=("locationsText",),
    places=_listing_places,
    requisition_id=_listing_requisition_id,
    arrangement=_listing_arrangement,
)


_ACRONYM_DEFINITION = re.compile(
    r"(?P<words>(?:[A-Z][A-Za-z]*\s+){1,7}[A-Z][A-Za-z]*)\s*\((?P<acronym>[A-Z]{2,8})\)"
)
_ROLE_NOUNS = frozenset(
    {
        "administrator",
        "advisor",
        "analyst",
        "architect",
        "associate",
        "consultant",
        "coordinator",
        "designer",
        "developer",
        "director",
        "engineer",
        "lead",
        "manager",
        "officer",
        "partner",
        "representative",
        "scientist",
        "specialist",
        "supervisor",
        "technician",
    }
)


def _workday_role_aliases(description: str, identity: str) -> tuple[str, ...]:
    """Return role names the description defines with an acronym the posting names itself by.

    The expansion is the capitalized words whose initials spell the acronym and ends in a
    role noun. The acronym or expansion must appear in ``identity`` (title, requisition
    name), so a collaborator the body mentions never becomes this posting's role.
    """
    identity_tokens = set(re.split(r"[^A-Za-z0-9]+", identity))
    identity_words = " ".join(re.split(r"[^a-z0-9]+", identity.casefold()))
    aliases: list[str] = []
    for match in _ACRONYM_DEFINITION.finditer(description):
        acronym = match.group("acronym")
        words = match.group("words").split()[-len(acronym) :]
        if len(words) != len(acronym) or words[-1].casefold() not in _ROLE_NOUNS:
            continue
        if "".join(word[0] for word in words) != acronym:
            continue
        alias = " ".join(words)
        if acronym not in identity_tokens and f" {alias.casefold()} " not in f" {identity_words} ":
            continue
        if alias not in aliases:
            aliases.append(alias)
    return tuple(aliases)


def _detail_country(posting: Posting) -> str:
    record = posting.record
    return (
        territory_name(stated_text(record, ("jobPostingInfo", "country", "alpha2Code")))
        or territory_name(stated_text(record, ("jobRequisitionLocation", "country", "alpha2Code")))
        or stated_text(record, ("jobPostingInfo", "country", "descriptor"))
    )


def _detail_extra_entries(posting: Posting) -> list[str]:
    """Return every secondary or additional location entry Workday states, repeats kept."""
    entries: list[str] = []
    for field_name in ("secondaryLocations", "additionalLocations"):
        items = stated(posting.record, ("jobPostingInfo", field_name))
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict):
                location = str(item.get("location") or item.get("name") or "").strip()
            else:
                location = str(item or "").strip()
            if location:
                entries.append(location)
    return entries


def _detail_extra_locations(posting: Posting) -> list[str]:
    """Return every secondary or additional location Workday states, as display text."""
    return list(dict.fromkeys(_detail_extra_entries(posting)))


def _detail_place_entries(posting: Posting) -> int:
    """Count the primary and every extra entry; a listing's "N Locations" counts repeats."""
    primary = stated_text(posting.record, ("jobPostingInfo", "location"))
    return int(bool(primary)) + len(_detail_extra_entries(posting))


def _detail_locations(posting: Posting) -> tuple[str, tuple[str, ...]]:
    """Return Workday's primary display value and every stated detail location."""
    primary = stated_text(posting.record, ("jobPostingInfo", "location"))
    stated_values = ([primary] if primary else []) + _detail_extra_locations(posting)
    return primary, extended_locations(stated_values, (_detail_country(posting),))


def _detail_places(posting: Posting) -> list[str]:
    return list(_detail_locations(posting)[1])


def _detail_location(posting: Posting) -> str:
    primary, locations = _detail_locations(posting)
    return location_display(primary, locations)


def _detail_locations_heading(posting: Posting) -> str:
    return ", ".join(_detail_locations(posting)[1])


def _detail_time_type(posting: Posting) -> str:
    """Return the raw ``timeType`` value as published, whatever shape it takes."""
    return str(stated(posting.record, ("jobPostingInfo", "timeType")) or "")


def _detail_description(posting: Posting) -> str:
    return str(stated(posting.record, ("jobPostingInfo", "jobDescription")) or "")


def _detail_arrangement(posting: Posting) -> ArrangementSignal:
    info = stated(posting.record, ("jobPostingInfo",))
    return workday_arrangement_facts(
        {}, info if isinstance(info, dict) else {}, _detail_places(posting)
    )


def _detail_role_aliases(posting: Posting) -> tuple[str, ...]:
    """Alias identity is the title plus the URL path Workday served the posting under."""
    title = stated_text(posting.record, ("jobPostingInfo", "title"))
    remainder = unquote(posting.groups.get("remainder", ""))
    description = plain_description(_detail_description(posting))
    return _workday_role_aliases(description, f"{title} {remainder}")


WORKDAY_DETAIL_MAPPING = BoardDeclaration(
    platform="workday",
    title=("jobPostingInfo", "title"),
    places=_detail_places,
    place_entries=_detail_place_entries,
    location=_detail_location,
    description=_detail_description,
    arrangement=_detail_arrangement,
    employment_types=("jobPostingInfo", "timeType"),
    role_aliases=_detail_role_aliases,
    heading=(("jobPostingInfo", "title"), _detail_locations_heading, _detail_time_type),
    # startDate moves forward when a posting is reposted or refreshed.
    posted=posted_on(("jobPostingInfo", "startDate"), PostedDateGrade.BOARD_DATE),
)
