"""Ask the model critic, merge its sampled draws, and turn the verdict into a finding."""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from difflib import SequenceMatcher
from statistics import median_low

from clearscout.application.config import CriticStatistic, Stage
from clearscout.application.master_cv.cv import CvInventory
from clearscout.application.prompts import render
from clearscout.application.tailoring.guardrails import Guardrails
from clearscout.application.tailoring.stages.context import (
    copied_text,
    record_block,
    requirement_text,
)
from clearscout.application.tailoring.stages.jobspec import JobSpec
from clearscout.domain.identity import normalize_text
from clearscout.domain.statements import SAME_STATEMENT
from clearscout.infrastructure.providers.client import Client
from clearscout.infrastructure.providers.plan import RequestPlan, Segment
from clearscout.infrastructure.web_fetch.company_research import ResearchSource
from clearscout.infrastructure.web_fetch.company_research import evidence_block as research_block
from clearscout.shared.results import Finding, Outcome

from .citations import marked_uncitable
from .lexicon import FUNCTION_WORDS, figures_in
from .rubric import (
    CRITIC_CHECK,
    DECIDING_BOUNDARIES,
    QUALITY_AXES,
    QUOTED_CLAIM_WORDS,
    WRITTEN_PARTS,
)
from .verdicts import CriticVerdict, DoubtedClaim

MINIMUM_PRESERVATION = 4
MINIMUM_QUALITY = 3

_QUOTED = re.compile('["\u201c]([^"\u201c\u201d]+)["\u201d]')


def doubts_support(verdict: CriticVerdict) -> bool:
    """Return whether the critic doubts a claim or scores preservation below its floor."""
    return bool(verdict.unsupported_claims) or verdict.preservation < MINIMUM_PRESERVATION


def critic_system_prompt(guardrails: Guardrails) -> str:
    """Return the rubric, carrying the floors :func:`critic_finding` acts on.

    The floors are rendered from the constants rather than written into the
    prompt, so the rubric and the code that acts on it are one decision. It names
    no document, so the resume and letter critics share one cached prefix.
    """
    return render(
        "critic_system",
        distinctions=guardrails.prompt_block(),
        preservation_floor=str(MINIMUM_PRESERVATION),
        quality_floor=str(MINIMUM_QUALITY),
    )


def critique(
    client: Client,
    *,
    markdown: str,
    citations: str,
    inventory: CvInventory,
    offered: Sequence[str],
    spec: JobSpec,
    guardrails: Guardrails,
    posting_hint: str | None = None,
    active_disputes: str | None = None,
    document: str = "resume",
    research: tuple[ResearchSource, ...] = (),
) -> tuple[CriticVerdict, tuple[CriticVerdict, ...]]:
    """Ask one bounded question: is this draft aligned, and did it stay within the evidence.

    The question is put ``critic_trials`` times. Above one, the doubts are merged,
    because one sampled critique misses an unsupported claim another draw catches.
    Every draw is returned beside the merged verdict, in draw order. ``offered`` is
    the entries the drafter could cite, which alignment and guidance are held to.
    """
    policy = client.settings.policy_for(Stage.CRITIC)
    plan = RequestPlan(
        stage=Stage.CRITIC,
        instructions=Segment(critic_system_prompt(guardrails), "critic_system"),
        task=Segment(
            render(
                "critic_user",
                role=spec.title,
                requirements=requirement_text(spec),
                asserted=copied_text(inventory, guardrails),
                document=document,
                draft=markdown,
                citations=citations,
                offered="\n".join(offered),
                research=research_block(research),
                written_parts=WRITTEN_PARTS[document],
                posting_hint=posting_hint or "",
                active_disputes=active_disputes or "",
            ),
            "critic_user",
        ),
        evidence=Segment(record_block(inventory)),
        max_tokens=policy.max_tokens,
        draws=client.settings.critic_trials,
    )

    def draw(index: int) -> CriticVerdict:
        return client.complete(plan, CriticVerdict, variant=f"trial-{index}")

    # The first draw writes the provider prompt cache the later draws read, so it answers alone.
    trials: tuple[CriticVerdict, ...] = (draw(0),)
    if plan.draws > 1:
        with ThreadPoolExecutor(max_workers=plan.draws - 1) as pool:
            later = [pool.submit(draw, index) for index in range(1, plan.draws)]
            trials += tuple(future.result() for future in later)
    return _doubted_by_any(trials, client.settings.critic_score_statistic), trials


