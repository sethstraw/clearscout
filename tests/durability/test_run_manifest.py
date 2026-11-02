"""What one model call leaves behind, which is what makes a run measurable.

The manifest is the only record of what a paid call was made of. It has to name
the request without keeping any of it, and it has to exist for a call that spent
tokens and returned something unusable.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import pytest
from pydantic import BaseModel

from tailorcv import __version__
from tailorcv.application.config import ImageDetail, Settings, Stage
from tailorcv.infrastructure.providers.base import CallRecord, Finish, UnusableResponseError
from tailorcv.infrastructure.providers.plan import (
    Image,
    RequestPlan,
    SearchPolicy,
    Segment,
    digest_of,
)
from tailorcv.shared.errors import ProviderError
from tests.durability.test_image_budget import png as _png
from tests.support.plans import plan_for


class Answer(BaseModel):
    text: str


SCRIPT: dict[str, Any] = {"Answer": lambda schema: {"text": "hello"}}


def _reading_images_as_sent(settings: Settings) -> Settings:
    """Return settings whose ingest model has a published sizing rule at ``original``."""
    ingest = settings.stages[Stage.INGEST].model_copy(
        update={"model": "gpt-5.6-terra", "image_detail": ImageDetail.ORIGINAL}
    )
    return settings.model_copy(update={"stages": {**settings.stages, Stage.INGEST: ingest}})


def _plan(**overrides: Any) -> RequestPlan:
    """Return a plan whose every segment is distinguishable in a digest."""
    fields: dict[str, Any] = {
        "stage": "match",
        "instructions": Segment("the stage instructions", "match_system"),
        "task": Segment("the dynamic ask", "match_user"),
        "evidence": Segment("the candidate record"),
        "max_tokens": 4096,
    }
    return RequestPlan(**{**fields, **overrides})


def test_a_recorded_call_names_the_request_it_was_made_from(client_factory) -> None:
    """A ledger entry that cannot identify its own request explains no result.

    Every column here answers a question a later comparison asks: which prompts
    ran, under which schema, at which effort, on which revision, with which
    tools. Without them a changed answer has no attributable cause.
    """
    client = client_factory(SCRIPT)

    client.complete(_plan(), Answer)

    entry = client.ledger.entries[0]
    assert entry.stage == "match"
    assert entry.provider == "openai"
    assert entry.effort == "medium"
    assert entry.revision != __version__
    assert "sha256:" in entry.revision
    assert entry.package_version == __version__
    assert entry.prompts == ("match_system", "match_user")
    assert entry.tools == ""
    assert entry.validated is True
    assert entry.instructions_digest == digest_of("the stage instructions")
    assert entry.evidence_digest == digest_of("the candidate record")
    assert entry.task_digest == digest_of("the dynamic ask")
    assert entry.schema_digest, "the schema sent on the wire was not identified"
    assert entry.response_id == "resp_scripted"


def test_the_tool_policy_a_call_ran_under_is_recorded(client_factory) -> None:
    """Hosted search changes what an answer is worth, so it cannot be invisible."""
    client = client_factory(SCRIPT)

    client.complete(_plan(search=SearchPolicy(max_calls=3)), Answer)

    entry = client.ledger.entries[0]
    assert entry.tools == "web_search"
    assert entry.search_policy is not None
    assert entry.search_policy["max_calls"] == 3


def test_the_detail_an_image_was_read_under_is_recorded_with_what_it_can_cost(
    client_factory,
) -> None:
    """A vision call nobody can price is a comparison with a hole in it."""
    client = client_factory(SCRIPT)
    client.settings = _reading_images_as_sent(client.settings)
    page = Image(data=_png(1024, 1024), media_type="image/png")

    client.complete(plan_for(stage=Stage.INGEST, images=(page,)), Answer)

    entry = client.ledger.entries[0]
    assert entry.image_detail == "original"
    assert entry.image_token_ceiling == 1229


def test_a_model_with_no_published_sizing_rule_records_no_count(client_factory) -> None:
    """The shipped ingest model is one OpenAI publishes no sizing rule for."""
    client = client_factory(SCRIPT)
    page = Image(data=_png(1024, 1024), media_type="image/png")

    client.complete(plan_for(stage=Stage.INGEST, images=(page,)), Answer)

    entry = client.ledger.entries[0]
    assert entry.image_detail == "high"
    assert entry.image_token_ceiling is None


def test_a_stage_that_sends_no_record_records_no_evidence_digest(client_factory) -> None:
    """An empty digest is the absence of a component, not a digest of nothing."""
    client = client_factory(SCRIPT)

    client.complete(plan_for(stage=Stage.JOBSPEC), Answer)

    assert client.ledger.entries[0].evidence_digest == ""


def test_each_component_moves_only_its_own_digest(client_factory) -> None:
    """A digest that moved with an unrelated edit cannot localize a change."""
    client = client_factory(SCRIPT)

    client.complete(_plan(), Answer)
    client.complete(_plan(task=Segment("a different ask", "match_user")), Answer)

    first, second = client.ledger.entries
    assert first.task_digest != second.task_digest
    assert first.instructions_digest == second.instructions_digest
    assert first.evidence_digest == second.evidence_digest


def test_where_a_request_is_cached_moves_no_digest_that_names_it(client_factory) -> None:
    """Cache placement changes the bill, not the request, so no recorded run reads as new."""
    client = client_factory(SCRIPT)

    client.complete(_plan(), Answer)
    client.complete(_plan(draws=2), Answer)

    first, second = client.ledger.entries
    assert second.cache_key == first.cache_key
    assert second.instructions_digest == first.instructions_digest
    assert second.evidence_digest == first.evidence_digest
    assert second.task_digest == first.task_digest


def test_the_manifest_keeps_no_text_from_the_request(client_factory) -> None:
    """Run metadata is not a transcript, and a digest is what keeps it from becoming one."""
    client = client_factory(SCRIPT)

    client.complete(_plan(), Answer)

    recorded = repr(asdict(client.ledger.entries[0]))
    for text in ("the stage instructions", "the dynamic ask", "the candidate record"):
        assert text not in recorded, f"{text!r} was stored rather than digested"


def test_a_billed_response_that_cannot_be_used_is_still_recorded(client_factory) -> None:
    """A call that spent tokens and left no row is spend nobody can attribute.

    Truncation, refusal, a content filter, and a payload that fails validation
    all arrive after the provider has billed for the request, so each still
    writes a ledger row.
    """
    client = client_factory(SCRIPT)

    def truncated(plan: RequestPlan, schema: Any, choice: Any) -> Any:
        raise UnusableResponseError(
            "OpenAI stopped at the token output limit",
            CallRecord(
                usage=_usage(), model=choice.model, finish=Finish.TRUNCATED, response_id="resp_cut"
            ),
        )

    client.provider.complete = truncated

    with pytest.raises(ProviderError, match="token output limit"):
        client.complete(_plan(), Answer)

    entry = client.ledger.entries[0]
    assert entry.validated is False
    assert entry.finish == "truncated"
    assert entry.usage.output_tokens == 5
    assert entry.response_id == "resp_cut"
    assert entry.instructions_digest == digest_of("the stage instructions")


def test_a_cache_hit_names_the_request_and_costs_nothing(client_factory, tmp_path) -> None:
    """A reused answer is still a call the run made, and it is still identifiable."""
    from tailorcv.infrastructure.providers.cache import ResponseCache

    client = client_factory(SCRIPT)
    with ResponseCache(enabled=True, directory=tmp_path / "manifest") as cache:
        client.cache = cache
        client.complete(_plan(), Answer)
        client.complete(_plan(), Answer)

    served, reused = client.ledger.entries
    assert reused.cached is True
    assert reused.dollars == 0.0
    assert reused.task_digest == served.task_digest
    assert reused.prompts == served.prompts
    # No provider spoke, so nothing it would have said is claimed.
    assert (reused.response_id, reused.finish, reused.service_tier) == ("", "", "")


def _usage() -> Any:
    """Return the usage a truncated response still bills for."""
    from tailorcv.shared.costs import Usage

    return Usage(input_tokens=11, output_tokens=5, reasoning_tokens=5)
