"""Write the derivative, then assemble it in Python rather than trusting prose.

The model returns structured content, not a document, and each piece of it
carries the identifiers it derives from. Python builds the Markdown: headings,
role metadata, dates, education, and publications are copied from the master CV
and never pass through a model at all.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from functools import cached_property
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from clearscout.application.config import Settings, Stage
from clearscout.application.master_cv.cv import CvInventory, Entry
from clearscout.application.prompts import render
from clearscout.application.tailoring.guardrails import Guardrails
from clearscout.application.tailoring.layout import Budget, markdown_lines
from clearscout.application.tailoring.stages.context import (
    chronology_block,
    earlier_heading,
    header_markdown,
    letterhead_markdown,
    requirement_text,
    role_heading,
)
from clearscout.application.tailoring.stages.jobspec import Importance, JobSpec
from clearscout.application.tailoring.stages.select import Plan
from clearscout.infrastructure.providers.client import Client
from clearscout.infrastructure.providers.plan import RequestPlan, Segment
from clearscout.infrastructure.web_fetch.company_research import ResearchSource
from clearscout.infrastructure.web_fetch.company_research import evidence_block as research_block
from clearscout.shared.errors import BudgetError

# Keep the block present because the template language has no conditionals.
FIRST_ATTEMPT = "(this is the first attempt)"

# The letter prompt states this limit and the letter check counts against it.
MAXIMUM_LETTER_WORDS = 350

# A letter sentence sharing this many consecutive ordinary words with its cited evidence repeats it.
LETTER_ECHO_WORDS = 8

# A comma or semicolon inside parentheses belongs to one skill item; a semicolon ends a series.
SKILL_SEPARATOR = re.compile(r",(?![^()]*\))")
SKILL_SERIES_END = re.compile(r";(?![^()]*\))")
CONJUNCTIONS = r"and|or|as\s+well\s+as"
SKILL_JOIN = re.compile(rf"^(?:{CONJUNCTIONS})\s+", re.IGNORECASE)
# A piece holding a conjunction closes the series a lead-in opened.
SKILL_SERIES_CLOSE = re.compile(rf"(?:^|\s)(?:{CONJUNCTIONS})\s", re.IGNORECASE)
PREPOSITIONS = r"with|of|to|in|for|on|at|by|from|across|against|about|into"
# Lowercase words ending in a preposition before a series' first item, as in "working
# knowledge of"; a capitalized word is a name, as in "Ruby on Rails", never a lead-in.
SKILL_LEAD_IN = re.compile(rf"^(?P<lead>(?:[a-z][\w'-]*\s+)+?(?:{PREPOSITIONS})\s+)\S")
# A drafted line may capitalize the lead-in it copied, as in "Familiar with Quux".
DRAFTED_LEAD_IN = re.compile(SKILL_LEAD_IN.pattern, re.IGNORECASE)
# Splits a led series into its items: at a comma outside parentheses or at a conjunction.
SKILL_MEMBER = re.compile(rf",(?![^()]*\))|\s+(?:{CONJUNCTIONS})\s+", re.IGNORECASE)
# A lead-in limits only names: one word, or words opening on a capital or digit, with no
# preposition. So "familiar with survival analysis, causal inference" splits per comma,
# and a listed "SQL, data modeling in dbt, Airflow" leaves Airflow inside the phrase's item.
SKILL_PHRASE = re.compile(rf"\b(?:{PREPOSITIONS})\b", re.IGNORECASE)
SERIES_ITEM_WORDS = 4

# A leading "label:" heads a skills text's items, whether the CV or the model wrote it.
SKILL_SUB_LABEL = re.compile(r"^\s*\**([^:()*\n]+?)\**:\**\s+")

# Heads the skill items whose entries sit under no heading in a CV whose skills carry headings.
ADDITIONAL_SKILLS_HEADING = "Additional skills"

LETTER_CLOSING = "Sincerely,"


class SourceType(StrEnum):
    """Where a citation source comes from."""

    CANDIDATE = "candidate"
    COMPANY = "company"


class DraftedLine(BaseModel):
    """One line of output prose and the evidence it was derived from."""

    model_config = ConfigDict(frozen=True)

    text: str
    source_ids: tuple[str, ...] = ()
    source_types: tuple[SourceType, ...] = ()

    @model_validator(mode="after")
    def _default_candidate_types(self) -> DraftedLine:
        if self.source_ids and not self.source_types:
            object.__setattr__(
                self,
                "source_types",
                tuple(SourceType.CANDIDATE for _ in self.source_ids),
            )
        return self


class DraftedSkill(BaseModel):
    """One skills line as a bold label and its comma-separated contents."""

    model_config = ConfigDict(frozen=True)

    label: str
    text: str
    source_ids: tuple[str, ...] = ()
    source_types: tuple[SourceType, ...] = ()

    @model_validator(mode="after")
    def _default_candidate_types(self) -> DraftedSkill:
        if self.source_ids and not self.source_types:
            object.__setattr__(
                self,
                "source_types",
                tuple(SourceType.CANDIDATE for _ in self.source_ids),
            )
        return self


class DraftedRole(BaseModel):
    """The prose for one role: an optional scope paragraph and its bullets."""

    model_config = ConfigDict(frozen=True)

    role_key: str
    context: DraftedLine | None = None
    bullets: tuple[DraftedLine, ...] = ()


class ResumeDraft(BaseModel):
    """Everything the model contributes to the derivative."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: DraftedLine
    skills: tuple[DraftedSkill, ...] = ()
    roles: tuple[DraftedRole, ...] = ()
    earlier_career: DraftedLine | None = None

    def lines(self) -> tuple[DraftedLine, ...]:
        """Return every drafted line, which is what the checks iterate over.

        Blank text is not a line: the assembler already skips it, and a review
        spells a removal as empty text where the field is required. Counting one
        here would report a section nobody can see as a line citing no evidence.
        """
        collected: list[DraftedLine] = [self.summary]
        collected += [
            DraftedLine(
                text=f"{skill.label}: {skill.text}",
                source_ids=skill.source_ids,
                source_types=skill.source_types,
            )
            for skill in self.skills
        ]
        for role in self.roles:
            if role.context is not None:
                collected.append(role.context)
            collected.extend(role.bullets)
        if self.earlier_career is not None:
            collected.append(self.earlier_career)
        return tuple(line for line in collected if line.text.strip())


