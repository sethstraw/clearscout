"""What an image costs the vendor that will read it, measured before it is sent.

An image past a limit its vendor documents as an error is refused here. One
whose format this cannot measure is reported as unmeasured.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from decimal import Decimal
from math import ceil, floor, sqrt
from typing import NamedTuple

from clearscout.application.config import ImageDetail, ProviderName
from clearscout.infrastructure.providers.plan import Image
from clearscout.shared.errors import ProviderError

# Anthropic bills a 28-pixel visual token; OpenAI a 32-pixel patch.
ANTHROPIC_PATCH = 28
OPENAI_PATCH = 32

# Claude 4.7 and later downscale an image to this token budget before reading it.
ANTHROPIC_MAX_TOKENS = 4784

# Anthropic answers an image past either of these with an invalid_request_error.
ANTHROPIC_MAX_EDGE = 8000
ANTHROPIC_MAX_BASE64_BYTES = 10 * 1024 * 1024

# OpenAI rejects, rather than resizes, an image still over this after sizing.
OPENAI_MAX_PATCHES = 30_000


class _Sizing(NamedTuple):
    """The edge an image is fitted within, and the patch budget it is shrunk to."""

    edge: int
    budget: int | None = None


_GPT_5_6_SIZING = {
    ImageDetail.LOW: _Sizing(512),
    ImageDetail.HIGH: _Sizing(2048, 2500),
    ImageDetail.ORIGINAL: _Sizing(65_535),
    ImageDetail.AUTO: _Sizing(65_535),
}

# From https://developers.openai.com/api/docs/guides/images-vision; a model
# missing here has no published sizing rule, so its ceiling is not reported.
_OPENAI_SIZING: dict[str, tuple[Decimal, dict[ImageDetail, _Sizing]]] = {
    "gpt-6-astra": (
        Decimal("1.2"),
        {
            ImageDetail.LOW: _Sizing(512),
            ImageDetail.HIGH: _Sizing(65_535, 2500),
            ImageDetail.ORIGINAL: _Sizing(65_535),
            ImageDetail.AUTO: _Sizing(65_535),
        },
    ),
    "gpt-5.6-sol": (Decimal("1.2"), _GPT_5_6_SIZING),
    "gpt-5.6-terra": (Decimal("1.2"), _GPT_5_6_SIZING),
    "gpt-5.6-luna": (Decimal("1.2"), _GPT_5_6_SIZING),
}

_JPEG_FRAME = frozenset(set(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC})
_JPEG_STANDALONE = frozenset({0x01, *range(0xD0, 0xDA)})


def dimensions(data: bytes) -> tuple[int, int] | None:
    """Return an image's pixel width and height, or nothing when unreadable."""
    for reader in (_png, _gif, _jpeg, _webp):
        size = reader(data)
        if size is not None:
            return size
    return None


def most_image_tokens(
    provider: ProviderName, model: str, detail: ImageDetail | None, images: Sequence[Image]
) -> int | None:
    """Return the most input tokens ``images`` can cost, or nothing when unstated.

    Nothing is returned when an image cannot be measured, or when the vendor
    publishes no sizing rule for the model and detail level it is read under.
    """
    total = 0
    for image in images:
        size = dimensions(image.data)
        if size is None:
            return None
        one = _one_image(provider, model, detail, *size)
        if one is None:
            return None
        total += one
    return total


def refuse_unanswerable(
    provider: ProviderName, model: str, detail: ImageDetail | None, images: Sequence[Image]
) -> None:
    """Raise when an image exceeds a limit the vendor documents as an error."""
    if provider is ProviderName.OPENAI:
        _refuse_openai(model, detail, images)
        return
    for image in images:
        encoded = (len(image.data) + 2) // 3 * 4
        if encoded > ANTHROPIC_MAX_BASE64_BYTES:
            raise ProviderError(
                f"An image encodes to {encoded // (1024 * 1024)} MB, and Anthropic "
                f"refuses one over {ANTHROPIC_MAX_BASE64_BYTES // (1024 * 1024)} MB.\n"
                "  Save it at a smaller size and import it again."
            )
        size = dimensions(image.data)
        if size is not None and max(size) > ANTHROPIC_MAX_EDGE:
            raise ProviderError(
                f"An image is {size[0]}x{size[1]} pixels, and Anthropic refuses an "
                f"edge over {ANTHROPIC_MAX_EDGE}.\n"
                "  Save it at a smaller size and import it again."
            )


