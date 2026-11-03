"""Screen postings independently and compare their requirements deterministically."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from clearscout.application.config import Settings
from clearscout.application.master_cv.cv import CvInventory
from clearscout.application.tailoring.elicit import FUNCTION_WORDS
from clearscout.application.tailoring.fit.facts import level_in
from clearscout.application.tailoring.stages.jobspec import (
    Dimension,
    Importance,
    JobSpec,
    Requirement,
)
from clearscout.application.tailoring.stages.match import (
    GATE_OVERLAP,
    Classification,
    EvidenceMap,
    GateAnswer,
    Match,
    Strength,
    answered_gates,
)
from clearscout.application.tailoring.stages.verify import WORD, bare_word
from clearscout.domain.records import PostingReadIntent
from clearscout.domain.requirements import (
    gate_kind_label,
    states_schedule_or_language,
    states_work_logistics,
)
from clearscout.domain.search_parameters.roles import Seniority
from clearscout.domain.taxonomy import Taxonomy, Vocabulary, extended
from clearscout.infrastructure.providers.credentials import Prompter
from clearscout.shared.costs import Ledger, ServiceTier
from clearscout.shared.costs.prices import ledger_for
from clearscout.shared.errors import ConfigError
from clearscout.shared.results import table_cell

from .requests import Request, _key, _sized

# Ignore very short words when finding shared requirements.
MIN_TERM = 3

# Ignore terms shared by most requirements as common noise.
COMMON_SHARE = 0.5

# Bound report cell length before truncation.
CELL = 90


class Verdict(StrEnum):
    """What the findings about one posting add up to."""

    PURSUE = "pursue"
    CAUTIOUS = "cautious pursue"
    SKIP = "skip"


# The weighted share of a posting's requirements a verdict commits outside of.
PURSUE_FIT = 60
SKIP_FIT = 50

# How many core must-haves answered weakly by transferable work cap a pursue.
THIN_CORES_CAP = 2

# How many of the rows that pulled fit down a fit reason names.
WEAKEST_NAMED = 2

# The separators between the alternatives one search boundary lists.
_ALTERNATIVES = re.compile(r";|,\s+or\s+|\s+or\s+", re.IGNORECASE)

# How each classification answers a requirement, as a fit reason words it.
_ANSWERED_BY = {
    Classification.DIRECT: "cited evidence",
    Classification.TRANSFERABLE: "transferable work",
    Classification.TAILORING_ONLY: "how it is written",
}


@dataclass(frozen=True)
class Recommendation:
    """One posting's verdict and every reason it rests on."""

    verdict: Verdict
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Screening:
    """What one posting of a batch produced, on its own."""

    request: Request
    key: str
    spec: JobSpec
    evidence: EvidenceMap
    posting_id: str
    read: str = ""
    report: Path | None = None
    ledger: Ledger = field(default_factory=Ledger)


@dataclass(frozen=True)
class Cluster:
    """One requirement more than one posting asks for, in each one's own words."""

    label: str
    joined_by: tuple[str, ...]
    wordings: tuple[tuple[str, str], ...]
    must_have_in: tuple[str, ...] = ()
    blocking_in: tuple[str, ...] = ()
    gap_ids: tuple[str, ...] = ()

    @property
    def postings(self) -> tuple[str, ...]:
        """Return the postings that state this requirement, in batch order."""
        return tuple(dict.fromkeys(key for key, _ in self.wordings))

    @property
    def leverage(self) -> int:
        """Return how many postings one closed gap here would help."""
        return len(self.postings)

    @property
    def subject(self) -> str:
        """Return what a discovery question about this should be about."""
        if self.joined_by:
            return " and ".join(self.joined_by[:2])
        return self.label.rstrip(".")


