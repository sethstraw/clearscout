"""A requirement carries whether it is a hard skill or a behavioral disposition."""

from __future__ import annotations

from tailorcv.application.prompts import packaged
from tailorcv.application.tailoring.stages.context import requirement_text
from tailorcv.application.tailoring.stages.jobspec import (
    Dimension,
    HardGate,
    Importance,
    JobSpec,
    Requirement,
)


def _spec() -> JobSpec:
    return JobSpec(
        title="Platform PM",
        company=None,
        seniority=None,
        location=None,
        requirements=(
            Requirement(
                id="r01", text="Python", importance=Importance.MUST_HAVE, dimension=Dimension.HARD
            ),
            Requirement(
                id="r02",
                text="Bias for shipping",
                importance=Importance.MUST_HAVE,
                dimension=Dimension.BEHAVIORAL,
            ),
        ),
        keywords=("python",),
        hard_gates=(HardGate(kind="location", text="Canada"),),
        origin="posting.txt",
    )


def test_a_requirement_defaults_to_hard() -> None:
    """Unlabeled requirements are the concrete kind; behavioral is opt-in."""
    requirement = Requirement(id="r01", text="Python", importance=Importance.MUST_HAVE)
    assert requirement.dimension is Dimension.HARD


def test_the_prompt_sends_dimension_alongside_importance() -> None:
    """The match model cannot apply the behavioral rule without seeing the label."""
    rendered = requirement_text(_spec())
    assert "(must_have, hard) Python" in rendered
    assert "(must_have, behavioral) Bias for shipping" in rendered


def test_the_jobspec_prompt_teaches_the_model_to_classify_dimension() -> None:
    """A prompt without the rule cannot ask the model to produce the field."""
    source = packaged("jobspec_system")
    assert "`dimension`" in source
    assert "behavioral" in source


def test_the_match_prompt_carries_the_behavioral_evidence_rule() -> None:
    """The match stage needs an explicit rule for how to answer behavioral requirements."""
    source = packaged("match_system")
    assert "soft_signal_evidence" in source
    assert 'dimension: "behavioral"' in source
    # The rule must still refuse title-halo for hard skills.
    assert "not inferred from a\ntitle" in source or "not inferred from a title" in source
    assert "Classify as `DIRECT` when the record shows that action pattern explicitly" in source


def test_the_match_prompt_distinguishes_adjacent_and_participation_only_evidence() -> None:
    """Adjacent capability and participation-only wording each have a stated boundary."""
    source = packaged("match_system")

    assert "Rules 2 and 3 constrain `DIRECT` and `TAILORING_ONLY`" in source
    assert "Use `GAP` only when no\n   concrete related capability or action is present" in source
    assert "a\n   programming language counts as one, is related only within the record's" in source
    assert "superset of the record's language or runs on its runtime" in source
    assert "proficiency qualifier, such as 4+ years of Elixir, is `GAP`" in source
    assert "another language does not transfer" not in source
    prose = " ".join(source.split())
    assert "Wording that limits the applicant's part in the requested work" in prose
    assert '"worked on", is never `DIRECT`' in prose
    assert "delivering the requested action or outcome" not in source
    assert "The limited contribution is what a truthful rewrite would keep" in prose
    assert "not a missing qualifier" in prose
    assert "names the requested capability as work the applicant took part in" in prose
    assert "meets every qualifier the requirement states" in prose
    assert "`TAILORING_ONLY` at `moderate` strength" in prose
    assert "action verb, such as improves, builds, or delivers, is not an ownership ask" in prose
    assert "A verb whose object is people" in prose
    assert "does not by itself name a disposition the requirement asks for" in prose
    assert "(such as production use, scale, domain, tool, or years)" not in prose
    assert "is not such a qualifier unless the requirement asks" in prose
    assert "otherwise use `TRANSFERABLE` or `GAP`" not in source


def test_the_match_prompt_reserves_weak_for_unmet_asks_of_the_applicant() -> None:
    """Only stated asks of the applicant cap strength; context, scale, or tool distance does not."""
    prose = " ".join(packaged("match_system").split())

    assert "Only what the requirement asks of the applicant is a qualifier" in prose
    assert "the employer's own product, policy, or team names" in prose
    assert "the teams the applicant would work with" in prose
    assert "only context, scale, or an adjacent tool of rule 4 may be `moderate`" in prose
    assert "No other unmet qualifier forces `weak`" in prose
    assert 'an "or" list is met by any one of its items' in prose
    assert "A qualifier the requirement states and the evidence does not meet" not in prose