class CoverLetter(BaseModel):
    """A cover letter grounded in the same evidence map as the resume."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    greeting: str
    paragraphs: tuple[DraftedLine, ...] = ()
    closing: str
    letterhead: str = ""

    def rendered(self) -> str:
        """Return the letter from greeting to signature, as the critic reads it."""
        body = "\n\n".join(paragraph.text.strip() for paragraph in self.paragraphs)
        return f"{self.greeting.strip()}\n\n{body}\n\n{self.closing.strip()}\n"

    def markdown(self) -> str:
        """Return the letter as its Markdown file holds it, under the copied letterhead."""
        if not self.letterhead.strip():
            return self.rendered()
        return f"{self.letterhead.strip()}\n\n{self.rendered()}"

    def cited_body(self) -> str:
        """Return each paragraph under the identifiers it cites, as an edit request shows it."""
        return "\n\n".join(
            f"Paragraph {number}, source_ids: {', '.join(paragraph.source_ids) or 'none'}\n"
            f"{paragraph.text.strip()}"
            for number, paragraph in enumerate(self.paragraphs, 1)
        )


def _require_evidence(entry_ids: tuple[str, ...]) -> tuple[str, ...]:
    """Return the drafting vocabulary, refusing to build a schema over nothing.

    An empty tuple would reach ``Literal[()]``, which fails deep inside pydantic
    with a bare AssertionError, and that assertion disappears under ``python -O``
    and produces a silently broken schema instead.
    """
    if not entry_ids:
        raise BudgetError(
            "Nothing in the master CV matched this posting well enough to write from.\n"
            "  The requirement map in the report shows what was looked for.\n"
            "  This usually means the posting is for a different field, or the CV's\n"
            "  experience sections did not parse. Run 'clearscout doctor' to check the parse."
        )
    return entry_ids


def skill_heading(entry: Entry) -> str:
    """Return the heading a skill entry sits under in the master CV, or the program's own."""
    return entry.label or entry.group or ADDITIONAL_SKILLS_HEADING


def skills_are_headed(entries: Iterable[Entry]) -> bool:
    """Return whether any skill entry carries a heading, which the program then supplies."""
    return any(entry.label or entry.group for entry in entries)


def skill_sub_label(text: str) -> tuple[str, str]:
    """Split a leading "label:" off a skills text, returning that label or "" and the items."""
    match = SKILL_SUB_LABEL.match(text)
    return (match.group(1).strip(), text[match.end() :]) if match else ("", text)


def skill_items(text: str) -> list[str]:
    """Split a skills text into items after any leading label, dropping joins and end marks.

    A lead-in phrase and its series stay one item, up to the first piece holding a
    conjunction or else to the semicolon or end, while every item it limits is a bare name.
    """
    _, listed = skill_sub_label(text)
    items: list[str] = []
    for segment in SKILL_SERIES_END.split(listed):
        series: list[str] = []
        for raw in SKILL_SEPARATOR.split(segment):
            piece = raw.strip() if series else SKILL_JOIN.sub("", raw.strip())
            if not series and not SKILL_LEAD_IN.match(piece):
                items.append(piece)
                continue
            series.append(piece)
            if SKILL_SERIES_CLOSE.search(piece):
                items += _series_items(series)
                series = []
        items += _series_items(series)
    return [cleaned for item in items if (cleaned := item.rstrip(".;:!? "))]


