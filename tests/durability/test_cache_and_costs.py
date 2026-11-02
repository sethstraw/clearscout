"""Cache keying and cost accounting, which both fail quietly when wrong."""

from __future__ import annotations

import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from tailorcv.application.master_cv.cv import parse_cv
from tailorcv.application.tailoring.stages import jobspec, match
from tailorcv.infrastructure.providers.cache import ResponseCache, cache_key
from tailorcv.infrastructure.web_fetch.ingest import JDText
from tailorcv.shared.costs import CallManifest, Ledger, ModelPrice, Usage
from tests.support.payloads import script
from tests.support.plans import plan_for

BASE: dict[str, Any] = {
    "provider": "anthropic",
    "stage": "draft",
    "max_tokens": 8192,
    "model": "claude-sonnet-5",
    "system": "s",
    "user": "u",
    "schema": {"type": "object"},
}


def _cv_variant(tmp_path: Path, master_cv: Path, name: str, old: str, new: str) -> Path:
    text = master_cv.read_text(encoding="utf-8")
    assert old in text
    variant = tmp_path / f"{name}.md"
    variant.write_text(text.replace(old, new, 1), encoding="utf-8")
    return variant


def _match_keys(client_factory: Any, guardrails: Any, *masters: Path) -> list[str]:
    keys = []
    for master in masters:
        client = client_factory(script({}))
        spec = jobspec.extract(client, JDText(text="A posting", origin="test", method="test"))
        match.match(client, parse_cv(master), spec, guardrails)
        keys.append(client.ledger.entries[-1].cache_key)
    return keys


def test_a_cv_difference_that_normalization_folds_away_keeps_the_match_key(
    client_factory, guardrails, master_cv: Path, tmp_path: Path
) -> None:
    padded = _cv_variant(tmp_path, master_cv, "padded", "source feeds\n", "source feeds  \n")
    assert parse_cv(padded).content_hash != parse_cv(master_cv).content_hash
    first, second = _match_keys(client_factory, guardrails, master_cv, padded)
    assert first == second


def test_editing_a_cited_bullet_moves_the_match_key(
    client_factory, guardrails, master_cv: Path, tmp_path: Path
) -> None:
    edited = _cv_variant(tmp_path, master_cv, "edited", "212 source feeds", "300 source feeds")
    first, second = _match_keys(client_factory, guardrails, master_cv, edited)
    assert first != second


def test_the_key_changes_when_the_response_schema_changes() -> None:
    assert cache_key(**BASE) != cache_key(**{**BASE, "schema": {"type": "string"}})


def test_the_key_changes_when_the_model_changes() -> None:
    assert cache_key(**BASE) != cache_key(**{**BASE, "model": "gpt-5"})


def test_the_key_is_stable_for_identical_inputs() -> None:
    assert cache_key(**BASE) == cache_key(**BASE)


def test_a_disabled_cache_stores_nothing(tmp_path: Path) -> None:
    with ResponseCache(enabled=False, directory=tmp_path) as cache:
        cache.set("k", "v")
        assert cache.get("k") is None


def test_cache_persists_text_without_object_deserialization(tmp_path: Path) -> None:
    with ResponseCache(enabled=True, directory=tmp_path) as cache:
        cache.set("k", '{"answer":"safe"}')

    database = tmp_path / "responses.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        declared_type = connection.execute(
            "SELECT type FROM pragma_table_info('responses') WHERE name = 'value'"
        ).fetchone()
        stored = connection.execute("SELECT value, typeof(value) FROM responses").fetchone()

    assert declared_type == ("TEXT",)
    assert stored == ('{"answer":"safe"}', "text")
    assert stat.S_IMODE(database.stat().st_mode) == 0o600

    with ResponseCache(enabled=True, directory=tmp_path) as reopened:
        assert reopened.get("k") == '{"answer":"safe"}'


def test_cache_updates_an_existing_key(tmp_path: Path) -> None:
    with ResponseCache(enabled=True, directory=tmp_path) as cache:
        cache.set("k", "old")
        cache.set("k", "new")
        assert cache.get("k") == "new"


def test_usage_adds_field_by_field() -> None:
    total = Usage(input_tokens=1, output_tokens=2) + Usage(input_tokens=3, cache_read_tokens=4)
    assert (total.input_tokens, total.output_tokens, total.cache_read_tokens) == (4, 2, 4)


