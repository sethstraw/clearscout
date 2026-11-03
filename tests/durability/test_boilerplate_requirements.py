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


def test_empty_text_is_not_boilerplate() -> None:
    assert is_boilerplate_requirement("") is False