def _grouped_doubts(trials: Sequence[CriticVerdict]) -> list[list[DoubtedClaim]]:
    """Return the trials' doubts grouped by the claim they make, one doubt per draw a group."""
    groups: list[list[DoubtedClaim]] = []
    keys: list[tuple[str, frozenset[str], frozenset[str]]] = []
    draws: list[set[int]] = []
    # The critic may reword a claim between draws. A group holds one doubt per draw, so
    # its size counts the draws that raised it and a draw's second reason stays its own.
    for draw, verdict in enumerate(trials):
        for doubt in verdict.unsupported_claims:
            key = _claim_key(doubt.claim)
            joined = next(
                (
                    index
                    for index, held in enumerate(keys)
                    if draw not in draws[index] and _same_claim(held, key)
                ),
                None,
            )
            if joined is None:
                keys.append(key)
                groups.append([doubt])
                draws.append({draw})
            else:
                groups[joined].append(doubt)
                draws[joined].add(draw)
    return groups


def _doubted_by_any(trials: Sequence[CriticVerdict], statistic: CriticStatistic) -> CriticVerdict:
    """Return one verdict carrying every doubt any trial raised, scored across the draws."""
    # A doubt unions because a false positive is quoted and cheap to reject.
    groups = _grouped_doubts(trials)
    # Preservation is the faithfulness gate and wants recall, so one draw finding the
    # draft unfaithful decides it. A quality axis is a judgement, where a lone harsh
    # draw is an opinion the others disagree with.
    scores = {
        axis: _central([verdict.scores()[axis] for verdict in trials], statistic)
        for axis in QUALITY_AXES
    }
    scores["preservation"] = min(verdict.preservation for verdict in trials)
    return trials[0].model_copy(
        update={
            **scores,
            "unsupported_claims": [_merged_doubt(group) for group in groups],
            "guidance": _deciding_draw(trials, scores).guidance,
        }
    )


def _deciding_draw(trials: Sequence[CriticVerdict], scores: Mapping[str, int]) -> CriticVerdict:
    """Return the draw whose guidance explains the axis the merged scores fail on."""
    # The redraft feedback quotes the failing score and the guidance together,
    # so a mixed pair steers the rewrite at a complaint that score never made.
    failing = tuple(axis for axis in ("alignment", "specificity") if scores[axis] < MINIMUM_QUALITY)
    if not failing or scores["preservation"] < MINIMUM_PRESERVATION:
        failing = ("preservation",)
    # min keeps the earliest of tied draws.
    return min(trials, key=lambda verdict: min(verdict.scores()[axis] for axis in failing))


def _central(scores: Sequence[int], statistic: CriticStatistic) -> int:
    """Return one quality axis merged across the draws, as an observed draw's own integer."""
    if statistic is CriticStatistic.MINIMUM:
        return min(scores)
    return median_low(scores)


def doubted_span(claim: str) -> str:
    """Return the words a doubt is about: its longest quotation, or the whole claim."""
    # A draw may quote the doubted line and append its reasoning, so the quote is the claim.
    quoted = _QUOTED.findall(claim)
    return max(quoted, key=len) if quoted else claim


def _claim_key(claim: str) -> tuple[str, frozenset[str], frozenset[str]]:
    """Return a claim's folded wording, the figure values it states, and its content words."""
    span = doubted_span(claim)
    values = frozenset(value for _, values in figures_in(span) for value in values)
    folded = normalize_text(span)
    return folded, values, frozenset(folded.split()) - FUNCTION_WORDS


def _same_claim(
    held: tuple[str, frozenset[str], frozenset[str]],
    key: tuple[str, frozenset[str], frozenset[str]],
) -> bool:
    """Return whether two doubts are one statement, reworded at most.

    A span inside the other is the same statement when both state the same figures
    and the shorter carries at least three content words. Otherwise two claims
    differing in a figure or in any content word are different claims however
    close their wording, and folding them would drop a real doubt.
    """
    shorter, longer = sorted((held, key), key=lambda claim: len(claim[0]))
    # A draw may quote less of the line another draw quotes whole.
    if (
        f" {shorter[0]} " in f" {longer[0]} "
        and held[1] == key[1]
        and len(shorter[2]) >= QUOTED_CLAIM_WORDS
    ):
        return True
    if held[1] != key[1] or held[2] != key[2]:
        return False
    return SequenceMatcher(None, held[0], key[0]).ratio() >= SAME_STATEMENT


