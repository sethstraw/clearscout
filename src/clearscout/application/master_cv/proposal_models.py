"""Typed responses and enum values for the master-CV proposal inbox."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from clearscout.domain import SearchProposalState
from clearscout.shared.models import StrictModel


class MasterCvProposalKind(StrEnum):
    """Which recurring pattern a proposal captures."""

    PROMOTE_TRANSFERABLE = "promote_transferable"
    ADD_MISSING = "add_missing"


class MasterCvProposalResponse(StrictModel):
    """One aggregated master-CV proposal awaiting a ruling."""

    id: str
    kind: MasterCvProposalKind
    requirement_key: str
    requirement_text: str
    evidence_note: str | None
    posting_count: int
    first_seen_at: datetime
    last_seen_at: datetime
    state: SearchProposalState
    proposed_at: datetime
    decided_at: datetime | None
