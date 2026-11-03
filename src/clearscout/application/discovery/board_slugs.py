"""Derive employer or title text from a supported ATS URL, without inventing.

A board adapter or a hosted-search proposal may hand over a listing whose URL
names the tenant and the role but whose display fields are blank. These helpers
read the slug and only the slug, so an unknown URL shape returns None and no
fake name is ever written.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import replace
from urllib.parse import unquote, urlparse

from clearscout.application.discovery.aggregator_catalog import STARTER_AGGREGATORS
from clearscout.application.discovery.source_reading import SourceListing
from clearscout.domain.identity import normalize_url
from clearscout.domain.staged_facts import ListingIdentityTrust
from clearscout.infrastructure.web_fetch.board_adapters import identify_board_url
from clearscout.infrastructure.web_fetch.board_mappings.schema_org import host_name_labels
from clearscout.infrastructure.web_fetch.board_slugs import title_from_url_slug
from clearscout.infrastructure.web_fetch.boards import (
    board_identity,
    canonicalise_source_url,
    job_board_address,
)
from clearscout.infrastructure.web_fetch.platforms import PLATFORMS

# Hosts whose first path segment names the tenant. A floor query's site clause names
# only these, since the EU hosts would spend its length budget on European boards.
_SEARCHED_PATH_TENANT_HOSTS = (
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "jobs.lever.co",
    "jobs.ashbyhq.com",
)
_PATH_TENANT_HOSTS = (
    *_SEARCHED_PATH_TENANT_HOSTS,
    "boards.eu.greenhouse.io",
    "job-boards.eu.greenhouse.io",
)
# Workday names the tenant as a subdomain of this domain.
_WORKDAY_DOMAIN = "myworkdayjobs.com"
TENANT_ATS_HOSTS = (*_SEARCHED_PATH_TENANT_HOSTS, _WORKDAY_DOMAIN)
"""ATS hosts a campaign floor query searches, in a fixed order."""
_EMPLOYER_SLUG_BOARDS = frozenset(platform.name for platform in PLATFORMS if platform.employer_slug)
_WORD_CHARACTERS = re.compile(r"[^\W_]+")
_WORD = re.compile(r"(?P<word>[^\W_]+)(?P<possessive>['\N{RIGHT SINGLE QUOTATION MARK}]s\b)?")
# Words of one name sit apart by a space, hyphen, ampersand, or period, never a comma.
_JOINER = re.compile(r"[ \t&.\-]{1,3}")
_MAX_NAME_WORDS = 4
# Job sites that list other employers' postings under their own name.
_JOB_SITE_DOMAINS = (
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "ziprecruiter.com",
    "wellfound.com",
    "builtin.com",
    "weworkremotely.com",
    *((urlparse(site.url).hostname or "").removeprefix("www.") for site in STARTER_AGGREGATORS),
)


def tenant_slug_from_url(url: str) -> str | None:
    """Return the ATS tenant slug carried by a supported board URL, or None."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = parsed.netloc.lower()
    parts = [segment for segment in parsed.path.split("/") if segment]
    if host in _PATH_TENANT_HOSTS:
        return parts[0] if parts else None
    if host.endswith(f".{_WORKDAY_DOMAIN}"):
        subdomain = host.split(".", 1)[0]
        return subdomain or None
    board = identify_board_url(url)
    if board is None or board[0] not in _EMPLOYER_SLUG_BOARDS:
        return None
    groups = board[1].groupdict()
    return groups.get("org") or groups.get("tenant") or None


def backfill_employer_from_url(url: str) -> str | None:
    """Derive a display employer from a supported ATS URL's tenant, or None for any other URL."""
    tenant = tenant_slug_from_url(url)
    if not tenant:
        return None
    decoded = unquote(tenant).replace("-", " ").replace("_", " ").strip()
    if not decoded or not re.search(r"[A-Za-z]", decoded):
        return None
    return re.sub(r"\s+", " ", decoded).title()


def employer_name(stated: str, url: str, text: str) -> tuple[str, bool]:
    """Return the employer name to write, and whether only the URL's tenant gave it.

    A stated name stands; otherwise the publisher text's own spelling of the tenant
    replaces the slug, which is used only when the text never spells it.
    """
    if stated.strip():
        return stated.strip(), False
    spelled = publisher_spelling_of_tenant(url, text)
    if spelled:
        return spelled, False
    tenant = backfill_employer_from_url(url) or ""
    return tenant, bool(tenant)


