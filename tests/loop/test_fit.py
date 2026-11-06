"""The deterministic reading, and above all the postings it must refuse to read.

Half of this file is negative controls, because the failure mode of a keyword
matcher is not silence but a confident answer about a posting it misread. Each
control is a posting a naive implementation gets wrong, and the assertion is
that this one says "not stated" instead. Every posting here is invented.
"""

from __future__ import annotations

import itertools
from datetime import date
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from clearscout.application.master_cv.cv import CvInventory, parse_cv
from clearscout.application.tailoring import fit
from clearscout.application.tailoring.fit.structure import AnswerName
from clearscout.application.tailoring.stages.jobspec import (
    HardGate,
    Importance,
    JobSpec,
    Requirement,
)
from clearscout.application.tailoring.stages.spans import locate
from clearscout.domain import taxonomy
from clearscout.domain.requirements import GateKind
from clearscout.domain.search_parameters import Acceptance, Arrangement, JobSearchParameters
from clearscout.domain.taxonomy import Vocabulary
from clearscout.infrastructure.web_fetch.board_mappings import plain_description
from clearscout.shared.layout import ITEM_MARKER, plain_layout
from clearscout.shared.text import sanitize
from tests.support.posting_bodies import (
    BODY_SHAPES,
    PREFERRED_HEADING,
    REQUIRED_HEADING,
    canonical_body,
)
from tests.support.reports import rendered_report, report_section

RULES_SECTION = "Read from the text by rule"

TODAY = date(2026, 6, 1)


def _spec_calling_it(importance: Importance, text: str) -> JobSpec:
    """A posting extraction that grades one requirement, so a row can disagree."""
    return JobSpec(
        title="Staff Engineer",
        company="Northwind",
        seniority=None,
        location=None,
        requirements=(Requirement(id="r01", text=text, importance=importance),),
        keywords=(),
        hard_gates=(),
        origin="test",
    )


def _importance_rows(
    posting: str, inventory: CvInventory, importance: Importance, text: str
) -> list[str]:
    """Every importance disagreement the reading reports about one requirement."""
    reading = fit.read(posting, inventory, today=TODAY)
    return [
        item.subject
        for item in fit.disagreements(reading, _spec_calling_it(importance, text))
        if item.subject.startswith("Importance of")
    ]


CLEARLY_GRADED = """
Staff Engineer, Northwind

Requirements:
- Eight years of experience building distributed systems

Nice to have:
- Experience running Kafka in production for a regulated workload
"""

# The same phrase graded two ways in one document.
CONTRADICTORY_GRADES = """
Staff Engineer, Northwind

Requirements:
- Experience running Kafka in production for a regulated workload

Nice to have:
- Experience running Kafka in production for a regulated estate
"""


def test_the_reading_disagrees_when_the_posting_grades_a_requirement_plainly(
    inventory: CvInventory,
) -> None:
    """The positive control. Without it the abstention below proves nothing."""
    assert _importance_rows(
        CLEARLY_GRADED,
        inventory,
        Importance.MUST_HAVE,
        "Experience running Kafka in production for a regulated workload",
    ) == ["Importance of r01"]


def test_the_reading_abstains_when_the_posting_grades_one_requirement_two_ways(
    inventory: CvInventory,
) -> None:
    """Picking either would print a disagreement whose "rules say" half is a coin toss."""
    assert (
        _importance_rows(
            CONTRADICTORY_GRADES,
            inventory,
            Importance.MUST_HAVE,
            "Experience running Kafka in production for a regulated workload",
        )
        == []
    )


DATA_ARCHITECT = """
Senior Data Architect, Northwind Analytics

Northwind runs a regulated data estate across eleven business domains.

Responsibilities:
- Own the canonical data model and the enterprise data model beneath it
- Chair the data governance forum and set data quality rules with each data domain
  ownership group
- Run master data management for customer and product, including the golden record,
  entity resolution, and the survivorship rules behind both
- Keep reference data and every shared code set current

Requirements:
- 8+ years of experience in data architecture
- Bachelor's degree in a technical field or equivalent experience
- Demonstrated data stewardship across a system of record

Preferred qualifications:
- Experience with an interoperability standard
- Public speaking is a plus

This role is hybrid, three days per week in the office in Riverbend. We are unable to
sponsor visas for this position.

Benefits and legal:
- We offer a 401(k) match and paid time off
- Northwind is an equal opportunity employer and all qualified applicants will receive
  consideration for employment without regard to any protected status
"""

SALES_PIPELINE = """
Account Executive, Mid-Market

You will own your pipeline end to end and manage the product story for our buyers.

Responsibilities:
- Build sales pipeline through prospecting calls and discovery call follow-up
- Close deals against a sales quota and keep the territory plan current
- Maintain the customer relationship management system

Requirements:
- 3+ years of experience selling software
- Willingness to travel
"""

FACTORY_PRODUCT = """
Production Supervisor, Riverbend Plant

Our product line runs three shifts.

Responsibilities:
- Hold the production schedule and the shift schedule for the line
- Watch inventory levels and supplier performance week to week
- Keep every standard operating procedure current and hit the throughput target

Requirements:
- 5 years of experience in a manufacturing setting
"""

AMBIGUOUS = """
Pipeline Manager

We are looking for a pipeline manager to own delivery of our product across the region.

What you will do:
- Manage the pipeline and keep stakeholders informed
- Drive the product forward with the team

Requirements:
- Strong communication
- Comfort with ambiguity
"""

INFLATED_TITLE = """
Senior Director of Data Strategy

Join our team as a Senior Director of Data Strategy.

What you will do:
- Assist the analytics team under close supervision
- Help maintain reports and spreadsheets

Requirements:
- 2 years of experience in an office setting
- Willingness to learn
"""

MANAGES_WORK_NOT_PEOPLE = """
Manager, Data Governance

You will manage the roadmap, manage stakeholders, and lead the migration to the new
catalog.

Responsibilities:
- Manage the backlog for the governance program and manage vendor relationships
- Publish data lineage, run metadata management, and keep the data catalog current
- Own the data quality rules and the business glossary

Requirements:
- 5 years of experience in data governance
"""

MANAGES_PEOPLE = """
Data Governance Lead

Responsibilities:
- Set data governance policy across the estate
- You will manage a team of four stewards and run their performance reviews

Requirements:
- 7 years of experience in data stewardship
"""

TITLED_MANAGER_IS_IC = """
Engineering Manager, Payments

Responsibilities:
- Write and review payment services code with the team
- This is an individual contributor role with no direct reports

Requirements:
- 6 years of experience building software
"""

BOILERPLATE_ONLY = """
Team Member, Riverbend Site

Riverbend is a good place to work.

Benefits and legal:
- We offer a 401(k) with company match and paid time off
- All employees must complete annual compliance training
- Riverbend is an equal opportunity employer and all qualified applicants will receive
  consideration for employment without regard to race, religion, or veteran status
- Reasonable accommodation is available on request
- The salary range for this position is posted below
"""

NURSE = """
Registered Nurse, Medical-Surgical Unit

Ridgeline operates a 200 bed hospital.

Responsibilities:
- Provide patient care on a 32 bed unit and complete each clinical assessment on
  admission
- Medication administration and shift handoff for a six patient assignment
- Contribute to the care plan and discharge planning with the care team

Requirements:
- Active licensure and 2 years of experience in an acute setting
- Bachelor of Science in Nursing preferred
- Must be able to work night shifts on site
"""

DESIGNER = """
Product Designer, Lakeshore Software

Requirements:
- 4 years of experience in interaction design
- A portfolio showing usability testing and wireframe work

Responsibilities:
- Extend the design system and run a weekly design critique
- Take each prototype iteration through usability testing
"""

FINANCIAL_ANALYST = """
Financial Analyst, Corporate FP&A

Requirements:
- 3 years of experience in a finance team
- Comfort building a financial model from a general ledger extract

Responsibilities:
- Own the month end close checklist and the budget forecast
- Present variance analysis by cost center
"""

BACKEND_ENGINEER = """
Backend Engineer, Kettle Logistics

Requirements:
- 4 years of experience writing server side software
- Fluency in at least one typed language

Responsibilities:
- Build and operate a rest api behind a microservice boundary
- Take part in code review and keep unit tests honest
- Pay down technical debt in the routing service
"""

SPONSORED_REMOTE = """
Staff Engineer, Remote

This role is fully remote. Visa sponsorship is available for the right candidate.

Requirements:
- 9 years of experience in distributed systems
"""

CONTRADICTORY_LOCATION = """
Analyst, Riverbend

This role is remote. You will also be expected on site in Riverbend most weeks.

Requirements:
- Experience with spreadsheets
"""

HYBRID_WITH_WORK_FROM_HOME = """
Product Manager, Real World Evidence

In this hybrid role, you will have a defined work location that includes work from
home and 3 office days set by you and your team.

Requirements:
- Experience with spreadsheets
"""

ONSITE_INTERVIEW_ONLY = """
Data Manager

We are hiring a Data Manager to own the reporting stack. You will come onsite for
interviews before an offer is made.

Requirements:
- Experience with spreadsheets
"""

ONSITE_ROLE = """
Data Manager

The successful candidate will work onsite at our Riverbend office.

Requirements:
- Experience with spreadsheets
"""

NO_DATES = """# PAT LEE

Toronto | pat@example.com

## SUMMARY

Analyst.

## PROFESSIONAL EXPERIENCE

### Northwind

**Analyst**

- Built the weekly revenue report
"""


def reading_of(posting: str, inventory: CvInventory, **kwargs: object) -> fit.Reading:
    """Read one posting against the shared fixture record."""
    vocabulary = kwargs.get("vocabulary")
    assert vocabulary is None or isinstance(vocabulary, Vocabulary)
    return fit.read(posting, inventory, vocabulary=vocabulary, today=TODAY)


def spec_for(
    *,
    seniority: str | None = None,
    location: str | None = None,
    requirements: tuple[tuple[str, str, Importance], ...] = (),
    gates: tuple[tuple[GateKind, str], ...] = (),
) -> JobSpec:
    """Build the model's side of a comparison, with only the fields it decides."""
    return JobSpec(
        title="Senior Data Architect",
        company="Northwind Analytics",
        seniority=seniority,
        location=location,
        requirements=tuple(
            Requirement(id=rid, text=text, importance=importance)
            for rid, text, importance in requirements
        ),
        keywords=(),
        hard_gates=tuple(HardGate(kind=kind, text=text, blocking=True) for kind, text in gates),
        origin="test",
    )


def test_pipeline_about_selling_is_not_read_as_data_work(inventory: CvInventory) -> None:
    """The taxonomy carries "sales pipeline" and "data pipeline", never the bare noun."""
    reading = reading_of(SALES_PIPELINE, inventory)
    assert reading.role_family.value == "Sales and business development"
    # The bare noun must not be what carried the reading.
    assert "pipeline" not in {phrase.casefold() for phrase in reading.role_family.evidence}


def test_product_about_a_physical_good_is_not_product_management(
    inventory: CvInventory,
) -> None:
    """A factory posting says "product line" and manages neither a roadmap nor a backlog."""
    reading = reading_of(FACTORY_PRODUCT, inventory)
    assert reading.role_family.value == "Operations and supply chain"
    assert "product" not in {phrase.casefold() for phrase in reading.role_family.evidence}


def test_a_posting_two_families_read_equally_well_is_refused(inventory: CvInventory) -> None:
    """The margin, doing the job the minimum cannot.

    This posting is about a pipeline, a product, delivery, and stakeholders,
    and that is not enough to place it.
    """
    reading = reading_of(AMBIGUOUS, inventory)
    assert reading.role_family.value is None
    assert reading.role_family.render() == "not stated"
    assert "vocabulary" in reading.role_family.note
    assert reading.specialty.value is None


def test_a_title_claiming_a_level_the_posting_does_not_carry_is_refused(
    inventory: CvInventory,
) -> None:
    """Title inflation. A director's title over two years and close supervision."""
    reading = reading_of(INFLATED_TITLE, inventory)
    assert reading.seniority.value is None
    assert "under close supervision" in reading.seniority.note
    assert "director" in reading.seniority.note
    # The years are still reported. The posting stated them; it is the level that
    # nothing in the text supports.
    assert reading.years.value == 2


def test_a_title_far_above_the_stated_experience_is_refused(inventory: CvInventory) -> None:
    """The same control without the supervision language, so the years carry it alone."""
    posting = "Director of Engineering\n\nRequirements:\n- 2 years of experience in a team\n"
    reading = reading_of(posting, inventory)
    assert reading.seniority.value is None
    assert "do not agree" in reading.seniority.note


@pytest.mark.parametrize(
    "item",
    ["Current CPR certification", "Valid driver's license.", "Active RN license in good standing"],
)
def test_a_bare_licence_item_leaves_prose_years_read(item: str, inventory: CvInventory) -> None:
    posting = (
        "Clinical Data Analyst\n\nAbout the role\n"
        "You will bring 7+ years of experience in healthcare analytics to our team.\n\n"
        f"Experience :\n- SQL and Python\n- {item}\n"
    )
    assert reading_of(posting, inventory).years.value == 7


def test_managing_work_is_not_managing_people(inventory: CvInventory) -> None:
    """Five uses of "manage" over a backlog, roadmap, vendors, and a migration, and no report."""
    reading = reading_of(MANAGES_WORK_NOT_PEOPLE, inventory)
    assert reading.orientation.value is None
    assert "manager" in reading.orientation.note
    assert "does not settle it" in reading.orientation.note


def test_a_posting_that_states_no_level_at_all_is_refused(inventory: CvInventory) -> None:
    """Weak seniority evidence. No level word, no experience floor, no answer."""
    reading = reading_of(AMBIGUOUS, inventory)
    assert reading.seniority.value is None
    assert "names a level" in reading.seniority.note
    assert reading.years.value is None


def test_legal_and_benefits_language_is_not_a_requirement(inventory: CvInventory) -> None:
    """Boilerplate. It says "must", "require", and "employees", and asks for nothing.

    A naive extractor reads five requirements out of this posting. The reading
    finds none, and says so on every question the boilerplate would have answered.
    """
    reading = reading_of(BOILERPLATE_ONLY, inventory)
    assert reading.requirements == ()
    assert reading.must_have.value is None
    assert reading.preferred.value is None
    assert reading.sections.value is None
    assert reading.education.value is None
    assert reading.years.value is None
    assert reading.role_family.value is None


def test_a_posting_offering_remote_beside_on_site_is_read_as_remote(
    inventory: CvInventory,
) -> None:
    """A posting naming remote is offering it, and the attendance it also names is kept."""
    reading = reading_of(CONTRADICTORY_LOCATION, inventory)
    assert reading.location.value == "remote"
    assert any("on site in Riverbend" in quote for quote in reading.location.evidence)


def test_a_benefits_perk_does_not_override_a_stated_hybrid_arrangement(
    inventory: CvInventory,
) -> None:
    """A perks-list "work from anywhere" is a benefit, not the role's arrangement."""
    reading = reading_of(
        """
Principal Data Product Manager

Riverbend

Product - Product Management /
Full time /
Hybrid

Benefits
- Flexible work hours & the opportunity to work from anywhere
""",
        inventory,
    )
    assert reading.location.value == "hybrid"


@pytest.mark.parametrize(
    "task",
    [
        "Conduct on-site and remote monitoring of clinical research studies.",
        "Conduct onsite and remote monitoring visits at investigator sites.",
        "Perform onsite/remote visits at investigator sites.",
        "Lead in-person or remote monitoring visits.",
        "Provide on-site support to hospital customers.",
        "Complete onsite monitoring visits each quarter.",
        "Schedule on-site visits with clinic staff.",
    ],
)
def test_an_arrangement_word_naming_a_task_states_no_arrangement(
    task: str, inventory: CvInventory
) -> None:
    reading = reading_of(
        f"Clinical Research Associate\n\nResponsibilities\n- {task}\n- Review site data.",
        inventory,
    )

    assert reading.location.value is None


@pytest.mark.parametrize(
    ("posting", "arrangement"),
    [
        ("This position is 4-day onsite position.", "on site"),
        ("We build remote patient monitoring tools. This is a fully remote role.", "remote"),
    ],
)
def test_an_arrangement_beside_a_task_noun_still_reads(
    posting: str, arrangement: str, inventory: CvInventory
) -> None:
    reading = reading_of(f"Clinical Research Associate\n\nAbout the role\n{posting}", inventory)

    assert reading.location.value == arrangement


def test_a_stated_hybrid_role_is_not_overridden_by_a_work_from_home_mention(
    inventory: CvInventory,
) -> None:
    """A work-from-home mention inside a stated hybrid role is the split's remote half."""
    reading = reading_of(HYBRID_WITH_WORK_FROM_HOME, inventory)
    assert reading.location.value == "hybrid"


def test_an_office_day_cadence_reads_as_hybrid_without_the_word_hybrid(
    inventory: CvInventory,
) -> None:
    """A stated weekly office cadence is a split week even when the posting says remote too."""
    reading = reading_of(
        """
Analyst

This position requires 3 days a week in the office and work from home the rest.

Requirements:
- Experience with spreadsheets
""",
        inventory,
    )
    assert reading.location.value == "hybrid"


@pytest.mark.parametrize(
    ("sentence", "arrangement"),
    [
        (
            "Hybrid Work Model: This section is applicable to onsite employees who are "
            "eligible for hybrid work location as specified by management and related "
            "policies. Acme has defined days for in-person/onsite collaboration and "
            "work-from-home days for individual-focused time.",
            "hybrid",
        ),
        ("Expect two in-office days and three work-from-home days per week.", "hybrid"),
        ("Globex has defined in-person days and work-from-home days.", "hybrid"),
        ("WFH days on Mondays and Fridays; the rest in office.", "hybrid"),
        ("Teams set remote days and office days together.", "hybrid"),
        ("This role is fully remote.", "remote"),
        ("You can work from home anywhere in Canada.", "remote"),
        ("Work from home with occasional travel.", "remote"),
        ("Remote, with optional in-person days at our Toronto office.", "remote"),
        ("We offer remote work; employees may choose in-person days.", "remote"),
    ],
)
def test_remote_days_in_a_split_week_are_hybrid_not_a_remote_offer(
    sentence: str, arrangement: str, inventory: CvInventory
) -> None:
    reading = reading_of(
        f"Analyst\n\nSpringfield, IL\n\nAbout the role\n{sentence}\n\n"
        "Requirements:\n- Experience with spreadsheets\n",
        inventory,
    )

    assert reading.location.value == arrangement


def test_an_employer_hybrid_model_does_not_override_a_remote_role(
    inventory: CvInventory,
) -> None:
    """A company "hybrid work model" that names no cadence does not make a remote role hybrid."""
    reading = reading_of(
        """
Senior Data Product Manager

Remote, United States. Northwind has a hybrid work model with hubs in several cities.

Requirements:
- Experience with spreadsheets
""",
        inventory,
    )
    assert reading.location.value == "remote"


def test_a_travel_share_beside_customer_sites_is_not_office_attendance(
    inventory: CvInventory,
) -> None:
    """A percentage stating travel is not a share of the week spent in an office."""
    reading = reading_of(
        """
Senior Product Manager

Requirements:
- Ability to travel for on-site customer meetings and events (approximately 10-15% travel).
- Experience with spreadsheets
""",
        inventory,
    )
    assert reading.location.value is None


def test_hybrid_offered_as_a_perk_is_not_the_roles_arrangement(
    inventory: CvInventory,
) -> None:
    """An arrangement listed among benefits as an option states no requirement to attend."""
    reading = reading_of(
        """
Senior Data Engineer

We offer competitive salaries, flexible working hours, hybrid work options, and an inclusive team.

Requirements:
- Experience with spreadsheets
""",
        inventory,
    )
    assert reading.location.value is None


_OFFER_HEADINGS = ("Benefits", "Perks", "Benefits & Perks", "What we offer")
_OFFERED_ATTENDANCE = (
    "Hybrid Work Environment",
    "Hybrid and remote options",
    "Flexible hybrid work",
    "A hybrid working model",
    "In-person team summits twice a year",
)


@pytest.mark.parametrize("heading", _OFFER_HEADINGS)
@pytest.mark.parametrize("item", _OFFERED_ATTENDANCE)
@pytest.mark.parametrize(
    ("stated", "expected"), [("", None), ("Location: Remote, United States\n\n", "remote")]
)
def test_attendance_listed_among_benefits_is_not_the_roles_arrangement(
    heading: str, item: str, stated: str, expected: str | None, inventory: CvInventory
) -> None:
    posting = (
        f"Product Manager\n\n{stated}About the role\nYou will own the roadmap.\n\n"
        f"Requirements:\n- SQL\n\n{heading}\n- {item}\n- Health insurance"
    )
    assert reading_of(posting, inventory).location.value == expected


@pytest.mark.parametrize("heading", _OFFER_HEADINGS)
@pytest.mark.parametrize(
    "items", ["- Hybrid schedule with 3 days a week in the office", "- Dental cover\n#LI-Hybrid"]
)
def test_a_cadence_or_tag_after_benefits_still_reads_as_hybrid(
    heading: str, items: str, inventory: CvInventory
) -> None:
    posting = (
        "Product Manager\n\nAbout the role\nYou will own the roadmap.\n\nRequirements:\n- SQL\n\n"
        f"{heading}\n{items}\n- Health insurance"
    )
    assert reading_of(posting, inventory).location.value == "hybrid"


_PERK_BULLETS = ("- ", "\N{ROCKET} ")
# An emoji item under "What we offer" leaves the heading unparsed, so "offer" opens its line.
_PERK_LISTS = [
    (heading, bullet)
    for heading, bullet in itertools.product(_OFFER_HEADINGS, _PERK_BULLETS)
    if (heading, bullet) != ("What we offer", "\N{ROCKET} ")
]


@pytest.mark.parametrize(("heading", "bullet"), _PERK_LISTS)
@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ("Work from our Springfield office-Hybrid", "hybrid"),
        ("Work from our Springfield office - Hybrid", "hybrid"),
        ("Work from our Springfield office \N{EN DASH} Hybrid", "hybrid"),
        ("Work from our Springfield office (hybrid)", "hybrid"),
        ("Work from our vibrant Springfield office-Hybrid", "hybrid"),
        ("Work from our new downtown Springfield office - Hybrid", "hybrid"),
        ("In-office 3 days a week at our Austin office", "on site"),
        ("On site at our Denver office", "on site"),
        ("Onsite at our Denver office", "on site"),
        ("In person at our Denver office", "on site"),
    ],
)
def test_a_perk_placing_the_arrangement_at_a_named_office_reads_as_attendance(
    heading: str, bullet: str, item: str, expected: str, inventory: CvInventory
) -> None:
    posting = (
        "Product Manager\n\nAbout the role\nYou will own the roadmap.\n\nRequirements:\n- SQL\n\n"
        f"{heading}\n{bullet}{item}\n{bullet}Health insurance"
    )
    assert reading_of(posting, inventory).location.value == expected


@pytest.mark.parametrize("heading", _OFFER_HEADINGS)
@pytest.mark.parametrize("bullet", _PERK_BULLETS)
@pytest.mark.parametrize(
    "item",
    [
        "Hybrid-friendly culture events",
        "Home office budget",
        "Hybrid setup kit for the Home Office",
        "Flexible PTO",
        "Free lunch at our Denver office",
        "Free on-site parking at our Denver office",
        "Hybrid options at our Austin office",
        "Catered lunch at the office, hybrid-friendly",
    ],
)
def test_an_ordinary_perk_states_no_arrangement(
    heading: str, bullet: str, item: str, inventory: CvInventory
) -> None:
    posting = (
        "Product Manager\n\nAbout the role\nYou will own the roadmap.\n\nRequirements:\n- SQL\n\n"
        f"{heading}\n{bullet}{item}\n{bullet}Health insurance"
    )
    assert reading_of(posting, inventory).location.value is None


@pytest.mark.parametrize("heading", _OFFER_HEADINGS)
def test_a_named_office_in_another_perk_on_the_same_line_states_no_arrangement(
    heading: str, inventory: CvInventory
) -> None:
    posting = (
        "Product Manager\n\nAbout the role\nYou will own the roadmap.\n\nRequirements:\n- SQL\n\n"
        f"{heading}\n\N{ROCKET} Hybrid-friendly culture events\n"
        "\N{ROCKET} Free lunch at our Denver office"
    )
    assert reading_of(posting, inventory).location.value is None


@pytest.mark.parametrize(
    "attendance",
    [
        "The role is on site five days per week.",
        "The role is in office three days per week.",
        "You are required in the office three days a week.",
        "Must be in the office five days a week.",
        "The successful candidate must be in the office three days a week.",
    ],
)
def test_remote_offered_as_a_perk_beside_committed_attendance_is_a_conflict(
    attendance: str, inventory: CvInventory
) -> None:
    """Neither side wins, so the arrangement is unknown and cannot refuse the posting."""
    reading = reading_of(
        f"""
Data Manager

Benefits include remote work options. {attendance}

Requirements:
- Experience with spreadsheets
""",
        inventory,
    )
    assert reading.location.value is None
    assert reading.location.conflict
    assert len(reading.location.evidence) == 2


@pytest.mark.parametrize(
    "arrangement",
    [
        "We offer remote work. This role is on site for the first week of onboarding.",
        "Benefits include remote work options. This role is fully remote; onsite visits are "
        "voluntary.",
        "Perks: remote work options, wellness stipend. This role is 100% remote apart from an "
        "in person orientation week.",
        "Remote work options are part of our benefits. The position is fully remote but you "
        "will be on site for your first week.",
        "Benefits include remote work options. You may come on site a few days a week if you like.",
        "We offer remote work. This role is on site once a week for the first month.",
        "Benefits include remote work options. Reception is in person five days a week.",
        "We offer remote work as a perk. Our in-office gym is open five days a week.",
        "Remote work options are part of our benefits. Hybrid teams are on site three days a week.",
        "Benefits include remote work options. Our cafeteria is in the office five days a week.",
        "Remote work is offered as a benefit. Your manager is required in the office three days "
        "a week.",
        "Benefits include remote work options. The lab technicians are required in the office "
        "five days a week.",
        "Benefits include remote work options. Reception is required in the office five days a "
        "week.",
        "We offer remote work for this role. Our reception desk is required in the office five "
        "days a week.",
        "Benefits include remote work options. The team you support is in the office five days "
        "a week.",
        "Benefits include remote work options. The team you'll support is in the office five "
        "days a week.",
        "Benefits include remote work options. Hybrid employees are required in the office "
        "three days a week.",
        "Benefits include remote work options. Snacks are provided for you in the office five "
        "days a week.",
        "Benefits include remote work options. You are welcome in the office five days a week.",
        "Benefits include remote work options. The manager for this role is required in the "
        "office three days a week.",
        "Benefits include remote work options. Working parents can use our creche in the office "
        "five days a week.",
        "Benefits include remote work options. On-site security is in the office five days a week.",
        "Benefits include remote work options. Required equipment is kept in the office five "
        "days a week.",
        "Benefits include remote work options. The candidate's future manager is required in "
        "the office three days a week.",
        "Benefits include remote work options.\n- On-site gym open in the office five days a week",
    ],
)
def test_a_bounded_visit_or_a_remote_declaration_keeps_an_offered_remote_role_remote(
    arrangement: str, inventory: CvInventory
) -> None:
    """Only a sentence committing this role to attend puts an offered remote role in doubt."""
    reading = reading_of(
        f"""
Data Manager

{arrangement}

Requirements:
- Experience with spreadsheets
""",
        inventory,
    )
    assert reading.location.value == "remote"


@pytest.mark.parametrize(
    "sentence",
    [
        "This role is remote, although the team is in the office five days a week.",
        "This role is remote, yet reception is in the office five days a week.",
        "This role is remote, whereas our reception is in the office five days a week.",
        "This role is remote, with our facilities team in the office five days a week.",
        "The team is in the office five days a week. This is a remote position.",
        "The role reports to a manager, who is in the office five days a week.",
        "This role is fully remote. Our engineers are in the office 40% of the week.",
    ],
)
def test_a_role_declared_remote_stays_remote_beside_a_perk_and_somebody_elses_attendance(
    sentence: str, inventory: CvInventory
) -> None:
    reading = reading_of(
        f"""
Data Manager

Benefits include remote work options. {sentence}

Requirements:
- Experience with spreadsheets
""",
        inventory,
    )
    assert reading.location.value == "remote"


@pytest.mark.parametrize(
    "sentence",
    [
        "You will partner with engineers in the office five days a week.",
        "You'll join a team based in the office five days a week.",
        "The role's hiring manager is in the office five days a week.",
        "The role is supported by an assistant in the office five days a week.",
    ],
)
def test_attendance_the_reader_cannot_attribute_is_a_conflict_and_never_an_arrangement(
    sentence: str, inventory: CvInventory
) -> None:
    reading = reading_of(
        f"""
Data Manager

Benefits include remote work options. {sentence}

Requirements:
- Experience with spreadsheets
""",
        inventory,
    )
    assert reading.location.value is None
    assert reading.location.conflict
    assert reading.location.note == (
        "the posting offers remote work and also states office attendance"
    )


@pytest.mark.parametrize(
    "sentence",
    [
        "Perks include remote work options. Our engineers are in the office 40% of the week.",
        "Perks include remote work options. Up to two days a week in the office.",
        "Benefits include remote work options and a hybrid schedule of three days in the office.",
        "Remote candidates are welcome to apply. Perks include remote work options. Our "
        "engineers are in the office 40% of the week.",
    ],
)
def test_a_split_week_nobody_is_named_for_is_a_conflict_beside_a_remote_perk(
    sentence: str, inventory: CvInventory
) -> None:
    reading = reading_of(
        f"""
Data Manager

{sentence}

Requirements:
- Experience with spreadsheets
""",
        inventory,
    )
    assert reading.location.value is None
    assert reading.location.conflict


@pytest.mark.parametrize(
    "sentence",
    [
        "This is a hybrid role with three days a week in the office.",
        "You will be in the office 40% of the week.",
    ],
)
def test_a_split_week_stated_for_this_role_stays_hybrid_beside_a_remote_perk(
    sentence: str, inventory: CvInventory
) -> None:
    reading = reading_of(
        f"""
Data Manager

Perks include remote work options. {sentence}

Requirements:
- Experience with spreadsheets
""",
        inventory,
    )
    assert reading.location.value == "hybrid"


def test_remote_offered_to_other_teams_beside_this_roles_attendance_is_a_conflict(
    inventory: CvInventory,
) -> None:
    reading = reading_of(
        """
Data Manager

This role is on site five days a week. Remote work is offered as a perk for other teams.

Requirements:
- Experience with spreadsheets
""",
        inventory,
    )
    assert reading.location.value is None
    assert reading.location.conflict


def test_a_client_mention_does_not_erase_an_office_cadence_in_the_same_sentence(
    inventory: CvInventory,
) -> None:
    """A sentence stating this role's office days keeps them whatever else it mentions."""
    reading = reading_of(
        """
Senior Product Manager

Remote candidates are welcome, but this position requires 3 days a week in the office with clients.

Requirements:
- Experience with spreadsheets
""",
        inventory,
    )
    assert reading.location.value == "hybrid"


def test_a_fully_remote_declaration_outweighs_a_stated_hybrid(
    inventory: CvInventory,
) -> None:
    """An unambiguous whole-role remote declaration wins even beside a hybrid cadence line."""
    reading = reading_of(
        """
Analyst

This is a fully remote role. Some teams keep a hybrid role with 3 office days a week.

Requirements:
- Experience with spreadsheets
""",
        inventory,
    )
    assert reading.location.value == "remote"


def test_a_full_week_of_office_days_is_on_site_not_hybrid(
    inventory: CvInventory,
) -> None:
    """Five office days a week is a full on-site week, not a hybrid split."""
    reading = reading_of(
        """
Data Manager

This is an onsite role requiring 5 office days a week.

Requirements:
- Experience with spreadsheets
""",
        inventory,
    )
    assert reading.location.value == "on site"


@pytest.mark.parametrize(
    ("tag", "arrangement"),
    [
        ("#LI-Remote", "remote"),
        ("#LIRemote", "remote"),
        ("#LI-Hybrid", "hybrid"),
        ("#LIHybrid", "hybrid"),
        ("#LI-Onsite", "on site"),
        ("#LIOnsite", "on site"),
    ],
)
def test_a_linkedin_arrangement_label_outweighs_generic_hybrid_policy(
    inventory: CvInventory, tag: str, arrangement: str
) -> None:
    """A publisher arrangement label is more specific than an employer-wide policy."""
    posting = f"""
Principal Data Product Manager

{tag}

Where You'll Work
We have a hybrid work model with office hubs. Roles vary: some can be performed
from anywhere in the country, while others are based near a hub.
"""

    reading = reading_of(posting, inventory)

    assert reading.location.value == arrangement
    assert any(tag in quote for quote in reading.location.evidence)


def test_a_linkedin_arrangement_label_does_not_override_a_role_specific_conflict(
    inventory: CvInventory,
) -> None:
    reading = reading_of(
        """
Principal Data Product Manager

#LI-Onsite
This role is fully remote within Canada.
""",
        inventory,
    )

    assert reading.location.value is None
    assert reading.location.conflict is True
    assert "#LI-Onsite" in reading.location.because()
    assert "fully remote" in reading.location.because()


@pytest.mark.parametrize(
    ("tag", "sentence", "arrangement"),
    [
        (
            "#LI-Remote",
            "If this role is remote, you will attend in-office planning events twice a year.",
            "remote",
        ),
        (
            "#LI-Hybrid",
            "Planning runs quarterly. If the position is hybrid, you will work from home "
            "two days a week.",
            "hybrid",
        ),
    ],
)
def test_a_condition_on_the_roles_own_arrangement_states_no_second_arrangement(
    tag: str, sentence: str, arrangement: str, inventory: CvInventory
) -> None:
    posting = f"Principal Data Product Manager\n\n{tag}\n\n{sentence}"
    location = reading_of(posting, inventory).location
    assert location.value == arrangement
    assert location.conflict is False


@pytest.mark.parametrize(
    "expectations",
    [
        "Office expectations For remote roles: If this role is remote, there will be "
        "in-office events that will require travel to and from our Northtown office.",
        "Office expectations: if this role is remote, there will be in-office events.",
    ],
    ids=["run-in label", "mid-line condition"],
)
def test_a_labelled_condition_mid_line_states_no_second_arrangement(
    expectations: str, inventory: CvInventory
) -> None:
    posting = (
        f"Principal Data Product Manager\n\n#LI-Remote\n\nLocation: US - Remote. {expectations}"
    )
    location = reading_of(posting, inventory).location
    assert location.value == "remote"
    assert location.conflict is False


def test_a_plain_second_arrangement_for_this_role_beside_a_tag_still_conflicts(
    inventory: CvInventory,
) -> None:
    posting = (
        "Principal Data Product Manager\n\n#LI-Remote\n\n"
        "This role is hybrid with two days a week in the office."
    )
    location = reading_of(posting, inventory).location
    assert location.value is None
    assert location.conflict is True


_REMOTE_PLACES_WITH_TAG = """
Senior Data Product Manager

USA-IL-Remote, USA - Austin, TX, USA-TX-Remote, USA-MA-Remote

Full time

Own the reporting roadmap for the Initech ledger product.

{tag}
"""


@pytest.mark.parametrize("tag", ["#LI-Hybrid", "#LI-Onsite"])
def test_a_remote_place_beside_an_attendance_tag_is_a_conflict_not_a_refusal(
    inventory: CvInventory, tag: str
) -> None:
    refuses_attendance = JobSearchParameters(
        arrangement=Arrangement(hybrid=Acceptance.REFUSED, on_site=Acceptance.REFUSED)
    )

    reading = fit.read(
        _REMOTE_PLACES_WITH_TAG.format(tag=tag),
        inventory,
        parameters=refuses_attendance,
        today=TODAY,
    )

    assert reading.location.value is None
    assert reading.location.conflict is True
    assert tag in reading.location.because()
    assert "USA-IL-Remote" in reading.location.because()
    assert reading.arrangement_fit.value is None
    assert "refuse" not in reading.arrangement_fit.note


def _tagged_posting(title: str, line: str, tag: str) -> str:
    return (
        f"{title}\n\n{line}\n\nSet the data strategy for the Initech ledger product.\n\n"
        f"{tag} Note: Please be on the lookout for job scams."
    )


@pytest.mark.parametrize("tag", ["#LI-Hybrid", "#LI-Onsite"])
@pytest.mark.parametrize(
    ("title", "line"),
    [
        ("Vice President - Head of Data Strategy (Remote)", ""),
        ("Vice President - Head of Data Strategy - Remote", ""),
        ("Head of Data Strategy | Remote", ""),
        ("Head of Data Strategy, Remote", ""),
        ("Senior Data Engineer (Remote, US)", ""),
        ("Data Scientist - Remote, USA", ""),
        ("Analyst - Remote (US)", ""),
        ("Analyst (Remote - Canada)", ""),
        ("Analyst (Remote) - Boston", ""),
        ("Analyst - Remote.", ""),
        ("Head of Data Strategy", "Location: Remote"),
        ("Head of Data Strategy", "Work Type: Remote"),
    ],
)
def test_a_stated_remote_beside_a_boilerplate_attendance_tag_is_a_conflict_not_a_refusal(
    inventory: CvInventory, title: str, line: str, tag: str
) -> None:
    reading = fit.read(
        _tagged_posting(title, line, tag), inventory, parameters=_REFUSING_IN_PLACE, today=TODAY
    )

    assert reading.location.value is None
    assert reading.location.conflict is True
    assert tag in reading.location.because()
    assert fit.refusal(fit.gates(reading, _REFUSING_IN_PLACE)) is None


