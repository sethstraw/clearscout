"""Mutable user notes kept outside the evidence system."""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from clearscout.application.failures import NotFoundError, ValidationError
from clearscout.application.workspaces.records.access import (
    active_parent_clauses,
    require_owned_entity,
    require_workspace,
)
from clearscout.application.workspaces.records.models import NoteResponse
from clearscout.application.workspaces.records.shared import audit_user_action
from clearscout.domain import utc_now
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import Note

_NOTE_ENTITY_TYPES = frozenset({"workspace", "job_posting", "application", "evaluation"})


class NoteRecordsService:
    """Create, search, and archive the notes that are never evidence."""

    def __init__(self, database: Database) -> None:
        """Bind these records to one durable database."""
        self.database = database

    def list_notes(
        self,
        workspace_id: str,
        *,
        search: str = "",
        entity_type: str | None = None,
        entity_id: str | None = None,
        include_archived: bool = False,
    ) -> list[NoteResponse]:
        """Search Markdown notes by title/body and optionally one owned attachment."""
        with self.database.session() as session:
            require_workspace(session, workspace_id)
            query = select(Note).where(Note.workspace_id == workspace_id)
            query = query.where(
                or_(
                    Note.entity_type == "workspace",
                    *active_parent_clauses(
                        Note.entity_type,
                        Note.entity_id,
                        workspace_id,
                        ("job_posting", "application", "evaluation"),
                    ),
                )
            )
            if not include_archived:
                query = query.where(Note.archived_at.is_(None))
            if entity_type is not None:
                self._note_attachment(session, workspace_id, entity_type, entity_id)
                query = query.where(Note.entity_type == entity_type, Note.entity_id == entity_id)
            elif entity_id is not None:
                raise ValidationError("A note entity_id requires entity_type.")
            term = search.strip()
            if term:
                query = query.where(
                    or_(
                        Note.title.icontains(term, autoescape=True),
                        Note.body.icontains(term, autoescape=True),
                    )
                )
            notes = session.scalars(
                query.order_by(Note.pinned.desc(), Note.updated_at.desc(), Note.id.desc())
            ).all()
            return [_note_record(note) for note in notes]

    def create_note(
        self,
        *,
        workspace_id: str,
        entity_type: str,
        entity_id: str | None,
        title: str,
        body: str,
        pinned: bool = False,
        correlation_id: str | None = None,
    ) -> NoteResponse:
        """Create a mutable user note that can never be selected as evidence."""
        clean_title, clean_body = _note_text(title, body)
        with self.database.session() as session:
            require_workspace(session, workspace_id)
            self._note_attachment(session, workspace_id, entity_type, entity_id)
            note = Note(
                workspace_id=workspace_id,
                entity_type=entity_type,
                entity_id=entity_id,
                title=clean_title,
                body=clean_body,
                pinned=pinned,
                created_by="local_user",
                origin="user_note",
                correlation_id=correlation_id,
            )
            session.add(note)
            session.flush()
            audit_user_action(
                session, workspace_id, "note.created", "note", note.id, correlation_id
            )
            return _note_record(note)

    def update_note(
        self,
        note_id: str,
        workspace_id: str,
        *,
        title: str,
        body: str,
        pinned: bool,
        correlation_id: str | None = None,
    ) -> NoteResponse:
        """Replace editable note fields without changing its attachment or provenance."""
        clean_title, clean_body = _note_text(title, body)
        with self.database.session() as session:
            note = self._note(session, note_id, workspace_id)
            note.title = clean_title
            note.body = clean_body
            note.pinned = pinned
            note.updated_at = utc_now()
            audit_user_action(
                session, workspace_id, "note.updated", "note", note.id, correlation_id
            )
            return _note_record(note)

    def archive_note(
        self, note_id: str, workspace_id: str, *, correlation_id: str | None = None
    ) -> NoteResponse:
        """Soft archive a note while preserving the timeline record."""
        with self.database.session() as session:
            note = self._note(session, note_id, workspace_id)
            note.archived_at = utc_now()
            audit_user_action(
                session, workspace_id, "note.archived", "note", note.id, correlation_id
            )
            return _note_record(note)

    @staticmethod
    def _note(session: Session, note_id: str, workspace_id: str) -> Note:
        note = session.get(Note, note_id)
        if note is None or note.workspace_id != workspace_id or note.archived_at is not None:
            raise NotFoundError(f"Note {note_id!r} does not exist in this workspace.")
        return note

    @staticmethod
    def _note_attachment(
        session: Session, workspace_id: str, entity_type: str, entity_id: str | None
    ) -> None:
        if entity_type not in _NOTE_ENTITY_TYPES:
            raise ValidationError(f"Notes cannot be attached to {entity_type!r}.")
        if entity_type == "workspace":
            if entity_id not in {None, workspace_id}:
                raise ValidationError("Workspace notes cannot name another entity.")
            return
        if entity_id is None:
            raise ValidationError(f"A {entity_type} note requires entity_id.")
        require_owned_entity(session, workspace_id, entity_type, entity_id)


def _note_record(note: Note) -> NoteResponse:
    """Return a note with its non-evidence boundary explicit."""
    return NoteResponse.model_validate(
        {
            "id": note.id,
            "workspace_id": note.workspace_id,
            "entity_type": note.entity_type,
            "entity_id": note.entity_id,
            "title": note.title,
            "body": note.body,
            "pinned": note.pinned,
            "created_at": note.created_at,
            "updated_at": note.updated_at,
            "archived_at": note.archived_at,
            "evidence_eligible": False,
        }
    )


def _note_text(title: str, body: str) -> tuple[str, str]:
    clean_title = title.strip()
    if not clean_title or len(clean_title) > 500:
        raise ValidationError("Note title must contain 1 to 500 characters.")
    if not body.strip():
        raise ValidationError("Note body cannot be empty.")
    return clean_title, body
