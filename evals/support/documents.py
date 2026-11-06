"""Assemble the inputs the drafting, letter, and critic stages need from one sample.

A document sample names a tracked synthetic CV under ``evals/cvs``, a posting,
and the classification each requirement gets. Matching runs through the shipped
stage against a canned provider, so the plan the later stages receive is the one
production selection builds rather than a hand-written stand-in. Nothing here
reaches a provider, a workspace, or a real CV.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from clearscout.application.config import Settings
from clearscout.application.master_cv.cv import CvInventory, parse_cv
from clearscout.application.tailoring.guardrails import Guardrails
from clearscout.application.tailoring.pipeline.rewrite import fitted_and_checked
from clearscout.application.tailoring.stages.draft import (
    CoverLetter,
    Fitted,
    ResumeDraft,
    cover_letter,
    draft,
    fit,
    skills_are_headed,
)
from clearscout.application.tailoring.stages.jobspec import JobSpec
from clearscout.application.tailoring.stages.match import (
    Classification,
    EvidenceMap,
    Strength,
    match,
)
from clearscout.application.tailoring.stages.select import Plan, build_plan
from clearscout.application.tailoring.stages.verify import (
    CRITIC_CHECK,
    LETTER_CRITIC,
    CriticVerdict,
    Report,
    critic_finding,
    line_citations,
)
from clearscout.infrastructure.web_fetch.company_research import (
    MAX_SOURCE_CHARACTERS,
    ResearchSource,
)
from evals.case import CVS, resolve_evidence
from evals.support.client import build_scripted_client
from evals.support.jobspec import canonical_job_spec, numbered_job_spec
from evals.support.scripted_provider import ScriptedProvider

# Recency scoring reads the calendar, so a fixed date keeps two runs of one sample identical.
PLANNING_DATE = date(2026, 6, 1)

# The page budget every document sample plans against.
PAGES = 2

# Synthetic rules every document sample is judged against. The forbidden phrases stay out of
# the honest fixtures, the required phrase is one their evidence carries, and the protected
# phrase is spelled one way in both tracked CVs.
DOCUMENT_GUARDRAILS = Guardrails(
    forbidden=["synergy", "results-driven"],
    required=["schema registry"],
    protected=["PostgreSQL"],
)

_MATCH_SCHEMA_KEY = "EvidenceMap"
DRAFT_SCHEMA_KEY = "ResumeDraft"
LETTER_SCHEMA_KEY = "CoverLetter"
CRITIC_SCHEMA_KEY = "CriticVerdict"


class DocumentSampleError(ValueError):
    """A document sample is malformed, or names something the CV does not hold."""


@dataclass(frozen=True)
class DocumentContext:
    """Everything deterministic one document sample runs on, before any prose exists."""

    sample_id: str
    cv: str
    inventory: CvInventory
    spec: JobSpec
    evidence: EvidenceMap
    plan: Plan
    guardrails: Guardrails
    settings: Settings
    aliases: Mapping[str, str]

    def entry_id(self, alias: str) -> str:
        """Return the entry identifier one readable alias names."""
        if alias not in self.aliases:
            raise DocumentSampleError(f"{self.sample_id}: no evidence alias named {alias!r}")
        return self.aliases[alias]

    def role_key(self, fragment: str) -> str:
        """Return the role key whose employer name contains ``fragment``, exactly once."""
        keys = [role.key for role in self.inventory.roles if fragment in role.organization]
        if len(keys) != 1:
            raise DocumentSampleError(
                f"{self.sample_id}: {fragment!r} names {len(keys)} roles in {self.cv}"
            )
        return keys[0]

    def line(self, line: Mapping[str, Any]) -> dict[str, Any]:
        """Render one scripted line as the drafting schema receives it."""
        evidence = line.get("evidence") or []
        if not isinstance(evidence, list):
            raise DocumentSampleError(
                f"{self.sample_id}: a scripted line's evidence must be a list"
            )
        return {
            "text": str(line.get("text", "")),
            "source_ids": [self.entry_id(str(alias)) for alias in evidence],
        }


def sample_metadata(sample: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the sample's solver-visible metadata object."""
    metadata = sample.get("metadata")
    if not isinstance(metadata, dict):
        raise DocumentSampleError("a document sample needs a metadata object")
    return metadata