def _merged_doubt(group: Sequence[DoubtedClaim]) -> DoubtedClaim:
    """Return the first draw's claim under the boundary the group settles on."""
    named = [doubt.boundary for doubt in group]
    deciding = next((boundary for boundary in DECIDING_BOUNDARIES if boundary in named), None)
    return group[0].model_copy(
        update={
            "boundary": deciding or max(named, key=named.count),
            "corroboration": len(group),
        }
    )


def critic_finding(
    verdict: CriticVerdict, check: str = CRITIC_CHECK, *, uncitable: Collection[str] = ()
) -> Finding:
    """Convert the critic's verdict into a finding with explicit quality floors.

    Preservation is the non-negotiable gate, and very weak alignment or specificity
    triggers a rewrite. Very weak prose, merely adequate quality, and a style flag
    are warnings. Style flags, weaknesses, and guidance the detail does not already
    quote ride along as advisories.
    """
    guidance = marked_uncitable(verdict.guidance, uncitable)
    finding = _critic_verdict_finding(verdict, check, guidance)
    noted = [*verdict.style_flags, *verdict.weaknesses]
    advisories = tuple(marked_uncitable(text, uncitable) for text in noted)
    if guidance and guidance not in finding.detail:
        advisories += (guidance,)
    return replace(finding, advisories=advisories)


def _critic_verdict_finding(verdict: CriticVerdict, check: str, guidance: str) -> Finding:
    """Return the critic's outcome and the detail a redraft is sent when it fails."""
    if verdict.unsupported_claims:
        detail = "unsupported claims: " + "; ".join(
            str(claim) for claim in verdict.unsupported_claims[:5]
        )
        if floors := _failed_floors(verdict):
            detail = f"{detail}. {'; '.join(floors)}. {guidance}".rstrip()
        return Finding(check, Outcome.FAIL, detail)
    if verdict.preservation < MINIMUM_PRESERVATION:
        return Finding(
            check,
            Outcome.FAIL,
            f"content preservation {verdict.preservation}/5: {guidance}",
        )
    scores = verdict.quality_scores()
    failing = [f"{name} {score}/5" for name, score in scores.items() if score < MINIMUM_QUALITY]
    summary = ", ".join(f"{name} {score}/5" for name, score in scores.items())
    if failing:
        blocking = min(verdict.alignment, verdict.specificity) < MINIMUM_QUALITY
        below = f"quality below {MINIMUM_QUALITY}/5: {', '.join(failing)}"
        if verdict.style_flags and not blocking:
            below += f"; {_style_flag_count(verdict)}"
        return Finding(
            check,
            Outcome.FAIL if blocking else Outcome.WARN,
            f"{below}. {guidance}",
            redraftable=blocking,
        )
    strong = all(score >= MINIMUM_PRESERVATION for score in scores.values())
    detail = (
        f"{summary}, preservation {verdict.preservation}/5, "
        f"quality mean {verdict.quality_average:.1f}/5"
    )
    if verdict.style_flags:
        detail += f"; {_style_flag_count(verdict)}"
    return Finding(
        check,
        Outcome.PASS if strong and not verdict.style_flags else Outcome.WARN,
        detail,
        redraftable=not verdict.style_flags,
    )


def _style_flag_count(verdict: CriticVerdict) -> str:
    """Name how many style flags lead the finding's advisories."""
    count = len(verdict.style_flags)
    return f"{count} style flag{'' if count == 1 else 's'}, listed first"


def _failed_floors(verdict: CriticVerdict) -> list[str]:
    """Return the floors that fail a draft on their own, as a finding words them."""
    floors = []
    if verdict.preservation < MINIMUM_PRESERVATION:
        floors.append(f"content preservation {verdict.preservation}/5")
    blocking = [
        f"{name} {score}/5"
        for name, score in (("alignment", verdict.alignment), ("specificity", verdict.specificity))
        if score < MINIMUM_QUALITY
    ]
    if blocking:
        floors.append(f"quality below {MINIMUM_QUALITY}/5: {', '.join(blocking)}")
    return floors
