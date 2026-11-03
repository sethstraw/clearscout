"""Token and money accounting for a run.

Tokens are always reported; dollars only when a price is configured for the
model that was actually called.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field, replace
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict


class ServiceTier(StrEnum):
    """How a request is served and billed; OpenAI ``flex`` costs half, is slower, may be refused."""

    STANDARD = "standard"
    FLEX = "flex"


class Usage(BaseModel):
    """Token counts from one model call, normalized across providers.

    Cached input is tracked separately; the master CV is a large constant
    prefix shared by every stage.
    """

    model_config = ConfigDict(frozen=True)

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # Reasoning tokens are included in output_tokens and billed at the output rate.
    reasoning_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        """Sum two usage records field by field."""
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
        )

    @property
    def answer_tokens(self) -> int:
        """Return the output tokens that became the answer rather than thought."""
        return max(0, self.output_tokens - self.reasoning_tokens)

    @property
    def total_tokens(self) -> int:
        """Return every token the call touched, cached or not.

        Reasoning is deliberately not added: it is already inside
        ``output_tokens``.
        """
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


class ModelPrice(BaseModel):
    """Dollars per million tokens for one model, and per thousand hosted searches.

    A hosted search is billed per call; the content it retrieves is billed
    again at the token rates.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    input: float
    output: float
    cache_read: float | None = None
    cache_write: float | None = None
    search_per_thousand_calls: float | None = None
    long_context_threshold: int | None = None
    long_context_input_multiplier: float = 1.0
    long_context_output_multiplier: float = 1.0

    def halved(self) -> ModelPrice:
        """Return these prices at the discounted tier, which bills at half.

        The vendor publishes one hosted-search rate across every tier, so that
        one carries through unhalved.
        """
        return self.model_copy(
            update={
                "input": self.input / 2,
                "output": self.output / 2,
                "cache_read": None if self.cache_read is None else self.cache_read / 2,
                "cache_write": None if self.cache_write is None else self.cache_write / 2,
            }
        )

    def context_multipliers(self, input_tokens: int) -> tuple[float, float]:
        """Return input and output rate factors for the request's full input count."""
        if self.long_context_threshold is not None and input_tokens > self.long_context_threshold:
            return self.long_context_input_multiplier, self.long_context_output_multiplier
        return 1.0, 1.0

    def prices_searches(self, search_calls: int) -> bool:
        """Return whether this price can account for ``search_calls`` hosted searches."""
        return not search_calls or self.search_per_thousand_calls is not None

    def cost(self, usage: Usage, search_calls: int = 0) -> float:
        """Return the dollar cost of ``usage`` and ``search_calls`` at these prices."""
        cache_read = self.cache_read if self.cache_read is not None else self.input
        cache_write = self.cache_write if self.cache_write is not None else self.input
        input_multiplier, output_multiplier = self.context_multipliers(
            usage.total_tokens - usage.output_tokens
        )
        tokens = (
            input_multiplier
            * (
                usage.input_tokens * self.input
                + usage.cache_read_tokens * cache_read
                + usage.cache_write_tokens * cache_write
            )
            + output_multiplier * usage.output_tokens * self.output
        ) / 1_000_000
        searches = search_calls * (self.search_per_thousand_calls or 0.0) / 1_000
        return tokens + searches


@dataclass(frozen=True)
class CallManifest:
    """Everything one model call is worth keeping, except its answer.

    ``model`` is what was asked for and ``served`` is what the provider says it
    answered with. They differ when an alias routes or a deprecation redirects.

    The request digests identify what the call was made of: the same four
    values name the same request, and a changed one names which part moved.
    ``response_digest`` names the answer the same way.

    The envelopes are the provider's own request and response bodies, and the
    request one carries prompt text including CV evidence.
    """

    stage: str
    model: str
    usage: Usage
    # ClearScout's own response cache, not the provider prompt cache counted in
    # ``usage``. True means no provider call was made and nothing was billed.
    cached: bool = False
    # The response cache key, derived for every call whether it hit or missed.
    cache_key: str = ""
    # Set only on a hit: the replayed row's stored time, as the cache wrote it.
    cached_at: str = ""
    dollars: float | None = None
    # Why ``dollars`` reads as it does, set by ``Ledger.record``: ``configured``,
    # ``configured:override``, ``cache_hit``, or ``unpriced:`` and what was missing.
    pricing_source: str = ""
    # Which price data produced ``dollars``, empty when no amount was written.
    price_snapshot: str = ""
    served: str = ""
    # A replayed answer has no provider finish reason.
    finish: str = ""
    provider: str = ""
    # Empty effort means the request did not specify one.
    effort: str = ""
    # Empty means the request left the vendor default in force.
    verbosity: str = ""
    image_detail: str = ""
    # None means unmeasured: an unreadable format, or a detail level the vendor
    # publishes no budget for.
    image_token_ceiling: int | None = None
    service_tier: str = ""
    # True when the discounted tier refused this call and it was answered on standard.
    resent_on_standard: bool = False
    response_id: str = ""
    revision: str = ""
    package_version: str = ""
    prompts: tuple[str, ...] = ()
    instructions_digest: str = ""
    evidence_digest: str = ""
    task_digest: str = ""
    schema_digest: str = ""
    # Empty when no provider answered, which is a cache hit or a failed send.
    response_digest: str = ""
    # The provider's own bodies. Both None on a replayed answer, which reached
    # no adapter.
    request_envelope: dict[str, Any] | None = None
    response_envelope: dict[str, Any] | None = None
    tools: str = ""
    # False marks a billed response that failed validation.
    validated: bool = True
    milliseconds: int = 0
    # None means the call offered no tool policy.
    search_policy: dict[str, Any] | None = None
    searches: tuple[dict[str, Any], ...] = ()
    search_request_count: int = 0
    # Pages the hosted tool opened or searched within, which ``search_count`` leaves out.
    page_actions: tuple[dict[str, Any], ...] = ()
    tool_version: str = ""

    @property
    def search_count(self) -> int:
        """Return how many hosted searches this call made."""
        if self.search_request_count:
            return self.search_request_count
        return len(self.searches)

    @property
    def substituted(self) -> bool:
        """Return whether the provider served a different model than was asked for."""
        return bool(self.served) and self.served != self.model


