"""Publisher-stated arrangement extraction for Workday CXS records."""

from __future__ import annotations

import pytest

from tailorcv.infrastructure.web_fetch.workday_arrangement import workday_arrangement_facts


def test_workday_arrangement_from_detail_remote_type_yes() -> None:
    assert workday_arrangement_facts({}, {"remoteType": "Yes"}, ()).value == "remote"


def test_workday_arrangement_from_detail_remote_type_descriptor_dict() -> None:
    detail = {"remoteType": {"descriptor": "Hybrid"}}

    assert workday_arrangement_facts({}, detail, ()).value == "hybrid"


def test_workday_bare_hybrid_word_in_the_body_states_no_arrangement() -> None:
    detail = {"jobDescription": "<p>We build hybrid cloud data products.</p>"}

    assert workday_arrangement_facts({}, detail, ()).value is None


def test_workday_body_hybrid_with_an_attendance_clause_is_hybrid() -> None:
    detail = {
        "jobDescription": "<p>This role offers a hybrid schedule, 3 days per week on campus.</p>"
    }

    assert workday_arrangement_facts({}, detail, ()).value == "hybrid"


@pytest.mark.parametrize(
    ("body", "token"),
    [
        ("<p>We build hybrid cloud products.</p><p>This role is fully onsite.</p>", "onsite"),
        (
            "<p>Fully onsite events each quarter.</p><p>This role is a hybrid position.</p>",
            "hybrid",
        ),
    ],
)
def test_workday_body_value_is_the_attendance_clause_kind(body: str, token: str) -> None:
    assert workday_arrangement_facts({}, {"jobDescription": body}, ()).value == token


@pytest.mark.parametrize("label", ["Office or Home", "Home or Office", "office or home"])
def test_workday_office_or_home_label_is_remote_capable(label: str) -> None:
    detail = {"remoteType": {"descriptor": label}}

    assert workday_arrangement_facts({}, detail, ()).value == "remote"


def test_workday_arrangement_as_needed_office_use_overrides_hybrid_label() -> None:
    detail = {
        "remoteType": {"descriptor": "Hybrid"},
        "jobDescription": (
            "Our Flexible Hybrid Work model allows employees to work virtually while "
            "utilizing our offices as collaboration centers. With leadership agreement, "
            "you can come and go from the office as needed."
        ),
    }

    assert workday_arrangement_facts({}, detail, ()).value == "remote"


def test_workday_arrangement_keeps_hybrid_when_office_phrase_is_paragraphs_away() -> None:
    detail = {
        "remoteType": {"descriptor": "Hybrid"},
        "jobDescription": (
            "<p>Some teams work virtually.</p>"
            "<p>You will own the data platform roadmap, partner with product managers "
            "across three business units, mentor engineers, run the weekly architecture "
            "review, and report progress to the steering committee every quarter.</p>"
            "<p>Facilities are open to all staff, and badge holders may come and go "
            "from the office as needed.</p>"
        ),
    }

    assert workday_arrangement_facts({}, detail, ()).value == "hybrid"


@pytest.mark.parametrize(
    ("tag", "arrangement"),
    [
        ("#LI-remote", "remote"),
        ("#LIRemote", "remote"),
        ("#LI-Hybrid", "hybrid"),
        ("#LIHybrid", "hybrid"),
        ("#LI-Onsite", "onsite"),
        ("#LIOnsite", "onsite"),
    ],
)
def test_workday_arrangement_from_linkedin_body_tag(tag: str, arrangement: str) -> None:
    detail = {"jobDescription": f"<p>Join the team. {tag}</p>"}

    assert workday_arrangement_facts({}, detail, ()).value == arrangement


_REMOTE_AMONG_PLACES = ("USA - Austin, TX", "USA-IL-Remote")


@pytest.mark.parametrize("tag", ["#LI-Hybrid", "#LI-Onsite"])
def test_workday_attendance_tag_beside_a_remote_place_is_unresolved(tag: str) -> None:
    detail = {"jobDescription": f"<p>Own the Initech ledger roadmap.</p><p>{tag}</p>"}

    signal = workday_arrangement_facts({}, detail, _REMOTE_AMONG_PLACES)

    assert signal.value is None
    assert signal.unresolved


@pytest.mark.parametrize(
    ("row", "detail"),
    [
        ({}, {"remoteType": {"descriptor": "Hybrid"}}),
        ({}, {"remoteType": "No"}),
        ({}, {"workLocations": [{"remoteType": {"descriptor": "On-site"}}]}),
        ({"bulletFields": ["Full Time", "Hybrid"]}, None),
        ({"remoteType": "Hybrid"}, None),
        ({}, {"remoteType": "Hybrid (8 days/month)"}),
        ({}, {"remoteType": "Flex Commuter / Hybrid"}),
        ({}, {"remoteType": "On-Site Required"}),
    ],
)
def test_workday_structured_attendance_beside_a_remote_place_is_unresolved(
    row: dict[str, object], detail: dict[str, object] | None
) -> None:
    signal = workday_arrangement_facts(row, detail, _REMOTE_AMONG_PLACES)

    assert signal.value is None
    assert signal.unresolved


