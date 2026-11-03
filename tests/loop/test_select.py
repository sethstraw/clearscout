"""Selection is arithmetic, so it is tested as arithmetic."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import pytest

from clearscout.application.master_cv.cv import CvInventory, Entry, EntryKind
from clearscout.application.tailoring.layout import Budget, wrapped_lines
from clearscout.application.tailoring.stages.jobspec import Importance, JobSpec, Requirement
from clearscout.application.tailoring.stages.match import (
    Classification,
    EvidenceMap,
    Match,
    Strength,
)
from clearscout.application.tailoring.stages.select import (
    MEASURED_OUTCOME_BONUS,
    TopUpStop,
    _outcome_bonus,
    _within,
    build_plan,
)
from clearscout.domain import EvidenceStatus

TODAY = date(2026, 8, 4)


@pytest.fixture
def page_holds(monkeypatch: pytest.MonkeyPatch) -> Callable[[int], None]:
    """Give selection this many master-text bullet lines, whatever the record's shape.

    The synthetic record fits on a page with room to spare, so a test about a
    page that runs out of lines has to say how many it holds.
    """

    def hold(lines: int) -> None:
        monkeypatch.setattr(Budget, "selection_lines", lambda self, fixed: lines)

    return hold


def make_spec(*requirements: tuple[str, str, Importance]) -> JobSpec:
    return JobSpec(
        title="Staff Data Platform Engineer",
        company="Globex",
        seniority="staff",
        location="Remote",
        requirements=tuple(
            Requirement(id=identifier, text=text, importance=importance)
            for identifier, text, importance in requirements
        ),
        keywords=("Kafka", "schema registry"),
        hard_gates=(),
        origin="test",
    )


def bullet_id(inventory: CvInventory, fragment: str) -> str:
    return next(entry.id for entry in inventory.entries if entry.text.startswith(fragment))


def make_bullet(entry_id: str, text: str, order: int = 0) -> Entry:
    return Entry(
        id=entry_id,
        kind=EntryKind.BULLET,
        section="Experience",
        text=text,
        role_key="acme",
        order=order,
    )


def test_within_keeps_the_strongest_bullet_past_the_per_role_allowance() -> None:
    """A role's best bullet taller than its allowance still shows when the page can afford it."""
    strong = make_bullet("b1", "x" * 230)  # three wrapped lines
    kept = _within([strong], allowance=2, scores={"b1": 5.0}, requirements_for={}, cap=5, budget=10)
    assert [entry.id for entry in kept] == ["b1"]


def test_within_still_drops_a_bullet_that_the_global_budget_cannot_afford() -> None:
    strong = make_bullet("b1", "x" * 230)  # three wrapped lines
    kept = _within([strong], allowance=2, scores={"b1": 5.0}, requirements_for={}, cap=5, budget=2)
    assert kept == []


def test_a_bullet_costs_the_lines_its_details_add(settings) -> None:
    from tests.support.resumes import cited, detailed_record, must_haves

    record = detailed_record()
    parent = record.of_kind(EntryKind.BULLET)[0]
    plan = build_plan(
        record, must_haves(1), EvidenceMap(matches=(cited("r01", [parent.id]),)), settings, TODAY
    )

    assert plan.role_bullets == {"example-org": (parent.id,)}
    assert wrapped_lines(parent.text) == 1
    assert plan.spent_lines == 2


def test_a_nested_line_under_a_degree_or_publication_is_reserved_its_own_line(settings) -> None:
    from clearscout.application.tailoring.layout import BLOCK_GAP_LINES
    from tests.support.resumes import must_haves, nested_copies_record

    one_publication = settings.model_copy(update={"min_publications": 1})
    plan = build_plan(nested_copies_record(), must_haves(1), EvidenceMap(), one_publication, TODAY)

    assert plan.fixed_lines["education"] == pytest.approx(BLOCK_GAP_LINES + 2)
    assert plan.fixed_lines["publications"] == pytest.approx(BLOCK_GAP_LINES + 2)


