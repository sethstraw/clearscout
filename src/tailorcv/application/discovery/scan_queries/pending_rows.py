"""Pending rows read one at a time or as a bounded list."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from tailorcv.application.discovery.content_resolution import (
    CurrentPostingContentReader,
    retained_text_cut_off,
    too_short_reviews,
)
from tailorcv.application.discovery.missed_requirement_reader import missed_requirement_texts_in
from tailorcv.application.discovery.pending import (
    PendingMarkers,
    campaign_staged_ids,
    dismissed_pending_discovery_ids,
    family_markers,
    listed_pending_discovery_filters,
    source_employer_names,
    undismissed_root_filters,
)
from tailorcv.application.discovery.proposals import accepted_titles
from tailorcv.application.discovery.relevance_review_queries import RelevanceReviewQueries
from tailorcv.application.discovery.relevance_review_rejections import rejections_for_pending_ids
from tailorcv.application.discovery.scan_queries.projection import (
    Projection,
    active_list_order,
    board_employer_of,
    current_content_of,
    current_contents,
    duplicate_suggestions_in_session,
    pending_response,
    ranked_discoveries,
    readiness_cohort_page,
    regional_places,
    screened_projection,
    staging_provenance_of,
)
from tailorcv.application.discovery.scan_queries.queue import QueueContext
from tailorcv.application.discovery.scan_queries.workspace import require_workspace_in
from tailorcv.application.failures import NotFoundError, ValidationError
from tailorcv.application.job_search.evaluation_targets import (
    EvaluatedJobMatch,
    evaluated_jobs_for_pending,
)
from tailorcv.application.job_search.models import (
    PendingDiscoveryResponse,
    PendingDuplicateSuggestionResponse,
    RelevanceReviewRejectionResponse,
    RelevanceReviewResponse,
)
from tailorcv.application.master_cv.versioning import current_version_id
from tailorcv.application.workspaces.parameters import parameters_in
from tailorcv.application.workspaces.policy import LearningLoopPolicyService
from tailorcv.domain.identity import normalize_text
from tailorcv.domain.places import PlaceReader
from tailorcv.domain.search_parameters import JobSearchParameters
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    Artifact,
    PendingDiscovery,
    PendingPostingContent,
    Workspace,
)
from tailorcv.shared.errors import TailorcvError


class PendingRowQueries:
    """Project pending rows into review responses, one row or one bounded list at a time."""

    def __init__(
        self, database: Database, content_reader: CurrentPostingContentReader | None
    ) -> None:
        """Bind row reads to the database and, when bodies are retained, their reader."""
        self.database = database
        self.content_reader = content_reader

    def cut_off_content_ids(
        self,
        session: Session,
        workspace_id: str,
        contents: Iterable[PendingPostingContent | None],
    ) -> frozenset[str]:
        """Return the current contents whose retained text reads as a fragment."""
        workspace = session.get(Workspace, workspace_id)
        retained = {
            content.artifact_id: content
            for content in contents
            if content is not None and content.artifact_id and content.content_sha256
        }
        if self.content_reader is None or workspace is None or not retained:
            return frozenset()
        too_short = too_short_reviews(session, workspace_id)
        artifacts = session.scalars(select(Artifact).where(Artifact.id.in_(retained)))
        flagged: set[str] = set()
        for artifact in artifacts:
            content = retained[artifact.id]
            if not too_short.may_flag(artifact.byte_size, content.content_sha256):
                continue
            try:
                body = self.content_reader.verified_body(artifact, workspace, content)
            except (TailorcvError, OSError):
                continue
            if retained_text_cut_off(body, content.extraction_method, too_short.digests):
                flagged.add(content.id)
        return frozenset(flagged)

    @staticmethod
    def _validate_list_limits(
        limit: int, closed_only: bool, ready_limit: int | None, attention_limit: int | None
    ) -> None:
        """Refuse list bounds the queue read cannot honor."""
        if limit < 1 or limit > 250:
            raise ValidationError("Pending discovery limit must be between 1 and 250.")
        if closed_only and ready_limit is not None:
            raise ValidationError("Closed posting history cannot be paged by readiness cohort.")
        if (ready_limit is None) != (attention_limit is None):
            raise ValidationError(
                "Ready and attention limits must be supplied together for cohort paging."
            )
        if ready_limit is not None and not 1 <= ready_limit <= 250:
            raise ValidationError("Ready pending discovery limit must be between 1 and 250.")
        if attention_limit is not None and not 1 <= attention_limit <= 250:
            raise ValidationError("Attention pending discovery limit must be between 1 and 250.")

    @staticmethod
    def _pending_list_query(
        workspace_id: str,
        dismissed: Select[tuple[str]],
        *,
        limit: int,
        closed_only: bool,
    ) -> Select[tuple[PendingDiscovery]]:
        """Select undismissed root rows: closed history newest first, or the active list."""
        if not closed_only:
            return (
                select(PendingDiscovery)
                .where(*listed_pending_discovery_filters(workspace_id, dismissed))
                .order_by(PendingDiscovery.first_seen_on, PendingDiscovery.employer)
            )
        return (
            select(PendingDiscovery)
            .where(
                *undismissed_root_filters(workspace_id, dismissed),
                PendingDiscovery.withdrawn_at.is_not(None),
            )
            .order_by(PendingDiscovery.withdrawn_at.desc(), PendingDiscovery.id)
            .limit(limit)
        )

    def list_pending_discoveries(
        self,
        workspace_id: str,
        *,
        limit: int = 50,
        closed_only: bool = False,
        ready_limit: int | None = None,
        attention_limit: int | None = None,
    ) -> list[PendingDiscoveryResponse]:
        """Return one active page, the newest closed postings, or bounded readiness cohorts."""
        self._validate_list_limits(limit, closed_only, ready_limit, attention_limit)
        with self.database.session() as session:
            require_workspace_in(session, workspace_id)
            dismissed = dismissed_pending_discovery_ids(workspace_id)
            rows = list(
                session.scalars(
                    self._pending_list_query(
                        workspace_id, dismissed, limit=limit, closed_only=closed_only
                    )
                )
            )
            parameters, searched, missed_texts, missed_penalty = self.pending_context(
                session, workspace_id
            )
            ranked = ranked_discoveries(rows, parameters, searched, missed_texts, missed_penalty)
            source_employers = source_employer_names(session, (row.source_id for row in rows))
            screened = [
                screened_projection(item, parameters, searched, source_employers) for item in ranked
            ]
            if not closed_only:
                ranks = family_markers(session, workspace_id, [item.row.id for item in screened])
                screened.sort(key=lambda item: (ranks[item.row.id].rank, *active_list_order(item)))
            page = screened if ready_limit is not None else screened[:limit]
            contents = current_contents(session, workspace_id, page)
            cut_off = self.cut_off_content_ids(session, workspace_id, contents.values())
            if ready_limit is not None and attention_limit is not None:
                page = readiness_cohort_page(
                    screened, contents, ready_limit, attention_limit, cut_off
                )
            page_rows = [item.row for item in page]
            page_ids = [row.id for row in page_rows]
            markers = family_markers(session, workspace_id, page_ids)
            reviews, rejections = self.relevance_facts(session, workspace_id, page_ids, markers)
            open_only = () if closed_only else (PendingDiscovery.withdrawn_at.is_(None),)
            elsewhere = regional_places(
                session, workspace_id, page_rows, PendingDiscovery.id.not_in(dismissed), *open_only
            )
            evaluated = evaluated_jobs_for_pending(session, workspace_id, page_rows)
            staged_by_campaign = campaign_staged_ids(session, page_ids)
            place_reader = parameters.place_reader
            return [
                pending_response(
                    item,
                    searched,
                    evaluated.get(item.row.id),
                    elsewhere.get(item.row.id, []),
                    current_content_of(contents, item.row),
                    reviews.get(item.row.id),
                    markers=markers[item.row.id],
                    board_employer=board_employer_of(source_employers, item.row.source_id),
                    staging_provenance=staging_provenance_of(item.row, staged_by_campaign),
                    relevance_review_rejection=rejections.get(item.row.id),
                    place_reader=place_reader,
                    cut_off=cut_off,
                )
                for item in page
            ]

    def pending_responses_for_rows(
        self,
        session: Session,
        workspace_id: str,
        selected: list[tuple[PendingDiscovery, PendingPostingContent | None]],
        *,
        searched: list[str],
        projections_by_id: dict[str, Projection],
        context: QueueContext,
        suggestions: dict[str, list[PendingDuplicateSuggestionResponse]],
        source_employers: dict[str, str],
        place_reader: PlaceReader,
    ) -> list[PendingDiscoveryResponse]:
        """Project only an already bounded database page into queue responses."""
        rows = [row for row, _content in selected]
        if not rows:
            return []
        content_by_pending = {row.id: content for row, content in selected}
        evaluated = evaluated_jobs_for_pending(session, workspace_id, rows)
        elsewhere = regional_places(
            session, workspace_id, rows, PendingDiscovery.withdrawn_at.is_(None)
        )
        return [
            pending_response(
                item,
                searched,
                evaluated.get(item.row.id),
                elsewhere.get(item.row.id, []),
                content_by_pending.get(item.row.id),
                context.reviews.get(item.row.id),
                context.markers[item.row.id],
                suggestions.get(item.row.id, []),
                board_employer_of(source_employers, item.row.source_id),
                staging_provenance=staging_provenance_of(item.row, context.campaign_staged),
                relevance_review_rejection=context.rejections.get(item.row.id),
                place_reader=place_reader,
                cut_off=context.cut_off,
            )
            for item in (projections_by_id[row.id] for row in rows)
        ]

    def get_pending_discovery(
        self,
        workspace_id: str,
        pending_id: str,
        *,
        session: Session | None = None,
    ) -> PendingDiscoveryResponse:
        """Project one pending discovery, comparing it with every root for duplicate suggestions.

        A caller inside a write transaction passes its session to read its own
        uncommitted mutation. Raises NotFoundError for a missing or foreign row.
        """
        if session is not None:
            return self._project_pending_discovery(session, workspace_id, pending_id)
        with self.database.session() as owned:
            return self._project_pending_discovery(owned, workspace_id, pending_id)

    def _project_pending_discovery(
        self, session: Session, workspace_id: str, pending_id: str
    ) -> PendingDiscoveryResponse:
        """Build one PendingDiscoveryResponse using shared queue context."""
        require_workspace_in(session, workspace_id)
        row = session.scalar(
            select(PendingDiscovery).where(
                PendingDiscovery.workspace_id == workspace_id,
                PendingDiscovery.id == pending_id,
                PendingDiscovery.archived_at.is_(None),
            )
        )
        if row is None:
            raise NotFoundError(f"Pending discovery '{pending_id}' does not exist.")
        parameters, searched, missed_texts, missed_penalty = self.pending_context(
            session, workspace_id
        )
        source_employers = source_employer_names(session, [row.source_id])
        projection = screened_projection(
            ranked_discoveries([row], parameters, searched, missed_texts, missed_penalty)[0],
            parameters,
            searched,
            source_employers,
        )
        markers = family_markers(session, workspace_id, [row.id])
        reviews, rejections = self.relevance_facts(session, workspace_id, [row.id], markers)
        match: EvaluatedJobMatch | None = evaluated_jobs_for_pending(
            session, workspace_id, [row]
        ).get(row.id)
        elsewhere: list[str] = []
        if row.regional_variant_of is None:
            siblings = session.scalars(
                select(PendingDiscovery).where(
                    PendingDiscovery.workspace_id == workspace_id,
                    PendingDiscovery.regional_variant_of == row.id,
                    PendingDiscovery.archived_at.is_(None),
                )
            )
            here = normalize_text(row.location)
            for sibling in siblings:
                if sibling.location and normalize_text(sibling.location) != here:
                    elsewhere.append(sibling.location)
        content = (
            session.get(PendingPostingContent, row.current_content_id)
            if row.current_content_id
            else None
        )
        return pending_response(
            projection,
            searched,
            match,
            elsewhere,
            content,
            reviews.get(row.id),
            markers=markers[row.id],
            duplicate_suggestions=duplicate_suggestions_in_session(
                session, workspace_id, [row.id]
            ).get(row.id, []),
            board_employer=board_employer_of(source_employers, row.source_id),
            staging_provenance=staging_provenance_of(row, campaign_staged_ids(session, [row.id])),
            relevance_review_rejection=rejections.get(row.id),
            place_reader=parameters.place_reader,
            cut_off=self.cut_off_content_ids(session, workspace_id, [content]),
        )

    def relevance_facts(
        self,
        session: Session,
        workspace_id: str,
        pending_ids: list[str],
        markers: Mapping[str, PendingMarkers],
    ) -> tuple[dict[str, RelevanceReviewResponse], dict[str, RelevanceReviewRejectionResponse]]:
        """Resolve the stored review and the rejection mark for a whole page in two reads."""
        return (
            RelevanceReviewQueries(self.database).current_for_pending_ids(
                session, workspace_id, pending_ids, markers=markers
            ),
            rejections_for_pending_ids(session, workspace_id, pending_ids),
        )

    def pending_context(
        self, session: Session, workspace_id: str
    ) -> tuple[JobSearchParameters, list[str], frozenset[str], int]:
        # Ranking reads the stated clusters; accepting a suggestion adds it to that
        # list, so nothing is widened past the cap the person never ordered here.
        """Read the search parameters, searched titles, and missed-term facts."""
        parameters = parameters_in(session, workspace_id)
        searched = [
            *parameters.seniority.stated_titles,
            *(title.title for title in accepted_titles(session, workspace_id)),
        ]
        current_cv_version = current_version_id(session, workspace_id)
        missed_texts = (
            missed_requirement_texts_in(session, workspace_id, current_cv_version)
            if current_cv_version is not None
            else frozenset()
        )
        missed_penalty = (
            LearningLoopPolicyService(self.database)
            .current_in_session(session, workspace_id)
            .missed_requirement_penalty
        )
        return parameters, searched, missed_texts, missed_penalty