@dataclass
class Batch:
    """Every posting's screening, kept apart, and their comparison."""

    screenings: tuple[Screening, ...]
    shared: tuple[Cluster, ...]
    opportunities: tuple[Cluster, ...]
    recommendations: dict[str, Recommendation]
    started: str = ""
    notes: list[str] = field(default_factory=list)

    def screening(self, key: str) -> Screening | None:
        """Return one posting's screening by its key."""
        return next((item for item in self.screenings if item.key == key), None)

    def ordered(self) -> tuple[Screening, ...]:
        """Return the postings in reading order: the person's priority first."""
        return tuple(
            sorted(
                self.screenings,
                key=lambda item: (-item.request.priority, self.screenings.index(item)),
            )
        )


def analyse(
    screenings: Sequence[Screening], *, vocabulary_from: Settings | None = None
) -> tuple[tuple[Cluster, ...], tuple[Cluster, ...]]:
    """Return shared requirements and where one closed gap helps most."""
    return compare(
        {item.key: item.spec for item in screenings},
        {item.key: item.evidence for item in screenings},
        vocabulary=vocabulary_from.vocabulary if vocabulary_from is not None else None,
    )


def compare(
    specs: Mapping[str, JobSpec],
    evidence: Mapping[str, EvidenceMap],
    *,
    vocabulary: Vocabulary | None = None,
) -> tuple[tuple[Cluster, ...], tuple[Cluster, ...]]:
    """Return shared requirements and ranked gaps for a set of postings."""
    lexicon = extended(vocabulary)
    everything = _entries(specs, evidence, gaps_only=False)
    common = _common_terms(everything)
    shared = tuple(
        cluster for cluster in _cluster(everything, common, lexicon) if cluster.leverage > 1
    )
    gaps = _cluster(_entries(specs, evidence, gaps_only=True), common, lexicon)
    return shared, gaps


def _cluster(
    entries: Sequence[_Entry], common: frozenset[str], lexicon: Taxonomy
) -> tuple[Cluster, ...]:
    """Group requirements that say the same thing, across postings."""
    groups: list[list[_Entry]] = []
    joins: list[list[str]] = []
    for entry in entries:
        for group, joined in zip(groups, joins, strict=True):
            shared = _same_requirement(entry, group[0], common, lexicon)
            if shared is not None:
                group.append(entry)
                joined.extend(term for term in shared if term not in joined)
                break
        else:
            groups.append([entry])
            joins.append([])
    built = [
        Cluster(
            label=group[0].text,
            joined_by=tuple(joined),
            wordings=tuple((entry.posting, entry.text) for entry in group),
            must_have_in=tuple(dict.fromkeys(entry.posting for entry in group if entry.must_have)),
            blocking_in=tuple(dict.fromkeys(entry.posting for entry in group if entry.blocking)),
            gap_ids=tuple(f"{entry.posting}:{entry.requirement_id}" for entry in group),
        )
        for group, joined in zip(groups, joins, strict=True)
    ]
    return tuple(
        sorted(
            built,
            key=lambda item: (
                -item.leverage,
                -len(item.must_have_in),
                -len(item.blocking_in),
                item.label,
            ),
        )
    )


@dataclass(frozen=True)
class _Entry:
    """One requirement of one posting, with everything clustering reads."""

    posting: str
    requirement_id: str
    text: str
    must_have: bool
    blocking: bool
    terms: frozenset[str]


def _entries(
    specs: Mapping[str, JobSpec], evidence: Mapping[str, EvidenceMap], *, gaps_only: bool
) -> tuple[_Entry, ...]:
    """Return requirements to cluster, in the order the postings state them."""
    found: list[_Entry] = []
    for key, spec in specs.items():
        gaps = (
            {match.requirement_id for match in evidence[key].gaps()} if key in evidence else set()
        )
        found.extend(_posting_entries(key, spec, gaps, gaps_only=gaps_only))
    return tuple(found)


def _posting_entries(key: str, spec: JobSpec, gaps: set[str], *, gaps_only: bool) -> list[_Entry]:
    """Return one posting's clusterable requirements, in the order it states them."""
    blocking = tuple(gate.text for gate in spec.hard_gates if gate.verified_blocking)
    found: list[_Entry] = []
    for requirement in spec.requirements:
        if requirement.importance is Importance.RESPONSIBILITY:
            continue
        if gaps_only and requirement.id not in gaps:
            continue
        found.append(
            _Entry(
                posting=key,
                requirement_id=requirement.id,
                text=requirement.text,
                must_have=requirement.importance is Importance.MUST_HAVE,
                blocking=any(_overlap(item, requirement.text) >= GATE_OVERLAP for item in blocking),
                terms=_terms(requirement.text),
            )
        )
    return found


