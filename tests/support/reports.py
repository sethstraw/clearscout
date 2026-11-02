"""Call ``render_report`` with only the collaborators one test names.

It takes eleven required arguments, so a test about one section would otherwise
have to assert through that section's private renderer.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from tailorcv.application.config import Settings
from tailorcv.application.master_cv.cv import CvInventory
from tailorcv.application.tailoring.fit import Reading, read
from tailorcv.application.tailoring.pipeline.report import render_report
from tailorcv.application.tailoring.stages.draft import DraftedLine, ResumeDraft, fit
from tailorcv.application.tailoring.stages.jobspec import JobSpec
from tailorcv.application.tailoring.stages.match import EvidenceMap
from tailorcv.application.tailoring.stages.select import Plan
from tailorcv.application.tailoring.stages.verify import KeywordCoverage, Report
from tailorcv.infrastructure.providers.credentials import Credential, Source
from tailorcv.infrastructure.web_fetch.ingest import JDText
from tailorcv.shared.costs import Ledger

RUN_DATE = date(2025, 1, 1)
POSTING = "Staff Data Platform Engineer\n\nBuild and run the ingestion platform.\n"


def _inventory() -> CvInventory:
    return CvInventory(source=Path("master_cv.md"), content_hash="cv-hash", name="Ada Lorne")


def _spec() -> JobSpec:
    return JobSpec(
        title="Staff Data Platform Engineer",
        company="Globex",
        seniority=None,
        location=None,
        requirements=(),
        keywords=(),
        hard_gates=(),
        origin="test",
    )


def rendered_report(
    *,
    jd: JDText | None = None,
    spec: JobSpec | None = None,
    evidence: EvidenceMap | None = None,
    plan: Plan | None = None,
    draft: ResumeDraft | None = None,
    verification: Report | None = None,
    inventory: CvInventory | None = None,
    settings: Settings | None = None,
    ledger: Ledger | None = None,
    credential: Credential | None = None,
    reading: Reading | None = None,
) -> str:
    """Render a whole report, defaulting every collaborator the caller leaves out."""
    if settings is None:
        settings = Settings()
    if inventory is None:
        inventory = _inventory()
    if verification is None:
        verification = Report(findings=(), coverage=KeywordCoverage(present=(), missing=()))
    if credential is None:
        credential = Credential("test-key", settings.provider, Source.ENVIRONMENT)
    if draft is None:
        draft = ResumeDraft(summary=DraftedLine(text="Ran the ingestion platform."))
    if plan is None:
        plan = Plan()
    return render_report(
        jd=JDText(text=POSTING, origin="fixture", method="fixture") if jd is None else jd,
        spec=_spec() if spec is None else spec,
        evidence=EvidenceMap(matches=()) if evidence is None else evidence,
        plan=plan,
        fitted=fit(draft, inventory, plan, settings, settings.rules),
        verification=verification,
        inventory=inventory,
        settings=settings,
        ledger=Ledger() if ledger is None else ledger,
        credential=credential,
        reading=read(POSTING, inventory, today=RUN_DATE) if reading is None else reading,
        today=RUN_DATE,
    )


def report_section(report: str, heading: str) -> str:
    """Return one ``## heading`` section of a rendered report, or "" when it is absent."""
    marker = f"\n## {heading}\n"
    found = report.find(marker)
    if found == -1:
        return ""
    start = found + 1
    end = report.find("\n## ", start + len(marker))
    return report[start:] if end == -1 else report[start:end]


__all__ = ["rendered_report", "report_section"]
