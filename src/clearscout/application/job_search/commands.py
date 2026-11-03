"""Write tracked-job and application state without provider work."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, time
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from clearscout.application.contracts import ApplicationEventInput
from clearscout.application.discovery.pending import retire_pending
from clearscout.application.documents.evidence_artifacts import EvidenceArtifactService
from clearscout.application.failures import ConflictError, NotFoundError, ValidationError
from clearscout.application.job_search._support import _application_event
from clearscout.application.job_search.evaluation_targets import (
    _employer,
    _transfer_pending_closing_date,
    find_job_by_durable_identifier,
    remember_posting_url,
)
from clearscout.application.job_search.models import (
    ApplicationEventResponse,
    ClosingDateEditInput,
    JobArchiveBatchResponse,
    JobArchiveResponse,
    JobDetailResponse,
    ManualJobReviewInput,
    PostingHintResponse,
)
from clearscout.application.job_search.read_models import _job_detail
from clearscout.application.workspaces.records.access import audit_columns
from clearscout.domain import (
    PROGRESSED_APPLICATION_STATUSES,
    ApplicationEventType,
    ApplicationStatus,
    utc_now,
)
from clearscout.domain.identity import normalize_text, normalize_url, requisition_key
from clearscout.infrastructure.persistence.models import (
    Application,
    ApplicationEvent,
    Artifact,
    AuditEvent,
    JobPosting,
    PendingDiscovery,
    PendingPostingContent,
    PostingIdentifier,
    PostingObservation,
    Workspace,
)

if TYPE_CHECKING:
    from clearscout.infrastructure.persistence.database import Database

# Named where the audit entry is written, so the promise the entry makes is
# visible beside it: archiving hides a job, it does not remove its history.
PRESERVED_CHILDREN = ["applications", "artifacts", "evaluations"]
MAX_JOB_ARCHIVE_BATCH = 250


def save_manual_job_review(
    database: Database,
    workspace_id: str,
    job_posting_id: str,
    review: ManualJobReviewInput,
    *,
    actor: str = "local_user",
    origin: str = "api",
    correlation_id: str | None = None,
) -> JobDetailResponse:
    """Record a person's current judgment without modifying model evidence."""
    with database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        job = session.get(JobPosting, job_posting_id)
        if (
            workspace is None
            or workspace.archived_at is not None
            or job is None
            or job.workspace_id != workspace_id
        ):
            raise NotFoundError(f"Job {job_posting_id!r} does not exist in this active workspace.")
        session.add(
            AuditEvent(
                workspace_id=workspace_id,
                action="job.manual_review_saved",
                entity_type="job_posting",
                entity_id=job.id,
                details=review.model_dump(),
                **audit_columns(actor, origin, correlation_id),
            )
        )
        session.flush()
        return JobDetailResponse.model_validate(
            _job_detail(database, session, job.id, workspace_id=workspace_id, include_archived=True)
        )


def set_closing_date(
    database: Database,
    workspace_id: str,
    job_posting_id: str,
    edit: ClosingDateEditInput,
    *,
    actor: str = "local_user",
    origin: str = "api",
    correlation_id: str | None = None,
) -> JobDetailResponse:
    """Save a personal tracking date without changing publisher evidence."""
    with database.exclusive_session() as session:
        workspace = session.get(Workspace, workspace_id)
        job = session.scalar(
            select(JobPosting)
            .where(JobPosting.id == job_posting_id, JobPosting.workspace_id == workspace_id)
            .with_for_update()
        )
        if workspace is None or workspace.archived_at is not None or job is None:
            raise NotFoundError(f"Job {job_posting_id!r} does not exist in this active workspace.")
        old_date = job.closing_date_override
        job.closing_date_override = edit.closing_date_override
        job.closing_date_override_updated_at = utc_now()
        session.add(
            AuditEvent(
                workspace_id=workspace_id,
                action="job.closing_date_changed",
                entity_type="job_posting",
                entity_id=job.id,
                details={
                    "old_closing_date_override": old_date.isoformat() if old_date else None,
                    "closing_date_override": (
                        edit.closing_date_override.isoformat()
                        if edit.closing_date_override
                        else None
                    ),
                },
                **audit_columns(actor, origin, correlation_id),
            )
        )
        session.flush()
        return JobDetailResponse.model_validate(
            _job_detail(database, session, job.id, workspace_id=workspace_id, include_archived=True)
        )


