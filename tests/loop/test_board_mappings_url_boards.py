"""Ashby, Workday, and Oracle declarations map a published record without losing a rule."""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import pytest

from clearscout.application.discovery.screening import (
    ScreeningCode,
    ScreeningDimension,
    screen_discovery,
)
from clearscout.application.discovery.source_reading import SourceReadOptions
from clearscout.domain.search_parameters import CountryNames, JobSearchParameters, Location
from clearscout.domain.staged_facts import PostedDateGrade
from clearscout.infrastructure.web_fetch.board_adapters import BoardApiFetcher
from clearscout.infrastructure.web_fetch.board_mappings.ashby import (
    ASHBY_MAPPING,
)
from clearscout.infrastructure.web_fetch.board_mappings.mapper import (
    Posting,
    posting_check,
    source_listing,
)
from clearscout.infrastructure.web_fetch.board_mappings.oracle import (
    ORACLE_DETAIL_MAPPING,
    ORACLE_LISTING_MAPPING,
)
from clearscout.infrastructure.web_fetch.board_mappings.workday import (
    WORKDAY_DETAIL_MAPPING,
    WORKDAY_LISTING_MAPPING,
    workday_country_from_path,
)
from clearscout.infrastructure.web_fetch.board_slugs import title_from_url_slug
from tests.support.doubles import ScriptedApi, published_listings
from tests.support.http import policy_client


def _ashby_job(**stated: Any) -> dict[str, Any]:
    return {
        "id": "ash-1",
        "title": "Clinical Data Analyst",
        "jobUrl": "https://jobs.ashbyhq.com/globex/ash-1",
        "location": "Northtown",
        "workplaceType": "hybrid",
        "employmentType": "FullTime",
        "descriptionPlain": "Support clinical data pipelines for hospital partners.",
        "descriptionHtml": "<p>Support clinical data pipelines for hospital partners.</p>",
        **stated,
    }


@pytest.mark.parametrize(
    ("same_posting", "protected_host"),
    [(True, "app.dover.com"), (False, "app.dover.com"), (True, "app.dover.com.evil.example")],
)
def test_a_protected_dover_uuid_is_matched_before_reading_its_company_from_detail(
    same_posting: bool,
    protected_host: str,
) -> None:
    tenant = "7c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f"
    protected_id = "9e8d7c6b-5a49-4382-a716-f5e4d3c2b1a0"
    other_id = "11111111-2222-4333-8444-555555555555"
    protected = f"https://{protected_host}/apply/Acme%20Health/{protected_id}"
    protected_matches = same_posting and protected_host == "app.dover.com"
    board = f"https://app.dover.io/dover/careers/{tenant}"
    jobs = f"https://app.dover.io/api/v1/careers-page/{tenant}/jobs"
    detail = "https://app.dover.io/api/v1/inbound/application-portal-job/"
    row_id = protected_id if same_posting else other_id
    requested: list[str] = []

    def answer(url: str) -> httpx.Response:
        requested.append(url)
        payload = (
            {"count": 1, "results": [{"id": row_id, "title": "Clinical Data Engineer"}]}
            if url == jobs
            else {
                "id": row_id,
                "client_id": tenant,
                "client_name": "Acme Health",
                "title": "Clinical Data Engineer",
                "user_provided_description": "<p>Build clinical data pipelines.</p>",
            }
        )
        return httpx.Response(200, request=httpx.Request("GET", url), json=payload)

    read = BoardApiFetcher(client=policy_client(answer)).listings(
        "Acme",
        board,
        options=SourceReadOptions(protected_posting_urls=frozenset({protected})),
    )

    assert requested == ([jobs] if protected_matches else [jobs, detail + row_id])
    assert read.detail.attempted == read.detail.read == int(not protected_matches)
    assert read.detail.failed == 0
    assert read.complete is True
    assert read.detail_coverage is not None
    assert set(read.detail_coverage.posting_urls) == {
        protected if protected_matches else f"https://app.dover.io/apply/Acme%20Health/{row_id}"
    }
    assert read.protected_posting_urls == ((protected,) if protected_matches else ())
    assert read.skip_reasons == ({"protected_posting": 1} if protected_matches else {})


# A remote place contradicts the structured value the same way for both
# consumers; a remote note in the body does not, since each consumer reads a
# different body field.
_ASHBY_REMOTE_PLACE = {"secondaryLocations": [{"location": "Remote"}]}


