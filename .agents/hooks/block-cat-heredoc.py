#!/usr/bin/env python3
"""Refuse heredoc file writes into the repository; force Write or Edit.

PreToolUse hook on Bash. Two shapes are blocked, both only when they touch the
repository tree: `cat > repo/path <<EOF`, and an interpreter heredoc
(`python3 - <<PY ... write_text ... PY`) that names a repository file, since a
script that edits a file is the same bypass as `cat`.

Scratch paths outside the repository, the configured $TMPDIR, and the agent memory
directories under .claude/ are allowed. Plain `cat file` reads are untouched.
Exit 2 blocks; any error fails open.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# `cat`, an optional flag, `>` or `>>`, then the target; the heredoc marker follows later.
CAT_REDIRECT = re.compile(r"(?:^|[;&|\n]|\|\|)\s*cat(?:\s+-[\w-]+)?\s*>{1,2}\s*(?P<target>\S+)")
HEREDOC = re.compile(r"<<-?\s*['\"]?[A-Za-z_][A-Za-z0-9_]*['\"]?|<<<")

# An interpreter reading a script from stdin, which can write any path it names.
INTERPRETER = re.compile(r"(?:^|[;&|\n]|\|\|)\s*(?:python3?|node|ruby|perl|php)\b(?=[^\n]*<<)")
PATH_TOKEN = re.compile(
    r"['\"]([\w./~-]+\.(?:py|pyi|ts|tsx|js|jsx|md|css|json|toml|ya?ml|sh|sql))['\"]"
)
WRITE_PRIMITIVE = re.compile(
    r"\b(?:write_text|write_bytes|open)\s*\([^\n]*(?:['\"](?:w|a|x|r\+)[bt+]*['\"]|write_)|"
    r"\b(?:writeFile|appendFile|writeFileSync|appendFileSync)\b|\.write\s*\("
)

AGENT_MEMORY_DIRS = (".claude/agent-memory", ".claude/agent-memory-local")

MESSAGE = (
    "Use Write or Edit for files in the repository tree; heredoc file creation is "
    "refused. If this really is a scratch file, put it under $TMPDIR, /tmp, or "
    "/private/tmp.\n"
)

INTERPRETER_MESSAGE = (
    "This is an interpreter heredoc naming a repository file, which edits it just as "
    "`cat >` would. Use Write or Edit instead. Run the script from $TMPDIR, /tmp, "
    "or /private/tmp if it genuinely only reads.\n"
)


def repo_root() -> Path:
    """Return the project directory Claude Code names, else the checkout holding this hook."""
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])


def resolves_under_repo(target: str) -> bool:
    """Return whether a write target lands inside the repository."""
    # Unexpanded, `$TMPDIR/x` would resolve against the working directory.
    target = os.path.expandvars(target)
    if not target or "$" in target:
        return False
    tmpdir = os.environ.get("TMPDIR", "").rstrip("/")
    if tmpdir and target.startswith(tmpdir + "/"):
        return False
    try:
        resolved = Path(target).expanduser().resolve()
        root = repo_root().resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return False
    # Read-only review agents have no Write tool and keep their memory through Bash.
    return not any(resolved.is_relative_to(root / d) for d in AGENT_MEMORY_DIRS)


def main() -> int:
    """Hook entry point: exit 2 to refuse, 0 to allow."""
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    command = str(payload.get("tool_input", {}).get("command", ""))
    if not command or not HEREDOC.search(command):
        return 0

    for match in CAT_REDIRECT.finditer(command):
        if resolves_under_repo(match.group("target").strip("'\"")):
            sys.stderr.write(MESSAGE)
            return 2

    if INTERPRETER.search(command) and WRITE_PRIMITIVE.search(command):
        for token in PATH_TOKEN.findall(command):
            if resolves_under_repo(token):
                sys.stderr.write(INTERPRETER_MESSAGE)
                return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - fails open
        sys.exit(0)
