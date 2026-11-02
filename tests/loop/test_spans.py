"""Placing a quoted span in the source that is said to contain it."""

from __future__ import annotations

from tailorcv.application.tailoring.stages.spans import locate, normalize
from tailorcv.domain import SpanStatus

POSTING = "Requirements:\n  - Must be in  Canada\n  - 5+ years of Python\n"


def test_a_quote_differing_only_in_spacing_and_case_is_found() -> None:
    """A model asked to copy a line reflows it, and that is not a different line."""
    span = locate("must be IN Canada", POSTING)

    assert span.status is SpanStatus.VERIFIED
    assert POSTING[span.start : span.end] == "Must be in  Canada"


def test_a_line_break_between_a_heading_and_its_colon_does_not_lose_the_quote() -> None:
    """A converted posting breaks after a bold heading, stating the same condition."""
    posting = (
        "**Office expectations**\n\nFor Remote Roles\n"
        ": If this role is remote, there will be in-office events.\n"
    )

    span = locate(
        "For Remote Roles: If this role is remote, there will be in-office events.",
        posting,
    )

    assert span.status is SpanStatus.VERIFIED
    assert posting[span.start : span.end] == (
        "For Remote Roles\n: If this role is remote, there will be in-office events."
    )


def test_a_quote_the_posting_does_not_contain_is_not_placed() -> None:
    """The whole point: wording nobody can find in the source is not a source quote."""
    span = locate("Must be in Ireland", POSTING)

    assert span.status is SpanStatus.NEEDS_REVIEW
    assert (span.start, span.end) == (-1, -1)


def test_an_empty_quote_is_not_placed() -> None:
    """An empty needle is in every haystack, which would verify nothing."""
    assert locate("", POSTING).status is SpanStatus.NEEDS_REVIEW
    assert locate("   \n  ", POSTING).status is SpanStatus.NEEDS_REVIEW


def test_punctuation_is_not_harmless_and_is_not_normalized() -> None:
    """Whitespace and case are noise; a changed word or mark changes the claim."""
    assert locate("must be in canada.", POSTING).status is SpanStatus.NEEDS_REVIEW
    assert locate("must be in the Canada", POSTING).status is SpanStatus.NEEDS_REVIEW


def test_case_folding_that_lengthens_a_character_keeps_the_offsets_usable() -> None:
    """Folding is many-to-one, so the offset map cannot be a running index."""
    source = "Strassen and STRAßEN"
    span = locate("strassen", source)

    assert source[span.start : span.end] == "Strassen"
    assert normalize("STRAßEN") == "strassen"
