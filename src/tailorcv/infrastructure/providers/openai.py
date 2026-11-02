"""OpenAI Responses adapter for structured output and hosted search."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel

from tailorcv.application.config import ModelChoice, ProviderName
from tailorcv.infrastructure.providers.base import (
    BackgroundDispatch,
    BackgroundPoll,
    CallRecord,
    Finish,
    PageAction,
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
from tailorcv.infrastructure.providers.failures import translate
from tailorcv.infrastructure.providers.plan import RequestPlan, SearchPolicy
from tailorcv.shared.costs import ServiceTier, Usage
from tailorcv.shared.errors import CapacityError, ProviderError, TailorcvError

if TYPE_CHECKING:  # pragma: no cover - import cost is only paid at runtime
    from openai import OpenAI

ModelT = TypeVar("ModelT", bound=BaseModel)

# OpenAI names the standard tier "default"; the flex tier keeps its name.
_OPENAI_SERVICE_TIER = {ServiceTier.STANDARD: "default", ServiceTier.FLEX: "flex"}

# Statuses the SDK retries after a wait; on flex the client backs off on the same tier instead.
_THROTTLE_STATUSES = frozenset({408, 409, 429})

# An in-stream error worth resending names a capacity code or capacity wording.
_CAPACITY_CODES = frozenset({"resource_unavailable", "rate_limit_exceeded"})
_CAPACITY_WORDING = re.compile(
    r"flex processing is temporarily unavailable|overloaded|rate limit", re.IGNORECASE
)
# Stream events that precede any output the response could be billed for.
_PREAMBLE_EVENTS = frozenset({"response.created", "response.queued", "response.in_progress"})


class _StreamError(Exception):
    """An ``error`` event yielded inside an accepted stream, shaped as ``translate`` reads it."""

    status_code = None

    def __init__(self, code: str | None, message: str, *, after_output: bool) -> None:
        super().__init__(message)
        self.code = code or None
        self.message = message
        self.body = {"code": code, "message": message}
        self.after_output = after_output


class OpenAIProvider:
    """Structured completion through the Responses API."""

    name = ProviderName.OPENAI

    def __init__(
        self,
        api_key: str,
        client: OpenAI | None = None,
        timeout: float | None = None,
        transport: Any | None = None,
    ) -> None:
        """Build a client, or accept an injected one so tests never touch the network."""
        if client is not None:
            self._client = client
            return
        try:
            from openai import DefaultHttpxClient
            from openai import OpenAI as SyncOpenAI
        except ImportError as error:  # pragma: no cover - dependency is declared
            raise ProviderError("The openai package is not installed.") from error
        # Omit timeout to preserve the SDK default.
        options: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
        if transport is not None:
            # The SDK's own client keeps its default timeouts and connection limits.
            options["http_client"] = DefaultHttpxClient(transport=transport)
        # SDK retries multiply the configured timeout.
        self._client = SyncOpenAI(api_key=api_key, max_retries=2, **options)

    def complete(
        self,
        plan: RequestPlan,
        schema: type[ModelT],
        choice: ModelChoice,
    ) -> tuple[ModelT, CallRecord]:
        """Send one request and return the validated object and its token usage."""
        return self._complete(plan, schema, choice)

    def complete_with_web_search(
        self,
        plan: RequestPlan,
        schema: type[ModelT],
        choice: ModelChoice,
    ) -> tuple[ModelT, CallRecord]:
        """Search on OpenAI's servers and return one validated object."""
        return self._complete(plan, schema, choice)

    def _complete(
        self,
        plan: RequestPlan,
        schema: type[ModelT],
        choice: ModelChoice,
    ) -> tuple[ModelT, CallRecord]:
        """Send one structured Responses request, optionally with web search."""
        import httpx
        from openai import APIError, APITimeoutError

        sent: dict[str, Any] = {
            **self._request(plan, schema, choice),
            "max_output_tokens": plan.max_tokens,
            "store": False,
            "stream": True,
        }
        # On flex the client owns every resend, so SDK retries would multiply its attempts.
        client = (
            self._client.with_options(max_retries=0)
            if choice.service_tier is ServiceTier.FLEX
            else self._client
        )
        try:
            # A non-streaming request withholds its headers until the answer is
            # complete, and the server disconnects it at about sixty seconds.
            events = client.responses.create(**sent)
            try:
                response, after_output = _terminal_response(events)
            finally:
                close = getattr(events, "close", None)
                if callable(close):
                    close()
        # The SDK wraps a failure before the headers; one during the stream read arrives raw.
        except (APITimeoutError, httpx.TransportError) as error:
            raise _timed_out(choice.service_tier) from error
        except (APIError, _StreamError) as error:
            flex = choice.service_tier is ServiceTier.FLEX
            if flex and _transient(error):
                raise _stalled_on_flex("failed before it answered") from error
            translated = translate(error, self.name, "complete this model request")
            # Output already streamed may be billed, so nothing after it is resent.
            if getattr(error, "after_output", False) and isinstance(translated, CapacityError):
                translated = ProviderError(str(translated))
            if flex and _flex_refusal(error) and not isinstance(translated, CapacityError):
                raise _throttled_on_flex(translated) from error
            raise translated from error

        if choice.service_tier is ServiceTier.FLEX and _unbilled_failure(
            response, after_output=after_output
        ):
            raise _throttled_on_flex(_failed_unbilled(response))
        return _response_result(
            response,
            schema,
            "OpenAI",
            plan.max_tokens,
            record_from=partial(_record, request=sent),
            text_from=_text,
        )

    def _request(
        self, plan: RequestPlan, schema: type[BaseModel], choice: ModelChoice
    ) -> dict[str, Any]:
        """Build the shared counted and dispatched model-facing fields."""
        instructions = plan.instruction_text()
        stated_detail = {} if choice.image_detail is None else {"detail": choice.image_detail.value}
        content: list[dict[str, Any]] = [
            {"type": "input_image", "image_url": image.data_url(), **stated_detail}
            for image in plan.images
        ]
        cache_mark = {"prompt_cache_breakpoint": {"mode": "explicit"}}
        if plan.evidence is not None:
            content.append({"type": "input_text", "text": plan.evidence.text, **cache_mark})
        # A single draw keeps its task out of the cache; repeat draws read it back.
        repeated = cache_mark if plan.draws > 1 else {}
        content.append({"type": "input_text", "text": plan.task.text, **repeated})
        # Explicit mode writes only marked prefixes; implicit mode would also write
        # the per-call content up to the end of the user message.
        extra: dict[str, Any] = {"prompt_cache_options": {"mode": "explicit"}}

        if choice.effort is not None:
            extra["reasoning"] = {"effort": choice.effort.value}
        extra["service_tier"] = _OPENAI_SERVICE_TIER[choice.service_tier]
        if plan.search is not None:
            extra["tools"] = [_web_search_tool(plan.search)]
            extra["max_tool_calls"] = plan.search.max_calls
            if plan.search.capture_sources:
                extra["include"] = ["web_search_call.action.sources"]

        # One Pydantic model renders both vendors' documents, each narrowed to
        # the keywords that vendor supports.
        text: dict[str, Any] = {
            "format": {
                "type": "json_schema",
                "name": schema.__name__,
                "schema": wire_schema(self.name, schema),
                "strict": True,
            }
        }
        if choice.verbosity is not None:
            text["verbosity"] = choice.verbosity.value
        # Top-level `instructions` cannot carry a breakpoint, so they travel as a
        # developer message that stays cached while the evidence after it changes.
        developer = {"type": "input_text", "text": instructions, **cache_mark}
        return {
            "model": choice.model,
            **extra,
            "input": [
                {"role": "developer", "content": [developer]},
                {"role": "user", "content": content},
            ],
            "text": text,
        }

    def dispatch_background(
        self,
        plan: RequestPlan,
        schema: type[ModelT],
        choice: ModelChoice,
        *,
        metadata: dict[str, str],
    ) -> BackgroundDispatch:
        """Send one request with the server told to answer it in the background."""
        from openai import APIError

        sent: dict[str, Any] = {
            **self._request(plan, schema, choice),
            "max_output_tokens": plan.max_tokens,
            "store": True,
            "background": True,
            "metadata": metadata,
        }
        try:
            response = self._client.with_options(max_retries=0).responses.create(**sent)
        except APIError as error:
            raise translate(
                error, self.name, "dispatch a background model request", creating=True
            ) from error
        response_id = str(_response_field(response, "id", "") or "")
        if not response_id:
            raise ProviderError("OpenAI returned no response id for the background request.")
        return BackgroundDispatch(response_id=response_id, request_envelope=envelope_of(sent))

    def poll_background(
        self, response_id: str, schema: type[ModelT], ceiling: int
    ) -> BackgroundPoll:
        """Return the current state of a dispatched response, without raising on failure."""
        from openai import APIError

        try:
            # Retrieve is idempotent, so the client's own retry count applies;
            # only a create is at risk of double-dispatching on retry.
            response = self._client.responses.retrieve(
                response_id, include=["web_search_call.action.sources"]
            )
        except APIError as error:
            raise translate(
                error, self.name, "poll a background model request", not_found="response"
            ) from error
        raw = envelope_of(response) or {}
        status = _response_field(response, "status", "")
        if status in ("queued", "in_progress"):
            return BackgroundPoll(status="pending", raw=raw)
        try:
            value, record = _response_result(
                response, schema, "OpenAI", ceiling, record_from=_record, text_from=_text
            )
        except UnusableResponseError as error:
            return BackgroundPoll(status="unusable", record=error.record, error=str(error), raw=raw)
        return BackgroundPoll(status="complete", value=value, record=record, raw=raw)

    def cancel_background(self, response_id: str) -> None:
        """Ask OpenAI to stop a dispatched response that has not finished."""
        from openai import APIError

        try:
            self._client.responses.cancel(response_id)
        except APIError as error:
            raise translate(
                error, self.name, "cancel a background model request", not_found="response"
            ) from error

    def available_models(self) -> frozenset[str] | None:
        """List the models this account can use, so a typo is caught early."""
        from openai import APIError

        try:
            return model_identifiers(self._client.models.list())
        except APIError as error:
            raise translate(error, self.name, "list the models on your account") from error

    def check_credentials(self) -> None:
        """Confirm the key works by listing models, which bills no tokens."""
        from openai import APIError

        try:
            self._client.models.list()
        except APIError as error:
            raise translate(error, self.name, "verify that key") from error


