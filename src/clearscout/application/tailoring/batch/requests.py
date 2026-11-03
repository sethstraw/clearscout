"""Read and validate the postings that make up one batch."""

from __future__ import annotations

import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from clearscout.application.tailoring.stages.jobspec import StoredPostingName
from clearscout.shared.errors import ConfigError

# Require two to five postings for meaningful shared-requirement analysis.
MIN_POSTINGS = 2
MAX_POSTINGS = 5


@dataclass(frozen=True)
class Request:
    """One posting and what the person said about it before reading it."""

    source: str
    priority: int = 0
    note: str = ""
    stored_name: StoredPostingName | None = None


def requests_from(path: Path) -> tuple[Request, ...]:
    """Read a batch file: the postings, and any priority or note against each."""
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigError(f"Cannot read the batch file {path}: {error}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(
            f"{path} is not valid TOML: {error}\n"
            '  A batch file is a list of [[posting]] tables, each with a "source".'
        ) from error
    entries = payload.get("posting")
    if not isinstance(entries, list) or not entries:
        raise ConfigError(
            f"{path} lists no postings.\n"
            "  Each one is a [[posting]] table with a source, and optionally a priority "
            "and a note."
        )
    return tuple(_request(path, index, entry) for index, entry in enumerate(entries, 1))


def _request(path: Path, index: int, entry: object) -> Request:
    """Return one posting from a batch file, refusing an entry that is not one."""
    if not isinstance(entry, dict) or not isinstance(entry.get("source"), str):
        raise ConfigError(f"{path}: posting {index} has no source.")
    return Request(
        source=str(entry["source"]),
        priority=int(entry.get("priority", 0) or 0),
        note=str(entry.get("note", "") or ""),
    )


def _sized(requests: Sequence[Request]) -> None:
    """Refuse a batch that is not one, saying which command is."""
    if len(requests) < MIN_POSTINGS:
        raise ConfigError(
            f"A batch compares postings, and {len(requests)} was given.\n"
            f"  Use 'clearscout screen' for one posting, or give at least {MIN_POSTINGS}."
        )
    if len(requests) > MAX_POSTINGS:
        raise ConfigError(
            f"{len(requests)} postings is more than a batch.\n"
            f"  At most {MAX_POSTINGS}, because past that the shared-requirement analysis "
            "reports\n  that every posting wants everything, and the report stops being "
            "something you read\n  in one sitting. Run it twice."
        )


def _key(request: Request, posting_id: str, taken: set[str]) -> str:
    """Return a short readable handle for one posting inside this batch."""
    base = posting_id or request.source.rsplit("/", 1)[-1] or "posting"
    if base not in taken:
        return base
    suffix = 2
    while f"{base}-{suffix}" in taken:
        suffix += 1
    return f"{base}-{suffix}"
