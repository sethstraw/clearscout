"""The inbox where a model's suggestions for the search wait for a ruling.

A search may propose the names this work goes by, the kinds of organization that
buy it, and the capabilities and domains it covers. Every one of them cites the
master CV version it was read from, is accepted or rejected rather than edited,
and is acted on only once it has been. Each inbox service supplies what it
stores and how it reads back; the queue itself is shared.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, TypedDict, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from clearscout.application.discovery.derived_vocabulary_rules import same_employer_archetype
from clearscout.application.discovery.refusal_patterns import standing_refusals
from clearscout.application.discovery.search_proposal_inbox import (
    active_proposal_count,
    effective_proposal_rows,
)
from clearscout.application.failures import ConflictError, NotFoundError, ValidationError
from clearscout.application.job_search.models import (
    RefusalPatternProposalResponse,
    RefusedPostingResponse,
    SearchArchetypeProposalInboxResponse,
    SearchArchetypeProposalResponse,
    SearchClusterProposalInboxResponse,
    SearchClusterProposalResponse,
    SearchProposalCapacityKind,
    SearchProposalCapacityResponse,
    SearchTitleProposalInboxResponse,
    SearchTitleProposalResponse,
)
from clearscout.application.master_cv.proposal_models import (
    MasterCvProposalKind,
    MasterCvProposalResponse,
)
from clearscout.application.master_cv.versioning import current_version_id
from clearscout.application.workspaces.parameters import SearchParametersService, parameters_in
from clearscout.domain import ProposalRejectionReason, SearchProposalState, utc_now
from clearscout.domain.identity import normalize_text
from clearscout.domain.search_parameters import (
    MAX_ARCHETYPES,
    MAX_CLUSTER_TERMS,
    MAX_CLUSTERS,
    MAX_TITLES,
    Cluster,
    ClusterKind,
    EmployerArchetype,
    Focus,
    Lane,
    TargetTitle,
)
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    DerivedCluster,
    DerivedEmployerArchetype,
    DerivedSearchTitle,
    MasterCvProposal,
    RefusalPatternProposal,
    Workspace,
)


class _Proposal(Protocol):
    """The columns every proposal row carries, whatever it proposes."""

    id: Any
    workspace_id: Any
    master_cv_version_id: Any
    created_at: Any
    origin: Any
    correlation_id: Any
    position: Any
    state: Any
    decided_at: Any
    rejection_reason: Any


class _Inbox[ResponseT](Protocol):
    """The item collection exposed by every typed proposal envelope."""

    items: list[ResponseT]


class ProposalInbox[RowT: _Proposal, ResponseT, InboxT]:
    """One workspace-effective queue of model suggestions and durable rulings."""

    def __init__(
        self,
        database: Database,
        row_type: type[RowT],
        reported: Callable[[RowT, str], ResponseT],
        envelope: Callable[[list[ResponseT], list[SearchProposalCapacityResponse]], InboxT],
        capacities: tuple[tuple[SearchProposalCapacityKind, int, dict[str, Any] | None], ...],
        label: str,
    ) -> None:
        """Bind the inbox to its durable store and the row it rules on."""
        self.database = database
        self.row_type = row_type
        self.reported = reported
        self.envelope = envelope
        self.capacities = capacities
        self.label = label

    def list(self, workspace_id: str) -> list[ResponseT]:
        """Return the workspace-effective proposal items without API metadata."""
        return cast(_Inbox[ResponseT], self.inbox(workspace_id)).items

    def inbox(self, workspace_id: str) -> InboxT:
        """Return every effective ruling and current-version pending suggestion.

        Decisions remain effective after the CV changes. Undecided suggestions
        do not, because their evidence belongs to the version that produced them.
        """
        with self.database.session() as session:
            if session.get(Workspace, workspace_id) is None:
                raise NotFoundError(f"Unknown workspace {workspace_id!r}.")
            version_id = current_version_id(session, workspace_id)
            if version_id is None:
                capacities = [
                    SearchProposalCapacityResponse(
                        kind=kind,
                        active_count=0,
                        limit=limit,
                        remaining=limit,
                    )
                    for kind, limit, _ in self.capacities
                ]
                return self.envelope([], capacities)
            rows = effective_proposal_rows(session, self.row_type, workspace_id, version_id)
            capacities = []
            for kind, limit, extra_filter in self.capacities:
                active_count = active_proposal_count(
                    session,
                    self.row_type,
                    workspace_id,
                    version_id,
                    extra_filter,
                )
                remaining = max(limit - active_count, 0)
                capacities.append(
                    SearchProposalCapacityResponse(
                        kind=kind,
                        active_count=active_count,
                        limit=limit,
                        remaining=remaining,
                    )
                )
            return self.envelope(
                [self.reported(row, version_id) for row in rows],
                capacities,
            )

    def accept(self, workspace_id: str, proposal_id: str) -> ResponseT:
        """Let every later search act on this suggestion."""
        return self._decide(workspace_id, proposal_id, SearchProposalState.ACCEPTED, None)

    def reject(
        self,
        workspace_id: str,
        proposal_id: str,
        reason: ProposalRejectionReason | None = None,
    ) -> ResponseT:
        """Refuse it, optionally naming why so a later search treats the ban narrowly."""
        return self._decide(workspace_id, proposal_id, SearchProposalState.REJECTED, reason)

    def _decide(
        self,
        workspace_id: str,
        proposal_id: str,
        state: SearchProposalState,
        reason: ProposalRejectionReason | None,
    ) -> ResponseT:
        """Record one ruling, which may reverse an earlier one."""
        with self.database.session() as session:
            row = session.get(self.row_type, proposal_id)
            if row is None or row.workspace_id != workspace_id:
                raise NotFoundError(
                    f"{self.label} {proposal_id!r} does not exist in this workspace."
                )
            row.state = state.value
            row.decided_at = utc_now()
            # Accepting clears any prior rejection reason so it does not shape future proposals.
            row.rejection_reason = reason.value if reason is not None else None
            version_id = current_version_id(session, workspace_id)
            if version_id is None:
                raise ConflictError("A search proposal cannot be ruled on without a master CV.")
            self._after_ruling(session, workspace_id, row, state)
            return self.reported(row, version_id)

    def _after_ruling(
        self, session: Session, workspace_id: str, row: RowT, state: SearchProposalState
    ) -> None:
        """Apply a ruling's effect on the stated parameters. The base queue has none."""
        del session, workspace_id, row, state