def _series_items(series: list[str]) -> list[str]:
    """Return a lead-in run's bare-name prefix as one item, then each later piece alone."""
    bare = next(
        (end for end in range(len(series), 0, -1) if _led_series(", ".join(series[:end]))), 0
    )
    led = [", ".join(series[:bare])] if bare else []
    return led + [SKILL_JOIN.sub("", piece) for piece in series[bare:]]


def _folded(text: str) -> str:
    return " ".join(text.casefold().split())


def _led_series(
    item: str, lead_in: re.Pattern[str] = SKILL_LEAD_IN
) -> tuple[str, frozenset[str]] | None:
    """Return an item's folded lead-in and the bare names it limits, or None when it has none."""
    match = lead_in.match(item)
    if match is None:
        return None
    members = [
        SKILL_JOIN.sub("", member.strip())
        for member in SKILL_MEMBER.split(item[match.end("lead") :])
    ]
    named = [member for member in members if member]
    if not all(_is_name(name) for name in named):
        return None
    return _folded(match["lead"]), frozenset(_folded(name) for name in named)


def _is_name(member: str) -> bool:
    """Return whether a series member is a name a lead-in may limit."""
    words = member.split()
    if SKILL_PHRASE.search(member) or len(words) > SERIES_ITEM_WORDS:
        return False
    return len(words) == 1 or member[0].isupper() or member[0].isdigit()


def _lists(own: str, item: str) -> bool:
    """Return whether an entry's item states ``item`` whole, or in part under its lead-in."""
    if _folded(own) == _folded(item):
        return True
    listed, drafted = _led_series(own), _led_series(item, DRAFTED_LEAD_IN)
    if listed is None or drafted is None:
        return False
    return listed[0] == drafted[0] and drafted[1] <= listed[1]


def _listing(item: str, cited: Iterable[Entry]) -> tuple[Entry, str] | None:
    """Return the first cited entry listing this item and the text, its own or a detail's."""
    return next(
        (
            (entry, text)
            for entry in cited
            for text in (entry.text, *entry.details)
            if any(_lists(own, item) for own in skill_items(text))
        ),
        None,
    )


def skill_placements(
    text: str, cited: Sequence[Entry]
) -> list[tuple[str, tuple[Entry, str] | None]]:
    """Return each drafted skills item with the entry and text listing it, or None.

    A drafted led item no entry lists is placed as its longest listed led prefix plus
    each later member, when every one of them is listed; the lead-in never drops.
    """
    placed: list[tuple[str, tuple[Entry, str] | None]] = []
    for item in skill_items(text):
        listing = _listing(item, cited)
        parts = None if listing else _led_parts(item, cited)
        placed += parts or [(item, listing)]
    return placed


def _led_parts(item: str, cited: Sequence[Entry]) -> list[tuple[str, tuple[Entry, str] | None]]:
    """Return a drafted led item split at its longest listed prefix, or [] when it cannot be."""
    pieces = [piece.strip() for piece in SKILL_SEPARATOR.split(item)]
    for end in range(len(pieces) - 1, 0, -1):
        head = ", ".join(pieces[:end])
        if (listing := _listing(head, cited)) is None:
            continue
        rest = [
            member
            for piece in pieces[end:]
            for part in SKILL_MEMBER.split(piece)
            if (member := SKILL_JOIN.sub("", part.strip()))
        ]
        homes = [(member, _listing(member, cited)) for member in rest]
        return [(head, listing), *homes] if all(home for _, home in homes) else []
    return []


def _grouped_skills(
    skills: Iterable[Mapping[str, Any]], entries: Mapping[str, Entry]
) -> list[dict[str, Any]]:
    """Put each drafted skill item under its home entry's CV heading and sub-label.

    An item no cited entry lists goes under the first citation's heading, alone.
    """
    grouped: dict[tuple[str, str], tuple[list[str], list[str]]] = {}
    for skill in skills:
        cited = [entries[entry_id] for entry_id in skill["source_ids"] if entry_id in entries]
        first = skill_heading(cited[0]) if cited else ADDITIONAL_SKILLS_HEADING
        for item, listing in skill_placements(skill["text"], cited):
            heading, sub_label = _placement(listing, first)
            _file_item(grouped.setdefault((heading, sub_label), ([], [])), item, heading, cited)
    return [
        {
            "label": heading,
            "text": f"{sub_label}: {', '.join(items)}" if sub_label else ", ".join(items),
            "source_ids": ids,
        }
        for (heading, sub_label), (items, ids) in grouped.items()
    ]


def _placement(listing: tuple[Entry, str] | None, first: str) -> tuple[str, str]:
    """Return the heading and the listing text's sub-label, else the first citation's heading."""
    if listing is None:
        return first, ""
    home, text = listing
    return skill_heading(home), skill_sub_label(text)[0]


