"""Synthetic application forms in each board's published shape, asking one select question."""

from __future__ import annotations

import json
from html import escape
from typing import Literal

from tailorcv.domain.artifacts import PostingSource

FormPlatform = Literal["greenhouse", "lever", "ashby"]
FORM_PLATFORMS: tuple[FormPlatform, ...] = ("greenhouse", "lever", "ashby")
US_STATE_OPTIONS = ("Alabama", "California", "District of Columbia", "NY", "Puerto Rico", "Texas")


def lever_cards(label: str, options: tuple[str, ...], *, required: bool = True) -> list[object]:
    """Lever's custom-question cards, as each card's ``baseTemplate`` states it."""
    field = {
        "type": "dropdown",
        "text": label,
        "required": required,
        "options": [{"text": option} for option in options],
    }
    return [{"text": "Location questions", "fields": [field]}]


def lever_apply_page(cards: list[object]) -> str:
    """A Lever apply page carrying each card's template in a hidden input."""
    inputs = "".join(
        f'<input type="hidden" value="{escape(json.dumps(card))}" '
        f'name="cards[card-{index}][baseTemplate]">'
        for index, card in enumerate(cards)
    )
    return f"<html><body><form>{inputs}</form></body></html>"


def ashby_form(label: str, options: tuple[str, ...], *, required: bool = True) -> dict[str, object]:
    """Ashby's ``applicationForm``, as its job page's posting query answers it."""
    field = {
        "type": "ValueSelect",
        "title": label,
        "selectableValues": [{"label": option, "value": option} for option in options],
    }
    return {"sections": [{"fieldEntries": [{"isRequired": required, "field": field}]}]}


def form_source(
    platform: FormPlatform, label: str, options: tuple[str, ...], *, required: bool = True
) -> PostingSource:
    """The posting source a body read keeps when the form asks one select question."""
    if platform == "lever":
        cards = lever_cards(label, options, required=required)
        return PostingSource(publisher="lever", record="{}", application_form=json.dumps(cards))
    if platform == "ashby":
        form = ashby_form(label, options, required=required)
        return PostingSource(publisher="ashby", record="{}", application_form=json.dumps(form))
    values = [{"label": option, "value": index} for index, option in enumerate(options)]
    field = {"name": "question_1", "type": "multi_value_single_select", "values": values}
    question = {"required": required, "label": label, "fields": [field]}
    record = {"id": 1, "title": "Operations Lead", "questions": [question]}
    return PostingSource(publisher="greenhouse", record=json.dumps(record))
