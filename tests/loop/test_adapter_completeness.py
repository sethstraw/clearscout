"""A board, sitemap, or host policy read never claims or drops more than it saw. No network."""

from __future__ import annotations

import importlib
import json
import pkgutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from socket import AF_INET, SOCK_STREAM
from typing import Any, cast, get_args
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from clearscout.application.discovery.source_reading import (
    SourceContinuationTarget,
    SourceDetailCheckpoint,
    SourceRead,
    SourceReadOptions,
)
from clearscout.domain import SourceReadOutcome
from clearscout.infrastructure.web_fetch import board_adapters, boards
from clearscout.infrastructure.web_fetch import platforms as platform_package
from clearscout.infrastructure.web_fetch.board_adapters import (
    STRUCTURED_PAGE_CURSOR_REASONS,
    BoardApiFetcher,
    identify_board,
    identify_board_url,
)
from clearscout.infrastructure.web_fetch.board_platform import BoardKind
from clearscout.infrastructure.web_fetch.ingest.sitemap import SitemapLimits, discover_job_urls
from clearscout.infrastructure.web_fetch.ingest.url import posting_readers
from clearscout.infrastructure.web_fetch.listing_facts import (
    EXHAUSTIVE_LOCATION_PROVIDERS,
    NON_EXHAUSTIVE_LOCATION_PROVIDERS,
)
from clearscout.infrastructure.web_fetch.platforms import PLATFORMS, workday
from clearscout.infrastructure.web_fetch.policy import (
    HostHeldError,
    PoliteHttpClient,
    RobotsDeniedError,
)
from clearscout.infrastructure.web_fetch.publisher_sources import (
    REGISTERED_POSTING_LINK,
    PublisherReadLimits,
    PublisherSourceFetcher,
)
from tests.support.doubles import ScriptedPage, published_listings
from tests.support.http import SITEMAP_NS, sitemap_urlset

_SR_API = "https://api.smartrecruiters.com/v1/companies/Globex/postings"


def _sr_job(index: int) -> dict[str, Any]:
    return {"id": str(index), "name": f"Role {index}", "location": {"city": "Northtown"}}


class _ScriptedSmartRecruiters(BoardApiFetcher):
    """Serve SmartRecruiters pages by offset; ``fail_at`` raises for that offset."""

    def __init__(self, total: int, *, page_size: int = 100, fail_at: int | None = None):
        self.total = total
        self.page_size = page_size
        self.fail_at = fail_at
        self.requested: list[str] = []

    def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
        self.requested.append(url)
        offset = int(parse_qs(urlsplit(url).query).get("offset", ["0"])[0])
        if offset == self.fail_at:
            raise httpx.ConnectError("unreachable", request=httpx.Request("GET", url))
        count = max(0, min(self.page_size, self.total - offset))
        return {
            "offset": offset,
            "limit": 100,
            "totalFound": self.total,
            "content": [_sr_job(offset + index) for index in range(count)],
        }


def test_smartrecruiters_pages_by_offset_until_its_stated_total() -> None:
    fetcher = _ScriptedSmartRecruiters(total=130)

    read = fetcher.listings("Globex", "https://careers.smartrecruiters.com/Globex")

    assert fetcher.requested == [
        f"{_SR_API}?limit=100&offset=0",
        f"{_SR_API}?limit=100&offset=100",
    ]
    assert read.complete is True
    assert len(read.listings) == 130
    assert read.listings[-1].title == "Role 129"


def test_a_short_smartrecruiters_page_does_not_end_a_read_below_its_stated_total() -> None:
    fetcher = _ScriptedSmartRecruiters(total=130, page_size=60)

    read = fetcher.listings("Globex", "https://careers.smartrecruiters.com/Globex")

    assert fetcher.requested == [
        f"{_SR_API}?limit=100&offset=0",
        f"{_SR_API}?limit=100&offset=60",
        f"{_SR_API}?limit=100&offset=120",
    ]
    assert read.complete is True
    assert len(read.listings) == 130


def test_a_failed_later_smartrecruiters_page_is_incomplete_and_resumable() -> None:
    fetcher = _ScriptedSmartRecruiters(total=130, fail_at=100)

    read = fetcher.listings("Globex", "https://careers.smartrecruiters.com/Globex")

    assert read.complete is False
    assert len(read.listings) == 100
    assert read.skip_reasons == {"fetch_failed": 1}
    assert read.exhausted_outcome is SourceReadOutcome.PUBLISHER_INDEX
    [cursor] = read.deferred_targets
    assert cursor.url == f"{_SR_API}?limit=100&offset=100"
    # A cursor found mid-chain stays automatic, so the tail is read without a manual step.
    assert cursor.deferred_reason in STRUCTURED_PAGE_CURSOR_REASONS

    fetcher.fail_at = None
    continued = fetcher.continue_listings(
        "Globex",
        read.deferred_targets,
        root_url="https://careers.smartrecruiters.com/Globex",
        handoff_scopes=(),
    )

    assert [listing.title for listing in continued.listings] == [
        f"Role {index}" for index in range(100, 130)
    ]
    assert continued.deferred_targets == ()
    assert continued.attempted_target_urls == (cursor.url,)


def test_a_deadline_cursor_is_continuation_work_not_a_time_limit_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.board_adapters.read_scope.MAX_PAGE_READ_SECONDS", 0.0
    )
    fetcher = _ScriptedSmartRecruiters(total=250)

    read = fetcher.listings("Globex", "https://careers.smartrecruiters.com/Globex")

    assert fetcher.requested == []
    assert read.complete is False
    assert read.skip_reasons == {}
    assert read.exhausted_outcome is SourceReadOutcome.PUBLISHER_INDEX
    assert [target.url for target in read.deferred_targets] == [f"{_SR_API}?limit=100&offset=0"]


def test_a_held_host_leaves_a_cursor_retryable_without_a_request() -> None:
    requested: list[str] = []

    def get(url: str, **_kwargs: object) -> httpx.Response:
        request = httpx.Request("GET", url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text="", request=request)
        requested.append(url)
        return httpx.Response(429, headers={"Retry-After": "300"}, request=request)

    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda _seconds: None,
        jitter=lambda _low, _high: 0.0,
        getter=get,
        resolver=_public_resolver,
        clock=lambda: 100.0,
    )
    fetcher = BoardApiFetcher(client=client)
    cursor = SourceContinuationTarget(
        url=f"{_SR_API}?limit=100&offset=100",
        label="SmartRecruiters page at offset 100",
        label_source="none",
        source_url="https://jobs.smartrecruiters.com/Globex",
        structural_context="other",
        relations=("same-origin", "publisher-content"),
        deferred_reason="smartrecruiters_page_pending",
    )
    first = fetcher.continue_listings(
        "Globex", (cursor,), root_url="https://jobs.smartrecruiters.com/Globex", handoff_scopes=()
    )
    requested.clear()

    held = fetcher.continue_listings(
        "Globex", (cursor,), root_url="https://jobs.smartrecruiters.com/Globex", handoff_scopes=()
    )

    assert first.attempted_target_urls == (cursor.url,)
    assert requested == []
    assert held.attempted_target_urls == ()
    assert held.deferred_targets == (cursor,)
    assert held.skip_reasons == {"host_held": 1}


