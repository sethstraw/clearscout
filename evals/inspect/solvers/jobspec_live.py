"""Live solver: run the shipped jobspec extraction against the real provider.

Only ``scripts/eval.sh --live`` should reach this solver. It sends one posting per
call with the task's synthetic stated boundaries and records the boundary that
survived the program's span check, which is the one that skips a posting.
The extraction solver sends no boundaries and records the requirements kept.
Each declared prompt arm holds one file per name in ``JOBSPEC_PROMPTS``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clearscout.application.config import ProviderName, Settings, Stage
from clearscout.application.tailoring.stages.jobspec import JobSpec, extract
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.web_fetch.ingest.text import JDText
from clearscout.shared.costs import Ledger
from clearscout.shared.costs.prices import table as price_table
from clearscout.shared.errors import NoRequirementsError
from clearscout.shared.layout import plain_layout
from evals.inspect.labeling import bytes_sha256
from evals.inspect.solvers.clearscout_live import (
    PromptArm,
    arms_metadata,
    billing_target,
    declared_arms,
    paid_call,
    paid_stage_metadata,
    prompt_snapshot_fingerprint,
    requested_arm,
    run_settings,
    sample_provenance,
)

JOBSPEC_PROMPTS = ("jobspec_system", "jobspec_user")
BILLING_ORIGIN = "eval.jobspec_boundary_live"
EXTRACTION_BILLING_ORIGIN = "eval.jobspec_extraction_live"


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


def extraction_result(spec: JobSpec | None) -> dict[str, Any]:
    """Return the requirements one extraction kept, or none when it read none."""
    requirements = [] if spec is None else spec.requirements
    return {
        "completion": str(len(requirements)),
        "requirements": [
            {
                "id": item.id,
                "text": item.text,
                "quote": item.quote_or_span,
                "importance": item.importance.value,
                "verified": item.verified,
            }
            for item in requirements
        ],
    }


def _extract_or_none(client: Any, sample: dict[str, Any]) -> JobSpec | None:
    # The stage refuses a posting with no requirements; here that is a scorable answer.
    try:
        return extract(client, posting(sample))
    except NoRequirementsError:
        return None


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
        paid = paid_call(
            lambda client: extract(client, posting(sample), boundaries=self.boundaries),
            provider_name=self.provider_name,
            settings=self.settings,
            run_ledger=self.ledger,
            run_metadata=self.run_metadata,
            prompt_snapshot=arm.snapshot,
            database=self.database,
            workspace_id=self.workspace_id,
            origin=BILLING_ORIGIN,
        )
        spec = paid.value
        provenance = sample_provenance(
            paid.ledger, self.ledger, self.run_metadata, paid.billing_failure
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


@dataclass(frozen=True)
class _JobspecExtractionLiveSolver:
    """One paid extraction run: one call per posting, trial and arm."""

    run_metadata: dict[str, Any]
    arms: tuple[PromptArm, ...]
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
        paid = paid_call(
            lambda client: _extract_or_none(client, sample),
            provider_name=self.provider_name,
            settings=self.settings,
            run_ledger=self.ledger,
            run_metadata=self.run_metadata,
            prompt_snapshot=arm.snapshot,
            database=self.database,
            workspace_id=self.workspace_id,
            origin=EXTRACTION_BILLING_ORIGIN,
        )
        provenance = sample_provenance(
            paid.ledger, self.ledger, self.run_metadata, paid.billing_failure
        )
        if arm.name is not None:
            provenance["arm"] = arm.name
        return {**extraction_result(paid.value), "metadata": provenance}


def jobspec_extraction_live_solver(dataset: Path) -> _JobspecExtractionLiveSolver:
    """Return the prepared solver that runs jobspec extraction live with no boundaries."""
    run_metadata = paid_stage_metadata(Stage.JOBSPEC, JOBSPEC_PROMPTS, dataset)
    arms = declared_arms(JOBSPEC_PROMPTS)
    if declared := arms_metadata(arms):
        # Declared arms supply every call's bytes, so the working-tree fingerprint names none.
        run_metadata.pop("prompt_suite_fingerprint", None)
        run_metadata.update(declared)
    provider_name = ProviderName(run_metadata["provider"])
    settings = run_settings(provider_name, Stage.JOBSPEC)
    database, workspace_id = billing_target()
    return _JobspecExtractionLiveSolver(
        run_metadata=run_metadata,
        arms=arms,
        provider_name=provider_name,
        settings=settings,
        ledger=Ledger(prices=price_table(settings.prices)),
        database=database,
        workspace_id=workspace_id,
    )
