"""The review queue's filters, lanes, facets, and snapshot digest."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple, get_args

from sqlalchemy import ColumnElement, or_

from clearscout.application.discovery.pending import PendingMarkers
from clearscout.application.discovery.scan_queries.projection import (
    Projection,
    active_list_order,
    queue_readiness,
    staging_provenance_of,
)
from clearscout.application.discovery.screening import (
    ArrangementCategory,
    ConstraintConfidence,
    constraint_confidence,
    resolve_arrangement_categories,
)
from clearscout.application.failures import ValidationError
from clearscout.application.job_search.models import (
    DiscoveryRecommendation,
    JobEmployerFacetResponse,
    PendingArrangementFacetResponse,
    PendingCategoryFacetResponse,
    PendingConstraintFacetResponse,
    PendingConstraintRoute,
    PendingDiscoveryFacetsResponse,
    PendingDiscoveryStagingProvenance,
    PendingMarkerFacetResponse,
    PendingProvenanceFacetResponse,
    PendingReadinessFacetResponse,
    PendingRelevanceFacetResponse,
    PendingRelevanceKey,
    PendingReviewStatus,
    PendingReviewStatusFacetResponse,
    RelevanceReviewRejectionResponse,
    RelevanceReviewResponse,
    RelevanceReviewStaleInput,
)
from clearscout.domain import PendingDiscoveryReadiness
from clearscout.domain.identity import normalize_text
from clearscout.domain.search_parameters import JobSearchParameters
from clearscout.infrastructure.persistence.models import PendingDiscovery, PendingPostingContent

# The page read ranks every visible row whatever the limit, so only the projected
# rows scale with it; this ceiling keeps one response bounded while "all" stays true.
PENDING_PAGE_MAX_LIMIT = 2000


_GROUPED_CONSTRAINT_ROUTES = frozenset({"pursue_only", "needs_clarification", "excluded_only"})


# Every phrase folds against every visible row, so one request states a ceiling.
MAX_COLLATERAL_PHRASES = 250


def validate_pending_page_request(
    ready_offset: int,
    attention_offset: int,
    limit: int,
    expected_snapshot_sha256: str | None,
    snapshot_before: datetime | None,
) -> None:
    """Reject malformed lane offsets and incomplete continuation snapshots."""
    if limit < 1 or limit > PENDING_PAGE_MAX_LIMIT:
        raise ValidationError(
            f"Pending discovery limit must be between 1 and {PENDING_PAGE_MAX_LIMIT}."
        )
    if ready_offset < 0 or attention_offset < 0:
        raise ValidationError("Pending discovery offsets cannot be negative.")
    if (ready_offset or attention_offset) and (
        expected_snapshot_sha256 is None or snapshot_before is None
    ):
        raise ValidationError(
            "A pending discovery snapshot and boundary are required after the first page."
        )


@dataclass(frozen=True)
class PendingQueueFilters:
    """What one queue page narrows to before paging; an unset field narrows nothing."""

    readiness: PendingDiscoveryReadiness | None = None
    relevance: PendingRelevanceKey | None = None
    review_status: PendingReviewStatus | None = None
    constraint: PendingConstraintRoute | None = None
    category: DiscoveryRecommendation | None = None
    arrangement: ArrangementCategory | None = None
    provenance: PendingDiscoveryStagingProvenance | None = None
    favorite: bool = False
    kept: bool = False
    search: str = ""
    employers: tuple[str, ...] = ()


class QueueContext(NamedTuple):
    """The per-row facts a queue page projects with."""

    markers: dict[str, PendingMarkers]
    reviews: dict[str, RelevanceReviewResponse]
    rejections: dict[str, RelevanceReviewRejectionResponse]
    campaign_staged: set[str]
    cut_off: frozenset[str] = frozenset()


class _QueueFacts(NamedTuple):
    readiness: PendingDiscoveryReadiness
    relevance: PendingRelevanceKey
    out_of_date: bool
    review_status: tuple[PendingReviewStatus, ...]
    constraint: ConstraintConfidence
    category: DiscoveryRecommendation
    arrangement: tuple[ArrangementCategory, ...]
    provenance: PendingDiscoveryStagingProvenance
    markers: PendingMarkers
    employer: str
    employer_key: str


class _ScreenedQueue(NamedTuple):
    lanes: dict[bool, list[str]]
    # Every candidate the search kept, with the names of the filters it failed.
    rows: list[tuple[_QueueFacts, frozenset[str]]]


class ScreenedRead(NamedTuple):
    """The candidates and parameters one screened queue read used."""

    candidates: Sequence[tuple[PendingDiscovery, PendingPostingContent | None]]
    parameters: JobSearchParameters
    searched: list[str]
    source_employers: dict[str, str]
    context: QueueContext
    projections: dict[str, Projection]
    screened: _ScreenedQueue


_STALE_STATUS: dict[RelevanceReviewStaleInput, PendingReviewStatus] = {
    "master_cv": "out_of_date_master_cv",
    "search_parameters": "out_of_date_search_parameters",
    "review_instructions": "out_of_date_review_instructions",
    "keep_note": "out_of_date_keep_note",
}


def search_filters(search: str) -> list[ColumnElement[bool]]:
    """Build the SQL conditions for a free-text queue search."""
    term = search.strip()
    if not term:
        return []
    columns = (PendingDiscovery.employer, PendingDiscovery.title, PendingDiscovery.location)
    return [or_(*(column.icontains(term, autoescape=True) for column in columns))]


def _relevance_key(
    review: RelevanceReviewResponse | None, rejection: RelevanceReviewRejectionResponse | None
) -> PendingRelevanceKey:
    if review is None:
        return "not_reviewed" if rejection is None else "review_rejected"
    if review.disputed_at is not None:
        return "disputed"
    if review.outcome == "judged" and review.recommendation is not None:
        return review.recommendation
    return "insufficient_posting_text"


def _review_statuses(review: RelevanceReviewResponse | None) -> tuple[PendingReviewStatus, ...]:
    if review is None:
        return ()
    if not review.out_of_date_because:
        return ("current",)
    return ("out_of_date", *(_STALE_STATUS[item] for item in review.out_of_date_because))


def _queue_facts(
    projection: Projection, content: PendingPostingContent | None, context: QueueContext
) -> _QueueFacts:
    row = projection.row
    review = context.reviews.get(row.id)
    arrangement, _source = resolve_arrangement_categories(
        projection.staged_facts.arrangements, row.location, row.discovery_url
    )
    return _QueueFacts(
        readiness=queue_readiness(projection, content, context.cut_off),
        relevance=_relevance_key(review, context.rejections.get(row.id)),
        out_of_date=review is not None and bool(review.out_of_date_because),
        review_status=_review_statuses(review),
        constraint=constraint_confidence(projection.screening),
        category=projection.recommendation,
        arrangement=arrangement,
        provenance=staging_provenance_of(row, context.campaign_staged),
        markers=context.markers[row.id],
        employer=row.employer.strip(),
        employer_key=normalize_text(row.employer),
    )


def _route_admits(route: PendingConstraintRoute | None, constraint: ConstraintConfidence) -> bool:
    if route is None:
        return True
    if route == "not_excluded":
        return constraint is not ConstraintConfidence.EXCLUDED
    if route == "pursue_only":
        return constraint is ConstraintConfidence.PURSUE
    if route == "excluded_only":
        return constraint is ConstraintConfidence.EXCLUDED
    if route == "needs_clarification":
        return constraint in (
            ConstraintConfidence.INVESTIGATE,
            ConstraintConfidence.NEEDS_VERIFICATION,
            ConstraintConfidence.UNCERTAIN_COMP,
        )
    return constraint == route


def _markers_admit(filters: PendingQueueFilters, markers: PendingMarkers) -> bool:
    if not (filters.favorite or filters.kept):
        return True
    return (filters.favorite and markers.favorite) or (filters.kept and markers.kept)


def _failed_filters(
    facts: _QueueFacts, filters: PendingQueueFilters, employer_keys: frozenset[str]
) -> frozenset[str]:
    marked = facts.markers.marked and filters.constraint not in _GROUPED_CONSTRAINT_ROUTES
    checks = {
        "readiness": marked or filters.readiness in (None, facts.readiness),
        "relevance": filters.relevance in (None, facts.relevance),
        "review_status": filters.review_status in (None, *facts.review_status),
        "constraint": marked or _route_admits(filters.constraint, facts.constraint),
        "category": filters.category in (None, facts.category),
        "arrangement": filters.arrangement in (None, *facts.arrangement),
        "provenance": filters.provenance in (None, facts.provenance),
        "markers": _markers_admit(filters, facts.markers),
        "employer": not employer_keys or facts.employer_key in employer_keys,
    }
    return frozenset(name for name, passed in checks.items() if not passed)


def screened_queue(
    projections_by_id: dict[str, Projection],
    content_by_pending: dict[str, PendingPostingContent | None],
    context: QueueContext,
    filters: PendingQueueFilters,
) -> _ScreenedQueue:
    """Split the rows every filter keeps into ordered lanes, keeping each row's failures."""
    employer_keys = frozenset(filter(None, (normalize_text(name) for name in filters.employers)))
    ordered: dict[bool, list[tuple[int, bool, int, int, str]]] = {True: [], False: []}
    rows: list[tuple[_QueueFacts, frozenset[str]]] = []
    for pending_id, projection in projections_by_id.items():
        facts = _queue_facts(projection, content_by_pending[pending_id], context)
        failed = _failed_filters(facts, filters, employer_keys)
        rows.append((facts, failed))
        if not failed:
            ordered[facts.readiness is PendingDiscoveryReadiness.READY].append(
                (facts.markers.rank, *active_list_order(projection), pending_id)
            )
    lanes = {lane: [item[-1] for item in sorted(entries)] for lane, entries in ordered.items()}
    return _ScreenedQueue(lanes=lanes, rows=rows)


