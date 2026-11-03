"""Own every persistence transition for one guided campaign's execution.

Each transition is its own short transaction, conditional on the campaign
state's ``owner_task_id``, so a recovered lease from an earlier task can never
resume a campaign a newer task now owns.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.sql import ColumnElement

from clearscout.application.discovery.guided_campaign import CampaignManifest
from clearscout.application.discovery.guided_campaign_admission import (
    CAPTURE_ACTION_KINDS,
    AdmissionResult,
)
from clearscout.application.failures import ConflictError, NotFoundError
from clearscout.application.workspaces.spend import record_calls
from clearscout.domain import utc_now
from clearscout.domain.campaigns import (
    AttemptErrorCategory,
    AttemptState,
    BranchOutcome,
    CampaignReason,
    CampaignStatus,
    CapturedUrlOutcome,
)
from clearscout.domain.records import TaskStatus
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    BackgroundTask,
    GuidedCampaign,
    GuidedCampaignAdmittedDiscovery,
    GuidedCampaignAttempt,
    GuidedCampaignBranch,
    GuidedCampaignBranchOutcome,
    GuidedCampaignCapturedUrl,
    GuidedCampaignState,
    GuidedCampaignUrlObservation,
    PendingDiscovery,
)
from clearscout.infrastructure.providers.base import BackgroundPoll
from clearscout.shared.costs import Ledger

logger = logging.getLogger(__name__)

# A campaign is resumable only while a live task might still finish it; a
# paused campaign closes through an explicit abandon instead of resuming.
RESUMABLE_STATUSES = (CampaignStatus.RUNNING, CampaignStatus.FAILED, CampaignStatus.CANCELLED)
# Non-final statuses a fresh approval may close and supersede with a new campaign.
# RUNNING is never here: release_lease clears a dead owner to FAILED or PAUSED_UNCERTAIN.
SUPERSEDABLE_STATUSES = (
    CampaignStatus.FAILED,
    CampaignStatus.CANCELLED,
    CampaignStatus.PAUSED_UNCERTAIN,
)
MARK_DISPATCHED_RETRIES = 3
MARK_DISPATCHED_BACKOFF_SECONDS = 0.2


@dataclass(frozen=True)
class AttemptRecord:
    """One campaign attempt's persisted execution state."""

    id: str
    position: int
    state: str
    search_cap: int
    provider_response_id: str | None
    request_sha256: str | None
    # The request envelope carries the prompt text, including CV evidence.
    request_envelope: dict[str, Any] | None
    model_call_id: str | None
    hosted_search_count: int
    page_action_count: int
    finish: str
    branch_outcomes: tuple[tuple[str, str], ...]
    error_category: str


@dataclass(frozen=True)
class StrandedCampaign:
    """An incomplete campaign holding a captured attempt whose local processing failed."""

    id: str
    manifest: dict[str, Any]


_STRANDED_ATTEMPT = and_(
    GuidedCampaignAttempt.state == AttemptState.CAPTURED,
    GuidedCampaignAttempt.error_category == AttemptErrorCategory.PROCESSING_FAILED,
)


def stranded_attempt_count(session: Session, workspace_id: str) -> int:
    """Count the attempts a processing retry would claim in this workspace."""
    return (
        session.scalar(
            select(func.count())
            .select_from(GuidedCampaignAttempt)
            .join(
                GuidedCampaign,
                GuidedCampaign.id == GuidedCampaignAttempt.campaign_id,
            )
            .join(
                GuidedCampaignState,
                GuidedCampaignState.campaign_id == GuidedCampaign.id,
            )
            .where(
                GuidedCampaign.workspace_id == workspace_id,
                GuidedCampaignState.status == CampaignStatus.INCOMPLETE,
                _STRANDED_ATTEMPT,
            )
        )
        or 0
    )


