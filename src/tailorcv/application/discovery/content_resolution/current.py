"""Verified current content, and the readiness and cut-off checks it feeds."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tailorcv.application.discovery.relevance_review_records import (
    CURRENT_FIRST,
    relevance_review_digest,
)
from tailorcv.application.documents.evidence_artifacts import EvidenceArtifactService
from tailorcv.application.failures import ValidationError
from tailorcv.domain import (
    PendingDiscoveryReadiness,
    PostingContentAccessStatus,
    PostingContentExtractionMethod,
    PostingIdentityStatus,
    PostingStatus,
)
from tailorcv.domain.records import RobotsReadBasis
from tailorcv.domain.staged_facts import PublisherClosingDate
from tailorcv.infrastructure.artifacts.runtime import RuntimeStorage
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    Artifact,
    PendingDiscovery,
    PendingPostingContent,
    RelevanceReview,
    Workspace,
)
from tailorcv.infrastructure.web_fetch.ingest import strip_injected_instructions


@dataclass(frozen=True)
class CurrentPostingContent:
    """Verified current publisher bytes suitable for semantic review and evaluation."""

    evidence_id: str
    body: bytes
    fetched_at: datetime
    extraction_method: PostingContentExtractionMethod
    http_status: int | None
    robots_basis: RobotsReadBasis | None = None
    publisher_closing_date: PublisherClosingDate | None = None


class CurrentPostingContentReader:
    """Read the current verified content projection through artifact integrity checks."""

    def __init__(self, database: Database, *, storage: RuntimeStorage) -> None:
        """Bind the current projection to its database and artifact boundary."""
        self.database = database
        self.artifacts = EvidenceArtifactService(storage)

    def read(self, workspace_id: str, pending_id: str) -> CurrentPostingContent | None:
        """Return verified current bytes, or none when the evidence is not usable."""
        with self.database.session() as session:
            workspace = session.get(Workspace, workspace_id)
            pending = session.get(PendingDiscovery, pending_id)
            if (
                workspace is None
                or pending is None
                or pending.workspace_id != workspace_id
                or pending.current_content_id is None
            ):
                return None
            evidence = session.get(PendingPostingContent, pending.current_content_id)
            if evidence is None:
                raise ValidationError("Current pending-posting content evidence is missing.")
            if not usable_current_evidence(pending, evidence):
                return None
            if evidence.artifact_id is None or evidence.extraction_method is None:
                raise ValidationError("Verified pending-posting content has no retained body.")
            artifact = session.get(Artifact, evidence.artifact_id)
            if artifact is None:
                raise ValidationError("Verified pending-posting content artifact is missing.")
            body = self.verified_body(artifact, workspace, evidence)
            return CurrentPostingContent(
                evidence.id,
                body,
                evidence.fetched_at,
                PostingContentExtractionMethod(evidence.extraction_method),
                evidence.http_status,
                RobotsReadBasis(evidence.robots_basis) if evidence.robots_basis else None,
                PublisherClosingDate.from_json(evidence.publisher_closing_date)
                if evidence.publisher_closing_date is not None
                else None,
            )

    def verified_body(
        self, artifact: Artifact, workspace: Workspace, evidence: PendingPostingContent
    ) -> bytes:
        """Return the artifact's bytes, refusing ones whose digest differs from the evidence."""
        body = self.artifacts.read(artifact, workspace)
        if hashlib.sha256(body).hexdigest() != evidence.content_sha256:
            raise ValidationError("Pending-posting content digest does not match its artifact.")
        return body


def usable_current_evidence(pending: PendingDiscovery, evidence: PendingPostingContent) -> bool:
    """Return whether the current evidence is safe to expose for evaluation."""
    target_url = pending.canonical_url or pending.discovery_url
    return bool(
        evidence.workspace_id == pending.workspace_id
        and evidence.pending_discovery_id == pending.id
        and evidence.requested_url == target_url
        and PostingContentAccessStatus(evidence.content_status)
        is PostingContentAccessStatus.READABLE
        and PostingIdentityStatus(evidence.identity_status) is PostingIdentityStatus.VERIFIED
        and evidence.posting_status is not None
        and PostingStatus(evidence.posting_status) is PostingStatus.ACTIVE
        and evidence.artifact_id is not None
        and evidence.extraction_method is not None
        and evidence.content_sha256 is not None
    )


_CUT_OFF_CHARACTERS = 800


_TERMINAL_CHARACTERS = frozenset(
    ".!?:;\"')]}"
    "\N{RIGHT DOUBLE QUOTATION MARK}\N{RIGHT SINGLE QUOTATION MARK}"
    "\N{RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK}"
)


CUT_OFF_SKIP_REASON = "The retained text appears cut off, so no evaluation was run."


def appears_cut_off(text: str) -> bool:
    """Return whether a short body's last non-blank line ends without terminal punctuation."""
    if len(text) >= _CUT_OFF_CHARACTERS:
        return False
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return bool(lines) and lines[-1][-1] not in _TERMINAL_CHARACTERS


