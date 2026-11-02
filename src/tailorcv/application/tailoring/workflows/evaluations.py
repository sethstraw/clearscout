"""Discovery and rerun bridge into the immutable evaluation transaction."""

from __future__ import annotations

import hashlib
import logging
import tempfile
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from tailorcv.application import job_search, prompts
from tailorcv.application.config import Settings, Stage
from tailorcv.application.contracts import EligibilityInput
from tailorcv.application.discovery.board_scan import SourceScanService
from tailorcv.application.discovery.content_resolution import (
    CUT_OFF_SKIP_REASON,
    CurrentPostingContent,
    CurrentPostingContentReader,
    PostingContentResolutionService,
    resolved_publisher_fetch,
    retained_text_cut_off,
    too_short_reviews,
)
from tailorcv.application.discovery.dispositions import DiscoveryDispositionService
from tailorcv.application.discovery.missed_requirement_reader import refresh_for_evaluation
from tailorcv.application.discovery.pending import family_head_dismissal, marked_ids
from tailorcv.application.discovery.posting_fetch import (
    FetchedPosting,
    PostingFetcher,
)
from tailorcv.application.discovery.posting_queries import same_body_eligibility
from tailorcv.application.discovery.posting_text import posting_facts, publisher_closing_date
from tailorcv.application.discovery.refusal_patterns import (
    POSTING_STATUS_CONDITION,
    RefusedPosting,
    file_refusal,
)
from tailorcv.application.discovery.scan_staging import staged_from_single_posting
from tailorcv.application.discovery.source_reading import SourceReader
from tailorcv.application.documents.evidence_artifacts import EvidenceArtifactService
from tailorcv.application.failures import (
    AlreadyEvaluatedError,
    EvaluationContentUnavailableError,
    EvaluationIdentityMissingError,
    NotFoundError,
    ValidationError,
)
from tailorcv.application.job_search.evaluation_targets import find_job_by_durable_identifier
from tailorcv.application.job_search.models import JobDetailResponse
from tailorcv.application.master_cv.cv import parse_cv
from tailorcv.application.tailoring.fit import Gate, Standing, gates, read, refusal
from tailorcv.application.tailoring.stages.context import citable_entries
from tailorcv.application.tailoring.stages.jobspec import HardGate, StoredPostingName
from tailorcv.application.tailoring.workflows._support import _posting_name, _read_master
from tailorcv.application.tailoring.workflows.evaluation_commit import (
    EvaluationCommitInputs,
    build_evaluation_commit,
    build_refusal_commit,
)
from tailorcv.application.tasks import BatchStepContext, TaskContext
from tailorcv.application.tasks.models import (
    EvaluationCostItemResponse,
    EvaluationCostStageResponse,
    EvaluationPreflightResponse,
)
from tailorcv.application.tasks.payloads import (
    DiscoveryEvaluateBatchPayload,
    EvaluationWorkflowPayload,
)
from tailorcv.application.workspaces.prompt_library import overrides_in
from tailorcv.application.workspaces.records.access import require_workspace
from tailorcv.application.workspaces.spend import SpendService, guarantee_billed
from tailorcv.domain import utc_now
from tailorcv.domain.identity import normalize_url, requisition_key, url_host
from tailorcv.domain.records import EvaluationDecision, RobotsReadBasis
from tailorcv.domain.search_parameters.conditions import excluded_by
from tailorcv.domain.staged_facts import PublisherClosingDate, StagedListingFacts, vouched_identity
from tailorcv.infrastructure.artifacts.runtime import RuntimeStorage
from tailorcv.infrastructure.build import current_build
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    CandidateProfile,
    Employer,
    JobEvaluation,
    JobPosting,
    MasterCvVersion,
    PendingDiscovery,
    PendingDiscoveryDispositionEvent,
    PendingPostingContent,
    PostingObservation,
    PostingUrl,
    Workspace,
)
from tailorcv.infrastructure.web_fetch.company_research import CompanyResearchFetcher
from tailorcv.infrastructure.web_fetch.policy import PoliteHttpClient
from tailorcv.shared.costs import ServiceTier, Usage
from tailorcv.shared.costs.prices import PriceSnapshot, current_prices, ledger_for
from tailorcv.shared.errors import IngestError, NoRequirementsError, TailorcvError

from .models import EvaluationRunner
from .runner import PipelineWorkflowRunner
from .service import WorkflowService

# A rerun of an already-stored posting re-fetches its body when the last stored
# observation is older than this, so ingest-layer fixes reach a re-evaluation
# without waiting for a new discovery.
_REEVAL_FETCH_STALE_HOURS = 24

_STORED_TEXT_FALLBACK = "stored_posting_text"

logger = logging.getLogger(__name__)


def _preflight_cost_total(values: list[Decimal | None]) -> Decimal | None:
    if any(value is None for value in values):
        return None
    return sum((value for value in values if value is not None), Decimal(0))


@dataclass(frozen=True)
class _EvaluationInputs:
    """Exact stored or fetched evidence for one evaluation run."""

    workspace_id: str
    pending_discovery_id: str | None
    job_posting_id: str | None
    supersedes_evaluation_id: str | None
    employer: str
    title: str
    # A title or employer no publisher vouched for labels the record but never refuses it.
    identity_trusted: bool
    name: StoredPostingName
    location: str
    requisition_id: str
    canonical_url: str
    discovery_url: str
    first_seen_at: datetime
    observed_at: datetime
    posting_status: str
    residence_rule: str
    employment_type: str
    payroll_arrangement: str
    source_timestamp: str | None
    posting_content: bytes
    extraction_method: str
    fetched_at: datetime | None
    http_status: int | None
    robots_basis: RobotsReadBasis | None
    master_content: bytes
    master_source_uri: str
    master_effective_at: datetime
    settings: Settings
    search_parameter_set_id: str | None
    prompt_overrides: dict[str, tuple[str, str]]
    staged_from_single_posting: bool = False
    # Names the stored text a failed re-read left this run on, or is empty.
    refresh_fallback: str = ""
    # True when a fresh re-read, observed at its own fetch time, supplied the observation.
    reread: bool = False
    # The location a board listed a pending discovery under, as the board wrote it.
    listing_location: str = ""
    publisher_closing_date: PublisherClosingDate | None = None

    def commit_inputs(self) -> EvaluationCommitInputs:
        """Return only the stored facts used by commit translation."""
        return EvaluationCommitInputs(
            workspace_id=self.workspace_id,
            supersedes_evaluation_id=self.supersedes_evaluation_id,
            employer=self.employer,
            title=self.title,
            location=self.location,
            requisition_id=self.requisition_id,
            canonical_url=self.canonical_url,
            discovery_url=self.discovery_url,
            first_seen_at=self.first_seen_at,
            observed_at=self.observed_at,
            posting_status=self.posting_status,
            residence_rule=self.residence_rule,
            employment_type=self.employment_type,
            payroll_arrangement=self.payroll_arrangement,
            source_timestamp=self.source_timestamp,
            extraction_method=self.extraction_method,
            fetched_at=self.fetched_at,
            http_status=self.http_status,
            robots_basis=self.robots_basis,
            master_content=self.master_content,
            master_source_uri=self.master_source_uri,
            master_effective_at=self.master_effective_at,
            search_parameter_set_id=self.search_parameter_set_id,
            publisher_closing_date=self.publisher_closing_date,
        )


