"""Map requirements to real evidence, and make inventing evidence impossible.

The response schema is built at runtime so that the field holding evidence
identifiers is a ``Literal`` over exactly the identifiers this parse of the
master CV produced. The same call classifies each requirement against that
evidence.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from enum import StrEnum
from itertools import pairwise
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from tailorcv.application.config import Stage
from tailorcv.application.master_cv.cv import CvInventory
from tailorcv.application.prompts import render
from tailorcv.application.tailoring.guardrails import Guardrails
from tailorcv.application.tailoring.stages.context import (
    PARTICIPATION,
    asserted_text,
    citable_entries,
    record_block,
    requirement_text,
)
from tailorcv.application.tailoring.stages.jobspec import (
    Dimension,
    HardGate,
    Importance,
    JobSpec,
    Requirement,
    requirement_vocabulary,
)
from tailorcv.domain import CITABLE_EVIDENCE, EvidenceStatus, GapClass, SpanStatus
from tailorcv.domain.requirements import PARAMETER_KINDS, GateKind, states_schedule_or_language
from tailorcv.infrastructure.providers.client import Client
from tailorcv.infrastructure.providers.plan import RequestPlan, Segment


class Classification(StrEnum):
    """How well the candidate's record answers a requirement.

    ``ASSERTED`` answers a requirement no accomplishment can evidence (work
    authorization, citizenship, residency, location); it is neither a gap nor
    cited evidence.
    """

    DIRECT = "DIRECT"
    TRANSFERABLE = "TRANSFERABLE"
    TAILORING_ONLY = "TAILORING_ONLY"
    ASSERTED = "ASSERTED"
    GAP = "GAP"


class Strength(StrEnum):
    """How strongly the cited evidence supports the requirement."""

    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"
    NONE = "none"


STRENGTHS = {
    Strength.STRONG: 1.0,
    Strength.MODERATE: 0.6,
    Strength.WEAK: 0.3,
    Strength.NONE: 0.0,
}


class Match(BaseModel):
    """One requirement, the evidence that answers it, and how well.

    ``evidence_status`` and ``gap_class`` are internal: the model is not asked
    for either and neither crosses the wire.
    """

    model_config = ConfigDict(frozen=True)

    requirement_id: str
    entry_ids: tuple[str, ...] = ()
    classification: Classification
    strength: Strength
    note: str = ""
    named_tools: tuple[str, ...] = ()
    evidence_status: EvidenceStatus = EvidenceStatus.VERIFIED
    gap_class: GapClass | None = None

    @property
    def score(self) -> float:
        """Return the numeric strength selection multiplies by requirement weight."""
        return STRENGTHS[self.strength]

    @property
    def may_support_a_claim(self) -> bool:
        """Return whether a draft may cite this match's evidence for a claim."""
        return self.evidence_status in CITABLE_EVIDENCE

    @property
    def needs_review(self) -> bool:
        """Return whether this run established nothing here, which is not itself a gap."""
        return self.gap_class is GapClass.INCOMPLETE_EVIDENCE