def _timed_out(tier: ServiceTier) -> ProviderError:
    """Return the error for a request that stopped answering before it finished."""
    if tier is ServiceTier.FLEX:
        return _stalled_on_flex("stopped answering before the response finished")
    return ProviderError(
        "OpenAI stopped answering before the response finished, so tailorcv could not\n"
        "complete this model request. Try again shortly."
    )


def _stalled_on_flex(what_happened: str) -> CapacityError:
    """Return the flex failure the client answers by resending on standard at once."""
    stalled = CapacityError(
        f"OpenAI's discounted tier {what_happened},\n"
        "so tailorcv could not complete this model request on it."
    )
    stalled.stalled = True
    return stalled


def _throttled_on_flex(translated: TailorcvError) -> CapacityError:
    """Return the flex failure the client answers by backing off on flex before standard."""
    throttled = CapacityError(str(translated))
    throttled.category = translated.category
    return throttled


def _transient(error: Exception) -> bool:
    """Return whether flex stopped answering: a server error or no reply."""
    from openai import APIConnectionError

    status = getattr(error, "status_code", None)
    return isinstance(error, APIConnectionError) or (isinstance(status, int) and status >= 500)


def _throttled(error: Exception) -> bool:
    """Return whether flex asked for a slower pace: a rate limit, request timeout, or conflict."""
    return getattr(error, "status_code", None) in _THROTTLE_STATUSES