@pytest.mark.parametrize(
    ("title", "tag", "arrangement"),
    [
        ("Clinical Program Manager - Remote Patient Monitoring", "#LI-Onsite", "on site"),
        ("GIS Analyst - Remote Sensing", "#LI-Onsite", "on site"),
        ("Field Engineer | Remote Operations Center", "#LI-Onsite", "on site"),
        ("Head of Data - Remote, 2 days in office", "#LI-Hybrid", "hybrid"),
        ("Head of Data (Remote not available)", "#LI-Hybrid", "hybrid"),
        ("Head of Data (Not Remote)", "#LI-Hybrid", "hybrid"),
    ],
)
def test_a_title_naming_remote_work_but_no_remote_role_leaves_the_tag_standing(
    inventory: CvInventory, title: str, tag: str, arrangement: str
) -> None:
    reading = fit.read(
        _tagged_posting(title, "", tag), inventory, parameters=_REFUSING_IN_PLACE, today=TODAY
    )

    assert reading.location.value == arrangement
    assert reading.location.conflict is False


@pytest.mark.parametrize(
    "line", ["- Remote work stipend for your home office", "Our remote teams meet quarterly."]
)
def test_a_hybrid_tag_still_reads_beside_remote_named_in_prose(
    inventory: CvInventory, line: str
) -> None:
    posting = f"Head of Data Strategy\n\n{line}\n\n#LI-Hybrid"

    assert reading_of(posting, inventory).location.value == "hybrid"


def test_a_spaced_workday_place_list_beside_a_trailing_hybrid_tag_is_a_conflict(
    inventory: CvInventory,
) -> None:
    places = (
        "USA - Coppell, TX, USA-TX-Remote, USA - Salt Lake City, UT, USA-IL-Remote, "
        "Harbor Point Ave, United States of America, USA-MA-Remote"
    )
    body = " ".join(
        [
            "You will own the reporting roadmap for the Initech ledger product.",
            "You will partner with engineering, design, and finance on quarterly planning.",
            "You will set metrics, run discovery interviews with customers, and write specs.",
        ]
        * 4
    )
    posting = f"Senior Data Product Manager\n\n{places}\n\nFull time\n\n{body}\n\n#LI-Hybrid"

    reading = reading_of(posting, inventory)

    assert reading.location.value is None
    assert reading.location.conflict is True
    assert "USA-TX-Remote" in reading.location.because()


@pytest.mark.parametrize(
    "place",
    ["Remote - US", "Remote (US)", "US Remote", "Remote, Canada", "Location: USA-IL-Remote"],
)
def test_one_listed_remote_place_beside_an_attendance_tag_is_a_conflict(
    inventory: CvInventory, place: str
) -> None:
    posting = f"Senior Data Product Manager\n\n{place}\n\nOwn the roadmap.\n\n#LI-Hybrid"

    reading = reading_of(posting, inventory)

    assert reading.location.value is None
    assert reading.location.conflict is True


@pytest.mark.parametrize(
    "places",
    [
        "USA - Austin, TX, USA - Chicago, IL",
        "Not remote, Austin, TX",
        "We support remote teams across the company",
        "Hybrid: 3 days in office, 2 remote",
        "Remote work stipend",
        "Remote-friendly culture",
        "Occasional remote",
        "Hybrid, not fully remote",
        "Not fully remote",
        "Partially remote",
    ],
)
def test_an_attendance_tag_holds_when_no_listed_place_is_remote(
    inventory: CvInventory, places: str
) -> None:
    posting = (
        f"Senior Data Product Manager\n\nChicago, IL\n\n{places}\n\nOwn the roadmap.\n\n#LI-Hybrid"
    )

    assert reading_of(posting, inventory).location.value == "hybrid"


def test_a_remote_tag_beside_remote_places_stays_remote(inventory: CvInventory) -> None:
    reading = reading_of(_REMOTE_PLACES_WITH_TAG.format(tag="#LI-Remote"), inventory)

    assert reading.location.value == "remote"
    assert reading.location.conflict is False


def test_conflicting_linkedin_arrangement_labels_abstain(inventory: CvInventory) -> None:
    reading = reading_of(
        """
Principal Data Product Manager

#LI-Hybrid
#LI-Onsite
""",
        inventory,
    )

    assert reading.location.value is None
    assert reading.location.conflict is True
    assert "#LI-Hybrid" in reading.location.because()
    assert "#LI-Onsite" in reading.location.because()


@pytest.mark.parametrize(
    "tag", ["#LI-Remote-first", "#LI-Remotely", "#LI-Hybridized", "#LI-Onsite-only"]
)
def test_a_linkedin_tag_lookalike_does_not_state_an_arrangement(
    inventory: CvInventory, tag: str
) -> None:
    reading = reading_of(f"Principal Data Product Manager\n\n{tag}", inventory)

    assert reading.location.value is None
    assert reading.location.conflict is False


@pytest.mark.parametrize(
    ("tag", "arrangement"),
    [("#LI-Remote", "remote"), ("#LI-Hybrid", "hybrid"), ("#LI-Onsite", "on site")],
)
def test_a_linkedin_tag_glued_to_the_next_block_is_still_read(
    tag: str, arrangement: str, inventory: CvInventory
) -> None:
    body = plain_layout(f"Apply by Friday.\n{tag}The data team at Northwind Rail plans trains.")
    assert reading_of("Data Engineer\n\n" + body, inventory).location.value == arrangement


def test_an_onsite_interview_does_not_set_the_working_arrangement(
    inventory: CvInventory,
) -> None:
    """An onsite mention confined to the interview is hiring logistics, not the arrangement."""
    assert reading_of(ONSITE_INTERVIEW_ONLY, inventory).location.value is None
    assert reading_of(ONSITE_ROLE, inventory).location.value == "on site"


def test_an_interview_heading_ends_the_text_the_arrangement_is_read_from(
    inventory: CvInventory,
) -> None:
    posting = (
        "Data Engineer\n\nResponsibilities\n- Build the reporting pipelines\n"
        "- Review data quality each week\n\nOur Interview Practices\n"
        "Final rounds include an in-person conversation at our office."
    )
    assert reading_of(posting, inventory).location.value is None


def test_a_location_quote_on_a_long_line_shows_a_hyphenated_phrase(
    inventory: CvInventory,
) -> None:
    posting = (
        "Data Engineer\n\nAbout the team\nNorthwind plans regional rail timetables with depots, "
        "planners and operators across the network, and the analytics group builds the models "
        "and reports those teams rely on every week, in a collaborative and remote-first culture."
    )
    location = reading_of(posting, inventory).location
    assert location.value == "remote"
    assert "remote-first" in location.evidence[0]


@pytest.mark.parametrize(
    ("sentence", "expected"),
    [
        (
            "Northwind provides medical and dental cover, along with other comprehensive "
            "wellness offerings, including on-site wellness rounding.",
            None,
        ),
        (
            "What We Are Offering: Hybrid working conditions with capacity to work from office "
            "or home.",
            "hybrid",
        ),
    ],
)
def test_arrangement_words_among_offerings_are_no_location(
    sentence: str, expected: str | None, inventory: CvInventory
) -> None:
    posting = f"Data Engineer\n\nAbout the team\nWe plan regional rail timetables.\n\n{sentence}"
    assert reading_of(posting, inventory).location.value == expected


def test_a_posting_that_states_its_market_is_read_and_quoted(inventory: CvInventory) -> None:
    """The confident case, with the evidence for every part of it."""
    reading = reading_of(DATA_ARCHITECT, inventory)
    assert reading.role_family.value == "Data architecture and governance"
    assert reading.specialty.value == "Master and reference data"
    assert reading.seniority.value == "senior"
    assert reading.years.value == 8
    assert reading.education.value == "bachelor's degree, or equivalent experience"
    assert reading.location.value == "hybrid"
    assert reading.authorization.value == "sponsorship is not offered"
    assert reading.sections.value == "Requirements, Preferred qualifications"
    assert reading.must_have.value == 3
    assert reading.preferred.value == 2
    # Every stated answer names what it was read from, so a reader can disagree
    # with the rule rather than with a verdict.
    assert all(answer.evidence for answer in reading.answers() if answer.stated)
    assert "title says 'data architect'" in reading.role_family.evidence


def test_the_taxonomy_reads_markets_that_are_not_software(inventory: CvInventory) -> None:
    """A nurse, a designer, and a financial analyst, read by the same table."""
    assert reading_of(NURSE, inventory).role_family.value == "Nursing"
    assert reading_of(DESIGNER, inventory).role_family.value == "Design and user experience"
    assert reading_of(FINANCIAL_ANALYST, inventory).role_family.value == "Finance and accounting"
    assert reading_of(BACKEND_ENGINEER, inventory).role_family.value == "Software engineering"


def test_the_taxonomy_reads_medical_information_as_its_own_market(
    inventory: CvInventory,
) -> None:
    posting = """Senior Manager, Medical Information

Develop balanced medical information responses for unsolicited medical inquiries.
Lead medical literature searches and maintain the medical response database.
Use inquiry trend analysis to govern reusable medical content.
"""

    reading = reading_of(posting, inventory)

    assert reading.role_family.value == "Medical and drug information"
    assert reading.specialty.value == "Medical information content and operations"


def test_business_analysis_is_sector_neutral_and_domain_evidence_is_a_specialty(
    inventory: CvInventory,
) -> None:
    posting = """Clinical Business Analyst

Translate clinical systems requirements into functional specifications.
Map laboratory workflows for a laboratory information management system.
"""

    reading = reading_of(posting, inventory)

    assert reading.role_family.value == "Business and process analysis"
    assert reading.specialty.value == "Clinical and scientific systems analysis"


def test_a_stated_people_responsibility_is_read_as_management(inventory: CvInventory) -> None:
    """The other side of the management control: a report, stated plainly."""
    reading = reading_of(MANAGES_PEOPLE, inventory)
    assert reading.orientation.value == "management"
    assert "manage a team of four stewards" in reading.orientation.evidence[0]


def test_a_stated_individual_contributor_role_outranks_its_title(
    inventory: CvInventory,
) -> None:
    """A stated individual contributor role outranks a manager's title with no direct reports."""
    reading = reading_of(TITLED_MANAGER_IS_IC, inventory)
    assert reading.orientation.value == "individual contributor"
    assert "no direct reports" in reading.orientation.evidence[0]


def test_a_line_may_overrule_the_heading_it_sits_under(inventory: CvInventory) -> None:
    """A line saying "a plus" under Requirements is preferred, and the reason says so."""
    reading = reading_of(DATA_ARCHITECT, inventory)
    preferred = [line for line in reading.requirements if line.stance is fit.Stance.PREFERRED]
    assert any("a plus" in line.reason for line in preferred)
    assert any("Preferred qualifications" in line.reason for line in preferred)


def test_sponsorship_and_remote_work_are_read_when_stated(inventory: CvInventory) -> None:
    """Both answers a person triages on, from the sentences that state them."""
    reading = reading_of(SPONSORED_REMOTE, inventory)
    assert reading.location.value == "remote"
    assert reading.authorization.value == "sponsorship is offered"
    assert reading.years.value == 9


def test_the_record_is_read_by_the_same_rules_as_the_posting(inventory: CvInventory) -> None:
    """What the CV's own language evidences, reported beside the posting's."""
    reading = reading_of(DATA_ARCHITECT, inventory)
    assert reading.record_families.stated
    assert reading.record_years.value == 2026 - 2014
    assert "earliest dated role starts 2014, latest ends 2026" in reading.record_years.evidence[0]


TWO_ROLES = """# PAT LEE

Toronto | pat@example.com

## PROFESSIONAL EXPERIENCE

### Northwind

**Product Manager**
2018 - 2021 | Toronto

- Owned the roadmap

### Riverbend Cafe

**Barista**
2015 - 2018

- Made coffee
"""


def test_the_record_readings_name_the_role_lines_they_counted(tmp_path: Path) -> None:
    """Years cite every dated role's dates; families cite only roles that carried one."""
    path = tmp_path / "CV.md"
    path.write_text(TWO_ROLES, encoding="utf-8")
    reading = reading_of(DATA_ARCHITECT, parse_cv(path))

    assert reading.record_years.value == 2021 - 2015
    assert [(line.role_key, line.field, line.text) for line in reading.record_years.lines] == [
        ("northwind", "dates", "2018 - 2021"),
        ("riverbend-cafe", "dates", "2015 - 2018"),
    ]
    assert reading.record_families.value == "Product management"
    assert [(line.role_key, line.field, line.text) for line in reading.record_families.lines] == [
        ("northwind", "title", "Product Manager"),
    ]


def test_a_record_with_no_dates_is_refused_rather_than_guessed(tmp_path: Path) -> None:
    """A CV whose roles carry no years cannot have a span, and does not get one."""
    path = tmp_path / "CV.md"
    path.write_text(NO_DATES, encoding="utf-8")
    reading = reading_of(DATA_ARCHITECT, parse_cv(path))
    assert reading.record_years.value is None
    assert "carries a date" in reading.record_years.note
    assert reading.record_families.value is None


def test_an_abstention_renders_as_words_rather_than_as_nothing(inventory: CvInventory) -> None:
    """Every answer prints. "not stated" is the finding, so it has to be visible."""
    reading = reading_of(BOILERPLATE_ONLY, inventory)
    # Every question about the posting, and only the two about the master CV are
    # answered, because the record is a real one and the posting says nothing.
    about_the_record = {"What the record reads as", "Years the record covers"}
    assert reading.abstentions == len(reading.answers()) - len(about_the_record)
    assert all(
        answer.render() == "not stated"
        for answer in reading.answers()
        if answer.question not in about_the_record
    )
    # Whether it answered or declined, it says what it read or what it looked for.
    assert all(answer.because() for answer in reading.answers())


def test_a_title_may_be_labelled_or_a_markdown_heading(inventory: CvInventory) -> None:
    """Postings arrive as Markdown, as plain text, and with the title in a field."""
    for posting in (
        "# Registered Nurse, Medical-Surgical Unit\n\nRequirements:\n- Active licensure\n",
        "Job title: Registered Nurse\n\nRequirements:\n- Active licensure\n",
    ):
        assert fit.read(posting, inventory, today=TODAY).role_family.value == "Nursing"


def test_a_house_acronym_counts_once_it_is_declared(inventory: CvInventory) -> None:
    """An alias is how somebody's market gets read without a pull request."""
    posting = (
        "Data Steward, Northwind\n\n"
        "Requirements:\n"
        "- Run MDM for customer and product across the estate\n"
        "- Keep the golden record current with each data domain ownership group\n"
    )
    without = reading_of(posting, inventory)
    assert without.specialty.value is None

    vocabulary = Vocabulary(aliases={"master data management": ["MDM"]})
    with_alias = fit.read(posting, inventory, vocabulary=vocabulary, today=TODAY)
    assert with_alias.specialty.value == "Master and reference data"


def test_an_added_phrase_extends_a_family_without_replacing_it() -> None:
    """Additions are appended. Nothing in settings can subtract vocabulary."""
    lexicon = taxonomy.extended(Vocabulary(families={"finance": ["burn rate"]}))
    finance = lexicon.family("finance")
    assert finance is not None
    assert "burn rate" in finance.terms
    assert "month end close" in finance.terms


def test_a_settings_key_that_names_nothing_is_refused_with_the_list() -> None:
    """A typo in a settings file must not silently do nothing for a year."""
    with pytest.raises(ValidationError, match="unknown family key"):
        Vocabulary(families={"data_architcture": ["canonical model"]})
    with pytest.raises(ValidationError, match="unknown specialty key"):
        Vocabulary(specialties={"master_data": ["golden record"]})


def test_the_shipped_taxonomy_is_returned_unchanged_when_nothing_is_declared() -> None:
    """The common path allocates nothing and is the same table every run."""
    assert taxonomy.extended(None).families is taxonomy.FAMILIES
    assert taxonomy.extended(Vocabulary()).families is taxonomy.FAMILIES


def test_a_phrase_is_matched_whole_and_not_inside_another_word() -> None:
    """The word "data" must not match "metadata", which is how a table like this rots."""
    lexicon = taxonomy.Taxonomy()
    assert lexicon.mentions("data pipeline", "we run a Data-Pipeline nightly")
    assert not lexicon.mentions("data", "metadata management")
    assert lexicon.present(("code review", "unit tests"), "we do code review") == ("code review",)


def test_a_requirement_the_posting_calls_optional_is_reported_as_a_disagreement(
    inventory: CvInventory,
) -> None:
    """The model called it essential; the posting lists it under "Preferred"."""
    reading = reading_of(DATA_ARCHITECT, inventory)
    spec = spec_for(
        requirements=(
            ("r01", "Experience with an interoperability standard", Importance.MUST_HAVE),
        ),
        gates=(("degree", "Bachelor's degree or equivalent experience"),),
    )
    found = fit.disagreements(reading, spec)
    assert [item.subject for item in found] == ["Importance of r01"]
    assert found[0].model_says == "must-have"
    assert found[0].rules_say == "preferred"
    assert "Preferred qualifications" in found[0].detail


PRICING_MODELS = (
    "Experience with or exposure to widget-based pricing models (e.g., cost-plus, tiered)"
)
PRICING_ANALYTICS = (
    "Strong experience in widget analytics, with a preference for widget-based pricing domains"
)


def _quoting(posting: str, quote: str = PRICING_MODELS) -> JobSpec:
    """A must-have reworded by the model, carrying its verified quote of a posting line."""
    return JobSpec(
        title="Pricing Analyst",
        company="Northwind",
        seniority=None,
        location=None,
        requirements=(
            Requirement(
                id="r01",
                text="Experience with widget-based pricing",
                importance=Importance.MUST_HAVE,
                quote_or_span=quote,
                source_span=locate(quote, posting),
            ),
        ),
        keywords=(),
        hard_gates=(),
        origin="test",
    )


def test_a_verified_requirement_is_compared_with_the_line_it_quotes(
    inventory: CvInventory,
) -> None:
    """A neighbouring line that shares more of the model's rewording is not its line."""
    posting = (
        "Pricing Analyst, Northwind\n\n"
        f"Responsibilities:\n- {PRICING_MODELS}\n\n"
        f"Qualifications:\n- {PRICING_ANALYTICS}\n"
    )
    spec = _quoting(posting)
    assert spec.requirements[0].verified
    assert fit.disagreements(reading_of(posting, inventory), spec) == ()


@pytest.mark.parametrize(
    "quote",
    [
        PRICING_MODELS,
        PRICING_MODELS.replace("widget-based", "the team\u2019s widget-based"),
        PRICING_MODELS.replace("cost-plus", "\u201ccost-plus\u201d"),
        PRICING_MODELS.replace("cost-plus, tiered", "cost-plus &amp; tiered"),
    ],
    ids=["plain", "curly-apostrophe", "curly-quotes", "html-entity"],
)
def test_a_verified_requirement_its_quoted_line_grades_differently_is_reported(
    inventory: CvInventory, quote: str
) -> None:
    """The quote pairs the requirement with its own line, and that line can still disagree."""
    posting = (
        "Pricing Analyst, Northwind\n\n"
        f"Requirements:\n- {PRICING_ANALYTICS}\n\n"
        f"Nice to have:\n- {quote}\n"
    )
    spec = _quoting(posting, quote)
    assert spec.requirements[0].verified
    found = fit.disagreements(reading_of(posting, inventory), spec)
    assert [item.subject for item in found] == ["Importance of r01"]
    assert found[0].rules_say == "preferred"
    assert found[0].evidence == (sanitize(quote),)


def test_a_seniority_the_title_contradicts_is_reported_as_a_disagreement(
    inventory: CvInventory,
) -> None:
    """The model read a staff role out of a posting whose title says senior."""
    reading = reading_of(DATA_ARCHITECT, inventory)
    found = fit.disagreements(
        reading, spec_for(seniority="staff", gates=(("degree", "Bachelor's degree or equivalent"),))
    )
    assert [item.subject for item in found] == ["Seniority"]
    assert found[0].rules_say == "senior"


def test_an_experience_floor_and_a_working_arrangement_are_compared(
    inventory: CvInventory,
) -> None:
    """Two more places the two readings can differ, each from the text itself."""
    reading = reading_of(DATA_ARCHITECT, inventory)
    found = fit.disagreements(
        reading,
        spec_for(location="fully remote", gates=(("minimum_years", "5 years of experience"),)),
    )
    subjects = [item.subject for item in found]
    assert "Where the work happens" in subjects
    assert "Years of experience required" in subjects


def test_a_degree_gate_the_text_does_not_carry_is_reported(inventory: CvInventory) -> None:
    """A degree requirement nobody wrote down is the expensive kind of mistake."""
    reading = reading_of(SALES_PIPELINE, inventory)
    found = fit.disagreements(
        reading, spec_for(gates=(("degree", "Bachelor's degree in business required"),))
    )
    assert [item.subject for item in found] == ["Education required"]
    assert found[0].rules_say == "not stated"


def test_an_unstated_degree_row_names_its_gate_by_position_among_all_gates(
    inventory: CvInventory,
) -> None:
    reading = reading_of(SALES_PIPELINE, inventory)
    spec = spec_for(
        gates=(("minimum_years", "3 years of experience"), ("degree", "Bachelor's degree required"))
    )

    [row] = [item for item in fit.disagreements(reading, spec) if item.gate is not None]

    assert (row.subject, row.gate, row.evidence) == ("Education required", 1, ())


def test_every_reading_answer_is_a_name_a_disagreement_may_cite(inventory: CvInventory) -> None:
    reading = reading_of(SALES_PIPELINE, inventory)

    assert set(get_args(AnswerName)) == {name for name, _ in reading.named()}


def test_a_degree_kind_gate_naming_no_degree_is_still_a_degree_gate(
    inventory: CvInventory,
) -> None:
    reading = reading_of(SALES_PIPELINE, inventory)
    found = fit.disagreements(
        reading, spec_for(gates=(("degree", "A relevant tertiary qualification is required"),))
    )
    assert [item.subject for item in found] == ["Education required"]


def test_a_degree_the_model_missed_is_reported_the_other_way(inventory: CvInventory) -> None:
    """The comparison runs in both directions, because either side can be wrong."""
    reading = reading_of(DATA_ARCHITECT, inventory)
    found = fit.disagreements(reading, spec_for())
    assert [item.subject for item in found] == ["Education required"]
    assert found[0].model_says == "no degree gate was extracted"


def test_two_abstentions_are_not_a_disagreement(inventory: CvInventory) -> None:
    """Silence on both sides agrees. Only an answered question can conflict."""
    reading = reading_of(AMBIGUOUS, inventory)
    assert fit.disagreements(reading, spec_for(seniority="senior", location="remote")) == ()


def test_agreement_is_reported_as_agreement(inventory: CvInventory) -> None:
    """The model and the rules reading the same posting the same way is the normal case."""
    reading = reading_of(DATA_ARCHITECT, inventory)
    spec = spec_for(
        seniority="senior",
        location="hybrid, three days per week in the office",
        requirements=(("r01", "8+ years of experience", Importance.MUST_HAVE),),
        gates=(
            ("degree", "Bachelor's degree or equivalent experience"),
            ("minimum_years", "8+ years of experience in data architecture"),
        ),
    )
    assert fit.disagreements(reading, spec) == ()


def test_a_responsibility_is_not_compared_as_a_requirement(inventory: CvInventory) -> None:
    """A responsibility has no stance to disagree with, so it is left alone."""
    reading = reading_of(DATA_ARCHITECT, inventory)
    spec = spec_for(
        requirements=(("r01", "Public speaking is a plus", Importance.RESPONSIBILITY),),
        gates=(("degree", "Bachelor's degree or equivalent"),),
    )
    assert fit.disagreements(reading, spec) == ()


def test_a_posting_the_rules_read_two_ways_argues_with_a_confident_model(
    inventory: CvInventory,
) -> None:
    """A refusal for cause is worth reporting against a model that answered anyway.

    Declining to place a level because a posting names none says nothing about
    the model. Declining because the title and the responsibilities describe
    different jobs is a finding, and a model reporting that field confidently is
    then worth arguing with.
    """
    inflated = reading_of(INFLATED_TITLE, inventory)
    assert inflated.seniority.conflict
    found = fit.disagreements(inflated, spec_for(seniority="director"))
    assert [item.subject for item in found] == ["Seniority"]
    assert found[0].rules_say == "not stated"

    # The other refusal, the one that means nothing, stays silent.
    quiet = reading_of(AMBIGUOUS, inventory)
    assert not quiet.seniority.conflict
    assert fit.disagreements(quiet, spec_for(seniority="director")) == ()


def test_an_empty_posting_produces_answers_rather_than_an_exception(
    inventory: CvInventory,
) -> None:
    """An empty posting answers "not stated" everywhere rather than raising."""
    reading = fit.read("", inventory, today=TODAY)
    assert reading.role_family.value is None
    assert reading.requirements == ()


def test_a_bare_line_over_a_list_is_read_as_its_heading(inventory: CvInventory) -> None:
    """Postings punctuate headings three ways, and one of them is not at all."""
    posting = (
        "Registered Nurse\n\nRequirements\n\n- Must hold active licensure\n"
        "- Bachelor of Science in Nursing is required\n"
    )
    reading = fit.read(posting, inventory, today=TODAY)
    assert reading.sections.value == "Requirements"
    assert reading.education.value == "bachelor's degree, stated as must-have"


def test_boilerplate_inside_a_requirement_block_is_still_boilerplate(
    inventory: CvInventory,
) -> None:
    """The legal sentence somebody pasted into the qualifications list."""
    posting = (
        "Data Engineer\n\nRequirements:\n"
        "- 5 years of experience with a data pipeline\n"
        "- All qualified applicants will receive consideration without regard to any "
        "protected status\n"
    )
    reading = fit.read(posting, inventory, today=TODAY)
    assert reading.must_have.value == 1
    assert reading.requirements[0].text.startswith("5 years")


def test_a_degree_named_outside_the_requirements_is_reported_as_such(
    inventory: CvInventory,
) -> None:
    """A degree in the introduction is a fact about the posting, stated carefully."""
    posting = (
        "Laboratory Manager\n\n"
        "Our team is led by a PhD chemist and works to a published bench protocol.\n\n"
        "Responsibilities:\n- Run assay development and instrument calibration\n"
    )
    reading = fit.read(posting, inventory, today=TODAY)
    assert reading.education.value == "doctorate, named outside any requirement section"


def test_the_report_says_what_the_section_is_and_prints_every_abstention(
    inventory: CvInventory,
) -> None:
    """Where it reaches a person, it has to say what it is and is not.

    A section a reader could mistake for a model's judgment, or for a score,
    would be worse than no section.
    """
    reading = reading_of(BOILERPLATE_ONLY, inventory)
    section = report_section(rendered_report(reading=reading, spec=spec_for()), RULES_SECTION)
    assert "Derived from the posting text by rules, not by a model" in section
    assert "not a score" in section
    assert "not a prediction" in section
    assert section.count("not stated") >= reading.abstentions
    assert "They agree on everything both of them answered" in section

    disagreeing = report_section(
        rendered_report(
            reading=reading_of(DATA_ARCHITECT, inventory), spec=spec_for(seniority="staff")
        ),
        RULES_SECTION,
    )
    assert "Where the model and the rules disagree" in disagreeing
    assert "does not\nknow which" in disagreeing


def _must(reading: fit.Reading) -> list[str]:
    return [line.text for line in reading.requirements if line.stance is fit.Stance.MUST_HAVE]


PROSE_REQUIREMENTS = """Data Platform Owner
Remote - Canada
Full time

We are growing fast and need a strong owner. You must have eight years of \
experience building streaming systems. A bachelor's degree in a technical field \
is required. Experience with schema governance is preferred.
"""

# The heading carries the U+2019 curly apostrophe boards write, built by code
# point so the source stays ASCII.
CURLY_APOSTROPHE_HEADING = (
    "Senior Data Engineer\nRemote - United States\n\n"
    "WHAT YOU" + chr(0x2019) + "LL NEED:\n"
    "- Five years of experience with distributed data systems\n"
    "- A bachelor's degree in a technical field\n"
)


def test_marker_bearing_prose_sentences_each_become_a_requirement(
    inventory: CvInventory,
) -> None:
    """A headingless prose paragraph yields its stated requirements, one per sentence."""
    reading = reading_of(PROSE_REQUIREMENTS, inventory)
    musts = _must(reading)
    # The two "must have" / "is required" sentences are separated, not one blob.
    assert len(musts) >= 2
    assert any("eight years" in text for text in musts)
    assert any("bachelor" in text.casefold() for text in musts)
    # The preferred sentence is not swept into a must-have.
    assert not any("schema governance" in text for text in musts)


def test_a_requirement_heading_written_with_a_curly_apostrophe_is_recognised(
    inventory: CvInventory,
) -> None:
    reading = reading_of(CURLY_APOSTROPHE_HEADING, inventory)
    assert len(_must(reading)) == 2


# Headings as bold paragraphs or h3, items wrapped in paragraphs, as Workday and Ashby send.
BOARD_MARKUP = (
    "<p><strong>About the role</strong></p><p>Lead the widget science team.</p>"
    "<h3>Qualifications</h3><ul><li><p>5+ years of experience with widget assays</p></li>"
    "<li><p>A degree in chemistry</p></li></ul>"
    "<p><strong>Nice to have</strong></p><ul><li><p>Experience with gadgets</p></li></ul>"
)


def test_a_board_description_reaches_the_reader_with_its_requirement_sections(
    inventory: CvInventory,
) -> None:
    posting = "Widget Scientist\nSpringfield\n\n" + plain_description(BOARD_MARKUP)
    reading = reading_of(posting, inventory)
    assert reading.sections.value == "Qualifications, Nice to have"
    assert reading.must_have.value == 2
    assert reading.preferred.value == 1
    assert reading.years.value == 5
    assert reading.years.evidence == ("5+ years of experience with widget assays",)


REPORTING_LINE = """Analytics Engineer
Remote - United States

You will build and own data pipelines. This role reports to the Vice President \
of Data Platform.

Requirements:
- Five years of experience with SQL
"""

LEAD_AS_A_VERB = """Product Analyst
Remote - Canada

In this role you will lead discussions and lead projects across many teams.

Requirements:
- Three years of experience with dashboards
"""


def test_a_reporting_line_does_not_stamp_the_role_at_the_managers_level(
    inventory: CvInventory,
) -> None:
    reading = reading_of(REPORTING_LINE, inventory)
    assert reading.seniority.value is None


@pytest.mark.parametrize(
    "sentence",
    [
        "In this role you will own renewals, selling to SVP or C-level stakeholders.",
        "This role presents quarterly results to VP-level audiences.",
        "The position builds relationships with VP and C-suite executives at each account.",
        "In this role you will engage executive sponsors and senior decision-makers.",
        "This role reports directly to the Vice President of Operations.",
        "The position is reporting directly into the Chief Operating Officer.",
        "In this role you will be supporting the Vice President of Sales.",
        "This role partners with the SVP of Procurement on every contract.",
        "The position advises director-level leaders across the business.",
        "This role focuses on developing senior\u2011level customer relationships.",
        "This role will report to the Senior Manager of Regional Finance.",
    ],
)
def test_a_counterpartys_rank_does_not_stamp_the_role_level(
    sentence: str, inventory: CvInventory
) -> None:
    posting = f"Account Manager\nRemote - Canada\n\n{sentence}\n\nRequirements:\n- CRM hygiene"
    assert reading_of(posting, inventory).seniority.value is None


@pytest.mark.parametrize(
    ("sentence", "level"),
    [
        ("This is a VP-level role leading our analytics group.", "executive"),
        ("This senior role partners with VP-level stakeholders.", "senior"),
        ("The position is a director-level role based in our Denver hub.", "director"),
        ("In this Senior Director role you will present to the CEO.", "director"),
        (
            "This is a senior sales role, reporting directly to the Regional Sales Manager.",
            "senior",
        ),
        ("Reporting to the SVP of Data, this Director-level role owns the roadmap.", "director"),
    ],
)
def test_a_level_the_body_places_on_the_role_is_read(
    sentence: str, level: str, inventory: CvInventory
) -> None:
    posting = f"Account Manager\nRemote - Canada\n\n{sentence}\n\nRequirements:\n- CRM hygiene"
    assert reading_of(posting, inventory).seniority.value == level


def test_the_verb_lead_in_the_body_is_not_read_as_the_lead_level(inventory: CvInventory) -> None:
    reading = reading_of(LEAD_AS_A_VERB, inventory)
    assert reading.seniority.value is None


def test_a_family_named_in_the_title_breaks_a_close_body_tie(inventory: CvInventory) -> None:
    """The title names product management and the body scores data engineering the same."""
    posting = (
        "Product Manager, Data Platform\n\n"
        "You will own our data pipeline, data warehouse, and data lake day to day."
    )
    reading = reading_of(posting, inventory)
    assert reading.role_family.value == "Product management"
    assert reading.role_family.evidence == ("title says 'product manager'",)


def test_two_families_both_named_in_the_title_still_abstain(inventory: CvInventory) -> None:
    """The tiebreak fires only when exactly one contender is named in the title."""
    posting = (
        "Product Manager and Data Engineer\n\n"
        "You will own the product roadmap, data pipeline, and data warehouse."
    )
    reading = reading_of(posting, inventory)
    assert reading.role_family.value is None
    assert "two ways" in reading.role_family.note


def test_a_company_blurb_first_line_is_not_the_title(inventory: CvInventory) -> None:
    posting = (
        "At Contoso Health, we believe health is everything and our mission is to "
        "build a better world.\nSenior Data Engineer\nRemote - United States\n"
    )
    reading = reading_of(posting, inventory)
    assert "title says 'data engineer'" in reading.role_family.evidence
    assert reading.seniority.value == "senior"


def test_a_long_pipe_delimited_title_is_still_read_as_the_title(inventory: CvInventory) -> None:
    """The blurb guard keys on company-statement phrasing, not on length."""
    posting = "Lead Product Software Engineer | Backend | Node.js | Azure | SQL | AI\nRemote\n"
    assert reading_of(posting, inventory).seniority.value == "lead"


@pytest.mark.parametrize(
    "title",
    [
        "SVP, Product Management - Data Products",
        "EVP Product",
        "VP, Data Strategy",
        "Vice President, Clinical Data",
    ],
)
def test_a_vice_president_title_reads_the_executive_level(
    title: str, inventory: CvInventory
) -> None:
    posting = f"{title}\nRemote - United States\n\nYou will set the data product strategy.\n"
    assert reading_of(posting, inventory).seniority.value == "executive"


@pytest.mark.parametrize(("source", "body"), BODY_SHAPES)
def test_every_body_construction_reads_its_requirement_sections(
    source: str, body: str, inventory: CvInventory
) -> None:
    reading = reading_of("Data Engineer\n\n" + canonical_body(source, body), inventory)
    assert reading.sections.value == f"{REQUIRED_HEADING}, {PREFERRED_HEADING}"
    assert reading.must_have.value == 2
    assert reading.preferred.value == 2
    assert reading.years.value == 5


# Prose that a list heuristic could mistake for items: each must reach the reader
# with no item line and no stated requirement.
PROSE_BODIES = [
    pytest.param(
        "markup",
        "<p><b>About the role</b></p><p>Northwind Rail runs the timetable. Every depot must "
        "be ready by Friday.</p><p>We keep the planner honest. You will own the "
        "release notes and the depot calendar</p>",
        id="about the role, narrative paragraphs",
    ),
    pytest.param(
        "markup",
        "<p><strong>About us</strong></p><p>Northwind Rail was founded by depot planners.</p>"
        "<p>We still answer the phone ourselves.</p><p>Every depot must be ready on Friday.</p>",
        id="about us, one sentence per paragraph",
    ),
    pytest.param(
        "text",
        "About the role\n\nNorthwind Rail runs the timetable that eleven depots\n"
        "plan against, and this team keeps it honest.\nEvery depot must be ready by\n"
        "Friday, and the planners check it the same day.",
        id="about the role, wrapped text",
    ),
    pytest.param(
        "text",
        "3 depots moved to the new planner last spring.\n"
        "12 more follow this year once the pilot ends.",
        id="sentences opening with a number",
    ),
    pytest.param(
        "text",
        "Well-being budgets are set each year.\nSelf-directed learning is encouraged here.\n"
        "-20 degree mornings are part of winter operations.",
        id="hyphenated words at line start",
    ),
    pytest.param(
        "text",
        "Drivers call the loop line the o route,\no for the shape it makes on the map.",
        id="the letter o as a word",
    ),
    pytest.param(
        "text",
        "*Pay depends on the depot you join.\nThe depots are all near a station.",
        id="one footnote asterisk",
    ),
    pytest.param(
        "text",
        "Location: Toronto, ON · Hybrid · Full-time\nNorthwind Rail plans trains.",
        id="middle dot metadata line",
    ),
]


@pytest.mark.parametrize(("source", "body"), PROSE_BODIES)
def test_prose_stays_prose(source: str, body: str, inventory: CvInventory) -> None:
    canonical = canonical_body(source, body)
    reading = reading_of("Data Engineer\n\n" + canonical, inventory)
    assert not [line for line in canonical.splitlines() if ITEM_MARKER.match(line)]
    assert not reading.must_have.stated
    assert not reading.preferred.stated


