"""Posting resolution preflight and closure reconcile requests and results."""

from datetime import datetime
from typing import Literal, Self

from pydantic import Field, model_validator

from tailorcv.application.job_search.models.vocabulary import PendingDiscoveryId
from tailorcv.shared.models import StrictModel


class PostingResolutionPreflightRequest(StrictModel):
    """An explicit bounded selection of pending postings for assisted recovery."""

    pending_discovery_ids: list[PendingDiscoveryId] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_discoveries(self) -> Self:
        """Keep the snapshot selection ordered while refusing duplicate work."""
        if len(set(self.pending_discovery_ids)) != len(self.pending_discovery_ids):
            raise ValueError("Posting resolution pending_discovery_ids must be unique.")
        return self


class PendingClosureReconcileRequest(StrictModel):
    """An explicit bounded selection whose saved current evidence may close it."""

    pending_discovery_ids: list[PendingDiscoveryId] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_discoveries(self) -> Self:
        """Reject a selection that could record the same closure twice."""
        if len(set(self.pending_discovery_ids)) != len(self.pending_discovery_ids):
            raise ValueError("Pending closure pending_discovery_ids must be unique.")
        return self


class PendingClosureReconcileResult(StrictModel):
    """The saved-evidence outcome for one requested pending discovery."""

    pending_discovery_id: PendingDiscoveryId
    outcome: Literal["applied", "unchanged", "not_currently_closed"]
    evidence_id: str | None = None
    observed_at: datetime | None = None


class PendingClosureReconcileResponse(StrictModel):
    """The per-discovery outcomes from applying saved closure evidence."""

    results: list[PendingClosureReconcileResult] = Field(min_length=1, max_length=50)
