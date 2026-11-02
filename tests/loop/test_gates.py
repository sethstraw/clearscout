"""The conditions a posting fails outright, decided before anything is spent."""

from __future__ import annotations

import itertools
from decimal import Decimal

import pytest

from tailorcv.application.discovery.screening import ScreeningCode, screen_discovery
from tailorcv.application.master_cv.cv import parse_cv
from tailorcv.application.tailoring.fit import Standing, gates, read, refusal, unanswered
from tailorcv.application.tailoring.workflows.evaluation_policy import compensation_score
from tailorcv.domain.search_parameters import (
    Acceptance,
    Arrangement,
    CompensationBand,
    Employers,
    JobSearchParameters,
    Location,
    Seniority,
    WorkEligibility,
    WorkingTimeZone,
)
from tailorcv.domain.staged_facts import StagedListingFacts
from tests.support.suite import FIXTURES

POSTING = """Staff Data Platform Engineer, Toronto

This role is on site in Toronto, Canada, 4 days a week in the office.
Salary: CAD 90,000 per year.

Requirements:
- 8 years of experience with streaming ingestion
- Experience with schema governance
"""

SILENT = """Staff Data Platform Engineer, Toronto

This role is on site in Toronto, Canada.

Requirements:
- 8 years of experience with streaming ingestion
"""


def _reading(posting: str, parameters: JobSearchParameters):
    return read(posting, parse_cv(FIXTURES / "master_cv.md"), parameters=parameters)


def _parameters(**overrides) -> JobSearchParameters:
    fields = {
        "locations": [Location(key="ca", country="Canada", metro="Toronto")],
        "arrangement": Arrangement(remote=Acceptance.PREFERRED, hybrid=Acceptance.REFUSED),
    }
    return JobSearchParameters(**{**fields, **overrides})


@pytest.mark.parametrize(
    ("title", "excluded", "refused"),
    [
        ("Internal Auditor", "intern", False),
        ("Engineering Intern", "intern", True),
        ("Sales-Account Manager", "sales manager", False),
        ("Manager of Sales", "sales manager", False),
        ("Finance Internships, Summer 2027", "internship", True),
        ("Finance Internship, Summer 2027", "internships", True),
        ("Clinical Manager Pharmacy Technicians", "pharmacy technician", True),
        ("Clinical Manager Pharmacy Technician", "Pharmacy Technicians", True),
        ("Project Manager - Research Nurses", "research nurse", True),
        ("Project Manager (Research Nurses)", "research nurse", True),
        ("Special Projects Manager - Data Centers", "data center", True),
        ("Special Projects Manager, Data Center", "data centers", True),
        ("Clinical Studies Lead", "clinical study", True),
        ("Clinical Study Lead", "clinical studies", True),
        ("Sr. Product Manager, Pharmacy Management System", "Pharmacy Manager", False),
        ("Product Manager (Pharmacy Management Systems)", "Pharmacy Manager", False),
        ("Medical Management Clinician Sr", "Medical Manager", False),
        ("Director, Product Management", "Director of Product Management", True),
        ("Senior Director, Product Management", "Director of Product Management", True),
        ("Director - Product Management", "Director of Product Management", True),
        ("Director (Product Management)", "Director of Product Management", True),
        ("Director, Product Management and Strategy", "Director of Product Management", True),
        ("Director, Alliance Management - Drug Discovery", "Alliance Management", True),
        ("Director, Alliance Management (Drug Discovery)", "Alliance Management", True),
        ("Alliance Manager, Oncology", "Alliance Management", True),
        ("Pharmacy Manager, Retail", "Pharmacy Management", True),
        ("Master Data Management Operations Analyst", "Master Data Management", True),
        ("Glass Technician", "glas technician", False),
        ("Café Manager", "Café Manager", True),
        ("Product Management of Platforms", "Product Manager", True),
        ("Product Management at Example", "Product Manager", True),
        ("Product Management II", "Product Manager", True),
        ("Product Management 2", "Product Manager", True),
        ("Product Management Senior", "Product Manager", True),
    ],
)
def test_title_exclusions_agree_in_discovery_and_evaluation(
    title: str, excluded: str, refused: bool
) -> None:
    parameters = _parameters(seniority=Seniority(excluded_titles=[excluded]))
    screened = screen_discovery("Example", title, "", parameters, [], StagedListingFacts())
    evaluated = gates(_reading(SILENT, parameters), parameters, title=title)
    assert any(item.code is ScreeningCode.TITLE_EXCLUDED for item in screened.findings) is refused
    family = next(item for item in evaluated if item.name == "job family")
    assert (family.standing is Standing.INELIGIBLE) is refused


def test_a_refused_arrangement_is_a_refusal_rather_than_a_low_score() -> None:
    """Relevance cannot override a condition somebody stated as a refusal."""
    parameters = _parameters()

    found = gates(_reading(POSTING, parameters), parameters, title="Staff Data Platform Engineer")

    refused = refusal(found)
    assert refused is not None
    assert refused.name == "arrangement"
    assert refused.standing is Standing.INELIGIBLE


def test_a_refused_arrangement_names_the_arrangement_and_quotes_the_clause_it_read() -> None:
    """The recorded reason says what was refused and the posting text it rests on."""
    parameters = _parameters(
        arrangement=Arrangement(remote=Acceptance.PREFERRED, on_site=Acceptance.REFUSED)
    )

    refused = refusal(gates(_reading(SILENT, parameters), parameters))

    assert refused is not None
    assert refused.name == "arrangement"
    assert refused.reason == (
        'the posting is on site, which you refuse: "This role is on site in Toronto, Canada."'
    )


_OFFICE_PROFILE = (
    "Our headquarters in Example City offers an open collaborative office environment. "
    "Employees enjoy convenient on-site amenities, including free onsite parking and gym "
    "facilities in the building. Our Springfield location is home to our research "
    "organization, with strong leadership based on site."
)


@pytest.mark.parametrize(
    ("role_line", "refused"),
    [("", False), ("This role is on-site; free parking is available.", True)],
)
def test_an_office_profile_refuses_on_site_only_beside_the_roles_own_attendance(
    role_line: str, refused: bool
) -> None:
    parameters = _parameters(
        arrangement=Arrangement(remote=Acceptance.PREFERRED, on_site=Acceptance.REFUSED)
    )
    posting = (
        f"Staff Data Platform Engineer\n\nAbout us\n{_OFFICE_PROFILE} {role_line}\n\n"
        "Requirements:\n- 8 years of experience with streaming ingestion\n"
    )

    found = refusal(gates(_reading(posting, parameters), parameters))

    assert (found is not None and found.name == "arrangement") is refused


def test_a_place_this_person_cannot_work_refuses_the_posting() -> None:
    parameters = _parameters(
        locations=[
            Location(
                key="ca",
                country="Canada",
                metro="Toronto",
                eligibility=WorkEligibility.INELIGIBLE,
            )
        ],
        arrangement=Arrangement(hybrid=Acceptance.ACCEPTABLE, on_site=Acceptance.ACCEPTABLE),
    )

    found = gates(_reading(POSTING, parameters), parameters)

    assert refusal(found) is not None
    assert [gate.standing for gate in found if gate.name == "residence"] == [Standing.INELIGIBLE]


def test_pay_below_a_stated_floor_scores_against_the_posting_without_refusing_it() -> None:
    """A job worth taking at less money is still a job that can be taken.

    The floor is a preference, so it belongs in the score. Refusing here would
    drop a posting the person might well want to see.
    """
    parameters = _parameters(
        arrangement=Arrangement(hybrid=Acceptance.ACCEPTABLE, on_site=Acceptance.ACCEPTABLE),
        compensation=[
            CompensationBand(location="ca", currency="CAD", minimum=Decimal(150000)),
        ],
    )
    reading = _reading(POSTING, parameters)

    found = gates(reading, parameters)

    assert reading.pay_fit.value is not None
    assert reading.pay_fit.value.startswith("below")
    assert refusal(found) is None, "pay under the floor must not refuse the posting"
    assert [gate.standing for gate in found if gate.name == "compensation"] == [Standing.ELIGIBLE]
    assert compensation_score(reading.pay_fit.value, reading.pay_fit.note).value == 0


def test_more_office_days_than_the_stated_maximum_is_its_own_condition() -> None:
    """An arrangement somebody accepts can still break the limit they put on it."""
    parameters = _parameters(
        arrangement=Arrangement(hybrid=Acceptance.ACCEPTABLE, maximum_office_days=2),
    )

    found = gates(_reading(POSTING, parameters), parameters)

    standings = {gate.name: gate.standing for gate in found}
    assert standings["arrangement"] is Standing.ELIGIBLE
    assert standings["office attendance"] is Standing.INELIGIBLE


