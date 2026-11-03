from __future__ import annotations

import collections
import contextlib
import json
import threading
import time
from collections.abc import Mapping
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
import pytest

from clearscout.application.discovery.source_reading import (
    SourceContinuationTarget,
    SourceReadOptions,
)
from clearscout.domain import PostingContentAccessStatus, SourceReadOutcome
from clearscout.domain.staged_facts import ListingIdentityTrust
from clearscout.infrastructure.web_fetch.publisher_sources import (
    PublisherReadLimits,
    PublisherSourceFetcher,
)


class _Client:
    def __init__(self, pages: Mapping[str, str | Exception | httpx.Response]) -> None:
        self.pages = pages
        self.requested: list[str] = []

    def get(self, url: str, **_kwargs: Any) -> httpx.Response:
        self.requested.append(url)
        result = self.pages[url]
        if isinstance(result, Exception):
            raise result
        if isinstance(result, httpx.Response):
            return result
        return httpx.Response(
            200,
            text=result,
            request=httpx.Request("GET", url),
        )


def _posting(title: str) -> str:
    return f"""
    <main><article><h1>{title}</h1><h2>Mandate</h2>
    <p>This role leads a substantial program across partner organizations and owns its
    measurable delivery outcomes, operating model, priorities, and stakeholder commitments.</p>
    <h2>Qualifications</h2><p>Candidates bring extensive leadership experience, sound
    judgement, clear communication, and the ability to guide multidisciplinary teams.</p>
    </article></main>
    """


def _declared_posting(title: str, employer: str, url: str) -> str:
    posting = {
        "@type": "JobPosting",
        "title": title,
        "hiringOrganization": {"name": employer},
        "url": url,
    }
    return '<script type="application/ld+json">' + json.dumps(posting) + "</script>"


def _deferred_urls(result: Any) -> tuple[str, ...]:
    return tuple(target.url for target in result.deferred_targets)


@pytest.mark.parametrize("declared_pager", [False, True])
def test_owned_numeric_pager_retains_disjoint_tracking_query_postings(declared_pager: bool) -> None:
    root_url = "https://publisher.example/jobs/"
    second_url = root_url + "?paged=2"
    addresses = [
        f"https://publisher.example/job/clinical-analyst-{number}/"
        + ("?ref=paged%3D2" if number > 2 else "")
        for number in range(1, 5)
    ]

    def links(urls: list[str]) -> str:
        return (
            "<ul>"
            + "".join(
                f'<li><a href="{url}">Clinical Analyst {index}</a></li>'
                for index, url in enumerate(urls)
            )
            + "</ul>"
        )

    pager_class = ' class="pagination"' if declared_pager else ""
    root = "<main>" + links(addresses[:2]) + f"<ul{pager_class}>"
    root += '<li><a class="page-numbers" href="?paged=2">2</a></li></ul></main>'
    pages = {
        second_url: "<main>" + links(addresses[2:]) + "</main>",
        **{
            url: '<script type="application/ld+json">'
            + json.dumps({"@type": "JobPosting", "title": "Clinical Analyst", "url": url})
            + "</script>"
            for url in addresses
        },
    }
    client = _Client(pages)
    read = PublisherSourceFetcher(client=cast(Any, client)).read("Acme", root_url, root)
    assert {item.url for item in read.listings} == set(
        addresses if declared_pager else addresses[:2]
    )
    assert (second_url in client.requested) is declared_pager
    assert read.complete is False


_PAGER_ROOT = "https://publisher.example/jobs/"


def _pager_listing(numbers: range, following: int | None) -> str:
    """A collection page listing posting links, with a declared next page when given."""
    items = "".join(
        f'<li><a href="/job/clinical-analyst-{n}/">Clinical Analyst {n}</a></li>' for n in numbers
    )
    pager = (
        f'<ul class="pagination"><li><a class="page-numbers" href="?paged={following}">'
        f"{following}</a></li></ul>"
        if following
        else ""
    )
    return f"<main><ul>{items}</ul>{pager}</main>"


def _job_record(n: int) -> str:
    url = f"https://publisher.example/job/clinical-analyst-{n}/"
    record = {"@type": "JobPosting", "title": f"Clinical Analyst {n}", "url": url}
    return '<script type="application/ld+json">' + json.dumps(record) + "</script>"


def test_a_finite_pager_deeper_than_the_depth_bound_reads_every_page() -> None:
    last = 6
    pages = {
        f"{_PAGER_ROOT}?paged={index}": _pager_listing(
            range(2 * index - 1, 2 * index + 1), index + 1 if index < last else None
        )
        for index in range(2, last + 1)
    }
    pages.update(
        {
            f"https://publisher.example/job/clinical-analyst-{n}/": _job_record(n)
            for n in range(1, 2 * last + 1)
        }
    )
    client = _Client(pages)

    read = PublisherSourceFetcher(client=cast(Any, client)).read(
        "Acme", _PAGER_ROOT, _pager_listing(range(1, 3), 2)
    )

    assert len({item.url for item in read.listings}) == 2 * last
    assert all(client.requested.count(f"{_PAGER_ROOT}?paged={i}") == 1 for i in range(2, last + 1))
    assert not any(
        target.deferred_reason == "depth_limit_reached" for target in read.deferred_targets
    )


def test_an_endless_pager_that_adds_no_postings_stops_at_the_depth_bound() -> None:
    pages = {
        f"{_PAGER_ROOT}?paged={index}": _pager_listing(range(1, 3), index + 1)
        for index in range(2, 40)
    }
    pages.update(
        {f"https://publisher.example/job/clinical-analyst-{n}/": _job_record(n) for n in (1, 2)}
    )
    client = _Client(pages)

    read = PublisherSourceFetcher(client=cast(Any, client)).read(
        "Acme", _PAGER_ROOT, _pager_listing(range(1, 3), 2)
    )

    assert [url for url in client.requested if "paged=" in url] == [
        f"{_PAGER_ROOT}?paged=2",
        f"{_PAGER_ROOT}?paged=3",
    ]
    assert [(target.url, target.deferred_reason) for target in read.deferred_targets] == [
        (f"{_PAGER_ROOT}?paged=4", "depth_limit_reached")
    ]


def test_single_publisher_opening_is_followed_without_a_repeated_link_cluster() -> None:
    root = '<main><div class="role"><a href="/roles/only">Only Role</a></div></main>'
    client = _Client({"https://publisher.example/roles/only": _posting("Only Role")})
    fetcher = PublisherSourceFetcher(client=cast(Any, client))

    result = fetcher.read("Example Health", "https://publisher.example/careers", root)

    assert [(item.title, item.url) for item in result.listings] == [
        ("Only Role", "https://publisher.example/roles/only")
    ]
    assert result.listings[0].content_status is PostingContentAccessStatus.READABLE
    assert result.outcome is SourceReadOutcome.PUBLISHER_LINKED_PAGES
    assert result.deferred_targets == ()


@pytest.mark.parametrize("failed_detail", [False, True])
def test_publisher_declared_list_pages_retain_each_read_and_protected_posting(
    failed_detail: bool,
) -> None:
    root_url = "https://publisher.example/"
    list_url = root_url + "jobs/search/321"
    detail_urls = [root_url + f"jobs/program-analyst-{number}" for number in range(1, 16)]
    pages: dict[str, str | Exception | httpx.Response] = {}
    for page_number, numbers in [(1, range(1, 11)), (2, range(11, 16))]:
        url = list_url if page_number == 1 else list_url + "/page2"
        rows = "".join(
            f'<div class="job_list_row"><p><a href="{detail_urls[number - 1]}">'
            f'Program Analyst {number}</a></p><a href="/jobs/{number}'
            '/other-jobs-matching/location-only">'
            "Metro area</a></div>"
            for number in numbers
        )
        pages[url] = (
            '<script src="/job_list.js"></script><div class="jResultsContent" data-jsid="321">'
            + rows
            + '<div id="jPaginateNumPages">2.0</div>'
            + f'<div id="jPaginateCurrPage">{page_number}</div></div>'
        )
    pages.update(
        {url: _posting(f"Program Analyst {number}") for number, url in enumerate(detail_urls, 1)}
    )
    location_searches = {
        root_url + f"jobs/{number}/other-jobs-matching/location-only" for number in range(1, 16)
    }
    pages.update(
        dict.fromkeys(location_searches, "<main><h1>Other openings in this area</h1></main>")
    )
    if failed_detail:
        pages[detail_urls[11]] = httpx.HTTPStatusError(
            "detail unavailable",
            request=httpx.Request("GET", detail_urls[11]),
            response=httpx.Response(503),
        )
    client = _Client(pages)
    gateway = root_url + "jobs/search"
    pages[gateway] = httpx.Response(
        200, text=cast(str, pages[list_url]), request=httpx.Request("GET", list_url)
    )
    root = f'<div><a href="{gateway}"><img alt="View all jobs"></a></div>'
    root += "".join(
        f'<div class="job_list_row"><a href="{url}">Program Analyst {number}</a></div>'
        for number, url in enumerate(detail_urls[:2], 1)
    )

    result = PublisherSourceFetcher(client=cast(Any, client)).read(
        "Example Health",
        root_url,
        root,
        options=SourceReadOptions(protected_posting_urls=frozenset({detail_urls[2]})),
    )

    expected = set(detail_urls) - {detail_urls[2]}
    if failed_detail:
        expected.remove(detail_urls[11])
        assert detail_urls[11] in _deferred_urls(result)
    assert {listing.url for listing in result.listings} == expected
    assert detail_urls[2] not in client.requested
    assert not location_searches.intersection(client.requested)
    assert result.protected_posting_urls == (detail_urls[2],)
    assert result.raw_count == 15
    assert not result.complete