def track_pending_discovery(
    database: Database,
    artifacts: EvidenceArtifactService,
    workspace_id: str,
    pending_discovery_id: str,
    *,
    actor: str = "local_user",
    origin: str = "api",
    correlation_id: str | None = None,
) -> JobDetailResponse:
    """Track a saved discovery without requiring an evaluation or provider call."""
    with database.session() as session:
        if session.get_bind().dialect.name == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        workspace = session.get(Workspace, workspace_id)
        pending = session.get(PendingDiscovery, pending_discovery_id)
        if (
            workspace is None
            or workspace.archived_at is not None
            or pending is None
            or pending.workspace_id != workspace_id
        ):
            raise NotFoundError("The pending discovery does not exist in this active workspace.")
        if pending.archived_at is not None:
            return _tracked_replay(database, session, workspace_id, pending)
        audit = audit_columns(actor, origin, correlation_id)
        job = _tracked_target(session, pending, audit, actor, origin, correlation_id)
        _retain_current_observation(session, artifacts, workspace, pending, job, audit)
        head = (
            session.get(PendingDiscovery, pending.regional_variant_of)
            if pending.regional_variant_of
            else pending
        )
        retire_pending(session, head or pending)
        session.add(
            AuditEvent(
                workspace_id=workspace_id,
                action="pending_discovery.tracked",
                entity_type="pending_discovery",
                entity_id=pending.id,
                details={"job_posting_id": job.id},
                **audit,
            )
        )
        session.flush()
        return JobDetailResponse.model_validate(
            _job_detail(database, session, job.id, workspace_id=workspace_id)
        )


def _tracked_replay(
    database: Database, session: Session, workspace_id: str, pending: PendingDiscovery
) -> JobDetailResponse:
    previous = session.scalar(
        select(AuditEvent)
        .where(
            AuditEvent.workspace_id == workspace_id,
            AuditEvent.action == "pending_discovery.tracked",
            AuditEvent.entity_type == "pending_discovery",
            AuditEvent.entity_id == pending.id,
        )
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(1)
    )
    job_id = previous.details.get("job_posting_id") if previous is not None else None
    if not isinstance(job_id, str):
        raise NotFoundError("The pending discovery does not exist in this active workspace.")
    return JobDetailResponse.model_validate(
        _job_detail(database, session, job_id, workspace_id=workspace_id, include_archived=True)
    )


def _matching_tracked_job(session: Session, pending: PendingDiscovery) -> JobPosting | None:
    identities = [
        find_job_by_durable_identifier(
            session,
            workspace_id=pending.workspace_id,
            employer=pending.employer,
            requisition_id=pending.requisition_id if kind == "requisition" else "",
            canonical_url=url if kind == "canonical" else "",
            discovery_url=url if kind == "discovery" else "",
        )
        for kind, url in (
            ("requisition", ""),
            ("canonical", pending.canonical_url),
            ("discovery", pending.discovery_url),
        )
    ]
    matches = {job.id: job for job in identities if job is not None}
    if len(matches) > 1:
        raise ConflictError("This discovery identifies more than one tracked posting.")
    job = next(iter(matches.values()), None)
    if job is not None and job.archived_at is not None:
        raise ConflictError("This posting is archived. Restore it before tracking this lead.")
    return job


