"""Read a posting from a screenshot, the input for LinkedIn and anything behind a login."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Protocol

from tailorcv.infrastructure.web_fetch.ingest.text import JDText, require_enough_text
from tailorcv.shared.errors import IngestError
from tailorcv.shared.layout import plain_layout

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})


class VisionReader(Protocol):
    """Reads posting text out of images.

    A protocol so ingest never imports the provider layer: the pipeline supplies
    a real reader and tests supply a function.
    """

    def __call__(self, images: list[bytes], media_type: str) -> str:
        """Return the posting text transcribed from ``images``."""
        ...


def media_type_of(path: Path) -> str:
    """Return the image media type of ``path``, refusing a file that is not one.

    Shared with the CV reader in ``sources``: an SDK sent a media type it cannot
    decode answers with a request error rather than anything a person can act on.
    """
    media_type, _ = mimetypes.guess_type(path.name)
    if media_type is None or not media_type.startswith("image/"):
        raise IngestError(f"{path} does not look like an image file.")
    return media_type


def from_image(path: Path, vision: VisionReader | None = None) -> JDText:
    """Return job description text extracted verbatim from an image."""
    if vision is None:
        raise IngestError(
            f"Reading {path.name} needs a vision-capable model and none was available."
        )
    result = plain_layout(vision([path.read_bytes()], media_type_of(path)))
    require_enough_text(result, str(path))
    return JDText(text=result, origin=str(path), method="vision (screenshot)")