def test_repeated_generic_site_links_do_not_become_automatic_fetch_work() -> None:
    root = (
        "<main><ul>"
        + "".join(
            f'<li><a href="/articles/{number}">Health system update {number}</a></li>'
            for number in range(250)
        )
        + "</ul></main>"
    )
    first = "https://publisher.example/articles/0"
    client = _Client({first: "<main><h1>Health system update</h1></main>"})

    result = PublisherSourceFetcher(client=cast(Any, client)).read(
        "Example Health", "https://publisher.example/careers", root
    )

    assert client.requested == [first]
    assert len(result.deferred_targets) == 250
    assert all(not target.automatic_eligible for target in result.deferred_targets)
    assert {target.deferred_reason for target in result.deferred_targets} == {
        "classification_ambiguous",
        "shape_probe_pending",
    }
    assert result.skip_reasons["shape_probe_pending"] == 249


def test_repeated_opaque_posting_shape_expands_after_one_successful_probe() -> None:
    urls = tuple(f"https://publisher.example/{number}" for number in range(410, 413))
    root = (
        "<main><ul>"
        + "".join(
            f'<li><a href="/{number}">Clinical Role {number}</a></li>' for number in range(410, 413)
        )
        + "</ul></main>"
    )
    client = _Client({url: _posting(f"Clinical Role {url.rsplit('/', 1)[-1]}") for url in urls})

    result = PublisherSourceFetcher(client=cast(Any, client)).read(
        "Example Health", "https://publisher.example/careers", root
    )

    assert set(client.requested) == set(urls)
    assert {listing.url for listing in result.listings} == set(urls)
    assert result.deferred_targets == ()


def test_a_separate_detail_body_confirms_opaque_sibling_links_for_the_public_reader() -> None:
    roles = (
        ("copper-leaf", "Senior Graphic Designer"),
        ("silver-branch", "Partnerships Manager"),
    )
    urls = tuple(f"https://publisher.example/careers/roles/{slug}" for slug, _ in roles)
    rail = (
        "<ul>"
        + "".join(f'<li><a href="/careers/roles/{slug}">{title}</a></li>' for slug, title in roles)
        + "</ul>"
    )
    root = f"<h1>Careers at Northwind</h1>{rail}"
    sections = "".join(
        f"<h2>{name}</h2><p>"
        + "Collaborate with the team to create useful materials, review outcomes, "
        "and support project delivery across the organization. " * 4 + "</p>"
        for name in (
            "About the role",
            "Responsibilities",
            "Qualifications",
            "Requirements",
            "Benefits",
            "Compensation",
        )
    )
    pages = {
        url: f"<html><head><title>{title} | Northwind</title></head><body>"
        f'{rail}<div role="region"><h1>{title}</h1>{sections}</div>{rail}</body></html>'
        for url, (_, title) in zip(urls, roles, strict=True)
    }
    client = _Client(pages)

    result = PublisherSourceFetcher(client=cast(Any, client)).read(
        "Northwind", "https://publisher.example/careers", root
    )

    assert client.requested == list(urls)
    assert {(listing.title, listing.url) for listing in result.listings} == {
        (title, url) for url, (_, title) in zip(urls, roles, strict=True)
    }
    assert all(
        listing.content_status is PostingContentAccessStatus.READABLE for listing in result.listings
    )
    assert not result.complete
    assert result.deferred_targets == ()


@pytest.mark.parametrize("second_layout", ["qualified-self-link", "secondary-heading"])
def test_a_second_role_body_promotes_the_remaining_publisher_links(second_layout: str) -> None:
    roles = (
        ("copper-leaf", "Senior Graphic Designer"),
        (
            "silver-branch",
            "Partnerships Manager"
            if second_layout == "qualified-self-link"
            else "Customer Strategy & Operations",
        ),
        ("green-stone", "Program Coordinator"),
    )
    urls = tuple(f"https://publisher.example/careers/roles/{slug}" for slug, _ in roles)
    rail = (
        "<section>"
        + "".join(
            f'<a href="{url}">{title}'
            f"{
                ' (12-month contract)'
                if index == 1 and second_layout == 'qualified-self-link'
                else ' Full-Time See Job Details Full-Time'
            }"
            "</a>"
            for index, (url, (_, title)) in enumerate(zip(urls, roles, strict=True))
        )
        + "</section>"
    )
    sections = "".join(
        f"<strong>{name}</strong><p>"
        + "Plan the partner program, maintain its delivery calendar, coordinate team resources, "
        "and review results with project owners across the organization. " * 5 + "</p>"
        for name in (
            ("Job Summary", "Duties and Tasks", "Qualifications", "How to Apply")
            if second_layout == "qualified-self-link"
            else ("What you'll do", "About You", "How to Apply")
        )
    )
    pages = {url: _posting(title) for url, (_, title) in zip(urls, roles, strict=True)}
    second_title = roles[1][1]
    title_tag = "h1" if second_layout == "qualified-self-link" else "h3"
    pages[urls[1]] = (
        f"<html><head><title>{second_title} | Northwind</title></head><body>"
        f"{rail if second_layout == 'qualified-self-link' else ''}<div>"
        f"<{title_tag}>{second_title}</{title_tag}>{sections}</div></body></html>"
    )
    client = _Client(pages)

    result = PublisherSourceFetcher(client=cast(Any, client)).read(
        "Northwind", "https://publisher.example/careers", f"<h1>Careers at Northwind</h1>{rail}"
    )

    assert client.requested == list(urls)
    assert {(listing.title, listing.url) for listing in result.listings} == {
        (title, url) for url, (_, title) in zip(urls, roles, strict=True)
    }
    assert all(
        listing.content_status is PostingContentAccessStatus.READABLE for listing in result.listings
    )
    assert result.deferred_targets == ()


def test_heuristic_posting_shape_waits_for_a_corroborating_probe() -> None:
    urls = tuple(f"https://publisher.example/{number}" for number in range(510, 516))
    root = (
        "<main><ul>"
        + "".join(
            f'<li><a href="/{number}">Systems Analyst {number}</a></li>'
            for number in range(510, 516)
        )
        + "</ul></main>"
    )
    pages: dict[str, str] = {
        url: _posting(f"Systems Analyst {url.rsplit('/', 1)[-1]}") for url in urls
    }
    pages[urls[1]] = "<main><h1>Health system update</h1></main>"
    client = _Client(pages)

    result = PublisherSourceFetcher(client=cast(Any, client)).read(
        "Example Health", "https://publisher.example/careers", root
    )

    assert client.requested == [urls[0], urls[1]]
    assert {listing.url for listing in result.listings} == {urls[0]}
    assert (
        tuple(
            target.url
            for target in result.deferred_targets
            if target.deferred_reason == "shape_probe_pending"
        )
        == urls[2:]
    )


