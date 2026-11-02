#!/usr/bin/env python3
"""Refuse a test command whose output is piped into a search.

PreToolUse hook on Bash; exit 2 blocks. Piping a suite into grep discards the run
and forces a second one. Fails open: the cost here is minutes, not safety.
"""

from __future__ import annotations

import json
import re
import sys

# A redirect followed by a search of the file will not match, which is the escape hatch.
RUNNERS = (
    r"(pytest|vitest|jest|npm\s+(--prefix\s+\S+\s+)?(run\s+)?test"
    r"|just\s+(test|pytest-log|gate)|scripts/gate\.sh|scripts/eval\.sh)"
)
CONSUMERS = r"(grep|rg|egrep|fgrep|head|tail|ag|awk|sed|less|more)"

# The runner has to start its command, so `grep -n x scripts/gate.sh | head` stays allowed.
RUNNER_AT_START = re.compile(
    r"^\s*(?:\w+=\S+\s+)*(?:sudo\s+|time\s+|uv\s+run\s+(?:--\S+\s+)*|npx\s+|bash\s+|sh\s+)*"
    + RUNNERS,
    re.IGNORECASE,
)
CONSUMER_AT_START = re.compile(rf"^\s*\S*{CONSUMERS}\b", re.IGNORECASE)
SEGMENTS = re.compile(r";|&&|\|\||\n")

MESSAGE = """This pipes test output into a search, which discards the run and
forces a rerun. In TailorCV, run `just pytest-log <name> <files>` and read the log
it names. Elsewhere, redirect the run once and search the file:

  uv run pytest ... > /tmp/pytest.out 2>&1
  grep -n "FAILED" /tmp/pytest.out
"""


def pipes_a_run(command: str) -> bool:
    """Return True when a segment runs a suite and pipes it straight into a search."""
    for segment in SEGMENTS.split(command):
        head, pipe, tail = segment.partition("|")
        if pipe and RUNNER_AT_START.search(head) and CONSUMER_AT_START.search(tail):
            return True
    return False


def main() -> int:
    """Hook entry point: exit 2 to refuse, 0 to allow."""
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    command = str(payload.get("tool_input", {}).get("command", ""))
    if pipes_a_run(command):
        sys.stderr.write(MESSAGE)
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - fails open
        sys.exit(0)
