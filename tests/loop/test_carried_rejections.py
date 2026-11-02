"""Rejected proposals surface to the next guided search across CV bumps."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from tailorcv.application.discovery.derived_vocabulary_rules import (
    SearchArchetype,
    SearchTitle,
)
from tailorcv.application.discovery.proposals import (
    CarriedRejection,
    CarriedRejections,
    SearchArchetypeProposalService,
    SearchTitleProposalService,
    accepted_archetypes,
    accepted_clusters,
    carried_rejections,
)
from tailorcv.application.discovery.scan_queries import SourceScanQueries
from tailorcv.application.discovery.search_parameter_summary import (
    accepted_proposals_block,
    carried_rejections_block,
)
from tailorcv.application.discovery.search_proposal_inbox import (
    SearchProposalInbox,
    remaining_capacity,
)
from tailorcv.application.workspaces import WorkspaceService
from tailorcv.domain import ProposalRejectionReason, SearchProposalState, new_id, utc_now
from tailorcv.domain.identity import normalize_text
from tailorcv.domain.search_parameters import (
    MAX_CLUSTER_TERMS,
    MAX_TITLES,
    ClusterKind,
    EmployerArchetype,
    TargetTitle,
)
from tailorcv.domain.statements import derived_proposal_identity
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    CandidateProfile,
    DerivedCluster,
    DerivedEmployerArchetype,
    DerivedSearchTitle,
    MasterCvVersion,
)
from tests.support.artifacts import runtime_storage


def _make_cv_version(database: Database, workspace_id: str, marker: str) -> str:
    """Insert one master CV version scoped to the workspace and return its id."""
    with database.session() as session:
        profile = session.scalars(
            select(CandidateProfile).where(CandidateProfile.workspace_id == workspace_id).limit(1)
        ).first()
        if profile is None:
            profile = CandidateProfile(workspace_id=workspace_id, display_name="Candidate")
            session.add(profile)
            session.flush()
        version = MasterCvVersion(
            candidate_profile_id=profile.id,
            content_sha256=(marker * 32)[:64],
            source_uri=f"file:///cv-{marker}.md",
            effective_at=utc_now(),
            byte_size=1,
        )
        session.add(version)
        session.flush()
        return str(version.id)


def _seed_rejection(
    database: Database,
    workspace_id: str,
    master_cv_version_id: str,
    title: str,
    *,
    decided_at: datetime,
    reason: ProposalRejectionReason | None = None,
) -> None:
    """Insert one already-rejected DerivedSearchTitle for the given CV version."""
    normalized = normalize_text(title)
    with database.session() as session:
        row = DerivedSearchTitle(
            id=new_id(),
            workspace_id=workspace_id,
            master_cv_version_id=master_cv_version_id,
            title=title,
            normalized_title=normalized,
            reason="prior run",
            evidence_ids=["evidence-1"],
            lane=None,
            position=0,
            state=SearchProposalState.REJECTED.value,
            origin="test",
            correlation_id=None,
            carried_identity=derived_proposal_identity(normalized),
            decided_at=decided_at,
            rejection_reason=None if reason is None else reason.value,
        )
        session.add(row)


def test_carried_rejections_span_master_cv_versions(durable_database: Database) -> None:
    """A rejection under a previous CV version reaches the next fan-out."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="carry", name="Carry"
    )
    version_a = _make_cv_version(durable_database, str(workspace.id), "a")
    _seed_rejection(
        durable_database,
        str(workspace.id),
        version_a,
        "Prompt Engineer",
        decided_at=utc_now(),
    )
    version_b = _make_cv_version(durable_database, str(workspace.id), "b")
    assert version_a != version_b

    carried = carried_rejections(durable_database, str(workspace.id), cap=40)

    assert "Prompt Engineer" in [entry.name for entry in carried.titles]