def _tracked_target(
    session: Session,
    pending: PendingDiscovery,
    audit: dict[str, Any],
    actor: str,
    origin: str,
    correlation_id: str | None,
) -> JobPosting:
    if not pending.employer.strip() or not pending.title.strip():
        raise ValidationError("An employer and title are required to track this posting.")
    job = _matching_tracked_job(session, pending)
    if job is None:
        employer = _employer(
            session,
            pending.workspace_id,
            pending.employer,
            actor=actor,
            origin=origin,
            correlation_id=correlation_id,
        )
        job = JobPosting(
            workspace_id=pending.workspace_id,
            employer_id=employer.id,
            title=pending.title.strip(),
            normalized_title=normalize_text(pending.title),
            first_seen_at=datetime.combine(pending.first_seen_on, time.min, UTC),
            **audit,
        )
        session.add(job)
        session.flush()
    _transfer_pending_closing_date(session, job, pending)
    _remember_pending_identity(session, job, pending, audit, actor, origin, correlation_id)
    return job


def _remember_pending_identity(
    session: Session,
    job: JobPosting,
    pending: PendingDiscovery,
    audit: dict[str, Any],
    actor: str,
    origin: str,
    correlation_id: str | None,
) -> None:
    if pending.requisition_id.strip():
        key = requisition_key(pending.employer, pending.requisition_id)
        exists = session.scalar(
            select(PostingIdentifier.id).where(
                PostingIdentifier.workspace_id == pending.workspace_id,
                PostingIdentifier.identifier_type == "requisition",
                PostingIdentifier.identifier_value == key,
            )
        )
        if exists is None:
            session.add(
                PostingIdentifier(
                    workspace_id=pending.workspace_id,
                    job_posting_id=job.id,
                    identifier_type="requisition",
                    identifier_value=key,
                    **audit,
                )
            )
    seen_urls: set[str] = set()
    for kind, url in (
        ("canonical", pending.canonical_url),
        ("discovery", pending.discovery_url),
    ):
        normalized = normalize_url(url)
        if not normalized or normalized in seen_urls:
            continue
        seen_urls.add(normalized)
        remember_posting_url(
            session,
            job,
            url=url,
            source_id=pending.source_id,
            url_type=kind,
            make_primary=kind == "canonical" or not normalize_url(pending.canonical_url),
            actor=actor,
            origin=origin,
            correlation_id=correlation_id,
        )


def _retain_current_observation(
    session: Session,
    artifacts: EvidenceArtifactService,
    workspace: Workspace,
    pending: PendingDiscovery,
    job: JobPosting,
    audit: dict[str, Any],
) -> None:
    # Content resolution imports job search for refresh; defer this shared guard.
    from clearscout.application.discovery.content_resolution import usable_current_evidence

    content = (
        session.get(PendingPostingContent, pending.current_content_id)
        if pending.current_content_id
        else None
    )
    if content is None or not usable_current_evidence(pending, content):
        return
    artifact = session.get(Artifact, content.artifact_id)
    if artifact is None or artifact.workspace_id != pending.workspace_id:
        raise ValidationError("Verified pending-posting content artifact is missing.")
    if hashlib.sha256(artifacts.read(artifact, workspace)).hexdigest() != content.content_sha256:
        raise ValidationError("Pending-posting content digest does not match its artifact.")
    session.add(
        PostingObservation(
            job_posting_id=job.id,
            observed_at=content.fetched_at,
            posting_status=content.posting_status,
            location=pending.location,
            body_artifact_id=content.artifact_id,
            extraction_method=content.extraction_method,
            fetched_at=content.fetched_at,
            http_status=content.http_status,
            robots_basis=content.robots_basis,
            content_sha256=content.content_sha256,
            publisher_closing_date=content.publisher_closing_date,
            **audit,
        )
    )


