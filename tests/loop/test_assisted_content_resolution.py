from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar, cast

import httpx2
import pytest
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select

from clearscout.application.config import ModelChoice, ProviderName, Settings, TierModels
from clearscout.application.discovery.assisted_content_resolution import (
    AssistedContentResolutionService,
    AssistedPostingRecoveryItem,
    PostingBodyCompleteness,
    PostingRecoveryObservation,
    PostingRecoveryOutcome,
    PostingRecoveryResponse,
    PostingSourceAuthority,
    request_plan,
)
from clearscout.application.discovery.content_resolution import (
    AddressHolder,
    PostingContentResolutionService,
    PostingResolutionPreflight,
    PostingResolutionTarget,
    ResolvedPostingContent,
    StoredEvidence,
    pending_discovery_readiness,
)
from clearscout.application.discovery.hints import PendingDiscoveryHintsService
from clearscout.application.discovery.posting_identity import BoardHolder, BoardRecord
from clearscout.application.failures import ConflictError, ValidationError
from clearscout.application.prompts import render
from clearscout.application.tasks import TaskQueueService
from clearscout.application.workspaces import WorkspaceService
from clearscout.domain import (
    PendingDiscoveryReadiness,
    PostingContentAccessStatus,
    PostingContentExtractionMethod,
    PostingIdentityStatus,
    PostingStatus,
    utc_now,
)
from clearscout.domain.identity import url_sha256
from clearscout.domain.records import PostingReadIntent
from clearscout.domain.staged_facts import ListingIdentityTrust, StagedListingFacts
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    AuditEvent,
    Employer,
    JobPosting,
    PendingDiscovery,
    PendingPostingContent,
    PostingUrl,
    Source,
)
from clearscout.infrastructure.providers.base import CallRecord, Provider, Search, SearchSource
from clearscout.infrastructure.providers.cache import ResponseCache
from clearscout.infrastructure.providers.client import Client
from clearscout.infrastructure.providers.plan import RequestPlan
from clearscout.shared.costs import Ledger, Usage
from clearscout.shared.costs.prices import PriceOverride, table
from tests.support.artifacts import runtime_storage
from tests.support.fake_provider import FakeProviderAPI
from tests.support.provider_clients import provider_for_http
from tests.support.scenarios import (
    dismiss_pending_discovery,
    employer_board,
    scan_workspace,
    seed_unread_row,
    tracked_board_posting,
)

ModelT = TypeVar("ModelT", bound=BaseModel)
NOW = datetime(2026, 8, 28, 12, tzinfo=UTC)
PUBLISHER_URL = "https://careers.example.test/jobs/operations-lead"
OTHER_URL = "https://jobs.example.test/openings/operations-lead"
# The listing address a scan staged and could not read, on a host serving many employers.
KNOWN_URL = "https://listings.example.test/view/operations-lead-4821"
SECOND_KNOWN_URL = "https://listings.example.test/view/operations-lead-5930"
ATS_URL = "https://boards.greenhouse.io/examplesystems/jobs/4821"


class ScriptedResolver:
    def __init__(self, results: list[ResolvedPostingContent]) -> None:
        self.results = results
        self.calls: list[tuple[str, str, str]] = []
        self.intents: list[PostingReadIntent] = []

    def resolve(
        self,
        url: str,
        *,
        employer: str,
        title: str,
        intent: PostingReadIntent = PostingReadIntent.CRAWL,
        board_url: str = "",
    ) -> ResolvedPostingContent:
        self.calls.append((url, employer, title))
        self.intents.append(intent)
        return self.results.pop(0)


class ScriptedContent:
    def __init__(
        self,
        targets: tuple[PostingResolutionTarget, ...],
        resolver: ScriptedResolver,
        snapshot: str = "a" * 64,
    ) -> None:
        self.targets = targets
        self.resolver = resolver
        self.snapshot = snapshot
        self.current = self
        self.ready_ids: set[str] = set()
        self.stored_results: list[tuple[str, ResolvedPostingContent]] = []
        self.stored_failures: list[tuple[str, dict[str, object]]] = []

    def read(self, _workspace_id: str, pending_id: str) -> object | None:
        return object() if pending_id in self.ready_ids else None

    def preflight(self, _workspace_id: str, ids: list[str]) -> PostingResolutionPreflight:
        by_id = {item.id: item for item in self.targets}
        return PostingResolutionPreflight(tuple(by_id[item_id] for item_id in ids), self.snapshot)

    def store_assisted_result(
        self,
        _workspace_id: str,
        target: PostingResolutionTarget,
        result: ResolvedPostingContent,
        _context: object,
    ) -> StoredEvidence:
        self.stored_results.append((target.id, result))
        return StoredEvidence(f"evidence-{target.id}")

    def store_assisted_failure(
        self,
        _workspace_id: str,
        target: PostingResolutionTarget,
        **kwargs: object,
    ) -> str:
        self.stored_failures.append((target.id, kwargs))
        return f"attempt-{target.id}"


class ScriptedSearchProvider:
    name = ProviderName.OPENAI

    def __init__(self, answers: list[tuple[dict[str, Any], CallRecord]]) -> None:
        self.answers = answers
        self.calls: list[RequestPlan] = []

    def complete(
        self, _plan: RequestPlan, _schema: type[ModelT], _choice: ModelChoice
    ) -> tuple[ModelT, CallRecord]:
        raise AssertionError("Assisted recovery must use hosted search.")

    def complete_with_web_search(
        self, plan: RequestPlan, schema: type[ModelT], _choice: ModelChoice
    ) -> tuple[ModelT, CallRecord]:
        self.calls.append(plan)
        payload, record = self.answers.pop(0)
        return schema.model_validate(payload), record

    def check_credentials(self) -> None:
        return None

    def available_models(self) -> frozenset[str] | None:
        return None


class CancelAfterOne:
    task_id = "task-assisted"

    def __init__(self) -> None:
        self.checks = 0

    def raise_if_cancelled(self) -> None:
        self.checks += 1
        if self.checks == 2:
            raise RuntimeError("cancelled")

    def progress(self, _stage: str, _detail: dict[str, Any] | None = None) -> None:
        return None


def _target(
    identity_id: str = "pending-1",
    url: str = KNOWN_URL,
    status: PostingContentAccessStatus = PostingContentAccessStatus.RENDER_REQUIRED,
) -> PostingResolutionTarget:
    return PostingResolutionTarget(
        identity_id,
        "Example Systems",
        "Operations Lead",
        url,
        status,
        "f" * 64,
        None,
        "Example Systems",
        "Operations Lead",
    )


def _observation(
    url: str = PUBLISHER_URL,
    *,
    authority: PostingSourceAuthority = PostingSourceAuthority.PUBLISHER_BODY,
    identity: str = "exact",
    completeness: PostingBodyCompleteness = PostingBodyCompleteness.COMPLETE,
) -> dict[str, str]:
    return {
        "url": url,
        "authority": authority.value,
        "completeness": completeness.value,
        "identity": identity,
        "posting_status": "active",
        "note": "The publisher page names the supplied title and employer.",
    }


def _response(
    identity_id: str = "pending-1",
    *,
    candidates: list[str] | None = None,
    supporting: list[str] | None = None,
    observations: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "identity_id": identity_id,
        "candidate_urls": candidates if candidates is not None else [PUBLISHER_URL],
        "supporting_urls": supporting if supporting is not None else [PUBLISHER_URL],
        "observations": observations if observations is not None else [_observation()],
    }


def _record(*sources: str, search_count: int = 1) -> CallRecord:
    searches = (
        Search(
            queries=("site:careers.example.test Operations Lead",),
            sources=tuple(SearchSource(url=url, title="Operations Lead") for url in sources),
        ),
    )
    return CallRecord(
        usage=Usage(input_tokens=100, output_tokens=50),
        model="gpt-6.1-sol",
        response_id="response-assisted",
        searches=searches if sources else (),
        search_count=search_count,
        tool_version="scripted_search",
    )


def _readable(
    url: str = PUBLISHER_URL,
    *,
    identity: PostingIdentityStatus = PostingIdentityStatus.VERIFIED,
) -> ResolvedPostingContent:
    return ResolvedPostingContent(
        requested_url=url,
        final_url=url,
        content_status=PostingContentAccessStatus.READABLE,
        identity_status=identity,
        posting_status=PostingStatus.ACTIVE,
        extraction_method=PostingContentExtractionMethod.STATIC_STRUCTURED,
        fetched_at=NOW,
        content=b"Operations Lead\nResponsibilities\nBuild reliable systems.",
        http_status=200,
        publisher_markup_present=True,
    )


