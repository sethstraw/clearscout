"""Eightfold career pages declare their employer domain and page their jobs API to its count."""

from __future__ import annotations

import json
from collections.abc import Iterator
from html import escape
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from clearscout.application.discovery.source_reading import SourceContinuationTarget
from clearscout.infrastructure.web_fetch.board_adapters import BoardApiFetcher, identify_board
from clearscout.infrastructure.web_fetch.platforms import eightfold
from clearscout.infrastructure.web_fetch.policy import FetchPolicyError, PoliteHttpClient
from tests.support.http import public_resolver


@pytest.fixture(autouse=True)
def _forget_declared_domains() -> Iterator[None]:
    eightfold._DOMAINS.clear()
    yield
    eightfold._DOMAINS.clear()


_BODY = (
    "<h2>Responsibilities</h2><p>Review clinical records and maintain governed reporting.</p>"
    "<h2>Qualifications</h2><p>Use structured data and clear writing with reviewers.</p>"
)


_NO_APPLY = object()
_NO_ERROR = {"message": "", "body": ""}


def _envelope(data: dict[str, object]) -> dict[str, object]:
    return {"status": 200, "error": dict(_NO_ERROR), "data": data, "metadata": None}


def _page(settings: object = None) -> str:
    declared = {"domain": "acme.example", "user": ""} if settings is None else settings
    apply = ""
    if declared is not _NO_APPLY:
        settings_json = escape(json.dumps(declared))
        apply = f'<code id="smartApplyData" style="display:none;">{settings_json}</code>'
    return (
        '<html><head><script>window._EF_GROUP_ID = "acme.example";</script></head>'
        f"<body>{apply}</body></html>"
    )


