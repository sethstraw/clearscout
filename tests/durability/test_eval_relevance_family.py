"""The relevance-review evaluation family scores the recommendation and the four axes.

These cases prove the support module composes the shipped call rather than a
copy of it, that an axis flip fails a row the recommendation alone would pass,
that the frozen labels never reach a solver, and that the live mode refuses to
spend without the complete approved run identity.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from evals.inspect import runner
from evals.inspect.scorers.relevance_verdict import recommendation_match
from evals.inspect.solvers import clearscout_live, relevance_live
from evals.inspect.solvers.clearscout_live import PromptArm
from evals.inspect.solvers.relevance_live import (
    RELEVANCE_PROMPTS,
    relevance_live_solver,
)
from evals.support.relevance import (
    AXES,
    RelevanceSampleError,
    relevance_case,
    relevance_result,
    run_schema,
    stated_conditions,
)
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select

from clearscout.application.config import ProviderName, Settings
from clearscout.application.discovery.relevance_review import (
    RelevanceReviewItemFailure,
    RelevanceReviewService,
    current_review_contract,
    review_batches,
    review_plan,
)
from clearscout.application.job_search.models import RelevanceReviewBatchResponse
from clearscout.application.master_cv.cv import parse_cv
from clearscout.application.master_cv.service import MasterCvService
from clearscout.application.prompts import load, overriding, packaged
from clearscout.application.tailoring.stages.context import citable_entries
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models.tailoring import CostRecord, ModelCall
from clearscout.infrastructure.providers.base import (
    CallRecord,
    Finish,
    UnusableResponseError,
    refuse_unusable,
    wire_schema,
)
from clearscout.infrastructure.providers.plan import RequestPlan, schema_digest
from clearscout.shared.costs import CallManifest, Ledger, Usage
from tests.support.artifacts import runtime_storage
from tests.support.scenarios import FIXTURES, workspace_with_retained_postings

POSTING_ID = "rrp-900"

BODY = (
    "We are hiring a senior platform engineer to keep our payment ingestion "
    "pipeline reliable.\n\n"
    "You will own reconciliation batch scheduling and the on-call rotation for "
    "settlements.\n\n"
    "This role is remote within Canada."
)


def _sample(**overrides: Any) -> dict[str, Any]:
    sample: dict[str, Any] = {
        "id": POSTING_ID,
        "split": "development",
        "cv_hash": "51703d56c2a53475",
        "input": {
            "search_context": "Places this person may work: Calgary, Alberta, Canada",
            "posting": {
                "id": POSTING_ID,
                "employer": "Globex",
                "title": "Senior Platform Engineer",
                "body": BODY,
            },
        },
        "expected_recommendation": "review",
        "expected_axes": {
            "core_work_alignment": "direct",
            "core_qualification_alignment": "established",
            "hard_condition": "passes",
            "decisive_basis": "core_match",
        },
        "axes_confidence": "high",
    }
    sample.update(overrides)
    return sample


def _answer(span_id: str) -> dict[str, Any]:
    return {
        "pending_discovery_id": POSTING_ID,
        "summary": "Payment platform reliability work this person has done.",
        "core_work_alignment": "direct",
        "core_qualification_alignment": "established",
        "hard_condition": "passes",
        "decisive_basis": "core_match",
        "posting_evidence_span_ids": [span_id],
        "master_cv_evidence_ids": ["syn-e1"],
        "recommendation": "review",
    }


def _solver_output(**overrides: Any) -> dict[str, Any]:
    output: dict[str, Any] = {
        "completion": "review",
        "axes": {
            "core_work_alignment": "direct",
            "core_qualification_alignment": "established",
            "hard_condition": "passes",
            "decisive_basis": "core_match",
        },
        "posting_evidence_span_ids": [f"{POSTING_ID}:span-0001"],
        "master_cv_evidence_ids": ["syn-e1"],
        "summary": "A direct match.",
        "unresolved_questions": [],
        "validation_failure": None,
        "outcome": "judged",
    }
    output.update(overrides)
    return output


def _insufficient_sample(**overrides: Any) -> dict[str, Any]:
    """The frozen row shape for a posting that states no one role."""
    return _sample(
        expected_recommendation="insufficient_evidence",
        expected_axes={
            "core_work_alignment": "unresolved",
            "core_qualification_alignment": "unresolved",
            "hard_condition": "unresolved",
            "decisive_basis": "insufficient_posting",
        },
        **overrides,
    )


def _insufficient_output() -> dict[str, Any]:
    """The shape `_validate_one` returns for an insufficient-posting answer."""
    return _solver_output(
        completion="UNKNOWN",
        outcome="insufficient_posting_text",
        axes=dict.fromkeys(AXES),
        summary="The page lists several openings and states no one role.",
    )


def test_a_case_composes_the_shipped_relevance_plan() -> None:
    """The evaluation renders the shipped wiring, not a hand-written stand-in."""
    case = relevance_case(_sample())

    plan = review_plan(Settings(), case.inventory, case.search_context, case.posting_block)

    assert plan.evidence is not None
    assert [plan.instructions.prompt, plan.evidence.prompt, plan.task.prompt] == list(
        RELEVANCE_PROMPTS
    )
    assert "Senior Platform Engineer" in plan.task.text


def test_a_case_refuses_a_citation_of_another_postings_span() -> None:
    """The schema leaves span ids open, so the shipped validation is what refuses a foreign one."""
    case = relevance_case(_sample())
    own = [span.id for span in case.spans[POSTING_ID]]
    assert len(own) > 1

    accepted = case.schema.model_validate({"recommendations": [_answer(own[1])]})
    foreign = case.schema.model_validate({"recommendations": [_answer("rrp-901:span-0001")]})

    assert accepted.model_dump()["recommendations"][0]["posting_evidence_span_ids"] == [own[1]]
    _, failures = case.validate(RelevanceReviewBatchResponse.model_validate(foreign.model_dump()))
    assert [failure.failure_code for failure in failures] == ["wrong_posting_span"]


def test_the_recommendation_and_the_axes_together_decide_the_pass_mark() -> None:
    """A row passes when the stage reproduced the whole reviewed verdict."""
    result = recommendation_match()(_sample(), _solver_output())

    assert result["value"] is True
    assert result["metadata"]["expected"] == "review"
    assert result["metadata"]["axes_matched"] == 4


def test_a_different_recommendation_fails_the_row() -> None:
    """A recommendation the reviewers did not reach is the regression to catch."""
    result = recommendation_match()(_sample(), _solver_output(completion="unlikely"))

    assert result["value"] is False
    assert "expected=review got=unlikely" in result["explanation"]


def _rejected_output() -> dict[str, Any]:
    """The shape `relevance_result` returns for an answer local validation rejected."""
    return relevance_result(
        [], [RelevanceReviewItemFailure(POSTING_ID, "leading_excerpt_only", "cited the header")]
    )


def test_a_rejected_answer_fails_even_with_the_expected_recommendation() -> None:
    """Validation rejected the item, so nothing it said counts as a review."""
    result = recommendation_match()(_sample(), _rejected_output())

    assert result["value"] is False
    assert "leading_excerpt_only" in result["explanation"]


def test_a_rejected_answer_is_not_counted_as_four_wrong_classifications() -> None:
    """The program discarded the answer, so its empty axes state nothing about the axes."""
    result = recommendation_match()(_sample(), _rejected_output())

    assert result["metadata"]["axes"] == {}
    assert result["metadata"]["axes_matched"] is None
    assert "axes differed" not in result["explanation"]


def test_a_flipped_axis_fails_the_row_the_recommendation_alone_would_pass() -> None:
    """Several axis combinations reach one recommendation, so the verdict hides the flip."""
    axes = {**_solver_output()["axes"], "hard_condition": "unresolved"}

    result = recommendation_match()(_sample(), _solver_output(axes=axes))

    assert result["value"] is False
    assert result["metadata"]["recommendation_matches"] is True
    assert result["metadata"]["axes_scored"] is True
    assert result["metadata"]["axes_matched"] == 3
    assert result["metadata"]["axes"]["hard_condition"] == {
        "expected": "passes",
        "observed": "unresolved",
        "matches": False,
    }
    assert "axes differed: hard_condition" in result["explanation"]


def test_a_low_confidence_row_is_scored_on_its_recommendation_alone() -> None:
    """The two labeller passes split over these axes, so the reference is one adjudication."""
    axes = {**_solver_output()["axes"], "hard_condition": "unresolved"}

    result = recommendation_match()(_sample(axes_confidence="low"), _solver_output(axes=axes))

    assert result["value"] is True
    assert result["metadata"]["axes_confidence"] == "low"
    assert result["metadata"]["axes_scored"] is False
    assert result["metadata"]["axes"]["hard_condition"]["matches"] is False
    assert "recorded, not scored" in result["explanation"]


def test_a_row_with_no_review_record_is_not_a_row_recording_no_disagreement() -> None:
    """An absent provenance record and a recorded zero are different facts."""
    absent = recommendation_match()(_sample(), _solver_output())
    recorded = recommendation_match()(
        _sample(
            provenance={
                "protocol_version": "clearscout-relevance-review-development-v1",
                "family": "relevance-review",
                "rubric_version": "relevance-rubric-v1",
                "reviewer_a": "labeller-a",
                "reviewer_b": "labeller-b",
                "disagreements": [],
            }
        ),
        _solver_output(),
    )

    assert absent["metadata"]["provenance"] is None
    assert recorded["metadata"]["provenance"]["disagreement_count"] == 0


def test_a_misspelled_review_record_is_refused_rather_than_read_as_absent() -> None:
    """A dropped key would turn a recorded review into a row nobody reviewed."""
    sample = _sample(provenence={"family": "relevance-review"})

    with pytest.raises(ValueError, match="provenence"):
        recommendation_match()(sample, _solver_output())


def test_an_insufficient_posting_answer_scores_as_the_decision_it_states() -> None:
    """The program nulls the recommendation for this outcome, so the outcome carries it."""
    result = recommendation_match()(_insufficient_sample(), _insufficient_output())

    assert result["value"] is True
    assert result["metadata"]["recommendation_matches"] is True


def test_nulled_axes_are_not_counted_against_an_insufficient_posting_answer() -> None:
    """Four nulls are the program's own doing, not four wrong classifications."""
    result = recommendation_match()(_insufficient_sample(), _insufficient_output())

    assert result["metadata"]["axes_matched"] is None
    assert result["metadata"]["axes_scored"] is None
    assert result["metadata"]["axes"] == {}
    assert "nulls the axes" in result["explanation"]


