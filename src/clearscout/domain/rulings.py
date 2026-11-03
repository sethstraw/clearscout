"""What a workspace can rule about a judgment the analysis made for it.

A ruling is a workspace's judgment on a specific requirement identified by its
durable ``requirement_key``. Rulings persist across re-evaluations, and drop or
mark the requirement in the gaps view accordingly. A relevance dispute is the
same kind of judgment over one discovery's relevance verdict.
"""

from __future__ import annotations

from enum import StrEnum


class Ruling(StrEnum):
    """Vocabulary the workspace can record against one requirement.

    ``met``, ``not_applicable`` and the machine-set ``reversed`` drop it from the
    gaps panel; the two disputes challenge the fit or the extraction until a paid
    re-run reverses them.
    """

    MET = "met"
    NOT_APPLICABLE = "not_applicable"
    DISPUTED_FIT = "disputed_fit"
    DISPUTED_EXTRACTION = "disputed_extraction"
    REVERSED = "reversed"


class RelevanceDispute(StrEnum):
    """Vocabulary for one relevance verdict.

    ``set_aside`` returns the discovery to triage beside the disputed verdict.
    """

    SET_ASIDE = "set_aside"


# Rulings that remove a requirement from the gaps view entirely.
HIDING_RULINGS = frozenset({Ruling.MET, Ruling.NOT_APPLICABLE, Ruling.REVERSED})