def test_a_contradicted_ashby_arrangement_stays_unresolved_in_a_board_listing() -> None:
    listing = source_listing(
        ASHBY_MAPPING, Posting(_ashby_job(**_ASHBY_REMOTE_PLACE), employer="Globex")
    )

    assert listing is not None
    assert listing.facts.arrangements.values == ()
    assert "hybrid" in listing.facts.arrangements.unresolved_values
    assert listing.facts.arrangements.complete is False


def test_a_contradicted_ashby_arrangement_stays_unresolved_in_a_single_posting_check() -> None:
    checked = posting_check(
        ASHBY_MAPPING,
        Posting(_ashby_job(**_ASHBY_REMOTE_PLACE)),
        note="Ashby published this.",
    )

    assert checked.facts.arrangements.values == ()
    assert "hybrid" in checked.facts.arrangements.unresolved_values
    assert checked.facts.arrangements.complete is False


def test_an_ashby_remote_offer_stated_only_in_the_html_body_is_seen_by_both_reads() -> None:
    job = _ashby_job(
        descriptionPlain="Support clinical data pipelines for hospital partners.",
        descriptionHtml="<p>This role is remote-friendly and can be performed from home.</p>",
    )

    listing = source_listing(ASHBY_MAPPING, Posting(job, employer="Globex"))
    checked = posting_check(ASHBY_MAPPING, Posting(job), note="Ashby published this.")

    assert listing is not None
    for facts in (listing.facts, checked.facts):
        assert facts.arrangements.values == ()
        assert "hybrid" in facts.arrangements.unresolved_values


def test_an_uncontradicted_ashby_arrangement_is_a_complete_fact() -> None:
    listing = source_listing(ASHBY_MAPPING, Posting(_ashby_job(), employer="Globex"))

    assert listing is not None
    assert listing.facts.arrangements.values == ("hybrid",)
    assert listing.facts.arrangements.unresolved_values == ()
    assert listing.facts.arrangements.complete is True
    assert listing.facts.arrangements.provenance == "ashby.arrangement"


def test_an_ashby_listing_states_its_fields_and_a_bounded_summary() -> None:
    job = _ashby_job(
        secondaryLocations=[{"location": "Southtown"}],
        compensation={"compensationTierSummary": "$100K - $120K"},
    )

    listing = source_listing(ASHBY_MAPPING, Posting(job, employer="Globex"))

    assert listing is not None
    assert (listing.employer, listing.title) == ("Globex", "Clinical Data Analyst")
    assert listing.url == "https://jobs.ashbyhq.com/globex/ash-1"
    assert listing.requisition_id == "ash-1"
    assert listing.location == "Northtown"
    assert listing.facts.locations.values == ("Northtown", "Southtown")
    assert listing.facts.employment_types.values == ("FullTime",)
    assert listing.facts.compensation.values == ("$100K - $120K",)
    assert listing.published_description == (
        "Clinical Data Analyst\n\nNorthtown\n\nFullTime\n\n"
        "Support clinical data pipelines for hospital partners."
    )


def test_an_ashby_salary_component_gives_the_summary_a_currency_and_period() -> None:
    job = _ashby_job(
        compensation={
            "compensationTierSummary": "$190K - $260K • 0.5% - 1% • Offers Bonus",
            "summaryComponents": [
                {
                    "compensationType": "Salary",
                    "interval": "1 YEAR",
                    "currencyCode": "USD",
                    "minValue": 190000,
                    "maxValue": 260000,
                },
                {
                    "compensationType": "EquityPercentage",
                    "interval": "NONE",
                    "currencyCode": None,
                    "minValue": 0.5,
                    "maxValue": 1,
                },
            ],
        },
    )

    listing = source_listing(ASHBY_MAPPING, Posting(job, employer="Globex"))

    assert listing is not None
    assert listing.facts.compensation.values == (
        "$190K - $260K • 0.5% - 1% • Offers Bonus (salary USD 190,000 - 260,000 per year)",
    )


def test_an_ashby_summary_without_a_salary_component_stays_as_published() -> None:
    job = _ashby_job(
        compensation={
            "compensationTierSummary": "$190K - $260K",
            "summaryComponents": [
                {"compensationType": "Salary", "interval": "NONE", "currencyCode": None}
            ],
        },
    )

    listing = source_listing(ASHBY_MAPPING, Posting(job, employer="Globex"))

    assert listing is not None
    assert listing.facts.compensation.values == ("$190K - $260K",)


