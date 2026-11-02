"""The shipped model price table, overridable per model, refreshable on request.

An unknown model is reported in tokens with no dollar figure.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Literal, Protocol

import genai_prices
from genai_prices import Usage, calc_price
from genai_prices.data_snapshot import DataSnapshot, get_snapshot, set_custom_snapshot
from genai_prices.types import TieredPrices
from pydantic import BaseModel, ConfigDict, Field

from tailorcv.shared.costs import Ledger, ModelPrice
from tailorcv.shared.models import StrictModel


def _rate(value: Decimal | TieredPrices | None) -> tuple[float | None, int | None, float]:
    """Translate a scalar or one context tier without discarding pricing policy."""
    if not isinstance(value, TieredPrices):
        return (float(value) if value is not None else None), None, 1.0
    if len(value.tiers) != 1 or value.base <= 0:
        raise ValueError("The bundled price card needs an unsupported context policy")
    tier = value.tiers[0]
    return float(value.base), tier.start, float(tier.price / value.base)


def _bundled_price(model: str, provider: str) -> ModelPrice:
    """Read the pinned offline catalog into the application's durable price shape."""
    card = calc_price(Usage(), model, provider_id=provider).model_price
    input_rate, threshold, input_multiplier = _rate(card.input_mtok)
    output_rate, output_threshold, output_multiplier = _rate(card.output_mtok)
    read_rate, read_threshold, read_multiplier = _rate(card.cache_read_mtok)
    write_rate, write_threshold, write_multiplier = _rate(card.cache_write_mtok)
    search_rate, search_threshold, _ = _rate(card.web_searches_kcount)
    if input_rate is None or output_rate is None:
        raise ValueError(f"Missing token prices for {model}")
    if (
        any(value != threshold for value in (output_threshold, read_threshold, write_threshold))
        or read_multiplier != input_multiplier
        or write_multiplier != input_multiplier
        or search_threshold is not None
    ):
        raise ValueError(f"Incompatible bundled pricing policy for {model}")
    return ModelPrice(
        input=input_rate,
        output=output_rate,
        cache_read=read_rate,
        cache_write=write_rate,
        search_per_thousand_calls=search_rate,
        long_context_threshold=threshold,
        long_context_input_multiplier=input_multiplier,
        long_context_output_multiplier=output_multiplier,
    )


# This closed set decides what may be priced. The catalog is consulted only to
# resolve a dated served name onto a member of it, never for the rate itself.
SHIPPED_MODELS: dict[str, str] = {
    model: provider
    for provider, models in {
        "anthropic": (
            "claude-sonnet-5",
            "claude-opus-5",
            "claude-opus-5-5",
            "claude-haiku-4-5-20251001",
        ),
        "openai": (
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "gpt-6-sol",
            "gpt-6.1-sol",
            "gpt-6-luna",
        ),
    }.items()
    for model in models
}

PRICES: dict[str, ModelPrice] = {
    model: _bundled_price(model, provider) for model, provider in SHIPPED_MODELS.items()
}

_CATALOG = f"genai-prices {genai_prices.__version__}"
# The pinned release and its PyPI upload date; the bundled data carries no date of its own.
BUNDLED_TABLE_VERSION = "0.2.1"
BUNDLED_TABLE_DATE = date(2026, 10, 30)


@dataclass(frozen=True)
class PriceProvenance:
    """The origin of one model's current rates and the date of the table they came from."""

    source_kind: Literal["bundled_catalog", "refreshed_catalog", "configured_override"]
    source_urls: tuple[str, ...]
    snapshot_label: str
    currency: Literal["USD"] = "USD"
    fetched_at: datetime | None = None
    table_date: date | None = None
    table_date_unavailable_reason: str = "Hand-entered rates belong to no dated price table."
    overridden_fields: tuple[str, ...] = ()
    base_source: PriceProvenance | None = None


