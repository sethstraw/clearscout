"""Phrase patterns that recognise how a posting states its work arrangement."""

from __future__ import annotations

import re

OFFERED_WORDS = (
    "option",
    "options",
    "optional",
    "benefit",
    "benefits",
    "perk",
    "perks",
    "offer",
    "offers",
    "offered",
    "offerings",
)
ROLE_ARRANGEMENT_REFERENCE = re.compile(r"\b(?:this|the) (?:role|position|job)\b", re.IGNORECASE)
ARRANGEMENT_WORD = r"(?:remote|hybrid|on[- ]?site|in[- ]office)"
ARRANGEMENT_LABEL = re.compile(
    r"\b(?:work\s+(?:model|arrangement|type|setting|style)|workplace(?:\s+type)?"
    r"|location\s+type)\s*:",
    re.IGNORECASE,
)
_IN_PLACE = r"(?:hybrid|on[- ]?site|in[- ]office|in[- ]person|in\s+the\s+office)"
AWAY = r"(?:remote(?:ly)?|virtual(?:ly)?|(?:at|from)\s+home)"
ANY_PLACE = rf"(?:{_IN_PLACE}|{AWAY})"
IN_PLACE_WORD = re.compile(rf"\b{_IN_PLACE}\b", re.IGNORECASE)
# A clause is about this role only when it opens on the role, the candidate, the
# reader under a commitment verb, or the bare requirement. "The team you support"
# and "the manager for this role" open on somebody else.
ROLE_SUBJECT = re.compile(
    r"^\W*(?:about\s+(?:this|the)\s+(?:role|position|job)\s*:?\s+)?"
    r"(?:(?:this|the)\s+(?:role|position|job)\b"
    r"|this\s+is\s+an?\s+(?:hybrid|on[- ]?site|in[- ]office)\b"
    r"|(?:(?:the|a|an)\s+)?(?:(?:successful|ideal)\s+)?(?:candidate|incumbent)\b"
    r"(?!'|\N{RIGHT SINGLE QUOTATION MARK})"
    r"|you(?:'ll|\s+(?:will|must|shall|should|need\s+to)|\s+are\s+(?:required|expected))\b"
    r"|(?:must|required|expected)\s+(?:to\s+)?(?:be|work|attend|report)\b)",
    re.IGNORECASE,
)
