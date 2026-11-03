"""Typed read models for workspace guardrail versions."""

from datetime import datetime

from clearscout.application.tailoring.guardrails import Guardrails
from clearscout.shared.models import StrictModel


class GuardrailStateResponse(StrictModel):
    """Current immutable guardrail version, or the unconfigured zero value."""

    workspace_id: str
    version: int
    content_sha256: str
    effective_at: datetime | None
    configured: bool
    rules: Guardrails


class GuardrailSavedResponse(StrictModel):
    """One append-only guardrail version after a write."""

    id: str
    workspace_id: str
    version: int
    content_sha256: str
    effective_at: datetime
    supersedes_id: str | None
    unchanged: bool
    configured: bool
    rules: Guardrails


class GuardrailVersionResponse(StrictModel):
    """One saved workspace guardrail version."""

    id: str
    version: int
    content_sha256: str
    effective_at: datetime
    supersedes_id: str | None
    created_by: str
    origin: str
    rules: Guardrails
