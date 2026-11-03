"""Greenhouse boards, their embeds, and the public job-board API."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from clearscout.infrastructure.web_fetch.board_mappings import GREENHOUSE_MAPPING
from clearscout.infrastructure.web_fetch.board_platform import HOST_START, BoardPlatform
from clearscout.infrastructure.web_fetch.board_reads import board_listings, whole

if TYPE_CHECKING:
    from clearscout.application.discovery.source_reading import SourceRead
    from clearscout.infrastructure.web_fetch.board_platform import BoardTransport

# The EU data centre serves the same boards under an `eu.` label.
GREENHOUSE = re.compile(r"boards\.(?:eu\.)?greenhouse\.io/(?P<org>[^/]+)/jobs/(?P<job>\d+)")
GREENHOUSE_EMBED = re.compile(
    r"job-boards\.(?:eu\.)?greenhouse\.io/(?P<org>[^/]+)/jobs/(?P<job>\d+)"
)

# An employer careers page can host the Greenhouse widget in place of a
# permalink: the `gh_jid` query parameter names the job on any URL, and the
# embed writes its board token into the page markup.
GREENHOUSE_HOSTED_JID = re.compile(r"[?&]gh_jid=(?P<job>\d+)")
GREENHOUSE_BOARD_TOKEN = re.compile(r"""data-board-token=["'](?P<org>[A-Za-z0-9._-]+)["']""")

# JSON board URL shapes; the embed path is handled separately.
GREENHOUSE_BOARD = re.compile(
    r"(?:boards|job-boards)\.(?:eu\.)?greenhouse\.io/(?!embed)(?P<org>[A-Za-z0-9._-]+)"
)
# Embedded board URLs use the `for` parameter across several path shapes.
GREENHOUSE_BOARD_EMBED = re.compile(
    HOST_START + r"greenhouse\.io/embed/job_board[^\"'\s]*[?&]for=(?P<org>[A-Za-z0-9._-]+)",
    re.IGNORECASE,
)
GREENHOUSE_APPLICATION_EMBED = re.compile(
    r"(?:^|//)boards\.(?:eu\.)?greenhouse\.io/embed/job_app[?&][^\"'\s]*"
    r"\bfor=(?P<org>[A-Za-z0-9._-]+)",
    re.IGNORECASE,
)


def _root(url: str, match: re.Match[str]) -> str:
    # The org is free text, so the region is read from the matched host, which ends
    # at the first slash after the match starts; a scheme-less address has no parse.
    host = url[: url.find("/", match.start())].casefold()
    region = "eu." if host.endswith(".eu.greenhouse.io") else ""
    return f"https://job-boards.{region}greenhouse.io/{match.group('org')}"


def _org_key(match: re.Match[str]) -> str:
    return match.group("org").casefold()


def _read(fetcher: BoardTransport, employer: str, groups: dict[str, str], _url: str) -> SourceRead:
    # `content=true` returns each posting's body markup so the arrangement
    # signal published there ("#LI-remote" and friends) is visible; the
    # "Work Setting" metadata is returned either way.
    org = groups.get("org", "")
    payload = fetcher.get_json(
        f"https://boards-api.greenhouse.io/v1/boards/{org}/jobs?content=true"
    )
    jobs = payload.get("jobs", [])
    return whole(board_listings(GREENHOUSE_MAPPING, jobs, employer), raw_count=len(jobs))


PLATFORM = BoardPlatform(
    name="greenhouse",
    board_patterns=(GREENHOUSE_BOARD_EMBED, GREENHOUSE_BOARD),
    adapter=_read,
    job_patterns=(GREENHOUSE, GREENHOUSE_EMBED),
    # The retired embed, a job permalink, and a tracked tenant URL all fold to the board.
    source_roots=(
        (GREENHOUSE_BOARD_EMBED, _root),
        (GREENHOUSE_EMBED, _root),
        (GREENHOUSE, _root),
        (GREENHOUSE_BOARD, _root),
    ),
    identity_rules=((GREENHOUSE_BOARD, _org_key), (GREENHOUSE_APPLICATION_EMBED, _org_key)),
    posting_api=True,
)