class SearchTitleProposalService(
    ProposalInbox[DerivedSearchTitle, SearchTitleProposalResponse, SearchTitleProposalInboxResponse]
):
    """List the titles a search proposed and record what the user decided."""

    def __init__(self, database: Database) -> None:
        """Bind the title inbox to the durable store."""
        super().__init__(
            database,
            DerivedSearchTitle,
            _reported,
            lambda items, capacities: SearchTitleProposalInboxResponse(
                items=items, capacities=capacities
            ),
            (("title", MAX_TITLES, None),),
            "Search title proposal",
        )


class SearchArchetypeProposalService(
    ProposalInbox[
        DerivedEmployerArchetype,
        SearchArchetypeProposalResponse,
        SearchArchetypeProposalInboxResponse,
    ]
):
    """List the kinds of employer a search proposed and record what the user decided."""

    def __init__(self, database: Database) -> None:
        """Bind the archetype inbox to the durable store."""
        super().__init__(
            database,
            DerivedEmployerArchetype,
            _reported_archetype,
            lambda items, capacities: SearchArchetypeProposalInboxResponse(
                items=items, capacities=capacities
            ),
            (("employer_archetype", MAX_ARCHETYPES, None),),
            "Employer archetype proposal",
        )


class SearchClusterProposalService(
    ProposalInbox[DerivedCluster, SearchClusterProposalResponse, SearchClusterProposalInboxResponse]
):
    """List the capabilities and domains a search proposed, and the rulings on them."""

    def __init__(self, database: Database) -> None:
        """Bind the cluster inbox to the durable store."""
        super().__init__(
            database,
            DerivedCluster,
            _reported_cluster,
            lambda items, capacities: SearchClusterProposalInboxResponse(
                items=items, capacities=capacities
            ),
            (
                ("capability", MAX_CLUSTERS, {"kind": ClusterKind.CAPABILITY.value}),
                ("domain", MAX_CLUSTERS, {"kind": ClusterKind.DOMAIN.value}),
            ),
            "Cluster proposal",
        )
        self._parameters = SearchParametersService(database)

    def _after_ruling(
        self, session: Session, workspace_id: str, row: DerivedCluster, state: SearchProposalState
    ) -> None:
        """Add an accepted cluster to the stated focus list, or drop a withdrawn one.

        A name already present on accept or already absent on reject writes no version.
        """
        if state not in (SearchProposalState.ACCEPTED, SearchProposalState.REJECTED):
            return
        kind = ClusterKind(row.kind)
        parameters = parameters_in(session, workspace_id)
        field = "capabilities" if kind is ClusterKind.CAPABILITY else "domains"
        listed = getattr(parameters.focus, field)
        present = next(
            (
                item
                for item in listed
                if item.stated and normalize_text(item.name) == normalize_text(row.name)
            ),
            None,
        )
        if state is SearchProposalState.ACCEPTED:
            if present is not None:
                return
            updated = [
                *listed,
                Cluster(
                    name=row.name, terms=list(row.terms)[:MAX_CLUSTER_TERMS], because=row.because
                ),
            ]
        else:
            if present is None:
                return
            updated = [item for item in listed if item is not present]
        focus = parameters.focus.model_copy(update={field: updated})
        self._parameters.save_in_session(
            session,
            workspace_id,
            parameters.model_copy(update={"focus": focus}),
            origin=f"proposal_{state.value}",
        )


