"""Keep one run's provider traffic and what the application did with it.

Off unless CLEARSCOUT_CAPTURE_DIR names a directory; nothing in the application
reads what it writes. The transport tees the response body rather than reading
it, since both SDKs stream and a buffered read would defeat the idle timeout.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import zlib
from collections.abc import Callable, Iterator
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, cast

import httpx

from clearscout.infrastructure.providers.base import BackgroundDispatch, BackgroundWebSearchProvider
from clearscout.infrastructure.providers.plan import RequestPlan

ENV_VAR = "CLEARSCOUT_CAPTURE_DIR"

# Credentials and cookies never reach the capture directory.
SECRET_HEADERS = frozenset(
    {"authorization", "x-api-key", "api-key", "cookie", "set-cookie", "proxy-authorization"}
)


def capture_root() -> Path | None:
    """Return the directory an approved run captures into, or None when off."""
    stated = os.environ.get(ENV_VAR, "").strip()
    if not stated:
        return None
    root = Path(stated).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _headers(headers: httpx.Headers) -> dict[str, str]:
    return {name: value for name, value in headers.items() if name.lower() not in SECRET_HEADERS}


def _inflate(raw: bytes) -> bytes:
    try:
        return zlib.decompress(raw)
    except zlib.error:
        return zlib.decompress(raw, -zlib.MAX_WBITS)


_DECODERS: dict[str, Callable[[bytes], bytes]] = {
    "identity": lambda raw: raw,
    "gzip": gzip.decompress,
    "x-gzip": gzip.decompress,
    "deflate": _inflate,
}


def _decoded(raw: bytes, encoding: str) -> bytes:
    """Return raw with each content-coding the header names undone, outermost first."""
    for coding in reversed(encoding.split(",")):
        name = coding.strip().lower()
        if not name:
            continue
        decoder = _DECODERS.get(name)
        if decoder is None:
            raise ValueError(f"no stdlib decoder for content-encoding {name}")
        raw = decoder(raw)
    return raw


def _body(raw: bytes, encoding: str = "") -> Any:
    """Return the body as JSON or text, or as base64 when it does not decode."""
    if not raw:
        return ""
    try:
        text = _decoded(raw, encoding).decode("utf-8")
    except (OSError, EOFError, ValueError, zlib.error) as error:
        return {
            "capture": "undecoded",
            "content_encoding": encoding or "identity",
            "reason": f"{type(error).__name__}: {error}",
            "base64": base64.b64encode(raw).decode("ascii"),
        }
    try:
        return json.loads(text)
    except ValueError:
        return text


def _write(path: Path, record: dict[str, Any]) -> None:
    path.write_text(json.dumps(record, indent=2, sort_keys=True, default=repr) + "\n", "utf-8")


def _claim(root: Path, prefix: str) -> Path:
    """Return a file no earlier run wrote. Each run builds its own recorder."""
    taken = 0
    for existing in root.glob(f"{prefix}-*.json"):
        number = existing.stem.rsplit("-", 1)[-1]
        if number.isdigit():
            taken = max(taken, int(number))
    while True:
        taken += 1
        path = root / f"{prefix}-{taken:04d}.json"
        try:
            path.touch(exist_ok=False)
        except FileExistsError:
            continue
        return path


class _TeedStream(httpx.SyncByteStream):
    """Hand each chunk to the SDK and to the capture file at the same time."""

    def __init__(self, inner: httpx.SyncByteStream, on_close: Any) -> None:
        self._inner = inner
        self._on_close = on_close
        self._chunks: list[bytes] = []

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._inner:
            self._chunks.append(chunk)
            yield chunk

    def close(self) -> None:
        closer = getattr(self._inner, "close", None)
        if closer is not None:
            closer()
        self._on_close(b"".join(self._chunks))


class WireRecorder(httpx.BaseTransport):
    """Forward every request and keep both directions of the exchange."""

    def __init__(self, root: Path, inner: httpx.BaseTransport | None = None) -> None:
        """Write exchanges into root, sending them through inner."""
        self._root = root
        self._inner = inner or httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Return the response unchanged, with its body copied as it is consumed."""
        response = self._inner.handle_request(request)
        sent = {
            "method": request.method,
            "url": str(request.url),
            "headers": _headers(request.headers),
            "body": _body(request.content),
        }

        def finish(body: bytes) -> None:
            # No file exists until the body has been consumed. The tee collects chunks off
            # the transport, so the body is still carrying its content-encoding here.
            _write(
                _claim(self._root, "wire"),
                {
                    "request": sent,
                    "response": {
                        "status": response.status_code,
                        "headers": _headers(response.headers),
                        "body": _body(body, response.headers.get("content-encoding", "")),
                    },
                },
            )

        inner_stream = cast(httpx.SyncByteStream, response.stream)
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_TeedStream(inner_stream, finish),
            extensions=response.extensions,
            request=request,
        )


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    dumper = getattr(value, "model_dump", None)
    if dumper is not None:
        return dumper(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return repr(value)


class CapturingProvider:
    """Record what the application asked a provider for and what it did with the answer."""

    def __init__(self, inner: Any, root: Path) -> None:
        """Record inner's calls into root, exposing background methods only if inner has them."""
        self._inner = inner
        self._root = root
        self.name = inner.name
        if isinstance(inner, BackgroundWebSearchProvider):
            self.dispatch_background = self._dispatch_background
            self.poll_background = self._poll_background
            self.cancel_background = self._cancel_background

    def check_credentials(self) -> None:
        """Ask the adapter to verify its key, recording nothing."""
        self._inner.check_credentials()

    def available_models(self) -> frozenset[str] | None:
        """Return the adapter's model identifiers, recording nothing."""
        models: frozenset[str] | None = self._inner.available_models()
        return models

    def complete(self, plan: RequestPlan, schema: type, choice: Any) -> tuple[Any, Any]:
        """Return the adapter's answer, recording the exchange."""
        return self._record("complete", plan, schema, choice)

    def complete_with_web_search(
        self, plan: RequestPlan, schema: type, choice: Any
    ) -> tuple[Any, Any]:
        """Return the adapter's searched answer, recording the exchange."""
        return self._record("complete_with_web_search", plan, schema, choice)

    def _asked(
        self, operation: str, plan: RequestPlan, schema: type, choice: Any
    ) -> dict[str, Any]:
        return {
            "operation": operation,
            "provider": str(self.name),
            "stage": str(plan.stage),
            "schema": schema.__name__,
            "choice": _plain(choice),
            "instructions": plan.instructions.text,
            "task": plan.task.text,
            "evidence": None if plan.evidence is None else plan.evidence.text,
            "images": len(plan.images),
            "search": _plain(plan.search),
        }

    def _record(
        self, operation: str, plan: RequestPlan, schema: type, choice: Any
    ) -> tuple[Any, Any]:
        asked = self._asked(operation, plan, schema, choice)
        path = _claim(self._root, "call")
        try:
            answer, record = getattr(self._inner, operation)(plan, schema, choice)
        except Exception as error:
            _write(path, {"asked": asked, "refused": type(error).__name__, "detail": str(error)})
            raise
        _write(path, {"asked": asked, "answer": _plain(answer), "record": _plain(record)})
        return answer, record

    def _dispatch_background(
        self, plan: RequestPlan, schema: type, choice: Any, *, metadata: dict[str, str]
    ) -> BackgroundDispatch:
        """Record a background dispatch and the response id it returned."""
        asked = self._asked("dispatch_background", plan, schema, choice)
        asked["metadata"] = dict(metadata)
        path = _claim(self._root, "call")
        try:
            dispatched: BackgroundDispatch = self._inner.dispatch_background(
                plan, schema, choice, metadata=metadata
            )
        except Exception as error:
            _write(path, {"asked": asked, "refused": type(error).__name__, "detail": str(error)})
            raise
        _write(path, {"asked": asked, "response_id": dispatched.response_id})
        return dispatched

    def _poll_background(self, response_id: str, schema: type, ceiling: int) -> Any:
        """Record one terminal poll of a background response; pending polls are skipped."""
        asked = {
            "operation": "poll_background",
            "provider": str(self.name),
            "response_id": response_id,
            "schema": schema.__name__,
            "ceiling": ceiling,
        }
        path = _claim(self._root, "call")
        try:
            poll = self._inner.poll_background(response_id, schema, ceiling)
        except Exception as error:
            _write(path, {"asked": asked, "refused": type(error).__name__, "detail": str(error)})
            raise
        if poll.status == "pending":
            path.unlink(missing_ok=True)
            return poll
        _write(
            path,
            {
                "asked": asked,
                "status": poll.status,
                "value": _plain(poll.value),
                "record": _plain(poll.record),
                "error": poll.error,
            },
        )
        return poll

    def _cancel_background(self, response_id: str) -> None:
        """Record one cancellation request."""
        path = _claim(self._root, "call")
        asked = {
            "operation": "cancel_background",
            "provider": str(self.name),
            "response_id": response_id,
        }
        try:
            self._inner.cancel_background(response_id)
        except Exception as error:
            _write(path, {"asked": asked, "refused": type(error).__name__, "detail": str(error)})
            raise
        _write(path, {"asked": asked})
