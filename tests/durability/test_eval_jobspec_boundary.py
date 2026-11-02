"""The live jobspec boundary task: its dataset, its scorer, and its stage wiring."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from evals.inspect import runner
from evals.inspect.runner import dataset_samples
from evals.inspect.scorers.jobspec_boundary import boundary_match
from evals.inspect.solvers.jobspec_live import boundary_result, load_boundaries, posting
from evals.inspect.tasks import jobspec_boundary_live_task as task

from tailorcv.application.config import Stage
from tailorcv.application.tailoring.stages.jobspec import (
    Importance,
    JobSpecExtraction,
    RawBoundaryMatch,
    RawCoreOccupation,
    RawRequirement,
    spec_from_extraction,
)
from tests.support.doubles import FakeTaskModule


def _samples() -> list[dict[str, Any]]:
    return dataset_samples(task.DATASET_PATH, split="development")


def test_the_task_declares_the_jobspec_stage() -> None:
    assert Stage.JOBSPEC.value == task.STAGE


def test_every_case_loads_with_a_label_inside_the_boundary_list() -> None:
    boundaries = load_boundaries(task.BOUNDARIES_PATH)
    samples = _samples()
    scorer = boundary_match()

    assert len({sample["id"] for sample in samples}) == len(samples) >= 8
    for sample in samples:
        expected = scorer.validate_sample(sample)
        assert expected is None or 1 <= expected <= len(boundaries), sample["id"]
    labels = {sample["expected_boundary"] for sample in samples}
    assert labels == {None, *range(1, len(boundaries))}


@pytest.mark.parametrize(
    ("expected", "observed", "passed", "error"),
    [
        (None, None, True, None),
        (2, 2, True, None),
        (None, 2, False, "false_positive"),
        (3, None, False, "false_negative"),
        (2, 3, False, "wrong_boundary"),
    ],
)
def test_the_scorer_names_how_a_boundary_missed(
    expected: int | None, observed: int | None, passed: bool, error: str | None
) -> None:
    sample = {"id": "case", "expected_boundary": expected, "why": "synthetic"}

    score = boundary_match()(sample, {"boundary": observed})

    assert score["value"] is passed
    assert score["metadata"]["boundary_error"] == error


def test_a_row_without_a_reason_is_refused() -> None:
    with pytest.raises(ValueError, match="why"):
        boundary_match().validate_sample({"id": "case", "expected_boundary": None})


def test_a_boundary_cited_on_the_occupation_scores_as_its_number() -> None:
    boundaries = load_boundaries(task.BOUNDARIES_PATH)
    sample = next(row for row in _samples() if row["id"] == "generic-ml-platform")
    jd = posting(sample)
    raw = JobSpecExtraction(
        title="Senior ML Platform Engineer",
        requirements=[
            RawRequirement(
                text="Kubernetes in production",
                quote_or_span="Terraform and Kubernetes in production",
                importance=Importance.MUST_HAVE,
            )
        ],
        core_occupation=RawCoreOccupation(
            text="ML platform engineer", quote_or_span="Senior ML Platform Engineer"
        ),
        boundary_match=RawBoundaryMatch(quote_or_span="Senior ML Platform Engineer", boundary=2),
    )

    result = boundary_result(spec_from_extraction(raw, jd, boundaries), boundaries)

    assert result["boundary"] == 2
    assert boundary_match()(sample, result)["value"] is True


def test_the_run_header_counts_hits_per_case_and_errors_overall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted = {"generic-ml-platform": iter([2, None, 3]), "domain-ai-lead": iter([None, 2, 2])}

    def solve(sample: dict[str, Any]) -> dict[str, Any]:
        return {"boundary": next(scripted[sample["id"]]), "metadata": {}}

    module = FakeTaskModule(
        TASK_NAME=task.TASK_NAME,
        DATASET_PATH=task.DATASET_PATH,
        PRODUCT_QUESTION=task.PRODUCT_QUESTION,
        solver=lambda: solve,
        scorer=task.scorer,
    )
    monkeypatch.setattr(runner, "_load_task_module", lambda reference: module)

    log_path = runner.run_task(
        "synthetic-task",
        log_dir=tmp_path,
        ids=["domain-ai-lead", "generic-ml-platform"],
        trials=3,
    )

    header = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert header["boundary_errors"] == {
        "false_positive": 2,
        "false_negative": 1,
        "wrong_boundary": 1,
    }
    assert header["boundary_cases"] == {
        "domain-ai-lead": {"trials": 3, "boundary_hits": 2, "correct": 1},
        "generic-ml-platform": {"trials": 3, "boundary_hits": 2, "correct": 1},
    }