class _CareerSite:
    def __init__(self, count: int = 25) -> None:
        self.origin = "https://careers.acme.example"
        self.root = self.origin + "/careers"
        self.count = count
        self.requested: list[str] = []
        self.short_answers: dict[int, int] = {}
        self.refused = False
        self.foreign: set[int] = set()
        self.listed_bodies = True
        self.unlisted_bodies: set[int] = set()
        self.error_positions: set[int] = set()
        self.settings: object = None
        self.failed_details: set[int] = set()
        self.stop_after_start: int | None = None
        self.stopped = False
        self.pcsx = False
        self.pcsx_envelope: dict[str, object] | None = None
        self.refused_paths: tuple[str, ...] = ()
        self.robots = ""
        self.fetcher = self.new_fetcher()

    def use_pcsx(self, declared: str | None = "acme.example") -> None:
        """Serve the PCSX shape: no apply configuration and a robots-declared domain."""
        self.pcsx = True
        self.settings = _NO_APPLY
        sitemap = f"?domain={declared}" if declared else ""
        self.robots = (
            "User-agent: *\nDisallow: /\nAllow: /careers\nAllow: /api/apply\nAllow: /api/pcsx\n"
            f"Sitemap: {self.origin}/careers/sitemap_index.xml{sitemap}\n"
        )

    def new_fetcher(self) -> BoardApiFetcher:
        """Build a fresh transport answering from this career site."""
        site = self

        class Fetcher(BoardApiFetcher):
            @staticmethod
            def deadline_elapsed() -> bool:
                return site.stopped or BoardApiFetcher.deadline_elapsed()

        def get(url: str, **_kwargs: object) -> httpx.Response:
            if url.endswith("/robots.txt"):
                return httpx.Response(200, request=httpx.Request("GET", url), text=site.robots)
            return site.answer(url)

        return Fetcher(
            client=PoliteHttpClient(
                user_agent="clearscout/test",
                getter=get,
                sleeper=lambda _seconds: None,
                jitter=lambda _low, _high: 0.0,
                resolver=public_resolver,
            )
        )

    def posting(self, number: int) -> str:
        host = "https://other.example" if number in self.foreign else self.origin
        return f"{host}/careers/job/{number}"

    def position(self, number: int) -> dict[str, object]:
        return {
            "id": number,
            "name": f"Internal Analyst {number}",
            "posting_name": f"Data Analyst {number}",
            "location": "Toronto, ON, Canada",
            "locations": ["Toronto, ON, Canada", "Ottawa, ON, Canada"],
            "canonicalPositionUrl": self.posting(number),
            "ats_job_id": f"R{number}",
            "work_location_option": "onsite",
            "job_description": _BODY
            if self.listed_bodies and number not in self.unlisted_bodies
            else "",
        }

    def jobs(self, start: int) -> list[dict[str, object]]:
        numbers = range(start + 1, min(start + 10, self.count) + 1)
        remaining = self.short_answers.get(start, 0)
        if remaining:
            self.short_answers[start] = remaining - 1
            numbers = numbers[:3]
        return [self.position(number) for number in numbers]

    def answer(self, url: str) -> httpx.Response:
        self.requested.append(url)
        parsed = urlsplit(url)
        request = httpx.Request("GET", url)
        if parsed.path == "/careers":
            return httpx.Response(200, request=request, text=_page(self.settings))
        if self.refused:
            return httpx.Response(403, request=request, text="Forbidden")
        if self.refused_paths and parsed.path.startswith(self.refused_paths):
            return httpx.Response(403, request=request, text="<html>Access denied</html>")
        query = parse_qs(parsed.query)
        if query.get("domain") != ["acme.example"]:
            return httpx.Response(404, request=request)
        if parsed.path.startswith("/api/pcsx/"):
            return self.answer_pcsx(parsed.path, query, request)
        if parsed.path == "/api/apply/v2/jobs":
            start = int(query["start"][0])
            if start == self.stop_after_start:
                self.stopped = True
            body = {"count": self.count, "positions": self.jobs(start)}
            return httpx.Response(200, request=request, json=body)
        if parsed.path.startswith("/api/apply/v2/jobs/"):
            number = int(parsed.path.rsplit("/", 1)[-1])
            if number in self.failed_details:
                return httpx.Response(404, request=request, text="Unavailable")
            return httpx.Response(
                200, request=request, json={**self.position(number), "job_description": _BODY}
            )
        return httpx.Response(404, request=request)

    def pcsx_position(self, number: int) -> dict[str, object]:
        return {
            "id": number,
            "displayJobId": f"D{number}",
            "atsJobId": f"R{number}",
            "name": f"Data Analyst {number}",
            "locations": ["Toronto, ON, Canada", "Ottawa, ON, Canada"],
            "workLocationOption": "hybrid",
            "department": "Analytics",
            "positionUrl": f"/careers/job/{number}",
        }

    def answer_pcsx(
        self, path: str, query: dict[str, list[str]], request: httpx.Request
    ) -> httpx.Response:
        if not self.pcsx:
            return httpx.Response(403, request=request, text="Forbidden")
        if self.pcsx_envelope is not None:
            return httpx.Response(200, request=request, json=self.pcsx_envelope)
        if path == "/api/pcsx/search":
            start = int(query["start"][0])
            if start == self.stop_after_start:
                self.stopped = True
            numbers = range(start + 1, min(start + 10, self.count) + 1)
            data = {"count": self.count, "positions": [self.pcsx_position(n) for n in numbers]}
            return httpx.Response(200, request=request, json=_envelope(data))
        if path == "/api/pcsx/position_details":
            number = int(query["position_id"][0])
            if number in self.error_positions:
                error = {"message": "Position not found", "body": ""}
                answer = {"status": 404, "error": error, "data": {}, "metadata": None}
                return httpx.Response(200, request=request, json=answer)
            if number in self.failed_details:
                return httpx.Response(404, request=request, text="Unavailable")
            data = {**self.pcsx_position(number), "jobDescription": _BODY}
            return httpx.Response(200, request=request, json=_envelope(data))
        return httpx.Response(404, request=request)

    def starts(self, path: str = "/api/apply/v2/jobs") -> list[int]:
        return [
            int(parse_qs(urlsplit(url).query)["start"][0])
            for url in self.requested
            if urlsplit(url).path == path
        ]


