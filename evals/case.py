"""What an evaluation case is: inputs, a scripted model, and declared expectations.

A case is a TOML file a reviewer reads. It holds the inputs, the model responses
written as data, ``[evidence]`` aliases for CV entries whose identifiers are
content hashes, and ``[expect]``, the invariants the case asserts. A violation
of an expectation is a defect; a moved baseline is a diff a reviewer judges.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from clearscout.application.master_cv.cv import CvInventory
from clearscout.application.tailoring.guardrails import Guardrails
from clearscout.application.tailoring.stages.jobspec import Dimension, Importance
from clearscout.application.tailoring.stages.match import Classification, Strength
from clearscout.domain import ClaimBoundary
from clearscout.domain.requirements import GateKind

CASES = Path(__file__).parent / "cases"
CVS = Path(__file__).parent / "cvs"

# An alias starting with this is a literal entry identifier rather than text
# to search for, which is how a case writes a model citing absent evidence.
LITERAL_ID = "id:"


class Frozen(BaseModel):
    """Base for every case model: immutable, and ``extra="forbid"`` so a misspelled key fails."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Mode(StrEnum):
    """How far through the pipeline a case runs; SCREEN stops after matching, DRAFT runs on."""

    SCREEN = "screen"
    DRAFT = "draft"


class ScriptedRequirement(Frozen):
    """One requirement as the scripted extraction model reports it."""

    text: str
    importance: Importance
    dimension: Dimension = Dimension.HARD


class ScriptedGate(Frozen):
    """One hard gate as the scripted extraction model reports it."""

    kind: GateKind
    text: str
    blocking: bool = False


class ScriptedSpec(Frozen):
    """The scripted answer to the extraction stage."""

    title: str
    company: str | None = None
    seniority: str | None = None
    location: str | None = None
    requirements: tuple[ScriptedRequirement, ...] = ()
    keywords: tuple[str, ...] = ()
    hard_gates: tuple[ScriptedGate, ...] = ()


class ScriptedMatch(Frozen):
    """The scripted answer for one requirement.

    ``requirement`` is the requirement's text, matched against the scripted
    extraction above. Identifiers are assigned by the program in order, so a
    case that named them would break the moment a requirement was inserted.
    """

    requirement: str
    classification: Classification
    strength: Strength = Strength.NONE
    evidence: tuple[str, ...] = ()
    note: str = ""


class ScriptedLine(Frozen):
    """One drafted line and the evidence aliases it derives from."""

    text: str
    evidence: tuple[str, ...] = ()


class ScriptedSkill(Frozen):
    """One drafted skills line."""

    label: str
    text: str
    evidence: tuple[str, ...] = ()


class ScriptedRole(Frozen):
    """The scripted prose for one role, named by a substring of the employer's name."""

    role: str
    context: ScriptedLine | None = None
    bullets: tuple[ScriptedLine, ...] = ()


class ScriptedDraft(Frozen):
    """The scripted answer to the drafting stage."""

    summary: ScriptedLine
    skills: tuple[ScriptedSkill, ...] = ()
    roles: tuple[ScriptedRole, ...] = ()
    earlier_career: ScriptedLine | None = None


class ScriptedCoverLetter(Frozen):
    """The scripted answer to the cover-letter stage."""

    paragraphs: tuple[ScriptedLine, ...] = ()


class ScriptedDoubt(Frozen):
    """One claim a scripted critic doubts, and the boundary it says the claim crosses."""

    claim: str
    boundary: ClaimBoundary


class ScriptedCritic(Frozen):
    """The scripted critic verdict.

    Scripting this proves the wiring between a critic's answer and the run's
    verdict. It proves nothing about a critic's judgment, which is what a live
    run would measure and what this cannot.
    """

    alignment: int = 5
    preservation: int = 5
    specificity: int = 5
    readability: int = 5
    ats_clarity: int = 5
    human_voice: int = 5
    unsupported_claims: tuple[ScriptedDoubt, ...] = ()
    style_flags: tuple[str, ...] = ()
    weaknesses: tuple[str, ...] = ()
    guidance: str = ""


class ScriptedReplacement(Frozen):
    """Text a scripted reviewer offers for a rejected line, citing aliases or ``id:`` literals."""

    text: str
    evidence: tuple[str, ...] = ()


class ScriptedDecision(Frozen):
    """One decision a scripted reviewer returns.

    ``slot`` is matched against the end of a proposal's slot, so a case names
    ``bullet[0]`` rather than a role key derived from the CV.
    """

    slot: str
    decision: str = "reject"
    replacement: ScriptedReplacement | None = None


class ScriptedDiscovery(Frozen):
    """A person, scripted, answering questions about a gap.

    Discovery is the one path where what a person says becomes a claim, so a
    case scripts what they said and what was then proposed from it.
    ``forbidden`` names sources a reply may not come from; each is tried with the
    same wording, and what is measured is that every one was refused.
    """

    about: str
    replies: tuple[str, ...] = ()
    claim: str = ""
    scope: str = ""
    # Matched against the end of a role key, so a case names the employer a
    # reader can see in the CV rather than a derived slug.
    placement: str = ""
    ambiguous: str = ""
    forbidden: tuple[str, ...] = ()
    gap: str = ""


