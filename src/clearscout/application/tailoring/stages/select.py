"""Choose what goes on the page. Deterministic Python, no model involved.

Selection is arithmetic over the evidence map: a requirement's importance times
how strongly a piece of evidence answers it, adjusted for recency. Full bullets
cover the recent years and earlier roles collapse to a short block, and nothing
ranks one kind of evidence above another.

The output is a plan rather than a document: a fixed set of always-included
items plus ``drop_order``, an ordered list of optional ones from first
sacrificed to last, so trimming is a re-assembly and not another drafting call.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from clearscout.application.config import Settings
from clearscout.application.master_cv.cv import CvInventory, Entry, EntryKind, Role
from clearscout.application.tailoring.layout import (
    CONTEXT_LINES,
    SECTION_LINES,
    SKILLS_LINES,
    SUMMARY_LINES,
    Budget,
    markdown_lines,
    paragraph_lines,
    wrapped_lines,
)
from clearscout.application.tailoring.stages.context import (
    earlier_heading,
    header_markdown,
    role_heading,
)
from clearscout.application.tailoring.stages.jobspec import Importance, JobSpec, Requirement
from clearscout.application.tailoring.stages.match import EvidenceMap, Match


class TopUpStop(StrEnum):
    """Why the top-up pass stopped adding cited bullets."""

    BUDGET = "budget"
    EVIDENCE = "evidence"


class Plan(BaseModel):
    """What the derivative may contain, and the order in which it gives ground."""

    model_config = ConfigDict(frozen=True)

    summary_ids: tuple[str, ...] = ()
    skill_ids: tuple[str, ...] = ()
    recent_roles: tuple[str, ...] = ()
    earlier_roles: tuple[str, ...] = ()
    role_context: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    role_bullets: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    education_ids: tuple[str, ...] = ()
    publication_ids: tuple[str, ...] = ()
    # Master-text lines the selected experience bullets cost.
    spent_lines: int = 0
    unspent_lines: int = 0
    # Lines must-have placement spent past the selection budget.
    overshoot_lines: int = 0
    # Cited bullets left off roles at the cap that the lines left after selection fit together.
    withheld_by_cap: tuple[str, ...] = ()
    drop_order: tuple[str, ...] = ()
    scores: dict[str, float] = Field(default_factory=dict)
    requirements_for: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    must_haves: tuple[str, ...] = ()
    # The page budget in lines, and the estimate of every block but experience bullets.
    page_lines: float = 0.0
    fixed_lines: dict[str, float] = Field(default_factory=dict)
    # Master-text bullet lines selection could spend, and each recent role's first share.
    selection_lines: int = 0
    allowances: dict[str, int] = Field(default_factory=dict)
    passed_cap: int = 0
    top_up_stop: TopUpStop | None = None

    def all_ids(self) -> tuple[str, ...]:
        """Return every entry the drafting stage is allowed to see, in page order."""
        copied = (*self.education_ids, *self.publication_ids)
        return tuple(dict.fromkeys((*self.drafted_ids(), *copied)))

    def drafted_ids(self) -> tuple[str, ...]:
        """Return the entries a drafting prompt shows as evidence, leaving out copied ones."""
        ids: list[str] = [*self.summary_ids, *self.skill_ids]
        for role_key in (*self.recent_roles, *self.earlier_roles):
            ids.extend(self.role_context.get(role_key, ()))
            ids.extend(self.role_bullets.get(role_key, ()))
        return tuple(dict.fromkeys(ids))

    def without(self, excluded: frozenset[str]) -> Plan:
        """Return the plan as the page prints it, with ``excluded`` bullets and papers gone."""
        if not excluded:
            return self
        return self.model_copy(
            update={
                "role_bullets": {
                    key: tuple(entry_id for entry_id in ids if entry_id not in excluded)
                    for key, ids in self.role_bullets.items()
                },
                "publication_ids": tuple(
                    entry_id for entry_id in self.publication_ids if entry_id not in excluded
                ),
            }
        )


# Diminish repeated evidence by thirds.
COVERAGE_DECAY = 1 / 3


def build_plan(
    inventory: CvInventory,
    spec: JobSpec,
    evidence: EvidenceMap,
    settings: Settings,
    today: date | None = None,
) -> Plan:
    """Score every entry against the evidence map and lay out the page budget."""
    scores, requirements_for = _score(inventory, spec, evidence, today)
    recent, earlier = _split_roles(inventory, settings, today)
    recent, earlier = _include_older_answers(inventory, spec, requirements_for, recent, earlier)

    role_bullets: dict[str, tuple[str, ...]] = {}
    role_context: dict[str, tuple[str, ...]] = {}
    # Reserve publication lines before allocating the remaining budget.
    publications = _relevant_publications(inventory, scores, settings)
    page = Budget(settings.pages)
    fixed = _fixed_lines(inventory, settings, recent, earlier, publications)
    budget = page.selection_lines(sum(fixed.values()))

    allowances = _allocate(
        _role_strengths(inventory, scores, recent), [role.key for role in recent], budget
    )

    for role in recent:
        role_bullets[role.key] = _best_bullets(
            inventory,
            role,
            scores,
            requirements_for,
            settings,
            allowances.get(role.key, settings.min_bullets_per_role),
            budget,
        )
        role_context[role.key] = _best_context(inventory, role, scores)

    # Distribute leftover lines after rounding role allocations down.
    stop = _top_up(inventory, scores, requirements_for, settings, recent, role_bullets, budget)
    must_haves = _cover_must_haves(
        inventory,
        spec,
        scores,
        requirements_for,
        settings,
        recent,
        role_bullets,
        budget,
        evidence=evidence,
        today=today,
    )
    spent = _spent_lines(inventory, role_bullets)
    withheld = _withheld_by_cap(inventory, scores, settings, recent, role_bullets, budget - spent)

    for role in earlier:
        role_context[role.key] = _first_context(inventory, role)
        role_bullets[role.key] = ()

    education = tuple(entry.id for entry in inventory.of_kind(EntryKind.EDUCATION))
    summary = tuple(entry.id for entry in inventory.of_kind(EntryKind.SUMMARY))
    skills = tuple(entry.id for entry in inventory.of_kind(EntryKind.SKILL))

    return Plan(
        summary_ids=summary,
        skill_ids=skills,
        recent_roles=tuple(role.key for role in recent),
        earlier_roles=tuple(role.key for role in earlier),
        role_context=role_context,
        role_bullets=role_bullets,
        education_ids=education,
        publication_ids=publications,
        spent_lines=spent,
        unspent_lines=max(0, budget - spent),
        overshoot_lines=max(0, spent - budget),
        withheld_by_cap=withheld,
        drop_order=_drop_order(
            publications,
            education,
            role_bullets,
            recent,
            scores,
            settings,
            requirements_for,
            must_haves,
        ),
        scores=scores,
        requirements_for=requirements_for,
        must_haves=must_haves,
        page_lines=page.total_lines,
        fixed_lines=fixed,
        selection_lines=budget,
        allowances=allowances,
        passed_cap=sum(
            max(0, len(ids) - settings.max_bullets_per_role) for ids in role_bullets.values()
        ),
        top_up_stop=stop,
    )


def _role_strengths(
    inventory: CvInventory, scores: dict[str, float], recent: list[Role]
) -> dict[str, float]:
    """Return each recent role's total bullet score, which decides its share of the budget."""
    return {
        role.key: sum(
            scores.get(entry.id, 0.0)
            for entry in inventory.entries_for(role.key)
            if entry.kind is EntryKind.BULLET
        )
        for role in recent
    }