def test_cached_calls_cost_nothing() -> None:
    ledger = Ledger(prices={"m": ModelPrice(input=3.0, output=15.0)})
    ledger.record(CallManifest("draft", "m", Usage(input_tokens=1000), cached=True))
    assert ledger.total_dollars == 0.0


def test_an_unpriced_model_reports_no_total_rather_than_a_wrong_one() -> None:
    ledger = Ledger()
    ledger.record(CallManifest("draft", "mystery", Usage(input_tokens=1000, output_tokens=100)))
    assert ledger.total_dollars is None
    assert "price not configured" in "\n".join(ledger.summary_lines())


def test_the_discounted_tier_is_priced_from_what_served_the_call() -> None:
    """A request refused the discounted tier is answered, and billed, at full rate."""
    prices = {"m": ModelPrice(input=3.0, output=15.0, cache_read=0.3)}
    usage = Usage(input_tokens=1_000_000, cache_read_tokens=1_000_000)

    flex = Ledger(prices=prices)
    flex.record(CallManifest("draft", "m", usage, service_tier="flex"))

    standard = Ledger(prices=prices)
    standard.record(CallManifest("draft", "m", usage, service_tier="default"))

    assert flex.total_dollars == 1.65
    assert standard.total_dollars == 3.3


def test_a_run_asks_every_stage_for_the_tier_the_client_was_given(client_factory) -> None:
    from tailorcv.application.config import Stage
    from tailorcv.shared.costs import ServiceTier

    class Answer(BaseModel):
        text: str

    client = client_factory({"Answer": lambda schema: {"text": "hello"}})
    assert client.service_tier is ServiceTier.STANDARD
    client.complete(plan_for(stage=Stage.DRAFT), Answer)

    client.service_tier = ServiceTier.FLEX
    client.complete(plan_for(stage=Stage.MATCH), Answer)

    assert [choice.service_tier for choice in client.provider.choices] == [
        ServiceTier.STANDARD,
        ServiceTier.FLEX,
    ]


def _refusing_the_discount(client: Any, refusals: int) -> list[Any]:
    """Refuse the first ``refusals`` flex requests, answer the rest, and return every tier asked."""
    from tailorcv.infrastructure.providers.base import CallRecord
    from tailorcv.shared.costs import ServiceTier
    from tailorcv.shared.errors import CapacityError

    scripted = client.provider.complete
    attempted: list[ServiceTier] = []

    def refuse_the_discount(plan: Any, schema: Any, choice: Any) -> Any:
        attempted.append(choice.service_tier)
        if choice.service_tier is ServiceTier.FLEX and len(attempted) <= refusals:
            raise CapacityError("no spare capacity")
        value, record = scripted(plan, schema, choice)
        served = "flex" if choice.service_tier is ServiceTier.FLEX else "default"
        return value, CallRecord(usage=record.usage, model=record.model, service_tier=served)

    client.provider.complete = refuse_the_discount
    return attempted


def test_a_refused_discount_waits_and_is_answered_at_the_discounted_tier(client_factory) -> None:
    """A refused flex request bills nothing, so waiting for capacity costs nothing extra."""
    from tailorcv.application.config import Stage
    from tailorcv.shared.costs import ServiceTier

    class Answer(BaseModel):
        text: str

    client = client_factory({"Answer": lambda schema: {"text": "hello"}})
    client.service_tier = ServiceTier.FLEX
    model = client.settings.choice_for_stage(Stage.DRAFT, ServiceTier.FLEX).model
    client.ledger.prices = {model: ModelPrice(input=3.0, output=15.0)}
    slept: list[float] = []
    client.sleeper = slept.append
    attempted = _refusing_the_discount(client, refusals=1)

    client.complete(plan_for(stage=Stage.DRAFT), Answer)

    assert attempted == [ServiceTier.FLEX, ServiceTier.FLEX]
    assert len(slept) == 1
    [entry] = client.ledger.entries
    assert entry.service_tier == "flex"
    flex_only = Ledger(prices=client.ledger.prices)
    flex_only.record(CallManifest("draft", model, entry.usage, service_tier="flex"))
    assert flex_only.total_dollars
    assert client.ledger.total_dollars == flex_only.total_dollars


