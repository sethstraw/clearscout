"""Reading a page's own JobPosting markup, in the shapes publishers emit."""

from __future__ import annotations

import json
from typing import Any

import pytest

from clearscout.infrastructure.web_fetch.board_mappings.mapper import plain_description
from clearscout.infrastructure.web_fetch.ingest.job_posting_markup import (
    MAX_DESCRIPTION_CHARACTERS,
    MAX_POSTINGS,
    BaseSalaryMarkup,
    job_postings,
)

FULL = {
    "@context": "https://schema.org",
    "@type": "JobPosting",
    "title": "Platform Engineer",
    "description": "Run the migrations.",
    "datePosted": "2026-08-01",
    "validThrough": "2026-09-01T00:00:00Z",
    "identifier": {"@type": "PropertyValue", "name": "Requisition ID", "value": "REQ-1"},
    "hiringOrganization": {"@type": "Organization", "name": "Northwind"},
    "jobLocation": {
        "@type": "Place",
        "address": {
            "@type": "PostalAddress",
            "addressLocality": "Leeds",
            "addressRegion": "England",
            "addressCountry": "GB",
        },
    },
    "url": "https://northwind.example/jobs/1",
}


def _page(*documents: Any, script_type: str = "application/ld+json") -> str:
    blocks = "".join(
        f'<script type="{script_type}">{json.dumps(document)}</script>' for document in documents
    )
    return f"<html><head>{blocks}</head><body><p>Ignore me.</p></body></html>"


def test_a_declared_posting_is_copied_field_for_field() -> None:
    (posting,) = job_postings(_page(FULL))

    assert posting.title == "Platform Engineer"
    assert posting.description == "Run the migrations."
    assert posting.employer == "Northwind"
    assert posting.location == "Leeds, England, United Kingdom"
    # A PropertyValue identifier names its scheme and states the requisition in the value.
    assert posting.identifier == "REQ-1"
    assert posting.url == "https://northwind.example/jobs/1"
    # The publisher's own expiry is what answers "still open" without matching
    # phrases in the body.
    assert posting.valid_through == "2026-09-01T00:00:00Z"
    assert posting.date_posted == "2026-08-01"


def test_a_posting_inside_a_graph_or_a_list_is_still_found() -> None:
    graph = {"@context": "https://schema.org", "@graph": [{"@type": "WebSite"}, FULL]}
    assert [item.title for item in job_postings(_page(graph))] == ["Platform Engineer"]
    assert [item.title for item in job_postings(_page([{"@type": "WebSite"}, FULL]))] == [
        "Platform Engineer"
    ]


def test_a_type_declared_as_a_list_still_counts_as_a_posting() -> None:
    both = {**FULL, "@type": ["JobPosting", "WebPage"]}

    assert [item.title for item in job_postings(_page(both))] == ["Platform Engineer"]


def test_markup_that_is_not_a_posting_is_ignored() -> None:
    assert job_postings(_page({"@type": "Organization", "name": "Northwind"})) == ()
    assert job_postings("<html><body>No markup at all.</body></html>") == ()


def test_a_broken_block_does_not_discard_the_others() -> None:
    page = (
        '<script type="application/ld+json">{ not json </script>'
        f'<script type="application/ld+json">{json.dumps(FULL)}</script>'
    )

    # One publisher's broken script says nothing about the next block down.
    assert [item.title for item in job_postings(page)] == ["Platform Engineer"]


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        (r"Solutions d\'assurance", "Solutions d'assurance"),
        (r"\'Quoted\' role", "'Quoted' role"),
        (r"It\'s \/ it\'s", "It's / it's"),
        (r"C:\\\'path", "C:\\'path"),
    ],
)
def test_a_javascript_escaped_apostrophe_in_linked_data_still_reads(
    written: str, expected: str
) -> None:
    page = (
        '<script type="application/ld+json">'
        f'{{"@type": "JobPosting", "title": "Tele-Interviewer", "industry": "{written}",'
        f' "description": "{written}"}}</script>'
    )

    (posting,) = job_postings(page)

    assert posting.title == "Tele-Interviewer"
    assert posting.description == expected


def test_only_linked_data_scripts_are_read() -> None:
    assert job_postings(_page(FULL, script_type="application/json")) == ()
    assert job_postings(_page(FULL, script_type="text/javascript")) == ()
    # A charset parameter is still linked data.
    assert len(job_postings(_page(FULL, script_type="application/ld+json; charset=utf-8"))) == 1


def test_a_posting_with_no_title_is_not_a_posting_worth_recording() -> None:
    assert job_postings(_page({"@type": "JobPosting", "description": "Nameless."})) == ()


