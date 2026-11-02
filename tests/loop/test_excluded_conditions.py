"""Matching a posting's stated requirements against hard-exclusion conditions.

A credential the person lacks refuses the posting; an alternative the person might
hold abstains rather than refusing, because a false drop is worse than a hand check.
Synthetic credentials stand in for the workspace's real ones, which live only in the
saved parameters.
"""

from __future__ import annotations

import pytest

from tailorcv.application.master_cv.cv import parse_cv
from tailorcv.application.tailoring.fit import Standing, gates, read
from tailorcv.domain.search_parameters import JobSearchParameters
from tailorcv.domain.search_parameters.conditions import excluded_by
from tests.support.suite import FIXTURES

TRADE = ("Requires an active locksmith or master electrician license", "not held")
FLIGHT = ("Requires an ATP, commercial, or airline transport certificate", "not held")
CONDITIONS = (TRADE, FLIGHT)


@pytest.mark.parametrize(
    ("statement", "refused"),
    [
        # Unambiguous, single credential requirements the person cannot satisfy: refuse.
        ("Active locksmith licensure is required.", "locksmith"),
        ("A master electrician license is required.", "locksmith"),
        ("Must hold a current airline transport certificate.", "airline"),
        # A prose disjunction may offer a credential the person holds, so it abstains
        # even when both branches are excluded; that case is left to the model and the
        # human.
        ("Locksmith or master electrician license required.", None),
        ("ATP or private pilot certificate required.", None),
        ("Locksmith license or equivalent experience.", None),
        # A positive mention, not a stated credential requirement: abstain.
        ("Locksmith apprenticeship strongly preferred.", None),
        ("Experience supporting facilities maintenance workflows.", None),
        # A credential the person's conditions never named: abstain.
        ("Registered welder certification required.", None),
        # No credential at all: abstain.
        ("8+ years of product management experience.", None),
    ],
)
def test_only_an_unsatisfiable_credential_requirement_refuses(
    statement: str, refused: str | None
) -> None:
    hit = excluded_by(CONDITIONS, [statement])
    if refused is None:
        assert hit is None
    else:
        assert hit is not None and refused in hit[0].casefold()


PRACTICE = ("Requires an active pharmacist or clinical practice license", "not held")
PHYSICIAN = ("Requires an MD, DO, or physician licence", "not held")
ENGINEER = ("Requires a Professional Engineer (PE) licence", "not held")
NURSE = ("Requires an active registered nurse licence", "not held")
US_LICENCE = ("Requires a US medical licence", "not held")
EU_REGISTRATION = ("Requires an EU pharmacist registration", "not held")
PA_CERTIFICATION = ("Requires a PA certification", "not held")
PA_LICENCE = ("Requires a Physician Assistant (PA) licence", "not held")
_JOINS = ("and", "of", "to", "in", "on", "at", "by")


@pytest.mark.parametrize(
    ("condition", "statement"),
    [
        (PRACTICE, "Current US healthcare professional license: PharmD"),
        (PRACTICE, "Licensed pharmacist required"),
        (PRACTICE, "Active pharmacist licensure in the state of hire"),
        (
            ("Requires an active RN, pharmacist or clinical practice license", "not held"),
            "Active RN licensure in state hired",
        ),
        (ENGINEER, "Licensed PE required"),
        (US_LICENCE, "US medical licence required"),
        (PA_CERTIFICATION, "PA certification required."),
        (PA_CERTIFICATION, "You are a PA with 3 years of surgical experience."),
    ],
)
def test_a_single_licence_requirement_naming_the_condition_refuses(
    condition: tuple[str, str], statement: str
) -> None:
    assert excluded_by([condition], [statement]) == condition


