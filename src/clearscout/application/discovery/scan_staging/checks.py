"""Staged rows a fresh publisher check refreshes or withdraws, and saved closures applied."""

from __future__ import annotations

from dataclasses import replace

from sqlalchemy import select

from clearscout.application.discovery.board_slugs import publisher_spelling_of_tenant
from clearscout.application.discovery.pending import (
    active_task_targets,
    corrected_title_ids,
    dismissed_pending_discovery_ids,
    release_variants_of_withdrawn,
)
from clearscout.application.discovery.scan_staging.families import (
    clear_regional_variant_family_if_identity_changed,
    record_relisted,
)
from clearscout.application.discovery.scan_staging.publisher_facts import (
    apply_publisher_check,
    name_employer_from_url,
)
from clearscout.application.discovery.source_lifecycle import employer_name_is_authoritative
from clearscout.application.discovery.source_reading import PostingCheck
from clearscout.application.failures import ConflictError, NotFoundError, ValidationError
from clearscout.application.job_search.models import (
    PendingClosureReconcileResponse,
    PendingClosureReconcileResult,
)
from clearscout.application.workspaces.records.access import audit_columns
from clearscout.domain import PostingStatus, utc_now
from clearscout.domain.staged_facts import ListingIdentityTrust, PublishedFact, StagedListingFacts
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    AuditEvent,
    Employer,
    PendingDiscovery,
    PendingPostingContent,
    Source,
)
from clearscout.shared.text import sanitize


