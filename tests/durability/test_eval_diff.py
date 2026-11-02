"""The per-case comparison a reader acts on when a run moves.

Trials are independent draws, so a case is compared by its passes out of its
trials, and each run by the tokens and dollars its rows recorded.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from evals.inspect.diff import diff
from evals.semantic import StageMeasurement

from tailorcv.shared.costs import Usage


def _log(path: Path, rows: list[dict[str, Any]], requested_model: str = "") -> Path:
    header: dict[str, Any] = {"task": "critic-live", "total": len(rows)}
    if requested_model:
        header["run"] = {"requested_model": requested_model}
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in [header, *rows]),
        encoding="utf-8",
    )
    return path


def _row(
    sample_id: str,
    value: bool,
    trial: int | None = None,
    arm: str | None = None,
    measurements: tuple[StageMeasurement, ...] = (),
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": sample_id,
        "score": {"value": value},
        "solver_output": {
            "metadata": {"measurements": [item.model_dump(mode="json") for item in measurements]}
        },
    }
    if trial is not None:
        row["trial"] = trial
    if arm is not None:
        row["arm"] = arm
    return row


def _trials(sample_id: str, values: list[bool], arm: str | None = None) -> list[dict[str, Any]]:
    return [_row(sample_id, value, trial, arm) for trial, value in enumerate(values)]


def test_a_case_is_compared_by_passes_out_of_trials_not_trial_by_trial(tmp_path: Path) -> None:
    """Two runs passing a case twice in three draws agree, whichever draws failed."""
    baseline = _log(
        tmp_path / "before.jsonl", _trials("planted_misattribution", [True, False, True])
    )
    candidate = _log(
        tmp_path / "after.jsonl", _trials("planted_misattribution", [False, True, True])
    )

    table = diff(str(baseline), str(candidate))

    assert "| planted_misattribution | 2/3 | 2/3 |" in table
    assert "trial" not in table


def test_a_case_one_run_never_scored_is_marked_absent(tmp_path: Path) -> None:
    """A case added to the set shows as absent from the run that never scored it."""
    baseline = _log(tmp_path / "before.jsonl", [_row("strong_verdict", True)])
    candidate = _log(
        tmp_path / "after.jsonl",
        [_row("strong_verdict", True), _row("planted_team_contribution", False)],
    )

    table = diff(str(baseline), str(candidate))

    assert "| strong_verdict | 1/1 | 1/1 |" in table
    assert "| planted_team_contribution | - | 0/1 |" in table


def test_a_run_with_arms_reports_each_arm_as_its_own_column(tmp_path: Path) -> None:
    """Arms in one run are the comparison that run was made for, so neither is pooled."""
    baseline = _log(tmp_path / "before.jsonl", _trials("case-6", [True, True]))
    candidate = _log(
        tmp_path / "after.jsonl",
        [*_trials("case-6", [True, False], arm="v1"), *_trials("case-6", [False, False], arm="v2")],
    )

    table = diff(str(baseline), str(candidate))

    assert "| case | baseline | candidate v1 | candidate v2 |" in table
    assert "| case-6 | 2/2 | 1/2 | 0/2 |" in table


def test_each_run_totals_the_tokens_and_dollars_its_rows_recorded(tmp_path: Path) -> None:
    """Spend is summed from every call a row made, and an unpriced call is counted, not zeroed."""
    priced = StageMeasurement(
        stage="critic",
        model="gpt-5.6-terra",
        usage=Usage(input_tokens=1_000, output_tokens=300, reasoning_tokens=100),
        dollars=Decimal("0.0150"),
    )
    unpriced = StageMeasurement(
        stage="critic",
        model="gpt-5.6-terra",
        usage=Usage(input_tokens=1_000, output_tokens=400, reasoning_tokens=400),
    )
    baseline = _log(
        tmp_path / "before.jsonl",
        [
            _row("strong_verdict", True, 0, measurements=(priced, priced)),
            _row("strong_verdict", False, 1, measurements=(unpriced,)),
        ],
    )
    candidate = _log(tmp_path / "after.jsonl", [_row("strong_verdict", True)])

    table = diff(str(baseline), str(candidate))

    assert "| baseline | 1/2 | 3000 | 1000 | 600 | $0.0300 (1 unpriced) |" in table
    assert "| candidate | 1/1 | 0 | 0 | 0 | $0.0000 |" in table


def test_the_model_each_run_requested_is_named(tmp_path: Path) -> None:
    """A model comparison is unreadable without knowing which run sent which model."""
    baseline = _log(tmp_path / "before.jsonl", [_row("strong_verdict", True)], "gpt-5.6-terra")
    candidate = _log(tmp_path / "after.jsonl", [_row("strong_verdict", True)], "gpt-6-sol")

    table = diff(str(baseline), str(candidate))

    assert f"baseline: {baseline} requested_model=gpt-5.6-terra" in table
    assert f"candidate: {candidate} requested_model=gpt-6-sol" in table


def test_the_comparison_states_what_size_of_gap_it_can_support(tmp_path: Path) -> None:
    """Without a significance test, the reader is told a one-case gap is noise."""
    rows = [_row(f"case-{index}", True) for index in range(10)]
    baseline = _log(tmp_path / "before.jsonl", rows)
    candidate = _log(tmp_path / "after.jsonl", rows)

    lines = diff(str(baseline), str(candidate)).splitlines()

    assert lines[-1] == (
        "Trials are independent draws; across 10 cases only a gap of several cases means anything."
    )
