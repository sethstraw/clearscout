"""Register imported master CVs and append validated edited versions."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from clearscout.application.config import Settings
from clearscout.application.failures import ConflictError, NotFoundError, ValidationError
from clearscout.application.master_cv.cv import CvInventory, Entry, parse_cv
from clearscout.application.master_cv.intake import (
    CANONICAL_FILENAME,
    SUPPORTED_SUFFIXES,
    MasterCvIntake,
    canonical_problem,
    parse_canonical_source,
    publish_canonical_master_cv,
)
from clearscout.application.master_cv.queries import MasterCvQueries
from clearscout.application.workspaces import MasterCvEditResponse, MasterCvImportResponse
from clearscout.application.workspaces.spend import record_calls
from clearscout.domain import ArtifactLogicalType, new_id, utc_now
from clearscout.domain.statements import Known, Stated, carried_identities
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    Artifact,
    AuditEvent,
    CandidateProfile,
    MasterCvEntry,
    MasterCvVersion,
    Workspace,
)
from clearscout.infrastructure.providers.credentials import Prompter
from clearscout.shared.errors import CvParseError


class _ArtifactStore(Protocol):
    """Artifact operations required by immutable master-CV registration."""

    def store(
        self,
        session: Session,
        *,
        workspace_id: str,
        logical_type: ArtifactLogicalType,
        content: bytes,
        relative_path: str,
        mime_type: str,
        related_entity_type: str | None,
        related_entity_id: str | None,
        provenance: dict[str, Any],
        audit: dict[str, Any],
    ) -> Artifact: ...

    def read(self, artifact: Artifact, workspace: Workspace) -> bytes: ...


def _audit(actor: str, origin: str, correlation_id: str | None) -> dict[str, Any]:
    """Return consistent provenance for master-CV writes."""
    return {"created_by": actor, "origin": origin, "correlation_id": correlation_id}


def _stated_entries(entries: Sequence[Entry]) -> list[Stated]:
    """Return each parsed entry as the statement the identity rule reads."""
    return [Stated(id=entry.id, scope=entry.scope, text=entry.text) for entry in entries]


def _known_entry(row: MasterCvEntry) -> Known:
    """Project a stored entry row as the statement the identity rule reads."""
    return Known(
        id=row.entry_id, key=row.entry_key, scope=row.role_key or row.section, text=row.text
    )


def _stored_entries(session: Session, profile_id: str) -> list[Known]:
    """Return the entries of the newest version, whose identities a new one inherits."""
    latest = session.scalar(
        select(MasterCvVersion)
        .where(MasterCvVersion.candidate_profile_id == profile_id)
        .order_by(MasterCvVersion.effective_at.desc(), MasterCvVersion.id.desc())
        .limit(1)
    )
    if latest is None:
        return []
    return [
        _known_entry(row)
        for row in session.scalars(
            select(MasterCvEntry).where(MasterCvEntry.master_cv_version_id == latest.id)
        )
    ]


def _write_entries(
    session: Session,
    version: MasterCvVersion,
    inventory: CvInventory,
    *,
    previous: Sequence[Known],
    audit: dict[str, Any],
) -> None:
    """Store this version's entries, unless it already has them."""
    if session.scalar(
        select(MasterCvEntry.id).where(MasterCvEntry.master_cv_version_id == version.id)
    ):
        return
    carried = carried_identities(previous, _stated_entries(inventory.entries))
    session.add_all(
        MasterCvEntry(
            master_cv_version_id=version.id,
            entry_id=entry.id,
            entry_key=carried.get(entry.id, "") or new_id(),
            kind=entry.kind.value,
            section=entry.section,
            role_key=entry.role_key,
            group=entry.group,
            label=entry.label,
            text=entry.text,
            details=list(entry.details),
            position=entry.order,
            **audit,
        )
        for entry in inventory.entries
    )
    session.flush()


def register_master_cv(
    session: Session,
    artifacts: _ArtifactStore,
    *,
    workspace: Workspace,
    content: bytes,
    inventory: CvInventory | None,
    original_source_uri: str,
    actor: str,
    origin: str,
    correlation_id: str | None,
    effective_at: datetime | None = None,
) -> tuple[MasterCvVersion, Artifact]:
    """Persist or reuse one checked immutable master-CV version."""
    audit = _audit(actor, origin, correlation_id)
    profile = session.scalar(
        select(CandidateProfile).where(
            CandidateProfile.workspace_id == workspace.id,
            CandidateProfile.archived_at.is_(None),
        )
    )
    if profile is None:
        profile = CandidateProfile(
            workspace_id=workspace.id,
            display_name=inventory.name if inventory is not None else "Local candidate",
            **audit,
        )
        session.add(profile)
        session.flush()
    digest = hashlib.sha256(content).hexdigest()
    existing = session.scalar(
        select(MasterCvVersion).where(
            MasterCvVersion.candidate_profile_id == profile.id,
            MasterCvVersion.content_sha256 == digest,
        )
    )
    if existing is not None:
        artifact = session.scalar(
            select(Artifact).where(
                Artifact.workspace_id == workspace.id,
                Artifact.storage_uri == existing.source_uri,
                Artifact.sha256 == digest,
                Artifact.archived_at.is_(None),
            )
        )
        if artifact is None or artifacts.read(artifact, workspace) != content:
            raise ValidationError("The stored master-CV version is unavailable or changed.")
        if inventory is not None:
            _write_entries(session, existing, inventory, previous=[], audit=audit)
        return existing, artifact
    previous = _stored_entries(session, profile.id)
    version_id = new_id()
    canonical = inventory is not None
    artifact = artifacts.store(
        session,
        workspace_id=workspace.id,
        logical_type="master_cv.snapshot.markdown" if canonical else "master_cv.snapshot.text",
        content=content,
        relative_path=(
            f"{workspace.id}/master-cv/{version_id}/CV_Master.md"
            if canonical
            else f"{workspace.id}/master-cv/{version_id}/source.txt"
        ),
        mime_type="text/markdown" if canonical else "text/plain",
        related_entity_type="master_cv_version",
        related_entity_id=version_id,
        provenance={"source": origin, "original_source_uri": original_source_uri},
        audit=audit,
    )
    version = MasterCvVersion(
        id=version_id,
        candidate_profile_id=profile.id,
        content_sha256=digest,
        source_uri=artifact.storage_uri,
        effective_at=effective_at or utc_now(),
        byte_size=len(content),
        **audit,
    )
    session.add(version)
    session.flush()
    if inventory is not None:
        _write_entries(session, version, inventory, previous=previous, audit=audit)
    session.add(
        AuditEvent(
            workspace_id=workspace.id,
            action="master_cv.version_created",
            entity_type="master_cv_version",
            entity_id=version.id,
            details={"sha256": digest, "artifact_id": artifact.id},
            **audit,
        )
    )
    return version, artifact


