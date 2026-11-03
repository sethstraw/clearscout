"""Unconditional attendance commitments read from publisher body sentences."""

from __future__ import annotations

import itertools
import time
from collections.abc import Callable
from datetime import date

import pytest

from clearscout.application.master_cv.cv import CvInventory
from clearscout.application.tailoring import fit
from clearscout.domain.arrangement_grammar import (
    attendance_is_hedged,
    attendance_statement,
    remote_offer_statement,
    remote_perk_or_culture,
)


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Hybrid Working: Minimum 3 days per week in the office", "hybrid"),
        ("The position is based onsite in Springfield, Illinois.", "on_site"),
        (
            "This role has an expectation of working in the office a minimum of 3 days per week.",
            "hybrid",
        ),
        ("Location: Springfield, IL (hybrid 3x a week)", "hybrid"),
        ("This role is a hybrid position.", "hybrid"),
        ("This is a hybrid role based in Springfield.", "hybrid"),
        (
            "The selected candidate is expected to work on-site at our Springfield office"
            " a minimum of two (2) days per week.",
            "hybrid",
        ),
        ("Hybrid: 3 days a week onsite", "hybrid"),
        ("You are required to be in the office 4 days per month.", "hybrid"),
        ("This is a hybrid position with office attendance of 2-3 times a week.", "hybrid"),
        (
            "This position is based onsite in Springfield, IL with 3 days a week in the lab.",
            "hybrid",
        ),
        (
            "This is a hybrid position requiring 2 days per week in the office, with the"
            " remaining days worked remotely.",
            "hybrid",
        ),
        ("This role offers a hybrid schedule, working 3 days per week on campus.", "hybrid"),
        ("We follow a hybrid work schedule with 2 days per week in the office.", "hybrid"),
        ("Hybrid working schedule: three days a week onsite.", "hybrid"),
    ],
)
def test_an_unconditional_commitment_reads_as_attendance(text: str, kind: str) -> None:
    assert attendance_statement(text) == (kind, text)


@pytest.mark.parametrize(
    "text",
    [
        "Hybrid: in the office 3 days a week at our Acme Inc. Toronto hub.",
        "This role is hybrid, e.g. in the office 3 days a week.",
        "Hybrid: in the office 3 days a week at our U.S. Midwest hub.",
        "Hybrid: in the office 3 days a week with the Sr. Director and team.",
    ],
)
def test_an_abbreviation_leaves_the_attendance_sentence_whole(text: str) -> None:
    assert attendance_statement(text) == ("hybrid", text)


@pytest.mark.parametrize(
    "text",
    [
        "In-person working gives us the platform we need to connect, work at pace and"
        " challenge perceptions.",
        "As is typical of an office-based role, employees must be able to use a computer.",
        "We support your flexibility to work from home or in the office.",
        "<ul><li>Hybrid work</li><li>Paid parental leave</li></ul>",
        "Experience with hybrid cloud platforms.",
        "This is a hybrid role blending analytics and product management.",
        "Onsite interviews are held at our Springfield office 2 days per week.",
        "Travel up to 25% to on-site client visits.",
        "You will spend onboarding week in the office 5 days per week.",
        "Attendance in the office 3 days per week may be required.",
        "If local, you are required to work in the office 3 days per week where applicable.",
        "This role is not hybrid.",
        "Our hybrid schedule lets you work from anywhere.",
        "The position is based in Springfield, IL.",
        "Team members work from the office 3 days per quarter.",
        # Descriptive copy about the work, other staff, or travel.
        "This is a hybrid role at the intersection of product and engineering.",
        "This is a hybrid role with responsibilities across data engineering and analytics.",
        "This is a hybrid position in clinical informatics and IT.",
        "The role is a hybrid, working across research and operations.",
        "This is a hybrid role working closely with clinicians and engineers.",
        "You will partner with clinicians based on-site at our partner hospitals.",
        "Our field engineers are based onsite at customer facilities.",
        "Our nurses work in the office 5 days per week and rely on this team for support.",
        "Our clinical staff work from the office five days a week.",
        "You will travel from the office to client sites 2 days per week.",
        # A cadence counting the days away from the office.
        "Hybrid 3 days a week from home.",
        "Hybrid: 3 days a week from home.",
        "Flexible hybrid 2x a week at home.",
        # A cadence that binds only candidates meeting a condition.
        "If the selected candidate lives within 50 miles of our Springfield office, they will"
        " be required to work on-site a minimum of two (2) days per week.",
        "If in Springfield or Shelbyville, you must work from the office at least three days"
        " a week.",
    ],
)
def test_a_perk_description_or_softened_sentence_keeps_the_row(text: str) -> None:
    assert attendance_statement(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "Hybrid: 3 days a week in the office. Work Anywhere: team members may work from any"
        " location for four weeks a year.",
        "You can work from anywhere in the United States.",
        "Springfield or remote.",
        "Open to remote if not aligned to office location with a strong preference East Coast.",
        "Remote available if not within commuting distance of an office.",
        "Remote work is considered if not near a hub.",
    ],
)
def test_a_remote_option_beside_attendance_is_still_an_offer(text: str) -> None:
    assert remote_offer_statement(text) is not None


