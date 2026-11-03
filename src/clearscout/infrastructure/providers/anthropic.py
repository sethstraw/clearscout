"""Anthropic Messages adapter for structured output and hosted search."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel

from clearscout.application.config import ModelChoice, ProviderName
from clearscout.infrastructure.providers.base import (
    CallRecord,
    Finish,
    Search,
    SearchSource,
    UnusableResponseError,
    _reported_usage,
    _response_field,
    _response_record,
    _response_result,
    envelope_of,
    model_identifiers,
    wire_schema,
)
from clearscout.infrastructure.providers.failures import translate
from clearscout.infrastructure.providers.plan import RequestPlan, SearchPolicy
from clearscout.shared.costs import Usage
from clearscout.shared.errors import ProviderError

if TYPE_CHECKING:  # pragma: no cover - import cost is only paid at runtime
    from anthropic import Anthropic

ModelT = TypeVar("ModelT", bound=BaseModel)
WEB_SEARCH_TOOL = "web_search_20260209"
MAX_PAUSE_CONTINUATIONS = 3


class AnthropicProvider:
    """Structured completion through the Anthropic Messages API."""

    name = ProviderName.ANTHROPIC

    def __init__(
        self,
        api_key: str,
        client: Anthropic | None = None,
        timeout: float | None = None,
        transport: Any | None = None,
    ) -> None:
        """Build a client, or accept an injected one so tests never touch the network."""
        if client is not None:
            self._client = client
            return
        try:
            from anthropic import Anthropic as SyncAnthropic
            from anthropic import DefaultHttpxClient
        except ImportError as error:  # pragma: no cover - dependency is declared
            raise ProviderError("The anthropic package is not installed.") from error
        # Omit timeout to preserve the SDK default.
        options: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
        if transport is not None:
            # The SDK's own client keeps its default timeouts and connection limits.
            options["http_client"] = DefaultHttpxClient(transport=transport)
        # SDK retries multiply the configured timeout.
        self._client = SyncAnthropic(api_key=api_key, max_retries=2, **options)

    def complete(
        self,
        plan: RequestPlan,
        schema: type[ModelT],
        choice: ModelChoice,
    ) -> tuple[ModelT, CallRecord]:
        """Send one structured request without provider-hosted tools."""
        return self._complete(plan, schema, choice)

    def complete_with_web_search(
        self,
        plan: RequestPlan,
        schema: type[ModelT],
        choice: ModelChoice,
    ) -> tuple[ModelT, CallRecord]:
        """Search on Anthropic's servers and return one validated object."""
        return self._complete(plan, schema, choice)

    def _complete(
        self,
        plan: RequestPlan,
        schema: type[ModelT],
        choice: ModelChoice,
    ) -> tuple[ModelT, CallRecord]:
        """Send a structured request and finish any paused server-tool turn."""
        system: list[dict[str, Any]] = [
            {"type": "text", "text": plan.kernel.text},
            {"type": "text", "text": plan.instructions.text},
        ]
        messages: list[dict[str, Any]] = [{"role": "user", "content": _user_content(plan)}]
        output_config = _output_config(self.name, schema, choice)

        tools = None if plan.search is None else [_web_search_tool(plan.search)]
        tool_version = WEB_SEARCH_TOOL if plan.search is not None else ""
        total_usage = Usage()
        usage_known = True
        collected: list[Search] = []
        search_request_count = 0
        response: Any = None
        sent: dict[str, Any] = {}

        def recorded(message: Any) -> CallRecord:
            return replace(
                _record(message, sent),
                usage=total_usage,
                usage_known=usage_known,
                searches=tuple(collected),
                search_count=search_request_count or len(collected),
                tool_version=tool_version,
            )

        for continuation in range(MAX_PAUSE_CONTINUATIONS + 1):
            previous = recorded(response) if response is not None else None
            request, sent = _turn_request(plan, choice, system, messages, output_config, tools)
            response = self._send_message(request, previous)

            usage, known = _usage(_response_field(response, "usage", None))
            total_usage += usage
            usage_known = usage_known and known
            # A paused response still contains searches completed in this turn.
            collected.extend(_searches(response))
            search_request_count += _search_count(response)
            stop_reason = _response_field(response, "stop_reason", "")
            if stop_reason != "pause_turn" or plan.search is None:
                break
            if continuation == MAX_PAUSE_CONTINUATIONS:
                raise UnusableResponseError(
                    "Anthropic paused its web-search turn too many times to finish safely.",
                    recorded(response),
                )
            # Continue the same server-tool turn with the unchanged assistant content.
            messages.append({"role": "assistant", "content": response.content})

        value, record = _response_result(
            response,
            schema,
            "Anthropic",
            plan.max_tokens,
            record_from=recorded,
            text_from=_text,
        )
        return value, record

    def _send_message(self, request: dict[str, Any], previous: CallRecord | None) -> Any:
        import httpx
        from anthropic import APIError

        try:
            # A non-streaming request withholds its headers until the answer is
            # complete, so a long one outlives the connection that carries it.
            with self._client.messages.stream(**request) as stream:
                return stream.get_final_message()
        # The SDK wraps a failure before the headers; one during the stream read arrives raw.
        except (APIError, httpx.TransportError) as error:
            translated = (
                translate(error, self.name, "complete this model request")
                if isinstance(error, APIError)
                else ProviderError(
                    "Anthropic stopped answering before the response finished, so clearscout\n"
                    "could not complete this model request. Try again shortly."
                )
            )
            if previous is not None:
                raise UnusableResponseError(
                    str(translated),
                    replace(previous, usage_known=False, finish=Finish.FAILED),
                ) from error
            raise translated from error

    def available_models(self) -> frozenset[str] | None:
        """List the models this account can use, so a typo is caught early."""
        from anthropic import APIError

        try:
            return model_identifiers(self._client.models.list(limit=1000))
        except APIError as error:
            raise translate(error, self.name, "list the models on your account") from error

    def check_credentials(self) -> None:
        """Confirm the key works by listing models, which bills no tokens."""
        from anthropic import APIError

        try:
            self._client.models.list(limit=1)
        except APIError as error:
            raise translate(error, self.name, "verify that key") from error