def test_a_discount_refused_on_every_retry_is_answered_at_the_standard_tier_and_billed_there(
    client_factory,
) -> None:
    """Waits that grow give capacity a chance to return before the run pays full rate."""
    from tailorcv.application.config import Stage
    from tailorcv.shared.costs import ServiceTier

    class Answer(BaseModel):
        text: str

    client = client_factory({"Answer": lambda schema: {"text": "hello"}})
    client.service_tier = ServiceTier.FLEX
    slept: list[float] = []
    client.sleeper = slept.append
    attempted = _refusing_the_discount(client, refusals=4)

    client.complete(plan_for(stage=Stage.DRAFT), Answer)

    assert attempted == [ServiceTier.FLEX] * 4 + [ServiceTier.STANDARD]
    assert [entry.service_tier for entry in client.ledger.entries] == ["default"]
    assert len(slept) == 3
    assert slept == sorted(set(slept))


def test_clients_refused_together_wait_different_times(client_factory) -> None:
    """Jitter keeps workers refused at the same moment from retrying at the same moment."""
    from tailorcv.application.config import Stage
    from tailorcv.shared.costs import ServiceTier

    class Answer(BaseModel):
        text: str

    waits: list[list[float]] = []
    for draw in (0.0, 0.9):
        client = client_factory({"Answer": lambda schema: {"text": "hello"}})
        client.service_tier = ServiceTier.FLEX
        client.jitter_source = lambda draw=draw: draw
        slept: list[float] = []
        client.sleeper = slept.append
        _refusing_the_discount(client, refusals=4)
        client.complete(plan_for(stage=Stage.DRAFT), Answer)
        waits.append(slept)

    unjittered, jittered = waits
    assert unjittered != jittered
    assert all(0 < short < long for short, long in zip(jittered, unjittered, strict=True))
    assert jittered == sorted(set(jittered))


def test_a_refusal_at_the_standard_tier_ends_the_run(client_factory) -> None:
    """The standard resend is one attempt, so a provider with no capacity at all stops the run."""
    from tailorcv.application.config import Stage
    from tailorcv.shared.costs import ServiceTier
    from tailorcv.shared.errors import CapacityError

    class Answer(BaseModel):
        text: str

    client = client_factory({"Answer": lambda schema: {"text": "hello"}})
    client.service_tier = ServiceTier.FLEX
    client.sleeper = [].append

    attempted: list[ServiceTier] = []

    def refuse_everything(plan: Any, schema: Any, choice: Any) -> Any:
        attempted.append(choice.service_tier)
        raise CapacityError("no spare capacity")

    client.provider.complete = refuse_everything

    with pytest.raises(CapacityError):
        client.complete(plan_for(stage=Stage.DRAFT), Answer)

    assert attempted == [ServiceTier.FLEX] * 4 + [ServiceTier.STANDARD]


def test_a_standard_tier_refusal_is_never_retried(client_factory) -> None:
    from tailorcv.application.config import Stage
    from tailorcv.shared.costs import ServiceTier
    from tailorcv.shared.errors import CapacityError

    class Answer(BaseModel):
        text: str

    client = client_factory({"Answer": lambda schema: {"text": "hello"}})
    slept: list[float] = []
    client.sleeper = slept.append
    attempted: list[ServiceTier] = []

    def refuse_everything(plan: Any, schema: Any, choice: Any) -> Any:
        attempted.append(choice.service_tier)
        raise CapacityError("no spare capacity")

    client.provider.complete = refuse_everything

    with pytest.raises(CapacityError):
        client.complete(plan_for(stage=Stage.DRAFT), Answer)

    assert attempted == [ServiceTier.STANDARD]
    assert slept == []


def test_the_ledger_carries_why_a_billed_call_stopped(client_factory) -> None:
    """A recorded call that dropped its finish reason cannot explain its own failure."""
    from tailorcv.application.config import Stage
    from tailorcv.infrastructure.providers.base import CallRecord, Finish
    from tailorcv.infrastructure.providers.plan import RequestPlan

    class Answer(BaseModel):
        text: str

    client = client_factory({"Answer": lambda schema: {"text": "hello"}})
    scripted = client.provider.complete

    def truncated(plan: RequestPlan, schema: Any, choice: Any) -> Any:
        value, record = scripted(plan, schema, choice)
        return value, CallRecord(usage=record.usage, model=record.model, finish=Finish.TRUNCATED)

    client.provider.complete = truncated
    client.complete(plan_for(stage=Stage.DRAFT), Answer)

    assert [entry.finish for entry in client.ledger.entries] == ["truncated"]