@pytest.mark.parametrize(
    "text",
    [
        "This role is not remote.",
        "We are not open to remote candidates.",
        "Remote work is not available for this role.",
        "If not local, remote is not an option.",
        "You must be in the office when not working remotely on approved days.",
        "When not working remotely, you will be based in our Springfield office three days"
        " per week.",
        "You are expected in the office if not approved to work remotely.",
    ],
)
def test_a_negated_remote_mention_is_no_offer(text: str) -> None:
    assert remote_offer_statement(text) is None


@pytest.mark.parametrize("tag", ["#LI-REMOTE", "#LI-Remote", "#LI-remote"])
@pytest.mark.parametrize(
    "after", ["", " Any data provided will be stored in accordance with our Privacy Policy."]
)
def test_a_remote_tag_run_in_after_an_unpunctuated_line_is_an_offer(tag: str, after: str) -> None:
    text = (
        "Other Required: 6+ years for a Sr. Records Analyst and 9+ years for a Lead Records "
        f"Analyst Familiarity with scheduling tools {tag}{after}"
    )
    assert remote_offer_statement(text) == tag


@pytest.mark.parametrize("initialism", ["U.S.", "U.K.", "E.U."])
def test_a_role_statement_after_an_initialism_is_its_own_sentence(initialism: str) -> None:
    role = "This role is hybrid, in the office 3 days a week."
    text = f"Remote work is optional for teams in the {initialism} {role}"
    assert attendance_statement(text) == ("hybrid", role)


def test_remote_days_inside_a_hybrid_schedule_are_not_a_remote_offer() -> None:
    body = (
        "This is a hybrid position requiring 2 days per week in the office, with the"
        " remaining days worked remotely."
    )

    assert remote_offer_statement(body) is None
    assert remote_offer_statement("The rest of the week you may work remotely.") is None


@pytest.mark.parametrize(
    "text",
    [
        "This is a hybrid role" + " " * 20_000 + "x",
        # An unclosed aside with many softened counts.
        "Hybrid (" + "3 days preferred and " * 400,
    ],
    ids=["whitespace", "unclosed aside"],
)
def test_a_long_run_of_whitespace_reads_in_linear_time(text: str) -> None:
    # This process's CPU time, which parallel workers cannot inflate: a linear read takes
    # milliseconds, and a quadratic one over this run takes over a second.
    started = time.process_time()
    attendance_statement(text)
    remote_offer_statement(text)

    assert time.process_time() - started < 0.5