# A pay line, a heading glued to its first item, a stance line, and the closing
# headings that end an unmarked list.
BARE_HEADING_BODIES = [
    pytest.param(
        "Requirements:\n- Five years of rail planning experience\n\n"
        "$120,000 to $140,000 base + bonus\n\nBase + bonus\nPaid every two weeks.",
        ("Requirements", 1, None),
        id="pay line",
    ),
    pytest.param(
        "Requirements Five years of rail planning experience\nFluency in SQL",
        (None, None, None),
        id="heading glued to its first line",
    ),
    pytest.param(
        "Responsibilities Lead the data platform roadmap\nMust hold a current rail safety card",
        (None, None, None),
        id="responsibility heading glued to its first line",
    ),
    pytest.param(
        "Requirements\n\nFive years of rail planning experience\n\nDegree preferred\n\n"
        "Fluency in SQL",
        ("Requirements", 2, 1),
        id="stance line under a bare heading",
    ),
    pytest.param(
        "Requirements\nFive years of rail planning experience\nFluency in SQL\n\n"
        "Benefits\nDental cover from day one\nA rail pass for the whole family\n\n"
        "About us\nNorthwind Rail has run trains since the canal closed\n\n"
        "Equal opportunity\nWe welcome applicants of every background",
        ("Requirements", 2, None),
        id="closing headings end an unmarked list",
    ),
    pytest.param(
        "You must have five years of rail planning experience.Your degree in logistics is "
        "required.Experience with SQL is preferred.",
        (None, 2, 1),
        id="sentences glued together",
    ),
    pytest.param(
        "About the role\nNorthwind Rail plans trains.\n\nRequirements Gathering Workshops\n"
        "Run them weekly with depots and planners.",
        (None, None, None),
        id="a heading word opening a phrase",
    ),
    pytest.param(
        "Key Requirements and Responsibilities\nFluency in SQL\n"
        "Five years of rail planning experience",
        ("Key Requirements and Responsibilities", 2, None),
        id="a qualified heading that joins two kinds",
    ),
    pytest.param(
        "About the role\nNorthwind Rail plans trains.\n\nRequirement Synthesis\n"
        "Run it weekly with depots and planners.",
        (None, None, None),
        id="a heading word followed by an activity",
    ),
    *(
        pytest.param(
            f"{heading}\nFluency in SQL\nFive years of rail planning experience",
            expected,
            id=f"items under {heading!r}",
        )
        for heading, expected in (
            ("What you bring to the table", ("What you bring to the table", 2, None)),
            ("Bonus if you have", ("Bonus if you have", None, 2)),
            ("Bonus Points", ("Bonus Points", None, 2)),
            ("Preferred KSA's", ("Preferred KSA's", None, 2)),
            ("Preferred Experience", ("Preferred Experience", None, 2)),
            ("Required", ("Required", 2, None)),
            ("Required Skills", ("Required Skills", 2, None)),
            ("Core Competencies", ("Core Competencies", 2, None)),
            ("Knowledge, Skills & Abilities", ("Knowledge, Skills & Abilities", 2, None)),
            (
                "Minimum Knowledge, Skills And Abilities",
                ("Minimum Knowledge, Skills And Abilities", 2, None),
            ),
            ("Desired Characteristics", ("Desired Characteristics", None, 2)),
            ("Qualities", ("Qualities", 2, None)),
            ("Personal Qualities", ("Personal Qualities", 2, None)),
            ("Essential Skills & Experience", ("Essential Skills & Experience", 2, None)),
            ("What we're looking for", ("What we're looking for", 2, None)),
            ("What We" + chr(0x2019) + "re Looking For", ("What We're Looking For", 2, None)),
            ("Education and Experience", ("Education and Experience", 2, None)),
            ("Education & Experience", ("Education & Experience", 2, None)),
        )
    ),
    *(
        pytest.param(
            f"Requirements\nFluency in SQL\nFive years of rail planning experience\n{heading}\n"
            "Plan the weekly timetable\nRun the depot reviews",
            ("Requirements", 2, None),
            id=f"duties under {heading!r}",
        )
        for heading in ("What you'll be doing", "What You'll Be Doing")
    ),
    *(
        pytest.param(
            f"Requirements\n\nFive years of rail planning experience\n\n{line}\n\nFluency in SQL",
            ("Requirements", 3, None),
            id=f"{line!r} under a bare heading",
        )
        for line in (
            "Degree desired",
            "Required to travel overnight",
            "Strong communication skills",
        )
    ),
    pytest.param(
        "Requirements\n\nFive years of rail planning experience\n\n"
        "Would be great at stakeholder work\n\nFluency in SQL",
        ("Requirements", 2, 1),
        id="a wish line under a bare heading",
    ),
    *(
        pytest.param(
            f"Requirements\nFluency in SQL\nFive years of rail planning experience\n{heading}\n"
            "Paid every two weeks\nA rail pass for the whole family",
            ("Requirements", 2, None),
            id=f"closing matter under {heading!r}",
        )
        for heading in ("Salary Range", "Equal Opportunity Statement")
    ),
]


@pytest.mark.parametrize(("body", "expected"), BARE_HEADING_BODIES)
def test_a_bare_heading_is_a_named_section_and_nothing_else(
    body: str, expected: tuple[str | None, int | None, int | None], inventory: CvInventory
) -> None:
    reading = reading_of("Data Engineer\n\n" + plain_layout(body), inventory)
    assert (reading.sections.value, reading.must_have.value, reading.preferred.value) == expected


NEED_LINES = "Five years of rail planning experience\nFluency in SQL"
WANT_LINES = "Experience with Kafka\nFamiliarity with Terraform"
NEEDED = (
    (fit.Stance.MUST_HAVE, "Five years of rail planning experience"),
    (fit.Stance.MUST_HAVE, "Fluency in SQL"),
)
WANTED = (
    (fit.Stance.PREFERRED, "Experience with Kafka"),
    (fit.Stance.PREFERRED, "Familiarity with Terraform"),
)
# Qualifications whose subject is a word closing matter also uses.
BENEFIT_WORD_ITEMS = (
    "Five years managing guest accommodation bookings",
    "Experience drafting privacy policy and data-handling documents",
    "Experience administering 401k and employer match plans",
    "Three years designing a benefits package for a regional workforce",
    "Knowledge of privacy policy frameworks",
)

GROUPED_PREFIX = "Qualifications\nRequired\n- Five years of SQL\nPreferred\n- Experience with Kafka"
GROUPED = (
    (fit.Stance.MUST_HAVE, "Five years of SQL"),
    (fit.Stance.PREFERRED, "Experience with Kafka"),
)

# The closing matter after an unmarked list ends the section it follows, so none of
# it reads as a requirement until another heading opens one.
CLOSING_MATTER = [
    pytest.param(
        f"Nice to have\n{WANT_LINES}\nExpected compensation range for this role is "
        "$140,000-160,000. Compensation depends on experience and location.",
        WANTED,
        id="pay sentence after the list",
    ),
    pytest.param(
        f"What you'll bring\n{NEED_LINES}\nSalary: $150,000 - 180,000k/yr",
        NEEDED,
        id="salary line after the list",
    ),
    pytest.param(
        f"Required Skills & Qualifications\n{NEED_LINES}\nNorthwind Rail welcomes "
        "candidates to apply to our inclusive community. We provide accommodations upon "
        "request to ensure fairness.",
        NEEDED,
        id="inclusion statement after the list",
    ),
    pytest.param(
        f"Required Skills & Qualifications\n{NEED_LINES}\n\nNorthwind Rail welcomes "
        "candidates to apply to our inclusive community. We provide accommodations upon "
        "request to ensure fairness.",
        NEEDED,
        id="inclusion paragraph after the list",
    ),
    pytest.param(
        f"What you'll bring\n{NEED_LINES}\nTravel as required\n#LI-AB1 We are committed "
        "to building a diverse team of Northwinders who plan trains in every region.",
        (*NEEDED, (fit.Stance.MUST_HAVE, "Travel as required")),
        id="recruiter tag glued to the closing sentence",
    ),
    pytest.param(
        f"Nice to Have\n{WANT_LINES}\nAbout the team This team builds the analytics "
        "capabilities the depots plan against\nBenefits to support your health, finances, "
        "and well-being Flexible paid time off and a rail pass",
        WANTED,
        id="closing headings glued to their text",
    ),
    pytest.param(
        f"Nice to Have\n{WANT_LINES}\nPeople matter at Northwind Rail and here are some "
        "of the perks and benefits we offer:\nFlexible hours\nA rail pass for the whole family",
        WANTED,
        id="perks introduced by a long line",
    ),
    pytest.param(
        f"What you'll bring\n{NEED_LINES}\nFor more information about how we collect and "
        "use your data, please review our Privacy Policy.",
        NEEDED,
        id="privacy notice after the list",
    ),
    pytest.param(
        f"What you'll bring\n{NEED_LINES}\nAbout the team This team builds the analytics "
        f"capabilities the depots plan against.\nNice to have\n{WANT_LINES}",
        (*NEEDED, *WANTED),
        id="a later heading opens the next section",
    ),
    pytest.param(
        "Requirements\nStrong understanding of healthcare security, compliance, and privacy "
        "requirements, including HIPAA\nCommitment to data integrity, privacy, and "
        "operational excellence.\nExperience collaborating with Operations, Engineering, "
        "Security, Privacy, and Legal teams.",
        (
            (
                fit.Stance.MUST_HAVE,
                "Strong understanding of healthcare security, compliance, and privacy "
                "requirements, including HIPAA",
            ),
            (
                fit.Stance.MUST_HAVE,
                "Commitment to data integrity, privacy, and operational excellence.",
            ),
            (
                fit.Stance.MUST_HAVE,
                "Experience collaborating with Operations, Engineering, Security, Privacy, "
                "and Legal teams.",
            ),
        ),
        id="requirements that name privacy and legal",
    ),
    pytest.param(
        f"What you'll bring\n{NEED_LINES}\nFlexible PTO and competitive pay, because "
        "work-life balance matters\nRRSP/401k match and competitive compensation packages\n"
        "Premium benefits package with customizable coverage",
        NEEDED,
        id="perk lines after the list",
    ),
    pytest.param(
        "What you'll bring\n- Five years of rail planning experience\n- Fluency in SQL\n"
        "- Benefits to support your health, finances, and well-being\n"
        "- You will receive a 401K plan with an employer match\n"
        "- This position is eligible for standard Company benefits including medical, "
        "dental, vision\n- Competitive compensation package that includes base salary, "
        "target bonus, and stock options\n- $140,000 - $200,000 a year",
        NEEDED,
        id="perk and pay items marked as the list's own",
    ),
    pytest.param(
        f"Nice to Have\n{WANT_LINES}\nPeople matter at Northwind Rail and these are some "
        "of the perks and benefits we created for our team:\nA rail pass for the whole family",
        WANTED,
        id="perks lead-in ending in a colon",
    ),
    pytest.param(
        f"Required Skills & Qualifications\n{NEED_LINES}\nNorthwind Rail Benefits & Perks\n"
        "Benefits starting from day one\nA rail pass for the whole family",
        NEEDED,
        id="a benefits heading that names the employer and joins two names",
    ),
    pytest.param(
        f"What you'll bring\n{NEED_LINES}\n$140,000 - $200,000 a year",
        NEEDED,
        id="a money range with no pay word",
    ),
    pytest.param(
        "Requirements\n- Experience managing programs valued up to $50 million.\n"
        "- Proven track record of managing $1M+ engagements\n"
        "- Familiarity with data privacy regulations\n"
        "- Knowledge of token systems, cryptographic signing, and key management.",
        (
            (fit.Stance.MUST_HAVE, "Experience managing programs valued up to $50 million."),
            (fit.Stance.MUST_HAVE, "Proven track record of managing $1M+ engagements"),
            (fit.Stance.MUST_HAVE, "Familiarity with data privacy regulations"),
            (
                fit.Stance.MUST_HAVE,
                "Knowledge of token systems, cryptographic signing, and key management.",
            ),
        ),
        id="requirements that name money and privacy",
    ),
    pytest.param(
        "Minimum Qualifications:\nRequired Technical Experience\n"
        "- Deep understanding of distributed systems\n- Experience designing REST APIs\n"
        "Modern Engineering Practices\n- Fluency with continuous delivery\n"
        "What Would Make You Stand Out\n- Experience scaling SaaS platforms",
        (
            (fit.Stance.MUST_HAVE, "Deep understanding of distributed systems"),
            (fit.Stance.MUST_HAVE, "Experience designing REST APIs"),
            (fit.Stance.MUST_HAVE, "Fluency with continuous delivery"),
            (fit.Stance.PREFERRED, "Experience scaling SaaS platforms"),
        ),
        id="sub-headings under a requirement heading",
    ),
    pytest.param(
        "Minimum Qualifications\nRequired\n- Fluency in SQL\nEducation\n- A degree in "
        "logistics\nResponsibilities\n- Run the depot rota\nLocation\n- Remote, Canada",
        (
            (fit.Stance.MUST_HAVE, "Fluency in SQL"),
            (fit.Stance.MUST_HAVE, "A degree in logistics"),
        ),
        id="a responsibility heading ends the grouped section",
    ),
    pytest.param(
        "Qualifications\n\nRequired\n\n- Three to five years in product management\n"
        "- Shipped AI products to production\n\nPreferred\n\n- Experience in healthcare data\n"
        "- Experience working remotely\n\nHow we work\n\n- Remote-first, real overlap\n"
        "- Written first, meet last\n- Day One mentality",
        (
            (fit.Stance.MUST_HAVE, "Three to five years in product management"),
            (fit.Stance.MUST_HAVE, "Shipped AI products to production"),
            (fit.Stance.PREFERRED, "Experience in healthcare data"),
            (fit.Stance.PREFERRED, "Experience working remotely"),
        ),
        id="a narrative heading after a sub-section of another stance ends the group",
    ),
    *(
        pytest.param(
            f"{GROUPED_PREFIX}\n{heading}\n- {items[0]}\n- {items[1]}",
            (*GROUPED, *((stance, item) for item in items)),
            id=f"{heading!r} after a sub-section of another stance",
        )
        for heading, stance, items in (
            (
                "Other Required",
                fit.Stance.MUST_HAVE,
                ("Able to travel up to 25%", "Proficient with spreadsheet software"),
            ),
            (
                "Knowledge, Skills & Abilities",
                fit.Stance.MUST_HAVE,
                ("Works well with cross-functional teams", "Presents technical options"),
            ),
            ("Desired Characteristics", fit.Stance.PREFERRED, ("Strategic thinker", "Curious")),
            ("Core Competencies", fit.Stance.MUST_HAVE, ("Stakeholder engagement", "Planning")),
            ("Competencies", fit.Stance.MUST_HAVE, ("Stakeholder engagement", "Planning")),
            ("Minimum Required", fit.Stance.MUST_HAVE, ("Fluency in Python", "Planning")),
        )
    ),
    pytest.param(
        GROUPED_PREFIX
        + "".join(
            f"\n{heading}\n- {item}"
            for heading, item in (
                ("How we work", "Remote-first, real overlap"),
                ("What We Offer", "A rail pass for the whole family"),
                ("We offer", "Dental cover from day one"),
                ("What Success Looks Like", "The first release reaches every depot"),
                ("Success Measures", "Depot adoption"),
                ("Why you'll love this role", "A small team that ships"),
                ("Our Cultural Beliefs", "Passengers first"),
            )
        ),
        GROUPED,
        id="narrative headings after a sub-section of another stance",
    ),
    pytest.param(
        "Qualifications\nRequired\n- Five years of SQL\nValued but Not Required\n- React\n- AWS",
        (
            (fit.Stance.MUST_HAVE, "Five years of SQL"),
            (fit.Stance.PREFERRED, "React"),
            (fit.Stance.PREFERRED, "AWS"),
        ),
        id="a negated stance heading in a group",
    ),
    pytest.param(
        "Valued but Not Required\n- React\n- AWS",
        ((fit.Stance.PREFERRED, "React"), (fit.Stance.PREFERRED, "AWS")),
        id="a negated stance heading on its own",
    ),
    pytest.param(
        f"Nice to have\n{WANT_LINES}\nMinimum Requirements and Qualifications\n{NEED_LINES}",
        (*WANTED, *NEEDED),
        id="a qualified heading that joins two names",
    ),
    pytest.param(
        f"Requirements\n{NEED_LINES}\n\nPreferred: Experience with Kafka; Familiarity with "
        "Terraform\n\nRemote within Canada. No visa sponsorship is available.",
        (*NEEDED, *WANTED),
        id="a run-in section ends with its own line",
    ),
    pytest.param(
        "Qualifications: The ideal candidate will have:\n- Five years of rail planning "
        "experience\n- Fluency in SQL",
        NEEDED,
        id="a run-in lead-in over a list",
    ),
    pytest.param(
        "What we require\n- Five years of rail planning experience\n- Fluency in SQL",
        NEEDED,
        id="a heading that says what the employer requires",
    ),
    pytest.param(
        "Requirements\n- Comfortable using AI-augmented tools (e.g., Copilot, Claude\n"
        "Code) as part of your workflow\n- Fluency in SQL\n- Five years of rail planning "
        "experience",
        (
            (
                fit.Stance.MUST_HAVE,
                "Comfortable using AI-augmented tools (e.g., Copilot, Claude Code) as part of "
                "your workflow",
            ),
            (fit.Stance.MUST_HAVE, "Fluency in SQL"),
            (fit.Stance.MUST_HAVE, "Five years of rail planning experience"),
        ),
        id="an item wrapped inside a bracket",
    ),
    pytest.param(
        "Requirements\n- " + "\n- ".join(BENEFIT_WORD_ITEMS),
        tuple((fit.Stance.MUST_HAVE, item) for item in BENEFIT_WORD_ITEMS),
        id="marked qualifications that name benefits and legal subjects",
    ),
    pytest.param(
        "Requirements\n" + "\n".join(BENEFIT_WORD_ITEMS) + "\nFluency in SQL",
        tuple((fit.Stance.MUST_HAVE, item) for item in (*BENEFIT_WORD_ITEMS, "Fluency in SQL")),
        id="unmarked qualifications that name benefits and legal subjects",
    ),
    pytest.param(
        "What you'll bring\n- Five years of rail planning experience\n- Fluency in SQL\n"
        "- Competitive pay based on your knowledge, skills and experience\n"
        "- A 401k with employer match after one year of service",
        NEEDED,
        id="marked pay and plan items that mention skills or years",
    ),
    pytest.param(
        f"What you'll bring\n{NEED_LINES}\nCompetitive pay based on your knowledge, skills "
        "and experience\nA 401k with employer match after one year of service",
        NEEDED,
        id="pay and plan lines that mention skills or years after the list",
    ),
    pytest.param(
        "Requirements: Fluency in SQL\n- Five years of rail planning experience\n"
        "- Experience with Kafka",
        (
            (fit.Stance.MUST_HAVE, "Fluency in SQL"),
            (fit.Stance.MUST_HAVE, "Five years of rail planning experience"),
            (fit.Stance.MUST_HAVE, "Experience with Kafka"),
        ),
        id="a run-in item over a list",
    ),
    pytest.param(
        "Qualifications: Fluency in SQL.\nFive years of analytics experience.\n"
        "Experience with Kafka.",
        (
            (fit.Stance.MUST_HAVE, "Fluency in SQL."),
            (fit.Stance.MUST_HAVE, "Five years of analytics experience."),
            (fit.Stance.MUST_HAVE, "Experience with Kafka."),
        ),
        id="unmarked items after a run-in item",
    ),
    pytest.param(
        f"Requirements\n{NEED_LINES}\n\nPreferred: Experience with Kafka; Familiarity with "
        "Terraform.\n\nRemote within Canada.",
        (
            *NEEDED,
            (fit.Stance.PREFERRED, "Experience with Kafka"),
            (fit.Stance.PREFERRED, "Familiarity with Terraform."),
        ),
        id="a one-sentence paragraph after a run-in section",
    ),
    pytest.param(
        "PHYSICAL JOB REQUIREMENTS\n\n"
        "- The physical demands described here are representative of those an employee must "
        "meet.\n\n"
        "Northwind builds a culture of inclusion. Discrimination has no place here.\n\n"
        "DIFFERENTIATING FACTORS\n\n"
        "Autonomy: Established and productive individual contributor.\n"
        "Works independently with general supervision.\n\n"
        "Required Knowledge and Experience: Requires a bachelor's degree and four years of "
        "experience.\n\n"
        "Recruitment Fraud Alert\n\n"
        "We are aware of phishing scams targeting job seekers. Please keep the following in "
        "mind:\n\nApply only through official Northwind channels.",
        ((fit.Stance.MUST_HAVE, "Requires a bachelor's degree and four years of experience."),),
        id="a physical job requirements heading",
    ),
    pytest.param(
        "Requirements\n- Four years of field service experience.\n\nWorking Conditions:\n\n"
        "- This is a field role based in the assigned territory.\n"
        "Will be exposed to outdoor weather conditions.\n"
        "May be required to travel more than 50% of the time.\n"
        "Required to use standard office equipment.\n- Occasional weekend work.",
        ((fit.Stance.MUST_HAVE, "Four years of field service experience."),),
        id="travel and physical conditions under a working conditions heading",
    ),
    *(
        pytest.param(
            f"Requirements\n- Fluency in SQL\n\n{heading}\n- Ability to lift 25 pounds is required",
            ((fit.Stance.MUST_HAVE, "Fluency in SQL"),),
            id=f"a stance line under {heading!r}",
        )
        for heading in ("Physical Demands", "Work Environment")
    ),
]


@pytest.mark.parametrize(("body", "expected"), CLOSING_MATTER)
def test_closing_matter_ends_the_section_above_it(
    body: str, expected: tuple[tuple[fit.Stance, str], ...], inventory: CvInventory
) -> None:
    reading = reading_of("Data Engineer\n\n" + plain_layout(body), inventory)
    assert tuple((line.stance, line.text) for line in reading.requirements) == expected


TITLED_PROSE = (
    "Data Handling Statement\n"
    "Employees are expected to use approved tools and follow company policy when handling "
    "records.\n"
    "Grow your career with a fast-growing team that is changing how care is delivered.\n"
    "Northwind is a winner of the 2025 Great Places Award for Remote Work! We have also "
    "received several culture awards. See the details here: https://example.com/awards/\n"
    "We are proud to be an equal opportunity employer."
)
DEGREE_ITEM = "Bachelor's degree in a quantitative field"
PLATFORM_ITEM = "Five years of experience running data platforms"
WRAPPED_ITEM = "- Experience with SQL and\ndbt"

# An unmarked line beside a list item is one of the list's items when it opens like one;
# a wrap, a line after a blank, a section name, a question, or a label over prose is not.
LINES_BESIDE_A_LIST = [
    pytest.param(
        f"Requirements\n- {DEGREE_ITEM}\n{PLATFORM_ITEM}",
        ((fit.Stance.MUST_HAVE, DEGREE_ITEM), (fit.Stance.MUST_HAVE, PLATFORM_ITEM)),
        id="an unmarked requirement after the last item",
    ),
    pytest.param(
        f"Requirements\n{WRAPPED_ITEM}\n- {DEGREE_ITEM}\n{PLATFORM_ITEM}\nFluency in SQL",
        (
            (fit.Stance.MUST_HAVE, "Experience with SQL and dbt"),
            (fit.Stance.MUST_HAVE, DEGREE_ITEM),
            (fit.Stance.MUST_HAVE, PLATFORM_ITEM),
            (fit.Stance.MUST_HAVE, "Fluency in SQL"),
        ),
        id="unmarked requirements after a list with a wrapped item",
    ),
    pytest.param(
        f"Requirements\n{WRAPPED_ITEM}\nNice to have\n- Experience with Kafka",
        (
            (fit.Stance.MUST_HAVE, "Experience with SQL and dbt"),
            (fit.Stance.PREFERRED, "Experience with Kafka"),
        ),
        id="a heading after a wrapped item",
    ),
    pytest.param(
        f"Requirements\n- {DEGREE_ITEM} or an equivalent\ncombination of education and experience",
        (
            (
                fit.Stance.MUST_HAVE,
                f"{DEGREE_ITEM} or an equivalent combination of education and experience",
            ),
        ),
        id="a wrap that continues in lower case",
    ),
    pytest.param(
        "Requirements\n- Five years of experience with\nPython and SQL",
        ((fit.Stance.MUST_HAVE, "Five years of experience with Python and SQL"),),
        id="a wrap after an item that runs on",
    ),
    pytest.param(
        f"Requirements\n- {DEGREE_ITEM}\n\nThe team reviews every application within a week.",
        ((fit.Stance.MUST_HAVE, DEGREE_ITEM),),
        id="a paragraph after the list",
    ),
    pytest.param(
        "\n".join(
            (
                "REQUIREMENTS",
                "- Eight years of data engineering experience.",
                "- Strong proficiency in Python or Scala.",
                "Advanced SQL expertise, including performance tuning.",
                "- Deep experience with Apache Spark.",
            )
        ),
        (
            (fit.Stance.MUST_HAVE, "Eight years of data engineering experience."),
            (fit.Stance.MUST_HAVE, "Strong proficiency in Python or Scala."),
            (fit.Stance.MUST_HAVE, "Advanced SQL expertise, including performance tuning."),
            (fit.Stance.MUST_HAVE, "Deep experience with Apache Spark."),
        ),
        id="an unmarked sentence between marked ones",
    ),
    pytest.param(
        "Preferred, Not required:\n- Healthcare industry experience\n"
        "- MBA or equivalent business experience\nFamiliarity with coding assistants\n\n#Remote",
        (
            (fit.Stance.PREFERRED, "Healthcare industry experience"),
            (fit.Stance.PREFERRED, "MBA or equivalent business experience"),
            (fit.Stance.PREFERRED, "Familiarity with coding assistants"),
        ),
        id="an unmarked last item before a blank line",
    ),
    pytest.param(
        "\n".join(
            (
                "Your mission",
                "- Prepare monthly review meetings",
                "- Contribute to automation projects",
                "Your qualifications",
                "You are studying for a master's degree in marketing.",
                "You are fluent in English and French.",
                "You are comfortable analysing data and tracking KPIs.",
            )
        ),
        (
            (fit.Stance.MUST_HAVE, "You are studying for a master's degree in marketing."),
            (fit.Stance.MUST_HAVE, "You are fluent in English and French."),
            (fit.Stance.MUST_HAVE, "You are comfortable analysing data and tracking KPIs."),
        ),
        id="a section name after a list opens its section",
    ),
    pytest.param(
        "\n".join(
            (
                "Requirements",
                "- Five years of consulting experience",
                "- Fluent in English and Swedish",
                "Inclusion statement",
                "Northwind is a catalyst for progress. We value every voice.",
                "Want to learn more?",
                "Visit the other sections of our website to find out more.",
                "Our Culture & Values",
                "Discover our culture and values.",
            )
        ),
        (
            (fit.Stance.MUST_HAVE, "Five years of consulting experience"),
            (fit.Stance.MUST_HAVE, "Fluent in English and Swedish"),
        ),
        id="a label over a paragraph after a list",
    ),
    pytest.param(
        "\n".join(
            (
                "Position Details:",
                "",
                "- This is a mobile position, and regular travel is essential.",
                "Will be exposed to local weather and may be required to stand for long periods.",
                "May be required to travel more than half of the time.",
                "Flexible hours, including some evenings as needed",
                "- Over 50% routine travel required",
            )
        ),
        (
            (
                fit.Stance.MUST_HAVE,
                "Will be exposed to local weather and may be required to stand for long periods.",
            ),
            (fit.Stance.MUST_HAVE, "May be required to travel more than half of the time."),
            (fit.Stance.MUST_HAVE, "Over 50% routine travel required"),
        ),
        id="unmarked items between marked ones outside a requirement section",
    ),
    pytest.param(
        "\n".join(
            (
                "Job Responsibilities",
                "",
                "- Performs site initiation and close-out visits. Documents each visit in a "
                "report.",
                "- For the monitoring plan: Assesses site processes",
                "Conducts source document review of appropriate site records and charts",
                "Verifies site compliance with data capture requirements",
                "- Reviews the site file for accuracy and completeness. Reconciles it with the "
                "master file.",
                "- Prepares for and attends investigator meetings.",
                "",
                "Qualifications:",
                "",
                "- Bachelor's degree in a related field",
                "- Knowledge of Good Clinical Practice",
            )
        ),
        (
            (fit.Stance.MUST_HAVE, "Bachelor's degree in a related field"),
            (fit.Stance.MUST_HAVE, "Knowledge of Good Clinical Practice"),
        ),
        id="an unmarked item over a marked one is no heading",
    ),
    pytest.param(
        "\n".join(
            (
                "Responsibilities",
                "7. Identify best practices from loss reviews",
                "8. Manage the partner ecosystem across the customer journey",
                "Qualifications",
                "Significant experience (8 years minimum) in a sales position",
                "Proven experience in consulting selling",
                "Exposure in software selling is required",
                "- Change management",
                "- Effective communication",
            )
        ),
        (
            (
                fit.Stance.MUST_HAVE,
                "Significant experience (8 years minimum) in a sales position",
            ),
            (fit.Stance.MUST_HAVE, "Proven experience in consulting selling"),
            (fit.Stance.MUST_HAVE, "Exposure in software selling is required"),
            (fit.Stance.MUST_HAVE, "Change management"),
            (fit.Stance.MUST_HAVE, "Effective communication"),
        ),
        id="unmarked items that marked items continue",
    ),
    pytest.param(
        "\n".join(
            (
                "About the role",
                "We build networks.",
                "",
                "Requirements",
                "- Five years of network routing experience.",
                "- Strong written communication skills.",
                "What is in it for you?",
                "- Work for a large software company",
                "- Opportunities for career progression",
            )
        ),
        (
            (fit.Stance.MUST_HAVE, "Five years of network routing experience."),
            (fit.Stance.MUST_HAVE, "Strong written communication skills."),
        ),
        id="a question over a list heads it",
    ),
    pytest.param(
        "\n".join(
            (
                "Preferred Qualifications",
                "- Experience with claims data",
                "- Exposure to Agile tools (Jira or similar)",
                "Education",
                "Bachelor's degree required in a quantitative field.",
            )
        ),
        (
            (fit.Stance.PREFERRED, "Experience with claims data"),
            (fit.Stance.PREFERRED, "Exposure to Agile tools (Jira or similar)"),
            (fit.Stance.MUST_HAVE, "Bachelor's degree required in a quantitative field."),
        ),
        id="a label after a list heads the sentence under it",
    ),
    pytest.param(
        "\n".join(
            (
                "Qualifications & Experience",
                "- Outstanding communication and negotiation skills.",
                "- Fluent in English and Swedish",
                "Inclusion statement",
                "Northwind helps industries connect the virtual and real worlds of their "
                "operations.",
                "Want to learn more?",
                "Visit the other sections of our website to find out more.",
                "Our Culture & Values",
                "Discover our culture and values.",
            )
        ),
        (
            (fit.Stance.MUST_HAVE, "Outstanding communication and negotiation skills."),
            (fit.Stance.MUST_HAVE, "Fluent in English and Swedish"),
        ),
        id="one-sentence prose after a label is no item",
    ),
    pytest.param(
        "\n".join(
            (
                "Requirements",
                "- Technical depth",
                "You have shipped services to production in Python.",
                "- Clear writing",
                "You write design documents others can act on.",
            )
        ),
        (
            (
                fit.Stance.MUST_HAVE,
                "Technical depth You have shipped services to production in Python.",
            ),
            (fit.Stance.MUST_HAVE, "Clear writing You write design documents others can act on."),
        ),
        id="a sentence under a label item describes it",
    ),
    pytest.param(
        "\n".join(
            (
                "Requirements",
                "- Experience building models in commercial CAD software.",
                "- Development of any type of simulation automation.",
                "What's in it for you",
                "",
                "- Work in a culture of collaboration and innovation.",
                "- Professional development and upskilling opportunities",
                "- Collaborative, mission-driven team culture",
            )
        ),
        (
            (fit.Stance.MUST_HAVE, "Experience building models in commercial CAD software."),
            (fit.Stance.MUST_HAVE, "Development of any type of simulation automation."),
        ),
        id="a short line over the list after a blank line heads it",
    ),
    pytest.param(
        "\n".join(
            (
                "Preferred Qualifications",
                "- Experience in drug information or a specialty area",
                "- Highly principled, with a record of ethical behavior",
                "Additional Information",
                "OUR CULTURAL BELIEFS:",
                "Patient minded: I act with the patient's best interest in mind.",
            )
        ),
        (
            (fit.Stance.PREFERRED, "Experience in drug information or a specialty area"),
            (fit.Stance.PREFERRED, "Highly principled, with a record of ethical behavior"),
        ),
        id="a line over a heading ends the list",
    ),
    pytest.param(
        "\n".join(
            (
                "Requirements",
                "- Five years of SQL",
                "- Excellent presentation skills",
                "Strong communication skills",
                "Required to lift 25 pounds",
                "Required to travel overnight",
                "Degree required",
                "Degree desired",
                "Would be great at stakeholder work",
                "- Fluency in Python",
            )
        ),
        (
            *(
                (fit.Stance.MUST_HAVE, text)
                for text in (
                    "Five years of SQL",
                    "Excellent presentation skills",
                    "Strong communication skills",
                    "Required to lift 25 pounds",
                    "Required to travel overnight",
                    "Degree required",
                    "Degree desired",
                )
            ),
            (fit.Stance.PREFERRED, "Would be great at stakeholder work"),
            (fit.Stance.MUST_HAVE, "Fluency in Python"),
        ),
        id="lines that use a section word are items of the list",
    ),
    *(
        pytest.param(
            "Expectations in this role (required)\n- Uses AI tools daily\n"
            f"Additional skills we would prioritize (nice to have){ellipsis}\n"
            "- Curiosity about data systems\n- A bias toward simple designs",
            (
                (fit.Stance.MUST_HAVE, "Uses AI tools daily"),
                (fit.Stance.PREFERRED, "Curiosity about data systems"),
                (fit.Stance.PREFERRED, "A bias toward simple designs"),
            ),
            id=f"a lead-in ending in {name} over a list heads it",
        )
        for name, ellipsis in (("three dots", "..."), ("an ellipsis", "…"))
    ),
    pytest.param(
        "About You\n- Bachelor's degree\n- Five years of product work\n\n"
        "It would be great if you also had...\n\n- Master's degree in a related field\n"
        "- Figma experience",
        (
            (fit.Stance.MUST_HAVE, "Bachelor's degree"),
            (fit.Stance.MUST_HAVE, "Five years of product work"),
            (fit.Stance.PREFERRED, "Master's degree in a related field"),
            (fit.Stance.PREFERRED, "Figma experience"),
        ),
        id="a wish lead-in over a list heads a preferred section",
    ),
    *(
        pytest.param(
            f"{heading}\n{items}\n\n{TITLED_PROSE}",
            expected,
            id=f"a titled prose paragraph after {name}",
        )
        for name, heading, items, expected in (
            (
                "a marked list",
                "Qualifications",
                "- Bachelor's degree in a relevant field\n"
                "- Experience with reporting tools preferred",
                (
                    (fit.Stance.MUST_HAVE, "Bachelor's degree in a relevant field"),
                    (fit.Stance.PREFERRED, "Experience with reporting tools preferred"),
                ),
            ),
            (
                "an unmarked list",
                "Qualifications",
                "Bachelor's degree in a relevant field\nExperience with reporting tools preferred",
                (
                    (fit.Stance.MUST_HAVE, "Bachelor's degree in a relevant field"),
                    (fit.Stance.PREFERRED, "Experience with reporting tools preferred"),
                ),
            ),
            (
                "a list in a group",
                "Qualifications\n\nRequired",
                "- Five years of SQL",
                ((fit.Stance.MUST_HAVE, "Five years of SQL"),),
            ),
            (
                "an unmarked preferred list",
                "Preferred Qualifications",
                "Experience with SQL\nExperience with Kafka",
                (
                    (fit.Stance.PREFERRED, "Experience with SQL"),
                    (fit.Stance.PREFERRED, "Experience with Kafka"),
                ),
            ),
        )
    ),
    pytest.param(
        "What You Bring\n\n6+ years building distributed systems.\n"
        "Deep expertise in system design and backend frameworks.\n\n"
        "Leadership Qualities & Mindset\n"
        "Provides technical clarity. Identifies systemic issues and drives shared fixes.\n\n"
        "Builds talent through thoughtful mentorship.\n"
        "Acts with humility and focuses on team success.\n\n"
        "Core Value Expectations\n\n"
        "Earn trust through transparent decisions.\n"
        "Win as a team by resolving cross-team challenges.\n\n"
        "Why This Role Matters\nYou keep the platform resilient and maintainable. "
        "Every team ships on what you build.",
        tuple(
            (fit.Stance.MUST_HAVE, text)
            for text in (
                "6+ years building distributed systems.",
                "Deep expertise in system design and backend frameworks.",
                "Provides technical clarity. Identifies systemic issues and drives shared fixes.",
                "Builds talent through thoughtful mentorship.",
                "Acts with humility and focuses on team success.",
                "Earn trust through transparent decisions.",
                "Win as a team by resolving cross-team challenges.",
            )
        ),
        id="titled paragraphs of qualities and of values",
    ),
    pytest.param(
        "Preferred\n- Experience with healthcare data standards.\n"
        "- Familiarity with cloud platforms.\n\n"
        "Apply now and join the Northwind team.\n\ncareers.northwind.example\n\n"
        "Application deadline\n\n"
        "Northwind accepts applications on a rolling basis; we will review applications until "
        "the role is filled.\n\nSponsorship\n\nSponsorship for this role is not guaranteed.\n\n"
        "As an organization dedicated to improving lives, Northwind fosters an inclusive "
        "environment and considers every applicant.",
        (
            (fit.Stance.PREFERRED, "Experience with healthcare data standards."),
            (fit.Stance.PREFERRED, "Familiarity with cloud platforms."),
        ),
        id="lone titles after prose that followed the list",
    ),
    pytest.param(
        "Essential Technical Qualifications\n"
        "Education: Master's degree in computer science or a related discipline.\n"
        "Programming: High proficiency in Python and one compiled language.\n"
        "Behavioral Signals & Leadership\n"
        "System Thinking: You clarify the problem before proposing a solution. You reason "
        "through trade-offs.\n"
        "Navigating Ambiguity: You thrive when requirements are vague.\n"
        "Influence without Authority: You persuade senior leaders with data-driven prototypes. "
        "You build trust across teams.\n\n"
        "What You'll Do:\nDesign and maintain backend systems.\nMentor junior engineers.",
        tuple(
            (fit.Stance.MUST_HAVE, text)
            for text in (
                "Education: Master's degree in computer science or a related discipline.",
                "Programming: High proficiency in Python and one compiled language.",
                "System Thinking: You clarify the problem before proposing a solution. You "
                "reason through trade-offs.",
                "Navigating Ambiguity: You thrive when requirements are vague.",
                "Influence without Authority: You persuade senior leaders with data-driven "
                "prototypes. You build trust across teams.",
            )
        ),
        id="a title over labelled lines of several sentences",
    ),
    pytest.param(
        "Preferred\nExperience with imaging interoperability standards.\n"
        "Experience with cloud-based SaaS platforms.\n\n"
        "What Success Looks Like in Year One\n\n"
        "Launch new image exchange capabilities.\nDeliver a clear product roadmap.\n"
        "Increase adoption across existing customers.",
        (
            (fit.Stance.PREFERRED, "Experience with imaging interoperability standards."),
            (fit.Stance.PREFERRED, "Experience with cloud-based SaaS platforms."),
        ),
        id="a success-outcome title after a list",
    ),
    pytest.param(
        "Core Competencies:\nSkill & Practice\n\n"
        "Applies sound engineering practice to deliver reliable systems.\n\n"
        "Knowledge & Application\n\n"
        "Understands the domain and applies it to customer problems.",
        (
            (
                fit.Stance.MUST_HAVE,
                "Applies sound engineering practice to deliver reliable systems.",
            ),
            (fit.Stance.MUST_HAVE, "Understands the domain and applies it to customer problems."),
        ),
        id="lone title-case labels under a requirement heading",
    ),
    pytest.param(
        "Location:\nRemote (East Coast Preferred)\n\nDuration:\n12 Months\n\nWhat you'll do\n\n"
        "- Run the weekly planning meeting\n\nRequirements\n\n- Five years of analytics experience",
        ((fit.Stance.MUST_HAVE, "Five years of analytics experience"),),
        id="a field value under a field label",
    ),
    pytest.param(
        "Required Experience:\n5 years experience as a dosimetrist\n"
        "In-depth understanding of radiation oncology workflows\n"
        "Extensive experience with treatment planning\nPreferred Experience:\n"
        "Experience with multiple planning systems\nCustomer service experience",
        (
            (fit.Stance.MUST_HAVE, "5 years experience as a dosimetrist"),
            (fit.Stance.MUST_HAVE, "In-depth understanding of radiation oncology workflows"),
            (fit.Stance.MUST_HAVE, "Extensive experience with treatment planning"),
            (fit.Stance.PREFERRED, "Experience with multiple planning systems"),
            (fit.Stance.PREFERRED, "Customer service experience"),
        ),
        id="unpunctuated item lines under a heading",
    ),
    *(
        pytest.param(
            layout(body),
            expected,
            id=f"{name} {spacing}",
        )
        for name, body, expected in (
            (
                "title-case sub-headings over one-sentence items",
                "Requirements\nBachelor's degree in a quantitative field.\n"
                "Five years of analytics experience.\nTechnical Skills\n"
                "Expert SQL with strong data modeling skills.\n"
                "Strong programming skills in Python or Scala.\nKnowledge & Skills\n"
                "Strong understanding of clinical trial conduct.\nExcellent communication skills.",
                tuple(
                    (fit.Stance.MUST_HAVE, text)
                    for text in (
                        "Bachelor's degree in a quantitative field.",
                        "Five years of analytics experience.",
                        "Expert SQL with strong data modeling skills.",
                        "Strong programming skills in Python or Scala.",
                        "Strong understanding of clinical trial conduct.",
                        "Excellent communication skills.",
                    )
                ),
            ),
            (
                "title-case sub-headings over one item each",
                "Qualifications\nFour years of data governance experience.\n"
                "Systems & AI Foundations\nWorking knowledge of modern data architectures.\n"
                "Business Acumen & Communication\n"
                "Proven ability to manage priorities across matrixed stakeholders.",
                tuple(
                    (fit.Stance.MUST_HAVE, text)
                    for text in (
                        "Four years of data governance experience.",
                        "Working knowledge of modern data architectures.",
                        "Proven ability to manage priorities across matrixed stakeholders.",
                    )
                ),
            ),
            (
                "an education sub-heading over a minimum and an asset",
                "Requirements\nFive years building web services.\nEducation\n"
                "Minimum: a bachelor's degree in computer science or a related field.\n"
                "An asset (not required): a master's degree in a related field.",
                (
                    (fit.Stance.MUST_HAVE, "Five years building web services."),
                    (
                        fit.Stance.MUST_HAVE,
                        "Minimum: a bachelor's degree in computer science or a related field.",
                    ),
                    (
                        fit.Stance.PREFERRED,
                        "An asset (not required): a master's degree in a related field.",
                    ),
                ),
            ),
            (
                "a title over a lead-in to the interview steps",
                "Requirements\n- Five years building web services.\n- Strong SQL.\n"
                "Interviewing with Northwind\nWe value your time and believe in a transparent "
                "hiring process. Here is the process you can expect:\n"
                "Follow the application process and submit your resume.\n"
                "A conversation with the hiring manager\nA practical case exercise\n"
                "Once all interviews are complete, the manager will be in touch with a final "
                "decision.",
                (
                    (fit.Stance.MUST_HAVE, "Five years building web services."),
                    (fit.Stance.MUST_HAVE, "Strong SQL."),
                ),
            ),
        )
        for spacing, layout in (
            ("line by line", str),
            ("with a blank line between lines", lambda body: body.replace("\n", "\n\n")),
        )
    ),
    pytest.param(
        "Requirements\nYou have five years of SQL experience.\nYou write clear design documents.",
        (
            (fit.Stance.MUST_HAVE, "You have five years of SQL experience."),
            (fit.Stance.MUST_HAVE, "You write clear design documents."),
        ),
        id="full-sentence requirements straight under the heading",
    ),
    pytest.param(
        "Requirements\n\nPython Programming\nYou write clean, tested code.",
        (
            (fit.Stance.MUST_HAVE, "Python Programming"),
            (fit.Stance.MUST_HAVE, "You write clean, tested code."),
        ),
        id="a title-case item first under the heading",
    ),
    pytest.param(
        "Requirements\n- Fluency in SQL\n\nTechnical Skills\n- Python\n- Kafka",
        ((fit.Stance.MUST_HAVE, "Fluency in SQL"),),
        id="a title-case heading over a list after a list",
    ),
    pytest.param(
        "Requirements\nFluency in SQL.\n\nFive years of experience building ETL\n"
        "Pipelines in Python and SQL.",
        (
            (fit.Stance.MUST_HAVE, "Fluency in SQL."),
            (
                fit.Stance.MUST_HAVE,
                "Five years of experience building ETL Pipelines in Python and SQL.",
            ),
        ),
        id="a requirement wrapped mid-sentence after a blank line",
    ),
]


