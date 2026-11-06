"""Use cited hosted search only to locate deterministic posting reads."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import Enum, StrEnum
from typing import Any, Literal, Protocol

from pydantic import AnyHttpUrl, BaseModel, Field, create_model
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.orm import Session

from clearscout.application.config import Settings, Stage
from clearscout.application.discovery.content_resolution import (
    AddressHolder,
    PostingContentResolutionService,
    PostingResolutionTarget,
    ResolvedPostingContent,
    StoredEvidence,
    validate_resolved_posting_content,
)
from clearscout.application.discovery.paid_task import billed_run
from clearscout.application.discovery.posting_identity import posting_host
from clearscout.application.discovery.scan_queries import (
    registered_board_holder,
    registered_employer_boards,
)
from clearscout.application.failures import ConflictError, ValidationError
from clearscout.application.prompts import digest as prompt_digest
from clearscout.application.prompts import load, render
from clearscout.application.tasks import TaskContext
from clearscout.application.tasks.models import PostingRecoveryApprovalManifest
from clearscout.application.workspaces import WorkspaceSettingsService
from clearscout.domain import (
    PostingContentAccessStatus,
    PostingIdentityStatus,
    PostingStatus,
)
from clearscout.domain.identity import exact_url_key, url_host
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.providers.base import (
    Provider,
    UnusableResponseError,
    wire_schema,
)
from clearscout.infrastructure.providers.client import Client
from clearscout.infrastructure.providers.plan import (
    RequestPlan,
    SearchDepth,
    SearchPolicy,
    Segment,
    schema_digest,
)
from clearscout.infrastructure.web_fetch.board_platform import host_match
from clearscout.infrastructure.web_fetch.boards import (
    BOARD_PATTERNS,
    JOB_URL_PATTERNS,
    single_tenant_board,
)
from clearscout.shared.costs import CallManifest, ModelPrice, Usage, cents
from clearscout.shared.costs.prices import priced_model
from clearscout.shared.costs.prices import table as price_table
from clearscout.shared.errors import IngestError, ProviderError
from clearscout.shared.models import StrictModel

MAX_ASSISTED_RECOVERY_ITEMS = 20
# OpenAI's max_tool_calls counts page opens as well as searches, and the prompt holds
# searches to two of the four; Anthropic's max_uses counts searches only.
MAX_HOSTED_TOOL_CALLS = 4
APPROVAL_LIFETIME = timedelta(minutes=15)


class PostingBodyCompleteness(StrEnum):
    """How complete a consulted page appears, never a content guarantee."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    IDENTITY_ONLY = "identity_only"
    NONE = "none"
    UNKNOWN = "unknown"


class PostingRecoveryOutcome(StrEnum):
    """One assisted identity's deterministic final disposition."""

    RECOVERED = "recovered"
    CLOSED = "closed"
    AMBIGUOUS = "ambiguous"
    SOURCE_FOUND_UNREADABLE = "source_found_unreadable"
    NOT_FOUND = "not_found"
    POLICY_REFUSED = "policy_refused"
    PROVIDER_FAILED = "provider_failed"
    # A billed answer the local cited-search checks refused, not a provider fault.
    RESPONSE_REFUSED = "response_refused"


# Plain Enum, not StrEnum: three members share a name with PostingRecoveryOutcome,
# and as StrEnums they would compare equal across the two types with no type error.
class PostingRecoveryDecision(Enum):
    """What one identity's cited candidates settle before any page is read."""

    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    SOURCE_FOUND_UNREADABLE = "source_found_unreadable"
    CANDIDATE_READY = "candidate_ready"


class PostingSourceAuthority(StrEnum):
    """The relationship a consulted page claims to the posting."""

    PUBLISHER_BODY = "publisher_body"
    PUBLISHER_INDEX = "publisher_index"
    ATS = "ats"
    AGGREGATOR = "aggregator"
    SEARCH_RESULT = "search_result"


