"""One posting to one written package, through the vendor SDKs, over HTTP.

The provider stands up as a real HTTP endpoint speaking each vendor's wire
format, so the SDK builds the request and parses the response and the only thing
replaced is the socket. The content is canned, so this says nothing about model
quality and everything about whether the parts fit together.
"""

from __future__ import annotations

import json
import re
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import pytest

from clearscout.application.config import Effort, ModelChoice, ProviderName, Settings
from clearscout.shared.errors import AuthError
from tests.support.fake_provider import FakeProviderAPI
from tests.support.payloads import independent_script, script
from tests.support.provider_clients import provider_for_http
from tests.support.suite import POSTING

# An identifier as the report prints it: a short kind prefix and a content hash.
CITATION = re.compile(r"\b[a-z]{1,3}-[0-9a-f]{4,}\b")


def assert_a_whole_run_happened(result: Any, master_cv: Path) -> None:
    """Assert what a finished run must be true of, whatever the model said.

    Exact strings are the wrong assertion against a language model: they fail on
    a rewording that changed nothing, and they pass on output that is fluent and
    fabricated. These are properties the product promises, and they hold for any
    correct run regardless of wording.
    """
    from clearscout.application.master_cv.cv import parse_cv

    materialized = result.materialized
    assert materialized is not None, "the CLI-shaped run did not materialize its package"
    document = materialized.document.read_text(encoding="utf-8")
    report = materialized.report.read_text(encoding="utf-8")

    assert materialized.document.suffix == ".md"
    assert len(document) > 400, "a resume this short means a stage returned nothing"
    assert materialized.report.is_file()
    assert (materialized.directory / "run.json").is_file(), "the run recorded no manifest"

    # Every stage ran and something was checked. A run that quietly skipped
    # drafting would still write files, and this is what tells them apart.
    assert result.ledger.entries, "no model call was recorded"
    assert result.ledger.total_usage.output_tokens > 0, "no tokens were accounted for"
    assert result.verification.findings, "nothing was checked"
    assert result.verification.critic is not None, "the critic never answered"

    # The truth boundary, observed from outside the program: every identifier
    # the report cites is one the master CV actually contains.
    inventory = parse_cv(master_cv)
    known = {entry.id for entry in inventory.entries}
    # Prefixes this CV produces, so a posting identifier is not mistaken for a
    # citation. Derived from the inventory, so a new entry kind is covered.
    prefixes = {entry.id.split("-", 1)[0] for entry in inventory.entries}
    cited = {found for found in CITATION.findall(report) if found.split("-", 1)[0] in prefixes}
    assert cited, "the provenance table cited nothing"
    unknown = cited - known
    assert not unknown, f"the report cites identifiers absent from the CV: {sorted(unknown)}"

    # A section a reader could mistake for a model's judgment, or for a score,
    # would be worse than not having it.
    assert "## Read from the text by rule" in report
    assert "not by a model" in report
    assert "not a score" in report
    assert result.reading is not None

    # A run writes somebody's career history to disk.
    for path in (materialized.document, materialized.report):
        assert path.stat().st_mode & 0o077 == 0, f"{path.name} is readable by others"


