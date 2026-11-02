"""What an image is measured to cost before a request carrying it is sent."""

from __future__ import annotations

import struct

import pytest

from tailorcv.application.config import ImageDetail, ProviderName
from tailorcv.infrastructure.providers.images import (
    dimensions,
    most_image_tokens,
    refuse_unanswerable,
)
from tailorcv.infrastructure.providers.plan import Image
from tailorcv.shared.errors import ProviderError

CLAUDE = "claude-sonnet-5"


def png(width: int, height: int) -> bytes:
    """Return a PNG signature and IHDR chunk stating one size."""
    header = struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + header + b"\x00\x00\x00\x00"


def jpeg(width: int, height: int) -> bytes:
    """Return a JPEG carrying an APP0 segment and then a baseline frame."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    frame = b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", height, width)
    return b"\xff\xd8" + app0 + frame + b"\x01" * 6


def gif(width: int, height: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00"


def webp_lossy(width: int, height: int) -> bytes:
    payload = b"\x00" * 3 + b"\x9d\x01\x2a" + struct.pack("<HH", width, height)
    return b"RIFF" + b"\x00" * 4 + b"WEBP" + b"VP8 " + b"\x00" * 4 + payload


def webp_lossless(width: int, height: int) -> bytes:
    bits = (width - 1) | ((height - 1) << 14)
    payload = b"\x2f" + struct.pack("<I", bits)
    return b"RIFF" + b"\x00" * 4 + b"WEBP" + b"VP8L" + b"\x00" * 4 + payload + b"\x00" * 5


def webp_extended(width: int, height: int) -> bytes:
    canvas = (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    return b"RIFF" + b"\x00" * 4 + b"WEBP" + b"VP8X" + b"\x00" * 4 + b"\x00" * 4 + canvas


@pytest.mark.parametrize("build", [png, jpeg, gif, webp_lossy, webp_lossless, webp_extended])
def test_a_size_is_read_from_every_format_the_vendors_accept(build) -> None:
    assert dimensions(build(1920, 1080)) == (1920, 1080)


def test_a_format_that_cannot_be_measured_is_reported_rather_than_guessed() -> None:
    assert dimensions(b"II*\x00nonsense") is None
    assert (
        most_image_tokens(
            ProviderName.ANTHROPIC, CLAUDE, None, [Image(data=b"II*\x00", media_type="image/tiff")]
        )
        is None
    )


# Every row is a size and visual-token count Anthropic publishes for the
# high-resolution tier, which is the tier Claude Sonnet 5 reads under.
@pytest.mark.parametrize(
    ("width", "height", "tokens"),
    [
        (200, 200, 64),
        (1000, 1000, 1296),
        (1092, 1092, 1521),
        (1920, 1080, 2691),
        (2000, 1500, 3888),
        (3840, 2160, 4784),
    ],
)
def test_anthropic_is_measured_at_the_published_visual_token_counts(
    width: int, height: int, tokens: int
) -> None:
    image = Image(data=png(width, height), media_type="image/png")

    assert most_image_tokens(ProviderName.ANTHROPIC, CLAUDE, None, [image]) == tokens


def test_anthropic_never_reports_more_than_the_tier_it_downscales_to() -> None:
    huge = Image(data=png(7000, 7000), media_type="image/png")

    assert most_image_tokens(ProviderName.ANTHROPIC, CLAUDE, None, [huge]) == 4784


# OpenAI's worked examples for gpt-6-astra at high detail.
@pytest.mark.parametrize(
    ("width", "height", "tokens"), [(1024, 1024, 1229), (2048, 2048, 3000), (4096, 512, 2458)]
)
def test_openai_matches_the_published_examples_under_a_patch_budget(
    width: int, height: int, tokens: int
) -> None:
    image = Image(data=png(width, height), media_type="image/png")

    tokens_read = most_image_tokens(ProviderName.OPENAI, "gpt-6-astra", ImageDetail.HIGH, [image])

    assert tokens_read == tokens


# 4000x3000 fits to 2048x1536 (3072 patches), then shrinks to 1824x1368 (2451).
@pytest.mark.parametrize(
    ("detail", "width", "height", "tokens"),
    [
        (ImageDetail.LOW, 1024, 768, 231),
        (ImageDetail.HIGH, 4000, 3000, 2942),
        (ImageDetail.ORIGINAL, 1800, 2400, 5130),
        (None, 1800, 2400, 5130),
    ],
)
def test_openai_fits_and_budgets_the_image_by_the_detail_it_is_read_under(
    detail: ImageDetail | None, width: int, height: int, tokens: int
) -> None:
    image = Image(data=png(width, height), media_type="image/png")

    assert most_image_tokens(ProviderName.OPENAI, "gpt-5.6-terra", detail, [image]) == tokens


@pytest.mark.parametrize("detail", [ImageDetail.LOW, ImageDetail.HIGH, ImageDetail.ORIGINAL])
def test_openai_reports_nothing_for_a_model_without_a_published_sizing_rule(
    detail: ImageDetail,
) -> None:
    image = Image(data=png(1024, 1024), media_type="image/png")

    assert most_image_tokens(ProviderName.OPENAI, "gpt-6-sol", detail, [image]) is None


def test_the_images_in_one_request_are_counted_together() -> None:
    pages = [Image(data=png(1000, 1000), media_type="image/png") for _ in range(3)]

    assert most_image_tokens(ProviderName.ANTHROPIC, CLAUDE, None, pages) == 1296 * 3


def test_an_edge_anthropic_refuses_is_refused_before_the_request_is_paid_for() -> None:
    image = Image(data=png(9000, 1000), media_type="image/png")

    with pytest.raises(ProviderError, match="8000"):
        refuse_unanswerable(ProviderName.ANTHROPIC, CLAUDE, None, [image])


def test_an_image_over_the_anthropic_encoded_size_is_refused() -> None:
    image = Image(data=b"\x00" * (8 * 1024 * 1024), media_type="image/png")

    with pytest.raises(ProviderError, match="MB"):
        refuse_unanswerable(ProviderName.ANTHROPIC, CLAUDE, None, [image])


def test_openai_is_not_held_to_anthropic_limits() -> None:
    image = Image(data=png(9000, 1000), media_type="image/png")

    refuse_unanswerable(ProviderName.OPENAI, "gpt-6-astra", ImageDetail.ORIGINAL, [image])


# 6400x4801 needs 200x151 = 30,200 patches; OpenAI rejects over 30,000.
@pytest.mark.parametrize("detail", [ImageDetail.ORIGINAL, ImageDetail.AUTO, None])
def test_an_image_over_the_openai_patch_limit_is_refused_before_it_is_sent(
    detail: ImageDetail | None,
) -> None:
    image = Image(data=png(6400, 4801), media_type="image/png")

    with pytest.raises(ProviderError, match="30000 patches"):
        refuse_unanswerable(ProviderName.OPENAI, "gpt-6-astra", detail, [image])


def test_an_image_at_the_openai_patch_limit_is_sent() -> None:
    image = Image(data=png(6400, 4800), media_type="image/png")

    refuse_unanswerable(ProviderName.OPENAI, "gpt-6-astra", ImageDetail.ORIGINAL, [image])


def test_a_detail_level_with_a_patch_budget_is_not_refused_for_a_large_image() -> None:
    image = Image(data=png(6400, 4801), media_type="image/png")

    refuse_unanswerable(ProviderName.OPENAI, "gpt-6-astra", ImageDetail.HIGH, [image])


def test_a_model_without_a_published_patch_rule_is_not_refused() -> None:
    image = Image(data=png(6400, 4801), media_type="image/png")

    refuse_unanswerable(ProviderName.OPENAI, "gpt-6-sol", ImageDetail.ORIGINAL, [image])
