"""Pending discovery screening stays deterministic before paid evaluation."""

from decimal import Decimal
from typing import Any, Literal

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from clearscout.application.discovery.board_slugs import (
    backfilled_listing,
    publisher_spelling_of_host,
    publisher_spelling_of_tenant,
)
from clearscout.application.discovery.scan_queries import refusal_text
from clearscout.application.discovery.screening import (
    BulkStagingRefusal,
    ConstraintConfidence,
    DiscoveryScreening,
    ScreeningCode,
    ScreeningDimension,
    ScreeningFinding,
    ScreeningStanding,
    arrangement_categories,
    body_states_saved_focus,
    bulk_staging_refusal,
    constraint_confidence,
    resolve_arrangement_categories,
    screen_discovery,
    select_title_survivors,
    structured_arrangement_holdout,
    structured_holdout,
    structured_location_holdout,
    supports_bulk_staging,
    supports_title_expansion,
)
from clearscout.application.discovery.source_reading import SourceListing
from clearscout.domain.search_parameters import (
    Acceptance,
    Arrangement,
    Cluster,
    CompensationBand,
    CountryNames,
    EmployerArchetype,
    Employers,
    Employment,
    Focus,
    JobSearchParameters,
    Location,
    Seniority,
    WorkEligibility,
)
from clearscout.domain.staged_facts import (
    ListingIdentityTrust,
    PublishedFact,
    StagedListingFacts,
)
from clearscout.infrastructure.web_fetch.listing_facts import published_listing_facts
from clearscout.infrastructure.web_fetch.workday_arrangement import workday_arrangement_facts
from tests.support.scenarios import (
    DATA_PRODUCT_TARGET_TITLES,
    data_product_search,
    home_market_search,
    published_location_facts,
    titles,
)


def _finding(
    dimension: ScreeningDimension,
    standing: ScreeningStanding,
    code: ScreeningCode,
) -> ScreeningFinding:
    return ScreeningFinding(dimension, standing, code)


def test_constraint_confidence_uses_only_typed_deterministic_findings() -> None:
    match = _finding(
        ScreeningDimension.LOCATION, ScreeningStanding.MATCH, ScreeningCode.LOCATION_MATCH
    )
    assert constraint_confidence(DiscoveryScreening((match,))) is ConstraintConfidence.PURSUE

    other_unknown = _finding(
        ScreeningDimension.ARRANGEMENT,
        ScreeningStanding.UNKNOWN,
        ScreeningCode.ARRANGEMENT_UNKNOWN,
    )
    assert (
        constraint_confidence(DiscoveryScreening((match, other_unknown)))
        is ConstraintConfidence.INVESTIGATE
    )

    compensation = _finding(
        ScreeningDimension.COMPENSATION,
        ScreeningStanding.UNKNOWN,
        ScreeningCode.COMPENSATION_UNKNOWN,
    )
    assert (
        constraint_confidence(DiscoveryScreening((match, compensation)))
        is ConstraintConfidence.UNCERTAIN_COMP
    )

    location = _finding(
        ScreeningDimension.LOCATION,
        ScreeningStanding.UNKNOWN,
        ScreeningCode.LOCATION_UNRESOLVED,
    )
    assert (
        constraint_confidence(DiscoveryScreening((location, compensation)))
        is ConstraintConfidence.NEEDS_VERIFICATION
    )

    refused = _finding(
        ScreeningDimension.LOCATION,
        ScreeningStanding.REFUSE,
        ScreeningCode.LOCATION_OUTSIDE,
    )
    assert (
        constraint_confidence(DiscoveryScreening((refused, location)))
        is ConstraintConfidence.EXCLUDED
    )


def test_an_explicit_country_outside_the_saved_places_is_a_typed_refusal() -> None:
    screening = screen_discovery(
        "Example Cooperative",
        "Platform Product Manager",
        "Southport, Ruritania",
        home_market_search("Platform Product Manager"),
        ["Platform Product Manager"],
        published_location_facts("Southport, Ruritania"),
    )

    assert screening.standing is ScreeningStanding.REFUSE
    assert screening.findings[1].standing is ScreeningStanding.REFUSE
    assert screening.findings[1].code is ScreeningCode.LOCATION_OUTSIDE
    assert screening.findings[1].subject == "Ruritania"


def test_iso_alpha3_prefix_resolves_to_a_foreign_country_refusal() -> None:
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="Canada")],
        countries=[CountryNames(country="Canada"), CountryNames(country="United States")],
        seniority=Seniority(target_titles=titles("Platform Product Manager")),
    )
    for location in (
        "POL-Remote",
        "SRB-Belgrade-Hybrid",
        "GBR-Remote",
        "BGR-Remote",
        "CHN-Client",
        "ARG-Buenos Aires-Hybrid",
        "Seoul, Republic of Korea",
        "Bucuresti, Romania",
        "Bangalore, India",
    ):
        facts = StagedListingFacts(
            locations=PublishedFact(values=(location,), provenance="board", complete=True)
        )
        screening = screen_discovery(
            "Example Cooperative",
            "Platform Product Manager",
            location,
            parameters,
            ["Platform Product Manager"],
            facts,
        )
        location_finding = screening.findings[1]
        assert location_finding.code is ScreeningCode.LOCATION_OUTSIDE, location
        assert location_finding.standing is ScreeningStanding.REFUSE, location


def test_a_country_in_its_older_iso_spelling_is_refused_for_a_north_american_search() -> None:
    location = "Sarajevo, Federation of Bosnia and Herzegovina, Bosnia and Herzegowina"
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="Canada")],
        countries=[CountryNames(country="Canada"), CountryNames(country="United States")],
        seniority=Seniority(target_titles=titles("Platform Product Manager")),
    )

    screening = screen_discovery(
        "Example Cooperative",
        "Platform Product Manager",
        location,
        parameters,
        ["Platform Product Manager"],
        published_location_facts(location),
    )

    assert screening.findings[1].code is ScreeningCode.LOCATION_OUTSIDE
    assert screening.standing is ScreeningStanding.REFUSE


def test_a_saved_country_spelled_in_a_foreign_street_address_still_refuses() -> None:
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="Canada")],
        countries=[CountryNames(country="Canada"), CountryNames(country="United States")],
        seniority=Seniority(target_titles=titles("Platform Product Manager")),
    )
    location = "GBR - London, Canada Square"
    screening = screen_discovery(
        "Example Cooperative",
        "Platform Product Manager",
        location,
        parameters,
        ["Platform Product Manager"],
        StagedListingFacts(
            locations=PublishedFact(values=(location,), provenance="board", complete=True)
        ),
    )

    assert screening.findings[1].standing is ScreeningStanding.REFUSE
    assert screening.findings[1].code is ScreeningCode.LOCATION_OUTSIDE
    assert screening.findings[1].subject == "United Kingdom"


@pytest.mark.parametrize(
    ("title", "standing", "code", "subject"),
    [
        (
            "Regional Named Account Executive - Northeast",
            ScreeningStanding.UNKNOWN,
            ScreeningCode.SENIORITY_UNKNOWN,
            "",
        ),
        ("Executive Assistant", ScreeningStanding.UNKNOWN, ScreeningCode.SENIORITY_UNKNOWN, ""),
        (
            "Executive Director, Data Platform",
            ScreeningStanding.UNKNOWN,
            ScreeningCode.SENIORITY_OUTSIDE,
            "executive",
        ),
        (
            "Senior Director, Data Platform",
            ScreeningStanding.MATCH,
            ScreeningCode.SENIORITY_MATCH,
            "senior, director",
        ),
        (
            "Director, Executive Programs",
            ScreeningStanding.MATCH,
            ScreeningCode.SENIORITY_MATCH,
            "director",
        ),
        (
            "Executive Vice President, Data",
            ScreeningStanding.UNKNOWN,
            ScreeningCode.SENIORITY_OUTSIDE,
            "executive",
        ),
    ],
)
def test_executive_is_a_rung_only_ahead_of_a_leadership_noun(
    title: str, standing: ScreeningStanding, code: ScreeningCode, subject: str
) -> None:
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="Canada")],
        seniority=Seniority(
            minimum="senior", maximum="director", target_titles=titles("Platform Product Manager")
        ),
    )
    screening = screen_discovery(
        "Example Cooperative",
        title,
        "Canada",
        parameters,
        ["Platform Product Manager"],
        published_location_facts("Canada"),
    )
    finding = next(f for f in screening.findings if f.dimension is ScreeningDimension.SENIORITY)

    assert finding.standing is standing
    assert finding.code is code
    assert finding.subject == subject


@pytest.mark.parametrize(
    ("title", "standing", "code", "subject"),
    [
        (
            "Associate Director, Data Governance",
            ScreeningStanding.MATCH,
            ScreeningCode.SENIORITY_MATCH,
            "director",
        ),
        (
            # An associate vice president states no rung, so the band cannot
            # refuse it on a level it never claimed.
            "Associate Vice President, Platform Data",
            ScreeningStanding.UNKNOWN,
            ScreeningCode.SENIORITY_UNKNOWN,
            "",
        ),
        (
            "Warehouse Associate",
            ScreeningStanding.UNKNOWN,
            ScreeningCode.SENIORITY_OUTSIDE,
            "associate",
        ),
        (
            "Associate Product Manager",
            ScreeningStanding.UNKNOWN,
            ScreeningCode.SENIORITY_OUTSIDE,
            "associate",
        ),
    ],
)
def test_associate_ahead_of_a_leadership_noun_is_a_modifier_not_a_rung(
    title: str, standing: ScreeningStanding, code: ScreeningCode, subject: str
) -> None:
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="Canada")],
        seniority=Seniority(
            minimum="senior", maximum="director", target_titles=titles("Platform Product Manager")
        ),
    )
    screening = screen_discovery(
        "Example Cooperative",
        title,
        "Canada",
        parameters,
        ["Platform Product Manager"],
        published_location_facts("Canada"),
    )
    finding = next(f for f in screening.findings if f.dimension is ScreeningDimension.SENIORITY)

    assert finding.standing is standing
    assert finding.code is code
    assert finding.subject == subject


@pytest.mark.parametrize(
    ("title", "standing", "code", "subject"),
    [
        (
            "Vice President, Clinical Development",
            ScreeningStanding.UNKNOWN,
            ScreeningCode.SENIORITY_OUTSIDE,
            "executive",
        ),
        (
            "VP, Data Products",
            ScreeningStanding.UNKNOWN,
            ScreeningCode.SENIORITY_OUTSIDE,
            "executive",
        ),
        (
            "SVP, Product Management - Data Products & Strategy",
            ScreeningStanding.UNKNOWN,
            ScreeningCode.SENIORITY_OUTSIDE,
            "executive",
        ),
        (
            "EVP Product",
            ScreeningStanding.UNKNOWN,
            ScreeningCode.SENIORITY_OUTSIDE,
            "executive",
        ),
        (
            "Senior Vice President, Clinical Development",
            ScreeningStanding.UNKNOWN,
            ScreeningCode.SENIORITY_OUTSIDE,
            "executive",
        ),
        (
            "President's Club Account Manager",
            ScreeningStanding.UNKNOWN,
            ScreeningCode.SENIORITY_UNKNOWN,
            "",
        ),
    ],
)
def test_vice_president_and_vp_read_as_the_executive_rung(
    title: str, standing: ScreeningStanding, code: ScreeningCode, subject: str
) -> None:
    """The word "president" alone, with no "vice" before it, states no rung."""
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="Canada")],
        seniority=Seniority(
            minimum="senior", maximum="director", target_titles=titles("Platform Product Manager")
        ),
    )
    screening = screen_discovery(
        "Example Cooperative",
        title,
        "Canada",
        parameters,
        ["Platform Product Manager"],
        published_location_facts("Canada"),
    )
    finding = next(f for f in screening.findings if f.dimension is ScreeningDimension.SENIORITY)

    assert finding.standing is standing
    assert finding.code is code
    assert finding.subject == subject


def test_an_explicit_foreign_country_refuses_even_without_complete_staged_facts() -> None:
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="Canada")],
        countries=[CountryNames(country="Canada"), CountryNames(country="United States")],
        seniority=Seniority(target_titles=titles("Platform Product Manager")),
    )
    for location in ("Tel Aviv, Israel", "Bangalore, India", "Munich, Germany"):
        facts = StagedListingFacts(
            locations=PublishedFact(values=(location,), provenance="board", complete=False)
        )
        screening = screen_discovery(
            "Example Cooperative",
            "Platform Product Manager",
            location,
            parameters,
            ["Platform Product Manager"],
            facts,
        )
        location_finding = screening.findings[1]
        assert location_finding.code is ScreeningCode.LOCATION_OUTSIDE, location
        assert location_finding.standing is ScreeningStanding.REFUSE, location


def test_a_coded_foreign_place_refuses_the_way_the_spelled_country_does() -> None:
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="Canada")],
        countries=[CountryNames(country="Canada"), CountryNames(country="United States")],
        seniority=Seniority(target_titles=titles("Platform Product Manager")),
    )
    for location in ("Islamabad, PK", "Sofia, BG", "PH - QUEZON CITY - GBF CENTER"):
        facts = StagedListingFacts(
            locations=PublishedFact(values=(location,), provenance="board", complete=False)
        )
        screening = screen_discovery(
            "Example Cooperative",
            "Platform Product Manager",
            location,
            parameters,
            ["Platform Product Manager"],
            facts,
        )
        location_finding = screening.findings[1]
        assert location_finding.code is ScreeningCode.LOCATION_OUTSIDE, location
        assert location_finding.standing is ScreeningStanding.REFUSE, location


def test_a_subdivision_code_never_refuses_a_place_inside_a_saved_country() -> None:
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="Canada")],
        countries=[CountryNames(country="Canada"), CountryNames(country="United States")],
        seniority=Seniority(target_titles=titles("Platform Product Manager")),
    )
    parameters = parameters.model_copy(
        update={
            "locations": [
                Location(key="home", country="Canada"),
                Location(key="remote_us", country="United States"),
            ]
        }
    )
    for location in (
        "New York, NY",
        "USA - Waltham, MA",
        "Moncton, NB E1C 4X9",
        "Regina, SK",
    ):
        facts = StagedListingFacts(
            locations=PublishedFact(values=(location,), provenance="board", complete=False)
        )
        screening = screen_discovery(
            "Example Cooperative",
            "Platform Product Manager",
            location,
            parameters,
            ["Platform Product Manager"],
            facts,
        )
        location_finding = screening.findings[1]
        assert location_finding.standing is not ScreeningStanding.REFUSE, location
    gurugram = "IN - GURUGRAM DLF DOWNTOWN BLOCK 4 FL 11&12"
    facts = StagedListingFacts(
        locations=PublishedFact(values=(gurugram,), provenance="board", complete=False)
    )
    screening = screen_discovery(
        "Example Cooperative",
        "Platform Product Manager",
        gurugram,
        parameters,
        ["Platform Product Manager"],
        facts,
    )
    assert screening.findings[1].code is ScreeningCode.LOCATION_OUTSIDE
    assert screening.findings[1].subject == "India"


def test_a_spelled_subdivision_matches_the_saved_country_its_code_matches() -> None:
    parameters = JobSearchParameters(
        locations=[
            Location(key="home", country="Canada"),
            Location(key="remote_us", country="United States"),
        ],
        countries=[CountryNames(country="Canada"), CountryNames(country="United States")],
        seniority=Seniority(target_titles=titles("Platform Product Manager")),
    )
    for location in (
        "Boston, Massachusetts",
        "Massachusetts - Boston",
        "Raleigh, North Carolina",
        "Indiana - Indianapolis",
        "Waterloo, Ontario",
    ):
        facts = StagedListingFacts(
            locations=PublishedFact(values=(location,), provenance="board", complete=True)
        )
        screening = screen_discovery(
            "Example Cooperative",
            "Platform Product Manager",
            location,
            parameters,
            ["Platform Product Manager"],
            facts,
        )
        location_finding = screening.findings[1]
        assert location_finding.code is ScreeningCode.LOCATION_MATCH, location


def test_a_country_spelled_inside_a_subdivision_name_does_not_place_the_posting() -> None:
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="Canada")],
        countries=[CountryNames(country="Canada")],
        seniority=Seniority(target_titles=titles("Platform Product Manager")),
    )
    for location in ("Newark, New Jersey", "Santa Fe, New Mexico"):
        facts = StagedListingFacts(
            locations=PublishedFact(values=(location,), provenance="board", complete=False)
        )
        screening = screen_discovery(
            "Example Cooperative",
            "Platform Product Manager",
            location,
            parameters,
            ["Platform Product Manager"],
            facts,
        )
        location_finding = screening.findings[1]
        assert location_finding.code is ScreeningCode.LOCATION_OUTSIDE, location
        assert location_finding.subject == "United States", location


def test_a_posting_open_in_two_markets_names_both_in_its_refusal() -> None:
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="Canada")],
        countries=[CountryNames(country="Canada")],
        seniority=Seniority(target_titles=titles("Platform Product Manager")),
    )
    for location, subject in (
        ("Hiring in Sudan and South Sudan", "South Sudan, Sudan"),
        ("Beijing, China and Hong Kong SAR China", "China, Hong Kong SAR China"),
        ("Conakry, Guinea or Port Moresby, Papua New Guinea", "Guinea, Papua New Guinea"),
    ):
        facts = StagedListingFacts(
            locations=PublishedFact(values=(location,), provenance="board", complete=False)
        )
        screening = screen_discovery(
            "Example Cooperative",
            "Platform Product Manager",
            location,
            parameters,
            ["Platform Product Manager"],
            facts,
        )
        location_finding = screening.findings[1]
        assert location_finding.code is ScreeningCode.LOCATION_OUTSIDE, location
        assert location_finding.subject == subject, location


def test_a_city_beside_a_spelled_us_state_is_outside_a_canada_only_search() -> None:
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="Canada")],
        countries=[CountryNames(country="Canada")],
        seniority=Seniority(target_titles=titles("Platform Product Manager")),
    )
    facts = StagedListingFacts(
        locations=PublishedFact(values=("Atlanta, Georgia",), provenance="board", complete=False)
    )
    screening = screen_discovery(
        "Example Cooperative",
        "Platform Product Manager",
        "Atlanta, Georgia",
        parameters,
        ["Platform Product Manager"],
        facts,
    )
    location_finding = screening.findings[1]
    assert location_finding.code is ScreeningCode.LOCATION_OUTSIDE
    assert location_finding.subject == "United States"


def test_one_foreign_country_does_not_refuse_an_unresolved_multi_location_posting() -> None:
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="Canada")],
        countries=[CountryNames(country="Canada"), CountryNames(country="United States")],
        seniority=Seniority(target_titles=titles("Platform Product Manager")),
    )
    facts = StagedListingFacts(
        locations=PublishedFact(
            values=("United Kingdom",),
            unresolved_values=("3 Locations",),
            provenance="workday.location",
            complete=False,
        )
    )

    screening = screen_discovery(
        "Example Cooperative",
        "Platform Product Manager",
        "3 Locations",
        parameters,
        ["Platform Product Manager"],
        facts,
    )

    assert screening.findings[1].code is ScreeningCode.LOCATION_UNRESOLVED
    assert screening.findings[1].standing is ScreeningStanding.UNKNOWN


def test_a_blank_location_with_incomplete_facts_stays_reviewable() -> None:
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="Canada")],
        countries=[CountryNames(country="Canada")],
        seniority=Seniority(target_titles=titles("Platform Product Manager")),
    )
    screening = screen_discovery(
        "Example Cooperative",
        "Platform Product Manager",
        "",
        parameters,
        ["Platform Product Manager"],
        StagedListingFacts(locations=PublishedFact(complete=False)),
    )
    assert screening.findings[1].code is ScreeningCode.LOCATION_MISSING
    assert screening.findings[1].standing is ScreeningStanding.UNKNOWN


def test_an_unresolved_place_stays_unknown_instead_of_becoming_a_refusal() -> None:
    screening = screen_discovery(
        "Example Cooperative",
        "Platform Product Manager",
        "Southport",
        home_market_search("Platform Product Manager"),
        ["Platform Product Manager"],
        published_location_facts("Southport"),
    )

    assert screening.standing is ScreeningStanding.UNKNOWN
    assert screening.findings[1].code is ScreeningCode.LOCATION_UNRESOLVED


def test_a_location_is_not_a_boundary_when_no_places_are_configured() -> None:
    parameters = JobSearchParameters(
        seniority=Seniority(target_titles=titles("Platform Product Manager"))
    )

    screening = screen_discovery(
        "Example Cooperative",
        "Platform Product Manager",
        "Southport, Ruritania",
        parameters,
        ["Platform Product Manager"],
        published_location_facts("Southport, Ruritania"),
    )

    assert screening.standing is ScreeningStanding.UNKNOWN
    assert screening.findings[1].code is ScreeningCode.LOCATION_UNRESTRICTED


def test_a_matching_place_marked_ineligible_is_a_typed_refusal() -> None:
    parameters = JobSearchParameters(
        locations=[
            Location(
                key="closed",
                country="Freedonia",
                eligibility=WorkEligibility.INELIGIBLE,
            )
        ],
        countries=[CountryNames(country="Freedonia")],
        seniority=Seniority(target_titles=titles("Platform Product Manager")),
    )

    screening = screen_discovery(
        "Example Cooperative",
        "Platform Product Manager",
        "Freedonia",
        parameters,
        ["Platform Product Manager"],
        published_location_facts("Freedonia"),
    )

    assert screening.standing is ScreeningStanding.REFUSE
    assert screening.findings[1].code is ScreeningCode.LOCATION_INELIGIBLE


def test_a_country_only_place_cannot_choose_between_finer_location_policies() -> None:
    parameters = JobSearchParameters(
        locations=[
            Location(key="north", country="Freedonia", region="Northland"),
            Location(key="south", country="Freedonia", region="Southland"),
        ],
        countries=[CountryNames(country="Freedonia")],
        seniority=Seniority(target_titles=titles("Platform Product Manager")),
    )

    screening = screen_discovery(
        "Example Cooperative",
        "Platform Product Manager",
        "Freedonia",
        parameters,
        ["Platform Product Manager"],
        published_location_facts("Freedonia"),
    )

    assert screening.standing is ScreeningStanding.UNKNOWN
    assert screening.findings[1].code is ScreeningCode.LOCATION_UNRESOLVED


def _two_market_parameters(*locations: Location) -> JobSearchParameters:
    return JobSearchParameters(
        locations=list(locations),
        countries=[CountryNames(country="Freedonia"), CountryNames(country="Ruritania")],
        seniority=Seniority(target_titles=titles("Platform Product Manager")),
    )


def _screen_location(parameters: JobSearchParameters, location: str) -> ScreeningFinding:
    screening = screen_discovery(
        "Example Cooperative",
        "Platform Product Manager",
        location,
        parameters,
        ["Platform Product Manager"],
        StagedListingFacts(
            locations=PublishedFact(values=(location,), provenance="board", complete=True)
        ),
    )
    return screening.findings[1]


def test_a_posting_open_in_two_saved_markets_is_a_location_match() -> None:
    parameters = _two_market_parameters(
        Location(key="free_remote", country="Freedonia"),
        Location(key="ruri_remote", country="Ruritania"),
    )

    finding = _screen_location(parameters, "Remote, Freedonia; Remote, Ruritania")

    assert finding.standing is ScreeningStanding.MATCH
    assert finding.code is ScreeningCode.LOCATION_MATCH
    assert finding.subject == "free_remote"


def test_two_saved_markets_resolve_to_the_stronger_acceptance() -> None:
    parameters = _two_market_parameters(
        Location(
            key="needs_sponsor",
            country="Freedonia",
            eligibility=WorkEligibility.SPONSORSHIP_REQUIRED,
        ),
        Location(key="authorized", country="Ruritania"),
    )

    finding = _screen_location(parameters, "Remote, Freedonia; Remote, Ruritania")

    assert finding.code is ScreeningCode.LOCATION_MATCH
    assert finding.subject == "authorized"


def test_an_ineligible_place_in_a_named_pair_still_refuses() -> None:
    parameters = _two_market_parameters(
        Location(key="authorized", country="Freedonia"),
        Location(key="closed", country="Ruritania", eligibility=WorkEligibility.INELIGIBLE),
    )

    finding = _screen_location(parameters, "Remote, Freedonia; Remote, Ruritania")

    assert finding.standing is ScreeningStanding.REFUSE
    assert finding.code is ScreeningCode.LOCATION_INELIGIBLE
    assert finding.subject == "closed"


def test_an_unnamed_office_stays_unresolved() -> None:
    parameters = _two_market_parameters(
        Location(key="free_remote", country="Freedonia"),
        Location(key="ruri_remote", country="Ruritania"),
    )

    finding = _screen_location(parameters, "Harborview office")

    assert finding.code is ScreeningCode.LOCATION_UNRESOLVED
    assert finding.standing is ScreeningStanding.UNKNOWN