_WD_API = "https://globex.wd1.myworkdayjobs.com/wday/cxs/globex/Careers/jobs"
_WD_SITE = "https://globex.wd1.myworkdayjobs.com/Careers"
_SR_SITE = "https://jobs.smartrecruiters.com/Globex"
_BOARD_PAGE = 20
_BOARD_TOTAL = 900


def _workday_payload(offset: int, count: int) -> dict[str, Any]:
    return {
        "total": _BOARD_TOTAL,
        "jobPostings": [
            {"title": f"Role {offset + index}", "externalPath": f"/job/Role-{offset + index}"}
            for index in range(count)
        ],
    }


def _smartrecruiters_payload(offset: int, count: int) -> dict[str, Any]:
    return {
        "offset": offset,
        "limit": _BOARD_PAGE,
        "totalFound": _BOARD_TOTAL,
        "content": [_sr_job(offset + index) for index in range(count)],
    }


@dataclass(frozen=True)
class _CursorBoard:
    """One page-cursor board, named by how its pages and continuation URLs are built."""

    reason: str
    source_url: str
    endpoint: str
    payload: Callable[[int, int], dict[str, Any]]

    def page_url(self, offset: int) -> str:
        """The frontier URL for the page at ``offset``, as the adapter writes it."""
        return f"{self.endpoint}?limit={_BOARD_PAGE}&offset={offset}"

    def cursor(self, offset: int) -> SourceContinuationTarget:
        """One pending frontier row for the page at ``offset``."""
        return SourceContinuationTarget(
            url=self.page_url(offset),
            label=f"Board page at offset {offset}",
            label_source="none",
            source_url=self.source_url,
            structural_context="other",
            relations=("same-origin", "publisher-content"),
            automatic_eligible=True,
            deferred_reason=self.reason,
        )


_CURSOR_BOARDS = (
    _CursorBoard(
        reason="workday_page_pending",
        source_url=_WD_SITE,
        endpoint=_WD_API,
        payload=_workday_payload,
    ),
    _CursorBoard(
        reason="smartrecruiters_page_pending",
        source_url=_SR_SITE,
        endpoint=_SR_API,
        payload=_smartrecruiters_payload,
    ),
)


class _PagedBoard:
    """Answer offset-paged board pages from memory, recording the page URLs requested.

    Workday carries its offset in a POST body, so the recorded URL is rebuilt
    from that body in the same form the frontier holds.
    """

    def __init__(
        self,
        payload: Callable[[int, int], dict[str, Any]],
        *,
        unreachable: frozenset[str] = frozenset(),
        short_at: int | None = None,
    ) -> None:
        """Serve ``payload`` per page, refusing ``unreachable`` and shortening ``short_at``."""
        self.payload = payload
        self.unreachable = unreachable
        self.short_at = short_at
        self.requested: list[str] = []
        # Page requests and pacing waits, in the order the policy made them.
        self.paced: list[tuple[str, float]] = []

    def get(self, url: str, **_kwargs: Any) -> httpx.Response:
        """Answer robots and every GET-paged board page."""
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text="", request=httpx.Request("GET", url))
        query = parse_qs(urlsplit(url).query)
        return self._page(
            "GET", url, int(query["offset"][0]), int(query.get("limit", [_BOARD_PAGE])[0])
        )

    def post(self, url: str, *, json: Any, **_kwargs: Any) -> httpx.Response:
        """Answer one POST-paged board page addressed by its request body."""
        offset, limit = int(json["offset"]), int(json["limit"])
        return self._page("POST", f"{url}?limit={limit}&offset={offset}", offset, limit)

    def _page(self, method: str, page_url: str, offset: int, limit: int) -> httpx.Response:
        self.requested.append(page_url)
        self.paced.append((page_url, 0.0))
        request = httpx.Request(method, page_url)
        if page_url in self.unreachable:
            raise httpx.ConnectError("unreachable", request=request)
        count = limit - 5 if offset == self.short_at else limit
        return httpx.Response(200, request=request, json=self.payload(offset, count))

    def client(self, *, interval: float = 0.0) -> PoliteHttpClient:
        """Wire these pages behind the outbound policy, pacing each host by ``interval``."""
        return PoliteHttpClient(
            user_agent="clearscout/test",
            sleeper=lambda seconds: self.paced.append(("wait", seconds)),
            jitter=lambda _low, _high: interval,
            getter=self.get,
            poster=self.post,
            resolver=_public_resolver,
            clock=lambda: 0.0,
        )


