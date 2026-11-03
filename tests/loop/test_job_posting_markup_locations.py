"""Every place a JobPosting states, and whether that list is honestly complete."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import pytest

from clearscout.application.discovery.screening import ScreeningCode, screen_discovery
from clearscout.domain.search_parameters import (
    CountryNames,
    JobSearchParameters,
    Location,
    Seniority,
)
from clearscout.domain.staged_facts import ListingIdentityTrust
from clearscout.infrastructure.web_fetch.board_adapters import BoardApiFetcher
from clearscout.infrastructure.web_fetch.board_mappings.schema_org import declared_check
from clearscout.infrastructure.web_fetch.ingest.job_posting_markup import job_postings
from clearscout.infrastructure.web_fetch.publisher_sources import PublisherSourceFetcher
from tests.support.scenarios import titles

URL = "https://careers.example-health.test/jobs/42"


def _page(**fields: Any) -> str:
    posting = {"@type": "JobPosting", "title": "Logistics Data Analyst", "url": URL, **fields}
    return f'<script type="application/ld+json">{json.dumps(posting)}</script>'


def _address(locality: str, region: str, country: str = "US") -> dict[str, str]:
    return {
        "@type": "PostalAddress",
        "addressLocality": locality,
        "addressRegion": region,
        "addressCountry": country,
    }


class _Pages:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages

    def get(self, url: str, **_kwargs: Any) -> httpx.Response:
        status = 200 if url in self.pages else 404
        return httpx.Response(
            status, text=self.pages.get(url, ""), request=httpx.Request("GET", url)
        )


def test_a_place_with_a_list_of_addresses_states_each_one() -> None:
    page = _page(
        jobLocation={
            "@type": "Place",
            "address": [_address("Springfield", "IL"), _address("Shelbyville", "IL")],
        }
    )

    (posting,) = job_postings(page)

    assert posting.locations == (
        "Springfield, IL, United States",
        "Shelbyville, IL, United States",
    )
    assert posting.locations_complete is True


def test_a_truncated_mixed_case_country_stays_a_word_and_never_becomes_a_code() -> None:
    page = _page(
        jobLocation={
            "@type": "Place",
            "address": [_address("Shinagawa", "Japa", "To"), _address("Ingelheim", "Germ", "Rh")],
        }
    )

    (posting,) = job_postings(page)

    assert posting.locations == ("Shinagawa, Japan", "Ingelheim, Germany")


def test_a_place_stated_only_by_name_keeps_that_name() -> None:
    (posting,) = job_postings(_page(jobLocation={"@type": "Place", "name": "Springfield, IL"}))

    assert posting.locations == ("Springfield, IL",)
    assert posting.location == "Springfield, IL"
    assert posting.locations_complete is True


def test_a_name_beside_an_unreadable_address_is_kept_but_never_complete() -> None:
    page = _page(
        jobLocation={
            "@type": "Place",
            "name": "Example Health Campus",
            "address": {"streetAddress": "100 Main Street"},
        }
    )

    (posting,) = job_postings(page)

    assert posting.locations == ("Example Health Campus",)
    assert posting.locations_complete is False


def test_a_street_address_alone_names_no_place_and_is_never_complete() -> None:
    page = _page(jobLocation={"address": {"streetAddress": "100 Main Street"}})

    (posting,) = job_postings(page)

    assert posting.locations == ()
    assert posting.locations_complete is False


def test_every_entry_of_a_location_list_is_kept_and_the_list_is_complete() -> None:
    page = _page(
        jobLocation=[
            {"@type": "Place", "address": _address("Springfield", "IL")},
            {"@type": "Place", "address": _address("Capital City", "OR")},
        ]
    )

    (posting,) = job_postings(page)

    assert posting.locations == (
        "Springfield, IL, United States",
        "Capital City, OR, United States",
    )
    assert posting.location == "Springfield, IL, United States"
    assert posting.locations_complete is True


def test_one_stated_place_is_the_location_exactly_as_the_place_reads() -> None:
    page = _page(jobLocation={"@type": "Place", "address": _address("Springfield", "IL")})

    (posting,) = job_postings(page)

    assert posting.locations == ("Springfield, IL, United States",)
    assert posting.location == "Springfield, IL, United States"


def test_a_posting_open_in_many_countries_states_one_place_not_all_of_them() -> None:
    countries = ["Freedonia", "Ruritania", "Sylvania", "Borduria", "Syldavia"]
    page = _page(
        jobLocationType="TELECOMMUTE",
        applicantLocationRequirements=[{"@type": "Country", "name": name} for name in countries],
    )

    (posting,) = job_postings(page)

    assert posting.locations == tuple(f"Remote, {name}" for name in countries)
    assert posting.location == "Remote, Freedonia"


def test_a_region_repeating_the_locality_does_not_spell_the_place_twice() -> None:
    page = _page(
        jobLocation=[
            {"@type": "Place", "address": _address("Springfield", "SPRINGFIELD")},
            {"@type": "Place", "address": _address("Capital City", "Capital City")},
        ]
    )

    (posting,) = job_postings(page)

    assert posting.locations == (
        "Springfield, United States",
        "Capital City, United States",
    )
    assert posting.location == "Springfield, United States"


def test_a_locality_the_region_only_contains_keeps_its_own_shorter_name() -> None:
    page = _page(
        jobLocation=[
            {"@type": "Place", "address": _address("Springfield", "Springfield Heights")},
            {"@type": "Place", "address": _address("Capital", "Capital City")},
        ]
    )

    (posting,) = job_postings(page)

    assert posting.locations == (
        "Springfield, Springfield Heights, United States",
        "Capital, Capital City, United States",
    )


def test_a_region_the_locality_contains_is_kept_so_two_places_never_compare_as_one() -> None:
    page = _page(
        jobLocation=[
            {"@type": "Place", "address": _address("Ogdenville City", "Ogdenville")},
            {"@type": "Place", "address": _address("Ogdenville City", "North Haverbrook")},
        ]
    )

    (posting,) = job_postings(page)

    assert posting.locations == (
        "Ogdenville City, Ogdenville, United States",
        "Ogdenville City, North Haverbrook, United States",
    )
    assert posting.location == "Ogdenville City, Ogdenville, United States"


def test_screening_reads_every_place_of_a_posting_whose_first_place_is_foreign() -> None:
    """Screening falls back to the location text, so the staged fact must state every place."""
    page = _page(
        hiringOrganization={"name": "Example Health"},
        jobLocation=[
            {"@type": "Place", "address": _address("Southport", "", "Germany")},
            {"@type": "Place", "address": _address("Riverton", "", "Canada")},
        ],
    )
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="Canada")],
        countries=[CountryNames(country="Canada")],
        seniority=Seniority(target_titles=titles("Logistics Data Analyst")),
    )

    (posting,) = job_postings(page)
    check = declared_check(posting, page_url=URL, read_at=datetime(2026, 1, 5, tzinfo=UTC))
    screening = screen_discovery(
        check.employer,
        check.title,
        check.location,
        parameters,
        ["Logistics Data Analyst"],
        check.facts,
    )

    assert check.facts.locations.values == ("Southport, Germany", "Riverton, Canada")
    assert screening.findings[1].code is ScreeningCode.LOCATION_MATCH


def test_one_unreadable_entry_leaves_the_list_incomplete() -> None:
    page = _page(
        jobLocation=[
            {"@type": "Place", "address": _address("Springfield", "IL")},
            {"@type": "Place", "address": {"streetAddress": "9 Elm Road"}},
        ]
    )

    (posting,) = job_postings(page)

    assert posting.locations == ("Springfield, IL, United States",)
    assert posting.locations_complete is False


def test_an_address_country_code_is_rendered_as_the_country() -> None:
    page = _page(
        jobLocation=[
            {"address": {"addressLocality": "Springfield", "addressCountry": "IN"}},
            {"address": {"addressLocality": "Shelbyville", "addressRegion": "IN"}},
            {"address": {"addressLocality": "Capital City", "addressCountry": "CA"}},
            {"address": _address("Ogdenville", "CA")},
            {"address": {"addressLocality": "North Haverbrook", "addressCountry": "DEU"}},
        ]
    )

    (posting,) = job_postings(page)

    assert posting.locations == (
        "Springfield, India",
        "Shelbyville, IN",
        "Capital City, Canada",
        "Ogdenville, CA, United States",
        "North Haverbrook, Germany",
    )


def test_telecommute_with_applicant_requirements_states_remote_places_never_complete() -> None:
    page = _page(
        jobLocationType="TELECOMMUTE",
        applicantLocationRequirements=[
            {"@type": "Country", "name": "United States"},
            {"@type": "State", "name": "Ontario"},
        ],
    )

    (posting,) = job_postings(page)

    assert posting.locations == ("Remote, United States", "Remote, Ontario")
    assert posting.locations_complete is False


def test_a_remote_country_stated_as_a_code_is_rendered_as_the_country() -> None:
    page = _page(
        jobLocationType="TELECOMMUTE",
        applicantLocationRequirements=[
            {"@type": "Country", "name": "DE"},
            "CA",
            {"name": "US"},
            {"@type": "State", "name": "DE"},
        ],
    )

    (posting,) = job_postings(page)

    assert posting.locations == (
        "Remote, Germany",
        "Remote, Canada",
        "Remote, United States",
        "Remote, DE",
    )
    assert posting.locations_complete is False


def test_telecommute_beside_an_office_address_is_remote_and_never_complete() -> None:
    for kind in ("TELECOMMUTE", "https://schema.org/Telecommute"):
        page = _page(
            jobLocationType=kind,
            jobLocation={"@type": "Place", "address": _address("Springfield", "IL")},
        )

        (posting,) = job_postings(page)

        assert posting.locations == ("Springfield, IL, United States", "Remote"), kind
        assert posting.locations_complete is False, kind


def test_applicant_requirements_alone_leave_the_list_incomplete() -> None:
    page = _page(
        jobLocation={"@type": "Place", "address": _address("Springfield", "IL")},
        applicantLocationRequirements={"@type": "Country", "name": "United States"},
    )

    (posting,) = job_postings(page)

    assert posting.locations == ("Springfield, IL, United States",)
    assert posting.locations_complete is False


def test_a_crawled_declared_posting_stages_every_place_and_the_honest_completeness() -> None:
    complete = _page(
        jobLocation=[
            {"@type": "Place", "address": _address("Springfield", "IL")},
            {"@type": "Place", "name": "Shelbyville, IL"},
        ]
    )
    remote = _page(jobLocationType="TELECOMMUTE", applicantLocationRequirements="United States")
    fetcher = PublisherSourceFetcher(client=cast(Any, object()))

    (complete_listing,) = fetcher.read("Example Health", URL, complete).listings
    (remote_listing,) = fetcher.read("Example Health", URL, remote).listings

    assert complete_listing.facts.locations.values == (
        "Springfield, IL, United States",
        "Shelbyville, IL",
    )
    assert complete_listing.facts.locations.complete is True
    assert remote_listing.facts.locations.values == ("Remote, United States",)
    assert remote_listing.facts.locations.complete is False


def test_a_careers_page_declaring_its_posting_stages_every_place() -> None:
    careers = "https://careers.example-health.test/"
    page = _page(
        jobLocation=[
            {"@type": "Place", "address": _address("Springfield", "IL")},
            {"@type": "Place", "address": _address("Capital City", "", "IL")},
        ]
    )

    read = BoardApiFetcher(client=cast(Any, _Pages({careers: page}))).listings(
        "Example Health", careers
    )

    (listing,) = read.listings
    assert listing.facts.locations.values == (
        "Springfield, IL, United States",
        "Capital City, Israel",
    )
    assert listing.facts.locations.complete is True


def test_a_us_town_beside_a_truncated_country_word_never_reads_as_foreign() -> None:
    # A SuccessFactors template states "Ridgefield, CT" with "Unit" and "Co" cut from words.
    page = (
        '<div class="jobDisplayShell" itemscope itemtype="http://schema.org/JobPosting">'
        '<meta itemprop="title" content="Logistics Data Analyst">'
        '<span itemprop="jobLocation" itemscope itemtype="http://schema.org/Place">'
        '<span itemprop="address" itemscope itemtype="http://schema.org/PostalAddress">'
        '<meta itemprop="addressLocality" content="Ridgefield, CT">'
        '<meta itemprop="addressRegion" content="Unit">'
        '<meta itemprop="addressCountry" content="Co"></span></span>'
        '<span itemprop="description">Model shipment data.</span></div>'
    )
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="United States")],
        countries=[CountryNames(country="United States")],
        seniority=Seniority(target_titles=titles("Logistics Data Analyst")),
    )

    (posting,) = job_postings(page)
    check = declared_check(posting, page_url=URL, read_at=datetime(2026, 1, 5, tzinfo=UTC))
    screening = screen_discovery(
        "Example Health",
        check.title,
        check.location,
        parameters,
        ["Logistics Data Analyst"],
        check.facts,
    )

    assert check.facts.locations.values == ("Ridgefield, CT, Unit, Co",)
    assert screening.findings[1].code is not ScreeningCode.LOCATION_OUTSIDE


@pytest.mark.parametrize(
    ("locality", "region", "country", "place", "outside"),
    [
        ("Shinagawa", "Japa", "To", "Shinagawa, Japan", True),
        ("Biberach", "Germ", "Ba", "Biberach, Germany", True),
        ("Manila", "Phil", "Na", "Manila, Philippines", True),
        # "Aust" names neither Australia nor Austria; Vienna suggests Austria, foreign either way.
        ("Vienna", "Aust", "Vi", "Vienna, Aust, Vi", True),
        ("Ridgefield, CT", "Unit", "Co", "Ridgefield, CT, Unit, Co", False),
    ],
)
def test_a_country_word_truncated_into_the_region_slot_names_its_one_country(
    locality: str, region: str, country: str, place: str, outside: bool | None
) -> None:
    # SuccessFactors cuts the country to four letters into addressRegion and the
    # region to two into addressCountry.
    markup = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Logistics Data Analyst",
        "description": "Model shipment data.",
        "hiringOrganization": "ExamplePRD",
        "jobLocation": {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "addressLocality": locality,
                "addressRegion": region,
                "addressCountry": country,
            },
        },
    }
    page = f'<script type="application/ld+json">{json.dumps(markup)}</script>'
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="United States")],
        countries=[CountryNames(country="United States")],
        seniority=Seniority(target_titles=titles("Logistics Data Analyst")),
    )

    (posting,) = job_postings(page)
    check = declared_check(posting, page_url=URL, read_at=datetime(2026, 1, 5, tzinfo=UTC))
    # The employer's own board vouches for its page, as a registered source read does.
    facts = replace(check.facts, identity_trust=ListingIdentityTrust.PUBLISHER)
    screening = screen_discovery(
        "Example Health",
        check.title,
        check.location,
        parameters,
        ["Logistics Data Analyst"],
        facts,
    )

    assert check.facts.locations.values == (place,)
    code = screening.findings[1].code
    if outside:
        assert code is ScreeningCode.LOCATION_OUTSIDE
    else:
        assert code is not ScreeningCode.LOCATION_OUTSIDE
