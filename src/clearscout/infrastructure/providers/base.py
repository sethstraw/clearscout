"""The one interface the pipeline is allowed to know about a model.

Callers work in Pydantic models and plain text; everything behind this
interface is SDK-specific.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Protocol, TypeVar, cast, runtime_checkable
from weakref import WeakKeyDictionary

from pydantic import BaseModel, ValidationError

from clearscout.application.config import ModelChoice, ProviderName
from clearscout.infrastructure.providers.plan import RequestPlan
from clearscout.shared.costs import Usage
from clearscout.shared.errors import ProviderError

ModelT = TypeVar("ModelT", bound=BaseModel)
_FieldT = TypeVar("_FieldT")
_ResponseT = TypeVar("_ResponseT")
_JSON_SCHEMAS: WeakKeyDictionary[type[BaseModel], dict[str, dict[str, Any]]] = WeakKeyDictionary()

# Anthropic's structured outputs support no numeric bound, no string-length
# bound, and no array bound beyond `minItems` of 0 or 1.
_UNBOUNDED_BY_ANTHROPIC = (
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "maxItems",
)

# Both vendors' structured outputs accept only the string formats they document;
# any other value, such as the "uri" a pydantic URL renders, is rejected by the
# endpoint rather than ignored, so it is moved into words on every wire path.
_FORMATS_DOCUMENTED = frozenset(
    {"date-time", "time", "date", "duration", "email", "hostname", "ipv4", "ipv6", "uuid"}
)


def json_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """Return the strict JSON Schema rendered from one model, optional fields nullable."""
    rendered = _rendered(schema, "strict", _without_undocumented_formats)
    _refuse_past_openai_enum_limits(schema.__name__, rendered)
    return rendered


# OpenAI strict mode's documented enum caps; a vocabulary over a very large
# master CV can pass them, and the endpoint then rejects every request.
_OPENAI_ENUM_VALUES = 1000
_OPENAI_LARGE_ENUM = 250
_OPENAI_LARGE_ENUM_CHARACTERS = 15_000


def _refuse_past_openai_enum_limits(name: str, schema: dict[str, Any]) -> None:
    """Raise before sending a schema whose enums OpenAI strict mode would reject."""
    enums: list[list[Any]] = []
    _collect_enums(schema, enums)
    total = sum(len(values) for values in enums)
    widest = max(
        (
            sum(len(value) for value in values)
            for values in enums
            if len(values) > _OPENAI_LARGE_ENUM and all(isinstance(v, str) for v in values)
        ),
        default=0,
    )
    if total > _OPENAI_ENUM_VALUES or widest > _OPENAI_LARGE_ENUM_CHARACTERS:
        refused = ProviderError(
            f"The {name} response schema passes OpenAI's structured-output enum limit: "
            f"{total:,} enum values (limit {_OPENAI_ENUM_VALUES:,}), and {widest:,} characters "
            f"in its largest enum (limit {_OPENAI_LARGE_ENUM_CHARACTERS:,}).\n"
            "  The document this stage reads names more entries or lines than one "
            "request can list; nothing was sent."
        )
        refused.certainly_not_created = True
        raise refused


def _collect_enums(node: Any, found: list[list[Any]]) -> None:
    """Append every ``enum`` list in ``node`` to ``found``."""
    if isinstance(node, list):
        for item in node:
            _collect_enums(item, found)
        return
    if not isinstance(node, dict):
        return
    values = node.get("enum")
    if isinstance(values, list):
        found.append(values)
    for value in node.values():
        _collect_enums(value, found)


def wire_schema(provider: ProviderName, schema: type[BaseModel]) -> dict[str, Any]:
    """Return the document this vendor is actually sent.

    Asked by the adapter that sends it and by the cache key and run manifest
    that describe it, so the record cannot come to disagree with the request.
    """
    if provider is ProviderName.ANTHROPIC:
        return anthropic_json_schema(schema)
    return json_schema(schema)


def anthropic_json_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """Narrow the model to Anthropic's subset, restating a lost bound in the description."""
    return _rendered(schema, "anthropic", _without_unsupported_bounds)


def _rendered(
    schema: type[BaseModel], dialect: str, narrow: Callable[[dict[str, Any]], dict[str, Any]]
) -> dict[str, Any]:
    """Return one model's document for one vendor, rendered at most once."""
    cached = _JSON_SCHEMAS.setdefault(schema, {})
    prepared = cached.get(dialect)
    if prepared is None:
        strict: dict[str, Any] = _strict(schema.model_json_schema())
        prepared = narrow(_share_enums(strict))
        cached[dialect] = prepared
    return prepared


