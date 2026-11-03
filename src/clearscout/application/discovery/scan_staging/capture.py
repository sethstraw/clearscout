"""Listing bytes retained as artifacts of the row they were staged into."""

from __future__ import annotations

import hashlib
from dataclasses import replace

from sqlalchemy import select
from sqlalchemy.orm import Session

from clearscout.application.discovery.posting_refresh import PostingRefreshService
from clearscout.application.discovery.source_reading import SourceListing
from clearscout.application.workspaces.records.access import audit_columns
from clearscout.domain import new_id
from clearscout.domain.artifacts import ArtifactLogicalType, PostingSource
from clearscout.domain.staged_facts import StagedListingFacts
from clearscout.infrastructure.persistence.models import Artifact, PendingDiscovery


class ListingCapture:
    """Retain a listing's text and source bytes on the row it was staged into."""

    def __init__(self, postings: PostingRefreshService) -> None:
        """Bind capture to the posting refresh whose artifact store keeps the bytes."""
        self.postings = postings

    def capture_listing_description(
        self,
        session: Session,
        workspace_id: str,
        pending: PendingDiscovery,
        listing: SourceListing,
        *,
        correlation_id: str | None,
    ) -> None:
        """Retain listing bytes and bind observed dates to their actual representations."""
        closing_date = listing.facts.closing_date
        description = listing.published_description.strip()
        if closing_date is None and (pending.listing_artifact_id is not None or not description):
            return
        # Files can outlive their database links, so each capture owns a fresh destination.
        directory = f"{workspace_id}/pending-listing/{pending.id}/{new_id()}"
        provenance = {"source": "discovery.listing_capture", "discovery_url": listing.url}
        audit = audit_columns("worker", "discovery.listing_capture", correlation_id)
        body_artifact_id = None
        if description:
            artifact = session.scalars(
                select(Artifact).where(
                    Artifact.workspace_id == workspace_id,
                    Artifact.related_entity_type == "pending_discovery",
                    Artifact.related_entity_id == pending.id,
                    Artifact.logical_type == "posting.listing.text",
                    Artifact.integrity_status == "verified",
                    Artifact.sha256 == hashlib.sha256(description.encode("utf-8")).hexdigest(),
                )
            ).first()
            if artifact is None:
                artifact = self.postings.artifacts.store(
                    session,
                    workspace_id=workspace_id,
                    logical_type="posting.listing.text",
                    content=description.encode("utf-8"),
                    relative_path=f"{directory}/listing.txt",
                    mime_type="text/plain",
                    related_entity_type="pending_discovery",
                    related_entity_id=pending.id,
                    provenance=provenance,
                    audit=audit,
                )
            body_artifact_id = artifact.id
            if pending.listing_artifact_id is None:
                pending.listing_artifact_id = artifact.id
        source_artifacts: dict[ArtifactLogicalType, str] = {}
        if listing.source is not None:
            source_artifacts = self._capture_listing_source(
                session,
                workspace_id,
                pending,
                listing.source,
                directory=directory,
                source_url=listing.url,
                correlation_id=correlation_id,
            )
        if closing_date is not None:
            facts = StagedListingFacts.from_json(pending.staged_facts)
            pending.staged_facts = replace(
                facts,
                closing_date=closing_date.with_artifacts(
                    body_artifact_id=body_artifact_id,
                    publisher_artifact_id=(
                        source_artifacts.get("posting.source.json")
                        or source_artifacts.get("posting.source.xml")
                        or source_artifacts.get("posting.source.html")
                    ),
                ),
            ).as_json()

    def _capture_listing_source(
        self,
        session: Session,
        workspace_id: str,
        pending: PendingDiscovery,
        source: PostingSource,
        *,
        directory: str,
        source_url: str,
        correlation_id: str | None,
    ) -> dict[ArtifactLogicalType, str]:
        """Reuse identical retained source bytes and capture each missing representation."""
        retained = {
            (artifact.logical_type, artifact.sha256): artifact.id
            for artifact in session.scalars(
                select(Artifact).where(
                    Artifact.workspace_id == workspace_id,
                    Artifact.related_entity_type == "pending_discovery",
                    Artifact.related_entity_id == pending.id,
                    Artifact.integrity_status == "verified",
                )
            )
        }
        representations: tuple[tuple[ArtifactLogicalType, str], ...] = (
            ("posting.source.json", source.record),
            ("posting.body.html", source.description_markup),
            ("posting.source.html", source.page_html),
            ("posting.source.xml", source.item_xml),
        )
        artifact_ids: dict[ArtifactLogicalType, str] = {
            kind: artifact_id
            for kind, content in representations
            if content
            and (
                artifact_id := retained.get(
                    (kind, hashlib.sha256(content.encode("utf-8")).hexdigest())
                )
            )
        }
        missing = replace(
            source,
            record="" if "posting.source.json" in artifact_ids else source.record,
            description_markup=""
            if "posting.body.html" in artifact_ids
            else source.description_markup,
            page_html="" if "posting.source.html" in artifact_ids else source.page_html,
            item_xml="" if "posting.source.xml" in artifact_ids else source.item_xml,
        )
        artifact_ids.update(
            self.postings.artifacts.store_posting_source(
                session,
                workspace_id=workspace_id,
                directory=directory,
                source=missing,
                related_entity_type="pending_discovery",
                related_entity_id=pending.id,
                provenance={
                    "source": "discovery.listing_capture",
                    "discovery_url": source_url,
                },
                audit=audit_columns("worker", "discovery.listing_capture", correlation_id),
            )
        )
        return artifact_ids
