"""Offer what a vocabulary proposal synthesized to the person who has to rule on it.

Titles, employer archetypes, and clusters are written against the master CV
version they were read from. A ruling already made survives a later run, and a
reviewed proposal does not spend the inbox budget for its kind. A rejection carries
across master-CV re-imports through its content address, so the same identity
is not offered again even after a new CV version becomes current.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tailorcv.application.discovery.derived_vocabulary_rules import (
    SearchArchetype,
    SearchCluster,
    SearchTitle,
    same_employer_archetype,
)
from tailorcv.domain import SearchProposalState, new_id
from tailorcv.domain.identity import normalize_text
from tailorcv.domain.search_parameters import (
    MAX_ARCHETYPES,
    MAX_CLUSTERS,
    MAX_TITLES,
    ClusterKind,
)
from tailorcv.domain.statements import derived_proposal_identity
from tailorcv.infrastructure.persistence.models import (
    DerivedCluster,
    DerivedEmployerArchetype,
    DerivedSearchTitle,
)


@dataclass(frozen=True)
class ProposalWriteReport:
    """Count the durable outcome of one proposal axis in a vocabulary run."""

    proposed: int = 0
    retained: int = 0
    dropped: tuple[dict[str, str], ...] = ()


def _proposal_filters(
    row_type: Any,
    workspace_id: str,
    master_cv_version_id: str,
    extra_filter: dict[str, Any] | None,
) -> tuple[list[Any], list[Any]]:
    """Return (scope, workspace) filter lists, both narrowed by extra_filter."""
    scope: list[Any] = [
        row_type.workspace_id == workspace_id,
        row_type.master_cv_version_id == master_cv_version_id,
    ]
    workspace: list[Any] = [row_type.workspace_id == workspace_id]
    for column_name, value in (extra_filter or {}).items():
        column = getattr(row_type, column_name)
        scope.append(column == value)
        workspace.append(column == value)
    return scope, workspace


def _proposal_identity(row: Any) -> tuple[str, str]:
    """Return the workspace decision identity, including a cluster's independent kind."""
    stored = row.carried_identity
    if stored is None:
        stored = getattr(row, "normalized_title", getattr(row, "normalized_name", str(row.id)))
    return (getattr(row, "kind", ""), stored)


def _effective_priority(row: Any) -> tuple[int, datetime, datetime, str]:
    """Rank decided rows above pending observations, then keep the latest ruling."""
    epoch = datetime.min.replace(tzinfo=UTC)
    decided_at = row.decided_at or epoch
    return (
        row.state != SearchProposalState.PROPOSED.value,
        decided_at,
        row.created_at,
        str(row.id),
    )


def effective_proposal_rows(
    session: Session,
    row_type: Any,
    workspace_id: str,
    master_cv_version_id: str,
    extra_filter: dict[str, Any] | None = None,
) -> list[Any]:
    """Return one workspace-effective row per identity.

    Decisions survive a master-CV refresh. Undecided suggestions do not: their
    citations belong to one CV version and only the current version remains
    actionable. A newer decision supersedes an older one; a pending duplicate
    cannot override a decision the user already made.
    """
    _, workspace_filters = _proposal_filters(
        row_type, workspace_id, master_cv_version_id, extra_filter
    )
    rows = session.scalars(select(row_type).where(*workspace_filters)).all()
    effective: dict[tuple[str, str], Any] = {}
    for row in rows:
        if (
            row.state == SearchProposalState.PROPOSED.value
            and row.master_cv_version_id != master_cv_version_id
        ):
            continue
        identity = _proposal_identity(row)
        kept = effective.get(identity)
        if kept is None or _effective_priority(row) > _effective_priority(kept):
            effective[identity] = row
    return sorted(
        effective.values(),
        key=lambda row: (row.created_at, row.position, str(row.id)),
    )


def active_proposal_count(
    session: Session,
    row_type: Any,
    workspace_id: str,
    master_cv_version_id: str,
    extra_filter: dict[str, Any] | None = None,
) -> int:
    """Count unreviewed workspace-effective identities; accepted vocabulary is separate."""
    return sum(
        1
        for row in effective_proposal_rows(
            session,
            row_type,
            workspace_id,
            master_cv_version_id,
            extra_filter,
        )
        if row.state == SearchProposalState.PROPOSED.value
    )