@dataclass(frozen=True)
class SupersedableCampaign:
    """The workspace's one latest campaign, when a fresh approval may close and supersede it."""

    id: str
    cycle: int
    manifest_sha256: str
    manifest: dict[str, Any]
    status: str
    owner_task_id: str | None


def _owner_is(owner_task_id: str | None) -> ColumnElement[bool]:
    """Return the column condition matching an observed owner, null-safely."""
    if owner_task_id is None:
        return GuidedCampaignState.owner_task_id.is_(None)
    return GuidedCampaignState.owner_task_id == owner_task_id


def supersedable_campaign_in_session(
    session: Session, workspace_id: str
) -> SupersedableCampaign | None:
    """Return the workspace's latest campaign, only when its status makes it supersedable.

    An older failed campaign behind a newer completed one is never returned.
    """
    row = session.execute(
        select(GuidedCampaign, GuidedCampaignState)
        .join(
            GuidedCampaignState,
            GuidedCampaignState.campaign_id == GuidedCampaign.id,
        )
        .where(GuidedCampaign.workspace_id == workspace_id)
        .order_by(GuidedCampaign.created_at.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    campaign, state = row
    if state.status not in SUPERSEDABLE_STATUSES:
        return None
    return SupersedableCampaign(
        id=campaign.id,
        cycle=campaign.cycle,
        manifest_sha256=campaign.manifest_sha256,
        manifest=campaign.manifest,
        status=state.status,
        owner_task_id=state.owner_task_id,
    )


def _campaign_and_state(
    session: Session, campaign_id: str
) -> tuple[GuidedCampaign, GuidedCampaignState]:
    """Load one campaign and its mutable state, or raise NotFoundError."""
    campaign = session.get(GuidedCampaign, campaign_id)
    if campaign is None:
        raise NotFoundError(f"Campaign {campaign_id!r} does not exist.")
    state = session.scalar(
        select(GuidedCampaignState).where(GuidedCampaignState.campaign_id == campaign_id)
    )
    if state is None:
        raise NotFoundError(f"Campaign {campaign_id!r} has no execution state.")
    return campaign, state


def _require_ownership(state: GuidedCampaignState, task_id: str) -> None:
    """Refuse a transition from a task that no longer owns this campaign's lease."""
    if state.owner_task_id != task_id:
        raise ConflictError("Another task now owns this campaign; this task's lease is stale.")


def _load_attempt(session: Session, campaign_id: str, position: int) -> GuidedCampaignAttempt:
    """Load one attempt or raise NotFoundError."""
    attempt = session.scalar(
        select(GuidedCampaignAttempt).where(
            GuidedCampaignAttempt.campaign_id == campaign_id,
            GuidedCampaignAttempt.position == position,
        )
    )
    if attempt is None:
        raise NotFoundError(f"Attempt {position} does not exist on campaign {campaign_id!r}.")
    return attempt


def _update_attempt_if_owned(
    session: Session,
    campaign_id: str,
    position: int,
    task_id: str,
    values: dict[str, Any],
) -> GuidedCampaignAttempt:
    """Update one attempt's own columns, fenced by ownership in the same UPDATE statement."""
    result = session.connection().execute(
        sa_update(GuidedCampaignAttempt)
        .where(
            GuidedCampaignAttempt.campaign_id == campaign_id,
            GuidedCampaignAttempt.position == position,
            exists().where(
                GuidedCampaignState.campaign_id == campaign_id,
                GuidedCampaignState.owner_task_id == task_id,
            ),
        )
        .values(**values)
    )
    if result.rowcount == 0:
        # A missing attempt and a stale lease both refuse the write; tell them
        # apart so the caller's error names the actual cause.
        _load_attempt(session, campaign_id, position)
        raise ConflictError("Another task now owns this campaign; this task's lease is stale.")
    session.flush()
    session.expire_all()
    return _load_attempt(session, campaign_id, position)


def _branch_outcome_row(
    branch_key: str,
    queries: tuple[str, ...],
    record: Any,
    poll: BackgroundPoll,
    has_unattributable_search: bool,
) -> dict[str, Any]:
    """Classify one branch's outcome for this attempt from its matched queries and status."""
    if queries:
        outcome = BranchOutcome.SEARCHED
    elif has_unattributable_search:
        outcome = BranchOutcome.UNATTRIBUTABLE
    elif record is None or record.finish.value == "truncated" or poll.status != "complete":
        outcome = BranchOutcome.TRUNCATED
    else:
        outcome = BranchOutcome.UNVISITED
    return {"branch_key": branch_key, "outcome": outcome, "matched_queries": list(queries)}


def _upsert_captured_url(
    session: Session, campaign_id: str, attempt_id: str, item: dict[str, Any]
) -> None:
    """Insert or update one captured-URL row, then record its observations for this attempt."""
    captured = session.scalar(
        select(GuidedCampaignCapturedUrl).where(
            GuidedCampaignCapturedUrl.campaign_id == campaign_id,
            GuidedCampaignCapturedUrl.url_sha256 == item["url_sha256"],
        )
    )
    if captured is None:
        captured = GuidedCampaignCapturedUrl(
            campaign_id=campaign_id,
            url_sha256=item["url_sha256"],
            url=item["url"],
            classification=item["classification"],
            model_selected=item.get("model_selected", False),
            model_why=item.get("model_why", ""),
        )
        session.add(captured)
        session.flush()
    else:
        # A later attempt bringing new search or page evidence, or
        # a fresh model pick, reopens a URL only a model had named
        # so far; an already-processed URL keeps its outcome.
        reopens = captured.outcome == CapturedUrlOutcome.UNRESOLVED and (
            any(
                observation["action_kind"] in CAPTURE_ACTION_KINDS
                for observation in item.get("observations", ())
            )
            or (item.get("model_selected", False) and not captured.model_selected)
        )
        if item.get("model_selected", False):
            captured.model_selected = True
            captured.model_why = item.get("model_why", captured.model_why)
        if reopens:
            captured.outcome = CapturedUrlOutcome.PENDING_READ
    for observation in item.get("observations", ()):
        session.add(
            GuidedCampaignUrlObservation(
                captured_url_id=captured.id,
                attempt_id=attempt_id,
                action_kind=observation["action_kind"],
                query=observation.get("query", ""),
            )
        )


class GuidedCampaignLedger:
    """The one owner of every guided-campaign persistence transition."""

    def __init__(self, database: Database) -> None:
        """Bind the ledger to the database it writes every transition against."""
        self.database = database

    def create_or_claim(
        self,
        workspace_id: str,
        manifest: CampaignManifest,
        cycle: int,
        task_id: str,
        request_snapshot: dict[str, Any],
    ) -> str:
        """Load an existing campaign and claim its lease, or create one, atomically."""
        with self.database.exclusive_session() as session:
            campaign = session.scalar(
                select(GuidedCampaign).where(
                    GuidedCampaign.workspace_id == workspace_id,
                    GuidedCampaign.manifest_sha256 == manifest.sha256,
                    GuidedCampaign.cycle == cycle,
                )
            )
            if campaign is None:
                return self._create_campaign(
                    session, workspace_id, manifest, cycle, task_id, request_snapshot
                )
            state = session.scalar(
                select(GuidedCampaignState).where(GuidedCampaignState.campaign_id == campaign.id)
            )
            if state is None:
                raise NotFoundError(f"Campaign {campaign.id!r} has no execution state.")
            if state.status == CampaignStatus.PAUSED_UNCERTAIN:
                raise ConflictError(
                    f"Campaign {campaign.id!r} is paused on an uncertain attempt; "
                    "abandon it or approve a new campaign."
                )
            if state.status not in RESUMABLE_STATUSES:
                raise ConflictError(f"Campaign {campaign.id!r} is {state.status} and final.")
            state.status = CampaignStatus.RUNNING
            state.owner_task_id = task_id
            session.flush()
            return campaign.id

    def stored_campaign(
        self, workspace_id: str, manifest_sha256: str, cycle: int
    ) -> GuidedCampaign | None:
        """Load the immutable campaign matching one approved task payload."""
        with self.database.session() as session:
            return session.scalar(
                select(GuidedCampaign).where(
                    GuidedCampaign.workspace_id == workspace_id,
                    GuidedCampaign.manifest_sha256 == manifest_sha256,
                    GuidedCampaign.cycle == cycle,
                )
            )

    def _create_campaign(
        self,
        session: Session,
        workspace_id: str,
        manifest: CampaignManifest,
        cycle: int,
        task_id: str,
        request_snapshot: dict[str, Any],
    ) -> str:
        """Insert one campaign with its immutable branches and planned attempts."""
        campaign = GuidedCampaign(
            workspace_id=workspace_id,
            search_parameter_set_id=manifest.identities.search_parameter_set_id,
            master_cv_version_id=manifest.identities.master_cv_version_id,
            manifest_sha256=manifest.sha256,
            cycle=cycle,
            compiler_version=manifest.compiler_version,
            manifest={**manifest.document(), "request_snapshot": request_snapshot},
            max_model_calls=manifest.bounds.max_model_calls,
            max_hosted_searches=manifest.bounds.max_hosted_searches,
            max_output_tokens=manifest.bounds.max_output_tokens,
            searches_per_attempt=manifest.bounds.searches_per_attempt,
            approved_task_id=task_id,
        )
        session.add(campaign)
        session.flush()
        attempt_position_by_branch_key = {
            key: attempt.position for attempt in manifest.attempts for key in attempt.branch_keys
        }
        for position, branch in enumerate(manifest.branches, 1):
            session.add(
                GuidedCampaignBranch(
                    campaign_id=campaign.id,
                    position=position,
                    branch_key=branch.key,
                    kind=branch.kind.value,
                    priority_family=branch.priority_family,
                    protected=branch.protected,
                    attempt_position=attempt_position_by_branch_key.get(branch.key),
                    document=branch.describe(),
                )
            )
        for attempt in manifest.attempts:
            session.add(
                GuidedCampaignAttempt(
                    campaign_id=campaign.id,
                    position=attempt.position,
                    state=AttemptState.PLANNED,
                    search_cap=attempt.search_cap,
                )
            )
        session.add(
            GuidedCampaignState(
                campaign_id=campaign.id, status=CampaignStatus.RUNNING, owner_task_id=task_id
            )
        )
        session.flush()
        return campaign.id

    def supersedable_campaign(self, workspace_id: str) -> SupersedableCampaign | None:
        """Return the workspace's latest campaign a fresh approval may close and supersede."""
        with self.database.session() as session:
            return supersedable_campaign_in_session(session, workspace_id)

    def claim_for_supersede(
        self,
        campaign_id: str,
        task_id: str,
        *,
        observed_status: str,
        observed_owner_task_id: str | None,
    ) -> bool:
        """Claim a stale campaign's lease, fenced against a concurrent claim.

        Returns False, writing nothing, when the observed status or owner no longer matches.
        """
        return self._claim_lease(
            campaign_id,
            task_id,
            GuidedCampaignState.status == observed_status,
            _owner_is(observed_owner_task_id),
            status=CampaignStatus.RUNNING,
        )

    def claim_for_processing_retry(self, campaign_id: str, task_id: str) -> bool:
        """Claim a closed campaign's lease to reprocess it, leaving its status unchanged.

        A lease held by a task that has not finished is never taken.
        """
        live_owner = exists().where(
            BackgroundTask.id == GuidedCampaignState.owner_task_id,
            BackgroundTask.status.not_in(
                (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)
            ),
        )
        return self._claim_lease(
            campaign_id,
            task_id,
            GuidedCampaignState.status == CampaignStatus.INCOMPLETE,
            or_(GuidedCampaignState.owner_task_id.is_(None), ~live_owner),
        )

    def _claim_lease(
        self,
        campaign_id: str,
        task_id: str,
        *conditions: ColumnElement[bool],
        status: str | None = None,
    ) -> bool:
        """Take one campaign's lease in a single fenced UPDATE; False when it did not match."""
        values: dict[str, Any] = {"owner_task_id": task_id}
        if status is not None:
            values["status"] = status
        with self.database.exclusive_session() as session:
            result = session.connection().execute(
                sa_update(GuidedCampaignState)
                .where(GuidedCampaignState.campaign_id == campaign_id, *conditions)
                .values(**values)
            )
            return result.rowcount > 0

    def stranded_campaigns(self, workspace_id: str) -> tuple[StrandedCampaign, ...]:
        """Return incomplete campaigns whose captured attempts wait on a processing retry."""
        with self.database.session() as session:
            rows = session.scalars(
                select(GuidedCampaign)
                .join(
                    GuidedCampaignState,
                    GuidedCampaignState.campaign_id == GuidedCampaign.id,
                )
                .where(
                    GuidedCampaign.workspace_id == workspace_id,
                    GuidedCampaignState.status == CampaignStatus.INCOMPLETE,
                    exists().where(
                        GuidedCampaignAttempt.campaign_id == GuidedCampaign.id,
                        _STRANDED_ATTEMPT,
                    ),
                )
                .order_by(GuidedCampaign.created_at)
            ).all()
            return tuple(StrandedCampaign(id=row.id, manifest=row.manifest) for row in rows)

    def record_processing_failure(
        self, campaign_id: str, position: int, task_id: str, error: str
    ) -> None:
        """Keep a captured attempt resumable after its local processing failed."""
        with self.database.session() as session:
            _update_attempt_if_owned(
                session,
                campaign_id,
                position,
                task_id,
                {
                    "error_category": AttemptErrorCategory.PROCESSING_FAILED,
                    "error_message": error[:4000],
                },
            )

    def release_lease(
        self, campaign_id: str, task_id: str, *, restore_status: str | None = None
    ) -> None:
        """Release this task's lease, fenced by ownership, after an unhandled failure.

        A paused-uncertain campaign keeps that status; every other status becomes failed,
        unless ``restore_status`` puts back the status a supersede claim replaced.
        """
        with self.database.session() as session:
            _, state = _campaign_and_state(session, campaign_id)
            if state.owner_task_id != task_id:
                return
            if restore_status is not None:
                state.status = restore_status
            elif state.status != CampaignStatus.PAUSED_UNCERTAIN:
                state.status = CampaignStatus.FAILED
            state.owner_task_id = None
            session.flush()

    def attempts(self, campaign_id: str) -> tuple[AttemptRecord, ...]:
        """Return every attempt's current state, in position order."""
        with self.database.session() as session:
            rows = session.scalars(
                select(GuidedCampaignAttempt)
                .where(GuidedCampaignAttempt.campaign_id == campaign_id)
                .order_by(GuidedCampaignAttempt.position)
            ).all()
            return tuple(_attempt_record(row) for row in rows)

    def mark_dispatching(
        self, campaign_id: str, position: int, task_id: str, request_sha256: str
    ) -> None:
        """Record that this task is about to create this attempt's provider request."""
        with self.database.session() as session:
            _update_attempt_if_owned(
                session,
                campaign_id,
                position,
                task_id,
                {"state": AttemptState.DISPATCHING, "request_sha256": request_sha256},
            )

    def mark_not_created(
        self,
        campaign_id: str,
        position: int,
        task_id: str,
        error: str,
        *,
        error_category: AttemptErrorCategory,
    ) -> None:
        """Return an attempt to planned when the provider certainly made no response."""
        with self.database.session() as session:
            _update_attempt_if_owned(
                session,
                campaign_id,
                position,
                task_id,
                {
                    "state": AttemptState.PLANNED,
                    "request_sha256": None,
                    "dispatched_at": None,
                    "error_category": error_category,
                    "error_message": error[:4000],
                },
            )
            _, state = _campaign_and_state(session, campaign_id)
            state.status = CampaignStatus.FAILED
            state.owner_task_id = None
            session.flush()

    def mark_dispatched(
        self,
        campaign_id: str,
        position: int,
        task_id: str,
        response_id: str,
        request_sha256: str,
        *,
        request_envelope: dict[str, Any] | None,
    ) -> None:
        """Record the provider's response id before any poll, retrying a transient database busy.

        A billed response with no recorded id would be unrecoverable. The request
        envelope is held until capture writes it to the model call's envelope row.
        """
        for attempt in range(MARK_DISPATCHED_RETRIES):
            try:
                with self.database.session() as session:
                    _update_attempt_if_owned(
                        session,
                        campaign_id,
                        position,
                        task_id,
                        {
                            "state": AttemptState.DISPATCHED,
                            "provider_response_id": response_id,
                            "request_sha256": request_sha256,
                            "request_envelope": request_envelope,
                            "dispatched_at": utc_now(),
                            "error_category": "",
                            "error_message": "",
                        },
                    )
                return
            except OperationalError:
                if attempt == MARK_DISPATCHED_RETRIES - 1:
                    raise
                time.sleep(MARK_DISPATCHED_BACKOFF_SECONDS * (attempt + 1))

    def mark_uncertain(
        self,
        campaign_id: str,
        position: int,
        task_id: str,
        error: str,
        *,
        error_category: AttemptErrorCategory | None = None,
    ) -> None:
        """Pause the campaign because an attempt left an unknown provider-side state.

        Keeps an existing ``dispatched_at``, setting it only if the attempt never reached one.
        """
        with self.database.session() as session:
            attempt = _load_attempt(session, campaign_id, position)
            values: dict[str, Any] = {
                "state": AttemptState.UNCERTAIN,
                "error_category": error_category or "",
                "error_message": error[:4000],
            }
            if attempt.dispatched_at is None:
                values["dispatched_at"] = utc_now()
            _update_attempt_if_owned(session, campaign_id, position, task_id, values)
            _, state = _campaign_and_state(session, campaign_id)
            state.status = CampaignStatus.PAUSED_UNCERTAIN
            session.flush()

    @staticmethod
    def abandon_uncertain_in_session(session: Session, campaign_id: str, position: int) -> None:
        """Abandon an uncertain attempt and close the campaign incomplete; it never resumes."""
        _, state = _campaign_and_state(session, campaign_id)
        if state.status != CampaignStatus.PAUSED_UNCERTAIN:
            raise ConflictError(f"Campaign {campaign_id!r} is {state.status}, not paused.")
        attempt = _load_attempt(session, campaign_id, position)
        if attempt.state != AttemptState.UNCERTAIN:
            raise ConflictError(f"Attempt {position} is {attempt.state}, not uncertain.")
        attempt.state = AttemptState.ABANDONED
        session.flush()
        GuidedCampaignLedger._write_branch_outcomes_in(session, campaign_id)
        attempts = session.scalars(
            select(GuidedCampaignAttempt).where(GuidedCampaignAttempt.campaign_id == campaign_id)
        ).all()
        reasons = [CampaignReason.ABANDONED_ATTEMPT.value]
        if any(
            item.state not in (AttemptState.PROCESSED, AttemptState.ABANDONED) for item in attempts
        ):
            reasons.append(CampaignReason.INCOMPLETE_ATTEMPTS.value)
        state.status = CampaignStatus.INCOMPLETE
        state.reasons = reasons
        state.completed_at = utc_now()
        state.owner_task_id = None
        session.flush()

    def capture(
        self,
        campaign_id: str,
        position: int,
        task_id: str,
        *,
        workspace_id: str,
        poll: BackgroundPoll,
        ledger: Ledger,
        provider: str,
        branch_queries: dict[str, tuple[str, ...]],
        has_unattributable_search: bool,
        captured_urls: tuple[dict[str, Any], ...],
        refused_model_picks: tuple[dict[str, str], ...] = (),
    ) -> None:
        """Write the model call, cost, and captured URL evidence in one transaction."""
        with self.database.session() as session:
            created = record_calls(
                session,
                workspace_id=workspace_id,
                task_id=task_id,
                ledger=ledger,
                provider=provider,
                origin="search.guided_campaign",
            )
            record = poll.record
            branch_outcomes = [
                _branch_outcome_row(branch_key, queries, record, poll, has_unattributable_search)
                for branch_key, queries in branch_queries.items()
            ]
            attempt = _update_attempt_if_owned(
                session,
                campaign_id,
                position,
                task_id,
                {
                    "state": AttemptState.CAPTURED,
                    "model_call_id": created[-1].id if created else None,
                    "hosted_search_count": record.search_count if record is not None else 0,
                    "page_action_count": record.page_action_count if record is not None else 0,
                    "finish": record.finish.value if record is not None else "",
                    "validated": poll.status == "complete",
                    "request_envelope": None,
                    "capture": {
                        "branch_outcomes": branch_outcomes,
                        "selected_url_sha256s": sorted(
                            item["url_sha256"]
                            for item in captured_urls
                            if item.get("model_selected", False)
                        ),
                        "board_url_sha256s": sorted(
                            item["url_sha256"]
                            for item in captured_urls
                            if item.get("model_board", False)
                        ),
                        "invalid_model_urls": len(refused_model_picks),
                        "refused_model_picks": list(refused_model_picks),
                    },
                    "captured_at": utc_now(),
                },
            )
            for item in captured_urls:
                _upsert_captured_url(session, campaign_id, attempt.id, item)
            session.flush()

    @staticmethod
    def record_admissions_in_session(
        session: Session,
        campaign_id: str,
        position: int,
        task_id: str,
        admissions: tuple[AdmissionResult, ...],
    ) -> None:
        """Record capture outcomes and every discovery this attempt staged, in the staging tx."""
        _, state = _campaign_and_state(session, campaign_id)
        _require_ownership(state, task_id)
        attempt = _load_attempt(session, campaign_id, position)
        for result in admissions:
            captured = session.scalar(
                select(GuidedCampaignCapturedUrl).where(
                    GuidedCampaignCapturedUrl.campaign_id == campaign_id,
                    GuidedCampaignCapturedUrl.url_sha256 == result.url_sha256,
                )
            )
            if captured is None:
                continue
            for pending_id in dict.fromkeys(result.pending_discovery_ids):
                session.add(
                    GuidedCampaignAdmittedDiscovery(
                        attempt_id=attempt.id,
                        captured_url_id=captured.id,
                        pending_discovery_id=pending_id,
                    )
                )
            captured.classification = result.classification
            captured.model_selected = result.model_selected
            captured.model_why = result.model_why
            captured.outcome = result.outcome
            captured.staged_basis = result.staged_basis
            # The report's finding link names only a posting this URL itself staged.
            captured.pending_discovery_id = (
                result.pending_discovery_ids[0]
                if result.outcome == CapturedUrlOutcome.STAGED and result.pending_discovery_ids
                else None
            )
            captured.source_id = result.source_id
            captured.detail = result.detail
            captured.robots_basis = result.robots_basis

    def admitted_discoveries(self, campaign_id: str, position: int) -> tuple[tuple[str, str], ...]:
        """Return (captured URL sha256, pending id) for this attempt's still-active staged rows.

        An archived or withdrawn row needs no retained body, so a retry skips it.
        """
        with self.database.session() as session:
            attempt = _load_attempt(session, campaign_id, position)
            rows = session.execute(
                select(
                    GuidedCampaignCapturedUrl.url_sha256,
                    GuidedCampaignAdmittedDiscovery.pending_discovery_id,
                )
                .join(
                    GuidedCampaignCapturedUrl,
                    GuidedCampaignCapturedUrl.id == GuidedCampaignAdmittedDiscovery.captured_url_id,
                )
                .join(
                    PendingDiscovery,
                    PendingDiscovery.id == GuidedCampaignAdmittedDiscovery.pending_discovery_id,
                )
                .where(
                    GuidedCampaignAdmittedDiscovery.attempt_id == attempt.id,
                    PendingDiscovery.archived_at.is_(None),
                    PendingDiscovery.withdrawn_at.is_(None),
                )
            ).all()
            return tuple((url_sha256, pending_id) for url_sha256, pending_id in rows)

    @staticmethod
    def mark_processed_in_session(
        session: Session,
        campaign_id: str,
        position: int,
        task_id: str,
    ) -> None:
        """Mark an attempt processed only after its dependent work completed."""
        _update_attempt_if_owned(
            session,
            campaign_id,
            position,
            task_id,
            {
                "state": AttemptState.PROCESSED,
                "processed_at": utc_now(),
                "error_category": "",
                "error_message": "",
            },
        )

    def finish(self, campaign_id: str, task_id: str, *, status: str, reasons: list[str]) -> None:
        """Set the campaign's terminal or paused status once execution has stopped."""
        with self.database.session() as session:
            _, state = _campaign_and_state(session, campaign_id)
            _require_ownership(state, task_id)
            if status in (CampaignStatus.COMPLETED, CampaignStatus.INCOMPLETE):
                self._write_branch_outcomes_in(session, campaign_id)
            state.status = status
            state.reasons = reasons
            if status in (
                CampaignStatus.COMPLETED,
                CampaignStatus.INCOMPLETE,
                CampaignStatus.CANCELLED,
            ):
                state.completed_at = utc_now()
                state.owner_task_id = None
            session.flush()

    @staticmethod
    def _write_branch_outcomes_in(session: Session, campaign_id: str) -> None:
        """Materialize captured branch evidence only when the campaign is final."""
        branches = {
            branch.branch_key: branch
            for branch in session.scalars(
                select(GuidedCampaignBranch).where(GuidedCampaignBranch.campaign_id == campaign_id)
            )
        }
        attempts = session.scalars(
            select(GuidedCampaignAttempt).where(GuidedCampaignAttempt.campaign_id == campaign_id)
        ).all()
        for attempt in attempts:
            for item in attempt.capture.get("branch_outcomes", []):
                branch = branches.get(item.get("branch_key"))
                if branch is None:
                    continue
                session.add(
                    GuidedCampaignBranchOutcome(
                        branch_id=branch.id,
                        attempt_id=attempt.id,
                        outcome=item["outcome"],
                        matched_queries=item["matched_queries"],
                    )
                )


def _attempt_record(row: GuidedCampaignAttempt) -> AttemptRecord:
    """Project one ORM attempt row to its immutable read record."""
    return AttemptRecord(
        id=row.id,
        position=row.position,
        state=row.state,
        search_cap=row.search_cap,
        provider_response_id=row.provider_response_id,
        request_sha256=row.request_sha256,
        request_envelope=row.request_envelope,
        model_call_id=row.model_call_id,
        hosted_search_count=row.hosted_search_count,
        page_action_count=row.page_action_count,
        finish=row.finish,
        branch_outcomes=tuple(
            (str(item["branch_key"]), str(item["outcome"]))
            for item in (row.capture or {}).get("branch_outcomes", [])
        ),
        error_category=row.error_category,
    )
