"""A board declaration maps a published record without losing a platform's own rule."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any

import pytest

from clearscout.application.discovery.screening import (
    ScreeningCode,
    ScreeningDimension,
    screen_discovery,
)
from clearscout.application.discovery.source_lifecycle import employer_name_is_authoritative
from clearscout.application.discovery.source_reading import SourceRead
from clearscout.domain import SourceReadOutcome
from clearscout.domain.artifacts import PostingSource
from clearscout.domain.search_parameters import JobSearchParameters, Location
from clearscout.domain.staged_facts import FreshnessState, ListingIdentityTrust, PostedDateGrade
from clearscout.infrastructure.web_fetch.application_form import required_residence_question
from clearscout.infrastructure.web_fetch.board_mappings.ashby import ASHBY_MAPPING
from clearscout.infrastructure.web_fetch.board_mappings.greenhouse import GREENHOUSE_MAPPING
from clearscout.infrastructure.web_fetch.board_mappings.lever import LEVER_MAPPING
from clearscout.infrastructure.web_fetch.board_mappings.mapper import (
    BoardDeclaration,
    Posting,
    posting_check,
    source_listing,
    source_listings,
)
from clearscout.infrastructure.web_fetch.board_mappings.schema_org import (
    declared_check,
    declared_listing,
)
from clearscout.infrastructure.web_fetch.board_mappings.workday import WORKDAY_DETAIL_MAPPING
from clearscout.infrastructure.web_fetch.ingest.job_posting_markup import (
    BaseSalaryMarkup,
    JobPostingMarkup,
)
from clearscout.infrastructure.web_fetch.listing_facts import lever_arrangement_values
from clearscout.infrastructure.web_fetch.platforms.lever import lever_form_cards
from tests.support.application_forms import (
    FORM_PLATFORMS,
    FormPlatform,
    form_source,
    lever_apply_page,
    lever_cards,
)
from tests.support.doubles import ScriptedApi, ScriptedPage, declaring_page, published_listings


def _lever_job(**stated: Any) -> dict[str, Any]:
    return {
        "id": "a1b2",
        "text": "Clinical Systems Analyst",
        "hostedUrl": "https://jobs.lever.co/globex/a1b2",
        "categories": {"location": "Northtown", "commitment": "Full-time"},
        "workplaceType": "hybrid",
        "descriptionPlain": "You will configure clinical systems for hospital teams.",
        **stated,
    }


_CONTRADICTIONS = [
    pytest.param(
        {"descriptionPlain": "This role can be performed fully remote within the country."},
        id="body-remote-offer",
    ),
    pytest.param(
        {"categories": {"location": "Northtown", "allLocations": ["Northtown", "Remote"]}},
        id="remote-place",
    ),
    pytest.param({"text": "Clinical Systems Analyst (Remote)"}, id="remote-title"),
    pytest.param(
        {"lists": [{"text": "Where you work", "content": "<p>Work from home is offered.</p>"}]},
        id="list-section-remote-offer",
    ),
]


@pytest.mark.parametrize("stated", _CONTRADICTIONS)
def test_a_contradicted_lever_arrangement_stays_unresolved_in_a_board_listing(
    stated: dict[str, Any],
) -> None:
    listing = source_listing(LEVER_MAPPING, Posting(_lever_job(**stated), employer="Globex"))

    assert listing is not None
    assert listing.facts.arrangements.values == ()
    assert "hybrid" in listing.facts.arrangements.unresolved_values
    assert listing.facts.arrangements.complete is False


@pytest.mark.parametrize("stated", _CONTRADICTIONS)
def test_a_contradicted_lever_arrangement_stays_unresolved_in_a_single_posting_check(
    stated: dict[str, Any],
) -> None:
    checked = posting_check(
        LEVER_MAPPING, Posting(_lever_job(**stated)), note="Lever published this."
    )

    assert checked.facts.arrangements.values == ()
    assert "hybrid" in checked.facts.arrangements.unresolved_values
    assert checked.facts.arrangements.complete is False


def test_an_uncontradicted_lever_arrangement_is_a_complete_fact() -> None:
    listing = source_listing(LEVER_MAPPING, Posting(_lever_job(), employer="Globex"))

    assert listing is not None
    assert listing.facts.arrangements.values == ("hybrid",)
    assert listing.facts.arrangements.unresolved_values == ()
    assert listing.facts.arrangements.complete is True
    assert listing.facts.arrangements.provenance == "lever.arrangement"


@pytest.mark.parametrize(
    ("salary", "expected"),
    [
        pytest.param(
            {"min": 90000, "max": 110000, "currency": "CAD", "interval": "per-year-salary"},
            ("CAD 90000-110000 per year",),
            id="full-range",
        ),
        pytest.param(
            {"min": 45, "max": 55, "currency": "USD", "interval": "per-week-wage"},
            ("USD 45-55",),
            id="period-screening-cannot-compare",
        ),
        pytest.param(
            {"min": 4000, "max": 5000, "currency": "CAD", "interval": "semi-monthly-salary"},
            ("CAD 4000-5000",),
            id="qualified-period-names-none",
        ),
        pytest.param({"min": 90000, "currency": "CAD"}, (), id="floor-only"),
    ],
)
def test_a_lever_salary_range_is_stated_with_the_parts_it_publishes(
    salary: dict[str, Any], expected: tuple[str, ...]
) -> None:
    listing = source_listing(LEVER_MAPPING, Posting(_lever_job(salaryRange=salary)))

    assert listing is not None
    assert listing.facts.compensation.values == expected


def test_a_lever_listing_states_its_fields_and_every_description_section() -> None:
    job = _lever_job(
        country="DE",
        lists=[{"text": "What you will do", "content": "<ul><li>Validate builds</li></ul>"}],
        additionalPlain="Globex is an equal opportunity employer.",
    )

    listing = source_listing(LEVER_MAPPING, Posting(job, employer="Globex"))

    assert listing is not None
    assert (listing.employer, listing.title) == ("Globex", "Clinical Systems Analyst")
    assert listing.url == "https://jobs.lever.co/globex/a1b2"
    assert listing.requisition_id == "a1b2"
    assert listing.location == "Northtown"
    assert listing.facts.locations.values == ("Northtown", "Germany")
    assert listing.facts.employment_types.values == ("Full-time",)
    assert listing.published_description == (
        "Clinical Systems Analyst\n\nNorthtown\n\n"
        "You will configure clinical systems for hospital teams.\n\n"
        "What you will do\n\n- Validate builds\n\nGlobex is an equal opportunity employer."
    )


def test_a_record_without_a_title_or_an_address_is_not_a_listing() -> None:
    assert (
        source_listing(LEVER_MAPPING, Posting(_lever_job(hostedUrl=""), employer="Globex")) is None
    )
    assert source_listing(LEVER_MAPPING, Posting(_lever_job(text=" "), employer="Globex")) is None


def test_a_single_posting_check_reads_the_employer_the_record_states() -> None:
    checked = posting_check(
        LEVER_MAPPING, Posting(_lever_job(company="Globex Health")), note="Lever published this."
    )

    assert checked.status == "readable"
    assert checked.platform == "lever"
    assert checked.employer == "Globex Health"
    assert checked.text == (
        "Clinical Systems Analyst\n\nNorthtown\n\n"
        "You will configure clinical systems for hospital teams."
    )


def _greenhouse_job(**stated: Any) -> dict[str, Any]:
    return {
        "id": 4412,
        "title": "Clinical Systems Analyst",
        "absolute_url": "https://boards.greenhouse.io/globex/jobs/4412",
        "location": {"name": "Northtown"},
        "content": "&lt;p&gt;You will configure clinical systems.&lt;/p&gt;",
        **stated,
    }


_EMPLOYER_RECORDS = [
    pytest.param(
        GREENHOUSE_MAPPING, _greenhouse_job(company_name="Globex Health"), id="stated-at-a-path"
    ),
    pytest.param(LEVER_MAPPING, _lever_job(company="Globex Health"), id="stated-by-a-derivation"),
]


@pytest.mark.parametrize(("declaration", "record"), _EMPLOYER_RECORDS)
def test_a_board_listing_prefers_the_employer_the_publisher_states(
    declaration: BoardDeclaration, record: dict[str, Any]
) -> None:
    listings = source_listings(declaration, [record], employer="Globexhealth")

    assert [listing.employer for listing in listings] == ["Globex Health"]


@pytest.mark.parametrize(("declaration", "record"), _EMPLOYER_RECORDS)
def test_a_board_listing_keeps_an_authoritative_employer_over_the_stated_one(
    declaration: BoardDeclaration, record: dict[str, Any]
) -> None:
    listings = source_listings(
        declaration, [record], employer="Initech", employer_is_authoritative=True
    )

    assert [listing.employer for listing in listings] == ["Initech"]


@pytest.mark.parametrize("prefer_publisher", [False, True])
def test_a_declared_publisher_preference_requires_a_stated_name_to_replace_the_source(
    prefer_publisher: bool,
) -> None:
    declaration = replace(
        GREENHOUSE_MAPPING,
        employer=lambda posting: posting.employer.upper(),
        prefer_publisher_employer=prefer_publisher,
    )

    listing = source_listing(
        declaration,
        Posting(_greenhouse_job(), employer="Initech", employer_is_authoritative=True),
    )

    assert listing is not None
    assert listing.employer == "Initech"
    assert listing.facts.identity_trust == ListingIdentityTrust.PUBLISHER


_ESCAPED_JOB = _greenhouse_job(
    title="Platform Reliability &amp; Data Lead",
    company_name="Globex Health &amp; Research",
    location={"name": "Northtown &#38; Remote"},
)


def test_a_board_listing_and_a_single_posting_check_decode_entities_in_their_names() -> None:
    listing = source_listing(GREENHOUSE_MAPPING, Posting(_ESCAPED_JOB, employer="Globex"))
    checked = posting_check(GREENHOUSE_MAPPING, Posting(_ESCAPED_JOB), note="Greenhouse.")

    decoded = ("Platform Reliability & Data Lead", "Globex Health & Research", "Northtown & Remote")
    assert listing is not None
    assert (listing.title, listing.employer, listing.location) == decoded
    assert (checked.title, checked.employer, checked.location) == decoded


def test_a_single_posting_check_carries_its_source_only_beside_a_body() -> None:
    bodiless = _greenhouse_job(content="")

    with_body = posting_check(GREENHOUSE_MAPPING, Posting(_greenhouse_job()), note="Greenhouse.")
    without = posting_check(GREENHOUSE_MAPPING, Posting(bodiless), note="Greenhouse.")

    assert with_body.source is not None
    assert with_body.source.description_markup == _greenhouse_job()["content"]
    assert (without.text, without.source) == ("", None)


@pytest.mark.parametrize("summary", ["Northtown &amp; Remote", "Northtown &#38; Remote"])
def test_an_entity_in_a_greenhouse_location_summary_does_not_split_the_place(summary: str) -> None:
    job = _greenhouse_job(location={"name": summary})

    listing = source_listing(GREENHOUSE_MAPPING, Posting(job, employer="Globex"))
    checked = posting_check(GREENHOUSE_MAPPING, Posting(job), note="Greenhouse.")

    assert listing is not None
    assert (
        listing.facts.locations.values == checked.facts.locations.values == ("Northtown & Remote",)
    )


def test_a_board_listing_and_a_single_posting_check_decode_entities_in_their_places() -> None:
    job = _lever_job(
        categories={
            "location": "Northtown &amp; Harbour",
            "allLocations": ["Northtown &amp; Harbour", "Southport &#38; Bay"],
        }
    )

    listing = source_listing(LEVER_MAPPING, Posting(job, employer="Globex"))
    checked = posting_check(LEVER_MAPPING, Posting(job), note="Lever published this.")

    assert listing is not None
    assert (
        listing.facts.locations.values
        == checked.facts.locations.values
        == (
            "Northtown & Harbour",
            "Southport & Bay",
        )
    )


_NAMELESS_RECORDS = [
    pytest.param(GREENHOUSE_MAPPING, _greenhouse_job(), id="path-states-nothing"),
    pytest.param(LEVER_MAPPING, _lever_job(), id="derivation-states-nothing"),
]


@pytest.mark.parametrize(("declaration", "record"), _EMPLOYER_RECORDS)
def test_a_board_listing_the_record_names_is_stamped_at_publisher_trust(
    declaration: BoardDeclaration, record: dict[str, Any]
) -> None:
    listings = source_listings(declaration, [record], employer="Globexhealth")

    assert [listing.facts.identity_trust for listing in listings] == [
        ListingIdentityTrust.PUBLISHER
    ]


@pytest.mark.parametrize(("declaration", "record"), _NAMELESS_RECORDS)
def test_a_board_listing_named_only_by_the_configuration_is_an_unverified_hint(
    declaration: BoardDeclaration, record: dict[str, Any]
) -> None:
    listings = source_listings(declaration, [record], employer="Globexhealth")

    assert [(listing.employer, listing.facts.identity_trust) for listing in listings] == [
        ("Globexhealth", ListingIdentityTrust.UNVERIFIED_HINT)
    ]


@pytest.mark.parametrize(("declaration", "record"), _NAMELESS_RECORDS)
def test_a_hand_named_board_employer_is_stamped_at_publisher_trust(
    declaration: BoardDeclaration, record: dict[str, Any]
) -> None:
    listings = source_listings(
        declaration, [record], employer="Initech", employer_is_authoritative=True
    )

    assert [(listing.employer, listing.facts.identity_trust) for listing in listings] == [
        ("Initech", ListingIdentityTrust.PUBLISHER)
    ]


def test_a_board_whose_platform_states_no_employer_keeps_publisher_trust() -> None:
    record = {"id": "as-1", "title": "Staff Engineer", "jobUrl": "https://example.test/as-1"}

    listings = source_listings(ASHBY_MAPPING, [record], employer="Globexhealth")

    assert [(listing.employer, listing.facts.identity_trust) for listing in listings] == [
        ("Globexhealth", ListingIdentityTrust.PUBLISHER)
    ]


def test_a_nameless_board_listing_from_an_evaluation_source_is_stamped_at_publisher_trust() -> None:
    listings = source_listings(
        GREENHOUSE_MAPPING,
        [_greenhouse_job()],
        employer="Initech",
        employer_is_authoritative=employer_name_is_authoritative("evaluation"),
    )

    assert [(listing.employer, listing.facts.identity_trust) for listing in listings] == [
        ("Initech", ListingIdentityTrust.PUBLISHER)
    ]


def test_a_nameless_board_listing_from_a_guided_campaign_source_is_an_unverified_hint() -> None:
    listings = source_listings(
        GREENHOUSE_MAPPING,
        [_greenhouse_job()],
        employer="Initech",
        employer_is_authoritative=employer_name_is_authoritative("search.guided_campaign"),
    )

    assert [(listing.employer, listing.facts.identity_trust) for listing in listings] == [
        ("Initech", ListingIdentityTrust.UNVERIFIED_HINT)
    ]


def test_a_platform_stating_no_employer_keeps_publisher_trust_for_every_source_origin() -> None:
    """Pin the platform guard: widening the authoritative origins must not unvouch these."""
    record = {"id": "as-1", "title": "Staff Engineer", "jobUrl": "https://example.test/as-1"}

    listings = source_listings(
        ASHBY_MAPPING,
        [record],
        employer="Initech",
        employer_is_authoritative=employer_name_is_authoritative("search.guided_campaign"),
    )

    assert [(listing.employer, listing.facts.identity_trust) for listing in listings] == [
        ("Initech", ListingIdentityTrust.PUBLISHER)
    ]


_WORK_SETTING = [{"name": "Work Setting", "value": "Hybrid"}]


def test_a_single_greenhouse_posting_reads_the_work_setting_the_board_listing_reads() -> None:
    job = _greenhouse_job(metadata=_WORK_SETTING, company_name="Globex Health")

    checked = posting_check(GREENHOUSE_MAPPING, Posting(job), note="Greenhouse published this.")

    assert checked.facts.arrangements.values == ("Hybrid",)
    assert checked.facts.arrangements.complete is True
    assert checked.employer == "Globex Health"
    assert checked.text == (
        "Clinical Systems Analyst\n\nNorthtown\n\nYou will configure clinical systems."
    )


@pytest.mark.parametrize(
    "stated",
    [
        pytest.param(
            {"metadata": _WORK_SETTING, "content": "<p>This role is open to remote work.</p>"},
            id="work-setting-against-body",
        ),
        pytest.param(
            {"metadata": _WORK_SETTING, "offices": [{"name": "Northtown"}, {"name": "Remote"}]},
            id="work-setting-against-remote-office",
        ),
        pytest.param(
            {"content": "<p>Work from home is offered for this team.</p><p>#LI-Hybrid</p>"},
            id="content-tag-against-body",
        ),
        pytest.param(
            {
                "location": {"name": "Remote"},
                "content": "<p>This hybrid role is based in Northtown and requires commuting "
                "to our office up to three days per week.</p>",
            },
            id="remote-location-against-hybrid-body",
        ),
    ],
)
def test_a_contradicted_greenhouse_arrangement_stays_unresolved_in_both_reads(
    stated: dict[str, Any],
) -> None:
    job = _greenhouse_job(**stated)

    listing = source_listing(GREENHOUSE_MAPPING, Posting(job, employer="Globex"))
    checked = posting_check(GREENHOUSE_MAPPING, Posting(job), note="Greenhouse published this.")

    assert listing is not None
    for facts in (listing.facts, checked.facts):
        assert facts.arrangements.values == ()
        assert facts.arrangements.unresolved_values
        assert facts.arrangements.complete is False


def test_a_greenhouse_content_tag_states_the_arrangement_when_no_work_setting_is_configured() -> (
    None
):
    job = _greenhouse_job(
        title="Systems Analyst",
        content="<p>You will configure reporting systems.</p><p>#LI-Remote</p>",
    )

    listing = source_listing(GREENHOUSE_MAPPING, Posting(job, employer="Globex"))

    assert listing is not None
    assert listing.facts.arrangements.values == ("remote",)
    assert listing.published_description == (
        "Systems Analyst\n\nNorthtown\n\nYou will configure reporting systems.\n\n#LI-Remote"
    )


_READ_AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
_LONG_BODY = " ".join(["You will configure clinical systems for hospital teams."] * 10)


def test_a_declared_posting_keeps_the_description_its_markup_states() -> None:
    markup = JobPostingMarkup(
        title="Systems Analyst",
        description=f"<p>{_LONG_BODY}</p><h3>Responsibilities</h3><p>Validate builds.</p>",
        description_paths=("description", "responsibilities"),
        locations=("Northtown",),
        location="Northtown",
        locations_complete=True,
        node='{"@type": "JobPosting", "title": "Systems Analyst"}',
    )

    listing = declared_listing(
        markup,
        employer="Globex",
        employer_is_authoritative=True,
        page_url="https://careers.globex.example/1",
        read_at=_READ_AT,
        page_markup="",
    )

    assert listing.url == "https://careers.globex.example/1"
    assert listing.employer == "Globex"
    assert listing.published_description == (
        f"{_LONG_BODY}\n\nResponsibilities\n\nValidate builds."
    )
    assert listing.summary == f"{_LONG_BODY} Responsibilities Validate builds."
    assert listing.facts.locations.complete is True
    assert listing.source == PostingSource(
        publisher="page",
        record=markup.node,
        description_markup=markup.description,
        description_paths=("description", "responsibilities"),
    )


@pytest.mark.parametrize(
    ("valid_through", "expected"),
    [
        ("2026-09-01", FreshnessState.LIKELY_STALE),
        ("2026-09-17T11:59:00+00:00", FreshnessState.LIKELY_STALE),
        ("2026-12-01", FreshnessState.UNKNOWN),
        ("next quarter", FreshnessState.UNKNOWN),
        ("", FreshnessState.UNKNOWN),
    ],
)
def test_only_a_stated_expiry_in_the_past_marks_a_declared_posting_likely_stale(
    valid_through: str, expected: FreshnessState
) -> None:
    markup = JobPostingMarkup(title="Clinical Systems Analyst", valid_through=valid_through)

    listing = declared_listing(
        markup,
        employer="Globex",
        employer_is_authoritative=True,
        page_url="https://careers.globex.example/1",
        read_at=_READ_AT,
        page_markup="",
    )

    assert listing.facts.freshness is expected
    assert listing.facts.closing_date is not None
    assert listing.facts.closing_date.source_url == "https://careers.globex.example/1"
    assert listing.facts.closing_date.observed_at == _READ_AT
    if valid_through:
        assert listing.facts.closing_date.statements[0].wording == valid_through
        assert listing.facts.closing_date.statements[0].basis == "valid_through"
    else:
        assert listing.facts.closing_date.statements == ()


@pytest.mark.parametrize(
    ("declaration", "record"),
    [
        pytest.param(LEVER_MAPPING, _lever_job(), id="lever"),
        pytest.param(GREENHOUSE_MAPPING, _greenhouse_job(), id="greenhouse"),
    ],
)
def test_a_board_listing_and_a_single_read_retain_the_same_body(
    declaration: BoardDeclaration, record: dict[str, Any]
) -> None:
    listing = source_listing(declaration, Posting(record, employer="Globex"))
    checked = posting_check(declaration, Posting(record), note="Published.")

    assert listing is not None
    assert listing.published_description
    assert listing.published_description == checked.text
    assert listing.source is not None
    assert listing.source == checked.source


def test_a_record_stating_no_description_retains_no_body() -> None:
    listing = source_listing(
        LEVER_MAPPING, Posting(_lever_job(descriptionPlain=""), employer="Globex")
    )

    assert listing is not None
    assert (listing.published_description, listing.source) == ("", None)


_WRAPPED_JOB = "0f5c2d1e-7a3b-4c9d-8e6f-1a2b3c4d5e6f"
_WRAPPER_URL = f"https://careers.globex.example/job/{_WRAPPED_JOB}/systems-analyst/"


def _wrapper_page(apply_url: str) -> str:
    return f'<main><h1>Systems Analyst</h1><a href="{apply_url}">Apply now</a></main>'


def _wrapper_listing(apply_url: str) -> str:
    return declared_listing(
        JobPostingMarkup(title="Systems Analyst", url=_WRAPPER_URL),
        employer="Globex",
        employer_is_authoritative=True,
        page_url=_WRAPPER_URL,
        read_at=_READ_AT,
        page_markup=_wrapper_page(apply_url),
    ).requisition_id


@pytest.mark.parametrize("host", ["jobs.lever.co", "jobs.eu.lever.co"])
def test_a_careers_page_linking_the_lever_posting_its_url_names_states_that_requisition(
    host: str,
) -> None:
    assert _wrapper_listing(f"https://{host}/globex/{_WRAPPED_JOB}/apply") == _WRAPPED_JOB


@pytest.mark.parametrize(
    "apply_url",
    [
        pytest.param(
            "https://jobs.lever.co/globex/9e8d7c6b-5a4f-4e3d-8c2b-1a0f9e8d7c6b/apply",
            id="another-lever-posting",
        ),
        pytest.param(
            f"https://jobs.ashbyhq.com/globex/{_WRAPPED_JOB}/application", id="ashby-posting"
        ),
        pytest.param("https://careers.globex.example/apply", id="no-board-link"),
    ],
)
def test_a_careers_page_not_linking_the_lever_posting_its_url_names_states_no_requisition(
    apply_url: str,
) -> None:
    assert _wrapper_listing(apply_url) == ""


def test_a_declared_description_too_short_to_be_a_body_is_kept_only_as_a_summary() -> None:
    markup = JobPostingMarkup(title="Clinical Systems Analyst", description="<p>Apply now.</p>")

    listing = declared_listing(
        markup,
        employer="Globex",
        employer_is_authoritative=True,
        page_url="https://careers.globex.example/1",
        read_at=_READ_AT,
        page_markup="",
    )

    assert listing.summary == "Apply now."
    assert (listing.published_description, listing.source) == ("", None)


@pytest.mark.parametrize(
    ("salary", "expected"),
    [
        pytest.param(
            BaseSalaryMarkup(currency="USD", minimum="90000", maximum="120000", unit="YEAR"),
            ("USD 90000-120000 per year",),
            id="range",
        ),
        pytest.param(
            BaseSalaryMarkup(currency="CAD", minimum="55", maximum="65.50", unit="hour"),
            ("CAD 55-65.50 per hour",),
            id="hourly-range",
        ),
        pytest.param(
            BaseSalaryMarkup(currency="CAD", maximum="65.50", unit="hour"), (), id="ceiling-only"
        ),
        pytest.param(
            BaseSalaryMarkup(currency="USD", minimum="9e4", maximum="1e5", unit="YEAR"),
            (),
            id="exponent-notation",
        ),
        pytest.param(
            BaseSalaryMarkup(currency="USD", minimum="120000", maximum="90000", unit="YEAR"),
            (),
            id="floor-above-ceiling",
        ),
        pytest.param(
            BaseSalaryMarkup(currency="USD", minimum="90000", unit="YEAR"), (), id="floor-only"
        ),
        pytest.param(
            BaseSalaryMarkup(currency="USD", minimum="0", maximum="0", unit="YEAR"),
            (),
            id="zero-placeholder",
        ),
        pytest.param(BaseSalaryMarkup(currency="USD", maximum="2000", unit="WEEK"), (), id="week"),
        pytest.param(BaseSalaryMarkup(maximum="120000", unit="YEAR"), (), id="no-currency"),
        pytest.param(
            BaseSalaryMarkup(currency="dollars", maximum="120000", unit="YEAR"),
            (),
            id="currency-not-a-code",
        ),
        pytest.param(None, (), id="unstated"),
    ],
)
def test_a_declared_salary_is_a_fact_only_when_every_part_is_stated(
    salary: BaseSalaryMarkup | None, expected: tuple[str, ...]
) -> None:
    markup = JobPostingMarkup(title="Clinical Systems Analyst", base_salary=salary)

    checked = declared_check(markup, page_url="https://careers.globex.example/1", read_at=_READ_AT)

    assert checked.facts is not None
    assert checked.facts.compensation.values == expected
    assert checked.facts.compensation.complete is bool(expected)


def test_a_declared_date_posted_is_a_board_date_in_utc() -> None:
    markup = JobPostingMarkup(
        title="Clinical Systems Analyst", date_posted="Thu Sep 17 02:00:00 UTC 2026"
    )

    checked = declared_check(markup, page_url="https://careers.globex.example/1", read_at=_READ_AT)

    posted = checked.facts.posted_date
    assert posted is not None
    assert posted.earliest.basis == "datePosted"
    assert posted.earliest.normalized_value == "2026-09-17T02:00:00+00:00"
    assert posted.earliest.grade is PostedDateGrade.BOARD_DATE


def test_a_declared_listing_and_a_declared_check_state_the_same_facts() -> None:
    markup = JobPostingMarkup(
        title="Clinical Systems Analyst",
        employer="Globex",
        locations=("Northtown",),
        locations_complete=True,
        employment_types=("FULL_TIME", "CONTRACTOR"),
        base_salary=BaseSalaryMarkup(currency="USD", maximum="120000", unit="YEAR"),
    )

    listing = declared_listing(
        markup,
        employer="Globex",
        employer_is_authoritative=False,
        page_url="https://careers.globex.example/1",
        read_at=_READ_AT,
        page_markup="",
    )
    checked = declared_check(markup, page_url="https://careers.globex.example/1", read_at=_READ_AT)

    assert listing.facts == checked.facts
    assert listing.facts.employment_types.values == ("FULL_TIME", "CONTRACTOR")


@pytest.mark.parametrize(
    ("page_url", "declared_employer", "expected"),
    [
        pytest.param(
            "https://jobs.initrode.example/listing/1",
            "jobs.initrode.example",
            False,
            id="host-named-as-the-employer",
        ),
        pytest.param(
            "https://jobs.initrode.example/listing/1", "Globex", False, id="aggregator-names-one"
        ),
        pytest.param(
            "https://careers.globexhealth.example/1",
            "Globex Health, Inc.",
            True,
            id="employer-is-the-host",
        ),
        pytest.param(
            "https://job-boards.greenhouse.io/globex/jobs/4001",
            "Aster Systems",
            True,
            id="single-tenant-board",
        ),
        pytest.param("https://careers.aim.example/1", "Aim", False, id="name-too-short"),
        pytest.param("https://notglobex.example/1", "Globex", False, id="label-is-not-the-name"),
        pytest.param("https://jobs.initrode.example/1", "Jobs", False, id="generic-label"),
    ],
)
def test_declared_markup_vouches_for_an_employer_only_when_the_page_answers_for_it(
    page_url: str, declared_employer: str, expected: bool
) -> None:
    markup = JobPostingMarkup(title="Clinical Systems Analyst", employer=declared_employer)

    checked = declared_check(markup, page_url=page_url, read_at=_READ_AT)

    assert checked.facts is not None
    assert checked.facts.publisher_vouched is expected


@pytest.mark.parametrize(
    ("page_url", "source_employer", "authoritative", "expected"),
    [
        pytest.param(
            "https://jobs.vendor.example/nw/1", "Northwind", True, True, id="hand-named-source"
        ),
        pytest.param(
            "https://jobs.vendor.example/nw/1", "Northwind", False, False, id="source-named-itself"
        ),
        pytest.param("https://jobs.vendor.example/nw/1", "", True, False, id="nobody-named"),
        pytest.param(
            "https://job-boards.greenhouse.io/northwind/jobs/4001",
            "",
            False,
            True,
            id="one-tenant",
        ),
    ],
)
def test_markup_naming_no_employer_is_vouched_only_by_a_hand_named_source_or_a_one_tenant_board(
    page_url: str, source_employer: str, authoritative: bool, expected: bool
) -> None:
    markup = JobPostingMarkup(title="Clinical Systems Analyst")

    listing = declared_listing(
        markup,
        employer=source_employer,
        employer_is_authoritative=authoritative,
        page_url=page_url,
        read_at=_READ_AT,
        page_markup="",
    )

    assert listing.facts.publisher_vouched is expected


@pytest.mark.parametrize(
    ("source_employer", "authoritative", "expected"),
    [
        pytest.param("Northwind", True, True, id="hand-named-source-names-it"),
        pytest.param("Northwind", False, True, id="campaign-source-names-it"),
        pytest.param("", True, False, id="no-source"),
    ],
)
def test_a_source_naming_the_declared_employer_vouches_for_it(
    source_employer: str, authoritative: bool, expected: bool
) -> None:
    markup = JobPostingMarkup(title="Clinical Systems Analyst", employer="Northwind")

    listing = declared_listing(
        markup,
        employer=source_employer,
        employer_is_authoritative=authoritative,
        page_url="https://jobs.vendor.example/nw/1",
        read_at=_READ_AT,
        page_markup="",
    )

    assert listing.facts.publisher_vouched is expected


@pytest.mark.parametrize(
    ("page_url", "declared_employer", "expected_employer"),
    [
        pytest.param(
            "https://jobs.initrode.example/listing/1",
            "jobs.initrode.example",
            "Northwind",
            id="name-is-the-page-host",
        ),
        pytest.param(
            "https://www.initrode.example/listing/1",
            "initrode.example",
            "Northwind",
            id="name-is-the-host-without-www",
        ),
        pytest.param(
            "https://careers.globexhealth.example/1",
            "Globex Health, Inc.",
            "Globex Health, Inc.",
            id="employer-named-on-its-own-host",
        ),
        pytest.param(
            "https://jobs.northwind.example/1",
            "Northwind Systems",
            "Northwind Systems",
            id="name-shares-a-label-with-the-host",
        ),
    ],
)
def test_a_declared_employer_spelling_the_page_host_gives_way_to_the_source_name(
    page_url: str, declared_employer: str, expected_employer: str
) -> None:
    markup = JobPostingMarkup(title="Systems Analyst", employer=declared_employer)

    listing = declared_listing(
        markup,
        employer="Northwind",
        employer_is_authoritative=True,
        page_url=page_url,
        read_at=_READ_AT,
        page_markup="",
    )

    assert listing.employer == expected_employer
    assert listing.facts.publisher_vouched is True


def test_a_page_host_named_as_the_employer_with_no_source_leaves_the_row_unnamed() -> None:
    markup = JobPostingMarkup(title="Systems Analyst", employer="jobs.initrode.example")
    page_url = "https://jobs.initrode.example/listing/1"

    listing = declared_listing(
        markup,
        employer="",
        employer_is_authoritative=False,
        page_url=page_url,
        read_at=_READ_AT,
        page_markup="",
    )
    checked = declared_check(markup, page_url=page_url, read_at=_READ_AT)

    assert listing.employer == checked.employer == ""
    assert listing.facts.publisher_vouched is False


def test_greenhouse_listings_are_read_from_its_documented_payload() -> None:
    """Map the board's own field names, and drop an entry with nowhere to point."""
    fetcher = ScriptedApi(
        {
            "jobs": [
                {
                    "id": 4001,
                    "title": "  Data Platform Lead  ",
                    "absolute_url": "https://boards.greenhouse.io/acme/jobs/4001",
                    "location": {"name": "Remote, Canada"},
                },
                {"id": 4002, "title": "No link", "location": {"name": "Toronto"}},
                {"id": 4003, "title": "Sparse", "absolute_url": "https://x.test/3"},
            ]
        }
    )

    listings = published_listings(fetcher, "Acme", "https://boards.greenhouse.io/acme")

    assert fetcher.requested == [
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
    ]
    assert listings is not None
    assert [item.title for item in listings] == ["Data Platform Lead", "Sparse"]
    assert listings[0].employer == "Acme"
    assert listings[0].url == "https://boards.greenhouse.io/acme/jobs/4001"
    assert listings[0].location == "Remote, Canada"
    assert listings[0].requisition_id == "4001"
    # Do not infer a location the posting does not state.
    assert listings[1].location == ""


