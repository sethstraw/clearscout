"""Decide which posting an evaluation appends to, and what it supersedes.

Identity work only: matching a proposal to a tracked posting, creating the
posting and its employer when the ledger has never seen it, recording the
identifiers it is now known by, and refusing an actionable decision for a place
this workspace cannot work in. The row lock and every write belong to the
transaction the caller opened.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from tailorcv.application.contracts import EvaluationCommit
from tailorcv.application.discovery.pending import pending_closing_date
from tailorcv.application.failures import ConflictError, NotFoundError, ValidationError
from tailorcv.application.workspaces import parameters_in
from tailorcv.application.workspaces.records.access import audit_columns
from tailorcv.domain import EvaluationDecision
from tailorcv.domain.identity import normalize_text, normalize_url, requisition_key
from tailorcv.infrastructure.persistence.models import (
    Employer,
    JobEvaluation,
    JobPosting,
    PendingDiscovery,
    PostingIdentifier,
    PostingUrl,
    Source,
    Workspace,
)

# Only these are gated on being able to work where the role is.
ACTIONABLE_DECISIONS = frozenset({EvaluationDecision.APPLY_NOW, EvaluationDecision.PREMIUM_TARGET})


@dataclass(frozen=True)
class _ResolvedTarget:
    """The job an evaluation appends to, and what it supersedes.

    ``replay`` marks a proposal identical to the current evaluation, which is
    answered with the stored detail rather than a second version.
    """

    job: JobPosting
    pending: PendingDiscovery | None
    current: JobEvaluation | None
    replay: bool = False


def _transfer_pending_closing_date(
    session: Session, job: JobPosting, pending: PendingDiscovery
) -> None:
    personal_date, edited_at = pending_closing_date(session, pending.workspace_id, pending.id)
    session.refresh(job, with_for_update=True)
    if job.closing_date_override_updated_at is None:
        job.closing_date_override = personal_date
        job.closing_date_override_updated_at = edited_at


def _active_pending(
    session: Session, workspace_id: str, pending_discovery_id: str | None
) -> PendingDiscovery | None:
    """Return the named pending discovery, refusing one outside this active workspace."""
    if pending_discovery_id is None:
        return None
    pending = session.get(PendingDiscovery, pending_discovery_id)
    if pending is None or pending.workspace_id != workspace_id or pending.archived_at is not None:
        raise NotFoundError("The pending discovery does not exist in this active workspace.")
    return pending


def _target_job(
    session: Session, proposal: EvaluationCommit, job_posting_id: str | None
) -> JobPosting | None:
    """Return the tracked posting this evaluation appends to, or None to create one."""
    job = session.get(JobPosting, job_posting_id) if job_posting_id is not None else None
    if job is not None and job.workspace_id != proposal.workspace_id:
        raise NotFoundError("The evaluation job does not exist in this active workspace.")
    if job is None and job_posting_id is not None:
        raise NotFoundError("The evaluation job does not exist in this active workspace.")
    if job is None:
        return _find_job(session, proposal)
    return job


def _current_evaluation(session: Session, job: JobPosting) -> JobEvaluation | None:
    """Return the newest evaluation version stored for a posting."""
    return session.scalar(
        select(JobEvaluation)
        .where(JobEvaluation.job_posting_id == job.id)
        .order_by(
            JobEvaluation.version_number.desc(),
            JobEvaluation.created_at.desc(),
            JobEvaluation.id.desc(),
        )
        .limit(1)
    )


def _resolve_target(
    session: Session,
    proposal: EvaluationCommit,
    *,
    digest: str,
    pending_discovery_id: str | None,
    job_posting_id: str | None,
    actor: str,
    origin: str,
    correlation_id: str | None,
) -> _ResolvedTarget:
    """Find or create the job this evaluation belongs to, holding its row lock."""
    workspace = session.get(Workspace, proposal.workspace_id)
    if workspace is None or workspace.archived_at is not None:
        raise NotFoundError(f"Workspace {proposal.workspace_id!r} does not exist.")
    pending = _active_pending(session, proposal.workspace_id, pending_discovery_id)
    _assert_residence_eligible(session, proposal)
    job = _target_job(session, proposal, job_posting_id)
    if job is None:
        return _ResolvedTarget(
            job=_new_job(
                session, proposal, actor=actor, origin=origin, correlation_id=correlation_id
            ),
            pending=pending,
            current=None,
        )
    if job.archived_at is not None:
        raise NotFoundError(f"Job {job.id!r} does not exist in this active workspace.")
    locked_job = session.scalar(select(JobPosting).where(JobPosting.id == job.id).with_for_update())
    if locked_job is None:
        raise NotFoundError(f"Job {job.id!r} no longer exists.")
    job = locked_job
    current = _current_evaluation(session, job)
    if current is None:
        if proposal.supersedes_evaluation_id is not None:
            raise ConflictError(
                "This posting has no current evaluation to supersede. "
                "Submit the first version without supersedes_evaluation_id."
            )
        return _ResolvedTarget(job=job, pending=pending, current=None)
    if current.payload_sha256 == digest:
        return _ResolvedTarget(job=job, pending=pending, current=current, replay=True)
    if proposal.supersedes_evaluation_id != current.id:
        raise ConflictError(
            "A changed evaluation must supersede the current evaluation "
            f"{current.id!r} (version "
            f"{current.version_number}). Refresh the job and retry."
        )
    return _ResolvedTarget(job=job, pending=pending, current=current)


def _new_job(
    session: Session,
    proposal: EvaluationCommit,
    *,
    actor: str,
    origin: str,
    correlation_id: str | None,
) -> JobPosting:
    """Append the posting an evaluation names when the ledger has never seen it."""
    if proposal.supersedes_evaluation_id is not None:
        raise ConflictError(
            "A new posting cannot supersede an evaluation. "
            "Submit the first version without supersedes_evaluation_id."
        )
    employer = _employer(
        session,
        proposal.workspace_id,
        proposal.employer,
        actor=actor,
        origin=origin,
        correlation_id=correlation_id,
    )
    job = JobPosting(
        workspace_id=proposal.workspace_id,
        employer_id=employer.id,
        title=proposal.title.strip(),
        normalized_title=normalize_text(proposal.title),
        first_seen_at=proposal.first_seen_at,
        **audit_columns(actor, origin, correlation_id),
    )
    session.add(job)
    session.flush()
    return job


def _employer(
    session: Session,
    workspace_id: str,
    name: str,
    *,
    actor: str,
    origin: str,
    correlation_id: str | None,
) -> Employer:
    """Return this workspace's employer of that name, creating it once."""
    normalized = normalize_text(name)
    employer = session.scalar(
        select(Employer).where(
            Employer.workspace_id == workspace_id,
            Employer.normalized_name == normalized,
        )
    )
    if employer is None:
        employer = Employer(
            workspace_id=workspace_id,
            name=name.strip(),
            normalized_name=normalized,
            **audit_columns(actor, origin, correlation_id),
        )
        session.add(employer)
        session.flush()
    return employer