@pytest.mark.parametrize("reader", [attendance_is_hedged, attendance_statement])
def test_a_day_count_before_a_long_run_of_whitespace_reads_in_linear_time(
    reader: Callable[[str], object],
) -> None:
    started = time.process_time()
    reader("Hybrid (3" + " " * 40_000 + "x")

    assert time.process_time() - started < 0.5


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        (
            "Full time position requires working 3 days a week in our Fairview office (Hybrid)",
            "hybrid",
        ),
        ("Commit to average 3 days per week in office in Lakeside.", "hybrid"),
        ("Hybrid work (minimum 3 days per week in the Ridgeton office).", "hybrid"),
        (
            "Location: Easton, Texas, on-site/hybrid, minimum three days per week on campus",
            "hybrid",
        ),
        ("This position is based in Millbrook, MN with a hybrid on-site schedule.", "hybrid"),
        ("This role will be Hybrid at 40 Harbor Street in Downtown Bayview", "hybrid"),
        ("Role is located in Westport, CA with a hybrid work model.", "hybrid"),
        ("Locations: Hybrid work arrangement in Fairview, CT, Lakeside, PA.", "hybrid"),
        ("This is a fully onsite, individual contributor role with broad visibility.", "on_site"),
        ("This is an onsite opportunity in Bayview, CA.", "on_site"),
        (
            "This position requires regular onsite presence at the assigned Northfield location.",
            "on_site",
        ),
        (
            "Position is local and work is onsite in Northfield, IN at the Northfield"
            " Technology Center.",
            "on_site",
        ),
        ("Location : Onsite, working in Northfield, IN", "on_site"),
        ("On-Site position Riverton, PA", "on_site"),
        ("A connected research campus: work on-site in Lakeside", "on_site"),
        ("... to join the Devices Division on-site in Bayview, CA or Ridgeton, MN.", "on_site"),
        ("Employees must work on-site at the Fairview Headquarters.", "on_site"),
    ],
)
def test_a_placed_or_role_stated_attendance_reads_its_kind(text: str, kind: str) -> None:
    found = attendance_statement(text)

    assert found is not None
    assert found[0] == kind


@pytest.mark.parametrize(
    "text",
    [
        "Relocation benefits are not available for this posting.",
        "Free onsite parking.",
        "Convenient on-site amenities.",
        "Experience with hybrid architectures.",
        "- On-site parking\n- Onsite gym",
        "We offer a hybrid work model.",
        "As is typical of an office-based role, employees must be able to use a computer for"
        " long periods.",
        "For employees located within a one-hour commute of an office, we require in-person"
        " attendance three days a week.",
        "Candidates who reside within about 50 miles of a designated office may be expected to"
        " work onsite.",
        "You should be comfortable working on-site in a customer's environment.",
        "Staff may work at the location closest to their home.",
        "Candidates must reside locally or be willing to relocate.",
        "Candidates must reside in the greater Lakeside area.",
        "Applicants not within a reasonable commuting distance of Lakeside will not be considered.",
        "This position requires attendance in the office 2 days per quarter.",
    ],
)
def test_a_perk_proximity_customer_or_residence_sentence_states_no_attendance(text: str) -> None:
    assert attendance_statement(text) is None


@pytest.mark.parametrize(
    "text",
    [
        *(
            f"For that reason, {quantifier} {noun} are required to be performed on-site."
            for quantifier in ("many", "some", "most", "certain", "other", "all")
            for noun in ("roles", "positions", "jobs", "employees")
        ),
        "30% traveling to customer onsite required.",
        "You will go on-site with clients, dig into their systems and map their data.",
        "You must work on-site with customers to install the platform.",
        "Client on-site work is required for this position.",
        "You are required to travel to a customer site in Lakeside.",
        "Work Location: Remote based in the US or on-site in Lakeside, IL or Fairview, OH",
        "This position works on-site at the Lakeside headquarters or other company location (or"
        " from a remote location with management approval).",
        "Lakeside based candidates will require a minimum of two onsite days per week.",
        "For Bay Area-based employees, this role requires being in our Lakeside office at least"
        " three days a week.",
        "Location: This is a hybrid remote/in-office role.",
        "This is a remote or in-office role.",
        "However, when working on initiatives specific to another site, they will be required to"
        " work onsite at that location.",
        "When traveling to Fairview, you must work on-site at the Fairview office.",
        "When visiting Lakeside, you are required to work on-site at the Lakeside office.",
        "Candidates who reside within 50 miles of the following locations will be required to"
        " perform onsite 3 days per week.",
    ],
)
def test_another_party_a_visit_a_choice_or_a_condition_states_no_attendance(text: str) -> None:
    assert attendance_statement(text) is None


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        (
            "This is a hybrid role working 2 days a week in one of our offices, preferably located"
            " in EST or CST.",
            "hybrid",
        ),
        ("On site in Lakeside, NY for 4 days a week", "hybrid"),
        ("Location: NYC (on-site in Lakeside office 3+ days/week)", "hybrid"),
        ("Able to work onsite in Lakeside 3 days/week", "hybrid"),
        (
            "This role can sit on-site in our Lakeside or Fairview offices a minimum of 4"
            " days/week",
            "hybrid",
        ),
        ("Hybrid: Tuesdays, Wednesdays and Thursdays onsite in Lakeside HQ", "hybrid"),
        ("You must work on-site in Lakeside 5 days a week.", "on_site"),
        ("You must work on-site in Lakeside.", "on_site"),
    ],
)
def test_a_placed_on_site_statement_takes_its_kind_from_its_day_count(text: str, kind: str) -> None:
    found = attendance_statement(text)

    assert found is not None
    assert found[0] == kind


