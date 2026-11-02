"""Which country a board's place string names, and when it names none.

Screening asks its own declared places first, so a table that answers wrongly
can stay hidden there. These read the table directly.
"""

from __future__ import annotations

import pytest

from tailorcv.domain import countries
from tailorcv.domain.places import PlaceReader


@pytest.mark.parametrize(
    ("place", "expected"),
    [
        ("Islamabad, PK", "Pakistan"),
        ("Sofia, BG", "Bulgaria"),
        ("PH - QUEZON CITY - GBF CENTER 2", "Philippines"),
        ("New York, NY", "United States"),
        ("Boston, Massachusetts", "United States"),
        ("Waterloo, Ontario", "Canada"),
        ("Newark, New Jersey", "United States"),
        ("Santa Fe, New Mexico", "United States"),
        ("Juba, South Sudan", "South Sudan"),
        ("Port Moresby, Papua New Guinea", "Papua New Guinea"),
        ("anywhere in the US", "United States"),
    ],
)
def test_one_named_place_answers_with_its_country(place: str, expected: str) -> None:
    assert countries.DEFAULT_TABLE.named_in(place) == expected


@pytest.mark.parametrize(
    "place",
    [
        "San Francisco, CA",
        "Regina, SK",
        "Atlanta, Georgia",
        "Remote",
        "IN - GURUGRAM DLF DOWNTOWN BLOCK 4 FL 11&12",
        "Hiring in Sudan and South Sudan",
    ],
)
def test_a_place_two_countries_could_claim_answers_with_none(place: str) -> None:
    assert countries.DEFAULT_TABLE.named_in(place) == ""


@pytest.mark.parametrize(
    ("place", "explicit", "inferred"),
    [
        ("San Francisco, CA", ("United States",), ()),
        ("Regina, SK", ("Canada",), ()),
        ("Atlanta, Georgia", ("United States",), ("United States",)),
        ("IN - GURUGRAM DLF DOWNTOWN BLOCK 4 FL 11&12", (), ("India",)),
    ],
)
def test_the_place_reader_resolves_what_the_table_leaves_unclaimed(
    place: str, explicit: tuple[str, ...], inferred: tuple[str, ...]
) -> None:
    reading = PlaceReader(countries.DEFAULT_TABLE).read(place)
    assert (reading.explicit, reading.inferred) == (explicit, inferred)


@pytest.mark.parametrize(
    ("place", "expected"),
    [
        ("Ankara, Turkey", "T\N{LATIN SMALL LETTER U WITH DIAERESIS}rkiye"),
        ("Ankara, Turkiye", "T\N{LATIN SMALL LETTER U WITH DIAERESIS}rkiye"),
        ("Riyadh, KSA", "Saudi Arabia"),
        ("Remote, UAE", "United Arab Emirates"),
    ],
)
def test_common_board_spellings_of_three_countries_resolve(place: str, expected: str) -> None:
    assert countries.DEFAULT_TABLE.named_in(place) == expected


@pytest.mark.parametrize(
    ("place", "expected"),
    [
        ("Russian Federation", "Russia"),
        ("Viet Nam", "Vietnam"),
        ("Slovak Republic", "Slovakia"),
    ],
)
def test_official_state_names_read_as_their_country(place: str, expected: str) -> None:
    assert countries.DEFAULT_TABLE.named_in(place) == expected


def test_a_country_abbreviation_inside_a_place_name_names_no_country() -> None:
    """A board writes the Dutch article in a town, not a country code."""
    assert countries.DEFAULT_TABLE.names_in("NLD - Alphen Aan Den Rijn") == ("Netherlands",)


@pytest.mark.parametrize("nation", ["England", "Scotland", "Wales", "Northern Ireland"])
def test_a_constituent_nation_resolves_to_the_united_kingdom(nation: str) -> None:
    assert countries.DEFAULT_TABLE.names_in(f"Anytown, {nation}") == ("United Kingdom",)


def test_new_england_names_no_country() -> None:
    assert countries.DEFAULT_TABLE.names_in("New England") == ()
    assert countries.DEFAULT_TABLE.names_in("Remote, New England") == ()
    assert countries.DEFAULT_TABLE.names_in("London, England") == ("United Kingdom",)
    assert countries.DEFAULT_TABLE.names_in("New England, UK") == ("United Kingdom",)


@pytest.mark.parametrize(
    "text",
    [
        "we can relocate you",
        "let us know",
        "the pol team",
        "nor do we",
        "Toronto, can relocate",
    ],
)
def test_an_english_word_spelled_like_a_country_code_names_no_country(text: str) -> None:
    assert countries.DEFAULT_TABLE.names_in(text) == ()


@pytest.mark.parametrize(
    ("text", "country"),
    [
        ("Toronto, CAN", "Canada"),
        ("Remote, Can", "Canada"),
        ("CAN - Toronto", "Canada"),
        ("Warsaw, Pol", "Poland"),
        ("Remote (US)", "United States"),
        ("must reside in the US", "United States"),
        ("must work in the uk", "United Kingdom"),
        ("Remote - us", "United States"),
    ],
)
def test_a_country_code_reads_as_a_code_or_in_a_place_slot(text: str, country: str) -> None:
    assert countries.DEFAULT_TABLE.names_in(text) == (country,)


