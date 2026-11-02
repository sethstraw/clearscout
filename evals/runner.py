"""Run every evaluation case through the real pipeline and report what moved.

    python -m evals                     # run every case and compare to baselines
    python -m evals --case poor_fit     # run one
    python -m evals --record-baselines  # rewrite the baselines, comparing nothing

The stages are the shipped ones with the provider scripted from the case file,
so what is measured is whether the program still enforces its guarantees when
the model misbehaves. The process runs with sockets disabled. One pass per case.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, NoReturn, TypeVar

from pydantic import BaseModel
from tests.support.schema import declares, literals

from evals import baseline
from evals.case import Case, CaseError, Mode, load_all, resolve_evidence
from evals.scorers import CaseRun, Score, cross_case, score
from evals.support.relevance import relevance_batch
from tailorcv.application import prompts
from tailorcv.application.config import DEFAULT_PROVIDER, ModelChoice, Settings, Stage
from tailorcv.application.discovery.assisted_content_resolution import (
    PostingRecoveryOutcome,
    PostingRecoveryResponse,
    recovery_decision,
    validated_recovery_candidates,
)
from tailorcv.application.discovery.assisted_content_resolution import (
    request_plan as posting_recovery_plan,
)
from tailorcv.application.discovery.assisted_content_resolution import (
    response_schema as posting_recovery_schema,
)
from tailorcv.application.discovery.content_resolution import PostingResolutionTarget
from tailorcv.application.discovery.derived_vocabulary_rules import searchable_titles
from tailorcv.application.discovery.guided_campaign import (
    CampaignAttemptPlan,
    CampaignBounds,
    CampaignIdentities,
    CampaignManifest,
    compile_campaign,
)
from tailorcv.application.discovery.guided_campaign_request import (
    attempt_request_plan,
    campaign_response_schema,
)
from tailorcv.application.discovery.guided_campaign_runner import (
    captured_url_rows,
    matched_queries,
)
from tailorcv.application.discovery.proposals import CarriedRejections
from tailorcv.application.discovery.relevance_review import (
    response_schema as relevance_review_schema,
)
from tailorcv.application.discovery.relevance_review import review_plan
from tailorcv.application.discovery.search_vocabulary import SearchVocabularyService
from tailorcv.application.discovery.search_vocabulary import (
    response_schema as search_vocabulary_schema,
)
from tailorcv.application.failures import ValidationError as AppValidationError
from tailorcv.application.job_search.models import RelevanceReviewBatchResponse
from tailorcv.application.master_cv import sources as cv_sources
from tailorcv.application.master_cv.cv import CvInventory, parse_cv
from tailorcv.application.tailoring import approval, changes, elicit, fit
from tailorcv.application.tailoring.pipeline import importer, rewrite
from tailorcv.application.tailoring.pipeline.models import VisionTranscript
from tailorcv.application.tailoring.pipeline.service import vision_reader
from tailorcv.application.tailoring.runs import RunIdentity
from tailorcv.application.tailoring.stages import draft as draft_stage
from tailorcv.application.tailoring.stages import jobspec as jobspec_stage
from tailorcv.application.tailoring.stages import match as match_stage
from tailorcv.application.tailoring.stages import select as select_stage
from tailorcv.application.tailoring.stages import structure as structure_stage
from tailorcv.application.tailoring.stages import verify as verify_stage
from tailorcv.application.tailoring.stages.context import citable_entries, record_block
from tailorcv.application.tailoring.stages.draft import CoverLetter, ResumeDraft
from tailorcv.application.tailoring.stages.jobspec import JobSpecExtraction
from tailorcv.application.tailoring.stages.match import response_schema as match_schema
from tailorcv.application.tailoring.stages.structure import response_schema as structure_schema
from tailorcv.application.tailoring.stages.verify import CriticVerdict
from tailorcv.domain import PostingContentAccessStatus
from tailorcv.domain.search_parameters import (
    MAX_ARCHETYPES,
    MAX_CLUSTERS,
    MAX_TITLES,
    JobSearchParameters,
    Lane,
    Seniority,
    TargetTitle,
)
from tailorcv.infrastructure.providers.base import (
    BackgroundDispatch,
    BackgroundPoll,
    CallRecord,
    Search,
    SearchSource,
    json_schema,
    parse_or_raise,
)
from tailorcv.infrastructure.providers.cache import ResponseCache, cache_key
from tailorcv.infrastructure.providers.client import Client
from tailorcv.infrastructure.providers.plan import RequestPlan
from tailorcv.infrastructure.web_fetch.ingest import JDText, require_enough_text
from tailorcv.infrastructure.web_fetch.ingest.image import from_image
from tailorcv.shared.costs import Ledger, Usage
from tailorcv.shared.errors import ApprovalError, DiscoveryError, ProviderError, TailorcvError

# The identity a scripted posting-recovery answer must echo, and the only
# posting identifier the harness ever asks recovery about.
POSTING_RECOVERY_IDENTITY_ID = "case-posting"

ModelT = TypeVar("ModelT", bound=BaseModel)

# Recency scoring reads the calendar, so without a fixed date a baseline
# would fail because a month passed.
TODAY = date(2026, 6, 1)

# Matched on a substring, because the matching, drafting, and letter schemas
# are built at runtime and carry a "Constrained" prefix.
STAGES = (
    ("JobSpecExtraction", "jobspec"),
    ("EvidenceMap", "match"),
    ("ResumeDraft", "draft"),
    ("CriticVerdict", "critic"),
    ("CoverLetter", "cover"),
    ("RelevanceReview", "relevance_review"),
    ("ImportedCv", "structure"),
    ("VisionTranscript", "vision"),
    ("SearchVocabulary", "search_vocabulary"),
    ("PostingRecovery", "posting_recovery"),
    ("CampaignAnswer", "guided_campaign"),
)

# Read back out of the schema that was sent, so a result can state what the
# model was actually allowed to say.
VOCABULARY_FIELD = {"match": "entry_ids", "draft": "source_ids", "cover": "source_ids"}

# What the recorded stamp is hashed over. `check_stages_are_fingerprinted`
# refuses a run reaching a stage missing from it.
EVALUATED_PROMPTS = {
    "jobspec": "jobspec",
    "match": "match",
    "draft": "draft",
    "critic": "critic",
    "relevance_review": "relevance_review",
    "cover": "cover_letter",
    "structure": "structure",
    "vision": "vision",
    "search_vocabulary": "search_vocabulary",
    "posting_recovery": "posting_recovery",
    "guided_campaign": "guided_campaign",
}

# A stage a case can script an answer for. Declaring a family evaluated without
# one claims coverage no case can reach.
SCRIPTABLE_STAGES = frozenset(
    {
        "jobspec",
        "match",
        "draft",
        "critic",
        "relevance_review",
        "cover",
        "structure",
        "vision",
        "search_vocabulary",
        "posting_recovery",
        "guided_campaign",
    }
)

# The other half of the same ledger: a family here is one no case reaches. A
# family in neither map fails `check_prompt_coverage_is_declared`. Empty while
# every shipped family has a scripted case.
UNEVALUATED_PROMPTS: dict[str, str] = {}


class EvalError(Exception):
    """The harness cannot run, or was asked to do something it refuses to do."""


class ScriptedProvider:
    """A provider that answers from the case file, and cannot answer anything else.

    Implements the same protocol the real adapters do, because it is the seam a
    live run replaces. Identifiers are resolved from the parsed CV before the run
    starts, and the response goes through the same ``parse_or_raise`` the real
    adapters use, so a rejection arrives as it would in production.
    """

    name = DEFAULT_PROVIDER

    def __init__(self, case: Case, inventory: CvInventory, alias_ids: dict[str, str]) -> None:
        """Hold the case, the parsed CV, and the resolved evidence identifiers."""
        self.case = case
        self.inventory = inventory
        self.alias_ids = alias_ids
        self.calls: list[str] = []
        self.vocabularies: dict[str, tuple[str, ...]] = {}
        self.skill_labels = True
        # Every request and the key the client would write it under. Read by the
        # batch scorers and by nothing else.
        self.prompts: list[str] = []
        self.cache_keys: list[str] = []
        # How many images each stage's request carried, counted on the way in.
        self.images: dict[str, int] = {}
        # Background responses staged by a scripted dispatch, keyed by response id.
        self._background_polls: dict[str, BackgroundPoll] = {}

    def complete(
        self,
        plan: RequestPlan,
        schema: type[ModelT],
        choice: ModelChoice,
    ) -> tuple[ModelT, CallRecord]:
        """Return the case's scripted answer for whichever stage this schema is."""
        stage = next((name for key, name in STAGES if key in schema.__name__), "")
        if not stage:
            raise EvalError(f"{self.case.name}: no scripted response for {schema.__name__}")
        self.calls.append(stage)
        self.images[stage] = self.images.get(stage, 0) + len(plan.images)
        self.prompts.append(f"{plan.instructions.text}\n{plan.task.text}\n{plan.cached_prefix}")
        self.cache_keys.append(
            cache_key(
                provider=self.name.value,
                stage=stage,
                max_tokens=plan.max_tokens,
                model=f"{choice.model}:{choice.effort.value if choice.effort else 'default'}",
                system=plan.instructions.text,
                user=plan.task.text,
                schema=json_schema(schema),
                extra=plan.cached_prefix,
                payload_digest=plan.payload_digest(),
            )
        )
        if field := VOCABULARY_FIELD.get(stage):
            self.vocabularies[stage] = tuple(literals(schema, field))
        self.skill_labels = declares(schema, "label")
        payload = self._payload(stage)
        record = CallRecord(usage=Usage(), model=choice.model, response_id="scripted")
        return parse_or_raise(schema, payload, "scripted provider"), record

    def complete_with_web_search(
        self,
        plan: RequestPlan,
        schema: type[ModelT],
        choice: ModelChoice,
    ) -> tuple[ModelT, CallRecord]:
        """Answer through the same scripted dispatch, with the case's hosted search attached."""
        value, record = self.complete(plan, schema, choice)
        searched = self._search_for(self.calls[-1])
        if searched is None:
            return value, record
        return value, replace(record, searches=(searched,), tool_version="scripted_web_search")

    def dispatch_background(
        self,
        plan: RequestPlan,
        schema: type[ModelT],
        choice: ModelChoice,
        *,
        metadata: dict[str, str],
    ) -> BackgroundDispatch:
        """Answer through the scripted hosted-search dispatch, and hold it for a later poll."""
        del metadata
        value, record = self.complete_with_web_search(plan, schema, choice)
        response_id = f"scripted-background-{len(self._background_polls)}"
        self._background_polls[response_id] = BackgroundPoll(
            status="complete", value=value, record=record
        )
        return BackgroundDispatch(response_id=response_id, request_envelope=None)

    def poll_background(
        self, response_id: str, schema: type[ModelT], ceiling: int
    ) -> BackgroundPoll:
        """Return the scripted poll staged for this response id, already complete."""
        del schema, ceiling
        return self._background_polls[response_id]

    def cancel_background(self, response_id: str) -> None:
        """Do nothing: a scripted background response has already completed."""

    def _search_for(self, stage: str) -> Search | None:
        """Return the hosted search a case scripted for this stage, if any."""
        scripted = {
            "posting_recovery": self.case.posting_recovery,
            "guided_campaign": self.case.guided_campaign,
        }.get(stage)
        if scripted is None:
            return None
        return Search(
            queries=scripted.search.queries,
            sources=tuple(
                SearchSource(url=item.url, title=item.title) for item in scripted.search.sources
            ),
        )

    def check_credentials(self) -> None:
        """Do nothing: a scripted provider has no credentials and must never need any."""

    def available_models(self) -> frozenset[str] | None:
        """Return nothing, which is what a provider that was not asked would say."""
        return None

    def _payload(self, stage: str) -> dict[str, Any]:
        """Build the raw response body for one stage from the case file."""
        builders = {
            "jobspec": self._jobspec,
            "match": self._match,
            "draft": self._draft,
            "critic": self._critic,
            "relevance_review": self._relevance_review,
            "cover": self._cover_letter,
            "structure": self._structure,
            "vision": self._vision,
            "search_vocabulary": self._search_vocabulary,
            "posting_recovery": self._posting_recovery_answer,
            "guided_campaign": self._guided_campaign_answer,
        }
        build = builders.get(stage)
        if build is None:
            raise EvalError(f"{self.case.name}: case scripts no response for the {stage} stage")
        return build()

    def _jobspec(self) -> dict[str, Any]:
        scripted = self.case.responses.jobspec
        return {
            "title": scripted.title,
            "company": scripted.company,
            "seniority": scripted.seniority,
            "location": scripted.location,
            "requirements": [
                {
                    "text": item.text,
                    "importance": item.importance.value,
                    "dimension": item.dimension.value,
                }
                for item in scripted.requirements
            ],
            "keywords": list(scripted.keywords),
            "hard_gates": [
                {"kind": gate.kind, "text": gate.text, "blocking": gate.blocking}
                for gate in scripted.hard_gates
            ],
        }

    def _match(self) -> dict[str, Any]:
        # Identifiers are assigned in extraction order, so a case names a requirement
        # by its text and the index of that text is the identifier.
        wanted = [item.text for item in self.case.responses.jobspec.requirements]
        matches = []
        for item in self.case.responses.match:
            if item.requirement not in wanted:
                raise EvalError(
                    f"{self.case.name}: scripted match names a requirement the scripted "
                    f"extraction does not state: {item.requirement!r}"
                )
            matches.append(
                {
                    "requirement_id": f"r{wanted.index(item.requirement) + 1:02d}",
                    "entry_ids": [self._entry(alias) for alias in item.evidence],
                    "classification": item.classification.value,
                    "strength": item.strength.value,
                    "note": item.note,
                }
            )
        return {"matches": matches}

    def _draft(self) -> dict[str, Any]:
        scripted = self.case.responses.draft
        if scripted is None:
            raise EvalError(f"{self.case.name}: reached drafting with no scripted draft")
        skills = [
            {
                **({"label": skill.label} if self.skill_labels else {}),
                "text": skill.text,
                "source_ids": [self._entry(alias) for alias in skill.evidence],
            }
            for skill in scripted.skills
        ]
        earlier = scripted.earlier_career
        return {
            "summary": self._line(scripted.summary),
            # A draft schema over a CV with no skill entries has no skills field at all,
            # and one over a plan with no earlier role has no earlier_career field.
            **({"skills": skills} if skills else {}),
            **({} if earlier is None else {"earlier_career": self._line(earlier)}),
            "roles": [
                {
                    "role_key": self._role(role.role),
                    "context": None if role.context is None else self._line(role.context),
                    "bullets": [self._line(bullet) for bullet in role.bullets],
                }
                for role in scripted.roles
            ],
        }

    def _critic(self) -> dict[str, Any]:
        scripted = self.case.responses.critic
        if scripted is None:
            raise EvalError(f"{self.case.name}: reached the critic with no scripted verdict")
        return {
            "alignment": scripted.alignment,
            "preservation": scripted.preservation,
            "specificity": scripted.specificity,
            "readability": scripted.readability,
            "ats_clarity": scripted.ats_clarity,
            "human_voice": scripted.human_voice,
            "unsupported_claims": [
                {"claim": doubt.claim, "boundary": doubt.boundary}
                for doubt in scripted.unsupported_claims
            ],
            "style_flags": list(scripted.style_flags),
            "weaknesses": list(scripted.weaknesses),
            "guidance": scripted.guidance,
        }

    def _relevance_review(self) -> dict[str, Any]:
        scripted = self.case.relevance_review
        if scripted is None:
            raise EvalError(f"{self.case.name}: reached relevance review with no scripted batch")
        return {
            "recommendations": [
                {
                    "pending_discovery_id": item.posting,
                    "recommendation": item.recommendation,
                    "summary": item.summary,
                    "core_work_alignment": item.core_work_alignment,
                    "core_qualification_alignment": item.core_qualification_alignment,
                    "hard_condition": item.hard_condition,
                    "decisive_basis": item.decisive_basis,
                    "posting_evidence_span_ids": [
                        f"{item.span_owner or item.posting}:span-{index:04d}"
                        for index in item.spans
                    ],
                    "master_cv_evidence_ids": [self._entry(alias) for alias in item.evidence],
                    "unresolved_questions": list(item.unresolved_questions),
                }
                for item in scripted.results
            ]
        }

    def _cover_letter(self) -> dict[str, Any]:
        scripted = self.case.responses.cover_letter
        if scripted is None:
            raise EvalError(f"{self.case.name}: reached the cover letter with no scripted answer")
        return {"paragraphs": [self._line(paragraph) for paragraph in scripted.paragraphs]}

    def _structure(self) -> dict[str, Any]:
        scripted = self.case.structure
        if scripted is None:
            raise EvalError(f"{self.case.name}: reached CV structuring with no scripted extract")

        def sourced(field: Any) -> dict[str, Any]:
            return {"text": field.text, "lines": list(field.lines)}

        def role(item: Any) -> dict[str, Any]:
            return {
                "organisation": sourced(item.organisation),
                "title": None if item.title is None else sourced(item.title),
                "title_of_record": (
                    None if item.title_of_record is None else sourced(item.title_of_record)
                ),
                "dates": None if item.dates is None else sourced(item.dates),
                "location": None if item.location is None else sourced(item.location),
                "context": [sourced(piece) for piece in item.context],
                "bullets": [sourced(piece) for piece in item.bullets],
            }

        return {
            "name": sourced(scripted.name),
            "contact": [sourced(piece) for piece in scripted.contact],
            "summary": [sourced(piece) for piece in scripted.summary],
            "skills": [sourced(piece) for piece in scripted.skills],
            "roles": [role(item) for item in scripted.roles],
            "education": [sourced(piece) for piece in scripted.education],
            "publications": [sourced(piece) for piece in scripted.publications],
            "dropped": [{"line": item.line, "reason": item.reason} for item in scripted.dropped],
        }

    def _vision(self) -> dict[str, Any]:
        scripted = self.case.vision
        if scripted is None:
            raise EvalError(f"{self.case.name}: reached vision transcription with no scripted text")
        return {"text": scripted.transcript}

    def _search_vocabulary(self) -> dict[str, Any]:
        scripted = self.case.search_vocabulary
        if scripted is None:
            raise EvalError(
                f"{self.case.name}: reached search vocabulary with no scripted proposal"
            )
        return {
            "titles": [
                {
                    "title": item.title,
                    "reason": item.reason,
                    "evidence_ids": [self._entry(alias) for alias in item.evidence],
                    "lane": item.lane or None,
                }
                for item in scripted.titles
            ],
            "archetypes": [],
            "capabilities": [],
            "domains": [],
        }

    def _posting_recovery_answer(self) -> dict[str, Any]:
        scripted = self.case.posting_recovery
        if scripted is None:
            raise EvalError(f"{self.case.name}: reached posting recovery with no scripted answer")
        return {
            "identity_id": POSTING_RECOVERY_IDENTITY_ID,
            "candidate_urls": list(scripted.candidate_urls),
            "supporting_urls": [],
            "observations": [
                {
                    "url": item.url,
                    "authority": item.authority,
                    "completeness": item.completeness,
                    "identity": item.identity,
                    "posting_status": item.posting_status,
                    "note": item.note,
                }
                for item in scripted.observations
            ],
        }

    def _guided_campaign_answer(self) -> dict[str, Any]:
        scripted = self.case.guided_campaign
        if scripted is None:
            raise EvalError(
                f"{self.case.name}: reached the guided campaign with no scripted answer"
            )
        return {
            "postings": [
                {"url": pick.url, "branch_keys": [], "why": pick.why}
                for pick in scripted.picks
                if not pick.board
            ],
            "boards": [
                {"url": pick.url, "branch_keys": [], "why": pick.why}
                for pick in scripted.picks
                if pick.board
            ],
        }

    def _line(self, line: Any) -> dict[str, Any]:
        return {"text": line.text, "source_ids": [self._entry(a) for a in line.evidence]}

    def _entry(self, alias: str) -> str:
        if alias not in self.alias_ids:
            raise EvalError(f"{self.case.name}: no [evidence] alias named {alias!r}")
        return self.alias_ids[alias]

    def _role(self, fragment: str) -> str:
        keys = [role.key for role in self.inventory.roles if fragment in role.organization]
        if len(keys) != 1:
            raise EvalError(
                f"{self.case.name}: {fragment!r} names {len(keys)} roles in {self.case.cv}"
            )
        return keys[0]


