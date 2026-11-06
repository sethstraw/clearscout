"""The draft, cover, and critic evaluation families score through the shipped verifiers.

Each scripted task drives the shipped stage against a canned provider and grades
the run on what ``verify.check``, ``verify.check_letter``, and
``verify.critic_finding`` returned. These cases prove the offline mode needs no
credential, that a planted defect still fails the check it names, and that the
live mode refuses to spend without the complete approved run identity.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest
from evals.inspect import runner
from evals.inspect.labeling import bytes_sha256
from evals.inspect.scorers.document_checks import (
    critic_contract,
    critic_doubt_contract,
    verifier_contract,
    verifier_result,
)
from evals.inspect.solvers import clearscout_live, document_live
from evals.inspect.solvers.clearscout_live import prompt_snapshot_fingerprint
from evals.inspect.solvers.document_scripted import (
    cover_scripted_solver,
    critic_scripted_solver,
    draft_scripted_solver,
)
from evals.support.documents import DocumentSampleError, document_context, letter_result

from clearscout.application.config import ProviderName, Settings, Stage
from clearscout.application.prompts import load
from clearscout.application.tailoring.runs import settings_hash
from clearscout.application.tailoring.stages.draft import CoverLetter, DraftedLine
from clearscout.application.tailoring.stages.verify.report import KeywordCoverage, Report
from clearscout.infrastructure.persistence.database import Database
from clearscout.shared.errors import ConfigError

TASKS = Path(__file__).resolve().parents[2] / "evals" / "inspect" / "tasks"
FIXTURES = Path(__file__).resolve().parents[2] / "evals" / "fixtures"

PROVIDER_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")


def _cases(name: str) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _case(name: str, sample_id: str) -> dict[str, Any]:
    return next(case for case in _cases(name) if case["id"] == sample_id)


def _reworded(sample: dict[str, Any], old: str, new: str) -> dict[str, Any]:
    """Return the sample with one phrase rewritten throughout its canned draft."""
    rewritten = copy.deepcopy(sample)
    canned = json.dumps(rewritten["metadata"]["scripted_draft"]).replace(old, new)
    rewritten["metadata"]["scripted_draft"] = json.loads(canned)
    return rewritten


def _failed_checks(output: dict[str, Any]) -> list[str]:
    """Return the checks the shipped verifier failed the attempt on."""
    return [finding["check"] for finding in output["findings"] if finding["outcome"] == "fail"]


def _header(log: Path) -> dict[str, Any]:
    header: dict[str, Any] = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    return header


@pytest.mark.parametrize(
    ("task", "dataset", "total"),
    [
        ("draft_scripted_task.py", "draft_cases.jsonl", 14),
        ("cover_scripted_task.py", "cover_cases.jsonl", 17),
        ("critic_scripted_task.py", "critic_cases.jsonl", 33),
    ],
)
def test_a_scripted_document_task_runs_offline_and_every_case_scores_as_declared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task: str, dataset: str, total: int
) -> None:
    """No credential, no network, and the declared outcome for every case."""
    for key in PROVIDER_KEYS:
        monkeypatch.delenv(key, raising=False)

    log = runner.run_task(str(TASKS / task), log_dir=tmp_path / "logs")

    header = _header(log)
    assert header["dataset"].endswith(dataset)
    assert (header["total"], header["passed"]) == (total, total)


@pytest.mark.parametrize("dataset", ["draft_cases.jsonl", "cover_cases.jsonl"])
def test_every_case_in_a_document_family_measures_its_own_posting(dataset: str) -> None:
    """One posting repeated would report N named cases and measure one trial."""
    cases = _cases(dataset)

    postings = {json.dumps(case["job_spec"], sort_keys=True) for case in cases}

    assert len(postings) == len(cases)


def _drafted_lines(case: dict[str, Any]) -> list[str]:
    draft = case["metadata"]["scripted_draft"]
    roles = draft["roles"]
    return sorted(
        [draft["summary"]["text"], *(skill["text"] for skill in draft["skills"])]
        + [role["context"]["text"] for role in roles if role.get("context")]
        + [bullet["text"] for role in roles for bullet in role["bullets"]]
    )


def _letter_sentences(case: dict[str, Any]) -> list[list[str]]:
    paragraphs = case["metadata"]["scripted_letter"]["paragraphs"]
    return [re.split(r"(?<=\.) ", paragraph["text"]) for paragraph in paragraphs]


def test_a_planted_critic_case_differs_from_its_clean_control_by_its_planted_line() -> None:
    """The document is the variable here, so a differing verdict is the one planted line."""
    cases = {case["id"]: case for case in _cases("critic_cases.jsonl")}

    planted = [case for case in cases.values() if "planted" in case["metadata"]]
    draft_controls = {
        "platform_engineer.md": cases["strong_verdict"],
        "hybrid_career.md": cases["clean_hybrid_control"],
    }

    assert any("scripted_letter" in case["metadata"] for case in planted)
    for case in planted:
        line = case["metadata"]["planted"]["line"]
        if "scripted_draft" in case["metadata"]:
            control = draft_controls.get(case["metadata"]["cv"])
            assert control is not None, case["id"]
            assert control["metadata"]["cv"] == case["metadata"]["cv"], case["id"]
            assert case["job_spec"] == control["job_spec"], case["id"]
            assert _drafted_lines(case) == sorted([*_drafted_lines(control), line]), case["id"]
            continue
        control = cases["letter_duties_listed"]
        assert case["job_spec"] == control["job_spec"], case["id"]
        changed = [
            (before, after)
            for kept, written in zip(
                _letter_sentences(control), _letter_sentences(case), strict=True
            )
            for before, after in zip(kept, written, strict=True)
            if before != after
        ]
        [(before, after)] = changed
        assert after == line, case["id"]
        assert line.startswith(before.removesuffix(".")), case["id"]


def test_the_draft_log_names_which_checks_failed_and_how_often(tmp_path: Path) -> None:
    """The header summary is what a reader acts on, not the per-sample rows."""
    log = runner.run_task(str(TASKS / "draft_scripted_task.py"), log_dir=tmp_path / "logs")

    assert _header(log)["failed_checks"] == {
        "duration scope": 1,
        "first person": 1,
        "must-have coverage": 1,
        "numbers traced": 3,
        "sources cited": 1,
    }


def test_the_log_counts_warned_checks_the_pass_mark_leaves_out(tmp_path: Path) -> None:
    """The pass mark is keyed on failures, so a warning reaches a reader only here."""
    log = runner.run_task(str(TASKS / "cover_scripted_task.py"), log_dir=tmp_path / "logs")

    header = _header(log)
    assert header["passed"] == header["total"]
    assert header["warned_checks"] == {
        "factual review": 17,
        "letter closing": 1,
        "letter echo": 2,
    }


def test_an_ownership_claim_the_evidence_does_not_carry_is_warned_and_counted(
    tmp_path: Path,
) -> None:
    """The one support check a resume case plants without failing the attempt."""
    log = runner.run_task(str(TASKS / "draft_scripted_task.py"), log_dir=tmp_path / "logs")

    assert _header(log)["warned_checks"]["ownership verbs"] == 2


def test_the_critic_log_counts_the_outcomes_the_shipped_floors_assigned(tmp_path: Path) -> None:
    """The critic family reports a distribution to read, not an invented pass rate."""
    log = runner.run_task(str(TASKS / "critic_scripted_task.py"), log_dir=tmp_path / "logs")

    assert _header(log)["critic_outcomes"] == {"fail": 21, "warn": 3, "pass": 9}


def test_a_planted_uncited_line_fails_provenance_and_its_untraceable_span() -> None:
    """The failing checks come from the shipped verifier, not from the sample's own opinion."""
    sample = _case("draft_cases.jsonl", "uncited_summary")

    result = verifier_contract()(sample, draft_scripted_solver()(sample))

    assert result["value"] is True
    # The line cites nothing, so the span it states is traceable to nothing either.
    assert result["failed_checks"] == ["numbers traced", "sources cited"]


