"""Find the master CV without being told where it is.

A candidate is confirmed by parsing it, not by its name. When no single
candidate parses, the error says exactly where it looked rather than guessing.
"""

from __future__ import annotations

from pathlib import Path

from tailorcv.application.master_cv.cv import CvInventory, parse_cv
from tailorcv.shared.errors import ConfigError

# Prefer named files, then one unambiguous Markdown file in the CV directory.
NAMED = ("CV_Master.md", "CV.md", "Master_CV.md", "Resume.md")
FOLDERS = (".", "cv", "documents", "resume")


def _offered_by(directory: Path) -> list[Path]:
    """Return the files one directory puts forward, named files before the sole one."""
    offered = [path for name in NAMED if (path := directory / name).is_file()]
    markdown = sorted(path for path in directory.glob("*.md") if path.is_file())
    if len(markdown) == 1:
        offered.append(markdown[0])
    return offered


def candidates(start: Path | None = None) -> list[Path]:
    """Return every file that reads as a master CV, best guess first."""
    root = (start or Path.cwd()).resolve()
    seen: list[Path] = []
    for folder in FOLDERS:
        directory = root / folder
        if not directory.is_dir():
            continue
        for path in _offered_by(directory):
            if path not in seen:
                seen.append(path)
    return [path for path in seen if reads_as_cv(path)]


def reads_as_cv(path: Path) -> bool:
    """Return whether a file parses as a CV with real content in it.

    Parsing is the test rather than the filename, so a README that happens to
    be the only Markdown file in a folder fails here.
    """
    try:
        inventory = parse_cv(path)
    except Exception:  # noqa: BLE001 - any parse failure means "not a CV"
        return False
    return len(inventory.entries) >= 5 and bool(inventory.roles)


def resolve_master_cv(explicit: Path | None, start: Path | None = None) -> Path:
    """Return the master CV to use, or explain precisely what was searched."""
    if explicit is not None:
        path = explicit.expanduser()
        if not path.is_file():
            raise ConfigError(f"Master CV not found: {path}")
        return path.resolve()

    found = candidates(start)
    if len(found) == 1:
        return found[0]
    if not found:
        root = (start or Path.cwd()).resolve()
        raise ConfigError(
            f"No master CV found under {root}.\n"
            f"  Looked for {', '.join(NAMED)} in {', '.join(FOLDERS)}/,\n"
            "  and for a single Markdown file in those folders.\n"
            "  Pass --cv PATH, or run 'tailorcv init' to set one up."
        )
    listing = "\n".join(f"  - {path}" for path in found)
    raise ConfigError(
        f"Several files read as a master CV:\n{listing}\n"
        "Pass --cv PATH to choose one, or run 'tailorcv init'."
    )


def describe(inventory: CvInventory) -> str:
    """Return a one-line summary of what a parse produced."""
    return (
        f"{len(inventory.entries)} entries across {len(inventory.roles)} roles "
        f"(content hash {inventory.content_hash})"
    )