def test_an_insufficient_outcome_still_fails_a_row_expecting_a_judged_verdict() -> None:
    """The outcome mapping must not turn every nulled answer into a pass."""
    result = recommendation_match()(_sample(), _insufficient_output())

    assert result["value"] is False


@pytest.mark.parametrize("field", ["expected_recommendation", "expected_axes"])
def test_a_sample_without_a_reviewed_label_is_refused(field: str) -> None:
    """Scoring an unlabeled row would report an accuracy nobody assigned."""
    sample = _sample()
    del sample[field]

    with pytest.raises(ValueError, match=field):
        recommendation_match()(sample, _solver_output())


def test_an_unlabeled_row_stops_the_run_before_any_solver_call() -> None:
    """A live call is billed, so an unscoreable row must fail before the first one."""
    unlabeled = _sample()
    del unlabeled["expected_recommendation"]
    dataset = "\n".join(json.dumps(row) for row in (_sample(), unlabeled)).encode()
    called: list[str] = []

    def solve(sample: dict[str, Any]) -> dict[str, Any]:
        called.append(sample["id"])
        return _solver_output()

    with pytest.raises(ValueError, match="expected_recommendation"):
        runner._score_rows(dataset, solve, recommendation_match())
    assert called == []


def test_a_frozen_row_stating_a_verdict_the_stage_forbids_is_refused() -> None:
    """Axes and a recommendation that cannot co-occur make a row unscoreable."""
    sample = _sample(expected_recommendation="unlikely")

    with pytest.raises(ValueError, match="no combination the stage allows"):
        recommendation_match()(sample, _solver_output())