@pytest.mark.parametrize(
    "location",
    ["Remote", "remote", "Work from Home", "WFH", "Remote Nationwide", "Remote Location"],
)
def test_a_remote_only_label_states_no_place_rather_than_an_unresolved_one(location: str) -> None:
    parameters = _two_market_parameters(
        Location(key="free_remote", country="Freedonia"),
        Location(key="ruri_remote", country="Ruritania"),
    )

    finding = _screen_location(parameters, location)

    assert finding.code is ScreeningCode.LOCATION_MISSING
    assert finding.standing is ScreeningStanding.UNKNOWN


def test_a_remote_label_beside_a_named_market_still_matches_it() -> None:
    parameters = _two_market_parameters(
        Location(key="free_remote", country="Freedonia"),
        Location(key="ruri_remote", country="Ruritania"),
    )

    finding = _screen_location(parameters, "Remote, Freedonia")

    assert finding.code is ScreeningCode.LOCATION_MATCH
    assert finding.subject == "free_remote"


@pytest.mark.parametrize("location", ["Remote-MO", "Remote-NY", "Remote - NY", "Remote - US"])
def test_a_remote_label_with_a_us_state_code_matches_a_saved_united_states_place(
    location: str,
) -> None:
    finding = _screen_location(_one_country("us", "United States"), location)

    assert finding.code is ScreeningCode.LOCATION_MATCH
    assert finding.subject == "us"


def test_a_bare_remote_label_states_no_place_to_a_united_states_search() -> None:
    finding = _screen_location(_one_country("us", "United States"), "Remote")

    assert finding.code is ScreeningCode.LOCATION_MISSING


def test_a_remote_state_code_that_spells_a_saved_country_stays_unresolved() -> None:
    parameters = JobSearchParameters(
        locations=[
            Location(key="us", country="United States"),
            Location(key="de", country="Germany"),
        ]
    )

    finding = _screen_location(parameters, "Remote-DE")

    assert finding.code is ScreeningCode.LOCATION_UNRESOLVED


def _us_and_canada() -> JobSearchParameters:
    return JobSearchParameters(
        locations=[
            Location(key="us", country="United States"),
            Location(key="ca", country="Canada"),
        ]
    )


@pytest.mark.parametrize(
    "location",
    ["IL - Work from home", "PA - Conshohocken - Office", "MA - Westborough", "NY - Remote"],
)
def test_a_leading_us_state_code_matches_a_saved_united_states_place(location: str) -> None:
    finding = _screen_location(_us_and_canada(), location)

    assert finding.code is ScreeningCode.LOCATION_MATCH
    assert finding.subject == "us"


@pytest.mark.parametrize("location", ["IL - Work from home", "PA - Conshohocken - Office"])
def test_a_leading_us_state_code_to_a_canada_only_search_stays_unresolved(location: str) -> None:
    finding = _screen_location(_one_country("ca", "Canada"), location)

    assert finding.code is ScreeningCode.LOCATION_UNRESOLVED


@pytest.mark.parametrize(
    ("location", "key", "country"),
    [("DE - Home Office", "de", "Germany"), ("CA - Remote", "ca", "Canada")],
)
def test_a_leading_code_that_spells_a_saved_country_is_never_refused(
    location: str, key: str, country: str
) -> None:
    finding = _screen_location(_one_country(key, country), location)

    assert finding.standing is not ScreeningStanding.REFUSE


def test_extra_occupation_words_make_a_title_ambiguous_instead_of_a_match() -> None:
    screening = screen_discovery(
        "Example Cooperative",
        "Platform Product Analytics Manager",
        "Freedonia",
        home_market_search("Platform Product Manager"),
        ["Platform Product Manager"],
        published_location_facts("Freedonia"),
    )

    assert screening.standing is ScreeningStanding.UNKNOWN
    assert screening.findings[0].code is ScreeningCode.TITLE_EXTRA_ROLE_WORDS
    assert screening.findings[0].subject == "Platform Product Manager"


def test_a_publisher_evidenced_role_alias_can_trigger_a_title_exclusion() -> None:
    parameters = home_market_search("Data Product Manager").model_copy(
        update={
            "seniority": Seniority(
                target_titles=titles("Data Product Manager"),
                excluded_titles=["Data Team Lead"],
            )
        }
    )
    facts = StagedListingFacts(
        role_aliases=PublishedFact(
            values=("Data Team Lead",),
            provenance="workday.job_description",
            complete=True,
        ),
        locations=published_location_facts("Freedonia").locations,
    )

    screening = screen_discovery(
        "Example Cooperative",
        "Clinical Data Management",
        "Freedonia",
        parameters,
        ["Data Product Manager"],
        facts,
    )

    assert screening.standing is ScreeningStanding.REFUSE
    assert screening.findings[0].code is ScreeningCode.TITLE_EXCLUDED
    assert screening.findings[0].subject == "Data Team Lead"


def test_a_publisher_evidenced_role_alias_can_supply_the_matching_title() -> None:
    facts = StagedListingFacts(
        role_aliases=PublishedFact(
            values=("Associate Data Team Lead",),
            provenance="workday.job_description",
            complete=True,
        ),
        locations=published_location_facts("Freedonia").locations,
    )

    screening = screen_discovery(
        "Example Cooperative",
        "Clinical Data Management",
        "Freedonia",
        home_market_search("Associate Data Team Lead"),
        ["Associate Data Team Lead"],
        facts,
    )

    assert screening.findings[0].standing is ScreeningStanding.MATCH
    assert screening.findings[0].code is ScreeningCode.TITLE_MATCH


def test_an_exactly_two_thirds_title_overlap_stays_available_for_relevance_review() -> None:
    screening = screen_discovery(
        "Example Cooperative",
        "Data Platform Lead",
        "Freedonia",
        home_market_search("Data Platform Manager"),
        ["Data Platform Manager"],
        published_location_facts("Freedonia"),
    )

    assert screening.standing is ScreeningStanding.UNKNOWN
    assert screening.findings[0].code is ScreeningCode.TITLE_NEAR_MATCH
    assert screening.findings[0].subject == "Data Platform Manager"


def test_bulk_board_staging_keeps_adjacent_titles_when_the_market_is_unresolved() -> None:
    matching_place = screen_discovery(
        "Example Cooperative",
        "Data Platform Lead",
        "Freedonia",
        home_market_search("Data Platform Manager"),
        ["Data Platform Manager"],
        published_location_facts("Freedonia"),
    )
    unresolved_place = screen_discovery(
        "Example Cooperative",
        "Data Platform Lead",
        "Unknown",
        home_market_search("Data Platform Manager"),
        ["Data Platform Manager"],
        published_location_facts("Unknown"),
    )
    generic_family = screen_discovery(
        "Example Cooperative",
        "Senior Product Manager - Platform Services",
        "Freedonia",
        home_market_search("Senior Business Data Product Manager"),
        ["Senior Business Data Product Manager"],
        published_location_facts("Freedonia"),
    )

    assert supports_bulk_staging(matching_place)
    assert supports_bulk_staging(unresolved_place)
    assert not supports_bulk_staging(generic_family)
    assert supports_bulk_staging(generic_family, include_bounded_review=True)


def test_an_adjacent_title_with_an_explicit_foreign_country_fails_the_title_gate() -> None:
    screening = screen_discovery(
        "Example Cooperative",
        "Data Platform Lead",
        "Southport, Ruritania",
        home_market_search("Data Platform Manager"),
        ["Data Platform Manager"],
        published_location_facts("Southport, Ruritania"),
    )

    assert screening.findings[1].code is ScreeningCode.LOCATION_OUTSIDE
    assert not supports_bulk_staging(screening, include_bounded_review=True)


def test_an_excluded_occupation_does_not_shadow_a_specialized_target() -> None:
    parameters = home_market_search("Medical Information Director").model_copy(
        update={
            "seniority": Seniority(
                target_titles=titles("Medical Information Director"),
                excluded_titles=["Medical Director"],
            )
        }
    )

    screening = screen_discovery(
        "Example Pharma",
        "Medical Information Director",
        "Freedonia",
        parameters,
        ["Medical Information Director"],
        published_location_facts("Freedonia"),
    )

    assert screening.findings[0].code is not ScreeningCode.TITLE_EXCLUDED


def test_below_band_associate_titles_remain_available_for_review() -> None:
    parameters = data_product_search(minimum="senior")
    screening = screen_discovery(
        "Example Cooperative",
        "Associate Product Manager - Fictional Suite",
        "Freedonia",
        parameters,
        list(DATA_PRODUCT_TARGET_TITLES),
        StagedListingFacts(locations=PublishedFact(values=("Freedonia",), complete=True)),
    )
    assert supports_bulk_staging(screening, include_bounded_review=True) is True


def test_an_exact_title_match_ignores_the_seniority_gate() -> None:
    """Seniority refuses only review-lane expansion, never an exact title match."""
    parameters = data_product_search(minimum="senior")
    screening = screen_discovery(
        "Example Cooperative",
        "Associate Data Product Manager",
        "Freedonia",
        parameters,
        list(DATA_PRODUCT_TARGET_TITLES),
        StagedListingFacts(locations=PublishedFact(values=("Freedonia",), complete=True)),
    )
    title = next(item for item in screening.findings if item.dimension is ScreeningDimension.TITLE)
    seniority = next(
        item for item in screening.findings if item.dimension is ScreeningDimension.SENIORITY
    )
    assert title.standing is ScreeningStanding.MATCH
    assert seniority.code is ScreeningCode.SENIORITY_OUTSIDE
    assert supports_bulk_staging(screening, include_bounded_review=True) is True


def test_an_exact_title_match_outside_the_saved_places_is_not_bulk_staged() -> None:
    """A MATCH title still clears LOCATION_MISSING but not LOCATION_OUTSIDE."""
    screening = screen_discovery(
        "Example Cooperative",
        "Data Platform Lead",
        "Southport, Ruritania",
        home_market_search("Data Platform Lead"),
        ["Data Platform Lead"],
        published_location_facts("Southport, Ruritania"),
    )

    title = next(item for item in screening.findings if item.dimension is ScreeningDimension.TITLE)
    assert title.standing is ScreeningStanding.MATCH
    assert not supports_bulk_staging(screening, include_bounded_review=True)
    assert (
        bulk_staging_refusal(screening, include_bounded_review=True)
        is BulkStagingRefusal.LOCATION_OUTSIDE_SAVED_PLACES
    )


def test_a_multi_market_location_refuses_when_a_sibling_value_only_suggests_a_saved_place() -> None:
    """Each value is read on its own, so one suggestion cannot clear the whole fact."""
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="United Kingdom")],
        countries=[
            CountryNames(country="United Kingdom"),
            CountryNames(country="Canada"),
            CountryNames(country="Ruritania"),
        ],
        seniority=Seniority(target_titles=titles("Data Platform Lead")),
    )
    # "London, Ontario" names Canada and suggests the saved United Kingdom.
    screening = screen_discovery(
        "Example Cooperative",
        "Data Platform Lead",
        "London, Ontario",
        parameters,
        ["Data Platform Lead"],
        StagedListingFacts(
            locations=PublishedFact(
                values=("London, Ontario", "Southport, Ruritania"),
                provenance="fictional.location",
                complete=True,
            )
        ),
    )

    assert (
        bulk_staging_refusal(screening, include_bounded_review=True)
        is BulkStagingRefusal.LOCATION_OUTSIDE_SAVED_PLACES
    )


def test_a_multi_market_location_naming_one_saved_market_is_still_bulk_staged() -> None:
    """A value naming only saved countries keeps a reachable multi-market posting admitted."""
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="United Kingdom")],
        countries=[
            CountryNames(country="United Kingdom"),
            CountryNames(country="Canada"),
            CountryNames(country="Ruritania"),
        ],
        seniority=Seniority(target_titles=titles("Data Platform Lead")),
    )
    screening = screen_discovery(
        "Example Cooperative",
        "Data Platform Lead",
        "London, Ontario",
        parameters,
        ["Data Platform Lead"],
        StagedListingFacts(
            locations=PublishedFact(
                values=("London, Ontario", "Southport, Ruritania", "Manchester, United Kingdom"),
                provenance="fictional.location",
                complete=True,
            )
        ),
    )

    assert supports_bulk_staging(screening, include_bounded_review=True) is True
    assert bulk_staging_refusal(screening, include_bounded_review=True) is None


def test_a_multi_market_location_holding_an_unplaceable_town_is_still_bulk_staged() -> None:
    """A value resting on a subdivision code no place claims puts the posting nowhere."""
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="United States")],
        countries=[CountryNames(country="United States"), CountryNames(country="Ruritania")],
        seniority=Seniority(target_titles=titles("Data Platform Lead")),
    )
    # "Southport, PA" names a subdivision code the gazetteer cannot resolve to one
    # place, so it cannot be read as outside and must not let a sibling refuse.
    screening = screen_discovery(
        "Example Cooperative",
        "Data Platform Lead",
        "Southport, PA",
        parameters,
        ["Data Platform Lead"],
        StagedListingFacts(
            locations=PublishedFact(
                values=("Southport, PA", "Vantage, Ruritania"),
                provenance="fictional.location",
                complete=True,
            )
        ),
    )

    assert bulk_staging_refusal(screening, include_bounded_review=True) is None


def _location_finding(*values: str) -> ScreeningFinding:
    screening = screen_discovery(
        "Example Cooperative",
        "Data Platform Lead",
        values[0],
        _hybrid_holdout_parameters(),
        [],
        StagedListingFacts(
            locations=PublishedFact(values=values, provenance="fictional.location", complete=True)
        ),
    )
    return next(
        item for item in screening.findings if item.dimension is ScreeningDimension.LOCATION
    )


@pytest.mark.parametrize(
    ("values", "country"),
    [
        (("Hyderabad - TS - IN", "India"), "India"),
        (("Pune - MH - IN", "India"), "India"),
        (("Moscow (City)", "Russian Federation"), "Russia"),
        (("Kiryat Ono, IL", "Israel"), "Israel"),
    ],
)
def test_a_code_its_sibling_names_as_a_country_screens_the_row_outside(
    values: tuple[str, ...], country: str
) -> None:
    """A trailing "IN" beside "India" is India's code, not an unverified Indiana."""
    finding = _location_finding(*values)

    assert finding.code is ScreeningCode.LOCATION_OUTSIDE
    assert finding.subject == country


@pytest.mark.parametrize(
    "values",
    [("Carmel, IN", "United States"), ("New York, NY",), ("Toronto, CA", "Canada")],
)
def test_a_saved_country_beside_a_state_code_still_matches(values: tuple[str, ...]) -> None:
    assert _location_finding(*values).standing is ScreeningStanding.MATCH


def test_a_lone_unverified_foreign_code_is_never_read_outside() -> None:
    assert _location_finding("Kiryat Ono, IL").code is ScreeningCode.LOCATION_UNRESOLVED


# Towns no gazetteer row names, each written with a US state code that is also a country code.
_UNKNOWN_TOWN_BESIDE_ITS_CODE_COUNTRY = (
    ("Fictionville, IN", "India"),
    ("Fictionville, IN 46000", "India"),
    ("Fictionville, CA", "Canada"),
    ("Fictionville, DE", "Germany"),
    ("Fictionville, IL", "Israel"),
    ("Fictionville, PA", "Panama"),
    ("Fictionville, CO", "Colombia"),
)


@pytest.mark.parametrize("values", _UNKNOWN_TOWN_BESIDE_ITS_CODE_COUNTRY)
def test_an_unknown_town_on_a_state_code_is_never_placed_by_its_sibling(
    values: tuple[str, ...],
) -> None:
    """Only a known city settles the code as a country; an unknown town may be a US one."""
    assert _location_finding(*values).code is not ScreeningCode.LOCATION_OUTSIDE


def test_an_exact_title_match_with_an_unverified_location_is_still_bulk_staged() -> None:
    """LOCATION_UNVERIFIED is an unknown standing bulk staging admits, like LOCATION_MISSING."""
    screening = DiscoveryScreening(
        (
            _finding(ScreeningDimension.TITLE, ScreeningStanding.MATCH, ScreeningCode.TITLE_MATCH),
            _finding(
                ScreeningDimension.LOCATION,
                ScreeningStanding.UNKNOWN,
                ScreeningCode.LOCATION_UNVERIFIED,
            ),
        )
    )
    assert supports_bulk_staging(screening, include_bounded_review=True) is True
    assert bulk_staging_refusal(screening, include_bounded_review=True) is None


def test_inferred_seniority_keeps_review_open_while_a_weak_title_still_stops() -> None:
    parameters = data_product_search(minimum="senior")
    below_band = screen_discovery(
        "Example Cooperative",
        "Associate Product Manager - Fictional Suite",
        "Freedonia",
        parameters,
        list(DATA_PRODUCT_TARGET_TITLES),
        StagedListingFacts(locations=PublishedFact(values=("Freedonia",), complete=True)),
    )
    weak_title = screen_discovery(
        "Example Biotech",
        "Lead Scientist - Commercial Drug Product",
        "Freedonia",
        home_market_search("Technical Product Manager, Drug Information"),
        ["Technical Product Manager, Drug Information"],
        published_location_facts("Freedonia"),
    )
    assert bulk_staging_refusal(below_band, include_bounded_review=True) is None
    assert (
        bulk_staging_refusal(weak_title, include_bounded_review=True)
        is BulkStagingRefusal.INSUFFICIENT_TITLE_EVIDENCE
    )


def test_an_exact_match_with_no_stated_location_still_supports_bulk_staging() -> None:
    """A missing location reads as LOCATION_MISSING, which a MATCH title still clears."""
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="Freedonia")],
        countries=[CountryNames(country="Freedonia")],
        seniority=Seniority(target_titles=titles("Pharmacist")),
    )
    screening = screen_discovery(
        "Acme", "Pharmacist", "", parameters, ["Pharmacist"], StagedListingFacts()
    )
    assert supports_bulk_staging(screening, include_bounded_review=True) is True


@pytest.mark.parametrize(
    "title",
    [
        "Scientist/ Senior Engineer - Commercial Drug Product",
        "Lead Scientist - Commercial Drug Product",
    ],
)
def test_shared_domain_words_do_not_turn_a_different_occupation_into_a_role_match(
    title: str,
) -> None:
    screening = screen_discovery(
        "Example Biotech",
        title,
        "Freedonia",
        home_market_search("Technical Product Manager, Drug Information"),
        ["Technical Product Manager, Drug Information"],
        published_location_facts("Freedonia"),
    )

    assert screening.standing is ScreeningStanding.UNKNOWN
    assert screening.findings[0].code is ScreeningCode.TITLE_MISMATCH
    assert not supports_bulk_staging(screening, include_bounded_review=True)


def test_one_shared_role_word_leaves_semantic_relevance_unresolved() -> None:
    screening = screen_discovery(
        "Example Cooperative",
        "Senior Compliance Manager",
        "Freedonia",
        home_market_search("Senior Clinical Data Product Manager"),
        ["Senior Clinical Data Product Manager"],
        published_location_facts("Freedonia"),
    )

    assert screening.standing is ScreeningStanding.UNKNOWN
    assert screening.findings[0].code is ScreeningCode.TITLE_MISMATCH


def test_strict_title_matching_cannot_make_a_semantic_relevance_decision() -> None:
    parameters = home_market_search("Data Product Manager").model_copy(
        update={
            "seniority": Seniority(
                target_titles=titles("Data Product Manager"),
                title_matching="strict",
            )
        }
    )

    screening = screen_discovery(
        "Example Cooperative",
        "Architect, Semantic Systems & AI Innovation",
        "Freedonia",
        parameters,
        ["Data Product Manager"],
        published_location_facts("Freedonia"),
    )

    assert screening.standing is ScreeningStanding.UNKNOWN
    assert screening.findings[0].code is ScreeningCode.TITLE_MISMATCH


def test_an_unusual_relevant_title_survives_until_an_explicit_hard_conflict() -> None:
    eligible_parameters = home_market_search("Data Product Manager").model_copy(
        update={
            "seniority": Seniority(
                target_titles=titles("Data Product Manager"),
                specialist_ic_role_nouns=["Architect"],
                strong_domain_tokens=["Semantic Systems"],
            )
        }
    )
    ineligible_parameters = JobSearchParameters(
        locations=[
            Location(
                key="closed",
                country="Freedonia",
                eligibility=WorkEligibility.INELIGIBLE,
            )
        ],
        countries=[CountryNames(country="Freedonia")],
        seniority=eligible_parameters.seniority,
    )

    title_only = screen_discovery(
        "Example Cooperative",
        "Architect, Semantic Systems & AI Innovation",
        "Freedonia",
        eligible_parameters,
        ["Data Product Manager"],
        published_location_facts("Freedonia"),
    )
    ineligible_location = screen_discovery(
        "Example Cooperative",
        "Architect, Semantic Systems & AI Innovation",
        "Freedonia",
        ineligible_parameters,
        ["Data Product Manager"],
        published_location_facts("Freedonia"),
    )

    assert title_only.standing is ScreeningStanding.UNKNOWN
    assert title_only.findings[0].code is ScreeningCode.TITLE_SPECIALIST_IC_REVIEW
    assert supports_bulk_staging(title_only)
    assert ineligible_location.standing is ScreeningStanding.REFUSE
    assert ineligible_location.findings[1].code is ScreeningCode.LOCATION_INELIGIBLE


@pytest.mark.parametrize(
    ("term", "title"),
    [
        (term, f"{writing} {noun}")
        for term in ("Formulary", "340B", "Clinical Content")
        for writing in dict.fromkeys(
            (term, term.upper(), term.replace(" ", "-"), term.replace(" ", " / "))
        )
        for noun in ("Advisor", "Nurse")
    ],
)
def test_configured_domain_words_retain_unconfigured_and_conflicting_roles_for_review(
    term: str, title: str
) -> None:
    parameters = home_market_search("Product Manager").model_copy(
        update={
            "seniority": Seniority(
                target_titles=titles("Product Manager", "Data Product Manager"),
                specialist_ic_role_nouns=["Engineer"],
                strong_domain_tokens=[term],
            )
        }
    )
    screening = screen_discovery(
        "Example Cooperative",
        title,
        "Freedonia",
        parameters,
        ["Product Manager", "Data Product Manager"],
        published_location_facts("Freedonia"),
    )

    title_finding = screening.findings[0]
    assert title_finding.code.value == "title_domain_review"
    assert title_finding.standing is ScreeningStanding.UNKNOWN
    assert title_finding.subject == term.casefold()
    assert "review" in title_finding.because
    assert supports_title_expansion(title_finding)
    assert supports_bulk_staging(screening, include_bounded_review=True)


@pytest.mark.parametrize(
    ("phrase", "title"),
    [
        (phrase, f"Senior {writing} Advisor")
        for phrase in ("Analytics", "Writer", "Content", "Standards", "Data Trust")
        for writing in dict.fromkeys(
            (phrase, phrase.upper(), phrase.replace(" ", "-"), phrase.replace(" ", " / "))
        )
    ],
)
@pytest.mark.parametrize("context", ["publisher", "registered_source"])
def test_configured_employer_membership_supplies_domain_review_context(
    phrase: str, title: str, context: str
) -> None:
    parameters = home_market_search("Service Manager").model_copy(
        update={
            "seniority": Seniority(
                target_titles=titles("Service Manager"), specialist_ic_role_nouns=[phrase]
            ),
            "employers": Employers(
                archetypes=[
                    EmployerArchetype(
                        name="Knowledge publisher", employer_names=["Example Cooperative"]
                    )
                ]
            ),
        }
    )
    facts = published_location_facts("Freedonia")
    if context == "registered_source":
        facts = StagedListingFacts(identity_trust=ListingIdentityTrust.UNVERIFIED_HINT)
    screening = screen_discovery(
        "Example Cooperative" if context == "publisher" else "Globex",
        title,
        "Freedonia",
        parameters,
        ["Service Manager"],
        facts,
        source_employer=" EXAMPLE Cooperative " if context == "registered_source" else "",
    )

    finding = screening.findings[0]
    assert finding.code is ScreeningCode.TITLE_DOMAIN_REVIEW
    assert finding.standing is ScreeningStanding.UNKNOWN
    assert phrase.casefold() in finding.subject
    assert "Knowledge publisher" in finding.because
    assert "example cooperative" in finding.because.casefold()
    assert phrase.casefold() in finding.because
    assert ("registered source" in finding.because) == (context == "registered_source")
    assert supports_title_expansion(finding)
    assert supports_bulk_staging(screening, include_bounded_review=True)
    assert constraint_confidence(screening) is not ConstraintConfidence.PURSUE


