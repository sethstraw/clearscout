"""The provider boundary: caching, accounting, key resolution, and validation."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import AnyHttpUrl, BaseModel, Field

from tailorcv.application.config import Effort, ModelChoice, Stage
from tailorcv.application.tailoring.stages.jobspec import requirement_vocabulary
from tailorcv.application.tailoring.stages.match import response_schema as match_schema
from tailorcv.infrastructure.providers.base import (
    CallRecord,
    Finish,
    _response_field,
    _response_record,
    _response_result,
    _without_undocumented_formats,
    _without_unsupported_bounds,
    anthropic_json_schema,
    json_schema,
    parse_or_raise,
)
from tailorcv.infrastructure.providers.cache import ResponseCache
from tailorcv.infrastructure.providers.client import Client
from tailorcv.infrastructure.providers.plan import SearchPolicy
from tailorcv.shared.costs import Ledger, Usage
from tailorcv.shared.errors import ProviderError
from tests.support.plans import plan_for
from tests.support.schema import production_schemas
from tests.support.suite import SUITE_PROVIDER


class Answer(BaseModel):
    value: str


class CountingProvider:
    # The provider the suite exercises, because these tests are about caching
    # and accounting rather than about a vendor.
    name = SUITE_PROVIDER

    def __init__(self) -> None:
        self.calls = 0
        self.choices: list[ModelChoice] = []

    def complete(self, plan, schema, choice):
        self.calls += 1
        self.choices.append(choice)
        return schema.model_validate({"value": "ok"}), CallRecord(
            usage=Usage(input_tokens=7, output_tokens=3), model=choice.model
        )

    def check_credentials(self) -> None:
        return None

    def available_models(self):
        return frozenset({"claude-sonnet-5", "gpt-5.6-terra"})


OPENED: list[ResponseCache] = []


@pytest.fixture(autouse=True)
def _close_caches():
    """Close every cache a test opened, so the suite leaks no sqlite connection."""
    yield
    while OPENED:
        OPENED.pop().close()


def build(settings, tmp_path, enabled: bool) -> tuple[Client, CountingProvider]:
    provider = CountingProvider()
    cache = ResponseCache(enabled=enabled, directory=tmp_path / "cache")
    OPENED.append(cache)
    return (
        Client(
            provider=provider,
            settings=settings,
            cache=cache,
            ledger=Ledger(),
        ),
        provider,
    )


def test_a_second_identical_call_is_served_from_cache(settings, tmp_path) -> None:
    client, provider = build(settings, tmp_path, enabled=True)
    plan = plan_for()
    assert client.complete(plan, Answer).value == "ok"
    assert client.complete(plan, Answer).value == "ok"
    assert provider.calls == 1
    assert client.ledger.entries[1].cached is True


def test_a_different_prompt_is_not_served_from_cache(settings, tmp_path) -> None:
    client, provider = build(settings, tmp_path, enabled=True)
    client.complete(plan_for(user="a"), Answer)
    client.complete(plan_for(user="b"), Answer)
    assert provider.calls == 2


def test_usage_is_recorded_per_stage(settings, tmp_path) -> None:
    client, _ = build(settings, tmp_path, enabled=False)
    client.complete(plan_for(stage=Stage.JOBSPEC), Answer)
    assert client.ledger.entries[0].stage == "jobspec"
    assert client.ledger.total_usage.input_tokens == 7


def test_web_search_refuses_a_provider_without_that_capability(settings, tmp_path) -> None:
    client, provider = build(settings, tmp_path, enabled=False)

    with pytest.raises(ProviderError, match="does not support provider-hosted web search"):
        client.complete(
            plan_for(stage=Stage.GUIDED_CAMPAIGN, search=SearchPolicy()),
            Answer,
        )

    assert provider.calls == 0


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        # Both unusable, and they need different remedies, so they read apart.
        (None, "no structured output"),
        ({"wrong": 1}, "did not match the expected schema"),
    ],
)
def test_an_unusable_response_names_which_way_it_was_unusable(
    payload: object, message: str
) -> None:
    with pytest.raises(ProviderError, match=message):
        parse_or_raise(Answer, payload, "Anthropic")


def test_an_already_parsed_response_passes_through() -> None:
    assert parse_or_raise(Answer, Answer(value="x"), "Anthropic").value == "x"


def test_the_schema_sent_on_the_wire_is_the_same_schema_every_time() -> None:
    """The cache key is derived from this document, so it has to be stable.

    Not object identity: an implementation that stopped memoizing is still
    correct, and one whose key ordering moved would strand every cached answer.
    """

    class FreshAnswer(BaseModel):
        value: str

    first = json_schema(FreshAnswer)
    assert json.dumps(first, sort_keys=True) == json.dumps(json_schema(FreshAnswer), sort_keys=True)

    class OtherAnswer(BaseModel):
        value: int

    assert json.dumps(json_schema(OtherAnswer), sort_keys=True) != json.dumps(
        first, sort_keys=True
    ), "two different contracts must not key the same cache entry"


def test_no_production_schema_sends_anthropic_a_bound_it_documents_as_unsupported() -> None:
    """Anthropic accepts no numeric, length, or array bound beyond minItems 0 or 1.

    Every production response schema is swept, so a newly added bound fails here.
    """
    rejected = {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "maxItems",
    }

    for name, model in production_schemas().items():
        document = json.dumps(anthropic_json_schema(model))
        present = sorted(key for key in rejected if f'"{key}"' in document)
        assert present == [], f"{name} sends Anthropic {present}"
        for value in _values_for("minItems", anthropic_json_schema(model)):
            assert value in (0, 1), f"{name} sends Anthropic minItems {value}"


def test_no_production_schema_sends_openai_a_format_it_documents_as_unsupported() -> None:
    """OpenAI rejects a string ``format`` outside its documented set outright.

    A pydantic URL type emits ``format: "uri"``, which fails the call before it
    reaches the model, so this sweeps every production schema.
    """
    documented = {
        "date-time",
        "time",
        "date",
        "duration",
        "email",
        "hostname",
        "ipv4",
        "ipv6",
        "uuid",
    }

    for name, model in production_schemas().items():
        present = sorted(
            str(value)
            for value in _values_for("format", json_schema(model))
            if value not in documented
        )
        assert present == [], f"{name} sends OpenAI format {present}"


def test_a_format_openai_cannot_express_is_stated_in_words_instead() -> None:
    """Dropping the keyword silently would drop the instruction with it."""

    class Located(BaseModel):
        url: AnyHttpUrl = Field(description="Where the posting lives.")

    narrowed = json_schema(Located)["properties"]["url"]

    assert "format" not in narrowed
    assert narrowed["description"] == "Where the posting lives. A string in uri format."


def test_a_bound_anthropic_cannot_express_is_stated_in_words_instead() -> None:
    """Dropping the keyword silently would drop the instruction with it."""

    class Scored(BaseModel):
        axis: int = Field(ge=1, le=5, description="How well it reads.")
        notes: list[str] = Field(default_factory=list, max_length=3)

    narrowed = anthropic_json_schema(Scored)["properties"]

    assert narrowed["axis"]["description"] == "How well it reads. Between 1 and 5."
    assert narrowed["notes"]["description"] == "At most 3 items."
    # OpenAI supports all of them, so its document keeps the machine-checked form.
    assert json_schema(Scored)["properties"]["axis"]["maximum"] == 5


def test_a_format_anthropic_cannot_express_is_stated_in_words_instead() -> None:
    """Anthropic rejects an undocumented format too, so the wire path strips it."""

    class Located(BaseModel):
        url: AnyHttpUrl = Field(description="Where the posting lives.")

    narrowed = anthropic_json_schema(Located)["properties"]["url"]

    assert "format" not in narrowed
    assert narrowed["description"].startswith("Where the posting lives.")
    assert narrowed["description"].endswith("A string in uri format.")


@pytest.mark.parametrize(
    ("entries", "width"),
    [(900, 10), (300, 60)],
    ids=["too-many-values", "too-many-characters"],
)
def test_a_match_schema_past_openai_enum_limits_is_refused_before_sending(
    entries: int, width: int
) -> None:
    """OpenAI strict mode caps a schema at 1000 enum values and a large enum at 15,000 chars."""
    ids = tuple(f"b-{index:0{width - 2}d}" for index in range(entries))
    schema = match_schema(ids, requirement_vocabulary(3))

    with pytest.raises(ProviderError, match="enum limit") as refused:
        json_schema(schema)
    assert refused.value.certainly_not_created


def test_a_match_schema_inside_openai_enum_limits_renders() -> None:
    ids = tuple(f"b-{index:08d}" for index in range(800))

    rendered = json_schema(match_schema(ids, requirement_vocabulary(3)))

    assert "b-00000799" in json.dumps(rendered)


def test_no_production_schema_sends_anthropic_a_format_it_documents_as_unsupported() -> None:
    """The Anthropic wire path narrows formats the same way the OpenAI one does."""
    documented = {
        "date-time",
        "time",
        "date",
        "duration",
        "email",
        "hostname",
        "ipv4",
        "ipv6",
        "uuid",
    }

    for name, model in production_schemas().items():
        present = sorted(
            str(value)
            for value in _values_for("format", anthropic_json_schema(model))
            if value not in documented
        )
        assert present == [], f"{name} sends Anthropic format {present}"


def test_schema_sweeps_stringify_a_non_string_description_rather_than_crash() -> None:
    """A hand-built node can carry a dict description, and joining it must not raise.

    The narrow shape is not produced by a well-formed pydantic schema, so it is
    asserted directly against both sweeps rather than through a model.
    """
    node = {"type": "string", "format": "uri", "description": {"const": "raw"}}

    for narrow in (_without_undocumented_formats, _without_unsupported_bounds):
        result = narrow(dict(node))
        assert "format" not in result
        assert result["description"].endswith("A string in uri format.")
        assert isinstance(result["description"], str)


def _values_for(key: str, node: object) -> list[object]:
    """Return every value stored under one key anywhere in a document."""
    if isinstance(node, list):
        return [found for item in node for found in _values_for(key, item)]
    if not isinstance(node, dict):
        return []
    here = [node[key]] if key in node else []
    return here + [found for value in node.values() for found in _values_for(key, value)]


def test_sdk_fields_and_shared_call_record_preserve_response_metadata() -> None:
    response = SimpleNamespace(model="served-model", id="response-1", optional=None)
    record = _response_record(response, Usage(input_tokens=1), Finish.COMPLETE, "priority")
    fallback: object = object()

    assert record.model == "served-model"
    assert record.response_id == "response-1"
    assert record.service_tier == "priority"
    assert _response_field(response, "missing", fallback) is fallback
    assert _response_field(response, "optional", fallback) is None


def test_shared_response_processing_refuses_before_reading_partial_text() -> None:
    events: list[str] = []

    def record_from(response: object) -> CallRecord:
        events.append("record")
        return CallRecord(usage=Usage(), finish=Finish.TRUNCATED)

    def text_from(response: object) -> str:
        events.append("text")
        return '{"value": "partial"}'

    with pytest.raises(ProviderError, match="token output limit"):
        _response_result(
            object(),
            Answer,
            "OpenAI",
            100,
            record_from=record_from,
            text_from=text_from,
        )
    assert events == ["record"]


def test_the_configured_effort_reaches_the_provider(settings, tmp_path) -> None:
    client, provider = build(settings, tmp_path, enabled=False)
    client.complete(plan_for(stage=Stage.JOBSPEC), Answer)
    client.complete(plan_for(stage=Stage.DRAFT), Answer)
    assert [choice.effort for choice in provider.choices] == [Effort.LOW, Effort.MEDIUM]


def test_changing_effort_alone_invalidates_the_cache(settings, tmp_path) -> None:
    """Two efforts are two different answers, so they must not share a key."""
    client, provider = build(settings, tmp_path, enabled=True)
    plan = plan_for(stage=Stage.JOBSPEC)
    client.complete(plan, Answer)

    raised = client.settings.stages[Stage.JOBSPEC].model_copy(update={"effort": Effort.HIGH})
    client.settings = client.settings.model_copy(
        update={"stages": {**client.settings.stages, Stage.JOBSPEC: raised}}
    )
    client.complete(plan, Answer)

    assert provider.calls == 2
