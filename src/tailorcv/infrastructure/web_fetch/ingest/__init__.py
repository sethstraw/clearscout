"""Read job postings into text: from a URL, a PDF, an image, or a publisher's pages.

``inputs.ingest`` dispatches one user-supplied argument; every later stage sees
only a :class:`JDText`.
"""

from tailorcv.infrastructure.web_fetch.ingest.image import IMAGE_SUFFIXES, VisionReader
from tailorcv.infrastructure.web_fetch.ingest.text import (
    MAXIMUM_USEFUL_CHARACTERS,
    MINIMUM_USEFUL_CHARACTERS,
    JDText,
    require_enough_text,
    strip_injected_instructions,
    strip_tags,
)

__all__ = [
    "IMAGE_SUFFIXES",
    "MAXIMUM_USEFUL_CHARACTERS",
    "MINIMUM_USEFUL_CHARACTERS",
    "JDText",
    "VisionReader",
    "require_enough_text",
    "strip_injected_instructions",
    "strip_tags",
]
