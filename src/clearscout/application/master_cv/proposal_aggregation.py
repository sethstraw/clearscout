"""Aggregate recurring rulings and gaps into master-CV proposals."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from clearscout.application.master_cv.proposal_models import MasterCvProposalKind
from clearscout.application.master_cv.versioning import current_version_id
from clearscout.application.workspaces.policy import LearningLoopPolicyService
from clearscout.domain import SearchProposalState, new_id, utc_now
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    JobEvaluation,
    JobPosting,
    MasterCvProposal,
    PostingRequirement,
    RequirementRuling,
    Workspace,
)

_WHITESPACE = re.compile(r"\s+")


def _normalize_note(value: str | None) -> str:
    """Return the note lowercased with runs of whitespace collapsed."""
    if not value:
        return ""
    return _WHITESPACE.sub(" ", value.casefold()).strip()


@dataclass
class _Aggregate:
    """Mutable accumulator for one requirement identity while grouping."""

    requirement_key: str
    requirement_text: str
    evidence_note: str | None
    postings: set[str] = field(default_factory=set)
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None

    def widen(self, posting_id: str, seen_at: datetime) -> None:
        """Extend the group with one more sighting."""
        self.postings.add(posting_id)
        if self.first_seen_at is None or seen_at < self.first_seen_at:
            self.first_seen_at = seen_at
        if self.last_seen_at is None or seen_at > self.last_seen_at:
            self.last_seen_at = seen_at


@dataclass(frozen=True)
class _Group:
    """One aggregation output row before it becomes a proposal."""

    requirement_key: str
    requirement_text: str
    evidence_note: str | None
    posting_count: int
    first_seen_at: datetime
    last_seen_at: datetime


def _promote_transferable_groups(
    session: Session, workspace_id: str, *, threshold: int
) -> list[_Group]:
    """Return groups where the same requirement identity is ruled Met on N+ postings."""
    rows = session.execute(
        select(
            RequirementRuling.requirement_key,
            RequirementRuling.note,
            PostingRequirement.text,
            PostingRequirement.job_posting_id,
            RequirementRuling.updated_at,
        )
        .join(
            PostingRequirement,
            PostingRequirement.requirement_key == RequirementRuling.requirement_key,
        )
        .join(
            JobPosting,
            JobPosting.id == PostingRequirement.job_posting_id,
        )
        .where(
            RequirementRuling.workspace_id == workspace_id,
            JobPosting.workspace_id == workspace_id,
            RequirementRuling.ruling == "met",
        )
    ).all()

    grouped: dict[tuple[str, str], _Aggregate] = {}
    for key, note, text, posting_id, ruled_at in rows:
        bucket = (key, _normalize_note(note))
        aggregate = grouped.setdefault(
            bucket,
            _Aggregate(requirement_key=key, requirement_text=text, evidence_note=note),
        )
        aggregate.widen(posting_id, ruled_at)

    return [
        _finalize(aggregate)
        for aggregate in grouped.values()
        if len(aggregate.postings) >= threshold
    ]


def _add_missing_groups(session: Session, workspace_id: str, *, threshold: int) -> list[_Group]:
    """Return groups where a requirement recurs across postings the user has not ruled."""
    version_id = current_version_id(session, workspace_id)
    if version_id is None:
        return []
    current_evaluation_ids = set(
        session.scalars(
            select(JobEvaluation.id)
            .join(JobPosting, JobPosting.id == JobEvaluation.job_posting_id)
            .where(
                JobPosting.workspace_id == workspace_id,
                JobEvaluation.master_cv_version_id == version_id,
            )
        ).all()
    )
    if not current_evaluation_ids:
        return []

    ruled_keys = set(
        session.scalars(
            select(RequirementRuling.requirement_key).where(
                RequirementRuling.workspace_id == workspace_id,
            )
        ).all()
    )

    rows = session.execute(
        select(
            PostingRequirement.requirement_key,
            PostingRequirement.text,
            PostingRequirement.job_posting_id,
            PostingRequirement.created_at,
        ).where(PostingRequirement.job_evaluation_id.in_(current_evaluation_ids))
    ).all()

    grouped: dict[str, _Aggregate] = {}
    for key, text, posting_id, observed_at in rows:
        if key in ruled_keys:
            continue
        aggregate = grouped.setdefault(
            key,
            _Aggregate(requirement_key=key, requirement_text=text, evidence_note=None),
        )
        aggregate.widen(posting_id, observed_at)

    return [
        _finalize(aggregate)
        for aggregate in grouped.values()
        if len(aggregate.postings) >= threshold
    ]


def _finalize(aggregate: _Aggregate) -> _Group:
    """Produce an immutable output group from a mutable accumulator."""
    if aggregate.first_seen_at is None or aggregate.last_seen_at is None:
        raise RuntimeError(
            f"aggregate for {aggregate.requirement_key} was finalized before any sighting"
        )
    return _Group(
        requirement_key=aggregate.requirement_key,
        requirement_text=aggregate.requirement_text,
        evidence_note=aggregate.evidence_note,
        posting_count=len(aggregate.postings),
        first_seen_at=aggregate.first_seen_at,
        last_seen_at=aggregate.last_seen_at,
    )


def _insert_missing(
    session: Session,
    workspace_id: str,
    *,
    kind: MasterCvProposalKind,
    groups: list[_Group],
) -> int:
    """Insert one row per group not already tracked in any state; return the count."""
    if not groups:
        return 0
    existing = set(
        session.scalars(
            select(MasterCvProposal.requirement_key).where(
                MasterCvProposal.workspace_id == workspace_id,
                MasterCvProposal.kind == kind,
            )
        ).all()
    )
    now = utc_now()
    inserted = 0
    for group in groups:
        if group.requirement_key in existing:
            continue
        session.add(
            MasterCvProposal(
                id=new_id(),
                workspace_id=workspace_id,
                kind=kind,
                requirement_key=group.requirement_key,
                requirement_text=group.requirement_text,
                evidence_note=group.evidence_note,
                posting_count=group.posting_count,
                first_seen_at=group.first_seen_at,
                last_seen_at=group.last_seen_at,
                proposed_at=now,
                state=SearchProposalState.PROPOSED.value,
            )
        )
        inserted += 1
    return inserted


@dataclass(frozen=True)
class AggregationReport:
    """How many proposals each aggregation added for one workspace."""

    promote_transferable: int
    add_missing: int


def refresh_master_cv_proposals(database: Database, workspace_id: str) -> AggregationReport:
    """Run both aggregations against one workspace's current state."""
    with database.session() as session:
        if session.get(Workspace, workspace_id) is None:
            return AggregationReport(promote_transferable=0, add_missing=0)
    policy = LearningLoopPolicyService(database).current(workspace_id)
    with database.session() as session:
        promote_groups = _promote_transferable_groups(
            session, workspace_id, threshold=policy.propose_after_ruling_count
        )
        missing_groups = _add_missing_groups(
            session, workspace_id, threshold=policy.propose_missing_after_count
        )
        promote_added = _insert_missing(
            session,
            workspace_id,
            kind=MasterCvProposalKind.PROMOTE_TRANSFERABLE,
            groups=promote_groups,
        )
        missing_added = _insert_missing(
            session, workspace_id, kind=MasterCvProposalKind.ADD_MISSING, groups=missing_groups
        )
    return AggregationReport(promote_transferable=promote_added, add_missing=missing_added)
