#!/usr/bin/env python3
"""Report paths named by a tracked instruction file that no longer exist.

Stop hook, and runnable directly with --check. Instruction files load on every
turn but nothing executes them, so a moved path goes unnoticed; this resolves
the slash-bearing paths they name and exits 2 with the misses.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

GIT_TIMEOUT = 5
INSTRUCTION_PATHSPECS = (
    "AGENTS.md",
    "CLAUDE.md",
    "*/AGENTS.md",
    "*/CLAUDE.md",
    ".claude/rules/*.md",
    ".agents/roles/*.md",
    ".agents/skills/*/SKILL.md",
    ".claude/agents/*.md",
)

FENCE = re.compile(r"```.*?```", re.DOTALL)
BACKTICKED = re.compile(r"`([^`\n]+)`")
MD_LINK = re.compile(r"\]\(([^)\s]+)\)")

# An @path import expands at launch and resolves against the importing file's directory.
IMPORT = re.compile(r"^@([^\s`]+)", re.MULTILINE)

# Placeholders, globs, and alternation are patterns rather than locations.
NOT_A_LITERAL_PATH = re.compile(r"[<>*|{}\[\]\s]|YYYY|MM-DD")


def repo_root() -> Path:
    """Return the project directory Claude Code names, else the checkout holding this hook."""
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])


def instruction_files(root: Path) -> list[Path]:
    """Return the tracked instruction files under root."""
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv
            ["git", "ls-files", "-z", "--", *INSTRUCTION_PATHSPECS],  # noqa: S607
            cwd=root,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if done.returncode != 0:
        return []
    return [root / name for name in done.stdout.split("\0") if name]


def _is_candidate(token: str) -> bool:
    if "/" not in token or token.startswith(("http://", "https://", "mailto:")):
        return False
    # A backticked @scope/name is an npm package; a real import is matched by IMPORT.
    if token.startswith("@"):
        return False
    # A leading slash with no second slash and no dot is a slash command such as /mcp.
    if token.startswith("/") and "/" not in token[1:] and "." not in token:
        return False
    return not NOT_A_LITERAL_PATH.search(token)


def candidates(text: str) -> list[str]:
    """Return the distinct slash-bearing literal paths the text names outside code fences."""
    text = FENCE.sub("", text)
    out: list[str] = []
    for raw in BACKTICKED.findall(text) + MD_LINK.findall(text):
        token = raw.strip().rstrip(".,;:")
        if _is_candidate(token) and token not in out:
            out.append(token)
    return out


def resolve(token: str, bases: list[Path]) -> bool:
    """Return whether the token names an existing path, absolute or under a base."""
    if token.startswith(("~", "/")):
        return Path(token).expanduser().exists()
    return any((base / token).exists() for base in bases)


def broken(path: Path, bases: list[Path]) -> list[str]:
    """Return the paths and imports in one instruction file that do not resolve."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    misses = [t for t in candidates(text) if not resolve(t, bases)]
    for token in IMPORT.findall(FENCE.sub("", text)):
        if not resolve(token, [path.parent, *bases]):
            misses.append(f"@{token} (import)")
    return misses


def report(root: Path) -> list[str]:
    """Return report lines for every tracked instruction file with a broken path."""
    lines: list[str] = []
    for path in instruction_files(root):
        misses = broken(path, [path.parent, root])
        if misses:
            lines.append(f"{path.relative_to(root)}:")
            lines.extend(f"    {m}" for m in misses)
    return lines


def main() -> int:
    """Hook entry point: exit 2 (1 with --check) when a named path is missing."""
    checking = "--check" in sys.argv
    if not checking:
        try:
            payload = json.load(sys.stdin)
        except ValueError:
            return 0
        # Claude Code re-runs Stop hooks after a block; report once per turn.
        if payload.get("stop_hook_active"):
            return 0

    lines = report(repo_root())
    if not lines:
        if checking:
            print("instruction paths all resolve")
        return 0

    message = (
        "Instruction files name paths that do not exist. An always-loaded file "
        "citing a moved artifact sends every future session to the wrong place, "
        "so repoint or drop these:\n" + "\n".join(lines)
    )
    if checking:
        print(message)
        return 1
    sys.stderr.write(message + "\n")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - never blocks stop
        sys.exit(0)
