"""Read the current relevance review for pending discoveries."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tailorcv.application.discovery.pending import PendingMarkers, family_markers
from tailorcv.application.discovery.relevance_review import current_review_contract
from tailorcv.application.discovery.relevance_review_records import (
    CURRENT_FIRST,
    RelevanceReviewContract,
    keep_note_digest,
)
from tailorcv.application.job_search.models import (
    RelevanceReviewResponse,
    RelevanceReviewStaleInput,
)
from tailorcv.application.master_cv.versioning import current_version_id
from tailorcv.application.workspaces.parameters import parameter_snapshot_in
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    PendingDiscovery,
    RelevanceReview,
    RelevanceReviewDispute,
)


@dataclass(frozen=True)
class _CurrentInputs:
    """The workspace inputs in force, which a stored review is dated against."""

    master_cv_version_id: str | None
    search_parameter_set_id: str | None
    contract: RelevanceReviewContract

    def changed_since(
        self, review: RelevanceReview, keep_note: str | None
    ) -> list[RelevanceReviewStaleInput]:
        """Name each input that differs from what the review was sent."""
        changed: list[RelevanceReviewStaleInput] = []
        # Preparation holds a too-short answer back on body and prompt alone.
        if review.recommendation is not None:
            if _moved(review.master_cv_version_id, self.master_cv_version_id):
                changed.append("master_cv")
            if _moved(review.search_parameter_set_id, self.search_parameter_set_id):
                changed.append("search_parameters")
            if review.keep_note_sha256 != keep_note_digest(keep_note):
                changed.append("keep_note")
        if (review.instructions_digest, review.schema_digest) != (
            self.contract.instructions_digest,
            self.contract.schema_digest,
        ):
            changed.append("review_instructions")
        return changed


def _moved(recorded: str, current: str | None) -> bool:
    return current is not None and recorded != current


def _sent_notes(
    session: Session,
    workspace_id: str,
    pending_ids: list[str],
    markers: Mapping[str, PendingMarkers] | None,
) -> dict[str, str | None]:
    """Return the family keep note preparation would send for each row."""
    resolved = (
        markers if markers is not None else family_markers(session, workspace_id, pending_ids)
    )
    return {pending_id: resolved[pending_id].review_note for pending_id in pending_ids}


class RelevanceReviewQueries:
    """Project current reviews by joining display identity from its owner."""

    def __init__(self, database: Database) -> None:
        """Bind current-review reads to their database."""
        self.database = database

    def current_for_pending_ids(
        self,
        session: Session,
        workspace_id: str,
        pending_ids: list[str],
        *,
        markers: Mapping[str, PendingMarkers] | None = None,
    ) -> dict[str, RelevanceReviewResponse]:
        """Return the latest review for each requested pending discovery.

        ``markers`` are the caller's resolved family markers for ``pending_ids``, if it has them.
        """
        if not pending_ids:
            return {}
        ranked = (
            select(
                RelevanceReview.id,
                func.row_number()
                .over(
                    partition_by=RelevanceReview.pending_discovery_id,
                    order_by=CURRENT_FIRST,
                )
                .label("position"),
            )
            .where(
                RelevanceReview.workspace_id == workspace_id,
                RelevanceReview.pending_discovery_id.in_(pending_ids),
            )
            .subquery()
        )
        rows = session.execute(
            select(
                RelevanceReview,
                PendingDiscovery.employer,
                PendingDiscovery.title,
                RelevanceReviewDispute,
            )
            .join(ranked, (ranked.c.id == RelevanceReview.id) & (ranked.c.position == 1))
            .join(
                PendingDiscovery,
                PendingDiscovery.id == RelevanceReview.pending_discovery_id,
            )
            .outerjoin(
                RelevanceReviewDispute,
                RelevanceReviewDispute.relevance_review_id == RelevanceReview.id,
            )
            .where(PendingDiscovery.workspace_id == workspace_id)
        ).all()
        inputs = _CurrentInputs(
            current_version_id(session, workspace_id),
            parameter_snapshot_in(session, workspace_id).parameter_set_id,
            current_review_contract(),
        )
        notes = _sent_notes(
            session,
            workspace_id,
            [
                review.pending_discovery_id
                for review, *_ in rows
                if review.recommendation is not None
            ],
            markers,
        )
        return {
            review.pending_discovery_id: self._response(
                review, employer, title, inputs, dispute, notes.get(review.pending_discovery_id)
            )
            for review, employer, title, dispute in rows
        }

    @staticmethod
    def _response(
        review: RelevanceReview,
        employer: str,
        title: str,
        inputs: _CurrentInputs,
        dispute: RelevanceReviewDispute | None,
        keep_note: str | None,
    ) -> RelevanceReviewResponse:
        return RelevanceReviewResponse.model_validate(
            {
                "id": review.id,
                "pending_discovery_id": review.pending_discovery_id,
                "employer": employer,
                "title": title,
                "outcome": "judged"
                if review.recommendation is not None
                else "insufficient_posting_text",
                "recommendation": review.recommendation,
                "summary": review.summary,
                "core_work_alignment": review.core_work_alignment,
                "core_qualification_alignment": review.core_qualification_alignment,
                "hard_condition": review.hard_condition,
                "decisive_basis": review.decisive_basis,
                "matched_exclusion": review.matched_exclusion,
                "posting_citations": review.posting_citations,
                "master_cv_evidence_ids": review.master_cv_evidence_ids,
                "unresolved_questions": review.unresolved_questions,
                "fetched_at": review.fetched_at,
                "reviewed_at": review.reviewed_at,
                "task_id": review.task_id,
                "master_cv_version_id": review.master_cv_version_id,
                "search_parameter_set_id": review.search_parameter_set_id,
                "out_of_date_because": inputs.changed_since(review, keep_note),
                "disputed_at": dispute.updated_at if dispute is not None else None,
                "dispute_note": dispute.note if dispute is not None else None,
            }
        )
