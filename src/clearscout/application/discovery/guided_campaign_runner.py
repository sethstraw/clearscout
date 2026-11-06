"""Run one guided campaign to completion, resumable after a worker retry.

Every execution transition goes through ``GuidedCampaignLedger``; this module
only decides which transition each attempt needs next and builds the request
and admission inputs the ledger and admission module persist.
"""

from __future__ import annotations

import hashlib
import logging
import re
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select

from clearscout.application.config import (
    Effort,
    ModelChoice,
    ProviderName,
    Settings,
    Stage,
    Verbosity,
)
from clearscout.application.discovery.board_scan import SourceScanService
from clearscout.application.discovery.content_resolution import (
    PostingContentResolutionService,
    resolved_publisher_fetch,
)
from clearscout.application.discovery.funnel_audit import FunnelAuditBuilder, FunnelOutcome
from clearscout.application.discovery.guided_campaign import (
    DEFAULT_MAX_HOSTED_SEARCHES,
    DEFAULT_MAX_MODEL_CALLS,
    CampaignAttemptPlan,
    CampaignBranch,
    CampaignIdentities,
    CampaignManifest,
)
from clearscout.application.discovery.guided_campaign_admission import (
    ObservedUrl,
    ReadUrl,
    admit_read_urls,
    model_pick_refusal,
    read_captured_urls,
)
from clearscout.application.discovery.guided_campaign_ledger import (
    AttemptRecord,
    GuidedCampaignLedger,
    StrandedCampaign,
)
from clearscout.application.discovery.guided_campaign_report import (
    compile_current_campaign,
    latest_campaign_report,
)
from clearscout.application.discovery.guided_campaign_request import (
    attempt_request_plan,
    campaign_response_schema,
)
from clearscout.application.discovery.posting_fetch import PostingFetcher
from clearscout.application.discovery.posting_identity import posting_url_key
from clearscout.application.discovery.search_parameter_summary import search_summary
from clearscout.application.discovery.search_planning_queries import SearchPlanningQueries
from clearscout.application.discovery.source_reading import SourceReader
from clearscout.application.failures import ConflictError, ValidationError
from clearscout.application.master_cv.cv import parse_cv
from clearscout.application.prompts import data_block
from clearscout.application.tailoring.stages.context import record_block
from clearscout.application.tasks import GuidedCampaignPayload, TaskContext
from clearscout.application.tasks.payloads import GuidedCampaignRetryPayload
from clearscout.domain import utc_now
from clearscout.domain.campaigns import (
    AttemptErrorCategory,
    AttemptState,
    BranchOutcome,
    CampaignReason,
    CampaignStatus,
    CapturedUrlClassification,
    CapturedUrlOutcome,
    UrlActionKind,
)
from clearscout.domain.identity import mentions, mentions_in_order, normalize_text
from clearscout.domain.search_parameters import JobSearchParameters
from clearscout.infrastructure.artifacts.runtime import RuntimeStorage
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    GuidedCampaignAttempt,
    GuidedCampaignCapturedUrl,
    GuidedCampaignUrlObservation,
)
from clearscout.infrastructure.providers.base import (
    BackgroundPoll,
    BackgroundWebSearchProvider,
    Provider,
    provider_for,
)
from clearscout.infrastructure.providers.cache import ResponseCache
from clearscout.infrastructure.providers.client import Client
from clearscout.infrastructure.providers.credentials import resolve
from clearscout.infrastructure.providers.plan import RequestPlan, SearchPolicy, Segment
from clearscout.infrastructure.web_fetch.policy import PoliteHttpClient
from clearscout.shared.costs import ServiceTier
from clearscout.shared.costs.prices import PriceOverride, ledger_for
from clearscout.shared.errors import (
    CapacityError,
    ClearscoutError,
    ProviderError,
    ProviderFailure,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 2.0
MAX_POLL_ATTEMPTS = 300


def poll_until_terminal(
    collect: Callable[[], BackgroundPoll],
    sleeper: Callable[[float], None],
    *,
    on_pending: Callable[[], None] = lambda: None,
) -> BackgroundPoll:
    """Collect a dispatched response until it leaves pending, sleeping between polls.

    ``on_pending`` runs after each pending poll, before the sleep.
    """
    for _ in range(MAX_POLL_ATTEMPTS):
        poll = collect()
        if poll.status != "pending":
            return poll
        on_pending()
        sleeper(POLL_INTERVAL_SECONDS)
    raise ProviderError("Background response did not reach a terminal state before polling paused.")


class _CancelledError(Exception):
    """Stop the campaign because its task was canceled."""


def _raise_if_cancelled(context: TaskContext | None) -> None:
    if context is not None and context.cancellation_requested():
        raise _CancelledError()


# One service instance serves every campaign and retry task, so this is threaded
# through a run as a local and never held on the service.
class _RunFunnel:
    """The attestation and withdrawal count one run accumulates, republishes, and returns."""

    def __init__(self) -> None:
        """Start an empty attestation with no phase to republish yet."""
        self._audit = FunnelAuditBuilder()
        self._phase: dict[str, Any] = {}
        self.withdrawn_on_read = 0

    def note_withdrawn_on_read(self, pending_ids: list[str]) -> None:
        """Count rows a content read closed, which the funnel's admission counts never carry."""
        self.withdrawn_on_read += len(pending_ids)

    def record(self, outcomes: Iterable[FunnelOutcome]) -> None:
        """Count the candidate rows a committed transaction offered the ledger."""
        for outcome in outcomes:
            self._audit.record(outcome)

    def payload(self) -> dict[str, Any]:
        """Return the full audit the campaign panel reads as its coverage denominator."""
        return self._audit.build().model_dump(mode="json")

    def checkpoint(self, context: TaskContext | None, **phase: Any) -> None:
        """Republish the attestation with the run's phase, which heartbeat overwrites whole."""
        if phase:
            self._phase = phase
        if context is not None:
            context.progress("running", {**self._phase, "funnel": self.payload()})


@dataclass(frozen=True)
class _RunInputs:
    """One coherent snapshot the runner uses to compile and describe a campaign."""

    manifest: CampaignManifest
    settings: Settings
    evidence_text: str
    parameters: JobSearchParameters
    searched_titles: list[str]
    known_source_roots: frozenset[str]
    known_posting_fingerprints: frozenset[str]
    request_text: dict[int, tuple[str, str]]

    def snapshot(self) -> dict[str, Any]:
        """Return the exact request, pricing, and admission inputs a resume must reuse."""
        return {
            "evidence_text": self.evidence_text,
            "parameters": self.parameters.model_dump(mode="json"),
            "searched_titles": self.searched_titles,
            "known_source_roots": sorted(self.known_source_roots),
            "known_posting_fingerprints": sorted(self.known_posting_fingerprints),
            "attempts": [
                {"position": position, "instructions": instructions, "task": task}
                for position, (instructions, task) in sorted(self.request_text.items())
            ],
            "prices": {
                name: override.model_dump(mode="json")
                for name, override in self.settings.prices.items()
            },
            "request_timeout": self.settings.request_timeout,
        }


def _request_digest(plan: RequestPlan) -> str:
    """Return the sha256 hex digest of exactly the text this request sends."""
    parts = [plan.instructions.text, plan.task.text]
    if plan.evidence is not None:
        parts.append(plan.evidence.text)
    return hashlib.sha256("\n\n".join(parts).encode("utf-8")).hexdigest()


def _choice(identities: CampaignIdentities) -> ModelChoice:
    """Rebuild the model choice pinned in a manifest, never the current settings."""
    return ModelChoice(
        model=identities.model,
        effort=Effort(identities.effort) if identities.effort else None,
        verbosity=Verbosity(identities.verbosity) if identities.verbosity else None,
        service_tier=ServiceTier(identities.service_tier)
        if identities.service_tier
        else ServiceTier.STANDARD,
    )


def _row_for(rows: dict[str, dict[str, Any]], url: str, sha: str) -> dict[str, Any]:
    """Return the captured-URL row for this hash, creating an unclassified one first."""
    return rows.setdefault(
        sha,
        {
            "url_sha256": sha,
            "url": url,
            "classification": CapturedUrlClassification.OTHER,
            "observations": [],
        },
    )


def _record_search_and_action_rows(record: Any, rows: dict[str, dict[str, Any]]) -> None:
    """Add an observation row for every search source and page action in a capture record."""
    for search in record.searches:
        for source in search.sources:
            row = _row_for(rows, source.url, posting_url_key(source.url))
            for query in search.queries or ("",):
                row["observations"].append(
                    {"action_kind": UrlActionKind.SEARCH_SOURCE, "query": query}
                )
    for action in record.page_actions:
        _row_for(rows, action.url, posting_url_key(action.url))["observations"].append(
            {"action_kind": action.kind, "query": ""}
        )


def _apply_model_picks(value: Any, rows: dict[str, dict[str, Any]]) -> tuple[dict[str, str], ...]:
    """Mark rows the model selected; return the picks the program would not fetch, with why."""
    refused: list[dict[str, str]] = []
    for item, is_board in (
        *((item, False) for item in getattr(value, "postings", [])),
        *((item, True) for item in getattr(value, "boards", [])),
    ):
        # Only a pick that would mint its own row is policed; one that matches a captured
        # row marks that row, whose own URL admission already polices.
        sha = posting_url_key(item.url)
        if sha not in rows:
            reason = model_pick_refusal(item.url)
            if reason:
                refused.append({"url": item.url, "reason": reason})
                continue
            _row_for(rows, item.url, sha)["observations"].append(
                {"action_kind": "model_written", "query": ""}
            )
        rows[sha]["model_selected"] = True
        rows[sha]["model_why"] = item.why
        if is_board:
            rows[sha]["model_board"] = True
    return tuple(refused)


def captured_url_rows(
    poll: BackgroundPoll,
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, str], ...]]:
    """Project the poll's capture into captured-URL rows; model picks only mark them.

    A search or page-action URL is kept as a row for admission to refuse; a model
    pick the program would not fetch is dropped here and returned as a refusal instead.
    """
    rows: dict[str, dict[str, Any]] = {}
    if poll.record is not None:
        _record_search_and_action_rows(poll.record, rows)
    refused_model_picks = _apply_model_picks(poll.value, rows)
    return tuple(rows.values()), refused_model_picks