def test_an_ashby_detail_check_prefers_the_html_body_over_the_plain_one() -> None:
    job = _ashby_job(
        descriptionPlain="thin summary",
        descriptionHtml="<p>Full body with every section the plain field omits.</p>",
    )

    checked = posting_check(ASHBY_MAPPING, Posting(job), note="Ashby published this.")

    assert "Full body with every section" in checked.text
    assert "thin summary" not in checked.text


def test_an_ashby_record_without_a_title_or_an_address_is_not_a_listing() -> None:
    assert source_listing(ASHBY_MAPPING, Posting(_ashby_job(jobUrl=""), employer="Globex")) is None
    assert source_listing(ASHBY_MAPPING, Posting(_ashby_job(title=" "), employer="Globex")) is None


# ---------------------------------------------------------------------------
# Workday
# ---------------------------------------------------------------------------


def _workday_job(**stated: Any) -> dict[str, Any]:
    return {
        "title": "Clinical Systems Manager",
        "externalPath": "/job/Northtown/Clinical-Systems-Manager_R-1234",
        "locationsText": "Northtown",
        "remoteType": {"descriptor": "Hybrid"},
        **stated,
    }


def test_a_workday_listing_states_its_fields_and_url() -> None:
    listing = source_listing(
        WORKDAY_LISTING_MAPPING,
        Posting(
            _workday_job(),
            employer="Globex",
            groups={"host": "globex.wd1.myworkdayjobs.com", "site": "External"},
        ),
    )

    assert listing is not None
    assert listing.title == "Clinical Systems Manager"
    assert listing.url == (
        "https://globex.wd1.myworkdayjobs.com/External"
        "/job/Northtown/Clinical-Systems-Manager_R-1234"
    )
    assert listing.location == "Northtown"
    assert listing.requisition_id == "R-1234"
    assert listing.facts.arrangements.values == ("hybrid",)
    assert listing.facts.arrangements.provenance == "workday.arrangement"


def test_a_workday_listing_defers_hybrid_beside_a_remote_listed_place() -> None:
    listing = source_listing(
        WORKDAY_LISTING_MAPPING,
        Posting(
            _workday_job(locationsText="USA-IL-Remote"),
            employer="Globex",
            groups={"host": "globex.wd1.myworkdayjobs.com", "site": "External"},
        ),
    )

    assert listing is not None
    assert listing.facts.arrangements.values == ()
    assert listing.facts.arrangements.unresolved_values
    assert listing.facts.arrangements.complete is False


def _workday_payload(**info: Any) -> dict[str, Any]:
    return {
        "jobPostingInfo": {
            "title": "Clinical Systems Manager (CSM)",
            "jobDescription": "<p>You will lead the Clinical Systems Manager (CSM) team.</p>",
            "location": "Northtown",
            "country": {"alpha2Code": "US"},
            "timeType": "Full time",
            **info,
        },
    }


def test_a_workday_detail_check_reads_an_alias_the_posting_names_itself_by() -> None:
    payload = _workday_payload()

    checked = posting_check(
        WORKDAY_DETAIL_MAPPING,
        Posting(payload, groups={"remainder": "Northtown/Clinical-Systems-Manager-CSM_R-1234"}),
        note="Workday published this.",
    )

    assert checked.facts.role_aliases.values == ("Clinical Systems Manager",)
    assert checked.facts.role_aliases.complete is True


def test_a_workday_detail_check_finds_no_alias_the_identity_does_not_name() -> None:
    payload = _workday_payload(
        title="Clinical Systems Manager",
        jobDescription="<p>Coordinate with the Client Success Manager (CSM) team.</p>",
    )

    checked = posting_check(
        WORKDAY_DETAIL_MAPPING,
        Posting(payload, groups={"remainder": "Northtown/Clinical-Systems-Manager_R-1234"}),
        note="Workday published this.",
    )

    assert checked.facts.role_aliases.values == ()


def test_a_workday_detail_check_reads_a_body_phrase_arrangement_with_its_own_provenance() -> None:
    payload = _workday_payload(jobDescription="<p>This is a fully-remote position.</p>")

    checked = posting_check(
        WORKDAY_DETAIL_MAPPING,
        Posting(payload, groups={"remainder": "Northtown/Clinical-Systems-Manager_R-1234"}),
        note="Workday published this.",
    )

    assert checked.facts.arrangements.values == ("remote",)
    assert checked.facts.arrangements.provenance == "workday.body_phrase.arrangement"


