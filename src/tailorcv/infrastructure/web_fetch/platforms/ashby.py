"""Ashby boards and the public job-board API with compensation."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, quote, unquote

from tailorcv.infrastructure.web_fetch.board_mappings import ASHBY_MAPPING
from tailorcv.infrastructure.web_fetch.board_platform import (
    BoardPlatform,
    hosted_org,
    query_groups,
)
from tailorcv.infrastructure.web_fetch.board_reads import board_listings, whole

if TYPE_CHECKING:
    from tailorcv.application.discovery.source_reading import SourceRead
    from tailorcv.infrastructure.web_fetch.board_platform import BoardTransport

ASHBY = re.compile(r"jobs\.ashbyhq\.com/(?P<org>[^/]+)/(?P<job>[0-9a-f-]{36})")
# An Ashby org may name a space, which its address percent-encodes.
ASHBY_BOARD = re.compile(r"jobs\.ashbyhq\.com/" + hosted_org(r"[A-Za-z0-9._%-]"))


ASHBY_FORM_API = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting"
# The query Ashby's own job page sends for the application form; the posting API omits it.
_FORM_QUERY = (
    "query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) "
    "{ jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName, "
    "jobPostingId: $jobPostingId) { id applicationForm { sections { fieldEntries "
    "{ ... on FormFieldEntry { id field isRequired } } } } } }"
)


def ashby_form_request(match: re.Match[str]) -> dict[str, Any]:
    """Return the form query body for the posting an ``ASHBY`` match names."""
    return {
        "operationName": "ApiJobPosting",
        "variables": {
            "organizationHostedJobsPageName": unquote(match.group("org")),
            "jobPostingId": match.group("job"),
        },
        "query": _FORM_QUERY,
    }


def ashby_form(payload: Any) -> Any:
    """Return the ``applicationForm`` a form query answered, or None."""
    for key in ("data", "jobPosting", "applicationForm"):
        payload = payload.get(key) if isinstance(payload, dict) else None
    return payload


def _read(fetcher: BoardTransport, employer: str, groups: dict[str, str], _url: str) -> SourceRead:
    # Pay is published only under `includeCompensation`, and only nested.
    org = groups.get("org", "")
    payload = fetcher.get_json(
        f"https://api.ashbyhq.com/posting-api/job-board/{quote(unquote(org), safe='')}"
        "?includeCompensation=true"
    )
    jobs = payload.get("jobs", [])
    # A staffing tenant hosts several client companies, each one a department.
    department = dict(parse_qsl(groups.get("board_query", ""))).get("department")
    if department is not None:
        jobs = [job for job in jobs if job.get("department") == department]
    return whole(board_listings(ASHBY_MAPPING, jobs, employer), raw_count=len(jobs))


PLATFORM = BoardPlatform(
    name="ashby",
    board_patterns=(ASHBY_BOARD,),
    adapter=_read,
    job_patterns=(ASHBY,),
    source_roots=((ASHBY, lambda _url, match: f"https://jobs.ashbyhq.com/{match.group('org')}"),),
    identity_rules=((ASHBY_BOARD, lambda match: match.group("org").casefold()),),
    posting_api=True,
    adapter_groups=query_groups,
)