def test_an_eightfold_group_assignment_names_the_board_and_a_mention_does_not() -> None:
    named = identify_board(_page())

    assert named is not None
    assert named[0] == "eightfold"
    assert (
        identify_board("<p>We hire through Eightfold. _EF_GROUP_ID is set by script.</p>") is None
    )
    assert identify_board('<script>window._EF_GROUP_IDS = "acme.example";</script>') is None


def test_every_page_to_the_stated_count_reads_complete_with_listed_bodies() -> None:
    site = _CareerSite()

    read = site.fetcher.listings("Acme", site.root)

    assert read.complete is True
    assert site.starts() == [0, 10, 20]
    assert [row.requisition_id for row in read.listings] == [str(n) for n in range(1, 26)]
    first = read.listings[0]
    assert (first.title, first.url) == (
        "Data Analyst 1",
        site.posting(1),
    )
    assert "Review clinical records" in first.published_description
    assert set(first.facts.locations.values) == {"Toronto, ON, Canada", "Ottawa, ON, Canada"}
    assert not any("/api/apply/v2/jobs/" in url for url in site.requested)


def test_the_api_is_asked_for_the_domain_the_page_declares_not_the_host() -> None:
    site = _CareerSite()

    site.fetcher.listings("Acme", site.root)

    api = [url for url in site.requested if "/api/" in url]
    assert api
    assert all(parse_qs(urlsplit(url).query)["domain"] == ["acme.example"] for url in api)


def test_an_empty_listed_body_is_read_from_the_position_api() -> None:
    site = _CareerSite(count=3)
    site.listed_bodies = False

    read = site.fetcher.listings("Acme", site.root)

    assert read.complete is True
    assert all("Review clinical records" in row.published_description for row in read.listings)


def test_a_truncated_page_is_retried_once_and_then_read() -> None:
    site = _CareerSite()
    site.short_answers = {10: 1}

    read = site.fetcher.listings("Acme", site.root)

    assert read.complete is True
    assert site.starts() == [0, 10, 10, 20]
    assert len(read.listings) == 25


def test_a_page_still_truncated_after_its_retry_leaves_its_cursor_and_is_not_the_end() -> None:
    site = _CareerSite()
    site.short_answers = {10: 2}

    read = site.fetcher.listings("Acme", site.root)

    assert read.complete is False
    assert site.starts() == [0, 10, 10]
    assert {row.requisition_id for row in read.listings} == {str(n) for n in range(1, 11)}
    assert [parse_qs(urlsplit(t.url).query)["__reader_page"] for t in read.deferred_targets] == [
        ["10"]
    ]


def test_a_refused_jobs_api_is_unread_not_an_empty_board() -> None:
    site = _CareerSite()
    site.refused = True

    with pytest.raises((httpx.HTTPError, FetchPolicyError)):
        site.fetcher.listings("Acme", site.root)


def test_a_read_cut_by_the_deadline_resumes_at_the_next_start() -> None:
    site = _CareerSite()
    site.stop_after_start = 10

    first = site.fetcher.listings("Acme", site.root)

    assert first.complete is False
    assert site.starts() == [0, 10]
    site.stopped = False
    site.stop_after_start = None
    site.requested.clear()
    site.fetcher = site.new_fetcher()

    resumed = site.fetcher.continue_listings(
        "Acme", first.deferred_targets, root_url=site.root, handoff_scopes=()
    )

    assert 0 not in site.starts()
    assert {row.requisition_id for row in (*first.listings, *resumed.listings)} == {
        str(n) for n in range(1, 26)
    }
    assert not resumed.deferred_targets


def test_a_deadline_cut_keeps_listed_bodies_and_defers_only_rows_needing_a_detail() -> None:
    site = _CareerSite()
    site.unlisted_bodies = {2}
    site.stop_after_start = 10

    read = site.fetcher.listings("Acme", site.root)

    assert {row.requisition_id for row in read.listings} == {str(n) for n in range(1, 21) if n != 2}
    assert [
        target.url
        for target in read.deferred_targets
        if target.deferred_reason == "eightfold_search_detail"
    ] == [site.posting(2)]


