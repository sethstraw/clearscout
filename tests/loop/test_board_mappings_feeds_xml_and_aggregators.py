"""Board declarations for the XML/RSS feed boards and the bounded aggregators."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx
import pytest
from defusedxml.ElementTree import fromstring

from clearscout.domain.staged_facts import PostedDateGrade
from clearscout.infrastructure.web_fetch.board_adapters import BoardApiFetcher
from clearscout.infrastructure.web_fetch.board_mappings.arbeitnow import ARBEITNOW_MAPPING
from clearscout.infrastructure.web_fetch.board_mappings.comeet import COMEET_MAPPING
from clearscout.infrastructure.web_fetch.board_mappings.himalayas import HIMALAYAS_MAPPING
from clearscout.infrastructure.web_fetch.board_mappings.icims import ICIMS_MAPPING
from clearscout.infrastructure.web_fetch.board_mappings.mapper import Posting, source_listing
from clearscout.infrastructure.web_fetch.board_mappings.personio import PERSONIO_MAPPING
from clearscout.infrastructure.web_fetch.board_mappings.phenom import PHENOM_MAPPING
from clearscout.infrastructure.web_fetch.board_mappings.teamtailor import TEAMTAILOR_MAPPING
from tests.support.doubles import ScriptedPage
from tests.support.http import policy_client

_TEAMTAILOR_NAMESPACE = "https://teamtailor.com/locations"


def _teamtailor_item(**overrides: str) -> Any:
    description = overrides.get("description", "&lt;p&gt;Support clinical teams.&lt;/p&gt;")
    status = overrides.get("remote_status", "")
    second_place = overrides.get("second_place", "Southtown")
    xml = (
        f'<item xmlns:tt="{_TEAMTAILOR_NAMESPACE}">'
        f"<title>{overrides.get('title', 'Clinical Systems Analyst')}</title>"
        f"<link>{overrides.get('link', 'https://globex.teamtailor.com/jobs/1')}</link>"
        f"<guid>{overrides.get('guid', 'tt-1')}</guid>"
        f"<description>{description}</description>"
        f"{f'<remoteStatus>{status}</remoteStatus>' if status else ''}"
        "<tt:locations>"
        "<tt:location><tt:name>Northtown</tt:name></tt:location>"
        f"<tt:location><tt:name>{second_place}</tt:name></tt:location>"
        "</tt:locations>"
        "</item>"
    )
    return fromstring(xml)


def test_a_teamtailor_listing_states_its_fields_and_namespaced_locations() -> None:
    listing = source_listing(TEAMTAILOR_MAPPING, Posting(_teamtailor_item(), employer="Globex"))

    assert listing is not None
    assert (listing.employer, listing.title) == ("Globex", "Clinical Systems Analyst")
    assert listing.url == "https://globex.teamtailor.com/jobs/1"
    assert listing.requisition_id == "tt-1"
    assert listing.location == "Northtown"
    assert listing.facts.locations.values == ("Northtown", "Southtown")
    assert listing.published_description == "Support clinical teams."
    assert listing.facts.role_aliases.complete is False


@pytest.mark.parametrize(
    ("status", "values", "unresolved", "complete"),
    [
        pytest.param("hybrid", ("hybrid",), (), True, id="hybrid"),
        pytest.param("fully", ("remote",), (), True, id="fully-remote"),
        pytest.param("none", (), (), False, id="unset-default-states-nothing"),
        pytest.param("temporary", (), ("remoteStatus: temporary",), False, id="temporary"),
    ],
)
def test_a_teamtailor_arrangement_reads_the_stated_remote_status(
    status: str, values: tuple[str, ...], unresolved: tuple[str, ...], complete: bool
) -> None:
    listing = source_listing(
        TEAMTAILOR_MAPPING, Posting(_teamtailor_item(remote_status=status), employer="Globex")
    )

    assert listing is not None
    assert listing.facts.arrangements.values == values
    assert listing.facts.arrangements.unresolved_values == unresolved
    assert listing.facts.arrangements.complete is complete


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"second_place": "Remote, Canada"}, id="remote-place"),
        pytest.param(
            {"description": "&lt;p&gt;You may work remotely from home.&lt;/p&gt;"},
            id="remote-offer-in-body",
        ),
    ],
)
def test_a_teamtailor_hybrid_status_is_unresolved_beside_remote(overrides: dict[str, str]) -> None:
    item = _teamtailor_item(remote_status="hybrid", **overrides)

    listing = source_listing(TEAMTAILOR_MAPPING, Posting(item, employer="Globex"))

    assert listing is not None
    assert listing.facts.arrangements.values == ()
    assert "hybrid" in listing.facts.arrangements.unresolved_values


def _personio_position(*, subcompany: str = "", description: str = "", salary: str = "") -> Any:
    sections = description or (
        "<jobDescription><value>&lt;p&gt;Configure systems.&lt;/p&gt;</value></jobDescription>"
    )
    xml = (
        "<position>"
        "<id>42</id>"
        "<name>Clinical Systems Analyst</name>"
        "<office>Northtown</office>"
        f"{f'<subcompany>{subcompany}</subcompany>' if subcompany else ''}"
        f"<jobDescriptions>{sections}</jobDescriptions>"
        f"{salary}"
        "</position>"
    )
    return fromstring(xml)


def test_a_personio_position_states_its_salary_information() -> None:
    salary = (
        "<salaryInformation><min>55000</min><max>65000</max>"
        "<currencyCode>EUR</currencyCode><type>yearly</type></salaryInformation>"
    )
    posting = Posting(
        _personio_position(salary=salary), employer="Globex", groups={"tenant": "globex"}
    )

    listing = source_listing(PERSONIO_MAPPING, posting)

    assert listing is not None
    assert listing.facts.compensation.values == ("EUR 55000-65000 per year",)


@pytest.mark.parametrize(
    ("subcompany", "expected"),
    [
        pytest.param("Globex Health", "Globex Health", id="subcompany-stated"),
        pytest.param("", "Globex", id="subcompany-absent"),
    ],
)
def test_a_personio_employer_prefers_the_stated_subcompany(subcompany: str, expected: str) -> None:
    posting = Posting(
        _personio_position(subcompany=subcompany),
        employer="Globex",
        groups={"tenant": "globex", "tld": "com"},
    )

    listing = source_listing(PERSONIO_MAPPING, posting)

    assert listing is not None
    assert listing.employer == expected


def test_a_personio_listing_builds_its_url_from_the_tenant_and_tld() -> None:
    posting = Posting(
        _personio_position(), employer="Globex", groups={"tenant": "globex", "tld": "de"}
    )

    listing = source_listing(PERSONIO_MAPPING, posting)

    assert listing is not None
    assert listing.url == "https://globex.jobs.personio.de/job/42?language=en"


def test_a_personio_listing_joins_every_job_description_section() -> None:
    sections = (
        "<jobDescription><value>&lt;p&gt;Configure systems.&lt;/p&gt;</value></jobDescription>"
        "<jobDescription><value>&lt;p&gt;Support hospital teams.&lt;/p&gt;</value></jobDescription>"
    )
    posting = Posting(
        _personio_position(description=sections),
        employer="Globex",
        groups={"tenant": "globex", "tld": "com"},
    )

    listing = source_listing(PERSONIO_MAPPING, posting)

    assert listing is not None
    assert listing.published_description == "Configure systems.\n\nSupport hospital teams."
    assert listing.location == "Northtown"


@pytest.mark.parametrize(
    ("mapping", "item", "title_tag"),
    [
        pytest.param(TEAMTAILOR_MAPPING, _teamtailor_item(), "title", id="teamtailor"),
        pytest.param(PERSONIO_MAPPING, _personio_position(), "name", id="personio"),
    ],
)
def test_a_feed_item_keeps_its_own_xml_as_its_source(
    mapping: Any, item: Any, title_tag: str
) -> None:
    posting = Posting(item, employer="Globex", groups={"tenant": "globex", "tld": "com"})

    listing = source_listing(mapping, posting)

    assert listing is not None and listing.source is not None
    assert listing.source.record == ""
    assert listing.source.page_html == ""
    kept = fromstring(listing.source.item_xml)
    assert kept.findtext(title_tag) == listing.title
    assert listing.source.description_markup.startswith("<p>")


def test_a_feed_item_source_leaves_out_the_feed_text_after_it() -> None:
    feed = fromstring(
        "<rss><channel>"
        "<item><title>Systems Analyst</title><link>https://globex.teamtailor.com/jobs/1</link>"
        "<guid>tt-1</guid><description>&lt;p&gt;Support depot teams.&lt;/p&gt;</description>"
        "</item>Trailing feed text"
        "<item><title>Unrelated Role</title></item>"
        "</channel></rss>"
    )
    item = feed.find("channel/item")

    listing = source_listing(TEAMTAILOR_MAPPING, Posting(item, employer="Globex"))

    assert listing is not None and listing.source is not None
    assert listing.source.page_html == ""
    assert listing.source.item_xml.endswith("</item>")
    assert "Trailing feed text" not in listing.source.item_xml
    assert item is not None and item.tail == "Trailing feed text"


def _comeet_job(**overrides: Any) -> dict[str, Any]:
    return {
        "name": "Clinical Systems Analyst",
        "url_active_page": "https://www.comeet.com/jobs/globex/1",
        "internal_use_custom_id": "REQ-1",
        "location": {"name": "Northtown"},
        "details": [{"name": "Description", "value": "<p>Configure systems.</p>"}],
        **overrides,
    }


@pytest.mark.parametrize(
    ("overrides", "values", "unresolved"),
    [
        pytest.param({"workplace_type": "Hybrid"}, ("hybrid",), (), id="workplace-type"),
        pytest.param({"workplace_type": "On-site"}, ("onsite",), (), id="hyphenated-onsite"),
        pytest.param(
            {"location": {"name": "Northtown", "is_remote": True}},
            ("remote",),
            (),
            id="remote-flag",
        ),
        pytest.param(
            {"workplace_type": "Flexible"}, (), ("workplace_type: Flexible",), id="unknown-type"
        ),
        pytest.param({"location": {"name": "Northtown", "is_remote": False}}, (), (), id="unset"),
    ],
)
def test_a_comeet_position_states_its_workplace_type_or_remote_flag(
    overrides: dict[str, Any], values: tuple[str, ...], unresolved: tuple[str, ...]
) -> None:
    listing = source_listing(COMEET_MAPPING, Posting(_comeet_job(**overrides), employer="Globex"))

    assert listing is not None
    assert listing.facts.arrangements.values == values
    assert listing.facts.arrangements.unresolved_values == unresolved


def test_a_comeet_listing_states_its_fields_and_nested_location() -> None:
    listing = source_listing(COMEET_MAPPING, Posting(_comeet_job(), employer="Globex"))

    assert listing is not None
    assert listing.url == "https://www.comeet.com/jobs/globex/1"
    assert listing.requisition_id == "REQ-1"
    assert listing.location == "Northtown"
    assert listing.published_description == "Configure systems."


@pytest.mark.parametrize(
    ("overrides", "expected_url", "expected_requisition"),
    [
        pytest.param({}, "https://www.comeet.com/jobs/globex/1", "REQ-1", id="primary-fields"),
        pytest.param(
            {
                "url_active_page": "",
                "url_comeet_hosted_page": "https://www.comeet.com/jobs/globex/2",
                "internal_use_custom_id": "",
                "uid": "uid-1",
            },
            "https://www.comeet.com/jobs/globex/2",
            "uid-1",
            id="fallback-fields",
        ),
    ],
)
def test_a_comeet_listing_falls_back_between_its_url_and_requisition_fields(
    overrides: dict[str, Any], expected_url: str, expected_requisition: str
) -> None:
    listing = source_listing(COMEET_MAPPING, Posting(_comeet_job(**overrides), employer="Globex"))

    assert listing is not None
    assert listing.url == expected_url
    assert listing.requisition_id == expected_requisition


def _icims_data(**overrides: Any) -> dict[str, Any]:
    return {
        "title": "Clinical Systems Analyst",
        "apply_url": "https://careers-globex.icims.com/jobs/1/analyst/job",
        "full_location": "Northtown, ST",
        "req_id": "REQ-1",
        "description": "<p>Configure systems.</p>",
        **overrides,
    }


def test_a_jibe_job_states_its_salary_bounds_without_a_guessed_currency() -> None:
    data = _icims_data(salary_min_value=70000.0, salary_max_value=90000.0)

    listing = source_listing(ICIMS_MAPPING, Posting(data, employer="Globex"))

    assert listing is not None
    assert listing.facts.compensation.values == ("70000-90000",)


def test_an_icims_job_page_is_rewritten_to_its_iframe_body() -> None:
    listing = source_listing(ICIMS_MAPPING, Posting(_icims_data(), employer="Globex"))

    assert listing is not None
    assert listing.url == "https://careers-globex.icims.com/jobs/1/job?in_iframe=1"
    assert listing.location == "Northtown, ST"
    assert listing.published_description == "Configure systems."


def test_an_icims_login_gated_apply_url_is_not_a_listing() -> None:
    data = _icims_data(apply_url="https://careers-globex.icims.com/jobs/intro/login")

    assert source_listing(ICIMS_MAPPING, Posting(data, employer="Globex")) is None


def _phenom_job(**overrides: Any) -> dict[str, Any]:
    return {
        "title": "Clinical Systems Analyst",
        "applyUrl": "https://careers.globex.com/job/1",
        "cityStateCountry": "Northtown, ST, US",
        "jobId": "1001",
        **overrides,
    }


@pytest.mark.parametrize(
    ("overrides", "expected_location", "expected_requisition"),
    [
        pytest.param({}, "Northtown, ST, US", "1001", id="primary-fields"),
        pytest.param(
            {
                "cityStateCountry": "",
                "location": "Southtown, ST, US",
                "jobId": "",
                "jobSeqNo": "seq-2",
            },
            "Southtown, ST, US",
            "seq-2",
            id="fallback-fields",
        ),
    ],
)
def test_a_phenom_listing_falls_back_between_its_location_and_requisition_fields(
    overrides: dict[str, Any], expected_location: str, expected_requisition: str
) -> None:
    listing = source_listing(PHENOM_MAPPING, Posting(_phenom_job(**overrides), employer="Globex"))

    assert listing is not None
    assert listing.location == expected_location
    assert listing.requisition_id == expected_requisition
    assert listing.facts.arrangements.complete is False


def _himalayas_job(**overrides: Any) -> dict[str, Any]:
    return {
        "title": "Clinical Systems Analyst",
        "applicationLink": "https://himalayas.app/jobs/globex-1",
        "guid": "guid-1",
        "companyName": "Globex",
        "locationRestrictions": ["Northtown", "Southtown"],
        **overrides,
    }


def test_a_himalayas_listing_joins_its_places_with_a_comma() -> None:
    listing = source_listing(HIMALAYAS_MAPPING, Posting(_himalayas_job(), employer=""))

    assert listing is not None
    assert listing.location == "Northtown, Southtown"
    assert listing.facts.locations.values == ("Northtown", "Southtown")


def test_a_himalayas_listing_recovers_its_employer_from_the_company_slug() -> None:
    job = _himalayas_job(companyName="name", companySlug="globex-health")

    listing = source_listing(HIMALAYAS_MAPPING, Posting(job, employer=""))

    assert listing is not None
    assert listing.employer == "Globex Health"


def test_a_himalayas_listing_prefers_the_stated_employer_over_the_configured_one() -> None:
    listing = source_listing(HIMALAYAS_MAPPING, Posting(_himalayas_job(), employer="Initech"))

    assert listing is not None
    assert listing.employer == "Globex"


def test_an_arbeitnow_listing_states_its_fields() -> None:
    job = {
        "title": "Clinical Systems Analyst",
        "url": "https://www.arbeitnow.com/jobs/globex-1",
        "slug": "globex-1",
        "company_name": "Globex",
        "location": "Northtown",
    }

    listing = source_listing(ARBEITNOW_MAPPING, Posting(job, employer=""))

    assert listing is not None
    assert (listing.employer, listing.location, listing.requisition_id) == (
        "Globex",
        "Northtown",
        "globex-1",
    )
    assert listing.facts.arrangements.complete is False


def test_personio_retains_all_description_sections_and_stated_employer() -> None:
    class ScriptedPersonio(BoardApiFetcher):
        def __init__(self) -> None:
            pass

        def get_feed(self, url: str) -> str:
            assert url == "https://hr-system.jobs.personio.com/xml?language=en"
            return """<?xml version="1.0"?>
                <workzag-jobs><position><id>2100001</id><subcompany>Synthetic</subcompany>
                <office>Synthetic / Austria / Innsbruck</office>
                <name>Process Analyst</name><jobDescriptions>
                <jobDescription><name>Role</name><value>
                <![CDATA[<p>Map pharma workflows.</p>]]></value></jobDescription>
                <jobDescription><name>Requirements</name><value>
                <![CDATA[<p>Know GxP.</p>]]></value></jobDescription>
                </jobDescriptions></position></workzag-jobs>"""

    read = ScriptedPersonio().listings(
        "Parent Group", "https://hr-system.jobs.personio.com/?language=en"
    )

    [listing] = read.listings
    assert read.complete is True
    assert listing.employer == "Synthetic"
    assert listing.url == "https://hr-system.jobs.personio.com/job/2100001?language=en"
    assert listing.location == "Synthetic / Austria / Innsbruck"
    assert listing.published_description == "Map pharma workflows.\n\nKnow GxP."


def test_comeet_requires_the_public_token_and_requests_full_details() -> None:
    requested: list[str] = []

    class ScriptedComeet(BoardApiFetcher):
        def __init__(self) -> None:
            pass

        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            requested.append(url)
            return [
                {
                    "uid": "87.405",
                    "name": "Clinical Data Product Manager",
                    "company_name": "Health Co",
                    "url_active_page": (
                        "https://www.comeet.co/jobs/health-co/E5.007/"
                        "clinical-data-product-manager/87.405"
                    ),
                    "location": {"name": "Remote, Canada"},
                    "details": [
                        {"name": "Description", "value": "<p>Own clinical data products.</p>"},
                        {"name": "Requirements", "value": "<p>Know regulated software.</p>"},
                    ],
                }
            ]

    source = (
        "https://www.comeet.co/careers-api/2.0/company/E5.007/positions"
        "?token=5E7236A0BCE5E7295111B55E70BCE"
    )
    read = ScriptedComeet().listings("Health Co", source)

    assert requested == [f"{source}&details=true"]
    [listing] = read.listings
    assert read.complete is True
    assert listing.requisition_id == "87.405"
    assert listing.location == "Remote, Canada"
    assert listing.published_description == (
        "Own clinical data products.\n\nKnow regulated software."
    )


@pytest.mark.parametrize(
    "source",
    ["https://www.comeet.com/jobs/health-co/E5.007", "https://www.comeet.com/jobs/health-co/"],
    ids=["company-page", "board-root"],
)
def test_comeet_reads_a_careers_page_through_the_token_its_company_data_states(
    source: str,
) -> None:
    requested: list[str] = []

    class ScriptedComeet(BoardApiFetcher):
        def __init__(self) -> None:
            pass

        def get_text(self, url: str, *, max_bytes: int = 0) -> str:
            requested.append(url)
            company = {"name": "Health Co", "company_uid": "E5.007", "token": "5E72A0BCE5E7295"}
            return (
                f"{url}\n<html><body><script>var COMPANY_DATA;\n"
                f"COMPANY_DATA = {json.dumps(company)};\nCOMPANY_POSITIONS_DATA = [];"
                "</script></body></html>"
            )

        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            requested.append(url)
            return [{"uid": "87.405", "name": "Clinical Data Manager", "url_active_page": url}]

    read = ScriptedComeet().listings("Health Co", source)

    assert requested == [
        source,
        "https://www.comeet.co/careers-api/2.0/company/E5.007/positions"
        "?token=5E72A0BCE5E7295&details=true",
    ]
    [listing] = read.listings
    assert listing.title == "Clinical Data Manager"


_EMPLOYER_CAREERS = "https://careers.example-med.com/careers/"
_COMEET_EMBED_API = "https://www.comeet.co/careers-api/2.0/company/E5.007/positions"


def _comeet_plugin_page(init: str) -> str:
    return (
        '<html><body><div id="comeet-careers"></div><script type="text/javascript">'
        f"window.comeetInit = function() {{ COMEET.init({init}); }};"
        "(function(d, s, id) { var js = d.createElement(s); js.id = id;"
        ' js.src = "//www.comeet.co/careers-api/api.js";'
        " d.body.appendChild(js); }(document, 'script', 'comeet-jsapi'));"
        "</script></body></html>"
    )


_COMEET_PLUGIN_INIT = (
    '{ "token": "5E72A0BCE5E7295", "company-uid": "E5.007", "company-name": "Example Med" }'
)


def _comeet_embed_client(page: str, requested: list[str], *, robots: str = "") -> BoardApiFetcher:
    def answer(url: str) -> httpx.Response:
        requested.append(url)
        request = httpx.Request("GET", url)
        if url.startswith("https://www.comeet.co/careers-api/2.0/company/"):
            row = {"uid": "87.405", "name": "Clinical Data Manager", "url_active_page": url}
            return httpx.Response(200, request=request, json=[row])
        return httpx.Response(200, request=request, text=page)

    return BoardApiFetcher(client=policy_client(answer, robots=robots))


@pytest.mark.parametrize(
    "init",
    [
        _COMEET_PLUGIN_INIT,
        "{'company-uid': 'E5.007', 'company-name': 'Example Med', 'token': '5E72A0BCE5E7295'}",
    ],
    ids=["token-first", "uid-first"],
)
def test_an_employer_page_embedding_the_comeet_plugin_reads_its_positions(init: str) -> None:
    requested: list[str] = []
    fetcher = _comeet_embed_client(_comeet_plugin_page(init), requested)

    assert fetcher.resolve(_EMPLOYER_CAREERS).platform == "comeet"
    read = fetcher.listings("Example Med", _EMPLOYER_CAREERS)

    assert f"{_COMEET_EMBED_API}?token=5E72A0BCE5E7295&details=true" in requested
    assert [listing.title for listing in read.listings] == ["Clinical Data Manager"]


@pytest.mark.parametrize(
    "page",
    [
        "<html><body><h1>Careers at Example Med</h1></body></html>",
        _comeet_plugin_page('{ "company-name": "Example Med" }'),
        "<script>MYCOMEET.init({ 'token': 'AB12', 'company-uid': 'E5.007' });</script>",
    ],
    ids=["no-plugin", "init-without-key", "other-object"],
)
def test_an_employer_page_without_a_comeet_plugin_key_names_no_comeet_board(page: str) -> None:
    fetcher = _comeet_embed_client(page, [])

    assert fetcher.resolve(_EMPLOYER_CAREERS).platform != "comeet"


def _comeet_settings_page(settings: str) -> str:
    return (
        '<html><body><div class="comeet-outlet"></div><script type="text/javascript">'
        f"/* <![CDATA[ */ var comeetvar = {settings}; /* ]]> */</script></body></html>"
    )


_COMEET_SETTINGS_TOKEN = "0123ABCDEF0123ABCDEF0123ABCDEF01234567"


def test_an_employer_page_with_the_comeet_wordpress_settings_reads_its_positions() -> None:
    requested: list[str] = []
    page = _comeet_settings_page(
        f'{{"comeet_token":"{_COMEET_SETTINGS_TOKEN}","comeet_uid":"9Z.123",'
        '"comeet_color":"","comeet_css_url":""}'
    )
    fetcher = _comeet_embed_client(page, requested)

    assert fetcher.resolve(_EMPLOYER_CAREERS).platform == "comeet"
    read = fetcher.listings("Example Med", _EMPLOYER_CAREERS)

    assert (
        "https://www.comeet.co/careers-api/2.0/company/9Z.123/positions"
        f"?token={_COMEET_SETTINGS_TOKEN}&details=true"
    ) in requested
    assert [listing.title for listing in read.listings] == ["Clinical Data Manager"]


def test_comeet_wordpress_settings_without_a_token_name_no_comeet_board() -> None:
    page = _comeet_settings_page('{"comeet_uid":"9Z.123","comeet_color":""}')

    assert _comeet_embed_client(page, []).resolve(_EMPLOYER_CAREERS).platform != "comeet"


def test_robots_on_the_comeet_api_host_still_refuse_an_embedded_plugin_read() -> None:
    requested: list[str] = []
    fetcher = _comeet_embed_client(
        _comeet_plugin_page(_COMEET_PLUGIN_INIT),
        requested,
        robots="User-agent: *\nDisallow: /careers-api/\n",
    )

    read = fetcher.listings("Example Med", _EMPLOYER_CAREERS)

    assert not any(url.startswith(_COMEET_EMBED_API) for url in requested)
    assert read.skip_reasons.get("board_api_policy_refused") == 1


def test_himalayas_recovers_the_employer_when_the_api_serves_its_name_placeholder() -> None:
    """Himalayas serves companyName='name' when limit>1; the slug carries the real one."""

    class PlaceholderHimalayas(ScriptedPage):
        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            return {
                "totalCount": 1,
                "jobs": [
                    {
                        "title": "Data Engineer",
                        "companyName": "name",
                        "companySlug": "northwind-remote",
                        "applicationLink": "https://himalayas.app/companies/northwind-remote/jobs/x",
                        "locationRestrictions": ["Remote"],
                        "guid": "guid-1",
                    }
                ],
            }

    read = PlaceholderHimalayas().listings("", "https://himalayas.app/jobs")

    assert read is not None
    assert read.listings[0].employer == "Northwind Remote"


def test_a_jibe_job_states_its_posted_date_rather_than_its_reindexed_create_date() -> None:
    data = _icims_data(
        posted_date="2026-09-03T14:20:00+0000", create_date="2026-10-15T17:55:00+0000"
    )

    listing = source_listing(ICIMS_MAPPING, Posting(data, employer="Globex"))

    assert listing is not None
    posted = listing.facts.posted_date
    assert posted is not None
    assert posted.earliest.basis == "posted_date"
    assert posted.earliest.normalized_value == "2026-09-03T14:20:00+00:00"
    assert posted.earliest.timezone == "+00:00"
    assert posted.earliest.grade is PostedDateGrade.FIRST_POSTED