@pytest.mark.parametrize(("body", "expected"), LINES_BESIDE_A_LIST)
def test_an_unmarked_line_beside_a_list_reads_as_an_item_a_heading_or_a_wrap(
    body: str, expected: tuple[tuple[fit.Stance, str], ...], inventory: CvInventory
) -> None:
    reading = reading_of("Data Engineer\n\n" + plain_layout(body), inventory)
    assert tuple((line.stance, line.text) for line in reading.requirements) == expected


_STANCE_ASIDES = ("nice to have", "preferred", "a plus", "bonus", "optional")
_LONG_LEAD_TEXT = (
    "Additional skills and experience we would prioritize for this team in its first year "
    "of building out the platform"
)
_LONG_LEAD = _LONG_LEAD_TEXT.split()
_LISTS_UNDER_A_LEAD = {
    "right under it": "{lead}\n- Experience with Kafka\n- Experience with dbt",
    "after a blank line": "\n{lead}\n\n- Experience with Kafka\n- Experience with dbt",
}


def _stance_lead(aside: str, words: int, end: str) -> str:
    """Return a bare line of ``words`` words closing on ``(aside)`` and then ``end``."""
    return " ".join(_LONG_LEAD[: words - len(aside.split())]) + f" ({aside}){end}"


def _stances_under_a_lead(lead: str, layout: str, inventory: CvInventory) -> dict[str, fit.Stance]:
    body = (
        "What you bring (required)\n- Five years of SQL\n- Fluency in Python\n"
        + _LISTS_UNDER_A_LEAD[layout].format(lead=lead)
    )
    reading = reading_of("Data Engineer\n\n" + plain_layout(body), inventory)
    return {line.whole: line.stance for line in reading.requirements}


@pytest.mark.parametrize("layout", list(_LISTS_UNDER_A_LEAD))
@pytest.mark.parametrize("end", ["", ":", "."])
@pytest.mark.parametrize("words", range(9, 15))
@pytest.mark.parametrize("aside", _STANCE_ASIDES)
def test_a_long_line_closing_on_a_stance_aside_heads_the_list_under_it(
    aside: str, words: int, end: str, layout: str, inventory: CvInventory
) -> None:
    lead = _stance_lead(aside, words, end)
    assert len(lead.split()) == words
    stances = _stances_under_a_lead(lead, layout, inventory)
    assert stances == {
        "Five years of SQL": fit.Stance.MUST_HAVE,
        "Fluency in Python": fit.Stance.MUST_HAVE,
        "Experience with Kafka": fit.Stance.PREFERRED,
        "Experience with dbt": fit.Stance.PREFERRED,
    }


@pytest.mark.parametrize("layout", list(_LISTS_UNDER_A_LEAD))
@pytest.mark.parametrize(
    "lead",
    [
        pytest.param(" ".join(_LONG_LEAD[:12]), id="no aside"),
        pytest.param(
            "Experience with Airflow is nice to have for this team and its partners",
            id="a stance mid-line",
        ),
        pytest.param(" ".join(_LONG_LEAD[:10]) + " (remote)", id="a place aside"),
        pytest.param(" ".join(_LONG_LEAD[:9]) + " (2 openings)", id="a count aside"),
        pytest.param(_stance_lead("nice to have", 17, ""), id="past the word bound"),
    ],
)
def test_a_long_line_with_no_closing_stance_aside_leaves_the_list_in_its_section(
    lead: str, layout: str, inventory: CvInventory
) -> None:
    stances = _stances_under_a_lead(lead, layout, inventory)
    assert stances["Experience with Kafka"] is fit.Stance.MUST_HAVE
    assert stances["Experience with dbt"] is fit.Stance.MUST_HAVE


def test_a_stance_aside_line_with_no_list_under_it_heads_nothing(inventory: CvInventory) -> None:
    body = (
        "What you bring (required)\n- Five years of SQL\n- Fluency in Python\n\n"
        f"{_stance_lead('nice to have', 12, '')}\n"
        "Experience with Kafka is expected for every engineer on the team.\n"
    )
    reading = reading_of("Data Engineer\n\n" + plain_layout(body), inventory)
    assert fit.Stance.PREFERRED not in {
        line.stance for line in reading.requirements if "Kafka" in line.whole
    }


@pytest.mark.parametrize(
    "wrap",
    [
        "across the data platform in production and in our analytics stack (nice to have)",
        "across the data platform we run in production and in analytics (nice to have)",
    ],
)
def test_a_wrapped_item_closing_on_a_stance_aside_stays_one_item(
    wrap: str, inventory: CvInventory
) -> None:
    item = "Experience with Kafka, Spark, and other streaming tools that we use"
    reading = reading_of(
        f"Data Engineer\n\nRequirements\n- {item}\n{wrap}\n- Python\n- dbt\n", inventory
    )
    assert [(line.whole, line.stance) for line in reading.requirements] == [
        (f"{item} {wrap}", fit.Stance.PREFERRED),
        ("Python", fit.Stance.MUST_HAVE),
        ("dbt", fit.Stance.MUST_HAVE),
    ]


def test_an_unmarked_item_closing_on_a_stance_aside_heads_no_list(inventory: CvInventory) -> None:
    item = "Experience with Airflow orchestration in production data settings (preferred)"
    reading = reading_of(
        f"Data Engineer\n\nRequirements\n- Five years of SQL\n{item}\n- Python\n- dbt\n",
        inventory,
    )
    assert [(line.whole, line.stance) for line in reading.requirements] == [
        ("Five years of SQL", fit.Stance.MUST_HAVE),
        (item, fit.Stance.PREFERRED),
        ("Python", fit.Stance.MUST_HAVE),
        ("dbt", fit.Stance.MUST_HAVE),
    ]


@pytest.mark.parametrize(
    ("heading", "above", "stance"),
    [
        ("Skills and experience (optional)", "Requirements", fit.Stance.PREFERRED),
        ("Tools and platforms (required)", "Nice to have", fit.Stance.MUST_HAVE),
        ("Tools and platforms (must have)", "Nice to have", fit.Stance.MUST_HAVE),
        (_stance_lead("required", 12, ""), "Nice to have", fit.Stance.MUST_HAVE),
        (_stance_lead("must have", 12, ""), "Nice to have", fit.Stance.MUST_HAVE),
    ],
)
def test_a_closing_stance_aside_sets_the_stance_of_the_list_it_heads(
    heading: str, above: str, stance: fit.Stance, inventory: CvInventory
) -> None:
    reading = reading_of(
        f"Data Engineer\n\n{above}\n- Experience with Kafka\n\n{heading}\n- SQL\n- Python\n",
        inventory,
    )
    stances = {line.whole: line.stance for line in reading.requirements}
    assert (stances["SQL"], stances["Python"]) == (stance, stance)


# A line's own stance words set its stance, except in a bulleted duty list, where
# only a degree, a held credential or an experience floor is still asked for.
STANCE_MARKER_LINES = [
    pytest.param(
        "Requirements\n"
        "- Technical aptitude and a builder's instinct. Coding is not required. You have "
        "prototyped product workflows yourself.\n"
        "- Three years of product management experience.\n"
        "- An asset (not required): a master's degree in a related field.\n"
        "- Experience with SQL is an asset.\n"
        "- Experience managing digital assets for a media library.",
        (
            (
                fit.Stance.MUST_HAVE,
                "Technical aptitude and a builder's instinct. Coding is not required. You have "
                "prototyped product workflows yourself.",
            ),
            (fit.Stance.MUST_HAVE, "Three years of product management experience."),
            (
                fit.Stance.PREFERRED,
                "An asset (not required): a master's degree in a related field.",
            ),
            (fit.Stance.PREFERRED, "Experience with SQL is an asset."),
            (fit.Stance.MUST_HAVE, "Experience managing digital assets for a media library."),
        ),
        id="negated and asset markers",
    ),
    pytest.param(
        "Required Education & Experience\n\n"
        "- Bachelor's degree in business or a related field.\n"
        "- Strong preference for candidates with SaaS experience.\n"
        "- Nice to have: experience with usage-based pricing.",
        (
            (fit.Stance.MUST_HAVE, "Bachelor's degree in business or a related field."),
            (fit.Stance.PREFERRED, "Strong preference for candidates with SaaS experience."),
            (fit.Stance.PREFERRED, "Nice to have: experience with usage-based pricing."),
        ),
        id="a stated preference under a required heading",
    ),
    pytest.param(
        "Job Responsibilities\n"
        "- Leads site visits for the monitoring plan: reviews site steps\n"
        "Confirms that site staff completed training.\n"
        "Verifies required data in the case report form is complete.\n"
        "Reports findings to the study lead.",
        (),
        id="a stance word in a duty",
    ),
    pytest.param(
        "Responsibilities\n- Estimate the resources required for each project\n"
        "- Build the skills data foundations required for adoption",
        (),
        id="a stance word in a bulleted duty",
    ),
    pytest.param(
        "About the Role\nYou will own the reporting stack. Must have 3+ years of experience "
        "with SQL.",
        ((fit.Stance.MUST_HAVE, "Must have 3+ years of experience with SQL."),),
        id="a qualification stated in prose under a role heading",
    ),
    pytest.param(
        "Responsibilities\n- Lead the monthly planning review\n"
        "- Master's degree with at least 5 years of experience in the industry\n"
        "- Bachelor's degree required; advanced degree preferred.\n"
        "- Post-graduate qualification (PhD or MD preferred)",
        (
            (
                fit.Stance.MUST_HAVE,
                "Master's degree with at least 5 years of experience in the industry",
            ),
            (fit.Stance.MUST_HAVE, "Bachelor's degree required; advanced degree preferred."),
            (fit.Stance.PREFERRED, "Post-graduate qualification (PhD or MD preferred)"),
        ),
        id="credentials listed among bulleted duties",
    ),
    pytest.param(
        "Responsibilities\n- Lead the monthly planning review\n"
        "- Active Secret security clearance required\n"
        "- Valid driver's license required\n"
        "- Must be fluent in Spanish",
        (
            (fit.Stance.MUST_HAVE, "Active Secret security clearance required"),
            (fit.Stance.MUST_HAVE, "Valid driver's license required"),
            (fit.Stance.MUST_HAVE, "Must be fluent in Spanish"),
        ),
        id="held credentials listed among bulleted duties",
    ),
    pytest.param(
        "Responsibilities\n- Manage software license renewals as required\n"
        "- Keep documentation fluent and current as required",
        (),
        id="a credential word in a bulleted duty",
    ),
    pytest.param(
        "Requirements\n- Fluency in SQL\n- Preference to candidates who have led a migration",
        (
            (fit.Stance.MUST_HAVE, "Fluency in SQL"),
            (fit.Stance.PREFERRED, "Preference to candidates who have led a migration"),
        ),
        id="a preference given to some candidates",
    ),
    *(
        pytest.param(
            f"Requirements\n- Fluency in SQL\n\n{heading}\n"
            "- Stakeholder sign-off required for each release",
            ((fit.Stance.MUST_HAVE, "Fluency in SQL"),),
            id=f"a stance word in a bulleted duty under {heading!r}",
        )
        for heading in ("In your first 90 days:", "30/60/90 Day Plan:", "Your First Year:")
    ),
]


@pytest.mark.parametrize(("body", "expected"), STANCE_MARKER_LINES)
def test_a_stance_marker_in_a_line_reads_by_what_its_section_is(
    body: str, expected: tuple[tuple[fit.Stance, str], ...], inventory: CvInventory
) -> None:
    reading = reading_of("Data Engineer\n\n" + plain_layout(body), inventory)
    assert tuple((line.stance, line.text) for line in reading.requirements) == expected


@pytest.mark.parametrize(
    ("line", "percent"),
    [("- Up to 60% travel within the region", 60), ("- Travel up to 40% of the time", 40)],
)
def test_a_travel_share_under_working_conditions_is_read_as_travel(
    line: str, percent: int, inventory: CvInventory
) -> None:
    reading = fit.read(
        "Field Engineer\n\nRequirements\n- Four years of field service experience.\n\n"
        f"Working Conditions:\n{line}",
        inventory,
        parameters=TRAVEL_LIMIT,
        today=TODAY,
    )

    assert reading.travel_fit.value == f"{percent} per cent against your 25: over"
    assert [line.text for line in reading.requirements] == [
        "Four years of field service experience."
    ]


LONG_OPENING = (
    "Northwind plans regional rail timetables with depots, planners and operators across "
    "the network, and this role joins the planning analytics group that builds the models, "
    "reports and tools those teams rely on every week to schedule crews and rolling s"
    "tock safely, with close attention to cost, punctuality and the passenger experience"
)
TRAVEL_LIMIT = JobSearchParameters(arrangement=Arrangement(maximum_travel_percent=25))


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("- Some travel expected (<25%).", "below 25 per cent against your 25: inside"),
        ("- Travel under 20% of the time.", "below 20 per cent against your 25: inside"),
        ("- Minimal travel may be required (less than 30%).", None),
        ("- Travel below 40%.", None),
        ("- Travel up to 30%.", "30 per cent against your 25: over"),
    ],
)
def test_travel_stated_below_a_bound_is_over_only_when_it_says_so(
    line: str, expected: str | None, inventory: CvInventory
) -> None:
    reading = fit.read(
        f"Data Engineer\n\nRequirements\n{line}", inventory, parameters=TRAVEL_LIMIT, today=TODAY
    )

    assert reading.travel_fit.value == expected


_WIDE_TRAVEL_LIMIT = JobSearchParameters(arrangement=Arrangement(maximum_travel_percent=50))


@pytest.mark.parametrize(
    ("line", "reading", "at_25", "at_50"),
    [
        ("- Some overnight travel required - 25-50%", "50", "conflict", "inside"),
        ("- Some overnight travel of up to 25-50% may be required", "50", "conflict", "inside"),
        ("- Travel 25 - 50%", "50", "conflict", "inside"),
        ("- Travel 25% - 50%", "50", "conflict", "inside"),
        ("- Travel 25 to 50%", "50", "conflict", "inside"),
        ("- Travel 25% to 50%", "50", "conflict", "inside"),
        ("- Travel between 25 and 50%", "50", "conflict", "inside"),
        ("- Travel between 25% and 50%", "50", "conflict", "inside"),
        ("- Travel 25 \u2013 50% of the time", "50", "conflict", "inside"),
        ("- Travel: 0-25%", "25", "inside", "inside"),
        ("- 10-15% travel", "15", "inside", "inside"),
        ("- 25-50% travel to client sites", "50", "conflict", "inside"),
        ("- Travel 25-30% of the time to depots", "30", "conflict", "inside"),
        ("- 25 to 30% travel to customer sites", "30", "conflict", "inside"),
        ("- Up to 70% travel during launches", "70", "over", "over"),
        ("- Travel up to 75%", "75", "over", "over"),
        ("- Travel approximately 30% - up to 75%", "75", "over", "conflict"),
        ("- Travel approximately 30%", "30", "over", "inside"),
        ("- Travel less than 10%", "below 10", "inside", "inside"),
        ("- Travel 25% - 50% of the time", "50", "conflict", "inside"),
        ("- Travel 30% to 50% of time", "50", "over", "inside"),
        ("- Travel between 25% and 50% of the year", "50", "conflict", "inside"),
        ("- 20% - 30% in travel to customer sites", "30", "conflict", "inside"),
        ("- Travel up to 10% and 100% remote otherwise", "10", "inside", "inside"),
        ("- Travel 10% and 90% remote work", "10", "inside", "inside"),
        ("- Travel 5% and 95% from home", "5", "inside", "inside"),
        ("- Travel 20% and 30% overnight", "20", "inside", "inside"),
        ("- Travel: 10% - 100% remote", "10", "inside", "inside"),
        ("- Travel up to 15% - 100% remote role", "15", "inside", "inside"),
        ("- Travel 10% - 90% from home", "10", "inside", "inside"),
        ("- Travel up to 10% and 100% employer-paid health premiums", "10", "inside", "inside"),
        ("- Travel 10% and 20% bonus", "10", "inside", "inside"),
        ("- Travel between 10% and 20% bonus", "10", "inside", "inside"),
        ("- Travel up to 25% - 50% of which is international", "25", "inside", "inside"),
        ("- Travel up to 20% to 30% of client sites", "20", "inside", "inside"),
        ("- Travel 25% to 150%", "25", "inside", "inside"),
        ("- Travel 10% - 90% telework", "10", "inside", "inside"),
        ("- Travel 10% - 90% WFH", "10", "inside", "inside"),
        ("- Travel 10% - 90% work-from-home", "10", "inside", "inside"),
        ("- Travel 10% - 90% virtual", "10", "inside", "inside"),
        ("- Travel 10% to 90% telecommuting", "10", "inside", "inside"),
        ("- Travel up to 10% - 90% at home office", "10", "inside", "inside"),
        ("- Travel 10% - 90% distributed team", "10", "inside", "inside"),
        ("- Travel between 10% and 90% remote", "10", "inside", "inside"),
        ("- Travel 10% - 80% field", "10", "inside", "inside"),
        ("- Travel 10% - 100% paid", "10", "inside", "inside"),
        ("- Travel 10% - 100% covered by company", "10", "inside", "inside"),
        ("- Travel up to 10% to 100% of territory accounts", "10", "inside", "inside"),
        ("- Travel 10% to 75% of our clinics", "10", "inside", "inside"),
        ("- Travel 10% to 40% of customer sites", "10", "inside", "inside"),
        ("- Travel 10% - 50% of travel is international", "10", "inside", "inside"),
        ("- Travel up to 10%; 50% of travel is international", "10", "inside", "inside"),
        (
            "- Willingness to travel (approximately 30% of travel may be required).",
            "30",
            "over",
            "inside",
        ),
        ("- Approximately 30% of travel is required.", "30", "over", "inside"),
        ("- Travel 10% - 90% is remote work", "10", "inside", "inside"),
        ("- Travel 10% - 90% will be remote", "10", "inside", "inside"),
        ("- Travel 10% - 90% based from home", "10", "inside", "inside"),
        ("- Travel 10% - 90% may be remote", "10", "inside", "inside"),
        ("- Travel 10% - 75% expected in office", "10", "inside", "inside"),
        ("- Travel 10% - 100% (remote role)", "10", "inside", "inside"),
        ("- Travel 10%-90% remote", "10", "inside", "inside"),
        ("- Travel up to 10% - 100% remote, flexible", "10", "inside", "inside"),
        ("- Travel 10% - 90% remote and flexible", "10", "inside", "inside"),
        ("- Travel: 10% - 90% telework, 10% travel", "10", "inside", "inside"),
        ("- Travel up to 10% | 90% remote", "10", "inside", "inside"),
        ("- Travel 10% - 90% WFH with minimal other travel", "10", "inside", "inside"),
        ("- Travel up to 10% - 90% virtual, no international travel", "10", "inside", "inside"),
        ("- Travel 10% - 90% telework (occasional travel)", "10", "inside", "inside"),
        ("- Travel 10% to 40% of customer sites, local travel only", "10", "inside", "inside"),
        (
            "- Travel 10% - 100% paid by company, all travel booked centrally",
            "10",
            "inside",
            "inside",
        ),
        ("- Travel 10% - 75% of our clinics require travel", "10", "inside", "inside"),
        ("- Travel 10% - 90% home office with some regional travel", "10", "inside", "inside"),
        ("- Travel 10% - 90% of the time remote", "90", "conflict", "conflict"),
        ("- Travel 10% - 90% international", "90", "conflict", "conflict"),
        ("- 10% - 90% travel", "90", "conflict", "conflict"),
        ("- Travel 10% to 50% annually, remainder remote", "50", "conflict", "inside"),
        ("- Travel 10% - 60% or more", "60", "conflict", "conflict"),
        ("- Travel 25% - 50% may be required for launches", "50", "conflict", "inside"),
        ("- Travel 25% - 50% may involve international travel", "50", "conflict", "inside"),
        ("- Travel 25% - 50% depending on project", "50", "conflict", "inside"),
        ("- Travel 25% to 50% for client visits", "25", "inside", "inside"),
        ("- Travel 25-50% for client visits", "50", "conflict", "inside"),
        ("- Travel 25% - 50% overnight", "50", "conflict", "inside"),
        ("- Travel 25% - 50% domestic and international", "50", "conflict", "inside"),
        ("- Travel 25% to 50% travel", "50", "conflict", "inside"),
        ("- Travel 25% - 50% may be required", "50", "conflict", "inside"),
        ("- Travel 25% - 50% is expected", "50", "conflict", "inside"),
        ("- Travel 25% - 50% required", "50", "conflict", "inside"),
        ("- Travel 25% - 50% depending on territory", "50", "conflict", "inside"),
        ("- Travel 25% - 50% based on client needs", "50", "conflict", "inside"),
        ("- Travel 25% - 50% of your time", "50", "conflict", "inside"),
        ("- Travel 25% - 50% of the week", "50", "conflict", "inside"),
        ("- Travel 25% - 50% per year", "50", "conflict", "inside"),
        ("- Travel 25% - 50% annually", "50", "conflict", "inside"),
        ("- Travel 25% - 50%.", "50", "conflict", "inside"),
        ("- Travel (25% - 50%)", "50", "conflict", "inside"),
        ("- Travel 25% - 50%, with some weekends", "50", "conflict", "inside"),
        ("- Travel 25% - 50% and some weekends", "50", "conflict", "inside"),
    ],
)
def test_a_travel_share_or_range_is_compared_against_the_limit(
    line: str, reading: str, at_25: str, at_50: str, inventory: CvInventory
) -> None:
    posting = f"Account Manager\n\nRequirements\n- Fluency in SQL\n{line}"
    values = [
        fit.read(posting, inventory, parameters=limit, today=TODAY).travel_fit.value
        for limit in (TRAVEL_LIMIT, _WIDE_TRAVEL_LIMIT)
    ]

    assert values == [
        None if at_25 == "conflict" else f"{reading} per cent against your 25: {at_25}",
        None if at_50 == "conflict" else f"{reading} per cent against your 50: {at_50}",
    ]


@pytest.mark.parametrize(
    "line",
    sorted(
        {
            f"- {line}"
            for low in (10, 25)
            for high in (30, 50)
            for join in ("-", " to ", " and ")
            for sign in ("", "%")
            for line in (
                f"Travel {'between ' if join == ' and ' else ''}{low}{sign}{join}{high}%",
                f"{'Between ' if join == ' and ' else ''}{low}{sign}{join}{high}% "
                "travel may be required",
            )
        }
    ),
)
def test_a_travel_range_crossing_the_limit_is_held_for_review(line: str) -> None:
    posting = f"Data Analyst\n\nRequirements:\n{line}"
    reading = fit.read(posting, parameters=TRAVEL_LIMIT, today=TODAY)
    gate = {item.name: item for item in fit.gates(reading, TRAVEL_LIMIT)}["travel"]

    assert reading.travel_fit.value is None
    assert reading.travel_fit.conflict
    assert gate.standing is fit.Standing.UNKNOWN
    assert reading.travel_fit.evidence == (line,)


@pytest.mark.parametrize(
    "body",
    [
        "- Travel 30-50% may be required",
        "- Travel between 30% and 50%",
        "- 30 to 50% travel",
        "- 50% travel may be required",
        "- 25-30% travel may be required\n- Travel 50% is required",
    ],
)
def test_travel_wholly_above_the_limit_still_refuses_the_posting(body: str) -> None:
    reading = fit.read(f"Data Analyst\n\nRequirements:\n{body}", parameters=TRAVEL_LIMIT)
    gate = {item.name: item for item in fit.gates(reading, TRAVEL_LIMIT)}["travel"]

    assert reading.travel_fit.value == "50 per cent against your 25: over"
    assert not reading.travel_fit.conflict
    assert gate.standing is fit.Standing.INELIGIBLE


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "broad",
    [
        "- Travel 10-75% may be required",
        "- Travel 10-90% may be required",
        "- Travel less than 90%",
    ],
)
@pytest.mark.parametrize(
    ("mandatory", "high"),
    [
        ("- Travel 30% is required", 30),
        ("- Travel 50% is required", 50),
        ("- Travel 30-50% is required", 50),
    ],
)
def test_definite_over_limit_travel_precedes_an_ambiguous_broader_statement(
    reverse: bool, broad: str, mandatory: str, high: int
) -> None:
    lines = (mandatory, broad) if reverse else (broad, mandatory)
    posting = "Data Analyst\n\nRequirements:\n" + "\n".join(lines)
    reading = fit.read(posting, parameters=TRAVEL_LIMIT, today=TODAY)
    gate = {item.name: item for item in fit.gates(reading, TRAVEL_LIMIT)}["travel"]

    assert reading.travel_fit.value == f"{high} per cent against your 25: over"
    assert not reading.travel_fit.conflict
    assert reading.travel_fit.evidence == (mandatory,)
    assert gate.standing is fit.Standing.INELIGIBLE


@pytest.mark.parametrize(
    ("body", "conflict"),
    [
        ("Requirements:\n- Travel 10-90% may be required", True),
        ("Requirements:\n- Travel less than 90%", False),
        ("Requirements:\n- Travel 10-90% may be required\n- Travel less than 50%", True),
        (
            "About the role\nTravel 50% is required\n\nRequirements:\n- Travel 10% is required",
            True,
        ),
    ],
)
def test_ambiguous_travel_or_a_requirement_context_dispute_still_needs_review(
    body: str, conflict: bool
) -> None:
    reading = fit.read(f"Data Analyst\n\n{body}", parameters=TRAVEL_LIMIT, today=TODAY)
    gate = {item.name: item for item in fit.gates(reading, TRAVEL_LIMIT)}["travel"]

    assert reading.travel_fit.value is None
    assert reading.travel_fit.conflict is conflict
    assert reading.travel_fit.evidence
    assert gate.standing is fit.Standing.UNKNOWN


def test_a_travel_quote_on_a_long_line_shows_the_share(inventory: CvInventory) -> None:
    posting = f"Data Engineer\n\nAbout the role\n{LONG_OPENING}; Travel up to 60% of the time."
    reading = fit.read(posting, inventory, parameters=TRAVEL_LIMIT, today=TODAY)
    assert reading.travel_fit.value == "60 per cent against your 25: over"
    assert "60%" in reading.travel_fit.evidence[0]


def test_a_sponsorship_quote_on_a_long_line_shows_the_phrase(inventory: CvInventory) -> None:
    posting = f"Data Engineer\n\nAbout the role\n{LONG_OPENING}; Northwind will not sponsor visas."
    reading = reading_of(posting, inventory)
    assert reading.authorization.value == "sponsorship is not offered"
    assert "will not sponsor" in reading.authorization.evidence[0]


def test_a_linkedin_tag_quote_on_a_long_line_shows_the_tag(inventory: CvInventory) -> None:
    posting = f"Data Engineer\n\nAbout the role\n{LONG_OPENING} #LI-Remote"
    location = reading_of(posting, inventory).location
    assert location.value == "remote"
    assert "#LI-Remote" in location.evidence[0]


@pytest.mark.parametrize(
    ("heading", "perk"),
    [
        ("Benefits", "- 100% remote with an annual team trip; travel covered by us"),
        ("Benefits", "- 100% remote, travel stipend included"),
        ("Benefits", "- 100% paid travel insurance"),
        ("Perks", "- We reimburse 100% of travel to our annual offsite"),
    ],
)
def test_a_perk_beside_travel_is_no_travel_share(
    heading: str, perk: str, inventory: CvInventory
) -> None:
    posting = f"Data Engineer\n\nRequirements\n- Fluency in SQL\n\n{heading}\n{perk}"
    reading = fit.read(posting, inventory, parameters=TRAVEL_LIMIT, today=TODAY)
    assert reading.travel_fit.value is None
    assert reading.travel_fit.note == "the posting states no travel share to compare"


_COST_SHARES = (
    "Travel expenses are reimbursed at {}",
    "Travel costs are covered at {}",
    "Travel is reimbursed at {}",
    "We reimburse {} of travel",
    "{} of travel expenses are reimbursed",
    "{} travel reimbursement",
    "A travel stipend covers {} of fares",
    "Travel per diem paid at {}",
    "Commission of up to {} on travel bookings",
    "Bonus of {} tied to travel sales",
    "{} travel allowance",
    "Travel insurance at {} of cost",
)
_COST_FIGURES = ("100%", "100 %", "100 percent", "100 per cent", "80%")


@pytest.mark.parametrize(
    ("share", "figure", "heading"),
    list(itertools.product(_COST_SHARES, _COST_FIGURES, ("About the role", "Working Conditions"))),
)
def test_a_percentage_of_travel_costs_or_pay_is_no_travel_share(
    share: str, figure: str, heading: str, inventory: CvInventory
) -> None:
    line = share.format(figure)
    posting = f"Field Engineer\n\nRequirements\n- Fluency in SQL\n\n{heading}:\n- {line}."
    reading = fit.read(posting, inventory, parameters=TRAVEL_LIMIT, today=TODAY)
    assert reading.travel_fit.value is None
    assert reading.travel_fit.note == "the posting states no travel share to compare"


@pytest.mark.parametrize(
    "line",
    [
        "Travel up to 60% of the time; expenses reimbursed",
        "Up to 60% travel, with a travel stipend",
        "60% travel, all costs covered",
        "Commission-based role with travel up to 60%",
        "Travel up to 60 percent, reimbursed per policy",
    ],
)
def test_a_travel_share_beside_a_cost_word_is_still_read(line: str, inventory: CvInventory) -> None:
    posting = f"Field Engineer\n\nRequirements\n- Fluency in SQL\n\nWorking Conditions:\n- {line}"
    reading = fit.read(posting, inventory, parameters=TRAVEL_LIMIT, today=TODAY)
    assert reading.travel_fit.value == "60 per cent against your 25: over"


@pytest.mark.parametrize(
    ("line", "stances"),
    [
        ("Nursing license would be an advantage", [fit.Stance.PREFERRED]),
        ("Nursing license is a big plus", [fit.Stance.PREFERRED]),
        ("Nursing license would be beneficial", [fit.Stance.PREFERRED]),
        ("Nursing license not required but preferred", [fit.Stance.PREFERRED]),
        ("Preference given to applicants with a nursing license", [fit.Stance.PREFERRED]),
        ("Nursing license isn't required", []),
        ("Nursing license is not a requirement", []),
        ("No degree required", []),
        ("No degree is required", []),
        ("Nursing license is optional", []),
        ("Bachelor's degree required; a master's is not required", [fit.Stance.MUST_HAVE]),
        ("No less than 5 years of scheduling experience required", [fit.Stance.MUST_HAVE]),
        ("Build mutually beneficial partnerships with clinics", [fit.Stance.MUST_HAVE]),
    ],
)
def test_a_preference_or_a_waiver_under_a_requirement_heading_asks_for_no_must_have(
    line: str, stances: list[fit.Stance], inventory: CvInventory
) -> None:
    reading = reading_of(f"Clinic Coordinator\n\nRequirements:\n- {line}", inventory)
    assert [requirement.stance for requirement in reading.requirements] == stances


