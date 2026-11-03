"""The shapes that name the applicant tracking system behind a career address.

Every platform's shapes are declared once in its module under ``platforms``;
the lists here are derived from that registry in its detection order. Most
platforms are named by a URL. Some serve the career site from the employer's own
domain, so they are named instead by a fingerprint of the fetched page.

Two platforms are not applicant tracking systems at all. An aggregator is a
single site listing many employers, so its address names no tenant and each
posting it returns states its own employer.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

from clearscout.infrastructure.web_fetch.board_platform import BoardKind, host_match
from clearscout.infrastructure.web_fetch.platforms import PLATFORMS
from clearscout.infrastructure.web_fetch.platforms.greenhouse import GREENHOUSE_HOSTED_JID
from clearscout.infrastructure.web_fetch.platforms.recruitee import RECRUITEE_JOB
from clearscout.infrastructure.web_fetch.policy import fetch_refusal

# The job id is base64, padding included.
LOXO_JOB = re.compile(
    r"(?P<tenant>[a-z0-9-]+)\.app\.loxo\.co/job/(?P<job>[A-Za-z0-9_-]+={0,2})(?=[/?#]|$)",
    re.IGNORECASE,
)

# An iCIMS career site no adapter reads by URL still names a tracking system
# rather than the employer's own site, so it may not stand as a company careers page.
ICIMS_SITE = re.compile(r"[a-z0-9-]+\.icims\.com(?:/|$)", re.IGNORECASE)

# Detection order is shared by direct URLs and career-page URLs.
BOARD_PATTERNS: tuple[tuple[BoardKind, re.Pattern[str]], ...] = tuple(
    (platform.name, pattern) for platform in PLATFORMS for pattern in platform.board_patterns
)

# Every tenant of these platforms is a staffing agency posting for its clients.
_AGENCY_JOB_PATTERNS = (LOXO_JOB,)

# A job-detail URL a fetch and a captured-URL classification both recognize,
# distinct from the board-root shapes above.
JOB_URL_PATTERNS: tuple[re.Pattern[str], ...] = (
    *(pattern for platform in PLATFORMS for pattern in platform.job_patterns),
    *_AGENCY_JOB_PATTERNS,
)

# Prefer structured board URLs before rendered HTML.
POSTING_API_ORDER: tuple[BoardKind, ...] = tuple(
    platform.name for platform in PLATFORMS if platform.posting_api
)

_SINGLE_TENANT_BOARD_KINDS = frozenset(
    platform.name for platform in PLATFORMS if platform.single_tenant
)


def canonicalise_source_url(url: str) -> str:
    """Return the canonical board-root form of a source address.

    A Greenhouse embed URL, a Greenhouse tenant URL with tracking parameters, and a
    single-posting permalink each reduce to their board root; any other URL is
    returned unchanged for the resolver to identify.
    """
    trimmed = url.strip()
    if not trimmed:
        return trimmed
    for platform in PLATFORMS:
        for pattern, formatter in platform.source_roots:
            match = host_match(pattern, trimmed)
            if match:
                return formatter(match.string, match)
    return trimmed


def board_identity(url: str) -> tuple[BoardKind, str] | None:
    """Return the board a root or detail URL names, keyed without locale or case, or None."""
    for platform in PLATFORMS:
        for pattern, key in platform.identity_rules:
            match = host_match(pattern, url)
            if match:
                return platform.name, key(match)
    return None


def board_read_scope(url: str, platform_name: str) -> tuple[tuple[str, str], ...] | None:
    """Identify a recognized board's read scope, retaining locale and tenant fields."""
    for platform in PLATFORMS:
        if platform.name != platform_name:
            continue
        for pattern in platform.board_patterns:
            match = host_match(pattern, url)
            if match is not None:
                groups = tuple(
                    sorted(
                        (key, (value or "").casefold()) for key, value in match.groupdict().items()
                    )
                )
                return groups or None
    return None


# ATS org slugs known to repost many employers' openings under one tenant, so a
# detail URL there names no single employer. Not a host ban: other tenants on
# the same ATS are ordinary employer boards.
MULTI_EMPLOYER_ATS_TENANTS = frozenset({"jobgether"})


# The group each platform's pattern uses for the tenant, most specific first.
_TENANT_GROUPS = ("org", "tenant", "company", "namespace", "site")


def _board_tenant(match: re.Match[str]) -> str:
    groups = match.groupdict()
    return next((groups[name].casefold() for name in _TENANT_GROUPS if groups.get(name)), "")


def single_tenant_board(url: str) -> bool:
    """Return whether a URL addresses a board that serves one employer."""
    parts = urlsplit(url)
    address = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    for kind, pattern in BOARD_PATTERNS:
        match = host_match(pattern, address)
        if match is None:
            continue
        if kind not in _SINGLE_TENANT_BOARD_KINDS:
            return False
        return _board_tenant(match) not in MULTI_EMPLOYER_ATS_TENANTS
    return False


def without_apply_suffix(url: str) -> str:
    """Trim a trailing apply path so it fingerprints as its posting.

    '/apply' and '/application' trim on any host, Recruitee's '/c/new' only on its postings.
    """
    parts = urlsplit(url)
    path = parts.path.rstrip("/").removesuffix("/apply").removesuffix("/application")
    if host_match(RECRUITEE_JOB, url):
        path = path.removesuffix("/c/new")
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def single_employer_detail_url(url: str) -> str | None:
    """Return a fetchable single-employer ATS job-detail URL without '/apply', or None."""
    if not url or fetch_refusal(url):
        return None
    match = next(
        (found for pattern in JOB_URL_PATTERNS if (found := host_match(pattern, url)) is not None),
        None,
    )
    if match is None or match.re in _AGENCY_JOB_PATTERNS:
        return None
    if _board_tenant(match) in MULTI_EMPLOYER_ATS_TENANTS:
        return None
    return without_apply_suffix(match.string)


def job_board_address(url: str) -> bool:
    """Return whether an address is an ATS page or posting rather than the employer's own site."""
    return (
        any(host_match(pattern, url) for _, pattern in BOARD_PATTERNS)
        or any(host_match(pattern, url) for pattern in JOB_URL_PATTERNS)
        or GREENHOUSE_HOSTED_JID.search(url) is not None
        or host_match(ICIMS_SITE, url) is not None
    )
