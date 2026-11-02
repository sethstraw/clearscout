"""A denied API does not deny an independently permitted publisher representation."""

from __future__ import annotations

import json
import re
from html import unescape
from typing import Any

import httpx
import pytest

from tailorcv.infrastructure.web_fetch.board_adapters import BoardApiFetcher
from tailorcv.infrastructure.web_fetch.platforms.smartrecruiters import (
    publisher_collection_markup,
)
from tailorcv.infrastructure.web_fetch.policy import PoliteHttpClient, RobotsDeniedError
from tailorcv.infrastructure.web_fetch.publisher_sources import (
    REGISTERED_POSTING_LINK,
    PublisherReadLimits,
    PublisherSourceFetcher,
)
from tests.support.http import public_resolver

_ROOT = "https://careers.smartrecruiters.com/Acme"
_BODY = "Responsibilities include clinical data analysis and documenting study methods. " * 8


def _job(number: int) -> str:
    return f"https://jobs.smartrecruiters.com/Acme/{100000 + number}-analyst-{number}"


def _links(*numbers: int) -> str:
    return "".join(f'<li><a href="{_job(n)}">Clinical Analyst {n}</a></li>' for n in numbers)


def _more(group: int, page: int = 0) -> str:
    return (
        f'<a data-page="{page}" data-value="{group}" data-type="department" '
        'data-more="/Acme/api/more" class="js-more" href="#">Show more jobs</a>'
    )


def _fetcher(
    *, root_denied: bool = False, failed_group: bool = False
) -> tuple[BoardApiFetcher, list[str]]:
    calls: list[str] = []
    root = (
        '<main><div class="js-openings-load" data-groups-pages="2" data-page="0" '
        'data-more="/Acme/api/groups"><ul>' + _links(1, 2) + _more(11) + "</ul></div></main>"
    )
    pages = {
        _ROOT: root,
        _ROOT + "/api/groups?page=1": "<main><ul>" + _links(2, 3) + _more(22) + "</ul></main>",
        _ROOT + "/api/more?page=1&type=department&value=11": "<main><ul>"
        + _links(4)
        + _more(11, 1)
        + "</ul></main>",
        _ROOT + "/api/more?page=2&type=department&value=11": "",
        _ROOT + "/api/more?page=1&type=department&value=22": "",
    }

    def get(url: str, **_kwargs: Any) -> httpx.Response:
        calls.append(url)
        if url.endswith("/robots.txt"):
            denied = "api.smartrecruiters.com" in url or (root_denied and "careers." in url)
            text = "User-agent: *\nDisallow: /" if denied else "User-agent: *\nAllow: /"
            return httpx.Response(200, text=text, request=httpx.Request("GET", url))
        if failed_group and "/api/groups" in url:
            return httpx.Response(503, request=httpx.Request("GET", url))
        if url.startswith("https://jobs.smartrecruiters.com/"):
            number = int(url.rsplit("-", 1)[1])
            record = {
                "@type": "JobPosting",
                "title": f"Clinical Analyst {number}",
                "url": url,
                "description": _BODY,
                "hiringOrganization": {"name": "Acme"},
            }
            page = '<script type="application/ld+json">' + json.dumps(record) + "</script>"
        else:
            page = pages[url]
        return httpx.Response(200, text=page, request=httpx.Request("GET", url))

    client = PoliteHttpClient(
        user_agent="tailorcv/test",
        getter=get,
        resolver=public_resolver,
        sleeper=lambda _seconds: None,
        jitter=lambda _low, _high: 0.0,
    )
    return BoardApiFetcher(client=client), calls


def test_denied_api_falls_back_to_permitted_group_and_department_pages() -> None:
    fetcher, calls = _fetcher()
    read = fetcher.listings("Acme", _ROOT)
    assert {row.url for row in read.listings} == {_job(n) for n in (1, 2, 3, 4)}
    assert all(row.published_description == _BODY.strip() for row in read.listings)
    assert calls.count(_job(2)) == 1
    assert not any("api.smartrecruiters.com/v1" in url for url in calls)
    assert not any("groups?page=2" in url for url in calls)
    assert read.complete is False
    assert read.skip_reasons["board_api_policy_refused"] == 1


