#!/usr/bin/env python3
"""Snapshot repository state and the last five user prompts before compaction.

PreCompact hook. Writes a small markdown file under .claude/state/, keeping the
five newest. Silent, never blocks.
"""

from __future__ import annotations

import datetime as _dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

GIT_TIMEOUT = 5
KEEP_SNAPSHOTS = 5
MAX_PROMPTS = 5
PROMPT_CHAR_CAP = 600


def run_git(args: list[str], cwd: Path) -> str:
    """Return git's stripped stdout, or an empty string when git fails."""
    result = subprocess.run(  # noqa: S603 - fixed argv
        ["git", *args],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
        cwd=str(cwd),
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _prompt_text(entry: dict[str, Any]) -> str:
    message = entry.get("message") or {}
    if entry.get("type") != "user" or message.get("role") != "user":
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    for chunk in content if isinstance(content, list) else []:
        if isinstance(chunk, dict) and chunk.get("type") == "text":
            text = chunk.get("text")
            if isinstance(text, str):
                return text.strip()
    return ""


def last_user_prompts(transcript_path: Path) -> list[str]:
    """Return the last few prompts the user typed, skipping tagged system text."""
    if not transcript_path.is_file():
        return []
    prompts: list[str] = []
    try:
        with transcript_path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                if '"user"' not in raw:
                    continue
                try:
                    text = _prompt_text(json.loads(raw))
                except ValueError:
                    continue
                # System reminders and tool results are wrapped in tags.
                if text and not text.startswith("<"):
                    prompts.append(text[:PROMPT_CHAR_CAP])
    except OSError:
        return []
    return prompts[-MAX_PROMPTS:]


def main() -> int:
    """Hook entry point: write the snapshot and always exit 0."""
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        payload = {}

    root = run_git(["rev-parse", "--show-toplevel"], Path(str(payload.get("cwd") or Path.cwd())))
    if not root:
        return 0
    cwd = Path(root)

    branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd) or "unknown"
    status = run_git(["status", "--short"], cwd)
    transcript = str(payload.get("transcript_path") or "")
    prompts = last_user_prompts(Path(transcript)) if transcript else []

    stamp = _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    state_dir = cwd / ".claude" / "state"
    session_id = str(payload.get("session_id") or "unknown")
    safe_session = "".join(c if c.isalnum() or c in "-." else "-" for c in session_id)[:80]
    target = state_dir / f"precompact-{safe_session}-{stamp}.md"

    body = [f"# PreCompact snapshot {stamp}", "", f"branch: {branch}", ""]
    body.extend(["## git status --short", "", status or "(clean)", "", "## Last user prompts"])
    for i, text in enumerate(prompts, 1):
        body.extend(["", f"### Prompt {i}", "", text])
    if not prompts:
        body.extend(["", "(no prompts captured)"])

    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(body) + "\n", encoding="utf-8")
        snapshots = sorted(state_dir.glob("precompact-*.md"), key=lambda p: p.stat().st_mtime)
        for stale in snapshots[:-KEEP_SNAPSHOTS]:
            stale.unlink()
    except OSError:
        return 0
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - never blocks compaction
        sys.exit(0)