def _file_item(
    line: tuple[list[str], list[str]], item: str, heading: str, cited: Sequence[Entry]
) -> None:
    """Add one item to a heading's line once, with the cited entries under that heading."""
    kept, ids = line
    if _folded(item) not in {_folded(existing) for existing in kept}:
        kept.append(item)
    ids += [entry.id for entry in cited if entry.id not in ids and skill_heading(entry) == heading]


def draft_schema(
    entry_ids: tuple[str, ...],
    role_keys: tuple[str, ...],
    skill_ids: tuple[str, ...] = (),
    *,
    labelled: bool = True,
    earlier: bool = True,
) -> type[BaseModel]:
    """Close the drafting schema over the identifiers selection actually chose.

    Narrower than the matching schema on purpose: the drafter may only cite what
    survived selection, so an unselected bullet cannot creep back in through a
    citation. A skills line cites skill entries only, and writes a label only when
    the CV's skills carry no heading for the program to supply. An earlier-career
    line is asked for only when ``earlier`` roles exist to print it under.
    """
    entry_literal = Literal[entry_ids]  # type: ignore[valid-type]
    role_literal: Any = Literal[role_keys] if role_keys else str
    line = create_model(
        "ConstrainedLine",
        __config__=ConfigDict(extra="forbid"),
        text=(str, ...),
        source_ids=(list[entry_literal], Field(default_factory=list)),
    )
    skills: dict[str, Any] = {}
    if skill_ids:
        skill_literal = Literal[skill_ids]  # type: ignore[valid-type]
        label: dict[str, Any] = {"label": (str, ...)} if labelled else {}
        skill = create_model(
            "ConstrainedSkill",
            __config__=ConfigDict(extra="forbid"),
            **label,
            text=(str, ...),
            source_ids=(list[skill_literal], Field(default_factory=list)),
        )
        skills["skills"] = (list[skill], Field(default_factory=list))  # type: ignore[valid-type]
    optional_line: Any = line | None
    role = create_model(
        "ConstrainedRole",
        __config__=ConfigDict(extra="forbid"),
        role_key=(role_literal, ...),
        context=(optional_line, None),
        bullets=(list[line], Field(default_factory=list)),  # type: ignore[valid-type]
    )
    earlier_career: dict[str, Any] = {"earlier_career": (line | None, None)} if earlier else {}
    return create_model(
        "ConstrainedResumeDraft",
        __config__=ConfigDict(extra="forbid"),
        summary=(line, ...),
        **skills,
        roles=(list[role], Field(default_factory=list)),  # type: ignore[valid-type]
        **earlier_career,
    )


def evidence_block(
    inventory: CvInventory, plan: Plan, spec: JobSpec, *, layout: bool = True
) -> str:
    """Render the selected evidence, grouped by role, for a writing prompt.

    ``layout`` adds the resume's bullet counts and earlier-role instruction, which a
    letter request leaves out.
    """
    must_haves = frozenset(
        item.id for item in spec.requirements if item.importance is Importance.MUST_HAVE
    )
    lines: list[str] = []
    first_earlier = plan.earlier_roles[0] if plan.earlier_roles else None
    for role_key in (*plan.recent_roles, *plan.earlier_roles):
        # Mark groups so identical formatting remains distinguishable.
        if role_key == first_earlier and inventory.role(role_key) is not None:
            instruction = " Summarise these; do not write bullets for them." if layout else ""
            lines.append(f"\n## Earlier roles.{instruction}")
        lines += _role_evidence(inventory, plan, role_key, must_haves, layout=layout)
    if plan.summary_ids or plan.skill_ids:
        lines.append("\n### background and skills")
        for entry_id in (*plan.summary_ids, *plan.skill_ids):
            lines.append(_evidence_line(inventory, plan, entry_id, must_haves))
    return "\n".join(lines).strip()


def _role_evidence(
    inventory: CvInventory, plan: Plan, role_key: str, must_haves: frozenset[str], *, layout: bool
) -> list[str]:
    """Render one role's heading, bullet count, and evidence, or nothing for an unknown role."""
    role = inventory.role(role_key)
    if role is None:
        return []
    lines = [
        f"\n### role_key: {role_key}",
        f"{role.organization} - {role.title or 'role'} ({role.dates or 'undated'})",
    ]
    lines += [
        _evidence_line(inventory, plan, entry_id, must_haves)
        for entry_id in plan.role_context.get(role_key, ())
    ]
    bullets = plan.role_bullets.get(role_key, ())
    if bullets and layout:
        lines.append(f"Write {len(bullets)} bullet(s) here, one for each evidence item below.")
    lines += [_evidence_line(inventory, plan, entry_id, must_haves) for entry_id in bullets]
    return lines


