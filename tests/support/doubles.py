"""Stand-ins for the effects a test must never reach.

A provider, a keyring, and a terminal, each answering from a script the test
wrote, so a failure names the behavior rather than a network or a machine.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, TypeVar

from genai_prices.data_snapshot import DataSnapshot
from pydantic import BaseModel

from clearscout.application.config import ModelChoice, ProviderName
from clearscout.application.discovery.content_resolution import ResolvedPostingContent
from clearscout.application.discovery.posting_fetch import FetchedPosting
from clearscout.application.discovery.source_reading import SourceListing, SourceRead, SourceReader
from clearscout.application.tailoring.batch.analysis import Recommendation
from clearscout.application.tailoring.pipeline.models import SerializedRunArtifacts
from clearscout.application.tailoring.stages.verify import Report
from clearscout.domain import SourceReadOutcome
from clearscout.domain.records import PostingReadIntent
from clearscout.infrastructure.providers.base import (
    BackgroundDispatch,
    BackgroundPoll,
    CallRecord,
    Search,
    SearchSource,
)
from clearscout.infrastructure.providers.plan import RequestPlan
from clearscout.infrastructure.web_fetch.board_adapters import DETECT_BYTES, BoardApiFetcher
from clearscout.shared.costs import Ledger, Usage
from tests.support.payloads import reads_request
from tests.support.suite import SUITE_PROVIDER

ModelT = TypeVar("ModelT", bound=BaseModel)


class UnexpectedPostingFetcher:
    """Fail if a test unexpectedly crosses the posting-fetch effect boundary."""

    def fetch(
        self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL, board_url: str = ""
    ) -> FetchedPosting:
        raise AssertionError(f"Unexpected posting fetch for {url}")


class UnexpectedContentResolver:
    """Fail if a test unexpectedly reads posting content through the resolver."""

    def resolve(self, url: str, **_kwargs: object) -> ResolvedPostingContent:
        raise AssertionError(f"Unexpected posting content read for {url}")


class ScriptedPriceCatalog:
    """A price source built from the pinned catalog, so no test fetches prices.

    ``rates`` replaces one model's published rates, ``dropped`` removes the
    catalog entries by model id, and ``error`` or ``empty`` make the fetch fail.
    ``unusable`` publishes data that is well formed enough to install but breaks
    when a price is read from it.
    """

    url = "https://prices.example.test/data.json"

    def __init__(
        self,
        *,
        rates: dict[str, float] | None = None,
        dropped: frozenset[str] = frozenset(),
        error: Exception | None = None,
        empty: bool = False,
        unusable: bool = False,
    ) -> None:
        """Script one fetch outcome."""
        self.rates = rates or {}
        self.dropped = dropped
        self.error = error
        self.empty = empty
        self.unusable = unusable
        self.fetches = 0

    def fetch(self) -> DataSnapshot | None:
        """Answer the scripted published price data."""
        from decimal import Decimal

        from genai_prices.data import providers
        from genai_prices.types import ModelPrice as CatalogPrice

        self.fetches += 1
        if self.error is not None:
            raise self.error
        if self.empty:
            return None
        if self.unusable:
            return DataSnapshot(
                providers=[replace(provider, models=None) for provider in providers],  # type: ignore[arg-type]
                from_auto_update=True,
            )
        rebuilt = []
        for provider in providers:
            models = []
            for model in provider.models:
                if model.id in self.dropped:
                    continue
                rate = self.rates.get(model.id)
                models.append(
                    model
                    if rate is None
                    else replace(
                        model,
                        prices=CatalogPrice(
                            input_mtok=Decimal(str(rate)),
                            output_mtok=Decimal(str(rate * 4)),
                            cache_read_mtok=Decimal("0.5"),
                            cache_write_mtok=Decimal("1.5"),
                            web_searches_kcount=Decimal(10),
                        ),
                    )
                )
            rebuilt.append(replace(provider, models=models))
        return DataSnapshot(providers=rebuilt, from_auto_update=True)


class ScriptedBoards:
    """Deterministic board source that reaches no network."""

    def __init__(
        self, boards: dict[str, list[SourceListing] | SourceRead], *, complete: bool = True
    ) -> None:
        self.boards = boards
        self.complete = complete
        self.requested: list[str] = []
        self.requested_urls: list[str] = []

    def listings(
        self,
        employer: str,
        url: str,
        *,
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        """Answer with the scripted read for the named source."""
        self.requested.append(employer)
        self.requested_urls.append(url)
        found = self.boards[employer]
        if isinstance(found, SourceRead):
            return found
        return SourceRead(
            listings=found,
            complete=self.complete,
            raw_count=len(found),
            outcome=(
                SourceReadOutcome.ATS_COMPLETE
                if self.complete
                else SourceReadOutcome.CRAWL_CONTINUATION
            ),
        )


class ScriptedPage(BoardApiFetcher):
    """A fetcher whose career-page read is scripted rather than networked."""

    def __init__(self, page: str = "") -> None:
        super().__init__()
        self.page = page
        self.fetched: list[str] = []

    def get_text(self, url: str, *, max_bytes: int = DETECT_BYTES) -> str:
        self.fetched.append(url)
        return self.page


class ScriptedApi(BoardApiFetcher):
    """Answer one board API call with a fixed payload and record what was asked."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.requested: list[str] = []

    def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
        self.requested.append(url)
        return self.payload