def test_a_defect_the_verifier_stops_catching_fails_the_draft_contract() -> None:
    """A check that silently starts passing is the regression this family exists to catch."""
    sample = _case("draft_cases.jsonl", "invented_figure")
    output = draft_scripted_solver()(sample)
    silenced = copy.deepcopy(output)
    for finding in silenced["findings"]:
        if finding["check"] == "numbers traced":
            finding["outcome"] = "pass"

    result = verifier_contract()(sample, silenced)

    assert result["value"] is False
    assert "numbers traced" in result["explanation"]


def test_a_letter_that_repeats_its_evidence_warns_without_failing() -> None:
    """A letter echo is a warning in the product, and the sample states it as one."""
    sample = _case("cover_cases.jsonl", "echoed_evidence")

    result = verifier_contract()(sample, cover_scripted_solver()(sample))

    assert result["value"] is True
    assert result["failed_checks"] == []
    assert "letter echo" in result["warned_checks"]


def test_a_warned_check_carries_its_reason_into_the_score() -> None:
    """A warned check named alone leaves a reader nothing to diagnose it with."""
    sample = _case("cover_cases.jsonl", "echoed_evidence")

    result = verifier_result()(sample, cover_scripted_solver()(sample))

    assert "letter echo" in result["checks"]["warned"]
    assert "consecutive words of cited evidence" in result["checks"]["warned_detail"]


def test_a_failed_check_carries_its_reason_into_the_score() -> None:
    """The failed-check detail keeps the key and the meaning its readers expect."""
    sample = _case("draft_cases.jsonl", "invented_figure")

    result = verifier_result()(sample, draft_scripted_solver()(sample))

    assert result["checks"]["failed"] == ["numbers traced"]
    assert "absent from cited evidence" in result["checks"]["detail"]
    assert "numbers traced" not in result["checks"]["warned_detail"]


def test_a_forbidden_phrase_written_into_a_draft_fails_the_forbidden_check() -> None:
    """The fixtures configure forbidden phrases, so a draft that writes one is rejected."""
    sample = _case("draft_cases.jsonl", "clean_attempt")
    planted = _reworded(sample, "schema governance", "results-driven schema governance")

    assert "forbidden strings" not in _failed_checks(draft_scripted_solver()(sample))
    assert "forbidden strings" in _failed_checks(draft_scripted_solver()(planted))


def test_a_draft_that_drops_the_required_phrase_fails_the_required_check() -> None:
    """The required phrase is one the evidence carries, so a draft can leave it out."""
    sample = _case("draft_cases.jsonl", "clean_attempt")
    planted = _reworded(sample, "schema registry", "registry")

    assert "required strings" not in _failed_checks(draft_scripted_solver()(sample))
    assert "required strings" in _failed_checks(draft_scripted_solver()(planted))


def test_a_draft_respelling_a_protected_phrase_fails_the_protected_check() -> None:
    """The protected phrase fires on any casing, and only the configured spelling passes."""
    sample = _case("draft_cases.jsonl", "clean_attempt")
    planted = _reworded(sample, "PostgreSQL", "Postgresql")

    assert "protected strings" not in _failed_checks(draft_scripted_solver()(sample))
    assert "protected strings" in _failed_checks(draft_scripted_solver()(planted))


def test_a_letter_respelling_a_protected_phrase_fails_the_protected_check() -> None:
    """The letter verifier runs the same string rules the resume is judged against."""
    sample = _case("cover_cases.jsonl", "clean_letter")
    planted = copy.deepcopy(sample)
    paragraphs = planted["metadata"]["scripted_letter"]["paragraphs"]
    paragraphs[0]["text"] = paragraphs[0]["text"] + " The warehouse runs on Postgresql."

    assert "protected strings" not in _failed_checks(cover_scripted_solver()(sample))
    assert "protected strings" in _failed_checks(cover_scripted_solver()(planted))


def test_a_critic_that_misses_a_planted_claim_fails_its_row() -> None:
    """A planted claim is only caught if the critic actually doubts it."""
    sample = _case("critic_cases.jsonl", "planted_misattribution")

    quiet = critic_doubt_contract()(
        sample, {"completion": "pass", "verdict": {"unsupported_claims": []}}
    )

    assert sample["expect"]["unsupported_claim"] is True
    assert quiet["value"] is False


def test_a_doubt_about_some_other_line_does_not_catch_a_planted_claim() -> None:
    """A planted row measures the planted line, so a doubt elsewhere is still a miss."""
    sample = _case("critic_cases.jsonl", "planted_absent_from_record")
    elsewhere = {"boundary": "adjacent", "claim": "\u201cModeled the reporting warehouse\u201d"}
    on_the_line = {
        "boundary": "absent_from_record",
        "claim": "\u201cowned the on-call rotation\u201d",
    }

    missed = critic_doubt_contract()(
        sample, {"completion": "fail", "verdict": {"unsupported_claims": [elsewhere]}}
    )
    caught = critic_doubt_contract()(
        sample, {"completion": "fail", "verdict": {"unsupported_claims": [on_the_line]}}
    )

    assert missed["value"] is False
    assert caught["value"] is True


