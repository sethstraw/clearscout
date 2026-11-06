"""The two SDK adapters, against fake clients. No test here reaches a network."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import pytest
from evals.support.inventory import synthetic_inventory
from pydantic import BaseModel

from clearscout.application.config import (
    FLEX_TIMEOUT_FLOOR,
    Effort,
    ImageDetail,
    ModelChoice,
    ProviderName,
    Settings,
    Stage,
    Verbosity,
)
from clearscout.application.discovery.relevance_review import review_plan
from clearscout.application.prompts import kernel
from clearscout.infrastructure.providers.anthropic import AnthropicProvider
from clearscout.infrastructure.providers.openai import OpenAIProvider
from clearscout.infrastructure.providers.plan import (
    Image,
    SearchDepth,
    SearchLocation,
    SearchPolicy,
)
from clearscout.shared.costs import ServiceTier
from clearscout.shared.errors import AuthError, ConfigError, ProviderError
from tests.support.plans import plan_for


class Answer(BaseModel):
    value: str


MISSING = object()
CHOICE = ModelChoice(model="model", effort=Effort.HIGH)
# Stands in for the vendor blob a result carries, which no record may keep.
SECRET_PASSAGE = "EqgfCioIARgBIiQ3YTAwMjY1Mi1mZjM5"


@contextmanager
def streaming(message: Any) -> Iterator[Any]:
    """Stand in for the streaming context manager the Anthropic SDK returns."""
    yield SimpleNamespace(get_final_message=lambda: message)


class FakeAnthropic:
    def __init__(self, parsed: Any = MISSING, error: Exception | None = None) -> None:
        self.parsed = Answer(value="ok") if parsed is MISSING else parsed
        self.error = error
        self.kwargs: dict[str, Any] = {}
        self.requests: list[dict[str, Any]] = []
        self.listed = False
        self.messages = SimpleNamespace(stream=self._stream)
        self.models = SimpleNamespace(list=self._list)

    def _stream(self, **kwargs: Any) -> Any:
        return streaming(self._parse(**kwargs))

    def _list(self, **kwargs: Any) -> Any:
        self.listed = True
        if self.error is not None:
            raise self.error
        return SimpleNamespace(data=[SimpleNamespace(id="model")])

    def _parse(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        self.requests.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=_json(self.parsed))],
            id="msg_01abc",
            model="claude-sonnet-5",
            stop_reason="end_turn",
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=20,
                cache_read_input_tokens=900,
                cache_creation_input_tokens=5,
                service_tier="standard",
                output_tokens_details=SimpleNamespace(reasoning_tokens=12),
            ),
        )


class SearchingAnthropic(FakeAnthropic):
    """Answer with a completed server-tool search beside the structured text."""

    def _parse(self, **kwargs: Any) -> Any:
        response = super()._parse(**kwargs)
        response.content = [
            SimpleNamespace(
                type="server_tool_use",
                id="srvtoolu_01",
                name="web_search",
                input={"query": "staff platform engineer toronto"},
            ),
            SimpleNamespace(
                type="web_search_tool_result",
                tool_use_id="srvtoolu_01",
                content=[
                    SimpleNamespace(
                        type="web_search_result",
                        url="https://boards.example.test/1",
                        title="Staff Platform Engineer",
                        page_age="April 30, 2026",
                        encrypted_content=SECRET_PASSAGE,
                    )
                ],
            ),
            *response.content,
        ]
        return response


class PausingAnthropic(FakeAnthropic):
    """Pause one server-tool turn, then return the structured answer."""

    def __init__(self) -> None:
        super().__init__()
        self.paused_content = [
            SimpleNamespace(
                type="server_tool_use",
                id="srvtoolu_01abc",
                name="web_search",
                input={"query": "current openings"},
            )
        ]

    def _parse(self, **kwargs: Any) -> Any:
        if not self.requests:
            self.kwargs = kwargs
            self.requests.append(kwargs)
            return SimpleNamespace(
                content=self.paused_content,
                id="msg_pause",
                model="claude-sonnet-5",
                stop_reason="pause_turn",
                usage=SimpleNamespace(
                    input_tokens=7,
                    output_tokens=3,
                    cache_read_input_tokens=0,
                    cache_creation_input_tokens=0,
                    service_tier="standard",
                    output_tokens_details=SimpleNamespace(reasoning_tokens=0),
                ),
            )
        return super()._parse(**kwargs)


class InterruptedAnthropic(PausingAnthropic):
    """Pause one server-tool turn, then lose the connection carrying the continuation."""

    def _parse(self, **kwargs: Any) -> Any:
        import httpx2
        from anthropic import APIConnectionError

        if self.requests:
            raise APIConnectionError(
                request=httpx2.Request("POST", "https://api.example.test/messages")
            )
        return super()._parse(**kwargs)


class _FakeStream:
    """Stand in for the SDK's typed events over one finished answer."""

    def __init__(self, response: Any) -> None:
        self._response = response

    def __iter__(self) -> Iterator[Any]:
        yield SimpleNamespace(type="response.completed", response=self._response)


class FakeOpenAI:
    def __init__(self, parsed: Any = MISSING, error: Exception | None = None) -> None:
        self.parsed = Answer(value="ok") if parsed is MISSING else parsed
        self.error = error
        self.kwargs: dict[str, Any] = {}
        self.listed = False
        self.responses = SimpleNamespace(create=self._create)
        self.models = SimpleNamespace(list=self._list)

    def with_options(self, **kwargs: Any) -> Any:
        return self

    def _create(self, **kwargs: Any) -> Any:
        return _FakeStream(self._parse(**kwargs))

    def _list(self, **kwargs: Any) -> Any:
        self.listed = True
        if self.error is not None:
            raise self.error
        return SimpleNamespace(data=[SimpleNamespace(id="model")])

    def _parse(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        # Shaped like a real Responses object, including the fields clearscout
        # reads to decide whether the answer is usable at all.
        return SimpleNamespace(
            output_text=_json(self.parsed),
            id="resp_01abc",
            model="gpt-5.6-terra",
            status="completed",
            incomplete_details=None,
            service_tier="default",
            output=[],
            usage=SimpleNamespace(
                input_tokens=1000,
                output_tokens=20,
                total_tokens=1020,
                input_tokens_details=SimpleNamespace(cached_tokens=900, cache_write_tokens=0),
                output_tokens_details=SimpleNamespace(reasoning_tokens=12),
            ),
        )


class SearchingOpenAI(FakeOpenAI):
    """Answer with a completed web_search_call beside the structured text."""

    def _parse(self, **kwargs: Any) -> Any:
        response = super()._parse(**kwargs)
        response.output = [
            SimpleNamespace(
                type="web_search_call",
                id="ws_01",
                status="completed",
                action=SimpleNamespace(
                    type="search",
                    query="staff platform engineer toronto",
                    sources=[
                        SimpleNamespace(
                            url="https://boards.example.test/1",
                            title="Staff Platform Engineer",
                        )
                    ],
                ),
            )
        ]
        return response


class TerminalEventsOpenAI(FakeOpenAI):
    """Expose the terminal response without an accumulating stream helper."""

    def __init__(self) -> None:
        super().__init__()
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Iterator[Any]:
        response = self._parse(**kwargs)
        return iter(
            [
                SimpleNamespace(type="response.output_text.delta", output_index=1),
                SimpleNamespace(type="response.completed", response=response),
            ]
        )


class CitingOpenAI(SearchingOpenAI):
    """Attach explicit search citations and one call count to the fake search."""

    def _parse(self, **kwargs: Any) -> Any:
        response = super()._parse(**kwargs)
        for item in response.output or []:
            action = getattr(item, "action", None)
            if getattr(item, "type", "") == "web_search_call" and action is not None:
                action.citations = [{"id": "o1"}, "openai-citation-2"]
        return response


class UnnamedCitationOpenAI(SearchingOpenAI):
    """Cite one identifier and one vendor shape that carries a passage instead of an id."""

    def _parse(self, **kwargs: Any) -> Any:
        response = super()._parse(**kwargs)
        for item in response.output or []:
            action = getattr(item, "action", None)
            if getattr(item, "type", "") == "web_search_call" and action is not None:
                action.citations = [
                    {"id": "cit_01"},
                    SimpleNamespace(cited_text=SECRET_PASSAGE, url="https://boards.example.test/1"),
                ]
        return response


class CitingAnthropic(SearchingAnthropic):
    """Attach explicit search citations to one tool use and one result."""

    def _parse(self, **kwargs: Any) -> Any:
        response = super()._parse(**kwargs)
        for block in response.content or []:
            if getattr(block, "type", "") == "server_tool_use":
                block.citations = [{"citation_id": "a1"}, "anthropic-citation-2"]
            elif getattr(block, "type", "") == "web_search_tool_result":
                block.citations = [{"id": "a-result-citation"}]
                results = getattr(block, "content", None)
                if isinstance(results, list) and results:
                    results[0].citations = [{"id": "a-source-citation"}]
        return response


def test_the_whole_anthropic_request_is_rendered_from_the_plan() -> None:
    """Every field of a Messages request comes from a named part of the plan.

    Pinning the whole shape makes any change to what is sent visible here.
    """
    client = FakeAnthropic()
    provider = AnthropicProvider(api_key="x", client=cast(Any, client))

    provider.complete(
        plan_for("instructions", "ask", evidence="record", max_tokens=1234), Answer, CHOICE
    )

    assert set(client.kwargs) == {
        "model",
        "max_tokens",
        "output_config",
        "system",
        "messages",
    }
    assert client.kwargs["model"] == "model"
    assert client.kwargs["max_tokens"] == 1234
    assert client.kwargs["system"] == [
        {"type": "text", "text": kernel()},
        {"type": "text", "text": "instructions"},
    ]
    assert client.kwargs["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "record", "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": "ask"},
            ],
        }
    ]


