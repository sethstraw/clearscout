"""Every location provider is classified exhaustive or not, with nothing silent."""

from __future__ import annotations

import ast
from itertools import product
from pathlib import Path

import pytest

from clearscout.domain.arrangement_grammar import attendance_statement, remote_offer_statement
from clearscout.domain.staged_facts import FreshnessState, PublishedFact
from clearscout.infrastructure.web_fetch.board_mappings import BOARD_DECLARATIONS
from clearscout.infrastructure.web_fetch.listing_facts import (
    EXHAUSTIVE_LOCATION_PROVIDERS,
    NON_EXHAUSTIVE_LOCATION_PROVIDERS,
    exhaustive_location_provider,
    lever_arrangement_values,
    linkedin_arrangement_signal,
    location_stated_arrangement,
    market_count,
    published_listing_facts,
)
from clearscout.shared.layout import plain_layout

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "clearscout"


def _provenance_literals() -> set[str]:
    """Return every literal platform string passed to published_listing_facts in src."""
    found: set[str] = set()
    for path in SRC_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _calls_published_listing_facts(node)):
                continue
            literal = _first_platform_argument(node)
            if literal is not None:
                found.add(literal)
    return found


def _calls_published_listing_facts(node: ast.Call) -> bool:
    func = node.func
    name = (
        func.id
        if isinstance(func, ast.Name)
        else func.attr
        if isinstance(func, ast.Attribute)
        else ""
    )
    return name == "published_listing_facts"


def _first_platform_argument(node: ast.Call) -> str | None:
    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
        return node.args[0].value
    return None


def test_every_provenance_passed_to_published_listing_facts_is_classified() -> None:
    """A new adapter must be sorted into the exhaustive or non-exhaustive set."""
    used = _provenance_literals() | {item.platform for item in BOARD_DECLARATIONS}
    known = EXHAUSTIVE_LOCATION_PROVIDERS | NON_EXHAUSTIVE_LOCATION_PROVIDERS
    unclassified = used - known
    assert not unclassified, (
        f"Provenance(s) {sorted(unclassified)} passed to published_listing_facts are not "
        "classified as exhaustive or non-exhaustive location providers."
    )
    assert used, "No published_listing_facts call sites were found to classify."


def test_exhaustive_and_non_exhaustive_provider_sets_do_not_overlap() -> None:
    assert not (EXHAUSTIVE_LOCATION_PROVIDERS & NON_EXHAUSTIVE_LOCATION_PROVIDERS)


def test_exhaustive_location_provider_strips_the_location_suffix() -> None:
    assert exhaustive_location_provider("greenhouse.location") is True
    assert exhaustive_location_provider("greenhouse") is True
    assert exhaustive_location_provider("icims.location") is False
    assert exhaustive_location_provider("unknown_provider") is False


_ATTENDANCE_PROBES: tuple[str, ...] = (
    "This position requires you to be in the office 3 days per week.",
    "This is an on-site role at our Austin office, Monday through Friday.",
    "The role is hybrid, with in-office days Tuesday and Thursday.",
    "You will be in the office 8 days a month.",
    "This role will be a hybrid role based in our Brisbane, California headquarters.",
    "Hybrid: Eight days a month, commuting to our closest office.",
    "This role requires 3 days per week in office.",
    "Our hybrid model requires employees to be in office three days a week.",
    "This team follows a hybrid model requiring 3 days per week in office.",
    "You are required to be in office 4 days per week.",
    "This position requires working in the office in Burlington, MA, at least 3 days per week.",
    # A requirement stated after the cadence in the same clause.
    "In the office 3 days per week is required.",
    "3 days per week in office is required.",
    "In-office presence 3 days per week required.",
    # Weekday schedules beside the office phrase or a "Hybrid:" label.
    "You must be on-site Monday through Thursday, 4 days per week.",
    "You must be in the office Tuesday-Thursday (3 days per week).",
    "Hybrid: Tuesday through Thursday, 3 days per week.",
    "Hybrid: Tues, Wed, Thurs in the office (3 days per week).",
    "This role requires in-office work 3 days per week.",
    "Previous experience required; you will be on-site 3 days per week.",
)

