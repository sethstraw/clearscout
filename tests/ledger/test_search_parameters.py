"""Job-search parameters: resolution, versioning, and what they refuse to conclude.

An unanswered question stays unanswered. A person who has configured nothing
is filtered on nothing, and a posting that says nothing does not acquire an
opinion because somebody filled in a form.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from tailorcv.application.config import load_settings
from tailorcv.application.failures import ConflictError, NotFoundError, ValidationError
from tailorcv.application.master_cv.cv import parse_cv
from tailorcv.application.tailoring.fit import Standing, gates, read
from tailorcv.application.tailoring.fit.lexicon import LEVELS
from tailorcv.application.tailoring.workflows.evaluation_policy import (
    OUTSIDE_DECLARED_SCORE,
    compensation_score,
    eligibility_score,
)
from tailorcv.application.workspaces import (
    SearchParametersService,
    WorkspaceService,
    parameter_summary,
)
from tailorcv.application.workspaces.parameters import digest
from tailorcv.domain.search_parameters import (
    ARRANGEMENTS,
    EMPLOYMENT_TYPES,
    OUTSIDE_DECLARED,
    Acceptance,
    Arrangement,
    CompensationBand,
    EmployerArchetype,
    Employers,
    Employment,
    EmploymentType,
    JobSearchParameters,
    Location,
    Seniority,
    WorkEligibility,
    WorkingTimeZone,
    basis_for,
)
from tailorcv.domain.search_parameters import LEVELS as LADDER
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.shared.errors import ConfigError
from tests.support.artifacts import runtime_storage
from tests.support.scenarios import store_parameters_without
from tests.support.suite import FIXTURES

FIXTURE = FIXTURES / "master_cv.md"


def test_explicit_employer_membership_survives_parameter_round_trip() -> None:
    parameters = JobSearchParameters(
        employers=Employers(
            archetypes=[
                EmployerArchetype(
                    name=" Clinical data ",
                    because="Medication information",
                    employer_names=[" Acme ", "Globex"],
                )
            ]
        )
    )
    restored = JobSearchParameters.model_validate_json(parameters.model_dump_json())
    assert restored.employers.stated_archetypes[0].employer_names == ["Acme", "Globex"]
    assert EmployerArchetype(name="Clinical data").employer_names == []


def test_employer_membership_requires_a_named_domain() -> None:
    with pytest.raises(PydanticValidationError, match="Name the employer type"):
        EmployerArchetype(name=" ", employer_names=["Acme"])


def test_employer_membership_persists_in_versioned_parameters(durable_database: Database) -> None:
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="membership", name="Membership"
    )
    service = SearchParametersService(durable_database)
    parameters = JobSearchParameters(
        employers=Employers(
            archetypes=[EmployerArchetype(name="Clinical data", employer_names=["Acme"])]
        )
    )
    saved = service.save(workspace.id, parameters)
    assert saved.version == 1
    assert service.current(workspace.id).employers.stated_archetypes[0].employer_names == ["Acme"]


def _inventory():
    return parse_cv(FIXTURE)


def _posting(*lines: str, title: str = "Data Engineer") -> str:
    """Return a posting with the blank line and bullet shape the block parser reads."""
    return "\n".join((f"# {title}", "", "## Requirements", "", *(f"- {line}" for line in lines)))


def test_the_ladder_and_the_arrangements_have_exactly_one_owner() -> None:
    """A preference and a reading must be ordering and naming the same lists."""
    assert tuple(name for name, _, _ in LEVELS) == LADDER
    assert ARRANGEMENTS == ("remote", "hybrid", "on site")


def test_empty_parameters_are_the_zero_value_and_change_nothing() -> None:
    parameters = JobSearchParameters()
    assert not parameters.configured
    assert parameters.actionable_locations == ()

    reading = read(_posting("Fully remote.", "Senior engineers only."), _inventory())

    for answer in (
        reading.place_fit,
        reading.arrangement_fit,
        reading.level_fit,
        reading.pay_fit,
    ):
        assert not answer.stated
        assert answer.render() == "not stated"
        assert "no job-search parameters" in answer.note


def test_empty_parameter_summary_explains_how_to_configure_search() -> None:
    assert parameter_summary(JobSearchParameters()) == (
        "No job-search parameters are set.",
        "Add a [job_search] table to your settings file to express any.",
    )


def test_payroll_only_parameters_are_configured_and_observable() -> None:
    parameters = JobSearchParameters(
        employment=Employment(payroll_arrangements=["employee payroll"])
    )

    assert parameters.configured
    assert "  payroll arrangements: employee payroll" in parameter_summary(parameters)


def test_shift_only_parameters_are_configured_and_observable() -> None:
    parameters = JobSearchParameters(working_day_shift_hours=2)

    assert parameters.configured
    assert "  hours you can shift your working day: 2" in parameter_summary(parameters)


def test_a_stated_preference_meets_a_stated_arrangement() -> None:
    parameters = JobSearchParameters(arrangement=Arrangement(remote=Acceptance.PREFERRED))

    reading = read(_posting("This role is fully remote."), _inventory(), parameters=parameters)

    assert reading.arrangement_fit.value == "preferred"
    assert reading.arrangement_fit.evidence


def test_a_silent_posting_abstains_even_when_a_preference_exists() -> None:
    """Saying nothing about where the work happens is not saying it is on site."""
    parameters = JobSearchParameters(arrangement=Arrangement(remote=Acceptance.PREFERRED))

    reading = read(_posting("You will build data pipelines."), _inventory(), parameters=parameters)

    assert not reading.arrangement_fit.stated
    assert "states no arrangement" in reading.arrangement_fit.note


def test_an_onsite_recruiting_step_is_not_the_jobs_arrangement() -> None:
    """An interview location cannot retire a role whose work arrangement is unstated."""
    parameters = JobSearchParameters(arrangement=Arrangement(on_site=Acceptance.REFUSED))

    reading = read(
        _posting(
            "Build trusted data products.",
            "Applicants may be required to appear onsite at an office as part of the "
            "recruitment process.",
        ),
        _inventory(),
        parameters=parameters,
    )

    assert reading.location.value is None
    assert not reading.arrangement_fit.stated
    assert "states no arrangement" in reading.arrangement_fit.note


def test_a_hybrid_role_about_recruiting_keeps_its_arrangement() -> None:
    """A flattened interview section cannot erase the preceding arrangement marker."""
    reading = read(
        _posting(
            "#LI-Hybrid Our Interview Practices To maintain a fair hiring process, candidates "
            "may be asked to appear onsite during the recruitment process."
        ),
        _inventory(),
    )

    assert reading.location.value == "hybrid"


def test_an_unanswered_arrangement_is_not_a_refusal() -> None:
    """A person who answered only the remote question has not refused hybrid."""
    parameters = JobSearchParameters(arrangement=Arrangement(remote=Acceptance.PREFERRED))

    reading = read(
        _posting("Hybrid, three days in the office."), _inventory(), parameters=parameters
    )

    assert not reading.arrangement_fit.stated
    assert "not said either way" in reading.arrangement_fit.note


def test_office_days_are_reported_against_a_stated_ceiling() -> None:
    parameters = JobSearchParameters(
        arrangement=Arrangement(hybrid=Acceptance.ACCEPTABLE, maximum_office_days=2)
    )

    reading = read(
        _posting("Hybrid, 4 days per week in the office."), _inventory(), parameters=parameters
    )

    assert reading.arrangement_fit.value is not None
    assert "4 office days against your 2" in reading.arrangement_fit.value


def test_office_days_stated_over_another_period_are_not_read_as_weekly() -> None:
    """A monthly cadence is remote working, and is never compared to a weekly ceiling."""
    parameters = JobSearchParameters(
        arrangement=Arrangement(hybrid=Acceptance.ACCEPTABLE, maximum_office_days=1)
    )

    reading = read(
        _posting("Hybrid, with 2 days in the office each month."),
        _inventory(),
        parameters=parameters,
    )

    assert reading.location.value == "remote"
    assert "office days against" not in (reading.arrangement_fit.value or "")


def test_office_days_with_no_stated_period_are_not_read_as_weekly() -> None:
    """A bare day count is not evidence of how often the office is required."""
    parameters = JobSearchParameters(
        arrangement=Arrangement(hybrid=Acceptance.ACCEPTABLE, maximum_office_days=1)
    )

    reading = read(
        _posting("Hybrid, with 3 days in the office."), _inventory(), parameters=parameters
    )

    assert reading.arrangement_fit.value == Acceptance.ACCEPTABLE.value


def test_office_attendance_less_often_than_weekly_reads_as_remote() -> None:
    """Hybrid is the label for weekly attendance; anything rarer is remote."""
    reading = read(
        _posting("Hybrid role, with the team together in the office 2 days a month."),
        _inventory(),
    )

    assert reading.location.value == "remote"


def test_an_arrangement_stated_in_the_company_description_is_read() -> None:
    """A company calling itself fully remote is saying where the work happens."""
    posting = "\n".join(
        (
            "# Director of Evidence Reviews",
            "",
            "## About Us",
            "",
            "Northwind Analytics is a fully remote research consultancy.",
            "",
            "## Requirements",
            "",
            "- Lead systematic reviews.",
        )
    )

    assert read(posting, _inventory()).location.value == "remote"


def test_an_arrangement_packed_onto_a_benefits_line_is_read() -> None:
    """One line carrying a benefit and the arrangement must not lose the arrangement."""
    posting = "\n".join(
        (
            "# Data Platform Manager",
            "",
            "## Work-Life Balance",
            "",
            "Fully remote, flexible paid time off, and company-wide seasonal breaks.",
            "",
            "## Requirements",
            "",
            "- Own the ingestion roadmap.",
        )
    )

    assert read(posting, _inventory()).location.value == "remote"


def test_a_posting_naming_remote_beside_on_site_offers_remote() -> None:
    reading = read(_posting("Work on site in Toronto or remote across Canada."), _inventory())

    assert reading.location.value == "remote"


def test_a_posting_ruling_remote_out_is_not_read_as_remote() -> None:
    """A refusal names remote, and naming it is not offering it."""
    reading = read(
        _posting(
            "This position is not eligible for remote work.", "Hybrid, 3 days a week in the office."
        ),
        _inventory(),
    )

    assert reading.location.value == "hybrid"


def test_unbulleted_qualifications_are_read_one_by_one() -> None:
    """Boards emit a list as plain sentences, and each is its own requirement."""
    posting = "\n".join(
        (
            "# Senior Product Manager",
            "",
            "Minimum Qualifications:",
            "",
            "Eight years of product management experience.",
            " Strong understanding of machine learning technologies.",
            " Knowledge of APIs, DevOps, and cloud infrastructure.",
        )
    )

    stated = [line.text for line in read(posting, _inventory()).requirements]

    assert len(stated) == 3
    assert any("machine learning" in text for text in stated)


def test_a_hard_wrapped_sentence_is_still_read_as_one_requirement() -> None:
    """A board wrapping one bullet across lines must not become several."""
    posting = "\n".join(
        (
            "# Senior Product Manager",
            "",
            "Minimum Qualifications:",
            "",
            "- Eight years of product management experience across data",
            "  platforms and healthcare technology environments",
        )
    )

    stated = [line.text for line in read(posting, _inventory()).requirements]

    assert len(stated) == 1
    assert "healthcare technology environments" in stated[0]


def test_a_physical_requirements_heading_is_boilerplate_not_a_requirement() -> None:
    """A heading naming requirements is still boilerplate when it is the legal kind."""
    posting = "\n".join(
        (
            "# Senior Product Manager",
            "",
            "## Minimum Qualifications",
            "",
            "- Five years of product management experience.",
            "",
            "## Physical Requirements",
            "",
            "- Sitting for prolonged periods of time.",
            "",
            "Privacy Policy: By applying you agree to the applicant privacy policy.",
        )
    )

    stated = [line.text for line in read(posting, _inventory()).requirements]

    assert any("Five years" in text for text in stated)
    assert not any("Sitting for prolonged" in text for text in stated)
    assert not any("Privacy Policy" in text for text in stated)


def test_a_hybrid_posting_that_states_no_cadence_stays_hybrid() -> None:
    """Silence about how often is not evidence that attendance is rare."""
    reading = read(_posting("Hybrid. Boston, Massachusetts."), _inventory())

    assert reading.location.value == "hybrid"


def test_a_stated_share_of_the_week_on_site_stays_hybrid() -> None:
    """A share of the working week is weekly attendance however it is worded."""
    reading = read(_posting("This 70% in-office structure helps foster mentorship."), _inventory())

    assert reading.location.value == "hybrid"


def test_onsite_attendance_may_be_conditioned_on_funded_travel() -> None:
    """The condition on its own is an opinion the record has to carry."""
    arrangement = Arrangement(onsite_requires_paid_travel=True)

    assert arrangement.onsite_requires_paid_travel is True
    assert JobSearchParameters(arrangement=arrangement).configured


def test_a_stored_arrangement_without_the_condition_reads_as_unstated() -> None:
    """A parameter document written before the field existed still loads."""
    stored = {"remote": "preferred", "maximum_office_days": 2}

    arrangement = Arrangement.model_validate(stored)

    assert arrangement.onsite_requires_paid_travel is None


def test_a_place_inherits_the_funded_travel_condition_it_does_not_restate() -> None:
    base = Arrangement(hybrid=Acceptance.ACCEPTABLE, onsite_requires_paid_travel=True)

    merged = Arrangement(on_site=Acceptance.ACCEPTABLE).over(base)

    assert merged.onsite_requires_paid_travel is True


def test_travel_and_time_zone_clauses_are_keyed_hard_gates() -> None:
    """Publisher requirements are compared with parameters, never tuple positions."""
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="Canada")],
        working_time_zone=WorkingTimeZone.MOUNTAIN,
        arrangement=Arrangement(maximum_travel_percent=10),
    )
    reading = read(
        _posting(
            "Must be based in a region in the Eastern Standard Time zone.",
            "Travel up to 25%.",
        ),
        _inventory(),
        parameters=parameters,
    )

    by_name = {gate.name: gate for gate in gates(reading, parameters)}

    assert by_name["time zone"].standing is Standing.INELIGIBLE
    assert "Mountain is 2 hours from Eastern" in by_name["time zone"].reason
    assert by_name["travel"].standing is Standing.INELIGIBLE
    assert "25 per cent against your 10" in by_name["travel"].reason


@pytest.mark.parametrize(
    ("zone", "hours", "standing"),
    [
        (WorkingTimeZone.PACIFIC, 3, Standing.INELIGIBLE),
        (WorkingTimeZone.MOUNTAIN, 2, Standing.INELIGIBLE),
        (WorkingTimeZone.CENTRAL, 1, Standing.INELIGIBLE),
        (WorkingTimeZone.EASTERN, 0, Standing.ELIGIBLE),
    ],
)
def test_every_offered_working_time_zone_is_measured_against_the_required_one(
    zone: WorkingTimeZone, hours: int, standing: Standing
) -> None:
    """Every zone the picker offers is a selection the reader can act on."""
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="Canada")], working_time_zone=zone
    )
    reading = read(
        _posting("Must be based in a region in the Eastern Standard Time zone."),
        _inventory(),
        parameters=parameters,
    )

    gate = {item.name: item for item in gates(reading, parameters)}["time zone"]

    assert gate.standing is standing
    unit = "hour" if hours == 1 else "hours"
    assert f"{zone.value} is {hours} {unit} from Eastern" in gate.reason


def test_a_level_is_reported_inside_or_outside_the_band() -> None:
    parameters = JobSearchParameters(seniority=Seniority(minimum="senior", maximum="principal"))

    inside = read(
        _posting("Six years of experience.", title="Senior Data Engineer"),
        _inventory(),
        parameters=parameters,
    )
    outside = read(
        _posting("One year of experience.", title="Junior Data Engineer"),
        _inventory(),
        parameters=parameters,
    )

    assert inside.level_fit.value == "inside your senior to principal band"
    assert outside.level_fit.value == "outside your senior to principal band"


def test_pay_compares_only_within_one_currency_and_never_converts() -> None:
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="Atlantis")],
        compensation=[
            CompensationBand(location="home", currency="GBP", minimum=Decimal(90000)),
        ],
    )

    above = read(
        _posting("The annual range is GBP 110,000 to 130,000."),
        _inventory(),
        parameters=parameters,
    )
    below = read(
        _posting("The range is GBP 70,000 to 80,000 per year."),
        _inventory(),
        parameters=parameters,
    )
    elsewhere = read(
        _posting("The annual range is USD 200,000 to 240,000."),
        _inventory(),
        parameters=parameters,
    )

    assert above.pay_fit.value == "at or above your GBP annual floor"
    assert below.pay_fit.value == "below your GBP annual floor"
    assert elsewhere.pay_fit.stated
    assert elsewhere.pay_fit.value is not None
    assert elsewhere.pay_fit.value.startswith("shown: USD ")
    assert "add a USD band to compare" in elsewhere.pay_fit.note


def test_a_place_with_no_band_says_so_rather_than_naming_a_blank_currency() -> None:
    """The posting lands on the one declared place that carries no band."""
    parameters = JobSearchParameters(
        locations=[
            Location(
                key="us_remote",
                country="United States",
                aliases=["US-based"],
                eligibility=WorkEligibility.AUTHORIZED,
            ),
            Location(
                key="canada_remote",
                country="Canada",
                eligibility=WorkEligibility.AUTHORIZED,
            ),
        ],
        compensation=[
            CompensationBand(
                location="us_remote",
                currency="USD",
                basis="annual",
                minimum=Decimal(120000),
            )
        ],
    )

    reading = read(
        _posting("Fully remote across Canada.", "The annual range is CAD 150,000 to 170,000."),
        _inventory(),
        parameters=parameters,
    )

    assert reading.place_fit.value == "canada_remote: authorized"
    assert reading.pay_fit.stated
    assert reading.pay_fit.value is not None
    assert reading.pay_fit.value.startswith("shown: ")
    assert "no band declared for canada_remote" in reading.pay_fit.note


def test_a_posting_with_no_pay_abstains_rather_than_failing_the_band() -> None:
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="Atlantis")],
        compensation=[CompensationBand(location="home", currency="EUR", minimum=Decimal(60000))],
    )

    reading = read(
        _posting("A competitive package is offered."), _inventory(), parameters=parameters
    )

    assert not reading.pay_fit.stated
    assert "states no pay" in reading.pay_fit.note


def test_pay_shown_when_the_posting_and_band_use_different_periods() -> None:
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="Atlantis")],
        compensation=[
            CompensationBand(
                location="home",
                currency="GBP",
                basis="annual",
                minimum=Decimal(90000),
            )
        ],
    )

    reading = read(
        _posting("The range is GBP 9,000 to 10,000 per month."),
        _inventory(),
        parameters=parameters,
    )

    assert reading.pay_fit.stated
    assert reading.pay_fit.value is not None
    assert reading.pay_fit.value.startswith("shown: GBP ")
    assert "your GBP bands are annual" in reading.pay_fit.note
    assert "add a GBP monthly band to compare" in reading.pay_fit.note


def test_pay_abstains_when_figure_is_below_the_salary_floor() -> None:
    """A stray sub-thousand figure is a stipend or story number, never pay."""
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="Atlantis")],
        compensation=[CompensationBand(location="home", currency="EUR", minimum=Decimal(60000))],
    )

    reading = read(
        _posting("The range is EUR 2 to 3."),
        _inventory(),
        parameters=parameters,
    )

    assert not reading.pay_fit.stated
    assert "states no pay to compare" in reading.pay_fit.note


def test_a_figure_that_only_one_period_fits_is_compared() -> None:
    """Omitting the period is normal; a seventy thousand euro hourly rate is not."""
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="Atlantis")],
        compensation=[CompensationBand(location="home", currency="EUR", minimum=Decimal(60000))],
    )

    reading = read(
        _posting("The range is EUR 70,000 to 80,000."),
        _inventory(),
        parameters=parameters,
    )

    assert reading.pay_fit.value == "at or above your EUR annual floor"


def test_decimal_hourly_pay_compares_without_truncation() -> None:
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="Atlantis")],
        compensation=[
            CompensationBand(
                location="home",
                currency="GBP",
                basis="hourly",
                minimum=Decimal("45.25"),
            )
        ],
    )

    reading = read(
        _posting("The rate is GBP 45.50 per hour."),
        _inventory(),
        parameters=parameters,
    )

    assert reading.pay_fit.value == "at or above your GBP hourly floor"


def test_a_band_must_name_a_declared_location() -> None:
    with pytest.raises(PydanticValidationError, match="not declared"):
        JobSearchParameters(
            locations=[Location(key="home", country="Atlantis")],
            compensation=[CompensationBand(location="typo", currency="EUR", minimum=Decimal(1))],
        )


def test_a_band_refuses_numbers_that_contradict_each_other() -> None:
    with pytest.raises(PydanticValidationError, match="is above"):
        CompensationBand(location="home", currency="EUR", minimum=Decimal(90), target=Decimal(50))
    with pytest.raises(PydanticValidationError, match="at least one of"):
        CompensationBand(location="home", currency="EUR")


def test_duplicate_location_keys_are_refused() -> None:
    with pytest.raises(PydanticValidationError, match="Duplicate location keys"):
        JobSearchParameters(
            locations=[
                Location(key="home", country="Atlantis"),
                Location(key="home", country="Elsewhere"),
            ]
        )


def test_an_unknown_level_name_is_refused_with_the_ladder_listed() -> None:
    with pytest.raises(PydanticValidationError, match="must be one of"):
        Seniority(minimum="grandmaster")
    with pytest.raises(PydanticValidationError, match="sits above"):
        Seniority(minimum="director", maximum="junior")


def test_ineligible_places_are_excluded_from_the_actionable_set() -> None:
    parameters = JobSearchParameters(
        locations=[
            Location(key="here", country="Atlantis", eligibility=WorkEligibility.AUTHORIZED),
            Location(
                key="visa", country="Elsewhere", eligibility=WorkEligibility.SPONSORSHIP_REQUIRED
            ),
            Location(key="no", country="Nowhere", eligibility=WorkEligibility.INELIGIBLE),
        ]
    )

    assert parameters.actionable_locations == ("here", "visa")


def test_settings_read_a_job_search_table(tmp_path: Path) -> None:
    (tmp_path / "tailorcv.toml").write_text(
        "\n".join(
            (
                "[job_search]",
                'score_weight_set = "Mine"',
                "[[job_search.locations]]",
                'key = "home"',
                'country = "Atlantis"',
                'eligibility = "authorized"',
                "[job_search.arrangement]",
                'remote = "preferred"',
                "[[job_search.compensation]]",
                'location = "home"',
                'currency = "EUR"',
                "minimum = 80000",
            )
        ),
        encoding="utf-8",
    )

    settings = load_settings(start=tmp_path)

    assert settings.job_search.configured
    assert settings.job_search.actionable_locations == ("home",)
    assert settings.job_search.arrangement.remote is Acceptance.PREFERRED
    assert settings.job_search.bands_for("home")[0].currency == "EUR"


def test_a_typo_in_the_table_names_the_file_and_the_field(tmp_path: Path) -> None:
    (tmp_path / "tailorcv.toml").write_text(
        "[job_search]\nlocatoins = []\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as failure:
        load_settings(start=tmp_path)

    assert "tailorcv.toml" in str(failure.value)
    assert "locatoins: unknown setting" in str(failure.value)


def test_versions_are_appended_and_an_identical_save_writes_nothing(
    durable_database: Database,
) -> None:
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="params", name="Params"
    )
    service = SearchParametersService(durable_database)
    parameters = JobSearchParameters(locations=[Location(key="home", country="Atlantis")])

    assert service.current(workspace.id) == JobSearchParameters()
    first = service.save(workspace.id, parameters)
    again = service.save(workspace.id, parameters)
    changed = service.save(
        workspace.id,
        parameters.model_copy(update={"score_weight_set": "Mine"}),
    )

    assert first.version == 1
    assert not first.unchanged
    assert service.describe(workspace.id).id == changed.id
    assert again.unchanged and again.version == 1
    assert changed.version == 2
    assert changed.supersedes_id == first.id
    assert [item.version for item in service.history(workspace.id)] == [2, 1]
    assert service.current(workspace.id).score_weight_set == "Mine"


def test_a_stale_edit_is_refused_rather_than_silently_winning(
    durable_database: Database,
) -> None:
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="race", name="Race"
    )
    service = SearchParametersService(durable_database)
    service.save(workspace.id, JobSearchParameters(locations=[Location(key="a", country="A")]))

    with pytest.raises(ConflictError, match="Reload before saving"):
        service.save(
            workspace.id,
            JobSearchParameters(locations=[Location(key="b", country="B")]),
            expected_version=0,
        )


def test_an_unchanged_save_over_a_row_stored_before_a_field_existed_writes_nothing(
    durable_database: Database,
) -> None:
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="shift", name="Shift"
    )
    service = SearchParametersService(durable_database)
    parameters = JobSearchParameters(working_time_zone=WorkingTimeZone.MOUNTAIN)
    store_parameters_without(durable_database, workspace.id, parameters, "working_day_shift_hours")

    again = service.save(workspace.id, parameters)

    assert service.current(workspace.id).working_day_shift_hours == 0
    assert again.unchanged and again.version == 1
    for refused in (-1, 13):
        with pytest.raises(PydanticValidationError):
            JobSearchParameters(working_day_shift_hours=refused)


def test_every_offered_employment_type_saves_and_an_invented_one_is_refused(
    durable_database: Database,
) -> None:
    """The picker's whole vocabulary is savable; only a value nobody offered is refused."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="engagements", name="Engagements"
    )
    service = SearchParametersService(durable_database)

    saved = service.save(
        workspace.id,
        JobSearchParameters(employment=Employment(types=list(EMPLOYMENT_TYPES))),
    )

    assert service.current(workspace.id).employment.types == list(EMPLOYMENT_TYPES)
    assert saved.version == 1

    with pytest.raises(ValidationError, match="apprenticeship"):
        service.save(
            workspace.id,
            JobSearchParameters(employment=Employment(types=["moonlighting"])),
            expected_version=saved.version,
        )


