"""Live solver: run production's CV import steps on one synthetic corpus CV.

Only ``scripts/eval.sh --live`` should reach this solver. It reads the source the
way ``import_cv`` does, with the vision reader on the same client, asks the
structure stage once, and records what the program assembled and verified.
``import_cv`` builds its own client and writes files, so its steps are run here
on the billed client instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from clearscout.application.config import ProviderName, Settings, Stage
from clearscout.application.master_cv import sources
from clearscout.application.prompts import load
from clearscout.application.tailoring.pipeline import importer
from clearscout.application.tailoring.pipeline.service import vision_reader
from clearscout.application.tailoring.stages import structure as structure_stage
from clearscout.infrastructure.persistence.database import Database
from clearscout.shared.costs import Ledger
from clearscout.shared.costs.prices import table as price_table
from evals.inspect.solvers.clearscout_live import (
    billing_target,
    paid_call,
    paid_stage_metadata,
    prompt_snapshot_fingerprint,
    run_settings,
    sample_provenance,
)
from evals.support.cv_formats import cv_format

STRUCTURE_PROMPTS = ("structure_system", "structure_user")
BILLING_ORIGIN = "eval.structure_live"


def import_steps(client: Any, path: Path, settings: Settings) -> dict[str, Any]:
    """Run ``import_cv``'s read, structure, assemble and verify steps and return what they kept."""
    source = sources.read(path, vision=vision_reader(client))
    extracted = structure_stage.structure(client, source)
    built = importer.assemble(extracted, source, settings.sections)
    found = importer.verify(source, extracted, built)
    return {
        "completion": str(len(extracted.roles)),
        "line_count": len(source.lines),
        "findings": [
            {"check": item.check, "outcome": item.outcome.value, "detail": item.detail}
            for item in found
        ],
        "extracted": extracted.model_dump(mode="json"),
        "dropped": [
            {"line": line.number, "text": line.text, "reason": reason}
            for line, reason in built.dropped
        ],
    }


@dataclass(frozen=True)
class _StructureLiveSolver:
    """One paid import run: one structure call per corpus CV and trial."""

    run_metadata: dict[str, Any]
    prompt_snapshot: Mapping[str, str]
    provider_name: ProviderName
    settings: Settings
    ledger: Ledger
    database: Database
    workspace_id: str

    def __call__(self, sample: dict[str, Any]) -> dict[str, Any]:
        current = {name: load(name) for name in STRUCTURE_PROMPTS}
        if (
            prompt_snapshot_fingerprint(STRUCTURE_PROMPTS, current)
            != self.run_metadata["prompt_suite_fingerprint"]
        ):
            raise RuntimeError(
                "Live evaluation refused: structure prompt bytes changed after run preflight."
            )
        path, _ = cv_format(str(sample["input"]["shape"]))
        paid = paid_call(
            lambda client: import_steps(client, path, self.settings),
            provider_name=self.provider_name,
            settings=self.settings,
            run_ledger=self.ledger,
            run_metadata=self.run_metadata,
            prompt_snapshot=self.prompt_snapshot,
            database=self.database,
            workspace_id=self.workspace_id,
            origin=BILLING_ORIGIN,
        )
        provenance = sample_provenance(
            paid.ledger, self.ledger, self.run_metadata, paid.billing_failure
        )
        return {**paid.value, "metadata": provenance}


def structure_live_solver(dataset: Path) -> _StructureLiveSolver:
    """Return the prepared solver that imports each corpus CV against the live provider."""
    run_metadata = paid_stage_metadata(Stage.STRUCTURE, STRUCTURE_PROMPTS, dataset)
    provider_name = ProviderName(run_metadata["provider"])
    settings = run_settings(provider_name, Stage.STRUCTURE)
    database, workspace_id = billing_target()
    return _StructureLiveSolver(
        run_metadata=run_metadata,
        prompt_snapshot=MappingProxyType({name: load(name) for name in STRUCTURE_PROMPTS}),
        provider_name=provider_name,
        settings=settings,
        ledger=Ledger(prices=price_table(settings.prices)),
        database=database,
        workspace_id=workspace_id,
    )