def _terms(text: str) -> frozenset[str]:
    """Return words that could distinguish one requirement from another."""
    return frozenset(
        folded
        for word in WORD.findall(text)
        if len(folded := bare_word(word)) >= MIN_TERM and folded not in FUNCTION_WORDS
    )


def _common_terms(entries: Sequence[_Entry]) -> frozenset[str]:
    """Return words this batch uses everywhere, which therefore join nothing."""
    if not entries:
        return frozenset()
    counts: dict[str, int] = {}
    for entry in entries:
        for term in entry.terms:
            counts[term] = counts.get(term, 0) + 1
    ceiling = max(1, int(len(entries) * COMMON_SHARE))
    return frozenset(term for term, count in counts.items() if count > ceiling)


def _same_requirement(
    left: _Entry, right: _Entry, common: frozenset[str], lexicon: Taxonomy
) -> tuple[str, ...] | None:
    """Return why two requirements are the same thing, or nothing when not."""
    if _flat(left.text) == _flat(right.text):
        return ("the same wording",)
    distinctive = (left.terms & right.terms) - common
    if distinctive:
        return tuple(sorted(distinctive))
    for family in lexicon.families:
        for phrase in family.terms:
            if lexicon.mentions(phrase, left.text) and lexicon.mentions(phrase, right.text):
                return (phrase,)
    return None


def _flat(text: str) -> str:
    """Return wording with case, spacing, and punctuation removed."""
    return " ".join(sorted(bare_word(word) for word in WORD.findall(text)))


def _overlap(left: str, right: str) -> float:
    """Return how much of the shorter phrase two texts share."""
    words_left = frozenset(bare_word(word) for word in WORD.findall(left))
    words_right = frozenset(bare_word(word) for word in WORD.findall(right))
    if not words_left or not words_right:
        return 0.0
    return len(words_left & words_right) / min(len(words_left), len(words_right))


def _recommendation_reasons(
    *,
    unanswered: list[GateAnswer],
    unpaired: list[GateAnswer],
    partial: list[GateAnswer],
    weak: list[GateAnswer],
    unverified: list[GateAnswer],
    essential: list[Requirement],
    unresolved: list[Requirement],
    by_parameters: list[GateAnswer] | None = None,
    hours_or_language: list[GateAnswer] | None = None,
    personal: list[Requirement] | None = None,
    logistics: list[Requirement] | None = None,
) -> list[str]:
    """Build the durable reasons supporting one recommendation verdict."""
    reasons: list[str] = []
    for gate in by_parameters or []:
        reasons.append(
            f"the posting states {gate_kind_label(gate.kind)} absolutely and your search "
            f"parameters answer it rather than the record: {table_cell(gate.text, CELL)}"
        )
    for gate in hours_or_language or []:
        reasons.append(
            "the posting states, absolutely, a condition about working hours or language "
            f"that a career record cannot answer: {table_cell(gate.text, CELL)}"
        )
    for gate in unanswered:
        reasons.append(
            f"the posting states {gate_kind_label(gate.kind)} absolutely and nothing in the "
            f"record answers it: {table_cell(gate.text, CELL)}"
        )
    for gate in unpaired:
        reasons.append(
            f"the posting states {gate_kind_label(gate.kind)} absolutely and this run paired "
            f"nothing to it, so it is unestablished rather than failed: "
            f"{table_cell(gate.text, CELL)}"
        )
    for gate in partial:
        reasons.append(
            f"the posting states {gate_kind_label(gate.kind)} absolutely and the record "
            f"answers it only in part: {table_cell(gate.text, CELL)}"
        )
    for gate in weak:
        reasons.append(
            f"the posting states {gate_kind_label(gate.kind)} absolutely and the record "
            f"answers it by {gate.summary}: {table_cell(gate.text, CELL)}"
        )
    for gate in unverified:
        reasons.append(
            f"the gate on {gate_kind_label(gate.kind)} quotes wording the posting does not "
            f"contain, so nothing has shown it is stated: {table_cell(gate.text, CELL)}"
        )
    for requirement in essential:
        reasons.append(
            f"{requirement.id} is stated as essential and the record does not answer it: "
            f"{table_cell(requirement.text, CELL)}"
        )
    for requirement in personal or []:
        reasons.append(
            f"{requirement.id} is stated as essential, a condition about working hours or "
            f"language that a career record cannot answer: {table_cell(requirement.text, CELL)}"
        )
    for requirement in logistics or []:
        reasons.append(
            f"{requirement.id} is stated as essential, a condition about travel, connectivity, "
            "or a workspace that you or your search parameters answer rather than the record: "
            f"{table_cell(requirement.text, CELL)}"
        )
    for requirement in unresolved:
        reasons.append(
            f"{requirement.id} is stated as essential and this run did not establish it "
            f"either way: {table_cell(requirement.text, CELL)}"
        )
    return reasons