def _evidence_line(
    inventory: CvInventory, plan: Plan, entry_id: str, must_haves: frozenset[str]
) -> str:
    """Render one evidence item with its identifier, the requirements it answers, and details."""
    try:
        entry = inventory.by_id(entry_id)
    except KeyError:  # pragma: no cover - plan ids come from the inventory
        return f"[{entry_id}] (missing)"
    answers = plan.requirements_for.get(entry_id, ())
    required = [requirement for requirement in answers if requirement in must_haves]
    marked = f"; must-have {', '.join(required)}" if required else ""
    suffix = f"  (answers {', '.join(answers)}{marked})" if answers else ""
    label = f"**{entry.label}:** " if entry.label else ""
    text, newline, details = entry.full_text.partition("\n")
    return f"[{entry.id}] {label}{text}{suffix}{newline}{details}"


def draft(
    client: Client,
    inventory: CvInventory,
    spec: JobSpec,
    plan: Plan,
    guardrails: Guardrails,
    feedback: str = "",
    *,
    posting_hint: str | None = None,
    active_disputes: str | None = None,
) -> ResumeDraft:
    """Write the derivative's prose from selected evidence only."""
    entry_ids = _require_evidence(plan.all_ids())
    skill_entries = {entry_id: inventory.by_id(entry_id) for entry_id in plan.skill_ids}
    headed = skills_are_headed(skill_entries.values())
    # Limit this check to recent roles, which rewriting can change.
    schema = draft_schema(
        entry_ids,
        plan.recent_roles,
        plan.skill_ids,
        labelled=not headed,
        earlier=bool(plan.earlier_roles),
    )
    policy = client.settings.policy_for(Stage.DRAFT)
    request = RequestPlan(
        stage=Stage.DRAFT,
        instructions=Segment(draft_system_prompt(guardrails), "draft_system"),
        task=Segment(
            render(
                "draft_user",
                role=spec.title,
                company=spec.company or "the employer",
                keywords=", ".join(spec.keywords) or "(none extracted)",
                evidence=requirement_text(spec) + "\n\n" + evidence_block(inventory, plan, spec),
                feedback=feedback or FIRST_ATTEMPT,
                posting_hint=posting_hint or "",
                active_disputes=active_disputes or "",
            ),
            "draft_user",
        ),
        # The selected entries are in the task above; this is the chronology
        # they sit in.
        evidence=Segment(chronology_block(inventory)),
        max_tokens=policy.max_tokens,
    )
    response = client.complete(request, schema)
    answer = response.model_dump(mode="json")
    if headed:
        answer["skills"] = _grouped_skills(answer.get("skills", ()), skill_entries)
    return ResumeDraft.model_validate(answer)


def draft_system_prompt(guardrails: Guardrails) -> str:
    """Return the drafting instructions with the candidate's binding distinctions."""
    return render("draft_system", distinctions=guardrails.prompt_block())


def cover_letter_system_prompt(guardrails: Guardrails) -> str:
    """Return the letter's instructions, carrying the limits this program enforces.

    ``verify.check_letter`` counts ``MAXIMUM_LETTER_WORDS`` and ``LETTER_ECHO_WORDS``,
    so rendering them from the constants keeps prompt and check one.
    """
    return render(
        "cover_letter_system",
        distinctions=guardrails.prompt_block(),
        word_limit=str(MAXIMUM_LETTER_WORDS),
        echo_words=str(LETTER_ECHO_WORDS),
    )


def cover_letter_schema(allowed_ids: tuple[str, ...]) -> type[BaseModel]:
    """Build a letter schema closed over the evidence this run selected."""
    entry_literal = Literal[allowed_ids]  # type: ignore[valid-type]
    line = create_model(
        "ConstrainedLetterLine",
        __config__=ConfigDict(extra="forbid"),
        text=(str, ...),
        source_ids=(list[entry_literal], Field(default_factory=list)),
    )
    return create_model(
        "ConstrainedCoverLetter",
        __config__=ConfigDict(extra="forbid"),
        paragraphs=(list[line], Field(default_factory=list)),  # type: ignore[valid-type]
    )