def test_a_doubt_inside_the_planted_line_under_another_boundary_does_not_catch_it() -> None:
    """The restatement half of a planted letter sentence is true, so doubting it is a miss."""
    sample = _case("critic_cases.jsonl", "planted_letter_duty_with_credit")
    restated = {
        "boundary": "miscited",
        "claim": "“The first duty is operating the streaming ingestion core and its feeds”",
    }
    credit = {"boundary": "absent_from_record", "claim": "“I owned its on-call rotation”"}

    missed = critic_doubt_contract()(
        sample, {"completion": "fail", "verdict": {"unsupported_claims": [restated]}}
    )
    caught = critic_doubt_contract()(
        sample, {"completion": "fail", "verdict": {"unsupported_claims": [credit]}}
    )

    assert missed["value"] is False
    assert caught["value"] is True


def test_a_boundary_the_case_accepts_catches_its_planted_line() -> None:
    """A case may list a second boundary that reads the same defect equally well."""
    sample = copy.deepcopy(_case("critic_cases.jsonl", "planted_absent_from_record"))
    doubt = {"boundary": "individual_ownership", "claim": "“owned the on-call rotation”"}
    output = {"completion": "fail", "verdict": {"unsupported_claims": [doubt]}}

    assert critic_doubt_contract()(sample, output)["value"] is False
    sample["metadata"]["planted"]["accepted_boundaries"] = ["individual_ownership"]
    assert critic_doubt_contract()(sample, output)["value"] is True


@pytest.mark.parametrize(
    ("sample_id", "boundary", "claim"),
    [
        (
            "planted_scope_widening",
            "team_contribution",
            "“Took the platform team's registry cutover from plan to release”"
            " credits the candidate with the team's cutover.",
        ),
        (
            "boundary_designed_as_shipped",
            "planned_not_shipped",
            "The registry entry says it was designed; “rolled it out to every producer team”"
            " says it took effect.",
        ),
        (
            "planted_letter_blanket_fit",
            "miscited",
            "“I have done each of these duties” rests on an entry that does not"
            " carry the warehouse or the modules.",
        ),
        (
            "clean_tenure_from_summary",
            "formal_title_or_tenure",
            "“Across twelve years the work moved from reporting pipelines to platform"
            " architecture” sits under one employer and claims twelve years there.",
        ),
    ],
)
def test_a_planted_line_is_caught_under_the_boundary_its_cited_evidence_reaches(
    sample_id: str, boundary: str, claim: str
) -> None:
    """Each case's label follows what its cited entries carry, not the defect it was built for."""
    sample = _case("critic_cases.jsonl", sample_id)
    doubt = {"boundary": boundary, "claim": claim}

    scored = critic_doubt_contract()(
        sample, {"completion": "fail", "verdict": {"unsupported_claims": [doubt]}}
    )

    assert scored["value"] is True


@pytest.mark.parametrize(
    ("claim", "caught"),
    [
        (
            "“operating the streaming ingestion core and its feeds ... and I have done"
            " each of these duties.” Rebuilding a tier is not operating the core.",
            True,
        ),
        (
            "“operating the streaming ingestion core … owned its on-call"
            " rotation” is not in the record.",
            False,
        ),
        (
            "“I have done each of these duties ... operating the streaming ingestion"
            " core and its feeds” reorders the letter.",
            False,
        ),
    ],
)
def test_a_quotation_elided_inside_the_planted_line_catches_it_only_when_every_part_is_there(
    claim: str, caught: bool
) -> None:
    """A critic shortens a long quote with an ellipsis, so each kept part must be the line's."""
    sample = _case("critic_cases.jsonl", "planted_letter_blanket_fit")
    doubt = {"boundary": "adjacent", "claim": claim}

    scored = critic_doubt_contract()(
        sample, {"completion": "fail", "verdict": {"unsupported_claims": [doubt]}}
    )

    assert scored["value"] is caught


def test_a_doubt_quoting_one_term_of_the_planted_line_does_not_catch_it() -> None:
    """A lone tool name recurs in lines nobody doubted, so it does not locate the claim."""
    sample = _case("critic_cases.jsonl", "planted_absent_from_record")
    term = {"boundary": "adjacent", "claim": "\u201con Kafka\u201d is not in the record."}

    scored = critic_doubt_contract()(
        sample, {"completion": "fail", "verdict": {"unsupported_claims": [term]}}
    )

    assert scored["value"] is False


def test_a_critic_that_doubts_a_clean_document_fails_its_row() -> None:
    """Flooding is the other failure, so a doubt on a clean case cannot score green."""
    sample = _case("critic_cases.jsonl", "clean_joint_citation")

    flooded = critic_doubt_contract()(
        sample,
        {
            "completion": "fail",
            "verdict": {"unsupported_claims": [{"boundary": "miscited", "claim": "x"}]},
        },
    )

    assert sample["expect"]["unsupported_claim"] is False
    assert flooded["value"] is False
    assert flooded["metadata"]["observed_boundaries"] == ["miscited"]


def test_a_career_span_kept_in_the_summary_is_a_clean_row_a_tenure_doubt_fails() -> None:
    """The placement rule reaches lines under a role, so a span in the summary stays clean."""
    sample = _case("critic_cases.jsonl", "clean_tenure_in_summary")
    draft = sample["metadata"]["scripted_draft"]
    doubt = {
        "boundary": "formal_title_or_tenure",
        "claim": "“Across twelve years the work moved from reporting pipelines to platform"
        " architecture” claims twelve years.",
    }

    scored = critic_doubt_contract()(
        sample, {"completion": "fail", "verdict": {"unsupported_claims": [doubt]}}
    )

    assert "twelve years" in draft["summary"]["text"]
    assert not any("years" in b["text"] for role in draft["roles"] for b in role["bullets"])
    assert sample["expect"] == {"critic_outcome": "pass", "unsupported_claim": False}
    assert scored["value"] is False