@dataclass(frozen=True)
class _AlreadyEvaluatedOutcome:
    """Typed completed-task result for a duplicate that spent nothing."""

    pending_discovery_id: str
    job_posting_id: str
    evaluation_id: str
    existing_decision: str

    def progress(self) -> dict[str, Any]:
        """Return the durable task result consumed by the pending queue."""
        return {
            "decision": "already_evaluated",
            "pending_discovery_id": self.pending_discovery_id,
            "job_posting_id": self.job_posting_id,
            "evaluation_id": self.evaluation_id,
            "existing_decision": self.existing_decision,
        }


def _credential_conflict(
    conditions: list[tuple[str, str]], hard_gates: tuple[HardGate, ...]
) -> str:
    """Return the first verified blocking gate whose quote names a lacking credential, or ""."""
    stated = (gate.quote_or_span or gate.text for gate in hard_gates if gate.verified_blocking)
    return next((text for text in stated if excluded_by(conditions, (text,)) is not None), "")


def _refuse_evaluated_duplicate(
    session: Session,
    *,
    workspace_id: str,
    employer: str,
    requisition_id: str,
    canonical_url: str,
    discovery_url: str,
) -> None:
    """Raise when these identifiers name a posting that already has an evaluation."""
    existing_job = find_job_by_durable_identifier(
        session,
        workspace_id=workspace_id,
        employer=employer,
        requisition_id=requisition_id,
        canonical_url=canonical_url,
        discovery_url=discovery_url,
    )
    if existing_job is None:
        return
    current_evaluation = session.scalar(
        select(JobEvaluation)
        .where(JobEvaluation.job_posting_id == existing_job.id)
        .order_by(JobEvaluation.version_number.desc(), JobEvaluation.id.desc())
        .limit(1)
    )
    if current_evaluation is not None:
        raise AlreadyEvaluatedError(
            existing_job.id, current_evaluation.id, current_evaluation.decision
        )


def _posting_identities(pending: PendingDiscovery) -> set[str]:
    """Return the requisition and address keys the pre-spend duplicate check matches on."""
    identities = {
        f"url|{normalized}"
        for normalized in map(normalize_url, (pending.canonical_url, pending.discovery_url))
        if normalized
    }
    if pending.requisition_id.strip():
        identities.add(f"requisition|{requisition_key(pending.employer, pending.requisition_id)}")
    return identities


