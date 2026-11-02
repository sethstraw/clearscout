"""The content rules a person will not let a resume break.

String rules (required, forbidden, protected, first person, ASCII) run as
regular expressions. Distinctions are judgment calls, injected verbatim into the
drafting and critic prompts.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field


class Guardrails(BaseModel):
    """The ``[rules]`` table: everything a person refuses to have written wrong."""

    model_config = ConfigDict(extra="forbid")

    protected: list[str] = Field(
        default_factory=list,
        description="Phrases that must appear verbatim whenever they are mentioned.",
    )
    required: list[str] = Field(
        default_factory=list,
        description=(
            "Phrases the assembled resume must contain, checked against the contact "
            "header and other text copied from the master CV rather than drafted."
        ),
    )
    forbidden: list[str] = Field(
        default_factory=list, description="Phrases that must never appear."
    )
    work_authorization: str | None = Field(
        default=None, description="Line printed under the contact details on every derivative."
    )
    distinctions: list[str] = Field(
        default_factory=list,
        description="Sentences separating what you did from what a keyword would imply.",
    )
    first_person: bool = Field(
        default=False, description="Whether first-person pronouns are allowed in the resume."
    )
    ascii_only: bool = Field(
        default=True, description="Whether to require ASCII punctuation throughout."
    )

    # Code-owned prompt content is included in the suite fingerprint.
    FORBIDDEN_INSTRUCTION: ClassVar[str] = "Never write any of these, in any form:"
    PROTECTED_INSTRUCTION: ClassVar[str] = (
        "Write each of these verbatim whenever you mention it, in any case. "
        "Never abbreviate or paraphrase one:"
    )
    NO_DISTINCTIONS: ClassVar[str] = "(No additional distinction rules are configured.)"
    # This is the empty template written by `tailorcv init`.
    PLACEHOLDER: ClassVar[str] = (
        "No fabricated roles, dates, metrics, employers, degrees, or capabilities."
    )

    def prompt_block(self) -> str:
        """Render the rules the model can satisfy or break in its own drafted text.

        ``required`` is excluded: it is checked against text the program copies
        verbatim, which the model never writes.
        """
        sections = []
        if self.distinctions:
            sections.append(
                "\n".join(f"{index}. {rule}" for index, rule in enumerate(self.distinctions, 1))
            )
        else:
            sections.append(self.NO_DISTINCTIONS)
        if self.forbidden:
            sections.append(
                self.FORBIDDEN_INSTRUCTION
                + "\n"
                + "\n".join(f"- {phrase}" for phrase in self.forbidden)
            )
        if self.protected:
            sections.append(
                self.PROTECTED_INSTRUCTION
                + "\n"
                + "\n".join(f"- {phrase}" for phrase in self.protected)
            )
        return "\n\n".join(sections)

    @property
    def personal_distinctions(self) -> list[str]:
        """Return the distinctions the user actually wrote."""
        return [rule for rule in self.distinctions if rule.strip() != self.PLACEHOLDER]

    @property
    def configured(self) -> bool:
        """Return whether any rule the user chose is in force."""
        return bool(
            self.protected
            or self.required
            or self.forbidden
            or self.personal_distinctions
            or self.work_authorization
            or self.first_person
            or not self.ascii_only
        )
