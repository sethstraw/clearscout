"""One pending row ranked, screened, and projected into its review response."""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

from sqlalchemy import ColumnElement, select
from sqlalchemy.orm import Session

from clearscout.application.contracts import PublisherClosingDateResponse
from clearscout.application.discovery.content_resolution import (
    pending_discovery_readiness,
    usable_current_evidence,
)
from clearscout.application.discovery.pending import (
    PendingMarkers,
    dismissed_pending_discovery_ids,
    root_discovery_filters,
)
from clearscout.application.discovery.posting_identity import (
    DuplicateCandidate,
    duplicate_suggestions,
)
from clearscout.application.discovery.ranking import Rank, rank
from clearscout.application.discovery.scan_queries.wording import render_screening
from clearscout.application.discovery.scan_staging import staged_by_campaign, staged_by_person
from clearscout.application.discovery.screening import (
    DiscoveryScreening,
    ScreeningCode,
    ScreeningStanding,
    constraint_confidence,
    resolve_arrangement_categories,
    screen_discovery,
    stated_places,
)
from clearscout.application.discovery.titles import best_title_answer
from clearscout.application.job_search.evaluation_targets import (
    EvaluatedJobMatch,
    evaluated_jobs_for_pending,
)
from clearscout.application.job_search.models import (
    DiscoveryRecommendation,
    PendingDiscoveryResponse,
    PendingDiscoveryStagingProvenance,
    PendingDuplicateSuggestionResponse,
    RelevanceReviewRejectionResponse,
    RelevanceReviewResponse,
    ScreeningFindingResponse,
    StagedListingFactsResponse,
)
from clearscout.domain import PendingDiscoveryReadiness
from clearscout.domain.identity import normalize_text
from clearscout.domain.places import PlaceReader
from clearscout.domain.records import RobotsReadBasis
from clearscout.domain.search_parameters import JobSearchParameters
from clearscout.domain.staged_facts import PublisherClosingDate, StagedListingFacts
from clearscout.infrastructure.persistence.models import (
    PendingDiscovery,
    PendingPostingContent,
    RelevanceReview,
    Source,
)


def _latest_publisher_closing_date(
    staged: StagedListingFacts, current: PendingPostingContent | None
) -> PublisherClosingDateResponse | None:
    fact = staged.closing_date
    if current is not None and current.publisher_closing_date is not None:
        observed = PublisherClosingDate.from_json(current.publisher_closing_date)
        if (
            fact is None
            or fact.observed_at is None
            or fact.observed_at <= (observed.observed_at or current.fetched_at)
        ):
            fact = observed
    return PublisherClosingDateResponse.model_validate(fact.as_json()) if fact is not None else None


def _identity_text(evidence: dict[str, object], field: str) -> str:
    value = evidence.get(field)
    return value.strip() if isinstance(value, str) else ""


def board_employer_of(names: dict[str, str], source_id: str | None) -> str:
    """Return the employer that owns a row's board, or "" when the row has none."""
    return names.get(source_id or "", "")


def _recommendation(
    row: PendingDiscovery,
    screening: DiscoveryScreening,
    parameters: JobSearchParameters,
) -> tuple[DiscoveryRecommendation, str]:
    """Render a free first read from typed screening findings."""
    if row.withdrawn_at is not None:
        return "unlikely", "The source no longer lists this opening."
    return render_screening(screening, parameters)


def ranked_discoveries(
    rows: list[PendingDiscovery],
    parameters: JobSearchParameters,
    searched: list[str],
    missed_texts: frozenset[str],
    missed_penalty: int,
) -> list[tuple[Rank, PendingDiscovery, StagedListingFacts]]:
    """Rank only identity text the publisher supplied, while retaining hints for display."""
    ranked = []
    for row in rows:
        facts = StagedListingFacts.from_json(row.staged_facts)
        trusted = facts.publisher_vouched
        # The stored display names one place, so ranking scores every market the
        # publisher stated, the same set screening reads.
        places = "; ".join(stated_places(facts.locations, row.location))
        ranked.append(
            (
                rank(
                    row.title if trusted else "",
                    places if trusted else "",
                    parameters,
                    searched,
                    missed_requirement_texts=missed_texts,
                    missed_requirement_penalty=missed_penalty,
                ),
                row,
                facts,
            )
        )
    return ranked