@dataclass(frozen=True)
class _RecommendationFindings:
    """The classified conditions that determine one recommendation."""

    unanswered: list[GateAnswer]
    unpaired: list[GateAnswer]
    partial: list[GateAnswer]
    weak: list[GateAnswer]
    unverified: list[GateAnswer]
    essential: list[Requirement]
    unresolved: list[Requirement]
    by_parameters: list[GateAnswer]
    hours_or_language: list[GateAnswer]
    personal: list[Requirement]
    logistics: list[Requirement]


@dataclass(frozen=True)
class _GateFindings:
    """The posting gates grouped by how this run answered them."""

    unanswered: list[GateAnswer]
    unpaired: list[GateAnswer]
    partial: list[GateAnswer]
    weak: list[GateAnswer]
    unverified: list[GateAnswer]
    by_parameters: list[GateAnswer]
    hours_or_language: list[GateAnswer]


def _gate_findings(spec: JobSpec, evidence: EvidenceMap) -> _GateFindings:
    """Partition posting gates by their recommendation effect."""
    gates = answered_gates(spec, evidence)
    by_parameters = [
        gate for gate in gates if gate.verified_blocking and gate.answered_by_parameters
    ]
    hours_or_language = [
        gate for gate in gates if gate.verified_blocking and gate.about_hours_or_language
    ]
    record_gates = [
        gate for gate in gates if gate not in by_parameters and not gate.about_hours_or_language
    ]
    unanswered = [
        gate
        for gate in record_gates
        if gate.verified_blocking and gate.classification is Classification.GAP
    ]
    partial = [gate for gate in record_gates if gate.verified_blocking and gate.partial]
    unpaired = [
        gate
        for gate in record_gates
        if gate.verified_blocking and gate.classification is None and not gate.partial
    ]
    return _GateFindings(
        unanswered=unanswered,
        unpaired=unpaired,
        partial=partial,
        weak=[
            gate
            for gate in record_gates
            if gate.verified
            and gate.needs_a_human
            and gate not in unanswered
            and gate not in unpaired
            and gate not in partial
        ],
        unverified=[gate for gate in gates if not gate.verified],
        by_parameters=by_parameters,
        hours_or_language=hours_or_language,
    )