def _refuse_openai(model: str, detail: ImageDetail | None, images: Sequence[Image]) -> None:
    """Raise when an image needs more patches than OpenAI accepts under ``model``."""
    if model not in _OPENAI_SIZING:
        return
    sizing = _OPENAI_SIZING[model][1][detail or ImageDetail.AUTO]
    for image in images:
        size = dimensions(image.data)
        if size is None:
            continue
        patches = _openai_patches(*size, sizing)
        if patches > OPENAI_MAX_PATCHES:
            raise ProviderError(
                f"An image is {size[0]}x{size[1]} pixels, or {patches} patches for {model}, "
                f"and OpenAI refuses one over {OPENAI_MAX_PATCHES} patches.\n"
                "  Save it at a smaller size and import it again."
            )


def _one_image(
    provider: ProviderName, model: str, detail: ImageDetail | None, width: int, height: int
) -> int | None:
    """Return one image's documented token ceiling for the vendor reading it."""
    if provider is ProviderName.ANTHROPIC:
        whole = _patches(width, height, ANTHROPIC_PATCH)
        return min(whole, ANTHROPIC_MAX_TOKENS)
    if model not in _OPENAI_SIZING:
        return None
    multiplier, sizing = _OPENAI_SIZING[model]
    # OpenAI reads a request that names no detail level as auto.
    patches = _openai_patches(width, height, sizing[detail or ImageDetail.AUTO])
    return ceil(patches * multiplier)


def _openai_patches(width: int, height: int, sizing: _Sizing) -> int:
    """Return OpenAI's patch count after fitting and budget-shrinking the image."""
    scale = min(1.0, sizing.edge / width, sizing.edge / height)
    if scale < 1.0:
        width, height = max(1, round(width * scale)), max(1, round(height * scale))
    patches = _patches(width, height, OPENAI_PATCH)
    if sizing.budget is not None and patches > sizing.budget:
        shrink = sqrt(OPENAI_PATCH**2 * sizing.budget / (width * height))
        across, down = width * shrink / OPENAI_PATCH, height * shrink / OPENAI_PATCH
        shrink *= min(floor(across) / across, floor(down) / down)
        patches = _patches(floor(width * shrink), floor(height * shrink), OPENAI_PATCH)
    return patches


def _patches(width: int, height: int, size: int) -> int:
    """Return how many patches of ``size`` pixels cover the image."""
    return ceil(width / size) * ceil(height / size)


def _png(data: bytes) -> tuple[int, int] | None:
    """Return the size in a PNG IHDR chunk."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) < 24:
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _gif(data: bytes) -> tuple[int, int] | None:
    """Return the size in a GIF logical screen descriptor."""
    if data[:6] not in {b"GIF87a", b"GIF89a"} or len(data) < 10:
        return None
    width, height = struct.unpack("<HH", data[6:10])
    return width, height


def _jpeg(data: bytes) -> tuple[int, int] | None:
    """Return the size in the first JPEG start-of-frame segment."""
    if not data.startswith(b"\xff\xd8"):
        return None
    index = 2
    while index + 3 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in _JPEG_STANDALONE:
            index += 2
            continue
        length = struct.unpack(">H", data[index + 2 : index + 4])[0]
        if marker in _JPEG_FRAME:
            if index + 9 > len(data):
                return None
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return width, height
        index += 2 + length
    return None


def _webp(data: bytes) -> tuple[int, int] | None:
    """Return the size in a WebP lossy, lossless, or extended chunk."""
    if not data.startswith(b"RIFF") or data[8:12] != b"WEBP" or len(data) < 30:
        return None
    if data[12:16] == b"VP8 ":
        width, height = struct.unpack("<HH", data[26:30])
        return width & 0x3FFF, height & 0x3FFF
    if data[12:16] == b"VP8L":
        bits = struct.unpack("<I", data[21:25])[0]
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if data[12:16] == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    return None
