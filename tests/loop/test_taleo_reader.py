"""Taleo anonymous searches retain scope, actual page membership, and detail outcomes."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from tailorcv.application.discovery.source_reading import (
    MAX_SOURCE_READ_ATTEMPTS,
    SourceReadOptions,
)
from tailorcv.domain import SourceReadOutcome
from tailorcv.infrastructure.web_fetch.board_adapters import BoardApiFetcher
from tailorcv.infrastructure.web_fetch.policy import PoliteHttpClient
from tests.support.doubles import declaring_page
from tests.support.http import public_resolver
from tests.support.suite import FIXTURES

_BODY = "Review clinical source records, document methods, and maintain governed reporting. " * 8
_CLASSIC = FIXTURES / "board_responses"


class _TaleoSite:
    def __init__(self) -> None:
        self.origin = "https://acme.taleo.net"
        self.site = "external"
        self.lang = "en"
        self.portal = "1001"
        self.root = self.origin + "/careersection/external/jobsearch.ftl?lang=en&portal=1001"
        self.pages = [[1, 2], [3], []]
        self.totals = [3, 3, 3]
        self.page_sizes: list[Any] = [2]
        self.requests: list[str] = []
        self.posts: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        self.failed: set[int] = set()
        self.redirects: dict[int, int] = {}
        self.fail_page: int | None = None
        self.reported_page: int | None = None
        self.bad_rows = False
        self.stopped = False
        self.interrupt_after: int | None = None
        self.stop_after_pages: int | None = None
        self.detail_reads = 0
        self.fields = {"KEYWORD": "", "LOCATION": ""}
        site = self

        class Fetcher(BoardApiFetcher):
            @staticmethod
            def deadline_elapsed() -> bool:
                return site.stopped

        self.fetcher: BoardApiFetcher = Fetcher(client=self.new_client())

    def new_client(self) -> PoliteHttpClient:
        """Build a fresh policy client that carries the publisher's required POST headers."""
        return PoliteHttpClient(
            user_agent="tailorcv/test",
            sleeper=lambda _seconds: None,
            jitter=lambda _low, _high: 0.0,
            getter=self.get,
            poster=self.post,
            resolver=public_resolver,
        )

    def detail_url(self, number: int) -> str:
        query = {
            "job": str(number),
            "lang": self.lang,
            "portal": self.portal,
            "tz": "GMT+00:00",
            "tzname": "UTC",
        }
        return self.origin + f"/careersection/{self.site}/jobdetail.ftl?" + urlencode(query)

    def markup(self) -> str:
        return (
            "<script>var require = {config: {'fs/FacetedSearchSettings': {"
            + f"lang:'{self.lang}',portalNo:'{self.portal}',"
            + f"urlOrigin:'{self.origin}',urlCode:'{self.site}',"
            + "columns:[[{type:'field', value:'title {value}', index:1}]],"
            + "defaultOrder:true,defaultSortingField:'JOB_TITLE'}}};</script>"
            + '<select id="sortBySelect">'
            + '<option value="JOB_TITLE" sortid="5" selected>Title</option></select>'
            + "".join(
                f'<input id="{key}" class="criteriaFieldInput" value="{value}">'
                for key, value in self.fields.items()
            )
        )

    def get(self, url: str, **_kwargs: Any) -> httpx.Response:
        self.requests.append(url)
        request = httpx.Request("GET", url)
        parsed = urlsplit(url)
        if parsed.path == "/robots.txt":
            return httpx.Response(200, request=request, text="")
        if parsed.path.endswith("jobsearch.ftl"):
            return httpx.Response(200, request=request, text=self.markup())
        if parsed.path.endswith("jobdetail.ftl"):
            number = int(parse_qs(parsed.query)["job"][0])
            self.detail_reads += 1
            if number in self.redirects:
                return httpx.Response(
                    302,
                    request=request,
                    headers={"Location": self.detail_url(self.redirects[number])},
                )
            if number in self.failed:
                return httpx.Response(404, request=request, text="Unavailable")
            if self.interrupt_after == self.detail_reads:
                self.stopped = True
                self.interrupt_after = None
            body = declaring_page(
                {
                    "@type": "JobPosting",
                    "title": f"Clinical Analyst {number}",
                    "description": _BODY,
                    "hiringOrganization": {"name": "Acme"},
                }
            )
            body += "<h1>Job Number</h1><h2>Job Description</h2>"
            body += (
                '<span id="requisitionDescriptionInterface.reqTitleLinkAction.row1">'
                f"Clinical Analyst {number}</span>"
            )
            return httpx.Response(200, request=request, text=body)
        return httpx.Response(404, request=request)

    def post(
        self, url: str, *, json: Any = None, headers: Any = None, **_kwargs: Any
    ) -> httpx.Response:
        selected = dict(headers or {})
        self.posts.append((url, json, selected))
        request = httpx.Request("POST", url)
        if not all(selected.get(key) for key in ("tz", "tzname")):
            return httpx.Response(500, request=request, text="An Error Occurred")
        page = int(json["pageNo"])
        if page == self.fail_page:
            return httpx.Response(503, request=request)
        ids = self.pages[page - 1] if page <= len(self.pages) else []
        page_size = self.page_sizes[min(page - 1, len(self.page_sizes) - 1)]
        rows = [
            {
                "jobId": str(100 + number),
                "contestNo": str(number),
                "column": [f"Clinical Analyst {number}", '["Remote, Canada"]', "Remote"],
                "linkedColumn": 0,
                "locationsColumns": [1],
            }
            for number in ids
        ]
        if self.stop_after_pages == len(self.posts):
            self.stopped = True
            self.stop_after_pages = None
        return httpx.Response(
            200,
            request=request,
            json={
                "requisitionList": None if self.bad_rows else rows,
                "pagingData": {
                    "currentPageNo": self.reported_page or page,
                    **({"pageSize": page_size} if page_size is not None else {}),
                    "totalCount": self.totals[min(page - 1, len(self.totals) - 1)],
                },
                "careerSectionUnAvailable": False,
            },
        )