def resolve_aliases(case: Case, inventory: CvInventory) -> dict[str, str]:
    """Turn a case's readable evidence aliases into entry identifiers."""
    return resolve_evidence(case.evidence, inventory, f"{case.name} in {case.cv}")


def build_provider(case: Case, inventory: CvInventory) -> ScriptedProvider:
    """Return the scripted provider a case runs against, the seam a live run would replace."""
    return ScriptedProvider(case, inventory, resolve_aliases(case, inventory))


def forbid_network() -> None:
    """Make any socket in this process raise, before a single case runs.

    An evaluation has no reason to open a socket, so the ability is removed
    rather than promised: a change that reached a provider, a board, or a URL by
    accident raises instead of billing.
    """

    def refuse(*_: object, **__: object) -> NoReturn:
        raise EvalError(
            "The evaluation harness tried to open a network connection.\n"
            "  Scripted evaluations are offline by construction. Something now\n"
            "  reaches a provider, a job board, or a URL, and it must not."
        )

    socket.socket = refuse  # type: ignore[assignment,misc]
    socket.create_connection = refuse
    socket.getaddrinfo = refuse


def execute(case: Case) -> CaseRun:
    """Run one case through the shipped stages and return what it produced.

    Stages are composed here rather than through ``pipeline.run``, which writes
    a directory and returns a summary. An evaluation needs the objects in
    between, and reaching them otherwise means substituting something inside the
    pipeline.
    """
    inventory = parse_cv(case.cv_path)
    if case.asserted:
        inventory = inventory.model_copy(
            update={"contact_lines": (*inventory.contact_lines, *case.asserted)}
        )
    settings = Settings(cv=case.cv_path, pages=case.pages, rules=case.rules, cache=False)
    ledger = Ledger()
    provider = build_provider(case, inventory)
    run = CaseRun(
        case=case,
        inventory=inventory,
        ledger=ledger,
        alias_ids=provider.alias_ids,
        critic_trials=settings.critic_trials,
    )
    started = time.monotonic()

    with ResponseCache(enabled=False) as cache:
        client = Client(
            provider=provider,
            settings=settings,
            cache=cache,
            ledger=ledger,
        )
        try:
            _stages(run, client, case, settings, inventory)
        except ProviderError as error:
            # The closed vocabulary doing its job, so the stage that refused is recorded
            # as a result rather than as a crash.
            run.rejected_stage = provider.calls[-1] if provider.calls else "ingest"
            run.failure = str(error).splitlines()[0]
        except TailorcvError as error:
            # Any other refusal: an unusable posting, an unparseable CV, a budget it
            # could not meet. Recorded, not attributed to the schema.
            run.failure = str(error).splitlines()[0]

    run.calls = tuple(provider.calls)
    run.vocabularies = dict(provider.vocabularies)
    run.prompts = tuple(provider.prompts)
    run.cache_keys = tuple(provider.cache_keys)
    run.vision_image_count = provider.images.get("vision", 0)
    _discovery(run, case, inventory)
    run.seconds = time.monotonic() - started
    return run