def test_alternate_representation_is_independently_policy_checked() -> None:
    fetcher, calls = _fetcher(root_denied=True)
    with pytest.raises(RobotsDeniedError):
        fetcher.listings("Acme", _ROOT)
    assert _ROOT not in calls


def test_failed_group_retains_success_and_exact_recovery_target() -> None:
    fetcher, _ = _fetcher(failed_group=True)
    read = fetcher.listings("Acme", _ROOT)
    assert {_job(1), _job(2)} <= {row.url for row in read.listings}
    assert _ROOT + "/api/groups?page=1" in {target.url for target in read.deferred_targets}
    assert read.complete is False


def _pager_page(numbers: tuple[int, ...], *, previous: str = "", following: str = "") -> str:
    """The openings list as the publisher serves it, with its no-script pager."""
    items = "".join(
        f'<li class="opening-job job column wide-1of2"><a href="{_job(n)}">'
        f"Clinical Analyst {n}</a></li>"
        for n in numbers
    )
    back = f'<a href="{previous}" class="link">Previous page</a>' if previous else "Previous page"
    ahead = f'<a href="{following}" class="link">Next page</a>' if following else ""
    return (
        '<div class="openings-body js-openings"><section class="openings-section opening">'
        '<ul data-page="0" data-more="/Acme/api/more" class="opening-jobs js-openings-load">'
        + items
        + '<li class="opening-job job column sr-nojs"><ul class="list--dotted">'
        f'<li class="link--block-target">{back}</li>'
        f'<li class="link--block-target">{ahead}</li>'
        "</ul></li></ul></section></div>"
    )


def _injected(markup: str) -> list[str]:
    return [unescape(href) for href in re.findall(r'class="page-numbers" href="([^"]+)"', markup)]


def test_the_first_publisher_page_exposes_its_declared_next_page() -> None:
    page = _pager_page((1, 2), following="?search=&amp;page=1")

    assert _injected(publisher_collection_markup(_ROOT, page)) == [_ROOT + "?search=&page=1"]


def test_a_middle_publisher_page_exposes_only_its_next_page() -> None:
    page = _pager_page((3, 4), previous="?search=&amp;page=1", following="?search=&amp;page=3")

    assert _injected(publisher_collection_markup(_ROOT + "?page=2", page)) == [
        _ROOT + "?search=&page=3"
    ]


def test_the_last_publisher_page_exposes_no_next_page() -> None:
    page = _pager_page((5,), previous="?search=&amp;page=2")

    assert publisher_collection_markup(_ROOT + "?search=&page=3", page) == page


@pytest.mark.parametrize(
    "following",
    [
        "https://careers.example.org/Acme?search=&amp;page=1",
        "https://careers.smartrecruiters.com/Globex?search=&amp;page=1",
        "?search=&amp;page=1&amp;sort=date",
        "?search=nurse&amp;page=1",
        "?search=&amp;page=next",
    ],
    ids=["off-host", "other-org", "extra-key", "changed-search", "non-numeric"],
)
def test_a_next_page_that_leaves_this_collection_is_not_exposed(following: str) -> None:
    page = _pager_page((1, 2), following=following)

    assert publisher_collection_markup(_ROOT, page) == page


_PAGED = {0: (1, 2), 1: (3, 4), 2: (5, 6), 3: (7, 8)}


