"""Sitemap job-URL discovery and the board reader's sitemap tier. No network."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, cast

import httpx
import pytest
from sqlalchemy import select

from tailorcv.application.discovery.board_scan import SourceScanService
from tailorcv.domain import SourceReadOutcome
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import SourceScan
from tailorcv.infrastructure.web_fetch.board_adapters import (
    SITEMAP_POSTING_FETCHES,
    BoardApiFetcher,
)
from tailorcv.infrastructure.web_fetch.ingest.sitemap import (
    SitemapLimits,
    discover_job_urls,
    is_document_url,
)
from tests.support.artifacts import runtime_storage
from tests.support.doubles import UnexpectedPostingFetcher
from tests.support.http import SITEMAP_NS, sitemap_rss, sitemap_urlset
from tests.support.scenarios import board_source, completed_earlier_scan, scan_workspace


class _Client:
    """Serve scripted pages and answer 404 for anything unscripted."""

    def __init__(self, pages: dict[str, str | Exception]) -> None:
        self.pages = pages
        self.requested: list[str] = []

    def get(self, url: str, **_kwargs: Any) -> httpx.Response:
        self.requested.append(url)
        if url not in self.pages:
            return httpx.Response(404, request=httpx.Request("GET", url))
        result = self.pages[url]
        if isinstance(result, Exception):
            raise result
        return httpx.Response(200, text=result, request=httpx.Request("GET", url))


def _fetch(client: _Client) -> Callable[[str], str | None]:
    """Adapt the fake client to the fetch callable discovery consumes."""

    def fetch(url: str) -> str | None:
        response = client.get(url)
        return response.text if response.status_code == 200 else None

    return fetch


def _index(*locs: str) -> str:
    entries = "".join(f"<sitemap><loc>{loc}</loc></sitemap>" for loc in locs)
    return f'<?xml version="1.0"?><sitemapindex xmlns="{SITEMAP_NS}">{entries}</sitemapindex>'


def _jsonld_page(title: str, url: str, employer: str = "Globex") -> str:
    posting = {
        "@type": "JobPosting",
        "title": title,
        "url": url,
        "hiringOrganization": {"name": employer},
        "jobLocation": {"address": {"addressLocality": "Remote"}},
    }
    return (
        '<html><body><script type="application/ld+json">'
        + json.dumps(posting)
        + "</script></body></html>"
    )


def test_robots_txt_declared_sitemap_is_used() -> None:
    origin = "https://globex.test"
    client = _Client(
        {
            f"{origin}/robots.txt": f"User-agent: *\nSitemap: {origin}/sm.xml",
            f"{origin}/sm.xml": sitemap_urlset(f"{origin}/jobs/a", f"{origin}/jobs/b"),
        }
    )

    discovery = discover_job_urls(_fetch(client), f"{origin}/careers", limits=SitemapLimits())

    assert discovery.candidate_urls == (f"{origin}/jobs/a", f"{origin}/jobs/b")
    assert discovery.truncated is False
    assert f"{origin}/robots.txt" in client.requested
    # The robots-declared sitemap wins, so the conventional path is never tried.
    assert f"{origin}/sitemap.xml" not in client.requested


def test_conventional_sitemap_is_tried_when_robots_declares_none() -> None:
    origin = "https://acme.test"
    client = _Client({f"{origin}/sitemap.xml": sitemap_urlset(f"{origin}/careers/lead")})

    discovery = discover_job_urls(_fetch(client), origin, limits=SitemapLimits())

    assert discovery.candidate_urls == (f"{origin}/careers/lead",)
    assert discovery.truncated is False
    assert f"{origin}/robots.txt" in client.requested
    assert f"{origin}/sitemap.xml" in client.requested


def test_sitemap_index_expands_child_sitemaps() -> None:
    origin = "https://globex.test"
    client = _Client(
        {
            f"{origin}/sitemap.xml": _index(f"{origin}/sm-1.xml", f"{origin}/sm-2.xml"),
            f"{origin}/sm-1.xml": sitemap_urlset(f"{origin}/jobs/1"),
            f"{origin}/sm-2.xml": sitemap_urlset(f"{origin}/jobs/2"),
        }
    )

    discovery = discover_job_urls(_fetch(client), origin, limits=SitemapLimits())

    assert set(discovery.candidate_urls) == {f"{origin}/jobs/1", f"{origin}/jobs/2"}
    assert discovery.truncated is False


def test_a_sitemap_served_as_an_rss_feed_lists_each_items_link() -> None:
    origin = "https://acme.test"
    feed = sitemap_rss(f"{origin}/careers/search", f"{origin}/jobs/1", f"{origin}/jobs/2")
    client = _Client({f"{origin}/sitemap.xml": feed})

    discovery = discover_job_urls(_fetch(client), origin, limits=SitemapLimits())

    # The channel's own link is the site, not a posting.
    assert discovery.candidate_urls == (f"{origin}/jobs/1", f"{origin}/jobs/2")
    assert discovery.truncated is False


@pytest.mark.parametrize("entity", ["&bull;", "&nbsp;", "&rsquo;"])
def test_an_rss_feed_carrying_html_named_entities_still_lists_its_links(entity: str) -> None:
    origin = "https://acme.test"
    feed = sitemap_rss(f"{origin}/careers", f"{origin}/careers/analyst").replace(
        "<title></title>", f"<title>Careers {entity} Acme</title>"
    )
    client = _Client({f"{origin}/sitemap.xml": feed})

    discovery = discover_job_urls(_fetch(client), origin, limits=SitemapLimits())

    assert discovery.candidate_urls == (f"{origin}/careers/analyst",)


def test_a_posting_path_naming_its_id_before_the_title_is_job_shaped() -> None:
    origin = "https://acme.test"
    client = _Client({f"{origin}/sitemap.xml": sitemap_urlset(f"{origin}/jobs/4471-data-analyst")})

    discovery = discover_job_urls(_fetch(client), origin, limits=SitemapLimits())

    assert discovery.candidate_urls == (f"{origin}/jobs/4471-data-analyst",)


def test_only_job_shaped_paths_are_returned() -> None:
    origin = "https://acme.test"
    client = _Client(
        {
            f"{origin}/sitemap.xml": sitemap_urlset(
                f"{origin}/jobs/data-lead",
                f"{origin}/careers/eng",
                f"{origin}/positions/42",
                f"{origin}/about",
                f"{origin}/blog/hiring",
                f"{origin}/team/careers-tips",
                f"{origin}/careers/category/intern-jobs-20",
                f"{origin}/careers/employment/students",
            )
        }
    )

    discovery = discover_job_urls(_fetch(client), origin, limits=SitemapLimits())

    assert discovery.candidate_urls == (
        f"{origin}/jobs/data-lead",
        f"{origin}/careers/eng",
        f"{origin}/positions/42",
        f"{origin}/careers/category/intern-jobs-20",
        f"{origin}/careers/employment/students",
    )
    assert discovery.truncated is False


def test_document_urls_are_recognized_by_extension_on_any_host() -> None:
    assert is_document_url("https://jobs.example/files/benefits-guide.pdf")
    assert is_document_url("https://jobs.example/careers/jobs/Role-Profile.DOCX")

    assert not is_document_url("https://jobs.example/careers/jobs/clinical-director")
    assert not is_document_url("https://jobs.example/careers/Page13096.aspx")


def test_candidate_url_cap_is_enforced() -> None:
    origin = "https://globex.test"
    locs = [f"{origin}/jobs/{n}" for n in range(50)]
    client = _Client({f"{origin}/sitemap.xml": sitemap_urlset(*locs)})

    discovery = discover_job_urls(
        _fetch(client), origin, limits=SitemapLimits(max_candidate_urls=10)
    )

    assert len(discovery.candidate_urls) == 10
    assert discovery.truncated is True


def test_a_sitemap_under_the_default_candidate_bound_is_not_truncated() -> None:
    """A 1,000-URL sitemap fits the default candidate bound whole, not just some prefix of it."""
    origin = "https://globex.test"
    locs = [f"{origin}/jobs/{n}" for n in range(1000)]
    client = _Client({f"{origin}/sitemap.xml": sitemap_urlset(*locs)})

    discovery = discover_job_urls(_fetch(client), origin, limits=SitemapLimits())

    assert len(discovery.candidate_urls) == 1000
    assert discovery.candidate_urls[0] == f"{origin}/jobs/0"
    assert discovery.candidate_urls[-1] == f"{origin}/jobs/999"
    assert discovery.truncated is False


def test_sitemap_fetch_cap_bounds_document_reads() -> None:
    origin = "https://acme.test"
    children = [f"{origin}/sm-{n}.xml" for n in range(8)]
    pages: dict[str, str | Exception] = {
        f"{origin}/robots.txt": f"Sitemap: {origin}/index.xml",
        f"{origin}/index.xml": _index(*children),
    }
    for n, child in enumerate(children):
        pages[child] = sitemap_urlset(f"{origin}/jobs/{n}")
    client = _Client(pages)

    discovery = discover_job_urls(
        _fetch(client),
        origin,
        limits=SitemapLimits(max_sitemap_fetches=3, max_child_sitemaps=8),
    )

    sitemap_reads = [url for url in client.requested if url.endswith(".xml")]
    assert len(sitemap_reads) == 3  # the index plus two of its children
    assert len(discovery.candidate_urls) == 2
    assert discovery.truncated is True


def test_child_sitemap_expansion_is_capped() -> None:
    origin = "https://globex.test"
    children = [f"{origin}/sm-{n}.xml" for n in range(6)]
    pages: dict[str, str | Exception] = {f"{origin}/sitemap.xml": _index(*children)}
    for n, child in enumerate(children):
        pages[child] = sitemap_urlset(f"{origin}/jobs/{n}")
    client = _Client(pages)

    discovery = discover_job_urls(
        _fetch(client), origin, limits=SitemapLimits(max_child_sitemaps=2)
    )

    assert len(discovery.candidate_urls) == 2
    assert discovery.truncated is True
    fetched_children = [url for url in client.requested if "/sm-" in url]
    assert len(fetched_children) == 2


def test_child_sitemap_cap_is_detected_even_when_the_capped_slot_is_a_duplicate() -> None:
    """A dropped child must be detected past the one loc that happens to sit at the cap."""
    origin = "https://globex.test"
    dup_child = f"{origin}/sm-dup.xml"
    new_child_1 = f"{origin}/sm-new-1.xml"
    new_child_2 = f"{origin}/sm-new-2.xml"
    client = _Client(
        {
            # dup_child is fetched first, as a robots-declared seed, so it is
            # already known by the time the index's own child list is read.
            f"{origin}/robots.txt": f"Sitemap: {dup_child}\nSitemap: {origin}/index.xml",
            dup_child: sitemap_urlset(f"{origin}/jobs/dup"),
            f"{origin}/index.xml": _index(new_child_1, dup_child, new_child_2),
            new_child_1: sitemap_urlset(f"{origin}/jobs/1"),
            new_child_2: sitemap_urlset(f"{origin}/jobs/2"),
        }
    )

    discovery = discover_job_urls(
        _fetch(client), origin, limits=SitemapLimits(max_child_sitemaps=1)
    )

    # new_child_2 was never fetched: the cap hit at dup_child, an already-seen
    # loc, which must not hide that a genuinely new child was dropped after it.
    assert new_child_2 not in client.requested
    assert discovery.truncated is True


def test_board_reader_reads_sitemap_discovered_postings() -> None:
    origin = "https://careers.globex.test"
    client = _Client(
        {
            f"{origin}/": "<html><body><h1>Careers</h1></body></html>",
            f"{origin}/robots.txt": f"Sitemap: {origin}/sitemap.xml",
            f"{origin}/sitemap.xml": sitemap_urlset(f"{origin}/jobs/data-lead", f"{origin}/about"),
            f"{origin}/jobs/data-lead": _jsonld_page("Data Lead", f"{origin}/jobs/data-lead"),
        }
    )

    read = BoardApiFetcher(client=cast(Any, client)).listings("Globex", f"{origin}/")

    assert read.outcome is SourceReadOutcome.PUBLISHER_INDEX
    assert not read.complete
    assert [(item.title, item.url) for item in read.listings] == [
        ("Data Lead", f"{origin}/jobs/data-lead")
    ]


def test_an_unadapted_icims_portal_reads_each_sitemap_posting_from_its_iframe_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "tailorcv.infrastructure.web_fetch.board_adapters.fetcher.SITEMAP_POSTING_FETCHES", 1
    )
    origin = "https://acme.icims.com"
    first, second = f"{origin}/jobs/1201/data-lead/job", f"{origin}/jobs/1202/analyst/job"
    shell = '<html><body><iframe src="?in_iframe=1"></iframe></body></html>'
    client = _Client(
        {
            f"{origin}/jobs/intro": "<html><body><h1>Careers</h1></body></html>",
            f"{origin}/sitemap.xml": sitemap_urlset(first, second),
            first: shell,
            second: shell,
            f"{origin}/jobs/1201/job?in_iframe=1": _jsonld_page("Data Lead", first, "Acme"),
            f"{origin}/jobs/1202/job?in_iframe=1": _jsonld_page("Analyst", second, "Acme"),
        }
    )
    fetcher = BoardApiFetcher(client=cast(Any, client))

    read = fetcher.listings("Acme", f"{origin}/jobs/intro")
    continued = fetcher.continue_listings(
        "Acme", read.deferred_targets, root_url=f"{origin}/jobs/intro", handoff_scopes=()
    )

    assert [(item.title, item.url) for item in read.listings] == [("Data Lead", first)]
    assert [(item.title, item.url) for item in continued.listings] == [("Analyst", second)]


def test_two_sitemap_addresses_for_one_icims_posting_fetch_its_iframe_body_once() -> None:
    origin = "https://acme.icims.com"
    body = f"{origin}/jobs/1201/job?in_iframe=1"
    client = _Client(
        {
            f"{origin}/jobs/intro": "<html><body><h1>Careers</h1></body></html>",
            f"{origin}/sitemap.xml": sitemap_urlset(
                f"{origin}/jobs/1201/data-lead/job", f"{origin}/jobs/1201/data-lead-remote/job"
            ),
            body: _jsonld_page("Data Lead", f"{origin}/jobs/1201/data-lead/job", "Acme"),
        }
    )

    read = BoardApiFetcher(client=cast(Any, client)).listings("Acme", f"{origin}/jobs/intro")

    assert [item.title for item in read.listings] == ["Data Lead"]
    assert client.requested.count(body) == 1


def test_a_humi_board_reads_the_postings_its_sitemap_lists() -> None:
    origin = "https://acme.applytojobs.ca"
    posting = f"{origin}/data%2Bplatform/43001"
    client = _Client(
        {
            f"{origin}/": "<html><body><h1>Acme Careers</h1></body></html>",
            f"{origin}/sitemap": sitemap_urlset(f"{origin}/", posting),
            posting: _jsonld_page("Data Platform Lead", posting, "Acme"),
        }
    )

    read = BoardApiFetcher(client=cast(Any, client)).listings("Acme", f"{origin}/")

    assert [(item.title, item.url) for item in read.listings] == [("Data Platform Lead", posting)]


def test_sitemap_postings_beyond_the_per_read_cap_enter_the_durable_frontier() -> None:
    origin = "https://careers.globex.test"
    urls = [f"{origin}/jobs/{index}" for index in range(SITEMAP_POSTING_FETCHES + 2)]
    pages: dict[str, str | Exception] = {
        f"{origin}/": "<html><body><h1>Careers</h1></body></html>",
        f"{origin}/robots.txt": f"Sitemap: {origin}/sitemap.xml",
        f"{origin}/sitemap.xml": sitemap_urlset(*urls),
    }
    pages.update({url: _jsonld_page(f"Role {index}", url) for index, url in enumerate(urls)})
    client = _Client(pages)
    fetcher = BoardApiFetcher(client=cast(Any, client))

    read = fetcher.listings("Globex", f"{origin}/")

    assert len(read.listings) == SITEMAP_POSTING_FETCHES
    assert read.outcome is SourceReadOutcome.CRAWL_CONTINUATION
    assert [target.url for target in read.deferred_targets] == urls[SITEMAP_POSTING_FETCHES:]
    # Discovery itself hit no bound here, so the note must not claim truncation.
    assert "stopped at a bound" not in read.note

    continued = fetcher.continue_listings(
        "Globex", read.deferred_targets, root_url=f"{origin}/", handoff_scopes=()
    )

    assert [listing.title for listing in continued.listings] == [
        f"Role {SITEMAP_POSTING_FETCHES}",
        f"Role {SITEMAP_POSTING_FETCHES + 1}",
    ]
    assert continued.deferred_targets == ()
    assert continued.attempted_target_urls == tuple(urls[SITEMAP_POSTING_FETCHES:])


def test_sitemap_discovery_beyond_the_candidate_bound_defers_all_and_notes_truncation() -> None:
    origin = "https://careers.globex.test"
    candidate_bound = SitemapLimits().max_candidate_urls
    urls = [f"{origin}/jobs/{index}" for index in range(candidate_bound + 1)]
    pages: dict[str, str | Exception] = {
        f"{origin}/": "<html><body><h1>Careers</h1></body></html>",
        f"{origin}/robots.txt": f"Sitemap: {origin}/sitemap.xml",
        f"{origin}/sitemap.xml": sitemap_urlset(*urls),
    }
    pages.update(
        {
            url: _jsonld_page(f"Role {index}", url)
            for index, url in enumerate(urls[:candidate_bound])
        }
    )
    client = _Client(pages)
    fetcher = BoardApiFetcher(client=cast(Any, client))

    read = fetcher.listings("Globex", f"{origin}/")

    assert read.raw_count == candidate_bound
    assert len(read.listings) == SITEMAP_POSTING_FETCHES
    assert len(read.deferred_targets) == candidate_bound - SITEMAP_POSTING_FETCHES
    assert [target.url for target in read.deferred_targets] == urls[
        SITEMAP_POSTING_FETCHES:candidate_bound
    ]
    assert "stopped at a bound" in read.note


def test_sitemap_read_uses_the_employer_declared_by_a_parent_board() -> None:
    origin = "https://parent.example"
    child_url = f"{origin}/careers/jobs/child-health-role"
    parent_url = f"{origin}/careers/jobs/parent-engineering-role"
    client = _Client(
        {
            f"{origin}/": "<html><body><h1>Careers</h1></body></html>",
            f"{origin}/robots.txt": f"Sitemap: {origin}/sitemap.xml",
            f"{origin}/sitemap.xml": sitemap_urlset(child_url, parent_url),
            child_url: _jsonld_page("Child Health Role", child_url, "Child Health"),
            parent_url: _jsonld_page("Parent Engineering Role", parent_url, "Parent Industries"),
        }
    )

    read = BoardApiFetcher(client=cast(Any, client)).listings("Child Health", f"{origin}/")

    assert [(listing.employer, listing.title) for listing in read.listings] == [
        ("Child Health", "Child Health Role"),
        ("Parent Industries", "Parent Engineering Role"),
    ]


def test_board_reader_falls_through_to_crawl_when_sitemap_is_empty() -> None:
    origin = "https://careers.acme.test"
    client = _Client(
        {
            f"{origin}/": '<div id="root"></div><script src="/app.js"></script>',
            f"{origin}/sitemap.xml": sitemap_urlset(f"{origin}/about", f"{origin}/team"),
        }
    )

    read = BoardApiFetcher(client=cast(Any, client)).listings("Acme", f"{origin}/")

    assert read.listings == []
    assert read.outcome is SourceReadOutcome.RENDER_REQUIRED


class _ScriptedSitemapClient:
    """Serve scripted pages for the sitemap tier; 404 for anything unscripted."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages

    def get(self, url: str, **_kwargs: Any) -> httpx.Response:
        if url not in self.pages:
            return httpx.Response(404, request=httpx.Request("GET", url))
        return httpx.Response(200, text=self.pages[url], request=httpx.Request("GET", url))