def _discovery(run: CaseRun, case: Case, inventory: CvInventory) -> None:
    """Drive a scripted person through discovery, and record what it refused.

    Outside the client block on purpose: discovery reaches no provider, so this
    measures deterministic Python against a scripted person. The master CV is
    never written, because an evaluation that edited a fixture would change what
    every later case measures.
    """
    scripted = case.discovery
    if scripted is None:
        return
    line = elicit.Line(
        subject=scripted.about,
        gap_ids=(scripted.gap,) if scripted.gap else (),
        postings=(case.name,),
    )
    for source in scripted.forbidden:
        run.discovery_forbidden_tried += 1
        try:
            line.with_reply(
                elicit.Reply(
                    text=scripted.claim or scripted.about,
                    source=elicit.Source(source),
                )
            )
        except DiscoveryError as error:
            run.discovery_refusals += (str(error).splitlines()[0],)
            run.discovery_forbidden_refused += 1
    for text in scripted.replies:
        question = line.next_question()
        if question is None:
            break
        line = line.with_reply(elicit.Reply(text=text))
    line = line.finished()
    run.discovery_line = line
    # Nothing is written here, so the gap is still a gap.
    run.discovery_unclosed = elicit.unclosed([line])
    if not scripted.claim:
        run.discovery_outcome = "confirmed" if line.answered else "unanswered"
        return
    try:
        run.discovery_pending = elicit.propose(
            line,
            text=scripted.claim,
            scope=scripted.scope,
            placement=elicit.Placement(role_key=_role_key(case, inventory, scripted.placement)),
            inventory=inventory,
            ambiguities=[scripted.ambiguous] if scripted.ambiguous else [],
        )
        run.discovery_outcome = "confirmed"
    except DiscoveryError as error:
        run.discovery_outcome = "refused"
        run.discovery_refusals += (str(error).splitlines()[0],)


