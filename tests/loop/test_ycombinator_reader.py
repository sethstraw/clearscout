"""Y Combinator job pages list one company's postings in their Inertia page props."""

from __future__ import annotations

import json
from html import escape
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from tailorcv.infrastructure.web_fetch import board_adapters
from tailorcv.infrastructure.web_fetch.board_adapters import BoardApiFetcher
from tailorcv.infrastructure.web_fetch.ingest import strip_tags
from tailorcv.infrastructure.web_fetch.policy import PoliteHttpClient
from tests.support.doubles import declaring_page
from tests.support.http import public_resolver

_ORIGIN = "https://www.ycombinator.com"
_ROOT = _ORIGIN + "/companies/acme/jobs"
_BODY = (
    "<h2>About the role</h2><p>Build reporting pipelines for clinical research teams and "
    "document reproducible methods for every reviewed analysis.</p>"
)


def _posting(number: int, title: str, location: str) -> dict[str, Any]:
    return {
        "id": 4000 + number,
        "title": title,
        "location": location,
        "url": f"/companies/acme/jobs/Ab{number}Xy-data-analyst",
        "applyUrl": f"https://account.example/authenticate?signup_job_id={4000 + number}",
        "type": "Full-time",
        "role": "engineering",
        "salaryRange": "",
        "createdAt": "about 4 hours",
    }


def _related() -> dict[str, Any]:
    return {
        "id": 9001,
        "title": "Foreign Engineer",
        "location": "Remote",
        "url": "/companies/globex/jobs/Zz9-foreign-engineer",
        "companyName": "Globex",
    }


def _state_page(props: object) -> str:
    state = json.dumps({"component": "WaasShowJobsPage", "props": props})
    return f'<html><body><div id="app" data-page="{escape(state)}"></div></body></html>'


class _Site:
    def __init__(self, second_title: str = "Research Engineer") -> None:
        self.second_title = second_title
        self.board: str = _state_page(
            {
                "company": {"name": "Acme"},
                "jobPostings": [
                    _posting(1, "\U0001f680 Data Analyst", "San Francisco, CA, US / Remote (US)"),
                    _posting(2, second_title, "Remote (US)"),
                ],
                "jobsUrl": "/jobs",
                "relatedJobs": [_related()],
            }
        )
        self.board_status = 200
        self.renamed_from: str | None = None
        self.failed: set[int] = set()
        self.requested: list[str] = []
        self.fetcher = self.new_fetcher()

    def new_fetcher(self) -> BoardApiFetcher:
        """Build a transport answering from this synthetic site."""

        def get(url: str, **_kwargs: object) -> httpx.Response:
            if url.endswith("/robots.txt"):
                return httpx.Response(200, request=httpx.Request("GET", url))
            return self.answer(url)

        return BoardApiFetcher(
            client=PoliteHttpClient(
                user_agent="tailorcv/test",
                getter=get,
                sleeper=lambda _seconds: None,
                jitter=lambda _low, _high: 0.0,
                clock=lambda: 0.0,
                resolver=public_resolver,
            )
        )

    @staticmethod
    def url(number: int) -> str:
        return f"{_ROOT}/Ab{number}Xy-data-analyst"

    def answer(self, url: str) -> httpx.Response:
        self.requested.append(url)
        request = httpx.Request("GET", url)
        path = urlsplit(url).path
        if self.renamed_from is not None and path == f"/companies/{self.renamed_from}/jobs":
            return httpx.Response(301, request=request, headers={"location": _ROOT})
        if path == "/companies/acme/jobs":
            return httpx.Response(self.board_status, request=request, text=self.board)
        for number in (1, 2):
            if url == self.url(number) and number not in self.failed:
                title = "\U0001f680 Data Analyst" if number == 1 else self.second_title
                page = declaring_page(
                    {
                        "@type": "JobPosting",
                        "title": title,
                        "description": _BODY,
                        "hiringOrganization": {"name": "Acme"},
                    }
                )
                return httpx.Response(200, request=request, text=page)
        return httpx.Response(404, request=request)


def test_the_company_jobs_address_names_the_board_and_a_lookalike_does_not() -> None:
    identified = board_adapters.identify_board(_ROOT)

    assert identified is not None
    assert identified[0] == "ycombinator"
    for address in (
        "https://www.ycombinator.com.evil.example/companies/acme/jobs",
        "https://news.ycombinator.com/companies/acme/jobs",
        "https://www.ycombinator.com/companies/acme",
        "https://careers.acme.example/companies/acme/jobs",
    ):
        found = board_adapters.identify_board(address)
        assert found is None or found[0] != "ycombinator", address


