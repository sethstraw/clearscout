"""Turn whatever the user pointed at into job description text.

A URL, a document, or an image arrives here, and every later stage sees only a
:class:`JDText`.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from tailorcv.domain.records import PostingReadIntent
from tailorcv.infrastructure.web_fetch.ingest.image import IMAGE_SUFFIXES, VisionReader, from_image
from tailorcv.infrastructure.web_fetch.ingest.pdf import from_pdf
from tailorcv.infrastructure.web_fetch.ingest.text import JDText, require_enough_text
from tailorcv.infrastructure.web_fetch.ingest.url import from_url
from tailorcv.shared.errors import IngestError
from tailorcv.shared.layout import plain_layout


def looks_like_url(argument: str) -> bool:
    """Return whether the argument should be treated as a web address."""
    parsed = urlparse(argument)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def ingest(
    argument: str,
    vision: VisionReader | None = None,
    *,
    intent: PostingReadIntent = PostingReadIntent.CRAWL,
) -> JDText:
    """Detect the input type, dispatch to its reader, and return job description text.

    ``vision`` reads a document with no extractable text. ``intent`` defaults to
    crawl; a caller reading one URL a person typed passes
    ``PostingReadIntent.SINGLE_POSTING``.
    """
    if looks_like_url(argument):
        return from_url(argument, intent=intent)

    path = Path(argument).expanduser()
    if not path.is_file():
        raise IngestError(
            f"{argument} is neither a http(s) URL nor a file that exists. "
            "Pass a posting URL, a saved PDF, or a screenshot."
        )
    if path.suffix.lower() == ".pdf":
        return from_pdf(path)
    if path.suffix.lower() in IMAGE_SUFFIXES:
        return from_image(path, vision=vision)
    if path.suffix.lower() in {".txt", ".md"}:
        text = plain_layout(path.read_text(encoding="utf-8"))
        require_enough_text(text, str(path))
        return JDText(text=text, origin=str(path), method="plain text")
    raise IngestError(
        f"Do not know how to read {path.suffix or 'a file with no extension'}. "
        "Supported inputs are a URL, .pdf, .png, .jpg, .txt, and .md."
    )