@pytest.mark.parametrize(
    "offered", [EmploymentType.SEASONAL, EmploymentType.FIXED_TERM, EmploymentType.FREELANCE]
)
def test_a_less_common_offered_employment_type_saves_on_its_own(
    durable_database: Database, offered: EmploymentType
) -> None:
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="engagements", name="Engagements"
    )
    service = SearchParametersService(durable_database)

    service.save(workspace.id, JobSearchParameters(employment=Employment(types=[offered.value])))

    assert service.current(workspace.id).employment.types == [offered.value]


def test_parameters_for_a_missing_workspace_are_refused(durable_database: Database) -> None:
    with pytest.raises(NotFoundError):
        SearchParametersService(durable_database).current("no-such-workspace")


def test_the_digest_is_stable_across_equal_documents() -> None:
    one = JobSearchParameters(locations=[Location(key="a", country="A")])
    two = JobSearchParameters(locations=[Location(key="a", country="A")])
    other = JobSearchParameters(locations=[Location(key="b", country="B")])

    assert digest(one) == digest(two)
    assert digest(one) != digest(other)


def _us_and_canada() -> JobSearchParameters:
    """Two remote markets a person can work in, kept apart on purpose."""
    return JobSearchParameters(
        locations=[
            Location(
                key="us_remote",
                country="United States",
                aliases=["USA", "US-based", "Remote (US)"],
                eligibility=WorkEligibility.AUTHORIZED,
                basis="Citizen",
            ),
            Location(
                key="canada_remote",
                country="Canada",
                metro="Toronto",
                eligibility=WorkEligibility.AUTHORIZED,
                basis="Permanent resident",
                arrangement=Arrangement(on_site=Acceptance.ACCEPTABLE),
            ),
            Location(
                key="eu_remote",
                country="Germany",
                eligibility=WorkEligibility.SPONSORSHIP_REQUIRED,
            ),
        ],
        arrangement=Arrangement(remote=Acceptance.PREFERRED, on_site=Acceptance.REFUSED),
    )


