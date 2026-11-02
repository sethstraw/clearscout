"""Capture keeps an approved run's provider traffic without changing the calls."""

from __future__ import annotations

import base64
import gzip
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from tailorcv.application.config import Stage
from tailorcv.infrastructure.providers.base import CallRecord
from tailorcv.infrastructure.providers.capture import (
    ENV_VAR,
    CapturingProvider,
    WireRecorder,
    capture_root,
)
from tailorcv.infrastructure.providers.plan import RequestPlan, Segment
from tailorcv.shared.costs import Usage


class _Answer(BaseModel):
    verdict: str


class _Chunks(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    def __iter__(self) -> Iterator[bytes]:
        yield from self._chunks


class _Streaming(httpx.BaseTransport):
    """Answer with a body delivered in separate chunks under the given headers."""

    def __init__(self, chunks: list[bytes], headers: dict[str, str]) -> None:
        self._chunks = chunks
        self._headers = headers

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=self._headers, stream=_Chunks(self._chunks))


def _sse() -> _Streaming:
    return _Streaming([b"data: one\n\n", b"data: two\n\n"], {"content-type": "text/event-stream"})


def _captured(root: Path) -> dict[str, Any]:
    record: dict[str, Any] = json.loads((root / "wire-0001.json").read_text(encoding="utf-8"))
    return record


def _plan() -> RequestPlan:
    return RequestPlan(
        stage=Stage.MATCH,
        instructions=Segment(text="Weigh the evidence."),
        task=Segment(text="Is this posting worth pursuing?"),
    )


def test_capture_is_off_until_the_directory_is_named(monkeypatch: Any) -> None:
    monkeypatch.delenv(ENV_VAR, raising=False)

    assert capture_root() is None


def test_a_streamed_answer_reaches_the_caller_before_it_is_written(
    tmp_path: Path,
) -> None:
    """The recorder tees the body; reading it first would end streaming."""
    transport = WireRecorder(tmp_path, inner=_sse())
    written = tmp_path / "wire-0001.json"

    with (
        httpx.Client(transport=transport) as client,
        client.stream("POST", "https://provider.test/v1/responses", json={"ask": "x"}) as reply,
    ):
        chunks = reply.iter_bytes()
        first = next(chunks)
        # A recorder that read the body would have written it all before
        # the caller saw anything.
        assert not written.exists()
        seen = [first, *chunks]

    assert seen == [b"data: one\n\n", b"data: two\n\n"]
    record = json.loads(written.read_text(encoding="utf-8"))
    assert record["response"]["body"] == "data: one\n\ndata: two\n\n"


def test_a_gzip_answer_is_captured_as_the_json_the_provider_sent(tmp_path: Path) -> None:
    body = gzip.compress(b'{"verdict": "pursue"}')
    inner = _Streaming([body], {"content-type": "application/json", "content-encoding": "gzip"})

    with httpx.Client(transport=WireRecorder(tmp_path, inner=inner)) as client:
        reply = client.post("https://provider.test/v1/responses", json={"ask": "x"})

    assert reply.json() == {"verdict": "pursue"}
    assert _captured(tmp_path)["response"]["body"] == {"verdict": "pursue"}


def test_a_gzip_stream_is_captured_as_readable_events(tmp_path: Path) -> None:
    """The events arrive as two compressed chunks, so the capture must rejoin them."""
    body = gzip.compress(b"data: one\n\ndata: two\n\n")
    half = len(body) // 2
    inner = _Streaming(
        [body[:half], body[half:]],
        {"content-type": "text/event-stream", "content-encoding": "gzip"},
    )

    with (
        httpx.Client(transport=WireRecorder(tmp_path, inner=inner)) as client,
        client.stream("POST", "https://provider.test/v1/responses", json={"ask": "x"}) as reply,
    ):
        seen = b"".join(reply.iter_bytes())

    assert seen == b"data: one\n\ndata: two\n\n"
    assert _captured(tmp_path)["response"]["body"] == "data: one\n\ndata: two\n\n"


