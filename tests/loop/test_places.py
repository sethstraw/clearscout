"""What the place reader adds to the country table, and what it only suggests."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from importlib import resources
from itertools import product
from pathlib import Path

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from tailorcv.domain.countries import DEFAULT_TABLE
from tailorcv.domain.places import PlaceReader, PlaceReading

READER = PlaceReader(DEFAULT_TABLE)


@pytest.mark.parametrize(
    ("place", "country"),
    [
        ("Wilmington, DE", "United States"),
        ("Jersey City, N.J.", "United States"),
        ("Pittsburgh, Pa.", "United States"),
        ("Halifax, N.S.", "Canada"),
        ("Naperville, IL", "United States"),
        ("Naperville IL 60540", "United States"),
        ("Naperville, IL 60540-1234", "United States"),
        ("Lebanon, TN", "United States"),
        ("Jamaica Plain, MA", "United States"),
        ("Panama City, FL", "United States"),
        ("Mobile, AL", "United States"),
        ("Washington, DC", "United States"),
        ("San Francisco, CA (Remote)", "United States"),
        ("Kitchener, ON", "Canada"),
        ("Moncton, NB E1C 4X9", "Canada"),
        ("Montr\N{LATIN SMALL LETTER E WITH ACUTE}al, QC", "Canada"),
    ],
)
def test_a_verified_city_and_subdivision_name_its_country(place: str, country: str) -> None:
    assert READER.read(place).explicit == (country,)


def test_an_unknown_leading_word_does_not_stop_the_city_from_verifying() -> None:
    assert READER.read("Downtown Naperville, IL").explicit == ("United States",)


@pytest.mark.parametrize(
    "place",
    [
        "Berlin, DE",
        "Munich, DE 80331",
        "Jakarta, ID 10110",
        "Pune, IN",
        "Toronto, CA",
        "New Westminster, CA",
        "Sorel Tracy, CA",
    ],
)
def test_a_colliding_code_with_a_foreign_city_is_never_north_american(place: str) -> None:
    reading = READER.read(place)
    assert reading.explicit == ()
    assert "United States" not in reading.inferred


@pytest.mark.parametrize(
    "place", ["Jersey City, nj", "Austin, tx", "Stra\N{LATIN SMALL LETTER SHARP S}e 5, Mexico, MO"]
)
def test_a_trailing_code_that_cannot_verify_still_reads_as_an_unverified_subdivision(
    place: str,
) -> None:
    assert READER.read(place).unverified_subdivision


@pytest.mark.parametrize(
    ("place", "locality"),
    [
        ("Hyderabad - TS - IN", True),
        ("Pune - MH - IN", True),
        ("Kiryat Ono, IL", True),
        ("Fictionville, IN", False),
        ("Fictionville, IN 46000", False),
    ],
)
def test_an_unverified_code_reads_whether_its_town_is_a_known_city(
    place: str, locality: bool
) -> None:
    reading = READER.read(place)
    assert reading.unverified_subdivision
    assert reading.locality is locality


@pytest.mark.parametrize("place", ["Richmond, CA", "Windsor, CA"])
def test_a_city_in_both_the_state_and_the_colliding_country_only_suggests_both(
    place: str,
) -> None:
    assert READER.read(place) == PlaceReading(
        explicit=(),
        inferred=("Canada", "United States"),
        unverified_subdivision=True,
        unverified_code_countries=("Canada",),
        locality=True,
    )


@pytest.mark.parametrize(
    ("place", "country"),
    [
        (f"{code}{separator}{town}{tail}", country)
        for code, town, country in (
            ("GA", "Atlanta", "United States"),
            ("PA", "Reading", "United States"),
            ("DE", "Wilmington", "United States"),
            ("IN", "Indianapolis", "United States"),
            ("ON", "Toronto", "Canada"),
        )
        for separator in (" - ", "-", " -", "- ", " \N{EN DASH} ", "\N{EM DASH}")
        for tail in ("", f", {country}")
    ],
)
def test_a_leading_state_code_verifies_the_town_after_it(place: str, country: str) -> None:
    assert READER.read(place).explicit == (country,)


@pytest.mark.parametrize("separator", [" - ", "-", " \N{EN DASH} "])
def test_a_leading_state_code_over_an_unindexed_town_reads_as_an_unverified_subdivision(
    separator: str,
) -> None:
    reading = READER.read(f"VA{separator}Chesterfield")
    assert reading.explicit == ()
    assert reading.unverified_subdivision


@pytest.mark.parametrize(
    ("place", "foreign"),
    [
        ("ga - atlanta", "Gabon"),
        ("pa-reading", "Panama"),
        ("va - chesterfield", "Vatican City"),
        ("VA - Chesterfield, United States", "Vatican City"),
    ],
)
def test_a_leading_state_code_never_names_its_namesake_country(place: str, foreign: str) -> None:
    reading = READER.read(place)
    assert foreign not in (*reading.explicit, *reading.inferred)


@pytest.mark.parametrize(
    ("place", "country"),
    [
        ("FR - Paris", "France"),
        ("IN - Chennai", "India"),
        ("DE - Berlin", "Germany"),
        ("CA - Toronto", "Canada"),
    ],
)
def test_a_leading_country_code_before_its_own_city_is_never_north_american(
    place: str, country: str
) -> None:
    reading = READER.read(place)
    assert "United States" not in (*reading.explicit, *reading.inferred)
    assert country in (*reading.explicit, *reading.inferred)


@pytest.mark.parametrize(
    "place", ["ON-SITE", "IN - Office", "VA - Remote", "CA - Fictionville", "DE - Home Office"]
)
def test_a_leading_code_before_no_indexed_town_is_not_read_as_a_subdivision(place: str) -> None:
    assert READER.read(place) == PlaceReading()


@pytest.mark.parametrize(
    ("place", "country"),
    [
        ("IL - Chicago", "United States"),
        ("CO-Denver", "United States"),
        ("US - Boston - MA", "United States"),
        ("UK - London", "United Kingdom"),
    ],
)
def test_a_leading_code_before_an_indexed_city_keeps_its_reading(place: str, country: str) -> None:
    assert READER.read(place).explicit == (country,)


@pytest.mark.parametrize(
    ("place", "country"),
    [
        ("CA - Toronto", "Canada"),
        ("IN - Bengaluru", "India"),
        ("DE - Berlin", "Germany"),
        ("PE - Lima", "Peru"),
        ("AL - Tirana", "Albania"),
        ("IL - Tel Aviv", "Israel"),
    ],
)
def test_a_leading_code_before_its_own_country_city_stays_that_country(
    place: str, country: str
) -> None:
    reading = READER.read(place)
    assert "United States" not in (*reading.explicit, *reading.inferred)
    assert reading.inferred == (country,)


@pytest.mark.parametrize(
    "place", ["ON-SITE", "IN-OFFICE", "il - work from home", "IL - Remote, Israel"]
)
def test_a_code_without_a_spaced_dash_or_beside_a_country_is_no_us_state(place: str) -> None:
    assert "United States" not in READER.read(place).explicit


@pytest.mark.parametrize(
    ("place", "reading"),
    [
        (
            "FL - Panama City Beach",
            PlaceReading(unverified_subdivision=True, locality=True),
        ),
        (
            "Panama City Beach, FL",
            PlaceReading(explicit=("United States",), unverified_subdivision=True, locality=True),
        ),
        ("Panama City Beach, Florida", PlaceReading(explicit=("United States",), locality=True)),
        ("FL - Panama City Beach - Remote", PlaceReading(locality=True)),
        (
            "Panama City Beach, FL, United States",
            PlaceReading(explicit=("United States",), locality=True),
        ),
        (
            "Panama City or Tampa, FL",
            PlaceReading(explicit=("Panama", "United States"), locality=True),
        ),
        (
            "Panama City / Tampa, FL",
            PlaceReading(explicit=("Panama", "United States"), locality=True),
        ),
    ],
)
def test_a_town_under_a_us_state_never_names_the_country_in_its_name(
    place: str, reading: PlaceReading
) -> None:
    assert READER.read(place) == reading


def test_panama_city_beside_panama_still_names_panama() -> None:
    assert READER.read("Panama City, Panama").explicit == ("Panama",)


@pytest.mark.parametrize(
    "place", ["Victoria", "Durham", "Cambridge", "Kingston", "Hamilton", "Waterloo", "Mobile"]
)
def test_an_ambiguous_or_common_bare_name_suggests_nothing(place: str) -> None:
    assert READER.read(place) == PlaceReading(locality=True)


@pytest.mark.parametrize(
    ("place", "country"),
    [
        ("Boston", "United States"),
        ("Paris", "France"),
        ("London", "United Kingdom"),
        ("Laredo", "United States"),
        ("Surrey", "Canada"),
        ("Greenwich", "United Kingdom"),
        ("Sunderland", "United Kingdom"),
    ],
)
def test_a_unique_or_dominant_bare_name_only_suggests_its_country(place: str, country: str) -> None:
    assert READER.read(place) == PlaceReading(inferred=(country,), locality=True)


def test_a_city_just_under_ten_times_larger_suggests_nothing() -> None:
    assert READER.read("Coventry") == PlaceReading(locality=True)


def test_a_bare_foreign_name_never_hides_a_unique_state_code() -> None:
    reading = READER.read("Greenwich, CT")
    assert reading.explicit == ("United States",)
    assert reading.inferred == ("United Kingdom",)


@pytest.mark.parametrize(
    "place",
    [
        "Jersey City",
        "Jersey City, nj - Hybrid",
        "New Britain",
        "Lake Wales",
        "Jamaica Plain",
    ],
)
def test_a_country_word_inside_a_domestic_city_names_no_country(place: str) -> None:
    reading = READER.read(place)
    assert reading.explicit == ()
    assert reading.outside({"united states"}) == ()


@pytest.mark.parametrize("place", ["Located in So Uk Estate", "bel air office"])
def test_a_lowercase_code_word_inside_a_place_names_no_country(place: str) -> None:
    assert READER.read(place).explicit == ()


@pytest.mark.parametrize("place", ["Jersey City / Toronto, Canada", "Chicago / Toronto, Canada"])
def test_a_domestic_city_listed_beside_a_foreign_one_still_counts_as_saved(place: str) -> None:
    assert READER.read(place).outside({"united states"}) == ()


def test_a_qualifying_state_name_still_verifies_over_the_city_s_own_country_word() -> None:
    assert READER.read("Jersey City, New Jersey").explicit == ("United States",)


@pytest.mark.parametrize(
    ("place", "country"),
    [
        ("Mexico City", "Mexico"),
        ("Panama City", "Panama"),
        ("Fort-de-France, Martinique", "Martinique"),
        ("St Helier, Jersey", "Jersey"),
        ("Saint Helier, Jersey", "Jersey"),
        ("Belfast, Northern Ireland", "United Kingdom"),
    ],
)
def test_a_foreign_city_carrying_its_own_country_word_still_names_it(
    place: str, country: str
) -> None:
    assert country in READER.read(place).explicit


@pytest.mark.parametrize(
    ("place", "country"),
    [
        ("Rotterdam, South Holland", "Netherlands"),
        ("The Hague, South Holland", "Netherlands"),
        ("South Holland, Netherlands", "Netherlands"),
        ("Rotterdam, South Holland, Netherlands", "Netherlands"),
        ("Beirut, Mount Lebanon", "Lebanon"),
        ("Mount Lebanon, Lebanon", "Lebanon"),
    ],
)
def test_a_region_sharing_a_domestic_town_name_keeps_the_country_the_text_corroborates(
    place: str, country: str
) -> None:
    assert READER.read(place).outside({"united states"}) == (country,)


def test_a_suggested_saved_country_stops_an_outside_reading() -> None:
    assert READER.read("Boston, Lincolnshire, England").outside({"united kingdom"}) == ()
    assert READER.read("Boston, Lincolnshire, England").outside({"united states"}) == ()
    assert READER.read("Leeds, England").outside({"united states"}) == ("United Kingdom",)


_WORDS = st.sampled_from(
    [
        "lebanon",
        "boston",
        "georgia",
        "ontario",
        "toronto",
        "us",
        "remote",
        "new jersey",
        "PK",
        "BG",
        "GBR",
        "canada square",
    ]
)


# Dashed town/state slots have contextual rulings beyond the country-name table.
_DASHED_STATE_FORM = re.compile(
    r"(?:(?:^|[, ])[^,-]+(?: -|,) (?:georgia|new jersey)|(?:georgia|new jersey)(?: -|,) [^,-]+)"
)


@given(st.lists(st.tuples(_WORDS, st.sampled_from([", ", " - ", " "])), max_size=5))
def test_without_a_qualified_form_or_a_wider_city_the_reader_names_what_the_table_names(
    parts: list[tuple[str, str]],
) -> None:
    text = "".join(word + separator for word, separator in parts)
    assume(_DASHED_STATE_FORM.search(text) is None)
    assert READER.read(text).explicit == DEFAULT_TABLE.names_in(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Round Rock, TX", "Texas"),
        ("Boston", "Massachusetts"),
        ("Durham, NC", "North Carolina"),
        ("Winnipeg, Manitoba", "Manitoba"),
    ],
)
def test_region_of_resolves_a_single_named_city(text: str, expected: str) -> None:
    """A qualifying subdivision spelled out in full must not itself count as a second city."""
    assert READER.region_of(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Round Rock, TX or Cedar Park, TX",
        "Austin, TX or Dallas, TX",
        "Winnipeg, MB; Halifax, NS",
    ],
)
def test_region_of_is_empty_for_two_distinct_cities(text: str) -> None:
    """Two real cities under the same read country must never guess a shared subdivision."""
    assert READER.region_of(text) == ""


def test_region_of_is_empty_for_an_ambiguous_bare_city() -> None:
    """A bare city with no verified single country resolves no subdivision."""
    assert READER.region_of("Springfield") == ""


def test_the_city_index_loads_once() -> None:
    script = (
        "import sys; from importlib import resources;"
        "from tailorcv.domain.countries import DEFAULT_TABLE;"
        "from tailorcv.domain.places import PlaceReader;"
        "reads = []; files = resources.files;"
        "resources.files = lambda package: reads.append(package) or files(package);"
        "PlaceReader(DEFAULT_TABLE).read('Boston');"
        "PlaceReader(DEFAULT_TABLE).read('Naperville, IL');"
        "print(reads)"
    )
    completed = subprocess.run(  # noqa: S603 - fixed arguments, current interpreter
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert completed.stdout.strip() == "['tailorcv.domain']"


def test_importing_the_readers_does_not_import_geonamescache() -> None:
    script = (
        "import sys, tailorcv.domain.countries, tailorcv.domain.places;"
        "sys.exit('geonamescache' in sys.modules)"
    )
    completed = subprocess.run([sys.executable, "-c", script], check=False)  # noqa: S603 - fixed arguments, current interpreter
    assert completed.returncode == 0


def test_the_shipped_index_and_the_licensing_doc_carry_the_geonames_attribution() -> None:
    data = json.loads(
        resources.files("tailorcv.domain").joinpath("gazetteer.json").read_text(encoding="utf-8")
    )
    licence = "https://creativecommons.org/licenses/by/4.0/"
    assert data["attribution"]["licence_url"] == licence
    assert "GeoNames" in data["attribution"]["creator"]
    doc = Path(__file__).parents[2].joinpath("docs", "licensing-exceptions.md").read_text()
    assert "gazetteer.json" in doc
    assert licence in doc


_COUNTRY_TOWNS = ("Lebanon", "Peru", "Denmark", "Mexico", "Jordan", "Poland", "Norway")
_PRESS_FORMS = ("Pa.", "Ind.", "Wis.", "Wisc.", "Mo.", "Minn.", "Minn", "Calif.", "N.Y.", "Ont.")


@pytest.mark.parametrize(
    ("town", "state", "tail"),
    list(product(_COUNTRY_TOWNS, _PRESS_FORMS, ("", " 17042", " (Remote)"))),
)
def test_a_press_state_abbreviation_never_reads_a_town_as_its_namesake_country(
    town: str, state: str, tail: str
) -> None:
    reading = READER.read(f"{town}, {state}{tail}")

    assert reading.outside({"united states", "canada"}) == ()


@pytest.mark.parametrize("place", ["Beirut, Lebanon", "Lima, Peru", "Copenhagen, Denmark"])
def test_a_country_named_after_a_comma_still_reads_as_that_country(place: str) -> None:
    assert READER.read(place).explicit == (place.split(", ")[1],)


_IVORY_COAST = "Côte d\N{RIGHT SINGLE QUOTATION MARK}Ivoire"
_ALTERNATE_COUNTRY_NAMES = (
    ("Bosnia and Herzegowina", "Bosnia & Herzegovina"),
    ("Bosnia and Herzegovina", "Bosnia & Herzegovina"),
    ("Bosnia-Herzegovina", "Bosnia & Herzegovina"),
    ("Swaziland", "Eswatini"),
    ("Republic of North Macedonia", "North Macedonia"),
    ("The former Yugoslav Republic of Macedonia", "North Macedonia"),
    ("Burma", "Myanmar (Burma)"),
    ("Myanmar", "Myanmar (Burma)"),
    ("Ivory Coast", _IVORY_COAST),
    ("Cote d'Ivoire", _IVORY_COAST),
    ("Cabo Verde", "Cape Verde"),
    ("East Timor", "Timor-Leste"),
    ("Lao People's Democratic Republic", "Laos"),
    ("Syrian Arab Republic", "Syria"),
    ("Kyrgyz Republic", "Kyrgyzstan"),
    ("Holy See", "Vatican City"),
    ("Democratic Republic of the Congo", "Congo - Kinshasa"),
    ("Republic of the Congo", "Congo - Brazzaville"),
    ("State of Palestine", "Palestinian Territories"),
    ("Macao", "Macao SAR China"),
    ("Faeroe Islands", "Faroe Islands"),
    ("Luxemburg", "Luxembourg"),
    ("Trinidad and Tobago", "Trinidad & Tobago"),
    ("Antigua and Barbuda", "Antigua & Barbuda"),
    ("Saint Kitts and Nevis", "St. Kitts & Nevis"),
    ("Saint Vincent and the Grenadines", "St. Vincent & Grenadines"),
    ("Sao Tome and Principe", "São Tomé & Príncipe"),
    ("Curacao", "Curaçao"),
    ("Reunion", "Réunion"),
)
_ALTERNATE_NAME_FORMS = ("{name}", "Ostvale, {name}", "Ostvale, North District, {name}")


@pytest.mark.parametrize(
    ("name", "country", "form"),
    [
        (name, country, form)
        for (name, country), form in product(_ALTERNATE_COUNTRY_NAMES, _ALTERNATE_NAME_FORMS)
    ],
)
def test_an_older_or_plain_country_spelling_reads_as_that_country(
    name: str, country: str, form: str
) -> None:
    reading = READER.read(form.format(name=name))

    assert reading.explicit == (country,)
    assert reading.outside({"united states", "canada"}) == (country,)


def test_a_mixed_old_and_new_bosnia_spelling_reads_outside_north_america() -> None:
    place = "Sarajevo, Federation of Bosnia and Herzegovina, Bosnia and Herzegowina"

    assert READER.read(place).outside({"united states", "canada"}) == ("Bosnia & Herzegovina",)


def test_greek_macedonia_names_no_north_macedonia() -> None:
    assert READER.read("Thessaloniki, Central Macedonia, Greece").explicit == ("Greece",)


_PROVINCE_TOWNS = (
    ("New Brunswick", "New Jersey"),
    ("Ontario", "California"),
    ("Yukon", "Oklahoma"),
)
_TOWN_FORMS = (
    "{town}, {state}",
    "{town}, {state}, United States of America",
    "{town}, {state}, USA",
    "{town}, {state} (Hybrid)",
    "United States - {state} - {town}",
    "{state} - {town}",
)


@pytest.mark.parametrize(
    ("town", "state", "form"),
    [(town, state, form) for (town, state), form in product(_PROVINCE_TOWNS, _TOWN_FORMS)],
)
def test_a_town_named_for_a_province_and_placed_under_its_state_reads_only_that_country(
    town: str, state: str, form: str
) -> None:
    assert READER.read(form.format(town=town, state=state)).explicit == ("United States",)


@pytest.mark.parametrize(
    "place",
    [
        "New Brunswick",
        "Ontario",
        "Yukon",
        "Ontario, Canada",
        "Fredericton, New Brunswick",
        "Fredericton, New Brunswick, Canada",
        "Toronto, Ontario, Canada",
        "Whitehorse, Yukon",
        "Canada - Ontario - Toronto",
    ],
)
def test_a_province_in_its_own_slot_still_reads_canada(place: str) -> None:
    assert READER.read(place).explicit == ("Canada",)


@pytest.mark.parametrize(
    "place",
    [
        "Ontario or California",
        "California, Ontario",
        "Ontario / New Jersey",
        "Delaware, Ontario",
        "Ontario, California, Texas",
        "Remote - Ontario, California, New York",
    ],
)
def test_a_province_listed_beside_a_state_still_names_canada(place: str) -> None:
    assert "Canada" in READER.read(place).explicit


# (town, state code, state name); the first five are indexed under that state.
_DASHED_TOWNS = (
    ("New Brunswick", "NJ", "New Jersey"),
    ("Ontario", "CA", "California"),
    ("Yukon", "OK", "Oklahoma"),
    ("Panama City", "FL", "Florida"),
    ("Lebanon", "PA", "Pennsylvania"),
    ("Ontario", "OR", "Oregon"),
    ("Jersey Shore", "PA", "Pennsylvania"),
    ("Grenada", "MS", "Mississippi"),
    ("Chesterfield", "VA", "Virginia"),
    ("Lebanon", "NH", "New Hampshire"),
)
_COUNTRY_TAILED_FORMS = (
    "{town} - {code} - US",
    "{town} - {state} - US",
    "{town} - {state} - United States",
    "{code} - {town} - US",
)
_DASHED_FORMS = ("{town} - {code}", "{code} - {town}")


@pytest.mark.parametrize(
    "place",
    [
        form.format(town=town, code=code, state=state)
        for (town, code, state), form in product(_DASHED_TOWNS, _COUNTRY_TAILED_FORMS)
    ],
)
def test_a_dashed_town_under_its_us_state_and_country_reads_only_the_united_states(
    place: str,
) -> None:
    reading = READER.read(place)
    assert reading.explicit == ("United States",)
    assert set(reading.inferred) <= {"United States"}


@pytest.mark.parametrize(
    "place",
    [
        form.format(town=town, code=code)
        for (town, code, _), form in product(_DASHED_TOWNS[:5], _DASHED_FORMS)
        # "Ontario, CA" reads Canada, and its dashed forms keep that reading.
        if code != "CA"
    ],
)
def test_a_dashed_indexed_town_and_its_state_code_read_only_the_united_states(place: str) -> None:
    reading = READER.read(place)
    assert reading.explicit == ("United States",)
    assert not reading.unverified_subdivision


@pytest.mark.parametrize(
    "place",
    [
        form.format(town=town, code=code)
        for (town, code, _), form in product(_DASHED_TOWNS[5:], _DASHED_FORMS)
    ],
)
def test_a_dashed_unindexed_town_rests_on_its_state_code_and_names_no_foreign_country(
    place: str,
) -> None:
    reading = READER.read(place)
    assert reading.explicit == ()
    assert set(reading.inferred) <= {"United States"}
    assert reading.unverified_subdivision
    assert reading.unverified_code_countries == ()


@pytest.mark.parametrize(
    "place",
    [
        "Lebanon - New Jersey",
        "Canada - New Jersey",
        "Mexico - Texas",
        "lebanon - new jersey, ",
        "New Jersey - Lebanon, New Jersey",
    ],
)
def test_a_spelled_state_with_no_country_tail_keeps_the_comma_forms_countries(place: str) -> None:
    assert READER.read(place).explicit == READER.read(place.replace(" - ", ", ")).explicit


@pytest.mark.parametrize("place", ["Ontario - CA", "CA - Ontario"])
def test_a_dashed_province_beside_the_canada_code_reads_like_its_comma_form(place: str) -> None:
    assert READER.read(place).explicit == READER.read("Ontario, CA").explicit == ("Canada",)


@pytest.mark.parametrize(
    ("place", "country"),
    [
        ("Panama City, Panama", "Panama"),
        ("Ciudad de Panama", "Panama"),
        ("Jersey, Channel Islands", "Jersey"),
        ("Grenada", "Grenada"),
        ("New Brunswick", "Canada"),
        ("Fredericton - New Brunswick - Canada", "Canada"),
    ],
)
def test_a_country_or_province_outside_a_us_state_slot_keeps_its_country(
    place: str, country: str
) -> None:
    assert READER.read(place).explicit == (country,)


@pytest.mark.parametrize(
    ("place", "country"),
    [
        ("Boston 200 Main Street", "United States"),
        ("Boston Main Street", "United States"),
        ("Naperville 1 Main Street", "United States"),
        ("Paris 12 Rue de la Paix", "France"),
    ],
)
def test_a_dominant_city_followed_by_a_street_still_suggests_its_country(
    place: str, country: str
) -> None:
    assert READER.read(place) == PlaceReading(inferred=(country,), locality=True)


@pytest.mark.parametrize(
    "place", ["Durham 100 Main Street", "Durham Main Street", "Cambridge 300 Technology Square"]
)
def test_an_ambiguous_city_followed_by_a_street_reads_no_country(place: str) -> None:
    """A street does not settle which country's Durham or Cambridge is meant; this is the limit."""
    assert READER.read(place) == PlaceReading(locality=True)


