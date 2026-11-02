"""Live solver: run the shipped jobspec extraction against the real provider.

Only ``scripts/eval.sh --live`` should reach this solver. It sends one posting per
call with the task's synthetic stated boundaries and records the boundary that
survived the program's span check, which is the one that skips a posting.
Each declared prompt arm holds one file per name in ``JOBSPEC_PROMPTS``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evals.inspect.labeling import bytes_sha256
from evals.inspect.solvers.tailorcv_live import (
    PromptArm,
    arms_metadata,
    bill_sample,
    billing_target,
    credential_for,
    declared_arms,
    paid_stage_metadata,
    prompt_snapshot_fingerprint,
    requested_arm,
    run_settings,
    sample_provenance,
)
from evals.support.client import build_live_client
from tailorcv.application.config import ProviderName, Settings, Stage
from tailorcv.application.prompts import overriding
from tailorcv.application.tailoring.stages.jobspec import JobSpec, extract
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.web_fetch.ingest.text import JDText
from tailorcv.shared.costs import Ledger
from tailorcv.shared.costs.prices import table as price_table
from tailorcv.shared.layout import plain_layout

JOBSPEC_PROMPTS = ("jobspec_system", "jobspec_user")
BILLING_ORIGIN = "eval.jobspec_boundary_live"


def load_boundaries(path: Path) -> tuple[str, ...]:
    """Return the numbered stated boundaries a boundary task sends, in order."""
    boundaries = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(boundaries, list) or not all(
        isinstance(item, str) and item.strip() for item in boundaries
    ):
        raise ValueError(f"{path} must be a JSON list of non-empty boundary strings")
    return tuple(boundaries)


def posting(sample: dict[str, Any]) -> JDText:
    """Return the posting a sample carries, laid out as a pasted text file is."""
    return JDText(
        text=plain_layout(str(sample["input"])), origin=f"eval:{sample['id']}", method="plain text"
    )


def boundary_result(spec: JobSpec, boundaries: Sequence[str]) -> dict[str, Any]:
    """Return the fired boundary's number and what the extraction cited for it."""
    fired = spec.excluded_boundary
    number = None if fired is None else list(boundaries).index(fired) + 1
    occupation = spec.core_occupation
    return {
        "completion": "none" if number is None else str(number),
        "boundary": number,
        "excluded_boundary_quote": spec.excluded_boundary_quote,
        "core_occupation": None if occupation is None else occupation.text,
        "core_occupation_quote": None if occupation is None else occupation.quote_or_span,
    }


@dataclass(frozen=True)
class _JobspecBoundaryLiveSolver:
    """One paid boundary run: one extraction per posting, trial and arm."""

    run_metadata: dict[str, Any]
    arms: tuple[PromptArm, ...]
    boundaries: tuple[str, ...]
    provider_name: ProviderName
    settings: Settings
    ledger: Ledger
    database: Database
    workspace_id: str

    def __call__(self, sample: dict[str, Any]) -> dict[str, Any]:
        arm = requested_arm(self.arms, sample.get("arm"))
        if prompt_snapshot_fingerprint(JOBSPEC_PROMPTS, arm.current()) != arm.fingerprint:
            raise RuntimeError(
                "Live evaluation refused: jobspec prompt bytes changed after run preflight."
            )
        sample_ledger = Ledger(prices=self.ledger.prices)
        client = build_live_client(
            self.provider_name,
            credential_for(self.provider_name),
            ledger=sample_ledger,
            settings=self.settings,
        )
        try:
            with overriding(arm.snapshot):
                spec = extract(client, posting(sample), boundaries=self.boundaries)
        finally:
            self.ledger.entries.extend(sample_ledger.entries)
            billing_failure = bill_sample(
                self.database,
                self.workspace_id,
                ledger=sample_ledger,
                provider=str(self.run_metadata["provider"]),
                origin=BILLING_ORIGIN,
            )
        provenance = sample_provenance(
            sample_ledger, self.ledger, self.run_metadata, billing_failure
        )
        if arm.name is not None:
            provenance["arm"] = arm.name
        return {**boundary_result(spec, self.boundaries), "metadata": provenance}


def jobspec_boundary_live_solver(
    dataset: Path, boundaries_path: Path
) -> _JobspecBoundaryLiveSolver:
    """Return the prepared solver that runs jobspec extraction live over the boundary set."""
    run_metadata = paid_stage_metadata(Stage.JOBSPEC, JOBSPEC_PROMPTS, dataset)
    arms = declared_arms(JOBSPEC_PROMPTS)
    if declared := arms_metadata(arms):
        # Declared arms supply every call's bytes, so the working-tree fingerprint names none.
        run_metadata.pop("prompt_suite_fingerprint", None)
        run_metadata.update(declared)
    boundaries = load_boundaries(boundaries_path)
    run_metadata["boundaries_sha256"] = bytes_sha256(boundaries_path.read_bytes())
    provider_name = ProviderName(run_metadata["provider"])
    settings = run_settings(provider_name, Stage.JOBSPEC)
    database, workspace_id = billing_target()
    return _JobspecBoundaryLiveSolver(
        run_metadata=run_metadata,
        arms=arms,
        boundaries=boundaries,
        provider_name=provider_name,
        settings=settings,
        ledger=Ledger(prices=price_table(settings.prices)),
        database=database,
        workspace_id=workspace_id,
    )