_NEGATED_REMOTE = (
    "This role is not remote.",
    "This position is not remote.",
    "The job isn't remote.",
    "The job isn\u2019t remote.",
    "This role is not fully remote.",
    "This is not a remote role.",
    "This is a non-remote role.",
    "Remote work isn't available for this role.",
    "Remote work is not available for this role.",
    "Remote: No.",
    "Remote eligible: No.",
)
_BESIDE_NEGATED_REMOTE = (("", None), (" The role is on-site in Riverbend.", "on site"))


@pytest.mark.parametrize(
    ("sentence", "beside"), list(itertools.product(_NEGATED_REMOTE, _BESIDE_NEGATED_REMOTE))
)
def test_negated_remote_never_answers_remote(
    sentence: str, beside: tuple[str, str | None], inventory: CvInventory
) -> None:
    extra, expected = beside
    posting = (
        f"Data Analyst\n\nAbout the role\n{sentence}{extra}\n\nRequirements:\n- SQL experience"
    )
    location = reading_of(posting, inventory).location
    assert location.value == expected
    assert not location.conflict


@pytest.mark.parametrize(
    "sentence",
    ["This role is fully remote, no travel required.", "Remote, no office visits.", "Remote."],
)
def test_remote_beside_a_later_no_still_answers_remote(
    sentence: str, inventory: CvInventory
) -> None:
    posting = f"Data Analyst\n\nAbout the role\n{sentence}\n\nRequirements:\n- SQL experience"
    assert reading_of(posting, inventory).location.value == "remote"


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("#LI-Remote", "remote"),
        ("#LI_Remote", "remote"),
        ("#LIRemote", "remote"),
        ("#LI-Hybrid", "hybrid"),
        ("#LI_Hybrid", "hybrid"),
        ("#LI-Onsite", "on site"),
        ("#LI-On-site", "on site"),
        ("#LI_Onsite", "on site"),
        ("# LI-Remote", "remote"),
        ("#LI-DNI", "hybrid"),
    ],
)
def test_every_linkedin_arrangement_tag_spelling_is_read(
    tag: str, expected: str | None, inventory: CvInventory
) -> None:
    posting = (
        f"Data Analyst\n\nAbout the role\nNorthwind runs a hybrid work model. {tag}\n\n"
        "Requirements:\n- SQL"
    )
    assert reading_of(posting, inventory).location.value == expected


@pytest.mark.parametrize(
    ("wish", "separator"),
    list(
        itertools.product(
            ("3+ years is preferred", "3+ years preferred", "ideally 3+ years", "3+ years a plus"),
            ("; ", ". "),
        )
    ),
)
def test_a_wished_figure_beside_a_required_one_leaves_the_required_floor(
    wish: str, separator: str, inventory: CvInventory
) -> None:
    body = (
        "Experience / Qualifications\n- Bachelor's degree in Statistics or a related field.\n"
        "- At least 1 year of experience in healthcare analytics or a related analytical "
        f"discipline{separator}{wish[0].upper() if separator == '. ' else wish[0]}{wish[1:]}."
    )
    assert reading_of(f"Data Analyst\n\n{body}", inventory).years.value == 1


@pytest.mark.parametrize("heading", ["What you should have:", "What You Should Have"])
def test_years_under_a_should_have_heading_beside_a_required_degree_set_the_floor(
    heading: str, inventory: CvInventory
) -> None:
    body = (
        f"{heading}\n- Degree in life sciences, pharmacy, or related discipline required ; "
        "advanced degree such as PhD, PharmD, or equivalent experience preferred\n"
        "- 6+ years of publications, medical affairs, or medical communications experience "
        "within an agency, pharmaceutical, biotech, academic, or related environment"
    )
    reading = reading_of(f"Medical Writer\n\n{body}", inventory)
    assert reading.years.value == 6
    assert [line.stance for line in reading.requirements] == [fit.Stance.MUST_HAVE] * 2


@pytest.mark.parametrize("figure", ["three", "Three"])
def test_a_possessive_minimum_beside_an_asset_line_sets_the_floor(
    figure: str, inventory: CvInventory
) -> None:
    body = (
        f"Requirements\n- Minimum {figure} years' clinical practice experience and a "
        "license to practice in the province with its College.\n"
        "- Extensive knowledge of pharmaceuticals.\n"
        "- Knowledge in pharmaco-economic assessment would be an asset."
    )
    assert reading_of(f"Clinical Pharmacist\n\n{body}", inventory).years.value == 3


_LADDER_DEGREES = ("Associate's degree", "Associate degree", "Associates degree")
_NO_DEGREE_RUNGS = (
    "High school diploma / GED and 12 years of scientific experience",
    "High school diploma and 12 years of scientific experience",
    "GED and 12 years of scientific experience",
    "An equivalent combination of education and experience totaling 12 years of scientific "
    "experience",
)


def _ladder_rungs(associate: str, last: str) -> tuple[str, ...]:
    return (
        "Master's degree and 6 years of scientific experience",
        "Bachelor's degree and 8 years of scientific experience",
        f"{associate} and 10 years of scientific experience",
        last,
    )


_LADDER_LAYOUTS = {
    "one paragraph": " OR ".join,
    "bullets ending in or": lambda rungs: (
        "\n".join(f"- {rung} OR" for rung in rungs[:-1]) + f"\n- {rungs[-1]}"
    ),
    "or bullets": lambda rungs: "\n- OR\n".join(f"- {rung}" for rung in rungs),
    "or lines": lambda rungs: "\nOR\n".join(f"- {rung}" for rung in rungs),
    "a bullet rung, an or bullet, then paragraphs": lambda rungs: (
        f"- {rungs[0]}\n- OR\n\n" + "\n\nOR\n\n".join(rungs[1:])
    ),
}


@pytest.mark.parametrize(
    ("associate", "last", "layout"),
    list(itertools.product(_LADDER_DEGREES, _NO_DEGREE_RUNGS, _LADDER_LAYOUTS)),
)
def test_a_ladder_with_a_path_needing_no_degree_answers_equivalent_experience(
    associate: str, last: str, layout: str, inventory: CvInventory
) -> None:
    rungs = _ladder_rungs(associate, last)
    body = f"Basic Qualifications:\n{_LADDER_LAYOUTS[layout](rungs)}"
    reading = reading_of(f"Research Scientist\n\n{body}", inventory)
    assert reading.education.value == "associate degree, or equivalent experience"
    assert reading.years.value == 6
    wholes = " ".join(line.whole for line in reading.requirements)
    assert all(rung in wholes for rung in rungs)


@pytest.mark.parametrize("layout", _LADDER_LAYOUTS)
def test_a_ladder_of_degrees_only_answers_its_lowest_degree_as_required(
    layout: str, inventory: CvInventory
) -> None:
    rungs = _ladder_rungs("Associate degree", "Doctorate and 2 years of scientific experience")
    body = f"Basic Qualifications:\n{_LADDER_LAYOUTS[layout](rungs)}"
    reading = reading_of(f"Research Scientist\n\n{body}", inventory)
    assert reading.education.value == "associate degree, stated as must-have"
    assert reading.years.value == 2


def test_a_diploma_named_on_its_own_line_leaves_a_degree_required(inventory: CvInventory) -> None:
    body = "Requirements:\n- Bachelor's degree in chemistry\n- High school diploma on file"
    reading = reading_of(f"Research Scientist\n\n{body}", inventory)
    assert reading.education.value == "bachelor's degree, stated as must-have"


@pytest.mark.parametrize(
    ("aside", "must", "heading"),
    list(
        itertools.product(
            (
                "(PharmD preferred)",
                "(Doctorate preferred)",
                "(PharmD/Doctorate preferred)",
                "(Master's a plus)",
                "(PhD desirable)",
            ),
            ("Required", "required", "is required", "- Mandatory"),
            ("Requirements:", "About the role:"),
        )
    ),
)
def test_a_preference_aside_leaves_a_required_line_must_have(
    aside: str, must: str, heading: str, inventory: CvInventory
) -> None:
    line = f"- Bachelor's Degree in Pharmacy {aside} {must}"
    reading = reading_of(f"Clinical Pharmacist\n\n{heading}\n{line}", inventory)
    assert [requirement.stance for requirement in reading.requirements] == [fit.Stance.MUST_HAVE]
    assert reading.education.value == "bachelor's degree, stated as must-have"


@pytest.mark.parametrize("must", ["is required", "Required", "required"])
@pytest.mark.parametrize("aside", ["(5+ years is preferred)", "(ideally 5+ years)"])
def test_a_years_figure_in_a_preference_aside_leaves_the_required_floor(
    must: str, aside: str, inventory: CvInventory
) -> None:
    line = f"- Minimum 3 years of experience {aside} in analytics roles {must}"
    reading = reading_of(f"Data Analyst\n\nRequirements:\n{line}", inventory)
    assert [requirement.stance for requirement in reading.requirements] == [fit.Stance.MUST_HAVE]
    assert reading.years.value == 3


def test_a_required_clause_before_a_semicolon_wish_keeps_the_line_must_have(
    inventory: CvInventory,
) -> None:
    reading = reading_of(
        "Data Analyst\n\nRequirements:\n- SQL required; Python preferred", inventory
    )
    assert [requirement.stance for requirement in reading.requirements] == [fit.Stance.MUST_HAVE]


@pytest.mark.parametrize(
    "line",
    [
        "- Bachelor's Degree in Pharmacy (PharmD preferred)",
        "- Python (preferred)",
        "- Python required (preferred)",
        "- SQL required (strongly preferred)",
        "- Dashboards required (a plus)",
        "- Dashboards required (nice to have)",
        "- Proficiency in a programming language (Python preferred)",
    ],
)
def test_a_preference_aside_with_no_must_marker_leaves_a_line_preferred(
    line: str, inventory: CvInventory
) -> None:
    reading = reading_of(f"Clinical Pharmacist\n\nRequirements:\n{line}", inventory)
    assert [requirement.stance for requirement in reading.requirements] == [fit.Stance.PREFERRED]


_WAIVER_BESIDE_A_REQUIREMENT = [
    pytest.param("3+ years of customer service experience, no degree required", 3, id="no degree"),
    pytest.param("5+ years of Python experience (Django optional)", 5, id="optional aside"),
    pytest.param(
        "4+ years of experience in logistics; no relocation needed", 4, id="no relocation"
    ),
    pytest.param("5 years of experience configuring optional SAP modules", 5, id="optional noun"),
    pytest.param("5+ years of experience in field sales; no travel needed", 5, id="no travel"),
    pytest.param("2+ years of retail experience, no degree needed", 2, id="no degree needed"),
    pytest.param("5+ years of experience required; Django optional", 5, id="must then optional"),
]


@pytest.mark.parametrize(("line", "years"), _WAIVER_BESIDE_A_REQUIREMENT)
def test_a_waiver_leaves_the_requirement_beside_it_on_the_line(
    line: str, years: int, inventory: CvInventory
) -> None:
    reading = reading_of(f"Data Analyst\n\nRequirements:\n- {line}\n- SQL", inventory)
    assert [requirement.stance for requirement in reading.requirements] == [
        fit.Stance.MUST_HAVE
    ] * 2
    assert reading.years.value == years


@pytest.mark.parametrize(
    "line",
    [
        "Bachelor's degree in nursing; wound care certification optional",
        "Bachelor's degree in nursing, CFA optional",
        "Bachelor's degree in nursing; no relocation needed",
    ],
)
def test_a_waiver_leaves_the_degree_beside_it_required(line: str, inventory: CvInventory) -> None:
    reading = reading_of(f"Staff Nurse\n\nRequirements:\n- {line}\n- SQL", inventory)
    assert reading.education.value == "bachelor's degree, stated as must-have"


@pytest.mark.parametrize(
    ("line", "tail"),
    list(
        itertools.product(
            (
                "5+ years of experience deciding what is beneficial to patients",
                "5+ years of experience building tools that are beneficial to clinicians",
            ),
            ("", "; SQL required", ". SQL required", ", SQL required", " (SQL required)"),
        )
    ),
)
def test_beneficial_to_someone_is_no_preference_anywhere_on_the_line(
    line: str, tail: str, inventory: CvInventory
) -> None:
    reading = reading_of(f"Data Analyst\n\nRequirements:\n- {line}{tail}\n- SQL", inventory)
    assert reading.requirements[0].stance is fit.Stance.MUST_HAVE
    assert reading.years.value == 5


_CLAUSE_JOINS = ("; ", ". ")
# A sentence goes on in lowercase, so the article after ". " is capitalised.
_ARTICLE_JOINS = ("; a", ". A")
_CUE_FORMS = ("would be beneficial", "is a plus", "is preferred", "would be an advantage")


@pytest.mark.parametrize(
    ("requirement", "years", "join", "cue"),
    [
        (requirement, years, join, cue)
        for requirement, years in (("5+ years of experience", 5), ("Experience with Tableau", None))
        for join, cue in itertools.product(_ARTICLE_JOINS, _CUE_FORMS)
    ],
)
def test_a_clause_with_no_cue_keeps_the_requirement_heading_stance(
    requirement: str, years: int | None, join: str, cue: str, inventory: CvInventory
) -> None:
    reading = reading_of(
        f"Data Analyst\n\nRequirements:\n- {requirement}{join} CPA {cue}\n- SQL", inventory
    )
    assert reading.requirements[0].stance is fit.Stance.MUST_HAVE
    assert reading.years.value == years


def test_a_skill_beside_a_benefit_clause_stays_must_have(inventory: CvInventory) -> None:
    line = (
        "Skill using an applicant tracking system to schedule candidates; Greenhouse experience "
        "is beneficial but not required."
    )
    reading = reading_of(f"Recruiting Coordinator\n\nRequirements:\n- {line}\n- SQL", inventory)
    assert reading.requirements[0].stance is fit.Stance.MUST_HAVE


@pytest.mark.parametrize(("join", "cue"), list(itertools.product(_CLAUSE_JOINS, _CUE_FORMS)))
def test_years_in_the_cued_clause_of_a_must_have_line_set_no_floor(
    join: str, cue: str, inventory: CvInventory
) -> None:
    reading = reading_of(
        f"Data Analyst\n\nRequirements:\n- Experience with Tableau{join}3+ years in "
        f"healthcare {cue}\n- SQL",
        inventory,
    )
    assert reading.requirements[0].stance is fit.Stance.MUST_HAVE
    assert reading.years.value is None


@pytest.mark.parametrize("cue", _CUE_FORMS)
def test_a_line_whose_only_clause_is_cued_stays_preferred(cue: str, inventory: CvInventory) -> None:
    reading = reading_of(
        f"Data Analyst\n\nRequirements:\n- 5+ years of experience in healthcare {cue}\n- SQL",
        inventory,
    )
    assert reading.requirements[0].stance is fit.Stance.PREFERRED
    assert reading.years.value is None


@pytest.mark.parametrize(("join", "cue"), list(itertools.product(_ARTICLE_JOINS, _CUE_FORMS)))
def test_a_mixed_line_under_a_preferred_heading_stays_preferred(
    join: str, cue: str, inventory: CvInventory
) -> None:
    reading = reading_of(
        f"Data Analyst\n\nNice to have:\n- 5+ years of experience{join} CPA {cue}", inventory
    )
    assert [line.stance for line in reading.requirements] == [fit.Stance.PREFERRED]


@pytest.mark.parametrize("rank", ["I", "II", "III", "Level I"])
def test_a_title_ending_in_a_roman_numeral_ends_its_sentence(
    rank: str, inventory: CvInventory
) -> None:
    requirement = "Candidates must have 2+ years of clinic experience."
    reading = reading_of(
        f"Clinical Associate\n\nWe are hiring a Clinical Associate {rank}. {requirement}\n",
        inventory,
    )
    assert [line.whole for line in reading.requirements] == [requirement]


def _run_on(text: str, inventory: CvInventory) -> fit.Reading:
    return reading_of(f"Planning Analyst\n\nAbout the role\n{text}\n", inventory)


def test_a_label_run_into_a_unit_opens_a_requirement_section(inventory: CvInventory) -> None:
    reading = _run_on(
        "Mandatory skills and qualifications : 10-14 years of relevant experience in records "
        "governance Expertise in at least one of records quality or retention scheduling etc. "
        "Good experience presenting results to executives is preferred.",
        inventory,
    )
    assert reading.years.value == 10
    assert [line.stance for line in reading.requirements] == [
        fit.Stance.MUST_HAVE,
        fit.Stance.PREFERRED,
    ]


def test_basic_and_preferred_labels_run_in_keep_their_own_stances(inventory: CvInventory) -> None:
    reading = _run_on(
        "Basic Qualifications: Acme requires that all applicants be enrolled at an accredited "
        "university and complete the first year of an MBA OR Master's OR Ph.D. program before the "
        "internship starts Preferred Qualifications: In the process of completing the first year "
        "of a Master's program in supply chain Experience with forecasting tools is a plus",
        inventory,
    )
    assert reading.education.value == "master's degree, stated as must-have"
    assert [line.stance for line in reading.requirements] == [
        fit.Stance.MUST_HAVE,
        fit.Stance.PREFERRED,
    ]


def test_a_required_word_under_a_run_in_preferred_label_stays_preferred(
    inventory: CvInventory,
) -> None:
    reading = _run_on(
        "Required Qualifications : High School Diploma PLUS a minimum 6 years OR Associate's "
        "Degree PLUS a minimum 4 years OR Bachelor's Degree PLUS a minimum 2 years of work "
        "experience in retail Preferred Qualifications: Bachelor's degree in logistics or business "
        "Familiarity with scheduling tools (i . e. , Planly) For degrees earned outside the "
        "country, a degree that satisfies the requirements of 12 A.B.C. 100.1(a) is required.",
        inventory,
    )
    assert reading.years.value == 2
    assert reading.education.value == "associate degree, or equivalent experience"
    assert [line.stance for line in reading.requirements] == [
        fit.Stance.MUST_HAVE,
        fit.Stance.PREFERRED,
    ]


@pytest.mark.parametrize(
    ("text", "years"),
    [
        (
            "Maintain the stock ledger Qualifications: Minimum Required: Bachelors and/or a "
            "combination of related experience Other Required: 6+ years' experience for a Sr. "
            "Inventory Planner and 9+ years for a Lead Inventory Planner Proficiency in "
            "spreadsheets #LI-REMOTE Any data provided as part of this application will be stored "
            "in accordance with our Privacy Policy.",
            6,
        ),
        (
            "Qualifications: Bachelor's degree in Statistics or a related field; 1-3 years of "
            "professional experience in client service is required; Basic understanding of "
            "statistical analysis. Acme is an equal opportunity employer and values a diverse "
            "workforce.",
            1,
        ),
    ],
)
def test_a_closing_legal_statement_is_cut_from_the_requirements_before_it(
    text: str, years: int, inventory: CvInventory
) -> None:
    reading = _run_on(text, inventory)
    assert reading.years.value == years
    assert reading.requirements
    assert all(line.stance is fit.Stance.MUST_HAVE for line in reading.requirements)
    assert not any(
        "privacy" in line.whole.casefold() or "equal opportunity" in line.whole.casefold()
        for line in reading.requirements
    )


def test_a_heading_phrase_run_in_opens_a_requirement_section(inventory: CvInventory) -> None:
    reading = _run_on(
        "Assist the Sr. Ledger Analyst with month-end close What we are looking for 1-3 years of "
        "experience in general bookkeeping Team player able to support timely reporting",
        inventory,
    )
    assert reading.years.value == 1
    assert [line.whole for line in reading.requirements] == [
        "1-3 years of experience in general bookkeeping Team player able to support timely "
        "reporting"
    ]
    assert reading.requirements[0].stance is fit.Stance.MUST_HAVE


@pytest.mark.parametrize(
    "text",
    [
        " \u00b7 ".join(
            [
                "Qualifications",
                "Bachelor's degree in Statistics or a related field",
                "1-3 years of professional experience in client service",
                "Familiarity with our planning tools (e.g. Toolbox, Ledgerly)",
                "Basic understanding of statistical analysis. Acme is an equal opportunity "
                "employer and values a diverse workforce.",
            ]
        ),
        "Assist the Sr. Ledger Analyst with month-end close What we are looking for 1-3 years of "
        "experience in general bookkeeping Team player able to support timely reporting for the "
        "Sr. Ledger Analyst and Controller Knowledge of payroll software is an asset",
    ],
)
def test_a_run_on_unit_keeps_its_years_floor(text: str, inventory: CvInventory) -> None:
    assert _run_on(text, inventory).years.value == 1


_GLYPH_ITEMS = (
    "Bachelor's degree in Statistics or a related field",
    "1-3 years of professional experience in client service",
    "Familiarity with our planning tools (e.g. Toolbox, Ledgerly)",
    "Basic understanding of statistical analysis.",
)


@pytest.mark.parametrize(
    ("glyph", "label", "lead"),
    list(
        itertools.product(
            ("·", "•", "▪", "‣", "●"),
            [
                ("Qualifications", fit.Stance.MUST_HAVE),
                ("Requirements", fit.Stance.MUST_HAVE),
                ("Preferred Qualifications", fit.Stance.PREFERRED),
            ],
            ("", "Role & Responsibilities {g} Maintain client accounts {g} "),
        )
    ),
)
def test_a_label_run_in_before_a_glyph_opens_a_section_of_its_items(
    glyph: str, label: tuple[str, fit.Stance], lead: str, inventory: CvInventory
) -> None:
    name, stance = label
    items = f" {glyph} ".join((name, *_GLYPH_ITEMS))
    reading = _run_on(
        f"{lead.format(g=glyph)}{items} Acme is committed to ensuring equal opportunity between "
        "women and men, integrating it as a strategic principle in its human resources policies.",
        inventory,
    )
    assert [(line.whole, line.stance) for line in reading.requirements] == [
        (item, stance) for item in _GLYPH_ITEMS
    ]
    assert reading.years.value == 1


def test_a_glyph_label_over_one_item_opens_no_section(inventory: CvInventory) -> None:
    reading = reading_of(
        "Data Analyst\nToronto · Qualifications · Hybrid\n\nRequirements\n- SQL\n", inventory
    )
    assert [line.whole for line in reading.requirements] == ["SQL"]


def _facilities(*sections: str) -> str:
    return "Facilities Coordinator\n\n" + "\n\n".join(sections) + "\n"


@pytest.mark.parametrize("label", ["Regulatory Requirements", "Key Qualifications"])
def test_a_label_inside_a_bullet_leaves_the_bullets_after_it_duties(
    label: str, inventory: CvInventory
) -> None:
    reading = reading_of(
        _facilities(
            f"Responsibilities\n- Ensure compliance with {label}: fire code, ADA\n"
            "- Schedule vendor visits\n- Keep records",
            "Requirements\n- 3+ years of facilities experience",
        ),
        inventory,
    )
    assert [line.whole for line in reading.requirements] == ["3+ years of facilities experience"]


def test_a_label_cut_from_a_paragraph_heads_only_that_paragraph(inventory: CvInventory) -> None:
    reading = reading_of(
        _facilities(
            "Responsibilities\nKeep the program current. Key Requirements: fire code, ADA\n\n"
            "Schedule vendor visits and keep records.",
            "Requirements\n- 3+ years of facilities experience",
        ),
        inventory,
    )
    assert [line.whole for line in reading.requirements] == [
        "fire code, ADA",
        "3+ years of facilities experience",
    ]


@pytest.mark.parametrize(
    "closing",
    [
        "Equal opportunity employer reporting is part of the job",
        "Privacy notice drafting for our website is part of the job",
    ],
)
def test_legal_words_inside_a_bullet_cut_no_requirement_after_it(
    closing: str, inventory: CvInventory
) -> None:
    reading = reading_of(
        _facilities(f"Requirements\n- 4+ years of HR experience. {closing}\n- SQL\n- Python"),
        inventory,
    )
    stances = {line.whole: line.stance for line in reading.requirements}
    assert stances["SQL"] is stances["Python"] is fit.Stance.MUST_HAVE


@pytest.mark.parametrize(
    ("thing", "end", "cue"),
    list(
        itertools.product(
            ("7+ years of experience in analytics", "Master's degree in statistics"),
            (".", " (analytics, BI, etc.)", " (M.S.)", ", etc."),
            ("Preferred", "Preferred.", "a plus", "Nice to have", "This is preferred"),
        )
    ),
)
def test_a_bare_cue_after_a_stop_is_the_line_s_own_stance(
    thing: str, end: str, cue: str, inventory: CvInventory
) -> None:
    reading = reading_of(f"Data Analyst\n\nRequirements:\n- {thing}{end} {cue}\n- SQL\n", inventory)
    assert reading.requirements[0].stance is fit.Stance.PREFERRED
    assert reading.years.value is None
    assert reading.education.value in (None, "master's degree, stated as preferred")


@pytest.mark.parametrize("body", ["This role is on-site in Springfield", "This role is remote"])
@pytest.mark.parametrize("tag", ["#LI-Hybrid", "#LI-Onsite", "#LI-AB1 #LI-Hybrid"])
@pytest.mark.parametrize(
    "after", ["Benefits include a 401(k)", "We offer paid time off", "Acme offers a 401(k)"]
)
def test_a_tag_run_into_benefits_text_does_not_contradict_the_body(
    body: str, tag: str, after: str, inventory: CvInventory
) -> None:
    reading = reading_of(f"Data Analyst\n\nAbout the role\n{body}. {tag} {after}\n", inventory)
    assert reading.location.value == ("on site" if "on-site" in body else "remote")


_CLOSING_PARAGRAPHS_AFTER_A_LIST = [
    (
        "Office Working Requirements\n- Work from our Springfield office three days a week\n\n"
        "The annual base pay ranges from $90,000 to $110,000, based on skills and experience. "
        "Benefits include a retirement plan.\nDate Posted\n15-Sep-2026\n"
        "Acme is an equal opportunity employer.\n#LI-Hybrid",
        [("Work from our Springfield office three days a week", fit.Stance.MUST_HAVE)],
    ),
    (
        "Preferred Requirements\n- Experience leading in a team environment.\n"
        "- Demonstrated problem-solving skills.\n\nThe annual base salary for this position ranges "
        "from $90,000 to $110,000, based on skills and experience. Acme offers a competitive "
        "benefits package.\nAcme, Inc. is an equal employment opportunity employer.\n"
        "#J-12345-Abcdef",
        [
            ("Experience leading in a team environment.", fit.Stance.PREFERRED),
            ("Demonstrated problem-solving skills.", fit.Stance.PREFERRED),
        ],
    ),
    (
        "Competencies\n- Working knowledge of clinical management tools\n"
        "- Proven experience in planning and change management\nAcme is required by law in some "
        "states to include an estimate of the pay range for this role, based on location and "
        "experience. This role is also eligible for a discretionary annual bonus, health "
        "insurance and paid time off.\nCurrent estimated range\n$90,000 - $110,000 USD\n\n"
        "Date Posted\n15-Sep-2026\nAcme is an equal opportunity employer.",
        [
            ("Working knowledge of clinical management tools", fit.Stance.MUST_HAVE),
            ("Proven experience in planning and change management", fit.Stance.MUST_HAVE),
        ],
    ),
]


@pytest.mark.parametrize(("text", "expected"), _CLOSING_PARAGRAPHS_AFTER_A_LIST)
def test_the_lines_after_a_closing_paragraph_do_not_rejoin_the_list_above_it(
    text: str, expected: list[tuple[str, fit.Stance]], inventory: CvInventory
) -> None:
    reading = reading_of(f"Data Analyst\n\n{text}\n", inventory)
    assert [(line.whole, line.stance) for line in reading.requirements] == expected


@pytest.mark.parametrize("key", ["Location", "Shift", "Schedule", "Work Site"])
def test_a_preference_key_in_a_run_opens_no_preferred_section(
    key: str, inventory: CvInventory
) -> None:
    reading = reading_of(
        "Data Analyst\n\nRequirements\n- 3+ years of SQL\n- Python\n"
        "Pay Range:   $120,000 - $160,000   Other compensation:   Bonus Eligible   "
        f"{key}  Preference:   Springfield-based talent preferred, remote considered\n\n"
        "Perks & Benefits\n- Health insurance\n",
        inventory,
    )
    assert [line.whole for line in reading.requirements] == ["3+ years of SQL", "Python"]


@pytest.mark.parametrize("glyph", ["•", "·", "▪"])
@pytest.mark.parametrize("label", ["Optional Skills", "Optional Qualifications"])
def test_an_optional_label_run_into_a_glyph_list_opens_a_preferred_section(
    glyph: str, label: str, inventory: CvInventory
) -> None:
    items = f" {glyph}  ".join(
        [
            "Qualifications",
            "8+ years of experience in software architecture",
            "Proficiency in Python and Java",
            f"Ability to lead technical reviews with global teams {label}",
            "Experience in academic publishing",
            "TOGAF or equivalent architecture certifications",
        ]
    )
    reading = _run_on(items, inventory)
    assert [line.stance for line in reading.requirements] == [
        fit.Stance.MUST_HAVE,
        fit.Stance.MUST_HAVE,
        fit.Stance.MUST_HAVE,
        fit.Stance.PREFERRED,
        fit.Stance.PREFERRED,
    ]


def test_a_bare_optional_heading_over_a_list_opens_a_preferred_section(
    inventory: CvInventory,
) -> None:
    reading = reading_of(
        "Data Analyst\n\nRequirements\n- SQL\n\nOptional\n- Tableau\n- dbt\n", inventory
    )
    assert [(line.whole, line.stance) for line in reading.requirements] == [
        ("SQL", fit.Stance.MUST_HAVE),
        ("Tableau", fit.Stance.PREFERRED),
        ("dbt", fit.Stance.PREFERRED),
    ]


@pytest.mark.parametrize(
    "line", ["Bachelor's Degree (Optional)", "Travel Optional", "Certifications Optional"]
)
def test_an_optional_line_inside_a_requirement_list_leaves_the_items_after_it_required(
    line: str, inventory: CvInventory
) -> None:
    reading = reading_of(
        f"Data Analyst\n\nRequirements\n- 3+ years of facilities experience\n{line}\n"
        "- Active locksmith license\n- 8+ years of SQL reporting\n",
        inventory,
    )
    stances = {requirement.whole: requirement.stance for requirement in reading.requirements}
    assert stances["Active locksmith license"] is fit.Stance.MUST_HAVE
    assert stances["8+ years of SQL reporting"] is fit.Stance.MUST_HAVE


@pytest.mark.parametrize(
    "heading",
    [
        "Required and Optional Skills",
        "Qualifications (required and optional)",
        "Skills: Required / Optional",
    ],
)
def test_a_heading_naming_required_and_optional_reads_its_items_required(
    heading: str, inventory: CvInventory
) -> None:
    reading = reading_of(f"Data Analyst\n\n{heading}\n- SQL\n- Python\n", inventory)
    assert [line.stance for line in reading.requirements] == [fit.Stance.MUST_HAVE] * 2


def test_an_optional_benefit_heading_opens_no_preferred_section(inventory: CvInventory) -> None:
    reading = reading_of(
        "Data Analyst\n\nOptional Team Offsites\nWe meet in person several times a year.\n"
        "Parental Leave: 12 weeks fully paid.\n",
        inventory,
    )
    assert reading.requirements == ()


def test_an_optional_label_over_its_own_text_is_no_heading(inventory: CvInventory) -> None:
    reading = reading_of(
        "Data Analyst\n\nResponsibilities\n- Build dashboards\nOptional: attach a cover letter\n",
        inventory,
    )
    assert reading.requirements == ()


def test_a_lowercase_qualifications_colon_in_a_duty_opens_no_section(
    inventory: CvInventory,
) -> None:
    reading = _run_on(
        "Keep records current and verify the following qualifications: licences and "
        "certifications for every clinician on the roster.",
        inventory,
    )
    assert reading.sections.value is None
    assert reading.requirements == ()


def test_a_ladder_run_on_to_a_wish_reads_its_lowest_rung(inventory: CvInventory) -> None:
    reading = _run_on(
        "An experienced analyst with at least 1 year of experience in Tier 1 or 2 years in Tier 2 "
        "advisory firms or An experienced professional with at least 4 years of non-advisory "
        "experience in relevant industries (e.g. retail, logistics, etc.) or An MBA / Masters "
        "graduate with at least 2 years of experience in relevant industries (e.g. retail, "
        "logistics, etc.) Experience in advisory work will be highly advantageous.",
        inventory,
    )
    assert reading.years.value == 2
    assert [line.stance for line in reading.requirements] == [
        fit.Stance.MUST_HAVE,
        fit.Stance.PREFERRED,
    ]


_ABBREVIATED_SUBJECTS = (
    "Ph.D. in chemistry, or a related field",
    "M.S. in statistics",
    "B.A. or B.S. in economics",
    "U.S. insurance licensure",
    "Experience with clinical workflows (e.g. triage)",
    "Knowledge of SQL, Python, etc.",
    "Experience reporting to a Sr. Manager",
)


@pytest.mark.parametrize(
    ("subject", "cue"),
    list(itertools.product(_ABBREVIATED_SUBJECTS, ("preferred", "is a plus", "would be an asset"))),
)
def test_an_abbreviation_period_leaves_a_cued_line_one_clause(
    subject: str, cue: str, inventory: CvInventory
) -> None:
    reading = reading_of(
        f"Chemist\n\nAdditional Qualifications and Skills\n- {subject} {cue}.\n- SQL", inventory
    )
    assert reading.requirements[0].stance is fit.Stance.PREFERRED


@pytest.mark.parametrize(
    "line",
    [
        "Bachelor's degree in finance preferred. Equivalent experience may substitute for the "
        "degree.",
        "Bachelor's degree in finance preferred; or 6+ years of relevant experience",
        "Bachelor's degree in finance preferred. Or 6+ years of relevant experience.",
        "Bachelor's degree in finance preferred; relevant experience can substitute for it",
        "Bachelor's degree in finance preferred; 4 years of experience in lieu of the degree",
        "Bachelor's degree in finance preferred; experience in place of a degree is accepted",
        "Bachelor's degree in finance preferred; equivalent experience is accepted",
    ],
)
def test_a_substitute_for_a_preferred_clause_keeps_the_line_preferred(
    line: str, inventory: CvInventory
) -> None:
    reading = reading_of(f"Financial Analyst\n\nRequirements:\n- {line}\n- SQL", inventory)
    assert reading.requirements[0].stance is fit.Stance.PREFERRED
    assert reading.years.value is None


def test_a_degree_with_its_own_equivalent_beside_a_wish_stays_must_have(
    inventory: CvInventory,
) -> None:
    line = "Bachelor's degree or equivalent; MBA preferred"
    reading = reading_of(f"Financial Analyst\n\nRequirements:\n- {line}\n- SQL", inventory)
    assert reading.requirements[0].stance is fit.Stance.MUST_HAVE


_ASSET_ADJECTIVES = (
    "significant",
    "strong",
    "great",
    "valuable",
    "definite",
    "real",
    "major",
    "key",
    "big",
    "huge",
    "added",
    "additional",
)
_SECOND_WISHES = (
    *(
        f"familiarity with billing codes is {'an' if word[0] in 'aeiou' else 'a'} {word} asset"
        for word in _ASSET_ADJECTIVES
    ),
    "billing and coding skills are assets",
    "billing and coding skills are valuable assets",
    "billing and coding skills would be a great asset",
    "prior leadership experience ideal",
    "prior leadership experience is ideal",
    "prior leadership experience would be ideal",
    "leadership and coaching skills are ideal",
)


@pytest.mark.parametrize(
    ("first", "second"),
    list(
        itertools.product(
            (
                "Board certification strongly preferred",
                "Experience with registry data is strongly preferred",
            ),
            _SECOND_WISHES,
        )
    ),
)
def test_an_asset_or_ideal_clause_beside_a_wish_keeps_the_line_preferred(
    first: str, second: str, inventory: CvInventory
) -> None:
    reading = reading_of(
        f"Clinical Analyst\n\nRequirements:\n- {first}; {second}\n- SQL", inventory
    )
    assert reading.requirements[0].stance is fit.Stance.PREFERRED


@pytest.mark.parametrize(
    ("line", "years"),
    [
        ("The ideal candidate has 5+ years of experience in billing", 5),
        ("Ideal candidates have 5+ years of experience in billing", 5),
        ("5+ years of experience with key asset management platforms", 5),
        ("5+ years of experience managing a key asset register", 5),
    ],
)
def test_an_ideal_candidate_or_an_asset_noun_is_no_wish(
    line: str, years: int, inventory: CvInventory
) -> None:
    reading = reading_of(f"Clinical Analyst\n\nRequirements:\n- {line}\n- SQL", inventory)
    assert reading.requirements[0].stance is fit.Stance.MUST_HAVE
    assert reading.years.value == years


@pytest.mark.parametrize(
    ("line", "years"),
    [
        ("12+ years in enterprise SaaS; bonus if you know healthcare billing", 12),
        ("5+ years of product work; healthcare experience strongly preferred", 5),
        ("SQL expertise is essential; SQL is the job here, not a bonus", None),
    ],
)
def test_an_uncued_clause_keeps_its_line_must_have_and_its_floor(
    line: str, years: int | None, inventory: CvInventory
) -> None:
    reading = reading_of(f"Product Analyst\n\nRequirements:\n- {line}\n- SQL", inventory)
    assert reading.requirements[0].stance is fit.Stance.MUST_HAVE
    assert reading.years.value == years


_PHRASE_JOINS = (", ", " and ", ", but ", "; ")
_WAIVED_PHRASES = (
    "CPA not required",
    "Python is optional",
    "travel not needed",
    "a master's is not required",
    "no degree required",
)


