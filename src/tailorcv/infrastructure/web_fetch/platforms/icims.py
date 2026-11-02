"""iCIMS career sites, read through a Jibe site's JSON or a classic site's sitemap.

A Jibe site is named by its CDN bundle; a classic site by its <label>-<tenant>.icims.com host.
"""

from __future__ import annotations

import re
from functools import partial
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

from tailorcv.infrastructure.web_fetch.board_mappings import (
    ICIMS_CLASSIC_MAPPING,
    ICIMS_MAPPING,
    icims_openings,
    icims_sitemap_url,
)
from tailorcv.infrastructure.web_fetch.board_platform import (
    HOST_END,
    HOST_START,
    BoardPlatform,
    host_match,
)
from tailorcv.infrastructure.web_fetch.board_reads import (
    PageAnswer,
    board_listings,
    continued_board_page,
    job_page_tenant,
    listed_page_continuations,
    listed_page_read,
    page_continuation_target,
    paged_board_read,
    sitemap_locations,
    unreadable_continuation,
    with_query,
)

if TYPE_CHECKING:
    from tailorcv.application.discovery.source_reading import (
        SourceContinuationTarget,
        SourceListing,
        SourceRead,
    )
    from tailorcv.infrastructure.web_fetch.board_platform import BoardTransport

# An iCIMS posting is /jobs/<id>/job or /jobs/<id>/<slug>/job; a delisted one 302s
# to /jobs/search?notFound=1, which this shape does not match (search is not <id>),
# so the redirect reads as leaving the posting.
ICIMS_JOB = re.compile(r"[a-z0-9-]+\.icims\.com/jobs/\d+(?:/[^/?]+)?/job\b")
# An iCIMS career site loads its bundle from the platform's own CDN, and its
# JSON API is on the same host as the page, so nothing else has to be captured.
ICIMS_BOARD = re.compile(r"[a-z0-9-]+\.jibecdn\.com/")

# A classic site is served from the platform's own <label>-<tenant> host, and the label
# is free-form (careers-, uscareers-, employment-), so it stays part of the tenant.
_CLASSIC_TENANT = HOST_START + r"(?P<tenant>[a-z0-9]+-[a-z0-9-]+)\.icims\.com"
ICIMS_CLASSIC_BOARD = re.compile(_CLASSIC_TENANT + HOST_END, re.IGNORECASE)
ICIMS_CLASSIC_JOB = re.compile(
    _CLASSIC_TENANT + r"/jobs/(?P<job>\d+)(?:/[^/?#]+)?/job(?=[?#]|$)", re.IGNORECASE
)

ICIMS_PAGE = 20
ICIMS_MAX_PAGES = 100
_PENDING = "icims_page_pending"


def _read(fetcher: BoardTransport, employer: str, groups: dict[str, str], url: str) -> SourceRead:
    """Read a classic site from its sitemap, and a Jibe site through its JSON API."""
    tenant = groups.get("tenant", "").casefold()
    if tenant:
        return _read_classic(fetcher, employer, tenant)
    return _read_jibe(fetcher, employer, url)


def _read_classic(fetcher: BoardTransport, employer: str, tenant: str) -> SourceRead:
    """Read every posting page a classic site's sitemap lists and what each page declares."""
    host = f"{tenant}.icims.com"
    pages, unread, gone = sitemap_locations(fetcher, icims_sitemap_url(host), "iCIMS")
    return listed_page_read(
        fetcher,
        employer,
        icims_openings(pages, host),
        mapping=ICIMS_CLASSIC_MAPPING,
        unread_note="The iCIMS sitemap listed no posting page.",
        source_url=_classic_root(host),
        groups={"tenant": tenant},
        pending_sitemaps=unread,
        sitemap_listed=True,
        gone_sitemaps=gone,
        skip_recent=True,
    )


def _classic_root(host: str) -> str:
    return f"https://{host}/jobs/search"