def find_job_by_durable_identifier(
    session: Session,
    *,
    workspace_id: str,
    employer: str,
    requisition_id: str,
    canonical_url: str,
    discovery_url: str,
    active_only: bool = False,
) -> JobPosting | None:
    """Return the posting these employer-scoped identifiers already belong to.

    Employer plus title is not considered here because two openings can share
    both and this lookup must not conflate them. Callers that want that fallback
    use :func:`_find_job`.
    """
    active = [JobPosting.archived_at.is_(None)] if active_only else []
    if requisition_id.strip():
        requisition = requisition_key(employer, requisition_id)
        found = session.scalar(
            select(JobPosting)
            .join(PostingIdentifier)
            .where(
                PostingIdentifier.workspace_id == workspace_id,
                PostingIdentifier.identifier_type == "requisition",
                PostingIdentifier.identifier_value == requisition,
                *active,
            )
        )
        if found is not None:
            return found
    for raw_url in (canonical_url, discovery_url):
        normalized = normalize_url(raw_url)
        if not normalized:
            continue
        found = session.scalar(
            select(JobPosting)
            .join(PostingIdentifier)
            .where(
                PostingIdentifier.workspace_id == workspace_id,
                PostingIdentifier.identifier_type == "url",
                PostingIdentifier.identifier_value == normalized,
                *active,
            )
        )
        if found is not None:
            return found
        found = session.scalar(
            select(JobPosting)
            .join(PostingUrl)
            .where(
                JobPosting.workspace_id == workspace_id,
                JobPosting.archived_at.is_(None),
                PostingUrl.archived_at.is_(None),
                PostingUrl.normalized_url == normalized,
            )
        )
        if found is not None:
            return found
    return None