def _role_key(case: Case, inventory: CvInventory, fragment: str) -> str:
    """Return the role a scripted placement names, by a fragment of the employer."""
    if not fragment:
        return inventory.roles[0].key if inventory.roles else ""
    keys = [role.key for role in inventory.roles if fragment in role.organization]
    if len(keys) != 1:
        raise EvalError(f"{case.name}: {fragment!r} names {len(keys)} roles in {case.cv}")
    return keys[0]


def _stages(
    run: CaseRun,
    client: Client,
    case: Case,
    settings: Settings,
    inventory: CvInventory,
) -> None:
    """Drive the shipped stages in order, recording each result as it lands."""
    require_enough_text(case.posting, f"the posting in {case.name}.toml")
    jd = JDText(text=case.posting, origin=f"evals/cases/{case.name}.toml", method="case file")

    # From the raw posting rather than the model's reading of it. That
    # independence is the only reason a disagreement means anything.
    run.reading = fit.read(case.posting, inventory, today=TODAY)
    run.spec = jobspec_stage.extract(client, jd)
    run.evidence = match_stage.match(client, inventory, run.spec, case.rules)
    # Free deterministic Python, so it runs even for a screening case: what a
    # posting would have put on a page shows how two similar ones were read.
    run.plan = select_stage.build_plan(inventory, run.spec, run.evidence, settings, TODAY)
    _relevance_review(run, client, case, inventory)
    _vision(run, client, case)
    _structure(run, client, case)
    _search_vocabulary(run, client, case, inventory)
    _posting_recovery(run, client, case)
    _guided_campaign(run, client, case, inventory)
    if case.mode is not Mode.DRAFT:
        return
    if not run.plan.all_ids():
        run.failure = "selection kept nothing, so there was nothing to draft from"
        return

    run.draft = draft_stage.draft(client, inventory, run.spec, run.plan, case.rules)
    run.fitted, report = rewrite.fitted_and_checked(
        run.draft,
        inventory=inventory,
        spec=run.spec,
        plan=run.plan,
        guardrails=case.rules,
        settings=settings,
    )
    run.markdown = run.fitted.markdown
    if report.passed and case.responses.critic is not None:
        report = rewrite.critiqued(
            client,
            run.fitted,
            report,
            inventory=inventory,
            spec=run.spec,
            guardrails=case.rules,
        )
    run.report = report
    _reviewed(run, case, settings, inventory)
    _cover_letter(run, client, case, inventory)


