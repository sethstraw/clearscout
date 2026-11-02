"""Stable identifiers and closed vocabularies shared by all adapters."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4


class ApplicationStatus(StrEnum):
    """A status recorded by one append-only application event."""

    NOT_APPLIED = "not_applied"
    PACKAGE_READY = "package_ready"
    APPLIED = "applied"
    INTERVIEWING = "interviewing"
    OFFER = "offer"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    DECLINED = "declined"
    CLOSED_BEFORE_APPLICATION = "closed_before_application"


class EvaluationDecision(StrEnum):
    """A completed evaluation's recommendation after hard gates."""

    APPLY_NOW = "apply_now"
    PREMIUM_TARGET = "premium_target"
    INVESTIGATE = "investigate"
    MONITOR = "monitor"
    DEPRIORITIZE = "deprioritize"
    INELIGIBLE = "ineligible"
    CLOSED_EVIDENCE = "closed_evidence"


class TaskStatus(StrEnum):
    """Durable task lifecycle shared by research and pipeline work."""

    QUEUED = "queued"
    RUNNING = "running"
    NEEDS_REVIEW = "needs_review"
    COMMITTING = "committing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PostingStatus(StrEnum):
    """Observed availability of a posting."""

    ACTIVE = "active"
    CLOSED = "closed"


class PostingContentAccessStatus(StrEnum):
    """Current ability to obtain a discovered posting's evaluable content."""

    READABLE = "readable"
    NOT_YET_READ = "not_yet_read"
    ACCESS_BLOCKED = "access_blocked"
    SESSION_REQUIRED = "session_required"
    RENDER_REQUIRED = "render_required"
    POLICY_REFUSED = "policy_refused"
    FETCH_FAILED = "fetch_failed"


class PostingRefreshOutcome(StrEnum):
    """What one refresh pass concluded about a posting it selected."""

    REFRESHED = "refreshed"
    WITHDRAWN_AS_CLOSED = "withdrawn_as_closed"
    UNVERIFIED = "unverified"
    NOT_CHECKABLE = "not_checkable"
    SKIPPED = "skipped"
    FAILED = "failed"


class PostingIdentityStatus(StrEnum):
    """Confidence that fetched evidence identifies the discovered posting."""

    NOT_YET_VERIFIED = "not_yet_verified"
    PUBLISHER_LISTED = "publisher_listed"
    VERIFIED = "verified"
    AMBIGUOUS = "ambiguous"
    CONTRADICTED = "contradicted"


class PostingContentExtractionMethod(StrEnum):
    """Deterministic acquisition path that produced posting content."""

    STATIC_STRUCTURED = "static_structured"
    STATIC_HTML = "static_html"
    RENDERED_HTML = "rendered_html"
    RENDERED_JSON = "rendered_json"
    USER_SUPPLIED_TEXT = "user_supplied_text"


class PostingReadIntent(StrEnum):
    """Why a page is requested: part of a crawl, or one posting a person would open."""

    CRAWL = "crawl"
    SINGLE_POSTING = "single_posting"


class RobotsReadBasis(StrEnum):
    """Why robots policy let a posting page be read."""

    ALLOWED = "allowed"
    SINGLE_POSTING_READ = "single_posting_read"


class PageClassification(StrEnum):
    """A publisher page's observed role, without assuming a board vendor."""

    POSTING = "posting"
    COLLECTION = "collection"
    NAVIGATION = "navigation"
    APPLICATION_CHANNEL = "application_channel"
    ERROR_OR_CHALLENGE = "error_or_challenge"
    JS_SHELL = "js_shell"
    AMBIGUOUS = "ambiguous"