def _branch_matches_query(branch: CampaignBranch, query: str, *, anchor_shared: bool) -> bool:
    """Return whether one captured query names this branch's anchors or focuses in order.

    When a sibling market chunk shares the anchor, the query must also name one of
    this branch's markets, or "remote" when it has none.
    """
    # A host such as boards.greenhouse.io must not read as the words of a focus.
    searched = re.sub(r"(?<!\w)-?site:[^\s()]+", " ", query, flags=re.IGNORECASE)
    searched = re.sub(r"\(\s*(?:(?:OR|\|)\s*)*\)", " ", searched, flags=re.IGNORECASE)
    normalized_query = normalize_text(searched)
    terms = (*branch.anchors, *(value for _, value in branch.focuses))
    if not any(mentions_in_order(normalized_query, normalize_text(term)) for term in terms):
        return False
    if not anchor_shared:
        return True
    market_terms = branch.markets or ("remote",)
    return any(mentions(normalized_query, normalize_text(market)) for market in market_terms)


def _attributed_queries(poll: BackgroundPoll) -> dict[str, frozenset[str]]:
    """Return each branch key's queries as the answer attributes them, normalized."""
    attributed: dict[str, set[str]] = {}
    for report in getattr(poll.value, "searches", None) or ():
        for key in report.branch_keys:
            attributed.setdefault(key, set()).add(normalize_text(report.query))
    return {key: frozenset(queries) for key, queries in attributed.items()}


