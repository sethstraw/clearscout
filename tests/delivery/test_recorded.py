"""Replay real recorded traffic through the real SDKs.

The vendor SDK builds the request, sets its own authentication headers, and
parses a body a real server actually sent. Recording a cassette needs a live
key, so a stage with no cassette skips and a fresh clone stays green, while a
canned exchange per stage proves the record-and-replay harness offline.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from collections import Counter
from collections.abc import Callable
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx2
import pytest

from clearscout.application.config import ProviderName, load_settings
from clearscout.application.master_cv.cv import parse_cv
from tests.support.cassette import Cassette, RecordingTransport, redact, replaying_client
from tests.support.fake_provider import FakeProviderAPI, openai_events
from tests.support.payloads import script
from tests.support.provider_clients import provider_for_http
from tests.support.suite import CASSETTES, REPO_ROOT

CASSETTE_CONFIG = CASSETTES / "record.toml"
CASSETTE_CV = CASSETTES / "master_cv.md"
RECORD_HINT = (
    "No {stage} cassette found. Record one with:\n"
    "  uv run python scripts/record_cassette.py --provider openai --stage {stage}"
)
FAKE_KEY = "not-a-real-key"


@cache
def recorder() -> ModuleType:
    """Load the recording script, whose stage drivers the replay sends again.

    Loaded from its path: `scripts/` is a directory of programs, and putting it
    on `sys.path` leaves it there for every test that follows.
    """
    spec = importlib.util.spec_from_file_location(
        "record_cassette", REPO_ROOT / "scripts" / "record_cassette.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Its dataclasses resolve their annotations through the module registry.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def provider_from(cassette: Cassette, provider: ProviderName) -> Any:
    """Build a real provider whose transport replays the cassette."""
    return provider_for_http(provider, replaying_client(cassette))


def _no_wait(seconds: float) -> None:
    del seconds


def replay(
    stage: str,
    provider: Any,
    name: ProviderName,
    workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Drive one stage exactly as the recorder did, through ``provider``, and return its result."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    workdir.mkdir(parents=True, exist_ok=True)
    run = recorder().stage_run(provider, name, workdir, api_key=FAKE_KEY, sleeper=_no_wait)
    return recorder().STAGES[stage](run)


def _cited_ids(report: str, inventory: Any) -> set[str]:
    """Return the CV identifiers a written report cites."""
    prefixes = {entry.id.split("-", 1)[0] for entry in inventory.entries}
    return {
        found
        for found in re.findall(r"\b[a-z]{1,3}-[0-9a-f]{4,}\b", report)
        if found.split("-", 1)[0] in prefixes
    }


def check_run(result: Any) -> None:
    """The run wrote a checked package with a critiqued resume and a critiqued letter."""
    assert result.materialized is not None
    materialized = result.materialized
    assert materialized.document.is_file()
    assert materialized.document.suffix == ".md"
    assert result.verification.passed
    assert materialized.report.is_file()
    assert (materialized.directory / "run.json").is_file()
    assert materialized.cover_letter is not None and materialized.cover_letter.is_file()
    assert result.verification.critic is not None, "the resume critic never answered"
    stages = Counter(entry.stage for entry in result.ledger.entries)
    assert stages["cover"] >= 1, "no cover letter was drafted"
    report = materialized.report.read_text(encoding="utf-8")
    _, found, letter = report.partition("## Critic")
    assert found, "the report has no critic section"
    letter = letter.partition("### Cover letter")[2].partition("\n## ")[0]
    # Only an answered critique renders scores; an unread letter renders its reason.
    assert "Hiring-quality mean" in letter, "the letter critic never answered"

    # Genuine model output, so assert the guarantee rather than the wording:
    # every identifier the report cites is one the fixture CV contains.
    inventory = parse_cv(CASSETTE_CV)
    known = {entry.id for entry in inventory.entries}
    cited = _cited_ids(report, inventory)
    assert cited, "the provenance table cited nothing"
    assert cited <= known, f"cites identifiers absent from the CV: {sorted(cited - known)}"


def check_vision(read: Any) -> None:
    """The screenshot came back as posting text long enough to tailor against."""
    assert read.method == "vision (screenshot)"
    assert "Product Owner" in read.text


def check_structure(result: Any) -> None:
    """The import wrote a CV whose every extracted piece cites a real source line."""
    assert result.path is not None and result.path.is_file()
    assert "EXAMPLE" in result.markdown
    assert result.findings, "nothing checked the import"
    lines = {line.number for line in result.source.lines}
    assert all(set(piece.lines) <= lines for piece in result.extracted.sourced())


def check_relevance_review(reviewed: Any) -> None:
    """Every posting in the batch was answered, and kept answers cite only CV entries."""
    case, valid, failures = reviewed
    answered = {item.pending_discovery_id for item in [*valid, *failures]}
    assert answered == case.pending_ids
    assert valid, f"every answer failed validation: {failures}"
    known = set(case.entry_ids)
    assert all(set(item.master_cv_evidence_ids) <= known for item in valid)


def check_search_vocabulary(proposed: Any) -> None:
    """The proposal names at least one title, and everything it cites is the CV's own."""
    from evals.inspect.solvers.search_vocabulary_live import case_inventory

    from clearscout.application.tailoring.stages.context import citable_entries

    assert proposed.titles, "the model proposed no title"
    assert all(title.evidence_ids for title in proposed.titles)
    sample = {"input": recorder().stage_inputs("search_vocabulary")}
    known = {entry.id for entry in citable_entries(case_inventory(sample))}
    proposals = [*proposed.titles, *proposed.archetypes, *proposed.capabilities, *proposed.domains]
    cited = {found for item in proposals for found in item.evidence_ids}
    assert cited <= known, f"cites identifiers absent from the CV: {sorted(cited - known)}"


