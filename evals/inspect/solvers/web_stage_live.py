"""Live solvers for the two hosted-web-search stages: posting recovery and guided campaign.

Only ``scripts/eval.sh --live`` should reach these solvers. Each sample sends one
production request through the production validation and stops before any page
read, so a run spends provider money and never touches a job board.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel

from clearscout.application.config import ProviderName, Settings, Stage
from clearscout.application.discovery.assisted_content_resolution import (
    RecoveryCandidate,
    RecoveryResponseRefusedError,
    cited_recovery_search,
)
from clearscout.application.discovery.content_resolution import PostingResolutionTarget
from clearscout.application.discovery.derived_vocabulary_rules import searched_titles
from clearscout.application.discovery.guided_campaign import (
    CampaignAttemptPlan,
    CampaignBounds,
    CampaignIdentities,
    CampaignManifest,
    compile_campaign,
)
from clearscout.application.discovery.guided_campaign_request import (
    RESPONSE_SCHEMA_VERSION,
    attempt_request_plan,
    campaign_response_schema,
)
from clearscout.application.discovery.guided_campaign_runner import (
    captured_url_rows,
    matched_queries,
    poll_until_terminal,
)
from clearscout.application.discovery.search_parameter_summary import search_summary
from clearscout.application.prompts import data_block, load
from clearscout.application.tailoring.stages.context import record_block
from clearscout.domain import PostingContentAccessStatus
from clearscout.domain.identity import url_host
from clearscout.domain.search_parameters import EmployerArchetype, JobSearchParameters
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.providers.base import BackgroundPoll, BackgroundWebSearchProvider
from clearscout.infrastructure.providers.client import Client
from clearscout.infrastructure.providers.plan import RequestPlan
from clearscout.shared.costs import Ledger
from clearscout.shared.costs.prices import table as price_table
from clearscout.shared.errors import ProviderError
from evals.inspect.solvers.clearscout_live import (
    billing_target,
    paid_call,
    paid_stage_metadata,
    prompt_snapshot_fingerprint,
    run_settings,
    sample_provenance,
)
from evals.support.inventory import synthetic_inventory

RECOVERY_PROMPTS = ("posting_recovery_system", "posting_recovery_user")
CAMPAIGN_PROMPTS = ("guided_campaign_system", "guided_campaign_user")
RECOVERY_BILLING_ORIGIN = "eval.posting_recovery_live"
CAMPAIGN_BILLING_ORIGIN = "eval.guided_campaign_live"
PROVIDER_FAILED = "provider_failed"
RESPONSE_REFUSED = "response_refused"
_FINGERPRINT = "0" * 64


def recovery_target(sample: dict[str, Any]) -> PostingResolutionTarget:
    """Return the pending identity one recovery case describes, as preflight would present it."""
    case = sample["input"]
    read = case.get("known_url_read_outcome") or PostingContentAccessStatus.NOT_YET_READ.value
    return PostingResolutionTarget(
        id=str(sample["id"]),
        employer=str(case["employer"]),
        title=str(case["title"]),
        url=str(case["known_url"]),
        content_status=PostingContentAccessStatus(read),
        fingerprint=_FINGERPRINT,
        current_content_id=None,
        expected_employer=str(case["employer"]),
        expected_title=str(case["title"]),
        registered_board_url=str(case.get("registered_board_url") or ""),
    )


def _candidate_row(candidate: RecoveryCandidate) -> dict[str, Any]:
    return {
        "url": candidate.url,
        "host": url_host(candidate.url),
        "authority": candidate.observation.authority.value,
        "identity": candidate.observation.identity,
        "already_failed": candidate.already_failed,
    }


def recover(client: Client, target: PostingResolutionTarget) -> dict[str, Any]:
    """Send one identity's cited search and settle it as production does, without reading a page.

    A provider failure other than a schema refusal stops the run rather than scoring a miss.
    """
    try:
        search = cited_recovery_search(target, client)
    except RecoveryResponseRefusedError as error:
        return {
            "completion": RESPONSE_REFUSED,
            "outcome": RESPONSE_REFUSED,
            "error": " ".join(str(error).split())[:500],
            "candidates": [],
            "picked": None,
        }
    return {
        "completion": search.decision.value,
        "outcome": search.decision.value,
        "candidates": [_candidate_row(candidate) for candidate in search.candidates],
        "picked": None if search.candidate is None else search.candidate.url,
    }


def working_prompts(names: tuple[str, ...]) -> Mapping[str, str]:
    """Return the working-tree prompt bytes a run binds to at preflight."""
    return MappingProxyType({name: load(name) for name in names})


def _unchanged(names: tuple[str, ...], snapshot: Mapping[str, str]) -> Mapping[str, str]:
    if prompt_snapshot_fingerprint(names, working_prompts(names)) != prompt_snapshot_fingerprint(
        names, snapshot
    ):
        raise RuntimeError("Live evaluation refused: prompt bytes changed after run preflight.")
    return snapshot


@dataclass(frozen=True)
class _PostingRecoveryLiveSolver:
    """One paid recovery run: one cited search per case and trial."""

    run_metadata: dict[str, Any]
    prompt_snapshot: Mapping[str, str]
    provider_name: ProviderName
    settings: Settings
    ledger: Ledger
    database: Database
    workspace_id: str

    def __call__(self, sample: dict[str, Any]) -> dict[str, Any]:
        snapshot = _unchanged(RECOVERY_PROMPTS, self.prompt_snapshot)
        target = recovery_target(sample)
        paid = paid_call(
            lambda client: recover(client, target),
            provider_name=self.provider_name,
            settings=self.settings,
            run_ledger=self.ledger,
            run_metadata=self.run_metadata,
            prompt_snapshot=snapshot,
            database=self.database,
            workspace_id=self.workspace_id,
            origin=RECOVERY_BILLING_ORIGIN,
        )
        provenance = sample_provenance(
            paid.ledger, self.ledger, self.run_metadata, paid.billing_failure
        )
        searches = sum(entry.search_count for entry in paid.ledger.entries)
        return {**paid.value, "hosted_searches": searches, "metadata": provenance}


def posting_recovery_live_solver(dataset: Path) -> _PostingRecoveryLiveSolver:
    """Return the prepared solver that runs posting recovery live over ``dataset``."""
    run_metadata = paid_stage_metadata(Stage.POSTING_RECOVERY, RECOVERY_PROMPTS, dataset)
    provider_name = ProviderName(run_metadata["provider"])
    settings = run_settings(provider_name, Stage.POSTING_RECOVERY)
    database, workspace_id = billing_target()
    return _PostingRecoveryLiveSolver(
        run_metadata=run_metadata,
        prompt_snapshot=working_prompts(RECOVERY_PROMPTS),
        provider_name=provider_name,
        settings=settings,
        ledger=Ledger(prices=price_table(settings.prices)),
        database=database,
        workspace_id=workspace_id,
    )


@dataclass(frozen=True)
class CampaignRequest:
    """One case's compiled campaign, the attempt it sends, and that attempt's request."""

    manifest: CampaignManifest
    attempt: CampaignAttemptPlan
    plan: RequestPlan
    schema: type[BaseModel]


def campaign_parameters(sample: dict[str, Any]) -> JobSearchParameters:
    """Return the synthetic search parameter set one campaign case carries."""
    return JobSearchParameters.model_validate(sample["input"]["parameters"])


def campaign_request(sample: dict[str, Any], settings: Settings) -> CampaignRequest:
    """Compile a case's campaign as a fresh approval would and build its named attempt."""
    parameters = campaign_parameters(sample)
    archetypes = [
        EmployerArchetype.model_validate(item) for item in sample["input"].get("archetypes", [])
    ]
    choice = settings.choice_for_stage(Stage.GUIDED_CAMPAIGN, settings.unattended_tier())
    identities = CampaignIdentities(
        search_parameter_set_id=str(sample["id"]),
        search_parameter_sha256=_FINGERPRINT,
        master_cv_version_id="synthetic",
        master_cv_sha256=_FINGERPRINT,
        accepted_vocabulary_sha256=_FINGERPRINT,
        admission_scope_sha256=_FINGERPRINT,
        response_schema_version=RESPONSE_SCHEMA_VERSION,
        provider=settings.provider.value,
        model=choice.model,
        effort=choice.effort.value if choice.effort is not None else "",
        verbosity=choice.verbosity.value if choice.verbosity is not None else "",
        service_tier=choice.service_tier.value,
    )
    manifest = compile_campaign(
        parameters,
        searched_titles(parameters, (), ()),
        archetypes,
        identities=identities,
        bounds=CampaignBounds(),
    )
    position = int(sample["input"].get("attempt_position", 1))
    attempt = next(item for item in manifest.attempts if item.position == position)
    evidence = "\n\n".join(
        part
        for part in (
            data_block("declared_search_preferences", search_summary(parameters, archetypes)),
            record_block(synthetic_inventory()),
        )
        if part
    )
    return CampaignRequest(
        manifest=manifest,
        attempt=attempt,
        plan=attempt_request_plan(manifest, attempt, evidence),
        schema=campaign_response_schema(tuple(branch.key for branch in manifest.branches)),
    )


