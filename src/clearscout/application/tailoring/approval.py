"""Let a person accept or reject proposed lines, without opening a way in.

Replacement text is not privileged: it goes through the verify stage's own
checks, called rather than copied. Lines the assembler copies from the master CV
are marked immutable, and a decision other than acceptance on one is refused.

Staleness is answered by digests rather than by timestamps, and the three
comparisons fail differently on purpose: the manifest against itself catches a
file edited after it was written, the manifest against a fresh review catches an
input that changed since, and the set digest catches a proposal added or removed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from clearscout.application.config import Settings
from clearscout.application.master_cv.cv import CvInventory
from clearscout.application.tailoring import changes
from clearscout.application.tailoring.changes import ACCEPT, INPUT_NAMES, REJECT, Proposal, Review
from clearscout.application.tailoring.guardrails import Guardrails
from clearscout.application.tailoring.stages import draft as draft_stage
from clearscout.application.tailoring.stages import verify as verify_stage
from clearscout.application.tailoring.stages.draft import DraftedLine, ResumeDraft
from clearscout.application.tailoring.stages.jobspec import JobSpec
from clearscout.application.tailoring.stages.select import Plan
from clearscout.shared.errors import ApprovalError
from clearscout.shared.results import Outcome

NO_ROUTE = (
    "Text a resume carries has to be supported by the master CV. The route for material\n"
    "  the CV does not yet hold is 'clearscout discover', which asks about it, refuses to\n"
    "  write anything you did not say, and adds what you confirm. Then run the posting\n"
    "  again."
)


class Replacement(BaseModel):
    """Text a reviewer supplied in place of a rejected line."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    source_ids: tuple[str, ...] = ()
    requirement_ids: tuple[str, ...] = ()


class Decision(BaseModel):
    """One proposal as the reviewer returned it.

    Every field the review wrote is carried back, so a digest recomputed from
    this object proves the reviewer's copy still says what the run said.
    ``extra="ignore"`` is deliberate: a manifest is a file a person edits, and
    refusing it over a comment key they added would stop them annotating it.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str
    slot: str = ""
    kind: str = ""
    immutable: bool = False
    text: str = ""
    evidence_ids: tuple[str, ...] = ()
    evidence: tuple[dict[str, str], ...] = ()
    requirement_ids: tuple[str, ...] = ()
    rationale: str = ""
    digest: str = ""
    decision: str = ACCEPT
    replacement: Replacement | None = None

    def content(self) -> dict[str, Any]:
        """Return the fields the digest covers, exactly as the review hashed them."""
        return {
            "id": self.id,
            "slot": self.slot,
            "kind": self.kind,
            "immutable": self.immutable,
            "text": self.text,
            "evidence_ids": list(self.evidence_ids),
            "evidence": [[item.get("source", ""), item.get("text", "")] for item in self.evidence],
            "requirement_ids": list(self.requirement_ids),
            "rationale": self.rationale,
        }


class Manifest(BaseModel):
    """An approval file: a reviewed ``changes.json`` with decisions written in."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    schema_version: int = Field(alias="schema")
    run: dict[str, Any] = Field(default_factory=dict)
    digest: str = ""
    proposals: tuple[Decision, ...] = ()

    def fingerprint(self) -> str:
        """Return a digest of the decisions, for the run identity."""
        return changes.digest(
            {
                "review": self.digest,
                "decisions": [
                    [item.id, item.digest, item.decision, _replacement_payload(item)]
                    for item in self.proposals
                ],
            }
        )


def _replacement_payload(decision: Decision) -> Any:
    """Return a replacement as JSON, or nothing when there is none."""
    if decision.replacement is None:
        return None
    return [
        decision.replacement.text,
        list(decision.replacement.source_ids),
        list(decision.replacement.requirement_ids),
    ]