@pytest.mark.parametrize("vendor", [ProviderName.ANTHROPIC, ProviderName.OPENAI])
@pytest.mark.transport
def test_the_whole_pipeline_runs_through_the_real_sdk(
    vendor: ProviderName, master_cv: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ingest to written package, for each vendor, with no key and no network."""
    from clearscout.application.tailoring.pipeline import RunCommand, run

    api = FakeProviderAPI(vendor.value, independent_script())
    http = httpx.Client(transport=httpx.MockTransport(api))
    monkeypatch.setattr(
        "clearscout.application.tailoring.pipeline.service.provider_for",
        lambda name, key, timeout=None: provider_for_http(vendor, http, max_retries=0),
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")

    posting = tmp_path / "posting.txt"
    posting.write_text(POSTING, encoding="utf-8")

    with tempfile.TemporaryDirectory() as directory:
        settings = Settings(cv=master_cv, output_dir=Path(directory), pages=2, provider=vendor)
        result = run(RunCommand(source=str(posting), settings=settings, cache_enabled=False))
        assert_a_whole_run_happened(result, master_cv)

    # Only the critic may be asked more than once, once per configured trial; any
    # other repeat would be a stage running twice.
    assert Counter(entry.stage for entry in result.ledger.entries) == {
        "jobspec": 1,
        "match": 1,
        "draft": 1,
        "critic": settings.critic_trials,
    }
    assert len(api.requests) == 3 + settings.critic_trials

    # A request that went out without a response schema would let a model answer
    # in any shape it liked.
    for body in api.requests:
        assert sent_a_schema(vendor, body), "a request went out with no response schema"


def sent_a_schema(vendor: ProviderName, body: dict[str, Any]) -> bool:
    """Return whether this request constrained the response to a schema."""
    if vendor is ProviderName.ANTHROPIC:
        return bool(body.get("output_config", {}).get("format", {}).get("schema"))
    return bool(body.get("text", {}).get("format", {}).get("schema"))


@pytest.mark.parametrize("vendor", [ProviderName.ANTHROPIC, ProviderName.OPENAI])
@pytest.mark.transport
def test_the_official_sdk_parses_a_fixed_response_not_derived_from_its_schema(
    vendor: ProviderName,
) -> None:
    """A response fixture remains useful even when the request schema changes."""
    from pydantic import BaseModel

    from tests.support.plans import plan_for

    class Answer(BaseModel):
        value: str

    fixture = Path(__file__).parents[1] / "fixtures" / "provider_responses" / "answer.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    api = FakeProviderAPI(vendor.value, {"Answer": lambda schema: payload})
    http = httpx.Client(transport=httpx.MockTransport(api))
    provider = provider_for_http(vendor, http, max_retries=0)
    value, record = provider.complete(
        plan_for(max_tokens=64), Answer, ModelChoice(model="fixture", effort=Effort.LOW)
    )

    assert value.value == "independent fixture response"
    assert record.response_id
    assert api.requests


@pytest.mark.parametrize("vendor", [ProviderName.ANTHROPIC, ProviderName.OPENAI])
@pytest.mark.transport
def test_the_official_sdk_lists_models_and_checks_credentials(
    vendor: ProviderName,
) -> None:
    """Model discovery and credential checks use the vendor's real HTTP path."""
    calls: list[str] = []

    def serve(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200,
            request=request,
            json={
                "object": "list",
                "data": [
                    {
                        "id": "fixture-model",
                        "type": "model",
                        "display_name": "Fixture model",
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                ],
                "has_more": False,
            },
        )

    with httpx.Client(transport=httpx.MockTransport(serve)) as http:
        provider = provider_for_http(vendor, http, max_retries=0)
        assert provider.available_models() == frozenset({"fixture-model"})
        provider.check_credentials()

    assert calls == ["/v1/models", "/v1/models"]


@pytest.mark.parametrize("vendor", [ProviderName.ANTHROPIC, ProviderName.OPENAI])
@pytest.mark.transport
def test_the_official_sdk_translates_a_rejected_key(
    vendor: ProviderName,
) -> None:
    """A real SDK error envelope becomes the user-facing authentication error."""

    def reject(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            request=request,
            json={"error": {"type": "authentication_error", "message": "invalid key"}},
        )

    with httpx.Client(transport=httpx.MockTransport(reject)) as http:
        provider = provider_for_http(vendor, http, max_retries=0)
        with pytest.raises(AuthError, match="key was refused"):
            provider.check_credentials()


@pytest.mark.parametrize("vendor", [ProviderName.ANTHROPIC, ProviderName.OPENAI])
@pytest.mark.transport
def test_a_request_offers_only_identifiers_that_exist(
    vendor: ProviderName, master_cv: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The vocabulary that leaves the machine is drawn from the CV, and nothing else.

    Checked on the wire rather than on the pydantic model, because the schema
    the model is actually bound by is the one that survives serialization. A
    schema that was correct in Python and lost its enum on the way out would
    look identical to every other test in this suite.
    """
    from clearscout.application.master_cv.cv import parse_cv
    from clearscout.application.tailoring.pipeline import RunCommand, run
    from tests.support.schema import literals

    api = FakeProviderAPI(vendor.value, script({}))
    http = httpx.Client(transport=httpx.MockTransport(api))
    monkeypatch.setattr(
        "clearscout.application.tailoring.pipeline.service.provider_for",
        lambda name, key, timeout=None: provider_for_http(vendor, http, max_retries=0),
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")

    posting = tmp_path / "posting.txt"
    posting.write_text(POSTING, encoding="utf-8")
    with tempfile.TemporaryDirectory() as directory:
        run(
            RunCommand(
                source=str(posting),
                settings=Settings(
                    cv=master_cv, output_dir=Path(directory), pages=2, provider=vendor
                ),
                cache_enabled=False,
            )
        )

    known = {entry.id for entry in parse_cv(master_cv).entries}
    offered: set[str] = set()
    for body in api.requests:
        schema = (
            body.get("output_config", {}).get("format", {})
            if vendor is ProviderName.ANTHROPIC
            else body.get("text", {}).get("format", {})
        ).get("schema", {})
        for field in ("source_ids", "entry_ids"):
            offered.update(literals(schema, field))

    assert offered, "no request carried an evidence vocabulary"
    assert offered <= known, (
        f"identifiers offered that are not in the CV: {sorted(offered - known)}"
    )
