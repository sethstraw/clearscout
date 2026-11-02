"""Build a campaign attempt's request plan and the one response schema its campaign shares."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, create_model

from tailorcv.application.config import Stage
from tailorcv.application.discovery.guided_campaign import (
    CampaignAttemptPlan,
    CampaignBranch,
    CampaignManifest,
)
from tailorcv.application.prompts import render
from tailorcv.infrastructure.providers.plan import RequestPlan, SearchPolicy, Segment

RESPONSE_SCHEMA_VERSION = "guided-campaign-response-v3"
MAX_WHY_LENGTH = 300
MAX_POSTINGS = 20
MAX_BOARDS = 10
MAX_SEARCHES = 20


def _clipped_why(value: object) -> object:
    """Cut an over-long reason to the bound, which the provider states but does not enforce."""
    return value[:MAX_WHY_LENGTH] if isinstance(value, str) else value


def _clipped_searches(value: object) -> object:
    """Cut an over-long searches list to the bound, so the rest of the answer is kept."""
    return value[:MAX_SEARCHES] if isinstance(value, list) else value


def campaign_response_schema(branch_keys: tuple[str, ...]) -> type[BaseModel]:
    """Build the strict response schema every attempt shares, closed over the branch keys.

    ``searches`` names the branches each query served, but a query credits a
    branch only when the program captured that query itself.
    """
    key_literal: Any = Literal[branch_keys] if branch_keys else str
    url_report_model = create_model(
        "ConstrainedCampaignUrlReport",
        __config__=ConfigDict(extra="forbid"),
        url=(str, ...),
        branch_keys=(list[key_literal], Field(default_factory=list)),
        why=(
            Annotated[str, BeforeValidator(_clipped_why)],
            Field(default="", max_length=MAX_WHY_LENGTH),
        ),
    )
    search_report_model = create_model(
        "ConstrainedCampaignSearchReport",
        __config__=ConfigDict(extra="forbid"),
        query=(str, ...),
        branch_keys=(list[key_literal], Field(default_factory=list)),
    )
    searches: Any = Annotated[
        list[search_report_model],  # type: ignore[valid-type]
        BeforeValidator(_clipped_searches),
    ]
    return create_model(
        "ConstrainedCampaignAnswer",
        __config__=ConfigDict(extra="forbid"),
        postings=(
            list[url_report_model],  # type: ignore[valid-type]
            Field(default_factory=list, max_length=MAX_POSTINGS),
        ),
        boards=(
            list[url_report_model],  # type: ignore[valid-type]
            Field(default_factory=list, max_length=MAX_BOARDS),
        ),
        searches=(searches, Field(default_factory=list, max_length=MAX_SEARCHES)),
    )


def _render_markets(market_arrangements: Sequence[tuple[str, tuple[str, ...]]]) -> str:
    """Render each market with its own accepted arrangements, never a union across markets."""
    return ", ".join(
        f"{market} ({', '.join(arrangements)})" if arrangements else market
        for market, arrangements in market_arrangements
    )


def _describe_branch_for_task(branch: CampaignBranch) -> str:
    """Render one branch's task-relevant facts as one line for the task block.

    A field the search parameters left unstated is omitted rather than rendered as an implied "any".
    """
    identity = ", ".join(branch.title_aliases) or ", ".join(
        f"{mode}: {value}" for mode, value in branch.focuses
    )
    fields = [f"lane: {branch.lane.value}" if branch.lane is not None else ""]
    if branch.market_arrangements:
        fields.append(f"markets: {_render_markets(branch.market_arrangements)}")
    if branch.seniority:
        fields.append(f"seniority: {branch.seniority}")
    if branch.floor_query:
        fields.append(f"floor query: {branch.floor_query}")
    detail = "; ".join(field for field in fields if field)
    return f"- {branch.key}: {branch.label} ({identity}){'; ' + detail if detail else ''}"


def _branches_block(manifest: CampaignManifest, attempt: CampaignAttemptPlan) -> str:
    """Render this attempt's own branches as the task-block text."""
    by_key = {branch.key: branch for branch in manifest.branches}
    lines = [_describe_branch_for_task(by_key[key]) for key in attempt.branch_keys]
    return "\n".join(lines)


def _campaign_search_parameters_text(manifest: CampaignManifest) -> str:
    """Render the declared preferences every branch of this campaign was compiled against.

    Kept per market, and omitted where the search parameters left a field unstated.
    """
    market_arrangements: dict[str, tuple[str, ...]] = {}
    seniority = ""
    for branch in manifest.branches:
        for market, arrangements in branch.market_arrangements:
            market_arrangements.setdefault(market, arrangements)
        if branch.seniority and not seniority:
            seniority = branch.seniority
    fields = []
    if market_arrangements:
        fields.append(f"markets: {_render_markets(tuple(market_arrangements.items()))}")
    if seniority:
        fields.append(f"seniority: {seniority}")
    return "; ".join(fields)


def _budget_split(manifest: CampaignManifest, attempt: CampaignAttemptPlan) -> str:
    """State how one attempt's cap splits into floor-query reservations and the calls left over."""
    floored = {branch.key for branch in manifest.branches if branch.floor_query}
    reserved = sum(1 for key in attempt.branch_keys if key in floored)
    unreserved = max(attempt.search_cap - reserved, 0)
    if reserved == 0:
        return (
            f"no floor query is reserved in this attempt, so all {unreserved} are yours to write."
        )
    return (
        f"{reserved} reserved for the listed floor queries, one per branch that lists one, "
        f"and {unreserved} unreserved."
    )


def attempt_request_plan(
    manifest: CampaignManifest, attempt: CampaignAttemptPlan, evidence_text: str
) -> RequestPlan:
    """Build one attempt's request: its own branches, the campaign's fixed bounds."""
    system_text = render("guided_campaign_system")
    task_text = render(
        "guided_campaign_user",
        search_parameters=_campaign_search_parameters_text(manifest),
        attempt_position=str(attempt.position),
        attempt_count=str(len(manifest.attempts)),
        branch_count=str(len(attempt.branch_keys)),
        search_budget=str(attempt.search_cap),
        budget_split=_budget_split(manifest, attempt),
        campaign_branch_count=str(len(manifest.branches)),
        branches=_branches_block(manifest, attempt),
    )
    return RequestPlan(
        stage=Stage.GUIDED_CAMPAIGN,
        instructions=Segment(system_text, "guided_campaign_system"),
        task=Segment(task_text, "guided_campaign_user"),
        evidence=Segment(evidence_text),
        search=SearchPolicy(max_calls=attempt.search_cap),
        max_tokens=manifest.bounds.max_output_tokens,
    )


__all__ = [
    "attempt_request_plan",
    "campaign_response_schema",
]