def load(path: Path) -> Manifest:
    """Read an approval manifest, refusing anything that is not one."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ApprovalError(f"Cannot read the approval file {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ApprovalError(
            f"{path} is not valid JSON: {error}\n"
            "  An approval file is a copy of a run's changes.json with decisions edited in."
        ) from error
    return from_payload(payload, source=str(path), description="approval file")


def from_payload(
    payload: object,
    *,
    source: str = "approval payload",
    description: str = "approval manifest",
) -> Manifest:
    """Validate an already-loaded approval payload."""
    if not isinstance(payload, dict):
        raise ApprovalError(f"{source} does not contain an approval object.")
    try:
        return Manifest.model_validate(payload)
    except ValidationError as error:
        message = f"{source} is not a usable {description}.\n  {_first_problem(error)}"
        raise ApprovalError(message) from error


def _first_problem(error: ValidationError) -> str:
    """Return the first validation problem, in words rather than as a dump."""
    first = error.errors()[0]
    where = ".".join(str(part) for part in first.get("loc", ())) or "the file"
    return f"{where}: {first.get('msg', 'is not valid')}"


@dataclass(frozen=True)
class Applied:
    """What a validated approval did to the draft."""

    draft: ResumeDraft
    plan: Plan
    decisions: dict[str, str]
    rejected: tuple[str, ...]
    replaced: tuple[str, ...]
    dropped_roles: tuple[str, ...]
    verification: verify_stage.Report | None = None

    @property
    def changed(self) -> bool:
        """Return whether the approval altered the document at all."""
        return bool(self.rejected)

    def notes(self) -> list[str]:
        """Return what the run should say about the review it applied."""
        if not self.changed:
            return ["The approval accepted every proposed line, so the draft is unchanged."]
        supplied = (
            f", {len(self.replaced)} of them replaced with text you supplied, which "
            "passed\n  the same evidence checks the drafted text did"
            if self.replaced
            else " and omitted"
        )
        lines = [f"{len(self.rejected)} proposed line(s) were rejected under review{supplied}."]
        if self.dropped_roles:
            lines.append(
                f"{len(self.dropped_roles)} role(s) lost every line and were removed "
                "with their headings,\n  so the document carries no heading with nothing "
                "under it."
            )
        return lines


def decide(
    manifest: Manifest,
    review: Review,
    *,
    plan: Plan,
    spec: JobSpec,
    inventory: CvInventory,
    guardrails: Guardrails,
) -> dict[str, Decision]:
    """Validate one approval against a fresh review, or refuse it by name.

    Every refusal here is a refusal to write, not a finding in a report: a
    review whose subject has moved is a review of something else.
    """
    if manifest.schema_version != changes.SCHEMA:
        raise ApprovalError(
            f"That approval was written for review schema {manifest.schema_version}, "
            f"and this version writes schema {changes.SCHEMA}.\n"
            "  Re-run the posting, read the new changes.md, and approve that."
        )
    _same_run(manifest, review)
    proposals = review.by_id()
    decisions = {item.id: item for item in manifest.proposals}
    _same_membership(proposals, decisions)
    # Each line before the set, so the specific answer wins: once every proposal
    # is known, the set digest can only report the order they were reviewed in.
    for item in manifest.proposals:
        _one(item, proposals[item.id], review.run)
    _same_order(manifest, review, proposals)
    _replacements(manifest, plan=plan, spec=spec, inventory=inventory, guardrails=guardrails)
    return decisions


def _same_run(manifest: Manifest, review: Review) -> None:
    """Refuse an approval made against different inputs, naming each one."""
    changed = [
        INPUT_NAMES.get(key, key)
        for key, value in review.run.items()
        if manifest.run.get(key) != value
    ]
    if changed:
        raise ApprovalError(
            f"That approval was made against a different run: {', '.join(changed)} "
            "changed since.\n"
            "  Re-run the posting, read the new changes.md, and approve that."
        )


def _same_membership(proposals: dict[str, Proposal], decisions: dict[str, Decision]) -> None:
    """Refuse an approval that is not about this exact set of lines."""
    unknown = sorted(set(decisions) - set(proposals))
    if unknown:
        raise ApprovalError(
            f"That approval decides {len(unknown)} line(s) this run did not propose: "
            f"{', '.join(unknown[:5])}.\n"
            "  Re-run the posting and approve the changes.json it wrote."
        )
    unreviewed = sorted(set(proposals) - set(decisions))
    if unreviewed:
        raise ApprovalError(
            f"That approval does not decide {len(unreviewed)} line(s) this run proposed: "
            f"{', '.join(unreviewed[:5])}.\n"
            "  An approval covers the whole draft, so a line nobody saw is not accepted "
            "by default."
        )


def _same_order(manifest: Manifest, review: Review, proposals: dict[str, Proposal]) -> None:
    """Refuse an approval of the right lines read in the wrong order."""
    recomputed = changes.set_digest(
        review.run, tuple(proposals[item.id].digest for item in manifest.proposals)
    )
    if manifest.digest != recomputed:
        raise ApprovalError(
            "That approval covers the same lines in a different order than this run "
            "proposed them.\n"
            "  Re-run the posting and approve the changes.json it wrote."
        )


def _one(item: Decision, proposal: Proposal, run: dict[str, Any]) -> None:
    """Validate one decision against the proposal it claims to be about."""
    self_digest = changes.digest_of(run, item.content())
    if item.digest != self_digest:
        raise ApprovalError(
            f"The approval file was edited after it was written: {item.id} no longer "
            "matches its own digest.\n"
            "  A digest covers the text, the evidence, the requirements, and the "
            "rationale, so\n  one of those was changed by hand. Re-run the posting and "
            "approve a fresh review."
        )
    if item.digest != proposal.digest:
        raise ApprovalError(
            f"That approval is stale: {item.id} was reviewed as different text or "
            "different evidence\n  than this run proposes. Re-run the posting, read the "
            "new changes.md, and approve that."
        )
    if item.decision not in (ACCEPT, REJECT):
        raise ApprovalError(
            f"{item.id} has decision {item.decision!r}. Use {ACCEPT!r} or {REJECT!r}."
        )
    if item.decision == ACCEPT and item.replacement is not None:
        raise ApprovalError(
            f"{item.id} is accepted and also carries replacement text.\n"
            "  Reject a line to replace it; accepting one means keeping it as written."
        )
    if item.decision == REJECT:
        _rejectable(item, proposal)


def _rejectable(item: Decision, proposal: Proposal) -> None:
    """Refuse a rejection of something a review is not entitled to change."""
    if proposal.immutable:
        raise ApprovalError(
            f"{item.id} is copied from your master CV, not written for this posting: "
            f"{proposal.slot}.\n"
            "  Employers, titles, dates, education, and publication citations are not a "
            "model's\n  or a reviewer's to rewrite. Edit the master CV and run the "
            "posting again."
        )
    if proposal.kind is changes.Kind.OMITTED:
        raise ApprovalError(
            f"{item.id} is already omitted, so there is nothing on the page to reject: "
            f"{proposal.slot}."
        )


def _replacements(
    manifest: Manifest,
    *,
    plan: Plan,
    spec: JobSpec,
    inventory: CvInventory,
    guardrails: Guardrails,
) -> None:
    """Hold every supplied line to the checks the model's lines are held to.

    The checks are the verify stage's own, called rather than restated, so a rule
    that tightens there tightens here too.
    """
    supplied = [
        (item.id, item.replacement) for item in manifest.proposals if item.replacement is not None
    ]
    if not supplied:
        return
    known = {requirement.id for requirement in spec.requirements}
    for proposal_id, replacement in supplied:
        unknown = sorted(set(replacement.requirement_ids) - known)
        if unknown:
            raise ApprovalError(
                f"The replacement for {proposal_id} names requirement(s) this posting "
                f"does not have: {', '.join(unknown)}.\n"
                "  The requirement map in the report lists the ones it does."
            )
    lines = tuple(
        DraftedLine(text=replacement.text, source_ids=replacement.source_ids)
        for _, replacement in supplied
    )
    findings = verify_stage.check_supplied(
        lines=lines, plan=plan, inventory=inventory, guardrails=guardrails
    )
    failures = [finding for finding in findings if finding.outcome is Outcome.FAIL]
    if failures:
        detail = "\n".join(f"  {finding.check}: {finding.detail}" for finding in failures)
        raise ApprovalError(
            "Replacement text is held to the same evidence checks the model's text is, "
            "and this failed:\n"
            f"{detail}\n  {NO_ROUTE}"
        )


def apply(
    decisions: dict[str, Decision], review: Review, draft: ResumeDraft, plan: Plan
) -> Applied:
    """Return the draft and the assembly plan a validated approval leaves behind.

    Rejection removes; it never substitutes. A block that loses everything is
    removed with its heading rather than printed empty.
    """
    proposals = review.by_id()
    edits: dict[str, DraftedLine | None] = {}
    rejected: list[str] = []
    replaced: list[str] = []
    for proposal_id, item in decisions.items():
        if item.decision != REJECT:
            continue
        rejected.append(proposal_id)
        slot = proposals[proposal_id].slot
        if item.replacement is None:
            edits[slot] = None
            continue
        replaced.append(proposal_id)
        edits[slot] = DraftedLine(
            text=item.replacement.text, source_ids=item.replacement.source_ids
        )
    rebuilt = changes.rebuild(draft, edits)
    dropped = changes.emptied_roles(rebuilt, plan)
    return Applied(
        draft=rebuilt,
        plan=_without(plan, dropped),
        decisions={key: item.decision for key, item in decisions.items()},
        rejected=tuple(sorted(rejected)),
        replaced=tuple(sorted(replaced)),
        dropped_roles=dropped,
    )


def applied(
    manifest: Manifest,
    review: Review,
    draft: ResumeDraft,
    plan: Plan,
    *,
    inventory: CvInventory,
    spec: JobSpec,
    settings: Settings,
    guardrails: Guardrails,
    excluded: frozenset[str],
    verification: verify_stage.Report,
) -> tuple[Applied, draft_stage.Document]:
    """Validate an approval, apply it, and check what it left behind.

    Every deterministic check runs again on the document that will actually be
    written, so nothing reaches a file without passing what the draft passed.
    Returns what the approval did and that document.
    """
    decisions = decide(
        manifest, review, plan=plan, spec=spec, inventory=inventory, guardrails=guardrails
    )
    result = apply(decisions, review, draft, plan)
    document = draft_stage.Document(
        result.draft, inventory, result.plan, settings, guardrails, excluded
    )
    markdown = document.markdown()
    checked = verify_stage.check(
        markdown=markdown,
        draft=document.printed(),
        # The original plan: rejecting a role's lines does not withdraw that role's
        # evidence from what a citation may name.
        plan=plan,
        inventory=inventory,
        spec=spec,
        guardrails=guardrails,
        excluded=excluded,
    )
    critiqued = _with_critic(checked, result, verification, markdown)
    return replace(result, verification=critiqued), document


def _with_critic(
    checked: verify_stage.Report,
    result: Applied,
    verification: verify_stage.Report,
    markdown: str,
) -> verify_stage.Report:
    """Carry the critic's verdict forward only where it still describes the text.

    Removing a line cannot introduce an unsupported claim, so the verdict holds
    for a subset of the draft; supplied text the critic never saw voids it.
    """
    if result.replaced:
        return checked.without_critic(
            "A reviewer supplied replacement text, and the critic did not read it, "
            "so this resume was not critiqued."
        )
    if verification.critic is None:
        skipped = (
            finding.detail
            for finding in verification.findings
            if finding.check == verify_stage.CRITIC_CHECK
        )
        return checked.without_critic(next(skipped, "No critique was asked for this draft."))
    return checked.with_critic(
        verification.critic, draws=verification.critic_draws, document=markdown
    )


def _without(plan: Plan, dropped: tuple[str, ...]) -> Plan:
    """Return the plan assembly should use once a role has lost every line.

    Only the assembly plan. The original still governs what may be cited, so
    rejecting one bullet does not remove that role's other evidence from the
    vocabulary a replacement may draw on.
    """
    if not dropped:
        return plan
    kept = tuple(key for key in plan.recent_roles if key not in dropped)
    if not kept and not plan.earlier_roles:
        raise ApprovalError(
            "Rejecting those lines would leave the resume with no experience section.\n"
            "  A document with nothing under Experience is not a resume, so this is "
            "refused\n  rather than written. Keep at least one line, or re-run without "
            "the approval."
        )
    return plan.model_copy(update={"recent_roles": kept})