class ScriptedPosting(Frozen):
    """One fetched posting a semantic-review batch is asked to classify."""

    id: str
    employer: str
    title: str
    location: str = ""
    body: str


class ScriptedRelevanceReview(Frozen):
    """The scripted answer for one posting in a relevance-review batch.

    ``posting`` is the scripted posting's identifier, and ``spans`` are 1-based
    positions in that posting's deterministic spans. Naming a span position that
    belongs to another posting is how a case writes a citation the program must
    reject.
    """

    posting: str
    # The posting whose spans ``spans`` indexes, when that is not this result's
    # own. The only way to write an answer citing another posting in the batch.
    span_owner: str = ""
    recommendation: str = "review"
    summary: str = ""
    core_work_alignment: str = "direct"
    core_qualification_alignment: str = "established"
    hard_condition: str = "passes"
    decisive_basis: str = "core_match"
    spans: tuple[int, ...] = (1,)
    evidence: tuple[str, ...] = ()
    unresolved_questions: tuple[str, ...] = ()


class RelevanceReview(Frozen):
    """One scripted relevance-review batch: its search context, postings, answer."""

    search_context: str
    postings: tuple[ScriptedPosting, ...]
    results: tuple[ScriptedRelevanceReview, ...] = ()


class ScriptedVision(Frozen):
    """A screenshot and the scripted transcript a vision-capable model reads from it."""

    image_base64: str
    transcript: str


class ScriptedSourcedField(Frozen):
    """One extracted CV-import field: its text and the 1-based source lines it cites."""

    text: str
    lines: tuple[int, ...] = ()


class ScriptedImportedRole(Frozen):
    """One position as the scripted CV-import extraction reports it."""

    organisation: ScriptedSourcedField
    title: ScriptedSourcedField | None = None
    title_of_record: ScriptedSourcedField | None = None
    dates: ScriptedSourcedField | None = None
    location: ScriptedSourcedField | None = None
    context: tuple[ScriptedSourcedField, ...] = ()
    bullets: tuple[ScriptedSourcedField, ...] = ()


class ScriptedDropped(Frozen):
    """One source line the scripted CV-import extraction deliberately left out."""

    line: int
    reason: str


class ScriptedStructure(Frozen):
    """A synthetic uploaded CV, and the scripted extraction of it.

    ``source`` is read as plain text the same way the importer reads a ``.md``
    or ``.txt`` upload, so its non-blank lines are numbered exactly as the
    scripted fields below must cite them.
    """

    source: str
    name: ScriptedSourcedField
    contact: tuple[ScriptedSourcedField, ...] = ()
    summary: tuple[ScriptedSourcedField, ...] = ()
    skills: tuple[ScriptedSourcedField, ...] = ()
    roles: tuple[ScriptedImportedRole, ...] = ()
    education: tuple[ScriptedSourcedField, ...] = ()
    publications: tuple[ScriptedSourcedField, ...] = ()
    dropped: tuple[ScriptedDropped, ...] = ()


class ScriptedProposedTitle(Frozen):
    """One title a search-vocabulary case scripts the model proposing."""

    title: str
    reason: str = ""
    evidence: tuple[str, ...] = ()
    lane: str = ""


class ScriptedSearchVocabulary(Frozen):
    """A scripted vocabulary answer and the search state whose exclusions the gate must apply."""

    excluded_titles: tuple[str, ...] = ()
    titles: tuple[ScriptedProposedTitle, ...] = ()


class ScriptedHostedSource(Frozen):
    """One URL a scripted hosted search claims to have read."""

    url: str
    title: str = ""


class ScriptedHostedSearch(Frozen):
    """One scripted hosted search: the queries asked, and what it read."""

    queries: tuple[str, ...] = ()
    sources: tuple[ScriptedHostedSource, ...] = ()


class ScriptedPostingRecoveryObservation(Frozen):
    """One URL-scoped observation a scripted posting-recovery answer reports."""

    url: str
    authority: str = "publisher_body"
    completeness: str = "complete"
    identity: str = "exact"
    posting_status: str = "active"
    note: str = "Matches the known posting on employer and title."


class ScriptedPostingRecovery(Frozen):
    """A scripted posting-recovery answer, and the hosted search behind it.

    Drives the pure identity-check and candidate-validation path, stopping
    before ``resolver.resolve``, which reads a live page.
    """

    employer: str = "Example Employer"
    title: str = "Staff Platform Engineer"
    known_url: str = "https://boards.example.test/postings/original"
    search: ScriptedHostedSearch
    candidate_urls: tuple[str, ...] = ()
    observations: tuple[ScriptedPostingRecoveryObservation, ...] = ()


class ScriptedCampaignPick(Frozen):
    """One URL a scripted guided-campaign answer selects, as a posting or a board."""

    url: str
    why: str = ""
    board: bool = False


class ScriptedGuidedCampaign(Frozen):
    """One captured campaign attempt, credited by the shipped runner on a synthetic manifest."""

    search: ScriptedHostedSearch
    picks: tuple[ScriptedCampaignPick, ...] = ()