# A character is at most four UTF-8 bytes, so a larger body cannot fall under the length rule.
_CUT_OFF_BYTES = 4 * _CUT_OFF_CHARACTERS


@dataclass(frozen=True)
class TooShortReviews:
    """Review digests found too short, and every retained body of the reviewed rows."""

    digests: frozenset[str]
    content_sha256s: frozenset[str]

    def may_flag(self, byte_size: int, content_sha256: str | None) -> bool:
        """Return whether a body of this size and digest can read as cut off."""
        return byte_size < _CUT_OFF_BYTES or content_sha256 in self.content_sha256s


def too_short_reviews(session: Session, workspace_id: str) -> TooShortReviews:
    """Return the current relevance reviews that found a body too short to judge."""
    ranked = (
        select(
            RelevanceReview.pending_discovery_id,
            RelevanceReview.body_sha256,
            RelevanceReview.recommendation,
            func.row_number()
            .over(partition_by=RelevanceReview.pending_discovery_id, order_by=CURRENT_FIRST)
            .label("rank"),
        )
        .where(RelevanceReview.workspace_id == workspace_id)
        .subquery()
    )
    rows = session.execute(
        select(ranked.c.body_sha256, PendingPostingContent.content_sha256)
        .join(PendingDiscovery, PendingDiscovery.id == ranked.c.pending_discovery_id)
        .outerjoin(
            PendingPostingContent,
            PendingPostingContent.pending_discovery_id == ranked.c.pending_discovery_id,
        )
        .where(ranked.c.rank == 1, ranked.c.recommendation.is_(None))
    ).all()
    return TooShortReviews(
        frozenset(digest for digest, _ in rows),
        frozenset(sha for _, sha in rows if sha is not None),
    )


def retained_text_cut_off(
    body: bytes, extraction_method: str | None, too_short: frozenset[str]
) -> bool:
    """Return whether retained text reads as a fragment; text a person attached never does."""
    if extraction_method and extraction_method.startswith(
        PostingContentExtractionMethod.USER_SUPPLIED_TEXT.value
    ):
        return False
    text = body.decode("utf-8", errors="replace")
    # A review digests the sanitized text it was sent, so the match applies the same chain.
    return (
        appears_cut_off(text)
        or relevance_review_digest(strip_injected_instructions(text)) in too_short
    )


def pending_discovery_readiness(
    pending: PendingDiscovery, evidence: PendingPostingContent | None
) -> PendingDiscoveryReadiness:
    """Return the server-owned reason a pending posting can or cannot be evaluated."""
    # Only a marker keeps a withdrawn row listed, and no content read can make it ready.
    if pending.withdrawn_at is not None:
        return PendingDiscoveryReadiness.WITHDRAWN
    if evidence is None:
        return _unreadable_readiness(PostingContentAccessStatus(pending.content_status))
    target_url = pending.canonical_url or pending.discovery_url
    if evidence.requested_url != target_url:
        return PendingDiscoveryReadiness.URL_MISMATCH
    content_status = PostingContentAccessStatus(evidence.content_status)
    if content_status is not PostingContentAccessStatus.READABLE:
        return _unreadable_readiness(content_status)
    identity_status = PostingIdentityStatus(evidence.identity_status)
    if identity_status is PostingIdentityStatus.CONTRADICTED:
        return PendingDiscoveryReadiness.IDENTITY_CONTRADICTED
    if identity_status is PostingIdentityStatus.AMBIGUOUS:
        return PendingDiscoveryReadiness.IDENTITY_AMBIGUOUS
    if identity_status is not PostingIdentityStatus.VERIFIED:
        return PendingDiscoveryReadiness.IDENTITY_NOT_VERIFIED
    if (
        evidence.posting_status is None
        or PostingStatus(evidence.posting_status) is not PostingStatus.ACTIVE
        or evidence.artifact_id is None
        or evidence.extraction_method is None
        or evidence.content_sha256 is None
    ):
        return PendingDiscoveryReadiness.EVIDENCE_INCOMPLETE
    return PendingDiscoveryReadiness.READY


def _unreadable_readiness(
    status: PostingContentAccessStatus,
) -> PendingDiscoveryReadiness:
    return {
        PostingContentAccessStatus.ACCESS_BLOCKED: PendingDiscoveryReadiness.ACCESS_BLOCKED,
        PostingContentAccessStatus.FETCH_FAILED: PendingDiscoveryReadiness.FETCH_FAILED,
        PostingContentAccessStatus.RENDER_REQUIRED: PendingDiscoveryReadiness.RENDER_REQUIRED,
        PostingContentAccessStatus.SESSION_REQUIRED: PendingDiscoveryReadiness.SESSION_REQUIRED,
        PostingContentAccessStatus.POLICY_REFUSED: PendingDiscoveryReadiness.POLICY_REFUSED,
    }.get(status, PendingDiscoveryReadiness.NO_BODY)
