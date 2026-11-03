"""Caching, accounting, and validation wrapped once around any provider.

A request plan, the model it resolved to, and the provider's own account of the
call become the one manifest the ledger keeps.
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, TypeVar, cast

from pydantic import BaseModel, ValidationError

from clearscout.application.config import ModelChoice, Settings
from clearscout.infrastructure.build import BuildIdentity, current_build
from clearscout.infrastructure.providers.base import (
    BackgroundDispatch,
    BackgroundPoll,
    BackgroundWebSearchProvider,
    CallRecord,
    Provider,
    UnusableResponseError,
    WebSearchProvider,
    wire_schema,
)
from clearscout.infrastructure.providers.cache import CachedResponse, ResponseCache, cache_key
from clearscout.infrastructure.providers.images import most_image_tokens, refuse_unanswerable
from clearscout.infrastructure.providers.plan import RequestPlan, digest_of, schema_digest
from clearscout.shared.costs import CallManifest, Ledger, ServiceTier, Usage
from clearscout.shared.errors import CapacityError, ProviderError

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)
SentT = TypeVar("SentT")

# OpenAI bills nothing for a refused flex request and advises backing off before paying standard.
FLEX_RETRY_DELAYS: tuple[float, ...] = (5.0, 10.0, 20.0)
# Shortens each delay by up to this fraction, so workers refused together do not retry together.
FLEX_RETRY_JITTER = 0.25


def envelope_digest(envelope: dict[str, Any] | None) -> str:
    """Return the digest naming one provider envelope, empty when there is none."""
    if envelope is None:
        return ""
    return digest_of(json.dumps(envelope, sort_keys=True, separators=(",", ":"), default=str))


def _refusal_is_final(choice: ModelChoice, error: CapacityError, *, creating: bool) -> bool:
    """Return whether a capacity refusal ends the call rather than earning another attempt.

    Resending after an ambiguous failure could create a second billed response.
    """
    return choice.service_tier is ServiceTier.STANDARD or (
        creating and not error.certainly_not_created
    )


def _identity(choice: ModelChoice) -> str:
    """Return the part of a cache key naming how the model was asked.

    A control the run does not state is left out of the key.
    """
    parts = [choice.model, choice.effort.value if choice.effort else "default"]
    parts.extend(
        f"{name}={value.value}"
        for name, value in (
            ("verbosity", choice.verbosity),
            ("image_detail", choice.image_detail),
        )
        if value is not None
    )
    return ":".join(parts)


class Watcher(Protocol):
    """Told about every model call, before it is sent and after it returns.

    A protocol because the client must not know whether anyone is listening or
    what they do with it.
    """

    def sending(self, choice: ModelChoice, plan: RequestPlan) -> None:
        """Called immediately before a request goes out."""
        ...

    def returned(self, manifest: CallManifest, seconds: float) -> None:
        """Called once a request has answered, or been served from the cache."""
        ...


@dataclass
class Client:
    """A provider plus the run-scoped state every stage shares."""

    provider: Provider
    settings: Settings
    cache: ResponseCache
    ledger: Ledger
    watcher: Watcher | None = None
    # Stages that must ask the model again even when an answer is cached.
    redraw: frozenset[str] = frozenset()
    # Interactive calls stay standard; queue work passes `Settings.unattended_tier()`.
    service_tier: ServiceTier = ServiceTier.STANDARD
    build_identity: BuildIdentity = field(default_factory=current_build)
    raise_if_cancelled: Callable[[], None] | None = None
    sleeper: Callable[[float], None] = time.sleep
    # Returns a float in [0, 1) that scales the retry jitter.
    jitter_source: Callable[[], float] = random.random

    def complete(self, plan: RequestPlan, schema: type[ModelT], *, variant: str = "") -> ModelT:
        """Run one stage's model call, using an available server tool when asked.

        The cache stores the validated object's JSON rather than the raw
        response, so a hit cannot smuggle in an unvalidated shape. ``variant``
        names one draw of a request a stage makes more than once; it enters the
        cache key alone, so the draws neither overwrite one another nor change
        what is sent.
        """
        if self.raise_if_cancelled is not None:
            self.raise_if_cancelled()
        choice = self.settings.choice_for_stage(plan.stage, self.service_tier)
        sent_schema = wire_schema(self.provider.name, schema)
        web_search = plan.search is not None
        if web_search and not isinstance(self.provider, WebSearchProvider):
            raise ProviderError(
                f"{self.provider.name.value.title()} does not support provider-hosted web search."
            )
        refuse_unanswerable(self.provider.name, choice.model, choice.image_detail, plan.images)
        # Empty for an unnamed draw, so a stage that asks once keys on its request alone.
        drawn = f"\nvariant={variant}" if variant else ""
        key = cache_key(
            provider=self.provider.name.value,
            stage=plan.stage,
            max_tokens=plan.max_tokens,
            model=_identity(choice),
            # The kernel is model-facing text no prompt file holds, so keying
            # the rendered instructions is what puts it in the key at all.
            system=plan.instruction_text(),
            user=plan.task.text,
            # The schema actually sent, so editing how it is built moves the key.
            schema=sent_schema,
            extra=f"{'' if plan.evidence is None else plan.evidence.text}"
            f"\nserver_web_search={web_search}{drawn}",
            payload_digest=plan.payload_digest(),
        )
        started = time.monotonic()
        described = self._describe(plan, choice, sent_schema, cache_key=key)
        if plan.stage not in self.redraw and (cached := self._cached(key, schema)) is not None:
            value, origin = cached
            self._keep(
                replace(
                    described,
                    cached=True,
                    cached_at=origin.stored_at,
                    revision=origin.revision,
                    package_version=origin.package_version,
                ),
                started,
            )
            return value

        value = self._complete_paid(plan, schema, choice, started=started, cache_key=key)
        try:
            self.cache.set(
                key,
                value.model_dump_json(),
                revision=described.revision,
                package_version=described.package_version,
            )
        except (sqlite3.Error, OSError):
            logger.warning("Response cache write failed; billed response retained.", exc_info=True)
        return value

    def dispatch_background(
        self,
        plan: RequestPlan,
        schema: type[ModelT],
        choice: ModelChoice,
        *,
        metadata: dict[str, str],
    ) -> BackgroundDispatch:
        """Send one uncached request in the background and return its id and request."""
        provider = self.provider
        if not isinstance(provider, BackgroundWebSearchProvider):
            raise ProviderError(
                f"{provider.name.value.title()} does not support background web search."
            )
        return self._send_on_flex_first(
            plan,
            choice,
            lambda sent: provider.dispatch_background(plan, schema, sent, metadata=metadata),
            creating=True,
        )

    def collect_background(
        self,
        response_id: str,
        plan: RequestPlan,
        schema: type[ModelT],
        choice: ModelChoice,
        started: float,
        *,
        request_envelope: dict[str, Any] | None,
    ) -> BackgroundPoll:
        """Poll one dispatched response, recording a terminal result once per response id.

        ``request_envelope`` is the request the dispatch sent, which the poll cannot return.
        """
        if not isinstance(self.provider, BackgroundWebSearchProvider):
            raise ProviderError(
                f"{self.provider.name.value.title()} does not support background web search."
            )
        sent_schema = wire_schema(self.provider.name, schema)
        described = self._describe(plan, choice, sent_schema)
        poll = self.provider.poll_background(response_id, schema, plan.max_tokens)
        if poll.status == "pending":
            return poll
        record = poll.record
        if record is None:
            raise ProviderError(
                f"{self.provider.name.value.title()} reported a finished background "
                "response with no usage record."
            )
        if any(entry.response_id == response_id for entry in self.ledger.entries):
            return poll
        answered = _answered(
            described,
            replace(record, request_envelope=request_envelope),
            validated=poll.status == "complete",
        )
        # The dispatched request names the tier it went out on, and OpenAI calls Flex "flex".
        sent_tier = (request_envelope or {}).get("service_tier")
        resent = choice.service_tier is ServiceTier.FLEX and sent_tier not in (
            None,
            ServiceTier.FLEX.value,
        )
        self._keep(
            replace(answered, resent_on_standard=resent),
            started,
            price_known=record.usage_known,
        )
        return poll

    def _complete_paid(
        self,
        plan: RequestPlan,
        schema: type[ModelT],
        choice: ModelChoice,
        *,
        started: float,
        cache_key: str = "",
    ) -> ModelT:
        """Send an uncached request and retain every returned billing record."""
        sent_schema = wire_schema(self.provider.name, schema)
        described = self._describe(plan, choice, sent_schema, cache_key=cache_key)
        web_search = plan.search is not None
        resent: list[bool] = []
        try:
            value, record = self._send_on_flex_first(
                plan,
                choice,
                lambda sent: self._ask(plan, schema, sent, web_search),
                on_resend=lambda: resent.append(True),
            )
        except UnusableResponseError as error:
            answered = _answered(described, error.record, validated=False)
            self._keep(
                replace(answered, resent_on_standard=bool(resent)),
                started,
                price_known=error.record.usage_known,
            )
            raise
        answered = _answered(described, record, validated=True)
        self._keep(
            replace(answered, resent_on_standard=bool(resent)),
            started,
            price_known=record.usage_known,
        )
        return value

    def _send_on_flex_first(
        self,
        plan: RequestPlan,
        choice: ModelChoice,
        send: Callable[[ModelChoice], SentT],
        *,
        creating: bool = False,
        on_resend: Callable[[], None] | None = None,
    ) -> SentT:
        """Send one request, backing off on a flex capacity refusal before resending on standard.

        A stalled flex call resends on standard at once. ``creating`` resends only a
        refusal that certainly created no response. ``on_resend`` hears the move to standard.
        """
        delays = iter(FLEX_RETRY_DELAYS)
        while True:
            self._before_sending(plan, choice)
            try:
                return send(choice)
            except CapacityError as error:
                if _refusal_is_final(choice, error, creating=creating):
                    raise
                delay = None if error.stalled else next(delays, None)
                if delay is None:
                    choice = choice.model_copy(update={"service_tier": ServiceTier.STANDARD})
                    if on_resend is not None:
                        on_resend()
                else:
                    self.sleeper(delay * (1 - FLEX_RETRY_JITTER * self.jitter_source()))

    def _before_sending(self, plan: RequestPlan, choice: ModelChoice) -> None:
        """Stop a cancelled run, then tell the watcher one attempt is going out."""
        if self.raise_if_cancelled is not None:
            self.raise_if_cancelled()
        if self.watcher is not None:
            self.watcher.sending(choice, plan)

    def _ask(
        self,
        plan: RequestPlan,
        schema: type[ModelT],
        choice: ModelChoice,
        web_search: bool,
    ) -> tuple[ModelT, CallRecord]:
        """Send one request through the adapter that can answer it."""
        if web_search:
            search_provider = cast(WebSearchProvider, self.provider)
            return search_provider.complete_with_web_search(plan, schema, choice)
        return self.provider.complete(plan, schema, choice)

    def _describe(
        self,
        plan: RequestPlan,
        choice: ModelChoice,
        wire_schema: dict[str, Any],
        *,
        cache_key: str = "",
    ) -> CallManifest:
        """Return everything known about a call before the provider answers."""
        return CallManifest(
            stage=plan.stage,
            model=choice.model,
            usage=Usage(),
            cache_key=cache_key,
            provider=self.provider.name.value,
            effort=choice.effort.value if choice.effort else "",
            verbosity=choice.verbosity.value if choice.verbosity else "",
            image_detail=choice.image_detail.value if choice.image_detail else "",
            image_token_ceiling=most_image_tokens(
                self.provider.name, choice.model, choice.image_detail, plan.images
            ),
            revision=self.build_identity.revision,
            package_version=self.build_identity.package_version,
            prompts=plan.prompts,
            instructions_digest=plan.instructions.digest,
            evidence_digest="" if plan.evidence is None else plan.evidence.digest,
            task_digest=plan.task.digest,
            schema_digest=schema_digest(wire_schema),
            tools=",".join(plan.tool_names),
            search_policy=None if plan.search is None else plan.search.stated(),
        )

    def _keep(self, manifest: CallManifest, started: float, *, price_known: bool = True) -> None:
        """Price the call, put it in the ledger, and tell anyone watching."""
        seconds = time.monotonic() - started
        entry = self.ledger.record(
            replace(manifest, milliseconds=round(seconds * 1000)), price_known=price_known
        )
        if self.watcher is not None:
            self.watcher.returned(entry, seconds)

    def _cached(self, key: str, schema: type[ModelT]) -> tuple[ModelT, CachedResponse] | None:
        """Return a cached response, or ``None`` when there is none to trust.

        A payload that no longer validates means the schema changed, not that
        the cache is corrupt, and a fresh call is the right answer to both.
        """
        payload = self.cache.get_entry(key)
        if payload is None:
            return None
        try:
            return schema.model_validate_json(payload.value), payload
        except ValidationError:
            return None


def _answered(manifest: CallManifest, record: CallRecord, *, validated: bool) -> CallManifest:
    """Return the manifest with what the provider said about the call."""
    return replace(
        manifest,
        usage=record.usage,
        served=record.model,
        finish=record.finish.value,
        service_tier=record.service_tier,
        response_id=record.response_id,
        validated=validated,
        search_request_count=record.search_count,
        searches=tuple(search.stated() for search in record.searches),
        page_actions=tuple(action.stated() for action in record.page_actions),
        tool_version=record.tool_version,
        response_digest=envelope_digest(record.response_envelope),
        request_envelope=record.request_envelope,
        response_envelope=record.response_envelope,
    )