def _service(
    targets: tuple[PostingResolutionTarget, ...],
    resolver: ScriptedResolver,
    database: Database | None = None,
) -> tuple[AssistedContentResolutionService, ScriptedContent]:
    """Build the service over scripted content; a preflight also needs a database to read."""
    content = ScriptedContent(targets, resolver)
    service = AssistedContentResolutionService(
        cast(Database, database),
        cast(PostingContentResolutionService, content),
    )
    return service, content


def _client(
    provider: Provider,
) -> tuple[Client, Ledger]:
    settings = Settings(provider=ProviderName.OPENAI)
    ledger = Ledger(prices=table(settings.prices))
    return (
        Client(
            provider=provider,
            settings=settings,
            cache=ResponseCache(enabled=False),
            ledger=ledger,
        ),
        ledger,
    )


class SearchingResponsesAPI(FakeProviderAPI):
    """Answer like the Responses API, with the hosted tool's own output items first."""

    def __init__(self, answer: dict[str, Any], tool_calls: list[dict[str, Any]]) -> None:
        super().__init__("openai", {"PostingRecovery": lambda _schema: answer})
        self.tool_calls = tool_calls
        self.incomplete_reason = ""

    def _envelope(self, content: str, service_tier: object = None) -> dict[str, Any]:
        envelope = super()._envelope(content, service_tier)
        envelope["output"] = [*self.tool_calls, *envelope["output"]]
        if self.incomplete_reason:
            envelope["status"] = "incomplete"
            envelope["incomplete_details"] = {"reason": self.incomplete_reason}
        return envelope


def _searched(
    call_id: str, *sources: str, status: str = "completed", query: str | None = None
) -> dict[str, Any]:
    action: dict[str, Any] = {"type": "search"}
    if query is not None:
        action["query"] = query
    if sources:
        action["sources"] = [{"type": "url", "url": url} for url in sources]
    return {"type": "web_search_call", "id": call_id, "status": status, "action": action}


def _opened(call_id: str, url: str | None, *, status: str = "completed") -> dict[str, Any]:
    return {
        "type": "web_search_call",
        "id": call_id,
        "status": status,
        "action": {"type": "open_page", "url": url},
    }


def _responses_api(
    answer: dict[str, Any], *tool_calls: dict[str, Any]
) -> tuple[Provider, SearchingResponsesAPI]:
    """Return the shipped OpenAI adapter answering over HTTP with these tool calls."""
    api = SearchingResponsesAPI(answer, list(tool_calls))
    http = httpx2.Client(transport=httpx2.MockTransport(api))
    return provider_for_http(ProviderName.OPENAI, http, max_retries=0), api


def test_response_schema_has_no_body_field_and_forbids_one() -> None:
    schema = PostingRecoveryResponse.model_json_schema()

    assert "body" not in schema["properties"]
    assert schema["additionalProperties"] is False
    with pytest.raises(PydanticValidationError):
        PostingRecoveryResponse.model_validate({**_response(), "body": "invented posting"})


def test_anthropic_models_price_hosted_search_at_ten_dollars_per_thousand() -> None:
    prices = table()

    assert prices["claude-sonnet-5"].search_per_thousand_calls == 10.0
    assert prices["claude-opus-5"].search_per_thousand_calls == 10.0
    assert prices["claude-haiku-4-5-20251001"].search_per_thousand_calls == 10.0


def test_preflight_binds_ordered_snapshot_and_all_cost_inputs(
    durable_database: Database,
) -> None:
    targets = (_target("pending-2", SECOND_KNOWN_URL), _target())
    service, _content = _service(targets, ScriptedResolver([]), durable_database)

    approval = service.preflight(
        "workspace-assisted",
        ["pending-2", "pending-1"],
        Settings(provider=ProviderName.OPENAI),
        now=NOW,
    )

    assert approval.pending_discovery_ids == ("pending-2", "pending-1")
    assert approval.snapshot_sha256 == "a" * 64
    assert approval.provider == "openai"
    assert approval.model == "gpt-6.1-sol"
    assert approval.estimate_cents > 0
    assert approval.expires_at == NOW + timedelta(minutes=15)
    assert all(
        len(value.removeprefix("sha256:")) == 64
        for value in (
            approval.system_prompt_sha256,
            approval.user_prompt_sha256,
            approval.schema_sha256,
            approval.settings_sha256,
            approval.price_sha256,
        )
    )


def test_preflight_refuses_a_posting_that_already_has_usable_content() -> None:
    target = _target()
    service, content = _service((target,), ScriptedResolver([]))
    content.ready_ids.add(target.id)

    with pytest.raises(ValidationError, match="still need content recovery"):
        service.preflight(
            "workspace-assisted",
            [target.id],
            Settings(provider=ProviderName.OPENAI),
            now=NOW,
        )


def test_preflight_refuses_a_model_without_a_complete_search_price(
    durable_database: Database,
) -> None:
    target = _target()
    service, _content = _service((target,), ScriptedResolver([]), durable_database)
    settings = Settings(provider=ProviderName.OPENAI)
    settings.models[ProviderName.OPENAI] = TierModels(
        extraction=ModelChoice(model="unpriced-model"),
        drafting=settings.models[ProviderName.OPENAI].drafting,
    )

    with pytest.raises(ValidationError, match="No complete token and hosted-search price"):
        service.preflight("workspace-assisted", [target.id], settings, now=NOW)


def test_preflight_prices_a_dated_model_snapshot_from_the_model_it_names(
    durable_database: Database,
) -> None:
    """An approval bound to a name with no price would be approved in cents and billed blind."""
    target = _target()
    service, _content = _service((target,), ScriptedResolver([]), durable_database)
    pinned = Settings(provider=ProviderName.OPENAI)
    base = pinned.models[ProviderName.OPENAI].extraction.model
    dated = Settings(provider=ProviderName.OPENAI)
    dated.models[ProviderName.OPENAI] = TierModels(
        extraction=ModelChoice(model=f"{base}-2026-04-01"),
        drafting=dated.models[ProviderName.OPENAI].drafting,
    )

    approval = service.preflight("workspace-assisted", [target.id], dated, now=NOW)
    pinned_approval = service.preflight("workspace-assisted", [target.id], pinned, now=NOW)

    assert approval.model == f"{base}-2026-04-01"
    assert approval.estimate_cents == pinned_approval.estimate_cents
    assert approval.price_sha256 == pinned_approval.price_sha256


@pytest.mark.parametrize("change", ["snapshot", "expired"])
def test_recovery_refuses_drift_and_expiry(durable_database: Database, change: str) -> None:
    target = _target()
    service, content = _service((target,), ScriptedResolver([]), durable_database)
    settings = Settings(provider=ProviderName.OPENAI)
    approval = service.preflight("workspace-assisted", [target.id], settings, now=NOW)
    current = NOW
    if change == "snapshot":
        content.snapshot = "b" * 64
    else:
        current = approval.expires_at

    error = ConflictError if change == "snapshot" else ValidationError
    with pytest.raises(error):
        service.recover(
            "workspace-assisted",
            approval,
            settings,
            task_id="task-assisted",
            now=current,
        )


@pytest.mark.parametrize(
    ("payload", "record"),
    [
        (_response(candidates=[OTHER_URL], supporting=[PUBLISHER_URL]), _record(PUBLISHER_URL)),
        (_response(), _record(search_count=0)),
    ],
)
def test_an_uncited_candidate_or_an_unsearched_answer_is_a_refused_answer(
    payload: dict[str, Any], record: CallRecord
) -> None:
    """The provider answered; the local checks refused what it said."""
    resolver = ScriptedResolver([])
    service, _content = _service((_target(),), resolver)
    provider = ScriptedSearchProvider([(payload, record)])
    client, ledger = _client(provider)

    result = service._recover_targets("workspace-assisted", (_target(),), client, None)

    assert result[0].outcome is PostingRecoveryOutcome.RESPONSE_REFUSED
    assert result[0].resolved is None
    assert resolver.calls == []
    assert [entry.validated for entry in ledger.entries] == [False]


def test_two_observations_of_one_url_refuse_the_whole_answer() -> None:
    resolver = ScriptedResolver([])
    service, _content = _service((_target(),), resolver)
    payload = _response(observations=[_observation(), _observation()])
    provider = ScriptedSearchProvider([(payload, _record(PUBLISHER_URL))])
    client, ledger = _client(provider)

    [item] = service._recover_targets("workspace-assisted", (_target(),), client, None)

    assert item.outcome is PostingRecoveryOutcome.RESPONSE_REFUSED
    assert [entry.validated for entry in ledger.entries] == [False]