def _best_bullets(
    inventory: CvInventory,
    role: Role,
    scores: dict[str, float],
    requirements_for: dict[str, tuple[str, ...]],
    settings: Settings,
    allowance: int,
    budget: int,
) -> tuple[str, ...]:
    """Return the bullets this role's allowance buys, back in document order."""
    bullets = [
        entry
        for entry in inventory.entries_for(role.key)
        if entry.kind is EntryKind.BULLET and scores.get(entry.id, 0.0) > 0
    ]
    bullets.sort(key=lambda entry: (-scores.get(entry.id, 0.0), entry.order))
    kept = _within(
        bullets,
        allowance,
        scores,
        requirements_for,
        settings.max_bullets_per_role,
        budget,
    )
    kept.sort(key=lambda entry: entry.order)
    return tuple(entry.id for entry in kept)


def _best_context(inventory: CvInventory, role: Role, scores: dict[str, float]) -> tuple[str, ...]:
    """Return the two context lines of a recent role that answer the posting best."""
    context = [
        entry for entry in inventory.entries_for(role.key) if entry.kind is EntryKind.ROLE_CONTEXT
    ]
    context.sort(key=lambda entry: (-scores.get(entry.id, 0.0), entry.order))
    return tuple(entry.id for entry in context[:2])


def _first_context(inventory: CvInventory, role: Role) -> tuple[str, ...]:
    """Return the opening context line of a collapsed role, in document order."""
    return tuple(
        entry.id
        for entry in inventory.entries_for(role.key)
        if entry.kind is EntryKind.ROLE_CONTEXT
    )[:1]