@pytest.mark.parametrize(
    ("requirement", "join", "waiver"),
    list(
        itertools.product(
            (
                "5+ years of experience required",
                "3+ years of experience in SQL",
                "5 years of experience in field sales",
            ),
            _PHRASE_JOINS,
            _WAIVED_PHRASES,
        )
    ),
)
def test_a_waived_phrase_joined_to_a_requirement_leaves_its_years(
    requirement: str, join: str, waiver: str, inventory: CvInventory
) -> None:
    reading = reading_of(
        f"Data Analyst\n\nRequirements:\n- {requirement}{join}{waiver}\n- SQL", inventory
    )
    assert [requirement.stance for requirement in reading.requirements] == [
        fit.Stance.MUST_HAVE
    ] * 2
    assert reading.years.value == int(requirement[0])


@pytest.mark.parametrize(
    ("join", "waiver"), list(itertools.product(_PHRASE_JOINS, _WAIVED_PHRASES))
)
def test_a_waived_phrase_joined_to_a_degree_leaves_the_degree_required(
    join: str, waiver: str, inventory: CvInventory
) -> None:
    reading = reading_of(
        f"Staff Nurse\n\nRequirements:\n- Bachelor's degree{join}{waiver}\n- SQL", inventory
    )
    assert reading.education.value == "bachelor's degree, stated as must-have"


@pytest.mark.parametrize(
    "line",
    [
        "Entry level position, no experience required.",
        "Entry-level role; no degree required",
        "This is an entry level position and no experience is needed.",
        "Entry level job, prior experience is not required",
    ],
)
def test_a_waiver_beside_only_an_opening_label_asks_for_nothing(
    line: str, inventory: CvInventory
) -> None:
    reading = reading_of(f"Data Analyst\n\nRequirements:\n- {line}\n- SQL", inventory)
    assert [requirement.whole for requirement in reading.requirements] == ["SQL"]


@pytest.mark.parametrize(
    "line",
    [
        "Experience in project management will be an advantage.",
        "Knowledge of HL7 is considered an advantage.",
        "Certifications in Lean are an advantage.",
        "Greenhouse experience is beneficial but not required.",
        "Experience with Epic is beneficial.",
        "Certifications in Lean are beneficial.",
    ],
)
def test_an_advantage_or_benefit_predicate_marks_the_line_preferred(
    line: str, inventory: CvInventory
) -> None:
    reading = reading_of(f"Data Analyst\n\nRequirements:\n- {line}\n- SQL", inventory)
    assert [requirement.stance for requirement in reading.requirements] == [
        fit.Stance.PREFERRED,
        fit.Stance.MUST_HAVE,
    ]


@pytest.mark.parametrize(
    "line",
    [
        "5+ years of experience turning analytics into an advantage for clients",
        "5+ years of experience deciding what is beneficial to patients",
        "5+ years of experience where speed is beneficial to the business",
    ],
)
def test_an_advantage_or_benefit_word_that_is_no_predicate_leaves_the_line_must_have(
    line: str, inventory: CvInventory
) -> None:
    reading = reading_of(f"Data Analyst\n\nRequirements:\n- {line}\n- SQL", inventory)
    assert reading.years.value == 5
    assert reading.requirements[0].stance is fit.Stance.MUST_HAVE


@pytest.mark.parametrize(
    "body",
    [
        "Our key strengths\n- 25 years of experience serving clinics\n- Offices in 12 cities",
        "Our Key Strengths:\n- 25 years of experience serving clinics\n- Offices in 12 cities",
        "What we look for in a vendor:\n- 25 years of experience in the market",
        "What we look for in our partners\n- 25 years of experience in the market",
    ],
)
def test_a_strengths_or_look_for_heading_about_someone_else_heads_no_requirements(
    body: str, inventory: CvInventory
) -> None:
    reading = reading_of(f"Data Analyst\n\n{body}\n\nRequirements:\n- SQL", inventory)
    assert [line.whole for line in reading.requirements] == ["SQL"]
    assert reading.years.value is None


@pytest.mark.parametrize(
    "heading", ["What we look for in you:", "What we look for in a candidate", "Your key strengths"]
)
def test_a_look_for_heading_about_the_candidate_heads_requirements(
    heading: str, inventory: CvInventory
) -> None:
    reading = reading_of(f"Data Analyst\n\n{heading}\n- 4+ years of SQL experience", inventory)
    assert reading.years.value == 4


@pytest.mark.parametrize(
    "sentence",
    [
        "Remote isn't new to us; this role is fully remote.",
        "Working remote isn't just a perk here, it's how the whole team works.",
        "Remote: no travel required.",
        "Remote: no office visits, ever.",
    ],
)
def test_a_remote_word_beside_a_negation_about_something_else_still_answers_remote(
    sentence: str, inventory: CvInventory
) -> None:
    posting = f"Data Analyst\n\nAbout the role\n{sentence}\n\nRequirements:\n- SQL"
    assert reading_of(posting, inventory).location.value == "remote"


@pytest.mark.parametrize(
    "line",
    [
        "- Doctorate degree and 2 years of product experience or\n"
        "- Master's degree and 6 years of product experience or\n"
        "- Bachelor's degree and 8 years of product experience or\n"
        "- Associate's degree and 10 years of product experience or\n"
        "- High school diploma and 12 years of product experience",
        "- \u2022 Bachelor's degree OR \u2022 Associate's degree and 2 years of experience "
        "OR \u2022 High school diploma / GED and 4 years of experience",
    ],
    ids=["rungs across lines", "or before a bullet glyph"],
)
def test_a_diploma_rung_answers_the_lowest_degree_or_equivalent_experience(
    line: str, inventory: CvInventory
) -> None:
    reading = reading_of(f"Data Engineer\n\nBasic Qualifications\n{line}", inventory)
    assert reading.education.value == "associate degree, or equivalent experience"


_HEADING_SHAPES = ("{}:", "{}", "## {}", "{}:\n")
_ASKING_HEADINGS = (
    ("Your Key Strengths", fit.Stance.MUST_HAVE),
    ("Key Strengths", fit.Stance.MUST_HAVE),
    ("KEY STRENGTHS", fit.Stance.MUST_HAVE),
    ("What we look for", fit.Stance.MUST_HAVE),
    ("What We Look For", fit.Stance.MUST_HAVE),
    ("Great-to-have", fit.Stance.PREFERRED),
    ("Great-to-haves", fit.Stance.PREFERRED),
    ("Great to have", fit.Stance.PREFERRED),
    ("Great To Have", fit.Stance.PREFERRED),
)


@pytest.mark.parametrize(
    ("heading", "shape"), list(itertools.product(_ASKING_HEADINGS, _HEADING_SHAPES))
)
def test_a_strengths_look_for_or_great_to_have_heading_names_its_section(
    heading: tuple[str, fit.Stance], shape: str, inventory: CvInventory
) -> None:
    name, stance = heading
    items = "- Experience with dbt\n- Familiarity with Airflow"
    posting = f"Data Engineer\n\nNorthwind plans timetables.\n\n{shape.format(name)}\n{items}"
    reading = reading_of(posting, inventory)
    assert [line.stance for line in reading.requirements] == [stance, stance]
    assert reading.sections.stated


@pytest.mark.parametrize("heading", ["Our strengths", "Strengths", "Why we look forward to it"])
def test_a_heading_near_the_new_names_stays_no_requirement_section(
    heading: str, inventory: CvInventory
) -> None:
    items = "- Experience with dbt\n- Familiarity with Airflow"
    reading = reading_of(f"Data Engineer\n\n{heading}:\n{items}", inventory)
    assert reading.requirements == ()


def _cased(text: str) -> tuple[str, ...]:
    return (text, text.upper(), " ".join(word[:1].upper() + word[1:] for word in text.split()))


# Markdown forms stay in sentence case with no closing mark: "## You bring:" heads no
# list, and "## What You Need For This Role" splits into two sections.
def _heading_forms(texts: tuple[str, ...], ends: tuple[str, ...]) -> list[str]:
    plain = {f"{cased}{end}" for text in texts for cased in _cased(text) for end in ends}
    return sorted(plain | {f"## {text}" for text in texts})


_NOUNS = ("role", "position", "job")
_YOU_WILL = ("you'll", "you" + chr(0x2019) + "ll", "you will")
# "To thrive in this role, you need to have" is nine words, one over what a bare line
# over a list may carry, so it heads a list only with a colon or as a Markdown heading.
_ASKING_SENTENCE_HEADINGS = (
    *_heading_forms(tuple(f"What do you need for this {noun}" for noun in _NOUNS), ("?", ":", "")),
    *_heading_forms(
        tuple(f"To qualify for this {noun}, {you} need" for noun in _NOUNS for you in _YOU_WILL),
        (":", ""),
    ),
    *_heading_forms(
        tuple(f"To thrive in this {noun}, you need to have" for noun in _NOUNS), (":",)
    ),
    *_heading_forms(("You bring",), (":", "")),
    "Skills and experience we need from you:",
    "Qualifications we need to know you hold:",
    "Requirements and what you need to know:",
    "Qualifications & What You Need to Know",
    "What you need to know / Requirements",
    "Minimum qualifications (what you need to know)",
    "What you need to know - requirements",
)
_NOT_ASKING_HEADINGS = (
    _heading_forms(
        (
            "What we bring",
            "What we offer you",
            *(f"What you{apostrophe}ll get" for apostrophe in ("'", chr(0x2019))),
            "What you can bring home",
            "Physical requirements",
            "What you need to know",
            *(f"What {you} need to know before applying" for you in _YOU_WILL),
        ),
        (":", ""),
    )
    + _heading_forms(
        (
            "What do you need to know",
            "What do you need to know before applying",
            "What do you need from us",
            "What you need from us",
        ),
        ("?", ":"),
    )
    + ["## What do you need to know?", "## What you need to know:"]
)


@pytest.mark.parametrize("heading", _ASKING_SENTENCE_HEADINGS)
def test_a_heading_asking_what_the_role_needs_opens_a_requirement_section(
    heading: str, inventory: CvInventory
) -> None:
    items = "- Experience with dbt\n- Familiarity with Airflow"
    posting = f"Data Engineer\n\nNorthwind plans timetables.\n\n{heading}\n{items}"
    reading = reading_of(posting, inventory)
    assert [line.stance for line in reading.requirements] == [fit.Stance.MUST_HAVE] * 2


@pytest.mark.parametrize("heading", _NOT_ASKING_HEADINGS)
def test_a_heading_near_the_asking_ones_opens_no_requirement_section(
    heading: str, inventory: CvInventory
) -> None:
    items = "- Experience with dbt\n- Familiarity with Airflow"
    posting = f"Data Engineer\n\nNorthwind plans timetables.\n\n{heading}\n{items}"
    assert reading_of(posting, inventory).requirements == ()


@pytest.mark.parametrize("heading", ["What you need to know:", "What do you need to know?"])
def test_a_heading_asking_what_to_know_keeps_only_stated_requirements(
    heading: str, inventory: CvInventory
) -> None:
    items = "- Interviews run in two rounds\n- 5+ years of experience with dbt is required"
    posting = f"Data Engineer\n\nRequirements:\n- SQL\n\n{heading}\n{items}"
    reading = reading_of(posting, inventory)
    assert [line.whole for line in reading.requirements] == [
        "SQL",
        "5+ years of experience with dbt is required",
    ]


_CLAUSE_PROSE: tuple[tuple[str, list[fit.Stance]], ...] = (
    (
        "Northwind offers generous perks. To qualify for Tuition Reimbursement, staff must hold "
        "an active locksmith licensure.",
        [],
    ),
    (
        "Perks are generous. What do you need To Know? Active locksmith licensure is nice to have.",
        [fit.Stance.PREFERRED],
    ),
    (
        "Perks are generous. What do you need For Travel? Active locksmith licensure is nice to "
        "have.",
        [fit.Stance.PREFERRED],
    ),
    ("We are a hospital. You need to have 3 years of ICU experience.", []),
    ("About the role: you bring Airflow skills and we bring coaching.", []),
)


@pytest.mark.parametrize(("text", "stances"), _CLAUSE_PROSE)
def test_a_sentence_opening_with_an_asking_phrase_is_not_cut_at_it(
    text: str, stances: list[fit.Stance], inventory: CvInventory
) -> None:
    posting = f"Facilities Program Manager\n\nNorthwind plans timetables.\n\n{text}\n"
    reading = reading_of(posting, inventory)
    assert [line.stance for line in reading.requirements] == stances


_MUST_CLAUSES = (
    "Fluency in English (required)",
    "Fluency in English is required",
    "You must be fluent in English",
    "Fluency in English is mandatory",
)
_PREFERRED_CLAUSES = (
    "French language skills are a great-to-have",
    "French is preferred",
    "French is a plus",
    "French would be an advantage",
)


@pytest.mark.parametrize(
    ("must", "separator", "wish"),
    list(itertools.product(_MUST_CLAUSES, ("; ", ". "), _PREFERRED_CLAUSES)),
)
def test_a_required_clause_keeps_its_line_must_have_beside_a_preferred_clause(
    must: str, separator: str, wish: str, inventory: CvInventory
) -> None:
    line = f"- {must}{separator}{wish}"
    for heading in ("Requirements:", "About the role:"):
        reading = reading_of(f"Translator\n\n{heading}\n{line}", inventory)
        assert [requirement.stance for requirement in reading.requirements] == [
            fit.Stance.MUST_HAVE
        ]


@pytest.mark.parametrize(
    "line",
    [
        "- Fluency in French is required, ideally at a native level",
        "- French not required but preferred",
        "- French is required or strongly preferred",
    ],
)
def test_a_preference_in_the_clause_that_requires_keeps_the_line_preferred(
    line: str, inventory: CvInventory
) -> None:
    reading = reading_of(f"Translator\n\nRequirements:\n{line}", inventory)
    assert [requirement.stance for requirement in reading.requirements] == [fit.Stance.PREFERRED]


_CAPITAL_FIGURES = ("5", "5+", "5 +", "5 or more")
_CAPITAL_UNITS = ("Years", "YEARS", "Yrs", "YRS", "Yrs.")
_CAPITAL_PHRASES = (
    "of Experience in Product Management",
    "of relevant experience in data engineering",
    "of Experience: Predominantly in building data platforms",
    "Experience in Product Management",
)


@pytest.mark.parametrize(
    ("figure", "unit", "phrase"),
    [
        combination
        for combination in itertools.product(_CAPITAL_FIGURES, _CAPITAL_UNITS, _CAPITAL_PHRASES)
        # "Yrs. Experience" reads as two sentences, so that unit takes an "of" phrase only.
        if not (combination[1].endswith(".") and combination[2].startswith("Experience"))
    ],
)
def test_a_years_figure_with_a_capitalized_unit_reads_its_floor(
    figure: str, unit: str, phrase: str, inventory: CvInventory
) -> None:
    line = f"- {figure} {unit} {phrase}"
    reading = reading_of(f"Product Manager\n\nRequirements:\n{line}", inventory)
    assert reading.years.value == 5


_POSSESSIVE_FIGURES = ("two", "Two", "two (2)", "2")
_POSSESSIVE_WORK = (
    "clinical research experience",
    "product management experience",
    "hands-on data engineering experience",
)


@pytest.mark.parametrize(
    ("opening", "figure", "apostrophe", "work"),
    list(
        itertools.product(
            ("Minimum of ", "At least ", ""),
            _POSSESSIVE_FIGURES,
            ("'", "\u2019"),
            _POSSESSIVE_WORK,
        )
    ),
)
def test_a_possessive_years_figure_before_its_work_reads_its_floor(
    opening: str, figure: str, apostrophe: str, work: str, inventory: CvInventory
) -> None:
    line = f"- {opening}{figure} years{apostrophe} {work}."
    reading = reading_of(f"Clinical Research Associate\n\nRequirements:\n{line}", inventory)
    assert reading.years.value == 2


@pytest.mark.parametrize(
    "text",
    [
        "Celebrating 10 Years of service to regional clinics",
        "Join us for our 5 Years anniversary celebration",
        "Northwind celebrates 25 YEARS of service this spring",
        "Celebrating ten years' service to our community",
        "Our team has two years' worth of roadmap ahead",
    ],
)
@pytest.mark.parametrize("heading", ["Requirements:", "About us"])
def test_a_capitalized_or_possessive_years_figure_naming_no_experience_states_no_years(
    text: str, heading: str, inventory: CvInventory
) -> None:
    reading = reading_of(f"Product Manager\n\n{heading}\n- {text}", inventory)
    assert reading.years.value is None


def test_years_stated_only_as_a_wish_are_no_must_have_floor(inventory: CvInventory) -> None:
    posting = "Data Engineer\n\nRequirements\n- Fluency in SQL\n- Ideally 5+ years of experience"
    reading = reading_of(posting, inventory)
    assert reading.years.value is None
    assert reading.years.note == "no must-have line states a number of years of experience"


DEGREE_LADDER = (
    "- Doctorate degree and 2 years of product experience or\n"
    "- Master's degree and 6 years of product experience or\n"
    "- Bachelor's degree and 8 years of product experience or\n"
    "- Associate's degree and 10 years of product experience or\n"
    "- High school diploma and 12 years of product experience"
)
LONG_REQUIREMENT = (
    "- Built and ran data pipelines for regional timetable planning, crew rostering, depot "
    "maintenance forecasting and passenger demand models across the Northwind network"
)


def test_a_years_figure_past_the_quote_still_sets_the_floor(inventory: CvInventory) -> None:
    posting = f"Data Engineer\n\nRequirements\n{LONG_REQUIREMENT}, with 6+ years of such experience"
    reading = reading_of(posting, inventory)
    assert reading.years.value == 6
    assert "6+ years" in reading.years.evidence[0]


def test_a_degree_past_the_quote_is_read_as_a_requirement(inventory: CvInventory) -> None:
    posting = f"Data Engineer\n\nRequirements\n{LONG_REQUIREMENT}, and a bachelor's degree"
    reading = reading_of(posting, inventory)
    assert reading.education.value == "bachelor's degree, stated as must-have"
    assert "bachelor's degree" in reading.education.evidence[0]


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("- 8 years of experience with a diploma, or 4 years with a bachelor's degree", 4),
        ("- 7 years of experience at the senior level or 5 years at the intermediate level", 5),
        ("- 5 years of experience with Python and 3 years with AWS", 5),
        ("- 5 years of experience in Python or Java and 3 years with AWS", 5),
        ("- 8 years of experience overall\n- 4 years of experience with dbt", 8),
        (DEGREE_LADDER, 2),
        (
            "- Doctorate degree and 2 years of product experience\nOR\n"
            "- Master's degree and 6 years of product experience\nOR\n"
            "- Bachelor's degree and 8 years of product experience",
            2,
        ),
        (
            "- Bachelor's degree and 8 years of product experience\n"
            "- Or - Master's degree and 6 years of product experience",
            6,
        ),
        ("- 5 or more years of experience with dbt", 5),
        ("- Five years of experience with dbt", 5),
        ("- Five (5) years' experience with dbt", 5),
        (
            "- 1\u20135+ years of experience in Engineering, data analytics, or AI/automation "
            "tinkering",
            1,
        ),
        ("- 3-5 years of experience in Engineering, data analytics, or AI/automation", 3),
        (
            "Bachelor's degree in Engineering or Master's degree in Data Science. 1\u20135+ years "
            "of experience in Engineering, data analytics, or AI/automation tinkering.",
            1,
        ),
    ],
    ids=[
        "degree ladder",
        "level ladder",
        "conjunctive line",
        "or between nouns",
        "separate lines",
        "rungs joined by a trailing or",
        "rungs joined by an OR line",
        "rungs joined by a leading or",
        "or more",
        "spelled",
        "spelled with digits",
        "range with a plus before a field list",
        "range before a field list",
        "range after a degree choice in a flat line",
    ],
)
def test_alternative_paths_gate_on_their_lowest_years_and_required_ones_on_the_highest(
    line: str, expected: int, inventory: CvInventory
) -> None:
    reading = reading_of(f"Data Engineer\n\nBasic Qualifications:\n{line}", inventory)
    assert reading.years.value == expected


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("- Three to five years of product experience", 3),
        ("- A minimum of five to seven years of relevant experience", 5),
        ("- Five (5) to seven (7) years of relevant experience", 5),
        ("- Three or four years of relevant experience", 3),
        ("- Two-to-four years of relevant experience", 2),
        ("- 4-6 years of experience in a supervisory role", 4),
        ("- Three or more years of experience with dbt", 3),
        ("- Five years of experience with dbt", 5),
        ("- Three to five years of SQL experience\n- At least 3 years of SQL experience", 3),
    ],
    ids=[
        "spelled to",
        "minimum of a spelled range",
        "spelled range with digits",
        "spelled or",
        "hyphenated spelled range",
        "digit range",
        "spelled or more",
        "single spelled figure",
        "spelled range beside a digit floor",
    ],
)
def test_a_spelled_years_range_reads_its_bottom(
    line: str, expected: int, inventory: CvInventory
) -> None:
    reading = reading_of(f"Data Engineer\n\nBasic Qualifications:\n{line}", inventory)
    assert reading.years.value == expected


_RANGE_SIDES = (("3", "5"), ("three", "five"), ("three", "5"), ("3", "five"))
_RANGE_JOINS = ("-", "\u2013", "\u2014", " - ", " to ", " or ", " through ", "-to-", " and ")
_YEAR_UNITS = ("year", "years", "yr", "yrs", "yrs.", "years'")
_SINGLE_FIGURES = ("3", "three", "three (3)", "three (3+)")
_FLOOR_FORMS = (
    "{}+",
    "{} +",
    "{} plus",
    "{} or more",
    "{}-plus",
    "at least {}",
    "a minimum of {}",
    "minimum {}",
    "min. {}",
)


def _experience(figure: str, unit: str) -> str:
    if unit.endswith("'"):
        return f"- {figure} {unit} experience in product"
    return f"- {figure} {unit} of product experience"


RANGE_FORMS = [
    pytest.param(
        _experience(f"{opening}{low}{join}{high}{plus}", unit), id=f"{low}{join}{high}{plus} {unit}"
    )
    for low, high in _RANGE_SIDES
    for join in _RANGE_JOINS
    for opening in (("between ",) if join == " and " else ("",))
    for plus in ("", "+")
    for unit in _YEAR_UNITS
]
FLOOR_FORMS = [
    pytest.param(_experience(form.format(figure), unit), id=f"{form.format(figure)} {unit}")
    for form in _FLOOR_FORMS
    for figure in _SINGLE_FIGURES
    for unit in _YEAR_UNITS
]


@pytest.mark.parametrize("line", RANGE_FORMS + FLOOR_FORMS)
def test_a_years_figure_or_range_reads_its_bottom_however_it_is_written(
    line: str, inventory: CvInventory
) -> None:
    reading = reading_of(f"Data Engineer\n\nBasic Qualifications:\n{line}", inventory)
    assert reading.years.value == 3


def test_a_bare_or_line_links_rungs_without_counting_as_a_requirement(
    inventory: CvInventory,
) -> None:
    body = plain_layout(
        "Basic Qualifications\n"
        "- Bachelor's degree and 8 years of product experience\nOR\n"
        "- Master's degree and 6 years of product experience\nOR\n"
        "- Doctorate degree and 2 years of product experience"
    )
    reading = reading_of(f"Product Manager\n\n{body}", inventory)
    assert reading.must_have.value == 3
    assert reading.years.value == 2
    assert all(line.text in body and line.whole in body for line in reading.requirements)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "Basic Qualifications:\n"
            "- Bachelor's degree and 8 years of experience in data engineering\nOR\n"
            "- Master's degree and 6 years of experience, preferred\n"
            "- 2+ years of experience with Airflow",
            8,
        ),
        (
            "Requirements:\n- 8+ years of experience in data engineering\nOR\n\n"
            "About you:\n- You must have 2+ years of experience with Airflow",
            8,
        ),
        (
            "Requirements:\n- 8+ years of experience in data engineering\n"
            "- Salary range $120,000 - $150,000\nOR\n- 2+ years of experience with Airflow",
            8,
        ),
        (
            "Requirements:\n- Bachelor's degree and 7+ years of software experience\n"
            "- Master's degree and 3+ years of experience preferred\n"
            "- High school diploma and 2 years of experience with forklifts",
            7,
        ),
    ],
    ids=[
        "or before a preferred rung",
        "or at the end of a block",
        "or after a skipped pay line",
        "must rungs either side of a preferred rung",
    ],
)
def test_alternatives_join_only_adjacent_lines_of_one_stance(
    body: str, expected: int, inventory: CvInventory
) -> None:
    reading = reading_of(f"Data Engineer\n\n{body}", inventory)
    assert reading.years.value == expected


_OR_AS_A_NAME = (
    "in the OR",
    "as an OR nurse",
    "with OR experience",
    "in the OR, ICU, or PACU",
    "in Operations Research (OR)",
    "with OR-Tools",
)
_LINES_NAMING_OR = (
    "- 5+ years of clinical experience {} and a Bachelor's degree in nursing",
    "- 5+ years of clinical experience {}, including 2+ years in a charge role",
)
OR_AS_A_NAME = [
    pytest.param(line.format(name), id=line.format(name)[2:])
    for name in _OR_AS_A_NAME
    for line in _LINES_NAMING_OR
] + [
    pytest.param(lines, id=lines)
    for lines in (
        "- 5+ years of experience in Operations Research (OR)\n"
        "- 2+ years of experience with Python",
        "- 2+ years of experience with Python\n- OR-Tools modelling with 5+ years of experience",
        "- 2+ years of experience with Epic\n- OR/ICU nursing with 5+ years of experience",
    )
]


@pytest.mark.parametrize("lines", OR_AS_A_NAME)
def test_an_or_naming_a_room_a_field_or_a_tool_opens_no_second_path(
    lines: str, inventory: CvInventory
) -> None:
    reading = reading_of(f"Data Engineer\n\nBasic Qualifications:\n{lines}", inventory)
    assert reading.years.value == 5


@pytest.mark.parametrize(
    "lines",
    [
        "- Bachelor's degree and 5 years of experience OR Master's degree and 3 years of "
        "experience",
        "- 5 years of experience; OR 3 years with a Master's",
        "- 5 years of experience OR 3 years of experience with a Master's degree",
        "- 5 years of experience OR - 3 years of experience with a Master's degree",
        "- Bachelor's degree and 5 years of experience OR\n"
        "- Master's degree and 3 years of experience",
        "- Bachelor's degree and 5 years of experience\n"
        "- OR Master's degree and 3 years of experience",
    ],
)
def test_a_capital_or_opening_a_path_gates_on_the_lowest_rung(
    lines: str, inventory: CvInventory
) -> None:
    reading = reading_of(f"Data Engineer\n\nBasic Qualifications:\n{lines}", inventory)
    assert reading.years.value == 3


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            "- Master's degree and 8 to 10 years of experience OR -Bachelor's degree and 10 to "
            "12 years of experience",
            8,
        ),
        ("- Ph.D grad with 1+ years of industry experience OR Masters grad with 4+ years", 1),
        (
            "- High school diploma or GED with 9 or more years of experience; or Associate "
            "degree with 7 or more years; or Bachelor's degree with 5 or more years",
            5,
        ),
    ],
    ids=["or before a dash flush to a degree", "or before an unlisted degree", "synonym diploma"],
)
def test_an_or_before_a_shape_the_lookahead_misses_still_opens_a_path(
    line: str, expected: int, inventory: CvInventory
) -> None:
    reading = reading_of(f"Data Engineer\n\nBasic Qualifications:\n{line}", inventory)
    assert reading.years.value == expected


@pytest.mark.parametrize(
    ("line", "degree"),
    [
        (
            "- Currently enrolled in an MBA program OR a Master's program OR a PharmD program "
            "OR Ph.D. program",
            "master's degree",
        ),
        (
            "- Bachelor's degree in life sciences or related field OR a four-year degree "
            "combined with relevant experience required",
            "bachelor's degree",
        ),
    ],
    ids=["or before an article", "or before an unlisted degree"],
)
def test_a_capital_or_opening_a_path_gates_on_the_lowest_degree(
    line: str, degree: str, inventory: CvInventory
) -> None:
    reading = reading_of(f"Data Engineer\n\nBasic Qualifications:\n{line}", inventory)
    assert reading.education.value == f"{degree}, stated as must-have"


def test_an_or_before_a_preferred_degree_leaves_the_required_one_standing(
    inventory: CvInventory,
) -> None:
    body = (
        "Basic Qualifications:\n- Master's degree in Computer Science\nOR\n"
        "- Bachelor's degree, preferred\n- PhD in statistics required"
    )
    reading = reading_of(f"Data Engineer\n\n{body}", inventory)
    assert reading.education.value == "master's degree, stated as must-have"


def test_rungs_pairing_different_degrees_with_years_join_one_ladder_without_an_or(
    inventory: CvInventory,
) -> None:
    ladder = DEGREE_LADDER.replace(
        "8 years of product experience or", "8 years of product experience"
    )
    reading = reading_of(f"Product Manager\n\nBasic Qualifications\n{ladder}", inventory)
    assert reading.years.value == 2


RUNG_WITHOUT_YEARS = (
    "- Doctorate degree OR Master's degree and 3 years of operations experience OR Bachelor's "
    "degree and 5 years of operations experience OR High school diploma / GED and 12 years of "
    "operations experience"
)


@pytest.mark.parametrize(
    ("lines", "expected"),
    [(RUNG_WITHOUT_YEARS, None), (f"{RUNG_WITHOUT_YEARS}\n- 5+ years process experience", 5)],
    ids=["alone", "beside a separate floor"],
)
def test_a_ladder_with_a_path_stating_no_years_sets_no_floor(
    lines: str, expected: int | None, inventory: CvInventory
) -> None:
    reading = reading_of(f"Operations Manager\n\nBasic Qualifications\n{lines}", inventory)
    assert reading.years.value == expected


# A lower degree, then a higher one carrying the years clause.
_DEGREE_PAIRS = (
    ("Bachelor's degree", "Master's degree"),
    ("Bachelor\u2019s degree", "Master\u2019s degree"),
    ("Bachelor of Science", "Master of Science"),
    ("Associate's degree", "Bachelor's degree"),
    ("High school diploma", "Bachelor's degree"),
    ("Master's degree", "PhD"),
)
_ATTACHED = (" with", " and", " plus", ",")
_FIVE_YEARS = ("5+ years", "5 or more years", "five years", "at least 5 years")
_FIELDS = ("", " in computer science")


@pytest.mark.parametrize(
    ("pair", "field", "attached", "figure"),
    list(itertools.product(_DEGREE_PAIRS, _FIELDS, _ATTACHED, _FIVE_YEARS)),
)
def test_a_years_clause_after_the_higher_degree_binds_the_lower_degree_before_it(
    pair: tuple[str, str], field: str, attached: str, figure: str, inventory: CvInventory
) -> None:
    lower, higher = pair
    line = f"- {lower}{field} or {higher}{field}{attached} {figure} of experience"
    reading = reading_of(f"Data Analyst\n\nRequirements\n{line}", inventory)
    assert reading.years.value == 5


# A diploma opens no alternative path, so a degree before it reads as one path with it.
@pytest.mark.parametrize(
    ("pair", "field", "join", "attached"),
    list(
        itertools.product(
            [pair for pair in _DEGREE_PAIRS if "diploma" not in pair[0]],
            _FIELDS,
            (", or", " or", " OR"),
            _ATTACHED,
        )
    ),
)
def test_a_higher_degree_before_a_degree_with_years_stands_in_for_the_years(
    pair: tuple[str, str], field: str, join: str, attached: str, inventory: CvInventory
) -> None:
    lower, higher = pair
    line = f"- {higher}{field}{join} {lower}{field}{attached} 2 years of experience"
    reading = reading_of(f"Data Analyst\n\nRequirements\n{line}", inventory)
    assert reading.years.value is None


@pytest.mark.parametrize(
    "line",
    [
        "- Experience shipping one of the years' largest releases",
        "- In five years we grew the experience team to forty people",
        "- Deliver a five-year plan for the depot experience programme",
        "- Manage a three-to-five year contract for the rider experience team",
        "- Join a firm with over 180 years' experience serving its markets",
    ],
)
def test_a_number_that_is_no_experience_floor_states_no_years(
    line: str, inventory: CvInventory
) -> None:
    reading = reading_of(f"Data Engineer\n\nRequirements\n{line}", inventory)
    assert reading.years.value is None


_WORK_FIGURES = (
    "3",
    "3+",
    "3 +",
    "3 plus",
    "at least 3",
    *(f"3{dash}5{plus}" for dash in ("-", chr(0x2013)) for plus in ("", "+")),
)
_WORK_UNITS = ("years", "yrs", "yrs.")
_WORK_PHRASES = (
    "as a Product Owner",
    "as an analyst",
    "working in product management",
    "working with clinicians",
    "in product management",
    "in data engineering",
    "of product management",
    "of hands-on software development",
    "with Python",
    "in program management",
    "of education technology product work",
    "in higher education administration",
    *(
        f"{verb} data engineering teams"
        for verb in (
            "leading",
            "building",
            "managing",
            "designing",
            "developing",
            "owning",
            "delivering",
            "running",
            "supporting",
        )
    ),
)
WORK_FORMS = (
    [
        pytest.param(f"- {figure} {unit} {phrase}", id=f"{figure} {unit} {phrase}")
        for figure in _WORK_FIGURES
        for unit in _WORK_UNITS
        for phrase in _WORK_PHRASES
    ]
    + [
        pytest.param(f"- 3+ years {phrase[0].upper()}{phrase[1:]}", id=f"3+ years {phrase} capital")
        for phrase in _WORK_PHRASES
    ]
    + [
        pytest.param(f"- {figure} {unit} {phrase}", id=f"{figure} {unit} {phrase}")
        for figure in ("three", "three (3)", "three to five", "Three")
        for unit in ("years", "yrs")
        for phrase in _WORK_PHRASES
        if phrase.split()[0] in ("in", "of", "with")
    ]
)


@pytest.mark.parametrize("line", WORK_FORMS)
def test_a_required_years_figure_followed_by_its_work_reads_its_floor(
    line: str, inventory: CvInventory
) -> None:
    reading = reading_of(f"Data Engineer\n\nRequirements\n{line}", inventory)
    assert reading.years.value == 3


_NOT_WORK_FIGURES = (
    "has served clinics for over 20 years",
    "founded 15 years ago",
    "a leader in logistics for 30 years",
    "complete certification within 2 years of hire",
    "complete onboarding within 2 years of the start date",
    "renew every 3 years",
    "a 2-year contract",
    "for 2 years after hire",
    "18 years or older",
    "must be at least 21 years of age",
    "vest over 4 years",
    "Expected completion of one year of study from an accredited college or university",
    "at least two years of college coursework in statistics",
    "2 years in a graduate program",
    "three years of undergraduate study",
    "Obtain certification within 2 years as a condition of employment",
    "Recertify every 3 years as required by the state",
    "Complete the program within 2 years in the role",
    "Commit to 2 years with the program",
    "Committing to 2 years with the fellowship",
    "Promotion is reviewed after 2 years in the role",
    "Hold the credential for the first 3 years in the position",
    "Completion of 2 years in an accredited nursing program",
    "Sign a 2 year in-office agreement",
)


@pytest.mark.parametrize(
    "posting",
    [pytest.param(f"Requirements\n- {text}", id=f"required: {text}") for text in _NOT_WORK_FIGURES]
    + [
        pytest.param(f"About us\n\nWe are hiring, and {text}.", id=f"prose: {text}")
        for text in (*_NOT_WORK_FIGURES, "we have spent 20 years building logistics software")
    ],
)
def test_a_years_figure_naming_no_work_states_no_years(
    posting: str, inventory: CvInventory
) -> None:
    reading = reading_of(f"Data Engineer\n\n{posting}", inventory)
    assert reading.years.value is None


@pytest.mark.parametrize(
    ("line", "degree"),
    [
        (
            "- Master's degree; or Bachelor's degree and 5 to 8 years of relevant experience",
            "bachelor's degree",
        ),
        (
            "- Bachelor's degree in a life science or related discipline required "
            "Or - Master's degree in a life science",
            "bachelor's degree",
        ),
        (
            "- Doctorate degree / Master's degree and 2 years of experience "
            "OR Bachelor's degree and 4 years of experience",
            "bachelor's degree",
        ),
        (
            "- 10 years of experience with a bachelor's degree or 7 years with a master's degree",
            "bachelor's degree",
        ),
        (
            "- Advanced scientific degree (PharmD, PhD, MD, or MS) required or strongly preferred",
            "master's degree",
        ),
        (
            "- Master's level oncology PA or NP degree with a minimum of 13 years of related "
            "experience; or, Accredited doctorate degree in a life-science discipline",
            "master's degree",
        ),
        (
            "- Advanced scientific degree (including PharmD, PhD, MD, or MS) required",
            "master's degree",
        ),
    ],
    ids=[
        "semicolon or",
        "or dash",
        "upper-case OR",
        "years ladder",
        "degrees listed after the degree",
        "a level phrased as master's level",
        "degrees listed after including",
    ],
)
def test_alternative_degree_paths_gate_on_the_lowest_degree(
    line: str, degree: str, inventory: CvInventory
) -> None:
    reading = reading_of(f"Data Engineer\n\nBasic Qualifications\n{line}", inventory)
    assert reading.education.value == f"{degree}, stated as must-have"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            "- Advanced clinical or scientific degree preferred (PharmD, PhD, NP, MD, or other "
            "relevant life sciences degree).",
            "doctorate, stated as preferred",
        ),
        (
            "- Education - Minimum: a Bachelor's degree in Computer Science or a related "
            "technical field. - An asset (not required): a Master's degree in computer science.",
            "bachelor's degree, stated as must-have",
        ),
        (
            "Northwind Rail plans regional timetables. Minimum Qualifications Bachelor's Degree "
            "in a technical field. Minimum of 3 years experience building data pipelines. "
            "Preferred Qualifications Hands-on experience with dbt, Jupyter Notebooks, and "
            "Master's degree in a technical field.",
            "bachelor's degree, stated as must-have",
        ),
        (
            "- Minimum Bachelor's degree, master's preferred or terminal degree "
            "(MD, PhD or PharmD)",
            "bachelor's degree, stated as must-have",
        ),
        (
            "Northwind Health advises hospital networks. 5+ years of client-facing experience "
            "with a Bachelor's degree OR; Advanced degree in a biological science (e.g., PhD), "
            "applied health (e.g., MPH), or business (e.g., MBA) with 1+ years of client-facing "
            "experience.",
            "bachelor's degree, stated as must-have",
        ),
        (
            "- Bachelor's Degree in Pharmacy (Doctorate preferred) Required",
            "bachelor's degree, stated as must-have",
        ),
        (
            "- Bachelor's degree in Computer Science, Engineering, or related field "
            "(Master's preferred)",
            "bachelor's degree, stated as must-have",
        ),
        (
            "- Bachelor's degree in Computer Science, Software Engineering, or a related field "
            "(advanced degree a plus)",
            "bachelor's degree, stated as must-have",
        ),
        ("- Master's degree preferred", "master's degree, stated as preferred"),
    ],
    ids=[
        "cue before a list",
        "minimum then an asset",
        "inline preferred heading",
        "minimum beside a preference",
        "examples after e.g.",
        "preferred aside before required",
        "preferred aside after a field list",
        "a plus aside after a field list",
        "cue on the degree itself",
    ],
)
def test_a_preferred_degree_never_answers_as_a_must_have(
    line: str, expected: str, inventory: CvInventory
) -> None:
    reading = reading_of(f"Data Engineer\n\nRequirements:\n{line}", inventory)
    assert len(reading.requirements) == 1
    assert reading.education.value == expected


