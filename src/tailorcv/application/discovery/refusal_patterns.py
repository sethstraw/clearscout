"""File rule refusals and aggregate repeated ones into hard-gate proposals."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, select
from sqlalchemy.orm import Session

from tailorcv.application.discovery.dispositions import (
    DiscoveryDispositionService,
    dismissed_pending_ids,
)
from tailorcv.application.discovery.pending import marked_ids
from tailorcv.application.workspaces.parameters import parameter_snapshot_in
from tailorcv.application.workspaces.policy import LearningLoopPolicyService
from tailorcv.domain import SearchProposalState, utc_now
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    PendingDiscovery,
    PostingRefusal,
    RefusalPatternProposal,
    Workspace,
)

# The publisher, not the person's rules, closes a posting, so no restore passes this
# condition and repeated closures never become a proposal a person could accept.
POSTING_STATUS_CONDITION = "posting status"

_MAX_EXAMPLES = 3
_WHITESPACE = re.compile(r"\s+")
# The posting text a reason quotes as its evidence, in straight or curly quotes.
_QUOTED = re.compile('"[^"]*"|\u201c[^\u201d]*\u201d')
_DANGLING = re.compile(r"[\s:;,]+$")


def normalize_reason(reason: str) -> str:
    """Return the grouping key: the reason without its quoted evidence, casefolded."""
    unquoted = _DANGLING.sub("", _QUOTED.sub("", reason))
    return _WHITESPACE.sub(" ", unquoted.strip().casefold())


def standing_refusals(
    session: Session, workspace_id: str
) -> dict[tuple[str, str], list[tuple[str, str]]]:
    """Return the employer and title of each refusal still in force, by pattern key.

    Only a refusal made under the current parameter set stands. One on a Pending
    row stands while that row stays dismissed; one on a tracked posting always does.
    """
    dismissed = dismissed_pending_ids(session, workspace_id)
    standing: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for condition, reason, pending_id, employer, title in session.execute(
        select(
            PostingRefusal.condition,
            PostingRefusal.reason,
            PostingRefusal.pending_discovery_id,
            PostingRefusal.employer,
            PostingRefusal.title,
        )
        .where(
            PostingRefusal.workspace_id == workspace_id,
            _current_parameters(session, workspace_id),
        )
        .order_by(PostingRefusal.refused_at.desc())
    ):
        if pending_id is None or pending_id in dismissed:
            standing.setdefault((condition, normalize_reason(reason or "")), []).append(
                (employer, title)
            )
    return standing


def restamp_refusals(session: Session, workspace_id: str, pending_id: str, condition: str) -> None:
    """File a row's latest refusal on `condition` again under the current parameter set.

    Refusals are immutable, so a re-screen that reconfirms one appends its copy.
    """
    parameter_set_id = parameter_snapshot_in(session, workspace_id).parameter_set_id
    filed = list(
        session.scalars(
            select(PostingRefusal)
            .where(
                PostingRefusal.workspace_id == workspace_id,
                PostingRefusal.pending_discovery_id == pending_id,
                PostingRefusal.condition == condition,
            )
            .order_by(PostingRefusal.refused_at.desc())
        )
    )
    if not filed or any(row.search_parameter_set_id == parameter_set_id for row in filed):
        return
    latest = filed[0]
    session.add(
        PostingRefusal(
            workspace_id=workspace_id,
            job_posting_id=latest.job_posting_id,
            pending_discovery_id=pending_id,
            search_parameter_set_id=parameter_set_id,
            employer=latest.employer,
            title=latest.title,
            condition=condition,
            reason=latest.reason,
            refused_at=datetime.now(UTC),
            origin="worker",
        )
    )


def _current_parameters(session: Session, workspace_id: str) -> ColumnElement[bool]:
    """Match a refusal made under the workspace's current parameter set."""
    parameter_set_id = parameter_snapshot_in(session, workspace_id).parameter_set_id
    if parameter_set_id is None:
        return PostingRefusal.search_parameter_set_id.is_(None)
    return PostingRefusal.search_parameter_set_id == parameter_set_id


