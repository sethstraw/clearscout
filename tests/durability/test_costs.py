from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from clearscout.application.tasks import TaskQueueService
from clearscout.application.workspaces import WorkspaceService
from clearscout.application.workspaces.spend import record_calls
from clearscout.bootstrap.logging import StructuredLogFormatter
from clearscout.infrastructure.persistence.database import Database
from clearscout.shared.costs import (
    CallManifest,
    Ledger,
    ModelPrice,
    Usage,
)
from tests.support.artifacts import runtime_storage


@pytest.mark.parametrize("priced", [True, False])
def test_every_call_of_a_paid_run_is_filed_with_its_cost_record(
    durable_database, tmp_path, settings, priced
):
    """A call written without its cost record cannot be told apart from a free one."""
    from pydantic import BaseModel
    from sqlalchemy import select

    from clearscout.application.config import Stage
    from clearscout.application.discovery.paid_task import billed_run
    from clearscout.infrastructure.persistence.models import CostRecord, ModelCall
    from tests.support.doubles import ScriptedProvider
    from tests.support.plans import plan_for

    class Answer(BaseModel):
        value: str

    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="advisory", name="Advisory", root_path=str(tmp_path)
    )
    task_id = _paid_run(durable_database, workspace.id, "search.guided_campaign", 10, priced=False)
    provider = ScriptedProvider({"Answer": lambda _: {"value": "retained"}})
    with billed_run(
        durable_database,
        workspace_id=workspace.id,
        task_id=task_id,
        settings=settings,
        origin="synthetic",
        provider=provider,
    ) as paid:
        model = settings.choice_for_stage(Stage.DRAFT).model
        paid.ledger.prices = {model: ModelPrice(input=100, output=100)} if priced else {}
        client = paid.build_client()
        for index in range(3):
            assert (
                client.complete(plan_for(stage=Stage.DRAFT, user=str(index)), Answer).value
                == "retained"
            )
        assert len(paid.ledger.entries) == 3
        assert (paid.ledger.total_dollars is None) is (not priced)
    assert len(provider.calls) == 3
    with durable_database.session() as session:
        calls = session.scalars(select(ModelCall).where(ModelCall.task_id == task_id)).all()
        costs = session.scalars(
            select(CostRecord).where(CostRecord.model_call_id.in_([c.id for c in calls]))
        ).all()
        assert len(calls) == len(costs) == 4
        assert sum(cost.amount is None for cost in costs) == (1 if priced else 4)


@pytest.mark.parametrize("tier", ["standard", "flex"])
@pytest.mark.parametrize("input_tokens", [271_999, 272_000, 272_001])
def test_long_context_prices_all_input_classes_and_output_at_the_published_rates(
    tier: str, input_tokens: int
) -> None:
    from clearscout.shared.costs import ServiceTier
    from clearscout.shared.costs.prices import table

    ledger = Ledger(prices=table())
    usage = Usage(
        input_tokens=input_tokens - 150_000,
        cache_read_tokens=100_000,
        cache_write_tokens=50_000,
        output_tokens=1000,
    )
    entry = ledger.record(
        CallManifest("draft", "gpt-5.6-terra", usage, service_tier=ServiceTier(tier))
    )
    input_cost = (usage.input_tokens * 2 + 100_000 * 0.2 + 50_000 * 2.5) / 1_000_000
    expected = input_cost + 0.012
    if input_tokens > 272_000:
        expected = input_cost * 2 + 0.018
    if tier == "flex":
        expected /= 2
    assert entry.dollars == pytest.approx(expected)


def test_price_overrides_keep_the_models_long_context_policy() -> None:
    from clearscout.shared.costs.prices import PriceOverride, table

    prices = table({"gpt-5.6-terra": PriceOverride(input=4, output=24)})
    assert prices["gpt-5.6-terra"].cost(
        Usage(input_tokens=280_000, output_tokens=1000)
    ) == pytest.approx(2.276)


def test_a_partial_price_override_leaves_the_rates_it_does_not_mention() -> None:
    """A user who retypes the token rates has not said cached reads cost that much."""
    from clearscout.shared.costs.prices import PriceOverride, table

    shipped = table()["claude-opus-5"]
    retyped = table({"claude-opus-5": PriceOverride(input=shipped.input, output=shipped.output)})[
        "claude-opus-5"
    ]
    million_cached = Usage(cache_read_tokens=1_000_000)

    assert shipped.cost(million_cached) == pytest.approx(0.5)
    assert retyped.cost(million_cached) == pytest.approx(0.5)
    assert retyped.cache_write == shipped.cache_write
    assert retyped.search_per_thousand_calls == shipped.search_per_thousand_calls
    assert retyped.prices_searches(5)


def test_a_call_priced_from_a_users_own_rate_records_that_the_rate_was_an_override() -> None:
    """A record reading "configured" credits the catalog with a number it never set."""
    from clearscout.shared.costs.prices import PriceOverride, ledger_for, snapshot_label

    ledger = ledger_for({"claude-opus-5": PriceOverride(input=1.0, output=2.0)})
    million = Usage(input_tokens=1_000_000)

    overridden = ledger.record(CallManifest("draft", "claude-opus-5", million))
    shipped = ledger.record(CallManifest("draft", "gpt-5.6-terra", million))

    assert overridden.pricing_source == "configured:override"
    assert overridden.dollars == pytest.approx(1.0)
    # The catalog is still what the shipped rates came from, override or not.
    assert overridden.price_snapshot == snapshot_label()
    assert shipped.pricing_source == "configured"


def test_a_dated_snapshot_of_an_overridden_model_is_recorded_as_an_override_too() -> None:
    """The user set the base model's rate, and the dated name bills from it."""
    from clearscout.shared.costs.prices import PriceOverride, ledger_for

    ledger = ledger_for({"claude-opus-5": PriceOverride(input=1.0, output=2.0)})

    entry = ledger.record(
        CallManifest(
            "draft",
            "claude-opus-5",
            Usage(input_tokens=1_000_000),
            served="claude-opus-5-20260514",
        )
    )

    assert entry.pricing_source == "configured:override"


def test_a_dated_served_snapshot_prices_from_its_base_model() -> None:
    """The provider answers with a dated name for the model that was pinned."""
    from clearscout.shared.costs.prices import table

    prices = table()
    usage = Usage(input_tokens=1000, output_tokens=1000)

    entry = Ledger(prices=prices).record(
        CallManifest("draft", "claude-opus-5", usage, served="claude-opus-5-20260514")
    )

    assert entry.dollars == pytest.approx(prices["claude-opus-5"].cost(usage))
    assert entry.pricing_source == "configured"


def test_the_dated_snapshot_openai_answers_an_alias_with_prices_from_that_alias() -> None:
    """OpenAI answers a request for an alias with its dated snapshot's name.

    The models listing carries both names, so the snapshot prices from the alias.
    """
    from clearscout.shared.costs.prices import table

    prices = table()
    usage = Usage(input_tokens=1000, output_tokens=1000)

    entry = Ledger(prices=prices).record(
        CallManifest(
            "draft",
            "gpt-5.6-terra",
            usage,
            served="gpt-5.6-terra-2026-03-05",
            provider="openai",
        )
    )

    assert entry.dollars == pytest.approx(prices["gpt-5.6-terra"].cost(usage))
    assert entry.pricing_source == "configured"


def test_a_sibling_model_name_is_not_priced_at_the_base_models_rate() -> None:
    """The catalog resolves claude-opus-5-mini onto claude-opus-5, which never ran."""
    from clearscout.shared.costs.prices import table

    entry = Ledger(prices=table()).record(
        CallManifest("draft", "claude-opus-5-mini", Usage(input_tokens=1000, output_tokens=1000))
    )

    assert entry.dollars is None
    assert entry.pricing_source == "unpriced:model"