def _snapshot_label() -> str:
    """Return the price data in force now, imported late because prices read this module."""
    from clearscout.shared.costs.prices import snapshot_label

    return snapshot_label()


@dataclass
class Ledger:
    """Running total for a whole run, in call order.

    Mutable and kept to one side because accounting is a side channel: no
    stage's output depends on it, and no stage should have to carry it.
    """

    prices: dict[str, ModelPrice] = field(default_factory=dict)
    # The models in ``prices`` whose rate a user typed rather than the catalog.
    overridden: frozenset[str] = frozenset()
    # Captured with the rates: a refresh mid-run must not label an amount with
    # price data that did not produce it.
    snapshot: str = field(default_factory=_snapshot_label)
    entries: list[CallManifest] = field(default_factory=list)

    def record(self, manifest: CallManifest, *, price_known: bool = True) -> CallManifest:
        """Price one call and keep it, returning the entry the ledger holds.

        Pricing is the ledger's job rather than the caller's: it owns the price
        table, and a manifest that arrived already priced could disagree with it.
        """
        # Deferred because the price table reads ``ModelPrice`` from this module.
        from clearscout.shared.costs.prices import priced_model

        key = (
            priced_model(self.prices, manifest.model, manifest.served, manifest.provider)
            if price_known
            else None
        )
        price = self.prices.get(key) if key is not None else None
        # Priced from the tier that served the call rather than the one asked
        # for, because a refused discounted request is answered at full rate.
        if price is not None and manifest.service_tier == ServiceTier.FLEX:
            price = price.halved()
        searches = manifest.search_count
        dollars: float | None
        if manifest.cached:
            dollars, source = 0.0, "cache_hit"
        elif not price_known:
            dollars, source = None, "unpriced:usage"
        elif price is None:
            dollars, source = None, "unpriced:model"
        elif not price.prices_searches(searches):
            dollars, source = None, "unpriced:search_rate"
        else:
            dollars = price.cost(manifest.usage, searches)
            source = "configured:override" if key in self.overridden else "configured"
        entry = replace(
            manifest,
            dollars=dollars,
            pricing_source=source,
            price_snapshot=self.snapshot if dollars is not None else "",
        )
        self.entries.append(entry)
        return entry

    def refuse(self, position: int) -> None:
        """Mark one recorded call as a billed answer the caller's own checks refused."""
        self.entries[position] = replace(self.entries[position], validated=False)

    def served_models(self) -> dict[str, str]:
        """Return the model the provider actually answered with, per billed stage."""
        return {entry.stage: entry.served for entry in self.entries if entry.served}

    def resolved_efforts(self) -> dict[str, str]:
        """Return the effort level each billed stage was called at."""
        return {entry.stage: entry.effort for entry in self.entries if entry.effort}

    @property
    def total_usage(self) -> Usage:
        """Return the summed token usage of every recorded call."""
        total = Usage()
        for entry in self.entries:
            total = total + entry.usage
        return total

    @property
    def total_dollars(self) -> float | None:
        """Return the run total, or ``None`` when any call had no known price."""
        if any(entry.dollars is None for entry in self.entries):
            return None
        return sum(entry.dollars or 0.0 for entry in self.entries)

    def total_spent(self) -> str:
        """Return the run total as one short phrase for the console."""
        calls = sum(1 for entry in self.entries if not entry.cached)
        return f"{_money(self.total_dollars)} ({calls} model call{'s' if calls != 1 else ''})"

    def summary_lines(self, wide: bool | None = None) -> list[str]:
        """Render one line per stage plus a total, for the console and the report.

        ``wide`` is forced to ``True`` by the report, because a file's contents
        must not depend on how wide the terminal was when it was written.
        """
        wide = shutil.get_terminal_size((80, 24)).columns >= 100 if wide is None else wide
        lines = []
        for entry in self.entries:
            money = "cached" if entry.cached else _money(entry.dollars)
            if wide:
                lines.append(
                    f"{entry.stage:<10} {entry.model:<24} "
                    f"in {entry.usage.input_tokens:>7} "
                    f"read {entry.usage.cache_read_tokens:>7} "
                    f"wrote {entry.usage.cache_write_tokens:>7} "
                    f"out {entry.usage.output_tokens:>6}  {money}"
                )
            else:
                lines.append(f"{entry.stage:<10} out {entry.usage.output_tokens:>6}  {money}")
        total = self.total_usage
        if wide:
            lines.append(
                f"{'total':<10} {'':<24} "
                f"in {total.input_tokens:>7} "
                f"read {total.cache_read_tokens:>7} "
                f"wrote {total.cache_write_tokens:>7} "
                f"out {total.output_tokens:>6}  {_money(self.total_dollars)}"
            )
        else:
            lines.append(
                f"{'total':<10} out {total.output_tokens:>6}  {_money(self.total_dollars)}"
            )
        return lines


def cents(dollars: Decimal | float) -> int:
    """Round a dollar amount up to whole cents, so no displayed cost understates the charge."""
    return int((Decimal(str(dollars)) * 100).to_integral_value(rounding=ROUND_CEILING))


def _money(dollars: float | None) -> str:
    """Format a dollar amount, or say plainly that no price was configured."""
    return "price not configured" if dollars is None else f"${dollars:.4f}"