def _trusted_title_answer(
    row: PendingDiscovery, facts: StagedListingFacts, searched: list[str]
) -> tuple[str, float] | None:
    """Match a target title only when the publisher supplied the staged identity."""
    if not facts.publisher_vouched:
        return None
    return best_title_answer(row.title, searched)


def duplicate_suggestions_in_session(
    session: Session, workspace_id: str, pending_ids: Sequence[str]
) -> dict[str, list[PendingDuplicateSuggestionResponse]]:
    """Return the likely duplicates of each listed discovery, for a person to confirm."""
    if not pending_ids:
        return {}
    # The queue's own filters minus the dismissal exclusion: a queued row still
    # needs to hear that a copy of this opening was already turned down.
    reviewed = (
        select(RelevanceReview.id)
        .where(RelevanceReview.pending_discovery_id == PendingDiscovery.id)
        .exists()
    )
    rows = session.execute(
        select(PendingDiscovery, PendingPostingContent, Source.source_type, reviewed)
        .outerjoin(
            PendingPostingContent,
            PendingPostingContent.id == PendingDiscovery.current_content_id,
        )
        .outerjoin(Source, Source.id == PendingDiscovery.source_id)
        .where(
            *root_discovery_filters(workspace_id),
            PendingDiscovery.withdrawn_at.is_(None),
        )
    ).all()
    discoveries = {row.id: row for row, _content, _source_type, _review in rows}
    if not discoveries:
        return {}
    evaluated = evaluated_jobs_for_pending(session, workspace_id, discoveries.values())
    dismissed = set(session.scalars(dismissed_pending_discovery_ids(workspace_id)))
    candidates = []
    for row, content, source_type, has_review in rows:
        facts = StagedListingFacts.from_json(row.staged_facts)
        # Usable evidence always carries a digest, so its presence is the retained body.
        retained = (
            content.content_sha256 or ""
            if content is not None and usable_current_evidence(row, content)
            else ""
        )
        candidates.append(
            DuplicateCandidate(
                pending_id=row.id,
                employer=row.employer,
                title=row.title,
                location=row.location,
                stated_places=facts.locations.values,
                urls=(row.canonical_url, row.discovery_url),
                requisition_id=row.requisition_id,
                body_digest=retained,
                publisher_vouched=facts.publisher_vouched,
                retained_body=bool(retained),
                relevance_reviewed=bool(has_review),
                already_evaluated=row.id in evaluated,
                dismissed=row.id in dismissed,
                source_type=source_type or "",
                first_seen_on=row.first_seen_on,
            )
        )
    wanted = set(pending_ids)
    return {
        pending_id: [
            PendingDuplicateSuggestionResponse(
                pending_discovery_id=item.other_id,
                employer=discoveries[item.other_id].employer,
                title=discoveries[item.other_id].title,
                location=discoveries[item.other_id].location,
                discovery_url=discoveries[item.other_id].discovery_url,
                dismissed=item.other_id in dismissed,
                signal=item.signal,
                keeper_pending_discovery_id=item.keeper_id,
                keeper_basis=item.keeper_basis,
            )
            for item in items
        ]
        for pending_id, items in duplicate_suggestions(candidates).items()
        if pending_id in wanted
    }


class Projection(NamedTuple):
    """One pending row with its rank, staged facts, screening, and advice."""

    standing: Rank
    row: PendingDiscovery
    staged_facts: StagedListingFacts
    screening: DiscoveryScreening
    recommendation: DiscoveryRecommendation
    recommendation_because: str


_RECOMMENDATION_ORDER = {"review": 0, "investigate": 1, "unlikely": 2}


def screened_projection(
    ranked: tuple[Rank, PendingDiscovery, StagedListingFacts],
    parameters: JobSearchParameters,
    searched: list[str],
    source_employers: dict[str, str],
) -> Projection:
    """Screen one ranked row and attach its recommendation."""
    standing, row, staged_facts = ranked
    screening = screen_discovery(
        row.employer,
        row.title,
        row.location,
        parameters,
        searched,
        staged_facts,
        source_employer=board_employer_of(source_employers, row.source_id),
    )
    return Projection(
        standing, row, staged_facts, screening, *_recommendation(row, screening, parameters)
    )