def test_outcome_bonus_applies_only_to_a_must_have_with_a_digit() -> None:
    with_digit = make_bullet("b1", "cut latency by 40 percent")
    without_digit = make_bullet("b2", "cut latency substantially")
    must_have = Requirement(id="r01", text="Latency", importance=Importance.MUST_HAVE)
    nice = Requirement(id="r02", text="Latency", importance=Importance.NICE_TO_HAVE)

    assert _outcome_bonus(must_have, with_digit) == MEASURED_OUTCOME_BONUS
    assert _outcome_bonus(must_have, without_digit) == 1.0
    assert _outcome_bonus(nice, with_digit) == 1.0
    assert _outcome_bonus(nice, without_digit) == 1.0


def test_must_haves_outweigh_nice_to_haves(inventory, settings) -> None:
    kafka = bullet_id(inventory, "Rebuilt the ingestion tier")
    lineage = bullet_id(inventory, "Built the lineage service")
    spec = make_spec(
        ("r01", "Kafka", Importance.MUST_HAVE),
        ("r02", "Lineage", Importance.NICE_TO_HAVE),
    )
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                entry_ids=(kafka,),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
            Match(
                requirement_id="r02",
                entry_ids=(lineage,),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
        )
    )
    plan = build_plan(inventory, spec, evidence, settings, TODAY)
    assert plan.scores[kafka] > plan.scores[lineage]


def test_measurable_must_have_evidence_breaks_a_tie(inventory, settings) -> None:
    measurable = bullet_id(inventory, "Rebuilt the ingestion tier")
    descriptive = bullet_id(inventory, "Delivered the first automated reporting pipeline")
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                entry_ids=(measurable,),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
            Match(
                requirement_id="r02",
                entry_ids=(descriptive,),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
        )
    )
    plan = build_plan(
        inventory,
        make_spec(
            ("r01", "Data platform delivery", Importance.MUST_HAVE),
            ("r02", "Data platform delivery", Importance.MUST_HAVE),
        ),
        evidence,
        settings,
        TODAY,
    )
    assert plan.scores[measurable] > plan.scores[descriptive]


def test_recency_favours_recent_work_without_erasing_older_work(inventory, settings) -> None:
    recent = bullet_id(inventory, "Rebuilt the ingestion tier")
    older = bullet_id(inventory, "Modeled the clinical reporting warehouse")
    spec = make_spec(("r01", "Pipelines", Importance.MUST_HAVE))
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                entry_ids=(recent, older),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
        )
    )
    plan = build_plan(inventory, spec, evidence, settings, TODAY)
    assert plan.scores[recent] > plan.scores[older] > 0


def test_unmatched_bullets_are_not_selected(inventory, settings) -> None:
    kafka = bullet_id(inventory, "Rebuilt the ingestion tier")
    spec = make_spec(("r01", "Kafka", Importance.MUST_HAVE))
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                entry_ids=(kafka,),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
        )
    )
    plan = build_plan(inventory, spec, evidence, settings, TODAY)
    assert plan.role_bullets["northwind-analytics"] == (kafka,)


def test_gap_matches_contribute_nothing(inventory, settings) -> None:
    kafka = bullet_id(inventory, "Rebuilt the ingestion tier")
    spec = make_spec(("r01", "Rust", Importance.MUST_HAVE))
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                entry_ids=(kafka,),
                classification=Classification.GAP,
                strength=Strength.NONE,
            ),
        )
    )
    plan = build_plan(inventory, spec, evidence, settings, TODAY)
    assert plan.scores == {}
    assert plan.role_bullets["northwind-analytics"] == ()


def test_evidence_that_may_not_support_a_claim_never_reaches_a_draft(inventory, settings) -> None:
    """Selection refuses on the evidence contract, not only on a zero strength.

    Matching forces a stated fact to zero strength, so this row cannot be
    produced by the current pipeline. It is what a change that stopped forcing
    it would send here, and a draft citing a bullet for the applicant's own
    residency is the fabrication that would follow.
    """
    kafka = bullet_id(inventory, "Rebuilt the ingestion tier")
    spec = make_spec(("r01", "Kafka", Importance.MUST_HAVE))
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                entry_ids=(kafka,),
                classification=Classification.ASSERTED,
                strength=Strength.STRONG,
                evidence_status=EvidenceStatus.SELF_ATTESTED,
            ),
        )
    )

    plan = build_plan(inventory, spec, evidence, settings, TODAY)

    assert plan.scores == {}


def test_old_roles_collapse_into_the_earlier_career_block(inventory, settings) -> None:
    spec = make_spec(("r01", "Pipelines", Importance.MUST_HAVE))
    plan = build_plan(inventory, spec, EvidenceMap(), settings, date(2036, 1, 1))
    assert "initech-systems" in plan.earlier_roles
    assert plan.role_bullets["initech-systems"] == ()