def _without_undocumented_formats(node: Any) -> Any:
    """Return ``node`` with every format the endpoint rejects moved into words.

    The response is still validated against the unnarrowed model, which is
    what actually enforces the format.
    """
    if isinstance(node, list):
        return [_without_undocumented_formats(item) for item in node]
    if not isinstance(node, dict):
        return node
    dropped = _undocumented_format(node)
    result = {
        key: _without_undocumented_formats(value)
        for key, value in node.items()
        if dropped is None or key != "format"
    }
    if dropped is not None:
        _state_in_words(result, [f"A string in {dropped} format."])
    return result


def _without_unsupported_bounds(node: Any) -> Any:
    """Return ``node`` with every bound or format Anthropic rejects moved into words.

    Anthropic rejects an undocumented ``format`` (the "uri" a pydantic URL
    renders) the same way OpenAI does, so it is stripped on this path too.
    """
    if isinstance(node, list):
        return [_without_unsupported_bounds(item) for item in node]
    if not isinstance(node, dict):
        return node
    dropped = _undocumented_format(node)
    stated = _bounds_in_words(node)
    if dropped is not None:
        stated.append(f"A string in {dropped} format.")
    result = {
        key: _without_unsupported_bounds(value)
        for key, value in node.items()
        if key not in _UNBOUNDED_BY_ANTHROPIC
        and not (key == "minItems" and value not in (0, 1))
        and not (key == "format" and dropped is not None)
    }
    if stated:
        _state_in_words(result, stated)
    return result


def _undocumented_format(node: dict[str, Any]) -> str | None:
    """Return the string ``format`` no vendor documents, or None when it is safe."""
    value = node.get("format")
    if value is None or value in _FORMATS_DOCUMENTED:
        return None
    return str(value)


def _state_in_words(result: dict[str, Any], additions: list[str]) -> None:
    """Fold restated bounds and formats into a node's description.

    A ``description`` is a string on a well-formed schema, but a hand-built
    node can carry a dict there, so coerce before joining rather than crash.
    """
    existing = result.get("description", "")
    if not isinstance(existing, str):
        existing = str(existing)
    result["description"] = " ".join(part for part in (existing, *additions) if part)


def _bounds_in_words(node: dict[str, Any]) -> list[str]:
    """Return the bounds this node states, as sentences a model can follow."""
    # A minItems of 0 or 1 survives as a keyword, so it is not restated here.
    counted = node.get("minItems") if node.get("minItems") not in (None, 0, 1) else None
    stated = [
        *_span(node.get("minimum"), node.get("maximum"), ""),
        *_span(node.get("minLength"), node.get("maxLength"), " characters"),
        *_span(counted, node.get("maxItems"), " items"),
    ]
    if (value := node.get("exclusiveMinimum")) is not None:
        stated.append(f"Greater than {value}.")
    if (value := node.get("exclusiveMaximum")) is not None:
        stated.append(f"Less than {value}.")
    if (value := node.get("multipleOf")) is not None:
        stated.append(f"A multiple of {value}.")
    return stated


def _span(low: Any, high: Any, unit: str) -> list[str]:
    """Return one inclusive range as a sentence, or nothing when there is none."""
    if low is not None and high is not None:
        return [f"Between {low} and {high}{unit}."]
    if low is not None:
        return [f"At least {low}{unit}."]
    if high is not None:
        return [f"At most {high}{unit}."]
    return []


def _share_enums(schema: dict[str, Any]) -> dict[str, Any]:
    """Emit each repeated enum once and reference it everywhere else.

    Pydantic inlines a ``Literal`` at every use site, and these vocabularies are
    runtime literals over every entry in a master CV, so a long record repeats a
    several-hundred-value enum against provider limits that count enum values
    and characters across the whole document.
    """
    counts: dict[str, int] = defaultdict(int)
    _walk_enums(schema, lambda key: counts.__setitem__(key, counts[key] + 1))
    shared = {key for key, count in counts.items() if count > 1}
    if not shared:
        return schema

    names = {key: f"clearscout_enum_{index}" for index, key in enumerate(sorted(shared))}
    # Add definitions before rewriting references.
    _walk_enums(schema, None, names)
    definitions = schema.setdefault("$defs", {})
    for key, name in names.items():
        definitions[name] = {"type": "string", "enum": json.loads(key)}
    return schema


