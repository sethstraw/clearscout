"""Preview and append reversible dispositions for pending discoveries."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal, cast

from sqlalchemy import exists, select

from tailorcv.application.discovery.pending import (
    clear_pending_markers,
    latest_disposition_statement,
    marked_ids,
    move_pending_markers,
    release_families,
    root_discovery_filters,
    source_employer_names,
    transfer_pending_closing_date,
)
from tailorcv.application.discovery.scan_staging.reasons import staged_by_person
from tailorcv.application.discovery.screening import ScreeningStanding, screen_discovery
from tailorcv.application.failures import ConflictError, NotFoundError, ValidationError
from tailorcv.application.job_search.models import (
    DismissedDiscoveryResponse,
    DispositionApply,
    DispositionPreview,
    DispositionSelection,
    DispositionTargetResponse,
    PendingDispositionResponse,
    PendingDuplicateCandidateResponse,
)
from tailorcv.application.workspaces.parameters import parameters_in
from tailorcv.domain import new_id, utc_now
from tailorcv.domain.search_parameters import JobSearchParameters
from tailorcv.domain.staged_facts import StagedListingFacts
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    JobPosting,
    JobSearchParameterSet,
    PendingDiscovery,
    PendingDiscoveryDispositionEvent,
)

# Every category a verified publisher body may append as a dismissal, and the
# only categories an automatic body restore may supersede.
BODY_REFUSAL_CATEGORIES = frozenset(
    {
        "location_outside",
        "location_residence_required",
        "arrangement_refused",
        "arrangement_office_limit",
        "travel_over_limit",
        "condition_excluded",
        "seniority_outside",
    }
)
# The body screen files the category a relevance review names by its fit gate, so a
# person's restore on either surface holds against the other.
GATE_REFUSAL_CATEGORIES = {
    "arrangement": "arrangement_refused",
    "office attendance": "arrangement_office_limit",
    "travel": "travel_over_limit",
    "credential": "condition_excluded",
    "residence requirement": "location_residence_required",
}

# An evaluation found no requirements in the body whose digest the dismissal
# stores; only a different retained body supersedes it.
NO_REQUIREMENTS_CATEGORY = "no_requirements"

# Every scan, continuation, and re-screen hold-out ends its origin this way.
STRUCTURED_HOLDOUT_SUFFIX = ".structured_holdout"

RESCREEN_RESTORE_ORIGIN = "discovery.rescreen_queue.restore"


@dataclass(frozen=True)
class StructuredHoldout:
    """The events a structured hold-out appended, and whether the row now stands held out."""

    events: list[PendingDispositionResponse]
    # True as well when an earlier program dismissal already holds the row out.
    held_out: bool


class DiscoveryDispositionService:
    """Resolve variants, enforce rules, and append dispositions atomically."""

    def __init__(self, database: Database) -> None:
        """Bind disposition decisions to one durable database."""
        self.database = database

    def preview(self, workspace_id: str, request: DispositionSelection) -> DispositionPreview:
        """Resolve exact primary and variant targets without changing state."""
        with self.database.session() as session:
            rows, primary_ids = self._targets(session, workspace_id, request)
            rows, primary_ids, skipped = self._unmarked(
                session, workspace_id, request, rows, primary_ids
            )
            rows, withdrawn = self._restorable(request, rows)
            self._validate_duplicate_reference(session, workspace_id, request, rows)
            parameter_set = self._parameter_set(session, workspace_id)
            screening = self._screenings(session, workspace_id, rows, request)
            latest = self._latest(session, workspace_id, [row.id for row in rows])
            digest = self._digest(request, primary_ids, rows, parameter_set, screening, latest)
            return DispositionPreview(
                snapshot_sha256=digest,
                search_parameter_set_id=parameter_set.id if parameter_set else None,
                skipped_marked_ids=skipped,
                skipped_withdrawn_ids=withdrawn,
                targets=[
                    DispositionTargetResponse(
                        id=row.id,
                        primary_id=row.regional_variant_of or row.id,
                        kind="regional_variant" if row.regional_variant_of else "primary",
                        employer=row.employer,
                        title=row.title,
                        location=row.location,
                        discovery_url=row.discovery_url,
                        canonical_url=row.canonical_url,
                        withdrawn_at=row.withdrawn_at,
                    )
                    for row in rows
                ],
                action=request.action,
                category=request.category,
                reason=request.reason,
            )

    def apply(
        self,
        workspace_id: str,
        request: DispositionApply,
        *,
        actor: str,
        origin: str,
        correlation_id: str | None,
    ) -> list[PendingDispositionResponse]:
        """Append a confirmed disposition while its preview remains current."""
        selection = DispositionSelection.model_validate(
            request.root.model_dump(exclude={"snapshot_sha256"})
        )
        # The markers and dispositions read here decide the append, so no writer may
        # commit between them.
        with self.database.exclusive_session() as session:
            rows, primary_ids = self._targets(session, workspace_id, selection)
            rows, primary_ids, _skipped = self._unmarked(
                session, workspace_id, selection, rows, primary_ids
            )
            rows, _withdrawn = self._restorable(selection, rows)
            parameter_set = self._parameter_set(session, workspace_id)
            screening = self._screenings(session, workspace_id, rows, selection, enforce_rule=False)
            latest = self._latest(session, workspace_id, [row.id for row in rows])
            actual = self._digest(selection, primary_ids, rows, parameter_set, screening, latest)
            if actual != request.snapshot_sha256:
                raise ConflictError(
                    "That disposition preview is stale. Preview the selection again."
                )
            self._validate_rule(selection, rows, screening)
            self._validate_duplicate_reference(session, workspace_id, selection, rows)
            return self._append_events(
                session,
                workspace_id,
                selection,
                rows,
                parameter_set,
                screening,
                latest,
                actual,
                actor=actor,
                origin=origin,
                correlation_id=correlation_id,
                restore_provenance=(
                    None
                    if selection.action != "restore"
                    else "audit"
                    if self._is_automatic_origin(origin)
                    else "person"
                ),
            )

    def dismiss_refusal_in_session(
        self,
        session: Any,
        workspace_id: str,
        pending_id: str,
        *,
        category: str,
        reason: str,
        search_parameter_set_id: str | None,
        actor: str,
        origin: str,
        correlation_id: str | None,
        screening_override: dict[str, Any] | None = None,
    ) -> list[PendingDispositionResponse]:
        """Record an evaluation refusal using the reversible disposition path."""
        selection = DispositionSelection.model_validate(
            {
                "action": "dismiss",
                "selection_mode": "rule",
                "category": category,
                "reason": reason,
                "pending_discovery_ids": [pending_id],
            }
        )
        rows, primary_ids = self._targets(session, workspace_id, selection, expand=False)
        latest = self._latest(session, workspace_id, [row.id for row in rows])
        if self._person_restore_stands(session, latest.get(pending_id), category):
            return []
        # Only a person's confirmed dismissal reaches a favorite or kept family, or a
        # posting the person added. An empty body still dismisses an added posting,
        # since that dismissal is what keeps it from being paid for again.
        if marked_ids(session, workspace_id, [pending_id]) or (
            category != NO_REQUIREMENTS_CATEGORY
            and any(row.id == pending_id and staged_by_person(row.reason) for row in rows)
        ):
            return []
        parameter_set = (
            session.get(JobSearchParameterSet, search_parameter_set_id)
            if search_parameter_set_id
            else self._parameter_set(session, workspace_id)
        )
        if parameter_set is not None and parameter_set.workspace_id != workspace_id:
            raise NotFoundError("The refusal search settings do not belong to this workspace.")
        parameters = (
            JobSearchParameters.model_validate(parameter_set.document)
            if parameter_set is not None
            else parameters_in(session, workspace_id)
        )
        screening = self._screenings(
            session,
            workspace_id,
            rows,
            selection,
            enforce_rule=False,
            parameters=parameters,
        )
        if screening_override is not None:
            screening[pending_id] = screening_override
        digest = self._digest(selection, primary_ids, rows, parameter_set, screening, latest)
        return self._append_events(
            session,
            workspace_id,
            selection,
            rows,
            parameter_set,
            screening,
            latest,
            digest,
            actor=actor,
            origin=origin,
            correlation_id=correlation_id,
            restore_provenance=None,
        )

    def person_restored_in_session(
        self, session: Any, workspace_id: str, pending_id: str, category: str
    ) -> bool:
        """Return whether a person's restore of this category still stands."""
        latest = self._latest(session, workspace_id, [pending_id])
        return self._person_restore_stands(session, latest.get(pending_id), category)

    def dismiss_structured_holdout_in_session(
        self,
        session: Any,
        workspace_id: str,
        pending_id: str,
        *,
        category: str,
        reason: str,
        actor: str,
        origin: str,
        correlation_id: str | None,
        screening_override: dict[str, Any],
    ) -> StructuredHoldout:
        """Dismiss a known row unless a dismissal stands or a person last restored it."""
        latest = self._latest(session, workspace_id, [pending_id]).get(pending_id)
        if latest is not None and latest.action == "dismiss":
            return StructuredHoldout([], held_out=self._is_automatic_origin(latest.origin))
        if latest is not None and latest.restore_provenance == "person":
            return StructuredHoldout([], held_out=False)
        events = self.dismiss_refusal_in_session(
            session,
            workspace_id,
            pending_id,
            category=category,
            reason=reason,
            search_parameter_set_id=None,
            actor=actor,
            origin=origin,
            correlation_id=correlation_id,
            screening_override=screening_override,
        )
        return StructuredHoldout(events, held_out=bool(events))

    def person_dismissed_ids_in_session(self, session: Any, workspace_id: str) -> frozenset[str]:
        """Return discoveries protected by the person's latest dismissal."""
        events = session.scalars(latest_disposition_statement(workspace_id))
        return frozenset(
            event.pending_discovery_id
            for event in events
            if event.action == "dismiss" and not self._is_automatic_origin(event.origin)
        )

    def person_dismissal_in_session(
        self, session: Any, workspace_id: str, pending_id: str
    ) -> PendingDiscoveryDispositionEvent | None:
        """Return the latest event when it is a person's dismissal, not an automatic one."""
        latest = self._latest(session, workspace_id, [pending_id]).get(pending_id)
        if latest is None or latest.action != "dismiss" or self._is_automatic_origin(latest.origin):
            return None
        return latest

    def structured_holdout_in_session(
        self, session: Any, workspace_id: str, pending_id: str
    ) -> PendingDiscoveryDispositionEvent | None:
        """Return the latest event when a structured hold-out is what dismisses this discovery."""
        latest = self._latest(session, workspace_id, [pending_id]).get(pending_id)
        if (
            latest is None
            or latest.action != "dismiss"
            or not latest.origin.endswith(STRUCTURED_HOLDOUT_SUFFIX)
        ):
            return None
        return latest

    def rescreen_restorable_in_session(
        self, session: Any, workspace_id: str, pending_id: str
    ) -> PendingDiscoveryDispositionEvent | None:
        """Return the latest event when it is a rule dismissal a re-screen can judge again.

        Only hold-outs, body screens, and the free fit gates qualify; a person's
        dismissal, an empty body, and a closed posting never do.
        """
        latest = self._latest(session, workspace_id, [pending_id]).get(pending_id)
        if latest is None or latest.action != "dismiss":
            return None
        origin, category = latest.origin, latest.category
        if (
            origin.endswith(STRUCTURED_HOLDOUT_SUFFIX)
            or (origin.endswith(".body_screen") and category in BODY_REFUSAL_CATEGORIES)
            or (origin.endswith(".refusal") and category in GATE_REFUSAL_CATEGORIES)
        ):
            return latest
        return None

    def restore_rescreened_in_session(
        self,
        session: Any,
        workspace_id: str,
        pending_id: str,
        *,
        category: str,
        actor: str,
        correlation_id: str | None,
    ) -> list[PendingDispositionResponse]:
        """Return a rule dismissal the current search rules no longer make to the queue."""
        return self._restore_automatic(
            session,
            workspace_id,
            pending_id,
            self._latest(session, workspace_id, [pending_id]),
            category=category,
            reason="Re-screened against the current search rules, which no longer refuse it.",
            actor=actor,
            origin=RESCREEN_RESTORE_ORIGIN,
            correlation_id=correlation_id,
        )

    @staticmethod
    def _is_automatic_origin(origin: str) -> bool:
        """Return whether an origin records a programmatic dismissal."""
        return origin.endswith((".refusal", ".body_screen", STRUCTURED_HOLDOUT_SUFFIX))

    def restore_body_refusal_in_session(
        self,
        session: Any,
        workspace_id: str,
        pending_id: str,
        *,
        cleared: frozenset[str],
        actor: str,
        origin: str,
        correlation_id: str | None,
        content_sha256: str | None = None,
        new_words: bool = True,
    ) -> list[PendingDispositionResponse]:
        """Restore the current automatic dismissal when a body unlike the judged one cleared it.

        A dismissal that recorded no judged body falls back to ``new_words`` from the last read.
        """
        if not origin.endswith(".body_screen"):
            raise ValidationError("An automatic body restore must carry a body-screen origin.")
        latest = self._latest(session, workspace_id, [pending_id])
        previous = latest.get(pending_id)
        if (
            previous is None
            or previous.action != "dismiss"
            or not previous.origin.startswith("source.")
            or not previous.origin.endswith(".body_screen")
            or previous.category not in BODY_REFUSAL_CATEGORIES & cleared
        ):
            return []
        judged = self._judged_body(previous, BODY_REFUSAL_CATEGORIES)
        if judged is None and not new_words:
            return []
        if judged is not None and judged == content_sha256:
            return []
        return self._restore_automatic(
            session,
            workspace_id,
            pending_id,
            latest,
            category=previous.category,
            reason=(
                "A newer verified publisher body no longer states the conflict that caused "
                "the automatic dismissal."
            ),
            actor=actor,
            origin=origin,
            correlation_id=correlation_id,
        )

    def dismiss_no_requirements_in_session(
        self,
        session: Any,
        workspace_id: str,
        pending_id: str,
        *,
        content_sha256: str,
        actor: str,
        origin: str,
        correlation_id: str | None,
    ) -> list[PendingDispositionResponse]:
        """Dismiss a row whose evaluated body yielded no requirements, unless already dismissed."""
        latest = self._latest(session, workspace_id, [pending_id]).get(pending_id)
        if latest is not None and latest.action == "dismiss":
            return []
        return self.dismiss_refusal_in_session(
            session,
            workspace_id,
            pending_id,
            category=NO_REQUIREMENTS_CATEGORY,
            reason=(
                "An evaluation found no requirements in this posting's body. It returns "
                "to Pending when a different body is retained, and stays restorable."
            ),
            search_parameter_set_id=None,
            actor=actor,
            origin=origin,
            correlation_id=correlation_id,
            screening_override={
                "standing": ScreeningStanding.REFUSE.value,
                "findings": [],
                "content_sha256": content_sha256,
            },
        )

    def no_requirements_body_in_session(
        self, session: Any, workspace_id: str, pending_id: str
    ) -> str | None:
        """Return the body digest a standing no-requirements dismissal names, if one stands."""
        return self._judged_body(self._latest(session, workspace_id, [pending_id]).get(pending_id))

    def restore_no_requirements_in_session(
        self,
        session: Any,
        workspace_id: str,
        pending_id: str,
        *,
        content_sha256: str,
        actor: str,
        origin: str,
        correlation_id: str | None,
    ) -> list[PendingDispositionResponse]:
        """Restore a no-requirements dismissal once a different body is retained."""
        latest = self._latest(session, workspace_id, [pending_id])
        judged = self._judged_body(latest.get(pending_id))
        if judged is None or judged == content_sha256:
            return []
        return self._restore_automatic(
            session,
            workspace_id,
            pending_id,
            latest,
            category=NO_REQUIREMENTS_CATEGORY,
            reason="A different posting body was retained after the one with no requirements.",
            actor=actor,
            origin=origin,
            correlation_id=correlation_id,
        )

    @staticmethod
    def _judged_body(
        event: PendingDiscoveryDispositionEvent | None,
        categories: frozenset[str] = frozenset({NO_REQUIREMENTS_CATEGORY}),
    ) -> str | None:
        if event is None or event.action != "dismiss" or event.category not in categories:
            return None
        return cast(str | None, event.screening.get("content_sha256"))

    def _restore_automatic(
        self,
        session: Any,
        workspace_id: str,
        pending_id: str,
        latest: dict[str, PendingDiscoveryDispositionEvent],
        *,
        category: str,
        reason: str,
        actor: str,
        origin: str,
        correlation_id: str | None,
    ) -> list[PendingDispositionResponse]:
        selection = DispositionSelection.model_validate(
            {
                "action": "restore",
                "selection_mode": "explicit",
                "category": category,
                "reason": reason,
                "pending_discovery_ids": [pending_id],
            }
        )
        rows, primary_ids = self._targets(session, workspace_id, selection, expand=False)
        # A withdrawn row stays dismissed until a read relists it.
        if any(row.withdrawn_at is not None for row in rows):
            return []
        parameter_set = self._parameter_set(session, workspace_id)
        screening = self._screenings(
            session,
            workspace_id,
            rows,
            selection,
            enforce_rule=False,
        )
        digest = self._digest(selection, primary_ids, rows, parameter_set, screening, latest)
        return self._append_events(
            session,
            workspace_id,
            selection,
            rows,
            parameter_set,
            screening,
            latest,
            digest,
            actor=actor,
            origin=origin,
            correlation_id=correlation_id,
            restore_provenance="audit",
        )

    def _append_events(
        self,
        session: Any,
        workspace_id: str,
        selection: DispositionSelection,
        rows: list[PendingDiscovery],
        parameter_set: JobSearchParameterSet | None,
        screening: dict[str, Any],
        latest: dict[str, PendingDiscoveryDispositionEvent],
        snapshot_sha256: str,
        *,
        actor: str,
        origin: str,
        correlation_id: str | None,
        restore_provenance: Literal["person", "audit"] | None,
    ) -> list[PendingDispositionResponse]:
        events: list[PendingDiscoveryDispositionEvent] = []
        for row in rows:
            previous = latest.get(row.id)
            if selection.action == "restore" and (previous is None or previous.action != "dismiss"):
                raise ConflictError(f"Pending discovery {row.id!r} is not currently dismissed.")
            event = PendingDiscoveryDispositionEvent(
                id=new_id(),
                workspace_id=workspace_id,
                pending_discovery_id=row.id,
                action=selection.action,
                restore_provenance=restore_provenance,
                category=selection.category,
                reason=selection.reason,
                duplicate_of_job_id=selection.duplicate_of_job_id,
                duplicate_of_pending_id=selection.duplicate_of_pending_id,
                selection_mode=selection.selection_mode,
                snapshot_sha256=snapshot_sha256,
                search_parameter_set_id=parameter_set.id if parameter_set else None,
                screening=screening[row.id],
                supersedes_event_id=previous.id if previous else None,
                created_by=actor,
                origin=origin,
                correlation_id=correlation_id,
            )
            session.add(event)
            events.append(event)
        if selection.action == "dismiss":
            original = self._duplicate_head(session, workspace_id, selection)
            if original is not None:
                for row in rows:
                    if selection.dismiss_marked:
                        move_pending_markers(row, original)
                    else:
                        transfer_pending_closing_date(row, original)
            if selection.dismiss_marked:
                clear_pending_markers(rows)
        # The audit timestamp is assigned on insert, and the response carries it.
        session.flush()
        if selection.action == "restore":
            self._release_from_withdrawn_heads(
                session,
                workspace_id,
                rows,
                actor=actor,
                origin=origin,
                correlation_id=correlation_id,
            )
        return [self._response(event) for event in events]

    @staticmethod
    def _release_from_withdrawn_heads(
        session: Any,
        workspace_id: str,
        rows: list[PendingDiscovery],
        *,
        actor: str,
        origin: str,
        correlation_id: str | None,
    ) -> None:
        """Release restored copies whose head was withdrawn, since that head stays dismissed."""
        heads = {row.regional_variant_of for row in rows} - {None}
        gone = {
            head.id
            for head in (session.get(PendingDiscovery, head_id) for head_id in heads)
            if head is not None and head.withdrawn_at is not None
        }
        release_families(
            session,
            workspace_id,
            [row for row in rows if row.regional_variant_of in gone],
            reason="its head was withdrawn",
            created_at=utc_now(),
            actor=actor,
            origin=origin,
            correlation_id=correlation_id,
        )

    def history(self, workspace_id: str, pending_id: str) -> list[PendingDispositionResponse]:
        """Return the immutable disposition chain for one discovery."""
        with self.database.session() as session:
            rows = session.scalars(
                select(PendingDiscoveryDispositionEvent)
                .where(
                    PendingDiscoveryDispositionEvent.workspace_id == workspace_id,
                    PendingDiscoveryDispositionEvent.pending_discovery_id == pending_id,
                )
                .order_by(
                    PendingDiscoveryDispositionEvent.created_at, PendingDiscoveryDispositionEvent.id
                )
            ).all()
            return [self._response(row) for row in rows]

    def dismissed(self, workspace_id: str) -> list[DismissedDiscoveryResponse]:
        """Enumerate current dismissals of live postings so the user can select rows to restore."""
        with self.database.session() as session:
            rows = session.scalars(latest_disposition_statement(workspace_id)).all()
            dismissed = [row for row in rows if row.action == "dismiss"]
            discoveries = {
                row.id: row
                for row in session.scalars(
                    select(PendingDiscovery).where(
                        PendingDiscovery.workspace_id == workspace_id,
                        PendingDiscovery.id.in_([row.pending_discovery_id for row in dismissed]),
                        PendingDiscovery.withdrawn_at.is_(None),
                    )
                )
            }
            return [
                DismissedDiscoveryResponse(
                    **self._response(row).model_dump(),
                    employer=discoveries[row.pending_discovery_id].employer,
                    title=discoveries[row.pending_discovery_id].title,
                    location=discoveries[row.pending_discovery_id].location,
                    discovery_url=discoveries[row.pending_discovery_id].discovery_url,
                    canonical_url=discoveries[row.pending_discovery_id].canonical_url,
                )
                for row in dismissed
                if row.pending_discovery_id in discoveries
            ]

    def duplicate_candidates(self, workspace_id: str) -> list[PendingDuplicateCandidateResponse]:
        """List active and currently dismissed roots available as duplicate targets."""
        with self.database.session() as session:
            latest = session.scalars(latest_disposition_statement(workspace_id)).all()
            dismissed = {row.pending_discovery_id for row in latest if row.action == "dismiss"}
            rows = session.scalars(
                select(PendingDiscovery)
                .where(*root_discovery_filters(workspace_id))
                .order_by(PendingDiscovery.employer, PendingDiscovery.title, PendingDiscovery.id)
            ).all()
            return [
                PendingDuplicateCandidateResponse(
                    id=row.id,
                    employer=row.employer,
                    title=row.title,
                    location=row.location,
                    discovery_url=row.discovery_url,
                    dismissed=row.id in dismissed,
                )
                for row in rows
                if row.withdrawn_at is None or row.id in dismissed
            ]

    @staticmethod
    def _parameter_set(session: Any, workspace_id: str) -> JobSearchParameterSet | None:
        return cast(
            JobSearchParameterSet | None,
            session.scalars(
                select(JobSearchParameterSet)
                .where(JobSearchParameterSet.workspace_id == workspace_id)
                .order_by(JobSearchParameterSet.version.desc())
                .limit(1)
            ).first(),
        )

    def _targets(
        self, session: Any, workspace_id: str, request: DispositionSelection, *, expand: bool = True
    ) -> tuple[list[PendingDiscovery], list[str]]:
        selected = session.scalars(
            select(PendingDiscovery).where(
                PendingDiscovery.workspace_id == workspace_id,
                PendingDiscovery.id.in_(request.pending_discovery_ids),
                PendingDiscovery.archived_at.is_(None),
            )
        ).all()
        if len(selected) != len(set(request.pending_discovery_ids)):
            raise NotFoundError("One or more pending discoveries do not exist in this workspace.")
        primary_ids = sorted({row.regional_variant_of or row.id for row in selected})
        # Reaching a whole variant family is what the human preview names before
        # anything changes. An automatic refusal judged one posting, so it may
        # only dispose of that one; a sibling market gets its own gate.
        if not expand:
            return list(selected), primary_ids
        rows = session.scalars(
            select(PendingDiscovery)
            .where(
                PendingDiscovery.workspace_id == workspace_id,
                (PendingDiscovery.id.in_(primary_ids))
                | (PendingDiscovery.regional_variant_of.in_(primary_ids)),
                PendingDiscovery.archived_at.is_(None),
            )
            .order_by(PendingDiscovery.id)
        ).all()
        if request.action == "restore":
            # A restore reverses dismissals, so a family member with none of its own stays out.
            latest = self._latest(session, workspace_id, [row.id for row in rows])
            chosen = set(request.pending_discovery_ids)
            rows = [
                row
                for row in rows
                if row.id in chosen or (row.id in latest and latest[row.id].action == "dismiss")
            ]
        return rows, primary_ids

    @staticmethod
    def _unmarked(
        session: Any,
        workspace_id: str,
        request: DispositionSelection,
        rows: list[PendingDiscovery],
        primary_ids: list[str],
    ) -> tuple[list[PendingDiscovery], list[str], list[str]]:
        """Drop favorite and kept families from an unconfirmed dismissal, naming their heads."""
        if request.action != "dismiss" or request.dismiss_marked:
            return rows, primary_ids, []
        held = marked_ids(session, workspace_id, primary_ids)
        if not held:
            return rows, primary_ids, []
        remaining = [row for row in rows if (row.regional_variant_of or row.id) not in held]
        if not remaining:
            raise ConflictError(
                "Every selected discovery is a favorite or kept. Dismiss each one from its "
                "own row to confirm it."
            )
        unheld = [pending_id for pending_id in primary_ids if pending_id not in held]
        return remaining, unheld, sorted(held)

    @staticmethod
    def _restorable(
        request: DispositionSelection, rows: list[PendingDiscovery]
    ) -> tuple[list[PendingDiscovery], list[str]]:
        """Drop withdrawn rows from a restore, naming them, since their postings are gone."""
        if request.action != "restore":
            return rows, []
        withdrawn = sorted(row.id for row in rows if row.withdrawn_at is not None)
        if not withdrawn:
            return rows, []
        remaining = [row for row in rows if row.withdrawn_at is None]
        if not remaining:
            raise ConflictError(
                "Every selected discovery was withdrawn by its source, so none can be restored."
            )
        return remaining, withdrawn

    def _screenings(
        self,
        session: Any,
        workspace_id: str,
        rows: list[PendingDiscovery],
        request: DispositionSelection,
        *,
        enforce_rule: bool = True,
        parameters: JobSearchParameters | None = None,
    ) -> dict[str, Any]:
        parameters = parameters or parameters_in(session, workspace_id)
        searched = list(parameters.seniority.stated_titles)
        source_employers = source_employer_names(session, (row.source_id for row in rows))
        result = {}
        for row in rows:
            screened = screen_discovery(
                row.employer,
                row.title,
                row.location,
                parameters,
                searched,
                StagedListingFacts.from_json(row.staged_facts),
                source_employer=source_employers.get(row.source_id or "", ""),
            )
            result[row.id] = {
                "standing": screened.standing,
                "findings": [
                    {
                        "dimension": item.dimension,
                        "standing": item.standing,
                        "code": item.code,
                        "subject": item.subject,
                    }
                    for item in screened.findings
                ],
            }
        if enforce_rule:
            self._validate_rule(request, rows, result)
        return result

    @staticmethod
    def _validate_rule(
        request: DispositionSelection,
        rows: list[PendingDiscovery],
        result: dict[str, Any],
    ) -> None:
        if request.selection_mode == "rule":
            if request.action != "dismiss":
                raise ValidationError("Rule selection is available only for dismissal.")
            for row in rows:
                findings = cast(list[dict[str, Any]], result[row.id]["findings"])
                if not any(
                    item["standing"] == ScreeningStanding.REFUSE
                    and item["code"] == request.category
                    for item in findings
                ):
                    raise ValidationError(
                        "Bulk rule dismissal may include only deterministic refusals "
                        "in its selected category."
                    )

    def _duplicate_head(
        self, session: Any, workspace_id: str, request: DispositionSelection
    ) -> PendingDiscovery | None:
        """Return the live, undismissed family head of the pending row this one duplicates."""
        if request.duplicate_of_pending_id is None:
            return None
        original = session.get(PendingDiscovery, request.duplicate_of_pending_id)
        if original is None or original.workspace_id != workspace_id:
            return None
        head = session.get(PendingDiscovery, original.regional_variant_of or original.id)
        if head is None or head.archived_at is not None:
            return None
        latest = self._latest(session, workspace_id, [head.id]).get(head.id)
        return None if latest is not None and latest.action == "dismiss" else head

    @staticmethod
    def _validate_duplicate_reference(
        session: Any,
        workspace_id: str,
        request: DispositionSelection,
        rows: list[PendingDiscovery],
    ) -> None:
        """Keep duplicate references in-workspace and outside the dismissed family."""
        if request.duplicate_of_job_id is not None:
            matching = session.scalar(
                select(
                    exists().where(
                        JobPosting.id == request.duplicate_of_job_id,
                        JobPosting.workspace_id == workspace_id,
                    )
                )
            )
            if not matching:
                raise ValidationError(
                    "The duplicate posting reference is not a tracked job in this workspace."
                )
        if request.duplicate_of_pending_id is None:
            return
        if request.duplicate_of_pending_id in {row.id for row in rows}:
            raise ValidationError("A pending discovery cannot duplicate itself or its variant.")
        matching_pending = session.scalar(
            select(
                exists().where(
                    PendingDiscovery.id == request.duplicate_of_pending_id,
                    PendingDiscovery.workspace_id == workspace_id,
                )
            )
        )
        if not matching_pending:
            raise ValidationError(
                "The duplicate pending reference is not a discovery in this workspace."
            )

    @staticmethod
    def _digest(
        request: DispositionSelection,
        primary_ids: list[str],
        rows: list[PendingDiscovery],
        parameter_set: JobSearchParameterSet | None,
        screening: dict[str, Any],
        latest: dict[str, PendingDiscoveryDispositionEvent],
    ) -> str:
        payload = {
            "request": request.model_dump(mode="json"),
            "primary_ids": primary_ids,
            "rows": [
                {
                    "id": row.id,
                    "fingerprint": row.fingerprint,
                    "regional_variant_of": row.regional_variant_of,
                }
                for row in rows
            ],
            "parameter_set": [parameter_set.id, parameter_set.content_sha256]
            if parameter_set
            else None,
            "screening": screening,
            "latest": sorted(
                (pending_id, event.id, event.action) for pending_id, event in latest.items()
            ),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()

    @staticmethod
    def _latest(
        session: Any, workspace_id: str, ids: list[str]
    ) -> dict[str, PendingDiscoveryDispositionEvent]:
        rows = session.scalars(latest_disposition_statement(workspace_id, ids)).all()
        return {row.pending_discovery_id: row for row in rows}

    @staticmethod
    def _person_restore_stands(
        session: Any, latest: PendingDiscoveryDispositionEvent | None, category: str
    ) -> bool:
        category = GATE_REFUSAL_CATEGORIES.get(category, category)
        event = latest
        while event is not None:
            if (
                event.action == "dismiss"
                and GATE_REFUSAL_CATEGORIES.get(event.category, event.category) == category
            ):
                return False
            superseded = (
                session.get(PendingDiscoveryDispositionEvent, event.supersedes_event_id)
                if event.supersedes_event_id is not None
                else None
            )
            if (
                event.action == "restore"
                and event.restore_provenance == "person"
                and superseded is not None
                and GATE_REFUSAL_CATEGORIES.get(superseded.category, superseded.category)
                == category
            ):
                return True
            event = superseded
        return False

    @staticmethod
    def _response(row: PendingDiscoveryDispositionEvent) -> PendingDispositionResponse:
        return PendingDispositionResponse(
            id=row.id,
            pending_discovery_id=row.pending_discovery_id,
            action=cast(Literal["dismiss", "restore"], row.action),
            selection_mode=cast(Literal["rule", "explicit", "automatic"], row.selection_mode),
            category=row.category,
            reason=row.reason,
            origin=row.origin,
            created_at=row.created_at,
            duplicate_of_job_id=row.duplicate_of_job_id,
            duplicate_of_pending_id=row.duplicate_of_pending_id,
            supersedes_event_id=row.supersedes_event_id,
        )


def dismissed_pending_ids(session: Any, workspace_id: str) -> set[str]:
    """Return discoveries whose unsuperseded disposition is a dismissal."""
    rows = session.scalars(latest_disposition_statement(workspace_id)).all()
    return {row.pending_discovery_id for row in rows if row.action == "dismiss"}
