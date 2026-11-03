"""Lever boards on the global and EU hosts, and the public postings API."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any

from clearscout.infrastructure.web_fetch.board_mappings import LEVER_MAPPING
from clearscout.infrastructure.web_fetch.board_platform import BoardPlatform, hosted_org
from clearscout.infrastructure.web_fetch.board_reads import board_listings, whole

if TYPE_CHECKING:
    from clearscout.application.discovery.source_reading import SourceRead
    from clearscout.infrastructure.web_fetch.board_platform import BoardTransport

LEVER = re.compile(r"jobs\.(?P<region>eu\.)?lever\.co/(?P<org>[^/]+)/(?P<job>[0-9a-f-]{36})")
# Lever's EU host serves its own tenants and API, so the region stays with the board.
LEVER_BOARD = re.compile(r"jobs\.(?P<region>eu\.)?lever\.co/" + hosted_org(r"[A-Za-z0-9._-]"))
# Ashby job ids are uuids too, so a uuid in a careers URL names no board until the
# page's own markup links that same id as a Lever posting.
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def wrapped_lever_job(url: str, markup: str) -> str:
    """Return the Lever job id a page's URL names and its markup links, or an empty string."""
    named = {found.casefold() for found in _UUID.findall(url)}
    if not named:
        return ""
    return next(
        (job for link in LEVER.finditer(markup) if (job := link.group("job").casefold()) in named),
        "",
    )


_CARD_TEMPLATE = re.compile(r"cards\[[^\]]+\]\[baseTemplate\]")


class _CardTemplates(HTMLParser):
    """Collect the JSON each custom-question card's hidden ``baseTemplate`` input carries."""

    def __init__(self) -> None:
        super().__init__()
        self.cards: list[Any] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        stated = dict(attrs)
        if tag != "input" or not _CARD_TEMPLATE.fullmatch(stated.get("name") or ""):
            return
        try:
            self.cards.append(json.loads(stated.get("value") or ""))
        except ValueError:
            return


def lever_form_cards(page: str) -> list[Any]:
    """Return the custom-question cards a Lever apply page states, as their templates."""
    parser = _CardTemplates()
    parser.feed(page)
    parser.close()
    return parser.cards


def lever_apply_url(match: re.Match[str]) -> str:
    """Return the apply page of the posting a ``LEVER`` match names."""
    return (
        f"https://jobs.{match.group('region') or ''}lever.co/"
        f"{match.group('org')}/{match.group('job')}/apply"
    )


def _root(_url: str, match: re.Match[str]) -> str:
    return f"https://jobs.{match.group('region') or ''}lever.co/{match.group('org')}"


def _key(match: re.Match[str]) -> str:
    org = match.group("org").casefold()
    return f"eu/{org}" if match.group("region") else org


def _read(fetcher: BoardTransport, employer: str, groups: dict[str, str], _url: str) -> SourceRead:
    region = groups.get("region", "")
    payload = fetcher.get_json(
        f"https://api.{region}lever.co/v0/postings/{groups.get('org', '')}?mode=json"
    )
    return whole(board_listings(LEVER_MAPPING, payload, employer), raw_count=len(payload))


PLATFORM = BoardPlatform(
    name="lever",
    board_patterns=(LEVER_BOARD,),
    adapter=_read,
    job_patterns=(LEVER,),
    source_roots=((LEVER, _root),),
    identity_rules=((LEVER_BOARD, _key),),
    posting_api=True,
)
