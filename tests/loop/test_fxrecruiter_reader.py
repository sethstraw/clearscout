"""Declared FX feeds supply identities while owned detail pages supply bodies."""

from __future__ import annotations

import datetime
from collections.abc import Callable
from html import escape
from urllib.parse import urlsplit

import httpx
import pytest

from clearscout.application.discovery.scan_sweep.listing_screen import (
    listed_title_screen,
    screen_listings,
)
from clearscout.application.discovery.source_reading import SourceListing, SourceReadOptions
from clearscout.domain.search_parameters import JobSearchParameters, Seniority
from clearscout.domain.staged_facts import PostedDateGrade
from clearscout.infrastructure.web_fetch.board_adapters import BoardApiFetcher
from clearscout.infrastructure.web_fetch.board_reads import PublisherSearchPage
from clearscout.infrastructure.web_fetch.ingest import strip_tags
from clearscout.infrastructure.web_fetch.ingest.careers import inspect_publisher_page
from clearscout.infrastructure.web_fetch.platforms import fxrecruiter
from clearscout.infrastructure.web_fetch.policy import PoliteHttpClient
from tests.support.doubles import declaring_page
from tests.support.http import public_resolver
from tests.support.scenarios import titles

_BODY = (
    "<h2>Responsibilities</h2><p>Review clinical records and maintain governed reporting. "
    "Document reproducible methods and present reviewed findings to research teams. </p>"
    "<h2>Qualifications</h2><p>Use structured data, analytical reasoning, and clear writing. "
    "Work with reviewers to verify source evidence and communicate practical conclusions.</p>"
)


class _FeedSite:
    def __init__(self, tenant: str = "acme", *, quota_interval: float = 0.0) -> None:
        self.origin = f"https://{tenant}.fxrecruiter.com"
        self.root = self.origin + "/jobs"
        self.requested: list[str] = []
        self.failed: set[int] = set()
        self.empty: set[int] = set()
        self.challenge: set[int] = set()
        self.redirects: dict[int, str] = {}
        self.valid_declaration = False
        self.foreign_feed = False
        self.foreign_detail = False
        self.stopped = False
        self.interrupt_after: int | None = None
        self.detail_reads = 0
        self.titles: dict[int, str] = {}
        self.now = 100.0
        self.quota_interval = quota_interval
        self.last_request_at: float | None = None
        self.request_starts: list[tuple[str, float]] = []
        self.fetcher = self.new_fetcher()

    def new_fetcher(self) -> BoardApiFetcher:
        """Build a fresh transport sharing this publisher's clock and responses."""
        site = self

        class Fetcher(BoardApiFetcher):
            @staticmethod
            def deadline_elapsed() -> bool:
                return site.stopped or BoardApiFetcher.deadline_elapsed()

        def get(url: str, **_kwargs: object) -> httpx.Response:
            if url.endswith("/robots.txt"):
                site.last_request_at = site.now
                site.request_starts.append((url, site.now))
                return httpx.Response(200, request=httpx.Request("GET", url))
            return site.answer(url)

        def sleep(seconds: float) -> None:
            site.now += seconds

        return Fetcher(
            client=PoliteHttpClient(
                user_agent="clearscout/test",
                getter=get,
                sleeper=sleep,
                jitter=lambda _low, _high: 0.5,
                clock=lambda: site.now,
                resolver=public_resolver,
            )
        )

    def url(self, number: int) -> str:
        return (
            f"{self.origin}/jobs/details/canada/ontario/data-analyst-{number}/{number}"
            "?utm_source=feed"
        )

    def title(self, number: int) -> str:
        return self.titles.get(number, f"Data Analyst {number}")

    def markup(self, number: int) -> str:
        if number in self.challenge:
            return '<main><h1>Sign in to continue</h1><input type="password"></main>'
        if self.valid_declaration:
            return declaring_page(
                {
                    "@type": "JobPosting",
                    "title": self.title(number),
                    "description": _BODY,
                    "hiringOrganization": {"name": "Acme"},
                }
            )
        return (
            f"<title>{self.title(number)} | Jobs from Acme</title>"
            f"<h1>{self.title(number)}</h1>"
            '<div hidden><input type="password"><h2>Account login</h2></div>'
            '<script type="application/ld+json">'
            '{"@type":"JobPosting",/* board comment */"title":"Untrusted title",'
            '"datePosted":"2026-10-01"}'
            "</script><main><h2>Related jobs</h2>"
            '<a href="/jobs/details/canada/ontario/manager/901">Related Manager</a>'
            '<a href="/jobs/details/canada/ontario/director/902">Related Director</a></main>'
            '<section class="job-description-bg">' + _BODY + '<a href="/apply">Apply</a></section>'
            '<footer><a href="https://other.example/jobs">Foreign Corporation</a></footer>'
        )

    def answer(self, url: str) -> httpx.Response:
        self.requested.append(url)
        self.request_starts.append((url, self.now))
        interval = (
            self.now - self.last_request_at if self.last_request_at is not None else float("inf")
        )
        self.last_request_at = self.now
        parsed = urlsplit(url)
        request = httpx.Request("GET", url)
        if parsed.netloc != urlsplit(self.origin).netloc:
            return httpx.Response(404, request=request)
        if parsed.path == "/jobs":
            feed = (
                "https://other.example" if self.foreign_feed else self.origin
            ) + "/feeds/jobs-rss"
            return httpx.Response(
                200,
                request=request,
                text=f'<link rel="alternate" type="application/rss+xml" href="{feed}">',
            )
        if parsed.path == "/feeds/jobs-rss":
            items = []
            for number in (1, 2, 3):
                address = self.url(number)
                if self.foreign_detail and number == 2:
                    address = address.replace(self.origin, "https://other.example")
                items.append(
                    f"<item><title>{escape(self.title(number))}</title>"
                    f"<link>{escape(address)}</link>"
                    f"<guid>{escape(address)}</guid>"
                    f"<description>Summary only: Ref {number + 500}</description>"
                    "</item>"
                )
            return httpx.Response(
                200,
                request=request,
                text='<rss version="2.0"><channel><title>Latest Jobs</title>'
                + "".join(items)
                + "</channel></rss>",
            )
        if parsed.path.startswith("/jobs/details/"):
            number = int(parsed.path.rsplit("/", 1)[-1])
            self.detail_reads += 1
            if self.interrupt_after == self.detail_reads:
                self.stopped = True
                self.interrupt_after = None
            if number in self.failed:
                return httpx.Response(404, request=request, text="Unavailable")
            if number in self.empty or interval + 1e-9 < self.quota_interval:
                return httpx.Response(200, request=request, headers={"Content-Length": "0"})
            if number in self.redirects:
                return httpx.Response(
                    302, request=request, headers={"Location": self.redirects[number]}
                )
            return httpx.Response(200, request=request, text=self.markup(number))
        return httpx.Response(404, request=request)