def _classic_rows(
    pages: list[str], target: SourceContinuationTarget
) -> list[dict[str, str]] | None:
    """Return the posting rows among ``pages`` for the classic site a continuation came from."""
    site = host_match(ICIMS_CLASSIC_BOARD, target.source_url)
    if site is None:
        return None
    return icims_openings(pages, f"{site.group('tenant').casefold()}.icims.com")


def _read_jibe(fetcher: BoardTransport, employer: str, url: str) -> SourceRead:
    """Read one iCIMS Jibe career site through the JSON its own search page reads.

    The site serves this API from the same host as the page, and each
    opening arrives under its own ``data`` object. A site larger than the
    page ceiling comes back marked incomplete.
    """
    host = urlsplit(url).netloc
    return paged_board_read(
        fetcher,
        (
            (partial(fetcher.get_json, _page_url(host, page)), _cursor(host, page))
            for page in range(1, ICIMS_MAX_PAGES + 1)
        ),
        partial(_answered, employer),
        _cursor(host, ICIMS_MAX_PAGES + 1),
    )


def _continue(
    fetcher: BoardTransport, employer: str, target: SourceContinuationTarget
) -> SourceRead:
    """Read one retained iCIMS API page and carry the next page."""
    parsed = urlsplit(target.url)
    query = parse_qs(parsed.query)
    try:
        page = int(query.get("page", [""])[0])
        limit = int(query.get("limit", [str(ICIMS_PAGE)])[0])
    except ValueError:
        return unreadable_continuation(target, "invalid_icims_cursor")
    base = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return continued_board_page(
        target,
        partial(fetcher.get_json, target.url),
        partial(_answered, employer),
        lambda records, total: (
            page_continuation_target(
                with_query(base, page=page + 1, limit=limit),
                source_url=target.source_url,
                label=f"iCIMS page {page + 1}",
                reason=_PENDING,
            )
            if records and records >= limit and (not total or page * limit < total)
            else None
        ),
        pending=_PENDING,
    )


def _page_url(host: str, page: int) -> str:
    return f"https://{host}/api/jobs?page={page}&limit={ICIMS_PAGE}"


def _cursor(host: str, page: int) -> SourceContinuationTarget:
    return page_continuation_target(
        _page_url(host, page),
        source_url=f"https://{host}/jobs/search",
        label=f"iCIMS page {page}",
        reason=_PENDING,
    )


def _answered(employer: str, payload: Any) -> PageAnswer:
    """Return one API page's listings, count, and stated total, refusing another shape."""
    if not isinstance(payload, dict):
        raise ValueError("iCIMS listing page is not a JSON object")
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        raise ValueError("iCIMS listing page has no jobs list")
    try:
        total = int(payload.get("totalCount") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("iCIMS listing page states a non-integer total") from exc
    return _listings(employer, jobs), len(jobs), total


def _listings(employer: str, jobs: list[Any]) -> list[SourceListing]:
    """Map one iCIMS page, whose openings each arrive under their own ``data`` object."""
    records = [job.get("data") for job in jobs if isinstance(job, dict)]
    return board_listings(ICIMS_MAPPING, records, employer)


PLATFORM = BoardPlatform(
    name="icims",
    label="iCIMS",
    board_patterns=(ICIMS_BOARD, ICIMS_CLASSIC_BOARD),
    adapter=_read,
    job_patterns=(ICIMS_CLASSIC_JOB, ICIMS_JOB),
    source_roots=(
        (
            ICIMS_CLASSIC_BOARD,
            lambda _url, match: _classic_root(f"{match.group('tenant').casefold()}.icims.com"),
        ),
    ),
    identity_rules=((ICIMS_CLASSIC_BOARD, lambda match: match.group("tenant").casefold()),),
    single_tenant=False,
    continuations={
        **listed_page_continuations(
            ICIMS_CLASSIC_MAPPING,
            _classic_rows,
            groups=job_page_tenant(ICIMS_CLASSIC_JOB),
            sitemaps=True,
        ),
        _PENDING: _continue,
    },
)
