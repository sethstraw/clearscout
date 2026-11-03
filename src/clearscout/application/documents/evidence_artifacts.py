"""Durable evidence artifacts with verified local reads."""

from __future__ import annotations

import mimetypes
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from clearscout.application.failures import ValidationError
from clearscout.domain import ArtifactIntegrityStatus, ArtifactLogicalType, utc_now
from clearscout.domain.artifacts import PostingSource
from clearscout.infrastructure.artifacts.runtime import RuntimeStorage
from clearscout.infrastructure.artifacts.store import LocalArtifactStore, file_uri_path
from clearscout.infrastructure.persistence.database import Database, register_session_finalizer
from clearscout.infrastructure.persistence.models import Artifact, Workspace


class EvidenceArtifactService:
    """Store bytes outside the database and verify them before every evidence read."""

    def __init__(self, storage: RuntimeStorage) -> None:
        """Configure the private writable root used for new evidence."""
        self.storage = storage
        self.root = storage.artifact_root

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
    ) -> Artifact:
        """Write immutable bytes and index their descriptor in the current transaction."""
        staged = LocalArtifactStore(self.root).stage(relative_path, content)
        descriptor = staged.descriptor
        artifact = Artifact(
            workspace_id=workspace_id,
            logical_type=logical_type,
            mime_type=mime_type or descriptor.mime_type,
            storage_uri=descriptor.storage_uri,
            byte_size=descriptor.byte_size,
            sha256=descriptor.sha256,
            provenance=provenance,
            related_entity_type=related_entity_type,
            related_entity_id=related_entity_id,
            last_verified_at=utc_now() if not staged.pending else None,
            integrity_status="verified" if not staged.pending else "staged",
            **audit,
        )
        session.add(artifact)
        session.flush()
        if staged.pending:
            # An exclusive session binds a Connection; its finalizer runs after that commit.
            bind = session.get_bind()
            engine = bind if isinstance(bind, Engine) else bind.engine
            artifact_id = artifact.id

            def finalize() -> None:
                staged.finalize()
                _settle_staged(engine, artifact_id, "verified")

            register_session_finalizer(
                session,
                after_commit=finalize,
                after_rollback=staged.discard,
            )
        return artifact

    def store_posting_source(
        self,
        session: Session,
        *,
        workspace_id: str,
        directory: str,
        source: PostingSource,
        related_entity_type: str,
        related_entity_id: str,
        provenance: dict[str, Any],
        audit: dict[str, Any],
    ) -> dict[ArtifactLogicalType, str]:
        """Store what a posting body was converted from, in the directory beside its body."""
        described = {
            **provenance,
            "publisher": source.publisher,
            "description_paths": list(source.description_paths),
        }
        kept: tuple[tuple[ArtifactLogicalType, str, str, str], ...] = (
            ("posting.source.json", source.record, "raw.json", "application/json"),
            ("posting.body.html", source.description_markup, "description.html", "text/html"),
            ("posting.source.html", source.page_html, "page.html", "text/html"),
            ("posting.source.xml", source.item_xml, "item.xml", "application/xml"),
        )
        artifact_ids: dict[ArtifactLogicalType, str] = {}
        for logical_type, content, name, mime_type in kept:
            if content:
                artifact = self.store(
                    session,
                    workspace_id=workspace_id,
                    logical_type=logical_type,
                    content=content.encode("utf-8"),
                    relative_path=f"{directory}/{name}",
                    mime_type=mime_type,
                    related_entity_type=related_entity_type,
                    related_entity_id=related_entity_id,
                    provenance=(
                        {**described, "reserialized": True}
                        if logical_type == "posting.source.xml"
                        else described
                    ),
                    audit=audit,
                )
                artifact_ids[logical_type] = artifact.id
        return artifact_ids

    def read(self, artifact: Artifact, workspace: Workspace) -> bytes:
        """Return bytes only when URI boundaries, size, and digest all verify."""
        path = file_uri_path(artifact.storage_uri)
        if path is None:
            raise ValidationError(f"Artifact {artifact.id!r} is not a supported local file.")
        readable_roots = self.storage.readable_roots(workspace.root_path)
        try:
            content, descriptor = LocalArtifactStore(self.root, readable_roots=readable_roots).read(
                path
            )
        except (FileNotFoundError, OSError) as exc:
            raise ValidationError(f"Artifact {artifact.id!r} is unavailable.") from exc
        if descriptor.sha256 != artifact.sha256 or descriptor.byte_size != artifact.byte_size:
            raise ValidationError(f"Artifact {artifact.id!r} failed its integrity check.")
        return content

    def reconcile(self, database: Database) -> dict[str, int]:
        """Finalize indexed staged artifacts and count how each row was left.

        An I/O failure leaves the row staged and counted as retrying for the next
        start, and a row outside this root stays staged for a start whose root holds it.
        """
        finalized = 0
        unresolved = 0
        outside_root = 0
        retrying = 0
        with database.session() as session:
            rows = session.execute(
                select(
                    Artifact.id, Artifact.storage_uri, Artifact.sha256, Artifact.byte_size
                ).where(Artifact.integrity_status == "staged")
            ).all()
        store = LocalArtifactStore(self.root)
        for artifact_id, storage_uri, sha256, byte_size in rows:
            path = file_uri_path(storage_uri)
            if path is None:
                unresolved += _settle_staged(database.engine, artifact_id, "unresolved")
                continue
            if not path.is_relative_to(store.root):
                outside_root += 1
                continue
            try:
                store.reconcile(path, sha256, byte_size)
            except ValidationError:
                unresolved += _settle_staged(database.engine, artifact_id, "unresolved")
                continue
            except OSError:
                retrying += 1
                continue
            finalized += _settle_staged(database.engine, artifact_id, "verified")
        return {
            "finalized": finalized,
            "unresolved": unresolved,
            "outside_root": outside_root,
            "retrying": retrying,
        }


def _settle_staged(bind: Engine, artifact_id: str, status: ArtifactIntegrityStatus) -> int:
    """Record one outcome for a row that is still staged, and count the rows changed."""
    values: dict[str, Any] = {"integrity_status": status}
    if status == "verified":
        values["last_verified_at"] = utc_now()
    with bind.begin() as connection:
        result = connection.execute(
            update(Artifact)
            .where(Artifact.id == artifact_id, Artifact.integrity_status == "staged")
            .values(**values)
        )
    return result.rowcount


def artifact_suffix(logical_type: ArtifactLogicalType, mime_type: str = "") -> str:
    """Choose a stable, non-executable extension for artifact storage."""
    fixed = {
        "posting.body.text": ".txt",
        "posting.listing.text": ".txt",
        "posting.body.html": ".html",
        "posting.source.json": ".json",
        "posting.source.html": ".html",
        "posting.source.xml": ".xml",
        "posting.source.pdf": ".pdf",
        "evaluation.fit_report.markdown": ".md",
    }
    if logical_type in fixed:
        return fixed[logical_type]
    guessed = mimetypes.guess_extension(mime_type, strict=False) if mime_type else None
    return guessed if guessed and guessed.isascii() else ".bin"
