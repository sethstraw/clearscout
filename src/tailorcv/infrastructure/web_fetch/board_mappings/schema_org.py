"""Map a page's own `JobPosting` markup, which arrives typed rather than as a board record."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from urllib.parse import urlparse

from tailorcv.application.discovery.posting_text import (
    publisher_closing_date,
    publisher_posted_date,
)
from tailorcv.application.discovery.source_reading import PostingCheck, SourceListing
from tailorcv.domain import PostingContentAccessStatus
from tailorcv.domain.artifacts import PostingSource
from tailorcv.domain.identity import employer_name_words, normalize_text
from tailorcv.domain.staged_facts import (
    FreshnessState,
    ListingIdentityTrust,
    PostedDateGrade,
    StagedListingFacts,
)
from tailorcv.infrastructure.web_fetch.board_mappings.mapper import (
    listing_summary,
    plain_description,
    salary_compensation,
)
from tailorcv.infrastructure.web_fetch.boards import single_tenant_board
from tailorcv.infrastructure.web_fetch.ingest import MINIMUM_USEFUL_CHARACTERS
from tailorcv.infrastructure.web_fetch.ingest.job_posting_markup import (
    JobPostingMarkup,
)
from tailorcv.infrastructure.web_fetch.listing_facts import published_listing_facts
from tailorcv.infrastructure.web_fetch.platforms.lever import wrapped_lever_job

# Host labels that name a career site rather than its owner, so they identify
# no employer on their own.
_GENERIC_HOST_LABELS = frozenset(
    {
        "www",
        "jobs",
        "job",
        "careers",
        "career",
        "apply",
        "applying",
        "recruiting",
        "recruit",
        "talent",
        "hire",
        "join",
        "work",
        "my",
        "secure",
        "boards",
        "people",
    }
)
# Shorter than this, a label match is a coincidence rather than a name.
_SHORTEST_IDENTIFYING_NAME = 4


def _employer_key(employer: str) -> str:
    return "".join(employer_name_words(employer))


def _letters_and_digits(value: str) -> str:
    return normalize_text(value).replace(" ", "")


def _names_a_host_label(employer: str, page_url: str) -> bool:
    """Return whether the employer name, or a word prefix of it, is a label of the page host."""
    labels = {
        label.replace("-", "") for label in (urlparse(page_url).hostname or "").lower().split(".")
    } - _GENERIC_HOST_LABELS
    words = employer_name_words(employer)
    return any(
        len(name) >= _SHORTEST_IDENTIFYING_NAME and name in labels
        for name in ("".join(words[:count]) for count in range(1, len(words) + 1))
    )


def host_name_labels(page_url: str) -> tuple[str, ...]:
    """Return the page host's labels that could name its owner, nearest the domain first."""
    labels = (urlparse(page_url).hostname or "").lower().split(".")[:-1]
    names = (label.replace("-", "") for label in reversed(labels))
    return tuple(
        name
        for name in names
        if name not in _GENERIC_HOST_LABELS and len(name) >= _SHORTEST_IDENTIFYING_NAME
    )


def spells_the_page_host(employer: str, page_url: str) -> bool:
    """Return whether the declared name is the page's own hostname rather than an employer."""
    host = (urlparse(page_url).hostname or "").lower().removeprefix("www.")
    declared = _letters_and_digits(employer.strip().lower().removeprefix("www."))
    return bool(declared) and declared == _letters_and_digits(host)


def _declared_employer(posting: JobPostingMarkup, page_url: str) -> str:
    # A mirror that spells its own host as the hiring organization names no
    # employer, so the row falls through to whoever the source names.
    declared = posting.employer.strip()
    return "" if spells_the_page_host(declared, page_url) else declared