def _price_provenance(
    model: str,
    provider: str,
    label: str,
    source: str | None = None,
    fetched: DataSnapshot | None = None,
) -> PriceProvenance:
    """Describe the catalog that supplied a shipped rate."""
    card = calc_price(Usage(), model, provider_id=provider)
    return PriceProvenance(
        source_kind="refreshed_catalog" if fetched is not None else "bundled_catalog",
        source_urls=(source,) if source is not None else tuple(card.provider.pricing_urls or ()),
        snapshot_label=label,
        fetched_at=fetched.timestamp if fetched is not None else None,
        table_date=fetched.timestamp.date() if fetched is not None else BUNDLED_TABLE_DATE,
        table_date_unavailable_reason="",
    )


@dataclass
class _InForce:
    """The accepted price data and the origins of the rates still in force."""

    label: str = _CATALOG
    fetched: DataSnapshot | None = None
    source: str | None = None
    provenance: dict[str, PriceProvenance] = field(
        default_factory=lambda: {
            model: _price_provenance(model, provider, _CATALOG)
            for model, provider in SHIPPED_MODELS.items()
        }
    )


_in_force = _InForce()
_refresh_lock = threading.Lock()


def snapshot_label() -> str:
    """Return the price data the table's rates were derived from."""
    return _in_force.label


class PriceOverride(BaseModel):
    """A price a user supplies for a model the built-in table does not know."""

    model_config = ConfigDict(extra="forbid")

    input: float
    output: float
    cache_read: float | None = None
    cache_write: float | None = None
    search_per_thousand_calls: float | None = None

    def to_price(self) -> ModelPrice:
        """Return the cost model this override describes."""
        return ModelPrice(
            input=self.input,
            output=self.output,
            cache_read=self.cache_read,
            cache_write=self.cache_write,
            search_per_thousand_calls=self.search_per_thousand_calls,
        )


@dataclass(frozen=True)
class PriceSnapshot:
    """One offline read of prices, their origins, and the catalog used for aliases."""

    prices: Mapping[str, ModelPrice]
    provenance: Mapping[str, PriceProvenance]
    label: str
    _catalog: DataSnapshot = field(repr=False, compare=False)

    def priced_model(
        self,
        requested: str,
        served: str = "",
        provider: str = "",
    ) -> str | None:
        """Resolve a model against this snapshot and the provider that serves it."""
        key = _priced_model(self.prices, requested, served, provider, self._catalog)
        if key is not None and provider and SHIPPED_MODELS.get(key, provider) != provider:
            return None
        return key


def current_prices(overrides: dict[str, PriceOverride] | None = None) -> PriceSnapshot:
    """Capture current rates and provenance without fetching new prices."""
    with _refresh_lock:
        prices = dict(PRICES)
        provenance = dict(_in_force.provenance)
        for model, override in (overrides or {}).items():
            base = provenance.get(model)
            supplied = override.model_dump(exclude_unset=True)
            prices[model] = (
                prices[model].model_copy(update=supplied)
                if model in prices
                else override.to_price()
            )
            provenance[model] = PriceProvenance(
                source_kind="configured_override",
                source_urls=(),
                snapshot_label=_in_force.label,
                overridden_fields=tuple(sorted(supplied)),
                base_source=base,
                table_date_unavailable_reason="Configured overrides have no price table date.",
            )
        return PriceSnapshot(
            prices=MappingProxyType(prices),
            provenance=MappingProxyType(provenance),
            label=_in_force.label,
            _catalog=get_snapshot(),
        )


def ledger_for(overrides: dict[str, PriceOverride] | None = None) -> Ledger:
    """Return a ledger that prices from the table and records which rates a user set."""
    return Ledger(prices=table(overrides), overridden=frozenset(overrides or ()))


def table(overrides: dict[str, PriceOverride] | None = None) -> dict[str, ModelPrice]:
    """Return the current prices with any user overrides applied."""
    return dict(current_prices(overrides).prices)


