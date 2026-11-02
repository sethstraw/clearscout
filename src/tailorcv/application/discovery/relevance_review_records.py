"""Append validated relevance reviews; the latest per discovery is current."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tailorcv.application.job_search.models import (
    RelevanceReviewAnswerOutcome,
    RelevanceReviewCitation,
    RelevanceReviewVerdict,
)
from tailorcv.domain import utc_now
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    PendingDiscovery,
    RelevanceReview,
)

# Newest first, with created_at and id breaking a tie between reviews recorded
# in the same batch.
CURRENT_FIRST = (
    RelevanceReview.reviewed_at.desc(),
    RelevanceReview.created_at.desc(),
    RelevanceReview.id.desc(),
)


def current_review_in(
    session: Session, workspace_id: str, pending_discovery_id: str
) -> RelevanceReview | None:
    """Return the review in force for one discovery, or None when none is stored."""
    return session.scalars(
        select(RelevanceReview)
        .where(
            RelevanceReview.workspace_id == workspace_id,
            RelevanceReview.pending_discovery_id == pending_discovery_id,
        )
        .order_by(*CURRENT_FIRST)
        .limit(1)
    ).first()


@dataclass(frozen=True)
class RelevanceReviewPostingInput:
    """Fetched posting text and source facts used by relevance review."""

    body: str
    fetched_at: datetime


@dataclass(frozen=True)
class RelevanceReviewContract:
    """The instructions and response-schema digests a review was produced under."""

    instructions_digest: str
    schema_digest: str


@dataclass(frozen=True)
class ValidatedRelevanceReview:
    """One provider recommendation after application-owned citation resolution.

    An ``insufficient_posting_text`` outcome carries no recommendation and no class.
    """

    pending_discovery_id: str
    outcome: RelevanceReviewAnswerOutcome
    recommendation: RelevanceReviewVerdict | None
    summary: str
    core_work_alignment: str | None
    core_qualification_alignment: str | None
    hard_condition: str | None
    decisive_basis: str | None
    posting_citations: list[RelevanceReviewCitation]
    master_cv_evidence_ids: list[str]
    unresolved_questions: list[str]
    matched_exclusion: str | None = None


class RelevanceReviewRecorder:
    """Append validated discovery reviews.

    History lasts while the posting's words, URL and title are unchanged; a
    change to any of them clears the row's reviews.
    """

    def __init__(self, database: Database) -> None:
        """Bind review writes to their database."""
        self.database = database

    def append(
        self,
        *,
        workspace_id: str,
        task_id: str,
        master_cv_version_id: str,
        search_parameter_set_id: str,
        search_context: str,
        contract: RelevanceReviewContract,
        pending: list[PendingDiscovery],
        inputs: dict[str, RelevanceReviewPostingInput],
        recommendations: list[ValidatedRelevanceReview],
        notes: Mapping[str, str],
    ) -> None:
        """Add one review per recommendation, keeping every earlier review as history.

        ``notes`` holds the keep note each row was sent with, as preparation read it.
        """
        if not recommendations:
            return
        by_id = {item.id: item for item in pending}
        with self.database.session() as session:
            for item in recommendations:
                source = by_id[item.pending_discovery_id]
                fetched = inputs[item.pending_discovery_id]
                values: dict[str, Any] = {
                    "workspace_id": workspace_id,
                    "pending_discovery_id": item.pending_discovery_id,
                    "task_id": task_id,
                    "master_cv_version_id": master_cv_version_id,
                    "search_parameter_set_id": search_parameter_set_id,
                    "recommendation": item.recommendation,
                    "summary": item.summary,
                    "core_work_alignment": item.core_work_alignment,
                    "core_qualification_alignment": item.core_qualification_alignment,
                    "hard_condition": item.hard_condition,
                    "decisive_basis": item.decisive_basis,
                    "matched_exclusion": item.matched_exclusion,
                    "keep_note_sha256": keep_note_digest(notes.get(item.pending_discovery_id)),
                    "posting_citations": [
                        citation.model_dump(mode="json") for citation in item.posting_citations
                    ],
                    "master_cv_evidence_ids": item.master_cv_evidence_ids,
                    "unresolved_questions": item.unresolved_questions,
                    "fetched_at": fetched.fetched_at,
                    "source_url_sha256": relevance_review_digest(
                        source.canonical_url or source.discovery_url
                    ),
                    "body_sha256": relevance_review_digest(fetched.body),
                    "search_context_sha256": relevance_review_digest(search_context),
                    "reviewed_at": utc_now(),
                    "instructions_digest": contract.instructions_digest,
                    "schema_digest": contract.schema_digest,
                    "created_by": "worker",
                    "origin": "discovery.relevance_review",
                    "correlation_id": task_id,
                }
                session.add(RelevanceReview(**values))


def keep_note_digest(note: str | None) -> str | None:
    """Return the digest a review keeps of the note it was sent, or None without one."""
    return relevance_review_digest(note) if note else None


def relevance_review_digest(value: Any) -> str:
    """Return the stable digest used to identify current review inputs."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