class PublisherCheckWrites:
    """Refresh or withdraw one staged row from a publisher check, or apply saved closures."""

    def __init__(self, database: Database) -> None:
        """Bind the writes to one durable database."""
        self.database = database

    def refresh_pending_discovery(
        self,
        workspace_id: str,
        pending_id: str,
        publisher_check: PostingCheck,
        *,
        actor: str,
        origin: str,
        correlation_id: str | None,
    ) -> None:
        """Replace mutable staged facts with a fresh readable publisher check.

        Raises ConflictError when a paid discovery task is queued or running for
        the row; a mid-flight extraction would otherwise overwrite the refresh.
        """
        if publisher_check.status != "readable":
            raise ValidationError("Publisher facts can be refreshed only from a readable check.")
        with self.database.session() as session:
            pending = session.scalar(
                select(PendingDiscovery).where(
                    PendingDiscovery.workspace_id == workspace_id,
                    PendingDiscovery.id == pending_id,
                )
            )
            if pending is None:
                raise NotFoundError(f"Pending discovery '{pending_id}' does not exist.")
            if pending.archived_at is not None:
                raise ConflictError("Only an active pending discovery can refresh publisher facts.")
            dismissed = set(session.scalars(dismissed_pending_discovery_ids(workspace_id)))
            if pending_id in dismissed:
                raise ConflictError("A dismissed pending discovery cannot refresh publisher facts.")
            if active_task_targets(session, workspace_id, pending_id):
                raise ConflictError(
                    "A guided search or discovery evaluation is in flight for this row; "
                    "wait for it to finish before refreshing publisher facts."
                )
            # Only publisher evidence withdraws a row, so a readable read reopens it.
            record_relisted(
                session,
                pending,
                action="pending_discovery.relisted_on_refresh",
                workspace_id=workspace_id,
                actor=actor,
                origin=origin,
                correlation_id=correlation_id,
                dismissed=dismissed,
            )
            replaced = {
                "employer": pending.employer,
                "title": pending.title,
                "location": pending.location,
            }
            prior_employer = StagedListingFacts.from_json(pending.staged_facts).employer
            previously_published_employer = (
                prior_employer.provenance == "oracle.employer"
                and pending.employer in prior_employer.values
            )
            if (
                publisher_check.platform == "oracle"
                and not publisher_check.employer
                and not previously_published_employer
            ):
                registration = session.execute(
                    select(Employer.name, Source.origin)
                    .join(Source, Source.employer_id == Employer.id)
                    .where(
                        Source.id == pending.source_id,
                        Source.workspace_id == workspace_id,
                        Source.source_type == "employer_board",
                        Source.archived_at.is_(None),
                        Employer.workspace_id == workspace_id,
                        Employer.archived_at.is_(None),
                    )
                ).first()
                if registration and registration[0]:
                    registered_employer, registered_origin = registration
                    pending.employer = sanitize(registered_employer)[:300]
                    publisher_check = replace(
                        publisher_check,
                        facts=replace(
                            publisher_check.facts,
                            identity_trust=(
                                publisher_check.facts.identity_trust
                                if employer_name_is_authoritative(registered_origin)
                                else ListingIdentityTrust.UNVERIFIED_HINT
                            ),
                            employer=PublishedFact(
                                values=(pending.employer,),
                                provenance="source.registered_employer",
                                complete=True,
                            ),
                        ),
                    )
            spelled = (
                ""
                if publisher_check.employer
                else publisher_spelling_of_tenant(pending.discovery_url, publisher_check.text)
            )
            if spelled and not pending.employer:
                # The publisher's text printed this spelling, so it vouches as a stated name.
                publisher_check = replace(publisher_check, employer=spelled)
            # A name from a tenant slug is a hint, so it is filled before the check
            # decides identity trust.
            name_employer_from_url(pending, pending.discovery_url)
            # The hint stands as the identity going in, so a derived name on its own
            # does not unlink a regional family.
            prior_identity = (pending.employer, pending.title)
            apply_publisher_check(
                pending,
                publisher_check,
                read_on=utc_now().date(),
                keep_title=pending_id in corrected_title_ids(session, workspace_id, [pending_id]),
            )
            clear_regional_variant_family_if_identity_changed(session, pending, prior_identity)
            session.add(
                AuditEvent(
                    workspace_id=workspace_id,
                    action="pending_discovery.publisher_facts_refreshed",
                    entity_type="pending_discovery",
                    entity_id=pending_id,
                    details={
                        "discovery_url": pending.discovery_url,
                        "platform": publisher_check.platform,
                        "replaced": replaced,
                    },
                    **audit_columns(actor, origin, correlation_id),
                )
            )
            session.flush()

    def withdraw_pending_discovery(
        self,
        workspace_id: str,
        pending_id: str,
        *,
        note: str,
        actor: str,
        origin: str,
        correlation_id: str | None,
    ) -> None:
        """Withdraw one unarchived row a fresh publisher check reports as closed.

        A row already withdrawn is left as it is.
        """
        with self.database.session() as session:
            pending = session.scalar(
                select(PendingDiscovery).where(
                    PendingDiscovery.workspace_id == workspace_id,
                    PendingDiscovery.id == pending_id,
                )
            )
            if pending is None:
                raise NotFoundError(f"Pending discovery '{pending_id}' does not exist.")
            if pending.archived_at is not None:
                raise ConflictError("Only an active pending discovery can be withdrawn.")
            if pending.withdrawn_at is not None:
                return
            if active_task_targets(session, workspace_id, pending_id):
                raise ConflictError(
                    "A guided search or discovery evaluation is in flight for this row; "
                    "wait for it to finish before withdrawing it."
                )
            pending.withdrawn_at = utc_now()
            session.add(
                AuditEvent(
                    workspace_id=workspace_id,
                    action="pending_discovery.withdrawn_on_refresh",
                    entity_type="pending_discovery",
                    entity_id=pending_id,
                    details={"discovery_url": pending.discovery_url, "note": note[:500]},
                    **audit_columns(actor, origin, correlation_id),
                )
            )
            release_variants_of_withdrawn(
                session,
                workspace_id,
                [pending_id],
                actor=actor,
                origin=origin,
                correlation_id=correlation_id,
            )
            session.flush()

    def reconcile_saved_closures(
        self,
        workspace_id: str,
        pending_discovery_ids: list[str],
        *,
        actor: str,
        origin: str,
        correlation_id: str | None,
    ) -> PendingClosureReconcileResponse:
        """Apply only the current saved publisher closure evidence, without a new read."""
        with self.database.session() as session:
            rows = session.scalars(
                select(PendingDiscovery).where(
                    PendingDiscovery.workspace_id == workspace_id,
                    PendingDiscovery.id.in_(pending_discovery_ids),
                    PendingDiscovery.archived_at.is_(None),
                )
            ).all()
            by_id = {row.id: row for row in rows}
            missing = [
                pending_id for pending_id in pending_discovery_ids if pending_id not in by_id
            ]
            if missing:
                raise NotFoundError(f"Pending discovery '{missing[0]}' does not exist.")
            results: list[PendingClosureReconcileResult] = []
            for pending_id in pending_discovery_ids:
                pending = by_id[pending_id]
                evidence = (
                    session.get(PendingPostingContent, pending.current_content_id)
                    if pending.current_content_id
                    else None
                )
                if (
                    evidence is None
                    or evidence.workspace_id != workspace_id
                    or evidence.pending_discovery_id != pending.id
                    or evidence.posting_status != PostingStatus.CLOSED
                ):
                    results.append(
                        PendingClosureReconcileResult(
                            pending_discovery_id=pending.id,
                            outcome="not_currently_closed",
                        )
                    )
                    continue
                if pending.withdrawn_at is not None:
                    results.append(
                        PendingClosureReconcileResult(
                            pending_discovery_id=pending.id,
                            outcome="unchanged",
                            evidence_id=evidence.id,
                            observed_at=evidence.fetched_at,
                        )
                    )
                    continue
                pending.withdrawn_at = evidence.fetched_at
                session.add(
                    AuditEvent(
                        workspace_id=workspace_id,
                        action="pending_discovery.saved_closure_applied",
                        entity_type="pending_discovery",
                        entity_id=pending.id,
                        details={
                            "evidence_id": evidence.id,
                            "observed_at": evidence.fetched_at.isoformat(),
                        },
                        **audit_columns(actor, origin, correlation_id),
                    )
                )
                results.append(
                    PendingClosureReconcileResult(
                        pending_discovery_id=pending.id,
                        outcome="applied",
                        evidence_id=evidence.id,
                        observed_at=evidence.fetched_at,
                    )
                )
            release_variants_of_withdrawn(
                session,
                workspace_id,
                [result.pending_discovery_id for result in results if result.outcome == "applied"],
                actor=actor,
                origin=origin,
                correlation_id=correlation_id,
            )
            session.flush()
            return PendingClosureReconcileResponse(results=results)