@pytest.mark.parametrize(
    "place",
    [
        "TX - Lyon",
        "Lyon - TX",
        "TX-Lyon",
        "Toulouse - TX - Remote",
        "OH-Leeds-Main Campus",
        "OH - Leeds - Main Campus",
        "Leeds, KY",
        "LEEDS, KY",
        "london, KY",
        "Leeds, AL",
        "Athens, Georgia",
        "Paris, GEORGIA",
        "Lyon, Texas",
        "lyon, texas",
        "Texas - Lyon",
        "Medical Plaza, Georgia",
        "FL - Saint Petersburg",
        "Saint Petersburg - FL",
        "St. Petersburg, FL",
        "Mary Medical Group, Georgia",
        "Lyon Health Partners - TX",
        "Lyon Health Partners, Texas",
    ],
)
def test_a_us_state_beside_a_city_known_only_abroad_reads_the_united_states(place: str) -> None:
    reading = READER.read(place)
    assert reading.explicit == ("United States",)
    assert reading.unverified_code_countries == ()


@pytest.mark.parametrize(
    ("place", "country"),
    [
        ("Tbilisi, Georgia", "Georgia"),
        ("Batumi - Georgia", "Georgia"),
        ("Tbilisi - GA", "Georgia"),
        ("Tbilisi Medical Group, Georgia", "Georgia"),
    ],
)
def test_a_state_that_also_spells_the_city_s_own_country_keeps_that_country(
    place: str, country: str
) -> None:
    reading = READER.read(place)
    assert reading.explicit == ()
    assert reading.inferred == (country,)


def test_a_spelled_state_the_city_is_not_indexed_under_names_no_region() -> None:
    assert READER.region_of("Paris, Georgia") == ""
    assert READER.region_of("Paris, Texas") == "Texas"