@pytest.mark.parametrize(
    ("employer", "trust", "members", "phrase", "title"),
    [
        ("Globex", ListingIdentityTrust.PUBLISHER, ["Acme"], "Writer", "Writer"),
        ("Acme Labs", ListingIdentityTrust.PUBLISHER, ["Acme"], "Writer", "Writer"),
        ("Acme", ListingIdentityTrust.UNVERIFIED_HINT, ["Acme"], "Writer", "Writer"),
        ("Acme", ListingIdentityTrust.PUBLISHER, [], "Writer", "Writer"),
        ("Acme", ListingIdentityTrust.PUBLISHER, ["Acme"], "", "Writer"),
        ("Acme", ListingIdentityTrust.PUBLISHER, ["Acme"], "Writer", "Cook"),
        ("Acme", ListingIdentityTrust.PUBLISHER, ["Acme"], "Writer", "Writerly Consultant"),
        ("Acme", ListingIdentityTrust.PUBLISHER, ["Acme"], "Content", "Contented Advisor"),
        (
            "Acme",
            ListingIdentityTrust.PUBLISHER,
            ["Acme"],
            "Data Trust",
            "Data Platform Trust Advisor",
        ),
    ],
)
def test_employer_domain_review_requires_an_exact_association_and_configured_phrase(
    employer: str,
    trust: ListingIdentityTrust,
    members: list[str],
    phrase: str,
    title: str,
) -> None:
    parameters = JobSearchParameters(
        seniority=Seniority(
            target_titles=titles("Service Manager"), specialist_ic_role_nouns=[phrase]
        ),
        employers=Employers(
            industries=["Knowledge publishing"],
            archetypes=[EmployerArchetype(name="Knowledge publisher", employer_names=members)],
        ),
    )
    screening = screen_discovery(
        employer,
        title,
        "",
        parameters,
        ["Service Manager"],
        StagedListingFacts(identity_trust=trust),
    )

    assert screening.findings[0].code is not ScreeningCode.TITLE_DOMAIN_REVIEW
    if title == "Cook" or not phrase:
        assert not supports_bulk_staging(screening, include_bounded_review=True)


@pytest.mark.parametrize("boundary", ["strict", "excluded_title", "excluded_employer"])
def test_employer_domain_review_preserves_explicit_title_and_employer_boundaries(
    boundary: str,
) -> None:
    parameters = JobSearchParameters(
        seniority=Seniority(
            target_titles=titles("Service Manager"),
            specialist_ic_role_nouns=["Writer"],
            title_matching="strict" if boundary == "strict" else "guided",
            excluded_titles=["Writer"] if boundary == "excluded_title" else [],
        ),
        employers=Employers(
            excluded_employers=["Acme"] if boundary == "excluded_employer" else [],
            archetypes=[EmployerArchetype(name="Knowledge publisher", employer_names=["Acme"])],
        ),
    )
    screening = screen_discovery(
        "Acme", "Writer", "", parameters, ["Service Manager"], StagedListingFacts()
    )

    assert screening.findings[0].code is not ScreeningCode.TITLE_DOMAIN_REVIEW
    if boundary != "excluded_employer":
        assert not supports_bulk_staging(screening, include_bounded_review=True)
    if boundary != "strict":
        assert screening.standing is ScreeningStanding.REFUSE


@pytest.mark.parametrize(
    "targets",
    [
        ["Service Manager"],
        ["Service Manager", "Operations Manager"],
        ["Operations Manager", "Service Manager"],
    ],
)
def test_employer_domain_review_does_not_depend_on_target_order(targets: list[str]) -> None:
    parameters = JobSearchParameters(
        seniority=Seniority(target_titles=titles(*targets), specialist_ic_role_nouns=["Writer"]),
        employers=Employers(
            archetypes=[EmployerArchetype(name="Knowledge publisher", employer_names=["Acme"])]
        ),
    )
    screened = screen_discovery("Acme", "Writer", "", parameters, targets, StagedListingFacts())
    exact = screen_discovery(
        "Acme", "Writer", "", parameters, [*targets, "Writer"], StagedListingFacts()
    )

    assert screened.findings[0].code is ScreeningCode.TITLE_DOMAIN_REVIEW
    assert exact.findings[0].code is ScreeningCode.TITLE_MATCH
    assert supports_bulk_staging(screened, include_bounded_review=True)
    assert supports_bulk_staging(exact, include_bounded_review=True)


@pytest.mark.parametrize(
    ("term", "title"),
    [
        ("Formulary", "Preformulary Advisor"),
        ("340B", "340Beta Advisor"),
        ("Clinical Content", "Clinical Contented Advisor"),
        ("Clinical Content", "Clinical Advisor"),
        ("Clinical Content", "Content Advisor"),
    ],
)
def test_partial_domain_words_never_supply_the_title_review_evidence(term: str, title: str) -> None:
    parameters = home_market_search("Service Manager").model_copy(
        update={
            "seniority": Seniority(
                target_titles=titles("Service Manager"), strong_domain_tokens=[term]
            )
        }
    )
    screening = screen_discovery(
        "Example Cooperative",
        title,
        "Freedonia",
        parameters,
        ["Service Manager"],
        published_location_facts("Freedonia"),
    )

    assert not supports_title_expansion(screening.findings[0], include_bounded_review=True)
    assert (
        bulk_staging_refusal(screening, include_bounded_review=True)
        is BulkStagingRefusal.INSUFFICIENT_TITLE_EVIDENCE
    )


@pytest.mark.parametrize(
    "rung", ["Associate", "Junior", "Executive Director", "VP", "Vice President"]
)
def test_title_inferred_levels_outside_the_saved_band_remain_open_for_review(rung: str) -> None:
    parameters = home_market_search("Service Manager").model_copy(
        update={
            "seniority": Seniority(
                target_titles=titles("Service Manager"),
                minimum="senior",
                maximum="director",
                strong_domain_tokens=["Linkage"],
            )
        }
    )
    screening = screen_discovery(
        "Example Cooperative",
        f"{rung} Linkage Advisor",
        "Freedonia",
        parameters,
        ["Service Manager"],
        published_location_facts("Freedonia"),
    )

    seniority = next(f for f in screening.findings if f.dimension is ScreeningDimension.SENIORITY)
    assert seniority.standing is ScreeningStanding.UNKNOWN
    assert seniority.code is ScreeningCode.SENIORITY_OUTSIDE
    assert "title" in seniority.because.lower() and "review" in seniority.because.lower()
    assert supports_bulk_staging(screening, include_bounded_review=True)


@pytest.mark.parametrize("excluded", ["SVP", "EVP", "Chief", "Vice President"])
def test_explicitly_excluded_titles_still_refuse_configured_domain_evidence(excluded: str) -> None:
    parameters = home_market_search("Service Manager").model_copy(
        update={
            "seniority": Seniority(
                target_titles=titles("Service Manager"),
                strong_domain_tokens=["Linkage"],
                excluded_titles=[excluded],
            )
        }
    )
    screening = screen_discovery(
        "Example Cooperative",
        f"{excluded} Linkage Advisor",
        "Freedonia",
        parameters,
        ["Service Manager"],
        published_location_facts("Freedonia"),
    )

    assert screening.standing is ScreeningStanding.REFUSE
    assert screening.findings[0].code is ScreeningCode.TITLE_EXCLUDED
    assert not supports_bulk_staging(screening, include_bounded_review=True)


def test_a_configured_specialist_role_and_domain_token_retain_a_low_overlap_title() -> None:
    parameters = home_market_search("Service Manager").model_copy(
        update={
            "seniority": Seniority(
                target_titles=titles("Service Manager"),
                specialist_ic_role_nouns=["Planner"],
                strong_domain_tokens=["Linkage"],
            )
        }
    )

    screening = screen_discovery(
        "Example Cooperative",
        "Principal Planner, Systems Linkage",
        "Freedonia",
        parameters,
        ["Service Manager"],
        published_location_facts("Freedonia"),
    )

    assert screening.standing is ScreeningStanding.UNKNOWN
    assert screening.findings[0].code is ScreeningCode.TITLE_SPECIALIST_IC_REVIEW
    assert screening.findings[0].subject == "planner, linkage"


def test_a_multiword_domain_term_is_not_reduced_to_one_generic_word() -> None:
    parameters = home_market_search("Service Manager").model_copy(
        update={
            "seniority": Seniority(
                target_titles=titles("Service Manager"),
                specialist_ic_role_nouns=["Analyst"],
                strong_domain_tokens=["Data Integration"],
            )
        }
    )

    generic = screen_discovery(
        "Example Cooperative",
        "Senior Data Analyst",
        "Freedonia",
        parameters,
        ["Service Manager"],
        published_location_facts("Freedonia"),
    )
    specific = screen_discovery(
        "Example Cooperative",
        "Senior Data Integration Analyst",
        "Freedonia",
        parameters,
        ["Service Manager"],
        published_location_facts("Freedonia"),
    )

    assert generic.findings[0].code is ScreeningCode.TITLE_SPECIALIST_IC_REVIEW
    assert generic.findings[0].subject == "analyst"
    assert generic.standing is ScreeningStanding.UNKNOWN
    assert specific.findings[0].code is ScreeningCode.TITLE_SPECIALIST_IC_REVIEW
    assert specific.findings[0].subject == "analyst, data integration"


def test_a_specialist_review_subject_preserves_operator_token_priority() -> None:
    parameters = home_market_search("Service Manager").model_copy(
        update={
            "seniority": Seniority(
                target_titles=titles("Service Manager"),
                specialist_ic_role_nouns=["Coordinator", "Planner"],
                strong_domain_tokens=["Systems", "Linkage"],
            )
        }
    )

    screening = screen_discovery(
        "Example Cooperative",
        "Principal Planner, Systems Linkage Coordinator",
        "Freedonia",
        parameters,
        ["Service Manager"],
        published_location_facts("Freedonia"),
    )

    assert screening.findings[0].code is ScreeningCode.TITLE_SPECIALIST_IC_REVIEW
    assert screening.findings[0].subject == "coordinator, systems"


def test_an_unconfigured_specialist_review_policy_reads_the_same_from_either_client() -> None:
    """A stated-blank list and an omitted one both leave relevance unresolved."""
    stated_blank = {
        "target_titles": [{"title": "Data Platform Lead"}],
        "excluded_titles": [],
        "specialist_ic_role_nouns": [],
        "strong_domain_tokens": [],
        "title_matching": "guided",
    }
    omitted = {"target_titles": [{"title": "Data Platform Lead"}]}

    for payload in (stated_blank, omitted):
        parameters = home_market_search("Data Platform Lead").model_copy(
            update={"seniority": Seniority.model_validate(payload)}
        )
        screening = screen_discovery(
            "Example Cooperative",
            "Principal Data Planner",
            "Freedonia",
            parameters,
            ["Data Platform Lead"],
            published_location_facts("Freedonia"),
        )

        assert screening.standing is ScreeningStanding.UNKNOWN
        assert screening.findings[0].code is ScreeningCode.TITLE_MISMATCH


def test_an_unsaved_role_noun_without_a_domain_token_keeps_nothing() -> None:
    seniority = Seniority(
        target_titles=titles("Service Manager"), specialist_ic_role_nouns=["Architect"]
    )
    screening = screen_discovery(
        "Example Cooperative",
        "Principal Planner, Systems Linkage",
        "Freedonia",
        home_market_search("Service Manager").model_copy(update={"seniority": seniority}),
        ["Service Manager"],
        published_location_facts("Freedonia"),
    )

    assert screening.standing is ScreeningStanding.UNKNOWN
    assert screening.findings[0].code is ScreeningCode.TITLE_MISMATCH
    assert not supports_bulk_staging(screening, include_bounded_review=True)


def _role_phrase_parameters() -> JobSearchParameters:
    return home_market_search("Program Manager").model_copy(
        update={
            "seniority": Seniority(
                target_titles=titles("Program Manager"),
                specialist_ic_role_nouns=[
                    "Domain Expert",
                    "Implementation Consultant",
                    "Business Consultant",
                    "Principal Consultant",
                    "Strategy Leader",
                    "Analyst",
                ],
                strong_domain_tokens=["Pharmacovigilance"],
            )
        }
    )


def _role_phrase_screening(
    title: str,
    parameters: JobSearchParameters | None = None,
    searched: list[str] | None = None,
) -> DiscoveryScreening:
    return screen_discovery(
        "Example Cooperative",
        title,
        "Freedonia",
        parameters or _role_phrase_parameters(),
        searched or ["Program Manager"],
        published_location_facts("Freedonia"),
    )


@pytest.mark.parametrize(
    ("title", "subject"),
    [
        ("Implementation Consultant", "implementation consultant"),
        ("Data, Analytics and AI Business Consultant", "business consultant"),
        ("Senior Implementation Consultant", "implementation consultant"),
        ("Principal Business Consultant", "business consultant"),
        ("Lead Domain Expert", "domain expert"),
        ("Senior Safety Domain Expert - ProductLine", "domain expert"),
        ("Implementation Consultant - Payroll", "implementation consultant"),
        ("Strategy Leader", "strategy leader"),
        ("Principal Consultant", "principal consultant"),
        ("Senior Data Analyst", "analyst"),
    ],
)
def test_a_saved_role_phrase_alone_retains_a_title_for_relevance_review(
    title: str, subject: str
) -> None:
    screening = _role_phrase_screening(title)

    assert screening.findings[0].code is ScreeningCode.TITLE_SPECIALIST_IC_REVIEW
    assert screening.findings[0].subject == subject
    assert supports_bulk_staging(screening)


_SAVED_NOUN_WRITINGS = [
    (noun, template.format(role=f"{prefix}{noun}"))
    for noun in ("Consultant", "Analyst", "Planner")
    for prefix in ("", "Senior ", "Principal ", "Lead ")
    for template in (
        "{role}",
        "{role}, Enterprise Services",
        "{role} - Enterprise Services",
        "{role}, Data Services",
    )
]


@pytest.mark.parametrize(("noun", "title"), _SAVED_NOUN_WRITINGS)
def test_an_explicitly_saved_bare_noun_survives_added_targets_in_either_order(
    noun: str, title: str
) -> None:
    parameters = _role_phrase_parameters().model_copy(
        update={
            "seniority": Seniority(
                target_titles=titles("Program Manager"),
                specialist_ic_role_nouns=[noun],
            )
        }
    )
    baseline = _role_phrase_screening(title, parameters)
    expanded = [
        _role_phrase_screening(title, parameters, searched)
        for searched in (
            ["Program Manager", "Registered Nurse"],
            ["Registered Nurse", "Program Manager"],
        )
    ]

    for screening in (baseline, *expanded):
        assert screening.findings[0].code is ScreeningCode.TITLE_SPECIALIST_IC_REVIEW
        assert screening.findings[0].standing is ScreeningStanding.UNKNOWN
        assert screening.findings[0].subject == noun.casefold()
        assert supports_bulk_staging(screening)


@pytest.mark.parametrize(
    "searched",
    [
        ["Program Manager"],
        ["Program Manager", "Sales Representative"],
        ["Sales Representative", "Program Manager"],
        ["Program Manager", "Service Manager"],
        ["Service Manager", "Program Manager"],
    ],
)
def test_an_explicitly_saved_noun_keeps_a_conflicting_role_family_for_review(
    searched: list[str],
) -> None:
    screening = _role_phrase_screening("Senior Data Analyst", searched=searched)

    assert screening.findings[0].code is ScreeningCode.TITLE_SPECIALIST_IC_REVIEW
    assert screening.findings[0].standing is ScreeningStanding.UNKNOWN
    assert screening.findings[0].subject == "analyst"
    assert supports_bulk_staging(screening)


@pytest.mark.parametrize(("noun", "title"), _SAVED_NOUN_WRITINGS)
def test_an_explicit_title_exclusion_refuses_an_explicitly_saved_bare_noun(
    noun: str, title: str
) -> None:
    parameters = _role_phrase_parameters().model_copy(
        update={
            "seniority": Seniority(
                target_titles=titles("Program Manager"),
                specialist_ic_role_nouns=[noun],
                excluded_titles=[title],
            )
        }
    )

    screening = _role_phrase_screening(title, parameters)

    assert screening.findings[0].code is ScreeningCode.TITLE_EXCLUDED
    assert screening.findings[0].standing is ScreeningStanding.REFUSE
    assert not supports_bulk_staging(screening)


@pytest.mark.parametrize(("noun", "title"), _SAVED_NOUN_WRITINGS)
def test_an_unsaved_bare_noun_still_stops_without_other_title_evidence(
    noun: str, title: str
) -> None:
    parameters = _role_phrase_parameters().model_copy(
        update={
            "seniority": Seniority(
                target_titles=titles("Program Manager"),
                specialist_ic_role_nouns=["Domain Expert"],
            )
        }
    )

    screening = _role_phrase_screening(title, parameters)

    assert screening.findings[0].code is not ScreeningCode.TITLE_SPECIALIST_IC_REVIEW
    assert bulk_staging_refusal(screening) is BulkStagingRefusal.INSUFFICIENT_TITLE_EVIDENCE


@pytest.mark.parametrize(
    "title",
    [
        "Senior Payroll Consultant",
        "Expert, Domain Services",
        "Senior Data Auditor",
        "Warehouse Supervisor",
    ],
)
def test_a_title_without_a_saved_role_phrase_or_other_evidence_still_stops(title: str) -> None:
    screening = _role_phrase_screening(title)

    assert screening.findings[0].code is not ScreeningCode.TITLE_SPECIALIST_IC_REVIEW
    assert bulk_staging_refusal(screening) is BulkStagingRefusal.INSUFFICIENT_TITLE_EVIDENCE


def test_a_saved_role_phrase_beside_a_domain_token_keeps_naming_the_token() -> None:
    screening = _role_phrase_screening("Pharmacovigilance Strategy Leader")

    assert screening.findings[0].code is ScreeningCode.TITLE_SPECIALIST_IC_REVIEW
    assert screening.findings[0].subject == "strategy leader, pharmacovigilance"


def _adjacent_parameters(
    title_matching: Literal["guided", "strict"] = "guided",
) -> JobSearchParameters:
    return home_market_search("Service Manager").model_copy(
        update={
            "seniority": Seniority(
                target_titles=titles("Service Manager"),
                specialist_ic_role_nouns=["Developer", "Engineer"],
                strong_domain_tokens=["Linkage"],
                excluded_titles=["Sales Engineer"],
                title_matching=title_matching,
            ),
            "focus": Focus(
                capabilities=[Cluster(name="tooling", terms=["developer tooling", "release"])],
                domains=[Cluster(name="registries", terms=["registry pipelines"])],
            ),
        }
    )


def _adjacent(title: str, summary: str = "", parameters: JobSearchParameters | None = None):
    return screen_discovery(
        "Freedonia Health",
        title,
        "Freedonia",
        parameters or _adjacent_parameters(),
        ["Service Manager"],
        published_location_facts("Freedonia"),
        summary=summary,
    )


def test_a_focus_term_in_the_title_retains_an_unfamiliar_role_for_bounded_review() -> None:
    screening = _adjacent("Senior Developer, Developer Tooling")

    assert screening.standing is ScreeningStanding.UNKNOWN
    assert screening.findings[0].code is ScreeningCode.TITLE_SPECIALIST_IC_REVIEW
    assert screening.findings[0].subject == "developer, developer tooling"
    assert supports_bulk_staging(screening)


def test_a_focus_term_in_the_posting_summary_retains_a_bare_role_noun() -> None:
    bare = _adjacent("Senior Developer, Acceleration Team")
    described = _adjacent(
        "Senior Developer, Acceleration Team",
        "You will build reusable developer tooling and measure what it saves engineers.",
    )
    single_word_only = _adjacent("Senior Developer, Acceleration Team", "Own the release notes.")

    assert bare.findings[0].code is ScreeningCode.TITLE_SPECIALIST_IC_REVIEW
    assert bare.findings[0].subject == "developer"
    assert described.findings[0].code is ScreeningCode.TITLE_SPECIALIST_IC_REVIEW
    assert described.findings[0].subject == "developer, developer tooling (posting)"
    assert described.findings[0].because == (
        "The posting states developer tooling, so this developer title is kept for "
        "relevance review."
    )
    assert single_word_only.findings[0].code is ScreeningCode.TITLE_SPECIALIST_IC_REVIEW
    assert single_word_only.findings[0].subject == "developer"


def test_a_focus_term_in_the_title_names_both_title_words_that_keep_it() -> None:
    screening = _adjacent("Senior Developer, Developer Tooling")

    assert screening.findings[0].because == (
        "The title states developer and developer tooling, so it is kept for relevance review."
    )


def _rescreened(parameters: JobSearchParameters) -> DiscoveryScreening:
    """Screen a stored row the way a rescreen does: its staged facts and no summary."""
    stored = StagedListingFacts(
        locations=published_location_facts("Freedonia").locations,
        focus_terms=PublishedFact(
            values=("developer tooling",), provenance="posting.summary", complete=True
        ),
    )
    return screen_discovery(
        "Freedonia Health",
        "Senior Developer, Acceleration Team",
        "Freedonia",
        parameters,
        ["Service Manager"],
        stored,
    )


def test_a_rescreen_without_a_summary_keeps_a_row_on_its_stored_focus_term() -> None:
    screening = _rescreened(_adjacent_parameters())

    assert screening.findings[0].code is ScreeningCode.TITLE_SPECIALIST_IC_REVIEW
    assert screening.findings[0].subject == "developer, developer tooling (posting)"


def test_a_stored_focus_term_no_longer_saved_keeps_nothing_on_a_rescreen() -> None:
    seniority = _adjacent_parameters().seniority.model_copy(
        update={"specialist_ic_role_nouns": ["Acceleration Developer"]}
    )
    saved = _adjacent_parameters().model_copy(update={"seniority": seniority})
    parameters = saved.model_copy(
        update={"focus": Focus(capabilities=[Cluster(name="tooling", terms=["release"])])}
    )

    retained = _rescreened(saved)
    screening = _rescreened(parameters)

    assert retained.findings[0].code is ScreeningCode.TITLE_SPECIALIST_IC_REVIEW
    assert retained.findings[0].subject == "acceleration developer, developer tooling (posting)"
    assert screening.findings[0].code is ScreeningCode.TITLE_MISMATCH


def test_an_unsaved_role_noun_at_a_domain_employer_is_not_evidence_by_itself() -> None:
    screening = _adjacent("Senior Designer, Payments", "Process card payments reliably.")

    assert screening.findings[0].code is ScreeningCode.TITLE_MISMATCH


def test_an_unsuitable_role_at_a_domain_employer_does_not_pass_on_industry() -> None:
    accountant = _adjacent("Accountant", "Support developer tooling and registry pipelines.")
    excluded = _adjacent("Sales Engineer, Developer Tooling")

    assert accountant.findings[0].code is ScreeningCode.TITLE_MISMATCH
    assert excluded.standing is ScreeningStanding.REFUSE
    assert excluded.findings[0].code is ScreeningCode.TITLE_EXCLUDED


def test_strict_title_matching_ignores_focus_terms_and_summaries() -> None:
    screening = _adjacent(
        "Senior Developer, Developer Tooling",
        "Build developer tooling.",
        _adjacent_parameters(title_matching="strict"),
    )

    assert screening.findings[0].code is ScreeningCode.TITLE_MISMATCH


def test_a_body_that_states_a_multiword_saved_term_returns_that_term() -> None:
    parameters = _adjacent_parameters()

    found = body_states_saved_focus(
        parameters, "Own registry pipelines end to end for our platform team."
    )

    assert found == "registry pipelines"


def test_a_cluster_name_that_is_not_a_stated_term_is_never_returned() -> None:
    parameters = _adjacent_parameters()

    # "tooling" and "registries" are cluster names, not stated terms.
    found = body_states_saved_focus(parameters, "We invest heavily in tooling and registries.")

    assert found is None


def test_a_partial_word_match_does_not_satisfy_a_stated_term() -> None:
    parameters = _adjacent_parameters()

    found = body_states_saved_focus(parameters, "We ship developer tools for platform teams.")

    assert found is None


def test_an_empty_body_never_states_a_saved_focus_term() -> None:
    parameters = _adjacent_parameters()

    assert body_states_saved_focus(parameters, "") is None
    assert body_states_saved_focus(parameters, "   ") is None


def test_an_adjacent_title_reaches_review_but_is_never_a_match() -> None:
    """The lane routes to semantic review; it makes no relevance claim of its own."""
    screening = _adjacent("Engineer, Registry Pipelines", "Sell registry subscriptions to clinics.")

    assert screening.standing is ScreeningStanding.UNKNOWN
    assert screening.findings[0].standing is ScreeningStanding.UNKNOWN
    assert screening.findings[0].code is ScreeningCode.TITLE_SPECIALIST_IC_REVIEW


def test_adjacent_titles_are_not_dropped_from_the_review_lane() -> None:
    candidates = [
        (index, _adjacent(f"Senior Developer, Developer Tooling {index}")) for index in range(40)
    ]

    eligible, family_omitted, stage_omitted = select_title_survivors(candidates, 500)

    assert len(eligible) == 40
    assert family_omitted == []
    assert stage_omitted == []