def test_taleo_declared_last_page_keeps_full_details_without_an_empty_probe() -> None:
    site = _TaleoSite()
    read = site.fetcher.listings("Acme", site.root)
    assert {item.title for item in read.listings} == {
        "Clinical Analyst 1",
        "Clinical Analyst 2",
        "Clinical Analyst 3",
    }
    assert all(_BODY.strip() in item.published_description for item in read.listings)
    assert [body["pageNo"] for _, body, _ in site.posts] == [1, 2]
    assert read.complete
    assert read.raw_count == 3
    assert read.listings[0].source is not None
    assert json.loads(read.listings[0].source.record)["row"]["jobId"] == "101"
    assert read.listings[0].requisition_id == "1"


def test_taleo_timezone_header_group_and_saved_filters_reach_polite_transport() -> None:
    site = _TaleoSite()
    site.fields["KEYWORD"] = "clinical"
    site.root += (
        "&tz=GMT-05%3A00&tzname=America%2FToronto&f=LOCATION%28123%2C456%29"
        "&a=EMPLOYEE_STATUS%281%29&s=5%7CD&multiline=true"
    )
    read = site.fetcher.listings("Acme", site.root)
    assert len(read.listings) == 3
    url, body, headers = site.posts[0]
    assert parse_qs(urlsplit(url).query) == {"lang": ["en"], "portal": ["1001"]}
    assert headers["tz"] == "GMT-05:00"
    assert headers["tzname"] == "America/Toronto"
    assert "Cookie" not in headers and "Authorization" not in headers
    assert body["fieldData"] == {"fields": {"KEYWORD": "clinical", "LOCATION": ""}, "valid": True}
    assert body["filterSelectionParam"]["searchFilterSelections"][0] == {
        "id": "LOCATION",
        "selectedValues": ["123", "456"],
    }
    assert {"id": "EMPLOYEE_STATUS", "selectedValues": ["1"]} in body[
        "advancedSearchFiltersSelectionParam"
    ]["searchFilterSelections"]
    assert body["sortingSelection"]["ascendingSortingOrder"] == "false"
    assert body["multilineEnabled"] is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("origin", "https://other.taleo.net"),
        ("site", "internal"),
        ("portal", "2002"),
        ("lang", "fr"),
    ],
)
def test_taleo_declared_scope_must_agree_before_search(field: str, value: str) -> None:
    site = _TaleoSite()
    setattr(site, field, value)
    with pytest.raises(ValueError):
        site.fetcher.listings("Acme", site.root)
    assert not site.posts


@pytest.mark.parametrize(
    "query",
    [
        "&portal=2002",
        "&tzname=UTC",
        "&tz=GMT%2B25%3A00&tzname=UTC",
        "&tz=GMT%2B00%3A00&tzname=Unknown%2FPlace",
        "&f=broken",
        "&a=UNKNOWN%281%29",
    ],
)
def test_taleo_malformed_or_ambiguous_context_refuses_search(query: str) -> None:
    site = _TaleoSite()
    with pytest.raises(ValueError):
        site.fetcher.listings("Acme", site.root + query)
    assert not site.posts


