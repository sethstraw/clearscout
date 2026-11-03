"""Required application-form questions a board publishes, read for a US-state residence rule."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from typing import Any

from clearscout.domain.artifacts import PostingSource
from clearscout.domain.countries import subdivision_name, subdivision_names, territory_sovereign

# A question about which state the applicant lives in, not where the job sits.
_RESIDENCE_LABEL = re.compile(
    r"\bstates?\b.*\b(?:reside|resides|residing|residence|resident|live|lives|living|located|based)\b"
    r"|\b(?:reside|resides|residing|residence|resident|live|lives|living|located|based)\b.*\bstates?\b",
    re.IGNORECASE,
)
_US_STATES = frozenset(name.casefold() for name in subdivision_names("United States"))

# One question: its label, whether the form requires it, and each select field's option labels.
type _Question = tuple[str, bool, list[list[str]]]


def _us_state(option: str) -> bool:
    text = option.strip()
    return (
        text.casefold() in _US_STATES
        or subdivision_name("United States", text.upper()) is not None
        or territory_sovereign(text) == "United States"
    )


def required_residence_question(source: PostingSource) -> str | None:
    """Return a required question asking the applicant's US state that offers nothing else."""
    return next(
        (
            label
            for label, required, option_sets in _questions(source)
            if required
            and _RESIDENCE_LABEL.search(label)
            and any(
                options and all(_us_state(option) for option in options) for options in option_sets
            )
        ),
        None,
    )


def _questions(source: PostingSource) -> Iterator[_Question]:
    if source.publisher == "greenhouse":
        # Greenhouse states its form in the single-job record read with ``questions=true``.
        record = _loaded(source.record)
        yield from _greenhouse_questions(record) if isinstance(record, Mapping) else ()
    elif source.publisher == "lever":
        cards = _loaded(source.application_form)
        yield from _lever_questions(cards) if isinstance(cards, list) else ()
    elif source.publisher == "ashby":
        form = _loaded(source.application_form)
        yield from _ashby_questions(form) if isinstance(form, Mapping) else ()


def _loaded(text: str) -> Any:
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def _mappings(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _labels(values: Any, key: str) -> list[str]:
    return [str(value.get(key) or "") for value in _mappings(values)]


def _greenhouse_questions(record: Mapping[str, Any]) -> Iterator[_Question]:
    for question in _mappings(record.get("questions")):
        fields = _mappings(question.get("fields"))
        yield (
            str(question.get("label") or ""),
            question.get("required") is True,
            [_labels(field["values"], "label") for field in fields if "values" in field],
        )


def _lever_questions(cards: list[Any]) -> Iterator[_Question]:
    for card in _mappings(cards):
        for field in _mappings(card.get("fields")):
            yield (
                str(field.get("text") or ""),
                field.get("required") is True,
                [_labels(field.get("options"), "text")],
            )


def _ashby_questions(form: Mapping[str, Any]) -> Iterator[_Question]:
    for section in _mappings(form.get("sections")):
        for entry in _mappings(section.get("fieldEntries")):
            field = entry.get("field")
            if isinstance(field, Mapping):
                yield (
                    str(field.get("title") or ""),
                    entry.get("isRequired") is True,
                    [_labels(field.get("selectableValues"), "label")],
                )