def test_publications_appear_only_when_the_posting_gives_a_reason(inventory, settings) -> None:
    """A publication list belongs on a CV, not automatically on a derivative.

    Publications appear when the posting gives a reason, not as a fixed floor;
    an uncited list costs the lines that experience wants.
    """
    spec = make_spec(("r01", "Publishing", Importance.NICE_TO_HAVE))
    assert build_plan(inventory, spec, EvidenceMap(), settings, TODAY).publication_ids == ()

    cited = inventory.of_kind(EntryKind.PUBLICATION)[0]
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                entry_ids=(cited.id,),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
        )
    )
    plan = build_plan(inventory, spec, evidence, settings, TODAY)
    assert cited.id in plan.publication_ids


def test_publications_stay_capped_and_in_document_order(inventory, settings) -> None:
    """Someone whose field expects a record can still ask for one."""
    spec = make_spec(("r01", "Publishing", Importance.NICE_TO_HAVE))
    wanted = settings.model_copy(update={"min_publications": 3})
    plan = build_plan(inventory, spec, EvidenceMap(), wanted, TODAY)
    available = inventory.of_kind(EntryKind.PUBLICATION)
    assert len(plan.publication_ids) == min(len(available), 3)
    positions = [inventory.by_id(entry_id).order for entry_id in plan.publication_ids]
    assert positions == sorted(positions)


def test_the_publication_floor_keeps_the_strongest_papers_not_the_earliest(
    inventory, settings
) -> None:
    """Trimming to ``min_publications`` gives up the weakest paper, wherever it sits."""
    earlier, later = inventory.of_kind(EntryKind.PUBLICATION)[:2]
    spec = make_spec(("r01", "Publishing", Importance.NICE_TO_HAVE))
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                entry_ids=(earlier.id,),
                classification=Classification.TRANSFERABLE,
                strength=Strength.WEAK,
            ),
            Match(
                requirement_id="r01",
                entry_ids=(later.id,),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
        )
    )
    one_floor = settings.model_copy(update={"min_publications": 1})
    plan = build_plan(inventory, spec, evidence, one_floor, TODAY)

    assert plan.scores[later.id] > plan.scores[earlier.id]
    assert plan.publication_ids == (earlier.id, later.id)
    assert later.id not in plan.drop_order
    assert earlier.id in plan.drop_order


def test_publications_are_sacrificed_before_experience_bullets(inventory, settings) -> None:
    kafka = bullet_id(inventory, "Rebuilt the ingestion tier")
    registry = bullet_id(inventory, "Designed the schema registry")
    terraform = bullet_id(inventory, "Wrote the Terraform modules")
    spec = make_spec(("r01", "Kafka", Importance.MUST_HAVE))
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                entry_ids=(kafka, registry, terraform),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
        )
    )
    plan = build_plan(inventory, spec, evidence, settings, TODAY)
    expendable_publications = plan.publication_ids[settings.min_publications :]
    assert plan.drop_order[: len(expendable_publications)] == tuple(
        sorted(expendable_publications, key=lambda entry_id: plan.scores.get(entry_id, 0.0))
    )
    assert set(plan.drop_order[len(expendable_publications) :]).issubset(
        set(plan.role_bullets["northwind-analytics"])
    )


def test_a_role_never_loses_its_last_bullets(inventory, settings) -> None:
    spec = make_spec(("r01", "Kafka", Importance.MUST_HAVE))
    ids = [
        bullet_id(inventory, "Rebuilt the ingestion tier"),
        bullet_id(inventory, "Designed the schema registry"),
        bullet_id(inventory, "Wrote the Terraform modules"),
    ]
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                entry_ids=tuple(ids),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
        )
    )
    plan = build_plan(inventory, spec, evidence, settings, TODAY)
    survivors = set(plan.role_bullets["northwind-analytics"]) - set(plan.drop_order)
    assert len(survivors) == 2