def test_the_solver_never_sees_the_frozen_relevance_labels() -> None:
    """The recommendation, the axes, their confidence, and the review record are scorer-only."""
    projected = runner._solver_input(_sample(provenance={"family": "relevance-review"}))

    assert set(projected) == {"id", "split", "cv_hash", "input"}


def test_a_validation_failure_shapes_an_unknown_result() -> None:
    """A rejected item still returns a row, and it cannot be mistaken for a review."""
    output = relevance_result(
        [], [RelevanceReviewItemFailure(POSTING_ID, "wrong_posting_span", "cited another posting")]
    )

    assert output["completion"] == "UNKNOWN"
    assert output["validation_failure"]["reason"] == "wrong_posting_span"
    assert output["validation_failure"]["detail"] == "cited another posting"
    assert output["axes"] == dict.fromkeys(AXES)


def test_a_rejected_row_carries_the_answer_that_was_rejected() -> None:
    """A paid rejection is only diagnosable if the row says what the model answered."""
    response = RelevanceReviewBatchResponse.model_validate(
        {"recommendations": [_answer(f"{POSTING_ID}:span-0001")]}
    )

    output = relevance_result(
        [],
        [RelevanceReviewItemFailure(POSTING_ID, "wrong_posting_span", "cited another posting")],
        response,
    )

    rejected = output["validation_failure"]["rejected_answer"]
    assert rejected["recommendation"] == "review"
    assert rejected["axes"]["decisive_basis"] == "core_match"


def test_a_rejected_row_in_a_batch_carries_its_own_answer() -> None:
    """A batch answers several postings, so a rejection must quote the one about this row."""
    other = {**_answer("rrp-901:span-0001"), "pending_discovery_id": "rrp-901"}
    response = RelevanceReviewBatchResponse.model_validate(
        {
            "recommendations": [
                other,
                {**_answer(f"{POSTING_ID}:span-0001"), "recommendation": "investigate"},
            ]
        }
    )

    output = relevance_result(
        [],
        [RelevanceReviewItemFailure(POSTING_ID, "inconsistent_verdict", "no allowed combination")],
        response,
    )

    assert output["validation_failure"]["rejected_answer"]["recommendation"] == "investigate"


class _RecordingClient:
    """Answer without a provider, recording the system prompt and schema in force for the call."""

    def __init__(
        self,
        seen: list[str],
        schemas: list[type[BaseModel]] | None = None,
        plans: list[RequestPlan] | None = None,
    ) -> None:
        self.settings = Settings()
        self._seen = seen
        self._schemas = [] if schemas is None else schemas
        self._plans = [] if plans is None else plans

    def complete(self, plan: RequestPlan, schema: type[BaseModel]) -> RelevanceReviewBatchResponse:
        """Return the empty batch answer, after noting which prompt bytes rendered it."""
        self._plans.append(plan)
        self._seen.append(load("relevance_review_system"))
        self._schemas.append(schema)
        return RelevanceReviewBatchResponse(recommendations=[])


def _arm_directory(path: Path, marker: str) -> Path:
    """Write one complete prompt version a run can declare as an arm."""
    path.mkdir(parents=True)
    for name in RELEVANCE_PROMPTS:
        (path / f"{name}.md").write_text(f"{packaged(name)}\n{marker}\n", encoding="utf-8")
    return path


def _armed_solver(
    arms: tuple[PromptArm, ...],
    monkeypatch: pytest.MonkeyPatch,
    seen: list[str],
    schemas: list[type[BaseModel]] | None = None,
    plans: list[RequestPlan] | None = None,
) -> Any:
    monkeypatch.setattr(clearscout_live, "credential_for", lambda provider_name: "token")
    monkeypatch.setattr(
        clearscout_live,
        "build_live_client",
        lambda *args, **kwargs: _RecordingClient(seen, schemas, plans),
    )
    return relevance_live._RelevanceLiveSolver(
        run_metadata={"provider": "openai", **clearscout_live.arms_metadata(arms)},
        arms=arms,
        provider_name=None,  # type: ignore[arg-type]
        settings=Settings(provider=ProviderName.OPENAI),
        ledger=Ledger(),
        database=None,  # type: ignore[arg-type]
        workspace_id="ws",
    )


def test_a_task_sends_the_schema_production_builds_for_that_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two postings under one search context share one call and production's task schema."""
    monkeypatch.delenv(clearscout_live.PROMPT_ARMS_ENV, raising=False)
    other = _sample(id="rrp-901")
    other["input"] = {
        **other["input"],
        "posting": {**other["input"]["posting"], "id": "rrp-901", "title": "Data Engineer"},
    }
    schemas: list[type[BaseModel]] = []
    solver = _armed_solver(relevance_live.declared_arms(), monkeypatch, [], schemas)

    list(solver.batch([_sample(), other]))

    cases = [relevance_case(_sample()), relevance_case(other)]
    conditions = tuple(
        dict.fromkeys(
            condition for case in cases for condition in stated_conditions(case.search_context)
        )
    )
    shipped = RelevanceReviewService.batch_schema(cases[0].inventory, conditions=conditions)
    assert [schema_digest(schema.model_json_schema()) for schema in schemas] == [
        schema_digest(shipped.model_json_schema())
    ]