@pytest.mark.parametrize(
    "places",
    [
        {"location": "USA-IL-Remote"},
        {"secondaryLocations": [{"location": "USA-TX-Remote"}]},
        {"additionalLocations": ["USA-MA-Remote"]},
    ],
)
@pytest.mark.parametrize(
    "stated",
    [
        {"remoteType": {"descriptor": "Hybrid"}},
        {"jobDescription": "<p>Own the Initech ledger roadmap.</p><p>#LI-Hybrid</p>"},
    ],
)
def test_a_workday_detail_check_defers_hybrid_beside_any_remote_listed_place(
    places: dict[str, Any], stated: dict[str, Any]
) -> None:
    checked = posting_check(
        WORKDAY_DETAIL_MAPPING,
        Posting(
            _workday_payload(**places, **stated),
            groups={"remainder": "Northtown/Ledger-Systems-Manager_R-1234"},
        ),
        note="Workday published this.",
    )

    assert checked.facts.arrangements.values == ()
    assert checked.facts.arrangements.unresolved_values


def test_a_workday_detail_check_states_every_location_and_the_heading_text() -> None:
    payload = _workday_payload(secondaryLocations=[{"location": "Southtown"}])

    checked = posting_check(
        WORKDAY_DETAIL_MAPPING,
        Posting(payload, groups={"remainder": "Northtown/Clinical-Systems-Manager_R-1234"}),
        note="Workday published this.",
    )

    assert checked.facts.locations.values == ("Northtown", "Southtown", "United States")
    assert checked.text.startswith(
        "Clinical Systems Manager (CSM)\n\nNorthtown, Southtown, United States\n\nFull time"
    )


# ---------------------------------------------------------------------------
# Oracle
# ---------------------------------------------------------------------------


def _oracle_job(**stated: Any) -> dict[str, Any]:
    return {
        "Id": "300012345",
        "Title": "Clinical Research Coordinator",
        "PrimaryLocation": "Northtown",
        "ShortDescriptionStr": "Coordinate multi-site clinical research.",
        "ExternalQualificationsStr": "Bachelor's degree required.",
        "ExternalResponsibilitiesStr": "Manage site visits and IRB submissions.",
        "WorkplaceTypeCode": "HYBRID",
        **stated,
    }


def test_an_oracle_listing_states_its_fields_and_leaves_the_body_to_the_detail_read() -> None:
    listing = source_listing(
        ORACLE_LISTING_MAPPING,
        Posting(
            _oracle_job(),
            employer="Globex",
            groups={"host": "globex.oraclecloud.com", "locale": "en", "site": "CX_1"},
        ),
    )

    assert listing is not None
    assert listing.title == "Clinical Research Coordinator"
    assert listing.url == (
        "https://globex.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/300012345"
    )
    assert listing.location == "Northtown"
    assert listing.requisition_id == "300012345"
    assert listing.published_description == ""


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        pytest.param("REMOTE", "remote", id="remote"),
        pytest.param("ONSITE", "onsite", id="onsite"),
        pytest.param("HYBRID", "hybrid", id="hybrid"),
    ],
)
def test_an_oracle_arrangement_reads_a_recognized_workplace_type_code(
    code: str, expected: str
) -> None:
    listing = source_listing(
        ORACLE_LISTING_MAPPING,
        Posting(
            _oracle_job(WorkplaceTypeCode=code),
            employer="Globex",
            groups={"host": "globex.oraclecloud.com", "locale": "en", "site": "CX_1"},
        ),
    )

    assert listing is not None
    assert listing.facts.arrangements.values == (expected,)
    assert listing.facts.arrangements.complete is True


@pytest.mark.parametrize("code", ["HYBRID", "ONSITE"])
def test_an_oracle_listing_attendance_code_is_unresolved_beside_a_remote_place(code: str) -> None:
    listing = source_listing(
        ORACLE_LISTING_MAPPING,
        Posting(
            _oracle_job(WorkplaceTypeCode=code, PrimaryLocation="USA-IL-Remote"),
            employer="Globex",
            groups={"host": "globex.oraclecloud.com", "locale": "en", "site": "CX_1"},
        ),
    )

    assert listing is not None
    assert listing.facts.arrangements.values == ()
    assert listing.facts.arrangements.unresolved_values


def test_an_oracle_detail_attendance_code_is_unresolved_beside_a_remote_offer() -> None:
    job = _oracle_job(
        WorkplaceTypeCode="ONSITE", ExternalDescriptionStr="You may work remotely from home."
    )

    checked = posting_check(ORACLE_DETAIL_MAPPING, Posting(job), note="Oracle published this.")

    assert checked.facts.arrangements.values == ()
    assert "onsite" in checked.facts.arrangements.unresolved_values