def _fixed_lines(
    inventory: CvInventory,
    settings: Settings,
    recent: list[Role],
    earlier: list[Role],
    publications: tuple[str, ...],
) -> dict[str, float]:
    """Return the estimated lines of every block on the page except experience bullets.

    Copied text is measured in the Markdown assembly writes for it. Drafted prose
    does not exist yet, so it is charged at what drafts of it rendered.
    """
    education = inventory.of_kind(EntryKind.EDUCATION)
    skills = bool(inventory.of_kind(EntryKind.SKILL))
    contexts = sum(
        1
        for role in recent
        if any(entry.kind is EntryKind.ROLE_CONTEXT for entry in inventory.entries_for(role.key))
    )
    headings = [role_heading(role) for role in recent] + [earlier_heading(earlier)]
    header = header_markdown(inventory, settings.header_drop, settings.rules.work_authorization)
    return {
        "header": markdown_lines(header),
        "sections": SECTION_LINES * (2 + skills + bool(education) + bool(publications)),
        "summary": SUMMARY_LINES,
        "skills": SKILLS_LINES if skills else 0.0,
        "roles": sum(markdown_lines(heading) for heading in headings if heading),
        "role_context": CONTEXT_LINES * (contexts + (1 if earlier else 0)),
        "education": sum(paragraph_lines((entry.text, *entry.details)) for entry in education),
        "publications": (
            paragraph_lines(
                line
                for entry_id in publications
                for line in (inventory.by_id(entry_id).text, *inventory.by_id(entry_id).details)
            )
            if publications
            else 0.0
        ),
    }


def _within(
    bullets: list[Entry],
    allowance: int,
    scores: dict[str, float],
    requirements_for: dict[str, tuple[str, ...]],
    cap: int,
    budget: int,
) -> list[Entry]:
    """Fill the allowance with the set that answers the most of the posting.

    Lines rather than bullets, because a two-line bullet costs twice what a
    one-line bullet costs. Each bullet is taken for what it adds rather than for
    what it is worth alone, so breadth competes with depth instead of losing to
    it: ranking by raw score fills the page with five answers to one must-have.
    """
    kept: list[Entry] = []
    spent = 0
    answered: dict[str, int] = defaultdict(int)
    remaining = list(bullets)

    while remaining and len(kept) < cap:
        best = max(
            remaining, key=lambda entry: _marginal(entry, scores, requirements_for, answered)
        )
        cost = wrapped_lines(best.full_text)
        # A role's strongest bullet is admitted past its per-role share when the
        # page budget still has room; otherwise a bullet taller than the
        # allowance drops and leaves the role empty.
        if spent + cost > allowance and (kept or cost > budget):
            break
        remaining.remove(best)
        kept.append(best)
        spent += cost
        for requirement_id in requirements_for.get(best.id, ()):
            answered[requirement_id] += 1
    return kept


