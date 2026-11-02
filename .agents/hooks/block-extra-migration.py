#!/usr/bin/env python3
"""Refuse a second Alembic migration before the first public tag.

PreToolUse hook on Write, Edit, NotebookEdit, Serena edits, and Codex apply_patch;
exit 2 blocks; fails open.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

VERSIONS_DIRECTORY = "persistence/migrations/versions"
PERMITTED = {"v0001_schema.py", "__init__.py"}
FILE_TOOLS = {"Write", "Edit", "NotebookEdit"}
# Each client names the Serena server itself (Claude `serena`, Codex `serena_tailorcv`).
SERENA_EDIT = re.compile(r"^mcp__serena\w*?__(?:replace|insert|rename|safe_delete)")

MESSAGE = """The repository has not been tagged, so `v0001_schema.py` is the only
migration and a schema change edits it in place. Adding a second revision here
would record a history for a release that does not exist.

Edit src/tailorcv/infrastructure/persistence/migrations/versions/v0001_schema.py
instead, and the mirrored constraints in
src/tailorcv/infrastructure/persistence/models/ beside it.
"""


def paths_from_payload(payload: dict[str, Any]) -> list[str]:
    """Normalize supported Claude, Serena, and Codex mutation payloads."""
    name = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") or {}
    paths: list[str] = []
    if name in FILE_TOOLS or SERENA_EDIT.match(name):
        for key in ("file_path", "path", "relative_path"):
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                paths.append(value)
    if name == "apply_patch":
        command = str(tool_input.get("command") or "")
        paths.extend(re.findall(r"(?m)^\*\*\* (?:Add|Update|Delete) File: (.+)$", command))
        paths.extend(re.findall(r"(?m)^\+\+\+ (?:b/)?(.+)$", command))
    return list(dict.fromkeys(paths))


def released() -> bool:
    """Report whether the repository carries a release tag, after which revisions append."""
    git = shutil.which("git")
    if git is None:
        return False
    root = os.environ.get("CLAUDE_PROJECT_DIR") or str(Path.cwd())
    result = subprocess.run(  # noqa: S603 - resolved executable, fixed arguments
        [git, "-C", root, "tag", "--list", "v*"],
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(result.stdout.strip())


def main() -> int:
    """Hook entry point: exit 2 to refuse, 0 to allow."""
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    if released():
        return 0
    for raw in paths_from_payload(payload):
        path = PurePosixPath(raw.replace("\\", "/"))
        if VERSIONS_DIRECTORY in path.as_posix() and path.name not in PERMITTED:
            sys.stderr.write(MESSAGE)
            return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - fails open
        sys.exit(0)