def test_an_oracle_arrangement_leaves_an_unrecognized_code_unresolved() -> None:
    checked = posting_check(
        ORACLE_DETAIL_MAPPING,
        Posting(_oracle_job(WorkplaceTypeCode="MOBILE")),
        note="Oracle published this.",
    )

    assert checked.facts.arrangements.values == ()
    assert "unrecognized WorkplaceTypeCode: MOBILE" in checked.facts.arrangements.unresolved_values
    assert checked.facts.arrangements.complete is False


def test_an_oracle_detail_check_states_its_heading_and_full_description() -> None:
    job = _oracle_job(
        ExternalDescriptionStr="Coordinate multi-site clinical research.",
        WorkplaceType="Hybrid",
    )

    checked = posting_check(ORACLE_DETAIL_MAPPING, Posting(job), note="Oracle published this.")

    assert checked.text == (
        "Clinical Research Coordinator\n\nNorthtown\n\nHybrid\n\n"
        "Coordinate multi-site clinical research.\n\n"
        "Manage site visits and IRB submissions.\n\nBachelor's degree required."
    )


def test_an_oracle_detail_check_states_the_published_legal_employer() -> None:
    job = _oracle_job(LegalEmployer="Globex Health Inc.", Organization="Research Informatics")

    checked = posting_check(ORACLE_DETAIL_MAPPING, Posting(job), note="Oracle published this.")

    assert checked.employer == "Globex Health Inc."


def test_an_oracle_posting_without_a_company_uses_its_registered_board_name() -> None:
    posting = Posting(
        _oracle_job(),
        employer="Acme",
        employer_is_authoritative=True,
        groups={"host": "acme.oraclecloud.com", "locale": "en", "site": "CX_9"},
    )
    result = source_listing(ORACLE_LISTING_MAPPING, posting)
    assert result is not None
    assert result.employer == "Acme"
    assert result.facts.employer.values == ("Acme",)
    assert result.facts.employer.provenance == "source.registered_employer"
    assert result.facts.employer.complete is True


@pytest.mark.parametrize("detail", [False, True], ids=["listing", "detail"])
@pytest.mark.parametrize("authoritative", [False, True], ids=["hint", "authoritative"])
@pytest.mark.parametrize(
    ("company_fields", "company"),
    [
        ({}, None),
        ({"LegalEmployer": None}, None),
        ({"LegalEmployer": ""}, None),
        ({"LegalEmployer": "   "}, None),
        ({"LegalEmployer": 1234}, None),
        ({"LegalEmployer": True}, None),
        ({"LegalEmployer": {"Name": "Globex Health Inc."}}, None),
        ({"LegalEmployer": ["Globex Health Inc."]}, None),
        ({"LegalEmployer": "  Globex Health Inc.  "}, "Globex Health Inc."),
    ],
    ids=["missing", "null", "empty", "blank", "numeric", "boolean", "nested", "list", "published"],
)
def test_oracle_employer_prefers_stated_company_over_registered_board(
    detail: bool, authoritative: bool, company_fields: dict[str, Any], company: str | None
) -> None:
    job = _oracle_job(
        BusinessUnit=1234,
        Organization=5678,
        ExternalDescriptionStr="Work for Globex Health Inc.",
        **company_fields,
    )
    posting = Posting(
        job,
        employer="Globex",
        employer_is_authoritative=authoritative,
        groups={"host": "globex.oraclecloud.com", "locale": "en", "site": "CX_1"},
    )
    result = (
        posting_check(ORACLE_DETAIL_MAPPING, posting, note="Oracle published this.")
        if detail
        else source_listing(ORACLE_LISTING_MAPPING, posting)
    )
    assert result is not None
    expected = company or "Globex"
    assert result.employer == expected
    assert result.title == "Clinical Research Coordinator"
    assert result.location == "Northtown"
    assert posting.employer == "Globex"


@pytest.mark.parametrize(
    "groups",
    [{}, {"host": "globex.oraclecloud.com"}, {"site": "CX_1"}, {"host": "", "site": "CX_1"}],
)
def test_oracle_employer_does_not_invent_missing_tenant_identifiers(
    groups: dict[str, str],
) -> None:
    result = posting_check(
        ORACLE_DETAIL_MAPPING,
        Posting(_oracle_job(), employer="Globex", groups=groups, employer_is_authoritative=True),
        note="Oracle published this.",
    )

    assert result.employer == "Globex"
    assert result.facts.employer.provenance == "source.registered_employer"


