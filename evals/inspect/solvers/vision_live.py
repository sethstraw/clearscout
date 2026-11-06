"""Live solver: send a locally rendered screenshot through production's vision reader.

Only ``scripts/eval.sh --live`` should reach this solver. It renders a synthetic
source to PNG, then reads it the way production reads a screenshot: a CV through
``sources.read`` and a posting through ``from_image``, both with ``vision_reader``
on the billed client.
"""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from clearscout.application.config import ProviderName, Settings, Stage
from clearscout.application.master_cv import sources
from clearscout.application.prompts import load
from clearscout.application.tailoring.pipeline.service import vision_reader
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.web_fetch.ingest.image import from_image
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
from evals.support.vision_render import Renderer, chrome_screenshot, source_text

VISION_PROMPTS = ("vision_system", "vision_user")
BILLING_ORIGIN = "eval.vision_live"


def transcribe(client: Any, image: Path, reader: str) -> list[str]:
    """Return the lines production's ``reader`` path keeps from one screenshot."""
    vision = vision_reader(client)
    if reader == "cv":
        return [line.text for line in sources.read(image, vision=vision).lines]
    if reader == "posting":
        return [line for line in from_image(image, vision=vision).text.splitlines() if line.strip()]
    raise ValueError(f"no vision reader path named {reader!r}")


@dataclass(frozen=True)
class _VisionLiveSolver:
    """One paid vision run: one screenshot per source and trial."""

    run_metadata: dict[str, Any]
    prompt_snapshot: Mapping[str, str]
    provider_name: ProviderName
    settings: Settings
    ledger: Ledger
    database: Database
    workspace_id: str
    render: Renderer

    def __call__(self, sample: dict[str, Any]) -> dict[str, Any]:
        current = {name: load(name) for name in VISION_PROMPTS}
        if (
            prompt_snapshot_fingerprint(VISION_PROMPTS, current)
            != self.run_metadata["prompt_suite_fingerprint"]
        ):
            raise RuntimeError(
                "Live evaluation refused: vision prompt bytes changed after run preflight."
            )
        png = self.render(source_text(str(sample["input"]["source"])))
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "screenshot.png"
            image.write_bytes(png)
            paid = paid_call(
                lambda client: transcribe(client, image, str(sample["input"]["reader"])),
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
        provenance["screenshot_sha256"] = hashlib.sha256(png).hexdigest()
        return {
            "completion": str(len(paid.value)),
            "transcript": paid.value,
            "line_count": len(paid.value),
            "metadata": provenance,
        }


def vision_live_solver(dataset: Path, render: Renderer = chrome_screenshot) -> _VisionLiveSolver:
    """Return the prepared solver that reads rendered screenshots with the live provider."""
    run_metadata = paid_stage_metadata(Stage.INGEST, VISION_PROMPTS, dataset)
    provider_name = ProviderName(run_metadata["provider"])
    settings = run_settings(provider_name, Stage.INGEST)
    database, workspace_id = billing_target()
    return _VisionLiveSolver(
        run_metadata=run_metadata,
        prompt_snapshot=MappingProxyType({name: load(name) for name in VISION_PROMPTS}),
        provider_name=provider_name,
        settings=settings,
        ledger=Ledger(prices=price_table(settings.prices)),
        database=database,
        workspace_id=workspace_id,
        render=render,
    )