def test_a_must_have_is_covered_even_when_a_nice_to_have_would_crowd_it_out(
    inventory, settings
) -> None:
    """Selection guarantees a must-have's answer over a stronger nice-to-have.

    Greedy filling ranks a strong nice-to-have above a weak must-have answer, so
    a two-bullet cap would take the nice-to-have and leave the must-have
    uncovered. The coverage guarantee displaces the nice-to-have instead.
    """
    strong_must = bullet_id(inventory, "Rebuilt the ingestion tier")
    strong_nice = bullet_id(inventory, "Designed the schema registry")
    weak_must = bullet_id(inventory, "Wrote the Terraform modules")
    spec = make_spec(
        ("r01", "Ingestion", Importance.MUST_HAVE),
        ("r02", "Infrastructure as code", Importance.MUST_HAVE),
        ("r03", "Schema registry", Importance.NICE_TO_HAVE),
    )
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                entry_ids=(strong_must,),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
            Match(
                requirement_id="r03",
                entry_ids=(strong_nice,),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
            Match(
                requirement_id="r02",
                entry_ids=(weak_must,),
                classification=Classification.DIRECT,
                strength=Strength.WEAK,
            ),
        )
    )
    capped = settings.model_copy(update={"max_bullets_per_role": 2})
    plan = build_plan(inventory, spec, evidence, capped, TODAY)
    bullets = plan.role_bullets["northwind-analytics"]
    answered = {
        requirement_id
        for entry_id in bullets
        for requirement_id in plan.requirements_for.get(entry_id, ())
    }
    assert "r02" in answered, "the sole must-have answer was left off the page"
    assert weak_must in bullets


def test_the_strongest_answer_to_a_covered_must_have_displaces_a_weaker_in_set_one(
    inventory, settings
) -> None:
    """A covered must-have is upgraded when a stronger answer sits unselected.

    Coverage alone checks presence, not strength, so an older, digitless answer
    would hold the must-have while a stronger recent one stays off the page.
    """
    strong = bullet_id(inventory, "Rebuilt the ingestion tier")  # recent, digit-bearing
    decoy = bullet_id(inventory, "Designed the schema registry")  # nice-to-have, off target
    weak = bullet_id(inventory, "Automated the reconciliation")  # older, digitless
    spec = make_spec(
        ("r01", "Ingestion", Importance.MUST_HAVE),
        ("r02", "Schema registry", Importance.NICE_TO_HAVE),
    )
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                entry_ids=(strong,),
                classification=Classification.DIRECT,
                strength=Strength.WEAK,
            ),
            Match(
                requirement_id="r02",
                entry_ids=(decoy,),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
            Match(
                requirement_id="r01",
                entry_ids=(weak,),
                classification=Classification.DIRECT,
                strength=Strength.WEAK,
            ),
        )
    )
    capped = settings.model_copy(update={"max_bullets_per_role": 1, "min_bullets_per_role": 0})
    plan = build_plan(inventory, spec, evidence, capped, TODAY)
    selected = {entry_id for ids in plan.role_bullets.values() for entry_id in ids}
    assert strong in selected, "the stronger unselected answer was never promoted"
    assert weak in plan.drop_order, "the weaker in-set answer should now be expendable"


def test_the_only_answer_to_a_must_have_is_never_sacrificed_by_trimming(
    inventory, settings
) -> None:
    """A must-have covered by one bullet keeps that bullet out of the drop order.

    Ranking purely by score would trim a low-scoring bullet that is a
    requirement's only answer and leave the must-have uncovered on a shorter page.
    """
    sole = bullet_id(inventory, "Rebuilt the ingestion tier")
    registry = bullet_id(inventory, "Designed the schema registry")
    terraform = bullet_id(inventory, "Wrote the Terraform modules")
    spec = make_spec(
        ("r01", "Ingestion", Importance.MUST_HAVE),
        ("r02", "Platform", Importance.MUST_HAVE),
    )
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                entry_ids=(sole,),
                classification=Classification.DIRECT,
                strength=Strength.WEAK,
            ),
            Match(
                requirement_id="r02",
                entry_ids=(registry, terraform),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
        )
    )
    plan = build_plan(inventory, spec, evidence, settings, TODAY)
    assert sole in plan.role_bullets["northwind-analytics"]
    assert sole not in plan.drop_order, "a must-have's only answer must not be expendable"


def test_all_ids_is_the_drafting_vocabulary(inventory, settings) -> None:
    spec = make_spec(("r01", "Kafka", Importance.MUST_HAVE))
    plan = build_plan(inventory, spec, EvidenceMap(), settings, TODAY)
    assert set(plan.all_ids()).issubset(set(inventory.ids()))
    assert set(plan.publication_ids).issubset(set(plan.all_ids()))


