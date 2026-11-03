"""What a publisher check states about a staged row, and the hints it replaces."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from clearscout.application.discovery.board_slugs import backfill_employer_from_url
from clearscout.application.discovery.source_reading import PostingCheck, SourceListing
from clearscout.domain.staged_facts import (
    FreshnessState,
    ListingIdentityTrust,
    PublishedFact,
    StagedListingFacts,
    merged_posted_date,
)
from clearscout.infrastructure.persistence.models import PendingDiscovery
from clearscout.infrastructure.web_fetch.listing_facts import stated_place
from clearscout.shared.text import sanitize


def apply_publisher_check(
    pending: PendingDiscovery,
    check: PostingCheck,
    *,
    read_on: date,
    keep_title: bool = False,
    keep_names: bool = False,
) -> None:
    """Write a readable check's names and facts onto a staged row."""
    prior_facts = StagedListingFacts.from_json(pending.staged_facts)
    registered_employer_vouched = (
        check.platform == "oracle"
        and check.facts.publisher_vouched
        and check.facts.employer.complete
        and check.facts.employer.provenance == "source.registered_employer"
        and check.facts.employer.values == (pending.employer,)
    )
    retained_unverified_identity = not prior_facts.publisher_vouched and hint_still_used(
        check,
        "" if registered_employer_vouched else pending.employer,
        pending.title,
        pending.location,
    )
    if not keep_names:
        pending.employer = sanitize(check.employer or pending.employer)[:300]
        if not keep_title:
            pending.title = sanitize(check.title or pending.title)[:500]
        pending.location = stated_place(sanitize(check.location or pending.location))[:500]
    pending.staged_facts = replace(
        with_silent_fields_kept(check.facts, prior_facts),
        identity_trust=(
            ListingIdentityTrust.UNVERIFIED_HINT
            if retained_unverified_identity
            else check.facts.identity_trust
        ),
        freshness=(
            check.facts.freshness
            if check.facts.freshness is not FreshnessState.UNKNOWN
            else prior_facts.freshness
        ),
        posted_date=merged_posted_date(prior_facts.posted_date, check.facts.posted_date, read_on),
    ).as_json()


def _restated(read: PublishedFact, stored: PublishedFact) -> PublishedFact:
    """Keep a stored field a fresh read says nothing about at all."""
    return read if read.values or read.unresolved_values else stored


def with_silent_fields_kept(
    read: StagedListingFacts, stored: StagedListingFacts
) -> StagedListingFacts:
    """Return a fresh read's facts, keeping each published field it says nothing about."""
    # An arrangement read off a place's wording lasts only as long as that place.
    place_restated = bool(read.locations.values or read.locations.unresolved_values)
    derived = "location_phrase" in stored.arrangements.provenance
    return replace(
        read,
        employer=_restated(read.employer, stored.employer),
        role_aliases=_restated(read.role_aliases, stored.role_aliases),
        locations=_restated(read.locations, stored.locations),
        arrangements=(
            read.arrangements
            if derived and place_restated
            else _restated(read.arrangements, stored.arrangements)
        ),
        employment_types=_restated(read.employment_types, stored.employment_types),
        compensation=_restated(read.compensation, stored.compensation),
        focus_terms=_restated(read.focus_terms, stored.focus_terms),
    )


def hint_still_used(
    check: PostingCheck | SourceListing, employer: str, title: str, location: str
) -> bool:
    """Return whether a name the check leaves blank is still filled from a hint."""
    return any(
        (
            not check.employer and bool(employer),
            not check.title and bool(title),
            not check.location and bool(location),
        )
    )


def name_employer_from_url(pending: PendingDiscovery, url: str) -> None:
    """Fill a blank employer from a board tenant slug and mark the identity an unverified hint."""
    if pending.employer or not (derived := backfill_employer_from_url(url)):
        return
    pending.employer = derived[:300]
    # An earlier read may have vouched for this row without ever stating an employer,
    # and a name read off the tenant must not inherit that trust.
    pending.staged_facts = replace(
        StagedListingFacts.from_json(pending.staged_facts),
        identity_trust=ListingIdentityTrust.UNVERIFIED_HINT,
    ).as_json()


def publisher_staged_facts(
    check: PostingCheck, employer: str, title: str, location: str
) -> StagedListingFacts:
    """Return a readable check's facts, keeping unverified trust while a hint stands in."""
    return replace(
        check.facts,
        identity_trust=(
            ListingIdentityTrust.UNVERIFIED_HINT
            if hint_still_used(check, employer, title, location)
            else check.facts.identity_trust
        ),
    )