def _paged_fetcher(
    max_pages: int = 250, max_frontier_items: int = 500
) -> tuple[BoardApiFetcher, list[str]]:
    """A four-page board whose API is refused, so only its publisher pages read."""
    calls: list[str] = []
    last = max(_PAGED)
    pages = {
        (_ROOT if index == 0 else f"{_ROOT}?search=&page={index}"): _pager_page(
            numbers,
            previous=f"?search=&amp;page={index - 1}" if index > 1 else "",
            following=f"?search=&amp;page={index + 1}" if index < last else "",
        )
        for index, numbers in _PAGED.items()
    }

    def get(url: str, **_kwargs: Any) -> httpx.Response:
        calls.append(url)
        request = httpx.Request("GET", url)
        if url.endswith("/robots.txt"):
            denied = "api.smartrecruiters.com" in url
            text = "User-agent: *\nDisallow: /" if denied else "User-agent: *\nAllow: /"
            return httpx.Response(200, text=text, request=request)
        if url.startswith("https://jobs.smartrecruiters.com/"):
            number = int(url.rsplit("-", 1)[1])
            record = {
                "@type": "JobPosting",
                "title": f"Clinical Analyst {number}",
                "url": url,
                "description": _BODY,
                "hiringOrganization": {"name": "Acme"},
            }
            text = '<script type="application/ld+json">' + json.dumps(record) + "</script>"
            return httpx.Response(200, text=text, request=request)
        return httpx.Response(200, text=pages[url], request=request)

    client = PoliteHttpClient(
        user_agent="tailorcv/test",
        getter=get,
        resolver=public_resolver,
        sleeper=lambda _seconds: None,
        jitter=lambda _low, _high: 0.0,
    )
    fetcher = BoardApiFetcher(client=client)
    fetcher.publishers = PublisherSourceFetcher(
        client=client,
        limits=PublisherReadLimits(max_pages=max_pages, max_frontier_items=max_frontier_items),
    )
    return fetcher, calls


_EVERY_JOB = {_job(n) for numbers in _PAGED.values() for n in numbers}


def test_a_refused_api_reads_every_publisher_page_through_its_next_links() -> None:
    fetcher, calls = _paged_fetcher()

    read = fetcher.listings("Acme", _ROOT)

    assert sorted(row.url for row in read.listings) == sorted(_EVERY_JOB)
    assert all(calls.count(job) == 1 for job in _EVERY_JOB)
    assert [url for url in calls if "careers." in url and "robots" not in url] == [
        _ROOT,
        *(f"{_ROOT}?search=&page={index}" for index in (1, 2, 3)),
    ]
    assert not any("/api/more" in url for url in calls)
    assert read.deferred_targets == ()
    assert read.complete is False


def test_a_cut_publisher_read_resumes_its_next_page_in_a_continuation() -> None:
    fetcher, _ = _paged_fetcher(max_pages=3)

    first = fetcher.listings("Acme", _ROOT)
    pending = [target for target in first.deferred_targets if "page=" in target.url]

    assert first.complete is False
    assert first.skip_reasons["page_limit_reached"] >= 1
    assert len(pending) == 1
    assert pending[0].automatic_eligible is True
    resumed, _ = _paged_fetcher()
    rest = resumed.continue_listings(
        "Acme",
        first.deferred_targets,
        root_url=_ROOT,
        handoff_scopes=first.handoff_scopes,
    )
    read_urls = [row.url for row in (*first.listings, *rest.listings)]
    assert sorted(read_urls) == sorted(_EVERY_JOB)
    assert not any(target.automatic_eligible for target in rest.deferred_targets)


def test_a_cut_read_keeps_its_postings_automatic_without_counting_them_skipped() -> None:
    fetcher, _ = _paged_fetcher(max_pages=2)

    first = fetcher.listings("Acme", _ROOT)
    postings = [target for target in first.deferred_targets if target.url in _EVERY_JOB]

    assert postings
    assert all(target.automatic_eligible for target in postings)
    assert all(target.deferred_reason == REGISTERED_POSTING_LINK for target in postings)
    assert set(first.skip_reasons) <= {"page_limit_reached", "board_api_policy_refused"}


def test_postings_past_the_frontier_budget_stay_automatic_under_their_own_reason() -> None:
    fetcher, _ = _paged_fetcher(max_pages=1, max_frontier_items=2)

    first = fetcher.listings("Acme", _ROOT)
    postings = [target for target in first.deferred_targets if target.url in _EVERY_JOB]

    assert first.skip_reasons.get("frontier_deferred", 0) > 0
    assert postings
    assert all(target.automatic_eligible for target in postings)
    assert all(target.deferred_reason == REGISTERED_POSTING_LINK for target in postings)


