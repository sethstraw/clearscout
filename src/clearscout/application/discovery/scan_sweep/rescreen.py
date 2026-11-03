"""Re-judging the pending queue's stored facts against current screening rules."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import select

from clearscout.application.discovery.content_resolution import (
    CurrentPostingContentReader,
    PostingContentResolutionService,
    body_pay,
    rescreened_body_refusal,
    with_body_pay,
)
from clearscout.application.discovery.dispositions import (
    STRUCTURED_HOLDOUT_SUFFIX,
    DiscoveryDispositionService,
    dismissed_pending_ids,
)
from clearscout.application.discovery.pending import (
    active_task_target_ids,
    dismissed_pending_discovery_ids,
    source_employer_names,
    visible_pending_discovery_filters,
)
from clearscout.application.discovery.refusal_patterns import restamp_refusals
from clearscout.application.discovery.screening import (
    DiscoveryScreening,
    ScreeningCode,
    screen_discovery,
    structured_holdout,
)
from clearscout.application.failures import NotFoundError, ValidationError
from clearscout.application.tailoring import fit
from clearscout.application.tasks import TaskContext
from clearscout.application.workspaces.parameters import parameters_in
from clearscout.domain.search_parameters import JobSearchParameters
from clearscout.domain.staged_facts import StagedListingFacts
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import PendingDiscovery
from clearscout.shared.errors import ClearscoutError


def screening_snapshot(screening: DiscoveryScreening) -> dict[str, Any]:
    """Serialize the current scan screening for the appended dismissal."""
    return {
        "standing": screening.standing,
        "findings": [
            {
                "dimension": finding.dimension,
                "standing": finding.standing,
                "code": finding.code,
                "subject": finding.subject,
            }
            for finding in screening.findings
        ],
    }


def pending_row_failure(pending_id: str, error: Exception) -> dict[str, str]:
    """Record one pending row a whole-queue task could not finish, and why."""
    return {
        "pending_discovery_id": pending_id,
        "error_category": type(error).__name__,
        "error_message": str(error),
    }


@dataclass
class PendingQueueRescreenOutcome:
    """What re-judging each active pending row against current screening rules did."""

    total: int = 0
    held_out_ids: list[str] = field(default_factory=list)
    unchanged: int = 0
    vanished_ids: list[str] = field(default_factory=list)
    skipped_ids: list[str] = field(default_factory=list)
    failures: list[dict[str, str]] = field(default_factory=list)
    restored_ids: list[str] = field(default_factory=list)

    def as_result(self) -> dict[str, Any]:
        """Return the task result a caller reads, with ids for every set a row may land in."""
        return {
            "total": self.total,
            "screened": len(self.held_out_ids) + self.unchanged,
            "held_out": len(self.held_out_ids),
            "held_out_ids": self.held_out_ids,
            "unchanged": self.unchanged,
            "vanished": len(self.vanished_ids),
            "vanished_ids": self.vanished_ids,
            "skipped_for_paid_task": len(self.skipped_ids),
            "skipped_ids": self.skipped_ids,
            "failed": len(self.failures),
            "failures": self.failures,
            "restored": len(self.restored_ids),
            "restored_ids": self.restored_ids,
        }


class PendingQueueRescreen:
    """Re-judge visible and rule-dismissed pending rows from what staging retained."""

    def __init__(
        self,
        database: Database,
        content_resolution: PostingContentResolutionService | None,
        current_content: CurrentPostingContentReader | None,
        collect_targets: Callable[[str, JobSearchParameters], list[str]],
    ) -> None:
        """Bind the rescreen to the retained bodies and the titles a sweep searches under."""
        self.database = database
        self.content_resolution = content_resolution
        self.current_content = current_content
        self.collect_targets = collect_targets

    def rescreen_pending_queue(
        self, workspace_id: str, context: TaskContext, *, actor: str, origin: str
    ) -> PendingQueueRescreenOutcome:
        """Re-judge every visible pending row's stored facts without any network fetch.

        Reads only what staging already retained: no publisher is contacted, and
        a row is never un-dismissed or archived. A row whose listing states no pay
        takes the pay its retained body states. A row the current rules now refuse
        gets one more appended, individually restorable hold-out.
        """
        with self.database.session() as session:
            parameters = parameters_in(session, workspace_id)
            locations = dict(
                session.execute(
                    select(PendingDiscovery.id, PendingDiscovery.location)
                    .where(
                        *visible_pending_discovery_filters(
                            workspace_id, dismissed_pending_discovery_ids(workspace_id)
                        )
                    )
                    .order_by(PendingDiscovery.id)
                ).all()
            )
        pending_ids = list(locations)
        searched = self.collect_targets(workspace_id, parameters)
        dispositions = DiscoveryDispositionService(self.database)
        outcome = PendingQueueRescreenOutcome(total=len(pending_ids))
        for position, pending_id in enumerate(pending_ids, start=1):
            context.raise_if_cancelled()
            context.progress(
                "rescreening",
                {
                    "current": position,
                    "total": len(pending_ids),
                    "held_out": len(outcome.held_out_ids),
                    "pending_discovery_id": pending_id,
                },
            )
            try:
                verdict = self._rescreen_row(
                    workspace_id,
                    pending_id,
                    parameters,
                    searched,
                    dispositions,
                    listing_location=locations[pending_id],
                    actor=actor,
                    origin=origin,
                    correlation_id=context.task_id,
                )
            except NotFoundError:
                outcome.vanished_ids.append(pending_id)
            except ClearscoutError as error:
                outcome.failures.append(pending_row_failure(pending_id, error))
            else:
                if verdict == "held_out":
                    outcome.held_out_ids.append(pending_id)
                elif verdict == "skipped":
                    outcome.skipped_ids.append(pending_id)
                else:
                    outcome.unchanged += 1
        self._restore_rescreened(
            workspace_id, parameters, searched, dispositions, outcome, context, actor=actor
        )
        return outcome

    def _restore_rescreened(
        self,
        workspace_id: str,
        parameters: JobSearchParameters,
        searched: list[str],
        dispositions: DiscoveryDispositionService,
        outcome: PendingQueueRescreenOutcome,
        context: TaskContext,
        *,
        actor: str,
    ) -> None:
        """Return to the queue each rule dismissal the current rules no longer make."""
        with self.database.session() as session:
            dismissed = sorted(
                session.scalars(
                    select(PendingDiscovery.id).where(
                        PendingDiscovery.id.in_(dismissed_pending_ids(session, workspace_id)),
                        PendingDiscovery.archived_at.is_(None),
                        PendingDiscovery.withdrawn_at.is_(None),
                    )
                )
            )
        for pending_id in dismissed:
            context.raise_if_cancelled()
            try:
                if self._rescreen_dismissed_row(
                    workspace_id,
                    pending_id,
                    parameters,
                    searched,
                    dispositions,
                    actor=actor,
                    correlation_id=context.task_id,
                ):
                    outcome.restored_ids.append(pending_id)
            except ClearscoutError as error:
                outcome.failures.append(pending_row_failure(pending_id, error))

    def _rescreen_dismissed_row(
        self,
        workspace_id: str,
        pending_id: str,
        parameters: JobSearchParameters,
        searched: list[str],
        dispositions: DiscoveryDispositionService,
        *,
        actor: str,
        correlation_id: str,
    ) -> bool:
        """Judge one rule-dismissed row again and restore it when nothing refuses it now."""
        description, _evidence_id = self._retained_body(workspace_id, pending_id)
        with self.database.session() as session:
            judged = dispositions.rescreen_restorable_in_session(session, workspace_id, pending_id)
            pending = session.get(PendingDiscovery, pending_id)
            if judged is None or pending is None:
                return False
            # Only a place hold-out reads no body; any other refusal needs the body it read.
            place_only = judged.origin.endswith(STRUCTURED_HOLDOUT_SUFFIX) and (
                judged.category == ScreeningCode.LOCATION_OUTSIDE.value
            )
            if not description and not place_only:
                return False
            facts = StagedListingFacts.from_json(pending.staged_facts)
            screening = screen_discovery(
                pending.employer,
                pending.title,
                pending.location,
                parameters,
                searched,
                facts,
                source_employer=source_employer_names(session, [pending.source_id]).get(
                    pending.source_id or "", ""
                ),
            )
            if structured_holdout(facts, pending.location, screening, parameters, description) or (
                description
                and rescreened_body_refusal(
                    pending,
                    description.encode(),
                    workspace_id,
                    self.database,
                    judged.screening or {},
                )
            ):
                restamp_refusals(session, workspace_id, pending_id, judged.category)
                return False
            judged_id, category = judged.id, judged.category
        with self.database.exclusive_session() as session:
            current = dispositions.rescreen_restorable_in_session(session, workspace_id, pending_id)
            if current is None or current.id != judged_id:
                return False
            if active_task_target_ids(session, workspace_id, [pending_id]):
                return False
            return bool(
                dispositions.restore_rescreened_in_session(
                    session,
                    workspace_id,
                    pending_id,
                    category=category,
                    actor=actor,
                    correlation_id=correlation_id,
                )
            )

    def _rescreen_row(
        self,
        workspace_id: str,
        pending_id: str,
        parameters: JobSearchParameters,
        searched: list[str],
        dispositions: DiscoveryDispositionService,
        *,
        listing_location: str,
        actor: str,
        origin: str,
        correlation_id: str,
    ) -> Literal["held_out", "unchanged", "skipped"]:
        """Judge one row in its own transaction, so a later failure keeps earlier hold-outs."""
        description, evidence_id = self._retained_body(workspace_id, pending_id)
        pay = (
            body_pay(
                fit.read(description, parameters=parameters, listing_location=listing_location)
            )
            if description
            else ()
        )
        # The hold-out depends on the latest disposition read here, so no person's
        # action may commit between that read and the append.
        with self.database.exclusive_session() as session:
            pending = session.scalar(
                select(PendingDiscovery).where(
                    PendingDiscovery.id == pending_id,
                    *visible_pending_discovery_filters(
                        workspace_id, dismissed_pending_discovery_ids(workspace_id)
                    ),
                )
            )
            if pending is None:
                raise NotFoundError(f"Pending discovery {pending_id!r} left the active queue.")
            # A row a paid task is running against is left alone, the same guard the
            # queue reset applies, so a review cannot lose its subject mid-task.
            if active_task_target_ids(session, workspace_id, [pending_id]):
                return "skipped"
            # Pay read against a body or place the row has since replaced is stale.
            if evidence_id is not None and (evidence_id, listing_location) == (
                pending.current_content_id,
                pending.location,
            ):
                staged = StagedListingFacts.from_json(pending.staged_facts)
                pending.staged_facts = with_body_pay(staged, pay).as_json()
            facts = StagedListingFacts.from_json(pending.staged_facts)
            screening = screen_discovery(
                pending.employer,
                pending.title,
                pending.location,
                parameters,
                searched,
                facts,
                source_employer=source_employer_names(session, [pending.source_id]).get(
                    pending.source_id or "", ""
                ),
            )
            holdout = structured_holdout(
                facts, pending.location, screening, parameters, description
            )
            if holdout is None:
                return "unchanged"
            dismissal = dispositions.dismiss_structured_holdout_in_session(
                session,
                workspace_id,
                pending_id,
                category=holdout.finding.code.value,
                reason=(
                    "Re-screened from stored publisher facts against current search "
                    "rules. The dismissal remains individually restorable."
                ),
                actor=actor,
                origin=origin,
                correlation_id=correlation_id,
                screening_override=screening_snapshot(holdout.screening),
            )
        return "held_out" if dismissal.events else "unchanged"

    def _retained_body(self, workspace_id: str, pending_id: str) -> tuple[str, str | None]:
        """Decode one row's verified retained body and its evidence id, or "" and none.

        Unreadable retained evidence reads as no body, which can only withhold a
        hold-out, so one corrupt artifact cannot fail a whole-queue re-screen.
        """
        reader = self.current_content or (
            self.content_resolution.current if self.content_resolution is not None else None
        )
        if reader is None:
            return "", None
        try:
            current = reader.read(workspace_id, pending_id)
        except ValidationError:
            return "", None
        if current is None:
            return "", None
        return current.body.decode("utf-8", errors="replace"), current.evidence_id