def cover_letter(
    client: Client,
    inventory: CvInventory,
    spec: JobSpec,
    plan: Plan,
    guardrails: Guardrails,
    feedback: str = "",
    *,
    research: tuple[ResearchSource, ...] = (),
    posting_hint: str | None = None,
    active_disputes: str | None = None,
    employer: str | None = None,
    previous: CoverLetter | None = None,
) -> CoverLetter:
    """Write a cover letter from the same evidence, only when explicitly asked.

    The model writes the paragraphs; the greeting names the stored ``employer``, never
    the spec's model-read company, and the closing and letterhead copy the master CV. A
    ``previous`` attempt is sent back for the model to edit rather than replace.
    """
    allowed_ids = (*_require_evidence(plan.drafted_ids()), *(item.id for item in research))
    schema = cover_letter_schema(allowed_ids)
    policy = client.settings.policy_for(Stage.COVER)
    request = RequestPlan(
        stage=Stage.COVER,
        instructions=Segment(cover_letter_system_prompt(guardrails), "cover_letter_system"),
        # Wrap model findings as untrusted prompt data.
        task=Segment(
            render(
                "cover_letter_user",
                role=spec.title,
                company=spec.company or "the employer",
                evidence=requirement_text(spec)
                + "\n\n"
                + evidence_block(inventory, plan, spec, layout=False),
                research=research_block(research),
                feedback=feedback or FIRST_ATTEMPT,
                previous_letter=FIRST_ATTEMPT if previous is None else previous.cited_body(),
                posting_hint=posting_hint or "",
                active_disputes=active_disputes or "",
            ),
            "cover_letter_user",
        ),
        evidence=Segment(chronology_block(inventory)),
        max_tokens=policy.max_tokens,
    )
    response = client.complete(request, schema)
    addressee = (
        f"{employer.strip()} hiring team" if employer and employer.strip() else "hiring team"
    )
    company = {item.id for item in research}
    return CoverLetter(
        greeting=f"Dear {addressee},",
        paragraphs=tuple(
            DraftedLine(
                text=paragraph["text"],
                source_ids=tuple(paragraph["source_ids"]),
                source_types=tuple(
                    SourceType.COMPANY if source_id in company else SourceType.CANDIDATE
                    for source_id in paragraph["source_ids"]
                ),
            )
            for paragraph in response.model_dump(mode="json")["paragraphs"]
        ),
        # A blank line, because Markdown joins single-newline lines into one.
        closing=f"{LETTER_CLOSING}\n\n{inventory.name}",
        letterhead=letterhead_markdown(inventory, client.settings.header_drop),
    )


def printed_evidence(draft: ResumeDraft, plan: Plan, excluded: frozenset[str]) -> set[str]:
    """Return the selected entries the page shows: cited by a printed line, or copied onto it."""
    selected = set(plan.all_ids())
    # Education and publications reach the page verbatim, so no drafted line cites them.
    copied = [
        *plan.education_ids,
        *(entry_id for entry_id in plan.publication_ids if entry_id not in excluded),
    ]
    cited = [
        entry_id
        for line in draft.lines()
        if not (line.source_ids and set(line.source_ids) <= excluded)
        for entry_id in line.source_ids
    ]
    return {entry_id for entry_id in (*copied, *cited) if entry_id in selected}


def _off_page(printed: ResumeDraft, plan: Plan, excluded: frozenset[str]) -> frozenset[str]:
    """Return the entries of ``excluded`` that ``printed`` no longer shows."""
    return excluded - printed_evidence(printed, plan, excluded)


@dataclass(frozen=True)
class Fitted:
    """A draft assembled to its page budget, and the entries fitting it cost."""

    draft: ResumeDraft
    plan: Plan
    # The draft as the page prints it: a bullet whose every source was dropped is gone.
    printed: ResumeDraft
    markdown: str
    # Every entry fit dropped; one sharing a line with a kept entry still prints.
    excluded: frozenset[str]
    # Entries fitting took off the page, in drop order.
    trimmed: tuple[str, ...]
    lines: dict[str, float]
    # Entries taken off the page past the fit because the reader asked with --trim.
    requested: tuple[str, ...] = ()
    # What a further --trim would take off the page, in order.
    remaining: tuple[str, ...] = ()
    # Estimated lines past the page budget once nothing else may be dropped.
    overflow: float = 0.0

    @property
    def total_lines(self) -> float:
        """Return the estimated rendered lines of the whole fitted document."""
        return sum(self.lines.values())

    @property
    def off_page(self) -> frozenset[str]:
        """Return the dropped entries the page no longer shows."""
        return _off_page(self.printed, self.plan, self.excluded)

    @property
    def printed_plan(self) -> Plan:
        """Return the selected evidence, cited or not, less what fitting took off the page."""
        return self.plan.without(self.off_page)

    def redrawn(self, document: Document) -> Fitted:
        """Return this fit describing ``document``, the same exclusions over an edited draft."""
        lines = document.block_lines()
        budget = Budget(document.settings.pages).total_lines
        return replace(
            self,
            draft=document.draft,
            printed=document.printed(),
            markdown=document.markdown(),
            lines=lines,
            overflow=max(0.0, sum(lines.values()) - budget),
        )