def test_leftover_budget_is_spent_rather_than_left_as_white_space(inventory, settings) -> None:
    """Allocation rounds every share down and never revisits the remainder.

    Without a second pass the leftover lines are claimed by nobody and the page
    ends half empty with cited bullets unused.
    """
    from clearscout.application.master_cv.cv import EntryKind
    from clearscout.application.tailoring.layout import wrapped_lines

    bullets = [entry for entry in inventory.entries if entry.kind is EntryKind.BULLET]
    spec = make_spec(
        *((f"r{i:02d}", f"requirement {i}", Importance.MUST_HAVE) for i in range(1, 6))
    )
    evidence = EvidenceMap(
        matches=tuple(
            Match(
                requirement_id=f"r{index:02d}",
                entry_ids=(entry.id,),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            )
            for index, entry in enumerate(bullets[:5], 1)
        )
    )
    plan = build_plan(inventory, spec, evidence, settings, TODAY)
    kept = [entry_id for ids in plan.role_bullets.values() for entry_id in ids]
    spent = sum(wrapped_lines(inventory.by_id(entry_id).text) for entry_id in kept)

    # Everything cited should reach the page when the budget can afford it.
    cited = {match.entry_ids[0] for match in evidence.matches}
    assert cited <= set(kept), f"cited evidence left unused: {sorted(cited - set(kept))}"
    assert spent > 0


def answering_one_must_have(*entry_ids: str) -> EvidenceMap:
    """Return evidence citing ``entry_ids`` as strong answers to the single must-have r01."""
    return EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                entry_ids=entry_ids,
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
        )
    )


def test_a_one_page_budget_is_filled_from_what_allocation_left_behind(inventory, settings) -> None:
    """The top-up pass, which is where a tight page actually gets its bullets.

    Allocation shares the budget between roles and rounds every share down, so
    on one page it hands each role a floor and stops. What is left is not
    rounding: it is room for more supported evidence, and leaving it empty
    prints white space instead of the record.
    """
    bullets = [entry for entry in inventory.entries if entry.kind is EntryKind.BULLET]
    spec = make_spec(("r01", "everything the posting asks for", Importance.MUST_HAVE))
    evidence = answering_one_must_have(*(entry.id for entry in bullets))
    one_page = settings.model_copy(update={"pages": 1})
    plan = build_plan(inventory, spec, evidence, one_page, TODAY)
    kept = [entry_id for ids in plan.role_bullets.values() for entry_id in ids]
    left = [entry for entry in bullets if entry.id not in kept]

    assert all(wrapped_lines(entry.text) > plan.unspent_lines for entry in left), (
        "a cited bullet that fit the lines left was not placed"
    )
    assert len(kept) > len(plan.recent_roles), "no role got more than its floor"
    assert set(kept) <= {entry.id for entry in bullets}, "an uncited bullet reached the page"


def test_breadth_comes_before_the_per_role_cap(inventory, settings, page_holds) -> None:
    """A role under its cap takes the last line before a stronger bullet over another's cap.

    The Terraform bullet outscores the reconciliation bullet, but its role is at
    the cap while the other role still has cited evidence left.
    """
    capped_role = [
        bullet_id(inventory, fragment)
        for fragment in (
            "Rebuilt the ingestion tier",
            "Designed the schema registry",
            "Wrote the Terraform modules",
        )
    ]
    other_role = [
        bullet_id(inventory, "Modeled the clinical reporting warehouse"),
        bullet_id(inventory, "Automated the reconciliation"),
    ]
    spec = make_spec(("r01", "Data platform delivery", Importance.MUST_HAVE))
    page_holds(6)
    capped = settings.model_copy(update={"max_bullets_per_role": 2})
    plan = build_plan(
        inventory, spec, answering_one_must_have(*capped_role, *other_role), capped, TODAY
    )

    assert plan.role_bullets["contoso-health"] == tuple(other_role)
    assert plan.role_bullets["northwind-analytics"] == tuple(capped_role[:2])
    # The other role took the last line, so Terraform no longer fits what the cap could release.
    assert wrapped_lines(inventory.by_id(capped_role[2]).text) > plan.unspent_lines
    assert plan.withheld_by_cap == ()
    assert plan.top_up_stop is TopUpStop.BUDGET


