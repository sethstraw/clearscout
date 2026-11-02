"""The local runtime writes actionable structured application events."""

from __future__ import annotations

import io
import json
import logging

from tailorcv.bootstrap.logging import StructuredLogFormatter, configure_application_logging


def test_application_logging_is_structured_and_idempotent() -> None:
    application = logging.getLogger("tailorcv")
    original_handlers = list(application.handlers)
    original_level = application.level
    original_propagate = application.propagate
    stream = io.StringIO()
    try:
        application.handlers.clear()
        configure_application_logging(stream=stream)
        configure_application_logging(stream=stream)

        logging.getLogger("tailorcv.test").info(
            "guided_board_check",
            extra={
                "task_id": "task-example",
                "host": "jobs.example.test",
                "duration_ms": 250,
                "outcome": "crawl_continuation",
            },
        )

        lines = stream.getvalue().splitlines()
        assert len(lines) == 1
        payload = json.loads(lines[0])
        assert payload["level"] == "INFO"
        assert payload["logger"] == "tailorcv.test"
        assert payload["event"] == "guided_board_check"
        assert payload["task_id"] == "task-example"
        assert payload["host"] == "jobs.example.test"
        assert payload["duration_ms"] == 250
        assert payload["outcome"] == "crawl_continuation"
        assert payload["timestamp"].endswith("+00:00")
    finally:
        application.handlers[:] = original_handlers
        application.setLevel(original_level)
        application.propagate = original_propagate


def test_application_logging_excludes_other_namespaces_and_owns_one_output() -> None:
    application = logging.getLogger("tailorcv")
    root = logging.getLogger()
    original_application_handlers = list(application.handlers)
    original_application_level = application.level
    original_propagate = application.propagate
    original_root_handlers = list(root.handlers)
    original_root_level = root.level
    plain = io.StringIO()
    structured = io.StringIO()
    try:
        application.handlers.clear()
        root.handlers[:] = [logging.StreamHandler(plain)]
        root.setLevel(logging.INFO)
        configure_application_logging(stream=structured)

        logging.getLogger("tailorcv.worker").info("task_started", extra={"task_id": "task-1"})
        logging.getLogger("httpx").info("GET https://user:secret@example.test/?token=secret")

        assert len(structured.getvalue().splitlines()) == 1
        assert plain.getvalue() == "GET https://user:secret@example.test/?token=secret\n"
        assert "secret" not in structured.getvalue()
    finally:
        application.handlers[:] = original_application_handlers
        application.setLevel(original_application_level)
        application.propagate = original_propagate
        root.handlers[:] = original_root_handlers
        root.setLevel(original_root_level)


def test_application_logging_reserves_core_metadata() -> None:
    record = logging.LogRecord("tailorcv.test", logging.INFO, __file__, 1, "actual", (), None)
    record.event = "overridden"
    record.timestamp = "not-a-time"

    payload = json.loads(StructuredLogFormatter().format(record))

    assert payload["event"] == "actual"
    assert payload["timestamp"] != "not-a-time"