def test_greenhouse_keeps_the_full_published_description_untruncated() -> None:
    """The board summary stays bounded for screening; retention keeps the whole body."""
    body = "Own the data platform. " * 400
    fetcher = ScriptedApi(
        {
            "jobs": [
                {
                    "id": 5001,
                    "title": "Data Platform Lead",
                    "absolute_url": "https://boards.greenhouse.io/acme/jobs/5001",
                    "location": {"name": "Remote"},
                    "content": f"<p>{body}</p>",
                }
            ]
        }
    )

    listings = published_listings(fetcher, "Acme", "https://boards.greenhouse.io/acme")

    assert listings is not None
    listing = listings[0]
    expected = body.strip()
    assert listing.published_description == f"Data Platform Lead\n\nRemote\n\n{expected}"
    assert len(listing.published_description) > 4000
    assert listing.summary == expected[: len(listing.summary)]
    assert listing.summary != expected


def test_lever_listings_are_read_from_its_top_level_array() -> None:
    """Lever answers with a bare array, and names its fields differently."""
    fetcher = ScriptedApi(
        [
            {
                "id": "b1c2d3",
                "text": "Staff Engineer",
                "hostedUrl": "https://jobs.lever.co/acme/b1c2d3",
                "categories": {"location": "Remote", "commitment": "Full-time"},
                "workplaceType": "remote",
                "descriptionPlain": "Build the platform.",
                "lists": [{"text": "Requirements", "content": "<li>Operate it</li>"}],
                "additionalPlain": "Equal opportunity employer.",
            },
            {"id": "nope", "text": "No link", "categories": {"location": "Remote"}},
        ]
    )

    read = fetcher.listings("Acme", "https://jobs.lever.co/acme")

    assert fetcher.requested == ["https://api.lever.co/v0/postings/acme?mode=json"]
    assert read is not None
    assert read.raw_count == 2
    assert read.skip_reasons == {"missing_required_posting_identity": 1}
    listings = read.listings
    assert [item.title for item in listings] == ["Staff Engineer"]
    assert listings[0].url == "https://jobs.lever.co/acme/b1c2d3"
    assert listings[0].location == "Remote"
    assert listings[0].requisition_id == "b1c2d3"
    assert listings[0].facts.locations.values == ("Remote",)
    assert listings[0].facts.locations.complete is True
    assert listings[0].facts.arrangements.values == ("remote",)
    assert listings[0].facts.employment_types.values == ("Full-time",)
    assert listings[0].published_description == (
        "Staff Engineer\n\nRemote\n\nBuild the platform.\n\n"
        "Requirements\n\n- Operate it\n\nEqual opportunity employer."
    )