def snapshot_digest(lanes: dict[bool, list[str]]) -> str:
    """Digest the ready and attention lane order."""
    order_document = {"ready": lanes[True], "attention": lanes[False]}
    return hashlib.sha256(
        json.dumps(order_document, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def _counted(screened: _ScreenedQueue, dimension: str | tuple[str, ...]) -> list[_QueueFacts]:
    """Return rows passing every filter except the requested dimensions."""
    ignored = {dimension} if isinstance(dimension, str) else set(dimension)
    return [facts for facts, failed in screened.rows if failed <= ignored]


def queue_facets(
    screened: _ScreenedQueue, filters: PendingQueueFilters
) -> PendingDiscoveryFacetsResponse:
    """Count each filter choice over the complete matching queue."""
    readiness_rows = _counted(screened, "readiness")
    constraint_rows = _counted(screened, ("constraint", "readiness"))
    relevance_rows = _counted(screened, "relevance")
    relevance = Counter(facts.relevance for facts in relevance_rows)
    stale = Counter(facts.relevance for facts in relevance_rows if facts.out_of_date)
    status_rows = _counted(screened, "review_status")
    statuses = Counter(status for facts in status_rows for status in facts.review_status)
    category = Counter(facts.category for facts in _counted(screened, "category"))
    arrangement_rows = _counted(screened, "arrangement")
    arrangement = Counter(value for facts in arrangement_rows for value in facts.arrangement)
    provenance = Counter(facts.provenance for facts in _counted(screened, "provenance"))
    marker_rows = _counted(screened, "markers")
    return PendingDiscoveryFacetsResponse(
        readiness=[
            PendingReadinessFacetResponse(
                value=value,
                count=sum(
                    (facts.markers.marked and filters.constraint not in _GROUPED_CONSTRAINT_ROUTES)
                    or facts.readiness is value
                    for facts in readiness_rows
                ),
                holding=sum(facts.readiness is value for facts in readiness_rows),
            )
            for value in PendingDiscoveryReadiness
        ],
        relevance=[
            PendingRelevanceFacetResponse(
                value=value, count=relevance[value], out_of_date=stale[value]
            )
            for value in get_args(PendingRelevanceKey)
        ],
        review_status=[
            PendingReviewStatusFacetResponse(value=value, count=statuses[value])
            for value in get_args(PendingReviewStatus)
        ],
        review_status_total=len(status_rows),
        constraint=[
            PendingConstraintFacetResponse(
                value=route,
                count=sum(
                    (facts.markers.marked and route not in _GROUPED_CONSTRAINT_ROUTES)
                    or (
                        _route_admits(route, facts.constraint)
                        and filters.readiness in (None, facts.readiness)
                    )
                    for facts in constraint_rows
                ),
            )
            for route in (
                "not_excluded",
                "pursue_only",
                "needs_clarification",
                "excluded_only",
                *ConstraintConfidence,
            )
        ],
        constraint_total=sum(
            facts.markers.marked or filters.readiness in (None, facts.readiness)
            for facts in constraint_rows
        ),
        category=[
            PendingCategoryFacetResponse(value=value, count=category[value])
            for value in get_args(DiscoveryRecommendation)
        ],
        arrangement=[
            PendingArrangementFacetResponse(value=value, count=arrangement[value])
            for value in get_args(ArrangementCategory)
        ],
        arrangement_total=len(arrangement_rows),
        provenance=[
            PendingProvenanceFacetResponse(value=value, count=provenance[value])
            for value in get_args(PendingDiscoveryStagingProvenance)
        ],
        markers=PendingMarkerFacetResponse(
            favorite=sum(facts.markers.favorite for facts in marker_rows),
            kept=sum(facts.markers.kept for facts in marker_rows),
        ),
    )


def employer_facets(screened: _ScreenedQueue) -> list[JobEmployerFacetResponse]:
    """Count rows per normalized employer, named by its most common spelling."""
    spellings: dict[str, Counter[str]] = {}
    for facts in _counted(screened, "employer"):
        if facts.employer_key:
            spellings.setdefault(facts.employer_key, Counter())[facts.employer] += 1
    facets = [
        JobEmployerFacetResponse(
            name=min(names.items(), key=lambda item: (-item[1], item[0]))[0],
            count=sum(names.values()),
        )
        for names in spellings.values()
    ]
    return sorted(facets, key=lambda facet: (-facet.count, facet.name))