def test_parameter_summary_renders_locations_and_arrangement() -> None:
    assert parameter_summary(_us_and_canada(), source=Path("/synthetic/tailorcv.toml")) == (
        "Read from: /synthetic/tailorcv.toml",
        "  us_remote: United States | authorized (Citizen)",
        "    also written as: USA, US-based, Remote (US)",
        "  canada_remote: Toronto, Canada | authorized (Permanent resident)",
        "    arrangement here: remote preferred, on site acceptable",
        "  eu_remote: Germany | sponsorship required",
        "  remote: preferred",
        "  on site: refused",
    )


def test_a_posting_is_placed_in_the_market_it_names() -> None:
    parameters = _us_and_canada()

    us = read(
        _posting("This role is fully remote for US-based candidates."),
        _inventory(),
        parameters=parameters,
    )
    canada = read(_posting("Fully remote across Canada."), _inventory(), parameters=parameters)

    assert us.place_fit.value == "us_remote: authorized"
    assert canada.place_fit.value == "canada_remote: authorized"
    assert us.place_fit.evidence


def test_a_country_only_location_does_not_claim_a_more_specific_market() -> None:
    parameters = JobSearchParameters(
        locations=[
            Location(key="city", country="Freedonia", region="Northland", metro="Northtown"),
            Location(key="country", country="Freedonia"),
        ]
    )

    assert tuple(place.key for place in parameters.matching_locations("Freedonia")) == ("country",)
    assert tuple(place.key for place in parameters.matching_locations("Northtown, Freedonia")) == (
        "city",
    )


