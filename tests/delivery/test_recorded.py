"""Replay real recorded traffic through the real SDKs.

The vendor SDK builds the request, sets its own authentication headers, and
parses a body a real server actually sent. Recording a cassette needs a live
key, so with none present these tests skip and a fresh clone stays green.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any

import pytest

from tailorcv.application.config import ProviderName, load_settings
from tailorcv.application.master_cv.cv import parse_cv
from tests.support.cassette import Cassette, redact, replaying_client
from tests.support.provider_clients import provider_for_http
from tests.support.suite import CASSETTES, REPO_ROOT

CASSETTE_CONFIG = CASSETTES / "record.toml"
CASSETTE_CV = CASSETTES / "master_cv.md"
RUN_CASSETTES = sorted(CASSETTES.glob("run_*.json"))
# The provider is read back from the filename, so run_openai.json replays as
# OpenAI without anything else being told.
RECORD_HINT = (
    "No cassette found. Record one with:\n"
    "  uv run python scripts/record_cassette.py tests/cassettes/posting.txt "
    "--config tests/cassettes/record.toml "
    "--out tests/cassettes/run_openai.json --provider openai"
)


def provider_from(cassette: Cassette, provider: ProviderName) -> Any:
    """Build a real provider whose transport replays the cassette."""
    return provider_for_http(provider, replaying_client(cassette))


@pytest.mark.parametrize(
    "cassette_path",
    RUN_CASSETTES or [pytest.param(None, marks=pytest.mark.skip(reason=RECORD_HINT))],
)
@pytest.mark.recorded
def test_a_recorded_run_replays_through_the_real_sdk(
    cassette_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pipeline completes against genuine model output, offline."""
    from tailorcv.application.tailoring.pipeline import RunCommand, run

    cassette = Cassette.load(cassette_path)
    provider = ProviderName(cassette_path.stem.removeprefix("run_"))
    monkeypatch.setattr(
        "tailorcv.application.tailoring.pipeline.service.provider_for",
        lambda name, key, timeout=None: provider_from(cassette, provider),
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")

    with tempfile.TemporaryDirectory() as directory:
        settings = load_settings(
            CASSETTE_CONFIG,
            overrides={"output_dir": Path(directory), "provider": provider},
        )
        result = run(
            RunCommand(
                source=str(CASSETTES / "posting.txt"), settings=settings, cache_enabled=False
            )
        )

        # Inside the block. These assert about files, and leaving the context
        # deletes the directory holding them.
        assert result.materialized is not None
        materialized = result.materialized
        assert materialized.document.is_file()
        assert materialized.document.suffix == ".md"
        assert result.verification.passed
        assert materialized.report.is_file()
        assert (materialized.directory / "run.json").is_file()

        # Genuine model output, so assert the guarantee rather than the wording:
        # every identifier the report cites is one the fixture CV contains.
        inventory = parse_cv(CASSETTE_CV)
        known = {entry.id for entry in inventory.entries}
        prefixes = {entry.id.split("-", 1)[0] for entry in inventory.entries}
        report = materialized.report.read_text(encoding="utf-8")
        cited = {
            found
            for found in re.findall(r"\b[a-z]{1,3}-[0-9a-f]{4,}\b", report)
            if found.split("-", 1)[0] in prefixes
        }
        assert cited, "the provenance table cited nothing"
        assert cited <= known, f"cites identifiers absent from the CV: {sorted(cited - known)}"


@pytest.mark.recorded
def test_the_anthropic_replay_seam_builds_a_working_provider(tmp_path: Path) -> None:
    """The recorded suite ships only an OpenAI cassette, so this covers Anthropic.

    The exchange is canned rather than paid for, so it proves the wiring and
    nothing about model quality. The request is the one the Anthropic SDK builds
    for itself, which is what a cassette has to match on to replay at all.
    """
    import httpx
    from anthropic import Anthropic
    from pydantic import BaseModel

    from tailorcv.application.config import Effort, ModelChoice
    from tailorcv.infrastructure.providers.anthropic import AnthropicProvider
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
    recording = httpx.Client(
        transport=RecordingTransport(
            cassette,
            inner=httpx.MockTransport(
                lambda request: httpx.Response(
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
    import httpx

    from tests.support.cassette import RecordingTransport

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={header: "identifying-metadata", "content-type": "application/json"},
            json={"answer": "retained"},
        )

    cassette = Cassette(path=tmp_path / "metadata.json")
    with httpx.Client(
        transport=RecordingTransport(cassette, inner=httpx.MockTransport(upstream))
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
    import httpx

    from tests.support.cassette import RecordingTransport

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"x-api-key": "sk-ant-api03-should-never-persist"},
            json={"error": "invalid key sk-ant-api03-should-never-persist supplied"},
        )

    cassette = Cassette(path=tmp_path / "leaky.json")
    client = httpx.Client(
        transport=RecordingTransport(cassette, inner=httpx.MockTransport(upstream))
    )
    client.get("https://api.anthropic.com/v1/models")
    cassette.save()

    written = cassette.path.read_text(encoding="utf-8")
    assert "sk-ant-api03-should-never-persist" not in written
    assert "x-api-key" not in written.lower()
    assert "REDACTED" in written


def test_matching_is_by_content_not_call_order(tmp_path: Path) -> None:
    """Skipping a cached stage must not shift every later reply by one."""
    import httpx

    from tests.support.cassette import ReplayTransport

    cassette = Cassette(path=tmp_path / "two.json")
    for body in (b"first", b"second"):
        cassette.add(
            httpx.Request("POST", "https://api.anthropic.com/v1/messages", content=body),
            httpx.Response(200, json={"which": body.decode()}),
        )

    transport = ReplayTransport(cassette)
    # Ask for the second interaction first. Order-based matching would hand back
    # the first, which is exactly what happens when a run skips a cached stage.
    second = transport.handle_request(
        httpx.Request("POST", "https://api.anthropic.com/v1/messages", content=b"second")
    )
    assert second.json() == {"which": "second"}
    first = transport.handle_request(
        httpx.Request("POST", "https://api.anthropic.com/v1/messages", content=b"first")
    )
    assert first.json() == {"which": "first"}


def test_identical_requests_replay_their_answers_in_recorded_order(tmp_path: Path) -> None:
    """Repeated critic trials send one body; each must get its own recorded draw."""
    import httpx

    from tests.support.cassette import ReplayTransport

    cassette = Cassette(path=tmp_path / "trials.json")
    for draw in ("first draw", "second draw"):
        cassette.add(
            httpx.Request("POST", "https://api.openai.com/v1/responses", content=b"same"),
            httpx.Response(200, json={"draw": draw}),
        )

    transport = ReplayTransport(cassette)
    answers = [
        transport.handle_request(
            httpx.Request("POST", "https://api.openai.com/v1/responses", content=b"same")
        ).json()
        for _ in range(2)
    ]

    assert answers == [{"draw": "first draw"}, {"draw": "second draw"}]


def test_redaction_removes_key_shaped_values() -> None:
    assert "sk-ant-abcdefgh12345" not in redact("token sk-ant-abcdefgh12345 end")
    assert "REDACTED" in redact("token sk-ant-abcdefgh12345 end")


def test_an_unrecorded_request_fails_instead_of_going_live(tmp_path: Path) -> None:
    """An offline suite must stay offline even when a cassette is incomplete."""
    import httpx

    from tests.support.cassette import ReplayTransport

    transport = ReplayTransport(Cassette(path=tmp_path / "empty.json"))
    with pytest.raises(LookupError, match="Re-record"):
        transport.handle_request(httpx.Request("POST", "https://api.anthropic.com/v1/messages"))


@pytest.mark.recorded
def test_record_then_replay_survives_a_round_trip_through_the_real_sdk(tmp_path: Path) -> None:
    """The mechanism itself: record real bytes, replay them, let the SDK parse.

    A model listing is used because its wire format is small and its parsing is
    the SDK's own. If a cassette can carry that through httpx and out of the
    vendor's client as typed objects, it can carry a completion too.
    """
    import httpx
    from anthropic import Anthropic

    from tailorcv.infrastructure.providers.anthropic import AnthropicProvider
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

    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "sk-ant-secret-value"
        return httpx.Response(200, json=listing)

    cassette = Cassette(path=tmp_path / "models.json")
    recorder = httpx.Client(
        transport=RecordingTransport(cassette, inner=httpx.MockTransport(upstream))
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
    import httpx
    from anthropic import Anthropic

    from tailorcv.infrastructure.providers.anthropic import AnthropicProvider
    from tailorcv.shared.errors import AuthError
    from tests.support.cassette import RecordingTransport

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"type": "error", "error": {"type": "authentication_error", "message": "invalid"}},
        )

    cassette = Cassette(path=tmp_path / "rejected.json")
    client = httpx.Client(
        transport=RecordingTransport(cassette, inner=httpx.MockTransport(upstream))
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
    import importlib.util
    import inspect

    from tailorcv.infrastructure.providers.base import provider_for

    # Loaded from its path: `scripts/` is a directory of programs, and putting it
    # on `sys.path` leaves it there for every test that follows.
    source = REPO_ROOT / "scripts" / "record_cassette.py"
    spec = importlib.util.spec_from_file_location("record_cassette", source)
    assert spec is not None and spec.loader is not None
    recorder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recorder)

    real = inspect.signature(provider_for)
    stand_in = inspect.signature(recorder.recording_provider(Cassette(path=Path("x"))))
    assert list(stand_in.parameters) == list(real.parameters)
    for name, parameter in real.parameters.items():
        assert stand_in.parameters[name].default == parameter.default, name


def test_a_gzipped_response_replays_as_the_text_that_was_stored(tmp_path: Path) -> None:
    """A stored body is plain text, so replay must not claim it is gzipped.

    Recording reads the response, so httpx has already decompressed it. Keeping
    `content-encoding: gzip` beside it makes httpx fail to gunzip on replay,
    which the vendor SDK surfaces as a connection error.
    """
    import gzip

    import httpx

    from tests.support.cassette import RecordingTransport

    payload = b'{"id": "resp_1", "object": "response"}'

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip", "content-type": "application/json"},
            content=gzip.compress(payload),
        )

    cassette = Cassette(path=tmp_path / "gzipped.json")
    client = httpx.Client(
        transport=RecordingTransport(cassette, inner=httpx.MockTransport(upstream))
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