def check_posting_recovery(search: Any) -> None:
    """The cited search settled into a decision for the one identity it was asked about."""
    from evals.inspect.solvers.web_stage_live import recovery_target

    target = recovery_target(recorder().stage_inputs("posting_recovery"))
    assert search.response.identity_id == target.id


def check_guided_campaign(outcome: Any) -> None:
    """The background attempt finished within its search cap."""
    from evals.inspect.solvers.web_stage_live import campaign_result

    request, poll = outcome
    assert poll.status == "complete", poll.error
    result = campaign_result(request, poll)
    assert 1 <= result["search_count"] <= request.attempt.search_cap


# What must hold of each replayed stage, declared apart from the recorder's list.
CHECKS: dict[str, Callable[[Any], None]] = {
    "run": check_run,
    "vision": check_vision,
    "structure": check_structure,
    "relevance_review": check_relevance_review,
    "search_vocabulary": check_search_vocabulary,
    "posting_recovery": check_posting_recovery,
    "guided_campaign": check_guided_campaign,
}


def _stage_cassettes() -> list[Any]:
    """Return one replay per recorded stage cassette, or a skip naming how to record it."""
    params: list[Any] = []
    for stage in CHECKS:
        found = [
            path
            for name in ProviderName
            if (path := recorder().cassette_path(stage, name)).is_file()
        ]
        params.extend(pytest.param(stage, path, id=path.stem) for path in found)
        if not found:
            hint = RECORD_HINT.format(stage=stage)
            params.append(pytest.param(stage, None, id=stage, marks=pytest.mark.skip(reason=hint)))
    return params


