"""Characterize guided-campaign execution: dispatch, capture, admission, completion."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError, OperationalError

from clearscout.application.config import ProviderName, Settings
from clearscout.application.discovery.content_resolution import (
    PostingContentResolutionService,
    ResolvedPostingContent,
)
from clearscout.application.discovery.guided_campaign import (
    DEFAULT_MAX_HOSTED_SEARCHES,
    DEFAULT_MAX_MODEL_CALLS,
    BranchKind,
    CampaignAttemptPlan,
    CampaignBounds,
    CampaignBranch,
    CampaignIdentities,
    CampaignManifest,
    compile_campaign,
)
from clearscout.application.discovery.guided_campaign_ledger import GuidedCampaignLedger
from clearscout.application.discovery.guided_campaign_report import (
    abandon_uncertain_attempt,
    build_campaign_preview,
    campaign_report,
    latest_campaign_report,
)
from clearscout.application.discovery.guided_campaign_request import (
    MAX_SEARCHES,
    MAX_WHY_LENGTH,
    campaign_response_schema,
)
from clearscout.application.discovery.guided_campaign_runner import (
    GuidedCampaignHandler,
    GuidedCampaignRetryHandler,
    GuidedCampaignService,
    captured_url_rows,
    matched_queries,
)
from clearscout.application.discovery.posting_fetch import FetchedPosting
from clearscout.application.discovery.source_reading import PostingCheck, SourceListing, SourceRead
from clearscout.application.failures import ConflictError, NotFoundError, ValidationError
from clearscout.application.master_cv import MasterCvService
from clearscout.application.tasks import TaskQueueService, validate_task_payload
from clearscout.application.tasks.registry import TASK_REGISTRY
from clearscout.application.workspaces import WorkspaceService
from clearscout.application.workspaces.parameters import SearchParametersService
from clearscout.bootstrap.worker import TaskCancelledError, Worker, default_handlers
from clearscout.domain import SearchProposalState, new_id, utc_now
from clearscout.domain.campaigns import AttemptErrorCategory, AttemptState, CampaignStatus
from clearscout.domain.identity import normalize_text
from clearscout.domain.records import (
    PostingContentAccessStatus,
    PostingContentExtractionMethod,
    PostingIdentityStatus,
    PostingReadIntent,
    PostingStatus,
    RobotsReadBasis,
    SourceReadOutcome,
)
from clearscout.domain.search_parameters import (
    Cluster,
    EmployerArchetype,
    Focus,
    JobSearchParameters,
    Location,
    Seniority,
    TargetTitle,
)
from clearscout.domain.statements import derived_proposal_identity
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    BackgroundTask,
    CostRecord,
    DerivedEmployerArchetype,
    DerivedSearchTitle,
    GuidedCampaign,
    GuidedCampaignAttempt,
    GuidedCampaignBranch,
    GuidedCampaignBranchOutcome,
    GuidedCampaignCapturedUrl,
    GuidedCampaignState,
    GuidedCampaignUrlObservation,
    ModelCall,
    ModelCallEnvelope,
    PendingDiscovery,
    Source,
    SourceScanOutcome,
)
from clearscout.infrastructure.providers.base import (
    BackgroundDispatch,
    BackgroundPoll,
    CallRecord,
    Finish,
    PageAction,
    Search,
    SearchSource,
    parse_or_raise,
)
from clearscout.infrastructure.providers.failures import translate
from clearscout.infrastructure.providers.plan import digest_of
from clearscout.shared.costs import CallManifest, ServiceTier, Usage, cents
from clearscout.shared.costs import Ledger as CostLedger
from clearscout.shared.costs.prices import PriceOverride
from clearscout.shared.errors import (
    CapacityError,
    ClearscoutError,
    ProviderError,
    ProviderFailure,
)
from tests.support.artifacts import runtime_storage
from tests.support.doubles import ScriptedProvider, UnexpectedPostingFetcher
from tests.support.scenarios import titles as stated_titles


class _ExplosiveProvider:
    """A background provider that fails the test if the runner ever calls it."""

    name = ProviderName.OPENAI

    def complete(self, plan: Any, schema: Any, choice: Any) -> Any:
        raise AssertionError("complete must not be called")

    def complete_with_web_search(self, plan: Any, schema: Any, choice: Any) -> Any:
        raise AssertionError("complete_with_web_search must not be called")

    def available_models(self) -> frozenset[str] | None:
        raise AssertionError("available_models must not be called")

    def dispatch_background(
        self, plan: Any, schema: Any, choice: Any, *, metadata: Any
    ) -> BackgroundDispatch:
        raise AssertionError("dispatch_background must not be called")

    def poll_background(self, response_id: str, schema: Any, ceiling: int) -> Any:
        raise AssertionError("poll_background must not be called")

    def cancel_background(self, response_id: str) -> None:
        raise AssertionError("cancel_background must not be called")

    def check_credentials(self) -> None:
        return None


def _workspace(database: Database, tmp_path: Path, master_cv: Path) -> str:
    """A workspace with saved search parameters and a stored master CV."""
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database))
        .create(slug="campaign", name="Campaign")
        .id
    )
    SearchParametersService(database).save(
        workspace_id,
        JobSearchParameters(
            seniority=Seniority(target_titles=stated_titles("Staff Data Engineer"))
        ),
    )
    MasterCvService(database, runtime_storage(database, tmp_path / "artifacts")).import_source(
        workspace_id=workspace_id,
        filename="CV_Master.md",
        content=master_cv.read_bytes(),
    )
    return workspace_id


def _service(
    database: Database,
    tmp_path: Path,
    provider: Any = None,
    *,
    sleeper: Callable[[float], None] = time.sleep,
) -> GuidedCampaignService:
    return GuidedCampaignService(
        database,
        storage=runtime_storage(database, tmp_path / "artifacts"),
        posting_fetcher=UnexpectedPostingFetcher(),
        content_resolution=_content_resolution(database, tmp_path),
        provider=provider,
        sleeper=sleeper,
    )


def _compile(database: Database, workspace_id: str, tmp_path: Path) -> CampaignManifest:
    return (
        _service(database, tmp_path)
        ._build_inputs(workspace_id, max_model_calls=4, max_hosted_searches=20)
        .manifest
    )


def test_a_preview_manifest_matches_what_the_runner_recompiles(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path, master_cv)

    with durable_database.session() as session:
        preview = build_campaign_preview(
            session, workspace_id, max_model_calls=4, max_hosted_searches=20
        )

    service = GuidedCampaignService(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path / "artifacts"),
        posting_fetcher=UnexpectedPostingFetcher(),
        content_resolution=_content_resolution(durable_database, tmp_path),
    )
    inputs = service._build_inputs(workspace_id, max_model_calls=4, max_hosted_searches=20)

    assert inputs.manifest.sha256 == preview.manifest.sha256


def test_a_requested_budget_above_the_module_default_is_honoured(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """Cost does not stop a run: the preview states the estimate and the request stands."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)

    inputs = _service(durable_database, tmp_path)._build_inputs(
        workspace_id,
        max_model_calls=DEFAULT_MAX_MODEL_CALLS + 4,
        max_hosted_searches=DEFAULT_MAX_HOSTED_SEARCHES + 20,
    )

    assert inputs.manifest.bounds.max_model_calls == DEFAULT_MAX_MODEL_CALLS + 4
    assert inputs.manifest.bounds.max_hosted_searches == DEFAULT_MAX_HOSTED_SEARCHES + 20