def accepted_titles(session: Session, workspace_id: str) -> tuple[TargetTitle, ...]:
    """Return the titles allowed by each identity's latest workspace decision."""
    version_id = current_version_id(session, workspace_id)
    if version_id is None:
        return ()
    return tuple(
        TargetTitle(title=row.title, lane=None if row.lane is None else Lane(row.lane))
        for row in effective_proposal_rows(session, DerivedSearchTitle, workspace_id, version_id)
        if row.state == SearchProposalState.ACCEPTED.value
    )


def accepted_archetypes(database: Database, workspace_id: str) -> tuple[EmployerArchetype, ...]:
    """Return the distinct employer kinds this workspace has accepted."""
    with database.session() as session:
        version_id = current_version_id(session, workspace_id)
        if version_id is None:
            return ()
        return accepted_archetypes_in(session, workspace_id, version_id)


def accepted_archetypes_in(
    session: Session, workspace_id: str, master_cv_version_id: str
) -> tuple[EmployerArchetype, ...]:
    """Return the distinct employer kinds this workspace has accepted, session-scoped."""
    rows = effective_proposal_rows(
        session, DerivedEmployerArchetype, workspace_id, master_cv_version_id
    )
    accepted: list[EmployerArchetype] = []
    for row in rows:
        if row.state != SearchProposalState.ACCEPTED.value:
            continue
        if any(same_employer_archetype(row.name, kept.name) for kept in accepted):
            continue
        accepted.append(EmployerArchetype(name=row.name, because=row.because))
    return tuple(accepted)


def widened_focus(database: Database, workspace_id: str, focus: Focus) -> Focus:
    """Return the focus a search states, widened by the clusters a person accepted."""
    with database.session() as session:
        return focus.plus(accepted_clusters(session, workspace_id))