_SUBJECTS = {"role": "This role", "position": "This position", "you": "You"}
_MODALS = {
    "requires": ("requires working", "are required to work"),
    "must": ("must work", "must work"),
    "minimum": ("will work a minimum of", "will work a minimum of"),
    "commit to": ("commits to working", "commit to working"),
    "expected to": ("is expected to work", "are expected to work"),
}
_COUNTS = {"digit": "3", "word": "three"}
_UNITS = ("a week", "per week")
_PLACES = ("in the office", "in our Fairview office", "on campus", "on-site")
_MATRIX = list(itertools.product(_SUBJECTS, _MODALS, _COUNTS, _UNITS, _PLACES))
_GUARDS = {
    "if": "If local, {clause}.",
    "unless": "{clause} unless approved for an exception.",
    "proximity": "{clause} when located within 50 miles of an office.",
    "interview": "{clause} during the interview process.",
    "benefit": "{clause} to receive a commuter stipend.",
}


def _cadence_clause(subject: str, modal: str, count: str, unit: str, place: str) -> str:
    verb = _MODALS[modal][subject == "you"]
    return f"{_SUBJECTS[subject]} {verb} {_COUNTS[count]} days {unit} {place}"


@pytest.mark.parametrize(("subject", "modal", "count", "unit", "place"), _MATRIX)
def test_every_written_form_of_a_weekly_cadence_reads_hybrid(
    subject: str, modal: str, count: str, unit: str, place: str
) -> None:
    sentence = _cadence_clause(subject, modal, count, unit, place) + "."

    assert attendance_statement(sentence) == ("hybrid", sentence)


@pytest.mark.parametrize("guard", _GUARDS)
@pytest.mark.parametrize(("subject", "modal", "count", "unit", "place"), _MATRIX)
def test_every_weekly_cadence_inside_a_guard_states_no_attendance(
    guard: str, subject: str, modal: str, count: str, unit: str, place: str
) -> None:
    clause = _cadence_clause(subject, modal, count, unit, place)
    if guard == "if":
        clause = clause[0].lower() + clause[1:] if subject != "you" else "you" + clause[3:]

    assert attendance_statement(_GUARDS[guard].format(clause=clause)) is None


_POSITIVE = "This role requires working 3 days a week in our Fairview office."


@pytest.mark.parametrize(
    "body",
    [
        _POSITIVE,
        f"About the team\n\n- Build ledgers\n- {_POSITIVE}\n- Ship weekly",
        f"We build ledgers. {_POSITIVE} We value craft.",
        f"<ul><li>Build ledgers.</li><li>{_POSITIVE}</li></ul>",
        _POSITIVE.replace("This role", "This role\N{RIGHT SINGLE QUOTATION MARK}s schedule"),
        _POSITIVE.replace(" ", "\N{NO-BREAK SPACE}"),
    ],
    ids=["alone", "bullet", "paragraph", "html", "curly", "nbsp"],
)
def test_an_attendance_sentence_reads_the_same_wherever_it_sits(body: str) -> None:
    found = attendance_statement(body)

    assert found is not None
    assert found[0] == "hybrid"