# Only a publisher-controlled page may be read for a posting body; every other
# authority is treated as a source found unreadable.
READABLE_SOURCE_AUTHORITIES = frozenset(
    {PostingSourceAuthority.PUBLISHER_BODY, PostingSourceAuthority.ATS}
)

# Read outcomes where the site refused the client, so its other pages refuse it too.
_HOST_REFUSALS = frozenset(
    {
        PostingContentAccessStatus.ACCESS_BLOCKED.value,
        PostingContentAccessStatus.SESSION_REQUIRED.value,
    }
)


class PostingRecoveryObservation(StrictModel):
    """One bounded, URL-scoped observation with no posting-body field."""

    url: AnyHttpUrl
    authority: PostingSourceAuthority
    completeness: PostingBodyCompleteness
    identity: Literal["exact", "plausible", "contradicted", "unknown"]
    posting_status: Literal["active", "closed", "unknown"]
    note: str = Field(min_length=1, max_length=500)


class PostingRecoveryResponse(StrictModel):
    """One identity's cited URL suggestions; content is intentionally absent."""

    identity_id: str = Field(min_length=1)
    candidate_urls: list[AnyHttpUrl] = Field(default_factory=list, max_length=4)
    supporting_urls: list[AnyHttpUrl] = Field(default_factory=list, max_length=8)
    observations: list[PostingRecoveryObservation] = Field(default_factory=list, max_length=8)


@dataclass(frozen=True)
class AssistedPostingRecoveryItem:
    """One identity's isolated outcome and deterministic effect result."""

    pending_discovery_id: str
    outcome: PostingRecoveryOutcome
    candidate_url: str | None = None
    observations: tuple[PostingRecoveryObservation, ...] = ()
    resolved: ResolvedPostingContent | None = None
    detail: str = ""
    content_evidence_id: str | None = None
    address_holder: AddressHolder | None = None


@dataclass(frozen=True)
class AssistedPostingRecoveryBatch:
    """Ordered per-item outcomes from one approved assisted batch."""

    items: tuple[AssistedPostingRecoveryItem, ...]
    model_calls_made: int


@dataclass(frozen=True)
class RecoveryCandidate:
    """One cited plausible identity and the page authority reported for it."""

    url: str
    observation: PostingRecoveryObservation
    # The known URL whose last read failed, or a page on a host that refused that read;
    # reading it repeats the failure.
    already_failed: bool = False


class CancellationContext(Protocol):
    """The task behavior used between isolated identity calls."""

    task_id: str

    def raise_if_cancelled(self) -> None:
        """Raise the queue's cancellation exception when requested."""

    def progress(self, stage: str, detail: dict[str, Any] | None = None) -> None:
        """Append one durable progress event."""