@pytest.mark.parametrize("board", _CURSOR_BOARDS, ids=lambda board: board.reason)
def test_board_pages_read_in_one_batch_are_not_deferred_again_by_their_neighbours(
    board: _CursorBoard, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each page names the next as pending, including pages this same batch just read."""
    monkeypatch.setattr(workday, "WORKDAY_CONTINUATION_PAGES", 3)
    server = _PagedBoard(board.payload)
    fetcher = BoardApiFetcher(client=server.client())
    frontier = tuple(board.cursor(offset) for offset in (400, 420, 440))

    read = fetcher.continue_listings(
        "Globex", frontier, root_url=board.source_url, handoff_scopes=()
    )

    assert len(read.listings) == 3 * _BOARD_PAGE
    assert [target.url for target in read.deferred_targets] == [board.page_url(460)]
    assert read.attempted_target_urls == tuple(target.url for target in frontier)
    assert server.requested == [board.page_url(offset) for offset in (400, 420, 440)]


@pytest.mark.parametrize(
    ("board", "resumed_at"),
    # A Workday cursor reads three pages from 420; a SmartRecruiters cursor reads one each.
    [(_CURSOR_BOARDS[0], 480), (_CURSOR_BOARDS[1], 460)],
    ids=[board.reason for board in _CURSOR_BOARDS],
)
def test_an_unreachable_board_page_stays_deferred_while_its_read_neighbours_do_not(
    board: _CursorBoard, resumed_at: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workday, "WORKDAY_CONTINUATION_PAGES", 3)
    server = _PagedBoard(board.payload, unreachable=frozenset({board.page_url(400)}))
    fetcher = BoardApiFetcher(client=server.client())
    frontier = tuple(board.cursor(offset) for offset in (400, 420, 440))

    read = fetcher.continue_listings(
        "Globex", frontier, root_url=board.source_url, handoff_scopes=()
    )

    assert [target.url for target in read.deferred_targets] == [
        board.page_url(400),
        board.page_url(resumed_at),
    ]
    assert len(server.requested) == len(set(server.requested))
    assert read.skip_reasons == {"fetch_failed": 1}


def test_one_workday_cursor_reads_its_batch_neighbours_without_requesting_them_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget = 5
    monkeypatch.setattr(workday, "WORKDAY_CONTINUATION_PAGES", budget)
    board = _CURSOR_BOARDS[0]
    server = _PagedBoard(board.payload)
    fetcher = BoardApiFetcher(client=server.client())
    frontier = tuple(board.cursor(offset) for offset in (400, 420, 440))

    read = fetcher.continue_listings(
        "Globex", frontier, root_url=board.source_url, handoff_scopes=()
    )

    pages = [board.page_url(400 + page * _BOARD_PAGE) for page in range(budget)]
    assert server.requested == pages
    assert read.attempted_target_urls == tuple(pages)
    assert len(read.listings) == budget * _BOARD_PAGE
    assert [target.url for target in read.deferred_targets] == [
        board.page_url(400 + budget * _BOARD_PAGE)
    ]


def test_each_continued_workday_page_waits_its_host_interval_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workday, "WORKDAY_CONTINUATION_PAGES", 4)
    board = _CURSOR_BOARDS[0]
    server = _PagedBoard(board.payload)
    fetcher = BoardApiFetcher(client=server.client(interval=0.5))

    read = fetcher.continue_listings(
        "Globex", (board.cursor(400),), root_url=board.source_url, handoff_scopes=()
    )

    pages = [board.page_url(offset) for offset in (400, 420, 440, 460)]
    first = server.paced.index((pages[0], 0.0))
    assert server.paced[first:] == [
        (pages[0], 0.0),
        ("wait", 0.5),
        (pages[1], 0.0),
        ("wait", 0.5),
        (pages[2], 0.0),
        ("wait", 0.5),
        (pages[3], 0.0),
    ]
    assert [target.url for target in read.deferred_targets] == [board.page_url(480)]


def test_a_short_final_workday_page_leaves_nothing_in_the_frontier() -> None:
    board = _CURSOR_BOARDS[0]
    final_offset = 440 + _BOARD_PAGE - 5

    def payload(offset: int, count: int) -> dict[str, Any]:
        return {
            **board.payload(offset, min(count, max(final_offset - offset, 0))),
            "total": final_offset,
        }

    server = _PagedBoard(payload, short_at=440)
    fetcher = BoardApiFetcher(client=server.client())
    frontier = tuple(
        replace(
            board.cursor(offset),
            detail_checkpoint=SourceDetailCheckpoint(
                platform="search:workday",
                board_url=board.source_url,
                listing_ids=tuple(
                    f"{board.source_url}/job/Role-{identifier}" for identifier in range(offset)
                ),
                listing_total=final_offset,
            ),
        )
        for offset in (400, 420, 440)
    )

    read = fetcher.continue_listings(
        "Globex", frontier, root_url=board.source_url, handoff_scopes=()
    )

    assert len(read.listings) == 2 * _BOARD_PAGE + 15
    assert {listing.url for listing in read.listings} == {
        f"{board.source_url}/job/Role-{identifier}" for identifier in range(400, final_offset)
    }
    assert read.deferred_targets == ()
    # A stated total may be capped, so the short page is confirmed by an empty one.
    assert server.requested == [board.page_url(offset) for offset in (400, 420, 440, 460)]
    assert read.outcome is SourceReadOutcome.PUBLISHER_LINKED_PAGES
    assert read.note == "Read 3 durable source-continuation targets."


def test_board_api_fetcher_forwards_continuation_scope_to_the_publisher_reader() -> None:
    url = "https://www.example-health.org/careers/opening-2"
    target = SourceContinuationTarget(
        url=url,
        label="Opening Two",
        label_source="visible",
        source_url="https://www.example-health.org/careers/",
        structural_context="list",
        relations=("same-origin", "repeated-target-shape"),
        depth=2,
    )
    page = """
    <main><ul>
      <li><a href="/careers/opening-3">Opening Three</a></li>
      <li><a href="/careers/opening-4">Opening Four</a></li>
      <li><a href="/info/patient-resources">Patient Resources</a></li>
    </ul></main>
    """
    fetcher = BoardApiFetcher(client=cast(Any, _Pages({url: page})))

    result = fetcher.continue_listings(
        "Example Health",
        (target,),
        root_url="https://careers.example.org/careers",
        handoff_scopes=(("www.example-health.org", "directory", "/careers/"),),
    )

    in_scope = {item.url for item in result.deferred_targets} | {
        item.url for item in result.listings
    }
    assert not any("/info/" in item for item in in_scope)
    assert result.skip_reasons.get("off_host_link", 0) == 1


class _Pages:
    """Serve scripted pages with an optional content type; 404 for anything unscripted."""

    def __init__(self, pages: dict[str, str | tuple[str, str]]) -> None:
        self.pages = pages
        self.requested: list[str] = []

    def get(self, url: str, **_kwargs: Any) -> httpx.Response:
        self.requested.append(url)
        request = httpx.Request("GET", url)
        if url not in self.pages:
            return httpx.Response(404, request=request)
        page = self.pages[url]
        text, content_type = page if isinstance(page, tuple) else (page, "text/html")
        return httpx.Response(
            200, content=text.encode(), headers={"Content-Type": content_type}, request=request
        )


def _posting_page(title: str, url: str) -> str:
    posting = {"@type": "JobPosting", "title": title, "url": url}
    return f'<script type="application/ld+json">{json.dumps(posting)}</script>'


def test_a_sitemap_read_reports_every_posting_page_it_fetched_inline() -> None:
    """A fetched page that declares no posting is coverage the read saw, not a page it missed."""
    origin = "https://careers.globex.example"
    listed = f"{origin}/jobs/role-0"
    blank = f"{origin}/jobs/role-1"
    pages: dict[str, str | tuple[str, str]] = {
        f"{origin}/": "<html><body><h1>Careers</h1></body></html>",
        f"{origin}/robots.txt": f"Sitemap: {origin}/sitemap.xml",
        f"{origin}/sitemap.xml": sitemap_urlset(listed, blank),
        listed: _posting_page("Role 0", listed),
        blank: "<html><body><h1>Role 1</h1></body></html>",
    }
    fetcher = BoardApiFetcher(client=cast(Any, _Pages(pages)))

    read = fetcher.listings("Globex", f"{origin}/")

    assert [listing.url for listing in read.listings] == [listed]
    assert read.attempted_target_urls == (listed, blank)


def test_a_readable_collection_keeps_plain_details_when_its_sitemap_has_a_protected_job() -> None:
    origin = "https://careers.globex.example"
    root = f"{origin}/careers/"
    protected = f"{origin}/jobs/retired-engineer"
    eligible = f"{origin}/jobs/data-engineer"
    pages: dict[str, str | tuple[str, str]] = {
        root: (
            "<main><h1>Open positions</h1><ul>"
            f'<li><a href="{protected}">Software Engineer</a></li>'
            f'<li><a href="{eligible}">Data Engineer</a></li></ul></main>'
        ),
        f"{origin}/robots.txt": f"Sitemap: {origin}/sitemap.xml",
        f"{origin}/sitemap.xml": sitemap_urlset(protected, eligible),
        eligible: (
            "<main><h1>Data Engineer</h1><h2>Responsibilities</h2>"
            "<p>Build reliable data services and maintain ingestion pipelines for our teams. "
            "Collaborate with engineers to design, test and document production systems.</p>"
            "<h2>Qualifications</h2><p>Experience with data engineering and software delivery. "
            "Communicate clearly, investigate failures and support dependable releases.</p></main>"
        ),
    }
    server = _Pages(pages)
    read = BoardApiFetcher(client=cast(Any, server)).listings(
        "Globex", root, options=SourceReadOptions(protected_posting_urls=frozenset({protected}))
    )

    assert [(item.url, item.title) for item in read.listings] == [(eligible, "Data Engineer")]
    assert protected not in server.requested
    assert protected in read.protected_posting_urls
    assert read.complete is False


def test_section_collection_links_keep_their_context_when_deferred() -> None:
    root = "https://careers.globex.example/careers/"
    page = (
        "<section><h1>Open positions</h1>"
        '<a href="/careers/software-engineer">Software Engineer</a>'
        '<a href="/careers/data-engineer">Data Engineer</a></section>'
    )
    server = _Pages({root: page})
    fetcher = BoardApiFetcher(client=cast(Any, server))
    fetcher.publishers = PublisherSourceFetcher(
        client=cast(Any, server), limits=PublisherReadLimits(max_pages=1)
    )

    read = fetcher.listings("Globex", root)

    assert {target.url for target in read.deferred_targets} == {
        "https://careers.globex.example/careers/software-engineer",
        "https://careers.globex.example/careers/data-engineer",
    }
    assert all(target.structural_context == "section" for target in read.deferred_targets)
    assert read.complete is False


def test_child_sitemaps_past_the_expansion_bound_become_resumable_work() -> None:
    origin = "https://careers.globex.example"
    children = [f"{origin}/sitemap-{index}.xml" for index in range(7)]
    jobs = [f"{origin}/jobs/role-{index}" for index in range(7)]
    index_xml = (
        f'<?xml version="1.0"?><sitemapindex xmlns="{SITEMAP_NS}">'
        + "".join(f"<sitemap><loc>{child}</loc></sitemap>" for child in children)
        + "</sitemapindex>"
    )
    pages: dict[str, str | tuple[str, str]] = {
        f"{origin}/": "<html><body><h1>Careers</h1></body></html>",
        f"{origin}/robots.txt": f"Sitemap: {origin}/index.xml",
        f"{origin}/index.xml": index_xml,
    }
    for index, (child, job) in enumerate(zip(children, jobs, strict=True)):
        pages[child] = sitemap_urlset(job)
        pages[job] = _posting_page(f"Role {index}", job)
    fetcher = BoardApiFetcher(client=cast(Any, _Pages(pages)))

    read = fetcher.listings("Globex", f"{origin}/")

    assert [listing.url for listing in read.listings] == jobs[:5]
    assert read.discovery_truncated is True
    children_left = [
        target
        for target in read.deferred_targets
        if target.deferred_reason == "sitemap_child_pending"
    ]
    assert [target.url for target in children_left] == children[5:]
    assert all(target.automatic_eligible for target in children_left)

    discovered = fetcher.continue_listings(
        "Globex", tuple(children_left), root_url=f"{origin}/", handoff_scopes=()
    )

    assert [(target.url, target.deferred_reason) for target in discovered.deferred_targets] == [
        (jobs[5], "sitemap_posting_pending"),
        (jobs[6], "sitemap_posting_pending"),
    ]
    assert all(
        target.deferred_reason in STRUCTURED_PAGE_CURSOR_REASONS
        for target in discovered.deferred_targets
    )

    postings = fetcher.continue_listings(
        "Globex", discovered.deferred_targets, root_url=f"{origin}/", handoff_scopes=()
    )

    assert [listing.title for listing in postings.listings] == ["Role 5", "Role 6"]


def test_a_nested_child_sitemap_that_fails_once_is_requeued_not_dropped() -> None:
    origin = "https://careers.globex.example"
    parent = f"{origin}/sitemap-parent.xml"
    nested = [f"{origin}/sitemap-nested-{index}.xml" for index in range(2)]
    job = f"{origin}/jobs/role-1"
    failures = {nested[1]: 1}

    class _FlakyPages(_Pages):
        def get(self, url: str, **kwargs: Any) -> httpx.Response:
            if failures.get(url):
                failures[url] -= 1
                return httpx.Response(500, request=httpx.Request("GET", url))
            return super().get(url, **kwargs)

    pages: dict[str, str | tuple[str, str]] = {
        parent: (
            f'<?xml version="1.0"?><sitemapindex xmlns="{SITEMAP_NS}">'
            + "".join(f"<sitemap><loc>{child}</loc></sitemap>" for child in nested)
            + "</sitemapindex>"
        ),
        nested[0]: sitemap_urlset(f"{origin}/jobs/role-0"),
        nested[1]: sitemap_urlset(job),
    }
    fetcher = BoardApiFetcher(client=cast(Any, _FlakyPages(pages)))
    target = SourceContinuationTarget(
        url=parent,
        label="Unread child sitemap",
        label_source="none",
        source_url=f"{origin}/",
        structural_context="other",
        relations=("same-origin", "publisher-content"),
        deferred_reason="sitemap_child_pending",
    )

    first = fetcher.continue_listings("Globex", (target,), root_url=f"{origin}/", handoff_scopes=())

    assert [(item.url, item.deferred_reason) for item in first.deferred_targets] == [
        (f"{origin}/jobs/role-0", "sitemap_posting_pending"),
        (nested[1], "sitemap_child_pending"),
    ]

    retried = fetcher.continue_listings(
        "Globex", (first.deferred_targets[1],), root_url=f"{origin}/", handoff_scopes=()
    )

    assert [item.url for item in retried.deferred_targets] == [job]


@pytest.mark.parametrize("status", [404, 410])
def test_a_child_sitemap_the_publisher_removed_is_finished_not_requeued(status: int) -> None:
    origin = "https://careers.globex.example"
    parent, gone = f"{origin}/sitemap-parent.xml", f"{origin}/sitemap-gone.xml"
    live = f"{origin}/sitemap-live.xml"

    class _GonePages(_Pages):
        def get(self, url: str, **kwargs: Any) -> httpx.Response:
            if url == gone:
                self.requested.append(url)
                return httpx.Response(status, request=httpx.Request("GET", url))
            return super().get(url, **kwargs)

    pages: dict[str, str | tuple[str, str]] = {
        parent: (
            f'<?xml version="1.0"?><sitemapindex xmlns="{SITEMAP_NS}">'
            f"<sitemap><loc>{live}</loc></sitemap><sitemap><loc>{gone}</loc></sitemap>"
            "</sitemapindex>"
        ),
        live: sitemap_urlset(f"{origin}/jobs/role-0"),
    }
    fetcher = BoardApiFetcher(client=cast(Any, _GonePages(pages)))

    def target(url: str) -> SourceContinuationTarget:
        return SourceContinuationTarget(
            url=url,
            label="Unread child sitemap",
            label_source="none",
            source_url=f"{origin}/",
            structural_context="other",
            relations=("same-origin", "publisher-content"),
            deferred_reason="sitemap_child_pending",
        )

    nested = fetcher.continue_listings(
        "Globex", (target(parent),), root_url=f"{origin}/", handoff_scopes=()
    )
    alone = fetcher.continue_listings(
        "Globex", (target(gone),), root_url=f"{origin}/", handoff_scopes=()
    )

    assert [(item.url, item.deferred_reason) for item in nested.deferred_targets] == [
        (f"{origin}/jobs/role-0", "sitemap_posting_pending")
    ]
    assert nested.skip_reasons.get("sitemap_child_gone") == 1
    assert alone.deferred_targets == ()
    assert alone.skip_reasons.get("sitemap_child_gone") == 1


def test_a_url_read_as_a_sitemap_stays_deferred_when_a_sibling_lists_it_as_a_posting() -> None:
    origin = "https://careers.globex.example"
    both = f"{origin}/jobs/role-feed"
    sibling = f"{origin}/sitemap-sibling.xml"
    pages: dict[str, str | tuple[str, str]] = {
        both: sitemap_urlset(f"{origin}/jobs/role-0"),
        sibling: sitemap_urlset(both),
    }
    fetcher = BoardApiFetcher(client=cast(Any, _Pages(pages)))
    targets = tuple(
        SourceContinuationTarget(
            url=url,
            label="Unread child sitemap",
            label_source="none",
            source_url=f"{origin}/",
            structural_context="other",
            relations=("same-origin", "publisher-content"),
            deferred_reason="sitemap_child_pending",
        )
        for url in (both, sibling)
    )

    read = fetcher.continue_listings("Globex", targets, root_url=f"{origin}/", handoff_scopes=())

    assert (both, "sitemap_posting_pending") in [
        (item.url, item.deferred_reason) for item in read.deferred_targets
    ]


def test_sitemap_discovery_counts_what_it_skips_and_skips_hubs_and_documents() -> None:
    origin = "https://jobs.example"
    pages: dict[str, str | tuple[str, str]] = {
        f"{origin}/sitemap.xml": sitemap_urlset(
            f"{origin}/jobs/data-lead",
            f"{origin}/careers",
            f"{origin}/en/jobs/",
            f"{origin}/careers/files/benefits-guide.pdf",
            f"{origin}/about",
        )
    }
    client = _Pages(pages)

    def fetch(url: str) -> str | None:
        response = client.get(url)
        return response.text if response.status_code == 200 else None

    discovery = discover_job_urls(fetch, origin, limits=SitemapLimits())

    assert discovery.candidate_urls == (f"{origin}/jobs/data-lead",)
    assert discovery.non_posting_locs == 4


def test_a_document_served_in_place_of_a_posting_is_finished_not_retried() -> None:
    url = "https://jobs.example/careers/jobs/role-profile"
    target = SourceContinuationTarget(
        url=url,
        label="Role profile",
        label_source="visible",
        source_url="https://jobs.example/careers",
        structural_context="list",
        relations=("same-origin", "publisher-content"),
    )
    client = _Pages({url: ("%PDF-1.7 Role profile and benefits", "application/pdf")})

    read = PublisherSourceFetcher(client=cast(Any, client)).continue_listings(
        "Example Health", (target,), root_url="https://jobs.example/careers", handoff_scopes=()
    )

    assert read.listings == []
    assert read.deferred_targets == ()
    assert read.attempted_target_urls == (url,)
    assert read.skip_reasons == {"non_posting_page": 1}


def _public_resolver(_host: str, port: int) -> list[tuple[object, ...]]:
    return [(AF_INET, SOCK_STREAM, 6, "", ("93.184.216.34", port))]


def _polite(
    host: str,
    answers: list[httpx.Response],
    *,
    robots: str = "",
    sleeps: list[float],
) -> tuple[PoliteHttpClient, list[str]]:
    requested: list[str] = []

    def get(url: str, **_kwargs: object) -> httpx.Response:
        request = httpx.Request("GET", url)
        if url == f"https://{host}/robots.txt":
            return httpx.Response(200, text=robots, request=request)
        requested.append(url)
        answer = answers.pop(0)
        return httpx.Response(
            answer.status_code, headers=answer.headers, text=answer.text, request=request
        )

    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=sleeps.append,
        jitter=lambda _low, _high: 0.5,
        getter=get,
        resolver=_public_resolver,
        clock=lambda: 100.0,
    )
    return client, requested


def test_a_bounded_retry_after_is_waited_out_once_before_retrying() -> None:
    sleeps: list[float] = []
    client, requested = _polite(
        "retry.example",
        [httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(200, text="ok")],
        sleeps=sleeps,
    )

    response = client.get("https://retry.example/jobs")

    assert response.status_code == 200
    assert requested == ["https://retry.example/jobs", "https://retry.example/jobs"]
    assert sleeps == [0.5, 2.0]


def test_a_retry_after_date_beyond_the_bound_is_not_retried_and_holds_the_host() -> None:
    sleeps: list[float] = []
    later = format_datetime(datetime.now(UTC) + timedelta(minutes=5), usegmt=True)
    client, requested = _polite(
        "busy.example",
        [httpx.Response(503, headers={"Retry-After": later}), httpx.Response(200)],
        sleeps=sleeps,
    )

    assert client.get("https://busy.example/jobs").status_code == 503
    with pytest.raises(HostHeldError):
        client.get("https://busy.example/jobs/2", max_elapsed_seconds=5.0)

    assert requested == ["https://busy.example/jobs"]


def test_robots_crawl_delay_spaces_consecutive_requests_to_a_host() -> None:
    sleeps: list[float] = []
    client, requested = _polite(
        "paced.example",
        [httpx.Response(200), httpx.Response(200), httpx.Response(200)],
        robots="User-agent: *\nCrawl-delay: 5\n",
        sleeps=sleeps,
    )

    client.get("https://paced.example/jobs/1")
    client.get("https://paced.example/jobs/2")
    with pytest.raises(HostHeldError):
        client.get("https://paced.example/jobs/3", max_elapsed_seconds=1.0)

    assert sleeps == [0.5, 5.0]
    assert requested == ["https://paced.example/jobs/1", "https://paced.example/jobs/2"]


# Keep one address fixture per registered board shape.
BOARD_ADDRESSES = (
    ("greenhouse", 'src="https://boards.greenhouse.io/embed/job_board/js?for=acme"'),
    ("greenhouse", "https://boards.greenhouse.io/acme"),
    ("lever", "https://jobs.lever.co/acme"),
    ("workday", "https://acme.wd5.myworkdayjobs.com/en-US/External"),
    ("ashby", "https://jobs.ashbyhq.com/acme"),
    ("oracle", "https://acme.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1"),
    ("smartrecruiters", "https://careers.smartrecruiters.com/acme"),
    ("smartrecruiters", "https://api.smartrecruiters.com/v1/companies/acme/postings"),
    ("dayforce", "https://jobs.dayforcehcm.com/en-US/acme/ACMECANDIDATEPORTAL"),
    ("bamboohr", "https://acme.bamboohr.com/careers"),
    ("gem", "https://jobs.gem.com/acme"),
    ("getro", "https://acme.getro.com/jobs"),
    (
        "getro",
        '<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":'
        '{"network":{"id":"4242"},"initialState":{"jobs":{"found":[],"total":0}}}}}</script>',
    ),
    ("workable", "https://apply.workable.com/acme"),
    ("workable", "https://www.workable.com/api/accounts/732243"),
    ("recruitee", "https://acme.recruitee.com"),
    ("pinpoint", "https://acme.pinpointhq.com"),
    (
        "paylocity",
        "https://recruiting.paylocity.com/recruiting/v2/api/feed/jobs/"
        "d887949e-3f67-43b9-9f6b-3371b3f9450e",
    ),
    ("teamtailor", "https://acme.teamtailor.com"),
    ("teamtailor", '<a href="https://www.teamtailor.com/en?utm_source=career_site">'),
    ("personio", "https://acme.jobs.personio.com"),
    (
        "comeet",
        "https://www.comeet.co/careers-api/2.0/company/E5.007/positions"
        "?token=5E7236A0BCE5E7295111B55E70BCE",
    ),
    ("comeet", "https://www.comeet.com/jobs/acme/E5.007"),
    (
        "comeet",
        '<script>COMEET.init({"token": "5E72A0BCE5E7295", "company-uid": "E5.007"});</script>',
    ),
    ("rippling", "https://ats.rippling.com/acme/jobs"),
    (
        "dover",
        '<iframe src="https://app.dover.io/dover/careers/'
        '7c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f?embed=1"></iframe>',
    ),
    (
        "adp",
        "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
        "?cid=0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d&ccId=19000101_000001&type=MP&lang=en_CA",
    ),
    ("breezy", "https://acme.breezy.hr"),
    ("jazzhr", "https://acme.applytojob.com/apply"),
    ("jobvite", "https://jobs.jobvite.com/acme/jobs"),
    ("hibob", "https://acme.careers.hibob.com"),
    ("zoho", "https://acme.zohorecruit.com/jobs/Careers"),
    ("avature", "https://acme.avature.net/careers"),
    ("avature", '<meta name="avature.portal.urlPath" content="careers"/>'),
    ("phenom", '<script>phApp.ddo = {"refNum":"ACMEUS"};</script>'),
    ("icims", '<link href="https://cdn.jibecdn.com/prod/acme/latest/main.css">'),
    ("icims", "https://careers-acme.icims.com/jobs/search"),
    ("successfactors", '<link href="//rmkcdn.successfactors.com/482f99c6/site.css">'),
    ("himalayas", "https://himalayas.app/jobs"),
    ("arbeitnow", "https://www.arbeitnow.com/"),
    ("tbk", '<script>TBK_APP_CONTEXT = {"api_map":{}};</script>'),
    (
        "alignerr",
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{"initialJobs":[]}}}</script>',
    ),
    ("cornerstone", "https://acme.csod.com/ux/ats/careersite/4/home?c=acme"),
    (
        "jobsyn",
        '<script>window.__NUXT__.config={public:{"x-origin":"careers.acme.example"}};</script>',
    ),
    (
        "jobsyn",
        '<script>window.__NUXT__.config={public:{"api-url":"https://prod-search-api.jobsyn.org/api/"}};</script>',
    ),
    ("darwinbox", "https://acme.darwinbox.com/ms/candidatev2/main/careers/allJobs"),
    ("taleo", "https://acme.taleo.net/careersection/external/jobsearch.ftl?lang=en&portal=101"),
    ("fxrecruiter", "https://acme.fxrecruiter.com/jobs/"),
    ("ycombinator", "https://www.ycombinator.com/companies/acme/jobs"),
    ("eightfold", '<script>window._EF_GROUP_ID = "acme.example";</script>'),
    (
        "jpaginate",
        '<a href="/jobs/search/">All jobs</a><div class="job_list_row">'
        '<a href="/jobs/data-analyst-101">Data Analyst</a></div>'
        '<script>load("/job_list.js")</script>',
    ),
    ("trinethire", "https://app.trinethire.com/companies/12345-example-neuro/jobs"),
    (
        "clearcompany",
        '<script src="https://careers-content.clearcompany.com/js/v1/career-site.js'
        '?siteId=0f6c2a4e-8b1d-4e7a-9c3f-5d2b7a1e6c90"></script>',
    ),
    (
        "clearcompany",
        "https://careers-api.clearcompany.com/v1/0f6c2a4e-8b1d-4e7a-9c3f-5d2b7a1e6c90",
    ),
    ("taleobusiness", "https://acme.tbe.taleo.net/acme01/ats/careers/v2/jobSearch?org=ACME&cws=1"),
    (
        "peoplesoft",
        "https://careers.acme.example/psc/hrprd/EMPLOYEE/HRMS/c/HRS_HRAM_FL.HRS_CG_SEARCH_FL.GBL"
        "?Page=HRS_APP_SCHJOB_FL&Action=U&FOCUS=Applicant&SiteId=2",
    ),
    ("shopify", '<link rel="canonical" href="https://www.shopify.com/careers"/>'),
    ("deel", '<link rel="canonical" href="https://www.deel.com/careers/"/>'),
)


def test_every_recognized_board_kind_has_one_static_adapter() -> None:
    """Detection and dispatch must share one enumerable platform vocabulary."""
    detected = tuple(dict.fromkeys(platform for platform, _ in boards.BOARD_PATTERNS))
    registry = vars(board_adapters).get("BOARD_ADAPTER_REGISTRY")

    assert isinstance(registry, dict), "board dispatch is not an explicit registry"
    assert tuple(registry) == detected
    assert all(spec.name == platform for platform, spec in registry.items())
    assert all(callable(spec.adapter) for spec in registry.values())


def test_every_registered_board_url_is_identified_as_its_own_platform() -> None:
    """A shape the registry carries and the scan misreads is a board nobody reaches."""
    assert [platform for platform, _ in BOARD_ADDRESSES] == [
        platform for platform, _ in boards.BOARD_PATTERNS
    ], "a board shape was registered or removed without an address to read it by"

    for platform, address in BOARD_ADDRESSES:
        identified = board_adapters.identify_board(address)
        assert identified is not None, f"{platform}: {address} matched no pattern"
        assert identified[0] == platform, f"{address} was read as {identified[0]}, not {platform}"


@pytest.mark.parametrize(
    "address",
    [
        # UKG's robots.txt refuses the board search, so its board stays unrecognized.
        "https://recruiting2.ultipro.com/GLO1001GLOBX/JobBoard/0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d/",
        "https://acme.rec.pro.ukg.net/GLO1001GLOBX/JobBoard/0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d/",
        "https://recruiting.ultipro.ca/GLO1001GLOBX/JobBoard/0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d/"
        "OpportunityDetail?opportunityId=7c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f",
        "https://www.comeet.com/jobs/acme/E5.007/clinical-data-manager/87.405",
        '<a href="https://www.comeet.com/jobs/acme/E5.007/clinical-data-manager/87.405">',
    ],
    ids=["ukg-board", "ukg-pro-board", "ukg-posting", "comeet-posting", "comeet-posting-link"],
)
def test_an_address_that_names_no_readable_board_is_not_identified_as_one(address: str) -> None:
    assert identify_board(address) is None


def test_a_page_embedding_a_workable_account_by_number_names_that_board() -> None:
    page = (
        '<script src="https://www.workable.com/assets/embed.js"></script><script>'
        "whr(document).ready(() => { whr_embed(732243, { base: 'jobs', detail: 'titles' }); });"
        "</script>"
    )

    found = identify_board(page)

    assert found is not None
    assert (found[0], found[1].group("org")) == ("workable", "732243")


@pytest.mark.parametrize(
    "page",
    [
        "https://notworkable.com/api/accounts/732243",
        "<script>my_whr_embed(732243, {});</script>",
        '<script src="https://www.workable.com/assets/embed.js"></script>',
    ],
    ids=["lookalike-host", "other-function", "script-without-account"],
)
def test_a_page_naming_no_workable_account_is_not_a_workable_board(page: str) -> None:
    found = identify_board(page)

    assert found is None or found[0] != "workable"


def test_a_comeet_posting_page_is_a_job_url() -> None:
    posting = "https://www.comeet.com/jobs/acme/E5.007/clinical-data-manager/87.405"

    assert boards.single_employer_detail_url(posting) == posting


def test_every_board_list_is_read_from_the_one_platform_registry() -> None:
    """A kind named in any board list must be a registered platform, and every platform listed."""
    kinds = [platform.name for platform in PLATFORMS]
    registered = set(kinds)
    structured = board_adapters.STRUCTURED_PAGE_CURSOR_REASONS - {
        "sitemap_child_pending",
        "sitemap_posting_pending",
        REGISTERED_POSTING_LINK,
    }

    assert len(kinds) == len(registered), "a platform kind is registered twice"
    assert registered == set(get_args(BoardKind))
    assert all(platform.board_patterns and callable(platform.adapter) for platform in PLATFORMS)
    assert {kind for kind, _ in boards.BOARD_PATTERNS} == registered
    assert tuple(board_adapters.BOARD_ADAPTER_REGISTRY) == tuple(kinds)
    assert set(boards.POSTING_API_ORDER) <= registered
    assert structured == {reason for platform in PLATFORMS for reason in platform.continuations}
    location_providers = (EXHAUSTIVE_LOCATION_PROVIDERS, NON_EXHAUSTIVE_LOCATION_PROVIDERS)
    assert all(sum(kind in group for group in location_providers) == 1 for kind in registered)


def test_every_platform_module_is_registered() -> None:
    """A platform module left out of the registry would never be detected or read."""
    declared = {
        id(importlib.import_module(f"{platform_package.__name__}.{module.name}").PLATFORM)
        for module in pkgutil.iter_modules(platform_package.__path__)
    }
    assert declared == {id(platform) for platform in PLATFORMS}


def test_every_platform_with_a_posting_api_has_a_single_posting_reader() -> None:
    """A posting-API kind with no reader would fail every captured posting URL it names."""
    assert set(boards.POSTING_API_ORDER) <= set(posting_readers())


def test_the_platforms_that_publish_json_are_tried_first() -> None:
    """A page-only board matching first would cost the API read that follows it."""
    detected_order = tuple(dict.fromkeys(platform for platform, _ in boards.BOARD_PATTERNS))
    assert detected_order[: len(boards.POSTING_API_ORDER)] == boards.POSTING_API_ORDER


def test_board_api_reads_use_the_shared_politeness_policy() -> None:
    requested: list[str] = []
    pauses: list[float] = []

    def get(url: str, **kwargs: object) -> httpx.Response:
        requested.append(url)
        request = httpx.Request("GET", url)
        if url.endswith("/robots.txt"):
            return httpx.Response(404, request=request)
        return httpx.Response(
            200,
            request=request,
            json={
                "jobs": [
                    {
                        "title": "Platform Engineer",
                        "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                    }
                ]
            },
        )

    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=pauses.append,
        jitter=lambda low, high: 0.5,
        getter=get,
        resolver=lambda host, port: [(2, 1, 6, "", ("93.184.216.34", port))],
        clock=lambda: 100.0,
    )
    listings = published_listings(
        BoardApiFetcher(client=client), "Acme", "https://boards.greenhouse.io/acme"
    )

    assert listings is not None
    assert [listing.title for listing in listings] == ["Platform Engineer"]
    assert requested == [
        "https://boards-api.greenhouse.io/robots.txt",
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true",
    ]
    assert pauses == [0.5]


def test_dayforce_feed_is_never_requested_when_robots_refuses_it() -> None:
    requested: list[str] = []

    def get(url: str, **kwargs: object) -> httpx.Response:
        requested.append(url)
        request = httpx.Request("GET", url)
        if url.endswith("/robots.txt"):
            return httpx.Response(
                200,
                request=request,
                text="User-agent: *\nDisallow: /\n",
            )
        pytest.fail(f"robots-refused Dayforce target reached the network: {url}")

    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda _seconds: None,
        getter=get,
        resolver=lambda _host, port: [(2, 1, 6, "", ("93.184.216.34", port))],
    )

    with pytest.raises(RobotsDeniedError):
        BoardApiFetcher(client=client).listings(
            "Acme Health", "https://jobs.dayforcehcm.com/en-US/acme/ACMECANDIDATEPORTAL"
        )

    assert requested == ["https://www.dayforcehcm.com/robots.txt"]


def test_detected_board_policy_refusal_falls_back_to_the_readable_publisher_page() -> None:
    page = (
        '<script src="https://jobs.dayforcehcm.com/en-US/acme/ACMECANDIDATEPORTAL"></script>'
        + """
        <main><article><h1>Clinical Informatics Lead</h1><h2>Mandate</h2>
        <p>This role leads a substantial clinical information program across partner
        organizations and owns its delivery outcomes and stakeholder commitments.</p>
        <h2>Qualifications</h2><p>Candidates bring extensive leadership experience,
        sound judgement, and the ability to guide multidisciplinary teams.</p></article></main>
        """
    )

    class RefusedDetectedBoard(ScriptedPage):
        def _dispatch(self, *_args: Any, **_kwargs: Any) -> SourceRead:
            raise RobotsDeniedError("https://www.dayforcehcm.com/api/acme/v1/jobfeeds")

    result = RefusedDetectedBoard(page).listings(
        "Acme Health", "https://careers.acme.example/opportunities"
    )

    assert [listing.title for listing in result.listings] == ["Clinical Informatics Lead"]
    assert result.skip_reasons["board_api_policy_refused"] == 1
    assert "already-readable publisher page" in result.note


def test_a_source_url_that_names_a_board_is_read_without_fetching_a_page() -> None:
    """A board named in the address is identified without a page read."""
    fetcher = ScriptedPage()
    assert identify_board("https://boards.greenhouse.io/acme") is not None
    # Unknown hosts enter generic publisher recovery and are fetched once.
    assert identify_board("https://careers.example.com/openings") is None
    assert published_listings(fetcher, "Careers", "https://careers.example.com/openings") == []
    assert fetcher.fetched == ["https://careers.example.com/openings"]


def test_workday_boards_are_recognised_as_machine_readable() -> None:
    """Workday publishes openings as data, so its read has an explicit outcome."""
    assert identify_board("https://example.wd1.myworkdayjobs.com/Careers") is not None
    assert identify_board("https://example.wd103.myworkdayjobs.com/en-US/Careers") is not None

    # Vanity career pages remain manual reads even when backed by Workday.
    assert identify_board("https://careers.example.com/openings") is None


def test_oracle_recruiting_cloud_boards_are_recognised_as_machine_readable() -> None:
    """Oracle serves each tenant from its own host, so the site number is part of the address."""
    tenant = "https://acme.fa.us6.oraclecloud.com/hcmUI/CandidateExperience"
    assert identify_board(f"{tenant}/en/sites/CX_4") is not None
    assert identify_board(f"{tenant}/en-US/sites/CX_2") is not None

    # A Fusion host naming no career site is not a board address.
    assert identify_board("https://acme.fa.us6.oraclecloud.com/hcmUI/") is None


def test_a_board_with_no_known_api_enters_generic_recovery() -> None:
    """Unrecognized sources retain an explicit unresolved read instead of disappearing."""
    fetcher = ScriptedPage("<html><body>No board here.</body></html>")
    assert published_listings(fetcher, "Example", "https://www.linkedin.com/jobs") == []


def test_a_vanity_career_domain_is_read_through_the_board_behind_it() -> None:
    """The point of the page read: find the API, then use it.

    A career site on a custom domain usually fronts a board that does publish
    data. Only the identifier is taken from the page; the openings still come
    from the platform's API.
    """
    page = (
        '<html><body><div id="grnhse_app"></div>'
        '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acmecorp"></script>'
        "</body></html>"
    )

    class Detected(ScriptedPage):
        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            assert url == "https://boards-api.greenhouse.io/v1/boards/acmecorp/jobs?content=true"
            return {
                "jobs": [
                    {
                        "title": "Data Platform Lead",
                        "absolute_url": "https://boards.greenhouse.io/acmecorp/jobs/9",
                        "location": {"name": "Remote"},
                        "id": 9,
                    }
                ]
            }

    listings = published_listings(Detected(page), "Acme", "https://careers.acme.com/")
    assert listings is not None
    assert [item.title for item in listings] == ["Data Platform Lead"]


@pytest.mark.parametrize(
    ("marker", "platform", "org"),
    [
        ("https://jobs.lever.co/acme", "lever", "acme"),
        ("https://jobs.ashbyhq.com/acme", "ashby", "acme"),
        ("https://careers.smartrecruiters.com/AcmeCorp", "smartrecruiters", "AcmeCorp"),
        ("https://apply.workable.com/acme", "workable", "acme"),
        ("https://www.workable.com/api/accounts/732243", "workable", "732243"),
        ("https://acme.recruitee.com/o/lead", "recruitee", "acme"),
        ("https://jobs.dayforcehcm.com/en-US/acme/ACMECANDIDATEPORTAL", "dayforce", None),
        ("https://gamma.bamboohr.com/careers", "bamboohr", None),
        ("https://jobs.gem.com/globex/am9icG9zdDrChY8Z", "gem", None),
        ("https://healthtech.pinpointhq.com/postings/abc-123", "pinpoint", None),
        (
            "https://recruiting.paylocity.com/recruiting/v2/api/feed/jobs/"
            "d887949e-3f67-43b9-9f6b-3371b3f9450e",
            "paylocity",
            None,
        ),
        ("https://alpha.teamtailor.com/jobs/7100001-product-manager", "teamtailor", None),
        ("https://synthetic.jobs.personio.com/job/2100001", "personio", None),
        (
            "https://www.comeet.co/careers-api/2.0/company/E5.007/positions"
            "?token=5E7236A0BCE5E7295111B55E70BCE",
            "comeet",
            None,
        ),
        (
            "https://ats.rippling.com/globex/jobs/3f2a9c1e-5b7d-4e8a-9c0f-1a2b3c4d5e6f",
            "rippling",
            "globex",
        ),
        (
            "https://app.dover.io/dover/careers/7c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f?embed=1",
            "dover",
            None,
        ),
        (
            "https://app.dover.com/dover/careers/7c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f?embed=1",
            "dover",
            None,
        ),
        ("https://acme.wd1.myworkdayjobs.com/Careers", "workday", None),
        ("https://wd1.myworkdaysite.com/en-US/recruiting/acme/Careers", "workday", None),
        (
            "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
            "?lang=en_US&amp;cid=0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
            "adp",
            None,
        ),
        ("https://acme.breezy.hr/p/7a86407-data-analyst", "breezy", None),
    ],
)
def test_board_identifiers_are_recognised_wherever_they_appear(
    marker: str, platform: str, org: str | None
) -> None:
    """An explicitly entered ATS address remains recognizable."""
    found = identify_board_url(marker)
    assert found is not None
    assert found[0] == platform
    if org is not None:
        assert found[1].group("org") == org
    linked = identify_board(f'<a href="{marker}">Openings</a>')
    is_posting = platform in {
        "recruitee",
        "gem",
        "pinpoint",
        "teamtailor",
        "personio",
        "rippling",
        "breezy",
    }
    assert (linked is None) == is_posting


@pytest.mark.parametrize(
    "root",
    [
        "https://acme.wd1.myworkdayjobs.com/Careers",
        "https://wd1.myworkdaysite.com/en-US/recruiting/acme/Careers",
    ],
)
@pytest.mark.parametrize("attribute", ['"{}"', "'{}'", "{}", "{} "])
def test_workday_roots_at_html_attribute_boundaries_remain_scoped(
    root: str, attribute: str
) -> None:
    found = identify_board(f"<a href={attribute.format(root)}>Openings</a>")

    assert found is not None
    assert found[0] == "workday"
    assert identify_board_url(f"https://click.example/r?u={root}") is None


@pytest.mark.parametrize(
    "asset",
    [
        f"https://{host}/{path}"
        for host in ("jobs.gem.com", "jobs.lever.co", "jobs.eu.lever.co", "jobs.ashbyhq.com")
        for path in (
            "assets/app.js",
            "_next/static/chunk.js",
            "static/app.css",
            "api",
            "api/config",
        )
    ],
)
def test_a_hosted_board_site_asset_names_no_board(asset: str) -> None:
    assert identify_board(f'<script src="{asset}"></script>') is None


@pytest.mark.parametrize(
    "host", ["breezy.hr", "www.breezy.hr", "app.breezy.hr", "api.breezy.hr", "assets.breezy.hr"]
)
def test_a_breezy_service_host_names_no_tenant_board(host: str) -> None:
    assert identify_board(f'<script src="https://{host}/widget.js"></script>') is None


@pytest.mark.parametrize("host", ["app.applytojob.com", "api.applytojob.com", "API.applytojob.com"])
def test_a_jazzhr_service_host_names_no_tenant_board(host: str) -> None:
    assert identify_board(f'<script src="https://{host}/widget.js"></script>') is None
    assert identify_board_url(f"https://{host}/apply/Xq7Lm2Pz9K") is None


@pytest.mark.parametrize(
    "posting",
    ["initech.applytojob.com%2Fapply%2FXq7Lm2Pz9K", "initech.breezy.hr%2Fp%2Fabc123"],
    ids=["jazzhr", "breezy"],
)
@pytest.mark.parametrize("slash", ["%2F", "%2f"])
def test_a_url_encoded_share_link_never_names_a_tenant_from_its_escape(
    posting: str, slash: str
) -> None:
    link = f"https://social.example.com/share?u=https%3A{slash}{slash}{posting}"

    found = identify_board(f'<a href="{link}">Share</a>')

    assert found is None or found[1].group("tenant") == "initech"


@pytest.mark.parametrize(
    ("marker", "platform", "org"),
    [
        ("https://jobs.gem.com/acme", "gem", "acme"),
        ("https://jobs.lever.co/Northwind.io", "lever", "Northwind.io"),
        ("https://jobs.ashbyhq.com/Northwind.io", "ashby", "Northwind.io"),
    ],
)
def test_a_hosted_board_org_reads_up_to_its_closing_quote(
    marker: str, platform: str, org: str
) -> None:
    found = identify_board(f'<a href="{marker}">Openings</a>')
    assert found is not None
    assert (found[0], found[1].group("org")) == (platform, org)