def test_an_excluded_title_or_employer_refuses_the_posting() -> None:
    parameters = _parameters(
        arrangement=Arrangement(hybrid=Acceptance.ACCEPTABLE, on_site=Acceptance.ACCEPTABLE),
        seniority=Seniority(excluded_titles=["Data Platform"]),
        employers=Employers(excluded_employers=["Globex"]),
    )
    reading = _reading(POSTING, parameters)

    found = gates(reading, parameters, title="Staff Data Platform Engineer", employer="Globex Inc")

    refused = {gate.name for gate in found if gate.standing is Standing.INELIGIBLE}
    assert refused == {"job family", "employer"}


CREDENTIAL_REQUIRED = """Facilities Program Manager, Remote

This role is remote within Canada.

Requirements:
- Active locksmith licensure is required
- 8 years of facilities program experience
"""

CREDENTIAL_ALTERNATIVE = """Facilities Program Manager, Remote

This role is remote within Canada.

Requirements:
- Locksmith or master electrician license required
- 8 years of facilities program experience
"""

_TRADE_CONDITION = ("Requires an active locksmith or master electrician license", "not held")


def test_a_required_credential_the_person_lacks_refuses_the_posting() -> None:
    """A must-have credential the person cannot satisfy is a refusal, not a low score."""
    parameters = _parameters(excluded_conditions=[_TRADE_CONDITION])

    found = gates(_reading(CREDENTIAL_REQUIRED, parameters), parameters)

    credential = next(gate for gate in found if gate.name == "credential")
    assert credential.standing is Standing.INELIGIBLE
    assert refusal(found) is not None


@pytest.mark.parametrize(
    "condition",
    ["Requires an active locksmith or master electrician license", "Active locksmith license"],
)
def test_a_credential_refusal_quotes_the_condition_it_matched(condition: str) -> None:
    parameters = _parameters(excluded_conditions=[(condition, "not held")])

    found = gates(_reading(CREDENTIAL_REQUIRED, parameters), parameters)

    credential = next(gate for gate in found if gate.name == "credential")
    assert credential.reason == f"the posting states an excluded condition: {condition}"


CREDENTIAL_PAST_THE_QUOTE = """Facilities Program Manager, Remote

This role is remote within Canada.

Requirements:
- Plan and run facilities programs across depots, stations, workshops and regional offices \
for the Northwind network, keeping budgets, vendors and schedules aligned; active locksmith \
licensure is required
"""


def test_a_required_credential_past_the_quote_length_refuses_the_posting() -> None:
    parameters = _parameters(excluded_conditions=[_TRADE_CONDITION])

    found = gates(_reading(CREDENTIAL_PAST_THE_QUOTE, parameters), parameters)

    credential = next(gate for gate in found if gate.name == "credential")
    assert credential.standing is Standing.INELIGIBLE


def test_a_credential_with_a_satisfiable_alternative_does_not_refuse() -> None:
    """A disjunction may offer a credential the person holds, so it must not be refused."""
    parameters = _parameters(excluded_conditions=[_TRADE_CONDITION])

    found = gates(_reading(CREDENTIAL_ALTERNATIVE, parameters), parameters)

    credential = next(gate for gate in found if gate.name == "credential")
    assert credential.standing is not Standing.INELIGIBLE


CREDENTIAL_PATHS = """Facilities Program Manager, Remote

This role is remote within Canada.

Requirements:
- Active locksmith license required
OR
- Active HVAC technician certification required
- 8 years of facilities program experience
"""


@pytest.mark.parametrize(
    ("posting", "refused"),
    [
        (CREDENTIAL_PATHS, False),
        (CREDENTIAL_PATHS.replace("OR\n", ""), True),
    ],
    ids=["linked by a bare or", "separate requirements"],
)
def test_a_credential_a_bare_or_line_offers_an_alternative_to_does_not_refuse(
    posting: str, refused: bool
) -> None:
    parameters = _parameters(excluded_conditions=[_TRADE_CONDITION])

    found = gates(_reading(posting, parameters), parameters)

    credential = next(gate for gate in found if gate.name == "credential")
    assert (credential.standing is Standing.INELIGIBLE) is refused


def test_a_condition_the_posting_does_not_state_is_unknown_rather_than_met() -> None:
    """A posting that says nothing has neither passed nor failed, and gets asked about."""
    parameters = _parameters(
        arrangement=Arrangement(hybrid=Acceptance.ACCEPTABLE, on_site=Acceptance.ACCEPTABLE),
        compensation=[CompensationBand(location="ca", currency="CAD", minimum=Decimal(150000))],
    )
    found = gates(_reading(SILENT, parameters), parameters)

    assert refusal(found) is None
    assert "compensation" in {gate.name for gate in unanswered(found)}


def _residence_parameters() -> JobSearchParameters:
    return _parameters(
        locations=[
            Location(key="us", country="United States"),
            Location(key="ca", country="Canada"),
        ],
        arrangement=Arrangement(remote=Acceptance.PREFERRED),
    )


FOREIGN_RESIDENCE = """Senior Product Manager
Remote

You must be located in Spain to be considered for this role.

Requirements:
- Five years of product management experience
"""

SATISFIABLE_RESIDENCE = """Senior Product Manager
Remote

You must be located in the United States or Canada.

Requirements:
- Five years of product management experience
"""

PRIVACY_NOTICE = """Senior Product Manager
Remote - United States

If you are based in California, we encourage you to read this important \
information for California residents.

Requirements:
- Five years of product management experience
"""


def _residence_requirement_gate(posting: str, parameters: JobSearchParameters):
    found = gates(_reading(posting, parameters), parameters, title="Senior Product Manager")
    return next(gate for gate in found if gate.name == "residence requirement")


def test_a_foreign_residence_requirement_refuses_a_candidate_who_cannot_meet_it() -> None:
    parameters = _residence_parameters()
    gate = _residence_requirement_gate(FOREIGN_RESIDENCE, parameters)
    assert gate.standing is Standing.INELIGIBLE
    refused = refusal(gates(_reading(FOREIGN_RESIDENCE, parameters), parameters))
    assert refused is not None and refused.name == "residence requirement"


def test_a_residence_requirement_a_declared_place_satisfies_does_not_refuse() -> None:
    gate = _residence_requirement_gate(SATISFIABLE_RESIDENCE, _residence_parameters())
    assert gate.standing is not Standing.INELIGIBLE


def test_a_california_privacy_notice_is_not_a_residence_requirement() -> None:
    gate = _residence_requirement_gate(PRIVACY_NOTICE, _residence_parameters())
    assert gate.standing is not Standing.INELIGIBLE


US_ONLY_RESIDENCE = """Senior Product Manager
Remote

You must be located in the United States to be considered.

Requirements:
- Five years of product management experience
"""


RESIDENCE_CODE_WORD = """Senior Product Manager
Remote

You must be based somewhere we can pay you.

Requirements:
- Five years of product management experience
"""


@pytest.mark.parametrize(
    "requirement",
    [
        "You must be based somewhere we can pay you.",
        "You must be located in Canada or the us.",
        "Candidates must be based in the us or canada.",
    ],
)
def test_a_lowercase_code_never_turns_a_residence_sentence_against_a_us_person(
    requirement: str,
) -> None:
    parameters = JobSearchParameters(
        locations=[Location(key="us", country="United States")],
        arrangement=Arrangement(remote=Acceptance.PREFERRED),
    )
    posting = RESIDENCE_CODE_WORD.replace(
        "You must be based somewhere we can pay you.", requirement
    )
    gate = _residence_requirement_gate(posting, parameters)
    assert gate.standing is not Standing.INELIGIBLE


def test_a_declared_country_spelling_variant_still_satisfies_the_requirement() -> None:
    """A USA declaration meets a United States requirement; both resolve through the table."""
    parameters = JobSearchParameters(
        locations=[Location(key="us", country="USA")],
        arrangement=Arrangement(remote=Acceptance.PREFERRED),
    )
    gate = _residence_requirement_gate(US_ONLY_RESIDENCE, parameters)
    assert gate.standing is not Standing.INELIGIBLE