@pytest.mark.parametrize("served", ["", "https://acme.wd5.myworkdayjobs.com/Careers"])
@pytest.mark.parametrize("status", [401, 403])
def test_workday_api_refusal_preserves_independently_readable_publisher_page(
    status: int, served: str
) -> None:
    root = "https://acme.wd1.myworkdayjobs.com/Careers"
    calls: list[str] = []
    record = {
        "@type": "JobPosting",
        "title": "Clinical Analyst",
        "description": _BODY,
        "hiringOrganization": {"name": "Acme"},
        "url": served or root,
    }

    def get(url: str, **_kwargs: Any) -> httpx.Response:
        calls.append(url)
        if served and url == root:
            return httpx.Response(
                302, headers={"Location": served}, request=httpx.Request("GET", url)
            )
        page = (
            ""
            if url.endswith("/robots.txt")
            else '<script type="application/ld+json">' + json.dumps(record) + "</script>"
        )
        return httpx.Response(200, text=page, request=httpx.Request("GET", url))

    def post(url: str, **_kwargs: Any) -> httpx.Response:
        return httpx.Response(status, request=httpx.Request("POST", url))

    client = PoliteHttpClient(
        user_agent="tailorcv/test",
        getter=get,
        poster=post,
        resolver=public_resolver,
        sleeper=lambda _seconds: None,
        jitter=lambda _low, _high: 0.0,
    )
    read = BoardApiFetcher(client=client).listings("Acme", root)
    assert [(row.title, row.url) for row in read.listings] == [("Clinical Analyst", served or root)]
    assert root in calls
    assert read.complete is False
    assert read.skip_reasons["board_api_access_refused"] == 1


_ICIMS_ROOT = "https://uscareers-acme.icims.com/jobs/search"
_ICIMS_SITEMAP = "https://uscareers-acme.icims.com/sitemap.xml"


_JIBE_SITE = "https://careers.acme.example/jobs"
_JIBE_API = "https://careers.acme.example/api/jobs?page=1&limit=20"
_JIBE_JOB = "https://careers.acme.example/jobs/1"
_JIBE_SHELL = '<link href="https://cdn.jibecdn.com/prod/acme/latest/main.css">'


def _icims_fetcher(
    sitemap_status: int,
    *,
    page_status: int = 200,
    sitemap_denied: bool = False,
    redirect_markup: str | None = None,
    jibe_api_status: int = 200,
    jibe_api_html: bool = False,
) -> tuple[BoardApiFetcher, list[str]]:
    calls: list[str] = []
    record = {
        "@type": "JobPosting",
        "title": "Clinical Analyst",
        "description": _BODY,
        "hiringOrganization": {"name": "Acme"},
        "url": _ICIMS_ROOT,
    }

    def get(url: str, **_kwargs: Any) -> httpx.Response:
        calls.append(url)
        if url.endswith("/robots.txt"):
            text = "User-agent: *\nDisallow: /sitemap.xml" if sitemap_denied else ""
            return httpx.Response(200, text=text, request=httpx.Request("GET", url))
        request = httpx.Request("GET", url)
        if redirect_markup is not None and url == _ICIMS_ROOT:
            return httpx.Response(302, headers={"Location": _JIBE_SITE}, request=request)
        if redirect_markup is not None and url == _JIBE_SITE:
            return httpx.Response(200, text=redirect_markup, request=request)
        if url == _JIBE_API and jibe_api_status != 200:
            return httpx.Response(jibe_api_status, request=request)
        if url == _JIBE_API and jibe_api_html:
            return httpx.Response(200, text="<html>Maintenance</html>", request=request)
        if url == _JIBE_API:
            job = {
                "title": "Clinical Analyst",
                "req_id": "R1",
                "apply_url": _JIBE_JOB,
                "full_location": "Northtown, ON",
                "description": _BODY,
            }
            return httpx.Response(
                200, json={"jobs": [{"data": job}], "totalCount": 1}, request=request
            )
        status = sitemap_status if url == _ICIMS_SITEMAP else page_status
        page = '<script type="application/ld+json">' + json.dumps(record) + "</script>"
        return httpx.Response(status, text=page, request=httpx.Request("GET", url))

    client = PoliteHttpClient(
        user_agent="tailorcv/test",
        getter=get,
        resolver=public_resolver,
        sleeper=lambda _seconds: None,
        jitter=lambda _low, _high: 0.0,
    )
    return BoardApiFetcher(client=client), calls


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_classic_icims_sitemap_reads_the_permitted_search_page(status: int) -> None:
    fetcher, calls = _icims_fetcher(status)

    read = fetcher.listings("Acme", _ICIMS_ROOT)

    assert [(row.title, row.url) for row in read.listings] == [("Clinical Analyst", _ICIMS_ROOT)]
    assert _ICIMS_SITEMAP in calls
    assert read.complete is False
    assert read.skip_reasons["board_api_access_refused"] == 1