def test_a_substitution_to_another_model_is_left_unpriced() -> None:
    """A redirect to another tier must not be priced at the tier that answered."""
    from clearscout.shared.costs.prices import table

    entry = Ledger(prices=table()).record(
        CallManifest(
            "draft",
            "claude-opus-5",
            Usage(input_tokens=1000, output_tokens=1000),
            served="claude-sonnet-5-20260514",
        )
    )

    assert entry.dollars is None
    assert entry.pricing_source == "unpriced:model"


def test_a_replayed_call_costs_nothing_and_says_so_without_a_price_table() -> None:
    """A cache hit made no provider call, so nothing was billed and nothing is unknown."""
    ledger = Ledger()

    entry = ledger.record(CallManifest("draft", "m", Usage(input_tokens=10), cached=True))

    assert entry.dollars == 0.0
    assert entry.pricing_source == "cache_hit"
    assert ledger.total_dollars == 0.0


def test_the_recorded_reason_says_why_a_call_carries_no_price(
    durable_database: Database, tmp_path: Path
) -> None:
    """A page that prints "configured" beside a blank cost states something untrue."""
    from clearscout.application.workspaces.spend import SpendService

    workspace_id = str(
        WorkspaceService(durable_database, runtime_storage(durable_database))
        .create(slug="reasons", name="Reasons", root_path=str(tmp_path))
        .id
    )
    usage = Usage(input_tokens=10, output_tokens=10)
    ledger = Ledger(prices={"m": ModelPrice(input=1.0, output=1.0)})
    ledger.record(CallManifest("draft", "m", usage))
    ledger.record(CallManifest("draft", "m", usage, search_request_count=1))
    ledger.record(CallManifest("draft", "mystery", usage))
    ledger.record(CallManifest("draft", "m", usage), price_known=False)
    ledger.record(CallManifest("draft", "m", usage, cached=True))
    with durable_database.session() as session:
        record_calls(
            session,
            workspace_id=workspace_id,
            task_id=None,
            ledger=ledger,
            provider="openai",
            origin="workflow.tailor",
        )

    summary = SpendService(durable_database).spend_summary(workspace_id)

    assert sorted(row.pricing_source or "" for row in summary.recent) == [
        "cache_hit",
        "configured",
        "unpriced:model",
        "unpriced:search_rate",
        "unpriced:usage",
    ]
    assert summary.unpriced_calls == 3
    assert [row.amount_usd for row in summary.recent if row.pricing_source == "cache_hit"] == [0.0]


@pytest.mark.parametrize("input_tokens", [1000, 280_000])
@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("openai", "gpt-5.6-sol"),
        ("openai", "gpt-5.6-terra"),
        ("openai", "gpt-5.6-luna"),
        ("openai", "gpt-6-sol"),
        ("openai", "gpt-6-luna"),
        ("anthropic", "claude-sonnet-5"),
        ("anthropic", "claude-opus-5"),
        ("anthropic", "claude-opus-5-5"),
        ("anthropic", "claude-haiku-4-5-20251001"),
    ],
)
def test_bundled_prices_preserve_catalog_cache_and_search_costs(
    provider: str, model: str, input_tokens: int
) -> None:
    from genai_prices import Usage as CatalogUsage
    from genai_prices import calc_price

    from clearscout.shared.costs.prices import table

    usage = Usage(
        input_tokens=input_tokens - 300,
        cache_read_tokens=200,
        cache_write_tokens=100,
        output_tokens=10,
    )
    expected = calc_price(
        CatalogUsage(
            input_tokens=input_tokens,
            cache_read_tokens=200,
            cache_write_tokens=100,
            output_tokens=10,
            web_searches=2,
        ),
        model,
        provider_id=provider,
    ).total_price
    assert table()[model].cost(usage, search_calls=2) == pytest.approx(float(expected))


def test_sol_uses_current_rates_without_accepting_catalog_prefix_matches() -> None:
    from clearscout.shared.costs.prices import table

    prices = table()
    assert prices["gpt-5.6-sol"].cost(
        Usage(input_tokens=1000, output_tokens=1000)
    ) == pytest.approx(0.024)
    assert "claude-opus-5-madeup" not in prices


@pytest.mark.parametrize(
    ("model", "rates", "threshold"),
    [
        # https://developers.openai.com/api/docs/changelog and the two model pages
        ("gpt-6-sol", (2.0, 10.0, 0.2, 2.5), 272_000),
        ("gpt-6-luna", (0.1, 0.5, 0.01, 0.125), 272_000),
        # https://developers.openai.com/api/docs/pricing; the pinned catalog lacks it
        ("gpt-6.1-sol", (2.0, 10.0, 0.1, 2.5), 272_000),
        # https://platform.claude.com/docs/en/about-claude/pricing
        ("claude-opus-5-5", (4.0, 20.0, 0.2, 5.0), None),
    ],
)
def test_a_newly_shipped_model_bills_at_its_published_rates(
    model: str, rates: tuple[float, float, float, float], threshold: int | None
) -> None:
    """Rates per million tokens: input, output, cache read, five-minute cache write."""
    from clearscout.shared.costs.prices import table

    price = table()[model]

    assert (price.input, price.output, price.cache_read, price.cache_write) == pytest.approx(rates)
    assert price.long_context_threshold == threshold


@pytest.mark.parametrize(
    "served",
    [
        "claude-sonnet-5-20260514",
        "claude-sonnet-5-2026-05-14",
        "claude-sonnet-5@20260514",
    ],
)
def test_every_published_snapshot_form_prices_from_its_base_model(served: str) -> None:
    """Providers date a snapshot three ways, including Google Cloud's "@" form."""
    from clearscout.shared.costs.prices import table

    entry = Ledger(prices=table()).record(
        CallManifest(
            "draft",
            "claude-sonnet-5",
            Usage(input_tokens=1000, output_tokens=1000),
            served=served,
            provider="anthropic",
        )
    )

    assert entry.pricing_source == "configured"


@pytest.mark.parametrize(
    "served",
    [
        "claude-sonnet-5-0514",
        "claude-sonnet-5-1106-preview",
        "claude-sonnet-5-v1:0",
        "claude-sonnet-5-20261301",
    ],
)
def test_a_suffix_that_is_not_a_published_snapshot_date_is_left_unpriced(served: str) -> None:
    """A four-digit date, a leading date, a platform revision, and an impossible date.

    The catalog resolves all four onto claude-sonnet-5, and a sibling model is
    named the same ways, so none of them may be billed at that model's rate.
    """
    from clearscout.shared.costs.prices import table

    entry = Ledger(prices=table()).record(
        CallManifest(
            "draft",
            "claude-sonnet-5",
            Usage(input_tokens=1000, output_tokens=1000),
            served=served,
            provider="anthropic",
        )
    )

    assert entry.pricing_source == "unpriced:model"


@pytest.mark.parametrize(
    ("provider", "name", "resolves_to"),
    [
        ("anthropic", "claude-sonnet-5", "claude-sonnet-5"),
        ("anthropic", "claude-sonnet-5-20260514", "claude-sonnet-5"),
        ("anthropic", "claude-sonnet-5@20260514", "claude-sonnet-5"),
        ("anthropic", "claude-opus-5", "claude-opus-5"),
        ("anthropic", "claude-opus-5-5", "claude-opus-5-5"),
        ("anthropic", "claude-haiku-4-5-20251001", "claude-haiku-4-5"),
        ("openai", "gpt-5.6-sol", "gpt-5.6-sol"),
        ("openai", "gpt-5.6", "gpt-5.6-sol"),
        ("openai", "gpt-5.6-terra", "gpt-5.6-terra"),
        ("openai", "gpt-5-6-terra", "gpt-5.6-terra"),
        ("openai", "gpt-5.6-terra-2026-02-01", "gpt-5.6-terra"),
        ("openai", "gpt-5.6-terra-20260201", "gpt-5.6-terra"),
        ("openai", "gpt-5.6-luna", "gpt-5.6-luna"),
        ("openai", "gpt-6-sol", "gpt-6-sol"),
        ("openai", "gpt-6-luna", "gpt-6-luna"),
    ],
)
def test_the_pinned_catalog_resolves_these_names_onto_these_models(
    provider: str, name: str, resolves_to: str
) -> None:
    """Pins a third-party convention: genai-prices decides these, not ClearScout.

    A failure says the catalog changed, not that the application broke.
    """
    from genai_prices import Usage as CatalogUsage
    from genai_prices import calc_price

    assert calc_price(CatalogUsage(), name, provider_id=provider).model.id == resolves_to


