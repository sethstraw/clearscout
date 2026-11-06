"""Public bootstrap search contracts retain full publisher bodies and scope."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from clearscout.application.discovery.source_reading import SourceReadOptions
from clearscout.infrastructure.web_fetch.board_adapters import BoardApiFetcher
from clearscout.infrastructure.web_fetch.policy import PoliteHttpClient
from tests.loop.test_board_mappings_declared_search import _SearchSite
from tests.support.http import public_resolver

_BODY = "<p>Responsibilities include analyzing clinical records and documenting methods.</p>" * 8
_CORNERSTONE = "https://acme.csod.com/ux/ats/careersite/4/home?c=acme"
_JOBSYN = "https://careers.acme.example/jobs/"
_DARWINBOX = "https://acme.darwinbox.com/ms/candidatev2/main/careers/allJobs"
_BODY_FIELDS = {"cornerstone": "externalDescription", "jobsyn": "description", "darwinbox": "jd"}


def test_jobsyn_endpoint_is_declared_by_the_jobs_route_modules() -> None:
    fetcher, calls = _public_search_fetcher("jobsyn")
    original = fetcher.client.getter
    assert original is not None

    def get(url: str, **kwargs: Any) -> httpx.Response:
        modules = {
            "/_nuxt/app.js": (
                "const route={name:`jobs`,path:`/jobs/`,component:()=>import(`./jobs.js`)};"
            ),
            "/_nuxt/jobs.js": (
                'import {search} from "./search.js"; import {client} from "./client.js";'
            ),
            "/_nuxt/client.js": 'const base="https://prod-search-api.jobsyn.org/api/";',
            "/_nuxt/search.js": (
                "const search=(config)=>request(`v1/${name(config.source)}/search`);"
            ),
        }
        path = urlsplit(url).path
        if path in modules:
            return httpx.Response(200, text=modules[path], request=httpx.Request("GET", url))
        return original(url, **kwargs)

    fetcher.client.getter = get
    read = fetcher.listings("Acme", _JOBSYN)
    assert len(read.listings) == 2
    assert all(row.published_description for row in read.listings)
    assert all(
        url.startswith("https://prod-search-api.jobsyn.org/") for url, _body, _headers in calls
    )


def test_jobsyn_endpoint_base_may_sit_one_import_below_the_route_modules() -> None:
    fetcher, calls = _public_search_fetcher("jobsyn")
    original = fetcher.client.getter
    assert original is not None

    def get(url: str, **kwargs: Any) -> httpx.Response:
        modules = {
            "/_nuxt/app.js": (
                "const route={name:`jobs`,path:`/jobs/`,component:()=>import(`./jobs.js`)};"
            ),
            "/_nuxt/jobs.js": 'import {search} from "./search.js";',
            "/_nuxt/search.js": (
                'import {base} from "./client.js";'
                "const search=(config)=>request(`v1/${name(config.source)}/search`);"
            ),
            "/_nuxt/client.js": 'const base="https://prod-search-api.jobsyn.org/api/";',
        }
        path = urlsplit(url).path
        if path in modules:
            return httpx.Response(200, text=modules[path], request=httpx.Request("GET", url))
        return original(url, **kwargs)

    fetcher.client.getter = get
    read = fetcher.listings("Acme", _JOBSYN)
    assert len(read.listings) == 2
    assert all(
        url.startswith("https://prod-search-api.jobsyn.org/") for url, _body, _headers in calls
    )


def _jobsyn_modules(
    modules: dict[str, str],
) -> tuple[BoardApiFetcher, list[tuple[str, dict[str, Any], dict[str, str]]], list[str]]:
    fetcher, calls = _public_search_fetcher("jobsyn")
    original = fetcher.client.getter
    assert original is not None
    requested: list[str] = []

    def get(url: str, **kwargs: Any) -> httpx.Response:
        path = urlsplit(url).path
        if path.startswith("/_nuxt/"):
            requested.append(path)
        if path in modules:
            return httpx.Response(200, text=modules[path], request=httpx.Request("GET", url))
        return original(url, **kwargs)

    fetcher.client.getter = get
    return fetcher, calls, requested


_JOBS_ROUTE = "const route={name:`jobs`,path:`/jobs/`,component:()=>import(`./jobs.js`)};"


def test_jobsyn_reads_when_a_missing_deeper_import_follows_the_declared_endpoint() -> None:
    fetcher, calls, _ = _jobsyn_modules(
        {
            "/_nuxt/app.js": _JOBS_ROUTE,
            "/_nuxt/jobs.js": 'import {search} from "./search.js";',
            "/_nuxt/search.js": (
                'import {gone} from "./gone.js";'
                'const url="https://prod-search-api.jobsyn.org/api/v1/solr/search";'
            ),
        }
    )
    read = fetcher.listings("Acme", _JOBSYN)
    assert len(read.listings) == 2
    assert calls


def test_jobsyn_stops_fetching_modules_at_its_cap() -> None:
    modules = {
        "/_nuxt/app.js": _JOBS_ROUTE,
        "/_nuxt/jobs.js": "".join(f'import {{a}} from "./m{n}.js";' for n in range(30)),
    }
    modules.update({f"/_nuxt/m{n}.js": f'import {{b}} from "./d{n}.js";' for n in range(30)})
    modules.update({f"/_nuxt/d{n}.js": "const nothing=1;" for n in range(30)})
    fetcher, calls, requested = _jobsyn_modules(modules)
    with pytest.raises(ValueError):
        fetcher.listings("Acme", _JOBSYN)
    assert len(requested) <= 24
    assert calls == []


def test_jobsyn_route_module_importing_the_entry_does_not_refetch_it() -> None:
    fetcher, _calls, requested = _jobsyn_modules(
        {
            "/_nuxt/app.js": _JOBS_ROUTE,
            "/_nuxt/jobs.js": 'import {app} from "./app.js"; import {search} from "./search.js";',
            "/_nuxt/search.js": 'const url="https://prod-search-api.jobsyn.org/api/v1/solr/search";',
        }
    )
    read = fetcher.listings("Acme", _JOBSYN)
    assert len(read.listings) == 2
    assert requested.count("/_nuxt/app.js") == 1


def test_declared_detail_redirect_cannot_read_a_protected_identity() -> None:
    site = _SearchSite("alignerr")
    site.pages = [[1], []]
    requested = site.answer
    protected = site.detail_url(99)

    def answer(url: str, **kwargs: Any) -> httpx.Response:
        if url == site.detail_url(1):
            return httpx.Response(
                302, headers={"Location": protected}, request=httpx.Request("GET", url)
            )
        return requested(url)

    site.fetcher.client.getter = answer
    read = site.fetcher.listings(
        "Acme", site.root, options=SourceReadOptions(protected_posting_urls=frozenset({protected}))
    )
    assert site.detail_reads == 0
    assert [listing.requisition_id for listing in read.listings] == ["1"]
    read = site.fetcher.continue_listings(
        "Acme",
        read.deferred_targets,
        root_url=site.root,
        handoff_scopes=(),
        options=SourceReadOptions(protected_posting_urls=frozenset({protected})),
    )
    assert protected not in site.requested
    assert not read.listings
    assert read.complete is False


def _bootstrap_cases(kind: str) -> tuple[str, str, str, list[dict[str, Any]]]:
    if kind == "cornerstone":
        context = {
            "corp": "acme",
            "user": -103,
            "officeUser": None,
            "package": "career-site",
            "page": "home",
            "cultureID": 1,
            "cultureName": "en-US",
            "token": "synthetic-public-token",
            "endpoints": {"cloud": "https://uk.api.csod.com/"},
        }
        root = (
            '<script>var csodPlayerRouteInfo={"package":"career-site","page":"home",'
            '"cid":"4","routeInfo":{"_rest_":"?c=acme"}};</script>'
            f"<script>csod.context={json.dumps(context)};</script>"
        )
        rows = [
            {
                "requisitionId": str(n),
                "displayJobTitle": f"Clinical Analyst {n}",
                "externalDescription": _BODY,
                "locations": [{"city": "Northtown", "country": "CA"}],
                "postingEffectiveDate": "10/10/2026",
                "postingExpirationDate": "-",
            }
            for n in (101, 102)
        ]
        return _CORNERSTONE, root, "https://uk.api.csod.com/rec-job-search/external/jobs", rows
    if kind == "jobsyn":
        root = (
            '<script>window.__NUXT__.config={public:{"job-folder":"careers-acme",'
            '"x-origin":"careers.acme.example",num_items:10,source:"solr"}};</script>'
            '<script type="module" src="/_nuxt/app.js"></script>'
        )
        rows = [
            {
                "guid": f"{n:032X}",
                "title_exact": f"Clinical Analyst {n}",
                "title_slug": f"clinical-analyst-{n}",
                "location_exact": "Northtown, ON",
                "city_slab_exact": "northtown-on",
                "description": _BODY,
                "company_exact": "Acme",
                "reqid": str(n),
            }
            for n in (101, 102)
        ]
        return _JOBSYN, root, "https://prod-search-api.jobsyn.org/api/v1/solr/search", rows
    rows = [
        {
            "id": f"a{n}",
            "title": f"Clinical Analyst {n}",
            "jd": _BODY,
            "tool_tip_locations": ["Northtown, ON, Canada"],
            "parent_company_id": "main",
        }
        for n in (101, 102)
    ]
    return (
        _DARWINBOX,
        '<meta name="application-name" content="Darwinbox">',
        ("https://acme.darwinbox.com/ms/candidateapi/job/alljobs?companyId=main"),
        rows,
    )


def _public_search_fetcher(
    kind: str,
    *,
    root_override: str | None = None,
    failure_page: int | None = None,
    repeated: bool = False,
    rows_override: list[Any] | None = None,
) -> tuple[BoardApiFetcher, list[tuple[str, dict[str, Any], dict[str, str]]]]:
    root_url, root, endpoint, rows = _bootstrap_cases(kind)
    if rows_override is not None:
        rows = rows_override
    calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    def payload(page: int) -> dict[str, Any]:
        selected = rows[:1] if repeated else rows[page - 1 : page]
        if kind == "cornerstone":
            return {"status": "Success", "data": {"requisitions": selected, "totalCount": 2}}
        if kind == "jobsyn":
            return {
                "jobs": selected,
                "pagination": {
                    "page": page,
                    "page_size": 10,
                    "total": 2,
                    "has_more_pages": page < 2,
                },
            }
        return {"status": "success", "data": selected, "job_counts": 2}

    def answer(url: str, **kwargs: Any) -> httpx.Response:
        if url.endswith("/robots.txt"):
            return httpx.Response(200, request=httpx.Request("GET", url), text="")
        if url == root_url:
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                text=root if root_override is None else root_override,
            )
        if url.endswith("/_nuxt/app.js"):
            return httpx.Response(
                200, request=httpx.Request("GET", url), text=f'const url="{endpoint}";'
            )
        if kind == "jobsyn" and url.startswith(endpoint):
            query = parse_qs(urlsplit(url).query)
            page = int(query["page"][0])
            calls.append((url, {}, dict(kwargs.get("headers", {}))))
            return httpx.Response(200, request=httpx.Request("GET", url), json=payload(page))
        return httpx.Response(404, request=httpx.Request("GET", url))

    def post(url: str, **kwargs: Any) -> httpx.Response:
        body = kwargs["json"]
        headers = dict(kwargs.get("headers", {}))
        calls.append((url, body, headers))
        page = int(body.get("pageNumber", body.get("page", 1)))
        if page == failure_page:
            return httpx.Response(503, request=httpx.Request("POST", url))
        if (
            kind == "cornerstone"
            and url == endpoint
            and httpx.Headers(headers).get("Authorization") != "Bearer synthetic-public-token"
        ):
            return httpx.Response(401, request=httpx.Request("POST", url))
        return httpx.Response(200, request=httpx.Request("POST", url), json=payload(page))

    client = PoliteHttpClient(
        user_agent="clearscout/test",
        getter=answer,
        poster=post,
        resolver=public_resolver,
        sleeper=lambda _seconds: None,
        jitter=lambda _low, _high: 0.0,
    )
    return BoardApiFetcher(client=client), calls


@pytest.mark.parametrize("kind", ["cornerstone", "jobsyn", "darwinbox"])
def test_public_search_transfers_disjoint_pages_full_bodies_and_stable_identity(kind: str) -> None:
    fetcher, calls = _public_search_fetcher(kind)
    root, _, endpoint, _ = _bootstrap_cases(kind)

    read = fetcher.listings("Acme", root)

    assert [listing.title for listing in read.listings] == [
        "Clinical Analyst 101",
        "Clinical Analyst 102",
    ]
    assert all(len(listing.published_description or "") > 240 for listing in read.listings)
    assert len({listing.url for listing in read.listings}) == 2
    assert len(calls) >= 2
    assert all(url.startswith(endpoint) for url, _, _ in calls)
    assert read.complete is True


@pytest.mark.parametrize(
    ("kind", "field", "stated", "day"),
    [
        (
            "cornerstone",
            "postingEffectiveDate",
            ("11/2/2026", "04/11/2026"),
            ("2026-11-02", "2026-04-11"),
        ),
        ("darwinbox", "posted_on", ("1792645200", "1792645200"), ("2026-10-22", "2026-10-22")),
    ],
)
def test_public_search_rows_stage_the_posted_date_each_row_states(
    kind: str, field: str, stated: tuple[str, str], day: tuple[str, str]
) -> None:
    rows = [
        {**row, field: value, "created_on": "2026-01-05T00:00:00Z"}
        for row, value in zip(_bootstrap_cases(kind)[3], stated, strict=True)
    ]
    fetcher, _ = _public_search_fetcher(kind, rows_override=rows)

    read = fetcher.listings("Acme", _bootstrap_cases(kind)[0])

    posted = [listing.facts.posted_date for listing in read.listings]
    assert [str(fact.earliest.day) if fact else None for fact in posted] == list(day)
    assert {fact.earliest.basis for fact in posted if fact} == {f"row.publisher.{field}"}


@pytest.mark.parametrize("kind", ["cornerstone", "darwinbox"])
def test_public_search_retains_success_when_a_later_page_fails(kind: str) -> None:
    fetcher, _ = _public_search_fetcher(kind, failure_page=2)

    read = fetcher.listings("Acme", _bootstrap_cases(kind)[0])

    assert [listing.title for listing in read.listings] == ["Clinical Analyst 101"]
    assert read.complete is False
    assert read.deferred_targets


@pytest.mark.parametrize("kind", ["cornerstone", "jobsyn", "darwinbox"])
def test_row_carried_search_bodies_survive_a_listing_cut(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetcher, calls = _public_search_fetcher(kind)
    root, _, endpoint, _ = _bootstrap_cases(kind)
    monkeypatch.setattr(
        fetcher, "deadline_elapsed", lambda: any(url.startswith(endpoint) for url, _, _ in calls)
    )

    read = fetcher.listings("Acme", root)

    assert [listing.title for listing in read.listings] == ["Clinical Analyst 101"]
    assert len(read.listings[0].published_description) > 240
    assert read.complete is False
    assert not any(target.deferred_reason.endswith("_detail") for target in read.deferred_targets)
    assert any(target.deferred_reason.endswith("_page") for target in read.deferred_targets)


@pytest.mark.parametrize("kind", ["cornerstone", "darwinbox"])
def test_public_search_repeated_pages_never_certify_complete_coverage(kind: str) -> None:
    fetcher, _ = _public_search_fetcher(kind, repeated=True)

    read = fetcher.listings("Acme", _bootstrap_cases(kind)[0])

    assert len(read.listings) == 1
    assert read.complete is False
    assert read.skip_reasons["repeated_publisher_page"] == 1
    assert read.deferred_targets and not read.deferred_targets[-1].automatic_eligible


@pytest.mark.parametrize(
    "change", ["missing_token", "private_user", "foreign_endpoint", "wrong_corp"]
)
def test_cornerstone_refuses_bootstrap_outside_its_public_root_scope(change: str) -> None:
    _, root, _, _ = _bootstrap_cases("cornerstone")
    edits = {
        "missing_token": ('"token": "synthetic-public-token"', '"token": ""'),
        "private_user": ('"user": -103', '"user": 123'),
        "foreign_endpoint": ("https://uk.api.csod.com/", "https://foreign.example/"),
        "wrong_corp": ('"corp": "acme"', '"corp": "globex"'),
    }
    original, replacement = edits[change]
    fetcher, calls = _public_search_fetcher(
        "cornerstone", root_override=root.replace(original, replacement)
    )

    with pytest.raises(ValueError):
        fetcher.listings("Acme", _CORNERSTONE)

    assert calls == []


def test_cornerstone_reads_a_root_whose_anonymous_user_is_the_alternate_id() -> None:
    _, root, endpoint, _ = _bootstrap_cases("cornerstone")
    fetcher, calls = _public_search_fetcher(
        "cornerstone", root_override=root.replace('"user": -103', '"user": -101')
    )

    read = fetcher.listings("Acme", _CORNERSTONE)

    assert [listing.title for listing in read.listings] == [
        "Clinical Analyst 101",
        "Clinical Analyst 102",
    ]
    assert all(url == endpoint for url, _, _ in calls)


def test_jobsyn_refuses_a_tenant_origin_not_bound_to_the_public_root() -> None:
    _, root, _, _ = _bootstrap_cases("jobsyn")
    fetcher, calls = _public_search_fetcher(
        "jobsyn",
        root_override=root.replace(
            '"x-origin":"careers.acme.example"', '"x-origin":"careers.globex.example"'
        ),
    )

    with pytest.raises(ValueError):
        fetcher.listings("Acme", _JOBSYN)

    assert calls == []


def _hosted_jobsyn(api: str) -> tuple[BoardApiFetcher, list[tuple[str, Any, dict[str, str]]]]:
    _, root, _, _ = _bootstrap_cases("jobsyn")
    hosted = root.replace('"x-origin":"careers.acme.example"', f'"api-url":"{api}"')
    fetcher, calls = _public_search_fetcher("jobsyn", root_override=hosted)
    original = fetcher.client.getter
    assert original is not None
    modules = {
        "/_nuxt/app.js": _JOBS_ROUTE,
        "/_nuxt/jobs.js": (
            "const go=(n,i)=>fetch(`${n.public[`api-url`]}v1/solr/search?${i}`,"
            "{headers:{'x-origin':n.public[`x-origin`]||window.location.hostname}});"
        ),
    }

    def get(url: str, **kwargs: Any) -> httpx.Response:
        path = urlsplit(url).path
        if path in modules:
            return httpx.Response(200, text=modules[path], request=httpx.Request("GET", url))
        return original(url, **kwargs)

    fetcher.client.getter = get
    return fetcher, calls


def test_jobsyn_root_naming_only_its_api_sends_its_own_host_as_origin() -> None:
    fetcher, calls = _hosted_jobsyn("https://prod-search-api.jobsyn.org/api/")

    read = fetcher.listings("Acme", _JOBSYN)

    assert sorted(listing.title for listing in read.listings) == [
        "Clinical Analyst 101",
        "Clinical Analyst 102",
    ]
    assert {httpx.Headers(headers)["x-origin"] for _, _, headers in calls} == {
        "careers.acme.example"
    }


def test_jobsyn_root_naming_another_api_is_refused() -> None:
    _, root, _, _ = _bootstrap_cases("jobsyn")
    foreign = root.replace(
        '"x-origin":"careers.acme.example"',
        '"x-origin":"careers.acme.example","api-url":"https://search.globex.example/api/"',
    )
    fetcher, calls = _public_search_fetcher("jobsyn", root_override=foreign)

    with pytest.raises(ValueError):
        fetcher.listings("Acme", _JOBSYN)

    assert calls == []


def test_cornerstone_search_never_reuses_credentials_in_another_search() -> None:
    fetcher, calls = _public_search_fetcher("cornerstone")
    fetcher.listings("Acme", _CORNERSTONE)
    fetcher.post_json("https://acme.example/search", {})

    assert httpx.Headers(calls[0][2])["Authorization"] == "Bearer synthetic-public-token"
    assert "Authorization" not in httpx.Headers(calls[-1][2])


def test_cornerstone_search_claims_no_origin_of_its_own() -> None:
    fetcher, calls = _public_search_fetcher("cornerstone")
    fetcher.listings("Acme", _CORNERSTONE)

    assert calls
    assert all("Origin" not in httpx.Headers(headers) for _url, _body, headers in calls)


def test_a_protected_darwinbox_identity_needs_no_detail_request() -> None:
    fetcher, _ = _public_search_fetcher("darwinbox")
    protected = "https://acme.darwinbox.com/ms/candidatev2/main/careers/jobDetails/a101"

    read = fetcher.listings(
        "Acme", _DARWINBOX, options=SourceReadOptions(protected_posting_urls=frozenset({protected}))
    )

    assert [listing.title for listing in read.listings] == ["Clinical Analyst 102"]
    assert protected in read.protected_posting_urls


def test_jobsyn_uses_published_location_instead_of_the_city_search_slab_for_its_url() -> None:
    root, _, _, rows = _bootstrap_cases("jobsyn")
    rows[0]["location_exact"] = "Atlanta, GA"
    rows[0]["city_slab_exact"] = "atlanta/georgia/usa/jobs::Atlanta, GA"
    fetcher, _ = _public_search_fetcher("jobsyn", rows_override=rows)

    read = fetcher.listings("Acme", root)

    assert read.listings[0].url == (
        f"https://careers.acme.example/atlanta-ga/clinical-analyst-101/{101:032X}/job/"
    )
    assert read.listings[0].requisition_id == f"{101:032X}"
    assert read.listings[0].location == "Atlanta, GA"


@pytest.mark.parametrize("kind", ["cornerstone", "jobsyn", "darwinbox"])
def test_a_missing_search_body_is_failed_and_retains_the_exact_publisher_identity(
    kind: str,
) -> None:
    root, _, _, rows = _bootstrap_cases(kind)
    body_key = _BODY_FIELDS[kind]
    rows[1].pop(body_key)
    fetcher, _ = _public_search_fetcher(kind, rows_override=rows)

    read = fetcher.listings("Acme", root)

    assert [listing.title for listing in read.listings] == ["Clinical Analyst 101"]
    assert read.complete is False
    assert read.detail.read == 1
    assert read.detail.failed == 1
    assert len(read.deferred_targets) == 1
    target = read.deferred_targets[0]
    assert target.source_url == root
    assert target.detail_checkpoint is not None
    expected = {"cornerstone": "102", "jobsyn": f"{102:032X}", "darwinbox": "a102"}[kind]
    assert target.detail_checkpoint.pending_ids == (expected,)
    assert target.detail_checkpoint.board_url == target.url


@pytest.mark.parametrize("kind", ["cornerstone", "jobsyn", "darwinbox"])
def test_search_detail_recovery_restores_only_the_retained_publisher_identity(kind: str) -> None:
    root, _, _, rows = _bootstrap_cases(kind)
    body_key = _BODY_FIELDS[kind]
    rows[1].pop(body_key)
    fetcher, _ = _public_search_fetcher(kind, rows_override=rows)
    first = fetcher.listings("Acme", root)
    assert len(first.deferred_targets) == 1
    target = first.deferred_targets[0]
    rows[1][body_key] = _BODY

    read = fetcher.continue_listings("Acme", (target,), root_url=root, handoff_scopes=())

    assert len(read.listings) == 1
    recovered = read.listings[0]
    assert target.detail_checkpoint is not None
    assert recovered.requisition_id == target.detail_checkpoint.pending_ids[0]
    assert recovered.url == target.url
    assert recovered.title == "Clinical Analyst 102"
    assert len(recovered.published_description) > 240
    assert read.detail.read == 1
    assert not read.deferred_targets


@pytest.mark.parametrize("kind", ["cornerstone", "jobsyn", "darwinbox"])
def test_search_detail_recovery_refuses_an_identity_whose_address_does_not_match(kind: str) -> None:
    root, _, _, rows = _bootstrap_cases(kind)
    body_key = _BODY_FIELDS[kind]
    rows[1].pop(body_key)
    fetcher, _ = _public_search_fetcher(kind, rows_override=rows)
    first = fetcher.listings("Acme", root)
    assert len(first.deferred_targets) == 1
    target = first.deferred_targets[0]
    assert target.detail_checkpoint is not None
    mismatched = target.url + ("&" if "?" in target.url else "?") + "tenant=other"
    target = replace(
        target,
        url=mismatched,
        detail_checkpoint=replace(target.detail_checkpoint, board_url=mismatched),
    )
    rows[1][body_key] = _BODY

    read = fetcher.continue_listings("Acme", (target,), root_url=root, handoff_scopes=())

    assert not read.listings
    assert read.detail.read == 0
    assert read.complete is False
    assert read.skip_reasons["publisher_detail_identity_changed"] == 1


@pytest.mark.parametrize("kind", ["cornerstone", "jobsyn", "darwinbox"])
def test_a_malformed_search_record_preserves_the_prior_good_page(kind: str) -> None:
    root, _, _, rows = _bootstrap_cases(kind)
    records: list[Any] = [rows[0], None]
    fetcher, _ = _public_search_fetcher(kind, rows_override=records)

    read = fetcher.listings("Acme", root)

    assert [listing.title for listing in read.listings] == ["Clinical Analyst 101"]
    assert read.detail.read == 1
    assert read.complete is False
    assert read.deferred_targets
    assert all(target.source_url == root for target in read.deferred_targets)


def test_cornerstone_refreshes_an_expired_anonymous_token_before_resuming_the_failed_page() -> None:
    root_url, root, endpoint, rows = _bootstrap_cases("cornerstone")
    fetcher, calls = _public_search_fetcher("cornerstone")
    getter = fetcher.client.getter
    assert getter is not None
    refreshed = False
    bootstrap_tokens: list[str] = []

    def answer(url: str, **kwargs: Any) -> httpx.Response:
        if url == root_url:
            token = "synthetic-refreshed-token" if refreshed else "synthetic-public-token"
            bootstrap_tokens.append(token)
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                text=root.replace("synthetic-public-token", token),
            )
        return getter(url, **kwargs)

    def post(url: str, **kwargs: Any) -> httpx.Response:
        body = kwargs["json"]
        headers = dict(kwargs.get("headers", {}))
        calls.append((url, body, headers))
        page = int(body["pageNumber"])
        token = "synthetic-refreshed-token" if refreshed else "synthetic-public-token"
        if httpx.Headers(headers).get("Authorization") != "Bearer " + token or (
            page == 2 and not refreshed
        ):
            return httpx.Response(401, request=httpx.Request("POST", url))
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "status": "Success",
                "data": {"requisitions": rows[page - 1 : page], "totalCount": 2},
            },
        )

    fetcher.client.getter = answer
    fetcher.client.poster = post

    first = fetcher.listings("Acme", root_url)

    assert [listing.title for listing in first.listings] == ["Clinical Analyst 101"]
    assert first.complete is False
    assert first.deferred_targets
    refreshed = True

    resumed = fetcher.continue_listings(
        "Acme", first.deferred_targets, root_url=root_url, handoff_scopes=()
    )

    assert [listing.title for listing in resumed.listings] == ["Clinical Analyst 102"]
    assert not resumed.deferred_targets
    assert bootstrap_tokens == ["synthetic-public-token", "synthetic-refreshed-token"]
    assert [body["pageNumber"] for _, body, _ in calls] == [1, 2, 2, 3]
    assert httpx.Headers(calls[2][2])["Authorization"] == "Bearer synthetic-refreshed-token"
    assert all(url == endpoint for url, _, _ in calls)


@pytest.mark.parametrize(
    ("kind", "query"),
    [
        ("cornerstone", "c=acme&searchText=clinical"),
        ("jobsyn", "department=research"),
        ("darwinbox", "companyId=other"),
    ],
)
def test_search_roots_refuse_filters_their_declared_request_cannot_preserve(
    kind: str, query: str
) -> None:
    root_url, root, _, _ = _bootstrap_cases(kind)
    scoped_root = root_url.partition("?")[0] + "?" + query
    fetcher, calls = _public_search_fetcher(kind)
    getter = fetcher.client.getter
    assert getter is not None

    def answer(url: str, **kwargs: Any) -> httpx.Response:
        if url == scoped_root:
            return httpx.Response(200, request=httpx.Request("GET", url), text=root)
        return getter(url, **kwargs)

    fetcher.client.getter = answer

    with pytest.raises(ValueError):
        fetcher.listings("Acme", scoped_root)

    assert calls == []


def test_cornerstone_refuses_a_different_site_in_its_bootstrap_route() -> None:
    _, root, _, _ = _bootstrap_cases("cornerstone")
    fetcher, calls = _public_search_fetcher(
        "cornerstone", root_override=root.replace('"cid":"4"', '"cid":"5"')
    )

    with pytest.raises(ValueError):
        fetcher.listings("Acme", _CORNERSTONE)

    assert calls == []


def test_jobsyn_refuses_an_endpoint_not_declared_by_its_selected_application() -> None:
    fetcher, calls = _public_search_fetcher("jobsyn")
    getter = fetcher.client.getter
    assert getter is not None

    def answer(url: str, **kwargs: Any) -> httpx.Response:
        if url.endswith("/_nuxt/app.js"):
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                text='const endpoint="https://foreign.example/api/v1/solr/search";',
            )
        return getter(url, **kwargs)

    fetcher.client.getter = answer

    with pytest.raises(ValueError):
        fetcher.listings("Acme", _JOBSYN)

    assert calls == []