_REMOTE_OFFER = "<p>You may work remotely from home.</p>"


@pytest.mark.parametrize(
    "detail",
    [
        {"remoteType": "No", "jobDescription": _REMOTE_OFFER},
        {"remoteType": "Hybrid (8 days/month)", "jobDescription": _REMOTE_OFFER},
        {"jobDescription": f"{_REMOTE_OFFER}<p>#LI-Hybrid</p>"},
    ],
)
def test_workday_attendance_beside_a_body_remote_offer_is_unresolved(
    detail: dict[str, object],
) -> None:
    signal = workday_arrangement_facts({}, detail, ())

    assert signal.value is None
    assert signal.unresolved


@pytest.mark.parametrize(
    ("detail", "arrangement"),
    [
        ({"remoteType": "Yes"}, "remote"),
        ({"remoteType": "Remote (Pre-Approved)"}, "Remote (Pre-Approved)"),
        ({"jobDescription": "<p>Join the team. #LI-Remote</p>"}, "remote"),
    ],
)
def test_workday_remote_answer_beside_a_remote_place_stays_remote(
    detail: dict[str, object], arrangement: str
) -> None:
    assert workday_arrangement_facts({}, detail, _REMOTE_AMONG_PLACES).value == arrangement


def test_workday_structured_attendance_holds_when_no_place_is_remote() -> None:
    signal = workday_arrangement_facts(
        {}, {"remoteType": "Hybrid"}, ("USA - Austin, TX", "USA - Chicago, IL")
    )

    assert signal.value == "hybrid"


@pytest.mark.parametrize(
    "tag", ["#LI-Remote-first", "#LI-Remotely", "#LI-Hybridized", "#LI-Onsite-only"]
)
def test_workday_arrangement_ignores_linkedin_tag_lookalikes(tag: str) -> None:
    detail = {"jobDescription": f"<p>Join the team. {tag}</p>"}

    assert workday_arrangement_facts({}, detail, ()).value is None


@pytest.mark.parametrize(
    "body",
    [
        "#LI-Onsite\n#LI-Remote",
        "#LI-Onsite\nThis role is fully remote within Canada.",
    ],
)
def test_workday_arrangement_keeps_conflicting_linkedin_source_unresolved(body: str) -> None:
    signal = workday_arrangement_facts({}, {"jobDescription": body}, ())

    assert signal.value is None
    assert signal.unresolved
    assert "#LI-Onsite" in signal.unresolved[0]


def test_workday_arrangement_remote_first_phrase_wins_over_absent_structured() -> None:
    detail = {"jobDescription": "We are a remote-first team building great things."}
    assert workday_arrangement_facts({}, detail, ()).value == "remote"


def test_workday_arrangement_contradictory_phrases_return_none() -> None:
    detail = {"jobDescription": "We are a remote-first culture, but this role is fully onsite."}
    assert workday_arrangement_facts({}, detail, ()).value is None


def test_workday_arrangement_none_when_no_signal_in_row_or_detail_or_body() -> None:
    row = {"title": "Engineer", "locationsText": "New York, NY"}
    detail = {"title": "Engineer", "jobDescription": "Own the codebase."}
    assert workday_arrangement_facts(row, detail, ("New York, NY",)).value is None


def test_workday_arrangement_structured_wins_over_body() -> None:
    detail = {"remoteType": "No", "jobDescription": "Come collaborate. #LI-Hybrid"}
    assert workday_arrangement_facts({}, detail, ()).value == "onsite"


def test_workday_arrangement_row_only_when_detail_absent() -> None:
    row = {"bulletFields": ["Remote"], "locationsText": "Anywhere"}
    assert workday_arrangement_facts(row, None, ("Anywhere",)).value == "remote"


def test_workday_arrangement_row_bullets_skip_non_arrangement_tokens() -> None:
    """Employment-type or seniority bullets do not consume the arrangement lookup."""
    row = {"bulletFields": ["Full Time", "Regular", "Remote"]}
    assert workday_arrangement_facts(row, None, ()).value == "remote"


@pytest.mark.parametrize(
    ("label", "arrangement"),
    [
        ("In Person", "onsite"),
        ("Office-based", "onsite"),
        ("Hybrid (8 days/month)", "Hybrid (8 days/month)"),
        ("Remote (Pre-Approved)", "Remote (Pre-Approved)"),
        ("Flex Commuter / Hybrid", "Flex Commuter / Hybrid"),
    ],
)
def test_workday_tenant_label_naming_a_known_arrangement_is_a_value(
    label: str, arrangement: str
) -> None:
    facts = workday_arrangement_facts({}, {"remoteType": label}, ())

    assert facts.value == arrangement
    assert facts.unresolved == ()


def test_workday_tenant_label_naming_no_known_arrangement_is_unresolved() -> None:
    facts = workday_arrangement_facts(
        {}, {"remoteType": {"descriptor": "Mobile"}}, _REMOTE_AMONG_PLACES
    )

    assert facts.value is None
    assert facts.unresolved == ("Mobile",)


