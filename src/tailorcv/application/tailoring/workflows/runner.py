"""Production adapter for the deterministic screening and tailoring pipelines."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from tailorcv.application import prompts
from tailorcv.application.config import Settings
from tailorcv.application.failures import ValidationError
from tailorcv.application.tailoring import approval as approval_store
from tailorcv.application.tailoring import batch, pipeline
from tailorcv.application.tailoring.stages.jobspec import StoredPostingName
from tailorcv.domain import TailoringOutcome
from tailorcv.infrastructure.build import current_build
from tailorcv.infrastructure.web_fetch.company_research import ResearchSource
from tailorcv.shared.costs import Ledger, ServiceTier

from .models import BatchOutput, EvaluationOutput, ScreenOutput, TailorOutput


class PipelineWorkflowRunner:
    """Delegate durable work to the existing deterministic pipelines."""

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
        """Run screening while retaining the structured findings for commitment."""
        result = pipeline.screen(
            str(source),
            settings,
            master_cv=master_cv,
            cache_enabled=settings.cache,
            service_tier=service_tier,
            ledger=ledger,
            stored_name=stored_name,
            listing_location=listing_location,
            fresh=fresh,
        )
        recommendation = batch.recommend(
            result.spec,
            result.evidence,
            boundaries=settings.job_search.focus.stated_boundaries,
            seniority=settings.job_search.seniority,
        )
        return EvaluationOutput(
            spec=result.spec,
            evidence=result.evidence,
            inventory=result.inventory,
            ledger=result.ledger,
            jd=result.jd,
            reading=result.reading,
            outcome=recommendation.verdict.value,
            reasons=recommendation.reasons,
            prompt_fingerprint=prompts.suite_fingerprint(),
            code_revision=current_build().revision or None,
        )

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
        stored_cv: pipeline.StoredMasterCv | None = None,
    ) -> TailorOutput:
        """Delegate drafting, verification, review, and reports to :func:`pipeline.run`."""
        result = pipeline.run(
            pipeline.RunCommand(
                source=str(source),
                settings=settings,
                master_cv=master_cv,
                want_cover_letter=cover_letter,
                research=research,
                cache_enabled=settings.cache,
                force=force,
                approval=(
                    approval_store.from_payload(approval, source="workflow approval")
                    if approval is not None
                    else None
                ),
                posting_hint=posting_hint,
                active_disputes=active_disputes,
                publisher_url=publisher_url,
                materialize=False,
                service_tier=service_tier,
                ledger=ledger,
                raise_if_cancelled=raise_if_cancelled,
                stored_name=stored_name,
                stored_cv=stored_cv,
            )
        )
        return TailorOutput(
            artifacts=result.serialized,
            outcome=(
                TailoringOutcome.INCOMPLETE
                if result.serialized.manifest.get("run", {}).get("outcome") == "incomplete"
                else TailoringOutcome.PASSED
                if result.verification.passed
                else TailoringOutcome.CHECKS_FAILED
            ),
            ledger=result.ledger,
        )

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
        """Delegate to :func:`batch.screen_batch`, preserving per-posting isolation."""
        names = stored_names if stored_names is not None else (None,) * len(sources)
        requests = tuple(
            batch.Request(str(source), priority=priority, note=note, stored_name=name)
            for source, priority, note, name in zip(sources, priorities, notes, names, strict=True)
        )
        result, inventory = batch.screen_batch(
            requests,
            settings,
            master_cv=master_cv,
            out=output_dir,
            cache_enabled=settings.cache,
            service_tier=service_tier,
            ledgers=ledgers,
            raise_if_cancelled=raise_if_cancelled,
        )
        report_path = batch.write(output_dir / "batch", result, inventory)
        normalized: list[ScreenOutput] = []
        for item in result.screenings:
            if item.report is None:
                raise ValidationError("The batch pipeline did not write a screening report.")
            normalized.append(
                ScreenOutput(
                    report=item.report.read_bytes(),
                    outcome=result.recommendations[item.key].verdict.value,
                    ledger=item.ledger,
                )
            )
        return BatchOutput(
            report=report_path.read_bytes(),
            record=report_path.with_name(batch.RECORD).read_bytes(),
            screenings=tuple(normalized),
        )
