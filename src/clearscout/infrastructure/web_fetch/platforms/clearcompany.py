"""ClearCompany career sites, named by their embed script and read from the paged careers API."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from clearscout.infrastructure.web_fetch.board_mappings import (
    CLEARCOMPANY_MAPPING,
    clearcompany_jobs_url,
)
from clearscout.infrastructure.web_fetch.board_platform import HOST_START, BoardPlatform
from clearscout.infrastructure.web_fetch.board_reads import (
    BoardDeadlineReachedError,
    board_listings,
    required_list_field,
    short_of_total,
    whole,
)

if TYPE_CHECKING:
    from clearscout.application.discovery.source_reading import SourceRead
    from clearscout.infrastructure.web_fetch.board_platform import BoardTransport

_SITE = r"(?P<site>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?![\w-])"
CLEARCOMPANY_SCRIPT = re.compile(
    HOST_START
    + r"careers-content\.clearcompany\.com/js/v1/career-site(?:-no-polyfill)?\.js"
    + r"\?(?:[^\"'\s<>#]*?&(?:amp;)?)?siteId="
    + _SITE,
    re.IGNORECASE,
)
CLEARCOMPANY_API = re.compile(
    HOST_START + r"careers-api\.clearcompany\.com/v1/" + _SITE, re.IGNORECASE
)

# A posting page sits on the tenant's own ClearCompany host; the apply form adds /apply.
CLEARCOMPANY_JOB = re.compile(
    HOST_START + r"(?P<tenant>[a-z0-9-]+)\.clearcompany\.com/careers/jobs/" + _SITE,
    re.IGNORECASE,
)

CLEARCOMPANY_PAGE_SIZE = 50
CLEARCOMPANY_MAX_PAGES = 100


def _page(fetcher: BoardTransport, site: str, index: int) -> tuple[list[Any], Any, list[str]]:
    """Return one page's results, its stated total, and anything that breaks its proof."""
    payload = fetcher.get_json(clearcompany_jobs_url(site, index, CLEARCOMPANY_PAGE_SIZE))
    results = required_list_field(payload, "results", "ClearCompany")
    problems = []
    if payload.get("currentPageIndex", index) != index:
        problems.append(f"The ClearCompany site answered page {index} with another page.")
    return results, payload.get("totalCount"), problems


def _read(fetcher: BoardTransport, employer: str, groups: dict[str, str], _url: str) -> SourceRead:
    """Read every page the site's stated total covers; certify only an exact distinct count."""
    site = groups.get("site", "").casefold()
    rows: list[Any] = []
    problems: list[str] = []
    total: Any = None
    for index in range(CLEARCOMPANY_MAX_PAGES):
        if index and fetcher.deadline_elapsed():
            listings = board_listings(CLEARCOMPANY_MAPPING, rows, employer)
            raise BoardDeadlineReachedError(listings, len(rows))
        results, stated_total, page_problems = _page(fetcher, site, index)
        problems.extend(page_problems)
        if index == 0:
            total = stated_total
        elif stated_total != total:
            problems.append("The ClearCompany site changed its stated total between pages.")
        if type(total) is not int or total < 0:
            problems.append("The ClearCompany site did not state a valid totalCount.")
            rows.extend(results)
            break
        rows.extend(results)
        if not results or len(rows) >= total:
            break
    else:
        problems.append("The ClearCompany site exceeds the supported page window.")
    listings = board_listings(CLEARCOMPANY_MAPPING, rows, employer)
    found = len({listing.requisition_id for listing in listings} - {""})
    read = whole(listings, raw_count=len(rows))
    if not problems and found == total:
        return read
    note = f"The ClearCompany site states {total} jobs and {found} distinct ones mapped."
    return short_of_total(read, " ".join(dict.fromkeys([note, *problems])))


PLATFORM = BoardPlatform(
    name="clearcompany",
    label="ClearCompany",
    board_patterns=(CLEARCOMPANY_SCRIPT, CLEARCOMPANY_API),
    adapter=_read,
    job_patterns=(CLEARCOMPANY_JOB,),
    identity_rules=(
        (CLEARCOMPANY_SCRIPT, lambda match: match.group("site").casefold()),
        (CLEARCOMPANY_API, lambda match: match.group("site").casefold()),
    ),
)