def test_an_answer_for_another_identity_is_a_billed_answer_the_schema_refused() -> None:
    """The response schema admits only this identity, so the adapter refuses any other."""
    resolver = ScriptedResolver([])
    service, _content = _service((_target(),), resolver)
    provider, _api = _responses_api(
        _response(identity_id="pending-other"),
        _searched("ws_1", PUBLISHER_URL, query="Example Systems Operations Lead"),
    )
    client, ledger = _client(provider)

    [item] = service._recover_targets("workspace-assisted", (_target(),), client, None)

    assert item.outcome is PostingRecoveryOutcome.RESPONSE_REFUSED
    assert resolver.calls == []
    assert [entry.validated for entry in ledger.entries] == [False]


def test_a_response_cut_off_at_the_output_limit_is_a_provider_failure() -> None:
    service, _content = _service((_target(),), ScriptedResolver([]))
    provider, api = _responses_api(
        _response(), _searched("ws_1", PUBLISHER_URL, query="Example Systems Operations Lead")
    )
    api.incomplete_reason = "max_output_tokens"
    client, ledger = _client(provider)

    [item] = service._recover_targets("workspace-assisted", (_target(),), client, None)

    assert item.outcome is PostingRecoveryOutcome.PROVIDER_FAILED
    assert [entry.validated for entry in ledger.entries] == [False]


def test_a_candidate_with_no_observation_of_its_own_is_skipped_beside_a_good_one() -> None:
    resolver = ScriptedResolver([_readable()])
    service, _content = _service((_target(),), resolver)
    payload = _response(candidates=[OTHER_URL, PUBLISHER_URL])
    provider = ScriptedSearchProvider([(payload, _record(PUBLISHER_URL, OTHER_URL))])
    client, _ledger = _client(provider)

    [item] = service._recover_targets("workspace-assisted", (_target(),), client, None)

    assert item.outcome is PostingRecoveryOutcome.RECOVERED
    assert resolver.calls == [(PUBLISHER_URL, "Example Systems", "Operations Lead")]


def test_an_exact_aggregator_candidate_does_not_make_a_first_party_one_ambiguous() -> None:
    """Only a candidate the application would read can compete for the read."""
    resolver = ScriptedResolver([_readable()])
    service, _content = _service((_target(),), resolver)
    payload = _response(
        candidates=[OTHER_URL, PUBLISHER_URL],
        observations=[
            _observation(OTHER_URL, authority=PostingSourceAuthority.AGGREGATOR),
            _observation(),
        ],
    )
    provider = ScriptedSearchProvider([(payload, _record(PUBLISHER_URL, OTHER_URL))])
    client, _ledger = _client(provider)

    [item] = service._recover_targets("workspace-assisted", (_target(),), client, None)

    assert item.outcome is PostingRecoveryOutcome.RECOVERED
    assert item.candidate_url == PUBLISHER_URL


def test_multiple_plausible_exact_candidates_remain_ambiguous() -> None:
    resolver = ScriptedResolver([])
    service, _content = _service((_target(),), resolver)
    payload = _response(
        candidates=[PUBLISHER_URL, OTHER_URL],
        supporting=[PUBLISHER_URL, OTHER_URL],
        observations=[_observation(), _observation(OTHER_URL)],
    )
    provider = ScriptedSearchProvider([(payload, _record(PUBLISHER_URL, OTHER_URL))])
    client, _ledger = _client(provider)

    result = service._recover_targets("workspace-assisted", (_target(),), client, None)

    assert result[0].outcome is PostingRecoveryOutcome.AMBIGUOUS
    assert "multiple plausible" in result[0].detail
    assert result[0].resolved is None
    assert resolver.calls == []


# The two identities the candidate filter refuses, against the two it accepts
# below. Together they pin the whole reported-identity vocabulary.
@pytest.mark.parametrize("identity", ["contradicted", "unknown"])
def test_a_cited_source_that_does_not_claim_this_posting_is_not_a_candidate_for_it(
    identity: str,
) -> None:
    """A page the search cited but which does not claim this job must not be read as it."""
    resolver = ScriptedResolver([])
    service, _content = _service((_target(),), resolver)
    payload = _response(observations=[_observation(identity=identity)])
    provider = ScriptedSearchProvider([(payload, _record(PUBLISHER_URL))])
    client, _ledger = _client(provider)

    result = service._recover_targets("workspace-assisted", (_target(),), client, None)

    assert result[0].outcome is PostingRecoveryOutcome.NOT_FOUND
    assert result[0].resolved is None
    assert resolver.calls == []


# Every authority inside READABLE_SOURCE_AUTHORITIES, so narrowing that set
# stops a publisher-controlled body from being read and fails here.
@pytest.mark.parametrize(
    ("identity", "authority"),
    [
        pytest.param("exact", PostingSourceAuthority.ATS, id="exact-ats"),
        pytest.param(
            "plausible", PostingSourceAuthority.PUBLISHER_BODY, id="plausible-publisher-body"
        ),
        pytest.param("plausible", PostingSourceAuthority.ATS, id="plausible-ats"),
    ],
)
def test_a_cited_source_claiming_this_posting_is_read(
    identity: str, authority: PostingSourceAuthority
) -> None:
    resolver = ScriptedResolver([_readable()])
    service, _content = _service((_target(),), resolver)
    payload = _response(observations=[_observation(authority=authority, identity=identity)])
    provider = ScriptedSearchProvider([(payload, _record(PUBLISHER_URL))])
    client, _ledger = _client(provider)

    result = service._recover_targets("workspace-assisted", (_target(),), client, None)

    assert result[0].outcome is PostingRecoveryOutcome.RECOVERED
    assert result[0].resolved is not None


# Every authority outside READABLE_SOURCE_AUTHORITIES, so widening that set to
# admit a mirror or a listing page fails here rather than silently reading one.
@pytest.mark.parametrize(
    "authority",
    [
        pytest.param(PostingSourceAuthority.PUBLISHER_INDEX, id="publisher-index"),
        pytest.param(PostingSourceAuthority.AGGREGATOR, id="aggregator"),
        pytest.param(PostingSourceAuthority.SEARCH_RESULT, id="search-result"),
    ],
)
def test_an_exact_source_that_is_not_publisher_controlled_is_never_used_as_a_body(
    authority: PostingSourceAuthority,
) -> None:
    resolver = ScriptedResolver([])
    service, _content = _service((_target(),), resolver)
    payload = _response(observations=[_observation(authority=authority)])
    provider = ScriptedSearchProvider([(payload, _record(PUBLISHER_URL))])
    client, _ledger = _client(provider)

    result = service._recover_targets("workspace-assisted", (_target(),), client, None)

    assert result[0].outcome is PostingRecoveryOutcome.SOURCE_FOUND_UNREADABLE
    assert "does not expose a publisher-controlled body" in result[0].detail
    assert result[0].candidate_url == PUBLISHER_URL
    assert result[0].resolved is None
    assert resolver.calls == []


def test_failure_isolation_keeps_later_identity_recoverable() -> None:
    targets = (_target(), _target("pending-2", SECOND_KNOWN_URL))
    resolver = ScriptedResolver([_readable(OTHER_URL)])
    service, _content = _service(targets, resolver)
    provider = ScriptedSearchProvider(
        [
            (_response(candidates=[OTHER_URL]), _record(PUBLISHER_URL)),
            (
                _response(
                    "pending-2",
                    candidates=[OTHER_URL],
                    supporting=[OTHER_URL],
                    observations=[_observation(OTHER_URL)],
                ),
                _record(OTHER_URL),
            ),
        ]
    )
    client, _ledger = _client(provider)

    results = service._recover_targets("workspace-assisted", targets, client, None)

    assert [item.outcome for item in results] == [
        PostingRecoveryOutcome.RESPONSE_REFUSED,
        PostingRecoveryOutcome.RECOVERED,
    ]
    assert results[1].resolved is not None
    assert len(provider.calls) == 2


def test_cancellation_stops_before_the_next_identity_call() -> None:
    targets = (_target(), _target("pending-2", SECOND_KNOWN_URL))
    resolver = ScriptedResolver([_readable()])
    service, _content = _service(targets, resolver)
    provider = ScriptedSearchProvider(
        [
            (_response(), _record(PUBLISHER_URL)),
            (_response("pending-2"), _record(OTHER_URL)),
        ]
    )
    client, _ledger = _client(provider)

    with pytest.raises(RuntimeError, match="cancelled"):
        service._recover_targets("workspace-assisted", targets, client, CancelAfterOne())

    assert len(provider.calls) == 1


def test_model_completeness_never_overrides_deterministic_identity() -> None:
    resolver = ScriptedResolver([_readable(identity=PostingIdentityStatus.CONTRADICTED)])
    service, _content = _service((_target(),), resolver)
    provider = ScriptedSearchProvider([(_response(), _record(PUBLISHER_URL))])
    client, _ledger = _client(provider)

    result = service._recover_targets("workspace-assisted", (_target(),), client, None)

    assert result[0].observations[0] == PostingRecoveryObservation.model_validate(_observation())
    assert result[0].outcome is PostingRecoveryOutcome.AMBIGUOUS


