"""Record real HTTP exchanges once, then replay them offline forever.

Recording at the transport layer means a cassette replays through the vendor's
own SDK, against bytes a real server actually sent. A cassette is readable JSON,
matched on method, URL, and a hash of the request body rather than on call
order, so a run that skips a cached stage still replays. Nothing secret is
written: authentication headers are dropped and key-shaped values are redacted.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2

SECRET_HEADERS = frozenset({"authorization", "x-api-key", "openai-organization", "cookie"})
IDENTIFYING_HEADERS = frozenset({"openai-project", "x-request-id", "request-id", "cf-ray", "date"})

# Headers that describe the wire form of a body this cassette does not store.
# Recording reads the response, so what lands in the file is already decompressed
# and `content-encoding` and `content-length` would both describe other bytes.
TRANSPORT_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})
KEY_SHAPED = re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,})")
REDACTED = "REDACTED"


def body_digest(content: bytes) -> str:
    """Return a hash of the body, which keeps a candidate's CV out of a committed file.

    A JSON object is hashed with its top-level keys sorted, so the SDK's field order does
    not matter while nested order, such as a schema's property order, still does.
    """
    try:
        parsed = json.loads(content)
    except ValueError:
        return hashlib.sha256(content).hexdigest()[:32]
    if isinstance(parsed, dict):
        parsed = dict(sorted(parsed.items()))
    canonical = json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def redact(text: str) -> str:
    """Remove anything key-shaped from text that will be written to disk."""
    return KEY_SHAPED.sub(REDACTED, text)


@dataclass(frozen=True)
class Interaction:
    """One request and the response it received."""

    method: str
    url: str
    request_digest: str
    status: int
    headers: dict[str, str]
    body: str

    @property
    def key(self) -> tuple[str, str, str]:
        """Return what this interaction is matched on."""
        return (self.method, self.url, self.request_digest)

    def response(self) -> httpx2.Response:
        """Rebuild the response for the SDK to parse as though it were live.

        Transport headers are dropped here as well as on the way in, so a
        cassette file that still carries them replays.
        """
        headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower() not in TRANSPORT_HEADERS
        }
        return httpx2.Response(self.status, headers=headers, content=self.body.encode())

    def to_json(self) -> dict[str, Any]:
        """Return the JSON form written to a cassette file."""
        return {
            "method": self.method,
            "url": self.url,
            "request_digest": self.request_digest,
            "status": self.status,
            "headers": self.headers,
            "body": self.body,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Interaction:
        """Rebuild an interaction from its cassette entry."""
        headers: Mapping[str, Any] = data.get("headers") or {}
        return cls(
            method=str(data["method"]),
            url=str(data["url"]),
            request_digest=str(data["request_digest"]),
            status=int(data["status"]),
            headers={str(name): str(value) for name, value in headers.items()},
            body=str(data["body"]),
        )


@dataclass
class Cassette:
    """A file of recorded interactions."""

    path: Path
    interactions: list[Interaction] = field(default_factory=list)
    replayed: set[int] = field(default_factory=set, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @classmethod
    def load(cls, path: Path) -> Cassette:
        """Read a cassette, or return an empty one when the file is absent."""
        if not path.is_file():
            return cls(path=path)
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(path=path, interactions=[Interaction.from_json(item) for item in data])

    def save(self) -> None:
        """Write the cassette, pretty enough for a human to review before committing."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps([item.to_json() for item in self.interactions], indent=2)
        self.path.write_text(redact(payload) + "\n", encoding="utf-8")

    def add(self, request: httpx2.Request, response: httpx2.Response) -> None:
        """Record one exchange, with secrets stripped."""
        self.interactions.append(
            Interaction(
                method=request.method,
                url=str(request.url),
                request_digest=body_digest(request.content),
                status=response.status_code,
                headers=safe_headers(response.headers),
                body=response.text,
            )
        )

    def find(self, request: httpx2.Request) -> Interaction | None:
        """Return the next unreplayed interaction matching ``request``, if any remains.

        Identical requests, such as repeated critic trials, replay their recorded
        answers in the order they were recorded rather than the first one each time.
        """
        wanted = (request.method, str(request.url), body_digest(request.content))
        # Concurrent critic trials look up one body at once and must not claim the same answer.
        with self._lock:
            for index, item in enumerate(self.interactions):
                if item.key == wanted and index not in self.replayed:
                    self.replayed.add(index)
                    return item
        return None

    @property
    def empty(self) -> bool:
        """Return whether there is anything to replay."""
        return not self.interactions


def safe_headers(headers: httpx2.Headers) -> dict[str, str]:
    """Remove credentials, account and request identifiers, and transport framing."""
    return {
        name: redact(value)
        for name, value in headers.items()
        if name.lower() not in SECRET_HEADERS
        and name.lower() not in IDENTIFYING_HEADERS
        and name.lower() not in TRANSPORT_HEADERS
        and not name.lower().startswith("set-cookie")
    }


class RecordingTransport(httpx2.BaseTransport):
    """Pass requests through to the real API and keep a copy of the answers."""

    def __init__(self, cassette: Cassette, inner: httpx2.BaseTransport | None = None) -> None:
        self._cassette = cassette
        self._inner = inner or httpx2.HTTPTransport()

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        """Forward the request, record the response, and return it unchanged."""
        response = self._inner.handle_request(request)
        response.read()
        self._cassette.add(request, response)
        return response


class ReplayTransport(httpx2.BaseTransport):
    """Answer requests from a cassette, and refuse to reach the network.

    An unmatched request raises rather than falling through. Silently going
    live would turn an offline test suite into one that fails on an aeroplane
    and bills someone in CI.
    """

    def __init__(self, cassette: Cassette) -> None:
        self._cassette = cassette
        self.unmatched: list[str] = []

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        """Return the recorded response, or fail loudly."""
        found = self._cassette.find(request)
        if found is None:
            description = f"{request.method} {request.url}"
            self.unmatched.append(description)
            raise LookupError(
                f"No recorded response for {description}. "
                f"Re-record the cassette at {self._cassette.path}."
            )
        return found.response()


def recording_client(cassette: Cassette) -> httpx2.Client:
    """Return an httpx client that records everything it does."""
    return httpx2.Client(transport=RecordingTransport(cassette), timeout=120.0)


def replaying_client(cassette: Cassette) -> httpx2.Client:
    """Return an httpx client that replays a cassette and never goes out."""
    return httpx2.Client(transport=ReplayTransport(cassette), timeout=1.0)