@pytest.mark.parametrize(
    ("title", "location", "stageable"),
    [
        ("Senior Developer, Developer Tooling", "Remote within Freedonia", True),
        ("Senior Developer, Developer Tooling", "", True),
        ("Senior Developer, Developer Tooling", "Harborview, Ruritania", False),
        ("Senior Service Manager, Fleet", "Remote within Freedonia", True),
    ],
)
def test_two_evidence_tokens_earn_the_read_that_settles_a_worded_location(
    title: str, location: str, stageable: bool
) -> None:
    """A missing location defers to the body read; a resolved foreign one still refuses."""
    screening = screen_discovery(
        "Freedonia Health",
        title,
        location,
        _adjacent_parameters(),
        ["Service Manager"],
        StagedListingFacts(
            locations=PublishedFact(values=(), provenance="careers_page", complete=False)
        ),
    )

    assert screening.findings[1].code is not ScreeningCode.LOCATION_MATCH
    assert supports_bulk_staging(screening, include_bounded_review=True) is stageable


def test_the_specialist_lane_expands_an_unresolved_location_only_when_the_fact_is_incomplete() -> (
    None
):
    """A read can settle a worded place a board left incomplete, not a complete office name."""
    complete_office = screen_discovery(
        "Freedonia Health",
        "Senior Developer, Developer Tooling",
        "Harborview office",
        _adjacent_parameters(),
        ["Service Manager"],
        StagedListingFacts(
            locations=PublishedFact(
                values=("Harborview office",), provenance="greenhouse.location", complete=True
            )
        ),
    )
    incomplete_worded = screen_discovery(
        "Freedonia Health",
        "Senior Developer, Developer Tooling",
        "Remote within Freedonia",
        _adjacent_parameters(),
        ["Service Manager"],
        StagedListingFacts(
            locations=PublishedFact(values=(), provenance="careers_page", complete=False)
        ),
    )

    assert complete_office.findings[0].code is ScreeningCode.TITLE_SPECIALIST_IC_REVIEW
    assert complete_office.findings[1].code is ScreeningCode.LOCATION_UNRESOLVED
    assert supports_bulk_staging(complete_office, include_bounded_review=True)

    assert incomplete_worded.findings[0].code is ScreeningCode.TITLE_SPECIALIST_IC_REVIEW
    assert incomplete_worded.findings[1].code is ScreeningCode.LOCATION_UNRESOLVED
    assert supports_bulk_staging(incomplete_worded, include_bounded_review=True)


def test_marketing_candidate_waits_for_relevance_review_against_product_manager_target() -> None:
    screening = screen_discovery(
        "Example Cooperative",
        "Manager, Product Marketing",
        "Freedonia",
        home_market_search("Senior Clinical Data Product Manager"),
        ["Senior Clinical Data Product Manager"],
        published_location_facts("Freedonia"),
    )

    assert screening.standing is ScreeningStanding.UNKNOWN
    assert screening.findings[0].code is ScreeningCode.TITLE_ROLE_FAMILY_CONFLICT


def test_accounting_manager_waits_for_relevance_review_against_product_manager_target() -> None:
    screening = screen_discovery(
        "Example Cooperative",
        "Senior Manager, Technical Accounting",
        "Freedonia",
        home_market_search("Senior Clinical Data Product Manager"),
        ["Senior Clinical Data Product Manager"],
        published_location_facts("Freedonia"),
    )

    assert screening.standing is ScreeningStanding.UNKNOWN
    assert screening.findings[0].code is ScreeningCode.TITLE_ROLE_FAMILY_CONFLICT


def test_a_named_health_occupation_cannot_pass_on_a_generic_manager_word() -> None:
    screening = screen_discovery(
        "Example Health",
        "Board Certified Behavior Analyst Manager",
        "Freedonia",
        home_market_search("Product Manager"),
        ["Product Manager"],
        published_location_facts("Freedonia"),
    )

    assert screening.standing is ScreeningStanding.UNKNOWN
    assert screening.findings[0].code is ScreeningCode.TITLE_ROLE_FAMILY_CONFLICT
    assert not supports_bulk_staging(screening, include_bounded_review=True)


def test_a_compound_health_occupation_cannot_match_health_data_on_clinical_manager_words() -> None:
    screening = screen_discovery(
        "Example Health",
        "Clinical Manager BCBA Hybrid",
        "Freedonia",
        home_market_search("Senior Manager, Clinical Informatics"),
        ["Senior Manager, Clinical Informatics"],
        published_location_facts("Freedonia"),
    )

    assert screening.standing is ScreeningStanding.UNKNOWN
    assert screening.findings[0].code is ScreeningCode.TITLE_ROLE_FAMILY_CONFLICT
    assert not supports_bulk_staging(screening, include_bounded_review=True)


@pytest.mark.parametrize("title", ["Clinical Nurse Manager", "Nurse Clinical Manager"])
def test_a_nurse_manager_cannot_match_clinical_informatics(title: str) -> None:
    screening = screen_discovery(
        "Example Health",
        title,
        "Freedonia",
        home_market_search("Senior Manager, Clinical Informatics"),
        ["Senior Manager, Clinical Informatics"],
        published_location_facts("Freedonia"),
    )

    assert screening.standing is ScreeningStanding.UNKNOWN
    assert screening.findings[0].code is ScreeningCode.TITLE_ROLE_FAMILY_CONFLICT
    assert not supports_bulk_staging(screening, include_bounded_review=True)


def test_a_health_occupation_marker_does_not_block_a_same_family_target() -> None:
    screening = screen_discovery(
        "Example Health",
        "Senior Behavior Analyst Manager",
        "Freedonia",
        home_market_search("Behavior Analyst Manager"),
        ["Behavior Analyst Manager"],
        published_location_facts("Freedonia"),
    )

    assert screening.findings[0].code is ScreeningCode.TITLE_MATCH


def test_marketing_candidate_matches_a_saved_marketing_target() -> None:
    screening = screen_discovery(
        "Example Cooperative",
        "Manager, Product Marketing",
        "Freedonia",
        home_market_search("Senior Clinical Data Product Manager", "Product Marketing Manager"),
        ["Senior Clinical Data Product Manager", "Product Marketing Manager"],
        published_location_facts("Freedonia"),
    )

    assert screening.standing is ScreeningStanding.MATCH
    assert screening.findings[0].code is ScreeningCode.TITLE_MATCH
    assert screening.findings[0].subject == "Product Marketing Manager"


def test_management_and_manager_share_one_title_family_token() -> None:
    screening = screen_discovery(
        "Example Cooperative",
        "Director Product Management",
        "Freedonia",
        home_market_search("Senior Clinical Data Product Manager"),
        ["Senior Clinical Data Product Manager"],
        published_location_facts("Freedonia"),
    )

    assert screening.standing is ScreeningStanding.UNKNOWN
    assert screening.findings[0].code is ScreeningCode.TITLE_ROLE_FAMILY_REVIEW


def test_a_shipped_role_family_preserves_titles_with_different_leadership_nouns() -> None:
    screening = screen_discovery(
        "Example Cooperative",
        "Enterprise Data Platform Lead",
        "Freedonia",
        home_market_search("Clinical Data Platform Manager"),
        ["Clinical Data Platform Manager"],
        published_location_facts("Freedonia"),
    )

    assert screening.standing is ScreeningStanding.UNKNOWN
    assert screening.findings[0].code is ScreeningCode.TITLE_ROLE_FAMILY_REVIEW


def test_leadership_noun_variants_do_not_require_a_shipped_taxonomy_example() -> None:
    screening = screen_discovery(
        "Example Cooperative",
        "Enterprise Software Engineering Lead",
        "Freedonia",
        home_market_search("Clinical Software Engineering Manager"),
        ["Clinical Software Engineering Manager"],
        published_location_facts("Freedonia"),
    )

    assert screening.standing is ScreeningStanding.UNKNOWN
    assert screening.findings[0].code is ScreeningCode.TITLE_ROLE_FAMILY_REVIEW


def _title_finding_for(title: str, stated: list[str]) -> ScreeningFinding:
    return screen_discovery(
        "Example Cooperative",
        title,
        "Freedonia",
        home_market_search(*stated),
        stated,
        published_location_facts("Freedonia"),
    ).findings[0]


@pytest.mark.parametrize(
    "stated",
    [
        ["Clinical Data Platform Manager", "Data Platform Sales Representative"],
        ["Data Platform Sales Representative", "Clinical Data Platform Manager"],
    ],
)
def test_the_most_favorable_of_equally_shared_stated_titles_decides(stated: list[str]) -> None:
    finding = _title_finding_for("Enterprise Data Platform Lead", stated)

    assert finding.code is ScreeningCode.TITLE_ROLE_FAMILY_REVIEW
    assert finding.subject == "Clinical Data Platform Manager"


def test_one_distinct_shared_word_earns_neither_near_match_nor_family_review() -> None:
    finding = _title_finding_for(
        "Pharmacy Network Contracting Manager", ["Staff Product Manager, Pharmacy"]
    )

    assert finding.code not in {
        ScreeningCode.TITLE_NEAR_MATCH,
        ScreeningCode.TITLE_ROLE_FAMILY_REVIEW,
    }


@pytest.mark.parametrize(
    ("title", "stated"),
    [
        ("Clinical Data Platform Lead", "Data Platform Manager"),
        ("Pharmacy Product Lead", "Staff Product Manager, Pharmacy"),
    ],
)
def test_two_distinct_shared_words_still_near_match(title: str, stated: str) -> None:
    assert _title_finding_for(title, [stated]).code is ScreeningCode.TITLE_NEAR_MATCH


@pytest.mark.parametrize(
    "title",
    ["Life Sciences Strategy Lead", "(Senior) Director, Portfolio Strategy, Life Sciences"],
)
def test_a_higher_share_stated_title_does_not_discard_a_lower_share_review(title: str) -> None:
    stated = ["Senior Product Manager, Life Sciences Data", "MLR Strategy Lead"]

    finding = _title_finding_for(title, stated)

    assert finding.code is ScreeningCode.TITLE_ROLE_FAMILY_REVIEW
    assert finding.subject == "Senior Product Manager, Life Sciences Data"


_MONOTONIC_TITLES = (
    "Life Sciences Strategy Lead",
    "(Senior) Director, Portfolio Strategy, Life Sciences",
    "Enterprise Data Platform Lead",
    "Director Product Management",
    "Principal Logistics Analyst",
    "Senior Clinical Data Analyst",
    "Data Platform Sales Representative",
    "Manager, Product Software Engineering",
)
_MONOTONIC_TARGETS = (
    "Senior Product Manager, Life Sciences Data",
    "MLR Strategy Lead",
    "Clinical Data Platform Manager",
    "Data Platform Sales Representative",
    "Director Logistics Analytics",
    "Warehouse Associate",
    "Senior Clinical Data Product Manager",
    "Data Engineer",
    "Strategy Consultant",
)


@given(
    title=st.sampled_from(_MONOTONIC_TITLES),
    stated=st.lists(st.sampled_from(_MONOTONIC_TARGETS), min_size=1, max_size=4, unique=True),
    appended=st.sampled_from(_MONOTONIC_TARGETS),
)
@example(
    title="Life Sciences Strategy Lead",
    stated=["Senior Product Manager, Life Sciences Data"],
    appended="MLR Strategy Lead",
)
def test_appending_a_stated_title_never_drops_a_retained_title(
    title: str, stated: list[str], appended: str
) -> None:
    before = _title_finding_for(title, stated)
    after = _title_finding_for(title, [*stated, appended])

    if supports_title_expansion(before, include_bounded_review=True):
        assert supports_title_expansion(after, include_bounded_review=True)
    assert after.code is not ScreeningCode.TITLE_MISMATCH or before.code in {
        ScreeningCode.TITLE_MISMATCH,
        ScreeningCode.TITLE_ROLE_FAMILY_CONFLICT,
    }


@pytest.mark.parametrize(
    ("title", "stated"),
    [
        (
            "Enterprise Data Platform Lead",
            ["Data Platform Support Technician", "Clinical Data Platform Manager"],
        ),
        (
            "Enterprise Data Platform Lead",
            ["Clinical Data Platform Nurse", "Data Platform Account Executive"],
        ),
        (
            "Enterprise Software Engineering Lead",
            ["Clinical Software Engineering Manager", "Software Engineering Sales Representative"],
        ),
        ("Director Product Management", ["Senior Clinical Data Product Manager", "Data Engineer"]),
        ("Principal Logistics Analyst", ["Director Logistics Analytics", "Warehouse Associate"]),
    ],
)
def test_stated_title_order_never_changes_the_title_answer(title: str, stated: list[str]) -> None:
    forward = _title_finding_for(title, stated)
    backward = _title_finding_for(title, stated[::-1])

    assert (backward.code, backward.standing) == (forward.code, forward.standing)


def test_informaticist_and_informatics_are_the_same_title_family_token() -> None:
    screening = screen_discovery(
        "Example Cooperative",
        "Principal Clinical Informaticist",
        "Freedonia",
        home_market_search("Director Clinical Informatics"),
        ["Director Clinical Informatics"],
        published_location_facts("Freedonia"),
    )

    assert screening.standing is ScreeningStanding.MATCH
    assert screening.findings[0].code is ScreeningCode.TITLE_MATCH


def test_excluded_title_matches_the_management_form_of_manager() -> None:
    """An excluded 'Clinical Data Manager' refuses a 'Clinical Data Management' listing."""
    parameters = home_market_search("Senior Clinical Data Product Manager").model_copy(
        update={
            "seniority": Seniority(
                target_titles=titles("Senior Clinical Data Product Manager"),
                excluded_titles=["Clinical Data Manager"],
            ),
        }
    )
    screening = screen_discovery(
        "Example Cooperative",
        "Clinical Data Management",
        "Freedonia",
        parameters,
        ["Senior Clinical Data Product Manager"],
        published_location_facts("Freedonia"),
    )

    assert screening.standing is ScreeningStanding.REFUSE
    assert screening.findings[0].code is ScreeningCode.TITLE_EXCLUDED
    assert screening.findings[0].subject == "Clinical Data Manager"


def test_excluded_title_still_refuses_the_exact_form() -> None:
    """The token-set check still refuses the exact excluded title."""
    parameters = home_market_search("Platform Product Manager").model_copy(
        update={
            "seniority": Seniority(
                target_titles=titles("Platform Product Manager"),
                excluded_titles=["Engineering Manager"],
            ),
        }
    )
    screening = screen_discovery(
        "Example Cooperative",
        "Engineering Manager, Platform",
        "Freedonia",
        parameters,
        ["Platform Product Manager"],
        published_location_facts("Freedonia"),
    )

    assert screening.standing is ScreeningStanding.REFUSE
    assert screening.findings[0].code is ScreeningCode.TITLE_EXCLUDED
    assert screening.findings[0].subject == "Engineering Manager"


def test_excluded_title_preserves_word_order_before_refusing_a_product_role() -> None:
    """An excluded 'Engineering Manager' does not refuse a manager of a product."""
    parameters = home_market_search("Principal Product Manager").model_copy(
        update={
            "seniority": Seniority(
                target_titles=titles("Principal Product Manager"),
                excluded_titles=["Engineering Manager"],
            ),
        }
    )
    screening = screen_discovery(
        "Example Cooperative",
        "Principal Product Manager, Engineering Intelligence & Insights",
        "Freedonia",
        parameters,
        ["Principal Product Manager"],
        published_location_facts("Freedonia"),
    )

    excluded_codes = [
        finding.code
        for finding in screening.findings
        if finding.code is ScreeningCode.TITLE_EXCLUDED
    ]
    assert excluded_codes == []


def test_excluded_title_does_not_match_unrelated_words_that_happen_to_share_one_token() -> None:
    """Only listings whose meaningful tokens contain every excluded token are refused."""
    parameters = home_market_search("Senior Clinical Data Product Manager").model_copy(
        update={
            "seniority": Seniority(
                target_titles=titles("Senior Clinical Data Product Manager"),
                excluded_titles=["Sales Engineer"],
            ),
        }
    )
    screening = screen_discovery(
        "Example Cooperative",
        "Sales Development Representative",
        "Freedonia",
        parameters,
        ["Senior Clinical Data Product Manager"],
        published_location_facts("Freedonia"),
    )

    excluded_codes = [
        finding.code
        for finding in screening.findings
        if finding.code is ScreeningCode.TITLE_EXCLUDED
    ]
    assert excluded_codes == []


def test_excluded_title_does_not_span_an_unconfigured_specialty_word() -> None:
    """Fuzzy exclusions must not shadow a viable specialized occupation."""
    for listing_title in ("Engineering Team Manager", "Engineering Senior Manager"):
        parameters = home_market_search("Platform Product Manager").model_copy(
            update={
                "seniority": Seniority(
                    target_titles=titles("Platform Product Manager"),
                    excluded_titles=["Engineering Manager"],
                ),
            }
        )
        screening = screen_discovery(
            "Example Cooperative",
            listing_title,
            "Freedonia",
            parameters,
            ["Platform Product Manager"],
            published_location_facts("Freedonia"),
        )

        assert screening.standing is ScreeningStanding.UNKNOWN, listing_title
        assert screening.findings[0].code is not ScreeningCode.TITLE_EXCLUDED


def test_excluded_title_does_not_refuse_when_the_exclusion_words_are_too_far_apart() -> None:
    """The allowed gap is bounded, so distant ordered words are not an exclusion."""
    parameters = home_market_search("Platform Product Manager").model_copy(
        update={
            "seniority": Seniority(
                target_titles=titles("Platform Product Manager"),
                excluded_titles=["Engineering Manager"],
            ),
        }
    )
    screening = screen_discovery(
        "Example Cooperative",
        "Engineering Program Delivery Manager",
        "Freedonia",
        parameters,
        ["Platform Product Manager"],
        published_location_facts("Freedonia"),
    )

    excluded_codes = [
        finding.code
        for finding in screening.findings
        if finding.code is ScreeningCode.TITLE_EXCLUDED
    ]
    assert excluded_codes == []


def test_a_matching_title_and_place_pass_without_explanatory_text_parsing() -> None:
    screening = screen_discovery(
        "Example Cooperative",
        "Senior Platform Product Manager",
        "Freedonia",
        home_market_search("Platform Product Manager"),
        ["Platform Product Manager"],
        published_location_facts("Freedonia"),
    )

    assert screening.standing is ScreeningStanding.MATCH
    assert [finding.code for finding in screening.findings] == [
        ScreeningCode.TITLE_MATCH,
        ScreeningCode.LOCATION_MATCH,
    ]


def test_every_complete_structured_fact_can_match_its_configured_boundary() -> None:
    parameters = home_market_search("Platform Product Manager").model_copy(
        update={
            "seniority": Seniority(
                minimum="senior",
                maximum="director",
                target_titles=titles("Platform Product Manager"),
            ),
            "employment": Employment(types=["permanent"]),
            "arrangement": Arrangement(remote=Acceptance.PREFERRED, hybrid=Acceptance.REFUSED),
            "compensation": [
                CompensationBand(location="home", currency="FRE", minimum=Decimal(120000))
            ],
        }
    )
    facts = StagedListingFacts(
        locations=PublishedFact(values=("Freedonia",), complete=True),
        arrangements=PublishedFact(values=("Remote",), complete=True),
        employment_types=PublishedFact(values=("Permanent",), complete=True),
        compensation=PublishedFact(values=("FRE 125,000 annual",), complete=True),
    )

    screening = screen_discovery(
        "Example Cooperative",
        "Senior Platform Product Manager",
        "Freedonia",
        parameters,
        ["Platform Product Manager"],
        facts,
    )

    assert screening.standing is ScreeningStanding.MATCH
    assert {finding.code for finding in screening.findings} >= {
        ScreeningCode.SENIORITY_MATCH,
        ScreeningCode.EMPLOYMENT_MATCH,
        ScreeningCode.ARRANGEMENT_MATCH,
        ScreeningCode.COMPENSATION_MATCH,
    }


def test_publisher_employment_aliases_match_configured_categories() -> None:
    for published, configured in (
        ("FullTime", "full time"),
        ("Consultant (DEEL)", "contract"),
    ):
        parameters = home_market_search("Platform Product Manager").model_copy(
            update={"employment": Employment(types=[configured])}
        )
        facts = StagedListingFacts(
            locations=PublishedFact(values=("Freedonia",), complete=True),
            employment_types=PublishedFact(values=(published,), complete=True),
        )

        screening = screen_discovery(
            "Example Cooperative",
            "Platform Product Manager",
            "Freedonia",
            parameters,
            ["Platform Product Manager"],
            facts,
        )

        employment = next(
            finding
            for finding in screening.findings
            if finding.dimension is ScreeningDimension.EMPLOYMENT_TYPE
        )
        assert employment.standing is ScreeningStanding.MATCH
        assert employment.code is ScreeningCode.EMPLOYMENT_MATCH


def test_an_unclassified_employment_label_stays_reviewable() -> None:
    parameters = home_market_search("Platform Product Manager").model_copy(
        update={"employment": Employment(types=["full time"])}
    )
    facts = StagedListingFacts(
        locations=PublishedFact(values=("Freedonia",), complete=True),
        employment_types=PublishedFact(values=("Employee",), complete=True),
    )

    screening = screen_discovery(
        "Example Cooperative",
        "Platform Product Manager",
        "Freedonia",
        parameters,
        ["Platform Product Manager"],
        facts,
    )

    employment = next(
        finding
        for finding in screening.findings
        if finding.dimension is ScreeningDimension.EMPLOYMENT_TYPE
    )
    assert employment.standing is ScreeningStanding.UNKNOWN
    assert employment.code is ScreeningCode.EMPLOYMENT_UNKNOWN


def test_an_exact_custom_employment_label_keeps_its_free_string_contract() -> None:
    facts = StagedListingFacts(
        locations=PublishedFact(values=("Freedonia",), complete=True),
        employment_types=PublishedFact(values=("Locum",), complete=True),
    )

    matched = screen_discovery(
        "Example Cooperative",
        "Platform Product Manager",
        "Freedonia",
        home_market_search("Platform Product Manager").model_copy(
            update={"employment": Employment(types=["locum"])}
        ),
        ["Platform Product Manager"],
        facts,
    )
    excluded = screen_discovery(
        "Example Cooperative",
        "Platform Product Manager",
        "Freedonia",
        home_market_search("Platform Product Manager").model_copy(
            update={"employment": Employment(excluded_types=["locum"])}
        ),
        ["Platform Product Manager"],
        facts,
    )

    assert any(finding.code is ScreeningCode.EMPLOYMENT_MATCH for finding in matched.findings)
    assert any(finding.code is ScreeningCode.EMPLOYMENT_EXCLUDED for finding in excluded.findings)


def test_a_known_employment_category_outside_the_saved_types_is_refused() -> None:
    parameters = home_market_search("Platform Product Manager").model_copy(
        update={"employment": Employment(types=["full time"])}
    )
    facts = StagedListingFacts(
        locations=PublishedFact(values=("Freedonia",), complete=True),
        employment_types=PublishedFact(values=("Part-time",), complete=True),
    )

    screening = screen_discovery(
        "Example Cooperative",
        "Platform Product Manager",
        "Freedonia",
        parameters,
        ["Platform Product Manager"],
        facts,
    )

    employment = next(
        finding
        for finding in screening.findings
        if finding.dimension is ScreeningDimension.EMPLOYMENT_TYPE
    )
    assert employment.standing is ScreeningStanding.REFUSE
    assert employment.code is ScreeningCode.EMPLOYMENT_OUTSIDE


@pytest.mark.parametrize(
    ("stated", "employment", "standing", "code"),
    [
        pytest.param(
            ("FULL_TIME", "CONTRACTOR"),
            Employment(excluded_types=["contract"]),
            ScreeningStanding.MATCH,
            ScreeningCode.EMPLOYMENT_MATCH,
            id="one-open-type-keeps-a-mixed-posting",
        ),
        pytest.param(
            ("Contractor", "Consultant"),
            Employment(excluded_types=["contract"]),
            ScreeningStanding.REFUSE,
            ScreeningCode.EMPLOYMENT_EXCLUDED,
            id="every-type-excluded",
        ),
        pytest.param(
            ("INTERN",),
            Employment(excluded_types=["internship"]),
            ScreeningStanding.REFUSE,
            ScreeningCode.EMPLOYMENT_EXCLUDED,
            id="intern-is-an-internship",
        ),
        pytest.param(
            ("PER_DIEM", "PART_TIME"),
            Employment(types=["full time"]),
            ScreeningStanding.UNKNOWN,
            ScreeningCode.EMPLOYMENT_UNKNOWN,
            id="an-unrecognized-type-beside-an-outside-one-is-not-refused",
        ),
    ],
)
def test_a_posting_stating_several_employment_types_is_judged_on_all_of_them(
    stated: tuple[str, ...],
    employment: Employment,
    standing: ScreeningStanding,
    code: ScreeningCode,
) -> None:
    screening = screen_discovery(
        "Example Cooperative",
        "Platform Product Manager",
        "Freedonia",
        home_market_search("Platform Product Manager").model_copy(
            update={"employment": employment}
        ),
        ["Platform Product Manager"],
        StagedListingFacts(
            locations=PublishedFact(values=("Freedonia",), complete=True),
            employment_types=PublishedFact(values=stated, complete=True),
        ),
    )

    found = next(
        finding
        for finding in screening.findings
        if finding.dimension is ScreeningDimension.EMPLOYMENT_TYPE
    )
    assert (found.standing, found.code) == (standing, code)