@pytest.mark.parametrize(
    ("field", "message"),
    [("unsupported_claim", "unsupported_claim"), ("critic_outcome", "critic_outcome")],
)
def test_an_undeclared_critic_row_stops_the_run_before_any_solver_call(
    field: str, message: str
) -> None:
    """A live critic call is billed, so an unscoreable row must fail before the first one."""
    sample = _case("critic_cases.jsonl", "strong_verdict")
    undeclared = copy.deepcopy(sample)
    del undeclared["expect"][field]
    dataset = "\n".join(json.dumps(row) for row in (sample, undeclared)).encode()
    called: list[str] = []

    def solve(row: dict[str, Any]) -> dict[str, Any]:
        called.append(row["id"])
        return {"completion": "pass", "verdict": {"unsupported_claims": []}}

    with pytest.raises(ValueError, match=message):
        runner._score_rows(dataset, solve, critic_doubt_contract())
    assert called == []


@pytest.mark.parametrize(("completion", "agrees"), [("warn", False), ("pass", True)])
def test_an_ordinary_low_axis_on_a_clean_document_still_scores_green(
    completion: str, agrees: bool
) -> None:
    """The six axes are a judgment, so the outcome is reported beside the pass mark, not in it."""
    sample = _case("critic_cases.jsonl", "strong_verdict")

    scored = critic_doubt_contract()(
        sample, {"completion": completion, "verdict": {"human_voice": 3, "unsupported_claims": []}}
    )

    assert scored["value"] is True
    assert scored["metadata"]["expected_critic_outcome"] == "pass"
    assert scored["metadata"]["outcome_matches"] is agrees


def test_a_planted_claim_states_no_figure_the_cited_evidence_lacks() -> None:
    """A planted line carrying a figure would be caught by numbers traced, not the critic."""
    planted = [case for case in _cases("critic_cases.jsonl") if "planted" in case["metadata"]]

    assert planted
    for case in planted:
        assert not re.search(r"\d", case["metadata"]["planted"]["line"])


def _doubting(sample: dict[str, Any], quote: str, boundary: str) -> dict[str, Any]:
    """Return the sample with its canned verdict doubting one claim under ``quote``."""
    doubted = copy.deepcopy(sample)
    doubted["metadata"]["scripted_verdict"]["unsupported_claims"] = [
        {"claim": "Modeled the reporting warehouse", "boundary": boundary, "evidence_quote": quote}
    ]
    return doubted


@pytest.mark.parametrize(
    ("quote", "boundary", "misquoted"),
    [
        ("Led the Mars colony relocation program", "adjacent", 1),
        ("", "adjacent", 1),
        ("", "absent_from_record", 0),
        ("Modeled the clinical reporting warehouse", "adjacent", 0),
    ],
)
def test_a_critic_row_counts_a_doubt_whose_quote_the_record_does_not_hold(
    quote: str, boundary: str, misquoted: int
) -> None:
    """A quote the CV lacks fails the row, unless the doubt says the record is silent."""
    sample = _doubting(_case("critic_cases.jsonl", "adequate_quality"), quote, boundary)

    for scorer in (critic_contract(), critic_doubt_contract()):
        scored = scorer(sample, critic_scripted_solver()(sample))

        assert scored["metadata"]["misquoted_doubts"] == misquoted
        if misquoted:
            assert scored["value"] is False


def test_a_live_critic_row_without_a_verdict_is_refused() -> None:
    """A missing verdict is the one thing this family cannot score."""
    with pytest.raises(ValueError, match="no verdict"):
        critic_contract()({"expect": {"critic_outcome": "pass"}}, {"completion": "pass"})


def test_a_live_critic_row_with_an_unknown_outcome_is_refused() -> None:
    """An outcome outside the shipped set would score against a label nothing defines."""
    with pytest.raises(ValueError, match="unknown shipped outcome"):
        critic_contract()(
            {"expect": {"critic_outcome": "pass"}},
            {"completion": "excellent", "verdict": {"alignment": 4}},
        )


def test_the_critic_family_records_the_verdict_beside_the_shipped_outcome() -> None:
    """A verdict a reader judges, and the outcome the shipped floors give it."""
    sample = _case("critic_cases.jsonl", "adequate_quality")

    result = critic_contract()(sample, critic_scripted_solver()(sample))

    assert result["value"] is True
    assert result["critic"]["outcome"] == "warn"
    assert result["critic"]["verdict"]["alignment"] == 3


def test_a_sample_bound_to_another_parse_of_the_cv_is_refused() -> None:
    """The frozen CV identity is what makes two runs of one sample comparable."""
    sample = {**_case("draft_cases.jsonl", "clean_attempt"), "cv_hash": "0000000000000000"}

    with pytest.raises(DocumentSampleError, match="cv_hash"):
        document_context(sample)


def test_a_sample_naming_a_cv_outside_the_tracked_set_is_refused() -> None:
    """A document evaluation reads a tracked synthetic CV, never a path a sample chose."""
    sample = copy.deepcopy(_case("draft_cases.jsonl", "clean_attempt"))
    sample["metadata"]["cv"] = "../../tests/fixtures/master_cv.md"

    with pytest.raises(DocumentSampleError, match="tracked evaluation CV"):
        document_context(sample)


def test_the_solver_never_sees_the_checks_its_sample_declares() -> None:
    """A declared failure is scorer-only material; reaching the solver would leak the answer."""
    projected = runner._solver_input(_case("draft_cases.jsonl", "uncited_summary"))

    assert "expect" not in projected
    assert set(projected["metadata"]) == {"cv", "evidence", "matches", "scripted_draft"}


@pytest.mark.parametrize(
    ("task", "stage"),
    [
        ("draft_live_task.py", Stage.DRAFT),
        ("draft_redraft_live_task.py", Stage.DRAFT),
        ("cover_live_task.py", Stage.COVER),
        ("cover_redraft_live_task.py", Stage.COVER),
        ("critic_live_task.py", Stage.CRITIC),
        ("match_live_task.py", Stage.MATCH),
    ],
)
def test_a_live_task_declares_the_stage_its_family_actually_runs(task: str, stage: Stage) -> None:
    """``scripts/eval.sh`` estimates this declaration, so it has to be the run's own stage."""
    module = runner._load_task_module(str(TASKS / task))

    assert Stage(module.STAGE) is stage
    if stage is not Stage.MATCH:
        assert stage in document_live.FAMILIES


def test_a_live_document_run_refuses_without_explicit_paid_consent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live families spend money, so an absent approval stops the run before any call."""
    monkeypatch.delenv("CLEARSCOUT_EVAL_LIVE_APPROVED_CENTS", raising=False)

    with pytest.raises(RuntimeError, match="paid-run consent"):
        document_live.document_live_solver(Stage.DRAFT, FIXTURES / "draft_cases.jsonl")


def test_a_live_document_run_refuses_a_stage_no_family_owns() -> None:
    """A stage with no document family would otherwise read another family's prompts."""
    with pytest.raises(RuntimeError, match="no document family"):
        document_live.document_live_solver(Stage.MATCH, FIXTURES / "draft_cases.jsonl")


