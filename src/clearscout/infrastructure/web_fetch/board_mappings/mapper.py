"""Map one published board record onto staged listing fields from a declaration.

A declaration names where each field lives in a board's record, as a key path,
or names the typed function that derives it. It holds no conditions of its own,
so every rule about what a publisher value means stays in a derivation.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from copy import copy
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any
from xml.etree.ElementTree import Element, tostring

from clearscout.application.discovery.posting_text import publisher_posted_date
from clearscout.application.discovery.source_reading import PostingCheck, SourceListing
from clearscout.domain.artifacts import PostingSource
from clearscout.domain.staged_facts import (
    ListingIdentityTrust,
    PostedDateGrade,
    PublishedFact,
    PublisherPostedDate,
    StagedListingFacts,
)
from clearscout.infrastructure.web_fetch.ingest import strip_tags
from clearscout.infrastructure.web_fetch.ingest.job_posting_markup import BaseSalaryMarkup
from clearscout.infrastructure.web_fetch.listing_facts import (
    ArrangementSignal,
    published_listing_facts,
    values,
)
from clearscout.shared.text import sanitize

# Enough of a description for screening to find a responsibility, not a body.
_LISTING_SUMMARY_CHARS = 4000
EMPLOYER_CHARS = 300

type Record = Mapping[str, Any] | Element
type Path = tuple[str, ...]


@dataclass(frozen=True)
class Posting:
    """One publisher record, the configured employer, and the address it was read under."""

    record: Record
    employer: str = ""
    groups: Mapping[str, str] = field(default_factory=dict)
    # The configured name wins unless the declaration explicitly prefers a
    # stated publisher name.
    employer_is_authoritative: bool = False


type Derivation[T] = Callable[[Posting], T]
type Source[T] = Path | Derivation[T]


@dataclass(frozen=True)
class BoardDeclaration:
    """Where one board's record states each listing field, or the function deriving it.

    ``arrangement`` takes a derivation and never a path, so a publisher value
    always passes through its platform's rule before it becomes a fact.
    """

    platform: str
    title: Source[str]
    places: Source[Any]
    # Whether the stated places are the posting's whole list; absent means they are.
    locations_complete: Derivation[bool] | None = None
    # How many place entries the record states before repeated labels collapse;
    # absent means one per distinct place.
    place_entries: Derivation[int] | None = None
    url: Source[str] | None = None
    location: Source[str] | None = None
    requisition_id: Source[str] | None = None
    employer: Source[str] | None = None
    description: tuple[Path, ...] | Derivation[str] | None = None
    arrangement: Derivation[ArrangementSignal] | None = None
    employment_types: Source[Any] | None = None
    compensation: Source[Any] | None = None
    role_aliases: Derivation[Sequence[str]] | None = None
    posted: Derivation[PublisherPostedDate | None] | None = None
    # The lines a retained body states before the description. A board sweep and a
    # single-posting read digest the same body, so both must compose it identically.
    heading: tuple[Source[str], ...] = ()
    prefer_publisher_employer: bool = False


@dataclass(frozen=True)
class MappedPosting:
    """The listing fields one record states, with its description as paragraphs."""

    title: str
    url: str
    employer: str
    location: str
    requisition_id: str
    facts: StagedListingFacts
    description: str
    place_entries: int = 0


def stated(record: Any, path: Path) -> Any:
    """Return the value a record states at a key path, or None when any step is absent."""
    value = record
    for key in path:
        if isinstance(value, Element):
            found = value.find(key)
            value = None if found is None else found.text
        elif isinstance(value, Mapping):
            value = value.get(key)
        else:
            return None
    return value


def stated_text(record: Any, path: Path) -> str:
    """Return a stated scalar as stripped text; a nested object is not a scalar."""
    value = stated(record, path)
    if value is None or isinstance(value, (Mapping, list, Element)):
        return ""
    return str(value).strip()


def plain_description(published: Any) -> str:
    """Return published description markup as plain paragraphs, or "" when absent."""
    if not isinstance(published, str) or not published.strip():
        return ""
    # Greenhouse ships its body entity-escaped, so the markup only appears after unescaping.
    return strip_tags(html.unescape(published))


def listing_summary(text: str) -> str:
    """Return publisher text as a listing summary: whitespace collapsed, then capped."""
    return " ".join(text.split())[:_LISTING_SUMMARY_CHARS]


def posted_on(
    path: Path, grade: PostedDateGrade, *, basis: str = ""
) -> Derivation[PublisherPostedDate | None]:
    """Return the derivation reading the posted date a record states at one key path."""
    return lambda posting: publisher_posted_date(
        stated_text(posting.record, path), basis=basis or ".".join(path), grade=grade
    )


def _read[T](source: Source[T] | None, posting: Posting) -> Any:
    if source is None:
        return None
    if isinstance(source, tuple):
        return stated(posting.record, source)
    return source(posting)


def _read_text(source: Source[str] | None, posting: Posting) -> str:
    if isinstance(source, tuple):
        return stated_text(posting.record, source)
    return str(_read(source, posting) or "").strip()


def _employer(declaration: BoardDeclaration, posting: Posting) -> str:
    """Apply the declaration's publisher preference before the configured-name rule."""
    source = declaration.employer
    if declaration.prefer_publisher_employer:
        published = _read_text(source, replace(posting, employer=""))
        if published:
            return published[:EMPLOYER_CHARS]
    if posting.employer_is_authoritative and posting.employer:
        return posting.employer[:EMPLOYER_CHARS]
    if isinstance(source, tuple):
        return (stated_text(posting.record, source) or posting.employer)[:EMPLOYER_CHARS]
    if source is None:
        return posting.employer[:EMPLOYER_CHARS]
    return (source(posting) or posting.employer)[:EMPLOYER_CHARS]