def test_every_configured_incomplete_publisher_fact_is_explicitly_unknown() -> None:
    parameters = home_market_search("Platform Product Manager").model_copy(
        update={
            "seniority": Seniority(
                minimum="senior", target_titles=titles("Platform Product Manager")
            ),
            "employment": Employment(types=["permanent"]),
            "arrangement": Arrangement(remote=Acceptance.PREFERRED),
            "compensation": [
                CompensationBand(location="home", currency="FRE", minimum=Decimal(120000))
            ],
        }
    )

    screening = screen_discovery(
        "Example Cooperative",
        "Platform Product Manager",
        "Freedonia",
        parameters,
        ["Platform Product Manager"],
        StagedListingFacts(),
    )

    assert screening.standing is ScreeningStanding.UNKNOWN
    assert {finding.code for finding in screening.findings} >= {
        ScreeningCode.SENIORITY_UNKNOWN,
        ScreeningCode.LOCATION_UNRESOLVED,
        ScreeningCode.EMPLOYMENT_UNKNOWN,
        ScreeningCode.ARRANGEMENT_UNKNOWN,
        ScreeningCode.COMPENSATION_UNKNOWN,
    }


def test_explicit_employer_employment_arrangement_and_pay_refusals_are_typed() -> None:
    parameters = home_market_search("Platform Product Manager").model_copy(
        update={
            "employers": Employers(excluded_employers=["Example Cooperative"]),
            "employment": Employment(types=["permanent"], excluded_types=["temporary"]),
            "arrangement": Arrangement(hybrid=Acceptance.ACCEPTABLE, maximum_office_days=1),
            "compensation": [
                CompensationBand(location="home", currency="FRE", minimum=Decimal(120000))
            ],
        }
    )
    facts = StagedListingFacts(
        locations=PublishedFact(values=("Freedonia",), complete=True),
        arrangements=PublishedFact(values=("Hybrid, 3 days per week",), complete=True),
        employment_types=PublishedFact(values=("Temporary",), complete=True),
        compensation=PublishedFact(values=("FRE 90,000 - 110,000 annual",), complete=True),
    )

    screening = screen_discovery(
        "Example Cooperative",
        "Platform Product Manager",
        "Freedonia",
        parameters,
        ["Platform Product Manager"],
        facts,
    )

    assert screening.standing is ScreeningStanding.REFUSE
    assert {finding.code for finding in screening.findings} >= {
        ScreeningCode.EMPLOYER_EXCLUDED,
        ScreeningCode.EMPLOYMENT_EXCLUDED,
        ScreeningCode.ARRANGEMENT_OFFICE_LIMIT,
        ScreeningCode.COMPENSATION_BELOW_MINIMUM,
    }


def _employer_finding_on_board(
    employer: str, board_employer: str, trust: ListingIdentityTrust
) -> ScreeningFinding:
    parameters = home_market_search("Platform Product Manager").model_copy(
        update={"employers": Employers(excluded_employers=["Globex"])}
    )
    screening = screen_discovery(
        employer,
        "Platform Product Manager",
        "",
        parameters,
        ["Platform Product Manager"],
        StagedListingFacts(identity_trust=trust),
        source_employer=board_employer,
    )
    return next(
        finding
        for finding in screening.findings
        if finding.dimension is ScreeningDimension.EMPLOYER
    )


@pytest.mark.parametrize(
    "trust", [ListingIdentityTrust.UNVERIFIED_HINT, ListingIdentityTrust.PUBLISHER]
)
def test_a_board_registered_to_an_excluded_employer_refuses_whatever_the_posting_names(
    trust: ListingIdentityTrust,
) -> None:
    finding = _employer_finding_on_board("Initech", "Globex", trust)

    assert (finding.standing, finding.code, finding.subject) == (
        ScreeningStanding.REFUSE,
        ScreeningCode.EMPLOYER_EXCLUDED,
        "Globex",
    )


def test_a_slug_backfilled_title_keeps_inferred_seniority_open_for_review() -> None:
    listing = backfilled_listing(
        SourceListing(
            "Acme Health, Inc.",
            "",
            "https://job-boards.greenhouse.io/acme-health/jobs/1/junior-support-associate",
        )
    )
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="Canada")],
        seniority=Seniority(
            minimum="senior", maximum="director", target_titles=titles("Platform Product Manager")
        ),
    )
    screening = screen_discovery(
        listing.employer,
        listing.title,
        "Canada",
        parameters,
        ["Platform Product Manager"],
        listing.facts,
    )
    finding = next(f for f in screening.findings if f.dimension is ScreeningDimension.SENIORITY)

    assert listing.title == "junior support associate"
    assert (finding.standing, finding.code) == (
        ScreeningStanding.UNKNOWN,
        ScreeningCode.SENIORITY_OUTSIDE,
    )


@pytest.mark.parametrize(
    ("url", "text", "spelled"),
    [
        (
            "https://jobs.lever.co/northwindhealth/1a2b",
            "About us\nNorthwind Health builds clinic scheduling tools.",
            "Northwind Health",
        ),
        (
            "https://jobs.ashbyhq.com/examplehealthcare/9f",
            "Join the ExampleHealthCare platform team.",
            "ExampleHealthCare",
        ),
        (
            "https://job-boards.eu.greenhouse.io/examplehealthcare/jobs/4001",
            "Join the ExampleHealthCare platform team.",
            "ExampleHealthCare",
        ),
        (
            "https://jobs.lever.co/northwindhealth/1a2b",
            "You will own Northwind Health's patient intake service.",
            "Northwind Health",
        ),
        (
            "https://example-works.wd5.myworkdayjobs.com/en-US/careers/job/1",
            "EXAMPLE WORKS is hiring a platform lead.",
            "Example Works",
        ),
        (
            "https://jobs.lever.co/northwindhealth/1a2b",
            "ABOUT NORTHWIND HEALTH\nNorthwind Health builds clinic scheduling tools.",
            "Northwind Health",
        ),
        (
            "https://jobs.ashbyhq.com/examplejpworks/9f",
            "Example J.P. Works leads the clearing platform.",
            "Example J.P. Works",
        ),
    ],
)
def test_the_publisher_spelling_of_a_board_tenant_is_read_from_the_posting_text(
    url: str, text: str, spelled: str
) -> None:
    assert publisher_spelling_of_tenant(url, text) == spelled


@pytest.mark.parametrize(
    "text",
    [
        "Our work contributes to northwind health outcomes across the region.",
        "Globex Freight is hiring a platform lead.",
        "Northwind and Health are two words here, never a name.",
        "We aim to serve Northwind. Health systems benefit.",
    ],
)
def test_no_publisher_spelling_is_read_from_lowercase_or_unrelated_text(text: str) -> None:
    assert publisher_spelling_of_tenant("https://jobs.lever.co/northwindhealth/1a2b", text) is None


@pytest.mark.parametrize(
    ("url", "page_title", "spelled"),
    [
        ("https://www.northwind.test/jobs/1", "Data Lead | Northwind", "Northwind"),
        ("https://www.globex.test/jobs/1", "Data Lead - Globex", "Globex"),
        ("https://www.globex.test/jobs/1", "Data Lead \N{EN DASH} Globex", "Globex"),
        ("https://initech.healthcare/jobs/data-lead/", "Initech", "Initech"),
        (
            "https://careers.acmehealth.test/jobs/consultant-1",
            "Consultant - Acme Health Careers",
            "Acme Health",
        ),
        ("https://www.initrode.test/jobs/1", "DATA LEAD | INITRODE", "INITRODE"),
        ("https://www.initech.test/jobs/1", "INITECH jobs | Data Lead at Initech", "Initech"),
        ("https://jobs.example-works.test/1", "Data Lead at ExampleWorks", "ExampleWorks"),
        ("https://careers.globex.co.uk/1", "Data Lead | Globex", "Globex"),
    ],
)
def test_an_employer_sites_page_title_spells_its_own_name(
    url: str, page_title: str, spelled: str
) -> None:
    assert publisher_spelling_of_host(url, page_title) == spelled


@pytest.mark.parametrize(
    ("url", "page_title"),
    [
        ("https://ca.linkedin.com/jobs/view/data-lead-1", "Data Lead - Globex | LinkedIn"),
        ("https://ca.indeed.com/viewjob?jk=1a2b", "Data Lead - Globex - Indeed"),
        ("https://remoteok.com/remote-jobs/1", "Data Lead at Globex | Remote OK"),
        ("https://job-boards.greenhouse.io/globex/jobs/1", "Data Lead at Greenhouse"),
        ("https://globex.bamboohr.com/careers/1", "Data Lead | Globex BambooHR"),
        ("https://jobs.smartrecruiters.com/Globex/7431", "Data Lead | SmartRecruiters"),
        ("https://careers-globex.icims.com/jobs/1234/data-lead/job", "Data Lead | iCIMS Globex"),
        ("https://www.glassdoor.com/job-listing/data-lead-1", "Data Lead | Glassdoor"),
        ("https://weworkremotely.com/remote-jobs/globex-data-lead", "Data Lead | We Work Remotely"),
        ("https://careers.acmehealth.test/jobs/1", "Data Lead | Careers"),
        ("https://initech.healthcare/jobs/1", "Data Lead | Healthcare Jobs"),
        ("https://www.northwind.test/jobs/1", "Data Lead | northwind"),
        ("https://www.northwind.test/jobs/1", "Data Lead | Globex"),
    ],
)
def test_no_employer_is_read_from_a_job_sites_title_or_one_not_spelling_the_host(
    url: str, page_title: str
) -> None:
    assert publisher_spelling_of_host(url, page_title) is None


@pytest.mark.parametrize("board_employer", ["Initech", ""])
def test_an_unvouched_excluded_posting_employer_on_a_clean_board_stays_unverified(
    board_employer: str,
) -> None:
    finding = _employer_finding_on_board(
        "Globex", board_employer, ListingIdentityTrust.UNVERIFIED_HINT
    )

    assert (finding.standing, finding.code) == (
        ScreeningStanding.UNKNOWN,
        ScreeningCode.EMPLOYER_UNVERIFIED,
    )


def _arrangement_screening(
    arrangement: Arrangement,
    location: str,
    value: str,
    *,
    locations: list[Location] | None = None,
) -> DiscoveryScreening:
    update: dict[str, object] = {"arrangement": arrangement}
    if locations is not None:
        update["locations"] = locations
    parameters = home_market_search("Platform Product Manager").model_copy(update=update)
    return screen_discovery(
        "Freedonia Health",
        "Platform Product Manager",
        location,
        parameters,
        ["Platform Product Manager"],
        StagedListingFacts(
            locations=PublishedFact(values=(location,), complete=True),
            arrangements=PublishedFact(values=(value,), complete=True),
        ),
    )


def _arrangement_of(screening: DiscoveryScreening) -> ScreeningFinding:
    return next(
        finding
        for finding in screening.findings
        if finding.dimension is ScreeningDimension.ARRANGEMENT
    )


@pytest.mark.parametrize("value", ["Hybrid", "Hybrid, 3 days per week"])
def test_a_hybrid_tag_where_no_saved_place_accepts_hybrid_refuses(value: str) -> None:
    screening = _arrangement_screening(
        Arrangement(remote=Acceptance.PREFERRED, hybrid=Acceptance.REFUSED),
        "Harborton, Freedonia",
        value,
    )

    assert _arrangement_of(screening).code is ScreeningCode.ARRANGEMENT_REFUSED
    assert screening.standing is ScreeningStanding.REFUSE


@pytest.mark.parametrize(
    ("value", "over_limit"),
    [
        ("Hybrid (9 days/month)", True),
        ("Hybrid, 12 days per month", True),
        ("Hybrid (8 days/month)", False),
        ("Hybrid (4 days/month)", False),
        ("Hybrid, 2 days/week", True),
    ],
)
def test_a_monthly_office_cadence_is_held_to_the_weekly_office_day_limit(
    value: str, over_limit: bool
) -> None:
    screening = _arrangement_screening(
        Arrangement(hybrid=Acceptance.ACCEPTABLE, maximum_office_days=1), "Freedonia", value
    )

    exceeded = _arrangement_of(screening).code is ScreeningCode.ARRANGEMENT_OFFICE_LIMIT
    assert exceeded is over_limit


def test_bare_on_site_tag_at_an_unmapped_place_stays_unconfirmed() -> None:
    """An unrecognized place names no site, so it never refuses on its own."""
    screening = _arrangement_screening(
        Arrangement(remote=Acceptance.PREFERRED, on_site=Acceptance.REFUSED),
        "Unmapped City",
        "On-site",
    )

    assert _arrangement_of(screening).code is ScreeningCode.ARRANGEMENT_REMOTE_UNCONFIRMED


def test_home_arrangement_override_accepts_the_tag_the_global_policy_refuses() -> None:
    """A place whose own arrangement accepts hybrid matches; the soft gate never engages."""
    screening = _arrangement_screening(
        Arrangement(remote=Acceptance.PREFERRED, hybrid=Acceptance.REFUSED),
        "Harborton, Freedonia",
        "Hybrid",
        locations=[
            Location(
                key="home",
                country="Freedonia",
                arrangement=Arrangement(hybrid=Acceptance.ACCEPTABLE),
            )
        ],
    )

    assert _arrangement_of(screening).code is ScreeningCode.ARRANGEMENT_MATCH


def test_arrangement_categories_share_descriptive_multi_value_screening_semantics() -> None:
    fact = PublishedFact(values=("Hybrid, 3 days per week", "Remote within Canada"), complete=True)

    assert arrangement_categories(fact) == ("hybrid", "remote")


def test_arrangement_categories_keep_period_limits_and_unclassified_values_explicit() -> None:
    assert arrangement_categories(PublishedFact(values=("Temporary remote",), complete=True)) == (
        "period_limited",
    )
    assert arrangement_categories(PublishedFact(values=("Flexible",), complete=True)) == (
        "unknown",
    )
    assert arrangement_categories(PublishedFact()) == ("unknown",)


def test_arrangement_published_takes_precedence() -> None:
    fact = PublishedFact(values=("Remote",), complete=True)
    result, source = resolve_arrangement_categories(
        fact, "Burlington, MA, USA", "https://example.com/postings/123"
    )
    assert result == ("remote",)
    assert source == "published"


def test_arrangement_url_slug_remote_overrides_inference() -> None:
    fact = PublishedFact()
    result, source = resolve_arrangement_categories(
        fact,
        "Chandler, Arizona, United States of America",
        "https://careers.example.com/job/Remote-United-States-of-America/Foo/12345",
    )
    assert result == ("remote",)
    assert source == "inferred_from_url"


def test_arrangement_location_string_remote_overrides_inference() -> None:
    fact = PublishedFact()
    result, source = resolve_arrangement_categories(fact, "Remote, USA", "")
    assert result == ("remote",)
    assert source == "inferred_from_location"


def test_arrangement_infers_on_site_for_specific_us_city() -> None:
    fact = PublishedFact()
    result, source = resolve_arrangement_categories(
        fact, "Burlington, MA, United States of America", "https://example.com/job/12345"
    )
    assert result == ("on_site",)
    assert source == "inferred_from_location"


def test_arrangement_infers_on_site_for_workday_usa_dash_city() -> None:
    fact = PublishedFact()
    result, source = resolve_arrangement_categories(
        fact, "USA - Coppell, TX", "https://example.com/job/12345"
    )
    assert result == ("on_site",)
    assert source == "inferred_from_location"


def test_arrangement_infers_on_site_for_specific_canada_city() -> None:
    fact = PublishedFact()
    result, source = resolve_arrangement_categories(
        fact, "Toronto, Ontario, Canada", "https://example.com/job/12345"
    )
    assert result == ("on_site",)
    assert source == "inferred_from_location"


def test_arrangement_stays_unknown_for_country_only_string() -> None:
    fact = PublishedFact()
    result, source = resolve_arrangement_categories(
        fact, "United States", "https://example.com/job/12345"
    )
    assert result == ("unknown",)
    assert source == "missing"


def test_arrangement_stays_unknown_when_location_and_url_empty() -> None:
    fact = PublishedFact()
    result, source = resolve_arrangement_categories(fact, "", "")
    assert result == ("unknown",)
    assert source == "missing"


def test_arrangement_location_string_onsite_hyphen_overrides_inference() -> None:
    fact = PublishedFact()
    result, source = resolve_arrangement_categories(fact, "On-site, USA", "")
    assert result == ("on_site",)
    assert source == "inferred_from_location"


def test_arrangement_url_slug_on_site_space_after_dash_normalization() -> None:
    fact = PublishedFact()
    result, source = resolve_arrangement_categories(
        fact, "", "https://careers.example.com/job/On-Site-Career/12345"
    )
    assert result == ("on_site",)
    assert source == "inferred_from_url"


def test_arrangement_stays_unknown_when_location_says_no_country() -> None:
    fact = PublishedFact()
    result, source = resolve_arrangement_categories(
        fact, "New York, NY; Boston, MA", "https://example.com/job/12345"
    )
    assert result == ("unknown",)
    assert source == "missing"


def _holdout_facts(
    provenance: str, *values: str, unresolved: tuple[str, ...] = ()
) -> StagedListingFacts:
    return StagedListingFacts(
        locations=PublishedFact(
            values=values,
            unresolved_values=unresolved,
            provenance=provenance,
            complete=not unresolved,
        )
    )


_OUTSIDE = ScreeningFinding(
    ScreeningDimension.LOCATION, ScreeningStanding.REFUSE, ScreeningCode.LOCATION_OUTSIDE
)


def _holdout_screening(
    facts: StagedListingFacts, *findings: ScreeningFinding
) -> DiscoveryScreening:
    return DiscoveryScreening(
        findings=findings or (_OUTSIDE,), location_fact_complete=facts.locations.complete
    )


def test_a_complete_exhaustive_foreign_location_is_held_out() -> None:
    facts = _holdout_facts("workday.location", "Germany")
    parameters = JobSearchParameters(locations=[Location(key="us", country="United States")])
    assert structured_location_holdout(facts, "Germany", _holdout_screening(facts), parameters)


def test_no_saved_location_holds_nothing_out() -> None:
    facts = _holdout_facts("workday.location", "New York, NY, United States")
    parameters = JobSearchParameters()
    assert not structured_location_holdout(
        facts, "New York, NY, United States", _holdout_screening(facts), parameters
    )


def test_a_foreign_location_without_a_location_outside_finding_is_not_held_out() -> None:
    facts = _holdout_facts("workday.location", "Germany")
    parameters = JobSearchParameters(locations=[Location(key="us", country="United States")])
    unresolved = ScreeningFinding(
        ScreeningDimension.LOCATION, ScreeningStanding.UNKNOWN, ScreeningCode.LOCATION_UNRESOLVED
    )
    assert not structured_location_holdout(
        facts, "Germany", _holdout_screening(facts, unresolved), parameters
    )


def test_a_lever_multi_location_posting_is_not_held_out_when_one_value_matches_a_saved_place() -> (
    None
):
    facts = _holdout_facts("lever.location", "Mexico City", "Toronto")
    parameters = JobSearchParameters(
        locations=[Location(key="toronto", country="Canada", region="Ontario", metro="Toronto")]
    )
    assert not structured_location_holdout(facts, "Toronto", _holdout_screening(facts), parameters)


def test_a_domestic_city_with_a_state_code_is_not_held_out_for_a_saved_united_states_search() -> (
    None
):
    facts = _holdout_facts("greenhouse.location", "Jersey City, NJ")
    parameters = JobSearchParameters(locations=[Location(key="us", country="United States")])
    assert not structured_location_holdout(
        facts, "Jersey City, NJ", _holdout_screening(facts), parameters
    )


@pytest.mark.parametrize(
    ("value", "saved_country"),
    [
        ("Toronto, CA", "Canada"),
        ("Pune, IN", "India"),
        ("Munich, DE", "Germany"),
        ("Nashville, TN", "United States"),
        ("Jamaica Plain, MA", "United States"),
        ("Lebanon, TN", "United States"),
        ("San Juan, Puerto Rico", "United States"),
        ("Charlotte Amalie, U.S. Virgin Islands", "United States"),
        ("Saint-Denis, R\u00e9union", "France"),
        ("Belfast, Northern Ireland", "United Kingdom"),
        ("Fort-de-France, Martinique", "France"),
    ],
)
def test_a_subdivision_code_or_territory_is_never_held_out(value: str, saved_country: str) -> None:
    facts = _holdout_facts("greenhouse.location", value)
    parameters = JobSearchParameters(locations=[Location(key="k", country=saved_country)])
    assert not structured_location_holdout(facts, value, _holdout_screening(facts), parameters)


@pytest.mark.parametrize(
    "location", ["Jersey City, nj", "Stra\N{LATIN SMALL LETTER SHARP S}e 5, Mexico, MO"]
)
def test_a_trailing_state_code_the_reader_cannot_verify_is_never_held_out(location: str) -> None:
    parameters = _one_country("us", "United States")
    screening = _screen_published(parameters, location)
    facts = _holdout_facts("greenhouse.location", location)
    assert not structured_location_holdout(facts, location, screening, parameters)


def test_a_bare_domestic_city_carrying_a_country_word_is_not_held_out() -> None:
    facts = _holdout_facts("greenhouse.location", "Jersey City")
    parameters = JobSearchParameters(locations=[Location(key="us", country="United States")])
    assert not structured_location_holdout(
        facts, "Jersey City", _holdout_screening(facts), parameters
    )


@pytest.mark.parametrize(
    "location", ["us/canada", "Canada / us", "Remote, us & canada", "Canada or us"]
)
def test_a_lowercase_saved_code_listed_beside_a_foreign_country_is_not_held_out(
    location: str,
) -> None:
    facts = _holdout_facts("greenhouse.location", location)
    parameters = JobSearchParameters(locations=[Location(key="us", country="United States")])
    assert not structured_location_holdout(facts, location, _holdout_screening(facts), parameters)


@pytest.mark.parametrize(
    "values",
    [
        ("Hyderabad - TS - IN", "India"),
        ("Pune - MH - IN", "India"),
        ("Moscow (City)", "Russian Federation"),
        ("Kiryat Ono, IL", "Israel"),
    ],
)
def test_a_value_its_sibling_places_in_an_unsaved_country_is_held_out(
    values: tuple[str, ...],
) -> None:
    facts = _holdout_facts("workday.location", *values)
    parameters = _hybrid_holdout_parameters()
    assert structured_location_holdout(facts, values[0], _holdout_screening(facts), parameters)


@pytest.mark.parametrize(
    "values",
    [
        ("Carmel, IN", "United States"),
        ("Kiryat Ono, IL",),
        ("Hyderabad - TS - IN", "Germany"),
        ("Southport, PA", "Vantage, Ruritania"),
        ("Toronto, CA", "Canada"),
        ("Hyderabad - TS - IN", "India", "Canada"),
        *_UNKNOWN_TOWN_BESIDE_ITS_CODE_COUNTRY,
    ],
)
def test_a_value_no_sibling_places_abroad_is_never_held_out(values: tuple[str, ...]) -> None:
    facts = _holdout_facts("workday.location", *values)
    parameters = _hybrid_holdout_parameters()
    assert not structured_location_holdout(facts, values[0], _holdout_screening(facts), parameters)


def test_a_lowercase_country_code_in_the_place_slot_is_still_held_out() -> None:
    facts = _holdout_facts("greenhouse.location", "Remote, Can")
    parameters = JobSearchParameters(locations=[Location(key="us", country="United States")])
    assert structured_location_holdout(facts, "Remote, Can", _holdout_screening(facts), parameters)


def test_a_foreign_region_sharing_a_domestic_town_name_is_still_held_out() -> None:
    facts = _holdout_facts("greenhouse.location", "Rotterdam, South Holland")
    parameters = JobSearchParameters(locations=[Location(key="us", country="United States")])
    assert structured_location_holdout(
        facts, "Rotterdam, South Holland", _holdout_screening(facts), parameters
    )


def test_a_crown_dependency_is_still_held_out_for_a_saved_united_kingdom_search() -> None:
    facts = _holdout_facts("greenhouse.location", "St Helier, Jersey")
    parameters = JobSearchParameters(locations=[Location(key="uk", country="United Kingdom")])
    assert structured_location_holdout(
        facts, "St Helier, Jersey", _holdout_screening(facts), parameters
    )


def test_a_publisher_region_is_not_held_out_for_a_saved_germany_search() -> None:
    facts = _holdout_facts("ashby.location", "Remote, EU")
    parameters = JobSearchParameters(locations=[Location(key="de", country="Germany")])
    assert not structured_location_holdout(
        facts, "Remote, EU", _holdout_screening(facts), parameters
    )


def test_generic_us_remote_is_not_held_out_for_a_canada_only_search() -> None:
    facts = _holdout_facts("workday.location", "Remote - United States")
    parameters = JobSearchParameters(locations=[Location(key="ca", country="Canada")])
    assert not structured_location_holdout(
        facts, "Remote - United States", _holdout_screening(facts), parameters
    )


