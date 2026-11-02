"""jPaginate portals are read page by page under their declared count, with a fresh search id."""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx
import pytest

from tailorcv.infrastructure.web_fetch.board_adapters import BoardApiFetcher, identify_board
from tests.support.http import policy_client

_BODY = (
    "<h3>Responsibilities</h3><p>Coordinate clinical reporting, review source records, "
    "and present governed findings to regional program teams across the zone.</p>"
    "<h3>Qualifications</h3><p>Use structured data, analytical reasoning, and clear writing. "
    "Work with reviewers to verify source evidence and communicate practical conclusions.</p>"
)


class _Portal:
    def __init__(self, pages: list[list[int]]) -> None:
        self.origin = "https://careers.example-health.test"
        self.root = self.origin + "/jobs/search/"
        self.pages = pages
        self.visits = 0
        self.requested: list[str] = []
        self.refused: set[int] = set()
        self.wrong_page: set[int] = set()
        self.stop_after_detail: int | None = None
        self.stop_after_page: int | None = None
        self.stopped = False
        self.counted = True
        self.total: int | None = None
        self.related: set[int] = set()
        self.slug = ""
        self.search_link = "/jobs/search/"
        self.fetcher = self.new_fetcher()

    def new_fetcher(self) -> BoardApiFetcher:
        site = self

        class Fetcher(BoardApiFetcher):
            @staticmethod
            def deadline_elapsed() -> bool:
                return site.stopped

        return Fetcher(client=policy_client(self.answer))

    @property
    def search_id(self) -> int:
        return 7000 + self.visits

    def job_url(self, number: int) -> str:
        return f"{self.origin}/jobs/clinical-data-analyst-{self.slug}{number}"

    def page(self, number: int, *, counted: bool = True) -> str:
        count = (
            f'<div id="jPaginateNumPages" class="ghost">{len(self.pages)}.0</div>'
            f'<div id="jPaginateCurrPage" class="ghost">{number}</div>'
        )
        rows = "".join(
            f'<div id="job_list_{job}" class="job_list_row">'
            f'<p><a href="{self.job_url(job)}" class="job_link">Clinical Data Analyst {job}</a></p>'
            '<p class="jlr_cat_loc"><span class="location"> Region One, Example City </span></p>'
            "</div>"
            for job in self.pages[number - 1]
        )
        total = f'<span class="total_results">{self.total}</span> results' if self.total else ""
        return (
            '<html><body><a href="https://elsewhere.example/jobs/other-role-1">Partner</a>'
            f'<a href="{self.origin}{self.search_link}">All jobs</a>'
            f'<a href="{self.origin}/jobs/featured-role-77">Featured role</a>'
            f"{rows}{count if counted else ''}{total}"
            '<script>$LAB.script("/job_list.js?v="+version);</script></body></html>'
        )

    def detail(self, number: int, path: str) -> str:
        body = _BODY
        related = ""
        if number in self.related:
            # The generic page reader calls this shape a collection of postings.
            body = "<h3>Your opportunity</h3><p>Coordinate clinical reporting for the zone.</p>"
            related = "<h2>Similar jobs</h2><ul>" + "".join(
                f'<li><a href="/jobs/regional-program-coordinator-{other}">'
                f"Regional Program Coordinator {other} - Example City</a></li>"
                for other in range(900, 908)
            )
            related += "</ul>"
        return (
            f"<html><head><title>Clinical Data Analyst {number}</title></head><body>"
            f'<h1 class="title">Clinical Data Analyst {number}</h1>'
            f'<div class="job_description">{body}</div>'
            f'<form action="{path}/apply"><button>Apply</button></form>{related}</body></html>'
        )

    def answer(self, url: str) -> httpx.Response:
        self.requested.append(url)
        request = httpx.Request("GET", url)
        path = urlsplit(url).path
        if path in {"", "/"}:
            return httpx.Response(200, request=request, text=self.page(1, counted=False))
        if path == "/jobs/search/":
            self.visits += 1
            return httpx.Response(
                302, request=request, headers={"Location": f"/jobs/search/{self.search_id}"}
            )
        if path == f"/jobs/search/{self.search_id}":
            return httpx.Response(200, request=request, text=self.page(1, counted=self.counted))
        prefix = f"/jobs/search/{self.search_id}/page"
        if path.startswith(prefix):
            number = int(path.removeprefix(prefix))
            if number in self.refused:
                return httpx.Response(403, request=request)
            shown = 1 if number in self.wrong_page else number
            self.stopped |= number == self.stop_after_page
            return httpx.Response(200, request=request, text=self.page(shown))
        if path.startswith("/jobs/search/"):
            # An expired search id answers its first page, never the page asked for.
            return httpx.Response(200, request=request, text=self.page(1))
        if path.startswith("/jobs/clinical-data-analyst-"):
            number = int(path.rsplit("-", 1)[1])
            if number == self.stop_after_detail:
                self.stopped = True
            return httpx.Response(
                200,
                request=request,
                text=self.detail(number, path),
            )
        return httpx.Response(404, request=request)


