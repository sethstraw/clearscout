"""Refuse broken local Markdown links without making a network request."""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from fnmatch import fnmatch
from os import walk
from pathlib import Path
from urllib.parse import unquote, urlsplit

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
INLINE_LINK = re.compile(r"!?\[([^\]]+)\]\([^)]+\)")
HTML_TAG = re.compile(r"<[^>]+>")
ANCHOR_PUNCTUATION = re.compile(r"[^a-z0-9_ -]")
# Exclude ignored local state so results are stable across clones.
SKIPPED_DIRECTORIES = frozenset({".git", ".serena", ".venv", "dist", "node_modules"})
SKIPPED_PATHS = frozenset(
    {".claude/agent-memory", ".claude/commands", ".claude/state", ".claude/worktrees"}
)
SKIPPED_FILES = ("CLAUDE.local.md", ".claude/rules/local-*.md")
# Match backticked repository paths while ignoring route, rule, and symbol syntax.
SOURCE_PATH = re.compile(
    r"`([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+\.(?:py|ts|tsx|css|sh|toml|json|md))`"
)


def repository_files(root: Path) -> Iterator[Path]:
    """Yield files while pruning ignored trees before descending into them."""
    for directory, children, files in walk(root):
        relative = Path(directory).relative_to(root)
        children[:] = [
            child
            for child in children
            if child not in SKIPPED_DIRECTORIES
            and (relative / child).as_posix() not in SKIPPED_PATHS
        ]
        yield from (
            Path(directory) / name
            for name in files
            if not any(fnmatch((relative / name).as_posix(), skip) for skip in SKIPPED_FILES)
        )


def markdown_files(root: Path) -> list[Path]:
    """Return project Markdown files while excluding generated dependency trees."""
    return sorted(path for path in repository_files(root) if path.suffix == ".md")


def _heading_anchor(label: str) -> str:
    """Return the GitHub-style anchor for one ASCII Markdown heading."""
    label = INLINE_LINK.sub(r"\1", label)
    label = HTML_TAG.sub("", label)
    label = label.replace("`", "").replace("*", "").replace("~", "").lower()
    label = ANCHOR_PUNCTUATION.sub("", label)
    return re.sub(r" +", "-", label.strip())


def heading_anchors(document: Path) -> set[str]:
    """Return the anchors GitHub assigns to headings, including duplicate suffixes."""
    anchors: set[str] = set()
    occurrences: dict[str, int] = {}
    for line in document.read_text(encoding="utf-8").splitlines():
        match = HEADING.match(line)
        if match is None:
            continue
        base = _heading_anchor(match.group(1))
        count = occurrences.get(base, 0)
        occurrences[base] = count + 1
        anchors.add(base if count == 0 else f"{base}-{count}")
    return anchors


def broken_links(root: Path) -> list[str]:
    """Return stable diagnostics for local targets and Markdown anchors that do not exist."""
    failures: list[str] = []
    anchors: dict[Path, set[str]] = {}
    for document in markdown_files(root):
        text = document.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            for match in LINK.finditer(line):
                raw = match.group(1).strip().strip("<>")
                target_text = raw.split(maxsplit=1)[0]
                if target_text.upper() in {"N/A", "UNKNOWN"}:
                    continue
                parsed = urlsplit(target_text)
                if parsed.scheme or parsed.netloc or not parsed.path:
                    continue
                target = (document.parent / unquote(parsed.path)).resolve()
                if not target.exists():
                    failures.append(
                        f"{document.relative_to(root)}:{line_number}: missing {target_text}"
                    )
                    continue
                if parsed.fragment and target.suffix == ".md":
                    known = anchors.setdefault(target, heading_anchors(target))
                    fragment = unquote(parsed.fragment)
                    if fragment not in known:
                        failures.append(
                            f"{document.relative_to(root)}:{line_number}: missing {target_text}"
                        )
    return failures


def missing_source_paths(root: Path) -> list[str]:
    """Return diagnostics for named repository paths that no longer exist.

    A document naming a module that has moved is worse than one naming none,
    because the reader goes looking. Matching allows a suffix, so prose may name
    a module by its last path segments instead of from the repository root.
    """
    failures: list[str] = []
    known = {str(path.relative_to(root)) for path in repository_files(root)}
    for document in markdown_files(root):
        for line_number, line in enumerate(
            document.read_text(encoding="utf-8").splitlines(), start=1
        ):
            for match in SOURCE_PATH.finditer(line):
                named = match.group(1)
                if named in known or any(path.endswith("/" + named) for path in known):
                    continue
                failures.append(f"{document.relative_to(root)}:{line_number}: missing {named}")
    return failures


def main(root: Path = REPOSITORY_ROOT) -> int:
    """Check one repository, this script's own by default, and print only actionable failures."""
    failures = broken_links(root)
    if failures:
        print("Broken local Markdown links:")
        print("\n".join(f"  {item}" for item in failures))
        return 1
    stale = missing_source_paths(root)
    if stale:
        print("Documented paths that do not exist:")
        print("\n".join(f"  {item}" for item in stale))
        return 1
    print(f"Local Markdown links are valid across {len(markdown_files(root))} files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