@pytest.mark.parametrize(("stage", "cassette_path"), _stage_cassettes())
@pytest.mark.recorded
def test_a_recorded_stage_replays_through_the_real_sdk(
    stage: str, cassette_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each stage completes against genuine model output, offline."""
    name = ProviderName(cassette_path.stem.removeprefix(f"{stage}_"))
    cassette = Cassette.load(cassette_path)
    CHECKS[stage](replay(stage, provider_from(cassette, name), name, tmp_path, monkeypatch))
    assert cassette.replayed == set(range(len(cassette.interactions))), (
        "a recorded call went unused"
    )


def test_every_recorded_stage_has_a_replay_check_and_a_canned_exchange() -> None:
    """A stage the recorder adds without a check would record money that nothing replays."""
    assert set(recorder().STAGES) == set(CHECKS) == set(CANNED)


def _web_search_call(query: str, url: str) -> dict[str, Any]:
    return {
        "type": "web_search_call",
        "id": "ws_canned",
        "status": "completed",
        "action": {"type": "search", "query": query, "sources": [{"type": "url", "url": url}]},
    }


def _response(answer: dict[str, Any], *output: dict[str, Any], status: str = "completed") -> Any:
    """Return a finished Responses object whose message carries ``answer`` as JSON."""
    message = {
        "type": "message",
        "id": "msg_canned",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": json.dumps(answer), "annotations": []}],
    }
    return {
        "id": "resp_canned",
        "object": "response",
        "created_at": 0,
        "status": status,
        "model": "gpt-6.1-sol",
        "output": [*output, message] if status == "completed" else [],
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 900,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 200,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 1100,
        },
    }


Upstream = Callable[[httpx2.Request], httpx2.Response]


def _canned_schemas(payloads: dict[str, Callable[..., dict[str, Any]]]) -> Upstream:
    return FakeProviderAPI(ProviderName.OPENAI.value, payloads)


def _canned_run() -> Upstream:
    payloads = script({})
    matched = payloads["EvidenceMap"]

    # A transferable answer is what sends the passing draft to the critic.
    def transferable(schema: Any) -> dict[str, Any]:
        answer = matched(schema)
        answer["matches"][0]["classification"] = "TRANSFERABLE"
        return answer

    return _canned_schemas({**payloads, "EvidenceMap": transferable})


def _canned_vision() -> Upstream:
    text = (
        "Clinical Data Product Owner\nNorthwind Health Data - Remote within Canada\n\n"
        + "Own the roadmap for terminology and master data products with data stewards. " * 8
    )
    return _canned_schemas({"VisionTranscript": lambda schema: {"text": text}})


def _canned_structure() -> Upstream:
    return _canned_schemas(
        {"ImportedCv": lambda schema: {"name": {"text": "JANE Q. EXAMPLE", "lines": [1]}}}
    )


def _canned_relevance_review() -> Upstream:
    def judged(alias: str, recommendation: str, basis: str) -> dict[str, Any]:
        direct = recommendation == "review"
        return {
            "pending_discovery_id": alias,
            "recommendation": recommendation,
            "summary": "Read from the posting's own words.",
            "outcome": "judged",
            "core_work_alignment": "direct" if direct else "absent",
            "core_qualification_alignment": "established" if direct else "absent",
            "hard_condition": "unresolved",
            "matched_exclusion": None,
            "decisive_basis": basis,
            "posting_evidence_span_ids": [f"{alias}:1"],
            "master_cv_evidence_ids": [],
            "unresolved_questions": [],
        }

    answer = {
        "recommendations": [
            judged("p1", "review", "core_match"),
            judged("p2", "unlikely", "core_gap"),
        ]
    }
    return _canned_schemas({"Relevance": lambda schema: answer})


def _canned_search_vocabulary() -> Upstream:
    from evals.inspect.solvers.search_vocabulary_live import case_inventory

    sample = {"input": recorder().stage_inputs("search_vocabulary")}
    cited = case_inventory(sample).entries[0].id
    title = {"title": "Data Platform Engineer", "reason": "The record builds data platforms."}
    answer = {
        "titles": [{**title, "evidence_ids": [cited], "lane": "specialist"}],
        "archetypes": [],
        "capabilities": [],
        "domains": [],
    }
    return _canned_schemas({"SearchVocabulary": lambda schema: answer})


def _canned_posting_recovery() -> Upstream:
    from evals.inspect.solvers.web_stage_live import recovery_target

    target = recovery_target(recorder().stage_inputs("posting_recovery"))
    searched = _web_search_call(
        "Initech Clinical Data Product Owner", "https://careers.initech.example/jobs"
    )
    body = openai_events(_response({"identity_id": target.id}, searched))
    return lambda request: httpx2.Response(
        200, headers={"content-type": "text/event-stream"}, content=body
    )


def _canned_guided_campaign() -> Upstream:
    searched = _web_search_call(
        "Clinical Data Product Owner remote Canada", "https://jobs.example.com/postings"
    )
    finished = _response({"postings": [], "boards": [], "searches": []}, searched)
    # The dispatch is queued, the first poll is still running, the second has finished.
    answers = iter([{**finished, "status": "in_progress", "output": []}, finished])

    def upstream(request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST":
            return httpx2.Response(200, json={**finished, "status": "queued", "output": []})
        return httpx2.Response(200, json=next(answers))

    return upstream


# A synthetic exchange per stage, so the harness is proven before any money is spent.
CANNED: dict[str, Callable[[], Upstream]] = {
    "run": _canned_run,
    "vision": _canned_vision,
    "structure": _canned_structure,
    "relevance_review": _canned_relevance_review,
    "search_vocabulary": _canned_search_vocabulary,
    "posting_recovery": _canned_posting_recovery,
    "guided_campaign": _canned_guided_campaign,
}


@pytest.mark.parametrize("stage", sorted(CANNED))
@pytest.mark.recorded
def test_a_stage_recorded_from_a_canned_exchange_replays_through_the_real_sdk(
    stage: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recording then replaying one stage works end to end, with no key and no network.

    The exchange is canned, so this proves the recorder's driver, the cassette,
    and the replay check fit together, and nothing about model quality.
    """
    name = ProviderName.OPENAI
    cassette = Cassette(path=recorder().cassette_path(stage, name, tmp_path))
    upstream = httpx2.MockTransport(CANNED[stage]())
    recording = httpx2.Client(transport=RecordingTransport(cassette, inner=upstream))
    replay(stage, provider_for_http(name, recording), name, tmp_path / "recorded", monkeypatch)
    cassette.save()

    stored = Cassette.load(cassette.path)
    assert stored.interactions, "the driver sent nothing to record"
    replayed = replay(stage, provider_from(stored, name), name, tmp_path / "replayed", monkeypatch)
    CHECKS[stage](replayed)
    assert stored.replayed == set(range(len(stored.interactions))), "a recorded call went unused"


@pytest.mark.recorded
def test_the_run_check_refuses_a_letter_the_critic_left_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two critiqued resume attempts must not stand in for a critiqued letter."""
    name = ProviderName.OPENAI
    provider = provider_for_http(name, httpx2.Client(transport=httpx2.MockTransport(_canned_run())))
    result = replay("run", provider, name, tmp_path, monkeypatch)
    check_run(result)

    report = result.materialized.report
    text = report.read_text(encoding="utf-8")
    head, _, letter = text.partition("### Cover letter")
    _, heading, rest = letter.partition("\n## ")
    unread = "\n\nThe critic did not read the cover letter.\n"
    report.write_text(head + "### Cover letter" + unread + heading + rest, encoding="utf-8")

    with pytest.raises(AssertionError, match="letter critic"):
        check_run(result)


def test_the_vocabulary_check_refuses_an_id_the_cv_lacks() -> None:
    """Genuine output must cite only the record's own ids, wherever it cites them."""
    from evals.inspect.solvers.search_vocabulary_live import case_inventory

    from clearscout.application.discovery.search_vocabulary import ProposedSearch

    sample = {"input": recorder().stage_inputs("search_vocabulary")}
    cited = case_inventory(sample).entries[0].id
    title = {"title": "Data Platform Engineer", "reason": "Builds data platforms."}
    proposed = ProposedSearch.model_validate(
        {
            "titles": [{**title, "evidence_ids": [cited]}],
            "archetypes": [{"name": "Health data", "because": "Read.", "evidence_ids": [cited]}],
        }
    )
    check_search_vocabulary(proposed)

    invented = proposed.model_copy(
        update={
            "archetypes": [
                proposed.archetypes[0].model_copy(update={"evidence_ids": ["zz-0000beef"]})
            ]
        }
    )
    with pytest.raises(AssertionError, match="zz-0000beef"):
        check_search_vocabulary(invented)


@pytest.mark.recorded
def test_the_anthropic_replay_seam_builds_a_working_provider(tmp_path: Path) -> None:
    """The recorded suite ships only an OpenAI cassette, so this covers Anthropic.

    The exchange is canned rather than paid for, so it proves the wiring and
    nothing about model quality. The request is the one the Anthropic SDK builds
    for itself, which is what a cassette has to match on to replay at all.
    """
    import httpx2
    from anthropic import Anthropic
    from pydantic import BaseModel

    from clearscout.application.config import Effort, ModelChoice
    from clearscout.infrastructure.providers.anthropic import AnthropicProvider
    from tests.support.cassette import RecordingTransport
    from tests.support.fake_provider import anthropic_events
    from tests.support.plans import plan_for

    class Answer(BaseModel):
        value: str

    # The adapter streams, so the seam answers in server-sent events.
    body = anthropic_events(
        {
            "id": "msg_replayed",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-5",
            "content": [{"type": "text", "text": '{"value": "from the cassette"}'}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 11, "output_tokens": 7},
        }
    )
    plan = plan_for(max_tokens=64)
    choice = ModelChoice(model="claude-sonnet-5", effort=Effort.LOW)

    cassette = Cassette(path=tmp_path / "run_anthropic.json")
    recording = httpx2.Client(
        transport=RecordingTransport(
            cassette,
            inner=httpx2.MockTransport(
                lambda request: httpx2.Response(
                    200, content=body, headers={"content-type": "text/event-stream"}
                )
            ),
        )
    )
    AnthropicProvider(
        api_key="not-a-real-key",
        client=Anthropic(api_key="not-a-real-key", http_client=recording),
    ).complete(plan, Answer, choice)
    cassette.save()

    replayed, record = provider_from(Cassette.load(cassette.path), ProviderName.ANTHROPIC).complete(
        plan, Answer, choice
    )

    assert replayed.value == "from the cassette"
    assert record.model == "claude-sonnet-5"
    assert record.response_id == "msg_replayed"


def test_recording_inputs_exercise_the_live_gate_boundaries() -> None:
    """The paid fixture should test tailoring judgment, not a generic engineer match."""
    settings = load_settings(CASSETTE_CONFIG)
    posting = (CASSETTES / "posting.txt").read_text(encoding="utf-8")
    cv = CASSETTE_CV.read_text(encoding="utf-8")
    inventory = parse_cv(CASSETTE_CV)

    assert settings.master_cv() == CASSETTE_CV.resolve()
    assert inventory.roles[0].title == "Reference Data Squad, Product Owner"
    assert inventory.roles[0].title_of_record == "Clinical Data Engineer - Life Sciences"
    assert "Three or more years of software product-management experience" in posting
    assert "enterprise master data management platform" in posting
    assert "March 2023 to September 2025" in cv
    assert "without enterprise MDM platform implementation" in cv
    assert len(settings.rules.personal_distinctions) == 3


def test_the_recording_fixtures_are_invented_rather_than_anonymised() -> None:
    """A public repository is not the place for a real organization or a real career.

    These two files are the paid inputs, so they are the ones most likely to be
    read as a sample of somebody's actual record. Everything in them describes
    an invented candidate, and the settings file says so rather than leaving a
    reader to infer it.
    """
    import tomllib

    cv = CASSETTE_CV.read_text(encoding="utf-8")
    text = CASSETTE_CONFIG.read_text(encoding="utf-8")
    config = tomllib.loads(text)

    assert cv.startswith("# JANE Q. EXAMPLE")
    assert "example.com" in cv
    assert "invented candidate" in text

    # An absolute POSIX path is not a path on every platform, and where the
    # throwaway package lands has no effect on what is recorded.
    assert "output_dir" not in config
    assert not str(config["cv"]).startswith("/")


def test_committed_cassettes_carry_no_credential() -> None:
    """Whatever is in the repository now, none of it may be a key."""
    for path in CASSETTES.glob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert "sk-ant-" not in text
        assert "sk-proj-" not in text
        assert '"authorization"' not in text.lower()
        assert '"x-api-key"' not in text.lower()


@pytest.mark.parametrize(
    "header", ["openai-project", "x-request-id", "request-id", "cf-ray", "date"]
)
def test_recording_omits_provider_account_and_request_metadata(tmp_path: Path, header: str) -> None:
    import httpx2

    from tests.support.cassette import RecordingTransport

    def upstream(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers={header: "identifying-metadata", "content-type": "application/json"},
            json={"answer": "retained"},
        )

    cassette = Cassette(path=tmp_path / "metadata.json")
    with httpx2.Client(
        transport=RecordingTransport(cassette, inner=httpx2.MockTransport(upstream))
    ) as client:
        client.get("https://api.openai.com/v1/models")
    cassette.save()

    stored = Cassette.load(cassette.path)
    assert header not in stored.interactions[0].headers
    with replaying_client(stored) as client:
        response = client.get("https://api.openai.com/v1/models")
    assert response.json() == {"answer": "retained"}
    assert response.headers["content-type"] == "application/json"


def test_redaction_survives_a_key_in_a_response_body(tmp_path: Path) -> None:
    """Headers are dropped wholesale; a key echoed in a body has to be caught too."""
    import httpx2

    from tests.support.cassette import RecordingTransport

    def upstream(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers={"x-api-key": "sk-ant-api03-should-never-persist"},
            json={"error": "invalid key sk-ant-api03-should-never-persist supplied"},
        )

    cassette = Cassette(path=tmp_path / "leaky.json")
    client = httpx2.Client(
        transport=RecordingTransport(cassette, inner=httpx2.MockTransport(upstream))
    )
    client.get("https://api.anthropic.com/v1/models")
    cassette.save()

    written = cassette.path.read_text(encoding="utf-8")
    assert "sk-ant-api03-should-never-persist" not in written
    assert "x-api-key" not in written.lower()
    assert "REDACTED" in written


def test_matching_is_by_content_not_call_order(tmp_path: Path) -> None:
    """Skipping a cached stage must not shift every later reply by one."""
    import httpx2

    from tests.support.cassette import ReplayTransport

    cassette = Cassette(path=tmp_path / "two.json")
    for body in (b"first", b"second"):
        cassette.add(
            httpx2.Request("POST", "https://api.anthropic.com/v1/messages", content=body),
            httpx2.Response(200, json={"which": body.decode()}),
        )

    transport = ReplayTransport(cassette)
    # Ask for the second interaction first. Order-based matching would hand back
    # the first, which is exactly what happens when a run skips a cached stage.
    second = transport.handle_request(
        httpx2.Request("POST", "https://api.anthropic.com/v1/messages", content=b"second")
    )
    assert second.json() == {"which": "second"}
    first = transport.handle_request(
        httpx2.Request("POST", "https://api.anthropic.com/v1/messages", content=b"first")
    )
    assert first.json() == {"which": "first"}


def _replays(recorded: bytes, sent: bytes, tmp_path: Path) -> bool:
    """Return whether a request sent with ``sent`` finds the one recorded with ``recorded``."""
    import httpx2

    url = "https://api.openai.com/v1/responses"
    cassette = Cassette(path=tmp_path / "one.json")
    cassette.add(httpx2.Request("POST", url, content=recorded), httpx2.Response(200, json={}))
    cassette.save()
    return Cassette.load(cassette.path).find(httpx2.Request("POST", url, content=sent)) is not None


def test_a_json_body_matches_whatever_order_the_sdk_writes_its_top_level_keys(
    tmp_path: Path,
) -> None:
    recorded = b'{"input":[{"role":"user","content":"hi"}],"model":"m","stream":true}'
    sent = b'{"model": "m", "stream": true, "input": [{"role": "user", "content": "hi"}]}'
    assert _replays(recorded, sent, tmp_path)


def test_a_json_body_with_reordered_schema_properties_does_not_match(tmp_path: Path) -> None:
    recorded = b'{"model":"m","schema":{"properties":{"title":{},"score":{}}}}'
    sent = b'{"model":"m","schema":{"properties":{"score":{},"title":{}}}}'
    assert not _replays(recorded, sent, tmp_path)


def test_a_json_body_with_a_changed_value_does_not_match(tmp_path: Path) -> None:
    recorded = b'{"model":"m","stream":true}'
    assert not _replays(recorded, b'{"stream":true,"model":"other"}', tmp_path)


def test_a_body_that_is_not_json_matches_by_its_bytes(tmp_path: Path) -> None:
    assert _replays(b"plain body", b"plain body", tmp_path)
    assert not _replays(b"plain body", b"plain  body", tmp_path)


def test_identical_requests_replay_their_answers_in_recorded_order(tmp_path: Path) -> None:
    """Repeated critic trials send one body; each must get its own recorded draw."""
    import httpx2

    from tests.support.cassette import ReplayTransport

    cassette = Cassette(path=tmp_path / "trials.json")
    for draw in ("first draw", "second draw"):
        cassette.add(
            httpx2.Request("POST", "https://api.openai.com/v1/responses", content=b"same"),
            httpx2.Response(200, json={"draw": draw}),
        )

    transport = ReplayTransport(cassette)
    answers = [
        transport.handle_request(
            httpx2.Request("POST", "https://api.openai.com/v1/responses", content=b"same")
        ).json()
        for _ in range(2)
    ]

    assert answers == [{"draw": "first draw"}, {"draw": "second draw"}]


def test_redaction_removes_key_shaped_values() -> None:
    assert "sk-ant-abcdefgh12345" not in redact("token sk-ant-abcdefgh12345 end")
    assert "REDACTED" in redact("token sk-ant-abcdefgh12345 end")


def test_an_unrecorded_request_fails_instead_of_going_live(tmp_path: Path) -> None:
    """An offline suite must stay offline even when a cassette is incomplete."""
    import httpx2

    from tests.support.cassette import ReplayTransport

    transport = ReplayTransport(Cassette(path=tmp_path / "empty.json"))
    with pytest.raises(LookupError, match="Re-record"):
        transport.handle_request(httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))


