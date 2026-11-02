"""OpenAI background dispatch, polling, and cancellation, against a fake SDK client."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from pydantic import BaseModel

from tailorcv.application.config import (
    BACKGROUND_WEB_SEARCH_PROVIDERS,
    Effort,
    ModelChoice,
    ProviderName,
)
from tailorcv.infrastructure.providers.anthropic import AnthropicProvider
from tailorcv.infrastructure.providers.base import (
    BackgroundDispatch,
    BackgroundWebSearchProvider,
    envelope_of,
)
from tailorcv.infrastructure.providers.capture import CapturingProvider
from tailorcv.infrastructure.providers.openai import OpenAIProvider
from tailorcv.infrastructure.providers.plan import SearchPolicy
from tailorcv.shared.costs import ServiceTier
from tailorcv.shared.errors import CapacityError, ProviderError, TailorcvError
from tests.delivery.test_adapters import OPENAI_BODY, FakeOpenAI
from tests.support.plans import plan_for
from tests.support.provider_clients import provider_for_http


class Answer(BaseModel):
    value: str


CHOICE = ModelChoice(model="model", effort=Effort.HIGH)


def _usage(input_tokens: int, output_tokens: int, *, reasoning_tokens: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        input_tokens_details=SimpleNamespace(cached_tokens=0, cache_write_tokens=0),
        output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning_tokens),
    )


class FakeBackgroundOpenAI:
    """Stand in for the SDK client's background dispatch, retrieve, and cancel."""

    def __init__(
        self,
        create_error: Exception | None = None,
        retrieve_error: Exception | None = None,
        cancel_error: Exception | None = None,
        refused_tier: str | None = None,
    ) -> None:
        self.with_options_calls: list[dict[str, Any]] = []
        self.create_kwargs: dict[str, Any] = {}
        self.retrieve_calls: list[tuple[str, dict[str, Any]]] = []
        self.cancel_calls: list[str] = []
        self.poll_responses: list[Any] = []
        self.created_id = "resp_bg_01"
        self.create_error = create_error
        self.retrieve_error = retrieve_error
        self.cancel_error = cancel_error
        self.refused_tier = refused_tier
        self.responses = SimpleNamespace(
            create=self._create, retrieve=self._retrieve, cancel=self._cancel
        )

    def with_options(self, **kwargs: Any) -> Any:
        self.with_options_calls.append(kwargs)
        return self

    def _create(self, **kwargs: Any) -> Any:
        if self.create_error is not None:
            raise self.create_error
        if kwargs.get("service_tier") == self.refused_tier:
            raise _status_error(429, "resource_unavailable")
        self.create_kwargs = kwargs
        return SimpleNamespace(id=self.created_id)

    def _retrieve(self, response_id: str, **kwargs: Any) -> Any:
        if self.retrieve_error is not None:
            raise self.retrieve_error
        self.retrieve_calls.append((response_id, kwargs))
        return self.poll_responses.pop(0)

    def _cancel(self, response_id: str) -> None:
        if self.cancel_error is not None:
            raise self.cancel_error
        self.cancel_calls.append(response_id)


def test_the_campaign_provider_list_names_exactly_the_adapters_with_background_search() -> None:
    """Preview refuses from the list, the runner from the adapter; they must agree."""
    adapters = {OpenAIProvider: ProviderName.OPENAI, AnthropicProvider: ProviderName.ANTHROPIC}

    background = {
        name
        for adapter, name in adapters.items()
        if all(hasattr(adapter, method) for method in ("dispatch_background", "poll_background"))
    }

    assert frozenset(background) == BACKGROUND_WEB_SEARCH_PROVIDERS
    assert set(adapters.values()) == set(ProviderName)


