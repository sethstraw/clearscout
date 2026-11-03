"""Narrate deterministic pipeline stages and paid calls."""

from __future__ import annotations

import time
from pathlib import Path

from clearscout.application.config import ModelChoice
from clearscout.domain.identity import url_host
from clearscout.infrastructure.providers.client import Watcher
from clearscout.infrastructure.providers.credentials import Prompter
from clearscout.infrastructure.providers.plan import RequestPlan
from clearscout.shared.costs import CallManifest


class _Commentary:
    """Print every model call as it happens for someone watching a long run."""

    def __init__(self, prompter: Prompter) -> None:
        self._prompter = prompter

    def sending(self, choice: ModelChoice, plan: RequestPlan) -> None:
        """Announce a request before it is sent."""
        effort = choice.effort.value if choice.effort else "default"
        cacheable = len(plan.cached_prefix)
        size = len(plan.instructions.text) + len(plan.task.text) + cacheable
        self._prompter.plain(
            f"      -> {plan.stage:<9} {choice.model} effort={effort} "
            f"sending {size:,} characters "
            f"({cacheable:,} cacheable), up to {plan.max_tokens:,} out"
        )
        if plan.images:
            self._prompter.plain(f"                   with {len(plan.images)} image(s)")

    def returned(self, manifest: CallManifest, seconds: float) -> None:
        """Report what came back, what it cost, and provider details."""
        stage = manifest.stage
        if manifest.cached:
            self._prompter.plain(f"      <- {stage:<9} served from cache, no charge")
            return
        usage = manifest.usage
        money = "price not configured" if manifest.dollars is None else f"${manifest.dollars:.4f}"
        self._prompter.plain(
            f"      <- {stage:<9} {seconds:6.1f}s  "
            f"in {usage.input_tokens:,} read {usage.cache_read_tokens:,} "
            f"wrote {usage.cache_write_tokens:,} out {usage.output_tokens:,}  {money}"
        )
        if usage.reasoning_tokens:
            share = usage.reasoning_tokens / usage.output_tokens if usage.output_tokens else 0
            self._prompter.plain(
                f"                   of which {usage.reasoning_tokens:,} reasoning "
                f"({share:.0%}), {usage.answer_tokens:,} answer"
            )
        details = [f"finish={manifest.finish}"]
        if manifest.response_id:
            details.append(manifest.response_id)
        if manifest.service_tier:
            details.append(f"tier={manifest.service_tier}")
        self._prompter.plain(f"                   {'  '.join(details)}")
        if manifest.substituted:
            self._prompter.plain(
                f"                   ! asked for {manifest.model}, served {manifest.served}"
            )


class _Progress:
    """Announce each stage as it starts and each paid call inside it."""

    def __init__(self, prompter: Prompter | None, total: int) -> None:
        self._prompter = prompter
        self._total = total
        self._index = 0
        self._started = time.monotonic()

    def begin(self, label: str) -> None:
        """Report that a stage has started."""
        self._index += 1
        self._started = time.monotonic()
        if self._prompter is not None:
            self._prompter.step(self._index, self._total, label)

    def within(self, label: str) -> None:
        """Report one paid call inside the current stage."""
        if self._prompter is not None:
            elapsed = time.monotonic() - self._started
            self._prompter.plain(f"      {label} ({elapsed:.0f}s in)")


def _commentary(prompter: Prompter | None, verbose: bool) -> Watcher | None:
    """Return a watcher only when someone asked to see calls."""
    if not verbose or prompter is None:
        return None
    return _Commentary(prompter)


def _origin(source: str) -> str:
    """Return a short description of where the posting is coming from."""
    return url_host(source) or Path(source).name