_NON_ATTENDANCE_PROBES: tuple[str, ...] = (
    "#LI-hybrid",
    "For Hybrid Roles: If this role is Hybrid, you will work from our office.",
    "For Hybrid Roles: If this role is Hybrid, you will work from our office. #LI-remote",
    "We embrace a hybrid work model. This role is fully remote.",
    "Our offices in New York and London are hybrid.",
    "This role is not hybrid; it is fully remote.",
    "No office attendance is required.",
    "This position can be Remote or Hybrid out of our Toronto office.",
    "Remote (US) or hybrid in Seattle.",
    "Remote, with quarterly on-site team gatherings.",
    "Travel to HQ up to 10% for onsite planning.",
    "This is a remote role; must live within 50 miles of a hub for occasional in-person meetings.",
    "Experience with hybrid cloud infrastructure.",
    "Final round is an on-site interview at our office.",
    "On-site customer visits up to 20%.",
    "Once a month in the office.",
    # A commuting-radius rule is a residence rule, not an office requirement.
    "Must live within 50 miles of our office.",
    # A cadence beside a perk states a benefit, not a requirement.
    "Employees in the office 3 days per week receive lunch.",
    # A bare "office"/"on-site" mention describes something, without requiring attendance.
    "Our Toronto office is open five days a week for anyone who wants it.",
    "Our on-site gym is open 7 days a week.",
    "The support desk in our office is staffed 5 days a week.",
    "Teams meet in the office 2 days per week, and this role can join them.",
    "This hybrid role blends data engineering and product management.",
    "You will be required to attend on-site training 2 days per week during onboarding.",
    "The team works on-site at customer hospitals; you must be comfortable with "
    "3 days per week of travel.",
    # A softened, optional, or capped cadence never commits to attendance.
    "Employees are not required to be in office, though many choose 2 days per week.",
    "Being in office 2 days per week is encouraged but not required.",
    "We are not requiring anyone to be in office 3 days per week.",
    "Home-based role; in office as required, about 1 day per week.",
    "Flexible hybrid: in office 1 day per week is optional.",
    "Occasional in office collaboration, typically 1 day per week, may be required.",
    "Up to 2 days per week in office is required.",
    # A day count near "office" that is not the employee's own attendance.
    "You will support our in office team 5 days per week.",
    "You will provide front desk coverage for in office staff five days a week.",
    "You will coordinate in office events and catering 2 days per week.",
    "This is a work-from-home position with in office days as required, typically 1 day per week.",
    "You will be working from home, with in office meetings as required 1 day per week.",
    "Please keep in office hours between 9 and 5, 5 days a week.",
    "Experience working in office environments 5 days per week.",
    "Proficiency in Office 365 is required 3 days per week.",
    "Requiring travel 3 days per week in office locations across the region.",
    # A trial-period cadence is not the posting's standing commitment.
    "For the first month you are required to be in office 3 days per week.",
    "In the first three months, you will be in office 5 days per week.",
    "During probation, you must be in office 5 days per week.",
    "Initially you are required to be in office 3 days per week, moving to fully flexible.",
    # A bare "in office" cadence with no requirement, a negation, or no weekly unit.
    "We don't require 3 days in office.",
    "0 days in office required.",
    "You must be in office 0 days per week.",
    "No requirement to be in office 3 days per week.",
    "You will not be required to be in office 3 days per week.",
    "It is no longer required to be in office 3 days per week.",
    "This role requires 3 days per week in Office 365 and Teams.",
    "The team gathers in office 2 days a year for planning.",
    "Many of our employees enjoy 3 days in office.",
    "We meet in office 2 days a quarter.",
    "Most teams choose 3 days in office.",
    "You may choose to work in office 3 days a week.",
    "Perks: stipend; 2 days in office voluntary; learning budget.",
    "Clients visit us 3 days in office each week.",
    "This role requires 4 days in office.",
    "Required: 3 days per week in office administration duties.",
    "You will spend 3 days per week in office supply audits.",
    "Requiring 2 days per week in office for employees within 50 miles of a hub.",
    "This role requires 3 days per week in the office to start and for the first year.",
    # The count or the requirement belongs to another clause, person, or period.
    "You will work 5 days per week, with 2 days in office each quarter.",
    "You will work 5 days per week, with 2 days in the office each quarter.",
    "You will join a team that averages 2 days per week in office.",
    "Your manager works 4 days per week in office, and you will report to them.",
    "We previously required 5 days a week in office.",
    "<li>You will partner with sales leaders</li><li>Office perks: 2 days per week in office"
    " with catered lunch</li>",
    "You won't be required to be in office 3 days per week.",
    "Candidates aren't required to be in office 3 days per week.",
    "You are not currently required to be in office 3 days per week.",
    "Hybrid: 0 days per week in office.",
    "You will spend 1 day per week in office at the client site.",
    "You will lead a team that is 3 days per week in office while you work from anywhere.",
    "Hybrid: 3 days per week in office is flexible and not enforced.",
    "Required: available 5 days a week; in office 1 day a month.",
    "You will be in office 2 days per quarter, working 5 days per week.",
    "You will be in the office 2 days per quarter, working 5 days per week.",
    # A heading's condition governs the bullets under it.
    "<p>For employees within 50 miles of a hub:</p><ul><li>You are required to be in the"
    " office 3 days per week</li></ul>",
    "<p>If the role is hybrid:</p><ul><li>You must be in the office 3 days per week</li></ul>",
    "Where applicable:<br>you will be in the office 3 days per week.",
    "<p>Depending on team:</p><ul><li>You must be in office 3 days per week</li></ul>",
    # "Office" as a modifier, or someone else's cadence.
    "You will report to the Office Manager 5 days a week.",
    "You need access to the office network VPN 5 days per week.",
    "You will travel to the office quarterly and work 5 days a week.",
    "You will support executives who are in the office 3 days per week.",
    "You will join a group that is in the office 3 days per week.",
    "Hybrid: occasional on-site; 5 days per week core hours.",
    "Required: on-site support for launches, and availability 5 days per week.",
    "You will work on-site at events, on a schedule of 5 days per week.",
    "This role requires 0 days per week in the office.",
    "You will be supported by the in-office team 5 days a week.",
    "You will provide on-site support for the office move 2 days per week.",
    "You will be expected to answer calls at the office phone line 5 days a week.",
    "<p><b>Hybrid:</b></p><ul><li>Local hires 3 days per week in the office</li>"
    "<li>Out-of-area hires fully distributed</li></ul>",
    "Hybrid: see details below Team socials happen in the office 2 days per week",
    "Work model Hybrid: flexible Team lunch in the office 1 day per week",
    "Previous policy Hybrid: 3 days per week.",
    "You will run on-site and virtual workshops 2 days per week.",
    "You will organize the in-office Tuesday lunch, about 1 day per week.",
    "You will lead on-site Monday stand-ups for the plant 2 days per week.",
    "You will coordinate on-site work orders 2 days per week.",
    "You will send reports to the office weekly and work 5 days per week.",
    "You will be supported at the office by a team of 5 days per week coordinators.",
    "You will run the in-office downtown pop-up 2 days per week.",
)