class EvidenceMap(BaseModel):
    """Every requirement's evidence, and the provenance record for the report."""

    model_config = ConfigDict(frozen=True)

    matches: tuple[Match, ...] = ()
    unrecognized: tuple[str, ...] = ()
    # Each (requirement id, tool) the model listed that the requirement's text never states.
    dropped_tools: tuple[tuple[str, str], ...] = ()

    def for_requirement(self, requirement_id: str) -> Match | None:
        """Return the match for one requirement."""
        return next((item for item in self.matches if item.requirement_id == requirement_id), None)

    def unnamed_tools(
        self, spec: JobSpec, inventory: CvInventory
    ) -> tuple[tuple[Requirement, tuple[str, ...]], ...]:
        """Return each answered requirement with the named tools none of its cited entries names."""
        found: list[tuple[Requirement, tuple[str, ...]]] = []
        for requirement in spec.requirements:
            match = self.for_requirement(requirement.id)
            if match is None:
                continue
            cited = [entry.full_text for entry in inventory.entries if entry.id in match.entry_ids]
            missing = tuple(
                tool
                for tool in match.named_tools
                if not any(names_tool(text, tool) for text in cited)
            )
            if cited and missing:
                found.append((requirement, missing))
        return tuple(found)

    def example_notes(self, spec: JobSpec) -> tuple[tuple[Requirement, str], ...]:
        """Return each requirement whose note calls an item of its example list missing."""
        found: list[tuple[Requirement, str]] = []
        for requirement in spec.requirements:
            match = self.for_requirement(requirement.id)
            clause = (
                _missing_example(
                    requirement.text,
                    match.note,
                    spec.company or "",
                    gap=match.classification is Classification.GAP,
                )
                if match
                else None
            )
            if clause:
                found.append((requirement, clause))
        return tuple(found)

    def gaps(self) -> tuple[Match, ...]:
        """Return requirements the record does not answer, which lead the report.

        A requirement answered by an asserted personal fact is not a gap, and
        neither is one this run failed to establish either way.
        """
        return tuple(
            item
            for item in self.matches
            if item.classification is Classification.GAP and not item.needs_review
        )

    def asserted(self) -> tuple[Match, ...]:
        """Return requirements answered by a stated fact rather than by evidence."""
        return tuple(
            item for item in self.matches if item.classification is Classification.ASSERTED
        )

    def needs_review(self) -> tuple[Match, ...]:
        """Return requirements this run could not establish either way."""
        return tuple(item for item in self.matches if item.needs_review)

    def weighted_fit(self, requirements: Sequence[Requirement]) -> int:
        """Return the share of the posting's weighted hard requirements the record answers."""
        # A behavioral requirement is answered by wording, so it would lift fit for free.
        requirements = [item for item in requirements if item.dimension is Dimension.HARD]
        offered = sum(requirement.weight for requirement in requirements)
        if not offered:
            return 0
        answered = sum(
            requirement.weight * match.score
            for requirement in requirements
            if (match := self.for_requirement(requirement.id)) is not None
        )
        return round(answered / offered * 100)

    def indirect_share(self, inventory: CvInventory, requirements: Sequence[Requirement]) -> float:
        """Return the share of answered requirements resting on indirect evidence.

        Transferable and stated answers count, and so does a tailoring-only answer
        to a hard requirement whose cited evidence says the applicant only took
        part in the work.
        """
        answered = tuple(
            item for item in self.matches if item.classification is not Classification.GAP
        )
        if not answered:
            return 0.0
        behavioral = frozenset(
            requirement.id
            for requirement in requirements
            if requirement.dimension is Dimension.BEHAVIORAL
        )
        leaning = sum(
            1 for item in answered if _rests_on_indirect_evidence(item, inventory, behavioral)
        )
        return leaning / len(answered)


def _rests_on_indirect_evidence(
    match: Match, inventory: CvInventory, behavioral: frozenset[str]
) -> bool:
    """Return whether a match leans on evidence weaker than direct work of the applicant's."""
    if match.classification in {Classification.TRANSFERABLE, Classification.ASSERTED}:
        return True
    # Helping others narrates a behavioral trait rather than limiting the applicant's part.
    if match.requirement_id in behavioral:
        return False
    return match.classification is Classification.TAILORING_ONLY and any(
        PARTICIPATION.search(entry.full_text)
        for entry in inventory.entries
        if entry.id in match.entry_ids
    )


# Minimum wording overlap for pairing a gate and requirement.
GATE_OVERLAP = 0.6

# Words by which a requirement's own prose states a circumstance the search
# parameters settle.
PARAMETER_CONDITIONS = (
    "location",
    "place",
    "residence",
    "reside",
    "relocation",
    "arrangement",
    "remote",
    "hybrid",
    "onsite",
    "on-site",
    "office",
    "attendance",
    "cadence",
    "travel",
    "time zone",
    "timezone",
    "authorisation",
    "authorization",
    "sponsorship",
    "visa",
    "work permit",
    "eligibility",
    "employment type",
    "compensation",
    "salary",
    "pay",
)


# Awards granted by an institution after years of study, examination, or
# vetting: a requirement naming one states a barrier no rewrite closes.
FORMAL_CREDENTIALS = (
    "phd",
    "ph.d",
    "doctorate",
    "doctoral",
    "terminal degree",
    "medical degree",
    "law degree",
    "juris doctor",
    "board certified",
    "board certification",
    "licensure",
    "professional license",
    "active license",
    "security clearance",
    "bar admission",
)