def _flex_refusal(error: Exception) -> bool:
    """Return whether flex refused for capacity before any output: worth backing off and resending.

    A refusal after the HTTP status counts only when its code or wording names capacity.
    """
    if getattr(error, "after_output", False):
        return False
    if _throttled(error):
        return True
    if isinstance(getattr(error, "status_code", None), int):
        return False
    code = getattr(error, "code", None)
    message = str(getattr(error, "message", "") or "")
    return code in _CAPACITY_CODES or _CAPACITY_WORDING.search(message) is not None


def _unbilled_failure(response: Any, *, after_output: bool) -> bool:
    """Return whether the response failed unbilled, for capacity, before any output."""
    if _response_field(response, "status", None) != "failed":
        return False
    if _response_field(response, "usage", None) is not None:
        return False
    error: Any = _response_field(response, "error", None)
    code = _response_field(error, "code", None)
    message = str(_response_field(error, "message", "") or "")
    return _flex_refusal(_StreamError(code, message, after_output=after_output))


def _failed_unbilled(response: Any) -> ProviderError:
    """Return the error naming why an unbilled response failed."""
    error: Any = _response_field(response, "error", None)
    code = _response_field(error, "code", "")
    message = _response_field(error, "message", "") or "no reason given"
    named = f" ({code})" if code else ""
    return ProviderError(
        "OpenAI failed the response before billing it, so tailorcv could not\n"
        f"complete this model request. OpenAI said{named}: {message}"
    )