def matched_queries(
    manifest: CampaignManifest, attempt_plan: CampaignAttemptPlan, poll: BackgroundPoll
) -> dict[str, tuple[str, ...]]:
    """Credit each of this attempt's branches with the captured queries that served it.

    A branch with a floor query is credited by the queries naming it; a branch
    without one only by a captured query the answer attributes to it.
    """
    by_key = {branch.key: branch for branch in manifest.branches}
    # Market chunks of one identity share the (anchors, focuses) pair; floorless
    # bundles share empty anchors but differ in focuses, so no two share the pair.
    owner_counts: dict[tuple[object, ...], int] = {}
    for key in attempt_plan.branch_keys:
        branch = by_key.get(key)
        if branch is not None:
            identity = (branch.anchors, branch.focuses)
            owner_counts[identity] = owner_counts.get(identity, 0) + 1
    captured_queries = tuple(
        query
        for search in (poll.record.searches if poll.record is not None else ())
        for query in search.queries
    )
    attributed = _attributed_queries(poll)

    def served(branch: CampaignBranch, query: str) -> bool:
        if not branch.floor_query:
            return normalize_text(query) in attributed.get(branch.key, frozenset())
        return _branch_matches_query(
            branch, query, anchor_shared=owner_counts[(branch.anchors, branch.focuses)] > 1
        )

    return {
        key: tuple(query for query in captured_queries if served(by_key[key], query))
        for key in attempt_plan.branch_keys
        if key in by_key
    }


def _error_category(error: Exception) -> AttemptErrorCategory:
    """Map one dispatch or poll failure to the stable category the ledger persists."""
    # A capacity refusal is also never created; the narrower category tells the operator more.
    if isinstance(error, CapacityError):
        return AttemptErrorCategory.CAPACITY
    if not isinstance(error, ClearscoutError):
        return AttemptErrorCategory.PROVIDER_ERROR
    if error.certainly_not_created:
        return AttemptErrorCategory.NOT_CREATED
    if error.category is not None:
        return AttemptErrorCategory(error.category.value)
    return AttemptErrorCategory.PROVIDER_ERROR


def _is_response_gone(error: Exception) -> bool:
    """Only a response the provider no longer has may mark a polled attempt uncertain."""
    return isinstance(error, ClearscoutError) and error.category == ProviderFailure.RESPONSE_GONE


