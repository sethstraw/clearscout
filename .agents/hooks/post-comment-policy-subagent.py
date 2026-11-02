#!/usr/bin/env python3
"""Hand a subagent the comment-policy violations in the file it just wrote.

PostToolUse hook on Write, Edit, and Serena's replace and insert edits. Acts only
for a subagent, and only in a project that ships the comment-policy checker.
Exit 2 returns the checker's output to the agent; every other path exits 0.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

TIMEOUT_SECONDS = 30
CHECKER = "scripts/check_comment_policy.py"
WRITE_TOOLS = frozenset({"Write", "Edit"})
SERENA_EDIT = re.compile(r"^mcp__serena\w*?__(replace|insert)\w*$")


def written_path(payload: dict[str, object]) -> Path | None:
    """Return the file a write tool changed, resolving Serena's project-relative path."""
    name = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    if name in WRITE_TOOLS:
        value = tool_input.get("file_path")
    elif SERENA_EDIT.match(name):
        value = tool_input.get("relative_path")
    else:
        return None
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd()) / path
    return path


def project_root(path: Path) -> Path | None:
    """Return the nearest ancestor that ships the checker, if any."""
    for candidate in path.parents:
        if (candidate / CHECKER).is_file():
            return candidate
    return None


def main() -> int:
    """Hook entry point: exit 2 with the checker's output, 0 otherwise."""
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    if not str(payload.get("agent_id") or "").strip():
        return 0
    path = written_path(payload)
    root = project_root(path) if path is not None and path.is_file() else None
    if root is None:
        return 0

    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["uv", "run", "--no-sync", "python", CHECKER, "--files", str(path)],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )
    if done.returncode == 0:
        return 0
    sys.stderr.write(
        "Comment policy refuses what you just wrote. Fix it before moving on:\n"
        f"{done.stdout}{done.stderr}"
    )
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - a broken check never blocks work
        sys.exit(0)