def test_openai_takes_the_terminal_response_without_accumulating_partial_events() -> None:
    provider = OpenAIProvider(api_key="x", client=cast(Any, TerminalEventsOpenAI()))

    result, _ = provider.complete(plan_for(), Answer, CHOICE)

    assert result == Answer(value="ok")


def test_the_whole_openai_request_is_rendered_from_the_plan() -> None:
    """The same pin for the Responses API."""
    client = FakeOpenAI()
    provider = OpenAIProvider(api_key="x", client=cast(Any, client))

    provider.complete(
        plan_for("instructions", "ask", evidence="record", max_tokens=1234), Answer, CHOICE
    )

    assert set(client.kwargs) == {
        "model",
        "reasoning",
        "input",
        "text",
        "max_output_tokens",
        "prompt_cache_options",
        "service_tier",
        "store",
        "stream",
    }
    assert client.kwargs["input"] == [
        {
            "role": "developer",
            "content": [
                {
                    "type": "input_text",
                    "text": f"{kernel()}\n\ninstructions",
                    "prompt_cache_breakpoint": {"mode": "explicit"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": "record",
                    "prompt_cache_breakpoint": {"mode": "explicit"},
                },
                {"type": "input_text", "text": "ask"},
            ],
        },
    ]
    assert client.kwargs["prompt_cache_options"] == {"mode": "explicit"}
    assert client.kwargs["max_output_tokens"] == 1234
    assert client.kwargs["store"] is False
    assert client.kwargs["stream"] is True


def test_a_request_with_no_record_caches_its_instructions_and_never_its_ask() -> None:
    """Implicit mode would write the posting each call sends once; explicit mode writes none."""
    client = FakeOpenAI()
    provider = OpenAIProvider(api_key="x", client=cast(Any, client))

    provider.complete(plan_for("instructions", "a posting read once"), Answer, CHOICE)

    developer, user = client.kwargs["input"]
    assert client.kwargs["prompt_cache_options"] == {"mode": "explicit"}
    assert developer["content"][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
    assert user["content"] == [{"type": "input_text", "text": "a posting read once"}]


@pytest.mark.parametrize(
    ("provider_class", "fake", "body", "marker"),
    [
        (AnthropicProvider, FakeAnthropic, "messages", "cache_control"),
        (OpenAIProvider, FakeOpenAI, "input", "prompt_cache_breakpoint"),
    ],
)
def test_a_single_relevance_review_batch_marks_its_evidence_for_the_next_task(
    provider_class, fake, body, marker
) -> None:
    """A later task over the same CV and parameters reads the evidence back from the cache."""
    client = fake()
    plan = review_plan(Settings(), synthetic_inventory(), "Remote only.", "DISCOVERY pd-1")

    provider_class(api_key="x", client=cast(Any, client)).complete(plan, Answer, CHOICE)

    evidence, task = client.kwargs[body][-1]["content"]
    assert plan.evidence is not None
    assert (evidence["text"], marker in evidence) == (plan.evidence.text, True)
    assert marker not in task


@pytest.mark.parametrize(
    ("provider_class", "fake", "body", "marker", "mark"),
    [
        (AnthropicProvider, FakeAnthropic, "messages", "cache_control", {"type": "ephemeral"}),
        (OpenAIProvider, FakeOpenAI, "input", "prompt_cache_breakpoint", {"mode": "explicit"}),
    ],
)
def test_repeated_draws_read_the_task_from_the_cache_as_well(
    provider_class, fake, body, marker, mark
) -> None:
    """The second and later draws of an identical request pay the cached rate for the task."""
    client = fake()
    plan = replace(plan_for("instructions", "ask", evidence="record"), draws=3)

    provider_class(api_key="x", client=cast(Any, client)).complete(plan, Answer, CHOICE)

    blocks = client.kwargs[body][-1]["content"]
    assert [(block["text"], block.get(marker)) for block in blocks] == [
        ("record", mark),
        ("ask", mark),
    ]


@pytest.mark.parametrize(
    ("provider_class", "fake", "body", "marker", "mark"),
    [
        (AnthropicProvider, FakeAnthropic, "messages", "cache_control", {"type": "ephemeral"}),
        (OpenAIProvider, FakeOpenAI, "input", "prompt_cache_breakpoint", {"mode": "explicit"}),
    ],
)
@pytest.mark.parametrize("hinted", [True, False])
def test_a_single_draw_caches_its_task_only_when_the_request_will_repeat(
    provider_class, fake, body, marker, mark, hinted
) -> None:
    """A repeated trial reads the task back; a single production draw writes no task cache."""
    client = fake()
    plan = replace(plan_for("instructions", "ask", evidence="record"), repeat_hint=hinted)

    provider_class(api_key="x", client=cast(Any, client)).complete(plan, Answer, CHOICE)

    blocks = client.kwargs[body][-1]["content"]
    assert [(block["text"], block.get(marker)) for block in blocks] == [
        ("record", mark),
        ("ask", mark if hinted else None),
    ]


@pytest.mark.parametrize(
    ("provider_class", "fake"), [(AnthropicProvider, FakeAnthropic), (OpenAIProvider, FakeOpenAI)]
)
def test_a_candidate_record_never_reaches_instruction_authority(provider_class, fake) -> None:
    """A CV may have come from anywhere, and both vendors obey operator content.

    Placing it in OpenAI's `instructions` or Anthropic's `system` would give
    somebody's employment history the highest authority in a drafting request.
    """
    client = fake()
    provider_class(api_key="x", client=cast(Any, client)).complete(
        plan_for(evidence="a poisoned record"), Answer, CHOICE
    )

    operator = client.kwargs.get("system") or [
        message for message in client.kwargs["input"] if message["role"] != "user"
    ]
    assert "a poisoned record" not in json.dumps(operator)


def test_anthropic_reports_cached_tokens_separately() -> None:
    provider = AnthropicProvider(api_key="x", client=cast(Any, FakeAnthropic()))
    _, record = provider.complete(plan_for(), Answer, CHOICE)
    usage = record.usage
    assert usage.input_tokens == 100
    assert usage.cache_read_tokens == 900
    assert usage.cache_write_tokens == 5


def test_anthropic_web_search_keeps_structured_output_and_resumes_a_pause() -> None:
    client = PausingAnthropic()
    provider = AnthropicProvider(api_key="x", client=cast(Any, client))

    answer, record = provider.complete_with_web_search(
        plan_for(search=SearchPolicy()), Answer, CHOICE
    )

    assert answer.value == "ok"
    assert len(client.requests) == 2
    first, continuation = client.requests
    expected_tool = {
        "type": "web_search_20260209",
        "name": "web_search",
        "max_uses": 5,
        # Named rather than defaulted: this tool version otherwise runs the
        # search from inside code execution, on a different retention path.
        "allowed_callers": ["direct"],
    }
    assert first["tools"] == [expected_tool]
    assert continuation["tools"] == [expected_tool]
    assert continuation["output_config"] == first["output_config"]
    assert [message["role"] for message in continuation["messages"]] == ["user", "assistant"]
    assert continuation["messages"][1]["content"] is client.paused_content
    assert record.usage.input_tokens == 107
    assert record.usage.output_tokens == 23
    assert record.usage.cache_read_tokens == 900


def test_anthropic_searches_directly_rather_than_through_code_execution() -> None:
    """The caller is stated because this tool version's default is not it.

    `web_search_20260209` and later default `allowed_callers` to code execution,
    which provisions a second server tool under another feature's retention terms.
    """
    client = FakeAnthropic()
    provider = AnthropicProvider(api_key="x", client=cast(Any, client))

    provider.complete_with_web_search(plan_for(search=SearchPolicy()), Answer, CHOICE)

    assert client.kwargs["tools"][0]["allowed_callers"] == ["direct"]


def test_anthropic_web_search_caps_repeated_pause_turns() -> None:
    from clearscout.infrastructure.providers.base import UnusableResponseError

    client = anthropic_saying("pause_turn")
    original = client.messages.stream
    requests: list[dict[str, Any]] = []

    def counted(**kwargs: Any) -> Any:
        requests.append(kwargs)
        return original(**kwargs)

    client.messages.stream = counted
    provider = AnthropicProvider(api_key="x", client=cast(Any, client))

    with pytest.raises(
        UnusableResponseError, match="paused its web-search turn too many times"
    ) as raised:
        provider.complete_with_web_search(plan_for(search=SearchPolicy()), Answer, CHOICE)

    assert len(requests) == 4
    assert raised.value.record.usage.input_tokens == 400
    assert raised.value.record.usage.output_tokens == 16000
    assert raised.value.record.usage_known is True


def test_anthropic_continuation_failure_retains_returned_usage(settings) -> None:
    from clearscout.infrastructure.providers.base import UnusableResponseError
    from clearscout.infrastructure.providers.cache import ResponseCache
    from clearscout.infrastructure.providers.client import Client
    from clearscout.shared.costs import Ledger

    ledger = Ledger()
    with ResponseCache(enabled=False) as cache:
        client = Client(
            AnthropicProvider("test", client=cast(Any, InterruptedAnthropic())),
            settings,
            cache,
            ledger,
        )
        with pytest.raises(UnusableResponseError):
            client.complete(plan_for(search=SearchPolicy()), Answer)
    assert len(ledger.entries) == 1
    assert ledger.entries[0].usage.input_tokens == 7
    assert ledger.entries[0].usage.output_tokens == 3
    assert ledger.entries[0].response_id == "msg_pause"
    assert ledger.entries[0].validated is False
    assert ledger.entries[0].dollars is None


@pytest.mark.parametrize("adapter", [OpenAIProvider, AnthropicProvider])
def test_a_stated_wait_reaches_the_client_the_adapter_builds(adapter: Any) -> None:
    """The flex floor buys nothing if it stops at the adapter's own signature."""
    stated = adapter(api_key="x", timeout=FLEX_TIMEOUT_FLOOR)
    unstated = adapter(api_key="x")

    assert stated._client.timeout == FLEX_TIMEOUT_FLOOR
    assert unstated._client.timeout != FLEX_TIMEOUT_FLOOR


def test_anthropic_sends_images_before_the_text() -> None:
    client = FakeAnthropic()
    provider = AnthropicProvider(api_key="x", client=cast(Any, client))
    plan = plan_for(images=(Image(data=b"\x89PNG", media_type="image/png"),))
    provider.complete(plan, Answer, CHOICE)
    content = client.kwargs["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[-1]["type"] == "text"


def test_openai_subtracts_cached_tokens_so_the_columns_mean_the_same_thing() -> None:
    provider = OpenAIProvider(api_key="x", client=cast(Any, FakeOpenAI()))
    _, record = provider.complete(plan_for(), Answer, CHOICE)
    usage = record.usage
    assert usage.input_tokens == 100
    assert usage.cache_read_tokens == 900


def test_openai_sends_images_as_data_urls() -> None:
    client = FakeOpenAI()
    provider = OpenAIProvider(api_key="x", client=cast(Any, client))
    plan = plan_for(images=(Image(data=b"\x89PNG", media_type="image/png"),))
    provider.complete(plan, Answer, CHOICE)
    content = client.kwargs["input"][-1]["content"]
    assert content[0]["image_url"].startswith("data:image/png;base64,")


def test_openai_states_the_detail_and_verbosity_the_stage_chose() -> None:
    client = FakeOpenAI()
    provider = OpenAIProvider(api_key="x", client=cast(Any, client))
    plan = plan_for(images=(Image(data=b"\x89PNG", media_type="image/png"),))
    choice = ModelChoice(model="model", image_detail=ImageDetail.ORIGINAL, verbosity=Verbosity.LOW)

    provider.complete(plan, Answer, choice)

    assert client.kwargs["input"][-1]["content"][0]["detail"] == "original"
    assert client.kwargs["text"]["verbosity"] == "low"


def test_openai_asks_a_relevance_review_batch_for_low_verbosity() -> None:
    client = FakeOpenAI()
    provider = OpenAIProvider(api_key="x", client=cast(Any, client))
    settings = Settings(provider=ProviderName.OPENAI)
    plan = review_plan(settings, synthetic_inventory(), "Remote only.", "DISCOVERY p1")

    provider.complete(plan, Answer, settings.choice_for_stage(Stage.RELEVANCE_REVIEW))

    assert client.kwargs["text"]["verbosity"] == "low"


def test_the_jobspec_request_asks_openai_for_low_verbosity() -> None:
    client = FakeOpenAI()
    provider = OpenAIProvider(api_key="x", client=cast(Any, client))
    choice = Settings(provider=ProviderName.OPENAI).choice_for_stage(Stage.JOBSPEC)

    provider.complete(plan_for(stage=Stage.JOBSPEC), Answer, choice)

    assert client.kwargs["text"]["verbosity"] == "low"


def test_openai_leaves_out_a_control_the_stage_did_not_choose() -> None:
    """An unstated control leaves the vendor default rather than one invented here."""
    client = FakeOpenAI()
    provider = OpenAIProvider(api_key="x", client=cast(Any, client))
    plan = plan_for(images=(Image(data=b"\x89PNG", media_type="image/png"),))

    provider.complete(plan, Answer, CHOICE)

    assert "detail" not in client.kwargs["input"][-1]["content"][0]
    assert "verbosity" not in client.kwargs["text"]


def test_anthropic_sends_neither_control_because_it_publishes_neither() -> None:
    client = FakeAnthropic()
    provider = AnthropicProvider(api_key="x", client=cast(Any, client))
    plan = plan_for(images=(Image(data=b"\x89PNG", media_type="image/png"),))
    choice = ModelChoice(model="model", image_detail=ImageDetail.ORIGINAL, verbosity=Verbosity.LOW)

    provider.complete(plan, Answer, choice)

    sent = json.dumps(client.kwargs, default=str)
    assert "original" not in sent
    assert "verbosity" not in sent


def test_openai_web_search_keeps_strict_structured_output_and_caps_tool_calls() -> None:
    client = FakeOpenAI()
    provider = OpenAIProvider(api_key="x", client=cast(Any, client))

    answer, _ = provider.complete_with_web_search(plan_for(search=SearchPolicy()), Answer, CHOICE)

    assert answer.value == "ok"
    assert client.kwargs["tools"] == [
        {"type": "web_search", "search_context_size": "medium", "external_web_access": True}
    ]
    assert client.kwargs["max_tool_calls"] == 5
    output_format = client.kwargs["text"]["format"]
    assert output_format["type"] == "json_schema"
    assert output_format["strict"] is True
    assert output_format["schema"]["properties"]["value"]["type"] == "string"


def test_one_search_policy_reaches_each_vendor_in_its_own_shape() -> None:
    """The same policy, and neither adapter loses a control the other lacks."""
    policy = SearchPolicy(
        max_calls=2,
        allowed_domains=("boards.greenhouse.io",),
        location=SearchLocation(country="CA", city="Toronto"),
        depth=SearchDepth.HIGH,
        live=False,
    )
    location = {"type": "approximate", "country": "CA", "city": "Toronto"}

    openai_client = FakeOpenAI()
    OpenAIProvider(api_key="x", client=cast(Any, openai_client)).complete_with_web_search(
        plan_for(search=policy), Answer, CHOICE
    )
    anthropic_client = FakeAnthropic()
    AnthropicProvider(api_key="x", client=cast(Any, anthropic_client)).complete_with_web_search(
        plan_for(search=policy), Answer, CHOICE
    )

    assert openai_client.kwargs["tools"] == [
        {
            "type": "web_search",
            "search_context_size": "high",
            "external_web_access": False,
            "filters": {"allowed_domains": ["boards.greenhouse.io"]},
            "user_location": location,
        }
    ]
    assert openai_client.kwargs["max_tool_calls"] == 2
    assert anthropic_client.kwargs["tools"] == [
        {
            "type": "web_search_20260209",
            "name": "web_search",
            "max_uses": 2,
            "allowed_callers": ["direct"],
            "allowed_domains": ["boards.greenhouse.io"],
            "user_location": location,
        }
    ]


def test_a_policy_naming_both_domain_lists_is_refused_before_it_is_sent() -> None:
    """Anthropic answers that combination with a 400, and a run has to pay to find out."""
    with pytest.raises(ConfigError, match="allowed domains or blocked domains"):
        SearchPolicy(allowed_domains=("a.test",), blocked_domains=("b.test",))


def test_anthropic_reports_what_its_search_asked_and_read() -> None:
    """A search whose queries and pages are dropped cannot explain the answer it fed."""
    client = SearchingAnthropic()
    provider = AnthropicProvider(api_key="x", client=cast(Any, client))

    _, record = provider.complete_with_web_search(plan_for(search=SearchPolicy()), Answer, CHOICE)

    assert record.tool_version == "web_search_20260209"
    assert [item.queries for item in record.searches] == [("staff platform engineer toronto",)]
    assert [(source.url, source.title) for source in record.searches[0].sources] == [
        ("https://boards.example.test/1", "Staff Platform Engineer")
    ]


def test_anthropic_records_search_citations_and_call_count() -> None:
    """Citations and call count are part of search provenance."""
    client = CitingAnthropic()
    provider = AnthropicProvider(api_key="x", client=cast(Any, client))

    _, record = provider.complete_with_web_search(plan_for(search=SearchPolicy()), Answer, CHOICE)

    assert record.search_count == 1
    assert record.searches[0].citations == ("a1", "anthropic-citation-2", "a-result-citation")
    assert record.searches[0].sources[0].citations == ("a-source-citation",)


class PageActingOpenAI(FakeOpenAI):
    """Answer with a search action beside page actions the same call took."""

    def _parse(self, **kwargs: Any) -> Any:
        response = super()._parse(**kwargs)
        response.output = [
            SimpleNamespace(
                type="web_search_call",
                id="ws_01",
                status="completed",
                action=SimpleNamespace(type="search", query="staff platform engineer"),
            ),
            SimpleNamespace(
                type="web_search_call",
                id="ws_02",
                status="completed",
                action=SimpleNamespace(type="open_page", url="https://boards.example.test/2"),
            ),
            SimpleNamespace(
                type="web_search_call",
                id="ws_03",
                status="completed",
                action=SimpleNamespace(
                    type="find_in_page",
                    url="https://boards.example.test/2",
                    pattern="salary",
                ),
            ),
        ]
        return response


def test_page_action_urls_are_captured_and_search_count_excludes_them() -> None:
    """Only a `search` action counts as a search; reading a page is not one."""
    client = PageActingOpenAI()
    provider = OpenAIProvider(api_key="x", client=cast(Any, client))

    _, record = provider.complete_with_web_search(plan_for(search=SearchPolicy()), Answer, CHOICE)

    assert record.search_count == 1
    assert record.page_action_count == 2
    assert [(item.kind, item.url, item.pattern) for item in record.page_actions] == [
        ("open_page", "https://boards.example.test/2", ""),
        ("find_in_page", "https://boards.example.test/2", "salary"),
    ]


class MultiQueryOpenAI(FakeOpenAI):
    """Answer with one search action carrying several queries, and the first one again alone."""

    def _parse(self, **kwargs: Any) -> Any:
        response = super()._parse(**kwargs)
        response.output = [
            SimpleNamespace(
                type="web_search_call",
                id="ws_01",
                status="completed",
                action=SimpleNamespace(
                    type="search",
                    query="staff platform engineer",
                    queries=["staff platform engineer", "toronto remote"],
                ),
            )
        ]
        return response


def test_a_multi_query_search_action_records_each_query_separately() -> None:
    client = MultiQueryOpenAI()
    provider = OpenAIProvider(api_key="x", client=cast(Any, client))

    _, record = provider.complete_with_web_search(plan_for(search=SearchPolicy()), Answer, CHOICE)

    assert record.searches[0].queries == ("staff platform engineer", "toronto remote")


def test_openai_reports_what_its_search_asked_and_read() -> None:
    """The same record from the other vendor's output items."""
    client = SearchingOpenAI()
    provider = OpenAIProvider(api_key="x", client=cast(Any, client))

    _, record = provider.complete_with_web_search(plan_for(search=SearchPolicy()), Answer, CHOICE)

    assert record.tool_version == "web_search"
    assert [item.queries for item in record.searches] == [("staff platform engineer toronto",)]
    assert [source.url for source in record.searches[0].sources] == [
        "https://boards.example.test/1"
    ]


def test_openai_records_search_citations_and_call_count() -> None:
    """Citations and call count are part of the raw hosted-search record."""
    client = CitingOpenAI()
    provider = OpenAIProvider(api_key="x", client=cast(Any, client))

    _, record = provider.complete_with_web_search(plan_for(search=SearchPolicy()), Answer, CHOICE)

    assert record.search_count == 1
    assert record.searches[0].citations == ("o1", "openai-citation-2")
    assert [source.url for source in record.searches[0].sources] == [
        "https://boards.example.test/1"
    ]


def test_a_citation_this_cannot_name_is_left_out_rather_than_dumped() -> None:
    """A citation with no identifier is left out of the record, not serialized whole.

    The vendor shape that has no `id` is the one carrying `cited_text`, so
    serializing it would write the quoted passage into `model_calls`.
    """
    client = UnnamedCitationOpenAI()
    provider = OpenAIProvider(api_key="x", client=cast(Any, client))

    _, record = provider.complete_with_web_search(plan_for(search=SearchPolicy()), Answer, CHOICE)

    assert record.searches[0].citations == ("cit_01",)
    assert SECRET_PASSAGE not in json.dumps([item.stated() for item in record.searches])


def test_a_search_record_keeps_no_quoted_passage() -> None:
    """Provenance is where an answer came from; a quoted passage is content."""
    client = SearchingAnthropic()
    provider = AnthropicProvider(api_key="x", client=cast(Any, client))

    _, record = provider.complete_with_web_search(plan_for(search=SearchPolicy()), Answer, CHOICE)

    kept = json.dumps([item.stated() for item in record.searches])
    assert SECRET_PASSAGE not in kept
    assert "encrypted_content" not in kept


@pytest.mark.parametrize(
    ("provider_class", "fake"), [(AnthropicProvider, FakeAnthropic), (OpenAIProvider, FakeOpenAI)]
)
def test_an_empty_structured_response_is_reported_not_swallowed(provider_class, fake) -> None:
    provider = provider_class(api_key="x", client=cast(Any, fake(parsed=None)))
    with pytest.raises(ProviderError, match="no structured output"):
        provider.complete(plan_for(), Answer, CHOICE)


def test_an_image_round_trips_through_base64() -> None:
    image = Image(data=b"\x89PNG\r\n", media_type="image/png")
    assert image.data_url().startswith("data:image/png;base64,")
    assert image.data_url().endswith(image.base64())


@pytest.mark.parametrize(
    ("provider_class", "fake"), [(AnthropicProvider, FakeAnthropic), (OpenAIProvider, FakeOpenAI)]
)
def test_checking_credentials_costs_no_tokens(provider_class: Any, fake: Any) -> None:
    """Listing models proves the key works without billing a completion."""
    client = fake()
    provider_class(api_key="x", client=cast(Any, client)).check_credentials()
    assert client.listed
    assert client.kwargs == {}


def test_a_rejected_anthropic_key_reports_the_console_url() -> None:
    import httpx2
    from anthropic import APIStatusError

    error = APIStatusError(
        "unauthorized",
        response=httpx2.Response(401, request=httpx2.Request("GET", "https://api.anthropic.com")),
        body=None,
    )
    provider = AnthropicProvider(api_key="x", client=cast(Any, FakeAnthropic(error=error)))
    with pytest.raises(AuthError, match="console.anthropic.com"):
        provider.check_credentials()


def test_a_rejected_openai_key_reports_the_console_url() -> None:
    import httpx2
    from openai import APIStatusError

    error = APIStatusError(
        "unauthorized",
        response=httpx2.Response(401, request=httpx2.Request("GET", "https://api.openai.com")),
        body=None,
    )
    provider = OpenAIProvider(api_key="x", client=cast(Any, FakeOpenAI(error=error)))
    with pytest.raises(AuthError, match="platform.openai.com"):
        provider.check_credentials()


def test_anthropic_passes_effort_through_output_config() -> None:
    client = FakeAnthropic()
    AnthropicProvider(api_key="x", client=cast(Any, client)).complete(
        plan_for(), Answer, ModelChoice(model="m", effort=Effort.XHIGH)
    )
    assert client.kwargs["output_config"]["effort"] == "xhigh"
    assert client.kwargs["output_config"]["format"]["type"] == "json_schema"


def test_anthropic_omits_output_config_when_no_effort_is_set() -> None:
    client = FakeAnthropic()
    AnthropicProvider(api_key="x", client=cast(Any, client)).complete(
        plan_for(), Answer, ModelChoice(model="m")
    )
    # output_config carries the schema; what must be absent is effort.
    assert "effort" not in client.kwargs["output_config"]


def test_openai_passes_effort_through_reasoning() -> None:
    client = FakeOpenAI()
    OpenAIProvider(api_key="x", client=cast(Any, client)).complete(
        plan_for(), Answer, ModelChoice(model="m", effort=Effort.LOW)
    )
    assert client.kwargs["reasoning"] == {"effort": "low"}


def test_openai_states_a_discounted_tier_and_omits_the_standard_one() -> None:
    client = FakeOpenAI()
    provider = OpenAIProvider(api_key="x", client=cast(Any, client))

    provider.complete(plan_for(), Answer, ModelChoice(model="m"))
    assert client.kwargs["service_tier"] == "default"

    provider.complete(plan_for(), Answer, ModelChoice(model="m", service_tier=ServiceTier.FLEX))
    assert client.kwargs["service_tier"] == "flex"


@pytest.mark.parametrize(
    ("provider_class", "fake"), [(AnthropicProvider, FakeAnthropic), (OpenAIProvider, FakeOpenAI)]
)
def test_available_models_reports_what_the_account_can_use(provider_class: Any, fake: Any) -> None:
    provider = provider_class(api_key="x", client=cast(Any, fake()))
    assert provider.available_models() == frozenset({"model"})


@pytest.mark.parametrize(
    ("provider_class", "fake"), [(AnthropicProvider, FakeAnthropic), (OpenAIProvider, FakeOpenAI)]
)
def test_a_silent_listing_is_none_not_an_empty_set(provider_class: Any, fake: Any) -> None:
    """None means the provider would not say, which must not read as missing."""
    client = fake()
    client.models = SimpleNamespace(list=lambda **kwargs: SimpleNamespace(data=[]))
    assert provider_class(api_key="x", client=cast(Any, client)).available_models() is None


def _json(parsed: Any) -> str:
    """Render what a model would actually have put on the wire."""
    if parsed is None:
        return ""
    if isinstance(parsed, BaseModel):
        return parsed.model_dump_json()
    return str(parsed)


def anthropic_saying(stop_reason: str, reasoning: int = 0) -> Any:
    """Return a fake Anthropic client whose message stopped for a given reason."""

    def parse(**kwargs: Any) -> Any:
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text='{"value": "partial')],
            id="msg_01abc",
            model="claude-sonnet-5",
            stop_reason=stop_reason,
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=4000,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
                service_tier="standard",
                output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning),
            ),
        )

    return SimpleNamespace(
        messages=SimpleNamespace(stream=lambda **kwargs: streaming(parse(**kwargs)))
    )