def test_a_full_page_blames_no_bullet_on_the_cap(tmp_path, settings) -> None:
    """Every bullet cited on one page: the lines ran out, so nothing left off still fits."""
    from tests.support.resumes import bullet_ids, cited, long_record, must_haves

    record = long_record(tmp_path)
    one_page = settings.model_copy(update={"pages": 1, "max_bullets_per_role": 8})
    evidence = EvidenceMap(matches=(cited("r01", bullet_ids(record)),))
    plan = build_plan(record, must_haves(1), evidence, one_page, TODAY)

    assert max(map(len, plan.role_bullets.values())) == one_page.max_bullets_per_role
    assert plan.unspent_lines == 0
    assert plan.withheld_by_cap == ()


def test_the_bullets_held_back_by_the_cap_fit_the_lines_left_together(tmp_path, settings) -> None:
    """Several one-line bullets each fit the one line left, but only one fits it with the rest."""
    from tests.support.resumes import bullet_ids, cited, long_record, must_haves

    record = long_record(tmp_path)
    bullets = bullet_ids(record)
    one_page = settings.model_copy(update={"pages": 1, "max_bullets_per_role": 7})
    evidence = EvidenceMap(matches=(cited("r01", bullets[:30] + bullets[30:60] + bullets[60:68]),))
    plan = build_plan(record, must_haves(1), evidence, one_page, TODAY)
    held = [record.by_id(entry_id) for entry_id in plan.withheld_by_cap]

    assert held
    assert plan.top_up_stop is TopUpStop.BUDGET
    assert sum(wrapped_lines(entry.text) for entry in held) <= plan.unspent_lines
    for entry in held:
        assert len(plan.role_bullets[str(entry.role_key)]) == one_page.max_bullets_per_role


def test_room_left_once_every_role_reaches_the_cap_deepens_the_roles_with_evidence_left(
    inventory, settings
) -> None:
    """Every role fills to the cap first; lines left then go past it, one bullet per role per level.

    Leaving them empty prints white space while cited evidence sits unused.
    """
    capped_role = [
        bullet_id(inventory, fragment)
        for fragment in (
            "Rebuilt the ingestion tier",
            "Designed the schema registry",
            "Wrote the Terraform modules",
        )
    ]
    other_role = [
        bullet_id(inventory, "Modeled the clinical reporting warehouse"),
        bullet_id(inventory, "Automated the reconciliation"),
    ]
    spec = make_spec(("r01", "Data platform delivery", Importance.MUST_HAVE))
    capped = settings.model_copy(update={"max_bullets_per_role": 2})
    plan = build_plan(
        inventory, spec, answering_one_must_have(*capped_role, *other_role), capped, TODAY
    )

    assert plan.role_bullets["northwind-analytics"] == tuple(capped_role)
    assert plan.role_bullets["contoso-health"] == tuple(other_role)
    assert plan.passed_cap == 1
    assert plan.withheld_by_cap == ()
    assert plan.top_up_stop is TopUpStop.EVIDENCE


def test_a_role_that_passed_the_cap_is_not_reported_as_held_back_by_it(
    inventory, settings, page_holds
) -> None:
    """Once a role goes past the cap, what it still leaves out was left out for lines."""
    capped_role = [
        bullet_id(inventory, fragment)
        for fragment in (
            "Rebuilt the ingestion tier",
            "Designed the schema registry",
            "Wrote the Terraform modules",
        )
    ]
    other_role = [
        bullet_id(inventory, "Modeled the clinical reporting warehouse"),
        bullet_id(inventory, "Automated the reconciliation"),
    ]
    cited = [*capped_role, *other_role]
    spec = make_spec(("r01", "Data platform delivery", Importance.MUST_HAVE))
    page_holds(sum(wrapped_lines(inventory.by_id(entry_id).text) for entry_id in cited) - 1)
    capped = settings.model_copy(update={"max_bullets_per_role": 1})
    plan = build_plan(inventory, spec, answering_one_must_have(*cited), capped, TODAY)

    assert len(plan.role_bullets["northwind-analytics"]) == 2
    assert plan.role_bullets["contoso-health"] == tuple(other_role)
    assert plan.passed_cap == 2
    assert plan.top_up_stop is TopUpStop.BUDGET
    assert plan.withheld_by_cap == (), "a role past its cap was said to be held back by it"