def _identity_trust(declaration: BoardDeclaration, posting: Posting) -> ListingIdentityTrust:
    """The trust the mapped employer carries, by where the name it returns came from."""
    # A person who named this board answers for its employer as a publisher does,
    # and a read with no configured name reports only what the record states.
    if not posting.employer or posting.employer_is_authoritative:
        return ListingIdentityTrust.PUBLISHER
    # Publisher-preferring platforms vouch for a configured fallback when no name is
    # stated, as platforms without an employer field do.
    if declaration.employer is None or declaration.prefer_publisher_employer:
        return ListingIdentityTrust.PUBLISHER
    stated_name = _employer(declaration, replace(posting, employer=""))
    return ListingIdentityTrust.PUBLISHER if stated_name else ListingIdentityTrust.UNVERIFIED_HINT


def _described(declaration: BoardDeclaration, posting: Posting) -> list[tuple[str, str, str]]:
    """Return each description section yielding text: where it is stated, its markup, its text."""
    source = declaration.description
    if source is None:
        return []
    if callable(source):
        stated_markup = [(f"{source.__module__}.{source.__qualname__}", source(posting))]
    else:
        stated_markup = [(".".join(path), stated(posting.record, path)) for path in source]
    return [
        (where, markup, text)
        for where, markup in stated_markup
        if (text := plain_description(markup))
    ]


def _description(declaration: BoardDeclaration, posting: Posting) -> str:
    return "\n\n".join(text for _where, _markup, text in _described(declaration, posting))