def test_carried_rejections_cap_keeps_the_most_recent(durable_database: Database) -> None:
    """A 50-item history reports the 40 newest rejections and drops the older ten."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="cap", name="Cap"
    )
    version_id = _make_cv_version(durable_database, str(workspace.id), "c")
    base_time = datetime(2026, 8, 1, tzinfo=UTC)
    for index in range(50):
        _seed_rejection(
            durable_database,
            str(workspace.id),
            version_id,
            f"Role {index:03d}",
            decided_at=base_time + timedelta(minutes=index),
        )

    carried = carried_rejections(durable_database, str(workspace.id), cap=40)

    assert len(carried.titles) == 40
    assert carried.titles[0].name == "Role 049"
    assert carried.titles[-1].name == "Role 010"


@pytest.mark.parametrize("reason", list(ProposalRejectionReason))
def test_every_reason_a_person_may_give_survives_the_carry(
    durable_database: Database, reason: ProposalRejectionReason
) -> None:
    """A reason the vocabulary offers must reach the next run, not just the ones in use."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug=f"reason-{reason.value.replace('_', '-')}", name="Reason"
    )
    version_id = _make_cv_version(durable_database, str(workspace.id), "r")
    _seed_rejection(
        durable_database,
        str(workspace.id),
        version_id,
        "Data Manager",
        decided_at=utc_now(),
        reason=reason,
    )

    carried = carried_rejections(durable_database, str(workspace.id), cap=40)

    entry = next(item for item in carried.titles if item.name == "Data Manager")
    assert entry.reason is reason


def test_carried_rejections_block_stays_empty_when_nothing_was_ruled() -> None:
    """A workspace with no rejections renders no preamble."""
    empty = CarriedRejections(titles=(), archetypes=(), clusters=())

    assert empty.has_any() is False
    assert carried_rejections_block(empty) == ""


def test_carried_rejections_block_keeps_a_title_holding_a_comma_as_one_entry() -> None:
    carried = CarriedRejections(
        titles=(
            CarriedRejection(name="Records Custodian, Regulated Archives"),
            CarriedRejection(name="Prompt Engineer"),
        ),
        archetypes=(
            CarriedRejection(name="Staffing agencies, contract only"),
            CarriedRejection(name="Series-A startup"),
        ),
        clusters=(
            CarriedRejection(name="prompt engineering"),
            CarriedRejection(name="records retention"),
        ),
    )

    rendered = carried_rejections_block(carried).splitlines()

    assert rendered == [
        "Titles already ruled out (do not propose): "
        "Records Custodian, Regulated Archives; Prompt Engineer",
        "Kinds of organisation already ruled out (do not propose): "
        "Staffing agencies, contract only; Series-A startup",
        "Capabilities and domains already ruled out (do not propose): "
        "prompt engineering; records retention",
    ]


def test_carried_rejections_block_shows_the_stored_reason() -> None:
    """A rejection with a reason renders as ``name (rejected: reason)``."""
    carried = CarriedRejections(
        titles=(
            CarriedRejection(name="Data Manager", reason=ProposalRejectionReason.WRONG_LEVEL),
            CarriedRejection(name="Data Steward"),
        ),
        archetypes=(),
        clusters=(),
    )

    rendered = carried_rejections_block(carried)

    assert "Data Manager (rejected: wrong_level)" in rendered
    # A rejection without a stated reason still renders as just its name.
    assert "Data Steward" in rendered
    assert "Data Steward (" not in rendered


def test_inbox_refuses_a_rejected_identity_under_a_new_cv_version(
    durable_database: Database,
) -> None:
    """A rejected title stays rejected when the same identity is re-proposed on a new CV."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="refuse", name="Refuse"
    )
    version_a = _make_cv_version(durable_database, str(workspace.id), "d")
    _seed_rejection(
        durable_database,
        str(workspace.id),
        version_a,
        "Prompt Engineer",
        decided_at=utc_now(),
    )
    version_b = _make_cv_version(durable_database, str(workspace.id), "e")

    with durable_database.session() as session:
        SearchProposalInbox().remember_titles_in_session(
            session,
            str(workspace.id),
            version_b,
            [
                SearchTitle(
                    title="Prompt Engineer",
                    reason="model tried again",
                    evidence_ids=("evidence-2",),
                    lane=None,
                    used=True,
                )
            ],
            correlation_id=None,
        )

    listed = SearchTitleProposalService(durable_database).inbox(str(workspace.id))

    assert [item.title for item in listed.items] == ["Prompt Engineer"]
    assert listed.items[0].state is SearchProposalState.REJECTED
    assert listed.items[0].master_cv_version_id == version_a
    assert listed.items[0].is_current_master_cv_version is False


def test_inbox_keeps_an_accepted_identity_across_cv_versions(
    durable_database: Database,
) -> None:
    """An acceptance is not re-offered when a new CV version becomes current."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="accept", name="Accept"
    )
    version_a = _make_cv_version(durable_database, str(workspace.id), "f")
    _seed_accepted_title(durable_database, str(workspace.id), version_a, "Platform Engineer")
    version_b = _make_cv_version(durable_database, str(workspace.id), "g")

    with durable_database.session() as session:
        report = SearchProposalInbox().remember_titles_in_session(
            session,
            str(workspace.id),
            version_b,
            [
                SearchTitle(
                    title="Platform Engineer",
                    reason="model tried again",
                    evidence_ids=("evidence-2",),
                    lane=None,
                    used=True,
                )
            ],
            correlation_id=None,
        )

    assert report.dropped == (
        {
            "identity": derived_proposal_identity(normalize_text("Platform Engineer")),
            "reason": "carried_accepted",
        },
    )
    listed = SearchTitleProposalService(durable_database).inbox(str(workspace.id))
    assert [item.title for item in listed.items] == ["Platform Engineer"]
    assert listed.items[0].state is SearchProposalState.ACCEPTED
    assert listed.items[0].master_cv_version_id == version_a
    assert listed.items[0].is_current_master_cv_version is False
    assert listed.capacities[0].active_count == 0
    assert listed.capacities[0].limit == MAX_TITLES
    assert listed.capacities[0].remaining == MAX_TITLES


