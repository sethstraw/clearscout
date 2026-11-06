"""Live solvers for the two hosted-web-search stages: posting recovery and guided campaign.

Only ``scripts/eval.sh --live`` should reach these solvers. Each sample sends one
production request through the production validation. Recovery stops before any
page read. A campaign sample then reads its picks through campaign admission's
read phase, under the published fetch policy, and writes nothing.
"""

from __future__ import annotations

import contextlib
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from functools import partial
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
from clearscout.application.discovery.board_scan import SourceScanService
from clearscout.application.discovery.board_slugs import board_identity_key
from clearscout.application.discovery.content_resolution import PostingResolutionTarget
from clearscout.application.discovery.derived_vocabulary_rules import searched_titles
from clearscout.application.discovery.guided_campaign import (
    CampaignAttemptPlan,
    CampaignBounds,
    CampaignIdentities,
    CampaignManifest,
    compile_campaign,
)
from clearscout.application.discovery.guided_campaign_admission import (
    ObservedUrl,
    ReadUrl,
    read_captured_urls,
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
from clearscout.application.discovery.posting_fetch import FetchedPosting, PostingFetcher
from clearscout.application.discovery.posting_identity import (
    BoardHolder,
    BoardRecord,
    board_holder,
    posting_identity_keys,
)
from clearscout.application.discovery.search_parameter_summary import search_summary
from clearscout.application.prompts import data_block, load
from clearscout.application.tailoring.stages.context import record_block
from clearscout.domain import PostingContentAccessStatus
from clearscout.domain.identity import requisition_key, url_host
from clearscout.domain.records import PostingReadIntent
from clearscout.domain.search_parameters import EmployerArchetype, JobSearchParameters
from clearscout.domain.staged_facts import StagedListingFacts
from clearscout.infrastructure.artifacts.runtime import RuntimeStorage
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.providers.base import BackgroundPoll, BackgroundWebSearchProvider
from clearscout.infrastructure.providers.client import Client
from clearscout.infrastructure.providers.plan import RequestPlan
from clearscout.infrastructure.web_fetch.ingest.url import USER_AGENT
from clearscout.infrastructure.web_fetch.policy import PoliteHttpClient
from clearscout.infrastructure.web_fetch.posting_fetch import PolitePostingFetcher
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
PREFLIGHT_HELD = "preflight_held"
_FINGERPRINT = "0" * 64


def recovery_identity(case_id: str) -> str:
    """Return the opaque identity id the model sees for a case; case ids name the answer."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"clearscout-eval:{case_id}"))


def recovery_target(sample: dict[str, Any]) -> PostingResolutionTarget:
    """Return the pending identity one recovery case describes, as preflight would present it."""
    case = sample["input"]
    read = case.get("known_url_read_outcome") or PostingContentAccessStatus.NOT_YET_READ.value
    board = str(case.get("registered_board_url") or "")
    return PostingResolutionTarget(
        id=recovery_identity(str(sample["id"])),
        employer=str(case["employer"]),
        title=str(case["title"]),
        url=str(case["known_url"]),
        content_status=PostingContentAccessStatus(read),
        fingerprint=_FINGERPRINT,
        current_content_id=None,
        expected_employer=str(case["employer"]),
        expected_title=str(case["title"]),
        registered_board_url=board,
        registered_boards=(board,) if board else (),
        board_records=tuple(_board_records(case)) if board else (),
    )


def _board_records(case: dict[str, Any]) -> list[BoardRecord]:
    """Return the records a case says its registered board stores."""
    board = str(case.get("registered_board_url") or "")
    employer = str(case["employer"])
    return [
        BoardRecord(
            holder=BoardHolder(row.get("kind", "job_posting"), f"board-{index}", row["url"]),
            source_id=board,
            title=str(row["title"]),
            places=tuple(row.get("places") or ()),
            requisitions=frozenset(
                requisition_key(employer, str(value)) for value in row.get("requisitions", ())
            ),
            closed=bool(row.get("closed", False)),
        )
        for index, row in enumerate(case.get("board_records") or [])
    ]


def preflight_holder(sample: dict[str, Any]) -> BoardHolder | None:
    """Return the registered-board record production preflight finds holding this case.

    A case lists its registered board's stored records under ``board_records``; each
    goes through the same title, place, requisition and open-record match preflight uses.
    """
    case = sample["input"]
    board = str(case.get("registered_board_url") or "")
    rows = case.get("board_records") or []
    if not board or not rows:
        return None
    employer = str(case["employer"])
    requisition = str(case.get("requisition") or "")
    return board_holder(
        str(case["title"]),
        tuple(case.get("places") or ()),
        frozenset({requisition_key(employer, requisition)} if requisition else ()),
        _board_records(case),
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
        holder = preflight_holder(sample)
        if holder is not None:
            unbilled = Ledger(prices=self.ledger.prices)
            return {
                "completion": PREFLIGHT_HELD,
                "outcome": PREFLIGHT_HELD,
                "candidates": [],
                "picked": holder.url,
                "hosted_searches": 0,
                "metadata": sample_provenance(unbilled, self.ledger, self.run_metadata),
            }
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
    parameters: JobSearchParameters
    searched_titles: tuple[str, ...]
    known_source_roots: frozenset[str] = frozenset()
    known_posting_fingerprints: frozenset[str] = frozenset()


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
    titles = searched_titles(parameters, (), ())
    manifest = compile_campaign(
        parameters,
        titles,
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
        parameters=parameters,
        searched_titles=tuple(item.title for item in titles),
        # A case plants what its workspace already holds, keyed as known_identities_in keys it.
        known_source_roots=frozenset(
            board_identity_key(str(url)) for url in sample["input"].get("registered_boards", [])
        ),
        known_posting_fingerprints=frozenset(
            key
            for url in sample["input"].get("known_urls", [])
            for key in posting_identity_keys(str(url))
        ),
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
        "page_action_count": 0 if poll.record is None else poll.record.page_action_count,
        "branches": [
            {
                "key": key,
                "label": by_key[key].label,
                "titles": list(by_key[key].title_aliases),
                "queries": list(queries),
            }
            for key, queries in matched_queries(request.manifest, request.attempt, poll).items()
        ],
        "postings": _url_picks(poll.value, "postings"),
        "boards": _url_picks(poll.value, "boards"),
        "refused_picks": [dict(item) for item in refused],
    }


class RecordedPostings:
    """A posting fetcher that keeps each read by its requested URL for the scorer."""

    def __init__(self, fetcher: PostingFetcher) -> None:
        """Wrap ``fetcher`` with an empty record of reads."""
        self.fetcher = fetcher
        self.reads: dict[str, FetchedPosting] = {}

    def fetch(
        self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL, board_url: str = ""
    ) -> FetchedPosting:
        """Read one posting through the wrapped fetcher and keep what it returned."""
        fetched = self.fetcher.fetch(url, intent=intent, board_url=board_url)
        self.reads[url] = fetched
        return fetched


@contextlib.contextmanager
def live_pick_scans(database: Database) -> Iterator[SourceScanService]:
    """Yield admission's read seam over the polite web client, closing the client after."""
    with contextlib.closing(PoliteHttpClient(user_agent=USER_AGENT)) as web:
        yield SourceScanService(
            database,
            storage=RuntimeStorage.resolve(database.url),
            posting_fetcher=RecordedPostings(PolitePostingFetcher(client=web)),
            http_client=web,
        )


def _read_row(read: ReadUrl, fetched: FetchedPosting | None) -> dict[str, Any]:
    check = None if fetched is None else fetched.publisher_check
    facts = check.facts if check is not None else StagedListingFacts()
    return {
        "url": read.item.url,
        "classification": read.classification.value,
        "outcome": read.outcome.value,
        "detail": read.detail,
        "staged_basis": None if read.staged_basis is None else read.staged_basis.value,
        "title": read.title,
        "employer": read.employer or read.board_employer,
        "findings": [finding.model_dump() for finding in read.screening_findings],
        "board_root": read.board_root,
        "board_listings": [
            {"url": listing.url, "title": listing.title, "location": listing.location}
            for listing in read.board_listings
        ],
        # The facts admission's title screen read, so the scorer can screen as it did.
        "role_aliases": list(facts.role_aliases.values),
        "focus_terms": list(facts.focus_terms.values),
        "publisher_vouched": facts.publisher_vouched,
        "posting_status": "" if fetched is None else fetched.posting_status,
    }


def pick_reads(
    scans: SourceScanService, request: CampaignRequest, poll: BackgroundPoll
) -> list[dict[str, Any]]:
    """Return what campaign admission's read phase decides for each model pick.

    Only picked rows are read, and only what the case planted counts as already held.
    """
    rows, _ = captured_url_rows(poll)
    picked = tuple(
        ObservedUrl(
            url=str(row["url"]),
            observations=tuple(
                (str(item["action_kind"]), str(item["query"])) for item in row["observations"]
            ),
            model_selected=True,
            model_why=str(row.get("model_why", "")),
            model_board=bool(row.get("model_board", False)),
        )
        for row in rows
        if row.get("model_selected")
    )
    reads = read_captured_urls(
        scans,
        parameters=request.parameters,
        searched_titles=list(request.searched_titles),
        captured=picked,
        known_source_roots=request.known_source_roots,
        known_posting_fingerprints=request.known_posting_fingerprints,
    )
    fetcher = scans.postings.fetcher
    recorded = fetcher.reads if isinstance(fetcher, RecordedPostings) else {}
    return [_read_row(read, recorded.get(read.item.url)) for read in reads]


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
    open_scans: Callable[[], AbstractContextManager[SourceScanService]]

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
        if isinstance(paid.value, dict):
            return {**paid.value, "metadata": provenance}
        request, poll = paid.value
        result = {**campaign_result(request, poll), "metadata": provenance}
        try:
            with self.open_scans() as scans:
                result["reads"] = pick_reads(scans, request, poll)
        except Exception as error:  # noqa: BLE001 - the attempt is billed; keep it, record why
            result["reads_error"] = f"{type(error).__name__}: {' '.join(str(error).split())}"[:500]
        return result

    def _attempt(
        self, client: Client, sample: dict[str, Any]
    ) -> dict[str, Any] | tuple[CampaignRequest, BackgroundPoll]:
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
        return request, poll


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
        open_scans=partial(live_pick_scans, database),
    )