class AssistedContentResolutionService:
    """Approve, search, cite-check, and deterministically resolve posting identities."""

    def __init__(
        self,
        database: Database,
        content: PostingContentResolutionService,
        *,
        provider: Provider | None = None,
    ) -> None:
        """Bind snapshot ownership, deterministic reads, billing, and a provider seam."""
        self.database = database
        self.content = content
        self.provider = provider

    def preflight(
        self,
        workspace_id: str,
        pending_discovery_ids: list[str],
        settings: Settings,
        *,
        now: datetime | None = None,
    ) -> PostingRecoveryApprovalManifest:
        """Return an expiring bounded estimate without provider access."""
        issued_at = _aware_now(now)
        preflight = self.content.preflight(workspace_id, pending_discovery_ids)
        already_ready = [
            target.id
            for target in preflight.targets
            if self.content.current.read(workspace_id, target.id) is not None
        ]
        if already_ready:
            raise ValidationError(
                "Assisted recovery accepts only postings that still need content recovery: "
                + ", ".join(already_ready)
            )
        if len(preflight.targets) > MAX_ASSISTED_RECOVERY_ITEMS:
            raise ValidationError(
                f"Assisted posting recovery accepts at most {MAX_ASSISTED_RECOVERY_ITEMS} items."
            )
        with self.database.session() as session:
            targets = _with_registered_boards(session, workspace_id, preflight.targets)
            on_board = [
                f"{target.id} is held by {holder.entity_type.replace('_', ' ')} "
                f"{holder.entity_id} at {holder.url}"
                for target in targets
                if (
                    holder := registered_board_holder(
                        session, workspace_id, target.id, count_dismissed=True
                    )
                )
            ]
        if on_board:
            raise ValidationError(
                "Assisted recovery refuses postings the employer's registered board already "
                "holds: " + "; ".join(on_board)
            )
        return _approval_manifest(
            targets,
            preflight.snapshot_sha256,
            settings,
            issued_at=issued_at,
        )

    def recover(
        self,
        workspace_id: str,
        approval: PostingRecoveryApprovalManifest,
        settings: Settings,
        *,
        task_id: str,
        context: CancellationContext | TaskContext | None = None,
        now: datetime | None = None,
    ) -> AssistedPostingRecoveryBatch:
        """Run one cited search per approved identity, isolating item failures."""
        approval_time = _aware_now(now)
        _validate_approval_time(approval, approval_time)
        current = self.content.preflight(workspace_id, list(approval.pending_discovery_ids))
        with self.database.session() as session:
            targets = _with_registered_boards(session, workspace_id, current.targets)
        expected = _approval_manifest(
            targets,
            current.snapshot_sha256,
            settings,
            issued_at=approval.issued_at,
        )
        if expected != approval:
            raise ConflictError("Assisted posting recovery changed after approval preflight.")
        with billed_run(
            self.database,
            workspace_id=workspace_id,
            task_id=task_id,
            settings=settings,
            origin="posting.assisted_content_resolution",
            provider=self.provider,
        ) as run:
            client = run.build_client()
            items = self._recover_targets(workspace_id, targets, client, context)
            return AssistedPostingRecoveryBatch(
                items=items,
                model_calls_made=len(run.ledger.entries),
            )

    def _recover_targets(
        self,
        workspace_id: str,
        targets: tuple[PostingResolutionTarget, ...],
        client: Client,
        context: CancellationContext | TaskContext | None,
    ) -> tuple[AssistedPostingRecoveryItem, ...]:
        items: list[AssistedPostingRecoveryItem] = []
        for position, target in enumerate(targets, start=1):
            if context is not None:
                context.raise_if_cancelled()
                context.progress(
                    "recovering_posting_content",
                    {
                        "current": position,
                        "total": len(targets),
                        "pending_discovery_id": target.id,
                    },
                )
            item = self._recover_one(target, client)
            if item.resolved is not None:
                resolved = _with_assisted_observations(item.resolved, item.observations)
                stored = self.content.store_assisted_result(
                    workspace_id,
                    target,
                    resolved,
                    context if isinstance(context, TaskContext) else None,
                )
                item = _with_stored_evidence(item, stored)
            else:
                attempt_evidence_id = self.content.store_assisted_failure(
                    workspace_id,
                    target,
                    outcome=item.outcome.value,
                    detail=item.detail,
                    candidate_url=item.candidate_url,
                    context=context if isinstance(context, TaskContext) else None,
                )
                item = replace(item, content_evidence_id=attempt_evidence_id)
            items.append(item)
            if context is not None:
                context.progress(
                    "posting_content_recovery_recorded",
                    {
                        "current": position,
                        "total": len(targets),
                        "pending_discovery_id": target.id,
                        "outcome": item.outcome.value,
                        "content_evidence_id": item.content_evidence_id,
                        "detail": item.detail,
                    },
                )
        return tuple(items)

    def _recover_one(
        self,
        target: PostingResolutionTarget,
        client: Client,
    ) -> AssistedPostingRecoveryItem:
        try:
            search = cited_recovery_search(target, client)
        except RecoveryResponseRefusedError as error:
            return _failed(target.id, PostingRecoveryOutcome.RESPONSE_REFUSED, error)
        except ProviderError as error:
            return _failed(target.id, PostingRecoveryOutcome.PROVIDER_FAILED, error)
        response, decision, candidate = search.response, search.decision, search.candidate
        if candidate is None or decision is not PostingRecoveryDecision.CANDIDATE_READY:
            outcome, detail = _DECIDED_WITHOUT_READING[decision]
            if decision is PostingRecoveryDecision.NOT_FOUND and not search.searched_sources:
                detail = "Hosted search returned no sources and opened no pages."
            if candidate is not None and candidate.already_failed:
                place = (
                    "is the known URL,"
                    if _url_key(candidate.url) == _url_key(target.url)
                    else f"is on {posting_host(target.url)}, where the known URL was"
                )
                detail = (
                    f"The cited candidate {place} already read as {known_url_read_outcome(target)}."
                )
            return AssistedPostingRecoveryItem(
                target.id,
                outcome,
                None if candidate is None else candidate.url,
                tuple(response.observations),
                detail=detail,
            )
        try:
            resolved = self.content.resolver.resolve(
                candidate.url,
                employer=target.expected_employer,
                title=target.expected_title,
            )
            validate_resolved_posting_content(candidate.url, resolved)
            outcome = _deterministic_outcome(resolved)
        except (IngestError, ValidationError, ValueError) as error:
            return AssistedPostingRecoveryItem(
                target.id,
                PostingRecoveryOutcome.SOURCE_FOUND_UNREADABLE,
                candidate.url,
                tuple(response.observations),
                detail=" ".join(str(error).split())[:500],
            )
        return AssistedPostingRecoveryItem(
            target.id,
            outcome,
            candidate.url,
            tuple(response.observations),
            resolved,
            _outcome_detail(outcome),
        )