def test_declared_posting_shape_expands_after_one_successful_probe() -> None:
    urls = tuple(f"https://publisher.example/{number}" for number in range(610, 613))
    root = (
        "<main><ul>"
        + "".join(
            f'<li><a href="/{number}">Systems Analyst {number}</a></li>'
            for number in range(610, 613)
        )
        + "</ul></main>"
    )
    client = _Client(
        {
            url: _declared_posting(
                f"Systems Analyst {url.rsplit('/', 1)[-1]}", "Example Health", url
            )
            for url in urls
        }
    )

    result = PublisherSourceFetcher(client=cast(Any, client)).read(
        "Example Health", "https://publisher.example/careers", root
    )

    assert set(client.requested) == set(urls)
    assert {listing.url for listing in result.listings} == set(urls)
    assert result.deferred_targets == ()


def test_declared_probe_expands_a_shape_its_siblings_never_corroborate() -> None:
    urls = tuple(f"https://publisher.example/{number}" for number in range(710, 714))
    root = (
        "<main><ul>"
        + "".join(
            f'<li><a href="/{number}">Systems Analyst {number}</a></li>'
            for number in range(710, 714)
        )
        + "</ul></main>"
    )
    pages: dict[str, str] = dict.fromkeys(urls[1:], "<main><h1>Health system update</h1></main>")
    pages[urls[0]] = _declared_posting("Systems Analyst 710", "Example Health", urls[0])
    client = _Client(pages)

    result = PublisherSourceFetcher(client=cast(Any, client)).read(
        "Example Health", "https://publisher.example/careers", root
    )

    assert set(client.requested) == set(urls)
    assert {listing.url for listing in result.listings} == {urls[0]}


def test_external_search_careers_handoff_skips_outside_root_marketing_probes() -> None:
    root_url = "https://publisher.example/en-ca/careers/"
    handoff_url = "https://handoff.example/global/en"
    served_url = "https://jobs.example/global/en"
    opening_urls = tuple(
        f"https://jobs.example/global/en/job/{number}" for number in range(123, 126)
    )
    same_host_opening = "https://publisher.example/job/456"
    marketing_urls = tuple(
        f"https://publisher.example/en-ca/about-us/{number}" for number in range(3)
    )
    root = (
        """
    <main>
      <a href="https://handoff.example/global/en">Search careers</a>
      <a href="https://publisher.example/job/456">Clinical Director</a>
      <ul>
    """
        + "".join(
            f'<li><a href="/en-ca/about-us/{number}">About us {number}</a></li>'
            for number in range(3)
        )
        + """
      </ul>
    </main>
    """
    )
    handoff = (
        "<main><ul>"
        + "".join(
            f'<li><a href="/global/en/job/{number}">Program Lead {number}</a></li>'
            for number in range(123, 126)
        )
        + "</ul></main>"
    )
    client = _Client(
        {
            handoff_url: httpx.Response(
                200,
                text=handoff,
                request=httpx.Request("GET", served_url),
            ),
            **{url: _posting(f"Program Lead {url.rsplit('/', 1)[-1]}") for url in opening_urls},
            same_host_opening: _posting("Clinical Director"),
        }
    )

    result = PublisherSourceFetcher(client=cast(Any, client)).read("Example Health", root_url, root)

    assert {listing.url for listing in result.listings} == {*opening_urls, same_host_opening}
    assert handoff_url in client.requested
    assert not set(marketing_urls) & set(client.requested)
    assert not set(marketing_urls) & set(_deferred_urls(result))
    assert result.raw_count == 4
    assert result.skip_reasons["outside_root_directory"] == len(marketing_urls)


def test_external_search_careers_handoff_keeps_opaque_probes_inside_the_root_directory() -> None:
    root_url = "https://publisher.example/en-ca/careers"
    opaque_urls = tuple(
        f"https://publisher.example/en-ca/careers/{number}" for number in range(410, 413)
    )
    root = (
        '<main><a href="https://handoff.example/openings">Search careers</a><ul>'
        + "".join(
            f'<li><a href="/en-ca/careers/{number}">Clinical Role {number}</a></li>'
            for number in range(410, 413)
        )
        + "</ul></main>"
    )
    client = _Client(
        {
            "https://handoff.example/openings": "<main></main>",
            **{url: _posting(f"Clinical Role {url.rsplit('/', 1)[-1]}") for url in opaque_urls},
        }
    )

    result = PublisherSourceFetcher(client=cast(Any, client)).read("Example Health", root_url, root)

    assert set(client.requested) == set(opaque_urls) | {"https://handoff.example/openings"}
    assert {listing.url for listing in result.listings} == set(opaque_urls)


def _link_list(links: tuple[tuple[str, str], ...]) -> str:
    items = "".join(f'<li><a href="{href}">{label}</a></li>' for href, label in links)
    return f"<main><ul>{items}</ul></main>"


def test_a_careers_root_without_a_handoff_excludes_the_corporate_site_outside_it() -> None:
    corporate = (
        ("/en-ca/services/x", "Consulting services"),
        ("/en-ca/about-us/leadership/a", "Alex Example, Chief Executive"),
        ("/global/en/about-us/report", "Annual report"),
        *((f"/en-ca/partners/{number}", f"Partner organization {number}") for number in (1, 2, 3)),
    )
    client = _Client({})

    result = PublisherSourceFetcher(client=cast(Any, client)).read(
        "Example Health", "https://publisher.example/en-ca/en-us/careers", _link_list(corporate)
    )

    assert client.requested == []
    assert result.deferred_targets == ()
    assert result.skip_reasons["outside_root_directory"] == len(corporate)
    assert result.outcome is not SourceReadOutcome.CRAWL_CONTINUATION


def test_a_careers_root_still_reads_a_same_host_posting_outside_its_directory() -> None:
    url = "https://publisher.example/job/12345"
    client = _Client({url: _posting("Logistics Director")})

    result = PublisherSourceFetcher(client=cast(Any, client)).read(
        "Example Health",
        "https://publisher.example/en/careers",
        _link_list((("/job/12345", "Logistics Director"),)),
    )

    assert [listing.url for listing in result.listings] == [url]
    assert "outside_root_directory" not in result.skip_reasons


@pytest.mark.parametrize("gateway_path", ["/en/job-search", "/en/search"])
def test_a_same_host_job_gateway_keeps_its_opaque_children_readable(gateway_path: str) -> None:
    gateway_url = f"https://publisher.example{gateway_path}"
    opening_urls = tuple(f"{gateway_url}/{number}" for number in (1, 2, 3))
    gateway = _link_list(
        tuple((f"{gateway_path}/{number}", f"Logistics Role {number}") for number in (1, 2, 3))
    )
    client = _Client(
        {
            gateway_url: gateway,
            **{url: _posting(f"Logistics Role {url.rsplit('/', 1)[-1]}") for url in opening_urls},
        }
    )

    result = PublisherSourceFetcher(client=cast(Any, client)).read(
        "Example Health",
        "https://publisher.example/en/careers/",
        _link_list(((gateway_path, "Search jobs"),)),
    )

    assert {listing.url for listing in result.listings} == set(opening_urls)
    assert result.deferred_targets == ()


@pytest.mark.parametrize("gateway_first", [True, False])
def test_a_same_host_job_gateway_widens_scope_wherever_it_sits_on_the_page(
    gateway_first: bool,
) -> None:
    team_url = "https://publisher.example/en/search/team"
    links = (("/en/search", "Search jobs"), ("/en/search/team", "Our recruiting team"))
    client = _Client({"https://publisher.example/en/search": "<main></main>"})

    result = PublisherSourceFetcher(client=cast(Any, client)).read(
        "Example Health",
        "https://publisher.example/en/careers/",
        _link_list(links if gateway_first else links[::-1]),
    )

    assert team_url in _deferred_urls(result)
    assert "outside_root_directory" not in result.skip_reasons


@pytest.mark.parametrize("narrow_first", [True, False])
def test_nested_job_gateways_admit_the_same_scope_in_either_order(narrow_first: bool) -> None:
    gateways = (("/x/careers/search", "Search careers"), ("/x/careers", "Careers"))
    opening_urls = tuple(f"https://publisher.example/x/careers/r/{n}" for n in (101, 102, 103))
    broad = _link_list(tuple((f"/x/careers/r/{n}", f"Logistics Role {n}") for n in (101, 102, 103)))
    client = _Client(
        {
            "https://publisher.example/x/careers/search": "<main></main>",
            "https://publisher.example/x/careers": broad,
            **{url: _posting(f"Logistics Role {url.rsplit('/', 1)[-1]}") for url in opening_urls},
        }
    )

    result = PublisherSourceFetcher(client=cast(Any, client)).read(
        "Example Health",
        "https://publisher.example/en/careers/",
        _link_list(gateways if narrow_first else gateways[::-1]),
    )

    assert {listing.url for listing in result.listings} == set(opening_urls)
    assert "outside_root_directory" not in result.skip_reasons


