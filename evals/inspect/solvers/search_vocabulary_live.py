"""Live solver: run the shipped search-vocabulary stage against the real provider.

Only ``scripts/eval.sh --live`` should reach this solver. Each sample names a CV
under ``evals/cvs/`` and the search parameters a person stated, and one paid call
proposes titles, archetypes, and clusters the way a fresh workspace's run would.
Each declared prompt arm holds one file per name in ``SEARCH_VOCABULARY_PROMPTS``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clearscout.application.config import ProviderName, Settings, Stage
from clearscout.application.discovery.proposals import CarriedRejections
from clearscout.application.discovery.search_vocabulary import (
    ProposedSearch,
    SearchVocabularyService,
    response_schema,
)
from clearscout.application.master_cv.cv import CvInventory, parse_cv
from clearscout.application.tailoring.stages.context import citable_entries
from clearscout.domain.search_parameters import (
    MAX_ARCHETYPES,
    MAX_CLUSTERS,
    MAX_TITLES,
    JobSearchParameters,
)
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.providers.client import Client
from clearscout.shared.costs import Ledger
from clearscout.shared.costs.prices import table as price_table
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

SEARCH_VOCABULARY_PROMPTS = ("search_vocabulary_system", "search_vocabulary_user")
BILLING_ORIGIN = "eval.search_vocabulary_live"
CV_DIR = Path(__file__).resolve().parents[2] / "cvs"


def case_inventory(sample: dict[str, Any]) -> CvInventory:
    """Return the parsed CV a sample names, refusing a path outside ``evals/cvs/``."""
    path = (CV_DIR / str(sample["input"]["cv"])).resolve()
    if path.parent != CV_DIR:
        raise ValueError(f"sample {sample.get('id')!r} names a CV outside {CV_DIR}")
    return parse_cv(path)


def case_parameters(sample: dict[str, Any]) -> JobSearchParameters:
    """Return the search parameters a sample states."""
    return JobSearchParameters.model_validate(sample["input"].get("parameters") or {})


def propose(client: Client, sample: dict[str, Any]) -> ProposedSearch:
    """Make the one vocabulary call a fresh workspace with this CV and these parameters makes.

    A fresh workspace has no accepted archetypes, carried rejections, accepted
    proposals, or registered boards, and its inbox holds every axis's full capacity.
    """
    inventory = case_inventory(sample)
    entry_ids = tuple(entry.id for entry in citable_entries(inventory))
    return SearchVocabularyService.ask(
        client,
        case_parameters(sample),
        (),
        inventory,
        response_schema(entry_ids),
        CarriedRejections(titles=(), archetypes=(), clusters=()),
        "",
        MAX_TITLES,
        MAX_ARCHETYPES,
        MAX_CLUSTERS,
        MAX_CLUSTERS,
        frozenset(),
    )


def proposal_result(proposed: ProposedSearch) -> dict[str, Any]:
    """Return the model's raw proposals as the scorer reads them."""
    dumped = proposed.model_dump(mode="json")
    return {
        "completion": f"{len(proposed.titles)} titles, {len(proposed.archetypes)} archetypes",
        **dumped,
    }


@dataclass(frozen=True)
class _SearchVocabularyLiveSolver:
    """One paid vocabulary run: one call per sample, trial and arm."""

    run_metadata: dict[str, Any]
    arms: tuple[PromptArm, ...]
    provider_name: ProviderName
    settings: Settings
    ledger: Ledger
    database: Database
    workspace_id: str

    def __call__(self, sample: dict[str, Any]) -> dict[str, Any]:
        arm = requested_arm(self.arms, sample.get("arm"))
        if prompt_snapshot_fingerprint(SEARCH_VOCABULARY_PROMPTS, arm.current()) != arm.fingerprint:
            raise RuntimeError(
                "Live evaluation refused: search_vocabulary prompt bytes changed after run "
                "preflight."
            )
        paid = paid_call(
            lambda client: propose(client, sample),
            provider_name=self.provider_name,
            settings=self.settings,
            run_ledger=self.ledger,
            run_metadata=self.run_metadata,
            prompt_snapshot=arm.snapshot,
            database=self.database,
            workspace_id=self.workspace_id,
            origin=BILLING_ORIGIN,
        )
        provenance = sample_provenance(
            paid.ledger, self.ledger, self.run_metadata, paid.billing_failure
        )
        if arm.name is not None:
            provenance["arm"] = arm.name
        return {**proposal_result(paid.value), "metadata": provenance}


def search_vocabulary_live_solver(dataset: Path) -> _SearchVocabularyLiveSolver:
    """Return the prepared solver that runs the search-vocabulary stage live."""
    run_metadata = paid_stage_metadata(Stage.SEARCH_VOCABULARY, SEARCH_VOCABULARY_PROMPTS, dataset)
    arms = declared_arms(SEARCH_VOCABULARY_PROMPTS)
    if declared := arms_metadata(arms):
        # Declared arms supply every call's bytes, so the working-tree fingerprint names none.
        run_metadata.pop("prompt_suite_fingerprint", None)
        run_metadata.update(declared)
    provider_name = ProviderName(run_metadata["provider"])
    settings = run_settings(provider_name, Stage.SEARCH_VOCABULARY)
    database, workspace_id = billing_target()
    return _SearchVocabularyLiveSolver(
        run_metadata=run_metadata,
        arms=arms,
        provider_name=provider_name,
        settings=settings,
        ledger=Ledger(prices=price_table(settings.prices)),
        database=database,
        workspace_id=workspace_id,
    )