def test_a_residence_city_suggesting_a_declared_country_is_not_a_failed_requirement() -> None:
    reading = read(
        _posting("Candidates must reside in Boston, Lincolnshire, England."),
        _inventory(),
        parameters=_us_and_canada(),
    )

    assert not reading.residence_requirement.value


def test_a_market_needing_sponsorship_is_reported_as_such_rather_than_hidden() -> None:
    parameters = _us_and_canada()

    reading = read(_posting("Remote within Germany."), _inventory(), parameters=parameters)

    assert reading.place_fit.value == "eu_remote: sponsorship required"


def test_a_posting_naming_no_declared_place_scores_down_without_refusing() -> None:
    """A market nobody declared ranks a posting down and does not rule it out.

    No residence rule follows, so nothing downstream reads it as somewhere the
    reader can work.
    """
    parameters = _us_and_canada()

    reading = read(
        _posting("Fully remote from anywhere in Brazil."), _inventory(), parameters=parameters
    )

    assert reading.place_fit.value == f": {OUTSIDE_DECLARED}"
    assert "names none of your places" in reading.place_fit.note
    assert eligibility_score(OUTSIDE_DECLARED, "").value == OUTSIDE_DECLARED_SCORE
    rule, _, _ = reading.place_fit.value.partition(": ")
    assert not rule


def test_a_posting_naming_two_declared_places_settles_standing_but_not_the_place() -> None:
    """Which place it is stays open; whether the person may work it does not.

    No key follows, so nothing keyed to one place, such as a currency band, is
    decided by a posting that names two countries.
    """
    parameters = _us_and_canada()

    reading = read(
        _posting("Open to remote candidates in the United States or Canada."),
        _inventory(),
        parameters=parameters,
    )

    assert reading.place_fit.value is not None
    assert reading.place_fit.value.endswith(": authorized")
    assert not reading.place_fit.conflict
    assert not reading.pay_fit.stated


def test_places_gate_nothing_when_none_are_declared() -> None:
    parameters = JobSearchParameters(arrangement=Arrangement(remote=Acceptance.PREFERRED))

    reading = read(
        _posting("Fully remote in the United States."), _inventory(), parameters=parameters
    )

    assert not reading.place_fit.stated
    assert "no job-search parameters" in reading.place_fit.note


def test_a_location_arrangement_overrides_the_general_one() -> None:
    """On site is refused everywhere except the one city that says otherwise."""
    parameters = _us_and_canada()

    in_toronto = read(
        _posting("This role is on site at our office in Toronto, Canada."),
        _inventory(),
        parameters=parameters,
    )
    in_the_us = read(
        _posting("This role is on site at our office in the United States."),
        _inventory(),
        parameters=parameters,
    )

    assert in_toronto.arrangement_fit.value == "acceptable"
    assert in_the_us.arrangement_fit.value == "refused"


def test_an_override_inherits_every_answer_it_does_not_give() -> None:
    parameters = _us_and_canada()

    reading = read(_posting("Fully remote across Canada."), _inventory(), parameters=parameters)

    assert reading.arrangement_fit.value == "preferred"
    assert parameters.arrangement_for("canada_remote").remote is Acceptance.PREFERRED
    assert parameters.arrangement_for("canada_remote").on_site is Acceptance.ACCEPTABLE
    assert parameters.arrangement_for(None).on_site is Acceptance.REFUSED
    assert parameters.arrangement_for("us_remote").on_site is Acceptance.REFUSED