def test_the_completeness_vocabulary_offered_to_the_model_is_the_one_it_may_answer_with() -> None:
    """The closed set is the contract with the model, so a member cannot quietly leave it."""
    assert set(PostingBodyCompleteness) == {
        PostingBodyCompleteness.COMPLETE,
        PostingBodyCompleteness.PARTIAL,
        PostingBodyCompleteness.IDENTITY_ONLY,
        PostingBodyCompleteness.NONE,
        PostingBodyCompleteness.UNKNOWN,
    }


@pytest.mark.parametrize("completeness", list(PostingBodyCompleteness))
def test_every_completeness_the_model_may_report_reaches_the_observation(
    completeness: PostingBodyCompleteness,
) -> None:
    """The model picks from this closed set, so every member must survive the read."""
    resolver = ScriptedResolver([_readable()])
    service, _content = _service((_target(),), resolver)
    observation = _observation(completeness=completeness)
    provider = ScriptedSearchProvider(
        [(_response(observations=[observation]), _record(PUBLISHER_URL))]
    )
    client, _ledger = _client(provider)

    result = service._recover_targets("workspace-assisted", (_target(),), client, None)

    assert result[0].observations[0].completeness is completeness


def test_only_deterministically_verified_active_content_becomes_usable() -> None:
    resolver = ScriptedResolver([_readable()])
    service, _content = _service((_target(),), resolver)
    provider = ScriptedSearchProvider([(_response(), _record(PUBLISHER_URL))])
    client, _ledger = _client(provider)

    result = service._recover_targets("workspace-assisted", (_target(),), client, None)

    assert result[0].outcome is PostingRecoveryOutcome.RECOVERED
    assert result[0].resolved is not None
    assert result[0].resolved.content == (
        b"Operations Lead\nResponsibilities\nBuild reliable systems."
    )
    assert resolver.intents == [PostingReadIntent.CRAWL]


def test_resolver_content_for_another_url_never_becomes_usable() -> None:
    resolver = ScriptedResolver([_readable(OTHER_URL)])
    service, _content = _service((_target(),), resolver)
    provider = ScriptedSearchProvider([(_response(), _record(PUBLISHER_URL))])
    client, _ledger = _client(provider)

    result = service._recover_targets("workspace-assisted", (_target(),), client, None)

    assert result[0].outcome is PostingRecoveryOutcome.SOURCE_FOUND_UNREADABLE
    assert result[0].resolved is None


def _unreadable(
    status: PostingContentAccessStatus, *, posting_status: PostingStatus | None = None
) -> ResolvedPostingContent:
    return ResolvedPostingContent(
        requested_url=PUBLISHER_URL,
        final_url=PUBLISHER_URL,
        content_status=status,
        identity_status=PostingIdentityStatus.NOT_YET_VERIFIED,
        posting_status=posting_status,
        extraction_method=None,
        fetched_at=NOW,
        http_status=None,
    )


@pytest.mark.parametrize(
    ("reread", "outcome"),
    [
        (
            _unreadable(
                PostingContentAccessStatus.FETCH_FAILED, posting_status=PostingStatus.CLOSED
            ),
            PostingRecoveryOutcome.CLOSED,
        ),
        (
            _unreadable(PostingContentAccessStatus.POLICY_REFUSED),
            PostingRecoveryOutcome.POLICY_REFUSED,
        ),
        (
            _unreadable(PostingContentAccessStatus.SESSION_REQUIRED),
            PostingRecoveryOutcome.SOURCE_FOUND_UNREADABLE,
        ),
    ],
)
def test_bodiless_deterministic_reread_is_stored_as_itself(
    reread: ResolvedPostingContent, outcome: PostingRecoveryOutcome
) -> None:
    service, content = _service((_target(),), ScriptedResolver([reread]))
    provider = ScriptedSearchProvider([(_response(), _record(PUBLISHER_URL))])
    client, _ledger = _client(provider)

    result = service._recover_targets("workspace-assisted", (_target(),), client, None)

    assert result[0].outcome is outcome
    assert result[0].content_evidence_id == "evidence-pending-1"
    assert content.stored_failures == []
    [(stored_id, stored)] = content.stored_results
    assert stored_id == "pending-1"
    assert stored.content_status is reread.content_status
    assert stored.posting_status is reread.posting_status
    assert stored.identity_evidence == {
        "assisted_observations": [_observation()],
    }


@pytest.mark.parametrize(
    ("trust", "corrected_title", "expected_employer", "display_title"),
    [
        (ListingIdentityTrust.UNVERIFIED_HINT, None, "", "Operations Lead"),
        (
            ListingIdentityTrust.PUBLISHER,
            "Operations Lead II",
            "Example Systems",
            "Operations Lead II",
        ),
    ],
)
def test_assisted_reread_never_checks_a_hint_title_as_publisher_identity(
    durable_database: Database,
    tmp_path: Path,
    trust: ListingIdentityTrust,
    corrected_title: str | None,
    expected_employer: str,
    display_title: str,
) -> None:
    workspace = WorkspaceService(
        durable_database, runtime_storage(durable_database, tmp_path)
    ).create(slug="assisted-identity", name="Assisted identity")
    with durable_database.session() as session:
        pending = PendingDiscovery(
            workspace_id=workspace.id,
            fingerprint="c" * 64,
            employer="Example Systems",
            title="Operations Lead",
            discovery_url=PUBLISHER_URL,
            reason="Search hint",
            first_seen_on=date(2026, 8, 28),
            staged_facts=StagedListingFacts(identity_trust=trust).as_json(),
        )
        session.add(pending)
        session.flush()
        pending_id = pending.id
    if corrected_title is not None:
        PendingDiscoveryHintsService(durable_database).correct_title(
            workspace.id, pending_id, corrected_title
        )
    resolver = ScriptedResolver([_unreadable(PostingContentAccessStatus.SESSION_REQUIRED)])
    content = PostingContentResolutionService(
        durable_database, resolver, storage=runtime_storage(durable_database, tmp_path)
    )
    service = AssistedContentResolutionService(durable_database, content)
    provider = ScriptedSearchProvider([(_response(pending_id), _record(PUBLISHER_URL))])
    client, _ledger = _client(provider)
    targets = content.preflight(workspace.id, [pending_id]).targets

    service._recover_targets(workspace.id, targets, client, None)

    assert resolver.calls == [(PUBLISHER_URL, expected_employer, "")]
    [plan] = provider.calls
    assert f'"title":"{display_title}"' in plan.task.text


class RecordingProgress:
    task_id = "task-assisted"

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any] | None]] = []

    def raise_if_cancelled(self) -> None:
        return None

    def progress(self, stage: str, detail: dict[str, Any] | None = None) -> None:
        self.events.append((stage, detail))


