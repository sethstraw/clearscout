"""Refusal gates for turning person-supplied answers into pending claims."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tailorcv.application.master_cv.cv import LIST_MARKER, CvInventory, EntryKind, kind_for_section
from tailorcv.application.tailoring.stages.context import CITABLE
from tailorcv.shared.errors import DiscoveryError

from .vocabulary import RANK, Ownership, Source, figures, reads_as, unsourced_words

if TYPE_CHECKING:
    from .conversation import Line, Reply


@dataclass(frozen=True)
class Placement:
    """Where in the master CV a confirmed claim goes.

    A role or a section, named by the key the parser assigns. Not cosmetic: an
    entry's section decides what kind of evidence it is and whether any stage may
    cite it at all.
    """

    role_key: str = ""
    section: str = ""

    @property
    def named(self) -> str:
        """Return the role or section this placement names, for a message."""
        return self.role_key or self.section


@dataclass(frozen=True)
class Pending:
    """Material discovery turned up. Not evidence, and not written anywhere.

    Everything a person has to confirm is a field here, so a confirmation is a
    decision about a printed object rather than about a summary of one.
    """

    subject: str
    text: str
    level: Ownership | None
    level_evidence: tuple[str, ...]
    metrics: tuple[str, ...]
    scope: str
    category: EntryKind
    placement: Placement
    gap_ids: tuple[str, ...]
    postings: tuple[str, ...]
    ambiguities: tuple[str, ...]
    replies: tuple[Reply, ...]

    def describe(self) -> list[str]:
        """Return the material as the eight things a person is confirming."""
        return [
            f"  wording:    {self.text}",
            f"  metrics:    {', '.join(self.metrics) or 'none stated, and none may be added'}",
            f"  ownership:  {self.level.value if self.level else 'none claimed'}"
            + (f"  (read from {', '.join(self.level_evidence)})" if self.level_evidence else ""),
            f"  scope:      {self.scope or 'not stated'}",
            f"  category:   {self.category.value}",
            f"  goes under: {self.placement.named}",
            f"  gaps:       {', '.join(self.gap_ids) or 'none named'}",
            "  source:     user-confirmed discovery, not the original document",
            f"  ambiguous:  {'; '.join(self.ambiguities) or 'nothing recorded'}",
        ]


def propose(
    line: Line,
    *,
    text: str,
    scope: str,
    placement: Placement,
    inventory: CvInventory,
    ambiguities: Sequence[str] = (),
) -> Pending:
    """Turn one answered line into pending material, or refuse to.

    Four refusals, in the order they are cheapest to explain. Nothing here
    writes anything, and material that survives all four is still pending.
    """
    if not line.answered:
        raise DiscoveryError(
            f"Discovery found nothing about {line.subject!r}, so there is nothing to record.\n"
            "  A gap that discovery did not close is still a gap, and it stays in the report."
        )
    _from_the_person(line.replies)
    stripped = " ".join(text.split())
    if not stripped:
        raise DiscoveryError("A discovered fact needs wording. Nothing was given.")
    if LIST_MARKER.match(stripped):
        raise DiscoveryError(
            "That wording starts with a list marker, which the master CV would not read.\n"
            "  The write adds the bullet itself. Propose the wording without the leading marker."
        )
    # Explain ownership conflicts instead of listing unmatched words.
    level, evidence = _no_higher_than_stated(stripped, line.replies)
    _only_figures_that_were_stated(stripped, line.replies)
    _only_words_that_were_said(stripped, line.replies)
    return Pending(
        subject=line.subject,
        text=stripped,
        level=level,
        level_evidence=evidence,
        metrics=figures(stripped),
        scope=" ".join(scope.split()),
        category=_category(placement, inventory),
        placement=placement,
        gap_ids=line.gap_ids,
        postings=line.postings,
        ambiguities=tuple(item for item in ambiguities if item.strip()),
        replies=line.replies,
    )


def _from_the_person(replies: Sequence[Reply]) -> None:
    """Refuse material built on anything but the person's own answers."""
    for reply in replies:
        if reply.source is not Source.PERSON:
            raise DiscoveryError(
                f"That material rests on {reply.source.value}, which cannot become evidence.\n"
                "  Generated resume text, a model's suggestion, and the job description are the "
                "three\n  ways a claim nobody made would reach the master CV. Ask the question "
                "again and\n  record what the person says."
            )