US_RESIDENCE_REQUIREMENTS = (
    "You must be located in the United States.",
    "Candidates must reside in the US.",
    "You must live within the United States.",
    "This position is open only to residents of the U.S.",
    "Candidates not only must reside in the US but also keep a local mailing address.",
    "You must be located in the U.S. or nearby. Citizens of any country may apply.",
)
US_PLACE_LABELS = (
    "Remote, U.S.",
    "This role is based remotely in the United States.",
    "You will join our US-based team.",
    "We are seeking US-based candidates.",
)
US_PAY_SENTENCES = (
    "The base salary range for this role is for residents of the United States only.",
    "The pay range below applies to candidates who must be located in the United States.",
)
US_WAIVED_RESIDENCE = (
    "You do not need to be located in the United States.",
    "You are not required to reside in the United States.",
    "You are not necessarily required to reside in the United States.",
    "There is no need to be located in the United States.",
    "No need to be located in the United States.",
    "You are no longer required to be located in the United States.",
)
US_RESIDENCE_OR_AUTHORIZATION = (
    "You must be located in the United States or be authorized to work in the United States.",
    "Candidates located in the United States are preferred; you must be located in North America.",
    "Candidates must be located in the United States or hold US citizenship.",
    "This role requires U.S. residency or valid U.S. work authorization.",
    "You must be located in the U.S. or be a U.S. citizen.",
    "This role requires U.S. residency or U.S. citizenship.",
    "Candidates must be located in the U.S. or hold U.S. citizenship.",
    "You must be located in the U.S. or hold a U.S. work permit.",
)
US_TIME_ZONE_RESIDENCE = (
    "You must be located in a U.S. time zone.",
    "You must be located in the Eastern or Central time zone of the United States.",
)
US_RESIDENCE_WITH_CANADA_WELCOME = (
    "Candidates located in Canada are welcome; you must be located in the United States.",
    "You must be located in the United States; candidates in Canada may also apply.",
)


def _resident_parameters(
    *,
    resident: str | None = "ca",
    relocation: bool | None = None,
    us_relocation: bool | None = None,
) -> JobSearchParameters:
    """A person living in Canada who is authorized to work for a US employer remotely."""
    return JobSearchParameters(
        locations=[
            Location(key="ca", country="Canada", resident=resident == "ca"),
            Location(
                key="us",
                country="United States",
                resident=resident == "us",
                arrangement=None
                if us_relocation is None
                else Arrangement(relocation=us_relocation),
            ),
        ],
        arrangement=Arrangement(remote=Acceptance.PREFERRED, relocation=relocation),
    )


def _with_sentence(sentence: str) -> str:
    required = "You must be located in the United States to be considered."
    return US_ONLY_RESIDENCE.replace(required, sentence)


@pytest.mark.parametrize("sentence", US_RESIDENCE_REQUIREMENTS)
def test_a_residence_requirement_outside_where_the_person_lives_refuses(sentence: str) -> None:
    gate = _residence_requirement_gate(_with_sentence(sentence), _resident_parameters())

    assert gate.standing is Standing.INELIGIBLE
    assert "United States" in gate.reason


@pytest.mark.parametrize(
    "sentence",
    US_PLACE_LABELS
    + US_PAY_SENTENCES
    + US_WAIVED_RESIDENCE
    + US_RESIDENCE_OR_AUTHORIZATION
    + US_TIME_ZONE_RESIDENCE
    + US_RESIDENCE_WITH_CANADA_WELCOME,
)
def test_a_place_label_a_pay_line_or_a_waiver_requires_no_residence(
    sentence: str,
) -> None:
    gate = _residence_requirement_gate(_with_sentence(sentence), _resident_parameters())

    assert gate.standing is not Standing.INELIGIBLE


@pytest.mark.parametrize(("resident", "relocation"), [("ca", True), (None, None), ("us", None)])
@pytest.mark.parametrize("sentence", US_RESIDENCE_REQUIREMENTS)
def test_a_residence_requirement_a_move_or_a_home_there_can_meet_does_not_refuse(
    sentence: str, resident: str | None, relocation: bool | None
) -> None:
    parameters = _resident_parameters(resident=resident, relocation=relocation)

    gate = _residence_requirement_gate(_with_sentence(sentence), parameters)

    assert gate.standing is not Standing.INELIGIBLE


@pytest.mark.parametrize(
    ("relocation", "us_relocation", "refused"),
    [(None, True, False), (False, True, False), (True, False, True), (True, None, False)],
)
@pytest.mark.parametrize("sentence", US_RESIDENCE_REQUIREMENTS)
def test_relocation_to_the_required_country_is_read_from_that_place(
    sentence: str, relocation: bool | None, us_relocation: bool | None, refused: bool
) -> None:
    parameters = _resident_parameters(relocation=relocation, us_relocation=us_relocation)

    gate = _residence_requirement_gate(_with_sentence(sentence), parameters)

    assert (gate.standing is Standing.INELIGIBLE) is refused


@pytest.mark.parametrize("country", ["Canada", "Germany"])
@pytest.mark.parametrize(
    "sentence", ["You must be located in US time zones.", *US_TIME_ZONE_RESIDENCE]
)
def test_a_time_zone_requirement_is_no_residence_rule_outside_that_country(
    sentence: str, country: str
) -> None:
    parameters = JobSearchParameters(
        locations=[Location(key="home", country=country)],
        arrangement=Arrangement(remote=Acceptance.PREFERRED),
    )

    gate = _residence_requirement_gate(_with_sentence(sentence), parameters)

    assert gate.standing is not Standing.INELIGIBLE


def test_a_posting_whose_only_paragraph_is_a_benefit_line_requires_no_residence() -> None:
    posting = (
        "You will build data pipelines for our analytics team using Python and SQL. "
        "Benefits include a remote work stipend for this fully remote role."
    )

    gate = _residence_requirement_gate(posting, _resident_parameters())

    assert gate.standing is not Standing.INELIGIBLE


def test_a_country_list_joined_by_a_serial_comma_is_read_whole() -> None:
    parameters = JobSearchParameters(
        locations=[Location(key="uk", country="United Kingdom", resident=True)],
        arrangement=Arrangement(remote=Acceptance.PREFERRED),
    )
    posting = _with_sentence(
        "This position is open only to residents of the United States, Canada, "
        "and the United Kingdom."
    )

    gate = _residence_requirement_gate(posting, parameters)

    assert gate.standing is not Standing.INELIGIBLE


LOCAL_RESIDENCE_SENTENCES = (
    *(
        f"Candidates {modal} {verb} {near}."
        for modal, verb, near in itertools.product(
            ("must", "are required to", "need to"),
            ("reside", "live"),
            (
                "locally",
                "within commuting distance",
                "within a reasonable commutable distance of the office",
            ),
        )
    ),
    "Candidates must reside locally or be willing to relocate.",
    "Candidates not within a reasonable commuting distance from the posting location(s) "
    "will not be considered.",
    "Relocation required.",
    "Applicants should reside within 50 miles of the Primary Location.",
    "Candidates must live within 30 miles of the Kingsmere office.",
)
RELOCATION_FIELDS = ("Relocation Required", "Relocation required", "Relocation is required")
THIRD_PARTIES = ("Study participants", "Volunteers", "Students", "Patients", "Trainees", "Members")
OTHER_ANCHORS = ("a major airport", "a client site", "one of our offices", "these locations")
NO_LOCAL_RESIDENCE_SENTENCES = (
    *(
        f"{field}{answer}"
        for field, answer in itertools.product(
            RELOCATION_FIELDS, (": No", ": N/A", ": None", ": Negotiable", "? No.")
        )
    ),
    *(
        f"{field}{separator}{answer}"
        for field, separator, answer in itertools.product(
            RELOCATION_FIELDS,
            (", ", " - ", "\n", "\n\n"),
            ("No", "N/A", "no.", "None", "Yes", "TBD", "Negotiable"),
        )
    ),
    "Is relocation required? No.",
    "Relocation assistance is offered where relocation is required.",
    "Relocation assistance is offered for roles where relocation is required.",
    "No sponsorship or relocation required.",
    "No visa sponsorship or relocation is required.",
    *(
        f"{subject} {modal} {near}."
        for subject, modal, near in itertools.product(
            THIRD_PARTIES,
            ("must", "are required to"),
            ("live locally", "reside within commuting distance"),
        )
    ),
    *(f"Candidates must live within commuting distance of {anchor}." for anchor in OTHER_ANCHORS),
    "Our offices are in Toronto, ON and Austin, TX. Candidates must live within commuting "
    "distance of one of these offices.",
    "Remote candidates are welcome; local candidates must live within commuting distance "
    "for onsite days.",
    "Relocation required for some positions.",
    "Candidates must reside locally, except for remote hires.",
    "Candidates must live locally, though we will consider remote candidates.",
    "No relocation required.",
    "For candidates within 50 miles of an office, relocation is required.",
    "If you live within commuting distance, you must live locally for onboarding.",
    "Teams must live locally.",
    "Applicants should reside within 30 miles of a major airport.",
    "If you live within 50 miles of the office, you must live locally.",
    "Preferred: live within 50 miles of the office.",
    "Applicants should ideally live within 50 miles of the office.",
    "Applicants should preferably reside locally.",
)
LOCAL_RESIDENCE_NEAR_AUSTIN = (
    "Candidates must reside locally or be willing to relocate.",
    "Must live within commuting distance of our Austin office.",
    "Applicants should reside within 50 miles of the Primary Location.",
    "Candidates must live within 30 miles of the Austin office.",
    "Relocation is required.",
    "Relocation required.",
    "Relocation required",
    "Relocation required, assistance provided.",
)


