"""Workday detail enrichment stays publisher-grounded and request-bounded."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from typing import Any, Literal

import httpx
import pytest
from sqlalchemy import select

from tailorcv.application.discovery.board_scan import SourceScanService
from tailorcv.application.discovery.scan_sweep import BoardSweep
from tailorcv.application.discovery.screening import (
    DiscoveryScreening,
    ScreeningCode,
    ScreeningDimension,
    ScreeningFinding,
    ScreeningStanding,
    screen_discovery,
)
from tailorcv.application.discovery.source_continuation import SourceContinuationHandler
from tailorcv.application.discovery.source_reading import (
    PostingCheck,
    SourceContinuationTarget,
    SourceDetailSummary,
    SourceListing,
    SourceRead,
    SourceReadOptions,
)
from tailorcv.application.workspaces import SearchParametersService
from tailorcv.bootstrap.worker import Worker
from tailorcv.domain import SourceReadOutcome
from tailorcv.domain.artifacts import PostingSource
from tailorcv.domain.search_parameters import (
    Acceptance,
    Arrangement,
    Cluster,
    CountryNames,
    Focus,
    JobSearchParameters,
    Location,
    Seniority,
)
from tailorcv.domain.staged_facts import StagedListingFacts
from tailorcv.infrastructure.artifacts.store import file_uri_path
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import Artifact, SourceScan
from tailorcv.infrastructure.web_fetch.ingest.url import (
    check_posting,
    from_url,
    guarded_posting_reads,
    workday_posting_detail,
)
from tailorcv.infrastructure.web_fetch.listing_facts import published_listing_facts
from tailorcv.shared.errors import IngestError
from tests.support.artifacts import runtime_storage
from tests.support.doubles import ScriptedBoards, UnexpectedPostingFetcher
from tests.support.http import policy_client
from tests.support.scenarios import board_source, scan_workspace, state_target_titles, titles


class _DetailClient:
    def __init__(self, payload: dict[str, Any] | None, *, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.requested: list[str] = []

    def get(self, url: str, **_kwargs: object) -> httpx.Response:
        self.requested.append(url)
        request = httpx.Request("GET", url)
        if self.payload is None:
            return httpx.Response(self.status_code, request=request)
        return httpx.Response(self.status_code, request=request, json=self.payload)


def _url(index: int = 1) -> str:
    return (
        "https://example.wd1.myworkdayjobs.com/en-US/Careers/job/"
        f"Canada/Clinical-Data-Management_R-{index}"
    )


def _wanting(country: str) -> JobSearchParameters:
    return JobSearchParameters(
        locations=[Location(key="home", country=country)],
        seniority=Seniority(target_titles=titles("Data Product Management")),
    )


def _location_finding(screening: DiscoveryScreening) -> ScreeningFinding:
    return next(
        finding
        for finding in screening.findings
        if finding.dimension is ScreeningDimension.LOCATION
    )


def test_workday_detail_preserves_all_locations_and_explicit_role_aliases() -> None:
    payload = {
        "jobPostingInfo": {
            "title": "Clinical Data Management",
            "location": "Toronto, Ontario",
            "secondaryLocations": [
                "Vancouver, British Columbia",
                {"location": "Remote, Canada"},
            ],
            "additionalLocations": [{"name": "Montreal, Quebec"}],
            "timeType": "Full time",
            "jobDescription": (
                "The Study Operations Lead (SOL) coordinates delivery. "
                "An Associate Study Operations Lead (ASOL) supports the work. "
                "Records live in the Electronic Health Record (EHR)."
            ),
        }
    }
    client = _DetailClient(payload)
    url = "https://example.wd1.myworkdayjobs.com/en-US/Careers/job/Canada/SOL-ASOL_R-1"

    checked = workday_posting_detail(url, client=client)  # type: ignore[arg-type]

    assert checked.status == "readable"
    assert checked.platform == "workday"
    assert checked.title == "Clinical Data Management"
    assert checked.location == "Toronto, Ontario"
    assert checked.facts.locations.values == (
        "Toronto, Ontario",
        "Vancouver, British Columbia",
        "Remote, Canada",
        "Montreal, Quebec",
    )
    assert checked.facts.locations.complete is True
    assert checked.facts.role_aliases.values == (
        "Study Operations Lead",
        "Associate Study Operations Lead",
    )
    assert checked.facts.role_aliases.provenance == "workday.job_description"
    assert checked.facts.role_aliases.complete is True
    assert checked.facts.as_json()["schema_version"] == 5
    assert client.requested == [
        "https://example.wd1.myworkdayjobs.com/wday/cxs/example/Careers/job/Canada/SOL-ASOL_R-1"
    ]


@pytest.mark.parametrize(
    "action", ["/apply", "/apply/", "/apply/applyManually", "/apply/autofillWithResume"]
)
def test_a_workday_apply_address_reads_its_posting_detail(action: str) -> None:
    client = _DetailClient({"jobPostingInfo": {"title": "Clinical Data Management"}})

    workday_posting_detail(f"{_url()}{action}", client=client)  # type: ignore[arg-type]

    assert client.requested == [
        "https://example.wd1.myworkdayjobs.com/wday/cxs/example/Careers/job/"
        "Canada/Clinical-Data-Management_R-1"
    ]


@pytest.mark.parametrize("guard_mode", ["explicit", "inherited", "composed"])
@pytest.mark.parametrize("shared_host", [False, True])
@pytest.mark.parametrize("locale", ["", "en-US/", "ja-JP/"])
@pytest.mark.parametrize("action", ["", "/apply", "/apply/autofillWithResume"])
@pytest.mark.parametrize("location", ["Remote", "Canada"])
def test_a_protected_workday_logical_posting_is_not_requested(
    guard_mode: str, shared_host: bool, locale: str, action: str, location: str
) -> None:
    host = "wd1.myworkdaysite.com" if shared_host else "acme.wd1.myworkdayjobs.com"
    scope = "recruiting/acme/Careers" if shared_host else "Careers"
    protected = f"https://{host}/en-US/{scope}/job/Remote/Data-Manager_R-1"
    url = f"https://{host}/{locale}{scope}/job/{location}/Data-Manager_R-1{action}"
    options = SourceReadOptions(protected_posting_urls=frozenset({protected}))
    requested: list[str] = []

    def serve(address: str) -> httpx.Response:
        requested.append(address)
        request = httpx.Request("GET", address)
        if "/job/" not in address:
            return httpx.Response(
                200,
                request=request,
                text='<script>window.workday = {tenant: "acme", siteId: "Careers"};</script>',
            )
        return httpx.Response(
            200, request=request, json={"jobPostingInfo": {"title": "Data Manager"}}
        )

    inherited = options.allows_posting if guard_mode != "explicit" else None
    explicit: Callable[[str], bool] | None = (
        options.allows_posting if guard_mode == "explicit" else None
    )
    if guard_mode == "composed":

        def permits(_address: str) -> bool:
            return True

        explicit = permits
    with guarded_posting_reads(inherited), guarded_posting_reads(None):
        checked = workday_posting_detail(url, client=policy_client(serve), request_guard=explicit)

    assert requested == []
    assert checked.status == "unreadable"
    assert checked.platform == "workday"
    assert checked.source is None
    assert checked.http_status is None
    assert checked.robots_basis is None
    assert checked.facts == StagedListingFacts()


@pytest.mark.parametrize("reader", ["detail", "check", "text"])
def test_a_protected_workday_posting_cannot_be_read_through_another_entry_point(
    reader: str,
) -> None:
    url = "https://acme.wd1.myworkdayjobs.com/en-US/Careers/job/Remote/Data-Manager_R-1"
    options = SourceReadOptions(protected_posting_urls=frozenset({url}))
    client = _DetailClient({"jobPostingInfo": {"title": "Data Manager"}})
    http = policy_client(client.get)

    with guarded_posting_reads(options.allows_posting):
        if reader == "text":
            with pytest.raises(IngestError):
                from_url(url, client=http)
        else:
            checked = (
                workday_posting_detail(url, client=http)
                if reader == "detail"
                else check_posting(url, client=http)
            )
            assert checked.status == "unreadable"
            assert checked.source is None
    assert client.requested == []


@pytest.mark.parametrize(("tenant", "site"), [("initech", "Careers"), ("acme", "OtherCareers")])
def test_workday_logical_protection_keeps_other_tenants_and_sites_readable(
    tenant: str, site: str
) -> None:
    host = "https://wd1.myworkdaysite.com"
    root = f"{host}/en-US/recruiting/{tenant}/{site}"
    protected = f"{host}/en-US/recruiting/acme/Careers/job/Remote/Data-Manager_R-1"
    endpoint = f"{host}/wday/cxs/{tenant}/{site}/job/Remote/Data-Manager_R-1"
    options = SourceReadOptions(protected_posting_urls=frozenset({protected}))
    requested: list[str] = []

    def serve(address: str) -> httpx.Response:
        requested.append(address)
        request = httpx.Request("GET", address)
        if address == root:
            return httpx.Response(
                200,
                request=request,
                text=f'<script>window.workday = {{tenant: "{tenant}", siteId: "{site}"}};</script>',
            )
        assert address == endpoint
        return httpx.Response(
            200, request=request, json={"jobPostingInfo": {"title": "Data Manager"}}
        )

    checked = workday_posting_detail(
        f"{root}/job/Remote/Data-Manager_R-1",
        client=policy_client(serve),
        request_guard=options.allows_posting,
    )

    assert checked.status == "readable"
    assert requested == [root, endpoint]


@pytest.mark.parametrize("redirect", [False, True])
def test_a_permitted_workday_logical_posting_still_checks_the_destination(redirect: bool) -> None:
    url = "https://acme.wd1.myworkdayjobs.com/en-US/Careers/job/Remote/Data-Manager_R-1"
    endpoint = (
        "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/Careers/job/Remote/Data-Manager_R-1"
    )
    target = "https://acme.wd1.myworkdayjobs.com/en-US/Careers/job/Remote/Data-Manager_R-2"
    denied = target if redirect else endpoint
    requested: list[str] = []

    def serve(address: str) -> httpx.Response:
        requested.append(address)
        assert address == endpoint
        return httpx.Response(
            302, request=httpx.Request("GET", address), headers={"Location": target}
        )

    checked = workday_posting_detail(
        url, client=policy_client(serve), request_guard=lambda address: address != denied
    )

    assert checked.status == "unreadable"
    assert checked.source is None
    assert requested == ([endpoint] if redirect else [])


@pytest.mark.parametrize("exit_kind", ["success", "refusal", "exception"])
def test_workday_guard_context_is_restored_after_each_exit(exit_kind: str) -> None:
    root = "https://acme.wd1.myworkdayjobs.com/en-US/Careers/job/Remote/Data-Manager"
    first, second = f"{root}_R-1", f"{root}_R-2"
    outer = SourceReadOptions(protected_posting_urls=frozenset({second}))
    client = _DetailClient({"jobPostingInfo": {"title": "Data Manager"}})
    http = policy_client(client.get)

    def inner(address: str) -> bool:
        if exit_kind == "exception":
            raise RuntimeError("Synthetic guard failure")
        return exit_kind == "success" or address != first

    with guarded_posting_reads(outer.allows_posting):
        if exit_kind == "exception":
            with pytest.raises(RuntimeError, match="Synthetic guard failure"):
                workday_posting_detail(first, client=http, request_guard=inner)
        else:
            checked = workday_posting_detail(first, client=http, request_guard=inner)
            assert checked.status == ("readable" if exit_kind == "success" else "unreadable")
        assert workday_posting_detail(first, client=http).status == "readable"
        client.requested.clear()
        assert workday_posting_detail(second, client=http).status == "unreadable"
        assert client.requested == []
    assert workday_posting_detail(second, client=http).status == "readable"


@pytest.mark.parametrize("locale", ["", "en-US/", "ja-JP/"])
@pytest.mark.parametrize("action", ["", "/apply", "/apply/autofillWithResume"])
def test_a_workday_shared_host_detail_uses_the_root_tenant_and_preserves_the_requisition(
    locale: str, action: str
) -> None:
    root = f"https://wd1.myworkdaysite.com/{locale}recruiting/example/External_Careers"
    endpoint = "https://wd1.myworkdaysite.com/wday/cxs/example/External_Careers/job/Remote/Data-Manager_R-1"
    requested: list[str] = []

    def serve(url: str) -> httpx.Response:
        requested.append(url)
        request = httpx.Request("GET", url)
        if url == root:
            return httpx.Response(
                200,
                request=request,
                text=(
                    '<script>window.workday = {tenant: "example", '
                    'siteId: "External_Careers"};</script>'
                ),
            )
        assert url == endpoint
        return httpx.Response(
            200,
            request=request,
            json={
                "jobPostingInfo": {
                    "title": "Data Manager",
                    "jobReqId": "R-1",
                    "jobPostingSiteId": "External_Careers",
                    "jobDescription": "<p>This role is fully remote.</p>",
                }
            },
        )

    checked = workday_posting_detail(
        f"{root}/job/Remote/Data-Manager_R-1{action}", client=policy_client(serve)
    )

    assert checked.status == "readable"
    assert checked.title == "Data Manager"
    assert checked.source is not None
    assert json.loads(checked.source.record)["jobPostingInfo"]["jobReqId"] == "R-1"
    assert checked.facts.arrangements.values == ("remote",)
    assert requested == [root, endpoint]


@pytest.mark.parametrize(
    ("tenant", "site"), [("other", "External_Careers"), ("example", "Other_Careers")]
)
def test_a_workday_shared_host_detail_refuses_a_root_declaring_another_scope(
    tenant: str, site: str
) -> None:
    root = "https://wd1.myworkdaysite.com/en-US/recruiting/example/External_Careers"
    requested: list[str] = []

    def serve(url: str) -> httpx.Response:
        requested.append(url)
        assert url == root
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            text=f'<script>window.workday = {{tenant: "{tenant}", siteId: "{site}"}};</script>',
        )

    checked = workday_posting_detail(
        f"{root}/job/Remote/Data-Manager_R-1", client=policy_client(serve)
    )

    assert checked.status == "unreadable"
    assert checked.platform == "workday"
    assert requested == [root]


def test_a_workday_address_inside_a_redirect_is_not_read_as_workday() -> None:
    client = _DetailClient({"jobPostingInfo": {"title": "Clinical Data Management"}})

    checked = workday_posting_detail(
        f"https://click.example.net/r?u={_url()}",
        client=client,  # type: ignore[arg-type]
    )

    assert checked.status == "unsupported"
    assert client.requested == []


def test_a_collaborator_role_named_in_the_body_does_not_refuse_the_posting() -> None:
    payload = {
        "jobPostingInfo": {
            "title": "Data Product Management",
            "location": "Remote, Canada",
            "jobDescription": "You work closely with the Project Manager (PM) on delivery.",
        }
    }
    checked = workday_posting_detail(
        _url(),
        client=_DetailClient(payload),  # type: ignore[arg-type]
    )
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="Canada")],
        seniority=Seniority(
            target_titles=titles("Data Product Management"), excluded_titles=["Project Manager"]
        ),
    )

    screening = screen_discovery(
        "Globex",
        checked.title,
        checked.location,
        parameters,
        ["Data Product Management"],
        checked.facts,
    )

    assert checked.facts.role_aliases.values == ()
    assert screening.standing is not ScreeningStanding.REFUSE


def test_workday_detail_states_the_country_only_its_structured_fields_name() -> None:
    payload = {
        "hiringOrganization": {"name": "Globex"},
        "jobPostingInfo": {
            "title": "Data Product Management",
            "location": "IN - Northtown",
            "additionalLocations": ["Southtown, Eastern Region"],
            "country": {"descriptor": "India", "alpha2Code": "IN"},
            "jobDescription": "Join the product data team.",
        },
    }

    checked = workday_posting_detail(
        _url(),
        client=_DetailClient(payload),  # type: ignore[arg-type]
    )

    # Workday names the legal entity, not the employer the board lists.
    assert checked.employer == ""
    assert checked.facts.locations.values == (
        "IN - Northtown",
        "Southtown, Eastern Region",
        "India",
    )


def test_workday_detail_reads_the_requisition_country_code_when_the_posting_names_none() -> None:
    payload = {
        "jobRequisitionLocation": {"country": {"alpha2Code": "CA"}},
        "jobPostingInfo": {
            "title": "Data Product Management",
            "location": "Northtown, Western Region",
            "jobDescription": "Join the product data team.",
        },
    }

    checked = workday_posting_detail(
        _url(),
        client=_DetailClient(payload),  # type: ignore[arg-type]
    )

    assert checked.facts.locations.values == ("Northtown, Western Region", "Canada")


def test_workday_detail_prefers_the_country_code_over_a_tenants_localized_country_name() -> None:
    payload = {
        "jobRequisitionLocation": {"country": {"alpha2Code": "CA"}},
        "jobPostingInfo": {
            "title": "Data Product Management",
            "location": "Northtown, Western Region",
            "country": {"descriptor": "Kanada"},
            "jobDescription": "Join the product data team.",
        },
    }

    checked = workday_posting_detail(
        _url(),
        client=_DetailClient(payload),  # type: ignore[arg-type]
    )

    assert checked.facts.locations.values == ("Northtown, Western Region", "Canada")


def test_a_workday_posting_stating_only_an_unwanted_country_is_refused() -> None:
    payload = {
        "jobPostingInfo": {
            "title": "Data Product Management",
            "location": "IN - Northtown",
            "additionalLocations": ["Southtown, Eastern Region"],
            "country": {"descriptor": "India"},
            "jobDescription": "Join the product data team.",
        }
    }
    checked = workday_posting_detail(
        _url(),
        client=_DetailClient(payload),  # type: ignore[arg-type]
    )

    screening = screen_discovery(
        "Globex",
        checked.title,
        checked.location,
        _wanting("Canada"),
        ["Data Product Management"],
        checked.facts,
    )

    assert _location_finding(screening).code is ScreeningCode.LOCATION_OUTSIDE
    assert _location_finding(screening).subject == "India"


def test_a_workday_posting_whose_second_market_is_a_wanted_country_is_not_refused() -> None:
    payload = {
        "jobPostingInfo": {
            "title": "Data Product Management",
            "location": "IN - Northtown",
            "additionalLocations": ["Southtown, Canada"],
            "country": {"descriptor": "India"},
            "jobDescription": "Join the product data team.",
        }
    }
    checked = workday_posting_detail(
        _url(),
        client=_DetailClient(payload),  # type: ignore[arg-type]
    )

    screening = screen_discovery(
        "Globex",
        checked.title,
        checked.location,
        _wanting("Canada"),
        ["Data Product Management"],
        checked.facts,
    )

    assert checked.facts.locations.values == (
        "IN - Northtown",
        "Southtown, Canada",
        "India",
    )
    assert screening.standing is not ScreeningStanding.REFUSE
    assert _location_finding(screening).code is ScreeningCode.LOCATION_MATCH


def test_manual_posting_check_reads_workday_detail_and_arrangement() -> None:
    payload = {
        "jobPostingInfo": {
            "title": "Data Product Management",
            "location": "Toronto, Canada",
            "jobDescription": "Join the product data team. #LI-Hybrid",
        }
    }

    checked = check_posting(
        _url(),
        client=_DetailClient(payload),  # type: ignore[arg-type]
    )

    assert checked.status == "readable"
    assert checked.platform == "workday"
    assert checked.facts.arrangements.values == ("hybrid",)


def test_workday_detail_rejects_incidental_or_ambiguous_acronyms() -> None:
    payload = {
        "jobPostingInfo": {
            "title": "Clinical Data Management",
            "location": "Remote, Canada",
            "jobDescription": (
                "The PM reviews TL metrics. DTL experience is helpful. "
                "A data team lead may coordinate contributors."
            ),
        }
    }

    checked = workday_posting_detail(
        _url(),
        client=_DetailClient(payload),  # type: ignore[arg-type]
    )

    assert checked.status == "readable"
    assert checked.facts.role_aliases.values == ()
    assert checked.facts.role_aliases.complete is True


def test_missing_workday_detail_is_an_explicit_unreadable_result() -> None:
    checked = workday_posting_detail(
        _url(),
        client=_DetailClient(None, status_code=404),  # type: ignore[arg-type]
    )

    assert checked.status == "unreadable"
    assert checked.platform == "workday"
    assert "HTTP 404" in checked.note
    assert checked.facts == StagedListingFacts()


_TENANT_HOST = "https://vhr-acme.wd1.myworkdayjobs.com"
_TENANT_SITE = "AcmeCareers"
_WRONG_TENANT = (422, {"errorCode": "HTTP_422"})


def _tenant_path(index: int = 7) -> str:
    return f"{_TENANT_SITE}/job/Remote/Data-Product-Manager_R-{index}"


def _tenant_site(
    requested: list[str],
    *,
    stated: str,
    answering: str = "acme",
    detail_status: int = 200,
    refusal: tuple[int, dict[str, Any]] = _WRONG_TENANT,
    page_failures: int = 0,
    page_missing: bool = False,
) -> Any:
    """Serve a Workday site whose CXS API answers only under the ``answering`` tenant.

    The career page answers 503 to its first ``page_failures`` requests, and 404 to
    every request when ``page_missing``.
    """

    def serve(url: str) -> httpx.Response:
        requested.append(url)
        request = httpx.Request("GET", url)
        if url == f"{_TENANT_HOST}/{_TENANT_SITE}":
            if requested.count(url) <= page_failures:
                return httpx.Response(503, request=request)
            if page_missing:
                return httpx.Response(404, request=request)
            page = f'<script>window.workday = {{\n  tenant: "{stated}",\n  siteId: "x"\n}};'
            return httpx.Response(200, request=request, text=page + "</script>")
        if url.startswith(f"{_TENANT_HOST}/wday/cxs/{answering}/{_TENANT_SITE}/job/"):
            if detail_status != 200:
                return httpx.Response(detail_status, request=request)
            payload = {
                "jobPostingInfo": {
                    "title": "Data Product Manager",
                    "location": "Remote",
                    "jobDescription": "Own the data platform remotely in Canada.",
                }
            }
            return httpx.Response(200, request=request, json=payload)
        status, body = refusal
        return httpx.Response(status, request=request, json=body)

    return serve


def _tenant_listings(count: int, title: str = "Data Product Manager") -> list[SourceListing]:
    return [
        SourceListing(
            employer="Acme",
            title=title,
            url=f"{_TENANT_HOST}/en-US/{_tenant_path(index)}",
            facts=published_listing_facts("workday", locations="Remote"),
        )
        for index in range(1, count + 1)
    ]


def _detail_sweep(client: Any) -> BoardSweep:
    sweep = BoardSweep.__new__(BoardSweep)
    sweep.posting_checker = partial(workday_posting_detail, client=client)
    return sweep


def _enrich_titles(
    sweep: BoardSweep,
    listings: list[SourceListing],
    parameters: JobSearchParameters | None = None,
    targets: list[str] | None = None,
) -> tuple[list[SourceListing], SourceDetailSummary]:
    read = sweep._enrich_workday_candidates(
        SourceRead(
            listings=listings,
            complete=True,
            raw_count=len(listings),
            outcome=SourceReadOutcome.ATS_COMPLETE,
        ),
        parameters
        or JobSearchParameters(seniority=Seniority(target_titles=titles("Data Product Manager"))),
        targets or ["Data Product Manager"],
        source_url=listings[0].url.partition("/job/")[0],
    )
    return read.listings, read.detail


def test_workday_enrichment_inherits_protection_for_a_logical_posting_variant() -> None:
    requested: list[str] = []
    client = policy_client(_tenant_site(requested, stated="acme"))
    listing = _tenant_listings(1)[0]
    protected = listing.url.replace("/en-US/", "/ja-JP/").replace("/Remote/", "/Canada/") + "/apply"
    options = SourceReadOptions(protected_posting_urls=frozenset({protected}))

    with guarded_posting_reads(options.allows_posting), guarded_posting_reads(None):
        enriched, detail = _enrich_titles(_detail_sweep(client), [listing])

    assert requested == []
    assert enriched[0].published_description == ""
    assert enriched[0].source is None
    assert (detail.attempted, detail.read, detail.failed) == (0, 0, 0)


def test_a_guard_protected_workday_survivor_is_reported_protected_not_failed() -> None:
    requested: list[str] = []
    client = policy_client(_tenant_site(requested, stated="acme"))
    readable, protected = _tenant_listings(2)
    options = SourceReadOptions(protected_posting_urls=frozenset({protected.url}))

    with guarded_posting_reads(options.allows_posting):
        read = _detail_sweep(client)._enrich_workday_candidates(
            SourceRead(
                listings=[readable, protected],
                complete=True,
                raw_count=2,
                outcome=SourceReadOutcome.ATS_COMPLETE,
            ),
            JobSearchParameters(seniority=Seniority(target_titles=titles("Data Product Manager"))),
            ["Data Product Manager"],
            source_url=readable.url.partition("/job/")[0],
        )

    assert [url for url in requested if url.endswith("_R-2")] == []
    assert any(url.endswith("_R-1") for url in requested)
    assert read.complete
    assert read.deferred_targets == ()
    assert read.protected_posting_urls == (protected.url,)
    assert read.skip_reasons == {"protected_posting": 1}
    assert (read.detail.attempted, read.detail.read, read.detail.failed) == (1, 1, 0)
    assert read.detail.failure_reasons == ()
    assert [item.url for item in read.listings] == [readable.url, protected.url]


def test_workday_details_halted_before_get_retain_exact_unattempted_identities() -> None:
    root = "https://globex.wd1.myworkdayjobs.com/en-US/Careers"
    listings = [
        SourceListing(
            employer="Globex",
            title="Data Product Manager",
            url=f"{root}/job/Remote/Data-Product-Manager_R-{index}",
            facts=published_listing_facts("workday", locations="2 Locations"),
        )
        for index in range(2)
    ]
    sweep = BoardSweep.__new__(BoardSweep)

    def unexpected(url: str) -> PostingCheck:
        raise AssertionError(f"Halted detail read reached GET: {url}")

    sweep.posting_checker = unexpected
    read = sweep._enrich_workday_candidates(
        SourceRead(
            listings=listings, complete=True, raw_count=2, outcome=SourceReadOutcome.ATS_COMPLETE
        ),
        JobSearchParameters(seniority=Seniority(target_titles=titles("Data Product Manager"))),
        ["Data Product Manager"],
        halted=lambda: True,
        source_url=root,
    )
    assert not read.complete
    assert read.detail.attempted == read.detail.read == read.detail.failed == 0
    assert read.detail.capped == 2
    assert {target.url for target in read.deferred_targets} == {item.url for item in listings}
    for target in read.deferred_targets:
        checkpoint = target.detail_checkpoint
        assert checkpoint is not None
        assert checkpoint.pending_ids == (target.url,)
        assert checkpoint.failed_attempts == ()


def test_workday_detail_reads_the_career_page_tenant_when_the_host_label_is_refused() -> None:
    requested: list[str] = []
    serve = _tenant_site(requested, stated="acme")

    checked = workday_posting_detail(
        f"{_TENANT_HOST}/en-US/{_tenant_path()}", client=policy_client(serve)
    )

    assert checked.status == "readable"
    assert checked.title == "Data Product Manager"
    assert requested == [
        f"{_TENANT_HOST}/wday/cxs/vhr-acme/{_tenant_path()}",
        f"{_TENANT_HOST}/{_TENANT_SITE}",
        f"{_TENANT_HOST}/wday/cxs/acme/{_tenant_path()}",
    ]


def test_a_refused_host_label_board_reads_its_career_page_once_per_sweep() -> None:
    requested: list[str] = []
    sweep = _detail_sweep(policy_client(_tenant_site(requested, stated="acme")))

    _enriched, detail = _enrich_titles(sweep, _tenant_listings(3))

    assert (detail.attempted, detail.read, detail.failed) == (3, 3, 0)
    assert requested == [
        f"{_TENANT_HOST}/wday/cxs/vhr-acme/{_tenant_path(1)}",
        f"{_TENANT_HOST}/{_TENANT_SITE}",
        *(f"{_TENANT_HOST}/wday/cxs/acme/{_tenant_path(index)}" for index in (1, 2, 3)),
    ]


def test_a_career_page_that_failed_is_read_again_for_the_next_posting() -> None:
    requested: list[str] = []
    serve = _tenant_site(requested, stated="acme", page_failures=2)
    sweep = _detail_sweep(policy_client(serve))

    _enriched, detail = _enrich_titles(sweep, _tenant_listings(3))

    assert (detail.attempted, detail.read, detail.failed) == (3, 2, 1)
    assert requested.count(f"{_TENANT_HOST}/{_TENANT_SITE}") == 3


def test_a_missing_career_page_is_read_once_per_sweep() -> None:
    requested: list[str] = []
    serve = _tenant_site(requested, stated="acme", page_missing=True)
    sweep = _detail_sweep(policy_client(serve))

    _enriched, detail = _enrich_titles(sweep, _tenant_listings(3))

    assert (detail.attempted, detail.read) == (3, 0)
    assert requested.count(f"{_TENANT_HOST}/{_TENANT_SITE}") == 1


def test_a_career_page_stating_the_host_label_tenant_is_read_once_per_sweep() -> None:
    requested: list[str] = []
    serve = _tenant_site(requested, stated="vhr-acme", answering="none")
    sweep = _detail_sweep(policy_client(serve))

    _enriched, detail = _enrich_titles(sweep, _tenant_listings(3))

    assert (detail.attempted, detail.read, detail.failed) == (3, 0, 3)
    assert requested == [
        f"{_TENANT_HOST}/wday/cxs/vhr-acme/{_tenant_path(1)}",
        f"{_TENANT_HOST}/{_TENANT_SITE}",
        f"{_TENANT_HOST}/wday/cxs/vhr-acme/{_tenant_path(2)}",
        f"{_TENANT_HOST}/wday/cxs/vhr-acme/{_tenant_path(3)}",
    ]


def test_a_robots_refused_career_page_is_never_requested() -> None:
    requested: list[str] = []
    robots = f"User-agent: *\nDisallow: /{_TENANT_SITE}\n"
    sweep = _detail_sweep(policy_client(_tenant_site(requested, stated="acme"), robots=robots))

    _enriched, detail = _enrich_titles(sweep, _tenant_listings(2))

    assert (detail.attempted, detail.read, detail.failed) == (2, 0, 2)
    assert requested == [
        f"{_TENANT_HOST}/wday/cxs/vhr-acme/{_tenant_path(index)}" for index in (1, 2)
    ]


def test_a_requisition_gone_under_the_stated_tenant_closes_on_a_liveness_check() -> None:
    requested: list[str] = []
    serve = _tenant_site(requested, stated="acme", detail_status=404)

    checked = check_posting(f"{_TENANT_HOST}/en-US/{_tenant_path()}", client=policy_client(serve))

    assert checked.status == "closed"
    assert "(HTTP 404)" in checked.note
    assert requested[-1] == f"{_TENANT_HOST}/wday/cxs/acme/{_tenant_path()}"


def test_a_requisition_gone_under_the_stated_tenant_stays_unreadable_in_enrichment() -> None:
    serve = _tenant_site([], stated="acme", detail_status=404)

    checked = workday_posting_detail(
        f"{_TENANT_HOST}/en-US/{_tenant_path()}", client=policy_client(serve)
    )

    assert checked.status == "unreadable"
    assert "HTTP 404" in checked.note


@pytest.mark.parametrize("status", [401, 403])
def test_a_host_label_refusal_with_a_workday_error_reads_the_stated_tenant(status: int) -> None:
    requested: list[str] = []
    refusal = (status, {"errorCode": "S22", "message": "permission denied"})
    serve = _tenant_site(requested, stated="acme", refusal=refusal)

    checked = check_posting(f"{_TENANT_HOST}/en-US/{_tenant_path()}", client=policy_client(serve))

    assert checked.status == "readable"
    assert checked.title == "Data Product Manager"
    assert requested[-1] == f"{_TENANT_HOST}/wday/cxs/acme/{_tenant_path()}"


def test_workday_enrichment_reads_only_title_survivors_up_to_the_cap() -> None:
    calls: list[str] = []

    def check(url: str) -> PostingCheck:
        calls.append(url)
        return PostingCheck(
            status="readable",
            source=PostingSource(
                publisher="workday", record="{}", description_paths=("description",)
            ),
            text="Own the data platform.",
            note="fixture",
            platform="workday",
            location="Remote, Canada",
            facts=published_listing_facts("workday", locations="Remote, Canada"),
        )

    sweep = BoardSweep.__new__(BoardSweep)
    sweep.posting_checker = check
    listing_count = 52
    listings = [
        SourceListing(
            employer="Example",
            title="Sales Representative",
            url=_url(0),
            facts=published_listing_facts("workday", locations="1 Location"),
        ),
        *(
            SourceListing(
                employer="Example",
                title=f"Data Product Manager {index}",
                url=_url(index + 1),
                facts=published_listing_facts("workday", locations="2 Locations"),
            )
            for index in range(listing_count)
        ),
    ]
    parameters = JobSearchParameters(
        seniority=Seniority(target_titles=titles("Data Product Manager"))
    )

    enriched, detail = _enrich_titles(
        sweep,
        listings,
        parameters,
        ["Data Product Manager"],
    )

    assert len(calls) == listing_count
    assert (detail.attempted, detail.read, detail.failed, detail.capped) == (
        listing_count,
        listing_count,
        0,
        0,
    )
    assert _url(0) not in calls
    assert enriched[0] == listings[0]
    assert enriched[1].facts.locations.values == ("Remote, Canada",)
    assert enriched[-1].facts.locations.values == ("Remote, Canada",)


def test_a_successful_detail_read_keeps_the_country_the_board_listing_derived() -> None:
    listing = SourceListing(
        employer="Example",
        title="Data Product Manager",
        url=_url(),
        location="2 Locations",
        facts=published_listing_facts("workday", locations=["2 Locations", "India"]),
    )
    sweep = BoardSweep.__new__(BoardSweep)
    sweep.posting_checker = lambda _url: PostingCheck(
        status="readable",
        source=PostingSource(publisher="workday", record="{}", description_paths=("description",)),
        text="Own the data platform.",
        note="fixture",
        platform="workday",
        location="Northtown",
        facts=published_listing_facts("workday", locations=["Northtown", "Southtown"]),
    )

    enriched, _detail = _enrich_titles(
        sweep,
        [listing],
        JobSearchParameters(seniority=Seniority(target_titles=titles("Data Product Manager"))),
        ["Data Product Manager"],
    )

    assert enriched[0].facts.locations.values == ("Northtown", "Southtown", "India")


def test_unreadable_workday_detail_keeps_the_board_summary() -> None:
    listing = SourceListing(
        employer="Example",
        title="Data Product Manager",
        url=_url(),
        location="2 Locations",
        facts=published_listing_facts("workday", locations="2 Locations"),
    )
    sweep = BoardSweep.__new__(BoardSweep)
    sweep.posting_checker = lambda _url: PostingCheck(
        status="unreadable",
        note="fixture missing",
        platform="workday",
    )

    enriched, detail = _enrich_titles(
        sweep,
        [listing],
        JobSearchParameters(seniority=Seniority(target_titles=titles("Data Product Manager"))),
        ["Data Product Manager"],
    )

    assert enriched[0].location == "2 Locations"
    assert enriched[0].facts.locations.complete is False
    assert enriched[0].facts.role_aliases.complete is False
    assert (detail.attempted, detail.read, detail.failed, detail.capped) == (1, 0, 1, 0)


def test_a_detail_body_never_replaces_the_summary_the_board_stated() -> None:
    listing = replace(_tenant_listings(1)[0], summary="Own the freight data roadmap.")
    sweep = BoardSweep.__new__(BoardSweep)
    sweep.posting_checker = lambda _url: PostingCheck(
        status="readable",
        source=PostingSource(publisher="workday", record="{}", description_paths=("description",)),
        note="fixture",
        platform="workday",
        text="Plan brand campaigns.",
    )

    enriched, _detail = _enrich_titles(sweep, [listing])

    assert enriched[0].summary == "Own the freight data roadmap."
    assert enriched[0].published_description == "Plan brand campaigns."


def _role_noun_parameters(
    *,
    title_matching: Literal["guided", "strict"] = "guided",
    focus_term: str = "clinical data integration",
) -> JobSearchParameters:
    return JobSearchParameters(
        seniority=Seniority(
            target_titles=titles("Logistics Planner"),
            specialist_ic_role_nouns=["Analyst"],
            title_matching=title_matching,
        ),
        focus=Focus(domains=[Cluster(name="integration", terms=[focus_term])]),
    )


@pytest.mark.parametrize(
    ("parameters", "reads"),
    [
        pytest.param(_role_noun_parameters(), 1, id="guided-multi-word-focus"),
        pytest.param(_role_noun_parameters(title_matching="strict"), 0, id="strict"),
        pytest.param(
            _role_noun_parameters(focus_term="integration"),
            1,
            id="saved-noun-without-domain-phrase",
        ),
        pytest.param(
            JobSearchParameters(seniority=Seniority(target_titles=titles("Logistics Planner"))),
            0,
            id="unsaved-noun",
        ),
    ],
)
def test_a_saved_role_noun_is_detail_read_in_guided_matching(
    parameters: JobSearchParameters, reads: int
) -> None:
    calls: list[str] = []

    def check(url: str) -> PostingCheck:
        calls.append(url)
        return PostingCheck(
            status="readable",
            source=PostingSource(
                publisher="workday", record="{}", description_paths=("description",)
            ),
            text="Own the data platform.",
            note="fixture",
            platform="workday",
        )

    sweep = BoardSweep.__new__(BoardSweep)
    sweep.posting_checker = check

    _enrich_titles(
        sweep, _tenant_listings(1, title="Senior Data Analyst"), parameters, ["Logistics Planner"]
    )

    assert len(calls) == reads


def test_workday_detail_outcomes_are_retained_with_the_source_scan(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    board_source(
        durable_database,
        workspace_id,
        "Globex",
        "https://globex.wd1.myworkdayjobs.com/Careers",
    )
    state_target_titles(durable_database, workspace_id, "Data Product Manager")
    listing_count = 52
    listings = [
        SourceListing(
            employer="Globex",
            title=f"Data Product Manager {index}",
            url=(
                "https://globex.wd1.myworkdayjobs.com/en-US/Careers/job/"
                f"Canada/Data-Product-Manager_R-{index}"
            ),
            facts=published_listing_facts("workday", locations="2 Locations"),
        )
        for index in range(listing_count)
    ]
    checked = 0

    def detail(url: str) -> PostingCheck:
        nonlocal checked
        checked += 1
        if checked == 3:
            return PostingCheck(status="unreadable", note="missing", platform="workday")
        return PostingCheck(
            status="readable",
            source=PostingSource(
                publisher="workday", record="{}", description_paths=("description",)
            ),
            text="Own the data platform.",
            note="fixture",
            platform="workday",
            location="Remote, Canada",
            facts=published_listing_facts("workday", locations="Remote, Canada"),
        )

    class RecoverableBoard(ScriptedBoards):
        def continue_listings(self, *args: Any, **kwargs: Any) -> SourceRead:
            raise AssertionError("This test inspects retained recovery without executing it.")

    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=RecoverableBoard({"Globex": listings}),
        workday_detail_checker=detail,
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert (
            scan.detail_attempted_count,
            scan.detail_read_count,
            scan.detail_failed_count,
            scan.detail_capped_count,
        ) == (listing_count, listing_count - 1, 1, 0)


@pytest.mark.parametrize("continuation", [False, True], ids=["root", "continued"])
def test_workday_enrichment_is_added_to_the_reader_detail_summary(
    durable_database: Database,
    continuation: bool,
) -> None:
    workspace_id = scan_workspace(durable_database)
    board_source(durable_database, workspace_id, "Acme", f"{_TENANT_HOST}/Careers")
    state_target_titles(durable_database, workspace_id, "Data Product Manager")
    listings = _tenant_listings(2)
    detail = SourceDetailSummary(
        attempted=3,
        read=1,
        failed=1,
        interrupted=1,
        capped=4,
        failure_reasons=(("http_status", 1),),
    )
    read = SourceRead(
        listings=listings,
        complete=False,
        raw_count=3,
        outcome=SourceReadOutcome.PUBLISHER_LINKED_PAGES,
        detail=detail,
    )
    target = SourceContinuationTarget(
        url=listings[0].url,
        label="Data Product Manager",
        label_source="visible",
        source_url=f"{_TENANT_HOST}/Careers",
        structural_context="main",
        relations=("publisher-content", "substantive-label"),
    )

    class ContinuedWorkday(ScriptedBoards):
        def continue_listings(
            self,
            _employer: str,
            _targets: tuple[SourceContinuationTarget, ...],
            *,
            root_url: str = "",
            handoff_scopes: tuple[tuple[str, str, str], ...] = (),
            employer_is_authoritative: bool = False,
            options: object = None,
        ) -> SourceRead:
            return replace(read, attempted_target_urls=(target.url,))

    root = (
        SourceRead(
            listings=[],
            complete=False,
            raw_count=0,
            outcome=SourceReadOutcome.CRAWL_CONTINUATION,
            deferred_targets=(target,),
        )
        if continuation
        else read
    )
    board = ContinuedWorkday({"Acme": root})
    checked: list[str] = []

    def checker(url: str) -> PostingCheck:
        checked.append(url)
        if url == listings[1].url:
            raise httpx.ReadTimeout("synthetic detail timeout", request=httpx.Request("GET", url))
        return PostingCheck(
            status="readable",
            source=PostingSource(
                publisher="workday", record="{}", description_paths=("description",)
            ),
            text="Own the data platform.",
            note="synthetic detail",
            platform="workday",
            location="Remote, Canada",
            facts=published_listing_facts("workday", locations="Remote, Canada"),
        )

    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=board,
        workday_detail_checker=checker,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.scan(workspace_id)
    if continuation:
        worker = Worker(
            durable_database,
            worker_id="workday-detail-summary-worker",
            handlers={
                "source.continue": SourceContinuationHandler(
                    durable_database,
                    board,
                    service.sweep,
                )
            },
        )
        assert worker.run_once()
    assert checked == [listing.url for listing in listings]
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert (
            scan.detail_attempted_count,
            scan.detail_read_count,
            scan.detail_failed_count,
            scan.detail_interrupted_count,
            scan.detail_capped_count,
        ) == (5, 2, 2, 1, 4)
        assert scan.detail_failure_reasons == {"http_status": 1, "timeout": 1}


def test_workday_detail_read_carries_the_body_into_the_holdout_check(
    durable_database: Database,
) -> None:
    """A verified Workday detail body's remote mention must block the structured hold-out.

    Only the detail read carries body text; the bare board sweep never does.
    Proving this end to end through `SourceScanService.scan` means the
    published description really reaches the arrangement hold-out check.
    """
    workspace_id = scan_workspace(durable_database)
    board_source(
        durable_database,
        workspace_id,
        "Globex",
        "https://globex.wd1.myworkdayjobs.com/Careers",
    )
    refused = Arrangement(
        remote=Acceptance.PREFERRED,
        hybrid=Acceptance.REFUSED,
        on_site=Acceptance.REFUSED,
        maximum_office_days=0,
    )
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            locations=[Location(key="canada_general", country="Canada", arrangement=refused)],
            countries=[CountryNames(country="Canada")],
            seniority=Seniority(target_titles=titles("Data Product Manager")),
            arrangement=refused,
        ),
    )
    listing = SourceListing(
        employer="Globex",
        title="Data Product Manager",
        url="https://globex.wd1.myworkdayjobs.com/en-US/Careers/job/Canada/x_R-1",
        facts=published_listing_facts("workday", locations="Canada"),
    )
    body = "This role is hybrid. Remote candidates will be considered."

    def detail(_url: str) -> PostingCheck:
        return PostingCheck(
            status="readable",
            source=PostingSource(
                publisher="workday", record="{}", description_paths=("description",)
            ),
            note="fixture",
            platform="workday",
            location="Toronto, Canada",
            facts=published_listing_facts(
                "workday", locations="Toronto, Canada", arrangements="hybrid"
            ),
            text=body,
        )

    result = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Globex": [listing]}),
        workday_detail_checker=detail,
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    assert [item["title"] for item in result["discoveries"]] == ["Data Product Manager"]


def test_a_workday_detail_body_keeps_its_record_beside_the_listing_capture(
    durable_database: Database,
) -> None:
    """The sweep states no body, so the detail read's record is what the capture converted."""
    workspace_id = scan_workspace(durable_database)
    board_source(
        durable_database, workspace_id, "Globex", "https://globex.wd1.myworkdayjobs.com/Careers"
    )
    state_target_titles(durable_database, workspace_id, "Data Product Manager")
    description = "<p>" + "Own the data product roadmap for freight analytics. " * 10 + "</p>"
    payload = {
        "jobPostingInfo": {
            "title": "Data Product Manager",
            "location": "Toronto, Canada",
            "jobDescription": description,
        }
    }

    def serve(url: str) -> httpx.Response:
        request = httpx.Request("GET", url)
        if "/wday/cxs/" in url:
            return httpx.Response(200, request=request, json=payload)
        return httpx.Response(404, request=request)

    listing = SourceListing(
        employer="Globex",
        title="Data Product Manager",
        url="https://globex.wd1.myworkdayjobs.com/en-US/Careers/job/Canada/x_R-1",
        facts=published_listing_facts("workday", locations="Canada"),
    )
    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Globex": [listing]}),
        workday_detail_checker=lambda url: workday_posting_detail(url, client=policy_client(serve)),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    with durable_database.session() as session:
        rows = session.scalars(select(Artifact)).all()
        files = {row.logical_type: file_uri_path(row.storage_uri) for row in rows}
    assert {kind: path.name for kind, path in files.items() if path is not None} == {
        "posting.listing.text": "listing.txt",
        "posting.source.json": "raw.json",
        "posting.body.html": "description.html",
    }
    assert len({path.parent for path in files.values() if path is not None}) == 1
    raw, markup = files["posting.source.json"], files["posting.body.html"]
    assert raw is not None and markup is not None
    assert json.loads(raw.read_bytes()) == payload
    assert markup.read_text(encoding="utf-8") == description