def publisher_spelling_of_tenant(url: str, text: str) -> str | None:
    """Return the posting text's own capitalized spelling of the URL's tenant, or None."""
    tenant = tenant_slug_from_url(url)
    return _spelling_of(_alphanumerics(unquote(tenant)), text) if tenant else None


def publisher_spelling_of_host(url: str, page_title: str) -> str | None:
    """Return the page title's spelling of an employer site's own name, or None.

    The spelling must equal a name label of the host, so only the site's own
    words are copied. A board or job site's title names that site, never the employer.
    """
    host = (urlparse(url).hostname or "").lower()
    if job_board_address(url) or any(
        host == site or host.endswith(f".{site}") for site in _JOB_SITE_DOMAINS
    ):
        return None
    for label in host_name_labels(url):
        runs = list(_capitalized_runs(page_title, label))
        # A brand a title prints in capitals is often spelled that way, so it is copied as printed.
        if runs:
            return next((run for run in runs if not run.isupper()), runs[0])
    return None


def _spelling_of(wanted: str, text: str) -> str | None:
    """Return the text's capitalized spelling of ``wanted``, or None.

    The spelling is one to four adjacent words, each starting with a capital, whose
    letters and digits run together equal ``wanted``. A mixed-case spelling wins;
    one found only in capitals, as a heading prints it, comes back title-cased.
    """
    if not wanted or wanted not in _alphanumerics(text):
        return None
    shouted = None
    for spelling in _capitalized_runs(text, wanted):
        if not spelling.isupper():
            return spelling
        shouted = shouted or spelling
    if shouted is None:
        return None
    return _WORD_CHARACTERS.sub(lambda word: word.group(0).capitalize(), shouted)


def _capitalized_runs(text: str, wanted: str) -> Iterator[str]:
    """Yield each run of capitalized words whose letters and digits spell ``wanted``."""
    words = list(_WORD.finditer(text))
    for index, first in enumerate(words):
        joined = ""
        previous: re.Match[str] | None = None
        for word in words[index : index + _MAX_NAME_WORDS]:
            if not word.group("word")[0].isupper() or not _continues(text, previous, word):
                break
            joined += word.group("word").lower()
            if joined == wanted:
                yield text[first.start() : word.end("word")]
                break
            if word.group("possessive") or not wanted.startswith(joined):
                break
            previous = word


def _continues(text: str, previous: re.Match[str] | None, word: re.Match[str]) -> bool:
    """Return whether ``word`` continues the name ``previous`` began."""
    if previous is None:
        return True
    gap = text[previous.end() : word.start()]
    # A period joins initials such as "J.P.", never two words across a sentence break.
    return _JOINER.fullmatch(gap) is not None and (
        "." not in gap or len(previous.group("word")) == 1
    )


def _alphanumerics(value: str) -> str:
    return "".join(_WORD_CHARACTERS.findall(value)).lower()


def backfill_title_from_url(url: str) -> str | None:
    """Derive a display title from a supported ATS URL slug, or None."""
    return title_from_url_slug(url)


def board_identity_key(url: str) -> str:
    """Return a root or detail URL's dedupe identity: locale- and case-free tenant, else the URL."""
    identity = board_identity(url)
    if identity is not None:
        kind, key = identity
        return f"{kind}:{key}"
    return normalize_url(canonicalise_source_url(url))


def backfilled_listing(listing: SourceListing) -> SourceListing:
    """Fill an empty employer or title from the URL slug, never overwriting."""
    employer = listing.employer
    title = listing.title
    facts = listing.facts
    if not employer:
        derived = backfill_employer_from_url(listing.url)
        if derived:
            employer = derived
            # A name read off the tenant slug is machine-derived, so it must not
            # keep whatever trust the listing arrived with.
            facts = replace(facts, identity_trust=ListingIdentityTrust.UNVERIFIED_HINT)
    if not title:
        derived_title = backfill_title_from_url(listing.url)
        if derived_title:
            # A slug title keeps the listing's trust because one flag governs the
            # title and the employer together, so downgrading here would blank an
            # employer the publisher did state.
            title = derived_title
    if employer == listing.employer and title == listing.title:
        return listing
    return replace(listing, employer=employer, title=title, facts=facts)
