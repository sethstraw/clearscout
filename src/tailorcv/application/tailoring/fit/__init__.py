"""Deterministic fit reading over posting text and CV evidence."""

from tailorcv.application.tailoring.fit.facts import disagreements, resident_countries
from tailorcv.application.tailoring.fit.gates import (
    Gate,
    Standing,
    excluded_condition,
    gates,
    refusal,
    unanswered,
)
from tailorcv.application.tailoring.fit.lexicon import NOT_STATED
from tailorcv.application.tailoring.fit.reader import read, residence_requirement
from tailorcv.application.tailoring.fit.structure import Answer, Disagreement, Reading, Stance

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
