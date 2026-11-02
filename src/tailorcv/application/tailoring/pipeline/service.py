"""Compose the deterministic intake, screen, draft, review, and report stages."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from tailorcv.application.config import Settings, Stage
from tailorcv.application.discovery import posting_identity as posting_module
from tailorcv.application.master_cv import sources
from tailorcv.application.master_cv.cv import CvInventory, parse_cv
from tailorcv.application.prompts import render as render_prompt
from tailorcv.application.tailoring import approval as approval_store
from tailorcv.application.tailoring import changes, fit
from tailorcv.application.tailoring import runs as runs_store
from tailorcv.application.tailoring.guardrails import Guardrails
from tailorcv.application.tailoring.stages import draft as draft_stage
from tailorcv.application.tailoring.stages import jobspec as jobspec_stage
from tailorcv.application.tailoring.stages import match as match_stage
from tailorcv.application.tailoring.stages import select as select_stage
from tailorcv.application.tailoring.stages import structure as structure_stage
from tailorcv.application.tailoring.stages import verify as verify_stage
from tailorcv.application.tailoring.stages.jobspec import JobSpec, StoredPostingName
from tailorcv.domain.records import PostingReadIntent
from tailorcv.infrastructure.artifacts import rendering
from tailorcv.infrastructure.artifacts.files import write_private
from tailorcv.infrastructure.providers.base import provider_for
from tailorcv.infrastructure.providers.cache import ResponseCache
from tailorcv.infrastructure.providers.client import Client
from tailorcv.infrastructure.providers.credentials import Credential, Prompter, resolve
from tailorcv.infrastructure.providers.plan import Image, RequestPlan, Segment
from tailorcv.infrastructure.web_fetch.company_research import ResearchSource, fingerprint
from tailorcv.infrastructure.web_fetch.ingest import JDText, VisionReader
from tailorcv.infrastructure.web_fetch.ingest.inputs import ingest
from tailorcv.shared.costs import Ledger, ServiceTier
from tailorcv.shared.costs.prices import ledger_for
from tailorcv.shared.errors import ConfigError

from . import importer
from .models import (
    PageSelection,
    RunResult,
    ScreenResult,
    SerializedRunArtifacts,
    StoredMasterCv,
    VisionTranscript,
)
from .progress import _commentary, _origin, _Progress
from .report import named_tool_notes, render_report, write_screen_report
from .reuse import REDRAWN, _caching, _reuse
from .rewrite import (
    Attempt,
    _draft_until_clean,
    _notes,
    _render_cover_letter,
    _require_experience,
    _warnings,
    kept_attempt,
)


@dataclass(frozen=True)
class RunCommand:
    """Everything one tailoring run needs, stated once."""

    source: str
    settings: Settings
    master_cv: Path | None = None
    api_key: str | None = None
    prompter: Prompter | None = None
    want_cover_letter: bool = False
    output_dir: Path | None = None
    cache_enabled: bool | None = None
    trim: int = 0
    force: bool = False
    verbose: bool = False
    want_render: bool = False
    approval: approval_store.Manifest | None = None
    research: tuple[ResearchSource, ...] = ()
    publisher_url: str | None = None
    service_tier: ServiceTier = ServiceTier.STANDARD
    posting_hint: str | None = None
    active_disputes: str | None = None
    materialize: bool = True
    ledger: Ledger | None = None
    raise_if_cancelled: Callable[[], None] | None = None
    intent: PostingReadIntent = PostingReadIntent.CRAWL
    stored_name: StoredPostingName | None = None
    stored_cv: StoredMasterCv | None = None


@dataclass
class _RunStart:
    inventory: CvInventory
    manifest: approval_store.Manifest | None
    guardrails: Guardrails
    started: datetime
    today: date
    ledger: Ledger
    credential: Credential


@dataclass
class _Prepared:
    jd: JDText
    spec: JobSpec
    posting: posting_module.Posting
    reading: fit.Reading
    evidence: match_stage.EvidenceMap
    plan: select_stage.Plan
    # The stored name a letter greets by; the spec's company may be model text.
    stored_name: StoredPostingName | None = None

    @property
    def employer(self) -> str | None:
        """Return the stored employer a letter may greet, if any."""
        return None if self.stored_name is None else self.stored_name.stated_employer


@dataclass
class _Drafted:
    fitted: draft_stage.Fitted
    verification: verify_stage.Report
    attempts: tuple[Attempt, ...]
    review: changes.Review
    decisions: dict[str, str]
    review_notes: list[str]


@dataclass
class _Written:
    materialized: runs_store.MaterializedRun | None
    verification: verify_stage.Report
    serialized: SerializedRunArtifacts
    notes: list[str]
    warnings: list[str]


def run(command: RunCommand) -> RunResult:
    """Run the whole pipeline: job description in, reviewed Markdown package out."""
    _validate_command(command)
    settings = command.settings
    start = _start(
        settings,
        command.master_cv,
        command.api_key,
        command.prompter,
        command.approval,
    )

    if command.ledger is not None:
        start.ledger = command.ledger

    with ResponseCache(enabled=_caching(settings, command.cache_enabled)) as cache:
        client = Client(
            provider=provider_for(
                settings.provider,
                start.credential.key,
                settings.timeout_for(command.service_tier),
            ),
            settings=settings,
            cache=cache,
            ledger=start.ledger,
            watcher=_commentary(command.prompter, command.verbose),
            redraw=REDRAWN if command.force else frozenset(),
            service_tier=command.service_tier,
            raise_if_cancelled=command.raise_if_cancelled,
        )
        steps = _Progress(command.prompter, total=4 + (1 if command.want_cover_letter else 0))
        prepared = _prepare(
            client,
            command.source,
            settings,
            start,
            steps,
            publisher_url=command.publisher_url,
            intent=command.intent,
            stored_name=command.stored_name,
        )
        research = _research_sources(command, prepared)
        root = command.output_dir or settings.output_dir
        identity = runs_store.identity_for(
            prepared.posting,
            start.inventory,
            settings,
            cover_letter=command.want_cover_letter,
            trimmed=command.trim,
            approval="" if start.manifest is None else start.manifest.fingerprint(),
            research_hash=fingerprint(research) if command.want_cover_letter else "",
        )
        finished = (
            None
            if command.force or not command.materialize
            else runs_store.existing_run(root, prepared.spec.slug(), identity)
        )
        if finished is not None:
            reused = _reuse(finished, prepared.spec, start.ledger, command.trim, prepared.reading)
            if command.want_render:
                if reused.materialized is None:
                    raise ConfigError("A reused tailoring run must be materialized.")
                outcome = rendering.render(reused.materialized.document)
                (reused.notes if outcome.ok else reused.warnings).append(outcome.message)
                reused.warnings.extend(outcome.warnings)
            return reused

        earlier = (
            runs_store.previous_run(root, prepared.spec.slug(), prepared.posting.identifier)
            if command.materialize
            else None
        )
        drafted = _draft_and_review(
            client,
            prepared,
            start,
            settings,
            steps,
            identity,
            trim=command.trim,
            posting_hint=command.posting_hint,
            active_disputes=command.active_disputes,
        )
        written = _write_package(
            client=client,
            root=root,
            identity=identity,
            earlier=earlier,
            prepared=prepared,
            drafted=drafted,
            start=start,
            settings=settings,
            steps=steps,
            want_cover_letter=command.want_cover_letter,
            research=research,
            materialize=command.materialize,
            stored_cv=command.stored_cv,
            posting_hint=command.posting_hint,
            active_disputes=command.active_disputes,
        )

    notes, warnings = written.notes, written.warnings
    if command.want_render:
        if written.materialized is None:
            raise ConfigError("Rendering requires a materialized tailoring run.")
        outcome = rendering.render(written.materialized.document)
        (notes if outcome.ok else warnings).append(outcome.message)
        warnings.extend(outcome.warnings)

    return RunResult(
        verification=written.verification,
        spec=prepared.spec,
        ledger=start.ledger,
        attempts=len(drafted.attempts),
        trimmed=command.trim,
        serialized=written.serialized,
        materialized=written.materialized,
        notes=notes,
        warnings=warnings,
        reading=prepared.reading,
    )


def _validate_command(command: RunCommand) -> None:
    if command.want_render and not command.materialize:
        raise ConfigError("Rendering requires a materialized tailoring run.")


def _research_sources(command: RunCommand, prepared: _Prepared) -> tuple[ResearchSource, ...]:
    research = command.research
    if not command.want_cover_letter or any(item.kind == "posting" for item in research):
        return research
    return (
        ResearchSource(
            id="company.posting",
            kind="posting",
            url=prepared.jd.origin,
            text=prepared.jd.text,
            method=prepared.jd.method,
        ),
        *research,
    )


def _start(
    settings: Settings,
    master_cv: Path | None,
    api_key: str | None,
    prompter: Prompter | None,
    approval: approval_store.Manifest | None,
) -> _RunStart:
    inventory = parse_cv(settings.master_cv(master_cv))
    guardrails = settings.rules
    started = datetime.now(UTC)
    today = started.date()
    ledger = ledger_for(settings.prices)
    credential = resolve(settings.provider, api_key, prompter=prompter)
    return _RunStart(inventory, approval, guardrails, started, today, ledger, credential)


def _prepare(
    client: Client,
    source: str,
    settings: Settings,
    start: _RunStart,
    steps: _Progress,
    *,
    publisher_url: str | None = None,
    intent: PostingReadIntent = PostingReadIntent.CRAWL,
    stored_name: StoredPostingName | None = None,
) -> _Prepared:
    steps.begin(f"Reading the posting from {_origin(source)}")
    jd = ingest(source, vision=vision_reader(client), intent=intent)
    if publisher_url:
        jd = jd.model_copy(update={"origin": publisher_url})
    steps.begin("Working out what the posting asks for")
    spec = jobspec_stage.extract(
        client, jd, boundaries=settings.job_search.focus.stated_boundaries
    ).named(stored_name)
    posting = posting_module.identify(jd)
    reading = fit.read(
        jd.text,
        start.inventory,
        vocabulary=settings.vocabulary,
        parameters=settings.job_search,
        today=start.today,
    )
    steps.begin("Finding the evidence in your CV that answers it")
    evidence = match_stage.match(client, start.inventory, spec, start.guardrails)
    plan = select_stage.build_plan(start.inventory, spec, evidence, settings, start.today)
    _require_experience(plan, spec)
    return _Prepared(jd, spec, posting, reading, evidence, plan, stored_name)


def _draft_and_review(
    client: Client,
    prepared: _Prepared,
    start: _RunStart,
    settings: Settings,
    steps: _Progress,
    identity: runs_store.RunIdentity,
    *,
    trim: int,
    posting_hint: str | None = None,
    active_disputes: str | None = None,
) -> _Drafted:
    steps.begin("Writing and checking the resume")
    fitted, verification, attempts = _draft_until_clean(
        client,
        inventory=start.inventory,
        spec=prepared.spec,
        plan=prepared.plan,
        guardrails=start.guardrails,
        settings=settings,
        trim=trim,
        steps=steps,
        evidence=prepared.evidence,
        posting_hint=posting_hint,
        active_disputes=active_disputes,
    )
    review = changes.review(
        identity=identity,
        draft=fitted.draft,
        plan=prepared.plan,
        inventory=start.inventory,
        spec=prepared.spec,
        evidence=prepared.evidence,
        settings=settings,
        guardrails=start.guardrails,
        excluded=fitted.excluded,
    )
    decisions: dict[str, str] = {}
    review_notes: list[str] = []
    if start.manifest is not None:
        steps.within("applying your review")
        applied, document = approval_store.applied(
            start.manifest,
            review,
            fitted.draft,
            prepared.plan,
            inventory=start.inventory,
            spec=prepared.spec,
            settings=settings,
            guardrails=start.guardrails,
            excluded=fitted.excluded,
            verification=verification,
        )
        fitted = fitted.redrawn(document)
        verification = applied.verification or verification
        decisions, review_notes = applied.decisions, applied.notes()
    return _Drafted(fitted, verification, attempts, review, decisions, review_notes)


def _write_package(
    *,
    client: Client,
    root: Path,
    identity: runs_store.RunIdentity,
    earlier: Path | None,
    prepared: _Prepared,
    drafted: _Drafted,
    start: _RunStart,
    settings: Settings,
    steps: _Progress,
    want_cover_letter: bool,
    research: tuple[ResearchSource, ...],
    materialize: bool,
    stored_cv: StoredMasterCv | None = None,
    posting_hint: str | None = None,
    active_disputes: str | None = None,
) -> _Written:
    verification = drafted.verification
    document_name = f"{prepared.spec.slug()}.md"
    report_name = "Tailoring_Report.md"
    if want_cover_letter:
        steps.begin("Writing and checking the cover letter")
    written_letter = _render_cover_letter(
        client,
        start.inventory,
        prepared.spec,
        # The letter may cite any drafted entry the resume fit did not take off the page.
        drafted.fitted.printed_plan,
        start.guardrails,
        settings=settings,
        steps=steps,
        want=want_cover_letter,
        research=research,
        posting_hint=posting_hint,
        active_disputes=active_disputes,
        employer=prepared.employer,
    )
    letter = None if written_letter is None else written_letter.letter
    rendered_review = changes.render(
        drafted.review,
        drafted.decisions,
        findings=drafted.verification.findings,
        letter=letter,
    )
    letter_name = "Cover_Letter.md" if written_letter is not None else None
    attempts = {"resume": [attempt.payload() for attempt in drafted.attempts]}
    kept = {"resume": kept_attempt(drafted.attempts)}
    critic_draws = {"resume": [draw.scores() for draw in verification.critic_draws]}
    if written_letter is not None:
        verification = verify_stage.Report(
            findings=(*verification.findings, *written_letter.verification.findings),
            coverage=verification.coverage,
            critic=verification.critic,
            critic_draws=verification.critic_draws,
        )
        attempts["cover_letter"] = [attempt.payload() for attempt in written_letter.attempts]
        kept["cover_letter"] = kept_attempt(written_letter.attempts)
        critic_draws["cover_letter"] = [
            draw.scores() for draw in written_letter.verification.critic_draws
        ]
    notes = (
        _notes(settings, prepared.plan, start.inventory, drafted.fitted)
        + drafted.review_notes
        + named_tool_notes(prepared.spec, prepared.evidence, start.inventory)
        + match_stage.example_list_notes(prepared.spec, prepared.evidence)
    )
    warnings = _warnings(verification, len(drafted.attempts), settings, drafted.fitted)
    report_text = render_report(
        jd=prepared.jd,
        spec=prepared.spec,
        evidence=prepared.evidence,
        plan=prepared.plan,
        fitted=drafted.fitted,
        verification=verification,
        inventory=start.inventory,
        settings=settings,
        ledger=start.ledger,
        credential=start.credential,
        reading=prepared.reading,
        today=start.today,
        previous=earlier,
        letter=written_letter,
        stored_cv=stored_cv,
        stored_name=prepared.stored_name,
    )
    artifact_bytes = {
        document_name: drafted.fitted.markdown.encode("utf-8"),
        report_name: report_text.encode("utf-8"),
        changes.CHANGES_JSON: rendered_review.json_text.encode("utf-8"),
        changes.CHANGES_MD: rendered_review.markdown_text.encode("utf-8"),
    }
    if letter_name is not None and written_letter is not None:
        artifact_bytes[letter_name] = written_letter.text.encode("utf-8")
    manifest = runs_store.Manifest(
        identity=identity,
        posting_url=prepared.jd.origin,
        posting_origin=prepared.posting.origin.value,
        retrieved=start.today.isoformat(),
        jd_hash=posting_module.content_hash(prepared.jd.text),
        started=start.started.isoformat(timespec="seconds"),
        outcome="passed" if verification.passed else "checks failed",
        checks=len(verification.findings),
        # The tailoring review shows these findings to a person, so critic ids read as text.
        findings=[
            {
                "check": item.check,
                "outcome": item.outcome.value,
                "detail": item.detail,
                "advisories": list(item.advisories),
            }
            for item in verification.for_reader(
                verify_stage.id_texts(
                    prepared.spec,
                    start.inventory,
                    written_letter.research if written_letter is not None else (),
                )
            ).findings
        ],
        coverage=verification.coverage.ratio * 100,
        served_models=start.ledger.served_models(),
        resolved_efforts=start.ledger.resolved_efforts(),
        attempts=attempts,
        kept=kept,
        critic_draws=critic_draws,
        cache_hits=_cache_hits(start.ledger),
        notes=[*warnings, *notes],
        selection=PageSelection.of(
            plan=prepared.plan,
            inventory=start.inventory,
            spec=prepared.spec,
            evidence=prepared.evidence,
            fitted=drafted.fitted,
            settings=settings,
        ).model_dump(mode="json"),
    )
    manifest_json, manifest_facts = manifest.serialize(artifact_bytes)
    serialized = SerializedRunArtifacts(
        document=artifact_bytes[document_name],
        report=artifact_bytes[report_name],
        manifest_json=manifest_json,
        manifest=manifest_facts,
        cover_letter=artifact_bytes.get(letter_name) if letter_name is not None else None,
        review_json=artifact_bytes[changes.CHANGES_JSON],
        review_markdown=artifact_bytes[changes.CHANGES_MD],
        review=rendered_review.payload,
    )
    materialized = None
    if materialize:
        with runs_store.staged(root, prepared.spec.slug(), identity) as staging:
            for name, content in artifact_bytes.items():
                staging.write(name, content.decode("utf-8"))
            staging.write(runs_store.MANIFEST, manifest_json.decode("utf-8"))
        directory = staging.destination
        materialized = runs_store.MaterializedRun(
            directory=directory,
            document=directory / document_name,
            report=directory / report_name,
            cover_letter=directory / letter_name if letter_name is not None else None,
            review=directory / changes.CHANGES_JSON,
        )
    return _Written(
        materialized=materialized,
        verification=verification,
        serialized=serialized,
        notes=notes,
        warnings=warnings,
    )


def _cache_hits(ledger: Ledger) -> list[dict[str, str]]:
    """Return what the cache row says about each replayed answer.

    A cache row stores no provider response id, so the key is what links a hit to its row.
    """
    return [
        {
            "stage": entry.stage,
            "cache_key": entry.cache_key,
            "code_revision": entry.revision,
            "package_version": entry.package_version,
            "stored_at": entry.cached_at,
        }
        for entry in ledger.entries
        if entry.cached
    ]


def import_cv(
    path: Path,
    settings: Settings,
    *,
    destination: Path,
    api_key: str | None = None,
    prompter: Prompter | None = None,
    cache_enabled: bool | None = None,
    force: bool = False,
) -> importer.ImportResult:
    """Convert an existing CV into the canonical form with provenance checks."""
    if destination.exists() and not force:
        raise ConfigError(
            f"{destination} already exists.\n"
            "  Pass --force to replace it, or --out to write somewhere else."
        )

    ledger = ledger_for(settings.prices)
    credential = resolve(settings.provider, api_key, prompter=prompter)

    with ResponseCache(enabled=_caching(settings, cache_enabled)) as cache:
        client = Client(
            provider=provider_for(settings.provider, credential.key, settings.request_timeout),
            settings=settings,
            cache=cache,
            ledger=ledger,
        )
        steps = _Progress(prompter, total=3)
        steps.begin(f"Reading {path.name}")
        source = sources.read(path, vision=vision_reader(client))
        steps.begin(f"Working out the structure of {len(source.lines)} lines")
        extracted = structure_stage.structure(client, source)
        steps.begin("Checking nothing was lost or invented")
        built = importer.assemble(extracted, source, settings.sections)
        findings = importer.verify(source, extracted, built)

    result = importer.ImportResult(
        markdown=built.markdown,
        source=source,
        extracted=extracted,
        dropped=tuple(built.dropped),
        findings=findings,
        ledger=ledger,
    )
    result.report = importer.write_report(destination.parent, result)
    result.path = write_private(destination, built.markdown)
    return result


def screen(
    source: str,
    settings: Settings,
    *,
    master_cv: Path | None = None,
    api_key: str | None = None,
    prompter: Prompter | None = None,
    cache_enabled: bool | None = None,
    out: Path | None = None,
    verbose: bool = False,
    service_tier: ServiceTier = ServiceTier.STANDARD,
    ledger: Ledger | None = None,
    raise_if_cancelled: Callable[[], None] | None = None,
    intent: PostingReadIntent = PostingReadIntent.CRAWL,
    stored_name: StoredPostingName | None = None,
    listing_location: str = "",
    fresh: bool = False,
) -> ScreenResult:
    """Answer whether a posting is worth applying to, and stop there.

    ``fresh`` asks every stage again and writes the new answers to the cache.
    """
    inventory = parse_cv(settings.master_cv(master_cv))
    guardrails = settings.rules
    ledger = ledger if ledger is not None else ledger_for(settings.prices)
    credential = resolve(settings.provider, api_key, prompter=prompter)

    with ResponseCache(enabled=_caching(settings, cache_enabled)) as cache:
        client = Client(
            provider=provider_for(
                settings.provider, credential.key, settings.timeout_for(service_tier)
            ),
            settings=settings,
            cache=cache,
            ledger=ledger,
            watcher=_commentary(prompter, verbose),
            redraw=frozenset(Stage) if fresh else frozenset(),
            service_tier=service_tier,
            raise_if_cancelled=raise_if_cancelled,
        )
        steps = _Progress(prompter, total=3)
        steps.begin(f"Reading the posting from {_origin(source)}")
        jd = ingest(source, vision=vision_reader(client), intent=intent)
        steps.begin("Working out what the posting asks for")
        spec = jobspec_stage.extract(
            client, jd, boundaries=settings.job_search.focus.stated_boundaries
        ).named(stored_name)
        today = datetime.now(UTC).date()
        reading = fit.read(
            jd.text,
            inventory,
            vocabulary=settings.vocabulary,
            parameters=settings.job_search,
            today=today,
            listing_location=listing_location,
        )
        steps.begin("Finding the evidence in your CV that answers it")
        evidence = match_stage.match(client, inventory, spec, guardrails)

        written = None
        if out is not None:
            posting = posting_module.identify(jd)
            directory = runs_store.screening_directory(out, spec.slug(), posting.identifier)
            written = write_screen_report(
                directory,
                jd=jd,
                spec=spec,
                evidence=evidence,
                inventory=inventory,
                settings=settings,
                ledger=ledger,
                credential=credential,
                reading=reading,
                today=today,
            )

    return ScreenResult(
        spec=spec,
        evidence=evidence,
        inventory=inventory,
        ledger=ledger,
        jd=jd,
        reading=reading,
        report=written,
    )


def vision_reader(client: Client) -> VisionReader:
    """Return a reader that transcribes images, keeping ingest provider-free."""

    def read(images: list[bytes], media_type: str) -> str:
        policy = client.settings.policy_for(Stage.INGEST)
        plan = RequestPlan(
            stage=Stage.INGEST,
            instructions=Segment(render_prompt("vision_system"), "vision_system"),
            task=Segment(render_prompt("vision_user"), "vision_user"),
            images=tuple(Image(data=data, media_type=media_type) for data in images),
            max_tokens=policy.max_tokens,
        )
        return client.complete(plan, VisionTranscript).text

    return read
