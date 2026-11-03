"""Append one posting observation per read, and the worker adapters that drive it.

A refresh never edits prior evidence: it stores the fetched body as a new
artifact, writes a new observation and diff, and refuses when another
observation arrived while it was reading.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from difflib import unified_diff
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from clearscout.application import job_search
from clearscout.application.discovery.paid_task import billed_run
from clearscout.application.discovery.posting_fetch import FetchedPosting, PostingFetcher
from clearscout.application.discovery.posting_queries import (
    latest_observation_id,
    same_body_eligibility,
)
from clearscout.application.discovery.refresh_decision import PreviousObservation, decide_refresh
from clearscout.application.documents.evidence_artifacts import EvidenceArtifactService
from clearscout.application.failures import ConflictError, NotFoundError, ValidationError
from clearscout.application.job_search.models import JobListQuery
from clearscout.application.job_search.posting_names import rename_posting
from clearscout.application.job_search.postings import RequirementRulingService
from clearscout.application.job_search.postings.rulings import (
    DisputeLike,
    cited_in_body,
    dispute_block,
    reverse_ruling,
)
from clearscout.application.tailoring.stages import jobspec
from clearscout.application.tasks import (
    BatchStepContext,
    PostingRefreshBatchPayload,
    PostingRefreshFilter,
    TaskContext,
)
from clearscout.application.workspaces import WorkspaceSettingsService
from clearscout.application.workspaces.records.access import audit_columns
from clearscout.domain import new_id, utc_now
from clearscout.domain.artifacts import ArtifactLogicalType, PostingSource
from clearscout.domain.identity import normalize_text, normalize_url
from clearscout.domain.records import PostingRefreshOutcome
from clearscout.domain.rulings import Ruling
from clearscout.domain.statements import derived_proposal_identity
from clearscout.infrastructure.artifacts.runtime import RuntimeStorage
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    Artifact,
    EligibilityRecord,
    JobPosting,
    PostingObservation,
    PostingObservationDiff,
    PostingUrl,
    SavedView,
    Workspace,
)
from clearscout.infrastructure.providers.base import Provider
from clearscout.infrastructure.providers.client import Client
from clearscout.infrastructure.web_fetch.ingest import JDText
from clearscout.infrastructure.web_fetch.ingest import url as url_ingest
from clearscout.shared.errors import ClearscoutError

_REFRESHED = PostingRefreshOutcome.REFRESHED.value
_CLOSED = PostingRefreshOutcome.WITHDRAWN_AS_CLOSED.value
_UNVERIFIED = PostingRefreshOutcome.UNVERIFIED.value


class PostingRefreshService:
    """Write a new observation and diff without mutating prior evidence."""

    def __init__(
        self,
        database: Database,
        *,
        storage: RuntimeStorage,
        fetcher: PostingFetcher,
    ) -> None:
        """Configure durable state, artifact storage, and a replaceable fetch seam."""
        self.database = database
        self.storage = storage
        self.artifacts = EvidenceArtifactService(storage)
        self.fetcher = fetcher
        self.rulings = RequirementRulingService(database)
        self.settings = WorkspaceSettingsService(database)

    def _store_read(
        self,
        session: Session,
        *,
        workspace_id: str,
        observation_id: str,
        source_url: str,
        body: bytes,
        source: PostingSource | None,
        body_artifact_id: str | None = None,
    ) -> tuple[str, dict[ArtifactLogicalType, str]]:
        """Store this read's body, and what it was converted from beside it."""
        audit = {"created_by": "worker", "origin": "posting.refresh"}
        directory = f"{workspace_id}/postings/{observation_id}"
        provenance = {"source": "posting.refresh", "source_url": source_url}
        if body_artifact_id is None:
            artifact = self.artifacts.store(
                session,
                workspace_id=workspace_id,
                logical_type="posting.body.text",
                content=body,
                relative_path=f"{directory}/body.txt",
                mime_type="text/plain",
                related_entity_type="posting_observation",
                related_entity_id=observation_id,
                provenance=provenance,
                audit=audit,
            )
            body_artifact_id = artifact.id
        source_artifact_ids: dict[ArtifactLogicalType, str] = {}
        if source is not None:
            source_artifact_ids = self.artifacts.store_posting_source(
                session,
                workspace_id=workspace_id,
                directory=directory,
                source=source,
                related_entity_type="posting_observation",
                related_entity_id=observation_id,
                provenance=provenance,
                audit=audit,
            )
        return body_artifact_id, source_artifact_ids

    def refresh(
        self,
        workspace_id: str,
        job_posting_id: str,
        context: TaskContext | None = None,
        *,
        title_hint: str = "",
        location_hint: str = "",
        board_url: str = "",
        client: Client | None = None,
        prefetched: FetchedPosting | None = None,
        require_source_read: Callable[[Session], None] | None = None,
    ) -> dict[str, Any]:
        """Refresh once with checkpoints before and after every external effect.

        With both ``client`` and ``guard`` supplied, and the workspace flag on,
        the new body is re-extracted once and a dispute may be marked reversed.
        """
        _raise_if_cancelled(context)
        previous = self._previous(workspace_id, job_posting_id)
        _progress(
            context,
            "fetching",
            {"job_posting_id": job_posting_id, "url": previous.source_url},
        )
        fetched = self._read_for_refresh(
            workspace_id, job_posting_id, previous.source_url, board_url, prefetched
        )
        _raise_if_cancelled(context)
        if fetched.posting_status == "unverified":
            return {
                "job_posting_id": job_posting_id,
                PostingRefreshOutcome.UNVERIFIED.value: True,
                "reason": fetched.verification_note
                or "The publisher could not verify this posting.",
                "model_calls_made": 0,
            }
        decision = decide_refresh(previous, fetched, title_hint, location_hint)
        fetched = decision.fetched
        new_body = decision.body
        body_bytes = decision.body_bytes
        new_sha = decision.content_sha256
        facts = decision.facts
        unchanged = decision.content_unchanged
        observation_id = new_id()
        body_diff = "".join(
            unified_diff(
                previous.body.splitlines(keepends=True),
                new_body.splitlines(keepends=True),
                fromfile=previous.observation_id or "",
                tofile=observation_id,
            )
        )
        changes = decision.changes
        # Refuse a concurrent-observation race before billing a paid re-extraction.
        with self.database.session() as session:
            latest_id = latest_observation_id(session, job_posting_id)
        if latest_id != previous.observation_id:
            raise ConflictError("A newer posting observation arrived during this refresh.")
        reextraction = _maybe_reextract(
            self.settings,
            self.rulings,
            workspace_id=workspace_id,
            job_posting_id=job_posting_id,
            body=new_body,
            source_url=previous.source_url,
            # A first read has nothing to dispute, so it never bills a re-extraction.
            client=client if previous.observation_id is not None else None,
        )
        _progress(
            context,
            "persisting",
            {
                "job_posting_id": job_posting_id,
                "content_unchanged": unchanged,
                "model_calls_made": reextraction.model_calls,
            },
        )
        _raise_if_cancelled(context)
        with self.database.exclusive_session() as session:
            if require_source_read is not None:
                require_source_read(session)
            self._require_prefetched_read(session, workspace_id, job_posting_id, prefetched)
            if latest_observation_id(session, job_posting_id) != previous.observation_id:
                raise ConflictError("A newer posting observation arrived during this refresh.")
            artifact_id = previous.artifact_id
            date = fetched.publisher_closing_date
            source_artifact_ids: dict[ArtifactLogicalType, str] = {}
            retains_publisher_date = bool(date and date.from_publisher_record)
            if fetched.posting_status != "closed" and (
                not unchanged
                or retains_publisher_date
                or changes["publisher_closing_date"]["changed"]
            ):
                artifact_id, source_artifact_ids = self._store_read(
                    session,
                    workspace_id=workspace_id,
                    observation_id=observation_id,
                    source_url=(
                        prefetched.final_url if prefetched is not None else previous.source_url
                    ),
                    body=body_bytes,
                    source=fetched.source,
                    body_artifact_id=previous.artifact_id if unchanged else None,
                )
            retained_date = previous.publisher_closing_date
            if fetched.posting_status != "closed" and date is not None:
                retained_date = date.with_artifacts(
                    body_artifact_id=artifact_id,
                    publisher_artifact_id=(
                        source_artifact_ids.get("posting.source.json")
                        or source_artifact_ids.get("posting.source.html")
                        or source_artifact_ids.get("posting.source.xml")
                    ),
                ).as_json()
            changes["publisher_closing_date"]["after"] = retained_date
            observation = PostingObservation(
                id=observation_id,
                job_posting_id=job_posting_id,
                observed_at=fetched.fetched_at,
                posting_status=fetched.posting_status,
                location=facts.location,
                residence_rule=facts.residence_rule,
                employment_type=facts.employment_type,
                payroll_arrangement=facts.payroll_arrangement,
                body_artifact_id=artifact_id,
                extraction_method=(
                    fetched.method
                    if fetched.posting_status == "closed"
                    or fetched.publisher_markup_present is True
                    else "refresh_hash_unchanged"
                    if unchanged
                    else fetched.method
                ),
                fetched_at=fetched.fetched_at,
                http_status=fetched.http_status,
                content_sha256=new_sha,
                robots_basis=fetched.robots_basis,
                publisher_closing_date=retained_date,
                created_by="worker",
                origin="posting.refresh",
            )
            session.add(observation)
            session.flush()
            if unchanged:
                self._carry_forward_eligibility(session, observation)
            job = session.get(JobPosting, job_posting_id)
            if job is not None:
                rename_posting(
                    session,
                    job,
                    facts.title,
                    audit=audit_columns(
                        "worker", "posting.refresh", getattr(context, "task_id", None)
                    ),
                    reason="a fresh publisher read states this title",
                )
            diff_id = None
            if previous.observation_id is not None:
                record = PostingObservationDiff(
                    workspace_id=workspace_id,
                    job_posting_id=job_posting_id,
                    from_observation_id=previous.observation_id,
                    to_observation_id=observation.id,
                    changes=changes,
                    body_diff=body_diff,
                    created_by="worker",
                    origin="posting.refresh",
                )
                session.add(record)
                session.flush()
                diff_id = record.id
            _apply_reextraction(
                session,
                workspace_id=workspace_id,
                job_posting_id=job_posting_id,
                observation_id=observation_id,
                body=new_body,
                reextraction=reextraction,
                correlation_id=getattr(context, "task_id", None),
            )
        _raise_if_cancelled(context)
        changed_fields = [
            key
            for key, value in changes.items()
            if isinstance(value, dict) and value.get("changed")
        ]
        return {
            "job_posting_id": job_posting_id,
            PostingRefreshOutcome.UNVERIFIED.value: False,
            PostingRefreshOutcome.WITHDRAWN_AS_CLOSED.value: fetched.posting_status == "closed",
            "observation_id": observation_id,
            "previous_observation_id": previous.observation_id,
            "diff_id": diff_id,
            "content_unchanged": unchanged,
            "unchanged_since": (
                previous.fetched_at.isoformat() if unchanged and previous.fetched_at else None
            ),
            "changed_fields": changed_fields,
            "observation_unchanged": not changed_fields,
            "model_calls_made": reextraction.model_calls,
            "estimated_cost_saved_usd": None,
            "short_circuit": unchanged and not changed_fields,
        }

    def _read_for_refresh(
        self,
        workspace_id: str,
        job_posting_id: str,
        source_url: str,
        board_url: str,
        prefetched: FetchedPosting | None,
    ) -> FetchedPosting:
        if prefetched is None:
            return self.fetcher.fetch(source_url, board_url=board_url)
        with self.database.session() as session:
            self._require_prefetched_read(session, workspace_id, job_posting_id, prefetched)
        return prefetched

    def _require_prefetched_read(
        self,
        session: Session,
        workspace_id: str,
        job_posting_id: str,
        prefetched: FetchedPosting | None,
    ) -> None:
        if prefetched is None:
            return
        job = session.get(JobPosting, job_posting_id)
        if job is None or job.archived_at is not None or job.workspace_id != workspace_id:
            raise NotFoundError(f"Job {job_posting_id!r} does not exist in this workspace.")
        if (
            prefetched.posting_status != "active"
            or not prefetched.text
            or not prefetched.text.strip()
            or prefetched.publisher_markup_present is not True
            or prefetched.source is None
            or not prefetched.final_url.strip()
        ):
            raise ValidationError("The supplied publisher read requires a verified full body.")
        known_url = session.scalar(
            select(PostingUrl.id)
            .where(
                PostingUrl.job_posting_id == job_posting_id,
                PostingUrl.archived_at.is_(None),
                PostingUrl.normalized_url == normalize_url(prefetched.final_url),
            )
            .limit(1)
        )
        if known_url is None:
            raise ValidationError("The supplied publisher read does not identify this posting.")

    def _previous(self, workspace_id: str, job_posting_id: str) -> PreviousObservation:
        with self.database.session() as session:
            job = session.get(JobPosting, job_posting_id)
            if job is None or job.archived_at is not None or job.workspace_id != workspace_id:
                raise NotFoundError(f"Job {job_posting_id!r} does not exist in this workspace.")
            observation = session.scalar(
                select(PostingObservation)
                .where(PostingObservation.job_posting_id == job.id)
                .order_by(PostingObservation.observed_at.desc(), PostingObservation.id.desc())
                .limit(1)
            )
            source_url = session.scalar(
                select(PostingUrl.url)
                .where(PostingUrl.job_posting_id == job.id)
                .order_by(PostingUrl.is_primary.desc(), PostingUrl.created_at, PostingUrl.id)
                .limit(1)
            )
            if not source_url:
                raise ValidationError("Posting refresh requires a stored source URL.")
            if observation is None:
                return PreviousObservation(
                    job_id=job.id,
                    workspace_id=workspace_id,
                    observation_id=None,
                    artifact_id=None,
                    body="",
                    content_sha256=None,
                    posting_status="",
                    location="",
                    residence_rule="",
                    employment_type="",
                    payroll_arrangement="",
                    fetched_at=None,
                    extraction_method="",
                    source_url=str(source_url),
                    title=job.title,
                )
            workspace = session.get(Workspace, workspace_id)
            body = ""
            # A body that cannot be read back counts as no stored body, so this read replaces it.
            readable_artifact_id = None
            if observation.body_artifact_id:
                artifact = session.get(Artifact, observation.body_artifact_id)
                if artifact is not None and workspace is not None:
                    try:
                        body = self.artifacts.read(artifact, workspace).decode("utf-8")
                        readable_artifact_id = artifact.id
                    except ValidationError:
                        body = ""
            location, residence_rule, employment_type, payroll_arrangement = self._same_body_facts(
                session, observation
            )
            return PreviousObservation(
                job_id=job.id,
                workspace_id=workspace_id,
                observation_id=observation.id,
                artifact_id=readable_artifact_id,
                body=body,
                content_sha256=observation.content_sha256,
                posting_status=observation.posting_status,
                location=location,
                residence_rule=residence_rule,
                employment_type=employment_type,
                payroll_arrangement=payroll_arrangement,
                fetched_at=observation.fetched_at,
                extraction_method=observation.extraction_method or "",
                source_url=str(source_url),
                title=job.title,
                publisher_closing_date=observation.publisher_closing_date,
            )

    @staticmethod
    def _same_body_facts(
        session: Session, observation: PostingObservation
    ) -> tuple[str, str, str, str]:
        """Recover only facts previously observed for the exact same posting body."""
        fields = (
            "location",
            "residence_rule",
            "employment_type",
            "payroll_arrangement",
        )
        values = {field: getattr(observation, field) for field in fields}
        if observation.content_sha256 and not all(values.values()):
            matching = session.scalars(
                select(PostingObservation)
                .where(
                    PostingObservation.job_posting_id == observation.job_posting_id,
                    PostingObservation.content_sha256 == observation.content_sha256,
                )
                .order_by(PostingObservation.observed_at.desc(), PostingObservation.id.desc())
            ).all()
            for candidate in matching:
                for field in fields:
                    values[field] = values[field] or getattr(candidate, field)
        return (
            values["location"],
            values["residence_rule"],
            values["employment_type"],
            values["payroll_arrangement"],
        )

    @staticmethod
    def _carry_forward_eligibility(session: Session, observation: PostingObservation) -> None:
        """Attach the latest eligibility record proven against this exact body."""
        if not observation.content_sha256:
            return
        records = same_body_eligibility(
            session,
            observation.job_posting_id,
            observation.content_sha256,
            excluding_observation_id=observation.id,
        )
        session.add_all(
            EligibilityRecord(
                job_posting_id=observation.job_posting_id,
                observation_id=observation.id,
                workspace_location_key=record.workspace_location_key,
                region_rule=record.region_rule,
                arrangement=record.arrangement,
                details=record.details,
                created_by="worker",
                origin="posting.refresh",
            )
            for record in records
        )