def test_an_or_inside_a_field_list_is_no_second_degree_path(inventory: CvInventory) -> None:
    line = "- Bachelor's degree in CS, Engineering, or a related field is a plus"
    reading = reading_of(f"Data Engineer\n\nRequirements\n{line}", inventory)
    assert reading.education.value == "bachelor's degree, stated as preferred"


def test_a_preferred_degree_on_a_flat_line_does_not_outrank_the_required_one(
    inventory: CvInventory,
) -> None:
    # A body flattened without punctuation arrives as one requirement line.
    flat = (
        "Bachelor's degree in computer science required 5+ years of experience building data "
        "pipelines for timetable planning across depots and crews SQL and Python fluency "
        "PhD in operations research a plus pension and paid leave offered"
    )
    reading = reading_of(f"Data Engineer\n\nRequirements\n{flat}", inventory)
    assert [line.whole for line in reading.requirements] == [flat]
    assert reading.education.value == "bachelor's degree, stated as must-have"


# A line after a finished sentence starts its own unit even when it opens with a glyph
# the reader does not mark, a lower-case letter, or a marker set flush to its text.
LINES_AFTER_A_FINISHED_SENTENCE = [
    pytest.param(
        "· Bachelor's degree in a related field.\n· Strong organizational skills.",
        (
            (fit.Stance.MUST_HAVE, "· Bachelor's degree in a related field."),
            (fit.Stance.MUST_HAVE, "· Strong organizational skills."),
        ),
        id="items opening with a middle dot",
    ),
    pytest.param(
        "Experience deploying models to production.\no 1 to 5 years of ML experience.",
        (
            (fit.Stance.MUST_HAVE, "Experience deploying models to production."),
            (fit.Stance.MUST_HAVE, "o 1 to 5 years of ML experience."),
        ),
        id="a sub-item opening with a letter o",
    ),
    pytest.param(
        "Python for data work - pandas and scripting.\n"
        "dbt or equivalent experience strongly preferred.",
        (
            (fit.Stance.MUST_HAVE, "Python for data work - pandas and scripting."),
            (fit.Stance.PREFERRED, "dbt or equivalent experience strongly preferred."),
        ),
        id="an item opening in lower case",
    ),
    pytest.param(
        "CPR and First Aid certification.\n*ACLS certification is a requirement",
        (
            (fit.Stance.MUST_HAVE, "CPR and First Aid certification."),
            (fit.Stance.MUST_HAVE, "*ACLS certification is a requirement"),
        ),
        id="an item opening with a flush asterisk",
    ),
]


@pytest.mark.parametrize(("body", "expected"), LINES_AFTER_A_FINISHED_SENTENCE)
def test_a_line_after_a_finished_sentence_starts_its_own_unit(
    body: str, expected: tuple[tuple[fit.Stance, str], ...], inventory: CvInventory
) -> None:
    # Read as stored, so a glyph the shared layout would mark stays unmarked.
    reading = reading_of(f"Data Engineer\n\nRequirements\n{body}", inventory)
    assert tuple((line.stance, line.text) for line in reading.requirements) == expected


_ON_SITE_FORMS = ("on-site", "on" + chr(0x2011) + "site", "onsite", "in person")
_HEDGED_ARRANGEMENTS = (
    "At Examplegen, many roles are required to be performed {}; your recruiter can share "
    "the expectations for this role.",
    "At Examplegen, some roles are required to be performed {}.",
    "Most roles at Examplegen are {}.",
    "Certain positions are required to work {}.",
    "Roles at our campus are performed {}.",
    "In such positions, work is performed {} at the assigned facility.",
)
_PLACING_NO_ROLE = (
    "We believe not all work needs to be done within an office.",
    "We believe not all work needs to be done on-site.",
    "Experience in hybrid delivery environments.",
    "Our hybrid ecosystem of cloud and on-prem systems keeps the lights on.",
    "Knowledge of hybrid cloud.",
    "Hybrid cloud experience is a plus.",
    "In-person customer service experience required.",
    "On" + chr(0x2011) + "site support experience preferred.",
    "We run hybrid batch and streaming workloads.",
    "Experience with Hybrid Clouds and container platforms.",
    "Designs hybrid platforms and hybrid networks for research sites.",
    "Delivery experience of any kind (for example Waterfall, Agile, Hybrid).",
    "Experience running Agile, Waterfall, or hybrid project methodologies.",
    "Comfortable with Scrum, Kanban and hybrid frameworks.",
    "Hybrid, Agile and Waterfall experience is a plus.",
    "Approach: You will use hybrid, agile and waterfall methods.",
    "Accountabilities: Delivery: you'll run hybrid, agile and scrum projects.",
    "Familiar with hybrid project management and predictive planning.",
    "Communicate verbally by phone or in person with care teams.",
    "Answer member questions by email, phone, or in person.",
    "Present results in person or by video call.",
    "Reach families over the phone and in-person.",
    "Meet with clinicians in person, by phone or by email.",
    "Coordinate on-site execution of our annual summit.",
    "Lead on-site event logistics for customer conferences.",
    "Run in-person workshops for clinic staff.",
    "Site visits are performed on-site or remotely.",
    "Advises study teams on operational issues based on site visits and contacts.",
    "The analyst resolves queries remotely and on site.",
    "You will build relationships with on-site clients.",
    "All candidates attend an in-person final interview.",
    "Flexibility to work in-office, virtually or a combination of both.",
    "While in-office collaboration matters, not all work needs an office.",
    "Comfortable supporting remote, hybrid, and onsite users across multiple locations.",
    "Supports hybrid, on-site, or remote staff at every clinic.",
    "Please speak with your recruiter and hiring manager for more information about on-site "
    "expectations for your role and location.",
    "Talk to your recruiter about in-office expectations for this role.",
    "Ask the hiring manager about on-site requirements for your location.",
    "For more information on hybrid work policy, contact your talent partner.",
    "Your recruiting team will explain the on-site guidelines for this position.",
    *(
        f"{kind} {noun} {rule}"
        for kind in ("Site-by-design", "Office-based", "Field-based and remote-by-design")
        for noun in ("roles", "positions")
        for rule in (
            "may be eligible for a hybrid work model with at least 50% onsite at your depot.",
            "require 100% of shifts onsite at your assigned warehouse.",
            "are remote, with travel to retail stores.",
        )
    ),
    "For field-based and remote-by-design roles the ability to travel is an essential function.",
    "For site-essential, hybrid or remote-by-design jobs, see the policy on the careers site.",
    "Our structure includes site-essential, site-by-design, field-based and remote-by-design jobs.",
)
_TRAILING = ("", " We ship dashboards every week.")


def _unplaced_arrangements() -> list[str]:
    hedged = [template.format(form) for template in _HEDGED_ARRANGEMENTS for form in _ON_SITE_FORMS]
    return sorted(
        {
            cased + trailing
            for sentence in (*hedged, *_PLACING_NO_ROLE)
            for cased in _cased(sentence)
            for trailing in _TRAILING
        }
    )


@pytest.mark.parametrize("sentence", _unplaced_arrangements())
def test_an_arrangement_word_in_a_sentence_placing_no_role_leaves_the_arrangement_unknown(
    sentence: str, inventory: CvInventory
) -> None:
    posting = f"Data Analyst\n\nAbout the role\n{sentence}\n\nRequirements:\n- SQL"
    assert reading_of(posting, inventory).location.value is None


@pytest.mark.parametrize("sentence", [*_HEDGED_ARRANGEMENTS[:2], *_PLACING_NO_ROLE[1:3]])
def test_an_unplaced_arrangement_word_leaves_a_remote_role_remote(
    sentence: str, inventory: CvInventory
) -> None:
    body = f"{sentence.format('on-site')} This position is fully remote."
    posting = f"Data Analyst\n\nAbout the role\n{body}\n\nRequirements:\n- SQL"
    assert reading_of(posting, inventory).location.value == "remote"


_WORK_TYPE_POLICY = (
    "Our structure includes site-essential, site-by-design, field-based and remote-by-design "
    "jobs. Site-essential roles require 100% of shifts onsite at your assigned facility. "
    "Site-by-design roles may be eligible for a hybrid work model with at least 50% onsite at "
    "your assigned facility. For field-based and remote-by-design roles the ability to travel "
    "to retail stores is an essential job function."
)


@pytest.mark.parametrize(
    ("role", "standing"),
    [
        ("", fit.Standing.UNKNOWN),
        ("This position is hybrid, 50% onsite. ", fit.Standing.INELIGIBLE),
        (
            "This is a hybrid role with 3 days per week in the Example City depot. ",
            fit.Standing.INELIGIBLE,
        ),
    ],
)
def test_an_employer_policy_for_named_work_types_does_not_refuse_a_role_it_never_types(
    role: str, standing: fit.Standing, inventory: CvInventory
) -> None:
    posting = (
        f"Route Planning Analyst\n\nField - Freedonia\n\nAbout the role\n{role}You will plan "
        f"freight routes for a retail logistics network.\n\n{_WORK_TYPE_POLICY}\n\n"
        "Requirements:\n- SQL"
    )
    refusing = JobSearchParameters(
        arrangement=Arrangement(remote=Acceptance.PREFERRED, hybrid=Acceptance.REFUSED)
    )
    reading = fit.read(posting, inventory, parameters=refusing, today=TODAY)
    found = {gate.name: gate.standing for gate in fit.gates(reading, refusing)}
    assert found["arrangement"] is standing


_AMENITIES = (
    "amenities",
    "parking",
    "gym",
    "fitness center",
    "cafeteria",
    "cafe",
    "dining",
    "childcare",
    "daycare",
    "clinic",
    "EV charging",
    "bike storage",
    "showers",
    "lab space",
)
_AMENITY_SENTENCES = (
    "Our Example City campus has free {form} {noun}.",
    "Enjoy {form} {noun} and a quiet library.",
    "The building includes {form} {noun} for everyone.",
)
_ON_SITE_WRITINGS = ("on-site", "onsite", "on site")
_OTHER_PARTY_ON_SITE = (
    "Our Springfield location is home to our research organization and a key scientific hub "
    "with strong leadership based {form}.",
    "Our team is based {form} at the Riverbend campus.",
    "Scientists working {form} at our campus run the assay labs.",
    "Our headquarters in Example City offers an open collaborative office environment. "
    "Employees benefit from convenient {form} amenities, including free {form} parking and "
    "gym facilities in the building.",
)


def _unplaced_facilities() -> list[str]:
    amenities = [
        template.format(form=form, noun=noun)
        for template in _AMENITY_SENTENCES
        for form in _ON_SITE_WRITINGS
        for noun in _AMENITIES
    ]
    parties = [
        template.format(form=form)
        for template in _OTHER_PARTY_ON_SITE
        for form in _ON_SITE_WRITINGS
    ]
    return sorted({cased for sentence in (*amenities, *parties) for cased in _cased(sentence)})


@pytest.mark.parametrize("sentence", _unplaced_facilities())
def test_an_on_site_facility_or_another_party_on_site_leaves_the_arrangement_unknown(
    sentence: str, inventory: CvInventory
) -> None:
    posting = f"Data Analyst\n\nAbout the company\n{sentence}\n\nRequirements:\n- SQL"
    assert reading_of(posting, inventory).location.value is None


_ROLE_ON_SITE_BESIDE_FACILITIES = (
    ("This is an {form} role.", "on site"),
    ("You will work {form} at our Boston campus.", "on site"),
    ("The position is based {form} five days a week.", "on site"),
    ("{Form} presence required.", "on site"),
    ("This role is {form}; free parking is available.", "on site"),
    ("This role is {form}; free {form} parking is available.", "on site"),
    ("Hybrid: 3 days {form}.", "hybrid"),
    ("This is an {form} role. Our leadership team is based {form}.", "on site"),
    ("You will work {form} in Denver. Enjoy {form} parking and a gym.", "on site"),
    ("You will join our team based {form} in Denver.", "on site"),
    ("{Form} data center technician, 5 days.", "on site"),
    ("This is an {form} data center role.", "on site"),
    ("Work model: {Form} data center", "on site"),
    ("Location: {Form} data center, Ashburn, VA", "on site"),
    ("You will work in our {form} data center every day.", "on site"),
    ("Position requires {form} data center work.", "on site"),
    ("This role is {form} at our Austin data center.", "on site"),
)


@pytest.mark.parametrize(
    ("sentence", "expected"),
    sorted(
        {
            (cased, expected)
            for template, expected in _ROLE_ON_SITE_BESIDE_FACILITIES
            for form in _ON_SITE_WRITINGS
            for cased in _cased(template.format(form=form, Form=form.capitalize()))
        }
    ),
)
def test_a_role_on_site_beside_facilities_keeps_its_arrangement(
    sentence: str, expected: str, inventory: CvInventory
) -> None:
    posting = f"Data Analyst\n\nAbout the role\n{sentence}\n\nRequirements:\n- SQL"
    assert reading_of(posting, inventory).location.value == expected


_OPERATIONS_DUTIES = (
    "Manages the validation of {form} data center operations.",
    "Serves as the subject matter expert for {form} operations of mission-critical systems.",
    "Owns lifecycle management for {form} systems.",
    "Provides {form} manufacturing support and oversees {form} studies.",
    "Maintains {form} infrastructure and {form} equipment.",
    "Monitors {form} servers across {form} data centers.",
    "Supports cloud-based and {form} infrastructure.",
    "Manages role-based access to {form} systems.",
    "Owns job-scheduling for {form} servers.",
    "Lead or support technology transfers, including gap analyses, risk assessments, "
    "technical documentation, and {form} manufacturing support.",
    "Serve as the technical owner for assigned domains, leading lifecycle management for "
    "{form} systems and advising stakeholders.",
    *(
        f"Acts as the engineering SME for {{form}} operations {follower} critical facility "
        "systems, operations, and uptime."
        for follower in (
            "related to",
            "relating to",
            "supporting",
            "including",
            "across",
            "that support",
            "which support",
            "used for",
        )
    ),
    "Lead {form} operations and vendor coordination.",
    "Works in partnership with vendors on {form} systems.",
    "Works in close collaboration with vendors on {form} systems.",
    "Partner with teams on {form} systems at scale.",
    "Owns {form} systems at the site level",
    "Manages {form} systems at the site level across 40 hospitals",
    "Experience with {form} systems at the lab level",
    "Standardize {form} operations at the facility level",
    "Collaborate with staff at our plants to modernize {form} systems",
    "Partner with operations leaders at our sites to improve {form} infrastructure",
    "Support users at our clinics and sites with {form} infrastructure",
    "Coordinate with IT at the corporate office on {form} systems",
    "Support recruiters hiring for {form} manufacturing support",
    "We are hiring for a Data Analyst to own {form} systems",
    "Partner with {form} operations at our sites",
    "Report on {form} equipment at our labs",
)
_ROLE_PLACING_DUTIES = (
    "You will work in our {form} data center every day.",
    "Position requires {form} data center work.",
    "This role is {form} at our Austin data center.",
    "{Form} data center role.",
    "Based {form} at our Ashburn data center.",
    "Work model: {Form} data center operations.",
    "Location: {Form} data center operations, Ashburn, VA",
    "Based at our {form} data center.",
    "Work in our {form} data center.",
    "Requires hands-on work at our {form} data center.",
    "Job site: our {form} data center.",
    "You will run our {form} data center.",
    "{Form} data center technician, 5 days.",
    "This is an {form} role supporting operations.",
    "The position is {form}, supporting systems.",
    "Work location: {Form} data center.",
    "Our {form} data center, where you will work daily, runs around the clock.",
    "100% {form} operations.",
    "Fully {form} operations.",
    "5 days per week {form} operations.",
    "{Form} operations, Monday to Friday.",
    "{Form} operations: 5 days/week.",
    "{Form} operations, 5 days a week, at our main campus.",
    "Work location - {Form} data center.",
    "Workplace type | {Form} operations",
    "Schedule:  {Form} operations",
    "Work Location: Fully {form} operations, Springfield",
    "{Form} operations.",
    "Lead {form} operations at our Riverbend campus.",
    "Lead {form} operations at the site.",
    "Ability to work {form} operations, nights and weekends.",
    "We are hiring for {form} operations in Springfield.",
    "Lead {form} operations at our campus",
    "Candidates must work in Springfield, overseeing {form} operations",
    "Work in Springfield supporting {form} operations",
    "Work in a regulated plant overseeing {form} manufacturing support",
    "Work in shifts across {form} operations",
)
# A duty as a bullet, as prose, and after an inline label run into its paragraph.
_DUTY_SHAPES = (
    "Responsibilities\n- {duty}",
    "About the role\n- {duty}",
    "About the role\nWe run the platform for our plants. {duty}",
    "About the role\nWe run the platform for our plants. Accountabilities: Platform "
    "Ownership: {duty}",
)


def _duty_postings(templates: tuple[str, ...]) -> list[str]:
    return sorted(
        {
            f"Data Analyst\n\n{shape.format(duty=cased)}\n\nRequirements:\n- SQL"
            for template in templates
            for form in _ON_SITE_WRITINGS
            for cased in _cased(template.format(form=form, Form=form.capitalize()))
            for shape in _DUTY_SHAPES
        }
    )


@pytest.mark.parametrize("posting", _duty_postings(_OPERATIONS_DUTIES))
def test_a_duty_on_on_site_operations_leaves_the_arrangement_unknown(
    posting: str, inventory: CvInventory
) -> None:
    assert reading_of(posting, inventory).location.value is None


@pytest.mark.parametrize("posting", _duty_postings(_ROLE_PLACING_DUTIES))
def test_a_duty_placing_the_role_on_site_keeps_its_arrangement(
    posting: str, inventory: CvInventory
) -> None:
    assert reading_of(posting, inventory).location.value == "on site"


@pytest.mark.parametrize(
    "title",
    sorted(
        {
            cased
            for template in (
                "Data Center Technician - {Form} Operations",
                "Data Center Technician ({Form} Operations)",
            )
            for form in _ON_SITE_WRITINGS
            for cased in _cased(template.format(Form=form.capitalize()))
        }
    ),
)
def test_a_title_naming_on_site_operations_reads_on_site(
    title: str, inventory: CvInventory
) -> None:
    posting = f"{title}\n\nAbout the role\nWe run the platform.\n\nRequirements:\n- SQL"
    assert reading_of(posting, inventory).location.value == "on site"


@pytest.mark.parametrize(
    ("schedule", "expected"),
    [("", None), ("You will work three days a week in the office.", "hybrid")],
)
def test_duties_on_on_site_systems_do_not_refuse_a_posting_as_on_site(
    schedule: str, expected: str | None, inventory: CvInventory
) -> None:
    posting = (
        f"Infrastructure Analyst\n\nAbout the role\n{schedule}\n\nResponsibilities\n"
        "- Manages the validation of on-site data center operations.\n"
        "- Owns lifecycle management for onsite systems.\n\nRequirements:\n- SQL"
    )
    refusing = JobSearchParameters(
        arrangement=Arrangement(remote=Acceptance.PREFERRED, on_site=Acceptance.REFUSED)
    )
    reading = fit.read(posting, inventory, parameters=refusing, today=TODAY)
    found = {gate.name: gate.standing for gate in fit.gates(reading, refusing)}
    assert reading.location.value == expected
    assert found["arrangement"] is not fit.Standing.INELIGIBLE


_REFUSING_IN_PLACE = JobSearchParameters(
    arrangement=Arrangement(
        remote=Acceptance.PREFERRED, hybrid=Acceptance.REFUSED, on_site=Acceptance.REFUSED
    )
)
_WISHED_ATTENDANCE = (
    "A candidate who can work {form} will be given preference.",
    "Candidates able to work {form} will receive preference.",
    "Preference will be given to candidates who can work {form}.",
    "Candidates who can work {form} are preferred.",
    "{Form} preferred.",
    "{Form} work is preferred but not required.",
    "Ideally, you can work {form} at our Example City office.",
    "Preferably {form} at our Example City office.",
)


def _arrangement_standing(posting: str, wanted: JobSearchParameters) -> fit.Standing:
    reading = fit.read(posting, parameters=wanted, today=TODAY)
    return {gate.name: gate.standing for gate in fit.gates(reading, wanted)}["arrangement"]


@pytest.mark.parametrize(
    "sentence",
    sorted(
        {
            cased
            for template in _WISHED_ATTENDANCE
            for form in _ON_SITE_WRITINGS
            for cased in _cased(template.format(form=form, Form=form.capitalize()))
        }
    ),
)
def test_a_wish_for_on_site_work_does_not_refuse_a_posting(sentence: str) -> None:
    posting = (
        "Program Manager\n\nLocation Specific Information\nThis role is located in Example "
        f"City. {sentence}\n\nAbout the role\nYou will run sequencing programs.\n\n"
        "Requirements:\n- PMP"
    )
    assert fit.read(posting, today=TODAY).location.value is None
    assert _arrangement_standing(posting, _REFUSING_IN_PLACE) is not fit.Standing.INELIGIBLE


@pytest.mark.parametrize(
    "body",
    sorted(
        {
            template.format(label=label, line=f"{marker}{sentence}")
            for heading in (
                "Preferred",
                "Preferred qualifications",
                "Nice to have",
                "Optional qualifications",
            )
            for form in (*_ON_SITE_WRITINGS, "in-office")
            for cadence in ("", " three days per week")
            for label, sentence in zip(
                _cased(f"{heading}:"),
                _cased(f"ability to work {form} in Boston{cadence}."),
                strict=True,
            )
            for marker in ("", "- ", "* ")
            for template in ("{label} {line}", "{label}\n{line}")
        }
        | {"Preferred Qualifications\n- Ability to work on-site in Boston."}
    ),
)
def test_a_preferred_block_attendance_capability_states_no_required_arrangement(
    body: str,
) -> None:
    posting = (
        f"Staff Engineer\n\nLocation Specific Information\n{body}\n\n"
        "About the role\nYou will build software."
    )
    reading = fit.read(posting, parameters=_REFUSING_IN_PLACE, today=TODAY)
    gates = {gate.name: gate.standing for gate in fit.gates(reading, _REFUSING_IN_PLACE)}

    assert reading.location.value is None
    assert gates["arrangement"] is fit.Standing.UNKNOWN
    assert fit.refusal(fit.gates(reading, _REFUSING_IN_PLACE)) is None


@pytest.mark.parametrize(
    ("required", "arrangement", "separator"),
    [
        *(
            (required, arrangement, separator)
            for required, arrangement in (
                ("This role is on-site in Boston.", "on site"),
                ("This role is hybrid in Boston.", "hybrid"),
                ("You will work on-site in Boston.", "on site"),
                ("Must be able to work on-site in Boston.", "on site"),
                ("On-site attendance in Boston is required.", "on site"),
                ("Must be willing to relocate to Boston.", "on site"),
            )
            for separator in ("\n- ", "\n\nAbout the role\n")
        ),
        ("Required qualifications:\n- Ability to work on-site in Boston.", "on site", "\n\n"),
    ],
)
@pytest.mark.parametrize(
    "body",
    [
        "Preferred: ability to work on-site in Boston.",
        "Preferred Qualifications\n- Ability to work on-site in Boston.",
        "Optional qualifications:\n- Ability to work on-site in Boston three days per week.",
    ],
)
def test_a_preferred_block_keeps_an_independent_attendance_commitment(
    body: str, separator: str, required: str, arrangement: str
) -> None:
    posting = (
        f"Staff Engineer\n\nLocation Specific Information\n{body}{separator}{required}\n\n"
        "Responsibilities:\n- Build software."
    )
    reading = fit.read(posting, parameters=_REFUSING_IN_PLACE, today=TODAY)
    gates = {gate.name: gate.standing for gate in fit.gates(reading, _REFUSING_IN_PLACE)}

    assert reading.location.value == arrangement
    assert gates["arrangement"] is fit.Standing.INELIGIBLE
    assert reading.location.evidence
    for quote in reading.location.evidence:
        assert quote.removeprefix("...").removesuffix("...") in posting


@pytest.mark.parametrize(
    "sentence",
    sorted(
        {
            cased
            for subject, ability in (
                ("The candidate", "is"),
                ("The successful candidate", "is"),
                ("The ideal candidate", "is"),
                ("Candidates", "are"),
                ("The candidates", "are"),
            )
            for capability in (
                "can work",
                f"{ability} able to work",
                "able to work",
                f"{'has' if ability == 'is' else 'have'} the ability to work",
            )
            for form in (*_ON_SITE_WRITINGS, "in-office")
            for cadence in ("", " three days per week")
            for cased in _cased(f"{subject} {capability} {form} in Boston{cadence}.")
        }
    ),
)
def test_a_preferred_candidate_capability_states_no_required_attendance(sentence: str) -> None:
    posting = (
        f"Staff Engineer\n\nPreferred Qualifications\n- {sentence}\n\n"
        "About the role\nYou will build software."
    )
    reading = fit.read(posting, parameters=_REFUSING_IN_PLACE, today=TODAY)
    gates = {gate.name: gate.standing for gate in fit.gates(reading, _REFUSING_IN_PLACE)}

    assert reading.location.value is None
    assert gates["arrangement"] is fit.Standing.UNKNOWN
    assert fit.refusal(fit.gates(reading, _REFUSING_IN_PLACE)) is None


@pytest.mark.parametrize(
    "sentence",
    sorted(
        {
            cased
            for subject, agreement in (
                ("The candidate", "is"),
                ("The successful candidate", "is"),
                ("The ideal candidate", "is"),
                ("Candidates", "are"),
                ("The candidates", "are"),
            )
            for commitment in ("will work", "must work", f"{agreement} required to work")
            for form in (*_ON_SITE_WRITINGS, "in-office")
            for cased in _cased(f"{subject} {commitment} {form} in Boston.")
        }
    ),
)
def test_a_preferred_candidate_commitment_keeps_required_attendance(sentence: str) -> None:
    posting = (
        f"Staff Engineer\n\nPreferred Qualifications\n- {sentence}\n\n"
        "About the role\nYou will build software."
    )
    reading = fit.read(posting, parameters=_REFUSING_IN_PLACE, today=TODAY)
    gates = {gate.name: gate.standing for gate in fit.gates(reading, _REFUSING_IN_PLACE)}

    assert reading.location.value == "on site"
    assert gates["arrangement"] is fit.Standing.INELIGIBLE
    assert reading.location.evidence
    for quote in reading.location.evidence:
        assert quote.removeprefix("...").removesuffix("...") in posting


_INVITED_OR_WISHED = (
    "Candidates local to or willing to relocate to Springfield, IL are encouraged to apply.",
    "Candidates willing to relocate to Springfield, IL are encouraged to apply.",
    "Applicants local to Springfield or willing to relocate to Springfield are welcome to apply.",
    *(
        f"{form} ({wish})"
        for form in ("On-site", "Onsite", "On site", "In-office")
        for wish in ("preferred", "Preferred")
    ),
    "On site - preferred",
)


@pytest.mark.parametrize(
    "line",
    sorted(
        {
            f"{marker}{cased}"
            for line in _INVITED_OR_WISHED
            for cased in _cased(line)
            for marker in ("", "- ")
        }
    ),
)
def test_a_relocation_invitation_or_a_bare_wished_attendance_states_no_arrangement(
    line: str,
) -> None:
    posting = f"Program Manager\n\nAbout the role\n{line}\n\nRequirements:\n- PMP"
    assert fit.read(posting, today=TODAY).location.value is None
    assert _arrangement_standing(posting, _REFUSING_IN_PLACE) is not fit.Standing.INELIGIBLE


_IN_PERSON_BELIEFS = tuple(
    template.format(form=form, Form=form.capitalize())
    for template in (
        "At Northwind, we believe {form} connection builds our culture.",
        "We believe that when people connect {form}, we learn faster and build trust.",
        "As we grow, we have always believed that an {form} culture is key to our success.",
        "We've always believed {form} interactions make better products.",
        "We strongly believe in the power of working {form}.",
    )
    for form in ("in-person", "in person", "onsite", "on-site", "in-office")
)
_ROLES_STATE_THEIR_OWN = (
    "For most roles, we require a minimum of 4 in-office days per week. Remote or field-based "
    "roles will state their arrangement in the job posting."
)


@pytest.mark.parametrize("belief", _IN_PERSON_BELIEFS)
def test_a_belief_in_in_person_connection_does_not_refuse_a_posting(belief: str) -> None:
    posting = (
        "Clinical Systems Specialist\n\nAbout the role\nYou will support lab systems.\n\n"
        f"Requirements:\n- SQL\n\nAbout us\n{belief} {_ROLES_STATE_THEIR_OWN}"
    )
    assert fit.read(posting, today=TODAY).location.value is None
    assert _arrangement_standing(posting, _REFUSING_IN_PLACE) is not fit.Standing.INELIGIBLE


@pytest.mark.parametrize(
    ("role", "policy", "standing"),
    [
        *(
            ("This is a hybrid role.", f"{opener} {cadence}.", fit.Standing.ELIGIBLE)
            for opener in (
                "For most roles, we require",
                "For many of our roles, we require",
                "Most roles at Northwind require",
            )
            for cadence in ("a minimum of 4 days per week in the office", "4 days a week in office")
        ),
        (
            "This role is hybrid with 4 days a week in the office.",
            "",
            fit.Standing.INELIGIBLE,
        ),
    ],
)
def test_office_days_set_for_most_roles_do_not_limit_this_hybrid_role(
    role: str, policy: str, standing: fit.Standing
) -> None:
    posting = (
        f"Clinical Systems Specialist\n\nAbout the role\n{role} You will support lab systems. "
        f"{policy}\n\nRequirements:\n- SQL"
    )
    two_days = JobSearchParameters(
        arrangement=Arrangement(
            remote=Acceptance.PREFERRED, hybrid=Acceptance.ACCEPTABLE, maximum_office_days=2
        )
    )
    reading = fit.read(posting, parameters=two_days, today=TODAY)
    found = {gate.name: gate.standing for gate in fit.gates(reading, two_days)}
    assert reading.location.value == "hybrid"
    assert found["office attendance"] is standing


_ROLE_INCLUSIONS = tuple(
    (middle.format(qualifier=qualifier), ending.format(qualifier=qualifier))
    for qualifier in (
        "including this one",
        "including this role",
        "including this position",
        "including this job",
        "including the incumbent's role",
    )
    for middle, ending in (
        (", {qualifier},", ", {qualifier}"),
        (" - {qualifier} -", " - {qualifier}"),
        (" ({qualifier})", " ({qualifier})"),
    )
)


@pytest.mark.parametrize(
    ("sentence", "arrangement"),
    sorted(
        {
            (cased, arrangement)
            for quantifier in ("Most", "Many")
            for word, arrangement in (
                ("hybrid", "hybrid"),
                *((form, "on site") for form in _ON_SITE_WRITINGS),
            )
            for middle, ending in _ROLE_INCLUSIONS
            for template in (
                "{quantifier} roles{middle} are {word} in Boston.",
                "{quantifier} roles are {word}{middle} in Boston.",
                "{quantifier} roles are {word} in Boston{ending}.",
            )
            for cased in _cased(
                template.format(quantifier=quantifier, middle=middle, ending=ending, word=word)
            )
        }
        | {
            ("This role, like most roles, is hybrid in Boston.", "hybrid"),
            ("This position is on-site in Boston, like many roles.", "on site"),
            ("The incumbent in this role works onsite in Boston, like most roles.", "on site"),
        }
    ),
)
def test_a_plural_role_policy_including_this_role_keeps_its_arrangement_and_place(
    sentence: str, arrangement: str
) -> None:
    posting = (
        f"Staff Engineer\n\nLocation Specific Information\n{sentence}\n\n"
        "About the role\nBuild software."
    )
    reading = fit.read(posting, parameters=_REFUSING_IN_PLACE, today=TODAY)
    gates = {gate.name: gate.standing for gate in fit.gates(reading, _REFUSING_IN_PLACE)}

    assert reading.location.value == arrangement
    assert gates["arrangement"] is fit.Standing.INELIGIBLE
    assert fit.refusal(fit.gates(reading, _REFUSING_IN_PLACE)) is not None
    assert reading.location.evidence
    for quote in reading.location.evidence:
        assert quote.removeprefix("...").removesuffix("...") in posting


@pytest.mark.parametrize(
    "sentence",
    sorted(
        {
            cased
            for quantifier in ("Most", "Many")
            for middle, ending in _ROLE_INCLUSIONS
            for template in (
                "{quantifier} roles{middle} are hybrid requiring four days per week in the office.",
                "{quantifier} roles are hybrid requiring four days per week in the office{ending}.",
            )
            for cased in _cased(
                template.format(quantifier=quantifier, middle=middle, ending=ending)
            )
        }
        | {"This role, like most roles, requires four days per week in the office."}
    ),
)
def test_a_plural_role_policy_including_this_role_keeps_its_required_office_days(
    sentence: str,
) -> None:
    posting = f"Staff Engineer\n\nAbout the role\n{sentence}\nBuild software."
    two_days = JobSearchParameters(
        arrangement=Arrangement(
            remote=Acceptance.PREFERRED, hybrid=Acceptance.ACCEPTABLE, maximum_office_days=2
        )
    )
    reading = fit.read(posting, parameters=two_days, today=TODAY)
    gates = {gate.name: gate.standing for gate in fit.gates(reading, two_days)}

    assert gates["office attendance"] is fit.Standing.INELIGIBLE
    assert reading.arrangement_fit.value is not None
    assert "4 office days against your 2" in reading.arrangement_fit.value
    assert reading.location.evidence
    for quote in reading.location.evidence:
        assert quote.removeprefix("...").removesuffix("...") in posting


@pytest.mark.parametrize(
    "sentence",
    sorted(
        {
            cased
            for quantifier in ("Most", "Many")
            for template in (
                "{quantifier} roles are hybrid in Boston.",
                "{quantifier} roles are on-site in Boston.",
                "{quantifier} roles are hybrid requiring four days per week in the office.",
                "{quantifier} roles are hybrid in Boston; your recruiter can share "
                "the expectations for this role.",
            )
            for cased in _cased(template.format(quantifier=quantifier))
        }
    ),
)
def test_a_plural_role_policy_without_this_role_binding_leaves_attendance_unknown(
    sentence: str,
) -> None:
    posting = f"Staff Engineer\n\nAbout the role\n{sentence}\nBuild software."
    reading = fit.read(posting, parameters=_REFUSING_IN_PLACE, today=TODAY)
    gates = {gate.name: gate.standing for gate in fit.gates(reading, _REFUSING_IN_PLACE)}

    assert reading.location.value is None
    assert gates["arrangement"] is fit.Standing.UNKNOWN
    assert fit.refusal(fit.gates(reading, _REFUSING_IN_PLACE)) is None


@pytest.mark.parametrize(
    "sentence",
    sorted(
        {
            cased
            for exclusion in ("except", "excluding", "not including", "other than")
            for role in ("this role", "this position", "this one")
            for middle, ending in (
                (", {qualifier},", ", {qualifier}"),
                (" - {qualifier} -", " - {qualifier}"),
                (" ({qualifier})", " ({qualifier})"),
            )
            for arrangement in (
                "hybrid in Boston",
                "on-site in Boston",
                "on-site three days per week",
                "hybrid requiring four days per week in the office",
            )
            for template in (
                "Most roles{middle} are {arrangement}.",
                "Most roles are {arrangement}{ending}.",
            )
            for cased in _cased(
                template.format(
                    middle=middle.format(qualifier=f"{exclusion} {role}"),
                    ending=ending.format(qualifier=f"{exclusion} {role}"),
                    arrangement=arrangement,
                )
            )
        }
    ),
)
def test_a_plural_role_policy_excluding_this_role_states_no_arrangement_or_office_limit(
    sentence: str,
) -> None:
    posting = f"Staff Engineer\n\nAbout the role\n{sentence}\nBuild software."
    reading = fit.read(posting, parameters=_REFUSING_IN_PLACE, today=TODAY)
    gates = {gate.name: gate.standing for gate in fit.gates(reading, _REFUSING_IN_PLACE)}

    assert reading.location.value is None
    assert gates["arrangement"] is fit.Standing.UNKNOWN
    assert gates["office attendance"] is fit.Standing.UNKNOWN
    assert fit.refusal(fit.gates(reading, _REFUSING_IN_PLACE)) is None

    two_days = JobSearchParameters(
        arrangement=Arrangement(
            remote=Acceptance.PREFERRED, hybrid=Acceptance.ACCEPTABLE, maximum_office_days=2
        )
    )
    stated_role = f"{posting}\nThis role is hybrid in Boston."
    stated = fit.read(stated_role, parameters=two_days, today=TODAY)
    stated_gates = {gate.name: gate.standing for gate in fit.gates(stated, two_days)}

    assert stated.location.value == "hybrid"
    assert stated_gates["arrangement"] is fit.Standing.ELIGIBLE
    assert stated_gates["office attendance"] is fit.Standing.ELIGIBLE
    assert stated.arrangement_fit.value == Acceptance.ACCEPTABLE.value