def remaining_capacity(
    session: Session,
    row_type: Any,
    workspace_id: str,
    master_cv_version_id: str,
    cap: int,
    extra_filter: dict[str, Any] | None = None,
) -> int:
    """Return how many more workspace-effective proposals this inbox can hold.

    Mirrors the count :func:`remember_proposals` uses to drop overflow, so the
    prompt can be told the same ceiling the write path enforces.
    """
    used = active_proposal_count(
        session, row_type, workspace_id, master_cv_version_id, extra_filter
    )
    return max(cap - used, 0)


def _carried_state(
    item: Any,
    identity: str,
    decisions: dict[str, str],
    decided_rows: Sequence[Any],
    equivalent_to_accepted: Callable[[Any, Any], bool] | None,
) -> str | None:
    """Return the prior ruling that prevents this proposal being offered."""
    exact = decisions.get(identity)
    if exact is not None or equivalent_to_accepted is None:
        return exact
    return next(
        (
            row.state
            for row in decided_rows
            if row.state == SearchProposalState.ACCEPTED.value and equivalent_to_accepted(item, row)
        ),
        None,
    )


def remember_proposals(
    session: Session,
    *,
    row_type: Any,
    workspace_id: str,
    master_cv_version_id: str,
    usable: list[Any],
    normalized_of: Callable[[Any], str],
    key_of: Callable[[Any], str],
    build_row: Callable[[Any, int, str, str], Any],
    update_row: Callable[[Any, Any], None],
    cap: int,
    extra_filter: dict[str, Any] | None = None,
    equivalent_to_accepted: Callable[[Any, Any], bool] | None = None,
) -> ProposalWriteReport:
    """Write proposals into their inbox, honoring rulings from every CV version.

    An existing row for the current version keeps its ruling and content-refreshes
    a proposed one. A ruling carried from a superseded version is honored: the
    same content-address identity is not offered again, whatever CV version
    recorded it.
    """
    scope_filters, _ = _proposal_filters(row_type, workspace_id, master_cv_version_id, extra_filter)
    existing = {key_of(row): row for row in session.scalars(select(row_type).where(*scope_filters))}
    decided_rows = [
        row
        for row in effective_proposal_rows(
            session,
            row_type,
            workspace_id,
            master_cv_version_id,
            extra_filter,
        )
        if row.state in (SearchProposalState.ACCEPTED.value, SearchProposalState.REJECTED.value)
    ]
    carried_decisions = {
        row.carried_identity: row.state for row in decided_rows if row.carried_identity
    }
    remaining = remaining_capacity(
        session,
        row_type,
        workspace_id,
        master_cv_version_id,
        cap,
        extra_filter,
    )
    dropped: list[dict[str, str]] = []
    retained = 0
    for offered, item in enumerate(usable):
        normalized = normalized_of(item)
        carried_id = derived_proposal_identity(normalized)
        stored = existing.get(normalized)
        if stored is not None:
            if stored.state == SearchProposalState.PROPOSED.value:
                update_row(stored, item)
            retained += 1
            continue
        carried_state = _carried_state(
            item,
            carried_id,
            carried_decisions,
            decided_rows,
            equivalent_to_accepted,
        )
        if carried_state is not None:
            dropped.append(
                {
                    "identity": carried_id,
                    "reason": f"carried_{carried_state}",
                }
            )
            continue
        if remaining == 0:
            dropped.append({"identity": carried_id, "reason": "proposal_capacity"})
            continue
        row = build_row(item, offered, normalized, carried_id)
        session.add(row)
        existing[normalized] = row
        remaining -= 1
        retained += 1
    return ProposalWriteReport(proposed=len(usable), retained=retained, dropped=tuple(dropped))