def test_a_cache_hit_records_no_finish_reason(client_factory, tmp_path: Path) -> None:
    """No provider spoke, so there is no reason generation stopped to record."""
    from tailorcv.application.config import Stage
    from tailorcv.infrastructure.providers.cache import ResponseCache

    class Answer(BaseModel):
        text: str

    client = client_factory({"Answer": lambda schema: {"text": "hello"}})
    with ResponseCache(enabled=True, directory=tmp_path / "hit") as cache:
        client.cache = cache
        client.complete(plan_for(stage=Stage.DRAFT), Answer)
        client.complete(plan_for(stage=Stage.DRAFT), Answer)

    assert [(entry.cached, entry.finish) for entry in client.ledger.entries] == [
        (False, "complete"),
        (True, ""),
    ]


def test_a_call_variant_stays_out_of_the_request(client_factory, tmp_path: Path) -> None:
    """Two draws of one request must send identical bytes.

    A discriminator that reached the request would change what the provider is
    sent and invalidate every recorded cassette.
    """
    from tailorcv.application.config import Stage

    class Answer(BaseModel):
        text: str

    client = client_factory({"Answer": lambda schema: {"text": "hello"}})
    with ResponseCache(enabled=True, directory=tmp_path / "variant") as cache:
        client.cache = cache
        client.complete(plan_for(stage=Stage.CRITIC), Answer, variant="trial-0")
        client.complete(plan_for(stage=Stage.CRITIC), Answer, variant="trial-1")

    sent = [plan for _, plan in client.provider.calls]
    assert len(sent) == 2
    assert sent[0] == sent[1]
    assert "trial-" not in f"{sent[1].cached_prefix}\n{sent[1].task.text}"


def test_each_draw_of_one_request_replays_its_own_cached_answer(
    client_factory, tmp_path: Path
) -> None:
    """Draws that shared one entry would overwrite each other and re-bill on every run."""
    from tailorcv.application.config import Stage

    class Answer(BaseModel):
        text: str

    answers = iter(["first", "second"])
    client = client_factory({"Answer": lambda schema: {"text": next(answers)}})
    with ResponseCache(enabled=True, directory=tmp_path / "draws") as cache:
        client.cache = cache
        drawn = [
            client.complete(plan_for(stage=Stage.CRITIC), Answer, variant=f"trial-{index}").text
            for index in range(2)
        ]
        replayed = [
            client.complete(plan_for(stage=Stage.CRITIC), Answer, variant=f"trial-{index}").text
            for index in range(2)
        ]

    assert drawn == ["first", "second"]
    assert replayed == drawn
    assert len(client.provider.calls) == 2


def test_the_key_separates_two_providers_configured_with_the_same_model() -> None:
    """A model name is not unique across vendors, and the answers differ."""
    assert cache_key(**BASE) != cache_key(**{**BASE, "provider": "openai"})


def test_the_key_changes_when_the_output_ceiling_changes() -> None:
    """Raising max_tokens after a truncated response must not replay the truncation."""
    assert cache_key(**BASE) != cache_key(**{**BASE, "max_tokens": 16000})


def test_the_key_separates_two_stages_that_agree_on_everything_else() -> None:
    """Nothing but the stage name distinguishes two stages with one prompt shape."""
    assert cache_key(**BASE) != cache_key(**{**BASE, "stage": "critic"})


def test_the_client_keys_on_the_schema_it_actually_sends(client_factory, monkeypatch) -> None:
    """Editing how the wire schema is built has to move the key with it.

    ``model_json_schema()`` is not what the provider is sent. It is passed
    through ``json_schema``, which withdraws defaults and shares repeated enums,
    so a key on the Pydantic model's own schema would miss a contract change.
    """
    from tailorcv.application.config import Stage

    class Answer(BaseModel):
        text: str

    client = client_factory({"Answer": lambda schema: {"text": "hello"}})
    keys: list[str] = []

    def spy(**kwargs: Any) -> str:
        keys.append(str(kwargs["schema"]))
        return cache_key(**kwargs)

    monkeypatch.setattr("tailorcv.infrastructure.providers.client.cache_key", spy)
    client.complete(plan_for(stage=Stage.DRAFT), Answer)

    assert keys, "the client did not derive a cache key"
    assert "additionalProperties" in keys[0], (
        "the key used the Pydantic schema rather than the one sent on the wire"
    )


def test_cache_write_failure_preserves_billing_and_the_answer(client_factory, monkeypatch):
    from unittest.mock import Mock

    from tailorcv.application.config import Stage

    class Answer(BaseModel):
        text: str

    client = client_factory({"Answer": lambda schema: {"text": "hello"}})
    monkeypatch.setattr(
        client.cache, "set", Mock(side_effect=sqlite3.OperationalError("disk full"))
    )
    answer = client.complete(plan_for(stage=Stage.DRAFT), Answer)
    assert answer.text == "hello"
    assert len(client.ledger.entries) == 1
    assert client.ledger.entries[0].validated is True


