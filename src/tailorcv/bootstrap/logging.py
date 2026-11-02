"""Configure one reviewed structured event stream for a TailorCV process."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import IO, Any

_STANDARD_FIELDS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "asctime",
    "message",
}
_HANDLER_MARKER = "_tailorcv_structured_handler"
_LOGGER_NAME = "tailorcv"
_RESERVED_FIELDS = frozenset({"timestamp", "level", "logger", "event"})


def _json_value(value: Any) -> Any:
    """Accept only stable JSON values; event producers must project richer objects."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _json_value(item) for key, item in value.items()}
    raise TypeError(f"Unsupported structured log value: {type(value).__name__}")


class StructuredLogFormatter(logging.Formatter):
    """Render standard record metadata and structured extras as one JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialize one log record without discarding application-defined fields."""
        extras = {
            key: _json_value(value)
            for key, value in record.__dict__.items()
            if key not in _STANDARD_FIELDS
            and key not in _RESERVED_FIELDS
            and not key.startswith("_")
        }
        payload: dict[str, Any] = {
            **extras,
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def configure_application_logging(
    *, level: int = logging.INFO, stream: IO[str] | None = None
) -> None:
    """Own one TailorCV-only stream without enabling third-party or access logs."""
    application = logging.getLogger(_LOGGER_NAME)
    application.setLevel(level)
    application.propagate = False
    for handler in application.handlers:
        if getattr(handler, _HANDLER_MARKER, False):
            handler.setLevel(level)
            return
    application.handlers.clear()
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(StructuredLogFormatter())
    setattr(handler, _HANDLER_MARKER, True)
    application.addHandler(handler)