def openai_saying(status: str, reason: str = "", reasoning: int = 0) -> Any:
    """Return a fake OpenAI client whose response ended in a given state."""

    def parse(**kwargs: Any) -> Any:
        return SimpleNamespace(
            output_text='{"value": "partial',
            id="resp_01abc",
            model="gpt-5.6-terra",
            status=status,
            incomplete_details=SimpleNamespace(reason=reason) if reason else None,
            service_tier="default",
            output=[],
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=12000,
                input_tokens_details=SimpleNamespace(cached_tokens=0, cache_write_tokens=0),
                output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning),
            ),
        )

    return SimpleNamespace(
        responses=SimpleNamespace(create=lambda **kwargs: _FakeStream(parse(**kwargs)))
    )


def test_a_truncated_openai_response_says_so_and_names_the_reasoning_cost() -> None:
    """The most expensive silent failure: a ceiling spent entirely on thinking.

    Reasoning is billed as output and counted inside it, so a drafting call at
    high effort can consume the whole budget and return nothing.
    """
    provider = OpenAIProvider(
        api_key="x", client=cast(Any, openai_saying("incomplete", "max_output_tokens", 11800))
    )
    with pytest.raises(ProviderError) as caught:
        provider.complete(plan_for(max_tokens=12000), Answer, CHOICE)
    message = str(caught.value)
    assert "12,000 token output limit" in message
    assert "11,800 of them on reasoning" in message
    assert "200 for the answer" in message
    assert "Raise max_tokens" in message