def test_a_short_alias_matches_words_and_not_fragments() -> None:
    """A two-letter alias is the kind somebody writes, so it has to be safe."""
    parameters = JobSearchParameters(
        locations=[Location(key="us_remote", country="United States", aliases=["US"])]
    )

    matched = read(_posting("Fully remote, US-based only."), _inventory(), parameters=parameters)
    fragment = read(
        _posting("We will discuss the plan in our first bus tour."),
        _inventory(),
        parameters=parameters,
    )

    assert matched.place_fit.value == "us_remote: authorized"
    assert fragment.place_fit.value == f": {OUTSIDE_DECLARED}"


def test_a_listing_location_places_a_body_that_names_no_place() -> None:
    reading = read(
        _posting("Lead product discovery with clinical teams.", "#LI-Remote"),
        _inventory(),
        parameters=_us_and_canada(),
        listing_location="Toronto, Canada",
    )

    assert reading.place_fit.value == "canada_remote: authorized"
    assert reading.place_fit.evidence == ("listing location: Toronto, Canada",)


def test_the_body_content_outranks_the_listing_location() -> None:
    reading = read(
        _posting("Fully remote within Germany."),
        _inventory(),
        parameters=_us_and_canada(),
        listing_location="United States",
    )

    assert reading.place_fit.value == "eu_remote: sponsorship required"


def test_a_listing_location_naming_no_declared_place_leaves_the_body_to_decide() -> None:
    reading = read(
        _about("We hire remotely across Canada."),
        _inventory(),
        parameters=_two_countries(),
        listing_location="Sao Paulo, Brazil",
    )

    assert reading.place_fit.value == "canada_remote: authorized"


def _about(sentence: str) -> str:
    """Return a posting stating ``sentence`` only in its about-us block."""
    return "\n".join(
        (
            "# Product Manager",
            "",
            "## About Us",
            "",
            sentence,
            "",
            "## Requirements",
            "",
            "- Five years of product management experience.",
        )
    )


def _two_countries() -> JobSearchParameters:
    return JobSearchParameters(
        locations=[
            Location(
                key="us_remote", country="United States", eligibility=WorkEligibility.AUTHORIZED
            ),
            Location(key="canada_remote", country="Canada", eligibility=WorkEligibility.AUTHORIZED),
        ]
    )


_MARKET_FRAMES = (
    "We serve {audience} {reach} {place}.",
    "Our platform is trusted by {audience} {reach} {place}.",
    "Our tools are used by {audience} {reach} {place}.",
    "Our app is available to {audience} {reach} {place}.",
    "{audience} {reach} {place} rely on our platform.",
    "Serving {audience} {reach} {place} since 2010, we grow every year.",
)
_AUDIENCES = ("patients", "hospitals", "health systems", "customers", "members", "clinics")
_REACHES = ("in", "across", "throughout", "nationwide in", "all over")
_MARKET_PLACES = ("the US", "the United States", "Canada", "the USA")


@pytest.mark.parametrize(
    "sentence",
    [
        "Healthcare that can serve everyone in the US.",
        "We serve patients across the US.",
        "Trusted by hospitals across the United States.",
        "We have customers throughout Canada.",
        "Our app is available to members nationwide in the US.",
        "Our records platform is used by clinics in all 50 states.",
    ],
)
def test_a_customer_market_sentence_is_not_a_hiring_place(sentence: str) -> None:
    reading = read(_about(sentence), _inventory(), parameters=_two_countries())

    assert reading.place_fit.value == f": {OUTSIDE_DECLARED}"


def test_no_written_form_of_a_customer_market_reads_as_a_hiring_place() -> None:
    parameters = _two_countries()
    sentences = [
        frame.format(audience=audience, reach=reach, place=place)
        for frame in _MARKET_FRAMES
        for audience in _AUDIENCES
        for reach in _REACHES
        for place in _MARKET_PLACES
    ]

    placed = [
        sentence
        for sentence in sentences
        if read(_about(sentence), parameters=parameters).place_fit.value != f": {OUTSIDE_DECLARED}"
    ]

    assert placed == []


@pytest.mark.parametrize(
    "sentence",
    [
        "This role is open to candidates in the US.",
        "Remote (US)",
        "You must be based in Canada.",
        "Location: United States",
        "We hire across the US and Canada.",
        "Candidates must reside in the United States.",
        "This position is fully remote within Canada.",
        "Work from anywhere in the USA.",
        "Our customers are global; this role is remote in Canada.",
        "We serve clinics nationwide. This role is remote in the US.",
        "Join our team members across the US.",
        "This position is available to candidates in the US.",
        "This role is available to applicants based in Canada.",
        "You will serve as the product lead for teams in Canada.",
    ],
)
def test_a_hiring_place_outside_the_requirements_still_places_the_posting(sentence: str) -> None:
    reading = read(_about(sentence), _inventory(), parameters=_two_countries())

    assert reading.place_fit.value not in (None, f": {OUTSIDE_DECLARED}")


def test_a_hiring_place_beside_a_customer_market_reads_only_the_hiring_place() -> None:
    reading = read(
        _about("Trusted by clinics across the United States. We hire in Canada."),
        _inventory(),
        parameters=_two_countries(),
    )

    assert reading.place_fit.value == "canada_remote: authorized"


@pytest.mark.parametrize(
    ("declared", "publisher"),
    [
        ("United States", "USA - Chicago, IL"),
        ("Canada", "CAN - Toronto, ON"),
        ("United Kingdom", "Great Britain - London"),
        ("Europe", "Remote within the EU"),
    ],
)
def test_shipped_country_spellings_do_not_require_location_aliases(
    declared: str, publisher: str
) -> None:
    """Publisher country vocabulary belongs to the product, not workspace preferences."""
    parameters = JobSearchParameters(locations=[Location(key="market", country=declared)])

    assert parameters.matching_locations(publisher) == tuple(parameters.locations)
    assert parameters.country_named_in(publisher) == declared
    assert parameters.matching_locations("We will discuss the role") == ()


def test_two_shipped_country_spellings_stay_two_markets() -> None:
    """A normalized alias cannot disappear merely because another country is named."""
    parameters = JobSearchParameters(
        locations=[
            Location(key="ca", country="Canada"),
            Location(key="us", country="United States"),
        ]
    )

    assert parameters.matching_locations("Remote in Canada or the USA") == tuple(
        parameters.locations
    )
    assert parameters.country_named_in("Remote in Canada or the USA") == ""


def test_a_declared_country_spelled_in_a_foreign_street_places_nothing() -> None:
    """A declared country's word inside a foreign street address places nothing."""
    parameters = JobSearchParameters(locations=[Location(key="ca", country="Canada")])

    assert parameters.matching_locations("GBR - London, Canada Square") == ()
    assert parameters.country_named_in("GBR - London, Canada Square") == ""
    assert parameters.matching_locations("Toronto, Canada") == tuple(parameters.locations)


@pytest.mark.parametrize(
    ("declared", "publisher"), [("USA", "New York, USA"), ("UK", "London, UK")]
)
def test_a_country_declared_by_alias_still_matches_its_own_word(
    declared: str, publisher: str
) -> None:
    """A declared spelling the table resolves is compared through the table, not by string."""
    parameters = JobSearchParameters(locations=[Location(key="x", country=declared)])

    assert parameters.matching_locations(publisher) == tuple(parameters.locations)
    assert parameters.country_named_in(publisher) == declared


def test_the_ledger_filters_on_the_place_an_evaluation_recorded(
    durable_database: Database, tmp_path: Path
) -> None:
    """Filtering reads the committed residence rule, not a fresh reading."""
    from tailorcv.application import job_search
    from tailorcv.application.contracts import EligibilityInput
    from tailorcv.application.job_search.models import JobListQuery
    from tests.support.artifacts import artifact_service
    from tests.support.scenarios import evaluation_commit

    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="places", name="Places"
    )
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")
    for employer, place, requisition in (
        ("Northern Co", "canada_remote", "REQ-1"),
        ("Southern Co", "us_remote", "REQ-2"),
    ):
        job_search.commit_evaluation(
            durable_database,
            artifacts,
            evaluation_commit(
                workspace.id,
                employer=employer,
                requisition_id=requisition,
                canonical_url=f"https://example.test/jobs/{requisition}",
                discovery_url=f"https://example.test/jobs/{requisition}",
                eligibility=EligibilityInput(workspace_location_key=place),
            ),
        )

    everything, total = job_search.list_jobs(
        durable_database, JobListQuery(workspace_id=workspace.id)
    )
    canada, canada_total = job_search.list_jobs(
        durable_database, JobListQuery(workspace_id=workspace.id, places=("canada_remote",))
    )

    assert total == 2
    assert {row.place for row in everything} == {"canada_remote", "us_remote"}
    assert canada_total == 1
    assert canada[0].employer == "Northern Co"
    assert canada[0].place == "canada_remote"