def published_listings(fetcher: SourceReader, source_name: str, url: str) -> list[SourceListing]:
    """Return what a board published, for tests that do not care about completeness."""
    read = fetcher.listings(source_name, url)
    return read.listings


def declaring_page(*postings: dict[str, Any]) -> str:
    """Build a careers page whose only posting evidence is its JSON-LD markup."""
    blocks = "".join(
        f'<script type="application/ld+json">{json.dumps(posting)}</script>' for posting in postings
    )
    return f"<html><head>{blocks}</head><body><p>Careers at Acme.</p></body></html>"


class ScriptedProvider:
    """A provider that answers from a script keyed on the requested schema.

    Keyed on the response schema rather than on call order, so a test stays
    readable when the pipeline retries a stage.
    """

    name = SUITE_PROVIDER
    usage = Usage(input_tokens=10, output_tokens=5, reasoning_tokens=3)

    def __init__(
        self,
        script: dict[str, Callable[..., dict[str, Any]]],
        credentials_ok: bool = True,
        web_search_sources: Sequence[Sequence[SearchSource]] | None = None,
        background_answers: Sequence[BackgroundPoll | Sequence[BackgroundPoll] | Exception]
        | None = None,
    ) -> None:
        self.script = script
        self.credentials_ok = credentials_ok
        self.calls: list[tuple[str, RequestPlan]] = []
        self.choices: list[ModelChoice] = []
        self.web_search_calls = 0
        # One entry per web-search call, consumed in order; falls back to the
        # default single source once exhausted or when nothing is configured.
        self.web_search_sources = (
            None if web_search_sources is None else [list(item) for item in web_search_sources]
        )
        # One entry per dispatch_background call, consumed in order.
        self._background_answers = list(background_answers or ())
        self.background_dispatch_calls: list[RequestPlan] = []
        self.background_metadata: list[dict[str, str]] = []
        self.background_cancelled: list[str] = []
        self.background_dispatches: list[BackgroundDispatch] = []
        self._background_queues: dict[str, list[BackgroundPoll]] = {}

    def complete(
        self, plan: RequestPlan, schema: type[ModelT], choice: ModelChoice
    ) -> tuple[ModelT, CallRecord]:
        key = next((name for name in self.script if name in schema.__name__), None)
        if key is None:
            raise AssertionError(f"No scripted response for {schema.__name__}")
        self.calls.append((schema.__name__, plan))
        self.choices.append(choice)
        record = CallRecord(
            usage=self.usage,
            model=choice.model,
            response_id="resp_scripted",
        )
        respond = self.script[key]
        # A writer's ids are in the request text, so its script reads the request too.
        answer = respond(schema, plan) if reads_request(respond) else respond(schema)
        return schema.model_validate(answer), record

    def complete_with_web_search(
        self, plan: RequestPlan, schema: type[ModelT], choice: ModelChoice
    ) -> tuple[ModelT, CallRecord]:
        """Exercise the server-search capability without touching a network."""
        self.web_search_calls += 1
        value, record = self.complete(plan, schema, choice)
        sources = (
            tuple(self.web_search_sources.pop(0))
            if self.web_search_sources
            else (SearchSource(url="https://boards.example.test/1", title="An opening"),)
        )
        searched = Search(queries=("scripted search",), sources=sources)
        return value, replace(record, searches=(searched,), tool_version="scripted_web_search")

    def check_credentials(self) -> None:
        from clearscout.shared.errors import AuthError

        if not self.credentials_ok:
            raise AuthError("that key was rejected")

    def available_models(self) -> frozenset[str] | None:
        return frozenset({"claude-sonnet-5", "gpt-6.1-sol"})

    def dispatch_background(
        self,
        plan: RequestPlan,
        schema: type[ModelT],
        choice: ModelChoice,
        *,
        metadata: dict[str, str],
    ) -> BackgroundDispatch:
        """Send one scripted background request and return its id and request envelope."""
        index = len(self.background_dispatch_calls)
        self.background_dispatch_calls.append(plan)
        self.background_metadata.append(metadata)
        self.choices.append(choice)
        if index >= len(self._background_answers):
            raise AssertionError(f"No scripted background answer for dispatch {index}")
        answer = self._background_answers[index]
        if isinstance(answer, Exception):
            raise answer
        response_id = f"resp_scripted_{index}"
        queue = [answer] if isinstance(answer, BackgroundPoll) else list(answer)
        self._background_queues[response_id] = queue
        dispatched = BackgroundDispatch(
            response_id=response_id,
            request_envelope={
                "model": choice.model,
                "input": plan.task.text,
                "background": True,
                "metadata": dict(metadata),
            },
        )
        self.background_dispatches.append(dispatched)
        return dispatched

    def poll_background(
        self, response_id: str, schema: type[ModelT], ceiling: int
    ) -> BackgroundPoll:
        """Return this response's next scripted poll, holding on the last one."""
        queue = self._background_queues[response_id]
        if len(queue) > 1:
            return queue.pop(0)
        return queue[0]

    def cancel_background(self, response_id: str) -> None:
        """Record a cancellation request without changing what polling returns."""
        self.background_cancelled.append(response_id)