def test_an_oracle_record_without_a_requisition_id_states_no_url() -> None:
    listing = source_listing(
        ORACLE_LISTING_MAPPING,
        Posting(
            _oracle_job(Id=""),
            employer="Globex",
            groups={"host": "globex.oraclecloud.com", "locale": "en", "site": "CX_1"},
        ),
    )

    assert listing is None


def test_a_workday_listing_keeps_a_conflicting_linkedin_tag_unresolved() -> None:
    job = {
        "title": "Data Product Manager",
        "externalPath": "/job/Remote/Data-Product-Manager_R-123",
        "jobDescription": "#LI-Onsite\nThis role is fully remote within Canada.",
    }
    posting = Posting(
        job, employer="Globex", groups={"host": "globex.wd1.myworkdayjobs.com", "site": "Careers"}
    )

    listing = source_listing(WORKDAY_LISTING_MAPPING, posting)

    assert listing is not None
    arrangement = listing.facts.arrangements
    assert arrangement.values == ()
    assert arrangement.complete is False
    assert "#LI-Onsite" in arrangement.unresolved_values[0]


def test_a_workday_bullet_that_is_not_a_requisition_number_is_not_stored_as_one() -> None:
    """`bulletFields` is display configuration, so a tenant may put a place in it.

    A tenant can return the location there and leave `locationsText` empty, so a
    bullet is not taken as the requisition number on its position alone.
    """

    class ScriptedWorkday(BoardApiFetcher):
        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            return {
                "total": 1,
                "jobPostings": [
                    {
                        "title": "General Ledger Senior Accountant",
                        "externalPath": (
                            "/job/Warsaw---Poland/General-Ledger-Senior-Accountant_R10601"
                        ),
                        "locationsText": "",
                        "bulletFields": ["Warsaw - Poland"],
                    }
                ],
            }

    read = ScriptedWorkday().listings("Example", "https://example.wd1.myworkdayjobs.com/Careers")

    assert read is not None
    assert [item.requisition_id for item in read.listings] == ["R10601"]


def test_workday_structured_paths_supply_country_evidence_without_guessing_regions() -> None:
    class ScriptedWorkday(BoardApiFetcher):
        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            return {
                "total": 4,
                "jobPostings": [
                    {
                        "title": "Data Lead",
                        "externalPath": "/job/IN---AHMEDABAD/Data-Lead_R-1",
                        "locationsText": "",
                    },
                    {
                        "title": "Data Lead",
                        "externalPath": "/job/Islamabad-PK/Data-Lead_R-2",
                        "locationsText": "",
                    },
                    {
                        "title": "Data Lead",
                        "externalPath": "/job/London-UK/Data-Lead_R-3",
                        "locationsText": "3 Locations",
                    },
                    {
                        "title": "Data Lead",
                        "externalPath": "/job/San-Francisco-CA/Data-Lead_R-4",
                        "locationsText": "",
                    },
                ],
            }

    read = ScriptedWorkday().listings("Example", "https://example.wd1.myworkdayjobs.com/Careers")

    assert read is not None
    india, pakistan, multi_location, california = read.listings
    assert india.facts.locations.values == ("India",)
    assert india.facts.locations.complete is True
    assert pakistan.facts.locations.values == ("Pakistan",)
    assert multi_location.facts.locations.values == ("United Kingdom",)
    assert multi_location.facts.locations.unresolved_values == ("3 Locations",)
    assert multi_location.facts.locations.complete is False
    assert california.facts.locations.values == ()
    assert california.facts.locations.complete is False


@pytest.mark.parametrize(
    ("path", "country"),
    [
        ("/External/job/USA---Atlanta-GA/Lead-Product-Manager_R0012345", "United States"),
        ("/External/job/CAN---Toronto-ON/Data-Lead_R-2", "Canada"),
        ("/External/job/GBR---London/Data-Lead_R-3", "United Kingdom"),
        ("/External/job/IN---AHMEDABAD/Data-Lead_R-1", "India"),
        ("/External/job/Islamabad-PK/Data-Lead_R-4", "Pakistan"),
        ("/External/job/San-Francisco-CA/Data-Lead_R-5", None),
        ("/External/job/Nashville-TN/Data-Lead_R-6", None),
        ("/External/job/Jamaica-Plain-MA/Data-Lead_R-7", None),
        ("/External/job/Philadelphia-PA/Data-Lead_R-8", None),
    ],
)
def test_a_workday_path_country_prefix_outranks_a_state_that_spells_an_iso_code(
    path: str, country: str | None
) -> None:
    assert workday_country_from_path(path) == country