def test_listed_postings_read_with_bodies_and_related_jobs_are_not_this_board() -> None:
    site = _Site()

    read = site.fetcher.listings("Acme", _ROOT)

    assert read.complete is True
    assert [(row.requisition_id, row.title, row.url) for row in read.listings] == [
        ("4001", "Data Analyst", site.url(1)),
        ("4002", "Research Engineer", site.url(2)),
    ]
    assert all(strip_tags(_BODY) in row.published_description for row in read.listings)
    assert list(read.listings[0].facts.locations.values) == ["San Francisco, CA, US / Remote (US)"]
    assert not any("globex" in url or "account.example" in url for url in site.requested)
    assert not any("account.example" in row.url for row in read.listings)


def test_a_listed_salary_range_is_kept_when_the_posting_page_states_none() -> None:
    site = _Site()
    paid = {**_posting(1, "Data Analyst", "Remote (US)"), "salaryRange": "$120K - $160K"}
    site.board = _state_page(
        {"company": {"name": "Acme"}, "jobPostings": [paid, _posting(2, "Research", "Remote")]}
    )

    listings = site.fetcher.listings("Acme", _ROOT).listings

    assert [row.facts.compensation.values for row in listings] == [("$120K - $160K",), ()]


@pytest.mark.parametrize(
    ("published", "read"),
    [
        (".NET Developer", ".NET Developer"),
        ("(Senior) Data Analyst", "(Senior) Data Analyst"),
        ("#1 Hire", "#1 Hire"),
        ("\U0001f469‍\U0001f4bb️ Research Engineer", "Research Engineer"),
    ],
)
def test_only_a_leading_emoji_badge_is_dropped_from_a_title(published: str, read: str) -> None:
    site = _Site(second_title=published)

    listings = site.fetcher.listings("Acme", _ROOT).listings

    assert [row.title for row in listings] == ["Data Analyst", read]


def test_a_failed_detail_resumes_by_its_stable_identity() -> None:
    site = _Site()
    site.failed = {2}

    first = site.fetcher.listings("Acme", _ROOT)

    assert first.complete is False
    assert [row.requisition_id for row in first.listings] == ["4001"]
    site.failed.clear()

    resumed = site.fetcher.continue_listings(
        "Acme", first.deferred_targets, root_url=_ROOT, handoff_scopes=()
    )

    assert [(row.requisition_id, row.url) for row in resumed.listings] == [("4002", site.url(2))]
    assert not resumed.deferred_targets


def test_a_renamed_company_reads_its_postings_under_the_redirected_slug() -> None:
    site = _Site()
    site.renamed_from = "acme-old"

    read = site.fetcher.listings("Acme", _ORIGIN + "/companies/acme-old/jobs")

    assert read.complete is True
    assert [(row.requisition_id, row.url) for row in read.listings] == [
        ("4001", site.url(1)),
        ("4002", site.url(2)),
    ]


def test_a_redirected_board_still_refuses_another_company_posting() -> None:
    site = _Site()
    site.renamed_from = "acme-old"
    site.board = _state_page({"company": {"name": "Acme"}, "jobPostings": [_related()]})

    with pytest.raises(ValueError, match="own board"):
        site.fetcher.listings("Acme", _ORIGIN + "/companies/acme-old/jobs")


def test_an_empty_posting_list_is_a_complete_empty_board() -> None:
    site = _Site()
    site.board = _state_page({"company": {"name": "Acme"}, "jobPostings": [], "relatedJobs": []})

    read = site.fetcher.listings("Acme", _ROOT)

    assert read.complete is True
    assert read.listings == []


@pytest.mark.parametrize(
    "board",
    [
        '<html><body><div id="app" data-page="{not json"></div></body></html>',
        "<html><body><p>Jobs at Acme</p></body></html>",
        _state_page({"company": {"name": "Acme"}, "relatedJobs": [_related()]}),
        _state_page({"company": {"name": "Acme"}, "jobPostings": {"count": 0}}),
    ],
    ids=["malformed_state", "no_state", "missing_postings", "postings_not_a_list"],
)
def test_an_unreadable_page_state_is_refused_rather_than_empty(board: str) -> None:
    site = _Site()
    site.board = board

    with pytest.raises(ValueError):
        site.fetcher.listings("Acme", _ROOT)


def test_a_refused_board_page_is_unread() -> None:
    site = _Site()
    site.board_status = 403

    with pytest.raises(httpx.HTTPStatusError):
        site.fetcher.listings("Acme", _ROOT)