def active_list_order(item: Projection) -> tuple[bool, int, int]:
    """Sort withdrawn rows last, then by recommendation and score."""
    return (
        item.row.withdrawn_at is not None,
        _RECOMMENDATION_ORDER[item.recommendation],
        -item.standing.score,
    )


def staging_provenance_of(
    row: PendingDiscovery, campaign_staged: set[str]
) -> PendingDiscoveryStagingProvenance:
    """Name what put this row in the queue, or say that nothing stored records it."""
    if row.id in campaign_staged:
        return "guided_campaign"
    if staged_by_person(row.reason):
        return "person_added"
    if row.source_id:
        return "board_scan"
    return "guided_campaign" if staged_by_campaign(row.reason) else "unrecorded"


def current_content_of(
    contents: dict[str, PendingPostingContent], row: PendingDiscovery
) -> PendingPostingContent | None:
    """Return the row's current content when one is stored."""
    return contents.get(row.current_content_id) if row.current_content_id else None


def current_contents(
    session: Session, workspace_id: str, candidates: list[Projection]
) -> dict[str, PendingPostingContent]:
    """Load the current content of every candidate in one query."""
    content_ids = [
        item.row.current_content_id for item in candidates if item.row.current_content_id
    ]
    if not content_ids:
        return {}
    return {
        item.id: item
        for item in session.scalars(
            select(PendingPostingContent).where(
                PendingPostingContent.workspace_id == workspace_id,
                PendingPostingContent.id.in_(content_ids),
            )
        )
    }


def queue_readiness(
    projection: Projection,
    content: PendingPostingContent | None,
    cut_off: frozenset[str] = frozenset(),
) -> PendingDiscoveryReadiness:
    """Keep content failures first, then a current screening refusal, then cut-off text."""
    readiness = pending_discovery_readiness(projection.row, content)
    if readiness is not PendingDiscoveryReadiness.READY:
        return readiness
    if projection.screening.standing is ScreeningStanding.REFUSE:
        return PendingDiscoveryReadiness.SCREENING_REFUSED
    if content is not None and content.id in cut_off:
        return PendingDiscoveryReadiness.PARTIAL_TEXT
    return readiness


def readiness_cohort_page(
    screened: list[Projection],
    contents: dict[str, PendingPostingContent],
    ready_limit: int,
    attention_limit: int,
    cut_off: frozenset[str],
) -> list[Projection]:
    """Bound each readiness lane separately, ready rows first."""
    ready: list[Projection] = []
    attention: list[Projection] = []
    for candidate in screened:
        readiness = queue_readiness(candidate, current_content_of(contents, candidate.row), cut_off)
        (ready if readiness is PendingDiscoveryReadiness.READY else attention).append(candidate)
    # Each lane owns its bound, so unread rows cannot consume the page
    # reserved for candidates a paid review can actually use.
    return [*ready[:ready_limit], *attention[:attention_limit]]


def regional_places(
    session: Session,
    workspace_id: str,
    rows: list[PendingDiscovery],
    *variant_filters: ColumnElement[bool],
) -> dict[str, list[str]]:
    """Map each root to the other locations its unarchived regional variants name."""
    if not rows:
        return {}
    variants = select(PendingDiscovery.regional_variant_of, PendingDiscovery.location).where(
        PendingDiscovery.workspace_id == workspace_id,
        PendingDiscovery.regional_variant_of.in_([row.id for row in rows]),
        PendingDiscovery.archived_at.is_(None),
        *variant_filters,
    )
    places = {row.id: normalize_text(row.location) for row in rows}
    elsewhere: dict[str, list[str]] = {}
    for kept, location in session.execute(variants):
        if kept is not None and location and normalize_text(location) != places.get(kept):
            elsewhere.setdefault(kept, []).append(location)
    return elsewhere