@pytest.mark.parametrize("tenant", ["acme", "globex"])
def test_declared_feed_reads_owned_bodies_with_hidden_login_and_invalid_jsonld(tenant: str) -> None:
    site = _FeedSite(tenant)

    read = site.fetcher.listings("Acme", site.root)

    assert read.complete is True
    assert [(row.title, row.requisition_id, row.url) for row in read.listings] == [
        (f"Data Analyst {number}", str(number), site.url(number)) for number in (1, 2, 3)
    ]
    assert all(strip_tags(_BODY) in row.published_description for row in read.listings)
    assert all("Related Manager" not in row.published_description for row in read.listings)
    assert {row.employer for row in read.listings} == {"Acme"}
    posted = [row.facts.posted_date for row in read.listings]
    assert {
        (date.day, date.earliest.basis, date.earliest.grade) if date else None for date in posted
    } == {(datetime.date(2026, 10, 1), "datePosted", PostedDateGrade.BOARD_DATE)}
    evidence = inspect_publisher_page(site.url(1), site.markup(1))
    assert "publisher structured data is not valid JSON" in evidence.evidence
    assert site.origin + "/feeds/jobs-rss" in site.requested
    assert not any(url.startswith("https://other.example") for url in site.requested)


def test_host_pacing_reads_every_body_from_a_publisher_with_a_request_quota() -> None:
    site = _FeedSite(quota_interval=2.2)

    read = site.fetcher.listings("Acme", site.root)

    assert {row.requisition_id for row in read.listings} == {"1", "2", "3"}
    assert all(strip_tags(_BODY) in row.published_description for row in read.listings)
    assert read.complete is True
    assert read.detail.failed == 0
    assert read.detail_read_urls == tuple(site.url(number) for number in (1, 2, 3))
    assert not read.deferred_targets
    assert all(
        later - earlier >= 2.2 - 1e-9
        for (_, earlier), (_, later) in zip(
            site.request_starts, site.request_starts[1:], strict=False
        )
    )


def test_empty_success_responses_retain_exact_failures_without_promoting_feed_summaries() -> None:
    site = _FeedSite(quota_interval=2.2)
    site.empty = {2}

    read = site.fetcher.listings("Acme", site.root)

    assert read.complete is False
    assert {row.requisition_id for row in read.listings} == {"1", "3"}
    assert read.detail.failed == 1
    assert site.url(2) in read.detail.failure_examples
    assert {target.url for target in read.deferred_targets} == {site.url(2)}
    assert all("Summary only" not in row.published_description for row in read.listings)
    assert site.requested.count(site.url(2)) == 1