def _must_have_findings(
    spec: JobSpec, evidence: EvidenceMap
) -> tuple[list[Requirement], list[Requirement], list[Requirement], list[Requirement]]:
    """Return failed, unresolved, hours-or-language, and logistics must-have requirements."""
    gaps = {match.requirement_id for match in evidence.gaps()}
    unresolved_ids = {match.requirement_id for match in evidence.needs_review()}
    must_have = [
        requirement
        for requirement in spec.requirements
        if requirement.importance is Importance.MUST_HAVE
    ]
    # Hours, language, travel, and workspace are the person's to answer, so a record
    # gap on one caps nothing.
    personal = [
        requirement for requirement in must_have if states_schedule_or_language(requirement.text)
    ]
    logistics = [
        requirement
        for requirement in must_have
        if requirement not in personal and states_work_logistics(requirement.text)
    ]
    answerable = [
        requirement
        for requirement in must_have
        if requirement not in personal
        and requirement not in logistics
        and requirement.dimension is Dimension.HARD
    ]
    return (
        [requirement for requirement in answerable if requirement.id in gaps],
        [requirement for requirement in answerable if requirement.id in unresolved_ids],
        personal,
        logistics,
    )


def _recommendation_findings(spec: JobSpec, evidence: EvidenceMap) -> _RecommendationFindings:
    """Partition gates and must-have requirements by their decision effect."""
    gates = _gate_findings(spec, evidence)
    essential, unresolved, personal, logistics = _must_have_findings(spec, evidence)
    return _RecommendationFindings(
        unanswered=gates.unanswered,
        unpaired=gates.unpaired,
        partial=gates.partial,
        weak=gates.weak,
        unverified=gates.unverified,
        essential=essential,
        unresolved=unresolved,
        by_parameters=gates.by_parameters,
        hours_or_language=gates.hours_or_language,
        personal=personal,
        logistics=logistics,
    )


def _ruled_out_occupation(spec: JobSpec, boundaries: tuple[str, ...]) -> str | None:
    """Return the reason text for a core occupation the search rules out, or None."""
    # A boundary named at extraction counts only while the search still states it, and
    # is quoted whole so the reason names the boundary as the person wrote it.
    if spec.excluded_boundary is not None and spec.excluded_boundary in boundaries:
        return (
            f"{table_cell(spec.excluded_boundary)} (the posting: "
            f'"{table_cell(spec.excluded_boundary_quote, CELL)}")'
        )
    archetype = _excluded_archetype(spec, boundaries)
    return None if archetype is None else table_cell(archetype, CELL)


def _excluded_archetype(spec: JobSpec, boundaries: tuple[str, ...]) -> str | None:
    """Return the core occupation a search boundary rules out, or None.

    Matches the model's distilled occupation once it has cited a span the posting
    contains, never the title or requirements. Each alternative a boundary lists
    must be grounded whole by the label; an unstructured qualifier abstains.
    """
    occupation = spec.core_occupation
    if occupation is None or not occupation.verified:
        return None
    occupation_terms = _archetype_terms(occupation.text)
    for boundary in boundaries:
        for alternative in _ALTERNATIVES.split(boundary):
            alternative_terms = _archetype_terms(alternative)
            if alternative_terms and alternative_terms <= occupation_terms:
                return occupation.text
    return None


def _archetype_terms(text: str) -> frozenset[str]:
    """Return the role-defining words a grounded occupation can establish."""
    return frozenset(
        word
        for token in WORD.findall(text)
        if (word := bare_word(token)) and word not in FUNCTION_WORDS
    )


def _posting_level(spec: JobSpec) -> tuple[str, str] | None:
    """Return the ladder rung the posting names and the text it was read from.

    Reads the title first and the seniority field only when the title names no
    rung, so the reason can cite whichever one actually carried it.
    """
    for text in (spec.title, spec.seniority):
        if text and (found := level_in(text)) is not None:
            return found[0], text
    return None


def _over_altitude(spec: JobSpec, seniority: Seniority | None) -> tuple[str, str] | None:
    """Return the posting's rung and its source when above the band ceiling, or None.

    Abstains when no band ceiling is set or the posting names no rung, so a cap
    never fires on an altitude the search did not bound.
    """
    if seniority is None:
        return None
    found = _posting_level(spec)
    if found is None:
        return None
    return found if seniority.exceeds(found[0]) else None


def _thin_years_minimum(gates: Sequence[GateAnswer]) -> GateAnswer | None:
    """Return a blocking years minimum the record answers below moderate strength, if at all."""
    return next(
        (
            gate
            for gate in gates
            if gate.kind == "minimum_years"
            and gate.strength not in (Strength.STRONG, Strength.MODERATE)
        ),
        None,
    )