def _relevance_review(run: CaseRun, client: Client, case: Case, inventory: CvInventory) -> None:
    """Put one scripted relevance-review batch through the shipped stage, offline."""
    scripted = case.relevance_review
    if scripted is None:
        return
    batch = relevance_batch(
        case.name,
        scripted.search_context,
        [posting.model_dump() for posting in scripted.postings],
        inventory,
    )
    plan = review_plan(client.settings, inventory, batch.search_context, batch.posting_block)
    run.relevance_review_instructions = plan.instructions.text
    run.relevance_review_evidence = "" if plan.evidence is None else plan.evidence.text
    run.relevance_review_task = plan.task.text
    raw = client.complete(plan, batch.schema)
    response = RelevanceReviewBatchResponse.model_validate(raw.model_dump(mode="json"))
    valid, failures = batch.validate(response)
    run.relevance_review_valid = tuple(valid)
    run.relevance_review_failures = tuple(failures)


def _cover_letter(run: CaseRun, client: Client, case: Case, inventory: CvInventory) -> None:
    """Draft and check a scripted cover letter against the same selected evidence.

    Reached only after a clean resume draft, the way ``rewrite._render_cover_letter``
    reaches it: the letter shares the plan a resume already selected from.
    """
    if case.responses.cover_letter is None:
        return
    if run.plan is None or run.spec is None or run.fitted is None:
        return
    printed = run.fitted.printed_plan
    letter = draft_stage.cover_letter(client, inventory, run.spec, printed, case.rules)
    run.cover_letter = letter
    run.cover_letter_report = verify_stage.check_letter(
        letter=letter, plan=printed, inventory=inventory, guardrails=case.rules
    )


def _structure(run: CaseRun, client: Client, case: Case) -> None:
    """Extract a scripted CV import through the shipped structure and importer stages, offline.

    Written to a real temporary file and read back with ``sources.read``, so the
    line numbers a scripted extraction cites are the ones the production reader
    would actually number.
    """
    scripted = case.structure
    if scripted is None:
        return
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as handle:
        handle.write(scripted.source)
        path = Path(handle.name)
    try:
        source = cv_sources.read(path)
    finally:
        path.unlink(missing_ok=True)
    extracted = structure_stage.structure(client, source)
    built = importer.assemble(extracted, source, client.settings.sections)
    run.structure_source = source
    run.structure_extracted = extracted
    run.structure_findings = importer.verify(source, extracted, built)