def test_accepted_prompt_context_spans_master_cv_versions(
    durable_database: Database,
) -> None:
    """A CV refresh does not hide a workspace decision from the next proposal call."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="accepted-context", name="Accepted Context"
    )
    version_a = _make_cv_version(durable_database, str(workspace.id), "u")
    _seed_accepted_title(durable_database, str(workspace.id), version_a, "Platform Engineer")
    _seed_accepted_archetype(
        durable_database, str(workspace.id), version_a, "Regulated research organizations"
    )
    _seed_accepted_cluster(
        durable_database,
        str(workspace.id),
        version_a,
        "streaming ingestion",
        ClusterKind.CAPABILITY,
    )
    version_b = _make_cv_version(durable_database, str(workspace.id), "v")

    with durable_database.session() as session:
        rendered = accepted_proposals_block(session, str(workspace.id), version_b)

    assert "- Titles: Platform Engineer" in rendered
    assert "- Archetypes: Regulated research organizations" in rendered
    assert "- Clusters: streaming ingestion" in rendered


def test_accepted_prompt_context_keeps_a_title_holding_a_comma_as_one_entry(
    durable_database: Database,
) -> None:
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="accepted-comma", name="Accepted Comma"
    )
    version_id = _make_cv_version(durable_database, str(workspace.id), "w")
    for title in ("Records Custodian, Regulated Archives", "Platform Engineer"):
        _seed_accepted_title(durable_database, str(workspace.id), version_id, title)

    with durable_database.session() as session:
        rendered = accepted_proposals_block(session, str(workspace.id), version_id)

    [titles_line] = [line for line in rendered.splitlines() if line.startswith("- Titles: ")]
    assert set(titles_line.removeprefix("- Titles: ").split("; ")) == {
        "Records Custodian, Regulated Archives",
        "Platform Engineer",
    }


def test_an_accepted_archetype_near_duplicate_is_not_offered_after_a_cv_refresh(
    durable_database: Database,
) -> None:
    """A lexical variant of an accepted employer kind does not re-enter the inbox."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="accepted-archetype", name="Accepted Archetype"
    )
    version_a = _make_cv_version(durable_database, str(workspace.id), "w")
    _seed_accepted_archetype(
        durable_database,
        str(workspace.id),
        version_a,
        "Geospatial knowledgebase and data-platform companies",
    )
    version_b = _make_cv_version(durable_database, str(workspace.id), "x")

    with durable_database.session() as session:
        report = SearchProposalInbox().remember_archetypes_in_session(
            session,
            str(workspace.id),
            version_b,
            [
                SearchArchetype(
                    name="Geospatial knowledgebase and reference-data product companies",
                    because="The record models linked geographic entities.",
                    evidence_ids=("evidence-2",),
                    used=True,
                )
            ],
            correlation_id=None,
        )

    assert report.dropped[0]["reason"] == "carried_accepted"
    listed = SearchArchetypeProposalService(durable_database).inbox(str(workspace.id))
    assert [item.name for item in listed.items] == [
        "Geospatial knowledgebase and data-platform companies"
    ]
    assert listed.items[0].is_current_master_cv_version is False