def accepted_clusters(
    session: Session, workspace_id: str
) -> tuple[tuple[ClusterKind, Cluster], ...]:
    """Return the clusters the user allowed, each with the kind it belongs to.

    Widens search and review with accepted clusters the stated list may not
    carry. Takes ``widened_focus``'s open session.
    """
    version_id = current_version_id(session, workspace_id)
    if version_id is None:
        return ()
    rows = effective_proposal_rows(session, DerivedCluster, workspace_id, version_id)
    accepted: list[tuple[ClusterKind, Cluster]] = []
    seen: set[tuple[ClusterKind, str]] = set()
    for row in rows:
        if row.state != SearchProposalState.ACCEPTED.value:
            continue
        kind = ClusterKind(row.kind)
        identity = (kind, normalize_text(row.name))
        if identity in seen:
            continue
        seen.add(identity)
        # A proposal accepted under a wider term bound keeps loading; only its
        # first MAX_CLUSTER_TERMS terms are read, as with a fresh proposal.
        cluster = Cluster(
            name=row.name, terms=list(row.terms)[:MAX_CLUSTER_TERMS], because=row.because
        )
        accepted.append((kind, cluster))
    return tuple(accepted)


def _rejection_reason(row: Any) -> ProposalRejectionReason | None:
    """Return the stored rejection reason as an enum, or None if absent."""
    stored = getattr(row, "rejection_reason", None)
    return None if stored is None else ProposalRejectionReason(stored)


class _ProposalLifecycleResponse(TypedDict):
    """The response fields shared by every derived-proposal lifecycle."""

    origin: str
    correlation_id: str | None
    master_cv_version_id: str
    is_current_master_cv_version: bool
    state: SearchProposalState
    proposed_at: datetime
    decided_at: datetime | None
    rejection_reason: ProposalRejectionReason | None


def _reported_lifecycle(row: Any, current_master_cv_version_id: str) -> _ProposalLifecycleResponse:
    """Project the durable decision and provenance shared by every proposal kind."""
    return {
        "origin": row.origin,
        "correlation_id": row.correlation_id,
        "master_cv_version_id": row.master_cv_version_id,
        "is_current_master_cv_version": row.master_cv_version_id == current_master_cv_version_id,
        "state": SearchProposalState(row.state),
        "proposed_at": row.created_at,
        "decided_at": row.decided_at,
        "rejection_reason": _rejection_reason(row),
    }


def _reported_cluster(
    row: DerivedCluster, current_master_cv_version_id: str
) -> SearchClusterProposalResponse:
    """Return one stored cluster proposal as the inbox reports it."""
    return SearchClusterProposalResponse(
        id=row.id,
        kind=ClusterKind(row.kind),
        name=row.name,
        terms=list(row.terms),
        because=row.because,
        evidence_ids=list(row.evidence_ids),
        **_reported_lifecycle(row, current_master_cv_version_id),
    )


def _reported_archetype(
    row: DerivedEmployerArchetype, current_master_cv_version_id: str
) -> SearchArchetypeProposalResponse:
    """Return one stored archetype proposal as the inbox reports it."""
    return SearchArchetypeProposalResponse(
        id=row.id,
        name=row.name,
        because=row.because,
        evidence_ids=list(row.evidence_ids),
        **_reported_lifecycle(row, current_master_cv_version_id),
    )


def _reported(
    row: DerivedSearchTitle, current_master_cv_version_id: str
) -> SearchTitleProposalResponse:
    """Return one stored proposal as the inbox reports it."""
    return SearchTitleProposalResponse(
        id=row.id,
        title=row.title,
        reason=row.reason,
        evidence_ids=list(row.evidence_ids),
        lane=None if row.lane is None else Lane(row.lane),
        **_reported_lifecycle(row, current_master_cv_version_id),
    )


@dataclass(frozen=True)
class CarriedRejection:
    """One rejection carried across CV versions, name and optional reason."""

    name: str
    reason: ProposalRejectionReason | None = None


@dataclass(frozen=True)
class CarriedRejections:
    """Rejections the workspace made, sorted newest first, capped for the prompt."""

    titles: tuple[CarriedRejection, ...]
    archetypes: tuple[CarriedRejection, ...]
    clusters: tuple[CarriedRejection, ...]

    def has_any(self) -> bool:
        """Return whether any rejection carried across CV versions is present."""
        return bool(self.titles or self.archetypes or self.clusters)