def scripted_payload(sample: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """Return one canned model answer the sample carries, by metadata key."""
    payload = sample_metadata(sample).get(key)
    if not isinstance(payload, dict):
        raise DocumentSampleError(f"a document sample is missing metadata.{key}")
    return payload


def _cv_path(name: Any) -> Path:
    if not isinstance(name, str) or not name.strip():
        raise DocumentSampleError("a document sample must name its CV in metadata.cv")
    path = CVS / name
    if path.parent != CVS or not path.is_file():
        raise DocumentSampleError(f"document sample CV is not a tracked evaluation CV: {name}")
    return path


def _match_rows(
    sample: Mapping[str, Any], spec: JobSpec, aliases: Mapping[str, str], sample_id: str
) -> list[dict[str, Any]]:
    rows = sample_metadata(sample).get("matches")
    if not isinstance(rows, list) or not rows:
        raise DocumentSampleError(f"{sample_id}: metadata.matches must name every requirement")
    wanted = [requirement.id for requirement in spec.requirements]
    if [str(row.get("requirement_id")) for row in rows] != wanted:
        raise DocumentSampleError(f"{sample_id}: metadata.matches must list {wanted} in that order")
    built = []
    for row in rows:
        classification = Classification(str(row.get("classification")))
        cited = row.get("evidence") or []
        if classification in {Classification.GAP, Classification.ASSERTED} and cited:
            raise DocumentSampleError(
                f"{sample_id}: a {classification.value} requirement cites no evidence"
            )
        built.append(
            {
                "requirement_id": str(row["requirement_id"]),
                "entry_ids": [aliases[str(alias)] for alias in cited],
                "classification": classification.value,
                "strength": Strength(str(row.get("strength", "none"))).value,
                "note": str(row.get("note", "")),
            }
        )
    return built


def document_context(sample: Mapping[str, Any]) -> DocumentContext:
    """Return the inventory, posting, evidence map, and plan one document sample runs on."""
    metadata = sample_metadata(sample)
    sample_id = str(sample.get("id") or "document sample")
    path = _cv_path(metadata.get("cv"))
    inventory = parse_cv(path)
    if sample.get("cv_hash") != inventory.content_hash:
        raise DocumentSampleError(
            f"{sample_id}: cv_hash does not match the tracked evaluation CV {path.name}"
        )
    if "job_spec" not in sample:
        raise DocumentSampleError(f"{sample_id}: a document sample must carry a job_spec")
    spec = numbered_job_spec(canonical_job_spec(sample["job_spec"]))
    aliases = resolve_evidence(metadata.get("evidence") or {}, inventory, sample_id)
    guardrails = DOCUMENT_GUARDRAILS
    settings = Settings(cv=path, pages=PAGES, rules=guardrails, cache=False)
    rows = _match_rows(sample, spec, aliases, sample_id)

    def script(_schema: type[BaseModel]) -> dict[str, Any]:
        return {"matches": rows}

    client = build_scripted_client(ScriptedProvider(script={_MATCH_SCHEMA_KEY: script}))
    evidence = match(client, inventory, spec, guardrails)
    plan = build_plan(inventory, spec, evidence, settings, PLANNING_DATE)
    if not plan.all_ids():
        raise DocumentSampleError(f"{sample_id}: selection kept nothing to write from")
    return DocumentContext(
        sample_id=sample_id,
        cv=path.name,
        inventory=inventory,
        spec=spec,
        evidence=evidence,
        plan=plan,
        guardrails=guardrails,
        settings=settings,
        aliases=aliases,
    )


def drafted_payload(
    scripted: Mapping[str, Any], context: DocumentContext, *, labelled: bool
) -> dict[str, Any]:
    """Render a scripted resume draft as the run's closed drafting schema receives it."""
    skills = [
        {
            **({"label": str(skill.get("label", ""))} if labelled else {}),
            "text": str(skill.get("text", "")),
            "source_ids": [context.entry_id(str(alias)) for alias in skill.get("evidence") or []],
        }
        for skill in scripted.get("skills") or []
    ]
    earlier = scripted.get("earlier_career")
    return {
        "summary": context.line(scripted["summary"]),
        # A draft schema over a CV with no skill entries has no skills field at all,
        # and one over a plan with no earlier role has no earlier_career field.
        **({"skills": skills} if skills else {}),
        **({} if earlier is None else {"earlier_career": context.line(earlier)}),
        "roles": [
            {
                "role_key": context.role_key(str(role["role"])),
                "context": None if role.get("context") is None else context.line(role["context"]),
                "bullets": [context.line(bullet) for bullet in role.get("bullets") or []],
            }
            for role in scripted.get("roles") or []
        ],
    }


def letter_payload(
    scripted: Mapping[str, Any],
    context: DocumentContext,
    research: Sequence[ResearchSource] = (),
) -> dict[str, Any]:
    """Render a scripted cover letter as the run's closed letter schema receives it.

    A paragraph cites a CV entry by its alias and a research source by its own id.
    """
    citable = replace(
        context, aliases={**context.aliases, **{item.id: item.id for item in research}}
    )
    return {
        "paragraphs": [citable.line(paragraph) for paragraph in scripted.get("paragraphs") or []]
    }


# A synthetic url for every sample's posting, which the research block prints beside its text.
POSTING_URL = "https://jobs.example.com/eval-posting"


def research_sources(sample: Mapping[str, Any]) -> tuple[ResearchSource, ...]:
    """Return the research a letter is written from: the posting, then the fixture's pages."""
    posting = sample.get("input")
    if not isinstance(posting, str) or not posting.strip():
        raise DocumentSampleError("a letter sample must carry its posting text in input")
    rows = sample_metadata(sample).get("research") or []
    if not isinstance(rows, list):
        raise DocumentSampleError("metadata.research must be a list of sources")
    stored = ResearchSource(
        id="company.posting",
        kind="posting",
        url=POSTING_URL,
        text=posting[:MAX_SOURCE_CHARACTERS],
        method="stored_posting_artifact",
    )
    return stored, *(
        ResearchSource(
            id=str(row["id"]),
            kind=row["kind"],
            url=str(row["url"]),
            text=str(row["text"]),
            method="fixture",
        )
        for row in rows
    )


def _scripted_client(key: str, payload: dict[str, Any]) -> Any:
    provider = ScriptedProvider(script={key: lambda _schema: payload})
    return build_scripted_client(provider)


def fixed_resume(sample: Mapping[str, Any], context: DocumentContext) -> ResumeDraft:
    """Return the sample's canned resume as the shipped drafting stage produces it."""
    entries = [context.inventory.by_id(entry_id) for entry_id in context.plan.skill_ids]
    payload = drafted_payload(
        scripted_payload(sample, "scripted_draft"), context, labelled=not skills_are_headed(entries)
    )
    client = _scripted_client(DRAFT_SCHEMA_KEY, payload)
    return draft(client, context.inventory, context.spec, context.plan, context.guardrails)


def fitted_resume(drafted: ResumeDraft, context: DocumentContext) -> tuple[Fitted, Report]:
    """Fit a drafted resume to the sample's pages and check what prints, as a run does."""
    return fitted_and_checked(
        drafted,
        inventory=context.inventory,
        spec=context.spec,
        plan=context.plan,
        guardrails=context.guardrails,
        settings=context.settings,
    )


def fixed_letter(
    sample: Mapping[str, Any], context: DocumentContext, research: tuple[ResearchSource, ...]
) -> CoverLetter:
    """Return the sample's canned cover letter as the shipped stage produces it."""
    payload = letter_payload(scripted_payload(sample, "scripted_letter"), context, research)
    client = _scripted_client(LETTER_SCHEMA_KEY, payload)
    return cover_letter(
        client, context.inventory, context.spec, context.plan, context.guardrails, research=research
    )


@dataclass(frozen=True)
class CriticDocument:
    """A fixed document under review, with what the product's critique site passes beside it."""

    markdown: str
    citations: str
    offered: tuple[str, ...]
    document: str
    research: tuple[ResearchSource, ...]
    check: str


def critic_document(sample: Mapping[str, Any], context: DocumentContext) -> CriticDocument:
    """Assemble the sample's canned resume or letter offline, as its critique site would see it.

    The draft travels as a citation index because the critic reads each line under
    the identifiers that line cites, which the rendered document does not carry.
    """
    if "scripted_letter" in sample_metadata(sample):
        research = research_sources(sample)
        letter = fixed_letter(sample, context, research)
        return CriticDocument(
            markdown=letter.rendered(),
            citations=line_citations(letter.paragraphs, context.inventory, "Paragraph"),
            offered=context.plan.drafted_ids(),
            document="cover letter",
            research=research,
            check=LETTER_CRITIC,
        )
    fitted = fit(
        fixed_resume(sample, context),
        context.inventory,
        context.plan,
        context.settings,
        context.guardrails,
    )
    return CriticDocument(
        markdown=fitted.markdown,
        citations=line_citations(fitted.printed.lines(), context.inventory),
        offered=fitted.printed_plan.all_ids(),
        document="resume",
        research=(),
        check=CRITIC_CHECK,
    )


def verdict_payload(scripted: Mapping[str, Any]) -> dict[str, Any]:
    """Return a scripted critic verdict, validated against the shipped verdict model."""
    return CriticVerdict.model_validate(dict(scripted)).model_dump(mode="json")


def findings(report: Report) -> list[dict[str, Any]]:
    """Return every check the shipped verifier ran, with its verdict and detail."""
    return [
        {
            "check": finding.check,
            "outcome": finding.outcome.value,
            "detail": finding.detail,
            "redraftable": finding.redraftable,
        }
        for finding in report.findings
    ]


def draft_result(fitted: Fitted, report: Report) -> dict[str, Any]:
    """Return the solver output one resume attempt produces, measured as it prints."""
    return {
        "completion": "pass" if report.passed else "fail",
        "findings": findings(report),
        "document": {"lines": len(fitted.printed.lines()), "characters": len(fitted.markdown)},
    }


def critic_result(
    verdict: CriticVerdict, draws: Sequence[CriticVerdict], check: str = CRITIC_CHECK
) -> dict[str, Any]:
    """Return the solver output one critique produces: the merged verdict and every draw."""
    finding = critic_finding(verdict, check)
    # Corroboration is counted across draws at merge time, so a single draw has none.
    per_draw = {"unsupported_claims": {"__all__": {"corroboration"}}}
    return {
        "completion": finding.outcome.value,
        "verdict": verdict.model_dump(mode="json"),
        "draws": [draw.model_dump(mode="json", exclude=per_draw) for draw in draws],
        "finding": {"check": finding.check, "detail": finding.detail},
    }


def letter_result(
    letter: CoverLetter, report: Report, *, keep_text: bool = False
) -> dict[str, Any]:
    """Return the solver output one cover-letter attempt produces, with its body when kept."""
    rendered = letter.rendered()
    document: dict[str, Any] = {
        "paragraphs": len(letter.paragraphs),
        "words": len(rendered.split()),
    }
    if keep_text:
        document["text"] = rendered
    return {
        "completion": "pass" if report.passed else "fail",
        "findings": findings(report),
        "document": document,
    }