def test_dispatch_sends_background_and_store_with_no_streaming_and_max_retries_zero() -> None:
    client = FakeBackgroundOpenAI()
    provider = OpenAIProvider(api_key="x", client=cast(Any, client))

    dispatched = provider.dispatch_background(
        plan_for(search=SearchPolicy()), Answer, CHOICE, metadata={"task_id": "t1"}
    )

    assert dispatched.response_id == "resp_bg_01"
    assert client.with_options_calls == [{"max_retries": 0}]
    assert client.create_kwargs["background"] is True
    assert client.create_kwargs["store"] is True
    assert "stream" not in client.create_kwargs
    assert client.create_kwargs["metadata"] == {"task_id": "t1"}
    assert client.create_kwargs["include"] == ["web_search_call.action.sources"]
    # On gpt-5.6 and later a key only partitions the cache, so one would keep
    # identical instructions from being shared between requests.
    assert "prompt_cache_key" not in client.create_kwargs


def test_dispatch_returns_the_request_it_sent_as_its_envelope() -> None:
    """The poll cannot return the request, so the dispatch is the only place it exists."""
    client = FakeBackgroundOpenAI()
    provider = OpenAIProvider(api_key="x", client=cast(Any, client))

    dispatched = provider.dispatch_background(
        plan_for(search=SearchPolicy()), Answer, CHOICE, metadata={"task_id": "t1"}
    )

    assert dispatched.request_envelope == envelope_of(client.create_kwargs)
    assert dispatched.request_envelope is not None
    assert dispatched.request_envelope["background"] is True
    assert dispatched.request_envelope["store"] is True
    assert dispatched.request_envelope["metadata"] == {"task_id": "t1"}


def test_dispatch_raises_when_the_provider_returns_no_response_id() -> None:
    client = FakeBackgroundOpenAI()
    client.created_id = ""
    provider = OpenAIProvider(api_key="x", client=cast(Any, client))

    with pytest.raises(ProviderError, match="no response id"):
        provider.dispatch_background(plan_for(), Answer, CHOICE, metadata={})


@pytest.mark.parametrize("status", ["queued", "in_progress"])
def test_poll_returns_pending_without_raising_and_asks_for_sources(status: str) -> None:
    client = FakeBackgroundOpenAI()
    client.poll_responses = [SimpleNamespace(status=status, id="resp_1")]
    provider = OpenAIProvider(api_key="x", client=cast(Any, client))

    poll = provider.poll_background("resp_1", Answer, 8192)

    assert poll.status == "pending"
    assert poll.value is None
    assert poll.record is None
    assert client.retrieve_calls == [("resp_1", {"include": ["web_search_call.action.sources"]})]
    assert client.with_options_calls == [], "retrieve is idempotent; it keeps the default retries"


def test_poll_returns_the_validated_value_and_record_when_completed() -> None:
    client = FakeBackgroundOpenAI()
    client.poll_responses = [
        SimpleNamespace(
            output_text=Answer(value="ok").model_dump_json(),
            id="resp_1",
            model="gpt-5.6-terra",
            status="completed",
            incomplete_details=None,
            service_tier="default",
            output=[],
            usage=_usage(10, 5),
        )
    ]
    provider = OpenAIProvider(api_key="x", client=cast(Any, client))

    poll = provider.poll_background("resp_1", Answer, 8192)

    assert poll.status == "complete"
    assert poll.value == Answer(value="ok")
    assert poll.record is not None
    assert poll.record.usage.input_tokens == 10
    assert poll.raw is not None


def test_poll_returns_unusable_with_a_record_for_a_cancelled_response() -> None:
    client = FakeBackgroundOpenAI()
    client.poll_responses = [
        SimpleNamespace(
            id="resp_1",
            model="gpt-5.6-terra",
            status="cancelled",
            incomplete_details=None,
            service_tier="default",
            output=[],
            usage=_usage(10, 5),
        )
    ]
    provider = OpenAIProvider(api_key="x", client=cast(Any, client))

    poll = provider.poll_background("resp_1", Answer, 8192)

    assert poll.status == "unusable"
    assert poll.value is None
    assert poll.record is not None
    assert poll.record.usage.input_tokens == 10