@pytest.mark.parametrize(
    "board", ["https://jobs.eu.lever.co/acme", "https://jobs.eu.lever.co/acme/?team=data"]
)
def test_an_eu_lever_board_is_read_from_lever_eu_api(board: str) -> None:
    fetcher = ScriptedApi([])

    fetcher.listings("Acme", board)

    assert fetcher.requested == ["https://api.eu.lever.co/v0/postings/acme?mode=json"]


def test_lever_listing_keeps_remote_location_when_workplace_type_conflicts() -> None:
    fetcher = ScriptedApi(
        [
            {
                "id": "b1c2d3",
                "text": "Data Architect",
                "hostedUrl": "https://jobs.lever.co/acme/b1c2d3",
                "categories": {"location": "Remote (Canada)"},
                "workplaceType": "hybrid",
            }
        ]
    )

    read = fetcher.listings("Acme", "https://jobs.lever.co/acme")

    assert read is not None
    listing = read.listings[0]
    assert listing.facts.locations.values == ("Remote (Canada)",)
    assert listing.facts.arrangements.values == ()
    assert listing.facts.arrangements.unresolved_values[0] == "hybrid"
    assert listing.facts.arrangements.complete is False


def test_lever_arrangement_defers_onsite_against_a_remotely_worded_location() -> None:
    """A location that says 'remotely' contradicts an on-site type the same as 'remote'."""
    arrangements, contradicted = lever_arrangement_values("onsite", "Works remotely")

    assert arrangements == []
    assert contradicted[0] == "onsite"


