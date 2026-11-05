#!/usr/bin/env python3
"""Open a session with a small read-only record of the working tree.

SessionStart hook. Prints additionalContext naming the checkout, branch, working
tree state, instruction files, and, after compaction, the latest snapshot.
Silent, never blocks.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

GIT_TIMEOUT = 3


def run_git(args: list[str], cwd: Path) -> tuple[bool, str]:
    """Return whether git succeeded, and its stdout or error text."""
    result = subprocess.run(  # noqa: S603 - fixed argv
        ["git", *args],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
        check=False,
        cwd=str(cwd),
    )
    if result.returncode != 0:
        return False, result.stderr.strip() or "could not inspect"
    return True, result.stdout.strip()


def _describe_tree(ok: bool, status: str) -> str:
    if not ok:
        return "could not inspect"
    return f"dirty ({len(status.splitlines())} paths)" if status else "clean"


def build_context(cwd: Path, source: str = "") -> str:
    """Return the context block for a session opened in cwd."""
    ok, root = run_git(["rev-parse", "--show-toplevel"], cwd)
    if not ok:
        return f"workspace inspection: {root} (cwd: {cwd})"
    root_path = Path(root)

    _, common = run_git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd)
    branch_ok, branch = run_git(["branch", "--show-current"], cwd)
    status_ok, status = run_git(["status", "--short"], cwd)
    branch = (branch or "detached") if branch_ok else "could not inspect"
    parts = [
        f"repository/worktree: {root_path}",
        f"git common directory: {common}",
        f"branch: {branch}",
        f"working tree: {_describe_tree(status_ok, status)}",
    ]
    instructions = [
        str(root_path / name) for name in ("AGENTS.md", ".claude/CLAUDE.md") if (root_path / name).is_file()
    ]
    if instructions:
        parts.append("instructions: " + ", ".join(instructions))
    if source == "compact":
        snapshots = list((root_path / ".claude/state").glob("precompact-*.md"))
        if snapshots:
            latest = max(snapshots, key=lambda p: p.stat().st_mtime)
            parts.append(f"pre-compaction recovery: {latest}")
    return "\n".join(parts)


def main() -> int:
    """Hook entry point: print the context block and exit 0."""
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        payload = {}
    cwd = Path(str(payload.get("cwd") or Path.cwd())).expanduser()
    context = build_context(cwd, str(payload.get("source") or ""))
    print(
        json.dumps(
            {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}
        )
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - never blocks the session
        sys.exit(0)