def refresh_for_workspace(
    session: Session,
    workspace_id: str,
    *,
    threshold: int,
    window_days: int,
    now: datetime | None = None,
) -> list[str]:
    """Insert one PROPOSED row per (condition, normalized_reason) at or over threshold.

    Skips groups already carrying a PROPOSED or ACCEPTED row. Returns the ids of
    proposals it wrote.
    """
    if session.get(Workspace, workspace_id) is None:
        return []
    parameter_filter = _current_parameters(session, workspace_id)
    horizon = (now or utc_now()) - timedelta(days=window_days)
    # Grouped here rather than in SQL, whose trim() leaves inner whitespace, so the
    # key matches the one refusals file under.
    groups: dict[tuple[str, str], list[tuple[str, datetime]]] = {}
    for condition, reason, refused_at in session.execute(
        select(PostingRefusal.condition, PostingRefusal.reason, PostingRefusal.refused_at)
        .where(
            PostingRefusal.workspace_id == workspace_id,
            PostingRefusal.refused_at >= horizon,
            PostingRefusal.condition != POSTING_STATUS_CONDITION,
            parameter_filter,
        )
        .order_by(PostingRefusal.refused_at)
    ):
        if key := normalize_reason(reason or ""):
            groups.setdefault((condition, key), []).append((reason, refused_at))
    written: list[str] = []
    for (condition, key), filed in groups.items():
        if (count := len(filed)) < threshold:
            continue
        first_seen, last_seen = filed[0][1], filed[-1][1]
        examples = list(dict.fromkeys(reason for reason, _ in filed))[:_MAX_EXAMPLES]
        existing = session.scalar(
            select(RefusalPatternProposal).where(
                RefusalPatternProposal.workspace_id == workspace_id,
                RefusalPatternProposal.condition == condition,
                RefusalPatternProposal.normalized_reason == key,
            )
        )
        if existing is not None and existing.state in {
            SearchProposalState.PROPOSED.value,
            SearchProposalState.ACCEPTED.value,
        }:
            continue
        if existing is not None:
            existing.refusal_count = int(count)
            existing.last_seen_at = last_seen
            existing.state = SearchProposalState.PROPOSED.value
            existing.decided_at = None
            existing.proposed_at = utc_now()
            existing.reason_examples = examples
            written.append(str(existing.id))
            continue
        row = RefusalPatternProposal(
            workspace_id=workspace_id,
            condition=condition,
            normalized_reason=key,
            reason_examples=examples,
            first_seen_at=first_seen,
            last_seen_at=last_seen,
            refusal_count=int(count),
            proposed_at=utc_now(),
            state=SearchProposalState.PROPOSED.value,
            origin="refusal_hook",
        )
        session.add(row)
        session.flush()
        written.append(str(row.id))
    return written


def refresh_after_refusal(
    database: Database, workspace_id: str, *, threshold: int, window_days: int
) -> list[str]:
    """Open a session, aggregate, commit; return ids of proposals written."""
    with database.session() as session:
        return refresh_for_workspace(
            session, workspace_id, threshold=threshold, window_days=window_days
        )


@dataclass(frozen=True)
class RefusedPosting:
    """The posting a rule refused, as the refusal record names it."""

    workspace_id: str
    employer: str
    title: str
    pending_discovery_id: str | None = None
    job_posting_id: str | None = None
    search_parameter_set_id: str | None = None


def file_refusal(
    database: Database,
    posting: RefusedPosting,
    *,
    condition: str,
    reason: str,
    task_id: str | None,
    origin: str,
    refresh: bool = True,
) -> tuple[str, str]:
    """File a rule refusal once, dismiss its Pending row reversibly, and refresh proposals.

    Returns the condition and reason on record; a retry saying exactly the same
    thing files nothing further. A caller filing a batch passes ``refresh=False``
    and calls ``refresh_proposals`` once afterwards. A favorite or kept family
    files nothing and gets back what it passed in, so a person's own decision
    never becomes evidence for a rule.
    """
    target: ColumnElement[bool] | None = None
    if posting.pending_discovery_id is not None:
        target = PostingRefusal.pending_discovery_id == posting.pending_discovery_id
    elif posting.job_posting_id is not None:
        target = PostingRefusal.job_posting_id == posting.job_posting_id
    # The marker read and the refusal write share one write lock.
    with database.exclusive_session() as session:
        if posting.pending_discovery_id is not None and marked_ids(
            session, posting.workspace_id, [posting.pending_discovery_id]
        ):
            return condition, reason
        if target is not None:
            # A changed limit refuses the same condition with a different reason, and that
            # reason has to reach the ledger under the current parameter set.
            filed = session.scalars(
                select(PostingRefusal)
                .where(
                    PostingRefusal.workspace_id == posting.workspace_id,
                    target,
                    PostingRefusal.condition == condition,
                )
                .order_by(PostingRefusal.refused_at.desc())
            )
            # SQLite folds only ASCII case, so the grouping key is compared here.
            wanted = normalize_reason(reason)
            for established in filed:
                if normalize_reason(established.reason) == wanted:
                    return established.condition, established.reason
        session.add(
            PostingRefusal(
                workspace_id=posting.workspace_id,
                job_posting_id=posting.job_posting_id,
                pending_discovery_id=posting.pending_discovery_id,
                task_id=task_id,
                search_parameter_set_id=posting.search_parameter_set_id,
                employer=posting.employer[:300],
                title=posting.title[:500],
                condition=condition,
                reason=reason,
                refused_at=datetime.now(UTC),
                origin="worker",
            )
        )
        if posting.pending_discovery_id is not None:
            pending = session.get(PendingDiscovery, posting.pending_discovery_id)
            # An archived row has left the queue and the disposition path refuses to see
            # it; attempting one anyway would roll this refusal back with it.
            if pending is not None and pending.archived_at is None:
                DiscoveryDispositionService(database).dismiss_refusal_in_session(
                    session,
                    posting.workspace_id,
                    pending.id,
                    category=condition,
                    reason=reason,
                    search_parameter_set_id=posting.search_parameter_set_id,
                    actor="worker",
                    origin=origin,
                    correlation_id=task_id,
                )
    if refresh:
        refresh_proposals(database, posting.workspace_id)
    return condition, reason


def refresh_proposals(database: Database, workspace_id: str) -> list[str]:
    """Aggregate filed refusals under the workspace's own learning-loop policy."""
    policy = LearningLoopPolicyService(database).current(workspace_id)
    return refresh_after_refusal(
        database,
        workspace_id,
        threshold=policy.propose_after_count,
        window_days=policy.propose_window_days,
    )