class GuidedCampaignService:
    """Compile, dispatch, capture, and admit one guided gap campaign."""

    def __init__(
        self,
        database: Database,
        *,
        storage: RuntimeStorage,
        posting_fetcher: PostingFetcher,
        content_resolution: PostingContentResolutionService,
        fetcher: SourceReader | None = None,
        provider: Provider | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        http_client: PoliteHttpClient | None = None,
    ) -> None:
        """Bind the runner to durable state, the network seam, and the model."""
        self.database = database
        self.scans = SourceScanService(
            database,
            storage=storage,
            fetcher=fetcher,
            posting_fetcher=posting_fetcher,
            http_client=http_client,
        )
        self.queries = SearchPlanningQueries(database, storage)
        self.ledger = GuidedCampaignLedger(database)
        self.content_resolution = content_resolution
        self.provider = provider
        self.sleeper = sleeper
        self.clock = clock

    def run(
        self,
        workspace_id: str,
        *,
        campaign_manifest_sha256: str,
        campaign_cycle: int,
        max_model_calls: int | None,
        max_hosted_searches: int | None,
        context: TaskContext | None,
    ) -> dict[str, Any]:
        """Run the approved campaign to a terminal or paused state, resuming as needed."""
        stored = self.ledger.stored_campaign(workspace_id, campaign_manifest_sha256, campaign_cycle)
        task_id = getattr(context, "task_id", None) or ""
        funnel = _RunFunnel()
        if stored is None:
            inputs = self._build_inputs(workspace_id, max_model_calls, max_hosted_searches)
            if inputs.manifest.sha256 != campaign_manifest_sha256:
                raise ConflictError(
                    "The approved campaign manifest no longer matches the current search "
                    "scope; preview and approve it again."
                )
        else:
            inputs = self._stored_inputs(workspace_id, stored.manifest)
        # The client is built before any claim, so a provider or credential failure owns nothing.
        with self._client_for(inputs.manifest.identities, inputs.settings) as client:
            try:
                if task_id:
                    self.retry_stranded_processing(workspace_id, task_id, context, funnel=funnel)
                if stored is None:
                    self._supersede_stale_campaign(workspace_id, task_id, context, funnel)
            except _CancelledError:
                with self.database.session() as session:
                    report = latest_campaign_report(session, workspace_id)
                return {
                    "campaign_id": None,
                    "reasons": [],
                    "cancelled": True,
                    "report_available": report is not None,
                    "withdrawn_on_read": funnel.withdrawn_on_read,
                    "funnel": funnel.payload(),
                }
            campaign_id = self.ledger.create_or_claim(
                workspace_id, inputs.manifest, campaign_cycle, task_id, inputs.snapshot()
            )
            try:
                reasons = self._drive(
                    workspace_id, campaign_id, task_id, inputs, client, context, funnel
                )
                cancelled = False
            except _CancelledError:
                self.ledger.finish(
                    campaign_id, task_id, status=CampaignStatus.CANCELLED, reasons=[]
                )
                reasons = []
                cancelled = True
            except Exception:
                # A failed task keeps only its last heartbeat, so the funnel is republished
                # here, and a dead lease or a refused heartbeat must not mask the failure.
                try:
                    funnel.checkpoint(context)
                except Exception:
                    logger.warning("Campaign funnel checkpoint failed", exc_info=True)
                self.ledger.release_lease(campaign_id, task_id)
                raise
        with self.database.session() as session:
            report = latest_campaign_report(session, workspace_id)
        return {
            "campaign_id": campaign_id,
            "reasons": reasons,
            "cancelled": cancelled,
            "report_available": report is not None,
            "withdrawn_on_read": funnel.withdrawn_on_read,
            "funnel": funnel.payload(),
        }

    @contextmanager
    def _client_for(self, identities: CampaignIdentities, settings: Settings) -> Iterator[Client]:
        """Build the one background-search client a run or a supersede pass dispatches through."""
        provider = self.provider or provider_for(
            ProviderName(identities.provider),
            resolve(ProviderName(identities.provider)).key,
            settings.request_timeout,
        )
        if not isinstance(provider, BackgroundWebSearchProvider):
            raise ProviderError(
                f"{provider.name.value.title()} does not support background web search."
            )
        with ResponseCache(enabled=False) as cache:
            yield Client(
                provider=provider,
                settings=settings,
                cache=cache,
                ledger=ledger_for(settings.prices),
                sleeper=self.sleeper,
            )

    def _supersede_stale_campaign(
        self, workspace_id: str, task_id: str, context: TaskContext | None, funnel: _RunFunnel
    ) -> None:
        """Collect a stale campaign's dispatched responses under its stored inputs, then close it.

        A failure on one attempt is recorded on that attempt and does not stop the close.
        """
        stale = self.ledger.supersedable_campaign(workspace_id)
        if stale is None:
            return
        if not self.ledger.claim_for_supersede(
            stale.id,
            task_id,
            observed_status=stale.status,
            observed_owner_task_id=stale.owner_task_id,
        ):
            # Another task already claimed or changed this campaign; there is
            # nothing left here for this task to supersede.
            return
        try:
            old_inputs = self._stored_inputs(workspace_id, stale.manifest)
            with self._client_for(old_inputs.manifest.identities, old_inputs.settings) as client:
                self._collect_stale_attempts(
                    workspace_id, stale.id, task_id, old_inputs, client, context, funnel
                )
        except Exception:
            self.ledger.release_lease(stale.id, task_id, restore_status=stale.status)
            raise
        self.ledger.finish(
            stale.id,
            task_id,
            status=CampaignStatus.INCOMPLETE,
            reasons=[CampaignReason.SUPERSEDED.value],
        )

    def _collect_stale_attempts(
        self,
        workspace_id: str,
        campaign_id: str,
        task_id: str,
        old_inputs: _RunInputs,
        client: Client,
        context: TaskContext | None,
        funnel: _RunFunnel,
    ) -> None:
        """Capture and process a superseded campaign's attempts, isolating each failure."""
        plans = {attempt.position: attempt for attempt in old_inputs.manifest.attempts}
        for listed in self.ledger.attempts(campaign_id):
            record = listed
            plan_attempt = plans.get(record.position)
            try:
                if (
                    record.state == AttemptState.DISPATCHED
                    and record.provider_response_id is not None
                    and plan_attempt is not None
                ):
                    self._poll_and_capture(
                        workspace_id,
                        campaign_id,
                        task_id,
                        record,
                        plan_attempt,
                        old_inputs,
                        client,
                        context,
                        funnel,
                    )
                    record = self._reload(campaign_id, record.position)
            except _CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 -- isolate one attempt, close the rest
                self.ledger.mark_uncertain(
                    campaign_id,
                    record.position,
                    task_id,
                    str(error),
                    error_category=_error_category(error),
                )
                continue
            if record.state == AttemptState.CAPTURED:
                self._process_or_record_failure(
                    workspace_id, campaign_id, task_id, record, old_inputs, context, funnel
                )

    def _process_or_record_failure(
        self,
        workspace_id: str,
        campaign_id: str,
        task_id: str,
        record: AttemptRecord,
        inputs: _RunInputs,
        context: TaskContext | None,
        funnel: _RunFunnel,
    ) -> None:
        """Process a captured attempt, leaving it captured and resumable when processing fails."""
        try:
            self._process(workspace_id, campaign_id, task_id, record, inputs, funnel, context)
        except Exception as error:  # noqa: BLE001 -- a later run or a manual retry resumes it
            _raise_if_cancelled(context)
            self.ledger.record_processing_failure(campaign_id, record.position, task_id, str(error))

    def retry_stranded_processing(
        self,
        workspace_id: str,
        task_id: str,
        context: TaskContext | None,
        *,
        funnel: _RunFunnel | None = None,
    ) -> int:
        """Reprocess captured attempts that earlier local processing left behind; never dispatch.

        Returns how many attempts this pass processed.
        """
        if not task_id:
            raise ConflictError("A processing retry needs the task that owns its lease.")
        pass_funnel = funnel if funnel is not None else _RunFunnel()
        processed = 0
        for stranded in self.ledger.stranded_campaigns(workspace_id):
            if not self.ledger.claim_for_processing_retry(stranded.id, task_id):
                continue
            try:
                processed += self._retry_campaign(
                    workspace_id, stranded, task_id, context, pass_funnel
                )
            except _CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Stranded campaign retry failed", extra={"campaign_id": stranded.id}
                )
            finally:
                self.ledger.release_lease(
                    stranded.id, task_id, restore_status=CampaignStatus.INCOMPLETE
                )
        return processed

    def _retry_campaign(
        self,
        workspace_id: str,
        stranded: StrandedCampaign,
        task_id: str,
        context: TaskContext | None,
        funnel: _RunFunnel,
    ) -> int:
        """Reprocess one claimed campaign's stranded attempts and count those now processed."""
        inputs = self._stored_inputs(workspace_id, stranded.manifest)
        processed = 0
        for record in self.ledger.attempts(stranded.id):
            if (
                record.state == AttemptState.CAPTURED
                and record.error_category == AttemptErrorCategory.PROCESSING_FAILED
            ):
                self._process_or_record_failure(
                    workspace_id, stranded.id, task_id, record, inputs, context, funnel
                )
                if self._reload(stranded.id, record.position).state == AttemptState.PROCESSED:
                    processed += 1
        return processed

    def _build_inputs(
        self, workspace_id: str, max_model_calls: int | None, max_hosted_searches: int | None
    ) -> _RunInputs:
        """Compile the manifest a fresh preview or approval would compile now."""
        with self.database.session() as session:
            compilation = compile_current_campaign(
                session,
                workspace_id,
                max_model_calls=max_model_calls or DEFAULT_MAX_MODEL_CALLS,
                max_hosted_searches=max_hosted_searches or DEFAULT_MAX_HOSTED_SEARCHES,
            )
        version = self.queries.master.read_version(workspace_id, compilation.master_cv.id)
        with tempfile.TemporaryDirectory(prefix="clearscout-guided-") as temporary:
            path = Path(temporary) / "master.md"
            path.write_text(version.content, encoding="utf-8")
            inventory = parse_cv(path)
        preferences = search_summary(compilation.parameters, compilation.archetypes)
        evidence_body = "\n\n".join(
            part
            for part in (
                data_block("declared_search_preferences", preferences),
                compilation.ruled_out_block,
                record_block(inventory),
            )
            if part
        )
        request_text = {
            attempt.position: (plan.instructions.text, plan.task.text)
            for attempt in compilation.manifest.attempts
            for plan in (attempt_request_plan(compilation.manifest, attempt, evidence_body),)
        }
        return _RunInputs(
            manifest=compilation.manifest,
            settings=compilation.settings,
            evidence_text=evidence_body,
            parameters=compilation.parameters,
            searched_titles=[item.title for item in compilation.titles],
            known_source_roots=compilation.known_source_roots,
            known_posting_fingerprints=compilation.known_posting_fingerprints,
            request_text=request_text,
        )

    def _stored_inputs(self, workspace_id: str, document: dict[str, Any]) -> _RunInputs:
        """Restore the request snapshot, prices, and timeout recorded at approval."""
        snapshot = document.get("request_snapshot")
        if not isinstance(snapshot, dict):
            raise ConflictError("The stored campaign lacks the approved request snapshot.")
        attempts = snapshot.get("attempts")
        parameters = snapshot.get("parameters")
        prices = snapshot.get("prices")
        request_timeout = snapshot.get("request_timeout")
        if (
            not isinstance(attempts, list)
            or not isinstance(parameters, dict)
            or not isinstance(prices, dict)
            or not isinstance(request_timeout, (int, float))
        ):
            raise ConflictError("The stored campaign request snapshot is malformed.")
        settings = self.queries.planning_inputs(workspace_id).settings.model_copy(
            update={
                "prices": {
                    str(name): PriceOverride.model_validate(override)
                    for name, override in prices.items()
                },
                "request_timeout": float(request_timeout),
            }
        )
        return _RunInputs(
            manifest=CampaignManifest.from_document(document),
            settings=settings,
            evidence_text=str(snapshot["evidence_text"]),
            parameters=JobSearchParameters.model_validate(parameters),
            searched_titles=[str(value) for value in snapshot["searched_titles"]],
            known_source_roots=frozenset(str(value) for value in snapshot["known_source_roots"]),
            known_posting_fingerprints=frozenset(
                str(value) for value in snapshot["known_posting_fingerprints"]
            ),
            request_text={
                int(attempt["position"]): (str(attempt["instructions"]), str(attempt["task"]))
                for attempt in attempts
            },
        )

    def _request_plan(self, inputs: _RunInputs, attempt: CampaignAttemptPlan) -> RequestPlan:
        """Rebuild one stored request plan without consulting mutable workspace inputs."""
        try:
            instructions, task = inputs.request_text[attempt.position]
        except KeyError as error:
            raise ConflictError(
                f"Campaign request snapshot has no attempt {attempt.position}."
            ) from error
        return RequestPlan(
            stage=Stage.GUIDED_CAMPAIGN,
            instructions=Segment(instructions, "guided_campaign_system"),
            task=Segment(task, "guided_campaign_user"),
            evidence=Segment(inputs.evidence_text),
            search=SearchPolicy(max_calls=attempt.search_cap),
            max_tokens=inputs.manifest.bounds.max_output_tokens,
        )

    def _drive(
        self,
        workspace_id: str,
        campaign_id: str,
        task_id: str,
        inputs: _RunInputs,
        client: Client,
        context: TaskContext | None,
        funnel: _RunFunnel,
    ) -> list[str]:
        """Advance every attempt one step at a time until the campaign pauses or finishes."""
        manifest = inputs.manifest
        plans = {attempt.position: attempt for attempt in manifest.attempts}
        for listed in self.ledger.attempts(campaign_id):
            record = listed
            plan_attempt = plans.get(record.position)
            if record.state in (AttemptState.DISPATCHING, AttemptState.UNCERTAIN):
                return self._finish_stuck(campaign_id, task_id, record, manifest)
            if record.state == AttemptState.PLANNED:
                if plan_attempt is None:
                    continue
                _raise_if_cancelled(context)
                self._dispatch(campaign_id, task_id, plan_attempt, inputs, client)
                record = self._reload(campaign_id, record.position)
            self._advance_dispatched_or_captured(
                workspace_id,
                campaign_id,
                task_id,
                record,
                plan_attempt,
                inputs,
                client,
                context,
                funnel,
            )
        return self._finish(campaign_id, task_id, manifest)

    def _finish_stuck(
        self, campaign_id: str, task_id: str, record: AttemptRecord, manifest: CampaignManifest
    ) -> list[str]:
        """Finish the campaign now because this attempt is stuck dispatching or uncertain."""
        if record.state == AttemptState.DISPATCHING:
            self.ledger.mark_uncertain(
                campaign_id, record.position, task_id, "dispatch left no response id"
            )
        return self._finish(campaign_id, task_id, manifest)

    def _advance_dispatched_or_captured(
        self,
        workspace_id: str,
        campaign_id: str,
        task_id: str,
        record: AttemptRecord,
        plan_attempt: CampaignAttemptPlan | None,
        inputs: _RunInputs,
        client: Client,
        context: TaskContext | None,
        funnel: _RunFunnel,
    ) -> AttemptRecord:
        """Poll a dispatched attempt to capture, then process a captured one."""
        if record.state == AttemptState.DISPATCHED and plan_attempt is not None:
            self._poll_and_capture(
                workspace_id,
                campaign_id,
                task_id,
                record,
                plan_attempt,
                inputs,
                client,
                context,
                funnel,
            )
            record = self._reload(campaign_id, record.position)
        if record.state == AttemptState.CAPTURED:
            _raise_if_cancelled(context)
            self._process(workspace_id, campaign_id, task_id, record, inputs, funnel, context)
        return record

    def _reload(self, campaign_id: str, position: int) -> AttemptRecord:
        return next(item for item in self.ledger.attempts(campaign_id) if item.position == position)

    def _dispatch(
        self,
        campaign_id: str,
        task_id: str,
        attempt_plan: CampaignAttemptPlan,
        inputs: _RunInputs,
        client: Client,
    ) -> None:
        """Create one background request, marking the attempt dispatching then dispatched."""
        manifest = inputs.manifest
        plan = self._request_plan(inputs, attempt_plan)
        schema = campaign_response_schema(tuple(branch.key for branch in manifest.branches))
        request_sha256 = _request_digest(plan)
        self.ledger.mark_dispatching(campaign_id, attempt_plan.position, task_id, request_sha256)
        try:
            dispatched = client.dispatch_background(
                plan,
                schema,
                _choice(manifest.identities),
                metadata={"request_sha256": request_sha256},
            )
        except Exception as error:
            if isinstance(error, ClearscoutError) and error.certainly_not_created:
                self.ledger.mark_not_created(
                    campaign_id,
                    attempt_plan.position,
                    task_id,
                    str(error),
                    error_category=_error_category(error),
                )
            else:
                self.ledger.mark_uncertain(
                    campaign_id,
                    attempt_plan.position,
                    task_id,
                    str(error),
                    error_category=_error_category(error),
                )
            raise
        logger.info(
            "Campaign attempt dispatched",
            extra={
                "campaign_id": campaign_id,
                "attempt_position": attempt_plan.position,
                "response_id": dispatched.response_id,
            },
        )
        self.ledger.mark_dispatched(
            campaign_id,
            attempt_plan.position,
            task_id,
            dispatched.response_id,
            request_sha256,
            request_envelope=dispatched.request_envelope,
        )

    def _collect_poll(
        self,
        campaign_id: str,
        task_id: str,
        record: AttemptRecord,
        client: Client,
        response_id: str,
        plan: RequestPlan,
        schema: type[BaseModel],
        choice: ModelChoice,
        started: float,
    ) -> BackgroundPoll:
        """Collect one poll of a dispatched response, pausing the campaign if it is gone.

        Every other poll failure leaves the attempt dispatched for a later retry.
        """
        try:
            return client.collect_background(
                response_id,
                plan,
                schema,
                choice,
                started,
                request_envelope=record.request_envelope,
            )
        except Exception as error:
            if _is_response_gone(error):
                self.ledger.mark_uncertain(
                    campaign_id,
                    record.position,
                    task_id,
                    str(error),
                    error_category=_error_category(error),
                )
            raise

    @staticmethod
    def _maybe_cancel(
        client: Client,
        context: TaskContext | None,
        response_id: str,
        record: AttemptRecord,
        cancelled: bool,
        funnel: _RunFunnel,
    ) -> bool:
        """Request cancellation of a still-pending response once, then report poll progress."""
        if context is not None and context.cancellation_requested() and not cancelled:
            try:
                if isinstance(client.provider, BackgroundWebSearchProvider):
                    client.provider.cancel_background(response_id)
            except ClearscoutError as error:
                # The response can finish between poll and cancel; the next poll captures it.
                logger.warning(
                    "Campaign attempt cancel was refused",
                    extra={"response_id": response_id, "error": str(error)},
                )
            cancelled = True
        funnel.checkpoint(context, phase="polling_background", attempt=record.position)
        return cancelled

    def _poll_until_terminal(
        self,
        campaign_id: str,
        task_id: str,
        record: AttemptRecord,
        response_id: str,
        plan: RequestPlan,
        schema: type[BaseModel],
        choice: ModelChoice,
        started: float,
        client: Client,
        context: TaskContext | None,
        funnel: _RunFunnel,
    ) -> tuple[BackgroundPoll, bool]:
        """Poll one dispatched response to a terminal status, canceling it at most once."""
        cancelled = False

        def pending() -> None:
            nonlocal cancelled
            cancelled = self._maybe_cancel(client, context, response_id, record, cancelled, funnel)

        poll = poll_until_terminal(
            lambda: self._collect_poll(
                campaign_id, task_id, record, client, response_id, plan, schema, choice, started
            ),
            self.sleeper,
            on_pending=pending,
        )
        return poll, cancelled

    def _poll_and_capture(
        self,
        workspace_id: str,
        campaign_id: str,
        task_id: str,
        record: AttemptRecord,
        attempt_plan: CampaignAttemptPlan,
        inputs: _RunInputs,
        client: Client,
        context: TaskContext | None,
        funnel: _RunFunnel,
    ) -> None:
        """Poll a dispatched attempt to a terminal state, then capture it."""
        if record.provider_response_id is None:
            raise ConflictError(
                f"Attempt {record.position} is dispatched with no provider response id."
            )
        response_id = record.provider_response_id
        # capture() records every entry in the ledger it is given, so each attempt gets its own.
        client.ledger = replace(client.ledger, entries=[])
        manifest = inputs.manifest
        choice = _choice(manifest.identities)
        schema = campaign_response_schema(tuple(branch.key for branch in manifest.branches))
        plan = self._request_plan(inputs, attempt_plan)
        started = self.clock()
        poll, cancelled = self._poll_until_terminal(
            campaign_id,
            task_id,
            record,
            response_id,
            plan,
            schema,
            choice,
            started,
            client,
            context,
            funnel,
        )
        captured_urls, refused_model_picks = captured_url_rows(poll)
        self.ledger.capture(
            campaign_id,
            record.position,
            task_id,
            workspace_id=workspace_id,
            poll=poll,
            ledger=client.ledger,
            provider=client.provider.name.value,
            branch_queries=matched_queries(manifest, attempt_plan, poll),
            has_unattributable_search=any(
                not search.queries
                for search in (poll.record.searches if poll.record is not None else ())
            ),
            captured_urls=captured_urls,
            refused_model_picks=refused_model_picks,
        )
        if cancelled:
            raise _CancelledError()

    def _process(
        self,
        workspace_id: str,
        campaign_id: str,
        task_id: str,
        record: AttemptRecord,
        inputs: _RunInputs,
        funnel: _RunFunnel,
        context: TaskContext | None = None,
    ) -> None:
        """Read captured URLs, admit them, retain staged bodies, then mark the attempt processed."""
        reads = read_captured_urls(
            self.scans,
            parameters=inputs.parameters,
            searched_titles=inputs.searched_titles,
            captured=self._observed_urls(campaign_id, record.id),
            known_source_roots=inputs.known_source_roots,
            known_posting_fingerprints=inputs.known_posting_fingerprints,
            should_stop=context.cancellation_requested if context is not None else None,
        )
        # A read cut short by cancellation would admit unread URLs as unresolved; keep it captured.
        _raise_if_cancelled(context)
        with self.database.exclusive_session() as session:
            admissions, funnel_rows = admit_read_urls(
                session,
                self.scans,
                workspace_id=workspace_id,
                today=utc_now().date(),
                correlation_id=task_id,
                reads=reads,
            )
            self.ledger.record_admissions_in_session(
                session, campaign_id, record.position, task_id, admissions
            )
        # The transaction rolls back whole, and a heartbeat opens its own write session,
        # so both the count and the checkpoint wait until it has closed.
        funnel.record(funnel_rows)
        funnel.checkpoint(context, phase="admitting_urls", attempt=record.position)
        _raise_if_cancelled(context)
        try:
            funnel.note_withdrawn_on_read(
                self._retain_staged_content(
                    workspace_id, campaign_id, record.position, reads, context
                )
            )
        except Exception:
            # Content resolution cancels through the worker's own exception type.
            _raise_if_cancelled(context)
            raise
        # Content resolution publishes its own funnel-free progress, which replaces this
        # payload whole, so the attestation is restored once retention has finished.
        funnel.checkpoint(context)
        _raise_if_cancelled(context)
        with self.database.exclusive_session() as session:
            self.ledger.mark_processed_in_session(session, campaign_id, record.position, task_id)

    def _retain_staged_content(
        self,
        workspace_id: str,
        campaign_id: str,
        position: int,
        reads: tuple[ReadUrl, ...],
        context: TaskContext | None,
    ) -> list[str]:
        """Retain prefetched bodies, resolve the rest, and return the rows the reads closed."""
        reads_by_key = {posting_url_key(read.item.url): read for read in reads}
        unresolved_ids: list[str] = []
        for url_sha256, pending_id in self.ledger.admitted_discoveries(campaign_id, position):
            if self.content_resolution.current.read(workspace_id, pending_id) is not None:
                continue
            read = reads_by_key.get(url_sha256)
            resolved = (
                resolved_publisher_fetch(read.staged_url or read.item.url, read.posting)
                if read is not None and read.posting is not None
                else None
            )
            if resolved is None:
                unresolved_ids.append(pending_id)
                continue
            self.content_resolution.store_prefetched_result(
                workspace_id,
                pending_id,
                resolved,
                context,
                origin="search.guided_campaign",
            )
        if not unresolved_ids:
            return []
        resolution = self.content_resolution.resolve_automatic(
            workspace_id,
            unresolved_ids,
            context,
            origin="search.guided_campaign",
        )
        return list(resolution["withdrawn_ids"])

    def _observed_urls(self, campaign_id: str, attempt_id: str) -> tuple[ObservedUrl, ...]:
        """Return this attempt's still-unread captured URLs with the actions that surfaced them."""
        with self.database.session() as session:
            attempt = session.get(GuidedCampaignAttempt, attempt_id)
            board_shas = frozenset(
                (attempt.capture or {}).get("board_url_sha256s", []) if attempt is not None else ()
            )
            captured = session.scalars(
                select(GuidedCampaignCapturedUrl).where(
                    GuidedCampaignCapturedUrl.campaign_id == campaign_id,
                    GuidedCampaignCapturedUrl.outcome == CapturedUrlOutcome.PENDING_READ,
                )
            ).all()
            observations = session.scalars(
                select(GuidedCampaignUrlObservation).where(
                    GuidedCampaignUrlObservation.attempt_id == attempt_id
                )
            ).all()
            by_captured: dict[str, list[tuple[str, str]]] = {}
            for observation in observations:
                by_captured.setdefault(observation.captured_url_id, []).append(
                    (observation.action_kind, observation.query)
                )
            return tuple(
                ObservedUrl(
                    url=row.url,
                    observations=tuple(by_captured[row.id]),
                    model_selected=row.model_selected,
                    model_why=row.model_why,
                    model_board=row.url_sha256 in board_shas,
                )
                for row in captured
                if row.id in by_captured
            )

    @staticmethod
    def _incompletion_reasons(
        attempts: tuple[AttemptRecord, ...], manifest: CampaignManifest
    ) -> list[str]:
        """Return every reason this campaign's captured evidence falls short of complete."""
        reasons: list[str] = []
        if any(
            item.state not in (AttemptState.PROCESSED, AttemptState.ABANDONED) for item in attempts
        ):
            reasons.append(CampaignReason.INCOMPLETE_ATTEMPTS.value)
        if any(item.finish == "truncated" for item in attempts):
            reasons.append(CampaignReason.TRUNCATED_RESPONSE.value)
        if sum(item.hosted_search_count for item in attempts) > manifest.bounds.max_hosted_searches:
            reasons.append(CampaignReason.SEARCH_OVERRUN.value)
        searched = {
            key
            for item in attempts
            for key, outcome in item.branch_outcomes
            if outcome == BranchOutcome.SEARCHED
        }
        protected = {branch.priority_family for branch in manifest.branches if branch.protected}
        covered = {branch.priority_family for branch in manifest.branches if branch.key in searched}
        if protected - covered:
            reasons.append(CampaignReason.PROTECTED_FAMILY_UNSEARCHED.value)
        return reasons

    def _finish(self, campaign_id: str, task_id: str, manifest: CampaignManifest) -> list[str]:
        """Settle the campaign from its captured evidence; an uncertain attempt leaves it paused."""
        attempts = self.ledger.attempts(campaign_id)
        if any(item.state == AttemptState.UNCERTAIN for item in attempts):
            return [CampaignReason.UNCERTAIN_ATTEMPT.value]
        reasons = self._incompletion_reasons(attempts, manifest)
        # A search overrun is reported but does not make an otherwise finished campaign incomplete.
        blocking = set(reasons) - {CampaignReason.SEARCH_OVERRUN.value}
        self.ledger.finish(
            campaign_id,
            task_id,
            status=CampaignStatus.INCOMPLETE if blocking else CampaignStatus.COMPLETED,
            reasons=reasons,
        )
        return reasons