def _marginal(
    entry: Entry,
    scores: dict[str, float],
    requirements_for: dict[str, tuple[str, ...]],
    answered: dict[str, int],
) -> float:
    """Return what this bullet adds given what has already been selected.

    Each requirement's share of the score decays by ``COVERAGE_DECAY`` for every
    already-selected bullet answering it. A bullet citing nothing keeps its raw
    score, so it can still be chosen when nothing better remains.
    """
    cited = requirements_for.get(entry.id, ())
    score = scores.get(entry.id, 0.0)
    if not cited:
        return score
    share = score / len(cited)
    return sum(share * (COVERAGE_DECAY ** answered[requirement_id]) for requirement_id in cited)


# A must-have whose bullet has a digit is nudged ahead of a digitless rival. At
# 1.2 this 20% bump is not a tie-breaker: a weaker match can overtake a stronger
# one (0.90 * 1.2 = 1.08 > 1.0), and "24/7" or "5 years" count as digits too.
MEASURED_OUTCOME_BONUS = 1.2

# Promote a stronger unselected answer over an in-set one only when it wins by
# more than float noise; a smaller gap is churn the trim pass would undo anyway.
PROMOTION_MARGIN = 1e-9


def _score(
    inventory: CvInventory,
    spec: JobSpec,
    evidence: EvidenceMap,
    today: date | None,
) -> tuple[dict[str, float], dict[str, tuple[str, ...]]]:
    """Return each entry's weight and the requirements it was cited against.

    An entry cited for three must-haves outscores one cited for a single nice
    to have, and recent work outscores identical older work.
    """
    scores: dict[str, float] = defaultdict(float)
    requirements: dict[str, list[str]] = defaultdict(list)
    roles = {role.key: role for role in inventory.roles}

    for item in evidence.matches:
        requirement = spec.requirement(item.requirement_id)
        if requirement is None or not _usable(item):
            continue
        contribution = requirement.weight * item.score
        for entry_id in item.entry_ids:
            entry = inventory.entry(entry_id)
            if entry is None:
                continue
            scores[entry_id] += (
                contribution
                * _outcome_bonus(requirement, entry)
                * _recency(roles.get(entry.role_key or ""), today)
            )
            requirements[entry_id].append(requirement.id)

    return dict(scores), {key: tuple(dict.fromkeys(value)) for key, value in requirements.items()}


def _usable(item: Match) -> bool:
    # A zero-strength row and a stated fact both cite nothing a draft may use.
    return item.score != 0 and item.may_support_a_claim


def _outcome_bonus(requirement: Requirement, entry: Entry) -> float:
    """Return the measured-outcome multiplier for this entry against a must-have."""
    if requirement.importance is Importance.MUST_HAVE and any(
        character.isdigit() for character in entry.text
    ):
        return MEASURED_OUTCOME_BONUS
    return 1.0


def _recency(role: Role | None, today: date | None) -> float:
    """Return a gentle recency multiplier, so an old answer to a must-have can still win."""
    if role is None:
        return 1.0
    return 1.0 / (1.0 + role.years_since_end(today) / 10.0)


def _split_roles(
    inventory: CvInventory, settings: Settings, today: date | None
) -> tuple[list[Role], list[Role]]:
    """Split roles into the ones that keep full bullets and the ones that collapse."""
    ordered = sorted(
        inventory.roles,
        key=lambda role: (role.current, role.end_year or 0, role.start_year or 0),
        reverse=True,
    )
    recent = [role for role in ordered if role.years_since_end(today) <= settings.full_bullet_years]
    earlier = [role for role in ordered if role not in recent]
    return recent, earlier


def _include_older_answers(
    inventory: CvInventory,
    spec: JobSpec,
    requirements_for: dict[str, tuple[str, ...]],
    recent: list[Role],
    earlier: list[Role],
) -> tuple[list[Role], list[Role]]:
    """Keep older must-have answers eligible for the same bounded bullet allocation."""
    must_haves = {item.id for item in spec.requirements if item.importance is Importance.MUST_HAVE}
    promoted = [
        role
        for role in earlier
        if any(
            entry.kind is EntryKind.BULLET
            and must_haves.intersection(requirements_for.get(entry.id, ()))
            for entry in inventory.entries_for(role.key)
        )
    ]
    return [*recent, *promoted], [role for role in earlier if role not in promoted]