def _user_content(plan: RequestPlan) -> list[dict[str, Any]]:
    """Render the plan's images, evidence, and task as one user message body."""
    content: list[dict[str, Any]] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": image.media_type,
                "data": image.base64(),
            },
        }
        for image in plan.images
    ]
    if plan.evidence is not None:
        content.append(
            {"type": "text", "text": plan.evidence.text, "cache_control": {"type": "ephemeral"}}
        )
    # A single draw keeps its task out of the cache; repeat draws read it back.
    repeated = {"cache_control": {"type": "ephemeral"}} if plan.draws > 1 else {}
    content.append({"type": "text", "text": plan.task.text, **repeated})
    return content


def _output_config(
    name: ProviderName,
    schema: type[BaseModel],
    choice: ModelChoice,
) -> dict[str, Any]:
    """Describe the structured output this call must return."""
    config: dict[str, Any] = {
        "format": {"type": "json_schema", "schema": wire_schema(name, schema)},
    }
    if choice.effort is not None:
        # Effort is the supported reasoning control for this model choice.
        config["effort"] = choice.effort.value
    return config


def _turn_request(
    plan: RequestPlan,
    choice: ModelChoice,
    system: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    output_config: dict[str, Any],
    tools: list[dict[str, Any]] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return this turn's request and a snapshot of what it sends."""
    request: dict[str, Any] = {
        "model": choice.model,
        "max_tokens": plan.max_tokens,
        "output_config": output_config,
        "system": system,
        "messages": messages,
    }
    if tools is not None:
        request["tools"] = tools
    # The request aliases the message list a continuation appends to, so the
    # record carries a copy taken before the next turn grows it.
    return request, envelope_of(request) or {}


def _web_search_tool(policy: SearchPolicy) -> dict[str, Any]:
    """Render the policy as Anthropic's tool definition."""
    tool: dict[str, Any] = {
        "type": WEB_SEARCH_TOOL,
        "name": "web_search",
        "max_uses": policy.max_calls,
        # From this tool version on, the default runs the search from inside
        # code execution, which is a different retention path.
        "allowed_callers": ["direct"],
    }
    if policy.allowed_domains:
        tool["allowed_domains"] = list(policy.allowed_domains)
    if policy.blocked_domains:
        tool["blocked_domains"] = list(policy.blocked_domains)
    if policy.location is not None and (stated := policy.location.stated()):
        tool["user_location"] = {"type": "approximate", **stated}
    return tool


def _searches(response: Any) -> list[Search]:
    """Pull the queries and the pages they returned out of one message."""
    content: list[Any] | None = _response_field(response, "content", None)
    queries: dict[str, str] = {}
    citations_by_query: dict[str, tuple[str, ...]] = {}
    found: list[Search] = []
    for block in content or []:
        kind = _response_field(block, "type", "")
        if kind == "server_tool_use":
            asked: Any = _response_field(block, "input", None)
            identifier = str(_response_field(block, "id", "") or "")
            if asked is None:
                queries[identifier] = ""
            else:
                queries[identifier] = str(_response_field(asked, "query", ""))
            citations_by_query[identifier] = _merge_citations(
                _citations(block),
                _citations(asked),
            )
        elif kind == "web_search_tool_result":
            identifier = str(_response_field(block, "tool_use_id", "") or "")
            citations = _merge_citations(citations_by_query.get(identifier, ()), _citations(block))
            asked_query = queries.get(identifier, "")
            found.append(
                Search(
                    queries=(asked_query,) if asked_query else (),
                    sources=_sources(block),
                    citations=citations,
                )
            )
    return found


def _search_count(response: Any) -> int:
    """Return this message's hosted search call count."""
    usage = _response_field(response, "usage", None)
    server_tools = _response_field(usage, "server_tool_use", None)
    requested: Any = _response_field(server_tools, "web_search_requests", 0)
    if isinstance(requested, int):
        return requested
    if isinstance(requested, str) and requested.isdigit():
        return int(requested)
    content: list[Any] | None = _response_field(response, "content", None)
    if not isinstance(content, list):
        return 0
    return sum(1 for block in content if _response_field(block, "type", "") == "server_tool_use")


def _citations(raw: Any) -> tuple[str, ...]:
    """Return normalized citation identifiers from one vendor payload block."""
    values: Any = _response_field(raw, "citations", None)
    if not isinstance(values, list):
        return ()
    found: list[str] = []
    for item in values:
        if isinstance(item, str):
            found.append(item)
        elif isinstance(item, (int, float, bool)):
            found.append(str(item))
        else:
            value: Any = _response_field(item, "id", None)
            if value is None:
                value = _response_field(item, "citation_id", None)
            if value is not None:
                found.append(str(value))
    return tuple(found)


def _merge_citations(*parts: tuple[str, ...]) -> tuple[str, ...]:
    """Merge citation sequences while preserving first-seen order."""
    unique: list[str] = []
    for part in parts:
        for item in part:
            if item not in unique:
                unique.append(item)
    return tuple(unique)


def _sources(block: Any) -> tuple[SearchSource, ...]:
    """Return the pages one result block carried, or none when it errored."""
    results: Any = _response_field(block, "content", None)
    if not isinstance(results, list):
        return ()
    return tuple(
        SearchSource(
            url=str(_response_field(item, "url", "") or ""),
            title=str(_response_field(item, "title", "") or ""),
            citations=_citations(item),
        )
        for item in results
        if _response_field(item, "type", "") == "web_search_result"
    )


def _text(response: Any) -> str | None:
    """Return the assistant's text, or ``None`` when it produced none.

    ``None`` rather than an empty string, because ``parse_or_raise`` treats it
    as a refusal or an exhausted budget, which is what a text-less answer is.
    """
    content: list[Any] | None = _response_field(response, "content", None)
    parts = [block.text for block in content or [] if _response_field(block, "type", "") == "text"]
    return "".join(parts) or None


def _record(response: Any, request: Mapping[str, Any] | None = None) -> CallRecord:
    """Pull everything Anthropic said about the call out of the message."""
    stop_reason: object | None = _response_field(response, "stop_reason", None)
    raw = _response_field(response, "usage", None)
    service_tier: object | None = _response_field(raw, "service_tier", None)
    usage, known = _usage(raw)
    return replace(
        _response_record(response, usage, _finish(stop_reason), service_tier, request),
        usage_known=known,
    )


def _finish(stop_reason: Any) -> Finish:
    """Translate a Messages API stop reason into the shared vocabulary.

    ``max_tokens`` is the one worth naming: the message ends mid-object, so
    without it the symptom reads as a schema mismatch rather than a small budget.
    """
    return {
        None: Finish.COMPLETE,
        "end_turn": Finish.COMPLETE,
        "stop_sequence": Finish.COMPLETE,
        "tool_use": Finish.COMPLETE,
        "max_tokens": Finish.TRUNCATED,
        "model_context_window_exceeded": Finish.TRUNCATED,
        "refusal": Finish.REFUSED,
        "pause_turn": Finish.UNKNOWN,
    }.get(stop_reason, Finish.UNKNOWN)


def _usage(raw: Any) -> tuple[Usage, bool]:
    """Normalize reported counts without losing an answer to incomplete accounting."""
    details = _response_field(raw, "output_tokens_details", None)
    usage, known = _reported_usage(
        input_tokens=_response_field(raw, "input_tokens", None),
        output_tokens=_response_field(raw, "output_tokens", None),
        cache_read_tokens=_response_field(raw, "cache_read_input_tokens", 0),
        cache_write_tokens=_response_field(raw, "cache_creation_input_tokens", 0),
        reasoning_tokens=_response_field(details, "reasoning_tokens", 0),
    )
    return usage, known and usage.reasoning_tokens <= usage.output_tokens