def _nested_places() -> JobSearchParameters:
    """Three declared places that nest, which is what a real settings file has."""
    return JobSearchParameters(
        locations=[
            Location(key="northtown", country="Canada", region="Manitoba", metro="Northtown"),
            Location(key="manitoba_remote", country="Canada", region="Manitoba"),
            Location(key="canada_remote", country="Canada"),
        ],
        compensation=[
            CompensationBand(
                location="canada_remote",
                currency="CAD",
                basis="annual",
                minimum=Decimal(150000),
            )
        ],
    )


def test_a_posting_naming_only_the_country_resolves_to_the_country_place() -> None:
    """Nested declarations all match the country, and the country one is the answer."""
    reading = read(
        _posting("Remote anywhere in Canada."), _inventory(), parameters=_nested_places()
    )

    assert reading.place_fit.value == "canada_remote: authorized"


def test_a_posting_naming_the_metro_resolves_to_the_metro_place() -> None:
    reading = read(
        _posting("Based in Northtown, Manitoba, Canada."),
        _inventory(),
        parameters=_nested_places(),
    )

    assert reading.place_fit.value == "northtown: authorized"


def _remote_country_and_hybrid_metro() -> JobSearchParameters:
    """Remote across the country, and hybrid accepted only around one metro."""
    return JobSearchParameters(
        locations=[
            Location(
                key="freedonia_remote",
                country="Freedonia",
                arrangement=Arrangement(remote=Acceptance.PREFERRED, hybrid=Acceptance.REFUSED),
            ),
            Location(
                key="northtown",
                country="Freedonia",
                region="Northland",
                metro="Northtown",
                arrangement=Arrangement(hybrid=Acceptance.ACCEPTABLE),
            ),
        ],
    )


@pytest.mark.parametrize(
    ("place", "standing"),
    [
        ("Hybrid, Northland, Freedonia", Standing.UNKNOWN),
        (
            "The location for this hybrid position is flexible within the Province of Northland, "
            "Freedonia.",
            Standing.UNKNOWN,
        ),
        (
            "Hybrid, Northland, Freedonia. Eligible to work remotely within Northland.",
            Standing.UNKNOWN,
        ),
        ("This is a hybrid position. Anywhere in Northland, Freedonia.", Standing.UNKNOWN),
        ("Hybrid role based anywhere within Northland, Freedonia.", Standing.UNKNOWN),
        ("Hybrid, Southland, Freedonia", Standing.INELIGIBLE),
        ("Hybrid, Freedonia", Standing.INELIGIBLE),
        (
            "Location: Easttown, Northland, Freedonia. This is a hybrid position.",
            Standing.INELIGIBLE,
        ),
        ("Hybrid, Red Hill, Northland, Freedonia", Standing.INELIGIBLE),
        ("Hybrid, Port St. Anne, Northland, Freedonia", Standing.INELIGIBLE),
        *(
            (f"Hybrid, Easttown, Northland, Freedonia. {aside}", Standing.INELIGIBLE)
            for aside in (
                "Travel to client sites across Northland up to 20%.",
                "Northland residents preferred.",
                "You will support Northland-based health systems.",
            )
        ),
        ("Data Engineer (Northland). Hybrid, Easttown, Northland, Freedonia.", Standing.INELIGIBLE),
        (
            "Hybrid. Office at Großestraße campus. Easttown, Northland, Freedonia",
            Standing.INELIGIBLE,
        ),
        ("Hybrid, Northtown, Northland, Freedonia", Standing.ELIGIBLE),
    ],
)
def test_a_hybrid_posting_naming_only_a_region_leaves_the_arrangement_open_for_a_metro_there(
    place: str, standing: Standing
) -> None:
    parameters = _remote_country_and_hybrid_metro()
    country_block = parameters.model_copy(
        update={"arrangement": parameters.arrangement_for("freedonia_remote")}
    )

    for wanted in (parameters, country_block):
        reading = read(_posting(place, "SQL"), _inventory(), parameters=wanted)
        found = {gate.name: gate.standing for gate in gates(reading, wanted)}
        assert found["arrangement"] is standing


@pytest.mark.parametrize(("days", "standing"), [(4, Standing.INELIGIBLE), (2, Standing.UNKNOWN)])
def test_a_region_only_hybrid_posting_stays_refused_past_the_metros_office_day_limit(
    days: int, standing: Standing
) -> None:
    parameters = _remote_country_and_hybrid_metro()
    metro = parameters.locations[1].model_copy(
        update={
            "arrangement": Arrangement(hybrid=Acceptance.ACCEPTABLE, maximum_office_days=2),
        }
    )
    parameters = parameters.model_copy(update={"locations": [parameters.locations[0], metro]})
    posting = _posting(
        "Hybrid, Northland, Freedonia.", f"You will work {days} days per week in the office."
    )

    reading = read(posting, _inventory(), parameters=parameters)

    assert {gate.name: gate.standing for gate in gates(reading, parameters)}[
        "arrangement"
    ] is standing


def test_a_compliance_standard_is_not_a_currency() -> None:
    """A three-letter run is not a currency code, so SOC 2 is not two thousand SOC."""
    reading = read(
        _posting("We maintain SOC 2 and ISO 27001 compliance."),
        _inventory(),
        parameters=_nested_places(),
    )

    assert not reading.pay_fit.stated
    assert "states no pay" in reading.pay_fit.note


def test_a_stray_figure_does_not_outrank_the_line_that_names_pay() -> None:
    reading = read(
        _posting(
            "We manage $3 billion in assets.",
            "The base salary range is $160,000 to $190,000 per year.",
        ),
        _inventory(),
        parameters=_nested_places(),
    )

    assert reading.pay_fit.value == "at or above your CAD annual floor"


def test_a_pay_period_on_a_neighbouring_line_still_counts() -> None:
    """Boards put the range on one line and the period on the next."""
    reading = read(
        _posting("Base salary range", "$160,000 - $190,000", "per year, plus equity."),
        _inventory(),
        parameters=_nested_places(),
    )

    assert reading.pay_fit.value == "at or above your CAD annual floor"


def test_a_range_written_without_a_currency_counts_on_a_pay_line() -> None:
    """Several boards state the currency in a heading and the range bare."""
    reading = read(
        _posting("Base salary in CAD", "165,800 - 276,300 annually"),
        _inventory(),
        parameters=_nested_places(),
    )

    assert reading.pay_fit.value == "at or above your CAD annual floor"


def test_a_bare_dollar_sign_takes_the_currency_of_the_place_it_is_in() -> None:
    """A dollar sign names no country; the band declared for the place does."""
    reading = read(
        _posting("Remote anywhere in Canada.", "The salary range is $160,000 to $190,000 a year."),
        _inventory(),
        parameters=_nested_places(),
    )

    assert reading.pay_fit.value == "at or above your CAD annual floor"


def test_a_posting_offering_hybrid_or_remote_is_read_as_the_remote_option() -> None:
    """Boards write "City-Hybrid / Country-Remote" to offer a choice.

    Reading the most restrictive option would refuse postings whose remote
    option is the one being applied for.
    """
    reading = read(
        _posting("Location: Mississauga-Hybrid / Canada-Remote"),
        _inventory(),
        parameters=_nested_places(),
    )

    assert reading.location.value == "remote"


def test_places_with_different_standing_stay_a_conflict() -> None:
    parameters = JobSearchParameters(
        locations=[
            Location(key="here", country="Atlantis", eligibility=WorkEligibility.AUTHORIZED),
            Location(key="there", country="Lemuria", eligibility=WorkEligibility.INELIGIBLE),
        ],
    )

    reading = read(
        _posting("Remote across Atlantis or Lemuria."), _inventory(), parameters=parameters
    )

    assert not reading.place_fit.stated
    assert reading.place_fit.conflict


