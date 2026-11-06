"""The critic's scored axes and the check names verification reports under."""

from __future__ import annotations

from clearscout.domain import ClaimBoundary

# Every axis the critic scores from 1 to 5, by field name.
SCORED_AXES = (
    "alignment",
    "preservation",
    "specificity",
    "readability",
    "ats_clarity",
    "human_voice",
)

# The axes that judge how the draft reads rather than whether it is faithful.
QUALITY_AXES = tuple(axis for axis in SCORED_AXES if axis != "preservation")

# Boundaries that decide a merged doubt ahead of the count.
DECIDING_BOUNDARIES = (ClaimBoundary.ABSENT_FROM_RECORD, ClaimBoundary.FORMAL_TITLE_OR_TENURE)

# A shorter quote with fewer content words may be its own doubt about part of a line.
QUOTED_CLAIM_WORDS = 3

# The checks that fail a line for claiming what its citations do not support.
SUPPORT_CHECKS = frozenset(
    {
        "sources cited",
        "numbers traced",
        "duration scope",
        "letter provenance",
        "letter numbers",
        "claim ownership",
    }
)

# The prose a model wrote in each document the critic reads, the only place it may find a claim.
WRITTEN_PARTS = {
    "resume": "the summary, the items on each skills line, the scope paragraph under each "
    "role, the bullets, and the earlier-career sentence",
    "cover letter": "the body paragraphs",
}

# The redraft reads the critic's ids; a person reads them through ``worded``.
CRITIC_CHECK = "critic"
# The letter's critic finding, named apart from the resume's in the merged run report.
LETTER_CRITIC = "letter critic"
CRITIC_CHECKS = (CRITIC_CHECK, LETTER_CRITIC)