@pytest.mark.recorded
def test_record_then_replay_survives_a_round_trip_through_the_real_sdk(tmp_path: Path) -> None:
    """The mechanism itself: record real bytes, replay them, let the SDK parse.

    A model listing is used because its wire format is small and its parsing is
    the SDK's own. If a cassette can carry that through httpx2 and out of the
    vendor's client as typed objects, it can carry a completion too.
    """
    import httpx2
    from anthropic import Anthropic

    from clearscout.infrastructure.providers.anthropic import AnthropicProvider
    from tests.support.cassette import RecordingTransport

    listing = {
        "data": [
            {"id": "claude-sonnet-5", "type": "model", "display_name": "Claude Sonnet 5"},
            {"id": "claude-haiku-4-5-20251001", "type": "model", "display_name": "Claude Haiku"},
        ],
        "has_more": False,
        "first_id": "claude-sonnet-5",
        "last_id": "claude-haiku-4-5-20251001",
    }

    def upstream(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["authorization"] == "Bearer sk-ant-secret-value"
        return httpx2.Response(200, json=listing)

    cassette = Cassette(path=tmp_path / "models.json")
    recorder = httpx2.Client(
        transport=RecordingTransport(cassette, inner=httpx2.MockTransport(upstream))
    )
    recorded = AnthropicProvider(
        api_key="sk-ant-secret-value",
        client=Anthropic(api_key="sk-ant-secret-value", http_client=recorder),
    )
    assert "claude-sonnet-5" in (recorded.available_models() or frozenset())
    cassette.save()

    written = cassette.path.read_text(encoding="utf-8")
    assert "sk-ant-secret-value" not in written

    replayed = AnthropicProvider(
        api_key="not-a-real-key",
        client=Anthropic(
            api_key="not-a-real-key", http_client=replaying_client(Cassette.load(cassette.path))
        ),
    )
    assert replayed.available_models() == frozenset(
        {"claude-sonnet-5", "claude-haiku-4-5-20251001"}
    )


@pytest.mark.recorded
def test_a_recorded_rejection_replays_as_a_real_auth_error(tmp_path: Path) -> None:
    """Recording a 401 lets the credential path be tested against a real refusal."""
    import httpx2
    from anthropic import Anthropic

    from clearscout.infrastructure.providers.anthropic import AnthropicProvider
    from clearscout.shared.errors import AuthError
    from tests.support.cassette import RecordingTransport

    def upstream(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            401,
            json={"type": "error", "error": {"type": "authentication_error", "message": "invalid"}},
        )

    cassette = Cassette(path=tmp_path / "rejected.json")
    client = httpx2.Client(
        transport=RecordingTransport(cassette, inner=httpx2.MockTransport(upstream))
    )
    provider = AnthropicProvider(
        api_key="bad", client=Anthropic(api_key="bad", http_client=client, max_retries=0)
    )
    with pytest.raises(AuthError, match="console.anthropic.com"):
        provider.check_credentials()
    cassette.save()

    replayed = AnthropicProvider(
        api_key="bad",
        client=Anthropic(
            api_key="bad",
            http_client=replaying_client(Cassette.load(cassette.path)),
            max_retries=0,
        ),
    )
    with pytest.raises(AuthError, match="console.anthropic.com"):
        replayed.check_credentials()


def test_the_recorder_substitutes_provider_for_compatibly() -> None:
    """The recorder replaces `provider_for`, so it must be callable the same way.

    A signature drift would otherwise surface only during a paid recording.
    """
    import inspect

    from clearscout.infrastructure.providers.base import provider_for

    real = inspect.signature(provider_for)
    stand_in = inspect.signature(recorder().recording_provider(Cassette(path=Path("x"))))
    assert list(stand_in.parameters) == list(real.parameters)
    for name, parameter in real.parameters.items():
        assert stand_in.parameters[name].default == parameter.default, name


def test_a_gzipped_response_replays_as_the_text_that_was_stored(tmp_path: Path) -> None:
    """A stored body is plain text, so replay must not claim it is gzipped.

    Recording reads the response, so httpx2 has already decompressed it. Keeping
    `content-encoding: gzip` beside it makes httpx2 fail to gunzip on replay,
    which the vendor SDK surfaces as a connection error.
    """
    import gzip

    import httpx2

    from tests.support.cassette import RecordingTransport

    payload = b'{"id": "resp_1", "object": "response"}'

    def upstream(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers={"content-encoding": "gzip", "content-type": "application/json"},
            content=gzip.compress(payload),
        )

    cassette = Cassette(path=tmp_path / "gzipped.json")
    client = httpx2.Client(
        transport=RecordingTransport(cassette, inner=httpx2.MockTransport(upstream))
    )
    client.post("https://api.openai.com/v1/responses", content=b"{}")
    cassette.save()

    # Neither the stored headers nor the replayed ones may describe an encoding
    # that the stored body does not have.
    stored = Cassette.load(cassette.path)
    assert "content-encoding" not in {name.lower() for name in stored.interactions[0].headers}

    replayed = replaying_client(stored)
    answer = replayed.post("https://api.openai.com/v1/responses", content=b"{}")
    assert answer.json() == {"id": "resp_1", "object": "response"}
