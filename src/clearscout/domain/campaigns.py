"""Closed vocabularies for a guided campaign's durable execution records."""

from __future__ import annotations

from enum import StrEnum


class CampaignStatus(StrEnum):
    """Where a campaign's execution stands."""

    RUNNING = "running"
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    PAUSED_UNCERTAIN = "paused_uncertain"
    CANCELLED = "cancelled"
    FAILED = "failed"


class AttemptState(StrEnum):
    """Where one grouped provider call stands."""

    PLANNED = "planned"
    DISPATCHING = "dispatching"
    DISPATCHED = "dispatched"
    CAPTURED = "captured"
    PROCESSED = "processed"
    UNCERTAIN = "uncertain"
    ABANDONED = "abandoned"


class BranchOutcome(StrEnum):
    """What the captured queries show about one branch."""

    SEARCHED = "searched"
    TRUNCATED = "truncated"
    UNVISITED = "unvisited"
    UNATTRIBUTABLE = "unattributable"


class CapturedUrlClassification(StrEnum):
    """The shape of a captured URL, decided before any fetch."""

    ATS_DETAIL = "ats_detail"
    JOB_PAGE_CANDIDATE = "job_page_candidate"
    BOARD = "board"
    OTHER = "other"


class CapturedUrlOutcome(StrEnum):
    """What processing decided about one captured URL."""

    PENDING_READ = "pending_read"
    STAGED = "staged"
    KNOWN_POSTING = "known_posting"
    REGISTERED_BOARD = "registered_board"
    BOARD_ADMITTED = "board_admitted"
    SCREENED_OUT = "screened_out"
    HELD_OUT = "held_out"
    UNREADABLE = "unreadable"
    POLICY_REFUSED = "policy_refused"
    UNRESOLVED = "unresolved"


class StagedBasis(StrEnum):
    """Which program rule staged a captured posting."""

    TITLE_EVIDENCE = "title_evidence"
    BODY_FOCUS_TERM = "body_focus_term"
    MODEL_SELECTED = "model_selected"


class UrlActionKind(StrEnum):
    """How a URL reached the capture: a provider action, or only the model's answer."""

    SEARCH_SOURCE = "search_source"
    OPEN_PAGE = "open_page"
    FIND_IN_PAGE = "find_in_page"
    MODEL_WRITTEN = "model_written"


class CampaignReason(StrEnum):
    """Why a campaign ended incomplete, or what its report must warn about."""

    INCOMPLETE_ATTEMPTS = "incomplete_attempts"
    ABANDONED_ATTEMPT = "abandoned_attempt"
    UNCERTAIN_ATTEMPT = "uncertain_attempt"
    TRUNCATED_RESPONSE = "truncated_response"
    SEARCH_OVERRUN = "search_overrun"
    PROTECTED_FAMILY_UNSEARCHED = "protected_family_unsearched"
    SUPERSEDED = "superseded"


class AttemptErrorCategory(StrEnum):
    """Why an attempt's dispatch, poll, or local processing failed, as the report shows it."""

    CAPACITY = "capacity"
    NOT_CREATED = "not_created"
    RESPONSE_GONE = "response_gone"
    RATE_LIMITED = "rate_limited"
    OVERLOADED = "overloaded"
    PROVIDER_ERROR = "provider_error"
    PROCESSING_FAILED = "processing_failed"


CAMPAIGN_STATUSES = tuple(item.value for item in CampaignStatus)
ATTEMPT_ERROR_CATEGORIES = tuple(item.value for item in AttemptErrorCategory)
ATTEMPT_STATES = tuple(item.value for item in AttemptState)
BRANCH_OUTCOMES = tuple(item.value for item in BranchOutcome)
CAPTURED_URL_CLASSIFICATIONS = tuple(item.value for item in CapturedUrlClassification)
CAPTURED_URL_OUTCOMES = tuple(item.value for item in CapturedUrlOutcome)
STAGED_BASES = tuple(item.value for item in StagedBasis)
URL_ACTION_KINDS = tuple(item.value for item in UrlActionKind)