def test_a_saved_usa_normalizes_to_united_states_so_a_matching_value_is_not_held_out() -> None:
    facts = _holdout_facts("workday.location", "New York, NY, United States")
    parameters = JobSearchParameters(locations=[Location(key="us", country="USA")])
    assert not structured_location_holdout(
        facts, "New York, NY, United States", _holdout_screening(facts), parameters
    )


def test_a_non_exhaustive_provider_is_never_held_out() -> None:
    facts = _holdout_facts("icims.location", "Germany")
    parameters = JobSearchParameters(locations=[Location(key="us", country="United States")])
    assert not structured_location_holdout(facts, "Germany", _holdout_screening(facts), parameters)


def test_an_incomplete_location_fact_is_never_held_out() -> None:
    facts = StagedListingFacts(
        locations=PublishedFact(values=("Germany",), provenance="workday.location", complete=False)
    )
    parameters = JobSearchParameters(locations=[Location(key="us", country="United States")])
    assert not structured_location_holdout(facts, "Germany", _holdout_screening(facts), parameters)


@pytest.mark.parametrize(("key", "country"), [("ca", "Canada"), ("us", "United States")])
def test_one_office_advertised_across_countries_reads_as_unknown_and_is_not_held_out(
    key: str, country: str
) -> None:
    places = ("Riverton, ON, Canada", "Riverton, NY, United States", "Riverton, OH, United States")
    facts = StagedListingFacts(
        locations=PublishedFact(values=places, provenance="dayforce.location", complete=False)
    )
    parameters = _one_country(key, country)
    location = "; ".join(places)

    screening = screen_discovery(
        "Acme",
        "Platform Product Manager",
        location,
        parameters,
        ["Platform Product Manager"],
        facts,
    )

    assert screening.findings[1].dimension is ScreeningDimension.LOCATION
    assert screening.findings[1].standing is ScreeningStanding.UNKNOWN
    assert not structured_location_holdout(facts, location, screening, parameters)


def test_an_unresolved_location_value_is_never_held_out() -> None:
    facts = _holdout_facts("workday.location", "3 Locations", unresolved=("3 Locations",))
    parameters = JobSearchParameters(locations=[Location(key="us", country="United States")])
    assert not structured_location_holdout(
        facts, "3 Locations", _holdout_screening(facts), parameters
    )


def test_an_unverified_hint_is_never_held_out() -> None:
    facts = StagedListingFacts(
        identity_trust=ListingIdentityTrust.UNVERIFIED_HINT,
        locations=PublishedFact(values=("Germany",), provenance="workday.location", complete=True),
    )
    parameters = JobSearchParameters(locations=[Location(key="us", country="United States")])
    assert not structured_location_holdout(facts, "Germany", _holdout_screening(facts), parameters)


def _unvouched_screening(title: str) -> DiscoveryScreening:
    parameters = home_market_search("Senior Clinical Data Product Manager").model_copy(
        update={
            "seniority": Seniority(
                target_titles=titles("Senior Clinical Data Product Manager"),
                excluded_titles=["Clinical Data Manager"],
            ),
        }
    )
    return screen_discovery(
        "Example Cooperative",
        title,
        "Freedonia",
        parameters,
        ["Senior Clinical Data Product Manager"],
        StagedListingFacts(identity_trust=ListingIdentityTrust.UNVERIFIED_HINT),
    )


def test_a_title_nobody_vouched_for_still_admits_a_matching_row() -> None:
    screening = _unvouched_screening("Senior Clinical Data Product Manager")

    assert screening.findings[0].standing is ScreeningStanding.MATCH
    assert supports_bulk_staging(screening, include_bounded_review=True)


def test_a_title_nobody_vouched_for_never_refuses_a_row() -> None:
    screening = _unvouched_screening("Clinical Data Management")

    assert screening.standing is not ScreeningStanding.REFUSE
    assert screening.findings[0].code is ScreeningCode.TITLE_UNVERIFIED


def _one_country(
    key: str, country: str, eligibility: WorkEligibility = WorkEligibility.AUTHORIZED
) -> JobSearchParameters:
    return JobSearchParameters(
        locations=[Location(key=key, country=country, eligibility=eligibility)]
    )


def _screen_published(parameters: JobSearchParameters, location: str) -> DiscoveryScreening:
    return screen_discovery(
        "Acme",
        "Platform Product Manager",
        location,
        parameters,
        ["Platform Product Manager"],
        _holdout_facts("greenhouse.location", location),
    )


@pytest.mark.parametrize(
    "location", ["Lebanon, NH", "Peru, IN", "Jersey Village, TX", "Scotland, CT"]
)
def test_a_town_named_like_a_country_under_a_state_code_is_never_refused(location: str) -> None:
    screening = _screen_published(_one_country("us", "United States"), location)
    assert ScreeningCode.LOCATION_OUTSIDE not in {finding.code for finding in screening.findings}


@pytest.mark.parametrize("location", ["Sunnyvale, CA", "Jamaica Plain, MA", "Lebanon, TN"])
def test_a_verified_city_and_state_code_matches_a_saved_united_states_place(location: str) -> None:
    finding = _screen_published(_one_country("us", "United States"), location).findings[1]
    assert finding.code is ScreeningCode.LOCATION_MATCH


@pytest.mark.parametrize("location", ["Greenwich, CT", "Sunderland, MA"])
def test_a_united_states_town_only_a_foreign_city_shares_never_refuses(location: str) -> None:
    finding = _screen_published(_one_country("us", "United States"), location).findings[1]
    assert finding.code in {ScreeningCode.LOCATION_MATCH, ScreeningCode.LOCATION_UNRESOLVED}


@pytest.mark.parametrize("location", ["Ontario, CA", "Richmond, CA", "Toronto, CA"])
def test_a_country_code_that_is_also_a_state_code_never_refuses_or_holds_out_canada(
    location: str,
) -> None:
    parameters = _one_country("ca", "Canada")
    screening = _screen_published(parameters, location)
    facts = _holdout_facts("greenhouse.location", location)
    assert screening.findings[1].standing is not ScreeningStanding.REFUSE
    assert not structured_location_holdout(facts, location, _holdout_screening(facts), parameters)


def test_a_bare_city_never_matches_an_ineligible_saved_country() -> None:
    parameters = _one_country("uk", "United Kingdom", eligibility=WorkEligibility.INELIGIBLE)
    finding = _screen_published(parameters, "London").findings[1]
    assert finding.standing is not ScreeningStanding.REFUSE


def test_a_saved_country_s_county_known_only_as_a_foreign_city_is_refused() -> None:
    """The gazetteer knows Surrey only as a Canadian city; this is the limit."""
    finding = _screen_published(_one_country("uk", "United Kingdom"), "Surrey").findings[1]
    assert finding.code is ScreeningCode.LOCATION_OUTSIDE
    assert finding.subject == "Canada"


def test_a_bare_city_suggesting_a_saved_country_stops_a_refusal() -> None:
    finding = _screen_published(
        _one_country("us", "United States"), "Boston, Lincolnshire, England"
    ).findings[1]
    assert finding.code is ScreeningCode.LOCATION_UNRESOLVED


@pytest.mark.parametrize(
    ("location", "country"),
    [
        ("Lyon", "France"),
        ("Toulouse, Occitanie", "France"),
        ("Lyon (Remote)", "France"),
        ("Hybrid - Lyon", "France"),
    ],
)
def test_a_city_placed_abroad_only_by_inference_is_outside_a_saved_country(
    location: str, country: str
) -> None:
    finding = _screen_published(_one_country("us", "United States"), location).findings[1]
    assert finding.code is ScreeningCode.LOCATION_OUTSIDE
    assert finding.subject == country


@pytest.mark.parametrize(
    "values",
    [("Lyon", "Austin"), ("Lyon", "Remote - United States"), ("Coventry",), ("TX - Lyon",)],
)
def test_an_inferred_foreign_city_beside_a_saved_country_or_no_country_is_not_outside(
    values: tuple[str, ...],
) -> None:
    screening = screen_discovery(
        "Acme",
        "Platform Product Manager",
        values[0],
        _one_country("us", "United States"),
        ["Platform Product Manager"],
        StagedListingFacts(
            locations=PublishedFact(values=values, provenance="greenhouse.location", complete=True)
        ),
    )
    assert screening.findings[1].code is not ScreeningCode.LOCATION_OUTSIDE


@pytest.mark.parametrize(
    ("location", "country"),
    [
        ("Perth - WA", "Australia"),
        ("WA - Perth", "Australia"),
        ("Chennai - TN", "India"),
        ("Coimbatore, TN", "India"),
        ("Bhubaneswar - OR", "India"),
        ("Raipur - CT", "India"),
        ("Florianopolis - SC", "Brazil"),
        ("Belem - PA", "Brazil"),
        ("Milan - MI", "Italy"),
        ("Palermo, PA", "Italy"),
        ("Como, CO", "Italy"),
        ("Trento, TN", "Italy"),
        ("Lagos - LA", "Nigeria"),
    ],
)
def test_a_foreign_subdivision_code_beside_its_city_is_not_outside_that_saved_country(
    location: str, country: str
) -> None:
    parameters = _one_country("home", country)
    assert parameters.place_reader.read(location).outside({country.casefold()}) == ()
    finding = _screen_published(parameters, location).findings[1]
    assert finding.code is not ScreeningCode.LOCATION_OUTSIDE


def test_a_verified_foreign_city_and_province_is_held_out() -> None:
    facts = _holdout_facts("greenhouse.location", "Surrey, BC, Canada")
    parameters = _one_country("uk", "United Kingdom")
    assert structured_location_holdout(facts, "Surrey, BC", _holdout_screening(facts), parameters)


def _hybrid_holdout_parameters() -> JobSearchParameters:
    """Austin accepts hybrid/on-site; the general block and both remote-only locations refuse."""
    refused = Arrangement(
        remote=Acceptance.PREFERRED,
        hybrid=Acceptance.REFUSED,
        on_site=Acceptance.REFUSED,
        maximum_office_days=0,
    )
    return JobSearchParameters(
        locations=[
            Location(
                key="austin",
                country="United States",
                region="Texas",
                metro="Austin",
                arrangement=Arrangement(
                    remote=Acceptance.PREFERRED,
                    hybrid=Acceptance.ACCEPTABLE,
                    on_site=Acceptance.ACCEPTABLE,
                    maximum_office_days=5,
                ),
            ),
            Location(key="canada_remote", country="Canada", arrangement=refused),
            Location(key="us_remote", country="United States", arrangement=refused),
        ],
        countries=[CountryNames(country="Canada"), CountryNames(country="United States")],
        arrangement=Arrangement(
            remote=Acceptance.PREFERRED,
            hybrid=Acceptance.REFUSED,
            on_site=Acceptance.REFUSED,
            maximum_travel_percent=10,
            relocation=False,
        ),
    )


def _hybrid_facts(platform: str, *locations: str) -> StagedListingFacts:
    return StagedListingFacts(
        locations=PublishedFact(values=locations, provenance=f"{platform}.location", complete=True),
        arrangements=PublishedFact(
            values=("hybrid",), provenance=f"{platform}.arrangement", complete=True
        ),
    )


def test_precise_hybrid_new_york_refuses_arrangement_and_is_held_out() -> None:
    parameters = _hybrid_holdout_parameters()
    facts = _hybrid_facts("lever", "New York, NY")
    screening = screen_discovery("Acme", "Data Engineer", "New York, NY", parameters, [], facts)
    finding = next(
        item for item in screening.findings if item.dimension is ScreeningDimension.ARRANGEMENT
    )
    assert finding.standing is ScreeningStanding.REFUSE
    assert finding.code is ScreeningCode.ARRANGEMENT_REFUSED
    assert finding.subject == "us_remote"
    assert structured_arrangement_holdout(
        facts, "New York, NY", screening, parameters, "This role is hybrid, based in NYC."
    )
    holdout = structured_holdout(
        facts, "New York, NY", screening, parameters, "This role is hybrid, based in NYC."
    )
    assert holdout is not None
    assert holdout.finding == finding
    assert holdout.screening == screening


def test_precise_hybrid_winnipeg_is_held_out() -> None:
    parameters = _hybrid_holdout_parameters()
    facts = _hybrid_facts("ashby", "Winnipeg, Manitoba")
    screening = screen_discovery(
        "Acme", "Data Engineer", "Winnipeg, Manitoba", parameters, [], facts
    )
    assert structured_arrangement_holdout(
        facts, "Winnipeg, Manitoba", screening, parameters, "This is a hybrid role."
    )


_ARRANGEMENT_REFUSED = ScreeningFinding(
    ScreeningDimension.ARRANGEMENT, ScreeningStanding.REFUSE, ScreeningCode.ARRANGEMENT_REFUSED
)


@pytest.mark.parametrize(
    "findings", [(_OUTSIDE, _ARRANGEMENT_REFUSED), (_ARRANGEMENT_REFUSED, _OUTSIDE)]
)
def test_a_listing_both_rules_hold_out_is_reported_as_the_location_reason(
    findings: tuple[ScreeningFinding, ...],
) -> None:
    parameters = _hybrid_holdout_parameters()
    facts = _hybrid_facts("lever", "Berlin, Germany")
    screening = _holdout_screening(facts, *findings)
    body = "This role is hybrid, based in Berlin."
    assert structured_arrangement_holdout(facts, "Berlin, Germany", screening, parameters, body)
    holdout = structured_holdout(facts, "Berlin, Germany", screening, parameters, body)
    assert holdout is not None
    assert holdout.finding == _OUTSIDE


def test_a_location_finding_whose_rule_does_not_hold_leaves_the_arrangement_reason() -> None:
    parameters = _hybrid_holdout_parameters()
    facts = _hybrid_facts("lever", "New York, NY")
    screening = _holdout_screening(facts, _OUTSIDE, _ARRANGEMENT_REFUSED)
    body = "This role is hybrid, based in NYC."
    holdout = structured_holdout(facts, "New York, NY", screening, parameters, body)
    assert holdout is not None
    assert holdout.finding == _ARRANGEMENT_REFUSED


def test_a_listing_neither_rule_holds_out_has_no_holdout() -> None:
    parameters = _hybrid_holdout_parameters()
    facts = _hybrid_facts("lever", "New York, NY")
    screening = _holdout_screening(facts, _OUTSIDE, _ARRANGEMENT_REFUSED)
    assert structured_holdout(facts, "New York, NY", screening, parameters, "") is None


def test_a_remote_place_alongside_a_precise_one_abandons_the_refusal() -> None:
    """Any raw location value naming remote voids the whole read, not just its own entry."""
    parameters = _hybrid_holdout_parameters()
    facts = _hybrid_facts("greenhouse", "New York, NY", "Remote - US")
    screening = screen_discovery("Acme", "Data Engineer", "New York, NY", parameters, [], facts)
    finding = next(
        item for item in screening.findings if item.dimension is ScreeningDimension.ARRANGEMENT
    )
    assert finding.standing is not ScreeningStanding.REFUSE


def test_an_uncorroborated_bare_city_outside_the_accepting_region_refuses() -> None:
    """Without a corroborating country the refusal names the arrangement, not a saved key."""
    parameters = _hybrid_holdout_parameters()
    facts = _hybrid_facts("lever", "Boston")
    screening = screen_discovery("Acme", "Data Engineer", "Boston", parameters, [], facts)
    finding = next(
        item for item in screening.findings if item.dimension is ScreeningDimension.ARRANGEMENT
    )
    assert finding.standing is ScreeningStanding.REFUSE
    assert finding.subject == "hybrid"


def test_places_refusing_trusts_a_bare_city_corroborated_by_an_explicit_country() -> None:
    """The same bare city refuses once a sibling signal explicitly names its country."""
    parameters = _hybrid_holdout_parameters()
    facts = _hybrid_facts("lever", "Boston", "United States")
    screening = screen_discovery(
        "Acme", "Data Engineer", "Boston, United States", parameters, [], facts
    )
    finding = next(
        item for item in screening.findings if item.dimension is ScreeningDimension.ARRANGEMENT
    )
    assert finding.standing is ScreeningStanding.REFUSE
    assert finding.subject == "us_remote"


def test_places_refusing_softens_for_a_region_less_saved_metro() -> None:
    """A saved metro with no stated region still counts as nearby for a same-country city."""
    accepting = Arrangement(
        remote=Acceptance.PREFERRED, hybrid=Acceptance.ACCEPTABLE, on_site=Acceptance.ACCEPTABLE
    )
    refused = Arrangement(
        remote=Acceptance.PREFERRED,
        hybrid=Acceptance.REFUSED,
        on_site=Acceptance.REFUSED,
        maximum_office_days=0,
    )
    parameters = JobSearchParameters(
        locations=[
            Location(
                key="austin_metro", country="United States", metro="Austin", arrangement=accepting
            ),
            Location(key="us_general", country="United States", arrangement=refused),
        ],
        countries=[CountryNames(country="United States")],
        arrangement=refused,
    )
    facts = _hybrid_facts("lever", "Round Rock, Texas")
    screening = screen_discovery(
        "Acme", "Data Engineer", "Round Rock, Texas", parameters, [], facts
    )
    finding = next(
        item for item in screening.findings if item.dimension is ScreeningDimension.ARRANGEMENT
    )
    assert finding.standing is not ScreeningStanding.REFUSE


def test_coarse_hybrid_canada_refuses_when_no_canadian_place_accepts_hybrid() -> None:
    parameters = _hybrid_holdout_parameters()
    facts = _hybrid_facts("ashby", "Canada")
    screening = screen_discovery("Acme", "Data Engineer", "Canada", parameters, [], facts)
    finding = next(
        item for item in screening.findings if item.dimension is ScreeningDimension.ARRANGEMENT
    )
    assert finding.code is ScreeningCode.ARRANGEMENT_REFUSED


def test_coarse_hybrid_texas_is_not_held_out() -> None:
    parameters = _hybrid_holdout_parameters()
    facts = _hybrid_facts("ashby", "Texas")
    screening = screen_discovery("Acme", "Data Engineer", "Texas", parameters, [], facts)
    assert not structured_arrangement_holdout(
        facts, "Texas", screening, parameters, "This is a hybrid role."
    )


def test_austin_hybrid_without_cadence_stays_unknown_and_not_held_out() -> None:
    parameters = _hybrid_holdout_parameters()
    facts = _hybrid_facts("lever", "Austin, Texas")
    screening = screen_discovery("Acme", "Data Engineer", "Austin, Texas", parameters, [], facts)
    finding = next(
        item for item in screening.findings if item.dimension is ScreeningDimension.ARRANGEMENT
    )
    assert finding.standing is ScreeningStanding.UNKNOWN
    assert not structured_arrangement_holdout(
        facts, "Austin, Texas", screening, parameters, "This is a hybrid role."
    )


def test_body_phrase_hybrid_at_a_refusing_place_is_held_out() -> None:
    """A body-inferred arrangement holds a listing out exactly as a structured one does."""
    parameters = _hybrid_holdout_parameters()
    facts = StagedListingFacts(
        locations=PublishedFact(
            values=("New York, NY",), provenance="workday.location", complete=True
        ),
        arrangements=PublishedFact(
            values=("hybrid",), provenance="workday.body_phrase.arrangement", complete=True
        ),
    )
    screening = screen_discovery("Acme", "Data Engineer", "New York, NY", parameters, [], facts)
    finding = next(
        item for item in screening.findings if item.dimension is ScreeningDimension.ARRANGEMENT
    )
    assert finding.standing is ScreeningStanding.REFUSE
    assert finding.code is ScreeningCode.ARRANGEMENT_REFUSED
    assert structured_arrangement_holdout(
        facts, "New York, NY", screening, parameters, "This role is hybrid, based in NYC."
    )


def test_body_phrase_hybrid_with_a_remote_offer_is_not_held_out() -> None:
    """A remote offer in the body still blocks the hold-out for a body-inferred arrangement."""
    parameters = _hybrid_holdout_parameters()
    facts = StagedListingFacts(
        locations=PublishedFact(
            values=("New York, NY",), provenance="workday.location", complete=True
        ),
        arrangements=PublishedFact(
            values=("hybrid",), provenance="workday.body_phrase.arrangement", complete=True
        ),
    )
    screening = screen_discovery("Acme", "Data Engineer", "New York, NY", parameters, [], facts)
    assert not structured_arrangement_holdout(
        facts,
        "New York, NY",
        screening,
        parameters,
        "This role is hybrid, based in NYC. #LI-remote",
    )


def test_a_remote_offer_in_the_body_blocks_the_arrangement_holdout() -> None:
    parameters = _hybrid_holdout_parameters()
    facts = _hybrid_facts("lever", "New York, NY")
    screening = screen_discovery("Acme", "Data Engineer", "New York, NY", parameters, [], facts)
    assert not structured_arrangement_holdout(
        facts, "New York, NY", screening, parameters, "This role is hybrid. #LI-remote"
    )


def test_a_conditional_remote_offer_in_the_body_blocks_the_arrangement_holdout() -> None:
    parameters = _hybrid_holdout_parameters()
    facts = _hybrid_facts("workday", "New York, NY")
    screening = screen_discovery("Acme", "Product Manager", "New York, NY", parameters, [], facts)
    body = "Open to remote if not aligned to office location with a strong preference East Coast."
    assert not structured_arrangement_holdout(facts, "New York, NY", screening, parameters, body)


def test_an_empty_body_never_holds_out_a_structured_arrangement() -> None:
    """A Workday sweep row carries no description, so the offer cannot be ruled out."""
    parameters = _hybrid_holdout_parameters()
    facts = _hybrid_facts("workday", "New York, NY")
    screening = screen_discovery("Acme", "Data Engineer", "New York, NY", parameters, [], facts)
    assert not structured_arrangement_holdout(facts, "New York, NY", screening, parameters, "")


def _prairie_parameters() -> JobSearchParameters:
    """Regina accepts hybrid/on-site; the Canada and US country markets are remote-only."""
    parameters = _hybrid_holdout_parameters()
    regina = Location(
        key="regina",
        country="Canada",
        region="Saskatchewan",
        metro="Regina",
        arrangement=Arrangement(
            remote=Acceptance.PREFERRED, hybrid=Acceptance.ACCEPTABLE, on_site=Acceptance.ACCEPTABLE
        ),
    )
    return parameters.model_copy(update={"locations": [regina, *parameters.locations[1:]]})


def _site_facts(
    *locations: str, arrangements: tuple[str, ...] = (), unresolved: tuple[str, ...] = ()
) -> StagedListingFacts:
    return StagedListingFacts(
        locations=PublishedFact(values=locations, provenance="workday.location", complete=True),
        arrangements=PublishedFact(
            values=arrangements,
            unresolved_values=unresolved,
            provenance="workday.arrangement",
            complete=bool(arrangements) and not unresolved,
        ),
    )


def _arrangement_held_out(
    parameters: JobSearchParameters, facts: StagedListingFacts, body: str
) -> bool:
    location = facts.locations.values[0] if facts.locations.values else ""
    screening = screen_discovery("Acme", "Data Engineer", location, parameters, [], facts)
    holdout = structured_holdout(facts, location, screening, parameters, body)
    return holdout is not None and holdout.finding.dimension is ScreeningDimension.ARRANGEMENT


_OFFICE_BODY = "You will join the data platform team at our downtown office."


@pytest.mark.parametrize(
    "facts",
    [
        pytest.param(_site_facts("Denver, Colorado", "United States"), id="us-city-no-arrangement"),
        pytest.param(
            _site_facts(
                "Brambleford, Vermont, United States of America",
                "United States of America",
                arrangements=("Hybrid Work",),
            ),
            id="structured-hybrid-at-an-unindexed-us-town",
        ),
        pytest.param(
            _site_facts("London, United Kingdom", "Denver, Colorado, United States"),
            id="uk-office-and-one-us-office",
        ),
        pytest.param(_site_facts("Winnipeg, Manitoba", "Canada"), id="canadian-city"),
        pytest.param(
            _site_facts(
                "Denver, Colorado",
                unresolved=("Hybrid Work", "publisher body offers remote: 2 days remote a week."),
            ),
            id="stored-body-note-the-current-reader-rejects",
        ),
    ],
)
def test_an_office_row_at_a_remote_only_market_with_no_remote_evidence_is_held_out(
    facts: StagedListingFacts,
) -> None:
    """On-site or hybrid work at a remote-only saved market never reaches Pending."""
    assert _arrangement_held_out(_prairie_parameters(), facts, _OFFICE_BODY)


@pytest.mark.parametrize("junk", ["Globex Holdings, Inc", "SF - Hybrid"])
def test_a_value_naming_no_place_does_not_cancel_the_remote_only_holdout(junk: str) -> None:
    facts = _site_facts("Denver, Colorado", "United States", junk)
    assert _arrangement_held_out(_prairie_parameters(), facts, _OFFICE_BODY)