@pytest.mark.parametrize(
    ("provider", "name"),
    [
        ("openai", "gpt-5.6-terra@20260201"),
        ("openai", "gpt-5.6-terra-v1:0"),
        ("openai", "gpt-5.6-luna-1106-preview"),
        ("anthropic", "claude-opus-5-mini"),
        ("anthropic", "claude-sonnet-5-2026-05-14"),
        ("anthropic", "claude-sonnet-5-1"),
    ],
)
def test_the_pinned_catalog_refuses_these_names_outright(provider: str, name: str) -> None:
    """Pins a third-party convention: these catalog entries match their names narrowly."""
    from genai_prices import Usage as CatalogUsage
    from genai_prices import calc_price

    with pytest.raises(LookupError):
        calc_price(CatalogUsage(), name, provider_id=provider)


@pytest.mark.parametrize(
    "name",
    [
        "claude-opus-5-mini",
        "claude-sonnet-5-1",
        "claude-haiku-4-5",
        "gpt-5.6",
        "claude-opus-5-v1:0",
    ],
)
def test_a_name_the_catalog_widens_onto_a_shipped_model_is_still_unpriced(name: str) -> None:
    """The catalog resolving a name is never on its own permission to bill it."""
    from clearscout.shared.costs.prices import priced_model, table

    assert priced_model(table(), name) is None


@pytest.mark.parametrize(
    ("model", "source_kind", "table_date"),
    [
        ("gpt-5.6-terra", "bundled_catalog", date(2026, 10, 30)),
        ("claude-opus-5", "bundled_catalog", date(2026, 10, 30)),
        ("gpt-6.1-sol", "bundled_catalog", date(2026, 10, 30)),
    ],
)
def test_current_prices_identifies_the_rate_source_and_the_bundled_table_date(
    model: str,
    source_kind: str,
    table_date: date | None,
) -> None:
    from clearscout.shared.costs.prices import current_prices, snapshot_label, table

    snapshot = current_prices()
    provenance = snapshot.provenance[model]

    assert snapshot.prices[model] == table()[model]
    assert snapshot.label == snapshot_label()
    assert provenance.source_kind == source_kind
    assert provenance.source_urls
    assert all(url.startswith("https://") for url in provenance.source_urls)
    assert provenance.currency == "USD"
    assert provenance.fetched_at is None
    assert provenance.table_date == table_date
    assert bool(provenance.table_date_unavailable_reason) == (table_date is None)


def test_the_bundled_table_date_is_the_pinned_genai_prices_release_date() -> None:
    import tomllib
    from pathlib import Path

    from clearscout.shared.costs.prices import BUNDLED_TABLE_DATE, BUNDLED_TABLE_VERSION

    lock = tomllib.loads((Path(__file__).parents[2] / "uv.lock").read_text())
    (pinned,) = [package for package in lock["package"] if package["name"] == "genai-prices"]

    assert pinned["version"] == BUNDLED_TABLE_VERSION
    assert pinned["sdist"]["upload-time"][:10] == BUNDLED_TABLE_DATE.isoformat()


def test_current_prices_keeps_partial_override_rates_and_their_base_source() -> None:
    from clearscout.shared.costs.prices import PriceOverride, current_prices

    original = current_prices()
    snapshot = current_prices({"gpt-5.6-terra": PriceOverride(input=4, output=24)})
    price = snapshot.prices["gpt-5.6-terra"]
    provenance = snapshot.provenance["gpt-5.6-terra"]

    assert price.input == 4
    assert price.output == 24
    assert price.cache_read == original.prices["gpt-5.6-terra"].cache_read
    assert price.long_context_threshold == original.prices["gpt-5.6-terra"].long_context_threshold
    assert provenance.source_kind == "configured_override"
    assert provenance.overridden_fields == ("input", "output")
    assert provenance.base_source == original.provenance["gpt-5.6-terra"]
    assert provenance.source_urls == ()
    assert provenance.fetched_at is None
    assert provenance.table_date is None
    assert provenance.table_date_unavailable_reason
    assert original.prices["gpt-5.6-terra"].input == 2
    with pytest.raises(TypeError):
        snapshot.prices["gpt-5.6-terra"] = ModelPrice(input=0, output=0)  # type: ignore[index]
    with pytest.raises(TypeError):
        snapshot.provenance["gpt-5.6-terra"] = provenance  # type: ignore[index]


@pytest.mark.parametrize(
    ("model", "provider", "expected"),
    [
        ("gpt-5.6-terra", "openai", "gpt-5.6-terra"),
        ("gpt-5.6-terra-2026-03-05", "openai", "gpt-5.6-terra"),
        ("gpt-5.6-terra", "anthropic", None),
        ("gpt-5.6-terra-2026-03-05", "anthropic", None),
        ("gpt-6.1-sol-2026-10-01", "openai", "gpt-6.1-sol"),
        ("gpt-6.1-sol-2026-10-01", "anthropic", None),
        ("gpt-5.6-terra-mini", "openai", None),
        ("unknown-model", "openai", None),
    ],
)
def test_current_prices_resolves_only_models_from_the_matching_provider(
    model: str,
    provider: str,
    expected: str | None,
) -> None:
    from clearscout.shared.costs.prices import current_prices

    assert current_prices().priced_model(model, provider=provider) == expected


def test_an_unknown_model_can_use_its_configured_price_without_claiming_a_catalog_source() -> None:
    from clearscout.shared.costs.prices import PriceOverride, current_prices

    snapshot = current_prices({"unknown-model": PriceOverride(input=7, output=9)})
    provenance = snapshot.provenance["unknown-model"]

    assert snapshot.priced_model("unknown-model", provider="openai") == "unknown-model"
    assert snapshot.prices["unknown-model"] == ModelPrice(input=7, output=9)
    assert provenance.source_kind == "configured_override"
    assert provenance.base_source is None
    assert provenance.source_urls == ()
    assert provenance.fetched_at is None