def _thin_core_must_haves(
    spec: JobSpec, evidence: EvidenceMap, person_answered: Sequence[Requirement]
) -> list[tuple[Requirement, Match]]:
    """Return the hard must-haves answered only weakly by transferable work, if enough to cap."""
    found: list[tuple[Requirement, Match]] = []
    for requirement in spec.requirements:
        if requirement.importance is not Importance.MUST_HAVE:
            continue
        if requirement.dimension is not Dimension.HARD or requirement in person_answered:
            continue
        match = evidence.for_requirement(requirement.id)
        if (
            match is not None
            and match.classification is Classification.TRANSFERABLE
            and match.strength is Strength.WEAK
        ):
            found.append((requirement, match))
    return found if len(found) >= THIN_CORES_CAP else []


def recommend(
    spec: JobSpec,
    evidence: EvidenceMap,
    *,
    boundaries: tuple[str, ...] = (),
    seniority: Seniority | None = None,
) -> Recommendation:
    """Return what the findings about one posting add up to, and why."""
    findings = _recommendation_findings(spec, evidence)
    reasons = _recommendation_reasons(
        unanswered=findings.unanswered,
        unpaired=findings.unpaired,
        partial=findings.partial,
        weak=findings.weak,
        unverified=findings.unverified,
        essential=findings.essential,
        unresolved=findings.unresolved,
        by_parameters=findings.by_parameters,
        hours_or_language=findings.hours_or_language,
        personal=findings.personal,
        logistics=findings.logistics,
    )
    # Named beside a gate skip too, so unwanted work reads apart from a waivable gate.
    excluded = _ruled_out_occupation(spec, boundaries)
    if excluded is not None:
        reasons.append(f"the posting's core occupation is work your search rules out: {excluded}")
    if findings.unanswered or excluded is not None:
        return Recommendation(Verdict.SKIP, tuple(reasons))
    direct = sum(1 for match in evidence.matches if match.classification is Classification.DIRECT)
    if not direct:
        reasons.append(
            "no requirement is answered by cited evidence, so this run established "
            "nothing to pursue"
        )
        return Recommendation(Verdict.CAUTIOUS, tuple(reasons))
    # An unsettled run commits in neither direction.
    unsettled = (
        findings.unresolved or findings.partial or len(findings.unpaired) + len(findings.weak) > 1
    )
    over_altitude = _over_altitude(spec, seniority)
    if over_altitude is not None:
        rung, source = over_altitude
        reasons.append(
            f"the posting reads at {rung}, above the seniority band your search sets: "
            f"{table_cell(source, CELL)}"
        )
    # The weak and unpaired gate reasons above already name the years minimum that caps.
    thin_years = _thin_years_minimum((*findings.weak, *findings.unpaired))
    thin_cores = _thin_core_must_haves(spec, evidence, (*findings.personal, *findings.logistics))
    if thin_cores:
        rows = "; ".join(_weak_row(requirement, match) for requirement, match in thin_cores)
        reasons.append(
            f"{len(thin_cores)} essential requirements are answered only weakly by "
            f"transferable work: {rows}"
        )
    fit = evidence.weighted_fit(spec.requirements)
    weakest = _weakest_answers(
        spec,
        evidence,
        named=frozenset(
            requirement.id
            for requirement in (
                *findings.essential,
                *findings.personal,
                *findings.logistics,
                *(requirement for requirement, _ in thin_cores),
            )
        ),
    )
    # An essential must-have the record cannot answer, a rung above the band, a years minimum
    # answered weakly or not paired, or several thinly answered core must-haves caps the
    # ceiling at CAUTIOUS; a weak fit still skips.
    capped = (
        unsettled
        or findings.essential
        or over_altitude is not None
        or thin_years is not None
        or bool(thin_cores)
    )
    # A body cut short of its requirements often marks nothing required and states no gate.
    unmarked = not any(r.importance is Importance.MUST_HAVE for r in spec.requirements) and not any(
        gate.verified_blocking for gate in spec.hard_gates
    )
    if not capped and fit >= PURSUE_FIT and unmarked:
        reasons.append(
            f"the record answers {fit} per cent of what the posting weights, but no "
            "requirement is marked required and no gate is stated, so the posting may be "
            f"incomplete{weakest}"
        )
        return Recommendation(Verdict.CAUTIOUS, tuple(reasons))
    if not capped and fit >= PURSUE_FIT:
        return Recommendation(
            Verdict.PURSUE,
            (*reasons, f"the record answers {fit} per cent of what the posting weights{weakest}"),
        )
    if not unsettled and fit <= SKIP_FIT:
        return Recommendation(
            Verdict.SKIP,
            (
                *reasons,
                f"the record answers only {fit} per cent of what the posting weights{weakest}",
            ),
        )
    if not capped:
        reasons.append(
            f"the record answers {fit} per cent of what the posting weights, above the "
            f"deprioritize line of {SKIP_FIT} and below the apply-now line of {PURSUE_FIT}"
            f"{weakest}"
        )
    return Recommendation(Verdict.CAUTIOUS, tuple(reasons))