@pytest.mark.parametrize(
    ("variable", "value", "field", "shipped"),
    [
        ("CLEARSCOUT_EVAL_LIVE_MODEL", "gpt-6-luna", "model", "gpt-6.1-sol"),
        ("CLEARSCOUT_EVAL_LIVE_EFFORT", "high", "effort", "low"),
    ],
)
def test_a_live_document_run_sends_the_choice_the_run_names_to_its_stage_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    durable_database_url: str,
    variable: str,
    value: str,
    field: str,
    shipped: str,
) -> None:
    """The paid stage takes the named choice and records it; other stages keep theirs."""
    from evals.support import client as eval_client

    from clearscout.infrastructure.persistence.models.workspaces import Workspace
    from tests.support.doubles import FakeProvider

    database = Database(durable_database_url)
    try:
        with database.session() as session:
            session.add(Workspace(slug="ws", name="Workspace"))
    finally:
        database.dispose()
    monkeypatch.setenv("CLEARSCOUT_EVAL_DATABASE_URL", durable_database_url)
    monkeypatch.setenv("CLEARSCOUT_EVAL_LIVE_APPROVED_CENTS", "100")
    monkeypatch.setenv("CLEARSCOUT_EVAL_LIVE_PROVIDER", "openai")
    monkeypatch.setenv(variable, value)
    monkeypatch.setenv("CLEARSCOUT_EVAL_DATASET_SPLIT", "development")
    monkeypatch.setenv("CLEARSCOUT_EVAL_SAMPLE_LIMIT", "5")
    monkeypatch.setenv("CLEARSCOUT_EVAL_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(clearscout_live, "credential_for", lambda provider: "test-key")
    monkeypatch.setattr(eval_client, "provider_for", lambda provider, key: FakeProvider())
    built: list[Any] = []

    class StoppedError(Exception):
        pass

    def observed_client(*args: Any, **kwargs: Any) -> Any:
        built.append(eval_client.build_live_client(*args, **kwargs).settings)
        raise StoppedError

    monkeypatch.setattr(clearscout_live, "build_live_client", observed_client)
    solver = document_live.document_live_solver(Stage.DRAFT, FIXTURES / "draft_cases.jsonl")

    try:
        with pytest.raises(StoppedError):
            solver(_cases("draft_cases.jsonl")[0])
    finally:
        solver.database.dispose()

    assert solver.run_metadata[f"requested_{field}"] == value
    assert solver.run_metadata["settings_hash"] != settings_hash(
        Settings(provider=ProviderName.OPENAI)
    )
    assert getattr(built[0].choice_for_stage(Stage.DRAFT), field) == value
    assert getattr(built[0].choice_for_stage(Stage.CRITIC), field) == shipped


@pytest.mark.parametrize(
    ("effort", "error", "message"),
    [
        ("turbo", RuntimeError, "no effort level called 'turbo'"),
        ("minimal", ConfigError, "gpt-6.1-sol has no effort level called 'minimal'"),
    ],
)
def test_a_live_document_run_refuses_an_effort_the_model_does_not_have(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    effort: str,
    error: type[Exception],
    message: str,
) -> None:
    """An effort outside the vocabulary or the model's own ladder stops the run before any call."""
    monkeypatch.setenv("CLEARSCOUT_EVAL_LIVE_APPROVED_CENTS", "100")
    monkeypatch.setenv("CLEARSCOUT_EVAL_LIVE_PROVIDER", "openai")
    monkeypatch.setenv("CLEARSCOUT_EVAL_LIVE_EFFORT", effort)
    monkeypatch.setenv("CLEARSCOUT_EVAL_DATASET_SPLIT", "development")
    monkeypatch.setenv("CLEARSCOUT_EVAL_SAMPLE_LIMIT", "5")
    monkeypatch.setenv("CLEARSCOUT_EVAL_OUTPUT_DIR", str(tmp_path))

    with pytest.raises(error, match=message):
        document_live.document_live_solver(Stage.CRITIC, FIXTURES / "draft_cases.jsonl")


def test_a_live_critic_row_keeps_what_each_draw_doubted_flagged_and_advised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, durable_database_url: str
) -> None:
    """Comparing efforts means reading each draw's own doubts, not only the merged counts."""
    from dataclasses import replace

    from evals.support import client as eval_client

    from clearscout.infrastructure.persistence.models.workspaces import Workspace
    from tests.support.doubles import ScriptedProvider

    database = Database(durable_database_url)
    try:
        with database.session() as session:
            session.add(Workspace(slug="ws", name="Workspace"))
    finally:
        database.dispose()
    monkeypatch.setenv("CLEARSCOUT_EVAL_DATABASE_URL", durable_database_url)
    monkeypatch.setenv("CLEARSCOUT_EVAL_LIVE_APPROVED_CENTS", "100")
    monkeypatch.setenv("CLEARSCOUT_EVAL_LIVE_PROVIDER", "openai")
    monkeypatch.setenv("CLEARSCOUT_EVAL_DATASET_SPLIT", "development")
    monkeypatch.setenv("CLEARSCOUT_EVAL_SAMPLE_LIMIT", "5")
    monkeypatch.setenv("CLEARSCOUT_EVAL_OUTPUT_DIR", str(tmp_path))
    scores = {
        "alignment": 4,
        "preservation": 4,
        "specificity": 3,
        "readability": 4,
        "ats_clarity": 4,
        "human_voice": 3,
    }
    answers = [
        {
            **scores,
            "unsupported_claims": [
                {
                    "claim": "Led a team of twelve analysts.",
                    "evidence_quote": "Worked on platform architecture within a team",
                    "boundary": "team_contribution",
                }
            ],
            "style_flags": ["stacked adjectives"],
            "weaknesses": ["The summary repeats the headline."],
            "guidance": "Name the pipeline the reporting work ran on.",
        },
        {
            **scores,
            "preservation": 5,
            "unsupported_claims": [],
            "style_flags": ["passive opener"],
            "weaknesses": ["No outcome for the migration."],
            "guidance": "Lead with the migration outcome.",
        },
    ]
    calls = iter(answers)
    provider = ScriptedProvider(script={"CriticVerdict": lambda _schema: next(calls)})
    monkeypatch.setattr(clearscout_live, "credential_for", lambda provider_name: "test-key")
    monkeypatch.setattr(eval_client, "provider_for", lambda provider_name, key: provider)
    shipped = document_live.document_live_solver(Stage.CRITIC, FIXTURES / "critic_cases.jsonl")
    # Two draws keep the answer order scripted; later draws are otherwise asked together.
    solver = replace(shipped, settings=shipped.settings.model_copy(update={"critic_trials": 2}))

    try:
        output = solver(_case("critic_cases.jsonl", "adequate_quality"))
    finally:
        solver.database.dispose()

    assert output["draws"] == answers
    assert output["verdict"]["unsupported_claims"][0]["corroboration"] == 1