def test_workday_role_remote_declaration_outweighs_home_office_policy_in_full_body() -> None:
    body = (
        "<h1>Data Engineer</h1><p>Initech is hiring a Data Engineer for its data team.</p>"
        "<h2>Responsibilities</h2><p>Develop and maintain cloud data pipelines. "
        "Collaborate with analysts and document integrations.</p>"
        "<h2>Qualifications</h2><p>Experience with SQL, Python, and data warehouses.</p>"
        "<h2>Work at Home Requirements</h2>"
        "<p>To ensure Home or Hybrid Home/Office employees' ability to work effectively, "
        "the self-provided internet service of Home or Hybrid Home/Office employees "
        "must meet the following criteria: reliable broadband is required. "
        "Work from a dedicated space lacking ongoing interruptions.</p>"
        "<p>Travel: While this is a remote position, occasional travel to Initech's "
        "offices for training or meetings may be required.</p>"
        "<h2>Scheduled Weekly Hours</h2><p>40</p>"
        "<h2>Benefits</h2><p>Initech offers health coverage and paid time off.</p>"
        "<h2>About us</h2><p>Initech develops data software.</p>"
        "<h2>Equal Opportunity Employer</h2><p>Applicants receive equal consideration.</p>"
    )

    signal = workday_arrangement_facts({}, {"jobDescription": body}, ("Austin, TX",))

    assert signal.value == "remote"
    assert signal.provenance_suffix == "body_phrase.arrangement"
    assert signal.unresolved == ()


@pytest.mark.parametrize(
    "declaration",
    [
        declaration
        for subject in ("role", "position", "job")
        for verb in ("is", "will be")
        for modifier in ("", "fully ")
        for declaration in (
            f"This {subject} {verb} {modifier}remote.",
            f"This {verb} a {modifier}remote {subject}.",
        )
    ],
)
@pytest.mark.parametrize("markup", [False, True])
def test_workday_role_remote_writing_variants_outweigh_generic_hybrid_policy(
    declaration: str, markup: bool
) -> None:
    body = (
        "Home or Hybrid Home/Office employees must have reliable internet. "
        f"{declaration} Occasional travel for training or meetings may be required."
    )
    if markup:
        body = f"<p>{body.upper().replace('REMOTE', '<strong>REMOTE</strong>')}</p>"

    signal = workday_arrangement_facts({}, {"jobDescription": body}, ())

    assert signal.value == "remote"
    assert signal.provenance_suffix == "body_phrase.arrangement"
    assert signal.unresolved == ()


@pytest.mark.parametrize(
    "body",
    [
        "This role develops remote patient monitoring products.",
        "This position supports remote access products.",
        "Apply through our virtual application process.",
        "This position includes a virtual interview.",
        "This role is not remote.",
        "This is not a remote position.",
        "If this is a remote position, reliable internet will be required.",
        "For remote roles, reliable internet is required.",
    ],
)
def test_workday_body_does_not_claim_remote_from_products_or_application_clauses(
    body: str,
) -> None:
    signal = workday_arrangement_facts({}, {"jobDescription": body}, ())

    assert signal.value is None
    assert signal.unresolved == ()


@pytest.mark.parametrize(
    "constraint",
    [
        "This role is hybrid.",
        "This position is fully onsite.",
        "You must work in the office three days per week.",
        "You must work in the office eight days per month.",
    ],
)
def test_workday_role_remote_declaration_with_actual_attendance_stays_unresolved(
    constraint: str,
) -> None:
    body = f"This is a remote position. {constraint}"

    signal = workday_arrangement_facts({}, {"jobDescription": body}, ())

    assert signal.value is None
    assert signal.unresolved
    assert constraint in signal.unresolved[0]


@pytest.mark.parametrize("label", ["Hybrid", "No", "Remote/Hybrid"])
def test_workday_role_remote_declaration_does_not_override_structured_conflicts(
    label: str,
) -> None:
    signal = workday_arrangement_facts(
        {}, {"remoteType": label, "jobDescription": "This is a remote position."}, ()
    )

    assert signal.value is None
    assert signal.unresolved


_HYBRID_SENTENCE = "This role requires working 3 days a week in our Fairview office."
_ONSITE_SENTENCE = "This role is fully onsite at the Fairview Headquarters."


@pytest.mark.parametrize(
    ("sentence", "token"), [(_HYBRID_SENTENCE, "hybrid"), (_ONSITE_SENTENCE, "onsite")]
)
def test_workday_body_attendance_sentence_reads_its_token(sentence: str, token: str) -> None:
    body = f"<p>We build hybrid cloud ledgers.</p><p>{sentence}</p>"

    signal = workday_arrangement_facts({}, {"jobDescription": body}, ())

    assert signal.value == token


def test_workday_body_attendance_beside_a_remote_place_is_unresolved() -> None:
    body = f"<p>We build ledgers.</p><p>{_HYBRID_SENTENCE}</p>"

    signal = workday_arrangement_facts({}, {"jobDescription": body}, ("Remote - US",))

    assert signal.value is None
    assert signal.unresolved == (_HYBRID_SENTENCE, "Remote - US")
