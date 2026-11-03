"""Workspace tags and their current assignments."""

from __future__ import annotations

import re
from collections.abc import Sequence

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from clearscout.application.failures import ConflictError, NotFoundError, ValidationError
from clearscout.application.workspaces.records.access import (
    active_parent_clauses,
    require_owned_entity,
    require_workspace,
)
from clearscout.application.workspaces.records.models import TagAssignmentResponse, TagResponse
from clearscout.application.workspaces.records.shared import audit_user_action
from clearscout.domain import utc_now
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import Tag, TagAssignment

_TAG_ENTITY_TYPES = frozenset({"job_posting", "application"})
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


class TagRecordsService:
    """Maintain workspace tags and the jobs and applications they mark."""

    def __init__(self, database: Database) -> None:
        """Bind these records to one durable database."""
        self.database = database

    def list_tags(self, workspace_id: str) -> list[TagResponse]:
        """List active workspace tags and their current assignments."""
        with self.database.session() as session:
            require_workspace(session, workspace_id)
            tags = session.scalars(
                select(Tag)
                .where(Tag.workspace_id == workspace_id, Tag.archived_at.is_(None))
                .order_by(Tag.name)
            ).all()
            return self._tag_records(session, tags, workspace_id)

    def create_tag(
        self,
        *,
        workspace_id: str,
        name: str,
        color: str,
        correlation_id: str | None = None,
    ) -> TagResponse:
        """Create one reusable colored tag."""
        clean_name, clean_color = _tag_values(name, color)
        with self.database.session() as session:
            require_workspace(session, workspace_id)
            duplicate = session.scalar(
                select(Tag).where(Tag.workspace_id == workspace_id, Tag.name == clean_name)
            )
            if duplicate is not None:
                raise ConflictError(f"Tag {clean_name!r} already exists.")
            tag = Tag(
                workspace_id=workspace_id,
                name=clean_name,
                color=clean_color,
                origin="user_tag",
                correlation_id=correlation_id,
            )
            session.add(tag)
            session.flush()
            audit_user_action(session, workspace_id, "tag.created", "tag", tag.id, correlation_id)
            return self._tag_record(session, tag)

    def update_tag(
        self,
        tag_id: str,
        workspace_id: str,
        *,
        name: str,
        color: str,
        correlation_id: str | None = None,
    ) -> TagResponse:
        """Edit tag presentation without changing its assignments."""
        clean_name, clean_color = _tag_values(name, color)
        with self.database.session() as session:
            tag = self._tag(session, tag_id, workspace_id)
            duplicate = session.scalar(
                select(Tag).where(
                    Tag.workspace_id == workspace_id,
                    Tag.name == clean_name,
                    Tag.id != tag.id,
                )
            )
            if duplicate is not None:
                raise ConflictError(f"Tag {clean_name!r} already exists.")
            tag.name = clean_name
            tag.color = clean_color
            audit_user_action(session, workspace_id, "tag.updated", "tag", tag.id, correlation_id)
            return self._tag_record(session, tag)

    def archive_tag(
        self, tag_id: str, workspace_id: str, *, correlation_id: str | None = None
    ) -> TagResponse:
        """Soft archive a tag; existing assignments remain as recorded metadata."""
        with self.database.session() as session:
            tag = self._tag(session, tag_id, workspace_id)
            tag.archived_at = utc_now()
            audit_user_action(session, workspace_id, "tag.archived", "tag", tag.id, correlation_id)
            return self._tag_record(session, tag)

    def assign_tag(
        self,
        *,
        tag_id: str,
        workspace_id: str,
        entity_type: str,
        entity_id: str,
        correlation_id: str | None = None,
    ) -> TagAssignmentResponse:
        """Attach one tag only to a job or application owned by the workspace."""
        if entity_type not in _TAG_ENTITY_TYPES:
            raise ValidationError("Tags may be assigned only to jobs and applications.")
        with self.database.session() as session:
            tag = self._tag(session, tag_id, workspace_id)
            require_owned_entity(session, workspace_id, entity_type, entity_id)
            existing = session.scalar(
                select(TagAssignment).where(
                    TagAssignment.tag_id == tag.id,
                    TagAssignment.entity_type == entity_type,
                    TagAssignment.entity_id == entity_id,
                )
            )
            if existing is None:
                existing = TagAssignment(
                    tag_id=tag.id,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    origin="user_tag",
                    correlation_id=correlation_id,
                )
                session.add(existing)
                session.flush()
            audit_user_action(
                session, workspace_id, "tag.assigned", entity_type, entity_id, correlation_id
            )
            return _assignment_record(existing, tag)

    def unassign_tag(
        self,
        *,
        tag_id: str,
        workspace_id: str,
        entity_type: str,
        entity_id: str,
        correlation_id: str | None = None,
    ) -> None:
        """Remove mutable tag metadata without touching the tagged entity."""
        with self.database.session() as session:
            tag = self._tag(session, tag_id, workspace_id)
            require_owned_entity(session, workspace_id, entity_type, entity_id)
            assignment = session.scalar(
                select(TagAssignment).where(
                    TagAssignment.tag_id == tag.id,
                    TagAssignment.entity_type == entity_type,
                    TagAssignment.entity_id == entity_id,
                )
            )
            if assignment is None:
                raise NotFoundError("Tag assignment does not exist.")
            session.delete(assignment)
            audit_user_action(
                session, workspace_id, "tag.unassigned", entity_type, entity_id, correlation_id
            )

    @staticmethod
    def _tag(session: Session, tag_id: str, workspace_id: str) -> Tag:
        tag = session.get(Tag, tag_id)
        if tag is None or tag.workspace_id != workspace_id or tag.archived_at is not None:
            raise NotFoundError(f"Tag {tag_id!r} does not exist in this workspace.")
        return tag

    @classmethod
    def _tag_record(cls, session: Session, tag: Tag) -> TagResponse:
        return cls._tag_records(session, [tag], tag.workspace_id)[0]

    @staticmethod
    def _tag_records(session: Session, tags: Sequence[Tag], workspace_id: str) -> list[TagResponse]:
        if not tags:
            return []
        assignments = session.scalars(
            select(TagAssignment)
            .where(
                TagAssignment.tag_id.in_([tag.id for tag in tags]),
                or_(
                    *active_parent_clauses(
                        TagAssignment.entity_type,
                        TagAssignment.entity_id,
                        workspace_id,
                        ("job_posting", "application"),
                    )
                ),
            )
            .order_by(
                TagAssignment.tag_id,
                TagAssignment.entity_type,
                TagAssignment.entity_id,
            )
        ).all()
        by_tag: dict[str, list[TagAssignment]] = {}
        for assignment in assignments:
            by_tag.setdefault(assignment.tag_id, []).append(assignment)
        return [
            TagResponse(
                id=tag.id,
                workspace_id=tag.workspace_id,
                name=tag.name,
                color=tag.color,
                created_at=tag.created_at,
                assignments=[_assignment_record(item, tag) for item in by_tag.get(tag.id, [])],
            )
            for tag in tags
        ]


def _assignment_record(assignment: TagAssignment, tag: Tag) -> TagAssignmentResponse:
    """Return one colored tag assignment."""
    return TagAssignmentResponse.model_validate(
        {
            "id": assignment.id,
            "tag_id": tag.id,
            "tag_name": tag.name,
            "color": tag.color,
            "entity_type": assignment.entity_type,
            "entity_id": assignment.entity_id,
        }
    )


def _tag_values(name: str, color: str) -> tuple[str, str]:
    clean_name = name.strip()
    clean_color = color.strip()
    if not clean_name or len(clean_name) > 120:
        raise ValidationError("Tag name must contain 1 to 120 characters.")
    if not _COLOR.fullmatch(clean_color):
        raise ValidationError("Tag color must be a six-digit hexadecimal color such as #4f46e5.")
    return clean_name, clean_color.lower()