def append_application_event(
    database: Database,
    job_posting_id: str,
    event: ApplicationEventInput,
    *,
    workspace_id: str | None = None,
    idempotency_key: str,
    actor: str = "local_user",
    origin: str = "api",
    correlation_id: str | None = None,
) -> ApplicationEventResponse:
    """Append a status event without editing or deleting earlier history."""
    if not idempotency_key.strip():
        raise ValidationError("An idempotency key is required for application events.")
    with database.session() as session:
        job = session.get(JobPosting, job_posting_id)
        if (
            job is None
            or job.archived_at is not None
            or (workspace_id is not None and job.workspace_id != workspace_id)
        ):
            raise NotFoundError(f"Job {job_posting_id!r} does not exist.")
        application = session.scalar(
            select(Application).where(Application.job_posting_id == job_posting_id)
        )
        if application is None:
            application = Application(
                workspace_id=job.workspace_id,
                job_posting_id=job.id,
                **audit_columns(actor, origin, correlation_id),
            )
            session.add(application)
            session.flush()
        existing = session.scalar(
            select(ApplicationEvent).where(
                ApplicationEvent.application_id == application.id,
                ApplicationEvent.idempotency_key == idempotency_key.strip(),
            )
        )
        if existing is not None:
            # The same key with the same body is the caller retrying, which is
            # the point of the key. The same key with a different body is a bug
            # somewhere upstream and must not append a second event.
            if existing.payload_sha256 == event.digest():
                return ApplicationEventResponse.model_validate(_application_event(existing))
            raise ConflictError("This idempotency key already names a different event.")
        if (
            event.event_type == ApplicationEventType.STATUS
            and event.status == ApplicationStatus.PACKAGE_READY
            and session.scalar(
                select(ApplicationEvent.id)
                .where(
                    ApplicationEvent.application_id == application.id,
                    ApplicationEvent.status.in_(PROGRESSED_APPLICATION_STATUSES),
                )
                .limit(1)
            )
            is not None
        ):
            raise ConflictError(
                "Package ready cannot be recorded after this application has progressed. "
                "Use a correction or reversal to amend its history."
            )
        target_id = event.corrects_event_id or event.reverses_event_id
        if target_id is not None:
            target = session.get(ApplicationEvent, target_id)
            if target is None or target.application_id != application.id:
                raise ValidationError(
                    "A correction or reversal may reference only this application's history."
                )
        written = ApplicationEvent(
            application_id=application.id,
            status=event.status.value,
            event_type=event.event_type,
            occurred_at=event.occurred_at,
            corrects_event_id=event.corrects_event_id,
            reverses_event_id=event.reverses_event_id,
            artifact_uri=event.artifact_uri,
            notes=event.notes,
            idempotency_key=idempotency_key.strip(),
            payload_sha256=event.digest(),
            **audit_columns(actor, origin, correlation_id),
        )
        session.add(written)
        session.flush()
        session.add(
            AuditEvent(
                workspace_id=job.workspace_id,
                action="application.event_appended",
                entity_type="application_event",
                entity_id=written.id,
                details={
                    "application_id": application.id,
                    "status": written.status,
                    "event_type": written.event_type,
                },
                **audit_columns(actor, origin, correlation_id),
            )
        )
        return ApplicationEventResponse.model_validate(_application_event(written))


def _set_job_archived(
    database: Database,
    job_posting_id: str,
    workspace_id: str,
    *,
    archived: bool,
    correlation_id: str | None,
) -> JobArchiveResponse:
    """Hide or restore one job, reporting whether this call was the one that moved it."""
    with database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        job = session.get(JobPosting, job_posting_id)
        if (
            workspace is None
            or workspace.archived_at is not None
            or job is None
            or job.workspace_id != workspace_id
        ):
            raise NotFoundError(f"Job {job_posting_id!r} does not exist in this workspace.")
        changed = _set_job_archived_in_session(
            session,
            job,
            workspace_id,
            archived=archived,
            correlation_id=correlation_id,
        )
        return JobArchiveResponse(
            job_posting_id=job.id,
            workspace_id=workspace_id,
            archived=archived,
            archived_at=job.archived_at,
            changed=changed,
        )