def pending_response(
    projection: Projection,
    searched: list[str],
    match: EvaluatedJobMatch | None,
    elsewhere: list[str],
    current_content: PendingPostingContent | None,
    relevance_review: RelevanceReviewResponse | None = None,
    markers: PendingMarkers | None = None,
    duplicate_suggestions: list[PendingDuplicateSuggestionResponse] | None = None,
    board_employer: str = "",
    *,
    staging_provenance: PendingDiscoveryStagingProvenance = "unrecorded",
    relevance_review_rejection: RelevanceReviewRejectionResponse | None = None,
    place_reader: PlaceReader,
    cut_off: frozenset[str] = frozenset(),
) -> PendingDiscoveryResponse:
    """Build the wire response for one projected pending row."""
    standing, row, staged_facts, screening, recommendation, recommendation_because = projection
    markers = markers or PendingMarkers()
    title_answer = _trusted_title_answer(row, staged_facts, searched)
    arrangement_result, arrangement_source = resolve_arrangement_categories(
        staged_facts.arrangements, row.location, row.discovery_url
    )
    identity_evidence = current_content.identity_evidence if current_content is not None else {}
    retained = (
        current_content
        if current_content is not None and usable_current_evidence(row, current_content)
        else None
    )
    return PendingDiscoveryResponse(
        id=row.id,
        employer=row.employer,
        title=row.title,
        location=row.location,
        discovery_url=row.discovery_url,
        canonical_url=row.canonical_url,
        content_status=row.content_status,
        readiness=queue_readiness(projection, current_content, cut_off),
        retained_content_id=row.current_content_id if retained is not None else None,
        retained_content_artifact_id=retained.artifact_id if retained is not None else None,
        countries=list(place_reader.read(row.location).countries),
        current_content_id=row.current_content_id,
        current_posting_status=current_content.posting_status if current_content else None,
        current_content_observed_at=current_content.fetched_at if current_content else None,
        publisher_closing_date=_latest_publisher_closing_date(staged_facts, current_content),
        closing_date_override=row.closing_date_override,
        current_identity_status=current_content.identity_status if current_content else None,
        current_extraction_method=(current_content.extraction_method if current_content else None),
        current_robots_basis=(
            RobotsReadBasis(current_content.robots_basis)
            if current_content is not None and current_content.robots_basis
            else None
        ),
        observed_publisher_employer=_identity_text(identity_evidence, "publisher_employer"),
        observed_publisher_title=_identity_text(identity_evidence, "publisher_title"),
        board_employer=(
            board_employer
            if board_employer and normalize_text(board_employer) != normalize_text(row.employer)
            else None
        ),
        listing_artifact_id=row.listing_artifact_id,
        content_note=row.content_note,
        reason=row.reason,
        staging_provenance=staging_provenance,
        first_seen_on=row.first_seen_on,
        favorite=markers.favorite,
        kept=markers.kept,
        keep_note=markers.keep_note,
        rank=standing.score,
        ranked_because=list(standing.because),
        matched_target_title=title_answer[0] if title_answer is not None else None,
        recommendation=recommendation,
        recommendation_because=recommendation_because,
        constraint_confidence=constraint_confidence(screening),
        screening_standing=screening.standing,
        screening_findings=[
            ScreeningFindingResponse(
                dimension=finding.dimension,
                standing=finding.standing,
                code=finding.code,
                subject=(
                    f"{'; '.join(staged_facts.locations.values) or row.location} "
                    "(geography only; work arrangement checked separately)"
                    if finding.code is ScreeningCode.LOCATION_MATCH
                    else finding.subject
                ),
            )
            for finding in screening.findings
        ],
        staged_facts=StagedListingFactsResponse.model_validate(staged_facts.as_json()),
        arrangement_categories=list(arrangement_result),
        arrangement_categories_source=arrangement_source,
        also_in=sorted(elsewhere),
        withdrawn_at=row.withdrawn_at,
        evaluated_job_id=match.job_posting_id if match is not None else None,
        evaluated_evaluation_id=match.evaluation_id if match is not None else None,
        evaluated_at=match.evaluated_at if match is not None else None,
        evaluated_decision=match.decision if match is not None else None,
        relevance_review=relevance_review,
        relevance_review_rejection=relevance_review_rejection,
        duplicate_suggestions=list(duplicate_suggestions or []),
    )