class SearchProposalInbox:
    """Write one run's proposals into the queues a person rules on."""

    def remember_titles_in_session(
        self,
        session: Session,
        workspace_id: str,
        master_cv_version_id: str,
        titles: list[SearchTitle],
        *,
        correlation_id: str | None,
    ) -> ProposalWriteReport:
        """Propose current-version titles inside the caller's transaction."""
        usable = [item for item in titles if item.used and item.evidence_ids][:MAX_TITLES]
        if not usable:
            return ProposalWriteReport()

        def build(
            item: SearchTitle, offered: int, normalized: str, carried: str
        ) -> DerivedSearchTitle:
            return DerivedSearchTitle(
                id=new_id(),
                workspace_id=workspace_id,
                master_cv_version_id=master_cv_version_id,
                title=item.title,
                normalized_title=normalized,
                reason=item.reason,
                evidence_ids=list(item.evidence_ids),
                lane=None if item.lane is None else item.lane.value,
                position=offered,
                state=SearchProposalState.PROPOSED.value,
                origin="search.vocabulary",
                correlation_id=correlation_id,
                carried_identity=carried,
            )

        def update(stored: DerivedSearchTitle, item: SearchTitle) -> None:
            stored.title = item.title
            stored.reason = item.reason
            stored.evidence_ids = list(item.evidence_ids)
            stored.lane = None if item.lane is None else item.lane.value

        return remember_proposals(
            session,
            row_type=DerivedSearchTitle,
            workspace_id=workspace_id,
            master_cv_version_id=master_cv_version_id,
            usable=usable,
            normalized_of=lambda item: normalize_text(item.title),
            key_of=lambda row: row.normalized_title,
            build_row=build,
            update_row=update,
            cap=MAX_TITLES,
        )

    def remember_archetypes_in_session(
        self,
        session: Session,
        workspace_id: str,
        master_cv_version_id: str,
        archetypes: list[SearchArchetype],
        *,
        correlation_id: str | None,
    ) -> ProposalWriteReport:
        """Propose current-version archetypes inside the caller's transaction."""
        usable = [item for item in archetypes if item.used and item.evidence_ids][:MAX_ARCHETYPES]
        if not usable:
            return ProposalWriteReport()

        def build(
            item: SearchArchetype, offered: int, normalized: str, carried: str
        ) -> DerivedEmployerArchetype:
            return DerivedEmployerArchetype(
                id=new_id(),
                workspace_id=workspace_id,
                master_cv_version_id=master_cv_version_id,
                name=item.name,
                normalized_name=normalized,
                because=item.because,
                evidence_ids=list(item.evidence_ids),
                position=offered,
                state=SearchProposalState.PROPOSED.value,
                origin="search.vocabulary",
                correlation_id=correlation_id,
                carried_identity=carried,
            )

        def update(stored: DerivedEmployerArchetype, item: SearchArchetype) -> None:
            stored.name = item.name
            stored.because = item.because
            stored.evidence_ids = list(item.evidence_ids)

        return remember_proposals(
            session,
            row_type=DerivedEmployerArchetype,
            workspace_id=workspace_id,
            master_cv_version_id=master_cv_version_id,
            usable=usable,
            normalized_of=lambda item: normalize_text(item.name),
            key_of=lambda row: row.normalized_name,
            build_row=build,
            update_row=update,
            cap=MAX_ARCHETYPES,
            equivalent_to_accepted=lambda item, row: same_employer_archetype(item.name, row.name),
        )

    def remember_clusters_in_session(
        self,
        session: Session,
        workspace_id: str,
        master_cv_version_id: str,
        clusters: list[SearchCluster],
        *,
        correlation_id: str | None,
    ) -> dict[str, ProposalWriteReport]:
        """Propose current-version clusters inside the caller's transaction."""
        reports: dict[str, ProposalWriteReport] = {}
        for kind in ClusterKind:
            usable = [
                item for item in clusters if item.kind is kind and item.used and item.evidence_ids
            ][:MAX_CLUSTERS]
            if not usable:
                reports[kind.value] = ProposalWriteReport()
                continue

            def build(
                item: SearchCluster,
                offered: int,
                normalized: str,
                carried: str,
                _kind: ClusterKind = kind,
            ) -> DerivedCluster:
                return DerivedCluster(
                    id=new_id(),
                    workspace_id=workspace_id,
                    master_cv_version_id=master_cv_version_id,
                    kind=_kind.value,
                    name=item.name,
                    normalized_name=normalized,
                    terms=list(item.terms),
                    because=item.because,
                    evidence_ids=list(item.evidence_ids),
                    position=offered,
                    state=SearchProposalState.PROPOSED.value,
                    origin="search.vocabulary",
                    correlation_id=correlation_id,
                    carried_identity=carried,
                )

            def update(stored: DerivedCluster, item: SearchCluster) -> None:
                stored.name = item.name
                stored.terms = list(item.terms)
                stored.because = item.because
                stored.evidence_ids = list(item.evidence_ids)

            reports[kind.value] = remember_proposals(
                session,
                row_type=DerivedCluster,
                workspace_id=workspace_id,
                master_cv_version_id=master_cv_version_id,
                usable=usable,
                normalized_of=lambda item: normalize_text(item.name),
                key_of=lambda row: row.normalized_name,
                build_row=build,
                update_row=update,
                cap=MAX_CLUSTERS,
                extra_filter={"kind": kind.value},
            )
        return reports
