"""Load the exact posting and CV bytes one workflow run is allowed to read."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import or_, select

from tailorcv.application.config import Settings
from tailorcv.application.failures import NotFoundError, ValidationError
from tailorcv.application.tailoring.workflows._support import _read_artifact, _read_master
from tailorcv.application.workspaces.prompt_library import overrides_in
from tailorcv.domain.staged_facts import StagedListingFacts
from tailorcv.infrastructure.persistence.models import (
    Artifact,
    CandidateProfile,
    Employer,
    JobPosting,
    MasterCvVersion,
    PendingDiscovery,
    PostingObservation,
    PostingUrl,
    Workspace,
)

if TYPE_CHECKING:
    from tailorcv.application.tailoring.workflows.service import WorkflowService


@dataclass(frozen=True)
class _WorkflowInputs:
    workspace: Workspace
    job: JobPosting
    master_version: MasterCvVersion
    observation_id: str
    posting_content: bytes
    posting_url: str
    careers_url: str
    employer_name: str
    master_content: bytes
    settings: Settings
    search_parameter_set_id: str | None
    prompt_overrides: dict[str, tuple[str, str]]
    # Every URL recorded for the job, and whether a publisher vouched for a pending
    # posting staged at one of them, which is the trust its evaluation named it with.
    posting_urls: tuple[str, ...]
    identity_vouched: bool


def _inputs(service: WorkflowService, workspace_id: str, job_posting_id: str) -> _WorkflowInputs:
    """Load exact posting/CV bytes after workspace and readable-root checks."""
    with service.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        job = session.get(JobPosting, job_posting_id)
        if (
            workspace is None
            or workspace.archived_at is not None
            or job is None
            or job.workspace_id != workspace_id
        ):
            raise NotFoundError("The workflow job does not exist in this workspace.")
        observation = session.scalar(
            select(PostingObservation)
            .where(
                PostingObservation.job_posting_id == job.id,
                PostingObservation.body_artifact_id.is_not(None),
            )
            .order_by(PostingObservation.observed_at.desc(), PostingObservation.id.desc())
        )
        if observation is None or observation.body_artifact_id is None:
            raise ValidationError("The job has no stored posting body to process.")
        posting = session.get(Artifact, observation.body_artifact_id)
        if posting is None or posting.workspace_id != workspace_id:
            raise ValidationError("The job's posting artifact is unavailable.")
        master_version = session.scalar(
            select(MasterCvVersion)
            .join(
                CandidateProfile,
                CandidateProfile.id == MasterCvVersion.candidate_profile_id,
            )
            .where(CandidateProfile.workspace_id == workspace_id)
            .order_by(MasterCvVersion.effective_at.desc(), MasterCvVersion.id.desc())
        )
        if master_version is None:
            raise ValidationError("The workspace has no master CV version.")
        posting_content = _read_artifact(service, posting, workspace)
        master_content = _read_master(service, master_version, workspace)
        posting_url = session.scalar(
            select(PostingUrl.url)
            .where(
                PostingUrl.job_posting_id == job.id,
                PostingUrl.archived_at.is_(None),
            )
            .order_by(PostingUrl.is_primary.desc(), PostingUrl.created_at, PostingUrl.id)
            .limit(1)
        )
        employer = session.get(Employer, job.employer_id)
        if employer is None:
            raise ValidationError("The job's employer record is unavailable.")
        posting_urls = tuple(
            dict.fromkeys(
                url
                for row in session.execute(
                    select(PostingUrl.url, PostingUrl.normalized_url).where(
                        PostingUrl.job_posting_id == job.id
                    )
                )
                for url in row
            )
        )
        identity_vouched = any(
            StagedListingFacts.from_json(facts).publisher_vouched
            for facts in session.scalars(
                select(PendingDiscovery.staged_facts).where(
                    PendingDiscovery.workspace_id == workspace_id,
                    or_(
                        PendingDiscovery.canonical_url.in_(posting_urls),
                        PendingDiscovery.discovery_url.in_(posting_urls),
                    ),
                )
            )
        )
        settings_snapshot = service.settings.snapshot(workspace_id)
        session.expunge(workspace)
        session.expunge(job)
        session.expunge(master_version)
        return _WorkflowInputs(
            workspace=workspace,
            job=job,
            master_version=master_version,
            observation_id=observation.id,
            posting_content=posting_content,
            posting_url=str(posting_url or ""),
            careers_url=employer.careers_url or "",
            employer_name=employer.name,
            master_content=master_content,
            settings=settings_snapshot.settings,
            search_parameter_set_id=settings_snapshot.search_parameter_set_id,
            prompt_overrides=overrides_in(session, workspace_id),
            posting_urls=posting_urls,
            identity_vouched=identity_vouched,
        )