# A role with a heading and no bullets reads as a gap in the record.
FLOOR_LINES = 3


def _answered_counts(
    requirements_for: dict[str, tuple[str, ...]], role_bullets: dict[str, tuple[str, ...]]
) -> dict[str, int]:
    """Return how many selected bullets already answer each requirement."""
    answered: dict[str, int] = defaultdict(int)
    for ids in role_bullets.values():
        for entry_id in ids:
            for requirement_id in requirements_for.get(entry_id, ()):
                answered[requirement_id] += 1
    return answered


def _unselected_bullets(
    inventory: CvInventory,
    scores: dict[str, float],
    recent: list[Role],
    role_bullets: dict[str, tuple[str, ...]],
) -> list[Entry]:
    """Return the scoring bullets of recent roles that are not on the page."""
    return [
        entry
        for role in recent
        for entry in inventory.entries_for(role.key)
        if entry.kind is EntryKind.BULLET
        and scores.get(entry.id, 0.0) > 0
        and entry.id not in role_bullets.get(role.key, ())
    ]


def _top_up(
    inventory: CvInventory,
    scores: dict[str, float],
    requirements_for: dict[str, tuple[str, ...]],
    settings: Settings,
    recent: list[Role],
    role_bullets: dict[str, tuple[str, ...]],
    budget: int,
) -> TopUpStop:
    """Fill the selection lines allocation left with the best cited bullets still unselected.

    Every role is filled to ``max_bullets_per_role`` before any role passes it.
    Room left after that deepens roles evenly: the ceiling rises by one only
    when no role under it has a cited bullet left, so each level adds at most
    one bullet per role. Returns why filling stopped.
    """
    spent = _spent_lines(inventory, role_bullets)
    answered = _answered_counts(requirements_for, role_bullets)
    available = _unselected_bullets(inventory, scores, recent, role_bullets)
    ceiling = settings.max_bullets_per_role

    while available:
        best, ceiling = _next_bullet(
            available, role_bullets, ceiling, budget - spent, scores, requirements_for, answered
        )
        if best is None:
            return TopUpStop.BUDGET
        available.remove(best)
        spent += wrapped_lines(best.full_text)
        _add_bullet(inventory, role_bullets, best)
        for requirement_id in requirements_for.get(best.id, ()):
            answered[requirement_id] += 1
    return TopUpStop.EVIDENCE


def _withheld_by_cap(
    inventory: CvInventory,
    scores: dict[str, float],
    settings: Settings,
    recent: list[Role],
    role_bullets: dict[str, tuple[str, ...]],
    room: int,
) -> tuple[str, ...]:
    """Return the capped roles' cited bullets that ``room`` lines hold together, best first."""
    at_cap = [
        entry
        for entry in _unselected_bullets(inventory, scores, recent, role_bullets)
        if _count(role_bullets, entry) == settings.max_bullets_per_role
    ]
    held: list[str] = []
    for entry in sorted(at_cap, key=lambda entry: -scores.get(entry.id, 0.0)):
        if (cost := wrapped_lines(entry.full_text)) <= room:
            held.append(entry.id)
            room -= cost
    return tuple(held)


def _next_bullet(
    available: list[Entry],
    role_bullets: dict[str, tuple[str, ...]],
    ceiling: int,
    room: int,
    scores: dict[str, float],
    requirements_for: dict[str, tuple[str, ...]],
    answered: dict[str, int],
) -> tuple[Entry | None, int]:
    """Return the next bullet top-up takes and the ceiling it was chosen under.

    ``available`` must not be empty. The ceiling rises past any level at which
    no role under it has a bullet left.
    """
    while not (under := [entry for entry in available if _count(role_bullets, entry) < ceiling]):
        ceiling += 1
    affordable = [entry for entry in under if wrapped_lines(entry.full_text) <= room]
    if not affordable:
        return None, ceiling
    best = max(affordable, key=lambda entry: _marginal(entry, scores, requirements_for, answered))
    return best, ceiling