def _lives_in_us(*, relocates_to_canada: bool = False) -> JobSearchParameters:
    return JobSearchParameters(
        locations=[
            Location(key="us", country="United States", resident=True),
            Location(
                key="ca",
                country="Canada",
                arrangement=Arrangement(relocation=True) if relocates_to_canada else None,
            ),
        ],
        arrangement=Arrangement(remote=Acceptance.PREFERRED),
    )


def _lives_in_canada() -> JobSearchParameters:
    return JobSearchParameters(
        locations=[
            Location(key="ca", country="Canada", resident=True),
            Location(key="us", country="United States"),
        ]
    )


def _local_gate(sentence: str, listing: str, parameters: JobSearchParameters):
    posting = _with_sentence(sentence)
    reading = read(posting, parameters=parameters, listing_location=listing)
    return next(gate for gate in gates(reading, parameters) if gate.name == "residence requirement")


@pytest.mark.parametrize("sentence", LOCAL_RESIDENCE_SENTENCES)
def test_living_near_a_foreign_listing_refuses_naming_its_country(sentence: str) -> None:
    gate = _local_gate(sentence, "Kingsmere, ON, Canada", _lives_in_us())

    assert gate.standing is Standing.INELIGIBLE
    assert "Canada" in gate.reason


@pytest.mark.parametrize("sentence", LOCAL_RESIDENCE_NEAR_AUSTIN)
def test_living_near_a_us_city_refuses_a_canada_resident_naming_the_city(sentence: str) -> None:
    gate = _local_gate(sentence, "Austin, TX", _lives_in_canada())

    assert gate.standing is Standing.INELIGIBLE
    assert gate.reason == "the posting requires residing near Austin, TX, in United States"


@pytest.mark.parametrize("sentence", NO_LOCAL_RESIDENCE_SENTENCES)
def test_an_answered_scoped_anchored_or_third_party_nearness_requires_nothing(
    sentence: str,
) -> None:
    gate = _local_gate(sentence, "Austin, TX", _lives_in_canada())

    assert gate.standing is not Standing.INELIGIBLE


@pytest.mark.parametrize("listing", ["Remote - US", "United States"])
def test_living_near_a_remote_or_country_listing_names_the_country(listing: str) -> None:
    gate = _local_gate("Candidates must reside locally.", listing, _lives_in_canada())

    assert gate.standing is Standing.INELIGIBLE
    assert gate.reason == "the posting requires residing in United States"


@pytest.mark.parametrize(
    ("listing", "parameters"),
    [
        ("Austin, TX", _lives_in_us()),
        ("Remote", _lives_in_us()),
        ("Remote - US", _lives_in_us()),
        ("Austin, TX, United States; Kingsmere, ON, Canada", _lives_in_us()),
        ("", _lives_in_us()),
        ("Kingsmere, ON, Canada", _lives_in_us(relocates_to_canada=True)),
    ],
)
def test_living_near_a_listing_the_person_can_meet_or_that_names_no_country_abstains(
    listing: str, parameters: JobSearchParameters
) -> None:
    gate = _local_gate("Candidates must reside locally.", listing, parameters)

    assert gate.standing is not Standing.INELIGIBLE


def _time_zone_gate(sentence: str, parameters: JobSearchParameters | None = None):
    parameters = parameters or JobSearchParameters(working_time_zone=WorkingTimeZone.PACIFIC)
    posting = f"Senior Product Manager\n\n{sentence}\n\nRequirements:\n- Five years of SQL"
    found = gates(_reading(posting, parameters), parameters)
    return next(gate for gate in found if gate.name == "time zone")


_TOLERANCES = (
    "within {} hours of it",
    "within {} hrs of it",
    "within a {}-hour offset",
    "{} hours either side",
    "within {} hours of that zone",
)
_THREE = ("3", "three", "Three", "three (3)", "2-3", "two to three")
_TWO = ("2", "two", "Two", "two (2)")


@pytest.mark.parametrize(
    ("tolerance", "figure"), list(itertools.product(_TOLERANCES, (*_THREE, *_TWO)))
)
def test_a_time_zone_tolerance_reads_its_figure_however_it_is_written(
    tolerance: str, figure: str
) -> None:
    hours = 3 if figure in _THREE else 2
    gate = _time_zone_gate(
        f"You must be located in the Eastern time zone, {tolerance.format(figure)}."
    )
    verdict = "reaches" if hours >= 3 else "outside"
    assert gate.reason == (
        f"Pacific is 3 hours from Eastern against a tolerance of {hours} hours: {verdict}"
    )


@pytest.mark.parametrize(
    "sentence",
    [
        "You must be located in the Eastern time zone.",
        "You must be located in the eastern time zone.",
        "You must be located in Eastern Time.",
        "You must be located in Eastern Standard Time.",
        "You must be located in the Eastern or Central time zones.",
        "You must be located in the Eastern/Central time zone.",
        "You must be based in a region in the Eastern Standard Time zone.",
        "Candidates must work in the time zone: Eastern.",
        "Candidates must work in the time zone of Eastern only.",
    ],
)
def test_a_named_time_zone_is_required(sentence: str) -> None:
    gate = _time_zone_gate(sentence)
    assert gate.reason == "Pacific is 3 hours from Eastern against a tolerance of 0 hours: outside"


@pytest.mark.parametrize(
    ("zone", "shift", "sentence", "standing", "reason"),
    [
        (
            WorkingTimeZone.MOUNTAIN,
            2,
            "You must be located in the Eastern time zone.",
            Standing.ELIGIBLE,
            "Mountain is 2 hours from Eastern against a tolerance of 0 hours "
            "plus your 2-hour shift: reaches",
        ),
        (
            WorkingTimeZone.PACIFIC,
            2,
            "You must be located in the Eastern time zone.",
            Standing.INELIGIBLE,
            "Pacific is 3 hours from Eastern against a tolerance of 0 hours "
            "plus your 2-hour shift: outside",
        ),
        (
            WorkingTimeZone.PACIFIC,
            1,
            "You must be located in the Eastern time zone, within 2 hours of it.",
            Standing.ELIGIBLE,
            "Pacific is 3 hours from Eastern against a tolerance of 2 hours "
            "plus your 1-hour shift: reaches",
        ),
    ],
)
def test_the_hours_a_person_can_shift_widen_a_posting_time_zone_tolerance(
    zone: WorkingTimeZone, shift: int, sentence: str, standing: Standing, reason: str
) -> None:
    parameters = JobSearchParameters(working_time_zone=zone, working_day_shift_hours=shift)

    gate = _time_zone_gate(sentence, parameters)

    assert (gate.standing, gate.reason) == (standing, reason)


@pytest.mark.parametrize(
    "sentence",
    [
        "You must work with teams across the Asia-Pacific time zone.",
        "You must be located near our Asia Pacific time zone clients.",
        "You must be based in the Mountain View office; all time zone questions go to HR.",
        "You must be based in the Mountain View office and work across every time zone.",
        "You must be located near our Central European time zone clients.",
        "Our team is based in Toronto and works across every time zone; collaboration is central.",
        "You must be located in the Pacific Northwest; time zone flexibility is expected.",
        "You must be based in Central America and overlap with our time zone.",
    ],
)
def test_a_zone_word_inside_another_name_requires_no_time_zone(sentence: str) -> None:
    gate = _time_zone_gate(sentence)
    assert gate.standing is Standing.UNKNOWN
    assert gate.reason == "the posting requires no time zone"


_OTHER_PEOPLE = (
    "teams",
    "a team",
    "customers",
    "partners",
    "clients",
    "stakeholders",
    "colleagues",
)
_RESIDENCE_VERBS = ("located", "based", "residing", "living")
_MODALS = (
    "You must be comfortable working with",
    "You will need to support",
    "Candidates must partner with",
)


@pytest.mark.parametrize(
    ("modal", "people", "verb"),
    list(itertools.product(_MODALS, _OTHER_PEOPLE, _RESIDENCE_VERBS)),
)
def test_a_residence_verb_about_other_people_requires_nothing_of_the_candidate(
    modal: str, people: str, verb: str
) -> None:
    posting = FOREIGN_RESIDENCE.replace(
        "You must be located in Spain to be considered for this role.",
        f"{modal} {people} {verb} in Spain.",
    )
    gate = _residence_requirement_gate(posting, _residence_parameters())
    assert gate.standing is Standing.ELIGIBLE


@pytest.mark.parametrize(
    "sentence",
    [
        "You must support customers who live in Spain.",
        "You must work with a partner that is based in Spain.",
    ],
)
def test_a_residence_verb_in_a_clause_about_someone_else_requires_nothing(sentence: str) -> None:
    posting = FOREIGN_RESIDENCE.replace(
        "You must be located in Spain to be considered for this role.", sentence
    )
    gate = _residence_requirement_gate(posting, _residence_parameters())
    assert gate.standing is Standing.ELIGIBLE


