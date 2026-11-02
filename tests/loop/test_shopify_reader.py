"""Shopify's careers page streams its posting list, and each posting page streams its body."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tailorcv.domain.countries import DEFAULT_TABLE
from tailorcv.domain.places import PlaceReader
from tailorcv.infrastructure.web_fetch.board_adapters import (
    BOARD_ADAPTER_REGISTRY,
    BoardApiFetcher,
    identify_board,
)
from tailorcv.infrastructure.web_fetch.board_reads import page_continuation_target
from tailorcv.infrastructure.web_fetch.boards import job_board_address
from tests.support.http import policy_client

_ROOT = "https://www.shopify.com/careers"
_ANALYST_ID = "0b6f4a1e-2c3d-4e5f-8a9b-1c2d3e4f5a6b"
_WRITER_ID = "7d8e9f0a-1b2c-4d3e-9f4a-5b6c7d8e9f0a"
_CLOSED_ID = "c1d2e3f4-a5b6-4c7d-8e9f-0a1b2c3d4e5f"
_ANALYST = f"{_ROOT}/data-analyst-merchant-insights_{_ANALYST_ID}"
_WRITER = f"{_ROOT}/technical-writer_{_WRITER_ID}"
_TORONTO, _REMOTE = "5e6f7a8b-9c0d-4e1f-a2b3-c4d5e6f7a8b9", "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
_LOCATIONS: list[dict[str, Any]] = [
    {"id": _TORONTO, "name": "Toronto Office", "externalName": "Toronto, Canada"},
    {"id": _REMOTE, "name": "Americas", "externalName": None},
]


def _stream(value: Any) -> list[Any]:
    """Flatten a value the way turbo-stream does: containers hold indexes into one table."""
    table: list[Any] = []

    def put(item: Any) -> int:
        if item is None:
            return -5
        index = len(table)
        table.append(None)
        if isinstance(item, dict):
            table[index] = {f"_{put(key)}": put(entry) for key, entry in item.items()}
        elif isinstance(item, list):
            table[index] = [put(entry) for entry in item]
        else:
            table[index] = item
        return index

    put(value)
    return table


def _page(canonical: str, loaders: dict[str, Any]) -> str:
    payload = json.dumps(json.dumps(_stream({"loaderData": loaders, "actionData": None})))
    return (
        f'<html><head><link rel="canonical" href="{canonical}"/></head><body>'
        f"<script>window.__reactRouterContext.streamController.enqueue({payload});</script>"
        "</body></html>"
    )


def _listed(identity: str, title: str, *, listed: bool = True) -> dict[str, Any]:
    return {"jobPosting": {"id": identity, "title": title, "isListed": listed}, "job": {}}


_CAREERS = _page(
    _ROOT,
    {
        "root": {"url": _ROOT},
        "($locale)/careers": {
            "jobPostingsWithJobs": [
                _listed(_ANALYST_ID, "Data Analyst, Merchant Insights"),
                _listed(_CLOSED_ID, "Retired Role", listed=False),
                _listed(_WRITER_ID, "Technical Writer"),
            ],
            "atsLocations": _LOCATIONS,
        },
    },
)


def _posting(
    identity: str,
    title: str,
    body: str,
    places: tuple[str, ...],
    *,
    pay: str | None = None,
    locations: list[dict[str, Any]] = _LOCATIONS,
) -> str:
    compensation = {
        "compensationTierSummary": pay,
        "displaySettings": {"shouldShowOnJobBoard": pay is not None},
    }
    return _page(
        f"{_ROOT}/x_{identity}",
        {
            "($locale)/careers/$posting": {
                "jobPosting": {
                    "id": identity,
                    "title": title,
                    "isListed": True,
                    "descriptionHtml": f"<p>{body}</p>",
                    "employmentType": "FullTime",
                    "locationIds": {
                        "primaryLocationId": places[0],
                        "secondaryLocationIds": list(places[1:]),
                    },
                    "compensation": compensation,
                    "applicationFormDefinition": {
                        "sections": [{"fields": [{"descriptionHtml": "<p>Upload a resume</p>"}]}]
                    },
                },
                "locations": locations,
            }
        },
    )


_PAGES = {
    _ROOT: _CAREERS,
    _ANALYST: _posting(
        _ANALYST_ID,
        "Data Analyst, Merchant Insights",
        "Model merchant retention for the insights team.",
        (_TORONTO, _REMOTE),
        pay="$90,000 - $120,000 CAD",
    ),
    _WRITER: _posting(_WRITER_ID, "Technical Writer", "Document the partner APIs.", (_REMOTE,)),
}


def _fetcher(pages: dict[str, str], requested: list[str]) -> BoardApiFetcher:
    def answer(url: str) -> httpx.Response:
        requested.append(url)
        request = httpx.Request("GET", url)
        if url not in pages:
            return httpx.Response(404, request=request)
        return httpx.Response(200, request=request, text=pages[url])

    return BoardApiFetcher(client=policy_client(answer))


def test_the_shopify_careers_page_is_recognized_by_its_markup_not_its_address() -> None:
    found = identify_board(_CAREERS, page_url=_ROOT)

    assert found is not None
    assert found[0] == "shopify"
    assert job_board_address(_ROOT) is False


def test_shopify_reads_each_listed_posting_with_its_streamed_body() -> None:
    requested: list[str] = []

    read = _fetcher(_PAGES, requested).listings("Shopify", _ROOT)

    assert requested == [_ROOT, _ROOT, _ANALYST, _WRITER]
    assert read.complete is True
    assert read.raw_count == 2
    analyst, writer = read.listings
    assert (analyst.title, analyst.url, analyst.requisition_id) == (
        "Data Analyst, Merchant Insights",
        _ANALYST,
        _ANALYST_ID,
    )
    assert analyst.published_description == "Model merchant retention for the insights team."
    assert analyst.facts.locations.values == ("Toronto, Canada", "Americas")
    assert analyst.facts.compensation.values == ("$90,000 - $120,000 CAD",)
    assert writer.facts.locations.values == ("Americas",)
    assert writer.facts.compensation.values == ()


def test_a_shopify_posting_page_without_its_route_loader_is_not_staged() -> None:
    requested: list[str] = []
    pages = {**_PAGES, _WRITER: "<html><body><p>Technical Writer</p></body></html>"}

    read = _fetcher(pages, requested).listings("Shopify", _ROOT)

    assert read.complete is False
    assert [listing.requisition_id for listing in read.listings] == [_ANALYST_ID]


@pytest.mark.parametrize(
    "page",
    [
        "<html><body><p>Careers</p></body></html>",
        _page(_ROOT, {"($locale)/careers": {"atsLocations": _LOCATIONS}}),
    ],
    ids=["no-stream", "no-posting-list"],
)
def test_a_shopify_careers_page_without_its_posting_list_reads_nothing(page: str) -> None:
    requested: list[str] = []
    fetcher = _fetcher({**_PAGES, _ROOT: page}, requested)

    with pytest.raises(ValueError, match="Shopify"):
        BOARD_ADAPTER_REGISTRY["shopify"].adapter(fetcher, "Shopify", {}, _ROOT)
    assert requested == [_ROOT]


def test_a_canonical_shopify_link_on_another_host_requests_nothing_from_shopify() -> None:
    requested: list[str] = []
    mirror = "https://mirror.example.org/careers"

    with pytest.raises(ValueError, match="another host"):
        _fetcher({mirror: _CAREERS}, requested).listings("Shopify", mirror)
    assert all(not url.startswith("https://www.shopify.com/") for url in requested)


def test_a_continued_shopify_posting_reads_its_streamed_page() -> None:
    requested: list[str] = []
    target = page_continuation_target(
        _WRITER, source_url=_ROOT, label="Listed posting", reason="shopify_posting_pending"
    )

    read = _fetcher(_PAGES, requested).continue_listings(
        "Shopify", (target,), root_url=_ROOT, handoff_scopes=()
    )

    assert requested == [_WRITER]
    assert [(listing.title, listing.requisition_id) for listing in read.listings] == [
        ("Technical Writer", _WRITER_ID)
    ]
    assert read.listings[0].facts.locations.values == ("Americas",)


def test_a_region_wide_shopify_location_names_the_countries_filed_under_it() -> None:
    hierarchy, office = (
        "1f2e3d4c-5b6a-4978-8a6b-5c4d3e2f1a0b",
        "2a3b4c5d-6e7f-4a8b-9c0d-1e2f3a4b5c6d",
    )
    office_us = "3b4c5d6e-7f8a-4b9c-8d1e-2f3a4b5c6d7e"
    locations: list[dict[str, Any]] = [
        {"id": hierarchy, "name": "Americas", "type": "LocationHierarchy", "address": None},
        {
            "id": _REMOTE,
            "name": "Americas",
            "type": "Location",
            "isRemote": True,
            "parentLocationId": hierarchy,
            "address": {"postalAddress": {"addressCountry": "Americas"}},
        },
        {
            "id": office,
            "name": "Canada ",
            "type": "Location",
            "parentLocationId": hierarchy,
            "address": {"postalAddress": {"addressCountry": "Canada"}},
        },
        {
            "id": office_us,
            "name": "New York City",
            "type": "Location",
            "parentLocationId": hierarchy,
            "address": {"postalAddress": {"addressCountry": "United States"}},
        },
    ]
    target = page_continuation_target(
        _WRITER, source_url=_ROOT, label="Listed posting", reason="shopify_posting_pending"
    )
    pages = {
        _WRITER: _posting(
            _WRITER_ID,
            "Technical Writer",
            "Document the partner APIs.",
            (_REMOTE,),
            locations=locations,
        )
    }

    read = _fetcher(pages, []).continue_listings(
        "Shopify", (target,), root_url=_ROOT, handoff_scopes=()
    )

    (place,) = read.listings[0].facts.locations.values
    assert place == "Americas (Canada, United States)"
    assert "Canada" in PlaceReader(DEFAULT_TABLE).read(place).countries
