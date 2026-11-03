"""ClearCompany career sites page every opening, body included, from their public careers API."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from clearscout.infrastructure.web_fetch.board_adapters import (
    BoardApiFetcher,
    identify_board,
    identify_board_url,
)
from clearscout.infrastructure.web_fetch.boards import job_board_address
from clearscout.infrastructure.web_fetch.policy import RobotsDeniedError
from tests.support.http import policy_client

_SITE = "0f6c2a4e-8b1d-4e7a-9c3f-5d2b7a1e6c90"
_API = f"https://careers-api.clearcompany.com/v1/{_SITE}"
_SCRIPT = f"https://careers-content.clearcompany.com/js/v1/career-site.js?siteId={_SITE}"
_EMPLOYER_PAGE = (
    '<html><head><script src="https://careers.example-lab.org/theme.js"></script></head><body>'
    '<div id="cc-career-site"></div>'
    f'<script src="{_SCRIPT}"></script></body></html>'
)


_TENANT = "https://examplelab.clearcompany.com/careers/jobs"


def _job_id(number: int) -> str:
    return f"7a1b2c3d-0000-4000-8000-{number:012d}"


def _job(number: int, **stated: Any) -> dict[str, Any]:
    return {
        "id": _job_id(number),
        "positionTitle": f"Research Associate {number}",
        "description": f"<p>Run assays for study {number}.</p>",
        "postedDate": "2026-09-30T00:00:00Z",
        "applyLink": f"{_TENANT}/{_job_id(number)}/apply?source=CJB-0",
        "locations": [
            {
                "city": "Springfield",
                "subdivision": "MA",
                "country": "US",
                "isRemote": False,
                "isNationwide": False,
            }
        ],
        "location": "Springfield, MA",
        **stated,
    }


def _fetcher(
    pages: list[list[dict[str, Any]]], total: int, requested: list[str], *, robots: str = ""
) -> BoardApiFetcher:
    def answer(url: str) -> httpx.Response:
        requested.append(url)
        request = httpx.Request("GET", url)
        index = int(parse_qs(urlsplit(url).query)["pageIndex"][0])
        results = pages[index] if index < len(pages) else []
        body = {
            "results": results,
            "totalCount": total,
            "currentPageIndex": index,
            "currentPageCount": len(results),
        }
        return httpx.Response(200, request=request, json=body)

    return BoardApiFetcher(client=policy_client(answer, robots=robots))


def _page_indexes(requested: list[str]) -> list[int]:
    return [int(parse_qs(urlsplit(url).query)["pageIndex"][0]) for url in requested]


@pytest.mark.parametrize(
    "address",
    [
        _EMPLOYER_PAGE,
        f'<script src="https://careers-content.clearcompany.com/js/v1/'
        f'career-site-no-polyfill.js?siteId={_SITE}"></script>',
        _API,
    ],
    ids=["employer-page", "no-polyfill-script", "api-address"],
)
def test_a_clearcompany_site_is_recognized_by_its_script_or_api(address: str) -> None:
    found = identify_board(address)

    assert found is not None
    assert (found[0], found[1].group("site")) == ("clearcompany", _SITE)


@pytest.mark.parametrize(
    "address",
    [
        f"https://careers-content.clearcompany.com.example-lab.org/js/v1/career-site.js?siteId={_SITE}",
        f"https://careers-api.clearcompany.com.example-lab.org/v1/{_SITE}",
        f"https://notcareers-api.clearcompany.com/v1/{_SITE}",
    ],
    ids=["suffixed-script-host", "suffixed-api-host", "prefixed-api-host"],
)
def test_a_lookalike_host_names_no_clearcompany_site(address: str) -> None:
    found = identify_board_url(address)

    assert found is None or found[0] != "clearcompany"


def test_clearcompany_pages_until_its_stated_total_and_certifies_the_whole_board() -> None:
    requested: list[str] = []
    first = [_job(index) for index in range(50)]
    second = [_job(index) for index in range(50, 63)]

    read = _fetcher([first, second], 63, requested).listings("Example Labs", _API)

    assert _page_indexes(requested) == [0, 1]
    assert all(url.startswith(_API + "?") for url in requested)
    assert read.complete is True
    assert read.raw_count == 63
    listing = read.listings[0]
    assert (listing.title, listing.requisition_id) == ("Research Associate 0", _job(0)["id"])
    assert listing.url == f"{_TENANT}/{_job_id(0)}"
    assert listing.published_description == "Run assays for study 0."
    assert listing.facts.locations.values == ("Springfield, MA, US",)


@pytest.mark.parametrize(
    ("stated", "expected"),
    [
        pytest.param(
            {"salaryRangeLow": "85000", "salaryRangeHigh": "105000", "salaryType": "Per Year"},
            ("85000-105000 per year",),
            id="yearly-range",
        ),
        pytest.param(
            {"salaryRangeLow": "38.5", "salaryRangeHigh": "44", "salaryType": None},
            ("38.5-44",),
            id="no-period",
        ),
        pytest.param(
            {"salaryRangeLow": "0", "salaryRangeHigh": "0", "salaryType": "Per Year"},
            (),
            id="zero-means-none",
        ),
    ],
)
def test_a_clearcompany_salary_range_is_stated_without_a_guessed_currency(
    stated: dict[str, Any], expected: tuple[str, ...]
) -> None:
    fetcher = _fetcher([[_job(1, **stated)]], 1, [])

    [listing] = fetcher.listings("Example Lab", _API).listings

    assert listing.facts.compensation.values == expected


def test_a_clearcompany_row_without_a_usable_title_does_not_certify() -> None:
    requested: list[str] = []
    rows = [_job(0), _job(1, positionTitle="")]

    read = _fetcher([rows], 2, requested).listings("Example Labs", _API)

    assert read.complete is False
    assert [listing.requisition_id for listing in read.listings] == [_job_id(0)]


def test_a_clearcompany_row_stating_only_title_still_maps() -> None:
    requested: list[str] = []
    row = _job(0, title="Research Associate 0")
    del row["positionTitle"]

    read = _fetcher([[row]], 1, requested).listings("Example Labs", _API)

    assert read.complete is True
    assert read.listings[0].title == "Research Associate 0"


def test_a_remote_nationwide_clearcompany_job_reads_as_remote_not_a_bare_country() -> None:
    requested: list[str] = []
    row = _job(
        0,
        locations=[
            {
                "city": "",
                "subdivision": "",
                "country": "US",
                "isRemote": True,
                "isNationwide": True,
            }
        ],
        location="Remote (US)",
    )

    [listing] = _fetcher([[row]], 1, requested).listings("Example Labs", _API).listings

    assert listing.facts.arrangements.values == ("remote",)
    assert listing.facts.locations.values != ("US",)
    assert "Remote (US)" in listing.facts.locations.values


def test_a_clearcompany_posting_page_is_a_job_board_address() -> None:
    posting = f"{_TENANT}/{_job_id(0)}"

    assert job_board_address(posting) is True
    assert identify_board_url(posting) is None


def test_a_clearcompany_site_listing_fewer_jobs_than_its_total_is_not_complete() -> None:
    requested: list[str] = []
    first = [_job(index) for index in range(50)]

    read = _fetcher([first], 63, requested).listings("Example Labs", _API)

    assert _page_indexes(requested) == [0, 1]
    assert read.complete is False
    assert len(read.listings) == 50


def test_a_clearcompany_site_repeating_a_page_is_not_complete() -> None:
    requested: list[str] = []
    first = [_job(index) for index in range(50)]

    read = _fetcher([first, first], 63, requested).listings("Example Labs", _API)

    assert read.complete is False
    assert len({listing.requisition_id for listing in read.listings}) == 50


def test_an_empty_clearcompany_site_stating_zero_is_a_complete_empty_read() -> None:
    requested: list[str] = []

    read = _fetcher([], 0, requested).listings("Example Labs", _API)

    assert _page_indexes(requested) == [0]
    assert read.complete is True
    assert read.listings == []


def test_a_clearcompany_api_refused_by_robots_is_never_requested() -> None:
    requested: list[str] = []
    fetcher = _fetcher([[_job(0)]], 1, requested, robots="User-agent: *\nDisallow: /\n")

    with pytest.raises(RobotsDeniedError):
        fetcher.listings("Example Labs", _API)

    assert requested == []
