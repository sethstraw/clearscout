"""Deel's careers page lists its postings in Next flight data; each overview page declares one."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from clearscout.infrastructure.web_fetch.board_adapters import (
    BOARD_ADAPTER_REGISTRY,
    BoardApiFetcher,
    identify_board,
)
from clearscout.infrastructure.web_fetch.board_reads import page_continuation_target
from clearscout.infrastructure.web_fetch.boards import job_board_address
from tests.support.http import policy_client

_ROOT = "https://www.deel.com/careers/"
_ANALYST_ID = "3f2e1d0c-9b8a-4c7d-8e6f-5a4b3c2d1e0f"
_LEAD_ID = "8a7b6c5d-4e3f-4a2b-9c1d-0e9f8a7b6c5d"
_CLOSED_ID = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"


def _overview(identity: str) -> str:
    return f"https://jobs.deel.com/deel/job-details/{identity}/overview"


def _job(identity: str, title: str, places: list[str], pay: str, *, shown: bool) -> dict[str, Any]:
    return {
        "id": 0,
        "attributes": {
            "ashby_id": identity,
            "title": title,
            "is_listed": identity != _CLOSED_ID,
            "external_link": _overview(identity).replace("/overview", "/application"),
            "compensation_tier_summary": pay,
            "should_display_compensation_on_Job_board": shown,
            "full_job_description": "",
            "all_locations": places,
            "SEO": "$undefined",
        },
    }


def _careers(jobs: list[dict[str, Any]]) -> str:
    flight = (
        '0:["$","main",null,{}]\n'
        '7:["$","section",null,{"data":{"link":"listing"},'
        f'"__component":"product.career-job-listing","jobs":{json.dumps(jobs)}}}]\n'
    )
    # The page streams its flight data in several chunks that split values mid-string.
    middle = len(flight) // 2
    pushes = "".join(
        f"<script>self.__next_f.push([1,{json.dumps(part)}])</script>"
        for part in (flight[:middle], flight[middle:])
    )
    return (
        f'<html><head><link rel="canonical" href="{_ROOT}"/></head><body>'
        f"<script>(self.__next_f=self.__next_f||[]).push([0])</script>{pushes}</body></html>"
    )


def _posting(title: str, place: str, body: str) -> str:
    declared = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": title,
        "description": f"<p>{body}</p>",
        "hiringOrganization": {"@type": "Organization", "name": "Deel"},
        "employmentType": "FULL_TIME",
        "jobLocation": {"@type": "Place", "address": {"addressCountry": place}},
    }
    return (
        '<html><head><script type="application/ld+json">'
        f"{json.dumps(declared)}</script></head><body></body></html>"
    )


_CAREERS = _careers(
    [
        _job(
            _ANALYST_ID,
            "Payroll Data Analyst",
            ["Canada", "United States"],
            "$$95,000 - $125,000 USD",
            shown=True,
        ),
        _job(_CLOSED_ID, "Retired Role", ["Canada"], "", shown=False),
        _job(_LEAD_ID, "Partnerships Lead", ["Germany"], "$$1 - $2 EUR", shown=False),
    ]
)
_PAGES = {
    _ROOT: _CAREERS,
    _overview(_ANALYST_ID): _posting(
        "Payroll Data Analyst", "Canada", "Reconcile payroll runs across countries."
    ),
    _overview(_LEAD_ID): _posting("Partnerships Lead", "Germany", "Grow the reseller channel."),
}


def _fetcher(pages: dict[str, str], requested: list[str]) -> BoardApiFetcher:
    def answer(url: str) -> httpx.Response:
        requested.append(url)
        request = httpx.Request("GET", url)
        if url not in pages:
            return httpx.Response(404, request=request)
        return httpx.Response(200, request=request, text=pages[url])

    return BoardApiFetcher(client=policy_client(answer))


def test_the_deel_careers_page_is_recognized_by_its_markup_not_its_address() -> None:
    found = identify_board(_CAREERS, page_url=_ROOT)

    assert found is not None
    assert found[0] == "deel"
    assert job_board_address(_ROOT) is False


def test_deel_reads_each_listed_posting_from_its_overview_page() -> None:
    requested: list[str] = []

    read = _fetcher(_PAGES, requested).listings("Deel", _ROOT)

    assert requested == [_ROOT, _ROOT, _overview(_ANALYST_ID), _overview(_LEAD_ID)]
    assert read.complete is True
    assert read.raw_count == 2
    analyst, lead = read.listings
    assert (analyst.title, analyst.url, analyst.requisition_id) == (
        "Payroll Data Analyst",
        _overview(_ANALYST_ID),
        _ANALYST_ID,
    )
    assert analyst.published_description == "Reconcile payroll runs across countries."
    assert analyst.facts.locations.values == ("Canada",)
    assert analyst.facts.compensation.values == ("$95,000 - $125,000 USD",)
    assert lead.facts.compensation.values == ()


def test_a_deel_overview_without_a_jobposting_is_not_staged() -> None:
    requested: list[str] = []
    pages = {**_PAGES, _overview(_LEAD_ID): "<html><body><p>Partnerships Lead</p></body></html>"}

    read = _fetcher(pages, requested).listings("Deel", _ROOT)

    assert read.complete is False
    assert [listing.requisition_id for listing in read.listings] == [_ANALYST_ID]


def test_a_deel_careers_page_without_its_job_listing_requests_no_posting() -> None:
    requested: list[str] = []
    page = f'<html><head><link rel="canonical" href="{_ROOT}"/></head><body></body></html>'
    fetcher = _fetcher({**_PAGES, _ROOT: page}, requested)

    with pytest.raises(ValueError, match="no job listing"):
        BOARD_ADAPTER_REGISTRY["deel"].adapter(fetcher, "Deel", {}, _ROOT)
    assert requested == [_ROOT]


def test_a_canonical_deel_link_on_another_host_requests_nothing_from_deel() -> None:
    requested: list[str] = []
    mirror = "https://mirror.example.org/careers/"

    with pytest.raises(ValueError, match="another host"):
        _fetcher({mirror: _CAREERS}, requested).listings("Deel", mirror)
    assert all("deel.com" not in url for url in requested)


def test_a_continued_deel_posting_reads_its_overview_page() -> None:
    requested: list[str] = []
    lead = _overview(_LEAD_ID)
    target = page_continuation_target(
        lead, source_url=_ROOT, label="Listed posting", reason="deel_posting_pending"
    )

    read = _fetcher(_PAGES, requested).continue_listings(
        "Deel", (target,), root_url=_ROOT, handoff_scopes=()
    )

    assert requested == [lead]
    assert [(listing.title, listing.requisition_id) for listing in read.listings] == [
        ("Partnerships Lead", _LEAD_ID)
    ]