def fit(
    draft_result: ResumeDraft,
    inventory: CvInventory,
    plan: Plan,
    settings: Settings,
    guardrails: Guardrails,
    trim: int = 0,
) -> Fitted:
    """Assemble the draft, dropping entries in drop order until its estimate fits the pages.

    Selection buys more than the page holds, so this is where the draft lands on
    the page. A drop is skipped when it would leave a must-have the page answered
    unanswered, or when a summary, context, skills, or earlier-career line cites
    it. ``trim`` further entries are taken off the page after that, at the reader's request.
    """
    budget = Budget(settings.pages).total_lines
    fitting = _Fitting(draft_result, inventory, plan, settings, guardrails)
    lines = fitting.document(frozenset()).block_lines()
    excluded: frozenset[str] = frozenset()
    trimmed: list[str] = []
    while sum(lines.values()) > budget and (step := fitting.step(excluded)):
        excluded, gone = step
        trimmed += gone
        lines = fitting.document(excluded).block_lines()
    excluded, requested = fitting.taken(excluded, trim)
    final = fitting.document(excluded)
    lines = final.block_lines()
    return Fitted(
        draft=draft_result,
        plan=plan,
        printed=final.printed(),
        markdown=final.markdown(),
        excluded=excluded,
        trimmed=tuple(trimmed),
        lines=lines,
        requested=requested,
        remaining=fitting.taken(excluded, len(plan.drop_order))[1],
        overflow=max(0.0, sum(lines.values()) - budget),
    )


@dataclass(frozen=True)
class _Fitting:
    """The fixed inputs of one fit, and which drops they allow."""

    draft: ResumeDraft
    inventory: CvInventory
    plan: Plan
    settings: Settings
    guardrails: Guardrails

    def document(self, excluded: frozenset[str]) -> Document:
        """Return the document with ``excluded`` dropped."""
        return Document(
            self.draft, self.inventory, self.plan, self.settings, self.guardrails, excluded
        )

    def next_drop(self, excluded: frozenset[str]) -> str | None:
        """Return the first entry in drop order that may go next, or None."""
        answered = self._answered(excluded)
        for entry_id in self.plan.drop_order:
            if entry_id in excluded or entry_id in self._cited_by_prose:
                continue
            # Dropping an entry the draft never printed would take nothing off the page.
            if entry_id not in self._on_page:
                continue
            if answered <= self._answered(excluded | {entry_id}):
                return entry_id
        return None

    def step(self, excluded: frozenset[str]) -> tuple[frozenset[str], tuple[str, ...]] | None:
        """Return the exclusions after the next drop and what it took off the page, or None."""
        drop = self.next_drop(excluded)
        if drop is None:
            return None
        after = excluded | {drop}
        shown, plan = self._shown, self.plan
        gone = _off_page(shown, plan, after) - _off_page(shown, plan, excluded)
        return after, tuple(entry_id for entry_id in plan.drop_order if entry_id in gone)

    def taken(self, excluded: frozenset[str], count: int) -> tuple[frozenset[str], tuple[str, ...]]:
        """Drop until ``count`` more entries are off the page; return the exclusions and those."""
        gone: list[str] = []
        while len(gone) < count and (step := self.step(excluded)):
            excluded, more = step
            gone += more
        return excluded, tuple(gone)

    @cached_property
    def _shown(self) -> ResumeDraft:
        """Return the draft without the roles and earlier career the page never prints."""
        # Which roles and earlier career print does not depend on what was dropped.
        return self.document(frozenset()).printed()

    @cached_property
    def _on_page(self) -> frozenset[str]:
        """Return the selected entries the page prints before anything is dropped."""
        return frozenset(printed_evidence(self._shown, self.plan, frozenset()))

    @cached_property
    def _cited_by_prose(self) -> frozenset[str]:
        """Return the entries a line other than an experience bullet cites."""
        draft = self.draft
        prose: list[DraftedLine | DraftedSkill] = [draft.summary, *draft.skills]
        prose += [role.context for role in draft.roles if role.context is not None]
        if draft.earlier_career is not None:
            prose.append(draft.earlier_career)
        return frozenset(entry_id for line in prose for entry_id in line.source_ids)

    def _answered(self, excluded: frozenset[str]) -> set[str]:
        """Return the must-haves the page answers with ``excluded`` dropped."""
        must_haves = set(self.plan.must_haves)
        return {
            requirement
            for entry_id in printed_evidence(self._shown, self.plan, excluded)
            for requirement in self.plan.requirements_for.get(entry_id, ())
            if requirement in must_haves
        }