@pytest.mark.parametrize(
    ("condition", "statement"),
    [
        # A degree, not a licence or a profession, and degree alternatives.
        (PRACTICE, "PharmD is required"),
        (PRACTICE, "PhD, MD or PharmD required"),
        (PRACTICE, "A PharmD with 5 years of experience"),
        (PHYSICIAN, "MD, or MSc in a life science"),
        # Others the role works beside, not what the candidate is.
        *(
            (condition, statement)
            for condition in (PRACTICE, PHYSICIAN)
            for statement in (
                "Experience working with physicians and pharmacists",
                "Partner with licensed clinicians",
                "Partner with licensed pharmacists and physicians",
                "Coordinate with licensed providers across sites",
            )
        ),
        # A profession noun that modifies something else.
        (PHYSICIAN, "A physician-led organization"),
        (PHYSICIAN, "A physician executive with board experience"),
        (PRACTICE, "A clinical analytics leader with EHR experience"),
        (TRADE, "A locksmith-trained facilities lead"),
        # A qualification whose phrase ends on a word the condition names.
        (PRACTICE, "A commitment to best practice."),
        (PRACTICE, "A passion for clinical practice"),
        (PRACTICE, "A background in clinical practice"),
        (PRACTICE, "An interest in clinical practice"),
        # A condition word joined to what follows, so the phrase's head lies further on.
        *(
            (condition, f"A {word} {join} {tail}")
            for condition, word in (
                (PRACTICE, "clinical"),
                (PRACTICE, "practice"),
                (PHYSICIAN, "physician"),
            )
            for join in _JOINS
            for tail in ("analytics background", "operational leadership background", "bridge")
        ),
        (PRACTICE, "A strong clinical and analytics background"),
        (PRACTICE, "A clinical to technical translator for the pharmacy team"),
        (PRACTICE, "A practice of documenting decisions"),
        *(
            (condition, f"A {noun} {join} our team will mentor you.")
            for condition, noun in ((PHYSICIAN, "physician"), (PRACTICE, "pharmacist"))
            for join in _JOINS
        ),
        (PHYSICIAN, "A physician in our network reviews each case."),
        (PHYSICIAN, "A liaison to the physician"),
        (PHYSICIAN, "A physician."),
        (PRACTICE, "A former pharmacist moving into analytics"),
        # A condition word that is not the one naming who holds the credential.
        *(
            (condition, f"{opening} {noun}{closing}")
            for condition in (ENGINEER, NURSE, PRACTICE)
            for opening in ("A seasoned", "An experienced", "You are a")
            for noun in ("engineer", "professional", "clinical leader")
            for closing in (
                " with deep Python experience",
                " who has shipped production systems",
                " with 10+ years in healthcare IT",
            )
        ),
        # A licence class the condition never names.
        (PRACTICE, "Active RN licensure in state hired"),
    ],
)
def test_a_degree_a_colleague_or_an_unnamed_licence_does_not_refuse(
    condition: tuple[str, str], statement: str
) -> None:
    assert excluded_by([condition], [statement]) is None


TECHNICIAN = ("Requires PTCB or CPhT certification", "not held")


@pytest.mark.parametrize(
    "statement",
    [
        "Active Pharmacy Technician license in the state of practice",
        "Current pharmacy technician registration required",
        "Licensed/registered Pharmacy Technician",
        "CPhT or state pharmacy technician license",
        "Must hold a valid technician license with the Board of Pharmacy",
    ],
)
@pytest.mark.parametrize("order", [(PRACTICE, TECHNICIAN), (TECHNICIAN, PRACTICE)])
def test_a_pharmacy_technician_licence_refuses_under_the_technician_condition(
    statement: str, order: tuple[tuple[str, str], ...]
) -> None:
    assert excluded_by(order, [statement]) == TECHNICIAN


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("Licensed pharmacist required", PRACTICE),
        ("Active pharmacist licensure in the state of hire", PRACTICE),
        ("Active State Board of Pharmacy License", PRACTICE),
        ("Partner with pharmacy technicians; licensed pharmacist required", PRACTICE),
        ("Valid driver's license required", None),
    ],
)
@pytest.mark.parametrize("order", [(PRACTICE, TECHNICIAN), (TECHNICIAN, PRACTICE)])
def test_a_pharmacist_or_generic_licence_never_reads_as_the_technician_condition(
    statement: str, expected: tuple[str, str] | None, order: tuple[tuple[str, str], ...]
) -> None:
    assert excluded_by(order, [statement]) == expected