class MemoryStore:
    """A KeyStore backed by a dictionary, for tests that must not touch a keyring."""

    def __init__(self, initial: dict[ProviderName, str] | None = None) -> None:
        self.keys: dict[ProviderName, str] = dict(initial or {})

    def get(self, provider: ProviderName) -> str | None:
        return self.keys.get(provider)

    def set(self, provider: ProviderName, key: str) -> None:
        self.keys[provider] = key

    def forget(self, provider: ProviderName) -> None:
        self.keys.pop(provider, None)


class ScriptedPrompter:
    """A Prompter that answers from a script, so onboarding tests need no terminal."""

    def __init__(
        self,
        secrets: list[str] | None = None,
        answers: list[str] | None = None,
        confirm: bool = True,
        decline: list[str] | None = None,
        interactive: bool = True,
    ) -> None:
        self.secrets = list(secrets or [])
        self.answers = list(answers or [])
        self._confirm = confirm
        # Questions to answer no to, matched on a substring, so a test can
        # decline one prompt without declining every prompt.
        self._decline = list(decline or [])
        self._interactive = interactive
        self.messages: list[str] = []
        self.opened: list[str] = []

    def interactive(self) -> bool:
        return self._interactive

    def notice(self, message: str) -> None:
        self.messages.append(message)

    def plain(self, message: str) -> None:
        # The pipeline narrates through the same object it asks questions with.
        self.messages.append(message)

    def confirm(self, question: str, default: bool = True) -> bool:
        self.messages.append(question)
        if any(fragment in question for fragment in self._decline):
            return False
        return self._confirm

    def ask(self, question: str, default: str = "") -> str:
        self.messages.append(question)
        return self.answers.pop(0) if self.answers else default

    def secret(self, question: str) -> str:
        self.messages.append(question)
        return self.secrets.pop(0) if self.secrets else ""

    def open_url(self, url: str) -> None:
        self.opened.append(url)

    def step(self, index: int, total: int, label: str) -> None:
        self.messages.append(f"[{index}/{total}] {label}")

    @property
    def transcript(self) -> str:
        return "\n".join(self.messages)


@dataclass(frozen=True)
class FakeScreenResult:
    """Typed pass-through for ``pipeline.screen`` when its consumers are mocked.

    The batch.recommend call that would read spec, evidence, inventory, jd, and
    reading is itself mocked here, so those stay opaque.
    """

    spec: object
    evidence: object
    inventory: object
    ledger: Ledger
    jd: object
    reading: object
    report: Path | None = None


@dataclass(frozen=True)
class FakeRunResult:
    """Typed pass-through for ``pipeline.run`` in the workflow-runner tests."""

    verification: Report
    ledger: Ledger
    serialized: SerializedRunArtifacts


@dataclass(frozen=True)
class FakeBatchScreening:
    """One screening the workflow runner reads out of a batch."""

    key: str
    ledger: Ledger
    report: Path | None


@dataclass(frozen=True)
class FakeBatchAnalysis:
    """Typed pass-through for ``batch.screen_batch``."""

    screenings: tuple[FakeBatchScreening, ...]
    recommendations: dict[str, Recommendation]


@dataclass
class FakeTaskModule:
    """Typed stand-in for an Inspect task module.

    The runner reads exactly these module-level symbols, so asking for another
    fails loudly; ``SCORE_KIND`` keeps a real module's default.
    """

    TASK_NAME: str
    DATASET_PATH: Path | None
    PRODUCT_QUESTION: Any
    solver: Callable[[], Any]
    scorer: Callable[[], Any]
    SCORE_KIND: str = "judged"


class FakeProvider:
    """Empty stand-in for a Provider whose calling stage the test monkeypatches."""

    name = SUITE_PROVIDER


@dataclass
class FakeImportResult:
    """Typed stand-in for ImportResult, carrying only the fields the caller reads."""

    passed: bool
    markdown: str
    report: Path
    ledger: Ledger = field(default_factory=Ledger)


__all__ = [
    "FakeBatchAnalysis",
    "FakeBatchScreening",
    "FakeImportResult",
    "FakeProvider",
    "FakeRunResult",
    "FakeScreenResult",
    "FakeTaskModule",
    "MemoryStore",
    "ScriptedPrompter",
    "ScriptedProvider",
    "UnexpectedPostingFetcher",
]
