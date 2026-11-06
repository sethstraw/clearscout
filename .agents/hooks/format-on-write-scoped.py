#!/usr/bin/env python3
"""Format a file the agent wrote whole, skipping vendored trees, build output, and huge files.

PostToolUse hook on Write. Always exits 0; the gate still checks formatting.
Prettier is skipped when the project has no local binary.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

TIMEOUT_SECONDS = 30
MAX_BYTES = 200 * 1024

PRETTIER_SUFFIXES = frozenset(
    {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".css", ".json", ".md", ".yml", ".yaml", ".html"}
)
FORMATTED_SUFFIXES = PRETTIER_SUFFIXES | {".py"}

# Directory names that mark output never formatted, in either the Python or the SPA tree.
SKIP_SEGMENTS = frozenset({".venv", "node_modules", "dist", ".serena"})
CLAUDE_INTERNAL = frozenset({"state", "cache"})

PRETTIER_BINARIES = ("web/node_modules/.bin/prettier", "node_modules/.bin/prettier")

ROOT_MARKERS = ("pyproject.toml", ".prettierrc.json", ".prettierrc")


def project_root(path: Path) -> Path | None:
    """Return the nearest ancestor carrying a formatter config, if any."""
    for candidate in path.parents:
        if any((candidate / marker).is_file() for marker in ROOT_MARKERS):
            return candidate
    return None


def has_skip_segment(path: Path) -> bool:
    """Return whether the path sits in build output, a vendored tree, or agent state."""
    parts = path.parts
    for i, segment in enumerate(parts):
        if segment in SKIP_SEGMENTS:
            return True
        if segment == ".claude" and i + 1 < len(parts) and parts[i + 1] in CLAUDE_INTERNAL:
            return True
    return False


def prettier_command(root: Path, path: Path) -> list[str] | None:
    """Return the Prettier invocation for a file, or None when Prettier is not installed."""
    for relative in PRETTIER_BINARIES:
        binary = root / relative
        if not binary.is_file():
            continue
        # Run from root, Prettier reads root's .gitignore and .prettierignore by default.
        return [str(binary), "--write", str(path)]
    return None


def format_command(root: Path, path: Path) -> list[str] | None:
    """Return the formatter invocation for a file, or None when none applies."""
    if path.suffix == ".py":
        return ["uv", "run", "--no-sync", "ruff", "format", str(path)]
    if path.suffix in PRETTIER_SUFFIXES:
        return prettier_command(root, path)
    return None


def main() -> int:
    """Hook entry point: format when it applies, and always exit 0."""
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0

    # Edit makes a small patch; the gate checks its formatting.
    if payload.get("tool_name") != "Write":
        return 0

    path = Path(str(payload.get("tool_input", {}).get("file_path", "")))
    if not path.is_file() or path.suffix not in FORMATTED_SUFFIXES or has_skip_segment(path):
        return 0
    try:
        if path.stat().st_size > MAX_BYTES:
            return 0
    except OSError:
        return 0

    root = project_root(path)
    command = format_command(root, path) if root else None
    if command is None:
        return 0
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        command, cwd=root, capture_output=True, timeout=TIMEOUT_SECONDS, check=False
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - never blocks work
        sys.exit(0)
