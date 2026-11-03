#!/usr/bin/env python3
"""Nudge when substantive work happened without reading a first-read document.

Stop hook. It can confirm only that a file was opened, not understood, so it
shows a warning rather than blocking: a two-line fix legitimately needs none of this.
The warning shows at most once per session.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

# The agent instructions file loads into every session, so it is not listed.
ONBOARDING_DOCS = (
    "README.md",
    "ARCHITECTURE.md",
    "CONTRIBUTING.md",
    "docs/contributing/backend-development.md",
    "docs/contributing/frontend-development.md",
)
MUTATING_TOOLS = {"Write", "Edit", "NotebookEdit"}
MUTATING_MCP_PREFIXES = (
    "mcp__serena__replace",
    "mcp__serena__insert",
    "mcp__serena__rename",
    "mcp__serena__safe_delete",
)
MUTATING_THRESHOLD = 3

# Edits made with sed and redirects count as much as the file tools do.
BASH_WRITE = re.compile(
    r"(?:\bsed\s+-i\b|\btee\b|(?<![0-9<>])>>?(?!\s*/dev/null)|<<-?\s*['\"]?[A-Za-z_])"
)
SCRATCH = re.compile(r"(?:/private)?/(?:tmp|var/tmp)/|\$TMPDIR")

MESSAGE = (
    "Stop hook: this session made {mutations} edit/write calls but never read "
    "{docs}. If this was substantive work rather than a small isolated fix, "
    "confirm the change matches current project state before it lands."
)


def reads_onboarding_doc(block: dict[str, Any]) -> bool:
    """Return True when this tool_use opened one of the first-read documents."""
    name = block.get("name") or ""
    tool_input = block.get("input") or {}
    if name == "Read":
        return str(tool_input.get("file_path") or "").endswith(ONBOARDING_DOCS)
    if name == "Bash":
        command = str(tool_input.get("command") or "")
        return any(doc in command for doc in ONBOARDING_DOCS)
    return False


def is_mutating(block: dict[str, Any]) -> bool:
    """Return True when this tool_use changed a file somewhere that is not scratch."""
    name = block.get("name") or ""
    if name in MUTATING_TOOLS or name.startswith(MUTATING_MCP_PREFIXES):
        return True
    if name != "Bash":
        return False
    command = str((block.get("input") or {}).get("command") or "")
    return bool(BASH_WRITE.search(command)) and not SCRATCH.search(command)


def _tool_uses(entry: dict[str, Any]) -> list[dict[str, Any]]:
    if entry.get("type") != "assistant":
        return []
    content = (entry.get("message") or {}).get("content") or []
    return [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]


def scan(transcript_path: Path) -> tuple[int, bool]:
    """Return (mutating call count, whether any first-read document was opened)."""
    mutations = 0
    read_doc = False
    try:
        with transcript_path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                try:
                    blocks = _tool_uses(json.loads(raw)) if raw.strip() else []
                except ValueError:
                    continue
                for block in blocks:
                    mutations += is_mutating(block)
                    read_doc = read_doc or reads_onboarding_doc(block)
    except OSError:
        return 0, True
    return mutations, read_doc


def nudge_marker(session_id: str) -> Path | None:
    """Return the per-session file recording that the nudge was shown, or None without an id."""
    safe = "".join(c if c.isalnum() or c in "-." else "-" for c in session_id)[:80]
    if not safe.strip("-."):
        return None
    # Outside the checkout, so the marker never shows up as a working-tree change.
    return Path(tempfile.gettempdir()) / "clearscout-hooks" / f"onboarding-nudged-{safe}"


def main() -> int:
    """Hook entry point: print a warning when it applies, and always exit 0."""
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    # Claude Code re-runs Stop hooks after a block; nudge once per turn.
    if payload.get("stop_hook_active"):
        return 0
    transcript = str(payload.get("transcript_path") or "")
    if not transcript:
        return 0

    mutations, read_doc = scan(Path(transcript))
    if read_doc or mutations < MUTATING_THRESHOLD:
        return 0
    marker = nudge_marker(str(payload.get("session_id") or ""))
    if marker is not None:
        if marker.exists():
            return 0
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    # A systemMessage on exit 0 warns the user without forcing another turn.
    message = MESSAGE.format(mutations=mutations, docs=", ".join(ONBOARDING_DOCS))
    json.dump({"systemMessage": message}, sys.stdout)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - never blocks stop
        sys.exit(0)