# A snapshot name is the base model plus a trailing release date, compact, dashed,
# or after Google Cloud's "@". A four-digit date, a date with anything after it,
# and a "-v1" revision are refused: a sibling model is named that way too.
_SNAPSHOT_SUFFIX = re.compile(
    r"[-@]20\d{2}(?:(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])|-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01]))$"
)


def _snapshot_base(
    name: str,
    provider: str = "",
    catalog: DataSnapshot | None = None,
) -> str | None:
    """Return the base model a dated snapshot name names, or ``None``."""
    base = _SNAPSHOT_SUFFIX.sub("", name)
    if base == name:
        return None
    data = catalog or get_snapshot()
    try:
        calculation = data.calc(Usage(), name, provider or None, None, None)
    except LookupError:
        # The catalog lists some shipped models without their dated forms.
        shipped_by = SHIPPED_MODELS.get(base)
        if shipped_by is None or provider not in ("", shipped_by):
            return None
        try:
            calculation = data.calc(Usage(), base, shipped_by, None, None)
        except LookupError:
            return None
    if provider and calculation.provider.id != provider:
        return None
    return base if calculation.model.id == base else None


def priced_model(
    prices: Mapping[str, ModelPrice], requested: str, served: str = "", provider: str = ""
) -> str | None:
    """Return the exact or dated price-table key for a call, or ``None``."""
    return _priced_model(prices, requested, served, provider)


def _priced_model(
    prices: Mapping[str, ModelPrice],
    requested: str,
    served: str,
    provider: str,
    catalog: DataSnapshot | None = None,
) -> str | None:
    """Resolve an exact or dated model name using one catalog."""
    name = served or requested
    if name in prices:
        return name
    base = _snapshot_base(name, provider, catalog)
    if base is None or base not in prices:
        return None
    if served and requested not in (served, base):
        return None
    return base


class ModelPriceChange(StrictModel):
    """One model whose rates the refreshed price data states differently.

    Every rate the application bills from appears here, before and after, so no
    movement is hidden: tokens per million, hosted search per thousand calls, and
    the long-context policy that multiplies the token rates.
    """

    model: str
    provider: str
    previous_input_usd_per_million: float
    input_usd_per_million: float
    previous_output_usd_per_million: float
    output_usd_per_million: float
    previous_cache_read_usd_per_million: float | None
    cache_read_usd_per_million: float | None
    previous_cache_write_usd_per_million: float | None
    cache_write_usd_per_million: float | None
    previous_search_usd_per_thousand_calls: float | None
    search_usd_per_thousand_calls: float | None
    previous_long_context_threshold: int | None
    long_context_threshold: int | None
    previous_long_context_input_multiplier: float
    long_context_input_multiplier: float
    previous_long_context_output_multiplier: float
    long_context_output_multiplier: float


def _change(model: str, previous: ModelPrice, current: ModelPrice) -> ModelPriceChange:
    """Describe one model's movement between two published price cards."""
    return ModelPriceChange(
        model=model,
        provider=SHIPPED_MODELS[model],
        previous_input_usd_per_million=previous.input,
        input_usd_per_million=current.input,
        previous_output_usd_per_million=previous.output,
        output_usd_per_million=current.output,
        previous_cache_read_usd_per_million=previous.cache_read,
        cache_read_usd_per_million=current.cache_read,
        previous_cache_write_usd_per_million=previous.cache_write,
        cache_write_usd_per_million=current.cache_write,
        previous_search_usd_per_thousand_calls=previous.search_per_thousand_calls,
        search_usd_per_thousand_calls=current.search_per_thousand_calls,
        previous_long_context_threshold=previous.long_context_threshold,
        long_context_threshold=current.long_context_threshold,
        previous_long_context_input_multiplier=previous.long_context_input_multiplier,
        long_context_input_multiplier=current.long_context_input_multiplier,
        previous_long_context_output_multiplier=previous.long_context_output_multiplier,
        long_context_output_multiplier=current.long_context_output_multiplier,
    )