def test_lever_listing_defers_onsite_workplace_type_against_a_remote_location() -> None:
    fetcher = ScriptedApi(
        [
            {
                "id": "c2d3e4",
                "text": "Data Architect",
                "hostedUrl": "https://jobs.lever.co/acme/c2d3e4",
                "categories": {"location": "Remote - US"},
                "workplaceType": "onsite",
            }
        ]
    )

    read = fetcher.listings("Acme", "https://jobs.lever.co/acme")

    assert read is not None
    listing = read.listings[0]
    assert listing.facts.locations.values == ("Remote - US",)
    assert listing.facts.arrangements.values == ()
    assert listing.facts.arrangements.unresolved_values[0] == "onsite"
    assert listing.facts.arrangements.complete is False


def test_lever_listing_keeps_hybrid_when_the_place_name_only_contains_remote() -> None:
    fetcher = ScriptedApi(
        [
            {
                "id": "d3e4f5",
                "text": "Data Architect",
                "hostedUrl": "https://jobs.lever.co/acme/d3e4f5",
                "categories": {"location": "Remoteville, Ontario"},
                "workplaceType": "hybrid",
            }
        ]
    )

    read = fetcher.listings("Acme", "https://jobs.lever.co/acme")

    assert read is not None
    listing = read.listings[0]
    assert listing.facts.arrangements.values == ("hybrid",)
    assert listing.facts.arrangements.unresolved_values == ()
    assert listing.facts.arrangements.complete is True