def test_a_truncated_anthropic_response_says_the_same_thing() -> None:
    provider = AnthropicProvider(api_key="x", client=cast(Any, anthropic_saying("max_tokens")))
    with pytest.raises(ProviderError, match="output limit"):
        provider.complete(plan_for(max_tokens=4000), Answer, CHOICE)


def test_a_refusal_is_not_reported_as_something_a_retry_would_fix() -> None:
    """A refusal and a truncation need different remedies, so they read apart."""
    provider = AnthropicProvider(api_key="x", client=cast(Any, anthropic_saying("refusal")))
    with pytest.raises(ProviderError) as caught:
        provider.complete(plan_for(), Answer, CHOICE)
    assert "retrying will not help" in str(caught.value)


def test_a_content_filter_is_distinguished_from_a_token_ceiling() -> None:
    provider = OpenAIProvider(
        api_key="x", client=cast(Any, openai_saying("incomplete", "content_filter"))
    )
    with pytest.raises(ProviderError, match="content filter"):
        provider.complete(plan_for(), Answer, CHOICE)


def test_a_failed_response_is_blamed_on_the_provider() -> None:
    provider = OpenAIProvider(api_key="x", client=cast(Any, openai_saying("failed")))
    with pytest.raises(ProviderError, match="provider's end"):
        provider.complete(plan_for(), Answer, CHOICE)