def test_the_cap_holds_on_a_page_with_no_lines_left(inventory, settings, page_holds) -> None:
    """A full page neither passes the cap nor blames it for evidence the lines could not hold."""
    bullets = [entry for entry in inventory.entries if entry.kind is EntryKind.BULLET]
    spec = make_spec(("r01", "everything the posting asks for", Importance.MUST_HAVE))
    page_holds(5)
    capped = settings.model_copy(update={"max_bullets_per_role": 1})
    plan = build_plan(
        inventory, spec, answering_one_must_have(*(entry.id for entry in bullets)), capped, TODAY
    )
    spent = sum(
        wrapped_lines(inventory.by_id(entry_id).text)
        for ids in plan.role_bullets.values()
        for entry_id in ids
    )

    assert spent <= 5, "selection spent more lines than the page holds"
    assert all(len(ids) <= 1 for ids in plan.role_bullets.values())
    assert plan.top_up_stop is TopUpStop.BUDGET
    assert plan.passed_cap == 0
    assert plan.withheld_by_cap == (), "the page ran out of lines, not the cap"


def test_bullets_past_the_cap_are_dropped_before_any_within_it(inventory, settings) -> None:
    """A stronger bullet a role holds past the cap goes before a weaker one within another's.

    Dropping the weaker one first would print a role past the cap while another
    role, still within it, lost a bullet the room could have kept.
    """
    capped_role = [
        bullet_id(inventory, fragment)
        for fragment in (
            "Rebuilt the ingestion tier",
            "Designed the schema registry",
            "Wrote the Terraform modules",
        )
    ]
    other_role = [
        bullet_id(inventory, "Modeled the clinical reporting warehouse"),
        bullet_id(inventory, "Automated the reconciliation"),
    ]
    spec = make_spec(
        ("r01", "Data platform delivery", Importance.MUST_HAVE),
        ("r02", "Reporting", Importance.NICE_TO_HAVE),
    )
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                entry_ids=tuple(capped_role),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
            Match(
                requirement_id="r02",
                entry_ids=tuple(other_role),
                classification=Classification.TRANSFERABLE,
                strength=Strength.WEAK,
            ),
        )
    )
    shallow = settings.model_copy(update={"max_bullets_per_role": 2, "min_bullets_per_role": 1})
    plan = build_plan(inventory, spec, evidence, shallow, TODAY)
    over_cap = min(capped_role, key=lambda entry_id: plan.scores[entry_id])

    assert len(plan.role_bullets["northwind-analytics"]) == 3
    assert max(plan.scores[entry_id] for entry_id in other_role) < plan.scores[over_cap]
    assert plan.drop_order[0] == over_cap
    assert set(other_role) & set(plan.drop_order), "the other role had nothing to drop"


def test_a_publication_that_alone_answers_a_must_have_is_never_listed_to_drop(
    inventory, settings
) -> None:
    publication = next(
        entry.id for entry in inventory.entries if entry.kind is EntryKind.PUBLICATION
    )
    spec = make_spec(("r01", "Published research", Importance.MUST_HAVE))
    plan = build_plan(
        inventory,
        spec,
        answering_one_must_have(publication),
        settings.model_copy(update={"min_publications": 0}),
        TODAY,
    )

    assert publication in plan.publication_ids
    assert publication not in plan.drop_order


def test_selection_lines_account_for_what_must_have_placement_spent_past_them(
    inventory, settings, page_holds
) -> None:
    """Placement that overshoots is recorded as overshoot, not hidden as zero lines unspent."""
    bullets = [entry for entry in inventory.entries if entry.kind is EntryKind.BULLET]
    spec = make_spec(("r01", "everything the posting asks for", Importance.MUST_HAVE))
    page_holds(1)
    plan = build_plan(
        inventory, spec, answering_one_must_have(*(entry.id for entry in bullets)), settings, TODAY
    )
    spent = sum(
        wrapped_lines(inventory.by_id(entry_id).text)
        for ids in plan.role_bullets.values()
        for entry_id in ids
    )

    assert plan.overshoot_lines > 0
    assert spent + plan.unspent_lines - plan.overshoot_lines == plan.selection_lines


