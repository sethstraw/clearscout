"""iCIMS's posting record: a Jibe site's API ``data`` object, or a classic site's posting page."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from clearscout.domain.staged_facts import PostedDateGrade
from clearscout.infrastructure.web_fetch.board_mappings.mapper import (
    BoardDeclaration,
    Posting,
    pay_range,
    posted_on,
    stated,
    stated_text,
)
from clearscout.infrastructure.web_fetch.board_mappings.page_posting import (
    page_posting_declaration,
)

_JOB_ID = re.compile(r"^/jobs/(\d+)(?:/|$)")


def icims_job_page(address: str) -> str:
    """Return the readable posting page for one apply_url, or "" when there is none.

    apply_url may be a login gate, and /jobs/<id>/job is a shell whose description
    lives in the in_iframe=1 body, which also carries the JobPosting markup.
    """
    parts = urlsplit(address)
    match = _JOB_ID.match(parts.path)
    if match:
        return f"{parts.scheme}://{parts.netloc}/jobs/{match.group(1)}/job?in_iframe=1"
    return "" if parts.path.rstrip("/").endswith("/login") else address


def _url(posting: Posting) -> str:
    return icims_job_page(stated_text(posting.record, ("apply_url",)))


def icims_sitemap_url(host: str) -> str:
    """Return the sitemap a classic iCIMS career site declares in its robots.txt."""
    return f"https://{host}/sitemap.xml"


def icims_openings(pages: list[str], host: str) -> list[dict[str, str]]:
    """Return one row per posting page on ``host``, addressed as its readable iframe body."""
    posting = re.compile(rf"https?://{re.escape(host)}/jobs/(?P<job>\d+)/(?:[^?#]+/)?job(?:\?.*)?")
    return [
        {"id": match.group("job"), "url": icims_job_page(page)}
        for page in pages
        if (match := posting.fullmatch(page)) is not None
    ]


def _compensation(posting: Posting) -> str | None:
    """Return a Jibe job's salary bounds; the API states neither currency nor period."""
    record = posting.record
    return pay_range(
        "", stated(record, ("salary_min_value",)), stated(record, ("salary_max_value",))
    )


# A classic site's list is its sitemap, and each posting page declares its JobPosting.
ICIMS_CLASSIC_MAPPING = page_posting_declaration("icims")

ICIMS_MAPPING = BoardDeclaration(
    platform="icims",
    title=("title",),
    url=_url,
    location=("full_location",),
    requisition_id=("req_id",),
    places=("full_location",),
    description=(("description",),),
    compensation=_compensation,
    # create_date and update_date move when a tenant re-indexes; posted_date does not.
    posted=posted_on(("posted_date",), PostedDateGrade.FIRST_POSTED),
)