def test_a_scan_whose_sitemap_discovery_was_truncated_stays_incomplete(
    durable_database: Database,
) -> None:
    """A truncated sitemap discovery must never look like a fully drained frontier.

    Six child sitemaps exceed the default five-child cap, so one is never fetched
    and real, undiscovered work stays outstanding. Every URL this scan *did*
    discover was already completed by an earlier scan, but the scan must still
    read as incomplete because discovery itself stopped early.
    """
    workspace_id = scan_workspace(durable_database)
    origin = "https://careers.globex.test"
    source_id = board_source(durable_database, workspace_id, "Globex", f"{origin}/")
    ns = "http://www.sitemaps.org/schemas/sitemap/0.9"
    children = [f"{origin}/sm-{n}.xml" for n in range(6)]
    job_urls = tuple(f"{origin}/jobs/{n}" for n in range(5))
    pages = {
        f"{origin}/": "<html><body><h1>Careers</h1></body></html>",
        f"{origin}/robots.txt": f"Sitemap: {origin}/index.xml",
        f"{origin}/index.xml": (
            f'<?xml version="1.0"?><sitemapindex xmlns="{ns}">'
            + "".join(f"<sitemap><loc>{child}</loc></sitemap>" for child in children)
            + "</sitemapindex>"
        ),
    }
    for child, job_url in zip(children, job_urls, strict=False):
        pages[child] = (
            f'<?xml version="1.0"?><urlset xmlns="{ns}"><url><loc>{job_url}</loc></url></urlset>'
        )
    # No scripted content for the job postings themselves or the sixth child:
    # every discovered posting stays unread, and the sixth child is dropped.
    earlier_scan = completed_earlier_scan(durable_database, workspace_id, source_id, job_urls)

    fetcher = BoardApiFetcher(client=cast(Any, _ScriptedSitemapClient(pages)))
    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=fetcher,
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan).where(SourceScan.id != earlier_scan)).one()
        assert scan.status == "incomplete"
        assert scan.read_outcome == SourceReadOutcome.CRAWL_CONTINUATION.value