class _ReextractionOutcome:
    """Disputes considered by one paid re-extraction, with the model's cited quotes.

    Empty when the paid path did not run.
    """

    __slots__ = ("disputes", "model_calls", "quote_by_hash")

    def __init__(
        self,
        *,
        disputes: Sequence[DisputeLike],
        quote_by_hash: dict[str, str],
        model_calls: int,
    ) -> None:
        self.disputes = disputes
        self.quote_by_hash = quote_by_hash
        self.model_calls = model_calls

    @classmethod
    def empty(cls) -> _ReextractionOutcome:
        return cls(disputes=[], quote_by_hash={}, model_calls=0)


def _maybe_reextract(
    settings_service: WorkspaceSettingsService,
    rulings_service: RequirementRulingService,
    *,
    workspace_id: str,
    job_posting_id: str,
    body: str,
    source_url: str,
    client: Client | None,
) -> _ReextractionOutcome:
    """Re-extract the posting once when a workspace has opted in and disputes exist.

    Returns the disputes fed to the model and every extracted requirement's
    ``quote_or_span``, keyed by ``derived_proposal_identity(requirement.text)``.
    """
    if client is None:
        return _ReextractionOutcome.empty()
    settings = settings_service.current(workspace_id)
    if not settings.learning.enable_dispute_reextraction:
        return _ReextractionOutcome.empty()
    disputes = rulings_service.active_extraction_disputes(
        workspace_id, job_posting_id=job_posting_id
    )
    if not disputes:
        return _ReextractionOutcome.empty()
    jd = JDText(text=body, origin=source_url, method="posting.refresh_with_reextraction")
    spec = jobspec.extract(client, jd, disputed_rulings=dispute_block(disputes))
    quote_by_hash = {
        derived_proposal_identity(item.text): item.quote_or_span for item in spec.requirements
    }
    return _ReextractionOutcome(disputes=disputes, quote_by_hash=quote_by_hash, model_calls=1)