def test_a_short_posting_beside_a_similar_jobs_list_keeps_its_row_identity() -> None:
    site = _Portal([[1, 2]])
    site.related = {2}

    read = site.fetcher.listings("Example", site.root)

    assert read.complete is True
    assert [(row.requisition_id, row.title) for row in read.listings] == [
        ("1", "Clinical Data Analyst 1"),
        ("2", "Clinical Data Analyst 2"),
    ]
    assert "Coordinate clinical reporting for the zone." in read.listings[1].published_description
    assert "Regional Program Coordinator" not in read.listings[1].published_description


@pytest.mark.parametrize("slug", ["%E2%80%93-surgical-", "o%27neil-", "caf%C3%A9-"])
def test_a_row_whose_slug_carries_a_percent_encoded_character_is_read(slug: str) -> None:
    site = _Portal([[1, 2], [3]])
    site.slug = slug
    site.total = 3

    read = site.fetcher.listings("Example", site.root)

    assert read.complete is True
    assert sorted(row.requisition_id for row in read.listings) == ["1", "2", "3"]


@pytest.mark.parametrize(("total", "complete"), [(3, True), (4, False)])
def test_the_declared_result_count_is_the_stated_total(total: int, complete: bool) -> None:
    site = _Portal([[1, 2], [3]])
    site.total = total

    read = site.fetcher.listings("Example", site.root)

    assert read.complete is complete
    assert ("publisher_listing_total_mismatch" in read.skip_reasons) is not complete


@pytest.mark.parametrize("search_link", ["/jobs/search/", "/jobs/search"])
def test_the_portal_root_is_recognized_by_its_row_markup_and_not_by_a_lookalike(
    search_link: str,
) -> None:
    site = _Portal([[1]])
    site.search_link = search_link
    page = site.page(1, counted=False)

    found = identify_board(page)

    assert found is not None
    assert found[0] == "jpaginate"
    assert identify_board(page.replace("/job_list.js", "/app.js")) is None
    assert identify_board(page.replace(search_link, "/careers/")) is None
    rows_only = page.replace('<script>$LAB.script("/job_list.js?v="+version);</script>', "")
    assert identify_board(rows_only) is None


@pytest.mark.parametrize("suffix", ["", "/"])
def test_a_source_saved_at_the_site_root_reads_every_declared_page(suffix: str) -> None:
    site = _Portal([[1, 2], [3, 4], [5]])

    read = site.fetcher.listings("Example", site.origin + suffix)

    assert read.complete is True
    assert sorted(row.requisition_id for row in read.listings) == ["1", "2", "3", "4", "5"]
    assert f"{site.origin}/jobs/search/{site.search_id}/page3" in site.requested