@pytest.mark.parametrize(
    ("sentence", "kind"),
    [
        (_POSITIVE, "hybrid"),
        ("This role is fully onsite at the Fairview Headquarters.", "on_site"),
    ],
)
def test_every_reader_takes_one_attendance_kind_from_one_body(
    sentence: str, kind: str, inventory: CvInventory
) -> None:
    body = f"<p>We build hybrid cloud ledgers.</p><p>{sentence}</p>"

    assert attendance_statement(body) == (kind, sentence)
    reading = fit.read(
        f"Data Manager\n\nBenefits include remote work options. {sentence}\n",
        inventory,
        vocabulary=None,
        today=date(2026, 6, 1),
    )
    assert reading.location.conflict
    assert any(sentence.rstrip(".") in quote for quote in reading.location.evidence)


_WEEK_REQUIREMENT = (
    "The role requires working in the office in Fairview at least {count} days {unit}"
)
_SOFTENER_PLACEMENTS = {
    "parenthetical": "{required} ({more} days {unit} preferred).",
    "trailing": "{required}, with {more} days {unit} preferred.",
    "sentence": "{required}. Working {more} days {unit} is preferred.",
}
_SOFTENER_MATRIX = list(itertools.product(_SOFTENER_PLACEMENTS, _COUNTS, _UNITS))


@pytest.mark.parametrize(("placement", "count", "unit"), _SOFTENER_MATRIX)
def test_a_softener_governs_only_the_count_it_modifies(
    placement: str, count: str, unit: str
) -> None:
    more = {"digit": "4", "word": "four"}[count]
    required = _WEEK_REQUIREMENT.format(count=_COUNTS[count], unit=unit)
    text = _SOFTENER_PLACEMENTS[placement].format(required=required, more=more, unit=unit)
    expected = required + "." if placement == "sentence" else text

    assert attendance_statement(text) == ("hybrid", expected)


@pytest.mark.parametrize(
    "subject",
    [
        *(f"{q.capitalize()} of Northwind's roles" for q in ("many", "most", "some", "all")),
        "Many of Northwind\N{RIGHT SINGLE QUOTATION MARK}s roles",
        "Many roles at Northwind",
    ],
)
def test_a_company_wide_subject_states_no_attendance(subject: str) -> None:
    assert attendance_statement(f"{subject} are required to be performed on-site.") is None


@pytest.mark.parametrize(
    "text",
    [
        "This is not an on-site role.",
        "This is a remote position, not an onsite position.",
        "You will travel on-site for quarterly team meetings.",
        "You will be in the office a few times per year.",
        "You will be onsite for onboarding week.",
        "Enjoy free lunches onsite at our Fairview HQ.",
        "Experience working on-site at CROs.",
        "Reports to the Corporate Strategy Office.",
        "Training is 6 weeks onsite in Lakeside.",
        "(May consider onsite in Ridgeton)",
        "We prefer candidates who can work onsite at our Fairview headquarters, but we are open"
        " to hybrid and remote arrangements.",
        "Our hybrid work model is based in trust.",
        "Our offices are located in Fairview.",
        "Partner closely with the hybrid role in Clinical Operations.",
        "Candidates within a 40-mile radius of the Bayview office are expected onsite three days"
        " a week.",
        "We offer a flexible approach to work with remote, hybrid, field or office work schedules.",
        "To ensure Home or Hybrid Home/Office employees' ability to work effectively, the internet"
        " service must meet requirements.",
        "Promote adoption of a hybrid training delivery mindset.",
        "You will join the team onsite for an annual planning meeting.",
        "- Ability to travel for engagement kickoffs, key milestones and on-site working sessions"
        " 20-30% of the time",
        "- Location : Hybrid / Remote",
        "Work Model: Hybrid or Remote",
        "Lakeside based hybrid employees will have a defined work location that includes work"
        " from home and 3 office days set by you and your team.",
    ],
)
def test_a_visit_perk_history_department_or_policy_states_no_attendance(text: str) -> None:
    assert attendance_statement(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "This hybrid position requires 12 days per month onsite.",
        "This is a 4-day onsite position.",
        "You will be onsite at least 50% of the time.",
        "You will work on-site 60% of the time.",
        "You will work in the office part of the week and remotely the rest.",
        "The other two office days are flexible.",
        "Hybrid, minimum four days on-site per week.",
        "Hybrid - minimum 3 days onsite per week.",
        "Hybrid; at least four days in office weekly.",
        "Work Location Assignment: Hybrid",
        'This position is currently classified as "hybrid" in accordance with our Hybrid and'
        " Remote Work policy.",
        "Work Model: Primarily on-site with hybrid flexibility",
    ],
)
def test_a_part_week_or_labelled_hybrid_statement_reads_hybrid(text: str) -> None:
    assert attendance_statement(text) == ("hybrid", text)


