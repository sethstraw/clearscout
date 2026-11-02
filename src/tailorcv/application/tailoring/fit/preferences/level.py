"""Level readings against the saved seniority."""

from __future__ import annotations

from tailorcv.application.tailoring.fit.structure import (
    Answer,
    unasked_answer,
)
from tailorcv.domain.search_parameters import (
    JobSearchParameters,
)


def level_fit(seniority: Answer[str], wanted: JobSearchParameters) -> Answer[str]:
    """Say whether the level the posting states is inside the band being read for."""
    question = "Level you are reading for"
    if not wanted.seniority.stated:
        return unasked_answer(question)
    if seniority.value is None:
        return Answer(
            question,
            note=f"the posting states no level to compare ({seniority.because() or 'no evidence'})",
            conflict=seniority.conflict,
        )
    covered = wanted.seniority.covers(seniority.value)
    if covered is None:
        return Answer(question, note=f"{seniority.value!r} is not a rung on the ladder")
    low = wanted.seniority.minimum or "any"
    high = wanted.seniority.maximum or "any"
    verdict = "inside" if covered else "outside"
    return Answer(question, f"{verdict} your {low} to {high} band", evidence=seniority.evidence)
