"""Feed-board declarations map a published record without losing a platform's own rule."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from clearscout.application.discovery.screening import (
    ScreeningCode,
    screen_discovery,
    supports_bulk_staging,
)
from clearscout.application.discovery.source_reading import SourceReadOptions
from clearscout.domain import SourceReadOutcome
from clearscout.domain.search_parameters import JobSearchParameters, Location, Seniority
from clearscout.domain.staged_facts import ListingIdentityTrust, PostedDateGrade
from clearscout.infrastructure.web_fetch.board_adapters import (
    BOARD_ADAPTER_REGISTRY,
    STRUCTURED_PAGE_CURSOR_REASONS,
    BoardApiFetcher,
)
from clearscout.infrastructure.web_fetch.board_mappings.adp import ADP_MAPPING
from clearscout.infrastructure.web_fetch.board_mappings.bamboohr import BAMBOOHR_MAPPING
from clearscout.infrastructure.web_fetch.board_mappings.breezy import BREEZY_MAPPING
from clearscout.infrastructure.web_fetch.board_mappings.dayforce import DAYFORCE_MAPPING
from clearscout.infrastructure.web_fetch.board_mappings.dover import DOVER_MAPPING
from clearscout.infrastructure.web_fetch.board_mappings.gem import GEM_MAPPING
from clearscout.infrastructure.web_fetch.board_mappings.mapper import (
    Posting,
    posting_check,
    source_listing,
)
from clearscout.infrastructure.web_fetch.board_mappings.paylocity import PAYLOCITY_MAPPING
from clearscout.infrastructure.web_fetch.board_mappings.pinpoint import PINPOINT_MAPPING
from clearscout.infrastructure.web_fetch.board_mappings.recruitee import RECRUITEE_MAPPING
from clearscout.infrastructure.web_fetch.board_mappings.rippling import RIPPLING_MAPPING
from clearscout.infrastructure.web_fetch.board_mappings.smartrecruiters import (
    SMARTRECRUITERS_MAPPING,
)
from clearscout.infrastructure.web_fetch.board_mappings.workable import WORKABLE_MAPPING
from clearscout.infrastructure.web_fetch.ingest import strip_tags
from clearscout.infrastructure.web_fetch.platforms import adp
from clearscout.infrastructure.web_fetch.policy import (
    FetchPolicyError,
    HostHeldError,
    PoliteHttpClient,
    RequestDeadlineExceeded,
    RobotsDeniedError,
)
from tests.support.http import paylocity_board_page, paylocity_page_job, policy_client
from tests.support.scenarios import titles


def _smartrecruiters_job(**stated: Any) -> dict[str, Any]:
    return {
        "id": "job-1",
        "name": "Clinical Systems Analyst",
        "ref": "",
        "refNumber": "REQ-100",
        "location": {
            "city": "Northtown",
            "region": "NT",
            "country": "us",
            "fullLocation": "Northtown, NT, United States",
        },
        "company": {"identifier": "GLOBEX1", "name": "Globex"},
        "typeOfEmployment": {"id": "permanent", "label": "Full-time"},
        "jobAd": {"sections": {}},
        **stated,
    }


@pytest.mark.parametrize(
    ("remote", "hybrid", "expected"),
    [
        pytest.param(True, False, ("remote",), id="remote"),
        pytest.param(False, True, ("hybrid",), id="hybrid"),
        pytest.param(False, False, ("onsite",), id="onsite-both-false"),
        pytest.param(None, None, (), id="unstated"),
        pytest.param(True, True, ("remote",), id="remote-outranks-hybrid"),
    ],
)
def test_a_smartrecruiters_arrangement_reads_the_location_booleans(
    remote: bool | None, hybrid: bool | None, expected: tuple[str, ...]
) -> None:
    job = _smartrecruiters_job(location={"city": "Northtown", "remote": remote, "hybrid": hybrid})

    listing = source_listing(
        SMARTRECRUITERS_MAPPING, Posting(job, employer="Globex", groups={"org": "globex"})
    )

    assert listing is not None
    assert listing.facts.arrangements.values == expected


@pytest.mark.parametrize(
    ("job", "expected"),
    [
        pytest.param(
            {"location": {"fullLocation": "USA-IL-Remote", "remote": False, "hybrid": True}},
            "hybrid",
            id="hybrid-beside-a-remote-place",
        ),
        pytest.param(
            {"location": {"fullLocation": "USA-IL-Remote", "remote": False, "hybrid": False}},
            "onsite",
            id="onsite-beside-a-remote-place",
        ),
        pytest.param(
            {
                "location": {"city": "Northtown", "remote": False, "hybrid": True},
                "jobAd": {
                    "sections": {"jobDescription": {"text": "You may work remotely from home."}}
                },
            },
            "hybrid",
            id="hybrid-beside-a-remote-offer",
        ),
    ],
)
def test_a_smartrecruiters_attendance_flag_is_unresolved_beside_remote(
    job: dict[str, Any], expected: str
) -> None:
    listing = source_listing(
        SMARTRECRUITERS_MAPPING,
        Posting(_smartrecruiters_job(**job), employer="Globex", groups={"org": "globex"}),
    )

    assert listing is not None
    assert listing.facts.arrangements.values == ()
    assert expected in listing.facts.arrangements.unresolved_values


def test_a_smartrecruiters_listing_states_its_fields_and_falls_back_to_the_board_url() -> None:
    job = _smartrecruiters_job(ref="")

    listing = source_listing(
        SMARTRECRUITERS_MAPPING, Posting(job, employer="Globex", groups={"org": "globex"})
    )

    assert listing is not None
    assert (listing.employer, listing.title) == ("Globex", "Clinical Systems Analyst")
    assert listing.url == "https://jobs.smartrecruiters.com/globex/job-1"
    assert listing.location == "Northtown, NT, United States"
    assert listing.requisition_id == "REQ-100"
    assert listing.facts.employment_types.values == ("Full-time",)


def test_a_smartrecruiters_listing_prefers_the_publisher_reference_link() -> None:
    job = _smartrecruiters_job(ref="https://jobs.smartrecruiters.com/globex/vanity-slug")

    listing = source_listing(
        SMARTRECRUITERS_MAPPING, Posting(job, employer="Globex", groups={"org": "globex"})
    )

    assert listing is not None
    assert listing.url == "https://jobs.smartrecruiters.com/globex/vanity-slug"


def test_a_smartrecruiters_detail_record_states_every_jobad_section_as_its_body() -> None:
    job = _smartrecruiters_job(
        jobAd={
            "sections": {
                "companyDescription": {"text": "Globex builds clinical systems."},
                "jobDescription": {"text": "Configure hospital systems."},
                "qualifications": {"text": "Five years of health IT."},
                "additionalInformation": {"text": "Globex is an equal opportunity employer."},
            }
        }
    )

    listing = source_listing(
        SMARTRECRUITERS_MAPPING, Posting(job, employer="Globex", groups={"org": "globex"})
    )

    assert listing is not None
    assert listing.published_description == (
        "Clinical Systems Analyst\n\nNorthtown, NT, United States\n\n"
        "Configure hospital systems.\n\nFive years of health IT.\n\n"
        "Globex is an equal opportunity employer.\n\nGlobex builds clinical systems."
    )
    assert listing.summary.startswith("Configure hospital systems.")


def test_a_smartrecruiters_list_record_without_a_jobad_states_no_body() -> None:
    job = _smartrecruiters_job()

    listing = source_listing(
        SMARTRECRUITERS_MAPPING, Posting(job, employer="Globex", groups={"org": "globex"})
    )

    assert listing is not None
    assert listing.published_description == ""


def _workable_location(**stated: Any) -> dict[str, Any]:
    return {
        "country": "United States",
        "countryCode": "US",
        "city": "Northtown",
        "region": "NT",
        "hidden": False,
        **stated,
    }


def _workable_job(**stated: Any) -> dict[str, Any]:
    return {
        "title": "Clinical Systems Analyst",
        "shortcode": "AB12CD34EF",
        "code": "HIR-0001",
        "employment_type": "",
        "telecommuting": False,
        "department": "Product",
        "url": "https://apply.workable.com/j/AB12CD34EF",
        "shortlink": "https://apply.workable.com/j/AB12CD34EF",
        "application_url": "https://apply.workable.com/j/AB12CD34EF/apply",
        "published_on": "2026-09-25",
        "created_at": "2026-09-25",
        "country": "United States",
        "city": "Northtown",
        "state": "NT",
        "education": "",
        "experience": "",
        "function": "",
        "industry": "",
        "locations": [_workable_location()],
        "description": (
            "<p><strong>About the Role</strong></p>"
            "<p>Configure clinical systems for hospital teams.</p>"
            "<ul><li>Validate builds</li></ul>"
        ),
        **stated,
    }


def test_a_workable_listing_states_its_fields_and_body() -> None:
    listing = source_listing(WORKABLE_MAPPING, Posting(_workable_job(), employer="Globex"))

    assert listing is not None
    assert listing.title == "Clinical Systems Analyst"
    assert listing.url == "https://apply.workable.com/j/AB12CD34EF"
    assert listing.requisition_id == "AB12CD34EF"
    assert listing.location == "Northtown, NT, United States"
    assert listing.facts.locations.values == ("Northtown, NT, United States",)
    assert listing.published_description == (
        "About the Role\n\nConfigure clinical systems for hospital teams.\n\n- Validate builds"
    )
    assert listing.summary.startswith("About the Role")


def test_a_workable_listing_falls_back_to_the_application_url() -> None:
    job = _workable_job(url="", application_url="https://globex.example/apply/abc123")

    listing = source_listing(WORKABLE_MAPPING, Posting(job, employer="Globex"))

    assert listing is not None
    assert listing.url == "https://globex.example/apply/abc123"


@pytest.mark.parametrize(
    ("locations", "expected"),
    [
        pytest.param(
            [_workable_location(city="", region=None)],
            ("United States",),
            id="country-only",
        ),
        pytest.param(
            [_workable_location(), _workable_location(city="Southtown", region="ST")],
            ("Northtown, NT, United States", "Southtown, ST, United States"),
            id="several-places",
        ),
        pytest.param(
            [_workable_location(), _workable_location(city="Eastport", region="EP", hidden=True)],
            ("Northtown, NT, United States",),
            id="hidden-entry-dropped",
        ),
        pytest.param([_workable_location(hidden=True)], ("United States",), id="all-hidden"),
        pytest.param(
            [
                _workable_location(hidden=True),
                _workable_location(city="Southtown", region="ST", hidden=True),
            ],
            ("United States",),
            id="all-hidden-one-country",
        ),
        pytest.param(
            [
                _workable_location(hidden=True),
                _workable_location(
                    country="Canada", countryCode="CA", city="Lakeview", region="LV", hidden=True
                ),
            ],
            ("United States", "Canada"),
            id="all-hidden-two-countries",
        ),
    ],
)
def test_a_workable_listing_states_each_visible_location_entry(
    locations: list[dict[str, Any]], expected: tuple[str, ...]
) -> None:
    job = _workable_job(locations=locations, city="Westvale", state="WV")

    listing = source_listing(WORKABLE_MAPPING, Posting(job, employer="Globex"))

    assert listing is not None
    assert listing.facts.locations.values == expected
    assert listing.location == (expected[0] if expected else "")


@pytest.mark.parametrize(
    ("country", "expected"),
    [("United States", ("United States",)), ("", ())],
    ids=["top-level-country", "no-country"],
)
def test_hidden_entries_naming_no_country_fall_back_to_the_top_level_country_alone(
    country: str, expected: tuple[str, ...]
) -> None:
    hidden = _workable_location(country="", countryCode="", hidden=True)
    job = _workable_job(locations=[hidden], city="Westvale", state="WV", country=country)

    listing = source_listing(WORKABLE_MAPPING, Posting(job, employer="Globex"))

    assert listing is not None
    assert listing.facts.locations.values == expected


@pytest.mark.parametrize("locations", [[], None], ids=["empty", "absent"])
def test_a_workable_listing_without_location_entries_states_its_top_level_place(
    locations: list[dict[str, Any]] | None,
) -> None:
    job = _workable_job(city="Westvale", state="WV")
    if locations is None:
        del job["locations"]
    else:
        job["locations"] = locations

    listing = source_listing(WORKABLE_MAPPING, Posting(job, employer="Globex"))

    assert listing is not None
    assert listing.location == "Westvale, WV, United States"
    assert listing.facts.locations.values == ("Westvale, WV, United States",)


@pytest.mark.parametrize(
    ("telecommuting", "expected"),
    [(True, ("remote",)), (False, ())],
    ids=["telecommuting", "not-telecommuting"],
)
def test_a_workable_telecommuting_flag_reads_as_remote(
    telecommuting: bool, expected: tuple[str, ...]
) -> None:
    job = _workable_job(telecommuting=telecommuting)

    listing = source_listing(WORKABLE_MAPPING, Posting(job, employer="Globex"))

    assert listing is not None
    assert listing.facts.arrangements.values == expected
    assert listing.facts.arrangements.unresolved_values == ()


def _recruitee_job(**stated: Any) -> dict[str, Any]:
    return {
        "title": "Clinical Systems Analyst",
        "careers_url": "https://globex.recruitee.com/o/clinical-systems-analyst",
        "careers_apply_url": "",
        "location": "Northtown, NT",
        "id": 4242,
        **stated,
    }


def test_a_recruitee_listing_states_its_fields() -> None:
    listing = source_listing(RECRUITEE_MAPPING, Posting(_recruitee_job(), employer="Globex"))

    assert listing is not None
    assert listing.title == "Clinical Systems Analyst"
    assert listing.url == "https://globex.recruitee.com/o/clinical-systems-analyst"
    assert listing.requisition_id == "4242"


def test_a_recruitee_offer_states_every_location_its_flags_and_its_salary() -> None:
    job = _recruitee_job(
        locations=[
            {"name": "Northtown, NT"},
            {"city": "Riverton", "state": "ON", "country": "Canada"},
        ],
        remote=True,
        hybrid=False,
        salary={"min": 60000, "max": 75000.0, "currency": "EUR", "period": "year"},
    )

    listing = source_listing(RECRUITEE_MAPPING, Posting(job, employer="Globex"))

    assert listing is not None
    assert listing.location == "Northtown, NT"
    assert listing.facts.locations.values == ("Northtown, NT", "Riverton, ON, Canada")
    assert listing.facts.arrangements.values == ("remote",)
    assert listing.facts.compensation.values == ("EUR 60000-75000 per year",)


def test_a_recruitee_offer_setting_both_flags_leaves_its_arrangement_unresolved() -> None:
    job = _recruitee_job(remote=True, hybrid=True)

    listing = source_listing(RECRUITEE_MAPPING, Posting(job, employer="Globex"))

    assert listing is not None
    assert listing.facts.arrangements.values == ()
    assert listing.facts.arrangements.unresolved_values == ("remote", "hybrid")


def test_a_recruitee_listing_falls_back_to_the_apply_url() -> None:
    job = _recruitee_job(careers_url="", careers_apply_url="https://globex.example/apply/4242")

    listing = source_listing(RECRUITEE_MAPPING, Posting(job, employer="Globex"))

    assert listing is not None
    assert listing.url == "https://globex.example/apply/4242"


def _dayforce_variant(**stated: Any) -> dict[str, Any]:
    return {
        "JobDetailsUrl": "https://www.dayforcehcm.com/job/1",
        "Title": "Clinical Systems Analyst",
        "ReferenceNumber": "REQ-1",
        "ParentRequisitionCode": "PREQ-1",
        "CompanyName": "Globex",
        "AddressLine1": "100 Example Way",
        "PostalCode": "00000",
        "City": "Northtown",
        "State": "NT",
        "Country": "US",
        "IsVirtualLocation": False,
        "Description": "<p>Configure clinical systems for hospital teams.</p>",
        **stated,
    }


def test_a_board_that_names_no_location_field_flattens_to_the_first_place_stated() -> None:
    first = _dayforce_variant()
    second = _dayforce_variant(City="Southtown", State="ST", Country="CA")
    third = _dayforce_variant(City="Eastport", State="EP", Country="IE")
    record = {**first, "variants": [first, second, third]}

    listing = source_listing(DAYFORCE_MAPPING, Posting(record, employer=""))

    assert listing is not None
    assert listing.location == "Northtown, NT, United States"
    assert listing.facts.locations.values == (
        "Northtown, NT, United States",
        "Southtown, ST, Canada",
        "Eastport, EP, Ireland",
    )


@pytest.mark.parametrize(
    ("flags", "values", "unresolved"),
    [
        pytest.param((True, True), ("remote",), (), id="every-variant-virtual"),
        pytest.param(
            (True, False),
            (),
            ("IsVirtualLocation: set on some regional variants",),
            id="variants-disagree",
        ),
        pytest.param((False, False), (), (), id="no-variant-virtual"),
    ],
)
def test_a_dayforce_virtual_location_is_remote_only_when_every_variant_says_so(
    flags: tuple[bool, bool], values: tuple[str, ...], unresolved: tuple[str, ...]
) -> None:
    variants = [
        _dayforce_variant(IsVirtualLocation=flags[0]),
        _dayforce_variant(City="Southtown", IsVirtualLocation=flags[1]),
    ]
    record = {**variants[0], "variants": variants}

    listing = source_listing(DAYFORCE_MAPPING, Posting(record, employer=""))

    assert listing is not None
    assert listing.facts.arrangements.values == values
    assert listing.facts.arrangements.unresolved_values == unresolved


@pytest.mark.parametrize(
    ("rates", "expected"),
    [
        pytest.param({"MinHiringRate": 80000.0, "MaxHiringRate": 95000.0}, ("80000-95000",)),
        pytest.param({"MinHiringRate": 0, "MaxHiringRate": 0}, (), id="unstated-rates"),
    ],
)
def test_a_dayforce_hiring_rate_range_is_stated_without_a_guessed_currency(
    rates: dict[str, Any], expected: tuple[str, ...]
) -> None:
    variant = _dayforce_variant(**rates)
    record = {**variant, "variants": [variant]}

    listing = source_listing(DAYFORCE_MAPPING, Posting(record, employer=""))

    assert listing is not None
    assert listing.facts.compensation.values == expected


def test_a_dayforce_office_advertised_to_many_regions_keeps_every_place_as_incomplete() -> None:
    office = {"City": "Riverton", "AddressLine1": "1 Harbour Road", "PostalCode": "A1B 2C3"}
    variants = [
        _dayforce_variant(**office, State="ON", Country="CA"),
        _dayforce_variant(**office, State="NY"),
        _dayforce_variant(**office, State="OH"),
    ]
    record = {**variants[0], "variants": variants}

    listing = source_listing(DAYFORCE_MAPPING, Posting(record, employer=""))

    assert listing is not None
    assert listing.facts.locations.values == (
        "Riverton, ON, Canada",
        "Riverton, NY, United States",
        "Riverton, OH, United States",
    )
    assert listing.facts.locations.unresolved_values == ()
    assert listing.facts.locations.complete is False


def test_dayforce_regional_variants_at_distinct_offices_are_a_complete_location_list() -> None:
    first = _dayforce_variant()
    second = _dayforce_variant(
        City="Southtown", State="ST", AddressLine1="200 Sample Avenue", PostalCode="11111"
    )
    record = {**first, "variants": [first, second]}

    listing = source_listing(DAYFORCE_MAPPING, Posting(record, employer=""))

    assert listing is not None
    assert listing.facts.locations.values == (
        "Northtown, NT, United States",
        "Southtown, ST, United States",
    )
    assert listing.facts.locations.complete is True


def test_a_dayforce_company_name_outranks_a_configured_employer() -> None:
    first = _dayforce_variant()
    record = {**first, "variants": [first]}

    listing = source_listing(DAYFORCE_MAPPING, Posting(record, employer="Initech"))

    assert listing is not None
    assert listing.employer == "Globex"


def test_a_hand_named_dayforce_board_keeps_its_configured_employer() -> None:
    first = _dayforce_variant()
    record = {**first, "variants": [first]}

    listing = source_listing(
        DAYFORCE_MAPPING, Posting(record, employer="Initech", employer_is_authoritative=True)
    )

    assert listing is not None
    assert listing.employer == "Initech"


def test_a_dayforce_listing_states_its_company_requisition_and_body() -> None:
    first = _dayforce_variant()
    record = {**first, "variants": [first]}

    listing = source_listing(DAYFORCE_MAPPING, Posting(record, employer=""))

    assert listing is not None
    assert listing.employer == "Globex"
    assert listing.requisition_id == "PREQ-1"
    assert listing.published_description == "Configure clinical systems for hospital teams."


def _bamboohr_row(**stated: Any) -> dict[str, Any]:
    return {
        "id": "77",
        "jobOpeningName": "Clinical Systems Analyst",
        "isRemote": False,
        **stated,
    }


def _bamboohr_opening(**stated: Any) -> dict[str, Any]:
    return {
        "jobOpeningName": "Clinical Systems Analyst",
        "jobOpeningStatus": "Open",
        "jobOpeningShareUrl": "https://globex.bamboohr.com/careers/77",
        "isRemote": False,
        "location": {"city": "Northtown", "state": "NT", "addressCountry": "US"},
        "description": "<p>Configure clinical systems for hospital teams.</p>",
        **stated,
    }


def test_a_bamboohr_listing_states_its_fields_from_the_detail_opening() -> None:
    record = {"row": _bamboohr_row(), "opening": _bamboohr_opening()}

    listing = source_listing(
        BAMBOOHR_MAPPING, Posting(record, employer="Globex", groups={"tenant": "globex"})
    )

    assert listing is not None
    assert listing.title == "Clinical Systems Analyst"
    assert listing.url == "https://globex.bamboohr.com/careers/77"
    assert listing.location == "Northtown, NT, US"
    assert listing.requisition_id == "77"
    assert listing.published_description == "Configure clinical systems for hospital teams."
    assert listing.facts.arrangements.values == ()


def test_a_bamboohr_remote_opening_is_a_resolved_arrangement_without_a_fabricated_place() -> None:
    record = {"row": _bamboohr_row(), "opening": _bamboohr_opening(isRemote=True)}

    listing = source_listing(
        BAMBOOHR_MAPPING, Posting(record, employer="Globex", groups={"tenant": "globex"})
    )

    assert listing is not None
    assert listing.facts.arrangements.values == ("remote",)
    assert listing.location == "Northtown, NT, US"


def test_a_bamboohr_remote_opening_without_a_location_states_no_place() -> None:
    opening = {
        "jobOpeningName": "Fully Remote Analyst",
        "jobOpeningStatus": "Open",
        "jobOpeningShareUrl": "https://globex.bamboohr.com/careers/78",
        "isRemote": True,
        "description": "<p>Work from anywhere.</p>",
    }
    record = {"row": _bamboohr_row(id="78", isRemote=True), "opening": opening}

    listing = source_listing(
        BAMBOOHR_MAPPING, Posting(record, employer="Globex", groups={"tenant": "globex"})
    )

    assert listing is not None
    assert listing.location == ""
    assert listing.facts.arrangements.values == ("remote",)


def test_a_bamboohr_opening_states_its_compensation_as_written() -> None:
    opening = _bamboohr_opening(compensation="$70,000 - $90,000 annually")
    record = {"row": _bamboohr_row(), "opening": opening}

    listing = source_listing(
        BAMBOOHR_MAPPING, Posting(record, employer="Globex", groups={"tenant": "globex"})
    )

    assert listing is not None
    assert listing.facts.compensation.values == ("$70,000 - $90,000 annually",)


def test_a_bamboohr_listing_falls_back_to_the_row_when_the_opening_states_nothing() -> None:
    row = _bamboohr_row(jobOpeningName="Row Title Only")
    opening = {"jobOpeningStatus": "Open", "isRemote": False}
    record = {"row": row, "opening": opening}

    listing = source_listing(
        BAMBOOHR_MAPPING, Posting(record, employer="Globex", groups={"tenant": "globex"})
    )

    assert listing is not None
    assert listing.title == "Row Title Only"
    assert listing.url == "https://globex.bamboohr.com/careers/77"


def _pinpoint_job(**stated: Any) -> dict[str, Any]:
    return {
        "title": "Clinical Systems Analyst",
        "url": "https://globex.pinpointhq.com/postings/abc",
        "description": "<p>Configure reporting systems for depot teams.</p>",
        "key_responsibilities": "<ul><li>Validate builds</li></ul>",
        "skills_knowledge_expertise": "<p>Five years of health IT.</p>",
        "benefits": "<p>Health, dental, and vision.</p>",
        "location": {"name": "Northtown"},
        "job": {"requisition_id": "REQ-9"},
        "id": "abc",
        **stated,
    }


def test_a_pinpoint_listing_states_its_fields_and_every_body_section() -> None:
    listing = source_listing(PINPOINT_MAPPING, Posting(_pinpoint_job(), employer="Globex"))

    assert listing is not None
    assert listing.location == "Northtown"
    assert listing.requisition_id == "REQ-9"
    assert listing.published_description == (
        "Configure reporting systems for depot teams.\n\n- Validate builds\n\n"
        "Five years of health IT.\n\nHealth, dental, and vision."
    )


@pytest.mark.parametrize(
    ("visible", "expected"),
    [
        pytest.param(True, ("CAD 90000-110000 per year",), id="shown"),
        pytest.param(False, (), id="hidden"),
    ],
)
def test_a_pinpoint_range_is_stated_only_when_the_posting_shows_it(
    visible: bool, expected: tuple[str, ...]
) -> None:
    job = _pinpoint_job(
        compensation_visible=visible,
        compensation_minimum=90000,
        compensation_maximum=110000,
        compensation_currency="CAD",
        compensation_frequency="year",
    )

    listing = source_listing(PINPOINT_MAPPING, Posting(job, employer="Globex"))

    assert listing is not None
    assert listing.facts.compensation.values == expected


def test_a_pinpoint_listing_states_its_workplace_and_employment_type() -> None:
    job = _pinpoint_job(workplace_type="remote", employment_type_text="Full Time")

    listing = source_listing(PINPOINT_MAPPING, Posting(job, employer="Globex"))

    assert listing is not None
    assert listing.facts.arrangements.values == ("remote",)
    assert listing.facts.arrangements.complete is True
    assert listing.facts.employment_types.values == ("Full Time",)


@pytest.mark.parametrize(
    "stated",
    [
        pytest.param({"location": {"name": "USA-IL-Remote"}}, id="remote-place"),
        pytest.param(
            {"benefits": "<p>You may work remotely from home.</p>"}, id="remote-offer-in-body"
        ),
    ],
)
@pytest.mark.parametrize("workplace", ["hybrid", "onsite"])
def test_a_pinpoint_attendance_type_is_unresolved_beside_remote(
    workplace: str, stated: dict[str, Any]
) -> None:
    job = _pinpoint_job(workplace_type=workplace, **stated)

    listing = source_listing(PINPOINT_MAPPING, Posting(job, employer="Globex"))

    assert listing is not None
    assert listing.facts.arrangements.values == ()
    assert workplace in listing.facts.arrangements.unresolved_values


def test_a_pinpoint_workplace_type_outside_the_published_three_is_unresolved() -> None:
    job = _pinpoint_job(workplace_type="flexible")

    listing = source_listing(PINPOINT_MAPPING, Posting(job, employer="Globex"))

    assert listing is not None
    assert listing.facts.arrangements.values == ()
    assert listing.facts.arrangements.unresolved_values == ("workplace_type: flexible",)


def test_a_pinpoint_requisition_id_falls_back_to_the_feed_id() -> None:
    job = _pinpoint_job(job={"requisition_id": ""})

    listing = source_listing(PINPOINT_MAPPING, Posting(job, employer="Globex"))

    assert listing is not None
    assert listing.requisition_id == "abc"


def _paylocity_job(**stated: Any) -> dict[str, Any]:
    return {
        "title": "Clinical Systems Analyst",
        "displayUrl": "https://recruiting.paylocity.com/recruiting/jobs/Details/1/globex",
        "applyUrl": "",
        "description": "<p>Configure clinical systems for hospital teams.</p>",
        "requirements": "<p>Five years of health IT.</p>",
        "jobLocation": {
            "locationDisplayName": "Northtown HQ",
            "city": "Northtown",
            "state": "NT",
        },
        "companyName": "Globex",
        "jobCode": "REQ-9",
        "jobId": "1",
        **stated,
    }


def test_a_paylocity_listing_states_its_fields_and_both_body_sections() -> None:
    listing = source_listing(PAYLOCITY_MAPPING, Posting(_paylocity_job(), employer=""))

    assert listing is not None
    assert listing.employer == "Globex"
    assert listing.location == "Northtown HQ; Northtown, NT"
    assert listing.requisition_id == "REQ-9"
    assert listing.published_description == (
        "Configure clinical systems for hospital teams.\n\nFive years of health IT."
    )


def test_a_paylocity_listing_falls_back_to_the_job_id_for_its_requisition() -> None:
    listing = source_listing(PAYLOCITY_MAPPING, Posting(_paylocity_job(jobCode=""), employer=""))

    assert listing is not None
    assert listing.requisition_id == "1"


def test_a_paylocity_listing_with_no_job_id_falls_back_to_the_display_then_apply_url() -> None:
    shown = _paylocity_job(jobId="")
    apply_only = _paylocity_job(jobId="", displayUrl="", applyUrl="https://globex.example/apply/1")

    listings = [
        source_listing(PAYLOCITY_MAPPING, Posting(job, employer="")) for job in (shown, apply_only)
    ]

    assert [listing.url if listing else None for listing in listings] == [
        "https://recruiting.paylocity.com/recruiting/jobs/Details/1/globex",
        "https://globex.example/apply/1",
    ]


def test_an_all_closed_bamboohr_board_is_a_complete_empty_read() -> None:
    class ScriptedBambooHR(BoardApiFetcher):
        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            if url.endswith("/careers/list"):
                return {"result": [{"id": "43", "jobOpeningName": "Withdrawn Role"}]}
            return {
                "result": {
                    "jobOpening": {
                        "jobOpeningName": "Withdrawn Role",
                        "jobOpeningStatus": "Closed",
                    }
                }
            }

    read = ScriptedBambooHR().listings("Gamma", "https://gamma.bamboohr.com/careers")

    assert read.complete is True
    assert read.outcome is SourceReadOutcome.ATS_COMPLETE
    assert read.listings == []
    assert read.raw_count == 1
    assert read.deferred_targets == ()
    assert (read.detail.attempted, read.detail.read, read.detail.failed) == (1, 1, 0)


def test_bamboohr_reads_every_detail_and_retains_complete_bodies() -> None:
    requested: list[str] = []

    class ScriptedBambooHR(BoardApiFetcher):
        def __init__(self) -> None:
            pass

        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            requested.append(url)
            if url.endswith("/careers/list"):
                return {
                    "meta": {"totalCount": 2},
                    "result": [
                        {"id": "43", "jobOpeningName": "Technical Deployment Manager"},
                        {"id": "44", "jobOpeningName": "Withdrawn Role"},
                    ],
                }
            job_id = url.split("/")[-2]
            return {
                "result": {
                    "jobOpening": {
                        "jobOpeningName": (
                            "Technical Deployment Manager" if job_id == "43" else "Withdrawn Role"
                        ),
                        "jobOpeningStatus": "Open" if job_id == "43" else "Closed",
                        "jobOpeningShareUrl": f"https://gamma.bamboohr.com/careers/{job_id}",
                        "location": {
                            "city": "Toronto",
                            "state": "Ontario",
                            "addressCountry": "Canada",
                        },
                        "description": "<p>Lead healthcare AI deployments and integrations.</p>",
                    }
                }
            }

    read = ScriptedBambooHR().listings("Gamma", "https://gamma.bamboohr.com/careers")

    assert requested == [
        "https://gamma.bamboohr.com/careers/list",
        "https://gamma.bamboohr.com/careers/43/detail",
        "https://gamma.bamboohr.com/careers/44/detail",
    ]
    assert read.complete is True
    assert read.raw_count == 2
    [listing] = read.listings
    assert listing.title == "Technical Deployment Manager"
    assert listing.location == "Toronto, Ontario, Canada"
    assert listing.requisition_id == "43"
    assert listing.published_description == "Lead healthcare AI deployments and integrations."


def test_gem_reads_its_board_list_with_each_post_body() -> None:
    requested: list[str] = []

    class ScriptedGem(BoardApiFetcher):
        def __init__(self) -> None:
            pass

        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            requested.append(url)
            return [
                {
                    "id": "am9icG9zdDrChY8Z",
                    "title": "Clinical Data Engineer",
                    "absolute_url": "https://jobs.gem.com/globex/am9icG9zdDrChY8Z",
                    "content": "<div><p>Build clinical data pipelines.</p></div>",
                    "location": {"name": "Canada - Remote"},
                    "location_type": "remote",
                    "employment_type": "full_time",
                    "offices": [{"location": {"name": "United States - Remote"}}],
                }
            ]

    read = ScriptedGem().listings("Globex", "https://jobs.gem.com/globex")

    assert requested == ["https://api.gem.com/job_board/v0/globex/job_posts/"]
    assert read.complete is True
    [listing] = read.listings
    assert listing.title == "Clinical Data Engineer"
    assert listing.url == "https://jobs.gem.com/globex/am9icG9zdDrChY8Z"
    assert listing.location == "Canada - Remote"
    assert listing.published_description == "Build clinical data pipelines."
    assert listing.facts.arrangements.values == ("remote",)
    assert listing.facts.locations.values == ("Canada - Remote", "United States - Remote")


@pytest.mark.parametrize(
    ("location_type", "values", "unresolved"),
    [
        ("hybrid", ("hybrid",), ()),
        ("in_office", ("onsite",), ()),
        ("flexible", (), ("location_type: flexible",)),
    ],
)
def test_a_gem_location_type_reads_as_its_arrangement(
    location_type: str, values: tuple[str, ...], unresolved: tuple[str, ...]
) -> None:
    post = {
        "id": "1",
        "title": "Clinical Data Engineer",
        "absolute_url": "https://jobs.gem.com/globex/1",
        "content": "<p>Build clinical data pipelines in Northtown.</p>",
        "location": {"name": "Northtown"},
        "location_type": location_type,
    }
    listing = source_listing(GEM_MAPPING, Posting(post, employer="Globex"))
    assert listing is not None
    assert listing.facts.arrangements.values == values
    assert listing.facts.arrangements.unresolved_values == unresolved


_RIPPLING_JOBS = "https://ats.rippling.com/api/v2/board/globex/jobs"
_RIPPLING_JOB = "3f2a9c1e-5b7d-4e8a-9c0f-1a2b3c4d5e6f"


def _rippling_row(**stated: Any) -> dict[str, Any]:
    return {
        "id": _RIPPLING_JOB,
        "name": "Clinical Data Engineer",
        "url": f"https://ats.rippling.com/globex/jobs/{_RIPPLING_JOB}",
        "department": {"name": "Engineering"},
        "locations": [
            {
                "name": "Remote (United States)",
                "country": "United States",
                "countryCode": "US",
                "state": None,
                "city": None,
                "workplaceType": "REMOTE",
            }
        ],
        "language": "en-US",
        **stated,
    }


def _rippling_detail(**stated: Any) -> dict[str, Any]:
    return {
        "uuid": _RIPPLING_JOB,
        "name": "Clinical Data Engineer",
        "companyName": "Globex Health",
        "description": {
            "company": "<p>Globex builds clinical tools.</p>",
            "role": "<p>Build clinical data pipelines.</p>",
        },
        "workLocations": ["Remote (United States)"],
        "employmentType": {"label": "SALARIED_FT", "id": "Salaried, full-time"},
        "payRangeDetails": [],
        **stated,
    }


def _rippling_page(
    rows: list[dict[str, Any]],
    *,
    total_items: int | None = None,
    total_pages: int = 1,
    page: int = 0,
) -> dict[str, Any]:
    return {
        "items": rows,
        "page": page,
        "pageSize": 20,
        "totalItems": len(rows) if total_items is None else total_items,
        "totalPages": total_pages,
    }


class _ScriptedRippling(BoardApiFetcher):
    def __init__(self, pages: dict[str, dict[str, Any]]) -> None:
        self.pages = pages
        self.requested: list[str] = []

    def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
        self.requested.append(url)
        if url in self.pages:
            return self.pages[url]
        return _rippling_detail(uuid=url.rsplit("/", 1)[-1])


_RIPPLING_SECOND = "7b8c9d0e-1f2a-4b3c-8d4e-5f6a7b8c9d0e"


def test_a_rippling_detail_states_each_published_pay_range() -> None:
    ranges = [
        {"currency": "USD", "rangeStart": 120000.0, "rangeEnd": 150000.0},
        {"currency": "CAD", "rangeStart": 140000, "rangeEnd": 170000},
        {"currency": "CAD", "rangeStart": None, "rangeEnd": 170000},
    ]
    record = {"row": _rippling_row(), "detail": _rippling_detail(payRangeDetails=ranges)}

    listing = source_listing(RIPPLING_MAPPING, Posting(record, employer="Globex"))

    assert listing is not None
    assert listing.facts.compensation.values == ("USD 120000-150000", "CAD 140000-170000")


def test_rippling_reads_every_detail_and_retains_its_company_and_role_body() -> None:
    fetcher = _ScriptedRippling({_RIPPLING_JOBS: _rippling_page([_rippling_row()])})

    read = fetcher.listings("Globex", "https://ats.rippling.com/globex/jobs")

    assert fetcher.requested == [_RIPPLING_JOBS, f"{_RIPPLING_JOBS}/{_RIPPLING_JOB}"]
    assert read.complete is True
    assert read.raw_count == 1
    [listing] = read.listings
    assert listing.title == "Clinical Data Engineer"
    assert listing.url == f"https://ats.rippling.com/globex/jobs/{_RIPPLING_JOB}"
    assert listing.employer == "Globex Health"
    assert listing.requisition_id == _RIPPLING_JOB
    assert listing.location == "Remote (United States)"
    assert listing.facts.locations.values == ("Remote (United States)", "United States")
    assert listing.facts.arrangements.values == ("remote",)
    assert listing.facts.employment_types.values == ("Salaried, full-time",)
    assert listing.facts.compensation.values == ()
    assert listing.published_description == (
        "Globex builds clinical tools.\n\nBuild clinical data pipelines."
    )


def test_rippling_reads_every_page_its_board_states() -> None:
    second = _rippling_row(id=_RIPPLING_SECOND, url="")
    fetcher = _ScriptedRippling(
        {
            _RIPPLING_JOBS: _rippling_page([_rippling_row()], total_items=2, total_pages=2),
            f"{_RIPPLING_JOBS}?page=1": _rippling_page(
                [second], total_items=2, total_pages=2, page=1
            ),
        }
    )

    read = fetcher.listings("Globex", "https://ats.rippling.com/globex/jobs")

    assert fetcher.requested == [
        _RIPPLING_JOBS,
        f"{_RIPPLING_JOBS}?page=1",
        f"{_RIPPLING_JOBS}/{_RIPPLING_JOB}",
        f"{_RIPPLING_JOBS}/{_RIPPLING_SECOND}",
    ]
    assert read.complete is True
    assert read.raw_count == 2
    assert read.listing_pages == 2
    assert [listing.requisition_id for listing in read.listings] == [
        _RIPPLING_JOB,
        _RIPPLING_SECOND,
    ]


def test_a_rippling_board_answering_every_page_with_the_first_is_not_complete() -> None:
    first = _rippling_page([_rippling_row()], total_items=2, total_pages=2)
    fetcher = _ScriptedRippling({_RIPPLING_JOBS: first, f"{_RIPPLING_JOBS}?page=1": first})

    read = fetcher.listings("Globex", "https://ats.rippling.com/globex/jobs")

    assert fetcher.requested.count(f"{_RIPPLING_JOBS}/{_RIPPLING_JOB}") == 1
    assert [listing.requisition_id for listing in read.listings] == [_RIPPLING_JOB]
    assert read.complete is False
    assert read.outcome is SourceReadOutcome.NO_SUPPORTED_REPRESENTATION


def test_a_rippling_board_stating_more_jobs_than_it_returned_is_not_complete() -> None:
    fetcher = _ScriptedRippling({_RIPPLING_JOBS: _rippling_page([_rippling_row()], total_items=2)})

    read = fetcher.listings("Globex", "https://ats.rippling.com/globex/jobs")

    assert fetcher.requested == [_RIPPLING_JOBS, f"{_RIPPLING_JOBS}/{_RIPPLING_JOB}"]
    assert [listing.requisition_id for listing in read.listings] == [_RIPPLING_JOB]
    assert read.complete is False
    assert read.outcome is SourceReadOutcome.NO_SUPPORTED_REPRESENTATION


@pytest.mark.parametrize("unique_total", [False, True])
def test_rippling_location_rows_keep_every_place_and_read_each_job_once(
    unique_total: bool,
) -> None:
    remote = _rippling_row()
    other_place = _rippling_placed_row("REMOTE")
    second = _rippling_row(id=_RIPPLING_SECOND, url="")
    rows = [remote, other_place, second]
    fetcher = _ScriptedRippling(
        {_RIPPLING_JOBS: _rippling_page(rows, total_items=2 if unique_total else 3)}
    )

    read = fetcher.listings("Globex", "https://ats.rippling.com/globex/jobs")

    assert read.complete is True
    assert read.raw_count == 2
    assert [listing.requisition_id for listing in read.listings] == [
        _RIPPLING_JOB,
        _RIPPLING_SECOND,
    ]
    assert read.listings[0].facts.locations.values == (
        "Remote (United States)",
        "Northtown, NT",
        "United States",
        "Canada",
    )
    assert read.listings[0].url == remote["url"]
    assert all(listing.published_description for listing in read.listings)
    assert fetcher.requested == [
        _RIPPLING_JOBS,
        f"{_RIPPLING_JOBS}/{_RIPPLING_JOB}",
        f"{_RIPPLING_JOBS}/{_RIPPLING_SECOND}",
    ]
    assert read.detail_coverage is not None
    assert read.detail_coverage.posting_urls == tuple(listing.url for listing in read.listings)
    assert read.detail_read_urls == read.detail_coverage.posting_urls
    assert len(rows) == 3
    assert len(remote["locations"]) == 1
    assert remote["locations"] != other_place["locations"]


def test_rippling_location_variants_and_exact_repeats_span_all_advertised_pages() -> None:
    remote = _rippling_row()
    other_place = _rippling_placed_row("REMOTE")
    second = _rippling_row(id=_RIPPLING_SECOND, url="")
    fetcher = _ScriptedRippling(
        {
            _RIPPLING_JOBS: _rippling_page([remote, second], total_items=4, total_pages=3),
            f"{_RIPPLING_JOBS}?page=1": _rippling_page(
                [other_place], total_items=4, total_pages=3, page=1
            ),
            f"{_RIPPLING_JOBS}?page=2": _rippling_page(
                [remote], total_items=4, total_pages=3, page=2
            ),
        }
    )

    read = fetcher.listings("Globex", "https://ats.rippling.com/globex/jobs")

    assert read.complete
    assert read.raw_count == 2
    assert read.listings[0].facts.locations.values == (
        "Remote (United States)",
        "Northtown, NT",
        "United States",
        "Canada",
    )
    assert fetcher.requested == [
        _RIPPLING_JOBS,
        f"{_RIPPLING_JOBS}?page=1",
        f"{_RIPPLING_JOBS}?page=2",
        f"{_RIPPLING_JOBS}/{_RIPPLING_JOB}",
        f"{_RIPPLING_JOBS}/{_RIPPLING_SECOND}",
    ]


def test_rippling_location_rows_with_mixed_workplaces_keep_the_arrangement_unresolved() -> None:
    fetcher = _ScriptedRippling(
        {
            _RIPPLING_JOBS: _rippling_page(
                [_rippling_placed_row("HYBRID"), _rippling_placed_row("ON_SITE")]
            )
        }
    )

    read = fetcher.listings("Globex", "https://ats.rippling.com/globex/jobs")

    assert read.complete
    [listing] = read.listings
    assert listing.facts.arrangements.values == ()
    assert listing.facts.arrangements.unresolved_values == ("hybrid", "onsite")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("totalItems", None),
        ("totalItems", True),
        ("totalItems", "1"),
        ("totalItems", -1),
        ("totalItems", 1.0),
        ("totalPages", None),
        ("totalPages", True),
        ("totalPages", "1"),
        ("totalPages", 0),
        ("totalPages", -1),
        ("totalPages", 1.0),
        ("page", None),
        ("page", False),
        ("page", 1),
        ("page", "0"),
        ("pageSize", 0),
        ("pageSize", True),
        ("pageSize", "20"),
    ],
)
def test_a_rippling_board_with_invalid_page_metadata_cannot_certify_coverage(
    field: str,
    value: Any,
) -> None:
    page = _rippling_page([_rippling_row()])
    page[field] = value
    fetcher = _ScriptedRippling({_RIPPLING_JOBS: page})

    read = fetcher.listings("Globex", "https://ats.rippling.com/globex/jobs")

    assert not read.complete
    assert read.detail_coverage is None
    assert read.listings[0].published_description


@pytest.mark.parametrize(
    ("field", "value"),
    [("totalItems", 3), ("totalPages", 1), ("page", 0), ("pageSize", 10)],
)
def test_a_rippling_board_with_changing_page_metadata_cannot_certify_coverage(
    field: str,
    value: int,
) -> None:
    second = _rippling_page(
        [_rippling_row(id=_RIPPLING_SECOND, url="")], total_items=2, total_pages=2, page=1
    )
    second[field] = value
    fetcher = _ScriptedRippling(
        {
            _RIPPLING_JOBS: _rippling_page([_rippling_row()], total_items=2, total_pages=2),
            f"{_RIPPLING_JOBS}?page=1": second,
        }
    )

    read = fetcher.listings("Globex", "https://ats.rippling.com/globex/jobs")

    assert not read.complete
    assert read.detail_coverage is None
    assert len(read.listings) == 2


@pytest.mark.parametrize("reordered", [False, True])
def test_repeated_rippling_pages_cannot_certify_even_when_the_unique_total_matches(
    reordered: bool,
) -> None:
    rows = [_rippling_row(), _rippling_row(id=_RIPPLING_SECOND, url="")]
    fetcher = _ScriptedRippling(
        {
            _RIPPLING_JOBS: _rippling_page(rows, total_items=2, total_pages=2),
            f"{_RIPPLING_JOBS}?page=1": _rippling_page(
                list(reversed(rows)) if reordered else list(rows),
                total_items=2,
                total_pages=2,
                page=1,
            ),
        }
    )

    read = fetcher.listings("Globex", "https://ats.rippling.com/globex/jobs")

    assert not read.complete
    assert read.detail_coverage is None
    assert read.raw_count == 2


@pytest.mark.parametrize(
    "change", ["empty-page", "missing-row", "missing-id", "invalid-id", "exact-only", "conflict"]
)
def test_rippling_physical_row_totals_need_valid_location_variant_groups(change: str) -> None:
    remote = _rippling_row()
    other = _rippling_placed_row("REMOTE")
    second = _rippling_row(id=_RIPPLING_SECOND, url="")
    rows = [remote, other, second]
    if change == "missing-row":
        rows = rows[:2]
    elif change == "missing-id":
        rows[-1] = _rippling_row(id="")
    elif change == "invalid-id":
        rows[-1] = _rippling_row(id=True)
    elif change == "exact-only":
        rows[1] = _rippling_row()
    elif change == "conflict":
        rows[1] = _rippling_placed_row("REMOTE") | {"name": "Clinical Systems Analyst"}
    pages = {_RIPPLING_JOBS: _rippling_page(rows, total_items=3)}
    if change == "empty-page":
        pages[_RIPPLING_JOBS] = _rippling_page(rows, total_items=3, total_pages=2)
        pages[f"{_RIPPLING_JOBS}?page=1"] = _rippling_page([], total_items=3, total_pages=2, page=1)
    fetcher = _ScriptedRippling(pages)

    read = fetcher.listings("Globex", "https://ats.rippling.com/globex/jobs")

    assert not read.complete
    assert read.detail_coverage is None
    assert read.listings[0].published_description
    if change == "conflict":
        assert "conflicting" in read.note
        assert "name" in read.note


@pytest.mark.parametrize("total", [1, 2])
@pytest.mark.parametrize(
    ("field", "value"), [("name", "Clinical Systems Analyst"), ("department", {"name": "Data"})]
)
def test_rippling_conflicting_identity_fields_are_incomplete_under_either_total(
    total: int,
    field: str,
    value: Any,
) -> None:
    fetcher = _ScriptedRippling(
        {
            _RIPPLING_JOBS: _rippling_page(
                [_rippling_row(), _rippling_placed_row("REMOTE") | {field: value}],
                total_items=total,
            )
        }
    )

    read = fetcher.listings("Globex", "https://ats.rippling.com/globex/jobs")

    assert not read.complete
    assert read.detail_coverage is None
    assert "conflicting" in read.note
    assert field in read.note
    [listing] = read.listings
    assert listing.facts.locations.values == ("Remote (United States)", "United States")
    assert listing.title == "Clinical Data Engineer"


@pytest.mark.parametrize("locations", [None, [], [None]])
def test_rippling_row_totals_cannot_count_unlocated_rows_inside_a_variant_group(
    locations: Any,
) -> None:
    fetcher = _ScriptedRippling(
        {
            _RIPPLING_JOBS: _rippling_page(
                [
                    _rippling_row(),
                    _rippling_placed_row("REMOTE"),
                    _rippling_row(locations=locations),
                ]
            )
        }
    )

    read = fetcher.listings("Globex", "https://ats.rippling.com/globex/jobs")

    assert not read.complete
    assert read.detail_coverage is None
    assert read.raw_count == 1


@pytest.mark.parametrize("pages", [0, 1])
def test_an_empty_rippling_board_with_a_valid_zero_total_is_complete(pages: int) -> None:
    fetcher = _ScriptedRippling({_RIPPLING_JOBS: _rippling_page([], total_pages=pages)})

    read = fetcher.listings("Globex", "https://ats.rippling.com/globex/jobs")

    assert read.complete
    assert read.raw_count == 0
    assert read.listings == []
    assert read.detail_coverage is not None
    assert read.detail_coverage.posting_urls == ()


@pytest.mark.parametrize("envelope", [{}, {"items": None}, {"items": []}])
def test_a_missing_rippling_advertised_page_cannot_certify_coverage(
    envelope: dict[str, Any],
) -> None:
    fetcher = _ScriptedRippling(
        {
            _RIPPLING_JOBS: _rippling_page([_rippling_row()], total_items=1, total_pages=2),
            f"{_RIPPLING_JOBS}?page=1": envelope,
        }
    )

    read = fetcher.listings("Globex", "https://ats.rippling.com/globex/jobs")

    assert not read.complete
    assert read.detail_coverage is None


def test_rippling_retains_the_existing_page_guard_without_certifying_a_capped_window() -> None:
    pages = {
        _RIPPLING_JOBS if page == 0 else f"{_RIPPLING_JOBS}?page={page}": _rippling_page(
            [_rippling_row(locations=[{"name": f"Place {page}", "workplaceType": "REMOTE"}])],
            total_items=100,
            total_pages=101,
            page=page,
        )
        for page in range(100)
    }
    fetcher = _ScriptedRippling(pages)

    read = fetcher.listings("Globex", "https://ats.rippling.com/globex/jobs")

    assert not read.complete
    assert read.detail_coverage is None
    assert read.raw_count == 1
    assert f"{_RIPPLING_JOBS}?page=99" in fetcher.requested
    assert f"{_RIPPLING_JOBS}?page=100" not in fetcher.requested
    assert fetcher.requested.count(f"{_RIPPLING_JOBS}/{_RIPPLING_JOB}") == 1


@pytest.mark.parametrize("stop", ["before-first", "deadline", "failure"])
def test_rippling_location_variants_keep_unique_detail_progress_until_recovery(
    monkeypatch: pytest.MonkeyPatch,
    stop: str,
) -> None:
    now = [100.0]
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.board_adapters.transport.time.monotonic",
        lambda: now[0],
    )
    rows = [
        _rippling_row(),
        _rippling_placed_row("REMOTE"),
        _rippling_row(id=_RIPPLING_SECOND, url=""),
    ]

    class InterruptedRippling(_ScriptedRippling):
        stopped = True

        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            if self.stopped and url == _RIPPLING_JOBS and stop == "before-first":
                now[0] += 60
            if self.stopped and url == f"{_RIPPLING_JOBS}/{_RIPPLING_JOB}" and stop == "deadline":
                now[0] += 60
            if self.stopped and url == f"{_RIPPLING_JOBS}/{_RIPPLING_SECOND}" and stop == "failure":
                self.requested.append(url)
                raise httpx.ReadError("connection lost", request=httpx.Request("GET", url))
            return super().get_json(url, headers=headers)

    fetcher = InterruptedRippling({_RIPPLING_JOBS: _rippling_page(rows)})
    first = fetcher.listings("Globex", "https://ats.rippling.com/globex/jobs")

    assert first.raw_count == 2
    assert not first.complete
    assert first.detail_coverage is None
    assert first.detail.failed == int(stop == "failure")
    [cursor] = first.deferred_targets
    assert cursor.detail_checkpoint is not None
    assert cursor.detail_checkpoint.pending_ids == (
        (_RIPPLING_JOB, _RIPPLING_SECOND) if stop == "before-first" else (_RIPPLING_SECOND,)
    )
    assert cursor.detail_checkpoint.failed_attempts == (
        ((_RIPPLING_SECOND, 1),) if stop == "failure" else ()
    )
    fetcher.stopped = False
    final = fetcher.continue_listings(
        "Globex", (cursor,), root_url="https://ats.rippling.com/globex/jobs", handoff_scopes=()
    )

    assert final.complete
    assert final.raw_count == 2
    assert final.deferred_targets == ()
    assert final.detail_coverage is not None
    assert final.detail_read_urls == final.detail_coverage.posting_urls
    assert len(final.detail_read_urls or ()) == 2
    bodies = {listing.requisition_id: listing for listing in [*first.listings, *final.listings]}
    assert bodies[_RIPPLING_JOB].facts.locations.values == (
        "Remote (United States)",
        "Northtown, NT",
        "United States",
        "Canada",
    )
    assert all(listing.published_description for listing in bodies.values())


@pytest.mark.parametrize(
    "failure", ["transport", "http-status", "deadline", "host-held", "paced-hold", "json"]
)
@pytest.mark.parametrize("checkpoint_scope", ["matching", "other-platform", "other-board"])
def test_rippling_retains_partial_rows_and_a_relist_obligation_after_a_later_page_fails(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    checkpoint_scope: str,
) -> None:
    now = [100.0]
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.board_adapters.transport.time.monotonic",
        lambda: now[0],
    )
    third_id = "11111111-2222-4333-8444-555555555553"
    protected_url = f"https://ats.rippling.com/globex/jobs/{_RIPPLING_SECOND}"
    options = SourceReadOptions(protected_posting_urls=frozenset({protected_url}))
    us_variant = _rippling_row(
        locations=[{"name": "United States", "countryCode": "US", "workplaceType": "REMOTE"}]
    )
    protected = _rippling_row(id=_RIPPLING_SECOND, url=protected_url)
    third = _rippling_row(id=third_id, url=f"https://ats.rippling.com/globex/jobs/{third_id}")
    pages = {
        _RIPPLING_JOBS: _rippling_page(
            [_rippling_row(), us_variant, protected], total_items=5, total_pages=2
        ),
        f"{_RIPPLING_JOBS}?page=1": _rippling_page(
            [_rippling_placed_row("REMOTE"), third], total_items=5, total_pages=2, page=1
        ),
    }

    class InterruptedPage(_ScriptedRippling):
        interrupted = True

        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            if url == f"{_RIPPLING_JOBS}?page=1" and self.interrupted:
                self.requested.append(url)
                request = httpx.Request("GET", url)
                if failure == "deadline":
                    now[0] += 60
                    raise RequestDeadlineExceeded("GET", url)
                if failure in {"host-held", "paced-hold"}:
                    raise HostHeldError(url, ready_at=100, paced=failure == "paced-hold")
                if failure == "json":
                    raise json.JSONDecodeError("invalid JSON", "{", 1)
                if failure == "http-status":
                    raise httpx.HTTPStatusError(
                        "Unavailable",
                        request=request,
                        response=httpx.Response(503, request=request),
                    )
                raise httpx.ReadError("connection lost", request=request)
            return super().get_json(url, headers=headers)

    fetcher = InterruptedPage(pages)
    first = fetcher.listings("Globex", "https://ats.rippling.com/globex/jobs", options=options)

    assert not first.complete
    assert first.raw_count == 2
    assert first.detail_coverage is None
    assert first.protected_posting_urls == (protected_url,)
    assert f"{_RIPPLING_JOBS}/{_RIPPLING_SECOND}" not in fetcher.requested
    assert first.detail.failed == 0
    [cursor] = first.deferred_targets
    assert cursor.deferred_reason == "rippling_detail_rows_pending"
    assert cursor.automatic_eligible is (failure != "host-held")
    assert ("host_held" in first.skip_reasons) is (failure == "host-held")
    assert cursor.detail_checkpoint is not None
    checkpoint = cursor.detail_checkpoint
    assert checkpoint.listing_unstable
    assert checkpoint.protected == ((_RIPPLING_SECOND, protected_url),)
    assert checkpoint.failed_attempts == ()
    if failure == "deadline":
        assert checkpoint.pending_ids == (_RIPPLING_JOB,)
        assert checkpoint.completed == ()
        assert first.skip_reasons.get("time_limit_reached") == 1
        assert first.detail.attempted == 0
    else:
        assert checkpoint.pending_ids == ()
        assert checkpoint.completed == (
            (_RIPPLING_JOB, f"https://ats.rippling.com/globex/jobs/{_RIPPLING_JOB}"),
        )
        [listing] = first.listings
        assert not listing.facts.locations.complete
        assert not listing.facts.arrangements.complete
        assert "Northtown, NT" not in listing.facts.locations.values
    assert "page 1" in first.note

    if failure == "transport":
        repeated = fetcher.continue_listings(
            "Globex",
            (cursor,),
            root_url="https://ats.rippling.com/globex/jobs",
            handoff_scopes=(),
            options=options,
        )
        assert not repeated.complete
        assert repeated.raw_count == 2
        [retry] = repeated.deferred_targets
        assert retry.url == cursor.url
        assert retry.detail_checkpoint == checkpoint
        assert fetcher.requested.count(f"{_RIPPLING_JOBS}/{_RIPPLING_JOB}") == 1

    if checkpoint_scope != "matching":
        cursor = replace(
            cursor,
            detail_checkpoint=replace(
                checkpoint,
                platform="dover" if checkpoint_scope == "other-platform" else checkpoint.platform,
                board_url="https://ats.rippling.com/acme/jobs"
                if checkpoint_scope == "other-board"
                else checkpoint.board_url,
            ),
        )
    fetcher.interrupted = False
    recovered = fetcher.continue_listings(
        "Globex",
        (cursor,),
        root_url="https://ats.rippling.com/globex/jobs",
        handoff_scopes=(),
        options=options,
    )

    assert recovered.complete
    assert recovered.raw_count == 3
    assert recovered.deferred_targets == ()
    assert recovered.detail_coverage is not None
    assert fetcher.requested.count(f"{_RIPPLING_JOBS}/{third_id}") == 1
    assert fetcher.requested.count(f"{_RIPPLING_JOBS}/{_RIPPLING_JOB}") == (
        1 if failure == "deadline" else 2
    )
    assert f"{_RIPPLING_JOBS}/{_RIPPLING_SECOND}" not in fetcher.requested
    remapped = next(
        listing for listing in recovered.listings if listing.requisition_id == _RIPPLING_JOB
    )
    assert "Northtown, NT" in remapped.facts.locations.values
    assert "Canada" in remapped.facts.locations.values
    assert remapped.facts.locations.complete
    assert remapped.facts.arrangements.complete


@pytest.mark.parametrize("ambiguity", ["conflicting-row", "invalid-locations", "missing-page"])
@pytest.mark.parametrize("country", ["United States", "Canada"])
def test_ambiguous_rippling_location_evidence_keeps_canada_candidates_available_for_review(
    ambiguity: str,
    country: str,
) -> None:
    first = _rippling_row(
        locations=[
            {
                "name": country,
                "countryCode": "US" if country == "United States" else "CA",
                "workplaceType": "REMOTE",
            }
        ]
    )
    rows = [first, _rippling_placed_row("ON_SITE")]
    if ambiguity == "conflicting-row":
        rows[1]["name"] = "Clinical Systems Analyst"
    elif ambiguity == "invalid-locations":
        rows[1]["locations"] = [None]
    pages = {_RIPPLING_JOBS: _rippling_page(rows)}
    if ambiguity == "missing-page":
        pages[_RIPPLING_JOBS] = _rippling_page([first], total_items=2, total_pages=2)
        pages[f"{_RIPPLING_JOBS}?page=1"] = {}
    read = _ScriptedRippling(pages).listings("Globex", "https://ats.rippling.com/globex/jobs")
    [listing] = read.listings
    parameters = JobSearchParameters(
        locations=[Location(key="canada", country="Canada")],
        seniority=Seniority(target_titles=titles("Clinical Data Engineer")),
    )

    screening = screen_discovery(
        listing.employer,
        listing.title,
        listing.location,
        parameters,
        ["Clinical Data Engineer"],
        listing.facts,
    )

    assert not read.complete
    assert not listing.facts.locations.complete
    assert not listing.facts.arrangements.complete
    assert "Northtown, NT" not in listing.facts.locations.values
    assert ScreeningCode.LOCATION_OUTSIDE not in {finding.code for finding in screening.findings}
    assert supports_bulk_staging(screening, include_bounded_review=True)
    if country == "Canada":
        assert "Canada" in listing.facts.locations.values


def test_partial_rippling_locations_do_not_certify_an_arrangement_inferred_from_a_place() -> None:
    rows = [
        _rippling_row(locations=[{"name": "United States - Remote"}]),
        _rippling_placed_row("ON_SITE") | {"name": "Clinical Systems Analyst"},
    ]
    read = _ScriptedRippling({_RIPPLING_JOBS: _rippling_page(rows)}).listings(
        "Globex", "https://ats.rippling.com/globex/jobs"
    )

    [listing] = read.listings
    assert not listing.facts.locations.complete
    assert not listing.facts.arrangements.complete
    assert listing.facts.arrangements.unresolved_values


def test_rippling_later_page_cancellation_propagates_without_reading_details() -> None:
    class CancelledPage(_ScriptedRippling):
        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            if url.endswith("?page=1"):
                raise asyncio.CancelledError
            return super().get_json(url, headers=headers)

    fetcher = CancelledPage(
        {_RIPPLING_JOBS: _rippling_page([_rippling_row()], total_items=2, total_pages=2)}
    )

    with pytest.raises(asyncio.CancelledError):
        fetcher.listings("Globex", "https://ats.rippling.com/globex/jobs")

    assert fetcher.requested == [_RIPPLING_JOBS]


def test_rippling_first_page_transport_failure_keeps_its_exception_contract() -> None:
    class FailedFirstPage(_ScriptedRippling):
        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            raise httpx.ReadError("connection lost", request=httpx.Request("GET", url))

    with pytest.raises(httpx.ReadError):
        FailedFirstPage({}).listings("Globex", "https://ats.rippling.com/globex/jobs")


def test_a_rippling_listing_falls_back_to_the_detail_places_without_a_list_row() -> None:
    record = {"row": {"id": _RIPPLING_JOB}, "detail": _rippling_detail()}

    listing = source_listing(RIPPLING_MAPPING, Posting(record, groups={"org": "globex"}))

    assert listing is not None
    assert listing.url == f"https://ats.rippling.com/globex/jobs/{_RIPPLING_JOB}"
    assert listing.location == "Remote (United States)"
    assert listing.facts.arrangements.values == ("remote",)


def _rippling_placed_row(*workplaces: str) -> dict[str, Any]:
    return _rippling_row(
        locations=[
            {"name": "Northtown, NT", "countryCode": "CA", "workplaceType": workplace}
            for workplace in workplaces
        ]
    )


@pytest.mark.parametrize(
    ("workplace", "arrangement"), [("HYBRID", "hybrid"), ("ON_SITE", "onsite")]
)
def test_a_rippling_attendance_workplace_type_reads_as_its_arrangement(
    workplace: str, arrangement: str
) -> None:
    record = {"row": _rippling_placed_row(workplace), "detail": _rippling_detail()}

    listing = source_listing(RIPPLING_MAPPING, Posting(record, groups={"org": "globex"}))

    assert listing is not None
    assert listing.facts.arrangements.values == (arrangement,)


def test_an_unknown_rippling_workplace_type_is_unresolved() -> None:
    record = {"row": _rippling_placed_row("FLEXIBLE"), "detail": _rippling_detail()}

    listing = source_listing(RIPPLING_MAPPING, Posting(record, groups={"org": "globex"}))

    assert listing is not None
    assert listing.facts.arrangements.values == ()
    assert listing.facts.arrangements.unresolved_values == ("workplaceType: FLEXIBLE",)


def test_rippling_places_stating_different_workplace_types_name_no_arrangement() -> None:
    record = {"row": _rippling_placed_row("HYBRID", "ON_SITE"), "detail": _rippling_detail()}

    listing = source_listing(RIPPLING_MAPPING, Posting(record, groups={"org": "globex"}))

    assert listing is not None
    assert listing.facts.arrangements.values == ()
    assert listing.facts.arrangements.unresolved_values == ("hybrid", "onsite")


_ADP_CID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
_ADP_BOARD = (
    "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
    f"?cid={_ADP_CID}&ccId=19000101_000001&type=MP&lang=en_CA"
)
_ADP_API = (
    "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/"
    "job-requisitions"
)
_ADP_QUERY = f"?cid={_ADP_CID}&ccId=19000101_000001&lang=en_CA&locale=en_CA"
_ADP_JOB = "9200000000001_1"


def _adp_requisition(**stated: Any) -> dict[str, Any]:
    return {
        "itemID": _ADP_JOB,
        "requisitionTitle": "Data Analyst",
        "postDate": "2026-10-01T16:51:00.000-04:00",
        "clientRequisitionID": "1001",
        "requisitionLocations": [
            {
                "address": {
                    "cityName": "Springfield",
                    "countrySubdivisionLevel1": {"codeValue": "ON"},
                    "countryCode": "CA",
                    "postalCode": "A1A 1A1",
                },
                "nameCode": {"shortName": " Springfield, ON, CA"},
            }
        ],
        "payGradeRange": {
            "minimumRate": {"amountValue": 90000.0, "currencyCode": "CAD"},
            "maximumRate": {"amountValue": 110000.0, "currencyCode": "CAD"},
        },
        "workLevelCode": {"shortName": "FULL TIME"},
        **stated,
    }


def _adp_client(
    rows: list[dict[str, Any]],
    *,
    total: int | str | None,
    requested: list[str],
    dropped: frozenset[str] = frozenset(),
    description: str = "<p>Analyze clinical claims data.</p>",
    career_center: str = "",
) -> PoliteHttpClient:
    def answer(url: str) -> httpx.Response:
        requested.append(url)
        request = httpx.Request("GET", url)
        path, _, query = url.partition("?")
        if career_center and parse_qs(query).get("ccId") != [career_center]:
            return httpx.Response(200, request=request, json={"jobRequisitions": []})
        if path.removeprefix(f"{_ADP_API}/") in dropped:
            raise httpx.RemoteProtocolError("Server disconnected", request=request)
        if path == _ADP_API:
            # ADP's `$skip` is 1-based: skip=1 starts at the first requisition.
            stated = parse_qs(query)
            skip = int(stated.get("$skip", ["1"])[0])
            top = int(stated.get("$top", [str(len(rows))])[0])
            page = rows[skip - 1 : skip - 1 + top]
            return httpx.Response(
                200,
                request=request,
                json={
                    "jobRequisitions": page,
                    "meta": {} if total is None else {"totalNumber": total},
                },
            )
        item = path.removeprefix(f"{_ADP_API}/")
        body = _adp_requisition(itemID=item, requisitionDescription=description)
        return httpx.Response(200, request=request, json=body)

    return policy_client(answer)


def test_adp_reads_every_requisition_and_its_detail_body() -> None:
    requested: list[str] = []
    fetcher = BoardApiFetcher(
        client=_adp_client([_adp_requisition()], total=1, requested=requested)
    )

    read = fetcher.listings("Globex", _ADP_BOARD)

    assert requested[-1] == f"{_ADP_API}/{_ADP_JOB}{_ADP_QUERY}"
    assert read.complete is True
    [listing] = read.listings
    assert listing.title == "Data Analyst"
    assert listing.url == (
        "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
        f"?cid={_ADP_CID}&ccId=19000101_000001&jobId={_ADP_JOB}&lang=en_CA"
    )
    assert listing.employer == "Globex"
    assert listing.requisition_id == "1001"
    assert listing.location == "Springfield, ON, CA"
    assert listing.facts.locations.values == ("Springfield, ON, CA", "Canada")
    assert listing.facts.compensation.values == ("CAD 90000-110000",)
    assert listing.facts.employment_types.values == ("FULL TIME",)
    assert listing.published_description == "Analyze clinical claims data."
    assert (read.detail.attempted, read.detail.read, read.detail.failed) == (1, 1, 0)


@pytest.mark.parametrize("authoritative", [False, True])
@pytest.mark.parametrize(
    ("description", "company"),
    [
        pytest.param("<p>Company: Initech</p>", "Initech", id="plain-label"),
        pytest.param("<p><strong>Company:</strong> Initech</p>", "Initech", id="inline-label"),
        pytest.param(
            "<p>Company: <span>Initech</span> &amp; <b>Globex</b></p>",
            "Initech & Globex",
            id="entities",
        ),
        pytest.param("<p>  cOmPaNy  :  Initech  </p>", "Initech", id="case-and-spacing"),
        pytest.param("<p>Company:<br>Initech<br>Services</p>", "Initech Services", id="breaks"),
        pytest.param("<p>Company: Initech\nServices</p>", "Initech Services", id="wrapped-name"),
        pytest.param("\n<p></p><p>Company: Initech</p>", "Initech", id="leading-empty-paragraph"),
    ],
)
def test_an_adp_board_reads_the_leading_company_label_over_its_source_name(
    description: str, company: str, authoritative: bool
) -> None:
    published = f"{description}<p>Analyze clinical claims data.</p>"
    fetcher = BoardApiFetcher(
        client=_adp_client([_adp_requisition()], total=1, requested=[], description=published)
    )

    read = fetcher.listings("Globex", _ADP_BOARD, employer_is_authoritative=authoritative)

    assert read.complete
    [listing] = read.listings
    assert listing.employer == company
    assert listing.facts.identity_trust == ListingIdentityTrust.PUBLISHER
    assert listing.published_description == strip_tags(published)
    check = posting_check(
        ADP_MAPPING,
        Posting(
            {"detail": _adp_requisition(requisitionDescription=published)},
            employer="Globex",
            employer_is_authoritative=authoritative,
        ),
        note="Synthetic ADP posting",
    )
    assert check.employer == company
    assert check.text == listing.published_description


@pytest.mark.parametrize("authoritative", [False, True])
@pytest.mark.parametrize(
    "description",
    [
        pytest.param("<p>Analyze clinical claims data.</p>", id="missing-label"),
        pytest.param("<p>Company:</p><p>Initech develops clinical software.</p>", id="empty-label"),
        pytest.param("<p>Company: &nbsp; </p><p>Analyze claims.</p>", id="blank-label"),
        pytest.param("<p>Analyze claims.</p><p>Company: Initech</p>", id="later-label"),
        pytest.param("<p>Our company is Initech.</p>", id="company-prose"),
        pytest.param("<p>Company overview: Initech</p>", id="different-label"),
        pytest.param("", id="missing-description"),
    ],
)
def test_an_adp_body_without_a_leading_company_value_keeps_the_source_name_and_trust(
    description: str, authoritative: bool
) -> None:
    fetcher = BoardApiFetcher(
        client=_adp_client([_adp_requisition()], total=1, requested=[], description=description)
    )

    read = fetcher.listings("Globex", _ADP_BOARD, employer_is_authoritative=authoritative)

    [listing] = read.listings
    assert listing.employer == "Globex"
    assert listing.facts.identity_trust == ListingIdentityTrust.PUBLISHER
    assert listing.published_description == strip_tags(description)


@pytest.mark.parametrize("apply_address", [False, True])
@pytest.mark.parametrize("same_tenant", [False, True])
def test_an_adp_read_skips_a_protected_item_before_its_detail_request(
    apply_address: bool, same_tenant: bool
) -> None:
    second_item = "9200000000002_1"
    canonical = (
        f"{_ADP_BOARD.partition('?')[0]}?cid={_ADP_CID}&ccId=19000101_000001"
        f"&jobId={_ADP_JOB}&lang=en_CA"
    )
    protected_url = canonical.replace("&ccId=19000101_000001", "")
    if apply_address:
        protected_url = protected_url.replace("lang=en_CA", "lang=en_US") + "&mode=apply"
    if not same_tenant:
        protected_url = protected_url.replace(_ADP_CID, "11111111-2222-4333-8444-555555555555")
    requested: list[str] = []
    fetcher = BoardApiFetcher(
        client=_adp_client(
            [
                _adp_requisition(itemID=_ADP_JOB, clientRequisitionID="1001"),
                _adp_requisition(itemID=second_item, clientRequisitionID="1001"),
            ],
            total=2,
            requested=requested,
        )
    )

    read = fetcher.listings(
        "Globex",
        _ADP_BOARD,
        options=SourceReadOptions(protected_posting_urls=frozenset({protected_url})),
    )

    expected_items = [second_item] if same_tenant else [_ADP_JOB, second_item]
    assert [url for url in requested if url.partition("?")[0] != _ADP_API] == [
        f"{_ADP_API}/{item}{_ADP_QUERY}" for item in expected_items
    ]
    assert [listing.url for listing in read.listings] == [
        canonical.replace(_ADP_JOB, item) for item in expected_items
    ]
    assert [listing.requisition_id for listing in read.listings] == ["1001"] * len(expected_items)
    assert set(read.detail_read_urls or ()) == {listing.url for listing in read.listings}
    assert read.raw_count == 2
    assert read.complete
    assert (read.detail.attempted, read.detail.read, read.detail.failed) == (
        len(expected_items),
        len(expected_items),
        0,
    )
    assert read.skip_reasons == ({"protected_posting": 1} if same_tenant else {})
    assert read.protected_posting_urls == ((canonical,) if same_tenant else ())
    assert read.detail_coverage is not None
    assert set(read.detail_coverage.posting_urls) == {
        canonical,
        canonical.replace(_ADP_JOB, second_item),
    }


def test_an_adp_board_stating_more_requisitions_than_it_returned_is_not_complete() -> None:
    requested: list[str] = []
    fetcher = BoardApiFetcher(
        client=_adp_client([_adp_requisition()], total=2, requested=requested)
    )

    read = fetcher.listings("Globex", _ADP_BOARD)

    assert [listing.title for listing in read.listings] == ["Data Analyst"]
    assert read.complete is False
    assert read.outcome is SourceReadOutcome.NO_SUPPORTED_REPRESENTATION


def _list_requests(requested: list[str]) -> list[dict[str, list[str]]]:
    return [parse_qs(url.partition("?")[2]) for url in requested if url.startswith(_ADP_API + "?")]


def test_adp_pages_its_requisitions_from_a_one_based_skip() -> None:
    rows = [_adp_requisition(itemID=f"92000000000{index:02d}_1") for index in range(45)]
    requested: list[str] = []
    fetcher = BoardApiFetcher(client=_adp_client(rows, total=len(rows), requested=requested))

    read = fetcher.listings("Globex", _ADP_BOARD)

    pages = _list_requests(requested)
    top = int(pages[0]["$top"][0])
    assert [page["$skip"] for page in pages[:2]] == [["1"], [str(1 + top)]]
    assert read.complete is True
    assert len({listing.url for listing in read.listings}) == len(rows)


@pytest.mark.parametrize("career_center", ["19000101_000001_2", "19000101_000001_3"])
@pytest.mark.parametrize("center_first", [False, True])
def test_adp_list_pages_and_postings_keep_the_requested_career_center(
    career_center: str, center_first: bool
) -> None:
    parts = [f"cid={_ADP_CID}", f"ccId={career_center}", "lang=en_CA"]
    if center_first:
        parts.reverse()
    board = f"{_ADP_BOARD.partition('?')[0]}?{'&'.join(parts)}"
    rows = [_adp_requisition(itemID=f"92000000000{index:02d}_1") for index in range(45)]
    requested: list[str] = []
    fetcher = BoardApiFetcher(
        client=_adp_client(rows, total=len(rows), requested=requested, career_center=career_center)
    )

    read = fetcher.listings("Globex", board)

    assert all(parse_qs(url.partition("?")[2]).get("ccId") == [career_center] for url in requested)
    assert len(_list_requests(requested)) == 3
    assert read.complete
    assert len(read.listings) == len(rows)
    assert all(
        parse_qs(listing.url.partition("?")[2]).get("ccId") == [career_center]
        for listing in read.listings
    )


@pytest.mark.parametrize("separator", ["&amp;", "&#038;", "&#x26;"])
def test_a_publisher_encoded_adp_board_reads_its_published_career_center(
    separator: str,
) -> None:
    publisher = "https://careers.example.org/jobs"
    career_center = "19000101_000001_2"
    parts = [f"cID={_ADP_CID}", f"ccId={career_center}", "lang=en_CA"]
    board = f"{_ADP_BOARD.partition('?')[0]}?{separator.join(parts)}"
    rows = [_adp_requisition(itemID=f"92000000000{index:02d}_1") for index in range(4)]
    requested: list[str] = []

    def answer(url: str) -> httpx.Response:
        request = httpx.Request("GET", url)
        if url == publisher:
            return httpx.Response(200, request=request, text=f'<a href="{board}">Careers</a>')
        requested.append(url)
        path, _, query = url.partition("?")
        if path == _ADP_API:
            scoped = parse_qs(query)
            if scoped.get("ccId") != [career_center] or scoped.get("lang") != ["en_CA"]:
                return httpx.Response(200, request=request, json={"jobRequisitions": []})
            return httpx.Response(
                200,
                request=request,
                json={"jobRequisitions": rows, "meta": {"totalNumber": len(rows)}},
            )
        return httpx.Response(
            200,
            request=request,
            json=_adp_requisition(itemID=path.removeprefix(f"{_ADP_API}/")),
        )

    read = BoardApiFetcher(client=policy_client(answer)).listings("Globex", publisher)

    assert read.complete
    assert len(read.listings) == len(rows)
    assert len(_list_requests(requested)) == 1
    for url in requested:
        query = parse_qs(url.partition("?")[2])
        assert query["cid"] == [_ADP_CID]
        assert query["ccId"] == [career_center]
        assert query["lang"] == ["en_CA"]
    assert all(
        parse_qs(listing.url.partition("?")[2])["ccId"] == [career_center]
        for listing in read.listings
    )


def test_an_adp_client_only_board_keeps_its_supported_request_shape() -> None:
    board = _ADP_BOARD.replace("&ccId=19000101_000001", "")
    requested: list[str] = []
    read = BoardApiFetcher(
        client=_adp_client([_adp_requisition()], total=1, requested=requested)
    ).listings("Globex", board)

    assert read.complete
    [listing] = read.listings
    assert listing.url == f"{board.partition('?')[0]}?cid={_ADP_CID}&jobId={_ADP_JOB}&lang=en_CA"
    assert all("ccId" not in parse_qs(url.partition("?")[2]) for url in requested)


@pytest.mark.parametrize(
    "total", [None, False, "0", -1, 0], ids=["absent", "boolean", "string", "negative", "zero"]
)
def test_an_empty_adp_board_is_complete_only_when_it_states_zero(total: int | str | None) -> None:
    requested: list[str] = []
    read = BoardApiFetcher(client=_adp_client([], total=total, requested=requested)).listings(
        "Globex", _ADP_BOARD
    )

    assert not read.listings
    stated_zero = type(total) is int and total == 0
    assert read.complete is stated_zero
    if not stated_zero:
        assert read.outcome is SourceReadOutcome.NO_SUPPORTED_REPRESENTATION
        assert read.detail_coverage is None
        assert "returned no requisitions without a valid total" in read.note
        assert "career-center address" in read.note


def test_a_dropped_detail_read_keeps_the_other_requisitions_and_marks_the_read_incomplete() -> None:
    rows = [_adp_requisition(itemID=f"920000000000{index}_1") for index in range(3)]
    fetcher = BoardApiFetcher(
        client=_adp_client(rows, total=3, requested=[], dropped=frozenset({"9200000000001_1"}))
    )

    read = fetcher.listings("Globex", _ADP_BOARD)

    assert len(read.listings) == 2
    assert read.complete is False
    assert "1 of 3" in read.note
    assert "time_limit_reached" not in read.skip_reasons
    assert (read.detail.attempted, read.detail.read, read.detail.failed) == (3, 2, 1)
    assert read.detail.failure_reasons == (("other", 1),)
    assert read.detail.failure_examples == ("9200000000001_1",)


@pytest.mark.parametrize("failure", ["timeout", "http_status", "other", "transport"])
def test_a_detail_failure_keeps_its_typed_reason_before_the_deadline(failure: str) -> None:
    class FailedDetailAdp(BoardApiFetcher):
        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            if url.partition("?")[0] == _ADP_API:
                return {"jobRequisitions": [_adp_requisition()], "meta": {"totalNumber": 1}}
            request = httpx.Request("GET", url)
            if failure == "timeout":
                raise httpx.ReadTimeout("timed out", request=request)
            if failure == "transport":
                raise httpx.ReadError("connection lost", request=request)
            if failure == "other":
                raise FetchPolicyError("Publisher policy refused the detail")
            response = httpx.Response(503, request=request)
            raise httpx.HTTPStatusError("Service unavailable", request=request, response=response)

    read = FailedDetailAdp().listings("Globex", _ADP_BOARD)

    assert read.listings == []
    assert (read.detail.attempted, read.detail.read, read.detail.failed) == (1, 0, 1)
    assert read.detail.interrupted == 0
    assert read.detail.capped == 0
    assert read.detail.failure_reasons == (("other" if failure == "transport" else failure, 1),)


@pytest.mark.parametrize(("paced", "reason"), [(False, "host_held"), (True, "time_limit_reached")])
def test_a_host_hold_before_a_detail_is_not_a_started_or_failed_request(
    paced: bool, reason: str
) -> None:
    class HeldDetailAdp(BoardApiFetcher):
        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            if url.partition("?")[0] == _ADP_API:
                return {"jobRequisitions": [_adp_requisition()], "meta": {"totalNumber": 1}}
            raise HostHeldError(url, ready_at=100, paced=paced)

    read = HeldDetailAdp().listings("Globex", _ADP_BOARD)

    assert read.complete is False
    assert read.skip_reasons == {reason: 1}
    assert read.detail.attempted == read.detail.failed == read.detail.interrupted == 0
    assert read.detail.capped == int(paced)
    assert read.detail.failure_reasons == ()
    assert len(read.deferred_targets) == 1
    target = read.deferred_targets[0]
    assert target.detail_checkpoint is not None
    assert target.detail_checkpoint.pending_ids == (_ADP_JOB,)
    assert target.automatic_eligible is paced
    assert target.deferred_reason == "adp_detail_rows_pending"
    assert ("publisher requested a wait" in read.note) is not paced
    assert ("deadline" in read.note) is paced


def test_a_list_row_without_an_identifier_does_not_count_as_a_detail_attempt() -> None:
    fetcher = BoardApiFetcher(
        client=_adp_client([_adp_requisition(itemID=""), _adp_requisition()], total=2, requested=[])
    )

    read = fetcher.listings("Globex", _ADP_BOARD)

    assert len(read.listings) == 1
    assert (read.detail.attempted, read.detail.read, read.detail.failed) == (1, 1, 0)


@pytest.mark.parametrize("cut_at", ["before-first", "between-details", "during-a-detail"])
def test_details_the_read_deadline_cuts_off_are_a_time_limit_and_not_failed_reads(
    monkeypatch: pytest.MonkeyPatch, cut_at: str
) -> None:
    now = [100.0]
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.board_adapters.transport.time.monotonic",
        lambda: now[0],
    )
    rows = [_adp_requisition(itemID=f"920000000000{index}_1") for index in range(3)]
    details: list[str] = []

    class DeadlineAdp(BoardApiFetcher):
        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            path = url.partition("?")[0]
            if path == _ADP_API:
                if cut_at == "before-first":
                    now[0] += 60.0
                return {"jobRequisitions": rows, "meta": {"totalNumber": len(rows)}}
            details.append(path.removeprefix(f"{_ADP_API}/"))
            if len(details) == 1:
                if cut_at == "between-details":
                    now[0] += 60.0
                return _adp_requisition(itemID=details[-1])
            now[0] += 60.0
            raise httpx.ReadTimeout("timed out", request=httpx.Request("GET", url))

    read = DeadlineAdp().listings("Globex", _ADP_BOARD)

    expected_read = 0 if cut_at == "before-first" else 1
    expected_interrupted = int(cut_at == "during-a-detail")
    assert len(details) == expected_read + expected_interrupted
    assert len(read.listings) == expected_read
    assert read.complete is False
    assert read.skip_reasons.get("time_limit_reached") == 1
    assert "failed" not in read.note
    assert f"{3 - expected_read} of 3" in read.note
    assert (read.detail.attempted, read.detail.read, read.detail.failed) == (
        expected_read + expected_interrupted,
        expected_read,
        0,
    )
    assert read.detail.interrupted == expected_interrupted
    assert read.detail.capped == 3 - expected_read - expected_interrupted
    assert read.detail.failure_reasons == ()


def test_detail_rows_the_deadline_cuts_off_resume_at_the_first_unread_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.board_adapters.transport.time.monotonic",
        lambda: now[0],
    )
    rows = [
        _adp_requisition(itemID=f"920000000000{index}_1", requisitionTitle=f"Analyst {index}")
        for index in range(5)
    ]
    details: list[str] = []
    seconds_per_detail = [20.0]

    class DeadlineAdp(BoardApiFetcher):
        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            path = url.partition("?")[0]
            if path == _ADP_API:
                return {"jobRequisitions": rows, "meta": {"totalNumber": len(rows)}}
            item = path.removeprefix(f"{_ADP_API}/")
            details.append(item)
            now[0] += seconds_per_detail[0]
            return next(row for row in rows if row["itemID"] == item)

    fetcher = DeadlineAdp()
    read = fetcher.listings("Initech", _ADP_BOARD)

    assert [listing.title for listing in read.listings] == ["Analyst 0", "Analyst 1"]
    assert read.raw_count == 5
    assert read.complete is False
    assert (read.detail.attempted, read.detail.read, read.detail.capped) == (2, 2, 3)
    [cursor] = read.deferred_targets
    assert cursor.detail_checkpoint is not None
    assert cursor.detail_checkpoint.pending_ids == tuple(row["itemID"] for row in rows[2:])

    seconds_per_detail[0] = 0.0
    details.clear()
    continued = fetcher.continue_listings(
        "Initech", (cursor,), root_url=_ADP_BOARD, handoff_scopes=()
    )

    assert details[0] == "9200000000002_1"
    assert [listing.title for listing in continued.listings] == [
        "Analyst 2",
        "Analyst 3",
        "Analyst 4",
    ]
    assert continued.complete is True
    assert continued.deferred_targets == ()
    assert (continued.detail.attempted, continued.detail.read, continued.detail.capped) == (3, 3, 0)


@pytest.mark.parametrize("name", ["adp", "breezy", "rippling", "dover", "bamboohr", "jazzhr"])
def test_every_detail_board_resumes_its_own_cut_detail_rows(name: str) -> None:
    assert f"{name}_detail_rows_pending" in BOARD_ADAPTER_REGISTRY[name].continuations
    assert f"{name}_detail_rows_pending" in STRUCTURED_PAGE_CURSOR_REASONS


def test_a_robots_refused_detail_read_still_fails_the_board_read() -> None:
    def answer(url: str) -> httpx.Response:
        request = httpx.Request("GET", url)
        if url.startswith(_ADP_API + "?"):
            body = {"jobRequisitions": [_adp_requisition()], "meta": {"totalNumber": 1}}
            return httpx.Response(200, request=request, json=body)
        raise RobotsDeniedError(url)

    fetcher = BoardApiFetcher(client=policy_client(answer))

    with pytest.raises(RobotsDeniedError):
        fetcher.listings("Globex", _ADP_BOARD)


def test_an_adp_read_that_fills_every_page_with_no_stated_total_is_not_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adp, "ADP_MAX_PAGES", 2)
    rows = [_adp_requisition(itemID=f"92000000000{index:02d}_1") for index in range(60)]
    fetcher = BoardApiFetcher(client=_adp_client(rows, total=None, requested=[]))

    read = fetcher.listings("Globex", _ADP_BOARD)

    assert read.complete is False
    assert read.outcome is SourceReadOutcome.NO_SUPPORTED_REPRESENTATION


_DOVER_CLIENT = "7c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f"
_DOVER_JOBS = f"https://app.dover.io/api/v1/careers-page/{_DOVER_CLIENT}/jobs"
_DOVER_DETAIL = "https://app.dover.io/api/v1/inbound/application-portal-job"
_DOVER_JOB = "9e8d7c6b-5a49-4382-a716-f5e4d3c2b1a0"


def _dover_row(job_id: str = _DOVER_JOB, **stated: Any) -> dict[str, Any]:
    return {
        "id": job_id,
        "title": "Clinical Data Engineer",
        "locations": [
            {
                "location_type": "REMOTE",
                "location_option": {
                    "display_name": "United States",
                    "location_type": "COUNTRY",
                    "country": "US",
                },
                "name": "United States",
            },
            {
                "location_type": "REMOTE",
                "location_option": {
                    "display_name": "Northtown, NT",
                    "location_type": "CITY",
                    "country": "CA",
                },
                "name": "Northtown, NT",
            },
        ],
        "workplace_type": "REMOTE",
        "is_published": True,
        "is_sample": False,
        **stated,
    }


def _dover_detail(**stated: Any) -> dict[str, Any]:
    return {
        "id": _DOVER_JOB,
        "client_id": _DOVER_CLIENT,
        "client_name": "Globex Health",
        "title": "Clinical Data Engineer",
        "location": "United States",
        "user_provided_description": "<p>Build clinical data pipelines.</p>",
        **stated,
    }


class _ScriptedDover(BoardApiFetcher):
    def __init__(self, board: dict[str, Any]) -> None:
        self.board = board
        self.requested: list[str] = []

    def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
        self.requested.append(url)
        return self.board if url == _DOVER_JOBS else _dover_detail()


@pytest.mark.parametrize("host", ["app.dover.io", "app.dover.com"])
def test_dover_reads_its_published_jobs_with_detail_bodies_and_skips_samples(host: str) -> None:
    fetcher = _ScriptedDover(
        {
            "count": 3,
            "results": [
                _dover_row(),
                _dover_row("11111111-2222-4333-8444-555555555555", is_sample=True),
                _dover_row("66666666-7777-4888-9999-000000000000", is_published=False),
            ],
        }
    )

    read = fetcher.listings("Globex", f"https://{host}/dover/careers/{_DOVER_CLIENT}?embed=1")

    assert fetcher.requested == [_DOVER_JOBS, f"{_DOVER_DETAIL}/{_DOVER_JOB}"]
    assert read.complete is True
    assert read.raw_count == 1
    assert read.skip_reasons == {"sample_or_unpublished_posting": 2}
    [listing] = read.listings
    assert listing.title == "Clinical Data Engineer"
    assert listing.url == f"https://app.dover.io/apply/Globex%20Health/{_DOVER_JOB}"
    assert listing.employer == "Globex Health"
    assert listing.requisition_id == _DOVER_JOB
    assert listing.facts.locations.values == ("United States", "Northtown, NT", "Canada")
    assert listing.facts.arrangements.values == ("remote",)
    assert listing.published_description == "Build clinical data pipelines."


def test_a_dover_board_stating_more_jobs_than_it_returned_is_not_complete() -> None:
    fetcher = _ScriptedDover({"count": 2, "results": [_dover_row()]})

    read = fetcher.listings("Globex", f"https://app.dover.io/dover/careers/{_DOVER_CLIENT}")

    assert [listing.title for listing in read.listings] == ["Clinical Data Engineer"]
    assert read.complete is False
    assert read.outcome is SourceReadOutcome.NO_SUPPORTED_REPRESENTATION


def test_a_dover_detail_states_its_pay_bounds_and_currency() -> None:
    pay = {"lower_bound": 120000, "upper_bound": 150000, "currency_code": "USD"}
    record = {"row": _dover_row(), "detail": _dover_detail(compensation=pay)}

    listing = source_listing(DOVER_MAPPING, Posting(record, employer="Globex"))

    assert listing is not None
    assert listing.facts.compensation.values == ("USD 120000-150000",)


def test_a_dover_workplace_type_other_than_remote_is_unresolved() -> None:
    record = {"row": _dover_row(workplace_type="HYBRID"), "detail": _dover_detail()}

    listing = source_listing(DOVER_MAPPING, Posting(record, employer="Globex"))

    assert listing is not None
    assert listing.facts.arrangements.values == ()
    assert listing.facts.arrangements.unresolved_values == ("workplace_type: HYBRID",)


_BREEZY_FEED = "https://acme.breezy.hr/json"
_BREEZY_PAGE = "https://acme.breezy.hr/p/7a86407-data-analyst"
_BREEZY_DECLARED_PAGE = (
    '<html><head><script type="application/ld+json">'
    '{"@context": "https://schema.org", "@type": "JobPosting", "title": "Data Analyst",'
    ' "description": "<p>Analyze clinical claims data.</p>"}'
    "</script></head><body><h1>Data Analyst</h1></body></html>"
)


def _breezy_location(**stated: Any) -> dict[str, Any]:
    return {
        "country": {"name": "United States", "id": "US"},
        "state": {"id": "NC", "name": "North Carolina"},
        "city": "Springfield",
        "primary": True,
        "is_remote": True,
        "remote_details": {"value": "remote", "label": "Fully remote, no location restrictions"},
        "name": "Springfield, NC",
        "id": "e55775",
        **stated,
    }


def _breezy_position(**stated: Any) -> dict[str, Any]:
    return {
        "id": "7a86407",
        "friendly_id": "7a86407-data-analyst",
        "name": "Data Analyst",
        "url": _BREEZY_PAGE,
        "published_date": "2026-09-17T15:20:45.752Z",
        "type": {"id": "fullTime", "name": "Full-Time"},
        "location": _breezy_location(),
        "department": "Analytics",
        "salary": "$50,000 - $70,000 / year",
        "company": {"name": "Acme, Inc."},
        **stated,
    }


def test_a_breezy_position_states_every_listed_location() -> None:
    position = _breezy_position(
        locations=[
            _breezy_location(),
            _breezy_location(
                name="Riverton, ON",
                city="Riverton",
                country={"name": "Canada", "id": "CA"},
                primary=False,
            ),
        ]
    )
    record = {"row": position, "detail": {}}

    listing = source_listing(
        BREEZY_MAPPING, Posting(record, employer="Acme", groups={"tenant": "acme"})
    )

    assert listing is not None
    assert listing.facts.locations.values == (
        "Springfield, NC",
        "Riverton, ON",
        "United States",
        "Canada",
    )


def _breezy_fetcher(
    positions: list[dict[str, Any]], *, requested: list[str], page: str = _BREEZY_DECLARED_PAGE
) -> BoardApiFetcher:
    def answer(url: str) -> httpx.Response:
        requested.append(url)
        request = httpx.Request("GET", url)
        if url == _BREEZY_FEED:
            return httpx.Response(200, request=request, json=positions)
        return httpx.Response(200, request=request, text=page)

    return BoardApiFetcher(client=policy_client(answer))


def test_breezy_reads_every_position_and_its_page_body() -> None:
    requested: list[str] = []
    fetcher = _breezy_fetcher([_breezy_position()], requested=requested)

    read = fetcher.listings("Acme", "https://acme.breezy.hr")

    assert requested == [_BREEZY_FEED, _BREEZY_PAGE]
    assert read.complete is True
    assert read.raw_count == 1
    [listing] = read.listings
    assert listing.title == "Data Analyst"
    assert listing.url == _BREEZY_PAGE
    assert listing.employer == "Acme, Inc."
    assert listing.requisition_id == "7a86407"
    assert listing.location == "Springfield, NC"
    assert listing.facts.locations.values == ("Springfield, NC", "United States")
    assert listing.facts.arrangements.values == ("remote",)
    assert listing.facts.employment_types.values == ("Full-Time",)
    assert listing.facts.compensation.values == ("$50,000 - $70,000 / year",)
    assert listing.published_description == "Analyze clinical claims data."


def test_a_breezy_position_whose_page_declares_no_posting_keeps_its_feed_fields() -> None:
    requested: list[str] = []
    fetcher = _breezy_fetcher(
        [_breezy_position()], requested=requested, page="<html><body>Apply</body></html>"
    )

    read = fetcher.listings("Acme", "https://acme.breezy.hr")

    assert read.complete is True
    [listing] = read.listings
    assert listing.title == "Data Analyst"
    assert listing.published_description == ""


@pytest.mark.parametrize(
    ("location", "arrangements", "unresolved"),
    [
        pytest.param(
            _breezy_location(is_remote=False, remote_details={"value": "hybrid"}),
            ("hybrid",),
            (),
            id="hybrid",
        ),
        pytest.param(
            _breezy_location(is_remote=False, remote_details={"value": "onsite"}),
            ("onsite",),
            (),
            id="onsite",
        ),
        pytest.param(
            _breezy_location(remote_details=None), ("remote",), (), id="remote-flag-alone"
        ),
        pytest.param(
            _breezy_location(is_remote=False, remote_details=None), (), (), id="nothing-stated"
        ),
        pytest.param(
            _breezy_location(remote_details={"value": "flexible"}),
            (),
            ("remote_details: flexible",),
            id="unknown-value",
        ),
    ],
)
def test_a_breezy_arrangement_reads_the_stated_remote_details(
    location: dict[str, Any], arrangements: tuple[str, ...], unresolved: tuple[str, ...]
) -> None:
    requested: list[str] = []
    fetcher = _breezy_fetcher([_breezy_position(location=location)], requested=requested)

    [listing] = fetcher.listings("Acme", "https://acme.breezy.hr").listings

    assert listing.requisition_id == "7a86407"
    assert listing.facts.arrangements.values == arrangements
    assert listing.facts.arrangements.unresolved_values == unresolved


_JAZZHR_BOARD = "https://acme.applytojob.com/apply"
_JAZZHR_ANALYST = f"{_JAZZHR_BOARD}/Xq7Lm2Pz9K/Clinical-Data-Analyst"
_JAZZHR_ENGINEER = f"{_JAZZHR_BOARD}/Rt4Wn8Bv3J/Data-Engineer"
_JAZZHR_LIST_PAGE = (
    "<html><body><ul>"
    f'<li><a href="{_JAZZHR_ANALYST}">Clinical Data Analyst</a></li>'
    '<li><a href="/apply/Rt4Wn8Bv3J/Data-Engineer">Data Engineer</a></li>'
    '<li><a href="/apply/Xq7Lm2Pz9K/Clinical-Data-Analyst">Apply</a></li>'
    "</ul>"
    '<a href="https://initech.applytojob.com/apply/Zz9Yy8Xx7W/Biostatistician">Partner</a>'
    "</body></html>"
)


def _jazzhr_posting_page(title: str, place: str, *, salary: str = "") -> str:
    return (
        '<html><head><script type="application/ld+json">'
        '{"@context": "https://schema.org", "@type": "JobPosting",'
        f' "title": "{title}", "datePosted": "2026-09-20",{salary}'
        ' "description": "<p>Analyze clinical claims data for care teams.</p>",'
        ' "jobLocation": {"@type": "Place", "address": {"@type": "PostalAddress",'
        f' "addressLocality": "{place}", "addressRegion": "ON", "addressCountry": "CA"}}}}'
        "}</script></head><body><h1>" + title + "</h1></body></html>"
    )


def _jazzhr_fetcher(pages: dict[str, str], *, requested: list[str]) -> BoardApiFetcher:
    def answer(url: str) -> httpx.Response:
        requested.append(url)
        return httpx.Response(200, request=httpx.Request("GET", url), text=pages[url])

    return BoardApiFetcher(client=policy_client(answer, robots="User-agent: *\nDisallow: /cb\n"))


def _jazzhr_rendered_page(
    *,
    title: str = '<div id="resumator-job-title"><h1>Data Engineer</h1></div>',
    description: str = (
        '<div id="resumator-job-description"><div class="richtext">'
        "<h2>Responsibilities</h2><p>Build data pipelines for Acme.</p>"
        "</div></div>"
    ),
    fields: str = "",
) -> str:
    return (
        '<html><head><script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"Organization","name":"Initech"}'
        "</script></head><body>"
        "<nav>Browse every opening</nav><aside>Other opportunities</aside>"
        + title
        + fields
        + description
        + "<form><label>Upload your resume</label><button>Apply now</button></form>"
        "</body></html>"
    )


def _jazzhr_responsive_page(
    *,
    description: str = (
        "<h1>About Acme</h1><div><p>Build data pipelines.</p>"
        "<div><h1>Responsibilities</h1><p>Review reliable SQL queries.</p></div></div>"
    ),
    fields: str = (
        '<div title="Location">Springfield, ON, Canada</div>'
        '<div id="resumator-job-employment" title="Type">Full Time</div>'
    ),
) -> str:
    return _jazzhr_rendered_page(
        title=(
            '<div class="job-header"><div class="container"><h2>Data Engineer</h2>'
            f'<div class="job-attributes-container">{fields}</div></div></div>'
        ),
        description=(
            '<div class="page-body job-details"><div class="container">'
            f'<div id="job-description" class="col col-xs-7 description">{description}</div>'
            '<div id="resumator-application-form"><form><label>Upload your resume</label>'
            '<input type="hidden" id="resumator-job-value" value="Rt4Wn8Bv3J">'
            "<button>Apply now</button></form></div></div></div>"
        ),
    )


@pytest.mark.parametrize("resumed", [False, True], ids=["list", "resume"])
@pytest.mark.parametrize(
    ("description", "expected"),
    [
        pytest.param(
            "<h1>About Acme</h1><div><p>Build data pipelines.</p>"
            "<div><h1>Responsibilities</h1><p>Review reliable SQL queries.</p></div></div>",
            "About Acme\n\nBuild data pipelines.\n\nResponsibilities\n\n"
            "Review reliable SQL queries.",
            id="nested-body-headings",
        ),
        pytest.param(
            "<p>Build &amp; review data.</p><ul><li>Write SQL.</li></ul>",
            "Build & review data.\n\n- Write SQL.",
            id="entities-and-lists",
        ),
        pytest.param(
            "<p>Build data.</p><script>const template = "
            "'<div class=job-header><h2>Other role</h2></div>';</script>"
            "<style>.job-header {display:none}</style>",
            "Build data.",
            id="script-and-style-templates",
        ),
    ],
)
def test_a_jazzhr_responsive_detail_reads_its_owned_title_body_and_fields(
    description: str, expected: str, resumed: bool
) -> None:
    requested: list[str] = []
    pages = {
        _JAZZHR_BOARD: _JAZZHR_LIST_PAGE,
        _JAZZHR_ANALYST: _jazzhr_posting_page("Clinical Data Analyst", "Springfield"),
        _JAZZHR_ENGINEER: "<html>No readable posting</html>"
        if resumed
        else _jazzhr_responsive_page(description=description),
    }
    fetcher = _jazzhr_fetcher(pages, requested=requested)
    read = fetcher.listings("Acme", _JAZZHR_BOARD)
    if resumed:
        assert not read.complete
        assert [listing.title for listing in read.listings] == ["Clinical Data Analyst"]
        [cursor] = read.deferred_targets
        assert cursor.detail_checkpoint is not None
        assert cursor.detail_checkpoint.failed_attempts == (("Rt4Wn8Bv3J", 1),)
        pages[_JAZZHR_ENGINEER] = _jazzhr_responsive_page(description=description)
        read = fetcher.continue_listings(
            "Acme", (cursor,), root_url=_JAZZHR_BOARD, handoff_scopes=()
        )

    assert read.complete
    engineer = next(listing for listing in read.listings if listing.url == _JAZZHR_ENGINEER)
    assert engineer.title == "Data Engineer"
    assert engineer.published_description == expected
    assert engineer.facts.locations.values == ("Springfield, ON, Canada",)
    assert not engineer.facts.locations.complete
    assert engineer.facts.employment_types.values == ("Full Time",)
    assert engineer.employer == "Acme"
    assert engineer.requisition_id == "Rt4Wn8Bv3J"
    assert read.detail.failed == 0
    assert read.detail_coverage is not None
    assert set(read.detail_coverage.posting_urls) == {_JAZZHR_ANALYST, _JAZZHR_ENGINEER}
    assert requested.count(_JAZZHR_ANALYST) == 1
    assert requested.count(_JAZZHR_ENGINEER) == (2 if resumed else 1)


@pytest.mark.parametrize(
    ("fields", "places", "employment"),
    [
        pytest.param("", (), (), id="missing-optional-fields"),
        pytest.param(
            '<div title="Location"> </div><div id="resumator-job-employment" title="Type"> </div>',
            (),
            (),
            id="empty-optional-fields",
        ),
        pytest.param(
            '<div title="Location">Springfield</div><div title="Location">Shelbyville</div>'
            '<div id="resumator-job-employment" title="Type">Full Time</div>'
            '<div id="resumator-job-employment" title="Type">Part Time</div>',
            (),
            (),
            id="ambiguous-optional-fields",
        ),
        pytest.param(
            '<div title="Location">Springfield</div>'
            '<div id="resumator-job-employment" title="Type">Type: Full Time</div>',
            ("Springfield",),
            ("Full Time",),
            id="labelled-type",
        ),
    ],
)
def test_a_jazzhr_responsive_detail_keeps_optional_fields_scoped_to_its_header(
    fields: str, places: tuple[str, ...], employment: tuple[str, ...]
) -> None:
    page = _jazzhr_responsive_page(fields=fields).replace(
        "</body>",
        '<nav><div title="Location">Elsewhere</div>'
        '<div id="resumator-job-employment" title="Type">Contract</div></nav></body>',
    )
    fetcher = _jazzhr_fetcher(
        {
            _JAZZHR_BOARD: _JAZZHR_LIST_PAGE,
            _JAZZHR_ANALYST: _jazzhr_posting_page("Clinical Data Analyst", "Springfield"),
            _JAZZHR_ENGINEER: page,
        },
        requested=[],
    )

    read = fetcher.listings("Acme", _JAZZHR_BOARD)

    assert read.complete
    engineer = read.listings[1]
    assert engineer.facts.locations.values == places
    assert not engineer.facts.locations.complete
    assert engineer.facts.employment_types.values == employment
    assert "Elsewhere" not in engineer.published_description
    assert "Upload your resume" not in engineer.published_description


@pytest.mark.parametrize(
    ("before", "after", "physical_detail"),
    [
        pytest.param('class="job-header"', 'class="other-header"', (2, 1, 1), id="missing-header"),
        pytest.param(
            '<div class="job-header">',
            '<section class="job-header">',
            (2, 1, 1),
            id="wrong-header-element",
        ),
        pytest.param(
            '</div></div></div><div class="page-body job-details">',
            '</div></div><div class="page-body job-details">',
            (2, 1, 1),
            id="truncated-header",
        ),
        pytest.param(
            '<div class="container"><h2>Data Engineer</h2>',
            '<h2>Data Engineer</h2><div class="container">',
            (2, 1, 1),
            id="title-outside-header-container",
        ),
        pytest.param(
            "</form></div></div></div>",
            "</form></div></div>",
            (2, 1, 1),
            id="truncated-body",
        ),
        pytest.param(
            '</div><div id="resumator-application-form">',
            '</div></div></div><div id="resumator-application-form">',
            (2, 1, 1),
            id="application-outside-owned-body",
        ),
        pytest.param(
            "<p>Build data pipelines.</p>",
            "<form><label>Upload a document</label></form>",
            (2, 1, 1),
            id="application-form-in-description",
        ),
        pytest.param(
            "<p>Build data pipelines.</p>",
            "<nav>Browse other jobs</nav>",
            (2, 1, 1),
            id="navigation-in-description",
        ),
        pytest.param(
            "</body>",
            '<div id="resumator-application-form"><form>Other form</form></body>',
            (2, 1, 1),
            id="truncated-duplicate-application",
        ),
        pytest.param(
            '<div class="job-header">',
            '<div class="job-header"><h2>Other role</h2></div><div class="job-header">',
            (2, 1, 1),
            id="duplicate-header",
        ),
        pytest.param(
            "</body>",
            '<div class="job-header"><h2>Other role</h2></body>',
            (2, 1, 1),
            id="truncated-duplicate-header",
        ),
        pytest.param(
            'class="job-header"', 'class="job-header-extra"', (2, 1, 1), id="similar-header-class"
        ),
        pytest.param(
            "<h2>Data Engineer</h2>", "<h1>Data Engineer</h1>", (2, 1, 1), id="no-owned-h2"
        ),
        pytest.param(
            "<h2>Data Engineer</h2>",
            "<h2>Data Engineer</h2><h2>Other role</h2>",
            (2, 1, 1),
            id="duplicate-owned-h2",
        ),
        pytest.param(
            "<h2>Data Engineer</h2>",
            "<h2>Data Engineer</h2><h2>Other role",
            (2, 1, 1),
            id="truncated-duplicate-h2",
        ),
        pytest.param(
            'class="page-body job-details"', 'class="page-body"', (2, 1, 1), id="missing-body-role"
        ),
        pytest.param(
            'class="page-body job-details"',
            'class="job-details"',
            (2, 1, 1),
            id="missing-body-scope",
        ),
        pytest.param(
            "</body>",
            '<div class="page-body job-details"><p>Other body</p></div></body>',
            (2, 1, 1),
            id="duplicate-body",
        ),
        pytest.param(
            "</body>",
            '<div class="page-body job-details"><p>Other body</p></body>',
            (2, 1, 1),
            id="truncated-duplicate-body",
        ),
        pytest.param(
            'id="job-description"',
            'id="other-description"',
            (2, 1, 1),
            id="missing-description",
        ),
        pytest.param(
            "</body>",
            '<div id="job-description"><p>Other description</p></div></body>',
            (2, 1, 1),
            id="duplicate-description-id",
        ),
        pytest.param(
            "</body>",
            '<span id="job-description">Other description</span></body>',
            (2, 1, 1),
            id="wrong-tag-duplicate-description-id",
        ),
        pytest.param(
            "</body>",
            "<div id=job-description><p>Other description</p></body>",
            (2, 1, 1),
            id="truncated-duplicate-description-id",
        ),
        pytest.param(
            "<h1>About Acme</h1><div><p>Build data pipelines.</p>"
            "<div><h1>Responsibilities</h1><p>Review reliable SQL queries.</p></div></div>",
            "<script>Only an unreadable template</script>",
            (2, 1, 1),
            id="empty-readable-body",
        ),
        pytest.param(
            'id="resumator-application-form"',
            'id="other-application"',
            (2, 1, 1),
            id="missing-application-evidence",
        ),
        pytest.param(
            "</body>",
            '<div id="resumator-application-form"><form>Other form</form></div></body>',
            (2, 1, 1),
            id="duplicate-application-id",
        ),
        pytest.param(
            'id="resumator-job-value"',
            'id="other-job-value"',
            (2, 1, 1),
            id="missing-identity",
        ),
        pytest.param(
            'type="hidden"', 'type="text"', (2, 1, 1), id="visible-identity-is-not-evidence"
        ),
        pytest.param('value="Rt4Wn8Bv3J"', 'value=""', (2, 1, 1), id="empty-identity"),
        pytest.param(
            'value="Rt4Wn8Bv3J"', 'value="Xq7Lm2Pz9K"', (2, 2, 0), id="mismatched-identity"
        ),
        pytest.param(
            "</body>",
            '<input type="hidden" id="resumator-job-value" value="Xq7Lm2Pz9K"></body>',
            (2, 1, 1),
            id="multiple-identities",
        ),
        pytest.param(
            '<input type="hidden" id="resumator-job-value" value="Rt4Wn8Bv3J">',
            '<span id="resumator-job-value">Rt4Wn8Bv3J</span>',
            (2, 1, 1),
            id="wrong-identity-element",
        ),
    ],
)
def test_an_unowned_or_ambiguous_jazzhr_responsive_detail_remains_retryable(
    before: str, after: str, physical_detail: tuple[int, int, int]
) -> None:
    requested: list[str] = []
    pages = {
        _JAZZHR_BOARD: _JAZZHR_LIST_PAGE,
        _JAZZHR_ANALYST: _jazzhr_posting_page("Clinical Data Analyst", "Springfield"),
        _JAZZHR_ENGINEER: _jazzhr_responsive_page().replace(before, after),
    }
    fetcher = _jazzhr_fetcher(pages, requested=requested)

    first = fetcher.listings("Acme", _JAZZHR_BOARD)

    assert not first.complete
    assert [listing.title for listing in first.listings] == ["Clinical Data Analyst"]
    assert (first.detail.attempted, first.detail.read, first.detail.failed) == physical_detail
    assert first.detail.failure_reasons == ((("parse", 1),) if physical_detail[2] else ())
    assert first.skip_reasons["missing_required_posting_identity"] == 1
    assert first.detail_read_urls == (_JAZZHR_ANALYST,)
    assert first.detail_coverage is None
    [cursor] = first.deferred_targets
    assert cursor.detail_checkpoint is not None
    assert cursor.detail_checkpoint.failed_attempts == (("Rt4Wn8Bv3J", 1),)
    pages[_JAZZHR_ENGINEER] = _jazzhr_responsive_page()

    final = fetcher.continue_listings("Acme", (cursor,), root_url=_JAZZHR_BOARD, handoff_scopes=())

    assert final.complete
    [engineer] = final.listings
    assert engineer.title == "Data Engineer"
    assert "Build data pipelines." in engineer.published_description
    assert final.detail_coverage is not None
    assert set(final.detail_coverage.posting_urls) == {_JAZZHR_ANALYST, _JAZZHR_ENGINEER}
    assert requested.count(_JAZZHR_ANALYST) == 1
    assert requested.count(_JAZZHR_ENGINEER) == 2


def test_a_jazzhr_declared_detail_precedes_the_responsive_template() -> None:
    fallback = _jazzhr_responsive_page()
    fetcher = _jazzhr_fetcher(
        {
            _JAZZHR_BOARD: _JAZZHR_LIST_PAGE,
            _JAZZHR_ANALYST: _jazzhr_posting_page("Clinical Data Analyst", "Springfield"),
            _JAZZHR_ENGINEER: _jazzhr_posting_page("Declared Engineer", "Springfield") + fallback,
        },
        requested=[],
    )

    read = fetcher.listings("Acme", _JAZZHR_BOARD)

    assert read.complete
    assert read.listings[1].title == "Declared Engineer"
    assert read.listings[1].published_description == "Analyze clinical claims data for care teams."


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        pytest.param(
            '<div class="richtext"><h2>Responsibilities</h2>'
            "<p>Build data pipelines for Acme.</p></div>",
            "Responsibilities\n\nBuild data pipelines for Acme.",
            id="nested-richtext",
        ),
        pytest.param(
            "<div><p>Build &amp; review data.</p><div>"
            "<p>Write reliable SQL&nbsp;queries.</p></div></div>",
            "Build & review data.\n\nWrite reliable SQL queries.",
            id="nested-divs-and-entities",
        ),
        pytest.param(
            "<p>Build data pipelines.</p><script>Apply for another job</script>"
            "<style>Apply again</style><ul><li>Review data.</li><li>Write SQL.</li></ul>",
            "Build data pipelines.\n\n- Review data.\n- Write SQL.",
            id="scripts-styles-and-list-layout",
        ),
        pytest.param(
            "<p>Build data pipelines.</p><script>const template = "
            "'<div id=\"resumator-job-description\">Template</div>';</script>",
            "Build data pipelines.",
            id="script-template-does-not-duplicate-a-field",
        ),
    ],
)
def test_a_jazzhr_rendered_detail_retains_only_its_description(
    description: str, expected: str
) -> None:
    requested: list[str] = []
    fetcher = _jazzhr_fetcher(
        {
            _JAZZHR_BOARD: _JAZZHR_LIST_PAGE,
            _JAZZHR_ANALYST: _jazzhr_posting_page("Clinical Data Analyst", "Springfield"),
            _JAZZHR_ENGINEER: _jazzhr_rendered_page(
                description=f'<div id="resumator-job-description">{description}</div>'
            ),
        },
        requested=requested,
    )

    read = fetcher.listings("Acme", _JAZZHR_BOARD)

    assert read.complete is True
    assert requested == [_JAZZHR_BOARD, _JAZZHR_ANALYST, _JAZZHR_ENGINEER]
    analyst, engineer = read.listings
    assert analyst.title == "Clinical Data Analyst"
    assert engineer.title == "Data Engineer"
    assert engineer.published_description == expected
    assert engineer.employer == "Acme"
    assert engineer.url == _JAZZHR_ENGINEER
    assert engineer.requisition_id == "Rt4Wn8Bv3J"
    assert engineer.facts.locations.values == ()
    assert engineer.facts.employment_types.values == ()
    assert engineer.facts.compensation.values == ()
    assert (read.detail.attempted, read.detail.read, read.detail.failed) == (2, 2, 0)


@pytest.mark.parametrize(
    ("fields", "places", "employment_types"),
    [
        pytest.param(
            '<h2 id="resumator-job-location"><strong>Location:</strong> Springfield, MA</h2>'
            '<h2 id="resumator-job-employment"><strong>Type:</strong>Full Time</h2>',
            ("Springfield, MA",),
            ("Full Time",),
            id="stated-place-and-type",
        ),
        pytest.param(
            '<h2 id="resumator-job-location"><strong>Location:</strong></h2>'
            '<h2 id="resumator-job-employment"><strong>Type:</strong></h2>',
            (),
            (),
            id="empty-labelled-fields",
        ),
        pytest.param(
            '<h2 id="resumator-job-location"><strong>Location:</strong> Springfield</h2>'
            '<h2 id="resumator-job-location"><strong>Location:</strong> Shelbyville</h2>'
            '<h2 id="resumator-job-employment"><strong>Type:</strong>Full Time</h2>'
            '<h2 id="resumator-job-employment"><strong>Type:</strong>Part Time</h2>',
            (),
            (),
            id="ambiguous-optional-fields",
        ),
    ],
)
def test_a_jazzhr_rendered_detail_retains_only_unambiguous_stated_fields(
    fields: str, places: tuple[str, ...], employment_types: tuple[str, ...]
) -> None:
    fetcher = _jazzhr_fetcher(
        {
            _JAZZHR_BOARD: _JAZZHR_LIST_PAGE,
            _JAZZHR_ANALYST: _jazzhr_posting_page("Clinical Data Analyst", "Springfield"),
            _JAZZHR_ENGINEER: _jazzhr_rendered_page(fields=fields),
        },
        requested=[],
    )

    read = fetcher.listings("Acme", _JAZZHR_BOARD)

    assert read.complete is True
    engineer = read.listings[1]
    assert engineer.facts.locations.values == places
    assert engineer.facts.employment_types.values == employment_types
    assert engineer.facts.locations.complete is False
    assert engineer.employer == "Acme"


def test_a_jazzhr_detail_prefers_its_declared_posting_over_rendered_fields() -> None:
    rendered = _jazzhr_rendered_page(
        fields='<h2 id="resumator-job-location"><strong>Location:</strong> Elsewhere</h2>'
    )
    page = _jazzhr_posting_page("Declared Engineer", "Springfield") + rendered
    fetcher = _jazzhr_fetcher(
        {
            _JAZZHR_BOARD: _JAZZHR_LIST_PAGE,
            _JAZZHR_ANALYST: _jazzhr_posting_page("Clinical Data Analyst", "Springfield"),
            _JAZZHR_ENGINEER: page,
        },
        requested=[],
    )

    read = fetcher.listings("Acme", _JAZZHR_BOARD)

    assert read.complete is True
    engineer = read.listings[1]
    assert engineer.title == "Declared Engineer"
    assert engineer.published_description == "Analyze clinical claims data for care teams."
    assert engineer.facts.locations.values == ("Springfield, ON, Canada",)


@pytest.mark.parametrize(
    ("title", "description"),
    [
        pytest.param(
            "", '<div id="resumator-job-description"><p>Build data.</p></div>', id="missing-title"
        ),
        pytest.param(
            '<div id="resumator-job-title"><h1>Data Engineer</h1></div>',
            "",
            id="missing-description",
        ),
        pytest.param(
            '<div id="resumator-job-title"><h1> </h1></div>',
            '<div id="resumator-job-description"><p>Build data.</p></div>',
            id="empty-title",
        ),
        pytest.param(
            '<div id="resumator-job-title"><h1>Data Engineer</h1></div>',
            '<div id="resumator-job-description"><script>Build data.</script></div>',
            id="empty-description",
        ),
        pytest.param(
            '<h1 id="resumator-job-title">Data Engineer</h1>',
            '<div id="resumator-job-description"><p>Build data.</p></div>',
            id="wrong-title-container",
        ),
        pytest.param(
            '<div id="resumator-job-title"><h1>Data Engineer</h1></div>' * 2,
            '<div id="resumator-job-description"><p>Build data.</p></div>',
            id="duplicate-title",
        ),
        pytest.param(
            '<div id="resumator-job-title"><h1>Data Engineer</h1></div>',
            '<div id="resumator-job-description"><p>Build data.</p></div>' * 2,
            id="duplicate-description",
        ),
        pytest.param(
            '<div id="resumator-job-title"><h1>Data Engineer</h1>',
            '<div id="resumator-job-description"><p>Build data.</p></div>',
            id="truncated-title",
        ),
        pytest.param(
            '<div id="resumator-job-title"><h1>Data Engineer</h1></div>',
            '<div id="resumator-job-description"><div><p>Build data.</p></div>',
            id="truncated-description",
        ),
        pytest.param(
            '<div id="resumator-job-title-extra"><h1>Data Engineer</h1></div>',
            '<div id="resumator-job-description"><p>Build data.</p></div>',
            id="similar-title-id",
        ),
        pytest.param(
            '<div id="resumator-job-title"><h1>Data Engineer</h1></div>'
            '<div id="resumator-job-title"><h1>Other Engineer</h1>',
            '<div id="resumator-job-description"><p>Build data.</p></div>',
            id="truncated-duplicate-title",
        ),
        pytest.param(
            '<div id="resumator-job-title"><h1>Data Engineer</h1></div>',
            '<div id="resumator-job-description"><p>Build data.</p></div>'
            '<div id="resumator-job-description"><p>Other data.</p>',
            id="truncated-duplicate-description",
        ),
        pytest.param(
            '<div id="resumator-job-title"><h1>Data Engineer</h1></div>'
            "<div id=resumator-job-title><h1>Other Engineer</h1>",
            '<div id="resumator-job-description"><p>Build data.</p></div>',
            id="unquoted-truncated-duplicate-title",
        ),
        pytest.param(
            '<div id="resumator-job-title"><h1>Data Engineer</h1></div>'
            '<h1 id="resumator-job-title">Other Engineer</h1>',
            '<div id="resumator-job-description"><p>Build data.</p></div>',
            id="wrong-tag-duplicate-title",
        ),
    ],
)
def test_an_incomplete_or_ambiguous_jazzhr_rendered_detail_remains_retryable(
    title: str, description: str
) -> None:
    requested: list[str] = []
    pages = {
        _JAZZHR_BOARD: _JAZZHR_LIST_PAGE,
        _JAZZHR_ANALYST: _jazzhr_posting_page("Clinical Data Analyst", "Springfield"),
        _JAZZHR_ENGINEER: _jazzhr_rendered_page(title=title, description=description),
    }
    fetcher = _jazzhr_fetcher(pages, requested=requested)

    first = fetcher.listings("Acme", _JAZZHR_BOARD)

    assert first.complete is False
    assert [listing.title for listing in first.listings] == ["Clinical Data Analyst"]
    assert (first.detail.attempted, first.detail.read, first.detail.failed) == (2, 1, 1)
    assert first.detail.failure_reasons == (("parse", 1),)
    [cursor] = first.deferred_targets
    assert cursor.detail_checkpoint is not None
    assert cursor.detail_checkpoint.failed_attempts == (("Rt4Wn8Bv3J", 1),)
    pages[_JAZZHR_ENGINEER] = _jazzhr_rendered_page()

    final = fetcher.continue_listings("Acme", (cursor,), root_url=_JAZZHR_BOARD, handoff_scopes=())

    assert final.complete is True
    [engineer] = final.listings
    assert engineer.title == "Data Engineer"
    assert engineer.published_description == "Responsibilities\n\nBuild data pipelines for Acme."
    assert final.detail_coverage is not None
    assert set(final.detail_coverage.posting_urls) == {_JAZZHR_ANALYST, _JAZZHR_ENGINEER}
    assert requested.count(_JAZZHR_ANALYST) == 1
    assert requested.count(_JAZZHR_ENGINEER) == 2


@pytest.mark.parametrize("board", ["https://acme.applytojob.com/", _JAZZHR_BOARD])
def test_jazzhr_reads_each_listed_opening_once_with_its_page_posting(board: str) -> None:
    requested: list[str] = []
    fetcher = _jazzhr_fetcher(
        {
            _JAZZHR_BOARD: _JAZZHR_LIST_PAGE,
            _JAZZHR_ANALYST: _jazzhr_posting_page("Clinical Data Analyst", "Springfield"),
            _JAZZHR_ENGINEER: _jazzhr_posting_page("Data Engineer", "Shelbyville"),
        },
        requested=requested,
    )

    read = fetcher.listings("Acme", board)

    assert requested == [_JAZZHR_BOARD, _JAZZHR_ANALYST, _JAZZHR_ENGINEER]
    assert read.complete is True
    assert read.raw_count == 2
    analyst, engineer = read.listings
    assert (analyst.title, engineer.title) == ("Clinical Data Analyst", "Data Engineer")
    assert analyst.url == _JAZZHR_ANALYST
    assert analyst.requisition_id == "Xq7Lm2Pz9K"
    assert analyst.employer == "Acme"
    assert analyst.facts.locations.values[0] == "Springfield, ON, Canada"
    assert analyst.published_description == "Analyze clinical claims data for care teams."
    assert (read.detail.attempted, read.detail.read, read.detail.failed) == (2, 2, 0)


def test_a_jazzhr_posting_keeps_the_salary_range_its_page_declares() -> None:
    salary = (
        ' "baseSalary": {"@type": "MonetaryAmount", "currency": "CAD", "value":'
        ' {"@type": "QuantitativeValue", "minValue": 85000, "maxValue": 95000,'
        ' "unitText": "YEAR"}},'
    )
    fetcher = _jazzhr_fetcher(
        {
            _JAZZHR_BOARD: _JAZZHR_LIST_PAGE,
            _JAZZHR_ANALYST: _jazzhr_posting_page(
                "Clinical Data Analyst", "Springfield", salary=salary
            ),
            _JAZZHR_ENGINEER: _jazzhr_posting_page("Data Engineer", "Shelbyville"),
        },
        requested=[],
    )

    analyst, engineer = fetcher.listings("Acme", _JAZZHR_BOARD).listings

    assert analyst.facts.compensation.values == ("CAD 85000-95000 per year",)
    assert engineer.facts.compensation.values == ()


def test_a_jazzhr_page_without_posting_markup_is_a_failed_detail() -> None:
    fetcher = _jazzhr_fetcher(
        {
            _JAZZHR_BOARD: _JAZZHR_LIST_PAGE,
            _JAZZHR_ANALYST: _jazzhr_posting_page("Clinical Data Analyst", "Springfield"),
            _JAZZHR_ENGINEER: "<html><body><h1>Data Engineer</h1></body></html>",
        },
        requested=[],
    )

    read = fetcher.listings("Acme", _JAZZHR_BOARD)

    assert [listing.title for listing in read.listings] == ["Clinical Data Analyst"]
    assert read.complete is False
    assert "1 of 2" in read.note
    assert (read.detail.attempted, read.detail.read, read.detail.failed) == (2, 1, 1)
    assert read.detail.failure_reasons == (("parse", 1),)
    assert read.detail.failure_examples == (_JAZZHR_ENGINEER,)


def test_a_jazzhr_board_page_listing_no_openings_is_not_read_as_an_empty_board() -> None:
    fetcher = _jazzhr_fetcher(
        {_JAZZHR_BOARD: "<html><body><div id='app'></div></body></html>"}, requested=[]
    )

    read = fetcher.listings("Acme", _JAZZHR_BOARD)

    assert read.listings == []
    assert read.complete is False
    assert read.outcome is SourceReadOutcome.NO_SUPPORTED_REPRESENTATION


def test_pinpoint_retains_all_published_body_sections() -> None:
    class ScriptedPinpoint(BoardApiFetcher):
        def __init__(self) -> None:
            pass

        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            assert url == "https://healthtech.pinpointhq.com/postings.json"
            return {
                "data": [
                    {
                        "id": "posting-1",
                        "title": "Clinical Product Manager",
                        "url": "https://healthtech.pinpointhq.com/postings/posting-1",
                        "description": "<p>Build clinical software.</p>",
                        "key_responsibilities": "<p>Own the roadmap.</p>",
                        "skills_knowledge_expertise": "<p>Know FHIR.</p>",
                        "benefits": "<p>Remote work.</p>",
                        "location": {"id": "1", "name": "Remote - Canada"},
                        "job": {"id": "job-1", "requisition_id": "PIN-7"},
                    }
                ]
            }

    read = ScriptedPinpoint().listings(
        "Health Tech", "https://healthtech.pinpointhq.com/postings/posting-1"
    )

    [listing] = read.listings
    assert read.complete is True
    assert listing.requisition_id == "PIN-7"
    assert listing.location == "Remote - Canada"
    assert listing.published_description == (
        "Build clinical software.\n\nOwn the roadmap.\n\nKnow FHIR.\n\nRemote work."
    )


_PAYLOCITY_TENANT = "0b7f2c1e-5a4d-4e8b-9c3f-7d6e5a4b3c2d"
_PAYLOCITY_PAGE = f"https://recruiting.paylocity.com/Recruiting/Jobs/All/{_PAYLOCITY_TENANT}"
_PAYLOCITY_FEED = (
    f"https://recruiting.paylocity.com/recruiting/v2/api/feed/jobs/{_PAYLOCITY_TENANT}"
)


def _paylocity_fetcher(
    page: httpx.Response | str, feed_jobs: list[dict[str, Any]] | httpx.Response
) -> tuple[BoardApiFetcher, list[str]]:
    requested: list[str] = []

    def answer(url: str) -> httpx.Response:
        requested.append(url)
        request = httpx.Request("GET", url)
        if url == _PAYLOCITY_PAGE:
            if isinstance(page, httpx.Response):
                return page
            return httpx.Response(200, request=request, text=page)
        if url == _PAYLOCITY_FEED:
            if isinstance(feed_jobs, httpx.Response):
                return feed_jobs
            return httpx.Response(
                200, request=request, json={"displayName": _PAYLOCITY_TENANT, "jobs": feed_jobs}
            )
        raise AssertionError(f"unexpected request {url}")

    return BoardApiFetcher(client=policy_client(answer)), requested


def test_a_paylocity_board_read_lists_every_public_opening_its_page_embeds() -> None:
    page = paylocity_board_page(
        [
            paylocity_page_job(7001234, "Senior Product Manager, Claims Platform", IsRemote=True),
            paylocity_page_job(7001235, "Clinical Data Analyst"),
            paylocity_page_job(7001236, "Internal Transfer Only", IsInternal=True),
            paylocity_page_job(7001237, ""),
        ]
    )
    fetcher, requested = _paylocity_fetcher(page, [])

    read = fetcher.listings("Examplehealth", f"{_PAYLOCITY_PAGE}/Examplehealth-Inc")

    assert requested == [_PAYLOCITY_PAGE, _PAYLOCITY_FEED]
    assert read.complete is True
    assert read.outcome is SourceReadOutcome.ATS_COMPLETE
    assert read.skip_reasons == {
        "internal_only_posting": 1,
        "missing_required_posting_identity": 1,
    }
    remote, onsite = read.listings
    assert [listing.url for listing in read.listings] == [
        "https://recruiting.paylocity.com/Recruiting/Jobs/Details/7001234",
        "https://recruiting.paylocity.com/Recruiting/Jobs/Details/7001235",
    ]
    assert remote.title == "Senior Product Manager, Claims Platform"
    assert remote.location == "Springfield, OH"
    assert remote.requisition_id == "7001234"
    assert remote.facts.arrangements.values == ("remote",)
    assert onsite.facts.arrangements.values == ()
    # The page's Description is a cut excerpt, so the posting body is read elsewhere.
    assert [(listing.published_description, listing.summary) for listing in read.listings] == [
        ("", ""),
        ("", ""),
    ]


def test_a_paylocity_opening_the_feed_also_carries_keeps_the_feed_body() -> None:
    page = paylocity_board_page([paylocity_page_job(13784, "Senior Biological Data Scientist")])
    feed_job = {
        "jobId": 13784,
        "jobCode": "BIO-12",
        "title": "Senior Biological Data Scientist",
        "displayUrl": (
            "https://recruiting.paylocity.com/recruiting/Jobs/Details/13784/"
            "Examplehealth/Senior-Biological-Data-Scientist"
        ),
        "description": "<p>Build translational data products.</p>",
        "requirements": "<p>Life-science data experience.</p>",
        "jobLocation": {"locationDisplayName": "Fully Remote", "city": "Northtown", "state": "NT"},
    }
    fetcher, _requested = _paylocity_fetcher(page, [feed_job])

    read = fetcher.listings("Examplehealth", _PAYLOCITY_FEED)

    [listing] = read.listings
    assert read.complete is True
    assert listing.url == "https://recruiting.paylocity.com/Recruiting/Jobs/Details/13784"
    assert listing.location == "Fully Remote; Northtown, NT"
    assert listing.requisition_id == "BIO-12"
    assert listing.published_description == (
        "Build translational data products.\n\nLife-science data experience."
    )


@pytest.mark.parametrize(
    "refused",
    [
        pytest.param(
            httpx.Response(404, request=httpx.Request("GET", _PAYLOCITY_FEED)), id="not-found"
        ),
        pytest.param(
            httpx.Response(
                200, request=httpx.Request("GET", _PAYLOCITY_FEED), text="<html>Down</html>"
            ),
            id="not-json",
        ),
    ],
)
def test_a_paylocity_page_job_list_is_the_whole_board_when_the_feed_is_unreadable(
    refused: httpx.Response,
) -> None:
    page = paylocity_board_page([paylocity_page_job(7001235, "Clinical Data Analyst")])
    fetcher, _requested = _paylocity_fetcher(page, refused)

    read = fetcher.listings("Examplehealth", _PAYLOCITY_PAGE)

    assert read.complete is True
    assert [listing.url for listing in read.listings] == [
        "https://recruiting.paylocity.com/Recruiting/Jobs/Details/7001235"
    ]


_FEED_ONLY_JOB = {
    "jobId": 9,
    "title": "Clinical Systems Analyst",
    "displayUrl": "https://recruiting.paylocity.com/recruiting/Jobs/Details/9/Examplehealth",
    "description": "<p>Configure clinical systems.</p>",
}


@pytest.mark.parametrize(
    ("page", "feed_jobs"),
    [
        pytest.param("<html><body>Current Openings</body></html>", [], id="no-page-data"),
        pytest.param(
            "<html><body>Current Openings</body></html>", [_FEED_ONLY_JOB], id="feed-only-jobs"
        ),
        pytest.param(
            paylocity_board_page([paylocity_page_job(1, "Analyst")])[:-80], [], id="cut-page-data"
        ),
        pytest.param(
            httpx.Response(404, request=httpx.Request("GET", _PAYLOCITY_PAGE)),
            [_FEED_ONLY_JOB],
            id="page-not-found",
        ),
    ],
)
def test_a_paylocity_read_without_the_page_job_list_is_never_complete(
    page: httpx.Response | str, feed_jobs: list[dict[str, Any]]
) -> None:
    fetcher, _requested = _paylocity_fetcher(page, feed_jobs)

    read = fetcher.listings("Examplehealth", _PAYLOCITY_FEED)

    assert read.complete is False
    assert read.outcome is SourceReadOutcome.NO_SUPPORTED_REPRESENTATION
    assert [listing.url for listing in read.listings] == [
        f"https://recruiting.paylocity.com/Recruiting/Jobs/Details/{job['jobId']}"
        for job in feed_jobs
    ]


@pytest.mark.parametrize(
    ("page_jobs", "expected_ids"),
    [
        pytest.param(
            [paylocity_page_job(7001235, "Clinical Data Analyst")], [7001235, 9], id="beside-page"
        ),
        pytest.param([], [9], id="empty-page-list"),
    ],
)
def test_a_paylocity_job_only_the_feed_lists_is_read_beside_the_page_list(
    page_jobs: list[dict[str, Any]], expected_ids: list[int]
) -> None:
    fetcher, _requested = _paylocity_fetcher(paylocity_board_page(page_jobs), [_FEED_ONLY_JOB])

    read = fetcher.listings("Examplehealth", _PAYLOCITY_PAGE)

    assert read.complete is True
    assert [listing.url for listing in read.listings] == [
        f"https://recruiting.paylocity.com/Recruiting/Jobs/Details/{job_id}"
        for job_id in expected_ids
    ]
    assert read.listings[-1].published_description == "Configure clinical systems."


def test_one_paylocity_job_keeps_one_address_on_every_read_path() -> None:
    listed = paylocity_board_page([paylocity_page_job(9, "Clinical Systems Analyst")])
    unlisted = "<html><body>Current Openings</body></html>"
    paths = {
        "page-only": (listed, []),
        "merged": (listed, [_FEED_ONLY_JOB]),
        "no-page-list": (unlisted, [_FEED_ONLY_JOB]),
    }

    addresses = {}
    for path, (page, feed_jobs) in paths.items():
        fetcher, _requested = _paylocity_fetcher(page, feed_jobs)
        [listing] = fetcher.listings("Examplehealth", _PAYLOCITY_PAGE).listings
        addresses[path] = listing.url

    assert set(addresses.values()) == {"https://recruiting.paylocity.com/Recruiting/Jobs/Details/9"}


class _CheckpointAdp(BoardApiFetcher):
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        super().__init__()
        self.now = 100.0
        self.rows = [
            _adp_requisition(itemID=f"920000000000{index}_1", requisitionTitle=f"Analyst {index}")
            for index in range(4)
        ]
        self.seconds = 20.0
        self.requested: list[str] = []
        self.failure: str | None = None
        self.cut_on_list = False
        self.total: int | None = None
        monkeypatch.setattr(
            "clearscout.infrastructure.web_fetch.board_adapters.transport.time.monotonic",
            lambda: self.now,
        )

    def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
        path = url.partition("?")[0]
        if path == _ADP_API:
            if self.cut_on_list:
                self.now += 60
            return {
                "jobRequisitions": self.rows,
                "meta": {"totalNumber": self.total if self.total is not None else len(self.rows)},
            }
        item = path.removeprefix(f"{_ADP_API}/")
        self.requested.append(item)
        if self.failure == "held":
            raise HostHeldError(url, ready_at=100)
        if self.failure == "fetch":
            raise httpx.ReadError("connection lost", request=httpx.Request("GET", url))
        if self.failure == "missing":
            return {}
        self.now += self.seconds
        return next(row for row in self.rows if row["itemID"] == item)


@pytest.mark.parametrize(
    "change", ["same", "added", "removed", "reordered", "repeated", "partial", "failed"]
)
def test_detail_read_evidence_counts_only_cumulatively_mapped_bodies(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    fetcher = _CheckpointAdp(monkeypatch)
    first = fetcher.listings("Initech", _ADP_BOARD)
    [cursor] = first.deferred_targets
    old_urls = tuple(listing.url for listing in first.listings)
    assert first.detail_read_urls == old_urls
    if change == "added":
        fetcher.rows.append(_adp_requisition(itemID="9200000000008_1"))
    elif change == "removed":
        fetcher.rows = fetcher.rows[1:]
    elif change == "reordered":
        fetcher.rows = [fetcher.rows[index] for index in (3, 1, 2, 0)]
    elif change == "repeated":
        fetcher.rows = [fetcher.rows[index] for index in (2, 2, 1, 3)]
        fetcher.total = 3
    elif change == "partial":
        fetcher.rows = fetcher.rows[2:]
        fetcher.total = 5
    elif change == "failed":
        fetcher.failure = "fetch"
    fetcher.seconds = 0

    final = fetcher.continue_listings("Initech", (cursor,), root_url=_ADP_BOARD, handoff_scopes=())

    retained = old_urls[1:] if change in ("removed", "repeated") else old_urls
    expected = set(retained) | {listing.url for listing in final.listings}
    assert set(final.detail_read_urls or ()) == expected
    assert len(final.detail_read_urls or ()) == len(expected)
    assert final.raw_count >= len(expected)
    assert final.complete is (change not in ("partial", "failed"))
    if change == "partial":
        assert final.raw_count == 4
        assert final.detail_coverage is None
        fetcher.total = 2
        complete = fetcher.continue_listings(
            "Initech", (cursor,), root_url=_ADP_BOARD, handoff_scopes=()
        )
        assert complete.complete
        assert complete.raw_count == 2
        assert set(complete.detail_read_urls or ()) == {
            listing.url for listing in complete.listings
        }
        assert not set(old_urls) & set(complete.detail_read_urls or ())
    if change == "failed":
        assert final.detail_read_urls == old_urls
        assert final.detail.failed == 2
        assert final.detail_coverage is None


def test_a_detail_checkpoint_finishes_after_multiple_slices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = _CheckpointAdp(monkeypatch)
    first = fetcher.listings("Initech", _ADP_BOARD)
    [cursor] = first.deferred_targets

    assert cursor.detail_checkpoint is not None
    assert cursor.detail_checkpoint.pending_ids == ("9200000000002_1", "9200000000003_1")
    final = fetcher.continue_listings("Initech", (cursor,), root_url=_ADP_BOARD, handoff_scopes=())

    assert final.complete is True
    assert final.deferred_targets == ()
    assert final.detail_coverage is not None
    assert final.detail_coverage.posting_urls == tuple(
        listing.url for listing in [*first.listings, *final.listings]
    )
    assert fetcher.requested == [row["itemID"] for row in fetcher.rows]


@pytest.mark.parametrize("career_center", ["19000101_000001_2", "19000101_000001_3"])
def test_adp_continuations_keep_their_career_center(
    monkeypatch: pytest.MonkeyPatch, career_center: str
) -> None:
    fetcher = _CheckpointAdp(monkeypatch)
    requested: list[str] = []
    original_get = fetcher.get_json

    def get_json(url: str, *, headers: Mapping[str, str] | None = None) -> Any:
        requested.append(url)
        return original_get(url, headers=headers)

    monkeypatch.setattr(fetcher, "get_json", get_json)
    board = _ADP_BOARD.replace("19000101_000001", career_center)
    first = fetcher.listings("Initech", board)
    [cursor] = first.deferred_targets

    assert parse_qs(cursor.url.partition("?")[2])["ccId"] == [career_center]
    assert cursor.detail_checkpoint is not None
    assert parse_qs(cursor.detail_checkpoint.board_url.partition("?")[2])["ccId"] == [career_center]
    fetcher.seconds = 0
    final = fetcher.continue_listings("Initech", (cursor,), root_url=board, handoff_scopes=())

    assert final.complete
    assert all(parse_qs(url.partition("?")[2]).get("ccId") == [career_center] for url in requested)
    assert fetcher.requested == [row["itemID"] for row in fetcher.rows]
    assert final.detail_coverage is not None
    assert all(
        parse_qs(url.partition("?")[2]).get("ccId") == [career_center]
        for url in final.detail_coverage.posting_urls
    )


def test_an_adp_checkpoint_from_another_career_center_does_not_skip_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = _CheckpointAdp(monkeypatch)
    first_board = _ADP_BOARD.replace("19000101_000001", "19000101_000001_2")
    second_board = first_board.replace("19000101_000001_2", "19000101_000001_3")
    [cursor] = fetcher.listings("Initech", first_board).deferred_targets
    fetcher.seconds = 0
    fetcher.requested.clear()

    read = fetcher.continue_listings(
        "Initech", (replace(cursor, url=second_board),), root_url=second_board, handoff_scopes=()
    )

    assert read.complete
    assert fetcher.requested == [row["itemID"] for row in fetcher.rows]
    assert all(
        parse_qs(listing.url.partition("?")[2]).get("ccId") == ["19000101_000001_3"]
        for listing in read.listings
    )


def test_a_detail_checkpoint_reconciles_reordered_added_and_removed_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = _CheckpointAdp(monkeypatch)
    first = fetcher.listings("Initech", _ADP_BOARD)
    [cursor] = first.deferred_targets
    new = _adp_requisition(itemID="9200000000008_1", requisitionTitle="New Analyst")
    fetcher.rows = [new, fetcher.rows[3], fetcher.rows[1], fetcher.rows[2]]
    fetcher.seconds = 0
    fetcher.requested.clear()

    final = fetcher.continue_listings("Initech", (cursor,), root_url=_ADP_BOARD, handoff_scopes=())

    assert [listing.title for listing in final.listings] == [
        "New Analyst",
        "Analyst 3",
        "Analyst 2",
    ]
    assert final.complete is True
    assert final.detail_coverage is not None
    assert set(final.detail_coverage.posting_urls) == {
        listing.url for listing in [first.listings[1], *final.listings]
    }
    assert fetcher.requested == ["9200000000008_1", "9200000000003_1", "9200000000002_1"]


@pytest.mark.parametrize("failure", ["fetch", "missing"])
def test_failed_detail_ids_keep_three_attempts_and_one_manual_retry(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    fetcher = _CheckpointAdp(monkeypatch)
    fetcher.rows = fetcher.rows[:1]
    fetcher.failure = failure
    read = fetcher.listings("Initech", _ADP_BOARD)
    [cursor] = read.deferred_targets

    for attempt in range(1, 4):
        [cursor] = read.deferred_targets
        assert cursor.detail_checkpoint is not None
        assert cursor.detail_checkpoint.failed_attempts == (("9200000000000_1", attempt),)
        assert cursor.automatic_eligible is (attempt < 3)
        assert read.detail_coverage is None
        assert read.detail.failed == 1
        if attempt < 3:
            read = fetcher.continue_listings(
                "Initech", (cursor,), root_url=_ADP_BOARD, handoff_scopes=()
            )
            assert read.deferred_targets[0].url == cursor.url

    automatic = fetcher.continue_listings(
        "Initech", (cursor,), root_url=_ADP_BOARD, handoff_scopes=()
    )
    assert automatic.detail.attempted == 0
    manual = fetcher.continue_listings(
        "Initech",
        (replace(cursor, manual_detail_retry=True),),
        root_url=_ADP_BOARD,
        handoff_scopes=(),
    )
    [after_manual] = manual.deferred_targets
    assert after_manual.detail_checkpoint is not None
    assert after_manual.detail_checkpoint.failed_attempts == (("9200000000000_1", 4),)
    assert after_manual.automatic_eligible is False
    assert after_manual.manual_detail_retry is False
    assert len(fetcher.requested) == 4


def test_a_cut_before_the_first_detail_keeps_every_stable_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = _CheckpointAdp(monkeypatch)
    fetcher.cut_on_list = True
    read = fetcher.listings("Initech", _ADP_BOARD)
    [cursor] = read.deferred_targets

    assert cursor.detail_checkpoint is not None
    assert cursor.detail_checkpoint.pending_ids == tuple(row["itemID"] for row in fetcher.rows)
    assert cursor.detail_checkpoint.failed_attempts == ()
    assert read.detail.attempted == 0
    fetcher.cut_on_list = False
    fetcher.seconds = 0
    final = fetcher.continue_listings("Initech", (cursor,), root_url=_ADP_BOARD, handoff_scopes=())
    assert final.complete is True
    assert len(final.listings) == 4


@pytest.mark.parametrize("mismatch", ["platform", "board"])
def test_a_detail_checkpoint_for_another_board_does_not_skip_current_rows(
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    fetcher = _CheckpointAdp(monkeypatch)
    [cursor] = fetcher.listings("Initech", _ADP_BOARD).deferred_targets
    assert cursor.detail_checkpoint is not None
    checkpoint = replace(
        cursor.detail_checkpoint,
        platform="rippling" if mismatch == "platform" else "adp",
        board_url=(
            cursor.detail_checkpoint.board_url.replace("en_CA", "fr_CA")
            if mismatch == "board"
            else cursor.detail_checkpoint.board_url
        ),
    )
    fetcher.seconds = 0
    fetcher.requested.clear()
    read = fetcher.continue_listings(
        "Initech",
        (replace(cursor, detail_checkpoint=checkpoint),),
        root_url=_ADP_BOARD,
        handoff_scopes=(),
    )

    assert read.complete is True
    assert fetcher.requested == [row["itemID"] for row in fetcher.rows]


def test_an_empty_current_board_prunes_completed_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = _CheckpointAdp(monkeypatch)
    [cursor] = fetcher.listings("Initech", _ADP_BOARD).deferred_targets
    fetcher.rows = []

    final = fetcher.continue_listings("Initech", (cursor,), root_url=_ADP_BOARD, handoff_scopes=())

    assert final.complete is True
    assert final.detail_coverage is not None
    assert final.detail_coverage.posting_urls == ()


def test_an_incomplete_enumeration_never_publishes_detail_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = _CheckpointAdp(monkeypatch)
    [cursor] = fetcher.listings("Initech", _ADP_BOARD).deferred_targets
    fetcher.rows = fetcher.rows[2:]
    fetcher.total = 5
    fetcher.seconds = 0

    read = fetcher.continue_listings("Initech", (cursor,), root_url=_ADP_BOARD, handoff_scopes=())

    assert read.complete is False
    assert read.detail_coverage is None
    assert read.outcome is SourceReadOutcome.NO_SUPPORTED_REPRESENTATION


def test_a_host_hold_keeps_checkpoint_attempt_counts_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = _CheckpointAdp(monkeypatch)
    [cursor] = fetcher.listings("Initech", _ADP_BOARD).deferred_targets
    fetcher.failure = "held"
    read = fetcher.continue_listings("Initech", (cursor,), root_url=_ADP_BOARD, handoff_scopes=())
    [held] = read.deferred_targets

    assert held.detail_checkpoint is not None
    assert held.detail_checkpoint == cursor.detail_checkpoint
    assert held.automatic_eligible is False
    assert read.detail.failed == 0
    assert read.detail.attempted == 0
    assert read.detail_coverage is None


@pytest.mark.parametrize("name", ["adp", "breezy", "rippling", "dover", "bamboohr", "jazzhr"])
def test_each_detail_board_preserves_stable_progress_and_strips_internal_cursors(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    now = [100.0]
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.board_adapters.transport.time.monotonic",
        lambda: now[0],
    )
    boards = {
        "adp": _ADP_BOARD,
        "breezy": "https://acme.breezy.hr",
        "rippling": "https://ats.rippling.com/globex/jobs",
        "dover": f"https://app.dover.com/dover/careers/{_DOVER_CLIENT}",
        "bamboohr": "https://globex.bamboohr.com/careers",
        "jazzhr": _JAZZHR_BOARD,
    }
    identifiers = ("11111111-2222-4333-8444-555555555551", "11111111-2222-4333-8444-555555555552")
    rows = {
        "adp": [_adp_requisition(itemID=f"920000000000{i}_1") for i in range(2)],
        "breezy": [
            _breezy_position(
                id=f"row{i}",
                url=f"https://acme.breezy.hr/p/row{i}-analyst",
                friendly_id=f"row{i}-analyst",
            )
            for i in range(2)
        ],
        "rippling": [
            _rippling_row(id=item, url=f"https://ats.rippling.com/globex/jobs/{item}")
            for item in identifiers
        ],
        "dover": [_dover_row(item) for item in identifiers],
        "bamboohr": [_bamboohr_row(id=str(77 + i)) for i in range(2)],
    }

    class ScriptedDetails(BoardApiFetcher):
        def __init__(self) -> None:
            super().__init__()
            self.seconds = 40.0
            self.requests: list[str] = []

        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            self.requests.append(url)
            assert "detail_checkpoint" not in url and "detail_offset" not in url
            if name == "adp" and url.startswith(_ADP_API + "?"):
                return {"jobRequisitions": rows[name], "meta": {"totalNumber": 2}}
            if name == "breezy" and url == _BREEZY_FEED:
                return rows[name]
            if name == "rippling" and url == _RIPPLING_JOBS:
                return _rippling_page(rows[name])
            if name == "dover" and url == _DOVER_JOBS:
                return {"results": rows[name], "count": 2}
            if name == "bamboohr" and url.endswith("/careers/list"):
                return {"result": rows[name]}
            now[0] += self.seconds
            item = url.partition("?")[0].rsplit("/", 1)[1]
            if name == "adp":
                return _adp_requisition(itemID=item)
            if name == "rippling":
                return _rippling_detail(uuid=item)
            if name == "dover":
                return _dover_detail(id=item)
            if name == "bamboohr":
                item = url.split("/careers/", 1)[1].split("/", 1)[0]
                return {
                    "result": {
                        "jobOpening": _bamboohr_opening(
                            jobOpeningShareUrl=f"https://globex.bamboohr.com/careers/{item}"
                        )
                    }
                }
            raise AssertionError(url)

        def get_text(self, url: str, *, max_bytes: int = 400_000) -> str:
            self.requests.append(url)
            assert "detail_checkpoint" not in url and "detail_offset" not in url
            if url == _JAZZHR_BOARD:
                return _JAZZHR_LIST_PAGE
            now[0] += self.seconds
            return _jazzhr_posting_page("Data Analyst", "Northtown")

    fetcher = ScriptedDetails()
    first = fetcher.listings("Initech", boards[name])
    [cursor] = first.deferred_targets
    assert cursor.detail_checkpoint is not None
    assert len(cursor.detail_checkpoint.completed) == 1
    assert len(cursor.detail_checkpoint.pending_ids) == 1
    fetcher.seconds = 0
    final = fetcher.continue_listings(
        "Initech", (cursor,), root_url=boards[name], handoff_scopes=()
    )

    assert final.complete is True
    assert final.detail.attempted == 1
    assert final.detail_coverage is not None
    assert final.detail_coverage.platform == name
    assert set(final.detail_coverage.posting_urls) == {
        listing.url for listing in [*first.listings, *final.listings]
    }


def test_a_closed_bamboohr_identity_stays_closed_across_detail_slices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.board_adapters.transport.time.monotonic",
        lambda: now[0],
    )

    class ClosedOpening(BoardApiFetcher):
        def __init__(self) -> None:
            super().__init__()
            self.seconds = 40.0
            self.closed = True
            self.requested: list[str] = []

        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            if url.endswith("/careers/list"):
                return {"result": [_bamboohr_row(id="77"), _bamboohr_row(id="78")]}
            item = url.split("/careers/", 1)[1].split("/", 1)[0]
            self.requested.append(item)
            now[0] += self.seconds
            return {
                "result": {
                    "jobOpening": _bamboohr_opening(
                        jobOpeningStatus="Closed" if self.closed and item == "77" else "Open",
                        jobOpeningShareUrl=f"https://globex.bamboohr.com/careers/{item}",
                    )
                }
            }

    board = "https://globex.bamboohr.com/careers"
    fetcher = ClosedOpening()
    first = fetcher.listings("Initech", board)
    [cursor] = first.deferred_targets
    assert cursor.detail_checkpoint is not None
    assert first.detail_read_urls == ()
    assert cursor.detail_checkpoint.closed_ids == ("77",)
    fetcher.seconds = 0
    fetcher.closed = False
    final = fetcher.continue_listings("Initech", (cursor,), root_url=board, handoff_scopes=())

    assert final.complete is True
    assert final.detail_coverage is not None
    assert final.detail_coverage.posting_urls == ("https://globex.bamboohr.com/careers/78",)
    assert final.detail_read_urls == ("https://globex.bamboohr.com/careers/78",)
    assert fetcher.requested == ["77", "78"]


def test_a_failed_jazzhr_page_remains_retryable_beside_successful_openings() -> None:
    requested: list[str] = []
    pages = {
        _JAZZHR_BOARD: _JAZZHR_LIST_PAGE,
        _JAZZHR_ANALYST: "<html>No readable posting declaration</html>",
        _JAZZHR_ENGINEER: _jazzhr_posting_page("Data Engineer", "Northtown"),
    }
    fetcher = _jazzhr_fetcher(pages, requested=requested)
    first = fetcher.listings("Initech", _JAZZHR_BOARD)
    [cursor] = first.deferred_targets

    assert cursor.detail_checkpoint is not None
    assert cursor.detail_checkpoint.failed_attempts == (("Xq7Lm2Pz9K", 1),)
    assert first.detail_read_urls == (_JAZZHR_ENGINEER,)
    assert [listing.title for listing in first.listings] == ["Data Engineer"]
    pages[_JAZZHR_ANALYST] = _jazzhr_posting_page("Clinical Data Analyst", "Northtown")
    final = fetcher.continue_listings(
        "Initech", (cursor,), root_url=_JAZZHR_BOARD, handoff_scopes=()
    )

    assert final.complete is True
    assert final.detail_coverage is not None
    assert set(final.detail_coverage.posting_urls) == {_JAZZHR_ANALYST, _JAZZHR_ENGINEER}
    assert set(final.detail_read_urls or ()) == {_JAZZHR_ANALYST, _JAZZHR_ENGINEER}
    assert requested.count(_JAZZHR_ENGINEER) == 1


def test_coverage_for_two_distinct_detail_boards_is_not_combined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = _CheckpointAdp(monkeypatch)
    [first] = fetcher.listings("Initech", _ADP_BOARD).deferred_targets
    other_board = _ADP_BOARD.replace(_ADP_CID, "11111111-2222-4333-8444-555555555555")
    [second] = fetcher.listings("Initech", other_board).deferred_targets
    fetcher.seconds = 0

    read = fetcher.continue_listings(
        "Initech", (first, second), root_url=_ADP_BOARD, handoff_scopes=()
    )

    assert read.detail_coverage is None
    assert read.complete is False


def test_a_legacy_positional_cursor_without_state_reads_every_current_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = _CheckpointAdp(monkeypatch)
    [cursor] = fetcher.listings("Initech", _ADP_BOARD).deferred_targets
    legacy = replace(cursor, url=_ADP_BOARD + "&detail_offset=999", detail_checkpoint=None)
    fetcher.seconds = 0
    fetcher.requested.clear()

    read = fetcher.continue_listings("Initech", (legacy,), root_url=_ADP_BOARD, handoff_scopes=())

    assert read.complete is True
    assert fetcher.requested == [row["itemID"] for row in fetcher.rows]


def test_an_adp_full_repeated_page_without_a_total_cannot_publish_coverage() -> None:
    rows = [_adp_requisition(itemID=f"92000000000{index:02d}_1") for index in range(20)]

    class RepeatedPage(BoardApiFetcher):
        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            if url.startswith(_ADP_API + "?"):
                return {"jobRequisitions": rows, "meta": {}}
            return _adp_requisition(itemID=url.partition("?")[0].rsplit("/", 1)[1])

    read = RepeatedPage().listings("Initech", _ADP_BOARD)

    assert read.complete is False
    assert read.detail_coverage is None
    assert read.outcome is SourceReadOutcome.NO_SUPPORTED_REPRESENTATION


def test_a_smartrecruiters_listing_states_its_released_date() -> None:
    job = _smartrecruiters_job(releasedDate="2026-10-01T15:51:25.123Z")

    listing = source_listing(
        SMARTRECRUITERS_MAPPING, Posting(job, employer="Globex", groups={"org": "globex"})
    )

    assert listing is not None
    posted = listing.facts.posted_date
    assert posted is not None
    assert posted.earliest.basis == "releasedDate"
    assert posted.earliest.normalized_value == "2026-10-01T15:51:25.123000+00:00"
    assert posted.earliest.grade is PostedDateGrade.FIRST_POSTED