def _carried_names(
    session: Session, workspace_id: str, row_type: Any, cap: int
) -> tuple[CarriedRejection, ...]:
    version_id = current_version_id(session, workspace_id)
    if version_id is None:
        return ()
    rows = sorted(
        (
            row
            for row in effective_proposal_rows(session, row_type, workspace_id, version_id)
            if row.state == SearchProposalState.REJECTED.value and row.carried_identity is not None
        ),
        key=lambda row: (row.decided_at or row.created_at, str(row.id)),
        reverse=True,
    )
    seen: set[str] = set()
    entries: list[CarriedRejection] = []
    for row in rows:
        identity = row.carried_identity
        if identity in seen:
            continue
        seen.add(identity)
        stored_reason = getattr(row, "rejection_reason", None)
        entries.append(
            CarriedRejection(
                name=row.name if hasattr(row, "name") else row.title,
                reason=None if stored_reason is None else ProposalRejectionReason(stored_reason),
            )
        )
        if len(entries) >= cap:
            break
    return tuple(entries)


def carried_rejections_in(session: Session, workspace_id: str, *, cap: int) -> CarriedRejections:
    """Return the newest rejection per carried identity inside the caller's transaction."""
    return CarriedRejections(
        titles=_carried_names(session, workspace_id, DerivedSearchTitle, cap),
        archetypes=_carried_names(session, workspace_id, DerivedEmployerArchetype, cap),
        clusters=_carried_names(session, workspace_id, DerivedCluster, cap),
    )


def carried_rejections(database: Database, workspace_id: str, *, cap: int) -> CarriedRejections:
    """Return the newest rejection per carried identity across every CV version."""
    with database.session() as session:
        return carried_rejections_in(session, workspace_id, cap=cap)


_MAX_REFUSED_POSTINGS = 10


def _reported_refusal_pattern(
    row: RefusalPatternProposal, standing: dict[tuple[str, str], list[tuple[str, str]]]
) -> RefusalPatternProposalResponse:
    """Return one stored refusal-pattern proposal as the inbox reports it."""
    refused = dict.fromkeys(standing.get((row.condition, row.normalized_reason), ()))
    return RefusalPatternProposalResponse(
        id=row.id,
        condition=row.condition,
        normalized_reason=row.normalized_reason,
        reason_examples=list(row.reason_examples),
        refused_postings=[
            RefusedPostingResponse(employer=employer, title=title)
            for employer, title in list(refused)[:_MAX_REFUSED_POSTINGS]
        ],
        refusal_count=row.refusal_count,
        first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at,
        state=SearchProposalState(row.state),
        proposed_at=row.proposed_at,
        decided_at=row.decided_at,
    )


