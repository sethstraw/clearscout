"""Compare the rule reading with the model's extraction."""

from __future__ import annotations

import re

from clearscout.application.tailoring.fit.facts.seniority import level_in
from clearscout.application.tailoring.fit.lexicon import (
    ARRANGEMENT_EVIDENCE,
    DEGREES,
    NOT_STATED,
    SAME_REQUIREMENT,
    YEARS,
    years_figure,
)
from clearscout.application.tailoring.fit.structure import (
    Disagreement,
    Reading,
    RequirementLine,
    Stance,
    quote_line,
)
from clearscout.application.tailoring.stages.jobspec import Importance, JobSpec
from clearscout.application.tailoring.stages.spans import locate
from clearscout.domain.taxonomy import says
from clearscout.shared.text import sanitize


def _words(text: str) -> frozenset[str]:
    """Return a phrase's comparable words, ignoring punctuation and case."""
    return frozenset(re.findall(r"[a-z0-9+]+", text.casefold()))


def _same_requirement(left: str, right: str) -> float:
    """Return how much of the shorter phrase two requirement texts share."""
    words_left, words_right = _words(left), _words(right)
    if not words_left or not words_right:
        return 0.0
    return len(words_left & words_right) / min(len(words_left), len(words_right))


def _seniority_disagreement(reading: Reading, spec: JobSpec) -> tuple[Disagreement, ...]:
    """Compare the level the model reported with what the posting's own words state.

    A refusal counts here only when it was a refusal for cause: declining because
    the posting names no level says nothing about the model's answer.
    """
    if not spec.seniority or level_in(spec.seniority) is None:
        return ()
    if not reading.seniority.stated and not reading.seniority.conflict:
        return ()
    model = level_in(spec.seniority)
    if model is not None and model[0] == reading.seniority.value:
        return ()
    return (
        Disagreement(
            subject="Seniority",
            model_says=spec.seniority,
            rules_say=reading.seniority.render(),
            detail=reading.seniority.because(),
            answer="seniority",
        ),
    )


def _location_disagreement(reading: Reading, spec: JobSpec) -> tuple[Disagreement, ...]:
    """Compare the working arrangement, which is the part of a location that gates."""
    if not spec.location or not (reading.location.stated or reading.location.conflict):
        return ()
    named = [name for name, phrases in ARRANGEMENT_EVIDENCE.items() if says(spec.location, phrases)]
    if not named or reading.location.value in named:
        return ()
    return (
        Disagreement(
            subject="Where the work happens",
            model_says=spec.location,
            rules_say=reading.location.render(),
            detail=reading.location.because(),
            answer="location",
        ),
    )


def _years_disagreement(reading: Reading, spec: JobSpec) -> tuple[Disagreement, ...]:
    """Compare the experience floor the model gated on with the one the text states."""
    if reading.years.value is None:
        return ()
    for gate in spec.hard_gates:
        match = YEARS.search(gate.text)
        if match is None:
            continue
        stated = years_figure(match)
        if stated == reading.years.value:
            return ()
        return (
            Disagreement(
                subject="Years of experience required",
                model_says=f"{stated}, from {quote_line(gate.text)!r}",
                rules_say=str(reading.years.value),
                detail=reading.years.because(),
                answer="years",
            ),
        )
    return ()


def _education_disagreement(reading: Reading, spec: JobSpec) -> tuple[Disagreement, ...]:
    """Compare a degree the model gated on with the degree language in the text."""
    gated = [
        (position, gate)
        for position, gate in enumerate(spec.hard_gates)
        if gate.kind == "degree" or any(says(gate.text, phrases) for _, phrases in DEGREES)
    ]
    if gated and not reading.education.stated:
        position, first = gated[0]
        return (
            Disagreement(
                subject="Education required",
                model_says=quote_line(first.text),
                rules_say=NOT_STATED,
                detail=reading.education.note,
                gate=position,
            ),
        )
    if not gated and reading.education.stated:
        return (
            Disagreement(
                subject="Education required",
                model_says="no degree gate was extracted",
                rules_say=str(reading.education.value),
                detail=reading.education.because(),
                answer="education",
            ),
        )
    return ()


def _importance_disagreements(reading: Reading, spec: JobSpec) -> tuple[Disagreement, ...]:
    """Compare, requirement by requirement, must-have against the posting's own wording.

    A requirement the model calls essential, sitting in the posting under "Nice
    to have", changes which gaps a person treats as disqualifying.
    """
    found: list[Disagreement] = []
    for requirement in spec.requirements:
        if requirement.importance is Importance.RESPONSIBILITY:
            continue
        line = (
            _quoted(reading.requirements, requirement.quote_or_span)
            if requirement.verified
            else _closest(reading.requirements, requirement.text)
        )
        model = (
            Stance.MUST_HAVE if requirement.importance is Importance.MUST_HAVE else Stance.PREFERRED
        )
        if line is None or line.stance is model:
            continue
        found.append(
            Disagreement(
                subject=f"Importance of {requirement.id}",
                model_says=model.value,
                rules_say=line.stance.value,
                detail=f"{line.reason}: {line.text}",
                evidence=(line.whole,),
            )
        )
    return tuple(found)


def _closest(lines: tuple[RequirementLine, ...], text: str) -> RequirementLine | None:
    """Return the best unambiguous line at or above the shared overlap threshold."""
    best: list[RequirementLine] = []
    best_overlap = SAME_REQUIREMENT
    for line in lines:
        overlap = _same_requirement(line.text, text)
        if overlap < best_overlap:
            continue
        if overlap > best_overlap:
            best, best_overlap = [line], overlap
        else:
            best.append(line)
    return _unambiguous(best)


def _quoted(lines: tuple[RequirementLine, ...], quote: str) -> RequirementLine | None:
    """Return the line holding a verified quote, or None when no line or lines that disagree do."""
    # Reader lines are sanitized text and a verified quote is raw posting text.
    clean = sanitize(quote)
    return _unambiguous([line for line in lines if locate(clean, line.whole).verified])


def _unambiguous(lines: list[RequirementLine]) -> RequirementLine | None:
    """Return the first line when every line shares its stance, else None."""
    if not lines or any(line.stance is not lines[0].stance for line in lines[1:]):
        return None
    return lines[0]


def disagreements(reading: Reading, spec: JobSpec) -> tuple[Disagreement, ...]:
    """Return every place a reading and the model's extraction do not match.

    Only where both of them said something: a rule with nothing to say about
    a field the model filled in is not evidence that the model was wrong.
    """
    return (
        *_seniority_disagreement(reading, spec),
        *_location_disagreement(reading, spec),
        *_years_disagreement(reading, spec),
        *_education_disagreement(reading, spec),
        *_importance_disagreements(reading, spec),
    )