class PriceRefreshResponse(StrictModel):
    """What a price refresh changed, and which price data is in force after it.

    A refresh that could not read the published prices, or that read prices
    covering none of the billable models, leaves every previous rate in force
    and says which of the two happened.
    """

    available: bool
    unavailable_reason: str | None = None
    snapshot: str
    previous_snapshot: str
    source: str
    changed: list[ModelPriceChange] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)
    # A model the fetched data no longer prices keeps the rate it already had.
    dropped: list[str] = Field(default_factory=list)


class PriceCatalog(Protocol):
    """A source of published price data: the library's updater, or a test double."""

    url: str

    def fetch(self) -> DataSnapshot | None:
        """Return the published price data, or ``None`` when the source published none."""
        ...


def published_catalog() -> PriceCatalog:
    """Return the library's fetcher, which reads the published prices when called."""
    from genai_prices import UpdatePrices

    return UpdatePrices()


def refresh_prices(catalog: PriceCatalog) -> PriceRefreshResponse:
    """Fetch published price data and re-derive the shipped models' rates.

    Only a caller asking for this changes a rate, so a run prices the same way
    twice. The closed model set is unchanged: a model the fetched data no longer
    prices is reported and keeps its previous rate rather than going unpriced.
    After a refresh the fetched data also decides which dated served names map
    onto a shipped model, so that mapping moves with the publisher.
    """
    with _refresh_lock:
        previous = _in_force.label
        try:
            fetched = catalog.fetch()
        except Exception as error:  # noqa: BLE001 - any failure keeps the previous prices
            return _unrefreshed(
                previous, catalog.url, f"That price source could not be read: {error}"
            )
        if fetched is None:
            return _unrefreshed(previous, catalog.url, "That price source published no data.")
        set_custom_snapshot(fetched)
        try:
            rates, dropped = _catalog_rates()
        except Exception as error:  # noqa: BLE001 - installed data that breaks pricing is put back
            set_custom_snapshot(_in_force.fetched)
            return _unrefreshed(
                previous, catalog.url, f"That price source could not be read: {error}"
            )
        if not rates:
            set_custom_snapshot(_in_force.fetched)
            return _unrefreshed(
                previous,
                catalog.url,
                "That price source priced none of the models TailorCV can bill.",
            )
        _in_force.fetched = fetched
        _in_force.label = f"{_CATALOG} fetched {fetched.timestamp.isoformat(timespec='seconds')}"
        _in_force.source = catalog.url
        _in_force.provenance = {
            **_in_force.provenance,
            **{
                model: _price_provenance(
                    model, SHIPPED_MODELS[model], _in_force.label, catalog.url, fetched
                )
                for model in rates
            },
        }
        changed = [
            _change(model, PRICES[model], rate)
            for model, rate in sorted(rates.items())
            if PRICES[model] != rate
        ]
        PRICES.update(rates)
        return PriceRefreshResponse(
            available=True,
            snapshot=_in_force.label,
            previous_snapshot=previous,
            source=catalog.url,
            changed=changed,
            unchanged=sorted(set(rates) - {item.model for item in changed}),
            dropped=dropped,
        )


def _catalog_rates() -> tuple[dict[str, ModelPrice], list[str]]:
    """Re-derive each shipped model's rate, naming the ones the data no longer prices."""
    rates: dict[str, ModelPrice] = {}
    dropped: list[str] = []
    for model, provider in SHIPPED_MODELS.items():
        try:
            rates[model] = _bundled_price(model, provider)
        except (LookupError, ValueError):
            dropped.append(model)
    return rates, sorted(dropped)


def _unrefreshed(previous: str, source: str, reason: str) -> PriceRefreshResponse:
    """Report a refresh that changed nothing, with the previous rates still in force."""
    return PriceRefreshResponse(
        available=False,
        unavailable_reason=reason,
        snapshot=previous,
        previous_snapshot=previous,
        source=source,
        unchanged=sorted(SHIPPED_MODELS),
    )