_REMOTE_OFFER_PROBES: tuple[str, ...] = (
    "Remote work is available for this role.",
    "You can work remotely.",
    "Work from home is an option.",
    "This position is eligible for remote work.",
    "Remote (Canada) candidates welcome.",
    "Remote candidates will be considered.",
    # A local-hire preference beside the offer does not withdraw it.
    "Local candidates preferred; remote candidates will be considered.",
    "Remote is possible for strong candidates; local candidates get priority.",
    "We welcome remote applicants, though local hires are preferred.",
)


def test_attendance_statement_reads_only_a_genuine_attendance_commitment() -> None:
    for text in _ATTENDANCE_PROBES:
        result = attendance_statement(text)
        assert result is not None, text
        assert result[0] in {"hybrid", "on_site"}, text
    for text in _NON_ATTENDANCE_PROBES:
        assert attendance_statement(text) is None, text


def test_each_item_line_of_a_list_is_a_sentence_of_its_own() -> None:
    """An unpunctuated item never borrows the condition stated in the item after it."""
    body = (
        "What to expect:\n"
        "- This role is hybrid, with three days a week in the Toronto office\n"
        "- Relocation support if you live outside Ontario"
    )
    assert attendance_statement(body) == (
        "hybrid",
        "- This role is hybrid, with three days a week in the Toronto office",
    )


def test_remote_offer_statement_finds_an_offer_but_not_a_conditional_boilerplate() -> None:
    offer = remote_offer_statement("This position can be Remote or Hybrid out of Toronto.")
    assert offer is not None
    assert remote_offer_statement("We embrace a hybrid work model. #LI-remote") is not None
    assert (
        remote_offer_statement(
            "For Hybrid Roles: If this role is Hybrid, you will work from our office."
        )
        is None
    )
    assert remote_offer_statement("This is a hybrid role based in our office.") is None


def test_lever_arrangement_values_moves_a_body_remote_offer_to_unresolved() -> None:
    arrangements, unresolved = lever_arrangement_values(
        "hybrid", "Mississauga, Ontario", "For Hybrid Roles: If this role is Hybrid. #LI-remote"
    )
    assert arrangements == []
    assert unresolved[0] == "hybrid"
    assert "remote" in unresolved[1]


def test_lever_arrangement_values_keeps_hybrid_when_the_body_has_no_remote_offer() -> None:
    arrangements, unresolved = lever_arrangement_values(
        "hybrid",
        "Mississauga, Ontario",
        "For Hybrid Roles: If this role is Hybrid, you will work from our office.",
    )
    assert arrangements == ["hybrid"]
    assert unresolved == []


def test_remote_offer_statement_matches_every_broadened_phrasing() -> None:
    """Any non-conditional, non-refused, non-negated sentence naming remote work is an offer."""
    for text in _REMOTE_OFFER_PROBES:
        assert remote_offer_statement(text) is not None, text


@pytest.mark.parametrize(
    "text",
    [
        "Remote work is not offered for this role.",
        "This is not a remote role.",
        "We do not offer remote work at this time.",
        "Remote patient monitoring is a key product area.",
        "Our remote monitoring team supports this product.",
    ],
)
def test_remote_offer_statement_rejects_negated_and_noun_phrase_mentions(text: str) -> None:
    """A negated sentence, either word order, and a "remote X" noun phrase are never an offer."""
    assert remote_offer_statement(text) is None, text


@pytest.mark.parametrize(
    "text",
    [
        "Our roles are structured across four work models: site-essential, site-by-design,"
        " field-based and remote-by-design.",
        "This structure includes site-essential, site-by-design, field-based and"
        " remote-by-design jobs.",
        "Our positions are classified as site-essential, site-by-design, field-based or"
        " remote-by-design.",
        "We have three work models: on-site, hybrid, and remote.",
        "Roles fall into three work models: in-office, hybrid and remote.",
        "For field-based and remote-by-design roles the ability to travel to visit customers is"
        " required.",
        "For site-by-design, field-based, or remote roles, a laptop is provided.",
    ],
)
def test_remote_listed_as_one_work_model_among_several_is_not_an_offer(text: str) -> None:
    assert remote_offer_statement(text) is None