def test_a_remote_posting_stating_only_a_country_keeps_that_country() -> None:
    remote = {**FULL, "jobLocation": {"address": {"addressCountry": "GB"}}}

    assert job_postings(_page(remote))[0].location == "United Kingdom"


def test_the_looser_shapes_publishers_use_for_the_same_fields_still_read() -> None:
    loose = {
        "@type": "JobPosting",
        "title": "Platform Engineer",
        # An employer as a bare string, an identifier as a number, and a
        # location given as a list are all common and all mean the same thing.
        "hiringOrganization": "Northwind",
        "identifier": 4567,
        "jobLocation": [{"address": {"addressLocality": "Leeds"}}],
    }

    (posting,) = job_postings(_page(loose))

    assert posting.employer == "Northwind"
    assert posting.identifier == "4567"
    assert posting.location == "Leeds"


def test_a_field_given_as_a_list_of_values_takes_the_first_one_that_says_something() -> None:
    listed = {"@type": "JobPosting", "title": ["", "Platform Engineer"]}

    assert job_postings(_page(listed))[0].title == "Platform Engineer"


def test_an_empty_or_unusable_block_is_passed_over() -> None:
    assert job_postings('<script type="application/ld+json">   </script>') == ()
    # A graph carrying something that is not an object at all.
    assert job_postings(_page({"@graph": ["not an object", FULL]}))[0].title == "Platform Engineer"


def test_an_address_that_is_not_broken_into_parts_is_kept_as_written() -> None:
    plain = {"@type": "JobPosting", "title": "Role", "jobLocation": {"address": "Leeds, UK"}}

    assert job_postings(_page(plain))[0].location == "Leeds, UK"


def test_a_listing_dump_is_bounded_rather_than_read_whole() -> None:
    many = [{**FULL, "title": f"Role {index}"} for index in range(MAX_POSTINGS + 10)]

    found = job_postings(_page(many))

    assert len(found) == MAX_POSTINGS
    assert found[0].title == "Role 0"


def test_stated_employment_types_are_copied_without_a_schema_address() -> None:
    stated = {**FULL, "employmentType": ["FULL_TIME", "https://schema.org/CONTRACTOR", "FULL_TIME"]}

    (posting,) = job_postings(_page(stated))

    assert posting.employment_types == ("FULL_TIME", "CONTRACTOR")


def test_a_monetary_amount_is_copied_part_by_part() -> None:
    stated = {
        **FULL,
        "baseSalary": {
            "@type": "MonetaryAmount",
            "currency": "USD",
            "value": {
                "@type": "QuantitativeValue",
                "minValue": 90000,
                "maxValue": 120000,
                "unitText": "YEAR",
            },
        },
    }

    (posting,) = job_postings(_page(stated))

    assert posting.base_salary == BaseSalaryMarkup(
        currency="USD", minimum="90000", maximum="120000", unit="YEAR"
    )


def test_a_bare_salary_figure_states_an_amount_and_no_currency() -> None:
    (posting,) = job_postings(_page({**FULL, "baseSalary": 50000}))

    assert posting.base_salary == BaseSalaryMarkup(maximum="50000")
    assert job_postings(_page(FULL))[0].base_salary is None


# json.dumps never emits an entity, so an escaped block is written out by hand.
_ESCAPED_LINKED_DATA = """
<html><head><script type="application/ld+json">
{"@context": "https://schema.org",
 "@type": "JobPosting",
 "title": "Clinical Data &amp; Taxonomy Lead",
 "hiringOrganization": {"@type": "Organization", "name": "Vanhorn &amp; Reed"},
 "description": "Own the &lt;b&gt;terminology&lt;/b&gt; service."}
</script></head><body></body></html>
"""

_LITERAL_AMPERSANDS = """
<html><head><script type="application/ld+json">
[{"@type": "JobPosting", "title": "R&D Lead"},
 {"@type": "JobPosting", "title": "AT&T Analyst"},
 {"@type": "JobPosting", "title": "Q&A Manager"},
 {"@type": "JobPosting", "title": "Sales &times Marketing"},
 {"@type": "JobPosting", "title": "Head of R&amp;D"}]
</script></head><body></body></html>
"""


def test_an_escaped_linked_data_posting_is_read_decoded() -> None:
    (posting,) = job_postings(_ESCAPED_LINKED_DATA)

    assert posting.title == "Clinical Data & Taxonomy Lead"
    assert posting.employer == "Vanhorn & Reed"
    assert posting.description == "Own the <b>terminology</b> service."


def test_an_ampersand_without_a_semicolon_stays_as_the_publisher_wrote_it() -> None:
    assert [item.title for item in job_postings(_LITERAL_AMPERSANDS)] == [
        "R&D Lead",
        "AT&T Analyst",
        "Q&A Manager",
        "Sales &times Marketing",
        "Head of R&D",
    ]