# A posting offering an alternative to the credential states no barrier.
EQUIVALENT_ROUTES = ("or equivalent", "equivalent experience", "or comparable")

# A stated minimum in years is not answered by picking something up.
EXPERIENCE_THRESHOLD = re.compile(r"\b\d+\s*\+?\s*(?:years|yrs)\b")

# A denial states the opposite of the phrase it governs, as in "no security clearance
# required" or "does not require a doctorate"; up to two words may sit between them.
NEGATED_LEAD_IN = re.compile(r"\b(?:no|not|without|never)\b(?:\s+\w+){0,2}\s*$")


def decided_by_parameters(kind: GateKind) -> bool:
    """Return whether the search parameters, not the record, settle this condition."""
    return kind in PARAMETER_KINDS


class GateAnswer(BaseModel):
    """A stated condition, and how well the record actually answers it.

    Hard gates and requirements are extracted as separate lists, so without
    joining them a gate the posting calls absolute appears in neither the gates
    table nor the gaps table.
    """

    model_config = ConfigDict(frozen=True)

    kind: GateKind
    text: str
    blocking: bool
    span_status: SpanStatus = SpanStatus.NEEDS_REVIEW
    classification: Classification | None = None
    strength: Strength | None = None
    partial: bool = False

    @property
    def verified(self) -> bool:
        """Return whether the posting was found to contain this gate's quote."""
        return self.span_status is SpanStatus.VERIFIED

    @property
    def verified_blocking(self) -> bool:
        """Return whether the posting was found to state this gate as absolute."""
        return self.blocking and self.verified

    @property
    def answered_by_parameters(self) -> bool:
        """Return whether the search parameters settle this condition instead of the record."""
        return decided_by_parameters(self.kind)

    @property
    def about_hours_or_language(self) -> bool:
        """Return whether this condition asks about hours or language, which no record answers."""
        return self.kind == "other" and states_schedule_or_language(self.text)

    @property
    def answered_by_record(self) -> bool:
        """Return whether the career record, not the parameters or the person, answers this."""
        return not (self.answered_by_parameters or self.about_hours_or_language)

    @property
    def directly_met(self) -> bool:
        """Return whether the record answers this gate with cited evidence."""
        return self.classification is Classification.DIRECT

    @property
    def needs_a_human(self) -> bool:
        """Return whether a person should read this gate before spending an afternoon."""
        return not self.verified or (self.blocking and not self.directly_met)

    @property
    def summary(self) -> str:
        """Return how the gate was answered, in one readable phrase."""
        if self.answered_by_parameters:
            return "your search parameters"
        if self.about_hours_or_language:
            return "you, since a career record cannot answer hours or language"
        if self.classification is None:
            return "answered in part" if self.partial else "not checked"
        if self.classification is Classification.DIRECT:
            return "direct evidence"
        if self.classification is Classification.GAP:
            return "no evidence"
        if self.classification is Classification.ASSERTED:
            return "stated by you"
        strength = f", {self.strength.value}" if self.strength else ""
        return f"{self.classification.value.casefold()}{strength}"


def answered_gates(spec: JobSpec, evidence: EvidenceMap) -> tuple[GateAnswer, ...]:
    """Pair each stated gate with the evidence for the requirement that restates it."""
    return tuple(_answer(gate, spec, evidence) for gate in spec.hard_gates)


def _answer(gate: HardGate, spec: JobSpec, evidence: EvidenceMap) -> GateAnswer:
    """Pair a gate with the verdict of the requirements that restate it.

    A settled requirement in the gate's own words decides it alone. Otherwise an
    answered requirement beside a gapped or unestablished one answers the gate
    in part, unless the parts are options the gate lists, which take their best
    answer; any other gate takes its weakest, since a combined condition is met
    only as well as its weakest part.
    """
    unpaired = GateAnswer(
        kind=gate.kind, text=gate.text, blocking=gate.blocking, span_status=gate.source_span.status
    )
    if unpaired.about_hours_or_language:
        return unpaired
    restated, parts = _overlapping_matches(gate.text, spec, evidence)
    unestablished = any(match.needs_review for match in restated)
    deciding, either = _deciding_matches(gate.text, restated, parts)
    considered = restated if restated and not unestablished else [*restated, *(m for _, m in parts)]
    partial = not either and _answered_in_part(unestablished, considered)
    selected: Match | None = None
    if not unestablished and not partial:
        answered = [match for match in deciding if match.classification is not Classification.GAP]
        settled = [match for match in deciding if not match.needs_review]
        pick = max if either else min
        selected = pick(answered, key=_weakness) if answered else next(iter(settled), None)
    return unpaired.model_copy(
        update={
            "classification": selected.classification if selected else None,
            "strength": selected.strength if selected else None,
            "partial": partial,
        }
    )


