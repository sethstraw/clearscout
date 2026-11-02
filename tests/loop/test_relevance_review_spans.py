"""Relevance review resolves application-owned spans to exact source bytes."""

from tailorcv.application.discovery.relevance_review_spans import posting_spans


def test_posting_spans_preserve_exact_repeated_whitespace_and_long_source_text() -> None:
    body = "First  sentence.\n\nSecond\tparagraph " + ("word  " * 180) + "finish."

    spans = posting_spans("pending-synthetic", body)

    assert len(spans) >= 3
    assert all(span.text in body for span in spans)
    assert all(body[span.char_start : span.char_end] == span.text for span in spans)
    assert len({span.id for span in spans}) == len(spans)


def test_posting_spans_accept_trailing_whitespace_without_citing_it() -> None:
    """Publisher formatting after the last word cannot erase every citation span."""
    for separator, suffix in ((" \n \n", " "), ("\r\n\t\r\n", "\n")):
        body = f"First paragraph.{separator}Second paragraph.{suffix}"

        spans = posting_spans("pending-synthetic", body)

        assert [span.text for span in spans] == ["First paragraph.", "Second paragraph."]
        assert all(body[span.char_start : span.char_end] == span.text for span in spans)