@pytest.mark.parametrize(
    "statement",
    ["Active RPh license", "Pharmacy technicians report to you"],
)
def test_a_pharmacist_licence_or_technician_colleagues_never_refuse_the_technician_condition(
    statement: str,
) -> None:
    assert excluded_by([TECHNICIAN], [statement]) is None


_DEGREE_FORMS = (
    "{a}/{b}",
    "{a}/{b} with at least 8 years of clinical experience.",
    "{a} or {b} degree required",
    "Medical degree ({a}/{b})",
    "{a} degree required",
    "{a} required",
    "Degree ({a}) required",
    "You are a {a}/{b} who has completed residency",
    "You are a {a} with board certification",
)
_CREDENTIAL_FORMS = (
    "MD/DO",
    "M.D.",
    "MD or DO",
    "DO or MD",
    "Physician (MD)",
    "MD degree",
    "Medical degree (MD)",
)


@pytest.mark.parametrize(
    "statement",
    [
        "You are a MD/DO Physician who has completed residency",
        "Doctor of Medicine (MD) required",
        "An MD with 5 years of experience",
        "MD OR DO REQUIRED",
        "MD/DO REQUIRED",
        "DO OR MD REQUIRED",
        "MD or DO degree required; board certification in internal or family medicine preferred.",
        "You are a MD/DO Physician who has completed residency in a teaching hospital.",
        "Physician (MD or DO) who completed residency in internal medicine.",
        "MD required.",
        "MD or DO degree from an accredited medical school",
        *(form.format(a=a, b=b) for form in _DEGREE_FORMS for a, b in (("MD", "DO"), ("DO", "MD"))),
        *(
            template.format(credential)
            for credential in _CREDENTIAL_FORMS
            for template in ("{} required.", "{} with at least 5 years of clinical experience.")
        ),
    ],
)
def test_a_degree_the_condition_names_refuses(statement: str) -> None:
    assert excluded_by([PHYSICIAN], [statement]) == PHYSICIAN


_NAMED_PAIRS = (("MD", "DO"), ("DO", "MD"), ("M.D.", "D.O."))
# A joined run of names, then a comma and prose that qualifies the credential.
_RUN_THEN_PROSE_FORMS = (
    "{a} or {b}, with at least 5 years of patient-care experience.",
    "{a}/{b}, board certified in a relevant specialty",
    "Terminal medical degree ({a} or {b}), specializing in one of: Pediatrics, Radiology.",
    "Physician [{a}/{b}], with 3+ years of practice",
    "{a}, {b}, with an active state licence; 2 years in a hospital setting",
)
# A joined run of names, then an "and" conjunct whose own alternatives join no names.
_RUN_THEN_CONJUNCT_FORMS = (
    "{a} or {b} degree from an accredited medical school AND 5 years of clinical research"
    " and/or basic science research experience.",
    "{a}/{b} and 3 years of research or teaching experience",
    "{a} or {b} required, and experience in trials and/or registries.",
)
# A joined run of names, then a comma and a prepositional phrase whose own list
# joins no names.
_RUN_THEN_QUALIFIER_FORMS = (
    "Medical degree ({a} or {b}), specializing in one of: Cardiology, Pediatrics, ENT/Allergy,"
    " and Surgery",
    "Medical degree {a} or {b}, specializing in one of: Cardiology, Pediatrics, ENT/Allergy,"
    " and Surgery",
    "Medical degree ({a} or {b}), specializing in Cardiology or Surgery",
    "{a}/{b}, board certified in Cardiology, Pediatrics, and/or Surgery",
    "{a} or {b}, with training in ENT/Allergy and 3 years of practice",
)


@pytest.mark.parametrize(
    "statement",
    [
        form.format(a=a, b=b)
        for form in (
            *_RUN_THEN_PROSE_FORMS,
            *_RUN_THEN_CONJUNCT_FORMS,
            *_RUN_THEN_QUALIFIER_FORMS,
        )
        for a, b in _NAMED_PAIRS
    ],
)
def test_a_joined_name_before_prose_refuses(statement: str) -> None:
    assert excluded_by([PHYSICIAN], [statement]) == PHYSICIAN