def _scripted_draft_payload(sample: dict[str, Any], scripted: dict[str, Any]) -> dict[str, Any]:
    """Render a canned resume as the drafting schema receives it for this sample's plan."""
    from evals.support.documents import drafted_payload

    from clearscout.application.tailoring.stages.draft import skills_are_headed

    context = document_context(sample)
    entries = [context.inventory.by_id(entry_id) for entry_id in context.plan.skill_ids]
    return drafted_payload(scripted, context, labelled=not skills_are_headed(entries))


def _redraft_solver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
    answers: list[dict[str, Any]],
) -> tuple[Any, Any]:
    """Return a two-attempt live draft solver whose provider answers ``answers`` in order."""
    from evals.inspect.tasks import draft_redraft_live_task
    from evals.support import client as eval_client

    from clearscout.infrastructure.persistence.models.workspaces import Workspace
    from tests.support.doubles import ScriptedProvider

    database = Database(database_url)
    try:
        with database.session() as session:
            session.add(Workspace(slug="ws", name="Workspace"))
    finally:
        database.dispose()
    monkeypatch.setenv("CLEARSCOUT_EVAL_DATABASE_URL", database_url)
    monkeypatch.setenv("CLEARSCOUT_EVAL_LIVE_APPROVED_CENTS", "100")
    monkeypatch.setenv("CLEARSCOUT_EVAL_LIVE_PROVIDER", "openai")
    monkeypatch.setenv("CLEARSCOUT_EVAL_DATASET_SPLIT", "development")
    monkeypatch.setenv("CLEARSCOUT_EVAL_SAMPLE_LIMIT", "14")
    monkeypatch.setenv("CLEARSCOUT_EVAL_OUTPUT_DIR", str(tmp_path))
    calls = iter(answers)
    provider = ScriptedProvider(script={"ResumeDraft": lambda _schema: next(calls)})
    monkeypatch.setattr(clearscout_live, "credential_for", lambda provider_name: "test-key")
    monkeypatch.setattr(eval_client, "provider_for", lambda provider_name, key: provider)
    return draft_redraft_live_task.solver(), provider


def test_a_redrafting_live_run_rewrites_a_failed_attempt_through_the_production_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, durable_database_url: str
) -> None:
    """Attempt one's failing checks reach attempt two as feedback; the kept attempt scores."""
    sample = _case("draft_cases.jsonl", "production_sized_invented_figure")
    clean = _case("draft_cases.jsonl", "production_sized_clean")["metadata"]["scripted_draft"]
    answers = [
        _scripted_draft_payload(sample, sample["metadata"]["scripted_draft"]),
        _scripted_draft_payload(sample, clean),
    ]
    solver, provider = _redraft_solver(tmp_path, monkeypatch, durable_database_url, answers)

    try:
        output = solver(sample)
    finally:
        solver.database.dispose()

    assert solver.run_metadata["draft_attempts"] == 2
    assert [call[0] for call in provider.calls] == ["ConstrainedResumeDraft"] * 2
    assert "1,900" in provider.calls[1][1].task.text
    assert [(item["attempt"], item["kept"]) for item in output["attempts"]] == [
        (1, False),
        (2, True),
    ]
    score = verifier_result(redrafting=True)(sample, output)
    assert score["value"] is True
    assert score["metadata"]["attempt_one"] == {
        "attempt": 1,
        "passed": False,
        "failed_checks": ["numbers traced"],
    }
    assert score["metadata"]["final"] == {"attempt": 2, "passed": True, "failed_checks": []}


def test_a_redrafting_live_run_stops_after_a_clean_first_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, durable_database_url: str
) -> None:
    sample = _case("draft_cases.jsonl", "production_sized_clean")
    answers = [_scripted_draft_payload(sample, sample["metadata"]["scripted_draft"])]
    solver, provider = _redraft_solver(tmp_path, monkeypatch, durable_database_url, answers)

    try:
        output = solver(sample)
    finally:
        solver.database.dispose()

    assert len(provider.calls) == 1
    score = verifier_result(redrafting=True)(sample, output)
    assert score["value"] is True
    assert score["metadata"]["attempts_used"] == 1
    assert score["metadata"]["attempt_one"]["passed"] is True


def test_the_critic_family_takes_no_second_attempt() -> None:
    with pytest.raises(RuntimeError, match="does not redraft"):
        document_live.document_live_solver(
            Stage.CRITIC, FIXTURES / "critic_cases.jsonl", draft_attempts=2
        )


def _scripted_letter_payload(sample: dict[str, Any], scripted: dict[str, Any]) -> dict[str, Any]:
    """Render a canned letter as the letter schema receives it for this sample's plan."""
    from evals.support.documents import letter_payload, research_sources

    return letter_payload(scripted, document_context(sample), research_sources(sample))


def _cover_redraft_provider(
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
    tmp_path: Path,
    answers: list[dict[str, Any]],
) -> Any:
    """Route the live cover family to a provider that answers ``answers`` in order."""
    from evals.support import client as eval_client

    from clearscout.infrastructure.persistence.models.workspaces import Workspace
    from tests.support.doubles import ScriptedProvider

    database = Database(database_url)
    try:
        with database.session() as session:
            session.add(Workspace(slug="ws", name="Workspace"))
    finally:
        database.dispose()
    monkeypatch.setenv("CLEARSCOUT_EVAL_DATABASE_URL", database_url)
    monkeypatch.setenv("CLEARSCOUT_EVAL_LIVE_APPROVED_CENTS", "100")
    monkeypatch.setenv("CLEARSCOUT_EVAL_LIVE_PROVIDER", "openai")
    monkeypatch.setenv("CLEARSCOUT_EVAL_DATASET_SPLIT", "development")
    monkeypatch.setenv("CLEARSCOUT_EVAL_SAMPLE_LIMIT", "16")
    monkeypatch.setenv("CLEARSCOUT_EVAL_OUTPUT_DIR", str(tmp_path))
    calls = iter(answers)
    provider = ScriptedProvider(script={"CoverLetter": lambda _schema: next(calls)})
    monkeypatch.setattr(clearscout_live, "credential_for", lambda provider_name: "test-key")
    monkeypatch.setattr(eval_client, "provider_for", lambda provider_name, key: provider)
    return provider