def test_a_refreshed_current_price_names_the_accepted_source_and_fetch_date(
    refreshable_prices: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from clearscout.shared.costs.prices import current_prices, refresh_prices
    from tests.support.doubles import ScriptedPriceCatalog

    catalog = ScriptedPriceCatalog(rates={"gpt-5.6-terra": 9.0})
    fetched = catalog.fetch()
    assert fetched is not None
    fetched.timestamp = datetime(2026, 10, 1, 12, 30, tzinfo=UTC)
    monkeypatch.setattr(catalog, "fetch", lambda: fetched)
    before = current_prices()

    report = refresh_prices(catalog)
    snapshot = current_prices()
    provenance = snapshot.provenance["gpt-5.6-terra"]

    assert report.available
    assert snapshot.prices["gpt-5.6-terra"].input == 9
    assert provenance.source_kind == "refreshed_catalog"
    assert provenance.source_urls == (catalog.url,)
    assert provenance.fetched_at == fetched.timestamp
    assert provenance.snapshot_label == snapshot.label == report.snapshot
    assert provenance.table_date == date(2026, 10, 1)
    assert provenance.table_date_unavailable_reason == ""
    assert snapshot.provenance["claude-opus-5"].table_date == date(2026, 10, 1)
    assert before.prices["gpt-5.6-terra"].input == 2
    assert before.provenance["gpt-5.6-terra"].fetched_at is None
    assert before.provenance["gpt-5.6-terra"].table_date == date(2026, 10, 30)


@pytest.mark.parametrize("failure", ["fetch", "empty", "unusable"])
def test_a_failed_refresh_keeps_current_prices_and_their_provenance(
    refreshable_prices: None,
    failure: str,
) -> None:
    from clearscout.shared.costs.prices import current_prices, refresh_prices
    from tests.support.doubles import ScriptedPriceCatalog

    assert refresh_prices(ScriptedPriceCatalog(rates={"gpt-5.6-terra": 9})).available
    before = current_prices()
    report = refresh_prices(
        ScriptedPriceCatalog(
            error=OSError("synthetic failure") if failure == "fetch" else None,
            empty=failure == "empty",
            unusable=failure == "unusable",
        )
    )

    assert not report.available
    after = current_prices()
    assert after.label == before.label
    assert after.prices == before.prices
    assert after.provenance == before.provenance


def test_a_dropped_model_keeps_the_source_of_its_retained_rate(
    refreshable_prices: None,
) -> None:
    from clearscout.shared.costs.prices import current_prices, refresh_prices
    from tests.support.doubles import ScriptedPriceCatalog

    before = current_prices()
    report = refresh_prices(ScriptedPriceCatalog(dropped=frozenset({"gpt-5.6-luna"})))
    after = current_prices()

    assert report.available
    assert report.dropped == ["gpt-5.6-luna"]
    assert after.prices["gpt-5.6-luna"] == before.prices["gpt-5.6-luna"]
    assert after.provenance["gpt-5.6-luna"] == before.provenance["gpt-5.6-luna"]
    assert after.provenance["gpt-5.6-terra"].source_kind == "refreshed_catalog"


def test_a_current_price_snapshot_keeps_alias_resolution_when_the_catalog_changes(
    refreshable_prices: None,
) -> None:
    from clearscout.shared.costs.prices import current_prices, refresh_prices
    from tests.support.doubles import ScriptedPriceCatalog

    before = current_prices()
    dated = "gpt-5.6-luna-2026-03-05"
    assert before.priced_model(dated, provider="openai") == "gpt-5.6-luna"

    assert refresh_prices(ScriptedPriceCatalog(dropped=frozenset({"gpt-5.6-luna"}))).available

    assert before.priced_model(dated, provider="openai") == "gpt-5.6-luna"
    assert current_prices().priced_model(dated, provider="openai") is None


def test_a_price_refresh_reports_the_rates_that_moved_and_the_snapshot_now_in_force(
    refreshable_prices: None,
) -> None:
    from clearscout.shared.costs.prices import refresh_prices, snapshot_label, table
    from tests.support.doubles import ScriptedPriceCatalog

    before = snapshot_label()
    catalog = ScriptedPriceCatalog(rates={"gpt-5.6-terra": 9.0})

    report = refresh_prices(catalog)

    assert report.available is True
    assert catalog.fetches == 1
    assert [change.model for change in report.changed] == ["gpt-5.6-terra"]
    moved = report.changed[0]
    assert moved.provider == "openai"
    assert moved.previous_input_usd_per_million == pytest.approx(2.0)
    assert moved.input_usd_per_million == pytest.approx(9.0)
    assert moved.output_usd_per_million == pytest.approx(36.0)
    assert "claude-opus-5" in report.unchanged
    assert report.previous_snapshot == before
    assert report.snapshot != before
    assert snapshot_label() == report.snapshot
    assert table()["gpt-5.6-terra"].input == pytest.approx(9.0)


def test_a_refresh_that_cannot_read_the_prices_leaves_the_previous_ones_in_force(
    refreshable_prices: None,
) -> None:
    """No network, a timeout, and bad data all arrive here as a failed fetch."""
    from clearscout.shared.costs.prices import refresh_prices, snapshot_label, table
    from tests.support.doubles import ScriptedPriceCatalog

    shipped = table()["gpt-5.6-terra"]
    before = snapshot_label()

    report = refresh_prices(ScriptedPriceCatalog(error=TimeoutError("connect timed out")))

    assert report.available is False
    assert "could not be read" in (report.unavailable_reason or "")
    assert report.changed == []
    assert report.snapshot == before
    assert snapshot_label() == before
    assert table()["gpt-5.6-terra"] == shipped


def test_prices_that_install_but_cannot_be_read_are_put_back(
    refreshable_prices: None,
) -> None:
    """A payload that validates and then breaks pricing must not stay installed."""
    from clearscout.shared.costs.prices import (
        PRICES,
        priced_model,
        refresh_prices,
        snapshot_label,
        table,
    )
    from tests.support.doubles import ScriptedPriceCatalog

    shipped = table()["gpt-5.6-terra"]
    before = snapshot_label()

    report = refresh_prices(ScriptedPriceCatalog(unusable=True))

    assert report.available is False
    assert report.snapshot == before
    assert snapshot_label() == before
    assert table()["gpt-5.6-terra"] == shipped
    assert priced_model(PRICES, "gpt-5.6-terra", "gpt-5.6-terra-2026-03-05") == "gpt-5.6-terra"


def test_a_ledger_prices_and_labels_from_the_same_data_across_a_refresh(
    refreshable_prices: None,
) -> None:
    """The snapshot on a record names the data that produced its amount."""
    from clearscout.shared.costs.prices import ledger_for, refresh_prices, snapshot_label
    from tests.support.doubles import ScriptedPriceCatalog

    ledger = ledger_for()
    before = snapshot_label()

    refresh_prices(ScriptedPriceCatalog(rates={"gpt-5.6-terra": 9.0}))
    entry = ledger.record(
        CallManifest("draft", "gpt-5.6-terra", Usage(input_tokens=1000, output_tokens=1000))
    )

    assert snapshot_label() != before
    assert entry.price_snapshot == before


def test_a_price_source_that_published_nothing_reads_differently_from_a_failure(
    refreshable_prices: None,
) -> None:
    from clearscout.shared.costs.prices import refresh_prices, table
    from tests.support.doubles import ScriptedPriceCatalog

    shipped = table()["gpt-5.6-terra"]

    report = refresh_prices(ScriptedPriceCatalog(empty=True))

    assert report.available is False
    assert "published no data" in (report.unavailable_reason or "")
    assert table()["gpt-5.6-terra"] == shipped


def test_a_refresh_names_a_model_the_prices_dropped_rather_than_unpricing_it(
    refreshable_prices: None,
) -> None:
    from clearscout.shared.costs.prices import refresh_prices, table
    from tests.support.doubles import ScriptedPriceCatalog

    shipped = table()["gpt-5.6-luna"]

    report = refresh_prices(ScriptedPriceCatalog(dropped=frozenset({"gpt-5.6-luna"})))

    assert report.available is True
    assert report.dropped == ["gpt-5.6-luna"]
    assert table()["gpt-5.6-luna"] == shipped


def test_a_refresh_prices_no_model_the_application_could_not_bill_before(
    refreshable_prices: None,
) -> None:
    """Refreshed rates for a closed set of models, not a wider one."""
    from clearscout.shared.costs.prices import SHIPPED_MODELS, refresh_prices, table
    from tests.support.doubles import ScriptedPriceCatalog

    refresh_prices(ScriptedPriceCatalog(rates={"gpt-5.6-terra": 9.0}))

    assert set(table()) == set(SHIPPED_MODELS)


def test_a_recorded_cost_names_the_price_data_that_produced_it(
    durable_database: Database, tmp_path: Path
) -> None:
    """A refresh moves the rates, so a past amount is only attributable with its source."""
    from clearscout.application.workspaces.spend import SpendService
    from clearscout.shared.costs.prices import snapshot_label, table

    workspace_id = str(
        WorkspaceService(durable_database, runtime_storage(durable_database))
        .create(slug="snapshots", name="Snapshots", root_path=str(tmp_path))
        .id
    )
    usage = Usage(input_tokens=10, output_tokens=10)
    ledger = Ledger(prices=table())
    ledger.record(CallManifest("draft", "gpt-5.6-terra", usage))
    ledger.record(CallManifest("draft", "mystery-model-1", usage))
    with durable_database.session() as session:
        record_calls(
            session,
            workspace_id=workspace_id,
            task_id=None,
            ledger=ledger,
            provider="openai",
            origin="workflow.tailor",
        )

    summary = SpendService(durable_database).spend_summary(workspace_id)

    priced = {row.model: row.price_snapshot for row in summary.recent}
    assert priced["gpt-5.6-terra"] == snapshot_label()
    assert priced["mystery-model-1"] is None


def test_every_billed_token_class_appears_in_the_summary() -> None:
    """A token class that is priced and not printed is a bill with a missing line.

    Cache writes are charged at the write rate, so a summary that omits them
    prints numbers that account for only part of the charge.
    """
    from clearscout.shared.costs import CallManifest, Ledger, ModelPrice, Usage

    ledger = Ledger(
        prices={"m": ModelPrice(input=2.0, output=12.0, cache_read=0.2, cache_write=2.5)}
    )
    usage = Usage(
        input_tokens=3,
        output_tokens=3862,
        cache_read_tokens=0,
        cache_write_tokens=22500,
    )
    entry = ledger.record(CallManifest("draft", "m", usage))
    assert entry.dollars is not None

    text = "\n".join(ledger.summary_lines(wide=True))
    # Every field the price function multiplies has to be legible in the output.
    for token_count in ("3", "3862", "22500"):
        assert token_count in text.replace(",", ""), f"{token_count} is billed and not shown"

    # The caller chooses the width and the terminal never does, so a written report is reproducible.
    assert ledger.summary_lines(wide=True) != ledger.summary_lines(wide=False)

    # And the printed numbers must account for the printed money.
    expected = (3 * 2.0 + 3862 * 12.0 + 0 * 0.2 + 22500 * 2.5) / 1_000_000
    assert abs(entry.dollars - expected) < 1e-9


def test_the_controls_a_call_ran_under_survive_into_the_workspace_record(
    durable_database: Database, tmp_path: Path
) -> None:
    """A control recorded in the ledger and dropped here is unmeasurable later."""
    from sqlalchemy import select

    from clearscout.infrastructure.persistence.models import ModelCall

    workspace_id = str(
        WorkspaceService(durable_database, runtime_storage(durable_database))
        .create(slug="controls", name="Controls", root_path=str(tmp_path))
        .id
    )
    ledger = Ledger()
    ledger.record(
        CallManifest(
            "ingest",
            "m",
            Usage(input_tokens=1),
            verbosity="low",
            image_detail="original",
            image_token_ceiling=1024,
        )
    )
    with durable_database.session() as session:
        record_calls(
            session,
            workspace_id=workspace_id,
            task_id=None,
            ledger=ledger,
            provider="openai",
            origin="cv.import",
        )
    with durable_database.session() as session:
        call = session.scalars(select(ModelCall)).one()

        assert call.verbosity == "low"
        assert call.image_detail == "original"
        assert call.image_token_ceiling == 1024


def test_recorded_model_calls_emit_privacy_safe_cost_telemetry(
    durable_database: Database, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    workspace_id = str(
        WorkspaceService(durable_database, runtime_storage(durable_database))
        .create(slug="telemetry", name="Telemetry", root_path=str(tmp_path))
        .id
    )
    ledger = Ledger(
        prices={
            "claude-sonnet-5": ModelPrice(input=1.0, output=2.0, search_per_thousand_calls=10.0),
        }
    )
    ledger.record(
        CallManifest(
            "guided_campaign",
            "claude-sonnet-5",
            Usage(input_tokens=100, output_tokens=20, reasoning_tokens=4),
            served="claude-sonnet-5-20260514",
            effort="medium",
            service_tier="priority",
            milliseconds=321,
            search_request_count=3,
            searches=({"query": "PRIVATE-QUERY-CANARY"},),
        )
    )
    task = TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="search.guided_campaign",
        payload={"campaign_manifest_sha256": "0" * 64, "campaign_cycle": 1},
        idempotency_key="cost-telemetry-task",
    )

    with (
        caplog.at_level(logging.INFO, logger="clearscout.application.workspaces.spend"),
        durable_database.session() as session,
    ):
        record_calls(
            session,
            workspace_id=workspace_id,
            task_id=task.id,
            ledger=ledger,
            provider="openai",
            origin="search.guided_campaign",
        )

    [record] = [
        item for item in caplog.records if item.getMessage() == "model_call_pricing_prepared"
    ]
    rendered = StructuredLogFormatter().format(record)
    assert record.__dict__["task_id"] == task.id
    assert record.__dict__["sequence"] == 1
    assert record.__dict__["search_request_count"] == 3
    assert record.__dict__["input_tokens"] == 100
    assert record.__dict__["output_tokens"] == 20
    assert record.__dict__["reasoning_tokens"] == 4
    assert record.__dict__["duration_ms"] == 321
    assert record.__dict__["amount_usd"] is not None
    assert record.__dict__["transaction_state"] == "prepared"
    assert "PRIVATE-QUERY-CANARY" not in rendered


def _paid_run(
    database: Database,
    workspace_id: str,
    task_type: str,
    tokens: int,
    *,
    priced: bool = True,
    payload: dict[str, Any] | None = None,
    calls: int = 1,
    status: str = "completed",
    provider: str = "openai",
    model: str = "m",
    cached: bool = False,
) -> str:
    """Leave the task row and cost records one finished paid run leaves behind."""
    from clearscout.infrastructure.persistence.models import BackgroundTask

    prices = {model: ModelPrice(input=1_000_000.0, output=0.0)} if priced else {}
    ledger = Ledger(prices=prices)
    for _ in range(calls):
        ledger.record(
            CallManifest("guided_campaign", model, Usage(input_tokens=tokens), cached=cached)
        )
    with database.session() as session:
        task = BackgroundTask(
            workspace_id=workspace_id,
            task_type=task_type,
            status=status,
            payload=dict(payload or {}),
            payload_sha256="0" * 64,
            idempotency_key=(
                f"{task_type}-{tokens}-{priced}-{calls}-{status}-{provider}-{model}-{cached}"
            ),
        )
        session.add(task)
        session.flush()
        task_id = str(task.id)
        record_calls(
            session,
            workspace_id=workspace_id,
            task_id=task_id,
            ledger=ledger,
            provider=provider,
            origin=task_type,
        )
    return task_id


def test_task_billing_distinguishes_no_calls_priced_and_unpriced_runs(
    durable_database: Database, tmp_path: Path
) -> None:
    from clearscout.application.failures import NotFoundError
    from clearscout.application.tasks import TaskQueueService
    from clearscout.application.workspaces.spend import SpendService

    workspace_id = str(
        WorkspaceService(durable_database, runtime_storage(durable_database))
        .create(slug="billing", name="Billing", root_path=str(tmp_path))
        .id
    )
    queue = TaskQueueService(durable_database)
    no_calls = queue.enqueue(
        workspace_id=workspace_id,
        task_type="discovery.evaluate",
        payload={},
        idempotency_key="no-calls",
    )
    priced_id = _paid_run(durable_database, workspace_id, "discovery.evaluate", 25)
    unpriced_id = _paid_run(
        durable_database,
        workspace_id,
        "discovery.evaluate",
        25,
        priced=False,
    )
    service = SpendService(durable_database)

    empty = service.task_billing(workspace_id, no_calls.id)
    priced = service.task_billing(workspace_id, priced_id)
    unpriced = service.task_billing(workspace_id, unpriced_id)

    assert (empty.status, empty.call_count, empty.actual_cost_usd) == (
        "no_model_calls",
        0,
        Decimal(0),
    )
    assert (priced.status, priced.call_count, priced.actual_cost_usd) == (
        "priced",
        1,
        Decimal(25),
    )
    assert (unpriced.status, unpriced.call_count, unpriced.actual_cost_usd) == (
        "unpriced",
        1,
        None,
    )
    assert unpriced.unpriced_call_count == 1

    other_workspace_id = str(
        WorkspaceService(durable_database, runtime_storage(durable_database))
        .create(slug="other-billing", name="Other billing", root_path=str(tmp_path / "other"))
        .id
    )
    with pytest.raises(NotFoundError, match="does not exist"):
        service.task_billing(other_workspace_id, priced_id)


@pytest.mark.parametrize(
    ("status", "call_count", "unpriced_call_count", "actual_cost_usd"),
    [
        ("no_model_calls", 1, 0, Decimal(0)),
        ("priced", 1, 1, Decimal("0.01")),
        ("unpriced", 1, 1, Decimal("0.01")),
    ],
)
def test_task_billing_contract_rejects_inconsistent_states(
    status: str,
    call_count: int,
    unpriced_call_count: int,
    actual_cost_usd: Decimal,
) -> None:
    from pydantic import ValidationError

    from clearscout.application.tasks import TaskBillingResponse

    with pytest.raises(ValidationError):
        TaskBillingResponse.model_validate(
            {
                "task_id": "task-1",
                "status": status,
                "call_count": call_count,
                "unpriced_call_count": unpriced_call_count,
                "actual_cost_usd": actual_cost_usd,
            }
        )


def test_a_kind_of_run_nobody_has_made_is_listed_with_nothing_measured(
    durable_database: Database, tmp_path: Path
) -> None:
    """Every paid kind is listed whether or not it has been run, so the blanks show."""
    from clearscout.application.workspaces.spend import SpendService

    workspace_id = str(
        WorkspaceService(durable_database, runtime_storage(durable_database))
        .create(slug="fresh", name="Fresh", root_path=str(tmp_path))
        .id
    )

    history = SpendService(durable_database).run_cost_history(workspace_id)

    assert [item.task_type for item in history] == [
        "posting.refresh_with_reextraction",
        "posting.refresh_with_rematch",
        "posting.recover_batch",
        "discovery.evaluate",
        "discovery.evaluate_batch",
        "discovery.relevance_review",
        "workflow.tailor",
        "workflow.approve",
        "workflow.batch",
        "search.guided_campaign",
        "search.vocabulary",
    ]
    assert all(item.measured_runs == 0 for item in history)
    assert all(
        item.per_unit_low is None and item.per_unit_median is None and item.per_unit_high is None
        for item in history
    )
    assert {item.task_type: item.unit for item in history} == {
        "posting.refresh_with_reextraction": "run",
        "posting.refresh_with_rematch": "run",
        "posting.recover_batch": "run",
        "discovery.evaluate": "run",
        "discovery.evaluate_batch": "posting",
        "discovery.relevance_review": "call",
        "workflow.tailor": "run",
        "workflow.approve": "run",
        "workflow.batch": "run",
        "search.guided_campaign": "attempt",
        "search.vocabulary": "call",
    }


def test_a_review_is_priced_per_model_call_and_an_unpriced_run_is_counted_not_averaged(
    durable_database: Database, tmp_path: Path
) -> None:
    """A review settles near the same figure per call whatever the batch carried.

    A task carrying a call nobody priced cannot be totalled, and averaging it in
    as zero would report the work as cheaper than it was.
    """
    from decimal import Decimal

    from clearscout.application.workspaces.spend import SpendService

    workspace_id = str(
        WorkspaceService(durable_database, runtime_storage(durable_database))
        .create(slug="measured", name="Measured", root_path=str(tmp_path))
        .id
    )
    # Per call these are 3, 1 and 8; per row they would be 0.6, 1 and 2.67.
    for rows, tokens, calls in ((10, 3, 2), (1, 1, 1), (3, 8, 1)):
        _paid_run(
            durable_database,
            workspace_id,
            "discovery.relevance_review",
            tokens,
            calls=calls,
            payload={"pending_discovery_ids": [f"pending-{index}" for index in range(rows)]},
        )
    _paid_run(
        durable_database,
        workspace_id,
        "discovery.relevance_review",
        9,
        priced=False,
        payload={"pending_discovery_ids": ["pending-unpriced"]},
    )

    review = next(
        item
        for item in SpendService(durable_database).run_cost_history(workspace_id)
        if item.task_type == "discovery.relevance_review"
    )

    assert review.measured_runs == 3
    assert review.unpriced_runs == 1
    assert review.unit == "call"
    assert review.per_unit_low == Decimal(1)
    assert review.per_unit_median == Decimal(3), "the median of 1, 3 and 8, not their mean of 4"
    assert review.per_unit_high == Decimal(8)


def test_a_wholly_replayed_run_measures_nothing_rather_than_zero(
    durable_database: Database, tmp_path: Path
) -> None:
    """A run served from the response cache did not do the work, so it costs nothing to report."""
    from clearscout.application.workspaces.spend import SpendService

    workspace_id = str(
        WorkspaceService(durable_database, runtime_storage(durable_database))
        .create(slug="replayed", name="Replayed", root_path=str(tmp_path))
        .id
    )
    _paid_run(durable_database, workspace_id, "search.vocabulary", 4, cached=True)

    vocabulary = next(
        item
        for item in SpendService(durable_database).run_cost_history(workspace_id)
        if item.task_type == "search.vocabulary"
    )

    assert vocabulary.measured_runs == 0
    assert vocabulary.per_unit_low is None
    assert vocabulary.per_unit_median is None
    assert vocabulary.per_unit_high is None


def test_a_median_is_drawn_from_the_runs_that_paid_a_provider(
    durable_database: Database, tmp_path: Path
) -> None:
    """A replayed zero beside real runs drags down the figure a paid run is approved against."""
    from clearscout.application.workspaces.spend import SpendService

    workspace_id = str(
        WorkspaceService(durable_database, runtime_storage(durable_database))
        .create(slug="mixed", name="Mixed", root_path=str(tmp_path))
        .id
    )
    _paid_run(durable_database, workspace_id, "search.vocabulary", 4)
    _paid_run(durable_database, workspace_id, "search.vocabulary", 8)
    _paid_run(durable_database, workspace_id, "search.vocabulary", 6, cached=True)

    vocabulary = next(
        item
        for item in SpendService(durable_database).run_cost_history(workspace_id)
        if item.task_type == "search.vocabulary"
    )

    assert vocabulary.measured_runs == 2
    assert vocabulary.per_unit_low == Decimal(4)
    assert vocabulary.per_unit_median == Decimal(6)
    assert vocabulary.per_unit_high == Decimal(8)


def test_a_wholly_replayed_run_is_counted_rather_than_dropped_without_a_reason(
    durable_database: Database, tmp_path: Path
) -> None:
    """A run left out of the sample with no count shows neither the run nor why."""
    from clearscout.application.workspaces.spend import SpendService

    workspace_id = str(
        WorkspaceService(durable_database, runtime_storage(durable_database))
        .create(slug="replayed", name="Replayed", root_path=str(tmp_path))
        .id
    )
    _paid_run(durable_database, workspace_id, "search.vocabulary", 4)
    _paid_run(durable_database, workspace_id, "search.vocabulary", 6, cached=True)
    _paid_run(durable_database, workspace_id, "search.vocabulary", 9, cached=True)

    vocabulary = next(
        item
        for item in SpendService(durable_database).run_cost_history(workspace_id)
        if item.task_type == "search.vocabulary"
    )

    assert vocabulary.measured_runs == 1
    assert vocabulary.replayed_runs == 2
    assert vocabulary.unpriced_runs == 0


def test_a_rate_can_be_read_for_one_provider_and_model_rather_than_the_whole_workspace(
    durable_database: Database, tmp_path: Path
) -> None:
    """A model priced differently would carry its rate into an estimate for another."""
    from decimal import Decimal

    from clearscout.application.workspaces.spend import per_unit_costs

    workspace_id = str(
        WorkspaceService(durable_database, runtime_storage(durable_database))
        .create(slug="rates", name="Rates", root_path=str(tmp_path))
        .id
    )
    _paid_run(durable_database, workspace_id, "search.guided_campaign", 2, model="cheap")
    _paid_run(durable_database, workspace_id, "search.guided_campaign", 40, model="dear")

    with durable_database.session() as session:
        cheap = per_unit_costs(session, workspace_id, provider="openai", model="cheap")
        unknown = per_unit_costs(session, workspace_id, provider="anthropic", model="cheap")
        everything = per_unit_costs(session, workspace_id)

    assert cheap["search.guided_campaign"].median == Decimal(2)
    assert unknown["search.guided_campaign"].median is None
    assert everything["search.guided_campaign"].median == Decimal(21)


def test_a_run_that_never_completed_is_left_out_of_the_per_unit_sample(
    durable_database: Database, tmp_path: Path
) -> None:
    """A canceled or failed run spent what it spent on work it did not finish."""
    from clearscout.application.workspaces.spend import SpendService

    workspace_id = str(
        WorkspaceService(durable_database, runtime_storage(durable_database))
        .create(slug="partial", name="Partial", root_path=str(tmp_path))
        .id
    )
    _paid_run(durable_database, workspace_id, "search.vocabulary", 2)
    _paid_run(durable_database, workspace_id, "search.vocabulary", 40, status="failed")
    _paid_run(durable_database, workspace_id, "search.vocabulary", 60, status="running")

    vocabulary = next(
        item
        for item in SpendService(durable_database).run_cost_history(workspace_id)
        if item.task_type == "search.vocabulary"
    )

    assert vocabulary.measured_runs == 1
    assert vocabulary.per_unit_median == Decimal(2)
    assert vocabulary.per_unit_high == Decimal(2)


def test_each_paid_kind_is_divided_by_the_work_its_own_run_recorded(
    durable_database: Database, tmp_path: Path
) -> None:
    """The unit of a run differs by kind, so each kind reads its own count."""
    from clearscout.application.workspaces.spend import SpendService

    workspace_id = str(
        WorkspaceService(durable_database, runtime_storage(durable_database))
        .create(slug="units", name="Units", root_path=str(tmp_path))
        .id
    )
    _paid_run(
        durable_database,
        workspace_id,
        "discovery.evaluate_batch",
        24,
        payload={"job_posting_ids": ["job-1", "job-2", "job-3"]},
    )
    _paid_run(durable_database, workspace_id, "search.guided_campaign", 5, calls=4)
    _paid_run(durable_database, workspace_id, "workflow.tailor", 7)

    history = {
        item.task_type: item
        for item in SpendService(durable_database).run_cost_history(workspace_id)
    }

    evaluate = history["discovery.evaluate_batch"]
    campaign = history["search.guided_campaign"]
    tailoring = history["workflow.tailor"]

    assert (evaluate.unit, evaluate.per_unit_median) == ("posting", Decimal(8))
    assert (campaign.unit, campaign.per_unit_median) == ("attempt", Decimal(5))
    assert (tailoring.unit, tailoring.per_unit_median) == ("run", Decimal(7))


def test_a_hosted_search_is_billed_at_its_own_rate_and_not_only_in_tokens() -> None:
    """A tool the vendor bills per call is money, and this module reports money."""
    ledger = Ledger(
        prices={
            "m": ModelPrice(
                input=2.0,
                output=12.0,
                cache_read=0.2,
                cache_write=2.5,
                search_per_thousand_calls=10.0,
            )
        }
    )

    entry = ledger.record(
        CallManifest("search", "m", Usage(input_tokens=1_000_000), search_request_count=23)
    )

    assert entry.dollars is not None
    assert abs(entry.dollars - (2.0 + 23 * 10.0 / 1_000)) < 1e-9


def test_a_search_nobody_priced_reports_no_number_rather_than_the_tokens_alone() -> None:
    """An unpriced tool makes the run total unknown, not smaller."""
    ledger = Ledger(prices={"m": ModelPrice(input=2.0, output=12.0)})

    entry = ledger.record(
        CallManifest("search", "m", Usage(input_tokens=1_000_000), search_request_count=1)
    )

    assert entry.dollars is None
    assert ledger.total_dollars is None


def test_the_discounted_tier_halves_tokens_and_leaves_the_search_fee_alone() -> None:
    """Flex discounts the model, not the hosted tool the vendor prices separately."""
    price = ModelPrice(input=2.0, output=12.0, search_per_thousand_calls=10.0)

    assert price.halved().search_per_thousand_calls == 10.0


def test_a_missed_call_and_its_replay_both_record_the_key_that_names_their_answer(
    durable_database: Database, tmp_path: Path, client_factory
) -> None:
    """Without the key, no stored call can be joined to the answer it produced."""
    from pydantic import BaseModel
    from sqlalchemy import select

    from clearscout.application.config import Stage
    from clearscout.infrastructure.persistence.models import ModelCall
    from clearscout.infrastructure.providers.cache import ResponseCache
    from tests.support.plans import plan_for

    class Answer(BaseModel):
        text: str

    workspace_id = str(
        WorkspaceService(durable_database, runtime_storage(durable_database))
        .create(slug="keys", name="Keys", root_path=str(tmp_path))
        .id
    )
    client = client_factory({"Answer": lambda schema: {"text": "hello"}})
    with ResponseCache(enabled=True, directory=tmp_path / "keyed") as cache:
        client.cache = cache
        client.complete(plan_for(stage=Stage.DRAFT), Answer)
        client.complete(plan_for(stage=Stage.DRAFT), Answer)
    with durable_database.session() as session:
        record_calls(
            session,
            workspace_id=workspace_id,
            task_id=None,
            ledger=client.ledger,
            provider="synthetic",
            origin="workflow.tailor",
        )

    with durable_database.session() as session:
        rows = session.scalars(select(ModelCall)).all()
        stored = sorted((row.cache_hit, row.cache_key or "", bool(row.cached_at)) for row in rows)

    assert [row[0] for row in stored] == [False, True]
    assert stored[0][1] and stored[0][1] == stored[1][1]
    assert [row[2] for row in stored] == [False, True]


def test_a_response_envelope_is_stored_beside_its_call_under_a_digest_that_names_it(
    durable_database: Database, tmp_path: Path
) -> None:
    """A run keeps the envelope itself and a digest that tells two answers apart."""
    from sqlalchemy import select

    from clearscout.infrastructure.persistence.models import ModelCall, ModelCallEnvelope
    from clearscout.infrastructure.providers.client import envelope_digest

    workspace_id = str(
        WorkspaceService(durable_database, runtime_storage(durable_database))
        .create(slug="envelopes", name="Envelopes", root_path=str(tmp_path))
        .id
    )
    answered = {"id": "resp_1", "output": [{"text": "a synthetic answer"}]}
    differing = {"id": "resp_2", "output": [{"text": "a different synthetic answer"}]}
    ledger = Ledger(prices={"m": ModelPrice(input=1.0, output=1.0)})
    for envelope in (answered, dict(answered), differing):
        ledger.record(
            CallManifest(
                "draft",
                "m",
                Usage(input_tokens=10, output_tokens=10),
                response_digest=envelope_digest(envelope),
                request_envelope={"model": "m", "input": "a synthetic prompt"},
                response_envelope=envelope,
            )
        )
    with durable_database.session() as session:
        record_calls(
            session,
            workspace_id=workspace_id,
            task_id=None,
            ledger=ledger,
            provider="synthetic",
            origin="workflow.tailor",
        )

    with durable_database.session() as session:
        stored = session.execute(
            select(ModelCall.response_digest, ModelCallEnvelope.response_envelope).join(
                ModelCallEnvelope, ModelCallEnvelope.model_call_id == ModelCall.id
            )
        ).all()

    envelopes = [envelope for _, envelope in stored]
    assert envelopes.count(answered) == 2
    assert differing in envelopes
    assert len({digest for digest, _ in stored}) == 2


def test_the_response_cache_archive_rescues_answers_it_cannot_attribute_to_a_call(
    durable_database: Database, tmp_path: Path
) -> None:
    """The cache overwrites itself on every rerun, so a copy is the only way to keep one."""
    from sqlalchemy import select

    from clearscout.application.workspaces.spend import archive_response_cache
    from clearscout.infrastructure.persistence.models import ResponseCacheArchive
    from clearscout.infrastructure.providers.cache import ResponseCache

    directory = tmp_path / "responses"
    with ResponseCache(enabled=True, directory=directory) as cache:
        cache.set("key-one", '{"text":"first"}', revision="abc", package_version="0.1.0")
        cache.set("key-two", '{"text":"second"}')

    with durable_database.session() as session:
        first = archive_response_cache(session, cache_directory=directory, origin="rescue")
    with durable_database.session() as session:
        second = archive_response_cache(session, cache_directory=directory, origin="rescue")
        rows = session.scalars(
            select(ResponseCacheArchive).order_by(ResponseCacheArchive.cache_key)
        ).all()

    assert (first, second) == (2, 0)
    assert [(row.cache_key, row.value) for row in rows] == [
        ("key-one", '{"text":"first"}'),
        ("key-two", '{"text":"second"}'),
    ]
    assert (rows[0].revision, rows[0].package_version) == ("abc", "0.1.0")
    assert not hasattr(ResponseCacheArchive, "model_call_id")
    with ResponseCache(enabled=True, directory=directory) as cache:
        assert cache.get("key-one") == '{"text":"first"}'
        assert cache.get("key-two") == '{"text":"second"}'


def test_the_response_cache_archive_reads_a_file_written_before_source_identity_existed(
    durable_database: Database, tmp_path: Path
) -> None:
    """A cache file from an older build has no identity columns to select."""
    import sqlite3

    from sqlalchemy import select

    from clearscout.application.workspaces.spend import archive_response_cache
    from clearscout.infrastructure.persistence.models import ResponseCacheArchive

    directory = tmp_path / "responses"
    directory.mkdir()
    with sqlite3.connect(directory / "responses.sqlite3") as connection:
        connection.execute(
            "CREATE TABLE responses (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
            "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP) STRICT"
        )
        connection.execute("INSERT INTO responses(key,value) VALUES ('old', '{\"text\":\"old\"}')")

    with durable_database.session() as session:
        archived = archive_response_cache(session, cache_directory=directory, origin="rescue")
    with durable_database.session() as session:
        rows = session.scalars(select(ResponseCacheArchive)).all()

    assert archived == 1
    assert [(row.cache_key, row.value) for row in rows] == [("old", '{"text":"old"}')]
    assert (rows[0].revision, rows[0].package_version) == (None, None)


def test_the_response_cache_archive_rescues_an_answer_still_only_in_the_write_ahead_log(
    durable_database: Database, tmp_path: Path
) -> None:
    """A rescue runs against a cache nobody has checkpointed, which is every live one."""
    from sqlalchemy import select

    from clearscout.application.workspaces.spend import archive_response_cache
    from clearscout.infrastructure.persistence.models import ResponseCacheArchive
    from clearscout.infrastructure.providers.cache import ResponseCache

    directory = tmp_path / "responses"
    with ResponseCache(enabled=True, directory=directory) as holder:
        holder.set("uncheckpointed", '{"text":"only in the log"}')
        assert (directory / "responses.sqlite3-wal").exists()
        with durable_database.session() as session:
            archived = archive_response_cache(session, cache_directory=directory, origin="rescue")

    with durable_database.session() as session:
        rows = session.scalars(select(ResponseCacheArchive)).all()

    assert archived == 1
    assert [(row.cache_key, row.value) for row in rows] == [
        ("uncheckpointed", '{"text":"only in the log"}')
    ]


def test_the_archive_cache_command_rescues_the_cached_answers_and_says_how_many(
    durable_database: Database, durable_database_url: str, tmp_path: Path
) -> None:
    from sqlalchemy import select
    from typer.testing import CliRunner

    from clearscout.bootstrap.cli import app
    from clearscout.infrastructure.persistence.models import ResponseCacheArchive
    from clearscout.infrastructure.providers.cache import ResponseCache

    directory = tmp_path / "responses"
    with ResponseCache(enabled=True, directory=directory) as cache:
        cache.set("key-one", '{"text":"first"}', revision="abc", package_version="0.1.0")
        cache.set("key-two", '{"text":"second"}')

    result = CliRunner().invoke(
        app,
        [
            "db",
            "archive-cache",
            "--cache-directory",
            str(directory),
            "--database-url",
            durable_database_url,
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Archived 2 cached answers" in result.output
    with durable_database.session() as session:
        rows = session.scalars(
            select(ResponseCacheArchive).order_by(ResponseCacheArchive.cache_key)
        ).all()
    assert [(row.cache_key, row.value, row.origin) for row in rows] == [
        ("key-one", '{"text":"first"}', "cli.db.archive-cache"),
        ("key-two", '{"text":"second"}', "cli.db.archive-cache"),
    ]


def test_running_the_archive_cache_command_again_adds_nothing(
    durable_database: Database, durable_database_url: str, tmp_path: Path
) -> None:
    """A rescue is run by hand, so running it twice must not double the archive."""
    from sqlalchemy import select
    from typer.testing import CliRunner

    from clearscout.bootstrap.cli import app
    from clearscout.infrastructure.persistence.models import ResponseCacheArchive
    from clearscout.infrastructure.providers.cache import ResponseCache

    directory = tmp_path / "responses"
    with ResponseCache(enabled=True, directory=directory) as cache:
        cache.set("key-one", '{"text":"first"}')
    arguments = [
        "db",
        "archive-cache",
        "--cache-directory",
        str(directory),
        "--database-url",
        durable_database_url,
    ]
    runner = CliRunner()
    assert runner.invoke(app, arguments).exit_code == 0

    repeated = runner.invoke(app, arguments)

    assert repeated.exit_code == 0, repeated.output
    assert "Archived 0 cached answers" in repeated.output
    with durable_database.session() as session:
        keys = session.scalars(select(ResponseCacheArchive.cache_key)).all()
    assert list(keys) == ["key-one"]


@pytest.mark.parametrize("present", [False, True])
def test_the_archive_cache_command_reports_zero_for_a_cache_that_holds_nothing(
    durable_database_url: str, tmp_path: Path, present: bool
) -> None:
    from typer.testing import CliRunner

    from clearscout.bootstrap.cli import app

    directory = tmp_path / "responses"
    if present:
        directory.mkdir()

    result = CliRunner().invoke(
        app,
        [
            "db",
            "archive-cache",
            "--cache-directory",
            str(directory),
            "--database-url",
            durable_database_url,
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Archived 0 cached answers" in result.output