def test_a_pacing_deadline_retains_successes_and_resumes_pending_details_on_a_fresh_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = _FeedSite(quota_interval=2.2)
    site.empty = {2}
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.board_adapters.transport.time.monotonic",
        lambda: site.now,
    )
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.board_adapters.read_scope.MAX_PAGE_READ_SECONDS", 10.0
    )

    first = site.fetcher.listings("Acme", site.root)

    assert first.complete is False
    assert {row.requisition_id for row in first.listings} == {"1"}
    assert first.detail.failed == 1
    assert {target.url for target in first.deferred_targets} == {site.url(2), site.url(3)}
    assert site.url(3) not in site.requested
    assert site.now == 110.0
    checkpoints = [
        target.detail_checkpoint for target in first.deferred_targets if target.detail_checkpoint
    ]
    assert {job_id for item in checkpoints for job_id in item.pending_ids} == {"2", "3"}
    assert {job_id for item in checkpoints for job_id, _url in item.completed} == set()
    site.empty.clear()
    site.fetcher.client.close()
    site.fetcher = site.new_fetcher()

    resumed = site.fetcher.continue_listings(
        "Acme", first.deferred_targets, root_url=site.root, handoff_scopes=()
    )

    assert {row.requisition_id for row in resumed.listings} == {"2", "3"}
    assert all(strip_tags(_BODY) in row.published_description for row in resumed.listings)
    assert not resumed.deferred_targets
    assert site.requested.count(site.url(1)) == 1
    assert site.requested.count(site.root) == 1
    assert site.requested.count(site.origin + "/feeds/jobs-rss") == 1


def test_paced_details_cannot_follow_a_redirect_to_a_protected_posting() -> None:
    site = _FeedSite(quota_interval=2.2)
    site.redirects = {1: site.url(2)}

    read = site.fetcher.listings(
        "Acme",
        site.root,
        options=SourceReadOptions(protected_posting_urls=frozenset({site.url(2)})),
    )

    assert site.url(2) not in site.requested
    assert site.url(2) in read.protected_posting_urls
    assert {row.requisition_id for row in read.listings} == {"3"}
    assert read.complete is False
    assert read.detail.failed == 1


def test_valid_posting_declarations_also_supply_full_bodies() -> None:
    site = _FeedSite()
    site.valid_declaration = True

    read = site.fetcher.listings("Acme", site.root)

    assert read.complete is True
    assert len(read.listings) == 3
    assert all(strip_tags(_BODY) in row.published_description for row in read.listings)


def test_failed_feed_details_retain_successes_and_resume_exact_stable_identity() -> None:
    site = _FeedSite()
    site.failed = {2}

    first = site.fetcher.listings("Acme", site.root)

    assert first.complete is False
    assert [row.requisition_id for row in first.listings] == ["1", "3"]
    assert first.detail.failed == 1
    assert site.url(2) in first.detail.failure_examples
    assert first.deferred_targets
    site.failed.clear()

    resumed = site.fetcher.continue_listings(
        "Acme", first.deferred_targets, root_url=site.root, handoff_scopes=()
    )

    assert [(row.requisition_id, row.url) for row in resumed.listings] == [("2", site.url(2))]
    assert not resumed.deferred_targets
    assert site.requested.count(site.url(1)) == 1


def test_interrupted_feed_details_resume_without_reenumerating_successes() -> None:
    site = _FeedSite()
    site.interrupt_after = 1

    first = site.fetcher.listings("Acme", site.root)

    assert first.complete is False
    assert [row.requisition_id for row in first.listings] == ["1"]
    assert first.deferred_targets
    site.stopped = False

    resumed = site.fetcher.continue_listings(
        "Acme", first.deferred_targets, root_url=site.root, handoff_scopes=()
    )

    assert {row.requisition_id for row in resumed.listings} == {"2", "3"}
    assert not resumed.deferred_targets
    assert site.requested.count(site.origin + "/feeds/jobs-rss") == 1


def test_protected_feed_details_are_covered_without_being_fetched() -> None:
    site = _FeedSite()

    read = site.fetcher.listings(
        "Acme",
        site.root,
        options=SourceReadOptions(protected_posting_urls=frozenset({site.url(2)})),
    )

    assert site.url(2) not in site.requested
    assert site.url(2) in read.protected_posting_urls
    assert {row.requisition_id for row in read.listings} == {"1", "3"}


def test_a_genuine_login_wall_never_promotes_the_feed_summary() -> None:
    site = _FeedSite()
    site.challenge = {2}

    read = site.fetcher.listings("Acme", site.root)

    assert read.complete is False
    assert {row.requisition_id for row in read.listings} == {"1", "3"}
    assert read.detail.failed == 1
    assert all("Summary only" not in row.published_description for row in read.listings)


