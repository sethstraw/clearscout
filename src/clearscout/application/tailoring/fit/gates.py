"""Which stated conditions a posting fails outright, before anything is spent.

Every gate here is decided from the deterministic reading and the person's own
parameters, so a posting can be refused without a model call. Three verdicts
rather than two: a posting that does not state a condition has not met it and
has not failed it, and that difference is the whole point of asking.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from clearscout.application.tailoring.fit.structure import Reading, RequirementLine, Stance
from clearscout.domain.search_parameters import Acceptance, JobSearchParameters, WorkEligibility
from clearscout.domain.search_parameters.conditions import excluded_by
from clearscout.domain.search_parameters.roles import title_excluded


class Standing(StrEnum):
    """What one condition says about whether this job can be taken at all."""

    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Gate:
    """One condition, how the posting answered it, and why."""

    name: str
    standing: Standing
    reason: str = ""

    def describe(self) -> dict[str, str]:
        """Return the gate as a report states it."""
        return {"name": self.name, "standing": self.standing.value, "reason": self.reason}


def _residence(reading: Reading) -> Gate:
    """Refuse a place this person has said they cannot work in."""
    place = reading.place_fit
    if place.value is None:
        return Gate("residence", Standing.UNKNOWN, place.note)
    _, _, standing = place.value.partition(": ")
    if standing == WorkEligibility.INELIGIBLE.value:
        return Gate("residence", Standing.INELIGIBLE, place.value)
    return Gate("residence", Standing.ELIGIBLE, place.value)


def _residence_requirement(reading: Reading) -> Gate:
    """Refuse a posting that requires living where this person declared no place.

    The reading abstains unless the requirement names a country no declared place
    satisfies, so a value here is already an unmeetable requirement.
    """
    answer = reading.residence_requirement
    if answer.value is not None:
        return Gate("residence requirement", Standing.INELIGIBLE, answer.value)
    return Gate("residence requirement", Standing.ELIGIBLE, answer.note)


def _arrangement(reading: Reading) -> tuple[Gate, Gate]:
    """Split the arrangement answer into the two conditions it carries."""
    answer = reading.arrangement_fit
    if answer.value is None:
        note = answer.note
        return (
            Gate("arrangement", Standing.UNKNOWN, note),
            Gate("office attendance", Standing.UNKNOWN, note),
        )
    accepted, _, breach = answer.value.partition(", but ")
    arrangement = (
        Gate("arrangement", Standing.INELIGIBLE, answer.note)
        if accepted == Acceptance.REFUSED.value
        else Gate("arrangement", Standing.ELIGIBLE, answer.value)
    )
    attendance = (
        Gate("office attendance", Standing.INELIGIBLE, breach)
        if breach
        else Gate("office attendance", Standing.ELIGIBLE, answer.value)
    )
    return arrangement, attendance


def _travel(reading: Reading) -> Gate:
    """Refuse a posting asking for more travel than this person accepts."""
    answer = reading.travel_fit
    if answer.value is None:
        return Gate("travel", Standing.UNKNOWN, answer.note)
    if answer.value.endswith(": over"):
        return Gate("travel", Standing.INELIGIBLE, answer.value)
    return Gate("travel", Standing.ELIGIBLE, answer.value)


def _time_zone(reading: Reading) -> Gate:
    """Refuse a posting requiring a working zone this person cannot reach."""
    answer = reading.time_zone_fit
    if answer.value is None:
        return Gate("time zone", Standing.UNKNOWN, answer.note)
    if answer.value.endswith(": outside"):
        return Gate("time zone", Standing.INELIGIBLE, answer.value)
    return Gate("time zone", Standing.ELIGIBLE, answer.value)


def _compensation(reading: Reading) -> Gate:
    """Report the pay the posting states against the person's floor.

    Pay under the floor scores against the posting and never refuses it.
    """
    answer = reading.pay_fit
    if answer.value is None:
        return Gate("compensation", Standing.UNKNOWN, answer.note)
    return Gate("compensation", Standing.ELIGIBLE, answer.value)


def _excluded_title(title: str, parameters: JobSearchParameters) -> Gate:
    """Refuse a job family this person asked to be left out."""
    excluded = [item for item in parameters.seniority.excluded_titles if item.strip()]
    if not excluded:
        return Gate("job family", Standing.UNKNOWN, "you have excluded no titles")
    if not title.strip():
        return Gate("job family", Standing.UNKNOWN, "the posting states no title")
    named = title_excluded(title, excluded)
    if named:
        return Gate("job family", Standing.INELIGIBLE, f"the title names {named}")
    return Gate("job family", Standing.ELIGIBLE, title)


def _excluded_employer(employer: str, parameters: JobSearchParameters) -> Gate:
    """Refuse an organization this person asked to leave out."""
    excluded = [item for item in parameters.employers.excluded_employers if item.strip()]
    if not excluded:
        return Gate("employer", Standing.UNKNOWN, "you have excluded no employers")
    if not employer.strip():
        return Gate("employer", Standing.UNKNOWN, "the posting names no employer")
    lowered = employer.casefold()
    named = next((item for item in excluded if item.casefold() in lowered), "")
    if named:
        return Gate("employer", Standing.INELIGIBLE, f"the employer is {named}")
    return Gate("employer", Standing.ELIGIBLE, employer)


def _must_statements(reading: Reading) -> tuple[str, ...]:
    """Return each must-have requirement, with lines a bare "or" links read as one."""
    chains: list[list[RequirementLine]] = []
    joined = False
    for line in reading.requirements:
        if joined:
            chains[-1].append(line)
        else:
            chains.append([line])
        joined = line.or_next
    return tuple(
        " or ".join(line.whole for line in chain)
        for chain in chains
        if chain[0].stance is Stance.MUST_HAVE
    )


def excluded_condition(reading: Reading, parameters: JobSearchParameters) -> Gate:
    """Refuse a posting that requires a credential this person has said they lack.

    Reads only the must-have lines, and abstains on a disjunction that offers an
    alternative the condition never named, so a satisfiable requirement is never a
    refusal.
    """
    conditions = [(item, reason) for item, reason in parameters.excluded_conditions if item.strip()]
    if not conditions:
        return Gate("credential", Standing.UNKNOWN, "you have excluded no conditions")
    hit = excluded_by(conditions, _must_statements(reading))
    if hit is not None:
        return Gate(
            "credential", Standing.INELIGIBLE, f"the posting states an excluded condition: {hit[0]}"
        )
    return Gate("credential", Standing.ELIGIBLE, "no excluded condition is required")


def gates(
    reading: Reading, parameters: JobSearchParameters, *, title: str = "", employer: str = ""
) -> tuple[Gate, ...]:
    """Return every stated condition, and how this posting answered it."""
    arrangement, attendance = _arrangement(reading)
    return (
        _residence(reading),
        _residence_requirement(reading),
        arrangement,
        attendance,
        _travel(reading),
        _time_zone(reading),
        _compensation(reading),
        _excluded_title(title, parameters),
        _excluded_employer(employer, parameters),
        excluded_condition(reading, parameters),
    )


def refusal(found: tuple[Gate, ...]) -> Gate | None:
    """Return the first condition this posting fails, if it fails one."""
    return next((gate for gate in found if gate.standing is Standing.INELIGIBLE), None)


def unanswered(found: tuple[Gate, ...]) -> tuple[Gate, ...]:
    """Return the conditions nothing has decided, which are the ones to go and ask."""
    return tuple(gate for gate in found if gate.standing is Standing.UNKNOWN)