def test_lever_preserves_the_complete_all_locations_set() -> None:
    """Lever's `allLocations` names every market; the primary summary alone hides them."""
    fetcher = ScriptedApi(
        [
            {
                "id": "loc1",
                "text": "Product Manager",
                "hostedUrl": "https://jobs.lever.co/acme/loc1",
                "categories": {
                    "location": "2 Locations",
                    "allLocations": ["Remote, USA", "Remote, Canada"],
                    "commitment": "Full-time",
                },
            }
        ]
    )

    listings = published_listings(fetcher, "Acme", "https://jobs.lever.co/acme")

    assert listings is not None
    assert listings[0].facts.locations.values == ("Remote, USA", "Remote, Canada")
    assert listings[0].facts.locations.complete is True
    assert listings[0].location == "Remote, USA"


def test_lever_names_the_country_a_bare_remote_posting_leaves_out() -> None:
    """Lever states the country as an ISO-2 code beside a location naming none."""
    fetcher = ScriptedApi(
        [
            {
                "id": "r1",
                "text": "Data Platform Lead",
                "company": "Acme",
                "hostedUrl": "https://jobs.lever.co/acme/r1",
                "categories": {"location": "Remote", "commitment": "Full-time"},
                "country": "CA",
            }
        ]
    )
    parameters = JobSearchParameters(locations=[Location(key="home", country="Canada")])

    listings = published_listings(fetcher, "Acme", "https://jobs.lever.co/acme")

    assert listings is not None
    assert listings[0].location == "Remote"
    assert listings[0].facts.locations.values == ("Remote", "Canada")
    screening = screen_discovery(
        "Acme", "Data Platform Lead", listings[0].location, parameters, [], listings[0].facts
    )
    location = next(
        finding
        for finding in screening.findings
        if finding.dimension is ScreeningDimension.LOCATION
    )
    assert location.code is ScreeningCode.LOCATION_MATCH


