"""Non-requirement text is refused where it enters and where it is read."""

from __future__ import annotations

import pytest

from clearscout.domain.requirements import is_boilerplate_requirement

BOILERPLATE = (
    "PUBLIC BURDEN STATEMENT: According to the Paperwork Reduction Act of 1995"
    " no persons are required to respond to a collection of information",
    "We are a federal contractor or subcontractor. The law requires us to"
    " provide equal employment opportunity to qualified people with disabilities",
    "Employer follows an affirmative action program in accordance with 41 CFR",
    "indicates a required field",
    "Methodological Expertise:</strong></div><div><br></div><div><br></div><ul><li>"
    "Provide relevant and useful literature review",
    "The role requires sitting for extended periods",
    "Extensive keyboard use",
    "Occasional walking or lifting up to 25 pounds",
    "Ability to reach, bend, and lift objects up to 15 lbs",
    "Reasonable accommodations may be made to enable individuals with disabilities",
    "Physical demands: prolonged sitting and repetitive keystrokes",
    "Close vision and visual acuity required",
)

REQUIREMENTS = (
    "5+ years of Software Product Management experience",
    "Strong technical foundation, including working knowledge of APIs",
    "Bachelor's degree in Computer Science or related field",
    "Must be legally authorized to work in Canada",
    # A real requirement mentioning 'equal' but not the disclosure phrase.
    "Delivers equal weight to quantitative and qualitative research",
    # Physical-demand vocabulary appears in legitimate PM responsibilities.
    "Skilled at hearing customer needs and translating them into a roadmap",
    "Deep domain expertise in payments and standing up new merchant integrations",
    "Comfortable reaching across teams to align on shared goals",
)


@pytest.mark.parametrize("text", BOILERPLATE)
def test_regulatory_and_ui_boilerplate_is_refused(text: str) -> None:
    assert is_boilerplate_requirement(text) is True


@pytest.mark.parametrize("text", REQUIREMENTS)
def test_actual_requirements_are_kept(text: str) -> None:
    assert is_boilerplate_requirement(text) is False


COMPUTER_USE_NOTICES = (
    "Ability to use computers and keyboards extensively",
    "Ability to use a computer and keyboard extensively.",
    "Extensive use of a computer and keyboard",
    "Regular use of a computer keyboard and mouse for extended periods",
    "Must be able to operate a computer for long periods",
    "Able to work at a computer for prolonged periods of time",
    "Prolonged computer use",
    "Extended screen time",
    "Repetitive use of a keyboard and mouse",
    "Frequent use of computer, keyboard, and mouse",
    "Long periods of screen time",
    "Long periods of time in front of a computer screen",
    "Constant use of computer monitors",
)

COMPUTER_SKILLS = (
    "Proficiency with computers and Microsoft Office",
    "Experience using computer-aided design tools",
    "Advanced keyboard shortcuts in Excel",
    "Computer science degree",
    "Ability to use data to drive decisions",
    "Extensive experience with computer vision",
    "Extensive use of SQL and Python for analysis",
    "Regular use of analytics dashboards to monitor product health",
    "Ability to use computer modeling software extensively",
    "Long periods of ambiguity in early-stage product work",
)


@pytest.mark.parametrize("text", COMPUTER_USE_NOTICES)
def test_computer_use_physical_notices_are_refused(text: str) -> None:
    assert is_boilerplate_requirement(text) is True


@pytest.mark.parametrize("text", COMPUTER_SKILLS)
def test_computer_skill_requirements_are_kept(text: str) -> None:
    assert is_boilerplate_requirement(text) is False


def test_empty_text_is_not_boilerplate() -> None:
    assert is_boilerplate_requirement("") is False