def test_a_redrafting_cover_run_rewrites_a_failed_letter_through_the_production_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, durable_database_url: str
) -> None:
    """Attempt one's failing checks and its letter reach attempt two; the kept attempt scores."""
    from evals.inspect.tasks import cover_redraft_live_task

    sample = _case("cover_cases.jsonl", "production_sized_letter_invented_figure")
    planted = sample["metadata"]["scripted_letter"]
    corrected = json.loads(json.dumps(planted).replace("1,900", "1,400"))
    answers = [
        _scripted_letter_payload(sample, planted),
        _scripted_letter_payload(sample, corrected),
    ]
    provider = _cover_redraft_provider(monkeypatch, durable_database_url, tmp_path, answers)
    solver = cover_redraft_live_task.solver()

    try:
        output = solver(sample)
    finally:
        solver.database.dispose()

    assert solver.run_metadata["draft_attempts"] == 2
    assert [call[0] for call in provider.calls] == ["ConstrainedCoverLetter"] * 2
    first, second = (call[1].task.text for call in provider.calls)
    kept_paragraph = "Litware gives clinic owners a morning view of the day before"
    assert "1,900" in second
    assert kept_paragraph in second
    assert kept_paragraph not in first
    assert [(item["attempt"], item["kept"]) for item in output["attempts"]] == [
        (1, False),
        (2, True),
    ]
    assert "1,400 columns" in output["document"]["text"]
    score = cover_redraft_live_task.scorer()(sample, output)
    assert score["value"] is True
    assert score["metadata"]["attempt_one"] == {
        "attempt": 1,
        "passed": False,
        "failed_checks": ["letter numbers"],
    }
    assert score["metadata"]["final"] == {"attempt": 2, "passed": True, "failed_checks": []}


def test_a_redrafting_cover_run_stops_after_a_clean_first_letter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, durable_database_url: str
) -> None:
    from evals.inspect.tasks import cover_redraft_live_task

    sample = _case("cover_cases.jsonl", "production_sized_letter_clean")
    answers = [_scripted_letter_payload(sample, sample["metadata"]["scripted_letter"])]
    provider = _cover_redraft_provider(monkeypatch, durable_database_url, tmp_path, answers)
    solver = cover_redraft_live_task.solver()

    try:
        output = solver(sample)
    finally:
        solver.database.dispose()

    assert len(provider.calls) == 1
    score = cover_redraft_live_task.scorer()(sample, output)
    assert score["value"] is True
    assert score["metadata"]["attempts_used"] == 1
    assert score["metadata"]["attempt_one"]["passed"] is True