def test_lever_country_does_not_blur_a_posting_open_in_two_markets() -> None:
    """A second market stays visible, and the person's own country still matches."""
    fetcher = ScriptedApi(
        [
            {
                "id": "r2",
                "text": "Data Platform Lead",
                "company": "Acme",
                "hostedUrl": "https://jobs.lever.co/acme/r2",
                "categories": {
                    "location": "2 Locations",
                    "allLocations": ["Toronto, Canada", "Berlin, Germany"],
                },
                "country": "CA",
            }
        ]
    )
    parameters = JobSearchParameters(locations=[Location(key="home", country="Canada")])

    listings = published_listings(fetcher, "Acme", "https://jobs.lever.co/acme")

    assert listings is not None
    assert listings[0].facts.locations.values == ("Toronto, Canada", "Berlin, Germany", "Canada")
    screening = screen_discovery(
        "Acme", "Data Platform Lead", listings[0].location, parameters, [], listings[0].facts
    )
    location = next(
        finding
        for finding in screening.findings
        if finding.dimension is ScreeningDimension.LOCATION
    )
    assert location.code is ScreeningCode.LOCATION_MATCH


def test_greenhouse_preserves_the_full_offices_list() -> None:
    """Greenhouse names every office in `offices`; the summary can collapse them."""
    fetcher = ScriptedApi(
        {
            "jobs": [
                {
                    "id": 42,
                    "title": "Staff Engineer",
                    "absolute_url": "https://boards.greenhouse.io/acme/jobs/42",
                    "location": {"name": "2 Locations"},
                    "offices": [{"name": "Toronto, ON"}, {"name": "Vancouver, BC"}],
                }
            ]
        }
    )

    listings = published_listings(fetcher, "Acme", "https://boards.greenhouse.io/acme")

    assert listings is not None
    assert listings[0].facts.locations.values == ("Toronto, ON", "Vancouver, BC")
    assert listings[0].facts.locations.complete is True
    assert listings[0].location == "Toronto, ON"


def test_a_greenhouse_office_label_never_stands_in_for_the_stated_places() -> None:
    """An office named after the employer is a label, so the job's own summary names the places."""
    fetcher = ScriptedApi(
        {
            "jobs": [
                {
                    "id": 77,
                    "title": "Staff Engineer",
                    "company_name": "Globexhealth Medical",
                    "absolute_url": "https://boards.greenhouse.io/globexhealth/jobs/77",
                    "location": {
                        "name": "Harborview, Freedonia; Vantage, Ruritania; Northreach, Freedonia"
                    },
                    "offices": [{"name": "Medical"}],
                }
            ]
        }
    )

    listings = published_listings(
        fetcher, "Globexhealth", "https://boards.greenhouse.io/globexhealth"
    )

    assert listings is not None
    assert listings[0].facts.locations.values == (
        "Harborview, Freedonia",
        "Vantage, Ruritania",
        "Northreach, Freedonia",
    )
    assert listings[0].location == (
        "Harborview, Freedonia; Vantage, Ruritania; Northreach, Freedonia"
    )


def test_a_workday_count_summary_names_one_place_and_keeps_every_stated_location() -> None:
    """The display location is one place; the locations fact carries the whole published set."""
    record = {
        "jobPostingInfo": {
            "title": "Data Management Lead",
            "location": "3 Locations",
            "secondaryLocations": [
                "Harborview, Freedonia",
                {"location": "Vantage, Ruritania"},
                {"name": "Northreach, Freedonia"},
            ],
            "jobDescription": "You will steward reference data across product teams.",
        }
    }

    checked = posting_check(
        WORKDAY_DETAIL_MAPPING, Posting(record, "Globexhealth"), note="manual check"
    )

    assert checked.location == "Harborview, Freedonia"
    assert checked.facts.locations.values == (
        "Harborview, Freedonia",
        "Vantage, Ruritania",
        "Northreach, Freedonia",
    )
    assert checked.facts.locations.unresolved_values == ("3 Locations",)


def _greenhouse_api_job(**overrides: Any) -> dict[str, Any]:
    """One Greenhouse job payload with the fields the adapter reads."""
    job: dict[str, Any] = {
        "id": 1,
        "title": "Staff Engineer",
        "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
        "location": {"name": "Seattle, WA"},
    }
    job.update(overrides)
    return job


@pytest.mark.parametrize(
    "location",
    ["Remote or New York, NY", "New York, NY; Remote", "New York, NY or Remote - US"],
)
def test_greenhouse_hybrid_metadata_with_a_remote_location_alternative_stays_unresolved(
    location: str,
) -> None:
    """A remote alternative in the location itself must contradict a structured tag too."""
    fetcher = ScriptedApi(
        {
            "jobs": [
                _greenhouse_api_job(
                    location={"name": location},
                    metadata=[{"name": "Work Setting", "value": "Hybrid"}],
                )
            ]
        }
    )

    listings = published_listings(fetcher, "Acme", "https://boards.greenhouse.io/acme")

    assert listings is not None
    assert listings[0].facts.arrangements.values == ()
    assert listings[0].facts.arrangements.unresolved_values


def test_greenhouse_arrangement_from_work_setting_metadata() -> None:
    """The Work Setting metadata entry is the strongest publisher-stated signal."""
    fetcher = ScriptedApi(
        {
            "jobs": [
                _greenhouse_api_job(
                    metadata=[{"name": "Work Setting", "value": "Remote"}],
                )
            ]
        }
    )

    listings = published_listings(fetcher, "Acme", "https://boards.greenhouse.io/acme")

    assert listings is not None
    assert listings[0].facts.arrangements.values == ("Remote",)
    assert listings[0].facts.arrangements.complete is True