class EvaluationWorkflowService:
    """Load evaluation inputs, run screening, and commit its durable findings."""

    def __init__(
        self,
        database: Database,
        storage: RuntimeStorage,
        *,
        fetcher: PostingFetcher,
        board_fetcher: SourceReader | None = None,
        runner: EvaluationRunner | None = None,
        content_resolution: PostingContentResolutionService | None = None,
        http_client: PoliteHttpClient | None = None,
    ) -> None:
        """Bind the task to persistence, network, and existing screening seams."""
        self.database = database
        self.fetcher = fetcher
        self.scans = SourceScanService(
            database,
            storage=storage,
            fetcher=board_fetcher,
            posting_fetcher=self.fetcher,
            http_client=http_client,
        )
        self.runner = runner or PipelineWorkflowRunner()
        self.workflows = WorkflowService(
            database,
            storage,
            PipelineWorkflowRunner(),
            research_fetcher=CompanyResearchFetcher(client=http_client),
        )
        self.artifacts = EvidenceArtifactService(storage)
        self.current_content = CurrentPostingContentReader(database, storage=storage)
        self.content_resolution = content_resolution

    def preflight(
        self, workspace_id: str, selection: DiscoveryEvaluateBatchPayload
    ) -> EvaluationPreflightResponse:
        """Estimate current selected model costs from retained inputs without running tasks."""
        with self.database.session() as session:
            require_workspace(session, workspace_id)
        settings = self.workflows.settings.snapshot(workspace_id).settings
        prices = current_prices(settings.prices)
        samples: dict[tuple[str, str, str], list[int]] = {}
        kind: Literal["job", "pending_discovery"] = (
            "job" if selection.job_posting_ids else "pending_discovery"
        )
        ids = selection.job_posting_ids or selection.pending_discovery_ids
        items = []
        for target_id in ids:
            try:
                item = self._preflight_item(
                    workspace_id, kind, target_id, settings, prices, samples
                )
            except (TailorcvError, OSError, ValueError) as exc:
                item = EvaluationCostItemResponse(
                    target_kind=kind,
                    target_id=target_id,
                    expected_unavailable_reason=str(exc),
                    range_unavailable_reason=str(exc),
                )
            items.append(item)
        expected = _preflight_cost_total([item.expected_cost_usd for item in items])
        low = _preflight_cost_total([item.range_low_usd for item in items])
        high = _preflight_cost_total([item.range_high_usd for item in items])
        return EvaluationPreflightResponse(
            workspace_id=workspace_id,
            generated_at=utc_now(),
            items=items,
            expected_cost_usd=expected,
            range_low_usd=low,
            range_high_usd=high,
            expected_unavailable_reason=(
                "Some selected postings lack an expected estimate; inspect their reasons."
                if expected is None
                else None
            ),
            range_unavailable_reason=(
                "A complete range is unavailable for some selected postings; inspect their reasons."
                if high is None
                else None
            ),
            basis=(
                "Current configured stage models and rates. Inputs use retained "
                "UTF-8 bytes and prompt sizes; expected outputs use usable paid-call medians for "
                "the same workspace, provider, stage and requested model. The range models one "
                "attempt with configured output budgets and standard-tier fallback."
            ),
            limitations=[
                "Advisory estimates never stop a confirmed task and are not spending ceilings.",
                "Tokenization, refreshed content, retries and repairs can exceed this range.",
                "Cache reuse or deterministic refusals can reduce charges to zero.",
                "This read makes no provider or publisher request and never refreshes prices.",
                "The price table carries one date: the bundled table's release date or the "
                "date a refreshed table was fetched. Hand-entered or overridden rates have none.",
            ],
        )

    def _preflight_item(
        self,
        workspace_id: str,
        kind: Literal["job", "pending_discovery"],
        target_id: str,
        settings: Settings,
        prices: PriceSnapshot,
        samples: dict[tuple[str, str, str], list[int]],
    ) -> EvaluationCostItemResponse:
        if kind == "pending_discovery":
            if self.person_dismissal(workspace_id, target_id) is not None:
                return EvaluationCostItemResponse(
                    target_kind=kind,
                    target_id=target_id,
                    expected_cost_usd=Decimal(0),
                    range_low_usd=Decimal(0),
                    range_high_usd=Decimal(0),
                    current_skip_reason="Person-dismissed posting.",
                )
            inputs = self._pending_inputs(workspace_id, target_id, context=None, local_only=True)
        else:
            inputs = self._job_inputs(workspace_id, target_id, refresh_posting=False)
        inputs = replace(inputs, settings=settings)
        gate = self.refusing_gate(inputs)
        if gate is not None or self.cut_off(inputs):
            return EvaluationCostItemResponse(
                target_kind=kind,
                target_id=target_id,
                expected_cost_usd=Decimal(0),
                range_low_usd=Decimal(0),
                range_high_usd=Decimal(0),
                current_skip_reason=(
                    CUT_OFF_SKIP_REASON
                    if gate is None
                    else f"Screening refuses this posting: {gate.reason or gate.name}"
                ),
            )
        inventory = parse_cv(Path("master.md"), text=inputs.master_content.decode("utf-8"))
        names: list[Literal["jobspec", "match"]] = ["jobspec"]
        if citable_entries(inventory):
            names.append("match")
        prompt_text = {name: text for name, (_, text) in inputs.prompt_overrides.items()}
        with prompts.overriding(prompt_text):
            stages = [self._preflight_stage(inputs, name, prices, samples) for name in names]
        expected = _preflight_cost_total([stage.expected_cost_usd for stage in stages])
        high = _preflight_cost_total([stage.range_high_usd for stage in stages])
        return EvaluationCostItemResponse(
            target_kind=kind,
            target_id=target_id,
            stages=stages,
            expected_cost_usd=expected,
            range_low_usd=Decimal(0) if high is not None else None,
            range_high_usd=high,
            expected_unavailable_reason=(
                "An expected stage estimate is unavailable; inspect the stage reasons."
                if expected is None
                else None
            ),
            range_unavailable_reason=(
                "Current pricing is unavailable for a configured stage." if high is None else None
            ),
            retained_details_read_at=inputs.fetched_at or inputs.observed_at,
        )

    def _preflight_stage(
        self,
        inputs: _EvaluationInputs,
        name: Literal["jobspec", "match"],
        prices: PriceSnapshot,
        samples: dict[tuple[str, str, str], list[int]],
    ) -> EvaluationCostStageResponse:
        settings = inputs.settings
        stage = Stage(name)
        tier = settings.unattended_tier()
        choice = settings.choice_for_stage(stage, tier)
        provider = settings.provider.value
        priced = prices.priced_model(choice.model, provider=provider)
        price = prices.prices.get(priced) if priced is not None else None
        provenance = prices.provenance.get(priced) if priced is not None else None
        prompt_bytes = sum(
            len(prompts.load(f"{name}_{suffix}").encode("utf-8")) for suffix in ("system", "user")
        )
        material_bytes = len(inputs.posting_content) + prompt_bytes
        allowance = 512
        if name == "match":
            material_bytes += len(inputs.master_content) + len(
                settings.rules.prompt_block().encode()
            )
            allowance += settings.policy_for(Stage.JOBSPEC).max_tokens
        estimated_input = (material_bytes + 3) // 4 + allowance
        input_high = material_bytes + allowance
        output_budget = settings.policy_for(stage).max_tokens
        key = (provider, choice.model, name)
        if key not in samples:
            samples[key] = SpendService(self.database).evaluation_output_tokens(
                inputs.workspace_id, name, provider, choice.model
            )
        outputs = samples[key]
        expected = None
        high = None
        expected_reason: str | None = (
            f"No usable paid output-token samples for the configured {name} model."
        )
        range_reason = None
        if price is None:
            expected_reason = range_reason = f"No current price is available for {choice.model}."
        else:
            high = Decimal(
                str(price.cost(Usage(input_tokens=input_high, output_tokens=output_budget)))
            )
            if outputs:
                expected_price = price.halved() if tier is ServiceTier.FLEX else price
                expected = Decimal(
                    str(
                        expected_price.cost(
                            Usage(
                                input_tokens=estimated_input,
                                output_tokens=min(output_budget, int(median(outputs))),
                            )
                        )
                    )
                )
                expected_reason = None
        return EvaluationCostStageResponse.model_validate(
            {
                "stage": name,
                "provider": provider,
                "model": choice.model,
                "requested_service_tier": tier.value,
                "price": price,
                "provenance": provenance,
                "input_token_estimate": estimated_input,
                "input_token_range_high": input_high,
                "output_token_budget": output_budget,
                "expected_cost_usd": expected,
                "range_low_usd": Decimal(0) if high is not None else None,
                "range_high_usd": high,
                "expected_unavailable_reason": expected_reason,
                "range_unavailable_reason": range_reason,
                "historical_output_samples": len(outputs),
            }
        )

    def inputs(
        self,
        workspace_id: str,
        *,
        pending_discovery_id: str | None,
        job_posting_id: str | None,
        context: TaskContext | None = None,
    ) -> _EvaluationInputs:
        """Load one pending discovery or one stored posting with its current CV."""
        if pending_discovery_id is not None:
            return self._pending_inputs(workspace_id, pending_discovery_id, context=context)
        if job_posting_id is None:
            raise ValidationError("Evaluation requires exactly one target.")
        return self._job_inputs(workspace_id, job_posting_id, context=context)

    def _pending_inputs(
        self,
        workspace_id: str,
        pending_id: str,
        *,
        context: TaskContext | None,
        local_only: bool = False,
    ) -> _EvaluationInputs:
        """Fetch a pending discovery after loading its exact workspace inputs."""
        with self.database.session() as session:
            workspace = session.get(Workspace, workspace_id)
            pending = session.get(PendingDiscovery, pending_id)
            if (
                workspace is None
                or workspace.archived_at is not None
                or pending is None
                or pending.workspace_id != workspace_id
                or pending.archived_at is not None
            ):
                raise NotFoundError("The pending discovery does not exist in this workspace.")
            master = session.scalar(
                select(MasterCvVersion)
                .join(
                    CandidateProfile,
                    CandidateProfile.id == MasterCvVersion.candidate_profile_id,
                )
                .where(CandidateProfile.workspace_id == workspace_id)
                .order_by(MasterCvVersion.effective_at.desc())
            )
            if master is None:
                raise ValidationError("The workspace has no master CV version.")
            master_content = _read_master(self.workflows, master, workspace)
            settings_snapshot = self.workflows.settings.snapshot(workspace_id)
            prompt_overrides = overrides_in(session, workspace_id)
            target_url = pending.canonical_url or pending.discovery_url
            employer = pending.employer
            title = pending.title
            location = pending.location
            requisition_id = pending.requisition_id
            canonical_url = pending.canonical_url
            discovery_url = pending.discovery_url
            first_seen_at = datetime.combine(pending.first_seen_on, time(), tzinfo=UTC)
            master_source_uri = master.source_uri
            master_effective_at = master.effective_at
            single_posting = staged_from_single_posting(pending.reason)
            identity_trusted = StagedListingFacts.from_json(pending.staged_facts).publisher_vouched
            if not title.strip() or not employer.strip():
                raise EvaluationIdentityMissingError(
                    "This discovery has no staged title or employer for the ledger to "
                    "record; refresh its publisher metadata to name it first."
                )
            # A duplicate that names an evaluated posting by requisition or URL
            # would spend the provider only to be rejected at commit; refuse it
            # before the network fetch and before any paid call.
            _refuse_evaluated_duplicate(
                session,
                workspace_id=workspace_id,
                employer=employer,
                requisition_id=requisition_id,
                canonical_url=canonical_url,
                discovery_url=discovery_url,
            )
        retained = (
            self.current_content.read(workspace_id, pending_id)
            if local_only
            else self._pending_content(workspace_id, pending_id, target_url, context)
        )
        if retained is None:
            raise ValidationError("Read this posting's details before preparing its cost estimate.")
        posting_content = retained.body
        observed_at = retained.fetched_at
        posting_status = "active"
        extraction_method = retained.extraction_method.value
        fetched_at = retained.fetched_at
        http_status = retained.http_status
        robots_basis = retained.robots_basis
        with self.database.session() as session:
            # A first read of the page can name the requisition of the posting it wraps.
            read = session.get(PendingDiscovery, pending_id)
            if read is not None and read.requisition_id != requisition_id:
                requisition_id = read.requisition_id
                _refuse_evaluated_duplicate(
                    session,
                    workspace_id=workspace_id,
                    employer=employer,
                    requisition_id=requisition_id,
                    canonical_url=canonical_url,
                    discovery_url=discovery_url,
                )
            name = _posting_name(
                session,
                workspace_id=workspace_id,
                title=title,
                employer=employer,
                body=posting_content,
                vouched=identity_trusted,
                pending_discovery_id=pending_id,
                posting_urls=(canonical_url, discovery_url),
            )
        facts = posting_facts(
            posting_content.decode("utf-8", errors="replace"), location_hint=location
        )
        return _EvaluationInputs(
            workspace_id=workspace_id,
            pending_discovery_id=pending_id,
            job_posting_id=None,
            supersedes_evaluation_id=None,
            employer=employer,
            title=title,
            identity_trusted=identity_trusted,
            name=name,
            location=facts.location,
            requisition_id=requisition_id,
            canonical_url=canonical_url,
            discovery_url=discovery_url,
            first_seen_at=first_seen_at,
            observed_at=observed_at,
            posting_status=posting_status,
            residence_rule=facts.residence_rule,
            employment_type=facts.employment_type,
            payroll_arrangement=facts.payroll_arrangement,
            source_timestamp=None,
            posting_content=posting_content,
            extraction_method=extraction_method,
            fetched_at=fetched_at,
            http_status=http_status,
            robots_basis=robots_basis,
            master_content=master_content,
            master_source_uri=master_source_uri,
            master_effective_at=master_effective_at,
            settings=settings_snapshot.settings,
            search_parameter_set_id=settings_snapshot.search_parameter_set_id,
            prompt_overrides=prompt_overrides,
            staged_from_single_posting=single_posting,
            listing_location=location,
            publisher_closing_date=retained.publisher_closing_date,
        )

    def _pending_content(
        self,
        workspace_id: str,
        pending_id: str,
        target_url: str,
        context: TaskContext | None,
    ) -> CurrentPostingContent:
        """Return retained content, reading the publisher only when none is retained."""
        retained = self.current_content.read(workspace_id, pending_id)
        if retained is not None:
            return retained
        fetched = self.fetcher.fetch(target_url)
        retained_fetch = resolved_publisher_fetch(target_url, fetched)
        if retained_fetch is None:
            raise EvaluationContentUnavailableError(
                "The discovered posting has no readable verified publisher body to retain "
                "before evaluation."
            )
        if self.content_resolution is None:
            raise ValidationError(
                "The discovered posting cannot be evaluated without content-evidence storage."
            )
        self.content_resolution.store_prefetched_result(
            workspace_id,
            pending_id,
            retained_fetch,
            context,
            origin="evaluation.prefetched_fetch",
        )
        retained = self.current_content.read(workspace_id, pending_id)
        if retained is None:
            raise EvaluationContentUnavailableError(
                "The discovered posting's retained publisher body is unavailable for evaluation."
            )
        return retained

    def _job_inputs(
        self,
        workspace_id: str,
        job_id: str,
        *,
        context: TaskContext | None = None,
        refresh_posting: bool = True,
    ) -> _EvaluationInputs:
        """Load an existing posting and the evaluation it must supersede."""
        with self.database.session() as session:
            job = session.get(JobPosting, job_id)
            if job is None or job.workspace_id != workspace_id or job.archived_at is not None:
                raise NotFoundError("The workflow job does not exist in this workspace.")
        workflow = self.workflows.inputs(workspace_id, job_id)
        with self.database.session() as session:
            job = session.get(JobPosting, job_id)
            # The commit refuses an archived posting; refusing it here keeps its paid stages
            # from running first.
            if job is None or job.archived_at is not None:
                raise NotFoundError("The workflow job does not exist in this workspace.")
            employer = session.get(Employer, job.employer_id)
            observation = session.scalar(
                select(PostingObservation)
                .where(PostingObservation.job_posting_id == job_id)
                .order_by(PostingObservation.observed_at.desc(), PostingObservation.id.desc())
            )
            current = session.scalar(
                select(JobEvaluation)
                .where(JobEvaluation.job_posting_id == job_id)
                .order_by(JobEvaluation.version_number.desc(), JobEvaluation.id.desc())
            )
            posting_url = session.scalar(
                select(PostingUrl)
                .where(
                    PostingUrl.job_posting_id == job_id,
                    PostingUrl.archived_at.is_(None),
                )
                .order_by(PostingUrl.is_primary.desc(), PostingUrl.created_at)
            )
            if employer is None or observation is None:
                raise ValidationError("The stored job is missing evaluation metadata.")
            url = posting_url.url if posting_url is not None else ""
            posting_content = workflow.posting_content
            extraction_method = observation.extraction_method or "stored_text"
            observed_at = observation.observed_at
            posting_status = observation.posting_status
            fetched_at = observation.fetched_at
            http_status = observation.http_status
            robots_basis = (
                RobotsReadBasis(observation.robots_basis) if observation.robots_basis else None
            )
            closing_date = (
                PublisherClosingDate.from_json(observation.publisher_closing_date)
                if observation.publisher_closing_date is not None
                else None
            )
        # An active posting whose last reading is stale gets one refresh attempt,
        # so a rerun reads it through the current ingest code.
        fresh, refresh_fallback = (
            self._maybe_refresh_posting(url, posting_status, observed_at, context=context)
            if refresh_posting
            else (None, "")
        )
        if fresh is not None:
            # A closed reading keeps the stored body as its evidence.
            if fresh.posting_status == "active" and fresh.text:
                posting_content = fresh.text.encode("utf-8")
                closing_date = fresh.publisher_closing_date or publisher_closing_date(
                    fresh.text, source_url=fresh.final_url or url, observed_at=fresh.fetched_at
                )
            extraction_method = fresh.method
            observed_at = fresh.fetched_at
            posting_status = fresh.posting_status
            fetched_at = fresh.fetched_at
            http_status = fresh.http_status
            robots_basis = fresh.robots_basis
        with self.database.session() as session:
            name = _posting_name(
                session,
                workspace_id=workspace_id,
                title=job.title,
                employer=employer.name,
                body=posting_content,
                vouched=workflow.identity_vouched,
                posting_urls=workflow.posting_urls,
            )
        return _EvaluationInputs(
            workspace_id=workspace_id,
            pending_discovery_id=None,
            job_posting_id=job_id,
            supersedes_evaluation_id=current.id if current is not None else None,
            employer=employer.name,
            title=job.title,
            # A tracked posting's identity is the ledger's own, entered by the person.
            identity_trusted=True,
            name=name,
            location=observation.location,
            requisition_id="",
            canonical_url=url,
            discovery_url=url,
            first_seen_at=job.first_seen_at,
            observed_at=observed_at,
            posting_status=posting_status,
            residence_rule=observation.residence_rule,
            employment_type=observation.employment_type,
            payroll_arrangement=observation.payroll_arrangement,
            source_timestamp=None,
            posting_content=posting_content,
            extraction_method=extraction_method,
            fetched_at=fetched_at,
            http_status=http_status,
            robots_basis=robots_basis,
            master_content=workflow.master_content,
            master_source_uri=workflow.master_version.source_uri,
            master_effective_at=workflow.master_version.effective_at,
            settings=workflow.settings,
            search_parameter_set_id=workflow.search_parameter_set_id,
            prompt_overrides=workflow.prompt_overrides,
            refresh_fallback=refresh_fallback,
            reread=fresh is not None,
            listing_location=observation.location or "",
            publisher_closing_date=closing_date,
        )

    def _maybe_refresh_posting(
        self,
        url: str,
        posting_status: str,
        observed_at: datetime,
        *,
        context: TaskContext | None = None,
    ) -> tuple[FetchedPosting | None, str]:
        """Re-fetch a stale active posting, or return None and the fallback a failed read took.

        A closed reading is returned so the status gate refuses the rerun as closed.
        """
        if not url or posting_status != "active":
            return None, ""
        if datetime.now(UTC) - observed_at < timedelta(hours=_REEVAL_FETCH_STALE_HOURS):
            return None, ""
        try:
            fetched = self.fetcher.fetch(url)
        except Exception as exc:  # noqa: BLE001 - a rerun must never fail on network trouble
            failure = {"error_type": type(exc).__name__}
        else:
            readable = fetched.posting_status == "active" and (fetched.text or "").strip()
            if fetched.posting_status == "closed" or readable:
                return fetched, ""
            # An unverified read is not evidence of closure; its method says why it failed.
            failure = {
                "reason": "empty_body" if fetched.posting_status == "active" else fetched.method
            }
        details = {
            "refresh_fallback": _STORED_TEXT_FALLBACK,
            "host": url_host(url) or "unknown",
            **failure,
        }
        logger.warning("posting_refresh_failed", extra=details)
        if context is not None:
            context.progress("posting_refresh_failed", details)
        return None, _STORED_TEXT_FALLBACK

    def refused_by(
        self, inputs: _EvaluationInputs, passing: frozenset[str] = frozenset()
    ) -> Gate | None:
        """Return the stated condition this posting fails, or nothing.

        Read by rule from the posting and the person's parameters, so it costs
        nothing and answers before a provider is asked anything. Rule gates named
        in ``passing`` are skipped.
        """
        if inputs.posting_status and inputs.posting_status != "active":
            return Gate(
                POSTING_STATUS_CONDITION,
                Standing.INELIGIBLE,
                f"the last reading of this posting says {inputs.posting_status}",
            )
        parameters = inputs.settings.job_search
        reading = read(
            inputs.posting_content.decode("utf-8", errors="replace"),
            parse_cv(
                Path("master.md"),
                text=inputs.master_content.decode("utf-8", errors="replace"),
            ),
            parameters=parameters,
            listing_location=inputs.listing_location,
        )
        title, employer = vouched_identity(inputs.identity_trusted, inputs.title, inputs.employer)
        found = gates(reading, parameters, title=title, employer=employer)
        return refusal(tuple(gate for gate in found if gate.name not in passing))

    def cut_off(self, inputs: _EvaluationInputs) -> bool:
        """Return whether the active posting's retained text reads as a fragment."""
        if inputs.posting_status and inputs.posting_status != "active":
            return False
        with self.database.session() as session:
            too_short = too_short_reviews(session, inputs.workspace_id).digests
        return retained_text_cut_off(inputs.posting_content, inputs.extraction_method, too_short)

    def refusing_gate(self, inputs: _EvaluationInputs) -> Gate | None:
        """Return the rule gate this posting fails that no marker or restore holds it past."""
        passing: frozenset[str] = frozenset()
        gate = self.refused_by(inputs)
        while gate is not None and self.restored_past(inputs, gate):
            passing |= {gate.name}
            gate = self.refused_by(inputs, passing)
        return gate

    def restored_past(self, inputs: _EvaluationInputs, gate: Gate) -> bool:
        """Return whether a person's marker or restore holds this discovery past the gate."""
        if inputs.pending_discovery_id is None or gate.name == POSTING_STATUS_CONDITION:
            return False
        with self.database.session() as session:
            if marked_ids(session, inputs.workspace_id, [inputs.pending_discovery_id]):
                return True
            return DiscoveryDispositionService(self.database).person_restored_in_session(
                session, inputs.workspace_id, inputs.pending_discovery_id, gate.name
            )

    def person_dismissal(
        self, workspace_id: str, pending_discovery_id: str
    ) -> PendingDiscoveryDispositionEvent | None:
        """Return the latest disposition when a person has dismissed this discovery."""
        with self.database.session() as session:
            return DiscoveryDispositionService(self.database).person_dismissal_in_session(
                session, workspace_id, pending_discovery_id
            )

    def family_head_dismissal(
        self, workspace_id: str, pending_discovery_id: str
    ) -> PendingDiscoveryDispositionEvent | None:
        """Return the dismissal of this regional variant's family head, of any origin."""
        with self.database.session() as session:
            return family_head_dismissal(session, workspace_id, pending_discovery_id)

    def structured_holdout(
        self, workspace_id: str, pending_discovery_id: str
    ) -> PendingDiscoveryDispositionEvent | None:
        """Return the latest disposition when the program itself held this discovery out."""
        with self.database.session() as session:
            return DiscoveryDispositionService(self.database).structured_holdout_in_session(
                session, workspace_id, pending_discovery_id
            )

    def pending_is_withdrawn(self, workspace_id: str, pending_discovery_id: str) -> bool:
        """Return whether a still-pending discovery closed after task selection."""
        with self.database.session() as session:
            pending = session.get(PendingDiscovery, pending_discovery_id)
            return (
                pending is not None
                and pending.workspace_id == workspace_id
                and pending.archived_at is None
                and pending.withdrawn_at is not None
            )

    def posting_groups(self, workspace_id: str, pending_discovery_ids: list[str]) -> dict[str, str]:
        """Map each selected row to the first selected row naming its posting.

        Rows name one posting when they share the employer's requisition or a posting address.
        """
        with self.database.session() as session:
            identities = {
                row.id: _posting_identities(row)
                for row in session.scalars(
                    select(PendingDiscovery).where(
                        PendingDiscovery.workspace_id == workspace_id,
                        PendingDiscovery.id.in_(pending_discovery_ids),
                    )
                )
            }
        position = {pending_id: index for index, pending_id in enumerate(pending_discovery_ids)}
        lead = {pending_id: pending_id for pending_id in pending_discovery_ids}

        def lead_of(pending_id: str) -> str:
            while lead[pending_id] != pending_id:
                pending_id = lead[pending_id]
            return pending_id

        first_named_by: dict[str, str] = {}
        for pending_id in pending_discovery_ids:
            for identity in identities.get(pending_id, ()):
                earlier = first_named_by.setdefault(identity, pending_id)
                ours, theirs = lead_of(pending_id), lead_of(earlier)
                if ours != theirs:
                    first, later = sorted((ours, theirs), key=position.__getitem__)
                    lead[later] = first
        return {pending_id: lead_of(pending_id) for pending_id in pending_discovery_ids}

    def record_refusal(
        self, inputs: _EvaluationInputs, gate: Gate, task_id: str | None
    ) -> dict[str, Any]:
        """File the refusal and report it; a tracked posting also gets a superseding evaluation."""
        condition, reason = file_refusal(
            self.database,
            RefusedPosting(
                workspace_id=inputs.workspace_id,
                employer=inputs.employer,
                title=inputs.title,
                pending_discovery_id=inputs.pending_discovery_id,
                job_posting_id=inputs.job_posting_id,
                search_parameter_set_id=inputs.search_parameter_set_id,
            ),
            condition=gate.name,
            reason=gate.reason,
            task_id=task_id,
            origin="evaluation.refusal",
        )
        refused = {
            "job_posting_id": inputs.job_posting_id,
            "decision": EvaluationDecision.INELIGIBLE.value,
            "refused_by": condition,
            "reason": reason,
        }
        # A pending row leaves the queue by its disposition; a tracked posting's stored
        # decision stays current until a newer version replaces it.
        if inputs.pending_discovery_id is not None or inputs.job_posting_id is None:
            return refused
        posting_text = inputs.posting_content.decode("utf-8", errors="replace")
        with self.database.session() as session:
            records = same_body_eligibility(
                session,
                inputs.job_posting_id,
                hashlib.sha256(posting_text.encode("utf-8")).hexdigest(),
            )
            eligibility = (
                EligibilityInput(
                    workspace_location_key=records[0].workspace_location_key,
                    region_rule=records[0].region_rule,
                    arrangement=records[0].arrangement,
                    details=records[0].details,
                )
                if records
                else None
            )
        proposal = build_refusal_commit(
            inputs.commit_inputs(),
            posting_text=posting_text,
            condition=condition,
            reason=reason,
            eligibility=eligibility,
            code_revision=current_build().revision or None,
        )
        detail = job_search.commit_evaluation(
            self.database,
            self.artifacts,
            proposal,
            origin="worker",
            correlation_id=task_id,
            job_posting_id=inputs.job_posting_id,
        )
        if detail.decision is None:
            raise ValidationError("The committed evaluation has no decision.")
        return {
            **refused,
            "evaluation_id": detail.evaluation_id,
            "evaluation_version": detail.evaluation_version,
            "decision": detail.decision.value,
        }

    def dismiss_no_requirements(self, inputs: _EvaluationInputs, task_id: str | None) -> None:
        """Dismiss the pending row when the body it still holds is the one evaluated."""
        if inputs.pending_discovery_id is None:
            return
        evaluated = hashlib.sha256(inputs.posting_content).hexdigest()
        with self.database.session() as session:
            pending = session.get(PendingDiscovery, inputs.pending_discovery_id)
            current = (
                session.get(PendingPostingContent, pending.current_content_id)
                if pending is not None and pending.current_content_id is not None
                else None
            )
            if current is None or current.content_sha256 != evaluated:
                return
            DiscoveryDispositionService(self.database).dismiss_no_requirements_in_session(
                session,
                inputs.workspace_id,
                inputs.pending_discovery_id,
                content_sha256=evaluated,
                actor="worker",
                origin="evaluation.body_screen",
                correlation_id=task_id,
            )

    def run(
        self, context: TaskContext, inputs: _EvaluationInputs, *, fresh: bool = False
    ) -> JobDetailResponse:
        """Run the existing screen machinery and commit its structured findings."""
        ledger = ledger_for(inputs.settings.prices)
        with guarantee_billed(
            self.database,
            workspace_id=inputs.workspace_id,
            task_id=context.task_id,
            ledger=ledger,
            provider=inputs.settings.provider,
            origin="evaluation",
        ):
            with tempfile.TemporaryDirectory(prefix="tailorcv-evaluate-") as temporary:
                root = Path(temporary)
                source = root / "posting.txt"
                master = root / "master.md"
                source.write_bytes(inputs.posting_content)
                master.write_bytes(inputs.master_content)
                prompt_text = {
                    name: content for name, (_, content) in inputs.prompt_overrides.items()
                }
                with prompts.overriding(prompt_text):
                    screened = self.runner.evaluate(
                        source,
                        inputs.settings,
                        master_cv=master,
                        service_tier=inputs.settings.unattended_tier(),
                        ledger=ledger,
                        stored_name=inputs.name,
                        listing_location=inputs.listing_location,
                        fresh=fresh,
                    )
            context.raise_if_cancelled()
            credential_gate = _credential_conflict(
                inputs.settings.job_search.excluded_conditions, screened.spec.hard_gates
            )
            proposal = build_evaluation_commit(
                inputs.commit_inputs(), screened, credential_gate=credential_gate
            )
            result = job_search.commit_evaluation(
                self.database,
                self.artifacts,
                proposal,
                origin="worker",
                correlation_id=context.task_id,
                pending_discovery_id=inputs.pending_discovery_id,
                job_posting_id=inputs.job_posting_id,
            )
            with self.database.session() as session:
                evaluation = session.get(JobEvaluation, result.evaluation_id)
                if evaluation is not None and evaluation.master_cv_version_id is not None:
                    refresh_for_evaluation(
                        session,
                        workspace_id=inputs.workspace_id,
                        master_cv_version_id=evaluation.master_cv_version_id,
                        evaluation_id=evaluation.id,
                        payload=evaluation.fit_report_payload,
                        origin="evaluation",
                        correlation_id=context.task_id,
                    )
                    session.commit()
        # A row staged one URL at a time carries an unverified employer hint, so its
        # evaluation promotes neither, even once a tracked board lists it.
        if not inputs.staged_from_single_posting:
            page = inputs.posting_content.decode("utf-8", errors="replace")
            for url in dict.fromkeys((inputs.canonical_url, inputs.discovery_url)):
                if url and self.scans.remember_if_readable(
                    inputs.workspace_id,
                    inputs.employer,
                    url,
                    evidence_text=page,
                    origin="evaluation",
                    correlation_id=context.task_id,
                ):
                    break
        return result


