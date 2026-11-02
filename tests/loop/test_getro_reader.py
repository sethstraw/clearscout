"""Getro talent-network boards read every portfolio opening from the page's embedded state."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tailorcv.infrastructure.web_fetch.board_adapters import (
    BoardApiFetcher,
    identify_board,
    identify_board_url,
)
from tailorcv.infrastructure.web_fetch.boards import board_identity
from tailorcv.infrastructure.web_fetch.policy import RobotsDeniedError
from tests.support.http import policy_client

_BOARD = "https://careers.example-ventures.com/jobs"
_HOSTED = "https://example-ventures.getro.com/jobs"


def _job(number: int, **stated: Any) -> dict[str, Any]:
    return {
        "id": 57000000 + number,
        "title": f"Director, Example Programs {number}",
        "slug": f"{57000000 + number}-director-example",
        "url": f"https://boards.greenhouse.io/examplebio/jobs/{4100 + number}",
        "locations": ["Boston, MA, USA"],
        "organization": {"id": 900, "name": "Example Bio", "slug": "example-bio"},
        "workMode": "on_site",
        "hasDescription": True,
        **stated,
    }


def _state(jobs: list[dict[str, Any]], total: int, *, lazy: bool = False) -> dict[str, Any]:
    return {
        "props": {
            "pageProps": {
                "network": {"id": "4242", "name": "Example Ventures"},
                "initialState": {
                    "jobs": {
                        "found": jobs,
                        "total": total,
                        "initialized": True,
                        "loading": False,
                        "isLazyLoading": lazy,
                        "isFilterLoading": False,
                    },
                    "companies": {"found": [{"name": "Example Bio"}], "total": 1},
                },
            }
        },
        "page": "/jobs",
    }


def _page(state: dict[str, Any] | str) -> str:
    body = state if isinstance(state, str) else json.dumps(state)
    return (
        "<html><head></head><body><div id='__next'></div>"
        f'<script id="__NEXT_DATA__" type="application/json">{body}</script></body></html>'
    )


def _fetcher(page: str, requested: list[str], *, robots: str = "") -> BoardApiFetcher:
    def answer(url: str) -> httpx.Response:
        requested.append(url)
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            content=page.encode(),
            headers={"Content-Type": "text/html"},
        )

    return BoardApiFetcher(client=policy_client(answer, robots=robots))


def test_a_getro_board_page_is_recognized_by_its_embedded_jobs_state() -> None:
    found = identify_board(_page(_state([_job(0)], 1)))

    assert found is not None
    assert (found[0], found[1].group("network")) == ("getro", "4242")


def test_a_hosted_getro_network_address_names_its_board() -> None:
    found = identify_board_url(_HOSTED)

    assert found is not None
    assert found[0] == "getro"
    assert board_identity(_HOSTED) == ("getro", "example-ventures")


@pytest.mark.parametrize(
    "page",
    [
        _page({"props": {"pageProps": {"initialJobs": []}}, "page": "/careers"}),
        _page({"props": {"pageProps": {"network": {"id": "4242"}}}, "page": "/"}),
        _page({"props": {"pageProps": {"initialState": {"jobs": {"found": []}}}}}),
    ],
    ids=["generic-next-page", "network-without-jobs", "jobs-without-network"],
)
def test_a_next_page_without_getro_jobs_state_is_not_a_getro_board(page: str) -> None:
    found = identify_board(page)

    assert found is None or found[0] != "getro"


@pytest.mark.parametrize(
    "address",
    ["https://api.getro.com/jobs", "https://example-ventures.getro.com.example.org/jobs"],
    ids=["api-host", "suffixed-host"],
)
def test_a_getro_service_or_lookalike_host_names_no_board(address: str) -> None:
    found = identify_board_url(address)

    assert found is None or found[0] != "getro"


def test_a_getro_page_stating_every_job_certifies_the_whole_board() -> None:
    requested: list[str] = []
    jobs = [_job(index) for index in range(18)]

    read = _fetcher(_page(_state(jobs, 18)), requested).listings("Example Ventures", _BOARD)

    assert not any("api.getro.com" in url for url in requested)
    assert read.complete is True
    assert read.raw_count == 18
    listing = read.listings[0]
    assert listing.title == "Director, Example Programs 0"
    assert listing.url == "https://boards.greenhouse.io/examplebio/jobs/4100"
    assert listing.employer == "Example Bio"
    assert listing.facts.locations.values == ("Boston, MA, USA",)


def test_a_getro_page_shipping_fewer_jobs_than_its_total_is_short() -> None:
    requested: list[str] = []
    jobs = [_job(index) for index in range(20)]

    read = _fetcher(_page(_state(jobs, 140)), requested).listings("Example Ventures", _BOARD)

    assert read.complete is False
    assert len(read.listings) == 20
    assert read.raw_count == 140
    assert "140" in read.note
    assert "missing_required_posting_identity" not in read.skip_reasons


def test_a_getro_page_still_lazy_loading_is_not_certified() -> None:
    requested: list[str] = []
    jobs = [_job(index) for index in range(3)]

    read = _fetcher(_page(_state(jobs, 3, lazy=True)), requested).listings(
        "Example Ventures", _BOARD
    )

    assert read.complete is False
    assert len(read.listings) == 3


def test_a_hosted_getro_board_with_malformed_state_is_a_short_empty_read() -> None:
    requested: list[str] = []
    page = _page('{"props":{"pageProps":{"network":{"id":"4242"},"initialState":{"jobs":{')

    read = _fetcher(page, requested).listings("Example Ventures", _HOSTED)

    assert requested == [_HOSTED]
    assert read.complete is False
    assert read.listings == []
    assert read.note


def test_a_remote_getro_work_mode_reads_as_remote() -> None:
    requested: list[str] = []
    jobs = [_job(0, workMode="remote"), _job(1)]

    read = _fetcher(_page(_state(jobs, 2)), requested).listings("Example Ventures", _BOARD)

    assert [listing.facts.arrangements.values for listing in read.listings] == [("remote",), ()]


def test_a_getro_job_detail_page_with_an_uninitialized_list_is_not_certified() -> None:
    requested: list[str] = []
    state = _state([], 0)
    state["props"]["pageProps"]["initialState"]["jobs"]["initialized"] = False

    read = _fetcher(_page(state), requested).listings("Example Ventures", _BOARD)

    assert read.complete is False
    assert read.listings == []


def test_a_getro_page_embedding_a_hibob_posting_is_still_a_getro_board() -> None:
    posting = "https://xanadu.careers.hibob.com/jobs/0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"

    found = identify_board(_page(_state([_job(0, url=posting)], 1)))

    assert found is not None
    assert found[0] == "getro"


@pytest.mark.parametrize(
    "address",
    [
        "https://example-ventures.getro.com/jobsearch",
        "https://app.getro.com/jobs",
        "https://jobs.getro.com/jobs",
    ],
    ids=["path-prefix", "app-host", "jobs-host"],
)
def test_a_getro_address_off_the_jobs_path_or_on_a_service_host_names_no_board(
    address: str,
) -> None:
    found = identify_board_url(address)

    assert found is None or found[0] != "getro"


def test_a_hosted_getro_page_whose_props_are_not_an_object_is_a_short_read() -> None:
    requested: list[str] = []

    read = _fetcher(_page('{"props": []}'), requested).listings("Example Ventures", _HOSTED)

    assert read.complete is False
    assert read.listings == []


def test_a_getro_board_refused_by_robots_is_never_requested() -> None:
    requested: list[str] = []
    fetcher = _fetcher(
        _page(_state([_job(0)], 1)), requested, robots="User-agent: *\nDisallow: /\n"
    )

    with pytest.raises(RobotsDeniedError):
        fetcher.listings("Example Ventures", _HOSTED)

    assert requested == []