def test_poll_returns_unusable_with_usage_for_an_incomplete_max_tokens_response() -> None:
    client = FakeBackgroundOpenAI()
    client.poll_responses = [
        SimpleNamespace(
            id="resp_1",
            model="gpt-5.6-terra",
            status="incomplete",
            incomplete_details=SimpleNamespace(reason="max_output_tokens"),
            service_tier="default",
            output=[],
            usage=_usage(10, 8000, reasoning_tokens=7900),
        )
    ]
    provider = OpenAIProvider(api_key="x", client=cast(Any, client))

    poll = provider.poll_background("resp_1", Answer, 8010)

    assert poll.status == "unusable"
    assert poll.record is not None
    assert poll.record.usage.output_tokens == 8000
    assert "output limit" in poll.error


def test_cancel_sends_the_response_id_to_the_provider() -> None:
    client = FakeBackgroundOpenAI()
    provider = OpenAIProvider(api_key="x", client=cast(Any, client))

    provider.cancel_background("resp_1")

    assert client.cancel_calls == ["resp_1"]
    assert client.with_options_calls == [], "cancel is idempotent; it keeps the default retries"


def _status_error(status: int, code: str | None = None) -> Exception:
    import httpx
    from openai import APIStatusError

    body = {"error": {"code": code}} if code else None
    return APIStatusError(
        "rejected",
        response=httpx.Response(status, request=httpx.Request("POST", "https://api.openai.com")),
        body=body,
    )


@pytest.mark.parametrize(("status", "code"), [(400, None), (401, None), (402, None), (429, None)])
def test_a_4xx_dispatch_failure_is_certainly_not_created(status: int, code: str | None) -> None:
    """A 4xx means the request never reached creation, so the attempt stays plannable."""
    client = FakeBackgroundOpenAI(create_error=_status_error(status, code))
    provider = OpenAIProvider(api_key="x", client=cast(Any, client))

    with pytest.raises(TailorcvError) as raised:
        provider.dispatch_background(plan_for(), Answer, CHOICE, metadata={})

    assert raised.value.certainly_not_created is True


@pytest.mark.parametrize("status", [500, 503])
def test_a_5xx_dispatch_failure_is_not_certainly_rejected(status: int) -> None:
    """A server error leaves it genuinely unknown whether anything was created."""
    client = FakeBackgroundOpenAI(create_error=_status_error(status))
    provider = OpenAIProvider(api_key="x", client=cast(Any, client))

    with pytest.raises(ProviderError) as raised:
        provider.dispatch_background(plan_for(), Answer, CHOICE, metadata={})

    assert raised.value.certainly_not_created is False


def test_a_connection_failure_dispatching_is_not_certainly_rejected() -> None:
    """No status at all means the request may or may not have reached the server."""
    import httpx
    from openai import APIConnectionError

    error = APIConnectionError(request=httpx.Request("POST", "https://api.openai.com"))
    client = FakeBackgroundOpenAI(create_error=error)
    provider = OpenAIProvider(api_key="x", client=cast(Any, client))

    with pytest.raises(ProviderError) as raised:
        provider.dispatch_background(plan_for(), Answer, CHOICE, metadata={})

    assert raised.value.certainly_not_created is False


def test_a_poll_of_a_missing_response_names_the_response_not_the_model() -> None:
    """A 404 on retrieve means the response id is gone, not that the model is unknown."""
    client = FakeBackgroundOpenAI(retrieve_error=_status_error(404))
    provider = OpenAIProvider(api_key="x", client=cast(Any, client))

    with pytest.raises(ProviderError, match="could not find that background response") as raised:
        provider.poll_background("resp_1", Answer, 8192)

    assert "configured model" not in str(raised.value)


def test_a_cancel_of_a_missing_response_names_the_response_not_the_model() -> None:
    client = FakeBackgroundOpenAI(cancel_error=_status_error(404))
    provider = OpenAIProvider(api_key="x", client=cast(Any, client))

    with pytest.raises(ProviderError, match="could not find that background response") as raised:
        provider.cancel_background("resp_1")

    assert "configured model" not in str(raised.value)