def _vision(run: CaseRun, client: Client, case: Case) -> None:
    """Transcribe a scripted screenshot through the shipped image-ingest path, offline.

    Runs the real ``from_image`` reader against a temporary PNG, so the length
    gate on the transcript is the one a real screenshot goes through too.
    """
    scripted = case.vision
    if scripted is None:
        return
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
        handle.write(base64.b64decode(scripted.image_base64))
        path = Path(handle.name)
    try:
        jd = from_image(path, vision=vision_reader(client))
    finally:
        path.unlink(missing_ok=True)
    run.vision_transcript = jd.text


def _search_vocabulary(run: CaseRun, client: Client, case: Case, inventory: CvInventory) -> None:
    """Ask once for CV-grounded vocabulary through the shipped search-vocabulary stage, offline.

    ``ask`` gathers a workspace's carried rejections and inbox capacity under a
    database session; a case supplies the detached, minimal equivalents instead.
    """
    scripted = case.search_vocabulary
    if scripted is None:
        return
    entry_ids = tuple(entry.id for entry in citable_entries(inventory))
    schema = search_vocabulary_schema(entry_ids)
    parameters = JobSearchParameters(
        seniority=Seniority(excluded_titles=list(scripted.excluded_titles))
    )
    proposed = SearchVocabularyService.ask(
        client,
        parameters,
        (),
        inventory,
        schema,
        CarriedRejections(titles=(), archetypes=(), clusters=()),
        "",
        MAX_TITLES,
        MAX_ARCHETYPES,
        MAX_CLUSTERS,
        MAX_CLUSTERS,
        frozenset(),
    )
    known_ids = {entry.id for entry in inventory.entries}
    run.guided_titles = tuple(searchable_titles(list(proposed.titles), parameters, known_ids))


def _posting_recovery(run: CaseRun, client: Client, case: Case) -> None:
    """Drive posting recovery's pure identity check through the shipped validators, offline.

    Calls the same ``validated_recovery_candidates`` and ``recovery_decision``
    the shipped service calls, then stops before ``resolver.resolve``, which
    reads a live page: a single cited publisher-body or ATS candidate is
    recorded as "candidate_ready" rather than resolved.
    """
    scripted = case.posting_recovery
    if scripted is None:
        return
    target = PostingResolutionTarget(
        POSTING_RECOVERY_IDENTITY_ID,
        scripted.employer,
        scripted.title,
        scripted.known_url,
        PostingContentAccessStatus.RENDER_REQUIRED,
        "e" * 64,
        None,
        scripted.employer,
        scripted.title,
    )
    before = len(client.ledger.entries)
    plan = posting_recovery_plan(target, client.settings)
    schema = posting_recovery_schema(target.id)
    raw = client.complete(plan, schema)
    response = PostingRecoveryResponse.model_validate(raw.model_dump(mode="json"))
    try:
        candidates = validated_recovery_candidates(target, response, client.ledger.entries[before:])
    except (AppValidationError, ValueError) as error:
        run.posting_recovery_outcome = PostingRecoveryOutcome.RESPONSE_REFUSED.value
        run.posting_recovery_detail = str(error).splitlines()[0]
        return
    decision, _ = recovery_decision(candidates)
    run.posting_recovery_outcome = decision.value


def _campaign_manifest() -> tuple[CampaignManifest, CampaignAttemptPlan]:
    """Compile a synthetic two-branch campaign the way a real search's scope would.

    Titles and identities are fixed and synthetic: this drives what the runner
    credits from one captured attempt, not what scope produced it.
    """
    parameters = JobSearchParameters(
        seniority=Seniority(
            target_titles=[
                TargetTitle(title="Clinical Data Steward", lane=Lane.SPECIALIST),
                TargetTitle(title="Program Operations Director", lane=Lane.LEADERSHIP),
            ]
        )
    )
    identities = CampaignIdentities(
        search_parameter_set_id="eval-params",
        search_parameter_sha256="a" * 64,
        master_cv_version_id="eval-cv",
        master_cv_sha256="b" * 64,
        accepted_vocabulary_sha256="c" * 64,
        admission_scope_sha256="d" * 64,
        response_schema_version="guided-campaign-response-eval",
        provider=DEFAULT_PROVIDER.value,
        model="scripted",
    )
    bounds = CampaignBounds(
        max_model_calls=1, max_hosted_searches=2, max_output_tokens=4000, searches_per_attempt=2
    )
    manifest = compile_campaign(
        parameters,
        parameters.seniority.tracked_titles,
        (),
        identities=identities,
        bounds=bounds,
    )
    return manifest, manifest.attempts[0]


def _guided_campaign(run: CaseRun, client: Client, case: Case, inventory: CvInventory) -> None:
    """Dispatch and collect one scripted campaign attempt through the shipped projections, offline.

    ``captured_url_rows`` and ``matched_queries`` are the production rule for
    crediting a capture; this records what they said rather than restating it.
    """
    scripted = case.guided_campaign
    if scripted is None:
        return
    manifest, attempt = _campaign_manifest()
    plan = attempt_request_plan(manifest, attempt, record_block(inventory))
    schema = campaign_response_schema(tuple(branch.key for branch in manifest.branches))
    choice = client.settings.choice_for_stage(Stage.GUIDED_CAMPAIGN)
    dispatched = client.dispatch_background(plan, schema, choice, metadata={})
    poll = client.collect_background(
        dispatched.response_id,
        plan,
        schema,
        choice,
        time.monotonic(),
        request_envelope=dispatched.request_envelope,
    )
    rows, refused_model_picks = captured_url_rows(poll)
    run.campaign_captured_rows = rows
    run.campaign_invalid_model_urls = len(refused_model_picks)
    run.campaign_branch_queries = matched_queries(manifest, attempt, poll)
    run.campaign_task_text = plan.task.text
    run.campaign_branch_labels = tuple(branch.label for branch in manifest.branches)