@pytest.mark.parametrize(
    "summary", ["Multiple Locations", "Various Locations", "2 Locations", "Several locations"]
)
def test_a_value_saying_the_list_is_incomplete_cancels_the_remote_only_holdout(
    summary: str,
) -> None:
    """The unlisted places may include the saved on-site market."""
    facts = _site_facts("Denver, Colorado", "United States", summary)
    assert not _arrangement_held_out(_prairie_parameters(), facts, _OFFICE_BODY)


def test_a_locality_with_no_trusted_country_still_cancels_the_remote_only_holdout() -> None:
    """A bare "Regina" may be the saved market's own site."""
    facts = _site_facts("Denver, Colorado", "United States", "Regina")
    assert not _arrangement_held_out(_prairie_parameters(), facts, _OFFICE_BODY)


def test_a_row_whose_values_name_no_place_is_not_held_out_as_remote_only() -> None:
    facts = _site_facts("Globex Holdings, Inc", "SF - Hybrid")
    assert not _arrangement_held_out(_prairie_parameters(), facts, _OFFICE_BODY)


def test_a_structured_hybrid_field_with_a_silent_body_still_refuses() -> None:
    parameters = _hybrid_holdout_parameters()
    facts = _hybrid_facts("lever", "New York, NY")
    screening = screen_discovery("Acme", "Data Engineer", "New York, NY", parameters, [], facts)
    assert structured_arrangement_holdout(
        facts, "New York, NY", screening, parameters, _OFFICE_BODY
    )


def test_an_unconfirmed_arrangement_holdout_records_the_inference_beside_the_open_finding() -> None:
    """Nothing published refuses the arrangement, so the recorded reason names the inference."""
    parameters = _prairie_parameters()
    facts = _site_facts("Denver, Colorado", "United States")
    screening = screen_discovery("Acme", "Data Engineer", "Denver, Colorado", parameters, [], facts)
    holdout = structured_holdout(facts, "Denver, Colorado", screening, parameters, _OFFICE_BODY)
    inferred = ScreeningFinding(
        ScreeningDimension.ARRANGEMENT,
        ScreeningStanding.REFUSE,
        ScreeningCode.ARRANGEMENT_REMOTE_ONLY_SITES,
    )
    assert holdout is not None
    assert holdout.finding == inferred
    assert holdout.screening.standing is ScreeningStanding.REFUSE
    expected = []
    for item in screening.findings:
        expected.append(item)
        if item.dimension is ScreeningDimension.ARRANGEMENT:
            assert item.code in {
                ScreeningCode.ARRANGEMENT_UNKNOWN,
                ScreeningCode.ARRANGEMENT_REMOTE_UNCONFIRMED,
            }
            expected.append(inferred)
    assert list(holdout.screening.findings) == expected
    assert refusal_text(holdout.finding, parameters) == (
        "No remote option was published, and every named site is a place you accept only as remote."
    )


def _winnipeg_parameters() -> JobSearchParameters:
    """Winnipeg accepts hybrid and on-site; the Canada and US country places are remote-only."""
    refused = Arrangement(
        remote=Acceptance.PREFERRED, hybrid=Acceptance.REFUSED, on_site=Acceptance.REFUSED
    )
    home = Location(
        key="home",
        country="Canada",
        region="Manitoba",
        metro="Winnipeg",
        resident=True,
        arrangement=Arrangement(
            remote=Acceptance.PREFERRED, hybrid=Acceptance.ACCEPTABLE, on_site=Acceptance.ACCEPTABLE
        ),
    )
    return JobSearchParameters(
        locations=[
            home,
            Location(key="canada_remote", country="Canada", arrangement=refused),
            Location(key="us_remote", country="United States", arrangement=refused),
        ],
        countries=[CountryNames(country="Canada"), CountryNames(country="United States")],
        arrangement=refused,
    )


_REFUSED_CODE = ScreeningCode.ARRANGEMENT_REFUSED
_OPEN_CODE = ScreeningCode.ARRANGEMENT_REMOTE_UNCONFIRMED


@pytest.mark.parametrize("arrangement", ["onsite", "hybrid"])
@pytest.mark.parametrize(
    ("location", "code"),
    [
        ("Waltham, MA, United States", _REFUSED_CODE),
        ("Laval, Quebec", _REFUSED_CODE),
        ("", _OPEN_CODE),
        ("STORE 0412", _OPEN_CODE),
        ("Unmapped City", _OPEN_CODE),
        ("Canada", _OPEN_CODE),
        ("Manitoba", _OPEN_CODE),
        ("Manitoba, Canada", _OPEN_CODE),
        ("Remote", _OPEN_CODE),
        ("New Brunswick, New Jersey, United States", _REFUSED_CODE),
        ("New Brunswick, NJ", _REFUSED_CODE),
        ("United States", _REFUSED_CODE),
        ("New Jersey", _REFUSED_CODE),
        ("New Jersey, United States", _REFUSED_CODE),
        ("Winnipeg, Manitoba", ScreeningCode.ARRANGEMENT_MATCH),
        *(
            (f"{town}, MB", _OPEN_CODE)
            for town in (
                "Selkirk",
                "Stonewall",
                "Niverville",
                "Springfield",
                "Oakbank",
                "Headingley",
                "Rural Municipality of Ritchot",
            )
        ),
        ("Selkirk, Manitoba", _OPEN_CODE),
    ],
)
def test_a_complete_office_arrangement_refuses_outside_a_place_accepting_it(
    arrangement: str, location: str, code: ScreeningCode
) -> None:
    """Only a resolvable named site outside the accepting metro's region refuses."""
    facts = StagedListingFacts(
        locations=PublishedFact(
            values=(location,) if location else (),
            provenance="workday.location",
            complete=bool(location),
        ),
        arrangements=PublishedFact(
            values=(arrangement,), provenance="workday.arrangement", complete=True
        ),
    )
    screening = screen_discovery(
        "Acme", "Data Engineer", location, _winnipeg_parameters(), [], facts
    )

    assert _arrangement_of(screening).code is code


@pytest.mark.parametrize(
    ("values", "code"),
    [
        (("Unmapped City", "United States"), _OPEN_CODE),
        (("SF Office", "United States"), _OPEN_CODE),
        (("Remote", "United States"), _OPEN_CODE),
        (("New Brunswick, New Jersey", "United States"), _REFUSED_CODE),
    ],
)
def test_a_named_site_with_no_country_keeps_a_country_value_from_refusing(
    values: tuple[str, ...], code: ScreeningCode
) -> None:
    facts = StagedListingFacts(
        locations=PublishedFact(values=values, provenance="workday.location", complete=True),
        arrangements=PublishedFact(
            values=("hybrid",), provenance="workday.arrangement", complete=True
        ),
    )
    screening = screen_discovery(
        "Acme", "Data Engineer", values[0], _winnipeg_parameters(), [], facts
    )

    assert _arrangement_of(screening).code is code


def test_an_unresolved_location_summary_keeps_an_office_arrangement_open() -> None:
    facts = StagedListingFacts(
        locations=PublishedFact(
            values=("Waltham, MA, United States",),
            unresolved_values=("3 Locations",),
            provenance="workday.location",
            complete=True,
        ),
        arrangements=PublishedFact(
            values=("hybrid",), provenance="workday.arrangement", complete=True
        ),
    )
    screening = screen_discovery(
        "Acme", "Data Engineer", "Waltham, MA, United States", _winnipeg_parameters(), [], facts
    )

    assert _arrangement_of(screening).code is _OPEN_CODE


def test_an_on_site_city_in_another_region_of_the_country_is_held_out() -> None:
    facts = _site_facts("Toronto, Ontario", "Canada", arrangements=("On-site",))
    assert _arrangement_held_out(_prairie_parameters(), facts, _OFFICE_BODY)


@pytest.mark.parametrize(
    "place", ["Saskatoon, Saskatchewan", "White City, Saskatchewan"], ids=["city", "suburb"]
)
def test_an_on_site_town_in_the_region_of_an_accepting_metro_is_not_held_out(place: str) -> None:
    """Saved places carry no distance, so the accepting metro's whole region stays visible."""
    facts = _site_facts(place, "Canada", arrangements=("On-site",))
    assert not _arrangement_held_out(_prairie_parameters(), facts, _OFFICE_BODY)


@pytest.mark.parametrize(
    ("facts", "body"),
    [
        pytest.param(
            _site_facts("Springfield - IL - US", "US, Remote"), _OFFICE_BODY, id="remote-place"
        ),
        pytest.param(
            _site_facts("Massachusetts - Virtual", "United States of America"),
            _OFFICE_BODY,
            id="virtual-place",
        ),
        pytest.param(
            _site_facts("Denver, Colorado", "United States"),
            "Remote candidates will be considered.",
            id="remote-offer-in-body",
        ),
        pytest.param(
            _site_facts("Hartford, Connecticut", "United States"),
            "Open to remote if not located near one of our offices, with a preference for "
            "Eastern time.",
            id="conditional-remote-offer-in-body",
        ),
        pytest.param(
            _site_facts("Denver, Colorado", "United States", arrangements=("Hybrid",)),
            "Hybrid work schedule for Denver-based candidates. Remote candidates will be "
            "considered.",
            id="local-hybrid-rule-plus-remote-offer",
        ),
        pytest.param(
            _site_facts("Denver, Colorado", "United States", arrangements=("Remote or Hybrid",)),
            _OFFICE_BODY,
            id="structured-value-naming-remote",
        ),
        pytest.param(
            _site_facts("Regina, Saskatchewan", arrangements=("On-site",)),
            _OFFICE_BODY,
            id="accepting-metro-on-site",
        ),
        pytest.param(
            _site_facts("Greater Regina Area", "Canada", arrangements=("Hybrid",)),
            _OFFICE_BODY,
            id="accepting-metro-area-hybrid",
        ),
        pytest.param(
            _site_facts("Saskatchewan", arrangements=("Hybrid",)),
            _OFFICE_BODY,
            id="region-only-hybrid",
        ),
        pytest.param(
            _site_facts(
                "Blue Bell, Pennsylvania",
                "United States",
                arrangements=(
                    workday_arrangement_facts({}, {"remoteType": "Office or Home"}, ()).value or "",
                ),
            ),
            _OFFICE_BODY,
            id="workday-office-or-home",
        ),
        pytest.param(_site_facts("United States"), _OFFICE_BODY, id="country-only"),
        pytest.param(_site_facts("Canada", arrangements=("Hybrid",)), _OFFICE_BODY, id="canada"),
        pytest.param(_site_facts("Denver, Colorado", "United States"), "", id="empty-body"),
    ],
)
def test_a_row_with_remote_evidence_or_no_named_site_is_not_held_out(
    facts: StagedListingFacts, body: str
) -> None:
    assert not _arrangement_held_out(_prairie_parameters(), facts, body)


@pytest.mark.parametrize(
    "place",
    [
        "Work From Home - Colorado",
        "Home Based - US",
        "Telecommute, United States",
        "Anywhere in the US",
        "USA - Nationwide",
        "Flexible, United States",
    ],
)
def test_a_place_value_meaning_remote_or_anywhere_is_not_held_out(place: str) -> None:
    facts = _site_facts(place, "United States")
    assert not _arrangement_held_out(_prairie_parameters(), facts, _OFFICE_BODY)


@pytest.mark.parametrize("arrangement", ["Telework", "Distributed", "Flexible", "Home Based"])
def test_an_arrangement_value_meaning_remote_or_flexible_is_not_held_out(arrangement: str) -> None:
    facts = _site_facts("Denver, Colorado", "United States", arrangements=(arrangement,))
    assert not _arrangement_held_out(_prairie_parameters(), facts, _OFFICE_BODY)


@pytest.mark.parametrize(
    "places",
    [
        ("Washington",),
        ("Virginia",),
        ("Arizona",),
        ("Colorado",),
        ("Wyoming",),
        ("Washington", "United States"),
        ("Virginia, US",),
        ("Ontario", "Canada"),
    ],
    ids=" + ".join,
)
def test_a_bare_state_or_province_with_no_arrangement_is_not_held_out(
    places: tuple[str, ...],
) -> None:
    """A bare region names no office, so it cannot show the work sits outside a saved market."""
    facts = _site_facts(*places)
    assert not _arrangement_held_out(_prairie_parameters(), facts, _OFFICE_BODY)


def test_an_incomplete_location_fact_never_holds_out_an_unknown_arrangement() -> None:
    facts = StagedListingFacts(
        locations=PublishedFact(
            values=("Denver, Colorado",),
            unresolved_values=("+2 more",),
            provenance="workday.location",
            complete=False,
        )
    )
    assert not _arrangement_held_out(_prairie_parameters(), facts, _OFFICE_BODY)


def test_lever_shaped_city_and_country_pair_refuses_the_matching_market() -> None:
    """A Lever-style ('Durham, NC', 'United States') pair resolves to us_remote."""
    parameters = _hybrid_holdout_parameters()
    facts = _hybrid_facts("lever", "Durham, NC", "United States")
    screening = screen_discovery("Acme", "Data Engineer", "Durham, NC", parameters, [], facts)
    arrangement = next(
        item for item in screening.findings if item.dimension is ScreeningDimension.ARRANGEMENT
    )
    assert arrangement.standing is ScreeningStanding.REFUSE
    assert arrangement.subject == "us_remote"


def test_winnipeg_and_canada_refuses_the_general_canada_market() -> None:
    """An unmatched precise city falls back to the general country market, not a sibling city's."""
    parameters = _hybrid_holdout_parameters()
    facts = _hybrid_facts("lever", "Winnipeg", "Canada")
    screening = screen_discovery("Acme", "Data Engineer", "Winnipeg", parameters, [], facts)
    arrangement = next(
        item for item in screening.findings if item.dimension is ScreeningDimension.ARRANGEMENT
    )
    assert arrangement.standing is ScreeningStanding.REFUSE
    assert arrangement.subject == "canada_remote"


@pytest.mark.parametrize(
    "city",
    [
        "Round Rock, Texas",
        "Cedar Park, Texas",
        "Pflugerville, Texas",
        "Leander, Texas",
        "Kyle, Texas",
        "Dallas, Texas",
    ],
)
def test_a_commuter_town_in_an_accepting_subdivision_stays_visible(city: str) -> None:
    """An unmatched precise city refuses only when no saved market in its own subdivision accepts.

    Austin's region is Texas, so every real Texas town here must stay visible
    even though none matches by name.
    """
    parameters = _hybrid_holdout_parameters()
    facts = _hybrid_facts("lever", city, "United States")
    screening = screen_discovery("Acme", "Data Engineer", city, parameters, [], facts)
    arrangement = next(
        item for item in screening.findings if item.dimension is ScreeningDimension.ARRANGEMENT
    )
    assert arrangement.standing is not ScreeningStanding.REFUSE
    assert not _arrangement_held_out(parameters, facts, _OFFICE_BODY)


def test_an_unindexed_texas_place_alongside_an_indexed_one_stays_visible() -> None:
    """A place absent from the gazetteer contributes no country evidence and never refuses."""
    parameters = _hybrid_holdout_parameters()
    facts = _hybrid_facts("lever", "Manor, Texas", "Round Rock, Texas", "United States")
    screening = screen_discovery("Acme", "Data Engineer", "Manor, Texas", parameters, [], facts)
    arrangement = next(
        item for item in screening.findings if item.dimension is ScreeningDimension.ARRANGEMENT
    )
    assert arrangement.standing is not ScreeningStanding.REFUSE


def test_munich_and_united_states_conflicting_country_pair_stays_unconfirmed() -> None:
    """A bare country word that disagrees with the precise city's own country blocks the read."""
    parameters = _hybrid_holdout_parameters()
    facts = _hybrid_facts("lever", "Munich", "United States")
    screening = screen_discovery("Acme", "Data Engineer", "Munich", parameters, [], facts)
    arrangement = next(
        item for item in screening.findings if item.dimension is ScreeningDimension.ARRANGEMENT
    )
    assert arrangement.standing is ScreeningStanding.UNKNOWN


def test_multi_location_with_an_accepting_metro_keeps_the_row() -> None:
    """A saved metro's own arrangement wins a multi-market posting outright, never a refusal."""
    parameters = _hybrid_holdout_parameters()
    facts = _hybrid_facts("lever", "Austin, Texas", "Winnipeg, Manitoba", "United States", "Canada")
    screening = screen_discovery("Acme", "Data Engineer", "Austin, Texas", parameters, [], facts)
    location = next(
        item for item in screening.findings if item.dimension is ScreeningDimension.LOCATION
    )
    assert location.subject == "austin"
    arrangement = next(
        item for item in screening.findings if item.dimension is ScreeningDimension.ARRANGEMENT
    )
    # Austin caps office days, so a bare "hybrid" tag with no stated cadence stays
    # unconfirmed rather than refused; the multi-location tie never reaches
    # us_remote or canada_remote.
    assert arrangement.standing is not ScreeningStanding.REFUSE


def test_body_arrangement_refusal_with_unknown_location_stays_visible() -> None:
    """An unresolved or missing location never lets a body refusal dismiss the row."""
    parameters = _hybrid_holdout_parameters()
    facts = StagedListingFacts(
        locations=PublishedFact(values=("Multiple Locations",), provenance="ashby.location"),
        arrangements=PublishedFact(
            values=("hybrid, This is a hybrid role.",),
            provenance="publisher_body.arrangement",
            complete=True,
        ),
    )
    screening = screen_discovery("Acme", "Data Engineer", "", parameters, [], facts)
    arrangement = next(
        item for item in screening.findings if item.dimension is ScreeningDimension.ARRANGEMENT
    )
    assert arrangement.standing is ScreeningStanding.UNKNOWN


def test_weekly_cadence_at_a_coarse_country_with_no_accepting_place_refuses() -> None:
    parameters = _hybrid_holdout_parameters()
    facts = StagedListingFacts(
        locations=PublishedFact(values=("Canada",), provenance="ashby.location", complete=True),
        arrangements=PublishedFact(
            values=("hybrid, This position requires you to be in the office 3 days per week.",),
            provenance="publisher_body.arrangement",
            complete=True,
        ),
    )
    screening = screen_discovery("Acme", "Data Engineer", "Canada", parameters, [], facts)
    arrangement = next(
        item for item in screening.findings if item.dimension is ScreeningDimension.ARRANGEMENT
    )
    assert arrangement.standing is ScreeningStanding.REFUSE


def test_weekly_cadence_at_a_precise_refusing_place_still_refuses() -> None:
    """A precise refusing place with an explicit weekly cadence must still refuse."""
    parameters = _hybrid_holdout_parameters()
    facts = StagedListingFacts(
        locations=PublishedFact(
            values=("New York, NY",), provenance="lever.location", complete=True
        ),
        arrangements=PublishedFact(
            values=("hybrid, This position requires you to be in the office 3 days per week.",),
            provenance="publisher_body.arrangement",
            complete=True,
        ),
    )
    screening = screen_discovery("Acme", "Data Engineer", "New York, NY", parameters, [], facts)
    arrangement = next(
        item for item in screening.findings if item.dimension is ScreeningDimension.ARRANGEMENT
    )
    assert arrangement.standing is ScreeningStanding.REFUSE
    assert arrangement.subject == "us_remote"


def test_incomplete_location_fact_never_refuses_the_arrangement() -> None:
    """A location fact with unresolved '+N more' values can never anchor a refusal."""
    parameters = _hybrid_holdout_parameters()
    facts = StagedListingFacts(
        locations=PublishedFact(
            values=("New York, NY",),
            unresolved_values=("+3 more",),
            provenance="workday.location",
            complete=False,
        ),
        arrangements=PublishedFact(
            values=("hybrid, This is a hybrid role.",),
            provenance="publisher_body.arrangement",
            complete=True,
        ),
    )
    screening = screen_discovery("Acme", "Data Engineer", "", parameters, [], facts)
    arrangement = next(
        item for item in screening.findings if item.dimension is ScreeningDimension.ARRANGEMENT
    )
    assert arrangement.standing is ScreeningStanding.UNKNOWN


def _tie_break_parameters() -> JobSearchParameters:
    """Two same-tier Texas metros, only one of which accepts hybrid."""
    accepting = Arrangement(
        remote=Acceptance.PREFERRED, hybrid=Acceptance.ACCEPTABLE, on_site=Acceptance.ACCEPTABLE
    )
    refusing = Arrangement(remote=Acceptance.PREFERRED, hybrid=Acceptance.REFUSED)
    return JobSearchParameters(
        locations=[
            Location(
                key="austin",
                country="United States",
                region="Texas",
                metro="Austin",
                arrangement=accepting,
            ),
            Location(
                key="dallas",
                country="United States",
                region="Texas",
                metro="Dallas",
                arrangement=refusing,
            ),
        ],
        countries=[CountryNames(country="United States")],
        arrangement=refusing,
    )


def _single_market_parameters(country: str) -> JobSearchParameters:
    """Return a person whose only saved, actionable market is ``country``."""
    return JobSearchParameters(
        locations=[Location(key="home", country=country)],
        countries=[CountryNames(country=country)],
    )


def test_preferred_tie_breaks_toward_the_accepting_place_in_either_list_order() -> None:
    """A tie between two same-tier saved metros prefers the one whose arrangement accepts."""
    parameters = _tie_break_parameters()
    forward = _hybrid_facts("lever", "Austin, Texas", "Dallas, Texas")
    backward = _hybrid_facts("lever", "Dallas, Texas", "Austin, Texas")
    for facts in (forward, backward):
        screening = screen_discovery(
            "Acme", "Data Engineer", "Austin, Texas", parameters, [], facts
        )
        location = next(
            item for item in screening.findings if item.dimension is ScreeningDimension.LOCATION
        )
        assert location.subject == "austin"


def test_a_search_with_no_target_titles_screens_every_title_as_unrestricted() -> None:
    """With nothing configured to match, the title boundary answers a match, not a mismatch."""
    screening = screen_discovery(
        "Northwind Logistics",
        "Warehouse Supervisor",
        "Northport, Freedonia",
        home_market_search(),
        [],
        published_location_facts("Northport, Freedonia"),
    )

    title = next(item for item in screening.findings if item.dimension is ScreeningDimension.TITLE)
    assert title.standing is ScreeningStanding.MATCH
    assert title.code is ScreeningCode.TITLE_UNRESTRICTED


def test_a_named_employer_outside_the_exclusions_is_a_typed_employer_match() -> None:
    """The employer boundary answers every screen, so a clean name gets its own typed match."""
    parameters = JobSearchParameters(
        employers=Employers(excluded_employers=["Initech"]),
        seniority=Seniority(target_titles=titles("Data Platform Lead")),
    )

    screening = screen_discovery(
        "Northwind Logistics",
        "Data Platform Lead",
        "",
        parameters,
        ["Data Platform Lead"],
        StagedListingFacts(),
    )

    employer = next(
        item for item in screening.findings if item.dimension is ScreeningDimension.EMPLOYER
    )
    assert employer.standing is ScreeningStanding.MATCH
    assert employer.code is ScreeningCode.EMPLOYER_MATCH


def test_a_discovery_with_no_employer_name_cannot_answer_a_configured_exclusion() -> None:
    """An exclusion list is configured and there is no name to read it against."""
    parameters = JobSearchParameters(
        employers=Employers(excluded_employers=["Initech"]),
        seniority=Seniority(target_titles=titles("Data Platform Lead")),
    )

    screening = screen_discovery(
        "",
        "Data Platform Lead",
        "",
        parameters,
        ["Data Platform Lead"],
        StagedListingFacts(),
    )

    employer = next(
        item for item in screening.findings if item.dimension is ScreeningDimension.EMPLOYER
    )
    assert employer.standing is ScreeningStanding.UNKNOWN
    assert employer.code is ScreeningCode.EMPLOYER_UNKNOWN


def test_an_unvouched_identity_leaves_a_stated_seniority_band_unanswered() -> None:
    """A search hint may not settle a level, so the band stays unverified rather than unknown."""
    parameters = JobSearchParameters(
        seniority=Seniority(target_titles=titles("Data Platform Lead"), minimum="senior"),
    )

    screening = screen_discovery(
        "Northwind Logistics",
        "Associate Data Platform Lead",
        "",
        parameters,
        ["Data Platform Lead"],
        StagedListingFacts(identity_trust=ListingIdentityTrust.UNVERIFIED_HINT),
    )

    seniority = next(
        item for item in screening.findings if item.dimension is ScreeningDimension.SENIORITY
    )
    assert seniority.standing is ScreeningStanding.UNKNOWN
    assert seniority.code is ScreeningCode.SENIORITY_UNVERIFIED