def _weakest_answers(spec: JobSpec, evidence: EvidenceMap, *, named: frozenset[str]) -> str:
    """Return the clause naming the rows that pulled fit down most, must-haves first.

    Within each importance the lowest-scoring answer leads, and a gap leads an
    answer of the same score. Rows in ``named`` already have a reason of their
    own and are left out. Empty when every other weighted row is answered strongly.
    """
    ranked: list[tuple[bool, float, bool, int, Requirement, Match | None]] = []
    for position, requirement in enumerate(spec.requirements):
        if (
            requirement.importance is Importance.RESPONSIBILITY
            or requirement.dimension is not Dimension.HARD
            or requirement.id in named
        ):
            continue
        match = evidence.for_requirement(requirement.id)
        # An ASSERTED row is answered by what the user states, never graded by strength.
        if match is not None and match.classification is Classification.ASSERTED:
            continue
        score = match.score if match is not None else 0.0
        if score >= 1.0:
            continue
        answered = match is not None and match.classification is not Classification.GAP
        must_have = requirement.importance is Importance.MUST_HAVE
        ranked.append((not must_have, score, answered, position, requirement, match))
    rows = [
        _weak_row(requirement, match)
        for *_, requirement, match in sorted(ranked, key=lambda row: row[:4])[:WEAKEST_NAMED]
    ]
    return f", and its weakest answers are {'; '.join(rows)}" if rows else ""


def _weak_row(requirement: Requirement, match: Match | None) -> str:
    """Return one requirement with its importance and how weakly the record answers it."""
    mark = "essential" if requirement.importance is Importance.MUST_HAVE else "preferred"
    if match is None or match.classification is Classification.GAP:
        grade = "not answered"
    else:
        grade = f"{match.strength.value} by {_ANSWERED_BY[match.classification]}"
    text = requirement.text.rstrip().removesuffix(".")
    return f"{requirement.id} ({mark}, {grade}): {table_cell(text, CELL)}"


def fit(spec: JobSpec, evidence: EvidenceMap) -> str:
    """Return one sentence saying how the record answers this posting."""
    counts: dict[str, int] = {}
    for match in evidence.matches:
        counts[match.classification.value] = counts.get(match.classification.value, 0) + 1
    parts = [
        f"{counts.get(Classification.DIRECT.value, 0)} answered by cited evidence",
        f"{counts.get(Classification.TRANSFERABLE.value, 0)} by transferable work",
        f"{counts.get(Classification.TAILORING_ONLY.value, 0)} only by how it is written",
        f"{counts.get(Classification.ASSERTED.value, 0)} by what you state rather than by evidence",
        f"{counts.get(Classification.GAP.value, 0)} not answered",
    ]
    return f"Of {len(spec.requirements)} extracted requirements: " + ", ".join(parts) + "."