class PendingDiscoveryReadiness(StrEnum):
    """Why a pending discovery is ready or needs a specific user action."""

    READY = "ready"
    SCREENING_REFUSED = "screening_refused"
    NO_BODY = "no_body"
    ACCESS_BLOCKED = "access_blocked"
    FETCH_FAILED = "fetch_failed"
    RENDER_REQUIRED = "render_required"
    SESSION_REQUIRED = "session_required"
    POLICY_REFUSED = "policy_refused"
    IDENTITY_NOT_VERIFIED = "identity_not_verified"
    IDENTITY_AMBIGUOUS = "identity_ambiguous"
    IDENTITY_CONTRADICTED = "identity_contradicted"
    URL_MISMATCH = "url_mismatch"
    EVIDENCE_INCOMPLETE = "evidence_incomplete"
    PARTIAL_TEXT = "partial_text"
    WITHDRAWN = "withdrawn"


class SourceReadOutcome(StrEnum):
    """How a source read produced postings or reached a bounded stopping point."""

    NOT_RECORDED = "not_recorded"
    ATS_COMPLETE = "ats_complete"
    PUBLISHER_INDEX = "publisher_index"
    PUBLISHER_LINKED_PAGES = "publisher_linked_pages"
    CRAWL_CONTINUATION = "crawl_continuation"
    ACCESS_BLOCKED = "access_blocked"
    SESSION_REQUIRED = "session_required"
    RENDER_REQUIRED = "render_required"
    POLICY_REFUSED = "policy_refused"
    NO_SUPPORTED_REPRESENTATION = "no_supported_representation"
    FETCH_FAILED = "fetch_failed"


class TailoringOutcome(StrEnum):
    """Durable outcome of one tailoring run."""

    PASSED = "passed"
    INCOMPLETE = "incomplete"
    CHECKS_FAILED = "checks failed"


class EvidenceClassification(StrEnum):
    """How candidate evidence relates to one requirement."""

    DIRECT = "DIRECT"
    TRANSFERABLE = "TRANSFERABLE"
    GAP = "GAP"
    TAILORING_ONLY = "TAILORING_ONLY"
    ASSERTED = "ASSERTED"


class EvidenceStatus(StrEnum):
    """How well a piece of evidence is established. Internal, and never on the wire."""

    VERIFIED = "verified"
    CORROBORATED = "corroborated"
    SELF_ATTESTED = "self_attested"
    UNRESOLVED = "unresolved"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"


# A self-attested fact answers a requirement and cites no achievement, so it
# stays out of this set.
CITABLE_EVIDENCE = frozenset({EvidenceStatus.VERIFIED, EvidenceStatus.CORROBORATED})


class ClaimBoundary(StrEnum):
    """Why the evidence a claim rests on does not carry it."""

    PROTOTYPE = "prototype"
    EXPLORATORY = "exploratory"
    FAMILIARITY = "familiarity"
    ADJACENT = "adjacent"
    PRODUCTION = "production"
    FORMAL_TITLE_OR_TENURE = "formal_title_or_tenure"
    TEAM_CONTRIBUTION = "team_contribution"
    INDIVIDUAL_OWNERSHIP = "individual_ownership"
    PLANNED_NOT_SHIPPED = "planned_not_shipped"
    MISCITED = "miscited"
    ABSENT_FROM_RECORD = "absent_from_record"


class GapClass(StrEnum):
    """Why a requirement went unanswered, separating fixable gaps from permanent ones."""

    INCOMPLETE_EVIDENCE = "incomplete_evidence"
    UNCLEAR_POSITIONING = "unclear_positioning"
    BOUNDED_LEARNABLE = "bounded_learnable"
    PERMANENT_FORMAL = "permanent_formal"
    GEOGRAPHY_EMPLOYMENT = "geography_employment"
    UNSUPPORTED_CORE = "unsupported_core"


class EvidenceStrength(StrEnum):
    """Strength assigned to one evidence relationship."""

    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"
    NONE = "none"


class EvidenceSourceKind(StrEnum):
    """Durable source side for an evidence anchor."""

    POSTING = "posting"
    CANDIDATE = "candidate"


class EvidenceDirection(StrEnum):
    """Direction in which evidence affects a finding."""

    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    LIMITS = "limits"


