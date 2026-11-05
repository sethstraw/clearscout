"""Posting identity and the append-only record of what each read observed.

A posting's stable facts and its verifications are deliberately separate: the
identity survives, and every later read is another observation rather than an
edit to the first one.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any, Literal, cast

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SQLColumnExpression,
    String,
    Table,
    Text,
    UniqueConstraint,
    delete,
    event,
    select,
    text,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from clearscout.domain import (
    POSTING_CONTENT_ACCESS_STATUSES,
    POSTING_CONTENT_EXTRACTION_METHODS,
    POSTING_IDENTITY_STATUSES,
    POSTING_STATUSES,
    PROPOSAL_REJECTION_REASONS,
    ROBOTS_READ_BASES,
    SEARCH_PROPOSAL_STATES,
    SOURCE_KINDS,
    SOURCE_READ_OUTCOMES,
    PostingContentAccessStatus,
    PostingContentExtractionMethod,
    PostingIdentityStatus,
    PostingStatus,
    SearchProposalState,
    SourceReadOutcome,
    utc_now,
)
from clearscout.domain.campaigns import (
    ATTEMPT_ERROR_CATEGORIES,
    ATTEMPT_STATES,
    BRANCH_OUTCOMES,
    CAMPAIGN_STATUSES,
    CAPTURED_URL_CLASSIFICATIONS,
    CAPTURED_URL_OUTCOMES,
    STAGED_BASES,
    URL_ACTION_KINDS,
)
from clearscout.domain.search_parameters import CLUSTER_KINDS, LANES
from clearscout.infrastructure.persistence.models.base import (
    AuditMixin,
    Base,
    IdMixin,
    SoftArchiveMixin,
    _allowed,
)
from clearscout.infrastructure.persistence.types import UTCDateTime


class Employer(IdMixin, AuditMixin, SoftArchiveMixin, Base):
    """Normalized employer identity."""

    __tablename__ = "employers"
    __table_args__ = (UniqueConstraint("workspace_id", "normalized_name", name="uq_employer"),)

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(300), nullable=False)
    careers_url: Mapped[str | None] = mapped_column(Text)


class JobPosting(IdMixin, AuditMixin, SoftArchiveMixin, Base):
    """Stable job identity; later verification belongs in observations."""

    __tablename__ = "job_postings"
    __table_args__ = (
        Index("ix_job_workspace_title", "workspace_id", "title"),
        Index("ix_job_employer", "employer_id"),
    )

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    employer_id: Mapped[str] = mapped_column(ForeignKey("employers.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    normalized_title: Mapped[str] = mapped_column(String(500), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    posting_hint: Mapped[str | None] = mapped_column(Text)
    closing_date_override: Mapped[date | None] = mapped_column(Date)
    closing_date_override_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class PostingIdentifier(IdMixin, AuditMixin, Base):
    """Alternate requisition, URL, or board identifier for one posting."""

    __tablename__ = "posting_identifiers"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "identifier_type", "identifier_value", name="uq_posting_identifier"
        ),
    )

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    job_posting_id: Mapped[str] = mapped_column(ForeignKey("job_postings.id"), nullable=False)
    identifier_type: Mapped[str] = mapped_column(String(80), nullable=False)
    identifier_value: Mapped[str] = mapped_column(Text, nullable=False)


class PostingUrl(IdMixin, AuditMixin, SoftArchiveMixin, Base):
    """A discovery or employer URL associated with a posting."""

    __tablename__ = "posting_urls"
    __table_args__ = (UniqueConstraint("job_posting_id", "normalized_url", name="uq_posting_url"),)

    job_posting_id: Mapped[str] = mapped_column(ForeignKey("job_postings.id"), nullable=False)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.id"))
    url_type: Mapped[str] = mapped_column(String(40), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_url: Mapped[str] = mapped_column(Text, nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class PostingObservation(IdMixin, AuditMixin, Base):
    """Append-only verification of status, location, and posting content."""

    __tablename__ = "posting_observations"
    __table_args__ = (
        Index("ix_observation_job_observed", "job_posting_id", "observed_at"),
        CheckConstraint(_allowed("posting_status", POSTING_STATUSES), name="ck_posting_status"),
        CheckConstraint(
            "http_status IS NULL OR http_status BETWEEN 100 AND 599",
            name="ck_observation_http_status",
        ),
        CheckConstraint(
            "robots_basis IS NULL OR " + _allowed("robots_basis", ROBOTS_READ_BASES),
            name="ck_observation_robots_basis",
        ),
    )

    job_posting_id: Mapped[str] = mapped_column(ForeignKey("job_postings.id"), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    posting_status: Mapped[str] = mapped_column(String(40), nullable=False)
    location: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    residence_rule: Mapped[str] = mapped_column(Text, nullable=False, default="")
    employment_type: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    payroll_arrangement: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    extraction_method: Mapped[str | None] = mapped_column(String(80))
    fetched_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    http_status: Mapped[int | None] = mapped_column(Integer)
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    # Null when no page body was read or the read predates robots provenance.
    robots_basis: Mapped[str | None] = mapped_column(String(40))
    publisher_closing_date: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class PostingObservationDiff(IdMixin, AuditMixin, Base):
    """Structured immutable comparison between two posting observations."""

    __tablename__ = "posting_observation_diffs"
    __table_args__ = (
        UniqueConstraint("from_observation_id", "to_observation_id", name="uq_observation_diff"),
        Index("ix_observation_diff_job", "job_posting_id", "created_at"),
    )

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    job_posting_id: Mapped[str] = mapped_column(ForeignKey("job_postings.id"), nullable=False)
    from_observation_id: Mapped[str] = mapped_column(
        ForeignKey("posting_observations.id"), nullable=False
    )
    to_observation_id: Mapped[str] = mapped_column(
        ForeignKey("posting_observations.id"), nullable=False
    )
    changes: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    body_diff: Mapped[str] = mapped_column(Text, nullable=False, default="")


class PendingDiscovery(IdMixin, AuditMixin, SoftArchiveMixin, Base):
    """Mutable discovery awaiting enough evidence for an evaluation."""

    __tablename__ = "pending_discoveries"
    __table_args__ = (
        UniqueConstraint("workspace_id", "fingerprint", name="uq_pending_fingerprint"),
        UniqueConstraint("workspace_id", "id", name="uq_pending_workspace_id"),
        CheckConstraint(
            _allowed("content_status", POSTING_CONTENT_ACCESS_STATUSES),
            name="ck_pending_content_status",
        ),
        CheckConstraint("keep_note IS NULL OR kept", name="ck_pending_keep_note_needs_keep"),
        ForeignKeyConstraint(
            ["workspace_id", "id", "current_content_id"],
            [
                "pending_posting_contents.workspace_id",
                "pending_posting_contents.pending_discovery_id",
                "pending_posting_contents.id",
            ],
            name="fk_pending_current_content",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    employer: Mapped[str] = mapped_column(String(300), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    location: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    staged_facts: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    requisition_id: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    discovery_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content_status: Mapped[PostingContentAccessStatus] = mapped_column(
        String(40),
        nullable=False,
        default=PostingContentAccessStatus.NOT_YET_READ,
        server_default=PostingContentAccessStatus.NOT_YET_READ.value,
    )
    content_note: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    current_content_id: Mapped[str | None] = mapped_column(String(36))
    # The board-published description captured at discovery, stored in full as a
    # posting.listing.text artifact so an unread row still carries evidence.
    listing_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    first_seen_on: Mapped[date] = mapped_column(Date, nullable=False)
    # The source that last listed this opening, and so the only one whose
    # complete read may withdraw it. Null when a search staged it directly.
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.id"))
    # The same opening advertised in another region.
    regional_variant_of: Mapped[str | None] = mapped_column(ForeignKey("pending_discoveries.id"))
    # Set when evidence closes this opening and cleared if a later active read confirms its return.
    withdrawn_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # Set when a complete multi-page read first missed this row; the next such miss withdraws it.
    absent_since: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # A person's markers; either one holds the row against every dismissal they did not confirm.
    favorite: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    kept: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    keep_note: Mapped[str | None] = mapped_column(String(300))
    closing_date_override: Mapped[date | None] = mapped_column(Date)
    closing_date_override_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class PendingPostingContent(IdMixin, AuditMixin, Base):
    """Append-only evidence from one pending-posting resolution attempt."""

    __tablename__ = "pending_posting_contents"
    __table_args__ = (
        CheckConstraint(
            _allowed("content_status", POSTING_CONTENT_ACCESS_STATUSES),
            name="ck_pending_posting_content_status",
        ),
        CheckConstraint(
            _allowed("identity_status", POSTING_IDENTITY_STATUSES),
            name="ck_pending_posting_identity_status",
        ),
        CheckConstraint(
            f"extraction_method IS NULL OR "
            f"{_allowed('extraction_method', POSTING_CONTENT_EXTRACTION_METHODS)}",
            name="ck_pending_content_extraction_method",
        ),
        CheckConstraint(
            f"posting_status IS NULL OR {_allowed('posting_status', POSTING_STATUSES)}",
            name="ck_pending_content_posting_status",
        ),
        CheckConstraint(
            "http_status IS NULL OR http_status BETWEEN 100 AND 599",
            name="ck_pending_content_http_status",
        ),
        CheckConstraint(
            "length(requested_url_sha256) = 64 AND "
            "(final_url_sha256 IS NULL OR length(final_url_sha256) = 64) AND "
            "(content_sha256 IS NULL OR length(content_sha256) = 64)",
            name="ck_pending_content_digest_lengths",
        ),
        CheckConstraint(
            "(final_url IS NULL AND final_url_sha256 IS NULL) OR "
            "(length(trim(final_url)) > 0 AND final_url_sha256 IS NOT NULL)",
            name="ck_pending_content_final_url_digest",
        ),
        CheckConstraint("length(trim(requested_url)) > 0", name="ck_pending_content_requested_url"),
        CheckConstraint(
            "length(identity_evidence) <= 16384", name="ck_pending_content_identity_evidence"
        ),
        CheckConstraint(
            "robots_basis IS NULL OR " + _allowed("robots_basis", ROBOTS_READ_BASES),
            name="ck_pending_content_robots_basis",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "pending_discovery_id"],
            ["pending_discoveries.workspace_id", "pending_discoveries.id"],
            name="fk_pending_content_owner",
        ),
        UniqueConstraint(
            "workspace_id",
            "pending_discovery_id",
            "id",
            name="uq_pending_content_owner_id",
        ),
        UniqueConstraint("supersedes_content_id", name="uq_pending_content_supersedes"),
        Index("ix_pending_content_history", "pending_discovery_id", "fetched_at", "created_at"),
    )

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    pending_discovery_id: Mapped[str] = mapped_column(String(36), nullable=False)
    artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    requested_url: Mapped[str] = mapped_column(Text, nullable=False)
    final_url: Mapped[str | None] = mapped_column(Text)
    requested_url_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    final_url_sha256: Mapped[str | None] = mapped_column(String(64))
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    # The body digest taken with this row's own staged places removed, so copies of
    # one opening fold. Null on a row stored before the read that computes it.
    place_blind_digest: Mapped[str | None] = mapped_column(String(12))
    content_status: Mapped[PostingContentAccessStatus] = mapped_column(String(40), nullable=False)
    identity_status: Mapped[PostingIdentityStatus] = mapped_column(
        String(40),
        nullable=False,
        default=PostingIdentityStatus.NOT_YET_VERIFIED,
        server_default=PostingIdentityStatus.NOT_YET_VERIFIED.value,
    )
    posting_status: Mapped[PostingStatus | None] = mapped_column(String(20))
    extraction_method: Mapped[PostingContentExtractionMethod | None] = mapped_column(String(40))
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    publisher_markup_present: Mapped[bool | None] = mapped_column(Boolean)
    identity_evidence: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default="{}"
    )
    supersedes_content_id: Mapped[str | None] = mapped_column(
        ForeignKey("pending_posting_contents.id")
    )
    # Null when no page body was read or the attempt predates robots provenance.
    robots_basis: Mapped[str | None] = mapped_column(String(40))
    publisher_closing_date: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class PendingDiscoveryDispositionEvent(IdMixin, AuditMixin, Base):
    """Append-only human disposition of one pending discovery."""

    __tablename__ = "pending_discovery_disposition_events"
    __table_args__ = (
        CheckConstraint("action IN ('dismiss', 'restore')", name="ck_pending_disposition_action"),
        CheckConstraint(
            "(action = 'dismiss' AND restore_provenance IS NULL) OR "
            "(action = 'restore' AND restore_provenance IS NOT NULL "
            "AND restore_provenance IN ('person', 'audit'))",
            name="ck_pending_disposition_restore_provenance",
        ),
        CheckConstraint(
            "duplicate_of_pending_id IS NULL OR "
            "(action = 'dismiss' AND selection_mode = 'explicit' "
            "AND category = 'duplicate_pending' "
            "AND duplicate_of_pending_id != pending_discovery_id)",
            name="ck_pending_disposition_pending_duplicate",
        ),
        CheckConstraint(
            "duplicate_of_job_id IS NULL OR "
            "(action = 'dismiss' AND selection_mode = 'explicit' "
            "AND category = 'duplicate_posting')",
            name="ck_pending_disposition_job_duplicate",
        ),
        CheckConstraint(
            "duplicate_of_job_id IS NULL OR duplicate_of_pending_id IS NULL",
            name="ck_pending_disposition_one_duplicate_reference",
        ),
        CheckConstraint(
            "action != 'dismiss' OR selection_mode != 'explicit' OR "
            "category != 'duplicate_pending' OR duplicate_of_pending_id IS NOT NULL",
            name="ck_pending_disposition_pending_duplicate_required",
        ),
        CheckConstraint(
            "action != 'dismiss' OR selection_mode != 'explicit' OR "
            "category != 'duplicate_posting' OR duplicate_of_job_id IS NOT NULL",
            name="ck_pending_disposition_job_duplicate_required",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "duplicate_of_pending_id"],
            ["pending_discoveries.workspace_id", "pending_discoveries.id"],
            name="fk_pending_disposition_duplicate_workspace",
        ),
        UniqueConstraint("supersedes_event_id", name="uq_pending_disposition_supersedes"),
        Index("ix_pending_disposition_latest", "pending_discovery_id", "created_at"),
    )

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    pending_discovery_id: Mapped[str] = mapped_column(
        ForeignKey("pending_discoveries.id"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    restore_provenance: Mapped[Literal["person", "audit"] | None] = mapped_column(String(20))
    category: Mapped[str] = mapped_column(String(120), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    duplicate_of_job_id: Mapped[str | None] = mapped_column(ForeignKey("job_postings.id"))
    duplicate_of_pending_id: Mapped[str | None] = mapped_column(String(36))
    selection_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    search_parameter_set_id: Mapped[str | None] = mapped_column(
        ForeignKey("job_search_parameter_sets.id")
    )
    screening: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    supersedes_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("pending_discovery_disposition_events.id")
    )


class RelevanceReview(IdMixin, AuditMixin, Base):
    """One validated relevance review; the latest per pending discovery is current.

    Reviews last while the posting body and title are unchanged; a change to
    either deletes the row's reviews.
    """

    __tablename__ = "relevance_reviews"
    __table_args__ = (
        Index("ix_relevance_review_latest", "pending_discovery_id", "reviewed_at"),
        ForeignKeyConstraint(
            ["workspace_id", "pending_discovery_id"],
            ["pending_discoveries.workspace_id", "pending_discoveries.id"],
            name="fk_relevance_review_pending_workspace",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "task_id"],
            ["background_tasks.workspace_id", "background_tasks.id"],
            name="fk_relevance_review_task_workspace",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "search_parameter_set_id"],
            ["job_search_parameter_sets.workspace_id", "job_search_parameter_sets.id"],
            name="fk_relevance_review_parameters_workspace",
        ),
        CheckConstraint(
            "recommendation IN ('review', 'investigate', 'unlikely', 'insufficient_evidence')",
            name="ck_relevance_review_recommendation",
        ),
        CheckConstraint(
            "core_work_alignment IN ('direct', 'transferable', 'absent', 'unresolved')",
            name="ck_relevance_review_core_work",
        ),
        CheckConstraint(
            "core_qualification_alignment IN "
            "('established', 'partially_established', 'absent', 'unresolved')",
            name="ck_relevance_review_core_qualification",
        ),
        CheckConstraint(
            "hard_condition IN ('passes', 'conflicts', 'unresolved')",
            name="ck_relevance_review_hard_condition",
        ),
        CheckConstraint(
            "decisive_basis IN ('core_match', "
            "'transferable_match', 'core_gap', 'hard_conflict', 'logistics_unknown', "
            "'insufficient_posting')",
            name="ck_relevance_review_decisive_basis",
        ),
        # All five are NULL for an answer that the posting text was too thin to judge.
        CheckConstraint(
            "(recommendation IS NULL AND core_work_alignment IS NULL "
            "AND core_qualification_alignment IS NULL AND hard_condition IS NULL "
            "AND decisive_basis IS NULL) OR (recommendation IS NOT NULL "
            "AND core_work_alignment IS NOT NULL AND core_qualification_alignment IS NOT NULL "
            "AND hard_condition IS NOT NULL AND decisive_basis IS NOT NULL)",
            name="ck_relevance_review_classes_together",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    pending_discovery_id: Mapped[str] = mapped_column(String(36), nullable=False)
    task_id: Mapped[str] = mapped_column(String(36), nullable=False)
    master_cv_version_id: Mapped[str] = mapped_column(
        ForeignKey("master_cv_versions.id"), nullable=False
    )
    search_parameter_set_id: Mapped[str] = mapped_column(String(36), nullable=False)
    recommendation: Mapped[str | None] = mapped_column(String(40))
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    core_work_alignment: Mapped[str | None] = mapped_column(String(40))
    core_qualification_alignment: Mapped[str | None] = mapped_column(String(40))
    hard_condition: Mapped[str | None] = mapped_column(String(40))
    decisive_basis: Mapped[str | None] = mapped_column(String(40))
    posting_citations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    master_cv_evidence_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    unresolved_questions: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    source_url_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    search_context_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    # NULL on a review recorded before these existed, which reads as out of date.
    instructions_digest: Mapped[str | None] = mapped_column(String(64))
    schema_digest: Mapped[str | None] = mapped_column(String(64))
    # The listed exclusion condition a hard_conflict named; NULL on every other basis.
    matched_exclusion: Mapped[str | None] = mapped_column(Text)
    # Digest of the family keep note sent with the review; NULL means no note was sent.
    keep_note_sha256: Mapped[str | None] = mapped_column(String(64))


class RelevanceReviewDispute(IdMixin, AuditMixin, Base):
    """A workspace judgment setting aside the model's verdict on one review.

    Mutable state: one row per pending discovery, replaced when the workspace
    disputes a newer review and removed when it withdraws the dispute, with
    every change written to ``audit_events``. The row names the review it
    disputes, so a later review is served as the model wrote it.
    """

    __tablename__ = "relevance_review_disputes"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "pending_discovery_id", name="uq_relevance_review_dispute_pending"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "pending_discovery_id"],
            ["pending_discoveries.workspace_id", "pending_discoveries.id"],
            name="fk_relevance_review_dispute_pending_workspace",
        ),
        CheckConstraint(
            "disposition IN ('set_aside')",
            name="ck_relevance_review_dispute_vocab",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    pending_discovery_id: Mapped[str] = mapped_column(String(36), nullable=False)
    relevance_review_id: Mapped[str] = mapped_column(
        ForeignKey("relevance_reviews.id"), nullable=False
    )
    disposition: Mapped[str] = mapped_column(String(24), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class RelevanceReviewRejection(IdMixin, AuditMixin, Base):
    """A paid review of one discovery that local validation refused to record.

    Mutable state: one row per pending discovery, replaced when a later paid
    review is rejected again and removed when one is accepted, with every
    change written to ``audit_events``. It is machine-written, and it carries
    no re-run of its own: spending again is the workspace's decision.
    """

    __tablename__ = "relevance_review_rejections"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "pending_discovery_id", name="uq_relevance_review_rejection_pending"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "pending_discovery_id"],
            ["pending_discoveries.workspace_id", "pending_discoveries.id"],
            name="fk_relevance_review_rejection_pending_workspace",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "task_id"],
            ["background_tasks.workspace_id", "background_tasks.id"],
            name="fk_relevance_review_rejection_task_workspace",
        ),
        CheckConstraint(
            "failure_code IN ('missing_result', 'duplicate_result', 'wrong_posting_span', "
            "'invalid_cv_evidence', 'inconsistent_verdict', 'leading_excerpt_only')",
            name="ck_relevance_review_rejection_vocab",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    pending_discovery_id: Mapped[str] = mapped_column(String(36), nullable=False)
    task_id: Mapped[str] = mapped_column(String(36), nullable=False)
    failure_code: Mapped[str] = mapped_column(String(40), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    rejected_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class GuidedCampaign(IdMixin, AuditMixin, Base):
    """Immutable compiled manifest for one exact accepted campaign scope and cycle."""

    __tablename__ = "guided_campaigns"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "manifest_sha256", "cycle", name="uq_guided_campaign_scope_cycle"
        ),
        UniqueConstraint("workspace_id", "id", name="uq_guided_campaign_workspace_id"),
        CheckConstraint("cycle >= 1", name="ck_guided_campaign_cycle_positive"),
        CheckConstraint("length(manifest_sha256) = 64", name="ck_guided_campaign_manifest_digest"),
        CheckConstraint("max_model_calls >= 1", name="ck_guided_campaign_max_model_calls"),
        CheckConstraint("max_hosted_searches >= 1", name="ck_guided_campaign_max_hosted_searches"),
        CheckConstraint("max_output_tokens >= 1", name="ck_guided_campaign_max_output_tokens"),
        CheckConstraint(
            "searches_per_attempt >= 1", name="ck_guided_campaign_searches_per_attempt"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "search_parameter_set_id"],
            ["job_search_parameter_sets.workspace_id", "job_search_parameter_sets.id"],
        ),
    )

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    search_parameter_set_id: Mapped[str] = mapped_column(String(36), nullable=False)
    master_cv_version_id: Mapped[str] = mapped_column(
        ForeignKey("master_cv_versions.id"), nullable=False
    )
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    cycle: Mapped[int] = mapped_column(Integer, nullable=False)
    compiler_version: Mapped[str] = mapped_column(String(80), nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    max_model_calls: Mapped[int] = mapped_column(Integer, nullable=False)
    max_hosted_searches: Mapped[int] = mapped_column(Integer, nullable=False)
    max_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    searches_per_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    approved_task_id: Mapped[str | None] = mapped_column(ForeignKey("background_tasks.id"))


class GuidedCampaignState(IdMixin, AuditMixin, Base):
    """Mutable execution status for one campaign, fenced by its owning task."""

    __tablename__ = "guided_campaign_states"
    __table_args__ = (
        UniqueConstraint("campaign_id", name="uq_guided_campaign_state_campaign"),
        CheckConstraint(
            _allowed("status", CAMPAIGN_STATUSES), name="ck_guided_campaign_state_status"
        ),
    )

    campaign_id: Mapped[str] = mapped_column(ForeignKey("guided_campaigns.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="running")
    owner_task_id: Mapped[str | None] = mapped_column(ForeignKey("background_tasks.id"))
    # The status reasons a report reads verbatim; recomputing them would drift.
    reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )


class GuidedCampaignBranch(IdMixin, AuditMixin, Base):
    """One immutable coverage identity compiled into a campaign manifest."""

    __tablename__ = "guided_campaign_branches"
    __table_args__ = (
        UniqueConstraint("campaign_id", "position", name="uq_guided_campaign_branch_position"),
        UniqueConstraint("campaign_id", "branch_key", name="uq_guided_campaign_branch_key"),
        CheckConstraint("position >= 1", name="ck_guided_campaign_branch_position_positive"),
        CheckConstraint("length(branch_key) = 64", name="ck_guided_campaign_branch_key_digest"),
        CheckConstraint(
            "attempt_position IS NULL OR attempt_position >= 1",
            name="ck_guided_campaign_branch_attempt_position",
        ),
    )

    campaign_id: Mapped[str] = mapped_column(ForeignKey("guided_campaigns.id"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    branch_key: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    priority_family: Mapped[str] = mapped_column(String(80), nullable=False)
    protected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    attempt_position: Mapped[int | None] = mapped_column(Integer)
    document: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class GuidedCampaignAttempt(IdMixin, AuditMixin, Base):
    """Mutable dispatch and capture state for one campaign attempt."""

    __tablename__ = "guided_campaign_attempts"
    __table_args__ = (
        UniqueConstraint("campaign_id", "position", name="uq_guided_campaign_attempt_position"),
        UniqueConstraint("provider_response_id", name="uq_guided_campaign_attempt_response_id"),
        UniqueConstraint("model_call_id", name="uq_guided_campaign_attempt_model_call"),
        CheckConstraint("position >= 1", name="ck_guided_campaign_attempt_position_positive"),
        CheckConstraint("search_cap >= 1", name="ck_guided_campaign_attempt_search_cap"),
        CheckConstraint("hosted_search_count >= 0", name="ck_guided_campaign_attempt_hosted_count"),
        CheckConstraint("page_action_count >= 0", name="ck_guided_campaign_attempt_page_count"),
        CheckConstraint(
            _allowed("state", ATTEMPT_STATES),
            name="ck_guided_campaign_attempt_state",
        ),
        CheckConstraint(
            _allowed("error_category", ("", *ATTEMPT_ERROR_CATEGORIES)),
            name="ck_guided_campaign_attempt_error_category",
        ),
        CheckConstraint(
            "request_sha256 IS NULL OR length(request_sha256) = 64",
            name="ck_guided_campaign_attempt_request_digest",
        ),
        CheckConstraint(
            "state NOT IN ('dispatched', 'captured', 'processed') "
            "OR provider_response_id IS NOT NULL",
            name="ck_guided_campaign_attempt_dispatched_has_response",
        ),
        CheckConstraint(
            "state NOT IN ('captured', 'processed') OR model_call_id IS NOT NULL",
            name="ck_guided_campaign_attempt_captured_has_model_call",
        ),
        CheckConstraint(
            "state = 'planned' OR request_sha256 IS NOT NULL",
            name="ck_guided_campaign_attempt_dispatched_has_request",
        ),
        CheckConstraint(
            "state NOT IN ('dispatched', 'captured', 'processed', 'uncertain') "
            "OR dispatched_at IS NOT NULL",
            name="ck_guided_campaign_attempt_dispatched_has_timestamp",
        ),
        CheckConstraint(
            "state NOT IN ('captured', 'processed') OR request_envelope IS NULL",
            name="ck_guided_campaign_attempt_captured_drops_request",
        ),
    )

    campaign_id: Mapped[str] = mapped_column(ForeignKey("guided_campaigns.id"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="planned")
    search_cap: Mapped[int] = mapped_column(Integer, nullable=False)
    request_sha256: Mapped[str | None] = mapped_column(String(64))
    provider_response_id: Mapped[str | None] = mapped_column(String(200))
    # Prompt text including CV evidence, held only until capture writes the envelope row.
    request_envelope: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    model_call_id: Mapped[str | None] = mapped_column(ForeignKey("model_calls.id"))
    hosted_search_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    page_action_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    finish: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    validated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    capture: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    error_category: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    dispatched_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    captured_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )


class GuidedCampaignBranchOutcome(IdMixin, AuditMixin, Base):
    """Immutable outcome for one campaign branch."""

    __tablename__ = "guided_campaign_branch_outcomes"
    __table_args__ = (
        UniqueConstraint("branch_id", name="uq_guided_branch_outcome_branch"),
        CheckConstraint(
            _allowed("outcome", BRANCH_OUTCOMES),
            name="ck_guided_branch_outcome_outcome",
        ),
    )

    branch_id: Mapped[str] = mapped_column(
        ForeignKey("guided_campaign_branches.id"), nullable=False
    )
    attempt_id: Mapped[str | None] = mapped_column(ForeignKey("guided_campaign_attempts.id"))
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    matched_queries: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)


class GuidedCampaignCapturedUrl(IdMixin, AuditMixin, Base):
    """Mutable admission state for one URL captured during a campaign."""

    __tablename__ = "guided_campaign_captured_urls"
    __table_args__ = (
        UniqueConstraint("campaign_id", "url_sha256", name="uq_guided_captured_url"),
        CheckConstraint("length(url_sha256) = 64", name="ck_guided_captured_url_digest"),
        CheckConstraint(
            _allowed("classification", CAPTURED_URL_CLASSIFICATIONS),
            name="ck_guided_captured_url_classification",
        ),
        CheckConstraint(
            _allowed("outcome", CAPTURED_URL_OUTCOMES),
            name="ck_guided_captured_url_outcome",
        ),
        CheckConstraint(
            "staged_basis IS NULL OR " + _allowed("staged_basis", STAGED_BASES),
            name="ck_guided_captured_url_staged_basis",
        ),
        CheckConstraint(
            "robots_basis IS NULL OR " + _allowed("robots_basis", ROBOTS_READ_BASES),
            name="ck_guided_captured_url_robots_basis",
        ),
    )

    campaign_id: Mapped[str] = mapped_column(ForeignKey("guided_campaigns.id"), nullable=False)
    url_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    classification: Mapped[str] = mapped_column(String(40), nullable=False)
    model_selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Model provenance only: never a fact staged onto a discovery or posting.
    model_why: Mapped[str] = mapped_column(Text, nullable=False, default="")
    outcome: Mapped[str] = mapped_column(String(40), nullable=False, default="pending_read")
    staged_basis: Mapped[str | None] = mapped_column(String(40))
    pending_discovery_id: Mapped[str | None] = mapped_column(ForeignKey("pending_discoveries.id"))
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.id"))
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Null until a posting page is read for this URL.
    robots_basis: Mapped[str | None] = mapped_column(String(40))
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )


class GuidedCampaignUrlObservation(IdMixin, AuditMixin, Base):
    """Immutable record of one search or page action that surfaced a captured URL."""

    __tablename__ = "guided_campaign_url_observations"
    __table_args__ = (
        Index("ix_guided_url_observation_captured", "captured_url_id"),
        CheckConstraint(
            _allowed("action_kind", URL_ACTION_KINDS),
            name="ck_guided_url_observation_action_kind",
        ),
    )

    captured_url_id: Mapped[str] = mapped_column(
        ForeignKey("guided_campaign_captured_urls.id"), nullable=False
    )
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("guided_campaign_attempts.id"), nullable=False
    )
    action_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False, default="")


class GuidedCampaignAdmittedDiscovery(IdMixin, AuditMixin, Base):
    """Immutable record that one attempt staged a pending discovery from a captured URL."""

    __tablename__ = "guided_campaign_admitted_discoveries"
    __table_args__ = (
        UniqueConstraint(
            "captured_url_id", "pending_discovery_id", name="uq_guided_admitted_discovery"
        ),
        Index("ix_guided_admitted_discovery_attempt", "attempt_id"),
    )

    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("guided_campaign_attempts.id"), nullable=False
    )
    captured_url_id: Mapped[str] = mapped_column(
        ForeignKey("guided_campaign_captured_urls.id"), nullable=False
    )
    pending_discovery_id: Mapped[str] = mapped_column(
        ForeignKey("pending_discoveries.id"), nullable=False
    )


class Source(IdMixin, AuditMixin, SoftArchiveMixin, Base):
    """One board the product scans, and whether it is scanned at all."""

    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_source_name"),
        CheckConstraint(_allowed("source_type", SOURCE_KINDS), name="ck_source_type"),
    )

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    # An aggregator lists many employers and is bound to none of them.
    employer_id: Mapped[str | None] = mapped_column(ForeignKey("employers.id"))
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    source_type: Mapped[str] = mapped_column(String(80), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # The condition the source's published terms attach to reading it, as the
    # user recorded it, with the address they read it at.
    terms_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    terms_condition: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # The board platform last resolved behind this address, and the address it
    # was found at. Null until a resolution has run; a careers page front ends
    # up with its own URL here rather than the vanity address saved above.
    resolved_platform: Mapped[str | None] = mapped_column(String(80))
    resolved_url: Mapped[str | None] = mapped_column(Text)


class _DerivedProposalLifecycleMixin:
    """Ordering and decision metadata shared by derived search proposals."""

    # One run shares a created_at, so position preserves the proposed order.
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state: Mapped[str] = mapped_column(
        String(40), nullable=False, default=SearchProposalState.PROPOSED.value
    )
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    rejection_reason: Mapped[str | None] = mapped_column(String(40))
    carried_identity: Mapped[str | None] = mapped_column(String(36))


class DerivedSearchTitle(_DerivedProposalLifecycleMixin, IdMixin, AuditMixin, Base):
    """A model-suggested title bound to the master-CV version that supports it.

    Only an accepted title is searched under; a rejected one is kept so the same
    name is not offered again.
    """

    __tablename__ = "derived_search_titles"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "master_cv_version_id", "normalized_title", name="uq_derived_title"
        ),
        CheckConstraint(_allowed("state", SEARCH_PROPOSAL_STATES), name="ck_search_title_state"),
        CheckConstraint(f"lane IS NULL OR {_allowed('lane', LANES)}", name="ck_search_title_lane"),
        CheckConstraint(
            "rejection_reason IS NULL OR "
            + _allowed("rejection_reason", PROPOSAL_REJECTION_REASONS),
            name="ck_search_title_rejection_reason",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    master_cv_version_id: Mapped[str] = mapped_column(
        ForeignKey("master_cv_versions.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    normalized_title: Mapped[str] = mapped_column(String(120), nullable=False)
    reason: Mapped[str] = mapped_column(String(300), nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    lane: Mapped[str | None] = mapped_column(String(40))


class DerivedEmployerArchetype(_DerivedProposalLifecycleMixin, IdMixin, AuditMixin, Base):
    """A model-suggested kind of employer, bound to the CV version that supports it.

    Only an accepted archetype is stated to a later search; a rejected one is
    kept so the same kind is not offered again.
    """

    __tablename__ = "derived_employer_archetypes"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "master_cv_version_id", "normalized_name", name="uq_derived_archetype"
        ),
        CheckConstraint(
            _allowed("state", SEARCH_PROPOSAL_STATES), name="ck_search_archetype_state"
        ),
        CheckConstraint(
            "rejection_reason IS NULL OR "
            + _allowed("rejection_reason", PROPOSAL_REJECTION_REASONS),
            name="ck_search_archetype_rejection_reason",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    master_cv_version_id: Mapped[str] = mapped_column(
        ForeignKey("master_cv_versions.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(120), nullable=False)
    because: Mapped[str] = mapped_column(String(300), nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)


class DerivedCluster(_DerivedProposalLifecycleMixin, IdMixin, AuditMixin, Base):
    """A model-suggested capability or domain, bound to the CV version behind it.

    One table for both kinds, because they differ in what they name rather than
    in shape, and a person rules on them from one queue.
    """

    __tablename__ = "derived_clusters"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "master_cv_version_id",
            "kind",
            "normalized_name",
            name="uq_derived_cluster",
        ),
        CheckConstraint(_allowed("kind", CLUSTER_KINDS), name="ck_cluster_kind"),
        CheckConstraint(_allowed("state", SEARCH_PROPOSAL_STATES), name="ck_cluster_state"),
        CheckConstraint(
            "rejection_reason IS NULL OR "
            + _allowed("rejection_reason", PROPOSAL_REJECTION_REASONS),
            name="ck_cluster_rejection_reason",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    master_cv_version_id: Mapped[str] = mapped_column(
        ForeignKey("master_cv_versions.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(120), nullable=False)
    terms: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    because: Mapped[str] = mapped_column(String(300), nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)


class SourceScan(IdMixin, AuditMixin, Base):
    """Latest source scan result for one labeled query against one source.

    Same-day rescans upsert this row in place; intraday history lives in the
    durable task record.
    """

    __tablename__ = "source_scans"
    __table_args__ = (
        UniqueConstraint("source_id", "scanned_on", "query_label", name="uq_source_scan"),
        CheckConstraint("raw_count >= read_count", name="ck_source_scan_raw_covers_read"),
        CheckConstraint(
            _allowed("read_outcome", SOURCE_READ_OUTCOMES),
            name="ck_source_scan_read_outcome",
        ),
        CheckConstraint(
            _allowed("exhausted_outcome", SOURCE_READ_OUTCOMES),
            name="ck_source_scan_exhausted_outcome",
        ),
        CheckConstraint(
            "detail_interrupted_count IS NULL OR "
            "detail_attempted_count = detail_read_count + detail_failed_count "
            "+ detail_interrupted_count",
            name="ck_source_scan_detail_outcome",
        ),
        CheckConstraint(
            "detail_interrupted_count IS NULL OR detail_interrupted_count >= 0",
            name="ck_source_scan_detail_interrupted_nonnegative",
        ),
        CheckConstraint("duration_ms >= 0", name="ck_source_scan_duration_nonnegative"),
    )

    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False)
    scanned_on: Mapped[date] = mapped_column(Date, nullable=False)
    query_label: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    read_outcome: Mapped[SourceReadOutcome] = mapped_column(
        String(40),
        nullable=False,
        default=SourceReadOutcome.NOT_RECORDED,
        server_default=SourceReadOutcome.NOT_RECORDED.value,
    )
    # Raw records returned by the platform and usable postings parsed from them.
    raw_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    read_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skip_reasons: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False, default=dict)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    new_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detail_attempted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detail_read_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detail_failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detail_interrupted_count: Mapped[int | None] = mapped_column(Integer)
    detail_capped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detail_failure_reasons: Mapped[dict[str, int]] = mapped_column(
        JSON, nullable=False, default=dict, server_default="{}"
    )
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Only a complete read may withdraw an absent opening. Null where no read
    # recorded it.
    complete: Mapped[bool | None] = mapped_column(Boolean)
    # True when discovering the read's candidates hit its own bound, so a drained
    # frontier never stands for full coverage.
    discovery_truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    # Listing pages fetched by this read and every continuation chunk that extended it.
    listing_pages: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # The outcome the adapter declared for this read once its deferred frontier is
    # empty; null when it declared none, and then draining never settles the scan.
    exhausted_outcome: Mapped[SourceReadOutcome | None] = mapped_column(String(40))
    detail_coverage: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    scan_started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    scan_completed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class SourceScanMember(IdMixin, AuditMixin, Base):
    """One source's progress and read reservation in a task-owned scan batch."""

    __tablename__ = "source_scan_members"
    __table_args__ = (
        UniqueConstraint("root_task_id", "source_id", name="uq_source_scan_member"),
        Index(
            "uq_source_scan_member_reserved_source",
            "source_id",
            unique=True,
            sqlite_where=text("released_at IS NULL"),
        ),
        CheckConstraint("ordinal >= 0", name="ck_source_scan_member_ordinal"),
        CheckConstraint(
            _allowed(
                "state",
                (
                    "queued",
                    "scanning",
                    "continuing",
                    "completed",
                    "failed",
                    "cancelled",
                    "already_in_progress",
                ),
            ),
            name="ck_source_scan_member_state",
        ),
        CheckConstraint(
            "active_attempt_count IS NULL OR active_attempt_count >= 1",
            name="ck_source_scan_member_attempt",
        ),
        CheckConstraint(
            "generation IS NULL OR generation >= 1", name="ck_source_scan_member_generation"
        ),
    )

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    root_task_id: Mapped[str] = mapped_column(ForeignKey("background_tasks.id"), nullable=False)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(40), nullable=False, default="queued")
    source_scan_id: Mapped[str | None] = mapped_column(ForeignKey("source_scans.id"))
    continuation_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_scan_continuations.id", name="fk_source_scan_member_continuation")
    )
    generation: Mapped[int | None] = mapped_column(Integer)
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    released_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    active_task_id: Mapped[str | None] = mapped_column(ForeignKey("background_tasks.id"))
    active_attempt_count: Mapped[int | None] = mapped_column(Integer)
    active_process_id: Mapped[int | None] = mapped_column(Integer)
    active_process_started_at: Mapped[str | None] = mapped_column(String(200))
    read_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    read_completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    error_category: Mapped[str | None] = mapped_column(String(120))
    error_message: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class SourceScanOutcome(IdMixin, AuditMixin, Base):
    """One terminal candidate outcome from a board-scan task.

    The task payload is intentionally bounded for responsive progress updates.
    This ledger keeps the full, body-free candidate audit independently of that
    display cap.
    """

    __tablename__ = "source_scan_outcomes"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "source_name",
            "candidate_kind",
            "normalized_url",
            name="uq_source_scan_outcome",
        ),
        Index("ix_source_scan_outcome_workspace_task", "workspace_id", "task_id", "created_at"),
        Index("ix_source_scan_outcome_workspace_reason", "workspace_id", "reason", "created_at"),
        Index("ix_source_scan_outcome_workspace_url", "workspace_id", "normalized_url"),
        Index("ix_source_scan_outcome_source_scan", "source_scan_id"),
        Index(
            "ix_source_scan_outcome_history",
            "workspace_id",
            "task_id",
            "source_name",
            "reason",
            "normalized_url",
        ),
        CheckConstraint(
            "candidate_kind IN ('direct_posting', 'board_lead', 'board_listing')",
            name="ck_source_scan_outcome_kind",
        ),
        CheckConstraint(
            "terminal_stage IN ('proposed', 'fetched', 'read', 'screened', 'staged', 'rejected')",
            name="ck_source_scan_outcome_stage",
        ),
        CheckConstraint(
            "reason IN ('staged', 'already_known', 'incomplete_posting', 'duplicate_url', "
            "'duplicate_lead', 'excluded_employer', 'hard_constraint_excluded', "
            "'insufficient_title_evidence', 'seniority_outside_band', "
            "'location_outside_saved_places', "
            "'stage_limit_reached', 'role_family_review_limit_reached', 'no_title_configured', "
            "'no_open_positions', 'no_matching_listings', 'no_new_openings', 'fetch_refused', "
            "'fetch_failed', 'no_readable_board')",
            name="ck_source_scan_outcome_reason",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    task_id: Mapped[str] = mapped_column(ForeignKey("background_tasks.id"), nullable=False)
    # Null when the task's source label does not identify one scan row. The
    # source label remains the captured evidence for that outcome.
    source_scan_id: Mapped[str | None] = mapped_column(ForeignKey("source_scans.id"))
    source_name: Mapped[str] = mapped_column(String(300), nullable=False)
    candidate_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_url: Mapped[str] = mapped_column(Text, nullable=False)
    employer: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    location: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    terminal_stage: Mapped[str] = mapped_column(String(40), nullable=False)
    reason: Mapped[str] = mapped_column(String(80), nullable=False)
    detail: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    screening_findings: Mapped[list[dict[str, str]]] = mapped_column(
        JSON, nullable=False, default=list
    )


class ScreenedOutTitle(IdMixin, AuditMixin, Base):
    """One screened-out listing across every scan that refused it, keyed by its address.

    A reviewed mark holds only while the title finding it was made against is current.
    """

    __tablename__ = "screened_out_titles"
    __table_args__ = (
        UniqueConstraint("workspace_id", "normalized_url", name="uq_screened_out_title"),
        Index("ix_screened_out_title_last_seen", "workspace_id", "last_seen_at"),
    )

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    normalized_url: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    # The pending queue's URL fingerprint, so a listing staged since leaves the view.
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_name: Mapped[str] = mapped_column(String(300), nullable=False)
    employer: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    reason: Mapped[str] = mapped_column(String(80), nullable=False)
    title_code: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    title_subject: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    reviewed_title_code: Mapped[str | None] = mapped_column(String(80))
    reviewed_title_subject: Mapped[str | None] = mapped_column(String(500))


class SourceScanContinuation(IdMixin, AuditMixin, Base):
    """Durable traversal aggregate created by one incomplete source scan."""

    __tablename__ = "source_scan_continuations"
    __table_args__ = (
        CheckConstraint("generation >= 1", name="ck_source_continuation_generation"),
        UniqueConstraint("source_scan_id", name="uq_source_scan_continuation"),
    )

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    source_scan_id: Mapped[str] = mapped_column(ForeignKey("source_scans.id"), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # [[host, kind, detail], ...] admitted for an off-host handoff; read back unchanged on resume.
    handoff_scopes: Mapped[list[list[str]]] = mapped_column(JSON, nullable=False)
    batch_member_id: Mapped[str | None] = mapped_column(ForeignKey("source_scan_members.id"))


class SourceListingMembership(Base):
    """One board's listed identities, stored once however many frontier rows carry them."""

    __tablename__ = "source_listing_memberships"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    membership: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class SourceScanFrontierItem(IdMixin, AuditMixin, Base):
    """One normalized publisher URL and its retryable traversal outcome."""

    __tablename__ = "source_scan_frontier_items"
    __table_args__ = (
        UniqueConstraint("continuation_id", "normalized_url", name="uq_source_frontier_url"),
        CheckConstraint(
            "status IN ('pending', 'completed', 'failed')", name="ck_source_frontier_status"
        ),
        CheckConstraint("attempt_count >= 0", name="ck_source_frontier_attempt_count"),
        CheckConstraint("depth >= 1", name="ck_source_frontier_depth"),
        Index("ix_source_frontier_work", "continuation_id", "status", "created_at"),
        Index("ix_source_frontier_listing_membership", "listing_membership_sha256"),
    )

    continuation_id: Mapped[str] = mapped_column(
        ForeignKey("source_scan_continuations.id"), nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_url: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    label_source: Mapped[str] = mapped_column(String(20), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    structural_context: Mapped[str] = mapped_column(String(20), nullable=False)
    relations: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    automatic_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    deferred_reason: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    # Every detail row of a board names the whole board's listing, so that part
    # is stored once in source_listing_memberships and joined back on read.
    stored_checkpoint: Mapped[dict[str, Any] | None] = mapped_column("detail_checkpoint", JSON)
    listing_membership_sha256: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("source_listing_memberships.sha256")
    )
    # Joined into the row's own SELECT: one snapshot, so a concurrent release of
    # the membership cannot tear the read, and a detached row keeps its listing.
    listing_membership: Mapped[SourceListingMembership | None] = relationship(
        viewonly=True, lazy="joined"
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    @hybrid_property
    def detail_checkpoint(self) -> dict[str, Any] | None:
        """Return the whole checkpoint, with its board membership joined back in."""
        stored = self.stored_checkpoint
        digest = self.listing_membership_sha256
        if stored is None or digest is None:
            return stored
        return {**stored, **self._listing_membership(digest)}

    @detail_checkpoint.inplace.setter
    def _detail_checkpoint_setter(self, value: dict[str, Any] | None) -> None:
        stored, membership = split_listing_membership(value)
        self.stored_checkpoint = stored
        if membership is None:
            self.listing_membership_sha256 = None
            self.__dict__.pop(_PENDING_MEMBERSHIP, None)
            return
        digest = listing_membership_digest(membership)
        self.listing_membership_sha256 = digest
        self.__dict__[_PENDING_MEMBERSHIP] = (digest, membership)

    @detail_checkpoint.inplace.expression
    @classmethod
    def _detail_checkpoint_expression(cls) -> SQLColumnExpression[dict[str, Any] | None]:
        return cls.stored_checkpoint

    def _listing_membership(self, digest: str) -> dict[str, Any]:
        pending = self.__dict__.get(_PENDING_MEMBERSHIP)
        if pending is not None and pending[0] == digest:
            return cast(dict[str, Any], pending[1])
        stored = self.listing_membership
        if stored is None or stored.sha256 != digest:
            raise LookupError(f"Listing membership {digest} is not stored.")
        return stored.membership


_PENDING_MEMBERSHIP = "_clearscout_listing_membership"
_MEMBERSHIP_KEYS = ("listing_ids", "listing_urls")


def split_listing_membership(
    checkpoint: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Separate a board's multi-listing membership from one row's own checkpoint."""
    if checkpoint is None or len(checkpoint.get("listing_ids") or ()) < 2:
        return checkpoint, None
    stored = {key: value for key, value in checkpoint.items() if key not in _MEMBERSHIP_KEYS}
    return stored, {key: checkpoint.get(key, []) for key in _MEMBERSHIP_KEYS}


def listing_membership_digest(membership: dict[str, Any]) -> str:
    """Return the content address one membership is stored under."""
    canonical = json.dumps(membership, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _store_listing_memberships(session: Session, _context: object, _instances: object) -> None:
    """Insert each membership a pending frontier row names before the row itself."""
    written: set[str] = set()
    for row in (*session.new, *session.dirty):
        if not isinstance(row, SourceScanFrontierItem):
            continue
        pending = row.__dict__.get(_PENDING_MEMBERSHIP)
        if pending is None or pending[0] != row.listing_membership_sha256 or pending[0] in written:
            continue
        session.connection().execute(
            sqlite_insert(_MEMBERSHIPS)
            .values(sha256=pending[0], membership=pending[1])
            .on_conflict_do_nothing()
        )
        written.add(pending[0])


def _release_listing_memberships(session: Session, _context: object) -> None:
    """Drop memberships no frontier row names after a flush that changed frontier rows."""
    if not any(
        isinstance(row, SourceScanFrontierItem)
        for row in (*session.new, *session.dirty, *session.deleted)
    ):
        return
    release_unnamed_listing_memberships(session)


def release_unnamed_listing_memberships(session: Session) -> None:
    """Delete listing memberships that no frontier row names."""
    frontier = cast(Table, SourceScanFrontierItem.__table__)
    named = select(frontier.c.listing_membership_sha256).where(
        frontier.c.listing_membership_sha256 == _MEMBERSHIPS.c.sha256
    )
    session.connection().execute(delete(_MEMBERSHIPS).where(~named.exists()))


_MEMBERSHIPS = cast(Table, SourceListingMembership.__table__)


event.listen(Session, "before_flush", _store_listing_memberships)
event.listen(Session, "after_flush", _release_listing_memberships)


class PostingRefusal(IdMixin, AuditMixin, Base):
    """One posting a stated condition refused, before any model call."""

    __tablename__ = "posting_refusals"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    job_posting_id: Mapped[str | None] = mapped_column(ForeignKey("job_postings.id"))
    pending_discovery_id: Mapped[str | None] = mapped_column(ForeignKey("pending_discoveries.id"))
    task_id: Mapped[str | None] = mapped_column(ForeignKey("background_tasks.id"))
    search_parameter_set_id: Mapped[str | None] = mapped_column(
        ForeignKey("job_search_parameter_sets.id")
    )
    employer: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    condition: Mapped[str] = mapped_column(String(60), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    refused_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class MissedRequirementText(IdMixin, AuditMixin, Base):
    """One requirement text an evaluation left unanswered under the current CV version."""

    __tablename__ = "missed_requirement_texts_v1"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "master_cv_version_id",
            "requirement_text",
            "source_evaluation_id",
            name="uq_missed_requirement_text",
        ),
        Index(
            "ix_missed_requirement_workspace_cv",
            "workspace_id",
            "master_cv_version_id",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    master_cv_version_id: Mapped[str] = mapped_column(
        ForeignKey("master_cv_versions.id"), nullable=False
    )
    source_evaluation_id: Mapped[str] = mapped_column(
        ForeignKey("job_evaluations.id"), nullable=False
    )
    requirement_text: Mapped[str] = mapped_column(Text, nullable=False)


class RefusalPatternProposal(IdMixin, AuditMixin, Base):
    """A recurring (condition, reason) refusal offered as an exclusion."""

    __tablename__ = "refusal_pattern_proposals"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "condition",
            "normalized_reason",
            name="uq_refusal_pattern_proposal",
        ),
        CheckConstraint(
            _allowed("state", SEARCH_PROPOSAL_STATES),
            name="ck_refusal_pattern_proposal_state",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    condition: Mapped[str] = mapped_column(String(60), nullable=False)
    normalized_reason: Mapped[str] = mapped_column(Text, nullable=False)
    reason_examples: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    refusal_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    proposed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    state: Mapped[str] = mapped_column(
        String(24), nullable=False, default=SearchProposalState.PROPOSED.value
    )
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


MASTER_CV_PROPOSAL_KINDS = ("promote_transferable", "add_missing")


class MasterCvProposal(IdMixin, AuditMixin, Base):
    """A proposed master-CV entry aggregated from recurring rulings or gaps."""

    __tablename__ = "master_cv_proposals"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "kind", "requirement_key", name="uq_master_cv_proposal_identity"
        ),
        CheckConstraint(
            _allowed("kind", MASTER_CV_PROPOSAL_KINDS), name="ck_master_cv_proposal_kind"
        ),
        CheckConstraint(
            _allowed("state", SEARCH_PROPOSAL_STATES), name="ck_master_cv_proposal_state"
        ),
    )

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    requirement_key: Mapped[str] = mapped_column(String(36), nullable=False)
    requirement_text: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_note: Mapped[str | None] = mapped_column(Text)
    posting_count: Mapped[int] = mapped_column(Integer, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    proposed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    state: Mapped[str] = mapped_column(
        String(40), nullable=False, default=SearchProposalState.PROPOSED.value
    )
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
