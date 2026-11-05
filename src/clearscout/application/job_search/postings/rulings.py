"""Workspace overrides on the analysis's judgment for one posting requirement.

The workspace states whether a requirement is met, not applicable, or disputed.
The ruling is keyed by the requirement's durable identity, so it survives a
re-evaluation of the posting. Every ruling change writes an ``audit_events``
entry, so the current state is one row and the history stays recoverable.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from clearscout.application.discovery.missed_requirement_reader import refresh_for_requirement_key
from clearscout.domain import utc_now
from clearscout.domain.rulings import Ruling
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    AuditEvent,
    PostingRequirement,
    RequirementRuling,
)


class RequirementRulingService:
    """Record, list, and clear workspace rulings on posting requirements."""

    def __init__(self, database: Database) -> None:
        """Bind the service to one durable database."""
        self.database = database

    def set(
        self,
        *,
        workspace_id: str,
        requirement_key: str,
        ruling: Ruling,
        note: str | None,
        audit: dict[str, Any],
    ) -> RequirementRuling:
        """Upsert the ruling for one requirement, appending an audit event."""
        with self.database.session() as session:
            row = session.scalar(
                select(RequirementRuling).where(
                    RequirementRuling.workspace_id == workspace_id,
                    RequirementRuling.requirement_key == requirement_key,
                )
            )
            action = "ruling.set"
            if row is None:
                row = RequirementRuling(
                    workspace_id=workspace_id,
                    requirement_key=requirement_key,
                    ruling=ruling.value,
                    note=note,
                    updated_at=utc_now(),
                    **audit,
                )
                session.add(row)
            else:
                row.ruling = ruling.value
                row.note = note
                row.updated_at = utc_now()
            session.flush()
            session.add(
                AuditEvent(
                    workspace_id=workspace_id,
                    action=action,
                    entity_type="requirement_ruling",
                    entity_id=row.id,
                    details={
                        "requirement_key": requirement_key,
                        "ruling": ruling.value,
                        "note": note,
                    },
                    **audit,
                )
            )
            session.flush()
            refresh_for_requirement_key(session, workspace_id, requirement_key, audit)
            session.flush()
            session.expunge(row)
            return row

    def clear(
        self,
        *,
        workspace_id: str,
        requirement_key: str,
        audit: dict[str, Any],
    ) -> bool:
        """Remove the ruling for one requirement. Returns whether a row was cleared."""
        with self.database.session() as session:
            row = session.scalar(
                select(RequirementRuling).where(
                    RequirementRuling.workspace_id == workspace_id,
                    RequirementRuling.requirement_key == requirement_key,
                )
            )
            if row is None:
                return False
            entity_id = row.id
            previous_ruling = row.ruling
            session.delete(row)
            session.add(
                AuditEvent(
                    workspace_id=workspace_id,
                    action="ruling.cleared",
                    entity_type="requirement_ruling",
                    entity_id=entity_id,
                    details={
                        "requirement_key": requirement_key,
                        "previous_ruling": previous_ruling,
                    },
                    **audit,
                )
            )
            session.flush()
            refresh_for_requirement_key(session, workspace_id, requirement_key, audit)
            return True

    def by_key(self, workspace_id: str) -> dict[str, Ruling]:
        """Return the current ruling for every ruled requirement, keyed by identity."""
        with self.database.session() as session:
            rows = session.scalars(
                select(RequirementRuling).where(RequirementRuling.workspace_id == workspace_id)
            ).all()
        return {row.requirement_key: Ruling(row.ruling) for row in rows}

    def _active_disputes_of(
        self,
        workspace_id: str,
        ruling: Ruling,
        *,
        job_posting_id: str | None = None,
    ) -> list[_ListedRuling]:
        """Return active disputes of one kind, optionally limited to one posting."""
        filters = [
            RequirementRuling.workspace_id == workspace_id,
            RequirementRuling.ruling == ruling.value,
        ]
        if job_posting_id is not None:
            filters.append(
                select(PostingRequirement.id)
                .where(
                    PostingRequirement.job_posting_id == job_posting_id,
                    PostingRequirement.requirement_key == RequirementRuling.requirement_key,
                )
                .exists()
            )
        with self.database.session() as session:
            rows = session.scalars(
                select(RequirementRuling)
                .where(*filters)
                .order_by(RequirementRuling.updated_at.desc())
            ).all()
            texts = _requirement_texts(session, [row.requirement_key for row in rows])
        return [
            _ListedRuling(
                requirement_key=row.requirement_key,
                ruling=ruling,
                note=row.note,
                updated_at=row.updated_at,
                requirement_text=texts.get(row.requirement_key, ""),
            )
            for row in rows
        ]

    def active_extraction_disputes(
        self, workspace_id: str, *, job_posting_id: str | None = None
    ) -> list[_ListedRuling]:
        """Return active extraction disputes, optionally limited to one posting."""
        return self._active_disputes_of(
            workspace_id, Ruling.DISPUTED_EXTRACTION, job_posting_id=job_posting_id
        )

    def active_fit_disputes(
        self, workspace_id: str, *, job_posting_id: str | None = None
    ) -> list[_ListedRuling]:
        """Return active fit disputes, optionally limited to one posting."""
        return self._active_disputes_of(
            workspace_id, Ruling.DISPUTED_FIT, job_posting_id=job_posting_id
        )

    def list(self, workspace_id: str) -> list[_ListedRuling]:
        """Return every ruling in one workspace, denormalized with requirement text."""
        with self.database.session() as session:
            rows = session.scalars(
                select(RequirementRuling)
                .where(RequirementRuling.workspace_id == workspace_id)
                .order_by(RequirementRuling.updated_at.desc())
            ).all()
            texts = _requirement_texts(session, [row.requirement_key for row in rows])
        return [
            _ListedRuling(
                requirement_key=row.requirement_key,
                ruling=Ruling(row.ruling),
                note=row.note,
                updated_at=row.updated_at,
                requirement_text=texts.get(row.requirement_key, ""),
            )
            for row in rows
        ]


class DisputeLike(Protocol):
    """The shape dispute_block needs from any input row."""

    requirement_key: str
    requirement_text: str
    note: str | None


def dispute_block(disputes: Iterable[DisputeLike]) -> str:
    """Return the disputed-rulings block for a prompt template, empty when none."""
    lines: list[str] = []
    for item in disputes:
        text = item.requirement_text or "(requirement text unavailable)"
        lines.append(f"- {item.requirement_key}: {text}")
        if item.note:
            lines.append(f"    note: {item.note}")
    return "\n".join(lines)


def cited_in_body(cited_text: str, body: str) -> bool:
    """Return whether the model's cited_text appears in the posting body.

    Compared on casefold + whitespace-collapsed forms so a re-extraction can
    overrule a dispute only when the cited text is actually in the posting.
    Empty strings are refused.
    """
    if not cited_text or not body:
        return False
    haystack = _normalize_span(body)
    needle = _normalize_span(cited_text)
    return bool(needle) and needle in haystack


def reverse_ruling(
    session: Session,
    *,
    workspace_id: str,
    requirement_key: str,
    disputed: Ruling,
    action: str,
    origin: str,
    details: dict[str, Any],
    correlation_id: str | None,
) -> str | None:
    """Reverse one ruling still standing at ``disputed`` and file its audit event.

    Returns the ruling id, or None when the row is gone or the workspace has
    already moved it off ``disputed``, so a paid re-read never overrules a
    ruling the workspace changed while that read was in flight.
    """
    row = session.scalar(
        select(RequirementRuling).where(
            RequirementRuling.workspace_id == workspace_id,
            RequirementRuling.requirement_key == requirement_key,
        )
    )
    if row is None or row.ruling != disputed.value:
        return None
    row.ruling = Ruling.REVERSED.value
    row.updated_at = utc_now()
    audit = {"created_by": "worker", "origin": origin, "correlation_id": correlation_id}
    session.add(
        AuditEvent(
            workspace_id=workspace_id,
            action=action,
            entity_type="requirement_ruling",
            entity_id=row.id,
            details={"requirement_key": requirement_key, **details},
            **audit,
        )
    )
    session.flush()
    refresh_for_requirement_key(session, workspace_id, requirement_key, audit)
    return row.id


_WHITESPACE = re.compile(r"\s+")


def _normalize_span(value: str) -> str:
    return _WHITESPACE.sub(" ", value.casefold()).strip()


def _requirement_texts(session: Session, requirement_keys: Sequence[str]) -> dict[str, str]:
    """Return the most recently stored text for each requirement key."""
    if not requirement_keys:
        return {}
    rows = session.scalars(
        select(PostingRequirement)
        .where(PostingRequirement.requirement_key.in_(requirement_keys))
        .order_by(PostingRequirement.created_at.desc())
    ).all()
    # First seen wins because the query is ordered newest first.
    texts: dict[str, str] = {}
    for row in rows:
        texts.setdefault(row.requirement_key, row.text)
    return texts


class _ListedRuling:
    """A ruling paired with the requirement text last observed under its key."""

    __slots__ = ("note", "requirement_key", "requirement_text", "ruling", "updated_at")

    def __init__(
        self,
        *,
        requirement_key: str,
        ruling: Ruling,
        note: str | None,
        updated_at: datetime,
        requirement_text: str,
    ) -> None:
        self.requirement_key = requirement_key
        self.ruling = ruling
        self.note = note
        self.updated_at = updated_at
        self.requirement_text = requirement_text