def test_a_workspace_on_a_provider_without_background_search_is_refused_at_preview(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """The refusal comes before approval, not as a failed paid task."""
    workspace_id = str(
        WorkspaceService(
            durable_database,
            runtime_storage(durable_database),
            settings=Settings(provider=ProviderName.ANTHROPIC),
        )
        .create(slug="anthropic-campaign", name="Anthropic campaign")
        .id
    )
    MasterCvService(
        durable_database, runtime_storage(durable_database, tmp_path / "artifacts")
    ).import_source(
        workspace_id=workspace_id, filename="CV_Master.md", content=master_cv.read_bytes()
    )

    with (
        durable_database.session() as session,
        pytest.raises(ValidationError, match="background web search"),
    ):
        build_campaign_preview(session, workspace_id, max_model_calls=4, max_hosted_searches=20)


def test_an_openai_campaign_is_pinned_to_the_flex_tier(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path, master_cv)

    identities = _compile(durable_database, workspace_id, tmp_path).identities

    assert identities.provider == ProviderName.OPENAI.value
    assert identities.service_tier == ServiceTier.FLEX.value


def test_campaign_evidence_carries_accepted_vocabulary_as_branches_and_hard_rejections_as_ruled_out(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    before = _compile(durable_database, workspace_id, tmp_path).identities
    with durable_database.session() as session:
        for title, state, reason in (
            ("Records Programme Lead", SearchProposalState.ACCEPTED, None),
            ("Prompt Engineer", SearchProposalState.REJECTED, None),
            ("Junior Records Clerk", SearchProposalState.REJECTED, "wrong_level"),
            ("Archive Assistant", SearchProposalState.REJECTED, "defer"),
            ("Records Officer", SearchProposalState.REJECTED, "duplicate"),
        ):
            normalized = normalize_text(title)
            session.add(
                DerivedSearchTitle(
                    id=new_id(),
                    workspace_id=workspace_id,
                    master_cv_version_id=before.master_cv_version_id,
                    title=title,
                    normalized_title=normalized,
                    reason="decided",
                    evidence_ids=["evidence-1"],
                    lane=None,
                    position=0,
                    state=state.value,
                    rejection_reason=reason,
                    origin="test",
                    correlation_id=None,
                    carried_identity=derived_proposal_identity(normalized),
                    decided_at=utc_now(),
                )
            )
        session.add(
            DerivedEmployerArchetype(
                id=new_id(),
                workspace_id=workspace_id,
                master_cv_version_id=before.master_cv_version_id,
                name="Example staffing agencies",
                normalized_name=normalize_text("Example staffing agencies"),
                because="decided",
                evidence_ids=["evidence-1"],
                position=0,
                state=SearchProposalState.REJECTED.value,
                rejection_reason="wrong_employer_type",
                origin="test",
                correlation_id=None,
                carried_identity=derived_proposal_identity("example staffing agencies"),
                decided_at=utc_now(),
            )
        )

    inputs = _service(durable_database, tmp_path)._build_inputs(
        workspace_id, max_model_calls=4, max_hosted_searches=20
    )

    assert any(
        "Records Programme Lead" in branch.title_aliases for branch in inputs.manifest.branches
    )
    assert "do not re-propose" not in inputs.evidence_text
    assert "do not propose" not in inputs.evidence_text
    ruled_out = inputs.evidence_text.split("<ruled_out_search_vocabulary>")[1]
    ruled_out = ruled_out.split("</ruled_out_search_vocabulary>")[0]
    assert "Prompt Engineer" in ruled_out
    assert "Junior Records Clerk" in ruled_out
    assert "Example staffing agencies" in ruled_out
    assert "Archive Assistant" not in inputs.evidence_text
    assert "Records Officer" not in inputs.evidence_text
    assert (
        inputs.manifest.identities.accepted_vocabulary_sha256 != before.accepted_vocabulary_sha256
    )


def test_newer_soft_rejections_never_push_a_hard_rejection_out_of_the_ruled_out_block(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    version_id = _compile(durable_database, workspace_id, tmp_path).identities.master_cv_version_id
    cap = Settings().discovery.max_carried_rejections
    hard_decided_at = utc_now() - timedelta(days=1)
    rejected = [("Prompt Engineer", "not_relevant", hard_decided_at)] + [
        (f"Records Assistant {index}", ("defer", "duplicate")[index % 2], utc_now())
        for index in range(cap)
    ]
    with durable_database.session() as session:
        for title, reason, decided_at in rejected:
            normalized = normalize_text(title)
            session.add(
                DerivedSearchTitle(
                    id=new_id(),
                    workspace_id=workspace_id,
                    master_cv_version_id=version_id,
                    title=title,
                    normalized_title=normalized,
                    reason="decided",
                    evidence_ids=["evidence-1"],
                    lane=None,
                    position=0,
                    state=SearchProposalState.REJECTED.value,
                    rejection_reason=reason,
                    origin="test",
                    correlation_id=None,
                    carried_identity=derived_proposal_identity(normalized),
                    decided_at=decided_at,
                )
            )

    inputs = _service(durable_database, tmp_path)._build_inputs(
        workspace_id, max_model_calls=4, max_hosted_searches=20
    )

    ruled_out = inputs.evidence_text.split("<ruled_out_search_vocabulary>")[1]
    assert "Prompt Engineer" in ruled_out.split("</ruled_out_search_vocabulary>")[0]
    assert "Records Assistant" not in inputs.evidence_text


def test_a_ruled_out_title_holding_a_comma_stays_one_entry_in_the_ruled_out_block(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    version_id = _compile(durable_database, workspace_id, tmp_path).identities.master_cv_version_id
    with durable_database.session() as session:
        for title, decided_at in (
            ("Prompt Engineer", utc_now() - timedelta(days=1)),
            ("Records Custodian, Regulated Archives", utc_now()),
        ):
            normalized = normalize_text(title)
            session.add(
                DerivedSearchTitle(
                    id=new_id(),
                    workspace_id=workspace_id,
                    master_cv_version_id=version_id,
                    title=title,
                    normalized_title=normalized,
                    reason="decided",
                    evidence_ids=["evidence-1"],
                    lane=None,
                    position=0,
                    state=SearchProposalState.REJECTED.value,
                    rejection_reason="not_relevant",
                    origin="test",
                    correlation_id=None,
                    carried_identity=derived_proposal_identity(normalized),
                    decided_at=decided_at,
                )
            )

    inputs = _service(durable_database, tmp_path)._build_inputs(
        workspace_id, max_model_calls=4, max_hosted_searches=20
    )

    ruled_out = inputs.evidence_text.split("<ruled_out_search_vocabulary>")[1]
    ruled_out = ruled_out.split("</ruled_out_search_vocabulary>")[0]
    assert "Titles: Records Custodian, Regulated Archives; Prompt Engineer" in ruled_out


def test_a_manifest_mismatch_fails_before_any_dispatch_or_write(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A stale approved manifest sha refuses the run before touching the provider or ledger."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    provider = _ExplosiveProvider()
    service = _service(durable_database, tmp_path, provider)

    with pytest.raises(ConflictError):
        service.run(
            workspace_id,
            campaign_manifest_sha256="0" * 64,
            campaign_cycle=1,
            max_model_calls=4,
            max_hosted_searches=20,
            context=None,
        )

    with durable_database.session() as session:
        created = session.scalar(
            select(GuidedCampaign).where(GuidedCampaign.workspace_id == workspace_id)
        )
    assert created is None


def _background_task(database: Database, workspace_id: str, idempotency_key: str) -> str:
    """Insert the background-task row that campaign and lease columns reference."""
    with database.session() as session:
        task = BackgroundTask(
            workspace_id=workspace_id,
            task_type="search.guided_campaign",
            status="running",
            payload={},
            payload_sha256="0" * 64,
            idempotency_key=idempotency_key,
        )
        session.add(task)
        session.flush()
        return task.id


def _complete_campaign_tasks(database: Database) -> None:
    """Finish every campaign task, which is what puts a run in the cost history."""
    with database.session() as session:
        session.execute(
            sa_update(BackgroundTask)
            .where(BackgroundTask.task_type == "search.guided_campaign")
            .values(status="completed")
        )


@dataclass
class _FakeContext:
    """Minimal task context for a runner test."""

    task_id: str
    queue: Any = None
    worker_id: str = "test-worker"

    def cancellation_requested(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        return None

    def progress(self, stage: str, payload: dict[str, Any]) -> None:
        return None


@dataclass
class _CancellingContext(_FakeContext):
    def cancellation_requested(self) -> bool:
        return True


@dataclass
class _CancelAfterDispatchContext(_FakeContext):
    """Cancellation arrives after the pre-dispatch check, while the response is polled."""

    checks: int = 0

    def cancellation_requested(self) -> bool:
        self.checks += 1
        return self.checks > 1


def test_a_dispatching_attempt_without_a_response_id_becomes_uncertain_and_is_not_redispatched(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A lease recovered mid-dispatch is marked uncertain, never redispatched."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    inputs = _service(durable_database, tmp_path)._build_inputs(
        workspace_id, max_model_calls=4, max_hosted_searches=20
    )
    manifest = inputs.manifest
    ledger = GuidedCampaignLedger(durable_database)
    crashed_task_id = _background_task(durable_database, workspace_id, "crashed")
    campaign_id = ledger.create_or_claim(
        workspace_id, manifest, 1, crashed_task_id, inputs.snapshot()
    )
    first_attempt = manifest.attempts[0]
    # A crash between "about to dispatch" and "got a response id" leaves an
    # attempt exactly here; the earlier task's lease is long gone.
    ledger.mark_dispatching(campaign_id, first_attempt.position, crashed_task_id, "a" * 64)

    recovering_task_id = _background_task(durable_database, workspace_id, "recovering")
    provider = _ExplosiveProvider()
    service = _service(durable_database, tmp_path, provider)
    result = service.run(
        workspace_id,
        campaign_manifest_sha256=manifest.sha256,
        campaign_cycle=1,
        max_model_calls=4,
        max_hosted_searches=20,
        context=_FakeContext(task_id=recovering_task_id),
    )

    resumed = next(item for item in ledger.attempts(campaign_id) if item.position == 1)
    assert resumed.state == "uncertain"
    assert result["reasons"] == ["uncertain_attempt"]


def _raw_attempt_update(database: Database, campaign_id: str, position: int, state: str) -> None:
    """Write an attempt's state with a bare UPDATE, bypassing every ledger method.

    Exercises the database trigger directly: no application code stands
    between this write and the schema's own transition guard.
    """
    with database.session() as session:
        session.connection().execute(
            sa_update(GuidedCampaignAttempt)
            .where(
                GuidedCampaignAttempt.campaign_id == campaign_id,
                GuidedCampaignAttempt.position == position,
            )
            .values(state=state)
        )


def test_an_uncertain_attempt_cannot_be_moved_to_dispatching(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """The schema's own trigger refuses any transition out of uncertain but abandoned."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    ledger = GuidedCampaignLedger(durable_database)
    task_id = _background_task(durable_database, workspace_id, "owner")
    campaign_id = ledger.create_or_claim(workspace_id, manifest, 1, task_id, {})
    position = manifest.attempts[0].position
    ledger.mark_dispatching(campaign_id, position, task_id, "a" * 64)
    ledger.mark_uncertain(campaign_id, position, task_id, "boom")

    with pytest.raises(IntegrityError):
        _raw_attempt_update(durable_database, campaign_id, position, "dispatching")


def test_a_processed_attempt_cannot_change(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """The schema's own trigger refuses any transition out of processed."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    ledger = GuidedCampaignLedger(durable_database)
    task_id = _background_task(durable_database, workspace_id, "owner")
    campaign_id = ledger.create_or_claim(workspace_id, manifest, 1, task_id, {})
    position = manifest.attempts[0].position
    ledger.mark_dispatching(campaign_id, position, task_id, "a" * 64)
    ledger.mark_dispatched(
        campaign_id, position, task_id, "resp_1", "a" * 64, request_envelope=None
    )
    cost_ledger = CostLedger()
    cost_ledger.record(CallManifest(stage="guided_campaign", model="gpt-5.6-terra", usage=Usage()))
    ledger.capture(
        campaign_id,
        position,
        task_id,
        workspace_id=workspace_id,
        poll=BackgroundPoll(
            status="complete",
            value=None,
            record=CallRecord(usage=Usage(input_tokens=10, output_tokens=5)),
        ),
        ledger=cost_ledger,
        provider="openai",
        branch_queries={},
        has_unattributable_search=False,
        captured_urls=(),
    )
    with durable_database.session() as session:
        ledger.mark_processed_in_session(session, campaign_id, position, task_id)

    with pytest.raises(IntegrityError):
        _raw_attempt_update(durable_database, campaign_id, position, "uncertain")


def test_a_stale_task_cannot_record_admissions_after_losing_the_lease(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """Staging from a task whose lease moved on is refused inside its own transaction."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    ledger = GuidedCampaignLedger(durable_database)
    stale_task_id = _background_task(durable_database, workspace_id, "stale")
    campaign_id = ledger.create_or_claim(workspace_id, manifest, 1, stale_task_id, {})
    position = manifest.attempts[0].position
    newer_task_id = _background_task(durable_database, workspace_id, "newer")
    with durable_database.session() as session:
        session.execute(
            sa_update(GuidedCampaignState)
            .where(GuidedCampaignState.campaign_id == campaign_id)
            .values(owner_task_id=newer_task_id)
        )

    with durable_database.session() as session, pytest.raises(ConflictError):
        ledger.record_admissions_in_session(session, campaign_id, position, stale_task_id, ())


def test_capture_writes_both_the_refused_pick_count_and_its_entries(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """The capture blob keeps the derived count alongside the url/reason entries it counts."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    ledger = GuidedCampaignLedger(durable_database)
    task_id = _background_task(durable_database, workspace_id, "owner")
    campaign_id = ledger.create_or_claim(workspace_id, manifest, 1, task_id, {})
    position = manifest.attempts[0].position
    ledger.mark_dispatching(campaign_id, position, task_id, "a" * 64)
    ledger.mark_dispatched(
        campaign_id, position, task_id, "resp_1", "a" * 64, request_envelope=None
    )
    refused = ({"url": "http://127.0.0.1/x", "reason": "not_a_hostname"},)
    cost_ledger = CostLedger()
    cost_ledger.record(CallManifest(stage="guided_campaign", model="gpt-5.6-terra", usage=Usage()))

    ledger.capture(
        campaign_id,
        position,
        task_id,
        workspace_id=workspace_id,
        poll=BackgroundPoll(
            status="complete",
            value=None,
            record=CallRecord(usage=Usage(input_tokens=10, output_tokens=5)),
        ),
        ledger=cost_ledger,
        provider="openai",
        branch_queries={},
        has_unattributable_search=False,
        captured_urls=(),
        refused_model_picks=refused,
    )

    with durable_database.session() as session:
        attempt = session.scalar(
            select(GuidedCampaignAttempt).where(
                GuidedCampaignAttempt.campaign_id == campaign_id,
                GuidedCampaignAttempt.position == position,
            )
        )
    assert attempt is not None
    assert attempt.capture["invalid_model_urls"] == 1
    assert attempt.capture["refused_model_picks"] == list(refused)


def test_a_polled_body_is_stored_once_beside_the_model_call_it_belongs_to(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """The attempt names the model call whose envelope holds the provider's own body."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    ledger = GuidedCampaignLedger(durable_database)
    task_id = _background_task(durable_database, workspace_id, "owner")
    campaign_id = ledger.create_or_claim(workspace_id, manifest, 1, task_id, {})
    position = manifest.attempts[0].position
    ledger.mark_dispatching(campaign_id, position, task_id, "a" * 64)
    ledger.mark_dispatched(
        campaign_id, position, task_id, "resp_1", "a" * 64, request_envelope=None
    )
    body = {"id": "resp_1", "status": "completed", "output": [{"type": "reasoning"}]}
    cost_ledger = CostLedger()
    cost_ledger.record(
        CallManifest(
            stage="guided_campaign",
            model="gpt-5.6-terra",
            usage=Usage(),
            response_envelope=body,
        )
    )

    ledger.capture(
        campaign_id,
        position,
        task_id,
        workspace_id=workspace_id,
        poll=BackgroundPoll(
            status="complete",
            value=None,
            record=CallRecord(
                usage=Usage(input_tokens=10, output_tokens=5), response_envelope=body
            ),
            raw=body,
        ),
        ledger=cost_ledger,
        provider="openai",
        branch_queries={},
        has_unattributable_search=False,
        captured_urls=(),
    )

    with durable_database.session() as session:
        attempt = session.scalar(
            select(GuidedCampaignAttempt).where(
                GuidedCampaignAttempt.campaign_id == campaign_id,
                GuidedCampaignAttempt.position == position,
            )
        )
        assert attempt is not None
        stored = session.scalar(
            select(ModelCallEnvelope.response_envelope).where(
                ModelCallEnvelope.model_call_id == attempt.model_call_id
            )
        )
        keys = sorted(attempt.capture)

    assert stored == body
    assert keys == [
        "board_url_sha256s",
        "branch_outcomes",
        "invalid_model_urls",
        "refused_model_picks",
        "selected_url_sha256s",
    ]


def _finished_poll(record: CallRecord, *picked_posting_urls: str) -> BackgroundPoll:
    """Return a completed attempt whose answer picks these posting URLs, or answers nothing."""
    value = campaign_response_schema(()).model_validate(
        {"postings": [{"url": url, "why": "looks relevant"} for url in picked_posting_urls]}
    )
    return BackgroundPoll(
        status="complete", value=value if picked_posting_urls else None, record=record
    )


def test_an_over_length_why_is_clipped_so_the_answer_keeps_its_pick() -> None:
    """The provider need not honour the why bound; the read clips it and keeps the answer."""
    posting_url = "https://jobs.example.test/openings/77"
    schema = campaign_response_schema(("a" * 64,))
    answer = json.dumps(
        {"postings": [{"url": posting_url, "branch_keys": ["a" * 64], "why": "w" * 310}]}
    )

    value = parse_or_raise(schema, answer, "OpenAI")
    rows, refused_picks = captured_url_rows(
        BackgroundPoll(status="complete", value=value, record=CallRecord(usage=Usage()))
    )
    [row] = rows

    assert refused_picks == ()
    assert (row["url"], row["model_selected"]) == (posting_url, True)
    assert row["model_why"] == "w" * MAX_WHY_LENGTH
    why_schema = schema.model_json_schema()["$defs"]["ConstrainedCampaignUrlReport"]
    assert why_schema["properties"]["why"]["maxLength"] == MAX_WHY_LENGTH


def test_an_over_long_searches_list_is_clipped_so_the_answer_keeps_its_pick() -> None:
    """The provider need not honour the searches bound; the read clips it and keeps the answer."""
    posting_url = "https://jobs.example.test/openings/78"
    schema = campaign_response_schema(("a" * 64,))
    answer = json.dumps(
        {
            "postings": [{"url": posting_url, "branch_keys": ["a" * 64], "why": "fits"}],
            "searches": [
                {"query": f"records lead query {index}", "branch_keys": ["a" * 64]}
                for index in range(MAX_SEARCHES + 5)
            ],
        }
    )

    value = parse_or_raise(schema, answer, "OpenAI")
    rows, _ = captured_url_rows(
        BackgroundPoll(status="complete", value=value, record=CallRecord(usage=Usage()))
    )

    assert [row["url"] for row in rows] == [posting_url]
    assert [item["query"] for item in value.model_dump()["searches"]] == [
        f"records lead query {index}" for index in range(MAX_SEARCHES)
    ]
    assert schema.model_json_schema()["properties"]["searches"]["maxItems"] == MAX_SEARCHES


def test_an_unparseable_model_written_url_is_dropped_and_recorded_with_its_reason() -> None:
    """A malformed model-picked URL never blocks capture; it is dropped and its reason kept."""
    poll = _finished_poll(CallRecord(usage=Usage()), "http://[broken/jobs/1")

    rows, refused_picks = captured_url_rows(poll)

    assert rows == ()
    assert refused_picks == ({"url": "http://[broken/jobs/1", "reason": "unreadable_url"},)


def test_an_unparseable_search_source_url_is_kept_with_a_fallback_key() -> None:
    """A malformed search-source URL is still captured, keyed by its own raw digest."""
    poll = _finished_poll(
        CallRecord(
            usage=Usage(),
            searches=(
                Search(
                    queries=("Staff Data Engineer jobs",),
                    sources=(SearchSource(url="http://[broken/jobs/1"),),
                ),
            ),
        )
    )

    rows, refused_picks = captured_url_rows(poll)
    [row] = rows

    assert refused_picks == ()
    assert row["url"] == "http://[broken/jobs/1"
    assert row["observations"] == [
        {"action_kind": "search_source", "query": "Staff Data Engineer jobs"}
    ]


def test_a_model_selection_matches_a_captured_url_by_posting_fingerprint() -> None:
    """An apply-path variant marks the captured posting URL as model-selected."""
    captured_url = "https://jobs.example.test/openings/42"
    poll = _finished_poll(
        CallRecord(
            usage=Usage(),
            searches=(
                Search(
                    queries=("Staff Data Engineer jobs",),
                    sources=(SearchSource(url=captured_url, title="Staff Data Engineer"),),
                ),
            ),
        ),
        f"{captured_url}/apply",
    )

    rows, refused_picks = captured_url_rows(poll)
    [row] = rows

    assert refused_picks == ()
    assert row["url"] == captured_url
    assert row["model_selected"] is True
    assert row["observations"] == [
        {"action_kind": "search_source", "query": "Staff Data Engineer jobs"}
    ]


def test_a_model_written_url_is_recorded_as_an_unresolved_observation() -> None:
    """A URL with no source evidence is retained for the report, never as capture evidence."""
    poll = _finished_poll(CallRecord(usage=Usage()), "https://jobs.example.test/phantom")

    rows, refused_picks = captured_url_rows(poll)
    [row] = rows

    assert refused_picks == ()
    assert row["model_selected"] is True
    assert row["observations"] == [{"action_kind": "model_written", "query": ""}]


def test_a_non_fetchable_model_pick_is_dropped_while_a_refused_observation_is_kept() -> None:
    """Policy refusal drops and counts a model pick, but never a search or page-action URL."""
    refused_source_url = "javascript:void(sourcelisting)"
    refused_page_url = "ftp://pages.example.test/listing"
    good_posting_url = "https://jobs.example.test/openings/42"
    poll = _finished_poll(
        CallRecord(
            usage=Usage(),
            searches=(
                Search(
                    queries=("Staff Data Engineer jobs",),
                    sources=(SearchSource(url=refused_source_url),),
                ),
            ),
            page_actions=(PageAction(kind="open_page", url=refused_page_url),),
        ),
        "not a url",
        "javascript:alert(1)",
        "ftp://x.example/a",
        good_posting_url,
    )

    rows, refused_picks = captured_url_rows(poll)
    by_url = {row["url"]: row for row in rows}

    assert len(refused_picks) == 3
    assert set(by_url) == {refused_source_url, refused_page_url, good_posting_url}
    assert by_url[good_posting_url]["model_selected"] is True
    assert "model_selected" not in by_url[refused_source_url]
    assert "model_selected" not in by_url[refused_page_url]


def test_a_refused_model_pick_keeps_its_url_and_distinct_reason() -> None:
    """A refused pick worth seeing after a paid run names its own URL, not just a count."""
    loopback_url = "http://127.0.0.1/x"
    credentialed_url = "https://user:pw@example.test/j"
    poll = _finished_poll(CallRecord(usage=Usage()), loopback_url, credentialed_url, "not a url")

    rows, refused_picks = captured_url_rows(poll)
    by_url = {item["url"]: item["reason"] for item in refused_picks}

    assert rows == ()
    assert by_url[loopback_url] == "not_a_hostname"
    assert by_url[credentialed_url] == "credentials_in_url"
    assert by_url["not a url"] == "not_an_http_url"


def test_a_schemeless_pick_still_selects_the_captured_row_it_names() -> None:
    """A pick the capture already saw marks that row, so a real posting is not de-selected."""
    posting_url = "https://jobs.example.test/openings/42"
    poll = _finished_poll(
        CallRecord(
            usage=Usage(),
            searches=(
                Search(
                    queries=("Staff Data Engineer jobs",),
                    sources=(SearchSource(url=posting_url),),
                ),
            ),
        ),
        "jobs.example.test/openings/42",
    )

    rows, refused_picks = captured_url_rows(poll)

    assert refused_picks == ()
    [row] = rows
    assert row["url"] == posting_url
    assert row["model_selected"] is True


def test_a_model_written_url_is_never_fetched_and_remains_unresolved(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """The report retains a model-only URL without treating it as search evidence."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    schema = campaign_response_schema(tuple(branch.key for branch in manifest.branches))
    value = schema.model_validate(
        {
            "postings": [
                {
                    "url": "https://jobs.example.test/phantom",
                    "branch_keys": [manifest.branches[0].key],
                    "why": "looks relevant",
                }
            ],
            "boards": [],
        }
    )
    provider = ScriptedProvider(
        script={},
        background_answers=[
            BackgroundPoll(status="complete", value=value, record=CallRecord(usage=Usage()))
        ],
    )
    task_id = _background_task(durable_database, workspace_id, "model-only")

    _service(durable_database, tmp_path, provider).run(
        workspace_id,
        campaign_manifest_sha256=manifest.sha256,
        campaign_cycle=1,
        max_model_calls=4,
        max_hosted_searches=20,
        context=_FakeContext(task_id=task_id),
    )

    with durable_database.session() as session:
        [captured] = session.scalars(select(GuidedCampaignCapturedUrl)).all()
        [observation] = session.scalars(select(GuidedCampaignUrlObservation)).all()
    assert captured.outcome == "unresolved"
    assert captured.detail == "not in captured search output"
    assert observation.action_kind == "model_written"


def test_only_captured_queries_credit_branches() -> None:
    """A model claim cannot credit a branch whose anchored query was not captured."""
    identities = CampaignIdentities(
        search_parameter_set_id="x",
        search_parameter_sha256="y" * 64,
        master_cv_version_id="z",
        master_cv_sha256="a" * 64,
        accepted_vocabulary_sha256="",
        admission_scope_sha256="b" * 64,
    )
    bounds = CampaignBounds(max_model_calls=4, max_hosted_searches=20)
    titles = [TargetTitle(title="Staff Data Engineer", lane=None)]
    archetypes = [EmployerArchetype(name="Fintech scaleups", because="matches background")]
    manifest = compile_campaign(
        JobSearchParameters(seniority=Seniority(target_titles=titles)),
        titles,
        archetypes,
        identities=identities,
        bounds=bounds,
    )
    assert len(manifest.branches) == 2
    assert len(manifest.attempts) == 1
    attempt_plan = manifest.attempts[0]
    assert set(attempt_plan.branch_keys) == {branch.key for branch in manifest.branches}
    branch_a, branch_b = manifest.branches

    poll = _finished_poll(
        CallRecord(
            usage=Usage(),
            searches=(Search(queries=(f"{branch_a.anchors[0]} openings",), sources=()),),
        )
    )

    matched = matched_queries(manifest, attempt_plan, poll)

    assert matched[branch_a.key] == (f"{branch_a.anchors[0]} openings",)
    assert matched[branch_b.key] == ()


class _FlakyPostingFetcher:
    """Fails the first fetch, then answers normally; models one admission retry."""

    def __init__(self) -> None:
        self.calls = 0

    def fetch(
        self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL, board_url: str = ""
    ) -> FetchedPosting:
        del intent, board_url
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("simulated admission-layer failure")
        return FetchedPosting(
            text="We are hiring a Staff Data Engineer.",
            posting_status="active",
            method="test",
            fetched_at=utc_now(),
            title="Staff Data Engineer",
            employer="Example Co",
        )


class _CountingPostingFetcher:
    """Returns one readable posting and counts how often campaign processing reads it."""

    def __init__(self) -> None:
        self.calls = 0

    def fetch(
        self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL, board_url: str = ""
    ) -> FetchedPosting:
        del intent, board_url
        self.calls += 1
        return FetchedPosting(
            text="We are hiring a Staff Data Engineer.",
            posting_status="active",
            method="test",
            fetched_at=utc_now(),
            title="Staff Data Engineer",
            employer="Example Co",
            robots_basis=RobotsReadBasis.SINGLE_POSTING_READ,
        )


class _ReadablePublisherPostingFetcher:
    """Supplies a publisher-verified body that campaign staging must retain."""

    def fetch(
        self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL, board_url: str = ""
    ) -> FetchedPosting:
        del intent, board_url
        return FetchedPosting(
            text="Staff Data Engineer\nBuild reliable data systems.",
            posting_status="active",
            method="test",
            fetched_at=utc_now(),
            title="Staff Data Engineer",
            employer="Example Co",
            final_url=url,
            publisher_check=PostingCheck(
                status="readable",
                note="fixture",
                platform="greenhouse",
                title="Staff Data Engineer",
                employer="Example Co",
            ),
        )


class _UnusedContentResolver:
    """The campaign must retain its prefetched body without a second network read."""

    def resolve(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("campaign retention must reuse the publisher fetch")


class _RetryContentResolver:
    """Supplies a readable verified body for every automatic posting read."""

    def resolve(
        self,
        url: str,
        *,
        employer: str,
        title: str,
        intent: PostingReadIntent = PostingReadIntent.CRAWL,
        board_url: str = "",
    ) -> ResolvedPostingContent:
        del intent
        return ResolvedPostingContent(
            requested_url=url,
            final_url=url,
            content_status=PostingContentAccessStatus.READABLE,
            identity_status=PostingIdentityStatus.VERIFIED,
            posting_status=PostingStatus.ACTIVE,
            extraction_method=PostingContentExtractionMethod.STATIC_HTML,
            fetched_at=utc_now(),
            content=b"Staff Data Engineer\nBuild reliable data systems.",
            publisher_markup_present=True,
            identity_evidence={"publisher": True, "employer": employer, "title": title},
            note="fixture",
        )


def _content_resolution(database: Database, tmp_path: Path) -> PostingContentResolutionService:
    return PostingContentResolutionService(
        database,
        _RetryContentResolver(),
        storage=runtime_storage(database, tmp_path / "artifacts"),
    )


def test_captured_then_an_admission_failure_resumes_without_a_second_dispatch(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A captured attempt an admission error leaves unprocessed resumes without redispatch.

    Exactly one ModelCall row is written for the one captured attempt,
    across both run() calls.
    """
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    branch_key = manifest.branches[0].key
    posting_url = "https://jobs.example.test/staff-data-engineer"
    schema = campaign_response_schema(tuple(branch.key for branch in manifest.branches))
    value = schema.model_validate(
        {
            "postings": [
                {"url": posting_url, "branch_keys": [branch_key], "why": "matches profile"}
            ],
            "boards": [],
        }
    )
    poll = BackgroundPoll(
        status="complete",
        value=value,
        record=CallRecord(
            usage=Usage(input_tokens=10, output_tokens=5),
            search_count=1,
            finish=Finish.COMPLETE,
            # A real search source, not just the model's own pick, so this URL
            # is genuinely captured and reaches the fetch step.
            searches=(
                Search(
                    queries=("Staff Data Engineer jobs",),
                    sources=(SearchSource(url=posting_url, title="Staff Data Engineer"),),
                ),
            ),
        ),
        raw={},
    )
    fetcher = _FlakyPostingFetcher()
    provider = ScriptedProvider(script={}, background_answers=[poll])
    service = GuidedCampaignService(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path / "artifacts"),
        posting_fetcher=fetcher,
        content_resolution=_content_resolution(durable_database, tmp_path),
        provider=provider,
    )
    first_task_id = _background_task(durable_database, workspace_id, "first")

    with pytest.raises(RuntimeError):
        service.run(
            workspace_id,
            campaign_manifest_sha256=manifest.sha256,
            campaign_cycle=1,
            max_model_calls=4,
            max_hosted_searches=20,
            context=_FakeContext(task_id=first_task_id),
        )

    with durable_database.session() as session:
        campaign = session.scalar(
            select(GuidedCampaign).where(
                GuidedCampaign.workspace_id == workspace_id,
                GuidedCampaign.manifest_sha256 == manifest.sha256,
            )
        )
    assert campaign is not None
    campaign_id = campaign.id
    ledger = GuidedCampaignLedger(durable_database)
    position = manifest.attempts[0].position
    mid_run = next(item for item in ledger.attempts(campaign_id) if item.position == position)
    assert mid_run.state == "captured"

    second_task_id = _background_task(durable_database, workspace_id, "second")
    service.run(
        workspace_id,
        campaign_manifest_sha256=manifest.sha256,
        campaign_cycle=1,
        max_model_calls=4,
        max_hosted_searches=20,
        context=_FakeContext(task_id=second_task_id),
    )

    final = next(item for item in ledger.attempts(campaign_id) if item.position == position)
    assert final.state == "processed"
    assert len(provider.background_dispatch_calls) == 1

    with durable_database.session() as session:
        calls = session.scalars(
            select(ModelCall).where(ModelCall.workspace_id == workspace_id)
        ).all()
    assert len(calls) == 1
    assert final.model_call_id == calls[0].id
    with durable_database.session() as session:
        captured = session.scalar(
            select(GuidedCampaignCapturedUrl).where(
                GuidedCampaignCapturedUrl.campaign_id == campaign_id
            )
        )
    assert captured is not None
    assert captured.outcome == "staged"
    assert captured.pending_discovery_id is not None
    with durable_database.session() as session:
        report = latest_campaign_report(session, workspace_id)
    assert report is not None
    assert report.findings[0].branch_labels == (manifest.branches[0].label,)


def test_a_finding_an_attributed_query_surfaced_carries_its_floorless_branch_label(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            seniority=Seniority(target_titles=stated_titles("Staff Data Engineer")),
            focus=Focus(capabilities=[Cluster(name="records custody")]),
        ),
    )
    manifest = _compile(durable_database, workspace_id, tmp_path)
    floorless = next(branch for branch in manifest.branches if not branch.floor_query)
    assert floorless.key in manifest.attempts[0].branch_keys
    posting_url = "https://jobs.example.test/openings/records-lead"
    query = "information governance lead openings"
    schema = campaign_response_schema(tuple(branch.key for branch in manifest.branches))
    poll = BackgroundPoll(
        status="complete",
        value=schema.model_validate(
            {
                "postings": [{"url": posting_url, "branch_keys": [floorless.key], "why": "fits"}],
                "searches": [{"query": query, "branch_keys": [floorless.key]}],
            }
        ),
        record=CallRecord(
            usage=Usage(),
            search_count=1,
            finish=Finish.COMPLETE,
            searches=(
                Search(queries=(query,), sources=(SearchSource(url=posting_url, title="Lead"),)),
            ),
        ),
        raw={},
    )
    provider = ScriptedProvider(script={}, background_answers=[poll])

    _run(
        durable_database,
        tmp_path,
        workspace_id,
        manifest,
        provider,
        key="attributed",
        posting_fetcher=_CountingPostingFetcher(),
    )

    with durable_database.session() as session:
        report = latest_campaign_report(session, workspace_id)
    assert report is not None
    row = next(item for item in report.branches if item.label == floorless.label)
    assert (row.outcome.value, row.matched_queries) == ("searched", (query,))
    assert [finding.branch_labels for finding in report.findings] == [(floorless.label,)]


def test_two_captured_attempts_write_one_model_call_each(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """Each completed response writes exactly one model call and cost record."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            seniority=Seniority(
                target_titles=stated_titles(
                    "Staff Data Engineer",
                    "Data Platform Architect",
                    "Technical Product Lead",
                    "Data Governance Manager",
                )
            )
        ),
    )
    manifest = _compile(durable_database, workspace_id, tmp_path)
    assert len(manifest.attempts) == 2
    schema = campaign_response_schema(tuple(branch.key for branch in manifest.branches))
    answer = schema.model_validate({"postings": [], "boards": []})
    provider = ScriptedProvider(
        script={},
        background_answers=[
            BackgroundPoll(
                status="complete",
                value=answer,
                record=CallRecord(usage=Usage(), response_id="response-one"),
            ),
            BackgroundPoll(
                status="complete",
                value=answer,
                record=CallRecord(usage=Usage(), response_id="response-two"),
            ),
        ],
    )
    task_id = _background_task(durable_database, workspace_id, "two-captures")

    _service(durable_database, tmp_path, provider).run(
        workspace_id,
        campaign_manifest_sha256=manifest.sha256,
        campaign_cycle=1,
        max_model_calls=4,
        max_hosted_searches=20,
        context=_FakeContext(task_id=task_id),
    )

    with durable_database.session() as session:
        calls = session.scalars(
            select(ModelCall)
            .where(ModelCall.workspace_id == workspace_id)
            .order_by(ModelCall.provider_response_id)
        ).all()
    assert [call.provider_response_id for call in calls] == ["response-one", "response-two"]


def test_a_url_captured_by_two_attempts_is_read_once_and_keeps_both_observations(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """Repeated evidence appends provenance without a duplicate capture or publisher read."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            seniority=Seniority(
                target_titles=stated_titles(
                    "Staff Data Engineer",
                    "Data Platform Architect",
                    "Technical Product Lead",
                    "Data Governance Manager",
                )
            )
        ),
    )
    manifest = _compile(durable_database, workspace_id, tmp_path)
    assert len(manifest.attempts) == 2
    posting_url = "https://jobs.example.test/jobs/staff-data-engineer"
    schema = campaign_response_schema(tuple(branch.key for branch in manifest.branches))
    answer = schema.model_validate({"postings": [], "boards": []})
    first_poll = BackgroundPoll(
        status="complete",
        value=answer,
        record=CallRecord(
            usage=Usage(),
            searches=(
                Search(
                    queries=("Staff Data Engineer jobs",),
                    sources=(SearchSource(url=posting_url, title="Staff Data Engineer"),),
                ),
            ),
        ),
    )
    second_poll = BackgroundPoll(
        status="complete",
        value=answer,
        record=CallRecord(
            usage=Usage(),
            searches=(
                Search(
                    queries=("Staff Data Engineer jobs",),
                    sources=(
                        SearchSource(url=f"{posting_url}/apply", title="Staff Data Engineer"),
                    ),
                ),
            ),
        ),
    )
    provider = ScriptedProvider(script={}, background_answers=[first_poll, second_poll])
    fetcher = _CountingPostingFetcher()
    service = GuidedCampaignService(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path / "artifacts"),
        posting_fetcher=fetcher,
        content_resolution=_content_resolution(durable_database, tmp_path),
        provider=provider,
    )
    task_id = _background_task(durable_database, workspace_id, "repeated-url")

    service.run(
        workspace_id,
        campaign_manifest_sha256=manifest.sha256,
        campaign_cycle=1,
        max_model_calls=4,
        max_hosted_searches=20,
        context=_FakeContext(task_id=task_id),
    )

    with durable_database.session() as session:
        captured = session.scalars(
            select(GuidedCampaignCapturedUrl).where(
                GuidedCampaignCapturedUrl.campaign_id
                == session.scalar(
                    select(GuidedCampaign.id).where(GuidedCampaign.workspace_id == workspace_id)
                )
            )
        ).all()
        observations = session.scalars(select(GuidedCampaignUrlObservation)).all()
    assert len(captured) == 1
    assert len(observations) == 2
    assert fetcher.calls == 1
    with durable_database.session() as session:
        report = latest_campaign_report(session, workspace_id)
    assert report is not None
    assert [attempt.captured_not_selected for attempt in report.attempts] == [1, 1]


def test_a_poll_timeout_resumes_the_same_response_without_a_second_dispatch(
    durable_database: Database, tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poll deadline retries the stored response ID rather than abandoning billed usage."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    initial_inputs = _service(durable_database, tmp_path)._build_inputs(
        workspace_id, max_model_calls=4, max_hosted_searches=20
    )
    manifest = initial_inputs.manifest
    evidence_digest = digest_of(initial_inputs.evidence_text)
    assert len(manifest.attempts) == 1
    monkeypatch.setattr(
        "clearscout.application.discovery.guided_campaign_runner.MAX_POLL_ATTEMPTS", 1
    )
    schema = campaign_response_schema(tuple(branch.key for branch in manifest.branches))
    provider = ScriptedProvider(
        script={},
        background_answers=[
            [
                BackgroundPoll(status="pending"),
                BackgroundPoll(
                    status="complete",
                    value=schema.model_validate({"postings": [], "boards": []}),
                    record=CallRecord(usage=Usage(), response_id="delayed-response"),
                ),
            ]
        ],
    )
    service = GuidedCampaignService(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path / "artifacts"),
        posting_fetcher=UnexpectedPostingFetcher(),
        content_resolution=_content_resolution(durable_database, tmp_path),
        provider=provider,
        sleeper=lambda _: None,
    )
    first_task_id = _background_task(durable_database, workspace_id, "poll-timeout")

    with pytest.raises(ProviderError, match="did not reach a terminal state"):
        service.run(
            workspace_id,
            campaign_manifest_sha256=manifest.sha256,
            campaign_cycle=1,
            max_model_calls=4,
            max_hosted_searches=20,
            context=_FakeContext(task_id=first_task_id),
        )

    assert len(provider.background_dispatch_calls) == 1
    with durable_database.session() as session:
        campaign = session.scalar(
            select(GuidedCampaign).where(GuidedCampaign.workspace_id == workspace_id)
        )
    assert campaign is not None
    attempts = GuidedCampaignLedger(durable_database).attempts(campaign.id)
    assert [attempt.state for attempt in attempts] == ["dispatched"]
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            seniority=Seniority(target_titles=stated_titles("Data Governance Manager"))
        ),
    )

    second_task_id = _background_task(durable_database, workspace_id, "poll-resume")
    service.run(
        workspace_id,
        campaign_manifest_sha256=manifest.sha256,
        campaign_cycle=1,
        max_model_calls=4,
        max_hosted_searches=20,
        context=_FakeContext(task_id=second_task_id),
    )

    assert len(provider.background_dispatch_calls) == 1
    with durable_database.session() as session:
        calls = session.scalars(
            select(ModelCall).where(ModelCall.workspace_id == workspace_id)
        ).all()
        costs = session.scalars(
            select(CostRecord).join(ModelCall).where(ModelCall.workspace_id == workspace_id)
        ).all()
    assert [call.provider_response_id for call in calls] == ["delayed-response"]
    assert calls[0].evidence_digest == evidence_digest
    assert len(costs) == 1


def _dispatch_and_time_out(
    database: Database,
    tmp_path: Path,
    master_cv: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: dict[str, Any],
) -> tuple[str, CampaignManifest, ScriptedProvider, str]:
    """Dispatch one attempt whose first run stops polling, leaving it dispatched."""
    workspace_id = _workspace(database, tmp_path, master_cv)
    manifest = _compile(database, workspace_id, tmp_path)
    monkeypatch.setattr(
        "clearscout.application.discovery.guided_campaign_runner.MAX_POLL_ATTEMPTS", 1
    )
    schema = campaign_response_schema(tuple(branch.key for branch in manifest.branches))
    provider = ScriptedProvider(
        script={},
        background_answers=[
            [
                BackgroundPoll(status="pending"),
                BackgroundPoll(
                    status="complete",
                    value=schema.model_validate({"postings": [], "boards": []}),
                    record=CallRecord(
                        usage=Usage(), response_id="resp_scripted_0", response_envelope=body
                    ),
                    raw=body,
                ),
            ]
        ],
    )
    with pytest.raises(ProviderError, match="did not reach a terminal state"):
        _run(database, tmp_path, workspace_id, manifest, provider, key="dispatch")
    with database.session() as session:
        campaign_id = session.scalar(
            select(GuidedCampaign.id).where(GuidedCampaign.workspace_id == workspace_id)
        )
    assert campaign_id is not None
    return workspace_id, manifest, provider, campaign_id


def _captured_envelopes(database: Database) -> tuple[GuidedCampaignAttempt, ModelCallEnvelope]:
    with database.session() as session:
        attempt = session.scalar(select(GuidedCampaignAttempt))
        assert attempt is not None
        envelope = session.scalar(
            select(ModelCallEnvelope).where(
                ModelCallEnvelope.model_call_id == attempt.model_call_id
            )
        )
    assert envelope is not None
    return attempt, envelope


def test_a_background_call_keeps_the_request_it_dispatched_beside_the_response(
    durable_database: Database, tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A capture in a later task still records the request, which only the dispatch saw."""
    body = {"id": "resp_scripted_0", "status": "completed", "output": []}
    workspace_id, manifest, provider, campaign_id = _dispatch_and_time_out(
        durable_database, tmp_path, master_cv, monkeypatch, body
    )
    sent = provider.background_dispatches[0].request_envelope
    assert sent is not None
    [held] = GuidedCampaignLedger(durable_database).attempts(campaign_id)
    assert held.request_envelope == sent

    _run(durable_database, tmp_path, workspace_id, manifest, provider, key="capture")

    attempt, envelope = _captured_envelopes(durable_database)
    assert envelope.request_envelope == sent
    assert envelope.response_envelope == body
    assert attempt.state == "processed"
    assert attempt.request_envelope is None


def test_a_dispatched_attempt_holding_no_request_still_captures_and_processes(
    durable_database: Database, tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An attempt dispatched before requests were held keeps only the response."""
    body = {"id": "resp_scripted_0", "status": "completed", "output": []}
    workspace_id, manifest, provider, campaign_id = _dispatch_and_time_out(
        durable_database, tmp_path, master_cv, monkeypatch, body
    )
    with durable_database.session() as session:
        session.execute(
            sa_update(GuidedCampaignAttempt)
            .where(GuidedCampaignAttempt.campaign_id == campaign_id)
            .values(request_envelope=None)
        )

    _run(durable_database, tmp_path, workspace_id, manifest, provider, key="capture")

    attempt, envelope = _captured_envelopes(durable_database)
    assert envelope.request_envelope is None
    assert envelope.response_envelope == body
    assert attempt.state == "processed"


def test_a_rate_limited_dispatch_is_recorded_as_its_own_error_category(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A speed limit is a distinct cause, so it must not record as a generic failure."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    error = ProviderError("the provider rate-limited the request")
    error.category = ProviderFailure.RATE_LIMITED
    provider = ScriptedProvider(script={}, background_answers=[error, error])
    task_id = _background_task(durable_database, workspace_id, "rate-limited")

    with pytest.raises(ProviderError, match="rate-limited"):
        _service(durable_database, tmp_path, provider).run(
            workspace_id,
            campaign_manifest_sha256=manifest.sha256,
            campaign_cycle=1,
            max_model_calls=4,
            max_hosted_searches=20,
            context=_FakeContext(task_id=task_id),
        )

    with durable_database.session() as session:
        report = latest_campaign_report(session, workspace_id)
    assert report is not None
    assert report.attempts[0].error_category == AttemptErrorCategory.RATE_LIMITED


def test_a_rejection_before_creation_that_is_not_capacity_is_recorded_as_not_created(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    error = ProviderError("the provider refused the request schema")
    error.certainly_not_created = True
    provider = ScriptedProvider(script={}, background_answers=[error])
    task_id = _background_task(durable_database, workspace_id, "refused-schema")

    with pytest.raises(ProviderError, match="refused the request schema"):
        _service(durable_database, tmp_path, provider).run(
            workspace_id,
            campaign_manifest_sha256=manifest.sha256,
            campaign_cycle=1,
            max_model_calls=4,
            max_hosted_searches=20,
            context=_FakeContext(task_id=task_id),
        )

    with durable_database.session() as session:
        report = latest_campaign_report(session, workspace_id)
    assert report is not None
    assert report.attempts[0].state == "planned"
    assert report.attempts[0].error_category == AttemptErrorCategory.NOT_CREATED


def test_an_uncategorised_dispatch_failure_is_recorded_as_a_provider_error(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    provider = ScriptedProvider(
        script={}, background_answers=[ProviderError("the connection dropped mid-request")]
    )
    task_id = _background_task(durable_database, workspace_id, "dropped")

    with pytest.raises(ProviderError, match="connection dropped"):
        _service(durable_database, tmp_path, provider).run(
            workspace_id,
            campaign_manifest_sha256=manifest.sha256,
            campaign_cycle=1,
            max_model_calls=4,
            max_hosted_searches=20,
            context=_FakeContext(task_id=task_id),
        )

    with durable_database.session() as session:
        report = latest_campaign_report(session, workspace_id)
    assert report is not None
    assert report.attempts[0].state == "uncertain"
    assert report.attempts[0].error_category == AttemptErrorCategory.PROVIDER_ERROR


def test_a_capacity_refusal_before_creation_keeps_the_attempt_planned_as_capacity(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A provider rejection before response creation does not consume an attempt."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    error = CapacityError("request rejected before creation")
    error.certainly_not_created = True
    # Every flex retry and the standard-tier resend are refused.
    provider = ScriptedProvider(script={}, background_answers=[error] * 5)
    task_id = _background_task(durable_database, workspace_id, "not-created")
    slept: list[float] = []

    with pytest.raises(CapacityError, match="rejected before creation"):
        _service(durable_database, tmp_path, provider, sleeper=slept.append).run(
            workspace_id,
            campaign_manifest_sha256=manifest.sha256,
            campaign_cycle=1,
            max_model_calls=4,
            max_hosted_searches=20,
            context=_FakeContext(task_id=task_id),
        )

    assert len(slept) == 3
    with durable_database.session() as session:
        campaign = session.scalar(
            select(GuidedCampaign).where(GuidedCampaign.workspace_id == workspace_id)
        )
    assert campaign is not None
    [attempt] = GuidedCampaignLedger(durable_database).attempts(campaign.id)[:1]
    assert attempt.state == "planned"
    with durable_database.session() as session:
        state = session.scalar(
            select(GuidedCampaignState).where(GuidedCampaignState.campaign_id == campaign.id)
        )
    assert state is not None
    assert state.status == "failed"
    assert state.owner_task_id is None
    with durable_database.session() as session:
        report = latest_campaign_report(session, workspace_id)
    assert report is not None
    assert report.attempts[0].error_category == AttemptErrorCategory.CAPACITY

    retry_task_id = _background_task(durable_database, workspace_id, "not-created-retry")
    _service(
        durable_database,
        tmp_path,
        ScriptedProvider(script={}, background_answers=[_floor_poll(manifest)]),
    ).run(
        workspace_id,
        campaign_manifest_sha256=manifest.sha256,
        campaign_cycle=1,
        max_model_calls=4,
        max_hosted_searches=20,
        context=_FakeContext(task_id=retry_task_id),
    )
    with durable_database.session() as session:
        report = latest_campaign_report(session, workspace_id)
    assert report is not None
    assert report.attempts[0].state == "processed"
    assert report.attempts[0].error_category is None


class _NoBackgroundProvider:
    """A provider with no background web search, as the Anthropic adapter is."""

    name = ProviderName.ANTHROPIC


def test_a_provider_without_background_search_claims_no_campaign(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    task_id = _background_task(durable_database, workspace_id, "no-background")

    with pytest.raises(ProviderError, match="does not support background web search"):
        _service(durable_database, tmp_path, _NoBackgroundProvider()).run(
            workspace_id,
            campaign_manifest_sha256=manifest.sha256,
            campaign_cycle=1,
            max_model_calls=4,
            max_hosted_searches=20,
            context=_FakeContext(task_id=task_id),
        )

    with durable_database.session() as session:
        assert session.scalar(select(GuidedCampaignState)) is None


def test_cancellation_captures_the_terminal_response_and_stops_the_campaign(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """Cancellation requests the provider stop, records its terminal result, and ends canceled."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    provider = ScriptedProvider(
        script={},
        background_answers=[
            [
                BackgroundPoll(status="pending"),
                BackgroundPoll(
                    status="unusable",
                    record=CallRecord(usage=Usage(), finish=Finish.CANCELLED),
                    error="OpenAI returned no usable output (cancelled).",
                ),
            ]
        ],
    )
    task_id = _background_task(durable_database, workspace_id, "cancelled")

    result = GuidedCampaignService(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path / "artifacts"),
        posting_fetcher=UnexpectedPostingFetcher(),
        content_resolution=_content_resolution(durable_database, tmp_path),
        provider=provider,
        sleeper=lambda _: None,
    ).run(
        workspace_id,
        campaign_manifest_sha256=manifest.sha256,
        campaign_cycle=1,
        max_model_calls=4,
        max_hosted_searches=20,
        context=_CancelAfterDispatchContext(task_id=task_id),
    )

    assert result["cancelled"] is True
    assert provider.background_cancelled == ["resp_scripted_0"]
    with durable_database.session() as session:
        state = session.scalar(select(GuidedCampaignState))
    assert state is not None
    assert state.status == "cancelled"
    assert state.owner_task_id is None


class _CancelRefusedProvider(ScriptedProvider):
    """The response finished before the cancel landed, so the provider refuses it."""

    def cancel_background(self, response_id: str) -> None:
        super().cancel_background(response_id)
        raise translate(_StatusError(400), ProviderName.OPENAI, "cancel a response")


def test_a_cancel_refused_because_the_response_finished_still_ends_cancelled(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    provider = _CancelRefusedProvider(
        script={}, background_answers=[[BackgroundPoll(status="pending"), _floor_poll(manifest)]]
    )
    task_id = _background_task(durable_database, workspace_id, "cancel-refused")

    result = _run(
        durable_database,
        tmp_path,
        workspace_id,
        manifest,
        provider,
        key="cancel-refused-run",
        context=_CancelAfterDispatchContext(task_id=task_id),
    )

    assert result["cancelled"] is True
    assert provider.background_cancelled == ["resp_scripted_0"]
    assert _campaign_state(durable_database).status == "cancelled"


def test_a_cancel_after_capture_leaves_the_attempt_captured_with_nothing_read(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    posting_url = "https://jobs.example.test/staff-data-engineer"
    schema = campaign_response_schema(tuple(branch.key for branch in manifest.branches))
    selected = {"url": posting_url, "branch_keys": [manifest.branches[0].key], "why": "fits"}
    poll = BackgroundPoll(
        status="complete",
        value=schema.model_validate({"postings": [selected], "boards": []}),
        record=CallRecord(
            usage=Usage(),
            searches=(
                Search(
                    queries=("Staff Data Engineer jobs",),
                    sources=(SearchSource(url=posting_url, title="Staff Data Engineer"),),
                ),
            ),
        ),
    )
    fetcher = _CountingPostingFetcher()
    task_id = _background_task(durable_database, workspace_id, "cancel-captured")

    result = _run(
        durable_database,
        tmp_path,
        workspace_id,
        manifest,
        ScriptedProvider(script={}, background_answers=[poll]),
        key="cancel-captured-run",
        posting_fetcher=fetcher,
        context=_CancelAfterDispatchContext(task_id=task_id),
    )

    assert result["cancelled"] is True
    assert fetcher.calls == 0
    with durable_database.session() as session:
        attempt = session.scalar(select(GuidedCampaignAttempt))
        captured = session.scalar(select(GuidedCampaignCapturedUrl))
    assert attempt is not None
    assert attempt.state == "captured"
    assert captured is not None
    assert captured.outcome == "pending_read"


@dataclass
class _SwitchedContext(_FakeContext):
    """Cancellation turns on only when something flips it, such as a posting read."""

    cancelled: bool = False

    def cancellation_requested(self) -> bool:
        return self.cancelled


class _CancellingPostingFetcher(_CountingPostingFetcher):
    def __init__(self, context: _SwitchedContext) -> None:
        super().__init__()
        self.context = context

    def fetch(
        self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL, board_url: str = ""
    ) -> FetchedPosting:
        self.context.cancelled = True
        return super().fetch(url, intent=intent)


def test_a_cancel_during_the_read_admits_nothing_and_keeps_the_attempt_captured(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    posting_url = "https://jobs.example.test/staff-data-engineer"
    poll = BackgroundPoll(
        status="complete",
        value=campaign_response_schema(
            tuple(branch.key for branch in manifest.branches)
        ).model_validate(
            {
                "postings": [
                    {
                        "url": posting_url,
                        "branch_keys": [manifest.branches[0].key],
                        "why": "matches profile",
                    }
                ],
                "boards": [],
            }
        ),
        record=CallRecord(
            usage=Usage(),
            searches=(
                Search(
                    queries=("Staff Data Engineer jobs",),
                    sources=(SearchSource(url=posting_url, title="Staff Data Engineer"),),
                ),
            ),
        ),
    )
    context = _SwitchedContext(
        task_id=_background_task(durable_database, workspace_id, "cancel-read")
    )
    fetcher = _CancellingPostingFetcher(context)

    result = _run(
        durable_database,
        tmp_path,
        workspace_id,
        manifest,
        ScriptedProvider(script={}, background_answers=[poll]),
        key="cancel-read-run",
        posting_fetcher=fetcher,
        context=context,
    )

    assert result["cancelled"] is True
    assert fetcher.calls == 1
    with durable_database.session() as session:
        attempt = session.scalar(select(GuidedCampaignAttempt))
        captured = session.scalar(select(GuidedCampaignCapturedUrl))
    assert attempt is not None
    assert attempt.state == "captured"
    assert captured is not None
    assert captured.outcome == "pending_read"


def _run(
    database: Database,
    tmp_path: Path,
    workspace_id: str,
    manifest: CampaignManifest,
    provider: Any,
    *,
    key: str,
    fetcher: Any = None,
    posting_fetcher: Any = None,
    context: Any = None,
) -> dict[str, Any]:
    task_id = _background_task(database, workspace_id, key)
    return GuidedCampaignService(
        database,
        storage=runtime_storage(database, tmp_path / "artifacts"),
        posting_fetcher=posting_fetcher or UnexpectedPostingFetcher(),
        content_resolution=_content_resolution(database, tmp_path),
        fetcher=fetcher,
        provider=provider,
        sleeper=lambda _: None,
    ).run(
        workspace_id,
        campaign_manifest_sha256=manifest.sha256,
        campaign_cycle=1,
        max_model_calls=4,
        max_hosted_searches=20,
        context=context or _FakeContext(task_id=task_id),
    )


def _campaign_state(database: Database) -> GuidedCampaignState:
    with database.session() as session:
        state = session.scalar(select(GuidedCampaignState))
    assert state is not None
    return state


def _floor_poll(
    manifest: CampaignManifest, *, usage: Usage | None = None, **record: Any
) -> BackgroundPoll:
    """A complete response whose captured searches ran every branch's floor query."""
    schema = campaign_response_schema(tuple(branch.key for branch in manifest.branches))
    return BackgroundPoll(
        status="complete",
        value=schema.model_validate({"postings": [], "boards": []}),
        record=CallRecord(
            usage=usage or Usage(),
            searches=tuple(
                Search(queries=(branch.floor_query,), sources=()) for branch in manifest.branches
            ),
            **record,
        ),
    )


def test_a_campaign_whose_captured_queries_reach_every_protected_family_completes(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """Branch evidence captured during the run settles the final status before it is stored."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    provider = ScriptedProvider(script={}, background_answers=[_floor_poll(manifest)])

    result = _run(durable_database, tmp_path, workspace_id, manifest, provider, key="complete")

    assert result["reasons"] == []
    assert _campaign_state(durable_database).status == "completed"
    with durable_database.session() as session:
        report = latest_campaign_report(session, workspace_id)
    assert report is not None
    assert {branch.outcome for branch in report.branches} == {"searched"}


def test_a_truncated_response_leaves_the_campaign_incomplete(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A response cut off at its token ceiling cannot count as a completed campaign."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    provider = ScriptedProvider(
        script={}, background_answers=[_floor_poll(manifest, finish=Finish.TRUNCATED)]
    )

    result = _run(durable_database, tmp_path, workspace_id, manifest, provider, key="truncated")

    assert result["reasons"] == ["truncated_response"]
    assert _campaign_state(durable_database).status == "incomplete"


def _searched_poll(manifest: CampaignManifest, *searches: Search) -> BackgroundPoll:
    """A complete response that ran exactly these searches and picked nothing."""
    schema = campaign_response_schema(tuple(branch.key for branch in manifest.branches))
    return BackgroundPoll(
        status="complete",
        value=schema.model_validate({"postings": [], "boards": []}),
        record=CallRecord(usage=Usage(), searches=searches),
    )


def _branch_outcomes(database: Database, workspace_id: str) -> list[str]:
    """Return the report's branch outcomes in manifest order."""
    with database.session() as session:
        report = latest_campaign_report(session, workspace_id)
    assert report is not None
    return [row.outcome.value for row in sorted(report.branches, key=lambda row: row.position)]


def test_a_branch_no_captured_query_reached_reads_unvisited(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace_id = _two_attempt_workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    searched = next(
        branch for branch in manifest.branches if branch.key == manifest.attempts[0].branch_keys[0]
    )
    provider = ScriptedProvider(
        script={},
        background_answers=[
            _searched_poll(manifest, Search(queries=(searched.floor_query,), sources=())),
            *[_searched_poll(manifest) for _ in manifest.attempts[1:]],
        ],
    )

    _run(durable_database, tmp_path, workspace_id, manifest, provider, key="one-branch")

    assert _branch_outcomes(durable_database, workspace_id) == [
        "searched" if branch is searched else "unvisited" for branch in manifest.branches
    ]


def test_a_search_reported_without_its_query_leaves_unmatched_branches_unattributable(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    provider = ScriptedProvider(
        script={}, background_answers=[_searched_poll(manifest, Search(queries=(), sources=()))]
    )

    _run(durable_database, tmp_path, workspace_id, manifest, provider, key="queryless")

    assert set(_branch_outcomes(durable_database, workspace_id)) == {"unattributable"}


def test_a_floorless_branch_the_answer_never_attributes_is_unvisited_and_ordered_first_next(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            seniority=Seniority(target_titles=stated_titles("Staff Data Engineer")),
            focus=Focus(capabilities=[Cluster(name="records custody")]),
        ),
    )
    manifest = _compile(durable_database, workspace_id, tmp_path)
    floored = [branch for branch in manifest.branches if branch.floor_query]
    [floorless] = [branch for branch in manifest.branches if not branch.floor_query]
    assert manifest.branches[0] is not floorless
    schema = campaign_response_schema(tuple(branch.key for branch in manifest.branches))
    poll = BackgroundPoll(
        status="complete",
        value=schema.model_validate(
            {
                "searches": [
                    {"query": branch.floor_query, "branch_keys": [branch.key]} for branch in floored
                ]
            }
        ),
        record=CallRecord(
            usage=Usage(),
            searches=tuple(Search(queries=(branch.floor_query,), sources=()) for branch in floored),
        ),
    )
    provider = ScriptedProvider(script={}, background_answers=[poll])

    _run(durable_database, tmp_path, workspace_id, manifest, provider, key="unattributed")

    assert _branch_outcomes(durable_database, workspace_id) == [
        "unvisited" if branch is floorless else "searched" for branch in manifest.branches
    ]
    assert _compile(durable_database, workspace_id, tmp_path).branches[0].key == floorless.key


def test_a_protected_family_no_captured_query_searched_leaves_the_campaign_incomplete(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    assert any(branch.protected for branch in manifest.branches)
    provider = ScriptedProvider(script={}, background_answers=[_searched_poll(manifest)])

    result = _run(durable_database, tmp_path, workspace_id, manifest, provider, key="no-search")

    assert result["reasons"] == ["protected_family_unsearched"]
    assert _campaign_state(durable_database).status == "incomplete"


def test_abandoning_one_attempt_while_another_is_still_planned_names_incomplete_attempts(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace_id = _two_attempt_workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    assert len(manifest.attempts) >= 2
    ledger = GuidedCampaignLedger(durable_database)
    task_id = _background_task(durable_database, workspace_id, "owner")
    campaign_id = ledger.create_or_claim(workspace_id, manifest, 1, task_id, {})
    position = manifest.attempts[0].position
    ledger.mark_dispatching(campaign_id, position, task_id, "a" * 64)
    ledger.mark_uncertain(campaign_id, position, task_id, "boom")

    with durable_database.session() as session:
        report = abandon_uncertain_attempt(session, workspace_id, campaign_id, position)

    assert [item.value for item in report.warnings] == ["abandoned_attempt", "incomplete_attempts"]


def test_a_search_source_without_a_recorded_query_is_still_capture_evidence() -> None:
    """A search action the provider reported without its query still captured its sources."""
    poll = BackgroundPoll(
        status="complete",
        value=None,
        record=CallRecord(
            usage=Usage(),
            searches=(
                Search(queries=(), sources=(SearchSource(url="https://jobs.example.test/jobs/7"),)),
            ),
        ),
    )

    rows, refused_picks = captured_url_rows(poll)
    [row] = rows

    assert refused_picks == ()
    assert row["observations"] == [{"action_kind": "search_source", "query": ""}]


def test_cancellation_before_a_planned_attempt_dispatches_nothing(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A canceled task never creates the next paid response."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    task_id = _background_task(durable_database, workspace_id, "cancel-first")

    result = _run(
        durable_database,
        tmp_path,
        workspace_id,
        manifest,
        _ExplosiveProvider(),
        key="cancel-first-run",
        context=_CancellingContext(task_id=task_id),
    )

    assert result["cancelled"] is True
    assert _campaign_state(durable_database).status == "cancelled"


def test_campaign_report_is_refused_for_another_workspace_or_an_unknown_id(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A campaign's report is reachable by id, but only inside its own workspace."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    ledger = GuidedCampaignLedger(durable_database)
    task_id = _background_task(durable_database, workspace_id, "owner")
    campaign_id = ledger.create_or_claim(workspace_id, manifest, 1, task_id, {})

    other_workspace_id = str(
        WorkspaceService(durable_database, runtime_storage(durable_database))
        .create(slug="campaign-other", name="Campaign other")
        .id
    )

    with durable_database.session() as session:
        report = campaign_report(session, workspace_id, campaign_id)
        assert report.campaign_id == campaign_id

        with pytest.raises(NotFoundError):
            campaign_report(session, other_workspace_id, campaign_id)

        with pytest.raises(NotFoundError):
            campaign_report(session, workspace_id, "unknown-campaign")


def test_abandon_is_refused_unless_the_campaign_is_paused(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A campaign a live task still owns cannot have an attempt abandoned under it."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    ledger = GuidedCampaignLedger(durable_database)
    task_id = _background_task(durable_database, workspace_id, "owner")
    campaign_id = ledger.create_or_claim(workspace_id, manifest, 1, task_id, {})
    position = manifest.attempts[0].position
    ledger.mark_dispatching(campaign_id, position, task_id, "a" * 64)
    ledger.mark_uncertain(campaign_id, position, task_id, "boom")
    with durable_database.session() as session:
        state = session.scalar(select(GuidedCampaignState))
        assert state is not None
        state.status = "running"

    with durable_database.session() as session, pytest.raises(ConflictError):
        abandon_uncertain_attempt(session, workspace_id, campaign_id, position)


def test_abandoning_an_uncertain_attempt_closes_the_campaign_incomplete(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """Abandoning an uncertain attempt ends the campaign; it never quietly resumes."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    ledger = GuidedCampaignLedger(durable_database)
    task_id = _background_task(durable_database, workspace_id, "owner")
    campaign_id = ledger.create_or_claim(workspace_id, manifest, 1, task_id, {})
    position = manifest.attempts[0].position
    ledger.mark_dispatching(campaign_id, position, task_id, "a" * 64)
    ledger.mark_uncertain(campaign_id, position, task_id, "boom")

    with durable_database.session() as session:
        report = abandon_uncertain_attempt(session, workspace_id, campaign_id, position)

    assert report.status.value == "incomplete"
    assert "abandoned_attempt" in [item.value for item in report.warnings]
    with durable_database.session() as session:
        state = session.scalar(select(GuidedCampaignState))
        assert state is not None
        assert state.status == "incomplete"
        assert state.owner_task_id is None
    # Abandoning is terminal: the same (sha, cycle) can never resume this campaign.
    with pytest.raises(ConflictError):
        ledger.create_or_claim(workspace_id, manifest, 1, task_id, {})


def _two_attempt_workspace(database: Database, tmp_path: Path, master_cv: Path) -> str:
    workspace_id = _workspace(database, tmp_path, master_cv)
    SearchParametersService(database).save(
        workspace_id,
        JobSearchParameters(
            seniority=Seniority(
                target_titles=stated_titles(
                    "Staff Data Engineer",
                    "Data Platform Architect",
                    "Technical Product Lead",
                    "Data Governance Manager",
                )
            )
        ),
    )
    return workspace_id


def test_a_search_overrun_never_stops_the_remaining_planned_attempts(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace_id = _two_attempt_workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    assert len(manifest.attempts) >= 2
    overrun = manifest.bounds.max_hosted_searches + 5
    provider = ScriptedProvider(
        script={},
        background_answers=[_floor_poll(manifest, search_count=overrun) for _ in manifest.attempts],
    )

    result = _run(durable_database, tmp_path, workspace_id, manifest, provider, key="overrun")

    assert len(provider.background_dispatch_calls) == len(manifest.attempts)
    assert result["reasons"] == ["search_overrun"]
    assert _campaign_state(durable_database).status == "completed"


def test_a_report_with_an_unpriced_call_totals_unpriced(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    provider = ScriptedProvider(
        script={}, background_answers=[_floor_poll(manifest, usage_known=False)]
    )

    _run(durable_database, tmp_path, workspace_id, manifest, provider, key="unpriced")

    with durable_database.session() as session:
        report = latest_campaign_report(session, workspace_id)
    assert report is not None
    assert [attempt.cost_cents for attempt in report.attempts] == [None]
    assert report.cost_cents is None
    with durable_database.session() as session:
        preview = build_campaign_preview(
            session, workspace_id, max_model_calls=4, max_hosted_searches=20
        )
    assert preview.estimated_cost_cents is None


def test_the_preview_estimates_cost_per_attempt_from_completed_campaigns(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    usage = Usage(input_tokens=400_000, output_tokens=100_000)
    provider = ScriptedProvider(script={}, background_answers=[_floor_poll(manifest, usage=usage)])
    _run(durable_database, tmp_path, workspace_id, manifest, provider, key="priced")
    _complete_campaign_tasks(durable_database)

    with durable_database.session() as session:
        report = latest_campaign_report(session, workspace_id)
        amount = session.scalar(select(CostRecord.amount))
        preview = build_campaign_preview(
            session, workspace_id, max_model_calls=4, max_hosted_searches=20
        )
    assert report is not None
    assert report.cost_cents is not None
    assert report.cost_cents > 0
    assert amount is not None
    assert preview.estimated_cost_cents == cents(amount * len(preview.manifest.attempts))


def test_a_campaign_that_never_completed_leaves_the_preview_without_an_estimate(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A run stopped part way priced work it did not finish, so it cannot set the rate."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    usage = Usage(input_tokens=400_000, output_tokens=100_000)
    provider = ScriptedProvider(script={}, background_answers=[_floor_poll(manifest, usage=usage)])
    _run(durable_database, tmp_path, workspace_id, manifest, provider, key="unfinished")

    with durable_database.session() as session:
        preview = build_campaign_preview(
            session, workspace_id, max_model_calls=4, max_hosted_searches=20
        )

    assert preview.estimated_cost_cents is None


def test_the_preview_shows_when_each_branch_was_last_searched_and_a_cost_outlook(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    provider = ScriptedProvider(script={}, background_answers=[_floor_poll(manifest)])
    _run(durable_database, tmp_path, workspace_id, manifest, provider, key="searched")

    with durable_database.session() as session:
        preview = build_campaign_preview(
            session, workspace_id, max_model_calls=4, max_hosted_searches=20
        )

    assert set(preview.last_searched_at) == {branch.key for branch in manifest.branches}
    assert preview.planned_search_fee_cents is not None
    assert preview.planned_search_fee_cents > 0


def test_the_preview_prices_a_dated_model_name_the_way_a_recorded_call_is_priced(
    durable_database: Database,
    tmp_path: Path,
    master_cv: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dated snapshot name is the same shipped model, so the fee is known here too."""
    monkeypatch.setenv(
        "CLEARSCOUT_MODELS",
        '{"openai":{"extraction":{"model":"gpt-5.6-terra-2026-03-05"},'
        '"drafting":{"model":"gpt-5.6-terra-2026-03-05"}}}',
    )
    workspace_id = _workspace(durable_database, tmp_path, master_cv)

    with durable_database.session() as session:
        preview = build_campaign_preview(
            session, workspace_id, max_model_calls=4, max_hosted_searches=20
        )

    assert preview.model == "gpt-5.6-terra-2026-03-05"
    assert preview.planned_search_fee_cents is not None


def test_captured_not_selected_counts_only_attempts_that_did_not_select_the_url(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A later attempt's pick does not rewrite what an earlier attempt left unselected."""
    workspace_id = _two_attempt_workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    posting_url = "https://jobs.example.test/jobs/staff-data-engineer"
    schema = campaign_response_schema(tuple(branch.key for branch in manifest.branches))
    record = CallRecord(
        usage=Usage(),
        searches=(
            Search(
                queries=("Staff Data Engineer jobs",),
                sources=(SearchSource(url=posting_url, title="Staff Data Engineer"),),
            ),
        ),
    )
    unselected = BackgroundPoll(
        status="complete",
        value=schema.model_validate({"postings": [], "boards": []}),
        record=record,
    )
    selected = BackgroundPoll(
        status="complete",
        value=schema.model_validate(
            {
                "postings": [{"url": posting_url, "branch_keys": [], "why": "fits"}],
                "boards": [],
            }
        ),
        record=record,
    )
    provider = ScriptedProvider(script={}, background_answers=[unselected, selected])

    _run(
        durable_database,
        tmp_path,
        workspace_id,
        manifest,
        provider,
        key="selection",
        posting_fetcher=_CountingPostingFetcher(),
    )

    with durable_database.session() as session:
        report = latest_campaign_report(session, workspace_id)
    assert report is not None
    assert [attempt.captured_not_selected for attempt in report.attempts] == [1, 0]


def _complete_read(listings: list[SourceListing]) -> SourceRead:
    return SourceRead(
        listings=listings,
        complete=True,
        raw_count=len(listings),
        outcome=SourceReadOutcome.ATS_COMPLETE,
    )


class _ListingReader:
    """Answers every board root with one listing and counts the reads."""

    def __init__(self) -> None:
        self.roots: list[str] = []

    def listings(
        self,
        employer: str,
        url: str,
        *,
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        self.roots.append(url)
        return _complete_read(
            [SourceListing(employer="Example Co", title="Staff Data Engineer", url=f"{url}/jobs/1")]
        )


def test_two_staged_details_from_one_new_board_admit_that_board_once(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """Staged postings keep their Pending rows, and their shared board registers once, disabled."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    details = (
        "https://boards.greenhouse.io/examplehealth/jobs/1001",
        "https://boards.greenhouse.io/examplehealth/jobs/1002",
    )
    schema = campaign_response_schema(tuple(branch.key for branch in manifest.branches))
    poll = BackgroundPoll(
        status="complete",
        value=schema.model_validate({"postings": [], "boards": []}),
        record=CallRecord(
            usage=Usage(),
            searches=(
                Search(
                    queries=("Staff Data Engineer jobs",),
                    sources=tuple(SearchSource(url=url) for url in details),
                ),
            ),
        ),
    )
    reader = _ListingReader()

    _run(
        durable_database,
        tmp_path,
        workspace_id,
        manifest,
        ScriptedProvider(script={}, background_answers=[poll]),
        key="boards",
        fetcher=reader,
        posting_fetcher=_CountingPostingFetcher(),
    )

    with durable_database.session() as session:
        report = latest_campaign_report(session, workspace_id)
        sources = session.scalars(select(Source).where(Source.workspace_id == workspace_id)).all()
    assert report is not None
    assert len(report.findings) == 2
    assert [board.url for board in report.boards_admitted] == [
        "https://job-boards.greenhouse.io/examplehealth"
    ]
    assert len(sources) == 1
    assert sources[0].enabled is False


def test_a_board_admissions_staged_postings_are_listed_by_the_report(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A model-named board's admission carries every posting staged from its listings."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    board_url = "https://boards.greenhouse.io/examplehealth"
    schema = campaign_response_schema(tuple(branch.key for branch in manifest.branches))
    poll = BackgroundPoll(
        status="complete",
        value=schema.model_validate(
            {"postings": [], "boards": [{"url": board_url, "branch_keys": [], "why": "fits"}]}
        ),
        record=CallRecord(
            usage=Usage(),
            searches=(
                Search(
                    queries=("Staff Data Engineer jobs",),
                    sources=(SearchSource(url=board_url),),
                ),
            ),
        ),
    )

    _run(
        durable_database,
        tmp_path,
        workspace_id,
        manifest,
        ScriptedProvider(script={}, background_answers=[poll]),
        key="board-postings",
        fetcher=_BoardListingsReader(),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    with durable_database.session() as session:
        report = latest_campaign_report(session, workspace_id)
        pending_ids = {
            row.id
            for row in session.scalars(
                select(PendingDiscovery).where(PendingDiscovery.workspace_id == workspace_id)
            )
        }
    assert report is not None
    assert len(report.boards_admitted) == 1
    admission = report.boards_admitted[0]
    assert {posting.pending_discovery_id for posting in admission.admitted_postings} == pending_ids
    assert [posting.title for posting in admission.admitted_postings] == [
        "Staff Data Engineer",
        "Staff Data Engineer",
    ]
    assert [posting.employer for posting in admission.admitted_postings] == [
        "Examplehealth Inc",
        "Examplehealth Inc",
    ]


class _NetworkListingsReader:
    """Answers a network board root with two screening matches from different employers."""

    def listings(
        self,
        employer: str,
        url: str,
        *,
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        return _complete_read(
            [
                SourceListing(
                    employer=name,
                    title="Staff Data Engineer",
                    url=f"https://jobs.example-{index}.test/jobs/{index}",
                )
                for index, name in enumerate(("Example Bio", "Northwind Labs"))
            ]
        )


def test_a_network_board_admission_is_listed_by_the_report_without_a_source(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    board_url = "https://example-ventures.getro.com/jobs"
    schema = campaign_response_schema(tuple(branch.key for branch in manifest.branches))
    poll = BackgroundPoll(
        status="complete",
        value=schema.model_validate(
            {"postings": [], "boards": [{"url": board_url, "branch_keys": [], "why": "fits"}]}
        ),
        record=CallRecord(
            usage=Usage(),
            searches=(
                Search(
                    queries=("Staff Data Engineer jobs",),
                    sources=(SearchSource(url=board_url),),
                ),
            ),
        ),
    )

    _run(
        durable_database,
        tmp_path,
        workspace_id,
        manifest,
        ScriptedProvider(script={}, background_answers=[poll]),
        key="network-board",
        fetcher=_NetworkListingsReader(),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    with durable_database.session() as session:
        report = latest_campaign_report(session, workspace_id)
        sources = session.scalars(select(Source).where(Source.workspace_id == workspace_id)).all()
    assert report is not None
    assert sources == []
    [admission] = report.boards_admitted
    assert (admission.url, admission.source_id) == (board_url, None)
    assert [posting.employer for posting in admission.admitted_postings] == [
        "Example Bio",
        "Northwind Labs",
    ]


def test_a_board_the_campaign_registers_is_left_out_of_the_recurring_sweep(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A model-named board is saved disabled, so no later sweep reads it unasked."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    board_url = "https://boards.greenhouse.io/examplehealth"
    schema = campaign_response_schema(tuple(branch.key for branch in manifest.branches))
    poll = BackgroundPoll(
        status="complete",
        value=schema.model_validate(
            {"postings": [], "boards": [{"url": board_url, "branch_keys": [], "why": "fits"}]}
        ),
        record=CallRecord(
            usage=Usage(),
            searches=(
                Search(
                    queries=("Staff Data Engineer jobs",),
                    sources=(SearchSource(url=board_url),),
                ),
            ),
        ),
    )

    _run(
        durable_database,
        tmp_path,
        workspace_id,
        manifest,
        ScriptedProvider(script={}, background_answers=[poll]),
        key="board-disabled",
        fetcher=_BoardListingsReader(),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    with durable_database.session() as session:
        sources = session.scalars(select(Source).where(Source.workspace_id == workspace_id)).all()
    assert [source.enabled for source in sources] == [False]


def test_a_campaign_retains_a_staged_publisher_body(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A campaign-ready posting enters Pending with the publisher body already retained."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    posting_url = "https://boards.greenhouse.io/examplehealth/jobs/1001"
    schema = campaign_response_schema(tuple(branch.key for branch in manifest.branches))
    poll = BackgroundPoll(
        status="complete",
        value=schema.model_validate({"postings": [], "boards": []}),
        record=CallRecord(
            usage=Usage(),
            searches=(
                Search(
                    queries=("Staff Data Engineer jobs",),
                    sources=(SearchSource(url=posting_url),),
                ),
            ),
        ),
    )
    task_id = _background_task(durable_database, workspace_id, "retained-body")
    service = GuidedCampaignService(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path / "artifacts"),
        fetcher=_ListingReader(),
        posting_fetcher=_ReadablePublisherPostingFetcher(),
        content_resolution=PostingContentResolutionService(
            durable_database,
            _UnusedContentResolver(),
            storage=runtime_storage(durable_database, tmp_path / "artifacts"),
        ),
        provider=ScriptedProvider(script={}, background_answers=[poll]),
    )

    service.run(
        workspace_id,
        campaign_manifest_sha256=manifest.sha256,
        campaign_cycle=1,
        max_model_calls=4,
        max_hosted_searches=20,
        context=_FakeContext(task_id=task_id),
    )

    with durable_database.session() as session:
        pending = session.scalar(
            select(PendingDiscovery).where(PendingDiscovery.workspace_id == workspace_id)
        )
    assert pending is not None
    assert pending.content_status == "readable"
    assert pending.current_content_id is not None


def test_a_retention_failure_resumes_the_captured_attempt_without_a_new_dispatch(
    durable_database: Database,
    tmp_path: Path,
    master_cv: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed retained-body write leaves its paid response captured for retry."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    posting_url = "https://boards.greenhouse.io/examplehealth/jobs/1001"
    schema = campaign_response_schema(tuple(branch.key for branch in manifest.branches))
    poll = BackgroundPoll(
        status="complete",
        value=schema.model_validate({"postings": [], "boards": []}),
        record=CallRecord(
            usage=Usage(),
            searches=(
                Search(
                    queries=("Staff Data Engineer jobs",),
                    sources=(SearchSource(url=posting_url),),
                ),
            ),
        ),
    )
    resolution = PostingContentResolutionService(
        durable_database,
        _RetryContentResolver(),
        storage=runtime_storage(durable_database, tmp_path / "artifacts"),
    )
    original_store = resolution.store_prefetched_result
    failed_once = False

    def fail_once(*args: Any, **kwargs: Any) -> str:
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("retained-body storage failed")
        return original_store(*args, **kwargs)

    monkeypatch.setattr(resolution, "store_prefetched_result", fail_once)
    provider = ScriptedProvider(script={}, background_answers=[poll])
    service = GuidedCampaignService(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path / "artifacts"),
        fetcher=_ListingReader(),
        posting_fetcher=_ReadablePublisherPostingFetcher(),
        content_resolution=resolution,
        provider=provider,
    )
    with pytest.raises(RuntimeError, match="retained-body storage failed"):
        service.run(
            workspace_id,
            campaign_manifest_sha256=manifest.sha256,
            campaign_cycle=1,
            max_model_calls=4,
            max_hosted_searches=20,
            context=_FakeContext(
                task_id=_background_task(durable_database, workspace_id, "retain-first")
            ),
        )

    with durable_database.session() as session:
        campaign = session.scalar(
            select(GuidedCampaign).where(GuidedCampaign.workspace_id == workspace_id)
        )
        assert campaign is not None
        attempt = session.scalar(
            select(GuidedCampaignAttempt).where(GuidedCampaignAttempt.campaign_id == campaign.id)
        )
    assert attempt is not None
    assert attempt.state == "captured"

    service.run(
        workspace_id,
        campaign_manifest_sha256=manifest.sha256,
        campaign_cycle=1,
        max_model_calls=4,
        max_hosted_searches=20,
        context=_FakeContext(
            task_id=_background_task(durable_database, workspace_id, "retain-second")
        ),
    )

    with durable_database.session() as session:
        campaign = session.scalar(
            select(GuidedCampaign).where(GuidedCampaign.workspace_id == workspace_id)
        )
        assert campaign is not None
        attempt = session.scalar(
            select(GuidedCampaignAttempt).where(GuidedCampaignAttempt.campaign_id == campaign.id)
        )
        pending = session.scalar(
            select(PendingDiscovery).where(PendingDiscovery.workspace_id == workspace_id)
        )
    assert attempt is not None
    assert attempt.state == "processed"
    assert pending is not None
    assert pending.current_content_id is not None
    assert len(provider.background_dispatch_calls) == 1


def test_a_web_campaign_without_content_resolution_is_refused_before_dispatch(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A campaign that could not retain staged bodies never spends a provider call."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    # No content_resolution: the handler refuses before building a service at all.
    handler = GuidedCampaignHandler(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path / "artifacts"),
        posting_fetcher=UnexpectedPostingFetcher(),
        provider=_ExplosiveProvider(),
    )
    context = _FakeContext(task_id=_background_task(durable_database, workspace_id, "no-resolver"))

    with pytest.raises(ValidationError, match="automatic content resolution"):
        handler(context, {"campaign_manifest_sha256": manifest.sha256, "campaign_cycle": 1})

    with durable_database.session() as session:
        assert session.scalar(select(GuidedCampaign)) is None


class _BoardListingsReader:
    """Answers a board root with two listings that both pass campaign screening."""

    def listings(
        self,
        employer: str,
        url: str,
        *,
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        return _complete_read(
            [
                SourceListing(
                    employer="Examplehealth Inc",
                    title="Staff Data Engineer",
                    url=f"https://job-boards.greenhouse.io/examplehealth/jobs/{index}",
                )
                for index in (2001, 2002)
            ]
        )


def test_a_failed_board_resolution_resumes_every_staged_listing_without_a_new_dispatch(
    durable_database: Database,
    tmp_path: Path,
    master_cv: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every listing a named board staged is resolved on retry, not only the first."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    board_url = "https://boards.greenhouse.io/examplehealth"
    schema = campaign_response_schema(tuple(branch.key for branch in manifest.branches))
    poll = BackgroundPoll(
        status="complete",
        value=schema.model_validate(
            {"postings": [], "boards": [{"url": board_url, "branch_keys": [], "why": "fits"}]}
        ),
        record=CallRecord(
            usage=Usage(),
            searches=(
                Search(
                    queries=("Staff Data Engineer jobs",),
                    sources=(SearchSource(url=board_url),),
                ),
            ),
        ),
    )
    resolution = PostingContentResolutionService(
        durable_database,
        _RetryContentResolver(),
        storage=runtime_storage(durable_database, tmp_path / "artifacts"),
    )
    original_resolve = resolution.resolve_automatic
    failed_once = False

    def fail_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("automatic resolution failed")
        return original_resolve(*args, **kwargs)

    monkeypatch.setattr(resolution, "resolve_automatic", fail_once)
    provider = ScriptedProvider(script={}, background_answers=[poll])
    service = GuidedCampaignService(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path / "artifacts"),
        fetcher=_BoardListingsReader(),
        posting_fetcher=UnexpectedPostingFetcher(),
        content_resolution=resolution,
        provider=provider,
    )
    with pytest.raises(RuntimeError, match="automatic resolution failed"):
        service.run(
            workspace_id,
            campaign_manifest_sha256=manifest.sha256,
            campaign_cycle=1,
            max_model_calls=4,
            max_hosted_searches=20,
            context=_FakeContext(
                task_id=_background_task(durable_database, workspace_id, "board-first")
            ),
        )
    service.run(
        workspace_id,
        campaign_manifest_sha256=manifest.sha256,
        campaign_cycle=1,
        max_model_calls=4,
        max_hosted_searches=20,
        context=_FakeContext(
            task_id=_background_task(durable_database, workspace_id, "board-second")
        ),
    )

    with durable_database.session() as session:
        attempt = session.scalar(select(GuidedCampaignAttempt))
        pending = session.scalars(
            select(PendingDiscovery).where(PendingDiscovery.workspace_id == workspace_id)
        ).all()
    assert attempt is not None
    assert attempt.state == "processed"
    assert len(pending) == 2
    assert all(row.current_content_id is not None for row in pending)
    assert len(provider.background_dispatch_calls) == 1


class _ClosedThenFlakyResolver:
    """Reports one board listing closed and fails the other's first read."""

    def __init__(self) -> None:
        self.failed = False

    def resolve(
        self,
        url: str,
        *,
        employer: str,
        title: str,
        intent: PostingReadIntent = PostingReadIntent.CRAWL,
        board_url: str = "",
    ) -> ResolvedPostingContent:
        del intent
        if url.endswith("/2001"):
            return ResolvedPostingContent(
                requested_url=url,
                final_url=url,
                content_status=PostingContentAccessStatus.NOT_YET_READ,
                identity_status=PostingIdentityStatus.NOT_YET_VERIFIED,
                posting_status=PostingStatus.CLOSED,
                extraction_method=None,
                fetched_at=utc_now(),
                http_status=404,
                identity_evidence={"closure": True},
                note="The publisher reports that this posting is closed.",
            )
        if not self.failed:
            self.failed = True
            raise RuntimeError("listing read failed")
        return _RetryContentResolver().resolve(url, employer=employer, title=title)


def test_a_retry_skips_a_staged_listing_the_first_pass_withdrew(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A listing closed before the failure needs no body, so the resumed attempt still completes."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    board_url = "https://boards.greenhouse.io/examplehealth"
    schema = campaign_response_schema(tuple(branch.key for branch in manifest.branches))
    poll = BackgroundPoll(
        status="complete",
        value=schema.model_validate(
            {"postings": [], "boards": [{"url": board_url, "branch_keys": [], "why": "fits"}]}
        ),
        record=CallRecord(
            usage=Usage(),
            searches=(
                Search(
                    queries=("Staff Data Engineer jobs",), sources=(SearchSource(url=board_url),)
                ),
            ),
        ),
    )
    provider = ScriptedProvider(script={}, background_answers=[poll])
    service = GuidedCampaignService(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path / "artifacts"),
        fetcher=_BoardListingsReader(),
        posting_fetcher=UnexpectedPostingFetcher(),
        content_resolution=PostingContentResolutionService(
            durable_database,
            _ClosedThenFlakyResolver(),
            storage=runtime_storage(durable_database, tmp_path / "artifacts"),
        ),
        provider=provider,
    )
    with pytest.raises(RuntimeError, match="listing read failed"):
        service.run(
            workspace_id,
            campaign_manifest_sha256=manifest.sha256,
            campaign_cycle=1,
            max_model_calls=4,
            max_hosted_searches=20,
            context=_FakeContext(
                task_id=_background_task(durable_database, workspace_id, "closed-first")
            ),
        )
    service.run(
        workspace_id,
        campaign_manifest_sha256=manifest.sha256,
        campaign_cycle=1,
        max_model_calls=4,
        max_hosted_searches=20,
        context=_FakeContext(
            task_id=_background_task(durable_database, workspace_id, "closed-second")
        ),
    )

    with durable_database.session() as session:
        attempt = session.scalar(select(GuidedCampaignAttempt))
    assert attempt is not None
    assert attempt.state == "processed"
    assert len(provider.background_dispatch_calls) == 1


class _WorkerCancelledError(Exception):
    """Stands in for the worker's own cancellation exception."""


@dataclass
class _CancelDuringReadContext(_FakeContext):
    """Cancellation arrives while staged postings are being read."""

    cancelled: bool = False

    def cancellation_requested(self) -> bool:
        return self.cancelled

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise _WorkerCancelledError()


class _CancellingResolver:
    """Requests cancellation from inside a posting read."""

    def __init__(self, context: _CancelDuringReadContext) -> None:
        self.context = context

    def resolve(
        self,
        url: str,
        *,
        employer: str,
        title: str,
        intent: PostingReadIntent = PostingReadIntent.CRAWL,
        board_url: str = "",
    ) -> ResolvedPostingContent:
        self.context.cancelled = True
        return _RetryContentResolver().resolve(url, employer=employer, title=title, intent=intent)


def test_cancelling_while_staged_postings_are_read_cancels_the_campaign(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A cooperative cancel during content retention ends canceled, not failed."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    board_url = "https://boards.greenhouse.io/examplehealth"
    schema = campaign_response_schema(tuple(branch.key for branch in manifest.branches))
    poll = BackgroundPoll(
        status="complete",
        value=schema.model_validate(
            {"postings": [], "boards": [{"url": board_url, "branch_keys": [], "why": "fits"}]}
        ),
        record=CallRecord(
            usage=Usage(),
            searches=(
                Search(
                    queries=("Staff Data Engineer jobs",), sources=(SearchSource(url=board_url),)
                ),
            ),
        ),
    )
    context = _CancelDuringReadContext(
        task_id=_background_task(durable_database, workspace_id, "cancel-read")
    )
    service = GuidedCampaignService(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path / "artifacts"),
        fetcher=_BoardListingsReader(),
        posting_fetcher=UnexpectedPostingFetcher(),
        content_resolution=PostingContentResolutionService(
            durable_database,
            _CancellingResolver(context),
            storage=runtime_storage(durable_database, tmp_path / "artifacts"),
        ),
        provider=ScriptedProvider(script={}, background_answers=[poll]),
    )

    service.run(
        workspace_id,
        campaign_manifest_sha256=manifest.sha256,
        campaign_cycle=1,
        max_model_calls=4,
        max_hosted_searches=20,
        context=context,
    )

    assert _campaign_state(durable_database).status == "cancelled"
    with durable_database.session() as session:
        attempt = session.scalar(select(GuidedCampaignAttempt))
    assert attempt is not None
    assert attempt.state == "captured"


class _FlakyPollProvider(ScriptedProvider):
    """Raise once when polling one chosen response id, then answer normally."""

    def __init__(self, *, fail_response_id: str, error: Exception, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._fail_response_id = fail_response_id
        self._error = error
        self._failed = False

    def poll_background(self, response_id: str, schema: Any, ceiling: int) -> BackgroundPoll:
        if response_id == self._fail_response_id and not self._failed:
            self._failed = True
            raise self._error
        return super().poll_background(response_id, schema, ceiling)


class _StatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"Error code: {status_code}")
        self.status_code = status_code


def _response_gone_error() -> ClearscoutError:
    return translate(
        _StatusError(404), ProviderName.OPENAI, "collect a response", not_found="response"
    )


def _overloaded_error() -> ClearscoutError:
    return translate(_StatusError(529), ProviderName.OPENAI, "collect a response")


def test_a_non_response_gone_poll_error_leaves_the_attempt_dispatched_and_releases_the_lease(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A transient poll failure (5xx, rate limit, overload) never pauses the campaign uncertain.

    The attempt stays dispatched against the same response id, and the dead
    task's lease is released so a later run can resume it.
    """
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    schema = campaign_response_schema(tuple(branch.key for branch in manifest.branches))
    completed = BackgroundPoll(
        status="complete",
        value=schema.model_validate({"postings": [], "boards": []}),
        record=CallRecord(usage=Usage(), response_id="resp_scripted_0"),
    )
    provider = _FlakyPollProvider(
        fail_response_id="resp_scripted_0",
        error=_overloaded_error(),
        script={},
        background_answers=[completed],
    )
    task_id = _background_task(durable_database, workspace_id, "overloaded-poll")

    with pytest.raises(ProviderError, match="overloaded"):
        _service(durable_database, tmp_path, provider).run(
            workspace_id,
            campaign_manifest_sha256=manifest.sha256,
            campaign_cycle=1,
            max_model_calls=4,
            max_hosted_searches=20,
            context=_FakeContext(task_id=task_id),
        )

    with durable_database.session() as session:
        campaign = session.scalar(
            select(GuidedCampaign).where(GuidedCampaign.workspace_id == workspace_id)
        )
        assert campaign is not None
        state = session.scalar(
            select(GuidedCampaignState).where(GuidedCampaignState.campaign_id == campaign.id)
        )
    assert state is not None
    assert state.status == CampaignStatus.FAILED
    assert state.owner_task_id is None
    [attempt] = GuidedCampaignLedger(durable_database).attempts(campaign.id)
    assert attempt.state == AttemptState.DISPATCHED

    second_task_id = _background_task(durable_database, workspace_id, "overloaded-poll-resume")
    _service(durable_database, tmp_path, provider).run(
        workspace_id,
        campaign_manifest_sha256=manifest.sha256,
        campaign_cycle=1,
        max_model_calls=4,
        max_hosted_searches=20,
        context=_FakeContext(task_id=second_task_id),
    )

    assert len(provider.background_dispatch_calls) == 1


def test_a_response_gone_poll_error_pauses_the_campaign_uncertain_and_releases_the_owner(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """Only a response the provider can no longer find pauses the campaign uncertain."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    schema = campaign_response_schema(tuple(branch.key for branch in manifest.branches))
    unreached = BackgroundPoll(
        status="complete",
        value=schema.model_validate({"postings": [], "boards": []}),
        record=CallRecord(usage=Usage(), response_id="resp_scripted_0"),
    )
    provider = _FlakyPollProvider(
        fail_response_id="resp_scripted_0",
        error=_response_gone_error(),
        script={},
        background_answers=[unreached],
    )
    task_id = _background_task(durable_database, workspace_id, "response-gone")

    with pytest.raises(ProviderError, match="could not find that background response"):
        _service(durable_database, tmp_path, provider).run(
            workspace_id,
            campaign_manifest_sha256=manifest.sha256,
            campaign_cycle=1,
            max_model_calls=4,
            max_hosted_searches=20,
            context=_FakeContext(task_id=task_id),
        )

    with durable_database.session() as session:
        campaign = session.scalar(
            select(GuidedCampaign).where(GuidedCampaign.workspace_id == workspace_id)
        )
        assert campaign is not None
        state = session.scalar(
            select(GuidedCampaignState).where(GuidedCampaignState.campaign_id == campaign.id)
        )
    assert state is not None
    assert state.status == CampaignStatus.PAUSED_UNCERTAIN
    assert state.owner_task_id is None
    [attempt] = GuidedCampaignLedger(durable_database).attempts(campaign.id)
    assert attempt.state == AttemptState.UNCERTAIN
    with durable_database.session() as session:
        report = latest_campaign_report(session, workspace_id)
    assert report is not None
    assert [row.error_category for row in report.attempts] == [AttemptErrorCategory.RESPONSE_GONE]


def test_reapproving_the_same_paused_campaign_explains_it_needs_abandoning_or_a_new_approval(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A retry against the exact paused campaign is refused with actionable wording, not "final"."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    provider = ScriptedProvider(script={}, background_answers=[_response_gone_error()])
    task_id = _background_task(durable_database, workspace_id, "paused-retry")
    with pytest.raises(ProviderError):
        _service(durable_database, tmp_path, provider).run(
            workspace_id,
            campaign_manifest_sha256=manifest.sha256,
            campaign_cycle=1,
            max_model_calls=4,
            max_hosted_searches=20,
            context=_FakeContext(task_id=task_id),
        )

    second_task_id = _background_task(durable_database, workspace_id, "paused-retry-2")
    with pytest.raises(ConflictError, match="abandon it or approve a new campaign") as caught:
        _service(durable_database, tmp_path, provider).run(
            workspace_id,
            campaign_manifest_sha256=manifest.sha256,
            campaign_cycle=1,
            max_model_calls=4,
            max_hosted_searches=20,
            context=_FakeContext(task_id=second_task_id),
        )
    assert "final" not in str(caught.value)


def test_a_stuck_dispatched_campaign_is_superseded_and_its_billed_response_collected_once(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A re-scoped approval collects a stuck dispatched response once and closes it superseded."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    posting_url = "https://jobs.example.test/staff-data-engineer"
    schema = campaign_response_schema(tuple(branch.key for branch in manifest.branches))
    value = schema.model_validate(
        {
            "postings": [
                {
                    "url": posting_url,
                    "branch_keys": [manifest.branches[0].key],
                    "why": "matches profile",
                }
            ],
            "boards": [],
        }
    )
    completed = BackgroundPoll(
        status="complete",
        value=value,
        record=CallRecord(
            usage=Usage(),
            response_id="resp_scripted_0",
            searches=(
                Search(
                    queries=("Staff Data Engineer jobs",),
                    sources=(SearchSource(url=posting_url, title="Staff Data Engineer"),),
                ),
            ),
        ),
    )
    provider = _FlakyPollProvider(
        fail_response_id="resp_scripted_0",
        error=_overloaded_error(),
        script={},
        background_answers=[completed],
    )
    service = GuidedCampaignService(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path / "artifacts"),
        posting_fetcher=_CountingPostingFetcher(),
        content_resolution=_content_resolution(durable_database, tmp_path),
        provider=provider,
    )
    first_task_id = _background_task(durable_database, workspace_id, "stuck-1")
    with pytest.raises(ProviderError):
        service.run(
            workspace_id,
            campaign_manifest_sha256=manifest.sha256,
            campaign_cycle=1,
            max_model_calls=4,
            max_hosted_searches=20,
            context=_FakeContext(task_id=first_task_id),
        )
    with durable_database.session() as session:
        stale = session.scalar(
            select(GuidedCampaign).where(GuidedCampaign.workspace_id == workspace_id)
        )
    assert stale is not None
    stale_id = stale.id

    # A differently scoped approval compiles a new manifest at cycle 2.
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            seniority=Seniority(target_titles=stated_titles("Data Governance Manager"))
        ),
    )
    new_manifest = _compile(durable_database, workspace_id, tmp_path)
    assert new_manifest.sha256 != manifest.sha256
    new_schema = campaign_response_schema(tuple(branch.key for branch in new_manifest.branches))
    new_poll = BackgroundPoll(
        status="complete",
        value=new_schema.model_validate({"postings": [], "boards": []}),
        record=CallRecord(usage=Usage(), response_id="resp_scripted_1"),
    )
    provider._background_answers.append(new_poll)
    second_task_id = _background_task(durable_database, workspace_id, "stuck-2")

    service.run(
        workspace_id,
        campaign_manifest_sha256=new_manifest.sha256,
        campaign_cycle=2,
        max_model_calls=4,
        max_hosted_searches=20,
        context=_FakeContext(task_id=second_task_id),
    )

    assert len(provider.background_dispatch_calls) == 2
    with durable_database.session() as session:
        stale_state = session.scalar(
            select(GuidedCampaignState).where(GuidedCampaignState.campaign_id == stale_id)
        )
        calls = session.scalars(
            select(ModelCall).where(ModelCall.provider_response_id == "resp_scripted_0")
        ).all()
    assert stale_state is not None
    assert stale_state.status == CampaignStatus.INCOMPLETE
    assert stale_state.reasons == ["superseded"]
    assert len(calls) == 1
    [stale_attempt] = GuidedCampaignLedger(durable_database).attempts(stale_id)
    assert stale_attempt.state == AttemptState.PROCESSED


@dataclass
class _StrandedSupersede:
    service: GuidedCampaignService
    provider: ScriptedProvider
    workspace_id: str
    stale_id: str
    new_manifest: CampaignManifest


def _strand_a_superseded_attempt(
    database: Database,
    tmp_path: Path,
    master_cv: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    failures: int = 1,
    supersede_context: Callable[[str], Any] | None = None,
    on_failure: Callable[[], None] = lambda: None,
    stale_pending: bool = False,
) -> _StrandedSupersede:
    """Supersede a stuck campaign whose captured posting fails content retention."""
    workspace_id = _workspace(database, tmp_path, master_cv)
    manifest = _compile(database, workspace_id, tmp_path)
    posting_url = "https://jobs.example.test/jobs/staff-data-engineer"
    schema = campaign_response_schema(tuple(branch.key for branch in manifest.branches))
    completed = BackgroundPoll(
        status="complete",
        value=schema.model_validate({"postings": [], "boards": []}),
        record=CallRecord(
            usage=Usage(),
            response_id="resp_scripted_0",
            searches=(
                Search(
                    queries=("Staff Data Engineer jobs",),
                    sources=(SearchSource(url=posting_url, title="Staff Data Engineer"),),
                ),
            ),
        ),
    )
    provider = _FlakyPollProvider(
        fail_response_id="resp_scripted_0",
        error=_overloaded_error(),
        script={},
        background_answers=[
            [BackgroundPoll(status="pending"), completed] if stale_pending else completed
        ],
    )
    resolution = _content_resolution(database, tmp_path)
    original_resolve = resolution.resolve_automatic
    remaining = failures

    def fail_first(*args: Any, **kwargs: Any) -> Any:
        nonlocal remaining
        if remaining > 0:
            remaining -= 1
            on_failure()
            raise RuntimeError("retention failed during supersede")
        return original_resolve(*args, **kwargs)

    monkeypatch.setattr(resolution, "resolve_automatic", fail_first)
    service = GuidedCampaignService(
        database,
        storage=runtime_storage(database, tmp_path / "artifacts"),
        posting_fetcher=_CountingPostingFetcher(),
        content_resolution=resolution,
        provider=provider,
        sleeper=lambda _seconds: None,
    )
    with pytest.raises(ProviderError):
        service.run(
            workspace_id,
            campaign_manifest_sha256=manifest.sha256,
            campaign_cycle=1,
            max_model_calls=4,
            max_hosted_searches=20,
            context=_FakeContext(task_id=_background_task(database, workspace_id, "strand-1")),
        )
    with database.session() as session:
        stale = session.scalar(
            select(GuidedCampaign).where(GuidedCampaign.workspace_id == workspace_id)
        )
    assert stale is not None
    SearchParametersService(database).save(
        workspace_id,
        JobSearchParameters(
            seniority=Seniority(target_titles=stated_titles("Data Governance Manager"))
        ),
    )
    new_manifest = _compile(database, workspace_id, tmp_path)
    new_schema = campaign_response_schema(tuple(branch.key for branch in new_manifest.branches))
    provider._background_answers.append(
        BackgroundPoll(
            status="complete",
            value=new_schema.model_validate({"postings": [], "boards": []}),
            record=CallRecord(usage=Usage(), response_id="resp_scripted_1"),
        )
    )
    service.run(
        workspace_id,
        campaign_manifest_sha256=new_manifest.sha256,
        campaign_cycle=2,
        max_model_calls=4,
        max_hosted_searches=20,
        context=(
            supersede_context(workspace_id)
            if supersede_context is not None
            else _FakeContext(task_id=_background_task(database, workspace_id, "strand-2"))
        ),
    )
    return _StrandedSupersede(service, provider, workspace_id, stale.id, new_manifest)


def test_a_retention_failure_during_supersede_keeps_the_attempt_captured_for_retry(
    durable_database: Database,
    tmp_path: Path,
    master_cv: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local processing failure is not an uncertain provider state; the new campaign runs."""
    stranded = _strand_a_superseded_attempt(durable_database, tmp_path, master_cv, monkeypatch)

    [stale_attempt] = GuidedCampaignLedger(durable_database).attempts(stranded.stale_id)
    assert stale_attempt.state == AttemptState.CAPTURED
    assert stale_attempt.error_category == AttemptErrorCategory.PROCESSING_FAILED
    with durable_database.session() as session:
        stale_state = session.scalar(
            select(GuidedCampaignState).where(GuidedCampaignState.campaign_id == stranded.stale_id)
        )
        staged = session.scalars(
            select(PendingDiscovery).where(PendingDiscovery.workspace_id == stranded.workspace_id)
        ).all()
    assert stale_state is not None
    assert (stale_state.status, stale_state.reasons, stale_state.owner_task_id) == (
        CampaignStatus.INCOMPLETE,
        ["superseded"],
        None,
    )
    assert len(stranded.provider.background_dispatch_calls) == 2
    assert [row.current_content_id for row in staged] == [None]


def test_the_next_campaign_run_retries_a_stranded_attempt_without_a_new_dispatch(
    durable_database: Database,
    tmp_path: Path,
    master_cv: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The next run reprocesses the captured attempt, retains the body, and keeps the close."""
    stranded = _strand_a_superseded_attempt(durable_database, tmp_path, master_cv, monkeypatch)
    with durable_database.session() as session:
        outcomes_before = session.scalar(
            select(func.count())
            .select_from(GuidedCampaignBranchOutcome)
            .join(
                GuidedCampaignBranch,
                GuidedCampaignBranch.id == GuidedCampaignBranchOutcome.branch_id,
            )
            .where(GuidedCampaignBranch.campaign_id == stranded.stale_id)
        )

    SearchParametersService(durable_database).save(
        stranded.workspace_id,
        JobSearchParameters(
            seniority=Seniority(target_titles=stated_titles("Technical Product Lead"))
        ),
    )
    next_manifest = _compile(durable_database, stranded.workspace_id, tmp_path)
    next_schema = campaign_response_schema(tuple(branch.key for branch in next_manifest.branches))
    stranded.provider._background_answers.append(
        BackgroundPoll(
            status="complete",
            value=next_schema.model_validate({"postings": [], "boards": []}),
            record=CallRecord(usage=Usage(), response_id="resp_scripted_2"),
        )
    )

    stranded.service.run(
        stranded.workspace_id,
        campaign_manifest_sha256=next_manifest.sha256,
        campaign_cycle=3,
        max_model_calls=4,
        max_hosted_searches=20,
        context=_FakeContext(
            task_id=_background_task(durable_database, stranded.workspace_id, "strand-3")
        ),
    )

    [stale_attempt] = GuidedCampaignLedger(durable_database).attempts(stranded.stale_id)
    assert stale_attempt.state == AttemptState.PROCESSED
    assert stale_attempt.error_category == ""
    with durable_database.session() as session:
        stale_state = session.scalar(
            select(GuidedCampaignState).where(GuidedCampaignState.campaign_id == stranded.stale_id)
        )
        staged = session.scalars(
            select(PendingDiscovery).where(PendingDiscovery.workspace_id == stranded.workspace_id)
        ).all()
        outcomes_after = session.scalar(
            select(func.count())
            .select_from(GuidedCampaignBranchOutcome)
            .join(
                GuidedCampaignBranch,
                GuidedCampaignBranch.id == GuidedCampaignBranchOutcome.branch_id,
            )
            .where(GuidedCampaignBranch.campaign_id == stranded.stale_id)
        )
    assert stale_state is not None
    assert (stale_state.status, stale_state.owner_task_id) == (CampaignStatus.INCOMPLETE, None)
    assert all(row.current_content_id is not None for row in staged)
    assert outcomes_after == outcomes_before
    # The stale response is never dispatched again; only the new approval dispatches.
    assert len(stranded.provider.background_dispatch_calls) == 3


def test_the_latest_report_counts_stranded_attempts_until_a_retry_processes_them(
    durable_database: Database,
    tmp_path: Path,
    master_cv: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The report the person sees names work an earlier, superseded campaign left behind."""
    stranded = _strand_a_superseded_attempt(durable_database, tmp_path, master_cv, monkeypatch)
    with durable_database.session() as session:
        before = latest_campaign_report(session, stranded.workspace_id)

    manual_id = _background_task(durable_database, stranded.workspace_id, "manual")
    stranded.service.retry_stranded_processing(
        stranded.workspace_id,
        manual_id,
        _FakeContext(task_id=manual_id),
    )

    with durable_database.session() as session:
        after = latest_campaign_report(session, stranded.workspace_id)
    assert before is not None
    assert after is not None
    assert (before.stranded_processing_attempts, after.stranded_processing_attempts) == (1, 0)


def test_cancelling_while_a_stale_attempt_is_polled_keeps_it_captured_and_starts_nothing(
    durable_database: Database,
    tmp_path: Path,
    master_cv: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A paid capture never becomes uncertain because the superseding task was canceled."""
    stranded = _strand_a_superseded_attempt(
        durable_database,
        tmp_path,
        master_cv,
        monkeypatch,
        supersede_context=lambda workspace_id: _CancellingContext(
            task_id=_background_task(durable_database, workspace_id, "cancel-poll")
        ),
        stale_pending=True,
    )

    [stale_attempt] = GuidedCampaignLedger(durable_database).attempts(stranded.stale_id)
    assert stale_attempt.state == AttemptState.CAPTURED
    with durable_database.session() as session:
        campaigns = session.scalar(
            select(func.count())
            .select_from(GuidedCampaign)
            .where(GuidedCampaign.workspace_id == stranded.workspace_id)
        )
    assert campaigns == 1


def test_a_stranded_attempt_on_a_campaign_retry_cannot_claim_is_not_counted(
    durable_database: Database,
    tmp_path: Path,
    master_cv: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The report offers a retry only for attempts the retry task will actually process."""
    stranded = _strand_a_superseded_attempt(durable_database, tmp_path, master_cv, monkeypatch)
    with durable_database.session() as session:
        session.execute(
            sa_update(GuidedCampaignState)
            .where(GuidedCampaignState.campaign_id == stranded.stale_id)
            .values(status=CampaignStatus.CANCELLED)
        )
    with durable_database.session() as session:
        report = latest_campaign_report(session, stranded.workspace_id)
    assert report is not None
    assert report.stranded_processing_attempts == 0


def test_a_stranded_campaign_that_cannot_be_rebuilt_never_blocks_the_retry_pass(
    durable_database: Database,
    tmp_path: Path,
    master_cv: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Old evidence that fails to load is skipped, so a new approval still runs."""
    stranded = _strand_a_superseded_attempt(durable_database, tmp_path, master_cv, monkeypatch)

    def unreadable(*args: Any, **kwargs: Any) -> Any:
        raise ConflictError("stored campaign inputs are malformed")

    monkeypatch.setattr(stranded.service, "_stored_inputs", unreadable)
    task_id = _background_task(durable_database, stranded.workspace_id, "rebuild-fails")

    processed = stranded.service.retry_stranded_processing(
        stranded.workspace_id, task_id, _FakeContext(task_id=task_id)
    )

    assert processed == 0
    [stale_attempt] = GuidedCampaignLedger(durable_database).attempts(stranded.stale_id)
    assert stale_attempt.error_category == AttemptErrorCategory.PROCESSING_FAILED


def test_cancelling_a_retry_task_ends_it_cancelled(
    durable_database: Database,
    tmp_path: Path,
    master_cv: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retry handler reports a cancel as the worker's cancellation, not a failure."""
    stranded = _strand_a_superseded_attempt(durable_database, tmp_path, master_cv, monkeypatch)
    task_id = _background_task(durable_database, stranded.workspace_id, "retry-cancel")

    @dataclass
    class _WorkerCancel(_CancellingContext):
        def raise_if_cancelled(self) -> None:
            raise TaskCancelledError()

    queue = SimpleNamespace(get=lambda _: SimpleNamespace(workspace_id=stranded.workspace_id))
    with pytest.raises(TaskCancelledError):
        GuidedCampaignRetryHandler(stranded.service)(
            cast(Any, _WorkerCancel(task_id=task_id, queue=queue)), {}
        )


def test_a_queued_retry_task_processes_stranded_attempts_without_a_provider_call(
    durable_database: Database,
    tmp_path: Path,
    master_cv: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The report's retry action is an unpaid task that finishes the stranded processing."""
    stranded = _strand_a_superseded_attempt(durable_database, tmp_path, master_cv, monkeypatch)
    calls_before = len(stranded.provider.calls)
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=stranded.workspace_id,
        task_type="search.guided_campaign_retry",
        payload=validate_task_payload("search.guided_campaign_retry", {}),
        idempotency_key="retry-stranded",
    )
    handlers = default_handlers(durable_database, artifact_root=tmp_path / "artifacts")
    handlers["search.guided_campaign_retry"] = GuidedCampaignRetryHandler(stranded.service)
    worker = Worker(durable_database, worker_id="retry-worker", handlers=handlers)

    assert worker.run_once()

    finished = queue.get(str(task.id))
    assert (finished.status, finished.error_message) == ("completed", None)
    assert finished.progress["processed_attempts"] == 1
    assert len(stranded.provider.calls) == calls_before
    with durable_database.session() as session:
        report = latest_campaign_report(session, stranded.workspace_id)
    assert report is not None
    assert report.stranded_processing_attempts == 0
    assert TASK_REGISTRY["search.guided_campaign_retry"].paid is False


def test_a_stranded_attempt_whose_retry_fails_again_stays_resumable_and_never_blocks(
    durable_database: Database,
    tmp_path: Path,
    master_cv: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second processing failure records the category again and releases the lease."""
    stranded = _strand_a_superseded_attempt(
        durable_database, tmp_path, master_cv, monkeypatch, failures=2
    )

    again_id = _background_task(durable_database, stranded.workspace_id, "again")
    processed = stranded.service.retry_stranded_processing(
        stranded.workspace_id,
        again_id,
        _FakeContext(task_id=again_id),
    )

    assert processed == 0
    [stale_attempt] = GuidedCampaignLedger(durable_database).attempts(stranded.stale_id)
    assert (stale_attempt.state, stale_attempt.error_category) == (
        AttemptState.CAPTURED,
        AttemptErrorCategory.PROCESSING_FAILED,
    )
    with durable_database.session() as session:
        stale_state = session.scalar(
            select(GuidedCampaignState).where(GuidedCampaignState.campaign_id == stranded.stale_id)
        )
    assert stale_state is not None
    assert (stale_state.status, stale_state.owner_task_id) == (CampaignStatus.INCOMPLETE, None)


def test_cancelling_during_supersede_retention_keeps_the_stale_campaign_and_starts_nothing(
    durable_database: Database,
    tmp_path: Path,
    master_cv: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancel while a superseded posting is read ends the task canceled with no new campaign."""
    context = _CancelDuringReadContext(task_id="")

    def cancel() -> None:
        context.cancelled = True

    def supersede_context(workspace_id: str) -> _CancelDuringReadContext:
        context.task_id = _background_task(durable_database, workspace_id, "cancel-supersede")
        return context

    stranded = _strand_a_superseded_attempt(
        durable_database,
        tmp_path,
        master_cv,
        monkeypatch,
        supersede_context=supersede_context,
        on_failure=cancel,
    )

    [stale_attempt] = GuidedCampaignLedger(durable_database).attempts(stranded.stale_id)
    assert (stale_attempt.state, stale_attempt.error_category) == (AttemptState.CAPTURED, "")
    with durable_database.session() as session:
        campaigns = session.scalars(
            select(GuidedCampaign).where(GuidedCampaign.workspace_id == stranded.workspace_id)
        ).all()
        stale_state = session.scalar(
            select(GuidedCampaignState).where(GuidedCampaignState.campaign_id == stranded.stale_id)
        )
    assert [campaign.id for campaign in campaigns] == [stranded.stale_id]
    assert stale_state is not None
    assert (stale_state.status, stale_state.owner_task_id) == (CampaignStatus.FAILED, None)
    assert len(stranded.provider.background_dispatch_calls) == 1


def test_a_processing_retry_claim_never_takes_a_lease_a_live_task_holds(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """The retry claim waits for an unfinished owner and takes a finished owner's lease."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    ledger = GuidedCampaignLedger(durable_database)
    owner_task_id = _background_task(durable_database, workspace_id, "live-owner")
    campaign_id = ledger.create_or_claim(workspace_id, manifest, 1, owner_task_id, {})
    with durable_database.session() as session:
        session.execute(
            sa_update(GuidedCampaignState)
            .where(GuidedCampaignState.campaign_id == campaign_id)
            .values(status=CampaignStatus.INCOMPLETE)
        )
        session.execute(
            sa_update(BackgroundTask)
            .where(BackgroundTask.id == owner_task_id)
            .values(status="running")
        )

    retry_task_id = _background_task(durable_database, workspace_id, "retry-task")
    assert ledger.claim_for_processing_retry(campaign_id, retry_task_id) is False

    with durable_database.session() as session:
        session.execute(
            sa_update(BackgroundTask)
            .where(BackgroundTask.id == owner_task_id)
            .values(status="failed")
        )

    assert ledger.claim_for_processing_retry(campaign_id, retry_task_id) is True
    state = _campaign_state(durable_database)
    assert (state.status, state.owner_task_id) == (CampaignStatus.INCOMPLETE, retry_task_id)


@pytest.mark.parametrize(
    ("stale_by", "stale_status"),
    [("refused", CampaignStatus.FAILED), ("response_gone", CampaignStatus.PAUSED_UNCERTAIN)],
)
def test_a_supersede_that_cannot_rebuild_the_stale_campaign_restores_it(
    durable_database: Database,
    tmp_path: Path,
    master_cv: Path,
    monkeypatch: pytest.MonkeyPatch,
    stale_by: str,
    stale_status: CampaignStatus,
) -> None:
    """The stale campaign keeps its status, so a paused one can still be abandoned."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    provider: ScriptedProvider
    if stale_by == "refused":
        refused = CapacityError("request rejected before creation")
        refused.certainly_not_created = True
        provider = ScriptedProvider(script={}, background_answers=[refused] * 5)
    else:
        provider = _FlakyPollProvider(
            fail_response_id="resp_scripted_0",
            error=_response_gone_error(),
            script={},
            background_answers=[_floor_poll(manifest)],
        )
    service = _service(durable_database, tmp_path, provider, sleeper=lambda _: None)
    with pytest.raises(ClearscoutError):
        service.run(
            workspace_id,
            campaign_manifest_sha256=manifest.sha256,
            campaign_cycle=1,
            max_model_calls=4,
            max_hosted_searches=20,
            context=_FakeContext(task_id=_background_task(durable_database, workspace_id, "s1")),
        )
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            seniority=Seniority(target_titles=stated_titles("Data Governance Manager"))
        ),
    )
    new_manifest = _compile(durable_database, workspace_id, tmp_path)

    def unreadable(*_: Any) -> Any:
        raise ConflictError("stored campaign inputs cannot be read")

    monkeypatch.setattr(service, "_stored_inputs", unreadable)
    with pytest.raises(ConflictError, match="cannot be read"):
        service.run(
            workspace_id,
            campaign_manifest_sha256=new_manifest.sha256,
            campaign_cycle=2,
            max_model_calls=4,
            max_hosted_searches=20,
            context=_FakeContext(task_id=_background_task(durable_database, workspace_id, "s2")),
        )

    state = _campaign_state(durable_database)
    assert state.status == stale_status
    assert state.owner_task_id is None


def test_an_older_failed_campaign_behind_a_newer_completed_one_is_never_supersedable(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """The preview and the runner agree: only the workspace's latest campaign is supersedable."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    ledger = GuidedCampaignLedger(durable_database)
    first_task_id = _background_task(durable_database, workspace_id, "older-failed")
    older_id = ledger.create_or_claim(workspace_id, manifest, 1, first_task_id, {})
    ledger.finish(older_id, first_task_id, status=CampaignStatus.FAILED, reasons=[])

    second_task_id = _background_task(durable_database, workspace_id, "newer-completed")
    newer_manifest = replace(
        manifest, bounds=replace(manifest.bounds, max_model_calls=manifest.bounds.max_model_calls)
    )
    newer_id = ledger.create_or_claim(workspace_id, newer_manifest, 2, second_task_id, {})
    ledger.finish(newer_id, second_task_id, status=CampaignStatus.COMPLETED, reasons=[])

    assert ledger.supersedable_campaign(workspace_id) is None
    with durable_database.session() as session:
        preview = build_campaign_preview(
            session, workspace_id, max_model_calls=4, max_hosted_searches=20
        )
    assert preview.supersedes_campaign_id is None
    assert preview.cycle == 3


def test_claim_for_supersede_refuses_a_lease_that_no_longer_matches(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A concurrently changed campaign is not claimed; the caller finds nothing to supersede."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    ledger = GuidedCampaignLedger(durable_database)
    task_id = _background_task(durable_database, workspace_id, "claim-race")
    campaign_id = ledger.create_or_claim(workspace_id, manifest, 1, task_id, {})
    ledger.mark_dispatching(campaign_id, manifest.attempts[0].position, task_id, "a" * 64)
    ledger.mark_uncertain(campaign_id, manifest.attempts[0].position, task_id, "boom")
    # The owner is still task_id, so a claim observing a stale (wrong) owner must refuse.

    claimed = ledger.claim_for_supersede(
        campaign_id,
        "someone-else",
        observed_status=CampaignStatus.PAUSED_UNCERTAIN,
        observed_owner_task_id="a-different-dead-task",
    )

    assert claimed is False
    with durable_database.session() as session:
        state = session.scalar(
            select(GuidedCampaignState).where(GuidedCampaignState.campaign_id == campaign_id)
        )
    assert state is not None
    assert state.status == CampaignStatus.PAUSED_UNCERTAIN
    assert state.owner_task_id == task_id


def test_mark_dispatched_retries_a_transient_database_busy_without_a_second_dispatch(
    durable_database: Database, tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A locked-database commit on the first attempt is retried, not surfaced as a failure."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    ledger = GuidedCampaignLedger(durable_database)
    task_id = _background_task(durable_database, workspace_id, "busy-retry")
    campaign_id = ledger.create_or_claim(workspace_id, manifest, 1, task_id, {})
    position = manifest.attempts[0].position
    ledger.mark_dispatching(campaign_id, position, task_id, "a" * 64)

    real_session = ledger.database.session
    attempts_made = {"count": 0}

    class _FlakySession:
        def __enter__(self) -> Any:
            attempts_made["count"] += 1
            self._cm = real_session()
            session = self._cm.__enter__()
            if attempts_made["count"] == 1:

                def flaky_commit(*args: Any, **kwargs: Any) -> None:
                    raise OperationalError("COMMIT", {}, Exception("database is locked"))

                session.commit = flaky_commit  # type: ignore[method-assign]
            return session

        def __exit__(self, *exc: Any) -> Any:
            return self._cm.__exit__(*exc)

    monkeypatch.setattr(ledger.database, "session", _FlakySession)

    ledger.mark_dispatched(
        campaign_id, position, task_id, "resp-1", "a" * 64, request_envelope=None
    )

    [attempt] = ledger.attempts(campaign_id)
    assert attempt.state == AttemptState.DISPATCHED
    assert attempt.provider_response_id == "resp-1"


def test_a_later_search_source_observation_reopens_a_url_the_model_only_named(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A URL first seen only as a model pick is reopened once real search evidence names it too."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            seniority=Seniority(
                target_titles=stated_titles(
                    "Staff Data Engineer",
                    "Data Platform Architect",
                    "Technical Product Lead",
                    "Data Governance Manager",
                )
            )
        ),
    )
    manifest = _compile(durable_database, workspace_id, tmp_path)
    assert len(manifest.attempts) == 2
    posting_url = "https://jobs.example.test/phantom"
    schema = campaign_response_schema(tuple(branch.key for branch in manifest.branches))
    model_only = schema.model_validate(
        {
            "postings": [
                {"url": posting_url, "branch_keys": [manifest.branches[0].key], "why": "plausible"}
            ],
            "boards": [],
        }
    )
    empty = schema.model_validate({"postings": [], "boards": []})
    first_poll = BackgroundPoll(
        status="complete", value=model_only, record=CallRecord(usage=Usage())
    )
    second_poll = BackgroundPoll(
        status="complete",
        value=empty,
        record=CallRecord(
            usage=Usage(),
            searches=(
                Search(
                    queries=("Staff Data Engineer jobs",),
                    sources=(SearchSource(url=posting_url, title="Staff Data Engineer"),),
                ),
            ),
        ),
    )
    provider = ScriptedProvider(script={}, background_answers=[first_poll, second_poll])
    fetcher = _CountingPostingFetcher()
    service = GuidedCampaignService(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path / "artifacts"),
        posting_fetcher=fetcher,
        content_resolution=_content_resolution(durable_database, tmp_path),
        provider=provider,
    )
    task_id = _background_task(durable_database, workspace_id, "reopen-unresolved")

    service.run(
        workspace_id,
        campaign_manifest_sha256=manifest.sha256,
        campaign_cycle=1,
        max_model_calls=4,
        max_hosted_searches=20,
        context=_FakeContext(task_id=task_id),
    )

    with durable_database.session() as session:
        [captured] = session.scalars(select(GuidedCampaignCapturedUrl)).all()
    # The model-only observation left it unresolved; the second attempt's real
    # search source reopened it, so it was fetched and staged.
    assert captured.outcome == "staged"
    assert fetcher.calls == 1


def test_the_stored_settings_use_the_snapshots_prices_and_timeout_not_live_ones(
    durable_database: Database, tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resume prices and polls with the values recorded when the campaign was approved."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    service = _service(durable_database, tmp_path)
    inputs = service._build_inputs(workspace_id, max_model_calls=4, max_hosted_searches=20)
    approved_settings = inputs.settings.model_copy(
        update={
            "prices": {"gpt-5.6-terra": PriceOverride(input=1.23, output=4.56)},
            "request_timeout": 111.0,
        }
    )
    approved_inputs = replace(inputs, settings=approved_settings)
    document = {
        **approved_inputs.manifest.document(),
        "request_snapshot": approved_inputs.snapshot(),
    }
    live_settings = inputs.settings.model_copy(update={"request_timeout": 5.0})
    monkeypatch.setattr(
        service.queries,
        "planning_inputs",
        lambda workspace_id: SimpleNamespace(settings=live_settings),
    )

    restored = service._stored_inputs(workspace_id, document)

    assert restored.settings.request_timeout == 111.0
    assert restored.settings.prices["gpt-5.6-terra"].input == 1.23


def _one_branch_manifest(branch: CampaignBranch) -> CampaignManifest:
    return CampaignManifest(
        identities=CampaignIdentities(
            search_parameter_set_id="x",
            search_parameter_sha256="y" * 64,
            master_cv_version_id="z",
            master_cv_sha256="a" * 64,
            accepted_vocabulary_sha256="",
            admission_scope_sha256="b" * 64,
        ),
        compiler_version="test",
        branches=(branch,),
        attempts=(CampaignAttemptPlan(position=1, branch_keys=(branch.key,), search_cap=5),),
        bounds=CampaignBounds(),
    )


def test_a_query_naming_an_anchors_words_in_order_credits_it_but_a_reordering_does_not() -> None:
    """A model paraphrase such as "Director of Clinical Informatics" is the same branch."""
    branch = CampaignBranch(
        key="branch-informatics",
        kind=BranchKind.TITLE_TOPIC,
        priority_family="leadership",
        protected=True,
        markets=(),
        market_arrangements=(),
        lane=None,
        seniority="",
        title_aliases=("Director Clinical Informatics",),
        focuses=(),
        floor_query="q1",
        anchors=("Director Clinical Informatics",),
        label="Director Clinical Informatics",
    )
    manifest = _one_branch_manifest(branch)
    poll = BackgroundPoll(
        status="complete",
        record=CallRecord(
            usage=Usage(),
            searches=(
                Search(
                    queries=(
                        "Director of Clinical Informatics jobs",
                        "Clinical Informatics Director openings",
                        "Directors Clinical Informatics",
                    ),
                    sources=(),
                ),
            ),
        ),
    )

    matched = matched_queries(manifest, manifest.attempts[0], poll)

    assert matched[branch.key] == ("Director of Clinical Informatics jobs",)


def test_a_focus_bundle_branch_is_credited_by_a_query_naming_one_of_its_focus_terms() -> None:
    branch = CampaignBranch(
        key="branch-bundle",
        kind=BranchKind.FOCUS_BUNDLE,
        priority_family="domain",
        protected=False,
        markets=(),
        market_arrangements=(),
        lane=None,
        seniority="",
        title_aliases=(),
        focuses=(("domain_intersection", "member's records"),),
        floor_query="members records job",
        # The anchor drops the apostrophe a floor query cannot carry, so only the
        # focus term spells the phrase the way a person writes it.
        anchors=("members records",),
        label="member's records",
    )
    manifest = _one_branch_manifest(branch)
    poll = BackgroundPoll(
        status="complete",
        record=CallRecord(
            usage=Usage(),
            searches=(Search(queries=("clinical trial roles in member's records",), sources=()),),
        ),
    )

    matched = matched_queries(manifest, manifest.attempts[0], poll)

    assert matched[branch.key] == ("clinical trial roles in member's records",)


def test_a_shared_anchor_credits_only_its_own_market_chunk() -> None:
    """Two market chunks of one title topic share anchors; a query must also name its own market."""
    identities = CampaignIdentities(
        search_parameter_set_id="x",
        search_parameter_sha256="y" * 64,
        master_cv_version_id="z",
        master_cv_sha256="a" * 64,
        accepted_vocabulary_sha256="",
        admission_scope_sha256="b" * 64,
    )
    branch_ny = CampaignBranch(
        key="branch-ny",
        kind=BranchKind.TITLE_TOPIC,
        priority_family="ic",
        protected=False,
        markets=("New York",),
        market_arrangements=(),
        lane=None,
        seniority="",
        title_aliases=("Staff Data Engineer",),
        focuses=(),
        floor_query="q1",
        anchors=("Staff Data Engineer",),
        label="Staff Data Engineer",
    )
    branch_sf = replace(branch_ny, key="branch-sf", markets=("San Francisco",), floor_query="q2")
    manifest = CampaignManifest(
        identities=identities,
        compiler_version="test",
        branches=(branch_ny, branch_sf),
        attempts=(
            CampaignAttemptPlan(
                position=1, branch_keys=(branch_ny.key, branch_sf.key), search_cap=5
            ),
        ),
        bounds=CampaignBounds(),
    )
    attempt_plan = manifest.attempts[0]
    poll = BackgroundPoll(
        status="complete",
        record=CallRecord(
            usage=Usage(),
            searches=(Search(queries=("Staff Data Engineer New York openings",), sources=()),),
        ),
    )

    matched = matched_queries(manifest, attempt_plan, poll)

    assert matched[branch_ny.key] == ("Staff Data Engineer New York openings",)
    assert matched[branch_sf.key] == ()


def test_a_site_operand_never_credits_a_branch_whose_anchor_names_that_host() -> None:
    parameters = JobSearchParameters(
        seniority=Seniority(
            target_titles=[
                TargetTitle(title="Staff Data Engineer"),
                TargetTitle(title="Greenhouse"),
            ]
        ),
        locations=[Location(key="northland", country="Example Northland")],
    )
    manifest = compile_campaign(
        parameters,
        parameters.seniority.tracked_titles,
        [],
        identities=_identities_for_matching(),
        bounds=CampaignBounds(),
    )
    by_label = {branch.label: branch for branch in manifest.branches}
    titled, host_named = by_label["Staff Data Engineer"], by_label["Greenhouse"]
    [attempt] = manifest.attempts
    own_site_query = "(site:boards.greenhouse.io) Example Northland platform engineer"
    poll = BackgroundPoll(
        status="complete",
        record=CallRecord(
            usage=Usage(),
            searches=(Search(queries=(titled.floor_query, own_site_query), sources=()),),
        ),
    )

    matched = matched_queries(manifest, attempt, poll)

    assert matched == {titled.key: (titled.floor_query,), host_named.key: ()}


def _floorless_pair_manifest() -> tuple[CampaignManifest, CampaignBranch, CampaignBranch]:
    """Two floorless bundles in attempt 1 and a floored title branch alone in attempt 2."""
    capability = CampaignBranch(
        key="branch-capability",
        kind=BranchKind.FOCUS_BUNDLE,
        priority_family="capability",
        protected=False,
        markets=("Example Northland",),
        market_arrangements=(),
        lane=None,
        seniority="",
        title_aliases=(),
        focuses=(("mandate_capability", "records custody"),),
        floor_query="",
        anchors=(),
        label="records custody",
    )
    domain = replace(
        capability,
        key="branch-domain",
        priority_family="domain",
        focuses=(("domain_intersection", "member networks"),),
        label="member networks",
    )
    titled = replace(
        capability,
        key="branch-title",
        kind=BranchKind.TITLE_TOPIC,
        priority_family="ic",
        title_aliases=("Staff Data Engineer",),
        focuses=(),
        floor_query='"Staff Data Engineer" job',
        anchors=("Staff Data Engineer",),
        label="Staff Data Engineer",
    )
    manifest = CampaignManifest(
        identities=_identities_for_matching(),
        compiler_version="test",
        branches=(capability, domain, titled),
        attempts=(
            CampaignAttemptPlan(position=1, branch_keys=(capability.key, domain.key), search_cap=5),
            CampaignAttemptPlan(position=2, branch_keys=(titled.key,), search_cap=4),
        ),
        bounds=CampaignBounds(),
    )
    return manifest, capability, domain


def _attributed_poll(
    manifest: CampaignManifest, captured: tuple[str, ...], attributed: dict[str, list[str]]
) -> BackgroundPoll:
    """A complete response that ran ``captured`` and attributes queries to branch keys."""
    schema = campaign_response_schema(tuple(branch.key for branch in manifest.branches))
    return BackgroundPoll(
        status="complete",
        value=schema.model_validate(
            {
                "searches": [
                    {"query": query, "branch_keys": keys} for query, keys in attributed.items()
                ]
            }
        ),
        record=CallRecord(usage=Usage(), searches=(Search(queries=captured, sources=()),)),
    )


def test_a_floorless_branch_is_credited_only_by_a_captured_query_attributed_to_it() -> None:
    manifest, capability, domain = _floorless_pair_manifest()
    poll = _attributed_poll(
        manifest,
        captured=(
            "information governance lead openings",
            "cooperative data analyst",
            "member networks analyst",
        ),
        attributed={
            "Information Governance lead openings": [capability.key],
            "cooperative data analyst": [domain.key, "branch-title"],
            "records custody jobs": [capability.key],
        },
    )

    matched = matched_queries(manifest, manifest.attempts[0], poll)

    assert matched == {
        capability.key: ("information governance lead openings",),
        domain.key: ("cooperative data analyst",),
    }


def test_an_attribution_never_credits_a_branch_that_lists_a_floor_query() -> None:
    manifest, _, _ = _floorless_pair_manifest()
    titled = manifest.branches[2]
    poll = _attributed_poll(
        manifest,
        captured=("data platform openings",),
        attributed={"data platform openings": [titled.key]},
    )

    matched = matched_queries(manifest, manifest.attempts[1], poll)

    assert matched == {titled.key: ()}


def _identities_for_matching() -> CampaignIdentities:
    return CampaignIdentities(
        search_parameter_set_id="x",
        search_parameter_sha256="y" * 64,
        master_cv_version_id="z",
        master_cv_sha256="a" * 64,
        accepted_vocabulary_sha256="",
        admission_scope_sha256="b" * 64,
    )


def _outcome_rows(database: Database, task_id: str) -> list[SourceScanOutcome]:
    """Every candidate-ledger row this task actually wrote."""
    with database.session() as session:
        return list(
            session.scalars(
                select(SourceScanOutcome).where(SourceScanOutcome.task_id == task_id)
            ).all()
        )


@dataclass
class _RecordingContext(_FakeContext):
    """Keeps every progress payload the runner published."""

    published: list[dict[str, Any]] = field(default_factory=list)

    def progress(self, stage: str, payload: dict[str, Any]) -> None:
        self.published.append(payload)


def _single_posting_poll(
    manifest: CampaignManifest, posting_url: str, *, response_id: str = ""
) -> BackgroundPoll:
    """A complete response whose search captured one posting the model also selected."""
    schema = campaign_response_schema(tuple(branch.key for branch in manifest.branches))
    return BackgroundPoll(
        status="complete",
        value=schema.model_validate(
            {
                "postings": [
                    {
                        "url": posting_url,
                        "branch_keys": [manifest.branches[0].key],
                        "why": "matches profile",
                    }
                ],
                "boards": [],
            }
        ),
        record=CallRecord(
            usage=Usage(),
            response_id=response_id,
            searches=(
                Search(
                    queries=("Staff Data Engineer jobs",),
                    sources=(SearchSource(url=posting_url, title="Staff Data Engineer"),),
                ),
            ),
        ),
    )


def test_a_completed_run_reports_the_funnel_rows_it_wrote(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """The campaign panel's coverage denominator is what this task actually recorded."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    posting_url = "https://jobs.example.test/staff-data-engineer"
    task_id = _background_task(durable_database, workspace_id, "funnel-complete")

    result = GuidedCampaignService(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path / "artifacts"),
        posting_fetcher=_CountingPostingFetcher(),
        content_resolution=_content_resolution(durable_database, tmp_path),
        provider=ScriptedProvider(
            script={}, background_answers=[_single_posting_poll(manifest, posting_url)]
        ),
    ).run(
        workspace_id,
        campaign_manifest_sha256=manifest.sha256,
        campaign_cycle=1,
        max_model_calls=4,
        max_hosted_searches=20,
        context=_FakeContext(task_id=task_id),
    )

    rows = _outcome_rows(durable_database, task_id)
    assert [row.terminal_stage for row in rows] == ["staged"]
    assert result["funnel"]["total_outcomes"] == len(rows)
    assert [outcome["url"] for outcome in result["funnel"]["outcomes"]] == [posting_url]


def test_a_candidate_the_ledger_deduped_still_counts_toward_the_reported_total(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """One task supersedes a stale campaign and runs a new one that captured the same URL.

    The ledger keeps the URL once while the total counts both admissions, so the
    difference is readable as a coverage gap instead of being invisible.
    """
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    posting_url = "https://jobs.example.test/staff-data-engineer"
    provider = _FlakyPollProvider(
        fail_response_id="resp_scripted_0",
        error=_overloaded_error(),
        script={},
        background_answers=[
            _single_posting_poll(manifest, posting_url, response_id="resp_scripted_0")
        ],
    )
    fetcher = _CountingPostingFetcher()
    service = GuidedCampaignService(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path / "artifacts"),
        posting_fetcher=fetcher,
        content_resolution=_content_resolution(durable_database, tmp_path),
        provider=provider,
    )
    with pytest.raises(ProviderError):
        service.run(
            workspace_id,
            campaign_manifest_sha256=manifest.sha256,
            campaign_cycle=1,
            max_model_calls=4,
            max_hosted_searches=20,
            context=_FakeContext(
                task_id=_background_task(durable_database, workspace_id, "stale-capture")
            ),
        )
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            seniority=Seniority(target_titles=stated_titles("Data Governance Manager"))
        ),
    )
    new_manifest = _compile(durable_database, workspace_id, tmp_path)
    provider._background_answers.append(
        _single_posting_poll(new_manifest, posting_url, response_id="resp_scripted_1")
    )
    task_id = _background_task(durable_database, workspace_id, "one-task-two-campaigns")

    result = service.run(
        workspace_id,
        campaign_manifest_sha256=new_manifest.sha256,
        campaign_cycle=2,
        max_model_calls=4,
        max_hosted_searches=20,
        context=_FakeContext(task_id=task_id),
    )

    rows = _outcome_rows(durable_database, task_id)
    assert fetcher.calls == 2
    assert len(rows) == 1
    assert result["funnel"]["total_outcomes"] == 2


def test_a_cancelled_run_reports_the_funnel_rows_it_had_already_written(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A cancel after an admission commit still attests to what that task recorded."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    posting_url = "https://jobs.example.test/staff-data-engineer"
    context = _CancelDuringReadContext(
        task_id=_background_task(durable_database, workspace_id, "cancel-funnel")
    )

    result = GuidedCampaignService(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path / "artifacts"),
        posting_fetcher=_CountingPostingFetcher(),
        content_resolution=PostingContentResolutionService(
            durable_database,
            _CancellingResolver(context),
            storage=runtime_storage(durable_database, tmp_path / "artifacts"),
        ),
        provider=ScriptedProvider(
            script={}, background_answers=[_single_posting_poll(manifest, posting_url)]
        ),
    ).run(
        workspace_id,
        campaign_manifest_sha256=manifest.sha256,
        campaign_cycle=1,
        max_model_calls=4,
        max_hosted_searches=20,
        context=context,
    )

    rows = _outcome_rows(durable_database, context.task_id)
    assert result["cancelled"] is True
    assert rows
    assert result["funnel"]["total_outcomes"] == len(rows)


def test_a_run_cancelled_before_it_claims_a_campaign_reports_an_empty_funnel(
    durable_database: Database,
    tmp_path: Path,
    master_cv: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exit before the claim attests too, because a retry pass may already have written."""
    stranded = _strand_a_superseded_attempt(durable_database, tmp_path, master_cv, monkeypatch)
    task_id = _background_task(durable_database, stranded.workspace_id, "cancel-before-claim")
    next_manifest = _compile(durable_database, stranded.workspace_id, tmp_path)

    result = stranded.service.run(
        stranded.workspace_id,
        campaign_manifest_sha256=next_manifest.sha256,
        campaign_cycle=4,
        max_model_calls=4,
        max_hosted_searches=20,
        context=_CancellingContext(task_id=task_id),
    )

    assert result["campaign_id"] is None
    assert result["cancelled"] is True
    assert result["funnel"]["total_outcomes"] == len(_outcome_rows(durable_database, task_id))


def test_a_run_that_fails_after_admission_checkpoints_the_funnel_it_committed(
    durable_database: Database, tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed run leaves its last heartbeat as the only honest account of its coverage."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    posting_url = "https://jobs.example.test/staff-data-engineer"
    resolution = _content_resolution(durable_database, tmp_path)

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("retention failed after admission")

    # The real retention path publishes its own funnel-free progress before the read fails.
    monkeypatch.setattr(resolution.resolver, "resolve", fail)
    context = _RecordingContext(
        task_id=_background_task(durable_database, workspace_id, "funnel-failure")
    )

    with pytest.raises(RuntimeError, match="retention failed after admission"):
        GuidedCampaignService(
            durable_database,
            storage=runtime_storage(durable_database, tmp_path / "artifacts"),
            posting_fetcher=_CountingPostingFetcher(),
            content_resolution=resolution,
            provider=ScriptedProvider(
                script={}, background_answers=[_single_posting_poll(manifest, posting_url)]
            ),
        ).run(
            workspace_id,
            campaign_manifest_sha256=manifest.sha256,
            campaign_cycle=1,
            max_model_calls=4,
            max_hosted_searches=20,
            context=context,
        )

    rows = _outcome_rows(durable_database, context.task_id)
    assert len(rows) == 1
    # The worker replaces progress whole, so only the last payload survives on the task row.
    assert context.published[-1]["funnel"]["total_outcomes"] == len(rows)


def test_a_run_that_retains_content_keeps_the_funnel_as_its_last_heartbeat(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """Content resolution publishes progress of its own, and coverage must outlive it."""
    workspace_id = _workspace(durable_database, tmp_path, master_cv)
    manifest = _compile(durable_database, workspace_id, tmp_path)
    posting_url = "https://jobs.example.test/staff-data-engineer"
    context = _RecordingContext(
        task_id=_background_task(durable_database, workspace_id, "funnel-retention")
    )

    GuidedCampaignService(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path / "artifacts"),
        posting_fetcher=_CountingPostingFetcher(),
        content_resolution=_content_resolution(durable_database, tmp_path),
        provider=ScriptedProvider(
            script={}, background_answers=[_single_posting_poll(manifest, posting_url)]
        ),
    ).run(
        workspace_id,
        campaign_manifest_sha256=manifest.sha256,
        campaign_cycle=1,
        max_model_calls=4,
        max_hosted_searches=20,
        context=context,
    )

    rows = _outcome_rows(durable_database, context.task_id)
    assert len(rows) == 1
    assert any("funnel" not in payload for payload in context.published)
    assert context.published[-1]["funnel"]["total_outcomes"] == len(rows)


def test_a_retry_task_reports_the_funnel_rows_its_pass_wrote(
    durable_database: Database,
    tmp_path: Path,
    master_cv: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retry task type attests to its coverage the same way a campaign run does."""
    stranded = _strand_a_superseded_attempt(durable_database, tmp_path, master_cv, monkeypatch)
    task_id = _background_task(durable_database, stranded.workspace_id, "retry-funnel")
    queue = SimpleNamespace(get=lambda _: SimpleNamespace(workspace_id=stranded.workspace_id))

    result = GuidedCampaignRetryHandler(stranded.service)(
        cast(Any, _FakeContext(task_id=task_id, queue=queue)), {}
    )

    rows = _outcome_rows(durable_database, task_id)
    assert result["processed_attempts"] == 1
    assert result["funnel"]["total_outcomes"] == len(rows)