class RefusalPatternProposalService:
    """Recurring refusals raised for a ruling, and the ruling recorded on each."""

    def __init__(self, database: Database) -> None:
        """Bind the service to one durable database."""
        self.database = database

    def list(self, workspace_id: str) -> list[RefusalPatternProposalResponse]:
        """Return every proposal for a workspace, newest first.

        An open proposal none of whose refusals still stands is left out: a
        re-screen returned every row it refused to the queue.
        """
        with self.database.session() as session:
            if session.get(Workspace, workspace_id) is None:
                raise NotFoundError(f"Unknown workspace {workspace_id!r}.")
            rows = session.scalars(
                select(RefusalPatternProposal)
                .where(RefusalPatternProposal.workspace_id == workspace_id)
                .order_by(
                    RefusalPatternProposal.proposed_at.desc(),
                    RefusalPatternProposal.id.desc(),
                )
            ).all()
            standing = standing_refusals(session, workspace_id)
            return [
                _reported_refusal_pattern(row, standing)
                for row in rows
                if row.state != SearchProposalState.PROPOSED.value
                or (row.condition, row.normalized_reason) in standing
            ]

    def accept(self, workspace_id: str, proposal_id: str) -> RefusalPatternProposalResponse:
        """Settle the pattern so aggregation stops proposing it."""
        with self.database.session() as session:
            row = self._proposal(session, workspace_id, proposal_id)
            state = self._state(row)
            if state is SearchProposalState.REJECTED:
                raise ConflictError(
                    "This refusal pattern was rejected. It must recur and be proposed again "
                    "before it can be accepted."
                )
            if state is SearchProposalState.ACCEPTED:
                return _reported_refusal_pattern(row, standing_refusals(session, workspace_id))
            row.state = SearchProposalState.ACCEPTED.value
            row.decided_at = utc_now()
            return _reported_refusal_pattern(row, standing_refusals(session, workspace_id))

    def reject(self, workspace_id: str, proposal_id: str) -> RefusalPatternProposalResponse:
        """Leave every future refusal on this pattern a case-by-case decision."""
        with self.database.session() as session:
            row = self._proposal(session, workspace_id, proposal_id)
            state = self._state(row)
            if state is SearchProposalState.REJECTED:
                return _reported_refusal_pattern(row, standing_refusals(session, workspace_id))
            if state is SearchProposalState.ACCEPTED:
                raise ConflictError(
                    "This refusal pattern was already accepted and cannot be rejected here."
                )
            row.state = SearchProposalState.REJECTED.value
            row.decided_at = utc_now()
            return _reported_refusal_pattern(row, standing_refusals(session, workspace_id))

    @staticmethod
    def _proposal(session: Session, workspace_id: str, proposal_id: str) -> RefusalPatternProposal:
        """Return one proposal owned by this workspace."""
        row = session.get(RefusalPatternProposal, proposal_id)
        if row is None or row.workspace_id != workspace_id:
            raise NotFoundError(
                f"Refusal-pattern proposal {proposal_id!r} does not exist in this workspace."
            )
        return row

    @staticmethod
    def _state(row: RefusalPatternProposal) -> SearchProposalState:
        """Return a stored state, refusing values this build cannot act on."""
        try:
            return SearchProposalState(row.state)
        except ValueError as error:
            raise ValidationError(
                "This refusal pattern cannot be decided because its state cannot be read."
            ) from error


class MasterCvProposalService:
    """The inbox of master-CV proposals for one workspace."""

    def __init__(self, database: Database) -> None:
        """Bind the inbox to its durable store."""
        self.database = database

    def list(self, workspace_id: str) -> list[MasterCvProposalResponse]:
        """Return every proposal in this workspace, newest last."""
        with self.database.session() as session:
            if session.get(Workspace, workspace_id) is None:
                raise NotFoundError(f"Unknown workspace {workspace_id!r}.")
            rows = session.scalars(
                select(MasterCvProposal)
                .where(MasterCvProposal.workspace_id == workspace_id)
                .order_by(MasterCvProposal.proposed_at, MasterCvProposal.id)
            ).all()
            return [_reported_master_cv_proposal(row) for row in rows]

    def accept(self, workspace_id: str, proposal_id: str) -> MasterCvProposalResponse:
        """Rule this proposal accepted; the master-CV editor takes it from here."""
        return self._decide(workspace_id, proposal_id, SearchProposalState.ACCEPTED)

    def reject(self, workspace_id: str, proposal_id: str) -> MasterCvProposalResponse:
        """Refuse this proposal; the aggregator will not resurface it."""
        return self._decide(workspace_id, proposal_id, SearchProposalState.REJECTED)

    def _decide(
        self, workspace_id: str, proposal_id: str, state: SearchProposalState
    ) -> MasterCvProposalResponse:
        with self.database.session() as session:
            row = session.get(MasterCvProposal, proposal_id)
            if row is None or row.workspace_id != workspace_id:
                raise NotFoundError(
                    f"Master-CV proposal {proposal_id!r} does not exist in this workspace."
                )
            row.state = state.value
            row.decided_at = utc_now()
            return _reported_master_cv_proposal(row)


def _reported_master_cv_proposal(row: MasterCvProposal) -> MasterCvProposalResponse:
    """Return one stored master-CV proposal as the inbox reports it."""
    return MasterCvProposalResponse(
        id=row.id,
        kind=MasterCvProposalKind(row.kind),
        requirement_key=row.requirement_key,
        requirement_text=row.requirement_text,
        evidence_note=row.evidence_note,
        posting_count=row.posting_count,
        first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at,
        state=SearchProposalState(row.state),
        proposed_at=row.proposed_at,
        decided_at=row.decided_at,
    )
