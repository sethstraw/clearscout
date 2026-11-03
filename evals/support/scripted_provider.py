"""Scripted provider the evaluation harness owns independently of tests.

``ScriptedProvider`` implements the in-process ``Provider`` protocol the
``Client`` accepts directly: no HTTP, no SDK, no vendor envelope. A change to
that protocol is made here and in ``tests/support/`` together.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any, TypeVar

from pydantic import BaseModel

from clearscout.application.config import ModelChoice, ProviderName
from clearscout.infrastructure.providers.base import CallRecord, Search, SearchSource
from clearscout.infrastructure.providers.plan import RequestPlan
from clearscout.shared.costs import Usage

ModelT = TypeVar("ModelT", bound=BaseModel)

EVAL_PROVIDER = ProviderName.OPENAI


class ScriptedProvider:
    """In-process provider that answers from a schema-keyed script.

    The Client accepts any object satisfying the ``Provider`` protocol; this
    one skips the vendor envelope entirely and returns validated pydantic
    models directly. The script is keyed on a substring of the response
    schema's class name, so a match-stage call routes to whichever script
    entry mentions ``EvidenceMap``.
    """

    name = EVAL_PROVIDER

    def __init__(
        self,
        script: dict[str, Callable[[type[BaseModel]], dict[str, Any]]],
        credentials_ok: bool = True,
    ) -> None:
        """Store the schema-keyed script and remember every call made through it."""
        self.script = script
        self.credentials_ok = credentials_ok
        self.calls: list[tuple[str, RequestPlan]] = []
        self.choices: list[ModelChoice] = []
        self.web_search_calls = 0

    def complete(
        self, plan: RequestPlan, schema: type[ModelT], choice: ModelChoice
    ) -> tuple[ModelT, CallRecord]:
        """Return the validated payload the script produces for this schema."""
        key = next((name for name in self.script if name in schema.__name__), None)
        if key is None:
            raise AssertionError(f"No scripted response for {schema.__name__}")
        self.calls.append((schema.__name__, plan))
        self.choices.append(choice)
        record = CallRecord(
            usage=Usage(input_tokens=10, output_tokens=5, reasoning_tokens=3),
            model=choice.model,
            response_id="resp_scripted",
        )
        return schema.model_validate(self.script[key](schema)), record

    def complete_with_web_search(
        self, plan: RequestPlan, schema: type[ModelT], choice: ModelChoice
    ) -> tuple[ModelT, CallRecord]:
        """Answer with a fabricated web-search citation set."""
        self.web_search_calls += 1
        value, record = self.complete(plan, schema, choice)
        searched = Search(
            queries=("scripted search",),
            sources=(SearchSource(url="https://boards.example.test/1", title="An opening"),),
        )
        return value, replace(record, searches=(searched,), tool_version="scripted_web_search")

    def check_credentials(self) -> None:
        """Raise when the script marks the credential invalid."""
        from clearscout.shared.errors import AuthError

        if not self.credentials_ok:
            raise AuthError("scripted credential refused")

    def available_models(self) -> frozenset[str] | None:
        """Return the small vocabulary the scripted mode advertises."""
        return frozenset({"claude-sonnet-5", "gpt-6.1-sol"})