def test_a_board_scan_reads_a_refused_host_label_boards_career_page_once(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    board_source(durable_database, workspace_id, "Acme", f"{_TENANT_HOST}/{_TENANT_SITE}")
    state_target_titles(durable_database, workspace_id, "Data Product Manager")
    requested: list[str] = []
    client = policy_client(_tenant_site(requested, stated="acme"))

    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": _tenant_listings(3)}),
        workday_detail_checker=partial(workday_posting_detail, client=client),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).scan(workspace_id)

    assert requested.count(f"{_TENANT_HOST}/{_TENANT_SITE}") == 1
    assert len(requested) == 5


def test_saved_workday_role_nouns_are_queued_with_or_without_a_domain_phrase(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    board_source(
        durable_database, workspace_id, "Globex", "https://globex.wd1.myworkdayjobs.com/Careers"
    )
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            seniority=Seniority(
                target_titles=titles("Logistics Planner"), specialist_ic_role_nouns=["Analyst"]
            ),
            focus=Focus(domains=[Cluster(name="integration", terms=["clinical data integration"])]),
        ),
    )
    board = "https://globex.wd1.myworkdayjobs.com/en-US/Careers/job/Remote"
    listings = [
        SourceListing(
            employer="Globex",
            title=title,
            url=f"{board}/{slug}",
            facts=published_listing_facts("workday", locations="Remote"),
        )
        for title, slug in (
            ("Senior Data Analyst", "Senior-Data-Analyst_R-1"),
            ("Senior Data Analyst", "Senior-Data-Analyst_R-2"),
            ("Senior Marketing Manager", "Senior-Marketing-Manager_R-3"),
        )
    ]
    bodies = {
        f"{board}/Senior-Data-Analyst_R-1": "You will own clinical data integration for trials.",
        f"{board}/Senior-Data-Analyst_R-2": "You will build quarterly sales dashboards.",
    }

    def detail(url: str) -> PostingCheck:
        return PostingCheck(
            status="readable",
            source=PostingSource(
                publisher="workday", record="{}", description_paths=("description",)
            ),
            note="fixture",
            platform="workday",
            location="Remote",
            facts=published_listing_facts("workday", locations="Remote"),
            text=bodies.get(url, "You will plan brand campaigns."),
        )

    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Globex": listings}),
        workday_detail_checker=detail,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    result = service.scan(workspace_id)

    assert [item["url"] for item in result["discoveries"]] == [
        listings[0].url,
        listings[1].url,
    ]
    with durable_database.session() as session:
        scan = session.scalars(select(SourceScan)).one()
        assert (scan.detail_attempted_count, scan.detail_read_count) == (2, 2)
    pending = service.list_pending_discoveries(workspace_id)
    assert {item.canonical_url for item in pending} == {listings[0].url, listings[1].url}
    queued = next(item for item in pending if item.canonical_url == listings[0].url)
    bare = next(item for item in pending if item.canonical_url == listings[1].url)
    bare_title = next(item for item in bare.screening_findings if item.dimension == "title")
    assert (bare_title.code, bare_title.subject) == (
        ScreeningCode.TITLE_SPECIALIST_IC_REVIEW,
        "analyst",
    )
    title = next(item for item in queued.screening_findings if item.dimension == "title")
    assert (title.code, title.subject) == (
        ScreeningCode.TITLE_SPECIALIST_IC_REVIEW,
        "analyst, clinical data integration (posting)",
    )
    assert queued.recommendation_because == (
        "The posting states clinical data integration, so this analyst title is kept for "
        "relevance review."
    )


