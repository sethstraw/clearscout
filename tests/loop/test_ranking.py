"""Queue ranking, and the reasons it reports."""

from __future__ import annotations

from clearscout.application.discovery.ranking import DiscoverySignal, rank
from clearscout.domain.search_parameters import (
    MAX_CLUSTERS,
    Cluster,
    CountryNames,
    Focus,
    JobSearchParameters,
    Location,
    Seniority,
    WorkEligibility,
)

SEARCHED = ["Data Platform Lead"]


def _parameters(*locations: Location) -> JobSearchParameters:
    return JobSearchParameters(locations=list(locations))


def test_a_title_that_answers_the_search_outranks_one_that_does_not() -> None:
    parameters = _parameters()

    answering = rank("Senior Data Platform Lead", "", parameters, SEARCHED)
    unrelated = rank("Warehouse Supervisor", "", parameters, SEARCHED)

    assert answering.score > unrelated.score
    assert answering.because == ("the title answers Data Platform Lead",)
    assert unrelated.because == ()


def test_somewhere_this_person_may_already_work_outranks_somewhere_they_may_not() -> None:
    parameters = _parameters(
        Location(key="ca", country="Canada"),
        Location(
            key="us",
            country="United States",
            eligibility=WorkEligibility.SPONSORSHIP_REQUIRED,
        ),
    )

    here = rank("Data Platform Lead", "Toronto, Canada", parameters, SEARCHED)
    there = rank("Data Platform Lead", "Austin, United States", parameters, SEARCHED)

    assert here.score > there.score
    assert "you may work in Canada already" in here.because
    assert "United States needs sponsorship" in there.because


def test_a_place_nobody_declared_adds_nothing_rather_than_subtracting() -> None:
    parameters = _parameters(Location(key="ca", country="Canada"))

    unplaced = rank("Data Platform Lead", "Somewhere Else", parameters, SEARCHED)
    stated = rank("Data Platform Lead", "", parameters, SEARCHED)

    assert unplaced.score == stated.score
    assert all("work in" not in reason for reason in unplaced.because)


def test_a_country_this_person_never_named_ranks_below_a_place_nobody_read() -> None:
    """A location nothing could read is unknown; another country is known to be out.

    One naming another country is somewhere the work is not available, and the
    queue should say so.
    """
    parameters = _parameters(Location(key="ca", country="Canada"))

    elsewhere = rank("Data Platform Lead", "Remote, Hungary", parameters, SEARCHED)
    unread = rank("Data Platform Lead", "Somewhere Else", parameters, SEARCHED)
    home = rank("Data Platform Lead", "Toronto, Canada", parameters, SEARCHED)

    assert elsewhere.score < unread.score < home.score
    assert any("Hungary" in reason for reason in elsewhere.because)


def test_a_persons_country_alias_extends_the_maintained_geography_vocabulary() -> None:
    """A private publisher spelling extends rather than replaces known countries."""
    shipped = _parameters(Location(key="ca", country="Canada"))
    stated = JobSearchParameters(
        locations=[Location(key="ca", country="Canada")],
        countries=[
            CountryNames(country="Canada"),
            CountryNames(country="Kenya", spellings=["republic of kenya"]),
        ],
    )

    unread = rank("Data Platform Lead", "Nairobi, Kenya", shipped, SEARCHED)
    elsewhere = rank("Data Platform Lead", "Nairobi, Republic of Kenya", stated, SEARCHED)

    assert "Kenya is not one of your places" in unread.because
    assert elsewhere.score == unread.score
    assert "Kenya is not one of your places" in elsewhere.because


def test_a_title_answering_the_craft_outranks_one_the_search_never_named() -> None:
    """A capability ranks work no stated title happens to name.

    Both titles here are unsearched, so a title filter alone rates them equally
    and the queue would order them arbitrarily.
    """
    parameters = JobSearchParameters(
        focus=Focus(
            capabilities=[Cluster(name="data platform engineering", terms=["pipeline", "etl"])]
        )
    )

    craft = rank("Senior Pipeline Developer", "", parameters, [])
    elsewhere = rank("Warehouse Supervisor", "", parameters, [])

    assert craft.score > elsewhere.score
    assert craft.because == ("the work is data platform engineering",)
    assert elsewhere.because == ()


def test_ranking_reads_only_the_first_capped_clusters_of_a_kind() -> None:
    """The form tells the person the first MAX_CLUSTERS are used, so the ranker has to agree."""
    words = ("pipeline", "warehouse", "streaming", "lineage", "catalog", "scheduler")
    assert len(words) == MAX_CLUSTERS + 1
    parameters = JobSearchParameters(
        focus=Focus(
            capabilities=[
                Cluster(name=f"craft {index}", terms=[word]) for index, word in enumerate(words)
            ]
        )
    )

    last_used = rank(f"Senior {words[MAX_CLUSTERS - 1].title()} Developer", "", parameters, [])
    first_unused = rank(f"Senior {words[MAX_CLUSTERS].title()} Developer", "", parameters, [])

    assert last_used.because == (f"the work is craft {MAX_CLUSTERS - 1}",)
    assert first_unused.because == ()


def test_a_cluster_nobody_gave_terms_to_still_matches_its_own_name() -> None:
    """Stating a capability is never worse than stating nothing."""
    parameters = JobSearchParameters(focus=Focus.model_validate({"capabilities": ["terminology"]}))

    named = rank("Clinical Terminology Analyst", "", parameters, [])

    assert named.because == ("the work is terminology",)


