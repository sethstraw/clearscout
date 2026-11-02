"""Name every editable piece of a draft, and rebuild the draft from decisions."""

from __future__ import annotations

from tailorcv.application.tailoring.stages.draft import (
    DraftedLine,
    DraftedRole,
    DraftedSkill,
    ResumeDraft,
)
from tailorcv.application.tailoring.stages.select import Plan

# Keep review, approval, and rebuild text byte-identical.
SUMMARY_SLOT = "summary"
EARLIER_SLOT = "earlier_career"

# Split skills replacements at the rendered label separator.
SKILL_SEPARATOR = ":"

# Distinguish an absent edit from an explicit removal.
_KEEP = DraftedLine(text="\0keep", source_ids=())


def _skill_slot(index: int) -> str:
    """Return the slot of one skills line."""
    return f"skills[{index}]"


def _context_slot(role_key: str) -> str:
    """Return the slot of one role's scope sentence."""
    return f"experience/{role_key}/context"


def _bullet_slot(role_key: str, index: int) -> str:
    """Return the slot of one bullet under one role."""
    return f"experience/{role_key}/bullet[{index}]"


def _role_slots(role: DraftedRole) -> list[tuple[str, DraftedLine]]:
    """Return one role's slots, its scope sentence before its bullets."""
    found: list[tuple[str, DraftedLine]] = []
    if role.context is not None:
        found.append((_context_slot(role.role_key), role.context))
    found += [
        (_bullet_slot(role.role_key, index), bullet) for index, bullet in enumerate(role.bullets)
    ]
    return found


def rebuild(draft: ResumeDraft, edits: dict[str, DraftedLine | None]) -> ResumeDraft:
    """Return the draft with the named slots replaced or removed.

    A slot absent from ``edits`` is untouched. ``None`` removes the line, and the
    summary becomes empty, which is how the assembler already spells a section
    that is not there.
    """
    summary = edits.get(SUMMARY_SLOT, draft.summary)
    skills = tuple(
        _skill_from(skill, line)
        for index, skill in enumerate(draft.skills)
        if (line := edits.get(_skill_slot(index), _KEEP)) is not None
    )
    roles = tuple(_rebuilt_role(role, edits) for role in draft.roles)
    earlier = edits.get(EARLIER_SLOT, draft.earlier_career)
    return ResumeDraft(
        summary=summary if summary is not None else DraftedLine(text="", source_ids=()),
        skills=skills,
        roles=roles,
        earlier_career=earlier,
    )


def emptied_roles(draft: ResumeDraft, plan: Plan) -> tuple[str, ...]:
    """Return the planned roles the draft now writes nothing for.

    Assembly is given a plan without those roles rather than a document with
    headings that stand over nothing.
    """
    written = {
        role.role_key
        for role in draft.roles
        if (role.context is not None and role.context.text.strip())
        or any(bullet.text.strip() for bullet in role.bullets)
    }
    return tuple(key for key in plan.recent_roles if key not in written)


def _skill_text(skill: DraftedSkill) -> str:
    """Return the reviewable form of one skills line, as the checks read it."""
    return f"{skill.label}: {skill.text}"


def _skill_from(skill: DraftedSkill, line: DraftedLine) -> DraftedSkill:
    """Return the skills line a decision left behind.

    A skills line is reviewed as ``Label: contents`` and keeps its drafted
    label. A replacement drops a leading label only when it is that label.
    """
    if line is _KEEP:
        return skill
    label, separator, contents = line.text.partition(SKILL_SEPARATOR)
    same = separator and label.strip().casefold() == skill.label.strip().casefold()
    text = contents if same else line.text
    return DraftedSkill(label=skill.label, text=text.strip(), source_ids=line.source_ids)


def _rebuilt_role(role: DraftedRole, edits: dict[str, DraftedLine | None]) -> DraftedRole:
    """Return one role's prose after the decisions that touched it."""
    context = edits.get(_context_slot(role.role_key), role.context)
    bullets = tuple(
        line if line is not _KEEP else bullet
        for index, bullet in enumerate(role.bullets)
        if (line := edits.get(_bullet_slot(role.role_key, index), _KEEP)) is not None
    )
    return DraftedRole(role_key=role.role_key, context=context, bullets=bullets)


def _in_document_order(draft: ResumeDraft, plan: Plan) -> list[tuple[str, DraftedLine]]:
    """Return the drafted slots ordered as the document prints them.

    The draft's own role order is the model's; the plan's is the page's. A role
    the plan does not carry still appears, at the end, because a proposal set
    that dropped a drafted line would not be a review of the draft.
    """
    by_role = {role.role_key: _role_slots(role) for role in draft.roles}
    ordered: list[tuple[str, DraftedLine]] = [(SUMMARY_SLOT, draft.summary)]
    ordered += [
        (_skill_slot(index), DraftedLine(text=_skill_text(skill), source_ids=skill.source_ids))
        for index, skill in enumerate(draft.skills)
    ]
    for role_key in plan.recent_roles:
        ordered += by_role.pop(role_key, [])
    for remaining in by_role.values():
        ordered += remaining
    if draft.earlier_career is not None:
        ordered.append((EARLIER_SLOT, draft.earlier_career))
    return ordered
