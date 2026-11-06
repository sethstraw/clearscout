"""Fail-closed checks for the paid evaluation shell boundary."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EVAL_SCRIPT = REPO_ROOT / "scripts" / "eval.sh"


def _run_eval(args: list[str], *, environment: dict[str, str] | None = None) -> Any:
    command = ["/bin/bash", str(EVAL_SCRIPT), *args]
    return subprocess.run(  # noqa: S603 - fixed local script with synthetic arguments
        command,
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _stub_uv(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        """#!/bin/sh
case "$*" in
  *evals.inspect.cost_estimate*)
    if [ "${STUB_ESTIMATE_FAIL:-}" = "1" ]; then exit 1; fi
    printf 'estimate for %s\\n' "$*"
    printf 'Estimated cost: $0.56\\n'
    ;;
  *evals.inspect.runner*)
    printf 'split=%s\\n' "$CLEARSCOUT_EVAL_DATASET_SPLIT"
    printf 'limit=%s\\n' "$CLEARSCOUT_EVAL_SAMPLE_LIMIT"
    printf 'sample_ids=%s\\n' "$CLEARSCOUT_EVAL_SAMPLE_IDS"
    printf 'trials=%s\\n' "$CLEARSCOUT_EVAL_TRIALS"
    printf 'model=%s\\n' "$CLEARSCOUT_EVAL_LIVE_MODEL"
    printf 'effort=%s\\n' "$CLEARSCOUT_EVAL_LIVE_EFFORT"
    printf 'prompt_arms=%s\\n' "$CLEARSCOUT_EVAL_PROMPT_ARMS"
    printf 'cv_version=%s\\n' "$CLEARSCOUT_EVAL_CV_VERSION"
    printf 'manifest=%s\\n' "$CLEARSCOUT_EVAL_SELECTION_MANIFEST_SHA256"
    printf 'manifest_bytes=%s\\n' "$CLEARSCOUT_EVAL_SELECTION_MANIFEST_BYTES_SHA256"
    printf 'task=%s\\n' "$*"
    ;;
  *evals.inspect.solvers.relevance_live*)
    printf 'render=%s\\n' "$*"
    printf 'cv_version=%s\\n' "$CLEARSCOUT_EVAL_CV_VERSION"
    printf 'prompt_arms=%s\\n' "$CLEARSCOUT_EVAL_PROMPT_ARMS"
    printf 'database=%s\\n' "$CLEARSCOUT_EVAL_DATABASE_URL"
    ;;
  *) exit 2 ;;