def test_a_country_beside_the_figure_decides_which_dollar_it_is() -> None:
    """Boards that pay differently by country write one line each.

    The country is stated beside the figure, so nothing has to be assumed about
    which dollar a bare sign means.
    """
    parameters = JobSearchParameters(
        locations=[
            Location(key="ca", country="Canada"),
            Location(key="us", country="United States"),
        ],
        compensation=[
            CompensationBand(
                location="ca", currency="CAD", basis="annual", minimum=Decimal(150000)
            ),
            CompensationBand(
                location="us", currency="USD", basis="annual", minimum=Decimal(200000)
            ),
        ],
    )

    reading = read(
        _posting(
            "The annual salary range depends on where you are:",
            "United States: $180,000 to $220,000",
            "Canada: $160,000 to $190,000",
        ),
        _inventory(),
        parameters=parameters,
    )

    assert reading.pay_fit.value == "below your USD annual floor"


def test_a_bare_dollar_with_no_country_beside_it_stays_open() -> None:
    """Nothing is assumed when the posting does not say which dollar it means."""
    parameters = JobSearchParameters(
        locations=[
            Location(key="ca", country="Canada"),
            Location(key="us", country="United States"),
        ],
        compensation=[
            CompensationBand(
                location="ca", currency="CAD", basis="annual", minimum=Decimal(150000)
            ),
            CompensationBand(
                location="us", currency="USD", basis="annual", minimum=Decimal(200000)
            ),
        ],
    )

    reading = read(
        _posting("Open to Canada or the United States.", "Salary: $180,000 per year"),
        _inventory(),
        parameters=parameters,
    )

    assert not reading.pay_fit.stated
    assert "names no country beside it" in reading.pay_fit.note


@pytest.mark.parametrize(
    ("place_line", "expected"),
    [
        ("Remote within the United States.", "shown: USD 120,000-150,000 annual"),
        ("Remote anywhere in Canada.", "below your CAD annual floor"),
        # With no country anywhere in the posting, the person's only band breaks the tie.
        ("Fully remote.", "below your CAD annual floor"),
    ],
    ids=["us posting", "canadian posting", "unplaced posting"],
)
def test_the_posting_country_decides_a_bare_dollar_before_the_persons_bands(
    place_line: str, expected: str
) -> None:
    parameters = JobSearchParameters(
        locations=[Location(key="ca", country="Canada")],
        compensation=[
            CompensationBand(location="ca", currency="CAD", basis="annual", minimum=Decimal(160000))
        ],
    )

    reading = read(
        _posting(place_line, "Salary: $120,000 - $150,000 per year"),
        _inventory(),
        parameters=parameters,
    )

    assert reading.pay_fit.value == expected


_REMOTE_MARKETS = JobSearchParameters(
    locations=[
        Location(key="home", country="Canada", region="Manitoba", metro="Winnipeg", resident=True),
        Location(key="ca_remote", country="Canada"),
        Location(key="us_remote", country="United States"),
    ]
)


@pytest.mark.parametrize(
    ("line", "listing"),
    [
        ("This is a remote position.", ""),
        ("Remote - Home Based", ""),
        ("", "Remote"),
        ("#LI-Remote", ""),
        ("", "100% Remote"),
        ("", "Remote."),
    ],
    ids=[
        "remote position",
        "home based",
        "bare remote listing",
        "li remote",
        "percent remote listing",
        "remote listing with a stop",
    ],
)
def test_a_remote_posting_that_names_no_country_leaves_the_market_open(
    line: str, listing: str
) -> None:
    reading = read(
        _posting("You will own the roadmap.", line),
        _inventory(),
        parameters=_REMOTE_MARKETS,
        listing_location=listing,
    )

    assert not reading.place_fit.stated
    assert "names no country" in reading.place_fit.note


@pytest.mark.parametrize(
    ("line", "listing"),
    [
        ("Work Anywhere", "Toronto"),
        ("#LI-Remote", "Toronto, Canada"),
        ("Fully remote.", "Toronto, ON"),
    ],
    ids=["work anywhere with a city", "city and country", "city and province"],
)
def test_a_remote_posting_listed_under_a_canadian_city_reads_as_canada_remote(
    line: str, listing: str
) -> None:
    reading = read(
        _posting("You will own the roadmap.", line),
        _inventory(),
        parameters=_REMOTE_MARKETS,
        listing_location=listing,
    )

    assert reading.place_fit.value == "ca_remote: authorized"


@pytest.mark.parametrize(
    ("line", "listing"),
    [
        ("This is a remote position.", "Remote - United Kingdom"),
        ("Remote within Germany.", ""),
        ("This is an on-site role.", ""),
    ],
    ids=["remote foreign listing", "remote foreign body", "no remote and no place"],
)
def test_a_posting_outside_the_remote_rule_still_reads_outside(line: str, listing: str) -> None:
    reading = read(
        _posting("You will own the roadmap.", line),
        _inventory(),
        parameters=_REMOTE_MARKETS,
        listing_location=listing,
    )

    assert reading.place_fit.value == f": {OUTSIDE_DECLARED}"


@pytest.mark.parametrize(
    "listing", ["Remote - EMEA", "Remote - LATAM", "Remote (APAC)", "Remote - Vancouver"]
)
def test_a_remote_listing_naming_a_region_still_reads_outside(listing: str) -> None:
    reading = read(
        _posting("You will own the roadmap.", "This is a remote position."),
        _inventory(),
        parameters=_REMOTE_MARKETS,
        listing_location=listing,
    )

    assert reading.place_fit.value == f": {OUTSIDE_DECLARED}"


def test_a_city_that_only_suggests_an_ineligible_country_never_refuses() -> None:
    parameters = JobSearchParameters(
        locations=[
            Location(key="ca_remote", country="Canada"),
            Location(
                key="us_remote", country="United States", eligibility=WorkEligibility.INELIGIBLE
            ),
        ]
    )

    reading = read(
        _posting("You will own the roadmap.", "This is a remote position."),
        _inventory(),
        parameters=parameters,
        listing_location="Remote - Boston",
    )

    assert reading.place_fit.value == f": {OUTSIDE_DECLARED}"


def test_a_settled_place_uses_its_own_compensation_band() -> None:
    parameters = JobSearchParameters(
        locations=[
            Location(key="toronto", country="Canada", metro="Toronto"),
            Location(key="vancouver", country="Canada", metro="Vancouver"),
        ],
        compensation=[
            CompensationBand(location="toronto", currency="CAD", minimum=Decimal(100000)),
            CompensationBand(location="vancouver", currency="CAD", minimum=Decimal(180000)),
        ],
    )

    reading = read(
        _posting("Vancouver, Canada.", "Salary: CAD 150,000 per year"),
        _inventory(),
        parameters=parameters,
    )

    assert reading.pay_fit.value == "below your CAD annual floor"


def test_an_explicit_trailing_currency_code_resolves_a_dollar_range() -> None:
    parameters = JobSearchParameters(
        locations=[
            Location(key="ca", country="Canada"),
            Location(key="us", country="United States"),
        ],
        compensation=[
            CompensationBand(location="ca", currency="CAD", minimum=Decimal(150000)),
            CompensationBand(location="us", currency="USD", minimum=Decimal(100000)),
        ],
    )

    reading = read(
        _posting("Salary: $100,000 - $120,000 USD per year"),
        _inventory(),
        parameters=parameters,
    )

    assert reading.pay_fit.value == "at or above your USD annual floor"


def test_pay_is_scored_against_all_three_numbers_the_band_states() -> None:
    """A band states minimum, target, and stretch, so a floor alone wastes two.

    Pay just over the floor and pay at the stretch both clear the floor, and
    they are not the same offer.
    """
    band = CompensationBand(
        location="ca",
        currency="CAD",
        basis="annual",
        minimum=Decimal(120000),
        target=Decimal(210000),
        stretch=Decimal(240000),
    )

    assert band.standing(Decimal(120000)) == 60
    assert band.standing(Decimal(210000)) == 90
    assert band.standing(Decimal(240000)) == 100
    assert band.standing(Decimal(60000)) == 30
    # Between the floor and the target, so between their two scores.
    assert 60 < band.standing(Decimal(140000)) < 90


def test_pay_above_the_stretch_scores_as_the_target_rather_than_the_top() -> None:
    """Pay past what was asked for can mean a role at another level."""
    band = CompensationBand(
        location="ca",
        currency="CAD",
        minimum=Decimal(120000),
        target=Decimal(210000),
        stretch=Decimal(240000),
    )
    no_stretch = CompensationBand(
        location="ca", currency="CAD", minimum=Decimal(120000), target=Decimal(210000)
    )

    assert band.standing(Decimal(240000)) == 100
    assert band.standing(Decimal(240001)) == 90
    assert band.standing(Decimal(300000)) == 90
    assert no_stretch.standing(Decimal(300000)) == 100