def test_a_search_page_without_a_page_count_keeps_a_cursor_and_is_not_complete() -> None:
    site = _Portal([[1, 2], [3]])
    site.counted = False

    read = site.fetcher.listings("Example", site.origin + "/")

    assert read.complete is False
    assert [
        target.automatic_eligible
        for target in read.deferred_targets
        if target.deferred_reason == "jpaginate_search_page"
    ] == [False]


def test_every_declared_page_is_read_once_under_one_search_id() -> None:
    site = _Portal([[1, 2], [3, 4], [5]])

    read = site.fetcher.listings("Example", site.root)

    assert read.complete is True
    assert [(row.requisition_id, row.url, row.title) for row in read.listings] == [
        (str(job), site.job_url(job), f"Clinical Data Analyst {job}") for job in range(1, 6)
    ]
    assert {row.facts.locations.values for row in read.listings} == {("Region One, Example City",)}
    assert all("governed findings" in row.published_description for row in read.listings)
    searches = [url for url in site.requested if "/jobs/search/" in url]
    assert searches == [
        site.root,
        f"{site.origin}/jobs/search/7001",
        site.root,
        f"{site.origin}/jobs/search/7002",
        f"{site.origin}/jobs/search/7002/page2",
        f"{site.origin}/jobs/search/7002/page3",
    ]
    assert not any("elsewhere.example" in url for url in site.requested)


def test_a_job_repeated_across_pages_is_kept_once_and_the_read_is_not_certified() -> None:
    site = _Portal([[1, 2], [2, 3]])

    read = site.fetcher.listings("Example", site.root)

    assert sorted(row.requisition_id for row in read.listings) == ["1", "2", "3"]
    assert site.requested.count(site.job_url(2)) == 1
    assert read.complete is False
    assert "publisher_listing_changed" in read.skip_reasons


@pytest.mark.parametrize("failure", ["refused", "wrong_page", "empty"])
def test_an_unread_page_inside_the_declared_count_is_never_read_as_the_end(failure: str) -> None:
    site = _Portal([[1, 2], [3], [4]])
    if failure == "refused":
        site.refused = {2}
    elif failure == "wrong_page":
        site.wrong_page = {2}
    else:
        site.pages[1] = []

    read = site.fetcher.listings("Example", site.root)

    assert read.complete is False
    assert sorted(row.requisition_id for row in read.listings) == ["1", "2"]
    assert read.skip_reasons.get("publisher_search_request_failed") == 1
    cursors = [
        target
        for target in read.deferred_targets
        if target.deferred_reason == "jpaginate_search_page"
    ]
    assert len(cursors) == 1
    assert f"{site.origin}/jobs/search/7002/page3" not in site.requested


def test_a_cut_read_resumes_at_its_page_under_a_freshly_resolved_search_id() -> None:
    site = _Portal([[1, 2], [3, 4], [5, 6]])
    site.stop_after_page = 2

    first = site.fetcher.listings("Example", site.root)

    assert first.complete is False
    assert first.listings == []
    assert {
        target.url
        for target in first.deferred_targets
        if target.deferred_reason == "jpaginate_search_detail"
    } == {site.job_url(number) for number in (1, 2, 3, 4)}
    cursor = next(
        target
        for target in first.deferred_targets
        if target.deferred_reason == "jpaginate_search_page"
    )
    assert "7002" not in cursor.url
    site.stopped = False
    site.fetcher = site.new_fetcher()
    earlier = len(site.requested)

    resumed = site.fetcher.continue_listings(
        "Example", (cursor,), root_url=site.root, handoff_scopes=()
    )

    pages = [url for url in site.requested[earlier:] if "/page" in url]
    assert pages == [f"{site.origin}/jobs/search/{site.search_id}/page3"]
    assert site.search_id != 7002
    assert sorted(row.requisition_id for row in resumed.listings) == ["5", "6"]
    assert site.job_url(1) not in site.requested
    assert not resumed.deferred_targets
