"""Compare two runner logs case by case, with what each run spent.

The runner writes a header line, then one row per repeat of each sample,
carrying ``id``, ``score.value``, ``trial`` when the run repeated its samples,
``arm`` when it declared several prompt versions, and the provider calls the
repeat made under ``solver_output.metadata.measurements``. Trials are
independent draws, so trial ``i`` of one run says nothing about trial ``i`` of
another; this reports passes out of trials for each case instead.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from evals.semantic import StageMeasurement


@dataclass
class _Tally:
    """Passes out of trials for one case, or spend summed over one run column."""

    passed: int = 0
    trials: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    dollars: Decimal = Decimal(0)
    unpriced: int = 0

    def add(self, row: dict[str, Any]) -> None:
        """Count one repeat's verdict and the calls it made."""
        score = row.get("score")
        self.trials += 1
        if isinstance(score, dict) and bool(score.get("value")):
            self.passed += 1
        for measurement in _measurements(row):
            self.input_tokens += measurement.usage.input_tokens
            self.output_tokens += measurement.usage.output_tokens
            self.reasoning_tokens += measurement.usage.reasoning_tokens
            if measurement.dollars is None:
                self.unpriced += 1
            else:
                self.dollars += measurement.dollars


@dataclass
class _Run:
    """One log: its requested model, and tallies per column and per case."""

    label: str
    requested_model: str
    columns: dict[str, _Tally] = field(default_factory=dict)
    cases: dict[tuple[str, str], _Tally] = field(default_factory=dict)


def _measurements(row: dict[str, Any]) -> list[StageMeasurement]:
    output = row.get("solver_output")
    metadata = output.get("metadata") if isinstance(output, dict) else None
    entries = metadata.get("measurements") if isinstance(metadata, dict) else None
    if not isinstance(entries, list):
        return []
    return [StageMeasurement.model_validate(entry) for entry in entries]


def _read(log_path: Path, label: str) -> _Run:
    """Tally one runner log by column (the run, or each of its arms) and case."""
    if not log_path.is_file():
        raise FileNotFoundError(log_path)
    records = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    header = records[0] if records and "id" not in records[0] else {}
    identity = header.get("run")
    requested = identity.get("requested_model") if isinstance(identity, dict) else None
    run = _Run(label=label, requested_model=str(requested or "unrecorded"))
    for record in records:
        sample_id = record.get("id")
        if not sample_id:
            continue
        arm = record.get("arm")
        column = label if arm is None else f"{label} {arm}"
        run.columns.setdefault(column, _Tally()).add(record)
        run.cases.setdefault((str(sample_id), column), _Tally()).add(record)
    return run


def _dollars(tally: _Tally) -> str:
    text = f"${tally.dollars:.4f}"
    return f"{text} ({tally.unpriced} unpriced)" if tally.unpriced else text


def diff(baseline_path: str, candidate_path: str) -> str:
    """Return markdown comparing two logs' per-case pass counts and per-run spend."""
    runs = [_read(Path(baseline_path), "baseline"), _read(Path(candidate_path), "candidate")]
    columns = [column for run in runs for column in run.columns]
    cases = sorted({case for run in runs for case, _column in run.cases})
    lines = [
        f"{run.label}: {path} requested_model={run.requested_model}"
        for run, path in zip(runs, (baseline_path, candidate_path), strict=True)
    ]
    lines += ["", "| case | " + " | ".join(columns) + " |", "| --- |" + " --- |" * len(columns)]
    for case in cases:
        cells = []
        for run in runs:
            for column in run.columns:
                tally = run.cases.get((case, column))
                cells.append(f"{tally.passed}/{tally.trials}" if tally is not None else "-")
        lines.append(f"| {case} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "| run | passed | input tokens | output tokens | reasoning tokens | dollars |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for run in runs:
        for column, tally in run.columns.items():
            lines.append(
                f"| {column} | {tally.passed}/{tally.trials} | {tally.input_tokens} "
                f"| {tally.output_tokens} | {tally.reasoning_tokens} | {_dollars(tally)} |"
            )
    lines += [
        "",
        f"Trials are independent draws; across {len(cases)} cases "
        "only a gap of several cases means anything.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(
        prog="python -m evals.inspect.diff",
        description="Compare two runner logs case by case and print markdown tables.",
    )
    parser.add_argument("baseline")
    parser.add_argument("candidate")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    print(diff(args.baseline, args.candidate))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