def test_two_declared_arms_each_send_their_own_prompt_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One run holds both prompt versions, and the arm the runner names picks the bytes."""
    monkeypatch.setenv(
        clearscout_live.PROMPT_ARMS_ENV,
        f"baseline={_arm_directory(tmp_path / 'baseline', 'Arm A.')},"
        f"candidate={_arm_directory(tmp_path / 'candidate', 'Arm B.')}",
    )
    arms = relevance_live.declared_arms()
    seen: list[str] = []
    solver = _armed_solver(arms, monkeypatch, seen)

    first = solver({**_sample(), "arm": "baseline"})
    second = solver({**_sample(), "arm": "candidate"})

    assert [text.splitlines()[-1] for text in seen] == ["Arm A.", "Arm B."]
    assert first["metadata"]["arm"] == "baseline"
    assert second["metadata"]["arm"] == "candidate"
    metadata = clearscout_live.arms_metadata(arms)
    assert [entry["arm"] for entry in metadata["prompt_arms"]] == ["baseline", "candidate"]
    assert (
        metadata["prompt_arms"][0]["prompt_suite_fingerprint"]
        != metadata["prompt_arms"][1]["prompt_suite_fingerprint"]
    )
    assert set(metadata["prompt_arms"][0]["prompts"]) == set(RELEVANCE_PROMPTS)


def test_an_undeclared_change_to_an_arm_is_refused_before_the_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fingerprint check still stands between an approved run and other bytes."""
    directory = _arm_directory(tmp_path / "baseline", "Arm A.")
    monkeypatch.setenv(clearscout_live.PROMPT_ARMS_ENV, f"baseline={directory}")
    seen: list[str] = []
    solver = _armed_solver(relevance_live.declared_arms(), monkeypatch, seen)
    solver({**_sample(), "arm": "baseline"})

    (directory / "relevance_review_system.md").write_text(
        f"{packaged('relevance_review_system')}\nArm C.\n", encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="prompt bytes changed after run preflight"):
        solver({**_sample(), "arm": "baseline"})
    assert len(seen) == 1


def test_a_trial_naming_an_arm_the_run_never_declared_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row can only be billed to a prompt version the run bound itself to."""
    monkeypatch.setenv(
        clearscout_live.PROMPT_ARMS_ENV,
        f"baseline={_arm_directory(tmp_path / 'baseline', 'Arm A.')}",
    )
    seen: list[str] = []
    solver = _armed_solver(relevance_live.declared_arms(), monkeypatch, seen)

    with pytest.raises(RuntimeError, match="undeclared prompt arm 'candidate'"):
        solver({**_sample(), "arm": "candidate"})
    assert seen == []


def test_an_undeclared_working_tree_change_still_refuses_a_single_version_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run declaring no arm keeps the check that binds it to the shipped prompts."""
    monkeypatch.delenv(clearscout_live.PROMPT_ARMS_ENV, raising=False)
    seen: list[str] = []
    solver = _armed_solver(relevance_live.declared_arms(), monkeypatch, seen)
    solver(_sample())
    edited = {name: f"{packaged(name)}\nEdited under the run.\n" for name in RELEVANCE_PROMPTS}
    monkeypatch.setattr(clearscout_live, "load", lambda name: edited[name])

    with pytest.raises(RuntimeError, match="prompt bytes changed after run preflight"):
        solver(_sample())
    assert len(seen) == 1


def test_a_run_declaring_no_arm_records_no_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default run's rows and metadata carry nothing about arms."""
    monkeypatch.delenv(clearscout_live.PROMPT_ARMS_ENV, raising=False)
    arms = relevance_live.declared_arms()
    solver = _armed_solver(arms, monkeypatch, [])

    result = solver(_sample())

    assert clearscout_live.arms_metadata(arms) == {}
    assert "arm" not in result["metadata"]


def test_a_live_relevance_run_refuses_without_explicit_paid_consent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The live family spends money, so an absent approval stops the run before any call."""
    monkeypatch.delenv("CLEARSCOUT_EVAL_LIVE_APPROVED_CENTS", raising=False)
    dataset = tmp_path / "relevance_review.jsonl"
    dataset.write_text("", encoding="utf-8")

    with pytest.raises(RuntimeError, match="paid-run consent"):
        relevance_live_solver(dataset)


NOTE = "Referred by a former teammate on the payments team."


def test_a_kept_posting_note_reaches_the_task_as_production_renders_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row with a note sends the notes block production sends, and a row without one none."""
    monkeypatch.delenv(clearscout_live.PROMPT_ARMS_ENV, raising=False)
    noted = _sample()
    noted["input"] = {**noted["input"], "posting": {**noted["input"]["posting"], "note": NOTE}}
    plans: list[RequestPlan] = []
    solver = _armed_solver(relevance_live.declared_arms(), monkeypatch, [], plans=plans)

    solver(noted)
    solver(_sample())

    assert (
        f"DISCOVERY {POSTING_ID}\nPerson's note (kept this posting): {NOTE}" in plans[0].task.text
    )
    assert "Person's note" not in plans[1].task.text


def test_a_posting_that_yields_no_citation_span_is_refused() -> None:
    """A body with no span leaves the model nothing to cite, so no call is composed for it."""
    blank = _sample()
    blank["input"] = {**blank["input"], "posting": {**blank["input"]["posting"], "body": " \n\n "}}

    with pytest.raises(RelevanceSampleError, match="yields no citation spans"):
        run_schema((blank,))


SECOND_CONTEXT = "Places this person may work: Halifax, Nova Scotia, Canada"


def _posting(index: int, body: str = BODY, context: str | None = None) -> dict[str, Any]:
    """One synthetic row whose sample id is its posting id."""
    sample_id = f"rrp-{index:03d}"
    sample = _sample(id=sample_id)
    sample["input"] = {
        "search_context": context or sample["input"]["search_context"],
        "posting": {
            **sample["input"]["posting"],
            "id": sample_id,
            "title": f"Platform Engineer {index}",
            "body": body,
        },
    }
    return sample