@pytest.mark.parametrize(
    "text",
    [
        'This position is currently classified as "hybrid" in accordance with Acme\'s Hybrid'
        " and Remote Work policy.",
        'This role is classified as "hybrid" following the Hybrid & Remote Work Policy.',
        "This role is office-based under the Acme Remote Work Policy.",
        "Combined Pay & Temp Status Disclosure (FOR REMOTE AND/OR CALIFORNIA BASED ROLES):",
        "Pay transparency (for remote and/or Colorado-based roles):",
        "For remote and/or New York based positions, the range is listed below.",
    ],
)
def test_a_remote_policy_name_or_pay_scope_header_is_not_an_offer(text: str) -> None:
    assert remote_offer_statement(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "Per our Remote Work Policy, this role may be performed remotely.",
        "This role is remote under our Hybrid and Remote Work policy.",
        "This role is open to remote and/or Denver-based candidates.",
        "This position is eligible under our Remote Work Policy.",
    ],
)
def test_a_remote_offer_beside_a_policy_name_or_pay_scope_is_still_an_offer(text: str) -> None:
    assert remote_offer_statement(text) is not None


@pytest.mark.parametrize(
    "text",
    [
        "Hybrid : work remotely up to two days per week; or select",
        "- Hybrid: work remotely up to two days per week.",
        "This is a hybrid role which allows for 2 days per week remote work and 3 days per week"
        " in the Boston office.",
        "We offer a flexible hybrid work model, with three days onsite and two days working"
        " remotely each week.",
        "Hybrid schedule: 3 days in the office, 2 days remote per week.",
        "Hybrid (work remotely 2 days a week).",
    ],
)
def test_remote_days_counted_inside_a_hybrid_cadence_are_not_an_offer(text: str) -> None:
    assert remote_offer_statement(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "This position will be located at Springfield, NJ site and will not have the ability"
        " to be located remotely.",
        "This role does not offer the option of working fully remotely.",
        "Candidates will not have the opportunity to work fully remotely.",
        "This position isn't eligible at this time to be performed remotely.",
    ],
)
def test_a_negated_ability_to_work_remotely_is_not_an_offer(text: str) -> None:
    assert remote_offer_statement(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "Experience operating within a remote, contractor/consulting engagement model with a"
        " monthly governance reporting cadence is a plus.",
        "- Experience working in a remote-first or distributed organization.",
        "Experience managing remote teams across time zones.",
        "Prior experience working remotely is preferred.",
        "Demonstrated experience leading remote, cross-functional teams.",
        "- Experience working in multicultural or remote team settings.",
    ],
)
def test_remote_experience_asked_of_a_candidate_is_not_an_offer(text: str) -> None:
    assert remote_offer_statement(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "Regular office presence is expected with flexibility for remote work as agreed with"
        " your manager.",
        "This role offers a hybrid working model, with a mix of on-site and remote work.",
        "Our teams operate in a flexible hybrid work model, combining in-person collaboration"
        " with remote flexibility.",
        "Hybrid work model with time split between our Springfield office and remote work.",
        "This role follows a hybrid work model, offering the flexibility to work remotely while"
        " maintaining regular in-person collaboration.",
    ],
)
def test_remote_flexibility_inside_a_hybrid_model_is_not_an_offer(text: str) -> None:
    assert remote_offer_statement(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "Location: Indianapolis, IN preferred. Remote candidates will be considered for the right"
        " experience.",
        "This position can be based remotely anywhere in the U.S.",
        "Location: Remote - This position can be based anywhere in the US.",
        "Join us as a Director, Clinical Operations based remotely reporting to the Senior"
        " Director.",
        "Remote work options may be considered on a case-by-case basis and if approved by the"
        " Company.",
        "The preferred location for this role is the New Jersey site but remote work may be"
        " possible.",
        "This role is remote with 20-30% in travel across Canada.",
        "This position can be on-site, hybrid, or remote.",
        "This role is remote-by-design.",
        "This role can be hybrid, 3 days in the office, or fully remote.",
        "Hybrid: 3 days a week in the office. Remote candidates will be considered.",
        "Candidates who are not local will have the ability to work remotely.",
        "This is a remote role; experience in healthcare is a plus.",
        "Experience the freedom of fully remote work.",
        "Remote candidates with experience in Python will be considered.",
        "This role is remote, with flexibility to work on-site when you choose.",
        "We offer the flexibility to work remotely.",
        "Hybrid or fully remote: remote 5 days a week is available.",
        "Hybrid schedule available, or work remotely 5 days a week if you prefer.",
        "This role is hybrid, with flexibility to work remotely full-time for candidates outside"
        " the region.",
        "Candidates who are not eligible for relocation may work remotely.",
        "Open to on-site, hybrid, or remote candidates.",
        "Location: Remote, hybrid, or on-site within the US.",
        "This opening can be on-site, hybrid, or remote.",
        "We hire on-site, hybrid, and remote across the US.",
    ],
)
def test_a_remote_offer_beside_a_near_miss_phrase_is_still_an_offer(text: str) -> None:
    assert remote_offer_statement(text) is not None


_DEFERRALS = [
    f"{noun} will have different workplace arrangements which {verb} {participle} {place}."
    for noun, verb, participle, place in product(
        ("Remote or field-based positions", "Remote roles", "Field or remote jobs"),
        ("will be", "are", "is"),
        ("indicated", "noted", "specified", "listed"),
        ("in the job posting", "on each job description", "within their requisition"),
    )
]
_HYBRID_FLEXIBILITY = [
    "We offer a hybrid working solution allowing time in the office and the flexibility to"
    " also work from home.",
    "Hybrid workplace model - flexibility to work from home within the respective country and"
    " the office.",
    "Our hybrid model gives you the flexibility of working remotely part of the week.",
]
_TRUE_OFFERS = [
    "Remote work is available.",
    "This position can be performed remotely.",
    "Open to remote candidates.",
    "This role is 100% remote.",
]


