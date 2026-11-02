"""Dispute blocks render into each prompt at the expected placeholder."""

from __future__ import annotations

from tailorcv.application.prompts import render


def test_draft_prompt_carries_active_disputes_block() -> None:
    """The dispute text lands under the active_disputes placeholder in the draft prompt."""
    body = render(
        "draft_user",
        role="Backend Engineer",
        company="Globex",
        keywords="routing",
        evidence="(evidence)",
        feedback="(no prior)",
        posting_hint="",
        active_disputes="- r-1: Five years of Python",
    )
    assert "Five years of Python" in body
    assert "DISPUTED" in body


def test_cover_letter_prompt_carries_active_disputes_block() -> None:
    """The dispute text lands in the cover-letter prompt."""
    body = render(
        "cover_letter_user",
        role="Backend Engineer",
        company="Globex",
        evidence="(evidence)",
        research="(research)",
        feedback="(no prior)",
        previous_letter="(no prior)",
        posting_hint="",
        active_disputes="- r-1: Five years of Python",
    )
    assert "Five years of Python" in body
    assert "DISPUTED" in body


def test_critic_prompt_carries_active_disputes_block() -> None:
    """The dispute text lands in the critic prompt."""
    body = render(
        "critic_user",
        role="Backend Engineer",
        requirements="(requirements)",
        asserted="(asserted)",
        document="resume",
        draft="(resume)",
        citations="(citations)",
        offered="(offered)",
        research="",
        written_parts="the bullets",
        posting_hint="",
        active_disputes="- r-1: Five years of Python",
    )
    assert "Five years of Python" in body
    assert "DISPUTED" in body


def test_jobspec_user_prompt_carries_disputed_rulings_block() -> None:
    """The disputed_rulings block reaches the jobspec extraction prompt."""
    body = render(
        "jobspec_user",
        jd_text="posting body here",
        stated_boundaries="",
        disputed_rulings="- r-1: Five years of Python",
    )
    assert "Five years of Python" in body
    assert "DISPUTED" in body


def test_dispute_blocks_are_empty_when_none_supplied() -> None:
    """Passing an empty string keeps the placeholder empty; no fabricated dispute."""
    body = render(
        "draft_user",
        role="Backend Engineer",
        company="Globex",
        keywords="routing",
        evidence="(evidence)",
        feedback="(no prior)",
        posting_hint="",
        active_disputes="",
    )
    assert "Five years of Python" not in body
