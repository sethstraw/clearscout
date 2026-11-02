"""Deterministic context shared across model-assisted stages."""

import re
from collections.abc import Sequence

from tailorcv.application.master_cv.cv import CvInventory, Entry, EntryKind, Role
from tailorcv.application.prompts import data_block
from tailorcv.application.tailoring.guardrails import Guardrails
from tailorcv.application.tailoring.stages.jobspec import JobSpec

CITABLE = (
    EntryKind.BULLET,
    EntryKind.ROLE_CONTEXT,
    EntryKind.SKILL,
    EntryKind.SUMMARY,
    EntryKind.PUBLICATION,
    EntryKind.EDUCATION,
    EntryKind.CREDENTIAL,
    EntryKind.APPOINTMENT,
    EntryKind.AWARD,
    EntryKind.PRESENTATION,
    EntryKind.WRITING,
    EntryKind.MEMBERSHIP,
)

# Immigration status uses a small fixed vocabulary.
AUTHORIZATION_MARKERS = (
    "work authorization",
    "authorized to work",
    "eligible to work",
    "right to work",
    "citizen",
    "permanent resident",
    "sponsorship",
    "work permit",
    "work visa",
    "green card",
)

# Wording that limits the applicant's part to taking part in the work. "Supported" is left
# out because a CV routinely uses it for scope, as in "supported 3M users".
PARTICIPATION = re.compile(
    r"\b(?:helped|contributed to|worked on|participated in)\b", re.IGNORECASE
)


def citable_entries(inventory: CvInventory) -> tuple[Entry, ...]:
    """Return the entries a requirement may cite: everything factual, minus structural noise."""
    return inventory.of_kind(*CITABLE)


def _evidence_inventory_text(entries: tuple[Entry, ...], inventory: CvInventory) -> str:
    """Render the evidence inventory as the constant prefix every stage shares.

    Grouped by role and section so the model can see recency and context, and
    stable in ordering so that provider prompt caching actually hits.
    """
    lines: list[str] = []
    current_scope: str | None = None
    for entry in entries:
        scope = entry.role_key or entry.section
        if scope != current_scope:
            current_scope = scope
            role = inventory.role(scope) if entry.role_key else None
            if role is not None:
                header = f"{role.organization} - {role.title or 'role'} ({role.dates or 'undated'})"
            else:
                header = entry.section
            lines.append(f"\n## {header}")
        lines.append(entry_line(entry))
    return "\n".join(lines).strip()


def entry_line(entry: Entry) -> str:
    """Return one entry as the candidate record shows it: identifier, label, text, and details."""
    label = f"**{entry.label}:** " if entry.label else ""
    return f"[{entry.id}] {label}{entry.full_text}"


def record_block(inventory: CvInventory) -> str:
    """Return the candidate record, byte-identical in every stage for the provider prompt cache."""
    return data_block(
        "candidate_record", _evidence_inventory_text(citable_entries(inventory), inventory)
    )


def chronology_block(inventory: CvInventory) -> str:
    """Return every role's employer, title, and dates, with no bullets.

    A gap or an overlap is visible only against every role.
    """
    lines = [
        f"{role.organization} - {role.title or 'role'} ({role.dates or 'undated'})"
        for role in inventory.roles
    ]
    return data_block("candidate_chronology", "\n".join(lines))


def requirement_text(spec: JobSpec) -> str:
    """Return the posting's requirements as both stages that read them send them.

    One function, so a change to how a requirement is identified or weighted
    reaches matching and criticism rather than one of them.
    """
    return "\n".join(
        f"[{requirement.id}] ({requirement.importance.value}, "
        f"{requirement.dimension.value}) {requirement.text}"
        for requirement in spec.requirements
    )


def asserted_text(inventory: CvInventory, guardrails: Guardrails) -> str:
    """Return the facts about the applicant that a requirement may be answered by.

    Contact details, titles of record, and a work authorization line are in no
    evidence inventory, and unmarked a requirement about status, eligibility, or
    location reads as a gap.
    """
    lines: list[str | None] = [*inventory.contact_lines]
    if guardrails.work_authorization:
        lines.append(guardrails.work_authorization)
    # A role's location is where that work was, and says nothing about where the
    # applicant now lives, so a residency requirement cannot be answered by one.
    lines.extend(role.title_of_record for role in inventory.roles)
    lines.extend(guardrails.protected)
    return _distinct_lines(lines)


