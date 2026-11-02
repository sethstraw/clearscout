"""The scripted evals pin which record entries each drafting stage was offered to cite."""

from __future__ import annotations

from collections.abc import Callable

from evals.case import Case, Responses, ScriptedSpec
from evals.scorers import CaseRun, score

from tailorcv.application.master_cv.cv import CvInventory, EntryKind
from tailorcv.application.tailoring.stages.select import Plan
from tailorcv.shared.costs import Ledger


def _closed(inventory: CvInventory, stage: str, offered: Callable[[Plan], tuple[str, ...]]) -> bool:
    """Score a run whose ``stage`` schema listed ``offered`` of a summary-and-education plan."""
    plan = Plan(
        summary_ids=(inventory.of_kind(EntryKind.SUMMARY)[0].id,),
        education_ids=(inventory.of_kind(EntryKind.EDUCATION)[0].id,),
    )
    run = CaseRun(
        case=Case(
            name="vocabulary-closed",
            proves="each drafting stage is offered only what it may cite",
            posting="synthetic",
            responses=Responses(jobspec=ScriptedSpec(title="Synthetic Role")),
        ),
        inventory=inventory,
        ledger=Ledger(),
        plan=plan,
        vocabularies={stage: offered(plan)},
    )
    return bool(next(item for item in score(run) if item.name == "vocabulary_closed").value)


def test_a_resume_draft_may_be_offered_the_entries_the_program_copies(
    inventory: CvInventory,
) -> None:
    """Education is printed verbatim, and the resume schema still lists it."""
    assert _closed(inventory, "draft", Plan.all_ids)


def test_a_letter_offered_an_entry_the_program_copies_is_not_closed(
    inventory: CvInventory,
) -> None:
    """A letter shows no education or publication as evidence, so it may not cite one."""
    assert _closed(inventory, "cover", Plan.drafted_ids)
    assert not _closed(inventory, "cover", Plan.all_ids)