def test_a_refused_classic_icims_root_redirecting_to_a_jibe_site_reads_the_jibe_board() -> None:
    fetcher, calls = _icims_fetcher(403, redirect_markup=_JIBE_SHELL)

    read = fetcher.listings("Acme", _ICIMS_ROOT)

    assert [(row.title, row.url) for row in read.listings] == [
        ("Clinical Analyst", f"{_JIBE_JOB}/job?in_iframe=1")
    ]
    assert _JIBE_API in calls
    assert read.complete is True
    assert "board_api_access_refused" not in read.skip_reasons


@pytest.mark.parametrize(
    ("markup", "api_status", "api_html"),
    [
        ('<a href="https://boards.greenhouse.io/globex">Partner jobs</a>', 200, False),
        (_JIBE_SHELL, 403, False),
        (_JIBE_SHELL, 200, True),
    ],
    ids=["linked-greenhouse-board", "refused-jibe-api", "html-jibe-api"],
)
def test_a_redirected_classic_icims_root_without_a_readable_own_board_keeps_the_publisher_read(
    markup: str, api_status: int, api_html: bool
) -> None:
    fetcher, calls = _icims_fetcher(
        403, redirect_markup=markup, jibe_api_status=api_status, jibe_api_html=api_html
    )

    read = fetcher.listings("Acme", _ICIMS_ROOT)

    assert read.listings == []
    assert read.complete is False
    assert read.skip_reasons["board_api_access_refused"] == 1
    assert not any("greenhouse" in call for call in calls)


def test_a_classic_icims_site_refusing_both_reads_raises() -> None:
    fetcher, _ = _icims_fetcher(403, page_status=403)

    with pytest.raises(httpx.HTTPStatusError):
        fetcher.listings("Acme", _ICIMS_ROOT)


def test_a_missing_classic_icims_sitemap_does_not_fall_back() -> None:
    fetcher, calls = _icims_fetcher(404)

    with pytest.raises(httpx.HTTPStatusError):
        fetcher.listings("Acme", _ICIMS_ROOT)
    assert _ICIMS_ROOT not in calls


def test_a_robots_refused_classic_icims_sitemap_does_not_fall_back() -> None:
    fetcher, calls = _icims_fetcher(200, sitemap_denied=True)

    with pytest.raises(RobotsDeniedError):
        fetcher.listings("Acme", _ICIMS_ROOT)
    assert _ICIMS_ROOT not in calls


def _fragment(*numbers: int) -> str:
    """A department fragment as the publisher's script loads it, with no pager of its own."""
    return "".join(
        f'<li class="opening-job"><a href="{_job(n)}" class="link--block">'
        f"<h4>Clinical Analyst {n}</h4></a></li>"
        for n in numbers
    )


def _department_url(page: int, value: str) -> str:
    return f"{_ROOT}/api/more?page={page}&type=department&value={value}"