def _deciding_matches(
    gate_text: str, restated: list[Match], parts: list[tuple[str, Match]]
) -> tuple[list[Match], bool]:
    """Return the matches that grade a gate, and whether they are options it lists.

    Parts wholly inside the gate's words displace any other part whose shared
    content words they already cover.
    """
    if restated:
        return restated, False
    wanted = _words(gate_text)
    inside = [(text, match) for text, match in parts if _words(text) <= wanted]
    covered = frozenset[str]().union(*(_words(text) for text, _ in inside))
    kept = [
        (text, match)
        for text, match in parts
        if not inside
        or _words(text) <= wanted
        or not ((_words(text) & wanted) - GATE_FUNCTION_WORDS) <= covered
    ]
    return [match for _, match in kept], _alternatives(gate_text, [text for text, _ in kept])


def _answered_in_part(unestablished: bool, considered: Sequence[Match]) -> bool:
    """Return whether an answered requirement sits beside a gapped or unestablished one."""
    answered = any(match.classification is not Classification.GAP for match in considered)
    if unestablished:
        return answered
    return answered and any(match.classification is Classification.GAP for match in considered)


def _weakness(match: Match) -> tuple[bool, float, bool]:
    """Order answers weakest first: transferable, then by strength, then indirect."""
    return (
        match.classification is not Classification.TRANSFERABLE,
        match.score,
        match.classification is Classification.DIRECT,
    )


def _overlapping_matches(
    gate_text: str, spec: JobSpec, evidence: EvidenceMap
) -> tuple[list[Match], list[tuple[str, Match]]]:
    """Return the matches restating the gate in its own words, then its parts with their text.

    A loosely worded preference is not the absolute condition, so a nice-to-have
    requirement is a part only when its words lie wholly inside the gate's.
    """
    wanted = _words(gate_text)
    restated: list[Match] = []
    parts: list[tuple[str, Match]] = []
    for requirement in spec.requirements:
        match = evidence.for_requirement(requirement.id)
        if match is None:
            continue
        words = _words(requirement.text)
        if wanted and words == wanted:
            restated.append(match)
        elif _overlap(wanted, words) >= GATE_OVERLAP and (
            requirement.importance is not Importance.NICE_TO_HAVE or words <= wanted
        ):
            parts.append((requirement.text, match))
    return restated, parts


# Words a loose part may share with a gate without restating any of its content.
GATE_FUNCTION_WORDS = frozenset(
    ("a", "an", "and", "or", "of", "in", "on", "the", "to", "for", "with", "at", "by", "as")
)

# A join between listed options: an "or" among separators, or a bare line break
# with an optional bullet, which counts only beside an "or" join.
OR_JOIN = re.compile(r"[\s,;]*or[\s,;]*(?:[-*]\s*)?", re.IGNORECASE)
LIST_BREAK = re.compile(r"[\s,;]*\n\s*(?:[-*]\s*)?")


def _alternatives(gate_text: str, texts: Sequence[str]) -> bool:
    """Return whether the parts are options the gate lists verbatim, in order, joined by "or"."""
    options = [text.strip().removesuffix(".") for text in texts]
    starts = [gate_text.find(option) for option in options]
    if len(options) < 2 or min(starts) < 0:
        return False
    placed = sorted(zip(starts, options, strict=True))
    joins = [
        gate_text[start + len(option) : following]
        for (start, option), (following, _) in pairwise(placed)
    ]
    either = [OR_JOIN.fullmatch(join) is not None for join in joins]
    return any(either) and all(
        joined or LIST_BREAK.fullmatch(join) for join, joined in zip(joins, either, strict=True)
    )