# A physician specialty noun glossed by the condition's name, as the candidate.
_GLOSSED_SPECIALIST_FORMS = (
    "{} (MD) with training in the relevant therapeutic area.",
    "{} (MD) with 5 years of clinical practice",
    "The {} (DO) who has completed residency",
    "You are the {} (M.D.) with industry experience",
)
_SPECIALISTS = (
    "Oncologist",
    "Cardiologist",
    "radiologist",
    "Psychiatrist",
    "Neurologist",
    "Pathologist",
    "Dermatologist",
    "Pediatrician",
    "Internist",
    "Surgeon",
    "Anesthesiologist",
    "Hospitalist",
)
# Specialty abbreviations in the qualifier after the names, where no alternative follows.
_SPECIALTY_QUALIFIER_FORMS = (
    "Medical degree ({a} or {b}), specializing in Pediatrics, OB/GYN, and Surgery",
    "{a} or {b}, specializing in one of the following: Pediatrics, OB/GYN, ENT, GI, and General"
    " Surgery",
    "Medical degree ({a} or {b}), with training in OB/GYN or ENT",
    "{a}/{b}, board certified in OB/GYN",
    "{a} or {b}, specializing in ENT or GI",
)


@pytest.mark.parametrize(
    "statement",
    [
        *(form.format(noun) for form in _GLOSSED_SPECIALIST_FORMS for noun in _SPECIALISTS),
        *(form.format(a=a, b=b) for form in _SPECIALTY_QUALIFIER_FORMS for a, b in _NAMED_PAIRS),
    ],
)
def test_a_glossed_specialist_or_a_specialty_qualifier_refuses(statement: str) -> None:
    assert excluded_by([PHYSICIAN], [statement]) == PHYSICIAN


@pytest.mark.parametrize(
    "statement",
    [
        # Colleagues holding the degree, not the candidate.
        *(
            template.format(credential)
            for credential in (*_CREDENTIAL_FORMS, "MD and PhD")
            for template in (
                "Partner with {} scientists across the portfolio.",
                "Experience partnering with {} clinicians required.",
                "Report findings to {} reviewers.",
            )
        ),
        "Experience partnering with MD and PhD scientists required.",
        # A stated preference, not a requirement.
        *(f"{credential} preferred." for credential in _CREDENTIAL_FORMS),
        "MD preferred.",
        "MD strongly preferred.",
        # Upper case hides whether a lone "DO" or "MD" is the credential or a word,
        # so only names joined to each other refuse there.
        "DO NOT APPLY IF YOU LACK A BS DEGREE",
        "DO NOT APPLY WITHOUT A BS DEGREE",
        "WHAT WE DO: BUILD CLINICAL TOOLS",
        "M.D. REQUIRED",
        "MD DEGREE REQUIRED",
        "PARTNER WITH MD AND PHD SCIENTISTS",
    ],
)
def test_a_colleague_a_preference_or_an_upper_case_word_does_not_refuse(statement: str) -> None:
    assert excluded_by([PHYSICIAN], [statement]) is None