def _count(role_bullets: dict[str, tuple[str, ...]], entry: Entry) -> int:
    """Return how many bullets ``entry``'s role already holds."""
    return len(role_bullets.get(entry.role_key or "", ()))


def _add_bullet(
    inventory: CvInventory, role_bullets: dict[str, tuple[str, ...]], entry: Entry
) -> None:
    """Add ``entry`` to its role's bullets, kept in document order."""
    key = entry.role_key or ""
    kept = [inventory.by_id(entry_id) for entry_id in role_bullets.get(key, ())] + [entry]
    kept.sort(key=lambda item: item.order)
    role_bullets[key] = tuple(item.id for item in kept)


def _sole_answers(
    entry_ids: tuple[str, ...] | list[str],
    requirements_for: dict[str, tuple[str, ...]],
    must_haves: tuple[str, ...],
) -> frozenset[str]:
    """Return the ids that are the only answer among ``entry_ids`` to a must-have.

    A must-have covered by a single entry on the page loses its coverage if that
    entry is dropped, so these are the entries trimming must leave alone.
    """
    protected: set[str] = set()
    for requirement_id in must_haves:
        answering = [
            entry_id
            for entry_id in entry_ids
            if requirement_id in requirements_for.get(entry_id, ())
        ]
        if len(answering) == 1:
            protected.add(answering[0])
    return frozenset(protected)


def _cover_must_haves(
    inventory: CvInventory,
    spec: JobSpec,
    scores: dict[str, float],
    requirements_for: dict[str, tuple[str, ...]],
    settings: Settings,
    recent: list[Role],
    role_bullets: dict[str, tuple[str, ...]],
    budget: int,
    *,
    evidence: EvidenceMap,
    today: date | None,
) -> tuple[str, ...]:
    """Guarantee each must-have's evidence reaches the page, then fit the budget.

    Returns the must-have requirement ids so the drop order can protect the sole
    answer to each of them.
    """
    must_haves = tuple(
        requirement.id
        for requirement in spec.requirements
        if requirement.importance is Importance.MUST_HAVE
    )
    _guarantee_must_have_coverage(
        inventory,
        scores,
        requirements_for,
        settings,
        recent,
        role_bullets,
        must_haves,
        requirement_scores={
            requirement_id: _score(
                inventory,
                spec,
                EvidenceMap(
                    matches=tuple(
                        item for item in evidence.matches if item.requirement_id == requirement_id
                    )
                ),
                today,
            )[0]
            for requirement_id in must_haves
        },
    )
    _trim_to_budget(inventory, scores, requirements_for, settings, role_bullets, must_haves, budget)
    return must_haves


def _guarantee_must_have_coverage(
    inventory: CvInventory,
    scores: dict[str, float],
    requirements_for: dict[str, tuple[str, ...]],
    settings: Settings,
    recent: list[Role],
    role_bullets: dict[str, tuple[str, ...]],
    must_haves: tuple[str, ...],
    *,
    requirement_scores: dict[str, dict[str, float]],
) -> None:
    """Put each must-have's strongest available evidence on the page.

    Greedy budget-filling can leave a must-have unanswered while its only strong
    evidence sits unselected in a role whose share went to other bullets, and it
    can answer one with a weak bullet while a stronger answer stays unselected.
    Both are fixed: an uncovered must-have gets its best evidence, and a covered
    one is upgraded when a strictly stronger unselected answer exists.
    """
    recent_keys = {role.key for role in recent}
    for requirement_id in must_haves:
        local_scores = requirement_scores[requirement_id]
        best = _strongest_unselected_answer(
            inventory, recent, local_scores, requirements_for, requirement_id, role_bullets
        )
        if best is None or (best.role_key or "") not in recent_keys:
            continue
        selected = [
            local_scores.get(entry_id, 0.0)
            for ids in role_bullets.values()
            for entry_id in ids
            if requirement_id in requirements_for.get(entry_id, ())
        ]
        # Place the best evidence when nothing selected answers this must-have;
        # when something does, promote only a strictly stronger answer, which the
        # trim pass then keeps over the weaker in-set one it now outranks.
        if selected and local_scores.get(best.id, 0.0) <= max(selected) + PROMOTION_MARGIN:
            continue
        _place_bullet(inventory, scores, requirements_for, settings, role_bullets, best, must_haves)


