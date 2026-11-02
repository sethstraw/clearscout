#!/usr/bin/env python3
"""Refuse git stash for every caller; tree-rewriting git and the full gate for a subagent.

PreToolUse hook on Bash. A stash or checkout in a shared checkout can revert or
rewrite files a concurrent agent is writing, and this hook cannot see whether one
is running. Fails open on unparseable stdin; fails closed on an unclassifiable stash.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

READ_STASH_SUBCOMMANDS = {"list", "show"}

# Denied only for a subagent; a human or the main session approves these through
# the ordinary permission rules.
AGENT_DENY_SUBCOMMANDS = {
    "checkout",
    "switch",
    "restore",
    "reset",
    "clean",
    "rebase",
    "merge",
    "pull",
    "cherry-pick",
    "revert",
    "am",
    "apply",
}

WRAPPERS = {"env", "command", "sudo", "time", "nohup", "xargs", "exec", "nice", "watch", "timeout"}
# Reserved words that can precede a command without naming one themselves.
KEYWORDS = {
    "if",
    "then",
    "elif",
    "else",
    "fi",
    "do",
    "done",
    "while",
    "until",
    "for",
    "in",
    "case",
    "esac",
    "!",
}
SHELL_C_WRAPPERS = {"bash", "sh", "zsh"}
GATE_SCRIPTS = {"gate.sh"}
GATE_RECIPES = {"gate"}
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# `timeout 5 cmd` and `timeout 1.5m cmd`: a bare duration, not a flag or the command.
TIMEOUT_DURATION = re.compile(r"^\d+(\.\d+)?[smhd]?$")
MAX_DEPTH = 6

GIT_GLOBAL_NOARG = {
    "--no-pager",
    "-P",
    "--no-replace-objects",
    "--bare",
    "--literal-pathspecs",
    "--no-optional-locks",
}
GIT_GLOBAL_WITHARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}

GIT_TOKEN = re.compile(r"(?:^|[\s;&|()`])git(?=[\s;&|()`]|$)")
STASH_TOKEN = re.compile(r"(?:^|[\s;&|()`])stash(?=[\s;&|()`]|$)")

STASH_REASON = (
    "Refused: other agents may be writing in this shared checkout, and a git "
    "stash rewrites their files. `git stash list`/`show` are fine; a human can "
    "run `stash` themselves outside this session."
)
AGENT_REASON = (
    "Refused for a subagent: other agents may be writing in this shared "
    "checkout, and this subcommand rewrites the working tree or history. "
    "Read-only git stays allowed; a human can run this directly."
)
GATE_REASON = (
    "Refused for a subagent: scripts/gate.sh runs once, from the main thread, on "
    "the integrated tree. Run focused tests with `just pytest-log <name> <files>` "
    "and report the log path instead."
)


class AmbiguousError(Exception):
    """A git-then-stash shape the parser could not classify as a read."""


def loose_git_then_stash(text: str) -> bool:
    """Return True when 'git' appears and 'stash' appears somewhere after it."""
    match = GIT_TOKEN.search(text)
    return bool(match and STASH_TOKEN.search(text, match.end()))


def split_top_level(text: str) -> list[str]:
    """Split on ; && || | & newlines and grouping punctuation, respecting quotes."""
    segments: list[str] = []
    buf: list[str] = []
    quote = None
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if quote:
            buf.append(c)
            if quote == '"' and c == "\\" and i + 1 < n:
                buf.append(text[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            buf.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            buf.append(c)
            buf.append(text[i + 1])
            i += 2
            continue
        if text[i : i + 2] in ("&&", "||"):
            segments.append("".join(buf))
            buf = []
            i += 2
            continue
        if c in (";", "|", "&", "\n", "(", ")", "{", "}"):
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    segments.append("".join(buf))
    return [s.strip() for s in segments if s.strip()]


def extract_and_neutralize(text: str) -> tuple[str, list[str]]:
    """Pull out $(...) and backtick bodies, leaving a neutral token in each place.

    Raises ValueError when a span is unbalanced.
    """
    out: list[str] = []
    subs: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text[i : i + 2] == "$(":
            depth = 1
            j = i + 2
            while j < n and depth:
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                j += 1
            if depth:
                raise ValueError("unbalanced $(")
            subs.append(text[i + 2 : j - 1])
            out.append("x")
            i = j
            continue
        if text[i] == "`":
            j = text.find("`", i + 1)
            if j == -1:
                raise ValueError("unbalanced `")
            subs.append(text[i + 1 : j])
            out.append("x")
            i = j + 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out), subs


def skip_wrappers(tokens: list[str]) -> int:
    """Return the index past leading assignments, shell keywords, and wrapper commands."""
    i, n = 0, len(tokens)
    while i < n:
        t = tokens[i]
        if ASSIGNMENT.match(t) or t in KEYWORDS:
            i += 1
            continue
        if t in WRAPPERS:
            i += 1
            if t == "timeout" and i < n and TIMEOUT_DURATION.match(tokens[i]):
                i += 1
            continue
        break
    return i


def skip_git_global_options(tokens: list[str], i: int) -> int:
    """Return the index of the git subcommand, past global options like -C and -c."""
    n = len(tokens)
    while i < n:
        t = tokens[i]
        if t in GIT_GLOBAL_NOARG:
            i += 1
        elif t in GIT_GLOBAL_WITHARG:
            i += 2
        elif any(t.startswith(p + "=") for p in GIT_GLOBAL_WITHARG):
            i += 1
        else:
            break
    return i


def _git_verdict(tokens: list[str], i: int, agent_id: str) -> str | None:
    j = skip_git_global_options(tokens, i + 1)
    if j >= len(tokens):
        return None
    subcommand = tokens[j]
    if subcommand == "stash":
        # Anything but a plain list/show cannot be proven to leave files alone.
        if j + 1 < len(tokens) and tokens[j + 1] in READ_STASH_SUBCOMMANDS:
            return None
        return "stash"
    if agent_id:
        if subcommand in AGENT_DENY_SUBCOMMANDS:
            return "agent"
        if subcommand == "worktree" and j + 1 < len(tokens) and tokens[j + 1] == "remove":
            return "agent"
    return None


def analyze_segment(segment: str, agent_id: str, depth: int) -> str | None:
    """Return the refusal kind for one simple command, or None to allow it."""
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError as exc:
        if loose_git_then_stash(segment):
            raise AmbiguousError(segment) from exc
        return None

    i = skip_wrappers(tokens)
    if i >= len(tokens):
        return None
    base = Path(tokens[i]).name

    if base in SHELL_C_WRAPPERS and "-c" in tokens[i + 1 :]:
        ci = tokens.index("-c", i + 1)
        if ci + 1 < len(tokens):
            return analyze(tokens[ci + 1], agent_id, depth + 1)
        return None

    if agent_id:
        if base in SHELL_C_WRAPPERS and i + 1 < len(tokens):
            base = Path(tokens[i + 1]).name
        if base in GATE_SCRIPTS:
            return "gate"
        if base == "just" and GATE_RECIPES & set(tokens[i + 1 : i + 2]):
            return "gate"

    if base != "git":
        return None
    return _git_verdict(tokens, i, agent_id)


def analyze(text: str, agent_id: str, depth: int = 0) -> str | None:
    """Return the refusal kind for a whole command line, or None to allow it."""
    if depth > MAX_DEPTH:
        return None
    try:
        neutral, subs = extract_and_neutralize(text)
    except ValueError as exc:
        if loose_git_then_stash(text):
            raise AmbiguousError(text) from exc
        return None

    for inner in subs:
        result = analyze(inner, agent_id, depth + 1)
        if result:
            return result

    for segment in split_top_level(neutral):
        result = analyze_segment(segment, agent_id, depth)
        if result:
            return result
    return None


def deny(reason: str) -> None:
    """Print a PreToolUse deny decision carrying the reason."""
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


def main() -> int:
    """Hook entry point: print a deny decision or nothing, and exit 0."""
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    command = str(payload.get("tool_input", {}).get("command", "")).strip()
    if not command:
        return 0

    agent_id = str(payload.get("agent_id") or "").strip()
    try:
        result = analyze(command, agent_id)
    except AmbiguousError:
        result = "stash"
    except Exception:  # noqa: BLE001 - unreadable shape, judge by the raw text
        result = "stash" if loose_git_then_stash(command) else None

    reasons = {"stash": STASH_REASON, "agent": AGENT_REASON, "gate": GATE_REASON}
    if result in reasons:
        deny(reasons[result])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - fails open
        sys.exit(0)
