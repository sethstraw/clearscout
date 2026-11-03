"""Read and append immutable workspace guardrail versions."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy import select

from clearscout.application.tailoring.guardrails import Guardrails
from clearscout.application.workspaces.guardrail_models import (
    GuardrailSavedResponse,
    GuardrailStateResponse,
    GuardrailVersionResponse,
)
from clearscout.application.workspaces.versioned import VersionedDocumentService
from clearscout.infrastructure.persistence.models import GuardrailVersion


def document_of(rules: Guardrails) -> dict[str, Any]:
    """Return the JSON-safe document stored for one guardrail version."""
    return rules.model_dump(mode="json")


def digest(rules: Guardrails) -> str:
    """Return a stable content hash for one validated guardrail document."""
    payload = json.dumps(document_of(rules), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class GuardrailService(
    VersionedDocumentService[
        GuardrailVersion, Guardrails, GuardrailSavedResponse, GuardrailStateResponse
    ]
):
    """The current guardrails for a workspace and their append-only history."""

    row_type = GuardrailVersion
    entity_label = "guardrails"

    def history(self, workspace_id: str) -> list[GuardrailVersionResponse]:
        """Return every rule version, newest first."""
        with self.database.session() as session:
            self._workspace(session, workspace_id)
            records = session.scalars(
                select(GuardrailVersion)
                .where(GuardrailVersion.workspace_id == workspace_id)
                .order_by(GuardrailVersion.version.desc())
            ).all()
            return [
                GuardrailVersionResponse.model_validate(
                    {
                        "id": record.id,
                        "version": record.version,
                        "content_sha256": record.content_sha256,
                        "effective_at": record.effective_at.isoformat(),
                        "supersedes_id": record.supersedes_id,
                        "created_by": record.created_by,
                        "origin": record.origin,
                        "rules": record.document,
                    }
                )
                for record in records
            ]

    def _document_of(self, document: Guardrails) -> dict[str, Any]:
        return document_of(document)

    def _parse(self, record: GuardrailVersion | None) -> Guardrails:
        if record is None:
            return Guardrails()
        return Guardrails.model_validate(record.document)

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
    ) -> GuardrailVersion:
        return GuardrailVersion(
            workspace_id=workspace_id,
            version=version,
            content_sha256=content_sha256,
            effective_at=effective_at,
            supersedes_id=supersedes_id,
            document=document,
            created_by=actor,
            origin=origin,
            correlation_id=correlation_id,
        )

    def _render_saved(self, record: GuardrailVersion, *, unchanged: bool) -> GuardrailSavedResponse:
        return GuardrailSavedResponse.model_validate(
            {
                "id": record.id,
                "workspace_id": record.workspace_id,
                "version": record.version,
                "content_sha256": record.content_sha256,
                "effective_at": record.effective_at.isoformat(),
                "supersedes_id": record.supersedes_id,
                "unchanged": unchanged,
                "configured": Guardrails.model_validate(record.document).configured,
                "rules": record.document,
            }
        )

    def _render_state(
        self, workspace_id: str, record: GuardrailVersion | None
    ) -> GuardrailStateResponse:
        rules = self._parse(record)
        return GuardrailStateResponse.model_validate(
            {
                "workspace_id": workspace_id,
                "version": record.version if record else 0,
                "content_sha256": record.content_sha256 if record else "",
                "effective_at": record.effective_at.isoformat() if record else None,
                "configured": rules.configured,
                "rules": document_of(rules),
            }
        )