def test_an_off_host_canonical_address_is_dropped_and_the_board_is_not_complete() -> None:
    site = _CareerSite(count=3)
    site.foreign = {2}

    read = site.fetcher.listings("Acme", site.root)

    assert {row.requisition_id for row in read.listings} == {"1", "3"}
    assert read.complete is False
    assert not any(url.startswith("https://other.example") for url in site.requested)


def _failed_detail_target(site: _CareerSite) -> SourceContinuationTarget:
    site.listed_bodies = False
    site.failed_details = {2}
    first = site.fetcher.listings("Acme", site.root)
    site.failed_details.clear()
    site.requested.clear()
    site.fetcher = site.new_fetcher()
    return next(t for t in first.deferred_targets if t.deferred_reason == "eightfold_search_detail")


def test_a_detail_continuation_requests_only_the_position_api() -> None:
    site = _CareerSite(count=3)
    target = _failed_detail_target(site)

    read = site.fetcher.continue_listings("Acme", (target,), root_url=site.root, handoff_scopes=())

    assert site.requested == [site.origin + "/api/apply/v2/jobs/2?domain=acme.example"]
    assert [row.requisition_id for row in read.listings] == ["2"]
    assert "Review clinical records" in read.listings[0].published_description


def test_a_detail_continuation_for_an_unread_host_reads_its_page_once() -> None:
    site = _CareerSite(count=3)
    target = _failed_detail_target(site)
    eightfold._DOMAINS.clear()
    detail = site.origin + "/api/apply/v2/jobs/2?domain=acme.example"

    first = site.fetcher.continue_listings("Acme", (target,), root_url=site.root, handoff_scopes=())
    site.fetcher.continue_listings("Acme", (target,), root_url=site.root, handoff_scopes=())

    assert site.requested == [site.root, detail, detail]
    assert "Review clinical records" in first.listings[0].published_description


@pytest.mark.parametrize("settings", [["acme.example"], "acme.example", {"user": ""}])
def test_an_apply_configuration_without_a_domain_object_is_refused(settings: object) -> None:
    site = _CareerSite()
    site.settings = settings

    with pytest.raises(ValueError, match="employer domain"):
        site.fetcher.listings("Acme", site.root)

    assert not any("/api/" in url for url in site.requested)


def test_a_pcsx_board_takes_its_robots_declared_domain_and_pages_search_to_its_count() -> None:
    site = _CareerSite()
    site.use_pcsx()

    read = site.fetcher.listings("Acme", site.root)

    assert read.complete is True
    assert site.starts("/api/pcsx/search") == [0, 10, 20]
    assert [row.requisition_id for row in read.listings] == [str(n) for n in range(1, 26)]
    first = read.listings[0]
    assert (first.title, first.url) == ("Data Analyst 1", site.origin + "/careers/job/1")
    assert "Review clinical records" in first.published_description
    assert set(first.facts.locations.values) == {"Toronto, ON, Canada", "Ottawa, ON, Canada"}
    api = [url for url in site.requested if "/api/" in url]
    assert all(parse_qs(urlsplit(url).query)["domain"] == ["acme.example"] for url in api)
    assert not any("/api/apply/v2/jobs/" in url for url in site.requested)


@pytest.mark.parametrize("declared", [None, "other.example"])
def test_a_pcsx_board_whose_robots_does_not_confirm_its_group_is_refused(
    declared: str | None,
) -> None:
    site = _CareerSite()
    site.use_pcsx(declared)

    with pytest.raises(ValueError, match="employer domain"):
        site.fetcher.listings("Acme", site.root)

    assert not any("/api/" in url for url in site.requested)