def _apply_reextraction(
    session: Session,
    *,
    workspace_id: str,
    job_posting_id: str,
    observation_id: str,
    body: str,
    reextraction: _ReextractionOutcome,
    correlation_id: str | None,
) -> None:
    """Overrule each dispute whose requirement's cited span appears in ``body``."""
    if not reextraction.disputes:
        return
    for dispute in reextraction.disputes:
        if not dispute.requirement_text:
            continue
        key = derived_proposal_identity(dispute.requirement_text)
        cited_text = reextraction.quote_by_hash.get(key, "")
        if not cited_text or not cited_in_body(cited_text, body):
            continue
        if normalize_text(cited_text) == normalize_text(dispute.requirement_text):
            continue
        reverse_ruling(
            session,
            workspace_id=workspace_id,
            requirement_key=dispute.requirement_key,
            disputed=Ruling.DISPUTED_EXTRACTION,
            action="ruling.reversed_by_reextraction",
            origin="posting.refresh_with_reextraction",
            details={
                "job_posting_id": job_posting_id,
                "observation_id": observation_id,
                "cited_text": cited_text,
            },
            correlation_id=correlation_id,
        )
    session.flush()


class PostingRefreshHandler:
    """Worker adapter for one posting refresh."""

    def __init__(
        self,
        database: Database,
        *,
        storage: RuntimeStorage,
        fetcher: PostingFetcher,
    ) -> None:
        """Configure the one-posting refresh service."""
        self.service = PostingRefreshService(database, storage=storage, fetcher=fetcher)

    def __call__(self, context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Refresh the validated posting identity in this task's workspace."""
        value = payload.get("job_posting_id")
        if not isinstance(value, str) or not value:
            raise ValidationError("posting.refresh requires job_posting_id.")
        task = context.queue.get(context.task_id)
        return self.service.refresh(str(task.workspace_id), value, context)


class PostingRefreshWithReextractionHandler:
    """Worker adapter that re-extracts the posting when active disputes match."""

    def __init__(
        self,
        database: Database,
        *,
        storage: RuntimeStorage,
        fetcher: PostingFetcher,
        provider: Provider | None = None,
    ) -> None:
        """Configure the paid re-extraction variant of the one-posting refresh."""
        self.database = database
        self.service = PostingRefreshService(database, storage=storage, fetcher=fetcher)
        self.settings = WorkspaceSettingsService(database)
        self.provider = provider

    def __call__(self, context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Refresh and optionally overrule workspace disputes for this posting."""
        value = payload.get("job_posting_id")
        if not isinstance(value, str) or not value:
            raise ValidationError("posting.refresh_with_reextraction requires job_posting_id.")
        task = context.queue.get(context.task_id)
        workspace_id = str(task.workspace_id)
        settings = self.settings.current(workspace_id)
        if not settings.learning.enable_dispute_reextraction:
            return self.service.refresh(workspace_id, value, context)
        with billed_run(
            self.database,
            workspace_id=workspace_id,
            task_id=context.task_id,
            settings=settings,
            origin="posting.refresh_with_reextraction",
            provider=self.provider,
        ) as paid:
            return self.service.refresh(
                workspace_id,
                value,
                context,
                client=paid.build_client(),
            )


class PostingRefreshBatchHandler:
    """Sequential polite batch refresh that skips a posting already read today."""

    def __init__(
        self,
        database: Database,
        *,
        storage: RuntimeStorage,
        fetcher: PostingFetcher,
    ) -> None:
        """Configure batch selection and the shared one-posting refresh service."""
        self.database = database
        self.service = PostingRefreshService(database, storage=storage, fetcher=fetcher)

    def __call__(self, context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Select due jobs and refresh them sequentially with checkpoints."""
        value = PostingRefreshBatchPayload.model_validate(payload)
        task = context.queue.get(context.task_id)
        workspace_id = str(task.workspace_id)
        job_ids = self._select_jobs(workspace_id, value)
        results, skipped, failures = self._refresh_jobs(
            context, workspace_id=workspace_id, job_ids=job_ids, force=value.force
        )
        unverified = [
            {
                "job_posting_id": str(item["job_posting_id"]),
                "reason": str(item.get("reason") or "The publisher could not verify this posting."),
            }
            for item in results
            if item.get(_UNVERIFIED)
        ]
        tallies = _tallies(results, skipped, failures)
        return {
            "selected": len(job_ids),
            _REFRESHED: tallies[_REFRESHED],
            "unchanged": tallies["unchanged"],
            "changed": tallies["changed"],
            _CLOSED: tallies[_CLOSED],
            _UNVERIFIED: tallies[_UNVERIFIED],
            "unverified_postings": unverified,
            "model_calls_made": 0,
            PostingRefreshOutcome.SKIPPED.value: skipped,
            PostingRefreshOutcome.FAILED.value: tallies[PostingRefreshOutcome.FAILED.value],
            "failures": failures,
            "results": results,
        }

    def _refresh_jobs(
        self,
        context: TaskContext,
        *,
        workspace_id: str,
        job_ids: list[str],
        force: bool,
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[dict[str, str]]]:
        """Refresh each eligible posting while retaining batch progress and failures."""
        results: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        failures: list[dict[str, str]] = []
        # A whole-site posting feed answers for every row on that site in this pass.
        with url_ingest.shared_posting_feeds():
            for position, job_id in enumerate(job_ids, start=1):
                context.raise_if_cancelled()
                if not force:
                    reason = self._not_due(workspace_id, job_id)
                    if reason:
                        skipped.append({"job_posting_id": job_id, "reason": reason})
                        continue
                step = BatchStepContext(
                    context,
                    current=position,
                    total=len(job_ids),
                    so_far=_tallies(results, skipped, failures),
                )
                step.progress("refreshing", {"job_posting_id": job_id})
                try:
                    results.append(self.service.refresh(workspace_id, job_id, step))
                except ClearscoutError as error:
                    failures.append(
                        {
                            "job_posting_id": job_id,
                            "error_category": type(error).__name__,
                            "error_message": str(error),
                        }
                    )
                context.raise_if_cancelled()
        return results, skipped, failures

    def _select_jobs(self, workspace_id: str, value: PostingRefreshBatchPayload) -> list[str]:
        if value.job_posting_ids:
            return list(value.job_posting_ids)
        if value.saved_view_id:
            with self.database.session() as session:
                saved = session.get(SavedView, value.saved_view_id)
                if (
                    saved is None
                    or saved.workspace_id != workspace_id
                    or saved.archived_at is not None
                    or saved.resource != "jobs"
                ):
                    raise NotFoundError("The saved jobs view does not exist in this workspace.")
                configuration = saved.configuration
            ids = configuration.get("job_posting_ids")
            if isinstance(ids, list) and all(isinstance(item, str) for item in ids):
                return list(dict.fromkeys(ids))[:250]
            filters = PostingRefreshFilter.model_validate(configuration.get("filters", {}))
            return self._filter_jobs(workspace_id, filters)
        if value.filters is None:
            raise ValidationError("Batch refresh selector is missing.")
        return self._filter_jobs(workspace_id, value.filters)

    def _filter_jobs(self, workspace_id: str, value: PostingRefreshFilter) -> list[str]:
        rows, _ = job_search.list_jobs(
            self.database,
            JobListQuery(
                workspace_id=workspace_id,
                decisions=tuple(value.decisions),
                application_statuses=tuple(value.application_statuses),
                completeness=value.completeness,
                limit=250,
            ),
        )
        return [item.job_posting_id for item in rows]

    def _not_due(self, workspace_id: str, job_id: str) -> str:
        with self.database.session() as session:
            job = session.get(JobPosting, job_id)
            if job is None or job.workspace_id != workspace_id:
                return "job_not_in_workspace"
            latest = session.scalar(
                select(PostingObservation)
                .where(PostingObservation.job_posting_id == job_id)
                .order_by(PostingObservation.observed_at.desc())
                .limit(1)
            )
            if latest is not None and latest.observed_at.date() >= utc_now().date():
                return "read_today"
        return ""


def _tallies(
    results: list[dict[str, Any]], skipped: list[dict[str, str]], failures: list[dict[str, str]]
) -> dict[str, int]:
    """Count a batch refresh's outcomes under the names its summary uses."""
    refreshed = [item for item in results if not item.get(_UNVERIFIED)]
    return {
        _REFRESHED: len(refreshed),
        "unchanged": sum(bool(item["observation_unchanged"]) for item in refreshed),
        "changed": sum(not bool(item["observation_unchanged"]) for item in refreshed),
        _CLOSED: sum(bool(item[_CLOSED]) for item in refreshed),
        _UNVERIFIED: len(results) - len(refreshed),
        PostingRefreshOutcome.SKIPPED.value: len(skipped),
        PostingRefreshOutcome.FAILED.value: len(failures),
    }


def _raise_if_cancelled(context: TaskContext | None) -> None:
    if context is not None:
        context.raise_if_cancelled()


def _progress(context: TaskContext | None, stage: str, details: dict[str, Any]) -> None:
    if context is not None:
        context.progress(stage, details)