def _long_body(characters: int) -> str:
    paragraph = "You will own settlement reconciliation and the on-call rotation for it."
    return "\n\n".join([paragraph] * (characters // len(paragraph) + 1))[:characters].rstrip()


def _task_ids(task: str) -> list[str]:
    """Return the posting ids one batch's task text names, in the order it names them."""
    return re.findall(r"^DISCOVERY (\S+)$", task, flags=re.MULTILINE)


class _BatchClient:
    """Answer each batch call without a provider, billing it to the ledger the call was given."""

    def __init__(self, plans: list[RequestPlan], ledger: Ledger, stops: Mapping[int, Finish]):
        self.settings = Settings()
        self._plans = plans
        self._ledger = ledger
        self._stops = stops

    def complete(self, plan: RequestPlan, schema: type[BaseModel]) -> RelevanceReviewBatchResponse:
        """Record the plan and its cost, then answer nothing or stop as scripted."""
        self._plans.append(plan)
        call = len(self._plans)
        usage = Usage(input_tokens=1_000, output_tokens=200)
        self._ledger.entries.append(
            CallManifest(
                stage="relevance_review",
                model="gpt-5.6-terra",
                usage=usage,
                dollars=0.03,
                response_id=f"resp-{call}",
            )
        )
        if call in self._stops:
            record = CallRecord(usage=usage, model="gpt-5.6-terra", finish=self._stops[call])
            refuse_unusable(record, "OpenAI", 4_000)
        return RelevanceReviewBatchResponse(recommendations=[])


def _batch_solver(
    monkeypatch: pytest.MonkeyPatch,
    samples: Sequence[dict[str, Any]],
    plans: list[RequestPlan],
    *,
    stops: Mapping[int, Finish] | None = None,
    billed: list[int] | None = None,
    arms: tuple[PromptArm, ...] | None = None,
) -> Any:
    """A live solver over ``samples`` whose calls reach a scripted client and a counted bill."""
    monkeypatch.setattr(clearscout_live, "credential_for", lambda provider_name: "token")
    monkeypatch.setattr(
        clearscout_live,
        "build_live_client",
        lambda *args, ledger, **kwargs: _BatchClient(plans, ledger, stops or {}),
    )

    def bill(*args: Any, ledger: Ledger, **kwargs: Any) -> None:
        if billed is not None:
            billed.append(len(ledger.entries))

    monkeypatch.setattr(clearscout_live, "bill_sample", bill)
    declared = arms if arms is not None else relevance_live.declared_arms()
    return relevance_live._RelevanceLiveSolver(
        run_metadata={"provider": "openai", **clearscout_live.arms_metadata(declared)},
        arms=declared,
        provider_name=ProviderName.OPENAI,
        settings=Settings(provider=ProviderName.OPENAI),
        ledger=Ledger(),
        database=None,  # type: ignore[arg-type]
        workspace_id="ws",
    )


def _capped_samples() -> list[dict[str, Any]]:
    """Twenty-one short postings, then four long ones the character cap splits early."""
    return [_posting(index) for index in range(21)] + [
        _posting(index, _long_body(50_000)) for index in range(21, 25)
    ]


def test_a_repeat_sends_the_batches_production_cuts_at_both_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Twenty-five rows under one search context are three calls, cut where production cuts."""
    monkeypatch.delenv(clearscout_live.PROMPT_ARMS_ENV, raising=False)
    samples = _capped_samples()
    plans: list[RequestPlan] = []
    solver = _batch_solver(monkeypatch, samples, plans)

    outputs = list(solver.batch(samples))

    ids = [sample["id"] for sample in samples]
    assert [_task_ids(plan.task.text) for plan in plans] == [ids[:20], ids[20:23], ids[23:]]
    bodies = {sample["id"]: len(sample["input"]["posting"]["body"]) for sample in samples}
    assert [_task_ids(plan.task.text) for plan in plans] == review_batches(ids, bodies.__getitem__)
    assert len({plan.cached_prefix for plan in plans}) == 1
    assert sorted(position for position, _ in outputs) == list(range(25))


def test_each_search_context_is_its_own_task_with_its_own_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One request carries one search context, so each context's evidence is its own prefix."""
    monkeypatch.delenv(clearscout_live.PROMPT_ARMS_ENV, raising=False)
    samples = [_posting(1), _posting(2, context=SECOND_CONTEXT), _posting(3)]
    plans: list[RequestPlan] = []
    solver = _batch_solver(monkeypatch, samples, plans)

    list(solver.batch(samples))

    assert [_task_ids(plan.task.text) for plan in plans] == [["rrp-001", "rrp-003"], ["rrp-002"]]
    evidence = [plan.evidence.text for plan in plans if plan.evidence is not None]
    assert [SECOND_CONTEXT in text for text in evidence] == [False, True]


def test_the_estimated_call_count_is_the_calls_a_run_makes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every trial sends production's batches again, and the estimate prices each one."""
    monkeypatch.delenv(clearscout_live.PROMPT_ARMS_ENV, raising=False)
    samples = _capped_samples()
    plans: list[RequestPlan] = []
    solver = _batch_solver(monkeypatch, samples, plans)

    for _trial in (0, 1):
        list(solver.batch(samples))

    assert [_task_ids(plan.task.text) for plan in plans[3:]] == [
        _task_ids(plan.task.text) for plan in plans[:3]
    ]
    assert relevance_live.review_calls(samples, trials=2) == len(plans) == 6
    assert relevance_live.review_calls(samples, trials=2, arms=2) == 12


def test_every_task_sends_one_schema_that_names_no_posting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The schema sits in the cached prefix, so tasks over different postings send the same one."""
    monkeypatch.delenv(clearscout_live.PROMPT_ARMS_ENV, raising=False)
    samples = [_posting(1), _posting(2, context=SECOND_CONTEXT), _posting(3)]
    solver = _batch_solver(monkeypatch, samples, [])

    schemas = {
        request.sample_ids: json.dumps(request.schema.model_json_schema())
        for request in solver.requests(samples)
    }

    assert list(schemas) == [("rrp-001", "rrp-003"), ("rrp-002",)]
    first, second = schemas.values()
    assert first == second
    assert "rrp-00" not in first


def test_a_batch_call_is_billed_once_and_each_member_records_its_share(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spend summed over the rows equals the one call, and every row names that call."""
    monkeypatch.delenv(clearscout_live.PROMPT_ARMS_ENV, raising=False)
    samples = [_posting(1), _posting(2), _posting(3)]
    billed: list[int] = []
    solver = _batch_solver(monkeypatch, samples, [], billed=billed)

    metadata = [output["metadata"] for _, output in solver.batch(samples)]

    assert billed == [1]
    assert [item["actual_dollars"] for item in metadata] == [pytest.approx(0.01)] * 3
    assert [len(item["measurements"]) for item in metadata] == [1, 0, 0]
    assert {json.dumps(item["batch"]) for item in metadata} == {
        json.dumps(
            {
                "index": 0,
                "size": 3,
                "sample_ids": ["rrp-001", "rrp-002", "rrp-003"],
                "call_dollars": 0.03,
                "response_ids": ["resp-1"],
                "cache_keys": [""],
            }
        )
    }


def test_every_trial_and_arm_sends_the_batch_production_would(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeats and arms compare the same batch, so only the prompt or chance differs."""
    monkeypatch.setenv(
        clearscout_live.PROMPT_ARMS_ENV,
        f"baseline={_arm_directory(tmp_path / 'baseline', 'Arm A.')},"
        f"candidate={_arm_directory(tmp_path / 'candidate', 'Arm B.')}",
    )
    samples = [_posting(1), _posting(2), _posting(3)]
    solver = _batch_solver(monkeypatch, samples, [], arms=relevance_live.declared_arms())
    dataset = "\n".join(json.dumps(sample) for sample in samples).encode()

    rows, total, _ = runner._score_rows(
        dataset,
        solver,
        lambda sample, output: {"value": False},
        trials=2,
        arms=("baseline", "candidate"),
    )

    orders = {
        (row["trial"], row["arm"]): row["solver_output"]["metadata"]["batch"]["sample_ids"]
        for row in rows
    }
    batch = ["rrp-001", "rrp-002", "rrp-003"]
    assert orders == {
        (0, "baseline"): batch,
        (1, "candidate"): batch,
        (2, "baseline"): batch,
        (3, "candidate"): batch,
    }
    assert total == 12
    assert {frozenset(row) for row in rows} == {
        frozenset({"id", "trial", "arm", "score", "solver_output"})
    }


def test_one_truncated_batch_call_fails_every_member_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The call that spent its budget decides both its rows; the other task still answers."""
    monkeypatch.delenv(clearscout_live.PROMPT_ARMS_ENV, raising=False)
    samples = [_posting(1), _posting(2), _posting(3, context=SECOND_CONTEXT)]
    solver = _batch_solver(monkeypatch, samples, [], stops={1: Finish.TRUNCATED})
    dataset = "\n".join(json.dumps(sample) for sample in samples).encode()

    rows, _, _ = runner._score_rows(
        dataset,
        solver,
        lambda sample, output: {"value": True},
        run_metadata={"stage": "relevance_review", "requested_model": "gpt-5.6-terra"},
    )

    assert len(rows) == 3
    outputs = {row["id"]: row["solver_output"] for row in rows}
    assert [outputs[name].get("failure") for name in ("rrp-001", "rrp-002", "rrp-003")] == [
        runner.OUTPUT_BUDGET_EXHAUSTED,
        runner.OUTPUT_BUDGET_EXHAUSTED,
        None,
    ]
    assert [len(outputs[name]["metadata"]["measurements"]) for name in ("rrp-001", "rrp-002")] == [
        1,
        0,
    ]
    assert [outputs[name]["metadata"]["batch"]["sample_ids"] for name in outputs] == [
        ["rrp-001", "rrp-002"],
        ["rrp-001", "rrp-002"],
        ["rrp-003"],
    ]


def test_a_batch_call_refused_for_another_reason_stops_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(clearscout_live.PROMPT_ARMS_ENV, raising=False)
    samples = [_posting(1), _posting(2)]
    solver = _batch_solver(monkeypatch, samples, [], stops={1: Finish.REFUSED})
    dataset = "\n".join(json.dumps(sample) for sample in samples).encode()

    with pytest.raises(UnusableResponseError, match="declined to answer"):
        runner._score_rows(dataset, solver, lambda sample, output: {"value": True})


def test_an_arm_whose_prompt_fails_its_placeholder_check_is_refused_when_declared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken arm stops the run before any row is rendered or billed."""
    directory = _arm_directory(tmp_path / "broken", "Arm A.")
    (directory / "relevance_review_user.md").write_text("Review these.\n", encoding="utf-8")
    monkeypatch.setenv(clearscout_live.PROMPT_ARMS_ENV, f"broken={directory}")

    with pytest.raises(RuntimeError, match="prompt arm broken: Prompt relevance_review_user"):
        relevance_live.declared_arms()


REAL_ID = "5b0e9c2a-1d4f-4c7e-9a3b-2f6d8e1c7a90"
REAL_CV = parse_cv(FIXTURES / "master_cv.md")


def _real_sample(cv_hash: str = REAL_CV.content_hash) -> dict[str, Any]:
    """A private-dataset row: a pending id for its id and the real CV's hash."""
    sample = _sample(id=REAL_ID, cv_hash=cv_hash)
    sample["input"] = {**sample["input"], "posting": {**sample["input"]["posting"], "id": REAL_ID}}
    return sample


def _dataset(path: Path, *samples: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(sample) + "\n" for sample in samples), encoding="utf-8")
    return path


def _stored_cv_version(url: str, tmp_path: Path) -> tuple[str, str]:
    """Store the fixture CV where a run resolving storage from ``url`` reads it back."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    database = Database(url)
    try:
        workspace_id, _ = workspace_with_retained_postings(database, artifacts, [])
        (version,) = MasterCvService(database, runtime_storage(database, artifacts)).versions(
            workspace_id
        )
        return version.id, version.content_sha256
    finally:
        database.dispose()


def _live_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, url: str, cv_version: str = ""
) -> None:
    """The approved-run environment scripts/eval.sh exports, for one synthetic sample."""
    for name, value in {
        "CLEARSCOUT_EVAL_LIVE_APPROVED_CENTS": "56",
        "CLEARSCOUT_EVAL_LIVE_PROVIDER": "openai",
        "CLEARSCOUT_EVAL_DATASET_SPLIT": "development",
        "CLEARSCOUT_EVAL_SAMPLE_LIMIT": "1",
        "CLEARSCOUT_EVAL_OUTPUT_DIR": str(tmp_path / "private output"),
        "CLEARSCOUT_EVAL_DATABASE_URL": url,
        relevance_live.CV_VERSION_ENV: cv_version,
    }.items():
        monkeypatch.setenv(name, value)
    for name in (
        clearscout_live.PROMPT_ARMS_ENV,
        "CLEARSCOUT_EVAL_ESTIMATED_CENTS",
        "CLEARSCOUT_EVAL_LIVE_MODEL",
        "CLEARSCOUT_EVAL_LIVE_EFFORT",
        "CLEARSCOUT_EVAL_SAMPLE_IDS",
        "CLEARSCOUT_EVAL_TRIALS",
    ):
        monkeypatch.delenv(name, raising=False)


def _real_answer(entry_id: str) -> dict[str, Any]:
    return {
        "recommendations": [
            {
                **_answer(f"{REAL_ID}:span-0001"),
                "pending_discovery_id": REAL_ID,
                "master_cv_evidence_ids": [entry_id],
            }
        ]
    }


def test_a_cv_version_run_closes_citations_over_that_cvs_entries_and_records_it(
    durable_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model may cite only the stored version's entries, and the run log names the version."""
    version_id, content_sha256 = _stored_cv_version(durable_database_url, tmp_path)
    _live_environment(monkeypatch, tmp_path, durable_database_url, version_id)
    real_ids = [entry.id for entry in citable_entries(REAL_CV)]

    solver = relevance_live_solver(_dataset(tmp_path / "private" / "cases.jsonl", _real_sample()))
    solver.database.dispose()

    [request] = solver.requests([_real_sample()])
    accepted = request.schema.model_validate(_real_answer(real_ids[0]))
    assert accepted.model_dump()["recommendations"][0]["master_cv_evidence_ids"] == [real_ids[0]]
    with pytest.raises(ValidationError):
        request.schema.model_validate(_real_answer("syn-e1"))
    assert solver.run_metadata["master_cv_version_id"] == version_id
    assert solver.run_metadata["master_cv_content_sha256"] == content_sha256
    assert solver.run_metadata["cv_hash"] == REAL_CV.content_hash


def test_a_run_naming_no_cv_version_reviews_against_the_synthetic_cv(
    durable_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stored_cv_version(durable_database_url, tmp_path)
    _live_environment(monkeypatch, tmp_path, durable_database_url)

    solver = relevance_live_solver(_dataset(tmp_path / "cases.jsonl", _sample()))
    solver.database.dispose()

    [request] = solver.requests([_sample()])
    accepted = request.schema.model_validate(
        {"recommendations": [_answer(f"{POSTING_ID}:span-0001")]}
    )
    assert accepted.model_dump()["recommendations"][0]["master_cv_evidence_ids"] == ["syn-e1"]
    assert "master_cv_version_id" not in solver.run_metadata


def test_a_cv_version_is_refused_for_a_dataset_inside_the_repository(
    durable_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tracked labels were frozen against the synthetic CV, so a real CV never reads them."""
    version_id, _ = _stored_cv_version(durable_database_url, tmp_path)
    _live_environment(monkeypatch, tmp_path, durable_database_url, version_id)
    repository = tmp_path / "repository"
    monkeypatch.setattr(clearscout_live, "_REPO_ROOT", repository)
    tracked = _dataset(repository / "evals" / "golden" / "relevance_review.jsonl", _real_sample())

    with pytest.raises(RuntimeError, match="outside the repository"):
        relevance_live_solver(tracked)


@pytest.mark.parametrize(
    ("version", "cv_hash", "message"),
    [
        ("stored", "51703d56c2a53475", "do not name cv_hash"),
        ("0b9d1f4e-0000-4000-8000-000000000000", REAL_CV.content_hash, "does not exist"),
    ],
)
def test_a_cv_version_run_refuses_rows_it_cannot_bind_to_that_cv(
    durable_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: str,
    cv_hash: str,
    message: str,
) -> None:
    """A row labelled against another CV, or a version the workspace lacks, stops the run."""
    version_id, _ = _stored_cv_version(durable_database_url, tmp_path)
    named = version_id if version == "stored" else version
    _live_environment(monkeypatch, tmp_path, durable_database_url, named)

    with pytest.raises(RuntimeError, match=message):
        relevance_live_solver(_dataset(tmp_path / "private" / "cases.jsonl", _real_sample(cv_hash)))


def _private_task(tmp_path: Path, dataset: Path) -> Path:
    task = tmp_path / "private_relevance_task.py"
    task.write_text(
        "from pathlib import Path\n"
        'STAGE = "relevance_review"\n'
        f"DATASET_PATH = Path({str(dataset)!r})\n",
        encoding="utf-8",
    )
    return task


def _refuse_provider(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("a render reached a provider")


def _spend_rows(url: str) -> int:
    database = Database(url)
    try:
        with database.session() as session:
            calls = session.scalar(select(func.count()).select_from(ModelCall))
            costs = session.scalar(select(func.count()).select_from(CostRecord))
        return int(calls or 0) + int(costs or 0)
    finally:
        database.dispose()


def test_a_render_writes_what_the_paid_run_sends_without_reaching_a_provider(
    durable_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each row and arm renders the plan and schema a paid call would send, and nothing is spent."""
    version_id, _ = _stored_cv_version(durable_database_url, tmp_path)
    _live_environment(monkeypatch, tmp_path, durable_database_url, version_id)
    monkeypatch.setenv(
        clearscout_live.PROMPT_ARMS_ENV,
        f"baseline={_arm_directory(tmp_path / 'baseline', 'Arm A.')},"
        f"candidate={_arm_directory(tmp_path / 'candidate', 'Arm B.')}",
    )
    noted = _real_sample()
    noted["input"]["posting"]["note"] = NOTE
    dataset = _dataset(tmp_path / "private" / "cases.jsonl", noted)
    monkeypatch.setattr(clearscout_live, "credential_for", _refuse_provider)
    monkeypatch.setattr(clearscout_live, "build_live_client", _refuse_provider)
    rendered = tmp_path / "rendered"

    code = relevance_live.main(
        [
            str(_private_task(tmp_path, dataset)),
            "--render-only",
            str(rendered),
            "--provider",
            "openai",
        ]
    )

    assert code == 0
    assert _spend_rows(durable_database_url) == 0
    files = {
        arm: json.loads((rendered / f"batch-000.{arm}.json").read_text())
        for arm in ("baseline", "candidate")
    }
    assert files["baseline"]["instructions_digest"] != files["candidate"]["instructions_digest"]
    assert files["baseline"]["cv_entry_ids"] == [entry.id for entry in citable_entries(REAL_CV)]
    assert files["baseline"]["sample_ids"] == [REAL_ID]
    assert files["baseline"]["posting_span_ids"][REAL_ID][0] == f"{REAL_ID}:span-0001"
    assert NOTE in files["baseline"]["task"]
    assert json.loads((rendered / "run.json").read_text())["master_cv_version_id"] == version_id
    assert {path.stat().st_mode & 0o777 for path in rendered.iterdir()} == {0o600}
    for declared in relevance_live.declared_arms():
        with overriding(declared.snapshot):
            recorded = current_review_contract().instructions_digest
        assert files[str(declared.name)]["instructions_digest"] == recorded

    plans: list[RequestPlan] = []
    schemas: list[type[BaseModel]] = []
    monkeypatch.setattr(clearscout_live, "credential_for", lambda provider_name: "token")
    monkeypatch.setattr(
        clearscout_live,
        "build_live_client",
        lambda *args, **kwargs: _RecordingClient([], schemas, plans),
    )
    solver = relevance_live_solver(dataset)
    for arm in ("baseline", "candidate"):
        solver({**noted, "arm": arm})
    solver.database.dispose()

    for (arm, stored), plan, schema in zip(files.items(), plans, schemas, strict=True):
        assert plan.evidence is not None
        assert stored["arm"] == arm
        assert stored["instructions"] == plan.instructions.text
        assert stored["instructions_digest"] == plan.instructions.digest
        assert stored["evidence"] == plan.evidence.text
        assert stored["task"] == plan.task.text
        assert stored["schema_digest"] == schema_digest(wire_schema(ProviderName.OPENAI, schema))


def test_a_render_writes_one_file_per_batch_naming_its_members(
    durable_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reviewer compares one file with one stored production batch, not one row."""
    _stored_cv_version(durable_database_url, tmp_path)
    _live_environment(monkeypatch, tmp_path, durable_database_url)
    samples = [_posting(1), _posting(2, context=SECOND_CONTEXT), _posting(3)]
    dataset = _dataset(tmp_path / "private" / "cases.jsonl", *samples)
    monkeypatch.setattr(clearscout_live, "build_live_client", _refuse_provider)
    rendered = tmp_path / "rendered"

    code = relevance_live.main(
        [
            str(_private_task(tmp_path, dataset)),
            "--render-only",
            str(rendered),
            "--provider",
            "openai",
        ]
    )

    assert code == 0
    assert sorted(path.name for path in rendered.iterdir()) == [
        "batch-000.working-tree.json",
        "batch-001.working-tree.json",
        "run.json",
    ]
    first = json.loads((rendered / "batch-000.working-tree.json").read_text())
    assert first["sample_ids"] == first["posting_ids"] == ["rrp-001", "rrp-003"]
    assert _task_ids(first["task"]) == ["rrp-001", "rrp-003"]
    assert set(first["posting_span_ids"]) == {"rrp-001", "rrp-003"}
    second = json.loads((rendered / "batch-001.working-tree.json").read_text())
    assert second["sample_ids"] == ["rrp-002"]
    assert second["search_context"] == SECOND_CONTEXT


def test_a_render_of_selected_ids_writes_only_the_batches_that_run_sends(
    durable_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proof of an --ids run shows that run's batches, not the whole dataset's."""
    _stored_cv_version(durable_database_url, tmp_path)
    _live_environment(monkeypatch, tmp_path, durable_database_url)
    samples = [_posting(1), _posting(2, context=SECOND_CONTEXT), _posting(3)]
    dataset = _dataset(tmp_path / "private" / "cases.jsonl", *samples)
    monkeypatch.setattr(clearscout_live, "build_live_client", _refuse_provider)
    rendered = tmp_path / "rendered"

    code = relevance_live.main(
        [
            str(_private_task(tmp_path, dataset)),
            "--render-only",
            str(rendered),
            "--provider",
            "openai",
            "--ids",
            "rrp-001",
            "rrp-002",
        ]
    )

    assert code == 0
    batches = [
        json.loads(path.read_text())["sample_ids"] for path in sorted(rendered.glob("batch-*"))
    ]
    assert batches == [["rrp-001"], ["rrp-002"]]


def test_a_render_refuses_a_split_no_run_could_name(tmp_path: Path) -> None:
    """A mistyped split must not render an empty proof that looks like a real one."""
    rendered = tmp_path / "rendered"

    with pytest.raises(SystemExit) as refused:
        relevance_live.main(
            [
                str(tmp_path / "task.py"),
                "--render-only",
                str(rendered),
                "--provider",
                "openai",
                "--split",
                "heldout",
            ]
        )

    assert refused.value.code == 2
    assert not rendered.exists()


@pytest.mark.parametrize("refusal", ["blank body", "output inside the repository"])
def test_a_render_that_cannot_stand_for_a_paid_run_writes_nothing(
    durable_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusal: str
) -> None:
    _stored_cv_version(durable_database_url, tmp_path)
    _live_environment(monkeypatch, tmp_path, durable_database_url)
    sample = _sample()
    rendered = tmp_path / "rendered"
    if refusal == "blank body":
        sample["input"]["posting"]["body"] = " \n\n "
    else:
        monkeypatch.setattr(clearscout_live, "_REPO_ROOT", tmp_path)
    task = _private_task(tmp_path, _dataset(tmp_path / "private" / "cases.jsonl", sample))

    code = relevance_live.main([str(task), "--render-only", str(rendered), "--provider", "openai"])

    assert code == 1
    assert not rendered.exists()
