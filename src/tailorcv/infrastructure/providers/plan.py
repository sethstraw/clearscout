"""What the application asks a model for, said once and rendered per vendor.

A plan names the parts of a request rather than a position in someone's API:
the stage's instructions, the record the model reads, and the one dynamic ask.
Each adapter turns those into its own native shape, and the run manifest
identifies a call by their digests rather than by their text.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from tailorcv.application.config import Stage
from tailorcv.application.prompts import kernel
from tailorcv.shared.errors import ConfigError


def digest_of(text: str) -> str:
    """Return the content digest a manifest identifies one component by."""
    return hashlib.blake2s(text.encode("utf-8"), digest_size=16).hexdigest()


def schema_digest(document: dict[str, Any]) -> str:
    """Return the digest of the schema document sent on the wire, not of the Pydantic model."""
    return digest_of(json.dumps(document, sort_keys=True, separators=(",", ":")))


@dataclass(frozen=True)
class Image:
    """One image sent to a vision-capable model."""

    data: bytes
    media_type: str

    def base64(self) -> str:
        """Return the image as a base64 string, which is what both SDKs want."""
        return base64.standard_b64encode(self.data).decode("ascii")

    def data_url(self) -> str:
        """Return the image as a data URL, which is what the OpenAI SDK wants."""
        return f"data:{self.media_type};base64,{self.base64()}"


class SearchDepth(StrEnum):
    """How much retrieved page content a search puts in front of the model."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class SearchLocation:
    """The approximate place a search is answered from."""

    country: str = ""
    city: str = ""
    region: str = ""
    timezone: str = ""

    def stated(self) -> dict[str, str]:
        """Return the fields that were given, which is what a vendor wants."""
        named = {
            "country": self.country,
            "city": self.city,
            "region": self.region,
            "timezone": self.timezone,
        }
        return {key: value for key, value in named.items() if value}


@dataclass(frozen=True)
class SearchPolicy:
    """What one provider-hosted search is allowed to do.

    Each adapter renders what its vendor supports: ``depth`` and ``live`` reach
    OpenAI only.
    """

    max_calls: int = 5
    allowed_domains: tuple[str, ...] = ()
    blocked_domains: tuple[str, ...] = ()
    location: SearchLocation | None = None
    depth: SearchDepth = SearchDepth.MEDIUM
    live: bool = True
    capture_sources: bool = True

    def __post_init__(self) -> None:
        """Refuse a policy one vendor would reject at request time."""
        if self.allowed_domains and self.blocked_domains:
            raise ConfigError(
                "A search policy names allowed domains or blocked domains, not both. "
                "Anthropic answers a request carrying both with a 400."
            )
        if self.max_calls < 1:
            raise ConfigError("A search policy that permits no search should be absent instead.")

    def stated(self) -> dict[str, Any]:
        """Return the policy as the run manifest records it."""
        return {
            "max_calls": self.max_calls,
            "allowed_domains": list(self.allowed_domains),
            "blocked_domains": list(self.blocked_domains),
            "location": {} if self.location is None else self.location.stated(),
            "depth": self.depth.value,
            "live": self.live,
        }


@dataclass(frozen=True)
class Segment:
    """One piece of a request, named by the template that produced it.

    ``prompt`` is empty for text the code assembles rather than loads, which is
    the difference between a wording change somebody can make in the prompt
    editor and one that needs a release.
    """

    text: str
    prompt: str = ""

    @property
    def digest(self) -> str:
        """Return this segment's content digest."""
        return digest_of(self.text)


@dataclass(frozen=True)
class RequestPlan:
    """One request to a model, in the application's vocabulary.

    ``evidence`` is separate from ``instructions`` because it is the large
    block every drafting stage sends unchanged, and no model may read it as
    instruction. ``kernel`` is derived rather than passed, so no caller can
    build a request without it. ``draws`` counts identical requests the stage
    sends; above one, adapters cache the task as well.
    """

    stage: Stage
    instructions: Segment
    task: Segment
    # Adapters always end the cached prefix after evidence: a later task sending the same
    # evidence reads it back, and a single-batch task pays one cache write for that.
    evidence: Segment | None = None
    images: tuple[Image, ...] = ()
    search: SearchPolicy | None = None
    max_tokens: int = 8192
    draws: int = 1

    @property
    def tool_names(self) -> tuple[str, ...]:
        """Return the provider-hosted tools this request offers."""
        return () if self.search is None else ("web_search",)

    @property
    def kernel(self) -> Segment:
        """Return the invariants this request carries whatever its prompts say."""
        return Segment(kernel())

    @property
    def cached_prefix(self) -> str:
        """Return everything an adapter may render ahead of a cache breakpoint."""
        parts = [self.kernel.text, self.instructions.text]
        if self.evidence is not None:
            parts.append(self.evidence.text)
        return "\n\n".join(parts)

    @property
    def prompts(self) -> tuple[str, ...]:
        """Return the prompt templates this request was rendered from, in order."""
        named = (self.evidence, self.instructions, self.task)
        return tuple(part.prompt for part in named if part is not None and part.prompt)

    def instruction_text(self) -> str:
        """Return everything the model is told to read as instruction."""
        return f"{self.kernel.text}\n\n{self.instructions.text}"

    def payload_digest(self) -> str:
        """Return a digest of everything in this request that is not text.

        The caching layer keys on the text segments, which are fixed templates
        for the vision path. Without this, every screenshot hashes the same.
        """
        if not self.images:
            return ""
        digest = hashlib.blake2s(digest_size=16)
        for image in self.images:
            digest.update(image.media_type.encode("ascii"))
            digest.update(image.data)
        return digest.hexdigest()