def _reviewed(run: CaseRun, case: Case, settings: Settings, inventory: CvInventory) -> None:
    """Build the review artifact, and put any scripted approval through it.

    Built for every drafting case whether or not a case decides anything, so a
    builder that crashed on an unusual draft fails here rather than in somebody's
    run. The identity is synthetic, because an evaluation has no posting
    identifier and no settings file.
    """
    if run.fitted is None or run.plan is None or run.spec is None or run.evidence is None:
        return
    if run.report is None or run.draft is None:
        return
    identity = RunIdentity(
        posting_id=f"case-{case.name}",
        cv_hash=inventory.content_hash,
        prompt_suite=prompts.suite_fingerprint(),
        settings_hash="evaluation",
        version="evaluation",
    )
    review = changes.review(
        identity=identity,
        draft=run.draft,
        plan=run.plan,
        inventory=inventory,
        spec=run.spec,
        evidence=run.evidence,
        settings=settings,
        guardrails=case.rules,
        excluded=run.fitted.excluded,
    )
    run.review = review
    if not case.approval:
        return
    payload = review.payload()
    for decision in case.approval:
        target = _decided(case, payload, decision.slot)
        target["decision"] = decision.decision
        if decision.replacement is not None:
            target["replacement"] = {
                "text": decision.replacement.text,
                "source_ids": [
                    _identifier(case, run, alias) for alias in decision.replacement.evidence
                ],
            }
    try:
        _, document = approval.applied(
            approval.Manifest.model_validate(payload),
            review,
            run.draft,
            run.plan,
            inventory=inventory,
            spec=run.spec,
            settings=settings,
            guardrails=case.rules,
            excluded=run.fitted.excluded,
            verification=run.report,
        )
        run.approval_outcome = "applied"
        run.fitted = run.fitted.redrawn(document)
        run.approved_markdown = run.fitted.markdown
    except ApprovalError as error:
        # A case scripting unsupported replacement text measures whether it is
        # refused, not whether the harness survives it.
        run.approval_outcome = "refused"
        run.approval_refusal = str(error).splitlines()[0]


def _decided(case: Case, payload: dict[str, Any], slot: str) -> dict[str, Any]:
    """Return the proposal a scripted decision names, by the end of its slot."""
    found: list[dict[str, Any]] = [
        item for item in payload["proposals"] if str(item["slot"]).endswith(slot)
    ]
    if len(found) != 1:
        raise EvalError(
            f"{case.name}: approval names slot {slot!r}, which matches {len(found)} proposals"
        )
    return found[0]


def _identifier(case: Case, run: CaseRun, alias: str) -> str:
    """Return the entry identifier one evidence alias stands for."""
    if alias not in run.alias_ids:
        raise EvalError(f"{case.name}: no [evidence] alias named {alias!r}")
    return run.alias_ids[alias]


def stage_schemas() -> dict[str, type[BaseModel]]:
    """Return the response contract each stage is held to, by stage.

    The runtime-built schemas are built over placeholder vocabularies, so a
    fingerprint tracks their shape without closing over one CV or one campaign.
    """
    return {
        "jobspec": JobSpecExtraction,
        "match": match_schema(("entry-1",), ("requirement-1",)),
        "draft": ResumeDraft,
        "cover": CoverLetter,
        "critic": CriticVerdict,
        "relevance_review": relevance_review_schema(("e1",), conditions=("condition",)),
        "structure": structure_schema(
            cv_sources.SourceDocument(
                origin="fingerprint",
                method="text",
                lines=(cv_sources.SourceLine(number=1, text="x"),),
            )
        ),
        "vision": VisionTranscript,
        "search_vocabulary": search_vocabulary_schema(("entry-1",)),
        "posting_recovery": posting_recovery_schema(POSTING_RECOVERY_IDENTITY_ID),
        "guided_campaign": campaign_response_schema(("branch-1",)),
    }


def schema_fingerprint(stages: Sequence[str] | None = None) -> str:
    """Return a fingerprint of the response contracts these stages were held to.

    Derived from the JSON Schema sent on the wire, because that is what a
    provider enforces. Passing no stages fingerprints every one of them.
    """
    schemas = stage_schemas()
    wanted = sorted(schemas) if stages is None else sorted(set(stages) & set(schemas))
    digest = hashlib.sha256()
    for stage in wanted:
        model = schemas[stage]
        digest.update(model.__name__.encode("ascii"))
        digest.update(json.dumps(json_schema(model), sort_keys=True).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:12]


def evaluated_prompt_names(stages: Sequence[str] | None = None) -> tuple[str, ...]:
    """Return the prompts these stages send, so nothing else can move the stamp.

    A prompt a case does not send is not a condition its baseline was recorded
    under, so editing one prompt moves only the baselines that sent it.
    """
    keys = sorted(EVALUATED_PROMPTS) if stages is None else sorted(set(stages))
    families = {EVALUATED_PROMPTS[stage] for stage in keys if stage in EVALUATED_PROMPTS}
    names = tuple(name for name in prompts.PROMPT_NAMES if name.rsplit("_", 1)[0] in families)
    unmatched = families - {name.rsplit("_", 1)[0] for name in names}
    if unmatched:
        raise EvalError(f"EVALUATED_PROMPTS names no prompt in the suite: {sorted(unmatched)}")
    return names


def provenance(run: CaseRun | None = None) -> dict[str, str]:
    """Return what one case's result must state about the conditions that produced it.

    Stamped over the stages that case reached, so editing a prompt or a schema
    moves the baselines that used it and leaves the rest comparable.
    """
    stages = None if run is None else sorted(set(run.calls))
    return {
        "source": "scripted",
        "prompt_suite": prompts.suite_fingerprint(evaluated_prompt_names(stages)),
        "schemas": schema_fingerprint(stages),
    }


def check_stages_are_fingerprinted(runs: dict[str, CaseRun]) -> None:
    """Refuse to report a stage whose prompts the stamp does not cover."""
    called = {stage for run in runs.values() for stage in run.calls}
    unstamped = sorted(called - set(EVALUATED_PROMPTS))
    if unstamped:
        raise EvalError(
            f"These stages ran but their prompts are outside the recorded stamp: {unstamped}.\n"
            "  Add them to EVALUATED_PROMPTS, or a change to their prompt would move\n"
            "  what the cases measure without moving what the baselines record."
        )