def _dispatch_on_flex_after_capacity_error(
    settings: Any,
    status: int,
    calls: list[dict[str, Any]],
    *,
    refusals: int = 1,
    slept: list[float] | None = None,
) -> BackgroundDispatch:
    from tailorcv.infrastructure.providers.cache import ResponseCache
    from tailorcv.infrastructure.providers.client import Client
    from tailorcv.shared.costs import Ledger, ServiceTier

    client_sdk = FakeBackgroundOpenAI()
    original_create = client_sdk._create

    def flaky_create(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if len(calls) <= refusals:
            raise _status_error(status, "resource_unavailable")
        return original_create(**kwargs)

    client_sdk.responses.create = flaky_create
    provider = OpenAIProvider(api_key="x", client=cast(Any, client_sdk))
    flex_choice = ModelChoice(model="model", effort=Effort.HIGH, service_tier=ServiceTier.FLEX)
    with ResponseCache(enabled=False) as cache:
        instance = Client(provider, settings, cache, Ledger())
        instance.sleeper = (slept if slept is not None else []).append
        return instance.dispatch_background(
            plan_for(), Answer, flex_choice, metadata={"task_id": "t1"}
        )


def test_a_rejected_flex_dispatch_waits_and_is_resent_on_flex(settings: Any) -> None:
    calls: list[dict[str, Any]] = []
    slept: list[float] = []
    dispatched = _dispatch_on_flex_after_capacity_error(settings, 429, calls, slept=slept)

    assert [call["service_tier"] for call in calls] == ["flex", "flex"]
    assert len(slept) == 1
    assert dispatched.request_envelope is not None
    assert dispatched.request_envelope["service_tier"] == "flex"


def test_a_flex_dispatch_refused_on_every_retry_is_resent_on_the_standard_tier(
    settings: Any,
) -> None:
    calls: list[dict[str, Any]] = []
    slept: list[float] = []
    _dispatch_on_flex_after_capacity_error(settings, 429, calls, refusals=4, slept=slept)

    assert [call["service_tier"] for call in calls] == ["flex"] * 4 + ["default"]
    assert len(slept) == 3
    assert slept == sorted(set(slept))


def test_a_resent_flex_dispatch_returns_the_standard_tier_request_as_its_envelope(
    settings: Any,
) -> None:
    """The resend created the response, so its request is the one worth keeping."""
    calls: list[dict[str, Any]] = []
    dispatched = _dispatch_on_flex_after_capacity_error(settings, 429, calls, refusals=4)

    assert dispatched.request_envelope is not None
    assert dispatched.request_envelope["service_tier"] == "default"


def test_an_ambiguous_flex_capacity_failure_is_never_resent(settings: Any) -> None:
    calls: list[dict[str, Any]] = []
    slept: list[float] = []
    with pytest.raises(CapacityError):
        _dispatch_on_flex_after_capacity_error(settings, 503, calls, slept=slept)

    assert [call["service_tier"] for call in calls] == ["flex"]
    assert slept == []


class _PlainAdapter:
    """An adapter shaped like AnthropicProvider: no background methods at all."""

    name = "anthropic"

    def check_credentials(self) -> None: ...

    def available_models(self) -> frozenset[str] | None:
        return None

    def complete(self, plan: Any, schema: Any, choice: Any) -> Any: ...

    def complete_with_web_search(self, plan: Any, schema: Any, choice: Any) -> Any: ...


def test_capturing_provider_without_background_methods_is_not_a_background_provider(
    tmp_path: Any,
) -> None:
    wrapped = CapturingProvider(_PlainAdapter(), tmp_path)

    assert not isinstance(wrapped, BackgroundWebSearchProvider)


def test_capturing_provider_around_openai_is_a_background_provider(tmp_path: Any) -> None:
    inner = OpenAIProvider(api_key="x", client=cast(Any, FakeOpenAI()))
    wrapped = CapturingProvider(inner, tmp_path)

    assert isinstance(wrapped, BackgroundWebSearchProvider)


def test_capturing_provider_writes_nothing_for_a_pending_poll_but_writes_a_terminal_one(
    tmp_path: Any,
) -> None:
    """A long-running attempt is polled many times; only the terminal poll is worth keeping."""
    sdk = FakeBackgroundOpenAI()
    inner = OpenAIProvider(api_key="x", client=cast(Any, sdk))
    wrapped = CapturingProvider(inner, tmp_path)

    sdk.poll_responses = [SimpleNamespace(status="in_progress", id="resp_1")]
    pending = wrapped.poll_background("resp_1", Answer, 8192)
    assert pending.status == "pending"
    assert list(tmp_path.glob("call-*.json")) == []

    sdk.poll_responses = [_terminal_response("resp_1")]
    terminal = wrapped.poll_background("resp_1", Answer, 8192)
    assert terminal.status == "complete"
    assert len(list(tmp_path.glob("call-*.json"))) == 1


def _retrieving_provider() -> Any:
    """Return the real adapter whose background retrieve answers one canned response body."""
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=OPENAI_BODY))
    return provider_for_http(ProviderName.OPENAI, httpx.Client(transport=transport))