@pytest.mark.parametrize(
    ("tag", "arrangement"),
    [
        ("#LI-Remote", "remote"),
        ("#LIRemote", "remote"),
        ("#LI-Hybrid", "hybrid"),
        ("#LIHybrid", "hybrid"),
        ("#LI-Onsite", "onsite"),
        ("#LIOnsite", "onsite"),
    ],
)
def test_greenhouse_arrangement_from_linkedin_body_tag(tag: str, arrangement: str) -> None:
    """LinkedIn arrangement labels may be published with or without a separator."""
    body = f"<p>We are hiring.</p><p>{tag}</p>"
    fetcher = ScriptedApi({"jobs": [_greenhouse_api_job(content=body)]})

    listings = published_listings(fetcher, "Beta", "https://boards.greenhouse.io/beta")

    assert listings is not None
    assert listings[0].facts.arrangements.values == (arrangement,)
    assert listings[0].facts.arrangements.complete is True


@pytest.mark.parametrize(
    ("location", "tag"),
    [
        ("New York, NY; Remote - US", "#LI-Hybrid"),
        ("Toronto, ON; Remote, Canada", "#LI-Hybrid"),
    ],
)
def test_greenhouse_content_tag_is_contradiction_checked_against_a_remote_location(
    location: str, tag: str
) -> None:
    """A #LI content tag must be checked the same as a structured Work Setting value."""
    fetcher = ScriptedApi(
        {"jobs": [_greenhouse_api_job(location={"name": location}, content=f"<p>{tag}</p>")]}
    )

    listings = published_listings(fetcher, "Beta", "https://boards.greenhouse.io/beta")

    assert listings is not None
    assert listings[0].facts.arrangements.values == ()
    assert listings[0].facts.arrangements.unresolved_values


@pytest.mark.parametrize(
    "tag", ["#LI-Remote-first", "#LI-Remotely", "#LI-Hybridized", "#LI-Onsite-only"]
)
def test_greenhouse_arrangement_ignores_linkedin_tag_lookalikes(tag: str) -> None:
    fetcher = ScriptedApi({"jobs": [_greenhouse_api_job(content=f"<p>We are hiring. {tag}</p>")]})

    listings = published_listings(fetcher, "Beta", "https://boards.greenhouse.io/beta")

    assert listings is not None
    assert listings[0].facts.arrangements.values == ()


@pytest.mark.parametrize(
    "body",
    [
        "<p>#LI-Onsite</p><p>#LI-Remote</p>",
        "<p>#LI-Onsite</p><p>This role is fully remote within Canada.</p>",
    ],
)
def test_greenhouse_arrangement_keeps_conflicting_linkedin_source_unresolved(body: str) -> None:
    fetcher = ScriptedApi({"jobs": [_greenhouse_api_job(content=body)]})

    listings = published_listings(fetcher, "Beta", "https://boards.greenhouse.io/beta")

    assert listings is not None
    arrangement = listings[0].facts.arrangements
    assert arrangement.values == ()
    assert arrangement.complete is False
    assert arrangement.unresolved_values
    assert "#LI-Onsite" in arrangement.unresolved_values[0]


def test_a_greenhouse_tag_conflict_in_escaped_content_quotes_the_role_sentence_without_markup() -> (
    None
):
    body = (
        "&lt;p&gt;#LI-REMOTE&lt;/p&gt;&lt;p&gt;&lt;span&gt;This role will be a hybrid role, "
        "with three days per week in our Northtown office.&lt;/span&gt;&lt;/p&gt;"
        "&lt;p&gt;Pay is 5 &amp;lt; 6 bands.&lt;/p&gt;"
    )
    fetcher = ScriptedApi({"jobs": [_greenhouse_api_job(content=body)]})

    listings = published_listings(fetcher, "Beta", "https://boards.greenhouse.io/beta")

    assert listings is not None
    assert listings[0].facts.arrangements.unresolved_values == (
        "LinkedIn tag #LI-REMOTE conflicts with role statement: This role will be a hybrid "
        "role, with three days per week in our Northtown office.",
    )


@pytest.mark.parametrize(
    ("tag", "arrangement"),
    [("#LI-Hybrid", "hybrid"), ("#LI-Onsite", "onsite")],
)
def test_greenhouse_arrangement_li_hybrid_and_li_onsite_variants(
    tag: str, arrangement: str
) -> None:
    fetcher = ScriptedApi({"jobs": [_greenhouse_api_job(content=f"<div>Team info. {tag}</div>")]})

    listings = published_listings(fetcher, "Globex", "https://boards.greenhouse.io/globex")

    assert listings is not None
    assert listings[0].facts.arrangements.values == (arrangement,)


def test_greenhouse_arrangement_metadata_wins_over_body_tag() -> None:
    """When the two disagree, take the closed-vocabulary field over prose markup."""
    fetcher = ScriptedApi(
        {
            "jobs": [
                _greenhouse_api_job(
                    metadata=[{"name": "Work Setting", "value": "Remote"}],
                    content="<p>#LI-Hybrid</p>",
                )
            ]
        }
    )

    listings = published_listings(fetcher, "Acme", "https://boards.greenhouse.io/acme")

    assert listings is not None
    assert listings[0].facts.arrangements.values == ("Remote",)


def test_greenhouse_arrangement_absent_when_no_signal() -> None:
    """No metadata and no LI tag stays unknown; nothing is inferred from location text."""
    fetcher = ScriptedApi(
        {"jobs": [_greenhouse_api_job(content="<p>Join our team in Seattle.</p>")]}
    )

    listings = published_listings(fetcher, "Initrode", "https://boards.greenhouse.io/initrode")

    assert listings is not None
    assert listings[0].facts.arrangements.values == ()
    assert listings[0].facts.arrangements.complete is False


def test_greenhouse_body_markup_becomes_a_bounded_plain_summary() -> None:
    """Screening reads responsibilities from the body the board already publishes."""
    fetcher = ScriptedApi(
        {
            "jobs": [
                {
                    "id": 11,
                    "title": "Senior Developer",
                    "absolute_url": "https://boards.greenhouse.io/acme/jobs/11",
                    "location": {"name": "Harborview"},
                    "content": "&lt;p&gt;Build &lt;b&gt;developer tooling&lt;/b&gt;.&lt;/p&gt;"
                    + "&lt;p&gt;More.&lt;/p&gt;" * 2000,
                },
                {
                    "id": 12,
                    "title": "Quiet Role",
                    "absolute_url": "https://boards.greenhouse.io/acme/jobs/12",
                    "location": {"name": "Harborview"},
                },
            ]
        }
    )

    listings = published_listings(fetcher, "Acme", "https://boards.greenhouse.io/acme")

    assert listings is not None
    assert listings[0].summary.startswith("Build developer tooling . More.")
    assert "<" not in listings[0].summary
    assert len(listings[0].summary) == 4000
    assert listings[1].summary == ""


def test_a_careers_page_that_names_no_board_is_read_from_what_it_declares() -> None:
    fetcher = ScriptedPage(
        declaring_page(
            {
                "@type": "JobPosting",
                "title": "Platform Engineer",
                "identifier": "REQ-1",
                "url": "https://acme.example/careers/1",
                "jobLocation": {"address": {"addressLocality": "Leeds"}},
            },
            {"@type": "JobPosting", "title": "Data Engineer"},
        )
    )

    listings = published_listings(fetcher, "Acme", "https://acme.example/careers")

    # Preserve unsupported platforms as manual reads.
    assert listings is not None
    assert [item.title for item in listings] == ["Platform Engineer", "Data Engineer"]
    assert listings[0].url == "https://acme.example/careers/1"
    assert listings[0].location == "Leeds"
    assert listings[0].requisition_id == "REQ-1"
    # The page URL remains usable when no posting URL is declared.
    assert listings[1].url == "https://acme.example/careers"


