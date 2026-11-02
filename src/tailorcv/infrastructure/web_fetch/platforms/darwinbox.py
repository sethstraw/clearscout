"""Darwinbox public career lists bound to the selected tenant and company path."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import urlencode, urlsplit

from tailorcv.infrastructure.web_fetch.board_mappings.page_posting import page_posting_declaration
from tailorcv.infrastructure.web_fetch.board_platform import HOST_START, BoardPlatform
from tailorcv.infrastructure.web_fetch.board_reads import (
    PublisherSearchPage,
    RecoverableSearchBoard,
    publisher_search_or_recovery,
)

if TYPE_CHECKING:
    from tailorcv.application.discovery.source_reading import SourceContinuationTarget, SourceRead
    from tailorcv.infrastructure.web_fetch.board_platform import BoardTransport

DARWINBOX_BOARD = re.compile(
    HOST_START + r"(?P<host>[a-z0-9-]+\.darwinbox\.com)"
    r"/ms/candidatev2/(?P<company>[A-Za-z0-9_-]+)/careers/(?:allJobs|home)(?=[/?#]|$)"
)
_PENDING = "darwinbox_search_page"
_MAPPING = page_posting_declaration("darwinbox")


def _read(
    fetcher: BoardTransport,
    employer: str,
    groups: dict[str, str],
    url: str,
    *,
    target: SourceContinuationTarget | None = None,
    recovery: SourceContinuationTarget | None = None,
) -> SourceRead:
    """Read the public company search without credentials or browser cookies."""
    host, company = groups["host"], groups["company"]
    if urlsplit(url).query or urlsplit(url).fragment:
        raise ValueError("Darwinbox selected search filters are not supported.")
    endpoint = f"https://{host}/ms/candidateapi/job/alljobs?" + urlencode({"companyId": company})

    def request(page: int) -> PublisherSearchPage:
        payload = fetcher.post_json(
            endpoint, {"companyId": company, "page": page, "sort_option": "new", "limit": 10}
        )
        records = payload.get("data")
        if payload.get("status") != "success" or not isinstance(records, list):
            raise ValueError("Darwinbox public search returned no valid company list.")
        rows = []
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("Darwinbox search contains an invalid posting record.")
            item = str(record.get("id") or "")
            if not re.fullmatch(r"[A-Za-z0-9_-]+", item):
                raise ValueError("Darwinbox public record has no stable detail identity.")
            address = f"https://{host}/ms/candidatev2/{company}/careers/jobDetails/{item}"
            rows.append(
                {
                    "id": item,
                    "url": address,
                    "title": record.get("title", ""),
                    "publisher": record,
                    "detail": {
                        "title": record.get("title", ""),
                        "description": record.get("jd", ""),
                        "places": record.get("tool_tip_locations", []),
                    },
                }
            )
        return PublisherSearchPage(rows, page + 1 if rows else None, payload.get("job_counts"))

    return publisher_search_or_recovery(
        fetcher,
        employer,
        source_url=url,
        reason=_PENDING,
        first_page=1,
        request=request,
        mapping=_MAPPING,
        detail=lambda row: row["detail"],
        target=target,
        recovery=recovery,
    )


def _source_groups(target: SourceContinuationTarget) -> dict[str, str]:
    """Return the public tenant and company a retained target's search was read under."""
    match = DARWINBOX_BOARD.search(target.source_url)
    if match is None:
        raise ValueError("Darwinbox continuation has no supported company source.")
    return match.groupdict()


_BOARD = RecoverableSearchBoard(_read, _source_groups)

PLATFORM = BoardPlatform(
    name="darwinbox",
    label="Darwinbox",
    board_patterns=(DARWINBOX_BOARD,),
    adapter=_read,
    continuations={_PENDING: _BOARD.resume, "darwinbox_search_detail": _BOARD.resume_detail},
)