def check_prompt_coverage_is_declared() -> None:
    """Refuse a suite where a shipped prompt family has no coverage decision.

    The two maps must together name every family exactly once.
    """
    evaluated = set(EVALUATED_PROMPTS.values())
    declared = evaluated | set(UNEVALUATED_PROMPTS)
    families = {name.rsplit("_", 1)[0] for name in prompts.PROMPT_NAMES}
    undeclared = sorted(families - declared)
    if undeclared:
        raise EvalError(
            f"These prompt families ship with no coverage decision: {undeclared}.\n"
            "  Exercise them in a case and add them to EVALUATED_PROMPTS, or name\n"
            "  them in UNEVALUATED_PROMPTS with the reason a case cannot reach them."
        )
    missing = sorted(declared - families)
    if missing:
        raise EvalError(
            f"These prompt families are declared but no longer ship: {missing}.\n"
            "  Remove them from EVALUATED_PROMPTS or UNEVALUATED_PROMPTS."
        )
    both = sorted(evaluated & set(UNEVALUATED_PROMPTS))
    if both:
        raise EvalError(
            f"These prompt families are declared both evaluated and not: {both}.\n"
            "  A family belongs to exactly one of the two maps."
        )
    unreachable = sorted(set(EVALUATED_PROMPTS) - SCRIPTABLE_STAGES)
    if unreachable:
        raise EvalError(
            f"These stages are declared evaluated but no case can script them: {unreachable}.\n"
            "  Give ScriptedProvider._payload a builder for the stage, or move the\n"
            "  family to UNEVALUATED_PROMPTS with the reason no case reaches it."
        )


def score_all(runs: dict[str, CaseRun]) -> dict[str, list[Score]]:
    """Return every score for every case, including the ones that span cases."""
    scored = {name: score(run) for name, run in runs.items()}
    for name, items in cross_case(runs).items():
        scored[name] += items
    return scored


def _record(case: str, payload: dict[str, Any]) -> None:
    """Write one baseline, the only write here, reached only from ``--record-baselines``."""
    baseline.BASELINES.mkdir(parents=True, exist_ok=True)
    path = baseline.path_for(case)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _run_all(cases: Sequence[Case]) -> dict[str, CaseRun]:
    """Run every case and return the results by name."""
    runs: dict[str, CaseRun] = {}
    for case in cases:
        run = execute(case)
        runs[case.name] = run
        state = "refused" if run.rejected_stage else "ran"
        print(f"  {case.name:<32} {state:<8} {len(run.calls)} call(s)  {run.seconds:5.2f}s")
    return runs


def _report(scored: dict[str, list[Score]], stamps: dict[str, dict[str, str]]) -> int:
    """Compare every case to its baseline and print what a reviewer has to decide."""
    violations: list[str] = []
    differences: list[baseline.Difference] = []
    unrecorded: list[str] = []
    drifted: list[str] = []

    for name in sorted(scored):
        for item in scored[name]:
            if not item.ok:
                violations.append(f"{name}: {item.name}: {item.value} ({item.detail})")
        recorded = baseline.load(name)
        if recorded is None:
            unrecorded.append(name)
            continue
        if baseline.provenance(recorded, stamps[name]):
            drifted.append(name)
        values = {item.name: item.value for item in scored[name]}
        differences += baseline.compare(name, recorded, values)

    if drifted:
        print(
            f"\n{len(drifted)} baseline(s) were recorded under different conditions than "
            f"this run: {', '.join(drifted)}"
        )
    if unrecorded:
        print(f"\n{len(unrecorded)} case(s) have no baseline: {', '.join(unrecorded)}")
    if violations:
        print(f"\n{len(violations)} declared expectation(s) violated:")
        for line in violations:
            print(f"  {line}")
    if differences:
        print(f"\n{len(differences)} measurement(s) differ from the baseline:")
        for difference in differences:
            print(f"  {difference.line()}")
    if not (violations or differences or unrecorded or drifted):
        print(f"\n{len(scored)} case(s) match their baselines.")
        return 0
    print(
        "\nA difference is not automatically a regression, and a baseline is not\n"
        "automatically wrong. Read the diff, decide, and only then re-record."
    )
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    """Run the evaluations, or record their baselines. Never both."""
    parser = argparse.ArgumentParser(
        prog="python -m evals",
        description="Run tailorcv's evaluation cases against the deterministic pipeline.",
    )
    parser.add_argument("--case", action="append", default=[], help="run only these cases")
    parser.add_argument(
        "--record-baselines",
        action="store_true",
        help="rewrite the baselines from this run and compare nothing",
    )
    args = parser.parse_args(argv)

    forbid_network()

    try:
        seal()
        check_prompt_coverage_is_declared()
        cases = [case for case in load_all() if not args.case or case.name in args.case]
        if not cases:
            raise EvalError(f"No case matched {args.case}. Look in evals/cases/.")
        print(f"{len(cases)} case(s), source scripted\n")
        runs = _run_all(cases)
        check_stages_are_fingerprinted(runs)
        scored = score_all(runs)
    except (EvalError, CaseError) as error:
        print(f"\n{error}", file=sys.stderr)
        return 2

    # Cross-case scorers add names to `scored` that no run produced, so a stamp
    # for one of those covers every stage the suite reached.
    stamps = {name: provenance(runs.get(name)) for name in scored}
    if args.record_baselines:
        for name, items in sorted(scored.items()):
            values = {item.name: item.value for item in items}
            _record(name, {**stamps[name], "case": name, "scores": values})
        print(f"\nRecorded {len(scored)} baseline(s). Read the diff before committing it.")
        return 0
    return _report(scored, stamps)


def seal() -> None:
    """Refuse to run against settings this machine happens to be carrying.

    Every field of ``Settings`` is settable from the environment, so an exported
    ``TAILORCV_PAGES`` would move a baseline for a reason that is not in the
    repository. Provider keys are not checked: nothing here resolves a credential
    and sockets are already closed.
    """
    stray = sorted(name for name in os.environ if name.startswith("TAILORCV_"))
    if stray:
        raise EvalError(
            "These environment variables would change what the evaluation measures:\n"
            f"  {', '.join(stray)}\n"
            "  Unset them and run again. A baseline has to be a property of the\n"
            "  repository, not of the shell it was recorded in."
        )


__all__ = ["EvalError", "ScriptedProvider", "build_provider", "execute", "main", "score_all"]
