"""Live solvers: run the shipped document stages against the real provider.

Only ``scripts/eval.sh --live`` should reach these solvers, because they are the
only document modes that spend money. Each refuses to launch unless the
environment carries the complete approved run identity, and each records the
prompt snapshot and dataset identity the run was bound to. Credentials load
through ``clearscout.infrastructure.providers.credentials.find``; nothing here prompts.

Only the family's own stage is paid. The posting, the evidence map, and the
plan are the deterministic ones ``evals.support.documents`` builds, and the
critic family reviews a document assembled from the sample's canned resume or
letter, so a critic result belongs to the critic rather than to a fresh draft.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from clearscout.application.config import ProviderName, Settings, Stage
from clearscout.application.prompts import load
from clearscout.application.tailoring.stages.draft import cover_letter, draft
from clearscout.application.tailoring.stages.verify import check_letter, critique
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
from evals.support.documents import (
    DocumentContext,
    critic_document,
    critic_result,
    document_context,
    draft_result,
    fitted_resume,
    letter_result,
    research_sources,
)

DRAFT_PROMPTS = ("draft_system", "draft_user")
COVER_PROMPTS = ("cover_letter_system", "cover_letter_user")
CRITIC_PROMPTS = ("critic_system", "critic_user")


@dataclass(frozen=True)
class _DocumentLiveSolver:
    """One family's paid run: fixed inputs, one live call per sample, shipped checks."""

    run_metadata: dict[str, Any]
    stage: Stage
    prompt_names: Sequence[str]
    prompt_snapshot: Mapping[str, str]
    provider_name: ProviderName
    settings: Settings
    ledger: Ledger
    database: Database
    workspace_id: str
    origin: str

    def __call__(self, sample: dict[str, Any]) -> dict[str, Any]:
        current = {name: load(name) for name in self.prompt_names}
        if (
            prompt_snapshot_fingerprint(self.prompt_names, current)
            != self.run_metadata["prompt_suite_fingerprint"]
        ):
            raise RuntimeError(
                f"Live evaluation refused: {self.stage.value} prompt bytes changed after "
                "run preflight."
            )
        context = document_context(sample)
        paid = paid_call(
            lambda client: self._measure(sample, context, client),
            provider_name=self.provider_name,
            settings=self.settings,
            run_ledger=self.ledger,
            run_metadata=self.run_metadata,
            prompt_snapshot=self.prompt_snapshot,
            database=self.database,
            workspace_id=self.workspace_id,
            origin=self.origin,
        )
        return {
            **paid.value,
            "metadata": sample_provenance(
                paid.ledger, self.ledger, self.run_metadata, paid.billing_failure
            ),
        }

    def _measure(
        self, sample: dict[str, Any], context: DocumentContext, client: Any
    ) -> dict[str, Any]:
        """Run this family's paid stage and return what the shipped verifier found."""
        raise NotImplementedError


@dataclass(frozen=True)
class _DraftLiveSolver(_DocumentLiveSolver):
    """Write attempt one live, then fit it and score what prints through ``verify.check``."""

    def _measure(
        self, sample: dict[str, Any], context: DocumentContext, client: Any
    ) -> dict[str, Any]:
        """Draft the resume live and check the first attempt."""
        del sample
        result = draft(client, context.inventory, context.spec, context.plan, context.guardrails)
        return draft_result(*fitted_resume(result, context))


@dataclass(frozen=True)
class _CoverLiveSolver(_DocumentLiveSolver):
    """Write the letter live, then score it through ``verify.check_letter``."""

    def _measure(
        self, sample: dict[str, Any], context: DocumentContext, client: Any
    ) -> dict[str, Any]:
        """Write the cover letter live from the sample's research and check it."""
        research = research_sources(sample)
        letter = cover_letter(
            client,
            context.inventory,
            context.spec,
            context.plan,
            context.guardrails,
            research=research,
            employer=context.spec.company,
        )
        report = check_letter(
            letter=letter,
            plan=context.plan,
            inventory=context.inventory,
            guardrails=context.guardrails,
            research=research,
        )
        # Live receipts are refused inside the repository, so the body stays private.
        return letter_result(letter, report, keep_text=True)


@dataclass(frozen=True)
class _CriticLiveSolver(_DocumentLiveSolver):
    """Critique a fixed document live, and record the verdict and the shipped finding."""

    def _measure(
        self, sample: dict[str, Any], context: DocumentContext, client: Any
    ) -> dict[str, Any]:
        """Assemble the sample's canned resume or letter offline, then critique it live."""
        fixed = critic_document(sample, context)
        verdict, draws = critique(
            client,
            markdown=fixed.markdown,
            citations=fixed.citations,
            inventory=context.inventory,
            offered=fixed.offered,
            spec=context.spec,
            guardrails=context.guardrails,
            document=fixed.document,
            research=fixed.research,
        )
        return critic_result(verdict, draws, fixed.check)


# Each document family, keyed by the stage its task module declares. The shell
# reads that declaration to estimate the same stage the run then records.
FAMILIES: dict[Stage, tuple[type[_DocumentLiveSolver], Sequence[str], str]] = {
    Stage.DRAFT: (_DraftLiveSolver, DRAFT_PROMPTS, "eval.draft_live"),
    Stage.COVER: (_CoverLiveSolver, COVER_PROMPTS, "eval.cover_live"),
    Stage.CRITIC: (_CriticLiveSolver, CRITIC_PROMPTS, "eval.critic_live"),
}


def document_live_solver(stage: Stage, dataset: Path) -> _DocumentLiveSolver:
    """Return the prepared solver that runs one document family's stage live."""
    if stage not in FAMILIES:
        raise RuntimeError(f"Live evaluation refused: no document family runs the {stage} stage.")
    kind, prompt_names, origin = FAMILIES[stage]
    run_metadata = paid_stage_metadata(stage, prompt_names, dataset)
    provider_name = ProviderName(run_metadata["provider"])
    settings = run_settings(provider_name, stage)
    database, workspace_id = billing_target()
    return kind(
        run_metadata=run_metadata,
        stage=stage,
        prompt_names=tuple(prompt_names),
        prompt_snapshot=MappingProxyType({name: load(name) for name in prompt_names}),
        provider_name=provider_name,
        settings=settings,
        ledger=Ledger(prices=price_table(settings.prices)),
        database=database,
        workspace_id=workspace_id,
        origin=origin,
    )