def test_near_duplicate_accepted_archetypes_produce_one_search_context(
    durable_database: Database,
) -> None:
    """Existing duplicate rulings cannot multiply future guided-campaign context."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="deduped-archetypes", name="Deduped Archetypes"
    )
    version_id = _make_cv_version(durable_database, str(workspace.id), "y")
    first_name = "Geospatial knowledgebase and data-platform companies"
    _seed_accepted_archetype(durable_database, str(workspace.id), version_id, first_name)
    _seed_accepted_archetype(
        durable_database,
        str(workspace.id),
        version_id,
        "Geospatial knowledgebase and reference-data product companies",
    )

    assert accepted_archetypes(durable_database, str(workspace.id)) == (
        EmployerArchetype(name=first_name, because="a fit"),
    )


def test_distinct_accepted_archetypes_keep_separate_search_context(
    durable_database: Database,
) -> None:
    """A shared industry word does not collapse distinct employer kinds."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="distinct-archetypes", name="Distinct Archetypes"
    )
    version_id = _make_cv_version(durable_database, str(workspace.id), "z")
    names = (
        "Geospatial knowledgebase and data-platform companies",
        "Geospatial device manufacturers",
    )
    for name in names:
        _seed_accepted_archetype(durable_database, str(workspace.id), version_id, name)

    assert accepted_archetypes(durable_database, str(workspace.id)) == tuple(
        EmployerArchetype(name=name, because="a fit") for name in names
    )


def _seed_accepted_title(
    database: Database, workspace_id: str, master_cv_version_id: str, title: str
) -> None:
    """Insert one accepted DerivedSearchTitle."""
    normalized = normalize_text(title)
    with database.session() as session:
        session.add(
            DerivedSearchTitle(
                id=new_id(),
                workspace_id=workspace_id,
                master_cv_version_id=master_cv_version_id,
                title=title,
                normalized_title=normalized,
                reason="accepted",
                evidence_ids=["evidence-1"],
                lane=None,
                position=0,
                state=SearchProposalState.ACCEPTED.value,
                origin="test",
                correlation_id=None,
                carried_identity=derived_proposal_identity(normalized),
                decided_at=utc_now(),
            )
        )


def _seed_proposed_title(
    database: Database, workspace_id: str, master_cv_version_id: str, title: str
) -> None:
    """Insert one pending title observation for an explicit lifecycle scenario."""
    normalized = normalize_text(title)
    with database.session() as session:
        session.add(
            DerivedSearchTitle(
                id=new_id(),
                workspace_id=workspace_id,
                master_cv_version_id=master_cv_version_id,
                title=title,
                normalized_title=normalized,
                reason="pending",
                evidence_ids=["evidence-2"],
                lane=None,
                position=0,
                state=SearchProposalState.PROPOSED.value,
                origin="search.vocabulary",
                correlation_id="task-2",
                carried_identity=derived_proposal_identity(normalized),
            )
        )


def _seed_accepted_archetype(
    database: Database, workspace_id: str, master_cv_version_id: str, name: str
) -> None:
    """Insert one accepted DerivedEmployerArchetype."""
    normalized = normalize_text(name)
    with database.session() as session:
        session.add(
            DerivedEmployerArchetype(
                id=new_id(),
                workspace_id=workspace_id,
                master_cv_version_id=master_cv_version_id,
                name=name,
                normalized_name=normalized,
                because="a fit",
                evidence_ids=["evidence-1"],
                position=0,
                state=SearchProposalState.ACCEPTED.value,
                origin="test",
                correlation_id=None,
                carried_identity=derived_proposal_identity(normalized),
                decided_at=utc_now(),
            )
        )


def _seed_accepted_cluster(
    database: Database,
    workspace_id: str,
    master_cv_version_id: str,
    name: str,
    kind: ClusterKind,
    terms: list[str] | None = None,
) -> None:
    """Insert one accepted DerivedCluster of the named kind."""
    normalized = normalize_text(name)
    with database.session() as session:
        session.add(
            DerivedCluster(
                id=new_id(),
                workspace_id=workspace_id,
                master_cv_version_id=master_cv_version_id,
                kind=kind.value,
                name=name,
                normalized_name=normalized,
                terms=[name] if terms is None else terms,
                because="a fit",
                evidence_ids=["evidence-1"],
                position=0,
                state=SearchProposalState.ACCEPTED.value,
                origin="test",
                correlation_id=None,
                carried_identity=derived_proposal_identity(normalized),
                decided_at=utc_now(),
            )
        )


