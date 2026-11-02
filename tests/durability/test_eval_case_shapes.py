"""Case files can state the requirement shapes the match and extraction prompts rule on."""

from __future__ import annotations

import pytest
from evals.case import CASES, Case, Expectations, Responses, ScriptedSpec, load
from evals.runner import execute
from evals.scorers import CaseRun, Score, score
from evals.support.inventory import synthetic_inventory

from tailorcv.application.tailoring.stages.jobspec import Dimension, HardGate, Importance, JobSpec
from tailorcv.application.tailoring.stages.match import Classification, Strength
from tailorcv.shared.costs import Ledger

CONDITIONAL = "If this role is remote, you must reside in a state where the employer employs staff"
ABSOLUTE = "Must be authorized to work in the United States without sponsorship"


def _scored(run: CaseRun, name: str) -> Score:
    return next(item for item in score(run) if item.name == name)


def _gated(conditional_blocks: bool) -> CaseRun:
    """Score a run whose extraction marked the conditional gate as ``conditional_blocks``."""
    spec = JobSpec(
        title="Senior Data Engineer",
        company="Globex",
        seniority=None,
        location=None,
        requirements=(),
        keywords=(),
        hard_gates=(
            HardGate(kind="work_authorization", text=ABSOLUTE, blocking=True),
            HardGate(kind="location", text=CONDITIONAL, blocking=conditional_blocks),
        ),
        origin="test",
    )
    case = Case(
        name="gates",
        proves="a conditional gate is found and never blocking",
        posting="synthetic",
        responses=Responses(jobspec=ScriptedSpec(title="Senior Data Engineer")),
        expect=Expectations(
            hard_gates=(ABSOLUTE, CONDITIONAL),
            blocking_gates=(ABSOLUTE,),
            nonblocking_gates=(CONDITIONAL,),
        ),
    )
    return CaseRun(case=case, inventory=synthetic_inventory(), ledger=Ledger(), spec=spec)


@pytest.mark.parametrize("conditional_blocks", [False, True])
def test_a_gate_the_case_declares_non_blocking_fails_when_extraction_blocks_on_it(
    conditional_blocks: bool,
) -> None:
    """The blocking-gate score says nothing about a gate that should not block."""
    run = _gated(conditional_blocks)

    kept = _scored(run, "nonblocking_gates_kept")

    assert _scored(run, "blocking_gates_found").ok
    assert kept.ok is not conditional_blocks
    assert kept.value == (0.0 if conditional_blocks else 1.0)


def _match_request(run: CaseRun) -> str:
    return next(
        sent for stage, sent in zip(run.calls, run.prompts, strict=True) if stage == "match"
    )


def test_a_case_marks_a_requirement_behavioral_and_matching_is_asked_it_that_way() -> None:
    run = execute(load(CASES / "match_disposition_behavioral.toml"))

    assert run.spec is not None
    dimensions = {item.text: item.dimension for item in run.spec.requirements}
    assert dimensions["High energy"] is Dimension.BEHAVIORAL
    assert dimensions["Experience building data pipelines in Python"] is Dimension.HARD
    assert "(must_have, behavioral) High energy" in _match_request(run)


def test_a_case_asserted_status_line_reaches_the_match_request() -> None:
    case = load(CASES / "match_rule_shapes.toml")
    run = execute(case)

    assert case.asserted
    assert all(line in _match_request(run) for line in case.asserted)
    assert _scored(run, "asserted_lines_unsent").value == 0


def _violations(case: Case) -> set[str]:
    return {item.name for item in score(execute(case)) if not item.ok}


def _rematched(case: Case, requirement: str, **changes: object) -> Case:
    """Return ``case`` with one scripted match row changed."""
    rows = tuple(
        row.model_copy(update=changes) if row.requirement == requirement else row
        for row in case.responses.match
    )
    return case.model_copy(update={"responses": case.responses.model_copy(update={"match": rows})})


def _retyped(case: Case, texts: tuple[str, ...], importance: Importance) -> Case:
    """Return ``case`` with the scripted extraction typing ``texts`` as ``importance``."""
    spec = case.responses.jobspec
    requirements = tuple(
        item.model_copy(update={"importance": importance}) if item.text in texts else item
        for item in spec.requirements
    )
    retyped = spec.model_copy(update={"requirements": requirements})
    return case.model_copy(
        update={"responses": case.responses.model_copy(update={"jobspec": retyped})}
    )


INCLUDING = "Experience with data platforms, including Snowflake, Databricks, and BigQuery"
YEARS = "3+ years of experience with Python"
AIRFLOW = "Airflow for pipeline scheduling"


def test_the_rule_shapes_case_scores_clean() -> None:
    assert _violations(load(CASES / "match_rule_shapes.toml")) == set()


@pytest.mark.parametrize(
    ("requirement", "changes"),
    [
        (
            INCLUDING,
            {"classification": Classification.GAP, "strength": Strength.NONE, "evidence": ()},
        ),
        (YEARS, {"classification": Classification.TAILORING_ONLY, "strength": Strength.MODERATE}),
        (YEARS, {"strength": Strength.WEAK}),
        (
            AIRFLOW,
            {
                "classification": Classification.TRANSFERABLE,
                "strength": Strength.MODERATE,
                "evidence": ("orchestration",),
            },
        ),
    ],
)
def test_each_rule_shape_claim_fails_when_the_answer_breaks_it(
    requirement: str, changes: dict[str, object]
) -> None:
    case = _rematched(load(CASES / "match_rule_shapes.toml"), requirement, **changes)

    assert "classification_agreement" in _violations(case)


def test_the_unstanced_qualifications_case_scores_clean() -> None:
    assert _violations(load(CASES / "jobspec_unstanced_qualifications.toml")) == set()


@pytest.mark.parametrize(
    ("texts", "importance", "violated"),
    [
        (("Familiarity with Python",), Importance.NICE_TO_HAVE, "must_have_accuracy"),
        (("Hands-on SQL experience",), Importance.MUST_HAVE, "nice_to_have_accuracy"),
        (("Working knowledge of dbt",), Importance.MUST_HAVE, "nice_to_have_accuracy"),
    ],
)
def test_an_unstanced_line_typed_against_its_place_fails_the_case(
    texts: tuple[str, ...], importance: Importance, violated: str
) -> None:
    case = _retyped(load(CASES / "jobspec_unstanced_qualifications.toml"), texts, importance)

    assert violated in _violations(case)


def test_an_asserted_line_no_match_request_carries_is_counted() -> None:
    case = load(CASES / "match_rule_shapes.toml")
    run = CaseRun(
        case=case,
        inventory=synthetic_inventory(),
        ledger=Ledger(),
        calls=("jobspec", "match"),
        prompts=("posting", "requirements without the asserted block"),
    )

    unsent = _scored(run, "asserted_lines_unsent")

    assert unsent.value == len(case.asserted)
    assert not unsent.ok
