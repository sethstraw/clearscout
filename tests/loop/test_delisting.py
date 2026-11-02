"""Delisted postings are recognized as closed, not queued for recovery.

A specific posting URL that is gone -- a board that no longer lists it, or a
redirect off the posting to the board's search page -- must read as a closure so
the row is withdrawn, not left as "render required" with a paid recovery offered.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tailorcv.application.discovery.posting_text import closure_signal
from tailorcv.domain.records import PostingReadIntent
from tailorcv.domain.staged_facts import ListingIdentityTrust
from tailorcv.infrastructure.web_fetch.ingest.url import (
    PublisherReadRefusedError,
    check_posting,
    from_url,
    shared_posting_feeds,
    workday_posting_detail,
)
from tailorcv.infrastructure.web_fetch.ingest.url.addresses import redirected_off_the_posting
from tailorcv.infrastructure.web_fetch.ingest.url.platform_readers import check_lever, check_workday
from tailorcv.infrastructure.web_fetch.policy import RobotsDeniedError
from tailorcv.shared.errors import IngestError
from tests.support.http import DAYFORCE_POSTING_URL, CountingFeedClient, dayforce_feed_row

_ASHBY_URL = "https://jobs.ashbyhq.com/globex/3c9a7e14-8b2d-4f6a-a1c5-7d0e9b2f4c81"


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retryable-status pause is irrelevant to these deterministic router tests."""
    monkeypatch.setattr(
        "tailorcv.infrastructure.web_fetch.ingest.url.transport.time.sleep", lambda *_: None
    )