def test_publisher_declared_posting_is_not_rejected_by_its_url_shape() -> None:
    url = "https://jobs.example/en/category/clinical-director/733/123/1"
    fetcher = ScriptedPage(
        declaring_page(
            {
                "@type": "JobPosting",
                "title": "Clinical Director",
                "url": url,
            }
        )
    )

    listings = published_listings(fetcher, "Example Health", "https://jobs.example/en/careers")

    assert listings is not None
    assert [(listing.title, listing.url) for listing in listings] == [("Clinical Director", url)]


def test_a_page_with_neither_a_board_nor_markup_stays_visible_for_recovery() -> None:
    fetcher = ScriptedPage("<html><body><p>Email us your CV.</p></body></html>")

    # An explicit incomplete read is not a claim that the employer has no openings.
    assert published_listings(fetcher, "Acme", "https://acme.example/careers") == []


def test_a_declared_posting_never_overrides_the_board_the_page_names() -> None:
    class RecordingDispatch(ScriptedPage):
        def __init__(self, page: str) -> None:
            super().__init__(page)
            self.dispatched: list[str] = []

        def _dispatch(self, source_name: str, platform: str, match: Any, url: str) -> SourceRead:
            self.dispatched.append(platform)
            return SourceRead(
                listings=[],
                complete=True,
                raw_count=0,
                outcome=SourceReadOutcome.ATS_COMPLETE,
            )

    fetcher = RecordingDispatch(
        'src="https://boards.greenhouse.io/embed/job_board/js?for=acme"'
        + declaring_page({"@type": "JobPosting", "title": "Should not win"})
    )

    listings = published_listings(fetcher, "Acme", "https://acme.example/careers")

    assert fetcher.dispatched == ["greenhouse"]
    assert listings == []


def test_the_careers_page_is_read_once_however_it_ends_up_being_used() -> None:
    fetcher = ScriptedPage(declaring_page({"@type": "JobPosting", "title": "Platform Engineer"}))

    published_listings(fetcher, "Acme", "https://acme.example/careers")

    assert fetcher.fetched == ["https://acme.example/careers"]


def test_a_board_search_carries_only_its_explicit_protocol_headers() -> None:
    import httpx

    from clearscout.infrastructure.web_fetch.board_adapters import BoardApiFetcher
    from clearscout.infrastructure.web_fetch.policy import PoliteHttpClient
    from tests.support.http import public_resolver

    received: list[dict[str, str]] = []

    def get(url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(200, request=httpx.Request("GET", url), text="")

    def post(url: str, **kwargs: Any) -> httpx.Response:
        received.append(dict(kwargs["headers"]))
        return httpx.Response(200, request=httpx.Request("POST", url), json={"jobs": []})

    client = PoliteHttpClient(
        user_agent="clearscout/test",
        getter=get,
        poster=post,
        resolver=public_resolver,
        sleeper=lambda _seconds: None,
        jitter=lambda _low, _high: 0.0,
    )
    fetcher = BoardApiFetcher(client=client)
    fetcher.post_json_with_headers(
        "https://acme.example/search", {}, headers={"tz": "GMT+00:00", "tzname": "UTC"}
    )
    fetcher.post_json("https://acme.example/search", {})

    assert received[0]["tz"] == "GMT+00:00"
    assert received[0]["tzname"] == "UTC"
    assert "tz" not in received[1]
    assert "authorization" not in {name.lower() for name in received[1]}


_FORM_STATES = ("Alaska", "Florida", "Guam", "NY", "District of Columbia", "Wyoming")


@pytest.mark.parametrize("platform", FORM_PLATFORMS)
@pytest.mark.parametrize(
    "label",
    [
        "Which state do you currently reside in?",
        "What state do you live in?",
        "State of residence",
        "In which U.S. state are you located?",
        "Where are you based? (state)",
        "Please select the state you are living in",
    ],
)
def test_a_required_state_of_residence_question_is_read_from_the_form(
    platform: FormPlatform, label: str
) -> None:
    assert required_residence_question(form_source(platform, label, _FORM_STATES)) == label


@pytest.mark.parametrize("platform", FORM_PLATFORMS)
@pytest.mark.parametrize(
    ("label", "options", "required"),
    [
        ("Which state do you reside in?", (*_FORM_STATES, "Outside the United States"), True),
        ("Which state do you reside in?", (*_FORM_STATES, "Other"), True),
        ("Which state do you reside in?", _FORM_STATES, False),
        ("Which state is this role based in?", ("Yes", "No"), True),
        ("Which state will you work from?", _FORM_STATES, True),
        ("Which state do you reside in?", (), True),
    ],
)
def test_a_state_question_someone_outside_the_us_can_answer_is_not_read(
    platform: FormPlatform, label: str, options: tuple[str, ...], required: bool
) -> None:
    source = form_source(platform, label, options, required=required)

    assert required_residence_question(source) is None


@pytest.mark.parametrize("form", ["", "not json", "[1, 2]", '{"sections": "none"}'])
@pytest.mark.parametrize("platform", ["lever", "ashby"])
def test_a_missing_or_unreadable_form_states_no_residence_question(
    platform: str, form: str
) -> None:
    source = PostingSource(publisher=platform, record="{}", application_form=form)

    assert required_residence_question(source) is None


def test_lever_cards_are_read_from_the_apply_page_inputs() -> None:
    cards = lever_cards("Which state do you live in?", _FORM_STATES)

    assert lever_form_cards(lever_apply_page(cards)) == cards


@pytest.mark.parametrize(
    ("declaration", "record", "basis", "day", "grade"),
    [
        pytest.param(
            GREENHOUSE_MAPPING,
            _greenhouse_job(
                first_published="2026-09-01T10:04:50-04:00",
                updated_at="2026-10-14T08:00:00-04:00",
            ),
            "first_published",
            date(2026, 9, 1),
            PostedDateGrade.FIRST_POSTED,
            id="greenhouse",
        ),
        pytest.param(
            LEVER_MAPPING,
            _lever_job(createdAt=1789000000000),
            "createdAt",
            date(2026, 9, 10),
            PostedDateGrade.FIRST_POSTED,
            id="lever-epoch-milliseconds",
        ),
        pytest.param(
            ASHBY_MAPPING,
            {
                "id": "as-1",
                "title": "Staff Engineer",
                "jobUrl": "https://example.test/as-1",
                "publishedAt": "2026-07-29T19:31:08.392+00:00",
            },
            "publishedAt",
            date(2026, 7, 29),
            PostedDateGrade.FIRST_POSTED,
            id="ashby",
        ),
    ],
)
def test_a_board_listing_states_the_posted_date_its_record_publishes(
    declaration: BoardDeclaration,
    record: dict[str, Any],
    basis: str,
    day: date,
    grade: PostedDateGrade,
) -> None:
    listing = source_listing(declaration, Posting(record, employer="Globex"))

    assert listing is not None
    posted = listing.facts.posted_date
    assert posted is not None
    assert (posted.earliest.basis, posted.day, posted.earliest.grade) == (basis, day, grade)


def test_a_workday_detail_start_date_is_only_a_board_date() -> None:
    record = {
        "jobPostingInfo": {
            "title": "Data Management Lead",
            "location": "Harborview, Freedonia",
            "startDate": "2026-10-08",
            "jobDescription": "You will steward reference data across product teams.",
        }
    }

    checked = posting_check(
        WORKDAY_DETAIL_MAPPING, Posting(record, "Globexhealth"), note="manual check"
    )

    posted = checked.facts.posted_date
    assert posted is not None
    assert posted.earliest.basis == "jobPostingInfo.startDate"
    assert posted.day == date(2026, 10, 8)
    assert posted.earliest.grade is PostedDateGrade.BOARD_DATE
