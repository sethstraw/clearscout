#!/usr/bin/env python3
"""Keep a review agent's writes inside its own memory directory.

PreToolUse hook on Write and Edit. Agent memory can turn Write and Edit back on
for an agent that otherwise denies them, so this refuses a `*-review` agent's
write outside `.claude/agent-memory-local/`. Exit 2 blocks; fails open.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

WRITE_TOOLS = frozenset({"Write", "Edit"})
MEMORY_DIRECTORY = Path(".claude/agent-memory-local")

MESSAGE = """A review agent is read-only outside its own memory directory.
Return the change as a finding for the coordinator instead of writing {path}.
"""


def refused_path(payload: dict[str, object]) -> str | None:
    """Return the path a review agent may not write, or None when the write is allowed."""
    if not str(payload.get("agent_type") or "").endswith("-review"):
        return None
    if payload.get("tool_name") not in WRITE_TOOLS:
        return None
    tool_input = payload.get("tool_input")
    value = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    if not isinstance(value, str) or not value:
        return None
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or str(payload.get("cwd") or Path.cwd()))
    path = Path(value) if Path(value).is_absolute() else root / value
    memory = (root / MEMORY_DIRECTORY).resolve()
    if path.resolve().is_relative_to(memory):
        return None
    return value


def main() -> int:
    """Hook entry point: exit 2 with a reason for a refused write, 0 otherwise."""
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    path = refused_path(payload)
    if path is None:
        return 0
    sys.stderr.write(MESSAGE.format(path=path))
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - a broken check never blocks work
        sys.exit(0)