def test_a_job_gateway_above_the_root_directory_does_not_unbound_the_site() -> None:
    teams = ("/en/teams/x", "/en/teams/y")
    gateway = _link_list(tuple((path, f"Our logistics team {path[-1]}") for path in teams))
    client = _Client({"https://publisher.example/en": gateway})

    result = PublisherSourceFetcher(client=cast(Any, client)).read(
        "Example Health",
        "https://publisher.example/en/careers/",
        _link_list((("/en", "Jobs"),)),
    )

    team_urls = {f"https://publisher.example{path}" for path in teams}
    assert client.requested == ["https://publisher.example/en"]
    assert not team_urls & set(_deferred_urls(result))
    assert result.skip_reasons["outside_root_directory"] == len(teams)


@pytest.mark.parametrize(
    ("root_path", "sibling_prefix"), [("/join-us/open-roles", "/join-us/"), ("/careers.html", "/")]
)
def test_a_leaf_root_still_reads_opaque_postings_beside_it(
    root_path: str, sibling_prefix: str
) -> None:
    opening_urls = tuple(
        f"https://publisher.example{sibling_prefix}{number}" for number in range(410, 413)
    )
    client = _Client(
        {url: _posting(f"Logistics Role {url.rsplit('/', 1)[-1]}") for url in opening_urls}
    )

    result = PublisherSourceFetcher(client=cast(Any, client)).read(
        "Example Health",
        f"https://publisher.example{root_path}",
        _link_list(
            tuple(
                (f"{sibling_prefix}{number}", f"Logistics Role {number}")
                for number in range(410, 413)
            )
        ),
    )

    assert {listing.url for listing in result.listings} == set(opening_urls)


def test_a_careers_root_without_a_handoff_still_probes_opaque_links_inside_it() -> None:
    opaque_urls = tuple(
        f"https://publisher.example/en/careers/{number}" for number in range(410, 413)
    )
    client = _Client(
        {url: _posting(f"Logistics Role {url.rsplit('/', 1)[-1]}") for url in opaque_urls}
    )

    result = PublisherSourceFetcher(client=cast(Any, client)).read(
        "Example Health",
        "https://publisher.example/en/careers",
        _link_list(
            tuple(
                (f"/en/careers/{number}", f"Logistics Role {number}") for number in range(410, 413)
            )
        ),
    )

    assert set(client.requested) == set(opaque_urls)
    assert {listing.url for listing in result.listings} == set(opaque_urls)


def test_broad_career_and_role_urls_are_not_assumed_to_be_postings() -> None:
    root = """
    <main>
      <a href="/careers/culture">Our culture</a>
      <a href="/people?role=nurse">Meet a nurse</a>
    </main>
    """
    client = _Client({})

    result = PublisherSourceFetcher(client=cast(Any, client)).read(
        "Example Health", "https://publisher.example/careers", root
    )

    assert client.requested == []
    assert result.listings == []
    assert set(_deferred_urls(result)) == {
        "https://publisher.example/careers/culture",
        "https://publisher.example/people?role=nurse",
    }


def test_parent_board_keeps_each_publishers_declared_employer() -> None:
    root = """
    <main><ul>
      <li><a href="/careers/jobs/one">Child Health Role</a></li>
      <li><a href="/careers/jobs/two">Parent Engineering Role</a></li>
    </ul></main>
    """
    first = "https://parent.example/careers/jobs/one"
    second = "https://parent.example/careers/jobs/two"
    client = _Client(
        {
            first: _declared_posting("Child Health Role", "Child Health", first),
            second: _declared_posting("Parent Engineering Role", "Parent Industries", second),
        }
    )

    result = PublisherSourceFetcher(client=cast(Any, client)).read(
        "Child Health", "https://parent.example/careers", root
    )

    assert {listing.title: listing.employer for listing in result.listings} == {
        "Child Health Role": "Child Health",
        "Parent Engineering Role": "Parent Industries",
    }


def test_linked_documents_are_kept_as_manual_frontier_work_not_fetched() -> None:
    root = """
    <main><ul>
      <li><a href="/jobs/files/benefits-guide.pdf">Benefits guide</a></li>
      <li><a href="/jobs/files/role-profile.docx">Role profile</a></li>
      <li><a href="/en/job/clinical-director/123">Clinical Director</a></li>
    </ul></main>
    """
    posting_url = "https://jobs.example/en/job/clinical-director/123"
    client = _Client({posting_url: _posting("Clinical Director")})

    result = PublisherSourceFetcher(client=cast(Any, client)).read(
        "Example Health", "https://jobs.example/en/careers", root
    )

    assert client.requested == [posting_url]
    assert [(listing.title, listing.url) for listing in result.listings] == [
        ("Clinical Director", posting_url)
    ]
    assert result.skip_reasons["non_html_document"] == 2
    assert {target.url for target in result.deferred_targets} == {
        "https://jobs.example/jobs/files/benefits-guide.pdf",
        "https://jobs.example/jobs/files/role-profile.docx",
    }
    assert all(not target.automatic_eligible for target in result.deferred_targets)


def test_declared_posting_on_a_navigation_shaped_root_is_retained() -> None:
    url = "https://jobs.example/en/category/clinical-director/733/123/1"

    result = PublisherSourceFetcher(client=cast(Any, _Client({}))).read(
        "Example Health", url, _declared_posting("Clinical Director", "Example Health", url)
    )

    assert [(listing.title, listing.url) for listing in result.listings] == [
        ("Clinical Director", url)
    ]
    assert "non_posting_page" not in result.skip_reasons


def test_plain_posting_on_a_navigation_shaped_root_is_retained() -> None:
    url = "https://jobs.example/en/category/clinical-director/733/123/1"

    result = PublisherSourceFetcher(client=cast(Any, _Client({}))).read(
        "Example Health", url, _posting("Clinical Director")
    )

    assert [(listing.title, listing.url) for listing in result.listings] == [
        ("Clinical Director", url)
    ]
    assert "non_posting_page" not in result.skip_reasons


def test_a_publisher_page_title_is_read_with_its_entities_decoded() -> None:
    url = "https://jobs.example/en/category/platform-lead/733/123/1"

    result = PublisherSourceFetcher(client=cast(Any, _Client({}))).read(
        "Example Health", url, _posting("Platform Reliability &amp; Data Lead")
    )

    assert [listing.title for listing in result.listings] == ["Platform Reliability & Data Lead"]


def test_crawled_posting_holds_a_configured_employer_as_an_unverified_hint() -> None:
    root = """
    <main><ul><li><a href="/en/job/platform-director/123">Platform Director</a></li></ul></main>
    """
    posting_url = "https://jobs.example/en/job/platform-director/123"
    client = _Client({posting_url: _posting("Platform Director")})

    result = PublisherSourceFetcher(client=cast(Any, client)).read(
        "Example Health", "https://jobs.example/en/careers", root
    )

    assert [(listing.employer, listing.facts.identity_trust) for listing in result.listings] == [
        ("Example Health", ListingIdentityTrust.UNVERIFIED_HINT)
    ]


def test_crawled_posting_keeps_publisher_trust_for_a_person_named_employer() -> None:
    root = """
    <main><ul><li><a href="/en/job/platform-director/123">Platform Director</a></li></ul></main>
    """
    posting_url = "https://jobs.example/en/job/platform-director/123"
    client = _Client({posting_url: _posting("Platform Director")})

    result = PublisherSourceFetcher(client=cast(Any, client)).read(
        "Example Health",
        "https://jobs.example/en/careers",
        root,
        employer_is_authoritative=True,
    )

    assert [(listing.employer, listing.facts.identity_trust) for listing in result.listings] == [
        ("Example Health", ListingIdentityTrust.PUBLISHER)
    ]


def test_root_page_posting_holds_a_configured_employer_as_an_unverified_hint() -> None:
    url = "https://jobs.example/en/category/platform-director/733/123/1"

    result = PublisherSourceFetcher(client=cast(Any, _Client({}))).read(
        "Example Health", url, _posting("Platform Director")
    )

    assert [listing.facts.identity_trust for listing in result.listings] == [
        ListingIdentityTrust.UNVERIFIED_HINT
    ]