def test_a_terminal_poll_is_captured_with_the_provider_body_only_once(tmp_path: Any) -> None:
    """The captured record already carries the response envelope the poll returned."""
    wrapped = CapturingProvider(_retrieving_provider(), tmp_path)

    poll = wrapped.poll_background("resp_envelope", Answer, 8192)

    written = json.loads((tmp_path / "call-0001.json").read_text(encoding="utf-8"))
    assert poll.raw is not None and poll.raw["status"] == "completed"
    assert written["record"]["response_envelope"] == poll.raw
    assert "raw" not in written


def test_a_terminal_poll_puts_both_envelopes_on_its_ledger_entry(settings: Any) -> None:
    """A campaign attempt reads both envelopes from this entry rather than storing its own copy."""
    from tailorcv.infrastructure.providers.cache import ResponseCache
    from tailorcv.infrastructure.providers.client import Client
    from tailorcv.shared.costs import Ledger

    sent = {"model": "model", "input": "a synthetic prompt", "background": True}
    ledger = Ledger()
    with ResponseCache(enabled=False) as cache:
        client = Client(_retrieving_provider(), settings, cache, ledger)
        poll = client.collect_background(
            "resp_envelope",
            plan_for(search=SearchPolicy()),
            Answer,
            CHOICE,
            time.monotonic(),
            request_envelope=sent,
        )

    assert poll.status == "complete"
    assert ledger.entries[0].response_envelope == poll.raw
    assert ledger.entries[0].request_envelope == sent


def _terminal_response(response_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        output_text=Answer(value="ok").model_dump_json(),
        id=response_id,
        model="gpt-5.6-terra",
        status="completed",
        incomplete_details=None,
        service_tier="default",
        output=[],
        usage=_usage(10, 5),
    )


def test_client_collect_background_records_exactly_one_ledger_entry_on_a_terminal_poll(
    settings: Any,
) -> None:
    from tailorcv.infrastructure.providers.cache import ResponseCache
    from tailorcv.infrastructure.providers.client import Client
    from tailorcv.shared.costs import Ledger

    sdk = FakeBackgroundOpenAI()
    provider = OpenAIProvider(api_key="x", client=cast(Any, sdk))
    ledger = Ledger()
    with ResponseCache(enabled=False) as cache:
        client = Client(provider, settings, cache, ledger)
        plan = plan_for(search=SearchPolicy())
        dispatched = client.dispatch_background(plan, Answer, CHOICE, metadata={"task_id": "t1"})
        response_id = dispatched.response_id

        sdk.poll_responses = [SimpleNamespace(status="in_progress", id=response_id)]
        pending = client.collect_background(
            response_id,
            plan,
            Answer,
            CHOICE,
            time.monotonic(),
            request_envelope=dispatched.request_envelope,
        )
        assert pending.status == "pending"
        assert len(ledger.entries) == 0

        sdk.poll_responses = [_terminal_response(response_id)]
        terminal = client.collect_background(
            response_id,
            plan,
            Answer,
            CHOICE,
            time.monotonic(),
            request_envelope=dispatched.request_envelope,
        )

    assert terminal.status == "complete"
    assert terminal.value == Answer(value="ok")
    assert len(ledger.entries) == 1
    assert ledger.entries[0].validated is True
    assert ledger.entries[0].request_envelope == dispatched.request_envelope


