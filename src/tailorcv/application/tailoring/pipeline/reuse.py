"""Return content-addressed runs that are already complete on disk."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tailorcv.application.config import Settings
from tailorcv.application.tailoring import changes, fit
from tailorcv.application.tailoring import runs as runs_store
from tailorcv.application.tailoring.stages import verify as verify_stage
from tailorcv.application.tailoring.stages.jobspec import JobSpec
from tailorcv.shared.costs import Ledger
from tailorcv.shared.errors import ConfigError

from .models import RunResult, SerializedRunArtifacts

# The stages ``--force`` asks again.
REDRAWN = frozenset({"draft", "critic", "cover"})


def _caching(settings: Settings, override: bool | None) -> bool:
    """Return whether this run should use the response cache."""
    return settings.cache if override is None else override


def _reuse(
    directory: Path, spec: JobSpec, ledger: Ledger, trim: int, reading: fit.Reading
) -> RunResult:
    """Load and return one complete run already materialized on disk."""
    manifest_json, manifest = _load_manifest(directory)
    started = str(manifest.get("run", {}).get("started") or "an earlier run")
    document = directory / f"{spec.slug()}.md"
    report = directory / "Tailoring_Report.md"
    letter = directory / "Cover_Letter.md"
    cover_letter = letter if letter.is_file() else None
    review_json_path = directory / changes.CHANGES_JSON
    review_path = review_json_path if review_json_path.is_file() else None
    review_markdown_path = directory / changes.CHANGES_MD
    review_markdown = review_markdown_path if review_markdown_path.is_file() else None
    review_json = review_path.read_bytes() if review_path is not None else None
    review_facts = json.loads(review_json) if review_json is not None else {}
    if not isinstance(review_facts, dict):
        raise ValueError("tailoring review must be a JSON object")
    return RunResult(
        verification=verify_stage.Report(
            findings=(), coverage=verify_stage.KeywordCoverage(present=(), missing=())
        ),
        spec=spec,
        ledger=ledger,
        attempts=0,
        trimmed=trim,
        serialized=SerializedRunArtifacts(
            document=document.read_bytes(),
            report=report.read_bytes(),
            manifest_json=manifest_json,
            manifest=manifest,
            cover_letter=cover_letter.read_bytes() if cover_letter is not None else None,
            review_json=review_json,
            review_markdown=review_markdown.read_bytes() if review_markdown is not None else None,
            review=review_facts,
        ),
        materialized=runs_store.MaterializedRun(
            directory=directory,
            document=document,
            report=report,
            cover_letter=cover_letter,
            review=review_path,
        ),
        reused=True,
        reading=reading,
        notes=[
            f"Nothing that decides the output has changed since {started}, so this "
            "run was returned unchanged and nothing was billed.",
            f"It {_outcome_phrase(manifest)} when it was drafted.",
            "Pass --force to draft it again anyway.",
        ],
        warnings=[],
    )


def _load_manifest(directory: Path) -> tuple[bytes, dict[str, Any]]:
    """Read one existing run manifest exactly once."""
    path = directory / runs_store.MANIFEST
    try:
        content = path.read_bytes()
        payload = json.loads(content)
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(
            f"{path} could not be read: {error}\n"
            "  Delete that run directory, or pass --force to draft the posting again."
        ) from error
    return content, payload if isinstance(payload, dict) else {}


def _outcome_phrase(manifest: dict[str, Any]) -> str:
    """Say what the returned run answered from its own record."""
    run_record = manifest.get("run", {})
    checks = int(run_record.get("checks", 0) or 0)
    passed = run_record.get("outcome") == "passed"
    if not checks:
        return "recorded no checks"
    return f"passed all {checks} checks" if passed else f"failed some of its {checks} checks"