def test_taleo_conflicting_totals_retain_all_actual_rows_with_incomplete_coverage() -> None:
    site = _TaleoSite()
    site.totals = [4, 3]
    read = site.fetcher.listings("Acme", site.root)
    assert len(read.listings) == 3
    assert not read.complete
    assert read.discovery_truncated
    assert read.skip_reasons["publisher_listing_changed"] == 1
    assert [body["pageNo"] for _, body, _ in site.posts] == [1, 2]


def test_taleo_repeated_page_is_manual_and_retains_successful_rows() -> None:
    site = _TaleoSite()
    site.pages = [[1, 2], [2, 1]]
    read = site.fetcher.listings("Acme", site.root)
    assert len(read.listings) == 2
    assert not read.complete
    assert len(read.deferred_targets) == 1
    assert not read.deferred_targets[0].automatic_eligible


def test_taleo_failed_detail_keeps_exact_url_and_stable_contest_identity() -> None:
    site = _TaleoSite()
    site.failed = {2}
    read = site.fetcher.listings("Acme", site.root)
    assert {item.requisition_id for item in read.listings} == {"1", "3"}
    target = next(item for item in read.deferred_targets if item.url == site.detail_url(2))
    assert target.detail_checkpoint is not None
    assert target.detail_checkpoint.pending_ids == ("2",)
    assert not read.complete
    site.failed.clear()
    resumed = site.fetcher.continue_listings(
        "Acme", (target,), root_url=site.root, handoff_scopes=()
    )
    assert [item.requisition_id for item in resumed.listings] == ["2"]


def test_taleo_interruption_resumes_exact_details_and_page_without_losing_membership() -> None:
    site = _TaleoSite()
    site.interrupt_after = 1
    first = site.fetcher.listings("Acme", site.root)
    assert [item.requisition_id for item in first.listings] == ["1"]
    site.stopped = False
    resumed = site.fetcher.continue_listings(
        "Acme", first.deferred_targets, root_url=site.root, handoff_scopes=()
    )
    assert {item.requisition_id for item in resumed.listings} == {"2", "3"}
    assert site.requests.count(site.detail_url(1)) == 1


def test_taleo_repeated_page_after_interruption_is_detected_across_cursor() -> None:
    site = _TaleoSite()
    site.pages = [[1, 2], [2, 1]]
    site.interrupt_after = 2
    first = site.fetcher.listings("Acme", site.root)
    site.stopped = False
    resumed = site.fetcher.continue_listings(
        "Acme", first.deferred_targets, root_url=site.root, handoff_scopes=()
    )
    assert not resumed.listings
    assert not resumed.complete
    assert any(not item.automatic_eligible for item in resumed.deferred_targets)


@pytest.mark.parametrize("failure", ["request", "rows", "cursor"])
def test_taleo_later_malformed_page_retains_prior_details(failure: str) -> None:
    site = _TaleoSite()
    original = site.post

    def post(url: str, **kwargs: Any) -> httpx.Response:
        if kwargs["json"]["pageNo"] == 2:
            site.fail_page = 2 if failure == "request" else None
            site.bad_rows = failure == "rows"
            site.reported_page = 1 if failure == "cursor" else None
        return original(url, **kwargs)

    site.fetcher = BoardApiFetcher(
        client=PoliteHttpClient(
            user_agent="tailorcv/test",
            sleeper=lambda _seconds: None,
            jitter=lambda _low, _high: 0.0,
            getter=site.get,
            poster=post,
            resolver=public_resolver,
        )
    )
    read = site.fetcher.listings("Acme", site.root)
    assert len(read.listings) == 2
    assert not read.complete
    assert any(not item.automatic_eligible for item in read.deferred_targets)


def test_taleo_protected_redirect_destination_is_never_read() -> None:
    site = _TaleoSite()
    site.redirects = {1: 2}
    read = site.fetcher.listings(
        "Acme",
        site.root,
        options=SourceReadOptions(
            fresh_details=True, protected_posting_urls=frozenset({site.detail_url(2)})
        ),
    )
    assert site.detail_url(2) not in site.requests
    assert [item.requisition_id for item in read.listings] == ["3"]
    assert not read.complete


@pytest.mark.parametrize("headers", [{}, {"tz": "GMT+00:00"}, {"tzname": "UTC"}])
def test_taleo_incomplete_timezone_group_fails_at_polite_transport(headers: dict[str, str]) -> None:
    site = _TaleoSite()
    with pytest.raises(httpx.HTTPStatusError):
        site.fetcher.post_json_with_headers(
            site.origin + "/careersection/rest/jobboard/searchjobs?lang=en&portal=1001",
            {"pageNo": 1},
            headers=headers,
        )