def test_the_pay_note_names_the_stretch_a_posting_floor_is_above() -> None:
    parameters = JobSearchParameters(
        locations=[Location(key="ca", country="Canada")],
        compensation=[
            CompensationBand(
                location="ca",
                currency="CAD",
                minimum=Decimal(120000),
                target=Decimal(210000),
                stretch=Decimal(240000),
            )
        ],
    )

    above = read(
        _posting("The base salary range is CAD 260,000 to 280,000 per year."),
        _inventory(),
        parameters=parameters,
    )
    at = read(
        _posting("The base salary range is CAD 240,000 to 280,000 per year."),
        _inventory(),
        parameters=parameters,
    )

    assert above.pay_fit.value == "at or above your CAD annual floor"
    assert above.pay_fit.note == (
        "the posting states this CAD annual pay range, and its floor is above your CAD "
        "annual stretch figure"
    )
    assert (above.pay_standing, above.pay_target_met, above.pay_above_stretch) == (
        90,
        True,
        "CAD annual",
    )
    assert at.pay_fit.note == "the posting states this CAD annual pay range"
    assert (at.pay_standing, at.pay_above_stretch) == (100, "")


def test_a_floor_only_band_never_meets_an_unstated_target() -> None:
    band = CompensationBand(location="ca", currency="CAD", minimum=Decimal(120000))

    assert not band.target_met(Decimal(120000))
    assert not band.target_met(Decimal(240000))


def test_the_reading_carries_where_in_the_band_the_pay_falls() -> None:
    parameters = JobSearchParameters(
        locations=[Location(key="ca", country="Canada")],
        compensation=[
            CompensationBand(
                location="ca",
                currency="CAD",
                basis="annual",
                minimum=Decimal(120000),
                target=Decimal(210000),
                stretch=Decimal(240000),
            )
        ],
    )

    modest = read(
        _posting("The base salary range is CAD 130,000 to 150,000 per year."),
        _inventory(),
        parameters=parameters,
    )
    strong = read(
        _posting("The base salary range is CAD 230,000 to 250,000 per year."),
        _inventory(),
        parameters=parameters,
    )

    assert modest.pay_fit.value == "at or above your CAD annual floor"
    assert strong.pay_fit.value == "at or above your CAD annual floor"
    assert modest.pay_standing is not None and strong.pay_standing is not None
    assert modest.pay_standing < strong.pay_standing
    assert (
        compensation_score(modest.pay_fit.value, modest.pay_fit.note, modest.pay_standing).value
        == modest.pay_standing
    )


def test_the_size_of_a_figure_settles_the_period_a_posting_omits() -> None:
    """A six figure sum is not an hourly rate, and sixty-eight is not a salary.

    Measured against the reader's own bands rather than a table of market rates,
    and it answers only when one period puts the figure in range.
    """
    bands = [
        CompensationBand(
            location="ca",
            currency="CAD",
            basis="annual",
            minimum=Decimal(120000),
            target=Decimal(210000),
            stretch=Decimal(240000),
        )
    ]

    assert basis_for(Decimal(194636), bands) == "annual"
    assert basis_for(Decimal(68), bands) == "hourly"
    assert basis_for(Decimal(9000), bands) == "monthly"
    assert basis_for(Decimal(1), bands) is None
    assert basis_for(Decimal(500000), []) is None


def test_a_posting_that_omits_its_pay_period_is_still_compared() -> None:
    parameters = JobSearchParameters(
        locations=[Location(key="ca", country="Canada")],
        compensation=[
            CompensationBand(
                location="ca",
                currency="CAD",
                basis="annual",
                minimum=Decimal(120000),
                target=Decimal(210000),
            )
        ],
    )

    reading = read(
        _posting("Salary Range CAD 165,800.00 - 276,300.00"),
        _inventory(),
        parameters=parameters,
    )

    assert reading.pay_fit.value == "at or above your CAD annual floor"
    assert reading.pay_standing is not None


def test_pay_reads_a_range_written_with_the_word_to() -> None:
    """A hyphen is one separator; boards also write ranges with the word ``to``."""
    parameters = JobSearchParameters(
        locations=[Location(key="ca", country="Canada")],
        compensation=[
            CompensationBand(location="ca", currency="CAD", basis="annual", minimum=Decimal(120000))
        ],
    )

    reading = read(
        _posting("The annual range is CAD 150000 to CAD 190000."),
        _inventory(),
        parameters=parameters,
    )

    assert reading.pay_fit.value == "at or above your CAD annual floor"
    assert reading.pay_fit.note == "the posting states this CAD annual pay range"


def test_pay_reads_an_em_dash_range() -> None:
    """A publisher's em dash separates figures the same way a hyphen would."""
    parameters = JobSearchParameters(
        locations=[Location(key="ca", country="Canada")],
        compensation=[
            CompensationBand(location="ca", currency="CAD", basis="annual", minimum=Decimal(120000))
        ],
    )

    reading = read(
        _posting("The annual range is CAD 150000—CAD 190000."),
        _inventory(),
        parameters=parameters,
    )

    assert reading.pay_fit.value == "at or above your CAD annual floor"
    assert reading.pay_fit.note == "the posting states this CAD annual pay range"


def test_pay_reads_unpunctuated_thousands_under_a_base_salary_label() -> None:
    """Boards that omit comma grouping still quote pay this reader must read."""
    parameters = JobSearchParameters(
        locations=[Location(key="ca", country="Canada")],
        compensation=[
            CompensationBand(location="ca", currency="CAD", basis="annual", minimum=Decimal(120000))
        ],
    )

    reading = read(
        _posting("Base salary: CAD 150000 - CAD 190000 per year."),
        _inventory(),
        parameters=parameters,
    )

    assert reading.pay_fit.value == "at or above your CAD annual floor"


def test_pay_reads_a_trailing_k_range() -> None:
    """Short-form thousands like the K suffix count once multiplied out."""
    parameters = JobSearchParameters(
        locations=[Location(key="us", country="United States")],
        compensation=[
            CompensationBand(location="us", currency="USD", basis="annual", minimum=Decimal(120000))
        ],
    )

    reading = read(
        _posting("Base pay: USD 150K-USD 190K per year."),
        _inventory(),
        parameters=parameters,
    )

    assert reading.pay_fit.value == "at or above your USD annual floor"
    assert reading.pay_fit.note == "the posting states this USD annual pay range"


def test_pay_reads_a_per_hour_rate() -> None:
    """A per-hour quote resolves to the hourly basis and compares to an hourly band."""
    parameters = JobSearchParameters(
        locations=[Location(key="us", country="United States")],
        compensation=[
            CompensationBand(location="us", currency="USD", basis="hourly", minimum=Decimal(50))
        ],
    )

    reading = read(
        _posting("The hourly rate is USD 60 to USD 85/hr."),
        _inventory(),
        parameters=parameters,
    )

    assert reading.pay_fit.value == "at or above your USD hourly floor"
    assert reading.pay_fit.note == "the posting states this USD hourly pay range"


def test_pay_prefers_the_declared_currency_on_a_cross_currency_line() -> None:
    """A note that quotes two currencies picks the one the workspace declared."""
    parameters = JobSearchParameters(
        locations=[
            Location(key="ca", country="Canada"),
            Location(key="us", country="United States"),
        ],
        compensation=[
            CompensationBand(location="us", currency="USD", basis="annual", minimum=Decimal(120000))
        ],
    )

    reading = read(
        _posting(
            "Fully remote in the United States.",
            "Base salary: CAD 200K (US equivalent USD 145K) per year.",
        ),
        _inventory(),
        parameters=parameters,
    )

    assert reading.pay_fit.value == "at or above your USD annual floor"


def test_pay_reads_an_annualized_label() -> None:
    """The word ``annualized`` marks a line as pay even without ``salary``."""
    parameters = JobSearchParameters(
        locations=[Location(key="us", country="United States")],
        compensation=[
            CompensationBand(location="us", currency="USD", basis="annual", minimum=Decimal(120000))
        ],
    )

    reading = read(
        _posting("Annualized: USD 150000 - USD 190000."),
        _inventory(),
        parameters=parameters,
    )

    assert reading.pay_fit.value == "at or above your USD annual floor"
    assert reading.pay_fit.note == "the posting states this USD annual pay range"