def remember_posting_url(
    session: Session,
    job: JobPosting,
    *,
    url: str,
    source_id: str | None,
    url_type: str,
    make_primary: bool,
    actor: str,
    origin: str,
    correlation_id: str | None,
) -> None:
    """Record a posting URL and make it the current read target when requested."""
    normalized = normalize_url(url)
    if not normalized:
        return
    audit = audit_columns(actor, origin, correlation_id)
    posting_url = session.scalar(
        select(PostingUrl).where(
            PostingUrl.job_posting_id == job.id,
            PostingUrl.normalized_url == normalized,
        )
    )
    if make_primary:
        for existing in session.scalars(
            select(PostingUrl).where(
                PostingUrl.job_posting_id == job.id,
                PostingUrl.is_primary.is_(True),
            )
        ):
            existing.is_primary = False
    if posting_url is None:
        session.add(
            PostingUrl(
                job_posting_id=job.id,
                source_id=source_id,
                url_type=url_type,
                url=url,
                normalized_url=normalized,
                is_primary=make_primary,
                **audit,
            )
        )
    elif make_primary:
        posting_url.is_primary = True
    if (
        session.scalar(
            select(PostingIdentifier.id).where(
                PostingIdentifier.workspace_id == job.workspace_id,
                PostingIdentifier.identifier_type == "url",
                PostingIdentifier.identifier_value == normalized,
            )
        )
        is None
    ):
        session.add(
            PostingIdentifier(
                workspace_id=job.workspace_id,
                job_posting_id=job.id,
                identifier_type="url",
                identifier_value=normalized,
                **audit,
            )
        )


def _find_job(session: Session, proposal: EvaluationCommit) -> JobPosting | None:
    """Find the posting this proposal is about, most reliable identifier first.

    A requisition identifier is the employer's own, a normalized URL is the
    address it was found at, and employer plus title is the last resort because
    two openings can legitimately share both.
    """
    found = find_job_by_durable_identifier(
        session,
        workspace_id=proposal.workspace_id,
        employer=proposal.employer,
        requisition_id=proposal.requisition_id,
        canonical_url=proposal.canonical_url,
        discovery_url=proposal.discovery_url,
    )
    if found is not None:
        return found
    # A proposal that names any durable identifier and did not match one must
    # not fall back to employer plus title; two openings can share both.
    if (
        proposal.requisition_id.strip()
        or normalize_url(proposal.canonical_url)
        or normalize_url(proposal.discovery_url)
    ):
        return None
    return session.scalar(
        select(JobPosting)
        .join(Employer)
        .where(
            JobPosting.workspace_id == proposal.workspace_id,
            Employer.normalized_name == normalize_text(proposal.employer),
            JobPosting.normalized_title == normalize_text(proposal.title),
        )
        .order_by(JobPosting.first_seen_at)
    )


def _named_source(session: Session, proposal: EvaluationCommit) -> Source | None:
    """Return the registered source this proposal names, if it names one."""
    if not proposal.source_name.strip():
        return None
    return session.scalar(
        select(Source).where(
            Source.workspace_id == proposal.workspace_id,
            Source.name == proposal.source_name.strip(),
        )
    )


def _identifiers(
    session: Session,
    job: JobPosting,
    proposal: EvaluationCommit,
    *,
    source: Source | None,
    actor: str,
    origin: str,
    correlation_id: str | None,
) -> None:
    """Record the requisition and URLs this posting is now known by."""
    audit = audit_columns(actor, origin, correlation_id)
    if proposal.requisition_id.strip():
        requisition = requisition_key(proposal.employer, proposal.requisition_id)
        if (
            session.scalar(
                select(PostingIdentifier.id).where(
                    PostingIdentifier.workspace_id == job.workspace_id,
                    PostingIdentifier.identifier_type == "requisition",
                    PostingIdentifier.identifier_value == requisition,
                )
            )
            is None
        ):
            session.add(
                PostingIdentifier(
                    workspace_id=job.workspace_id,
                    job_posting_id=job.id,
                    identifier_type="requisition",
                    identifier_value=requisition,
                    **audit,
                )
            )
    seen: set[str] = set()
    for url_type, raw_url in (
        ("canonical", proposal.canonical_url),
        ("discovery", proposal.discovery_url),
    ):
        normalized = normalize_url(raw_url)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        remember_posting_url(
            session,
            job,
            url=raw_url,
            source_id=source.id if source is not None else None,
            url_type=url_type,
            make_primary=url_type == "canonical",
            actor=actor,
            origin=origin,
            correlation_id=correlation_id,
        )


def _location_disjuncts(rule: str) -> list[str]:
    """Split a multi-market residence answer ("canada_remote or us_remote") into keys."""
    parts = [rule]
    for separator in (" or ", ",", "/"):
        parts = [piece for part in parts for piece in part.split(separator)]
    return [stripped for part in parts if (stripped := part.strip())]


