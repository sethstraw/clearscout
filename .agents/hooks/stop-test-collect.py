#!/usr/bin/env python3
"""Check focused pytest collection when test files changed.

Stop hook. Runs `pytest --collect-only` over every changed `tests/**/test_*.py`
that still exists, and exits 2 with the collection error so a task does not close
on a test module pytest cannot import.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

GIT_TIMEOUT = 5
PYTEST_TIMEOUT = 12
STATUS_PREFIX = 3
NO_TESTS_COLLECTED = 5


def run(args: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
    """Return the exit code and combined output, or (1, "") when the tool cannot run."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv
            args, capture_output=True, text=True, timeout=timeout, cwd=str(cwd), check=False
        )
    except (subprocess.TimeoutExpired, OSError):
        return 1, ""
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def changed_test_files(cwd: Path) -> list[str]:
    """Return changed or untracked test modules under tests/ that still exist."""
    code, out = run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd, GIT_TIMEOUT
    )
    if code != 0:
        return []
    fields = out.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) <= STATUS_PREFIX:
            continue
        # With -z a rename or copy is followed by a separate field naming its source.
        if entry[0] in "RC" or entry[1] in "RC":
            index += 1
        paths.append(entry[STATUS_PREFIX:])
    return [
        p
        for p in paths
        if p.startswith("tests/")
        and Path(p).name.startswith("test_")
        and p.endswith(".py")
        and (cwd / p).is_file()
    ]


def collection_result(cwd: Path, paths: list[str]) -> tuple[str, str]:
    """Return ("ok" | "error" | "unknown", detail) for collecting the given modules."""
    code, out = run(
        ["uv", "run", "--no-sync", "pytest", "--collect-only", *paths], cwd, PYTEST_TIMEOUT
    )
    if code in (0, NO_TESTS_COLLECTED):
        return "ok", out
    if not out:
        return "unknown", "collection could not run or timed out"
    return "error", out[-1200:]


def main() -> int:
    """Hook entry point: exit 2 on a collection error, 0 otherwise."""
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        payload = {}

    # Claude Code re-runs Stop hooks after a block; warn once per turn.
    if payload.get("stop_hook_active"):
        return 0

    cwd = Path.cwd()
    if not (cwd / ".git").exists():
        return 0
    changed = changed_test_files(cwd)
    if not changed:
        return 0

    outcome, detail = collection_result(cwd, changed)
    if outcome == "error":
        sys.stderr.write("Focused pytest collection failed for changed tests:\n" + detail + "\n")
        return 2
    if outcome == "unknown":
        message = (
            "Stop hook: focused pytest collection status unknown (tool unavailable or timed out)."
        )
        print(json.dumps({"systemMessage": message}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - never blocks Stop
        sys.exit(0)