class _Client:
    """A client whose Ashby board API returns a jobs list not holding the posting."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def get(self, url: str, **_kwargs: object) -> httpx.Response:
        return httpx.Response(200, request=httpx.Request("GET", url), json=self.payload)


def _board_without_the_posting() -> _Client:
    return _Client({"jobs": [{"id": "a-different-open-job", "title": "Something else"}]})


def test_ashby_board_that_no_longer_lists_the_posting_reads_as_closed() -> None:
    checked = check_posting(_ASHBY_URL, client=_board_without_the_posting())  # type: ignore[arg-type]
    assert checked.status == "closed"
    assert "has closed" in checked.note.casefold()


def test_a_transiently_empty_ashby_board_is_unreadable_not_closed() -> None:
    """An empty board is a transient read, not proof this posting closed."""
    checked = check_posting(_ASHBY_URL, client=_Client({"jobs": []}))  # type: ignore[arg-type]
    assert checked.status == "unreadable"


def test_from_url_raises_a_closure_when_the_board_no_longer_lists_the_posting() -> None:
    """The authoritative board verdict becomes a closure the fetcher records."""
    with pytest.raises(IngestError) as raised:
        from_url(_ASHBY_URL, client=_board_without_the_posting())  # type: ignore[arg-type]
    assert closure_signal(str(raised.value))


def test_a_delisted_icims_posting_redirect_reads_as_leaving_the_posting() -> None:
    posting = "https://careers-northwind.icims.com/jobs/30001/job?in_iframe=1"
    search = "https://careers-northwind.icims.com/jobs/search?ss=1&notFound=1"
    assert redirected_off_the_posting(posting, search) is True


def test_a_posting_address_inside_a_redirect_is_not_read_as_left() -> None:
    wrapped = "https://click.example.net/r?u=https://careers-acme.icims.com/jobs/30001/job"
    assert redirected_off_the_posting(wrapped, "https://www.example.com/") is False


def test_an_icims_posting_that_still_resolves_is_not_read_as_left() -> None:
    posting = "https://careers-northwind.icims.com/jobs/30001/job?in_iframe=1"
    assert redirected_off_the_posting(posting, posting) is False


def test_the_slug_bearing_icims_posting_shape_is_also_recognised() -> None:
    posting = "https://careers-northwind.icims.com/jobs/30001/software-developer/job"
    search = "https://careers-northwind.icims.com/jobs/search?notFound=1"
    assert redirected_off_the_posting(posting, search) is True


@pytest.mark.parametrize(
    "posting",
    ["https://acme.app.loxo.co/job/MTAwMTQtd2lkZ2V0cw==", "https://acme.app.loxo.co/job/YWJjZGVm"],
    ids=["digit-bearing", "letters-only"],
)
def test_a_loxo_posting_redirected_to_its_board_reads_as_leaving_the_posting(
    posting: str,
) -> None:
    assert redirected_off_the_posting(posting, "https://acme.app.loxo.co/jobs") is True
    assert redirected_off_the_posting(posting, posting) is False


class _Router:
    """A client returning a canned (status, body) answer per URL substring.

    A mapping or list body is served as JSON and a string body as text. The first
    matching route wins; an unmatched URL answers HTTP 404 so a test that forgets
    a route fails loudly rather than silently reading nothing. A URL matching
    ``denied`` is refused the way the fetch policy refuses a disallowed host, and
    ``redirected_to`` answers from that address instead of the requested one.
    A POSTed site search is answered from ``searches`` by the text it searched for.
    """

    def __init__(
        self,
        routes: list[tuple[str, int, dict[str, Any] | list[Any] | str | None]],
        *,
        denied: tuple[str, ...] = (),
        redirected_to: str = "",
        searches: dict[str, tuple[int, dict[str, Any] | str | None]] | None = None,
    ) -> None:
        self.routes = routes
        self.denied = denied
        self.redirected_to = redirected_to
        self.searches = searches or {}
        self.requested: list[str] = []
        self.searched: list[dict[str, Any]] = []

    def sleeper(self, _seconds: float) -> None:
        """Return at once, since a canned answer has nothing to wait for."""

    def get(self, url: str, **_kwargs: object) -> httpx.Response:
        self.requested.append(url)
        if any(substring in url for substring in self.denied):
            raise RobotsDeniedError(url)
        request = httpx.Request("GET", self.redirected_to or url)
        for substring, status, payload in self.routes:
            if substring in url:
                return _canned(status, request, payload)
        return httpx.Response(404, request=request)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        body = kwargs["json"]
        self.searched.append(body)
        status, payload = self.searches.get(body["searchText"], (404, None))
        return _canned(status, httpx.Request("POST", url), payload)


def _canned(
    status: int, request: httpx.Request, payload: dict[str, Any] | list[Any] | str | None
) -> httpx.Response:
    """Serve a mapping or list as JSON, a string as text, and None as an empty body."""
    if payload is None:
        return httpx.Response(status, request=request)
    if isinstance(payload, str):
        return httpx.Response(status, request=request, text=payload)
    return httpx.Response(status, request=request, json=payload)


_GREENHOUSE_URL = "https://job-boards.greenhouse.io/acme/jobs/999"
_LEVER_URL = "https://jobs.lever.co/acme/5e2d8b90-1f4c-4a7e-b3d6-8c9a0e1f2b47"
_LEVER_EU_URL = "https://jobs.eu.lever.co/acme/5e2d8b90-1f4c-4a7e-b3d6-8c9a0e1f2b47"
_WORKDAY_URL = "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/R-123"
_SMARTRECRUITERS_URL = "https://jobs.smartrecruiters.com/Globex/743999000111-staff-data-engineer"
_DAYFORCE_URL = DAYFORCE_POSTING_URL.format(reference="12345")
_DAYFORCE_OTHER_URL = DAYFORCE_POSTING_URL.format(reference="67890")
# iCIMS is a recognized posting address with no API checker behind it, so its
# pages exercise what a posting's own markup can answer.
_ICIMS_URL = "https://careers-acme.icims.com/jobs/12345/job"
_ICIMS_SLUG_URL = "https://careers-acme.icims.com/jobs/12345/clinical-data-manager/job"
_ICIMS_OTHER_URL = "https://careers-acme.icims.com/jobs/67890/job"
_ICIMS_INDEX = "https://careers-acme.icims.com/jobs/search?notFound=1"


def _declared_block(title: str, employer: str, *, url: str = "", identifier: str = "") -> str:
    """Build one JobPosting block, stating only the identity fields it is given."""
    stated: dict[str, Any] = {
        "@type": "JobPosting",
        "title": title,
        "hiringOrganization": {"name": employer},
        "description": "Build governed data pipelines for clinical analytics. " * 12,
    }
    if url:
        stated["url"] = url
    if identifier:
        stated["identifier"] = identifier
    return json.dumps(stated)


def _page_declaring(*blocks: str) -> str:
    """Build a posting page stating each block in its own linked-data script."""
    scripts = "".join(f'<script type="application/ld+json">{block}</script>' for block in blocks)
    return f"<html><head>{scripts}</head><body><main><h1>Careers</h1></main></body></html>"


def test_greenhouse_single_404_and_listing_omitting_it_reads_as_closed() -> None:
    """The single-job 404 is distrusted alone; the complete listing omitting it closes."""
    client = _Router(
        [("jobs?content=true", 200, {"jobs": [{"id": 111}]}), ("/jobs/999", 404, None)]
    )
    checked = check_posting(_GREENHOUSE_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "closed"
    assert closure_signal(checked.note)


def test_greenhouse_transiently_empty_board_stays_unreadable() -> None:
    """An empty board listing is a transient read, not proof the posting closed."""
    client = _Router([("jobs?content=true", 200, {"jobs": []}), ("/jobs/999", 404, None)])
    checked = check_posting(_GREENHOUSE_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "unreadable"


def test_lever_single_posting_404_reads_as_closed_on_a_liveness_fetch() -> None:
    """A direct fetch of a gone Lever posting records a closure via its API 404."""
    client = _Router([("api.lever.co/v0/postings", 404, None)])
    with pytest.raises(IngestError) as raised:
        from_url(_LEVER_URL, client=client)  # type: ignore[arg-type]
    assert closure_signal(str(raised.value))


def test_lever_404_on_the_check_path_reads_as_closed() -> None:
    """The Find-jobs refresh is a deliberate liveness check, so a 404 closes."""
    client = _Router([("api.lever.co/v0/postings", 404, None)])
    checked = check_posting(_LEVER_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "closed"


def test_lever_410_gone_also_reads_as_closed() -> None:
    """Not every publisher answers a gone posting with 404; 410 Gone closes too."""
    client = _Router([("api.lever.co/v0/postings", 410, None)])
    checked = check_posting(_LEVER_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "closed"


def test_lever_transient_503_is_not_closed_even_on_a_liveness_fetch() -> None:
    """A 503 is a transient read; only 404/410 from the canonical endpoint closes."""
    client = _Router([("api.lever.co/v0/postings", 503, None)])
    checked = check_lever(_LEVER_URL, client=client, gone_is_closure=True)  # type: ignore[arg-type]
    assert checked is not None and checked.status == "unreadable"


def test_a_lever_address_inside_a_redirect_is_not_read_from_the_lever_api() -> None:
    client = _Router([("api.lever.co/v0/postings", 200, {"text": "Staff Engineer"})])
    checked = check_lever(f"https://click.example.net/r?u={_LEVER_URL}", client=client)  # type: ignore[arg-type]
    assert checked is None
    assert client.requested == []


def test_lever_eu_host_is_read_from_the_eu_api() -> None:
    client = _Router([("api.eu.lever.co/v0/postings", 200, {"text": "Staff Engineer"})])
    checked = check_posting(_LEVER_EU_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "readable"
    assert any("api.eu.lever.co" in url for url in client.requested)


def test_workday_cxs_404_reads_as_closed_on_a_liveness_fetch() -> None:
    """A direct fetch of a gone Workday requisition records a closure via CXS 404."""
    client = _Router([("/wday/cxs/", 404, None)])
    with pytest.raises(IngestError) as raised:
        from_url(_WORKDAY_URL, client=client)  # type: ignore[arg-type]
    assert closure_signal(str(raised.value))


def test_workday_404_on_the_check_path_reads_as_closed() -> None:
    """The Find-jobs refresh closes a gone Workday requisition on its CXS 404."""
    client = _Router([("/wday/cxs/", 404, None)])
    checked = check_posting(_WORKDAY_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "closed"
    assert "(HTTP 404)" in checked.note


def test_workday_detail_enrichment_stays_unreadable_on_a_404() -> None:
    """Enrichment reads a just-listed job's detail; a race 404 must not close it."""
    client = _Router([("/wday/cxs/", 404, None)])
    checked = workday_posting_detail(_WORKDAY_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "unreadable"


@pytest.mark.parametrize("status", [422, 429, 503])
def test_workday_wrong_site_throttling_or_outage_is_never_closed(status: int) -> None:
    """A renamed site (422), throttling, or an outage is no refusal, so no search can close it."""
    client = _Router([("/wday/cxs/", status, None)], searches=_workday_site_search())
    checked = check_workday(_WORKDAY_REQUISITION_URL, client=client, gone_is_closure=True)  # type: ignore[arg-type]
    assert checked is not None and checked.status == "unreadable"


_WORKDAY_REQUISITION_URL = (
    "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/Springfield-IL/"
    "Clinical-Data-Manager_R-123"
)
_WORKDAY_REFUSED = {
    "errorCode": "S22",
    "errorCaseId": "0a1b2c3d-case",
    "httpStatus": 403,
    "message": "permission denied",
    "messageParams": {},
}
_WORKDAY_REQUESTED_POSTING = {
    "title": "Clinical Data Manager",
    "externalPath": "/job/Springfield-IL/Clinical-Data-Manager_R-123",
    "bulletFields": ["R-123"],
}
_WORKDAY_CONTROL_POSTING = {
    "title": "Staff Data Engineer",
    "externalPath": "/job/Shelbyville/Staff-Data-Engineer_R-456",
    "bulletFields": ["R-456"],
}


def _workday_found(
    *postings: dict[str, Any], total: int | None = None
) -> tuple[int, dict[str, Any]]:
    """Answer one site search with these postings."""
    stated = len(postings) if total is None else total
    return 200, {"total": stated, "jobPostings": list(postings), "facets": []}


def _workday_site_search(
    overrides: dict[str, tuple[int, dict[str, Any] | str | None]] | None = None,
) -> dict[str, tuple[int, dict[str, Any] | str | None]]:
    """Script a populated site whose search finds its listed control and not R-123."""
    return {
        "R-123": _workday_found(),
        "": _workday_found(_WORKDAY_CONTROL_POSTING, total=12),
        "R-456": _workday_found(_WORKDAY_CONTROL_POSTING),
        **(overrides or {}),
    }


@pytest.mark.parametrize("status", [401, 403])
def test_a_workday_refusal_the_site_search_no_longer_finds_reads_as_closed(status: int) -> None:
    """A search that finds a listed control but not this requisition proves the delisting."""
    client = _Router([("/wday/cxs/", status, _WORKDAY_REFUSED)], searches=_workday_site_search())
    checked = check_posting(_WORKDAY_REQUISITION_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "closed"
    assert closure_signal(checked.note)
    assert f"HTTP {status}, errorCode S22" in checked.note
    assert {body["limit"] for body in client.searched} == {20}


def test_a_liveness_read_of_a_workday_requisition_its_search_omits_records_the_closure() -> None:
    client = _Router(
        [("/wday/cxs/", 403, _WORKDAY_REFUSED), ("myworkdayjobs.com", 200, _WORKDAY_SHELL)],
        searches=_workday_site_search(),
    )
    with pytest.raises(IngestError) as raised:
        from_url(_WORKDAY_REQUISITION_URL, client=client)  # type: ignore[arg-type]
    assert closure_signal(str(raised.value))


@pytest.mark.parametrize(
    "listed",
    [
        pytest.param(_WORKDAY_REQUESTED_POSTING, id="by-id-and-address"),
        pytest.param(
            {**_WORKDAY_REQUESTED_POSTING, "bulletFields": ["Springfield, IL"]}, id="by-address"
        ),
        pytest.param(
            {
                "externalPath": "/job/springfield-il/clinical-data-manager_r-123",
                "bulletFields": [],
            },
            id="by-address-in-another-case",
        ),
        pytest.param(
            {**_WORKDAY_REQUESTED_POSTING, "externalPath": "/job/Remote/Data-Manager_R-123"},
            id="by-id",
        ),
    ],
)
def test_workday_403_permission_denied_is_unreadable_not_closed(listed: dict[str, Any]) -> None:
    """S22 "permission denied" leaves a requisition the site's own search still lists open."""
    client = _Router(
        [("/wday/cxs/", 403, _WORKDAY_REFUSED)],
        searches=_workday_site_search({"R-123": _workday_found(listed)}),
    )
    checked = check_posting(_WORKDAY_REQUISITION_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "unreadable"
    assert checked.note.startswith("Workday returned HTTP 403, errorCode S22")
    assert "still lists R-123" in checked.note
    assert not closure_signal(checked.note)


@pytest.mark.parametrize(
    "searched",
    [
        pytest.param({"R-123": (404, None)}, id="requisition-search-not-found"),
        pytest.param({"R-123": (200, {"total": 0})}, id="requisition-search-malformed"),
        pytest.param({"R-123": (200, "<html></html>")}, id="requisition-search-not-json"),
        pytest.param({"": _workday_found(total=0)}, id="empty-site"),
        pytest.param({"": (503, None)}, id="unfiltered-search-fails"),
        pytest.param(
            {"": _workday_found({**_WORKDAY_CONTROL_POSTING, "bulletFields": ["Shelbyville"]})},
            id="listing-names-no-requisition",
        ),
        pytest.param({"R-456": _workday_found()}, id="control-search-misses"),
    ],
)
def test_a_workday_refusal_the_site_search_cannot_confirm_stays_unreadable(
    searched: dict[str, tuple[int, dict[str, Any] | str | None]],
) -> None:
    """Some sites search by meaning or bullet a place, so a miss needs a found control."""
    client = _Router(
        [("/wday/cxs/", 403, _WORKDAY_REFUSED)], searches=_workday_site_search(searched)
    )
    checked = check_posting(_WORKDAY_REQUISITION_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "unreadable"
    assert "could not confirm" in checked.note
    assert not closure_signal(checked.note)


_WORKDAY_SUFFIXED_PATH = "/job/Springfield-IL/Clinical-Data-Manager_R-123-1"
_WORKDAY_SUFFIXED_URL = f"https://acme.wd5.myworkdayjobs.com/en-US/careers{_WORKDAY_SUFFIXED_PATH}"


@pytest.mark.parametrize(
    "listed",
    [
        pytest.param({"externalPath": "/job/Remote/Data-Manager_R-123"}, id="by-stem"),
        pytest.param({"externalPath": _WORKDAY_SUFFIXED_PATH, "bulletFields": []}, id="by-address"),
    ],
)
def test_a_suffixed_workday_requisition_whose_stem_search_lists_it_stays_unreadable(
    listed: dict[str, Any],
) -> None:
    """Bullets state a suffixed requisition's stem, which the search finds when its id misses."""
    posting = {"title": "Clinical Data Manager", "bulletFields": ["R-123"], **listed}
    client = _Router(
        [("/wday/cxs/", 403, _WORKDAY_REFUSED)],
        searches=_workday_site_search(
            {"R-123-1": _workday_found(), "R-123": _workday_found(posting)}
        ),
    )
    checked = check_posting(_WORKDAY_SUFFIXED_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "unreadable"
    assert "still lists R-123-1" in checked.note
    assert not closure_signal(checked.note)


def test_a_suffixed_workday_requisition_its_search_omits_under_both_ids_reads_as_closed() -> None:
    client = _Router(
        [("/wday/cxs/", 403, _WORKDAY_REFUSED)],
        searches=_workday_site_search({"R-123-1": _workday_found(), "R-123": _workday_found()}),
    )
    checked = check_posting(_WORKDAY_SUFFIXED_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "closed"
    assert "finds listed R-456 but not R-123-1 or R-123" in checked.note


def test_one_deadline_bounds_every_read_of_a_workday_liveness_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow tenant's searches share the detail read's deadline rather than each taking one."""
    now = [1000.0]
    monkeypatch.setattr(
        "tailorcv.infrastructure.web_fetch.ingest.url.transport.time.monotonic", lambda: now[0]
    )

    class _SlowRouter(_Router):
        def get(self, url: str, **kwargs: object) -> httpx.Response:
            now[0] += 12.0
            return super().get(url, **kwargs)

        def post(self, url: str, **kwargs: Any) -> httpx.Response:
            now[0] += 12.0
            return super().post(url, **kwargs)

    client = _SlowRouter([("/wday/cxs/", 403, _WORKDAY_REFUSED)], searches=_workday_site_search())
    checked = check_posting(_WORKDAY_REQUISITION_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "unreadable"
    # The career page read for the stated tenant spends the same deadline.
    assert "the unfiltered search could not be reached (ReadTimeout)" in checked.note


def test_a_crawl_never_closes_a_workday_refusal() -> None:
    """Enrichment reads a just-listed job, so even a search that omits it closes nothing."""
    client = _Router([("/wday/cxs/", 403, _WORKDAY_REFUSED)], searches=_workday_site_search())
    checked = workday_posting_detail(_WORKDAY_REQUISITION_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "unreadable"


def test_workday_not_found_error_code_reads_as_closed_with_its_evidence() -> None:
    body = {"errorCode": "S21", "httpStatus": 400, "message": "not found"}
    client = _Router([("/wday/cxs/", 400, body)])
    checked = check_posting(_WORKDAY_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "closed"
    assert "HTTP 400, errorCode S21" in checked.note
    assert closure_signal(checked.note)


def test_workday_bare_403_without_an_error_body_stays_unreadable() -> None:
    """A WAF/CDN 403 carries no Workday errorCode, so even an omitting search cannot close it."""
    client = _Router([("/wday/cxs/", 403, None)], searches=_workday_site_search())
    checked = check_workday(_WORKDAY_REQUISITION_URL, client=client, gone_is_closure=True)  # type: ignore[arg-type]
    assert checked is not None and checked.status == "unreadable"


def test_oracle_absent_requisition_reads_as_closed_on_the_check_path() -> None:
    """The Find-jobs refresh closes an Oracle requisition its ById finder omits."""
    oracle_url = (
        "https://acme.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_4/job/2020001"
    )
    client = _Router([("hcmRestApi", 200, {"items": []})])
    checked = check_posting(oracle_url, client=client)  # type: ignore[arg-type]
    assert checked.status == "closed"
    assert closure_signal(checked.note)


def test_oracle_answer_without_an_item_list_is_unreadable_not_closed() -> None:
    """A 200 missing its item list, or contradicting its own count, is no delisting."""
    oracle_url = (
        "https://acme.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_4/job/2020001"
    )
    for payload in ({"error": "shape changed"}, {"items": [], "count": 1}):
        client = _Router([("hcmRestApi", 200, payload)])
        checked = check_posting(oracle_url, client=client)  # type: ignore[arg-type]
        assert checked.status == "unreadable"


def test_oracle_transient_failure_stays_unreadable() -> None:
    """A non-200 from the Oracle finder is transient, not a delisting."""
    oracle_url = (
        "https://acme.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_4/job/2020001"
    )
    client = _Router([("hcmRestApi", 503, None)])
    checked = check_posting(oracle_url, client=client)  # type: ignore[arg-type]
    assert checked.status == "unreadable"


def test_the_dayforce_feed_states_the_identity_of_the_posting_it_lists() -> None:
    """Dayforce publishes no single-posting endpoint, so its whole site feed answers."""
    listed = dayforce_feed_row("12345", "Clinical Data Manager")
    client = CountingFeedClient(
        [
            listed,
            # A posting is stated once per region it serves.
            {**listed, "City": "Shelbyville"},
            dayforce_feed_row("67890", "Staff Data Engineer"),
        ]
    )
    checked = check_posting(_DAYFORCE_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "readable"
    assert (checked.title, checked.employer) == ("Clinical Data Manager", "Acme Health")
    assert checked.location == "Springfield, IL, United States"
    assert checked.facts.identity_trust is ListingIdentityTrust.PUBLISHER
    assert client.requested == [
        "https://www.dayforcehcm.com/api/globex/v1/jobfeeds?internalJobBoardCode=CANDIDATEPORTAL"
    ]


def test_a_dayforce_feed_that_no_longer_lists_the_posting_reads_as_closed() -> None:
    """The feed states every open job on the site, so an omission is a delisting."""
    client = CountingFeedClient([dayforce_feed_row("67890", "Staff Data Engineer")])
    checked = check_posting(_DAYFORCE_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "closed"
    assert closure_signal(checked.note)


@pytest.mark.parametrize("payload", [[], {"error": "shape changed"}, "not a feed"])
def test_a_dayforce_feed_stating_no_postings_is_unreadable_not_closed(
    payload: dict[str, Any] | str | list[Any],
) -> None:
    """An empty or malformed feed is a transient read, not proof this posting closed."""
    client = _Router([("dayforcehcm.com/api", 200, payload)])
    checked = check_posting(_DAYFORCE_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "unreadable"
    assert not closure_signal(checked.note)


def test_one_pass_reads_a_site_feed_once_for_every_posting_on_it() -> None:
    """The feed answers for the whole site, so a batch refresh must not download it per row."""
    client = CountingFeedClient(
        [
            dayforce_feed_row("12345", "Clinical Data Manager"),
            dayforce_feed_row("67890", "Staff Data Engineer"),
        ]
    )
    with shared_posting_feeds():
        first = check_posting(_DAYFORCE_URL, client=client)  # type: ignore[arg-type]
        second = check_posting(_DAYFORCE_OTHER_URL, client=client)  # type: ignore[arg-type]
    assert (first.title, second.title) == ("Clinical Data Manager", "Staff Data Engineer")
    assert len(client.requested) == 1


def test_a_crawl_does_not_reuse_a_feed_read_the_exemption_obtained() -> None:
    """A read shared inside a pass answers only the intent it ran at."""
    client = CountingFeedClient([dayforce_feed_row("12345", "Clinical Data Manager")])
    with shared_posting_feeds():
        check_posting(
            _DAYFORCE_URL,
            client=client,  # type: ignore[arg-type]
            intent=PostingReadIntent.SINGLE_POSTING,
        )
        check_posting(_DAYFORCE_URL, client=client)  # type: ignore[arg-type]
    assert len(client.requested) == 2


def test_a_later_pass_reads_the_site_feed_again() -> None:
    """A posting withdrawn between passes has to be visible, so no read outlives its pass."""
    client = CountingFeedClient([dayforce_feed_row("12345", "Clinical Data Manager")])
    for _ in range(2):
        with shared_posting_feeds():
            checked = check_posting(_DAYFORCE_URL, client=client)  # type: ignore[arg-type]
            assert checked.status == "readable"
    assert len(client.requested) == 2


def test_a_refused_dayforce_feed_is_a_publisher_refusal_not_a_shell_page_read() -> None:
    """The posting page is a client-side shell, so a refused feed leaves nothing to read."""
    shell = "<html><body><main><p>Loading this position.</p></main></body></html>"
    client = _Router([("dayforcehcm.com", 200, shell)], denied=("dayforcehcm.com/api",))
    with pytest.raises(PublisherReadRefusedError) as raised:
        from_url(_DAYFORCE_URL, client=client)  # type: ignore[arg-type]
    assert "Dayforce could not be reached" in str(raised.value)


_BAMBOOHR_URL = "https://globex.bamboohr.com/careers/77"


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (403, None),
        (500, None),
        (200, {"result": None}),
    ],
)
def test_a_bamboohr_detail_holding_no_opening_is_unreadable_not_closed(
    status: int, payload: dict[str, Any] | None
) -> None:
    """Only a gone endpoint or a stated non-open status closes a BambooHR posting."""
    client = _Router([("bamboohr.com/careers/77/detail", status, payload)])
    checked = check_posting(_BAMBOOHR_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "unreadable"
    assert not closure_signal(checked.note)


def test_a_bamboohr_opening_stating_no_status_still_reads() -> None:
    opening = {"jobOpeningName": "Clinical Data Manager", "description": f"<p>{'x ' * 400}</p>"}
    client = _Router([("bamboohr.com/careers/77/detail", 200, {"result": {"jobOpening": opening}})])
    checked = check_posting(_BAMBOOHR_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "readable"
    assert checked.title == "Clinical Data Manager"


def test_a_refused_bamboohr_detail_is_a_publisher_refusal_not_a_shell_page_read() -> None:
    """The careers page is a shell the detail endpoint fills, so a refusal leaves nothing."""
    shell = "<html><body><main><p>Loading this position.</p></main></body></html>"
    client = _Router([("bamboohr.com", 200, shell)], denied=("/detail",))
    with pytest.raises(PublisherReadRefusedError):
        from_url(_BAMBOOHR_URL, client=client)  # type: ignore[arg-type]


_GEM_URL = "https://jobs.gem.com/globex/am9icG9zdDrChY8Z"


@pytest.mark.parametrize(("status", "payload"), [(403, None), (500, None), (200, {"id": "x"})])
def test_a_gem_post_holding_no_record_is_unreadable_not_closed(
    status: int, payload: dict[str, Any] | None
) -> None:
    client = _Router([("api.gem.com/job_board", status, payload)])
    checked = check_posting(_GEM_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "unreadable"
    assert not closure_signal(checked.note)


_TELUS_URL = "https://www.telusinternational.ai/cmp/public/jobs/available/4321"


@pytest.mark.parametrize(
    ("status", "payload"), [(403, None), (500, None), (200, {"id": 4321, "status": "active"})]
)
def test_a_telus_post_holding_no_record_is_unreadable_not_closed(
    status: int, payload: dict[str, Any] | None
) -> None:
    client = _Router([("api.telusinternational.ai", status, payload)])
    checked = check_posting(_TELUS_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "unreadable"
    assert not closure_signal(checked.note)


def test_a_refused_telus_post_is_a_publisher_refusal_not_a_shell_page_read() -> None:
    shell = "<html><body><main><p>Loading this position.</p></main></body></html>"
    client = _Router([("telusinternational.ai", 200, shell)], denied=("api.telusinternational.ai",))
    with pytest.raises(PublisherReadRefusedError):
        from_url(_TELUS_URL, client=client)  # type: ignore[arg-type]


def test_a_refused_gem_post_is_a_publisher_refusal_not_a_shell_page_read() -> None:
    """The post page is a script bundle the board API fills, so a refusal leaves nothing."""
    shell = "<html><body><main><p>Loading this position.</p></main></body></html>"
    client = _Router([("jobs.gem.com", 200, shell)], denied=("api.gem.com",))
    with pytest.raises(PublisherReadRefusedError):
        from_url(_GEM_URL, client=client)  # type: ignore[arg-type]


# A career-site shell states enough site chrome to pass the length floor.
_WORKDAY_SHELL = (
    "<html><head><script src='/wday/assets/app.js'></script></head><body><main>"
    "<p>Acme Health is a regional provider serving families across three counties.</p>"
    "<p>Search our open roles by keyword, location, or department, and save a search "
    "to hear about new ones as they are posted.</p>"
    "<p>Create a candidate profile to track each application you submit and to "
    "update your contact details at any time.</p>"
    "<p>We welcome applicants of every background and provide accommodations during "
    "the hiring process on request.</p>"
    "<p>Follow us to learn about our teams, our benefits, and the communities we "
    "serve.</p>"
    "</main></body></html>"
)
_WORKDAY_STILL_LISTED = _workday_site_search({"R-123": _workday_found(_WORKDAY_REQUESTED_POSTING)})


def test_a_refused_workday_detail_is_a_publisher_refusal_not_a_shell_page_read() -> None:
    """The career site renders from the refused API, so its shell holds no posting to read."""
    client = _Router(
        [("/wday/cxs/", 403, _WORKDAY_REFUSED), ("myworkdayjobs.com", 200, _WORKDAY_SHELL)],
        searches=_WORKDAY_STILL_LISTED,
    )
    with pytest.raises(PublisherReadRefusedError) as raised:
        from_url(_WORKDAY_REQUISITION_URL, client=client)  # type: ignore[arg-type]
    assert raised.value.http_status == 403
    assert not closure_signal(str(raised.value))


def test_a_refused_workday_detail_still_reads_a_page_declaring_the_posting() -> None:
    page = _page_declaring(
        _declared_block("Clinical Data Manager", "Acme Health", identifier="R-123")
    )
    client = _Router(
        [("/wday/cxs/", 403, _WORKDAY_REFUSED), ("myworkdayjobs.com", 200, page)],
        searches=_WORKDAY_STILL_LISTED,
    )
    result = from_url(_WORKDAY_REQUISITION_URL, client=client)  # type: ignore[arg-type]
    assert result.method == "publisher markup"
    assert result.title == "Clinical Data Manager"


def test_a_refused_publisher_api_falls_back_to_what_the_posting_page_declares() -> None:
    """An API host that refuses automated reads leaves the page's own markup to answer."""
    page = _page_declaring(
        _declared_block("Staff Data Engineer", "Globex Health", url=_SMARTRECRUITERS_URL)
    )
    client = _Router(
        [("jobs.smartrecruiters.com", 200, page)],
        denied=("api.smartrecruiters.com",),
    )
    checked = check_posting(_SMARTRECRUITERS_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "readable"
    assert (checked.title, checked.employer) == ("Staff Data Engineer", "Globex Health")
    assert client.requested.count(_SMARTRECRUITERS_URL) == 1


def test_a_closed_publisher_verdict_is_not_overridden_by_the_posting_page() -> None:
    """A board that no longer lists the posting outranks whatever its page still declares."""
    page = _page_declaring(_declared_block("Staff Data Engineer", "Globex Health", url=_ASHBY_URL))
    client = _Router(
        [
            ("api.ashbyhq.com", 200, {"jobs": [{"id": "a-different-open-job"}]}),
            ("jobs.ashbyhq.com", 200, page),
        ]
    )
    checked = check_posting(_ASHBY_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "closed"
    assert _ASHBY_URL not in client.requested


def test_a_publisher_with_no_api_is_checkable_from_its_declared_markup() -> None:
    """A posting page stating JobPosting metadata is checkable without a board API."""
    page = _page_declaring(_declared_block("Clinical Data Manager", "Acme Health", url=_ICIMS_URL))
    client = _Router([("icims.com", 200, page)])
    checked = check_posting(_ICIMS_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "readable"
    assert (checked.title, checked.employer) == ("Clinical Data Manager", "Acme Health")


@pytest.mark.parametrize("requested", [_ICIMS_URL, _ICIMS_SLUG_URL])
def test_a_declared_identifier_in_the_requested_path_identifies_the_posting(
    requested: str,
) -> None:
    """Publishers that state an identifier rather than a canonical url are read too."""
    page = _page_declaring(
        _declared_block("Clinical Data Manager", "Acme Health", identifier="12345")
    )
    client = _Router([("icims.com", 200, page)])
    checked = check_posting(requested, client=client)  # type: ignore[arg-type]
    assert checked.status == "readable"
    assert (checked.title, checked.employer) == ("Clinical Data Manager", "Acme Health")


def test_a_declared_identifier_inside_another_number_identifies_nothing() -> None:
    """A short identifier is a digit run in most paths, so it must be a whole token."""
    page = _page_declaring(_declared_block("Clinical Data Manager", "Acme Health", identifier="7"))
    client = _Router([("icims.com", 200, page)])
    checked = check_posting(_ICIMS_OTHER_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "unsupported"


_SLUG_ONLY_URL = "https://careers.acme.example/job/0f1e2d3c/clinical-data-manager-springfield/"
# Some sites state a site hash as the identifier, which no request URL carries.
_SITE_HASH_IDENTIFIER = "5e6f7a8b" * 8


@pytest.mark.parametrize("identifier", ["", _SITE_HASH_IDENTIFIER])
def test_a_lone_block_is_identified_by_its_title_in_the_path(identifier: str) -> None:
    """An identifier the URL never carries says nothing about which posting this is."""
    page = _page_declaring(
        _declared_block("Clinical Data Manager", "Acme Health", identifier=identifier)
    )
    client = _Router([("careers.acme.example", 200, page)])
    checked = check_posting(_SLUG_ONLY_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "readable"
    assert (checked.title, checked.employer) == ("Clinical Data Manager", "Acme Health")


@pytest.mark.parametrize("title", ["Staff Platform Engineer", "Manager"])
def test_a_lone_block_whose_title_the_path_does_not_spell_identifies_nothing(title: str) -> None:
    page = _page_declaring(_declared_block(title, "Acme Health"))
    client = _Router([("careers.acme.example", 200, page)])
    checked = check_posting(_SLUG_ONLY_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "unsupported"


@pytest.mark.parametrize("identifier", ["", _SITE_HASH_IDENTIFIER])
@pytest.mark.parametrize(
    ("title", "requested"),
    [
        ("Clinical Data", "https://careers.acme.example/careers/clinical-data/"),
        ("Data Manager", "https://careers.acme.example/departments/data/manager"),
        ("Engineering Jobs", "https://careers.acme.example/engineering/jobs"),
        ("Registered Nurse", "https://careers.acme.example/careers/registered-nurse-jobs"),
        (
            "Registered Nurse",
            "https://careers.acme.example/careers/registered-nurse-jobs-in-ogdenville",
        ),
        ("Engineering Manager", "https://careers.acme.example/jobs/engineering-manager/page/2"),
        ("Registered Nurse", "https://careers.acme.example/careers/2024/registered-nurse-jobs"),
        (
            "Registered Nurse",
            "https://careers.acme.example/search/results/1187/registered-nurse-jobs",
        ),
    ],
)
def test_a_category_page_spelling_a_lone_blocks_title_identifies_nothing(
    title: str, requested: str, identifier: str
) -> None:
    """A category page carries its subject in the path and names no single posting."""
    page = _page_declaring(_declared_block(title, "Acme Health", identifier=identifier))
    client = _Router([("careers.acme.example", 200, page)])
    checked = check_posting(requested, client=client)  # type: ignore[arg-type]
    assert checked.status == "unsupported"


def test_a_lone_block_on_a_page_the_read_was_redirected_to_identifies_nothing() -> None:
    """A withdrawn posting that lands on another page must not take that page's identity."""
    page = _page_declaring(_declared_block("Clinical Data Manager", "Acme Health"))
    client = _Router(
        [("careers.acme.example", 200, page)],
        redirected_to="https://careers.acme.example/job/9a8b7c6d/clinical-data-manager-shelbyville/",
    )
    checked = check_posting(_SLUG_ONLY_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "unsupported"


def test_a_featured_block_on_a_page_the_read_was_redirected_to_identifies_nothing() -> None:
    """Only the landed address separates a landing page from the posting it features."""
    page = _page_declaring(
        _declared_block("Registered Nurse", "Acme Health", identifier=_SITE_HASH_IDENTIFIER)
    )
    client = _Router(
        [("careers.acme.example", 200, page)],
        redirected_to="https://careers.acme.example/jobs/registered-nurse-shelbyville-9137",
    )
    requested = "https://careers.acme.example/jobs/registered-nurse-springfield-4821"
    checked = check_posting(requested, client=client)  # type: ignore[arg-type]
    assert checked.status == "unsupported"


def test_a_lone_blocks_title_matches_a_slug_that_drops_its_small_words() -> None:
    page = _page_declaring(_declared_block("Manager of Clinical Data", "Acme Health"))
    client = _Router([("careers.acme.example", 200, page)])
    requested = "https://careers.acme.example/job/0f1e2d3c/manager-clinical-data-springfield/"
    checked = check_posting(requested, client=client)  # type: ignore[arg-type]
    assert checked.status == "readable"


def test_a_posting_page_declaring_nothing_stays_unsupported() -> None:
    """With no API and no publisher markup there is still nothing free to check."""
    body = "<html><body><main><p>Careers at Acme Health.</p></main></body></html>"
    client = _Router([("icims.com", 200, body)])
    checked = check_posting(_ICIMS_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "unsupported"


def test_a_page_listing_several_postings_vouches_for_none_of_them() -> None:
    """An index declares other employers' jobs with the same publisher authority."""
    page = _page_declaring(
        _declared_block("Clinical Data Manager", "Acme Health", url=_ICIMS_URL),
        _declared_block("Staff Data Engineer", "Globex Health", url=_ICIMS_OTHER_URL),
    )
    client = _Router([("icims.com", 200, page)])
    checked = check_posting(_ICIMS_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "unsupported"


def test_a_block_naming_another_posting_leaves_the_api_answer_standing() -> None:
    """One block is trusted only when it names the posting that was requested."""
    page = _page_declaring(
        _declared_block("Staff Data Engineer", "Globex Health", url=_ICIMS_OTHER_URL)
    )
    client = _Router(
        [("jobs.smartrecruiters.com", 200, page)],
        denied=("api.smartrecruiters.com",),
    )
    checked = check_posting(_SMARTRECRUITERS_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "unreadable"
    assert checked.employer == ""


def test_a_posting_that_redirects_to_an_index_is_not_read_as_that_posting() -> None:
    """An index reached by redirect declares some other open job, not the requested one."""
    page = _page_declaring(_declared_block("Clinical Data Manager", "Acme Health", url=_ICIMS_URL))
    client = _Router([("icims.com", 200, page)], redirected_to=_ICIMS_INDEX)
    checked = check_posting(_ICIMS_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "unsupported"


_SITE_POSTING_PATH = "/job/3f2a9c1e-7b4d-4e8a-9c2f-1a2b3c4d5e6f/clinical-data-manager/"
_SITE_POSTING_URL = f"https://careers.example.com{_SITE_POSTING_PATH}"
_SITE_LISTING_URL = "https://careers.example.com/job-search-results/"
_VIEW_POSTING_URL = "https://ca.linkedin.com/jobs/view/clinical-data-manager-4398765210"
_VIEW_SEARCH_URL = (
    "https://www.linkedin.com/jobs/search?keywords=Clinical%20Data&trk=expired_jd_redirect"
)
_NUMBERED_POSTING_URL = "https://careers.example.com/jobs/48213/"
_ROLE_BLURB = "".join(
    f"<p>{sentence}</p>"
    for sentence in (
        "Acme Health hires across clinical, research, and operations teams in three states.",
        "Every role lists its schedule, its pay range, and the benefits that come with it.",
        "Applicants may save a search and receive new openings by message as they are posted.",
        "Accommodations are available at any stage of the hiring process on request.",
        "Most teams offer hybrid schedules, and several offer fully remote arrangements.",
    )
)


def _listing_page(*paths: str) -> str:
    """Build a same-site listing page linking several postings of one address shape."""
    links = "".join(
        f'<li><a href="{path}">{title}</a></li>'
        for path, title in zip(paths, ("Registered Nurse", "Pharmacy Technician"), strict=True)
    )
    return (
        f"<html><body><main><h1>Open roles</h1><ul>{links}</ul>{_ROLE_BLURB}</main></body></html>"
    )


_SITE_LISTING = _listing_page("/job/registered-nurse/", "/job/pharmacy-technician/")
_VIEW_SEARCH = _listing_page(
    "/jobs/view/registered-nurse-4466001111", "/jobs/view/pharmacy-technician-4466002222"
)


@pytest.mark.parametrize(
    ("requested", "landed", "page"),
    [
        pytest.param(_SITE_POSTING_URL, _SITE_LISTING_URL, _SITE_LISTING, id="same-host"),
        pytest.param(_VIEW_POSTING_URL, _VIEW_SEARCH_URL, _VIEW_SEARCH, id="country-host"),
        pytest.param(
            f"https://example.com{_SITE_POSTING_PATH}",
            "https://www.example.com/job-search-results/",
            _SITE_LISTING,
            id="www-host",
        ),
    ],
)
def test_a_posting_its_own_site_redirects_to_a_listing_reads_as_closed(
    requested: str, landed: str, page: str
) -> None:
    """A site that drops a posting's identifier on the way to its own listing has closed it."""
    client = _Router([("", 200, page)], redirected_to=landed)
    with pytest.raises(IngestError) as raised:
        from_url(requested, client=client)  # type: ignore[arg-type]
    assert closure_signal(str(raised.value))


_POSTING_PAGE = (
    "<html><body><main><h1>Clinical Data Manager</h1><h2>Responsibilities</h2>"
    f"{_ROLE_BLURB}</main></body></html>"
)
_SIGN_IN_PAGE = (
    "<html><body><main><h1>Sign in</h1><form><input name='email'></form>"
    f"{_ROLE_BLURB}</main></body></html>"
)
_POSTING_WITH_SIMILAR_JOBS = (
    "<html><body><main><h1>Senior Data Engineer</h1><h2>Responsibilities</h2>"
    f"{_ROLE_BLURB}<h2>Similar jobs</h2><ul>"
    '<li><a href="/jobs/registered-nurse/">Registered Nurse</a></li>'
    '<li><a href="/jobs/pharmacy-technician/">Pharmacy Technician</a></li>'
    "</ul></main></body></html>"
)


@pytest.mark.parametrize(
    ("requested", "landed", "page"),
    [
        pytest.param(
            _SITE_POSTING_URL,
            "https://careers.example.com/en-ca/job/3f2a9c1e-7b4d-4e8a-9c2f-1a2b3c4d5e6f/",
            _SITE_LISTING,
            id="identifier-kept",
        ),
        pytest.param(
            _VIEW_POSTING_URL,
            "https://www.linkedin.com/jobs/search?currentJobId=4398765210",
            _VIEW_SEARCH,
            id="identifier-kept-in-query",
        ),
        pytest.param(
            _NUMBERED_POSTING_URL,
            "https://careers.example.com/jobs#/detail/48213",
            _SITE_LISTING,
            id="identifier-kept-in-fragment",
        ),
        pytest.param(
            _NUMBERED_POSTING_URL,
            "https://careers.example.com/jobs/senior-data-engineer/",
            _POSTING_WITH_SIMILAR_JOBS,
            id="identifier-to-slug",
        ),
        pytest.param(
            _NUMBERED_POSTING_URL,
            "https://careers.example.com/jobs/search-relevance-engineer/",
            _POSTING_WITH_SIMILAR_JOBS,
            id="slug-with-search",
        ),
        pytest.param(
            _NUMBERED_POSTING_URL,
            "https://careers.example.com/jobs/clinical-results-analyst/",
            _POSTING_WITH_SIMILAR_JOBS,
            id="slug-with-results",
        ),
        pytest.param(
            _NUMBERED_POSTING_URL,
            "https://careers.example.com/jobs/registered-nurse-multiple-positions/",
            _POSTING_WITH_SIMILAR_JOBS,
            id="slug-ending-in-a-listing-word",
        ),
        pytest.param(
            _SITE_POSTING_URL,
            "https://elsewhere.example/job-search-results/",
            _SITE_LISTING,
            id="another-site",
        ),
        pytest.param(
            f"https://acme.co.uk{_SITE_POSTING_PATH}",
            "https://globex.co.uk/job-search-results/",
            _SITE_LISTING,
            id="another-site-under-one-suffix",
        ),
        pytest.param(
            f"https://acme.jobs.example{_SITE_POSTING_PATH}",
            "https://globex.jobs.example/job-search-results/",
            _SITE_LISTING,
            id="another-tenant",
        ),
        pytest.param(
            _SITE_POSTING_URL, "https://careers.example.com/login", _SIGN_IN_PAGE, id="login"
        ),
        pytest.param(
            _SITE_POSTING_URL,
            "https://careers.example.com/roles/current",
            _POSTING_PAGE,
            id="posting",
        ),
        pytest.param(
            "https://careers.example.com/careers/clinical-data-manager/",
            _SITE_LISTING_URL,
            _SITE_LISTING,
            id="no-identifier-requested",
        ),
    ],
)
def test_a_redirect_that_is_not_a_withdrawal_is_read_as_the_page_it_reached(
    requested: str, landed: str, page: str
) -> None:
    client = _Router([("", 200, page)], redirected_to=landed)
    result = from_url(requested, client=client)  # type: ignore[arg-type]
    assert result.origin == landed


_WORKABLE_URL = "https://apply.workable.com/globex/j/4A1B2C3D4E"
_RECRUITEE_URL = "https://globex.recruitee.com/o/clinical-data-manager"


@pytest.mark.parametrize(
    ("requested", "landed"),
    [
        pytest.param(_WORKABLE_URL, "https://www.workable.com/", id="workable"),
        # A Recruitee tenant that no longer exists lands on the vendor home.
        pytest.param(_RECRUITEE_URL, "https://recruitee.com/", id="recruitee"),
        pytest.param(_RECRUITEE_URL, "https://globex.recruitee.com/", id="recruitee-tenant-home"),
        pytest.param(
            _WORKABLE_URL, "https://apply.workable.com/globex/", id="workable-tenant-home"
        ),
        pytest.param(
            "https://apply.workable.com/globex24/j/4A1B2C3D4E",
            "https://apply.workable.com/globex24/",
            id="workable-tenant-named-with-digits",
        ),
    ],
)
def test_an_ats_posting_redirected_to_the_vendor_home_reads_as_closed(
    requested: str, landed: str
) -> None:
    client = _Router([("", 200, _POSTING_PAGE)], redirected_to=landed)
    with pytest.raises(IngestError) as raised:
        from_url(requested, client=client)  # type: ignore[arg-type]
    assert closure_signal(str(raised.value))


@pytest.mark.parametrize("requested", [_WORKABLE_URL, _RECRUITEE_URL])
def test_an_ats_posting_that_stays_on_its_address_reads_normally(requested: str) -> None:
    client = _Router([("", 200, _POSTING_PAGE)])
    result = from_url(requested, client=client)  # type: ignore[arg-type]
    assert result.origin == requested
    assert "Clinical Data Manager" in result.text


@pytest.mark.parametrize(
    ("requested", "landed"),
    [
        pytest.param(
            _RECRUITEE_URL,
            "https://careers.globex.example/o/clinical-data-manager",
            id="recruitee-custom-domain",
        ),
        pytest.param(
            _RECRUITEE_URL,
            "https://globex.recruitee.com/l/en/o/clinical-data-manager",
            id="recruitee-locale",
        ),
        pytest.param(
            _WORKABLE_URL,
            "https://careers.globex.example/jobs/4A1B2C3D4E",
            id="workable-custom-domain",
        ),
        pytest.param(
            "https://careers-globex.icims.com/jobs/30001/job",
            "https://careers.globex.example/openings?job=30001",
            id="icims-id-in-query",
        ),
    ],
)
def test_an_ats_posting_redirected_to_an_address_keeping_its_identifier_reads_normally(
    requested: str, landed: str
) -> None:
    client = _Router([("", 200, _POSTING_PAGE)], redirected_to=landed)
    result = from_url(requested, client=client)  # type: ignore[arg-type]
    assert result.origin == landed
    assert "Clinical Data Manager" in result.text


_BARE_POSTING_URL = "https://www.jobs.example/jobs/view/4398765210/"
_CANONICAL_POSTING_URL = (
    "https://www.jobs.example/jobs/view/clinical-data-manager-at-acme-health-4398765210"
)


def _page_naming_its_canonical(canonical: str) -> str:
    """Build a bare posting address's page, which declares only where the posting lives."""
    return (
        f'<html><head><link rel="canonical" href="{canonical}">'
        f'<meta property="og:url" content="{canonical}"></head>'
        "<body><main><h1>More roles</h1></main></body></html>"
    )


def test_a_bare_posting_address_is_checked_through_its_declared_canonical_page() -> None:
    canonical = _page_declaring(
        _declared_block("Clinical Data Manager", "Acme Health", url=_CANONICAL_POSTING_URL)
    )
    client = _Router(
        [
            (_CANONICAL_POSTING_URL, 200, canonical),
            ("www.jobs.example", 200, _page_naming_its_canonical(_CANONICAL_POSTING_URL)),
        ]
    )
    checked = check_posting(_BARE_POSTING_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "readable"
    assert (checked.title, checked.employer) == ("Clinical Data Manager", "Acme Health")


@pytest.mark.parametrize(
    "declared_canonical",
    [
        "https://www.jobs.example/jobs/view/clinical-data-manager-at-acme-health-4466005677",
        "https://elsewhere.example/jobs/view/clinical-data-manager-at-acme-health-4398765210",
        "https://www.jobs.example/jobs/view/clinical-data-manager-at-acme-health",
    ],
)
def test_a_canonical_page_naming_another_posting_or_host_is_not_checked(
    declared_canonical: str,
) -> None:
    canonical = _page_declaring(
        _declared_block("Clinical Data Manager", "Acme Health", url=declared_canonical)
    )
    client = _Router(
        [
            (declared_canonical, 200, canonical),
            ("www.jobs.example", 200, _page_naming_its_canonical(declared_canonical)),
        ]
    )
    checked = check_posting(_BARE_POSTING_URL, client=client)  # type: ignore[arg-type]
    assert checked.status == "unsupported"
    assert declared_canonical not in client.requested