@pytest.mark.parametrize(
    "sentence",
    [
        "You must be located in Spain.",
        "Candidates must reside in Spain.",
        "You are required to be based in Spain.",
        "You will need to live in Spain.",
        "Must be living in Spain for the length of the contract.",
        "You must be based in Spain to partner with our teams.",
    ],
)
def test_a_residence_verb_governing_the_candidate_is_a_requirement(sentence: str) -> None:
    posting = FOREIGN_RESIDENCE.replace(
        "You must be located in Spain to be considered for this role.", sentence
    )
    gate = _residence_requirement_gate(posting, _residence_parameters())
    assert gate.standing is Standing.INELIGIBLE


_PREFERRED_WORDING = (
    "{} would be an advantage",
    "{} would be an added advantage",
    "{} is a plus",
    "{} is a big plus",
    "{} is a strong plus",
    "{} nice to have",
    "{} is nice-to-have",
    "{} would be beneficial",
    "{} is highly beneficial",
    "{} not required but preferred",
    "{} is not required, but a plus",
    "Preference given to applicants with {}",
    "{} is good to have",
)
_WAIVED_WORDING = (
    "{} is not required",
    "{} isn't required",
    "{} isn\u2019t required",
    "{} not required",
    "{} is not a requirement",
    "{} is not mandatory",
    "{} is optional",
    "No {} required",
    "No {} is required",
)
_CREDENTIALS = ("Active locksmith licensure", "locksmith license")


@pytest.mark.parametrize(
    ("wording", "credential"),
    list(itertools.product((*_PREFERRED_WORDING, *_WAIVED_WORDING), _CREDENTIALS)),
)
def test_a_credential_a_requirement_line_prefers_or_waives_does_not_refuse(
    wording: str, credential: str
) -> None:
    line = wording.format(credential)
    posting = CREDENTIAL_REQUIRED.replace("Active locksmith licensure is required", line)
    parameters = _parameters(excluded_conditions=[_TRADE_CONDITION])

    found = gates(_reading(posting, parameters), parameters)

    credential_gate = next(gate for gate in found if gate.name == "credential")
    assert credential_gate.standing is Standing.ELIGIBLE


_ZONE_LISTS = ("Eastern", "Eastern or Central", "Eastern and Central", "Eastern/Central")
_ZONE_WORDS = ("Time Zones", "time zone", "Time")
_PREFERRED_ZONES = (
    "This role has a preference for candidates based in the {}.",
    "Ideally, you are based in the {}.",
    "Candidates based in the {} are preferred.",
    "{} strongly preferred.",
    "{} is a plus.",
    "{} is nice to have.",
    "{} is a bonus.",
    "Working {} hours would be great.",
)
_BESIDE_A_WISH = ("{}", "You must work remotely. {}", "{} You must work remotely.")


@pytest.mark.parametrize(
    ("wish", "zones", "word", "position"),
    list(itertools.product(_PREFERRED_ZONES, _ZONE_LISTS, _ZONE_WORDS, _BESIDE_A_WISH)),
)
def test_a_zone_named_as_a_preference_requires_no_time_zone(
    wish: str, zones: str, word: str, position: str
) -> None:
    gate = _time_zone_gate(position.format(wish.format(f"{zones} {word}")))
    assert gate.standing is Standing.UNKNOWN
    assert gate.reason == "the posting requires no time zone"


@pytest.mark.parametrize(
    "sentence",
    [
        "You must work Eastern Time hours. Pacific time zone is a plus.",
        "Candidates must be located in the Eastern or Central time zones.",
        "Required: you must work in overlap with Eastern Standard Time.",
    ],
)
def test_a_required_zone_beside_a_wish_is_still_required(sentence: str) -> None:
    gate = _time_zone_gate(sentence)
    assert gate.reason == "Pacific is 3 hours from Eastern against a tolerance of 0 hours: outside"


_OFFICE_LOWS = ("3", "three", "Three", "three (3)")
_OFFICE_RANGES = (
    "{}",
    "{}+",
    "{}-4",
    "{} - 4",
    "{}~4",
    "{} to 4",
    "{} to four",
    "{} or 4",
    "{}\u20134",
)
_OFFICE_PERIODS = ("days a week", "days per week", "days each week", "days/week", "days weekly")
OFFICE_CADENCES = [
    f"{form.format(low)} {period}"
    for low, form, period in itertools.product(_OFFICE_LOWS, _OFFICE_RANGES, _OFFICE_PERIODS)
] + [
    "between 3 and 4 days a week",
    "3x per week",
    "3 x per week",
    "three times a week",
    "3 times per week",
    "3 days/wk",
]


def _office_gate(cadence: str, limit: int):
    parameters = _parameters(
        arrangement=Arrangement(
            hybrid=Acceptance.ACCEPTABLE, on_site=Acceptance.ACCEPTABLE, maximum_office_days=limit
        )
    )
    posting = POSTING.replace("4 days a week in the office", f"in the office {cadence}")
    found = gates(_reading(posting, parameters), parameters)
    return next(gate for gate in found if gate.name == "office attendance")


@pytest.mark.parametrize("cadence", OFFICE_CADENCES)
def test_an_office_cadence_reads_its_bottom_however_it_is_written(cadence: str) -> None:
    assert _office_gate(cadence, 3).standing is Standing.ELIGIBLE
    over = _office_gate(cadence, 2)
    assert over.standing is Standing.INELIGIBLE
    assert over.reason == "3 office days against your 2"


@pytest.mark.parametrize(
    "cadence", ["3 days a month", "3 days per quarter", "for 3 days of onboarding", "3 days"]
)
def test_days_stated_over_no_week_are_no_weekly_cadence(cadence: str) -> None:
    assert _office_gate(cadence, 2).standing is not Standing.INELIGIBLE


_NEAR_AN_OFFICE = (
    "For employees located within a one-hour commute of a Northwind office, we require "
    "in-person attendance two days per week, either from a Northwind office or with clients.",
    "If you live within 50 miles of our office, you will work in the office 2 days a week.",
    "Employees near one of our hubs work in the office two days per week.",
    "Those based near our office attend in person 2 days per week.",
    "Staff within commuting distance work in the office 2 days a week.",
    "For people who commute to our Toronto office, attendance is 2 days a week in the office.",
    "If you are within commuting distance, you will work in the office 2 days a week.",
)


_NEARNESS_FORMS = (
    "near our Toronto office",
    "within 20 miles of the office",
    "within 30 km of the office",
    "within an hour of the office",
    "local to the office",
    "local to one of our hubs",
    "close to our hub",
)


def _cadence_for_near_people(form: str, *, after: bool, capital: bool) -> str:
    """Return a cadence sentence set for people near an office, condition first or last."""
    if after:
        sentence = f"in the office 3 days per week for staff {form}."
    else:
        sentence = f"staff {form} attend in person 3 days per week."
    return sentence[0].upper() + sentence[1:] if capital else sentence


_NEAR_AN_OFFICE_VARIANTS = [
    _cadence_for_near_people(form, after=after, capital=capital)
    for form, after, capital in itertools.product(_NEARNESS_FORMS, (False, True), (False, True))
]


_LOCAL_PEOPLE = (
    *(
        f"local {people}"
        for people in (
            "candidates",
            "applicants",
            "employees",
            "hires",
            "staff",
            "team members",
            "talent",
        )
    ),
    "candidates who are local",
    "locals",
)
_FOR_LOCAL_PEOPLE = [
    *(f"{people.capitalize()} are expected on site 2 days per week." for people in _LOCAL_PEOPLE),
    *(
        f"On-site attendance of 2 days per week applies to {people}."
        for people in _LOCAL_PEOPLE
        if people != "locals"
    ),
    "If you are local, you are expected on site 2 days per week.",
    "You are expected on site 2 days per week if you are local.",
]
_NON_LOCAL_ALTERNATIVES = (
    "Candidates outside Toronto may work remotely.",
    "Staff living outside the Toronto area work from home.",
    "Remote candidates travel to Toronto quarterly.",
)
_PLACE_WORDS = ("locations", "cities", "hubs", "offices", "sites", "areas", "markets", "regions")
_FOR_A_SUBSET_OF_PLACES = [
    *(
        f"{preposition} {which} {places}, the position follows a hybrid model with 3 days "
        "per week in office."
        for preposition, which, places in itertools.product(
            ("For", "In", "At"), ("these", "those"), _PLACE_WORDS
        )
    ),
    "For candidates in these cities, the position is hybrid with 3 days per week in office.",
    "For the locations above, the position is hybrid with 3 days per week in office.",
    "For the offices listed, the position is hybrid with 3 days per week in office.",
]