def test_reasoning_tokens_are_carried_but_never_double_counted() -> None:
    """Both vendors report reasoning inside output_tokens, not beside it."""
    from clearscout.shared.costs import Usage

    usage = Usage(input_tokens=100, output_tokens=4000, reasoning_tokens=3200)
    assert usage.answer_tokens == 800
    assert usage.total_tokens == 4100, "reasoning must not be added to the total"


def test_a_truncated_response_never_escapes_as_a_validation_error() -> None:
    """A response cut off at the token ceiling reaches `refuse_unusable`.

    Both SDKs' `parse` helpers validate inside the call, so a response cut off at
    the token ceiling raises `pydantic.ValidationError` rather than an `APIError`
    and flies past `refuse_unusable`. Both adapters call `create` instead, so the
    record is inspected before anything tries to parse partial JSON.
    """
    for provider, client in (
        (AnthropicProvider, anthropic_saying("max_tokens", reasoning=3800)),
        (OpenAIProvider, openai_saying("incomplete", "max_output_tokens", 3800)),
    ):
        instance = provider(api_key="x", client=cast(Any, client))
        with pytest.raises(ProviderError) as caught:
            instance.complete(plan_for(max_tokens=4000), Answer, CHOICE)
        assert "output limit" in str(caught.value)
        assert "3,800" in str(caught.value)


