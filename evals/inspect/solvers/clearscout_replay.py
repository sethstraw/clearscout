"""Replay solver: report the classification a workspace database already stores.

The solver is a replay-consistency check, not a prompt-quality check. It opens
a workspace database read-only, finds the newest evaluation for the posting the
sample points at, and reports the classification the stored match evidence
carries for the requested requirement key. Nothing here re-runs the model.

A sample carries the database URL through ``metadata['database_url']`` or the
process falls back to the ``CLEARSCOUT_EVAL_DATABASE_URL`` environment variable.
A run predating the stored classification answers ``UNKNOWN``. A requirement
with no stored evidence row at all answers ``GAP`` without asserting
``outcome_complete``, which the scorer refuses to credit.
"""

from __future__ import annotations

import os
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models.evaluations import (
    JobEvaluation,
    MatchEvidence,
    PostingRequirement,
)

_DATABASE_URL_ENV = "CLEARSCOUT_EVAL_DATABASE_URL"
_UNKNOWN = "UNKNOWN"
_SUPPORTING_CLASSIFICATIONS = frozenset({"DIRECT", "TAILORING_ONLY", "TRANSFERABLE", "ASSERTED"})


def _resolve_database_url(sample: dict[str, Any]) -> str | None:
    """Read the database URL from sample metadata first, then from the environment."""
    metadata = sample.get("metadata") or {}
    url = metadata.get("database_url")
    if isinstance(url, str) and url.strip():
        return url.strip()
    env = os.environ.get(_DATABASE_URL_ENV, "").strip()
    return env or None


def _classification_for(
    session: Session, *, posting_id: str, requirement_key: str
) -> tuple[str, list[str], bool]:
    """Return the stored classification, its citations, and whether a row stores it."""
    evaluation_id = session.execute(
        select(JobEvaluation.id)
        .where(JobEvaluation.job_posting_id == posting_id)
        .order_by(JobEvaluation.version_number.desc())
        .limit(1)
    ).scalar_one_or_none()
    if evaluation_id is None:
        return _UNKNOWN, [], False

    requirement_present = session.execute(
        select(PostingRequirement.id)
        .where(PostingRequirement.job_evaluation_id == evaluation_id)
        .where(PostingRequirement.requirement_key == requirement_key)
        .limit(1)
    ).scalar_one_or_none()
    if requirement_present is None:
        return _UNKNOWN, [], False

    citations = session.execute(
        select(MatchEvidence.classification, MatchEvidence.master_cv_entry_key)
        .where(MatchEvidence.evaluation_id == evaluation_id)
        .where(MatchEvidence.requirement_key == requirement_key)
    ).all()

    if not citations:
        # No evidence row was ever written here, which is not a stored gap.
        return "GAP", [], False
    supporting = [row for row in citations if row[0] in _SUPPORTING_CLASSIFICATIONS]
    if supporting:
        entry_keys = [row[1] for row in supporting if row[1]]
        return supporting[0][0], entry_keys, True
    return "GAP", [], True


def _sample_metadata(sample: dict[str, Any]) -> dict[str, Any]:
    metadata = sample.get("metadata")
    return dict(metadata) if isinstance(metadata, dict) else {}


def _flat_metadata(sample: dict[str, Any]) -> dict[str, Any]:
    """Read metadata fields from either sample.metadata or the sample top level."""
    merged = _sample_metadata(sample)
    for key in ("posting_id", "requirement_key", "database_url"):
        if key not in merged and key in sample:
            merged[key] = sample[key]
    return merged


def clearscout_replay_solver() -> Any:
    """Return the plain solver callable that reads the stored classification."""

    def solve(sample: dict[str, Any]) -> dict[str, Any]:
        metadata = _flat_metadata(sample)
        posting_id = metadata.get("posting_id")
        requirement_key = metadata.get("requirement_key")
        database_url = _resolve_database_url({"metadata": metadata})

        if not isinstance(posting_id, str) or not posting_id.strip():
            raise ValueError("replay sample is missing posting_id")
        if not isinstance(requirement_key, str) or not requirement_key.strip():
            raise ValueError("replay sample is missing requirement_key")
        if not database_url:
            raise ValueError("replay sample has no database_url or CLEARSCOUT_EVAL_DATABASE_URL")

        database = Database(database_url, require_existing=True)
        try:
            with database.session() as session:
                classification, entry_keys, outcome_complete = _classification_for(
                    session, posting_id=posting_id, requirement_key=requirement_key
                )
        finally:
            database.dispose()

        return {
            "completion": classification,
            "metadata": {
                **metadata,
                "entry_keys": entry_keys,
                "missing": classification == _UNKNOWN,
                "outcome_complete": outcome_complete,
            },
        }

    return solve