class Responses(Frozen):
    """Every scripted model answer this case supplies, by stage."""

    jobspec: ScriptedSpec
    match: tuple[ScriptedMatch, ...] = ()
    draft: ScriptedDraft | None = None
    critic: ScriptedCritic | None = None
    cover_letter: ScriptedCoverLetter | None = None


class Expectations(Frozen):
    """The invariants a case asserts. Anything omitted is not asserted.

    Every field is a claim about the program rather than about the model, which
    is the only kind of claim a scripted run can support.
    """

    requirements: tuple[str, ...] = ()
    must_have: tuple[str, ...] = ()
    nice_to_have: tuple[str, ...] = ()
    hard_gates: tuple[str, ...] = ()
    blocking_gates: tuple[str, ...] = ()
    # Gates found in the posting that must not block, such as a conditional clause.
    nonblocking_gates: tuple[str, ...] = ()
    classifications: dict[str, Classification] = Field(default_factory=dict)
    strengths: dict[str, Strength] = Field(default_factory=dict)
    gaps: tuple[str, ...] = ()
    citable: tuple[str, ...] = ()
    uncitable: tuple[str, ...] = ()
    immutable: tuple[str, ...] = ()
    absent: tuple[str, ...] = ()
    injection_markers: tuple[str, ...] = ()
    # Structural rather than behavioral: a scripted model cannot show that a
    # boundary changes an answer, only that the phrase was wrapped.
    bounded: tuple[str, ...] = ()
    posting_only: tuple[str, ...] = ()
    checks: dict[str, str] = Field(default_factory=dict)
    rejected_stage: str = ""
    # "not stated" is a value like any other, and is what a negative control
    # asserts.
    derived: dict[str, str] = Field(default_factory=dict)
    approval: str = ""
    discovery: str = ""
    posting_recovery: str = ""
    # Several rules all end at "response_refused", so a case that means one of them
    # names a phrase from its refusal.
    posting_recovery_refusal: str = ""
    distinct_from: tuple[str, ...] = ()
    # Batch screening compares postings, so a case about it needs more than one.
    batch: tuple[str, ...] = ()
    # What proves postings are not combined is that no prompt built for one of
    # them contains another's marker.
    batch_marker: str = ""
    # Every count defaults to zero, which is what a clean run finds, so this is
    # where a case states that it deliberately produced something.
    measured: dict[str, int] = Field(default_factory=dict)


class Case(Frozen):
    """One evaluation case, loaded from one TOML file."""

    name: str
    proves: str
    mode: Mode = Mode.DRAFT
    cv: str = "platform_engineer.md"
    pages: int = 2
    posting: str
    evidence: dict[str, str] = Field(default_factory=dict)
    # Status lines the applicant states, carried as the CV's contact lines are.
    asserted: tuple[str, ...] = ()
    rules: Guardrails = Field(default_factory=Guardrails)
    responses: Responses
    approval: tuple[ScriptedDecision, ...] = ()
    discovery: ScriptedDiscovery | None = None
    relevance_review: RelevanceReview | None = None
    vision: ScriptedVision | None = None
    structure: ScriptedStructure | None = None
    search_vocabulary: ScriptedSearchVocabulary | None = None
    posting_recovery: ScriptedPostingRecovery | None = None
    guided_campaign: ScriptedGuidedCampaign | None = None
    expect: Expectations = Field(default_factory=Expectations)

    @property
    def cv_path(self) -> Path:
        """Return the master CV this case runs against."""
        return CVS / self.cv


class CaseError(Exception):
    """A case file is malformed, or names something that is not in the CV."""


def resolve_evidence(
    aliases: Mapping[str, str], inventory: CvInventory, where: str
) -> dict[str, str]:
    """Turn readable evidence aliases into entry identifiers, one entry per alias.

    Entry identifiers are content hashes, so an alias names a phrase instead.
    The phrase must appear in exactly one entry, which stops a case from quietly
    meaning something else after the CV is edited.
    """
    resolved: dict[str, str] = {}
    for alias, needle in aliases.items():
        if needle.startswith(LITERAL_ID):
            resolved[alias] = needle[len(LITERAL_ID) :]
            continue
        found = [entry.id for entry in inventory.entries if needle in entry.full_text]
        if len(found) != 1:
            raise CaseError(
                f"{where}: evidence alias {alias!r} matches {len(found)} entries; "
                "it must match exactly one"
            )
        resolved[alias] = found[0]
    return resolved


def load(path: Path) -> Case:
    """Read one case file, failing with the file name attached."""
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise CaseError(f"{path.name}: {error}") from error
    try:
        case = Case.model_validate(payload)
    except ValidationError as error:
        raise CaseError(f"{path.name}: {error}") from error
    if case.name != path.stem:
        raise CaseError(f"{path.name}: declares name {case.name!r}, which is not the file name")
    return case


def load_all(directory: Path = CASES) -> tuple[Case, ...]:
    """Read every case, in file-name order, so a run is reproducible."""
    return tuple(load(path) for path in sorted(directory.glob("*.toml")))