@pytest.mark.parametrize("text", _DEFERRALS + _HYBRID_FLEXIBILITY)
def test_deferral_boilerplate_and_hybrid_home_flexibility_are_not_an_offer(text: str) -> None:
    assert remote_offer_statement(text) is None


@pytest.mark.parametrize(
    ("text", "offer"), list(product(_DEFERRALS[::6] + _HYBRID_FLEXIBILITY, _TRUE_OFFERS))
)
def test_a_real_offer_after_deferral_or_hybrid_flexibility_is_still_read(
    text: str, offer: str
) -> None:
    assert remote_offer_statement(f"{text} {offer}") == offer


@pytest.mark.parametrize(
    "text",
    [
        "This is a fully remote position and pay ranges are outlined in the job posting.",
        "This is a remote role and eligible states are listed in the job posting.",
        "This is a remote role, and responsibilities are listed in the job description.",
        "Remote roles are open in the locations that are listed in the job posting.",
        "We are hiring for remote positions in the states that are specified in the job"
        " description.",
        "Remote positions are listed on each job posting.",
        "This remote role's schedule is noted in the job description.",
        "This is a remote-based role and the territory is outlined in the job description.",
    ],
)
def test_a_remote_role_beside_other_posting_details_is_still_an_offer(text: str) -> None:
    assert remote_offer_statement(text) is not None


_REMOTE_TASK_PHRASES = [
    "remote care",
    "remote-care",
    "remote conferencing",
    "remote technical support",
    "remote expert",
    "remote experts",
    "remote coverage",
    "remote detailing",
    "remote diagnostics",
    "remote diagnostic",
    "remote caching",
    "remote cache",
    "remote caches",
    "remote execution",
    "remote supervision",
    "remote pilot",
    "remote calls",
    "remote call",
    "remote development",
    "remote desktop",
    "Remote Calls",
]
_TASK_FRAMES = [
    "- Provide {phrase} for clinical teams.",
    "The group delivers {phrase} services across acute settings.",
    "Manage CRM processes (event invitations, consent, {phrase}, etc.).",
    "Experience with {phrase} tooling is a plus.",
    "{phrase}",
]


@pytest.mark.parametrize(
    "text",
    [frame.format(phrase=phrase) for phrase in _REMOTE_TASK_PHRASES for frame in _TASK_FRAMES],
)
def test_remote_modifying_a_task_or_product_noun_is_not_an_offer(text: str) -> None:
    assert remote_offer_statement(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "- Provide remote technical support to agents in the field.",
        "- Provide remote supervision of technicians at partner sites.",
        "- Coordinate remote coverage for nurses on leave.",
        "- Support remote calls with sales managers.",
        "Use remote caching for build engineers.",
        "Manage remote experts and field engineers on site.",
        "Responsibilities: remote coverage for overnight reads.",
    ],
)
def test_a_role_noun_beyond_a_function_word_does_not_make_a_task_an_offer(text: str) -> None:
    assert remote_offer_statement(text) is None


@pytest.mark.parametrize(
    ("phrase", "offer"), list(product(_REMOTE_TASK_PHRASES[::3], _TRUE_OFFERS))
)
def test_an_offer_after_a_remote_task_phrase_is_still_read(phrase: str, offer: str) -> None:
    assert remote_offer_statement(f"- Provide {phrase} for clinical teams. {offer}") == offer


@pytest.mark.parametrize(
    "text",
    [
        "This is a remote role providing remote technical support to pharmacies.",
        "You will work remotely and lead remote care programs.",
        "Fully remote: coordinate remote coverage for trainers.",
        "We are hiring a remote engineer to own remote caching.",
        "We are hiring for remote development roles across Canada.",
        "This is a remote care position.",
        "This is a remote care coordinator position.",
        "Remote Technical Support Specialist",
        "We are hiring a Remote Call Center Agent.",
        "As a Remote Care Nurse you will monitor patients.",
    ],
)
def test_a_sentence_with_a_remote_task_phrase_and_an_offer_is_still_an_offer(text: str) -> None:
    assert remote_offer_statement(text) == text


_REMOTE_MANNER_SKILLS = [
    *(
        f"{lead} in a remote {noun} with clinical teams."
        for lead in ("Able to work effectively", "Collaborates well", "Comfortable operating")
        for noun in ("manner", "fashion", "way")
    ),
    *(
        f"{lead} remotely {prep} {obj}."
        for lead in ("Comfortable working", "Supports users", "Troubleshoots issues")
        for prep, obj in (
            ("on", "shared networks and secure systems"),
            ("through", "screen sharing and ticketing"),
            ("via", "remote desktop tools"),
            ("using", "VPN connections"),
        )
    ),
    "Supports users remotely.",
    "Assists customers remotely when issues arise.",
    "Troubleshooting devices remotely and on site.",
    *(
        f"Thrives on teamwork in {article}{left} and remote {noun}."
        for article in ("", "a ")
        for left in ("global", "distributed", "virtual")
        for noun in ("environments", "context", "settings")
    ),
    "Strong teamwork skills in distributed or remote environments.",
    "Computer skills that include working remotely on shared networks and cloud-based systems.",
    "Support pharmacy automation remotely through a rotating on-call schedule.",
    "Supported pharmacy operations remotely.",
    "Diagnosing faults remotely and on site.",
    "Experience building services in a distributed or remote environment.",
]