def _strongest_unselected_answer(
    inventory: CvInventory,
    recent: list[Role],
    scores: dict[str, float],
    requirements_for: dict[str, tuple[str, ...]],
    requirement_id: str,
    role_bullets: dict[str, tuple[str, ...]],
) -> Entry | None:
    """Return the highest-scoring bullet answering ``requirement_id`` not yet on the page."""
    candidates = [
        entry
        for role in recent
        for entry in inventory.entries_for(role.key)
        if entry.kind is EntryKind.BULLET
        and requirement_id in requirements_for.get(entry.id, ())
        and entry.id not in role_bullets.get(role.key, ())
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda entry: scores.get(entry.id, 0.0))


def _place_bullet(
    inventory: CvInventory,
    scores: dict[str, float],
    requirements_for: dict[str, tuple[str, ...]],
    settings: Settings,
    role_bullets: dict[str, tuple[str, ...]],
    entry: Entry,
    must_haves: tuple[str, ...],
) -> None:
    """Add ``entry`` to its role, displacing the weakest non-protected bullet at the cap."""
    key = entry.role_key or ""
    current = list(role_bullets.get(key, ()))
    if len(current) >= settings.max_bullets_per_role:
        selected = tuple(item for ids in role_bullets.values() for item in ids)
        protected = _sole_answers((*selected, entry.id), requirements_for, must_haves)
        expendable = [entry_id for entry_id in current if entry_id not in protected]
        if not expendable:
            return
        current.remove(min(expendable, key=lambda entry_id: scores.get(entry_id, 0.0)))
    current.append(entry.id)
    ordered = sorted(
        (inventory.by_id(entry_id) for entry_id in current), key=lambda item: item.order
    )
    role_bullets[key] = tuple(item.id for item in ordered)


def _spent_lines(inventory: CvInventory, role_bullets: dict[str, tuple[str, ...]]) -> int:
    """Return the wrapped-line cost of every selected experience bullet."""
    return sum(
        wrapped_lines(inventory.by_id(entry_id).full_text)
        for ids in role_bullets.values()
        for entry_id in ids
    )


def _trim_to_budget(
    inventory: CvInventory,
    scores: dict[str, float],
    requirements_for: dict[str, tuple[str, ...]],
    settings: Settings,
    role_bullets: dict[str, tuple[str, ...]],
    must_haves: tuple[str, ...],
    budget: int,
) -> None:
    """Shed the weakest expendable bullets until the page fits its line budget.

    Guaranteeing coverage can push a role past its share, so this lands the plan
    back on ~``pages`` while protecting the sole answer to any must-have and each
    role's configured bullet floor.
    """
    while _spent_lines(inventory, role_bullets) > budget:
        target = _weakest_expendable(scores, requirements_for, settings, role_bullets, must_haves)
        if target is None:
            return
        key, weakest = target
        role_bullets[key] = tuple(entry_id for entry_id in role_bullets[key] if entry_id != weakest)


def _weakest_expendable(
    scores: dict[str, float],
    requirements_for: dict[str, tuple[str, ...]],
    settings: Settings,
    role_bullets: dict[str, tuple[str, ...]],
    must_haves: tuple[str, ...],
) -> tuple[str, str] | None:
    """Return the (role, id) of the weakest bullet trimming may drop, or None."""
    selected = tuple(entry_id for ids in role_bullets.values() for entry_id in ids)
    protected = _sole_answers(selected, requirements_for, must_haves)
    expendable = [
        (key, entry_id)
        for key, ids in role_bullets.items()
        for entry_id in ids
        if entry_id not in protected and len(role_bullets[key]) > settings.min_bullets_per_role
    ]
    if not expendable:
        return None
    return min(expendable, key=lambda pair: scores.get(pair[1], 0.0))