def test_taleo_declaration_supplies_missing_root_query_for_detail_resume() -> None:
    site = _TaleoSite()
    site.root = site.root.split("?")[0]
    site.failed = {2}
    read = site.fetcher.listings("Acme", site.root)
    target = next(item for item in read.deferred_targets if item.url == site.detail_url(2))
    site.failed.clear()
    resumed = site.fetcher.continue_listings(
        "Acme", (target,), root_url=site.root, handoff_scopes=()
    )
    assert [item.requisition_id for item in resumed.listings] == ["2"]


_RETRY_AFTER_HOLD = ("", 429, None, "host_held")
_LONG_CRAWL_DELAY = ("User-agent: *\nCrawl-delay: 45\n", 200, None, "host_held")
_PACED_CRAWL_DELAY = ("User-agent: *\nCrawl-delay: 5\n", 200, 1.0, "time_limit_reached")
_HOLDS = pytest.mark.parametrize(
    ("robots", "status", "budget", "reason"),
    [_RETRY_AFTER_HOLD, _LONG_CRAWL_DELAY, _PACED_CRAWL_DELAY],
    ids=["retry-after", "crawl-delay-past-the-page-budget", "crawl-delay-within-the-page-budget"],
)


def _held_client(
    site: _TaleoSite,
    monkeypatch: pytest.MonkeyPatch,
    robots: str,
    budget: float | None,
) -> str:
    """Give ``site`` a fresh client serving ``robots``; return a URL that answers the hold."""
    if budget is not None:
        monkeypatch.setattr(
            "tailorcv.infrastructure.web_fetch.board_adapters.read_scope.MAX_PAGE_READ_SECONDS",
            budget,
        )
    hold_url = site.origin + "/publisher-hold"

    def get(address: str, **kwargs: Any) -> httpx.Response:
        request = httpx.Request("GET", address)
        if urlsplit(address).path == "/robots.txt":
            return httpx.Response(200, request=request, text=robots)
        if address == hold_url:
            status = 200 if robots else 429
            return httpx.Response(status, request=request, headers={"Retry-After": "120"})
        return site.get(address, **kwargs)

    site.fetcher.client.close()
    site.fetcher.client = site.new_client()
    site.fetcher.client.getter = get
    return hold_url


@_HOLDS
def test_taleo_context_host_holds_retain_the_exact_detail_without_an_attempt(
    monkeypatch: pytest.MonkeyPatch, robots: str, status: int, budget: float | None, reason: str
) -> None:
    site = _TaleoSite()
    site.pages = [[1], []]
    site.totals = [1, 1]
    site.failed = {1}
    first = site.fetcher.listings("Acme", site.root)
    [original] = first.deferred_targets
    assert original.detail_checkpoint is not None
    target = replace(
        original,
        automatic_eligible=True,
        manual_detail_retry=True,
        detail_checkpoint=replace(
            original.detail_checkpoint, failed_attempts=(("1", MAX_SOURCE_READ_ATTEMPTS),)
        ),
    )
    site.failed.clear()
    hold_url = _held_client(site, monkeypatch, robots, budget)
    assert site.fetcher.client.get(hold_url).status_code == status
    previous_requests = tuple(site.requests)

    held = site.fetcher.continue_listings("Acme", (target,), root_url=site.root, handoff_scopes=())

    assert not held.complete and not held.listings
    assert held.outcome is SourceReadOutcome.CRAWL_CONTINUATION
    assert held.skip_reasons == {"host_held": 1}
    assert held.attempted_target_urls == ()
    assert held.detail.attempted == held.detail.failed == held.detail.read == 0
    assert held.deferred_targets == (
        replace(target, automatic_eligible=reason == "time_limit_reached"),
    )
    assert tuple(site.requests) == previous_requests
    assert site.detail_reads == 1
    site.fetcher.client.close()
    site.fetcher.client = site.new_client()

    resumed = site.fetcher.continue_listings(
        "Acme", held.deferred_targets, root_url=site.root, handoff_scopes=()
    )

    assert [row.requisition_id for row in resumed.listings] == ["1"]
    assert _BODY.strip() in resumed.listings[0].published_description
    assert resumed.detail.attempted == resumed.detail.read == 1
    assert resumed.attempted_target_urls == (target.url,)
    assert not resumed.deferred_targets