def test_failed_attempt_beside_a_retained_body_reports_its_own_evidence(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace = WorkspaceService(
        durable_database, runtime_storage(durable_database, tmp_path)
    ).create(slug="assisted-retained", name="Assisted retained")
    with durable_database.session() as session:
        pending = PendingDiscovery(
            workspace_id=workspace.id,
            fingerprint="d" * 64,
            employer="Example Systems",
            title="Operations Lead",
            discovery_url=PUBLISHER_URL,
            reason="Publisher-listed role",
            first_seen_on=date(2026, 8, 28),
        )
        session.add(pending)
        session.flush()
        pending_id = pending.id
    content = PostingContentResolutionService(
        durable_database,
        ScriptedResolver([_readable(identity=PostingIdentityStatus.CONTRADICTED)]),
        storage=runtime_storage(durable_database, tmp_path),
    )
    content.resolve_batch(
        workspace.id,
        [pending_id],
        content.preflight(workspace.id, [pending_id]).snapshot_sha256,
        origin="discovery.refresh_batch",
    )
    targets = content.preflight(workspace.id, [pending_id]).targets
    assert targets[0].current_content_id is not None
    ambiguous = _response(
        pending_id,
        candidates=[PUBLISHER_URL, OTHER_URL],
        supporting=[PUBLISHER_URL, OTHER_URL],
        observations=[_observation(), _observation(OTHER_URL)],
    )
    provider = ScriptedSearchProvider([(ambiguous, _record(PUBLISHER_URL, OTHER_URL))])
    client, _ledger = _client(provider)
    progress = RecordingProgress()

    [item] = AssistedContentResolutionService(durable_database, content)._recover_targets(
        workspace.id, targets, client, progress
    )

    assert item.outcome is PostingRecoveryOutcome.AMBIGUOUS
    assert item.content_evidence_id not in {None, targets[0].current_content_id}
    [recorded] = [
        detail for stage, detail in progress.events if stage == "posting_content_recovery_recorded"
    ]
    assert recorded is not None and recorded["content_evidence_id"] == item.content_evidence_id
    after = content.preflight(workspace.id, [pending_id]).targets
    assert after[0].current_content_id == targets[0].current_content_id


def _staged_row(database: Database, workspace_id: str, url: str) -> str:
    with database.session() as session:
        pending = PendingDiscovery(
            workspace_id=workspace_id,
            fingerprint=url_sha256(url),
            employer="Example Systems",
            title="Operations Lead",
            discovery_url=url,
            reason="Listing a scan could not read",
            first_seen_on=date(2026, 8, 28),
        )
        session.add(pending)
        session.flush()
        return pending.id


def _tracked_job(database: Database, workspace_id: str, url: str) -> str:
    with database.session() as session:
        employer = Employer(
            workspace_id=workspace_id, name="Example Systems", normalized_name="example systems"
        )
        session.add(employer)
        session.flush()
        job = JobPosting(
            workspace_id=workspace_id,
            employer_id=employer.id,
            title="Operations Lead",
            normalized_title="operations lead",
            first_seen_at=utc_now(),
        )
        session.add(job)
        session.flush()
        session.add(
            PostingUrl(
                job_posting_id=job.id,
                url_type="application",
                url=url,
                normalized_url=url,
                is_primary=True,
            )
        )
        return job.id


def _recovered_at_publisher(
    database: Database, tmp_path: Path, workspace_id: str, pending_id: str
) -> AssistedPostingRecoveryItem:
    content = PostingContentResolutionService(
        database, ScriptedResolver([_readable()]), storage=runtime_storage(database, tmp_path)
    )
    provider = ScriptedSearchProvider([(_response(pending_id), _record(PUBLISHER_URL))])
    client, _ledger = _client(provider)
    targets = content.preflight(workspace_id, [pending_id]).targets
    [item] = AssistedContentResolutionService(database, content)._recover_targets(
        workspace_id, targets, client, None
    )
    return item


def _row_as_read(
    database: Database, pending_id: str
) -> tuple[PendingDiscovery, PendingDiscoveryReadiness, list[AuditEvent]]:
    with database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None and pending.current_content_id is not None
        evidence = session.get(PendingPostingContent, pending.current_content_id)
        repoints = session.scalars(
            select(AuditEvent).where(
                AuditEvent.entity_id == pending_id,
                AuditEvent.action == "pending_discovery.repointed_on_content_read",
            )
        ).all()
        session.expunge_all()
        return pending, pending_discovery_readiness(pending, evidence), list(repoints)


def test_a_posting_recovered_at_another_address_leaves_its_row_ready_there(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace = WorkspaceService(
        durable_database, runtime_storage(durable_database, tmp_path)
    ).create(slug="assisted-repoint", name="Assisted repoint")
    pending_id = _staged_row(durable_database, workspace.id, KNOWN_URL)

    item = _recovered_at_publisher(durable_database, tmp_path, workspace.id, pending_id)

    pending, readiness, repoints = _row_as_read(durable_database, pending_id)
    assert item.outcome is PostingRecoveryOutcome.RECOVERED
    assert item.address_holder is None
    assert readiness is PendingDiscoveryReadiness.READY
    assert pending.canonical_url == PUBLISHER_URL
    assert (pending.discovery_url, pending.fingerprint, pending.requisition_id) == (
        KNOWN_URL,
        url_sha256(KNOWN_URL),
        "",
    )
    [event] = repoints
    assert event.details["previous_url"] == KNOWN_URL
    assert event.details["url"] == PUBLISHER_URL
    assert event.origin == "posting.recover_batch"


@pytest.mark.parametrize("holder", ["pending_discovery", "job_posting"])
def test_a_recovered_address_another_record_holds_is_not_taken_and_the_result_names_it(
    durable_database: Database, tmp_path: Path, holder: str
) -> None:
    workspace = WorkspaceService(
        durable_database, runtime_storage(durable_database, tmp_path)
    ).create(slug="assisted-held", name="Assisted held")
    pending_id = _staged_row(durable_database, workspace.id, KNOWN_URL)
    holder_id = (
        _staged_row(durable_database, workspace.id, PUBLISHER_URL)
        if holder == "pending_discovery"
        else _tracked_job(durable_database, workspace.id, PUBLISHER_URL)
    )

    item = _recovered_at_publisher(durable_database, tmp_path, workspace.id, pending_id)

    pending, readiness, repoints = _row_as_read(durable_database, pending_id)
    assert item.address_holder == AddressHolder(holder, holder_id)
    assert holder_id in item.detail
    assert pending.canonical_url == ""
    assert readiness is PendingDiscoveryReadiness.URL_MISMATCH
    assert repoints == []


@pytest.mark.parametrize(
    "held_as",
    [
        f"{PUBLISHER_URL}/",
        f"{PUBLISHER_URL}?utm_source=board",
        PUBLISHER_URL.replace("careers.", "WWW.Careers."),
    ],
)
def test_a_recovered_address_another_row_holds_in_another_form_is_not_taken(
    durable_database: Database, tmp_path: Path, held_as: str
) -> None:
    workspace = WorkspaceService(
        durable_database, runtime_storage(durable_database, tmp_path)
    ).create(slug="assisted-held-form", name="Assisted held form")
    pending_id = _staged_row(durable_database, workspace.id, KNOWN_URL)
    holder_id = _staged_row(durable_database, workspace.id, SECOND_KNOWN_URL)
    with durable_database.session() as session:
        holder = session.get(PendingDiscovery, holder_id)
        assert holder is not None
        holder.canonical_url = held_as

    item = _recovered_at_publisher(durable_database, tmp_path, workspace.id, pending_id)

    pending, readiness, repoints = _row_as_read(durable_database, pending_id)
    assert item.address_holder == AddressHolder("pending_discovery", holder_id)
    assert pending.canonical_url == ""
    assert readiness is PendingDiscoveryReadiness.URL_MISMATCH
    assert repoints == []


def test_a_repoint_after_approval_refuses_the_approved_recovery_before_any_call(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace = WorkspaceService(
        durable_database, runtime_storage(durable_database, tmp_path)
    ).create(slug="assisted-drift", name="Assisted drift")
    pending_id = _staged_row(durable_database, workspace.id, KNOWN_URL)
    content = PostingContentResolutionService(
        durable_database,
        ScriptedResolver([]),
        storage=runtime_storage(durable_database, tmp_path),
    )
    provider = ScriptedSearchProvider([])
    service = AssistedContentResolutionService(durable_database, content, provider=provider)
    settings = Settings(provider=ProviderName.OPENAI)
    approval = service.preflight(workspace.id, [pending_id], settings, now=NOW)
    [target] = content.preflight(workspace.id, [pending_id]).targets
    content.store_assisted_result(workspace.id, target, _readable(), None)
    assert _row_as_read(durable_database, pending_id)[0].canonical_url == PUBLISHER_URL

    with pytest.raises(ConflictError):
        service.recover(workspace.id, approval, settings, task_id="task-assisted", now=NOW)

    assert provider.calls == []


def test_preflight_refuses_a_row_its_employers_registered_board_already_holds(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = scan_workspace(durable_database, slug="assisted-board-held")
    board = employer_board(durable_database, workspace_id, "Example Systems")
    job_id = tracked_board_posting(
        durable_database, workspace_id, board, title="Operations Lead", url=PUBLISHER_URL
    )
    pending_id = seed_unread_row(
        durable_database,
        workspace_id,
        employer="Example Systems",
        title="Operations Lead (Remote)",
        url=KNOWN_URL,
    )
    content = PostingContentResolutionService(
        durable_database,
        ScriptedResolver([]),
        storage=runtime_storage(durable_database, tmp_path),
    )
    provider = ScriptedSearchProvider([])
    service = AssistedContentResolutionService(durable_database, content, provider=provider)

    with pytest.raises(ValidationError, match="registered board already holds") as refused:
        service.preflight(
            workspace_id, [pending_id], Settings(provider=ProviderName.OPENAI), now=NOW
        )

    assert f"job posting {job_id} at {PUBLISHER_URL}" in str(refused.value)
    assert provider.calls == []
    with durable_database.session() as session:
        row = session.get(PendingDiscovery, pending_id)
        assert row is not None
        assert (row.canonical_url, row.current_content_id) == ("", None)


@pytest.mark.parametrize("dismissed", [True, False])
def test_preflight_refuses_a_row_a_board_twin_holds_beside_the_roles_closed_earlier_posting(
    durable_database: Database, tmp_path: Path, dismissed: bool
) -> None:
    """The board reposted the role at a new address; the person may have dismissed the twin."""
    workspace_id = scan_workspace(durable_database, slug="assisted-board-twin")
    board = employer_board(durable_database, workspace_id, "Example Systems")
    board_url = "https://example-systems.example.test/careers/jobs"
    tracked_board_posting(
        durable_database,
        workspace_id,
        board,
        title="Operations Lead",
        url=f"{board_url}/7c1e4b2a-0d3f-4a5b-8c6d-9e0f1a2b3c4d",
        closed=True,
    )
    twin_url = f"{board_url}/2f0c6a1e-5b7d-4c3e-9a8f-1d2e3f4a5b6c"
    twin = seed_unread_row(
        durable_database,
        workspace_id,
        employer="Example Systems",
        title="Operations Lead",
        url=twin_url,
        source_id=board,
    )
    reader = PostingContentResolutionService(
        durable_database,
        ScriptedResolver([_readable(twin_url)]),
        storage=runtime_storage(durable_database, tmp_path),
    )
    snapshot = reader.preflight(workspace_id, [twin]).snapshot_sha256
    reader.resolve_batch(workspace_id, [twin], snapshot, origin="discovery.refresh_batch")
    if dismissed:
        dismiss_pending_discovery(durable_database, workspace_id, twin)
    pending_id = seed_unread_row(
        durable_database,
        workspace_id,
        employer="Example Systems",
        title="Operations Lead",
        url="https://careers.example.test/careers/job/?gh_jid=4821",
    )
    provider = ScriptedSearchProvider([])
    service = AssistedContentResolutionService(
        durable_database,
        PostingContentResolutionService(
            durable_database,
            ScriptedResolver([]),
            storage=runtime_storage(durable_database, tmp_path),
        ),
        provider=provider,
    )

    with pytest.raises(ValidationError, match="registered board already holds") as refused:
        service.preflight(
            workspace_id, [pending_id], Settings(provider=ProviderName.OPENAI), now=NOW
        )

    assert f"pending discovery {twin} at {twin_url}" in str(refused.value)
    assert provider.calls == []


def _identity_block(task: str) -> dict[str, str]:
    block = task.split("<posting_identity>\n", 1)[1].split("\n</posting_identity>", 1)[0]
    return cast(dict[str, str], json.loads(block))


def _identity_sent(target: PostingResolutionTarget) -> dict[str, str]:
    return _identity_block(request_plan(target, Settings(provider=ProviderName.OPENAI)).task.text)


_NORTHWIND_BOARD = "https://northwind.example.test/careers"


@pytest.mark.parametrize(
    ("boards", "expected"),
    [(0, None), (1, _NORTHWIND_BOARD), (2, None)],
)
def test_the_request_names_the_employers_board_only_when_the_workspace_registers_exactly_one(
    durable_database: Database, tmp_path: Path, boards: int, expected: str | None
) -> None:
    """Approval and recovery each look the board up, and the approved manifest still matches."""
    workspace_id = scan_workspace(durable_database, slug="assisted-board-hint")
    first = employer_board(durable_database, workspace_id, "Northwind") if boards else ""
    if boards == 2:
        with durable_database.session() as session:
            registered = session.get(Source, first)
            assert registered is not None
            session.add(
                Source(
                    workspace_id=workspace_id,
                    employer_id=registered.employer_id,
                    name="Northwind research careers",
                    source_type="employer_board",
                    url="https://jobs.lever.co/northwind-research",
                    priority=1,
                    enabled=True,
                )
            )
    pending_id = seed_unread_row(
        durable_database,
        workspace_id,
        employer="Northwind",
        title="Operations Lead",
        url=KNOWN_URL,
    )
    provider = ScriptedSearchProvider(
        [(_response(pending_id, candidates=[], supporting=[], observations=[]), _record())]
    )
    service = AssistedContentResolutionService(
        durable_database,
        PostingContentResolutionService(
            durable_database,
            ScriptedResolver([]),
            storage=runtime_storage(durable_database, tmp_path),
        ),
        provider=provider,
    )
    settings = Settings(provider=ProviderName.OPENAI)
    approval = service.preflight(workspace_id, [pending_id], settings, now=NOW)
    task = TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="posting.recover_batch",
        payload={"approval": approval.model_dump(mode="json")},
        idempotency_key=f"recover-{pending_id}",
    )

    batch = service.recover(workspace_id, approval, settings, task_id=task.id, now=NOW)

    assert [item.outcome for item in batch.items] == [PostingRecoveryOutcome.NOT_FOUND]
    [plan] = provider.calls
    sent = _identity_block(plan.task.text)
    assert sent.get("registered_board_url") == expected
    assert ("registered_board_url" in sent) is (expected is not None)


def test_a_board_changed_after_approval_refuses_the_approved_recovery_before_any_call(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = scan_workspace(durable_database, slug="assisted-board-moved")
    board_id = employer_board(durable_database, workspace_id, "Northwind")
    pending_id = seed_unread_row(
        durable_database, workspace_id, employer="Northwind", title="Operations Lead", url=KNOWN_URL
    )
    provider = ScriptedSearchProvider([])
    service = AssistedContentResolutionService(
        durable_database,
        PostingContentResolutionService(
            durable_database,
            ScriptedResolver([]),
            storage=runtime_storage(durable_database, tmp_path),
        ),
        provider=provider,
    )
    settings = Settings(provider=ProviderName.OPENAI)
    approval = service.preflight(workspace_id, [pending_id], settings, now=NOW)
    with durable_database.session() as session:
        board = session.get(Source, board_id)
        assert board is not None
        board.url = "https://jobs.lever.co/northwind"

    with pytest.raises(ConflictError):
        service.recover(workspace_id, approval, settings, task_id="task-board-moved", now=NOW)
    assert provider.calls == []


@pytest.mark.parametrize(
    ("board", "names_ats_host"),
    [
        ("https://boards.greenhouse.io/examplesystems", True),
        ("https://jobs.lever.co/examplesystems", False),
    ],
)
def test_the_known_ats_host_is_named_only_when_no_registered_board_sits_on_another_host(
    board: str, names_ats_host: bool
) -> None:
    target = replace(
        _target(url=ATS_URL, status=PostingContentAccessStatus.NOT_YET_READ),
        registered_board_url=board,
    )

    sent = _identity_sent(target)

    assert sent["registered_board_url"] == board
    assert ("known_publisher_domain" in sent) is names_ats_host


def test_the_request_reports_the_failed_read_and_names_no_unconfirmed_host_as_publisher() -> None:
    """A listing host that serves many employers is where the read already failed."""
    assert _identity_sent(_target()) == {
        "identity_id": "pending-1",
        "employer": "Example Systems",
        "title": "Operations Lead",
        "known_url": KNOWN_URL,
        "known_url_read_outcome": "render_required",
    }


def test_the_request_names_an_employers_own_ats_host_as_its_publisher_domain() -> None:
    target = _target(url=ATS_URL, status=PostingContentAccessStatus.NOT_YET_READ)

    sent = _identity_sent(target)

    assert sent["known_publisher_domain"] == "boards.greenhouse.io"
    assert "known_url_read_outcome" not in sent


def test_the_prompt_states_the_candidate_rules_the_validator_enforces() -> None:
    """A candidate without its own observation, or without a shown identity, is discarded."""
    system = render("posting_recovery_system")

    assert "observation of that same URL" in system
    assert "exact or plausible" in system
    assert "An answer with no candidate is acceptable." in system
    assert "List at most one candidate URL" in system
    assert "reads a candidate only when its observation says publisher_body or ats" in system
    assert "four hosted tool actions in all, and at most two of them may be searches" in system


def test_the_hosted_tool_budget_leaves_room_to_open_pages_beside_two_searches() -> None:
    """The provider counts a page open against the same cap as a search."""
    resolver = ScriptedResolver([_readable()])
    service, _content = _service((_target(),), resolver)
    provider, api = _responses_api(
        _response(),
        _searched("ws_1", PUBLISHER_URL, query="Example Systems Operations Lead careers"),
        _opened("ws_2", PUBLISHER_URL),
        _searched("ws_3", OTHER_URL, query="Example Systems Operations Lead"),
        _opened("ws_4", OTHER_URL),
    )
    client, _ledger = _client(provider)

    [item] = service._recover_targets("workspace-assisted", (_target(),), client, None)

    assert api.requests[0]["max_tool_calls"] == 4
    assert item.outcome is PostingRecoveryOutcome.RECOVERED


def test_a_page_the_hosted_tool_opened_counts_as_consulted() -> None:
    """The search listed only the reposting; the publisher page was reached by opening it."""
    resolver = ScriptedResolver([_readable()])
    service, _content = _service((_target(),), resolver)
    provider, _api = _responses_api(
        _response(),
        _searched("ws_1", KNOWN_URL, query="Example Systems Operations Lead"),
        _opened("ws_2", PUBLISHER_URL),
    )
    client, _ledger = _client(provider)

    [item] = service._recover_targets("workspace-assisted", (_target(),), client, None)

    assert item.outcome is PostingRecoveryOutcome.RECOVERED
    assert resolver.calls == [(PUBLISHER_URL, "Example Systems", "Operations Lead")]


def test_a_page_open_that_was_cut_off_does_not_make_its_url_citable() -> None:
    resolver = ScriptedResolver([])
    service, _content = _service((_target(),), resolver)
    provider, _api = _responses_api(
        _response(),
        _searched("ws_1", KNOWN_URL, query="Example Systems Operations Lead"),
        _opened("ws_2", PUBLISHER_URL, status="searching"),
    )
    client, ledger = _client(provider)

    [item] = service._recover_targets("workspace-assisted", (_target(),), client, None)

    assert item.outcome is PostingRecoveryOutcome.RESPONSE_REFUSED
    assert resolver.calls == []
    assert [entry.validated for entry in ledger.entries] == [False]


def test_a_readable_known_url_is_read_again_and_not_reported_as_failed() -> None:
    """A readable page that did not verify is not a failed read, so a fresh read may settle it."""
    target = _target(status=PostingContentAccessStatus.READABLE)
    resolver = ScriptedResolver([_readable(KNOWN_URL)])
    service, _content = _service((target,), resolver)
    payload = _response(
        candidates=[KNOWN_URL], supporting=[], observations=[_observation(KNOWN_URL)]
    )
    provider = ScriptedSearchProvider([(payload, _record(PUBLISHER_URL))])
    client, _ledger = _client(provider)

    [item] = service._recover_targets("workspace-assisted", (target,), client, None)

    assert "known_url_read_outcome" not in _identity_sent(target)
    assert resolver.calls == [(KNOWN_URL, "Example Systems", "Operations Lead")]
    assert item.outcome is PostingRecoveryOutcome.RECOVERED


def test_the_known_url_may_be_cited_as_support_without_a_search_returning_it() -> None:
    resolver = ScriptedResolver([_readable()])
    service, _content = _service((_target(),), resolver)
    payload = _response(supporting=[KNOWN_URL, PUBLISHER_URL])
    provider = ScriptedSearchProvider([(payload, _record(PUBLISHER_URL))])
    client, _ledger = _client(provider)

    [item] = service._recover_targets("workspace-assisted", (_target(),), client, None)

    assert item.outcome is PostingRecoveryOutcome.RECOVERED


def test_searches_that_returned_no_sources_are_not_found_rather_than_a_failure() -> None:
    """One search came back empty, and the next search and a page open were cut off."""
    resolver = ScriptedResolver([])
    service, _content = _service((_target(),), resolver)
    provider, _api = _responses_api(
        _response(candidates=[], supporting=[], observations=[]),
        _searched("ws_1", query="Example Systems Operations Lead careers"),
        _searched("ws_2", status="searching"),
        _opened("ws_3", None, status="searching"),
    )
    client, ledger = _client(provider)

    [item] = service._recover_targets("workspace-assisted", (_target(),), client, None)

    assert item.outcome is PostingRecoveryOutcome.NOT_FOUND
    assert "no sources" in item.detail
    assert [entry.validated for entry in ledger.entries] == [True]


def test_a_candidate_that_is_the_already_failed_known_url_is_not_read_again() -> None:
    resolver = ScriptedResolver([_unreadable(PostingContentAccessStatus.RENDER_REQUIRED)])
    service, _content = _service((_target(),), resolver)
    payload = _response(
        candidates=[KNOWN_URL], supporting=[], observations=[_observation(KNOWN_URL)]
    )
    provider = ScriptedSearchProvider([(payload, _record(PUBLISHER_URL))])
    client, _ledger = _client(provider)

    [item] = service._recover_targets("workspace-assisted", (_target(),), client, None)

    assert item.outcome is PostingRecoveryOutcome.SOURCE_FOUND_UNREADABLE
    assert item.candidate_url == KNOWN_URL
    assert "render_required" in item.detail
    assert resolver.calls == []


_BLOCKED_CAREERS_URL = "https://careers.example.test/careers/job/?gh_jid=4821"
_EMBED_URL = "https://careers.example.test/careers/job/?ashby_jid=2f0c6a1e-5b7d-4c3e"


@pytest.mark.parametrize(
    ("status", "candidate"),
    [
        (PostingContentAccessStatus.ACCESS_BLOCKED, _EMBED_URL),
        (
            PostingContentAccessStatus.SESSION_REQUIRED,
            _EMBED_URL.replace("://careers.", "://www.careers."),
        ),
    ],
)
def test_a_candidate_on_the_host_that_refused_the_known_url_is_not_read(
    status: PostingContentAccessStatus, candidate: str
) -> None:
    resolver = ScriptedResolver([_readable(candidate)])
    target = _target(url=_BLOCKED_CAREERS_URL, status=status)
    service, _content = _service((target,), resolver)
    payload = _response(
        candidates=[candidate], supporting=[], observations=[_observation(candidate)]
    )
    provider = ScriptedSearchProvider([(payload, _record(candidate))])
    client, _ledger = _client(provider)

    [item] = service._recover_targets("workspace-assisted", (target,), client, None)

    assert item.outcome is PostingRecoveryOutcome.SOURCE_FOUND_UNREADABLE
    assert item.candidate_url == candidate
    assert item.detail == (
        f"The cited candidate is on careers.example.test, where the known URL was already "
        f"read as {status.value}."
    )
    assert resolver.calls == []


_REFUSED_ATS_URL = "https://boards.greenhouse.io/northwind/jobs/4821"


@pytest.mark.parametrize(
    "candidate",
    [
        "https://boards.greenhouse.io/northwind/jobs/5930",
        "https://boards.greenhouse.io/globex/jobs/7316",
    ],
)
def test_a_candidate_on_the_shared_ats_host_that_refused_the_known_url_is_read(
    candidate: str,
) -> None:
    """One tenant's refused record says nothing about the host's other records."""
    resolver = ScriptedResolver([_readable(candidate)])
    target = _target(url=_REFUSED_ATS_URL, status=PostingContentAccessStatus.ACCESS_BLOCKED)
    service, _content = _service((target,), resolver)
    payload = _response(
        candidates=[candidate], supporting=[], observations=[_observation(candidate)]
    )
    provider = ScriptedSearchProvider([(payload, _record(candidate))])
    client, _ledger = _client(provider)

    [item] = service._recover_targets("workspace-assisted", (target,), client, None)

    assert item.outcome is PostingRecoveryOutcome.RECOVERED
    assert resolver.calls == [(candidate, "Example Systems", "Operations Lead")]


def test_the_refused_known_url_on_a_shared_ats_host_is_not_read_again() -> None:
    resolver = ScriptedResolver([])
    target = _target(url=_REFUSED_ATS_URL, status=PostingContentAccessStatus.SESSION_REQUIRED)
    service, _content = _service((target,), resolver)
    payload = _response(
        candidates=[_REFUSED_ATS_URL],
        supporting=[],
        observations=[_observation(_REFUSED_ATS_URL)],
    )
    provider = ScriptedSearchProvider([(payload, _record(PUBLISHER_URL))])
    client, _ledger = _client(provider)

    [item] = service._recover_targets("workspace-assisted", (target,), client, None)

    assert item.outcome is PostingRecoveryOutcome.SOURCE_FOUND_UNREADABLE
    assert item.detail == (
        "The cited candidate is the known URL, already read as session_required."
    )
    assert resolver.calls == []


def test_a_candidate_on_the_host_of_a_known_url_that_needed_rendering_is_read() -> None:
    resolver = ScriptedResolver([_readable(_EMBED_URL)])
    target = _target(url=_BLOCKED_CAREERS_URL, status=PostingContentAccessStatus.RENDER_REQUIRED)
    service, _content = _service((target,), resolver)
    payload = _response(
        candidates=[_EMBED_URL], supporting=[], observations=[_observation(_EMBED_URL)]
    )
    provider = ScriptedSearchProvider([(payload, _record(_EMBED_URL))])
    client, _ledger = _client(provider)

    [item] = service._recover_targets("workspace-assisted", (target,), client, None)

    assert item.outcome is PostingRecoveryOutcome.RECOVERED
    assert resolver.calls == [(_EMBED_URL, "Example Systems", "Operations Lead")]


def test_the_estimate_prices_every_hosted_tool_call_as_a_search(
    durable_database: Database,
) -> None:
    """Only the total tool budget is enforced, so the estimate cannot assume page opens."""
    target = _target()
    service, _content = _service((target,), ScriptedResolver([]), durable_database)
    settings = Settings(
        provider=ProviderName.OPENAI,
        prices={"gpt-6.1-sol": PriceOverride(input=0, output=0, search_per_thousand_calls=1000.0)},
    )

    approval = service.preflight("workspace-assisted", [target.id], settings, now=NOW)

    assert approval.estimate_cents == 400


_GLOBEX_ASHBY = "https://jobs.ashbyhq.com/globex"
_GLOBEX_GREENHOUSE = "https://job-boards.greenhouse.io/globex"
_ASHBY_JOB = "7d3b2c1a-4e5f-4a6b-8c9d-0e1f2a3b4c5d"
_ASHBY_RECORD = f"{_GLOBEX_ASHBY}/{_ASHBY_JOB}"
_GREENHOUSE_RECORD = f"{_GLOBEX_GREENHOUSE}/jobs/8005904003"


def _board_target(
    board: str, *records: str, closed: tuple[str, ...] = ()
) -> PostingResolutionTarget:
    """A target whose employer registers ``board``, which stores these records."""
    return replace(
        _target(),
        registered_board_url=board,
        registered_boards=(board,),
        board_records=tuple(
            BoardRecord(
                BoardHolder("job_posting", f"job-{position}", url),
                "source-globex",
                "Operations Lead",
                (),
                frozenset(),
                closed=url in closed,
            )
            for position, url in enumerate((*records, *closed))
        ),
    )


def _recover_cited(
    target: PostingResolutionTarget, candidate: str, *, cited: bool = True
) -> tuple[AssistedPostingRecoveryItem, ScriptedResolver]:
    resolver = ScriptedResolver([_readable(url) for url in (_ASHBY_RECORD, _GREENHOUSE_RECORD)])
    service, _content = _service((target,), resolver)
    payload = _response(
        candidates=[candidate], supporting=[], observations=[_observation(candidate)]
    )
    record = _record(candidate) if cited else _record(PUBLISHER_URL)
    client, _ledger = _client(ScriptedSearchProvider([(payload, record)]))
    [item] = service._recover_targets("workspace-assisted", (target,), client, None)
    return item, resolver


@pytest.mark.parametrize(
    ("board", "candidate", "record"),
    [
        (
            _GLOBEX_ASHBY,
            f"https://careers.globex.example/careers/job/?ashby_jid={_ASHBY_JOB}",
            _ASHBY_RECORD,
        ),
        (
            _GLOBEX_GREENHOUSE,
            "https://careers.globex.example/careers/job/?gh_jid=8005904003",
            _GREENHOUSE_RECORD,
        ),
    ],
)
def test_a_careers_page_job_id_is_read_at_the_registered_boards_open_record(
    board: str, candidate: str, record: str
) -> None:
    item, resolver = _recover_cited(_board_target(board, record), candidate)

    assert item.candidate_url == record
    assert [call[0] for call in resolver.calls] == [record]


@pytest.mark.parametrize(
    ("board", "candidate", "record"),
    [
        (
            _GLOBEX_ASHBY,
            "https://careers.globex.example/careers/job/"
            "?ashby_jid=11111111-2222-4333-8444-555555555555",
            _ASHBY_RECORD,
        ),
        (
            _GLOBEX_GREENHOUSE,
            "https://careers.globex.example/careers/job/?gh_jid=8002983003",
            _GREENHOUSE_RECORD,
        ),
    ],
)
def test_a_careers_page_job_id_the_board_does_not_list_as_open_is_refused_stale(
    board: str, candidate: str, record: str
) -> None:
    item, resolver = _recover_cited(_board_target(board, record), candidate)

    assert item.outcome is PostingRecoveryOutcome.SOURCE_FOUND_UNREADABLE
    assert item.candidate_url == candidate
    assert "does not list as open" in item.detail
    assert resolver.calls == []


def test_an_uncited_candidate_the_registered_board_stores_is_read_without_a_citation() -> None:
    target = _board_target(_GLOBEX_ASHBY, _ASHBY_RECORD)

    item, resolver = _recover_cited(target, _ASHBY_RECORD, cited=False)

    assert item.outcome is PostingRecoveryOutcome.RECOVERED
    assert [call[0] for call in resolver.calls] == [_ASHBY_RECORD]


def test_an_uncited_candidate_the_registered_board_does_not_store_stays_refused() -> None:
    target = _board_target(_GLOBEX_ASHBY, _ASHBY_RECORD)
    other = f"{_GLOBEX_ASHBY}/11111111-2222-4333-8444-555555555555"

    item, resolver = _recover_cited(target, other, cited=False)

    assert item.outcome is PostingRecoveryOutcome.RESPONSE_REFUSED
    assert "uncited observation" in item.detail
    assert resolver.calls == []


_CAREERS_RECORD = "https://careers.globex.example/open-roles/job?gh_jid=8005904003"


@pytest.mark.parametrize("cited", [True, False])
def test_a_board_record_stored_at_a_careers_page_job_id_is_read_there(cited: bool) -> None:
    target = _board_target(_GLOBEX_GREENHOUSE, _CAREERS_RECORD)

    item, resolver = _recover_cited(target, _CAREERS_RECORD, cited=cited)

    assert item.candidate_url == _CAREERS_RECORD
    assert [call[0] for call in resolver.calls] == [_CAREERS_RECORD]


def test_another_careers_address_for_that_job_id_is_read_at_the_stored_record() -> None:
    target = _board_target(_GLOBEX_GREENHOUSE, _CAREERS_RECORD)
    candidate = "https://careers.globex.example/careers/job/?gh_jid=8005904003"

    item, resolver = _recover_cited(target, candidate)

    assert item.candidate_url == _CAREERS_RECORD
    assert [call[0] for call in resolver.calls] == [_CAREERS_RECORD]


def test_a_careers_page_job_id_naming_the_rows_own_requisition_is_not_called_stale() -> None:
    own = "https://careers.globex.example/careers/job/?gh_jid=8005904003"
    target = replace(_board_target(_GLOBEX_GREENHOUSE), url=_GREENHOUSE_RECORD)

    item, resolver = _recover_cited(target, own)

    assert "does not list as open" not in item.detail
    assert [call[0] for call in resolver.calls] == [own]


def test_a_careers_page_job_id_naming_a_closed_board_record_is_refused_stale() -> None:
    target = _board_target(_GLOBEX_GREENHOUSE, closed=(_GREENHOUSE_RECORD,))
    candidate = "https://careers.globex.example/careers/job/?gh_jid=8005904003"

    item, resolver = _recover_cited(target, candidate)

    assert item.outcome is PostingRecoveryOutcome.SOURCE_FOUND_UNREADABLE
    assert "does not list as open" in item.detail
    assert resolver.calls == []


def test_an_uncited_candidate_at_a_closed_board_record_stays_refused() -> None:
    target = _board_target(_GLOBEX_ASHBY, closed=(_ASHBY_RECORD,))

    item, resolver = _recover_cited(target, _ASHBY_RECORD, cited=False)

    assert item.outcome is PostingRecoveryOutcome.RESPONSE_REFUSED
    assert resolver.calls == []


def test_an_uncited_candidate_a_registered_board_tracks_in_the_database_is_read(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = scan_workspace(durable_database, slug="assisted-board-stored")
    board = employer_board(durable_database, workspace_id, "Northwind")
    stored = "https://northwind.example.test/careers/analytics-lead"
    tracked_board_posting(durable_database, workspace_id, board, title="Analytics Lead", url=stored)
    pending_id = seed_unread_row(
        durable_database, workspace_id, employer="Northwind", title="Operations Lead", url=KNOWN_URL
    )
    resolver = ScriptedResolver([_readable(stored)])
    payload = _response(
        pending_id, candidates=[stored], supporting=[], observations=[_observation(stored)]
    )
    provider = ScriptedSearchProvider([(payload, _record(PUBLISHER_URL))])
    service = AssistedContentResolutionService(
        durable_database,
        PostingContentResolutionService(
            durable_database, resolver, storage=runtime_storage(durable_database, tmp_path)
        ),
        provider=provider,
    )
    settings = Settings(provider=ProviderName.OPENAI)
    approval = service.preflight(workspace_id, [pending_id], settings, now=NOW)
    task = TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="posting.recover_batch",
        payload={"approval": approval.model_dump(mode="json")},
        idempotency_key=f"recover-{pending_id}",
    )

    [item] = service.recover(workspace_id, approval, settings, task_id=task.id, now=NOW).items

    assert item.outcome is PostingRecoveryOutcome.RECOVERED
    assert item.candidate_url == stored
    assert [call[0] for call in resolver.calls] == [stored]