class GuidedCampaignHandler:
    """Worker adapter for one approved guided-campaign cycle."""

    def __init__(
        self,
        database: Database,
        *,
        storage: RuntimeStorage,
        posting_fetcher: PostingFetcher,
        content_resolution: PostingContentResolutionService | None = None,
        fetcher: SourceReader | None = None,
        provider: Provider | None = None,
        http_client: PoliteHttpClient | None = None,
    ) -> None:
        """Build the campaign service when automatic content resolution is available."""
        self.service = (
            GuidedCampaignService(
                database,
                storage=storage,
                fetcher=fetcher,
                posting_fetcher=posting_fetcher,
                content_resolution=content_resolution,
                provider=provider,
                http_client=http_client,
            )
            if content_resolution is not None
            else None
        )

    def __call__(self, context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Run one approved guided-campaign cycle in this task's workspace."""
        if self.service is None:
            raise ValidationError("A guided campaign requires automatic content resolution.")
        value = GuidedCampaignPayload.model_validate(payload)
        task = context.queue.get(context.task_id)
        return self.service.run(
            str(task.workspace_id),
            campaign_manifest_sha256=value.campaign_manifest_sha256,
            campaign_cycle=value.campaign_cycle,
            max_model_calls=value.max_model_calls,
            max_hosted_searches=value.max_hosted_searches,
            context=context,
        )


class GuidedCampaignRetryHandler:
    """Worker adapter that reprocesses stranded captured attempts without a provider call."""

    def __init__(self, service: GuidedCampaignService | None) -> None:
        """Bind the campaign service; None when the worker has no content resolution."""
        self.service = service

    def __call__(self, context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Run one retry pass over this task's workspace."""
        GuidedCampaignRetryPayload.model_validate(payload)
        if self.service is None:
            raise ValidationError(
                "A campaign processing retry requires automatic content resolution."
            )
        task = context.queue.get(context.task_id)
        funnel = _RunFunnel()
        try:
            processed = self.service.retry_stranded_processing(
                str(task.workspace_id), context.task_id, context, funnel=funnel
            )
        except _CancelledError:
            context.raise_if_cancelled()
            raise
        return {
            "processed_attempts": processed,
            "withdrawn_on_read": funnel.withdrawn_on_read,
            "funnel": funnel.payload(),
        }
