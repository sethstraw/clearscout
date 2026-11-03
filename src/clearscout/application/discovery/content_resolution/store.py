"""Appending one posting read as durable evidence and moving its row to match."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import replace
from typing import Any, TypeGuard

from sqlalchemy import delete, exists, or_, select
from sqlalchemy.orm import Session

from clearscout.application.discovery.content_resolution.results import (
    ResolvedPostingContent,
    validate_result,
    withdraws_row,
)
from clearscout.application.discovery.content_resolution.retained import (
    body_backed,
    current_content,
    identity_of_retained_body,
    keeps_readable_body,
    latest_bodied,
    relaid,
    repoint_to_read_address,
    retain_date_without_body,
    take_read_identity,
    take_wrapped_lever_identity,
    with_retained_body_recovery,
)
from clearscout.application.discovery.content_resolution.screen import (
    BodyScreen,
    body_screen,
    body_screening_snapshot,
    family_order,
    row_places,
    take_body_pay,
    useful_body,
)
from clearscout.application.discovery.content_resolution.targets import StoredEvidence, StoredTarget
from clearscout.application.discovery.dispositions import DiscoveryDispositionService
from clearscout.application.discovery.pending import (
    dismissed_pending_discovery_ids,
    move_pending_markers,
    release_variants_of_withdrawn,
    visible_pending_discovery_filters,
)
from clearscout.application.discovery.posting_identity import place_blind_hash, same_words
from clearscout.application.discovery.posting_text import publisher_closing_date
from clearscout.application.discovery.relevance_disputes import RelevanceDisputeService
from clearscout.application.discovery.relevance_review_rejections import (
    RelevanceReviewRejectionService,
)
from clearscout.application.discovery.screening import ScreeningCode
from clearscout.application.documents.evidence_artifacts import EvidenceArtifactService
from clearscout.application.failures import ConflictError, ValidationError
from clearscout.application.tasks import TaskContext
from clearscout.application.workspaces.parameters import SearchParametersService
from clearscout.application.workspaces.records.access import audit_columns
from clearscout.domain import ArtifactLogicalType, PostingIdentityStatus, PostingStatus, new_id
from clearscout.domain.identity import normalize_text
from clearscout.domain.staged_facts import PublisherClosingDate
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    Artifact,
    AuditEvent,
    PendingDiscovery,
    PendingPostingContent,
    RelevanceReview,
    Workspace,
)


def _content_record(
    evidence_id: str,
    workspace_id: str,
    pending_id: str,
    result: ResolvedPostingContent,
    *,
    artifact_id: str | None = None,
    content_sha256: str | None = None,
    place_blind_digest: str | None = None,
    supersedes_content_id: str | None,
    audit: dict[str, Any],
) -> PendingPostingContent:
    """Return the content row recording one read of a pending posting."""
    return PendingPostingContent(
        id=evidence_id,
        workspace_id=workspace_id,
        pending_discovery_id=pending_id,
        artifact_id=artifact_id,
        requested_url=result.requested_url,
        final_url=result.final_url,
        requested_url_sha256=_text_sha256(result.requested_url),
        final_url_sha256=_text_sha256(result.final_url) if result.final_url is not None else None,
        content_sha256=content_sha256,
        place_blind_digest=place_blind_digest,
        content_status=result.content_status,
        identity_status=result.identity_status,
        posting_status=result.posting_status,
        extraction_method=result.extraction_method,
        fetched_at=result.fetched_at,
        http_status=result.http_status,
        publisher_markup_present=result.publisher_markup_present,
        identity_evidence=result.identity_evidence or {},
        robots_basis=result.robots_basis,
        publisher_closing_date=result.publisher_closing_date.as_json()
        if result.publisher_closing_date is not None
        else None,
        supersedes_content_id=supersedes_content_id,
        **audit,
    )


class ContentEvidenceStore:
    """Append posting reads as evidence and keep each row's withdrawal and family current."""

    def __init__(self, database: Database, artifacts: EvidenceArtifactService) -> None:
        """Bind evidence writes to the database and the artifact store the service reads."""
        self.database = database
        self.artifacts = artifacts

    def _stored_body(
        self, session: Session, workspace_id: str, previous: PendingPostingContent | None
    ) -> bytes:
        """Return the bytes of the row's current body, or none when it has none or they are lost."""
        if previous is None:
            return b""
        try:
            return self.retained_body(session, workspace_id, previous)
        except ValidationError:
            # A lost or corrupt stored body is repaired by storing this read.
            return b""

    def _retained_closing_date(
        self,
        session: Session,
        workspace_id: str,
        evidence_id: str,
        result: ResolvedPostingContent,
        body_artifact_id: str | None,
        source_ids: dict[ArtifactLogicalType, str],
        previous: PendingPostingContent | None,
        *,
        origin: str,
        audit: dict[str, Any],
    ) -> PublisherClosingDate:
        fact = result.publisher_closing_date or publisher_closing_date(
            (result.content or b"").decode("utf-8"),
            source_url=result.final_url or result.requested_url,
            observed_at=result.fetched_at,
        )
        previous_fact = (
            PublisherClosingDate.from_json(previous.publisher_closing_date)
            if previous is not None and previous.publisher_closing_date is not None
            else None
        )
        changed = (
            previous_fact is None
            or fact.source_url != previous_fact.source_url
            or fact.with_artifacts(body_artifact_id=None, publisher_artifact_id=None).statements
            != previous_fact.with_artifacts(
                body_artifact_id=None, publisher_artifact_id=None
            ).statements
        )
        if not source_ids and changed and result.source is not None:
            source_ids = self.artifacts.store_posting_source(
                session,
                workspace_id=workspace_id,
                directory=f"{workspace_id}/pending-posting-content/{evidence_id}",
                source=result.source,
                related_entity_type="pending_posting_content",
                related_entity_id=evidence_id,
                provenance={
                    "source": origin,
                    "requested_url": result.requested_url,
                    "final_url": result.final_url,
                },
                audit=audit,
            )
        publisher_artifact_id = (
            source_ids.get("posting.source.json")
            or source_ids.get("posting.source.html")
            or source_ids.get("posting.source.xml")
        )
        if (
            publisher_artifact_id is None
            and not changed
            and previous_fact is not None
            and any(item.basis == "valid_through" for item in previous_fact.statements)
        ):
            publisher_artifact_id = previous_fact.artifact_id
        return fact.with_artifacts(
            body_artifact_id=body_artifact_id, publisher_artifact_id=publisher_artifact_id
        )

    def _store_read(
        self,
        session: Session,
        workspace_id: str,
        evidence_id: str,
        target_url: str,
        result: ResolvedPostingContent,
        content: bytes,
        *,
        origin: str,
        audit: dict[str, Any],
    ) -> tuple[str, dict[ArtifactLogicalType, str]]:
        """Store this read's new body, and what it was converted from beside it."""
        directory = f"{workspace_id}/pending-posting-content/{evidence_id}"
        artifact = self.artifacts.store(
            session,
            workspace_id=workspace_id,
            logical_type="posting.body.text",
            content=content,
            relative_path=f"{directory}/body.txt",
            mime_type="text/plain",
            related_entity_type="pending_posting_content",
            related_entity_id=evidence_id,
            provenance={
                "source": origin,
                "original_target_url": target_url,
                "requested_url": result.requested_url,
                "final_url": result.final_url,
                "extraction_method": (
                    result.extraction_method.value if result.extraction_method else None
                ),
            },
            audit=audit,
        )
        source_ids: dict[ArtifactLogicalType, str] = {}
        if result.source is not None:
            source_ids = self.artifacts.store_posting_source(
                session,
                workspace_id=workspace_id,
                directory=directory,
                source=result.source,
                related_entity_type="pending_posting_content",
                related_entity_id=evidence_id,
                provenance={
                    "source": origin,
                    "requested_url": result.requested_url,
                    "final_url": result.final_url,
                },
                audit=audit,
            )
        return str(artifact.id), source_ids

    def retained_body(
        self, session: Session, workspace_id: str, evidence: PendingPostingContent
    ) -> bytes:
        """Return the bytes one retained evidence row stands on, or none when it has none."""
        workspace = session.get(Workspace, workspace_id)
        artifact = (
            session.get(Artifact, evidence.artifact_id)
            if evidence.artifact_id is not None
            else None
        )
        if workspace is None or artifact is None:
            return b""
        body = self.artifacts.read(artifact, workspace)
        if hashlib.sha256(body).hexdigest() != evidence.content_sha256:
            raise ValidationError("Pending-posting content digest does not match its artifact.")
        return body

    def _body_identical_primary_id(
        self,
        session: Session,
        workspace_id: str,
        pending: PendingDiscovery,
        content_sha256: str,
        place_blind_digest: str | None,
        body_refused: bool,
    ) -> str | None:
        """Return the head of this row's regional family, or none when this row leads it."""
        if session.scalar(
            select(
                exists().where(
                    PendingDiscovery.workspace_id == workspace_id,
                    PendingDiscovery.regional_variant_of == pending.id,
                )
            )
        ):
            return None
        employer = normalize_text(pending.employer)
        title = normalize_text(pending.title)
        requisition = normalize_text(pending.requisition_id)
        # A row that states no place has nothing removed from its body, so its
        # place-blind digest would only restate a looser body digest.
        same_body = [PendingPostingContent.content_sha256 == content_sha256]
        if place_blind_digest is not None:
            same_body.append(PendingPostingContent.place_blind_digest == place_blind_digest)
        dismissed = dismissed_pending_discovery_ids(workspace_id)
        candidates = session.scalars(
            select(PendingDiscovery)
            .join(
                PendingPostingContent,
                PendingPostingContent.id == PendingDiscovery.current_content_id,
            )
            .where(
                *visible_pending_discovery_filters(workspace_id, dismissed),
                PendingDiscovery.id != pending.id,
                or_(*same_body),
                PendingPostingContent.identity_status == PostingIdentityStatus.VERIFIED,
                PendingPostingContent.posting_status == PostingStatus.ACTIVE,
            )
        ).all()
        matches = [
            other
            for other in candidates
            if normalize_text(other.employer) == employer
            and normalize_text(other.title) == title
            # A board that states a requisition id has already said which opening
            # this is, so two stated ids fold only when they are the same id.
            and normalize_text(other.requisition_id) == requisition
        ]
        if not matches:
            return None
        parameters = SearchParametersService(self.database).current(workspace_id)
        searched = list(parameters.seniority.stated_titles)
        order = {other.id: family_order(other, parameters, searched) for other in matches}
        head = min(matches, key=lambda other: order[other.id])
        mine = family_order(pending, parameters, searched, body_refused=body_refused)
        if order[head.id] >= mine:
            return None
        return head.id

    def link_regional_variant(
        self,
        session: Session,
        workspace_id: str,
        pending: PendingDiscovery,
        *,
        identity_status: PostingIdentityStatus,
        posting_status: PostingStatus | None,
        content_sha256: str | None,
        place_blind_digest: str | None,
        body_is_useful: bool,
        body_refused: bool,
    ) -> None:
        """Link one verified, active, useful body to its visible regional-family head."""
        if not (
            pending.regional_variant_of is None
            and content_sha256 is not None
            and body_is_useful
            and identity_status is PostingIdentityStatus.VERIFIED
            and posting_status is PostingStatus.ACTIVE
        ):
            return
        primary_id = self._body_identical_primary_id(
            session, workspace_id, pending, content_sha256, place_blind_digest, body_refused
        )
        if primary_id is not None:
            pending.regional_variant_of = primary_id
            head = session.get(PendingDiscovery, primary_id)
            if head is not None:
                move_pending_markers(pending, head)

    def store(
        self,
        workspace_id: str,
        target: StoredTarget,
        result: ResolvedPostingContent,
        context: TaskContext | None,
        *,
        origin: str,
        expected_requested_url: str,
        actor: str = "worker",
        correlation_id: str | None = None,
        adopt_read_address: bool = False,
        require_source_read: Callable[[Session], None] | None = None,
    ) -> StoredEvidence:
        """Append one read for a target that has not changed, and move its row to match."""
        validate_result(expected_requested_url, result)
        result = identity_of_retained_body(result)
        evidence_id = new_id()
        correlation_id = context.task_id if context is not None else correlation_id
        # The write lock is held from the row's read, so no re-point lands between check and write.
        with self.database.exclusive_session() as session:
            if require_source_read is not None:
                require_source_read(session)
            pending = session.get(PendingDiscovery, target.id)
            if not _same_target(pending, workspace_id, target):
                raise ConflictError("A pending posting changed while its content was resolving.")
            artifact_id = None
            content_sha256 = None
            place_blind_digest = None
            body_is_useful = False
            same_text = False
            previous = current_content(session, target)
            bodied = latest_bodied(session, previous)
            stored = self._stored_body(session, workspace_id, bodied)
            if keeps_readable_body(previous, result, stored):
                # A kept-aside read joins no chain, since each row supersedes at most one other.
                session.add(
                    _content_record(
                        evidence_id,
                        workspace_id,
                        target.id,
                        result,
                        supersedes_content_id=None,
                        audit=audit_columns(actor, origin, correlation_id),
                    )
                )
                pending.content_note = (
                    f"{result.note} The retained posting body remains current.".strip()
                )
                return StoredEvidence(evidence_id, kept_body=True)
            result = with_retained_body_recovery(result, bodied, stored)
            result = retain_date_without_body(result, previous)
            result = relaid(stored, result)
            if result.content is not None:
                source_ids: dict[ArtifactLogicalType, str] = {}
                text = result.content.decode("utf-8", errors="ignore")
                same_text = bool(stored) and same_words(
                    stored.decode("utf-8", errors="ignore"), text
                )
                places = row_places(pending)
                content_sha256 = hashlib.sha256(result.content).hexdigest()
                place_blind_digest = place_blind_hash(text, places) if places else None
                body_is_useful = useful_body(result.content)
                if bodied is not None and stored == result.content:
                    # Bytes read back intact keep the artifact, and the source, that made them.
                    artifact_id = bodied.artifact_id
                else:
                    artifact_id, source_ids = self._store_read(
                        session,
                        workspace_id,
                        evidence_id,
                        target.url,
                        result,
                        result.content,
                        origin=origin,
                        audit=audit_columns(actor, origin, correlation_id),
                    )
                result = replace(
                    result,
                    publisher_closing_date=self._retained_closing_date(
                        session,
                        workspace_id,
                        evidence_id,
                        result,
                        artifact_id,
                        source_ids,
                        bodied,
                        origin=origin,
                        audit=audit_columns(actor, origin, correlation_id),
                    ),
                )
            record = _content_record(
                evidence_id,
                workspace_id,
                target.id,
                result,
                artifact_id=artifact_id,
                content_sha256=content_sha256,
                place_blind_digest=place_blind_digest,
                supersedes_content_id=target.current_content_id,
                audit=audit_columns(actor, origin, correlation_id),
            )
            session.add(record)
            session.flush()
            address_holder = (
                repoint_to_read_address(
                    session,
                    workspace_id,
                    pending,
                    result,
                    evidence_id=evidence_id,
                    audit=audit_columns(actor, origin, correlation_id),
                )
                if adopt_read_address
                else None
            )
            same_url = previous is not None and previous.requested_url == result.requested_url
            body_changed = not same_url or bodied is None or bodied.content_sha256 != content_sha256
            screen = self._stated_body_screen(pending, result, workspace_id)
            refusal = screen.refusal
            pending.current_content_id = record.id
            take_wrapped_lever_identity(pending, result)
            take_read_identity(pending, result)
            take_body_pay(pending, screen.pay_reading)
            self.link_regional_variant(
                session,
                workspace_id,
                pending,
                identity_status=result.identity_status,
                posting_status=result.posting_status,
                content_sha256=content_sha256,
                place_blind_digest=place_blind_digest,
                body_is_useful=body_is_useful,
                body_refused=refusal is not None,
            )
            pending.content_status = result.content_status
            pending.content_note = result.note
            # Reviews judged the words, so a read that keeps no body or only lays them
            # out anew keeps them; new words or a new URL clear them, their dispute and
            # a rejected mark.
            new_words = result.content is not None and body_changed and not same_text
            if not same_url or new_words:
                RelevanceDisputeService(self.database).clear_in_session(
                    session,
                    workspace_id,
                    pending.id,
                    actor=actor,
                    origin=origin,
                    correlation_id=correlation_id,
                )
                session.execute(
                    delete(RelevanceReview).where(
                        RelevanceReview.pending_discovery_id == pending.id
                    )
                )
                RelevanceReviewRejectionService(self.database).clear_in_session(
                    session,
                    workspace_id,
                    pending.id,
                    actor=actor,
                    origin=origin,
                    correlation_id=correlation_id,
                )
            relisted = self._apply_listing_state(
                session,
                workspace_id,
                pending,
                result,
                evidence_id=evidence_id,
                origin=origin,
                actor=actor,
                correlation_id=correlation_id,
            )
            if body_changed and content_sha256 is not None:
                DiscoveryDispositionService(self.database).restore_no_requirements_in_session(
                    session,
                    workspace_id,
                    pending.id,
                    content_sha256=content_sha256,
                    actor=actor,
                    origin=f"{origin}.body_screen",
                    correlation_id=correlation_id,
                )
            self._record_body_screen(
                session,
                workspace_id,
                pending,
                result,
                refusal,
                cleared=screen.cleared,
                content_sha256=content_sha256,
                new_words=not same_url or new_words,
                origin=origin,
                actor=actor,
                correlation_id=correlation_id,
            )
        return StoredEvidence(evidence_id, relisted, address_holder)

    def _apply_listing_state(
        self,
        session: Session,
        workspace_id: str,
        pending: PendingDiscovery,
        result: ResolvedPostingContent,
        *,
        evidence_id: str,
        origin: str,
        actor: str,
        correlation_id: str | None,
    ) -> bool:
        """Move the row's withdrawal to match this read, returning whether the read relisted it."""
        was_withdrawn = pending.withdrawn_at is not None
        if withdraws_row(result):
            pending.withdrawn_at = result.fetched_at
        elif result.posting_status is PostingStatus.ACTIVE:
            pending.withdrawn_at = None
        if (pending.withdrawn_at is not None) != was_withdrawn:
            session.add(
                AuditEvent(
                    workspace_id=workspace_id,
                    action=(
                        "pending_discovery.relisted_on_content_read"
                        if was_withdrawn
                        else "pending_discovery.withdrawn_on_content_read"
                    ),
                    entity_type="pending_discovery",
                    entity_id=pending.id,
                    details={
                        "discovery_url": pending.discovery_url,
                        "evidence_id": evidence_id,
                        "note": result.note[:500],
                    },
                    **audit_columns(actor, origin, correlation_id),
                )
            )
        if pending.withdrawn_at is not None and not was_withdrawn:
            release_variants_of_withdrawn(
                session,
                workspace_id,
                [pending.id],
                actor=actor,
                origin=origin,
                correlation_id=correlation_id,
            )
        return was_withdrawn and pending.withdrawn_at is None

    def _stated_body_screen(
        self, pending: PendingDiscovery, result: ResolvedPostingContent, workspace_id: str
    ) -> BodyScreen:
        """Return the boundary this attempt's own retained body conflicts with, and its reading."""
        if not body_backed(result) or result.content is None:
            return BodyScreen(None, None)
        return body_screen(pending, result.content, workspace_id, self.database, result.source)

    def _record_body_screen(
        self,
        session: Session,
        workspace_id: str,
        pending: PendingDiscovery,
        result: ResolvedPostingContent,
        refusal: tuple[ScreeningCode, str] | None,
        *,
        cleared: frozenset[str],
        content_sha256: str | None,
        new_words: bool,
        origin: str,
        actor: str,
        correlation_id: str | None,
    ) -> None:
        """Append the automatic disposition a source's own body read states for this row."""
        if not origin.startswith("source.") or not body_backed(result):
            return
        dispositions = DiscoveryDispositionService(self.database)
        if refusal is None:
            dispositions.restore_body_refusal_in_session(
                session,
                workspace_id,
                pending.id,
                cleared=cleared,
                content_sha256=content_sha256,
                new_words=new_words,
                actor=actor,
                origin=f"{origin}.body_screen",
                correlation_id=correlation_id,
            )
            return
        code, subject = refusal
        dispositions.dismiss_refusal_in_session(
            session,
            workspace_id,
            pending.id,
            category=code.value,
            reason=(
                "A verified publisher body explicitly conflicts with the current "
                f"search boundary ({subject}). It was screened automatically and "
                "stays restorable."
            ),
            search_parameter_set_id=None,
            actor=actor,
            origin=f"{origin}.body_screen",
            correlation_id=correlation_id,
            screening_override=body_screening_snapshot(code, subject, content_sha256),
        )


def _same_target(
    pending: PendingDiscovery | None,
    workspace_id: str,
    target: StoredTarget,
) -> TypeGuard[PendingDiscovery]:
    return bool(
        pending is not None
        and pending.workspace_id == workspace_id
        and pending.archived_at is None
        and pending.fingerprint == target.fingerprint
        and pending.employer == target.employer
        and pending.title == target.title
        and (pending.canonical_url or pending.discovery_url) == target.url
        and pending.current_content_id == target.current_content_id
        and pending.content_status == target.content_status
    )


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
