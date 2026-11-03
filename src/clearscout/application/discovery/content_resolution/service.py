"""Snapshot-bound posting resolution: targets, automatic passes, and stored results."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from clearscout.application.discovery.content_resolution.current import CurrentPostingContentReader
from clearscout.application.discovery.content_resolution.reads import PostingReadLanes
from clearscout.application.discovery.content_resolution.results import (
    LISTING_PAGE_NOTE,
    MAX_IDENTITY_EVIDENCE_BYTES,
    ResolvedPostingContent,
    withdraws_row,
)
from clearscout.application.discovery.content_resolution.screen import (
    body_screen,
    take_body_pay,
    useful_body,
)
from clearscout.application.discovery.content_resolution.store import ContentEvidenceStore
from clearscout.application.discovery.content_resolution.targets import (
    PostingContentResolver,
    PostingResolutionPreflight,
    PostingResolutionTarget,
    PrefetchedContent,
    StoredEvidence,
    StoredTarget,
)
from clearscout.application.discovery.dispositions import BODY_REFUSAL_CATEGORIES
from clearscout.application.discovery.pending import (
    corrected_title_ids,
    latest_disposition_statement,
)
from clearscout.application.discovery.posting_text import lists_many_postings
from clearscout.application.discovery.scan_staging.reasons import staged_by_person
from clearscout.application.discovery.source_lifecycle import source_read_address
from clearscout.application.documents.evidence_artifacts import EvidenceArtifactService
from clearscout.application.failures import ConflictError, NotFoundError, ValidationError
from clearscout.application.tasks import TaskContext
from clearscout.application.workspaces.records.access import audit_columns
from clearscout.domain import (
    PostingContentAccessStatus,
    PostingContentExtractionMethod,
    PostingIdentityStatus,
    PostingStatus,
    new_id,
    utc_now,
)
from clearscout.domain.identity import url_sha256
from clearscout.domain.staged_facts import StagedListingFacts
from clearscout.infrastructure.artifacts.runtime import RuntimeStorage
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    PendingDiscovery,
    PendingPostingContent,
    Source,
)
from clearscout.infrastructure.web_fetch.ingest import (
    MAXIMUM_USEFUL_CHARACTERS,
    MINIMUM_USEFUL_CHARACTERS,
    strip_injected_instructions,
)

MAX_RESOLUTION_ITEMS = 50


# A bodiless assisted attempt keeps these, since each names the remediation the source needs.
_SOURCE_SPECIFIC_STATUSES = frozenset(
    {
        PostingContentAccessStatus.ACCESS_BLOCKED,
        PostingContentAccessStatus.SESSION_REQUIRED,
        PostingContentAccessStatus.RENDER_REQUIRED,
        PostingContentAccessStatus.POLICY_REFUSED,
    }
)


@dataclass
class _AutomaticResolution:
    """Mutable counters for one deterministic automatic-resolution pass."""

    selected: int
    completed: int = 0
    retained: int = 0
    attempted: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)
    evidence_ids: list[str] = field(default_factory=list)
    withdrawn_ids: list[str] = field(default_factory=list)
    relisted_ids: list[str] = field(default_factory=list)

    def report(self) -> dict[str, Any]:
        return {
            "selected": self.selected,
            "attempted": self.attempted,
            "retained": self.retained,
            "outcomes": self.outcomes,
            "evidence_ids": self.evidence_ids,
            "withdrawn_ids": self.withdrawn_ids,
            "relisted_ids": self.relisted_ids,
        }


class PostingContentResolutionService:
    """Own snapshot binding, deterministic resolution, and append-only evidence."""

    def __init__(
        self,
        database: Database,
        resolver: PostingContentResolver,
        *,
        storage: RuntimeStorage,
        automatic_lanes: int = 1,
    ) -> None:
        """Configure persistence, the deterministic effect port, and artifact storage."""
        if automatic_lanes < 1 or automatic_lanes > 8:
            raise ValueError("Automatic content-resolution lanes must be between 1 and 8.")
        self.database = database
        self.resolver = resolver
        self.automatic_lanes = automatic_lanes
        self.artifacts = EvidenceArtifactService(storage)
        self.current = CurrentPostingContentReader(database, storage=storage)
        self.evidence = ContentEvidenceStore(database, self.artifacts)
        self.reads = PostingReadLanes(resolver, automatic_lanes)

    def resolve_automatic(
        self,
        workspace_id: str,
        pending_discovery_ids: list[str],
        context: TaskContext | None = None,
        *,
        origin: str,
        prefetched: Mapping[str, PrefetchedContent] | None = None,
        require_source_read: Callable[[Session], None] | None = None,
    ) -> dict[str, Any]:
        """Persist current content for every unresolved target without a user batch step.

        A target whose address and identity match a ``prefetched`` read takes that
        read instead of reading the posting again.
        """
        ordered_ids = list(dict.fromkeys(pending_discovery_ids))
        state = _AutomaticResolution(selected=len(ordered_ids))
        unresolved = self._automatic_targets(
            workspace_id,
            ordered_ids,
            state,
            correlation_id=context.task_id if context is not None else None,
        )
        self._resolve_automatic_targets(
            workspace_id,
            unresolved,
            state,
            context,
            origin=origin,
            prefetched=prefetched or {},
            require_source_read=require_source_read,
        )
        self._automatic_progress(context, state, None)
        return state.report()

    def prefetch(
        self,
        workspace_id: str,
        url: str,
        *,
        employer: str,
        title: str,
        task_id: str | None = None,
    ) -> PrefetchedContent:
        """Read one posting ahead of staging its row, keeping a raised error to replay later."""
        try:
            result = self.reads.read_posting(
                url,
                employer=employer,
                title=title,
                identity={"workspace_id": workspace_id, "url_sha256": url_sha256(url)},
                task_id=task_id,
            )
        except Exception as exc:  # noqa: BLE001 - re-raised where the live read would raise
            return PrefetchedContent(url, employer, title, failure=exc)
        return PrefetchedContent(url, employer, title, result=result)

    def _automatic_targets(
        self,
        workspace_id: str,
        ordered_ids: list[str],
        state: _AutomaticResolution,
        *,
        correlation_id: str | None,
    ) -> list[StoredTarget]:
        targets = [
            target
            for offset in range(0, len(ordered_ids), MAX_RESOLUTION_ITEMS)
            for target in self._targets(
                workspace_id, ordered_ids[offset : offset + MAX_RESOLUTION_ITEMS]
            )
        ]
        with self.database.session() as session:
            current = {
                evidence.pending_discovery_id: evidence
                for evidence in session.scalars(
                    select(PendingPostingContent).where(
                        PendingPostingContent.workspace_id == workspace_id,
                        PendingPostingContent.pending_discovery_id.in_(ordered_ids),
                    )
                )
            }
            dismissed_by_body = {
                event.pending_discovery_id
                for event in session.scalars(
                    latest_disposition_statement(workspace_id, ordered_ids)
                )
                if (
                    event.action == "dismiss"
                    and event.origin.startswith("source.")
                    and event.origin.endswith(".body_screen")
                    and event.category in BODY_REFUSAL_CATEGORIES
                )
            }
        unresolved: list[StoredTarget] = []
        for target in targets:
            try:
                retained = self.current.read(workspace_id, target.id)
            except ValidationError:
                retained = None
            if retained is None or (
                target.id in dismissed_by_body
                and current.get(target.id) is not None
                and current[target.id].correlation_id != correlation_id
            ):
                unresolved.append(target)
            else:
                state.retained += 1
                state.completed += 1
        return unresolved

    def _resolve_automatic_targets(
        self,
        workspace_id: str,
        unresolved: list[StoredTarget],
        state: _AutomaticResolution,
        context: TaskContext | None,
        *,
        origin: str,
        prefetched: Mapping[str, PrefetchedContent],
        require_source_read: Callable[[Session], None] | None = None,
    ) -> None:
        evidence: dict[int, str] = {}
        withdrawn: dict[int, str] = {}
        relisted: dict[int, str] = {}

        def store_result(
            position: int, target: StoredTarget, result: ResolvedPostingContent
        ) -> None:
            stored = self.evidence.store(
                workspace_id,
                target,
                result,
                context,
                origin=origin,
                expected_requested_url=target.url,
                require_source_read=require_source_read,
            )
            evidence[position] = stored.id
            state.attempted += 1
            state.completed += 1
            key = result.content_status.value
            state.outcomes[key] = state.outcomes.get(key, 0) + 1
            if withdraws_row(result):
                withdrawn[position] = target.id
            if stored.relisted:
                relisted[position] = target.id
            self._automatic_progress(context, state, target)

        self.reads.resolve_in_lanes(
            workspace_id,
            unresolved,
            context,
            on_started=lambda _position, target: self._automatic_progress(context, state, target),
            on_resolved=store_result,
            prefetched=prefetched,
        )
        state.evidence_ids.extend(evidence[index] for index in sorted(evidence))
        state.withdrawn_ids.extend(withdrawn[index] for index in sorted(withdrawn))
        state.relisted_ids.extend(relisted[index] for index in sorted(relisted))

    def _automatic_progress(
        self,
        context: TaskContext | None,
        state: _AutomaticResolution,
        target: StoredTarget | None,
    ) -> None:
        if context is None:
            return
        context.raise_if_cancelled()
        payload: dict[str, Any] = {
            "phase": "reading_content",
            "completed": state.selected if target is None else state.completed,
            "total": state.selected,
            "item_kind": "posting",
        }
        if target is not None:
            active = min(self.automatic_lanes, state.selected - state.completed)
            payload["item_kind"] = "posting_batch"
            payload["item_label"] = (
                f"Reading up to {active} posting{'s' if active != 1 else ''} in parallel"
            )
        context.progress("resolving_content", payload)

    def preflight(
        self,
        workspace_id: str,
        pending_discovery_ids: list[str],
    ) -> PostingResolutionPreflight:
        """Bind an ordered, unique set of pending identities to their current state."""
        targets = self._targets(workspace_id, pending_discovery_ids)
        return PostingResolutionPreflight(
            targets=tuple(
                PostingResolutionTarget(
                    item.id,
                    item.employer,
                    item.title,
                    item.url,
                    item.content_status,
                    item.fingerprint,
                    item.current_content_id,
                    item.expected_employer,
                    item.expected_title,
                )
                for item in targets
            ),
            snapshot_sha256=_snapshot(targets),
        )

    def resolve_batch(
        self,
        workspace_id: str,
        pending_discovery_ids: list[str],
        snapshot_sha256: str,
        context: TaskContext | None = None,
        *,
        origin: str,
        correlation_id: str | None = None,
        report_progress: bool = True,
    ) -> dict[str, Any]:
        """Resolve every snapshot-bound target and append one durable attempt per item."""
        targets = self._targets(workspace_id, pending_discovery_ids)
        if _snapshot(targets) != snapshot_sha256:
            raise ConflictError("Pending posting content changed after resolution preflight.")
        outcomes: dict[str, int] = {}
        evidence: dict[int, str] = {}
        withdrawn: dict[int, str] = {}
        relisted: dict[int, str] = {}
        kept: dict[int, str] = {}

        def report_started(position: int, target: StoredTarget) -> None:
            if context is not None and report_progress:
                context.progress(
                    "resolving",
                    {
                        "current": position + 1,
                        "total": len(targets),
                        "pending_discovery_id": target.id,
                        "completed_evidence_ids": [evidence[index] for index in sorted(evidence)],
                    },
                )

        def store_result(
            position: int, target: StoredTarget, result: ResolvedPostingContent
        ) -> None:
            stored = self.evidence.store(
                workspace_id,
                target,
                result,
                context,
                origin=origin,
                expected_requested_url=target.url,
                correlation_id=correlation_id,
            )
            evidence[position] = stored.id
            key = result.content_status.value
            outcomes[key] = outcomes.get(key, 0) + 1
            if withdraws_row(result):
                withdrawn[position] = target.id
            if stored.relisted:
                relisted[position] = target.id
            if stored.kept_body:
                kept[position] = target.id

        self.reads.resolve_in_lanes(
            workspace_id,
            targets,
            context,
            on_started=report_started,
            on_resolved=store_result,
        )
        evidence_ids = [evidence[index] for index in sorted(evidence)]
        return {
            "selected": len(targets),
            "completed": len(evidence_ids),
            "outcomes": outcomes,
            "evidence_ids": evidence_ids,
            "withdrawn_ids": [withdrawn[index] for index in sorted(withdrawn)],
            "relisted_ids": [relisted[index] for index in sorted(relisted)],
            "kept_ids": [kept[index] for index in sorted(kept)],
        }

    def _targets(self, workspace_id: str, ids: list[str]) -> list[StoredTarget]:
        if not ids or len(ids) > MAX_RESOLUTION_ITEMS or len(set(ids)) != len(ids):
            raise ValidationError(
                f"Posting resolution requires 1-{MAX_RESOLUTION_ITEMS} unique pending IDs."
            )
        with self.database.session() as session:
            rows = session.scalars(
                select(PendingDiscovery).where(
                    PendingDiscovery.workspace_id == workspace_id,
                    PendingDiscovery.id.in_(ids),
                    PendingDiscovery.archived_at.is_(None),
                    PendingDiscovery.withdrawn_at.is_(None),
                )
            ).all()
            by_id = {row.id: row for row in rows}
            if set(by_id) != set(ids):
                raise NotFoundError("A selected pending discovery is unavailable.")
            corrected = corrected_title_ids(session, workspace_id, ids)
            boards = _listing_boards(session, workspace_id, rows)
            return [_stored_target(by_id[pending_id], corrected, boards) for pending_id in ids]

    def store_prefetched_result(
        self,
        workspace_id: str,
        pending_discovery_id: str,
        result: ResolvedPostingContent,
        context: TaskContext | None,
        *,
        origin: str,
        require_source_read: Callable[[Session], None] | None = None,
    ) -> str:
        """Retain direct publisher content already read by a deterministic workflow."""
        targets = self._targets(workspace_id, [pending_discovery_id])
        if len(targets) != 1:
            raise ConflictError("The verified direct posting is no longer pending.")
        return self.evidence.store(
            workspace_id,
            targets[0],
            result,
            context,
            origin=origin,
            expected_requested_url=result.requested_url,
            require_source_read=require_source_read,
        ).id

    def store_assisted_result(
        self,
        workspace_id: str,
        target: PostingResolutionTarget,
        result: ResolvedPostingContent,
        context: TaskContext | None,
    ) -> StoredEvidence:
        """Append content found for one snapshot-bound assisted target, adopting its address."""
        stored = StoredTarget(
            target.id,
            target.fingerprint,
            target.employer,
            target.title,
            target.url,
            target.current_content_id,
            target.content_status,
            target.expected_employer,
            target.expected_title,
        )
        return self.evidence.store(
            workspace_id,
            stored,
            result,
            context,
            origin="posting.recover_batch",
            expected_requested_url=result.requested_url,
            adopt_read_address=True,
        )

    def store_assisted_failure(
        self,
        workspace_id: str,
        target: PostingResolutionTarget,
        *,
        outcome: str,
        detail: str,
        candidate_url: str | None,
        context: TaskContext | None,
    ) -> str:
        """Retain an unsuccessful assisted attempt and return its evidence id."""
        stored = StoredTarget(
            target.id,
            target.fingerprint,
            target.employer,
            target.title,
            target.url,
            target.current_content_id,
            target.content_status,
            target.expected_employer,
            target.expected_title,
        )
        ambiguous = outcome == "ambiguous"
        if target.content_status in _SOURCE_SPECIFIC_STATUSES:
            content_status = target.content_status
        elif ambiguous:
            content_status = PostingContentAccessStatus.NOT_YET_READ
        else:
            content_status = PostingContentAccessStatus.FETCH_FAILED
        return self.evidence.store(
            workspace_id,
            stored,
            ResolvedPostingContent(
                requested_url=target.url,
                final_url=candidate_url,
                content_status=content_status,
                identity_status=(
                    PostingIdentityStatus.AMBIGUOUS
                    if ambiguous
                    else PostingIdentityStatus.NOT_YET_VERIFIED
                ),
                posting_status=None,
                extraction_method=None,
                fetched_at=utc_now(),
                identity_evidence={"method": "assisted_recovery", "outcome": outcome},
                note=detail,
            ),
            context,
            origin="posting.recover_batch",
            expected_requested_url=target.url,
        ).id

    def store_user_supplied_text(
        self,
        workspace_id: str,
        pending_discovery_id: str,
        content: str,
        *,
        attested_current_exact_posting: bool,
        expected_current_content_id: str | None,
        expected_url: str,
        correlation_id: str | None,
    ) -> str:
        """Attach text a user attests is the current body of this exact posting."""
        if not attested_current_exact_posting:
            raise ValidationError("Current exact-posting attestation is required.")
        sanitized = strip_injected_instructions(content).strip()
        if len(sanitized) < MINIMUM_USEFUL_CHARACTERS:
            raise ValidationError(
                "User-supplied posting text must contain at least "
                f"{MINIMUM_USEFUL_CHARACTERS} useful characters after sanitization."
            )
        if len(sanitized) > MAXIMUM_USEFUL_CHARACTERS:
            raise ValidationError(
                "User-supplied posting text exceeds the "
                f"{MAXIMUM_USEFUL_CHARACTERS:,} character posting limit."
            )
        target = self._targets(workspace_id, [pending_discovery_id])[0]
        if target.current_content_id != expected_current_content_id or target.url != expected_url:
            raise ConflictError(
                "The pending posting changed after its URL and evidence were reviewed."
            )
        return self.evidence.store(
            workspace_id,
            target,
            ResolvedPostingContent(
                requested_url=target.url,
                final_url=target.url,
                content_status=PostingContentAccessStatus.READABLE,
                identity_status=PostingIdentityStatus.VERIFIED,
                posting_status=PostingStatus.ACTIVE,
                extraction_method=PostingContentExtractionMethod.USER_SUPPLIED_TEXT,
                fetched_at=utc_now(),
                content=sanitized.encode("utf-8"),
                identity_evidence={
                    "method": "user_attestation",
                    "attested_current_exact_posting": True,
                    "instructions_removed": sanitized != content.strip(),
                },
                note="A user attached and attested the current posting text.",
            ),
            None,
            origin="pending_posting_content.user_supplied",
            expected_requested_url=target.url,
            actor="local_user",
            correlation_id=correlation_id,
        ).id

    def confirm_current_identity(
        self,
        workspace_id: str,
        pending_discovery_id: str,
        *,
        attested_same_posting: bool,
        expected_current_content_id: str,
        expected_url: str,
        correlation_id: str | None,
    ) -> str:
        """Confirm that retained readable publisher evidence is the staged posting."""
        if not attested_same_posting:
            raise ValidationError("Same-posting identity attestation is required.")
        with self.database.session() as session:
            pending = session.get(PendingDiscovery, pending_discovery_id)
            if (
                pending is None
                or pending.workspace_id != workspace_id
                or pending.archived_at is not None
                or pending.withdrawn_at is not None
                or pending.current_content_id is None
            ):
                raise NotFoundError("The selected pending discovery is unavailable.")
            current = session.get(PendingPostingContent, pending.current_content_id)
            if current is None or current.workspace_id != workspace_id:
                raise ConflictError("The pending posting has no current evidence to confirm.")
            target_url = pending.canonical_url or pending.discovery_url
            if current.id != expected_current_content_id or target_url != expected_url:
                raise ConflictError(
                    "The pending posting changed after its identity evidence was reviewed."
                )
            if (
                PostingContentAccessStatus(current.content_status)
                is not PostingContentAccessStatus.READABLE
                or current.artifact_id is None
                or current.extraction_method is None
                or current.content_sha256 is None
                or current.posting_status is None
                or PostingStatus(current.posting_status) is not PostingStatus.ACTIVE
            ):
                raise ConflictError("Only retained readable active evidence can be confirmed.")
            if current.requested_url != target_url:
                raise ConflictError("The retained evidence belongs to a different posting URL.")
            if PostingIdentityStatus(current.identity_status) is PostingIdentityStatus.VERIFIED:
                raise ConflictError("The current posting identity is already verified.")
            body = self.evidence.retained_body(session, workspace_id, current)
            # A page of job cards is not this posting whatever a person attests, so
            # no attestation turns one into verified evidence.
            if lists_many_postings(body.decode("utf-8", errors="replace")):
                raise ConflictError(f"{LISTING_PAGE_NOTE} It cannot be confirmed as this posting.")
            identity_evidence = {
                **current.identity_evidence,
                "user_attested_same_posting": True,
                "prior_identity_status": PostingIdentityStatus(current.identity_status).value,
            }
            encoded = json.dumps(
                identity_evidence, ensure_ascii=True, separators=(",", ":")
            ).encode("ascii")
            if len(encoded) > MAX_IDENTITY_EVIDENCE_BYTES:
                raise ValidationError(
                    "Confirmed posting identity evidence exceeds the retained evidence limit."
                )
            evidence_id = new_id()
            record = PendingPostingContent(
                id=evidence_id,
                workspace_id=workspace_id,
                pending_discovery_id=pending.id,
                artifact_id=current.artifact_id,
                requested_url=current.requested_url,
                final_url=current.final_url,
                requested_url_sha256=current.requested_url_sha256,
                final_url_sha256=current.final_url_sha256,
                content_sha256=current.content_sha256,
                place_blind_digest=current.place_blind_digest,
                content_status=PostingContentAccessStatus.READABLE,
                identity_status=PostingIdentityStatus.VERIFIED,
                posting_status=PostingStatus.ACTIVE,
                extraction_method=current.extraction_method,
                fetched_at=current.fetched_at,
                http_status=current.http_status,
                publisher_markup_present=current.publisher_markup_present,
                identity_evidence=identity_evidence,
                supersedes_content_id=current.id,
                **audit_columns(
                    "local_user", "pending_posting_content.identity_confirmed", correlation_id
                ),
            )
            session.add(record)
            session.flush()
            pending.current_content_id = evidence_id
            screen = body_screen(pending, body, workspace_id, self.database)
            take_body_pay(pending, screen.pay_reading)
            self.evidence.link_regional_variant(
                session,
                workspace_id,
                pending,
                identity_status=PostingIdentityStatus.VERIFIED,
                posting_status=PostingStatus.ACTIVE,
                content_sha256=current.content_sha256,
                place_blind_digest=current.place_blind_digest,
                body_is_useful=useful_body(body),
                body_refused=screen.refusal is not None,
            )
            pending.content_status = PostingContentAccessStatus.READABLE
            pending.content_note = (
                "A user confirmed that the retained publisher body is this posting."
            )
            return evidence_id


def _snapshot(targets: list[StoredTarget]) -> str:
    payload = [
        {
            "id": item.id,
            "fingerprint": item.fingerprint,
            "employer": item.employer,
            "title": item.title,
            "url": item.url,
            "current_content_id": item.current_content_id,
            "content_status": item.content_status.value,
            "expected_employer": item.expected_employer,
            "expected_title": item.expected_title,
        }
        for item in targets
    ]
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _listing_boards(
    session: Session, workspace_id: str, rows: Sequence[PendingDiscovery]
) -> dict[str, str]:
    """Return the address each row's listing source is scanned at, by source id."""
    source_ids = {row.source_id for row in rows if row.source_id}
    if not source_ids:
        return {}
    sources = session.scalars(
        select(Source).where(Source.workspace_id == workspace_id, Source.id.in_(source_ids))
    )
    return {
        source.id: source_read_address(source.url, source.resolved_url)[0] for source in sources
    }


_UNSTATED_EMPLOYER = frozenset({"source.registered_employer", "source.employer_hint"})


def _stored_target(
    row: PendingDiscovery, corrected_ids: set[str], boards: Mapping[str, str]
) -> StoredTarget:
    """Bind one pending row, expecting only publisher-trusted identity on a re-read."""
    facts = StagedListingFacts.from_json(row.staged_facts)
    publisher = facts.publisher_vouched
    # A registered or hinted employer name is ours, not the publisher's; its tenant
    # label ("AcmePRD") would contradict it.
    stated_employer = publisher and facts.employer.provenance not in _UNSTATED_EMPLOYER
    return StoredTarget(
        row.id,
        row.fingerprint,
        row.employer,
        row.title,
        row.canonical_url or row.discovery_url,
        row.current_content_id,
        PostingContentAccessStatus(row.content_status),
        row.employer if stated_employer else "",
        # A hand-corrected title is a person's hint, not publisher identity.
        row.title if publisher and row.id not in corrected_ids else "",
        boards.get(row.source_id or "", ""),
        unread_person_add=(
            staged_by_person(row.reason)
            and row.current_content_id is None
            and row.content_status == PostingContentAccessStatus.NOT_YET_READ
        ),
    )