@pytest.mark.parametrize(
    ("robots", "budget", "reason"),
    [
        (_LONG_CRAWL_DELAY[0], _LONG_CRAWL_DELAY[2], _LONG_CRAWL_DELAY[3]),
        (_PACED_CRAWL_DELAY[0], _PACED_CRAWL_DELAY[2], _PACED_CRAWL_DELAY[3]),
    ],
    ids=["crawl-delay-past-the-page-budget", "crawl-delay-within-the-page-budget"],
)
def test_taleo_search_page_hold_resumes_by_itself_only_when_paced(
    monkeypatch: pytest.MonkeyPatch, robots: str, budget: float | None, reason: str
) -> None:
    site = _TaleoSite()
    _held_client(site, monkeypatch, robots, budget)

    read = site.fetcher.listings("Acme", site.root)

    assert not read.complete and not read.listings
    assert read.skip_reasons[reason] == 1
    assert "publisher_search_request_failed" not in read.skip_reasons
    assert site.posts == []
    [cursor] = read.deferred_targets
    assert cursor.deferred_reason == "taleo_search_page"
    assert parse_qs(urlsplit(cursor.url).query)["__reader_page"] == ["1"]
    assert cursor.automatic_eligible is (reason == "time_limit_reached")


def test_taleo_detail_checkpoint_cannot_substitute_another_tenant() -> None:
    site = _TaleoSite()
    site.failed = {2}
    read = site.fetcher.listings("Acme", site.root)
    target = next(item for item in read.deferred_targets if item.url == site.detail_url(2))
    assert target.detail_checkpoint is not None
    foreign = site.detail_url(2).replace("acme.taleo.net", "other.taleo.net")
    forged = replace(target, detail_checkpoint=replace(target.detail_checkpoint, board_url=foreign))
    previous = len(site.requests)
    resumed = site.fetcher.continue_listings(
        "Acme", (forged,), root_url=site.root, handoff_scopes=()
    )
    assert not resumed.listings
    assert foreign not in site.requests[previous:]


@pytest.mark.parametrize(
    "path",
    [
        "/careersection/external/nested/jobsearch.ftl",
        "/careersection//jobsearch.ftl",
        "/careersection/rest/jobboard/searchjobs",
        "/careersection/external/jobsearch.ftl/extra",
    ],
)
def test_taleo_malformed_search_paths_do_not_issue_a_search(path: str) -> None:
    site = _TaleoSite()
    try:
        read = site.fetcher.listings("Acme", site.origin + path + "?lang=en&portal=1001")
    except httpx.HTTPStatusError:
        pass
    else:
        assert not read.listings
    assert not site.posts


def test_taleo_declared_locale_site_and_portal_are_preserved() -> None:
    site = _TaleoSite()
    site.lang, site.site, site.portal = "fr", "public_fr", "2002"
    site.root = site.origin + "/careersection/public_fr/jobsearch.ftl?lang=fr&portal=2002"
    read = site.fetcher.listings("Acme", site.root)
    assert len(read.listings) == 3
    assert all("/careersection/public_fr/" in item.url for item in read.listings)
    assert all(parse_qs(urlsplit(item.url).query)["lang"] == ["fr"] for item in read.listings)
    assert all(parse_qs(urlsplit(item.url).query)["portal"] == ["2002"] for item in read.listings)
    assert parse_qs(urlsplit(site.posts[0][0]).query) == {"lang": ["fr"], "portal": ["2002"]}


@pytest.mark.parametrize("total", [3, 4])
def test_taleo_declared_last_page_never_requests_an_out_of_window_repeat(total: int) -> None:
    site = _TaleoSite()
    site.pages = [[1, 2], list(range(3, total + 1))]
    site.pages.append(list(reversed(site.pages[-1])))
    site.totals = [total]
    read = site.fetcher.listings("Acme", site.root)
    assert [body["pageNo"] for _, body, _ in site.posts] == [1, 2]
    assert {item.requisition_id for item in read.listings} == {
        str(number) for number in range(1, total + 1)
    }
    assert read.complete
    assert read.raw_count == total
    assert not read.deferred_targets


def test_taleo_short_page_inside_the_declared_window_keeps_reading() -> None:
    site = _TaleoSite()
    site.pages = [[1], [2, 3], [4, 5]]
    site.totals = [5]
    read = site.fetcher.listings("Acme", site.root)
    assert [body["pageNo"] for _, body, _ in site.posts] == [1, 2, 3]
    assert {item.requisition_id for item in read.listings} == {"1", "2", "3", "4", "5"}
    assert read.complete
    assert not read.deferred_targets


