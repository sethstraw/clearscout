"""Extract someone's CV into the shape this tool reads, cleaning it up on the way.

The model writes rather than labels source lines, because a bullet that wrapped
across three lines in a PDF arrives as three lines and a packed header arrives
as one. Every field it emits carries the source line numbers it came from, those
numbers are a ``Literal`` over the lines that exist, and what it wrote is checked
word by word against what it cited, dates included.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from tailorcv.application.config import Stage
from tailorcv.application.master_cv.sources import SourceDocument
from tailorcv.application.prompts import render
from tailorcv.infrastructure.providers.client import Client
from tailorcv.infrastructure.providers.plan import RequestPlan, Segment
from tailorcv.shared.errors import IngestError


class Sourced(BaseModel):
    """One piece of text, and the source lines it was taken from."""

    model_config = ConfigDict(frozen=True)

    text: str
    lines: tuple[int, ...] = ()


class ImportedRole(BaseModel):
    """One position, split into the fields the document contract expects."""

    model_config = ConfigDict(frozen=True)

    organisation: Sourced
    title: Sourced | None = None
    title_of_record: Sourced | None = None
    dates: Sourced | None = None
    location: Sourced | None = None
    context: tuple[Sourced, ...] = ()
    bullets: tuple[Sourced, ...] = ()


class ImportedSection(BaseModel):
    """A section of the CV that no named field holds, under its own heading.

    The heading is copied from the source like any other field, so grants,
    awards, presentations, service, certifications, and languages keep the words
    the CV gave them instead of being filed under a field that means something
    else.
    """

    model_config = ConfigDict(frozen=True)

    heading: Sourced
    items: tuple[Sourced, ...] = ()


class Dropped(BaseModel):
    """A source line deliberately left out, and why."""

    model_config = ConfigDict(frozen=True)

    line: int
    reason: str


class ImportedCv(BaseModel):
    """A whole CV, extracted into the shape this tool reads."""

    model_config = ConfigDict(frozen=True)

    name: Sourced
    contact: tuple[Sourced, ...] = ()
    summary: tuple[Sourced, ...] = ()
    skills: tuple[Sourced, ...] = ()
    roles: tuple[ImportedRole, ...] = ()
    education: tuple[Sourced, ...] = ()
    publications: tuple[Sourced, ...] = ()
    sections: tuple[ImportedSection, ...] = ()
    dropped: tuple[Dropped, ...] = ()

    def sourced(self) -> list[Sourced]:
        """Return every piece of text the model wrote, for checking."""
        pieces = [self.name, *self.contact, *self.summary, *self.skills]
        pieces += [*self.education, *self.publications]
        for section in self.sections:
            pieces += [section.heading, *section.items]
        for role in self.roles:
            optional = (role.title, role.title_of_record, role.dates, role.location)
            pieces += [role.organisation, *(field for field in optional if field)]
            pieces += [*role.context, *role.bullets]
        return pieces


def _require_lines(numbers: tuple[int, ...]) -> tuple[int, ...]:
    """Refuse to build a schema over an empty vocabulary.

    ``Literal[()]`` fails inside pydantic with a bare assertion, and assertions
    vanish under ``python -O``, which would leave a silently broken schema.
    """
    if not numbers:
        raise IngestError(
            "No usable lines came out of that document, so there is nothing to structure.\n"
            "  If it is a scan or an image-only PDF, screenshot it and import the image."
        )
    return numbers


def response_schema(source: SourceDocument) -> type[BaseModel]:
    """Build a response schema closed over this document's real line numbers."""
    numbers = _require_lines(tuple(line.number for line in source.lines))
    line_literal = Literal[numbers]  # type: ignore[valid-type]
    sourced = create_model(
        "ConstrainedSourced",
        __config__=ConfigDict(extra="forbid"),
        text=(str, ...),
        lines=(list[line_literal], Field(default_factory=list)),
    )
    dropped = create_model(
        "ConstrainedDropped",
        __config__=ConfigDict(extra="forbid"),
        line=(line_literal, ...),
        reason=(str, ...),
    )
    role = create_model(
        "ConstrainedRole",
        __config__=ConfigDict(extra="forbid"),
        organisation=(sourced, ...),
        title=(sourced | None, None),
        title_of_record=(sourced | None, None),
        dates=(sourced | None, None),
        location=(sourced | None, None),
        context=(list[sourced], Field(default_factory=list)),  # type: ignore[valid-type]
        bullets=(list[sourced], Field(default_factory=list)),  # type: ignore[valid-type]
    )
    section = create_model(
        "ConstrainedSection",
        __config__=ConfigDict(extra="forbid"),
        heading=(sourced, ...),
        items=(list[sourced], Field(default_factory=list)),  # type: ignore[valid-type]
    )
    return create_model(
        "ConstrainedImportedCv",
        __config__=ConfigDict(extra="forbid"),
        name=(sourced, ...),
        contact=(list[sourced], Field(default_factory=list)),  # type: ignore[valid-type]
        summary=(list[sourced], Field(default_factory=list)),  # type: ignore[valid-type]
        skills=(list[sourced], Field(default_factory=list)),  # type: ignore[valid-type]
        roles=(list[role], Field(default_factory=list)),  # type: ignore[valid-type]
        education=(list[sourced], Field(default_factory=list)),  # type: ignore[valid-type]
        publications=(list[sourced], Field(default_factory=list)),  # type: ignore[valid-type]
        sections=(list[section], Field(default_factory=list)),  # type: ignore[valid-type]
        dropped=(list[dropped], Field(default_factory=list)),  # type: ignore[valid-type]
    )


def structure(client: Client, source: SourceDocument) -> ImportedCv:
    """Extract the CV, letting the model tidy but not invent."""
    policy = client.settings.policy_for(Stage.STRUCTURE)
    plan = RequestPlan(
        stage=Stage.STRUCTURE,
        instructions=Segment(render("structure_system"), "structure_system"),
        task=Segment(render("structure_user", source=source.numbered()), "structure_user"),
        max_tokens=policy.max_tokens,
    )
    response = client.complete(plan, response_schema(source))
    return ImportedCv.model_validate(response.model_dump(mode="json"))