_TARGETS = ["Data Analyst"]
_ANALYST_SEARCH = JobSearchParameters(seniority=Seniority(target_titles=titles(*_TARGETS)))


def _title_screened(protected: frozenset[str] = frozenset()) -> SourceReadOptions:
    screen = listed_title_screen(_ANALYST_SEARCH, _TARGETS, source_employer="Acme")
    return SourceReadOptions(protected_posting_urls=protected, listed_title_screen=screen)


def _title_buckets(listings: list[SourceListing]) -> tuple[list[str], int, list[str]]:
    wanted, refused, unsupported = screen_listings(
        listings, _ANALYST_SEARCH, _TARGETS, source_employer="Acme"
    )
    return sorted(item.url for item in wanted), len(refused), sorted(i.url for i, _ in unsupported)


def test_a_listed_title_the_search_rules_out_is_listed_without_a_detail_read() -> None:
    site, unscreened_site = _FeedSite(), _FeedSite()
    site.titles = unscreened_site.titles = {2: "Warehouse Forklift Operator"}

    read = site.fetcher.listings("Acme", site.root, options=_title_screened())
    unscreened = unscreened_site.fetcher.listings("Acme", unscreened_site.root)

    assert site.url(2) not in site.requested
    assert unscreened_site.url(2) in unscreened_site.requested
    assert read.complete is True
    assert not read.deferred_targets
    assert read.detail_read_urls == (site.url(1), site.url(3))
    [aside] = [row for row in read.listings if row.requisition_id == "2"]
    assert (aside.title, aside.url, aside.published_description) == (
        "Warehouse Forklift Operator",
        site.url(2),
        "",
    )
    assert _title_buckets(read.listings) == _title_buckets(unscreened.listings)
    assert _title_buckets(read.listings)[2] == [site.url(2)]


@pytest.mark.parametrize("title", ["Data Analyst 2", ""], ids=["on-target", "title-less"])
def test_a_listed_title_the_search_could_keep_is_still_detail_read(title: str) -> None:
    site = _FeedSite()
    site.titles = {2: title}

    site.fetcher.listings("Acme", site.root, options=_title_screened())

    assert site.requested.count(site.url(2)) == 1


def test_a_protected_posting_is_never_set_aside_by_its_title() -> None:
    site = _FeedSite()
    site.titles = {2: "Warehouse Forklift Operator"}
    read = site.fetcher.listings(
        "Acme", site.root, options=_title_screened(frozenset({site.url(2)}))
    )

    assert site.url(2) in read.protected_posting_urls
    assert {row.requisition_id for row in read.listings} == {"1", "3"}


def _row(item: str, url: str, title: str, body: str = "") -> dict[str, object]:
    """Build one FX search row; a ``body`` makes the row carry its own detail."""
    row: dict[str, object] = {"id": item, "url": url, "title": title}
    if body:
        row["detail"] = {"title": title, "description": body, "employer": "Acme"}
    return row


def _two_page_feed(
    monkeypatch: pytest.MonkeyPatch,
    pages: list[list[dict[str, object]]],
    after_first: Callable[[], None] | None = None,
) -> None:
    """Serve the FX search as consecutive pages, calling ``after_first`` on the first."""

    def request(_fetcher: object, _feed: str, _source: str, page: int) -> PublisherSearchPage:
        if page == 0 and after_first is not None:
            after_first()
        return PublisherSearchPage(pages[page], page + 1 if page + 1 < len(pages) else None)

    monkeypatch.setattr(fxrecruiter, "_request", request)


def test_an_identity_relisted_at_an_off_target_address_is_counted_with_both_listings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = _FeedSite()
    moved = site.origin + "/jobs/details/canada/ontario/forklift-operator/1"
    # A row carrying its body is read on its own page, before the relisting arrives.
    _two_page_feed(
        monkeypatch,
        [
            [_row("1", site.url(1), "Data Analyst 1", _BODY)],
            [
                _row("1", moved, "Warehouse Forklift Operator"),
                _row("4", site.url(4), "Data Analyst 4"),
            ],
        ],
    )

    read = site.fetcher.listings("Acme", site.root, options=_title_screened())

    assert moved not in site.requested
    assert sorted((row.url, bool(row.published_description)) for row in read.listings) == sorted(
        [(site.url(1), True), (moved, False), (site.url(4), True)]
    )
    assert read.raw_count >= len(read.listings)


@pytest.mark.parametrize("boundary", ["foreign_feed", "foreign_detail"])
def test_declared_feeds_and_detail_urls_remain_bound_to_the_publisher(boundary: str) -> None:
    site = _FeedSite()
    setattr(site, boundary, True)

    try:
        read = site.fetcher.listings("Acme", site.root)
    except ValueError:
        read = None

    assert read is None or read.complete is False
    assert not any(url.startswith("https://other.example") for url in site.requested)