def test_both_providers_are_sent_the_same_schema_with_the_same_contract() -> None:
    """Every property is required and numeric bounds survive, for both vendors.

    The two SDK helpers treat `required` and numeric bounds differently, so an
    optional list would be optional on one provider and mandatory on the other.
    A critic omitting `unsupported_claims` would read as "everything is supported".
    """
    from pydantic import Field

    from clearscout.infrastructure.providers.base import json_schema

    class Verdict(BaseModel):
        score: int = Field(ge=1, le=5)
        claims: list[str] = Field(default_factory=list)

    schema = json_schema(Verdict)
    assert schema["required"] == ["score", "claims"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["score"]["minimum"] == 1
    assert "default" not in schema["properties"]["claims"]


def test_an_unrecognised_stop_reason_is_named_rather_than_reported_as_a_schema_fault() -> None:
    """Both adapters can produce `UNKNOWN`, and it is refused before validation.

    Anthropic answers `pause_turn` and OpenAI answers `incomplete` with a reason
    this version has never seen. Neither is a finished object, so parsing the
    partial JSON would report a misleading schema mismatch.
    """
    for provider, client in (
        (AnthropicProvider, anthropic_saying("pause_turn")),
        (OpenAIProvider, openai_saying("incomplete", "some_new_reason", 0)),
    ):
        instance = provider(api_key="x", client=cast(Any, client))
        with pytest.raises(ProviderError) as caught:
            instance.complete(plan_for(max_tokens=4000), Answer, CHOICE)
        message = str(caught.value)
        assert "does not recognise" in message
        assert "schema" not in message


def test_the_finish_vocabulary_is_normalized_but_not_symmetric() -> None:
    """A value the enum can express is not a value both vendors can send.

    `filtered` and `failed` are Responses API states with no Anthropic stop
    reason behind them, so the OpenAI adapter can produce them and the Anthropic
    adapter never can.
    """
    from clearscout.infrastructure.providers.anthropic import _finish as anthropic_finish
    from clearscout.infrastructure.providers.base import Finish
    from clearscout.infrastructure.providers.openai import _finish as openai_finish

    anthropic_reasons = [
        None,
        "end_turn",
        "stop_sequence",
        "tool_use",
        "max_tokens",
        "model_context_window_exceeded",
        "refusal",
        "pause_turn",
        "a_reason_this_version_does_not_know",
    ]
    reachable = {anthropic_finish(reason) for reason in anthropic_reasons}
    assert Finish.FILTERED not in reachable
    assert Finish.FAILED not in reachable
    # The rest of the vocabulary is reachable, so the exclusion above is about
    # these two values rather than about a mapping that answers one way.
    assert reachable == {Finish.COMPLETE, Finish.TRUNCATED, Finish.REFUSED, Finish.UNKNOWN}

    assert openai_finish(SimpleNamespace(status="failed")) is Finish.FAILED
    assert (
        openai_finish(
            SimpleNamespace(
                status="incomplete",
                incomplete_details=SimpleNamespace(reason="content_filter"),
            )
        )
        is Finish.FILTERED
    )


def test_one_reader_pulls_model_identifiers_out_of_either_vendors_listing() -> None:
    """Both adapters read model identifiers through one shared function."""
    from clearscout.infrastructure.providers.base import model_identifiers

    listing = SimpleNamespace(data=[SimpleNamespace(id="a"), SimpleNamespace(id="b")])
    assert model_identifiers(listing) == frozenset({"a", "b"})
    assert model_identifiers(SimpleNamespace(data=[])) is None
    assert model_identifiers(SimpleNamespace()) is None


def test_a_nested_object_is_made_strict_too() -> None:
    """Strictness has to reach every object, not just the top one."""
    from clearscout.infrastructure.providers.base import json_schema

    class Inner(BaseModel):
        a: str
        b: str = "x"

    class Outer(BaseModel):
        inner: Inner

    schema = json_schema(Outer)
    inner = schema["$defs"]["Inner"]
    assert inner["required"] == ["a", "b"]
    assert inner["additionalProperties"] is False


def test_a_repeated_vocabulary_is_emitted_once() -> None:
    """Pydantic inlines a Literal at every use site, and these are huge.

    The drafting schema carries the same entry-id vocabulary in three places and
    the import schema carries its line-number vocabulary in two, against provider
    limits that count enum values across the whole document. Measured rather than
    read, because a share that references itself destroys the vocabulary the
    anti-fabrication guarantee rests on and still looks correct.
    """
    from typing import Literal

    from pydantic import create_model

    from clearscout.infrastructure.providers.base import json_schema

    # Two distinct models sharing one vocabulary, as the drafting schema has. One
    # model used twice would not show the problem, since pydantic already
    # references a repeated model.
    ids = tuple(f"b-{index:04x}" for index in range(50))
    literal = Literal[ids]  # type: ignore[valid-type]
    line = create_model("L", text=(str, ...), source_ids=(list[literal], ...))
    skill = create_model("S", label=(str, ...), source_ids=(list[literal], ...))
    # Same reason as the Literal above: these models exist only at runtime, so
    # a static checker cannot treat one as a type.
    document = create_model("D", summary=(line, ...), skills=(list[skill], ...))  # type: ignore[valid-type]

    schema = json_schema(document)
    shared = [name for name in schema["$defs"] if name.startswith("clearscout_enum")]
    assert len(shared) == 1
    # The vocabulary survives intact, and is not a reference to itself.
    assert schema["$defs"][shared[0]]["enum"] == list(ids)

    inline = 0

    def count(node: object) -> None:
        nonlocal inline
        if isinstance(node, list):
            for item in node:
                count(item)
        elif isinstance(node, dict):
            if isinstance(node.get("enum"), list):
                inline += 1
            for value in node.values():
                count(value)

    count(schema)
    assert inline == 1, "the vocabulary should appear exactly once"


@pytest.fixture
def no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Build the SDK client the way a machine with no proxy configured would.

    Both SDKs read the standard proxy variables when they construct a transport.
    """
    for name in ("ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)


def test_an_adapter_builds_a_client_with_the_retry_count_it_declares(no_proxy: None) -> None:
    """The SDK default of two retries silently multiplies the configured timeout.

    300 seconds becomes 900 in the worst case, and somebody watching a stalled
    run should be able to work that out from what the program asked for.
    """
    from clearscout.infrastructure.providers.anthropic import AnthropicProvider
    from clearscout.infrastructure.providers.openai import OpenAIProvider

    anthropic = AnthropicProvider(api_key="not-a-real-key", timeout=300.0)
    openai = OpenAIProvider(api_key="not-a-real-key", timeout=300.0)
    assert anthropic._client.max_retries == 2
    assert openai._client.max_retries == 2
    assert anthropic._client.timeout == 300.0
    assert openai._client.timeout == 300.0


def test_no_configured_timeout_leaves_the_sdk_default_alone(no_proxy: None) -> None:
    """None means "use the SDK default", not "wait forever"."""
    from anthropic import Anthropic
    from openai import OpenAI

    from clearscout.infrastructure.providers.anthropic import AnthropicProvider
    from clearscout.infrastructure.providers.openai import OpenAIProvider

    assert (
        AnthropicProvider(api_key="not-a-real-key")._client.timeout
        == Anthropic(api_key="not-a-real-key").timeout
    )
    assert (
        OpenAIProvider(api_key="not-a-real-key")._client.timeout
        == OpenAI(api_key="not-a-real-key").timeout
    )


@pytest.mark.parametrize("vendor", ["openai", "anthropic"])
@pytest.mark.parametrize("damage", ["missing", "none", "negative", "bool", "text", "reasoning"])
def test_incomplete_usage_keeps_answers_and_continuation_unpriced(vendor, damage, settings):
    from clearscout.infrastructure.providers.cache import ResponseCache
    from clearscout.infrastructure.providers.client import Client
    from clearscout.shared.costs import Ledger, ModelPrice

    sdk: Any = FakeOpenAI() if vendor == "openai" else FakeAnthropic()
    parse = sdk._parse

    def damaged_response(**kwargs):
        response = parse(**kwargs)
        if damage == "missing":
            del response.usage
        elif damage == "none":
            response.usage = None
        elif damage == "reasoning":
            response.usage.output_tokens_details.reasoning_tokens = 2000
        else:
            response.usage.input_tokens = {"negative": -1, "bool": True, "text": "100"}[damage]
        return response

    sdk._parse = damaged_response
    provider = (
        OpenAIProvider("test", client=cast(Any, sdk))
        if vendor == "openai"
        else AnthropicProvider("test", client=cast(Any, sdk))
    )
    served = "gpt-5.6-terra" if vendor == "openai" else "claude-sonnet-5"
    ledger = Ledger(prices={served: ModelPrice(input=1, output=1)})
    with ResponseCache(enabled=False) as cache:
        client = Client(provider, settings, cache, ledger)
        for index in range(2):
            assert client.complete(plan_for(user=str(index)), Answer).value == "ok"
    assert len(ledger.entries) == 2
    assert all(entry.validated and entry.response_id for entry in ledger.entries)
    assert all(entry.dollars is None for entry in ledger.entries)
    assert ledger.total_dollars is None


@pytest.mark.parametrize("served_price", [None, 20])
def test_returned_model_prices_the_answer_without_rejecting_it(settings, served_price):
    from clearscout.infrastructure.providers.cache import ResponseCache
    from clearscout.infrastructure.providers.client import Client
    from clearscout.shared.costs import Ledger, ModelPrice

    class SubstitutedModel(FakeOpenAI):
        def _parse(self, **kwargs):
            response = super()._parse(**kwargs)
            response.model = "served-synthetic"
            return response

    prices = {"gpt-5.6-terra": ModelPrice(input=1, output=1)}
    if served_price is not None:
        prices["served-synthetic"] = ModelPrice(input=served_price, output=served_price)
    ledger = Ledger(prices=prices)
    with ResponseCache(enabled=False) as cache:
        client = Client(
            OpenAIProvider("test", client=cast(Any, SubstitutedModel())),
            settings,
            cache,
            ledger,
        )
        for index in range(2):
            assert client.complete(plan_for(user=str(index)), Answer).value == "ok"
    assert len(ledger.entries) == 2
    assert all(entry.served == "served-synthetic" for entry in ledger.entries)
    if served_price is None:
        assert ledger.total_dollars is None
    else:
        assert ledger.total_dollars == pytest.approx(2 * 1020 * served_price / 1_000_000)


def answering(vendor: str, body: dict[str, Any]) -> Any:
    """Return the real SDK adapter answering one canned body over a mock transport."""
    import httpx2

    from clearscout.application.config import ProviderName
    from tests.support.fake_provider import anthropic_events, openai_events
    from tests.support.provider_clients import provider_for_http

    render = anthropic_events if vendor == "anthropic" else openai_events
    transport = httpx2.MockTransport(
        lambda request: httpx2.Response(
            200, content=render(body), headers={"content-type": "text/event-stream"}
        )
    )
    return provider_for_http(ProviderName(vendor), httpx2.Client(transport=transport))


OPENAI_BODY: dict[str, Any] = {
    "id": "resp_envelope",
    "object": "response",
    "created_at": 0,
    "status": "completed",
    "model": "gpt-5.6-terra",
    "output": [
        {
            "type": "reasoning",
            "id": "rs_envelope",
            "summary": [{"type": "summary_text", "text": "compared two synthetic openings"}],
            "encrypted_content": "ENCRYPTED-REASONING-BLOB",
        },
        {
            "type": "message",
            "id": "msg_envelope",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": '{"value": "ok"}', "annotations": []}],
        },
    ],
    "parallel_tool_calls": False,
    "tool_choice": "auto",
    "tools": [],
    "usage": {
        "input_tokens": 12,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 8,
        "output_tokens_details": {"reasoning_tokens": 4},
        "total_tokens": 20,
    },
}

ANTHROPIC_BODY: dict[str, Any] = {
    "id": "msg_envelope",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-5",
    "content": [{"type": "text", "text": '{"value": "ok"}'}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {
        "input_tokens": 12,
        "output_tokens": 8,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 30,
        "cache_creation": {"ephemeral_5m_input_tokens": 30, "ephemeral_1h_input_tokens": 0},
    },
}


def test_the_openai_envelope_keeps_what_the_adapter_never_parses() -> None:
    """Reasoning content, the summary, the raw status and the reported total survive.

    None of these reach a `CallRecord` field, so an envelope that only
    re-serialized what was parsed would carry none of them.
    """
    value, record = answering("openai", OPENAI_BODY).complete(plan_for(), Answer, CHOICE)

    assert record.response_envelope is not None
    reasoning = record.response_envelope["output"][0]
    assert reasoning["encrypted_content"] == "ENCRYPTED-REASONING-BLOB"
    assert reasoning["summary"][0]["text"] == "compared two synthetic openings"
    assert record.response_envelope["status"] == "completed"
    assert record.response_envelope["usage"]["total_tokens"] == 20

    assert value.value == "ok"
    assert record.finish.value == "complete"
    assert record.model == "gpt-5.6-terra"
    assert record.response_id == "resp_envelope"
    assert record.usage.input_tokens == 12
    assert record.usage.reasoning_tokens == 4


def test_the_anthropic_envelope_keeps_what_the_adapter_never_parses() -> None:
    """The per-TTL cache breakdown and the raw stop reason survive into the record.

    The adapter keeps the flat `cache_creation_input_tokens` total and a
    `Finish` value, so neither of these is recoverable from what it parsed.
    """
    value, record = answering("anthropic", ANTHROPIC_BODY).complete(plan_for(), Answer, CHOICE)

    assert record.response_envelope is not None
    assert record.response_envelope["usage"]["cache_creation"]["ephemeral_5m_input_tokens"] == 30
    assert record.response_envelope["stop_reason"] == "end_turn"

    assert value.value == "ok"
    assert record.finish.value == "complete"
    assert record.model == "claude-sonnet-5"
    assert record.response_id == "msg_envelope"
    assert record.usage.cache_write_tokens == 30


@pytest.mark.parametrize(
    ("vendor", "body", "prompt_field"),
    [("openai", OPENAI_BODY, "input"), ("anthropic", ANTHROPIC_BODY, "system")],
)
def test_the_request_envelope_is_the_request_as_sent_and_is_json(
    vendor: str, body: dict[str, Any], prompt_field: str
) -> None:
    """Whatever the adapter handed the SDK is recorded, and it serializes."""
    _, record = answering(vendor, body).complete(
        plan_for("instructions", "ask", evidence="record", max_tokens=1234), Answer, CHOICE
    )

    assert record.request_envelope is not None
    assert json.loads(json.dumps(record.request_envelope)) == record.request_envelope
    assert record.request_envelope["model"] == "model"
    assert "ask" in json.dumps(record.request_envelope[prompt_field])
    assert "record" in json.dumps(record.request_envelope)


def test_a_continued_search_turn_records_a_request_that_still_serializes() -> None:
    """A pause continuation puts vendor content blocks back into the request."""
    provider = AnthropicProvider(api_key="x", client=cast(Any, PausingAnthropic()))

    _, record = provider.complete(
        plan_for(search=SearchPolicy(max_calls=2)), Answer, ModelChoice(model="model")
    )

    assert record.request_envelope is not None
    assert len(record.request_envelope["messages"]) == 2
    assert json.loads(json.dumps(record.request_envelope)) == record.request_envelope


def test_a_failed_continuation_records_the_paused_turn_as_it_was_sent() -> None:
    """The record for the paused turn is evidence of one request, not of the next one."""
    from clearscout.infrastructure.providers.base import UnusableResponseError

    provider = AnthropicProvider(api_key="x", client=cast(Any, InterruptedAnthropic()))

    with pytest.raises(UnusableResponseError) as raised:
        provider.complete_with_web_search(plan_for(search=SearchPolicy()), Answer, CHOICE)

    envelope = raised.value.record.request_envelope
    assert envelope is not None
    assert [message["role"] for message in envelope["messages"]] == ["user"]


def _stalled_stream(events: str, failure: type[Exception] | None = None) -> Any:
    """Return a response body that sends the first event and then raises ``failure``."""
    import httpx2

    class Stalled(httpx2.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield events.split("\n\n", maxsplit=1)[0].encode() + b"\n\n"
            raise (failure or httpx2.ReadTimeout)("The synthetic stream stopped sending.")

    return Stalled()


def _stalling_openai(
    stall_on: frozenset[str],
    requests: list[str] | None = None,
    failure: type[Exception] | None = None,
) -> Any:
    """Return the real OpenAI adapter whose stream stops mid-read on the named tiers."""
    import httpx2

    from clearscout.application.config import ProviderName
    from tests.support.fake_provider import openai_events
    from tests.support.provider_clients import provider_for_http

    def handler(request: httpx2.Request) -> httpx2.Response:
        tier = json.loads(request.content)["service_tier"]
        if requests is not None:
            requests.append(tier)
        headers = {"content-type": "text/event-stream"}
        if tier in stall_on:
            stalled = _stalled_stream(openai_events(OPENAI_BODY), failure)
            return httpx2.Response(200, stream=stalled, headers=headers)
        answered = openai_events({**OPENAI_BODY, "service_tier": tier})
        return httpx2.Response(200, content=answered, headers=headers)

    return provider_for_http(
        ProviderName.OPENAI, httpx2.Client(transport=httpx2.MockTransport(handler)), max_retries=0
    )


def test_a_flex_stream_that_stalls_mid_read_is_a_capacity_refusal() -> None:
    from clearscout.shared.errors import CapacityError

    provider = _stalling_openai(frozenset({"flex"}))

    with pytest.raises(CapacityError):
        provider.complete(
            plan_for(), Answer, CHOICE.model_copy(update={"service_tier": ServiceTier.FLEX})
        )


def test_a_standard_stream_that_stalls_mid_read_is_a_provider_error_not_a_refusal() -> None:
    from clearscout.shared.errors import CapacityError

    provider = _stalling_openai(frozenset({"default"}))

    with pytest.raises(ProviderError) as raised:
        provider.complete(plan_for(), Answer, CHOICE)

    assert not isinstance(raised.value, CapacityError)


DROPPED_CONNECTIONS = ["RemoteProtocolError", "ReadError"]


@pytest.mark.parametrize("dropped", DROPPED_CONNECTIONS)
def test_an_openai_connection_dropped_mid_stream_is_translated_on_either_tier(
    dropped: str,
) -> None:
    import httpx2

    from clearscout.shared.errors import CapacityError

    failure = getattr(httpx2, dropped)
    flex = _stalling_openai(frozenset({"flex"}), failure=failure)
    standard = _stalling_openai(frozenset({"default"}), failure=failure)

    with pytest.raises(CapacityError):
        flex.complete(
            plan_for(), Answer, CHOICE.model_copy(update={"service_tier": ServiceTier.FLEX})
        )
    with pytest.raises(ProviderError) as raised:
        standard.complete(plan_for(), Answer, CHOICE)
    assert not isinstance(raised.value, CapacityError)


def test_a_standard_server_error_is_a_provider_error_not_a_refusal() -> None:
    import httpx2

    from clearscout.application.config import ProviderName
    from clearscout.shared.errors import CapacityError
    from tests.support.provider_clients import provider_for_http

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(500, json={"error": {"message": "Synthetic server error."}})

    provider = provider_for_http(
        ProviderName.OPENAI, httpx2.Client(transport=httpx2.MockTransport(handler)), max_retries=0
    )

    with pytest.raises(ProviderError) as raised:
        provider.complete(plan_for(), Answer, CHOICE)

    assert not isinstance(raised.value, CapacityError)


def test_a_flex_request_that_times_out_before_its_headers_is_a_capacity_refusal() -> None:
    import httpx2

    from clearscout.application.config import ProviderName
    from clearscout.shared.errors import CapacityError
    from tests.support.provider_clients import provider_for_http

    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("No headers arrived.", request=request)

    provider = provider_for_http(
        ProviderName.OPENAI, httpx2.Client(transport=httpx2.MockTransport(handler)), max_retries=0
    )

    with pytest.raises(CapacityError):
        provider.complete(
            plan_for(), Answer, CHOICE.model_copy(update={"service_tier": ServiceTier.FLEX})
        )


def test_a_stalled_flex_call_is_answered_and_recorded_at_the_standard_tier(settings) -> None:
    requests: list[str] = []
    slept: list[float] = []

    value, ledger = _ledgered(_stalling_openai(frozenset({"flex"}), requests), settings, slept)

    assert value.value == "ok"
    assert requests == ["flex", "default"]
    assert slept == []
    [entry] = ledger.entries
    assert entry.service_tier == "default"
    assert entry.response_id == "resp_envelope"
    assert entry.validated


def _ledgered(provider: Any, settings: Any, slept: list[float]) -> tuple[Answer, Any]:
    """Complete one flex-first call through the client and return its value and ledger."""
    from clearscout.infrastructure.providers.cache import ResponseCache
    from clearscout.infrastructure.providers.client import Client
    from clearscout.shared.costs import Ledger

    ledger = Ledger(prices={})
    with ResponseCache(enabled=False) as cache:
        client = Client(provider, settings, cache, ledger)
        client.service_tier = ServiceTier.FLEX
        client.sleeper = slept.append
        return client.complete(plan_for(), Answer), ledger


@pytest.mark.parametrize("failure", ["timeout", "connection", "server_error"])
def test_a_flex_request_that_fails_before_answering_goes_to_standard_without_sdk_retries(
    settings, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """The adapter keeps the SDK's two retries, which would resend a flex failure twice."""
    import time

    import httpx2

    from clearscout.application.config import ProviderName
    from tests.support.fake_provider import openai_events
    from tests.support.provider_clients import provider_for_http

    requests: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        tier = json.loads(request.content)["service_tier"]
        requests.append(tier)
        if tier == "flex":
            if failure == "timeout":
                raise httpx2.ReadTimeout("No headers arrived.", request=request)
            if failure == "connection":
                raise httpx2.ConnectError("The synthetic host refused.", request=request)
            return httpx2.Response(500, json={"error": {"message": "Synthetic server error."}})
        answered = openai_events({**OPENAI_BODY, "service_tier": tier})
        return httpx2.Response(200, content=answered, headers={"content-type": "text/event-stream"})

    sdk_slept: list[float] = []
    monkeypatch.setattr(time, "sleep", sdk_slept.append)
    provider = provider_for_http(
        ProviderName.OPENAI, httpx2.Client(transport=httpx2.MockTransport(handler)), max_retries=2
    )
    slept: list[float] = []

    value, ledger = _ledgered(provider, settings, slept)

    assert value.value == "ok"
    assert requests == ["flex", "default"]
    assert slept == sdk_slept == []
    [entry] = ledger.entries
    assert entry.service_tier == "default"


def test_a_flex_capacity_refusal_still_backs_off_before_standard(settings) -> None:
    import httpx2

    from clearscout.application.config import ProviderName
    from tests.support.fake_provider import openai_events
    from tests.support.provider_clients import provider_for_http

    requests: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        tier = json.loads(request.content)["service_tier"]
        requests.append(tier)
        if tier == "flex":
            refusal = {"error": {"code": "resource_unavailable", "message": "No capacity."}}
            return httpx2.Response(429, json=refusal)
        answered = openai_events({**OPENAI_BODY, "service_tier": tier})
        return httpx2.Response(200, content=answered, headers={"content-type": "text/event-stream"})

    provider = provider_for_http(
        ProviderName.OPENAI, httpx2.Client(transport=httpx2.MockTransport(handler)), max_retries=0
    )
    slept: list[float] = []

    value, ledger = _ledgered(provider, settings, slept)

    assert value.value == "ok"
    assert requests == ["flex"] * 4 + ["default"]
    assert len(slept) == 3
    [entry] = ledger.entries
    assert entry.service_tier == "default"


@pytest.mark.parametrize(
    ("status", "code"),
    [(429, "rate_limit_exceeded"), (408, "request_timeout"), (409, "conflict")],
)
def test_a_flex_rate_limit_timeout_or_conflict_backs_off_and_is_answered_on_flex(
    settings, status: int, code: str
) -> None:
    import httpx2

    from clearscout.application.config import ProviderName
    from tests.support.fake_provider import openai_events
    from tests.support.provider_clients import provider_for_http

    requests: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        tier = json.loads(request.content)["service_tier"]
        requests.append(tier)
        if len(requests) == 1:
            return httpx2.Response(status, json={"error": {"code": code, "message": "Slow down."}})
        answered = openai_events({**OPENAI_BODY, "service_tier": tier})
        return httpx2.Response(200, content=answered, headers={"content-type": "text/event-stream"})

    provider = provider_for_http(
        ProviderName.OPENAI, httpx2.Client(transport=httpx2.MockTransport(handler)), max_retries=0
    )
    slept: list[float] = []

    value, ledger = _ledgered(provider, settings, slept)

    assert value.value == "ok"
    assert requests == ["flex", "flex"]
    assert len(slept) == 1
    [entry] = ledger.entries
    assert entry.service_tier == "flex"


FLEX_UNAVAILABLE = (
    "Flex processing is temporarily unavailable. Please try again later or use standard processing."
)


def _refusing_openai(
    refused: dict[str, Any],
    requests: list[str],
    refuse_on: frozenset[str] = frozenset({"flex"}),
    max_retries: int = 0,
    before: tuple[dict[str, Any], ...] = (),
) -> Any:
    """Return the real OpenAI adapter whose stream ends in ``refused`` on the named tiers.

    ``before`` holds the frames sent between ``response.created`` and the refusal.
    """
    import httpx2

    from clearscout.application.config import ProviderName
    from tests.support.fake_provider import _frames, openai_events
    from tests.support.provider_clients import provider_for_http

    def handler(request: httpx2.Request) -> httpx2.Response:
        tier = json.loads(request.content)["service_tier"]
        requests.append(tier)
        if tier in refuse_on:
            created = {"type": "response.created", "sequence_number": 0, "response": OPENAI_BODY}
            body = _frames([created, *before, refused])
        else:
            body = openai_events({**OPENAI_BODY, "service_tier": tier})
        return httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"})

    return provider_for_http(
        ProviderName.OPENAI,
        httpx2.Client(transport=httpx2.MockTransport(handler)),
        max_retries=max_retries,
    )


STREAM_ERROR: dict[str, Any] = {
    "type": "error",
    "error": {"message": FLEX_UNAVAILABLE, "code": "server_error"},
}
# The Responses API's documented error event carries its code and message at the top level.
ERROR_EVENT: dict[str, Any] = {
    "type": "error",
    "sequence_number": 1,
    "code": "server_error",
    "message": FLEX_UNAVAILABLE,
    "param": None,
}
OUTPUT_ADDED: dict[str, Any] = {
    "type": "response.output_item.added",
    "sequence_number": 1,
    "output_index": 0,
    "item": {"id": "msg_1", "type": "message", "role": "assistant", "status": "in_progress"},
}


def _failed_response(
    usage: dict[str, Any] | None,
    code: str = "server_error",
    message: str = FLEX_UNAVAILABLE,
) -> dict[str, Any]:
    """Return a ``response.failed`` frame whose response carries ``usage``."""
    failed = {
        **OPENAI_BODY,
        "status": "failed",
        "output": [],
        "usage": usage,
        "error": {"code": code, "message": message},
    }
    return {"type": "response.failed", "sequence_number": 1, "response": failed}


@pytest.mark.parametrize(
    "refused",
    [STREAM_ERROR, ERROR_EVENT, _failed_response(None)],
    ids=["stream_error", "error_event", "unbilled_failure"],
)
def test_a_flex_refusal_inside_the_stream_backs_off_rather_than_failing(
    refused: dict[str, Any],
) -> None:
    from clearscout.shared.errors import CapacityError

    provider = _refusing_openai(refused, [])

    with pytest.raises(CapacityError) as raised:
        provider.complete(
            plan_for(), Answer, CHOICE.model_copy(update={"service_tier": ServiceTier.FLEX})
        )

    assert not raised.value.stalled
    assert FLEX_UNAVAILABLE in str(raised.value)
    assert "server_error" in str(raised.value)


@pytest.mark.parametrize(
    "refused", [STREAM_ERROR, ERROR_EVENT], ids=["stream_error", "error_event"]
)
def test_a_standard_refusal_inside_the_stream_is_translated_and_not_resent(
    refused: dict[str, Any],
) -> None:
    from clearscout.shared.errors import CapacityError

    requests: list[str] = []
    provider = _refusing_openai(refused, requests, frozenset({"default"}), max_retries=2)

    with pytest.raises(ProviderError) as raised:
        provider.complete(plan_for(), Answer, CHOICE)

    assert not isinstance(raised.value, CapacityError)
    assert "server_error" in str(raised.value)
    assert requests == ["default"]


@pytest.mark.parametrize(
    "refused",
    [STREAM_ERROR, ERROR_EVENT, _failed_response(None)],
    ids=["stream_error", "error_event", "unbilled_failure"],
)
def test_a_flex_refusal_inside_the_stream_is_answered_on_standard(
    settings, refused: dict[str, Any]
) -> None:
    requests: list[str] = []
    slept: list[float] = []

    value, ledger = _ledgered(_refusing_openai(refused, requests), settings, slept)

    assert value.value == "ok"
    assert requests == ["flex"] * 4 + ["default"]
    assert len(slept) == 3
    [entry] = ledger.entries
    assert entry.service_tier == "default"


def test_a_task_whose_flex_call_was_resent_on_standard_reports_how_many_were(
    settings, durable_database, tmp_path
) -> None:
    from clearscout.application.tasks import TaskQueueService
    from clearscout.application.workspaces import WorkspaceService
    from clearscout.application.workspaces.spend import SpendService, record_calls
    from tests.support.artifacts import runtime_storage

    workspace_id = str(
        WorkspaceService(durable_database, runtime_storage(durable_database))
        .create(slug="fallback", name="Fallback", root_path=str(tmp_path))
        .id
    )
    queue = TaskQueueService(durable_database)
    fell_back, answered = (
        queue.enqueue(
            workspace_id=workspace_id,
            task_type="discovery.evaluate",
            payload={},
            idempotency_key=key,
        )
        for key in ("fell-back", "answered-on-flex")
    )
    _value, refused_ledger = _ledgered(_refusing_openai(ERROR_EVENT, []), settings, [])
    _value, flex_ledger = _ledgered(_refusing_openai(ERROR_EVENT, [], frozenset()), settings, [])
    with durable_database.session() as session:
        for task, ledger in ((fell_back, refused_ledger), (answered, flex_ledger)):
            record_calls(
                session,
                workspace_id=workspace_id,
                task_id=task.id,
                ledger=ledger,
                provider="openai",
                origin="discovery.evaluate",
            )

    billing = SpendService(durable_database)
    assert billing.task_billing(workspace_id, fell_back.id).resent_on_standard_count == 1
    assert billing.task_billing(workspace_id, answered.id).resent_on_standard_count == 0


def test_a_billed_flex_failure_is_never_resent(settings) -> None:
    from clearscout.infrastructure.providers.base import UnusableResponseError

    requests: list[str] = []
    billed = _failed_response(OPENAI_BODY["usage"])

    with pytest.raises(UnusableResponseError, match="provider's end"):
        _ledgered(_refusing_openai(billed, requests), settings, [])

    assert requests == ["flex"]


@pytest.mark.parametrize(
    "refused", [STREAM_ERROR, ERROR_EVENT], ids=["stream_error", "error_event"]
)
def test_a_flex_refusal_after_output_began_is_never_resent(
    settings, refused: dict[str, Any]
) -> None:
    from clearscout.shared.errors import CapacityError

    requests: list[str] = []
    provider = _refusing_openai(refused, requests, before=(OUTPUT_ADDED,))

    with pytest.raises(ProviderError) as raised:
        _ledgered(provider, settings, [])

    assert not isinstance(raised.value, CapacityError)
    assert FLEX_UNAVAILABLE in str(raised.value)
    assert requests == ["flex"]


@pytest.mark.parametrize(
    "refused",
    [
        {
            "type": "error",
            "error": {
                "message": "Your input exceeds the context window.",
                "code": "invalid_prompt",
            },
        },
        {
            "type": "error",
            "sequence_number": 1,
            "code": "context_length_exceeded",
            "message": "Your input exceeds the context window.",
            "param": None,
        },
    ],
    ids=["stream_error", "error_event"],
)
def test_a_flex_stream_error_that_is_not_about_capacity_is_never_resent(
    settings, refused: dict[str, Any]
) -> None:
    from clearscout.shared.errors import CapacityError

    requests: list[str] = []
    slept: list[float] = []

    with pytest.raises(ProviderError) as raised:
        _ledgered(_refusing_openai(refused, requests), settings, slept)

    assert not isinstance(raised.value, CapacityError)
    assert "Your input exceeds the context window." in str(raised.value)
    assert str(refused.get("code") or refused["error"]["code"]) in str(raised.value)
    assert requests == ["flex"]
    assert slept == []


@pytest.mark.parametrize(
    ("before", "failed"),
    [
        ((), _failed_response(None, "invalid_prompt", "Your input exceeds the context window.")),
        ((OUTPUT_ADDED,), _failed_response(None)),
    ],
    ids=["not_about_capacity", "after_output"],
)
def test_an_unbilled_flex_failure_is_resent_only_for_capacity_before_output(
    settings, before: tuple[dict[str, Any], ...], failed: dict[str, Any]
) -> None:
    from clearscout.shared.errors import CapacityError

    requests: list[str] = []
    slept: list[float] = []

    with pytest.raises(ProviderError) as raised:
        _ledgered(_refusing_openai(failed, requests, before=before), settings, slept)

    assert not isinstance(raised.value, CapacityError)
    assert requests == ["flex"]
    assert slept == []


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("rate_limit_exceeded", "Rate limit reached for requests."),
        ("resource_unavailable", "Resource unavailable."),
        (None, "The server is overloaded. Please try again later."),
    ],
)
def test_a_flex_stream_error_about_capacity_is_answered_on_standard(
    settings, code: str | None, message: str
) -> None:
    requests: list[str] = []
    refused = {**ERROR_EVENT, "code": code, "message": message}

    value, _ledger = _ledgered(_refusing_openai(refused, requests), settings, [])

    assert value.value == "ok"
    assert requests == ["flex"] * 4 + ["default"]


