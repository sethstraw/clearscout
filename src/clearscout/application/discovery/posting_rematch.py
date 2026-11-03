"""Re-run matching for disputed fit requirements against the current master CV."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from clearscout.application import prompts
from clearscout.application.discovery.paid_task import billed_run
from clearscout.application.discovery.posting_queries import latest_observation_id
from clearscout.application.failures import ConflictError, ValidationError
from clearscout.application.job_search.postings import RequirementRulingService
from clearscout.application.job_search.postings.rulings import reverse_ruling
from clearscout.application.master_cv.cv import parse_cv
from clearscout.application.master_cv.versioning import current_version_id
from clearscout.application.tailoring.stages import match as match_stage
from clearscout.application.tailoring.stages.jobspec import (
    Dimension,
    Importance,
    JobSpec,
    Requirement,
    requirement_vocabulary,
)
from clearscout.application.tailoring.workflows import WorkflowService
from clearscout.application.tasks import TaskContext
from clearscout.application.workspaces import WorkspaceSettingsService
from clearscout.domain.rulings import Ruling
from clearscout.infrastructure.artifacts.runtime import RuntimeStorage
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    AuditEvent,
    JobEvaluation,
    MasterCvEntry,
    MatchEvidence,
    PostingObservation,
    PostingRequirement,
)
from clearscout.infrastructure.providers.base import Provider
from clearscout.infrastructure.providers.client import Client
from clearscout.infrastructure.web_fetch.company_research import CompanyResearchFetcher
from clearscout.infrastructure.web_fetch.policy import PoliteHttpClient


@dataclass(frozen=True)
class _RematchState:
    evaluation_id: str
    requirements: dict[str, PostingRequirement]
    entry_keys: dict[str, str]
    previous_keys: dict[str, frozenset[str] | None]
    dimensions: dict[str, Dimension]


class PostingRematchService:
    """Re-match fit disputes without writing a new evaluation."""

    def __init__(
        self,
        database: Database,
        *,
        storage: RuntimeStorage,
        http_client: PoliteHttpClient | None = None,
    ) -> None:
        """Configure stored workflow inputs and fit-dispute reads."""
        self.database = database
        self.workflows = WorkflowService(
            database, storage, research_fetcher=CompanyResearchFetcher(client=http_client)
        )
        self.rulings = RequirementRulingService(database)

    def rematch(
        self,
        workspace_id: str,
        job_posting_id: str,
        client_factory: Callable[[], Client],
        context: TaskContext | None = None,
    ) -> dict[str, Any]:
        """Reverse fit disputes only when matching cites a previously uncited CV entry."""
        inputs = self.workflows.inputs(workspace_id, job_posting_id)
        disputes = self.rulings.active_fit_disputes(workspace_id, job_posting_id=job_posting_id)
        if not disputes:
            return _result(job_posting_id, inputs.observation_id, inputs.master_version.id, ())

        state = self._load_state(
            job_posting_id,
            inputs.observation_id,
            inputs.master_version.id,
            tuple(dispute.requirement_key for dispute in disputes),
        )
        if not state.requirements:
            return _result(job_posting_id, inputs.observation_id, inputs.master_version.id, ())

        inventory = parse_cv(Path("master-cv.md"), text=inputs.master_content.decode("utf-8"))
        client = client_factory()
        key_by_identifier, spec = _match_spec(inputs.job.title, inputs.posting_url, state)
        prompt_text = {name: content for name, (_, content) in inputs.prompt_overrides.items()}
        calls_before = len(client.ledger.entries)
        with prompts.overriding(prompt_text):
            evidence = match_stage.match(client, inventory, spec, inputs.settings.rules)
        model_calls = len(client.ledger.entries) - calls_before
        new_keys = _current_evidence_keys(key_by_identifier, state.entry_keys, evidence)

        reversed_ids = self._apply(
            workspace_id=workspace_id,
            job_posting_id=job_posting_id,
            observation_id=inputs.observation_id,
            master_cv_version_id=inputs.master_version.id,
            evaluation_id=state.evaluation_id,
            previous_keys=state.previous_keys,
            new_keys=new_keys,
            correlation_id=getattr(context, "task_id", None),
        )
        return _result(
            job_posting_id,
            inputs.observation_id,
            inputs.master_version.id,
            reversed_ids,
            model_calls=model_calls,
        )

    def _load_state(
        self,
        job_posting_id: str,
        observation_id: str,
        master_cv_version_id: str,
        requirement_keys: tuple[str, ...],
    ) -> _RematchState:
        """Load the evaluated requirements and durable evidence identities."""
        with self.database.session() as session:
            evaluation = session.scalar(
                select(JobEvaluation)
                .where(JobEvaluation.job_posting_id == job_posting_id)
                .order_by(JobEvaluation.version_number.desc())
                .limit(1)
            )
            if evaluation is None or evaluation.observation_id is None:
                raise ValidationError("Fit re-match requires a current evaluated posting.")
            current_observation = session.get(PostingObservation, observation_id)
            evaluated_observation = session.get(PostingObservation, evaluation.observation_id)
            if (
                current_observation is None
                or evaluated_observation is None
                or current_observation.content_sha256 != evaluated_observation.content_sha256
            ):
                raise ConflictError("The posting changed after its current evaluation.")
            rows = session.scalars(
                select(PostingRequirement)
                .where(
                    PostingRequirement.job_posting_id == job_posting_id,
                    PostingRequirement.job_evaluation_id == evaluation.id,
                    PostingRequirement.requirement_key.in_(requirement_keys),
                )
                .order_by(PostingRequirement.position, PostingRequirement.id)
            ).all()
            latest_by_key = {row.requirement_key: row for row in rows}
            entry_keys = {
                str(entry_id): str(entry_key)
                for entry_id, entry_key in session.execute(
                    select(MasterCvEntry.entry_id, MasterCvEntry.entry_key).where(
                        MasterCvEntry.master_cv_version_id == master_cv_version_id
                    )
                ).all()
            }
            previous_keys = _previous_evidence_keys(session, evaluation.id, latest_by_key)
            dimensions = _dimensions(evaluation.fit_report_payload)
        return _RematchState(
            evaluation_id=evaluation.id,
            requirements=latest_by_key,
            entry_keys=entry_keys,
            previous_keys=previous_keys,
            dimensions=dimensions,
        )

    def _apply(
        self,
        *,
        workspace_id: str,
        job_posting_id: str,
        observation_id: str,
        master_cv_version_id: str,
        evaluation_id: str,
        previous_keys: dict[str, frozenset[str] | None],
        new_keys: dict[str, frozenset[str] | None],
        correlation_id: str | None,
    ) -> tuple[str, ...]:
        """Persist reversals after proving the inputs are still current."""
        reversed_ids: list[str] = []
        with self.database.session() as session:
            if latest_observation_id(session, job_posting_id) != observation_id:
                raise ConflictError("A newer posting observation arrived during this re-match.")
            latest_evaluation_id = session.scalar(
                select(JobEvaluation.id)
                .where(JobEvaluation.job_posting_id == job_posting_id)
                .order_by(JobEvaluation.version_number.desc())
                .limit(1)
            )
            if latest_evaluation_id != evaluation_id:
                raise ConflictError("A newer evaluation arrived during this re-match.")
            latest_master_cv_version_id = current_version_id(session, workspace_id)
            if latest_master_cv_version_id != master_cv_version_id:
                raise ConflictError("A newer master CV arrived during this re-match.")
            for requirement_key, cited_keys in new_keys.items():
                prior_keys = previous_keys.get(requirement_key)
                if cited_keys is None or prior_keys is None:
                    session.add(
                        AuditEvent(
                            workspace_id=workspace_id,
                            action="ruling.rematch_skipped_missing_entry_keys",
                            entity_type="requirement_ruling",
                            entity_id=requirement_key,
                            details={
                                "requirement_key": requirement_key,
                                "job_posting_id": job_posting_id,
                                "observation_id": observation_id,
                                "prior_keys_resolved": prior_keys is not None,
                                "new_keys_resolved": cited_keys is None,
                                "master_cv_version_id": master_cv_version_id,
                            },
                            created_by="worker",
                            origin="posting.refresh_with_rematch",
                            correlation_id=correlation_id,
                        )
                    )
                    continue
                if not cited_keys.difference(prior_keys):
                    continue
                ruling_id = reverse_ruling(
                    session,
                    workspace_id=workspace_id,
                    requirement_key=requirement_key,
                    disputed=Ruling.DISPUTED_FIT,
                    action="ruling.reversed_by_rematch",
                    origin="posting.refresh_with_rematch",
                    details={
                        "job_posting_id": job_posting_id,
                        "observation_id": observation_id,
                        "previous_evidence_keys": sorted(prior_keys),
                        "new_evidence_keys": sorted(cited_keys),
                        "master_cv_version_id": master_cv_version_id,
                    },
                    correlation_id=correlation_id,
                )
                if ruling_id is not None:
                    reversed_ids.append(ruling_id)
            session.flush()
        return tuple(reversed_ids)


def _match_spec(
    title: str,
    posting_url: str,
    state: _RematchState,
) -> tuple[dict[str, str], JobSpec]:
    identifiers = requirement_vocabulary(len(state.requirements))[: len(state.requirements)]
    key_by_identifier: dict[str, str] = {}
    requirements: list[Requirement] = []
    for identifier, (key, row) in zip(identifiers, state.requirements.items(), strict=True):
        key_by_identifier[identifier] = key
        requirements.append(
            Requirement(
                id=identifier,
                text=row.text,
                importance=Importance(row.importance),
                dimension=state.dimensions.get(key, Dimension.HARD),
            )
        )
    return key_by_identifier, JobSpec(
        title=title,
        company=None,
        seniority=None,
        location=None,
        requirements=tuple(requirements),
        keywords=(),
        hard_gates=(),
        origin=posting_url,
    )


def _previous_evidence_keys(
    session: Session,
    evaluation_id: str,
    requirements: dict[str, PostingRequirement],
) -> dict[str, frozenset[str] | None]:
    previous: dict[str, frozenset[str] | None] = {}
    for key in requirements:
        prior = session.execute(
            select(MatchEvidence.master_cv_entry_id, MatchEvidence.master_cv_entry_key).where(
                MatchEvidence.evaluation_id == evaluation_id,
                MatchEvidence.requirement_key == key,
                MatchEvidence.source_kind == "candidate",
            )
        ).all()
        previous[key] = (
            None
            if any(entry_id is not None and entry_key is None for entry_id, entry_key in prior)
            else frozenset(str(entry_key) for _, entry_key in prior if entry_key is not None)
        )
    return previous


def _dimensions(payload: dict[str, Any] | None) -> dict[str, Dimension]:
    return {
        str(item.get("requirement_key")): Dimension(str(item.get("dimension", "hard")))
        for item in (payload or {}).get("requirements", [])
        if isinstance(item, dict) and item.get("requirement_key")
    }


def _current_evidence_keys(
    key_by_identifier: dict[str, str],
    entry_keys: dict[str, str],
    evidence: match_stage.EvidenceMap,
) -> dict[str, frozenset[str] | None]:
    current: dict[str, frozenset[str] | None] = {}
    for identifier, key in key_by_identifier.items():
        matched = evidence.for_requirement(identifier)
        cited = matched.entry_ids if matched is not None and matched.may_support_a_claim else ()
        current[key] = (
            None
            if any(str(item) not in entry_keys for item in cited)
            else frozenset(entry_keys[str(item)] for item in cited)
        )
    return current


class PostingRefreshWithRematchHandler:
    """Run the paid match stage for one posting's active fit disputes."""

    def __init__(
        self,
        database: Database,
        *,
        storage: RuntimeStorage,
        provider: Provider | None = None,
        http_client: PoliteHttpClient | None = None,
    ) -> None:
        """Configure the paid fit re-match task handler."""
        self.database = database
        self.service = PostingRematchService(database, storage=storage, http_client=http_client)
        self.settings = WorkspaceSettingsService(database)
        self.rulings = RequirementRulingService(database)
        self.provider = provider

    def __call__(self, context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Run one opted-in fit re-match or finish without resolving credentials."""
        value = payload.get("job_posting_id")
        if not isinstance(value, str) or not value:
            raise ValidationError("posting.refresh_with_rematch requires job_posting_id.")
        task = context.queue.get(context.task_id)
        workspace_id = str(task.workspace_id)
        settings = self.settings.current(workspace_id)
        if not settings.learning.enable_dispute_rematch or not self.rulings.active_fit_disputes(
            workspace_id, job_posting_id=value
        ):
            return _result(value, None, None, ())
        with billed_run(
            self.database,
            workspace_id=workspace_id,
            task_id=context.task_id,
            settings=settings,
            origin="posting.refresh_with_rematch",
            provider=self.provider,
        ) as paid:
            return self.service.rematch(workspace_id, value, paid.build_client, context)


def _result(
    job_posting_id: str,
    observation_id: str | None,
    master_cv_version_id: str | None,
    reversed_ruling_ids: tuple[str, ...],
    *,
    model_calls: int = 0,
) -> dict[str, Any]:
    return {
        "job_posting_id": job_posting_id,
        "observation_id": observation_id,
        "master_cv_version_id": master_cv_version_id,
        "reversed_ruling_ids": list(reversed_ruling_ids),
        "model_calls_made": model_calls,
    }