def _walk_enums(
    node: Any,
    seen: Callable[[str], None] | None = None,
    replace: dict[str, str] | None = None,
) -> None:
    """Visit every object carrying an ``enum``, counting or rewriting it."""
    if isinstance(node, list):
        for item in node:
            _walk_enums(item, seen, replace)
        return
    if not isinstance(node, dict):
        return
    values = node.get("enum")
    # A one-value enum is a constant, not a reusable vocabulary.
    if isinstance(values, list) and len(values) > 1 and all(isinstance(v, str) for v in values):
        key = json.dumps(values, sort_keys=True)
        if seen is not None:
            seen(key)
        elif replace is not None and key in replace:
            node.clear()
            node["$ref"] = f"#/$defs/{replace[key]}"
            return
    for value in node.values():
        _walk_enums(value, seen, replace)


def _strict(node: Any) -> Any:
    """Return ``node`` with strict-mode rules applied to every object in it."""
    if isinstance(node, list):
        return [_strict(item) for item in node]
    if not isinstance(node, dict):
        return node
    # Remove defaults so required fields remain required to the model.
    result = {key: _strict(value) for key, value in node.items() if key != "default"}
    if result.get("type") == "object" and "properties" in result:
        result["additionalProperties"] = False
        result["required"] = list(result["properties"])
    return result


class Finish(StrEnum):
    """Why a response stopped, normalized across two vendors.

    Normalized does not mean symmetric: ``filtered``, ``failed``, and
    ``cancelled`` are OpenAI Responses API states with no Anthropic equivalent.
    """

    COMPLETE = "complete"
    TRUNCATED = "truncated"
    REFUSED = "refused"
    FILTERED = "filtered"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SearchSource:
    """One page a hosted search consulted."""

    url: str
    title: str = ""
    citations: tuple[str, ...] = ()


@dataclass(frozen=True)
class Search:
    """One hosted search: what was asked, and what it read.

    A URL and a title only. A passage quoted out of a page is content rather
    than provenance.
    """

    queries: tuple[str, ...] = ()
    sources: tuple[SearchSource, ...] = ()
    citations: tuple[str, ...] = ()

    def stated(self) -> dict[str, Any]:
        """Return this search as the run manifest records it."""
        return {
            "queries": list(self.queries),
            "sources": [
                {"url": item.url, "title": item.title, "citations": list(item.citations)}
                for item in self.sources
            ],
            "citations": list(self.citations),
        }


@dataclass(frozen=True)
class PageAction:
    """One hosted-search step that read a page rather than asking a query."""

    kind: str
    url: str
    pattern: str = ""
    # The tool call's own status; "completed" is the only one that read the page.
    status: str = ""

    def stated(self) -> dict[str, Any]:
        """Return this page action as the run manifest records it."""
        return {"kind": self.kind, "url": self.url, "pattern": self.pattern, "status": self.status}


@dataclass(frozen=True)
class CallRecord:
    """Everything a provider said about one call except its answer.

    Carried out of the adapter rather than logged inside it, so the caching
    layer, the ledger, and the live narration all see the same facts.
    """

    usage: Usage
    model: str = ""
    response_id: str = ""
    finish: Finish = Finish.COMPLETE
    service_tier: str = ""
    tool_version: str = ""
    searches: tuple[Search, ...] = ()
    search_count: int = 0
    page_actions: tuple[PageAction, ...] = ()
    page_action_count: int = 0
    usage_known: bool = True
    # The request envelope carries the prompt text, including CV evidence.
    request_envelope: dict[str, Any] | None = None
    response_envelope: dict[str, Any] | None = None


@dataclass(frozen=True)
class BackgroundPoll:
    """One check of a dispatched background response, with its raw body."""

    status: Literal["pending", "complete", "unusable"]
    value: BaseModel | None = None
    record: CallRecord | None = None
    error: str = ""
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class BackgroundDispatch:
    """The provider's id for a dispatched background response and the request that created it."""

    response_id: str
    # The request envelope carries the prompt text, including CV evidence.
    request_envelope: dict[str, Any] | None


