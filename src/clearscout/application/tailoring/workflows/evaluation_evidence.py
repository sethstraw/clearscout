"""Anchoring evaluation findings back to the exact text that supports them.

Every stored score points at readable bytes in the stored posting or the master
CV. The first supporting anchor carries the three whole-evaluation components,
and factors cite it by key when a posting yielded no requirements.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from clearscout.application.contracts import ScoreEvidenceInput
from clearscout.application.master_cv.cv import CvInventory
from clearscout.application.tailoring.fit import Disagreement, Reading
from clearscout.application.tailoring.stages.jobspec import Requirement
from clearscout.application.tailoring.stages.match import EvidenceMap, Match
from clearscout.application.tailoring.stages.spans import locate
from clearscout.domain.records import (
    EvidenceClassification,
    EvidenceDirection,
    EvidenceSourceKind,
    EvidenceStrength,
    ScoreComponent,
)
from clearscout.shared.text import sanitize_with_offsets

# The components an overall evaluation is scored on rather than one requirement.
LEADING_COMPONENTS = [
    ScoreComponent.FIT,
    ScoreComponent.EVIDENCE,
    ScoreComponent.OPPORTUNITY,
]

# How far into the posting a fallback anchor may quote when the exact text is
# not found in the stored body.
FALLBACK_CHARACTERS = 240

_READING_COMPONENTS = {
    "place_fit": ScoreComponent.ELIGIBILITY,
    "pay_fit": ScoreComponent.COMPENSATION,
}


class EvidenceAnchors:
    """Collect score evidence in the order it is anchored."""

    def __init__(self, posting_text: str, candidate_text: str) -> None:
        """Bind the two bodies every anchor is located within."""
        self.posting_text = posting_text
        self.candidate_text = candidate_text
        self.items: list[ScoreEvidenceInput] = []
        self.leading_claimed = False

    @property
    def empty(self) -> bool:
        """Report whether anything has been anchored yet."""
        return not self.items

    def leading(
        self, direction: EvidenceDirection = EvidenceDirection.SUPPORTS
    ) -> list[ScoreComponent]:
        """Return the whole-evaluation components, which only a supporting anchor may carry."""
        if self.leading_claimed or direction is not EvidenceDirection.SUPPORTS:
            return []
        self.leading_claimed = True
        return list(LEADING_COMPONENTS)

    def posting(
        self,
        key: str,
        text: str,
        *,
        components: list[ScoreComponent] | None = None,
        classification: EvidenceClassification = EvidenceClassification.DIRECT,
        strength: EvidenceStrength = EvidenceStrength.MODERATE,
        requirement_id: str | None = None,
        direction: EvidenceDirection = EvidenceDirection.SUPPORTS,
    ) -> str:
        """Anchor text to the stored posting, quoting its opening if not found."""
        start = self.posting_text.find(text)
        anchored_text = text
        if start < 0:
            start, end = _sanitized_span(text, self.posting_text)
            anchored_text = self.posting_text[start:end]
        self.items.append(
            ScoreEvidenceInput(
                key=key,
                source_kind=EvidenceSourceKind.POSTING,
                text=anchored_text,
                char_start=start,
                char_end=start + len(anchored_text),
                locator=f"posting:{key}",
                components=components or [],
                classification=classification,
                strength=strength,
                requirement_id=requirement_id,
                direction=direction,
            )
        )
        return key

    def candidate(
        self,
        key: str,
        text: str,
        *,
        entry_id: str,
        components: list[ScoreComponent],
        classification: EvidenceClassification,
        strength: EvidenceStrength,
        requirement_id: str,
    ) -> str:
        """Anchor a master-CV entry, naming the entry when its text has moved."""
        start = self.candidate_text.find(text)
        self.items.append(
            ScoreEvidenceInput(
                key=key,
                source_kind=EvidenceSourceKind.CANDIDATE,
                text=text,
                char_start=start if start >= 0 else None,
                char_end=start + len(text) if start >= 0 else None,
                locator="" if start >= 0 else f"master-cv-entry:{entry_id}",
                master_cv_entry_id=entry_id,
                components=components,
                classification=classification,
                strength=strength,
                requirement_id=requirement_id,
            )
        )
        return key


def _sanitized_span(text: str, posting: str) -> tuple[int, int]:
    """Return the raw span of sanitized text in the posting, or of its opening when absent."""
    folded, offsets = sanitize_with_offsets(posting)
    span = locate(text, folded)
    if not span.verified:
        return 0, min(len(posting), FALLBACK_CHARACTERS)
    return offsets[span.start][0], offsets[span.end - 1][1]


def _unanswered_anchor(
    anchors: EvidenceAnchors, position: int, requirement: Requirement, match: Match | None
) -> str:
    """Anchor a requirement the record does not answer to its own words in the posting."""
    classification = (
        EvidenceClassification(match.classification.value)
        if match is not None
        else EvidenceClassification.GAP
    )
    strength = (
        EvidenceStrength(match.strength.value) if match is not None else EvidenceStrength.NONE
    )
    direction = (
        EvidenceDirection.LIMITS
        if classification in {EvidenceClassification.GAP, EvidenceClassification.ASSERTED}
        else EvidenceDirection.SUPPORTS
    )
    return anchors.posting(
        f"requirement-{position}-posting",
        requirement.text,
        components=anchors.leading(direction),
        classification=classification,
        strength=strength,
        requirement_id=requirement.id,
        direction=direction,
    )


def requirement_evidence(
    anchors: EvidenceAnchors,
    requirements: Sequence[Requirement],
    evidence_map: EvidenceMap,
    inventory: CvInventory,
) -> dict[str, list[str]]:
    """Anchor each requirement to its candidate entries, or to the posting.

    A requirement the record answers is anchored to the entries that answer it.
    One it does not is anchored to the requirement's own words in the posting,
    marked as limiting rather than supporting, so an unmet requirement still
    points at something a person can read.
    """
    keys_by_requirement: dict[str, list[str]] = {}
    for position, requirement in enumerate(requirements, 1):
        match = evidence_map.for_requirement(requirement.id)
        keys: list[str] = []
        if match is not None and match.entry_ids:
            for entry_position, entry_id in enumerate(match.entry_ids, 1):
                entry = inventory.by_id(entry_id)
                keys.append(
                    anchors.candidate(
                        f"requirement-{position}-candidate-{entry_position}",
                        entry.text,
                        entry_id=entry_id,
                        components=anchors.leading(),
                        classification=EvidenceClassification(match.classification.value),
                        strength=EvidenceStrength(match.strength.value),
                        requirement_id=requirement.id,
                    )
                )
        else:
            keys.append(_unanswered_anchor(anchors, position, requirement, match))
        keys_by_requirement[requirement.id] = keys
    return keys_by_requirement


def posting_body_anchor(anchors: EvidenceAnchors) -> None:
    """Anchor the posting itself when no supporting anchor carries the whole-evaluation scores.

    An evaluation whose every anchor limits its score could not otherwise say
    what the score was read from, so the stored body becomes the evidence of
    last resort. This also covers an evaluation with no anchor at all.
    """
    if not anchors.leading_claimed:
        anchors.leading_claimed = True
        anchors.posting(
            "posting-body",
            anchors.posting_text[: min(len(anchors.posting_text), FALLBACK_CHARACTERS)],
            components=list(LEADING_COMPONENTS),
        )


def disagreement_evidence(
    anchors: EvidenceAnchors,
    found: Sequence[Disagreement],
    reading_keys: Mapping[str, list[str]],
    hard_gate_evidence_keys: Sequence[str],
    fallback_key: str,
) -> list[tuple[Disagreement, list[str]]]:
    """Pair each disagreement with the evidence keys it cites, in order.

    A row backed by a reading answer cites that answer's keys, a row naming a hard
    gate cites that gate's anchor, and a row quoting posting lines anchors them. A
    row with none of these cites ``fallback_key``.
    """
    paired: list[tuple[Disagreement, list[str]]] = []
    for position, item in enumerate(found, 1):
        if item.answer:
            keys = list(reading_keys[item.answer])
        elif item.gate is not None:
            keys = [hard_gate_evidence_keys[item.gate]]
        else:
            keys = [
                anchors.posting(f"disagreement-{position}-{quote_position}", quote)
                for quote_position, quote in enumerate(item.evidence, 1)
            ]
        paired.append((item, keys or [fallback_key]))
    return paired


def reading_evidence(anchors: EvidenceAnchors, reading: Reading) -> dict[str, list[str]]:
    """Anchor every quote the deterministic reading relied on.

    A conflicting answer located two statements that cannot both be true, so its
    quotes contradict the finding instead of supporting it. A question the
    posting did not answer quotes nothing and anchors nothing.
    """
    keys_by_reading: dict[str, list[str]] = {}
    for name, answer in reading.named():
        keys: list[str] = []
        component = _READING_COMPONENTS.get(name)
        direction = EvidenceDirection.CONTRADICTS if answer.conflict else EvidenceDirection.SUPPORTS
        for position, quote in enumerate(answer.evidence, 1):
            keys.append(
                anchors.posting(
                    f"reading-{name}-{position}",
                    quote,
                    components=[component] if component is not None else [],
                    direction=direction,
                )
            )
        keys_by_reading[name] = keys
    return keys_by_reading