@pytest.mark.parametrize(
    ("segment", "country"),
    [
        ("GA---Atlanta", "United States"),
        ("PA---Reading", "United States"),
        ("DE---Wilmington", "United States"),
        ("IN---Indianapolis", "United States"),
        ("VA---Richmond", "United States"),
        ("ON---Toronto", "Canada"),
        ("VA---Chesterfield", None),
        ("CA---Fictionville", None),
        ("FR---Paris", "France"),
        ("DE---Berlin", "Germany"),
        ("CA---Toronto", "Canada"),
        ("IN---Chennai", "India"),
        ("IN---Hyderabad", "India"),
        ("CA---Vancouver", "Canada"),
        ("IN---Bengaluru", "India"),
        # Richmond is indexed both in Canada and under California.
        ("CA---Richmond", None),
        # The city index spells these Bengaluru and Frankfurt am Main.
        ("IN---Bangalore", None),
        ("DE---Frankfurt", None),
    ],
)
def test_a_workday_path_state_code_prefix_names_the_state_s_country_not_its_namesake(
    segment: str, country: str | None
) -> None:
    assert workday_country_from_path(f"/External/job/{segment}/Data-Lead_R-9") == country


@pytest.mark.parametrize(
    "suffix", ["", "/apply", "/apply/", "/apply/applyManually", "/apply/autofillWithResume", "?a=1"]
)
def test_a_workday_title_slug_is_the_posting_segment_under_any_action(suffix: str) -> None:
    url = f"https://acme.wd5.myworkdayjobs.com/en-US/External/job/Remote/Staff-Engineer_R12345{suffix}"

    assert title_from_url_slug(url) == "Staff Engineer"


def test_a_workday_title_slug_inside_a_redirect_is_not_read() -> None:
    url = "https://acme.wd5.myworkdayjobs.com/en-US/External/job/Remote/Staff-Engineer_R12345"

    assert title_from_url_slug(f"https://click.example.net/r/{url.split('://', 1)[1]}") is None


def _ashby_place(city: str, region: str, country: str) -> dict[str, Any]:
    """Build one Ashby secondary location the way its posting API states them."""
    return {
        "location": city,
        "address": {
            "postalAddress": {
                "addressLocality": city,
                "addressRegion": region,
                "addressCountry": country,
            }
        },
    }


def test_ashby_listings_are_read_from_its_posting_api() -> None:
    """Ashby states a secondary location as an object, and pay only when asked for it."""
    fetcher = ScriptedApi(
        {
            "jobs": [
                {
                    "id": "9f8e",
                    "title": "Principal Data Engineer",
                    "jobUrl": "https://jobs.ashbyhq.com/acme/9f8e",
                    "location": "Harborview",
                    "address": {
                        "postalAddress": {
                            "addressLocality": "Harborview",
                            "addressRegion": "Eastmarch",
                            "addressCountry": "Freedonia",
                        }
                    },
                    "secondaryLocations": [
                        _ashby_place("Lakeside", "Westmarch", "Freedonia"),
                    ],
                    "workplaceType": "Hybrid",
                    "employmentType": "FullTime",
                    "compensation": {"compensationTierSummary": "$140,000-$170,000 CAD"},
                    "descriptionPlain": "Build developer tooling  for the team.\n\nRemote.",
                },
                {"id": "none", "title": "No link", "location": "Harborview"},
            ]
        }
    )

    listings = published_listings(fetcher, "Acme", "https://jobs.ashbyhq.com/acme")

    assert fetcher.requested == [
        "https://api.ashbyhq.com/posting-api/job-board/acme?includeCompensation=true"
    ]
    assert listings is not None
    assert [item.title for item in listings] == ["Principal Data Engineer"]
    assert listings[0].url == "https://jobs.ashbyhq.com/acme/9f8e"
    assert listings[0].location == "Harborview"
    assert listings[0].requisition_id == "9f8e"
    assert listings[0].facts.locations.values == ("Harborview", "Lakeside", "Freedonia")
    # The body's own "Remote." sentence contradicts the Hybrid workplace type.
    assert listings[0].facts.arrangements.values == ()
    assert listings[0].facts.arrangements.unresolved_values[0] == "Hybrid"
    assert listings[0].facts.employment_types.values == ("FullTime",)
    assert listings[0].facts.compensation.values == ("$140,000-$170,000 CAD",)
    assert listings[0].summary == "Build developer tooling for the team. Remote."