def test_generic_category_and_employment_routes_can_be_posting_pages() -> None:
    urls = (
        "https://publisher.example/jobs/category/clinical-director",
        "https://publisher.example/jobs/employment/program-lead",
    )
    root = """
    <main><ul>
      <li><a href="/jobs/category/clinical-director">Clinical Director</a></li>
      <li><a href="/jobs/employment/program-lead">Program Lead</a></li>
    </ul></main>
    """
    client = _Client({urls[0]: _posting("Clinical Director"), urls[1]: _posting("Program Lead")})

    result = PublisherSourceFetcher(client=cast(Any, client)).read(
        "Example Health", "https://publisher.example/careers", root
    )

    assert set(client.requested) == set(urls)
    assert {(listing.title, listing.url) for listing in result.listings} == {
        ("Clinical Director", urls[0]),
        ("Program Lead", urls[1]),
    }


def test_external_publisher_handoff_keeps_blocked_links_out_of_the_listing_queue() -> None:
    root = """
    <main><p>Current opportunities are hosted by our recruiting partner.</p>
    <a href="https://portal.example/list">View opportunities</a></main>
    """
    index = """
    <main><table>
      <tr><td><a href="/detail?role=41">Clinical Data Lead</a></td></tr>
      <tr><td><a href="/detail?role=42">Program Director</a></td></tr>
    </table></main>
    """
    blocked = "<title>Unavailable</title><main><h1>We apologize</h1><p>Error</p></main>"
    client = _Client(
        {
            "https://portal.example/list": index,
            "https://portal.example/detail?role=41": blocked,
            "https://portal.example/detail?role=42": blocked,
        }
    )
    fetcher = PublisherSourceFetcher(client=cast(Any, client))

    result = fetcher.read("Example Institute", "https://institute.example/careers", root)

    assert result.listings == []
    assert result.raw_count == 2
    assert result.outcome is SourceReadOutcome.CRAWL_CONTINUATION
    assert _deferred_urls(result) == (
        "https://portal.example/detail?role=41",
        "https://portal.example/detail?role=42",
    )


def test_javascript_shell_is_not_reported_as_an_empty_source() -> None:
    client = _Client({})
    fetcher = PublisherSourceFetcher(client=cast(Any, client))

    result = fetcher.read(
        "Example Startup",
        "https://startup.example/careers",
        '<div id="root"></div><script src="/assets/jobs.js"></script>',
    )

    assert result.listings == []
    assert not result.complete
    assert result.outcome is SourceReadOutcome.RENDER_REQUIRED
    assert "js_shell" in result.note


def test_page_limit_returns_the_exact_unvisited_frontier() -> None:
    root = """
    <main><ul><li><a href="/roles/one">Role One</a></li>
    <li><a href="/roles/two">Role Two</a></li></ul></main>
    """
    client = _Client(
        {
            "https://publisher.example/roles/one": _posting("Role One"),
            "https://publisher.example/roles/two": _posting("Role Two"),
        }
    )
    fetcher = PublisherSourceFetcher(
        client=cast(Any, client),
        limits=PublisherReadLimits(max_pages=1),
    )

    result = fetcher.read("Example Health", "https://publisher.example/careers", root)

    assert [(item.title, item.content_status) for item in result.listings] == [
        ("Role One", PostingContentAccessStatus.READABLE),
    ]
    assert _deferred_urls(result) == ("https://publisher.example/roles/two",)
    assert result.deferred_targets[0].label == "Role Two"
    assert result.skip_reasons["page_limit_reached"] == 1
    assert result.outcome is SourceReadOutcome.CRAWL_CONTINUATION


def test_time_limit_returns_the_exact_unvisited_frontier(monkeypatch: Any) -> None:
    root = """
    <main><ul><li><a href="/roles/one">Role One</a></li>
    <li><a href="/roles/two">Role Two</a></li></ul></main>
    """
    now = [0.0]

    class TimedClient(_Client):
        def get(self, url: str, **kwargs: Any) -> httpx.Response:
            response = super().get(url, **kwargs)
            now[0] = 31.0
            return response

    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.publisher_sources.time.monotonic",
        lambda: now[0],
    )
    client = TimedClient(
        {
            "https://publisher.example/roles/one": _posting("Role One"),
            "https://publisher.example/roles/two": _posting("Role Two"),
        }
    )
    fetcher = PublisherSourceFetcher(
        client=cast(Any, client),
        limits=PublisherReadLimits(max_elapsed_seconds=30.0),
    )

    result = fetcher.read("Example Health", "https://publisher.example/careers", root)

    assert [(item.title, item.content_status) for item in result.listings] == [
        ("Role One", PostingContentAccessStatus.READABLE),
    ]
    assert client.requested == ["https://publisher.example/roles/one"]
    assert _deferred_urls(result) == ("https://publisher.example/roles/two",)
    assert result.skip_reasons["time_limit_reached"] == 1


def test_ambiguous_publisher_opening_remains_only_in_the_durable_frontier() -> None:
    root = '<main><a href="/roles/only">Only Role</a></main>'
    ambiguous = "<main><h1>Only Role</h1><p>Brief publisher description.</p></main>"
    client = _Client({"https://publisher.example/roles/only": ambiguous})
    fetcher = PublisherSourceFetcher(client=cast(Any, client))

    result = fetcher.read("Example Health", "https://publisher.example/careers", root)

    assert result.listings == []
    assert _deferred_urls(result) == ("https://publisher.example/roles/only",)
    assert result.outcome is SourceReadOutcome.CRAWL_CONTINUATION


def test_failed_unique_publisher_opening_remains_only_in_the_durable_frontier() -> None:
    root = '<main><a href="/roles/only">Only Role</a></main>'
    url = "https://publisher.example/roles/only"
    client = _Client({url: httpx.ConnectError("publisher unavailable")})
    fetcher = PublisherSourceFetcher(client=cast(Any, client))

    result = fetcher.read("Example Health", "https://publisher.example/careers", root)

    assert result.listings == []
    assert _deferred_urls(result) == (url,)


def test_stable_http_failure_routes_directly_to_manual_recovery() -> None:
    root = '<main><a href="/roles/only">Only Role</a></main>'
    url = "https://publisher.example/roles/only"
    request = httpx.Request("GET", url)
    response = httpx.Response(404, request=request)
    failure = httpx.HTTPStatusError("not found", request=request, response=response)

    result = PublisherSourceFetcher(client=cast(Any, _Client({url: failure}))).read(
        "Example Health", "https://publisher.example/careers", root
    )

    [target] = result.deferred_targets
    assert target.automatic_eligible is False
    assert target.deferred_reason == "stable_http_404"


def test_depth_limit_defers_only_links_not_already_visited_or_queued() -> None:
    root = '<main><a href="/roles/collection">View roles</a></main>'
    collection = """
    <main><ul>
      <li><a href="/roles/collection">Collection</a></li>
      <li><a href="/roles/new">New Role</a></li>
    </ul></main>
    """
    client = _Client(
        {
            "https://publisher.example/roles/collection": collection,
            "https://publisher.example/roles/new": _posting("New Role"),
        }
    )
    fetcher = PublisherSourceFetcher(
        client=cast(Any, client),
        limits=PublisherReadLimits(max_depth=1),
    )

    result = fetcher.read("Example Health", "https://publisher.example/careers", root)

    assert _deferred_urls(result) == ()
    assert [item.title for item in result.listings] == ["New Role"]
    assert result.skip_reasons.get("depth_limit_reached", 0) == 0


def test_deferred_link_labels_are_bounded_for_durable_frontier() -> None:
    long_label = "Role " + ("with an unusually verbose publisher label " * 20)
    root = f'<main><a href="/roles/one">{long_label}</a></main>'
    result = PublisherSourceFetcher(
        client=cast(Any, _Client({"https://publisher.example/roles/one": "<main></main>"})),
        limits=PublisherReadLimits(max_pages=0),
    ).read("Example Health", "https://publisher.example/careers", root)

    [target] = result.deferred_targets
    assert len(target.label) <= 300
    assert target.label.startswith("Role with an unusually verbose")


