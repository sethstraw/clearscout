"""Scripted solvers: drive the shipped document stages against a canned provider.

Each solver assembles the deterministic context in ``evals.support.documents``,
feeds the sample's canned model answer through the in-process ``ScriptedProvider``,
and reports what the shipped verifier found. This catches a stale prompt template,
a closed-vocabulary regression, or a verifier that stopped catching a planted
defect; it measures nothing about answer quality.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from clearscout.application.tailoring.stages.verify import check_letter, critique
from evals.support.client import build_scripted_client
from evals.support.documents import (
    CRITIC_SCHEMA_KEY,
    critic_document,
    critic_result,
    document_context,
    draft_result,
    fitted_resume,
    fixed_letter,
    fixed_resume,
    letter_result,
    research_sources,
    scripted_payload,
    verdict_payload,
)
from evals.support.scripted_provider import ScriptedProvider


def draft_scripted_solver() -> Any:
    """Return the solver that drafts a resume from a canned answer and checks attempt one."""

    def solve(sample: dict[str, Any]) -> dict[str, Any]:
        context = document_context(sample)
        return draft_result(*fitted_resume(fixed_resume(sample, context), context))

    return solve


def cover_scripted_solver() -> Any:
    """Return the solver that writes a cover letter from a canned answer and checks it."""

    def solve(sample: dict[str, Any]) -> dict[str, Any]:
        context = document_context(sample)
        research = research_sources(sample)
        letter = fixed_letter(sample, context, research)
        report = check_letter(
            letter=letter,
            plan=context.plan,
            inventory=context.inventory,
            guardrails=context.guardrails,
            research=research,
        )
        return letter_result(letter, report)

    return solve


def critic_scripted_solver() -> Any:
    """Return the solver that critiques a fixed document with a canned verdict."""

    def solve(sample: dict[str, Any]) -> dict[str, Any]:
        context = document_context(sample)
        fixed = critic_document(sample, context)
        verdict = verdict_payload(scripted_payload(sample, "scripted_verdict"))

        def script(_schema: type[BaseModel]) -> dict[str, Any]:
            return verdict

        client = build_scripted_client(ScriptedProvider(script={CRITIC_SCHEMA_KEY: script}))
        returned, draws = critique(
            client,
            markdown=fixed.markdown,
            citations=fixed.citations,
            inventory=context.inventory,
            offered=fixed.offered,
            spec=context.spec,
            guardrails=context.guardrails,
            document=fixed.document,
            research=fixed.research,
        )
        return critic_result(returned, draws, fixed.check)

    return solve