def names_tool(text: str, tool: str) -> bool:
    """Return whether the text names the tool as a whole term, ignoring case and spacing."""
    wanted = " ".join(tool.split())
    if not wanted:
        return False
    # Lookarounds rather than \b, so a term ending in a symbol, such as C++, still bounds.
    pattern = rf"(?<!\w){re.escape(wanted).replace(r'\ ', r'\s+')}(?!\w)"
    return re.search(pattern, text, re.IGNORECASE) is not None


# Cues opening examples of an ask, which needs none of the listed items. A match
# note also reads "including" as one.
EXAMPLE_CUES = r"\be\.?\s?g\b|\bsuch\s+as\b|\bfor\s+example\b"
_EXAMPLE_CUE = re.compile(rf"{EXAMPLE_CUES}|\bincluding\b", re.IGNORECASE)
# A list opened inside parentheses runs to their close, an "including" list to a
# clause stop, and any other also to a purpose ("to read", "for product work").
_ASIDE_CLOSE = re.compile(r"\)")
_INCLUDED_CLOSE = re.compile(r"[.;](?:\s|$)|\)")
_LIST_CLOSE = re.compile(rf"{_INCLUDED_CLOSE.pattern}|\s(?:(?:in\s+order\s+)?to|for)\s")
# Wording by which a model's note calls part of a requirement unshown. A bare "is not"
# must end its clause and a leading "not" must open one.
_GRANTED = r"\b(documented|shown|evidenced|demonstrated|established)\b"
_NOT_SHOWN = re.compile(
    r"\b(?:not|never)\s+(?:(?:explicitly|directly|clearly)\s+)?"
    r"(?:established|shown|evidenced|demonstrated|stated|documented|claimed|explicit)\b"
    r"|\b(?:is|are|was|were)\s+not\s*(?:,|$)|^\s*not\b(?!\s+(?:only|just|merely)\b)"
    rf"|{_GRANTED}\s*,\s*not\b"
    r"|\bwithout\s+(?:the\s+)?(?:\w+\s+)?(?:breadth|full|entire|complete)\b"
    r"|\bno\s+evidence\s+(?:of|for)\b|\bmissing\b|\blacks?\b|\babsent\b",
    re.IGNORECASE,
)
# A clause opening with "not" that still grants an item concedes a side point.
_CONCEDED = re.compile(
    rf"^\s*not\b.*\b(?:is|are|was|were)\s+(?:(?!not\b)\w+\s+)?{_GRANTED}", re.IGNORECASE
)
# "documented, not claimed" grants the item and denies only a weaker reading of it.
_GRANT_CONTRASTED = re.compile(
    rf"{_GRANTED}\s*,\s*not\s+(?:claimed|asserted|implied|inferred|assumed|self-reported)\b",
    re.IGNORECASE,
)
# Narrowing an example to familiarity, which falls short only of an ask for more.
_FAMILIARITY_ONLY = r"\bas\s+familiarity\b|\bfamiliarity\s+only\b"
_NOT_SHOWN_OR_FAMILIARITY_ONLY = re.compile(
    rf"{_NOT_SHOWN.pattern}|{_FAMILIARITY_ONLY}", re.IGNORECASE
)
_FAMILIARITY = frozenset(("familiarity", "familiar"))
# Wording by which a note calls an example list unshown as a whole.
_WHOLE_LIST = re.compile(
    r"\b(?:full|entire|complete|breadth)\b"
    r"|\ball\s+(?:\d+|two|three|four|five|six|seven|eight|nine|ten|listed|named|stated|specified)\b"
    r"|\b(?:specified|stated|named|listed)\s+(?:set|mix|range|list)\b",
    re.IGNORECASE,
)
_NOTE_CLAUSE = re.compile(r"[;.](?:\s|$)|\b(?:but|though|although|while|whereas)\b", re.IGNORECASE)
_SPECIFIC = re.compile(r"(\w+)-specific\b")
_LIST_FILLER = GATE_FUNCTION_WORDS | {"other", "etc", "more"}
# Nouns before which a capitalized word in the ask names the employer's own offering.
_OFFERING = re.compile(
    r"\s+(?:product|platform|solution|suite|app|application|model|engagement|service|tool)s?\b"
)
_SENTENCE_OPEN = re.compile(r"(?:^|[.;:!?])\s*$")