def test_failed_continuation_retains_the_publishers_exact_frontier_identity() -> None:
    root = """
    <main><ul><li><a href="/roles/one">Role One</a></li>
    <li><a href="/roles/two">Role Two</a></li></ul></main>
    """
    url = "https://publisher.example/roles/two"
    initial_client = _Client(
        {
            "https://publisher.example/roles/one": _posting("Role One"),
            url: _posting("Role Two"),
        }
    )
    initial = PublisherSourceFetcher(
        client=cast(Any, initial_client), limits=PublisherReadLimits(max_pages=1)
    ).read("Example Health", "https://publisher.example/careers", root)
    failed = PublisherSourceFetcher(
        client=cast(Any, _Client({url: httpx.ConnectError("unavailable")}))
    ).continue_listings(
        "Example Health",
        initial.deferred_targets,
        root_url="https://publisher.example/careers",
        handoff_scopes=initial.handoff_scopes,
    )

    assert failed.listings == []
    assert len(failed.deferred_targets) == 1
    [retry] = failed.deferred_targets
    [original] = initial.deferred_targets
    assert retry.url == original.url
    assert retry.label == original.label
    assert retry.relations == original.relations
    assert retry.automatic_eligible is True
    assert retry.deferred_reason == "transient_network_error"
    assert failed.attempted_target_urls == (url,)


def test_continuation_reports_only_targets_visited_before_its_deadline(
    monkeypatch: Any,
) -> None:
    root = """
    <main><ul><li><a href="/roles/one">Role One</a></li>
    <li><a href="/roles/two">Role Two</a></li></ul></main>
    """
    first_url = "https://publisher.example/roles/one"
    second_url = "https://publisher.example/roles/two"
    initial = PublisherSourceFetcher(
        client=cast(Any, _Client({})), limits=PublisherReadLimits(max_pages=0)
    ).read("Example Health", "https://publisher.example/careers", root)
    now = [0.0]

    class TimedClient(_Client):
        def get(self, url: str, **kwargs: Any) -> httpx.Response:
            response = super().get(url, **kwargs)
            now[0] = 31.0
            return response

    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.publisher_sources.time.monotonic",
        lambda: now[0],
    )
    continued = PublisherSourceFetcher(
        client=cast(
            Any,
            TimedClient(
                {
                    first_url: _posting("Role One"),
                    second_url: _posting("Role Two"),
                }
            ),
        ),
        limits=PublisherReadLimits(max_elapsed_seconds=30.0),
    ).continue_listings(
        "Example Health",
        initial.deferred_targets,
        root_url="https://publisher.example/careers",
        handoff_scopes=initial.handoff_scopes,
    )

    assert continued.attempted_target_urls == (first_url,)
    assert _deferred_urls(continued) == (second_url,)


def test_continuation_preserves_a_redirected_external_gateway_scope() -> None:
    root_url = "https://publisher.example/en-ca/careers"
    gateway_url = "https://handoff.example/global/en"
    served_url = "https://jobs.example/global/en"
    opening_urls = tuple(
        f"https://jobs.example/global/en/job/{number}" for number in range(123, 126)
    )
    root = '<main><a href="https://handoff.example/global/en">Search careers</a></main>'
    handoff = (
        "<main><ul>"
        + "".join(
            f'<li><a href="/global/en/job/{number}">Program Lead {number}</a></li>'
            for number in range(123, 126)
        )
        + "</ul></main>"
    )
    initial = PublisherSourceFetcher(
        client=cast(Any, _Client({})), limits=PublisherReadLimits(max_pages=0)
    ).read("Example Health", root_url, root)
    continued = PublisherSourceFetcher(
        client=cast(
            Any,
            _Client(
                {
                    gateway_url: httpx.Response(
                        200,
                        text=handoff,
                        request=httpx.Request("GET", served_url),
                    )
                }
            ),
        ),
        limits=PublisherReadLimits(max_pages=1),
    ).continue_listings(
        "Example Health",
        initial.deferred_targets,
        root_url=root_url,
        handoff_scopes=initial.handoff_scopes,
    )
    completed = PublisherSourceFetcher(
        client=cast(
            Any,
            _Client(
                {url: _posting(f"Program Lead {url.rsplit('/', 1)[-1]}") for url in opening_urls}
            ),
        )
    ).continue_listings(
        "Example Health",
        continued.deferred_targets,
        root_url=root_url,
        handoff_scopes=continued.handoff_scopes,
    )

    assert _deferred_urls(initial) == (gateway_url,)
    assert set(_deferred_urls(continued)) == set(opening_urls)
    assert ("jobs.example", "directory", "/global/en/") in continued.handoff_scopes
    assert {listing.url for listing in completed.listings} == set(opening_urls)


def test_partner_page_does_not_admit_a_second_external_hop() -> None:
    root = (
        "<main><p>Openings are hosted by our partner.</p>"
        '<a href="https://partner.example/list">Openings</a></main>'
    )
    partner = """
    <main><ul>
      <li><a href="/roles/one">Role One</a></li>
      <li><a href="/roles/two">Role Two</a></li>
      <li><a href="https://other.example/roles/other">Other Role</a></li>
      <li><a href="https://third.example/careers">Third-party careers</a></li>
    </ul></main>
    """
    client = _Client(
        {
            "https://partner.example/list": partner,
            "https://partner.example/roles/one": _posting("Role One"),
            "https://partner.example/roles/two": _posting("Role Two"),
        }
    )
    fetcher = PublisherSourceFetcher(client=cast(Any, client))

    result = fetcher.read("Example Health", "https://institute.example/careers", root)

    admitted = {item.url for item in result.listings}
    deferred = set(_deferred_urls(result))
    partner_roles = {
        "https://partner.example/roles/one",
        "https://partner.example/roles/two",
    }
    assert partner_roles <= admitted | deferred
    assert admitted | deferred <= partner_roles
    assert not any(url.startswith("https://other.example") for url in admitted | deferred)
    assert not any(url.startswith("https://third.example") for url in admitted | deferred)
    assert result.skip_reasons.get("off_host_link", 0) == 2


def test_off_host_extensions_at_depth_limit_are_refused_and_counted() -> None:
    root = '<main><a href="https://partner.example/index">Openings</a></main>'
    index = """
    <main><ul>
      <li><a href="/roles/one">Role One</a></li>
      <li><a href="/roles/two">Role Two</a></li>
      <li><a href="https://elsewhere.example/careers">Elsewhere</a></li>
    </ul></main>
    """
    client = _Client(
        {
            "https://partner.example/index": index,
            "https://partner.example/roles/one": _posting("Role One"),
            "https://partner.example/roles/two": _posting("Role Two"),
        }
    )
    fetcher = PublisherSourceFetcher(
        client=cast(Any, client),
        limits=PublisherReadLimits(max_depth=1),
    )

    result = fetcher.read("Example Health", "https://institute.example/careers", root)

    assert _deferred_urls(result) == ()
    assert {item.title for item in result.listings} == {"Role One", "Role Two"}
    assert result.skip_reasons.get("off_host_link", 0) == 1


def test_same_host_frontier_overflow_is_deferred_without_losing_links() -> None:
    root = '<main><a href="https://pub.example/index">Openings</a></main>'
    index = (
        "<main><ul>"
        + "".join(f'<li><a href="/roles/{n}">Role {n}</a></li>' for n in range(1, 6))
        + "</ul></main>"
    )
    client = _Client(
        {
            "https://pub.example/index": index,
            "https://pub.example/roles/contact-centre-manager": _posting("Contact Centre Manager"),
        }
    )
    fetcher = PublisherSourceFetcher(
        client=cast(Any, client),
        limits=PublisherReadLimits(max_pages=1, max_depth=2, max_frontier_items=3),
    )

    result = fetcher.read("Example Health", "https://pub.example/careers", root)

    assert len(_deferred_urls(result)) == 5
    assert result.skip_reasons.get("frontier_deferred", 0) == 2


