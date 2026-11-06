"""Publisher-declared FX RSS identities with full posting-page details."""

from __future__ import annotations

import re
from functools import partial
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit

from clearscout.domain.records import PageClassification
from clearscout.infrastructure.web_fetch.board_mappings import page_posting
from clearscout.infrastructure.web_fetch.board_mappings.page_posting import page_posting_declaration
from clearscout.infrastructure.web_fetch.board_platform import BoardPlatform
from clearscout.infrastructure.web_fetch.board_reads import (
    MAX_API_RESPONSE_BYTES,
    PublisherSearchPage,
    page_markup,
    publisher_search_detail,
    publisher_search_read,
    required_feed_root,
)
from clearscout.infrastructure.web_fetch.ingest import strip_tags
from clearscout.infrastructure.web_fetch.ingest.careers import inspect_publisher_page

if TYPE_CHECKING:
    from collections.abc import Mapping

    from clearscout.application.discovery.source_reading import SourceContinuationTarget, SourceRead
    from clearscout.infrastructure.web_fetch.board_platform import BoardTransport

FXRECRUITER_BOARD = re.compile(
    r"https?://(?P<tenant>[a-z0-9-]+)\.fxrecruiter\.com/jobs/?$", re.IGNORECASE
)
_MINIMUM_REQUEST_INTERVAL_SECONDS = 2.2
_DETAIL_PATH = re.compile(r"/jobs/details/.+/(?P<id>[0-9]+)/?$")
_REASON = "fxrecruiter_search_page"
_MAPPING = page_posting_declaration("fxrecruiter")


class _FeedLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.addresses: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if (
            tag == "link"
            and "alternate" in (values.get("rel") or "").lower().split()
            and values.get("type") == "application/rss+xml"
            and values.get("href")
        ):
            self.addresses.append(values["href"] or "")


def _same_publisher(address: str, source_url: str) -> bool:
    parsed, source = urlsplit(address), urlsplit(source_url)
    return parsed.scheme == source.scheme and parsed.netloc == source.netloc and not parsed.username


def _feed_url(fetcher: BoardTransport, source_url: str) -> str:
    response = fetcher.get_text(source_url, max_bytes=MAX_API_RESPONSE_BYTES)
    final, separator, _body = response.partition("\n")
    if (
        separator
        and final.startswith(("http://", "https://"))
        and not _same_publisher(final, source_url)
    ):
        raise ValueError("FX jobs root redirected outside its publisher")
    page = page_markup(response)
    if (
        inspect_publisher_page(source_url, page).classification
        is PageClassification.ERROR_OR_CHALLENGE
    ):
        raise ValueError("FX jobs root is an error or challenge page")
    parser = _FeedLinks()
    parser.feed(page)
    addresses = {urljoin(source_url, address) for address in parser.addresses}
    if len(addresses) != 1:
        raise ValueError("FX jobs root did not declare one jobs RSS feed")
    feed = addresses.pop()
    parsed = urlsplit(feed)
    if (
        not _same_publisher(feed, source_url)
        or parsed.path != "/feeds/jobs-rss"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("FX jobs feed did not belong to its publisher")
    return feed


def _request(
    fetcher: BoardTransport, feed: str, source_url: str, _page: int
) -> PublisherSearchPage:
    root = required_feed_root(fetcher.get_feed(feed), "rss", "FX jobs", feed)
    channels = root.findall("./channel")
    if len(channels) != 1:
        raise ValueError("FX jobs feed did not declare one RSS channel")
    rows = []
    for item in channels[0].findall("./item"):
        address = urljoin(feed, (item.findtext("link") or "").strip())
        identity = _DETAIL_PATH.fullmatch(urlsplit(address).path)
        if not _same_publisher(address, source_url) or identity is None:
            raise ValueError("FX jobs feed did not state a publisher posting address")
        rows.append(
            {
                "id": identity["id"],
                "url": address,
                "title": (item.findtext("title") or "").strip(),
            }
        )
    return PublisherSearchPage(rows, None)


def _detail(fetcher: BoardTransport, row: dict[str, Any]) -> Mapping[str, Any] | None:
    address = str(row["url"])
    response = fetcher.get_text(address, max_bytes=MAX_API_RESPONSE_BYTES)
    final, separator, _body = response.partition("\n")
    if (
        separator
        and final.startswith(("http://", "https://"))
        and not _same_publisher(final, address)
    ):
        return None
    page = page_markup(response)
    evidence = inspect_publisher_page(address, page)
    if evidence.classification is not PageClassification.POSTING:
        return None
    if evidence.canonical_url and (
        not _same_publisher(evidence.canonical_url, address)
        or urlsplit(evidence.canonical_url).path != urlsplit(address).path
    ):
        return None
    declared = page_posting(page)
    if declared is not None and strip_tags(declared["description"]):
        return declared
    if not evidence.title or not strip_tags(evidence.posting_body_markup):
        return None
    # A declaration stating no body still states the day the job was posted.
    return {
        "posted": declared["posted"] if declared is not None else "",
        "title": evidence.title,
        "description": evidence.posting_body_markup,
        "employer": "",
        "places": [],
        "places_complete": False,
        "employment_types": [],
        "compensation": None,
    }


def _read(fetcher: BoardTransport, employer: str, _groups: dict[str, str], url: str) -> SourceRead:
    fetcher.register_host_minimum_interval(url, _MINIMUM_REQUEST_INTERVAL_SECONDS)
    feed = _feed_url(fetcher, url)
    return publisher_search_read(
        fetcher,
        employer,
        source_url=url,
        reason=_REASON,
        first_page=0,
        request=partial(_request, fetcher, feed, url),
        mapping=_MAPPING,
        detail=partial(_detail, fetcher),
        listed_titles=True,
    )


def _continue(
    fetcher: BoardTransport, employer: str, target: SourceContinuationTarget
) -> SourceRead:
    return _read(fetcher, employer, {}, target.source_url)


def _continue_detail(
    fetcher: BoardTransport, employer: str, target: SourceContinuationTarget
) -> SourceRead:
    fetcher.register_host_minimum_interval(target.source_url, _MINIMUM_REQUEST_INTERVAL_SECONDS)
    return publisher_search_detail(
        fetcher, employer, target, mapping=_MAPPING, detail=partial(_detail, fetcher)
    )


PLATFORM = BoardPlatform(
    name="fxrecruiter",
    board_patterns=(FXRECRUITER_BOARD,),
    adapter=_read,
    continuations={_REASON: _continue, "fxrecruiter_search_detail": _continue_detail},
    label="FX jobs RSS",
)
