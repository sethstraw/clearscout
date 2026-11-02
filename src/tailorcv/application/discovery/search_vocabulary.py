"""Suggest CV-grounded search vocabulary from the master CV record.

Durable tasks propose titles, employer archetypes, and clusters without hosted
tools or model-written URLs. Approved gap campaigns and relevance review run
through their own task types and handlers.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model
from sqlalchemy.orm import Session

from tailorcv.application import prompts
from tailorcv.application.config import Settings, Stage
from tailorcv.application.discovery.derived_vocabulary_rules import (
    ProposedArchetype,
    ProposedCluster,
    ProposedTitle,
    SearchArchetype,
    SearchCluster,
    SearchTitle,
    searchable_archetypes,
    searchable_clusters,
    searchable_titles,
)
from tailorcv.application.discovery.funnel_audit import FunnelAuditBuilder
from tailorcv.application.discovery.proposals import (
    CarriedRejections,
    accepted_archetypes,
    carried_rejections,
    widened_focus,
)
from tailorcv.application.discovery.search_parameter_summary import (
    accepted_proposals_block,
    carried_rejections_block,
    search_summary,
)
from tailorcv.application.discovery.search_planning_queries import SearchPlanningQueries
from tailorcv.application.discovery.search_proposal_inbox import (
    SearchProposalInbox,
    remaining_capacity,
)
from tailorcv.application.failures import ValidationError
from tailorcv.application.master_cv.cv import CvInventory
from tailorcv.application.prompts import data_block
from tailorcv.application.tailoring.stages.context import citable_entries, record_block
from tailorcv.application.tasks import SearchVocabularyPayload, TaskContext
from tailorcv.application.workspaces.prompt_library import overrides_in
from tailorcv.application.workspaces.spend import record_calls
from tailorcv.domain.search_parameters import (
    MAX_ARCHETYPES,
    MAX_CLUSTER_TERMS,
    MAX_CLUSTERS,
    MAX_TITLES,
    ClusterKind,
    EmployerArchetype,
    JobSearchParameters,
    Lane,
)
from tailorcv.infrastructure.artifacts.runtime import RuntimeStorage
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    AuditEvent,
    DerivedCluster,
    DerivedEmployerArchetype,
    DerivedSearchTitle,
)
from tailorcv.infrastructure.providers.base import Provider, provider_for
from tailorcv.infrastructure.providers.cache import ResponseCache
from tailorcv.infrastructure.providers.client import Client
from tailorcv.infrastructure.providers.credentials import resolve
from tailorcv.infrastructure.providers.plan import RequestPlan, Segment
from tailorcv.shared.costs import Ledger
from tailorcv.shared.costs.prices import ledger_for

__all__ = [
    "ProposedSearch",
    "SearchVocabularyHandler",
    "SearchVocabularyService",
    "response_schema",
    "system_prompt",
]


class ProposedSearch(BaseModel):
    """One model answer, read back through a schema this module owns."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    titles: list[ProposedTitle] = Field(default_factory=list)
    archetypes: list[ProposedArchetype] = Field(default_factory=list)
    capabilities: list[ProposedCluster] = Field(default_factory=list)
    domains: list[ProposedCluster] = Field(default_factory=list)


@dataclass(frozen=True)
class _Proposed:
    """What one vocabulary call produced, and what it cost."""

    titles: tuple[ProposedTitle, ...]
    archetypes: tuple[ProposedArchetype, ...]
    capabilities: tuple[ProposedCluster, ...]
    domains: tuple[ProposedCluster, ...]
    ledger: Ledger
    provider: str


@dataclass(frozen=True)
class _PreparedCompletion:
    """Screened proposals ready for one short atomic business commit."""

    titles: tuple[SearchTitle, ...]
    archetypes: tuple[SearchArchetype, ...]
    clusters: tuple[SearchCluster, ...]
    correlation_id: str | None


