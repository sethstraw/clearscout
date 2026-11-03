"""Reasoning and evidence precede the label in every judged response schema.

Structured-output models fill fields in declaration order, so emitting the
evidence and reasoning fields before the field that commits to a label or score
makes the model reason before it decides. Match and the relevance review send a
runtime create_model schema, not their internal validation model, so those two
assert on the schema actually sent to the provider.
"""

from __future__ import annotations

from typing import get_args

from pydantic import BaseModel

from clearscout.application.discovery.relevance_review import response_schema
from clearscout.application.tailoring.stages.jobspec import RawRequirement
from clearscout.application.tailoring.stages.match import response_schema as match_response_schema
from clearscout.application.tailoring.stages.verify import CriticVerdict

_FINDINGS = ("unsupported_claims", "style_flags", "weaknesses", "guidance")
_SCORES = ("alignment", "preservation", "specificity", "readability", "ats_clarity", "human_voice")


def _before(model: type[BaseModel], earlier: str, later: str) -> bool:
    order = list(model.model_fields)
    return order.index(earlier) < order.index(later)


def test_match_states_its_note_before_the_classification_and_strength() -> None:
    outer = match_response_schema(("e-1",), ("r-1",))
    (item,) = get_args(outer.model_fields["matches"].annotation)
    order = list(item.model_fields)
    assert order.index("note") < order.index("classification")
    assert order.index("note") < order.index("strength")


def test_critic_states_its_findings_before_the_numeric_scores() -> None:
    for finding in _FINDINGS:
        for score in _SCORES:
            assert _before(CriticVerdict, finding, score)


def test_extraction_quotes_the_source_before_labelling_importance() -> None:
    assert _before(RawRequirement, "quote_or_span", "importance")
    assert _before(RawRequirement, "quote_or_span", "dimension")


def test_relevance_review_emits_the_recommendation_after_its_reasoning() -> None:
    outer = response_schema(("e-1",), conditions=("c-1",))
    (item,) = get_args(outer.model_fields["recommendations"].annotation)
    order = list(item.model_fields)
    assert order[-1] == "recommendation"
    assert order.index("summary") < order.index("recommendation")
    assert order.index("decisive_basis") < order.index("recommendation")