def _assert_residence_eligible(session: Session, proposal: EvaluationCommit) -> None:
    """Refuse an actionable decision for a place this person cannot work.

    The allowlist is the workspace's own declared locations. A workspace that
    has declared none is not refused; only the shape rule in
    :class:`EvaluationCommit` applies.
    """
    if proposal.decision not in ACTIONABLE_DECISIONS:
        return
    parameters = parameters_in(session, proposal.workspace_id)
    if not parameters.locations:
        return
    rule = proposal.eligibility.workspace_location_key.strip()
    allowed = parameters.actionable_locations
    if rule in allowed:
        return
    # A posting remote in several declared markets answers with a disjunction;
    # it is workable when any one named market is.
    disjuncts = _location_disjuncts(rule)
    if len(disjuncts) > 1 and any(part in allowed for part in disjuncts):
        return
    declared = parameters.location(rule)
    if declared is not None:
        raise ValidationError(
            f"{proposal.decision.value!r} is an actionable decision, but this workspace "
            f"records {rule!r} ({declared.render()}) as {declared.eligibility.value}."
        )
    listed = ", ".join(allowed) or "none"
    raise ValidationError(
        f"{proposal.decision.value!r} is an actionable decision, but "
        f"{rule!r} is not a location this workspace can work in.\n"
        f"  Locations declared as workable: {listed}\n"
        "  Add it under [job_search] in your settings file, or on the Job search page."
    )


@dataclass(frozen=True)
class EvaluatedJobMatch:
    """The current evaluation of a posting a pending discovery already names."""

    job_posting_id: str
    evaluation_id: str
    decision: str
    evaluated_at: datetime


def _pending_identifier_indices(
    discoveries: Iterable[PendingDiscovery],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    requisitions: dict[str, list[str]] = {}
    urls: dict[str, list[str]] = {}
    for row in discoveries:
        pending_id = row.id
        if row.requisition_id.strip():
            key = requisition_key(row.employer, row.requisition_id)
            requisitions.setdefault(key, []).append(pending_id)
        for raw_url in (row.canonical_url, row.discovery_url):
            normalized = normalize_url(raw_url)
            if normalized:
                urls.setdefault(normalized, []).append(pending_id)
    return requisitions, urls


def _resolve_pending_to_job(
    session: Session,
    workspace_id: str,
    identifier_type: str,
    index: dict[str, list[str]],
    pending_to_job: dict[str, str],
) -> None:
    if not index:
        return
    rows = session.execute(
        select(PostingIdentifier.identifier_value, PostingIdentifier.job_posting_id).where(
            PostingIdentifier.workspace_id == workspace_id,
            PostingIdentifier.identifier_type == identifier_type,
            PostingIdentifier.identifier_value.in_(list(index)),
        )
    ).all()
    for value, job_id in rows:
        for pending_id in index.get(value, ()):
            pending_to_job.setdefault(pending_id, job_id)


def _latest_evaluation_by_job(
    session: Session, job_ids: list[str]
) -> dict[str, tuple[str, str, datetime]]:
    evaluation_rows = session.execute(
        select(
            JobEvaluation.job_posting_id,
            JobEvaluation.id,
            JobEvaluation.decision,
            JobEvaluation.evaluated_at,
            JobEvaluation.version_number,
        )
        .where(JobEvaluation.job_posting_id.in_(job_ids))
        .order_by(JobEvaluation.job_posting_id, JobEvaluation.version_number.desc())
    ).all()
    latest: dict[str, tuple[str, str, datetime]] = {}
    for job_id, evaluation_id, decision, evaluated_at, _version in evaluation_rows:
        if job_id not in latest:
            latest[job_id] = (evaluation_id, decision, evaluated_at)
    return latest


def evaluated_jobs_for_pending(
    session: Session,
    workspace_id: str,
    discoveries: Iterable[PendingDiscovery],
) -> dict[str, EvaluatedJobMatch]:
    """Return the current evaluation of every pending whose durable identifiers name one.

    Batched to two IN queries so a queue of hundreds does not run per-row.
    """
    requisitions, urls = _pending_identifier_indices(discoveries)
    if not requisitions and not urls:
        return {}
    pending_to_job: dict[str, str] = {}
    _resolve_pending_to_job(session, workspace_id, "requisition", requisitions, pending_to_job)
    _resolve_pending_to_job(session, workspace_id, "url", urls, pending_to_job)
    if not pending_to_job:
        return {}
    latest_by_job = _latest_evaluation_by_job(session, list(set(pending_to_job.values())))
    matches: dict[str, EvaluatedJobMatch] = {}
    for pending_id, job_id in pending_to_job.items():
        latest = latest_by_job.get(job_id)
        if latest is None:
            continue
        evaluation_id, decision, evaluated_at = latest
        matches[pending_id] = EvaluatedJobMatch(
            job_posting_id=job_id,
            evaluation_id=evaluation_id,
            decision=decision,
            evaluated_at=evaluated_at,
        )
    return matches
