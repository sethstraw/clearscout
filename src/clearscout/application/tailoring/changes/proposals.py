"""Turn one draft into proposals, each classified and explained from the record."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from clearscout.application.config import Settings
from clearscout.application.master_cv.cv import CvInventory
from clearscout.application.tailoring.changes.record import (
    Excerpt,
    Kind,
    Proposal,
    Review,
    _run_payload,
    digest_of,
    set_digest,
)
from clearscout.application.tailoring.changes.slots import _in_document_order
from clearscout.application.tailoring.guardrails import Guardrails
from clearscout.application.tailoring.runs import RunIdentity
from clearscout.application.tailoring.stages.draft import Document, DraftedLine, ResumeDraft
from clearscout.application.tailoring.stages.jobspec import JobSpec
from clearscout.application.tailoring.stages.match import EvidenceMap
from clearscout.application.tailoring.stages.select import Plan
from clearscout.application.tailoring.stages.verify import NUMBER, WORD, bare_word
from clearscout.shared.results import table_cell

# This list affects explanatory text, not validation.
_STOPWORD_TEXT = (
    "about above across after against among and are around because been before being "
    "between both but during each for from had has have how including into its more "
    "most not off only other over own same some such than that the their them then "
    "there these they this those through under until upon was were what when where "
    "which while who will with within without would you your"
)
STOPWORDS = frozenset(_STOPWORD_TEXT.split())
MIN_NEW_WORD = 4
MAX_NEW_WORDS = 8
RATIONALE_QUOTE = 160


def review(
    *,
    identity: RunIdentity,
    draft: ResumeDraft,
    plan: Plan,
    inventory: CvInventory,
    spec: JobSpec,
    evidence: EvidenceMap,
    settings: Settings,
    guardrails: Guardrails,
    excluded: frozenset[str] = frozenset(),
) -> Review:
    """Return every reviewable line of one draft, in the order it appears."""
    builder = _Builder(
        plan=plan,
        inventory=inventory,
        spec=spec,
        evidence=evidence,
        excluded=excluded,
        run=_run_payload(identity),
    )
    return builder.review(draft, settings, guardrails)


@dataclass(frozen=True)
class _Builder:
    """Assemble the proposals for one draft. Nothing here asks a model anything."""

    plan: Plan
    inventory: CvInventory
    spec: JobSpec
    evidence: EvidenceMap
    excluded: frozenset[str]
    run: dict[str, Any]

    def review(self, draft: ResumeDraft, settings: Settings, guardrails: Guardrails) -> Review:
        """Return the finished review, with identifiers and digests assigned."""
        raw = [
            *self._header(draft, settings, guardrails),
            *self._written(draft),
            *self._copied(),
            *self._unused(draft),
        ]
        proposals: list[Proposal] = []
        for index, proposal in enumerate(raw, 1):
            numbered = replace(proposal, id=f"p{index:02d}")
            proposals.append(replace(numbered, digest=digest_of(self.run, numbered.content())))
        digests = tuple(proposal.digest for proposal in proposals)
        return Review(
            run=self.run, proposals=tuple(proposals), digest=set_digest(self.run, digests)
        )

    def _header(
        self, draft: ResumeDraft, settings: Settings, guardrails: Guardrails
    ) -> list[Proposal]:
        """Return the name, contact, and work authorization lines, all copied.

        Read out of the assembler rather than rebuilt here, so the review lists
        the header the document actually carries.
        """
        document = Document(draft, self.inventory, self.plan, settings, guardrails, self.excluded)
        lines = [line for line in document.header().splitlines() if line.strip()]
        authorization = guardrails.work_authorization or ""
        built: list[Proposal] = []
        for index, line in enumerate(lines):
            settings_line = bool(authorization) and line == authorization
            source = "settings rules" if settings_line else "master CV header"
            slot = "header/name" if index == 0 else f"header/line[{index}]"
            built.append(
                self._proposal(
                    slot=slot,
                    kind=Kind.PRESERVED,
                    immutable=True,
                    text=line.lstrip("# ").strip() if index == 0 else line,
                    evidence_ids=(),
                    excerpts=(Excerpt(source=source, text=line),),
                )
            )
        return built

    def _written(self, draft: ResumeDraft) -> list[Proposal]:
        """Return every line the model wrote, in document order."""
        return [
            self._line_proposal(slot, line) for slot, line in _in_document_order(draft, self.plan)
        ]

    def _line_proposal(self, slot: str, line: DraftedLine) -> Proposal:
        """Return one drafted line as a proposal, classified and explained."""
        excerpts = self._excerpts(line.source_ids)
        trimmed = bool(line.source_ids) and set(line.source_ids) <= self.excluded
        kind = Kind.OMITTED if trimmed else _classify(line, excerpts)
        return self._proposal(
            slot=slot,
            kind=kind,
            immutable=False,
            text=line.text,
            evidence_ids=tuple(line.source_ids),
            excerpts=excerpts,
            trimmed=trimmed,
        )

    def _copied(self) -> list[Proposal]:
        """Return the education and publication entries, copied verbatim."""
        built: list[Proposal] = []
        for index, entry_id in enumerate(self.plan.education_ids):
            built.append(self._verbatim(f"education[{index}]", entry_id, trimmed=False))
        for index, entry_id in enumerate(self.plan.publication_ids):
            built.append(
                self._verbatim(
                    f"publications[{index}]", entry_id, trimmed=entry_id in self.excluded
                )
            )
        return built

    def _verbatim(self, slot: str, entry_id: str, *, trimmed: bool) -> Proposal:
        """Return one entry the assembler copies rather than drafts."""
        excerpts = self._excerpts((entry_id,))
        text = excerpts[0].text if excerpts else ""
        return self._proposal(
            slot=slot,
            kind=Kind.OMITTED if trimmed else Kind.PRESERVED,
            immutable=True,
            text="" if trimmed else text,
            evidence_ids=(entry_id,),
            excerpts=excerpts,
            trimmed=trimmed,
        )

    def _unused(self, draft: ResumeDraft) -> list[Proposal]:
        """Return the selected evidence no line drew on.

        What the model did not use is part of what a reviewer is deciding
        about, and it is the only part of a review a resume cannot show.
        """
        used = {entry_id for line in draft.lines() for entry_id in line.source_ids}
        used |= set(self.plan.education_ids) | set(self.plan.publication_ids)
        return [
            self._proposal(
                slot=f"unused/{entry_id}",
                kind=Kind.OMITTED,
                immutable=False,
                text="",
                evidence_ids=(entry_id,),
                excerpts=self._excerpts((entry_id,)),
                trimmed=entry_id in self.excluded,
            )
            for entry_id in self.plan.all_ids()
            if entry_id not in used
        ]

    def _proposal(
        self,
        *,
        slot: str,
        kind: Kind,
        immutable: bool,
        text: str,
        evidence_ids: tuple[str, ...],
        excerpts: tuple[Excerpt, ...],
        trimmed: bool = False,
    ) -> Proposal:
        """Return one proposal with its requirements resolved and its rationale built."""
        requirements = self._requirements(evidence_ids)
        return Proposal(
            id="",
            slot=slot,
            kind=kind,
            immutable=immutable,
            text=text,
            evidence_ids=evidence_ids,
            evidence=excerpts,
            requirement_ids=requirements,
            rationale=self._rationale(kind, text, evidence_ids, excerpts, requirements, trimmed),
        )

    def _requirements(self, evidence_ids: tuple[str, ...]) -> tuple[str, ...]:
        """Return the requirement identifiers the cited evidence was kept for."""
        found: list[str] = []
        for entry_id in evidence_ids:
            for requirement_id in self.plan.requirements_for.get(entry_id, ()):
                if requirement_id not in found:
                    found.append(requirement_id)
        return tuple(sorted(found))

    def _excerpts(self, evidence_ids: tuple[str, ...]) -> tuple[Excerpt, ...]:
        """Return the exact master-CV text behind each identifier."""
        found: list[Excerpt] = []
        for entry_id in evidence_ids:
            try:
                entry = self.inventory.by_id(entry_id)
            except KeyError:  # pragma: no cover - the vocabulary is closed over the CV
                found.append(Excerpt(source=entry_id, text="(not in this master CV)"))
                continue
            found.append(Excerpt(source=entry.id, text=entry.full_text))
        return tuple(found)

    def _rationale(
        self,
        kind: Kind,
        text: str,
        evidence_ids: tuple[str, ...],
        excerpts: tuple[Excerpt, ...],
        requirements: tuple[str, ...],
        trimmed: bool,
    ) -> str:
        """Return why this line is here, built from what code already knows."""
        parts = [self._origin(kind, evidence_ids, trimmed)]
        if answered := self._answers(requirements):
            parts.append(answered)
        # Rewritten text requires source validation; copied text already has it.
        if kind in (Kind.NEW, Kind.REFRAMED):
            if figures := _figures(text):
                parts.append(f"Carries {', '.join(figures)}, each present in the cited evidence.")
            parts.append(_wording(text, excerpts))
        return " ".join(parts)

    def _origin(self, kind: Kind, evidence_ids: tuple[str, ...], trimmed: bool) -> str:
        """Return the clause describing where this line came from."""
        cited = ", ".join(evidence_ids)
        if kind is Kind.OMITTED and trimmed:
            return f"Selected from {cited} and dropped to meet the page budget."
        if kind is Kind.OMITTED:
            return f"Selected as evidence ({cited}) and used by no line in the draft."
        if kind is Kind.PRESERVED and not evidence_ids:
            return (
                "Copied from the master CV. Employers, titles, dates, education, and "
                "publication citations are not a model's or a reviewer's to rewrite."
            )
        if kind is Kind.PRESERVED:
            return f"Repeats {cited} word for word."
        if kind is Kind.REFRAMED:
            return f"Rewrites {cited}."
        if not evidence_ids:
            return "Cites no evidence, which the provenance check rejects."
        return f"Joins {len(evidence_ids)} entries ({cited}); no single entry states this."

    def _answers(self, requirements: tuple[str, ...]) -> str:
        """Return the clause naming the requirements this line was kept for."""
        if not requirements:
            return ""
        first = self.spec.requirement(requirements[0])
        if first is None:  # pragma: no cover - the map is built from this spec
            return f"Answers {', '.join(requirements)}."
        match = self.evidence.for_requirement(first.id)
        how = f", {match.classification.value.casefold()}" if match is not None else ""
        # Strip the source's own period before truncation so the ellipsis a
        # truncated quote ends with is never stripped away with it.
        quote = table_cell(first.text.rstrip("."), RATIONALE_QUOTE)
        stop = "" if quote.endswith("...") else "."
        return f"Answers {', '.join(requirements)} ({first.importance.value}{how}): {quote}{stop}"


def _classify(line: DraftedLine, excerpts: tuple[Excerpt, ...]) -> Kind:
    """Return PRESERVED for a cited entry's exact text, REFRAMED for one rewritten, else NEW."""
    if any(_normalized(line.text) == _normalized(item.text) for item in excerpts):
        return Kind.PRESERVED
    return Kind.REFRAMED if len(excerpts) == 1 else Kind.NEW


def _normalized(text: str) -> str:
    """Return text with whitespace collapsed, for a verbatim comparison."""
    return " ".join(text.split())


def _figures(text: str) -> tuple[str, ...]:
    """Return the numeric tokens a reviewer should check, in the order they read."""
    return tuple(dict.fromkeys(NUMBER.findall(text)))


def _wording(text: str, excerpts: tuple[Excerpt, ...]) -> str:
    """Return the clause naming words this line uses and its sources do not."""
    known = {bare_word(word) for item in excerpts for word in WORD.findall(item.text)}
    fresh: list[str] = []
    for word in WORD.findall(text):
        folded = bare_word(word)
        if len(folded) < MIN_NEW_WORD or folded in STOPWORDS or folded in known:
            continue
        if folded not in fresh:
            fresh.append(folded)
    if not fresh:
        return "Uses no word its cited evidence does not."
    shown = ", ".join(fresh[:MAX_NEW_WORDS])
    more = f", and {len(fresh) - MAX_NEW_WORDS} more" if len(fresh) > MAX_NEW_WORDS else ""
    return f"Wording not in the cited evidence: {shown}{more}."
