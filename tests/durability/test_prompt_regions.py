"""Regions of a prompt an override may not change, and how they are enforced."""

from __future__ import annotations

import pytest

from clearscout.application.prompts import (
    INVARIANT_CLOSE,
    INVARIANT_OPEN,
    PROMPT_NAMES,
    check_invariants,
    check_template,
    invariant_regions,
    load,
    overriding,
    packaged,
    render,
    without_markers,
)
from clearscout.shared.errors import ConfigError

# Prompts stating a rule from the invariant list, so a release that drops a
# region's markers is caught rather than quietly making the region editable.
FIXED = (
    "jobspec_system",
    "match_system",
    "match_user",
    "draft_system",
    "critic_system",
    "critic_user",
    "cover_letter_system",
    "cover_letter_user",
    "guided_campaign_system",
    "structure_system",
    "vision_system",
)

UNMARKED = (
    "guided_campaign_user",
    "search_vocabulary_user",
    "relevance_review_evidence",
    "relevance_review_user",
    "posting_recovery_user",
    "structure_user",
    "vision_user",
)


# The browser editor asserts these same three strings, because it decides what
# to protect and the server decides what to accept.
INLINE_MARKER = "Rule text <!--invariant-->\nBody.\n<!--/invariant-->"
JOINED_OPENER = "<!--invariant-->Body.\n<!--/invariant-->"
TWO_REGIONS = (
    "<!--invariant-->\nFirst.\n<!--/invariant-->\n"
    "Between.\n"
    "<!--invariant-->\nSecond.\n<!--/invariant-->"
)


def test_a_marker_that_does_not_start_its_own_line_is_ordinary_text() -> None:
    """The editor and the server must agree here, or a protected region fails to save."""
    assert invariant_regions(INLINE_MARKER) == ()
    assert invariant_regions(JOINED_OPENER) == ()


def test_every_marked_region_is_found_and_taken_verbatim() -> None:
    assert invariant_regions(TWO_REGIONS) == ("First.\n", "Second.\n")


@pytest.mark.parametrize("name", FIXED)
def test_a_prompt_stating_an_invariant_marks_it(name: str) -> None:
    """A rule nobody marked is a rule an override can delete."""
    assert invariant_regions(packaged(name))


@pytest.mark.parametrize("name", PROMPT_NAMES)
def test_no_marker_reaches_a_model(name: str) -> None:
    """The markers annotate the file. They are not something to tell a model."""
    sent = without_markers(packaged(name))

    assert INVARIANT_OPEN not in sent
    assert INVARIANT_CLOSE not in sent


@pytest.mark.parametrize("name", [name for name in PROMPT_NAMES if name not in UNMARKED])
def test_a_region_survives_stripping_word_for_word(name: str) -> None:
    """Stripping removes the marker lines and nothing of the text between them."""
    for region in invariant_regions(packaged(name)):
        assert region in without_markers(packaged(name))


def test_an_override_that_rewrites_a_fixed_region_is_refused() -> None:
    """The point of the mechanism: the rule cannot be edited out."""
    weakened = packaged("draft_system").replace(
        "Never introduce a skill, tool, standard, metric, date, employer,",
        "Introduce a skill, tool, standard, metric, date, employer,",
    )

    with pytest.raises(ConfigError) as caught:
        check_invariants("draft_system", weakened)

    assert "region 1 is not editable" in str(caught.value)


def test_an_override_that_deletes_the_markers_is_refused() -> None:
    """Removing the fence is how somebody would try to remove what it fences."""
    unmarked = without_markers(packaged("draft_system"))

    with pytest.raises(ConfigError) as caught:
        check_invariants("draft_system", unmarked)

    assert "fixes 2 region(s)" in str(caught.value)


def test_an_override_may_still_rewrite_everything_else() -> None:
    """A locked region is not a locked prompt: style and rubric stay editable."""
    edited = packaged("draft_system").replace(
        "Use no first-person pronouns.", "Write in whatever person reads best."
    )

    check_template("draft_system", edited)


def test_a_bound_override_is_re_checked_before_it_is_sent() -> None:
    """Saving is not the only way an override reaches a request."""
    weakened = packaged("critic_system").replace(
        "Be strict about authorship", "Be relaxed about authorship"
    )

    with overriding({"critic_system": weakened}):
        with pytest.raises(ConfigError):
            load("critic_system")
        with pytest.raises(ConfigError):
            render(
                "critic_system",
                distinctions="none",
                preservation_floor="4",
                quality_floor="3",
            )