def test_a_pcsx_read_cut_by_the_deadline_resumes_at_the_next_start() -> None:
    site = _CareerSite()
    site.use_pcsx()
    site.stop_after_start = 10

    first = site.fetcher.listings("Acme", site.root)

    assert first.complete is False
    assert site.starts("/api/pcsx/search") == [0, 10]
    site.stopped = False
    site.stop_after_start = None
    site.requested.clear()
    site.fetcher = site.new_fetcher()

    resumed = site.fetcher.continue_listings(
        "Acme", first.deferred_targets, root_url=site.root, handoff_scopes=()
    )

    assert site.starts("/api/pcsx/search") == [20]
    assert {row.requisition_id for row in (*first.listings, *resumed.listings)} == {
        str(n) for n in range(1, 26)
    }
    assert not resumed.deferred_targets


def test_a_pcsx_detail_continuation_reads_the_position_details_api() -> None:
    site = _CareerSite(count=3)
    site.use_pcsx()
    target = _failed_detail_target(site)

    read = site.fetcher.continue_listings("Acme", (target,), root_url=site.root, handoff_scopes=())

    details = [url for url in site.requested if "/api/pcsx/position_details" in url]
    assert [parse_qs(urlsplit(url).query)["position_id"] for url in details] == [["2"]]
    assert [row.requisition_id for row in read.listings] == ["2"]
    assert "Review clinical records" in read.listings[0].published_description


def test_a_pcsx_error_answer_for_one_position_defers_only_that_position() -> None:
    site = _CareerSite(count=3)
    site.use_pcsx()
    site.error_positions = {2}

    read = site.fetcher.listings("Acme", site.root)

    assert [row.requisition_id for row in read.listings] == ["1", "3"]
    assert read.complete is False
    assert [
        target.url
        for target in read.deferred_targets
        if target.deferred_reason == "eightfold_search_detail"
    ] == [site.posting(2)]


def test_a_refused_pcsx_search_is_unread_not_an_empty_board() -> None:
    site = _CareerSite()
    site.use_pcsx()
    site.refused_paths = ("/api/pcsx/",)

    with pytest.raises((httpx.HTTPError, FetchPolicyError)):
        site.fetcher.listings("Acme", site.root)

    assert not any("/api/apply/" in url for url in site.requested)


def test_a_classic_board_whose_jobs_api_is_refused_is_unread_and_never_asks_pcsx() -> None:
    site = _CareerSite()
    site.refused_paths = ("/api/apply/",)

    with pytest.raises((httpx.HTTPError, FetchPolicyError)):
        site.fetcher.listings("Acme", site.root)

    assert not any("/api/pcsx/" in url for url in site.requested)


def test_a_refused_classic_detail_leaves_the_next_read_on_the_jobs_api() -> None:
    site = _CareerSite(count=3)
    site.listed_bodies = False
    site.refused_paths = ("/api/apply/v2/jobs/",)
    site.fetcher.listings("Acme", site.root)
    site.refused_paths = ()
    site.requested.clear()
    site.fetcher = site.new_fetcher()

    read = site.fetcher.listings("Acme", site.root)

    assert read.complete is True
    assert site.starts() == [0]
    assert not any("/api/pcsx/" in url for url in site.requested)


@pytest.mark.parametrize(
    "envelope",
    [
        {
            "status": 400,
            "error": {"message": "Invalid domain", "body": ""},
            "data": {"count": 0, "positions": []},
        },
        {
            "status": 200,
            "error": {"message": "bad domain", "body": ""},
            "data": {"count": 0, "positions": []},
        },
        {"status": 200, "error": "Invalid domain", "data": {"count": 0, "positions": []}},
        {"status": 200, "error": _NO_ERROR, "data": {"positions": []}},
        {"status": 200, "error": _NO_ERROR, "data": {"count": "0", "positions": []}},
    ],
)
def test_a_pcsx_search_that_answers_an_error_or_no_count_is_refused_not_empty(
    envelope: dict[str, object],
) -> None:
    site = _CareerSite()
    site.use_pcsx()
    site.pcsx_envelope = envelope

    with pytest.raises(ValueError, match="PCSX"):
        site.fetcher.listings("Acme", site.root)