@pytest.mark.parametrize(
    ("pages", "total"),
    [
        ([[1, 2], [3]], 4),
        ([[1, 2], []], 6),
        ([[1, 2, 3]], 2),
    ],
    ids=["declared-last-shortfall", "empty-before-last", "more-identities-than-total"],
)
def test_taleo_exhaustion_with_a_total_mismatch_records_the_gap_without_a_page_cursor(
    pages: list[list[int]], total: int
) -> None:
    site = _TaleoSite()
    site.pages = pages
    site.totals = [total]
    read = site.fetcher.listings("Acme", site.root)
    expected = {str(number) for page in pages for number in page}
    assert not read.complete
    assert read.discovery_truncated
    assert {item.requisition_id for item in read.listings} == expected
    assert all(_BODY.strip() in item.published_description for item in read.listings)
    assert read.raw_count == len(expected)
    assert read.skip_reasons["publisher_listing_total_mismatch"] == 1
    assert "publisher_listing_changed" not in read.skip_reasons
    assert f"{len(expected)} distinct identities" in read.note
    assert f"{total} listings" in read.note
    assert "Review the publisher search" in read.note
    assert [body["pageNo"] for _, body, _ in site.posts] == list(range(1, len(pages) + 1))
    assert not read.deferred_targets
    [receipt] = read.publisher_listing_receipts
    assert receipt.board_url == site.root
    assert set(receipt.listing_ids) == expected
    assert receipt.listing_total == total
    assert not receipt.listing_unstable
    assert len(receipt.page_fingerprints) == len(pages)


@pytest.mark.parametrize("page_size", [None, True, False, 0, -1, "2", 2.5, {}, []])
def test_taleo_missing_or_invalid_page_size_refuses_to_read_details(page_size: Any) -> None:
    site = _TaleoSite()
    site.page_sizes = [page_size]
    with pytest.raises(ValueError, match="positive page size"):
        site.fetcher.listings("Acme", site.root)
    assert not any("jobdetail.ftl" in url for url in site.requests)
    assert [body["pageNo"] for _, body, _ in site.posts] == [1]


def test_taleo_later_invalid_page_size_retains_prior_success_and_membership() -> None:
    site = _TaleoSite()
    site.page_sizes = [2, 0]
    read = site.fetcher.listings("Acme", site.root)
    assert {item.requisition_id for item in read.listings} == {"1", "2"}
    assert not read.complete
    assert read.skip_reasons["publisher_search_request_failed"] == 1
    [target] = read.deferred_targets
    assert not target.automatic_eligible
    assert target.detail_checkpoint is not None
    assert target.detail_checkpoint.listing_ids == ("1", "2")
    assert target.detail_checkpoint.listing_total == 3
    assert len(target.detail_checkpoint.page_fingerprints) == 1
    assert [body["pageNo"] for _, body, _ in site.posts] == [1, 2]


def test_taleo_later_zero_total_with_rows_keeps_reading_the_remaining_window() -> None:
    site = _TaleoSite()
    site.pages = [[1], [2, 3], [4, 5]]
    site.totals = [5, 0, 5]
    read = site.fetcher.listings("Acme", site.root)
    assert {item.requisition_id for item in read.listings} == {"1", "2", "3", "4", "5"}
    assert [body["pageNo"] for _, body, _ in site.posts] == [1, 2, 3]
    assert read.complete


@pytest.mark.parametrize("continued", [False, True])
def test_taleo_zero_on_final_page_uses_retained_positive_boundary(continued: bool) -> None:
    site = _TaleoSite()
    site.pages = [[1, 2], [3], [3]]
    site.totals = [3, 0, 0]
    if continued:
        site.interrupt_after = 1
        first = site.fetcher.listings("Acme", site.root)
        site.stopped = False
        read = site.fetcher.continue_listings(
            "Acme", first.deferred_targets, root_url=site.root, handoff_scopes=()
        )
    else:
        read = site.fetcher.listings("Acme", site.root)
    assert [body["pageNo"] for _, body, _ in site.posts] == [1, 2]
    assert not read.deferred_targets
    assert not read.discovery_truncated
    assert "publisher_listing_changed" not in read.skip_reasons
    assert "repeated_publisher_page" not in read.skip_reasons
    assert {item.requisition_id for item in read.listings} == (
        {"2", "3"} if continued else {"1", "2", "3"}
    )
    assert site.requests.count(site.detail_url(1)) == 1


