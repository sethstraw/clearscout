"""Read the durable facts search planning needs before it can propose work."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from tailorcv.application.config import Settings
from tailorcv.application.discovery.board_slugs import board_identity_key
from tailorcv.application.discovery.dispositions import dismissed_pending_ids
from tailorcv.application.discovery.posting_identity import posting_identity_keys
from tailorcv.application.failures import ValidationError
from tailorcv.application.master_cv import MasterCvService
from tailorcv.application.master_cv.cv import CvInventory, parse_cv
from tailorcv.application.workspaces import WorkspaceSettingsService
from tailorcv.domain import PostingStatus
from tailorcv.infrastructure.artifacts.runtime import RuntimeStorage
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    JobPosting,
    JobSearchParameterSet,
    PendingDiscovery,
    PostingObservation,
    PostingUrl,
    Source,
)

_NO_PARAMETERS = (
    "Search planning needs saved search parameters for this workspace. "
    "Save them, then plan the search again."
)


@dataclass(frozen=True)
class _KnownSearchIdentities:
    """Existing source and posting identities a guided run must not rediscover."""

    source_roots: frozenset[str]
    posting_fingerprints: frozenset[str]


@dataclass(frozen=True)
class SearchPlanningInputs:
    """One coherent parameter/CV snapshot used for plan identity and execution."""

    settings: Settings
    inventory: CvInventory
    master_cv_version_id: str
    search_parameter_set_id: str
    search_parameter_sha256: str


def known_identities_in(session: Session, workspace_id: str) -> _KnownSearchIdentities:
    """Project source and posting identities inside the caller's transaction."""
    source_rows = session.execute(
        select(Source.url, Source.resolved_url).where(Source.workspace_id == workspace_id)
    ).all()
    source_roots = frozenset(board_identity_key(url) for row in source_rows for url in row if url)
    dismissed = dismissed_pending_ids(session, workspace_id)
    pending_rows = session.execute(
        select(
            PendingDiscovery.fingerprint,
            PendingDiscovery.canonical_url,
            PendingDiscovery.discovery_url,
        ).where(
            PendingDiscovery.workspace_id == workspace_id,
            or_(
                and_(
                    PendingDiscovery.archived_at.is_(None),
                    PendingDiscovery.withdrawn_at.is_(None),
                ),
                PendingDiscovery.id.in_(dismissed),
            ),
        )
    ).all()
    pending = frozenset(row.fingerprint for row in pending_rows) | frozenset(
        key
        for row in pending_rows
        for url in (row.canonical_url, row.discovery_url)
        if url
        for key in posting_identity_keys(url)
    )
    latest_posting_status = (
        select(PostingObservation.posting_status)
        .where(PostingObservation.job_posting_id == JobPosting.id)
        .order_by(PostingObservation.observed_at.desc(), PostingObservation.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    tracked = frozenset(
        key
        for url in session.scalars(
            select(PostingUrl.normalized_url)
            .join(JobPosting, PostingUrl.job_posting_id == JobPosting.id)
            .where(
                JobPosting.workspace_id == workspace_id,
                or_(
                    latest_posting_status.is_(None),
                    latest_posting_status != PostingStatus.CLOSED,
                ),
            )
        )
        for key in posting_identity_keys(url)
    )
    return _KnownSearchIdentities(
        source_roots=source_roots,
        posting_fingerprints=pending | tracked,
    )


class SearchPlanningQueries:
    """Read the workspace state used to prepare and expand a planned search."""

    def __init__(self, database: Database, storage: RuntimeStorage) -> None:
        """Bind the durable state that search planning reads."""
        self.database = database
        self.settings = WorkspaceSettingsService(database)
        self.master = MasterCvService(database, storage)

    def inputs(self, workspace_id: str) -> tuple[Settings, CvInventory, str]:
        """Load the settings a run reads and the record it describes."""
        inputs = self.planning_inputs(workspace_id)
        return inputs.settings, inputs.inventory, inputs.master_cv_version_id

    def planning_inputs(self, workspace_id: str) -> SearchPlanningInputs:
        """Load one parameter document with the immutable identity that supplied it."""
        snapshot = self.settings.snapshot(workspace_id)
        version = self.master.read_version(workspace_id)
        if snapshot.search_parameter_set_id is None:
            raise ValidationError(_NO_PARAMETERS)
        with self.database.session() as session:
            parameter_set = session.get(JobSearchParameterSet, snapshot.search_parameter_set_id)
            if parameter_set is None or parameter_set.workspace_id != workspace_id:
                raise ValidationError(_NO_PARAMETERS)
        with tempfile.TemporaryDirectory(prefix="tailorcv-search-") as temporary:
            path = Path(temporary) / "master.md"
            path.write_text(version.content, encoding="utf-8")
            inventory = parse_cv(path)
        return SearchPlanningInputs(
            settings=snapshot.settings,
            inventory=inventory,
            master_cv_version_id=version.id,
            search_parameter_set_id=parameter_set.id,
            search_parameter_sha256=parameter_set.content_sha256,
        )

    def known_identities(self, workspace_id: str) -> _KnownSearchIdentities:
        """Project source choices and posting deduplication before provider work."""
        with self.database.session() as session:
            return known_identities_in(session, workspace_id)
