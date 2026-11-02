"""Typed inputs and outputs for durable workflow execution."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from tailorcv.application.config import Settings
from tailorcv.application.master_cv.cv import CvInventory
from tailorcv.application.tailoring.fit import Reading
from tailorcv.application.tailoring.pipeline.models import (
    SerializedRunArtifacts,
    StoredMasterCv,
)
from tailorcv.application.tailoring.stages.jobspec import JobSpec, StoredPostingName
from tailorcv.application.tailoring.stages.match import EvidenceMap
from tailorcv.domain import TailoringOutcome
from tailorcv.infrastructure.web_fetch.company_research import ResearchSource
from tailorcv.infrastructure.web_fetch.ingest import JDText
from tailorcv.shared.costs import Ledger, ServiceTier


@dataclass(frozen=True)
class ScreenOutput:
    """Normalized result of the shipped screening pipeline."""

    report: bytes
    outcome: str
    ledger: Ledger


@dataclass(frozen=True)
class EvaluationOutput:
    """Complete screening findings needed by the durable evaluation adapter."""

    spec: JobSpec
    evidence: EvidenceMap
    inventory: CvInventory
    ledger: Ledger
    jd: JDText
    reading: Reading
    outcome: str
    reasons: tuple[str, ...]
    prompt_fingerprint: str
    code_revision: str | None = None


@dataclass(frozen=True)
class TailorOutput:
    """Exact serialized package and outcome from one shipped tailoring run."""

    artifacts: SerializedRunArtifacts
    outcome: TailoringOutcome
    ledger: Ledger


@dataclass(frozen=True)
class BatchOutput:
    """Normalized report plus one independently produced screening per posting."""

    report: bytes
    record: bytes
    screenings: tuple[ScreenOutput, ...]


class WorkflowRunner(Protocol):
    """Injectable boundary for tailoring and isolated comparisons."""

    def tailor(
        self,
        source: Path,
        settings: Settings,
        *,
        master_cv: Path,
        cover_letter: bool,
        research: tuple[ResearchSource, ...] = (),
        force: bool,
        approval: dict[str, Any] | None,
        posting_hint: str | None = None,
        active_disputes: str | None = None,
        publisher_url: str | None = None,
        service_tier: ServiceTier = ServiceTier.STANDARD,
        ledger: Ledger | None = None,
        raise_if_cancelled: Callable[[], None] | None = None,
        stored_name: StoredPostingName | None = None,
        stored_cv: StoredMasterCv | None = None,
    ) -> TailorOutput:
        """Run the existing tailoring path."""
        ...

    def compare(
        self,
        sources: tuple[Path, ...],
        settings: Settings,
        *,
        master_cv: Path,
        output_dir: Path,
        priorities: tuple[int, ...],
        notes: tuple[str, ...],
        service_tier: ServiceTier = ServiceTier.STANDARD,
        ledgers: list[Ledger] | None = None,
        raise_if_cancelled: Callable[[], None] | None = None,
        stored_names: tuple[StoredPostingName, ...] | None = None,
    ) -> BatchOutput:
        """Run the existing isolated batch-screening path."""
        ...


@runtime_checkable
class EvaluationRunner(Protocol):
    """Narrow injected boundary for structured evaluation screening."""

    def evaluate(
        self,
        source: Path,
        settings: Settings,
        *,
        master_cv: Path,
        service_tier: ServiceTier = ServiceTier.STANDARD,
        ledger: Ledger | None = None,
        stored_name: StoredPostingName | None = None,
        listing_location: str = "",
        fresh: bool = False,
    ) -> EvaluationOutput:
        """Run screening and retain the structured findings for commitment."""
        ...