def test_an_accepted_cluster_saved_under_a_wider_term_bound_still_loads(
    durable_database: Database,
) -> None:
    """A stored cluster with more terms than the bound loads, trimmed to the bound."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="wide-terms", name="Wide Terms"
    )
    version = _make_cv_version(durable_database, str(workspace.id), "w")
    terms = [f"term {index}" for index in range(MAX_CLUSTER_TERMS + 2)]
    _seed_accepted_cluster(
        durable_database, str(workspace.id), version, "wide cluster", ClusterKind.DOMAIN, terms
    )

    with durable_database.session() as session:
        [(kind, cluster)] = accepted_clusters(session, str(workspace.id))

    assert kind is ClusterKind.DOMAIN
    assert cluster.terms == terms[:MAX_CLUSTER_TERMS]


def test_accepted_proposals_block_is_empty_when_nothing_has_been_accepted(
    durable_database: Database,
) -> None:
    """A fresh workspace renders no accepted-proposals evidence."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="empty", name="Empty"
    )
    version_id = _make_cv_version(durable_database, str(workspace.id), "f")

    with durable_database.session() as session:
        assert accepted_proposals_block(session, str(workspace.id), version_id) == ""


def test_accepted_proposals_block_names_every_axis_the_workspace_accepted(
    durable_database: Database,
) -> None:
    """The block reads back the accepted titles, archetypes, and clusters."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="named", name="Named"
    )
    version_id = _make_cv_version(durable_database, str(workspace.id), "g")
    _seed_accepted_title(durable_database, str(workspace.id), version_id, "Platform Lead")
    _seed_accepted_archetype(durable_database, str(workspace.id), version_id, "Series-A startup")
    _seed_accepted_cluster(
        durable_database,
        str(workspace.id),
        version_id,
        "streaming ingestion",
        ClusterKind.CAPABILITY,
    )

    with durable_database.session() as session:
        rendered = accepted_proposals_block(session, str(workspace.id), version_id)

    assert "Already accepted (do not re-propose):" in rendered
    assert "- Titles: Platform Lead" in rendered
    assert "- Archetypes: Series-A startup" in rendered
    assert "- Clusters: streaming ingestion" in rendered


def test_reject_persists_the_stated_reason_and_accept_clears_it(
    durable_database: Database,
) -> None:
    """A reason survives on the row until an accept overrides it."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="reason-round", name="Round"
    )
    version_id = _make_cv_version(durable_database, str(workspace.id), "s")
    _seed_rejection(
        durable_database,
        str(workspace.id),
        version_id,
        "Program Manager",
        decided_at=utc_now(),
    )
    service = SearchTitleProposalService(durable_database)
    proposals = service.inbox(str(workspace.id))
    proposal = next(item for item in proposals.items if item.title == "Program Manager")

    ruled = service.reject(str(workspace.id), proposal.id, ProposalRejectionReason.WRONG_MANDATE)
    assert ruled.state is SearchProposalState.REJECTED
    assert ruled.rejection_reason is ProposalRejectionReason.WRONG_MANDATE

    restored = service.accept(str(workspace.id), proposal.id)
    assert restored.state is SearchProposalState.ACCEPTED
    assert restored.rejection_reason is None


def test_remaining_capacity_counts_only_unreviewed_rows(
    durable_database: Database,
) -> None:
    """Accepted and rejected rows do not consume an unreviewed proposal slot."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="cap-count", name="CapCount"
    )
    version_id = _make_cv_version(durable_database, str(workspace.id), "h")
    _seed_rejection(
        durable_database,
        str(workspace.id),
        version_id,
        "Prompt Engineer",
        decided_at=utc_now(),
    )
    _seed_accepted_title(durable_database, str(workspace.id), version_id, "Platform Lead")

    with durable_database.session() as session:
        remaining = remaining_capacity(
            session, DerivedSearchTitle, str(workspace.id), version_id, MAX_TITLES
        )

    assert remaining == MAX_TITLES


def test_a_superseded_pending_suggestion_is_not_an_actionable_workspace_decision(
    durable_database: Database,
) -> None:
    """Pending evidence expires with its CV version while explicit rulings survive."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="stale-pending", name="Stale pending"
    )
    version_a = _make_cv_version(durable_database, str(workspace.id), "k")
    _seed_proposed_title(durable_database, str(workspace.id), version_a, "Platform Lead")
    _make_cv_version(durable_database, str(workspace.id), "l")

    listed = SearchTitleProposalService(durable_database).inbox(str(workspace.id))

    assert listed.items == []
    assert listed.capacities[0].active_count == 0
    assert listed.capacities[0].remaining == MAX_TITLES