@pytest.mark.parametrize(
    ("condition", "statement"),
    [
        # An alternative the condition never names might be held.
        (PHYSICIAN, "PhD, MD or PharmD in a life science"),
        (PHYSICIAN, "MD, or MSc in epidemiology"),
        (PHYSICIAN, "PhD or MD preferred"),
        (PHYSICIAN, "MD/PhD required"),
        (PHYSICIAN, "MD (or equivalent) required"),
        (PHYSICIAN, "Advanced degree (PharmD, PhD, MD) preferred"),
        # Colleagues holding the degree, not the candidate.
        (PHYSICIAN, "Experience partnering with MD/DO clinicians"),
        (PHYSICIAN, "Collaborate with MD physicians across sites"),
        # A degree the condition never names.
        (PHYSICIAN, "PharmD is required"),
        (ENGINEER, "PE or EIT certification required"),
        # An alternative offered in a following clause.
        (PHYSICIAN, "MD or DO; or PharmD"),
        # A run of names that continues into, or is followed by, an unnamed alternative.
        *(
            (PHYSICIAN, statement)
            for statement in (
                "PhD, PharmD, MD, or equivalent advanced scientific degree.",
                "Advanced degree (PhD, MD or PharmD) in life sciences",
                "Licensed clinician (MD, DO, NP, RD, or PharmD) with 8+ years",
                "Clinical credential or equivalent depth: MD, PharmD, NP, PA, RN, MPH, or 5+"
                " years in health IT",
                "MD, DO, or equivalent medical degree (minimum requirement)",
                "M.D., D.O., or equivalent medical degree.",
                "MD or DO, PharmD, or PA with 5 years of experience",
                "MD/DO, PhD for research roles",
                "MD or DO degree required, or equivalent experience.",
                "MD or DO required or equivalent combination of education and experience",
                "MD and/or equivalent clinical experience",
                "MD or DO [or equivalent], with 5 years of patient care",
                "Experience at a US academic center (MD or PhD faculty)",
                "Cardiologist (MD) or nurse practitioner (NP) with 5 years",
                "MD or DO, PhD, or PharmD",
                "MD or DO, or equivalent experience",
                "MD or DO, or PhD in a related field",
                "MD, PhD/PharmD preferred",
                "MD/DO, pharmacist or nurse practitioner with 5 years",
                "MD or DO, specializing in cardiology, or equivalent experience",
                "Medical degree (MD or DO), board certified in Cardiology, or PhD in a related"
                " field",
                "MD or DO and 3 years of experience, or PharmD",
                "MD/DO and 5+ years of industry experience or PhD with 10 years of drug"
                " development",
                "MD or DO and board certification or PhD in a relevant field",
                "MD/DO and residency training or PharmD and fellowship",
                "MD or DO and board certification, or equivalent experience",
                "MD or DO, specializing in ENT or GI, or PhD",
                "MD or DO, specializing in Pediatrics or ENT, or PharmD",
                "MD or DO, with training in ENT or PharmD",
                *(
                    f"MD or DO, specializing in Oncology or {degree}"
                    for degree in ("MPH", "MBA", "MSN", "RN", "PA")
                ),
                "MD or DO, board certified in Pediatrics or NP certification",
                "MD or DO, Doctor of Pharmacy or Doctor of Nursing Practice",
                "MD/DO, physician assistant with prescribing authority or nurse practitioner",
                "MD or DO, advanced degree in life sciences or equivalent experience",
            )
        ),
        # A name glossing a market, a title, or a place, not a clinician.
        (ENGINEER, "Private equity (PE) experience required"),
        (ENGINEER, "Prior Private Equity (PE) deal experience"),
        (PHYSICIAN, "Prior Managing Director (MD) experience in consulting"),
        (PHYSICIAN, "Hybrid schedule, Bethesda (MD) office three days a week"),
        (PHYSICIAN, "Medical Director (MD) oversight of all protocols"),
        (PHYSICIAN, "Reporting line: Medical Director (MD)"),
        (PA_LICENCE, "Pittsburgh (PA) hybrid role"),
        # A run of names in a list of colleagues the role works with.
        (PHYSICIAN, "Collaborate with physicians (MD or DO), pharmacists and nurses"),
        (PHYSICIAN, "Experience partnering with physicians (MD/DO), nurses, and care teams"),
        (PHYSICIAN, "Work closely with MD/DO, nursing, and pharmacy leadership"),
        (PHYSICIAN, "Experience working alongside clinicians (MD, DO), nurses and pharmacists"),
        # A name in brackets glossing a place or a colleague, not the candidate.
        (PHYSICIAN, "Must be located in Baltimore (MD)"),
        (PHYSICIAN, "Offices in Bethesda (MD) and Austin (TX)"),
        (PHYSICIAN, "Partner with a cardiologist (MD) on each study"),
        (PHYSICIAN, "Partner with Cardiologist (MD) teams"),
        (PHYSICIAN, "CARDIOLOGIST (MD) REQUIRED"),
        (PHYSICIAN, "Cardiologist (MD) preferred"),
        (PHYSICIAN, "Partner with MD and PhD scientists"),
        (PHYSICIAN, "Report To Cardiologist (MD)"),
        (PHYSICIAN, "Partner with Oncologist (MD) teams"),
        (PHYSICIAN, "reports to the Chief Medical Officer (MD)"),
        # A name glossing a noun outside the physician specialties.
        *(
            (PHYSICIAN, f"{noun} (MD) with 5 years of clinical practice")
            for noun in (
                "Technician",
                "Scientist",
                "Statistician",
                "Pharmacist",
                "Dentist",
                "Therapist",
            )
        ),
        # The English verb spelled like a condition abbreviation.
        (PHYSICIAN, "Do you hold an active state license"),
        # A waiver or a wish, not a requirement.
        *(
            (PHYSICIAN, statement)
            for statement in (
                "An MD is not required; clinical analytics experience is",
                "MD not required.",
                "No MD required",
                "MD/DO not required",
                "You do not need an MD to apply",
                "MD desired",
                "MD strongly desired",
                "MD helpful but not necessary",
                "MD desirable",
                "An MD is an asset",
                "MD an advantage",
            )
        ),
        # Colleagues or an organization, not what the candidate holds.
        (PHYSICIAN, "Experience partnering with MD/DO teams"),
        (PHYSICIAN, "Support MD and DO faculty"),
        (PHYSICIAN, "Experience in an MD-led organization"),
        (PHYSICIAN, "Experience with EMR systems used by MD practices"),
        # A place or a market written like a credential abbreviation.
        (PHYSICIAN, "Must be located in the Baltimore MD area"),
        (US_LICENCE, "Must be authorized to work in the US"),
        (ENGINEER, "Experience with PE-backed companies"),
        (EU_REGISTRATION, "Must have EU work authorization"),
        (PA_CERTIFICATION, "Must reside in PA"),
    ],
)
def test_a_degree_alternative_or_colleague_does_not_refuse(
    condition: tuple[str, str], statement: str
) -> None:
    assert excluded_by([condition], [statement]) is None