@pytest.mark.parametrize(
    ("stated", "expected"),
    [
        ("Ship the release train.", "Ship the release train."),
        (
            ["Ship the release train.", "Review schema changes."],
            "Ship the release train.\n\nReview schema changes.",
        ),
        (
            {
                "@type": "EducationalOccupationalCredential",
                "description": "Ship the release train.",
            },
            "Ship the release train.",
        ),
    ],
    ids=["string", "list", "node"],
)
def test_a_stated_section_is_appended_under_its_own_heading(stated: Any, expected: str) -> None:
    (posting,) = job_postings(_page({**FULL, "responsibilities": stated}))

    assert posting.description == f"Run the migrations.\n\nResponsibilities\n\n{expected}"
    assert posting.description_paths == ("description", "responsibilities")


@pytest.mark.parametrize(
    ("description", "responsibilities", "expected"),
    [
        (
            "<p>Run the migrations.</p>",
            ["Own the schema.", "Review & ship."],
            "<p>Run the migrations.</p>\n\n<h3>Responsibilities</h3>"
            "\n\n<p>Own the schema.</p>\n\n<p>Review &amp; ship.</p>",
        ),
        (
            "Run the migrations.",
            "<ul><li>Own the schema.</li></ul>",
            "<p>Run the migrations.</p>\n\n<h3>Responsibilities</h3>"
            "\n\n<ul><li>Own the schema.</li></ul>",
        ),
    ],
    ids=["markup-description", "markup-section"],
)
def test_sections_beside_markup_are_written_as_block_elements(
    description: str, responsibilities: Any, expected: str
) -> None:
    stated = {**FULL, "description": description, "responsibilities": responsibilities}

    (posting,) = job_postings(_page(stated))

    assert posting.description == expected
    assert plain_description(posting.description).split("\n\n")[:2] == [
        "Run the migrations.",
        "Responsibilities",
    ]


def test_every_stated_section_follows_the_description_in_a_fixed_order() -> None:
    stated = {
        **FULL,
        "experienceRequirements": {
            "@type": "OccupationalExperienceRequirements",
            "description": "Five years on call.",
        },
        "educationRequirements": {
            "@type": "EducationalOccupationalCredential",
            "credentialCategory": "bachelor degree",
        },
        "skills": ["Python", "SQL"],
        "qualifications": "A shipped migration.",
        "responsibilities": "Own the schema.",
    }

    (posting,) = job_postings(_page(stated))

    assert posting.description == (
        "Run the migrations.\n\nResponsibilities\n\nOwn the schema."
        "\n\nQualifications\n\nA shipped migration.\n\nSkills\n\nPython\n\nSQL"
        "\n\nEducation requirements\n\nbachelor degree"
        "\n\nExperience requirements\n\nFive years on call."
    )


def test_a_section_the_description_already_states_is_not_repeated() -> None:
    stated = {
        **FULL,
        "description": "<p>Run the migrations.</p><ul><li>Own the schema.</li></ul>",
        "responsibilities": "Own the schema.",
        "qualifications": ["own the SCHEMA", "A shipped migration."],
        "skills": "A shipped migration.",
    }

    (posting,) = job_postings(_page(stated))

    assert posting.description == (
        "<p>Run the migrations.</p><ul><li>Own the schema.</li></ul>"
        "\n\n<h3>Qualifications</h3>\n\n<p>A shipped migration.</p>"
    )
    assert posting.description_paths == ("description", "qualifications")


def test_description_fragments_keep_later_duties_without_repeating_a_section() -> None:
    stated = {
        **FULL,
        "description": [
            "<p>We build useful software.</p>",
            "<h2>The role</h2><p>Own the schema.</p>",
            "<p>Work with a distributed team.</p>",
        ],
        "responsibilities": "Own the schema.",
    }

    (posting,) = job_postings(_page(stated))

    assert posting.description == (
        "<p>We build useful software.</p>\n\n"
        "<h2>The role</h2><p>Own the schema.</p>\n\n"
        "<p>Work with a distributed team.</p>"
    )
    assert posting.description_paths == ("description",)


def test_sections_without_a_description_still_make_the_body() -> None:
    stated = {"@type": "JobPosting", "title": "Platform Engineer", "responsibilities": "Own it."}

    (posting,) = job_postings(_page(stated))

    assert posting.description == "Responsibilities\n\nOwn it."
    assert posting.description_paths == ("responsibilities",)


def test_the_whole_body_is_bounded_not_only_the_description() -> None:
    long = "x" * (MAX_DESCRIPTION_CHARACTERS - 10)
    stated = {**FULL, "description": long, "responsibilities": "Own the schema."}

    (posting,) = job_postings(_page(stated))

    assert len(posting.description) == MAX_DESCRIPTION_CHARACTERS
    assert posting.description.startswith(long)