def response_schema(entry_ids: tuple[str, ...]) -> type[BaseModel]:
    """Build a vocabulary response schema closed over the identifiers this CV produced.

    A proposal may cite only entries that exist, so a citation to something
    absent cannot be expressed.
    """
    if not entry_ids:
        raise ValidationError("Search vocabulary requires citable CV entries.")
    evidence: Any = list[Literal[entry_ids]]  # type: ignore[valid-type]
    title_model = create_model(
        "ConstrainedSearchTitle",
        __config__=ConfigDict(extra="forbid"),
        title=(str, ...),
        reason=(str, ...),
        evidence_ids=(evidence, Field(min_length=1)),
        # Both vendors emulate an optional field with a null union.
        lane=(Lane | None, Field(default=None)),
    )
    archetype_model = create_model(
        "ConstrainedEmployerArchetype",
        __config__=ConfigDict(extra="forbid"),
        name=(str, ...),
        because=(str, ...),
        evidence_ids=(evidence, Field(min_length=1)),
    )
    cluster_model = create_model(
        "ConstrainedCluster",
        __config__=ConfigDict(extra="forbid"),
        name=(str, ...),
        terms=(list[str], Field(default_factory=list, max_length=MAX_CLUSTER_TERMS)),
        because=(str, ...),
        evidence_ids=(evidence, Field(min_length=1)),
    )
    return create_model(
        "ConstrainedSearchVocabulary",
        __config__=ConfigDict(extra="forbid"),
        titles=(
            list[title_model],  # type: ignore[valid-type]
            Field(default_factory=list, max_length=MAX_TITLES),
        ),
        archetypes=(
            list[archetype_model],  # type: ignore[valid-type]
            Field(default_factory=list, max_length=MAX_ARCHETYPES),
        ),
        capabilities=(
            list[cluster_model],  # type: ignore[valid-type]
            Field(default_factory=list, max_length=MAX_CLUSTERS),
        ),
        domains=(
            list[cluster_model],  # type: ignore[valid-type]
            Field(default_factory=list, max_length=MAX_CLUSTERS),
        ),
    )


def system_prompt(
    *,
    remaining_titles: int,
    remaining_archetypes: int,
    remaining_capabilities: int,
    remaining_domains: int,
) -> str:
    """Return the proposal instructions bounded by what the inbox can still hold.

    Every number is applied in Python whatever the model does. Per-axis caps
    render as remaining capacity rather than the constant ceiling, so a run
    against a nearly-full inbox is told the truth about what it may propose.
    """
    return prompts.render(
        "search_vocabulary_system",
        title_limit=str(remaining_titles),
        archetype_limit=str(remaining_archetypes),
        capability_limit=str(remaining_capabilities),
        domain_limit=str(remaining_domains),
        term_limit=str(MAX_CLUSTER_TERMS),
    )


def _completed_result(
    proposed: _Proposed,
    titles: Sequence[SearchTitle],
    archetypes: Sequence[SearchArchetype],
    clusters: Sequence[SearchCluster],
) -> dict[str, Any]:
    """Project one completed vocabulary run.

    A vocabulary run proposes no postings, leads, or board expansions: those
    fields are constant because nothing in this task type can produce them.
    """
    return {
        "posting_links_proposed": 0,
        "posting_links_discarded": 0,
        "leads_proposed": 0,
        "leads_fetched": 0,
        "leads_retained_for_scans": 0,
        "postings_recorded": 0,
        "leads_discarded": 0,
        "model_calls_made": len(proposed.ledger.entries),
        "titles": [item.describe() for item in titles],
        "archetypes": [item.describe() for item in archetypes],
        "clusters": [item.describe() for item in clusters],
        "postings": [],
        "leads": [],
        "expansions": [],
        "discoveries": [],
        "funnel": FunnelAuditBuilder().build().model_dump(mode="json"),
    }


