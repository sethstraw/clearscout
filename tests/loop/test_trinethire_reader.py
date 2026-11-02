"""TriNet Hire boards list each opening in a rendered job table and state its body on its page."""

from __future__ import annotations

import httpx
import pytest

from tailorcv.infrastructure.web_fetch.board_adapters import (
    BOARD_ADAPTER_REGISTRY,
    BoardApiFetcher,
    identify_board,
    identify_board_url,
)
from tailorcv.infrastructure.web_fetch.boards import board_identity, canonicalise_source_url
from tests.support.http import policy_client

_ORIGIN = "https://app.trinethire.com"
_BOARD = f"{_ORIGIN}/companies/12345-example-neuro/jobs"
_ANALYST = f"{_BOARD}/4021-clinical-data-analyst"
_ENGINEER = f"{_BOARD}/4022-research-software-engineer"
_LIST_PAGE = (
    '<html><body><table class="job-list"><tbody>'
    '<tr class="job"><td><a href="/companies/12345-example-neuro/jobs/4021-clinical-data-analyst'
    '?ref=careers">Clinical Data Analyst</a></td><td>Springfield, MA</td></tr>'
    f'<tr class="job"><td><a href="{_ENGINEER}?source[name]=site">Research Software Engineer'
    "</a></td><td>Remote</td></tr>"
    "</tbody></table></body></html>"
)
_EMPTY_PAGE = (
    '<html><body><table class="job-list"><tbody>'
    '<tr class="no-job"><td>There are no open positions at this time.</td></tr>'
    "</tbody></table></body></html>"
)


def _detail(title: str, place: str, kind: str, body: str) -> str:
    return (
        '<html><body><div class="job-header">'
        f'<h3 class="job-name">{title}</h3>'
        f'<ul class="job-meta"><li>{place}</li><li>{kind}</li></ul></div>'
        f'<div class="job-descr content"><p>{body}</p></div></body></html>'
    )


_PAGES = {
    _BOARD: _LIST_PAGE,
    _ANALYST: _detail(
        "Clinical Data Analyst",
        "Springfield, MA",
        "Full Time",
        "Curate trial datasets for the neurology program.",
    ),
    _ENGINEER: _detail(
        "Research Software Engineer", "Remote", "Contract", "Build tools for imaging studies."
    ),
}


def _fetcher(pages: dict[str, str], requested: list[str]) -> BoardApiFetcher:
    def answer(url: str) -> httpx.Response:
        requested.append(url)
        request = httpx.Request("GET", url)
        if url not in pages:
            return httpx.Response(404, request=request)
        return httpx.Response(200, request=request, text=pages[url])

    return BoardApiFetcher(client=policy_client(answer))


@pytest.mark.parametrize(
    "address",
    [
        _BOARD,
        f'<iframe src="{_ORIGIN}/companies/12345-example-neuro/jobs/embed" width="100%"></iframe>',
    ],
    ids=["board-url", "embedded-board"],
)
def test_a_trinethire_board_is_recognized_by_its_address_or_embed(address: str) -> None:
    found = identify_board(address)

    assert found is not None
    assert (found[0], found[1].group("company"), found[1].group("org")) == (
        "trinethire",
        "12345",
        "example-neuro",
    )


def test_a_trinethire_posting_folds_to_its_board_root() -> None:
    assert canonicalise_source_url(_ANALYST + "?ref=careers") == _BOARD


@pytest.mark.parametrize(
    "address",
    [
        "https://app.trinethire.com.example-lab.org/companies/12345-example-neuro/jobs",
        "https://notapp.trinethire.com/companies/12345-example-neuro/jobs",
        "https://careers.example-lab.org/companies/12345-example-neuro/jobs",
    ],
    ids=["suffixed-host", "prefixed-host", "employer-host"],
)
def test_a_lookalike_host_names_no_trinethire_board(address: str) -> None:
    found = identify_board_url(address)

    assert found is None or found[0] != "trinethire"


def test_trinethire_reads_each_listed_opening_with_its_page_body() -> None:
    requested: list[str] = []

    read = _fetcher(_PAGES, requested).listings("Example Neuro", _BOARD)

    assert requested == [_BOARD, _ANALYST, _ENGINEER]
    assert read.complete is True
    assert read.raw_count == 2
    analyst, engineer = read.listings
    assert (analyst.title, analyst.url, analyst.requisition_id) == (
        "Clinical Data Analyst",
        _ANALYST,
        "4021",
    )
    assert analyst.published_description == "Curate trial datasets for the neurology program."
    assert analyst.facts.locations.values == ("Springfield, MA",)
    assert analyst.facts.employment_types.values == ("Full Time",)
    assert (engineer.title, engineer.requisition_id) == ("Research Software Engineer", "4022")
    assert engineer.facts.employment_types.values == ("Contract",)


