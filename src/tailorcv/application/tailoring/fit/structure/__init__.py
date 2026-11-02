"""Posting structure: the reading models and the block, heading and requirement readers."""

from tailorcv.application.tailoring.fit.structure.blocks import (
    posting_blocks,
)
from tailorcv.application.tailoring.fit.structure.boilerplate import (
    content_lines,
    is_boilerplate,
)
from tailorcv.application.tailoring.fit.structure.quotes import (
    first_saying,
    posting_title,
    quote_line,
)
from tailorcv.application.tailoring.fit.structure.reading import (
    Answer,
    AnswerName,
    Block,
    Disagreement,
    Reading,
    RequirementLine,
    SectionKind,
    Stance,
    unasked_answer,
)
from tailorcv.application.tailoring.fit.structure.requirements import (
    counted_at_stance,
    requirement_lines,
    requirement_sections,
)
from tailorcv.application.tailoring.fit.structure.waivers import (
    ASIDE,
    blanked,
    preference_asides,
    unwaived,
    waived_spans,
)

__all__ = [
    "ASIDE",
    "Answer",
    "AnswerName",
    "Block",
    "Disagreement",
    "Reading",
    "RequirementLine",
    "SectionKind",
    "Stance",
    "blanked",
    "content_lines",
    "counted_at_stance",
    "first_saying",
    "is_boilerplate",
    "posting_blocks",
    "posting_title",
    "preference_asides",
    "quote_line",
    "requirement_lines",
    "requirement_sections",
    "unasked_answer",
    "unwaived",
    "waived_spans",
]