def _grouped_fetcher(pages: dict[str, str]) -> tuple[BoardApiFetcher, list[str]]:
    calls: list[str] = []

    def get(url: str, **_kwargs: Any) -> httpx.Response:
        calls.append(url)
        request = httpx.Request("GET", url)
        if url.endswith("/robots.txt"):
            denied = "api.smartrecruiters.com" in url
            text = "User-agent: *\nDisallow: /" if denied else "User-agent: *\nAllow: /"
            return httpx.Response(200, text=text, request=request)
        if url.startswith("https://jobs.smartrecruiters.com/"):
            number = int(url.rsplit("-", 1)[1])
            record = {
                "@type": "JobPosting",
                "title": f"Clinical Analyst {number}",
                "url": url,
                "description": _BODY,
                "hiringOrganization": {"name": "Acme"},
            }
            text = '<script type="application/ld+json">' + json.dumps(record) + "</script>"
            return httpx.Response(200, text=text, request=request)
        return httpx.Response(200, text=pages.get(url, ""), request=request)

    client = PoliteHttpClient(
        user_agent="tailorcv/test",
        getter=get,
        resolver=public_resolver,
        sleeper=lambda _seconds: None,
        jitter=lambda _low, _high: 0.0,
    )
    return BoardApiFetcher(client=client), calls


_DEPARTMENT_RUNS = [range(1000 + 10 * page, 1010 + 10 * page) for page in range(6)]


def _grouped_board() -> tuple[dict[str, str], set[str]]:
    """Three groups pages, a seven-page department ending on one posting, and a one-posting one."""
    root = (
        '<main><div class="js-openings-load" data-groups-pages="3" data-page="0" '
        'data-more="/Acme/api/groups"><ul>' + _fragment(1, 2) + "</ul></div></main>"
    )
    department = [*_DEPARTMENT_RUNS, range(1060, 1061)]
    pages = {
        _ROOT: root,
        _ROOT + "/api/groups?page=1": "<ul>" + _fragment(3, 4) + "</ul>" + _more(7),
        _ROOT + "/api/groups?page=2": "<ul>" + _fragment(5, 6) + "</ul>" + _more(9),
        **{
            _department_url(index + 1, "7"): "<ul>" + _fragment(*run) + "</ul>"
            for index, run in enumerate(department)
        },
        _department_url(1, "9"): "<ul>" + _fragment(2000) + "</ul>",
    }
    numbers = {1, 2, 3, 4, 5, 6, 2000} | {n for run in department for n in run}
    return pages, {_job(n) for n in numbers}


def test_a_grouped_board_reads_every_department_page_until_one_is_empty() -> None:
    pages, every = _grouped_board()
    fetcher, calls = _grouped_fetcher(pages)

    read = fetcher.listings("Acme", _ROOT)

    assert sorted(row.url for row in read.listings) == sorted(every)
    assert all(calls.count(job) == 1 for job in every)
    assert all(calls.count(_department_url(page, "7")) == 1 for page in range(1, 9))
    assert _department_url(9, "7") not in calls
    assert _department_url(2, "9") in calls
    assert _department_url(3, "9") not in calls
    deferred = {target.url for target in read.deferred_targets}
    assert not deferred & {_department_url(8, "7"), _department_url(2, "9")}
    assert read.skip_reasons["collection_exhausted"] == 2


def test_a_department_whose_second_page_is_empty_stops() -> None:
    pages = {
        _ROOT: (
            '<main><div class="js-openings-load" data-groups-pages="1" data-page="0" '
            'data-more="/Acme/api/groups"><ul>'
            + _fragment(1, 2)
            + "</ul>"
            + _more(7)
            + "</div></main>"
        ),
        _department_url(1, "7"): "<ul>" + _fragment(*range(10, 20)) + "</ul>",
    }
    fetcher, calls = _grouped_fetcher(pages)

    read = fetcher.listings("Acme", _ROOT)

    assert {row.url for row in read.listings} == {_job(n) for n in (1, 2, *range(10, 20))}
    assert _department_url(2, "7") in calls
    assert _department_url(3, "7") not in calls
    assert _department_url(2, "7") not in {target.url for target in read.deferred_targets}
    assert read.skip_reasons["collection_exhausted"] == 1


