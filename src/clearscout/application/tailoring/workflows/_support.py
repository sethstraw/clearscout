"""Verified reads and lineage writes shared by more than one workflow use case."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from clearscout.application.discovery.board_slugs import (
    backfill_employer_from_url,
    publisher_spelling_of_tenant,
)
from clearscout.application.failures import ValidationError
from clearscout.application.tailoring.stages.jobspec import StoredPostingName
from clearscout.infrastructure.artifacts.store import LocalArtifactStore, file_uri_path
from clearscout.infrastructure.persistence.models import (
    Artifact,
    MasterCvVersion,
    PendingPostingContent,
    RunPromptVersion,
    Workspace,
)

if TYPE_CHECKING:
    from clearscout.application.tailoring.workflows.service import WorkflowService


def _readable_roots(workspace: Workspace) -> tuple[Path, ...]:
    """Widen the store to the workspace root only when that root is really there."""
    if not workspace.root_path:
        return ()
    candidate = Path(workspace.root_path).expanduser().resolve()
    return (candidate,) if candidate.is_dir() else ()


def _read_artifact(service: WorkflowService, artifact: Artifact, workspace: Workspace) -> bytes:
    """Return stored artifact bytes that still match the digest recorded for them."""
    path = file_uri_path(artifact.storage_uri)
    if path is None:
        raise ValidationError("Workflow source artifact is not a supported local file.")
    store = LocalArtifactStore(service.root, readable_roots=_readable_roots(workspace))
    content, descriptor = store.read(path)
    if descriptor.sha256 != artifact.sha256:
        raise ValidationError("Workflow source artifact failed its integrity check.")
    return content


def _read_master(service: WorkflowService, version: MasterCvVersion, workspace: Workspace) -> bytes:
    """Return master CV bytes that still match the digest recorded for them."""
    path = file_uri_path(version.source_uri)
    if path is None:
        raise ValidationError("Master CV version is not a supported local file.")
    store = LocalArtifactStore(service.root, readable_roots=_readable_roots(workspace))
    content, descriptor = store.read(path)
    if descriptor.sha256 != version.content_sha256:
        raise ValidationError("Master CV version failed its integrity check.")
    return content


def _prompt_lineage(
    session: Session,
    prompt_version_ids: tuple[str, ...],
    *,
    tailoring_run_id: str | None = None,
    batch_run_id: str | None = None,
) -> None:
    """File which edited prompts a stored workflow was produced under.

    A workflow under the packaged suite writes nothing, which is the correct
    answer: there is no version row to point at, and the suite fingerprint
    already in the manifest says which packaged suite it was.
    """
    for version_id in prompt_version_ids:
        session.add(
            RunPromptVersion(
                prompt_version_id=version_id,
                tailoring_run_id=tailoring_run_id,
                batch_run_id=batch_run_id,
                origin="workflow.prompts",
            )
        )


def _posting_name(
    session: Session,
    *,
    workspace_id: str,
    title: str,
    employer: str,
    body: bytes,
    vouched: bool,
    pending_discovery_id: str | None = None,
    posting_urls: tuple[str, ...] = (),
) -> StoredPostingName:
    """Return the stored title and the employer name a run should carry.

    A name that is only a posting URL's title-cased tenant takes the body's own
    spelling of it, or is flagged as address-only. A stored employer a publisher
    vouched for then stands; otherwise the employer the publisher stated with this
    exact body, read for this pending posting or at one of this job's URLs,
    replaces it when one was recorded.
    """
    named = _address_named(title, employer, body, posting_urls)
    if vouched or (pending_discovery_id is None and not posting_urls):
        return named
    if pending_discovery_id is not None:
        scope = PendingPostingContent.pending_discovery_id == pending_discovery_id
    else:
        digests = {hashlib.sha256(url.encode("utf-8")).hexdigest() for url in posting_urls}
        scope = or_(
            PendingPostingContent.requested_url_sha256.in_(digests),
            PendingPostingContent.final_url_sha256.in_(digests),
        )
    recorded = session.scalars(
        select(PendingPostingContent.identity_evidence)
        .where(
            PendingPostingContent.workspace_id == workspace_id,
            PendingPostingContent.content_sha256 == hashlib.sha256(body).hexdigest(),
            scope,
        )
        .order_by(PendingPostingContent.fetched_at.desc(), PendingPostingContent.id.desc())
    )
    for evidence in recorded:
        stated = evidence.get("publisher_employer") if isinstance(evidence, dict) else None
        if isinstance(stated, str) and stated.strip():
            return StoredPostingName(title=title, employer=stated.strip())
    return named


def _address_named(
    title: str, employer: str, body: bytes, posting_urls: tuple[str, ...]
) -> StoredPostingName:
    """Return the name to carry when the employer may be only a posting URL's tenant.

    The body's own spelling of that tenant replaces it; with none, it is flagged.
    """
    tenants = [
        url for url in posting_urls if employer and backfill_employer_from_url(url) == employer
    ]
    if not tenants:
        return StoredPostingName(title=title, employer=employer)
    text = body.decode("utf-8", errors="replace")
    spelled = next(
        (found for url in tenants if (found := publisher_spelling_of_tenant(url, text))), None
    )
    if spelled is None:
        return StoredPostingName(title=title, employer=employer, employer_from_address_only=True)
    return StoredPostingName(title=title, employer=spelled)