def _example_spans(text: str) -> list[tuple[int, int, int]]:
    """Return each example list's cue start, first item start, and end."""
    spans = []
    for cue in _EXAMPLE_CUE.finditer(text):
        rest = text[cue.end() :]
        items = cue.end() + len(rest) - len(rest.lstrip(" \t\n.,:"))
        inside = text.rfind("(", 0, cue.start()) > text.rfind(")", 0, cue.start())
        if inside:
            close = _ASIDE_CLOSE
        elif cue.group().casefold() == "including":
            close = _INCLUDED_CLOSE
        else:
            close = _LIST_CLOSE
        stop = close.search(text, items)
        spans.append((cue.start(), items, stop.start() if stop else len(text)))
    return spans


def _missing_example(text: str, note: str, company: str, *, gap: bool) -> str | None:
    """Return the note's clause calling unshown a word only the requirement's examples use.

    A "<Word>-specific" clause counts too when the ask never states that word, or
    when the word names the employer or an offering the ask names; so does a clause
    calling a requirement's example list unshown as a whole.
    """
    spans = _example_spans(text)
    ask = text
    for start, _, end in spans:
        ask = ask[:start] + " " * (end - start) + ask[end:]
    asked = _words(ask)
    listed = frozenset[str]().union(*(_words(text[items:end]) for _, items, end in spans))
    listed_only = listed - asked - _LIST_FILLER
    asked_singular = {_singular(word) for word in asked}
    # A gap may call an entry familiarity only against an ask for more.
    narrowed = _NOT_SHOWN if gap or asked & _FAMILIARITY else _NOT_SHOWN_OR_FAMILIARITY_ONLY
    for clause in _NOTE_CLAUSE.split(note):
        reading = _GRANT_CONTRASTED.sub(r"\1", clause)
        if _CONCEDED.search(clause) or not narrowed.search(reading):
            continue
        named = _words(narrowed.sub(" ", reading))
        unasked = any(
            _singular(word.casefold()) not in asked_singular
            or _names_the_employer(word, text, company)
            for word in _SPECIFIC.findall(clause)
        )
        whole_list = bool(spans) and _WHOLE_LIST.search(clause) is not None
        if named & listed_only or unasked or whole_list:
            return clause.strip()
    return None


def _singular(word: str) -> str:
    """Return a word with a plural's trailing "s" dropped."""
    return word.removesuffix("s") if len(word) > 3 else word


def _names_the_employer(word: str, text: str, company: str) -> bool:
    """Return whether the word is the employer's name or an offering the ask names by it.

    Only a title-case word counts, so an acronym such as AI or a generic "health" stays a domain.
    """
    if not word.istitle():
        return False
    if word.casefold() in _words(company):
        return True
    for found in re.finditer(rf"\b{re.escape(word)}(?={_OFFERING.pattern})", text):
        if not _SENTENCE_OPEN.search(text[: found.start()]):
            return True
    return False


def example_list_notes(spec: JobSpec, evidence: EvidenceMap) -> list[str]:
    """Return one advisory per requirement whose note holds an unrequired item against it."""
    return [
        f'The note for "{requirement.text}" says "{clause}", holding against the record '
        "something the ask does not require: a listed example, or the employer's own name "
        "or offering."
        for requirement, clause in evidence.example_notes(spec)
    ]


def _words(text: str) -> frozenset[str]:
    """Return the comparable words of a phrase, ignoring punctuation and case."""
    return frozenset(re.findall(r"[a-z0-9+]+", text.casefold()))


