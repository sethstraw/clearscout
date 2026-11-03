"""Fail-closed controls for the prompt-quality evaluation runner."""

from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from evals.inspect import build_calibration, runner
from evals.inspect.labeling import bytes_sha256
from evals.inspect.product_questions import ProductQuestion
from evals.inspect.scorers.match_classification import (
    classification_match,
    per_requirement_scripted_contract,
)
from evals.inspect.solvers import clearscout_live
from evals.inspect.solvers.clearscout_live import live_run_metadata
from evals.support import client as eval_client
from evals.support.inventory import synthetic_inventory

from clearscout.application.config import Stage
from clearscout.application.prompts import load as load_prompt
from clearscout.application.prompts import overriding, packaged, suite_fingerprint
from clearscout.application.tailoring.stages.match import (
    Classification,
    EvidenceMap,
    Match,
    Strength,
)
from clearscout.infrastructure.artifacts.runtime import RuntimeStorage
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.providers.base import (
    CallRecord,
    Finish,
    UnusableResponseError,
    refuse_unusable,
)
from clearscout.infrastructure.providers.credentials import Credential, Source
from clearscout.shared.costs import CallManifest, ServiceTier, Usage
from clearscout.shared.errors import ProviderError
from tests.support.doubles import FakeProvider, FakeTaskModule
from tests.support.suite import REPO_ROOT, SUITE_PROVIDER


