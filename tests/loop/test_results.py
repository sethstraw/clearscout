"""The shared result vocabulary."""

from __future__ import annotations

import pytest

from clearscout.shared.results import Finding, Outcome, table_cell, worst


def test_a_run_fails_on_a_failure_and_survives_a_warning() -> None:
    """What decides a run's exit code, and the reason WARN exists at all."""
    passing = Finding("a", Outcome.PASS)
    warning = Finding("b", Outcome.WARN)
    failing = Finding("c", Outcome.FAIL)

    assert not worst(()).failed
    assert not worst((passing, warning)).failed
    assert worst((passing, warning)) is Outcome.WARN
    assert worst((passing, warning, failing)).failed


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        # A pipe or a newline would break the table, and these arrive from a
        # posting or a CV this code did not write.
        ("  spaced\ntext | cell  ", None, "spaced text \\| cell"),
        ("a | b | c", None, "a \\| b \\| c"),
        ("line\r\nbreak", None, "line break"),
        ("abcdef", 5, "ab..."),
        ("abc", 5, "abc"),
        # A limit too small for the "..." marker cuts hard and never exceeds it.
        ("abcdef", 2, "ab"),
        ("abcdef", 0, ""),
        # A truncated cell ends at a word boundary, never mid-word.
        ("alpha beta gamma", 12, "alpha..."),
        ("alpha beta gamma", 14, "alpha beta..."),
    ],
)
def test_a_table_cell_is_safe_to_write_into_a_table(
    text: str, limit: int | None, expected: str
) -> None:
    rendered = table_cell(text) if limit is None else table_cell(text, limit)
    assert rendered == expected
    assert "\n" not in rendered and "\r" not in rendered
    assert "|" not in rendered.replace("\\|", "")