def copied_text(inventory: CvInventory, guardrails: Guardrails) -> str:
    """Return the lines in the document that no model wrote and none may judge.

    The name, contact details, role metadata, and a work authorization line are
    in no evidence inventory, and unmarked they read as unsupported claims.
    """
    lines: list[str | None] = [inventory.name, *inventory.contact_lines]
    if guardrails.work_authorization:
        lines.append(guardrails.work_authorization)
    # Every field the assembler copies into a role header, whatever the draft says.
    for role in inventory.roles:
        lines += [role.organization, role.title, role.title_of_record, role.dates, role.location]
    lines.extend(guardrails.protected)
    return _distinct_lines(lines)


def _distinct_lines(lines: list[str | None]) -> str:
    """Return the non-empty lines, stripped, in order, each appearing once."""
    seen = dict.fromkeys(line.strip() for line in lines if line and line.strip())
    return "\n".join(seen)


def contact_is_dropped(contact: str, prefixes: tuple[str, ...]) -> bool:
    """Return whether this contact line starts, in any case, with a prefix derivatives omit."""
    stripped = contact.strip().casefold()
    return any(
        stripped.startswith(prefix.strip().casefold()) for prefix in prefixes if prefix.strip()
    )


def states_work_authorization(line: str) -> bool:
    """Return whether a line's subject is the applicant's right to work."""
    lowered = line.casefold()
    return any(marker in lowered for marker in AUTHORIZATION_MARKERS)


def _kept_contacts(
    inventory: CvInventory, header_drop: tuple[str, ...], authorization: str | None
) -> list[str]:
    """Return the master CV's contact lines a derived document keeps, in order."""
    return [
        contact
        for contact in inventory.contact_lines
        # The guardrails' statement replaces the master CV's only when one is set.
        if not (authorization and states_work_authorization(contact))
        and not contact_is_dropped(contact, header_drop)
    ]


def header_markdown(
    inventory: CvInventory, header_drop: tuple[str, ...], authorization: str | None
) -> str:
    """Return the name, the kept contact lines, and the work authorization statement.

    All of it is copied, from the master CV and from guardrails, never composed.
    """
    lines = [f"# {inventory.name}"]
    for contact in _kept_contacts(inventory, header_drop, authorization):
        lines += ["", contact]
    if authorization:
        lines += ["", authorization]
    return "\n".join(lines)


def letterhead_markdown(inventory: CvInventory, header_drop: tuple[str, ...]) -> str:
    """Return the name and the first kept contact line, copied from the master CV.

    A letter states no work authorization, so a contact line that does is passed over.
    """
    contact = next(
        (
            line
            for line in _kept_contacts(inventory, header_drop, None)
            if not states_work_authorization(line)
        ),
        None,
    )
    return f"# {inventory.name}" if contact is None else f"# {inventory.name}\n\n{contact}"


def role_heading(role: Role) -> str:
    """Return a role's heading and the title, title of record, and dates copied beneath it."""
    lines = [f"### {role.organization}", ""]
    if role.title:
        lines.append(f"**{role.title}**  ")
    if role.title_of_record:
        lines.append(f"(title of record: {role.title_of_record})  ")
    if role.dates:
        location = f" | {role.location}" if role.location else ""
        lines.append(f"{role.dates}{location}")
    return "\n".join(lines).rstrip()


def earlier_heading(roles: Sequence[Role]) -> str:
    """Return the collapsed earlier-career heading and the date span its roles cover.

    The span is stitched from the source date strings rather than rebuilt from
    parsed years, so month names and style stay as the master CV wrote them.
    """
    if not roles:
        return ""
    oldest = min(roles, key=lambda role: role.start_year or 9999)
    newest = max(roles, key=lambda role: role.end_year or role.start_year or 0)
    start = (oldest.dates or "").split(" to ")[0].strip()
    end = (newest.dates or "").split(" to ")[-1].strip()
    span = f"{start} to {end}" if start and end else (start or end)
    return "### Earlier Career" + (f"\n\n{span}" if span else "")
