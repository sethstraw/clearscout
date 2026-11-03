"""Ask about work the master CV does not record, and refuse to invent any of it.

The only place in clearscout where a claim can come into existence, and the only
path that writes to the master CV. Nothing here asks a model anything.
"""

from .conversation import (
    Confirmed,
    Line,
    Outcome,
    Question,
    Reply,
    confirm,
    confirm_with,
    converse,
    discover,
    gather,
    lines_for,
    unclosed,
)
from .gates import Pending, Placement, propose
from .journal import Written, write
from .plan import Edit, plan, preview, rewritten
from .vocabulary import (
    FUNCTION_WORDS,
    JOURNAL,
    Angle,
    Ownership,
    Point,
    Source,
    reads_as,
    unsourced_words,
)

__all__ = [
    "FUNCTION_WORDS",
    "JOURNAL",
    "Angle",
    "Confirmed",
    "Edit",
    "Line",
    "Outcome",
    "Ownership",
    "Pending",
    "Placement",
    "Point",
    "Question",
    "Reply",
    "Source",
    "Written",
    "confirm",
    "confirm_with",
    "converse",
    "discover",
    "gather",
    "lines_for",
    "plan",
    "preview",
    "propose",
    "reads_as",
    "rewritten",
    "unclosed",
    "unsourced_words",
    "write",
]