def map_posting(declaration: BoardDeclaration, posting: Posting) -> MappedPosting:
    """Read every field a declaration names from one record."""
    places = tuple(sanitize(place) for place in values(_read(declaration.places, posting)))
    signal = declaration.arrangement(posting) if declaration.arrangement else ArrangementSignal()
    aliases = declaration.role_aliases(posting) if declaration.role_aliases else None
    # The first place stated; every other one a record names stays in the locations fact.
    location = (
        _read_text(declaration.location, posting)
        if declaration.location is not None
        else next(iter(values(places)), "")
    )
    description = _description(declaration, posting)
    employer = sanitize(_employer(declaration, posting))
    published_employer = sanitize(
        _read_text(declaration.employer, replace(posting, employer=""))[:EMPLOYER_CHARS]
    )
    employer_fact = PublishedFact(
        values=(employer,) if employer else (),
        provenance=(
            f"{declaration.platform}.employer"
            if employer and employer == published_employer
            else "source.registered_employer"
            if employer and posting.employer_is_authoritative
            else "source.employer_hint"
            if employer
            else ""
        ),
        complete=bool(employer),
    )
    return MappedPosting(
        title=sanitize(_read_text(declaration.title, posting)),
        url=_read_text(declaration.url, posting),
        employer=employer,
        location=sanitize(location),
        requisition_id=_read_text(declaration.requisition_id, posting),
        facts=replace(
            published_listing_facts(
                declaration.platform,
                identity_trust=_identity_trust(declaration, posting),
                locations=places,
                locations_complete=(
                    declaration.locations_complete(posting)
                    if declaration.locations_complete
                    else True
                ),
                description=description,
                arrangements=signal.value,
                arrangements_unresolved=signal.unresolved,
                arrangement_provenance=signal.provenance_suffix,
                employment_types=_read(declaration.employment_types, posting) or None,
                compensation=_read(declaration.compensation, posting) or None,
                role_aliases=None if aliases is None else list(aliases),
            ),
            employer=employer_fact,
            posted_date=declaration.posted(posting) if declaration.posted else None,
        ),
        description=description,
        place_entries=(
            declaration.place_entries(posting) if declaration.place_entries else len(places)
        ),
    )


def _body(declaration: BoardDeclaration, posting: Posting, description: str) -> str:
    """Return the body both reads retain; a record with no description has none."""
    if not description:
        return ""
    parts = (*(_read_text(line, posting) for line in declaration.heading), description)
    return "\n\n".join(part for part in parts if part.strip())


def source_listing(declaration: BoardDeclaration, posting: Posting) -> SourceListing | None:
    """Map one board-feed record, or None when it states no title or no address."""
    mapped = map_posting(declaration, posting)
    if not mapped.title or not mapped.url:
        return None
    body = _body(declaration, posting, mapped.description)
    return SourceListing(
        employer=mapped.employer,
        title=mapped.title,
        url=mapped.url,
        location=mapped.location,
        requisition_id=mapped.requisition_id,
        facts=mapped.facts,
        # Screening reads the description as one line; the retained body keeps paragraphs.
        summary=listing_summary(mapped.description),
        published_description=body,
        source=_source(declaration, posting) if body else None,
    )


def source_listings(
    declaration: BoardDeclaration,
    records: Iterable[Any],
    *,
    employer: str,
    employer_is_authoritative: bool = False,
    groups: Mapping[str, str] | None = None,
) -> list[SourceListing]:
    """Map one feed page, dropping entries that are not records or state no identity."""
    postings = (
        Posting(record, employer, groups or {}, employer_is_authoritative)
        for record in records
        if isinstance(record, (Mapping, Element))
    )
    return [
        listing
        for posting in postings
        if (listing := source_listing(declaration, posting)) is not None
    ]


def posting_check(declaration: BoardDeclaration, posting: Posting, *, note: str) -> PostingCheck:
    """Map one single-posting record into readable publisher metadata and its text."""
    mapped = map_posting(declaration, posting)
    text = _body(declaration, posting, mapped.description)
    return PostingCheck(
        status="readable",
        note=note,
        platform=declaration.platform,
        title=mapped.title,
        employer=mapped.employer,
        location=mapped.location,
        facts=mapped.facts,
        text=text,
        source=_source(declaration, posting) if text else None,
        location_entries=mapped.place_entries,
    )


def _source(declaration: BoardDeclaration, posting: Posting) -> PostingSource:
    """Return the record and the markup a retained body was converted from."""
    described = _described(declaration, posting)
    record = posting.record
    return PostingSource(
        publisher=declaration.platform,
        record=json.dumps(dict(record), ensure_ascii=False) if isinstance(record, Mapping) else "",
        description_markup="\n\n".join(markup for _where, markup, _text in described),
        description_paths=tuple(where for where, _markup, _text in described),
        item_xml=_item_xml(record) if isinstance(record, Element) else "",
    )


