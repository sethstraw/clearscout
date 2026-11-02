"""Extract a publisher-stated arrangement token from a Workday CXS record."""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from typing import Any

from tailorcv.domain.arrangement_grammar import attendance_statement, remote_offer_statement
from tailorcv.infrastructure.web_fetch.listing_facts import (
    REMOTE_PLACE,
    ArrangementSignal,
    linkedin_arrangement_signal,
    remote_checked_arrangement,
    without_linkedin_arrangement_tags,
)

_STRUCTURED_FIELDS: tuple[str, ...] = ("remoteType", "workRemoteType")

_NORMALIZE: dict[str, str] = {
    "yes": "remote",
    "fully remote": "remote",
    "remote": "remote",
    # A choice between office and home lets the role be worked from home.
    "office or home": "remote",
    "home or office": "remote",
    "sometimes": "hybrid",
    "hybrid": "hybrid",
    "flexible": "hybrid",
    "no": "onsite",
    "on-site": "onsite",
    "onsite": "onsite",
    "on site": "onsite",
    "in person": "onsite",
    "in-person": "onsite",
    "office-based": "onsite",
    "office based": "onsite",
}
# remoteType is a list each tenant defines, so a label may wrap a known word in its own.
_NAMED_ARRANGEMENT = re.compile(r"\b(?:remote|hybrid|on[- ]?site)\b", re.IGNORECASE)

_PHRASE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bremote[- ]first\b"), "remote"),
    (re.compile(r"\bfully[- ]remote\b"), "remote"),
    (re.compile(r"\bhybrid\b"), "hybrid"),
    (re.compile(r"\bfully[- ]on[- ]?site\b"), "onsite"),
)

# The grammar's attendance kinds, in this reader's arrangement tokens.
_BODY_KIND: dict[str, str] = {"hybrid": "hybrid", "on_site": "onsite"}

_REMOTE_ROLE = re.compile(
    r"^(?:travel:\s*)?(?:while\s+)?(?:this|the)\s+(?:"
    r"(?:role|position|job)\s+(?:is|will\s+be)\s+(?:fully\s+)?remote\b"
    r"|(?:is|will\s+be)\s+(?:a\s+)?(?:fully\s+)?remote\s+(?:role|position|job)\b)",
    re.IGNORECASE,
)

_VIRTUAL_AS_NEEDED = re.compile(
    r"\bwork virtually\b.{0,120}\b(?:come and go from|use|utilize) (?:the |our )?offices?"
    r".{0,60}\bas needed\b",
    re.DOTALL,
)


def workday_arrangement_facts(
    row: dict[str, Any], detail: dict[str, Any] | None, places: Sequence[str]
) -> ArrangementSignal:
    """Return a publisher arrangement answer or unresolved contradictory source text.

    A remote place in ``places``, or a remote offer in the body, leaves a
    hybrid or on-site answer unresolved.
    """
    body = detail.get("jobDescription") if detail is not None else None
    if isinstance(body, str) and body and _virtual_as_needed(body):
        return ArrangementSignal(value="remote")
    for source in (detail, row):
        structured = _structured_signal(source)
        if structured is None:
            continue
        if structured.value is None:
            return structured
        return remote_checked_arrangement(structured.value, places, body)
    if isinstance(body, str) and body:
        return _body_signal(body, places)
    return ArrangementSignal()


def _body_signal(body: str, places: Sequence[str]) -> ArrangementSignal:
    tag = linkedin_arrangement_signal(body)
    if tag.value is not None:
        return remote_checked_arrangement(tag.value, places, body)
    if tag.unresolved:
        return tag
    text = without_linkedin_arrangement_tags(body)
    offer = remote_offer_statement(text)
    attendance = attendance_statement(text)
    remote_places = tuple(place for place in places if REMOTE_PLACE.search(place))
    if attendance is not None and (offer is not None or remote_places):
        return ArrangementSignal(
            unresolved=(attendance[1], *((offer,) if offer is not None else remote_places)),
            provenance_suffix="body_phrase.arrangement",
        )
    if offer is not None and _REMOTE_ROLE.search(offer):
        return ArrangementSignal(value="remote", provenance_suffix="body_phrase.arrangement")
    if attendance is not None:
        return ArrangementSignal(
            value=_BODY_KIND[attendance[0]], provenance_suffix="body_phrase.arrangement"
        )
    if _dominant_body_signal(text) == "remote":
        return ArrangementSignal(value="remote", provenance_suffix="body_phrase.arrangement")
    return ArrangementSignal()


def _virtual_as_needed(body: str) -> bool:
    normalized = html.unescape(re.sub(r"<[^>]+>", " ", body)).lower()
    return _VIRTUAL_AS_NEEDED.search(normalized) is not None


def _structured_signal(source: Any) -> ArrangementSignal | None:
    if not isinstance(source, dict):
        return None
    label = _field_label(source) or _work_location_label(source)
    if label:
        return _labelled(label)
    stated = _bullet_signal(source) or _locations_text_signal(source)
    return ArrangementSignal(value=stated) if stated is not None else None


def _field_label(source: dict[str, Any]) -> str:
    for name in _STRUCTURED_FIELDS:
        value = _read_descriptor(source.get(name))
        if value:
            return value
    return ""


def _work_location_label(source: dict[str, Any]) -> str:
    work_locations = source.get("workLocations")
    if not isinstance(work_locations, list) or not work_locations:
        return ""
    first = work_locations[0]
    return _read_descriptor(first.get("remoteType")) if isinstance(first, dict) else ""


def _bullet_signal(source: dict[str, Any]) -> str | None:
    bullets = source.get("bulletFields")
    if not isinstance(bullets, list):
        return None
    for item in bullets:
        if not (isinstance(item, str) and item.strip()):
            continue
        normalized = _NORMALIZE.get(item.strip().lower())
        if normalized:
            return normalized
    return None


def _locations_text_signal(source: dict[str, Any]) -> str | None:
    locations_text = source.get("locationsText")
    if not isinstance(locations_text, str) or not locations_text.strip():
        return None
    return _NORMALIZE.get(locations_text.strip().lower())


def _read_descriptor(value: Any) -> str:
    if isinstance(value, dict):
        descriptor = value.get("descriptor")
        return str(descriptor).strip() if descriptor else ""
    if isinstance(value, str):
        return value.strip()
    return ""


def _labelled(label: str) -> ArrangementSignal:
    """Read a tenant's label; one naming no known arrangement is unresolved, never a fact."""
    known = _NORMALIZE.get(label.strip().lower())
    if known is not None:
        return ArrangementSignal(value=known)
    if _NAMED_ARRANGEMENT.search(label):
        return ArrangementSignal(value=label.strip())
    return ArrangementSignal(unresolved=(label.strip(),))


def _dominant_body_signal(text: str) -> str | None:
    """Return the one arrangement word the body uses, or None when it uses several."""
    normalized = html.unescape(re.sub(r"<[^>]+>", " ", text)).lower()
    matched: set[str] = set()
    for pattern, arrangement in _PHRASE_PATTERNS:
        if pattern.search(normalized):
            matched.add(arrangement)
    if len(matched) != 1:
        return None
    return next(iter(matched))