_ORGANIZATION = {
    "@type": "Organization",
    "@id": "https://northwind.example/#org",
    "name": "Northwind",
}
_REFERENCING = {**FULL, "hiringOrganization": {"@id": "https://northwind.example/#org"}}


def test_an_employer_referenced_by_id_in_the_same_block_is_resolved() -> None:
    (posting,) = job_postings(_page([_ORGANIZATION, _REFERENCING]))

    assert posting.employer == "Northwind"


def test_an_employer_referenced_by_id_in_another_block_is_resolved() -> None:
    (posting,) = job_postings(_page(_REFERENCING, _ORGANIZATION))

    assert posting.employer == "Northwind"


def test_an_employer_referenced_by_id_inside_a_graph_is_resolved() -> None:
    graph = {"@context": "https://schema.org", "@graph": [_REFERENCING, _ORGANIZATION]}

    assert job_postings(_page(graph))[0].employer == "Northwind"


def test_an_employer_reference_nothing_declares_stays_blank() -> None:
    other = {**_ORGANIZATION, "@id": "https://northwind.example/#brand"}

    assert job_postings(_page(_REFERENCING, other))[0].employer == ""


_MICRODATA_PAGE = """
<html><body>
<main itemscope itemtype="http://schema.org/JobPosting">
  <h1 itemprop="title">Medical Information &amp; <span>Team Lead</span></h1>
  <ul>
    <li itemprop="jobLocation" itemscope itemtype="http://schema.org/Place">
      <span itemprop="address" itemscope itemtype="http://schema.org/PostalAddress">
        <meta itemprop="addressLocality" content="Leeds">
        <meta itemprop="addressRegion" content="England">
        <meta itemprop="addressCountry" content="United Kingdom">
      </span>
    <li itemprop="employmentType">Full-time
  </ul>
  <div itemprop="hiringOrganization" itemscope itemtype="http://schema.org/Organization">
    <meta itemprop="name" content="Northwind">
  </div>
  <meta itemprop="datePosted" content="2026-05-07T08:42:41.933Z">
  <div itemprop="description"><h2>The role</h2><p>Lead the team.<br>Answer enquiries.</p></div>
</main>
<p>Unrelated footer text.</p>
</body></html>
"""


def test_a_posting_declared_as_microdata_is_read_like_linked_data() -> None:
    (posting,) = job_postings(_MICRODATA_PAGE)
    declared = '{"@type": "JobPosting", "title": "Medical Information &amp; Team Lead"}'
    (twin,) = job_postings(f'<script type="application/ld+json">{declared}</script>')

    assert posting.title == "Medical Information & Team Lead"
    assert twin.title == posting.title
    assert posting.employer == "Northwind"
    assert posting.location == "Leeds, England, United Kingdom"
    assert posting.locations_complete is True
    assert posting.date_posted == "2026-05-07T08:42:41.933Z"
    # The description keeps its block markup, as a linked-data description does.
    assert posting.description == "<h2>The role</h2><p>Lead the team.<br>Answer enquiries.</p>"


def test_repeated_microdata_descriptions_keep_the_role_between_company_sections() -> None:
    page = """<html><body>
    <main itemscope itemtype="https://schema.org/JobPosting">
      <h1 itemprop="title">Operations Lead</h1>
      <div itemprop="description"><h2>Our purpose</h2><p>We help customers.</p></div>
      <div itemprop="description"><h2>The role</h2><p>Lead service operations.</p></div>
      <div itemprop="description"><h2>Your qualifications</h2><p>Five years of experience.</p></div>
      <div itemprop="description"><h2>About us</h2><p>Our team is growing.</p></div>
    </main><p>Other jobs you may like.</p></body></html>"""

    (posting,) = job_postings(page)

    assert posting.description == (
        "<h2>Our purpose</h2><p>We help customers.</p>\n\n"
        "<h2>The role</h2><p>Lead service operations.</p>\n\n"
        "<h2>Your qualifications</h2><p>Five years of experience.</p>\n\n"
        "<h2>About us</h2><p>Our team is growing.</p>"
    )
    assert "Other jobs" not in posting.description


def test_linked_data_outranks_microdata_on_the_same_page() -> None:
    page = _page(FULL).replace("<p>Ignore me.</p>", _MICRODATA_PAGE)

    assert [item.title for item in job_postings(page)] == ["Platform Engineer"]


def test_microdata_for_another_type_declares_no_posting() -> None:
    page = '<div itemscope itemtype="http://schema.org/Organization"><b itemprop="name">X</b></div>'

    assert job_postings(page) == ()