def test_unique_older_answer_is_available_for_drafting(inventory, settings):
    older = bullet_id(inventory, "Delivered the first automated reporting pipeline")
    spec = make_spec(("r01", "Reporting automation", Importance.MUST_HAVE))
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                entry_ids=(older,),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
        )
    )
    plan = build_plan(
        inventory,
        spec,
        evidence,
        settings.model_copy(update={"full_bullet_years": 5}),
        date(2026, 9, 6),
    )
    assert older in plan.all_ids()
    assert inventory.by_id(older).role_key in plan.recent_roles


def test_replacement_preserves_coverage_in_the_final_set(inventory, settings, page_holds):
    """The stronger answer takes a full role's last slot from a bullet nothing needs."""
    page_holds(3)
    a, b, c = [
        bullet_id(inventory, fragment)
        for fragment in (
            "Rebuilt the ingestion tier",
            "Designed the schema registry",
            "Wrote the Terraform modules",
        )
    ]
    spec = make_spec(
        ("r01", "Streaming ingestion", Importance.MUST_HAVE),
        ("r02", "Infrastructure as code", Importance.MUST_HAVE),
    )
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                entry_ids=(a, b),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
            Match(
                requirement_id="r02",
                entry_ids=(b,),
                classification=Classification.DIRECT,
                strength=Strength.WEAK,
            ),
            Match(
                requirement_id="r02",
                entry_ids=(c,),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
        )
    )
    plan = build_plan(
        inventory, spec, evidence, settings.model_copy(update={"max_bullets_per_role": 2}), TODAY
    )
    role = inventory.by_id(a).role_key

    kept = plan.role_bullets[role]
    assert kept == (b, c), "the stronger r02 answer did not replace the weakest bullet"
    assert all(
        any(requirement.id in plan.requirements_for.get(entry_id, ()) for entry_id in kept)
        for requirement in spec.requirements
    ), "the replacement left a must-have unanswered"


def test_aggregate_breadth_does_not_masquerade_as_requirement_strength(
    inventory, settings, page_holds
):
    page_holds(2)
    strong = bullet_id(inventory, "Rebuilt the ingestion tier")
    broad = bullet_id(inventory, "Designed the schema registry")
    spec = make_spec(
        ("r01", "Streaming", Importance.MUST_HAVE),
        *(
            (f"r{number:02}", f"Adjacent preference {number}", Importance.NICE_TO_HAVE)
            for number in range(2, 9)
        ),
    )
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                entry_ids=(strong,),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
            Match(
                requirement_id="r01",
                entry_ids=(broad,),
                classification=Classification.DIRECT,
                strength=Strength.WEAK,
            ),
            *(
                Match(
                    requirement_id=f"r{number:02}",
                    entry_ids=(broad,),
                    classification=Classification.DIRECT,
                    strength=Strength.STRONG,
                )
                for number in range(2, 9)
            ),
        )
    )
    plan = build_plan(
        inventory, spec, evidence, settings.model_copy(update={"max_bullets_per_role": 1}), TODAY
    )
    assert plan.scores[broad] > plan.scores[strong]
    assert strong in plan.all_ids()
    assert broad not in plan.all_ids()


def test_replacement_cannot_erase_a_different_must_have(inventory, settings, page_holds):
    """A third must-have's only answer stays off the page rather than erasing another's."""
    page_holds(3)
    a, b, c = [
        bullet_id(inventory, fragment)
        for fragment in (
            "Rebuilt the ingestion tier",
            "Designed the schema registry",
            "Wrote the Terraform modules",
        )
    ]
    spec = make_spec(
        ("r01", "Streaming ingestion", Importance.MUST_HAVE),
        ("r02", "Schema governance", Importance.MUST_HAVE),
        ("r03", "Infrastructure as code", Importance.MUST_HAVE),
    )
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                entry_ids=(a,),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
            Match(
                requirement_id="r02",
                entry_ids=(b,),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
            Match(
                requirement_id="r03",
                entry_ids=(c,),
                classification=Classification.DIRECT,
                strength=Strength.WEAK,
            ),
        )
    )
    plan = build_plan(
        inventory, spec, evidence, settings.model_copy(update={"max_bullets_per_role": 2}), TODAY
    )
    role = inventory.by_id(a).role_key

    assert plan.role_bullets[role] == (a, b)
    assert c not in plan.all_ids(), "r03's answer displaced the sole answer to another must-have"
