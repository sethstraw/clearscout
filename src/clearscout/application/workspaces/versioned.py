"""Append-only versioned workspace document, shared by every policy service.

One row per version, keyed by (workspace_id, version). A write appends and
records who wrote it; an unchanged write skips the append and reports the
previous version. Optimistic concurrency refuses a write whose stated
expected_version has already been superseded.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Generic, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from clearscout.application.failures import ConflictError, NotFoundError
from clearscout.domain import utc_now
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import Workspace

RowT = TypeVar("RowT")
DocT = TypeVar("DocT")
SavedT = TypeVar("SavedT")
StateT = TypeVar("StateT")


class VersionedDocumentService(Generic[RowT, DocT, SavedT, StateT]):
    """Append-only history for one workspace document.

    Subclasses declare ``row_type`` and ``entity_label`` and override the four
    seams (``_document_of``, ``_parse``, ``_render_saved``, ``_render_state``)
    to bind a concrete document, row class, and response projection.
    """

    row_type: type[RowT]
    entity_label: str

    def __init__(self, database: Database) -> None:
        """Bind the version chain to one durable database."""
        self.database = database

    def current(self, workspace_id: str) -> DocT:
        """Return the document in force, or the zero value when none was saved."""
        with self.database.session() as session:
            return self.current_in_session(session, workspace_id)

    def current_in_session(self, session: Session, workspace_id: str) -> DocT:
        """Return the current document inside the caller's transaction."""
        self._workspace(session, workspace_id)
        return self._parse(self._latest(session, workspace_id))

    def describe(self, workspace_id: str) -> StateT:
        """Return the document plus the version metadata a caller needs."""
        with self.database.session() as session:
            self._workspace(session, workspace_id)
            record = self._latest(session, workspace_id)
            return self._render_state(workspace_id, record)

    def save(
        self,
        workspace_id: str,
        document: DocT,
        *,
        actor: str = "local_user",
        origin: str = "clearscout",
        correlation_id: str | None = None,
        expected_version: int | None = None,
    ) -> SavedT:
        """Append a new version, refusing a stale ``expected_version``.

        When ``expected_version`` is set and does not match the current
        version a ConflictError is raised; when omitted, the write proceeds
        against whichever version is current.
        """
        with self.database.session() as session:
            return self.save_in_session(
                session,
                workspace_id,
                document,
                actor=actor,
                origin=origin,
                correlation_id=correlation_id,
                expected_version=expected_version,
            )

    def save_in_session(
        self,
        session: Session,
        workspace_id: str,
        document: DocT,
        *,
        actor: str = "local_user",
        origin: str = "clearscout",
        correlation_id: str | None = None,
        expected_version: int | None = None,
    ) -> SavedT:
        """Append a version inside the caller's transaction."""
        self._workspace(session, workspace_id)
        previous = self._latest(session, workspace_id)
        previous_row: Any = previous
        current_version: int = previous_row.version if previous_row else 0
        if expected_version is not None and expected_version != current_version:
            raise ConflictError(
                f"These {self.entity_label} are at version {current_version}, not "
                f"{expected_version}. Reload before saving so the other edit is not lost."
            )
        content_sha256 = self._digest(document)
        # A stored hash may cover fewer fields than the model, so compare the re-read document.
        if previous is not None and self._digest(self._parse(previous)) == content_sha256:
            return self._render_saved(previous, unchanged=True)
        record = self._build_row(
            workspace_id=workspace_id,
            version=current_version + 1,
            content_sha256=content_sha256,
            effective_at=utc_now(),
            supersedes_id=previous_row.id if previous_row else None,
            document=self._document_of(document),
            actor=actor,
            origin=origin,
            correlation_id=correlation_id,
        )
        session.add(record)
        session.flush()
        return self._render_saved(record, unchanged=False)

    def _digest(self, document: DocT) -> str:
        """Return a stable content hash of the stored document form."""
        payload = json.dumps(self._document_of(document), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _latest(self, session: Session, workspace_id: str) -> RowT | None:
        """Return the newest version for a workspace, or ``None``."""
        row_type: Any = self.row_type
        return session.scalars(
            select(row_type)
            .where(row_type.workspace_id == workspace_id)
            .order_by(row_type.version.desc())
            .limit(1)
        ).first()

    @staticmethod
    def _workspace(session: Session, workspace_id: str) -> Workspace:
        """Return the workspace, or raise NotFoundError when unknown or archived."""
        workspace = session.get(Workspace, workspace_id)
        if workspace is None or workspace.archived_at is not None:
            raise NotFoundError(f"Workspace does not exist: {workspace_id}")
        return workspace

    def _build_row(
        self,
        *,
        workspace_id: str,
        version: int,
        content_sha256: str,
        effective_at: datetime,
        supersedes_id: str | None,
        document: dict[str, Any],
        actor: str,
        origin: str,
        correlation_id: str | None,
    ) -> RowT:
        raise NotImplementedError

    def _document_of(self, document: DocT) -> dict[str, Any]:
        raise NotImplementedError

    def _parse(self, record: RowT | None) -> DocT:
        raise NotImplementedError

    def _render_saved(self, record: RowT, *, unchanged: bool) -> SavedT:
        raise NotImplementedError

    def _render_state(self, workspace_id: str, record: RowT | None) -> StateT:
        raise NotImplementedError