class SearchVocabularyService:
    """Propose CV-grounded search vocabulary from the current master CV record."""

    def __init__(
        self,
        database: Database,
        *,
        storage: RuntimeStorage,
        provider: Provider | None = None,
    ) -> None:
        """Bind the search to its durable state and its model."""
        self.database = database
        self.queries = SearchPlanningQueries(database, storage)
        self.inbox = SearchProposalInbox()
        self.provider = provider

    def search(
        self,
        workspace_id: str,
        payload: SearchVocabularyPayload,
        context: TaskContext | None = None,
    ) -> dict[str, Any]:
        """Propose vocabulary under this workspace's effective prompt overrides."""
        with self.database.session() as session:
            prompt_text = {
                key: text for key, (_, text) in overrides_in(session, workspace_id).items()
            }
        with prompts.overriding(prompt_text):
            return self._search_with_effective_prompts(workspace_id, payload, context)

    def _search_with_effective_prompts(
        self,
        workspace_id: str,
        payload: SearchVocabularyPayload,  # noqa: ARG002 - payload carries no fields
        context: TaskContext | None,
    ) -> dict[str, Any]:
        """Execute one vocabulary proposal under effective prompts."""
        inputs = self.queries.planning_inputs(workspace_id)
        settings = inputs.settings
        inventory = inputs.inventory
        master_cv_version_id = inputs.master_cv_version_id
        # An accepted cluster is the person's opinion now, so it states itself to
        # this run the same way it orders the queue.
        parameters = settings.job_search.model_copy(
            update={"focus": widened_focus(self.database, workspace_id, settings.job_search.focus)}
        )
        if context is not None:
            context.raise_if_cancelled()
            context.progress(
                "proposing",
                {
                    "phase": "proposing_vocabulary",
                    "completed": 0,
                    "item_kind": "search_plan",
                    "item_label": "Building the search plan",
                },
            )
        allowed = accepted_archetypes(self.database, workspace_id)
        proposed = self._propose(
            workspace_id,
            settings,
            parameters,
            inventory,
            allowed,
            master_cv_version_id,
            context,
        )
        self._file(
            workspace_id, proposed.ledger, proposed.provider, getattr(context, "task_id", None)
        )
        prepared = self._prepare_completion(
            context,
            parameters,
            inventory,
            proposed,
        )
        with self.database.session() as session:
            result = self._complete(
                session,
                workspace_id,
                master_cv_version_id,
                proposed,
                prepared,
            )
        result["search_parameter_set_id"] = inputs.search_parameter_set_id
        result["search_parameter_sha256"] = inputs.search_parameter_sha256
        result["capability_note"] = (
            "CV-grounded vocabulary only. No postings or new employer boards were "
            "discovered. Review suggestions, then scan registered sources."
        )
        return result

    def _prepare_completion(
        self,
        context: TaskContext | None,
        parameters: JobSearchParameters,
        inventory: CvInventory,
        proposed: _Proposed,
    ) -> _PreparedCompletion:
        """Screen proposals before any business write transaction."""
        known_ids = {entry.id for entry in citable_entries(inventory)}
        titles = searchable_titles(list(proposed.titles), parameters, known_ids)
        archetypes = searchable_archetypes(list(proposed.archetypes), parameters, known_ids)
        clusters = [
            *searchable_clusters(
                list(proposed.capabilities), parameters, ClusterKind.CAPABILITY, known_ids
            ),
            *searchable_clusters(list(proposed.domains), parameters, ClusterKind.DOMAIN, known_ids),
        ]
        return _PreparedCompletion(
            titles=tuple(titles),
            archetypes=tuple(archetypes),
            clusters=tuple(clusters),
            correlation_id=getattr(context, "task_id", None),
        )

    def _complete(
        self,
        session: Session,
        workspace_id: str,
        master_cv_version_id: str,
        proposed: _Proposed,
        prepared: _PreparedCompletion,
    ) -> dict[str, Any]:
        """Commit prepared business state without progress calls or external I/O."""
        titles = list(prepared.titles)
        title_report = self.inbox.remember_titles_in_session(
            session,
            workspace_id,
            master_cv_version_id,
            titles,
            correlation_id=prepared.correlation_id,
        )
        archetypes = list(prepared.archetypes)
        archetype_report = self.inbox.remember_archetypes_in_session(
            session,
            workspace_id,
            master_cv_version_id,
            archetypes,
            correlation_id=prepared.correlation_id,
        )
        clusters = list(prepared.clusters)
        cluster_reports = self.inbox.remember_clusters_in_session(
            session,
            workspace_id,
            master_cv_version_id,
            clusters,
            correlation_id=prepared.correlation_id,
        )
        result = _completed_result(proposed, titles, archetypes, clusters)
        self._record_completed(
            session,
            workspace_id,
            task_id=prepared.correlation_id,
            reports={
                "titles": title_report,
                "archetypes": archetype_report,
                **{f"clusters.{kind}": report for kind, report in cluster_reports.items()},
            },
        )
        return result

    def _propose(
        self,
        workspace_id: str,
        settings: Settings,
        parameters: JobSearchParameters,
        inventory: CvInventory,
        allowed: Sequence[EmployerArchetype],
        master_cv_version_id: str,
        context: TaskContext | None,
    ) -> _Proposed:
        """Ask once for CV-grounded vocabulary; this task proposes no postings or leads."""
        ledger = ledger_for(settings.prices)
        provider_name = (
            self.provider.name.value if self.provider is not None else settings.provider.value
        )
        entry_ids = tuple(entry.id for entry in citable_entries(inventory))
        carried = carried_rejections(
            self.database,
            workspace_id,
            cap=settings.discovery.max_carried_rejections,
        )
        known = self.queries.known_identities(workspace_id)
        with self.database.session() as session:
            accepted_block = accepted_proposals_block(session, workspace_id, master_cv_version_id)
            remaining_titles = remaining_capacity(
                session, DerivedSearchTitle, workspace_id, master_cv_version_id, MAX_TITLES
            )
            remaining_archetypes = remaining_capacity(
                session,
                DerivedEmployerArchetype,
                workspace_id,
                master_cv_version_id,
                MAX_ARCHETYPES,
            )
            # Clusters are capped per kind, so each kind's remaining capacity is counted
            # and the prompt is told both.
            remaining_capabilities = remaining_capacity(
                session,
                DerivedCluster,
                workspace_id,
                master_cv_version_id,
                MAX_CLUSTERS,
                extra_filter={"kind": ClusterKind.CAPABILITY.value},
            )
            remaining_domains = remaining_capacity(
                session,
                DerivedCluster,
                workspace_id,
                master_cv_version_id,
                MAX_CLUSTERS,
                extra_filter={"kind": ClusterKind.DOMAIN.value},
            )
        schema = response_schema(entry_ids)
        service_tier = settings.unattended_tier()
        provider = self.provider or provider_for(
            settings.provider,
            resolve(settings.provider).key,
            settings.timeout_for(service_tier),
        )
        with ResponseCache(enabled=False) as cache:
            client = Client(
                provider=provider,
                settings=settings,
                cache=cache,
                ledger=ledger,
                service_tier=service_tier,
            )
            try:
                base = self.ask(
                    client,
                    parameters,
                    allowed,
                    inventory,
                    schema,
                    carried,
                    accepted_block,
                    remaining_titles,
                    remaining_archetypes,
                    remaining_capabilities,
                    remaining_domains,
                    known.source_roots,
                )
            except Exception:
                # A completed call has already billed the account.
                self._file(
                    workspace_id, ledger, provider.name.value, getattr(context, "task_id", None)
                )
                raise
        return _Proposed(
            titles=tuple(base.titles),
            archetypes=tuple(base.archetypes),
            capabilities=tuple(base.capabilities),
            domains=tuple(base.domains),
            ledger=ledger,
            provider=provider_name,
        )

    @staticmethod
    def ask(
        client: Client,
        parameters: JobSearchParameters,
        allowed: Sequence[EmployerArchetype],
        inventory: CvInventory,
        schema: type[BaseModel],
        carried: CarriedRejections,
        accepted_block: str,
        remaining_titles: int,
        remaining_archetypes: int,
        remaining_capabilities: int,
        remaining_domains: int,
        registered_source_roots: frozenset[str],
    ) -> ProposedSearch:
        """Make one vocabulary call about the CV record."""
        stated_preferences = search_summary(parameters, allowed)
        stated = stated_preferences
        rejection_block = carried_rejections_block(carried)
        evidence_body = record_block(inventory)
        if parameters.configured or allowed:
            evidence_body = (
                f"{data_block('declared_search_preferences', stated_preferences)}\n\n"
                f"{evidence_body}"
            )
        if accepted_block:
            evidence_body = f"{accepted_block}\n\n{evidence_body}"
        if rejection_block:
            evidence_body = f"{evidence_body}\n\n{rejection_block}"
        if registered_source_roots:
            registered = "\n".join(f"- {root}" for root in sorted(registered_source_roots))
            evidence_body = (
                f"{evidence_body}\n\n{data_block('registered_board_sources', registered)}"
            )
        policy = client.settings.policy_for(Stage.SEARCH_VOCABULARY)
        plan = RequestPlan(
            stage=Stage.SEARCH_VOCABULARY,
            instructions=Segment(
                system_prompt(
                    remaining_titles=remaining_titles,
                    remaining_archetypes=remaining_archetypes,
                    remaining_capabilities=remaining_capabilities,
                    remaining_domains=remaining_domains,
                ),
                "search_vocabulary_system",
            ),
            task=Segment(
                prompts.render("search_vocabulary_user", search_parameters=stated),
                "search_vocabulary_user",
            ),
            evidence=Segment(evidence_body),
            search=None,
            max_tokens=policy.max_tokens,
        )
        answer = client.complete(plan, schema)
        return ProposedSearch.model_validate(answer.model_dump(mode="json"))

    def _file(self, workspace_id: str, ledger: Ledger, provider: str, task_id: str | None) -> None:
        """Write one ledger's calls against the workspace that paid for them."""
        with self.database.session() as session:
            record_calls(
                session,
                workspace_id=workspace_id,
                task_id=task_id,
                ledger=ledger,
                provider=provider,
                origin="search.vocabulary",
            )

    def _record_completed(
        self,
        session: Session,
        workspace_id: str,
        *,
        task_id: str | None,
        reports: dict[str, Any],
    ) -> None:
        """Append the proposal outcomes of one vocabulary run to workspace history."""
        session.add(
            AuditEvent(
                workspace_id=workspace_id,
                action="search_vocabulary.completed",
                entity_type="background_task",
                entity_id=task_id,
                details={
                    "proposal_axes": {
                        axis: {
                            "proposed": report.proposed,
                            "retained": report.retained,
                            "dropped": list(report.dropped),
                        }
                        for axis, report in reports.items()
                    }
                },
            )
        )


class SearchVocabularyHandler:
    """Worker adapter for one search-vocabulary proposal run."""

    def __init__(
        self,
        database: Database,
        *,
        storage: RuntimeStorage,
        provider: Provider | None = None,
    ) -> None:
        """Configure the search service with its model seam."""
        self.service = SearchVocabularyService(database, storage=storage, provider=provider)

    def __call__(self, context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Run one search-vocabulary proposal in this task's workspace."""
        value = SearchVocabularyPayload.model_validate(payload)
        task = context.queue.get(context.task_id)
        return self.service.search(str(task.workspace_id), value, context)