def test_workday_multi_location_detail_stays_incomplete_without_every_location(
    durable_database: Database,
) -> None:
    """A primary-only detail read for "3 Locations" must never read as resolved."""
    workspace_id = scan_workspace(durable_database)
    board_source(
        durable_database,
        workspace_id,
        "Globex",
        "https://globex.wd1.myworkdayjobs.com/Careers",
    )
    state_target_titles(durable_database, workspace_id, "Data Product Manager")
    listing = SourceListing(
        employer="Globex",
        title="Data Product Manager",
        url="https://globex.wd1.myworkdayjobs.com/en-US/Careers/job/Multi/x_R-1",
        facts=published_listing_facts("workday", locations="3 Locations"),
    )

    def detail(_url: str) -> PostingCheck:
        return PostingCheck(
            status="readable",
            source=PostingSource(
                publisher="workday", record="{}", description_paths=("description",)
            ),
            text="Own the data platform.",
            note="fixture",
            platform="workday",
            location="Reading, Berkshire, United Kingdom",
            facts=published_listing_facts(
                "workday", locations="Reading, Berkshire, United Kingdom"
            ),
        )

    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Globex": [listing]}),
        workday_detail_checker=detail,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.scan(workspace_id)

    (row,) = service.list_pending_discoveries(workspace_id)
    assert row.staged_facts.locations.complete is False
    assert "3 Locations" in row.staged_facts.locations.unresolved_values