def test_the_screening_code_vocabulary_is_the_closed_set_callers_may_branch_on() -> None:
    """The codes are a contract with every reader, so a member cannot quietly leave it."""
    assert set(ScreeningCode) == {
        ScreeningCode.TITLE_MATCH,
        ScreeningCode.TITLE_EXCLUDED,
        ScreeningCode.TITLE_TEST_REQUISITION,
        ScreeningCode.TITLE_MISMATCH,
        ScreeningCode.TITLE_NEAR_MATCH,
        ScreeningCode.TITLE_ROLE_FAMILY_REVIEW,
        ScreeningCode.TITLE_SPECIALIST_IC_REVIEW,
        ScreeningCode.TITLE_DOMAIN_REVIEW,
        ScreeningCode.TITLE_ROLE_FAMILY_CONFLICT,
        ScreeningCode.TITLE_EXTRA_ROLE_WORDS,
        ScreeningCode.TITLE_UNRESTRICTED,
        ScreeningCode.TITLE_UNVERIFIED,
        ScreeningCode.EMPLOYER_MATCH,
        ScreeningCode.EMPLOYER_EXCLUDED,
        ScreeningCode.EMPLOYER_UNKNOWN,
        ScreeningCode.EMPLOYER_UNVERIFIED,
        ScreeningCode.SENIORITY_MATCH,
        ScreeningCode.SENIORITY_OUTSIDE,
        ScreeningCode.SENIORITY_UNKNOWN,
        ScreeningCode.SENIORITY_UNVERIFIED,
        ScreeningCode.LOCATION_MATCH,
        ScreeningCode.LOCATION_UNRESTRICTED,
        ScreeningCode.LOCATION_OUTSIDE,
        ScreeningCode.LOCATION_INELIGIBLE,
        ScreeningCode.LOCATION_MISSING,
        ScreeningCode.LOCATION_UNRESOLVED,
        ScreeningCode.LOCATION_UNVERIFIED,
        ScreeningCode.LOCATION_RESIDENCE_REQUIRED,
        ScreeningCode.EMPLOYMENT_MATCH,
        ScreeningCode.EMPLOYMENT_EXCLUDED,
        ScreeningCode.EMPLOYMENT_OUTSIDE,
        ScreeningCode.EMPLOYMENT_UNKNOWN,
        ScreeningCode.ARRANGEMENT_MATCH,
        ScreeningCode.ARRANGEMENT_REFUSED,
        ScreeningCode.ARRANGEMENT_OFFICE_LIMIT,
        ScreeningCode.ARRANGEMENT_REMOTE_UNCONFIRMED,
        ScreeningCode.ARRANGEMENT_REMOTE_ONLY_SITES,
        ScreeningCode.ARRANGEMENT_UNKNOWN,
        ScreeningCode.TRAVEL_OVER_LIMIT,
        ScreeningCode.CONDITION_EXCLUDED,
        ScreeningCode.COMPENSATION_MATCH,
        ScreeningCode.COMPENSATION_BELOW_MINIMUM,
        ScreeningCode.COMPENSATION_UNKNOWN,
        ScreeningCode.COMPENSATION_STATED,
        ScreeningCode.COMPENSATION_TARGET_MET,
        ScreeningCode.COMPENSATION_BELOW_TARGET,
    }


def _us_person(**extra: Any) -> JobSearchParameters:
    return JobSearchParameters(
        locations=[Location(key="home", country="United States")],
        countries=[CountryNames(country="United States")],
        **extra,
    )


def _stated(value: str) -> PublishedFact:
    return PublishedFact(values=(value,), complete=True) if value else PublishedFact()


def _screened(
    parameters: JobSearchParameters,
    dimension: str,
    *,
    arrangements: str = "",
    compensation: str = "",
    employment_types: str = "",
) -> ScreeningCode:
    screening = screen_discovery(
        "Freedonia Health",
        "Platform Product Manager",
        "United States",
        parameters,
        [],
        StagedListingFacts(
            locations=_stated("United States"),
            arrangements=_stated(arrangements),
            compensation=_stated(compensation),
            employment_types=_stated(employment_types),
        ),
    )
    return next(f for f in screening.findings if f.dimension.value == dimension).code


_PAY_THOUSANDS = {
    "comma": "{},000",
    "space": "{} 000",
    "no-break space": "{}\u00a0000",
    "narrow space": "{}\u202f000",
    "word": "{} thousand",
    "k": "{}k",
    "spaced k": "{} k",
    "spaced K": "{} K",
}


@pytest.mark.parametrize(
    ("form", "separator", "pair", "code"),
    [
        (form, separator, pair, code)
        for form in _PAY_THOUSANDS
        for separator in ("-", " - ", "\u2013", " \u2014 ", " to ")
        for pair, code in (
            ((125, 160), ScreeningCode.COMPENSATION_MATCH),
            ((95, 130), ScreeningCode.COMPENSATION_MATCH),
            ((60, 90), ScreeningCode.COMPENSATION_BELOW_MINIMUM),
        )
    ],
)
def test_pay_reads_its_top_in_every_thousands_form(
    form: str, separator: str, pair: tuple[int, int], code: ScreeningCode
) -> None:
    low, high = (_PAY_THOUSANDS[form].format(value) for value in pair)
    parameters = _us_person(
        compensation=[CompensationBand(location="home", currency="USD", minimum=Decimal(100000))]
    )

    value = f"USD {low}{separator}{high} per year"
    assert _screened(parameters, "compensation", compensation=value) is code


def _no_floor_person() -> JobSearchParameters:
    return _us_person(
        compensation=[
            CompensationBand(
                location="home",
                currency="USD",
                target=Decimal(170000),
                stretch=Decimal(210000),
            )
        ]
    )


def _pay_screening(compensation: str) -> DiscoveryScreening:
    return screen_discovery(
        "Freedonia Health",
        "Platform Product Manager",
        "United States",
        _no_floor_person(),
        [],
        StagedListingFacts(locations=_stated("United States"), compensation=_stated(compensation)),
    )


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("USD 90,000 - 120,000 per year", ScreeningCode.COMPENSATION_BELOW_TARGET),
        ("USD 140,000 - 175,000 per year", ScreeningCode.COMPENSATION_TARGET_MET),
        ("USD 210,000 - 250,000 per year", ScreeningCode.COMPENSATION_TARGET_MET),
        ("CAD 150,000 per year", ScreeningCode.COMPENSATION_STATED),
        ("USD 150,000", ScreeningCode.COMPENSATION_STATED),
    ],
)
def test_a_band_with_no_minimum_shows_stated_pay_against_its_target(
    value: str, code: ScreeningCode
) -> None:
    screening = _pay_screening(value)

    pay = next(f for f in screening.findings if f.dimension is ScreeningDimension.COMPENSATION)
    assert (pay.standing, pay.code, pay.subject) == (ScreeningStanding.MATCH, code, value)
    assert constraint_confidence(screening) is not ConstraintConfidence.UNCERTAIN_COMP


def test_a_band_with_no_minimum_and_no_stated_pay_leaves_compensation_unasked() -> None:
    screening = _pay_screening("")

    assert ScreeningDimension.COMPENSATION not in {f.dimension for f in screening.findings}
    assert constraint_confidence(screening) is not ConstraintConfidence.UNCERTAIN_COMP


def test_a_band_with_no_minimum_shows_pay_an_incomplete_listing_fact_states() -> None:
    value = "USD 140,000 - 175,000 per year"
    screening = screen_discovery(
        "Freedonia Health",
        "Platform Product Manager",
        "United States",
        _no_floor_person(),
        [],
        StagedListingFacts(
            locations=_stated("United States"), compensation=PublishedFact(values=(value,))
        ),
    )

    pay = next(f for f in screening.findings if f.dimension is ScreeningDimension.COMPENSATION)
    assert (pay.code, pay.subject) == (ScreeningCode.COMPENSATION_TARGET_MET, value)


def test_an_unplaced_row_under_bands_with_no_minimum_is_not_compensation_uncertain() -> None:
    screening = screen_discovery(
        "Freedonia Health",
        "Platform Product Manager",
        "",
        _no_floor_person(),
        [],
        StagedListingFacts(compensation=_stated("USD 140,000 - 175,000 per year")),
    )

    assert ScreeningCode.COMPENSATION_UNKNOWN not in {f.code for f in screening.findings}


def _floor_only_in_north() -> JobSearchParameters:
    return JobSearchParameters(
        locations=[
            Location(key="home", country="United States"),
            Location(key="north", country="Canada"),
        ],
        countries=[CountryNames(country="United States"), CountryNames(country="Canada")],
        compensation=[CompensationBand(location="north", currency="CAD", minimum=Decimal(120000))],
    )


def test_a_place_with_no_band_shows_stated_pay_though_another_place_has_a_floor() -> None:
    value = "USD 140,000 - 175,000 per year"
    screening = screen_discovery(
        "Freedonia Health",
        "Platform Product Manager",
        "United States",
        _floor_only_in_north(),
        [],
        StagedListingFacts(locations=_stated("United States"), compensation=_stated(value)),
    )

    pay = next(f for f in screening.findings if f.dimension is ScreeningDimension.COMPENSATION)
    assert (pay.standing, pay.code, pay.subject) == (
        ScreeningStanding.MATCH,
        ScreeningCode.COMPENSATION_STATED,
        value,
    )


def test_an_unplaced_row_stays_compensation_uncertain_when_any_band_has_a_floor() -> None:
    screening = screen_discovery(
        "Freedonia Health",
        "Platform Product Manager",
        "",
        _floor_only_in_north(),
        [],
        StagedListingFacts(compensation=_stated("USD 140,000 - 175,000 per year")),
    )

    assert ScreeningCode.COMPENSATION_UNKNOWN in {f.code for f in screening.findings}


@pytest.mark.parametrize(
    ("fact", "code"),
    [
        (_stated("USD 120,000 - 150,000 per year"), ScreeningCode.COMPENSATION_MATCH),
        (_stated("USD 60,000 - 90,000 per year"), ScreeningCode.COMPENSATION_BELOW_MINIMUM),
        (
            _stated("$190K - $260K • Offers Bonus (salary USD 190,000 - 260,000 per year)"),
            ScreeningCode.COMPENSATION_MATCH,
        ),
        (
            _stated("$70K - $90K • Offers Bonus (salary USD 70,000 - 90,000 per year)"),
            ScreeningCode.COMPENSATION_BELOW_MINIMUM,
        ),
        (
            PublishedFact(values=("USD 120000-150000 per year",)),
            ScreeningCode.COMPENSATION_UNKNOWN,
        ),
    ],
)
def test_a_band_with_a_minimum_shows_the_pay_it_judged(
    fact: PublishedFact, code: ScreeningCode
) -> None:
    parameters = _us_person(
        compensation=[CompensationBand(location="home", currency="USD", minimum=Decimal(100000))]
    )
    screening = screen_discovery(
        "Freedonia Health",
        "Platform Product Manager",
        "United States",
        parameters,
        [],
        StagedListingFacts(locations=_stated("United States"), compensation=fact),
    )

    pay = next(f for f in screening.findings if f.dimension is ScreeningDimension.COMPENSATION)
    assert (pay.code, pay.subject) == (code, fact.values[0])


_REMOTE_ONLY = Arrangement(
    remote=Acceptance.PREFERRED, hybrid=Acceptance.REFUSED, on_site=Acceptance.REFUSED
)


@pytest.mark.parametrize(
    "value",
    [
        "Not Remote",
        "not remote",
        "Non-Remote",
        "Non Remote",
        "NOT REMOTE",
        "Onsite (not remote)",
        "Hybrid Remote",
        "Hybrid (Remote 2 days)",
        "Remote/Hybrid",
        "hybrid, You must work in the office 3 days per week and remotely the remaining days.",
        "on site, This role is fully on-site; remote work is not available.",
    ],
)
def test_a_negated_or_hybrid_remote_value_never_matches_a_remote_only_person(value: str) -> None:
    code = _screened(_us_person(arrangement=_REMOTE_ONLY), "arrangement", arrangements=value)

    assert code is not ScreeningCode.ARRANGEMENT_MATCH


@pytest.mark.parametrize(
    "value", ["Remote", "Fully Remote", "Remote - United States", "Work From Home", "WFH"]
)
def test_a_remote_value_matches_a_remote_only_person(value: str) -> None:
    code = _screened(_us_person(arrangement=_REMOTE_ONLY), "arrangement", arrangements=value)

    assert code is ScreeningCode.ARRANGEMENT_MATCH


@pytest.mark.parametrize(
    ("low", "separator", "high", "unit"),
    [
        (low, separator, high, unit)
        for (low, high) in (("3", "4"), ("three", "four"), ("3", "five"))
        for separator in ("-", " - ", "\u2013", " to ", " or ", "~", "\u301c")
        for unit in ("days per week", "days a week", "days/week", "days each week")
    ],
)
def test_an_office_day_range_is_held_to_the_limit_by_its_bottom(
    low: str, separator: str, high: str, unit: str
) -> None:
    parameters = _us_person(
        arrangement=Arrangement(hybrid=Acceptance.ACCEPTABLE, maximum_office_days=3)
    )

    value = f"Hybrid, {low}{separator}{high} {unit}"
    assert _screened(parameters, "arrangement", arrangements=value) is (
        ScreeningCode.ARRANGEMENT_MATCH
    )


@pytest.mark.parametrize("value", ["Permanent", "Full-time, Permanent", "Permanent Full-Time"])
def test_a_permanent_posting_is_not_refused_as_outside_full_time(value: str) -> None:
    parameters = _us_person(employment=Employment(types=["Full-time"]))

    code = _screened(parameters, "employment_type", employment_types=value)

    assert code is not ScreeningCode.EMPLOYMENT_OUTSIDE


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("Regular Full-Time", ScreeningCode.EMPLOYMENT_MATCH),
        ("Full-Time Employee", ScreeningCode.EMPLOYMENT_MATCH),
        ("Part-time", ScreeningCode.EMPLOYMENT_OUTSIDE),
        ("Contract", ScreeningCode.EMPLOYMENT_OUTSIDE),
        ("Temp", ScreeningCode.EMPLOYMENT_OUTSIDE),
        ("Co-op", ScreeningCode.EMPLOYMENT_OUTSIDE),
    ],
)
def test_an_employment_label_reads_every_category_it_names(value: str, code: ScreeningCode) -> None:
    parameters = _us_person(employment=Employment(types=["Full-time"]))

    assert _screened(parameters, "employment_type", employment_types=value) is code


@pytest.mark.parametrize(
    "value",
    [
        joiner.join(parts) if case == "as written" else joiner.join(parts).upper()
        for parts in (
            ("Contract", "to", "Hire"),
            ("Temp", "to", "Perm"),
            ("Contract", "to", "Permanent"),
        )
        for joiner in (" ", "-")
        for case in ("as written", "upper")
    ]
    + [
        f"{term}{link}{target}"
        for term in ("Contract", "Temp", "Temporary", "Contractor")
        for link in (" with option to ", " w/ option to ", " to ", "-to-", ", then ")
        for target in ("hire", "FTE", "Full-Time", "perm", "permanent")
    ]
    + ["Contract (possible conversion)", "Contract, conversion possible", "Contract-to-hire (C2H)"],
)
def test_a_conversion_label_is_not_refused_as_outside_full_time(value: str) -> None:
    parameters = _us_person(employment=Employment(types=["Full-time"]))

    code = _screened(parameters, "employment_type", employment_types=value)

    assert code is ScreeningCode.EMPLOYMENT_UNKNOWN


@pytest.mark.parametrize("value", ["USD 1.5M per year", "USD 1.2 million per year"])
def test_pay_stated_in_millions_reads_in_millions(value: str) -> None:
    parameters = _us_person(
        compensation=[CompensationBand(location="home", currency="USD", minimum=Decimal(100000))]
    )

    assert _screened(parameters, "compensation", compensation=value) is (
        ScreeningCode.COMPENSATION_MATCH
    )


_ARRANGEMENT_JOINERS = (" or ", " Or ", "/", " / ", " and ", ", ", " & ", " or fully ")


@pytest.mark.parametrize(
    ("first", "joiner", "second"),
    [
        (first, joiner, second)
        for first, second in (("Hybrid", "Remote"), ("Remote", "Hybrid"))
        for joiner in _ARRANGEMENT_JOINERS
    ],
)
def test_remote_and_hybrid_offered_together_refuse_neither_person(
    first: str, joiner: str, second: str
) -> None:
    value = f"{first}{joiner}{second}"
    for arrangement in (
        _REMOTE_ONLY,
        Arrangement(remote=Acceptance.REFUSED, hybrid=Acceptance.PREFERRED),
    ):
        code = _screened(_us_person(arrangement=arrangement), "arrangement", arrangements=value)

        assert code is ScreeningCode.ARRANGEMENT_UNKNOWN


@pytest.mark.parametrize(
    "value",
    [
        "Hybrid (3 days onsite)",
        "Hybrid - 3 days in office",
        "Hybrid (on-site 3 days per week)",
        "Hybrid, in-person 2 days a week",
        "Hybrid: In Office Tuesdays",
    ],
)
def test_hybrid_with_on_site_words_reads_as_hybrid(value: str) -> None:
    hybrid_only = Arrangement(hybrid=Acceptance.ACCEPTABLE, on_site=Acceptance.REFUSED)
    code = _screened(_us_person(arrangement=hybrid_only), "arrangement", arrangements=value)

    assert code is ScreeningCode.ARRANGEMENT_MATCH


def test_hybrid_with_on_site_words_keeps_its_day_count_for_the_limit() -> None:
    parameters = _us_person(
        arrangement=Arrangement(hybrid=Acceptance.ACCEPTABLE, maximum_office_days=2)
    )

    code = _screened(parameters, "arrangement", arrangements="Hybrid (on-site 3 days per week)")

    assert code is ScreeningCode.ARRANGEMENT_OFFICE_LIMIT


# Only these spellings are the generic US-remote label; every other US-remote label
# is a verified place a person outside the US can be held out of.
_GENERIC_US_REMOTE = ("US Remote", "Remote - US", "Remote, US", "Remote (United States)")


@pytest.mark.parametrize(
    "label",
    [
        "Remote - USA",
        "USA - Remote",
        "Remote (USA)",
        "U.S. Remote",
        "Remote in the US",
        "Remote in the United States of America",
        "Remote US-only",
        "Remote, USA only",
    ],
)
def test_a_us_remote_label_other_than_the_generic_one_is_held_out_for_a_canada_search(
    label: str,
) -> None:
    parameters = _single_market_parameters("Canada")
    facts = published_listing_facts("greenhouse", locations=[label])
    screening = screen_discovery("Freedonia Health", "Analyst", label, parameters, [], facts)

    assert structured_location_holdout(facts, label, screening, parameters)


@pytest.mark.parametrize(
    ("label", "dash"),
    [(label, dash) for label in _GENERIC_US_REMOTE for dash in ("\u2013", "\u2014")],
)
def test_a_dash_glyph_reads_the_generic_label_as_the_hyphen_does(label: str, dash: str) -> None:
    parameters = _single_market_parameters("Canada")
    written = label.replace("-", dash)
    held: list[bool] = []
    for value in (label, written):
        facts = published_listing_facts("greenhouse", locations=[value])
        screening = screen_discovery("Freedonia Health", "Analyst", value, parameters, [], facts)
        held.append(structured_location_holdout(facts, value, screening, parameters))

    assert held == [False, False]


_LIMIT_TWO = Arrangement(hybrid=Acceptance.ACCEPTABLE, maximum_office_days=2)


@pytest.mark.parametrize(
    "value",
    [
        f"Hybrid{opener}{away} {count} days {period}{closer}"
        for away in ("remote", "remotely", "from home", "WFH", "working from home")
        for count in ("3", "three", "4")
        for period in ("a week", "per week", "each week")
        for opener, closer in ((" (", ")"), (", ", ""))
    ]
    + [
        f"Hybrid, {count} days {period} {away}"
        for away in ("remote", "remotely", "from home", "WFH")
        for count in ("3", "4")
        for period in ("a week", "per week")
    ],
)
def test_a_count_of_days_away_from_the_office_is_no_office_day_count(value: str) -> None:
    code = _screened(_us_person(arrangement=_LIMIT_TWO), "arrangement", arrangements=value)

    assert code is not ScreeningCode.ARRANGEMENT_OFFICE_LIMIT


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (f"Hybrid, {away} {more} days per week and {office} {fewer} days per week", code)
        for away in ("remote", "remotely", "from home", "WFH")
        for office in ("in office", "in the office", "in our office", "onsite", "on-site")
        for more, fewer, code in (
            ("3", "2", ScreeningCode.ARRANGEMENT_MATCH),
            ("2", "3", ScreeningCode.ARRANGEMENT_OFFICE_LIMIT),
        )
    ]
    + [
        ("Hybrid, 4 days per week remote and 1 in office", ScreeningCode.ARRANGEMENT_MATCH),
        ("Hybrid, 1 day per week remote and 4 in office", ScreeningCode.ARRANGEMENT_OFFICE_LIMIT),
        ("Hybrid, 2 days onsite and 3 days remote per week", ScreeningCode.ARRANGEMENT_MATCH),
        (
            "Hybrid, 3 days onsite and 2 days remote per week",
            ScreeningCode.ARRANGEMENT_OFFICE_LIMIT,
        ),
    ],
)
def test_the_office_count_is_read_where_a_label_states_both_counts(
    value: str, code: ScreeningCode
) -> None:
    assert _screened(_us_person(arrangement=_LIMIT_TWO), "arrangement", arrangements=value) is code


@pytest.mark.parametrize(
    "value",
    [
        f"Hybrid, up to {count} days {period}"
        for count in ("3", "three", "4", "5")
        for period in ("a week", "per week", "/week", "in office per week")
    ],
)
def test_an_up_to_ceiling_on_office_days_never_refuses(value: str) -> None:
    code = _screened(_us_person(arrangement=_LIMIT_TWO), "arrangement", arrangements=value)

    assert code is not ScreeningCode.ARRANGEMENT_OFFICE_LIMIT


@pytest.mark.parametrize(
    "value",
    [
        shape.format(number=number, days=days)
        for shape in (
            "Hybrid, office in Building {number}, {days} days per week",
            "Hybrid (Building {number} office), {days} days per week",
            "Hybrid - Floor {number} office, {days} days/week",
            "Hybrid, Suite {number} office, {days} days a week",
        )
        for number in ("5", "6", "12")
        for days in ("1", "2")
    ]
    + [
        f"Hybrid, {noun} {number} {where}, 2 days per week"
        for noun in ("Floor", "Building", "Suite", "Level", "Room", "team of")
        for number in ("3", "5")
        for where in ("onsite", "in office", "on-site")
    ],
)
def test_a_number_naming_an_office_is_no_office_day_count(value: str) -> None:
    code = _screened(_us_person(arrangement=_LIMIT_TWO), "arrangement", arrangements=value)

    assert code is ScreeningCode.ARRANGEMENT_MATCH


@pytest.mark.parametrize(
    "value",
    [
        f"Hybrid, {days} days per week; 1 {event} per {period}"
        for days in ("3", "4")
        for event in ("office visit", "team offsite in office", "in-person offsite")
        for period in ("quarter", "month", "year")
    ]
    + ["Hybrid, 3 days per week in our home office in Austin"]
    + [
        f"Hybrid, {days} days per week in office ({away_days} {away})"
        for days in ("3", "4")
        for away_days in ("1", "2")
        for away in ("from home", "remote", "WFH", "days at home")
    ],
)
def test_a_visit_count_leaves_the_weekly_office_count_to_the_limit(value: str) -> None:
    code = _screened(_us_person(arrangement=_LIMIT_TWO), "arrangement", arrangements=value)

    assert code is ScreeningCode.ARRANGEMENT_OFFICE_LIMIT


@pytest.mark.parametrize(
    "title",
    [
        "DO NOT APPLY - TEST REQ - CRG - Specialist, Clinical Lab Scientist - St. Louis US",
        "Do Not Apply: Data Analyst",
        "[DO NOT APPLY] Product Manager",
        "TEST REQ - Senior Scientist",
        "Test Requisition | Program Manager",
        "Product Owner (test req)",
        "test requisition - do not apply",
    ],
)
def test_a_publisher_test_requisition_is_refused(title: str) -> None:
    screening = screen_discovery(
        "Acme",
        title,
        "Freedonia",
        home_market_search(title),
        [title],
        published_location_facts("Freedonia"),
    )

    assert screening.standing is ScreeningStanding.REFUSE
    assert screening.findings[0].code is ScreeningCode.TITLE_TEST_REQUISITION
    assert refusal_text(screening.findings[0], home_market_search(title)).startswith(
        "The employer marks this posting as a test requisition: "
    )


@pytest.mark.parametrize(
    "title",
    [
        "Principal Software Development Engineer in Test",
        "Test Manager, JR102, JR181",
        "Test Automation Engineer",
        "QA Test Lead",
        "Requisition Coordinator",
        "Apply Now: Data Engineer",
        "Clinical Test Development Scientist",
        "Software Engineer, Testing Infrastructure",
    ],
)
def test_a_title_naming_testing_work_is_not_a_test_requisition(title: str) -> None:
    screening = screen_discovery(
        "Globex",
        title,
        "Freedonia",
        home_market_search(title),
        [title],
        published_location_facts("Freedonia"),
    )

    assert screening.findings[0].code is ScreeningCode.TITLE_MATCH