def _terminal_response(events: Any) -> tuple[Any, bool]:
    """Return the terminal event's response and whether output preceded it.

    A stream error, whether the SDK raised it or yielded it as an event, leaves
    tagged with whether output preceded it.
    """
    from openai import APIError

    output = False
    try:
        for event in events:
            kind = _response_field(event, "type", "")
            if kind in {"response.completed", "response.failed", "response.incomplete"}:
                return event.response, output
            if kind == "error":
                code = _response_field(event, "code", None)
                message = _response_field(event, "message", "") or "The response stream failed."
                raise _StreamError(code, str(message), after_output=output)
            output = output or kind not in _PREAMBLE_EVENTS
    except APIError as error:
        error.after_output = output  # type: ignore[attr-defined]
        raise
    raise ProviderError("OpenAI ended the response stream without a terminal event.")


def _web_search_tool(policy: SearchPolicy) -> dict[str, Any]:
    """Render the policy as the Responses web-search tool.

    ``caller`` has no equivalent here: OpenAI runs the search itself.
    """
    tool: dict[str, Any] = {
        "type": "web_search",
        "search_context_size": policy.depth.value,
        "external_web_access": policy.live,
    }
    filters: dict[str, Any] = {}
    if policy.allowed_domains:
        filters["allowed_domains"] = list(policy.allowed_domains)
    if policy.blocked_domains:
        filters["blocked_domains"] = list(policy.blocked_domains)
    if filters:
        tool["filters"] = filters
    if policy.location is not None and (stated := policy.location.stated()):
        tool["user_location"] = {"type": "approximate", **stated}
    return tool


def _searches(response: Any) -> tuple[Search, ...]:
    """Pull the queries and the pages they returned out of the output items."""
    output: list[Any] | None = _response_field(response, "output", None)
    found: list[Search] = []
    for item in output or []:
        if _response_field(item, "type", "") != "web_search_call":
            continue
        action: Any = _response_field(item, "action", None)
        if _response_field(action, "type", "") != "search":
            continue
        found.append(
            Search(
                queries=_queries(action),
                sources=_sources(action),
                citations=_citations(action),
            )
        )
    return tuple(found)


def _page_actions(response: Any) -> tuple[PageAction, ...]:
    """Return the pages a hosted search opened or read within, not queried."""
    output: list[Any] | None = _response_field(response, "output", None)
    found: list[PageAction] = []
    for item in output or []:
        if _response_field(item, "type", "") != "web_search_call":
            continue
        action: Any = _response_field(item, "action", None)
        kind = str(_response_field(action, "type", "") or "")
        if kind not in ("open_page", "find_in_page"):
            continue
        found.append(
            PageAction(
                kind=kind,
                url=str(_response_field(action, "url", "") or ""),
                pattern=str(_response_field(action, "pattern", "") or ""),
                status=str(_response_field(item, "status", "") or ""),
            )
        )
    return tuple(found)


def _search_count(response: Any) -> int:
    """Return how many hosted calls actually searched, excluding page actions."""
    output: list[Any] | None = _response_field(response, "output", None)
    count = 0
    for item in output or []:
        if _response_field(item, "type", "") != "web_search_call":
            continue
        action: Any = _response_field(item, "action", None)
        if _response_field(action, "type", "") == "search":
            count += 1
    return count


def _citations(raw: Any) -> tuple[str, ...]:
    """Return the identifiers a tool action cited, dropping any it cannot name.

    Serializing an unnamed one whole would put a quoted passage in the record.
    """
    found: list[str] = []
    values: Any = _response_field(raw, "citations", None)
    if not isinstance(values, list):
        return ()
    for value in values:
        if isinstance(value, str):
            found.append(value)
            continue
        if isinstance(value, (int, float, bool)):
            found.append(str(value))
            continue
        for field in ("id", "citation_id", "anchor"):
            named: Any = _response_field(value, field, None)
            if named is not None:
                found.append(str(named))
                break
    return tuple(found)