@pytest.mark.parametrize(
    ("count", "primary", "additional"),
    [
        ("3 Locations", "Remote", ["Remote", "Remote"]),
        (
            "5 Locations",
            "Pune, India",
            ["Chennai, India", "Pune, India", "Chennai, India", "Kochi, India"],
        ),
    ],
)
def test_workday_detail_stating_every_entry_completes_though_labels_repeat(
    durable_database: Database, count: str, primary: str, additional: list[str]
) -> None:
    """Workday repeats a label once per entry, so N entries can name fewer than N places."""
    workspace_id = scan_workspace(durable_database)
    board_source(
        durable_database, workspace_id, "Globex", "https://globex.wd1.myworkdayjobs.com/Careers"
    )
    state_target_titles(durable_database, workspace_id, "Data Product Manager")
    url = "https://globex.wd1.myworkdayjobs.com/en-US/Careers/job/Remote/x_R-3"
    listing = SourceListing(
        employer="Globex",
        title="Data Product Manager",
        url=url,
        facts=published_listing_facts("workday", locations=count),
    )
    payload = {
        "jobPostingInfo": {
            "title": "Data Product Manager",
            "location": primary,
            "additionalLocations": additional,
            "jobDescription": "<p>Own the data product.</p>",
        }
    }

    def detail(checked_url: str) -> PostingCheck:
        return workday_posting_detail(checked_url, client=_DetailClient(payload))  # type: ignore[arg-type]

    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Globex": [listing]}),
        workday_detail_checker=detail,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.scan(workspace_id)

    (row,) = service.list_pending_discoveries(workspace_id)
    assert list(row.staged_facts.locations.unresolved_values) == []
    assert row.staged_facts.locations.complete is True


