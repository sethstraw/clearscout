"""Identify loaded application bytes once for provider and run provenance."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from functools import cache
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

from tailorcv import __version__


@dataclass(frozen=True)
class BuildIdentity:
    """A source/build identity, distinct from the package's release version."""

    revision: str
    package_version: str


@cache
def current_build() -> BuildIdentity:
    """Snapshot source or installed distribution identity once per process."""
    return identify_build(Path(__file__).resolve().parents[1], __version__)


def identify_build(package: Path, version: str) -> BuildIdentity:
    """Identify executable source, saying so when an install carries no source."""
    sources = sorted(package.rglob("*.py"))
    if not sources:
        record = _distribution_record()
        revision = "record-sha256:" + hashlib.sha256(record.encode()).hexdigest() if record else ""
        return BuildIdentity(revision, version)
    repository = package.parent.parent
    prompts = (
        repository / "prompts" if (repository / "pyproject.toml").is_file() else package / "prompts"
    )
    digest = hashlib.sha256()
    for prefix, root, files in (
        ("source", package, sources),
        ("prompts", prompts, sorted(prompts.glob("*.md"))),
    ):
        for path in files:
            digest.update(f"{prefix}/{path.relative_to(root)}\0".encode())
            digest.update(path.read_bytes())
            digest.update(b"\0")
    revision = "sha256:" + digest.hexdigest()
    if git := _git_identity(repository):
        revision = f"{git}/{revision}"
    return BuildIdentity(revision, version)


def _distribution_record() -> str:
    try:
        return distribution("tailorcv").read_text("RECORD") or ""
    except (PackageNotFoundError, OSError):
        return ""


def _git_identity(repository: Path) -> str:
    if not (repository / ".git").exists():
        return ""
    executable = shutil.which("git")
    if executable is None:
        return ""
    try:
        revision = subprocess.run(  # noqa: S603 - fixed arguments to the resolved Git executable
            [executable, "rev-parse", "HEAD"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
        changed = subprocess.run(  # noqa: S603 - fixed arguments to the resolved Git executable
            [
                executable,
                "status",
                "--porcelain",
                "--untracked-files=normal",
                "--",
                "src/tailorcv",
                "prompts",
                "pyproject.toml",
                "uv.lock",
            ],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    return f"git:{revision}{'+dirty' if changed else ''}"