@pytest.mark.parametrize("text", _REMOTE_MANNER_SKILLS)
def test_working_in_a_remote_manner_or_supporting_users_remotely_is_not_an_offer(
    text: str,
) -> None:
    assert remote_offer_statement(text) is None


@pytest.mark.parametrize(
    ("text", "offer"),
    list(product(_REMOTE_MANNER_SKILLS[::4], [*_TRUE_OFFERS, "This is a fully remote role."])),
)
def test_an_offer_after_a_remote_manner_phrase_is_still_read(text: str, offer: str) -> None:
    assert remote_offer_statement(f"{text} {offer}") == offer


@pytest.mark.parametrize(
    "text",
    [
        "You can work remotely from anywhere in Canada.",
        "This role is remote.",
        "Work remotely through our Canadian entity.",
        "Remote work environment with flexible hours.",
        "We are a remote-first company.",
        "You will work in a remote environment.",
        "Supports users across the region; this role is fully remote.",
        "You will work remotely using our secure systems.",
        "This role works remotely using company-provided tools.",
        "Employees work remotely via a secure VPN connection.",
        "You can work remotely on our cloud platform from anywhere in the US.",
        "We support employees working remotely across the US.",
        "Our culture supports working remotely.",
        "This position can be performed in a remote manner.",
        "This job may be performed in a remote fashion from your home office.",
        "You will join our global and remote team.",
        "This role is part of a distributed and remote team.",
        "Remote Desktop Support Specialist",
    ],
)
def test_a_true_offer_beside_remote_manner_wording_stays_an_offer(text: str) -> None:
    assert remote_offer_statement(text) == text


def test_a_deferral_followed_by_a_fully_remote_role_returns_that_offer() -> None:
    offer = "This role is fully remote within the US."
    body = (
        "Remote or field-based positions will have different workplace arrangements which will"
        f" be indicated in the job posting. {offer}"
    )
    assert remote_offer_statement(body) == offer


def test_lever_arrangement_values_moves_a_remote_location_in_the_list_to_unresolved() -> None:
    arrangements, unresolved = lever_arrangement_values(
        "hybrid", ["Remote - Canada", "Toronto, Ontario"]
    )
    assert arrangements == []
    assert unresolved[0] == "hybrid"


@pytest.mark.parametrize(
    ("location", "arrangement"),
    [
        ("Palo Alto - Hybrid", "hybrid"),
        ("NY office", "onsite"),
        ("United States - Remote", "remote"),
        ("Boston, Massachusetts", None),
        ("Head Office", "onsite"),
    ],
)
def test_location_stated_arrangement_reads_only_a_trailing_attendance_word(
    location: str, arrangement: str | None
) -> None:
    """A place name that happens to start with "Remote" states no arrangement."""
    assert location_stated_arrangement(location) == arrangement


def test_published_listing_facts_reads_an_arrangement_from_the_location_when_unstated() -> None:
    """A board with no dedicated arrangement field still surfaces one stated in the location."""
    facts = published_listing_facts("bamboohr", locations="Palo Alto - Hybrid")
    assert facts.arrangements.values == ("hybrid",)
    assert facts.arrangements.complete
    assert facts.arrangements.provenance == "bamboohr.location_phrase.arrangement"


@pytest.mark.parametrize(
    ("location", "description", "stated"),
    [
        (
            "Riverbend - Remote",
            "This hybrid role is based in Riverbend and requires commuting to our "
            "office up to three days per week.",
            "remote",
        ),
        ("Palo Alto - Hybrid", "This is a fully remote role.", "hybrid"),
        (
            "Riverbend - Remote",
            "You are required in the office three days per week and may work from home the "
            "other two.",
            "remote",
        ),
    ],
)
def test_published_listing_facts_leaves_a_location_arrangement_the_body_contradicts_unresolved(
    location: str, description: str, stated: str
) -> None:
    """A location suffix is not the fact when the posting's own body states otherwise."""
    facts = published_listing_facts("greenhouse", locations=location, description=description)
    assert facts.arrangements.values == ()
    assert not facts.arrangements.complete
    assert facts.arrangements.unresolved_values[0] == stated
    assert description in facts.arrangements.unresolved_values[1]


@pytest.mark.parametrize(
    "description",
    [
        "This is a fully remote role.",
        "This is a hybrid role at the intersection of product and engineering.",
        "During onboarding you will be in the office three days a week.",
        "Employees were required to be in the office three days a week until last year.",
    ],
)
def test_published_listing_facts_keeps_a_location_arrangement_the_body_does_not_contradict(
    description: str,
) -> None:
    facts = published_listing_facts(
        "greenhouse", locations="United States - Remote", description=description
    )
    assert facts.arrangements.values == ("remote",)
    assert facts.arrangements.complete


def test_published_listing_facts_never_infers_an_arrangement_from_a_bare_place() -> None:
    facts = published_listing_facts("bamboohr", locations="Boston, Massachusetts")
    assert facts.arrangements.values == ()
    assert not facts.arrangements.complete