def test_workday_multi_location_detail_completes_once_every_location_is_read(
    durable_database: Database,
) -> None:
    """A detail read naming every stated location resolves the row completely."""
    workspace_id = scan_workspace(durable_database)
    board_source(
        durable_database,
        workspace_id,
        "Globex",
        "https://globex.wd1.myworkdayjobs.com/Careers",
    )
    state_target_titles(durable_database, workspace_id, "Data Product Manager")
    listing = SourceListing(
        employer="Globex",
        title="Data Product Manager",
        url="https://globex.wd1.myworkdayjobs.com/en-US/Careers/job/Multi/x_R-2",
        facts=published_listing_facts("workday", locations="2 Locations"),
    )

    def detail(_url: str) -> PostingCheck:
        return PostingCheck(
            status="readable",
            source=PostingSource(
                publisher="workday", record="{}", description_paths=("description",)
            ),
            text="Own the data platform.",
            note="fixture",
            platform="workday",
            location="Reading, Berkshire, United Kingdom",
            facts=published_listing_facts(
                "workday",
                locations=["Reading, Berkshire, United Kingdom", "Athens, Greece"],
            ),
        )

    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Globex": [listing]}),
        workday_detail_checker=detail,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.scan(workspace_id)

    (row,) = service.list_pending_discoveries(workspace_id)
    assert row.staged_facts.locations.complete is True
    assert list(row.staged_facts.locations.unresolved_values) == []
