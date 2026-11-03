"""The gap-search campaign compiler folds markets into branches and stays bounded."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from pydantic import ValidationError

from clearscout.application.discovery.guided_campaign import (
    BranchKind,
    CampaignBounds,
    CampaignBranch,
    CampaignIdentities,
    _floor_query,
    _ordered_protected_branches,
    compile_campaign,
)
from clearscout.application.discovery.guided_campaign_request import (
    attempt_request_plan,
    campaign_response_schema,
)
from clearscout.domain.search_parameters import (
    Acceptance,
    Arrangement,
    Cluster,
    EmployerArchetype,
    Focus,
    JobSearchParameters,
    Lane,
    Location,
    Seniority,
    TargetTitle,
)
from clearscout.infrastructure.providers.plan import SearchPolicy

ATS_SITE_CLAUSE = (
    "(site:boards.greenhouse.io OR site:job-boards.greenhouse.io OR site:jobs.lever.co"
    " OR site:jobs.ashbyhq.com OR site:myworkdayjobs.com)"
)


def _budgeted(floor_query: str) -> str:
    """The part of a floor query the length bound applies to: everything after the site clause."""
    assert floor_query.startswith(f"{ATS_SITE_CLAUSE} ")
    return floor_query.removeprefix(f"{ATS_SITE_CLAUSE} ")


# 28 titles that share no role words, so each one compiles to its own branch.
MANY_SYNTHETIC_TITLES = (
    "Beacon Orchard Steward",
    "Cobalt Meadow Liaison",
    "Harbor Juniper Curator",
    "Marble Summit Navigator",
    "Willow Lantern Advisor",
    "Quartz Valley Facilitator",
    "Maple Horizon Strategist",
    "Silver Prairie Coordinator",
    "Amber Forest Designer",
    "Crimson River Planner",
    "Golden Canyon Specialist",
    "Indigo Garden Partner",
    "Velvet Mountain Consultant",
    "Cedar Aurora Builder",
    "Opal Thunder Advocate",
    "Birch Cosmos Integrator",
    "Coral Tundra Producer",
    "Ember Delta Custodian",
    "Frost Lagoon Archivist",
    "Granite Meridian Overseer",
    "Sapphire Basin Warden",
    "Bronze Thicket Keeper",
    "Larkspur Fjord Envoy",
    "Onyx Savanna Herald",
    "Teal Glacier Scribe",
    "Copper Dune Ranger",
    "Jasper Brook Marshal",
    "Ivory Canopy Sentinel",
)


def _identities(**overrides: Any) -> CampaignIdentities:
    default = CampaignIdentities(
        search_parameter_set_id="params-1",
        search_parameter_sha256="a" * 64,
        master_cv_version_id="cv-1",
        master_cv_sha256="b" * 64,
        accepted_vocabulary_sha256="c" * 64,
        admission_scope_sha256="d" * 64,
        response_schema_version="guided-campaign-response-v1",
        provider="openai",
        model="gpt-test",
        effort="medium",
        verbosity="low",
        service_tier="default",
    )
    return replace(default, **overrides)


def _bounds(**overrides: Any) -> CampaignBounds:
    return replace(CampaignBounds(), **overrides)


def _two_market_parameters() -> JobSearchParameters:
    return JobSearchParameters(
        seniority=Seniority(
            target_titles=[
                TargetTitle(title="Clinical Data Steward", lane=Lane.SPECIALIST),
                TargetTitle(title="Clinical Data Custodian", lane=Lane.SPECIALIST),
                TargetTitle(title="Program Operations Director", lane=Lane.LEADERSHIP),
            ]
        ),
        locations=[
            Location(key="north", country="Example Northland"),
            Location(key="south", country="Example Southland"),
        ],
        focus=Focus(
            capabilities=[Cluster(name="data stewardship")],
            domains=[Cluster(name="regulated records")],
        ),
    )


def test_title_topics_and_focus_bundles_merge_markets_into_one_branch_each() -> None:
    parameters = _two_market_parameters()

    manifest = compile_campaign(
        parameters,
        parameters.seniority.tracked_titles,
        [EmployerArchetype(name="member-owned data networks")],
        identities=_identities(),
        bounds=_bounds(),
    )

    title_branches = [b for b in manifest.branches if b.kind is BranchKind.TITLE_TOPIC]
    focus_branches = [b for b in manifest.branches if b.kind is BranchKind.FOCUS_BUNDLE]
    assert title_branches and focus_branches
    for branch in manifest.branches:
        assert set(branch.markets) == {"Example Northland", "Example Southland"}
    # Two markets did not multiply into four title branches: one specialist
    # topic and one leadership topic, not one per (topic, market) pair.
    assert len(title_branches) == 2
    assert all(branch.floor_query for branch in title_branches)


def _termed_focus_parameters() -> JobSearchParameters:
    return JobSearchParameters(
        seniority=Seniority(
            target_titles=[TargetTitle(title="Clinical Data Steward", lane=Lane.SPECIALIST)]
        ),
        locations=[Location(key="north", country="Example Northland")],
        focus=Focus(
            capabilities=[
                Cluster(
                    name="record custody craft",
                    terms=["data stewardship", "metadata management"],
                )
            ],
            domains=[Cluster(name="regulated records")],
        ),
    )


def test_a_focus_bundle_carries_no_floor_even_when_its_cluster_states_terms() -> None:
    parameters = _termed_focus_parameters()

    manifest = compile_campaign(
        parameters,
        parameters.seniority.tracked_titles,
        [EmployerArchetype(name="member-owned data networks")],
        identities=_identities(),
        bounds=_bounds(),
    )

    by_family = {
        branch.priority_family: branch
        for branch in manifest.branches
        if branch.kind is BranchKind.FOCUS_BUNDLE
    }
    assert set(by_family) == {"capability", "domain", "archetype"}
    for branch in by_family.values():
        assert (branch.floor_query, branch.anchors) == ("", ())
        assert branch.markets == ("Example Northland",)
    [titled] = [b for b in manifest.branches if b.kind is BranchKind.TITLE_TOPIC]
    assert titled.anchors == ("Clinical Data Steward",)
    assert titled.floor_query.startswith(f'{ATS_SITE_CLAUSE} ("Clinical Data Steward")')


def test_a_bundle_with_no_posting_terms_is_still_assigned_an_attempt() -> None:
    parameters = _two_market_parameters()

    manifest = compile_campaign(
        parameters,
        parameters.seniority.tracked_titles,
        [EmployerArchetype(name="member-owned data networks")],
        identities=_identities(),
        bounds=_bounds(),
    )

    bundles = [b for b in manifest.branches if b.kind is BranchKind.FOCUS_BUNDLE]
    assigned = {key for attempt in manifest.attempts for key in attempt.branch_keys}
    assert [b.floor_query for b in bundles] == ["", "", ""]
    assert {b.key for b in bundles} <= assigned
    assert [b.focuses for b in bundles] == [
        (("mandate_capability", "data stewardship"),),
        (("domain_intersection", "regulated records"),),
        (("employer_archetype", "member-owned data networks"),),
    ]


def _many_title_parameters(count: int = len(MANY_SYNTHETIC_TITLES)) -> JobSearchParameters:
    return JobSearchParameters(
        seniority=Seniority(
            target_titles=[TargetTitle(title=title) for title in MANY_SYNTHETIC_TITLES[:count]]
        ),
        locations=[Location(key="north", country="Example Northland")],
    )


def test_every_branch_of_a_plan_larger_than_the_old_bound_is_assigned_an_attempt() -> None:
    parameters = _many_title_parameters()

    manifest = compile_campaign(
        parameters,
        parameters.seniority.tracked_titles,
        [],
        identities=_identities(),
        bounds=_bounds(),
    )

    assert len(manifest.branches) == 28, "the fixture must exceed the old 24-branch ceiling"
    assigned = [key for attempt in manifest.attempts for key in attempt.branch_keys]
    assert sorted(assigned) == sorted(branch.key for branch in manifest.branches)
    assert len(manifest.attempts) == 14


def test_each_attempts_search_cap_is_its_branch_count_plus_headroom() -> None:
    parameters = _many_title_parameters()

    manifest = compile_campaign(
        parameters,
        parameters.seniority.tracked_titles,
        [],
        identities=_identities(),
        bounds=_bounds(),
    )

    assert [len(attempt.branch_keys) for attempt in manifest.attempts] == [2] * 14
    assert all(attempt.search_cap == len(attempt.branch_keys) + 3 for attempt in manifest.attempts)
    assert sum(attempt.search_cap for attempt in manifest.attempts) == 70


def test_attempts_hold_at_most_two_branches_and_cover_every_protected_family_early() -> None:
    parameters = JobSearchParameters(
        seniority=Seniority(
            target_titles=[
                TargetTitle(title="Clinical Data Steward", lane=Lane.SPECIALIST),
                TargetTitle(title="Program Operations Director", lane=Lane.LEADERSHIP),
                TargetTitle(title="Field Operations Lead", lane=None),
            ]
        ),
        locations=[Location(key="north", country="Example Northland")],
        focus=Focus(
            capabilities=[Cluster(name="data stewardship")],
            domains=[Cluster(name="regulated records")],
        ),
    )

    manifest = compile_campaign(
        parameters,
        parameters.seniority.tracked_titles,
        [EmployerArchetype(name="member-owned data networks")],
        identities=_identities(),
        bounds=_bounds(),
    )

    assert all(len(attempt.branch_keys) <= 2 for attempt in manifest.attempts)
    protected_families = {
        branch.priority_family for branch in manifest.branches if branch.protected
    }
    first_two_attempts_branches = {
        key for attempt in manifest.attempts[:2] for key in attempt.branch_keys
    }
    by_key = {branch.key: branch for branch in manifest.branches}
    covered_families = {by_key[key].priority_family for key in first_two_attempts_branches}
    assert protected_families <= covered_families


def test_attempts_are_truncated_to_max_model_calls_and_extra_branches_are_unassigned() -> None:
    parameters = _many_title_parameters()

    manifest = compile_campaign(
        parameters,
        parameters.seniority.tracked_titles,
        [],
        identities=_identities(),
        bounds=_bounds(max_model_calls=2),
    )

    assert len(manifest.attempts) == 2
    assigned = {key for attempt in manifest.attempts for key in attempt.branch_keys}
    assert len(assigned) == 2 * 2
    assert len(manifest.branches) > len(assigned)
    deferred = [branch for branch in manifest.branches if branch.key not in assigned]
    assert deferred


@pytest.mark.parametrize("max_hosted_searches", [10, 12])
def test_attempts_never_reserve_more_searches_than_the_hosted_search_bound(
    max_hosted_searches: int,
) -> None:
    parameters = _many_title_parameters()

    manifest = compile_campaign(
        parameters,
        parameters.seniority.tracked_titles,
        [],
        identities=_identities(),
        bounds=_bounds(max_model_calls=8, max_hosted_searches=max_hosted_searches),
    )

    assert sum(attempt.search_cap for attempt in manifest.attempts) <= max_hosted_searches
    assert all(len(attempt.branch_keys) <= attempt.search_cap for attempt in manifest.attempts)
    assert manifest.attempts[-1].search_cap >= 1


def test_a_family_whose_branches_are_all_focus_bundles_is_never_protected() -> None:
    parameters = _two_market_parameters()

    manifest = compile_campaign(
        parameters,
        parameters.seniority.tracked_titles,
        [EmployerArchetype(name="member-owned data networks")],
        identities=_identities(),
        bounds=_bounds(),
    )

    bundle_families = {
        branch.priority_family
        for branch in manifest.branches
        if branch.kind is BranchKind.FOCUS_BUNDLE
    }
    protected_families = {
        branch.priority_family for branch in manifest.branches if branch.protected
    }
    assert bundle_families == {"capability", "domain", "archetype"}
    assert protected_families == {"specialist", "leadership"}


def test_a_family_mixing_a_bundle_with_a_title_topic_still_protects_its_lead() -> None:
    bundle = CampaignBranch(
        key="bundle",
        kind=BranchKind.FOCUS_BUNDLE,
        priority_family="specialist",
        protected=False,
        markets=("Example Northland",),
        market_arrangements=(),
        lane=None,
        seniority="",
        title_aliases=(),
        focuses=(("mandate_capability", "data stewardship"),),
        floor_query="data stewardship (Example Northland) job",
        anchors=("data stewardship",),
        label="data stewardship",
    )
    topic = replace(
        bundle,
        key="topic",
        kind=BranchKind.TITLE_TOPIC,
        title_aliases=("Clinical Data Steward",),
        focuses=(),
        anchors=("Clinical Data Steward",),
    )

    ordered = _ordered_protected_branches((bundle, topic), frozenset())

    assert [branch.key for branch in ordered if branch.protected] == ["bundle"]


def test_previously_searched_branches_sort_after_unsearched_ones() -> None:
    parameters = _two_market_parameters()
    identities = _identities()
    bounds = _bounds()

    first = compile_campaign(
        parameters,
        parameters.seniority.tracked_titles,
        [EmployerArchetype(name="member-owned data networks")],
        identities=identities,
        bounds=bounds,
    )
    already_searched = frozenset({first.branches[0].key})

    recompiled = compile_campaign(
        parameters,
        parameters.seniority.tracked_titles,
        [EmployerArchetype(name="member-owned data networks")],
        identities=identities,
        bounds=bounds,
        searched_branch_keys=already_searched,
    )

    ordered_keys = [branch.key for branch in recompiled.branches]
    assert ordered_keys.index(first.branches[0].key) > 0
    unsearched_positions = [
        index for index, key in enumerate(ordered_keys) if key not in already_searched
    ]
    searched_positions = [
        index for index, key in enumerate(ordered_keys) if key in already_searched
    ]
    assert max(unsearched_positions) < min(searched_positions)


def test_manifest_sha_is_stable_and_moves_with_bounds_identities_or_a_branch() -> None:
    parameters = _two_market_parameters()
    identities = _identities()
    bounds = _bounds()
    archetypes = [EmployerArchetype(name="member-owned data networks")]

    first = compile_campaign(
        parameters,
        parameters.seniority.tracked_titles,
        archetypes,
        identities=identities,
        bounds=bounds,
    )
    repeated = compile_campaign(
        parameters,
        parameters.seniority.tracked_titles,
        archetypes,
        identities=identities,
        bounds=bounds,
    )
    different_bounds = compile_campaign(
        parameters,
        parameters.seniority.tracked_titles,
        archetypes,
        identities=identities,
        bounds=_bounds(max_model_calls=3),
    )
    different_identities = compile_campaign(
        parameters,
        parameters.seniority.tracked_titles,
        archetypes,
        identities=_identities(model="a-different-model"),
        bounds=bounds,
    )
    fewer_titles = compile_campaign(
        parameters,
        parameters.seniority.tracked_titles[:1],
        archetypes,
        identities=identities,
        bounds=bounds,
    )

    assert first.sha256 == repeated.sha256
    assert first.sha256 != different_bounds.sha256
    assert first.sha256 != different_identities.sha256
    assert first.sha256 != fewer_titles.sha256


def test_every_floor_query_quotes_each_anchor_as_a_phrase_under_the_length_bound() -> None:
    parameters = _termed_focus_parameters()

    manifest = compile_campaign(
        parameters,
        parameters.seniority.tracked_titles,
        [EmployerArchetype(name="member-owned data networks")],
        identities=_identities(),
        bounds=_bounds(),
    )

    floored = [branch for branch in manifest.branches if branch.floor_query]
    assert {branch.kind for branch in floored} == {BranchKind.TITLE_TOPIC}
    for branch in floored:
        assert len(_budgeted(branch.floor_query)) <= 300
        for anchor in branch.anchors:
            assert f'"{anchor}"' in branch.floor_query


def test_a_floor_query_is_trimmed_rather_than_left_over_the_length_bound() -> None:
    parameters = JobSearchParameters(
        seniority=Seniority(
            target_titles=[TargetTitle(title="Extremely Long Synthetic Title Alias Number One")]
        ),
        locations=[
            Location(key=f"place-{index}", country=f"Example Country Number {index} District")
            for index in range(12)
        ],
    )

    manifest = compile_campaign(
        parameters,
        parameters.seniority.tracked_titles,
        [],
        identities=_identities(),
        bounds=_bounds(),
    )

    # No market is ever left off the campaign: one that does not fit its
    # first branch's query starts a further branch instead of being dropped.
    assert len(manifest.branches) > 1
    covered = {market for branch in manifest.branches for market in branch.markets}
    assert len(covered) == 12
    for branch in manifest.branches:
        assert len(_budgeted(branch.floor_query)) <= 300
        assert branch.anchors[0] in branch.floor_query
        for market in branch.markets:
            assert market in branch.floor_query


def test_the_response_schema_is_identical_across_every_attempt_of_one_campaign() -> None:
    parameters = _two_market_parameters()
    manifest = compile_campaign(
        parameters,
        parameters.seniority.tracked_titles,
        [EmployerArchetype(name="member-owned data networks")],
        identities=_identities(),
        bounds=_bounds(),
    )
    branch_keys = tuple(branch.key for branch in manifest.branches)

    schema_one = campaign_response_schema(branch_keys)
    schema_two = campaign_response_schema(branch_keys)

    assert schema_one.model_json_schema() == schema_two.model_json_schema()


def test_the_response_schema_closes_branch_key_over_exactly_the_campaigns_branches() -> None:
    branch_keys = ("a" * 64, "b" * 64)

    schema = campaign_response_schema(branch_keys)

    defs = schema.model_json_schema()["$defs"]
    branch_key_enum = defs["ConstrainedCampaignUrlReport"]["properties"]["branch_keys"]["items"][
        "enum"
    ]
    assert set(branch_key_enum) == set(branch_keys)


def test_the_response_schema_rejects_a_branch_key_outside_the_campaign() -> None:
    schema = campaign_response_schema(("a" * 64, "b" * 64))

    with pytest.raises(ValidationError, match="branch_keys"):
        schema.model_validate(
            {
                "postings": [
                    {"url": "https://example.test/jobs/1", "branch_keys": ["not-a-real-branch"]}
                ],
                "boards": [],
            }
        )


def test_the_response_schema_rejects_a_posting_with_an_employer_field() -> None:
    schema = campaign_response_schema(("a" * 64,))

    with pytest.raises(ValidationError, match="employer"):
        schema.model_validate(
            {
                "postings": [
                    {
                        "url": "https://example.test/jobs/1",
                        "branch_keys": [],
                        "why": "plausible",
                        "employer": "Example Employer",
                    }
                ],
                "boards": [],
            }
        )


def test_the_search_cap_varies_the_task_and_policy_but_never_the_cached_instructions() -> None:
    parameters = _two_market_parameters()
    manifest = compile_campaign(
        parameters,
        parameters.seniority.tracked_titles,
        [EmployerArchetype(name="member-owned data networks")],
        identities=_identities(),
        bounds=_bounds(searches_per_attempt=5),
    )
    attempt = manifest.attempts[0]

    plan = attempt_request_plan(manifest, attempt, evidence_text="a synthetic evidence block")
    smaller = attempt_request_plan(
        manifest,
        replace(attempt, search_cap=attempt.search_cap - 1, branch_keys=attempt.branch_keys[:1]),
        evidence_text="a synthetic evidence block",
    )

    assert f"search budget of {attempt.search_cap}" in " ".join(plan.task.text.split())
    assert plan.instructions.text == smaller.instructions.text
    assert isinstance(plan.search, SearchPolicy)
    assert plan.search.max_calls == attempt.search_cap
    for branch_key in attempt.branch_keys:
        assert branch_key in plan.task.text


def test_a_quote_and_apostrophe_in_the_title_do_not_break_the_floor_query() -> None:
    parameters = JobSearchParameters(
        seniority=Seniority(target_titles=[TargetTitle(title='Owner\'s "Lead" Representative')])
    )

    manifest = compile_campaign(
        parameters,
        parameters.seniority.tracked_titles,
        [],
        identities=_identities(),
        bounds=_bounds(),
    )

    [branch] = manifest.branches
    assert branch.floor_query.count('"') == 2
    assert "\\" not in branch.floor_query
    assert branch.anchors[0] in branch.floor_query


def test_the_request_plan_renders_the_floor_query_as_plain_text_not_python_repr() -> None:
    parameters = JobSearchParameters(
        seniority=Seniority(target_titles=[TargetTitle(title='Owner\'s "Lead" Representative')])
    )
    manifest = compile_campaign(
        parameters,
        parameters.seniority.tracked_titles,
        [],
        identities=_identities(),
        bounds=_bounds(),
    )
    attempt = manifest.attempts[0]

    plan = attempt_request_plan(manifest, attempt, evidence_text="a synthetic evidence block")

    [branch] = manifest.branches
    assert f"floor query: {branch.floor_query}" in plan.task.text
    assert "\\'" not in plan.task.text
    assert '\\"' not in plan.task.text


def test_an_overlong_anchor_is_trimmed_at_a_term_boundary_not_mid_word() -> None:
    """Domain titles cap at 120 characters, so this exercises the trimmer directly."""
    long_anchor = " ".join(f"WordNumber{n}" for n in range(60))

    query = _floor_query((long_anchor,), (), False)

    assert len(_budgeted(query)) <= 300
    assert query.count('"') == 2
    inner = query.split('"')[1]
    assert inner != long_anchor, "the fixture must actually force a trim"
    assert long_anchor.startswith(inner)
    assert long_anchor[len(inner) : len(inner) + 1] == " "


def test_a_title_floor_restricts_to_the_ats_hosts_and_ends_on_its_markets() -> None:
    parameters = JobSearchParameters(
        seniority=Seniority(target_titles=[TargetTitle(title="Clinical Data Steward")]),
        locations=[Location(key="northland", country="Example Northland")],
    )

    manifest = compile_campaign(
        parameters,
        parameters.seniority.tracked_titles,
        [],
        identities=_identities(),
        bounds=_bounds(),
    )

    [branch] = manifest.branches
    assert branch.floor_query == (
        f'{ATS_SITE_CLAUSE} ("Clinical Data Steward") ({" OR ".join(branch.markets)})'
    )


def test_four_long_aliases_keep_every_market_in_their_floor_query() -> None:
    aliases = (
        "Senior Product Manager, Example Medical Content",
        "Senior Product Manager, Example Sciences Data",
        "Senior Technical Product Manager, Example Data Platform",
        "Principal Product Manager, Example Information Products",
    )
    parameters = JobSearchParameters(
        seniority=Seniority(target_titles=[TargetTitle(title=alias) for alias in aliases]),
        locations=[
            Location(key="northland", country="Example Northland"),
            Location(key="southland", country="Example Southland"),
        ],
    )

    manifest = compile_campaign(
        parameters,
        parameters.seniority.tracked_titles,
        [],
        identities=_identities(),
        bounds=_bounds(),
    )

    [branch] = manifest.branches
    assert branch.markets == ("Example Northland", "Example Southland")
    for term in (*aliases, *branch.markets):
        assert term in branch.floor_query


def test_trimming_keeps_the_site_clause_whole_and_cuts_places_before_anchors() -> None:
    anchors = ("Synthetic Records Steward", "Synthetic Lineage Analyst")
    places = tuple(f"Example Country Number {index} District" for index in range(12))

    query = _floor_query(anchors, places, False)

    assert len(_budgeted(query)) <= 300
    assert query.startswith(f'{ATS_SITE_CLAUSE} ("Synthetic Records Steward" OR ')
    assert '"Synthetic Lineage Analyst"' in query
    assert places[0] in query
    assert places[-1] not in query


def test_an_overlong_anchor_is_trimmed_without_cutting_into_the_site_clause() -> None:
    long_anchor = " ".join(f"WordNumber{n}" for n in range(60))

    query = _floor_query((long_anchor,), ("Example Northland",), True)

    assert len(_budgeted(query)) <= 300
    assert query.startswith(f'{ATS_SITE_CLAUSE} ("WordNumber0 ')
    assert query.count('"') == 2


def test_each_markets_own_arrangements_are_rendered_not_unioned_across_the_campaign() -> None:
    parameters = JobSearchParameters(
        seniority=Seniority(target_titles=[TargetTitle(title="Clinical Data Steward")]),
        arrangement=Arrangement(remote=Acceptance.ACCEPTABLE, on_site=Acceptance.ACCEPTABLE),
        locations=[
            Location(key="north", country="Example Northland"),
            Location(
                key="south",
                country="Example Southland",
                arrangement=Arrangement(remote=Acceptance.REFUSED, on_site=Acceptance.ACCEPTABLE),
            ),
        ],
    )

    manifest = compile_campaign(
        parameters,
        parameters.seniority.tracked_titles,
        [],
        identities=_identities(),
        bounds=_bounds(),
    )
    attempt = manifest.attempts[0]

    plan = attempt_request_plan(manifest, attempt, evidence_text="a synthetic evidence block")

    assert "Example Northland (remote, on site)" in plan.task.text
    assert "Example Southland (on site)" in plan.task.text
    assert "Example Southland (remote" not in plan.task.text


def test_an_unstated_market_arrangement_or_level_is_omitted_rather_than_implied() -> None:
    """Silence in the search parameters is unknown, so no branch line may read as permission."""
    parameters = JobSearchParameters(
        seniority=Seniority(target_titles=[TargetTitle(title="Clinical Data Steward")])
    )
    manifest = compile_campaign(
        parameters,
        parameters.seniority.tracked_titles,
        [],
        identities=_identities(),
        bounds=_bounds(),
    )

    task = attempt_request_plan(manifest, manifest.attempts[0], evidence_text="evidence").task.text

    for implied in ("markets:", "seniority:", "any authorized", "any accepted", "any market"):
        assert implied not in task, f"an unstated field rendered as {implied!r}"


def test_the_attempt_framing_and_declared_search_parameters_come_from_the_prompt_file() -> None:
    parameters = _two_market_parameters()
    manifest = compile_campaign(
        parameters,
        parameters.seniority.tracked_titles,
        [EmployerArchetype(name="member-owned data networks")],
        identities=_identities(),
        bounds=_bounds(),
    )
    attempt = manifest.attempts[0]

    plan = attempt_request_plan(manifest, attempt, evidence_text="a synthetic evidence block")

    flattened_task = " ".join(plan.task.text.split())
    assert (
        f"This is attempt {attempt.position} of {len(manifest.attempts)} in this campaign, "
        f"covering exactly {len(attempt.branch_keys)} of this campaign's "
        f"{len(manifest.branches)} branches, for a search budget of {attempt.search_cap} "
        "tool calls:"
    ) in flattened_task
    assert "declared search parameters: markets: Example Northland, Example Southland" in (
        flattened_task
    )


def _budget_sentence(reserved: int, unreserved: int) -> str:
    if reserved == 0:
        return (
            f"no floor query is reserved in this attempt, so all {unreserved} are yours to write."
        )
    return (
        f"{reserved} reserved for the listed floor queries, one per branch that lists one, "
        f"and {unreserved} unreserved."
    )


def test_an_attempt_whose_branches_list_no_floor_query_is_told_every_search_is_its_own() -> None:
    parameters = JobSearchParameters(
        locations=[Location(key="north", country="Example Northland")],
        focus=Focus(capabilities=[Cluster(name="data stewardship")]),
    )
    manifest = compile_campaign(parameters, [], [], identities=_identities(), bounds=_bounds())
    [attempt] = manifest.attempts
    [branch] = manifest.branches
    assert branch.floor_query == ""

    task = " ".join(
        attempt_request_plan(manifest, attempt, evidence_text="evidence").task.text.split()
    )

    assert (
        f"This is attempt 1 of 1 in this campaign, covering exactly 1 of this campaign's 1 "
        f"branches, for a search budget of {attempt.search_cap} tool calls: no floor query is "
        f"reserved in this attempt, so all {attempt.search_cap} are yours to write. The evidence "
        "block above states this person's record;"
    ) in task
    assert "reserved for the listed floor queries" not in task


def test_each_attempts_budget_is_rendered_as_one_reserved_search_per_branch_plus_the_rest() -> None:
    parameters = _many_title_parameters()
    manifest = compile_campaign(
        parameters,
        parameters.seniority.tracked_titles,
        [],
        identities=_identities(),
        bounds=_bounds(max_model_calls=2, max_hosted_searches=7),
    )
    first, second = manifest.attempts

    tasks = [
        " ".join(attempt_request_plan(manifest, item, evidence_text="evidence").task.text.split())
        for item in (first, second)
    ]

    assert (len(first.branch_keys), first.search_cap) == (2, 5)
    assert f"search budget of 5 tool calls: {_budget_sentence(2, 3)}" in tasks[0]
    # The hosted-search bound leaves this attempt no headroom, so the unreserved
    # count is the cap less its branches rather than a fixed three.
    assert (len(second.branch_keys), second.search_cap) == (2, 2)
    assert f"search budget of 2 tool calls: {_budget_sentence(2, 0)}" in tasks[1]


def test_a_final_attempt_with_one_branch_reserves_one_search() -> None:
    parameters = _many_title_parameters(3)
    manifest = compile_campaign(
        parameters,
        parameters.seniority.tracked_titles,
        [],
        identities=_identities(),
        bounds=_bounds(),
    )
    final = manifest.attempts[-1]

    task = " ".join(
        attempt_request_plan(manifest, final, evidence_text="evidence").task.text.split()
    )

    assert (len(final.branch_keys), final.search_cap) == (1, 4)
    assert f"search budget of 4 tool calls: {_budget_sentence(1, 3)}" in task


def test_a_branch_with_no_floor_query_reserves_no_search_and_lists_no_floor() -> None:
    parameters = _two_market_parameters()
    manifest = compile_campaign(
        parameters,
        parameters.seniority.tracked_titles,
        [EmployerArchetype(name="member-owned data networks")],
        identities=_identities(),
        bounds=_bounds(),
    )
    by_key = {branch.key: branch for branch in manifest.branches}
    floorless = {key for key, branch in by_key.items() if not branch.floor_query}

    reserved_counts = []
    floorless_lines = 0
    for attempt in manifest.attempts:
        task = attempt_request_plan(manifest, attempt, evidence_text="evidence").task.text
        reserved = sum(1 for key in attempt.branch_keys if key not in floorless)
        reserved_counts.append(reserved)
        budget = _budget_sentence(reserved, attempt.search_cap - reserved)
        flattened = " ".join(task.split())
        assert f"search budget of {attempt.search_cap} tool calls: {budget}" in flattened
        if reserved == 0:
            assert "reserved floor query has run" not in flattened
        for line in task.splitlines():
            if any(f"- {key}:" in line for key in floorless):
                floorless_lines += 1
                assert "floor query" not in line
    assert (len(floorless), floorless_lines) == (3, 3)
    assert 0 in reserved_counts