esac
""",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{bin_dir}{os.pathsep}{environment['PATH']}"
    return environment


def _live_args(tmp_path: Path, task: str = "match_live_task.py") -> list[str]:
    return [
        "--live",
        f"evals/inspect/tasks/{task}",
        "--data-root",
        str(tmp_path / "workspace"),
        "--limit",
        "10",
        "--split",
        "development",
        "--provider",
        "openai",
        "--i-approve",
        "0.56",
        "--output-dir",
        str(tmp_path / "private output"),
        "--selection-manifest",
        str(tmp_path / "selection manifest.json"),
        "--selection-manifest-sha256",
        "a" * 64,
        "--selection-manifest-bytes-sha256",
        "b" * 64,
    ]


def test_live_shell_requires_option_values() -> None:
    result = _run_eval(["--live", "--data-root"])

    assert result.returncode == 64
    assert "Missing value for --live option: --data-root" in result.stderr


def _document_live_args(tmp_path: Path, task: str = "draft_live_task.py") -> list[str]:
    """Live arguments for a family that presents no selection manifest."""
    return [
        "--live",
        f"evals/inspect/tasks/{task}",
        "--data-root",
        str(tmp_path / "workspace"),
        "--limit",
        "5",
        "--split",
        "development",
        "--provider",
        "openai",
        "--i-approve",
        "0.56",
        "--output-dir",
        str(tmp_path / "private output"),
    ]


def _declared_stage(task: str) -> str:
    """Return the stage a live task module declares, read the way the shell reads it."""
    text = (REPO_ROOT / "evals" / "inspect" / "tasks" / task).read_text(encoding="utf-8")
    line = next(row for row in text.splitlines() if row.startswith("STAGE = "))
    return line.partition("=")[2].strip().strip('"')


def _estimated_stage(stdout: str) -> str:
    """Return the stage the cost estimate was asked for."""
    line = next(row for row in stdout.splitlines() if row.startswith("estimate for "))
    fields = line.split()
    return fields[fields.index("--stage") + 1]


@pytest.mark.parametrize(
    ("named", "estimated", "sent"),
    [
        ([], "--provider openai --limit", ["model=", "effort="]),
        (["--model", "gpt-6-sol"], "--model gpt-6-sol", ["model=gpt-6-sol", "effort="]),
        (["--effort", "low"], "--effort low", ["model=", "effort=low"]),
        (
            ["--model", "gpt-6-luna", "--effort", "high"],
            "--model gpt-6-luna --effort high",
            ["model=gpt-6-luna", "effort=high"],
        ),
    ],
)
def test_live_shell_hands_the_named_model_and_effort_to_the_estimate_and_the_run(
    tmp_path: Path, named: list[str], estimated: str, sent: list[str]
) -> None:
    """The estimate prices what the run will send, which is the shipped choice when unnamed."""
    result = _run_eval([*_document_live_args(tmp_path), *named], environment=_stub_uv(tmp_path))

    assert result.returncode == 0, result.stderr
    estimate = next(row for row in result.stdout.splitlines() if row.startswith("estimate for "))
    assert estimated in estimate
    assert set(sent) <= set(result.stdout.splitlines())


def test_live_shell_refuses_a_stage_the_caller_tries_to_choose(tmp_path: Path) -> None:
    """The task file owns its stage; an option would let the estimate name another one."""
    result = _run_eval(
        [*_live_args(tmp_path), "--stage", "triage"],
        environment=_stub_uv(tmp_path),
    )

    assert result.returncode == 64
    assert "Unknown --live option: --stage" in result.stderr


@pytest.mark.parametrize(
    "task",
    [
        "draft_live_task.py",
        "cover_live_task.py",
        "critic_live_task.py",
        "structure_live_task.py",
        "vision_live_task.py",
    ],
)
def test_live_shell_estimates_the_stage_the_task_file_declares(tmp_path: Path, task: str) -> None:
    """The estimate and the run name one stage, because both come from the task file."""
    result = _run_eval(_document_live_args(tmp_path, task), environment=_stub_uv(tmp_path))

    assert result.returncode == 0, result.stderr
    assert _estimated_stage(result.stdout) == _declared_stage(task)
    assert task in result.stdout
    assert "limit=5" in result.stdout


def test_live_shell_refuses_a_task_file_that_declares_no_stage(tmp_path: Path) -> None:
    """A task with no stage would silently estimate whatever the default happened to be."""
    stageless = tmp_path / "stageless_live_task.py"
    stageless.write_text('TASK_NAME = "stageless-live"\n', encoding="utf-8")
    arguments = _document_live_args(tmp_path)
    arguments[1] = str(stageless)

    result = _run_eval(arguments, environment=_stub_uv(tmp_path))

    assert result.returncode == 64
    assert "declares no STAGE" in result.stderr


@pytest.mark.parametrize(
    "task",
    [
        "draft_live_task.py",
        "cover_live_task.py",
        "critic_live_task.py",
        "relevance_live_task.py",
    ],
)
def test_live_shell_refuses_a_manifest_the_stage_does_not_use(tmp_path: Path, task: str) -> None:
    result = _run_eval(
        [
            *_document_live_args(tmp_path, task),
            "--selection-manifest",
            str(tmp_path / "selection manifest.json"),
        ],
        environment=_stub_uv(tmp_path),
    )

    assert result.returncode == 64
    assert "only the match stage takes a selection manifest" in result.stderr


def test_live_shell_still_requires_the_match_manifest(tmp_path: Path) -> None:
    arguments = _live_args(tmp_path)
    index = arguments.index("--selection-manifest-sha256")
    del arguments[index : index + 2]

    result = _run_eval(arguments, environment=_stub_uv(tmp_path))

    assert result.returncode == 64


def test_live_shell_accepts_the_relevance_review_stage(tmp_path: Path) -> None:
    """The stage the relevance task declares is one the shell runs and estimates."""
    result = _run_eval(
        _document_live_args(tmp_path, "relevance_live_task.py"), environment=_stub_uv(tmp_path)
    )

    assert result.returncode == 0, result.stderr
    assert _estimated_stage(result.stdout) == "relevance_review"
    assert "relevance_live_task.py" in result.stdout


def test_live_shell_estimates_the_jobspec_stage_with_prompt_arms(tmp_path: Path) -> None:
    """The boundary task's stage runs, is estimated, and passes its arms to the run."""
    result = _run_eval(
        [
            *_document_live_args(tmp_path, "jobspec_boundary_live_task.py"),
            "--prompt-arm",
            f"baseline={_arm_directory(tmp_path, 'baseline')}",
            "--prompt-arm",
            f"candidate={_arm_directory(tmp_path, 'candidate')}",
        ],
        environment=_stub_uv(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert _estimated_stage(result.stdout) == "jobspec"
    assert "--arms 2" in result.stdout


def test_live_shell_propagates_exact_manifest_identity(tmp_path: Path) -> None:
    result = _run_eval(
        _live_args(tmp_path),
        environment=_stub_uv(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert "split=development" in result.stdout
    assert "limit=10" in result.stdout
    assert f"manifest={'a' * 64}" in result.stdout
    assert f"manifest_bytes={'b' * 64}" in result.stdout


def test_live_shell_repeats_each_sample_for_an_approved_trial_count(tmp_path: Path) -> None:
    """A trial count reaches both the estimate and the runner, so one approval covers N repeats."""
    result = _run_eval(
        [*_document_live_args(tmp_path), "--trials", "10"], environment=_stub_uv(tmp_path)
    )

    assert result.returncode == 0, result.stderr
    assert "trials=10" in result.stdout
    assert "--trials 10" in result.stdout


def test_live_shell_defaults_to_one_trial(tmp_path: Path) -> None:
    result = _run_eval(_document_live_args(tmp_path), environment=_stub_uv(tmp_path))

    assert result.returncode == 0, result.stderr
    assert "trials=1" in result.stdout


def test_live_shell_refuses_a_trial_count_that_is_not_a_positive_integer(tmp_path: Path) -> None:
    result = _run_eval(
        [*_document_live_args(tmp_path), "--trials", "0"], environment=_stub_uv(tmp_path)
    )

    assert result.returncode == 64
    assert "--trials must be a positive integer" in result.stderr


def test_live_shell_runs_the_named_samples_instead_of_the_first_of_the_split(
    tmp_path: Path,
) -> None:
    """An approval naming particular cases selects them, and no limit reaches the run."""
    result = _run_eval(
        [*_document_live_args(tmp_path), "--limit", "2", "--ids", "rrp-009,rrp-010"],
        environment=_stub_uv(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert "--ids rrp-009 rrp-010" in result.stdout
    assert "--limit" not in result.stdout
    assert "sample_ids=rrp-009,rrp-010" in result.stdout
    assert "limit=2" in result.stdout


def test_live_shell_forwards_a_sample_limit_when_no_ids_are_named(tmp_path: Path) -> None:
    result = _run_eval(_document_live_args(tmp_path), environment=_stub_uv(tmp_path))

    assert result.returncode == 0, result.stderr
    assert "--limit 5" in result.stdout
    assert "--ids" not in result.stdout
    assert "sample_ids=" in result.stdout


def test_live_shell_refuses_a_malformed_id_list(tmp_path: Path) -> None:
    result = _run_eval(
        [*_document_live_args(tmp_path), "--ids", "rrp-009 rrp-010"],
        environment=_stub_uv(tmp_path),
    )

    assert result.returncode == 64
    assert "--ids must be a comma-separated list" in result.stderr


def _arm_directory(tmp_path: Path, name: str) -> str:
    directory = tmp_path / name
    directory.mkdir(parents=True)
    return str(directory)


def test_live_shell_declares_two_prompt_arms_to_the_estimate_and_the_runner(
    tmp_path: Path,
) -> None:
    """One approved run carries both prompt versions, and both are counted as calls."""
    result = _run_eval(
        [
            *_document_live_args(tmp_path, "relevance_live_task.py"),
            "--trials",
            "10",
            "--prompt-arm",
            f"baseline={_arm_directory(tmp_path, 'baseline')}",
            "--prompt-arm",
            f"candidate={_arm_directory(tmp_path, 'candidate')}",
        ],
        environment=_stub_uv(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    estimate_line = next(line for line in result.stdout.splitlines() if "cost_estimate" in line)
    # A batched stage is priced from the rows the run selects, so the estimate reads the task.
    assert "--arms 2 --task " in estimate_line
    assert "relevance_live_task.py --split " in estimate_line
    assert f"prompt_arms=baseline={tmp_path / 'baseline'},candidate={tmp_path / 'candidate'}" in (
        result.stdout
    )


def test_live_shell_declares_no_prompt_arm_by_default(tmp_path: Path) -> None:
    """A run that named no arm still runs one arm, and declares nothing."""
    result = _run_eval(
        _document_live_args(tmp_path, "relevance_live_task.py"), environment=_stub_uv(tmp_path)
    )

    assert result.returncode == 0, result.stderr
    assert "prompt_arms=" in result.stdout
    assert "--arms 1" in result.stdout


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        (["baseline"], "must be name=directory"),
        (["Baseline=DIR"], "lowercase letters, digits, or dashes"),
        (["missing=/nonexistent/prompt/arm"], "directory does not exist"),
        (["baseline=DIR", "baseline=DIR"], "duplicate --prompt-arm name"),
    ],
)
def test_live_shell_refuses_a_malformed_prompt_arm(
    tmp_path: Path, entries: list[str], message: str
) -> None:
    """A bad arm stops the run before the estimate and before any paid call."""
    directory = _arm_directory(tmp_path, "arm")
    arguments = _document_live_args(tmp_path, "relevance_live_task.py")
    for entry in entries:
        arguments.extend(["--prompt-arm", entry.replace("DIR", directory)])

    result = _run_eval(arguments, environment=_stub_uv(tmp_path))

    assert result.returncode == 64
    assert message in result.stderr
    assert "estimate for" not in result.stdout


def test_live_shell_refuses_prompt_arms_for_a_stage_that_ignores_them(tmp_path: Path) -> None:
    """Only the relevance solver reads the declaration, so another stage must refuse it."""
    result = _run_eval(
        [
            *_document_live_args(tmp_path, "draft_live_task.py"),
            "--prompt-arm",
            f"baseline={_arm_directory(tmp_path, 'baseline')}",
        ],
        environment=_stub_uv(tmp_path),
    )

    assert result.returncode == 64
    assert "only the relevance_review and jobspec stages take prompt arms" in result.stderr


def test_live_shell_runs_with_consent_when_estimate_is_unavailable(tmp_path: Path) -> None:
    environment = _stub_uv(tmp_path)
    environment["STUB_ESTIMATE_FAIL"] = "1"

    result = _run_eval(
        _live_args(tmp_path),
        environment=environment,
    )

    assert result.returncode == 0
    assert "cost estimate is unavailable. Costs are advisory" in result.stderr
    assert "split=development" in result.stdout


def test_live_shell_hands_a_named_cv_version_to_the_relevance_run(tmp_path: Path) -> None:
    """The run reads the version from the same workspace it bills."""
    result = _run_eval(
        [*_document_live_args(tmp_path, "relevance_live_task.py"), "--cv-version", "cv-7f3a"],
        environment=_stub_uv(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert "cv_version=cv-7f3a" in result.stdout
    assert "relevance_live_task.py" in result.stdout


@pytest.mark.parametrize("render", [False, True])
def test_live_shell_refuses_relevance_only_options_for_another_stage(
    tmp_path: Path, render: bool
) -> None:
    """Only the relevance solver reads a CV version or renders its requests."""
    if render:
        arguments = _render_args(tmp_path, "draft_live_task.py")
    else:
        arguments = [*_document_live_args(tmp_path), "--cv-version", "cv-7f3a"]

    result = _run_eval(arguments, environment=_stub_uv(tmp_path))

    assert result.returncode == 64
    assert "only the relevance_review stage takes --cv-version or --render-only" in result.stderr


def test_live_shell_refuses_a_malformed_cv_version(tmp_path: Path) -> None:
    result = _run_eval(
        [*_document_live_args(tmp_path, "relevance_live_task.py"), "--cv-version", "a b"],
        environment=_stub_uv(tmp_path),
    )

    assert result.returncode == 64
    assert "--cv-version must be one master-CV version id" in result.stderr


def _render_args(tmp_path: Path, task: str = "relevance_live_task.py") -> list[str]:
    return [
        "--live",
        f"evals/inspect/tasks/{task}",
        "--data-root",
        str(tmp_path / "workspace"),
        "--provider",
        "openai",
        "--render-only",
        str(tmp_path / "render"),
    ]


def test_live_shell_renders_without_an_estimate_an_approval_or_a_run(tmp_path: Path) -> None:
    """A render reaches neither the cost estimate nor the paid runner."""
    result = _run_eval(
        [
            *_render_args(tmp_path),
            "--cv-version",
            "cv-7f3a",
            "--prompt-arm",
            f"baseline={_arm_directory(tmp_path, 'baseline')}",
        ],
        environment=_stub_uv(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert "estimate for" not in result.stdout
    assert "task=" not in result.stdout
    render = next(row for row in result.stdout.splitlines() if row.startswith("render="))
    assert "relevance_live_task.py" in render
    assert f"--render-only {tmp_path / 'render'} --provider openai" in render
    assert "cv_version=cv-7f3a" in result.stdout
    assert f"prompt_arms=baseline={tmp_path / 'baseline'}" in result.stdout
    assert f"database=sqlite+pysqlite:///{tmp_path / 'workspace'}/clearscout.sqlite3" in (
        result.stdout
    )


@pytest.mark.parametrize(
    "extra",
    [
        ["--i-approve", "0.56"],
        ["--output-dir", "OUT"],
        ["--trials", "3"],
    ],
)
def test_live_shell_refuses_a_paid_scope_option_beside_a_render(
    tmp_path: Path, extra: list[str]
) -> None:
    """A render spends nothing and sends one trial, so a paid option is a mistake."""
    arguments = [*_render_args(tmp_path), *extra]
    arguments = [str(tmp_path / "out") if item == "OUT" else item for item in arguments]

    result = _run_eval(arguments, environment=_stub_uv(tmp_path))

    assert result.returncode == 64
    assert "--render-only renders one trial and spends nothing" in result.stderr
    assert "render=" not in result.stdout


def test_live_shell_refuses_a_private_dataset_beside_a_render(tmp_path: Path) -> None:
    dataset = tmp_path / "private.jsonl"
    dataset.write_text("{}\n", encoding="utf-8")

    result = _run_eval(
        [*_render_args(tmp_path), "--dataset", str(dataset)], environment=_stub_uv(tmp_path)
    )

    assert result.returncode == 64
    assert "--render-only renders the task fixture and does not take --dataset" in result.stderr
    assert "render=" not in result.stdout


@pytest.mark.parametrize(
    ("extra", "passed"),
    [
        (["--ids", "rrp-009,rrp-011"], "--ids rrp-009 rrp-011"),
        (["--limit", "5", "--split", "development"], "--limit 5 --split development"),
    ],
)
def test_live_shell_renders_the_rows_the_paid_run_selects(
    tmp_path: Path, extra: list[str], passed: str
) -> None:
    """A render proves a selected run's batches, so the selection reaches it unchanged."""
    result = _run_eval([*_render_args(tmp_path), *extra], environment=_stub_uv(tmp_path))

    assert result.returncode == 0, result.stderr
    render = next(row for row in result.stdout.splitlines() if row.startswith("render="))
    assert render.endswith(f"--provider openai {passed}")