def _overlap(left: frozenset[str], right: frozenset[str]) -> float:
    """Return how much of the shorter phrase the two share."""
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def response_schema(
    entry_ids: tuple[str, ...], requirement_ids: tuple[str, ...]
) -> type[BaseModel]:
    """Build a response schema whose identifier fields are closed over real values.

    ``create_model`` rather than a static class because the vocabulary is the
    parse of the master CV that just happened, and the whole guarantee is that
    a citation cannot be anything else. ``requirement_ids`` is the offered
    vocabulary, which is wider than one posting's own requirements.
    """
    entry_literal = Literal[entry_ids]  # type: ignore[valid-type]
    requirement_literal = Literal[requirement_ids]  # type: ignore[valid-type]
    match_model = create_model(
        "ConstrainedMatch",
        __config__=ConfigDict(extra="forbid"),
        requirement_id=(requirement_literal, ...),
        # Listed before the citations, so the tools a requirement names shape what is cited.
        named_tools=(list[str], Field(default_factory=list)),
        entry_ids=(list[entry_literal], Field(default_factory=list)),
        note=(str, ""),
        classification=(Classification, ...),
        strength=(Strength, ...),
    )
    return create_model(
        "ConstrainedEvidenceMap",
        __config__=ConfigDict(extra="forbid"),
        matches=(list[match_model], Field(default_factory=list)),  # type: ignore[valid-type]
    )


def _normalized_matches(response: BaseModel, spec: JobSpec) -> EvidenceMap:
    """Make the model's one-row-per-requirement contract true in Python.

    JSON Schema cannot require each identifier to occur exactly once in an
    array, so missing, duplicate, or contradictory rows become explicit gaps.
    Weak output may hide a real match; it can never hide a gap. A row naming an
    identifier this posting does not have is discarded and recorded.
    """
    payload = response.model_dump(mode="json")
    raw = EvidenceMap.model_validate(payload)
    requirement_ids = tuple(requirement.id for requirement in spec.requirements)
    grouped, unrecognized = _grouped_rows(raw.matches, requirement_ids)
    dropped: list[tuple[str, str]] = []
    normalized = []
    for requirement in spec.requirements:
        rows = [_stated_tools(requirement, row, dropped) for row in grouped[requirement.id]]
        normalized.append(_normalized_row(requirement, rows, spec))
    return EvidenceMap(
        matches=tuple(normalized),
        unrecognized=tuple(dict.fromkeys(unrecognized)),
        dropped_tools=tuple(dict.fromkeys(dropped)),
    )


def _stated_tools(requirement: Requirement, row: Match, dropped: list[tuple[str, str]]) -> Match:
    """Keep the listed tools the requirement's own text names, adding the rest to ``dropped``."""
    listed = tuple(dict.fromkeys(tool.strip() for tool in row.named_tools if tool.strip()))
    kept = tuple(tool for tool in listed if names_tool(requirement.text, tool))
    dropped += [(requirement.id, tool) for tool in listed if tool not in kept]
    return row.model_copy(update={"named_tools": kept})


def _grouped_rows(
    rows: Sequence[Match], requirement_ids: tuple[str, ...]
) -> tuple[dict[str, list[Match]], list[str]]:
    """Group the returned rows by requirement, separating out identifiers this posting lacks."""
    grouped: dict[str, list[Match]] = {requirement_id: [] for requirement_id in requirement_ids}
    unrecognized: list[str] = []
    for item in rows:
        known = grouped.get(item.requirement_id)
        if known is None:
            unrecognized.append(item.requirement_id)
            continue
        known.append(item)
    return grouped, unrecognized


def _normalized_row(requirement: Requirement, candidates: list[Match], spec: JobSpec) -> Match:
    """Reduce one requirement's rows to the single match the rest of the run may read."""
    requirement_id = requirement.id
    if len(candidates) != 1:
        reason = "missing" if not candidates else "duplicated"
        return _gap(
            requirement_id,
            f"Model result was {reason}; treated as a gap.",
            GapClass.INCOMPLETE_EVIDENCE,
        )

    item = candidates[0]
    entry_ids = tuple(dict.fromkeys(item.entry_ids))[:4]
    if item.classification is Classification.ASSERTED:
        # Remove unsupported citations so this match cannot reach selection.
        return Match(
            requirement_id=requirement_id,
            classification=Classification.ASSERTED,
            strength=Strength.NONE,
            note=item.note or "Answered by a fact the applicant states.",
            evidence_status=EvidenceStatus.SELF_ATTESTED,
        )
    if item.classification is Classification.GAP:
        if entry_ids or item.strength is not Strength.NONE:
            # A gap that cites evidence settles nothing either way, like a missing row.
            return _gap(
                requirement_id,
                "Model returned contradictory evidence for a gap; treated as a gap.",
                GapClass.INCOMPLETE_EVIDENCE,
            )
        return _gap(
            requirement_id,
            item.note or "No supported evidence was returned.",
            _gap_class(requirement, spec),
        )
    if not entry_ids or item.strength is Strength.NONE:
        # A model holding the whole record and citing nothing for it is the
        # anti-fabrication catch, not a run that learned nothing.
        return _gap(
            requirement_id,
            "Model returned a classification without support; treated as a gap.",
            _gap_class(requirement, spec),
        )
    return item.model_copy(update={"entry_ids": entry_ids})