def test_an_answer_in_an_unsupported_encoding_is_captured_losslessly(tmp_path: Path) -> None:
    body = b"\x00\x8f\x1b compressed by something we cannot undo"
    inner = _Streaming([body], {"content-type": "application/json", "content-encoding": "compress"})

    with httpx.Client(transport=WireRecorder(tmp_path, inner=inner)) as client:
        client.post("https://provider.test/v1/responses", json={"ask": "x"})

    kept = _captured(tmp_path)["response"]["body"]
    assert kept["capture"] == "undecoded"
    assert kept["content_encoding"] == "compress"
    assert base64.b64decode(kept["base64"]) == body
    assert "\\ufffd" not in (tmp_path / "wire-0001.json").read_text(encoding="utf-8")


def test_an_answer_that_is_not_text_is_captured_losslessly(tmp_path: Path) -> None:
    body = b"\xff\xd8\xff\xe0 not utf-8 at all"
    inner = _Streaming([body], {"content-type": "application/octet-stream"})

    with httpx.Client(transport=WireRecorder(tmp_path, inner=inner)) as client:
        client.post("https://provider.test/v1/responses", json={"ask": "x"})

    kept = _captured(tmp_path)["response"]["body"]
    assert kept["capture"] == "undecoded"
    assert base64.b64decode(kept["base64"]) == body
    assert "\\ufffd" not in (tmp_path / "wire-0001.json").read_text(encoding="utf-8")


def test_a_captured_request_body_stays_the_json_that_was_sent(tmp_path: Path) -> None:
    """The request is never content-encoded, so it must not take the lossless path."""
    with httpx.Client(transport=WireRecorder(tmp_path, inner=_sse())) as client:
        client.post(
            "https://provider.test/v1/responses",
            json={"ask": "x"},
            headers={"content-encoding": "gzip"},
        )

    assert _captured(tmp_path)["request"]["body"] == {"ask": "x"}


def test_a_captured_request_keeps_no_credential(tmp_path: Path) -> None:
    transport = WireRecorder(tmp_path, inner=_sse())

    with httpx.Client(transport=transport) as client:
        client.post(
            "https://provider.test/v1/responses",
            json={"ask": "x"},
            headers={"authorization": "Bearer sk-live-secret"},
        )

    written = (tmp_path / "wire-0001.json").read_text(encoding="utf-8")
    assert "sk-live-secret" not in written
    assert "authorization" not in written.lower()


class _Adapter:
    """Stand in for one vendor adapter at the seam capture wraps."""

    name = "openai"

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error

    def complete(self, plan: RequestPlan, schema: type, choice: Any) -> tuple[Any, CallRecord]:
        if self._error is not None:
            raise self._error
        return schema(verdict="pursue"), CallRecord(usage=Usage(), model="gpt-test")


def test_a_completed_call_records_what_was_sent_and_what_came_back(tmp_path: Path) -> None:
    provider = CapturingProvider(_Adapter(), tmp_path)

    answer, _record = provider.complete(_plan(), _Answer, None)

    assert answer.verdict == "pursue"
    record = json.loads((tmp_path / "call-0001.json").read_text(encoding="utf-8"))
    assert record["asked"]["stage"] == "match"
    assert record["asked"]["instructions"] == "Weigh the evidence."
    assert record["asked"]["task"] == "Is this posting worth pursuing?"
    assert record["answer"] == {"verdict": "pursue"}
    assert record["record"]["model"] == "gpt-test"


def test_a_later_run_keeps_what_an_earlier_run_captured(tmp_path: Path) -> None:
    """Each run builds its own provider, and the capture is the only evidence a paid run leaves."""
    first = CapturingProvider(_Adapter(), tmp_path)
    first.complete(_plan(), _Answer, None)

    second = CapturingProvider(_Adapter(), tmp_path)
    second.complete(_plan(), _Answer, None)

    assert sorted(path.name for path in tmp_path.glob("call-*.json")) == [
        "call-0001.json",
        "call-0002.json",
    ]


def test_a_refused_call_is_recorded_and_still_raises(tmp_path: Path) -> None:
    provider = CapturingProvider(_Adapter(RuntimeError("the provider refused")), tmp_path)

    with pytest.raises(RuntimeError):
        provider.complete(_plan(), _Answer, None)

    record = json.loads((tmp_path / "call-0001.json").read_text(encoding="utf-8"))
    assert record["refused"] == "RuntimeError"
    assert record["detail"] == "the provider refused"