@pytest.mark.parametrize(
    "text",
    [
        "This is a hybrid role, where you will spend at least eight days a month at our office,"
        " where you'll enjoy the benefits of connecting with your colleagues in person.",
        "Perks of joining Northwind in Lakeside 25 vacation days Daily in-office lunch stipend"
        " Hybrid work model: 2 days/ week in our office in Lakeside Employee-led initiatives"
        " including annual company-wide innovation day",
        "Perks of joining Northwind Competitive salary Hybrid work model, with two days per week"
        " based in our Lakeside office 25 days of annual paid leave",
        "This is a permanent, full-time position that can be based in multiple locations,"
        " including Lakeside and Fairview (hybrid working model).",
        "In this hybrid role, you'll have a defined work location that includes work from home"
        " and 3 office days set by you and your team.",
    ],
)
def test_a_requirement_beside_a_perk_or_annual_word_reads_hybrid(text: str) -> None:
    found = attendance_statement(text)

    assert found is not None
    assert found[0] == "hybrid"


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("This on-site role spends 10% of the time on documentation.", "on_site"),
        ("On-site support is required for 20% of the time.", None),
    ],
)
def test_a_share_of_the_work_is_not_a_share_of_attendance(text: str, kind: str | None) -> None:
    found = attendance_statement(text)

    assert (found and found[0]) == kind


def test_a_hybrid_label_after_a_place_line_reads_hybrid() -> None:
    sentence = "Hybrid, minimum four days on-site per week."

    assert attendance_statement(f"WHEN & WHERE: Fairview, NY. {sentence}") == ("hybrid", sentence)


@pytest.mark.parametrize(
    "text",
    [
        "We cannot offer a fully remote option.",
        "Home-based internet requirements do not apply.",
        "Complete the Virtual Job Tryout.",
        "Interviews are conducted remotely.",
        "We offer a flexible approach to work with remote, hybrid, field or office work schedules.",
        "To ensure Home or Hybrid Home/Office employees' ability to work effectively, the internet"
        " service must meet requirements.",
    ],
)
def test_a_refusal_assessment_or_process_mention_is_no_remote_offer(text: str) -> None:
    assert remote_offer_statement(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "Hybrid (Remote & On-site). Remote work is available on other days.",
        "You may work remotely on the remaining days.",
    ],
)
def test_the_remote_remainder_of_a_hybrid_week_is_no_offer(text: str) -> None:
    assert remote_offer_statement(text) is None


def test_remote_work_on_other_days_after_a_hybrid_cadence_is_no_offer() -> None:
    text = "Hybrid: 3 days a week in office. Remote work is available on other days."

    assert remote_offer_statement(text) is None
    assert attendance_statement(text) == ("hybrid", "Hybrid: 3 days a week in office.")


@pytest.mark.parametrize(
    "text",
    [
        "This is a fully remote role; interviews are conducted remotely.",
        "The work can be performed remotely across the United States.",
        "Location: Remote first (home based)\nInterview dates: panels will be held remotely in"
        " May.",
        "Our team members work remotely across time zones.",
        "This is a home-based role; home-based internet is required.",
        "Fully remote, with optional office time on other days.",
        "The interview process and this position are fully remote.",
        "Interviews and the job itself are fully remote.",
        "Interviews are virtual and the role is remote.",
        "After the interview, you will work remotely full time.",
        "Following a successful interview, this role can be performed remotely.",
    ],
)
def test_an_offer_beside_a_process_or_equipment_mention_is_still_an_offer(text: str) -> None:
    assert remote_offer_statement(text) is not None


def test_remote_work_available_for_the_role_is_an_offer() -> None:
    text = "Remote work is available for this role."

    assert remote_offer_statement(text) == text


def test_virtual_full_time_work_is_a_remote_offer() -> None:
    text = (
        "This role enables associates to work virtually full-time, except for required"
        " in-person training."
    )

    assert remote_offer_statement(text) == text