def _write_dataset(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _selection_identity(dataset_sha256: str = "c" * 64, **_scope: object) -> dict[str, str]:
    return {
        "selection_manifest_path": "/private/evaluations/selection-manifest.json",
        "selection_manifest_sha256": "a" * 64,
        "selection_manifest_bytes_sha256": "b" * 64,
        "dataset_sha256": dataset_sha256,
        "review_context_sha256": "d" * 64,
        "synthetic_inventory_hash": synthetic_inventory().content_hash,
        "candidate_pool_sha256": "e" * 64,
        "extraction_qa_sha256": "f" * 64,
        "review_set_sha256": "1" * 64,
        "resolution_set_sha256": "2" * 64,
    }


def test_calibration_output_must_stay_outside_the_public_repository(tmp_path: Path) -> None:
    """Workspace-derived calibration text belongs in an explicitly private location."""
    assert (
        build_calibration._resolve_output(tmp_path / "calibration.jsonl")
        == (tmp_path / "calibration.jsonl").resolve()
    )

    with pytest.raises(ValueError, match="outside the public repository"):
        build_calibration._resolve_output(Path.cwd() / "evals" / "golden" / "calibration.jsonl")


def test_calibration_rejects_a_public_output_before_resolving_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad destination must fail before the workspace is opened."""
    monkeypatch.setattr(
        RuntimeStorage,
        "resolve",
        lambda **_kwargs: pytest.fail("storage was resolved before output validation"),
    )

    with pytest.raises(ValueError, match="outside the public repository"):
        build_calibration.build(Path("unused"), Path.cwd() / "calibration.jsonl")


def test_calibration_cli_requires_an_explicit_output() -> None:
    """Private calibration evidence has no implicit public destination."""
    with pytest.raises(SystemExit):
        build_calibration._parse_args(["--data-root", "private-data"])


def test_runner_executes_only_the_approved_count_from_the_named_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = tmp_path / "match.jsonl"
    _write_dataset(
        dataset,
        [
            {"id": "dev-1", "split": "development"},
            {"id": "holdout-1", "split": "holdout"},
            {"id": "dev-2", "split": "development"},
        ],
    )
    executed: list[str] = []

    def solve(sample: dict[str, Any]) -> dict[str, Any]:
        executed.append(str(sample["id"]))
        return {
            "completion": "GAP",
            "metadata": {"entry_keys": [], "run": {"provider": "openai"}},
        }

    task = FakeTaskModule(
        TASK_NAME="match-live",
        DATASET_PATH=dataset,
        PRODUCT_QUESTION=ProductQuestion.REQUIREMENT_CLASSIFICATION,
        solver=lambda: solve,
        scorer=lambda: lambda sample, output: {"value": True},
    )
    monkeypatch.setattr(runner, "_load_task_module", lambda reference: task)

    log_path = runner.run_task(
        "synthetic-task",
        log_dir=tmp_path / "logs",
        limit=1,
        split="development",
    )

    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert executed == ["dev-1"]
    assert records[0] == {
        "task": "match-live",
        "dataset": str(dataset),
        "product_question": "requirement_classification",
        "score_kind": "judged",
        "total": 1,
        "passed": 1,
        "accuracy": 1.0,
        "limit": 1,
        "split": "development",
        "run": {"provider": "openai"},
    }
    assert records[1]["id"] == "dev-1"


def _id_dataset(tmp_path: Path) -> Path:
    dataset = tmp_path / "ids.jsonl"
    _write_dataset(
        dataset,
        [{"id": f"dev-{index}", "split": "development"} for index in range(1, 5)],
    )
    return dataset


def _recording_task(dataset: Path, executed: list[str], run_metadata: dict[str, Any]) -> Any:
    def solve(sample: dict[str, Any]) -> dict[str, Any]:
        executed.append(str(sample["id"]))
        return {"completion": "GAP", "metadata": {"entry_keys": [], "run": run_metadata}}

    cast(Any, solve).run_metadata = run_metadata
    return FakeTaskModule(
        TASK_NAME="match-live",
        DATASET_PATH=dataset,
        PRODUCT_QUESTION=ProductQuestion.REQUIREMENT_CLASSIFICATION,
        solver=lambda: solve,
        scorer=lambda: lambda sample, output: {"value": True},
    )


def test_runner_executes_the_named_samples_rather_than_the_first_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Selecting two non-contiguous ids runs those two, not the leading two."""
    dataset = _id_dataset(tmp_path)
    executed: list[str] = []
    monkeypatch.setattr(
        runner, "_load_task_module", lambda reference: _recording_task(dataset, executed, {})
    )

    log_path = runner.run_task(
        "synthetic-task",
        log_dir=tmp_path / "logs",
        split="development",
        ids=["dev-2", "dev-4"],
    )

    header = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert executed == ["dev-2", "dev-4"]
    assert header["ids"] == ["dev-2", "dev-4"]
    assert header["total"] == 2


def test_runner_emits_selected_samples_in_dataset_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The order ids are typed in cannot reorder the run."""
    dataset = _id_dataset(tmp_path)
    executed: list[str] = []
    monkeypatch.setattr(
        runner, "_load_task_module", lambda reference: _recording_task(dataset, executed, {})
    )

    runner.run_task("synthetic-task", log_dir=tmp_path / "logs", ids=["dev-4", "dev-1"])

    assert executed == ["dev-1", "dev-4"]


def test_runner_refuses_an_id_absent_from_the_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A misspelled id names itself rather than silently running fewer samples."""
    dataset = _id_dataset(tmp_path)
    executed: list[str] = []
    monkeypatch.setattr(
        runner, "_load_task_module", lambda reference: _recording_task(dataset, executed, {})
    )

    with pytest.raises(ValueError, match="dev-9"):
        runner.run_task("synthetic-task", log_dir=tmp_path / "logs", ids=["dev-1", "dev-9"])
    assert executed == []


def test_runner_refuses_selecting_by_id_and_by_count_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two selections in one run could silently keep fewer samples than asked for."""
    dataset = _id_dataset(tmp_path)
    executed: list[str] = []
    monkeypatch.setattr(
        runner, "_load_task_module", lambda reference: _recording_task(dataset, executed, {})
    )

    with pytest.raises(ValueError, match="--ids and --limit"):
        runner.run_task("synthetic-task", log_dir=tmp_path / "logs", limit=1, ids=["dev-1"])
    assert executed == []


def test_runner_refuses_ids_disagreeing_with_the_approved_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An approval naming four samples cannot be spent on four others."""
    dataset = _id_dataset(tmp_path)
    executed: list[str] = []
    run_metadata = {"sample_ids": ["dev-1", "dev-2"], "sample_limit": 2}
    monkeypatch.setattr(
        runner,
        "_load_task_module",
        lambda reference: _recording_task(dataset, executed, run_metadata),
    )

    with pytest.raises(ValueError, match="approved sample ids"):
        runner.run_task("synthetic-task", log_dir=tmp_path / "logs", ids=["dev-1", "dev-3"])
    assert executed == []


def test_runner_accepts_the_approved_ids_in_any_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The approved selection is a set, so the order typed does not refuse the run."""
    dataset = _id_dataset(tmp_path)
    executed: list[str] = []
    run_metadata = {"sample_ids": ["dev-1", "dev-3"], "sample_limit": 2}
    monkeypatch.setattr(
        runner,
        "_load_task_module",
        lambda reference: _recording_task(dataset, executed, run_metadata),
    )

    runner.run_task("synthetic-task", log_dir=tmp_path / "logs", ids=["dev-3", "dev-1"])

    assert executed == ["dev-1", "dev-3"]


def test_a_recorded_family_reports_no_rate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A verdict a maintainer reads has no pass mark, so a rate would overclaim."""
    dataset = tmp_path / "critic.jsonl"
    _write_dataset(dataset, [{"id": "case-1", "split": "development"}])

    task = FakeTaskModule(
        TASK_NAME="critic-live",
        DATASET_PATH=dataset,
        PRODUCT_QUESTION=ProductQuestion.DRAFT_CRITIQUE,
        SCORE_KIND="recorded",
        solver=lambda: lambda sample: {"completion": "fail"},
        scorer=lambda: (
            lambda sample, output: {
                "value": True,
                "critic": {"outcome": "fail"},
            }
        ),
    )
    monkeypatch.setattr(runner, "_load_task_module", lambda reference: task)

    log_path = runner.run_task("synthetic-task", log_dir=tmp_path / "logs")

    header = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert header["score_kind"] == "recorded"
    assert "accuracy" not in header
    assert "passed" not in header
    assert header["total"] == 1
    assert header["critic_outcomes"] == {"fail": 1}


def test_a_run_reports_every_row_whose_axes_did_not_decide_its_mark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reader subtracting these from the total must be left with the rows the axes marked.

    A split reference and a discarded answer both leave the recommendation to
    decide alone, so a header naming only the first overstates the denominator.
    """
    dataset = tmp_path / "relevance_review.jsonl"
    _write_dataset(
        dataset,
        [
            {"id": "rrp-001", "split": "development"},
            {"id": "rrp-002", "split": "development"},
            {"id": "rrp-003", "split": "development"},
        ],
    )
    recorded = {"rrp-001": True, "rrp-002": False, "rrp-003": None}
    task = FakeTaskModule(
        TASK_NAME="relevance-live",
        DATASET_PATH=dataset,
        PRODUCT_QUESTION=ProductQuestion.FIT_RECOMMENDATION,
        solver=lambda: lambda sample: {"completion": "review"},
        scorer=lambda: (
            lambda sample, output: {
                "value": True,
                "metadata": {"axes_scored": recorded[sample["id"]]},
            }
        ),
    )
    monkeypatch.setattr(runner, "_load_task_module", lambda reference: task)

    log_path = runner.run_task("synthetic-task", log_dir=tmp_path / "logs")

    header = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert header["total"] == 3
    assert header["axes_recorded_not_scored"] == 1
    assert header["axes_not_recorded"] == 1


def test_a_task_declaring_an_unknown_score_kind_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unrecognized declaration must stop the run rather than default to a rate."""
    dataset = tmp_path / "unknown.jsonl"
    _write_dataset(dataset, [{"id": "case-1", "split": "development"}])

    task = FakeTaskModule(
        TASK_NAME="odd",
        DATASET_PATH=dataset,
        PRODUCT_QUESTION=ProductQuestion.DRAFT_CRITIQUE,
        SCORE_KIND="advisory",
        solver=lambda: lambda sample: {"completion": "fail"},
        scorer=lambda: lambda sample, output: {"value": True},
    )
    monkeypatch.setattr(runner, "_load_task_module", lambda reference: task)

    with pytest.raises(ValueError, match="unknown SCORE_KIND"):
        runner.run_task("synthetic-task", log_dir=tmp_path / "logs")


def test_a_task_without_a_default_dataset_requires_an_explicit_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = tmp_path / "replay.jsonl"
    _write_dataset(dataset, [{"id": "replay-1", "current_classification": "GAP"}])
    task = FakeTaskModule(
        TASK_NAME="match-replay",
        DATASET_PATH=None,
        PRODUCT_QUESTION=ProductQuestion.REQUIREMENT_CLASSIFICATION,
        solver=lambda: lambda sample: {"completion": "GAP", "metadata": {"entry_keys": []}},
        scorer=lambda: (
            lambda sample, output: {
                "value": sample["current_classification"] == output["completion"]
            }
        ),
    )
    monkeypatch.setattr(runner, "_load_task_module", lambda reference: task)

    with pytest.raises(ValueError, match="explicit --dataset"):
        runner.run_task("replay-task", log_dir=tmp_path / "missing")

    log = runner.run_task(
        "replay-task",
        log_dir=tmp_path / "logs",
        dataset_path=dataset,
    )
    header = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert header["dataset"] == str(dataset)


def test_a_paid_partial_run_keeps_each_completed_row_durably(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = tmp_path / "paid.jsonl"
    _write_dataset(dataset, [{"id": "paid-1"}, {"id": "paid-2"}])
    calls = 0
    run_metadata = {"provider": "openai"}

    def solve(sample: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ProviderError("provider disconnected")
        return {
            "completion": "GAP",
            "metadata": {"entry_keys": [], "run": run_metadata},
        }

    solve.run_metadata = run_metadata  # type: ignore[attr-defined]
    task = FakeTaskModule(
        TASK_NAME="match-live",
        DATASET_PATH=dataset,
        PRODUCT_QUESTION=ProductQuestion.REQUIREMENT_CLASSIFICATION,
        solver=lambda: solve,
        scorer=lambda: lambda sample, output: {"value": True},
    )
    monkeypatch.setattr(runner, "_load_task_module", lambda reference: task)
    log_dir = tmp_path / "logs"

    with pytest.raises(ProviderError, match="disconnected"):
        runner.run_task("paid-task", log_dir=log_dir)

    partials = list(log_dir.glob("*.partial.jsonl"))
    assert len(partials) == 1
    assert partials[0].stat().st_mode & 0o777 == 0o600
    records = [json.loads(line) for line in partials[0].read_text(encoding="utf-8").splitlines()]
    assert records[0]["status"] == "running"
    assert records[0]["run"] == {"provider": "openai"}
    assert [record["id"] for record in records[1:-1]] == ["paid-1"]
    assert records[-1] == {"status": "failed", "error_type": "ProviderError"}
    assert not list(log_dir.glob("*-match-live.jsonl"))


def test_a_paid_receipt_keeps_the_solver_result_when_scoring_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = tmp_path / "paid.jsonl"
    _write_dataset(dataset, [{"id": "paid-1"}])
    run_metadata = {"provider": "openai"}

    def solve(sample: dict[str, Any]) -> dict[str, Any]:
        return {
            "completion": "GAP",
            "metadata": {"entry_keys": [], "run": run_metadata},
        }

    def fail_scoring(_sample: dict[str, Any], _output: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("scorer failed")

    solve.run_metadata = run_metadata  # type: ignore[attr-defined]
    task = FakeTaskModule(
        TASK_NAME="match-live",
        DATASET_PATH=dataset,
        PRODUCT_QUESTION=ProductQuestion.REQUIREMENT_CLASSIFICATION,
        solver=lambda: solve,
        scorer=lambda: fail_scoring,
    )
    monkeypatch.setattr(runner, "_load_task_module", lambda reference: task)
    log_dir = tmp_path / "logs"

    with pytest.raises(RuntimeError, match="scorer failed"):
        runner.run_task("paid-task", log_dir=log_dir)

    partials = list(log_dir.glob("*.partial.jsonl"))
    assert len(partials) == 1
    records = [json.loads(line) for line in partials[0].read_text(encoding="utf-8").splitlines()]
    assert records[1] == {
        "id": "paid-1",
        "solver_output": {
            "completion": "GAP",
            "metadata": {"entry_keys": [], "run": run_metadata},
        },
    }
    assert records[-1] == {"status": "failed", "error_type": "RuntimeError"}


_LIVE_MATCH = {"provider": "openai", "stage": "match", "requested_model": "gpt-5.6-terra"}


def _answer_or_stop(finishes: dict[int, Finish]) -> Any:
    """Return a paid solver whose numbered calls stop with the given finish."""
    calls = 0

    def solve(sample: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls in finishes:
            usage = Usage(input_tokens=2_000, output_tokens=4_000, reasoning_tokens=4_000)
            record = CallRecord(usage=usage, model="gpt-5.6-terra", finish=finishes[calls])
            refuse_unusable(record, "OpenAI", 4_000)
        return {"completion": "GAP", "metadata": {"entry_keys": [], "run": _LIVE_MATCH}}

    solve.run_metadata = _LIVE_MATCH  # type: ignore[attr-defined]
    return solve


def _paid_task(dataset: Path, solve: Any) -> FakeTaskModule:
    return FakeTaskModule(
        TASK_NAME="match-live",
        DATASET_PATH=dataset,
        PRODUCT_QUESTION=ProductQuestion.REQUIREMENT_CLASSIFICATION,
        solver=lambda: solve,
        scorer=lambda: lambda sample, output: {"value": True},
    )


def test_a_repeat_that_exhausts_its_output_budget_fails_and_the_run_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One model that thinks past its ceiling is a failed repeat, not a lost arm."""
    dataset = tmp_path / "paid.jsonl"
    _write_dataset(dataset, [{"id": "paid-1"}, {"id": "paid-2"}])
    task = _paid_task(dataset, _answer_or_stop({2: Finish.TRUNCATED}))
    monkeypatch.setattr(runner, "_load_task_module", lambda reference: task)

    log_path = runner.run_task("paid-task", log_dir=tmp_path / "logs", trials=2)

    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    header, rows = records[0], records[1:]
    assert (header["total"], header["passed"]) == (4, 3)
    assert header["failures"] == {"output_budget_exhausted": 1}
    failed = rows[1]
    assert (failed["id"], failed["trial"], failed["score"]["value"]) == ("paid-1", 1, False)
    assert failed["score"]["explanation"].startswith(
        "output budget exhausted in the match stage: OpenAI stopped at the 4,000 token"
    )
    measurement = failed["solver_output"]["metadata"]["measurements"][0]
    assert (measurement["stage"], measurement["usage"]["reasoning_tokens"]) == ("match", 4_000)
    assert not list((tmp_path / "logs").glob("*.partial.jsonl"))


_TRUNCATING_BATCH_TASK = """
from pathlib import Path

from evals.inspect.product_questions import ProductQuestion
from evals.inspect.outcomes import FailedCall
from clearscout.infrastructure.providers.base import (
    CallRecord, Finish, UnusableResponseError, refuse_unusable,
)
from clearscout.shared.costs import Usage

TASK_NAME = "truncating-batch"
DATASET_PATH = Path(__file__).with_name("batch.jsonl")
PRODUCT_QUESTION = ProductQuestion.FIT_RECOMMENDATION


class _Solver:
    def __call__(self, sample):
        raise AssertionError("batch only")

    def batch(self, samples):
        usage = Usage(input_tokens=10, output_tokens=40, reasoning_tokens=40)
        record = CallRecord(usage=usage, model="gpt-5.6-terra", finish=Finish.TRUNCATED)
        try:
            refuse_unusable(record, "OpenAI", 40)
        except UnusableResponseError as error:
            failed = FailedCall(error, {"sample_ids": [sample["id"] for sample in samples]})
        for position in range(len(samples)):
            yield position, failed


def solver():
    return _Solver()


def scorer():
    return lambda sample, output: {"value": True}
"""


def test_a_truncated_batch_call_fails_its_rows_when_the_runner_runs_as_a_module(
    tmp_path: Path,
) -> None:
    """``eval.sh`` runs the runner as ``__main__``, so a solver's outcome type must still match."""
    _write_dataset(tmp_path / "batch.jsonl", [{"id": "case-1"}, {"id": "case-2"}])
    task = tmp_path / "truncating_task.py"
    task.write_text(_TRUNCATING_BATCH_TASK, encoding="utf-8")
    logs = tmp_path / "logs"

    completed = subprocess.run(  # noqa: S603 - the current interpreter on a synthetic task
        [sys.executable, "-m", "evals.inspect.runner", str(task), "--log-dir", str(logs)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    (log_path,) = logs.glob("*.jsonl")
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()[1:]]
    assert [row["solver_output"]["failure"] for row in rows] == [runner.OUTPUT_BUDGET_EXHAUSTED] * 2
    assert [row["solver_output"]["metadata"]["batch"]["sample_ids"] for row in rows] == [
        ["case-1", "case-2"]
    ] * 2


def test_a_truncated_whole_posting_repeat_still_counts_its_requirements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A truncated row has no answer, but its requirements are still owed and all fail."""
    dataset = tmp_path / "multi.jsonl"
    matches = {"delivery": "GAP", "orchestration": "GAP"}
    _write_dataset(dataset, [{"id": "multi-1", "metadata": {"scripted_matches": matches}}])
    answered = _answer_or_stop({2: Finish.TRUNCATED})

    def solve(sample: dict[str, Any]) -> dict[str, Any]:
        output: dict[str, Any] = answered(sample)
        output["per_requirement"] = [
            {"requirement_id": key, "completion": "GAP", "metadata": {"entry_keys": []}}
            for key in matches
        ]
        return output

    solve.run_metadata = _LIVE_MATCH  # type: ignore[attr-defined]
    task = FakeTaskModule(
        TASK_NAME="match-live",
        DATASET_PATH=dataset,
        PRODUCT_QUESTION=ProductQuestion.REQUIREMENT_CLASSIFICATION,
        solver=lambda: solve,
        scorer=per_requirement_scripted_contract,
    )
    monkeypatch.setattr(runner, "_load_task_module", lambda reference: task)

    log_path = runner.run_task("paid-task", log_dir=tmp_path / "logs", trials=2)

    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    header, truncated = records[0], records[2]
    assert header["requirement_total"] == 4
    assert truncated["score"]["failure"] == "output_budget_exhausted"
    assert truncated["score"]["requirements"] == {"total": 2, "passed": 0, "results": []}


def test_an_unusable_answer_other_than_truncation_stops_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal after a truncation aborts, so only the budget case scores as a model failure."""
    dataset = tmp_path / "paid.jsonl"
    _write_dataset(dataset, [{"id": "paid-1"}, {"id": "paid-2"}])
    task = _paid_task(dataset, _answer_or_stop({1: Finish.TRUNCATED, 2: Finish.REFUSED}))
    monkeypatch.setattr(runner, "_load_task_module", lambda reference: task)
    log_dir = tmp_path / "logs"

    with pytest.raises(UnusableResponseError, match="declined to answer"):
        runner.run_task("paid-task", log_dir=log_dir)

    partial = next(log_dir.glob("*.partial.jsonl"))
    records = [json.loads(line) for line in partial.read_text(encoding="utf-8").splitlines()]
    assert records[1]["solver_output"]["failure"] == "output_budget_exhausted"
    assert records[-1] == {"status": "failed", "error_type": "UnusableResponseError"}
    assert not list(log_dir.glob("*-match-live.jsonl"))


def test_runner_redacts_labels_before_invoking_the_solver(tmp_path: Path) -> None:
    """The solver input cannot observe expected answers or reviewer material."""
    dataset = tmp_path / "match.jsonl"
    rows: list[dict[str, Any]] = [
        {
            "id": "dev-1",
            "split": "development",
            "requirement_text": "Synthetic requirement",
            "dimension": "hard",
            "cv_hash": "synthetic-hash",
            "expected_classification": "DIRECT",
            "target": "DIRECT",
            "current_classification": "DIRECT",
            "reviewer_rationale": "private label evidence",
            "metadata": {
                "expected_classification": "DIRECT",
                "scripted_classification": "GAP",
            },
        }
    ]
    dataset.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    observed: list[dict[str, Any]] = []

    def solve(sample: dict[str, Any]) -> dict[str, Any]:
        observed.append(sample)
        return {"completion": "DIRECT", "metadata": {"entry_keys": ["b-r1a"]}}

    runner._score_rows(
        dataset.read_bytes(),
        solve,
        lambda sample, output: {"value": sample["expected_classification"] == "DIRECT"},
    )

    assert observed == [
        {
            "id": "dev-1",
            "split": "development",
            "requirement_text": "Synthetic requirement",
            "dimension": "hard",
            "cv_hash": "synthetic-hash",
            "metadata": {"scripted_classification": "GAP"},
        }
    ]


@pytest.mark.parametrize("label", ["DIRECT", "TAILORING_ONLY", "TRANSFERABLE"])
def test_match_scorer_requires_citations_for_evidence_labels(label: str) -> None:
    score = classification_match()

    assert not score(
        {"expected_classification": label},
        {"completion": label, "metadata": {"entry_keys": [], "outcome_complete": True}},
    )["value"]


@pytest.mark.parametrize("label", ["ASSERTED", "GAP"])
def test_match_scorer_requires_no_citations_for_non_evidence_labels(label: str) -> None:
    score = classification_match()

    assert score(
        {"expected_classification": label},
        {"completion": label, "metadata": {"entry_keys": [], "outcome_complete": True}},
    )["value"]
    assert not score(
        {"expected_classification": label},
        {"completion": label, "metadata": {"entry_keys": ["b-r1a"], "outcome_complete": True}},
    )["value"]


def test_match_scorer_does_not_credit_an_unestablished_requirement() -> None:
    """A run that established nothing fails even though its label reads GAP."""
    result = classification_match()(
        {"expected_classification": "GAP"},
        {
            "completion": "GAP",
            "metadata": {
                "entry_keys": [],
                "outcome_complete": False,
                "gap_class": "incomplete_evidence",
                "note": "Model result was missing; treated as a gap.",
            },
        },
    )

    assert result["value"] is False
    assert result["metadata"]["classification_matches"] is True
    assert result["metadata"]["outcome_complete"] is False
    assert result["metadata"]["gap_class"] == "incomplete_evidence"
    assert result["metadata"]["note"] == "Model result was missing; treated as a gap."
    assert "run did not establish this requirement" in result["explanation"]


@pytest.mark.parametrize(
    ("expected", "answer", "entry_keys", "citation_valid", "citation_problem"),
    [
        ("GAP", "DIRECT", [], False, "no supporting entry was cited"),
        ("DIRECT", "GAP", ["syn-e1"], False, "cited evidence is not permitted"),
        ("TRANSFERABLE", "DIRECT", ["syn-e1"], True, ""),
    ],
)
def test_match_scorer_reports_citation_validity_even_on_a_label_mismatch(
    expected: str,
    answer: str,
    entry_keys: list[str],
    citation_valid: bool,
    citation_problem: str,
) -> None:
    result = classification_match()(
        {"expected_classification": expected},
        {"completion": answer, "metadata": {"entry_keys": entry_keys, "outcome_complete": True}},
    )

    assert result["value"] is False
    assert result["metadata"] == {
        "expected": expected,
        "classification_matches": False,
        "citation_valid": citation_valid,
        "outcome_complete": True,
        "expected_strength": None,
        "strength_matches": None,
        "provenance": None,
    }
    assert "classification mismatch" in result["explanation"]
    if citation_problem:
        assert citation_problem in result["explanation"]


@pytest.mark.parametrize(
    ("expected_strength", "answer", "answered_strength", "strength_matches", "passes"),
    [
        ("moderate", "TAILORING_ONLY", "moderate", True, True),
        ("moderate", "TAILORING_ONLY", "strong", False, True),
        ("moderate", "DIRECT", "moderate", None, False),
        (None, "TAILORING_ONLY", "moderate", None, True),
    ],
    ids=["agrees", "differs", "class-mismatch", "unlabelled"],
)
def test_match_scorer_reports_strength_beside_the_pass_mark(
    expected_strength: str | None,
    answer: str,
    answered_strength: str,
    strength_matches: bool | None,
    passes: bool,
) -> None:
    """Strength is compared only where the class matched, and never moves the pass mark."""
    sample: dict[str, Any] = {"expected_classification": "TAILORING_ONLY"}
    if expected_strength is not None:
        sample["expected_strength"] = expected_strength

    result = classification_match()(
        sample,
        {
            "completion": answer,
            "metadata": {
                "entry_keys": ["syn-e7"],
                "outcome_complete": True,
                "strength": answered_strength,
            },
        },
    )

    assert result["value"] is passes
    assert result["metadata"]["expected_strength"] == expected_strength
    assert result["metadata"]["strength_matches"] is strength_matches


@pytest.mark.parametrize(
    ("answer", "passes"),
    [("TAILORING_ONLY", True), ("TRANSFERABLE", True), ("DIRECT", False), ("GAP", False)],
)
def test_a_contested_row_passes_any_classification_in_its_accepted_set(
    answer: str, passes: bool
) -> None:
    entry_keys = [] if answer == "GAP" else ["syn-e7"]
    result = classification_match()(
        {"expected_classification": ["TAILORING_ONLY", "TRANSFERABLE"]},
        {"completion": answer, "metadata": {"entry_keys": entry_keys, "outcome_complete": True}},
    )

    assert result["value"] is passes
    assert result["metadata"]["expected"] == ["TAILORING_ONLY", "TRANSFERABLE"]
    assert result["metadata"]["classification_matches"] is passes


@pytest.mark.parametrize(
    ("answered_strength", "strength_matches"),
    [("strong", True), ("moderate", True), ("weak", False)],
)
def test_a_contested_strength_agrees_with_any_strength_in_its_accepted_set(
    answered_strength: str, strength_matches: bool
) -> None:
    result = classification_match()(
        {"expected_classification": "DIRECT", "expected_strength": ["strong", "moderate"]},
        {
            "completion": "DIRECT",
            "metadata": {
                "entry_keys": ["syn-e7"],
                "outcome_complete": True,
                "strength": answered_strength,
            },
        },
    )

    assert result["value"] is True
    assert result["metadata"]["expected_strength"] == ["strong", "moderate"]
    assert result["metadata"]["strength_matches"] is strength_matches


@pytest.mark.parametrize(
    ("answer", "answered_strength", "strength_matches"),
    [
        ("DIRECT", "strong", True),
        ("DIRECT", "moderate", False),
        ("TRANSFERABLE", "moderate", True),
        ("TRANSFERABLE", "strong", False),
    ],
)
def test_an_accepted_set_pairs_each_classification_with_its_own_strength(
    answer: str, answered_strength: str, strength_matches: bool
) -> None:
    result = classification_match()(
        {
            "expected_classification": ["DIRECT", "TRANSFERABLE"],
            "expected_strength": ["strong", "moderate"],
        },
        {
            "completion": answer,
            "metadata": {
                "entry_keys": ["syn-e7"],
                "outcome_complete": True,
                "strength": answered_strength,
            },
        },
    )

    assert result["value"] is True
    assert result["metadata"]["strength_matches"] is strength_matches


@pytest.mark.parametrize(
    ("row", "answer", "strength", "strength_matches"),
    [
        (
            {
                "expected_classification": ["GAP", "TRANSFERABLE"],
                "expected_strength": ["none", "weak"],
            },
            "GAP",
            "none",
            None,
        ),
        (
            {
                "expected_classification": ["GAP", "TRANSFERABLE"],
                "expected_strength": ["none", "weak"],
            },
            "TRANSFERABLE",
            "weak",
            True,
        ),
        (
            {"expected_classification": "ASSERTED", "expected_strength": "none"},
            "ASSERTED",
            "none",
            None,
        ),
    ],
    ids=["forced-gap", "evidence-backed", "forced-asserted"],
)
def test_an_answer_that_forces_none_compares_no_strength(
    row: dict[str, Any], answer: str, strength: str, strength_matches: bool | None
) -> None:
    entry_keys = ["syn-e7"] if answer == "TRANSFERABLE" else []
    result = classification_match()(
        row,
        {
            "completion": answer,
            "metadata": {"entry_keys": entry_keys, "outcome_complete": True, "strength": strength},
        },
    )

    assert result["value"] is True
    assert result["metadata"]["strength_matches"] is strength_matches


@pytest.mark.parametrize(
    "row",
    [
        {"expected_classification": []},
        {"expected_classification": ["DIRECT", ""]},
        {"expected_classification": ["DIRECT", 3]},
        {"expected_classification": ["DIRECT", "LIKELY"]},
        {"expected_classification": "DIRECT", "expected_strength": ["strong", "high"]},
        {"expected_classification": ["DIRECT", "GAP"], "expected_strength": ["strong"]},
        {"expected_classification": ["DIRECT", "GAP"], "expected_strength": "strong"},
        {"expected_classification": "DIRCT", "expected_strength": "strong"},
        {"expected_classification": "GAP", "expected_strength": "strong"},
        {},
    ],
    ids=[
        "empty",
        "blank",
        "not-text",
        "unknown-class",
        "unknown-strength",
        "unpaired",
        "gap-with-strength",
        "single-unknown-class",
        "single-gap-with-strength",
        "no-label",
    ],
)
def test_a_malformed_accepted_set_stops_the_run_before_any_solver_call(
    tmp_path: Path, row: dict[str, Any]
) -> None:
    dataset = tmp_path / "match.jsonl"
    well_formed = {"id": "m-1", "expected_classification": "GAP", "expected_strength": "none"}
    _write_dataset(dataset, [well_formed, {"id": "m-2", **row}])
    called: list[str] = []

    def solve(sample: dict[str, Any]) -> dict[str, Any]:
        called.append(sample["id"])
        return {"completion": "GAP", "metadata": {"outcome_complete": True}}

    with pytest.raises(ValueError, match="accepted"):
        runner._score_rows(dataset.read_bytes(), solve, classification_match())
    assert called == []


def test_development_reads_never_serve_a_held_out_row(tmp_path: Path) -> None:
    dataset = tmp_path / "match.jsonl"
    _write_dataset(
        dataset,
        [
            {"id": "dev-1", "split": "development", "requirement_text": "Synthetic one"},
            {"id": "held-1", "split": "holdout", "requirement_text": "Synthetic two"},
        ],
    )

    def served(**selection: Any) -> list[str]:
        observed: list[str] = []

        def solve(sample: dict[str, Any]) -> dict[str, Any]:
            observed.append(sample["id"])
            return {"completion": "GAP"}

        runner._score_rows(dataset.read_bytes(), solve, lambda _s, _o: {"value": True}, **selection)
        return observed

    assert served() == ["dev-1"]
    assert served(split="development") == ["dev-1"]
    assert served(split="holdout") == ["held-1"]
    with pytest.raises(ValueError, match="no sample for the selected ids: held-1"):
        served(ids=["held-1"])


def test_a_run_reports_strength_agreement_over_matched_evidence_backed_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row whose score compared no strength leaves the denominator."""
    dataset = tmp_path / "match.jsonl"
    _write_dataset(
        dataset,
        [
            {"id": "m-1", "split": "development"},
            {"id": "m-2", "split": "development"},
            {"id": "m-3", "split": "development"},
        ],
    )
    compared = {"m-1": True, "m-2": False, "m-3": None}
    task = FakeTaskModule(
        TASK_NAME="match-live",
        DATASET_PATH=dataset,
        PRODUCT_QUESTION=ProductQuestion.REQUIREMENT_CLASSIFICATION,
        solver=lambda: lambda sample: {"completion": "DIRECT"},
        scorer=lambda: (
            lambda sample, output: {
                "value": True,
                "metadata": {"strength_matches": compared[sample["id"]]},
            }
        ),
    )
    monkeypatch.setattr(runner, "_load_task_module", lambda reference: task)

    log_path = runner.run_task("synthetic-task", log_dir=tmp_path / "logs")

    header = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert header["strength_agreed"] == 1
    assert header["strength_compared"] == 2


def test_a_run_reports_critic_outcome_agreement_beside_the_pass_mark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row that compared no outcome leaves the denominator, and agreement never moves passed."""
    dataset = tmp_path / "critic.jsonl"
    _write_dataset(
        dataset,
        [
            {"id": "c-1", "split": "development"},
            {"id": "c-2", "split": "development"},
            {"id": "c-3", "split": "development"},
        ],
    )
    compared = {"c-1": True, "c-2": False, "c-3": None}
    task = FakeTaskModule(
        TASK_NAME="critic-live",
        DATASET_PATH=dataset,
        PRODUCT_QUESTION=ProductQuestion.DRAFT_CRITIQUE,
        solver=lambda: lambda sample: {"completion": "warn"},
        scorer=lambda: (
            lambda sample, output: {
                "value": True,
                "metadata": {"outcome_matches": compared[sample["id"]]},
            }
        ),
    )
    monkeypatch.setattr(runner, "_load_task_module", lambda reference: task)

    log_path = runner.run_task("synthetic-task", log_dir=tmp_path / "logs")

    header = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert header["passed"] == 3
    assert header["critic_outcome_agreed"] == 1
    assert header["critic_outcome_compared"] == 2


def _match_provenance(**changes: Any) -> dict[str, Any]:
    return {
        "protocol_version": "clearscout-match-development-v1",
        "family": "match",
        "rubric_version": "match-rubric-v1",
        "reviewer_a": "reviewer-a",
        "reviewer_b": "reviewer-b",
        "disagreements": [],
        **changes,
    }


def _gap_answer() -> dict[str, Any]:
    return {"completion": "GAP", "metadata": {"entry_keys": [], "outcome_complete": True}}


def test_match_scorer_reports_the_review_record_the_row_carries() -> None:
    """The match file documents the record, so scoring reads it rather than ignoring it."""
    record = _match_provenance(
        disagreements=["classification"], adjudicator="adjudicator", adjudicator_kind="agent"
    )

    result = classification_match()(
        {"expected_classification": "GAP", "provenance": record}, _gap_answer()
    )

    assert result["metadata"]["provenance"]["disagreement_count"] == 1
    assert result["metadata"]["provenance"]["adjudicator_kind"] == "agent"


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (
            _match_provenance(disagreements=["classification"]),
            "recorded exactly for material disagreements",
        ),
        (_match_provenance(disagreements=["recommendation"]), "does not decide"),
        (
            _match_provenance(
                protocol_version="clearscout-relevance-review-development-v1",
                family="relevance-review",
            ),
            "another review family",
        ),
    ],
)
def test_match_scorer_refuses_a_review_record_nobody_could_have_written(
    record: dict[str, Any], message: str
) -> None:
    """An invented record must fail the run rather than ride along unread."""
    with pytest.raises(ValueError, match=message):
        classification_match()(
            {"expected_classification": "GAP", "provenance": record}, _gap_answer()
        )


@pytest.mark.parametrize("entry_keys", ["syn-e1", [""], [1]])
def test_match_scorer_rejects_a_malformed_citation_collection(entry_keys: object) -> None:
    result = classification_match()(
        {"expected_classification": "DIRECT"},
        {"completion": "DIRECT", "metadata": {"entry_keys": entry_keys, "outcome_complete": True}},
    )

    assert result["value"] is False
    assert result["metadata"]["citation_valid"] is False
    assert "list of non-empty strings" in result["explanation"]


def test_runner_refuses_split_selection_when_a_sample_has_no_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = tmp_path / "match.jsonl"
    _write_dataset(dataset, [{"id": "unpartitioned"}])
    task = FakeTaskModule(
        TASK_NAME="match-live",
        DATASET_PATH=dataset,
        PRODUCT_QUESTION=ProductQuestion.REQUIREMENT_CLASSIFICATION,
        solver=lambda: lambda sample: {"completion": "GAP", "metadata": {}},
        scorer=lambda: lambda sample, output: {"value": True},
    )
    monkeypatch.setattr(runner, "_load_task_module", lambda reference: task)

    with pytest.raises(ValueError, match="does not declare a split"):
        runner.run_task(
            "synthetic-task",
            log_dir=tmp_path / "logs",
            limit=1,
            split="development",
        )


def test_live_run_metadata_requires_an_explicit_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLEARSCOUT_EVAL_LIVE_APPROVED_CENTS", "100")
    monkeypatch.delenv("CLEARSCOUT_EVAL_LIVE_PROVIDER", raising=False)
    monkeypatch.setenv("CLEARSCOUT_EVAL_DATASET_SPLIT", "development")
    monkeypatch.setenv("CLEARSCOUT_EVAL_SAMPLE_LIMIT", "1")
    monkeypatch.setenv("CLEARSCOUT_EVAL_OUTPUT_DIR", str(tmp_path))

    with pytest.raises(RuntimeError, match="explicit provider"):
        live_run_metadata()


def test_live_run_metadata_records_derived_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLEARSCOUT_EVAL_LIVE_APPROVED_CENTS", "100")
    monkeypatch.setenv("CLEARSCOUT_EVAL_LIVE_PROVIDER", "openai")
    monkeypatch.setenv("CLEARSCOUT_EVAL_DATASET_SPLIT", "regression")
    monkeypatch.setenv("CLEARSCOUT_EVAL_SAMPLE_LIMIT", "1")
    monkeypatch.setenv("CLEARSCOUT_EVAL_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(clearscout_live, "_selection_manifest_identity", _selection_identity)

    metadata = live_run_metadata()

    assert metadata["provider"] == "openai"
    assert metadata["requested_model"]
    assert metadata["prompt_suite_fingerprint"]
    assert metadata["settings_hash"]
    assert metadata["approved_cents"] == 100
    assert metadata["dataset_split"] == "regression"
    assert metadata["output_dir"] == str(tmp_path.resolve())
    assert metadata["selection_manifest_sha256"] == "a" * 64
    assert metadata["dataset_sha256"] == "c" * 64


def test_live_run_metadata_refuses_a_public_repository_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLEARSCOUT_EVAL_LIVE_APPROVED_CENTS", "100")
    monkeypatch.setenv("CLEARSCOUT_EVAL_LIVE_PROVIDER", "openai")
    monkeypatch.setenv("CLEARSCOUT_EVAL_DATASET_SPLIT", "holdout")
    monkeypatch.setenv("CLEARSCOUT_EVAL_SAMPLE_LIMIT", "1")
    monkeypatch.setenv("CLEARSCOUT_EVAL_OUTPUT_DIR", str(Path.cwd() / "reports" / "eval"))

    with pytest.raises(RuntimeError, match="outside the public repository"):
        live_run_metadata()


def _billable(monkeypatch: pytest.MonkeyPatch, database_url: str) -> None:
    """Give a live run the single workspace it bills its calls to."""
    from clearscout.infrastructure.persistence.models.workspaces import Workspace

    database = Database(database_url)
    try:
        with database.session() as session:
            session.add(Workspace(slug="ws", name="Workspace"))
    finally:
        database.dispose()
    monkeypatch.setenv("CLEARSCOUT_EVAL_DATABASE_URL", database_url)


def _live_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cents: str = "100") -> None:
    monkeypatch.setenv("CLEARSCOUT_EVAL_LIVE_APPROVED_CENTS", cents)
    monkeypatch.setenv("CLEARSCOUT_EVAL_LIVE_PROVIDER", "openai")
    monkeypatch.setenv("CLEARSCOUT_EVAL_DATASET_SPLIT", "development")
    monkeypatch.setenv("CLEARSCOUT_EVAL_SAMPLE_LIMIT", "1")
    monkeypatch.setenv("CLEARSCOUT_EVAL_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(clearscout_live, "_selection_manifest_identity", _selection_identity)


def test_live_run_metadata_keeps_large_advisory_estimates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The estimate is metadata, without an application spending ceiling."""
    _live_env(monkeypatch, tmp_path, cents="100000")
    monkeypatch.delenv("CLEARSCOUT_EVAL_ESTIMATED_CENTS", raising=False)

    assert live_run_metadata()["approved_cents"] == 100000


def test_live_run_metadata_keeps_differing_estimates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An estimate mismatch does not revoke explicit run consent."""
    _live_env(monkeypatch, tmp_path, cents="100")
    monkeypatch.setenv("CLEARSCOUT_EVAL_ESTIMATED_CENTS", "250")

    metadata = live_run_metadata()
    assert metadata["approved_cents"] == 100
    assert metadata["estimated_cents"] == 250


def test_live_run_metadata_accepts_estimate_within_one_cent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The estimate match is inclusive to one cent of drift."""
    _live_env(monkeypatch, tmp_path, cents="100")
    monkeypatch.setenv("CLEARSCOUT_EVAL_ESTIMATED_CENTS", "101")

    metadata = live_run_metadata()

    assert metadata["approved_cents"] == 100
    assert metadata["estimated_cents"] == 101
    assert metadata["sample_limit"] == 1


def test_a_live_match_run_sends_the_model_the_run_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, durable_database_url: str
) -> None:
    """The named model reaches the client that makes the paid call and the run log."""
    _live_env(monkeypatch, tmp_path)
    _billable(monkeypatch, durable_database_url)
    monkeypatch.setenv("CLEARSCOUT_EVAL_LIVE_MODEL", "gpt-6-sol")
    monkeypatch.setattr(
        clearscout_live,
        "find_credential",
        lambda provider: Credential(
            key="test-key", provider=SUITE_PROVIDER, source=Source.ENVIRONMENT
        ),
    )
    monkeypatch.setattr(eval_client, "provider_for", lambda provider, key: FakeProvider())
    sent: list[str] = []

    def observed_match(client: Any, inventory: Any, spec: Any, guardrails: Any) -> EvidenceMap:
        sent.append(client.settings.choice_for_stage(Stage.MATCH).model)
        return EvidenceMap(matches=())

    monkeypatch.setattr(clearscout_live, "match", observed_match)
    inventory = synthetic_inventory()
    solver = clearscout_live.clearscout_live_solver()

    try:
        solver(
            {
                "requirement_text": "Synthetic requirement",
                "dimension": "hard",
                "cv_hash": inventory.content_hash,
            }
        )
    finally:
        solver.database.dispose()

    assert sent == ["gpt-6-sol"]
    assert solver.run_metadata["requested_model"] == "gpt-6-sol"


def test_a_live_openai_run_asks_for_the_flex_tier_and_records_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, durable_database_url: str
) -> None:
    """Nobody waits on an eval, so it pays the half-price tier and its log says so."""
    _live_env(monkeypatch, tmp_path)
    _billable(monkeypatch, durable_database_url)
    monkeypatch.setattr(
        clearscout_live,
        "find_credential",
        lambda provider: Credential(
            key="test-key", provider=SUITE_PROVIDER, source=Source.ENVIRONMENT
        ),
    )
    monkeypatch.setattr(eval_client, "provider_for", lambda provider, key: FakeProvider())
    sent: list[ServiceTier] = []

    def observed_match(client: Any, inventory: Any, spec: Any, guardrails: Any) -> EvidenceMap:
        sent.append(client.service_tier)
        return EvidenceMap(matches=())

    monkeypatch.setattr(clearscout_live, "match", observed_match)
    inventory = synthetic_inventory()
    solver = clearscout_live.clearscout_live_solver()

    try:
        solver(
            {
                "requirement_text": "Synthetic requirement",
                "dimension": "hard",
                "cv_hash": inventory.content_hash,
            }
        )
    finally:
        solver.database.dispose()

    assert sent == [ServiceTier.FLEX]
    assert solver.run_metadata["service_tier"] == "flex"


def test_a_live_requirement_the_run_left_unanswered_reports_no_strength(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, durable_database_url: str
) -> None:
    """The defaulted GAP carries the strength the product pairs with it, not a missing key."""
    _live_env(monkeypatch, tmp_path)
    _billable(monkeypatch, durable_database_url)
    monkeypatch.setattr(
        clearscout_live,
        "find_credential",
        lambda provider: Credential(
            key="test-key", provider=SUITE_PROVIDER, source=Source.ENVIRONMENT
        ),
    )
    monkeypatch.setattr(eval_client, "provider_for", lambda provider, key: FakeProvider())
    monkeypatch.setattr(
        clearscout_live,
        "match",
        lambda client, inventory, spec, guardrails: EvidenceMap(matches=()),
    )
    inventory = synthetic_inventory()
    solver = clearscout_live.clearscout_live_solver()

    try:
        output = solver(
            {
                "requirement_text": "Synthetic requirement",
                "dimension": "hard",
                "cv_hash": inventory.content_hash,
            }
        )
    finally:
        solver.database.dispose()

    assert output["completion"] == "GAP"
    assert output["metadata"]["strength"] == "none"
    assert output["metadata"]["outcome_complete"] is False


def test_live_solver_records_actual_provider_measurement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, durable_database_url: str
) -> None:
    """The private log can prove actual model identity, usage, latency, and spend."""
    _live_env(monkeypatch, tmp_path)
    _billable(monkeypatch, durable_database_url)
    monkeypatch.setattr(
        clearscout_live,
        "find_credential",
        lambda provider: Credential(
            key="test-key", provider=SUITE_PROVIDER, source=Source.ENVIRONMENT
        ),
    )
    monkeypatch.setattr(eval_client, "provider_for", lambda provider, key: FakeProvider())

    observed_guardrails: list[Any] = []

    def measured_match(client: Any, inventory: Any, spec: Any, guardrails: Any) -> EvidenceMap:
        observed_guardrails.append(guardrails)
        client.ledger.record(
            CallManifest(
                stage="match",
                model="gpt-5.6-terra",
                served="gpt-5.6-terra",
                provider="openai",
                effort="low",
                service_tier="default",
                finish="completed",
                usage=Usage(input_tokens=1_000, output_tokens=100),
                milliseconds=250,
            )
        )
        requirement_id = spec.requirements[0].id
        return EvidenceMap(
            matches=(
                Match(
                    requirement_id=requirement_id,
                    classification=Classification.DIRECT,
                    strength=Strength.STRONG,
                    entry_ids=("b-synthetic",),
                ),
            ),
        )

    monkeypatch.setattr(clearscout_live, "match", measured_match)

    inventory = synthetic_inventory()
    sample = {
        "requirement_text": "Synthetic requirement",
        "dimension": "hard",
        "cv_hash": inventory.content_hash,
    }
    solver = clearscout_live.clearscout_live_solver()
    output = solver(sample)
    second = solver(sample)

    assert output["metadata"]["strength"] == "strong"
    assert output["metadata"]["actual_dollars"] == pytest.approx(0.0032)
    assert output["metadata"]["cumulative_dollars"] == pytest.approx(0.0032)
    assert second["metadata"]["actual_dollars"] == pytest.approx(0.0032)
    assert second["metadata"]["cumulative_dollars"] == pytest.approx(0.0064)
    assert len(second["metadata"]["measurements"]) == 1
    measurement = output["metadata"]["measurements"][0]
    assert measurement["stage"] == "match"
    assert measurement["model"] == "gpt-5.6-terra"
    assert measurement["served_model"] == "gpt-5.6-terra"
    assert measurement["usage"] == {
        "input_tokens": 1_000,
        "output_tokens": 100,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
    }
    assert Decimal(measurement["dollars"]) == Decimal("0.0032")
    assert measurement["milliseconds"] == 250
    assert all(item.work_authorization for item in observed_guardrails)


def test_live_solver_records_the_tools_a_single_requirement_answer_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, durable_database_url: str
) -> None:
    """A log shows which named tools the cited entries were answered for."""
    _live_env(monkeypatch, tmp_path)
    _billable(monkeypatch, durable_database_url)
    monkeypatch.setattr(
        clearscout_live,
        "find_credential",
        lambda provider: Credential(
            key="test-key", provider=SUITE_PROVIDER, source=Source.ENVIRONMENT
        ),
    )
    monkeypatch.setattr(eval_client, "provider_for", lambda provider, key: FakeProvider())

    def tool_naming_match(client: Any, inventory: Any, spec: Any, guardrails: Any) -> EvidenceMap:
        return EvidenceMap(
            matches=(
                Match(
                    requirement_id=spec.requirements[0].id,
                    classification=Classification.DIRECT,
                    strength=Strength.STRONG,
                    entry_ids=("b-synthetic",),
                    named_tools=("ToolX", "ToolY"),
                ),
            ),
        )

    monkeypatch.setattr(clearscout_live, "match", tool_naming_match)
    inventory = synthetic_inventory()
    solver = clearscout_live.clearscout_live_solver()

    try:
        output = solver(
            {
                "requirement_text": "Synthetic requirement naming ToolX and ToolY",
                "dimension": "hard",
                "cv_hash": inventory.content_hash,
            }
        )
    finally:
        solver.database.dispose()

    assert output["metadata"]["entry_keys"] == ["b-synthetic"]
    assert output["metadata"]["named_tools"] == ["ToolX", "ToolY"]


def test_live_solver_returns_one_outcome_per_requirement_for_a_whole_posting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, durable_database_url: str
) -> None:
    """A whole-posting live sample sends the spec through one call and maps outcomes back."""
    _live_env(monkeypatch, tmp_path)
    _billable(monkeypatch, durable_database_url)
    monkeypatch.setattr(
        clearscout_live,
        "find_credential",
        lambda provider: Credential(
            key="test-key", provider=SUITE_PROVIDER, source=Source.ENVIRONMENT
        ),
    )
    monkeypatch.setattr(eval_client, "provider_for", lambda provider, key: FakeProvider())
    observed_ids: list[list[str]] = []

    def measured_match(client: Any, inventory: Any, spec: Any, guardrails: Any) -> EvidenceMap:
        observed_ids.append([requirement.id for requirement in spec.requirements])
        client.ledger.record(
            CallManifest(
                stage="match",
                model="gpt-5.6-terra",
                served="gpt-5.6-terra",
                provider="openai",
                effort="low",
                service_tier="default",
                finish="completed",
                usage=Usage(input_tokens=1_000, output_tokens=100),
                milliseconds=120,
            )
        )
        return EvidenceMap(
            matches=tuple(
                Match(
                    requirement_id=requirement.id,
                    classification=Classification.DIRECT if index == 0 else Classification.GAP,
                    strength=Strength.STRONG if index == 0 else Strength.NONE,
                    entry_ids=("syn-e1",) if index == 0 else (),
                    named_tools=("ToolX",) if index == 0 else (),
                )
                for index, requirement in enumerate(spec.requirements)
            )
        )

    monkeypatch.setattr(clearscout_live, "match", measured_match)
    inventory = synthetic_inventory()
    sample = {
        "cv_hash": inventory.content_hash,
        "job_spec": {
            "title": "Role",
            "company": "Aster Systems",
            "seniority": None,
            "location": None,
            "requirements": [
                {"id": "delivery", "text": "Deliver a service.", "importance": "must_have"},
                {
                    "id": "collaboration",
                    "text": "Coordinate partners.",
                    "importance": "nice_to_have",
                    "dimension": "behavioral",
                },
            ],
            "keywords": [],
            "hard_gates": [],
            "origin": "test",
        },
    }

    output = clearscout_live.clearscout_live_solver()(sample)

    # The stage saw the positional vocabulary; outcomes return under the sample ids.
    assert observed_ids == [["r01", "r02"]]
    assert "completion" not in output
    assert [(item["requirement_id"], item["completion"]) for item in output["per_requirement"]] == [
        ("delivery", "DIRECT"),
        ("collaboration", "GAP"),
    ]
    assert output["per_requirement"][0]["metadata"]["entry_keys"] == ["syn-e1"]
    assert [item["metadata"]["named_tools"] for item in output["per_requirement"]] == [
        ["ToolX"],
        [],
    ]
    assert [item["metadata"]["strength"] for item in output["per_requirement"]] == [
        "strong",
        "none",
    ]
    assert output["metadata"]["actual_dollars"] == pytest.approx(0.0032)


def test_live_solver_continues_above_the_advisory_estimate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, durable_database_url: str
) -> None:
    _live_env(monkeypatch, tmp_path, cents="1")
    _billable(monkeypatch, durable_database_url)
    monkeypatch.setattr(
        clearscout_live,
        "find_credential",
        lambda provider: Credential(
            key="test-key", provider=SUITE_PROVIDER, source=Source.ENVIRONMENT
        ),
    )
    monkeypatch.setattr(eval_client, "provider_for", lambda provider, key: FakeProvider())
    calls = 0

    def measured_match(client: Any, inventory: Any, spec: Any, guardrails: Any) -> Any:
        nonlocal calls
        calls += 1
        client.ledger.record(
            CallManifest(
                stage="match",
                model="gpt-5.6-terra",
                served="gpt-5.6-terra",
                provider="openai",
                service_tier="default",
                finish="completed",
                usage=Usage(input_tokens=1_000, output_tokens=100),
            )
        )
        return EvidenceMap()

    monkeypatch.setattr(clearscout_live, "match", measured_match)
    inventory = synthetic_inventory()
    sample = {
        "requirement_text": "Synthetic requirement",
        "dimension": "hard",
        "cv_hash": inventory.content_hash,
    }
    solver = clearscout_live.clearscout_live_solver()

    for _ in range(4):
        solver(sample)
    solver(sample)
    assert calls == 5
    assert solver.ledger.total_dollars is not None
    assert solver.ledger.total_dollars > 0.01


def test_live_solver_continues_after_an_unpriced_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, durable_database_url: str
) -> None:
    _live_env(monkeypatch, tmp_path)
    _billable(monkeypatch, durable_database_url)
    monkeypatch.setattr(
        clearscout_live,
        "find_credential",
        lambda provider: Credential(
            key="test-key", provider=SUITE_PROVIDER, source=Source.ENVIRONMENT
        ),
    )
    monkeypatch.setattr(eval_client, "provider_for", lambda provider, key: FakeProvider())

    def unpriced_match(client: Any, inventory: Any, spec: Any, guardrails: Any) -> Any:
        client.ledger.record(
            CallManifest(
                stage="match",
                model="unpriced-model",
                provider="openai",
                finish="completed",
                usage=Usage(output_tokens=1),
            )
        )
        return EvidenceMap()

    monkeypatch.setattr(clearscout_live, "match", unpriced_match)
    inventory = synthetic_inventory()
    sample = {
        "requirement_text": "Synthetic requirement",
        "dimension": "hard",
        "cv_hash": inventory.content_hash,
    }
    solver = clearscout_live.clearscout_live_solver()

    first = solver(sample)
    assert first["metadata"]["actual_dollars"] is None
    second = solver(sample)
    assert second["metadata"]["actual_dollars"] is None
    assert len(solver.ledger.entries) == 2


def test_live_solver_uses_one_immutable_prompt_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, durable_database_url: str
) -> None:
    _live_env(monkeypatch, tmp_path)
    _billable(monkeypatch, durable_database_url)
    prompt_a = {
        "match_system": packaged("match_system") + "\nSnapshot A.\n",
        "match_user": packaged("match_user") + "\nSnapshot A.\n",
    }
    prompt_b = {
        "match_system": packaged("match_system") + "\nSnapshot B.\n",
        "match_user": packaged("match_user") + "\nSnapshot B.\n",
    }
    current = dict(prompt_a)
    monkeypatch.setattr(clearscout_live, "load", lambda name: current[name])
    monkeypatch.setattr(
        clearscout_live,
        "find_credential",
        lambda provider: Credential(
            key="test-key", provider=SUITE_PROVIDER, source=Source.ENVIRONMENT
        ),
    )
    monkeypatch.setattr(eval_client, "provider_for", lambda provider, key: FakeProvider())
    observed: list[str] = []

    def observe_prompt(client: Any, inventory: Any, spec: Any, guardrails: Any) -> Any:
        observed.append(load_prompt("match_system"))
        return EvidenceMap()

    monkeypatch.setattr(clearscout_live, "match", observe_prompt)
    solver_a = clearscout_live.clearscout_live_solver()
    inventory = synthetic_inventory()
    sample = {
        "requirement_text": "Synthetic requirement",
        "dimension": "hard",
        "cv_hash": inventory.content_hash,
    }

    solver_a(sample)
    solver_a(sample)
    current.update(prompt_b)
    with pytest.raises(RuntimeError, match="prompt bytes changed"):
        solver_a(sample)
    solver_b = clearscout_live.clearscout_live_solver()

    with overriding(prompt_a):
        fingerprint_a = suite_fingerprint(("match_system", "match_user"))
    with overriding(prompt_b):
        fingerprint_b = suite_fingerprint(("match_system", "match_user"))
    assert observed == [prompt_a["match_system"], prompt_a["match_system"]]
    assert solver_a.run_metadata["prompt_suite_fingerprint"] == fingerprint_a
    assert solver_b.run_metadata["prompt_suite_fingerprint"] == fingerprint_b
    assert load_prompt("match_system") != prompt_a["match_system"]


def test_runner_requires_product_question_symbol_on_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task without PRODUCT_QUESTION fails before scoring so gap analysis stays honest."""
    dataset = tmp_path / "match.jsonl"
    _write_dataset(dataset, [{"id": "dev-1", "split": "development"}])
    # SimpleNamespace here, not FakeTaskModule: this test verifies the runner
    # rejects a task module missing PRODUCT_QUESTION, so the double must be
    # able to omit that attribute entirely (a dataclass would require it).
    task = SimpleNamespace(
        TASK_NAME="match-live",
        DATASET_PATH=dataset,
        solver=lambda: lambda sample: {"completion": "GAP", "metadata": {}},
        scorer=lambda: lambda sample, output: {"value": True},
    )
    monkeypatch.setattr(runner, "_load_task_module", lambda reference: task)

    with pytest.raises(AttributeError, match="PRODUCT_QUESTION"):
        runner.run_task(
            "synthetic-task",
            log_dir=tmp_path / "logs",
            limit=1,
            split="development",
        )


def test_runner_refuses_an_unknown_product_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task cannot claim coverage outside the closed product-question rubric."""
    dataset = tmp_path / "match.jsonl"
    _write_dataset(dataset, [{"id": "dev-1", "split": "development"}])
    task = FakeTaskModule(
        TASK_NAME="match-live",
        DATASET_PATH=dataset,
        PRODUCT_QUESTION="invented_question",
        solver=lambda: lambda sample: {"completion": "GAP", "metadata": {}},
        scorer=lambda: lambda sample, output: {"value": True},
    )
    monkeypatch.setattr(runner, "_load_task_module", lambda reference: task)

    with pytest.raises(ValueError, match="invented_question"):
        runner.run_task(
            "synthetic-task",
            log_dir=tmp_path / "logs",
            limit=1,
            split="development",
        )


def test_runner_refuses_log_dir_disagreeing_with_recorded_output_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live run pointing --log-dir somewhere other than the approved output_dir fails closed."""
    dataset = tmp_path / "match.jsonl"
    _write_dataset(dataset, [{"id": "dev-1", "split": "development"}])
    other_output = tmp_path / "elsewhere"
    other_output.mkdir()
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    solver_calls = 0

    def solve(sample: dict[str, Any]) -> dict[str, Any]:
        nonlocal solver_calls
        solver_calls += 1
        return {
            "completion": "GAP",
            "metadata": {"entry_keys": [], "run": {"output_dir": str(other_output)}},
        }

    cast(Any, solve).run_metadata = {"output_dir": str(other_output)}

    task = FakeTaskModule(
        TASK_NAME="match-live",
        DATASET_PATH=dataset,
        PRODUCT_QUESTION=ProductQuestion.REQUIREMENT_CLASSIFICATION,
        solver=lambda: solve,
        scorer=lambda: lambda sample, output: {"value": True},
    )
    monkeypatch.setattr(runner, "_load_task_module", lambda reference: task)

    with pytest.raises(ValueError, match="does not match the approved output_dir"):
        runner.run_task(
            "synthetic-task",
            log_dir=log_dir,
            limit=1,
            split="development",
        )
    assert solver_calls == 0


@pytest.mark.parametrize(
    ("run_metadata", "limit", "split", "message"),
    [
        ({"dataset_split": "holdout"}, 1, "development", "approved dataset split"),
        ({"sample_limit": 2}, 1, "development", "approved sample limit"),
        ({"dataset_sha256": "0" * 64}, 1, "development", "dataset bytes"),
    ],
)
def test_runner_refuses_scope_disagreeing_with_live_approval_before_scoring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_metadata: dict[str, Any],
    limit: int,
    split: str,
    message: str,
) -> None:
    """Approved live scope is fixed before any sample reaches the solver."""
    dataset = tmp_path / "match.jsonl"
    _write_dataset(dataset, [{"id": "dev-1", "split": "development"}])
    solver_calls = 0

    def solve(sample: dict[str, Any]) -> dict[str, Any]:
        nonlocal solver_calls
        solver_calls += 1
        return {"completion": "GAP", "metadata": {"run": run_metadata}}

    cast(Any, solve).run_metadata = run_metadata
    task = FakeTaskModule(
        TASK_NAME="match-live",
        DATASET_PATH=dataset,
        PRODUCT_QUESTION=ProductQuestion.REQUIREMENT_CLASSIFICATION,
        solver=lambda: solve,
        scorer=lambda: lambda sample, output: {"value": True},
    )
    monkeypatch.setattr(runner, "_load_task_module", lambda reference: task)

    with pytest.raises(ValueError, match=message):
        runner.run_task("synthetic-task", log_dir=tmp_path, limit=limit, split=split)
    assert solver_calls == 0


def test_runner_repeats_each_sample_once_per_approved_trial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three trials of one input land as three rows a reader can tell apart."""
    dataset = tmp_path / "match.jsonl"
    _write_dataset(dataset, [{"id": "dev-1", "split": "development"}, {"id": "dev-2"}])
    run_metadata = {"trials": 3}
    answers = iter(["DIRECT", "GAP", "DIRECT", "GAP", "GAP", "DIRECT"])

    def solve(sample: dict[str, Any]) -> dict[str, Any]:
        return {"completion": next(answers), "metadata": {"run": run_metadata}}

    cast(Any, solve).run_metadata = run_metadata
    task = FakeTaskModule(
        TASK_NAME="match-live",
        DATASET_PATH=dataset,
        PRODUCT_QUESTION=ProductQuestion.REQUIREMENT_CLASSIFICATION,
        solver=lambda: solve,
        scorer=lambda: lambda sample, output: {"value": output["completion"] == "DIRECT"},
    )
    monkeypatch.setattr(runner, "_load_task_module", lambda reference: task)

    log_path = runner.run_task("synthetic-task", log_dir=tmp_path / "logs", trials=3)

    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    header, rows = records[0], records[1:]
    assert header["trials"] == 3
    assert header["total"] == 6
    assert header["passed"] == 3
    assert len({(row["id"], row["trial"]) for row in rows}) == 6
    assert [row["trial"] for row in rows if row["id"] == "dev-1"] == [0, 1, 2]
    assert [row["solver_output"]["completion"] for row in rows if row["id"] == "dev-1"] == [
        "DIRECT",
        "GAP",
        "DIRECT",
    ]


def test_runner_refuses_a_trial_count_disagreeing_with_live_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run repeating samples the approval did not cover fails before any solver call."""
    dataset = tmp_path / "match.jsonl"
    _write_dataset(dataset, [{"id": "dev-1", "split": "development"}])
    run_metadata = {"trials": 3}
    solver_calls = 0

    def solve(sample: dict[str, Any]) -> dict[str, Any]:
        nonlocal solver_calls
        solver_calls += 1
        return {"completion": "GAP", "metadata": {"run": run_metadata}}

    cast(Any, solve).run_metadata = run_metadata
    task = FakeTaskModule(
        TASK_NAME="match-live",
        DATASET_PATH=dataset,
        PRODUCT_QUESTION=ProductQuestion.REQUIREMENT_CLASSIFICATION,
        solver=lambda: solve,
        scorer=lambda: lambda sample, output: {"value": True},
    )
    monkeypatch.setattr(runner, "_load_task_module", lambda reference: task)

    with pytest.raises(ValueError, match="approved trial count"):
        runner.run_task("synthetic-task", log_dir=tmp_path / "logs", trials=2)
    assert solver_calls == 0


def _armed_task(dataset: Path, run_metadata: dict[str, Any], seen: list[dict[str, Any]]) -> Any:
    """A task whose solver declares the run metadata given and records what it saw."""

    def solve(sample: dict[str, Any]) -> dict[str, Any]:
        seen.append(sample)
        return {"completion": "GAP", "metadata": {"run": run_metadata}}

    cast(Any, solve).run_metadata = run_metadata
    return FakeTaskModule(
        TASK_NAME="relevance-live",
        DATASET_PATH=dataset,
        PRODUCT_QUESTION=ProductQuestion.FIT_RECOMMENDATION,
        solver=lambda: solve,
        scorer=lambda: lambda sample, output: {"value": True},
    )


def _two_arms() -> dict[str, Any]:
    return {
        "trials": 2,
        "arms": 2,
        "prompt_arms": [
            {"arm": "baseline", "prompt_suite_fingerprint": "a" * 16},
            {"arm": "candidate", "prompt_suite_fingerprint": "b" * 16},
        ],
    }


def test_runner_interleaves_two_declared_prompt_arms_within_one_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both arms run against every sample, alternating, and every row names its arm."""
    dataset = tmp_path / "relevance.jsonl"
    _write_dataset(dataset, [{"id": "case-1"}, {"id": "case-2"}])
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(
        runner, "_load_task_module", lambda reference: _armed_task(dataset, _two_arms(), seen)
    )

    log_path = runner.run_task("armed-task", log_dir=tmp_path / "logs", trials=2, arms=2)

    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    header, rows = records[0], records[1:]
    assert header["arms"] == 2
    assert header["trials"] == 2
    assert header["total"] == 8
    assert [sample["arm"] for sample in seen[:4]] == [
        "baseline",
        "candidate",
        "baseline",
        "candidate",
    ]
    first = [row for row in rows if row["id"] == "case-1"]
    assert [(row["trial"], row["arm"]) for row in first] == [
        (0, "baseline"),
        (1, "candidate"),
        (2, "baseline"),
        (3, "candidate"),
    ]
    assert len({(row["id"], row["trial"]) for row in rows}) == 8


class _BatchingSolver:
    """Answer every sample of a repeat in one call, in reverse, recording each call."""

    def __init__(self, run_metadata: dict[str, Any]) -> None:
        self.run_metadata = run_metadata
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, sample: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("a batching solver is handed a whole repeat")

    def batch(self, samples: list[dict[str, Any]]) -> Any:
        """Yield each sample's answer, naming it by position."""
        self.calls.append(([sample["id"] for sample in samples], samples[0].get("arm")))
        for position in reversed(range(len(samples))):
            completion = "DIRECT" if samples[position]["id"] == "case-1" else "GAP"
            yield position, {"completion": completion, "metadata": {"run": self.run_metadata}}


def test_a_batching_solver_is_called_once_per_repeat_and_scored_per_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each arm and trial is one call over every sample, and each row keeps its identity."""
    dataset = tmp_path / "relevance.jsonl"
    _write_dataset(dataset, [{"id": "case-1"}, {"id": "case-2"}])
    solver = _BatchingSolver(_two_arms())
    task = FakeTaskModule(
        TASK_NAME="relevance-live",
        DATASET_PATH=dataset,
        PRODUCT_QUESTION=ProductQuestion.FIT_RECOMMENDATION,
        solver=lambda: solver,
        scorer=lambda: lambda sample, output: {"value": output["completion"] == "DIRECT"},
    )
    monkeypatch.setattr(runner, "_load_task_module", lambda reference: task)

    log_path = runner.run_task("batched-task", log_dir=tmp_path / "logs", trials=2, arms=2)

    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    header, rows = records[0], records[1:]
    assert solver.calls == [
        (["case-1", "case-2"], "baseline"),
        (["case-1", "case-2"], "candidate"),
        (["case-1", "case-2"], "baseline"),
        (["case-1", "case-2"], "candidate"),
    ]
    assert (header["total"], header["passed"]) == (8, 4)
    assert sorted(
        (row["id"], row["trial"], row["arm"]) for row in rows if row["id"] == "case-1"
    ) == [
        ("case-1", 0, "baseline"),
        ("case-1", 1, "candidate"),
        ("case-1", 2, "baseline"),
        ("case-1", 3, "candidate"),
    ]
    assert all(
        row["score"]["value"] == (row["id"] == "case-1")
        and set(row) == {"id", "trial", "arm", "score", "solver_output"}
        for row in rows
    )


@pytest.mark.parametrize(
    ("run_metadata", "trials", "arms"),
    [({"provider": "openai"}, 1, 2), (_two_arms(), 2, 1), (_two_arms(), 2, 3)],
)
def test_runner_refuses_an_arm_count_the_solver_did_not_declare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_metadata: dict[str, Any],
    trials: int,
    arms: int,
) -> None:
    """The arms a run pays for are the ones its solver declared, checked before any call."""
    dataset = tmp_path / "relevance.jsonl"
    _write_dataset(dataset, [{"id": "case-1"}])
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(
        runner, "_load_task_module", lambda reference: _armed_task(dataset, run_metadata, seen)
    )

    with pytest.raises(ValueError, match="does not match the declared prompt arms"):
        runner.run_task("armed-task", log_dir=tmp_path / "logs", trials=trials, arms=arms)
    assert seen == []


def test_a_run_declaring_one_prompt_version_labels_no_arm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default run keeps the rows, the header, and the solver input it always had."""
    dataset = tmp_path / "relevance.jsonl"
    _write_dataset(dataset, [{"id": "case-1"}])
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(
        runner,
        "_load_task_module",
        lambda reference: _armed_task(dataset, {"trials": 2}, seen),
    )

    log_path = runner.run_task("armed-task", log_dir=tmp_path / "logs", trials=2)

    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert "arms" not in records[0]
    assert [set(row) for row in records[1:]] == [{"id", "trial", "score", "solver_output"}] * 2
    assert all("arm" not in sample for sample in seen)


def test_a_run_without_trials_records_no_trial_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One trial per sample is the unrepeated run, so its rows carry no index."""
    dataset = tmp_path / "match.jsonl"
    _write_dataset(dataset, [{"id": "dev-1", "split": "development"}])
    task = FakeTaskModule(
        TASK_NAME="match-live",
        DATASET_PATH=dataset,
        PRODUCT_QUESTION=ProductQuestion.REQUIREMENT_CLASSIFICATION,
        solver=lambda: lambda sample: {"completion": "GAP", "metadata": {"entry_keys": []}},
        scorer=lambda: lambda sample, output: {"value": True},
    )
    monkeypatch.setattr(runner, "_load_task_module", lambda reference: task)

    log_path = runner.run_task("synthetic-task", log_dir=tmp_path / "logs")

    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert "trials" not in records[0]
    assert set(records[1]) == {"id", "score", "solver_output"}


def test_runner_reports_requirement_counts_beside_sample_counts(tmp_path: Path) -> None:
    """A whole-posting run reports one sample and five requirement outcomes, separately."""
    log_path = runner.run_task(
        "evals.inspect.tasks.match_scripted_multi_task",
        log_dir=tmp_path / "logs",
    )

    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    header = records[0]
    assert header["total"] == 1
    assert header["passed"] == 1
    assert header["requirement_total"] == 5
    assert header["requirement_passed"] == 5
    row_score = records[1]["score"]
    assert row_score["requirements"]["total"] == 5
    assert {result["requirement_id"] for result in row_score["requirements"]["results"]} == {
        "delivery",
        "orchestration",
        "adjacent-tool",
        "work-auth",
        "mentoring",
    }


def test_solver_input_carries_a_validated_job_spec_but_no_labels() -> None:
    """A whole-posting sample reaches the solver as a spec, without expected labels."""
    sample = {
        "id": "multi",
        "cv_hash": "synthetic-hash",
        "job_spec": {
            "title": "Role",
            "company": "Aster Systems",
            "seniority": None,
            "location": None,
            "requirements": [
                {"id": "delivery", "text": "Deliver a service.", "importance": "must_have"}
            ],
            "keywords": [],
            "hard_gates": [],
            "origin": "test",
        },
        "expected_classifications": {"delivery": "DIRECT"},
        "metadata": {"scripted_matches": {"delivery": "DIRECT"}},
    }

    projected = runner._solver_input(sample)

    assert "expected_classifications" not in projected
    assert projected["metadata"] == {"scripted_matches": {"delivery": "DIRECT"}}
    assert [item["id"] for item in projected["job_spec"]["requirements"]] == ["delivery"]


def test_solver_input_refuses_a_label_nested_in_a_job_spec_requirement() -> None:
    """A label smuggled into the spec fails closed rather than reaching the solver."""
    sample = {
        "job_spec": {
            "title": "Role",
            "company": None,
            "seniority": None,
            "location": None,
            "requirements": [
                {
                    "id": "delivery",
                    "text": "Deliver a service.",
                    "importance": "must_have",
                    "expected_classification": "DIRECT",
                }
            ],
            "keywords": [],
            "hard_gates": [],
            "origin": "test",
        },
    }

    with pytest.raises(ValueError, match="unsupported fields"):
        runner._solver_input(sample)


def _multi_output(*outcomes: dict[str, Any]) -> dict[str, Any]:
    return {"per_requirement": list(outcomes)}


def _outcome(requirement_id: str, completion: str, *, complete: bool = True) -> dict[str, Any]:
    entry_keys = [] if completion in {"GAP", "ASSERTED"} else ["syn-e1"]
    return {
        "requirement_id": requirement_id,
        "completion": completion,
        "metadata": {"entry_keys": entry_keys, "outcome_complete": complete},
    }


def _multi_sample() -> dict[str, Any]:
    return {"metadata": {"scripted_matches": {"a": "DIRECT", "b": "GAP"}}}


def test_per_requirement_scorer_passes_full_coverage() -> None:
    result = per_requirement_scripted_contract()(
        _multi_sample(), _multi_output(_outcome("a", "DIRECT"), _outcome("b", "GAP"))
    )

    assert result["value"] is True
    assert result["requirements"] == {
        "total": 2,
        "passed": 2,
        "results": result["requirements"]["results"],
    }


@pytest.mark.parametrize(
    ("outputs", "message"),
    [
        ([], "no per-requirement outcomes"),
        ([("a", "DIRECT")], "coverage mismatch"),
        ([("a", "DIRECT"), ("a", "GAP")], "duplicate"),
        ([("a", "DIRECT"), ("c", "GAP")], "coverage mismatch"),
    ],
)
def test_per_requirement_scorer_fails_coverage_explicitly(
    outputs: list[tuple[str, str]], message: str
) -> None:
    outcomes = [_outcome(rid, label) for rid, label in outputs]
    result = per_requirement_scripted_contract()(_multi_sample(), _multi_output(*outcomes))

    assert result["value"] is False
    assert result["requirements"]["total"] == 2
    assert result["requirements"]["passed"] == 0
    assert message in result["explanation"]


def test_per_requirement_scorer_does_not_credit_a_defaulted_gap() -> None:
    """A requirement the run could not establish fails even though its label reads GAP."""
    result = per_requirement_scripted_contract()(
        _multi_sample(),
        _multi_output(_outcome("a", "DIRECT"), _outcome("b", "GAP", complete=False)),
    )

    assert result["value"] is False
    assert result["requirements"]["passed"] == 1
    incomplete = next(
        item for item in result["requirements"]["results"] if item["requirement_id"] == "b"
    )
    assert incomplete["outcome_complete"] is False
    assert incomplete["value"] is False


def test_runner_scores_the_same_dataset_snapshot_it_approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = tmp_path / "match.jsonl"
    original = [{"id": "approved", "split": "development"}]
    replacement = [{"id": "replaced", "split": "development"}]
    _write_dataset(dataset, original)
    approved_bytes = dataset.read_bytes()
    seen: list[str] = []

    def solve(sample: dict[str, Any]) -> dict[str, Any]:
        seen.append(str(sample["id"]))
        return {"completion": "GAP", "metadata": {"run": run_metadata}}

    run_metadata = {"dataset_sha256": bytes_sha256(approved_bytes)}
    cast(Any, solve).run_metadata = run_metadata

    def solver() -> Any:
        _write_dataset(dataset, replacement)
        return solve

    task = FakeTaskModule(
        TASK_NAME="match-live",
        DATASET_PATH=dataset,
        PRODUCT_QUESTION=ProductQuestion.REQUIREMENT_CLASSIFICATION,
        solver=solver,
        scorer=lambda: lambda sample, output: {"value": True},
    )
    monkeypatch.setattr(runner, "_load_task_module", lambda reference: task)

    runner.run_task("synthetic-task", log_dir=tmp_path / "logs", split="development")

    assert seen == ["approved"]
