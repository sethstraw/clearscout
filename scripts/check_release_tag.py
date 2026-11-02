"""Refuse a release tag that does not match the package version."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    """Return whether the release tag names the package version exactly."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: check_release_tag.py vX.Y.Z")
        return 64
    tag = args[0]
    version = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    expected = f"v{version}"
    if tag != expected:
        print(f"BLOCKER: release tag {tag!r} does not match package version {expected!r}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
