"""Build a one-requirement ``JobSpec`` from one Inspect sample.

The scripted and live solvers score per requirement, so the harness assembles
a minimum ``JobSpec`` around each sample's text. Nothing here reads a real
posting or a workspace database: the requirement text and the requirement id
come from the sample, and every other field takes a stable placeholder value
so two runs on the same sample produce byte-identical inputs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from tailorcv.application.master_cv.cv import CvInventory
from tailorcv.application.tailoring.stages.jobspec import (
    Dimension,
    Importance,
    JobSpec,
    Requirement,
    requirement_vocabulary,
)

if TYPE_CHECKING:
    from tailorcv.application.tailoring.stages.match import EvidenceMap

_ORIGIN = "evals.inspect.solvers"


def validated_dimension(sample: dict[str, Any], inventory: CvInventory) -> Dimension:
    """Validate a match sample's frozen inventory identity and dimension."""
    cv_hash = sample.get("cv_hash")
    if cv_hash != inventory.content_hash:
        raise ValueError("Match sample CV hash does not match the synthetic inventory")

    value = sample.get("dimension")
    try:
        return Dimension(str(value).strip().lower())
    except ValueError as error:
        raise ValueError(f"Unknown match sample dimension: {value!r}") from error


def one_requirement_spec(
    *,
    requirement_id: str,
    requirement_text: str,
    dimension: Dimension = Dimension.HARD,
    title: str = "Backend Engineer",
    company: str = "Aster Systems",
) -> JobSpec:
    """Return a ``JobSpec`` carrying exactly one requirement."""
    requirement = Requirement(
        id=requirement_id,
        text=requirement_text,
        importance=Importance.MUST_HAVE,
        dimension=dimension,
    )
    return JobSpec(
        title=title,
        company=company,
        seniority=None,
        location=None,
        requirements=(requirement,),
        keywords=(),
        hard_gates=(),
        origin=_ORIGIN,
    )


def positional_requirement_ids(count: int) -> tuple[str, ...]:
    """Return the first ``count`` ids the match schema offers, from the shipped owner."""
    return requirement_vocabulary(count)[:count]


def _reject_unknown_fields(value: Any, model: type[BaseModel], where: str) -> None:
    # JobSpec/Requirement are extra="ignore", so a nested expected label would
    # validate and reach the solver; reject unknown keys to keep it out.
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object")
    unknown = sorted(set(value) - set(model.model_fields))
    if unknown:
        raise ValueError(f"{where} carries unsupported fields: {unknown}")


def canonical_job_spec(value: Any) -> JobSpec:
    """Validate an explicit posting as a JobSpec, forbidding scorer-only material."""
    _reject_unknown_fields(value, JobSpec, "job_spec")
    requirements = value.get("requirements")
    if isinstance(requirements, list):
        for index, requirement in enumerate(requirements):
            _reject_unknown_fields(requirement, Requirement, f"job_spec.requirements[{index}]")
    spec = JobSpec.model_validate(value)
    ids = [requirement.id for requirement in spec.requirements]
    if not ids:
        raise ValueError("job_spec must carry at least one requirement")
    if any(not identifier or identifier != identifier.strip() for identifier in ids):
        raise ValueError("job_spec requirement ids must be nonempty and unpadded")
    if len(ids) != len(set(ids)):
        raise ValueError("job_spec requirement ids must be unique")
    if any(not requirement.text.strip() for requirement in spec.requirements):
        raise ValueError("job_spec requirement text must not be empty")
    return spec


def sample_job_spec(sample: dict[str, Any], inventory: CvInventory) -> JobSpec:
    """Return the sample's posting: an explicit JobSpec or a one-requirement spec."""
    if "job_spec" not in sample:
        return one_requirement_spec(
            requirement_id=positional_requirement_ids(1)[0],
            requirement_text=str(sample.get("input") or sample.get("requirement_text") or ""),
            dimension=validated_dimension(sample, inventory),
        )
    if any(key in sample for key in ("input", "requirement_text", "dimension")):
        raise ValueError("a job_spec sample cannot also carry single-requirement inputs")
    if sample.get("cv_hash") != inventory.content_hash:
        raise ValueError("Match sample CV hash does not match the synthetic inventory")
    return canonical_job_spec(sample["job_spec"])


def numbered_job_spec(spec: JobSpec) -> JobSpec:
    """Renumber onto the shipped vocabulary; an external id would normalize to all-gap."""
    ids = positional_requirement_ids(len(spec.requirements))
    renumbered = tuple(
        requirement.model_copy(update={"id": identifier})
        for identifier, requirement in zip(ids, spec.requirements, strict=True)
    )
    return spec.model_copy(update={"requirements": renumbered})


def requirement_outcomes(spec: JobSpec, evidence: EvidenceMap) -> list[dict[str, Any]]:
    """Map positional match rows back to each requirement's stable id.

    A row the run could not establish is reported incomplete, so the scorer
    cannot credit a silent omission; a coverage mismatch fails explicitly.
    """
    positional = list(positional_requirement_ids(len(spec.requirements)))
    observed = [item.requirement_id for item in evidence.matches]
    if evidence.unrecognized or observed != positional:
        raise ValueError("match returned outcomes that do not cover each requirement exactly once")
    by_id = {item.requirement_id: item for item in evidence.matches}
    outcomes: list[dict[str, Any]] = []
    for identifier, requirement in zip(positional, spec.requirements, strict=True):
        row = by_id[identifier]
        outcomes.append(
            {
                "requirement_id": requirement.id,
                "completion": row.classification.value,
                "metadata": {
                    "entry_keys": list(row.entry_ids),
                    "named_tools": list(row.named_tools),
                    "strength": row.strength.value,
                    "outcome_complete": not row.needs_review,
                },
            }
        )
    return outcomes