class _UnfinishedAttemptError(ProviderError):
    """A dispatched background response that never reached a terminal poll."""

    def __init__(self, response_id: str, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.response_id = response_id


def _cancel_quietly(client: Client, response_id: str) -> None:
    if not isinstance(client.provider, BackgroundWebSearchProvider):
        return
    # The response may finish between poll and cancel, so a refused cancel is expected.
    with contextlib.suppress(Exception):
        client.provider.cancel_background(response_id)


def _poll_to_terminal(client: Client, request: CampaignRequest) -> BackgroundPoll:
    """Dispatch one attempt and poll it to a terminal status, as the campaign runner does.

    An attempt that stops short is cancelled, and its response id is raised for spend
    reconciliation, since the provider may still bill it.
    """
    choice = client.settings.choice_for_stage(Stage.GUIDED_CAMPAIGN, client.service_tier)
    dispatched = client.dispatch_background(
        request.plan, request.schema, choice, metadata={"origin": CAMPAIGN_BILLING_ORIGIN}
    )
    started = time.monotonic()
    try:
        return poll_until_terminal(
            lambda: client.collect_background(
                dispatched.response_id,
                request.plan,
                request.schema,
                choice,
                started,
                request_envelope=dispatched.request_envelope,
            ),
            client.sleeper,
        )
    except BaseException as error:
        _cancel_quietly(client, dispatched.response_id)
        if isinstance(error, ProviderError):
            raise _UnfinishedAttemptError(dispatched.response_id, error) from error
        raise


def _url_picks(value: Any, field: str) -> list[dict[str, Any]]:
    return [
        {"url": item.url, "branch_keys": list(item.branch_keys), "why": item.why}
        for item in getattr(value, field, None) or ()
    ]


def campaign_result(request: CampaignRequest, poll: BackgroundPoll) -> dict[str, Any]:
    """Return what one attempt searched, which branches production credits, and its picks."""
    searches = poll.record.searches if poll.record is not None else ()
    _, refused = captured_url_rows(poll)
    by_key = {branch.key: branch for branch in request.manifest.branches}
    return {
        "completion": poll.status,
        "status": poll.status,
        "error": poll.error,
        "search_cap": request.attempt.search_cap,
        "search_count": 0 if poll.record is None else poll.record.search_count or len(searches),
        "queries": [query for search in searches for query in search.queries],
        "unattributable_searches": sum(1 for search in searches if not search.queries),
        "branches": [
            {"key": key, "label": by_key[key].label, "queries": list(queries)}
            for key, queries in matched_queries(request.manifest, request.attempt, poll).items()
        ],
        "postings": _url_picks(poll.value, "postings"),
        "boards": _url_picks(poll.value, "boards"),
        "refused_picks": [dict(item) for item in refused],
    }


@dataclass(frozen=True)
class _GuidedCampaignLiveSolver:
    """One paid campaign run: one background attempt per case and trial."""

    run_metadata: dict[str, Any]
    prompt_snapshot: Mapping[str, str]
    provider_name: ProviderName
    settings: Settings
    ledger: Ledger
    database: Database
    workspace_id: str

    def __call__(self, sample: dict[str, Any]) -> dict[str, Any]:
        snapshot = _unchanged(CAMPAIGN_PROMPTS, self.prompt_snapshot)
        paid = paid_call(
            lambda client: self._attempt(client, sample),
            provider_name=self.provider_name,
            settings=self.settings,
            run_ledger=self.ledger,
            run_metadata=self.run_metadata,
            prompt_snapshot=snapshot,
            database=self.database,
            workspace_id=self.workspace_id,
            origin=CAMPAIGN_BILLING_ORIGIN,
        )
        provenance = sample_provenance(
            paid.ledger, self.ledger, self.run_metadata, paid.billing_failure
        )
        return {**paid.value, "metadata": provenance}

    def _attempt(self, client: Client, sample: dict[str, Any]) -> dict[str, Any]:
        # The prompts render inside paid_call, so the arm's bytes are the ones sent.
        request = campaign_request(sample, self.settings)
        try:
            poll = _poll_to_terminal(client, request)
        except _UnfinishedAttemptError as error:
            return {
                "completion": PROVIDER_FAILED,
                "status": PROVIDER_FAILED,
                "error": " ".join(str(error).split())[:500],
                "unbilled_response_id": error.response_id,
            }
        return campaign_result(request, poll)


def guided_campaign_live_solver(dataset: Path) -> _GuidedCampaignLiveSolver:
    """Return the prepared solver that runs one guided campaign attempt per case live."""
    run_metadata = paid_stage_metadata(Stage.GUIDED_CAMPAIGN, CAMPAIGN_PROMPTS, dataset)
    provider_name = ProviderName(run_metadata["provider"])
    settings = run_settings(provider_name, Stage.GUIDED_CAMPAIGN)
    database, workspace_id = billing_target()
    return _GuidedCampaignLiveSolver(
        run_metadata=run_metadata,
        prompt_snapshot=working_prompts(CAMPAIGN_PROMPTS),
        provider_name=provider_name,
        settings=settings,
        ledger=Ledger(prices=price_table(settings.prices)),
        database=database,
        workspace_id=workspace_id,
    )
