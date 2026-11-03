"""Read the three learning-loop signals the product exposes.

Each signal is one integer numerator over one integer denominator, computed by
rule from durable rows. The service delegates the ranked queue read to
``SourceScanQueries`` so the same ranking the SPA sees is what the missed-match
signal measures against.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from clearscout.application.failures import NotFoundError
from clearscout.application.workspaces.metrics_models import (
    LearningLoopMetricsResponse,
    LearningLoopSignal,
)
from clearscout.domain import SearchProposalState, utc_now
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    DerivedCluster,
    DerivedEmployerArchetype,
    DerivedSearchTitle,
    MasterCvProposal,
    Workspace,
)

if TYPE_CHECKING:
    from clearscout.application.discovery.scan_queries import SourceScanQueries

TOP_N_PENDING = 10

_IDENTITY_ROW_TYPES = (DerivedSearchTitle, DerivedEmployerArchetype, DerivedCluster)


class LearningLoopMetricsService:
    """One snapshot of the three learning-loop signals for one workspace."""

    def __init__(self, database: Database, queries: SourceScanQueries) -> None:
        """Bind the reader to the durable store and the shared queue query facade."""
        self.database = database
        self.queries = queries

    def read(self, workspace_id: str) -> LearningLoopMetricsResponse:
        """Return the three signals for the workspace, refusing an unknown one."""
        with self.database.session() as session:
            if session.get(Workspace, workspace_id) is None:
                raise NotFoundError(f"Unknown workspace {workspace_id!r}.")
            reproposed = self._reproposed(session, workspace_id)
            acceptance = self._master_cv_acceptance(session, workspace_id)
        missed = self._pending_queue_missed(workspace_id)
        return LearningLoopMetricsResponse(
            workspace_id=workspace_id,
            computed_at=utc_now(),
            top_n=TOP_N_PENDING,
            reproposed_rejected_identities=reproposed,
            master_cv_proposal_acceptance=acceptance,
            pending_queue_missed_matches=missed,
        )

    @staticmethod
    def _reproposed(session: Session, workspace_id: str) -> LearningLoopSignal:
        numerator = 0
        denominator = 0
        for row_type in _IDENTITY_ROW_TYPES:
            rejected_identities = set(
                session.scalars(
                    select(row_type.carried_identity).where(
                        row_type.workspace_id == workspace_id,
                        row_type.state == SearchProposalState.REJECTED.value,
                        row_type.carried_identity.is_not(None),
                    )
                ).all()
            )
            proposed_rows = session.scalars(
                select(row_type.carried_identity).where(
                    row_type.workspace_id == workspace_id,
                    row_type.state == SearchProposalState.PROPOSED.value,
                    row_type.carried_identity.is_not(None),
                )
            ).all()
            for identity in proposed_rows:
                denominator += 1
                if identity is not None and identity in rejected_identities:
                    numerator += 1
        return LearningLoopSignal(numerator=numerator, denominator=denominator)

    @staticmethod
    def _master_cv_acceptance(session: Session, workspace_id: str) -> LearningLoopSignal:
        denominator = (
            session.scalar(
                select(func.count())
                .select_from(MasterCvProposal)
                .where(MasterCvProposal.workspace_id == workspace_id)
            )
            or 0
        )
        numerator = (
            session.scalar(
                select(func.count())
                .select_from(MasterCvProposal)
                .where(
                    MasterCvProposal.workspace_id == workspace_id,
                    MasterCvProposal.state == SearchProposalState.ACCEPTED.value,
                )
            )
            or 0
        )
        return LearningLoopSignal(numerator=numerator, denominator=denominator)

    def _pending_queue_missed(self, workspace_id: str) -> LearningLoopSignal:
        from clearscout.application.discovery.missed_requirement_reader import (
            missed_requirement_texts_in,
        )
        from clearscout.application.master_cv.versioning import current_version_id

        pendings = self.queries.list_pending_discoveries(workspace_id, limit=TOP_N_PENDING)
        with self.database.session() as session:
            current_cv_version = current_version_id(session, workspace_id)
            missed_texts = (
                missed_requirement_texts_in(session, workspace_id, current_cv_version)
                if current_cv_version is not None
                else frozenset()
            )
        numerator = 0
        if missed_texts:
            for pending in pendings:
                haystack = f"{pending.title} {pending.location}".casefold()
                if any(text and text in haystack for text in missed_texts):
                    numerator += 1
        return LearningLoopSignal(numerator=numerator, denominator=len(pendings))