class EvaluationHandler:
    """Evaluate one pending discovery or rerun one stored posting."""

    def __init__(self, service: EvaluationWorkflowService) -> None:
        """Bind the handler to its application service."""
        self.service = service

    def __call__(self, context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Evaluate the selected target and publish its durable ledger identity."""
        value = EvaluationWorkflowPayload.model_validate(payload)
        task = context.queue.get(context.task_id)
        return self.evaluate_target(
            context,
            str(task.workspace_id),
            created_at=task.created_at,
            pending_discovery_id=value.pending_discovery_id,
            job_posting_id=value.job_posting_id,
            fresh=value.fresh,
        )

    def posting_groups(self, workspace_id: str, pending_discovery_ids: list[str]) -> dict[str, str]:
        """Map each selected row to the first selected row naming its posting."""
        return self.service.posting_groups(workspace_id, pending_discovery_ids)

    def _settled_before_spend(
        self, context: TaskContext, workspace_id: str, pending_discovery_id: str
    ) -> dict[str, Any] | None:
        """Return the skip record when the row is settled, read before any fetch or spend."""
        # A dismissal, hold-out, or withdrawal landing mid-run is not honored. The commit
        # refuses a variant whose family head is dismissed by anyone, so that skips too.
        dismissal = self.service.person_dismissal(
            workspace_id, pending_discovery_id
        ) or self.service.family_head_dismissal(workspace_id, pending_discovery_id)
        if dismissal is not None:
            skipped = {
                "pending_discovery_id": pending_discovery_id,
                "skipped": "dismissed",
                "disposition_event_id": dismissal.id,
            }
            context.progress("dismissed_skipped", skipped)
            return skipped
        held_out = self.service.structured_holdout(workspace_id, pending_discovery_id)
        if held_out is not None:
            return self._skipped(context, pending_discovery_id, "held_out", held_out.reason)
        if self.service.pending_is_withdrawn(workspace_id, pending_discovery_id):
            skipped = {"pending_discovery_id": pending_discovery_id, "skipped": "withdrawn"}
            context.progress("withdrawn_skipped", skipped)
            return skipped
        return None

    def evaluate_target(
        self,
        context: TaskContext,
        workspace_id: str,
        *,
        created_at: datetime,
        pending_discovery_id: str | None,
        job_posting_id: str | None,
        fresh: bool = False,
    ) -> dict[str, Any]:
        """Evaluate one discovery or rerun target and return its progress record."""
        if pending_discovery_id is not None:
            settled = self._settled_before_spend(context, workspace_id, pending_discovery_id)
            if settled is not None:
                return settled
        inputs = self._inputs_or_skip(
            context,
            workspace_id,
            pending_discovery_id=pending_discovery_id,
            job_posting_id=job_posting_id,
        )
        if isinstance(inputs, dict):
            return inputs
        if inputs.job_posting_id is not None and not inputs.reread:
            inputs = replace(inputs, observed_at=created_at)
        context.progress(
            "inputs_verified",
            {
                "pending_discovery_id": inputs.pending_discovery_id,
                "job_posting_id": inputs.job_posting_id,
            },
        )
        context.raise_if_cancelled()
        gate = self.service.refusing_gate(inputs)
        fallback = {"refresh_fallback": inputs.refresh_fallback} if inputs.refresh_fallback else {}
        if gate is not None:
            refusal_result = self.service.record_refusal(inputs, gate, context.task_id) | fallback
            context.progress("posting_refused", refusal_result)
            return refusal_result
        if self.service.cut_off(inputs):
            return self._skipped(
                context,
                inputs.pending_discovery_id,
                "partial_text",
                CUT_OFF_SKIP_REASON,
                job_posting_id=inputs.job_posting_id,
            )
        try:
            detail = self.service.run(context, inputs, fresh=fresh)
        except IngestError as error:
            if pending_discovery_id is None:
                raise
            if isinstance(error, NoRequirementsError):
                self.service.dismiss_no_requirements(inputs, context.task_id)
            return self._skipped(context, pending_discovery_id, "unreadable", str(error))
        if detail.decision is None:
            raise ValidationError("The committed evaluation has no decision.")
        result = {
            "job_posting_id": detail.job_posting_id,
            "evaluation_id": detail.evaluation_id,
            "evaluation_version": detail.evaluation_version,
            "decision": detail.decision.value,
        } | fallback
        context.progress("evaluation_committed", result)
        return result

    @staticmethod
    def _skipped(
        context: TaskContext,
        pending_discovery_id: str | None,
        reason: str,
        detail: str,
        *,
        job_posting_id: str | None = None,
    ) -> dict[str, Any]:
        """Report one no-spend outcome for a target and return its task result."""
        skipped: dict[str, Any] = {"pending_discovery_id": pending_discovery_id}
        if job_posting_id is not None:
            skipped["job_posting_id"] = job_posting_id
        skipped |= {"skipped": reason, "detail": detail}
        context.progress(f"{reason}_skipped", skipped)
        return skipped

    def _inputs_or_skip(
        self,
        context: TaskContext,
        workspace_id: str,
        *,
        pending_discovery_id: str | None,
        job_posting_id: str | None,
    ) -> _EvaluationInputs | dict[str, Any]:
        """Load inputs or report a durable no-spend outcome for a pending discovery."""
        try:
            return self.service.inputs(
                workspace_id,
                pending_discovery_id=pending_discovery_id,
                job_posting_id=job_posting_id,
                context=context,
            )
        except AlreadyEvaluatedError as exc:
            if not isinstance(pending_discovery_id, str):
                raise RuntimeError(
                    "An already-evaluated pending discovery must retain its source identity."
                ) from exc
            already = _AlreadyEvaluatedOutcome(
                pending_discovery_id=pending_discovery_id,
                job_posting_id=exc.job_posting_id,
                evaluation_id=exc.evaluation_id,
                existing_decision=exc.existing_decision,
            ).progress()
            context.progress("already_evaluated", already)
            return already
        except EvaluationIdentityMissingError as error:
            if pending_discovery_id is None:
                raise RuntimeError("Only a pending discovery can lack a staged name.") from error
            return self._skipped(context, pending_discovery_id, "unnamed", str(error))
        except (EvaluationContentUnavailableError, IngestError) as error:
            if pending_discovery_id is None:
                raise RuntimeError(
                    "Only a pending discovery can lack evaluation content."
                ) from error
            return self._skipped(context, pending_discovery_id, "unreadable", str(error))


class DiscoveryEvaluateBatchHandler:
    """Evaluate a selection of pending discoveries or tracked postings in one approved task."""

    def __init__(self, evaluator: EvaluationHandler, *, lanes: int = 1) -> None:
        """Reuse the single-target evaluation handler for each row, ``lanes`` rows at a time."""
        if lanes < 1 or lanes > 8:
            raise ValueError("Evaluation batch lanes must be between 1 and 8.")
        self.evaluator = evaluator
        self.lanes = lanes

    def __call__(self, context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Evaluate each selected discovery or posting and record per-row failures."""
        value = DiscoveryEvaluateBatchPayload.model_validate(payload)
        task = context.queue.get(context.task_id)
        workspace_id = str(task.workspace_id)
        posting_batch = bool(value.job_posting_ids)
        target_ids = value.job_posting_ids if posting_batch else value.pending_discovery_ids
        target_key = "job_posting_id" if posting_batch else "pending_discovery_id"
        sweep = _EvaluationSweep(context, total=len(target_ids))
        # A row's pre-spend duplicate check sees only committed evaluations, so rows
        # naming one posting run one after another and the later one is not paid for.
        group = (
            {target_id: target_id for target_id in target_ids}
            if posting_batch
            else self.evaluator.posting_groups(workspace_id, target_ids)
        )

        def evaluate(target_id: str) -> dict[str, Any]:
            return self.evaluator.evaluate_target(
                sweep,
                workspace_id,
                created_at=task.created_at,
                pending_discovery_id=None if posting_batch else target_id,
                job_posting_id=target_id if posting_batch else None,
            )

        stop = self._run_lanes(context, sweep, target_ids, target_key, group, evaluate)
        if stop is None:
            return sweep.summary()
        if isinstance(stop, TailorcvError):
            context.progress("evaluating", sweep.summary())
        raise stop

    def _run_lanes(
        self,
        context: TaskContext,
        sweep: _EvaluationSweep,
        target_ids: list[str],
        target_key: str,
        group: dict[str, str],
        evaluate: Callable[[str], dict[str, Any]],
    ) -> Exception | None:
        """Run rows ``lanes`` at a time, one per group, and return the error that stopped them."""
        stop: Exception | None = None
        with ThreadPoolExecutor(max_workers=min(self.lanes, max(1, len(target_ids)))) as pool:
            waiting = list(enumerate(target_ids))
            futures: dict[Future[dict[str, Any]], tuple[int, str]] = {}
            running_groups: set[str] = set()

            def submit_next() -> bool:
                ready = next((row for row in waiting if group[row[1]] not in running_groups), None)
                if ready is None:
                    return False
                context.raise_if_cancelled()
                waiting.remove(ready)
                position, target_id = ready
                running_groups.add(group[target_id])
                sweep.start({target_key: target_id})
                futures[pool.submit(evaluate, target_id)] = (position, target_id)
                return True

            while len(futures) < self.lanes and submit_next():
                pass
            while futures:
                future = next(as_completed(futures))
                position, target_id = futures.pop(future)
                running_groups.discard(group[target_id])
                error = _settle(sweep, future, position, {target_key: target_id})
                stop = stop or error
                while stop is None and len(futures) < self.lanes and submit_next():
                    pass
        return stop


def _settle(
    sweep: _EvaluationSweep,
    future: Future[dict[str, Any]],
    position: int,
    target: dict[str, str],
) -> Exception | None:
    """Record one finished row and return the error that stops the batch, if any."""
    try:
        sweep.record(position, future.result())
    except TailorcvError as error:
        sweep.fail(
            position,
            {**target, "error_category": type(error).__name__, "error_message": str(error)},
        )
        # No later row can be paid for, so no row starts and the task fails
        # carrying what it and the rows already running recorded.
        return error if error.account_level else None
    except Exception as error:  # noqa: BLE001 - re-raised once running rows drain
        return error
    return None


class _EvaluationSweep:
    """Hold one evaluation batch's outcomes and serialize its progress across lanes."""

    def __init__(self, context: TaskContext, *, total: int) -> None:
        self._context = context
        self.queue = context.queue
        self.task_id = context.task_id
        self.worker_id = context.worker_id
        self._total = total
        self._lock = threading.Lock()
        self._results: dict[int, dict[str, Any]] = {}
        self._skipped: dict[int, dict[str, Any]] = {}
        self._failures: dict[int, dict[str, str]] = {}

    def start(self, payload: dict[str, Any]) -> None:
        """Report that one more row started."""
        self.progress("evaluating", payload)

    def progress(self, stage: str, payload: dict[str, Any]) -> None:
        """Report a row's stage with the batch step and the live tallies."""
        with self._lock:
            try:
                self._step().progress(stage, payload)
            except OperationalError as exc:
                # A busy SQLite writer must not fail paid work; the worker's lease keeper
                # renews the lease on its own thread.
                logger.warning(
                    "evaluation_batch_progress_skipped",
                    extra={
                        "task_id": self.task_id,
                        "stage": stage,
                        "error_type": type(exc).__name__,
                    },
                )

    def cancellation_requested(self) -> bool:
        """Return whether the batch task was asked to cancel."""
        return self._context.cancellation_requested()

    def raise_if_cancelled(self) -> None:
        """Stop at the batch task's cancellation point."""
        self._context.raise_if_cancelled()

    def record(self, position: int, outcome: dict[str, Any]) -> None:
        """Keep one row's outcome at its selection position."""
        with self._lock:
            if outcome.get("skipped") is not None:
                self._skipped[position] = outcome
            else:
                self._results[position] = outcome

    def fail(self, position: int, failure: dict[str, str]) -> None:
        """Keep one row's failure at its selection position."""
        with self._lock:
            self._failures[position] = failure

    def summary(self) -> dict[str, Any]:
        """Return the batch result with every row list in selection order."""
        with self._lock:
            tallies = self._tallies()
            return {
                "selected": self._total,
                "evaluated": tallies["evaluated"],
                "refused": tallies["refused"],
                "skipped": tallies["skipped"],
                "skipped_rows": _in_order(self._skipped),
                "failed": tallies["failed"],
                "failures": _in_order(self._failures),
                "results": _in_order(self._results),
            }

    def _step(self) -> BatchStepContext:
        # Rows finished plus one, so "N of total" never reaches the total while
        # several lanes still run; one lane reports the row's own position.
        finished = len(self._results) + len(self._skipped) + len(self._failures)
        return BatchStepContext(
            self._context,
            current=min(finished + 1, self._total),
            total=self._total,
            so_far=self._tallies(),
        )

    def _tallies(self) -> dict[str, int]:
        refused = sum(1 for outcome in self._results.values() if "refused_by" in outcome)
        return {
            "evaluated": len(self._results) - refused,
            "refused": refused,
            "skipped": len(self._skipped),
            "failed": len(self._failures),
        }


def _in_order[T](by_position: dict[int, T]) -> list[T]:
    return [by_position[position] for position in sorted(by_position)]