def test_a_subject_counts_for_less_than_the_craft_that_does_it() -> None:
    """Both are real signals, and the work is the one being hired for."""
    parameters = JobSearchParameters(
        focus=Focus(
            capabilities=[Cluster(name="platform engineering", terms=["platform"])],
            domains=[Cluster(name="clinical care", terms=["clinical"])],
        )
    )

    craft = rank("Platform Engineer", "", parameters, [])
    subject = rank("Clinical Analyst", "", parameters, [])

    assert craft.score > subject.score > 0


def test_work_this_person_ruled_out_ranks_below_work_they_said_nothing_about() -> None:
    """A boundary ranks a posting down; hiding it is what an exclusion does."""
    parameters = JobSearchParameters(focus=Focus(boundaries=["engineering management"]))

    refused = rank("Engineering Management Lead", "", parameters, [])
    neutral = rank("Warehouse Supervisor", "", parameters, [])

    assert refused.score < neutral.score
    assert refused.because == ("you ruled out engineering management",)


def test_a_boundary_does_not_rank_down_a_title_that_merely_shares_one_word() -> None:
    """``management`` normalizes to ``manager``, which every product title carries.

    Ranking `Director, Product Management` down for a line-management boundary
    would be a people-management false negative.
    """
    parameters = JobSearchParameters(
        focus=Focus(boundaries=["line management as the defining mandate"])
    )

    standing = rank("Director, Product Management", "", parameters, [])

    assert "you ruled out line management as the defining mandate" not in standing.because
    assert standing.score >= 0


def test_a_search_with_no_titles_ranks_on_what_is_left() -> None:
    """Nothing to compare a title against is not a reason to call every title bad."""
    parameters = _parameters(Location(key="ca", country="Canada"))

    standing = rank("Data Platform Lead", "Toronto, Canada", parameters, [])

    assert standing.score > 0
    assert standing.because == ("you may work in Canada already",)


def test_a_ranking_reason_names_a_place_the_way_its_owner_wrote_it() -> None:
    parameters = _parameters(
        Location(key="manitoba_remote", country="Canada", region="Manitoba", metro="Northtown")
    )

    standing = rank("Data Platform Lead", "Northtown, Canada", parameters, [])

    assert standing.because == ("you may work in Northtown already",)


def test_a_stated_seniority_band_is_a_ranking_signal() -> None:
    parameters = JobSearchParameters(seniority=Seniority(minimum="senior", maximum="staff"))

    inside = rank("Staff Data Platform Lead", "", parameters, [])
    outside = rank("Director Data Platform Lead", "", parameters, [])

    assert inside.score > outside.score
    assert inside.because == ("the level is staff",)
    assert outside.because == ("the level director is outside your stated band",)


def test_a_job_noun_that_spells_a_rung_carries_no_seniority_signal() -> None:
    parameters = JobSearchParameters(seniority=Seniority(minimum="senior", maximum="director"))

    sales = rank("Regional Named Account Executive", "", parameters, [])
    leader = rank("Executive Vice President, Data", "", parameters, [])

    assert sales.because == ()
    assert leader.because == ("the level executive is outside your stated band",)


def test_associate_ahead_of_director_ranks_as_director() -> None:
    parameters = JobSearchParameters(seniority=Seniority(minimum="senior", maximum="director"))

    leader = rank("Associate Director, Data Governance", "", parameters, [])
    unranked = rank("Associate Vice President, Data", "", parameters, [])
    junior = rank("Warehouse Associate", "", parameters, [])

    assert leader.because == ("the level is director",)
    assert unranked.because == ()
    assert junior.because == ("the level associate is outside your stated band",)


def test_a_title_the_search_named_is_attributed_to_the_title_signal() -> None:
    """Title matching is the baseline the other signals are measured against."""
    parameters = JobSearchParameters(
        focus=Focus(capabilities=[Cluster(name="data platform engineering", terms=["platform"])])
    )

    named = rank("Data Platform Lead", "", parameters, SEARCHED)

    assert named.signal is DiscoverySignal.TITLE


def test_a_craft_match_no_title_named_is_attributed_to_responsibility() -> None:
    parameters = JobSearchParameters(
        focus=Focus(capabilities=[Cluster(name="data platform engineering", terms=["pipeline"])])
    )

    craft = rank("Senior Pipeline Developer", "", parameters, SEARCHED)

    assert craft.signal is DiscoverySignal.RESPONSIBILITY


def test_a_craft_and_a_subject_together_are_attributed_to_the_domain_intersection() -> None:
    parameters = JobSearchParameters(
        focus=Focus(
            capabilities=[Cluster(name="platform engineering", terms=["pipeline"])],
            domains=[Cluster(name="clinical care", terms=["clinical"])],
        )
    )

    both = rank("Clinical Pipeline Developer", "", parameters, SEARCHED)

    assert both.signal is DiscoverySignal.DOMAIN_INTERSECTION


def test_a_subject_alone_is_left_unattributed_rather_than_called_an_intersection() -> None:
    """An unattributed row is a real answer; a defaulted bucket is a fabrication."""
    parameters = JobSearchParameters(
        focus=Focus(
            capabilities=[Cluster(name="platform engineering", terms=["pipeline"])],
            domains=[Cluster(name="clinical care", terms=["clinical"])],
        )
    )

    subject = rank("Clinical Analyst", "", parameters, SEARCHED)
    unmatched = rank("Warehouse Supervisor", "", parameters, SEARCHED)

    assert subject.score > 0
    assert subject.signal is None
    assert unmatched.signal is None


def test_a_bare_city_takes_no_elsewhere_penalty_when_it_suggests_a_saved_country() -> None:
    parameters = _parameters(Location(key="us", country="United States"))
    assert not rank(
        "Platform Product Manager", "Boston, Lincolnshire, England", parameters, []
    ).because