_TITLE_ACRONYMS = ("AI", "IT", "PR", "HR", "QA", "ML", "PM", "BA", "BI", "SA", "NP", "PT", "TL")
_TITLE_SHAPES = (
    "{} Product Manager",
    "{} Powered Product Manager",
    "Senior Analyst, {} Operations",
    "Director, {} Strategy (Remote)",
    "{}/UX Designer",
    "Senior PM, {}/Data",
    "Product Manager ({})",
    "Data Scientist, {}.",
)


@pytest.mark.parametrize(
    "text",
    [shape.format(acronym) for acronym in _TITLE_ACRONYMS for shape in _TITLE_SHAPES],
)
def test_a_title_acronym_spelled_like_a_country_code_names_no_country(text: str) -> None:
    assert countries.DEFAULT_TABLE.names_in(text) == ()
    assert PlaceReader(countries.DEFAULT_TABLE).read(text).explicit == ()


@pytest.mark.parametrize(
    ("place", "country"),
    [
        ("Milan, IT", "Italy"),
        ("IT - Milan", "Italy"),
        ("IT, Milan", "Italy"),
        ("IT", "Italy"),
        ("Zagreb, HR", "Croatia"),
        ("Doha, QA (Hybrid)", "Qatar"),
        ("Lisbon, PT 1000-001", "Portugal"),
        ("Islamabad, PK 44000", "Pakistan"),
        ("PK - Lahore - Gulberg", "Pakistan"),
        ("Remote, PK - Hybrid", "Pakistan"),
        ("Anytownx, PK - Hybrid", "Pakistan"),
        ("PK Remote", "Pakistan"),
        ("PL | Remote", "Poland"),
        ("PL (Remote)", "Poland"),
        ("Warsaw, PL & Remote", "Poland"),
        ("PT / Remote", "Portugal"),
        ("Remote, BR - CLT", "Brazil"),
        ("Remote, MX | Full-time", "Mexico"),
        ("Remote, IT - Full time", "Italy"),
        ("Milan, IT - Hybrid", "Italy"),
        ("IT | Milan", "Italy"),
        ("Doha, QA - Onsite", "Qatar"),
        ("Austin, TX - Remote", "United States"),
        ("Toronto, ON / Remote", "Canada"),
    ],
)
def test_a_code_that_fills_its_place_slot_names_its_country(place: str, country: str) -> None:
    assert countries.DEFAULT_TABLE.names_in(place) == (country,)
    assert PlaceReader(countries.DEFAULT_TABLE).read(place).explicit == (country,)


@pytest.mark.parametrize(
    "text",
    [
        "us/canada",
        "Canada / us",
        "Remote, us & canada",
        "Canada or us",
        "Candidates must be based in the us or canada.",
    ],
)
def test_a_lowercase_code_listed_beside_a_country_still_names_its_country(text: str) -> None:
    assert countries.DEFAULT_TABLE.names_in(text) == ("Canada", "United States")


def test_a_code_word_that_continues_the_sentence_is_not_listed() -> None:
    sentence = "You must be based in Germany or can relocate there."
    assert countries.DEFAULT_TABLE.names_in(sentence) == ("Germany",)


@pytest.mark.parametrize(
    ("code", "sovereign"),
    [
        ("PR", "US"),
        ("GU", "US"),
        ("VI", "US"),
        ("AS", "US"),
        ("MP", "US"),
        ("MQ", "FR"),
        ("GP", "FR"),
        ("GF", "FR"),
        ("RE", "FR"),
        ("YT", "FR"),
        ("IC", "ES"),
    ],
)
def test_every_modeled_territory_resolves_to_a_maintained_name_and_sovereign(
    code: str, sovereign: str
) -> None:
    name = countries.territory_name(code)
    assert name is not None
    assert name in countries.DEFAULT_NAMES
    assert countries.territory_sovereign(name) == countries.territory_name(sovereign)
    assert countries.territory_sovereign(name) in countries.DEFAULT_NAMES


def test_a_country_spelled_in_the_street_after_a_leading_country_is_a_street() -> None:
    """Workday leads with the country; the slot after the last comma is the address."""
    assert countries.DEFAULT_TABLE.names_in("GBR - London, Canada Square") == ("United Kingdom",)
    assert countries.DEFAULT_TABLE.names_in("  GBR - London, Canada Square") == ("United Kingdom",)
    assert countries.DEFAULT_TABLE.names_in("USA - New York, Jersey Street") == ("United States",)


@pytest.mark.parametrize(
    "place",
    ["United States, Canada", "Remote - Canada, United States", "US - Remote or Canada"],
)
def test_two_countries_outside_the_street_slot_are_both_named(place: str) -> None:
    assert countries.DEFAULT_TABLE.names_in(place) == ("Canada", "United States")


@pytest.mark.parametrize("stub", ["Co", "To", "Al", "In", " Co "])
def test_a_mixed_case_pair_is_never_a_territory_code(stub: str) -> None:
    assert countries.territory_name(stub) is None


@pytest.mark.parametrize(("code", "name"), [("CO", "Colombia"), ("co", "Colombia")])
def test_a_one_case_pair_still_reads_as_a_territory_code(code: str, name: str) -> None:
    assert countries.territory_name(code) == name