@pytest.mark.parametrize(
    ("sentence", "included"),
    sorted(
        {
            (cased.replace("WHITESPACE", gap), positive)
            for prefix in ("not", "without")
            for gap in (" ", "  ", "   ", "\t", "\t\t", " \t  ")
            for role in ("this role", "this one")
            for shape in (", {qualifier}", " - {qualifier}", " ({qualifier})")
            for arrangement in (
                "hybrid in Boston",
                "on-site three days per week",
                "hybrid requiring four days per week in the office",
            )
            for cased, positive in zip(
                _cased(
                    f"Most roles are {arrangement}"
                    f"{shape.format(qualifier=f'{prefix}WHITESPACEincluding {role}')}."
                ),
                _cased(
                    f"Most roles are {arrangement}{shape.format(qualifier=f'including {role}')}."
                ),
                strict=True,
            )
        }
    ),
)
def test_a_plural_role_policy_negates_inclusion_across_whitespace(
    sentence: str, included: str
) -> None:
    posting = f"Staff Engineer\n\nAbout the role\n{sentence}\nBuild software."
    reading = fit.read(posting, parameters=_REFUSING_IN_PLACE, today=TODAY)
    gates = {gate.name: gate.standing for gate in fit.gates(reading, _REFUSING_IN_PLACE)}

    assert reading.location.value is None
    assert gates["arrangement"] is fit.Standing.UNKNOWN
    assert gates["office attendance"] is fit.Standing.UNKNOWN
    assert fit.refusal(fit.gates(reading, _REFUSING_IN_PLACE)) is None

    two_days = JobSearchParameters(
        arrangement=Arrangement(
            remote=Acceptance.PREFERRED, hybrid=Acceptance.ACCEPTABLE, maximum_office_days=2
        )
    )
    stated = fit.read(
        f"{posting}\nThis role is hybrid in Boston.", parameters=two_days, today=TODAY
    )
    stated_gates = {gate.name: gate.standing for gate in fit.gates(stated, two_days)}

    assert stated_gates["arrangement"] is fit.Standing.ELIGIBLE
    assert stated_gates["office attendance"] is fit.Standing.ELIGIBLE
    assert stated.arrangement_fit.value == Acceptance.ACCEPTABLE.value

    included_posting = f"Staff Engineer\n\nAbout the role\n{included}\nBuild software."
    bound = fit.read(included_posting, parameters=_REFUSING_IN_PLACE, today=TODAY)

    assert fit.refusal(fit.gates(bound, _REFUSING_IN_PLACE)) is not None
    assert bound.location.evidence
    for quote in bound.location.evidence:
        assert quote.removeprefix("...").removesuffix("...") in included_posting


@pytest.mark.parametrize(
    ("sentence", "arrangement"),
    [
        ("This role is hybrid in Boston.", "hybrid"),
        ("This position is on-site in Boston.", "on site"),
        ("The incumbent in this role works onsite in Boston.", "on site"),
        ("This role requires four days per week in the office.", "hybrid"),
    ],
)
def test_a_direct_role_attendance_statement_keeps_its_required_arrangement(
    sentence: str, arrangement: str
) -> None:
    posting = f"Staff Engineer\n\nAbout the role\n{sentence}\nBuild software."
    reading = fit.read(posting, parameters=_REFUSING_IN_PLACE, today=TODAY)
    gates = {gate.name: gate.standing for gate in fit.gates(reading, _REFUSING_IN_PLACE)}

    assert reading.location.value == arrangement
    assert gates["arrangement"] is fit.Standing.INELIGIBLE


_HYBRID_DESCRIBING_THE_WORK = (
    "Experience developing digital, live and hybrid medical education programs.",
    "You will plan hybrid events for our customer community.",
    "Plan virtual and hybrid events and webinars.",
    "Deliver hybrid learning programs for field clinicians.",
    "Run hybrid meetings and conferences for the sales team.",
    "Design hybrid training sessions and course formats.",
    "Develop programs across digital, live virtual, and/or in-person formats.",
    "Deliver training in virtual and/or in-person sessions.",
    "Facilitate workshops remotely and/or on-site.",
    "Run virtual, in-person, or hybrid events.",
)
_FORMAT_ITEMS = (
    ("in-person", "hybrid"),
    ("virtual", "hybrid"),
    ("live virtual", "in-person", "hybrid"),
)


def _format_lists() -> tuple[str, ...]:
    """Return lists of three or four delivery formats led by a word naming no arrangement."""
    lists = {
        f"{', '.join((lead, *items[:-1]))}{comma} {join} {items[-1]}"
        for lead in ("digital", "on-demand")
        for items in _FORMAT_ITEMS
        for join in ("and", "or", "and/or")
        for comma in (",", "")
    }
    return tuple(
        template.format(listed)
        for listed in sorted(lists)
        for template in ("We deliver programs that are {}.", "- Programs: {}", "Run {} events.")
    )


_HYBRID_GOVERNING_THE_ROLE = (
    "This is a hybrid role.",
    "This hybrid role is primarily customer-facing.",
    "Hybrid schedule, 3 days in office.",
    "Work model: Hybrid",
    "Hybrid position based in Denver.",
    "You will work hybrid from our Denver office.",
    "We offer a hybrid working experience with 3 office days.",
    "This role follows a hybrid format.",
)


@pytest.mark.parametrize(
    ("sentence", "expected"),
    sorted(
        {
            (cased, expected)
            for sentences, expected in (
                ((*_HYBRID_DESCRIBING_THE_WORK, *_format_lists()), None),
                (_HYBRID_GOVERNING_THE_ROLE, "hybrid"),
            )
            for sentence in sentences
            for cased in _cased(sentence)
        },
        key=str,
    ),
)
def test_hybrid_is_an_arrangement_only_when_it_governs_the_role(
    sentence: str, expected: str | None, inventory: CvInventory
) -> None:
    posting = f"Events Manager\n\nAbout the role\n{sentence}\n\nRequirements:\n- Planning"
    assert reading_of(posting, inventory).location.value == expected


_UNPLACED_HYBRID = (
    "This is a hybrid role.",
    "This hybrid role is primarily customer-facing.",
    "Work model: Hybrid",
    "Location: United States\nThis is a hybrid role.",
    "Hybrid, United States",
    "Hybrid - Flexible",
    "Hybrid (flexible schedule)",
    "Work model - Hybrid",
    "Workplace type: Hybrid",
    "This is a hybrid role.\n- Python, Go, and SQL skills",
    "This is a hybrid role. Strong SQL, BI, and Excel skills.",
    "This is a hybrid role.\n- Experience with AWS, GCP, or Azure",
    "This is a hybrid role.\n- Fluent in Excel, PowerPoint, and Word",
    "This is a hybrid role in clinical informatics, bridging data and care teams.",
    "This is a hybrid role.\n- Own data pipelines, on-call rotations, and alerting.",
    "This is a hybrid role.\n- Run ETL, de-identification, and analytics, in-house.",
    "This is a hybrid role in Data Engineering.",
    "This is a hybrid role in Product Management.",
    "This is a hybrid role.\n- Partner with Initech, Co. and Globex on delivery.",
    "This is a hybrid role.\n- Partner with Initech, Co. & Globex on delivery.",
)
_PLACED_HYBRID = (
    "This is a hybrid role in Austin, TX-Hybrid.",
    "This is a hybrid role in London, UK.",
    "Austin, TX-Hybrid",
    "London, UK-Hybrid",
    "This is a hybrid role in Chicago.",
    "Hybrid role in Chicago.",
    "This hybrid position is in Denver.",
    "This is a hybrid role in Toronto, ON.",
    "This is a hybrid role in our Springfield office.",
    "Hybrid schedule, 3 days in office.",
    "Hybrid position based in Denver.",
    "Work model: Hybrid\nLocation: Denver, CO",
    "Hybrid: 2 days a week on site.",
    "Hybrid, 3 days per week in our Austin, TX office.",
    "This is a hybrid role based in the Denver area.",
    "Work model: Hybrid\nLocation: Denver",
    "Work model: Hybrid\nLocation - Denver, CO",
    "Denver, CO - Hybrid",
    "This hybrid role works from our Boston location.",
    "This is a hybrid role; you must live within commuting distance of Denver.",
    "This is a hybrid role. Candidates must reside within 50 miles of Atlanta.",
    "hybrid position based in denver.",
    "Travel / Work Environment / Physical Requirements\n"
    "- This is a hybrid role requiring onsite attendance two (2) days per week",
    "Work Environment\n- Hybrid, two mandatory days at the Springfield Office.",
    "In addition to base salary, we offer equity, comprehensive benefits, and generous perks."
    " We hire in Springfield with a hybrid model requiring 3 days per week in office.",
    "Palo Alto - Hybrid",
    "Denver (Hybrid)",
    "- Hybrid - Porto",
    "- Hybrid (Denver)",
    "- Hybrid, Easttown, Northland",
    "- Hybrid Based (NY or NJ)",
    "This is a hybrid role. Office at Springfield campus.",
    "- Trial Supplies Associate - Hybrid Based (NY or NJ) - Client Dedicated",
)


@pytest.mark.parametrize(
    ("body", "standing"),
    sorted(
        {
            (cased, standing)
            for bodies, standing in (
                (_UNPLACED_HYBRID, fit.Standing.UNKNOWN),
                (_PLACED_HYBRID, fit.Standing.INELIGIBLE),
            )
            for body in bodies
            for cased in _cased(body)
        },
        key=str,
    ),
)
def test_a_refused_hybrid_refuses_only_beside_an_office_place_or_day_count(
    body: str, standing: fit.Standing
) -> None:
    posting = f"Account Manager\n\nAbout the role\n{body}\n\nRequirements:\n- CRM"
    assert _arrangement_standing(posting, _REFUSING_IN_PLACE) is standing


@pytest.mark.parametrize(
    "sentence",
    sorted(
        {
            cased
            for place in ("Austin, TX", "London, UK")
            for dash in ("-", " - ", "- ", " -")
            for template in (
                "{place}{dash}Hybrid",
                "This is a hybrid role in {place}{dash}Hybrid",
                "This is a hybrid role in {place}",
            )
            for end in ("", ".", ";", "!")
            if end or template != "This is a hybrid role in {place}"
            for cased in _cased(template.format(place=place, dash=dash) + end)
        }
    ),
)
def test_a_hybrid_city_and_known_code_place_the_role(sentence: str) -> None:
    posting = f"Data Analyst\n\nAbout the role\n{sentence}\n\nRequirements:\n- SQL"
    reading = fit.read(posting, parameters=_REFUSING_IN_PLACE, today=TODAY)
    gate = {item.name: item for item in fit.gates(reading, _REFUSING_IN_PLACE)}["arrangement"]

    assert reading.location.value == "hybrid"
    assert reading.arrangement_fit.value == "refused"
    assert gate.standing is fit.Standing.INELIGIBLE


@pytest.mark.parametrize(
    "sentence",
    sorted(
        {
            cased
            for body in (
                "This is a hybrid role in Data Engineering.",
                "This is a hybrid role in Product Management.",
                "This is a hybrid role. Strong SQL, BI, and Excel skills.",
                "This is a hybrid role. Partner with Initech, Co. and Globex.",
                "This is a hybrid role. Partner with Initech, Co. & Globex.",
                "This is a hybrid role. Austin.",
                "This is a hybrid role. London.",
            )
            for cased in _cased(body)
        }
    ),
)
def test_a_field_company_or_ambiguous_city_does_not_place_a_hybrid_role(sentence: str) -> None:
    posting = f"Data Analyst\n\nAbout the role\n{sentence}\n\nBenefits:\nCommuter assistance."
    reading = fit.read(posting, parameters=_REFUSING_IN_PLACE, today=TODAY)
    gate = {item.name: item for item in fit.gates(reading, _REFUSING_IN_PLACE)}["arrangement"]

    assert gate.standing is fit.Standing.UNKNOWN


@pytest.mark.parametrize(
    "benefit",
    [
        "Pre-tax commuter and parking benefits.",
        "Commuter assistance is offered.",
        "Commuting expenses may be reimbursed.",
        "Paid leave and health coverage.",
    ],
)
@pytest.mark.parametrize(
    ("role", "standing"),
    [
        (
            "This hybrid role combines customer support and implementation planning.",
            fit.Standing.UNKNOWN,
        ),
        ("This hybrid role is based in Austin, TX.", fit.Standing.INELIGIBLE),
        (
            "This is a hybrid role; you must live within commuting distance of Denver.",
            fit.Standing.INELIGIBLE,
        ),
    ],
)
def test_commuter_benefits_do_not_place_an_unplaced_hybrid_role(
    benefit: str, role: str, standing: fit.Standing
) -> None:
    posting = (
        "Implementation Consultant\n\nResponsibilities\n"
        f"{role}\nDevelop implementation plans and support customers.\n\n"
        f"Benefits\n{benefit}\n\nRequirements\n- Customer support experience"
    )

    assert _arrangement_standing(posting, _REFUSING_IN_PLACE) is standing


@pytest.mark.parametrize(
    "scope",
    [
        "Range and benefit information applies to the stated locations only.",
        "Benefits are available at the stated locations.",
        "Compensation varies across the listed locations.",
    ],
)
@pytest.mark.parametrize(
    ("role", "standing"),
    [
        ("This hybrid role combines customer support and planning.", fit.Standing.UNKNOWN),
        ("This hybrid role is based in Austin, TX.", fit.Standing.INELIGIBLE),
    ],
)
def test_compensation_scope_does_not_place_an_unplaced_hybrid_role(
    scope: str, role: str, standing: fit.Standing
) -> None:
    posting = (
        f"Implementation Consultant\n\nAbout the role\n{role}\n"
        "Support customers and develop implementation plans.\n\n"
        f"Benefits\n{scope}\nPre-tax commuter and parking benefits.\n\n"
        "Requirements\n- Customer support experience"
    )

    assert _arrangement_standing(posting, _REFUSING_IN_PLACE) is standing


@pytest.mark.parametrize(
    "body",
    [
        f"About the role\nThis role is 100% on-site.\n\nAbout us\n{_IN_PERSON_BELIEFS[0]}",
        "About the role\nWe believe in-person work matters, so staff are required to be on-site.",
        "About the role\nIn-office 1-2 days per week.",
        "Requirements:\n- Must be able to work on-site",
        "Requirements:\n- Must be able to work on-site; local candidates will be given preference",
        "About the role\nOn-site in Example City; local candidates preferred.",
        "About the role\nOnsite in Example City, local candidates preferred.",
        "About the role\nThis role is on-site, and local candidates are preferred.",
    ],
)
def test_on_site_work_the_posting_states_still_refuses_beside_a_wish(body: str) -> None:
    posting = f"Program Manager\n\n{body}\n\nResponsibilities:\n- Run sequencing programs"
    assert _arrangement_standing(posting, _REFUSING_IN_PLACE) is fit.Standing.INELIGIBLE


@pytest.mark.parametrize("template", _WISHED_ATTENDANCE)
def test_a_wished_attendance_among_requirements_does_not_refuse_a_posting(template: str) -> None:
    sentence = template.format(form="on-site", Form="On-site")
    posting = (
        f"Program Manager\n\nResponsibilities:\n- Run sequencing programs\n\n"
        f"Requirements:\n- PMP\n- {sentence}"
    )
    assert _arrangement_standing(posting, _REFUSING_IN_PLACE) is not fit.Standing.INELIGIBLE


_WANTING_IN_PLACE = {
    name: JobSearchParameters(
        arrangement=Arrangement(
            remote=Acceptance.REFUSED,
            hybrid=Acceptance.PREFERRED if name == "hybrid" else Acceptance.REFUSED,
            on_site=Acceptance.PREFERRED if name == "on site" else Acceptance.REFUSED,
        )
    )
    for name in ("hybrid", "on site")
}


@pytest.mark.parametrize(
    ("sentence", "arrangement"),
    [
        ("Because we believe in collaboration, all employees work on-site.", "on site"),
        (
            "We believe great work happens in person, so our team works on-site at our Boston "
            "office.",
            "on site",
        ),
        ("Work model: On-site, because we believe in in-person collaboration.", "on site"),
        ("On-site: we believe in working together at our Boston campus.", "on site"),
        ("Our team works on-site in Boston, and we believe in mentorship.", "on site"),
        ("We believe in being together: you will work on-site at our Boston lab.", "on site"),
        ("Onsite in Boston; we believe in in-person collaboration.", "on site"),
        ("We believe in in-person collaboration, and require employees to work onsite.", "on site"),
        (
            "We believe in in-person collaboration; employees are expected to work onsite five "
            "days.",
            "on site",
        ),
        ("As we have always believed, our employees work on-site.", "on site"),
        ("#LI-Onsite We believe diverse teams build better products.", "on site"),
        ("Onsite in Example City (local candidates preferred).", "on site"),
        ("On-site in Springfield (local candidates preferred)", "on site"),
        ("Must be willing to relocate to Springfield, IL.", "on site"),
        ("Onsite in Example City - local candidates preferred.", "on site"),
        ("Location: Boston, MA (On-site) - PCR experience preferred", "on site"),
        (
            "Onsite position at our Boston lab with preference for candidates with PCR experience.",
            "on site",
        ),
        ("We need an onsite lab manager who ideally has LIMS experience.", "on site"),
        ("Hybrid in Boston with preference for local candidates.", "hybrid"),
    ],
)
def test_attendance_stated_beside_a_belief_or_a_wish_on_something_else_still_reads(
    sentence: str, arrangement: str
) -> None:
    posting = (
        f"Program Manager\n\nAbout the role\n{sentence}\n\n"
        "Responsibilities:\n- Run sequencing programs"
    )
    assert fit.read(posting, today=TODAY).location.value == arrangement
    assert _arrangement_standing(posting, _REFUSING_IN_PLACE) is fit.Standing.INELIGIBLE
    wanting = _WANTING_IN_PLACE[arrangement]
    assert _arrangement_standing(posting, wanting) is fit.Standing.ELIGIBLE


@pytest.mark.parametrize(
    "body",
    [
        "Requirements:\n- PCR experience preferred - onsite in Boston",
        "About the role\nLocation: Boston, MA - PCR experience preferred - On-site",
        "About the role\nLocal candidates preferred - role is on-site in Boston.",
        "About the role\nLocal candidates preferred (role is on-site in Boston).",
        "Requirements:\n- Bachelor's degree preferred | Onsite",
        "About the role\nWe believe in teamwork - everyone works on-site.",
    ],
)
def test_on_site_work_set_off_from_a_wish_or_belief_still_reads_on_site(body: str) -> None:
    posting = f"Program Manager\n\n{body}\n\nResponsibilities:\n- Run sequencing programs"
    assert fit.read(posting, today=TODAY).location.value == "on site"
    assert _arrangement_standing(posting, _REFUSING_IN_PLACE) is fit.Standing.INELIGIBLE
    assert _arrangement_standing(posting, _WANTING_IN_PLACE["on site"]) is fit.Standing.ELIGIBLE


@pytest.mark.parametrize(
    "body",
    [
        "Requirements:\n- Preferred - ability to work onsite in Boston",
        "Requirements:\n- Preferred | ability to work onsite in Boston",
        "Requirements:\n- Preferred (ability to work onsite in Boston)",
        "About the role\nPreferred qualifications - ability to work on-site in Boston.",
        "Requirements:\n- Preferred | On-site availability",
        "About the role\nOn-site preferred (remote considered).",
    ],
)
def test_on_site_work_a_wish_labels_or_qualifies_is_still_only_wished(body: str) -> None:
    posting = f"Program Manager\n\n{body}\n\nResponsibilities:\n- Run sequencing programs"
    assert fit.read(posting, today=TODAY).location.value != "on site"
    assert _arrangement_standing(posting, _REFUSING_IN_PLACE) is not fit.Standing.INELIGIBLE


@pytest.mark.parametrize(
    "posting",
    [
        "Data Engineer\n\nHybrid\nThis job can be done remotely if you are on call.",
        "Data Engineer (Hybrid)\n\nThis role can be performed remotely if approved by leadership.",
        "Data Engineer\n\nPosition Type: Onsite\nThis role may be performed remotely unless "
        "business needs require on-site presence.",
        "Data Engineer\n\nThis role may be performed remotely if you live in Ohio; otherwise it "
        "is on site in Columbus.",
        "Data Engineer\n\nThis role is based in our Springfield office. The role may be "
        "performed remotely if needed.",
    ],
)
def test_a_role_led_remote_condition_that_admits_locals_is_no_remote_offer(posting: str) -> None:
    assert fit.read(posting, today=TODAY).location.value != "remote"


@pytest.mark.parametrize(
    "condition",
    [
        "unless you are in a leadership position",
        "if you are away on business travel",
        "if you are more than 2 years into the program",
        "if you live outside of the Springfield area but within the state of Illinois",
        "unless you live in a state where we are not registered",
    ],
)
def test_a_role_led_remote_condition_not_about_where_you_live_is_no_reading(
    condition: str,
) -> None:
    posting = f"Data Engineer\n\nThis role may be performed remotely {condition}.\n"
    assert fit.read(posting, today=TODAY).location.value is None


@pytest.mark.parametrize(
    "condition",
    [
        "if you live outside the Springfield area",
        "if you live more than 50 miles from our Springfield office",
        "unless you live in Ohio",
    ],
)
def test_a_role_led_remote_condition_on_living_elsewhere_offers_remote(condition: str) -> None:
    posting = f"Data Engineer\n\nThis role may be performed remotely {condition}.\n"
    assert fit.read(posting, today=TODAY).location.value == "remote"


_PLACED_ARRANGEMENTS = (
    ("This is a hybrid role, three days a week in our Boston office.", "hybrid"),
    ("All roles at Examplegen are hybrid, three days a week in the office.", "hybrid"),
    ("WHEN & WHERE: Sampletown, NY. Hybrid, minimum four days {} per week.", "hybrid"),
    ("Work model: onsite 4 days a week.", "on site"),
    ("This position is fully remote.", "remote"),
    ("You will lead analytics supporting our international business in a hybrid role.", "hybrid"),
    ("You will work {} at our Example City campus.", "on site"),
    ("USA-TX-Austin-Hybrid, United States", "hybrid"),
    ("Work from our Pune office-Hybrid", "hybrid"),
    ("Working in a hybrid way from both the office and at home.", "hybrid"),
    ("We expect analysts to meet regularly in person in our Denver office.", "on site"),
    ("Traditional office or hybrid environment, depending on location.", "hybrid"),
    ("Based in Chicago (preferred), or in Dallas (hybrid with travel to Chicago).", "hybrid"),
    ("Clinical Lead Hybrid", "hybrid"),
    ("Product Director - Raleigh, NC (Hybrid)", "hybrid"),
    ("This role is hybrid; ask your recruiter about hybrid expectations for your team.", "hybrid"),
    ("Your recruiter can share details on the hybrid schedule for your team.", "hybrid"),
    ("This is a hybrid role with 3 days per week in the Example City office.", "hybrid"),
    ("This position is hybrid, 50% onsite.", "hybrid"),
    ("Work model: 4 days {}.", "on site"),
    ("This position is onsite, like all our site-based jobs.", "on site"),
    ("Unlike our site-based jobs, this position is 100% remote.", "remote"),
    ("This is one of our office-based roles, working 5 days onsite in Example City.", "on site"),
    ("This is one of our remote-first positions.", "remote"),
    ("This is a hybrid role building hybrid clouds.", "hybrid"),
    ("This is a hybrid role; Agile, Waterfall and Kanban experience helps.", "hybrid"),
    ("This role is {}; you will answer questions by phone or in person.", "on site"),
    ("For this role, {} presence is required five days a week.", "on site"),
    ("For these roles, {} presence is required five days a week.", "on site"),
    ("Work model: Hybrid, Agile and Scrum experience a plus.", "hybrid"),
    *(
        (f"{lead} hybrid, Agile and Waterfall experience required.", "hybrid")
        for lead in (
            *(
                f"{subject} {verb}"
                for subject in ("This role", "This position", "The role", "This job")
                for verb in ("is", "will be", "is fully", "is currently")
            ),
            "Work model:",
            "Work arrangement:",
            "Workplace type:",
            "Location type:",
        )
    ),
)


@pytest.mark.parametrize(
    "title",
    [
        "Hybrid Care Coordinator - Springfield",
        "Clinical Lead Hybrid",
        "Product Director - Raleigh, NC (Hybrid)",
    ],
)
def test_an_arrangement_in_the_title_line_sets_the_arrangement(
    title: str, inventory: CvInventory
) -> None:
    posting = f"{title}\n\nAbout the role\nWe build dashboards.\n\nRequirements:\n- SQL"
    assert reading_of(posting, inventory).location.value == "hybrid"


@pytest.mark.parametrize(
    ("sentence", "expected"),
    sorted(
        {
            (cased + trailing, expected)
            for template, expected in _PLACED_ARRANGEMENTS
            for form in _ON_SITE_FORMS[:2]
            for cased in _cased(template.format(form))
            for trailing in _TRAILING
        }
    ),
)
def test_an_arrangement_sentence_placing_this_role_sets_the_arrangement(
    sentence: str, expected: str, inventory: CvInventory
) -> None:
    posting = f"Data Analyst\n\nAbout the role\n{sentence}\n\nRequirements:\n- SQL"
    assert reading_of(posting, inventory).location.value == expected


_ARRANGEMENT_MATRIX = sorted(
    {
        cased
        for sentence in (
            *_INVITED_OR_WISHED,
            *_HYBRID_DESCRIBING_THE_WORK,
            *_format_lists(),
            *_HYBRID_GOVERNING_THE_ROLE,
            *_UNPLACED_HYBRID,
            *_PLACED_HYBRID,
            *(
                template.format(form)
                for template, _ in _PLACED_ARRANGEMENTS
                for form in _ON_SITE_FORMS
            ),
            *(
                f"{lead} {listed} work, with travel."
                for lead in ("We offer", "This is a hybrid role. We offer")
                for listed in ("digital, live virtual, in-person, and hybrid", "remote and on-site")
            ),
        )
        for cased in _cased(sentence)
    }
)


@pytest.mark.parametrize("sentence", _ARRANGEMENT_MATRIX)
def test_every_arrangement_quote_is_verbatim_posting_text(
    sentence: str, inventory: CvInventory
) -> None:
    posting = f"Data Analyst\n\nAbout the role\n{sentence}\n\nRequirements:\n- SQL"
    # The reader quotes the sanitized body, and recases a line written in capitals.
    flat = " ".join(sanitize(posting).split()).casefold()
    for quote in reading_of(posting, inventory).location.evidence:
        assert quote.removeprefix("...").removesuffix("...").casefold() in flat


@pytest.mark.parametrize(
    "heading",
    [
        "Northwind: Powering Hybrid Energy",
        "Hybrid OR Team",
        "Browse Hybrid Jobs",
        "Hybrid Vehicle Program",
    ],
)
@pytest.mark.parametrize("marked", ["## {}", "{}:"])
def test_an_arrangement_word_inside_a_heading_name_sets_no_arrangement(
    heading: str, marked: str, inventory: CvInventory
) -> None:
    posting = (
        f"Data Analyst\n\n{marked.format(heading)}\nWe build dashboards.\n\nRequirements:\n- SQL"
    )
    assert reading_of(posting, inventory).location.value is None


def test_an_either_way_phrase_leaves_the_role_s_own_arrangement(inventory: CvInventory) -> None:
    sentence = "You will work on-site in Denver, partnering with on-site and remote teams."
    posting = f"Data Analyst\n\nAbout the role\n{sentence}\n\nRequirements:\n- SQL"
    assert reading_of(posting, inventory).location.value == "on site"


@pytest.mark.parametrize(
    "sentence",
    sorted(
        {
            cased
            for away in ("at home", "virtually", "remotely")
            for template in (
                "As a hybrid organization, you and your leader choose where you work, "
                "{} or in the office.",
                "We are a hybrid organization offering employees the choice and flexibility "
                "to work from both the office and {}.",
            )
            for cased in _cased(template.format(away))
        }
    ),
)
@pytest.mark.parametrize("heading", ["About the role", "Benefits"])
def test_an_explicit_home_or_office_choice_requires_no_attendance(
    sentence: str, heading: str
) -> None:
    posting = f"Data Analyst\n\n{heading}\n{sentence}"
    reading = fit.read(posting, parameters=_REFUSING_IN_PLACE, today=TODAY)

    assert reading.location.value is None
    assert _arrangement_standing(posting, _REFUSING_IN_PLACE) is not fit.Standing.INELIGIBLE


@pytest.mark.parametrize(
    "required",
    [
        "This role is on-site in Springfield, IL.",
        "This role requires three days per week in the office.",
        "We are looking to hire someone local to or willing to relocate to Springfield, IL.",
        "Must be willing to relocate to Springfield, IL.",
    ],
)
def test_a_workplace_choice_does_not_waive_separate_mandatory_attendance(required: str) -> None:
    posting = (
        "Data Analyst\n\nAbout the role\n"
        "As a hybrid organization, employees choose where they work, at home or in the office.\n"
        f"{required}"
    )

    assert _arrangement_standing(posting, _REFUSING_IN_PLACE) is fit.Standing.INELIGIBLE


def test_an_in_person_requirement_line_sets_no_arrangement(inventory: CvInventory) -> None:
    posting = (
        "Data Analyst\n\nRequirements:\n- In-person customer service experience required\n- SQL"
    )
    assert reading_of(posting, inventory).location.value is None


_VIRTUAL_ROLE_BODY = """
Clinical Case Manager, Northwind

About the role
{role_statement}, except for required in-person training sessions.
Alternate locations may be considered if candidates reside within commuting distance of an office.
Please note that per our policy on hybrid/virtual work, candidates not within a reasonable
commuting distance from the posting location will not be considered for employment.

Responsibilities:
- Review complex clinical cases and coordinate care plans with the nursing team.
- Collaborate with leadership to improve orientation and training materials.
- Explain case decisions and document the evidence supporting each recommendation.

Requirements:
- An active nursing license is required.
- Three years of experience reviewing clinical cases.
- Experience with case management systems and clinical documentation.

Workplace policy
Northwind operates a Hybrid Workforce Strategy.
Unless specified as primarily virtual by the hiring manager, associates are required to
work at a Northwind location at least once per week, and potentially several times per week.

Benefits:
- Paid leave and a retirement plan.

Equal opportunity
Northwind is an equal opportunity employer. Reasonable accommodations are available.
"""
_VIRTUAL_ROLE_STATEMENTS = sorted(
    {
        cased
        for role, verb, people, schedule in itertools.product(
            ("role", "position", "job"),
            ("enables", "allows", "permits"),
            ("associates", "employees", "you"),
            ("full-time", "full time"),
        )
        for cased in _cased(f"This {role} {verb} {people} to work virtually {schedule}")
    }
    | {
        cased
        for schedule, template in itertools.product(
            ("full-time", "full time"),
            (
                "This role is virtual {schedule}",
                "The position will be {schedule} virtual",
                "This is a {schedule} virtual role",
            ),
        )
        for cased in _cased(template.format(schedule=schedule))
    }
)


@pytest.mark.parametrize("role_statement", _VIRTUAL_ROLE_STATEMENTS)
def test_a_full_time_virtual_role_stays_remote_beside_commuting_and_workforce_policy(
    role_statement: str,
) -> None:
    posting = _VIRTUAL_ROLE_BODY.format(role_statement=role_statement)
    reading = fit.read(posting, parameters=_REFUSING_IN_PLACE, today=TODAY)
    gates = {gate.name: gate.standing for gate in fit.gates(reading, _REFUSING_IN_PLACE)}

    assert reading.location.value == "remote"
    assert reading.location.conflict is False
    assert reading.arrangement_fit.value == Acceptance.PREFERRED.value
    assert gates["arrangement"] is fit.Standing.ELIGIBLE
    assert any(
        "required in-person training sessions" in quote for quote in reading.location.evidence
    )
    flat = " ".join(posting.split())
    assert all(
        quote.removesuffix("...").removeprefix("...") in flat for quote in reading.location.evidence
    )
    reference = fit.read(
        _VIRTUAL_ROLE_BODY.format(role_statement="This role is fully remote"),
        parameters=_REFUSING_IN_PLACE,
        today=TODAY,
    )
    assert {
        gate.name: gate.standing
        for gate in fit.gates(reference, _REFUSING_IN_PLACE)
        if gate.name not in {"arrangement", "office attendance"}
    } == {
        name: standing
        for name, standing in gates.items()
        if name not in {"arrangement", "office attendance"}
    }


@pytest.mark.parametrize(
    "category",
    sorted(
        {
            cased
            for preposition, work_types in itertools.product(
                ("on", "for"), ("hybrid/virtual", "hybrid / virtual", "virtual/hybrid")
            )
            for cased in _cased(f"policy {preposition} {work_types} work")
        }
    ),
)
def test_a_hybrid_virtual_policy_category_states_no_role_arrangement(category: str) -> None:
    posting = (
        f"Data Analyst\n\nUnder our {category}, candidates must live within commuting "
        "distance of an office.\n\nRequirements:\n- SQL"
    )
    reading = fit.read(posting, parameters=_REFUSING_IN_PLACE, today=TODAY)

    assert reading.location.value is None
    assert reading.location.conflict is False
    assert _arrangement_standing(posting, _REFUSING_IN_PLACE) is fit.Standing.UNKNOWN


@pytest.mark.parametrize(
    "attendance",
    [
        "This role is hybrid with two days a week in the office.",
        "This position is onsite at our Springfield office.",
        "You must work three days per week in the office.",
        "The candidate must work on site five days a week.",
        "This role is hybrid.",
    ],
)
@pytest.mark.parametrize(
    "remote",
    ["This role enables associates to work virtually full-time", "This role is fully remote"],
)
def test_a_whole_role_remote_declaration_conflicts_with_mandatory_role_attendance(
    remote: str,
    attendance: str,
) -> None:
    posting = (
        _VIRTUAL_ROLE_BODY.format(role_statement=remote) + f"\n\nAbout this role\n{attendance}"
    )
    reading = fit.read(posting, parameters=_REFUSING_IN_PLACE, today=TODAY)

    assert reading.location.value is None
    assert reading.location.conflict is True
    assert reading.arrangement_fit.value is None
    assert _arrangement_standing(posting, _REFUSING_IN_PLACE) is fit.Standing.UNKNOWN
    assert any(attendance in quote for quote in reading.location.evidence)


@pytest.mark.parametrize("tag", ["#LI-Hybrid", "#LI-Onsite"])
def test_a_virtual_full_time_role_keeps_a_contrary_recruiter_tag_as_a_conflict(tag: str) -> None:
    posting = (
        _VIRTUAL_ROLE_BODY.format(
            role_statement="This role enables associates to work virtually full-time"
        )
        + f"\n\n{tag}"
    )
    reading = fit.read(posting, parameters=_REFUSING_IN_PLACE, today=TODAY)

    assert reading.location.value is None
    assert reading.location.conflict is True
    assert _arrangement_standing(posting, _REFUSING_IN_PLACE) is fit.Standing.UNKNOWN


@pytest.mark.parametrize(
    ("attendance", "expected"),
    [
        ("This role is hybrid with two days a week in the office.", "hybrid"),
        ("You must work three days a week in the office.", "hybrid"),
        ("This role is onsite at our Springfield office.", "on site"),
    ],
)
def test_a_hybrid_virtual_policy_category_keeps_a_stated_role_attendance_requirement(
    attendance: str,
    expected: str,
) -> None:
    posting = (
        "Data Analyst\n\nOur policy on hybrid/virtual work requires candidates to live near "
        f"the office. {attendance}\n\nRequirements:\n- SQL"
    )
    reading = fit.read(posting, parameters=_REFUSING_IN_PLACE, today=TODAY)

    assert reading.location.value == expected
    assert _arrangement_standing(posting, _REFUSING_IN_PLACE) is fit.Standing.INELIGIBLE


@pytest.mark.parametrize(
    "sentence",
    [
        "This role enables associates to deliver virtual training full-time.",
        "This role allows employees to work on virtual training full time.",
        "This position is responsible for virtual full-time training programs.",
        "Virtual full-time courses are offered to employees.",
        "Unless this role is virtual full-time, employees attend the office weekly.",
    ],
)
def test_a_virtual_training_task_or_conditional_role_is_no_remote_declaration(
    sentence: str,
) -> None:
    reading = fit.read(f"Data Analyst\n\n{sentence}\n\nRequirements:\n- SQL", today=TODAY)

    assert reading.location.value != "remote"


@pytest.mark.parametrize(
    "training",
    [
        "This role requires five days a week on site for the first month of training.",
        "The candidate must work on site five days a week during onboarding.",
        "You must be in the office three days a week for the first week of onboarding.",
    ],
)
def test_a_virtual_role_keeps_bounded_training_from_claiming_regular_attendance(
    training: str,
) -> None:
    posting = (
        _VIRTUAL_ROLE_BODY.format(
            role_statement="This role enables associates to work virtually full-time"
        )
        + f"\n\nAbout this role\n{training}"
    )
    reading = fit.read(posting, parameters=_REFUSING_IN_PLACE, today=TODAY)

    assert reading.location.value == "remote"
    assert reading.location.conflict is False
    assert _arrangement_standing(posting, _REFUSING_IN_PLACE) is fit.Standing.ELIGIBLE