def test_published_listing_facts_keeps_each_unresolved_reason_given_as_a_tuple() -> None:
    facts = published_listing_facts(
        "greenhouse",
        arrangements_unresolved=("Hybrid", "publisher body offers remote: Work from anywhere."),
    )
    assert facts.arrangements.unresolved_values == (
        "Hybrid",
        "publisher body offers remote: Work from anywhere.",
    )


def test_published_listing_facts_states_role_aliases_read_from_the_description() -> None:
    facts = published_listing_facts("lever", role_aliases=["Clinical Data Manager"])
    assert facts.role_aliases.values == ("Clinical Data Manager",)
    assert facts.role_aliases.provenance == "lever.job_description"
    assert facts.role_aliases.complete


def test_published_listing_facts_leaves_unread_role_aliases_incomplete() -> None:
    facts = published_listing_facts("lever", locations="Northtown")
    assert facts.role_aliases == PublishedFact()
    assert facts.freshness is FreshnessState.UNKNOWN


def test_published_listing_facts_carries_the_freshness_hint_it_is_given() -> None:
    facts = published_listing_facts("schema_org", freshness=FreshnessState.LIKELY_STALE)
    assert facts.freshness is FreshnessState.LIKELY_STALE


@pytest.mark.parametrize(
    ("tag", "listing"),
    [("#LI-Remote", "remote"), ("#LI-Hybrid", "hybrid"), ("#LI-Onsite", "onsite")],
)
def test_a_linkedin_tag_glued_to_the_next_block_is_still_read(tag: str, listing: str) -> None:
    body = plain_layout(f"Apply by Friday.\n{tag}The data team at Northwind Rail plans trains.")
    assert linkedin_arrangement_signal(body).value == listing
    assert "The data team" in body


@pytest.mark.parametrize(
    ("tag", "listing"),
    [
        ("#LI-On-site", "onsite"),
        ("#LI-On-Site", "onsite"),
        ("#LI_Remote", "remote"),
        ("# LI-Remote", "remote"),
    ],
)
def test_every_separator_in_a_linkedin_tag_reads_the_same(tag: str, listing: str) -> None:
    assert linkedin_arrangement_signal(f"We plan trains.\n{tag}").value == listing


def test_a_linkedin_tag_conflict_quotes_only_the_role_sentence_of_a_collapsed_body() -> None:
    body = (
        "Summary #LI-Onsite Location: Northtown. Relocation is not offered. "
        "The preferred location for this role is Northtown but remote work may be possible. "
        "Northwind Rail plans trains for the whole region. Benefits include a pension."
    )
    signal = linkedin_arrangement_signal(body)
    assert signal.value is None
    assert signal.unresolved == (
        "LinkedIn tag #LI-Onsite conflicts with role statement: The preferred location for "
        "this role is Northtown but remote work may be possible.",
    )


@pytest.mark.parametrize(
    ("count", "unit"),
    list(
        product(
            ("3", "three", "three (3)", "3-4", "three to four", "3\u20134", "3~4"),
            (
                "days a week",
                "days per week",
                "days/week",
                "days / week",
                "x per week",
                "x/week",
                "times a week",
                "-days per week",
                "days weekly",
                "days each week",
            ),
        )
    ),
)
def test_every_weekly_office_count_form_commits_to_attendance(count: str, unit: str) -> None:
    joined = unit[0] in "-x/" and not count[-1].isalpha()
    sentence = f"You will be required to work in the office {count}{'' if joined else ' '}{unit}."

    assert attendance_statement(sentence) == ("hybrid", sentence.replace("\u2013", "-"))


@pytest.mark.parametrize(
    ("count", "committed"),
    [("2-4", False), ("two to four", False), ("4-6", True), ("four to six", True)],
)
def test_a_monthly_office_range_is_a_cadence_only_when_its_bottom_is(
    count: str, committed: bool
) -> None:
    sentence = f"You will be required to work in the office {count} days per month."

    assert (attendance_statement(sentence) is not None) is committed


@pytest.mark.parametrize(
    ("value", "count"),
    [
        ("3 Locations", 3),
        ("Three Locations", 3),
        ("3 Locations Available", 3),
        ("3 Locations (US)", 3),
        ("3 Offices", 3),
        ("Multiple Locations", 2),
        ("+2 more", 2),
        ("2 more locations", 2),
    ],
)
def test_every_location_count_summary_states_its_count(value: str, count: int) -> None:
    assert market_count(value) == count
    assert published_listing_facts("greenhouse", locations=[value]).locations.complete is False


@pytest.mark.parametrize(
    ("location", "arrangement"),
    [
        ("Toronto, ON (Hybrid)", "hybrid"),
        ("Toronto, ON - Hybrid.", "hybrid"),
        ("Toronto, ON (Remote)", "remote"),
        ("Toronto - On Site", "onsite"),
        ("Toronto - In-Person", "onsite"),
        ("Toronto - In Person", "onsite"),
        ("Canada - Remote Office", "remote"),
        ("Canada - Home-Office", None),
        ("Canada - Virtual Office", None),
    ],
)
def test_every_trailing_arrangement_form_in_a_location_is_read(
    location: str, arrangement: str | None
) -> None:
    assert location_stated_arrangement(location) == arrangement