def _response_field(response: object | None, name: str, default: _FieldT) -> _FieldT:
    """Read one optional field from an SDK response without leaking ``Any``."""
    if response is None:
        return default
    if isinstance(response, Mapping):
        return cast(_FieldT, response.get(name, default))
    return cast(_FieldT, getattr(response, name, default))


def _json_envelope(value: Any) -> Any:
    """Render an SDK object as JSON-able data, keeping every field it carries.

    An SDK model dumps itself; anything the SDK does not model is walked, and a
    value with no JSON form becomes its own repr rather than failing the dump.
    """
    dumper = getattr(value, "model_dump", None)
    if callable(dumper):
        return dumper(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _json_envelope(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_envelope(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def envelope_of(value: object) -> dict[str, Any] | None:
    """Return one request or response as a JSON object, or None when it is not one."""
    rendered = _json_envelope(value)
    return rendered if isinstance(rendered, dict) else None


def _response_record(
    response: object,
    usage: Usage,
    finish: Finish,
    service_tier: object | None,
    request: Mapping[str, Any] | None = None,
) -> CallRecord:
    """Build the vendor-neutral record fields carried by both SDK responses."""
    return CallRecord(
        usage=usage,
        model=str(_response_field(response, "model", "") or ""),
        response_id=str(_response_field(response, "id", "") or ""),
        finish=finish,
        service_tier=str(service_tier or ""),
        request_envelope=None if request is None else envelope_of(request),
        response_envelope=envelope_of(response),
    )


def _reported_usage(**counts: object) -> tuple[Usage, bool]:
    """Keep valid reported token counts without treating missing accounting as free."""
    counts = {
        key: 0 if value is None and key not in {"input_tokens", "output_tokens"} else value
        for key, value in counts.items()
    }
    valid = {key: value for key, value in counts.items() if type(value) is int and value >= 0}
    return Usage(**valid), len(valid) == len(counts)


class UnusableResponseError(ProviderError):
    """A response that arrived, was billed for, and still cannot be used.

    Carries the record so the caller can put the call in the ledger before the
    error travels: a request that spent tokens and left no row is
    indistinguishable from one that never happened.
    """

    def __init__(self, message: str, record: CallRecord) -> None:
        """Bind the provider's own account of the call to the failure."""
        super().__init__(message)
        self.record = record


def refuse_unusable(record: CallRecord, provider: str, ceiling: int) -> None:
    """Raise a specific error when a response cannot possibly be parsed.

    Called before schema validation so the message names the actual cause.
    Truncation matters most: reasoning is billed as output and counted inside
    it, so a model can spend a whole ceiling thinking and return nothing.
    """
    if record.finish is Finish.TRUNCATED:
        thinking = record.usage.reasoning_tokens
        spent = (
            f" It used {thinking:,} of them on reasoning, leaving "
            f"{record.usage.answer_tokens:,} for the answer."
            if thinking
            else ""
        )
        raise UnusableResponseError(
            f"{provider} stopped at the {ceiling:,} token output limit before "
            f"finishing the object.{spent}\n"
            "  Raise max_tokens for this stage, or lower the effort so less of "
            "the budget goes to reasoning.",
            record,
        )
    if record.finish is Finish.REFUSED:
        raise UnusableResponseError(
            f"{provider} declined to answer this request.\n"
            "  This is a prompt problem, not a transient one, so retrying will "
            "not help. The posting or the CV contains something the model will "
            "not work with.",
            record,
        )
    if record.finish is Finish.FILTERED:
        raise UnusableResponseError(
            f"{provider} stopped this response on a content filter.\n"
            "  Retrying will not help. Check the posting text for anything that "
            "would trip a safety classifier.",
            record,
        )
    if record.finish is Finish.FAILED:
        raise UnusableResponseError(
            f"{provider} reported the response itself as failed, with no usable "
            "output. This is the provider's end; try again shortly.",
            record,
        )
    if record.finish is Finish.CANCELLED:
        raise UnusableResponseError(
            f"{provider} cancelled this response before it finished.",
            record,
        )
    if record.finish is Finish.UNKNOWN:
        raise UnusableResponseError(
            f"{provider} gave a reason for stopping that this version does not "
            "recognise, so the response cannot be treated as finished.\n"
            "  Generation was paused, cancelled, or ended for a reason the "
            "vendor has added since. One retry is worth trying; if it repeats, "
            "the adapter has to learn the new reason.",
            record,
        )


def _response_result(
    response: _ResponseT,
    schema: type[ModelT],
    provider: str,
    ceiling: int,
    *,
    record_from: Callable[[_ResponseT], CallRecord],
    text_from: Callable[[_ResponseT], str | None],
) -> tuple[ModelT, CallRecord]:
    """Refuse an unusable response before parsing its structured text."""
    record = record_from(response)
    refuse_unusable(record, provider, ceiling)
    try:
        value = parse_or_raise(schema, text_from(response), provider)
    except ProviderError as error:
        raise UnusableResponseError(str(error), record) from error
    return value, record


class Provider(Protocol):
    """The core structured-completion boundary every model provider supplies.

    Vendor-specific server tools use separate runtime-checkable capability
    protocols, so a caller cannot silently ask an adapter for a tool it does
    not implement.
    """

    name: ProviderName

    def complete(
        self,
        plan: RequestPlan,
        schema: type[ModelT],
        choice: ModelChoice,
    ) -> tuple[ModelT, CallRecord]:
        """Return a validated instance of the schema and how the call went."""
        ...

    def check_credentials(self) -> None:
        """Raise AuthError when the key is unusable."""
        ...

    def available_models(self) -> frozenset[str] | None:
        """Return the model identifiers this account can use, or None when it would not say."""
        ...


@runtime_checkable
class WebSearchProvider(Provider, Protocol):
    """A provider that can execute web search on its own infrastructure."""

    def complete_with_web_search(
        self,
        plan: RequestPlan,
        schema: type[ModelT],
        choice: ModelChoice,
    ) -> tuple[ModelT, CallRecord]:
        """Return one structured answer informed by provider-hosted web search."""
        ...


@runtime_checkable
class BackgroundWebSearchProvider(WebSearchProvider, Protocol):
    """A provider that can dispatch a hosted-search request and poll it later."""

    def dispatch_background(
        self,
        plan: RequestPlan,
        schema: type[ModelT],
        choice: ModelChoice,
        *,
        metadata: dict[str, str],
    ) -> BackgroundDispatch:
        """Send one request in the background and return its response id and request."""
        ...

    def poll_background(
        self, response_id: str, schema: type[ModelT], ceiling: int
    ) -> BackgroundPoll:
        """Return the current state of a dispatched response."""
        ...

    def cancel_background(self, response_id: str) -> None:
        """Ask the provider to stop a dispatched response that is still running."""
        ...


def provider_for(name: ProviderName, key: str, timeout: float | None = None) -> Provider:
    """Construct a provider, importing only the SDK it needs.

    The import is local so that a broken or absent SDK for one vendor never
    blocks someone using the other.
    """
    from clearscout.infrastructure.providers.capture import (
        CapturingProvider,
        WireRecorder,
        capture_root,
    )

    root = capture_root()
    transport = None if root is None else WireRecorder(root)
    if name is ProviderName.ANTHROPIC:
        from clearscout.infrastructure.providers.anthropic import AnthropicProvider

        built: Provider = AnthropicProvider(api_key=key, timeout=timeout, transport=transport)
    else:
        from clearscout.infrastructure.providers.openai import OpenAIProvider

        built = OpenAIProvider(api_key=key, timeout=timeout, transport=transport)
    return built if root is None else CapturingProvider(built, root)


def model_identifiers(listing: Any) -> frozenset[str] | None:
    """Pull model identifiers out of either vendor's listing response.

    Both SDKs return a page object with a ``data`` list of records carrying an
    ``id``, so one reader serves both. ``None`` means the provider would not say.
    """
    data: list[object] | None = _response_field(listing, "data", None)
    if not data:
        return None
    identifiers: set[str] = set()
    for item in data:
        identifier: object | None = _response_field(item, "id", None)
        if identifier:
            identifiers.add(str(identifier))
    return frozenset(identifiers)


def parse_or_raise(schema: type[ModelT], payload: object, provider: str) -> ModelT:
    """Turn a provider's parsed payload into a validated model or a clear error."""
    if payload is None:
        raise ProviderError(
            f"The {provider} response contained no structured output. "
            "This usually means the model refused or hit its output limit."
        )
    if isinstance(payload, schema):
        return payload
    try:
        if isinstance(payload, str):
            return schema.model_validate_json(payload)
        return schema.model_validate(payload)
    except ValidationError as error:
        raise ProviderError(
            f"The {provider} response did not match the expected schema:\n{error}"
        ) from error
