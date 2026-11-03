"""Draft, verify, and rewrite within one shared deterministic attempt budget."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, TypeVar

from clearscout.application.config import Settings
from clearscout.application.master_cv.cv import CvInventory
from clearscout.application.tailoring.guardrails import Guardrails
from clearscout.application.tailoring.layout import DRAFTED_SHARE, Budget, wrapped_lines
from clearscout.application.tailoring.stages import draft as draft_stage
from clearscout.application.tailoring.stages import select as select_stage
from clearscout.application.tailoring.stages import verify as verify_stage
from clearscout.application.tailoring.stages.jobspec import JobSpec
from clearscout.application.tailoring.stages.match import EvidenceMap
from clearscout.infrastructure.providers.client import Client
from clearscout.infrastructure.providers.plan import digest_of
from clearscout.infrastructure.web_fetch.company_research import ResearchSource
from clearscout.shared.errors import BudgetError
from clearscout.shared.results import Finding

from .progress import _Progress

# A resume this many lines short of its page budget gets a note saying why.
UNSPENT_WORTH_MENTIONING = 10

ArtifactT = TypeVar("ArtifactT")


def _require_experience(plan: select_stage.Plan, spec: JobSpec) -> None:
    """Refuse to write a resume whose experience section would be empty."""
    if any(plan.role_bullets.get(role_key) for role_key in plan.recent_roles):
        return
    raise BudgetError(
        f"Nothing in the master CV answered any requirement of {spec.title!r}, so the\n"
        "experience section would be empty. Either the posting is for a different\n"
        "field, or the CV's experience did not parse. Run 'clearscout doctor' to check\n"
        "the parse, and read the requirement map in a previous report."
    )


@dataclass(frozen=True)
class Attempt:
    """One write-and-check pass and a digest of the feedback sent to the next one.

    The rejected text is not kept; the findings and digest identify why it was rejected.
    """

    number: int
    findings: tuple[Finding, ...]
    feedback_digest: str = ""
    kept: bool = False

    def payload(self) -> dict[str, Any]:
        """Return the attempt as the run manifest records it."""
        return {
            "attempt": self.number,
            "findings": [
                {"check": item.check, "outcome": item.outcome.value, "detail": item.detail}
                for item in self.findings
            ],
            "feedback_digest": self.feedback_digest,
        }


def _until_clean(
    *,
    steps: _Progress,
    label: str,
    attempts: int,
    produce: Callable[[str, int], tuple[ArtifactT, verify_stage.Report]],
) -> tuple[ArtifactT, verify_stage.Report, tuple[Attempt, ...]]:
    """Write, check, and rewrite until the checks pass, stall, or run out of attempts.

    A check failing again stalls the loop unless its counted offenders shrank since it last failed.
    """
    feedback = ""
    seen: dict[str, int | None] = {}
    history: list[Attempt] = []
    tried: list[tuple[ArtifactT, verify_stage.Report]] = []
    for attempt in range(1, attempts + 1):
        steps.within(f"{label}, attempt {attempt} of {attempts}")
        written, report = produce(feedback, attempt)
        tried.append((written, report))
        if not report.passed:
            steps.within(f"{len(report.findings)} finding(s) to answer")
        if report.passed or not report.worth_redrafting or attempt == attempts:
            history.append(Attempt(attempt, report.findings))
            break
        counts = {check: report.offenders.get(check) for check in report.redraftable_checks()}
        stalled = sorted(
            check
            for check, count in counts.items()
            if check in seen and not _shrank(seen[check], count)
        )
        if stalled:
            steps.within(f"{', '.join(stalled)} failed again, so no more attempts")
            history.append(Attempt(attempt, report.findings))
            break
        seen.update(counts)
        feedback = report.feedback()
        history.append(Attempt(attempt, report.findings, digest_of(feedback)))
    kept = min(range(1, len(tried) + 1), key=lambda number: _rank(tried[number - 1][1], number))
    written, report = tried[kept - 1]
    return written, report, tuple(replace(item, kept=item.number == kept) for item in history)


def _rank(report: verify_stage.Report, attempt: int) -> tuple[bool, bool, int, int, int, int]:
    """Return the sort key that picks the attempt to keep, lowest first."""
    failures = report.failures
    # A clean attempt ranks first. A failing attempt the critic judged beats one it never read,
    # whose support nobody checked; then fewer support failures beat fewer length or style ones.
    offenders = sum(report.offenders.get(finding.check, 1) for finding in failures)
    return (
        bool(failures),
        report.critic is None,
        report.unsupported_failures(),
        len(failures),
        offenders,
        attempt,
    )


def kept_attempt(history: tuple[Attempt, ...]) -> int:
    """Return the number of the attempt whose document was kept."""
    return next(item.number for item in history if item.kept)


def _shrank(before: int | None, after: int | None) -> bool:
    """Return whether a counted check has fewer offenders than when it last failed."""
    return before is not None and after is not None and after < before


def _worth_criticising(
    attempt: int,
    verification: verify_stage.Report,
    evidence: EvidenceMap,
    inventory: CvInventory,
    spec: JobSpec,
    settings: Settings,
) -> bool:
    """Return whether this draft carries a risk the critic is paid to catch."""
    return (
        attempt > 1
        or verification.coverage.ratio < settings.critic_coverage_floor
        or evidence.indirect_share(inventory, spec.requirements)
        > settings.critic_transferable_ceiling
    )


def _draft_until_clean(
    client: Client,
    *,
    inventory: CvInventory,
    spec: JobSpec,
    plan: select_stage.Plan,
    guardrails: Guardrails,
    settings: Settings,
    trim: int,
    steps: _Progress,
    evidence: EvidenceMap,
    posting_hint: str | None = None,
    active_disputes: str | None = None,
) -> tuple[draft_stage.Fitted, verify_stage.Report, tuple[Attempt, ...]]:
    """Draft, fit to the page, check, and rewrite the resume within the attempt budget."""
    active_hint = posting_hint if settings.postings.enable_hint else None

    def produce(feedback: str, attempt: int) -> tuple[draft_stage.Fitted, verify_stage.Report]:
        drafted = draft_stage.draft(
            client,
            inventory,
            spec,
            plan,
            guardrails,
            feedback,
            posting_hint=active_hint,
            active_disputes=active_disputes,
        )
        fitted, verification = fitted_and_checked(
            drafted,
            inventory=inventory,
            spec=spec,
            plan=plan,
            guardrails=guardrails,
            settings=settings,
            trim=trim,
        )
        if not verification.passed:
            return fitted, verification
        if not _worth_criticising(attempt, verification, evidence, inventory, spec, settings):
            steps.within("checks passed and nothing looks risky, so no critic")
            return fitted, verification.without_critic(
                "The draft passed every check on its first attempt with strong coverage "
                "and directly cited evidence, so no critique was asked for."
            )
        steps.within("checks passed, asking the critic")
        return fitted, critiqued(
            client,
            fitted,
            verification,
            inventory=inventory,
            spec=spec,
            guardrails=guardrails,
            posting_hint=active_hint,
            active_disputes=active_disputes,
        )

    return _until_clean(
        steps=steps,
        label="drafting",
        attempts=settings.max_draft_attempts,
        produce=produce,
    )


def fitted_and_checked(
    drafted: draft_stage.ResumeDraft,
    *,
    inventory: CvInventory,
    spec: JobSpec,
    plan: select_stage.Plan,
    guardrails: Guardrails,
    settings: Settings,
    trim: int = 0,
) -> tuple[draft_stage.Fitted, verify_stage.Report]:
    """Fit a draft to its pages and run every programmatic check on what prints."""
    fitted = draft_stage.fit(drafted, inventory, plan, settings, guardrails, trim)
    verification = verify_stage.check(
        markdown=fitted.markdown,
        draft=fitted.printed,
        plan=plan,
        inventory=inventory,
        spec=spec,
        guardrails=guardrails,
        excluded=fitted.excluded,
    )
    return fitted, verification


def critiqued(
    client: Client,
    fitted: draft_stage.Fitted,
    verification: verify_stage.Report,
    *,
    inventory: CvInventory,
    spec: JobSpec,
    guardrails: Guardrails,
    posting_hint: str | None = None,
    active_disputes: str | None = None,
) -> verify_stage.Report:
    """Ask the critic about the printed resume and return the checks with its verdict."""
    verdict, draws = verify_stage.critique(
        client,
        markdown=fitted.markdown,
        citations=verify_stage.line_citations(fitted.printed.lines(), inventory),
        inventory=inventory,
        offered=fitted.printed_plan.all_ids(),
        spec=spec,
        guardrails=guardrails,
        posting_hint=posting_hint,
        active_disputes=active_disputes,
    )
    return verification.with_critic(verdict, draws=draws)


@dataclass(frozen=True)
class WrittenLetter:
    """The cover letter that landed, its text, checks, attempts, and citable research."""

    letter: draft_stage.CoverLetter
    text: str
    verification: verify_stage.Report
    attempts: tuple[Attempt, ...]
    research: tuple[ResearchSource, ...] = ()


def _render_cover_letter(
    client: Client,
    inventory: CvInventory,
    spec: JobSpec,
    plan: select_stage.Plan,
    guardrails: Guardrails,
    *,
    settings: Settings,
    steps: _Progress,
    want: bool,
    research: tuple[ResearchSource, ...] = (),
    posting_hint: str | None = None,
    active_disputes: str | None = None,
    employer: str | None = None,
) -> WrittenLetter | None:
    """Draft, check, critique, and render a requested cover letter."""
    if not want:
        return None
    active_hint = posting_hint if settings.postings.enable_hint else None
    drafted: list[draft_stage.CoverLetter] = []

    def produce(feedback: str, attempt: int) -> tuple[draft_stage.CoverLetter, verify_stage.Report]:
        del attempt
        letter = draft_stage.cover_letter(
            client,
            inventory,
            spec,
            plan,
            guardrails,
            feedback,
            research=research,
            posting_hint=active_hint,
            active_disputes=active_disputes,
            employer=employer,
            previous=drafted[-1] if drafted else None,
        )
        drafted.append(letter)
        verification = verify_stage.check_letter(
            letter=letter,
            plan=plan,
            inventory=inventory,
            guardrails=guardrails,
            research=research,
        )
        if not verification.passed:
            return letter, verification
        steps.within("letter checks passed, asking the critic")
        verdict, draws = verify_stage.critique(
            client,
            markdown=letter.rendered(),
            citations=verify_stage.line_citations(letter.paragraphs, inventory, "Paragraph"),
            inventory=inventory,
            offered=plan.drafted_ids(),
            spec=spec,
            guardrails=guardrails,
            posting_hint=active_hint,
            active_disputes=active_disputes,
            document="cover letter",
            research=research,
        )
        return letter, verification.with_critic(verdict, verify_stage.LETTER_CRITIC, draws)

    letter, report, attempts = _until_clean(
        steps=steps,
        label="writing the letter",
        attempts=settings.max_draft_attempts,
        produce=produce,
    )
    return WrittenLetter(letter, letter.markdown(), report, attempts, research)


def _notes(
    settings: Settings,
    plan: select_stage.Plan,
    inventory: CvInventory,
    fitted: draft_stage.Fitted,
) -> list[str]:
    """Return neutral facts about the run."""
    budget = Budget(settings.pages)
    notes = [
        f"Length was fitted to {settings.pages} page(s) from an estimate, not measured "
        f"(clearscout does not render): about {fitted.total_lines:.0f} of "
        f"{budget.total_lines:.0f} lines, page {settings.pages} about "
        f"{budget.last_page_share(fitted.total_lines):.0%} full.\n"
        + (
            f"  If the rendered PDF runs long: --trim 1   (up to {len(fitted.remaining)} "
            "can be dropped)"
            if fitted.remaining
            else "  Nothing more can be dropped without losing a must-have answer or a "
            "role's last bullets."
        )
    ]
    if reason := short_page(settings, plan, fitted):
        notes.append(reason)
    notes += cap_notes(settings, plan, inventory, fitted)
    notes += publication_notes(settings, plan, inventory, fitted)
    if unprinted := unprinted_earlier_career(fitted):
        notes.append(unprinted)
    if fitted.requested:
        notes.append(f"Dropped {len(fitted.requested)} item(s) in drop order at your request.")
    return notes


def short_page(
    settings: Settings,
    plan: select_stage.Plan,
    fitted: draft_stage.Fitted,
) -> str:
    """Return why the fitted resume ends short of its pages, or "" when it does not."""
    budget = Budget(settings.pages)
    short = budget.total_lines - fitted.total_lines
    if short < UNSPENT_WORTH_MENTIONING or fitted.requested:
        return ""
    fewer = settings.pages - 1
    if fewer and fitted.total_lines <= Budget(fewer).total_lines:
        opening = (
            f"The resume ends about {short:.0f} line(s) short of {settings.pages} page(s), and "
            f"its estimate fits on {fewer}: re-run with --pages {fewer}."
        )
    else:
        opening = (
            f"The resume ends about {short:.0f} line(s) short of {settings.pages} page(s); "
            f"page {settings.pages} is about {budget.last_page_share(fitted.total_lines):.0%} "
            "full."
        )
    if plan.top_up_stop is select_stage.TopUpStop.EVIDENCE:
        reason = (
            "\n  Every cited bullet from the recent roles (the last "
            f"{settings.full_bullet_years} years) is already on it, and clearscout does not "
            "fill a page with uncited material."
        )
    else:
        reason = "\n  Cited bullets were left out, but the drafted text ran shorter than estimated."
    return opening + reason


def cap_notes(
    settings: Settings,
    plan: select_stage.Plan,
    inventory: CvInventory,
    fitted: draft_stage.Fitted,
) -> list[str]:
    """Return what the per-role cap did to the printed page, where it did anything."""
    cap = settings.max_bullets_per_role
    printed = {role.role_key: len(role.bullets) for role in fitted.printed.roles}
    past = [key for key in plan.recent_roles if printed.get(key, 0) > cap]
    notes = [
        f"{_role_name(inventory, key)} prints {printed[key]} bullets, past "
        f"max_bullets_per_role = {cap}."
        for key in past
    ]
    if past and any(printed.get(key, 0) < cap for key in plan.recent_roles):
        notes.append("Every role below the cap had already placed all its cited bullets.")
    if plan.withheld_by_cap and _page_holds(plan.withheld_by_cap, inventory, settings, fitted):
        notes.append(
            f"{len(plan.withheld_by_cap)} cited bullet(s) that fit the page were held back by "
            f"max_bullets_per_role = {cap}\n  so other roles could place theirs; raise it in "
            "clearscout.toml to let a strong role take more of the page."
        )
    if bare := _roles_without_bullets(plan, inventory, fitted):
        one = len(bare) == 1
        notes.append(
            f"{'; '.join(bare)} {'prints' if one else 'print'} without bullets. If "
            f"{'it holds' if one else 'they hold'} relevant work the master CV does not yet "
            "describe, adding it gives selection more to choose from."
        )
    return notes


UNOFFERED_NOTE_LIMIT = 5


def publication_notes(
    settings: Settings,
    plan: select_stage.Plan,
    inventory: CvInventory,
    fitted: draft_stage.Fitted,
) -> list[str]:
    """Return the papers left off the offer or trimmed, and must-consider ids the CV lacks."""
    unoffered = sorted(plan.unoffered_publications, key=lambda key: -plan.scores.get(key, 0.0))
    notes = []
    if len(unoffered) > UNOFFERED_NOTE_LIMIT:
        notes.append(
            f"{len(unoffered)} publications available, not offered; the strongest "
            f"{UNOFFERED_NOTE_LIMIT} follow, and the run manifest lists them all."
        )
    notes += [
        f"Publication available, not offered (id {entry_id}"
        f"{', cited but over max_publications' if plan.scores.get(entry_id, 0.0) > 0 else ''})"
        f": {inventory.by_id(entry_id).text}"
        for entry_id in unoffered[:UNOFFERED_NOTE_LIMIT]
    ]
    notes += [
        f"Publication trimmed for length: {inventory.by_id(entry_id).text}"
        for entry_id in fitted.trimmed
        if entry_id in settings.must_consider_publications
    ]
    if plan.stale_must_consider:
        notes.append(
            "must_consider_publications names id(s) with no publication in the master CV: "
            f"{', '.join(plan.stale_must_consider)}"
        )
    return notes


def unprinted_earlier_career(fitted: draft_stage.Fitted) -> str:
    """Return a note quoting a drafted earlier-career line the page left off, or ""."""
    drafted = fitted.draft.earlier_career
    if drafted is None or not drafted.text.strip() or fitted.printed.earlier_career is not None:
        return ""
    return (
        "The draft wrote an earlier-career line, but no earlier role prints on this resume,\n"
        f'  so it was left off: "{drafted.text.strip()}"'
    )


def overflow_warning(settings: Settings, fitted: draft_stage.Fitted) -> str:
    """Return the warning for a resume estimated past its pages, or "" when it fits."""
    if not fitted.overflow:
        return ""
    return (
        f"The resume is estimated about {fitted.overflow:.0f} line(s) past {settings.pages} "
        "page(s), and nothing left may be dropped\n  without losing a must-have answer or a "
        "role's last bullets. Render it and check the page count."
    )


def _page_holds(
    entry_ids: tuple[str, ...],
    inventory: CvInventory,
    settings: Settings,
    fitted: draft_stage.Fitted,
) -> bool:
    """Return whether the fitted page, trimmed of nothing, has room to print ``entry_ids`` too."""
    # Selection buys more lines than the page holds, so its own room left can outrun the page.
    room = Budget(settings.pages).total_lines - fitted.total_lines
    master = sum(wrapped_lines(inventory.by_id(entry_id).full_text) for entry_id in entry_ids)
    return not fitted.trimmed and master * DRAFTED_SHARE <= room


def _roles_without_bullets(
    plan: select_stage.Plan, inventory: CvInventory, fitted: draft_stage.Fitted
) -> list[str]:
    """Return the recent roles the page prints without a bullet, each told apart from the rest."""
    printed = {role.role_key for role in fitted.printed.roles if role.bullets}
    return [
        _role_name(inventory, role_key) for role_key in plan.recent_roles if role_key not in printed
    ]


def _role_name(inventory: CvInventory, role_key: str) -> str:
    """Return a role's employer, with its title and dates so two at one employer differ."""
    role = inventory.role(role_key)
    if role is None:  # pragma: no cover - plan keys come from the inventory
        return role_key
    detail = ", ".join(part for part in (role.title, role.dates) if part)
    return f"{role.organization} ({detail})" if detail else role.organization


def _warnings(
    verification: verify_stage.Report,
    attempts: int,
    settings: Settings,
    fitted: draft_stage.Fitted,
) -> list[str]:
    """Return the things a person needs to act on."""
    warnings = []
    if overflow := overflow_warning(settings, fitted):
        warnings.append(overflow)
    if not verification.passed:
        warnings.append(
            f"Do not send this as it stands. The checks above failed after {attempts} "
            "attempt(s);\n  the package was written so you can read them."
        )
    elif attempts > 1:
        warnings.append(f"The draft was rewritten {attempts - 1} time(s) in response to checks.")
    if verification.coverage.missing:
        warnings.append(
            f"{len(verification.coverage.missing)} posting phrase(s) are absent. Check the "
            "report\n  before deciding whether the master CV genuinely supports them."
        )
    return warnings