def test_a_board_grouped_by_location_reads_every_page_of_the_location() -> None:
    location = _ROOT + "/api/more?page={}&type=location&value=Example+City%2C+CA"
    pages = {
        _ROOT: (
            '<main><div class="js-openings-load" data-groups-pages="1" data-page="0" '
            'data-more="/Acme/api/groups"><ul>'
            + _fragment(1, 2)
            + '</ul><a data-page="0" data-value="Example%20City%2C%20CA" data-type="location" '
            'data-more="/Acme/api/more" class="js-more" href="#">Show more jobs</a></div></main>'
        ),
        location.format(1): "<ul>" + _fragment(*range(10, 20)) + "</ul>",
        location.format(2): "<ul>" + _fragment(*range(20, 25)) + "</ul>",
    }
    fetcher, calls = _grouped_fetcher(pages)

    read = fetcher.listings("Acme", _ROOT)

    assert {row.url for row in read.listings} == {_job(n) for n in (1, 2, *range(10, 25))}
    assert location.format(3) in calls
    assert location.format(4) not in calls
    assert location.format(3) not in {target.url for target in read.deferred_targets}
    assert read.skip_reasons["collection_exhausted"] == 1


def test_a_department_fragment_exposes_its_next_page() -> None:
    markup = publisher_collection_markup(_department_url(3, "7"), "<ul>" + _fragment(1) + "</ul>")

    assert _injected(markup) == [_department_url(3, "7"), _department_url(4, "7")]


@pytest.mark.parametrize(
    "url",
    [
        "https://careers.example.org/Acme/api/more?page=1&type=department&value=7",
        "https://careers.smartrecruiters.com/Acme/api/other?page=1&type=department&value=7",
        "https://careers.smartrecruiters.com/Acme/api/more?page=x&type=department&value=7",
        "https://careers.smartrecruiters.com/Acme/api/more?page=1&type=brand&value=7",
    ],
    ids=["off-host", "other-path", "non-numeric", "other-type"],
)
def test_a_fragment_outside_this_department_pager_exposes_no_next_page(url: str) -> None:
    assert _injected(publisher_collection_markup(url, "<ul>" + _fragment(1) + "</ul>")) == []


def test_an_empty_department_fragment_exposes_no_next_page() -> None:
    assert publisher_collection_markup(_department_url(2, "7"), "") == ""


@pytest.mark.parametrize(
    "more", ["/Globex/api/more", "https://careers.example.org/Acme/api/more"], ids=["org", "host"]
)
def test_a_department_link_to_another_org_or_host_is_not_exposed(more: str) -> None:
    page = (
        "<ul>" + _fragment(1) + "</ul>"
        f'<a class="js-more" data-type="department" data-value="7" data-page="0" '
        f'data-more="{more}" href="#">Show more jobs</a>'
    )

    markup = publisher_collection_markup(_ROOT + "/api/groups?page=1", page)

    assert "api/more?" not in markup


@pytest.mark.parametrize("group", ["department", "location"])
@pytest.mark.parametrize(
    "value",
    ["Halifax%2FDartmouth", "Bath%20%26%20Wells", "Toronto%252C%20CA"],
    ids=["slash", "ampersand", "double-encoded"],
)
def test_a_group_whose_value_falls_outside_the_safe_shape_is_not_exposed(
    group: str, value: str
) -> None:
    page = (
        "<ul>" + _fragment(1) + "</ul>"
        f'<a class="js-more" data-type="{group}" data-value="{value}" data-page="0" '
        'data-more="/Acme/api/more" href="#">Show more jobs</a>'
    )

    markup = publisher_collection_markup(_ROOT + "/api/groups?page=1", page)

    assert "api/more?" not in markup


def test_a_department_fragment_read_as_the_root_is_not_fetched_again() -> None:
    root = _department_url(1, "7")
    fetcher, calls = _grouped_fetcher({_department_url(2, "7"): ""})
    publishers = PublisherSourceFetcher(client=fetcher.client)

    read = publishers.read("Acme", root, "<ul>" + _fragment(1) + "</ul>")

    assert root not in calls
    assert {row.url for row in read.listings} == {_job(1)}