def test_a_second_terminal_poll_of_the_same_response_records_no_second_ledger_entry(
    settings: Any,
) -> None:
    """A resume after a crash, or a runner that polls again, must not bill twice."""
    from tailorcv.infrastructure.providers.cache import ResponseCache
    from tailorcv.infrastructure.providers.client import Client
    from tailorcv.shared.costs import Ledger

    sdk = FakeBackgroundOpenAI()
    provider = OpenAIProvider(api_key="x", client=cast(Any, sdk))
    ledger = Ledger()
    with ResponseCache(enabled=False) as cache:
        client = Client(provider, settings, cache, ledger)
        plan = plan_for(search=SearchPolicy())
        dispatched = client.dispatch_background(plan, Answer, CHOICE, metadata={"task_id": "t1"})
        response_id = dispatched.response_id

        sdk.poll_responses = [_terminal_response(response_id)]
        first = client.collect_background(
            response_id,
            plan,
            Answer,
            CHOICE,
            time.monotonic(),
            request_envelope=dispatched.request_envelope,
        )
        sdk.poll_responses = [_terminal_response(response_id)]
        second = client.collect_background(
            response_id,
            plan,
            Answer,
            CHOICE,
            time.monotonic(),
            request_envelope=dispatched.request_envelope,
        )

    assert first.status == second.status == "complete"
    assert len(ledger.entries) == 1


def test_collect_background_describes_the_call_with_the_passed_choice_not_current_settings(
    settings: Any,
) -> None:
    """A resume must record the dispatch-time choice, since settings can move on."""
    from tailorcv.infrastructure.providers.cache import ResponseCache
    from tailorcv.infrastructure.providers.client import Client
    from tailorcv.shared.costs import Ledger

    sdk = FakeBackgroundOpenAI()
    provider = OpenAIProvider(api_key="x", client=cast(Any, sdk))
    ledger = Ledger()
    dispatched = ModelChoice(model="model", effort=Effort.HIGH)
    with ResponseCache(enabled=False) as cache:
        client = Client(provider, settings, cache, ledger)
        plan = plan_for(search=SearchPolicy())
        response_id = client.dispatch_background(
            plan, Answer, dispatched, metadata={"task_id": "t1"}
        ).response_id

        # A resume that resolved a different choice from current settings must
        # not change what gets recorded: only the passed-in choice does.
        sdk.poll_responses = [_terminal_response(response_id)]
        different_choice = ModelChoice(model="model", effort=Effort.LOW)
        client.collect_background(
            response_id, plan, Answer, different_choice, time.monotonic(), request_envelope=None
        )

    assert len(ledger.entries) == 1
    assert ledger.entries[0].effort == "low"


@pytest.mark.parametrize(
    ("refused_tier", "resent"), [("flex", True), (None, False)], ids=["refused", "accepted"]
)
def test_a_background_flex_call_records_whether_standard_answered_it(
    settings: Any, refused_tier: str | None, resent: bool
) -> None:
    from tailorcv.infrastructure.providers.cache import ResponseCache
    from tailorcv.infrastructure.providers.client import Client
    from tailorcv.shared.costs import Ledger

    sdk = FakeBackgroundOpenAI(refused_tier=refused_tier)
    provider = OpenAIProvider(api_key="x", client=cast(Any, sdk))
    flex = CHOICE.model_copy(update={"service_tier": ServiceTier.FLEX})
    ledger = Ledger()
    slept: list[float] = []
    with ResponseCache(enabled=False) as cache:
        client = Client(provider, settings, cache, ledger)
        client.sleeper = slept.append
        plan = plan_for(search=SearchPolicy())
        dispatched = client.dispatch_background(plan, Answer, flex, metadata={})
        sdk.poll_responses = [_terminal_response(dispatched.response_id)]
        client.collect_background(
            dispatched.response_id,
            plan,
            Answer,
            flex,
            time.monotonic(),
            request_envelope=dispatched.request_envelope,
        )

    [entry] = ledger.entries
    assert entry.resent_on_standard is resent