def _allocate(scores: dict[str, float], order: list[str], budget: int) -> dict[str, int]:
    """Share the line budget across roles in proportion to how well they answer."""
    if not order:
        return {}
    floor = min(FLOOR_LINES, max(1, budget // len(order)))
    allowance = dict.fromkeys(order, floor)
    remaining = budget - floor * len(order)
    total = sum(max(0.0, scores.get(key, 0.0)) for key in order)
    if remaining <= 0 or total <= 0:
        return allowance
    for key in order:
        share = int(remaining * (max(0.0, scores.get(key, 0.0)) / total))
        allowance[key] += share
    return allowance


def _relevant_publications(
    inventory: CvInventory, scores: dict[str, float], settings: Settings
) -> tuple[str, ...]:
    """Return the publications this posting gives a reason to include.

    On a derivative a publication list competes with experience for the same
    lines, so relevance decides rather than the shape of a document.
    ``min_publications`` is still honored, because a researcher wants a record
    visible even where the posting never names a paper.
    """
    ranked = sorted(
        inventory.of_kind(EntryKind.PUBLICATION),
        key=lambda entry: (-scores.get(entry.id, 0.0), entry.order),
    )
    earned = [entry for entry in ranked if scores.get(entry.id, 0.0) > 0.0]
    floor = min(settings.min_publications, len(ranked))
    if len(earned) < floor:
        earned = ranked[:floor]
    kept = earned[: settings.max_publications]
    kept.sort(key=lambda entry: entry.order)
    return tuple(entry.id for entry in kept)


def _drop_order(
    publications: tuple[str, ...],
    education: tuple[str, ...],
    role_bullets: dict[str, tuple[str, ...]],
    recent: list[Role],
    scores: dict[str, float],
    settings: Settings,
    requirements_for: dict[str, tuple[str, ...]],
    must_haves: tuple[str, ...],
) -> tuple[str, ...]:
    """Order the optional items from first sacrificed to last.

    Bullets a role holds past ``max_bullets_per_role`` go first, so a trimmed
    page passes the cap only where nothing within it could use the room. They
    leave from the role holding the most, weakest first, which undoes top-up's
    deepening a level at a time. Everything else expendable follows, weakest
    first. The floors keep shape: the strongest papers up to the configured
    minimum survive, and no role loses its last couple of bullets. The only answer to a
    must-have is never listed.
    """
    selected = tuple(entry_id for ids in role_bullets.values() for entry_id in ids)
    protected = _sole_answers((*selected, *publications, *education), requirements_for, must_haves)
    over_cap: dict[str, list[str]] = {}
    ranked_publications = sorted(publications, key=lambda entry_id: -scores.get(entry_id, 0.0))
    within: list[str] = ranked_publications[settings.min_publications :]
    floor = settings.min_bullets_per_role
    cap = max(settings.max_bullets_per_role, floor)
    for role in recent:
        # Rank before applying the floor so document order cannot bias retention.
        by_strength = sorted(
            role_bullets.get(role.key, ()),
            key=lambda entry_id: -scores.get(entry_id, 0.0),
        )
        within.extend(by_strength[floor:cap])
        over_cap[role.key] = by_strength[cap:]

    def weakest_first(ids: list[str]) -> list[str]:
        return sorted(
            (entry_id for entry_id in ids if entry_id not in protected),
            key=lambda entry_id: scores.get(entry_id, 0.0),
        )

    queues = {key: queue for key, ids in over_cap.items() if (queue := weakest_first(ids))}
    held = {key: len(role_bullets.get(key, ())) for key in queues}
    return (*_deepest_first(queues, held, scores), *weakest_first(within))


def _deepest_first(
    queues: dict[str, list[str]], held: dict[str, int], scores: dict[str, float]
) -> list[str]:
    """Order each role's droppable bullets, weakest first, taking from whichever role holds most.

    Ties go to the weaker next bullet, as top-up gives a level's room to the stronger one.
    """
    order: list[str] = []
    while queues:
        key = max(queues, key=lambda role: (held[role], -scores.get(queues[role][0], 0.0)))
        order.append(queues[key].pop(0))
        held[key] -= 1
        if not queues[key]:
            del queues[key]
    return order