def _item_xml(item: Element) -> str:
    """Serialize a feed item without its tail, the feed text that follows it."""
    detached = copy(item)
    detached.tail = None
    return tostring(detached, encoding="unicode")


# schema.org's unitText values that name a pay period screening can compare; WEEK has none.
_PAY_PERIODS = {"HOUR": "hour", "DAY": "day", "MONTH": "month", "YEAR": "year"}
_CURRENCY_CODE = re.compile(r"[A-Z]{3}")
_PLAIN_AMOUNT = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _amount(written: str) -> Decimal | None:
    # Only plain digits: an exponent or a sign would be misread once written back out.
    if _PLAIN_AMOUNT.fullmatch(written.strip()) is None:
        return None
    amount = Decimal(written.replace(",", "").strip())
    return amount if amount > 0 else None


def salary_compensation(salary: BaseSalaryMarkup | None) -> str | None:
    """Write a stated salary range the way screening reads one, or nothing when a part is missing.

    Screening compares the highest amount to the person's minimum and can refuse,
    so only a stated floor and ceiling together are trusted as a fact.
    """
    if salary is None:
        return None
    period = _PAY_PERIODS.get(salary.unit.strip().upper())
    currency = salary.currency.strip()
    minimum, maximum = _amount(salary.minimum), _amount(salary.maximum)
    if period is None or _CURRENCY_CODE.fullmatch(currency) is None:
        return None
    if minimum is None or maximum is None or minimum > maximum:
        return None
    return f"{currency} {minimum:f}-{maximum:f} per {period}"


_PERIOD_WORDS = {
    "annual": "YEAR",
    "annually": "YEAR",
    "annum": "YEAR",
    "yearly": "YEAR",
    "year": "YEAR",
    "monthly": "MONTH",
    "month": "MONTH",
    "daily": "DAY",
    "day": "DAY",
    "hourly": "HOUR",
    "hour": "HOUR",
}
# Words publishers wrap a period in: "per-year-salary", "Per Hour", "an hour".
_PERIOD_FILLER = frozenset({"per", "a", "an", "salary", "wage", "pay", "rate", "base"})


def pay_period(wording: Any) -> str:
    """Return the schema.org unit a publisher's period wording names, or "" unless exactly one."""
    words = re.findall(r"[a-z]+", wording.casefold()) if isinstance(wording, str) else []
    named = {_PERIOD_WORDS[word] for word in words if word in _PERIOD_WORDS}
    # A qualifier such as "semi" in "semi-monthly" changes the period, so it names none.
    unknown = [word for word in words if word not in _PERIOD_WORDS and word not in _PERIOD_FILLER]
    if len(named) != 1 or unknown:
        return ""
    return named.pop()


def _written_amount(amount: Any) -> str:
    """Write a stated amount as text; a JSON number loses its float tail."""
    if isinstance(amount, str):
        return amount
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return ""
    return f"{Decimal(str(amount)).normalize():f}"


def pay_range(currency: Any, minimum: Any, maximum: Any, unit: str = "") -> str | None:
    """Write a stated floor and ceiling with whichever of currency and period the record states.

    With both parts it reads as ``salary_compensation`` writes it; without one, screening
    shows the range and cannot compare it, so a partial range never refuses a listing.
    """
    floor, ceiling = _amount(_written_amount(minimum)), _amount(_written_amount(maximum))
    if floor is None or ceiling is None or floor > ceiling:
        return None
    code = currency.strip() if isinstance(currency, str) else ""
    period = _PAY_PERIODS.get(unit)
    parts = (
        code if _CURRENCY_CODE.fullmatch(code) else "",
        f"{floor:f}-{ceiling:f}",
        f"per {period}" if period else "",
    )
    return " ".join(part for part in parts if part)