def test_exact_navigation_links_are_retained_at_the_back_of_the_frontier() -> None:
    root = '<main><a href="https://pub.example/index">Openings</a></main>'
    index = """
    <main><ul>
      <li><a href="/about"> About   Us </a></li>
      <li><a href="/contact">Contact Us</a></li>
      <li><a href="/roles/contact-centre-manager">Contact Centre Manager</a></li>
    </ul></main>
    """
    client = _Client(
        {
            "https://pub.example/index": index,
            "https://pub.example/roles/contact-centre-manager": _posting("Contact Centre Manager"),
        }
    )
    fetcher = PublisherSourceFetcher(
        client=cast(Any, client), limits=PublisherReadLimits(max_depth=1)
    )

    result = fetcher.read("Example Health", "https://pub.example/careers", root)

    assert _deferred_urls(result) == (
        "https://pub.example/about",
        "https://pub.example/contact",
    )
    assert [target.automatic_eligible for target in result.deferred_targets] == [
        False,
        False,
    ]
    assert [target.deferred_reason for target in result.deferred_targets] == [
        "low_priority_navigation",
        "low_priority_navigation",
    ]
    assert result.skip_reasons.get("low_priority_navigation", 0) == 2
    assert [item.title for item in result.listings] == ["Contact Centre Manager"]


def test_repeated_off_host_links_are_counted_once() -> None:
    root = '<main><a href="https://pub.example/index">Openings</a></main>'
    index = """
    <main><ul>
      <li><a href="/roles/one">Role One</a></li>
      <li><a href="/roles/two">Role Two</a></li>
      <li><a href="https://elsewhere.example/x">Elsewhere</a></li>
      <li><a href="https://elsewhere.example/x">Elsewhere again</a></li>
      <li><a href="https://elsewhere.example/x">Elsewhere thrice</a></li>
    </ul></main>
    """
    client = _Client(
        {
            "https://pub.example/index": index,
            "https://pub.example/roles/one": _posting("Role One"),
            "https://pub.example/roles/two": _posting("Role Two"),
        }
    )
    fetcher = PublisherSourceFetcher(
        client=cast(Any, client), limits=PublisherReadLimits(max_depth=1)
    )

    result = fetcher.read("Example Health", "https://pub.example/careers", root)

    assert result.skip_reasons.get("off_host_link", 0) == 1


def test_a_crawl_from_a_careers_root_does_not_wander_into_the_employer_general_site() -> None:
    root = '<main><a href="https://www.example-health.org/careers/">Careers</a></main>'
    hub = """
    <main><ul>
      <li><a href="/careers/opening-1">Opening One</a></li>
      <li><a href="/info/patient-resources">Patient Resources</a></li>
      <li><a href="/info/visitor-guide">Visitor Guide</a></li>
    </ul></main>
    """
    client = _Client({"https://www.example-health.org/careers/": hub})
    fetcher = PublisherSourceFetcher(client=cast(Any, client))

    result = fetcher.read("Example Health", "https://careers.example-health.org/careers", root)

    in_scope = set(_deferred_urls(result)) | {item.url for item in result.listings}
    assert "https://www.example-health.org/careers/opening-1" in in_scope
    assert not any("/info/" in url for url in in_scope)
    assert result.skip_reasons.get("off_host_link", 0) == 2


def test_a_handoff_link_without_a_trailing_slash_still_scopes_to_its_own_directory() -> None:
    root = '<main><a href="https://www.example-health.org/careers">Careers</a></main>'
    hub = """
    <main><ul>
      <li><a href="/careers/opening-1">Opening One</a></li>
      <li><a href="/info/patient-resources">Patient Resources</a></li>
      <li><a href="/info/visitor-guide">Visitor Guide</a></li>
    </ul></main>
    """
    client = _Client({"https://www.example-health.org/careers": hub})
    fetcher = PublisherSourceFetcher(client=cast(Any, client))

    result = fetcher.read("Example Health", "https://careers.example-health.org/careers", root)

    in_scope = set(_deferred_urls(result)) | {item.url for item in result.listings}
    assert "https://www.example-health.org/careers/opening-1" in in_scope
    assert not any("/info/" in url for url in in_scope)
    assert result.skip_reasons.get("off_host_link", 0) == 2


def test_a_shared_ats_tenant_link_does_not_admit_a_different_tenant() -> None:
    root = '<main><a href="https://jobs.lever.co/acme">Careers</a></main>'
    hub = """
    <main><ul>
      <li><a href="/acme/opening-1">Opening One</a></li>
      <li><a href="/beta/opening-1">Other tenant opening</a></li>
      <li><a href="/beta/opening-2">Other tenant opening two</a></li>
    </ul></main>
    """
    client = _Client({"https://jobs.lever.co/acme": hub})
    fetcher = PublisherSourceFetcher(client=cast(Any, client))

    result = fetcher.read("Example Health", "https://careers.example.org/careers", root)

    in_scope = set(_deferred_urls(result)) | {item.url for item in result.listings}
    assert "https://jobs.lever.co/acme/opening-1" in in_scope
    assert not any("/beta/" in url for url in in_scope)
    assert result.skip_reasons.get("off_host_link", 0) == 2


@pytest.mark.parametrize("redirect", [False, True])
def test_an_individual_ats_referral_cannot_expand_into_a_partner_board(redirect: bool) -> None:
    detail = "https://acme.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/4127"
    landed = detail.replace("acme.", "globex.") if redirect else detail
    sibling = landed.replace("/job/4127", "/job/4407")
    hub = (
        f'<main><h1>Open positions</h1><ul><li><a href="{sibling}">Data Platform Lead</a></li>'
        f'<li><a href="{sibling}2">Operations Lead</a></li>'
        f'<li><a href="{sibling}3">Research Lead</a></li></ul></main>'
    )
    page = httpx.Response(200, text=hub, request=httpx.Request("GET", landed))
    client = _Client(
        {
            detail: page,
            sibling: _posting("Data Platform Lead"),
            sibling + "2": _posting("Operations Lead"),
            sibling + "3": _posting("Research Lead"),
        }
    )
    fetcher = PublisherSourceFetcher(client=cast(Any, client))
    result = fetcher.read(
        "Example Health",
        "https://careers.example.org/careers",
        f'<main><a href="{detail}">Careers</a></main>',
    )
    assert client.requested == [detail]
    assert sibling not in _deferred_urls(result)
    assert result.listings == []
    assert result.complete is False


def test_a_bare_workable_short_link_does_not_admit_a_different_tenants_short_link() -> None:
    root = '<main><a href="https://apply.workable.com/j/1A2B3C4D5E">Careers</a></main>'
    hub = """
    <main><ul>
      <li><a href="/j/1A2B3C4D5E/role-a">Role A</a></li>
      <li><a href="/j/1A2B3C4D5E/role-b">Role B</a></li>
      <li><a href="/j/9Z8Y7X6W5V">Other tenant opening</a></li>
    </ul></main>
    """
    client = _Client(
        {
            "https://apply.workable.com/j/1A2B3C4D5E": hub,
            "https://apply.workable.com/j/1A2B3C4D5E/role-a": _posting("Role A"),
            "https://apply.workable.com/j/1A2B3C4D5E/role-b": _posting("Role B"),
        }
    )
    fetcher = PublisherSourceFetcher(client=cast(Any, client))

    result = fetcher.read("Example Health", "https://careers.example.org/careers", root)

    in_scope = set(_deferred_urls(result)) | {item.url for item in result.listings}
    assert {item.title for item in result.listings} == {"Role A", "Role B"}
    assert not any("9Z8Y7X6W5V" in url for url in in_scope)
    assert result.skip_reasons.get("off_host_link", 0) == 1


def test_same_host_detail_pages_outside_the_root_directory_are_still_traversed() -> None:
    root = '<main><a href="https://jobs.example/search">View jobs</a></main>'
    hub = """
    <main><ul>
      <li><a href="/job/123">Clinical Director</a></li>
      <li><a href="/job/124">Program Lead</a></li>
    </ul></main>
    """
    urls = ("https://jobs.example/job/123", "https://jobs.example/job/124")
    client = _Client(
        {
            "https://jobs.example/search": hub,
            urls[0]: _posting("Clinical Director"),
            urls[1]: _posting("Program Lead"),
        }
    )

    result = PublisherSourceFetcher(client=cast(Any, client)).read(
        "Example Health", "https://jobs.example/job-search-results/", root
    )

    assert {listing.url for listing in result.listings} == set(urls)
    assert result.skip_reasons.get("off_host_link", 0) == 0


def _continuation_target(url: str, *, label: str = "Role") -> SourceContinuationTarget:
    """A durable frontier target that resolves to one same-origin posting."""
    return SourceContinuationTarget(
        url=url,
        label=label,
        label_source="visible",
        source_url="https://board.example/careers",
        structural_context="list",
        relations=("same-origin", "repeated-target-shape"),
    )


