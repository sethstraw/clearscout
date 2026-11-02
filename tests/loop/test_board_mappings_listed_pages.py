"""Boards read as a published list joined to each posting's page.

These are Jobvite, Avature, SuccessFactors, and classic iCIMS. Also the single-request
boards whose list states everything a scan reads: HiBob and Zoho Recruit.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import replace
from datetime import date
from typing import Any

import httpx
import pytest

from tailorcv.application.discovery.source_reading import (
    SourceContinuationTarget,
    SourceDetailCheckpoint,
    SourceReadOptions,
)
from tailorcv.domain import SourceReadOutcome
from tailorcv.domain.identity import normalize_fetch_url
from tailorcv.domain.staged_facts import PostedDateGrade
from tailorcv.infrastructure.web_fetch import boards
from tailorcv.infrastructure.web_fetch.board_adapters import (
    BoardApiFetcher,
    identify_board,
    identify_board_url,
)
from tailorcv.infrastructure.web_fetch.board_mappings import avature_page_posting
from tailorcv.infrastructure.web_fetch.board_reads import (
    SITEMAP_CHILDREN,
    page_continuation_target,
    publisher_search_scope,
    scope_owned_by_root,
)
from tailorcv.infrastructure.web_fetch.board_reads.scope import _registered_publisher_source
from tailorcv.infrastructure.web_fetch.policy import (
    FetchPolicyError,
    HostHeldError,
    PoliteHttpClient,
)
from tailorcv.infrastructure.web_fetch.publisher_sources import (
    PublisherReadLimits,
    PublisherSourceFetcher,
)
from tests.support.doubles import declaring_page
from tests.support.http import policy_client, public_resolver, sitemap_rss, sitemap_urlset


def _fetcher(
    pages: dict[str, str],
    requested: list[str],
    *,
    robots: str = "",
    statuses: dict[str, int] | None = None,
) -> BoardApiFetcher:
    def answer(url: str) -> httpx.Response:
        requested.append(url)
        if url in (statuses or {}):
            return httpx.Response((statuses or {})[url], request=httpx.Request("GET", url))
        if url not in pages:
            return httpx.Response(404, request=httpx.Request("GET", url))
        return httpx.Response(200, request=httpx.Request("GET", url), text=pages[url])

    return BoardApiFetcher(client=policy_client(answer, robots=robots))


def _posting(title: str, *places: dict[str, str], **stated: Any) -> dict[str, Any]:
    return {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": title,
        "datePosted": "2026-09-20",
        "description": f"<p>Lead {title.lower()} work for clinical data teams.</p>",
        "jobLocation": [
            {"@type": "Place", "address": {"@type": "PostalAddress", **place}} for place in places
        ],
        **stated,
    }


# ---------------------------------------------------------------------------
# Jobvite
# ---------------------------------------------------------------------------

_JOBVITE_BOARD = "https://jobs.jobvite.com/acme/jobs"
_JOBVITE_ANALYST = "https://jobs.jobvite.com/acme/job/oAb12cDe"
_JOBVITE_ENGINEER = "https://jobs.jobvite.com/acme/job/oFg34hIj"


def _jobvite_row(job: str, title: str, location: str, *, layout: str = "table") -> str:
    if layout == "list":
        return (
            f'<li class="row"><a class="flex-row" href="/acme/job/{job}">'
            f'<div class="jv-job-list-name">{title}</div>'
            f'<div class="jv-job-list-location">{location}</div></a></li>'
        )
    return (
        f'<tr><td class="jv-job-list-name"><a href="/acme/job/{job}">{title}</a></td>'
        f'<td class="jv-job-list-location">{location}</td></tr>'
    )


_JOBVITE_TABLE = (
    "<html><body><h3>Data</h3><table class='jv-job-list'><thead><tr>"
    "<th class='jv-cws-sr-only'>Job listing</th><th>Job location</th></tr></thead><tbody>"
    + _jobvite_row("oAb12cDe", "Clinical Data Analyst", "\n Springfield,\n Ohio\n")
    + _jobvite_row("oFg34hIj", "Data Engineer", '<div class="jv-meta">\n 3 Locations\n</div>')
    + _jobvite_row("oAb12cDe", "Clinical Data Analyst", "Springfield, Ohio")
    + "</tbody></table>"
    + '<table class="jv-job-list"><tbody><tr><td class="jv-job-list-name">'
    '<a href="/initech/job/oZz99yXw">Partner role</a></td></tr></tbody></table>'
    "</body></html>"
)


_JOBVITE_LIST = (
    "<html><body><h3>Data</h3><div class='jv-job-list'><ul>"
    + _jobvite_row("oAb12cDe", "Clinical Data Analyst", "\n Springfield,\n Ohio\n", layout="list")
    + _jobvite_row(
        "oFg34hIj", "Data Engineer", '<div class="jv-meta">3 Locations</div>', layout="list"
    )
    + _jobvite_row("oAb12cDe", "Clinical Data Analyst", "Springfield, Ohio", layout="list")
    + _jobvite_row("oZz99yXw", "Partner role", "Shelbyville", layout="list").replace(
        "/acme/", "/initech/"
    )
    + "</ul></div></body></html>"
)


def _jobvite_pages(**overrides: str) -> dict[str, str]:
    return {
        _JOBVITE_BOARD: _JOBVITE_TABLE,
        _JOBVITE_ANALYST: declaring_page(
            _posting(
                "Clinical Data Analyst", {"addressCountry": "United States"}, identifier="oAb12cDe"
            )
        ),
        _JOBVITE_ENGINEER: declaring_page(
            _posting(
                "Data Engineer",
                {"addressLocality": "Springfield", "addressRegion": "OH", "addressCountry": "US"},
                {"addressLocality": "Shelbyville", "addressRegion": "OH", "addressCountry": "US"},
                hiringOrganization="Acme Health",
            )
        ),
        **overrides,
    }


@pytest.mark.parametrize(
    "address",
    [
        "https://jobs.jobvite.com/acme",
        "https://jobs.jobvite.com/acme/jobs",
        "https://jobs.jobvite.com/Acme/jobs?nl=0",
        "https://jobs.jobvite.com/acme/job/oAb12cDe",
        "https://jobs.jobvite.com/careers/acme/job/oAb12cDe",
    ],
)
def test_every_jobvite_address_form_names_one_board_root(address: str) -> None:
    identified = identify_board_url(address)

    assert identified is not None and identified[0] == "jobvite"
    assert boards.canonicalise_source_url(address) == _JOBVITE_BOARD


def test_a_jobvite_posting_page_is_a_single_employer_job_url() -> None:
    assert boards.single_employer_detail_url(_JOBVITE_ANALYST) == _JOBVITE_ANALYST


@pytest.mark.parametrize("board_page", [_JOBVITE_TABLE, _JOBVITE_LIST], ids=["table", "list"])
def test_jobvite_reads_each_listed_opening_once_with_its_page_posting(board_page: str) -> None:
    requested: list[str] = []
    fetcher = _fetcher(_jobvite_pages(**{_JOBVITE_BOARD: board_page}), requested)

    read = fetcher.listings("Acme", "https://jobs.jobvite.com/acme")

    assert requested == [_JOBVITE_BOARD, _JOBVITE_ANALYST, _JOBVITE_ENGINEER]
    assert read.complete is True
    assert read.raw_count == 2
    analyst, engineer = read.listings
    assert (analyst.title, analyst.url, analyst.requisition_id) == (
        "Clinical Data Analyst",
        _JOBVITE_ANALYST,
        "oAb12cDe",
    )
    assert analyst.location == "Springfield, Ohio"
    assert analyst.facts.locations.values == ("Springfield, Ohio",)
    assert analyst.facts.locations.complete is True
    assert (
        analyst.published_description == "Lead clinical data analyst work for clinical data teams."
    )
    assert engineer.facts.locations.values == (
        "Springfield, OH, United States",
        "Shelbyville, OH, United States",
    )
    assert engineer.employer == "Acme Health"
    posted = analyst.facts.posted_date
    assert posted is not None
    assert (posted.earliest.basis, posted.day) == ("datePosted", date(2026, 9, 20))
    assert posted.earliest.grade is PostedDateGrade.BOARD_DATE


@pytest.mark.parametrize("board_page", [_JOBVITE_TABLE, _JOBVITE_LIST], ids=["table", "list"])
@pytest.mark.parametrize("fresh_details", [False, True])
def test_a_protected_listed_posting_is_covered_without_a_detail_request(
    fresh_details: bool,
    board_page: str,
) -> None:
    requested: list[str] = []
    fetcher = _fetcher(_jobvite_pages(**{_JOBVITE_BOARD: board_page}), requested)

    read = fetcher.listings(
        "Acme",
        _JOBVITE_BOARD,
        options=SourceReadOptions(
            fresh_details=fresh_details,
            protected_posting_urls=frozenset({_JOBVITE_ANALYST}),
        ),
    )

    assert requested == [_JOBVITE_BOARD, _JOBVITE_ENGINEER]
    assert [listing.url for listing in read.listings] == [_JOBVITE_ENGINEER]
    assert read.complete is True
    assert read.raw_count == 2
    assert read.detail.attempted == read.detail.read == 1
    assert read.detail.failed == 0
    assert read.skip_reasons["protected_posting"] == 1
    assert read.protected_posting_urls == (_JOBVITE_ANALYST,)

    requested.clear()
    fetcher.listings("Acme", _JOBVITE_BOARD)
    assert requested == [_JOBVITE_BOARD, _JOBVITE_ANALYST, _JOBVITE_ENGINEER]


@pytest.mark.parametrize(
    "reason", ["jobvite_posting_pending", "sitemap_posting_pending", "frontier_deferred"]
)
def test_a_protected_continued_posting_finishes_without_a_request_or_retry(reason: str) -> None:
    requested: list[str] = []
    fetcher = _fetcher(_jobvite_pages(), requested)
    target = page_continuation_target(
        _JOBVITE_ANALYST, source_url=_JOBVITE_BOARD, label="Listed posting", reason=reason
    )

    read = fetcher.continue_listings(
        "Acme",
        (target,),
        root_url=_JOBVITE_BOARD,
        handoff_scopes=(),
        options=SourceReadOptions(protected_posting_urls=frozenset({_JOBVITE_ANALYST})),
    )

    assert requested == []
    assert read.listings == []
    assert read.deferred_targets == ()
    assert read.attempted_target_urls == ()
    assert read.protected_posting_urls == (_JOBVITE_ANALYST,)
    assert read.skip_reasons == {"protected_posting": 1}
    assert read.detail.attempted == read.detail.failed == 0


def test_a_protected_direct_publisher_source_makes_no_transport_request() -> None:
    requested: list[str] = []
    url = "https://careers.acme.example/jobs/analyst"
    fetcher = _fetcher({url: declaring_page(_posting("Clinical Data Analyst"))}, requested)

    read = fetcher.listings(
        "Acme", url, options=SourceReadOptions(protected_posting_urls=frozenset({url}))
    )

    assert requested == []
    assert read.listings == []
    assert read.protected_posting_urls == (url,)
    assert read.deferred_targets == ()


@pytest.mark.parametrize("protected_first", [True, False])
def test_a_generic_publisher_skips_a_protected_link_and_reads_the_other_posting(
    protected_first: bool,
) -> None:
    root = "https://careers.acme.example/jobs"
    protected = root + "/analyst"
    readable = root + "/engineer"
    requested: list[str] = []
    client = _fetcher(
        {
            protected: declaring_page(_posting("Clinical Data Analyst")),
            readable: declaring_page(_posting("Data Engineer")),
        },
        requested,
    ).client
    fetcher = PublisherSourceFetcher(client=client, limits=PublisherReadLimits(max_pages=1))
    links = [
        '<div class="role"><a href="/jobs/analyst">Clinical Data Analyst</a></div>',
        '<div class="role"><a href="/jobs/engineer">Data Engineer</a></div>',
    ]
    if not protected_first:
        links.reverse()

    read = fetcher.read(
        "Acme",
        root,
        "<main>" + "".join(links) + "</main>",
        options=SourceReadOptions(protected_posting_urls=frozenset({protected})),
    )

    assert requested == [readable]
    assert [listing.url for listing in read.listings] == [readable]
    assert read.skip_reasons["protected_posting"] == 1
    assert read.protected_posting_urls == (protected,)
    assert all(target.url != protected for target in read.deferred_targets)


def test_a_generic_sitemap_skips_a_protected_posting_page() -> None:
    root = "https://careers.acme.example/jobs"
    sitemap = "https://careers.acme.example/sitemap.xml"
    protected = root + "/analyst"
    readable = root + "/engineer"
    requested: list[str] = []
    fetcher = _fetcher(
        {
            root: "<html><body>Careers</body></html>",
            sitemap: sitemap_urlset(protected, readable),
            protected: declaring_page(_posting("Clinical Data Analyst")),
            readable: declaring_page(_posting("Data Engineer")),
        },
        requested,
        robots=f"User-agent: *\nAllow: /\nSitemap: {sitemap}\n",
    )

    read = fetcher.listings(
        "Acme",
        root,
        options=SourceReadOptions(protected_posting_urls=frozenset({protected})),
    )

    assert protected not in requested
    assert readable in requested
    assert [listing.url for listing in read.listings] == [readable]
    assert read.protected_posting_urls == (protected,)
    assert protected not in (read.attempted_target_urls or ())
    assert read.skip_reasons["protected_posting"] == 1


@pytest.mark.parametrize("reverse", [False, True])
def test_jobvite_deduplicates_mixed_owned_rows_and_ignores_navigation(reverse: bool) -> None:
    analyst = _jobvite_row("oAb12cDe", "Clinical Data Analyst", "Springfield, Ohio")
    engineer = _jobvite_row(
        "oFg34hIj", "Data Engineer", '<div class="jv-meta">3 Locations</div>', layout="list"
    )
    sections = [
        f'<table class="jv-job-list">{analyst}</table>',
        f'<div class="jv-job-list"><ul>{engineer}</ul></div>',
    ]
    if reverse:
        sections.reverse()
    navigation = _jobvite_row("oNo00Nav", "Navigation role", "Springfield", layout="list")
    foreign = _jobvite_row("oNo00Ext", "Foreign role", "Springfield", layout="list").replace(
        'href="/acme/', 'href="https://elsewhere.example/acme/'
    )
    board = (
        "".join(sections)
        + sections[0]
        + navigation
        + f'<div class="jv-job-list"><ul>{foreign}</ul></div>'
    )
    requested: list[str] = []

    read = _fetcher(_jobvite_pages(**{_JOBVITE_BOARD: board}), requested).listings(
        "Acme", _JOBVITE_BOARD
    )

    expected = (
        [_JOBVITE_ENGINEER, _JOBVITE_ANALYST] if reverse else [_JOBVITE_ANALYST, _JOBVITE_ENGINEER]
    )
    assert requested == [_JOBVITE_BOARD, *expected]
    assert [listing.url for listing in read.listings] == expected
    assert read.raw_count == 2
    assert read.complete is True


@pytest.mark.parametrize(
    "broken", ["no-container", "no-row", "no-anchor", "foreign-tenant", "truncated"]
)
def test_jobvite_refuses_unowned_or_incomplete_list_rows(broken: str) -> None:
    page = _JOBVITE_LIST
    if broken == "no-container":
        page = page.replace("jv-job-list'", "navigation'")
    elif broken == "no-row":
        page = page.replace('class="row"', 'class="navigation"')
    elif broken == "no-anchor":
        page = page.replace('class="flex-row"', 'class="navigation"')
    elif broken == "foreign-tenant":
        page = page.replace("/acme/job/", "/initech/job/")
    else:
        page = page.replace("</ul></div></body></html>", "")
    requested: list[str] = []

    read = _fetcher(_jobvite_pages(**{_JOBVITE_BOARD: page}), requested).listings(
        "Acme", _JOBVITE_BOARD
    )

    assert requested == [_JOBVITE_BOARD]
    assert read.listings == []
    assert read.complete is False
    assert read.outcome is SourceReadOutcome.NO_SUPPORTED_REPRESENTATION


def test_a_jobvite_page_without_its_table_reads_unreadable_not_empty() -> None:
    fetcher = _fetcher({_JOBVITE_BOARD: "<html><body><div id='app'></div></body></html>"}, [])

    read = fetcher.listings("Acme", _JOBVITE_BOARD)

    assert read.listings == []
    assert read.complete is False
    assert read.outcome is SourceReadOutcome.NO_SUPPORTED_REPRESENTATION
    assert read.note == "The Jobvite board page listed no openings."


@pytest.mark.parametrize("board_page", [_JOBVITE_TABLE, _JOBVITE_LIST], ids=["table", "list"])
@pytest.mark.parametrize("failed_page", ["<html><body>Apply</body></html>", None])
def test_a_jobvite_posting_page_without_posting_markup_is_a_failed_detail(
    board_page: str,
    failed_page: str | None,
) -> None:
    pages = _jobvite_pages(**{_JOBVITE_BOARD: board_page})
    if failed_page is None:
        del pages[_JOBVITE_ENGINEER]
    else:
        pages[_JOBVITE_ENGINEER] = failed_page
    fetcher = _fetcher(pages, [])

    read = fetcher.listings("Acme", _JOBVITE_BOARD)

    assert [listing.title for listing in read.listings] == ["Clinical Data Analyst"]
    assert read.complete is False
    assert (read.detail.attempted, read.detail.read, read.detail.failed) == (2, 1, 1)
    assert "1 of 2" in read.note


@pytest.mark.parametrize("separator", ["\u2028", "\r", "\x85", "\x0c"])
def test_a_line_separator_before_a_jobvite_table_reads_each_opening_once(separator: str) -> None:
    page = _JOBVITE_TABLE.replace("<h3>Data</h3>", f"<h3>Data{separator}teams{separator}</h3>")
    fetcher = _fetcher(_jobvite_pages(**{_JOBVITE_BOARD: page}), [])

    read = fetcher.listings("Acme", _JOBVITE_BOARD)

    assert [listing.requisition_id for listing in read.listings] == ["oAb12cDe", "oFg34hIj"]
    assert read.listings[0].location == "Springfield, Ohio"
    assert read.complete is True


@pytest.mark.parametrize("board_page", [_JOBVITE_TABLE, _JOBVITE_LIST], ids=["table", "list"])
@pytest.mark.parametrize(
    "href",
    [
        "/acme/jobs/alljobs",
        "https://jobs.jobvite.com/acme/jobs/alljobs",
        "/acme/jobs/category",
        "/acme/jobs/category?c=Data",
    ],
)
def test_a_jobvite_table_linking_more_openings_is_not_read_as_the_whole_board(
    href: str,
    board_page: str,
) -> None:
    paged = board_page.replace("</body>", f'<a href="{href}">More</a></body>')
    fetcher = _fetcher(_jobvite_pages(**{_JOBVITE_BOARD: paged}), [])

    read = fetcher.listings("Acme", _JOBVITE_BOARD)

    assert [listing.requisition_id for listing in read.listings] == ["oAb12cDe", "oFg34hIj"]
    assert read.complete is False
    assert "further pages" in read.note


_JOBVITE_SCIENTIST = "https://jobs.jobvite.com/acme/job/oKl56mNo"
_JOBVITE_SECOND_PAGE = (
    "<html><body><table class='jv-job-list'><tbody>"
    + _jobvite_row("oKl56mNo", "Data Scientist", "Shelbyville, Ohio")
    + "</tbody></table></body></html>"
)


def _paged_jobvite(next_href: str, **overrides: str) -> dict[str, str]:
    linked = _JOBVITE_TABLE.replace("</body>", f'<a href="{next_href}">Next</a></body>')
    return _jobvite_pages(
        **{
            _JOBVITE_BOARD: linked,
            f"{_JOBVITE_BOARD}?p=1": _JOBVITE_SECOND_PAGE,
            _JOBVITE_SCIENTIST: declaring_page(
                _posting("Data Scientist", {"addressCountry": "United States"})
            ),
            **overrides,
        }
    )


@pytest.mark.parametrize("next_href", ["/acme/jobs?p=1", "/acme/jobs?nl=0&amp;p=1"])
def test_a_paged_jobvite_table_reads_every_page_it_links(next_href: str) -> None:
    requested: list[str] = []
    fetcher = _fetcher(_paged_jobvite(next_href), requested)

    read = fetcher.listings("Acme", _JOBVITE_BOARD)

    assert requested[:2] == [_JOBVITE_BOARD, f"{_JOBVITE_BOARD}?p=1"]
    assert [listing.requisition_id for listing in read.listings] == [
        "oAb12cDe",
        "oFg34hIj",
        "oKl56mNo",
    ]
    assert read.complete is True


def test_a_jobvite_list_page_that_cannot_be_read_leaves_the_board_short() -> None:
    pages = _paged_jobvite("/acme/jobs?p=1")
    del pages[f"{_JOBVITE_BOARD}?p=1"]
    fetcher = _fetcher(pages, [])

    read = fetcher.listings("Acme", _JOBVITE_BOARD)

    assert [listing.requisition_id for listing in read.listings] == ["oAb12cDe", "oFg34hIj"]
    assert read.complete is False
    assert "list page 1 could not be read" in read.note


def test_a_jobvite_list_page_repeating_the_first_leaves_the_board_short() -> None:
    linked = _JOBVITE_TABLE.replace("</body>", '<a href="/acme/jobs?p=1">Next</a></body>')
    fetcher = _fetcher(_paged_jobvite("/acme/jobs?p=1", **{f"{_JOBVITE_BOARD}?p=1": linked}), [])

    read = fetcher.listings("Acme", _JOBVITE_BOARD)

    assert [listing.requisition_id for listing in read.listings] == ["oAb12cDe", "oFg34hIj"]
    assert read.complete is False
    assert "list page 1 listed no new openings" in read.note


@pytest.mark.parametrize(
    "href", ["/acme/jobs", "/acme/jobs?nl=0", "/acme/job/oAb12cDe", "/acme/jobs#top"]
)
def test_a_jobvite_link_back_to_its_own_table_or_a_posting_keeps_the_read_whole(
    href: str,
) -> None:
    linked = _JOBVITE_TABLE.replace("</body>", f'<a href="{href}">Jobs</a></body>')
    fetcher = _fetcher(_jobvite_pages(**{_JOBVITE_BOARD: linked}), [])

    read = fetcher.listings("Acme", _JOBVITE_BOARD)

    assert read.complete is True


# ---------------------------------------------------------------------------
# Classic iCIMS
# ---------------------------------------------------------------------------

_ICIMS_SITE = "https://careers-acme.icims.com"
_ICIMS_ROOT = f"{_ICIMS_SITE}/jobs/search"
_ICIMS_SITEMAP = f"{_ICIMS_SITE}/sitemap.xml"
_ICIMS_ANALYST = f"{_ICIMS_SITE}/jobs/7001/job?in_iframe=1"
_ICIMS_ENGINEER = f"{_ICIMS_SITE}/jobs/7002/job?in_iframe=1"
# The rule set a classic iCIMS site publishes, with its own sitemap declared.
_ICIMS_ROBOTS = (
    "User-agent: *\n"
    f"Sitemap: {_ICIMS_SITEMAP}\n"
    "Disallow: /jobs/*referral\nDisallow: /jobs/referral\n"
    "Disallow: /jobs/*login\nDisallow: /jobs/login\n"
    "Disallow: /jobs/*candidate\nDisallow: /jobs/candidate\n"
    "Disallow: /jobs/reminder\nDisallow: /connect\n"
)
_ICIMS_URLSET = sitemap_urlset(
    f"{_ICIMS_SITE}/jobs/intro",
    f"{_ICIMS_SITE}/jobs/7001/clinical-data-analyst/job",
    f"{_ICIMS_SITE}/jobs/7002/data-engineer-%28remote%29*/job",
    f"{_ICIMS_SITE}/jobs/7001/clinical-data-analyst/job",
    "https://careers-initech.icims.com/jobs/7003/partner-role/job",
)


def _icims_pages(**overrides: str) -> dict[str, str]:
    return {
        _ICIMS_SITEMAP: _ICIMS_URLSET,
        _ICIMS_ANALYST: declaring_page(
            _posting(
                "Clinical Data Analyst",
                {"addressLocality": "Springfield", "addressRegion": "OH", "addressCountry": "US"},
                hiringOrganization="Acme Health",
            )
        ),
        _ICIMS_ENGINEER: declaring_page(
            _posting("Data Engineer", {"addressCountry": "United States"})
        ),
        **overrides,
    }


@pytest.mark.parametrize(
    "address",
    [
        "https://careers-acme.icims.com",
        "https://careers-acme.icims.com/jobs/search?ss=1",
        "https://Careers-Acme.icims.com/jobs/intro",
        "https://careers-acme.icims.com/jobs/7001/clinical-data-analyst/job",
    ],
)
def test_every_classic_icims_address_form_names_one_board_root(address: str) -> None:
    identified = identify_board_url(address)

    assert identified is not None and identified[0] == "icims"
    assert boards.canonicalise_source_url(address) == _ICIMS_ROOT


def test_a_classic_icims_posting_page_is_a_single_employer_job_url() -> None:
    posting = f"{_ICIMS_SITE}/jobs/7001/clinical-data-analyst/job"

    assert boards.single_employer_detail_url(posting) == posting


@pytest.mark.parametrize(
    "address", ["https://cdn13.icims.com/assets/main.css", "https://careers.icims.com/jobs"]
)
def test_an_icims_host_outside_the_classic_form_is_not_a_classic_board(address: str) -> None:
    assert identify_board_url(address) is None


def test_classic_icims_reads_each_sitemap_posting_once_from_its_page() -> None:
    requested: list[str] = []
    fetcher = _fetcher(_icims_pages(), requested, robots=_ICIMS_ROBOTS)

    read = fetcher.listings("Acme", _ICIMS_ROOT)

    assert requested == [_ICIMS_SITEMAP, _ICIMS_ANALYST, _ICIMS_ENGINEER]
    assert read.complete is True
    assert read.raw_count == 2
    analyst, engineer = read.listings
    assert (analyst.title, analyst.url, analyst.requisition_id) == (
        "Clinical Data Analyst",
        _ICIMS_ANALYST,
        "7001",
    )
    assert analyst.employer == "Acme Health"
    assert analyst.facts.locations.values == ("Springfield, OH, United States",)
    assert (
        analyst.published_description == "Lead clinical data analyst work for clinical data teams."
    )
    assert (engineer.title, engineer.requisition_id) == ("Data Engineer", "7002")


def test_classic_icims_never_requests_a_posting_page_robots_disallows() -> None:
    requested: list[str] = []
    robots = _ICIMS_ROBOTS + "Disallow: /jobs/7002/\n"
    fetcher = _fetcher(_icims_pages(), requested, robots=robots)

    read = fetcher.listings("Acme", _ICIMS_ROOT)

    assert requested == [_ICIMS_SITEMAP, _ICIMS_ANALYST]
    assert [listing.requisition_id for listing in read.listings] == ["7001"]
    assert read.skip_reasons["robots_disallowed"] == 1
    assert read.complete is False
    assert read.deferred_targets == ()


def test_classic_icims_reads_the_postings_after_one_robots_disallows() -> None:
    requested: list[str] = []
    robots = _ICIMS_ROBOTS + "Disallow: /jobs/7001/\n"
    fetcher = _fetcher(_icims_pages(), requested, robots=robots)

    read = fetcher.listings("Acme", _ICIMS_ROOT)

    assert requested == [_ICIMS_SITEMAP, _ICIMS_ENGINEER]
    assert [(listing.title, listing.requisition_id) for listing in read.listings] == [
        ("Data Engineer", "7002")
    ]
    assert read.skip_reasons["robots_disallowed"] == 1
    assert read.deferred_targets == ()


def test_a_classic_icims_sitemap_listing_no_posting_reads_unreadable_not_empty() -> None:
    empty = sitemap_urlset(f"{_ICIMS_SITE}/jobs/intro")
    fetcher = _fetcher(_icims_pages(**{_ICIMS_SITEMAP: empty}), [], robots=_ICIMS_ROBOTS)

    read = fetcher.listings("Acme", _ICIMS_ROOT)

    assert read.listings == []
    assert read.complete is False
    assert read.note == "The iCIMS sitemap listed no posting page."


def test_a_continued_classic_icims_posting_reads_its_page() -> None:
    requested: list[str] = []
    fetcher = _fetcher(_icims_pages(), requested, robots=_ICIMS_ROBOTS)
    target = page_continuation_target(
        _ICIMS_ENGINEER,
        source_url=_ICIMS_ROOT,
        label="Listed posting",
        reason="icims_posting_pending",
    )

    read = fetcher.continue_listings("Acme", (target,), root_url=_ICIMS_ROOT, handoff_scopes=())

    assert requested == [_ICIMS_ENGINEER]
    assert [(listing.url, listing.requisition_id) for listing in read.listings] == [
        (_ICIMS_ENGINEER, "7002")
    ]


def test_a_continued_classic_icims_posting_robots_disallows_is_not_owed_again() -> None:
    requested: list[str] = []
    robots = _ICIMS_ROBOTS + "Disallow: /jobs/7002/\n"
    fetcher = _fetcher(_icims_pages(), requested, robots=robots)
    target = page_continuation_target(
        _ICIMS_ENGINEER,
        source_url=_ICIMS_ROOT,
        label="Listed posting",
        reason="icims_posting_pending",
    )

    read = fetcher.continue_listings("Acme", (target,), root_url=_ICIMS_ROOT, handoff_scopes=())

    assert requested == []
    assert read.listings == []
    assert read.skip_reasons["robots_disallowed"] == 1
    assert read.deferred_targets == ()


# ---------------------------------------------------------------------------
# HiBob
# ---------------------------------------------------------------------------

_HIBOB_FEED = "https://acme.careers.hibob.com/api/job-ad"


def _hibob_ad(**stated: Any) -> dict[str, Any]:
    return {
        "id": "c328a12c-1c6a-4d34-bfa7-ac322bc32c5d",
        "title": "Clinical Data Manager",
        "employmentType": "Regular Full Time",
        "site": "Springfield Office",
        "country": "United States",
        "description": "<p>Own the clinical data pipeline.</p>",
        "responsibilities": "<ul><li>Run data reviews.</li></ul>",
        "requirements": "<ul><li>Five years in clinical data.</li></ul>",
        "benefits": "<p>Health coverage.</p>",
        "workspaceTypeId": "hybrid",
        "workspaceType": "Hybrid",
        "payTransparencyMinSalary": "None",
        "payTransparencyMaxSalary": "None",
        "payTransparencySalaryCurrency": "None",
        "payTransparencySalaryPayPeriod": "None",
        **stated,
    }


def _hibob_fetcher(
    payload: Any, sent: list[tuple[str, dict[str, str]]], *, robots: str = ""
) -> BoardApiFetcher:
    def get(url: str, **kwargs: Any) -> httpx.Response:
        request = httpx.Request("GET", url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, request=request, text=robots)
        headers = {key.casefold(): value for key, value in (kwargs.get("headers") or {}).items()}
        sent.append((url, headers))
        if headers.get("companyidentifier") != "acme":
            return httpx.Response(401, request=request, json={"error": "unauthorized"})
        return httpx.Response(200, request=request, json=payload)

    client = PoliteHttpClient(
        user_agent="tailorcv/test",
        sleeper=lambda _seconds: None,
        jitter=lambda _low, _high: 0.0,
        getter=get,
        resolver=public_resolver,
    )
    return BoardApiFetcher(client=client)


@pytest.mark.parametrize(
    "address",
    [
        "https://acme.careers.hibob.com",
        "https://Acme.careers.hibob.com/",
        "https://acme.careers.hibob.com/jobs",
    ],
)
def test_every_hibob_address_form_names_one_board_root(address: str) -> None:
    identified = identify_board(address)

    assert identified is not None and identified[0] == "hibob"
    assert boards.canonicalise_source_url(address) == "https://acme.careers.hibob.com"


def test_hibob_reads_every_job_ad_naming_its_company_in_the_header() -> None:
    sent: list[tuple[str, dict[str, str]]] = []
    # The careers app answers its robots.txt path with the app shell, which states no rule.
    shell = "<!DOCTYPE html><html><body><careers-app-root></careers-app-root></body></html>"
    fetcher = _hibob_fetcher(
        {"filterGroups": [], "jobAdDetails": [_hibob_ad()]}, sent, robots=shell
    )

    read = fetcher.listings("Acme", "https://acme.careers.hibob.com")

    assert [url for url, _headers in sent] == [_HIBOB_FEED]
    assert sent[0][1]["companyidentifier"] == "acme"
    assert read.complete is True
    [listing] = read.listings
    assert listing.title == "Clinical Data Manager"
    assert listing.url == "https://acme.careers.hibob.com/jobs/c328a12c-1c6a-4d34-bfa7-ac322bc32c5d"
    assert listing.employer == "Acme"
    assert listing.facts.locations.values == ("Springfield Office", "United States")
    # The ad names one site, which says nothing of other places the role is open in.
    assert listing.facts.locations.complete is False
    assert listing.facts.arrangements.values == ("hybrid",)
    assert listing.facts.employment_types.values == ("Regular Full Time",)
    assert listing.facts.compensation.values == ()
    assert listing.published_description.split("\n\n") == [
        "Own the clinical data pipeline.",
        "- Run data reviews.",
        "- Five years in clinical data.",
        "Health coverage.",
    ]


@pytest.mark.parametrize(
    ("stated", "arrangements", "unresolved"),
    [
        pytest.param({"workspaceTypeId": "remote"}, ("remote",), (), id="remote"),
        pytest.param({"workspaceTypeId": "on_site"}, ("onsite",), (), id="on-site"),
        pytest.param({"workspaceTypeId": "flex"}, (), ("workspaceType: flex",), id="unknown"),
    ],
)
def test_a_hibob_arrangement_reads_the_stated_workspace_type(
    stated: dict[str, str], arrangements: tuple[str, ...], unresolved: tuple[str, ...]
) -> None:
    fetcher = _hibob_fetcher({"jobAdDetails": [_hibob_ad(**stated)]}, [])

    [listing] = fetcher.listings("Acme", "https://acme.careers.hibob.com").listings

    assert listing.facts.arrangements.values == arrangements
    assert listing.facts.arrangements.unresolved_values == unresolved


def test_a_hibob_pay_range_is_kept_only_when_floor_ceiling_and_period_are_stated() -> None:
    ad = _hibob_ad(
        payTransparencyMinSalary="90000",
        payTransparencyMaxSalary="110000",
        payTransparencySalaryCurrency="USD",
        payTransparencySalaryPayPeriod="Annual",
    )
    fetcher = _hibob_fetcher({"jobAdDetails": [ad]}, [])

    [listing] = fetcher.listings("Acme", "https://acme.careers.hibob.com").listings

    assert listing.facts.compensation.values == ("USD 90000-110000 per year",)


def test_a_hibob_answer_without_its_job_ad_list_refuses_the_board() -> None:
    fetcher = _hibob_fetcher({"error": "maintenance"}, [])

    with pytest.raises(ValueError, match="jobAdDetails"):
        fetcher.listings("Acme", "https://acme.careers.hibob.com")


# ---------------------------------------------------------------------------
# Zoho Recruit
# ---------------------------------------------------------------------------

_ZOHO_BOARD = "https://acme.zohorecruit.com/jobs/Careers"


def _zoho_page(openings: list[dict[str, Any]]) -> str:
    value = html.escape(json.dumps(openings), quote=True)
    return (
        '<html><body><input type="hidden" value="[{&#34;api_name&#34;:&#34;Job_Opening_Name&#34;}]"'
        ' id="moduleMeta">'
        f'<input type="hidden" value="{value}" id="jobs"></body></html>'
    )


def _zoho_opening(**stated: Any) -> dict[str, Any]:
    return {
        "Remote_Job": False,
        "Job_Type": "Permanent - Full time",
        "Job_Opening_Name": "Clinical Data Analyst",
        "Posting_Title": "Clinical Data Analyst",
        "Country": "Canada",
        "id": "589893000022148081",
        "City": "Springfield",
        "Publish": True,
        **stated,
    }


@pytest.mark.parametrize(
    ("address", "root"),
    [
        ("https://acme.zohorecruit.com", _ZOHO_BOARD),
        ("https://acme.zohorecruit.com/jobs/Careers", _ZOHO_BOARD),
        ("https://Acme.zohorecruit.com/jobs/Careers/589893000022148081", _ZOHO_BOARD),
        ("https://acme.zohorecruit.eu/jobs/Portal2", "https://acme.zohorecruit.eu/jobs/Portal2"),
    ],
)
def test_every_zoho_address_form_names_its_portal_root(address: str, root: str) -> None:
    identified = identify_board_url(address)

    assert identified is not None and identified[0] == "zoho"
    assert boards.canonicalise_source_url(address) == root


def test_zoho_reads_every_published_opening_the_careers_page_embeds() -> None:
    requested: list[str] = []
    page = _zoho_page(
        [
            _zoho_opening(),
            _zoho_opening(
                id="589893000022268148",
                Posting_Title="Data Engineer & Analyst",
                City="Shelbyville",
                State="Ontario",
                Remote_Job=True,
            ),
            _zoho_opening(id="589893000000000001", Publish=False),
        ]
    )
    fetcher = _fetcher({_ZOHO_BOARD: page}, requested)

    read = fetcher.listings("Acme", "https://acme.zohorecruit.com")

    assert requested == [_ZOHO_BOARD]
    assert read.complete is True
    analyst, engineer = read.listings
    assert analyst.url == f"{_ZOHO_BOARD}/589893000022148081"
    assert analyst.requisition_id == "589893000022148081"
    assert analyst.facts.locations.values == ("Springfield, Canada",)
    assert analyst.facts.locations.complete is False
    assert analyst.facts.arrangements.values == ()
    assert analyst.facts.employment_types.values == ("Permanent - Full time",)
    assert engineer.title == "Data Engineer & Analyst"
    assert engineer.facts.locations.values == ("Shelbyville, Ontario, Canada",)
    assert engineer.facts.arrangements.values == ("remote",)


def test_a_zoho_page_without_its_opening_list_reads_unreadable_not_empty() -> None:
    fetcher = _fetcher({_ZOHO_BOARD: "<html><body><div id='app'></div></body></html>"}, [])

    read = fetcher.listings("Acme", _ZOHO_BOARD)

    assert read.listings == []
    assert read.complete is False
    assert read.outcome is SourceReadOutcome.NO_SUPPORTED_REPRESENTATION
    assert read.note == "The Zoho Recruit careers page embedded no opening list."


def test_a_zoho_portal_stating_an_empty_list_is_an_empty_board() -> None:
    fetcher = _fetcher({_ZOHO_BOARD: _zoho_page([])}, [])

    read = fetcher.listings("Acme", _ZOHO_BOARD)

    assert read.listings == []
    assert read.complete is True


# ---------------------------------------------------------------------------
# Avature
# ---------------------------------------------------------------------------

_AVATURE = "https://acme.avature.net/careers"
_AVATURE_INDEX = f"{_AVATURE}/sitemap_index.xml"
_AVATURE_SITEMAP = f"{_AVATURE}/sitemap.xml"
_AVATURE_MANAGER = f"{_AVATURE}/JobDetail/Product-Manager/5703"
_AVATURE_ANALYST = f"{_AVATURE}/JobDetail/Data-Analyst/5804"
_AVATURE_ROBOTS = (
    "User-agent: *\nAllow: /careers\nDisallow: /careers/*qtvc=\n"
    f"Sitemap: {_AVATURE_INDEX}\nDisallow: /\n"
)


def _sitemap_index(*children: str) -> str:
    entries = "".join(f"<sitemap><loc>{child}</loc></sitemap>" for child in children)
    return (
        '<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{entries}</sitemapindex>"
    )


def _avature_field(label: str, value: str) -> str:
    labelled = f'<div class="article__content__view__field__label">{label}</div>' if label else ""
    return (
        f'<div class="article__content__view__field ">{labelled}'
        f'<div class="article__content__view__field__value">{value}</div></div>'
    )


def _avature_page(title: str, *, fields: str) -> str:
    declared = json.dumps(
        {"@context": "https://schema.org/", "@type": "JobPosting", "title": title}
    )
    return (
        f"<html><body><article class='article article--details'><div class='article__content'>"
        f"<div class='article__content__view'>{fields}</div></div></article>"
        f'<script type="application/ld+json">{declared}</script></body></html>'
    )


_AVATURE_MANAGER_FIELDS = (
    _avature_field("Location", "Remote")
    + _avature_field("Full-time/Part-time", "Full Time")
    + _avature_field("Country", "United States")
    + _avature_field("Job ID", "5703")
)
_AVATURE_BODY = _avature_field(
    "", "<p>Own the claims product roadmap.</p><div><p>Work with clinicians.</p></div>"
) + _avature_field("", "<ul><li>Ten years in product.</li></ul>")


@pytest.mark.parametrize(
    ("address", "root"),
    [
        ("https://acme.avature.net/careers", _AVATURE),
        ("https://acme.avature.net/careers/SearchJobs", _AVATURE),
        (_AVATURE_MANAGER, _AVATURE),
        (
            "https://acme.avature.net/en_US/careers/SearchJobs",
            "https://acme.avature.net/en_US/careers",
        ),
    ],
)
def test_every_avature_address_form_names_its_portal_root(address: str, root: str) -> None:
    identified = identify_board_url(address)

    assert identified is not None and identified[0] == "avature"
    assert boards.canonicalise_source_url(address) == root


def test_avature_reads_the_sitemap_listed_postings_and_each_page_fields() -> None:
    requested: list[str] = []
    pages = {
        _AVATURE_INDEX: _sitemap_index(_AVATURE_SITEMAP),
        _AVATURE_SITEMAP: sitemap_urlset(
            f"{_AVATURE}/SearchJobs", f"{_AVATURE}/JobDetail", _AVATURE_MANAGER, _AVATURE_ANALYST
        ),
        _AVATURE_MANAGER: _avature_page(
            "Product Manager", fields=_AVATURE_MANAGER_FIELDS + _AVATURE_BODY
        ),
        _AVATURE_ANALYST: _avature_page(
            "Data Analyst",
            fields=_avature_field("Location", "Springfield, OH") + _AVATURE_BODY,
        ),
    }
    fetcher = _fetcher(pages, requested, robots=_AVATURE_ROBOTS)

    read = fetcher.listings("Acme", "https://acme.avature.net/careers/SearchJobs")

    assert requested == [_AVATURE_INDEX, _AVATURE_SITEMAP, _AVATURE_MANAGER, _AVATURE_ANALYST]
    assert read.complete is True
    manager, analyst = read.listings
    assert (manager.title, manager.url, manager.requisition_id) == (
        "Product Manager",
        _AVATURE_MANAGER,
        "5703",
    )
    assert manager.facts.locations.values == ("Remote", "United States")
    assert manager.facts.employment_types.values == ("Full Time",)
    assert manager.published_description.split("\n\n") == [
        "Own the claims product roadmap.",
        "Work with clinicians.",
        "- Ten years in product.",
    ]
    assert analyst.facts.locations.values == ("Springfield, OH",)


@pytest.mark.parametrize("separator", ["\u2028", "\r"])
def test_a_line_separator_before_avature_fields_reads_each_field_once(separator: str) -> None:
    fields = "\n".join(
        (
            _avature_field("Location", "Remote"),
            _avature_field("Full-time/Part-time", "Full Time"),
            _avature_field("Country", "United States"),
            _avature_field("", "<p>Own the claims product roadmap.</p>"),
        )
    )
    page = _avature_page("Product Manager", fields=fields).replace(
        "<body>", f"<body><p>Acme{separator}careers{separator}</p>\n"
    )
    pages = {
        _AVATURE_INDEX: _sitemap_index(_AVATURE_SITEMAP),
        _AVATURE_SITEMAP: sitemap_urlset(_AVATURE_MANAGER),
        _AVATURE_MANAGER: page,
    }
    fetcher = _fetcher(pages, [], robots=_AVATURE_ROBOTS)

    [manager] = fetcher.listings("Acme", _AVATURE).listings

    assert manager.facts.locations.values == ("Remote", "United States")
    assert manager.facts.employment_types.values == ("Full Time",)
    assert manager.published_description == "Own the claims product roadmap."


@pytest.mark.parametrize(
    "asset",
    [
        "https://acme.avature.net/portal/12/js/main.js",
        "https://acme.avature.net/careers/images/logo.png",
    ],
)
def test_a_page_loading_an_avature_asset_names_no_avature_board(asset: str) -> None:
    page = f'<html><head><script src="{asset}"></script></head><body>Careers</body></html>'

    identified = identify_board(page)

    assert identified is None


_OWN_HOST = "https://careers.acme.example"
_OWN_PORTAL = f"{_OWN_HOST}/en_US/careers"
_OWN_INDEX = f"{_OWN_PORTAL}/sitemap_index.xml"
_OWN_SITEMAP = f"{_OWN_PORTAL}/sitemap.xml"
_OWN_FRENCH_SITEMAP = f"{_OWN_HOST}/fr_CA/careers/sitemap.xml"
_OWN_MANAGER = f"{_OWN_PORTAL}/JobDetail/Product-Manager/5703"
_OWN_ANALYST = f"{_OWN_PORTAL}/JobDetail/Data-Analyst/5804"
_OWN_ROBOTS = "User-agent: *\nAllow: /careers\nAllow: /*/careers\nDisallow: /\n"


def _portal_page(*, page: str = "SearchJobs") -> str:
    return (
        '<html><head><meta name="avature.portal.id" content="4"/>'
        '<meta name="avature.portal.urlPath" content="careers"/>'
        '<meta name="avature.portal.lang" content="en_US"/>'
        f'<meta name="avature.portal.page" content="{page}"/></head>'
        "<body><h1>Acme</h1>Search jobs</body></html>"
    )


def _own_places(*places: tuple[str, str, str]) -> str:
    groups = "".join(
        '<ul class="MultipleDataSetFields">'
        + "".join(
            f'<li class="MultipleDataSetField"><span class="MultipleDataSetFieldLabel">{label}:'
            f'</span> <span class="MultipleDataSetFieldValue">{value}</span></li>'
            for label, value in zip(("Location", "State", "Country"), place, strict=True)
        )
        + "</ul><br/>"
        for place in places
    )
    return (
        '<div class="article__content__view__field field--locations">'
        '<div class="article__content__view__field__value">'
        f"<strong>Locations</strong>: {', '.join(places[0])}&nbsp; {groups}"
        '<ul class="MultipleDataSetFields"></ul></div></div>'
    )


def _own_page(title: str, *, fields: str) -> str:
    """A JobDetail page that declares no JobPosting, as portals on an employer domain serve."""
    return (
        f'<html><head><meta property="og:title" content="{title}" /></head><body><h1>Acme</h1>'
        "<article class='article article--details'><div class='article__content'>"
        f"<div class='article__content__view'>{fields}</div></div></article></body></html>"
    )


_OWN_MANAGER_PAGE = _own_page(
    "Product Manager",
    fields=_own_places(
        ("Springfield", "Ohio", "United States"), ("Shelbyville", "Ohio", "United States")
    )
    + _avature_field("Worker Type", "Regular Employee")
    + _avature_field("Job Type", "Full Time")
    + _AVATURE_BODY,
)


def test_a_portal_on_the_employer_domain_is_named_by_its_page_markup() -> None:
    identified = identify_board(_portal_page())

    assert identified is not None and identified[0] == "avature"
    assert identify_board_url(f"{_OWN_PORTAL}/SearchJobs") is None


def test_a_portal_on_the_employer_domain_reads_its_own_locale_pages_and_fields() -> None:
    requested: list[str] = []
    pages = {
        f"{_OWN_PORTAL}/SearchJobs": _portal_page(),
        _OWN_INDEX: _sitemap_index(_OWN_SITEMAP, _OWN_FRENCH_SITEMAP),
        _OWN_SITEMAP: sitemap_urlset(f"{_OWN_PORTAL}/SearchJobs", _OWN_MANAGER, _OWN_ANALYST),
        _OWN_FRENCH_SITEMAP: sitemap_urlset(_OWN_MANAGER.replace("/en_US/", "/fr_CA/")),
        _OWN_MANAGER: _OWN_MANAGER_PAGE,
        _OWN_ANALYST: _own_page(
            "Data Analyst",
            fields=_avature_field("Location", "Springfield, OH") + _AVATURE_BODY,
        ),
    }
    fetcher = _fetcher(pages, requested, robots=_OWN_ROBOTS)

    read = fetcher.listings("Acme", f"{_OWN_PORTAL}/SearchJobs")

    assert requested == [
        f"{_OWN_PORTAL}/SearchJobs",
        _OWN_INDEX,
        _OWN_SITEMAP,
        _OWN_MANAGER,
        _OWN_ANALYST,
    ]
    assert read.complete is True
    manager, analyst = read.listings
    assert (manager.title, manager.url, manager.requisition_id) == (
        "Product Manager",
        _OWN_MANAGER,
        "5703",
    )
    assert manager.facts.locations.values == (
        "Springfield, Ohio, United States",
        "Shelbyville, Ohio, United States",
    )
    assert manager.facts.employment_types.values == ("Full Time",)
    assert manager.published_description.split("\n\n") == [
        "Own the claims product roadmap.",
        "Work with clinicians.",
        "- Ten years in product.",
    ]
    assert (analyst.title, analyst.facts.locations.values) == ("Data Analyst", ("Springfield, OH",))


def test_a_portal_named_without_a_locale_keeps_each_posting_once_across_locales() -> None:
    root = f"{_OWN_HOST}/careers"
    french = _OWN_MANAGER.replace("/en_US/", "/fr_CA/")
    pages = {
        root: _portal_page(),
        f"{root}/sitemap_index.xml": _sitemap_index(_OWN_FRENCH_SITEMAP, _OWN_SITEMAP),
        _OWN_SITEMAP: sitemap_urlset(_OWN_MANAGER),
        _OWN_FRENCH_SITEMAP: sitemap_urlset(french, f"{_OWN_HOST}/fr_CA/other/JobDetail/X/77"),
        _OWN_MANAGER: _OWN_MANAGER_PAGE,
    }
    requested: list[str] = []

    read = _fetcher(pages, requested, robots=_OWN_ROBOTS).listings("Acme", root)

    assert [(listing.url, listing.requisition_id) for listing in read.listings] == [
        (_OWN_MANAGER, "5703")
    ]
    assert _OWN_FRENCH_SITEMAP not in requested
    assert french not in requested


def test_a_portal_named_without_a_locale_reads_the_first_listed_when_english_is_absent() -> None:
    root = f"{_OWN_HOST}/careers"
    german_sitemap = f"{_OWN_HOST}/de_DE/careers/sitemap.xml"
    french = _OWN_MANAGER.replace("/en_US/", "/fr_CA/")
    pages = {
        root: _portal_page(),
        f"{root}/sitemap_index.xml": _sitemap_index(_OWN_FRENCH_SITEMAP, german_sitemap),
        _OWN_FRENCH_SITEMAP: sitemap_urlset(french),
        french: _OWN_MANAGER_PAGE,
    }
    requested: list[str] = []

    read = _fetcher(pages, requested, robots=_OWN_ROBOTS).listings("Acme", root)

    assert [listing.url for listing in read.listings] == [french]
    assert german_sitemap not in requested


def test_one_sitemap_listing_a_posting_in_two_locales_keeps_the_english_page() -> None:
    root = f"{_OWN_HOST}/careers"
    french = _OWN_MANAGER.replace("/en_US/", "/fr_CA/")
    pages = {
        root: _portal_page(),
        f"{root}/sitemap_index.xml": sitemap_urlset(french, _OWN_MANAGER),
        _OWN_MANAGER: _OWN_MANAGER_PAGE,
    }
    requested: list[str] = []

    read = _fetcher(pages, requested, robots=_OWN_ROBOTS).listings("Acme", root)

    assert [listing.url for listing in read.listings] == [_OWN_MANAGER]
    assert french not in requested


def test_an_avature_net_index_naming_the_employer_domain_reads_postings_there() -> None:
    pages = {
        "https://acme.avature.net/en_US/careers/sitemap_index.xml": _sitemap_index(_OWN_SITEMAP),
        _OWN_SITEMAP: sitemap_urlset(_OWN_MANAGER),
        _OWN_MANAGER: _OWN_MANAGER_PAGE,
    }
    fetcher = _fetcher(pages, [], robots=_OWN_ROBOTS)

    read = fetcher.listings("Acme", "https://acme.avature.net/en_US/careers")

    assert [listing.url for listing in read.listings] == [_OWN_MANAGER]


class _DetailClock(BoardApiFetcher):
    """Serve fixed pages; each JobDetail read spends the whole board deadline."""

    def __init__(
        self, pages: dict[str, str], now: list[float], *, failing: frozenset[str] = frozenset()
    ) -> None:
        super().__init__(client=policy_client(self._answer, robots=_OWN_ROBOTS))
        self.pages = pages
        self.now = now
        self.failing = failing

    def _answer(self, url: str) -> httpx.Response:
        if "/JobDetail/" in url:
            self.now[0] += 60.0
        if url in self.failing:
            return httpx.Response(500, request=httpx.Request("GET", url))
        if url not in self.pages:
            return httpx.Response(404, request=httpx.Request("GET", url))
        return httpx.Response(200, request=httpx.Request("GET", url), text=self.pages[url])


def test_employer_domain_portal_continuations_stay_scoped_and_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr(
        "tailorcv.infrastructure.web_fetch.board_adapters.transport.time.monotonic", lambda: now[0]
    )
    later = f"{_OWN_PORTAL}/sitemap-2.xml"
    pages = {
        f"{_OWN_PORTAL}/SearchJobs": _portal_page(),
        _OWN_INDEX: _sitemap_index(_OWN_SITEMAP, later),
        _OWN_SITEMAP: sitemap_urlset(_OWN_MANAGER, _OWN_ANALYST),
        _OWN_MANAGER: _OWN_MANAGER_PAGE,
    }

    read = _DetailClock(pages, now, failing=frozenset({later})).listings(
        "Acme", f"{_OWN_PORTAL}/SearchJobs"
    )

    assert [listing.url for listing in read.listings] == [_OWN_MANAGER]
    deferred = {target.deferred_reason: target for target in read.deferred_targets}
    assert {(reason, target.url) for reason, target in deferred.items()} == {
        ("avature_posting_pending", _OWN_ANALYST),
        ("avature_sitemap_pending", later),
    }
    for target in deferred.values():
        assert target.source_url == f"{_OWN_PORTAL}/SearchJobs"
        scope = publisher_search_scope(target)
        assert scope is not None and scope_owned_by_root(scope[1], f"{_OWN_PORTAL}/SearchJobs")

    resumed = _fetcher(
        {
            _OWN_ANALYST: _own_page("Data Analyst", fields=_AVATURE_BODY),
            later: sitemap_urlset(f"{_OWN_PORTAL}/JobDetail/Data-Engineer/5905"),
        },
        [],
        robots=_OWN_ROBOTS,
    ).continue_listings(
        "Acme", tuple(deferred.values()), root_url=f"{_OWN_PORTAL}/SearchJobs", handoff_scopes=()
    )

    assert [listing.url for listing in resumed.listings] == [_OWN_ANALYST]
    assert [target.url for target in resumed.deferred_targets] == [
        f"{_OWN_PORTAL}/JobDetail/Data-Engineer/5905"
    ]


@pytest.mark.parametrize("reason", ["avature_posting_pending", "avature_sitemap_pending"])
def test_an_employer_domain_source_is_avature_only_when_a_read_declared_it(reason: str) -> None:
    assert _registered_publisher_source("avature", reason, _AVATURE)
    assert _registered_publisher_source("avature", reason, _OWN_PORTAL, declared="avature")
    assert not _registered_publisher_source("avature", reason, _OWN_PORTAL)
    assert not _registered_publisher_source("avature", reason, f"{_OWN_HOST}/news")


def test_a_paragraph_template_avature_page_reads_its_fields_and_rich_text_body() -> None:
    def paragraph(label: str, value: str) -> str:
        return (
            f'<p class="paragraph "><span data-map="item-title"><strong>{label}:</strong></span>'
            f'<span data-map="item-value">{value}</span></p>'
        )

    page = _own_page("Store Educator", fields="").replace(
        "</article>",
        "</article><article class='article article--details'>"
        + paragraph("Location", "Canada")
        + paragraph("City", "Springfield")
        + paragraph("Time Type", "Part-time")
        + "</article><div class='article__content--rich-text' itemprop='description'>"
        "<p>Educate guests on the product line.</p></div>",
    )
    pages = {
        _OWN_INDEX: _sitemap_index(_OWN_SITEMAP),
        _OWN_SITEMAP: sitemap_urlset(_OWN_MANAGER),
        _OWN_MANAGER: page,
        f"{_OWN_PORTAL}/SearchJobs": _portal_page(),
    }

    [educator] = (
        _fetcher(pages, [], robots=_OWN_ROBOTS).listings("Acme", f"{_OWN_PORTAL}/SearchJobs")
    ).listings

    assert educator.title == "Store Educator"
    assert educator.facts.locations.values == ("Canada", "Springfield")
    assert educator.facts.employment_types.values == ("Part-time",)
    assert educator.published_description == "Educate guests on the product line."


def test_an_avature_page_with_neither_jobposting_nor_og_title_states_no_posting() -> None:
    page = _own_page("", fields=_AVATURE_BODY)

    assert avature_page_posting(page) is None


class _ClockedPages(BoardApiFetcher):
    """Serve fixed pages and feeds; the clock passes the read deadline after the first page."""

    def __init__(self, pages: dict[str, str], now: list[float]) -> None:
        self.pages = pages
        self.now = now
        self.requested: list[str] = []

    def get_text(self, url: str, *, max_bytes: int = 0) -> str:
        self.requested.append(url)
        self.now[0] += 60.0
        return self.pages[url]

    def get_feed(self, url: str) -> str:
        self.requested.append(url)
        return self.pages[url]


@pytest.mark.parametrize("board_page", [_JOBVITE_TABLE, _JOBVITE_LIST], ids=["table", "list"])
@pytest.mark.parametrize("cut_at", ["before-first", "between-details", "during-a-detail"])
def test_jobvite_counts_every_distinct_owned_row_when_details_hit_the_deadline(
    monkeypatch: pytest.MonkeyPatch, board_page: str, cut_at: str
) -> None:
    now = [100.0]
    monkeypatch.setattr(
        "tailorcv.infrastructure.web_fetch.board_adapters.transport.time.monotonic", lambda: now[0]
    )
    pages = _jobvite_pages(**{_JOBVITE_BOARD: board_page})
    requested: list[str] = []

    class DeadlinePages(BoardApiFetcher):
        def get_text(self, url: str, *, max_bytes: int = 0) -> str:
            requested.append(url)
            if url == _JOBVITE_BOARD:
                if cut_at == "before-first":
                    now[0] += 60.0
            elif url == _JOBVITE_ANALYST:
                now[0] += 60.0
                if cut_at == "during-a-detail":
                    raise httpx.ReadTimeout("timed out", request=httpx.Request("GET", url))
            return pages[url]

    read = DeadlinePages().listings("Acme", _JOBVITE_BOARD)

    successful = int(cut_at == "between-details")
    interrupted = int(cut_at == "during-a-detail")
    assert read.raw_count == 2
    assert [listing.url for listing in read.listings] == ([_JOBVITE_ANALYST] if successful else [])
    assert read.detail.attempted == successful + interrupted
    assert read.detail.read == successful
    assert read.detail.failed == 0
    assert read.detail.interrupted == interrupted
    assert read.detail.capped == 2 - successful - interrupted
    assert read.detail.failure_reasons == ()
    assert requested == (
        [_JOBVITE_BOARD] if cut_at == "before-first" else [_JOBVITE_BOARD, _JOBVITE_ANALYST]
    )
    assert read.complete is False
    assert read.outcome is SourceReadOutcome.CRAWL_CONTINUATION
    assert [target.url for target in read.deferred_targets] == (
        [_JOBVITE_ENGINEER] if successful else [_JOBVITE_ANALYST, _JOBVITE_ENGINEER]
    )
    assert {target.source_url for target in read.deferred_targets} == {_JOBVITE_BOARD}


def test_jobvite_counts_failed_protected_and_capped_declared_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr(
        "tailorcv.infrastructure.web_fetch.board_adapters.transport.time.monotonic", lambda: now[0]
    )
    third = _JOBVITE_ENGINEER.rsplit("/", 1)[0] + "/oKl56mNo"
    board_page = (
        _JOBVITE_TABLE
        + '<table class="jv-job-list"><tbody>'
        + _jobvite_row("oKl56mNo", "Product Manager", "Springfield, Ohio")
        + "</tbody></table>"
    )
    requested: list[str] = []

    class DeadlinePages(BoardApiFetcher):
        def get_text(self, url: str, *, max_bytes: int = 0) -> str:
            requested.append(url)
            if url == _JOBVITE_BOARD:
                return board_page
            now[0] += 60.0
            return "<html><body>Apply</body></html>"

    read = DeadlinePages().listings(
        "Acme",
        _JOBVITE_BOARD,
        options=SourceReadOptions(protected_posting_urls=frozenset({_JOBVITE_ENGINEER})),
    )

    assert read.raw_count == 3
    assert read.listings == []
    assert requested == [_JOBVITE_BOARD, _JOBVITE_ANALYST]
    assert read.detail.attempted == read.detail.failed == 1
    assert read.detail.read == read.detail.interrupted == 0
    assert read.detail.capped == 1
    assert read.protected_posting_urls == (_JOBVITE_ENGINEER,)
    assert read.skip_reasons["protected_posting"] == 1
    assert read.complete is False
    assert read.outcome is SourceReadOutcome.CRAWL_CONTINUATION
    assert [target.url for target in read.deferred_targets] == [_JOBVITE_ANALYST, third]


def test_native_listed_root_retains_exact_membership_and_defers_failed_rows() -> None:
    requested: list[str] = []
    fetcher = _fetcher(_jobvite_pages(**{_JOBVITE_ENGINEER: "<html>Apply</html>"}), requested)

    read = fetcher.listings("Acme", _JOBVITE_BOARD)

    assert read.raw_count == 2
    assert read.detail_read_urls == (_JOBVITE_ANALYST,)
    (receipt,) = read.publisher_listing_receipts
    assert dict(receipt.listing_urls) == {
        "oAb12cDe": _JOBVITE_ANALYST,
        "oFg34hIj": _JOBVITE_ENGINEER,
    }
    assert receipt.completed == (("oAb12cDe", _JOBVITE_ANALYST),)
    (failed,) = read.deferred_targets
    assert failed.url == _JOBVITE_ENGINEER
    assert failed.detail_checkpoint is not None
    assert failed.detail_checkpoint.pending_ids == ("oFg34hIj",)


@pytest.mark.parametrize("invalid", ["missing", "foreign", "identity", "root", "no_body"])
def test_native_listed_body_proof_refuses_unowned_or_unread_details(invalid: str) -> None:
    root = _JOBVITE_BOARD
    url = _JOBVITE_ANALYST
    identity = "oAb12cDe"
    if invalid == "foreign":
        url = url.replace("/acme/", "/initech/")
    if invalid == "identity":
        identity = "different-id"
    if invalid == "root":
        root = root.replace("/acme/", "/initech/")
    target = page_continuation_target(
        url, source_url=root, label="Listed posting", reason="jobvite_posting_pending"
    )
    if invalid != "missing":
        target = replace(
            target,
            detail_checkpoint=SourceDetailCheckpoint(
                platform="jobvite",
                board_url=url,
                pending_ids=(identity,),
                listing_ids=(identity,),
                listing_urls=((identity, url),),
            ),
        )
    page = (
        "<html>Apply</html>"
        if invalid == "no_body"
        else declaring_page(_posting("Clinical Analyst", {"addressCountry": "US"}))
    )
    read = _fetcher({url: page}, []).continue_listings(
        "Acme", (target,), root_url=_JOBVITE_BOARD, handoff_scopes=()
    )

    assert read.detail_read_urls in (None, ())
    if invalid == "no_body":
        assert [pending.url for pending in read.deferred_targets] == [url]


def test_native_listed_host_hold_keeps_manual_recovery_without_spending_detail_history() -> None:
    class HeldPages(BoardApiFetcher):
        def get_text(self, url: str, *, max_bytes: int = 0) -> str:
            if url == _JOBVITE_BOARD:
                return _JOBVITE_TABLE
            raise HostHeldError(url, ready_at=100)

    read = HeldPages().listings("Acme", _JOBVITE_BOARD)

    assert read.raw_count == 2
    assert (read.detail.attempted, read.detail.read, read.detail.failed) == (0, 0, 0)
    assert read.skip_reasons["host_held"] == 1
    assert {target.url for target in read.deferred_targets} == {_JOBVITE_ANALYST, _JOBVITE_ENGINEER}
    assert all(not target.automatic_eligible for target in read.deferred_targets)
    assert all(
        target.detail_checkpoint is not None and not target.detail_checkpoint.failed_attempts
        for target in read.deferred_targets
    )


def test_native_listed_root_keeps_every_failed_identity_beyond_failure_examples() -> None:
    identities = tuple(f"role{index:04d}" for index in range(10))
    urls = tuple(f"https://jobs.jobvite.com/acme/job/{identity}" for identity in identities)
    page = (
        '<table class="jv-job-list"><tbody>'
        + "".join(_jobvite_row(identity, "Clinical Analyst", "US") for identity in identities)
        + "</tbody></table>"
    )
    pages = {_JOBVITE_BOARD: page, **dict.fromkeys(urls, "<html>Apply</html>")}

    read = _fetcher(pages, []).listings("Acme", _JOBVITE_BOARD)

    assert read.raw_count == read.detail.failed == len(identities)
    assert {pending.url for pending in read.deferred_targets} == set(urls)
    assert all(
        pending.detail_checkpoint is not None
        and pending.detail_checkpoint.failed_attempts == ((identity, 1),)
        for identity, pending in zip(identities, read.deferred_targets, strict=True)
    )


@pytest.mark.parametrize("platform", ["jobvite", "avature", "successfactors", "jazzhr"])
@pytest.mark.parametrize("foreign", [False, True])
def test_native_listed_body_proof_names_only_its_original_owned_opening(
    platform: str, foreign: bool
) -> None:
    roots = {
        "jobvite": _JOBVITE_BOARD,
        "avature": _AVATURE,
        "successfactors": f"{_SF_SITE}/Health/",
        "jazzhr": "https://acme.applytojob.com/apply",
    }
    urls = {
        "jobvite": _JOBVITE_ANALYST,
        "avature": _AVATURE_MANAGER,
        "successfactors": _SF_PRODUCT,
        "jazzhr": "https://acme.applytojob.com/apply/AbCd123456/Clinical-Analyst",
    }
    identities = {
        "jobvite": "oAb12cDe",
        "avature": "5703",
        "successfactors": "1443467433",
        "jazzhr": "AbCd123456",
    }
    root, url, identity = roots[platform], urls[platform], identities[platform]
    if foreign:
        url = {
            "jobvite": url.replace("/acme/", "/initech/"),
            "avature": url.replace("/careers/", "/another/"),
            "successfactors": url.replace("/Health/", "/OtherBrand/"),
            "jazzhr": url.replace("acme.applytojob", "initech.applytojob"),
        }[platform]
    target = replace(
        page_continuation_target(
            url, source_url=root, label="Listed posting", reason=platform + "_posting_pending"
        ),
        detail_checkpoint=SourceDetailCheckpoint(
            platform=platform,
            board_url=url,
            pending_ids=(identity,),
            listing_ids=(identity,),
            listing_urls=((identity, url),),
        ),
    )
    page = (
        _avature_page("Clinical Analyst", fields=_AVATURE_BODY)
        if platform == "avature"
        else declaring_page(_posting("Clinical Analyst", {"addressCountry": "US"}))
    )

    read = _fetcher({url: page}, []).continue_listings(
        "Acme", (target,), root_url=root, handoff_scopes=()
    )

    if foreign:
        assert read.detail_read_urls in (None, ())
    else:
        assert read.detail_read_urls == (url,)
        assert read.listings[0].requisition_id == identity
        assert read.listings[0].url == url


def test_native_listed_mixed_batch_keeps_foreign_bodies_out_of_its_receipt() -> None:
    foreign = _JOBVITE_ENGINEER.replace("/acme/", "/initech/")
    pairs = (("oAb12cDe", _JOBVITE_ANALYST), ("oFg34hIj", foreign))
    targets = tuple(
        replace(
            page_continuation_target(
                url,
                source_url=_JOBVITE_BOARD,
                label="Listed posting",
                reason="jobvite_posting_pending",
            ),
            detail_checkpoint=SourceDetailCheckpoint(
                platform="jobvite",
                board_url=url,
                pending_ids=(identity,),
                listing_ids=(identity,),
                listing_urls=((identity, url),),
            ),
        )
        for identity, url in pairs
    )
    page = declaring_page(_posting("Clinical Analyst", {"addressCountry": "US"}))

    read = _fetcher({_JOBVITE_ANALYST: page, foreign: page}, []).continue_listings(
        "Acme", targets, root_url=_JOBVITE_BOARD, handoff_scopes=()
    )

    assert {listing.url for listing in read.listings} == {_JOBVITE_ANALYST, foreign}
    assert read.detail_read_urls == (_JOBVITE_ANALYST,)


def test_native_listed_scope_refuses_a_sitemap_for_another_avature_portal() -> None:
    target = replace(
        page_continuation_target(
            "https://acme.avature.net/another/sitemap.xml",
            source_url=_AVATURE,
            label="Listed sitemap",
            reason="avature_sitemap_pending",
        ),
        detail_checkpoint=SourceDetailCheckpoint(
            platform="listed:avature", board_url="https://acme.avature.net/another/sitemap.xml"
        ),
    )

    assert publisher_search_scope(target) is None


def test_postings_the_read_deadline_cuts_off_are_continuation_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr(
        "tailorcv.infrastructure.web_fetch.board_adapters.transport.time.monotonic", lambda: now[0]
    )
    third = f"{_AVATURE}/JobDetail/Data-Engineer/5905"
    fetcher = _ClockedPages(
        {
            _AVATURE_INDEX: _sitemap_index(_AVATURE_SITEMAP),
            _AVATURE_SITEMAP: sitemap_urlset(_AVATURE_MANAGER, _AVATURE_ANALYST, third),
            _AVATURE_MANAGER: _avature_page("Product Manager", fields=_AVATURE_BODY),
        },
        now,
    )

    read = fetcher.listings("Acme", _AVATURE)

    assert fetcher.requested == [_AVATURE_INDEX, _AVATURE_SITEMAP, _AVATURE_MANAGER]
    assert read.raw_count == 3
    assert read.detail.attempted == read.detail.read == 1
    assert read.detail.failed == read.detail.interrupted == 0
    assert read.detail.capped == 2
    assert [listing.title for listing in read.listings] == ["Product Manager"]
    assert read.complete is False
    assert read.outcome is SourceReadOutcome.CRAWL_CONTINUATION
    assert read.skip_reasons.get("time_limit_reached") == 1
    assert [(target.url, target.deferred_reason) for target in read.deferred_targets] == [
        (_AVATURE_ANALYST, "avature_posting_pending"),
        (third, "avature_posting_pending"),
    ]
    assert {target.source_url for target in read.deferred_targets} == {_AVATURE}


def _continued(url: str, reason: str, *, source_url: str = _AVATURE) -> SourceContinuationTarget:
    return page_continuation_target(
        url, source_url=source_url, label="Listed posting", reason=reason
    )


def test_a_continued_avature_posting_keeps_the_fields_its_page_renders() -> None:
    pages = {
        _AVATURE_ANALYST: _avature_page(
            "Data Analyst",
            fields=_avature_field("Location", "Springfield, OH")
            + _avature_field("Full-time/Part-time", "Part Time")
            + _AVATURE_BODY,
        ),
    }
    fetcher = _fetcher(pages, [], robots=_AVATURE_ROBOTS)

    read = fetcher.continue_listings(
        "Acme",
        (_continued(_AVATURE_ANALYST, "avature_posting_pending"),),
        root_url=_AVATURE,
        handoff_scopes=(),
    )

    (analyst,) = read.listings
    assert (analyst.title, analyst.requisition_id) == ("Data Analyst", "5804")
    assert analyst.facts.locations.values == ("Springfield, OH",)
    assert analyst.facts.employment_types.values == ("Part Time",)
    assert read.deferred_targets == ()

    assert (read.detail.attempted, read.detail.read, read.detail.failed) == (1, 1, 0)


def test_a_continued_jobvite_posting_keeps_the_requisition_its_address_names() -> None:
    page = declaring_page(
        _posting(
            "Clinical Data Analyst",
            {"addressLocality": "Springfield", "addressRegion": "OH", "addressCountry": "US"},
            employmentType="PART_TIME",
        )
    )
    fetcher = _fetcher(_jobvite_pages(**{_JOBVITE_ANALYST: page}), [])

    read = fetcher.continue_listings(
        "Acme",
        (_continued(_JOBVITE_ANALYST, "jobvite_posting_pending", source_url=_JOBVITE_BOARD),),
        root_url=_JOBVITE_BOARD,
        handoff_scopes=(),
    )

    (analyst,) = read.listings
    assert (analyst.title, analyst.requisition_id) == ("Clinical Data Analyst", "oAb12cDe")
    assert analyst.facts.locations.values == ("Springfield, OH, United States",)
    assert analyst.facts.employment_types.values == ("PART_TIME",)
    assert read.deferred_targets == ()


def test_a_platform_sitemap_index_in_a_child_preserves_its_filtered_frontier() -> None:
    child = f"{_AVATURE}/nested-index.xml"
    leaf = f"{_AVATURE}/nested-jobs.xml"
    other_portal = "https://acme.avature.net/another/JobDetail/Data-Analyst/5804"
    requested: list[str] = []
    pages = {
        _AVATURE_INDEX: _sitemap_index(child),
        child: _sitemap_index(leaf),
        leaf: sitemap_urlset(_AVATURE_ANALYST, other_portal, f"{_AVATURE}/blog/team"),
        _AVATURE_ANALYST: _avature_page(
            "Data Analyst",
            fields=_avature_field("Location", "Springfield, OH")
            + _avature_field("Full-time/Part-time", "Part Time")
            + _AVATURE_BODY,
        ),
    }
    fetcher = _fetcher(pages, requested, robots=_AVATURE_ROBOTS)

    initial = fetcher.listings("Acme", _AVATURE)

    assert initial.complete is False
    assert initial.listings == []
    assert [(target.url, target.deferred_reason) for target in initial.deferred_targets] == [
        (leaf, "avature_sitemap_pending")
    ]
    discovered = fetcher.continue_listings(
        "Acme", initial.deferred_targets, root_url=_AVATURE, handoff_scopes=()
    )
    assert [(target.url, target.deferred_reason) for target in discovered.deferred_targets] == [
        (_AVATURE_ANALYST, "avature_posting_pending")
    ]
    final = fetcher.continue_listings(
        "Acme", discovered.deferred_targets, root_url=_AVATURE, handoff_scopes=()
    )
    (analyst,) = final.listings
    assert analyst.url == _AVATURE_ANALYST
    assert analyst.facts.locations.values == ("Springfield, OH",)
    assert analyst.facts.employment_types.values == ("Part Time",)
    assert final.deferred_targets == ()
    assert other_portal not in requested


def test_a_continued_avature_child_sitemap_keeps_only_the_portal_postings() -> None:
    child = f"{_AVATURE}/sitemap-2.xml"
    blog = f"{_AVATURE}/blog/data-careers-at-acme"
    fetcher = _fetcher({child: sitemap_urlset(_AVATURE_ANALYST, blog)}, [], robots=_AVATURE_ROBOTS)

    read = fetcher.continue_listings(
        "Acme",
        (_continued(child, "avature_sitemap_pending"),),
        root_url=_AVATURE,
        handoff_scopes=(),
    )

    assert [(target.url, target.deferred_reason) for target in read.deferred_targets] == [
        (_AVATURE_ANALYST, "avature_posting_pending")
    ]


def test_a_continued_avature_posting_that_fails_to_fetch_stays_deferred_under_its_reason() -> None:
    fetcher = _fetcher({}, [], robots=_AVATURE_ROBOTS, statuses={_AVATURE_ANALYST: 500})

    read = fetcher.continue_listings(
        "Acme",
        (_continued(_AVATURE_ANALYST, "avature_posting_pending"),),
        root_url=_AVATURE,
        handoff_scopes=(),
    )

    assert read.listings == []
    assert [(target.url, target.deferred_reason) for target in read.deferred_targets] == [
        (_AVATURE_ANALYST, "avature_posting_pending")
    ]

    assert (read.detail.attempted, read.detail.read, read.detail.failed) == (1, 0, 1)
    assert read.detail.failure_reasons == (("http_status", 1),)
    assert read.detail.failure_examples == (_AVATURE_ANALYST,)


def test_a_continued_avature_page_declaring_no_posting_keeps_its_failed_detail_pending() -> None:
    closed = "<html><body>This posting has closed.</body></html>"
    fetcher = _fetcher({_AVATURE_ANALYST: closed}, [], robots=_AVATURE_ROBOTS)

    read = fetcher.continue_listings(
        "Acme",
        (_continued(_AVATURE_ANALYST, "avature_posting_pending"),),
        root_url=_AVATURE,
        handoff_scopes=(),
    )

    assert read.listings == []
    assert [(target.url, target.deferred_reason) for target in read.deferred_targets] == [
        (_AVATURE_ANALYST, "avature_posting_pending")
    ]
    assert read.skip_reasons == {"posting_page_declared_no_posting": 1}

    assert (read.detail.attempted, read.detail.read, read.detail.failed) == (1, 0, 1)
    assert read.detail.failure_reasons == (("parse", 1),)


def test_an_avature_sitemap_listing_no_posting_reads_unreadable_not_empty() -> None:
    pages = {
        _AVATURE_INDEX: _sitemap_index(_AVATURE_SITEMAP),
        _AVATURE_SITEMAP: sitemap_urlset(f"{_AVATURE}/SearchJobs"),
    }
    fetcher = _fetcher(pages, [], robots=_AVATURE_ROBOTS)

    read = fetcher.listings("Acme", _AVATURE)

    assert read.listings == []
    assert read.complete is False
    assert read.outcome is SourceReadOutcome.NO_SUPPORTED_REPRESENTATION


def test_an_avature_answer_that_is_not_a_sitemap_refuses_the_board() -> None:
    fetcher = _fetcher(
        {_AVATURE_INDEX: "<html><body>Maintenance</body></html>"}, [], robots=_AVATURE_ROBOTS
    )

    with pytest.raises(ValueError, match="sitemap"):
        fetcher.listings("Acme", _AVATURE)


# ---------------------------------------------------------------------------
# SAP SuccessFactors
# ---------------------------------------------------------------------------

_SF_SITE = "https://careers.acme.example"
_SF_PAGE = (
    "<html><head>"
    '<link type="text/css" rel="stylesheet" href="//rmkcdn.successfactors.com/482f99c6/site.css" />'
    "</head><body><h1>Acme careers</h1></body></html>"
)
_SF_PRODUCT = f"{_SF_SITE}/Health/job/Remote-Product-Manager-AB/1443467433/"
_SF_ENGINEER = f"{_SF_SITE}/Health/job/Springfield-Data-Engineer/1407949433/"
_SF_OTHER_BRAND = f"{_SF_SITE}/job/Springfield-Field-Engineer/1387645433/"


@pytest.mark.parametrize(
    "markers", ["both", "bundle", "configuration"], ids=["talemetry", "bundle-only", "config-only"]
)
@pytest.mark.parametrize(
    "bundle_origin",
    ["", "//careers.globex.test", "https://careers.globex.test", "http://careers.globex.test"],
)
def test_a_talemetry_career_site_needs_both_markers_to_ignore_successfactors_assets(
    markers: str,
    bundle_origin: str,
) -> None:
    url = "https://careers.globex.test/"
    asset = '<link rel="shortcut icon" href="//rmkcdn.successfactors.com/482f99c6/logo.png">'
    bundle = (
        f'<script src="{bundle_origin}/pack/talemetry_careersites/index.abc123def456.js"></script>'
    )
    configuration = (
        '<script type="text/javascript">'
        'CareerSite.Path.configure({"search_jobs_json":"/search/jobs.json","job":"/jobs/:id"});'
        "</script>"
    )
    page = (
        f"<html><head>{asset}"
        f"{bundle if markers != 'configuration' else ''}"
        f"{configuration if markers != 'bundle' else ''}"
        "</head><body><h1>Careers at Globex</h1></body></html>"
    )

    resolved = _fetcher({url: page}, []).resolve(url)

    if markers == "both":
        assert resolved.platform is None
        assert resolved.match is None
    else:
        assert resolved.platform == "successfactors"
        assert resolved.match is not None
    assert resolved.url == url
    assert resolved.page is not None


@pytest.mark.parametrize(
    ("board", "platform"),
    [(_JOBVITE_BOARD, "jobvite"), ("https://arbeitnow.com/", "arbeitnow")],
)
def test_a_talemetry_page_keeps_its_published_outbound_board_readable(
    board: str, platform: str
) -> None:
    url = "https://careers.globex.test/"
    page = (
        '<link rel="shortcut icon" href="//rmkcdn.successfactors.com/482f99c6/logo.png">'
        '<script src="/pack/talemetry_careersites/index.abc123def456.js"></script>'
        '<script>CareerSite.Path.configure({"search_jobs_json":"/search/jobs.json",'
        '"job":"/jobs/:id"});</script>'
        f'<a href="{board}">Search jobs</a>'
    )

    resolved = _fetcher({url: page}, []).resolve(url)

    assert resolved.platform == platform
    assert resolved.match is not None


def test_commented_talemetry_scripts_do_not_override_a_successfactors_board() -> None:
    page = (
        _SF_PAGE + '<!-- <script src="/pack/talemetry_careersites/index.abc123def456.js"></script>'
        '<script>CareerSite.Path.configure({"search_jobs_json":"/search/jobs.json",'
        '"job":"/jobs/:id"});</script> -->'
    )

    identified = identify_board(page)

    assert identified is not None and identified[0] == "successfactors"


def test_a_malformed_script_address_does_not_block_successfactors_detection() -> None:
    page = '<script src="https://["></script>' + _SF_PAGE

    identified = identify_board(page)

    assert identified is not None and identified[0] == "successfactors"


@pytest.mark.parametrize("path", ["/jobs", "/jobs/search"])
@pytest.mark.parametrize(
    "asset",
    [
        '<link rel="stylesheet" href="//rmkcdn.successfactors.com/acme/site.css">',
        '<link rel="icon" href="//rmkcdn.successfactors.com/acme/favicon.ico">',
        '<img src="//rmkcdn.successfactors.com/acme/logo.png">',
    ],
    ids=["stylesheet", "favicon", "logo"],
)
def test_classic_icims_assets_do_not_name_a_successfactors_board(path: str, asset: str) -> None:
    url = f"https://careers-acme.icims.com{path}"
    page = (
        f"<html><head>{asset}</head><body>"
        '<noscript><iframe src="/jobs?in_iframe=1"></iframe></noscript>'
        '<table class="iCIMS_JobSearchTable"><tr><td>'
        '<a href="/jobs/1234/data-analyst/job">Data Analyst</a>'
        "</td></tr></table></body></html>"
    )
    requested: list[str] = []

    resolved = _fetcher({url: page}, requested).resolve(url)

    assert resolved.platform == "icims"
    assert resolved.page is None
    assert requested == []


def test_a_redirect_to_classic_icims_names_icims_rather_than_successfactors() -> None:
    requested_url = f"{_SF_SITE}/jobs"
    served_url = "https://careers-acme.icims.com/jobs/search"

    def answer(url: str) -> httpx.Response:
        if url == requested_url:
            return httpx.Response(
                302, headers={"Location": served_url}, request=httpx.Request("GET", url)
            )
        assert url == served_url
        return httpx.Response(200, request=httpx.Request("GET", url), text=_SF_PAGE)

    resolved = BoardApiFetcher(client=policy_client(answer)).resolve(requested_url)

    assert resolved.platform == "icims"
    assert resolved.url == served_url


@pytest.mark.parametrize("url", [_SF_SITE, "https://careers.icims.com.acme.example/jobs"])
def test_a_custom_career_host_is_resolved_by_its_successfactors_cdn(url: str) -> None:
    resolved = _fetcher({url: _SF_PAGE}, []).resolve(url)

    assert resolved.platform == "successfactors"
    assert resolved.url == url


@pytest.mark.parametrize("url", [_SF_SITE, "https://careers-acme.icims.com/jobs"])
def test_a_jibe_representation_still_names_the_icims_board(url: str) -> None:
    page = _SF_PAGE + '<link href="https://cdn.jibecdn.com/prod/acme/latest/main.css">'

    resolved = _fetcher({url: page}, []).resolve(url)

    assert resolved.platform == "icims"


@pytest.mark.parametrize("url", [_SF_SITE, "https://acme.icims.com/jobs"])
def test_a_published_outbound_board_is_resolved_on_a_page_with_successfactors_assets(
    url: str,
) -> None:
    page = _SF_PAGE + f'<a href="{_JOBVITE_BOARD}">Search jobs</a>'

    resolved = _fetcher({url: page}, []).resolve(url)

    assert resolved.platform == "jobvite"
    assert resolved.match is not None


def _successfactors_unlabelled_page(title: str = "Data Engineer") -> str:
    return (
        f"<html><head><title>{html.escape(title)} Job Details | Acme</title>"
        f'<meta property="og:title" content="{html.escape(title)}"></head><body>'
        "<h1>Careers at Acme</h1>"
        '<div class="jobDisplayShell" itemscope itemtype="http://schema.org/JobPosting">'
        '<div class="jobTitle"><a href="/applybutton/">Apply now</a></div>'
        '<div class="joblayouttoken"><span itemprop="description" '
        'data-careersite-propertyid="description" class="rtltextaligneligible">'
        '<span class="jobdescription"><p><strong>About the team</strong></p>'
        "<p>Build clinical data systems.</p><ul><li>Maintain published terminology.</li>"
        "<li>Review incoming clinical records.</li></ul>"
        "<p>Final published requirement.</p></span></span></div></div>"
        "<aside>Cookie preferences and unrelated navigation</aside></body></html>"
    )


@pytest.mark.parametrize("continued", [False, True])
@pytest.mark.parametrize("title", ["Data Engineer", "Director, Data & Reporting"])
def test_successfactors_reads_an_owned_unlabelled_body_with_agreeing_publisher_titles(
    continued: bool,
    title: str,
) -> None:
    requested: list[str] = []
    fetcher = _fetcher(
        {
            f"{_SF_SITE}/Health/": _SF_PAGE,
            f"{_SF_SITE}/sitemap.xml": sitemap_urlset(_SF_ENGINEER),
            _SF_ENGINEER: _successfactors_unlabelled_page(title),
        },
        requested,
    )

    read = (
        fetcher.continue_listings(
            "Acme",
            (
                _continued(
                    _SF_ENGINEER, "successfactors_posting_pending", source_url=f"{_SF_SITE}/Health/"
                ),
            ),
            root_url=f"{_SF_SITE}/Health/",
            handoff_scopes=(),
        )
        if continued
        else fetcher.listings("Acme", f"{_SF_SITE}/Health/")
    )

    assert len(read.listings) == 1
    posting = read.listings[0]
    assert posting.title == title
    assert posting.url == _SF_ENGINEER
    assert posting.requisition_id == "1407949433"
    assert posting.published_description == (
        "About the team\n\nBuild clinical data systems.\n\n"
        "- Maintain published terminology.\n- Review incoming clinical records.\n\n"
        "Final published requirement."
    )
    assert posting.employer == "Acme"
    assert posting.facts.employer.provenance == "source.employer_hint"
    assert posting.location == ""
    assert posting.facts.locations.values == ()
    assert posting.facts.locations.complete is False
    assert posting.facts.employment_types.values == ()
    assert posting.facts.compensation.values == ()
    assert (read.detail.attempted, read.detail.read, read.detail.failed) == (1, 1, 0)
    assert read.complete is (not continued)
    assert read.deferred_targets == ()
    assert requested == (
        [_SF_ENGINEER]
        if continued
        else [f"{_SF_SITE}/Health/", f"{_SF_SITE}/sitemap.xml", _SF_ENGINEER]
    )


@pytest.mark.parametrize(
    "broken",
    [
        "missing-og",
        "missing-title",
        "disagrees",
        "duplicate-og",
        "duplicate-title",
        "generic-index",
        "generic-title",
        "two-shells",
        "two-descriptions",
        "two-bodies",
        "missing-owner",
        "empty-body",
        "truncated",
        "unclosed-head",
        "labelled-missing-title",
    ],
)
def test_successfactors_refuses_ambiguous_unlabelled_identity_or_body(broken: str) -> None:
    page = _successfactors_unlabelled_page()
    if broken == "missing-og":
        page = page.replace('<meta property="og:title" content="Data Engineer">', "")
    elif broken == "missing-title":
        page = page.replace("<title>Data Engineer Job Details | Acme</title>", "")
    elif broken == "disagrees":
        page = page.replace('content="Data Engineer"', 'content="Product Manager"')
    elif broken == "duplicate-og":
        page = page.replace("</head>", '<meta property="og:title" content="Data Engineer"></head>')
    elif broken == "duplicate-title":
        page = page.replace("</head>", "<title>Data Engineer Job Details | Acme</title></head>")
    elif broken == "generic-index":
        page = page.replace("Data Engineer Job Details | Acme", "Data Engineer | Acme Careers")
    elif broken == "generic-title":
        page = page.replace("Data Engineer", "Careers")
    elif broken == "two-shells":
        page = page.replace(
            "<aside>",
            '<div class="jobDisplayShell" itemscope itemtype="http://schema.org/JobPosting"></div><aside>',
        )
    elif broken == "two-descriptions":
        page = page.replace(
            "</span></span></div>",
            '</span></span><span itemprop="description" data-careersite-propertyid="description" '
            'class="rtltextaligneligible"><span class="jobdescription">'
            "<p>Other body.</p></span></span></div>",
        )
    elif broken == "two-bodies":
        page = page.replace(
            "</span></span></div>",
            '</span><span class="jobdescription"><p>Other body.</p></span></span></div>',
        )
    elif broken == "missing-owner":
        page = page.replace(
            'data-careersite-propertyid="description"', 'data-careersite-propertyid="navigation"'
        )
    elif broken == "empty-body":
        page = page.replace(
            '<span class="jobdescription"><p><strong>About the team</strong></p>'
            "<p>Build clinical data systems.</p><ul><li>Maintain published terminology.</li>"
            "<li>Review incoming clinical records.</li></ul><p>Final published requirement.</p>",
            '<span class="jobdescription"> ',
        )
    elif broken == "truncated":
        page = page.replace("</span></span></div></div>", "")
    elif broken == "unclosed-head":
        page = page.replace("</head>", "")
    else:
        page = page.replace(
            '<div class="joblayouttoken">',
            '<div class="joblayouttoken"><span class="joblayouttoken-label">Description:</span>',
        )
    fetcher = _fetcher(
        {
            f"{_SF_SITE}/Health/": _SF_PAGE,
            f"{_SF_SITE}/sitemap.xml": sitemap_urlset(_SF_ENGINEER),
            _SF_ENGINEER: page,
        },
        [],
    )

    read = fetcher.listings("Acme", f"{_SF_SITE}/Health/")

    assert read.listings == []
    assert read.complete is False
    assert read.detail.failed == 1


@pytest.mark.parametrize("title", ["", "Careers &bull; Acme"])
def test_successfactors_reads_the_postings_an_rss_feed_at_its_sitemap_address_links(
    title: str,
) -> None:
    feed = sitemap_rss(_SF_SITE, _SF_PRODUCT, _SF_OTHER_BRAND)
    fetcher = _fetcher(
        {
            f"{_SF_SITE}/Health/": _SF_PAGE,
            f"{_SF_SITE}/sitemap.xml": feed.replace("<title></title>", f"<title>{title}</title>"),
            _SF_PRODUCT: declaring_page(_posting("Product Manager", {"addressLocality": "Remote"})),
        },
        [],
    )

    read = fetcher.listings("Acme", f"{_SF_SITE}/Health/")

    assert [listing.url for listing in read.listings] == [_SF_PRODUCT]
    assert read.complete is True


@pytest.mark.parametrize("continued", [False, True])
@pytest.mark.parametrize(
    "notice",
    [
        "This position has been filled.",
        "This job is no longer accepting applications.",
        "This requisition is not open for new applications.",
    ],
)
def test_successfactors_closed_posting_page_settles_without_a_listing_or_retry(
    continued: bool, notice: str
) -> None:
    page = (
        "<html><head><title>Job Details | Acme</title></head><body>"
        f"<h1>Careers at Acme</h1><div class='message'><p>{notice}</p></div>"
        "<a href='/Health/'>Search all jobs</a></body></html>"
    )
    fetcher = _fetcher(
        {
            f"{_SF_SITE}/Health/": _SF_PAGE,
            f"{_SF_SITE}/sitemap.xml": sitemap_urlset(_SF_PRODUCT, _SF_ENGINEER),
            _SF_PRODUCT: declaring_page(_posting("Product Manager", {"addressLocality": "Remote"})),
            _SF_ENGINEER: page,
        },
        [],
    )

    read = (
        fetcher.continue_listings(
            "Acme",
            (
                _continued(
                    _SF_ENGINEER, "successfactors_posting_pending", source_url=f"{_SF_SITE}/Health/"
                ),
            ),
            root_url=f"{_SF_SITE}/Health/",
            handoff_scopes=(),
        )
        if continued
        else fetcher.listings("Acme", f"{_SF_SITE}/Health/")
    )

    assert [listing.url for listing in read.listings] == ([] if continued else [_SF_PRODUCT])
    assert read.closed_posting_urls == (_SF_ENGINEER,)
    assert read.detail.failed == 0
    assert read.deferred_targets == ()
    assert "posting_page_declared_no_posting" not in read.skip_reasons
    if not continued:
        assert read.complete is True


@pytest.mark.parametrize("platform", ["jobvite", "successfactors"])
def test_a_listed_rescan_defers_a_recently_read_posting_without_requesting_it(
    platform: str,
) -> None:
    if platform == "jobvite":
        pages, board, recent, unread = (
            _jobvite_pages(),
            _JOBVITE_BOARD,
            _JOBVITE_ANALYST,
            _JOBVITE_ENGINEER,
        )
    else:
        board, recent, unread = f"{_SF_SITE}/Health/", _SF_PRODUCT, _SF_ENGINEER
        pages = {
            board: _SF_PAGE,
            f"{_SF_SITE}/sitemap.xml": sitemap_urlset(_SF_PRODUCT, _SF_ENGINEER),
            _SF_PRODUCT: declaring_page(_posting("Product Manager", {"addressLocality": "Remote"})),
            _SF_ENGINEER: declaring_page(_posting("Data Engineer", {"addressLocality": "Remote"})),
        }
    requested: list[str] = []

    read = _fetcher(pages, requested).listings(
        "Acme",
        board,
        options=SourceReadOptions(recent_posting_urls=frozenset({normalize_fetch_url(recent)})),
    )

    assert recent not in requested
    assert unread in requested
    assert [listing.url for listing in read.listings] == [unread]
    assert [(t.url, t.deferred_reason) for t in read.deferred_targets] == [
        (recent, f"{platform}_posting_pending")
    ]
    assert (read.detail.attempted, read.detail.read, read.detail.capped) == (1, 1, 0)
    assert "time_limit_reached" not in read.skip_reasons


_SF_LIVE_BODY = "<p>" + " ".join(["The team ships care tools for clinics and labs."] * 20) + "</p>"


@pytest.mark.parametrize(
    "notice",
    [
        "<div hidden><p>This job is no longer available.</p></div>",
        "<aside><p>Job not found? Search all openings.</p></aside>",
    ],
)
def test_successfactors_long_page_with_a_hidden_or_aside_notice_is_not_closed(
    notice: str,
) -> None:
    page = (
        "<html><head><title>Job Details | Acme</title></head><body>"
        f"<h1>Careers at Acme</h1>{notice}<main>{_SF_LIVE_BODY}</main></body></html>"
    )
    fetcher = _fetcher(
        {
            f"{_SF_SITE}/Health/": _SF_PAGE,
            f"{_SF_SITE}/sitemap.xml": sitemap_urlset(_SF_ENGINEER),
            _SF_ENGINEER: page,
        },
        [],
    )

    read = fetcher.listings("Acme", f"{_SF_SITE}/Health/")

    assert read.closed_posting_urls == ()


@pytest.mark.parametrize("label", ["Title:", " Title : "])
@pytest.mark.parametrize("continued", [False, True])
def test_successfactors_reads_labelled_details_without_structured_properties(
    label: str, continued: bool
) -> None:
    page = (
        '<div class="jobDisplayShell" itemscope itemtype="http://schema.org/JobPosting">'
        '<div class="joblayouttoken"><span class="joblayouttoken-label">'
        f'{label}</span><span class="rtltextaligneligible">Data Engineer</span></div>'
        '<div class="joblayouttoken"><span class="joblayouttoken-label">Description:</span>'
        '<span class="rtltextaligneligible"><div><p>Build clinical data systems.</p>'
        "<p>Maintain published terminology.</p></div></span></div></div>"
        "<div>Cookie preferences and unrelated navigation</div>"
    )
    fetcher = _fetcher(
        {
            f"{_SF_SITE}/Health/": _SF_PAGE,
            f"{_SF_SITE}/sitemap.xml": sitemap_urlset(_SF_ENGINEER),
            _SF_ENGINEER: page,
        },
        [],
    )

    read = (
        fetcher.continue_listings(
            "Acme",
            (
                _continued(
                    _SF_ENGINEER, "successfactors_posting_pending", source_url=f"{_SF_SITE}/Health/"
                ),
            ),
            root_url=f"{_SF_SITE}/Health/",
            handoff_scopes=(),
        )
        if continued
        else fetcher.listings("Acme", f"{_SF_SITE}/Health/")
    )

    assert len(read.listings) == 1
    posting = read.listings[0]
    assert posting.title == "Data Engineer"
    assert posting.url == _SF_ENGINEER
    assert "Build clinical data systems." in posting.published_description
    assert "Maintain published terminology." in posting.published_description
    assert "Cookie preferences" not in posting.published_description
    assert (read.detail.read, read.detail.failed) == (1, 0)


def _successfactors_itemprop_page(shape: str) -> str:
    og = '<meta property="og:title" content="Data Engineer">'
    if shape == "untyped":
        return (
            f"<html><head><title>Acme Careers</title>{og}</head><body>"
            '<div class="jobDisplayShell" itemscope>'
            '<span itemprop="title" data-careersite-propertyid="title">Data Engineer</span>'
            '<span itemprop="description" data-careersite-propertyid="description" '
            'class="jobdescription"><p>Build clinical data systems.</p>'
            "<p>Maintain published terminology.</p></span></div></body></html>"
        )
    company, department = ("Company", "Department")
    if shape == "localized":
        company, department = ("Unternehmen", "Abteilung")
    return (
        f"<html><head><title>Stellenangebot bei Acme</title>{og}</head><body>"
        '<h1><span itemprop="title">Data Engineer</span></h1>'
        '<div class="jobDisplayShell" itemscope itemtype="http://schema.org/JobPosting">'
        f'<div class="joblayouttoken"><span class="joblayouttoken-label">{company}:</span>'
        '<span class="rtltextaligneligible">Acme</span></div>'
        f'<div class="joblayouttoken"><span class="joblayouttoken-label">{department}:</span>'
        '<span class="rtltextaligneligible">Data</span></div>'
        '<div class="joblayouttoken"><span itemprop="description"></span></div>'
        '<div class="joblayouttoken"><span itemprop="description">'
        "<p>Build clinical data systems.</p></span>"
        '<span itemprop="description"> </span>'
        '<span itemprop="description"><p>Maintain published terminology.</p></span></div>'
        "</div><aside>Cookie preferences and unrelated navigation</aside></body></html>"
    )


@pytest.mark.parametrize("shape", ["untyped", "mixed", "localized"])
def test_successfactors_reads_a_posting_anchored_on_its_itemprop_title_and_body(
    shape: str,
) -> None:
    fetcher = _fetcher(
        {
            f"{_SF_SITE}/Health/": _SF_PAGE,
            f"{_SF_SITE}/sitemap.xml": sitemap_urlset(_SF_ENGINEER),
            _SF_ENGINEER: _successfactors_itemprop_page(shape),
        },
        [],
    )

    read = fetcher.listings("Acme", f"{_SF_SITE}/Health/")

    assert [listing.title for listing in read.listings] == ["Data Engineer"]
    assert read.listings[0].published_description == (
        "Build clinical data systems.\n\nMaintain published terminology."
    )
    assert (read.detail.read, read.detail.failed) == (1, 0)


@pytest.mark.parametrize(
    "broken", ["two-title-spans", "og-disagrees", "no-body", "typed-elsewhere", "generic-title"]
)
@pytest.mark.parametrize("shape", ["untyped", "mixed"])
def test_successfactors_refuses_an_ambiguous_itemprop_posting(shape: str, broken: str) -> None:
    page = _successfactors_itemprop_page(shape)
    if broken == "two-title-spans":
        page = page.replace("<body>", '<body><span itemprop="title">Product Manager</span>')
    elif broken == "og-disagrees":
        page = page.replace('content="Data Engineer"', 'content="Product Manager"')
    elif broken == "no-body":
        page = re.sub(r"<p>[^<]*</p>", "", page)
    elif broken == "typed-elsewhere":
        page = page.replace("itemscope", 'itemscope itemtype="http://schema.org/Event"', 1)
        page = page.replace('itemtype="http://schema.org/JobPosting"', "")
    else:
        page = page.replace("Data Engineer", "Careers")
    fetcher = _fetcher(
        {
            f"{_SF_SITE}/Health/": _SF_PAGE,
            f"{_SF_SITE}/sitemap.xml": sitemap_urlset(_SF_ENGINEER),
            _SF_ENGINEER: page,
        },
        [],
    )

    read = fetcher.listings("Acme", f"{_SF_SITE}/Health/")

    assert read.listings == []
    assert read.detail.failed == 1


@pytest.mark.parametrize(
    "broken", ["missing-title", "missing-body", "duplicate-title", "two-shells", "truncated"]
)
def test_successfactors_does_not_invent_identity_from_ambiguous_labelled_details(
    broken: str,
) -> None:
    title = (
        '<div class="joblayouttoken"><span class="joblayouttoken-label">Title:</span>'
        '<span class="rtltextaligneligible">Data Engineer</span></div>'
    )
    body = (
        '<div class="joblayouttoken"><span class="joblayouttoken-label">Description:</span>'
        '<span class="rtltextaligneligible"><p>Build clinical data systems.</p></span></div>'
    )
    page = (
        '<div class="jobDisplayShell" itemscope itemtype="http://schema.org/JobPosting">'
        + ("" if broken == "missing-title" else title)
        + ("" if broken == "missing-body" else body)
        + (title if broken == "duplicate-title" else "")
        + ("" if broken == "truncated" else "</div>")
    )
    if broken == "two-shells":
        page += page
    fetcher = _fetcher(
        {
            f"{_SF_SITE}/Health/": _SF_PAGE,
            f"{_SF_SITE}/sitemap.xml": sitemap_urlset(_SF_ENGINEER),
            _SF_ENGINEER: page,
        },
        [],
    )

    read = fetcher.listings("Acme", f"{_SF_SITE}/Health/")

    assert read.listings == []
    assert read.complete is False
    assert read.detail.failed == 1


def test_successfactors_reads_the_brand_postings_its_sitemap_lists() -> None:
    requested: list[str] = []
    pages = {
        f"{_SF_SITE}/Health/": _SF_PAGE,
        f"{_SF_SITE}/sitemap.xml": sitemap_urlset(_SF_OTHER_BRAND, _SF_PRODUCT, _SF_ENGINEER),
        _SF_PRODUCT: declaring_page(
            _posting(
                "Product Manager",
                {"addressLocality": "Remote", "addressRegion": "AB", "addressCountry": "CA"},
                hiringOrganization="Acme",
            )
        ),
        _SF_ENGINEER: declaring_page(_posting("Data Engineer", {"addressLocality": "Springfield"})),
    }
    fetcher = _fetcher(pages, requested, robots="User-agent: *\nDisallow: /applybutton/\n")

    read = fetcher.listings("Acme", f"{_SF_SITE}/Health/")

    assert requested == [
        f"{_SF_SITE}/Health/",
        f"{_SF_SITE}/sitemap.xml",
        _SF_PRODUCT,
        _SF_ENGINEER,
    ]
    assert read.complete is True
    product, engineer = read.listings
    assert (product.title, product.url, product.requisition_id) == (
        "Product Manager",
        _SF_PRODUCT,
        "1443467433",
    )
    assert product.facts.locations.values == ("Remote, AB, Canada",)
    assert engineer.title == "Data Engineer"

    assert (read.detail.attempted, read.detail.read, read.detail.failed) == (2, 2, 0)


@pytest.mark.parametrize("cut_at", ["before-first", "between-details", "during-a-detail"])
def test_successfactors_detail_counts_distinguish_deadline_cuts(
    monkeypatch: pytest.MonkeyPatch, cut_at: str
) -> None:
    now = [100.0]
    monkeypatch.setattr(
        "tailorcv.infrastructure.web_fetch.board_adapters.transport.time.monotonic", lambda: now[0]
    )
    requested: list[str] = []

    class DeadlinePages(BoardApiFetcher):
        def get_feed(self, _url: str) -> str:
            if cut_at == "before-first":
                now[0] += 60.0
            return sitemap_urlset(_SF_PRODUCT, _SF_ENGINEER)

        def get_text(self, url: str, *, max_bytes: int = 0) -> str:
            if url == f"{_SF_SITE}/Health/":
                return _SF_PAGE
            requested.append(url)
            if len(requested) == 1:
                if cut_at == "between-details":
                    now[0] += 60.0
                return declaring_page(_posting("Product Manager", {"addressLocality": "Remote"}))
            now[0] += 60.0
            raise httpx.ReadTimeout("timed out", request=httpx.Request("GET", url))

    read = DeadlinePages().listings("Acme", f"{_SF_SITE}/Health/")

    successful = int(cut_at != "before-first")
    interrupted = int(cut_at == "during-a-detail")
    assert read.raw_count == 2
    assert len(read.listings) == successful
    assert read.detail.attempted == successful + interrupted
    assert read.detail.read == successful
    assert read.detail.failed == 0
    assert read.detail.interrupted == interrupted
    assert read.detail.capped == 2 - successful - interrupted
    assert read.detail.failure_reasons == ()
    assert [target.url for target in read.deferred_targets] == (
        [_SF_PRODUCT, _SF_ENGINEER] if cut_at == "before-first" else [_SF_ENGINEER]
    )


def test_a_protected_posting_past_the_deadline_is_never_capped_or_rearmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr(
        "tailorcv.infrastructure.web_fetch.board_adapters.transport.time.monotonic", lambda: now[0]
    )
    requested: list[str] = []
    sitemap = f"{_SF_SITE}/sitemap.xml"

    def answer(url: str) -> httpx.Response:
        requested.append(url)
        if url == sitemap:
            now[0] += 60.0
            page = sitemap_urlset(_SF_PRODUCT, _SF_ENGINEER)
        elif url == f"{_SF_SITE}/Health/":
            page = _SF_PAGE
        else:
            page = declaring_page(_posting("Data Engineer"))
        return httpx.Response(200, request=httpx.Request("GET", url), text=page)

    read = BoardApiFetcher(client=policy_client(answer)).listings(
        "Acme",
        f"{_SF_SITE}/Health/",
        options=SourceReadOptions(protected_posting_urls=frozenset({_SF_ENGINEER})),
    )

    assert _SF_PRODUCT not in requested and _SF_ENGINEER not in requested
    assert read.raw_count == 2
    assert read.listings == []
    assert read.detail.read == 0
    assert read.detail.attempted == read.detail.failed == 0
    assert read.detail.capped == 1
    assert read.protected_posting_urls == (_SF_ENGINEER,)
    assert [target.url for target in read.deferred_targets] == [_SF_PRODUCT]


def test_a_continuation_batch_counts_postings_left_unattempted_at_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr(
        "tailorcv.infrastructure.web_fetch.board_adapters.transport.time.monotonic", lambda: now[0]
    )
    fetcher = _ClockedPages(
        {_SF_PRODUCT: declaring_page(_posting("Product Manager", {"addressLocality": "Remote"}))},
        now,
    )
    targets = tuple(
        _continued(url, "successfactors_posting_pending", source_url=f"{_SF_SITE}/Health/")
        for url in (_SF_PRODUCT, _SF_ENGINEER)
    )

    read = fetcher.continue_listings(
        "Acme", targets, root_url=f"{_SF_SITE}/Health/", handoff_scopes=()
    )

    assert read.detail.attempted == 1
    assert read.detail.read == 1
    assert read.detail.capped == 1
    assert [target.url for target in read.deferred_targets] == [_SF_ENGINEER]


@pytest.mark.parametrize("failure", ["timeout", "interrupted", "held", "policy"])
def test_a_continued_posting_records_only_actual_detail_outcomes(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    now = [100.0]
    monkeypatch.setattr(
        "tailorcv.infrastructure.web_fetch.board_adapters.transport.time.monotonic", lambda: now[0]
    )

    class RefusedPage(BoardApiFetcher):
        def get_text(self, url: str, *, max_bytes: int = 0) -> str:
            if failure == "held":
                raise HostHeldError(url, ready_at=200.0)
            if failure == "policy":
                raise FetchPolicyError("Publisher policy refused the page")
            if failure == "interrupted":
                now[0] += 60.0
            raise httpx.ReadTimeout("timed out", request=httpx.Request("GET", url))

    target = _continued(
        _SF_PRODUCT, "successfactors_posting_pending", source_url=f"{_SF_SITE}/Health/"
    )
    read = RefusedPage().continue_listings(
        "Acme", (target,), root_url=f"{_SF_SITE}/Health/", handoff_scopes=()
    )

    assert read.listings == []
    assert read.deferred_targets == (target,)
    assert read.detail.attempted == int(failure in {"timeout", "interrupted"})
    assert read.detail.read == 0
    assert read.detail.failed == int(failure == "timeout")
    assert read.detail.interrupted == int(failure == "interrupted")
    assert read.detail.capped == 0
    assert read.detail.failure_reasons == ((("timeout", 1),) if failure == "timeout" else ())


def test_a_sitemap_in_the_legacy_google_namespace_still_lists_the_brand_postings() -> None:
    legacy = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<?xml-stylesheet type="text/xsl" href="{_SF_SITE}/view/xsl/sitemapssl.xsl"?>'
        '<urlset xmlns="http://www.google.com/schemas/sitemap/0.9">'
        f"<url><loc>{_SF_ENGINEER}</loc><changefreq>daily</changefreq></url></urlset>"
    )
    pages = {
        f"{_SF_SITE}/Health/": _SF_PAGE,
        f"{_SF_SITE}/sitemap.xml": legacy,
        _SF_ENGINEER: declaring_page(_posting("Data Engineer", {"addressLocality": "Springfield"})),
    }
    fetcher = _fetcher(pages, [])

    read = fetcher.listings("Acme", f"{_SF_SITE}/Health/")

    assert [listing.title for listing in read.listings] == ["Data Engineer"]
    assert read.complete is True


def test_children_past_the_sitemap_index_bound_are_continuation_targets() -> None:
    children = [f"{_SF_SITE}/sitemap-{index}.xml" for index in range(SITEMAP_CHILDREN + 5)]
    pages = {
        f"{_SF_SITE}/Health/": _SF_PAGE,
        f"{_SF_SITE}/sitemap.xml": _sitemap_index(*children),
        **{child: sitemap_urlset() for child in children},
        children[0]: sitemap_urlset(_SF_ENGINEER),
        _SF_ENGINEER: declaring_page(_posting("Data Engineer", {"addressLocality": "Springfield"})),
    }
    requested: list[str] = []
    fetcher = _fetcher(pages, requested)

    read = fetcher.listings("Acme", f"{_SF_SITE}/Health/")

    assert [listing.title for listing in read.listings] == ["Data Engineer"]
    assert not set(children[SITEMAP_CHILDREN:]) & set(requested)
    assert read.complete is False
    assert read.outcome is SourceReadOutcome.CRAWL_CONTINUATION
    assert [(target.url, target.deferred_reason) for target in read.deferred_targets] == [
        (child, "successfactors_sitemap_pending") for child in children[SITEMAP_CHILDREN:]
    ]


def test_a_child_sitemap_that_fails_is_left_for_continuation_and_the_rest_still_read() -> None:
    failing, listed = f"{_SF_SITE}/sitemap-0.xml", f"{_SF_SITE}/sitemap-1.xml"
    pages = {
        f"{_SF_SITE}/Health/": _SF_PAGE,
        f"{_SF_SITE}/sitemap.xml": _sitemap_index(failing, listed),
        listed: sitemap_urlset(_SF_ENGINEER),
        _SF_ENGINEER: declaring_page(_posting("Data Engineer", {"addressLocality": "Springfield"})),
    }
    fetcher = _fetcher(pages, [], statuses={failing: 500})

    read = fetcher.listings("Acme", f"{_SF_SITE}/Health/")

    assert [listing.title for listing in read.listings] == ["Data Engineer"]
    assert read.complete is False
    assert read.outcome is SourceReadOutcome.CRAWL_CONTINUATION
    assert [(target.url, target.deferred_reason) for target in read.deferred_targets] == [
        (failing, "successfactors_sitemap_pending")
    ]


@pytest.mark.parametrize("body", ["", "<html><body>busy</body></html>"])
def test_a_child_sitemap_that_answers_no_xml_is_left_for_continuation(body: str) -> None:
    empty, listed = f"{_SF_SITE}/sitemap-0.xml", f"{_SF_SITE}/sitemap-1.xml"
    pages = {
        f"{_SF_SITE}/Health/": _SF_PAGE,
        f"{_SF_SITE}/sitemap.xml": _sitemap_index(empty, listed),
        empty: body,
        listed: sitemap_urlset(_SF_ENGINEER),
        _SF_ENGINEER: declaring_page(_posting("Data Engineer", {"addressLocality": "Springfield"})),
    }
    fetcher = _fetcher(pages, [])

    read = fetcher.listings("Acme", f"{_SF_SITE}/Health/")

    assert [listing.title for listing in read.listings] == ["Data Engineer"]
    assert read.complete is False
    assert [(target.url, target.deferred_reason) for target in read.deferred_targets] == [
        (empty, "successfactors_sitemap_pending")
    ]


@pytest.mark.parametrize("status", [404, 410])
def test_a_child_sitemap_the_publisher_removed_leaves_the_read_complete(status: int) -> None:
    gone, listed = f"{_SF_SITE}/sitemap-0.xml", f"{_SF_SITE}/sitemap-1.xml"
    pages = {
        f"{_SF_SITE}/Health/": _SF_PAGE,
        f"{_SF_SITE}/sitemap.xml": _sitemap_index(gone, listed),
        listed: sitemap_urlset(_SF_ENGINEER),
        _SF_ENGINEER: declaring_page(_posting("Data Engineer", {"addressLocality": "Springfield"})),
    }
    fetcher = _fetcher(pages, [], statuses={gone: status})

    read = fetcher.listings("Acme", f"{_SF_SITE}/Health/")

    assert [listing.title for listing in read.listings] == ["Data Engineer"]
    assert read.deferred_targets == ()
    assert read.complete is True
    assert read.skip_reasons.get("sitemap_child_gone") == 1


def test_a_continued_child_index_counts_the_sitemaps_it_lists_that_are_gone() -> None:
    child = f"{_SF_SITE}/sitemap-3.xml"
    gone, listed = f"{_SF_SITE}/sitemap-3a.xml", f"{_SF_SITE}/sitemap-3b.xml"
    fetcher = _fetcher(
        {child: _sitemap_index(gone, listed), listed: sitemap_urlset(_SF_ENGINEER)},
        [],
        statuses={gone: 404},
    )

    read = fetcher.continue_listings(
        "Acme",
        (_continued(child, "successfactors_sitemap_pending", source_url=f"{_SF_SITE}/Health/"),),
        root_url=f"{_SF_SITE}/Health/",
        handoff_scopes=(),
    )

    assert [(target.url, target.deferred_reason) for target in read.deferred_targets] == [
        (_SF_ENGINEER, "successfactors_posting_pending")
    ]
    assert read.skip_reasons.get("sitemap_child_gone") == 1


@pytest.mark.parametrize(("status", "finished"), [(404, True), (410, True), (500, False)])
def test_a_continued_child_sitemap_is_finished_only_when_the_publisher_removed_it(
    status: int, finished: bool
) -> None:
    child = f"{_SF_SITE}/sitemap-3.xml"
    fetcher = _fetcher({}, [], statuses={child: status})

    read = fetcher.continue_listings(
        "Acme",
        (_continued(child, "successfactors_sitemap_pending", source_url=f"{_SF_SITE}/Health/"),),
        root_url=f"{_SF_SITE}/Health/",
        handoff_scopes=(),
    )

    owed = [(target.url, target.deferred_reason) for target in read.deferred_targets]
    assert owed == ([] if finished else [(child, "successfactors_sitemap_pending")])
    assert ("sitemap_child_gone" in read.skip_reasons) is finished


@pytest.mark.parametrize("continued", [False, True])
@pytest.mark.parametrize("status", [404, 410])
def test_a_sitemap_listed_page_the_publisher_removed_is_closed_not_retried(
    continued: bool, status: int
) -> None:
    pages = {
        f"{_SF_SITE}/Health/": _SF_PAGE,
        f"{_SF_SITE}/sitemap.xml": sitemap_urlset(_SF_PRODUCT, _SF_ENGINEER),
        _SF_PRODUCT: declaring_page(_posting("Product Manager", {"addressLocality": "Remote"})),
    }
    fetcher = _fetcher(pages, [], statuses={_SF_ENGINEER: status})

    read = (
        fetcher.continue_listings(
            "Acme",
            (
                _continued(
                    _SF_ENGINEER, "successfactors_posting_pending", source_url=f"{_SF_SITE}/Health/"
                ),
            ),
            root_url=f"{_SF_SITE}/Health/",
            handoff_scopes=(),
        )
        if continued
        else fetcher.listings("Acme", f"{_SF_SITE}/Health/")
    )

    assert [listing.url for listing in read.listings] == ([] if continued else [_SF_PRODUCT])
    assert read.closed_posting_urls == (_SF_ENGINEER,)
    assert read.detail.failed == 0
    assert read.deferred_targets == ()
    if not continued:
        assert read.complete is True


@pytest.mark.parametrize("continued", [False, True])
def test_sitemap_listed_pages_all_answering_404_stay_owed_rather_than_closed(
    continued: bool,
) -> None:
    pages = {
        f"{_SF_SITE}/Health/": _SF_PAGE,
        f"{_SF_SITE}/sitemap.xml": sitemap_urlset(_SF_PRODUCT, _SF_ENGINEER),
    }
    fetcher = _fetcher(pages, [], statuses={_SF_PRODUCT: 404, _SF_ENGINEER: 410})

    read = (
        fetcher.continue_listings(
            "Acme",
            tuple(
                _continued(url, "successfactors_posting_pending", source_url=f"{_SF_SITE}/Health/")
                for url in (_SF_PRODUCT, _SF_ENGINEER)
            ),
            root_url=f"{_SF_SITE}/Health/",
            handoff_scopes=(),
        )
        if continued
        else fetcher.listings("Acme", f"{_SF_SITE}/Health/")
    )

    assert read.closed_posting_urls == ()
    assert read.complete is False
    assert read.detail.failed == 2
    assert [(target.url, target.deferred_reason) for target in read.deferred_targets] == [
        (_SF_PRODUCT, "successfactors_posting_pending"),
        (_SF_ENGINEER, "successfactors_posting_pending"),
    ]


def test_a_sitemap_listed_page_that_fails_otherwise_stays_owed() -> None:
    pages = {
        f"{_SF_SITE}/Health/": _SF_PAGE,
        f"{_SF_SITE}/sitemap.xml": sitemap_urlset(_SF_ENGINEER),
    }
    fetcher = _fetcher(pages, [], statuses={_SF_ENGINEER: 500})

    read = fetcher.listings("Acme", f"{_SF_SITE}/Health/")

    assert read.closed_posting_urls == ()
    assert read.complete is False
    assert [(target.url, target.deferred_reason) for target in read.deferred_targets] == [
        (_SF_ENGINEER, "successfactors_posting_pending")
    ]


def test_a_sitemap_posting_page_without_markup_leaves_the_rest_of_the_board_readable() -> None:
    pages = {
        f"{_SF_SITE}/Health/": _SF_PAGE,
        f"{_SF_SITE}/sitemap.xml": sitemap_urlset(_SF_PRODUCT, _SF_ENGINEER),
        _SF_PRODUCT: "<html><body>Loading job details.</body></html>",
        _SF_ENGINEER: declaring_page(_posting("Data Engineer", {"addressLocality": "Springfield"})),
    }
    fetcher = _fetcher(pages, [])

    read = fetcher.listings("Acme", f"{_SF_SITE}/Health/")

    assert [listing.title for listing in read.listings] == ["Data Engineer"]
    assert read.complete is False
    assert read.outcome is SourceReadOutcome.CRAWL_CONTINUATION
    assert "1 of 2" in read.note

    assert (read.detail.attempted, read.detail.read, read.detail.failed) == (2, 1, 1)
    assert read.detail.failure_reasons == (("parse", 1),)
    assert read.detail.failure_examples == (_SF_PRODUCT,)


def test_a_successfactors_sitemap_with_no_posting_for_the_brand_reads_unreadable() -> None:
    pages = {
        f"{_SF_SITE}/Health/": _SF_PAGE,
        f"{_SF_SITE}/sitemap.xml": sitemap_urlset(_SF_OTHER_BRAND),
    }
    fetcher = _fetcher(pages, [])

    read = fetcher.listings("Acme", f"{_SF_SITE}/Health/")

    assert read.listings == []
    assert read.complete is False
    assert read.outcome is SourceReadOutcome.NO_SUPPORTED_REPRESENTATION


def test_a_continued_successfactors_child_sitemap_keeps_only_the_brand_postings() -> None:
    child = f"{_SF_SITE}/sitemap-3.xml"
    fetcher = _fetcher({child: sitemap_urlset(_SF_OTHER_BRAND, _SF_ENGINEER)}, [])

    read = fetcher.continue_listings(
        "Acme",
        (_continued(child, "successfactors_sitemap_pending", source_url=f"{_SF_SITE}/Health/"),),
        root_url=f"{_SF_SITE}/Health/",
        handoff_scopes=(),
    )

    assert [(target.url, target.deferred_reason) for target in read.deferred_targets] == [
        (_SF_ENGINEER, "successfactors_posting_pending")
    ]