class ScoreComponent(StrEnum):
    """Independently supported score component."""

    FIT = "fit"
    COMPENSATION = "compensation"
    ELIGIBILITY = "eligibility"
    EVIDENCE = "evidence"
    OPPORTUNITY = "opportunity"


class FindingState(StrEnum):
    """Whether a scored fact is supported, absent, or conflicting."""

    SUPPORTED = "supported"
    NOT_STATED = "not_stated"
    CONFLICT = "conflict"


class SpanStatus(StrEnum):
    """Whether a quoted span was found in the source it cites."""

    VERIFIED = "verified"
    NEEDS_REVIEW = "needs_review"


# A ruling here is accept or reject; a resume line's ``ApprovalDecisionValue`` can also replace.
class SearchProposalState(StrEnum):
    """Whether the user has accepted or rejected a search proposal, or not yet ruled on it."""

    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class ProposalRejectionReason(StrEnum):
    """Why a person rejected a proposal; any but duplicate or defer is ruled out of a campaign."""

    NOT_RELEVANT = "not_relevant"
    WRONG_LEVEL = "wrong_level"
    WRONG_MANDATE = "wrong_mandate"
    WRONG_EMPLOYER_TYPE = "wrong_employer_type"
    DUPLICATE = "duplicate"
    DEFER = "defer"
    OTHER = "other"


PROPOSAL_REJECTION_REASONS: tuple[str, ...] = tuple(item.value for item in ProposalRejectionReason)


class SourceKind(StrEnum):
    """Whose openings a source lists; only an employer board's complete read shows a pulled one."""

    EMPLOYER_BOARD = "employer_board"
    AGGREGATOR = "aggregator"


class DuplicateSuggestionSignal(StrEnum):
    """Which deterministic rule linked two discoveries as likely the same opening."""

    RETAINED_BODY_DIGEST = "retained_body_digest"
    TITLE_AND_EMPLOYER = "title_and_employer"
    TITLE_AND_PLACE_HINT = "title_and_place_hint"


class DuplicateKeeperBasis(StrEnum):
    """Which ordering rule separates the discovery to keep from the one it is compared with."""

    IN_QUEUE = "in_queue"
    ALREADY_EVALUATED = "already_evaluated"
    EMPLOYER_BOARD_SOURCE = "employer_board_source"
    PUBLISHER_VOUCHED = "publisher_vouched"
    RELEVANCE_REVIEWED = "relevance_reviewed"
    RETAINED_BODY = "retained_body"
    FIRST_SEEN = "first_seen"
    IDENTIFIER_ORDER = "identifier_order"


class ExplicitDismissalCategory(StrEnum):
    """Why a person explicitly removed a posting from the pending queue."""

    CLOSED_AT_SOURCE = "closed_at_source"
    OUT_OF_SCOPE_TITLE = "out_of_scope_title"
    OUT_OF_SCOPE_LOCATION = "out_of_scope_location"
    OUT_OF_SCOPE_ARRANGEMENT = "out_of_scope_arrangement"
    OUT_OF_SCOPE_TRAVEL = "out_of_scope_travel"
    OUT_OF_SCOPE_COMPENSATION = "out_of_scope_compensation"
    INVALID_PAGE = "invalid_page"
    DUPLICATE_PENDING = "duplicate_pending"
    DUPLICATE_POSTING = "duplicate_posting"
    EMPLOYER_EXCLUDED = "employer_excluded"
    CURRENT_EVIDENCE_UNAVAILABLE = "current_evidence_unavailable"
    OTHER = "other"