def test_an_ashby_org_with_a_space_is_read_under_its_whole_name() -> None:
    fetcher = ScriptedApi(
        {
            "jobs": [
                {
                    "id": "5e6f",
                    "title": "Staff Engineer",
                    "jobUrl": "https://jobs.ashbyhq.com/Acme%20Labs/5e6f",
                    "location": "Harborview",
                }
            ]
        }
    )

    listings = published_listings(fetcher, "Acme Labs", "https://jobs.ashbyhq.com/Acme%20Labs")

    assert fetcher.requested == [
        "https://api.ashbyhq.com/posting-api/job-board/Acme%20Labs?includeCompensation=true"
    ]
    assert listings is not None
    assert [item.title for item in listings] == ["Staff Engineer"]


def test_an_ashby_department_in_the_board_address_keeps_only_that_departments_jobs() -> None:
    """A staffing tenant hosts several client companies, each named as a department."""
    fetcher = ScriptedApi(
        {
            "jobs": [
                _ashby_job(
                    id=job_id,
                    title=title,
                    department=department,
                    team=department,
                    jobUrl=f"https://jobs.ashbyhq.com/acme-talent/{job_id}",
                )
                for job_id, title, department in (
                    ("c6fc", "Senior Account Executive", "Globex Health"),
                    ("e7a3", "Clinical Data Analyst", "Initech & Co"),
                    ("a1b2", "Data Engineer", "Globex Health"),
                )
            ]
        }
    )

    read = fetcher.listings(
        "Globex Health", "https://jobs.ashbyhq.com/acme-talent?department=Globex%20Health"
    )

    assert fetcher.requested == [
        "https://api.ashbyhq.com/posting-api/job-board/acme-talent?includeCompensation=true"
    ]
    assert [listing.requisition_id for listing in read.listings] == ["c6fc", "a1b2"]
    assert read.complete is True


def test_ashby_publishes_no_pay_when_the_posting_carries_none() -> None:
    """A posting without a compensation object states no pay, not an empty one."""
    fetcher = ScriptedApi(
        {
            "jobs": [
                {
                    "id": "7a1b",
                    "title": "Staff Engineer",
                    "jobUrl": "https://jobs.ashbyhq.com/acme/7a1b",
                    "location": "Harborview",
                    "compensation": None,
                }
            ]
        }
    )

    listings = published_listings(fetcher, "Acme", "https://jobs.ashbyhq.com/acme")

    assert listings is not None
    assert listings[0].facts.compensation.values == ()
    assert listings[0].facts.compensation.complete is False


def test_ashby_two_country_posting_is_not_refused_where_the_person_may_work() -> None:
    """Adding each stated country keeps a market the person can take out of refusal."""
    fetcher = ScriptedApi(
        {
            "jobs": [
                {
                    "id": "3c4d",
                    "title": "Data Platform Lead",
                    "jobUrl": "https://jobs.ashbyhq.com/acme/3c4d",
                    "location": "Harborview",
                    "address": {
                        "postalAddress": {
                            "addressLocality": "Harborview",
                            "addressRegion": "Eastmarch",
                            "addressCountry": "Freedonia",
                        }
                    },
                    "secondaryLocations": [
                        _ashby_place("Vantage", "Sudmark", "Ruritania"),
                    ],
                }
            ]
        }
    )
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="Ruritania")],
        countries=[CountryNames(country="Freedonia"), CountryNames(country="Ruritania")],
    )

    listings = published_listings(fetcher, "Acme", "https://jobs.ashbyhq.com/acme")

    assert listings is not None
    assert listings[0].facts.locations.values == (
        "Harborview",
        "Vantage",
        "Freedonia",
        "Ruritania",
    )
    screening = screen_discovery(
        "Acme", "Data Platform Lead", listings[0].location, parameters, [], listings[0].facts
    )
    location = next(
        finding
        for finding in screening.findings
        if finding.dimension is ScreeningDimension.LOCATION
    )
    assert location.code is ScreeningCode.LOCATION_MATCH


def test_an_oracle_detail_check_states_its_external_posted_start_date() -> None:
    job = _oracle_job(ExternalPostedStartDate="2026-09-15T04:00:00+00:00")

    checked = posting_check(ORACLE_DETAIL_MAPPING, Posting(job), note="Oracle published this.")

    posted = checked.facts.posted_date
    assert posted is not None
    assert posted.earliest.basis == "ExternalPostedStartDate"
    assert posted.day == date(2026, 9, 15)
    assert posted.earliest.grade is PostedDateGrade.FIRST_POSTED