def _only_words_that_were_said(claim: str, replies: Sequence[Reply]) -> None:
    """Refuse a claim written in words no answer used."""
    missing = unsourced_words(claim, replies)
    if missing:
        raise DiscoveryError(
            f"That wording uses {len(missing)} word(s) no answer used: {', '.join(missing[:6])}.\n"
            "  A discovered fact is written in the words the person said it in. Wording that "
            "came\n  from a draft, from a model, or from the posting fails here on the words it "
            "uses,\n  which is the point: it is a claim nobody made."
        )


def _only_figures_that_were_stated(claim: str, replies: Sequence[Reply]) -> None:
    """Refuse a claim carrying a figure no answer stated."""
    stated = {value for reply in replies for value in figures(reply.text)}
    invented = [value for value in figures(claim) if value not in stated]
    if invented:
        raise DiscoveryError(
            f"That wording states {', '.join(invented)}, which no answer did.\n"
            "  A metric that was not stated does not exist. If the number is real, answer the "
            "question\n  with it and propose the wording again; if it is an estimate, leave it "
            "out."
        )


def _no_higher_than_stated(
    claim: str, replies: Sequence[Reply]
) -> tuple[Ownership | None, tuple[str, ...]]:
    """Return the claim's rung, refusing one above what the answers support.

    A claim that names no rung takes the one the answers evidence; a claim that
    names one above them is refused with both phrases quoted.
    """
    evidenced = reads_as("\n".join(reply.text for reply in replies))
    claimed = reads_as(claim)
    if claimed.value is None:
        return evidenced.value, evidenced.evidence
    if evidenced.value is None:
        raise DiscoveryError(
            f"That wording claims {claimed.value}, and no answer says what the person did.\n"
            f"  It was read from {', '.join(claimed.evidence)}. Answer the question with what "
            "you did\n  with it, in your own words, and propose the wording again."
        )
    if RANK[claimed.value] > RANK[evidenced.value]:
        raise DiscoveryError(
            f"That wording claims {claimed.value} and the answers describe "
            f"{evidenced.value}.\n"
            f"  The wording says {', '.join(claimed.evidence)}; the answers say "
            f"{', '.join(evidenced.evidence)}.\n"
            "  Exposure, use, support, evaluation, configuration, implementation, ownership,\n"
            "  leadership, and deployment are different claims, and this one climbs. Write it "
            "at\n  the level the answers describe."
        )
    return claimed.value, claimed.evidence


def _category(placement: Placement, inventory: CvInventory) -> EntryKind:
    """Return the kind of evidence a claim will be, from where it is going.

    Read from the placement rather than chosen, because the parser decides this
    and the write checks that it agreed.
    """
    if placement.role_key:
        role = inventory.role(placement.role_key)
        if role is None:
            raise DiscoveryError(
                f"The master CV has no role {placement.role_key!r}.\n"
                f"  It has: {', '.join(item.key for item in inventory.roles) or 'no roles'}."
            )
        return EntryKind.BULLET
    if placement.section not in inventory.sections:
        raise DiscoveryError(
            f"The master CV has no section {placement.section!r}.\n"
            f"  It has: {', '.join(inventory.sections) or 'no sections'}."
        )
    kind = kind_for_section(placement.section)
    if kind not in CITABLE:
        raise DiscoveryError(
            f"Nothing in {placement.section!r} can be cited by a resume, so a fact put there "
            "would\n  never be used. Put it under a role, or under a section this tool reads as "
            "evidence."
        )
    return kind
