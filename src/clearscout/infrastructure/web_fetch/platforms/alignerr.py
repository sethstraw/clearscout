"""A Next jobs page whose declared bundle uses a public offset search."""

from __future__ import annotations

import json
import re
from functools import partial
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

from clearscout.infrastructure.web_fetch.board_mappings.page_posting import page_posting_declaration
from clearscout.infrastructure.web_fetch.board_platform import BoardPlatform
from clearscout.infrastructure.web_fetch.board_reads import (
    MAX_API_RESPONSE_BYTES,
    PublisherSearchBoard,
    PublisherSearchPage,
    page_markup,
    page_posting_detail,
    publisher_search_read,
    required_list_field,
)
from clearscout.infrastructure.web_fetch.ingest.html_blocks import html_blocks

if TYPE_CHECKING:
    from clearscout.application.discovery.source_reading import SourceContinuationTarget, SourceRead
    from clearscout.infrastructure.web_fetch.board_platform import BoardTransport

ALIGNERR_CONFIG = re.compile(
    r'<script\b[^>]*\bid=["\']__NEXT_DATA__["\'][^>]*>[^<]*"initialJobs"\s*:',
    re.IGNORECASE,
)
_JOBS_BUNDLE = re.compile(r"/_next/static/chunks/pages/jobs-[A-Za-z0-9_-]+\.js")
_REASON = "alignerr_search_page"
_MAPPING = page_posting_declaration("alignerr", row_places=True)


def _config(fetcher: BoardTransport, source_url: str) -> tuple[str, dict[str, str]]:
    page = page_markup(fetcher.get_text(source_url, max_bytes=MAX_API_RESPONSE_BYTES))
    scripts = html_blocks(page, "script")
    roots = [block for block in scripts if block.attrs.get("id") == "__NEXT_DATA__"]
    if len(roots) != 1:
        raise ValueError("Jobs root did not declare one Next page")
    data = json.loads(roots[0].inner)
    props = data.get("props", {}).get("pageProps", {}) if isinstance(data, dict) else {}
    if (
        not isinstance(props, dict)
        or not isinstance(props.get("initialJobs"), list)
        or data.get("page") != "/jobs"
    ):
        raise ValueError("Next page did not declare the jobs search")
    source = urlsplit(source_url)
    bundles = []
    for block in scripts:
        address = urlsplit(urljoin(source_url, block.attrs.get("src", "")))
        if (
            _JOBS_BUNDLE.fullmatch(address.path)
            and address.scheme == source.scheme
            and address.netloc == source.netloc
            and not address.username
        ):
            bundles.append(address.geturl())
    if len(set(bundles)) != 1:
        raise ValueError("Jobs root did not declare one same-origin jobs bundle")
    response = fetcher.get_text(bundles[0], max_bytes=MAX_API_RESPONSE_BYTES)
    final, separator, _body = response.partition("\n")
    if separator and final.startswith(("http://", "https://")):
        redirected = urlsplit(final)
        if redirected.scheme != source.scheme or redirected.netloc != source.netloc:
            raise ValueError("Jobs bundle redirected outside its root")
    script = page_markup(response)
    if (
        re.search(r'\bfetch\(\s*["\']/api/jobs\?["\']', script) is None
        or "URLSearchParams" not in script
        or "offset:" not in script
        or "limit:" not in script
    ):
        raise ValueError("Jobs bundle did not declare its public offset search")
    filters = dict(parse_qsl(source.query, keep_blank_values=True))
    filters.pop("offset", None)
    filters.pop("limit", None)
    return source._replace(path="/api/jobs", query="", fragment="").geturl(), filters


def _request(
    fetcher: BoardTransport,
    endpoint: str,
    filters: dict[str, str],
    offset: int,
) -> PublisherSearchPage:
    query = {**filters, "limit": "60", "offset": str(offset)}
    payload = fetcher.get_json(endpoint + "?" + urlencode(query))
    jobs = required_list_field(payload, "jobs", "Public jobs search")
    rows = []
    for job in jobs:
        if not isinstance(job, dict) or not job.get("id") or not job.get("applyUrl"):
            raise ValueError("Public jobs search did not state an identity and apply URL")
        rows.append(
            {
                "id": str(job["id"]),
                "url": urljoin(endpoint, str(job["applyUrl"])),
                "title": job.get("title", ""),
                "summary": job.get("description", ""),
                "places": [job.get("location", "")],
            }
        )
    total = payload.get("total") if isinstance(payload, dict) else None
    return PublisherSearchPage(
        rows,
        offset + len(jobs) if jobs else None,
        total if type(total) is int and total >= 0 else None,
    )


def _search(
    fetcher: BoardTransport,
    employer: str,
    source_url: str,
    target: SourceContinuationTarget | None = None,
) -> SourceRead:
    endpoint, filters = _config(fetcher, source_url)
    return publisher_search_read(
        fetcher,
        employer,
        source_url=source_url,
        reason=_REASON,
        first_page=0,
        request=partial(_request, fetcher, endpoint, filters),
        mapping=_MAPPING,
        detail=page_posting_detail(fetcher),
        target=target,
        enumeration_only=True,
    )


_BOARD = PublisherSearchBoard(_search, _MAPPING, page_posting_detail)

PLATFORM = BoardPlatform(
    name="alignerr",
    board_patterns=(ALIGNERR_CONFIG,),
    adapter=_BOARD.read,
    continuations={_REASON: _BOARD.resume, "alignerr_search_detail": _BOARD.resume_detail},
    label="Public jobs search",
)