def _set_job_archived_in_session(
    session: Session,
    job: JobPosting,
    workspace_id: str,
    *,
    archived: bool,
    correlation_id: str | None,
) -> bool:
    """Apply the one archive-state and audit policy inside the caller's transaction."""
    changed = (job.archived_at is None) == archived
    if not changed:
        return False
    job.archived_at = utc_now() if archived else None
    session.add(
        AuditEvent(
            workspace_id=workspace_id,
            action="job.archived" if archived else "job.unarchived",
            entity_type="job_posting",
            entity_id=job.id,
            details={"children_preserved": list(PRESERVED_CHILDREN)},
            correlation_id=correlation_id,
        )
    )
    return True


def archive_job(
    database: Database,
    job_posting_id: str,
    workspace_id: str,
    *,
    correlation_id: str | None = None,
) -> JobArchiveResponse:
    """Hide a job and its active projections without deleting child history."""
    return _set_job_archived(
        database, job_posting_id, workspace_id, archived=True, correlation_id=correlation_id
    )


def archive_jobs(
    database: Database,
    job_posting_ids: list[str],
    workspace_id: str,
    *,
    correlation_id: str | None = None,
) -> JobArchiveBatchResponse:
    """Atomically archive the exact tracked jobs named by the caller."""
    if (
        not job_posting_ids
        or len(job_posting_ids) > MAX_JOB_ARCHIVE_BATCH
        or len(job_posting_ids) != len(set(job_posting_ids))
    ):
        raise ValidationError(f"Bulk archive requires 1 to {MAX_JOB_ARCHIVE_BATCH} unique job ids.")
    with database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        if workspace is None or workspace.archived_at is not None:
            raise NotFoundError(f"Workspace {workspace_id!r} does not exist.")
        jobs = session.scalars(
            select(JobPosting).where(
                JobPosting.workspace_id == workspace_id,
                JobPosting.id.in_(job_posting_ids),
            )
        ).all()
        by_id = {job.id: job for job in jobs}
        missing = [job_id for job_id in job_posting_ids if job_id not in by_id]
        if missing:
            raise NotFoundError(
                "Bulk archive refused unknown jobs in this workspace: " + ", ".join(missing)
            )
        changed = 0
        for job_id in job_posting_ids:
            changed += _set_job_archived_in_session(
                session,
                by_id[job_id],
                workspace_id,
                archived=True,
                correlation_id=correlation_id,
            )
        return JobArchiveBatchResponse(
            job_posting_ids=job_posting_ids,
            requested_count=len(job_posting_ids),
            archived_count=changed,
            already_archived_count=len(job_posting_ids) - changed,
        )


def unarchive_job(
    database: Database,
    job_posting_id: str,
    workspace_id: str,
    *,
    correlation_id: str | None = None,
) -> JobArchiveResponse:
    """Restore a job and the preserved child records beneath it."""
    return _set_job_archived(
        database, job_posting_id, workspace_id, archived=False, correlation_id=correlation_id
    )


def set_posting_hint(
    database: Database,
    job_posting_id: str,
    workspace_id: str,
    hint: str | None,
    *,
    correlation_id: str | None = None,
) -> PostingHintResponse:
    """Replace one posting's hint, or clear it. The hint is not evidence."""
    normalized = hint.strip() if hint is not None else None
    if normalized == "":
        normalized = None
    with database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        job = session.get(JobPosting, job_posting_id)
        if (
            workspace is None
            or workspace.archived_at is not None
            or job is None
            or job.workspace_id != workspace_id
        ):
            raise NotFoundError(f"Job {job_posting_id!r} does not exist in this workspace.")
        changed = job.posting_hint != normalized
        if changed:
            job.posting_hint = normalized
            session.add(
                AuditEvent(
                    workspace_id=workspace_id,
                    action="job.hint_set" if normalized is not None else "job.hint_cleared",
                    entity_type="job_posting",
                    entity_id=job.id,
                    details={"length": len(normalized) if normalized is not None else 0},
                    correlation_id=correlation_id,
                )
            )
        return PostingHintResponse(
            job_posting_id=job.id,
            workspace_id=workspace_id,
            hint=job.posting_hint,
            changed=changed,
        )