def _states(text: str, phrases: Sequence[str]) -> bool:
    """Return whether the text names one of these phrases as whole words and does not deny it."""
    lowered = text.casefold()
    return any(
        not NEGATED_LEAD_IN.search(lowered[: found.start()])
        for phrase in phrases
        for found in re.finditer(rf"\b{re.escape(phrase)}\b", lowered)
    )


def _about_circumstances(requirement: Requirement, spec: JobSpec) -> bool:
    """Return whether this requirement restates a circumstance the parameters settle."""
    if _states(requirement.text, PARAMETER_CONDITIONS):
        return True
    wanted = _words(requirement.text)
    return any(
        decided_by_parameters(gate.kind) and _overlap(wanted, _words(gate.text)) >= GATE_OVERLAP
        for gate in spec.hard_gates
    )


def _gap_class(requirement: Requirement, spec: JobSpec) -> GapClass:
    """Return the class an unanswered gap supports, reading the posting's own words.

    The branches read the quote located in the posting body, falling back to the
    extracted wording when no quote was placed. A requirement answering none of
    them keeps the general class rather than naming a barrier nothing showed.
    """
    stated = requirement.quote_or_span if requirement.verified else requirement.text
    optional = requirement.importance is Importance.NICE_TO_HAVE
    if _about_circumstances(requirement, spec):
        return GapClass.GEOGRAPHY_EMPLOYMENT
    if (
        not optional
        and _states(stated, FORMAL_CREDENTIALS)
        and not _states(stated, EQUIVALENT_ROUTES)
    ):
        return GapClass.PERMANENT_FORMAL
    if requirement.dimension is Dimension.BEHAVIORAL:
        return GapClass.UNCLEAR_POSITIONING
    if optional and not EXPERIENCE_THRESHOLD.search(stated):
        return GapClass.BOUNDED_LEARNABLE
    return GapClass.UNSUPPORTED_CORE


def _gap(requirement_id: str, note: str, gap_class: GapClass) -> Match:
    """Return an unresolved gap carrying no strength and no citation."""
    return Match(
        requirement_id=requirement_id,
        classification=Classification.GAP,
        strength=Strength.NONE,
        note=note,
        evidence_status=EvidenceStatus.UNRESOLVED,
        gap_class=gap_class,
    )


def match(
    client: Client, inventory: CvInventory, spec: JobSpec, guardrails: Guardrails
) -> EvidenceMap:
    """Map every requirement to real evidence identifiers in one call.

    ``guardrails`` is here so this stage sees the asserted facts it is judged
    by: the record holds citable achievements only, and a posting can ask for
    work authorization, residency, or a location that no achievement evidences.
    """
    entries = citable_entries(inventory)
    entry_ids = tuple(entry.id for entry in entries)
    requirement_ids = tuple(requirement.id for requirement in spec.requirements)
    if not entry_ids or not requirement_ids:
        return EvidenceMap()

    schema = response_schema(entry_ids, requirement_vocabulary(len(requirement_ids)))
    policy = client.settings.policy_for(Stage.MATCH)
    plan = RequestPlan(
        stage=Stage.MATCH,
        instructions=Segment(
            render("match_system", distinctions=guardrails.prompt_block()), "match_system"
        ),
        task=Segment(
            render(
                "match_user",
                requirements=requirement_text(spec),
                role=spec.title,
                asserted=asserted_text(inventory, guardrails),
            ),
            "match_user",
        ),
        evidence=Segment(record_block(inventory)),
        max_tokens=policy.max_tokens,
    )
    response = client.complete(plan, schema)
    return _normalized_matches(response, spec)