def test_taleo_legacy_total_cursor_stops_at_a_zero_final_page_without_certifying_missing_ids() -> (
    None
):
    site = _TaleoSite()
    site.pages = [[1, 2], [3], [3]]
    site.totals = [3, 0, 0]
    site.stop_after_pages = 1
    first = site.fetcher.listings("Acme", site.root)
    target = next(
        item for item in first.deferred_targets if item.deferred_reason == "taleo_search_page"
    )
    legacy = replace(
        target, url=target.url + "&__reader_total=3&__reader_unstable=0", detail_checkpoint=None
    )
    site.stopped = False
    read = site.fetcher.continue_listings("Acme", (legacy,), root_url=site.root, handoff_scopes=())
    assert [body["pageNo"] for _, body, _ in site.posts] == [1, 2]
    assert [item.requisition_id for item in read.listings] == ["3"]
    assert not read.complete
    assert not read.discovery_truncated
    assert "publisher_listing_total_mismatch" not in read.skip_reasons
    assert "repeated_publisher_page" not in read.skip_reasons
    [relist] = read.deferred_targets
    assert parse_qs(urlsplit(relist.url).query)["__reader_pass"] == ["2"]
    assert relist.automatic_eligible


def test_taleo_resumed_last_page_checks_accumulated_identities_against_the_total() -> None:
    site = _TaleoSite()
    site.totals = [4]
    site.stop_after_pages = 1
    first = site.fetcher.listings("Acme", site.root)
    assert first.listings == []
    first_page = next(
        item for item in first.deferred_targets if item.deferred_reason == "taleo_search_page"
    )
    assert first_page.detail_checkpoint is not None
    assert first_page.detail_checkpoint.listing_ids == ("1", "2")
    assert first_page.detail_checkpoint.listing_total == 4
    site.stopped = False
    resumed = site.fetcher.continue_listings(
        "Acme", first.deferred_targets, root_url=site.root, handoff_scopes=()
    )
    assert {item.requisition_id for item in resumed.listings} == {"1", "2", "3"}
    assert not resumed.complete
    assert not resumed.discovery_truncated
    assert "publisher_listing_total_mismatch" not in resumed.skip_reasons
    [relist] = [
        item for item in resumed.deferred_targets if item.deferred_reason == "taleo_search_page"
    ]
    assert relist.detail_checkpoint is not None
    assert set(relist.detail_checkpoint.listing_ids) == {"1", "2", "3"}
    assert first_page.url in (resumed.attempted_target_urls or ())
    assert site.requests.count(site.detail_url(1)) == 1
    assert [body["pageNo"] for _, body, _ in site.posts] == [1, 2]

    again = site.fetcher.continue_listings("Acme", (relist,), root_url=site.root, handoff_scopes=())

    assert again.skip_reasons["publisher_listing_total_mismatch"] == 1
    assert "publisher reports 4 listings" in again.note
    assert "retained 3 distinct identities" in again.note
    [manual] = [
        item for item in again.deferred_targets if item.deferred_reason == "taleo_search_page"
    ]
    assert parse_qs(urlsplit(manual.url).query)["__reader_pass"] == ["3"]
    assert not manual.automatic_eligible
    assert site.requests.count(site.detail_url(1)) == 1


def test_taleo_last_page_gap_keeps_the_exact_failed_body_target_and_attempt() -> None:
    site = _TaleoSite()
    site.totals = [4]
    site.failed = {2}
    read = site.fetcher.listings("Acme", site.root)
    assert {item.requisition_id for item in read.listings} == {"1", "3"}
    assert not read.complete
    assert read.raw_count == 3
    assert read.detail.read == 2
    assert read.detail.failed == 1
    assert read.skip_reasons["publisher_listing_total_mismatch"] == 1
    [body] = read.deferred_targets
    assert body.deferred_reason == "taleo_search_detail"
    assert body.url == site.detail_url(2)
    assert body.detail_checkpoint is not None
    assert body.detail_checkpoint.pending_ids == ("2",)
    assert body.detail_checkpoint.failed_attempts == (("2", 1),)
    assert not body.automatic_eligible
    assert set(body.detail_checkpoint.listing_ids) == {"1", "2", "3"}
    assert body.detail_checkpoint.listing_total == 4
    assert [request["pageNo"] for _, request, _ in site.posts] == [1, 2]