EXPLICIT_DISMISSAL_REASONS: dict[ExplicitDismissalCategory, str] = {
    ExplicitDismissalCategory.CLOSED_AT_SOURCE: "The posting is closed at its source.",
    ExplicitDismissalCategory.OUT_OF_SCOPE_TITLE: (
        "The title is outside the current search scope."
    ),
    ExplicitDismissalCategory.OUT_OF_SCOPE_LOCATION: (
        "The location is outside the current search scope."
    ),
    ExplicitDismissalCategory.OUT_OF_SCOPE_ARRANGEMENT: (
        "The work arrangement is outside the current search scope."
    ),
    ExplicitDismissalCategory.OUT_OF_SCOPE_TRAVEL: (
        "The travel requirement is outside the current search scope."
    ),
    ExplicitDismissalCategory.OUT_OF_SCOPE_COMPENSATION: (
        "The compensation is outside the current search scope."
    ),
    ExplicitDismissalCategory.INVALID_PAGE: "The page is not a job posting.",
    ExplicitDismissalCategory.DUPLICATE_PENDING: (
        "The posting duplicates another pending discovery."
    ),
    ExplicitDismissalCategory.DUPLICATE_POSTING: (
        "The posting duplicates another tracked opportunity."
    ),
    ExplicitDismissalCategory.EMPLOYER_EXCLUDED: (
        "The employer is excluded from the current search."
    ),
    ExplicitDismissalCategory.CURRENT_EVIDENCE_UNAVAILABLE: (
        "The posting was not evaluated: I chose to dismiss it because trustworthy current "
        "posting evidence remained unavailable."
    ),
}


class FactorType(StrEnum):
    """Decision-factor grouping used in an evaluation."""

    PRO = "pro"
    CON = "con"
    GAP = "gap"
    RISK = "risk"


class ApplicationEventType(StrEnum):
    """Append-only application-history operation."""

    STATUS = "status"
    CORRECTION = "correction"
    REVERSAL = "reversal"


APPLICATION_STATUSES = tuple(item.value for item in ApplicationStatus)

# Once one of these has been recorded, a new plain package-ready event would
# describe preparation as though it happened after the application progressed.
PROGRESSED_APPLICATION_STATUSES = (
    ApplicationStatus.APPLIED.value,
    ApplicationStatus.INTERVIEWING.value,
    ApplicationStatus.OFFER.value,
    ApplicationStatus.REJECTED.value,
    ApplicationStatus.WITHDRAWN.value,
    ApplicationStatus.DECLINED.value,
    ApplicationStatus.CLOSED_BEFORE_APPLICATION.value,
)

# Which statuses mean a job is still being pursued is one product rule, asked by
# the command center's count and by the pipeline board's columns.
PURSUED_STATUSES = (
    ApplicationStatus.PACKAGE_READY.value,
    ApplicationStatus.APPLIED.value,
    ApplicationStatus.INTERVIEWING.value,
    ApplicationStatus.OFFER.value,
)
EVALUATION_DECISIONS = tuple(item.value for item in EvaluationDecision)
POSTING_STATUSES = tuple(item.value for item in PostingStatus)
POSTING_CONTENT_ACCESS_STATUSES = tuple(item.value for item in PostingContentAccessStatus)
POSTING_CONTENT_EXTRACTION_METHODS = tuple(item.value for item in PostingContentExtractionMethod)
ROBOTS_READ_BASES = tuple(item.value for item in RobotsReadBasis)
POSTING_IDENTITY_STATUSES = tuple(item.value for item in PostingIdentityStatus)
SCORE_COMPONENTS = tuple(item.value for item in ScoreComponent)
SEARCH_PROPOSAL_STATES = tuple(item.value for item in SearchProposalState)
SOURCE_KINDS = tuple(item.value for item in SourceKind)
SOURCE_READ_OUTCOMES = tuple(item.value for item in SourceReadOutcome)
TAILORING_OUTCOMES = tuple(item.value for item in TailoringOutcome)
TASK_STATUSES = tuple(item.value for item in TaskStatus)


def new_id() -> str:
    """Return a collision-safe, storage-neutral identifier."""
    return str(uuid4())


def utc_now() -> datetime:
    """Return one timezone-aware UTC instant."""
    return datetime.now(UTC)