@dataclass(frozen=True)
class Document:
    """One derivative, rendered section by section into Markdown."""

    draft: ResumeDraft
    inventory: CvInventory
    plan: Plan
    settings: Settings
    guardrails: Guardrails
    excluded: frozenset[str] = frozenset()

    def markdown(self) -> str:
        """Return the finished document."""
        body = "\n\n---\n\n".join(block for block in self._blocks().values() if block)
        return body.rstrip() + "\n"

    def block_lines(self) -> dict[str, float]:
        """Return the estimated rendered lines of each block the document prints."""
        return {name: markdown_lines(block) for name, block in self._blocks().items() if block}

    def printed(self) -> ResumeDraft:
        """Return the draft without the roles, bullets, and earlier career this document omits."""
        recent = set(self.plan.recent_roles)
        roles = tuple(
            role.model_copy(
                update={
                    "bullets": tuple(
                        bullet for bullet in role.bullets if not self._fully_excluded(bullet)
                    )
                }
            )
            for role in self.draft.roles
            if role.role_key in recent
        )
        earlier = self.draft.earlier_career if self._earlier() else None
        return self.draft.model_copy(update={"roles": roles, "earlier_career": earlier})

    def _blocks(self) -> dict[str, str]:
        """Return every block in page order, empty where the document omits it."""
        return {
            "header": self.header(),
            "summary": self.summary(),
            "skills": self.skills(),
            "experience": self.experience(),
            "education": self.education(),
            "publications": self.publications(),
        }

    def header(self) -> str:
        """Return the name, contact line, and work authorization statement."""
        return header_markdown(
            self.inventory, self.settings.header_drop, self.guardrails.work_authorization
        )

    def summary(self) -> str:
        """Return the summary section."""
        text = self.draft.summary.text.strip()
        return f"## {self.settings.sections.summary}\n\n{text}" if text else ""

    def skills(self) -> str:
        """Return the labeled skills section, in the shape labeled sections need."""
        lines = [
            f"**{skill.label.strip().rstrip(':')}:** {skill.text.strip()}"
            for skill in self.draft.skills
            if skill.text.strip()
        ]
        if not lines:
            return ""
        return f"## {self.settings.sections.skills}\n\n" + "\n\n".join(lines)

    def experience(self) -> str:
        """Return every role block, most recent first, then earlier career."""
        drafted = {role.role_key: role for role in self.draft.roles}
        blocks = [
            block
            for role_key in self.plan.recent_roles
            if (block := self._role(role_key, drafted.get(role_key)))
        ]
        if earlier := self._earlier():
            blocks.append(earlier)
        if not blocks:
            return ""
        return f"## {self.settings.sections.experience}\n\n" + "\n\n".join(blocks)

    def education(self) -> str:
        """Return the education section, copied verbatim from the master CV.

        Each detail follows a two-space hard line break, so it prints on its own line.
        """
        entries = self._entries(self.plan.education_ids)
        if not entries:
            return ""
        return f"## {self.settings.sections.education}\n\n" + "\n\n".join(
            "  \n".join((entry.text, *entry.details)) for entry in entries
        )

    def publications(self) -> str:
        """Return the selected publications, copied verbatim with details as nested items."""
        entries = [
            entry
            for entry in self._entries(self.plan.publication_ids)
            if entry.id not in self.excluded
        ]
        if not entries:
            return ""
        return f"## {self.settings.sections.publications}\n\n" + "\n".join(
            f"- {entry.full_text}" for entry in entries
        )

    def _role(self, role_key: str, drafted: DraftedRole | None) -> str:
        """Return one role: verbatim metadata from the CV, drafted prose beneath."""
        role = self.inventory.role(role_key)
        if role is None:  # pragma: no cover - plan keys come from the inventory
            return ""
        lines = [role_heading(role)]
        if drafted is None:
            return lines[0]
        if drafted.context is not None and drafted.context.text.strip():
            lines += ["", drafted.context.text.strip()]
        if bullets := self._bullets(drafted):
            lines += ["", *bullets]
        return "\n".join(lines).rstrip()

    def _bullets(self, drafted: DraftedRole) -> list[str]:
        """Return the surviving bullets for one role."""
        return [
            f"- {bullet.text.strip()}"
            for bullet in drafted.bullets
            # Drop a bullet only when all of its sources were removed.
            if bullet.text.strip() and not self._fully_excluded(bullet)
        ]

    def _fully_excluded(self, line: DraftedLine) -> bool:
        """Return whether trimming removed every source this line rests on."""
        return bool(line.source_ids) and set(line.source_ids) <= self.excluded

    def _earlier(self) -> str:
        """Return the collapsed earlier-career block with a real date span."""
        if self.draft.earlier_career is None or not self.plan.earlier_roles:
            return ""
        heading = earlier_heading(
            [role for key in self.plan.earlier_roles if (role := self.inventory.role(key))]
        )
        if not heading:
            return ""
        text = self.draft.earlier_career.text.strip()
        return f"{heading}\n\n{text}" if text else heading

    def _entries(self, ids: tuple[str, ...]) -> list[Entry]:
        """Look up entries by identifier, skipping any that no longer exist."""
        found = []
        for entry_id in ids:
            try:
                found.append(self.inventory.by_id(entry_id))
            except KeyError:  # pragma: no cover - plan ids come from the inventory
                continue
        return found
