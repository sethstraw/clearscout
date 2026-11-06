"""Publisher-declared search pages retain scope, identities, and detail outcomes."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from clearscout.application.discovery.source_reading import (
    SourceContinuationTarget,
    SourceDetailCheckpoint,
    SourceRead,
    SourceReadOptions,
)
from clearscout.domain.records import SourceReadOutcome
from clearscout.infrastructure.web_fetch.board_adapters import BoardApiFetcher
from clearscout.infrastructure.web_fetch.board_reads import search_page_number
from clearscout.infrastructure.web_fetch.policy import RobotsDeniedError
from tests.support.doubles import declaring_page
from tests.support.http import policy_client

_BODY = "Review clinical source records, document methods, and maintain governed reporting. " * 8


@pytest.mark.parametrize("failed_page", [None, 1])
def test_alignerr_enumerates_exact_membership_before_any_body_request(
    failed_page: int | None,
) -> None:
    site = _SearchSite("alignerr", scope="?category=Research&search=data")
    site.fail_page = failed_page
    read = site.fetcher.listings("Acme", site.root)
    assert site.detail_reads == 0
    expected = (1, 2, 3) if failed_page is None else (1, 2)
    assert [(row.requisition_id, row.url) for row in read.listings] == [
        (str(number), site.detail_url(number)) for number in expected
    ]
    assert all(not row.published_description for row in read.listings)
    assert all(row.facts.publisher_vouched and row.source is not None for row in read.listings)
    details = [
        target for target in read.deferred_targets if target.deferred_reason.endswith("_detail")
    ]
    assert {target.url for target in details} == {site.detail_url(number) for number in expected}
    assert all(
        target.detail_checkpoint and target.detail_checkpoint.listing_facts for target in details
    )
    if failed_page is None:
        assert read.listing_coverage is not None
        assert read.listing_coverage.posting_urls == tuple(
            site.detail_url(number) for number in expected
        )
        assert read.exhausted_outcome is not None
    else:
        cursor = next(
            target for target in read.deferred_targets if target.deferred_reason.endswith("_page")
        )
        assert cursor.detail_checkpoint is not None
        assert dict(cursor.detail_checkpoint.listing_urls) == {
            str(number): site.detail_url(number) for number in expected
        }
        site.fail_page = None
        resumed = site.fetcher.continue_listings(
            "Acme", (cursor,), root_url=site.root, handoff_scopes=()
        )
        assert site.detail_reads == 0
        assert resumed.listing_coverage is not None
        assert set(resumed.listing_coverage.posting_urls) == {
            site.detail_url(number) for number in (1, 2, 3)
        }
        assert resumed.raw_count == 3


class _SearchSite:
    def __init__(self, protocol: str, host: str = "acme.example", scope: str = "") -> None:
        self.protocol = protocol
        self.origin = f"https://{host}"
        self.root = self.origin + ("/careers/" if protocol == "tbk" else "/jobs") + scope
        self.pages: list[list[int]] = [[1, 2], [3], []]
        self.totals = [3, 3, 3]
        self.requested: list[str] = []
        self.api_queries: list[dict[str, list[str]]] = []
        self.failed_details: set[int] = set()
        self.fail_page: int | None = None
        self.stopped = False
        self.interrupt_after: int | None = None
        self.stop_after_pages: int | None = None
        self.detail_reads = 0
        self.foreign_api = False
        self.foreign_script = False
        self.private_method = False
        site = self

        class Fetcher(BoardApiFetcher):
            @staticmethod
            def deadline_elapsed() -> bool:
                return site.stopped

        self.fetcher = Fetcher(client=policy_client(self.answer))

    def detail_url(self, number: int) -> str:
        return f"{self.origin}/jobs/role-{number}?locale=fr"

    def markup(self) -> str:
        if self.protocol == "tbk":
            base = (
                "https://other.example" if self.foreign_api else self.origin
            ) + "/app/themes/tbk-ba3e-theme/api"
            script = (
                "https://other.example" if self.foreign_script else self.origin
            ) + "/app/mu-plugins/tbk-gutenberg/build-front/front.jsx.js"
            return (
                '<div data-component="CareersIndex" data-cid="careers-index"></div>'
                "<script>var TBK_GUTENBERG = "
                + json.dumps({"apiBase": base})
                + ";var TBK_APP_CONTEXT = "
                + json.dumps(
                    {
                        "api_map": [
                            {
                                "function": "getFilteredJobs",
                                "method": "GET",
                                "route": "/wp/career-search",
                                "anonymous": not self.private_method,
                            }
                        ]
                    }
                )
                + ";var TBK_HYDRATE_COMPONENTS = "
                + json.dumps(
                    {
                        "careers-index": {
                            "perPage": 10,
                            "defaultFilters": [
                                {"taxonomy": "department"},
                                {"taxonomy": "location"},
                            ],
                        }
                    }
                )
                + f';</script><script src="{script}"></script>'
            )
        script = (
            "https://other.example" if self.foreign_script else self.origin
        ) + "/_next/static/chunks/pages/jobs-synthetic.js"
        return (
            '<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(
                {
                    "props": {"pageProps": {"initialJobs": [], "initialTotal": 3}},
                    "page": "/jobs",
                    "query": {},
                }
            )
            + f'</script><script src="{script}"></script>'
        )

    def row(self, number: int) -> dict[str, Any]:
        if self.protocol == "tbk":
            return {
                "ID": number,
                "careerTitle": f"Data Analyst {number}",
                "careerLink": {"href": self.detail_url(number)},
                "careerLocation": [{"name": "Remote, Canada"}],
            }
        return {
            "id": str(number),
            "title": f"Data Analyst {number}",
            "applyUrl": self.detail_url(number),
            "location": "Remote, Canada",
            "description": "A summary that cannot establish a complete posting body...",
        }

    def answer(self, url: str) -> httpx.Response:
        self.requested.append(url)
        parsed = urlsplit(url)
        if parsed.netloc != urlsplit(self.origin).netloc:
            return httpx.Response(404, request=httpx.Request("GET", url))
        if parsed.path in {"/careers/", "/jobs"}:
            return httpx.Response(200, request=httpx.Request("GET", url), text=self.markup())
        if parsed.path.endswith("front.jsx.js"):
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                text="window.TBK_GUTENBERG; window.tbk_gutenberg_render;",
            )
        if parsed.path.endswith("jobs-synthetic.js"):
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                text=(
                    "new URLSearchParams({limit:String(60),offset:String(e)});"
                    'fetch("/api/jobs?".concat(params));'
                ),
            )
        if parsed.path.endswith("/career-search") or parsed.path == "/api/jobs":
            query = parse_qs(parsed.query)
            self.api_queries.append(query)
            offset = int(query.get("offset", ["0"])[0])
            if self.protocol == "tbk":
                page = int(query.get("currentPage", ["1"])[0]) - 1
            else:
                page = len(self.pages)
                position = 0
                for index, numbers in enumerate(self.pages):
                    if position == offset:
                        page = index
                        break
                    position += len(numbers)
            if page == self.fail_page:
                return httpx.Response(503, request=httpx.Request("GET", url))
            rows = (
                [self.row(number) for number in self.pages[page]] if page < len(self.pages) else []
            )
            total = self.totals[min(page, len(self.totals) - 1)]
            payload = (
                {"success": True, "code": 200, "data": {"cards": rows, "total": total}}
                if self.protocol == "tbk"
                else {"jobs": rows, "total": total, "offset": offset, "limit": 60}
            )
            if self.stop_after_pages == len(self.api_queries):
                self.stopped = True
                self.stop_after_pages = None
            return httpx.Response(200, request=httpx.Request("GET", url), json=payload)
        if parsed.path.startswith("/jobs/role-"):
            number = int(parsed.path.removeprefix("/jobs/role-"))
            self.detail_reads += 1
            if self.interrupt_after == self.detail_reads:
                self.stopped = True
                self.interrupt_after = None
            if number in self.failed_details:
                return httpx.Response(404, request=httpx.Request("GET", url), text="Unavailable")
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                text=declaring_page(
                    {
                        "@type": "JobPosting",
                        "title": f"Data Analyst {number}",
                        "description": _BODY,
                        "hiringOrganization": {"name": "Acme"},
                    }
                ),
            )
        return httpx.Response(404, request=httpx.Request("GET", url))


@pytest.mark.parametrize("protocol", ["tbk", "alignerr"])
@pytest.mark.parametrize("host", ["acme.example", "globex.example"])
@pytest.mark.parametrize("moving_total", [False, True])
def test_declared_search_pages_keep_scope_and_continue_after_short_pages_and_moving_totals(
    protocol: str,
    host: str,
    moving_total: bool,
) -> None:
    scope = (
        "#!department=research&location=remote"
        if protocol == "tbk"
        else "?category=Research&search=data"
    )
    site = _SearchSite(protocol, host, scope)
    if moving_total:
        site.totals = [2, 8, 3]

    read = site.fetcher.listings("Acme", site.root)

    assert read.complete is (protocol == "tbk" and not moving_total)
    assert read.discovery_truncated is moving_total
    assert read.raw_count == 3
    assert [(row.url, row.requisition_id) for row in read.listings] == [
        (site.detail_url(number), str(number)) for number in (1, 2, 3)
    ]
    assert {row.published_description for row in read.listings} == (
        {_BODY.strip()} if protocol == "tbk" else {""}
    )
    if protocol == "alignerr":
        assert site.detail_reads == 0
        details = tuple(
            target
            for target in read.deferred_targets
            if target.deferred_reason == "alignerr_search_detail"
        )
        recovered = site.fetcher.continue_listings(
            "Acme", details, root_url=site.root, handoff_scopes=()
        )
        assert [(row.url, row.requisition_id) for row in recovered.listings] == [
            (site.detail_url(number), str(number)) for number in (1, 2, 3)
        ]
        assert {row.published_description for row in recovered.listings} == {_BODY.strip()}
        assert not recovered.deferred_targets
    assert {row.location for row in read.listings} == {"Remote, Canada"}
    assert len(site.api_queries) == 3
    if protocol == "tbk":
        assert [query["currentPage"] for query in site.api_queries] == [["1"], ["2"], ["3"]]
        assert all(query["perPage"] == ["10"] for query in site.api_queries)
        assert all(
            query["department"] == ["research"] and query["location"] == ["remote"]
            for query in site.api_queries
        )
    else:
        assert [query["offset"] for query in site.api_queries] == [["0"], ["2"], ["3"]]
        assert all(
            query["category"] == ["Research"] and query["search"] == ["data"]
            for query in site.api_queries
        )
    assert all(urlsplit(url).netloc == host for url in site.requested)


@pytest.mark.parametrize("protocol", ["tbk", "alignerr"])
def test_repeated_search_pages_remain_incomplete_even_when_reordered(protocol: str) -> None:
    site = _SearchSite(protocol)
    site.pages = [[1, 2], [2, 1]]

    read = site.fetcher.listings("Acme", site.root)

    assert read.complete is False
    assert {row.requisition_id for row in read.listings} == {"1", "2"}
    assert len(site.api_queries) == 2
    assert len([url for url in site.requested if "/jobs/role-" in url]) == (
        2 if protocol == "tbk" else 0
    )


@pytest.mark.parametrize("protocol", ["tbk"])
def test_a_failed_search_detail_keeps_successes_and_the_published_failure_target(
    protocol: str,
) -> None:
    site = _SearchSite(protocol)
    site.failed_details = {2}

    read = site.fetcher.listings("Acme", site.root)

    assert read.complete is False
    assert [row.requisition_id for row in read.listings] == ["1", "3"]
    assert read.detail.failed == 1
    assert site.detail_url(2) in read.detail.failure_examples
    assert all("summary" not in row.published_description.lower() for row in read.listings)


def _refusing_detail(
    site: _SearchSite, numbers: set[int], error: Exception, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    fetched = site.fetcher.get_text
    refused: list[str] = []

    def get_text(url: str, **kwargs: Any) -> str:
        if url in {site.detail_url(number) for number in numbers}:
            refused.append(url)
            raise error
        return fetched(url, **kwargs)

    monkeypatch.setattr(site.fetcher, "get_text", get_text)
    return refused


def test_a_malformed_detail_after_the_last_page_keeps_every_other_listed_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = _SearchSite("tbk")
    _refusing_detail(site, {2}, json.JSONDecodeError("Malformed", "{", 1), monkeypatch)

    read = site.fetcher.listings("Acme", site.root)

    assert "publisher_search_request_failed" not in read.skip_reasons
    assert not any(target.deferred_reason == "tbk_search_page" for target in read.deferred_targets)
    assert [row.requisition_id for row in read.listings] == ["1", "3"]
    assert [
        target.url
        for target in read.deferred_targets
        if target.deferred_reason == "tbk_search_detail"
    ] == [site.detail_url(2)]
    assert read.complete is False


def test_a_robots_refused_detail_is_counted_once_and_never_requested_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = _SearchSite("tbk")
    site.pages = [[1, 2], [3], []]
    site.failed_details = {3}
    refused = _refusing_detail(site, {2}, RobotsDeniedError(site.detail_url(2)), monkeypatch)

    read = site.fetcher.listings("Acme", site.root)

    assert "publisher_search_request_failed" not in read.skip_reasons
    assert read.skip_reasons["robots_disallowed"] == 1
    assert [row.requisition_id for row in read.listings] == ["1"]
    assert site.detail_url(2) not in {target.url for target in read.deferred_targets}
    assert read.deferred_targets
    site.failed_details = set()

    site.fetcher.continue_listings(
        "Acme", read.deferred_targets, root_url=site.root, handoff_scopes=()
    )

    assert refused == [site.detail_url(2)]


@pytest.mark.parametrize("numbers", [{2}, {1, 2, 3}])
def test_a_search_with_robots_refused_details_never_reads_complete(
    numbers: set[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    site = _SearchSite("tbk")
    _refusing_detail(site, numbers, RobotsDeniedError("refused"), monkeypatch)

    read = site.fetcher.listings("Acme", site.root)

    assert read.skip_reasons["robots_disallowed"] == len(numbers)
    assert read.complete is False
    assert read.outcome is not SourceReadOutcome.ATS_COMPLETE


def test_a_repeated_page_settles_its_details_outside_the_page_request_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = _SearchSite("tbk")
    site.pages = [[1, 2, 4], [1, 2, 4], []]
    _refusing_detail(site, {2}, ValueError("Synthetic detail scope refusal"), monkeypatch)

    with pytest.raises(ValueError, match="Synthetic detail scope refusal"):
        site.fetcher.listings("Acme", site.root)


@pytest.mark.parametrize("protocol", ["tbk", "alignerr"])
def test_protected_search_identities_are_covered_without_fetching_their_detail(
    protocol: str,
) -> None:
    site = _SearchSite(protocol)

    read = site.fetcher.listings(
        "Acme",
        site.root,
        options=SourceReadOptions(protected_posting_urls=frozenset({site.detail_url(2)})),
    )

    assert site.detail_url(2) not in site.requested
    assert site.detail_url(2) in read.protected_posting_urls
    assert {row.requisition_id for row in read.listings} == (
        {"1", "3"} if protocol == "tbk" else {"1", "2", "3"}
    )


@pytest.mark.parametrize("protocol", ["tbk"])
def test_a_failed_detail_keeps_its_attempt_when_a_later_detail_hits_the_deadline(
    protocol: str,
) -> None:
    site = _SearchSite(protocol)
    site.pages = [[1, 2, 3], []]
    site.failed_details = {1}
    site.interrupt_after = 2

    read = site.fetcher.listings("Acme", site.root)

    assert [row.requisition_id for row in read.listings] == ["2"]
    failed = next(target for target in read.deferred_targets if target.url == site.detail_url(1))
    unread = next(target for target in read.deferred_targets if target.url == site.detail_url(3))
    assert failed.detail_checkpoint is not None
    assert failed.detail_checkpoint.failed_attempts == (("1", 1),)
    assert failed.automatic_eligible is False
    assert unread.detail_checkpoint is not None
    assert unread.detail_checkpoint.failed_attempts == ()
    assert unread.automatic_eligible is True


@pytest.mark.parametrize("protocol", ["tbk"])
def test_an_interrupted_search_resumes_unread_details_and_later_pages(protocol: str) -> None:
    site = _SearchSite(protocol)
    site.interrupt_after = 1

    first = site.fetcher.listings("Acme", site.root)

    assert first.complete is False
    assert first.deferred_targets
    assert [row.requisition_id for row in first.listings] == ["1"]
    assert all(target.source_url == site.root for target in first.deferred_targets)
    site.stopped = False

    resumed = site.fetcher.continue_listings(
        "Acme", first.deferred_targets, root_url=site.root, handoff_scopes=()
    )

    assert {row.requisition_id for row in resumed.listings} == {"2", "3"}
    assert not resumed.deferred_targets
    assert site.requested.count(site.detail_url(1)) == 1


@pytest.mark.parametrize("protocol", ["tbk", "alignerr"])
def test_a_transient_search_page_failure_retains_a_resumable_frontier(protocol: str) -> None:
    site = _SearchSite(protocol)
    site.fail_page = 1

    first = site.fetcher.listings("Acme", site.root)

    assert first.complete is False
    assert {row.requisition_id for row in first.listings} == {"1", "2"}
    assert first.deferred_targets
    site.fail_page = None

    targets = tuple(
        target
        for target in first.deferred_targets
        if protocol == "tbk" or target.deferred_reason == "alignerr_search_page"
    )
    resumed = site.fetcher.continue_listings("Acme", targets, root_url=site.root, handoff_scopes=())

    assert [row.requisition_id for row in resumed.listings] == ["3"]
    if protocol == "alignerr":
        assert site.detail_reads == 0
        assert resumed.listing_coverage is not None
        details = tuple(
            target
            for target in (*first.deferred_targets, *resumed.deferred_targets)
            if target.deferred_reason == "alignerr_search_detail"
        )
        recovered = site.fetcher.continue_listings(
            "Acme", details, root_url=site.root, handoff_scopes=()
        )
        assert {row.requisition_id for row in recovered.listings} == {"1", "2", "3"}
        assert all(row.published_description == _BODY.strip() for row in recovered.listings)
        assert not recovered.deferred_targets
    else:
        assert not resumed.deferred_targets


@pytest.mark.parametrize("boundary", ["foreign_api", "private_method"])
def test_a_tbk_root_never_uses_a_foreign_or_private_search_declaration(boundary: str) -> None:
    site = _SearchSite("tbk")
    setattr(site, boundary, True)

    try:
        read = site.fetcher.listings("Acme", site.root)
    except ValueError:
        read = None

    assert read is None or read.complete is False
    assert site.api_queries == []
    assert not any(url.startswith("https://other.example") for url in site.requested)


def test_a_tbk_root_declared_search_does_not_require_an_external_renderer() -> None:
    site = _SearchSite("tbk")
    site.foreign_script = True

    read = site.fetcher.listings("Acme", site.root)

    assert read.complete is True
    assert len(read.listings) == 3
    assert not any(url.endswith(".js") for url in site.requested)


@pytest.mark.parametrize("boundary", ["deadline", "failed", "repeated", "changed", "short"])
def test_alignerr_slices_preserve_membership_and_never_certify_an_unstable_list(
    boundary: str,
) -> None:
    site = _SearchSite("alignerr")
    original = site.answer

    def answer(url: str, **kwargs: Any) -> httpx.Response:
        response = original(url)
        if (
            boundary == "deadline"
            and urlsplit(url).path == "/api/jobs"
            and len(site.api_queries) == 1
        ):
            site.stopped = True
        return response

    site.fetcher.client.getter = answer
    if boundary == "failed":
        site.fail_page = 1
    elif boundary == "repeated":
        site.pages = [[1, 2], [2, 1]]
    elif boundary == "changed":
        site.totals = [2, 8, 3]
    first = site.fetcher.listings("Acme", site.root)
    assert site.detail_reads == 0
    assert first.raw_count == (2 if boundary in {"deadline", "failed", "repeated"} else 3)
    if boundary in {"deadline", "failed", "repeated"}:
        cursor = next(
            target
            for target in first.deferred_targets
            if target.deferred_reason == "alignerr_search_page"
        )
        assert cursor.detail_checkpoint is not None
        assert dict(cursor.detail_checkpoint.listing_urls) == {
            "1": site.detail_url(1),
            "2": site.detail_url(2),
        }
        site.stopped = False
        site.fail_page = None
        if boundary == "repeated":
            assert first.listing_coverage is None
            assert not cursor.automatic_eligible
        else:
            resumed = site.fetcher.continue_listings(
                "Acme", (cursor,), root_url=site.root, handoff_scopes=()
            )
            assert resumed.raw_count == 3
            assert resumed.listing_coverage is not None
            assert set(resumed.listing_coverage.posting_urls) == {
                site.detail_url(n) for n in (1, 2, 3)
            }
    elif boundary == "changed":
        assert first.discovery_truncated
        assert first.listing_coverage is None
    else:
        assert first.listing_coverage is not None
    assert site.detail_reads == 0


def test_an_alignerr_root_never_fetches_a_foreign_jobs_bundle() -> None:
    site = _SearchSite("alignerr")
    site.foreign_script = True

    try:
        read = site.fetcher.listings("Acme", site.root)
    except ValueError:
        read = None

    assert read is None or read.complete is False
    assert site.api_queries == []
    assert not any(url.startswith("https://other.example") for url in site.requested)


def test_a_cut_listing_resumes_with_no_detail_request_before_the_listing_ends() -> None:
    site = _SearchSite("tbk")
    site.stop_after_pages = 1

    first = site.fetcher.listings("Acme", site.root)

    assert site.detail_reads == 0
    assert first.complete is False
    cursor = next(
        target for target in first.deferred_targets if target.deferred_reason == "tbk_search_page"
    )
    assert {
        target.url
        for target in first.deferred_targets
        if target.deferred_reason == "tbk_search_detail"
    } == {site.detail_url(1), site.detail_url(2)}
    site.stopped = False
    site.requested.clear()

    resumed = site.fetcher.continue_listings(
        "Acme", (cursor,), root_url=site.root, handoff_scopes=()
    )

    searches = [index for index, url in enumerate(site.requested) if "/career-search" in url]
    details = [index for index, url in enumerate(site.requested) if "/jobs/role-" in url]
    assert details and max(searches) < min(details)
    assert [row.requisition_id for row in resumed.listings] == ["3"]
    assert resumed.complete is False


def test_a_search_page_cursor_url_carries_only_its_page() -> None:
    site = _SearchSite("tbk")
    site.stop_after_pages = 1

    read = site.fetcher.listings("Acme", site.root)

    cursor = next(
        target for target in read.deferred_targets if target.deferred_reason == "tbk_search_page"
    )
    assert parse_qs(urlsplit(cursor.url).query) == {"__reader_page": ["2"]}
    assert cursor.detail_checkpoint is not None
    assert cursor.detail_checkpoint.listing_total == 3


def test_a_declared_total_mismatch_records_the_gap_without_a_page_cursor() -> None:
    site = _SearchSite("tbk")
    site.totals = [4, 4, 4]

    read = site.fetcher.listings("Acme", site.root)

    assert read.skip_reasons["publisher_listing_total_mismatch"] == 1
    assert read.complete is False
    assert not any(target.deferred_reason == "tbk_search_page" for target in read.deferred_targets)
    assert {row.requisition_id for row in read.listings} == {"1", "2", "3"}


def _page_cursor(read: SourceRead) -> SourceContinuationTarget:
    (cursor,) = (
        target for target in read.deferred_targets if target.deferred_reason == "tbk_search_page"
    )
    return cursor


def _resume(site: _SearchSite, cursor: SourceContinuationTarget) -> SourceRead:
    site.stopped = False
    return site.fetcher.continue_listings("Acme", (cursor,), root_url=site.root, handoff_scopes=())


def _churned(site: _SearchSite, cause: str) -> None:
    if cause == "mismatch":
        site.totals = [4, 4, 4]
    else:
        site.pages = [[4, 1], [2, 3], []]


@pytest.mark.parametrize("cause", ["mismatch", "unstable"])
def test_a_listing_that_changes_between_tasks_relists_once_from_the_first_page(
    cause: str,
) -> None:
    site = _SearchSite("tbk")
    site.stop_after_pages = 1
    first = site.fetcher.listings("Acme", site.root)
    _churned(site, cause)

    read = _resume(site, _page_cursor(first))

    relist = _page_cursor(read)
    assert parse_qs(urlsplit(relist.url).query) == {"__reader_page": ["1"], "__reader_pass": ["2"]}
    assert relist.automatic_eligible is True
    assert "publisher_listing_total_mismatch" not in read.skip_reasons
    assert "publisher_listing_changed" not in read.skip_reasons
    assert read.discovery_truncated is False
    checkpoint = relist.detail_checkpoint
    assert checkpoint is not None
    assert checkpoint.listing_pass == 2
    assert checkpoint.pass_listing_ids == ()
    assert checkpoint.page_fingerprints == ()
    assert checkpoint.listing_total is None
    assert checkpoint.listing_unstable is False


@pytest.mark.parametrize("cause", ["mismatch", "unstable"])
def test_a_relisted_pass_certifies_the_current_membership_without_rereading_bodies(
    cause: str,
) -> None:
    site = _SearchSite("tbk")
    site.stop_after_pages = 1
    first = site.fetcher.listings("Acme", site.root)
    _churned(site, cause)
    relist = _page_cursor(_resume(site, _page_cursor(first)))
    site.pages, site.totals = [[4, 1], [2, 3], []], [4, 4, 4]
    site.requested.clear()

    read = _resume(site, relist)

    assert not any(target.deferred_reason == "tbk_search_page" for target in read.deferred_targets)
    assert "publisher_listing_total_mismatch" not in read.skip_reasons
    assert read.discovery_truncated is False
    assert [url for url in site.requested if "/jobs/role-" in url] == [site.detail_url(4)]
    (receipt,) = read.publisher_listing_receipts
    assert set(receipt.listing_ids) == {"1", "2", "3", "4"}
    assert receipt.listing_pass == 2
    assert set(receipt.pass_listing_ids) == {"1", "2", "3", "4"}


def test_a_relisted_pass_receipt_narrows_to_the_identities_the_publisher_still_lists() -> None:
    site = _SearchSite("tbk")
    site.stop_after_pages = 1
    first = site.fetcher.listings("Acme", site.root)
    site.totals = [4, 4, 4]
    relist = _page_cursor(_resume(site, _page_cursor(first)))
    site.pages, site.totals = [[2, 3], [5], []], [3, 3, 3]

    read = _resume(site, relist)

    assert not any(target.deferred_reason == "tbk_search_page" for target in read.deferred_targets)
    (receipt,) = read.publisher_listing_receipts
    assert set(receipt.listing_ids) == {"2", "3", "5"}
    assert {item for item, _url in receipt.completed} <= {"2", "3", "5"}


def test_a_second_changed_pass_leaves_one_manual_relist_naming_both_counts() -> None:
    site = _SearchSite("tbk")
    site.stop_after_pages = 1
    first = site.fetcher.listings("Acme", site.root)
    site.totals = [4, 4, 4]
    relist = _page_cursor(_resume(site, _page_cursor(first)))

    read = _resume(site, relist)

    manual = _page_cursor(read)
    assert parse_qs(urlsplit(manual.url).query) == {"__reader_page": ["1"], "__reader_pass": ["3"]}
    assert manual.automatic_eligible is False
    assert "reports 4 listings" in read.note
    assert "retained 3 distinct identities" in read.note
    assert read.discovery_truncated is False


def test_a_second_unstable_pass_matching_its_total_says_the_listing_changed() -> None:
    site = _SearchSite("tbk")
    site.stop_after_pages = 1
    first = site.fetcher.listings("Acme", site.root)
    _churned(site, "unstable")
    relist = _page_cursor(_resume(site, _page_cursor(first)))
    site.pages, site.totals = [[1, 2], [2, 3], []], [3, 3, 3]

    read = _resume(site, relist)

    manual = _page_cursor(read)
    assert parse_qs(urlsplit(manual.url).query)["__reader_pass"] == ["3"]
    assert manual.automatic_eligible is False
    assert read.skip_reasons["publisher_listing_changed"] == 1
    assert "publisher_listing_total_mismatch" not in read.skip_reasons
    assert "changed during two listing passes" in read.note
    assert "distinct identities" not in read.note


def test_a_relisted_page_cursor_never_shares_an_address_with_the_first_pass() -> None:
    site = _SearchSite("tbk")
    site.stop_after_pages = 1
    first_cursor = _page_cursor(site.fetcher.listings("Acme", site.root))
    site.totals = [4, 4, 4]
    relist = _page_cursor(_resume(site, first_cursor))
    site.stop_after_pages = len(site.api_queries) + 1

    second_cursor = _page_cursor(_resume(site, relist))

    assert search_page_number(first_cursor, 0) == search_page_number(second_cursor, 0) == 2
    assert second_cursor.url != first_cursor.url
    assert second_cursor.automatic_eligible is True
    checkpoint = second_cursor.detail_checkpoint
    assert checkpoint is not None
    assert checkpoint.listing_pass == 2
    assert set(checkpoint.pass_listing_ids) == {"1", "2"}


def test_a_stored_cursor_without_a_listing_pass_resumes_as_the_first_pass() -> None:
    site = _SearchSite("tbk")
    site.stop_after_pages = 1
    cursor = _page_cursor(site.fetcher.listings("Acme", site.root))
    assert cursor.detail_checkpoint is not None
    stored = cursor.detail_checkpoint.as_json()
    stored.pop("listing_pass", None)
    stored.pop("pass_listing_ids", None)
    restored = SourceDetailCheckpoint.from_json(stored)
    assert restored == cursor.detail_checkpoint

    read = _resume(site, replace(cursor, detail_checkpoint=restored))

    assert not any(target.deferred_reason == "tbk_search_page" for target in read.deferred_targets)
    assert [row.requisition_id for row in read.listings] == ["3"]


def test_a_listing_pass_cannot_name_an_identity_outside_its_membership() -> None:
    with pytest.raises(ValueError):
        SourceDetailCheckpoint.from_json(
            {
                "platform": "search:tbk_search_page",
                "board_url": "https://acme.example/careers/",
                "listing_ids": ["1"],
                "listing_pass": 2,
                "pass_listing_ids": ["9"],
            }
        )