def test_a_newer_pending_duplicate_cannot_override_an_older_explicit_acceptance(
    durable_database: Database,
) -> None:
    """A repeated model observation has less authority than the user's durable ruling."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="pending-vs-decision", name="Pending versus decision"
    )
    version_a = _make_cv_version(durable_database, str(workspace.id), "m")
    _seed_accepted_title(durable_database, str(workspace.id), version_a, "Platform Lead")
    version_b = _make_cv_version(durable_database, str(workspace.id), "n")
    _seed_proposed_title(durable_database, str(workspace.id), version_b, "Platform Lead")

    listed = SearchTitleProposalService(durable_database).inbox(str(workspace.id))
    consumed = SourceScanQueries(durable_database).derived_titles(str(workspace.id))

    assert len(listed.items) == 1
    assert listed.items[0].state is SearchProposalState.ACCEPTED
    assert listed.items[0].master_cv_version_id == version_a
    assert listed.items[0].is_current_master_cv_version is False
    assert consumed == [TargetTitle(title="Platform Lead")]


def test_a_newer_rejection_supersedes_an_older_acceptance_for_every_title_consumer(
    durable_database: Database,
) -> None:
    """One latest workspace ruling drives both the inbox and search title consumption."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="latest-title-ruling", name="Latest title ruling"
    )
    version_a = _make_cv_version(durable_database, str(workspace.id), "i")
    _seed_accepted_title(durable_database, str(workspace.id), version_a, "Platform Lead")
    version_b = _make_cv_version(durable_database, str(workspace.id), "j")
    _seed_rejection(
        durable_database,
        str(workspace.id),
        version_b,
        "Platform Lead",
        decided_at=utc_now() + timedelta(seconds=1),
        reason=ProposalRejectionReason.NOT_RELEVANT,
    )

    consumed = SourceScanQueries(durable_database).derived_titles(str(workspace.id))
    listed = SearchTitleProposalService(durable_database).inbox(str(workspace.id))

    assert consumed == []
    assert len(listed.items) == 1
    assert listed.items[0].state is SearchProposalState.REJECTED
    assert listed.items[0].master_cv_version_id == version_b
    assert listed.items[0].rejection_reason is ProposalRejectionReason.NOT_RELEVANT


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))


def test_accepted_vocabulary_above_the_inbox_limit_keeps_room_for_new_titles(
    durable_database: Database,
) -> None:
    """Accepted knowledge persists while only unreviewed proposals consume slots."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="accepted-capacity", name="Accepted capacity"
    )
    workspace_id = str(workspace.id)
    version_id = _make_cv_version(durable_database, workspace_id, "z")
    for index in range(MAX_TITLES + 2):
        _seed_accepted_title(durable_database, workspace_id, version_id, f"Accepted role {index}")
    with durable_database.session() as session:
        report = SearchProposalInbox().remember_titles_in_session(
            session,
            workspace_id,
            version_id,
            [
                SearchTitle(
                    title=f"New role {index}",
                    reason="Synthetic evidence",
                    evidence_ids=("evidence-1",),
                    lane=None,
                    used=True,
                )
                for index in range(MAX_TITLES + 1)
            ],
            correlation_id=None,
        )
    assert report.retained == MAX_TITLES
    assert report.dropped == ()
    with durable_database.session() as session:
        overflow = SearchProposalInbox().remember_titles_in_session(
            session,
            workspace_id,
            version_id,
            [
                SearchTitle(
                    title="Another new role",
                    reason="Synthetic evidence",
                    evidence_ids=("evidence-1",),
                    lane=None,
                    used=True,
                )
            ],
            correlation_id=None,
        )
    assert [item["reason"] for item in overflow.dropped] == ["proposal_capacity"]
    service = SearchTitleProposalService(durable_database)
    listed = service.inbox(workspace_id)
    assert (
        len([item for item in listed.items if item.state is SearchProposalState.ACCEPTED])
        == MAX_TITLES + 2
    )
    assert listed.capacities[0].active_count == MAX_TITLES
    assert listed.capacities[0].remaining == 0
    pending = next(item for item in listed.items if item.state is SearchProposalState.PROPOSED)
    service.accept(workspace_id, pending.id)
    assert service.inbox(workspace_id).capacities[0].remaining == 1