def test_an_embedded_trinethire_board_reads_the_board_it_frames() -> None:
    requested: list[str] = []
    embed = f"{_BOARD}/embed"

    read = _fetcher(_PAGES, requested).listings("Example Neuro", embed)

    assert requested[0] == _BOARD
    assert [listing.requisition_id for listing in read.listings] == ["4021", "4022"]


def test_a_trinethire_board_stating_no_openings_is_a_complete_empty_read() -> None:
    requested: list[str] = []

    read = _fetcher({_BOARD: _EMPTY_PAGE}, requested).listings("Example Neuro", _BOARD)

    assert requested == [_BOARD]
    assert read.complete is True
    assert read.listings == []


def test_a_trinethire_page_without_its_job_table_is_not_an_empty_board() -> None:
    requested: list[str] = []
    page = "<html><body><p>Something went wrong.</p></body></html>"

    read = _fetcher({_BOARD: page}, requested).listings("Example Neuro", _BOARD)

    assert read.complete is False
    assert read.listings == []


def test_a_trinethire_detail_without_a_body_is_not_staged_as_complete() -> None:
    requested: list[str] = []
    pages = {**_PAGES, _ENGINEER: "<html><body><h3 class='job-name'>Engineer</h3></body></html>"}

    read = _fetcher(pages, requested).listings("Example Neuro", _BOARD)

    assert read.complete is False
    assert [
        listing.requisition_id for listing in read.listings if listing.published_description
    ] == ["4021"]


def test_trinethire_resumes_its_own_unread_posting_pages() -> None:
    assert "trinethire_posting_pending" in BOARD_ADAPTER_REGISTRY["trinethire"].continuations


def _table(*rows: str) -> str:
    return (
        f'<html><body><table class="job-list"><tbody>{"".join(rows)}</tbody></table></body></html>'
    )


def test_a_trinethire_row_linking_another_slug_of_the_same_company_is_read() -> None:
    requested: list[str] = []
    row = (
        '<tr class="job"><td><a href="/companies/12345-example-neuro-inc/jobs/'
        '4021-clinical-data-analyst">Clinical Data Analyst</a></td></tr>'
    )
    other = f"{_ORIGIN}/companies/12345-example-neuro-inc/jobs/4021-clinical-data-analyst"
    pages = {_BOARD: _table(row), other: _PAGES[_ANALYST]}

    read = _fetcher(pages, requested).listings("Example Neuro", _BOARD)

    assert read.complete is True
    assert [listing.requisition_id for listing in read.listings] == ["4021"]


@pytest.mark.parametrize(
    "stray",
    [
        '<tr class="job"><td>Clinical Data Analyst</td></tr>',
        '<tr class="job"><td><a href="/companies/99999-example-other/jobs/4021-x">X</a></td></tr>',
    ],
    ids=["no-link", "other-company"],
)
def test_a_trinethire_job_row_that_cannot_be_read_keeps_the_board_uncertified(stray: str) -> None:
    requested: list[str] = []
    row = f'<tr class="job"><td><a href="{_ENGINEER}">Research Software Engineer</a></td></tr>'
    pages = {**_PAGES, _BOARD: _table(row, stray)}

    read = _fetcher(pages, requested).listings("Example Neuro", _BOARD)

    assert read.complete is False
    assert [listing.requisition_id for listing in read.listings] == ["4022"]


@pytest.mark.parametrize(
    "paging",
    [
        '<ul class="pagination"><li><a href="#">2</a></li></ul>',
        '<link rel="next" href="https://app.trinethire.com/companies/12345-example-neuro/jobs/p2">',
        '<a href="/companies/12345-example-neuro/jobs?page=2">Next</a>',
    ],
    ids=["pagination-class", "rel-next", "page-query"],
)
def test_a_trinethire_table_with_paging_is_not_the_whole_board(paging: str) -> None:
    requested: list[str] = []
    pages = {**_PAGES, _BOARD: _LIST_PAGE.replace("</body>", paging + "</body>")}

    read = _fetcher(pages, requested).listings("Example Neuro", _BOARD)

    assert read.complete is False
    assert "paging" in read.note
    assert [listing.requisition_id for listing in read.listings] == ["4021", "4022"]


def test_two_slugs_of_one_trinethire_company_are_one_source() -> None:
    first = board_identity(_BOARD)
    renamed = board_identity(f"{_ORIGIN}/companies/12345-example-neuro-inc/jobs")

    assert first is not None
    assert first == renamed == ("trinethire", "12345")