def strongest(
    spec: JobSpec, evidence: EvidenceMap, inventory: CvInventory, limit: int = 3
) -> tuple[str, ...]:
    """Return the record's best answers to this posting, strongest first."""
    ranked = sorted(
        (
            match
            for match in evidence.matches
            if match.classification is Classification.DIRECT and match.entry_ids
        ),
        key=lambda match: (-match.score, match.requirement_id),
    )
    found: list[str] = []
    for match in ranked[:limit]:
        requirement = spec.requirement(match.requirement_id)
        try:
            text = inventory.by_id(match.entry_ids[0]).full_text
        except KeyError:  # pragma: no cover - the vocabulary is closed over the CV
            continue
        subject = requirement.text if requirement is not None else match.requirement_id
        found.append(
            f"{match.entry_ids[0]} ({match.strength.value}) answers "
            f"{table_cell(subject, CELL)}: {table_cell(text, CELL)}"
        )
    return tuple(found)


def gaps_of(spec: JobSpec, evidence: EvidenceMap) -> tuple[str, ...]:
    """Return requirements the posting states that the record does not answer."""
    found: list[str] = []
    for match in evidence.gaps():
        requirement = spec.requirement(match.requirement_id)
        if requirement is None:  # pragma: no cover - the map is built from this spec
            continue
        mark = "essential" if requirement.importance is Importance.MUST_HAVE else "preferred"
        found.append(f"{requirement.id} ({mark}): {table_cell(requirement.text, CELL)}")
    return tuple(found)


def screen_batch(
    requests: Sequence[Request],
    settings: Settings,
    *,
    master_cv: Path | None = None,
    api_key: str | None = None,
    prompter: Prompter | None = None,
    cache_enabled: bool | None = None,
    out: Path | None = None,
    verbose: bool = False,
    service_tier: ServiceTier = ServiceTier.STANDARD,
    ledgers: list[Ledger] | None = None,
    raise_if_cancelled: Callable[[], None] | None = None,
    intent: PostingReadIntent = PostingReadIntent.CRAWL,
) -> tuple[Batch, CvInventory]:
    """Screen every posting separately and return the batch and parsed record.

    ``intent`` is ``SINGLE_POSTING`` only when the person named these sources in this request.
    """
    _sized(requests)
    from clearscout.application.tailoring.pipeline import screen as screen_posting

    screenings: list[Screening] = []
    inventory: CvInventory | None = None
    for request in requests:
        if raise_if_cancelled is not None:
            raise_if_cancelled()
        ledger = ledger_for(settings.prices)
        if ledgers is not None:
            ledgers.append(ledger)
        result = screen_posting(
            request.source,
            settings,
            master_cv=master_cv,
            api_key=api_key,
            prompter=prompter,
            cache_enabled=cache_enabled,
            out=out,
            verbose=verbose,
            service_tier=service_tier,
            ledger=ledger,
            raise_if_cancelled=raise_if_cancelled,
            intent=intent,
            stored_name=request.stored_name,
        )
        inventory = result.inventory
        from clearscout.application.discovery.posting_identity import identify

        posting = identify(result.jd)
        screenings.append(
            Screening(
                request=request,
                key=_key(request, posting.identifier, {item.key for item in screenings}),
                spec=result.spec,
                evidence=result.evidence,
                posting_id=posting.identifier,
                read=result.jd.summary(),
                report=result.report,
                ledger=result.ledger,
            )
        )
    if inventory is None:  # pragma: no cover - guarded by _sized
        raise ConfigError("A batch needs at least one posting.")
    shared, opportunities = analyse(screenings, vocabulary_from=settings)
    return (
        Batch(
            screenings=tuple(screenings),
            shared=shared,
            opportunities=opportunities,
            recommendations={
                item.key: recommend(
                    item.spec,
                    item.evidence,
                    boundaries=settings.job_search.focus.stated_boundaries,
                    seniority=settings.job_search.seniority,
                )
                for item in screenings
            },
            started=datetime.now(UTC).isoformat(timespec="seconds"),
        ),
        inventory,
    )