_REGISTERED_NURSE = ("Requires an active RN licence", "not held")


@pytest.mark.parametrize(
    ("condition", "item", "standing"),
    [
        # A bare item naming a licence the candidate must hold.
        (PRACTICE, "Active State Board of Pharmacy License", Standing.INELIGIBLE),
        (PRACTICE, "Current pharmacist license in good standing", Standing.INELIGIBLE),
        (_REGISTERED_NURSE, "Current RN license in good standing", Standing.INELIGIBLE),
        (_REGISTERED_NURSE, "Active RN licensure in the state of hire", Standing.INELIGIBLE),
        (ENGINEER, "Valid PE licensure", Standing.INELIGIBLE),
        (ENGINEER, "Unrestricted PE license.", Standing.INELIGIBLE),
        # Colleagues, a wish, a waiver, a duty, or a licence word that is not the head.
        (PRACTICE, "Partner with licensed pharmacists", Standing.ELIGIBLE),
        (PRACTICE, "Pharmacy license a plus", Standing.ELIGIBLE),
        (PRACTICE, "Active pharmacy license preferred", Standing.ELIGIBLE),
        (PRACTICE, "Pharmacy licensure not required", Standing.ELIGIBLE),
        (PRACTICE, "Active pharmacy license not required", Standing.ELIGIBLE),
        (PRACTICE, "Maintain state licensing filings for the company", Standing.ELIGIBLE),
        (PRACTICE, "Current awareness of pharmacy licensure trends", Standing.ELIGIBLE),
        (PRACTICE, "Current pharmacy license renewal fees reimbursed", Standing.ELIGIBLE),
        # A later clause waives, conditions, or defers the licence.
        (_REGISTERED_NURSE, "Active RN license; not required for this role", Standing.ELIGIBLE),
        (_REGISTERED_NURSE, "Active RN license; not required", Standing.ELIGIBLE),
        (_REGISTERED_NURSE, "Active RN license. Not required.", Standing.ELIGIBLE),
        (_REGISTERED_NURSE, "Active RN license: optional", Standing.ELIGIBLE),
        (_REGISTERED_NURSE, "Active RN license; optional", Standing.ELIGIBLE),
        (_REGISTERED_NURSE, "Current RN license, if applicable", Standing.ELIGIBLE),
        (_REGISTERED_NURSE, "Current RN license, where applicable", Standing.ELIGIBLE),
        (_REGISTERED_NURSE, "Current RN license (for clinical track only)", Standing.ELIGIBLE),
        (
            _REGISTERED_NURSE,
            "Current RN license, for clinical-track candidates only",
            Standing.ELIGIBLE,
        ),
        (
            _REGISTERED_NURSE,
            "RN license to be obtained within 6 months of hire",
            Standing.ELIGIBLE,
        ),
        (
            _REGISTERED_NURSE,
            "Active RN license to be obtained within 6 months of hire",
            Standing.ELIGIBLE,
        ),
        (_REGISTERED_NURSE, "Current RN license in the clinical track only", Standing.ELIGIBLE),
        (_REGISTERED_NURSE, "Active RN license in any state if applicable", Standing.ELIGIBLE),
        (
            _REGISTERED_NURSE,
            "Valid RN license in a compact state for clinical-track hires only",
            Standing.ELIGIBLE,
        ),
        (
            _REGISTERED_NURSE,
            "Current RN license with the option to waive for analytics hires",
            Standing.ELIGIBLE,
        ),
        (_REGISTERED_NURSE, "Active RN license in lieu of a degree", Standing.ELIGIBLE),
        (
            _REGISTERED_NURSE,
            "Active RN license to practice in Ohio for the clinical track",
            Standing.ELIGIBLE,
        ),
    ],
)
def test_a_bare_licence_item_in_an_experience_list_refuses(
    condition: tuple[str, str], item: str, standing: Standing
) -> None:
    posting = (
        "Clinical Data Lead\n\nExperience :\n"
        "- Minimum of 10 years of experience in clinical analytics\n"
        "- SQL and Python skills required\n"
        f"- {item}\n"
    )
    parameters = JobSearchParameters(excluded_conditions=[condition])
    reading = read(posting, parse_cv(FIXTURES / "master_cv.md"), parameters=parameters)
    found = next(gate for gate in gates(reading, parameters) if gate.name == "credential")
    assert found.standing is standing


