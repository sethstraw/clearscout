"""Declared FX feeds supply identities while owned detail pages supply bodies."""

from __future__ import annotations

from html import escape
from urllib.parse import urlsplit

import httpx
import pytest

from tailorcv.application.discovery.source_reading import SourceReadOptions
from tailorcv.infrastructure.web_fetch.board_adapters import BoardApiFetcher
from tailorcv.infrastructure.web_fetch.ingest import strip_tags
from tailorcv.infrastructure.web_fetch.ingest.careers import inspect_publisher_page
from tailorcv.infrastructure.web_fetch.policy import PoliteHttpClient
from tests.support.doubles import declaring_page
from tests.support.http import public_resolver

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
                user_agent="tailorcv/test",
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

    def markup(self, number: int) -> str:
        if number in self.challenge:
            return '<main><h1>Sign in to continue</h1><input type="password"></main>'
        if self.valid_declaration:
            return declaring_page(
                {
                    "@type": "JobPosting",
                    "title": f"Data Analyst {number}",
                    "description": _BODY,
                    "hiringOrganization": {"name": "Acme"},
                }
            )
        return (
            f"<title>Data Analyst {number} | Jobs from Acme</title>"
            f"<h1>Data Analyst {number}</h1>"
            '<div hidden><input type="password"><h2>Account login</h2></div>'
            '<script type="application/ld+json">'
            '{"@type":"JobPosting",/* invalid JSON */"title":"Untrusted title"}'
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
                    f"<item><title>Data Analyst {number}</title><link>{escape(address)}</link>"
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
        "tailorcv.infrastructure.web_fetch.board_adapters.transport.time.monotonic",
        lambda: site.now,
    )
    monkeypatch.setattr(
        "tailorcv.infrastructure.web_fetch.board_adapters.read_scope.MAX_PAGE_READ_SECONDS", 10.0
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
    assert {job_id for item in checkpoints for job_id, _url in item.completed} == {"1"}
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
        options=SourceReadOptions(
            fresh_details=True, protected_posting_urls=frozenset({site.url(2)})
        ),
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
        options=SourceReadOptions(
            fresh_details=True,
            protected_posting_urls=frozenset({site.url(2)}),
        ),
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