def _with_stored_evidence(
    item: AssistedPostingRecoveryItem, stored: StoredEvidence
) -> AssistedPostingRecoveryItem:
    """Attach the stored evidence, and name the record that kept the recovered address."""
    holder = stored.address_holder
    if holder is None:
        return replace(item, content_evidence_id=stored.id)
    detail = (
        f"{item.detail} The address is already held by "
        f"{holder.entity_type.replace('_', ' ')} {holder.entity_id}, so this row keeps its own."
    )
    return replace(item, content_evidence_id=stored.id, address_holder=holder, detail=detail)


def _with_assisted_observations(
    resolved: ResolvedPostingContent,
    observations: tuple[PostingRecoveryObservation, ...],
) -> ResolvedPostingContent:
    """Retain the cited observations alongside the deterministic result."""
    identity_evidence = dict(resolved.identity_evidence or {})
    identity_evidence["assisted_observations"] = [
        observation.model_dump(mode="json") for observation in observations
    ]
    return replace(resolved, identity_evidence=identity_evidence)


class AssistedContentResolutionHandler:
    """Execute an approved assisted posting-recovery task with current settings."""

    def __init__(self, database: Database, service: AssistedContentResolutionService) -> None:
        """Bind the recovery service and workspace-effective settings owner."""
        self.service = service
        self.settings = WorkspaceSettingsService(database)

    def __call__(self, context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Recover and durably record every item in the approved ordered batch."""
        task = context.queue.get(context.task_id)
        approval = PostingRecoveryApprovalManifest.model_validate(payload.get("approval"))
        batch = self.service.recover(
            str(task.workspace_id),
            approval,
            self.settings.current(str(task.workspace_id)),
            task_id=context.task_id,
            context=context,
            now=task.created_at,
        )
        return {
            "selected": len(batch.items),
            "model_calls_made": batch.model_calls_made,
            "items": [
                {
                    "pending_discovery_id": item.pending_discovery_id,
                    "outcome": item.outcome.value,
                    "candidate_url": item.candidate_url,
                    "detail": item.detail,
                    "content_evidence_id": item.content_evidence_id,
                }
                for item in batch.items
            ],
        }


def response_schema(identity_id: str) -> type[BaseModel]:
    """Close the response's identity field over the one identity being recovered."""
    identity = Literal[identity_id]  # type: ignore[valid-type]
    return create_model(
        "ConstrainedPostingRecoveryResponse",
        __base__=PostingRecoveryResponse,
        identity_id=(identity, ...),
    )


def _search_policy() -> SearchPolicy:
    return SearchPolicy(
        max_calls=MAX_HOSTED_TOOL_CALLS,
        depth=SearchDepth.MEDIUM,
        live=True,
        capture_sources=True,
    )


def known_url_read_outcome(target: PostingResolutionTarget) -> str | None:
    """Return why the last deterministic read of the known URL failed, or None if it did not."""
    if target.content_status in {
        PostingContentAccessStatus.NOT_YET_READ,
        PostingContentAccessStatus.READABLE,
    }:
        return None
    return target.content_status.value


def request_plan(target: PostingResolutionTarget, settings: Settings) -> RequestPlan:
    """Compose the cited-search request one posting identity sends."""
    policy = settings.policy_for(Stage.POSTING_RECOVERY)
    fields: dict[str, str] = {
        "identity_id": target.id,
        "employer": target.employer,
        "title": target.title,
        "known_url": target.url,
    }
    if (known_read := known_url_read_outcome(target)) is not None:
        fields["known_url_read_outcome"] = known_read
    # Only an employer's own ATS tenant is named; any other host may be a reposting site.
    # A registered board on another host means the employer left that tenant.
    board_host = url_host(target.registered_board_url) if target.registered_board_url else ""
    host = url_host(target.url) if single_tenant_board(target.url) else ""
    if host and board_host in {"", host}:
        fields["known_publisher_domain"] = host
    if target.registered_board_url:
        fields["registered_board_url"] = target.registered_board_url
    identity = json.dumps(
        fields,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return RequestPlan(
        stage=Stage.POSTING_RECOVERY,
        instructions=Segment(render("posting_recovery_system"), "posting_recovery_system"),
        task=Segment(
            render("posting_recovery_user", posting_identity=identity),
            "posting_recovery_user",
        ),
        search=_search_policy(),
        max_tokens=policy.max_tokens,
    )


def _approval_manifest(
    targets: tuple[PostingResolutionTarget, ...],
    snapshot_sha256: str,
    settings: Settings,
    *,
    issued_at: datetime,
) -> PostingRecoveryApprovalManifest:
    choice = settings.choice_for_stage(Stage.POSTING_RECOVERY)
    prices = price_table(settings.prices)
    priced = priced_model(prices, choice.model, provider=settings.provider.value)
    price = prices.get(priced) if priced is not None else None
    if price is None or not price.prices_searches(MAX_HOSTED_TOOL_CALLS):
        raise ValidationError(
            f"No complete token and hosted-search price is configured for {choice.model}."
        )
    plans = tuple(request_plan(target, settings) for target in targets)
    schemas = tuple(response_schema(target.id) for target in targets)
    estimate_cents = _estimate_cents(settings, plans, schemas, price)
    schema_sha256 = _combined_digest(
        schema_digest(wire_schema(settings.provider, schema)) for schema in schemas
    )
    setting = settings.policy_for(Stage.POSTING_RECOVERY)
    settings_sha256 = _json_digest(
        {
            "stage": setting.model_dump(mode="json"),
            "choice": choice.model_dump(mode="json"),
            "search": _search_policy().stated(),
        }
    )
    price_sha256 = _json_digest({"model": priced, "price": price.model_dump(mode="json")})
    return PostingRecoveryApprovalManifest(
        pending_discovery_ids=tuple(target.id for target in targets),
        snapshot_sha256=snapshot_sha256,
        identity_sha256=_combined_digest(plan.task.digest for plan in plans),
        provider=settings.provider.value,
        model=choice.model,
        system_prompt_sha256=prompt_digest(load("posting_recovery_system")),
        user_prompt_sha256=prompt_digest(load("posting_recovery_user")),
        schema_sha256=schema_sha256,
        settings_sha256=settings_sha256,
        price_sha256=price_sha256,
        estimate_cents=estimate_cents,
        issued_at=issued_at,
        expires_at=issued_at + APPROVAL_LIFETIME,
    )


def _estimate_cents(
    settings: Settings,
    plans: tuple[RequestPlan, ...],
    schemas: tuple[type[BaseModel], ...],
    price: ModelPrice,
) -> int:
    dollars = 0.0
    for plan, schema in zip(plans, schemas, strict=True):
        document = wire_schema(settings.provider, schema)
        input_ceiling = (
            sum(
                len(value.encode("utf-8"))
                for value in (
                    plan.instruction_text(),
                    plan.task.text,
                    json.dumps(document, sort_keys=True, separators=(",", ":")),
                )
            )
            + 512
        )
        # The provider enforces only the total tool budget, so every call in it is
        # priced as the fee-bearing search it could be; page opens carry no fee.
        dollars += price.cost(
            Usage(input_tokens=input_ceiling, output_tokens=plan.max_tokens),
            MAX_HOSTED_TOOL_CALLS,
        )
    return cents(dollars)


def _validate_approval_time(
    approval: PostingRecoveryApprovalManifest,
    now: datetime,
) -> None:
    if approval.issued_at.tzinfo is None or approval.expires_at.tzinfo is None:
        raise ValidationError("Posting recovery approval timestamps must be timezone-aware.")
    if approval.expires_at != approval.issued_at + APPROVAL_LIFETIME:
        raise ValidationError("Posting recovery approval has an invalid expiry interval.")
    if now < approval.issued_at or now >= approval.expires_at:
        raise ValidationError("Posting recovery approval is not currently valid.")


def validated_recovery_candidates(
    target: PostingResolutionTarget,
    response: PostingRecoveryResponse,
    new_entries: list[CallManifest],
) -> list[RecoveryCandidate]:
    """Check one recovery response against its own ledger call and return its cited candidates."""
    if response.identity_id != target.id:
        raise ValidationError("Posting recovery returned a different identity.")
    if len(new_entries) != 1:
        raise ValidationError("Posting recovery requires exactly one provider call per identity.")
    manifest = new_entries[0]
    _validate_search_record(manifest)
    known_read = known_url_read_outcome(target)
    host_refused = known_read in _HOST_REFUSALS and not _shared_ats_host(target.url)
    return _cited_candidates(
        target.url,
        response,
        _captured_source_urls(manifest),
        known_failed=known_read is not None,
        failed_host=posting_host(target.url) if host_refused else None,
    )


def _shared_ats_host(url: str) -> bool:
    """Return whether a URL is a record on an ATS or aggregator host serving many employers."""
    return any(host_match(pattern, url) for _, pattern in BOARD_PATTERNS) or any(
        host_match(pattern, url) for pattern in JOB_URL_PATTERNS
    )


def _with_registered_boards(
    session: Session, workspace_id: str, targets: tuple[PostingResolutionTarget, ...]
) -> tuple[PostingResolutionTarget, ...]:
    return tuple(
        replace(
            target,
            registered_board_url=_registered_board_url(session, workspace_id, target.employer),
        )
        for target in targets
    )


def _registered_board_url(session: Session, workspace_id: str, employer: str) -> str:
    """Return the employer's job board address when the workspace registers exactly one."""
    urls = list(registered_employer_boards(session, workspace_id, employer).values())
    return urls[0] if len(urls) == 1 else ""


class RecoveryResponseRefusedError(ValidationError):
    """A recovery answer the program refused; its billed calls are already marked refused."""


@dataclass(frozen=True)
class RecoverySearch:
    """One identity's validated cited search and the decision it settles, before any read."""

    response: PostingRecoveryResponse
    candidates: list[RecoveryCandidate]
    decision: PostingRecoveryDecision
    candidate: RecoveryCandidate | None
    searched_sources: bool


def cited_recovery_search(target: PostingResolutionTarget, client: Client) -> RecoverySearch:
    """Send one identity's cited search and settle it without reading a page.

    A provider failure other than a schema refusal propagates unchanged.
    """
    entries = client.ledger.entries
    before = len(entries)
    try:
        raw = client.complete(request_plan(target, client.settings), response_schema(target.id))
        response = PostingRecoveryResponse.model_validate(raw.model_dump(mode="json"))
        candidates = validated_recovery_candidates(target, response, entries[before:])
    except (ProviderError, ValidationError, ValueError) as error:
        if isinstance(error, ProviderError) and not _schema_refused(error):
            raise
        for position in range(before, len(entries)):
            client.ledger.refuse(position)
        raise RecoveryResponseRefusedError(str(error)) from error
    decision, candidate = recovery_decision(candidates)
    return RecoverySearch(
        response,
        candidates,
        decision,
        candidate,
        searched_sources=bool(_captured_source_urls(entries[before])),
    )


def recovery_decision(
    candidates: list[RecoveryCandidate],
) -> tuple[PostingRecoveryDecision, RecoveryCandidate | None]:
    """Settle one identity from its cited candidates, and name the one worth reading."""
    if not candidates:
        return PostingRecoveryDecision.NOT_FOUND, None
    readable = [
        candidate
        for candidate in candidates
        if candidate.observation.authority in READABLE_SOURCE_AUTHORITIES
        and not candidate.already_failed
    ]
    if len(readable) > 1:
        return PostingRecoveryDecision.AMBIGUOUS, None
    if readable:
        return PostingRecoveryDecision.CANDIDATE_READY, readable[0]
    return PostingRecoveryDecision.SOURCE_FOUND_UNREADABLE, candidates[0]


_DECIDED_WITHOUT_READING: dict[PostingRecoveryDecision, tuple[PostingRecoveryOutcome, str]] = {
    PostingRecoveryDecision.NOT_FOUND: (
        PostingRecoveryOutcome.NOT_FOUND,
        "Hosted search returned no cited exact publisher candidate.",
    ),
    PostingRecoveryDecision.AMBIGUOUS: (
        PostingRecoveryOutcome.AMBIGUOUS,
        "Hosted search returned multiple plausible exact publisher candidates.",
    ),
    PostingRecoveryDecision.SOURCE_FOUND_UNREADABLE: (
        PostingRecoveryOutcome.SOURCE_FOUND_UNREADABLE,
        "The exact cited source does not expose a publisher-controlled body.",
    ),
}


def _captured_source_urls(manifest: CallManifest) -> set[str]:
    urls: set[str] = set()
    for search in manifest.searches:
        sources = search.get("sources", [])
        if not isinstance(sources, list):
            continue
        for source in sources:
            if isinstance(source, dict) and isinstance(source.get("url"), str):
                urls.add(_url_key(source["url"]))
    # An aborted page action read nothing, and an open_page action may name no URL.
    urls.update(
        _url_key(action["url"])
        for action in manifest.page_actions
        if action.get("status") == "completed" and action.get("url")
    )
    return urls


def _validate_search_record(manifest: CallManifest) -> None:
    if not 1 <= manifest.search_count <= MAX_HOSTED_TOOL_CALLS:
        raise ValidationError(
            f"Posting recovery requires 1-{MAX_HOSTED_TOOL_CALLS} actual hosted searches."
        )


def _cited_candidates(
    known_url: str,
    response: PostingRecoveryResponse,
    captured_urls: set[str],
    *,
    known_failed: bool,
    failed_host: str | None,
) -> list[RecoveryCandidate]:
    known = _url_key(known_url)
    _validate_supporting_urls(response, known, captured_urls)
    observations = _observations_by_url(response, known, captured_urls)
    accepted: dict[str, RecoveryCandidate] = {}
    for candidate_url in response.candidate_urls:
        candidate = str(candidate_url)
        key = _url_key(candidate)
        _require_cited_candidate(key, known, captured_urls)
        observation = observations.get(key)
        if observation is None or observation.identity not in {"exact", "plausible"}:
            continue
        failed = (known_failed and key == known) or posting_host(candidate) == failed_host
        accepted.setdefault(key, RecoveryCandidate(candidate, observation, already_failed=failed))
    return list(accepted.values())


def _validate_supporting_urls(
    response: PostingRecoveryResponse, known_url: str, captured_urls: set[str]
) -> None:
    for url in response.supporting_urls:
        key = _url_key(str(url))
        if key != known_url and key not in captured_urls:
            raise ValidationError("Posting recovery returned an uncited supporting URL.")


def _observations_by_url(
    response: PostingRecoveryResponse,
    known_url: str,
    captured_urls: set[str],
) -> dict[str, PostingRecoveryObservation]:
    observations: dict[str, PostingRecoveryObservation] = {}
    for observation in response.observations:
        key = _url_key(str(observation.url))
        if key != known_url and key not in captured_urls:
            raise ValidationError("Posting recovery returned an uncited observation URL.")
        if key in observations:
            raise ValidationError("Posting recovery returned conflicting duplicate observations.")
        observations[key] = observation
    return observations


def _require_cited_candidate(key: str, known_url: str, captured_urls: set[str]) -> None:
    if key != known_url and key not in captured_urls:
        raise ValidationError("Posting recovery returned an uncited candidate URL.")


def _deterministic_outcome(result: ResolvedPostingContent) -> PostingRecoveryOutcome:
    if result.content_status is PostingContentAccessStatus.POLICY_REFUSED:
        return PostingRecoveryOutcome.POLICY_REFUSED
    if result.posting_status is PostingStatus.CLOSED:
        return PostingRecoveryOutcome.CLOSED
    if (
        result.content_status is PostingContentAccessStatus.READABLE
        and result.identity_status is PostingIdentityStatus.VERIFIED
        and result.posting_status is PostingStatus.ACTIVE
        and result.content
        and result.final_url
        and result.extraction_method is not None
    ):
        return PostingRecoveryOutcome.RECOVERED
    if result.identity_status in {
        PostingIdentityStatus.AMBIGUOUS,
        PostingIdentityStatus.CONTRADICTED,
    }:
        return PostingRecoveryOutcome.AMBIGUOUS
    return PostingRecoveryOutcome.SOURCE_FOUND_UNREADABLE


def _outcome_detail(outcome: PostingRecoveryOutcome) -> str:
    return {
        PostingRecoveryOutcome.RECOVERED: (
            "Deterministic publisher read verified an active posting body."
        ),
        PostingRecoveryOutcome.CLOSED: "Deterministic publisher read found a closure signal.",
        PostingRecoveryOutcome.AMBIGUOUS: (
            "Deterministic publisher read could not verify one identity."
        ),
        PostingRecoveryOutcome.SOURCE_FOUND_UNREADABLE: (
            "The cited source did not yield usable content."
        ),
        PostingRecoveryOutcome.POLICY_REFUSED: "Published fetch policy refused the cited source.",
    }[outcome]


def _schema_refused(error: ProviderError) -> bool:
    """Return whether the adapter refused a billed answer for failing the response schema."""
    parsed = error.__cause__
    return (
        isinstance(error, UnusableResponseError)
        and parsed is not None
        and isinstance(parsed.__cause__, PydanticValidationError)
    )


def _failed(
    identity_id: str, outcome: PostingRecoveryOutcome, error: Exception
) -> AssistedPostingRecoveryItem:
    return AssistedPostingRecoveryItem(
        identity_id,
        outcome,
        detail=" ".join(str(error).split())[:500],
    )


def _url_key(value: str) -> str:
    key = exact_url_key(value)
    if not key:
        raise ValidationError("Posting recovery URL must be absolute HTTP or HTTPS.")
    return key


def _combined_digest(values: Any) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(str(value).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _json_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def _aware_now(now: datetime | None) -> datetime:
    value = now or datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("Posting recovery time must be timezone-aware.")
    return value