RN = ("Requires an active RN or registered nurse licence", "not held")


@pytest.mark.parametrize(
    "statement",
    [
        "Holds an active Registered Respiratory Therapist license with 3-5 years of ICU work.",
        "Must maintain a current, unrestricted Registered Pharmacist license in any state.",
        "Experience managing licensed and registered pharmacy technicians required.",
        "Registered Dietitian license required.",
        "Registered Psychotherapist license required.",
    ],
)
def test_another_registered_profession_does_not_read_as_the_nurse_condition(
    statement: str,
) -> None:
    assert excluded_by([RN], [statement]) is None


@pytest.mark.parametrize(
    "statement",
    [
        "Hold an active registered nursing license with NICU experience.",
        "Registered Nurse with active state license.",
        "Current New York State license as a Registered Nurse.",
        "Multi-state licensure as a Registered Nurse.",
        "Registration and licensure as an RN in the province.",
    ],
)
def test_a_nursing_licence_in_any_word_form_refuses_the_nurse_condition(statement: str) -> None:
    assert excluded_by([RN], [statement]) == RN


def test_no_conditions_configured_never_refuses() -> None:
    assert excluded_by((), ["Active locksmith licensure is required."]) is None


def test_the_first_matching_condition_is_returned_with_its_reason() -> None:
    hit = excluded_by(CONDITIONS, ["Active locksmith licensure is required."])
    assert hit == TRADE