_PERK_SUBJECTS = ("Work-from-Home", "Work from home", "WFH", "Remote work", "Remote")
_PERK_KINDS = ("stipend", "Stipend", "allowance", "reimbursement", "budget", "benefit")
_PERK_LINES = [
    f"{lead}{subject}{setup} {kind}"
    for lead, subject, setup, kind in itertools.product(
        ("- ", "Monthly "), _PERK_SUBJECTS, ("", " setup", " equipment"), _PERK_KINDS
    )
] + [
    f"A {kind} for your {subject} setup."
    for kind, subject in itertools.product(
        ("stipend", "allowance", "reimbursement", "budget"),
        ("remote work", "work from home", "WFH"),
    )
]
_EQUIPMENT_NOUNS = (
    "telework",
    "teleworking",
    "home office",
    "home-office",
    "remote work",
    "work from home",
    "WFH",
)
_EQUIPMENT_FORMS = (
    "reimbursement of wellness expenses and {noun} equipment",
    "{noun} equipment reimbursement",
    "a {noun} allowance",
    "a {noun} stipend",
    "a one-time stipend to set up your {noun} space",
    "a budget for {noun} equipment",
)
_EQUIPMENT_PERK_LINES = [
    f"Competitive salary, flexible vacation, and {form.format(noun=noun)}."
    for noun, form in itertools.product(_EQUIPMENT_NOUNS, _EQUIPMENT_FORMS)
]
_CONDITIONAL_BENEFIT_BLOCKS = [
    f"{heading}:\n- Internet stipend\n- {item}"
    for heading, item in itertools.product(
        (
            "For roles listed as United States - Remote",
            "For roles listed as Remote",
            "For positions posted as remote",
            "For remote roles",
            "For remote and hybrid roles",
        ),
        ("Remote work flexibility", "Work from home equipment", "Fully remote schedule"),
    )
]
_CULTURE_LINES = [
    "You are energized by a remote-first, highly cross-functional environment.",
    "We are a remote-first company with hubs in Fairview and Northtown.",
    "You thrive in a remote-first culture.",
    "You enjoy working on a remote-friendly team.",
    "We are a remote-friendly company with offices in Fairview.",
    "Northwind Rail is remote first, with headquarters in Fairview.",
]
_NOT_AN_OFFER = [
    *_PERK_LINES,
    *_EQUIPMENT_PERK_LINES,
    *_CONDITIONAL_BENEFIT_BLOCKS,
    *_CULTURE_LINES,
]
_GENUINE_OFFERS = [
    "We are located in the Bay Area, but we are open to remote candidates within the United"
    " States.",
    "We are based in Northtown, but we are open to remote candidates.",
    "This role can be performed remotely.",
    "This position can be performed remotely from anywhere in the US.",
    "Remote candidates will be considered.",
    "Remote applicants will be considered for this role.",
    "We are open to remote candidates for this role anywhere in the US.",
    "This role is fully remote.",
    "You may work remotely from anywhere in Canada.",
    "Remote work available.",
    "We offer a stipend for this fully remote position, plus home office equipment.",
    "We offer a stipend for this remote role and remote work equipment.",
    "Reimbursement of travel for this remote role and home office furniture.",
    "Budget for working remotely and home office equipment.",
    "Budget for working from anywhere and home office equipment.",
    "Stipend of $500 for teleworkers and home office equipment.",
]


@pytest.mark.parametrize("text", _EQUIPMENT_PERK_LINES)
def test_a_home_working_equipment_benefit_is_a_perk(text: str) -> None:
    assert remote_perk_or_culture(text)


@pytest.mark.parametrize("text", _NOT_AN_OFFER)
def test_a_remote_perk_conditional_benefit_or_culture_line_is_no_offer(text: str) -> None:
    assert remote_offer_statement(text) is None


@pytest.mark.parametrize("offer", _GENUINE_OFFERS)
@pytest.mark.parametrize("beside", ["", *_NOT_AN_OFFER[::7]])
def test_a_genuine_offer_reads_beside_a_perk_conditional_or_culture_line(
    beside: str, offer: str
) -> None:
    assert remote_offer_statement(f"{beside}\n\n{offer}" if beside else offer) == offer
