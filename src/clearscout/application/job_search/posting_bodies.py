"""Posting source decoding, sanitization, and content-type policy."""

from __future__ import annotations

import base64
import binascii

from clearscout.application.contracts import PostingBodyInput
from clearscout.application.failures import ValidationError
from clearscout.infrastructure.web_fetch.ingest import strip_injected_instructions

# PostingObservation.extraction_method is a String(80) column.
_EXTRACTION_METHOD_LIMIT = 80
_INSTRUCTIONS_REMOVED = "; injected instructions removed"


def posting_source_bytes(value: PostingBodyInput) -> bytes:
    """Decode one strict posting input without accepting ambiguous encodings."""
    if value.encoding == "utf-8":
        return value.content.encode("utf-8")
    try:
        return base64.b64decode(value.content, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValidationError("Posting body content is not valid base64.") from exc


def posting_body_text(value: PostingBodyInput, source: bytes) -> str:
    """Return the text used for evidence spans, without any injected instruction."""
    return strip_injected_instructions(_extracted_text(value, source))


def posting_extraction_method(value: PostingBodyInput) -> str:
    """Return the recorded extraction method, stating when an instruction was removed."""
    extracted = _extracted_text(value, posting_source_bytes(value))
    if strip_injected_instructions(extracted) == extracted:
        return value.extraction_method
    room = _EXTRACTION_METHOD_LIMIT - len(_INSTRUCTIONS_REMOVED)
    return f"{value.extraction_method[:room]}{_INSTRUCTIONS_REMOVED}"


def _extracted_text(value: PostingBodyInput, source: bytes) -> str:
    if value.extracted_text is not None:
        return value.extracted_text
    try:
        return source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("Text and HTML posting bodies must be valid UTF-8.") from exc


def posting_mime_type(value: PostingBodyInput) -> str:
    """Validate or supply the MIME type implied by the logical artifact type."""
    expected = {
        "posting.body.text": "text/plain",
        "posting.body.html": "text/html",
        "posting.source.pdf": "application/pdf",
    }
    if value.logical_type == "posting.source.image":
        if not value.mime_type.startswith("image/"):
            raise ValidationError("Image posting sources require an image/* MIME type.")
        return value.mime_type
    required = expected[value.logical_type]
    if value.mime_type and value.mime_type.split(";", 1)[0].strip().lower() != required:
        raise ValidationError(f"{value.logical_type} requires MIME type {required}.")
    return required