def _vouched(
    posting: JobPostingMarkup,
    page_url: str,
    source_employer: str,
    *,
    employer_is_authoritative: bool,
) -> bool:
    """Return whether this page speaks for the employer its markup names.

    Markup vouches for the posting, not for whoever it names, so an aggregator
    page naming an employer leaves that identity an unverified hint.
    """
    if single_tenant_board(page_url):
        return True
    declared = _declared_employer(posting, page_url)
    if not declared:
        # The row then takes the source's employer, which vouches for itself only
        # when a person named it rather than a campaign reading it off a URL.
        return employer_is_authoritative and bool(source_employer.strip())
    if source_employer.strip() and _employer_key(source_employer) == _employer_key(declared):
        return True
    return _names_a_host_label(declared, page_url)


def _declared_facts(
    posting: JobPostingMarkup,
    read_at: datetime,
    page_url: str,
    source_employer: str,
    *,
    employer_is_authoritative: bool,
) -> StagedListingFacts:
    closing_date = publisher_closing_date(
        plain_description(posting.description),
        valid_through=posting.valid_through,
        source_url=posting.url or page_url,
        observed_at=read_at,
    )
    stale = closing_date.has_passed(read_at) is True
    facts = published_listing_facts(
        "schema_org",
        identity_trust=(
            ListingIdentityTrust.PUBLISHER
            if _vouched(
                posting,
                page_url,
                source_employer,
                employer_is_authoritative=employer_is_authoritative,
            )
            else ListingIdentityTrust.UNVERIFIED_HINT
        ),
        locations=list(posting.locations),
        description=posting.description,
        locations_complete=posting.locations_complete,
        employment_types=list(posting.employment_types) or None,
        compensation=salary_compensation(posting.base_salary),
        freshness=FreshnessState.LIKELY_STALE if stale else FreshnessState.UNKNOWN,
    )
    return replace(
        facts,
        closing_date=closing_date,
        posted_date=publisher_posted_date(
            posting.date_posted, basis="datePosted", grade=PostedDateGrade.BOARD_DATE
        ),
    )


def declared_check(posting: JobPostingMarkup, *, page_url: str, read_at: datetime) -> PostingCheck:
    """Return what one read posting page declares about itself as publisher metadata."""
    return PostingCheck(
        status="readable",
        note="The page declared this posting metadata.",
        platform="schema_org",
        title=posting.title,
        employer=_declared_employer(posting, page_url),
        location=posting.location,
        # A page read on its own has no source, so only its address can vouch.
        facts=_declared_facts(posting, read_at, page_url, "", employer_is_authoritative=False),
    )


def declared_listing(
    posting: JobPostingMarkup,
    *,
    employer: str,
    employer_is_authoritative: bool,
    page_url: str,
    read_at: datetime,
    page_markup: str,
    content_status: PostingContentAccessStatus = PostingContentAccessStatus.NOT_YET_READ,
    content_note: str = "",
) -> SourceListing:
    """Return one publisher-declared posting with its description and expiry hint.

    ``employer_is_authoritative`` says a person named ``employer``, which is what
    lets it vouch for markup that names no employer of its own. ``page_markup``
    is the page the declaration came from.
    """
    description = plain_description(posting.description)
    # A shorter declaration is not the body a later read of this page keeps.
    body = description if len(description) >= MINIMUM_USEFUL_CHARACTERS else ""
    return SourceListing(
        employer=_declared_employer(posting, page_url) or employer,
        title=posting.title,
        url=posting.url or page_url,
        location=posting.location,
        requisition_id=posting.identifier
        or wrapped_lever_job(posting.url or page_url, page_markup),
        facts=_declared_facts(
            posting,
            read_at,
            page_url,
            employer,
            employer_is_authoritative=employer_is_authoritative,
        ),
        content_status=content_status,
        content_note=content_note,
        summary=listing_summary(description),
        published_description=body,
        source=(
            PostingSource(
                publisher="page",
                record=posting.node,
                description_markup=posting.description,
                description_paths=posting.description_paths,
            )
            if body
            else None
        ),
    )