def test_the_cover_redraft_task_passes_the_smoke_stage_before_its_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    durable_database_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The smoke reads the attempts the redraft scorer requires, and is reused as the trial."""
    from decimal import Decimal

    from evals.inspect.tasks import cover_redraft_live_task

    sample = _cases("cover_cases.jsonl")[0]
    answers = [_scripted_letter_payload(sample, sample["metadata"]["scripted_letter"])] * 2
    provider = _cover_redraft_provider(monkeypatch, durable_database_url, tmp_path, answers)
    monkeypatch.setenv("CLEARSCOUT_EVAL_SAMPLE_LIMIT", "1")
    monkeypatch.setenv("CLEARSCOUT_EVAL_TRIALS", "2")

    log = runner.run_task(
        cover_redraft_live_task.__name__,
        log_dir=tmp_path,
        limit=1,
        split="development",
        trials=2,
        smoke_approved_usd=Decimal(1),
    )

    assert f"smoke: ok case={sample['id']}" in capsys.readouterr().out
    assert len(provider.calls) == 2
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()[1:]]
    assert [row["score"]["value"] for row in rows] == [True, True]
    assert [row["score"]["metadata"]["attempts_used"] for row in rows] == [1, 1]


def test_the_redraft_scorer_requires_the_attempts_its_smoke_reads() -> None:
    assert verifier_result(redrafting=True).required_fields == ("findings", "attempts")
    assert verifier_result().required_fields == ("findings",)


def test_the_production_sized_draft_cases_carry_ten_requirements_and_keywords() -> None:
    for case_id in ("production_sized_clean", "production_sized_invented_figure"):
        spec = _case("draft_cases.jsonl", case_id)["job_spec"]
        musts = [item for item in spec["requirements"] if item["importance"] == "must_have"]
        assert len(spec["requirements"]) >= 10, case_id
        assert len(musts) >= 8, case_id
        assert spec["keywords"], case_id


def test_the_production_sized_cover_cases_carry_production_length_inputs() -> None:
    """A production letter request carries a full posting, a careers page, and ~30 requirements."""
    for case_id in ("production_sized_letter_clean", "production_sized_letter_invented_figure"):
        case = _case("cover_cases.jsonl", case_id)
        requirements = case["job_spec"]["requirements"]
        musts = [item for item in requirements if item["importance"] == "must_have"]
        assert len(requirements) >= 25, case_id
        assert len(musts) >= 10, case_id
        assert case["job_spec"]["keywords"], case_id
        assert len(case["input"]) >= 5000, case_id
        assert len(case["metadata"]["research"][0]["text"]) >= 3500, case_id


def test_a_live_letter_critique_is_asked_as_the_product_asks_about_a_letter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, durable_database_url: str
) -> None:
    """A letter is critiqued by paragraph, beside the company research its paragraphs cite."""
    from dataclasses import replace

    from evals.support import client as eval_client

    from clearscout.infrastructure.persistence.models.workspaces import Workspace
    from tests.support.doubles import ScriptedProvider

    database = Database(durable_database_url)
    try:
        with database.session() as session:
            session.add(Workspace(slug="ws", name="Workspace"))
    finally:
        database.dispose()
    monkeypatch.setenv("CLEARSCOUT_EVAL_DATABASE_URL", durable_database_url)
    monkeypatch.setenv("CLEARSCOUT_EVAL_LIVE_APPROVED_CENTS", "100")
    monkeypatch.setenv("CLEARSCOUT_EVAL_LIVE_PROVIDER", "openai")
    monkeypatch.setenv("CLEARSCOUT_EVAL_DATASET_SPLIT", "development")
    monkeypatch.setenv("CLEARSCOUT_EVAL_SAMPLE_LIMIT", "5")
    monkeypatch.setenv("CLEARSCOUT_EVAL_OUTPUT_DIR", str(tmp_path))
    sample = _case("critic_cases.jsonl", "letter_company_paragraph")
    verdict = sample["metadata"]["scripted_verdict"]
    provider = ScriptedProvider(script={"CriticVerdict": lambda _schema: verdict})
    monkeypatch.setattr(clearscout_live, "credential_for", lambda provider_name: "test-key")
    monkeypatch.setattr(eval_client, "provider_for", lambda provider_name, key: provider)
    shipped = document_live.document_live_solver(Stage.CRITIC, FIXTURES / "critic_cases.jsonl")
    solver = replace(shipped, settings=shipped.settings.model_copy(update={"critic_trials": 1}))

    try:
        output = solver(runner._solver_input(sample))
    finally:
        solver.database.dispose()

    [(_, request)] = provider.calls
    research = sample["metadata"]["research"][0]
    assert 0 <= request.task.text.find(sample["input"]) < request.task.text.find(research["text"])
    assert "cover letter" in request.instructions.text
    assert "Paragraph 3, source_ids: company.careers" in request.task.text
    assert research["text"] in request.task.text
    assert output["finding"]["check"] == "letter critic"
    assert output["completion"] == sample["expect"]["critic_outcome"]


def test_a_live_cover_letter_is_written_beside_the_posting_then_the_company_research(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, durable_database_url: str
) -> None:
    """Production researches a letter from the stored posting first, so the eval does too."""
    from evals.support import client as eval_client
    from evals.support.documents import LETTER_SCHEMA_KEY, letter_payload, research_sources

    from clearscout.infrastructure.persistence.models.workspaces import Workspace
    from tests.support.doubles import ScriptedProvider

    database = Database(durable_database_url)
    try:
        with database.session() as session:
            session.add(Workspace(slug="ws", name="Workspace"))
    finally:
        database.dispose()
    monkeypatch.setenv("CLEARSCOUT_EVAL_DATABASE_URL", durable_database_url)
    monkeypatch.setenv("CLEARSCOUT_EVAL_LIVE_APPROVED_CENTS", "100")
    monkeypatch.setenv("CLEARSCOUT_EVAL_LIVE_PROVIDER", "openai")
    monkeypatch.setenv("CLEARSCOUT_EVAL_DATASET_SPLIT", "development")
    monkeypatch.setenv("CLEARSCOUT_EVAL_SAMPLE_LIMIT", "5")
    monkeypatch.setenv("CLEARSCOUT_EVAL_OUTPUT_DIR", str(tmp_path))
    sample = runner._solver_input(_case("critic_cases.jsonl", "letter_company_paragraph"))
    answer = letter_payload(
        sample["metadata"]["scripted_letter"], document_context(sample), research_sources(sample)
    )
    provider = ScriptedProvider(script={LETTER_SCHEMA_KEY: lambda _schema: answer})
    monkeypatch.setattr(clearscout_live, "credential_for", lambda provider_name: "test-key")
    monkeypatch.setattr(eval_client, "provider_for", lambda provider_name, key: provider)
    solver = document_live.document_live_solver(Stage.COVER, FIXTURES / "cover_cases.jsonl")

    try:
        result = solver(sample)
    finally:
        solver.database.dispose()

    [(_, request)] = provider.calls
    text = request.task.text
    research = sample["metadata"]["research"][0]
    provenance = next(f for f in result["findings"] if f["check"] == "letter provenance")
    assert provenance["outcome"] == "pass", provenance["detail"]
    assert 0 <= text.find("company.posting") < text.find(research["id"])
    assert 0 <= text.find(sample["input"]) < text.find(research["text"])


def test_a_live_document_run_binds_its_stage_prompts_and_dataset_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, durable_database_url: str
) -> None:
    """The log has to prove which prompts and which dataset the money was spent on."""
    from clearscout.infrastructure.persistence.models.workspaces import Workspace

    database = Database(durable_database_url)
    try:
        with database.session() as session:
            session.add(Workspace(slug="ws", name="Workspace"))
    finally:
        database.dispose()
    monkeypatch.setenv("CLEARSCOUT_EVAL_DATABASE_URL", durable_database_url)
    monkeypatch.setenv("CLEARSCOUT_EVAL_LIVE_APPROVED_CENTS", "100")
    monkeypatch.setenv("CLEARSCOUT_EVAL_LIVE_PROVIDER", "openai")
    monkeypatch.setenv("CLEARSCOUT_EVAL_DATASET_SPLIT", "development")
    monkeypatch.setenv("CLEARSCOUT_EVAL_SAMPLE_LIMIT", "5")
    monkeypatch.setenv("CLEARSCOUT_EVAL_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("CLEARSCOUT_CRITIC_TRIALS", "3")
    dataset = FIXTURES / "draft_cases.jsonl"

    solver = document_live.document_live_solver(Stage.CRITIC, dataset)

    try:
        metadata = solver.run_metadata
        assert metadata["stage"] == "critic"
        assert metadata["critic_trials"] == 3
        assert metadata["prompt_suite_fingerprint"] == prompt_snapshot_fingerprint(
            document_live.CRITIC_PROMPTS,
            {name: load(name) for name in document_live.CRITIC_PROMPTS},
        )
        assert metadata["dataset_sha256"] == bytes_sha256(dataset.read_bytes())
        assert metadata["sample_limit"] == 5
        assert "selection_manifest_sha256" not in metadata
    finally:
        solver.database.dispose()


@pytest.mark.parametrize("keep_text", [True, False])
def test_a_letter_result_carries_the_body_only_when_the_run_keeps_it(keep_text: bool) -> None:
    """Live receipts sit in a private directory and keep the letter; scripted logs do not."""
    letter = CoverLetter(
        greeting="Dear hiring team,",
        paragraphs=(DraftedLine(text="Adatum serves regional carriers."),),
        closing="Sincerely,",
    )
    report = Report(findings=(), coverage=KeywordCoverage(present=(), missing=()))

    document = letter_result(letter, report, keep_text=keep_text)["document"]

    assert document["words"] == len(letter.rendered().split())
    assert document.get("text") == (letter.rendered() if keep_text else None)