def test_cancellation_after_a_response_blocks_the_next_paid_stage(client_factory):
    from tailorcv.application.config import Stage

    class Answer(BaseModel):
        text: str

    cancelled = False

    def answer(schema):
        nonlocal cancelled
        cancelled = True
        return {"text": "hello"}

    def check():
        if cancelled:
            raise RuntimeError("cancelled")

    client = client_factory({"Answer": answer})
    client.raise_if_cancelled = check
    assert client.complete(plan_for(stage=Stage.DRAFT), Answer).text == "hello"
    with pytest.raises(RuntimeError, match="cancelled"):
        client.complete(plan_for(stage=Stage.MATCH), Answer)
    assert len(client.ledger.entries) == 1


def test_call_history_separates_source_identity_from_package_version(client_factory):
    from tailorcv.infrastructure.build import BuildIdentity

    class Answer(BaseModel):
        text: str

    client = client_factory({"Answer": lambda _: {"text": "Answer"}})
    client.build_identity = BuildIdentity("sha256:source-one", "0.1.0")
    client.complete(plan_for(), Answer)
    assert client.ledger.entries[-1].revision == "sha256:source-one"
    assert client.ledger.entries[-1].package_version == "0.1.0"


def test_cache_hits_preserve_the_original_build_identity(client_factory, tmp_path):
    from tailorcv.infrastructure.build import BuildIdentity

    class Answer(BaseModel):
        text: str

    client = client_factory({"Answer": lambda _: {"text": "Answer"}})
    with ResponseCache(directory=tmp_path / "source-cache") as cache:
        client.cache = cache
        client.build_identity = BuildIdentity("sha256:source-one", "0.1.0")
        first = client.complete(plan_for(), Answer)
        client.build_identity = BuildIdentity("sha256:source-two", "0.2.0")
        second = client.complete(plan_for(), Answer)
    assert first == second
    cached = client.ledger.entries[-1]
    assert cached.cached
    assert cached.revision == "sha256:source-one"
    assert cached.package_version == "0.1.0"


def test_a_paid_call_carries_the_provider_envelopes_and_a_digest_of_the_answer(client_factory):
    """The envelopes are the only record of what was sent and what came back."""
    from dataclasses import replace

    from tailorcv.infrastructure.providers.client import envelope_digest

    class Answer(BaseModel):
        text: str

    client = client_factory({"Answer": lambda _: {"text": "Answer"}})
    answered = {"id": "resp_scripted", "output": [{"text": "a synthetic answer"}]}
    scripted = client.provider.complete

    def with_envelopes(plan, schema, choice):
        value, record = scripted(plan, schema, choice)
        return value, replace(
            record,
            request_envelope={"model": choice.model, "input": "a synthetic prompt"},
            response_envelope=answered,
        )

    client.provider.complete = with_envelopes
    client.complete(plan_for(), Answer)

    entry = client.ledger.entries[-1]
    assert entry.request_envelope == {"model": entry.model, "input": "a synthetic prompt"}
    assert entry.response_envelope == answered
    assert entry.response_digest == envelope_digest(answered)
    assert entry.response_digest != envelope_digest({"id": "resp_other", "output": []})


def test_a_replayed_call_carries_no_envelope_and_no_response_digest(client_factory, tmp_path):
    """No adapter answered, so there is no request or response body to keep."""

    class Answer(BaseModel):
        text: str

    client = client_factory({"Answer": lambda _: {"text": "Answer"}})
    with ResponseCache(directory=tmp_path / "replayed") as cache:
        client.cache = cache
        client.complete(plan_for(), Answer)
        client.complete(plan_for(), Answer)

    replayed = client.ledger.entries[-1]
    assert replayed.cached
    assert replayed.cache_key
    assert replayed.cache_key == client.ledger.entries[0].cache_key
    assert (replayed.request_envelope, replayed.response_envelope) == (None, None)
    assert replayed.response_digest == ""


def test_old_cache_payloads_keep_unknown_source_identity(tmp_path):
    database = tmp_path / "responses.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE responses (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
            "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP) STRICT"
        )
        connection.execute("INSERT INTO responses(key,value) VALUES ('old', '{}')")
    for _ in range(2):
        with ResponseCache(directory=tmp_path) as cache:
            old = cache.get_entry("old")
            assert old is not None
            assert old.value == "{}"
            assert old.revision == old.package_version == ""