def _attendance_in(sentences: str, limit: int, *, remote: Acceptance = Acceptance.REFUSED):
    """Return the office attendance gate for a hybrid Toronto posting stating ``sentences``."""
    parameters = _parameters(
        arrangement=Arrangement(
            remote=remote,
            hybrid=Acceptance.ACCEPTABLE,
            on_site=Acceptance.ACCEPTABLE,
            maximum_office_days=limit,
        )
    )
    posting = POSTING.replace(
        "This role is on site in Toronto, Canada, 4 days a week in the office.",
        f"This role is hybrid in Toronto, Canada. {sentences}",
    )
    found = gates(_reading(posting, parameters), parameters)
    return next(gate for gate in found if gate.name == "office attendance")


@pytest.mark.parametrize(
    "sentence", [*_NEAR_AN_OFFICE, *_NEAR_AN_OFFICE_VARIANTS, *_FOR_A_SUBSET_OF_PLACES]
)
def test_a_cadence_set_under_a_condition_of_place_sets_no_office_days(sentence: str) -> None:
    parameters = _parameters(
        arrangement=Arrangement(
            hybrid=Acceptance.ACCEPTABLE, on_site=Acceptance.ACCEPTABLE, maximum_office_days=1
        )
    )
    posting = POSTING.replace(
        "This role is on site in Toronto, Canada, 4 days a week in the office.",
        f"This role is hybrid in Toronto, Canada. {sentence}",
    )
    found = gates(_reading(posting, parameters), parameters)
    standings = {gate.name: gate.standing for gate in found}
    assert standings["arrangement"] is Standing.ELIGIBLE
    assert standings["office attendance"] is Standing.ELIGIBLE


@pytest.mark.parametrize(
    ("sentence", "alternative"), list(itertools.product(_FOR_LOCAL_PEOPLE, _NON_LOCAL_ALTERNATIVES))
)
def test_a_cadence_for_local_people_beside_an_alternative_sets_no_office_days(
    sentence: str, alternative: str
) -> None:
    gate = _attendance_in(f"{sentence} {alternative}", 1, remote=Acceptance.ACCEPTABLE)
    assert gate.standing is not Standing.INELIGIBLE


@pytest.mark.parametrize("sentence", _FOR_LOCAL_PEOPLE)
def test_a_cadence_for_local_people_with_no_alternative_still_sets_office_days(
    sentence: str,
) -> None:
    gate = _attendance_in(f"The team is based in our Toronto office. {sentence}", 1)
    assert gate.reason == "2 office days against your 1"


def test_an_unconditional_minimum_cadence_still_sets_office_days() -> None:
    gate = _office_gate("on average a minimum of three days per week", 2)
    assert gate.reason == "3 office days against your 2"


def test_a_monthly_cadence_counts_its_whole_weeks() -> None:
    assert _office_gate("8 days per month", 1).standing is Standing.ELIGIBLE
    assert _office_gate("8 days per month", 0).standing is Standing.INELIGIBLE


_REMOTE_ONLY = Arrangement(
    remote=Acceptance.PREFERRED,
    hybrid=Acceptance.REFUSED,
    on_site=Acceptance.REFUSED,
    maximum_office_days=0,
)


def _remote_only_standings(sentences: str) -> dict[str, Standing]:
    """Return each arrangement gate's standing for a remote-only search reading ``sentences``."""
    parameters = _parameters(arrangement=_REMOTE_ONLY)
    posting = POSTING.replace(
        "This role is on site in Toronto, Canada, 4 days a week in the office.", sentences
    )
    return {gate.name: gate.standing for gate in gates(_reading(posting, parameters), parameters)}


@pytest.mark.parametrize(
    "sentences",
    [
        "This role will not have the ability to be located remotely.",
        "You will not be able to work remotely in this position.",
        "Remote: No",
        "This is an on-site role in Toronto.\n\nBenefits\n- A remote-friendly culture",
        "Location: Remote, Canada\n4 days onsite per week required.",
    ],
)
def test_a_posting_ruling_out_remote_work_fails_a_remote_only_search(sentences: str) -> None:
    standings = _remote_only_standings(sentences)

    assert Standing.INELIGIBLE in {standings["arrangement"], standings["office attendance"]}


def test_ruling_out_remote_alone_leaves_a_search_accepting_hybrid_open() -> None:
    parameters = _parameters(
        arrangement=Arrangement(remote=Acceptance.PREFERRED, hybrid=Acceptance.ACCEPTABLE)
    )
    posting = POSTING.replace(
        "This role is on site in Toronto, Canada, 4 days a week in the office.",
        "This role will not have the ability to be located remotely.",
    )
    found = gates(_reading(posting, parameters), parameters)

    assert next(gate for gate in found if gate.name == "arrangement").standing is Standing.UNKNOWN


@pytest.mark.parametrize("option", ["#LI-Remote", "Work Anywhere.", "Work from anywhere."])
def test_an_explicit_remote_option_beside_a_cadence_passes_a_remote_only_search(
    option: str,
) -> None:
    standings = _remote_only_standings(
        f"Hybrid Working: Minimum 3 days per week in the office. {option}"
    )

    assert standings["arrangement"] is Standing.ELIGIBLE
    assert standings["office attendance"] is Standing.ELIGIBLE


_REMOTE_DOMAIN_NOUNS = (
    "remote patient monitoring",
    "remote-patient-monitoring",
    "Remote Patient Monitoring",
    "remote monitoring",
    "remote care",
    "remote sensing",
    "remote access",
    "remote diagnostics",
    "remote health",
    "remotely monitored patient",
)
_DOMAIN_SENTENCES = (
    "Northwind Health builds {noun} offerings.",
    "Our platform serves acute and {noun} settings.",
    "- Experience with {noun} programs",
)


@pytest.mark.parametrize(
    ("noun", "sentence"), list(itertools.product(_REMOTE_DOMAIN_NOUNS, _DOMAIN_SENTENCES))
)
def test_remote_naming_a_product_leaves_the_stated_attendance_to_decide(
    noun: str, sentence: str
) -> None:
    standings = _remote_only_standings(
        f"{sentence.format(noun=noun)}\nWe're working a minimum of 4 days a week onsite."
    )

    assert Standing.INELIGIBLE in {standings["arrangement"], standings["office attendance"]}


@pytest.mark.parametrize(
    "sentences",
    [
        "Location: REMOTE - US\n"
        "Employees who live near one of our hubs work in the office 2 days per week.",
        "We also support remote work across a number of U.S. states.\n"
        "Our Springfield hub works a hybrid schedule.",
        "Hybrid Working: Minimum 3 days per week in the office. #LI-Remote",
    ],
)
def test_remote_work_offered_beside_a_hub_cadence_stays_open(sentences: str) -> None:
    standings = _remote_only_standings(sentences)

    assert Standing.INELIGIBLE not in {standings["arrangement"], standings["office attendance"]}


@pytest.mark.parametrize("dash", ["-", "–"], ids=["hyphen", "en dash"])  # noqa: RUF001
def test_a_hybrid_role_with_distance_bands_fails_a_remote_only_search(dash: str) -> None:
    standings = _remote_only_standings(
        "While we are remote friendly, we prefer candidates near our Toronto office. "
        "This is a hybrid role. Applicants must live within 50 miles of the office. "
        "In-office expectations depend on distance; remaining days may be worked from home.\n"
        f"- 0{dash}30 miles: 3 days per week in-office\n"
        f"- 30{dash}50 miles: 1 day per week in-office"
    )

    assert standings["arrangement"] is Standing.INELIGIBLE
    assert standings["office attendance"] is Standing.INELIGIBLE


@pytest.mark.parametrize("dash", ["-", "–"], ids=["hyphen", "en dash"])  # noqa: RUF001
def test_a_remote_friendly_stance_beside_distance_bands_keeps_the_office_days(
    dash: str,
) -> None:
    standings = _remote_only_standings(
        "- While we are remote friendly, we prefer candidates near our Toronto or Ottawa "
        "office hubs.\n"
        "- In-office expectations depend on your distance from the office.\n"
        f"- 0{dash}30 km: 3 days per week in-office\n"
        f"- 30{dash}80 km: 1 day per week in-office"
    )

    assert standings["office attendance"] is Standing.INELIGIBLE


@pytest.mark.parametrize(
    "sentences",
    [
        "This is a hybrid role based in our Toronto office, or remotely within Canada.",
        "This is a hybrid role, based in any of our offices or remotely within Canada.",
        "Hybrid in Toronto, or fully remote anywhere in Canada.",
        "Remote work is not available outside Canada; this is a remote role within Canada.",
        "This is a fully remote role. You will not be able to work remotely from outside Canada.",
        "This is a remote role. Remote work is not available abroad.",
    ],
)
def test_a_remote_alternative_or_a_limit_on_where_passes_a_remote_only_search(
    sentences: str,
) -> None:
    standings = _remote_only_standings(sentences)

    assert Standing.INELIGIBLE not in {standings["arrangement"], standings["office attendance"]}


