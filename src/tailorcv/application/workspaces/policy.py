"""Read and append immutable workspace learning-loop policy versions.

The knobs the learning loop reads are editable, versioned, and audit-tracked
the same way guardrails are.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from tailorcv.application.workspaces.policy_models import (
    LearningLoopPolicySavedResponse,
    LearningLoopPolicyStateResponse,
)
from tailorcv.application.workspaces.versioned import VersionedDocumentService
from tailorcv.domain.policy import LearningLoopPolicy
from tailorcv.infrastructure.persistence.models import LearningLoopPolicyVersion


def _document_of(policy: LearningLoopPolicy) -> dict[str, Any]:
    """Return the JSON-safe document stored for one policy version."""
    return policy.model_dump(mode="json")


class LearningLoopPolicyService(
    VersionedDocumentService[
        LearningLoopPolicyVersion,
        LearningLoopPolicy,
        LearningLoopPolicySavedResponse,
        LearningLoopPolicyStateResponse,
    ]
):
    """The current learning-loop policy for a workspace and its append-only history."""

    row_type = LearningLoopPolicyVersion
    entity_label = "learning-loop policy"

    def _document_of(self, document: LearningLoopPolicy) -> dict[str, Any]:
        return _document_of(document)

    def _parse(self, record: LearningLoopPolicyVersion | None) -> LearningLoopPolicy:
        if record is None:
            return LearningLoopPolicy()
        return LearningLoopPolicy.model_validate(record.document)

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
    ) -> LearningLoopPolicyVersion:
        return LearningLoopPolicyVersion(
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

    def _render_saved(
        self, record: LearningLoopPolicyVersion, *, unchanged: bool
    ) -> LearningLoopPolicySavedResponse:
        return LearningLoopPolicySavedResponse.model_validate(
            {
                "id": record.id,
                "workspace_id": record.workspace_id,
                "version": record.version,
                "content_sha256": record.content_sha256,
                "effective_at": record.effective_at.isoformat(),
                "supersedes_id": record.supersedes_id,
                "unchanged": unchanged,
                "policy": record.document,
            }
        )

    def _render_state(
        self, workspace_id: str, record: LearningLoopPolicyVersion | None
    ) -> LearningLoopPolicyStateResponse:
        policy = self._parse(record)
        return LearningLoopPolicyStateResponse.model_validate(
            {
                "workspace_id": workspace_id,
                "version": record.version if record else 0,
                "content_sha256": record.content_sha256 if record else "",
                "effective_at": record.effective_at.isoformat() if record else None,
                "policy": _document_of(policy),
            }
        )