class MasterCvVersionCommands:
    """Own master-CV writes and the transaction that registers each immutable version."""

    def __init__(
        self, database: Database, intake: MasterCvIntake, queries: MasterCvQueries
    ) -> None:
        """Configure commands with prepared intake and current-version queries."""
        self.database = database
        self.intake = intake
        self.queries = queries

    def import_source(
        self,
        *,
        workspace_id: str,
        filename: str,
        content: bytes,
        mime_type: str = "application/octet-stream",
        settings: Settings | None = None,
        api_key: str | None = None,
        prompter: Prompter | None = None,
        replace: bool = False,
        confirm_reread: bool = True,
        actor: str = "local_user",
        origin: str = "api",
        correlation_id: str | None = None,
    ) -> MasterCvImportResponse:
        """Prepare a source, register its immutable version, then publish its convenience copy.

        Without ``confirm_reread``, a source the model would have to re-read is refused.
        """
        del mime_type
        if (
            not confirm_reread
            and content
            and Path(filename).suffix.lower() in SUPPORTED_SUFFIXES
            and parse_canonical_source(Path(filename), content) is None
        ):
            raise ConflictError(
                "This CV needs a paid re-read by the model before it can be imported. "
                "Confirm the re-read and retry."
            )
        prepared = self.intake.prepare(
            workspace_id=workspace_id,
            filename=filename,
            content=content,
            settings=settings,
            api_key=api_key,
            prompter=prompter,
            replace=replace,
        )
        with self.database.session() as session:
            workspace = session.get(Workspace, workspace_id)
            if workspace is None or workspace.archived_at is not None:
                raise NotFoundError(f"Workspace {workspace_id!r} does not exist.")
            version, artifact = register_master_cv(
                session,
                self.queries.artifacts.artifacts,
                workspace=workspace,
                content=prepared.markdown.encode("utf-8"),
                inventory=prepared.inventory,
                original_source_uri=prepared.source_path.as_uri(),
                actor=actor,
                origin=origin,
                correlation_id=correlation_id,
            )
            record_calls(
                session,
                workspace_id=workspace.id,
                task_id=None,
                ledger=prepared.ledger,
                provider="configured",
                origin="master_cv.import",
            )
            result = MasterCvImportResponse.model_validate(
                {
                    "workspace_id": workspace.id,
                    "master_cv_version_id": version.id,
                    "artifact_id": artifact.id,
                    "canonical_path": str(prepared.canonical_path),
                    "source_path": str(prepared.source_path),
                    "report_path": str(prepared.report_path) if prepared.report_path else None,
                    "method": prepared.method,
                    "content_sha256": version.content_sha256,
                    "entry_count": len(prepared.inventory.entries),
                }
            )
        publish_canonical_master_cv(
            prepared.canonical_path, prepared.markdown, prepared.existing_content
        )
        return result

    def save_edit(
        self,
        workspace_id: str,
        content: str,
        *,
        confirm_reread: bool = False,
        actor: str = "local_user",
        origin: str = "api",
        correlation_id: str | None = None,
    ) -> MasterCvEditResponse:
        """Validate edited Markdown and append it through the import command.

        Text the canonical parser rejects reaches the model only with ``confirm_reread``.
        """
        try:
            inventory = parse_cv(Path(CANONICAL_FILENAME), text=content)
        except CvParseError as error:
            raise ValidationError(f"The edited master CV no longer parses: {error}") from error
        problem = canonical_problem(inventory)
        if problem and not confirm_reread:
            raise ConflictError(
                f"The edited master CV needs a paid re-read, because {problem}. "
                "Confirm the re-read and retry."
            )
        existing = self.queries.versions(workspace_id)
        known = {item.id for item in existing}
        previous = existing[0].id if existing else None
        result = self.import_source(
            workspace_id=workspace_id,
            filename=CANONICAL_FILENAME,
            content=content.encode("utf-8"),
            replace=True,
            actor=actor,
            origin=origin,
            correlation_id=correlation_id,
        )
        return MasterCvEditResponse(
            **result.model_dump(),
            previous_master_cv_version_id=previous,
            created=result.master_cv_version_id not in known,
        )