@pytest.mark.parametrize(
    "visits",
    [
        "Performs initiation and close-out visits (performed on-site or remotely).",
        "Monitoring visits are conducted in person or remotely.",
        "Visits may happen onsite or remotely within the region.",
    ],
)
def test_a_task_done_on_site_or_remotely_offers_no_remote_role(visits: str) -> None:
    standings = _remote_only_standings(
        f"{visits}\nThis is a hybrid role based in our Toronto office."
    )

    assert Standing.INELIGIBLE in {standings["arrangement"], standings["office attendance"]}


@pytest.mark.parametrize(
    "line",
    [
        "Active locksmith licensure; optional weekend overtime",
        "Active locksmith licensure; no relocation needed",
        "Active locksmith licensure (weekend overtime optional)",
    ],
)
def test_a_waiver_beside_a_required_credential_keeps_the_refusal(line: str) -> None:
    posting = CREDENTIAL_REQUIRED.replace("Active locksmith licensure is required", line)
    parameters = _parameters(excluded_conditions=[_TRADE_CONDITION])
    found = gates(_reading(posting, parameters), parameters)
    assert next(gate for gate in found if gate.name == "credential").standing is Standing.INELIGIBLE


@pytest.mark.parametrize("label", ["Qualifications:", "Requirements:"])
@pytest.mark.parametrize(
    "currency", ["All amounts in EUR.", "Figures are in EUR.", "Pay is quoted in EUR."]
)
def test_a_closing_statement_is_not_cut_from_the_currency_of_the_pay_before_it(
    label: str, currency: str
) -> None:
    parameters = _parameters(
        locations=[Location(key="de", country="Germany")],
        arrangement=Arrangement(remote=Acceptance.PREFERRED, on_site=Acceptance.ACCEPTABLE),
        compensation=[CompensationBand(location="de", currency="EUR", minimum=Decimal(50000))],
    )
    posting = (
        f"Data Analyst\n\nLocation: Berlin, Germany. {label} 3+ years of AP experience. Salary: "
        f"60,000 - 70,000 per year. Acme is an equal opportunity employer. {currency}\n"
    )
    found = gates(_reading(posting, parameters), parameters)
    assert next(gate for gate in found if gate.name == "compensation").standing is (
        Standing.ELIGIBLE
    )


@pytest.mark.parametrize("key", ["Location", "Shift", "Schedule", "Work Site"])
def test_a_preference_key_in_a_pay_run_leaves_the_pay_to_compare(key: str) -> None:
    parameters = _parameters(
        locations=[Location(key="us", country="United States")],
        arrangement=Arrangement(remote=Acceptance.PREFERRED, on_site=Acceptance.ACCEPTABLE),
        compensation=[CompensationBand(location="us", currency="USD", minimum=Decimal(100000))],
    )
    posting = (
        "Data Analyst\n\nRequirements\n- 3+ years of SQL\n- Python\n"
        "Pay Range:   $120,000 - $160,000   Other compensation:   Bonus Eligible   "
        f"{key}  Preference:   Springfield-based talent preferred, remote considered\n\n"
        "Perks & Benefits\n- Health insurance\n"
    )
    found = gates(_reading(posting, parameters), parameters)
    assert next(gate for gate in found if gate.name == "compensation").reason == (
        "at or above your USD annual floor"
    )


def _credential_standing(*sections: str, condition: tuple[str, str] = _TRADE_CONDITION) -> Standing:
    """Return the credential gate for a facilities posting made of ``sections``."""
    posting = "Facilities Coordinator\n\n" + "\n\n".join(sections) + "\n"
    parameters = _parameters(excluded_conditions=[condition])
    found = gates(_reading(posting, parameters), parameters)
    return next(gate for gate in found if gate.name == "credential").standing


_PRACTICE_CONDITION = ("Requires an active pharmacist or clinical practice license", "not held")
_PHYSICIAN_CONDITION = ("Requires an MD, DO, or physician licence", "not held")


@pytest.mark.parametrize(
    ("condition", "line", "standing"),
    [
        (_PRACTICE_CONDITION, "Current US healthcare professional license: PharmD", "ineligible"),
        (_PRACTICE_CONDITION, "PhD, MD or PharmD required", "eligible"),
        (_PRACTICE_CONDITION, "PharmD is required", "eligible"),
        (_PHYSICIAN_CONDITION, "MD, or MSc in a life science", "eligible"),
        (_PHYSICIAN_CONDITION, "Advanced degree (PharmD, PhD, MD) preferred", "eligible"),
        (_PHYSICIAN_CONDITION, "Experience working with physicians and pharmacists", "eligible"),
        (_PRACTICE_CONDITION, "Partner with licensed clinicians", "eligible"),
        (_PRACTICE_CONDITION, "Pharmacy residency preferred", "eligible"),
        (_PRACTICE_CONDITION, "A background in clinical practice", "eligible"),
    ],
)
@pytest.mark.parametrize("heading", ["Qualifications", "What you bring", "Requirements:"])
def test_a_required_licence_refuses_and_a_degree_or_colleague_does_not(
    condition: tuple[str, str], line: str, standing: str, heading: str
) -> None:
    found = _credential_standing(
        f"{heading}\n- {line}\n- 5+ years of analytics experience", condition=condition
    )
    assert found.value == standing


_LABELS_IN_A_DUTY = [
    f"{lead} {label}: fire code, ADA"
    for lead, label in itertools.product(
        ("Ensure compliance with", "Lead audits against our", "Working familiarity with"),
        ("Regulatory Requirements", "Safety Requirements", "Key Qualifications"),
    )
]


@pytest.mark.parametrize("duty", _LABELS_IN_A_DUTY)
@pytest.mark.parametrize("heading", ["Responsibilities", "What you'll do", "Nice to have"])
def test_a_label_inside_a_bullet_heads_no_bullet_after_it(duty: str, heading: str) -> None:
    standing = _credential_standing(
        f"{heading}\n- {duty}\n- Active locksmith license\n- Keep records",
        "Requirements\n- 3+ years of facilities experience",
    )
    assert standing is Standing.ELIGIBLE


@pytest.mark.parametrize(
    "label", ["Preferred Supplier List", "Preferred Vendor Programs", "Nice To Have Items"]
)
def test_a_preferred_label_inside_a_bullet_leaves_later_requirements_required(label: str) -> None:
    standing = _credential_standing(
        f"Requirements\n- 4+ years of buying experience, maintaining our {label}: audits\n"
        "- Active locksmith license\n- SQL"
    )
    assert standing is Standing.INELIGIBLE


@pytest.mark.parametrize(
    "closing",
    [
        "Equal opportunity employer reporting is part of the job",
        "Privacy notice drafting for our website is part of the job",
        "Reasonable accommodation requests are handled by this role",
    ],
)
def test_legal_words_inside_a_bullet_leave_the_bullets_after_it_required(closing: str) -> None:
    standing = _credential_standing(
        f"Requirements\n- 4+ years of HR experience. {closing}\n- Active locksmith license\n- SQL"
    )
    assert standing is Standing.INELIGIBLE


_BARE_CUE_ENDS = [
    f"{end} {cue}"
    for end, cue in itertools.product(
        (".", " (state, county, etc.)", " (U.S.)", " (L.S.)", ", etc."),
        ("Preferred", "Preferred.", "a plus", "Nice to have", "This is preferred"),
    )
]


@pytest.mark.parametrize(
    "line",
    [
        *(f"Active locksmith license{end}" for end in _BARE_CUE_ENDS),
        *(
            f"Active locksmith license {abbreviation}. Level 2 preferred"
            for abbreviation in ("incl", "esp", "min", "pref", "Incl", "INCL")
        ),
    ],
)
def test_a_bare_cue_after_a_credential_leaves_it_preferred(line: str) -> None:
    assert _credential_standing(f"Requirements:\n- {line}\n- SQL") is Standing.ELIGIBLE


_PHRASE_JOINS = (", ", " and ", ", but ", "; ")


@pytest.mark.parametrize(
    ("join", "waiver"),
    list(
        itertools.product(
            _PHRASE_JOINS,
            ("overtime is optional", "travel not needed", "a degree is not required"),
        )
    ),
)
def test_a_waived_phrase_joined_to_a_required_credential_keeps_the_refusal(
    join: str, waiver: str
) -> None:
    line = f"Active locksmith licensure{join}{waiver}"
    posting = CREDENTIAL_REQUIRED.replace("Active locksmith licensure is required", line)
    parameters = _parameters(excluded_conditions=[_TRADE_CONDITION])
    found = gates(_reading(posting, parameters), parameters)
    assert next(gate for gate in found if gate.name == "credential").standing is Standing.INELIGIBLE


