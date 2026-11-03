"""HiBob careers sites, read through the job-ad feed their own app reads."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from clearscout.infrastructure.web_fetch.board_mappings import HIBOB_MAPPING, hibob_job_ads_url
from clearscout.infrastructure.web_fetch.board_platform import HOST_END, HOST_START, BoardPlatform
from clearscout.infrastructure.web_fetch.board_reads import (
    board_listings,
    required_list_field,
    whole,
)

if TYPE_CHECKING:
    from clearscout.application.discovery.source_reading import SourceRead
    from clearscout.infrastructure.web_fetch.board_platform import BoardTransport

# A posting address is an app route that serves no posting markup, so no job pattern reads one.
HIBOB_BOARD = re.compile(
    HOST_START + r"(?P<tenant>(?!www\.)[a-z0-9-]+)\.careers\.hibob\.com" + HOST_END,
    re.IGNORECASE,
)


def _read(fetcher: BoardTransport, employer: str, groups: dict[str, str], _url: str) -> SourceRead:
    """Read every published job ad, each stating its whole body, in one request.

    The feed answers only a request naming its company, so the tenant travels as a header.
    """
    tenant = groups.get("tenant", "").casefold()
    payload = fetcher.get_json(hibob_job_ads_url(tenant), headers={"companyidentifier": tenant})
    ads = required_list_field(payload, "jobAdDetails", "HiBob")
    return whole(
        board_listings(HIBOB_MAPPING, ads, employer, {"tenant": tenant}), raw_count=len(ads)
    )


PLATFORM = BoardPlatform(
    name="hibob",
    label="HiBob",
    board_patterns=(HIBOB_BOARD,),
    adapter=_read,
    source_roots=(
        (
            HIBOB_BOARD,
            lambda _url, match: f"https://{match.group('tenant').casefold()}.careers.hibob.com",
        ),
    ),
    employer_slug=True,
)