def test_declared_posting_in_a_navigation_shaped_continuation_is_retained() -> None:
    url = "https://jobs.example/en/category/clinical-director/733/123/1"
    client = _Client({url: _declared_posting("Clinical Director", "Example Health", url)})

    result = PublisherSourceFetcher(client=cast(Any, client)).continue_listings(
        "Example Health",
        (_continuation_target(url, label="Clinical Director"),),
        root_url="https://board.example/careers",
        handoff_scopes=(),
    )

    assert [(listing.title, listing.url) for listing in result.listings] == [
        ("Clinical Director", url)
    ]
    assert "non_posting_page" not in result.skip_reasons
    assert result.deferred_targets == ()


def test_plain_posting_in_a_navigation_shaped_continuation_is_retained() -> None:
    url = "https://jobs.example/en/category/clinical-director/733/123/1"
    client = _Client({url: _posting("Clinical Director")})

    result = PublisherSourceFetcher(client=cast(Any, client)).continue_listings(
        "Example Health",
        (_continuation_target(url, label="Clinical Director"),),
        root_url="https://board.example/careers",
        handoff_scopes=(),
    )

    assert [(listing.title, listing.url) for listing in result.listings] == [
        ("Clinical Director", url)
    ]
    assert "non_posting_page" not in result.skip_reasons
    assert result.deferred_targets == ()


class _ConcurrencyProbeClient(_Client):
    """Record peak simultaneous in-flight GETs, overall and per host."""

    def __init__(
        self,
        pages: dict[str, str | Exception],
        *,
        gate: threading.Barrier | None = None,
        hold: float = 0.0,
    ) -> None:
        super().__init__(pages)
        self._state_lock = threading.Lock()
        self._in_flight = 0
        self._per_host: collections.Counter[str] = collections.Counter()
        self.peak = 0
        self.host_peak: collections.Counter[str] = collections.Counter()
        self._gate = gate
        self._hold = hold

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        host = urlsplit(url).hostname or ""
        with self._state_lock:
            self._in_flight += 1
            self._per_host[host] += 1
            self.peak = max(self.peak, self._in_flight)
            self.host_peak[host] = max(self.host_peak[host], self._per_host[host])
        try:
            if self._gate is not None:
                with contextlib.suppress(threading.BrokenBarrierError):
                    self._gate.wait()
            elif self._hold:
                time.sleep(self._hold)
            return super().get(url, **kwargs)
        finally:
            with self._state_lock:
                self._in_flight -= 1
                self._per_host[host] -= 1


def test_distinct_host_continuations_are_read_concurrently() -> None:
    first = "https://alpha.example/roles/one"
    second = "https://beta.example/roles/two"
    # Both fetches must reach the barrier at once, so a serial reader cannot pass.
    client = _ConcurrencyProbeClient(
        {first: _posting("Role One"), second: _posting("Role Two")},
        gate=threading.Barrier(2, timeout=3),
    )

    result = PublisherSourceFetcher(client=cast(Any, client)).continue_listings(
        "Example Health",
        (
            _continuation_target(first, label="Role One"),
            _continuation_target(second, label="Role Two"),
        ),
        root_url="https://board.example/careers",
        handoff_scopes=(),
    )

    assert client.peak == 2
    assert {item.title for item in result.listings} == {"Role One", "Role Two"}
    assert {item.content_status for item in result.listings} == {
        PostingContentAccessStatus.READABLE
    }


def test_same_host_continuations_are_never_read_concurrently() -> None:
    urls = [f"https://one.example/roles/{n}" for n in range(1, 4)]
    client = _ConcurrencyProbeClient(
        {url: _posting(f"Role {n}") for n, url in enumerate(urls, 1)}, hold=0.02
    )

    result = PublisherSourceFetcher(client=cast(Any, client)).continue_listings(
        "Example Health",
        tuple(_continuation_target(url) for url in urls),
        root_url="https://board.example/careers",
        handoff_scopes=(),
    )

    assert client.host_peak["one.example"] == 1
    assert len(result.listings) == 3


def test_page_limit_caps_total_reads_across_distinct_host_lanes() -> None:
    urls = [f"https://h{n}.example/roles/only" for n in range(1, 4)]
    client = _ConcurrencyProbeClient({url: _posting("Role") for url in urls}, hold=0.01)

    result = PublisherSourceFetcher(
        client=cast(Any, client), limits=PublisherReadLimits(max_pages=2)
    ).continue_listings(
        "Example Health",
        tuple(_continuation_target(url) for url in urls),
        root_url="https://board.example/careers",
        handoff_scopes=(),
    )

    assert len(client.requested) == 2
    assert len(result.listings) == 2
    assert len(_deferred_urls(result)) == 1
    assert result.skip_reasons.get("page_limit_reached", 0) == 1


def test_deadline_stops_dispatch_before_the_next_host_lane(monkeypatch: Any) -> None:
    first = "https://alpha.example/roles/one"
    second = "https://beta.example/roles/two"
    now = [0.0]

    class _TimedProbe(_ConcurrencyProbeClient):
        def get(self, url: str, **kwargs: Any) -> httpx.Response:
            response = super().get(url, **kwargs)
            now[0] = 31.0
            return response

    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.publisher_sources.time.monotonic",
        lambda: now[0],
    )
    client = _TimedProbe({first: _posting("Role One"), second: _posting("Role Two")})

    result = PublisherSourceFetcher(
        client=cast(Any, client),
        limits=PublisherReadLimits(max_elapsed_seconds=30.0, max_concurrent_hosts=1),
    ).continue_listings(
        "Example Health",
        (
            _continuation_target(first, label="Role One"),
            _continuation_target(second, label="Role Two"),
        ),
        root_url="https://board.example/careers",
        handoff_scopes=(),
    )

    assert client.requested == [first]
    assert result.attempted_target_urls == (first,)
    assert set(_deferred_urls(result)) == {second}
    assert result.skip_reasons.get("time_limit_reached", 0) == 1


def test_a_resume_from_the_saved_apex_address_keeps_the_served_host_in_scope() -> None:
    fresh = PublisherSourceFetcher(client=cast(Any, _Client({}))).read(
        "Example Health", "https://www.example-health.org/careers", "<main></main>"
    )
    target = SourceContinuationTarget(
        url="https://www.example-health.org/careers/team",
        label="Our team",
        label_source="visible",
        source_url="https://www.example-health.org/careers",
        structural_context="list",
        relations=("same-origin",),
        depth=1,
    )
    hub = """
    <main><ul>
      <li><a href="/careers/opening-3">Opening Three</a></li>
      <li><a href="/careers/opening-4">Opening Four</a></li>
      <li><a href="/careers/opening-5">Opening Five</a></li>
    </ul></main>
    """
    openings = {
        f"https://www.example-health.org/careers/opening-{n}": _posting(f"Opening {n}")
        for n in (3, 4, 5)
    }
    client = _Client({"https://www.example-health.org/careers/team": hub, **openings})

    result = PublisherSourceFetcher(client=cast(Any, client)).continue_listings(
        "Example Health",
        (target,),
        root_url="https://example-health.org/careers",
        handoff_scopes=fresh.handoff_scopes,
    )

    assert result.skip_reasons.get("off_host_link", 0) == 0
    assert set(openings) <= set(_deferred_urls(result)) | {item.url for item in result.listings}


def test_a_resumed_continuation_with_only_depth_two_targets_keeps_its_original_scope() -> None:
    target = SourceContinuationTarget(
        url="https://www.example-health.org/careers/opening-2",
        label="Opening Two",
        label_source="visible",
        source_url="https://www.example-health.org/careers/",
        structural_context="list",
        relations=("same-origin", "repeated-target-shape"),
        depth=2,
    )
    page = """
    <main><ul>
      <li><a href="/info/patient-resources">Patient Resources</a></li>
      <li><a href="/info/visitor-guide">Visitor Guide</a></li>
    </ul></main>
    """
    client = _Client({"https://www.example-health.org/careers/opening-2": page})

    result = PublisherSourceFetcher(client=cast(Any, client)).continue_listings(
        "Example Health",
        (target,),
        root_url="https://careers.example-health.org/careers",
        handoff_scopes=(("www.example-health.org", "directory", "/careers/"),),
    )

    in_scope = set(_deferred_urls(result)) | {item.url for item in result.listings}
    assert not any("/info/" in url for url in in_scope)
    assert result.skip_reasons.get("off_host_link", 0) == 2
