"""Read the seniority level a posting asks for."""

from __future__ import annotations

import re

from clearscout.application.tailoring.fit.lexicon import (
    COUNTERPARTY_RANK,
    LEAD_AS_NOUN,
    LEVEL_TOLERANCE,
    LEVELS,
    ORG_CONTEXT,
    SUPERVISED,
)
from clearscout.application.tailoring.fit.structure import (
    Answer,
)
from clearscout.domain.taxonomy import says
from clearscout.shared.text import sentence_pieces


def level_in(text: str) -> tuple[str, int] | None:
    """Return the highest seniority level the text names, and the years it implies."""
    found = [
        (position, name, years)
        for position, (name, tokens, years) in enumerate(LEVELS)
        if says(text, tokens)
    ]
    if not found:
        return None
    _, name, years = max(found)
    return name, years


def read_seniority(title: str, body: str, years: Answer[int]) -> Answer[str]:
    """Return the level the posting states, or abstain and say what contradicted what.

    Nothing here turns years into a level: six years is a senior role at one
    employer and a mid one at the next.
    """
    found = level_in(title) or level_in(_level_sentences(body))
    if found is None:
        return Answer(
            "Seniority",
            note=(
                "neither the title nor the posting names a level, and a level is not "
                "inferred from an experience floor"
            ),
        )
    name, implied = found
    if supervised := says(body, SUPERVISED):
        return Answer(
            "Seniority",
            note=(
                f"the title says {name!r} and the posting says {supervised[0]!r}, "
                "which do not describe the same role"
            ),
            conflict=True,
        )
    if years.value is not None and implied - years.value > LEVEL_TOLERANCE:
        return Answer(
            "Seniority",
            note=(
                f"the title says {name!r} and the posting asks for {years.value} year(s) "
                "of experience, which do not agree"
            ),
            conflict=True,
        )
    return Answer("Seniority", name, evidence=(f"title says {name!r}",))


def _level_sentences(body: str) -> str:
    """Return only the sentences that state the level of the role itself.

    A sentence describing the org chart ("reports to the VP") and a counterparty
    ("VP-level stakeholders") name someone else's rank, and the bare verb "lead"
    is not a level, so all three are dropped before a level is read.
    """
    kept: list[str] = []
    for line in body.splitlines():
        for piece in sentence_pieces(line):
            sentence = ORG_CONTEXT.sub(" ", COUNTERPARTY_RANK.sub(" ", piece))
            folded = sentence.casefold()
            if not any(word in folded for word in ("level", "role", "position")):
                continue
            if "lead" in folded and LEAD_AS_NOUN.search(sentence) is None:
                kept.append(re.sub(r"\blead\b", " ", sentence, flags=re.IGNORECASE))
            else:
                kept.append(sentence)
    return "\n".join(kept)