def test_taleo_last_page_gap_counts_a_protected_identity_without_reading_its_body() -> None:
    site = _TaleoSite()
    site.totals = [4]
    protected = site.detail_url(2)
    read = site.fetcher.listings(
        "Acme",
        site.root,
        options=SourceReadOptions(protected_posting_urls=frozenset({protected})),
    )
    assert protected not in site.requests
    assert protected in read.protected_posting_urls
    assert {item.requisition_id for item in read.listings} == {"1", "3"}
    assert not read.complete
    assert read.raw_count == 3
    assert read.skip_reasons["publisher_listing_total_mismatch"] == 1
    assert not read.deferred_targets
    [receipt] = read.publisher_listing_receipts
    assert set(receipt.listing_ids) == {"1", "2", "3"}
    assert receipt.listing_total == 4
    assert [body["pageNo"] for _, body, _ in site.posts] == [1, 2]


class _ClassicTaleoSite:
    """A classic (FTL) section answering from trimmed recorded replies."""

    def __init__(self, search_page: str = "taleo_classic_search.html") -> None:
        self.root = "https://acme.taleo.net/careersection/2/jobsearch.ftl"
        self.search_page = search_page
        self.requests: list[str] = []
        self.fetcher = BoardApiFetcher(
            client=PoliteHttpClient(
                user_agent="tailorcv/test",
                sleeper=lambda _seconds: None,
                jitter=lambda _low, _high: 0.0,
                getter=self.get,
                resolver=public_resolver,
            )
        )

    def pages(self) -> list[int]:
        return [
            int(parse_qs(urlsplit(url).query)["rlPager.currentPage"][0])
            for url in self.requests
            if urlsplit(url).path.endswith("jobsearch.ajax")
        ]

    def get(self, url: str, **_kwargs: Any) -> httpx.Response:
        self.requests.append(url)
        request = httpx.Request("GET", url)
        path = urlsplit(url).path
        if path == "/robots.txt":
            return httpx.Response(200, request=request, text="")
        if path.endswith("jobsearch.ftl"):
            name = self.search_page
        elif path.endswith("jobsearch.ajax"):
            # Past its last page the pager answers its last page again.
            page = int(parse_qs(urlsplit(url).query)["rlPager.currentPage"][0])
            name = f"taleo_classic_list_{min(page, 2)}.txt"
        elif path.endswith("jobdetail.ftl"):
            name = "taleo_classic_detail.html"
        else:
            return httpx.Response(404, request=request)
        return httpx.Response(200, request=request, text=(_CLASSIC / name).read_text())


def test_taleo_classic_section_lists_each_record_with_its_job_number_place_and_dates() -> None:
    site = _ClassicTaleoSite()
    read = site.fetcher.listings("Acme", site.root)
    listed = {item.requisition_id: item for item in read.listings}
    assert set(listed) == {"5001", "5002", "5003"}
    first = listed["5001"]
    assert first.title == "Clinical Data Analyst"
    assert listed["5002"].title == "Data Analyst: Clinical Research"
    assert first.url == "https://acme.taleo.net/careersection/2/jobdetail.ftl?job=5001&lang=en"
    assert first.location == "Ontario-Toronto"
    assert listed["5002"].location == "Ontario-Ottawa"
    assert first.facts.employment_types.values == ("Full-time",)
    assert first.facts.closing_date is not None
    assert [item.normalized_value for item in first.facts.closing_date.statements] == ["2026-10-28"]
    assert first.source is not None
    row = json.loads(first.source.record)["row"]
    assert (row["requisitionId"], row["posted"], row["closing"]) == (
        "600101",
        "Oct 14, 2026",
        "Oct 28, 2026",
    )


def test_taleo_classic_section_pages_to_its_stated_total_without_probing_past_it() -> None:
    site = _ClassicTaleoSite()
    read = site.fetcher.listings("Acme", site.root)
    assert site.pages() == [1, 2]
    assert read.complete
    assert read.raw_count == 3
    assert len(read.listings) == 3


def test_taleo_classic_detail_reads_each_history_description_part_once_decoded() -> None:
    site = _ClassicTaleoSite()
    read = site.fetcher.listings("Acme", site.root)
    body = next(item for item in read.listings if item.requisition_id == "5001")
    text = body.published_description
    assert text.count("Review clinical source records: document methods") == 1
    assert text.count("Degree in statistics or epidemiology") == 1
    assert "Acme is an equal opportunity employer." in text
    assert "Guidelines for submitting" not in text
    assert "!*!" not in text and "\\" not in text and "%3C" not in text


def test_taleo_unavailable_classic_section_fails_naming_that_without_listing() -> None:
    site = _ClassicTaleoSite("taleo_classic_unavailable.html")
    with pytest.raises(ValueError, match="career section is unavailable"):
        site.fetcher.listings("Acme", site.root)
    assert not site.pages()