@pytest.mark.parametrize(
    "sentence",
    [
        "You must live within commuting distance of our Toronto office and work in the "
        "office 3 days a week.",
        "You must be based near our Toronto office and work in the office 3 days per week.",
        "Candidates must reside within 50 miles of Toronto and work in the office 3 days a week.",
        "In the office 3 days per week, responding within 2 hours of a client request.",
        "In the office 3 days per week at our office close to the downtown campus.",
        "Staff within the clinic attend in person 3 days per week.",
        "In the office 3 days per week, working with people within the care team.",
    ],
)
def test_a_cadence_beside_a_nearness_requirement_still_sets_office_days(sentence: str) -> None:
    parameters = _parameters(
        arrangement=Arrangement(
            hybrid=Acceptance.ACCEPTABLE, on_site=Acceptance.ACCEPTABLE, maximum_office_days=2
        )
    )
    posting = POSTING.replace(
        "This role is on site in Toronto, Canada, 4 days a week in the office.",
        f"This role is hybrid in Toronto, Canada. {sentence}",
    )
    found = gates(_reading(posting, parameters), parameters)
    attendance = next(gate for gate in found if gate.name == "office attendance")
    assert attendance.reason == "3 office days against your 2"


@pytest.mark.parametrize(
    "sentence",
    [
        "You must work closely with clients and be based in Spain.",
        "You must confirm that you are located in Spain.",
        "You must, like other members of our team, be located in Spain.",
        "You must partner with our teams and live in Spain.",
    ],
)
def test_a_residence_verb_governing_the_candidate_beside_other_people_refuses(
    sentence: str,
) -> None:
    posting = FOREIGN_RESIDENCE.replace(
        "You must be located in Spain to be considered for this role.", sentence
    )
    gate = _residence_requirement_gate(posting, _residence_parameters())
    assert gate.standing is Standing.INELIGIBLE


@pytest.mark.parametrize(
    ("sentence", "reason"),
    [
        (
            "You must be located in the Eastern (EST) time zone.",
            "Pacific is 3 hours from Eastern against a tolerance of 0 hours: outside",
        ),
        (
            "You must be located in the Central US time zone.",
            "Pacific is 2 hours from Central against a tolerance of 0 hours: outside",
        ),
        (
            "You must be located in the Eastern time zone; bilingual Spanish is a plus.",
            "Pacific is 3 hours from Eastern against a tolerance of 0 hours: outside",
        ),
        (
            "You must be located in the Eastern time zone, within 2-3 hours of it.",
            "Pacific is 3 hours from Eastern against a tolerance of 3 hours: reaches",
        ),
        (
            "You must be located in the Eastern and/or Central time zones.",
            "Pacific is 3 hours from Eastern against a tolerance of 0 hours: outside",
        ),
    ],
)
def test_a_required_zone_reads_beside_an_abbreviation_a_wish_or_a_range(
    sentence: str, reason: str
) -> None:
    assert _time_zone_gate(sentence).reason == reason


_EASTERN_OUTSIDE = "Pacific is 3 hours from Eastern against a tolerance of 0 hours: outside"


@pytest.mark.parametrize(
    ("requirement", "join", "wish"),
    list(
        itertools.product(
            (
                "You must be located in the Eastern time zone",
                "Candidates must be based in the Eastern time zone",
            ),
            (", ", " and ", ", with ", " with ", ", and ", ", but ", " but ", "; ", ". "),
            ("healthcare experience is a plus", "Spanish would be an advantage", "Spanish a plus"),
        )
    ),
)
def test_a_wish_joined_to_a_required_zone_leaves_the_zone_required(
    requirement: str, join: str, wish: str
) -> None:
    # A sentence goes on in lowercase, so a wish after ". " opens with a capital.
    wish = wish[0].upper() + wish[1:] if join == ". " else wish
    assert _time_zone_gate(f"{requirement}{join}{wish}.").reason == _EASTERN_OUTSIDE


@pytest.mark.parametrize(
    ("opener", "before"),
    list(
        itertools.product(
            (
                "Ideally, you are based in",
                "Ideally you are based in",
                "Preferably, you are located in",
                "Preferably based in",
            ),
            ("", "You must work remotely. "),
        )
    ),
)
def test_a_wish_opening_the_sentence_leaves_the_zone_wished_for(opener: str, before: str) -> None:
    gate = _time_zone_gate(f"{before}{opener} the Eastern time zone.")
    assert gate.reason == "the posting requires no time zone"


@pytest.mark.parametrize(
    "sentence",
    [
        "Pacific time zone is preferred, but you must be located in the Eastern time zone.",
        "Pacific time zone is a plus; you must be located in the Eastern time zone.",
        "You must be located in the Eastern time zone, where our hours are beneficial to clients.",
        "Preferably based in Toronto; you must be located in the Eastern time zone.",
        "Ideally, you know SQL; you must be located in the Eastern time zone.",
    ],
)
def test_a_required_zone_beside_a_wished_zone_or_a_benefit_is_required(sentence: str) -> None:
    assert _time_zone_gate(sentence).reason == _EASTERN_OUTSIDE


@pytest.mark.parametrize(
    "sentence",
    [
        "You must be based in the US, with the Eastern time zone a plus.",
        "You must be based in the US with the Eastern time zone preferred.",
    ],
)
def test_a_zone_inside_a_wished_with_phrase_is_wished_for(sentence: str) -> None:
    assert _time_zone_gate(sentence).reason == "the posting requires no time zone"


@pytest.mark.parametrize(
    ("place", "join"),
    list(itertools.product(("the United States", "the US", "Canada"), (", ", " and ", " / "))),
)
def test_a_zone_listed_after_a_place_is_required(place: str, join: str) -> None:
    sentence = f"You must be located in {place}{join}Eastern time zone."
    assert _time_zone_gate(sentence).reason == _EASTERN_OUTSIDE


@pytest.mark.parametrize(
    ("sentence", "reason"),
    [
        (
            "Located in US or Canada, Eastern time zone or 1 hour offset.",
            "Pacific is 3 hours from Eastern against a tolerance of 1 hour: outside",
        ),
        ("You must work 9am-5pm, Eastern Time.", _EASTERN_OUTSIDE),
    ],
)
def test_a_zone_after_a_comma_is_required(sentence: str, reason: str) -> None:
    assert _time_zone_gate(sentence).reason == reason


@pytest.mark.parametrize(
    "sentence",
    [
        "For candidates located in the Toronto area, the role is in the office 3 days per week.",
        "For employees based in our Toronto hub, attendance is in the office 3 days per week.",
        "For staff living in Ontario, the role is in the office 3 days a week.",
        "If you are located in Toronto, you will work in the office 3 days a week.",
        "If you live in the Toronto area, you will be in the office 3 days per week.",
        "For staff living in the Toronto area, the role is in the office 3 days a week.",
        "Staff living in the Toronto area attend in person 3 days per week.",
        "Candidates local to Toronto work in the office 3 days per week.",
        "For staff local to Toronto, the role is in the office 3 days a week.",
        "For this location, the role is hybrid 3 days per week in the office.",
        "For staff in our Toronto office, the role is in the office 3 days a week.",
        "Working on local time, the role is in the office 3 days per week.",
        "Following local regulations, the role is in the office 3 days per week.",
        "The role is in the office 3 days per week, visiting local clinical sites.",
        *(
            f"The role is in the office 3 days per week, {tail}."
            for tail in (
                "supporting clinics with experience in these areas",
                "covering accounts in these regions",
                "while you grow our share in these markets",
                "selling in the regions listed below",
                "advising employees near retirement",
                "coaching staff close to the bedside",
                "building loyalty with locals and tourists",
                "for these cities",
            )
        ),
        "The role is in the office 3 days per week at these locations: Toronto, Ottawa.",
        "Remote work is optional for teams in the U.S. The role is in the office 3 days per week.",
        "Hybrid for staff near our offices in the U.S. Everyone else is in the office 3 days per "
        "week.",
    ],
)
def test_a_cadence_for_people_in_a_place_still_sets_office_days(sentence: str) -> None:
    parameters = _parameters(
        arrangement=Arrangement(
            hybrid=Acceptance.ACCEPTABLE, on_site=Acceptance.ACCEPTABLE, maximum_office_days=2
        )
    )
    posting = POSTING.replace(
        "This role is on site in Toronto, Canada, 4 days a week in the office.",
        f"This role is hybrid in Toronto, Canada. {sentence}",
    )
    found = gates(_reading(posting, parameters), parameters)
    attendance = next(gate for gate in found if gate.name == "office attendance")
    assert attendance.reason == "3 office days against your 2"