def _queries(action: Any) -> tuple[str, ...]:
    """Return the search text as it arrived: one query, or several kept separate."""
    # A multi-query action also repeats its first query alone in `query`.
    several: Any = _response_field(action, "queries", None)
    if isinstance(several, list) and several:
        return tuple(str(item) for item in several)
    single: object | None = _response_field(action, "query", None)
    return (str(single),) if single else ()


def _sources(action: Any) -> tuple[SearchSource, ...]:
    """Return the pages one search read, when they were asked for."""
    sources: Any = _response_field(action, "sources", None)
    if not isinstance(sources, list):
        return ()
    return tuple(
        SearchSource(
            url=str(_response_field(item, "url", "") or ""),
            title=str(_response_field(item, "title", "") or ""),
            citations=_citations(item),
        )
        for item in sources
    )


def _text(response: Any) -> str | None:
    """Return the assistant's text, or ``None`` when it produced none."""
    collected: object | None = _response_field(response, "output_text", None)
    if collected:
        return str(collected)
    parts: list[str] = []
    output: list[Any] | None = _response_field(response, "output", None)
    for item in output or []:
        content: list[Any] | None = _response_field(item, "content", None)
        for part in content or []:
            if _response_field(part, "type", "") == "output_text":
                parts.append(str(part.text))
    return "".join(parts) or None


def _record(response: Any, request: Mapping[str, Any] | None = None) -> CallRecord:
    """Pull everything OpenAI said about the call out of the response object."""
    service_tier: object | None = _response_field(response, "service_tier", None)
    searches = _searches(response)
    search_count = _search_count(response)
    page_actions = _page_actions(response)
    usage, usage_known = _usage(_response_field(response, "usage", None))
    return replace(
        _response_record(
            response,
            usage,
            _finish(response),
            service_tier,
            request,
        ),
        searches=searches,
        search_count=search_count,
        page_actions=page_actions,
        page_action_count=len(page_actions),
        tool_version="web_search" if search_count or page_actions else "",
        usage_known=usage_known,
    )


def _finish(response: Any) -> Finish:
    """Translate the Responses API's status into the shared vocabulary.

    ``incomplete`` carries the reason that matters: without reading it, a
    truncated response and a content-filtered one are both just empty output.
    """
    status: str | None = _response_field(response, "status", None)
    if status in (None, "completed"):
        return Finish.REFUSED if _refused(response) else Finish.COMPLETE
    if status == "failed":
        return Finish.FAILED
    if status == "cancelled":
        return Finish.CANCELLED
    if status == "incomplete":
        details: object | None = _response_field(response, "incomplete_details", None)
        reason = _response_field(details, "reason", "")
        if reason == "max_output_tokens":
            return Finish.TRUNCATED
        if reason == "content_filter":
            return Finish.FILTERED
        return Finish.UNKNOWN
    return Finish.UNKNOWN


def _refused(response: Any) -> bool:
    """Return whether the model answered with an explicit refusal part."""
    output: list[Any] | None = _response_field(response, "output", None)
    for item in output or []:
        content: list[Any] | None = _response_field(item, "content", None)
        for part in content or []:
            if _response_field(part, "type", "") == "refusal":
                return True
    return False


def _usage(raw: Any) -> tuple[Usage, bool]:
    """Normalize reported counts and identify missing or inconsistent accounting."""
    inputs = _response_field(raw, "input_tokens_details", None)
    outputs = _response_field(raw, "output_tokens_details", None)
    usage, known = _reported_usage(
        input_tokens=_response_field(raw, "input_tokens", None),
        output_tokens=_response_field(raw, "output_tokens", None),
        cache_read_tokens=_response_field(inputs, "cached_tokens", 0),
        cache_write_tokens=_response_field(inputs, "cache_write_tokens", 0),
        reasoning_tokens=_response_field(outputs, "reasoning_tokens", 0),
    )
    cached = usage.cache_read_tokens + usage.cache_write_tokens
    total: object = _response_field(raw, "total_tokens", None)
    known = (
        known
        and cached <= usage.input_tokens
        and usage.reasoning_tokens <= usage.output_tokens
        and (
            total is None
            or (type(total) is int and total == usage.input_tokens + usage.output_tokens)
        )
    )
    return usage.model_copy(update={"input_tokens": max(0, usage.input_tokens - cached)}), known