PAUSED_ANTHROPIC_BODY: dict[str, Any] = {**ANTHROPIC_BODY, "stop_reason": "pause_turn"}


def _stalling_anthropic(answered_turns: int, failure: type[Exception] | None = None) -> Any:
    """Return the real Anthropic adapter that pauses for some turns, then stalls mid-read."""
    import httpx2

    from clearscout.application.config import ProviderName
    from tests.support.fake_provider import anthropic_events
    from tests.support.provider_clients import provider_for_http

    sent: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        headers = {"content-type": "text/event-stream"}
        if len(sent) <= answered_turns:
            return httpx2.Response(
                200, content=anthropic_events(PAUSED_ANTHROPIC_BODY), headers=headers
            )
        stalled = _stalled_stream(anthropic_events(ANTHROPIC_BODY), failure)
        return httpx2.Response(200, stream=stalled, headers=headers)

    return provider_for_http(
        ProviderName.ANTHROPIC,
        httpx2.Client(transport=httpx2.MockTransport(handler)),
        max_retries=0,
    )


def test_an_anthropic_stream_that_stalls_mid_read_is_a_provider_error() -> None:
    from clearscout.infrastructure.providers.base import UnusableResponseError

    with pytest.raises(ProviderError) as raised:
        _stalling_anthropic(0).complete(plan_for(), Answer, CHOICE)

    assert not isinstance(raised.value, UnusableResponseError)


@pytest.mark.parametrize("dropped", DROPPED_CONNECTIONS)
def test_an_anthropic_connection_dropped_mid_stream_is_a_provider_error(dropped: str) -> None:
    import httpx2

    from clearscout.infrastructure.providers.base import UnusableResponseError

    with pytest.raises(ProviderError) as raised:
        _stalling_anthropic(0, getattr(httpx2, dropped)).complete(plan_for(), Answer, CHOICE)

    assert not isinstance(raised.value, UnusableResponseError)


def test_an_anthropic_continuation_that_stalls_keeps_the_paused_turn_record() -> None:
    from clearscout.infrastructure.providers.base import UnusableResponseError

    with pytest.raises(UnusableResponseError) as raised:
        _stalling_anthropic(1).complete_with_web_search(
            plan_for(search=SearchPolicy()), Answer, CHOICE
        )

    assert raised.value.record.response_id == "msg_envelope"
    assert not raised.value.record.usage_known
