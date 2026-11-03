"""Deterministic fit reading over posting text and CV evidence."""

from clearscout.application.tailoring.fit.facts import disagreements, resident_countries
from clearscout.application.tailoring.fit.gates import (
    Gate,
    Standing,
    excluded_condition,
    gates,
    refusal,
    unanswered,
)
from clearscout.application.tailoring.fit.lexicon import NOT_STATED
from clearscout.application.tailoring.fit.reader import read, residence_requirement
from clearscout.application.tailoring.fit.structure import Answer, Disagreement, Reading, Stance

__all__ = [
    "NOT_STATED",
    "Answer",
    "Disagreement",
    "Gate",
    "Reading",
    "Stance",
    "Standing",
    "disagreements",
    "excluded_condition",
    "gates",
    "read",
    "refusal",
    "residence_requirement",
    "resident_countries",
    "unanswered",
]
