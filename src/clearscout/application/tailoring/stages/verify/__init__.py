"""Check the derivative, cheapest and most certain checks first.

Everything a regular expression can decide runs before any model is asked, and
the critic runs last, only on a draft that has already passed them. Checks read
the Markdown clearscout produced; page count is a renderer's question.
"""

from .citations import id_texts, line_citations, worded
from .critic import (
    critic_finding,
    critic_system_prompt,
    critique,
    doubted_span,
    misquoted_evidence,
)
from .letter import check_letter
from .lexicon import FUNCTION_WORDS, NUMBER, WORD, bare_word
from .report import KeywordCoverage, Report, keyword_coverage
from .resume import check, check_supplied
from .rubric import CRITIC_CHECK, LETTER_CRITIC, SCORED_AXES, SUPPORT_CHECKS
from .verdicts import CriticVerdict, DoubtedClaim

__all__ = [
    "CRITIC_CHECK",
    "FUNCTION_WORDS",
    "LETTER_CRITIC",
    "NUMBER",
    "SCORED_AXES",
    "SUPPORT_CHECKS",
    "WORD",
    "CriticVerdict",
    "DoubtedClaim",
    "KeywordCoverage",
    "Report",
    "bare_word",
    "check",
    "check_letter",
    "check_supplied",
    "critic_finding",
    "critic_system_prompt",
    "critique",
    "doubted_span",
    "id_texts",
    "keyword_coverage",
    "line_citations",
    "misquoted_evidence",
    "worded",
]
