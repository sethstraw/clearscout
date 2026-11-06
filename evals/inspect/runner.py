"""Bespoke in-repo runner for the evaluation harness.

Loads a task module by dotted path or file path, iterates its
``DATASET_PATH`` JSONL, calls ``solver()(row)`` on each row, applies
``scorer()(row, solver_output)``, and writes one JSONL evaluation log. A solver
exposing ``batch(rows, trial)`` is instead handed every selected row once per
repeat and yields ``(position, output)`` pairs, a ``FailedCall`` in place of
the output of a call answered unusably.
No third-party framework: the license policy in this repository refuses the
dev-tier exception ``inspect-ai`` would have needed for its transitive
``agent-client-protocol``.

Log shape (each line is one JSON object):
1. Header: task, dataset, selected scope, result counts, the per-label counts a
   reader acts on, and live-run identity when present.
2. One line per sample: ``{"id": str, "score": {...}, "solver_output": {...}}``,
   carrying ``trial`` when the run repeated its samples and ``arm`` when it
   declared more than one prompt version. A repeat whose model spent its output
   budget before answering is a failing row whose score names that ``failure``.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import sys
from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from clearscout.infrastructure.providers.base import Finish, UnusableResponseError
from clearscout.shared.errors import ClearscoutError
from evals.inspect.labeling import bytes_sha256
from evals.inspect.outcomes import BatchOutcome, FailedCall
from evals.inspect.product_questions import ProductQuestion
from evals.retrieval_metrics import Rate
from evals.semantic import StageMeasurement
from evals.support.jobspec import canonical_job_spec

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOG_DIR = REPO_ROOT / "reports" / "eval"

# A task declares which of these it is. `recorded` families produce a verdict a
# maintainer reads, so the runner reports their tallies and no rate.
JUDGED = "judged"
RECORDED = "recorded"
_SCORE_KINDS = frozenset({JUDGED, RECORDED})
OUTPUT_BUDGET_EXHAUSTED = "output_budget_exhausted"
HOLDOUT_SPLIT = "holdout"
SPLITS = ("development", "regression", HOLDOUT_SPLIT)

Solver = Callable[[dict[str, Any]], dict[str, Any]]
Scorer = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]

_SOLVER_INPUT_FIELDS = frozenset(
    {
        "id",
        "input",
        "requirement_text",
        "dimension",
        "cv_hash",
        "requirement_key",
        "posting_id",
        "split",
    }
)
_SOLVER_METADATA_FIELDS = frozenset(
    {
        "cv",
        "database_url",
        "evaluation_id",
        "evidence",
        "matches",
        "posting_id",
        "requirement_key",
        "research",
        "scripted_classification",
        "scripted_draft",
        "scripted_letter",
        "scripted_matches",
        "scripted_verdict",
    }
)


def _load_task_module(reference: str) -> Any:
    """Load a task module by dotted path or by file path."""
    path = Path(reference)
    if path.exists():
        resolved = path.resolve()
        spec = importlib.util.spec_from_file_location(f"evals_task_{resolved.stem}", resolved)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load task module at {resolved}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    return importlib.import_module(reference)


def _iter_samples(
    dataset_content: bytes,
    *,
    limit: int | None = None,
    split: str | None = None,
    ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Select the samples one run scores, in dataset order.

    ``ids`` names samples explicitly and refuses any it cannot find, so a run
    can never quietly score fewer samples than it asked for.
    """
    if limit is not None and limit < 1:
        raise ValueError("evaluation limit must be at least 1")
    if ids is not None:
        if limit is not None:
            raise ValueError("evaluation refused: --ids and --limit select samples two ways")
        if not ids:
            raise ValueError("evaluation sample ids must name at least one sample")
    wanted = set(ids) if ids is not None else None
    selected: list[dict[str, Any]] = []
    for index, line in enumerate(dataset_content.decode("utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        record = json.loads(stripped)
        record.setdefault("id", f"sample-{index}")
        if wanted is not None and str(record["id"]) not in wanted:
            continue
        # Only a run that names the holdout split reads a held-out row.
        if split != HOLDOUT_SPLIT and record.get("split") == HOLDOUT_SPLIT:
            continue
        if split is not None:
            sample_split = record.get("split")
            if not isinstance(sample_split, str) or not sample_split.strip():
                raise ValueError(f"sample {record['id']!r} does not declare a split")
            if sample_split != split:
                continue
        selected.append(record)
        if limit is not None and len(selected) >= limit:
            break
    if wanted is not None:
        missing = wanted - {str(record["id"]) for record in selected}
        if missing:
            named = ", ".join(sorted(missing))
            raise ValueError(f"evaluation dataset has no sample for the selected ids: {named}")
    return selected


def selected_samples(
    task_reference: str,
    *,
    limit: int | None = None,
    split: str | None = None,
    ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Return the samples a run of the task's own dataset would score."""
    module = _load_task_module(task_reference)
    return dataset_samples(Path(module.DATASET_PATH), limit=limit, split=split, ids=ids)


def dataset_samples(
    dataset: Path,
    *,
    limit: int | None = None,
    split: str | None = None,
    ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Return the samples a run of this dataset would score."""
    return _iter_samples(dataset.read_bytes(), limit=limit, split=split, ids=ids)


def _score_rows(
    dataset_content: bytes,
    solver: Solver,
    scorer: Scorer,
    *,
    limit: int | None = None,
    split: str | None = None,
    ids: Sequence[str] | None = None,
    trials: int = 1,
    arms: Sequence[str | None] = (None,),
    row_sink: Callable[[dict[str, Any]], None] | None = None,
    run_metadata: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    if trials < 1:
        raise ValueError("evaluation trials must be at least 1")
    rows: list[dict[str, Any]] = []
    passed = 0
    total = 0
    repeats = trials * len(arms)
    samples = _iter_samples(dataset_content, limit=limit, split=split, ids=ids)
    # A malformed expected label stops the run here, before any solver call is billed.
    validate_sample = getattr(scorer, "validate_sample", None)
    if callable(validate_sample):
        for sample in samples:
            validate_sample(sample)
    batch = getattr(solver, "batch", None)
    outcomes = (
        _batched_outcomes(batch, samples, repeats, arms)
        if callable(batch)
        else _single_outcomes(solver, samples, repeats, arms)
    )
    # One truncated batch call fails every member; only its first row carries the usage.
    measured: list[UnusableResponseError] = []
    for sample, repeat, outcome in outcomes:
        arm = arms[repeat % len(arms)]
        identity = _row_identity(sample.get("id"), repeat, repeats, arm)
        exhausted: UnusableResponseError | None = None
        if isinstance(outcome, FailedCall):
            # A truncated answer scores as a failed row; every other error
            # stops the run, so an outage never scores as a model failure.
            if outcome.error.record.finish is not Finish.TRUNCATED:
                raise outcome.error
            exhausted = outcome.error
            first = not any(seen is exhausted for seen in measured)
            measured.append(exhausted)
            solver_output = _exhausted_output(
                exhausted, run_metadata or {}, measured=first, batch=outcome.batch
            )
        else:
            solver_output = outcome
        if row_sink is not None:
            row_sink({**identity, "solver_output": solver_output})
        score = (
            scorer(sample, solver_output)
            if exhausted is None
            else _exhausted_score(exhausted, run_metadata or {}, scorer, sample)
        )
        total += 1
        if bool(score.get("value")):
            passed += 1
        rows.append({**identity, "score": score, "solver_output": solver_output})
    return rows, total, passed


def _single_outcomes(
    solver: Solver,
    samples: Sequence[dict[str, Any]],
    repeats: int,
    arms: Sequence[str | None],
) -> Iterator[tuple[dict[str, Any], int, BatchOutcome]]:
    """Call the solver once per sample and repeat, samples outer."""
    for sample in samples:
        for repeat in range(repeats):
            try:
                yield sample, repeat, solver(_solver_input(sample, arms[repeat % len(arms)]))
            except UnusableResponseError as error:
                yield sample, repeat, FailedCall(error)


def _batched_outcomes(
    batch: Callable[..., Iterable[tuple[int, BatchOutcome]]],
    samples: Sequence[dict[str, Any]],
    repeats: int,
    arms: Sequence[str | None],
) -> Iterator[tuple[dict[str, Any], int, BatchOutcome]]:
    """Call a batching solver once per repeat over every sample, yielding rows as calls return."""
    for repeat in range(repeats):
        arm = arms[repeat % len(arms)]
        inputs = [_solver_input(sample, arm) for sample in samples]
        for position, outcome in batch(inputs):
            yield samples[position], repeat, outcome


def _row_identity(sample_id: Any, repeat: int, repeats: int, arm: str | None) -> dict[str, Any]:
    """Name one emitted row: its sample, its repeat index, and its prompt arm.

    Arms alternate within the repeat index, so ``repeat // arms`` pairs the rows
    one trial of every arm produced, and an unrepeated run records no index.
    """
    identity: dict[str, Any] = {"id": sample_id}
    if repeats > 1:
        identity["trial"] = repeat
    if arm is not None:
        identity["arm"] = arm
    return identity


def _exhausted_output(
    error: UnusableResponseError,
    run_metadata: dict[str, Any],
    *,
    measured: bool = True,
    batch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Stand in for the output of a call that spent its whole output budget.

    The call was billed, so its reported usage is kept as a measurement on one
    row (``measured``); its price stays unknown here because the solver's
    ledger priced it.
    """
    stage = str(run_metadata.get("stage") or "")
    record = error.record
    model = str(run_metadata.get("requested_model") or record.model)
    metadata: dict[str, Any] = {"measurements": []}
    if measured and stage and model:
        measurement = StageMeasurement(
            stage=stage,
            model=model,
            served_model=record.model,
            usage=record.usage,
            service_tier=record.service_tier,
        )
        metadata["measurements"].append(measurement.model_dump(mode="json"))
    if batch is not None:
        metadata["batch"] = batch
    # _run_metadata refuses a run once any row carries an identity another lacks.
    if run_metadata:
        metadata["run"] = run_metadata
    return {"failure": OUTPUT_BUDGET_EXHAUSTED, "stage": stage, "metadata": metadata}


def _exhausted_score(
    error: UnusableResponseError,
    run_metadata: dict[str, Any],
    scorer: Scorer,
    sample: dict[str, Any],
) -> dict[str, Any]:
    """Fail a repeat whose model spent its output budget before answering.

    A per-requirement scorer's requirements all fail, so the run's
    requirement total still counts them.
    """
    stage = run_metadata.get("stage")
    where = f" in the {stage} stage" if stage else ""
    detail = str(error).splitlines()[0]
    score: dict[str, Any] = {
        "value": False,
        "answer": "",
        "failure": OUTPUT_BUDGET_EXHAUSTED,
        "explanation": f"output budget exhausted{where}: {detail}",
    }
    expected_requirements = getattr(scorer, "expected_requirements", None)
    if callable(expected_requirements):
        score["requirements"] = {
            "total": expected_requirements(sample),
            "passed": 0,
            "results": [],
        }
    return score


def _solver_input(sample: dict[str, Any], arm: str | None = None) -> dict[str, Any]:
    """Project a labeled dataset row onto the fields a solver may observe."""
    projected = {key: sample[key] for key in _SOLVER_INPUT_FIELDS if key in sample}
    if arm is not None:
        projected["arm"] = arm
    if "job_spec" in sample:
        # canonical_job_spec refuses a label nested in the spec, so scorer-only
        # material cannot ride a whole-posting sample into the solver.
        projected["job_spec"] = canonical_job_spec(sample["job_spec"]).model_dump(mode="json")
    metadata = sample.get("metadata")
    if isinstance(metadata, dict):
        safe_metadata = {key: metadata[key] for key in _SOLVER_METADATA_FIELDS if key in metadata}
        if safe_metadata:
            projected["metadata"] = safe_metadata
    return projected


def _requirement_counts(rows: list[dict[str, Any]]) -> tuple[int, int] | None:
    """Sum requirement outcomes across scores, or None if no row carried any."""
    total = 0
    passed = 0
    seen = False
    for row in rows:
        score = row.get("score")
        requirements = score.get("requirements") if isinstance(score, dict) else None
        if isinstance(requirements, dict):
            seen = True
            total += int(requirements.get("total", 0))
            passed += int(requirements.get("passed", 0))
    return (total, passed) if seen else None


def _tally(
    rows: list[dict[str, Any]], read: Callable[[dict[str, Any]], Iterable[str]]
) -> dict[str, int]:
    """Count one label per row, highest count first, so a reader sees the worst first."""
    counts: dict[str, int] = {}
    for row in rows:
        score = row.get("score")
        if not isinstance(score, dict):
            continue
        for label in read(score):
            counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _checks(score: dict[str, Any], outcome: str) -> Iterable[str]:
    checks = score.get("checks")
    named = checks.get(outcome) if isinstance(checks, dict) else None
    return [str(item) for item in named] if isinstance(named, list) else []


def _critic_outcomes(score: dict[str, Any]) -> Iterable[str]:
    critic = score.get("critic")
    outcome = critic.get("outcome") if isinstance(critic, dict) else None
    return [str(outcome)] if isinstance(outcome, str) else []


def _failures(score: dict[str, Any]) -> Iterable[str]:
    failure = score.get("failure")
    return [failure] if isinstance(failure, str) else []


def _axes_that_did_not_decide(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Count rows the axes did not mark, split by whether the score recorded any."""
    counts = {"axes_recorded_not_scored": 0, "axes_not_recorded": 0}
    for row in rows:
        score = row.get("score")
        metadata = score.get("metadata") if isinstance(score, dict) else None
        if not isinstance(metadata, dict) or "axes_scored" not in metadata:
            continue
        scored = metadata["axes_scored"]
        if scored is False:
            counts["axes_recorded_not_scored"] += 1
        elif scored is None:
            counts["axes_not_recorded"] += 1
    return {name: count for name, count in counts.items() if count}


def _boundary_errors(score: dict[str, Any]) -> Iterable[str]:
    metadata = score.get("metadata")
    error = metadata.get("boundary_error") if isinstance(metadata, dict) else None
    return [error] if isinstance(error, str) else []


def _boundary_cases(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Count trials, fired boundaries, and correct answers per case and arm."""
    cases: dict[str, dict[str, int]] = {}
    for row in rows:
        score = row.get("score")
        metadata = score.get("metadata") if isinstance(score, dict) else None
        if not isinstance(metadata, dict) or "observed_boundary" not in metadata:
            continue
        key = str(row.get("id")) if row.get("arm") is None else f"{row['id']}@{row['arm']}"
        case = cases.setdefault(key, {"trials": 0, "boundary_hits": 0, "correct": 0})
        case["trials"] += 1
        case["boundary_hits"] += metadata["observed_boundary"] is not None
        case["correct"] += metadata.get("boundary_error") is None
    return cases


def _agreement(
    rows: list[dict[str, Any]],
    flag: str,
    name: str,
) -> dict[str, int]:
    """Count the rows whose score compared ``flag``, and how many agreed, reported as ``name``."""
    compared = [
        metadata[flag]
        for row in rows
        if isinstance(score := row.get("score"), dict)
        and isinstance(metadata := score.get("metadata"), dict)
        and isinstance(metadata.get(flag), bool)
    ]
    if not compared:
        return {}
    return {f"{name}_agreed": sum(compared), f"{name}_compared": len(compared)}


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the per-label counts a reader acts on.

    Warnings are tallied beside failures because the pass mark is keyed on
    failures alone, so a warned check would otherwise reach nobody who did not
    read the per-sample rows.
    """
    summary: dict[str, Any] = {}
    if failed := _tally(rows, lambda score: _checks(score, "failed")):
        summary["failed_checks"] = failed
    if warned := _tally(rows, lambda score: _checks(score, "warned")):
        summary["warned_checks"] = warned
    if outcomes := _tally(rows, _critic_outcomes):
        summary["critic_outcomes"] = outcomes
    if failures := _tally(rows, _failures):
        summary["failures"] = failures
    if errors := _tally(rows, _boundary_errors):
        summary["boundary_errors"] = errors
    if cases := _boundary_cases(rows):
        summary["boundary_cases"] = cases
    summary.update(_axes_that_did_not_decide(rows))
    summary.update(_agreement(rows, "strength_matches", "strength"))
    summary.update(_agreement(rows, "outcome_matches", "critic_outcome"))
    return summary


def _run_metadata(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the invariant run identity reported by every solver result."""
    identities: list[dict[str, Any]] = []
    for row in rows:
        solver_output = row.get("solver_output")
        metadata = solver_output.get("metadata") if isinstance(solver_output, dict) else None
        identity = metadata.get("run") if isinstance(metadata, dict) else None
        if identity is not None:
            if not isinstance(identity, dict):
                raise ValueError("solver run metadata must be an object")
            identities.append(identity)
    if not identities:
        return {}
    inconsistent = any(identity != identities[0] for identity in identities[1:])
    if len(identities) != len(rows) or inconsistent:
        raise ValueError("solver run metadata must be present and identical on every sample")
    return identities[0]


def _solver_run_metadata(solver: Solver) -> dict[str, Any]:
    """Return run metadata exposed by a solver before it executes."""
    metadata = getattr(solver, "run_metadata", None)
    if metadata is None:
        return {}
    if not isinstance(metadata, dict):
        raise ValueError("solver preflight run metadata must be an object")
    return dict(metadata)


def _declared_arms(run_metadata: dict[str, Any]) -> tuple[str | None, ...]:
    """Return the prompt-arm names a solver declared, or the one unnamed arm."""
    declared = run_metadata.get("prompt_arms")
    if declared is None:
        return (None,)
    if not isinstance(declared, list) or not declared:
        raise ValueError("solver prompt arms must be a non-empty list")
    names: list[str | None] = []
    for entry in declared:
        name = entry.get("arm") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name:
            raise ValueError("every declared prompt arm must carry a name")
        names.append(name)
    if len(set(names)) != len(names):
        raise ValueError("declared prompt arm names must be distinct")
    return tuple(names)


def _verify_run_scope(
    log_dir: Path,
    *,
    dataset_content: bytes,
    limit: int | None,
    split: str | None,
    ids: Sequence[str] | None,
    trials: int,
    arms: int,
    run_metadata: dict[str, Any],
) -> None:
    """Refuse a live run whose runner scope differs from its approval."""
    declared_arms = len(_declared_arms(run_metadata))
    if arms != declared_arms:
        raise ValueError(
            "evaluation refused: --arms does not match the declared prompt arms "
            f"(arms={arms}, declared={declared_arms})"
        )
    recorded = run_metadata.get("output_dir")
    if isinstance(recorded, str) and recorded and log_dir.resolve() != Path(recorded).resolve():
        raise ValueError(
            "evaluation refused: --log-dir does not match the approved output_dir "
            f"(log_dir={log_dir}, output_dir={recorded})"
        )
    approved_split = run_metadata.get("dataset_split")
    if approved_split is not None and split != approved_split:
        raise ValueError(
            "evaluation refused: --split does not match the approved dataset split "
            f"(split={split}, approved_split={approved_split})"
        )
    approved_ids = run_metadata.get("sample_ids")
    if approved_ids is not None and set(ids or ()) != set(approved_ids):
        raise ValueError(
            "evaluation refused: --ids does not match the approved sample ids "
            f"(ids={sorted(ids or ())}, approved_ids={sorted(approved_ids)})"
        )
    approved_limit = run_metadata.get("sample_limit")
    # Naming ids selects exactly that many samples, so they answer the approved
    # count in place of --limit.
    selected = len(ids) if ids is not None else limit
    if approved_limit is not None and selected != approved_limit:
        raise ValueError(
            "evaluation refused: the selection does not match the approved sample limit "
            f"(selected={selected}, approved_limit={approved_limit})"
        )
    approved_trials = run_metadata.get("trials")
    if approved_trials is not None and trials != approved_trials:
        raise ValueError(
            "evaluation refused: --trials does not match the approved trial count "
            f"(trials={trials}, approved_trials={approved_trials})"
        )
    approved_dataset_sha256 = run_metadata.get("dataset_sha256")
    if (
        approved_dataset_sha256 is not None
        and bytes_sha256(dataset_content) != approved_dataset_sha256
    ):
        raise ValueError(
            "evaluation refused: dataset bytes do not match the approved selection manifest"
        )


def _scope_header(
    *,
    limit: int | None = None,
    split: str | None = None,
    ids: Sequence[str] | None = None,
    trials: int = 1,
    arms: int = 1,
) -> dict[str, Any]:
    """Return the selection a run header states, carrying only what the run set."""
    scope: dict[str, Any] = {}
    if limit is not None:
        scope["limit"] = limit
    if ids is not None:
        scope["ids"] = list(ids)
    if split is not None:
        scope["split"] = split
    if trials > 1:
        scope["trials"] = trials
    if arms > 1:
        scope["arms"] = arms
    return scope


def _usd_total(rows: list[dict[str, Any]]) -> tuple[float | None, int]:
    """Return the summed spend live solvers recorded, or None, and the paid rows left unpriced.

    A truncated batch row carries no share of its call, so each such batch's
    call cost is counted once.
    """
    spent: list[float] = []
    unpriced = 0
    batches: set[str] = set()
    for row in rows:
        solver_output = row.get("solver_output")
        if not isinstance(solver_output, dict):
            continue
        metadata = solver_output.get("metadata")
        if not isinstance(metadata, dict):
            continue
        batch = metadata.get("batch")
        if "actual_dollars" in metadata:
            dollars = metadata["actual_dollars"]
        elif "failure" in solver_output and isinstance(batch, dict):
            identity = json.dumps(batch, sort_keys=True)
            if identity in batches:
                continue
            batches.add(identity)
            dollars = batch.get("call_dollars")
        elif "failure" in solver_output:
            dollars = None
        else:
            continue
        if dollars is None:
            unpriced += 1
        else:
            spent.append(float(dollars))
    return (sum(spent) if spent else None), unpriced


def _write_log(
    log_dir: Path,
    *,
    task_name: str,
    dataset_path: Path,
    product_question: str,
    rows: Iterable[dict[str, Any]],
    total: int,
    passed: int,
    scope: dict[str, Any] | None = None,
    run_metadata: dict[str, Any] | None = None,
    requirement_counts: tuple[int, int] | None = None,
    summary: dict[str, Any] | None = None,
    log_path: Path | None = None,
    score_kind: str = JUDGED,
) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    if log_path is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        log_path = log_dir / f"{stamp}-{task_name}.jsonl"
    with log_path.open("w", encoding="utf-8") as handle:
        log_path.chmod(0o600)
        header: dict[str, Any] = {
            "task": task_name,
            "dataset": str(dataset_path),
            "product_question": product_question,
            "score_kind": score_kind,
            "total": total,
        }
        rows = list(rows)
        # A recorded family has no pass mark, so a rate over its rows would be a
        # quality claim it cannot support; its tallies carry the result instead.
        if score_kind == JUDGED:
            header["passed"] = passed
            header["accuracy"] = passed / total if total else 0.0
            if (
                interval := Rate(numerator=passed, denominator=total).wilson_interval()
            ) is not None:
                header["accuracy_wilson95"] = list(interval)
            # Trials repeat a case, so the interval's n overstates independent evidence.
            distinct = len({str(row["id"]) for row in rows if "id" in row})
            if distinct and distinct != total:
                header["distinct_cases"] = distinct
        usd_total, unpriced_rows = _usd_total(rows)
        if usd_total is not None:
            header["usd_total"] = usd_total
        if unpriced_rows:
            header["usd_unpriced_rows"] = unpriced_rows
        if requirement_counts is not None:
            # One sample can carry several requirement outcomes; report both.
            requirement_total, requirement_passed = requirement_counts
            header["requirement_total"] = requirement_total
            header["requirement_passed"] = requirement_passed
        if summary:
            header.update(summary)
        if scope:
            header.update(scope)
        if run_metadata:
            header["run"] = run_metadata
        handle.write(json.dumps(header) + "\n")
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return log_path


class _PartialReceipt:
    """Durably append paid solver results until the final log can be written."""

    def __init__(
        self,
        path: Path,
        *,
        task_name: str,
        dataset_path: Path,
        product_question: str,
        run_metadata: dict[str, Any],
        scope: dict[str, Any],
    ) -> None:
        self.path = path
        self._handle = path.open("x", encoding="utf-8")
        path.chmod(0o600)
        header: dict[str, Any] = {
            "task": task_name,
            "dataset": str(dataset_path),
            "product_question": product_question,
            "status": "running",
            "run": run_metadata,
        }
        header.update(scope)
        self._write(header)

    def _write(self, record: dict[str, Any]) -> None:
        self._handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._handle.flush()
        os.fsync(self._handle.fileno())

    def append(self, row: dict[str, Any]) -> None:
        """Append one solver result immediately after its paid call returns."""
        self._write(row)

    def fail(self, error: Exception) -> None:
        """Record a bounded failure marker without serializing provider content."""
        self._write({"status": "failed", "error_type": type(error).__name__})

    def close(self) -> None:
        """Close the durable sidecar without deleting it."""
        self._handle.close()


def run_task(
    task_reference: str,
    *,
    log_dir: Path = DEFAULT_LOG_DIR,
    dataset_path: Path | None = None,
    limit: int | None = None,
    split: str | None = None,
    ids: Sequence[str] | None = None,
    trials: int = 1,
    arms: int = 1,
) -> Path:
    """Run one task and return the log file path.

    ``limit`` takes the first matching samples and ``ids`` names them instead;
    a run passes one or the other. ``trials`` repeats every selected sample
    that many times, so a run can measure how often one unchanged input
    produces the same answer. ``arms`` is the number of prompt versions the
    solver declared; each sample runs ``trials`` times per arm, with the arms
    alternating, and every row names the arm it came from.
    """
    module = _load_task_module(task_reference)
    for symbol in ("TASK_NAME", "DATASET_PATH", "PRODUCT_QUESTION", "solver", "scorer"):
        if not hasattr(module, symbol):
            raise AttributeError(f"Task module {task_reference!r} is missing symbol {symbol!r}")
    task_name = str(module.TASK_NAME)
    selected_dataset = dataset_path if dataset_path is not None else module.DATASET_PATH
    if selected_dataset is None:
        raise ValueError(
            f"task {task_name!r} requires an explicit --dataset path; "
            "see evals/inspect/METHODOLOGY.md"
        )
    resolved_dataset = Path(selected_dataset)
    if not resolved_dataset.is_file():
        raise FileNotFoundError(
            f"No dataset at {resolved_dataset}. See evals/inspect/METHODOLOGY.md."
        )
    dataset_content = resolved_dataset.read_bytes()
    product_question = ProductQuestion(module.PRODUCT_QUESTION).value
    score_kind = str(getattr(module, "SCORE_KIND", JUDGED))
    if score_kind not in _SCORE_KINDS:
        raise ValueError(
            f"task {task_name!r} declares an unknown SCORE_KIND: {score_kind!r}; "
            f"expected one of {sorted(_SCORE_KINDS)}"
        )
    if trials < 1:
        raise ValueError("evaluation trials must be at least 1")
    if arms < 1:
        raise ValueError("evaluation arms must be at least 1")
    # A task's solver() reads its module DATASET_PATH, so --dataset reaches the solver here.
    default_dataset = module.DATASET_PATH
    module.DATASET_PATH = resolved_dataset
    try:
        solver: Solver = module.solver()
    finally:
        module.DATASET_PATH = default_dataset
    preflight_metadata = _solver_run_metadata(solver)
    _verify_run_scope(
        log_dir,
        dataset_content=dataset_content,
        limit=limit,
        split=split,
        ids=ids,
        trials=trials,
        arms=arms,
        run_metadata=preflight_metadata,
    )
    arm_names = _declared_arms(preflight_metadata)
    scope = _scope_header(limit=limit, split=split, ids=ids, trials=trials, arms=arms)
    log_dir.mkdir(parents=True, exist_ok=True)
    if not log_dir.is_dir():
        raise ValueError(f"evaluation refused: log destination is not a directory: {log_dir}")
    scorer: Scorer = module.scorer()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    log_path = log_dir / f"{stamp}-{task_name}.jsonl"
    receipt = (
        _PartialReceipt(
            log_path.with_suffix(".partial.jsonl"),
            task_name=task_name,
            dataset_path=resolved_dataset,
            product_question=product_question,
            run_metadata=preflight_metadata,
            scope=scope,
        )
        if preflight_metadata
        else None
    )
    try:
        rows, total, passed = _score_rows(
            dataset_content,
            solver,
            scorer,
            limit=limit,
            split=split,
            ids=ids,
            trials=trials,
            arms=arm_names,
            row_sink=receipt.append if receipt is not None else None,
            run_metadata=preflight_metadata,
        )
        if total == 0:
            named_split = f" split {split!r}" if split is not None else ""
            raise ValueError(f"evaluation dataset selected no samples for{named_split}")
        run_metadata = _run_metadata(rows)
        if preflight_metadata and run_metadata != preflight_metadata:
            raise ValueError("solver run metadata changed after preflight")
        summary = _summary(rows)
        log_path = _write_log(
            log_dir,
            task_name=task_name,
            dataset_path=resolved_dataset,
            product_question=product_question,
            rows=rows,
            total=total,
            passed=passed,
            scope=scope,
            run_metadata=run_metadata,
            requirement_counts=_requirement_counts(rows),
            summary=summary,
            log_path=log_path,
            score_kind=score_kind,
        )
    except Exception as error:
        if receipt is not None:
            try:
                receipt.fail(error)
            finally:
                receipt.close()
        raise
    else:
        if receipt is not None:
            receipt.close()
            receipt.path.unlink()
    if score_kind == JUDGED:
        accuracy = passed / total if total else 0.0
        print(f"task={task_name} total={total} accuracy={accuracy:.3f} log={log_path}")
    else:
        print(f"task={task_name} total={total} log={log_path}")
    for label, counts in summary.items():
        if isinstance(counts, int):
            print(f"{label}: {counts}")
            continue
        print(f"{label}: " + ", ".join(f"{name} x{count}" for name, count in counts.items()))
    return log_path


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(
        prog="python -m evals.inspect.runner",
        description="Run one in-repo evaluation task and write a JSONL log.",
    )
    parser.add_argument("task", help="Task module dotted path or file path.")
    parser.add_argument("--dataset", type=Path, help="Explicit task dataset path.")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--limit", type=int)
    selection.add_argument(
        "--ids",
        nargs="+",
        metavar="ID",
        help="Run exactly these sample ids, in dataset order, instead of --limit.",
    )
    parser.add_argument("--split", choices=SPLITS)
    parser.add_argument(
        "--trials", type=int, default=1, help="Run each selected sample this many times."
    )
    parser.add_argument(
        "--arms",
        type=int,
        default=1,
        help="Number of prompt versions the solver declared; trials repeat per arm.",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        run_task(
            args.task,
            log_dir=args.log_dir,
            dataset_path=args.dataset,
            limit=args.limit,
            split=args.split,
            ids=args.ids,
            trials=args.trials,
            arms=args.arms,
        )
    except (
        FileNotFoundError,
        ImportError,
        AttributeError,
        ValueError,
        RuntimeError,
        ClearscoutError,
    ) as error:
        print(f"eval runner error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