@pytest.mark.parametrize(
    ("location", "arrangement"),
    [
        ("Remote", "remote"),
        ("remote", "remote"),
        ("Work from Home", "remote"),
        ("Work From Home", "remote"),
        ("Work-from-home", "remote"),
        ("WFH", "remote"),
        ("Remote Nationwide", "remote"),
        ("Remote Location", "remote"),
        ("Remote-IN", "remote"),
        ("Remote - IN", "remote"),
        ("Remote, Georgia", "remote"),
        ("Remote: Ohio", "remote"),
        ("Remote (US)", "remote"),
        ("AR - Work from home", "remote"),
        ("MS - Work from  home", "remote"),
        ("Remote Oaks", None),
        ("Remote Oaks, CA", None),
        ("Remoteville", None),
        ("Home Office", None),
    ],
)
def test_a_remote_location_label_states_a_remote_arrangement(
    location: str, arrangement: str | None
) -> None:
    """A remote word leading the label, or a trailing work-from-home, names the arrangement."""
    assert location_stated_arrangement(location) == arrangement


@pytest.mark.parametrize(
    "sentence",
    ["WFH role.", "Telecommute position.", "This is a virtual position.", "Home based role."],
)
def test_every_work_from_home_form_is_a_remote_offer(sentence: str) -> None:
    assert remote_offer_statement(sentence) == sentence


def test_a_hybrid_tag_contradicts_a_remote_location() -> None:
    facts = published_listing_facts(
        "greenhouse", locations=["Remote"], description="<p>Build ledgers.</p><p>#LI-Hybrid</p>"
    )

    assert facts.arrangements.values == ()
    assert facts.arrangements.unresolved_values == (
        "remote",
        "publisher body states attendance: LinkedIn hybrid tag",
    )


def test_a_body_attendance_sentence_contradicts_a_remote_location() -> None:
    sentence = "This role is fully onsite at the Fairview Headquarters."
    body = f"<p>We build hybrid cloud ledgers.</p><p>{sentence}</p>"

    facts = published_listing_facts("greenhouse", locations=["Remote"], description=body)

    assert facts.arrangements.values == ()
    assert facts.arrangements.unresolved_values[0] == "remote"
    assert sentence in facts.arrangements.unresolved_values[1]


@pytest.mark.parametrize("tag", ["#LI-Hybrid", "#LI - Hybrid", "#LI -Hybrid"])
def test_a_spaced_linkedin_tag_reads_its_arrangement(tag: str) -> None:
    assert linkedin_arrangement_signal(f"Build ledgers. {tag}").value == "hybrid"


@pytest.mark.parametrize(
    "body",
    [
        "<ul><li>Work-from-Home Stipend</li></ul>",
        "<p>For roles listed as United States - Remote:</p>"
        "<ul><li>Remote work flexibility</li></ul>",
        "<p>We are a remote-first company with hubs in Fairview.</p>",
        "<ul><li>Competitive salary, flexible vacation, and reimbursement of wellness expenses"
        " and telework equipment</li></ul>",
    ],
)
def test_a_stated_hybrid_value_survives_a_perk_conditional_or_culture_line(body: str) -> None:
    assert lever_arrangement_values("Hybrid", "Northtown", body) == (["Hybrid"], [])


_HYBRID_DAY_STATEMENTS = (
    [
        f"This is a hybrid position with a minimum of {count} {days} {place} required"
        " on a weekly basis."
        for count, days, place in product(
            ("1", "2", "3", "4", "three"), ("day", "days"), ("onsite", "on-site", "on site")
        )
    ]
    + [f"This role is hybrid, in office {count} days a week." for count in ("1", "2", "3", "4")]
    + [
        f"This position is hybrid and requires {count} days on-site each week."
        for count in ("two", "four")
    ]
)


@pytest.mark.parametrize("tag", ["#LI-HYBRID", "#LI-Hybrid", "#LI-hybrid", "#LIHybrid"])
@pytest.mark.parametrize("statement", _HYBRID_DAY_STATEMENTS)
def test_a_hybrid_role_statement_naming_office_days_agrees_with_a_hybrid_tag(
    tag: str, statement: str
) -> None:
    for body in (
        f"This role builds ledgers.\n{statement}\n{tag}",
        f"Summary {tag} This role builds ledgers. {statement} Benefits include a pension.",
    ):
        signal = linkedin_arrangement_signal(body)
        assert (signal.value, signal.unresolved) == ("hybrid", ()), body


@pytest.mark.parametrize(
    ("statement", "arrangement"),
    [
        ("This position is hybrid with 3 days onsite each week.", "hybrid"),
        ("This role is fully onsite at the Fairview office.", "onsite"),
    ],
)
def test_conflicting_tags_defer_to_the_role_statement(statement: str, arrangement: str) -> None:
    signal = linkedin_arrangement_signal(f"{statement}\n#LI-Onsite\n#LI-Hybrid")
    assert (signal.value, signal.unresolved) == (arrangement, ())


def test_conflicting_tags_without_a_role_statement_stay_unresolved() -> None:
    signal = linkedin_arrangement_signal(
        "Build ledgers for Northwind Rail.\n#LI-Onsite\n#LI-Hybrid"
    )
    assert signal.value is None
    assert signal.unresolved == ("conflicting LinkedIn tags: #LI-Onsite, #LI-Hybrid",)
