"""A stand-in API server that answers like a real provider, over real HTTP.

It exists so the end-to-end replay test does not depend on somebody having paid
for a cassette. The content is canned, so it proves nothing about model quality.
Payload functions may inspect the request schema for anti-fabrication tests, or
ignore it for an independent response oracle.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

Payload = Callable[[dict[str, Any]], dict[str, Any]]


def _enum(node: dict[str, Any], resolve: Callable[[Any], Any]) -> list[Any]:
    """Return the enum values on a node, following one level of reference."""
    if "enum" in node:
        return list(node["enum"])
    for branch in ("items", "anyOf", "allOf"):
        value = node.get(branch)
        if isinstance(value, dict):
            return _enum(resolve(value), resolve)
        if isinstance(value, list):
            for item in value:
                values = _enum(resolve(item), resolve)
                if values:
                    return values
    return []


def _frames(frames: list[dict[str, Any]]) -> str:
    """Render one server-sent event per frame."""
    return "".join(f"event: {frame['type']}\ndata: {json.dumps(frame)}\n\n" for frame in frames)


def anthropic_events(message: dict[str, Any]) -> str:
    """Render a finished Anthropic message as the event stream its SDK reads."""
    text = message["content"][0]["text"]
    return _frames(
        [
            {
                "type": "message_start",
                "message": {**message, "content": [], "stop_reason": None},
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": message["stop_reason"], "stop_sequence": None},
                "usage": {"output_tokens": message["usage"]["output_tokens"]},
            },
            {"type": "message_stop"},
        ]
    )


def openai_events(response: dict[str, Any]) -> str:
    """Render a finished Responses object as the event stream its SDK reads."""
    return _frames(
        [
            {"type": "response.created", "sequence_number": 0, "response": response},
            {"type": "response.completed", "sequence_number": 1, "response": response},
        ]
    )


class FakeProviderAPI:
    """Answer requests the way one vendor does with a selected canned payload."""

    def __init__(self, vendor: str, payloads: dict[str, Payload]) -> None:
        self.vendor = vendor
        self.payloads = payloads
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Serve one request, or fail loudly about which schema was unhandled."""
        body = json.loads(request.content or b"{}")
        self.requests.append(body)
        schema = self._schema(body)
        name = str(schema.get("title") or schema.get("name") or "")
        key = next((candidate for candidate in self.payloads if candidate in name), None)
        if key is None:
            raise AssertionError(f"No canned payload for schema {name!r}")
        content = json.dumps(self.payloads[key](schema))
        envelope = self._envelope(content, body.get("service_tier"))
        if body.get("stream"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=self._events(envelope),
            )
        return httpx.Response(200, json=envelope)

    def _events(self, response: dict[str, Any]) -> str:
        """Render the finished response as the event stream the SDK reads."""
        if self.vendor == "anthropic":
            return anthropic_events(response)
        return openai_events(response)

    def _schema(self, body: dict[str, Any]) -> dict[str, Any]:
        """Pull the response schema out of whichever field this vendor uses."""
        if self.vendor == "anthropic":
            fmt = body.get("output_config", {}).get("format", {})
        else:
            fmt = body.get("text", {}).get("format", {})
        schema = fmt.get("schema", {})
        return {**schema, "title": schema.get("title") or fmt.get("name") or ""}

    def _envelope(self, content: str, service_tier: object = None) -> dict[str, Any]:
        """Wrap the content in the vendor's own response shape.

        A tier is reported back the way a provider names the one that served,
        which is what a discounted call is priced from.
        """
        if self.vendor == "anthropic":
            return {
                "id": "msg_fake",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-5",
                "content": [{"type": "text", "text": content}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 1200,
                    "output_tokens": 300,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
            }
        response: dict[str, Any] = {
            "id": "resp_fake",
            "object": "response",
            "created_at": 0,
            "status": "completed",
            "model": "gpt-6.1-sol",
            "output": [
                {
                    "type": "message",
                    "id": "msg_fake",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": content, "annotations": []}],
                }
            ],
            "parallel_tool_calls": False,
            "tool_choice": "auto",
            "tools": [],
            "usage": {
                "input_tokens": 1200,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 300,
                "output_tokens_details": {"reasoning_tokens": 120},
                "total_tokens": 1500,
            },
        }
        if service_tier is not None:
            response["service_tier"] = service_tier
        return response
