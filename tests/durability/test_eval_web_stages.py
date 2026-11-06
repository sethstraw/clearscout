"""The live posting-recovery and guided-campaign tasks: datasets, solvers, scorers, headers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from evals.inspect import runner
from evals.inspect.runner import dataset_samples
from evals.inspect.scorers.guided_campaign import campaign_attempt
from evals.inspect.scorers.posting_recovery import recovered_posting
from evals.inspect.solvers import clearscout_live, web_stage_live
from evals.inspect.tasks import guided_campaign_live_task as campaign_task
from evals.inspect.tasks import posting_recovery_live_task as recovery_task
from pydantic import BaseModel

from clearscout.application.config import ProviderName, Settings, Stage
from clearscout.application.discovery.board_scan import SourceScanService
from clearscout.application.discovery.guided_campaign_admission import NOT_CAPTURED_DETAIL
from clearscout.application.discovery.posting_fetch import FetchedPosting
from clearscout.application.discovery.source_reading import PostingCheck
from clearscout.domain import utc_now
from clearscout.domain.records import PageClassification, PostingReadIntent
from clearscout.domain.staged_facts import PublishedFact, StagedListingFacts
from clearscout.infrastructure.artifacts.runtime import RuntimeStorage
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.providers.base import (
    BackgroundPoll,
    CallRecord,
    Search,
    SearchSource,
)
from clearscout.infrastructure.providers.cache import ResponseCache
from clearscout.infrastructure.providers.client import Client
from clearscout.infrastructure.web_fetch.policy import PoliteHttpClient
from clearscout.shared.costs import Ledger, Usage
from clearscout.shared.errors import ProviderError
from tests.support.doubles import ScriptedBoards, ScriptedProvider

CONTOSO_CAREERS = "https://careers.contoso.example/jobs/data-platform-engineer"
ASHBY_OTHER = "https://jobs.ashbyhq.com/acme/00000000-0000-4000-8000-000000000002"
FABRIKAM_CAREERS = "https://careers.fabrikam.example/openings/payments-reliability"


def _samples(dataset: Path) -> list[dict[str, Any]]:
    return dataset_samples(dataset, split="development")


def _case(dataset: Path, case_id: str) -> dict[str, Any]:
    return next(row for row in _samples(dataset) if row["id"] == case_id)


def _no_wait(_seconds: float) -> None:
    raise AssertionError("a scripted poll must finish without waiting")


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    provider: ScriptedProvider,
    sleeper: Callable[[float], None] = _no_wait,
) -> None:
    def build(_name: ProviderName, _key: str, *, ledger: Ledger, settings: Settings) -> Client:
        return Client(
            provider=provider,
            settings=settings,
            cache=ResponseCache(enabled=False),
            ledger=ledger,
            sleeper=sleeper,
        )

    monkeypatch.setattr(clearscout_live, "credential_for", lambda provider_name: "token")
    monkeypatch.setattr(clearscout_live, "build_live_client", build)
    monkeypatch.setattr(clearscout_live, "bill_sample", lambda *args, **kwargs: None)


def _observation(url: str, authority: str = "publisher_body") -> dict[str, str]:
    return {
        "url": url,
        "authority": authority,
        "completeness": "complete",
        "identity": "exact",
        "posting_status": "active",
        "note": "The page names the supplied title and employer.",
    }


def _answer(identity: str, *candidates: str, authority: str = "publisher_body") -> dict[str, Any]:
    return {
        "identity_id": web_stage_live.recovery_identity(identity),
        "candidate_urls": list(candidates),
        "supporting_urls": [],
        "observations": [_observation(url, authority) for url in candidates],
    }


def _recovery_provider(answers: list[tuple[dict[str, Any], list[str]]]) -> ScriptedProvider:
    """Answer each recovery call in order, citing that call's own sources."""
    payloads: Iterator[dict[str, Any]] = iter(answer for answer, _ in answers)
    return ScriptedProvider(
        {"PostingRecoveryResponse": lambda _schema: next(payloads)},
        web_search_sources=[
            [SearchSource(url=url, title="Opening") for url in sources] for _, sources in answers
        ],
    )


def _recovery_solver(
    monkeypatch: pytest.MonkeyPatch, provider: ScriptedProvider
) -> web_stage_live._PostingRecoveryLiveSolver:
    _wire(monkeypatch, provider)
    monkeypatch.delenv(clearscout_live.PROMPT_ARMS_ENV, raising=False)
    return web_stage_live._PostingRecoveryLiveSolver(
        run_metadata={"provider": "openai"},
        prompt_snapshot=web_stage_live.working_prompts(web_stage_live.RECOVERY_PROMPTS),
        provider_name=ProviderName.OPENAI,
        settings=Settings(provider=ProviderName.OPENAI),
        ledger=Ledger(),
        database=None,  # type: ignore[arg-type]
        workspace_id="ws",
    )


def _recovery_score(
    monkeypatch: pytest.MonkeyPatch, case_id: str, answer: dict[str, Any], sources: list[str]
) -> tuple[dict[str, Any], ScriptedProvider]:
    provider = _recovery_provider([(answer, sources)])
    sample = _case(recovery_task.DATASET_PATH, case_id)
    output = _recovery_solver(monkeypatch, provider)({"id": sample["id"], "input": sample["input"]})
    score: dict[str, Any] = recovered_posting()(sample, output)
    return score, provider


def test_both_tasks_declare_their_production_stage() -> None:
    assert Stage.POSTING_RECOVERY.value == recovery_task.STAGE
    assert Stage.GUIDED_CAMPAIGN.value == campaign_task.STAGE


@pytest.mark.parametrize(
    ("dataset", "scorer"),
    [
        (recovery_task.DATASET_PATH, recovered_posting()),
        (campaign_task.DATASET_PATH, campaign_attempt()),
    ],
)
def test_every_tracked_case_loads_and_validates(dataset: Path, scorer: Any) -> None:
    samples = _samples(dataset)

    assert len({sample["id"] for sample in samples}) == len(samples) >= 2
    for sample in samples:
        scorer.validate_sample(sample)


def test_a_recovery_label_with_a_relative_url_is_refused() -> None:
    sample = _case(recovery_task.DATASET_PATH, "pr-withdrawn")
    broken = {**sample, "expect": {"urls": ["/jobs/1"], "hosts": []}}

    with pytest.raises(ValueError, match="cannot key"):
        recovered_posting().validate_sample(broken)


def test_the_cited_expected_url_is_a_url_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    score, provider = _recovery_score(
        monkeypatch,
        "pr-ats-tenant-render",
        _answer("pr-ats-tenant-render", CONTOSO_CAREERS),
        [CONTOSO_CAREERS],
    )

    assert score["value"] is True
    assert score["metadata"]["matched"] == "url"
    assert score["metadata"]["outcome"] == "candidate_ready"
    assert score["metadata"]["hosted_searches"] == 1
    assert score["metadata"]["candidates"][0]["host"] == "careers.contoso.example"
    # The production request names the employer's own ATS tenant as its publisher.
    sent = provider.calls[0][1].task.text
    assert '"known_publisher_domain":"boards.greenhouse.io"' in sent
    assert '"known_url_read_outcome":"render_required"' in sent


GLOBEX_BOARD = "https://jobs.ashbyhq.com/globex"
GLOBEX_OPEN = f"{GLOBEX_BOARD}/00000000-0000-4000-8000-000000000011"
GLOBEX_CLOSED = f"{GLOBEX_BOARD}/00000000-0000-4000-8000-000000000010"


def _board_case(*records: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "pr-board-held",
        "split": "development",
        "input": {
            "employer": "Globex",
            "title": "Data Platform Engineer",
            "known_url": "https://listings.example.com/view/data-platform-4410",
            "known_url_read_outcome": None,
            "registered_board_url": GLOBEX_BOARD,
            "board_records": list(records),
        },
        "expect": {"urls": [GLOBEX_OPEN], "hosts": []},
        "why": "The employer's registered board holds the open posting.",
    }


def test_a_case_preflight_holds_is_scored_by_its_holder_without_a_paid_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _recovery_provider([])
    sample = _board_case(
        {"url": GLOBEX_CLOSED, "title": "Data Platform Engineer", "closed": True},
        {"url": GLOBEX_OPEN, "title": "Data Platform Engineer"},
    )

    output = _recovery_solver(monkeypatch, provider)({"id": sample["id"], "input": sample["input"]})
    score = recovered_posting()(sample, output)

    assert score["value"] is True
    assert score["metadata"]["outcome"] == "preflight_held"
    assert score["checks"]["warned"] == ["preflight_held"]
    assert score["metadata"]["matched"] == "url"
    assert score["metadata"]["hosted_searches"] == 0
    assert output["metadata"]["actual_dollars"] == 0
    assert provider.calls == []


def test_a_case_preflight_cannot_settle_goes_to_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _recovery_provider([(_answer("pr-board-held", GLOBEX_OPEN), [GLOBEX_OPEN])])
    sample = _board_case({"url": GLOBEX_CLOSED, "title": "Data Platform Engineer", "closed": True})

    output = _recovery_solver(monkeypatch, provider)({"id": sample["id"], "input": sample["input"]})

    assert output["outcome"] != "preflight_held"
    assert len(provider.calls) == 1


def test_the_model_never_sees_the_case_id_that_names_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _recovery_provider([(_answer("pr-board-held", GLOBEX_OPEN), [GLOBEX_OPEN])])
    sample = _board_case({"url": GLOBEX_CLOSED, "title": "Data Platform Engineer", "closed": True})

    _recovery_solver(monkeypatch, provider)({"id": sample["id"], "input": sample["input"]})

    assert "pr-board-held" not in repr(provider.calls[0])


# Another title on the board, so preflight leaves the case to the model.
_OTHER_ROLE = {"url": GLOBEX_OPEN, "title": "Analytics Engineer"}
_GLOBEX_JID = (
    "https://careers.globex.example/careers/job/?ashby_jid=00000000-0000-4000-8000-000000000011"
)


@pytest.mark.parametrize(
    ("answer", "cited"),
    [
        (_GLOBEX_JID, [_GLOBEX_JID]),
        (GLOBEX_OPEN, ["https://listings.example.com/view/other"]),
    ],
)
def test_a_careers_job_id_or_uncited_stored_record_scores_the_board_url(
    monkeypatch: pytest.MonkeyPatch, answer: str, cited: list[str]
) -> None:
    provider = _recovery_provider([(_answer("pr-board-held", answer), cited)])
    sample = _board_case(_OTHER_ROLE)

    output = _recovery_solver(monkeypatch, provider)({"id": sample["id"], "input": sample["input"]})
    score = recovered_posting()(sample, output)

    assert output["picked"] == GLOBEX_OPEN
    assert score["value"] is True
    assert score["metadata"]["matched"] == "url"


def test_a_case_naming_the_employers_board_sends_it_as_production_would(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _recovery_provider(
        [(_answer("pr-ats-tenant-render", CONTOSO_CAREERS), [CONTOSO_CAREERS])]
    )
    sample = _case(recovery_task.DATASET_PATH, "pr-ats-tenant-render")
    board = "https://jobs.ashbyhq.com/northwind"

    _recovery_solver(monkeypatch, provider)(
        {"id": sample["id"], "input": {**sample["input"], "registered_board_url": board}}
    )

    sent = provider.calls[0][1].task.text
    assert f'"registered_board_url":"{board}"' in sent
    assert '"known_publisher_domain"' not in sent


def test_another_url_on_an_accepted_host_passes_as_a_host_only_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    score, _ = _recovery_score(
        monkeypatch,
        "pr-url-or-ats-host",
        _answer("pr-url-or-ats-host", ASHBY_OTHER, authority="ats"),
        [ASHBY_OTHER],
    )

    assert score["value"] is True
    assert score["metadata"]["matched"] == "host"
    assert score["checks"] == {"failed": [], "warned": ["host_only"]}


def test_a_host_only_match_misses_a_case_that_names_only_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    elsewhere = "https://careers.contoso.example/jobs/another-role"
    score, _ = _recovery_score(
        monkeypatch,
        "pr-ats-tenant-render",
        _answer("pr-ats-tenant-render", elsewhere),
        [elsewhere],
    )

    assert score["value"] is False
    assert score["metadata"]["matched"] == "host"
    assert score["checks"]["failed"] == ["miss:candidate_ready"]


def test_an_uncited_candidate_is_refused_as_production_refuses_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    score, _ = _recovery_score(
        monkeypatch,
        "pr-aggregator-unread",
        _answer("pr-aggregator-unread", FABRIKAM_CAREERS),
        ["https://listings.example.com/view/other"],
    )

    assert score["value"] is False
    assert score["metadata"]["outcome"] == "response_refused"
    assert score["metadata"]["candidates"] == []


def test_a_refused_recovery_answer_is_billed_as_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _recovery_provider(
        [(_answer("pr-aggregator-unread", FABRIKAM_CAREERS), ["https://listings.example.com/x"])]
    )
    solver = _recovery_solver(monkeypatch, provider)
    sample = _case(recovery_task.DATASET_PATH, "pr-aggregator-unread")

    output = solver({"id": sample["id"], "input": sample["input"]})

    assert output["outcome"] == "response_refused"
    assert [entry.validated for entry in solver.ledger.entries] == [False]


def test_a_transient_provider_failure_stops_the_recovery_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(_schema: type[BaseModel]) -> dict[str, Any]:
        raise ProviderError("503 Service Unavailable")

    provider = ScriptedProvider({"PostingRecoveryResponse": unavailable})
    solver = _recovery_solver(monkeypatch, provider)
    sample = _case(recovery_task.DATASET_PATH, "pr-withdrawn")

    with pytest.raises(ProviderError, match="503"):
        solver({"id": sample["id"], "input": sample["input"]})


def test_a_withdrawn_posting_passes_only_with_no_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty, _ = _recovery_score(
        monkeypatch, "pr-withdrawn", _answer("pr-withdrawn"), ["https://listings.example.com/x"]
    )
    copied = "https://reposts.example.org/settlement-lead"
    cited, _ = _recovery_score(
        monkeypatch,
        "pr-withdrawn",
        _answer("pr-withdrawn", copied, authority="aggregator"),
        [copied],
    )

    assert empty["value"] is True
    assert empty["metadata"]["outcome"] == "not_found"
    assert cited["value"] is False
    assert cited["checks"]["failed"] == ["unexpected_candidate"]


def _live_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, limit: int) -> None:
    monkeypatch.setenv("CLEARSCOUT_EVAL_LIVE_APPROVED_CENTS", "100")
    monkeypatch.setenv("CLEARSCOUT_EVAL_LIVE_PROVIDER", "openai")
    monkeypatch.setenv("CLEARSCOUT_EVAL_DATASET_SPLIT", "development")
    monkeypatch.setenv("CLEARSCOUT_EVAL_SAMPLE_LIMIT", str(limit))
    monkeypatch.setenv("CLEARSCOUT_EVAL_OUTPUT_DIR", str(tmp_path))
    monkeypatch.delenv(clearscout_live.PROMPT_ARMS_ENV, raising=False)
    monkeypatch.setattr(web_stage_live, "billing_target", lambda: (None, "ws"))


def _header(log: Path) -> dict[str, Any]:
    header: dict[str, Any] = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    return header


def test_a_recovery_run_header_counts_url_hits_host_hits_and_misses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _live_environment(monkeypatch, tmp_path, 4)
    _wire(
        monkeypatch,
        _recovery_provider(
            [
                (_answer("pr-ats-tenant-render", CONTOSO_CAREERS), [CONTOSO_CAREERS]),
                (_answer("pr-aggregator-unread"), ["https://listings.example.com/a"]),
                (_answer("pr-url-or-ats-host", ASHBY_OTHER, authority="ats"), [ASHBY_OTHER]),
                (_answer("pr-withdrawn"), ["https://listings.example.com/b"]),
            ]
        ),
    )

    log = runner.run_task(
        str(Path(recovery_task.__file__)), log_dir=tmp_path, limit=4, split="development"
    )

    header = _header(log)
    assert (header["total"], header["passed"]) == (4, 3)
    assert header["failed_checks"] == {"miss:not_found": 1}
    assert header["warned_checks"] == {"host_only": 1}


def test_a_live_run_given_a_dataset_reads_and_records_that_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    private = tmp_path / "private" / "cases.jsonl"
    private.parent.mkdir()
    lines = recovery_task.DATASET_PATH.read_text(encoding="utf-8").splitlines()
    private.write_text(
        next(line for line in lines if '"pr-withdrawn"' in line) + "\n", encoding="utf-8"
    )
    _live_environment(monkeypatch, tmp_path, 1)
    _wire(monkeypatch, _recovery_provider([(_answer("pr-withdrawn"), ["https://x.example/b"])]))

    log = runner.run_task(
        str(Path(recovery_task.__file__)),
        log_dir=tmp_path,
        dataset_path=private,
        limit=1,
        split="development",
    )

    header = _header(log)
    assert header["dataset"] == str(private)
    assert header["run"]["dataset_sha256"] == hashlib.sha256(private.read_bytes()).hexdigest()
    assert (header["total"], header["passed"]) == (1, 1)


def _campaign_poll(
    request: web_stage_live.CampaignRequest,
    queries: list[str],
    *,
    postings: list[str] = (),  # type: ignore[assignment]
    attributed: bool = True,
    sources: tuple[str, ...] = (),
) -> BackgroundPoll:
    keys = list(request.attempt.branch_keys)
    value: BaseModel = request.schema.model_validate(
        {
            "postings": [{"url": url, "branch_keys": keys[:1], "why": "Fits."} for url in postings],
            "boards": [],
            "searches": [
                {"query": query, "branch_keys": keys if attributed else []} for query in queries
            ],
        }
    )
    found = tuple(SearchSource(url=url) for url in ("https://example.com/s", *sources))
    record = CallRecord(
        usage=Usage(input_tokens=100, output_tokens=50),
        model="gpt-6.1-sol",
        response_id="resp_scripted_0",
        searches=tuple(Search(queries=(query,), sources=found) for query in queries),
    )
    return BackgroundPoll(status="complete", value=value, record=record)


class _PostingsByUrl:
    """Answer each scripted posting URL's read and refuse any other."""

    def __init__(self, postings: dict[str, FetchedPosting]) -> None:
        self.postings = postings

    def fetch(
        self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL, board_url: str = ""
    ) -> FetchedPosting:
        return self.postings[url]


@contextmanager
def _pick_scans(postings: dict[str, FetchedPosting] | None = None) -> Iterator[SourceScanService]:
    """Admission's read seam over scripted posting reads and empty boards.

    The read phase opens no database.
    """
    yield SourceScanService(
        None,  # type: ignore[arg-type]
        storage=RuntimeStorage.resolve("sqlite+pysqlite:////synthetic/eval-picks.sqlite3"),
        posting_fetcher=web_stage_live.RecordedPostings(_PostingsByUrl(postings or {})),
        fetcher=ScriptedBoards({"": []}),
    )


def _campaign_request(case_id: str) -> tuple[dict[str, Any], web_stage_live.CampaignRequest]:
    sample = _case(campaign_task.DATASET_PATH, case_id)
    return sample, web_stage_live.campaign_request(sample, Settings(provider=ProviderName.OPENAI))


def _campaign_score(
    monkeypatch: pytest.MonkeyPatch,
    sample: dict[str, Any],
    poll: BackgroundPoll,
    postings: dict[str, FetchedPosting] | None = None,
) -> tuple[dict[str, Any], ScriptedProvider]:
    provider = ScriptedProvider({}, background_answers=[poll])
    _wire(monkeypatch, provider)
    monkeypatch.delenv(clearscout_live.PROMPT_ARMS_ENV, raising=False)
    solver = web_stage_live._GuidedCampaignLiveSolver(
        run_metadata={"provider": "openai"},
        prompt_snapshot=web_stage_live.working_prompts(web_stage_live.CAMPAIGN_PROMPTS),
        provider_name=ProviderName.OPENAI,
        settings=Settings(provider=ProviderName.OPENAI),
        ledger=Ledger(),
        database=None,  # type: ignore[arg-type]
        workspace_id="ws",
        open_scans=partial(_pick_scans, postings),
    )
    output = solver({"id": sample["id"], "input": sample["input"]})
    score: dict[str, Any] = campaign_attempt()(sample, output)
    return score, provider


def _floor_queries(request: web_stage_live.CampaignRequest) -> list[str]:
    by_key = {branch.key: branch for branch in request.manifest.branches}
    return [by_key[key].floor_query for key in request.attempt.branch_keys]


def test_an_attempt_serving_every_branch_within_its_cap_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample, request = _campaign_request("gc-two-titles")
    poll = _campaign_poll(
        request,
        _floor_queries(request),
        postings=["https://boards.greenhouse.io/acme/jobs/4100002"],
    )

    score, provider = _campaign_score(monkeypatch, sample, poll)

    assert score["value"] is True, score["checks"]
    assert score["requirements"] == {"total": 2, "passed": 2}
    assert score["metadata"]["search_count"] == 2
    assert score["metadata"]["picks"][0]["classification"] == "ats_detail"
    # The solver sent the production attempt request with its own search cap.
    sent = provider.background_dispatch_calls[0]
    assert sent.search is not None and sent.search.max_calls == request.attempt.search_cap
    assert sent.task.text == request.plan.task.text


def test_an_unserved_branch_an_excluded_title_and_a_refused_pick_each_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample, request = _campaign_request("gc-two-titles")
    first = _floor_queries(request)[0]
    poll = _campaign_poll(
        request,
        [first, "Senior Sales Engineer payments Calgary"],
        postings=["http://127.0.0.1/jobs/1"],
    )

    score, _ = _campaign_score(monkeypatch, sample, poll)

    assert score["value"] is False
    assert sorted(score["checks"]["failed"]) == [
        "branch_uncredited",
        "excluded_term",
        "refused_pick",
    ]
    assert score["metadata"]["refused_picks"][0]["reason"] == "not_a_hostname"


def test_an_excluded_employer_named_in_a_query_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    sample, request = _campaign_request("gc-two-titles")
    queries = [f"{query} Northbridge" for query in _floor_queries(request)]

    score, _ = _campaign_score(monkeypatch, sample, _campaign_poll(request, queries))

    assert score["checks"]["failed"] == ["excluded_term", "excluded_term"]


def test_searches_past_the_attempt_cap_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    sample, request = _campaign_request("gc-two-titles")
    queries = _floor_queries(request) * request.attempt.search_cap

    score, _ = _campaign_score(monkeypatch, sample, _campaign_poll(request, queries))

    assert score["checks"]["failed"] == ["search_count_out_of_bounds"]


def test_a_focus_bundle_is_credited_only_by_a_query_the_answer_attributes_to_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample, request = _campaign_request("gc-title-and-focus")
    queries = [*(query for query in _floor_queries(request) if query), "payment reconciliation"]

    credited, _ = _campaign_score(monkeypatch, sample, _campaign_poll(request, queries))
    unattributed, _ = _campaign_score(
        monkeypatch, sample, _campaign_poll(request, queries, attributed=False)
    )

    assert credited["value"] is True, credited["checks"]
    assert unattributed["metadata"]["uncredited_branches"] == ["payment reconciliation"]


def _recovery_output(outcome: str, picked: str | None, *cited: str) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "picked": picked,
        "candidates": [{"url": url} for url in cited],
    }


@pytest.mark.parametrize(
    "output",
    [
        _recovery_output("ambiguous", None, CONTOSO_CAREERS, FABRIKAM_CAREERS),
        _recovery_output("source_found_unreadable", CONTOSO_CAREERS, CONTOSO_CAREERS),
    ],
    ids=["ambiguous", "source_found_unreadable"],
)
def test_a_recovery_that_reads_nothing_fails_even_when_it_cites_the_expected_url(
    output: dict[str, Any],
) -> None:
    sample = _case(recovery_task.DATASET_PATH, "pr-ats-tenant-render")

    score = recovered_posting()(sample, output)

    assert score["value"] is False
    assert score["checks"]["failed"] == [f"miss:{output['outcome']}"]
    assert score["checks"]["warned"] == ["expected_cited_not_picked"]


def test_a_ready_recovery_that_picked_another_url_fails_with_the_cited_one_warned() -> None:
    sample = _case(recovery_task.DATASET_PATH, "pr-ats-tenant-render")
    other = "https://listings.example.com/view/data-platform"
    output = _recovery_output("candidate_ready", other, other, CONTOSO_CAREERS)

    score = recovered_posting()(sample, output)

    assert score["value"] is False
    assert score["checks"]["warned"] == ["expected_cited_not_picked"]


def _campaign_solver(
    monkeypatch: pytest.MonkeyPatch, provider: ScriptedProvider
) -> web_stage_live._GuidedCampaignLiveSolver:
    _wire(monkeypatch, provider, sleeper=lambda _seconds: None)
    monkeypatch.delenv(clearscout_live.PROMPT_ARMS_ENV, raising=False)
    return web_stage_live._GuidedCampaignLiveSolver(
        run_metadata={"provider": "openai"},
        prompt_snapshot=web_stage_live.working_prompts(web_stage_live.CAMPAIGN_PROMPTS),
        provider_name=ProviderName.OPENAI,
        settings=Settings(provider=ProviderName.OPENAI),
        ledger=Ledger(),
        database=None,  # type: ignore[arg-type]
        workspace_id="ws",
        open_scans=_pick_scans,
    )


def test_a_campaign_attempt_that_never_finishes_is_cancelled_and_named_for_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample, _ = _campaign_request("gc-two-titles")
    provider = ScriptedProvider({}, background_answers=[[BackgroundPoll(status="pending")]])

    output = _campaign_solver(monkeypatch, provider)({"id": sample["id"], "input": sample["input"]})

    assert provider.background_cancelled == ["resp_scripted_0"]
    assert output["unbilled_response_id"] == "resp_scripted_0"
    assert campaign_attempt()(sample, output)["value"] is False


def test_a_campaign_poll_that_raises_is_cancelled_even_when_the_cancel_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample, _ = _campaign_request("gc-two-titles")
    provider = ScriptedProvider({}, background_answers=[[BackgroundPoll(status="pending")]])

    def broken_poll(*_args: Any, **_kwargs: Any) -> BackgroundPoll:
        raise ProviderError("connection reset")

    def refused_cancel(response_id: str) -> None:
        provider.background_cancelled.append(response_id)
        raise ProviderError("already completed")

    monkeypatch.setattr(provider, "poll_background", broken_poll)
    monkeypatch.setattr(provider, "cancel_background", refused_cancel)

    output = _campaign_solver(monkeypatch, provider)({"id": sample["id"], "input": sample["input"]})

    assert provider.background_cancelled == ["resp_scripted_0"]
    assert output["unbilled_response_id"] == "resp_scripted_0"
    assert "connection reset" in output["error"]


SRE_OPEN = "https://boards.greenhouse.io/acme/jobs/4100101"
SRE_ROLE = "Site Reliability Engineer"


def _read(
    url: str,
    outcome: str,
    *,
    title: str = "",
    employer: str = "",
    detail: str = "",
    findings: tuple[tuple[str, str], ...] = (),
    role_aliases: tuple[str, ...] = (),
    publisher_vouched: bool = True,
    posting_status: str = "active",
) -> dict[str, Any]:
    return {
        "url": url,
        "classification": "ats_detail",
        "outcome": outcome,
        "detail": detail,
        "staged_basis": "title_evidence" if outcome == "staged" else None,
        "title": title,
        "employer": employer,
        "findings": [{"dimension": dim, "code": code, "subject": ""} for dim, code in findings],
        "board_root": "",
        "board_listings": [],
        "role_aliases": list(role_aliases),
        "focus_terms": [],
        "publisher_vouched": publisher_vouched,
        "posting_status": posting_status,
    }


def _graded(
    reads: list[dict[str, Any]],
    *,
    boards: tuple[str, ...] = (),
    why: str = "Reliability ownership for a payments platform.",
    case: str = "gc-excluded-employers",
) -> dict[str, Any]:
    """Score one finished single-branch attempt whose picks were read."""
    sample, request = _campaign_request(case)
    [key] = request.attempt.branch_keys
    branch = next(item for item in request.manifest.branches if item.key == key)
    boards_picked = set(boards)
    output = {
        "status": "complete",
        "search_count": 1,
        "search_cap": request.attempt.search_cap,
        "queries": [branch.floor_query],
        "branches": [
            {
                "key": key,
                "label": branch.label,
                "titles": list(branch.title_aliases),
                "queries": [branch.floor_query],
            }
        ],
        "postings": [
            {"url": read["url"], "branch_keys": [key], "why": why}
            for read in reads
            if read["url"] not in boards_picked
        ],
        "boards": [{"url": url, "branch_keys": [key], "why": why} for url in boards],
        "refused_picks": [],
        "reads": reads,
    }
    score: dict[str, Any] = campaign_attempt()(sample, output)
    return score


def test_a_pick_admission_stages_on_its_branch_title_passes_every_pick_measure() -> None:
    score = _graded([_read(SRE_OPEN, "staged", title=SRE_ROLE, employer="Acme")])

    assert score["value"] is True, score["checks"]
    assert score["checks"]["warned"] == []
    assert score["metadata"]["pick_quality"]["rates"] == {
        "admitted": {"passed": 1, "total": 1},
        "grounded": {"passed": 1, "total": 1},
        "novel": {"passed": 1, "total": 1},
        "open_posting": {"passed": 1, "total": 1},
        "board_proven": {"passed": 0, "total": 0},
        "title_fit": {"passed": 1, "total": 1},
        "place_fit": {"passed": 1, "total": 1},
    }
    assert score["metadata"]["pick_quality"]["flagged"] == []


@pytest.mark.parametrize(
    ("read", "failed", "warned"),
    [
        (
            _read(SRE_OPEN, "screened_out", title=SRE_ROLE, employer="Northwind"),
            ["excluded_employer_pick"],
            [],
        ),
        (
            _read(SRE_OPEN, "screened_out", title="Site Reliability Intern", employer="Acme"),
            ["excluded_title_pick"],
            ["title_misfit"],
        ),
        (
            _read(SRE_OPEN, "screened_out", title="Payroll Specialist", employer="Acme"),
            [],
            ["title_misfit"],
        ),
        (
            _read(
                SRE_OPEN,
                "held_out",
                title=SRE_ROLE,
                employer="Acme",
                findings=(("location", "location_outside"),),
            ),
            [],
            ["place_misfit"],
        ),
        (
            _read(
                SRE_OPEN,
                "screened_out",
                title=SRE_ROLE,
                employer="Acme",
                findings=(("location", "location_ineligible"),),
            ),
            [],
            ["place_misfit"],
        ),
        (
            _read(
                SRE_OPEN,
                "held_out",
                title=SRE_ROLE,
                employer="Acme",
                findings=(("arrangement", "arrangement_remote_only_sites"),),
            ),
            [],
            ["place_misfit"],
        ),
        (
            _read(SRE_OPEN, "held_out", title=SRE_ROLE, employer="Acme", detail="certification"),
            [],
            ["held_out_pick"],
        ),
        (_read(SRE_OPEN, "unreadable", posting_status="closed"), [], ["closed_pick"]),
        (_read(SRE_OPEN, "unresolved", detail="page is not a single posting"), [], ["unread_pick"]),
        (_read(SRE_OPEN, "unresolved", detail=NOT_CAPTURED_DETAIL), [], ["ungrounded_pick"]),
    ],
    ids=[
        "excluded-employer",
        "excluded-title",
        "title-misfit",
        "place-misfit",
        "place-ineligible",
        "office-only-at-remote-only-sites",
        "held-out",
        "closed",
        "not-one-posting",
        "never-a-search-result",
    ],
)
def test_a_pick_admission_would_not_stage_fails_or_warns_by_its_reason(
    read: dict[str, Any], failed: list[str], warned: list[str]
) -> None:
    score = _graded([read])

    assert score["checks"] == {"failed": failed, "warned": warned}
    assert score["value"] is (not failed)
    assert score["metadata"]["pick_quality"]["rates"]["admitted"] == {"passed": 0, "total": 1}
    [flagged] = score["metadata"]["pick_quality"]["flagged"]
    assert flagged["url"] == SRE_OPEN


def test_a_second_pick_of_one_posting_fails_as_a_duplicate() -> None:
    variant = "https://job-boards.greenhouse.io/acme/jobs/4100101?gh_src=search"
    score = _graded(
        [
            _read(SRE_OPEN, "staged", title=SRE_ROLE, employer="Acme"),
            _read(variant, "staged", title=SRE_ROLE, employer="Acme"),
        ]
    )

    assert score["checks"]["failed"] == ["duplicate_pick"]
    assert score["metadata"]["pick_quality"]["flagged"][0]["url"] == variant
    assert score["metadata"]["pick_quality"]["rates"]["admitted"] == {"passed": 1, "total": 2}


def test_a_board_pick_counts_only_when_admission_stages_one_of_its_listings() -> None:
    board = "https://jobs.ashbyhq.com/acme"
    proven = _read(board, "board_admitted", employer="Acme")
    unproven = _read("https://boards.greenhouse.io/acme", "unresolved", detail="none staged")

    score = _graded([proven, unproven], boards=(board, unproven["url"]))

    assert score["checks"]["warned"] == ["board_unproven"]
    assert score["metadata"]["pick_quality"]["rates"]["board_proven"] == {"passed": 1, "total": 2}


def _job(number: int) -> str:
    return f"https://boards.greenhouse.io/acme/jobs/{4500000 + number}"


def test_title_fit_rates_matches_against_mismatches_and_lists_undecided_titles_apart() -> None:
    titles = (
        "Senior Data Engineer",
        "Sr. Data Engineer",
        "Senior Data Engineer, Platform",
        "Data Engineer III",
        "Senior Analytics Engineer",
    )
    reads = [
        _read(_job(number), "staged", title=title, employer="Acme")
        for number, title in enumerate(titles)
    ]

    score = _graded(reads, case="gc-senior-ic-remote-us")

    assert score["checks"] == {"failed": [], "warned": ["title_misfit"]}
    quality = score["metadata"]["pick_quality"]
    assert quality["rates"]["title_fit"] == {"passed": 1, "total": 2}
    assert quality["title_undecided"] == [_job(1), _job(2), _job(3)]


def test_a_title_the_publisher_lists_under_a_searched_alias_fits() -> None:
    read = _read(
        _job(0),
        "staged",
        title="Data Engineer III",
        employer="Acme",
        role_aliases=("Senior Data Engineer",),
    )

    score = _graded([read], case="gc-senior-ic-remote-us")

    assert score["metadata"]["pick_quality"]["rates"]["title_fit"] == {"passed": 1, "total": 1}
    assert score["metadata"]["pick_quality"]["title_undecided"] == []


def test_an_excluded_title_no_publisher_vouched_for_is_undecided_and_never_fails() -> None:
    read = _read(
        SRE_OPEN,
        "screened_out",
        title="Site Reliability Intern",
        employer="Acme",
        publisher_vouched=False,
    )

    score = _graded([read])

    assert score["checks"] == {"failed": [], "warned": []}
    assert score["metadata"]["pick_quality"]["title_undecided"] == [SRE_OPEN]


def test_a_pick_no_search_returned_counts_against_the_grounded_rate() -> None:
    reads = [
        _read(SRE_OPEN, "staged", title=SRE_ROLE, employer="Acme"),
        _read(_job(1), "unresolved", detail=NOT_CAPTURED_DETAIL),
    ]

    score = _graded(reads)

    assert score["metadata"]["pick_quality"]["rates"]["grounded"] == {"passed": 1, "total": 2}


UNSAFE_READ = "posting could not be read safely"


@pytest.mark.parametrize(
    ("first_status", "warned"),
    [
        ("", ["unread_pick", "unread_pick", "pick_reads_failed"]),
        ("closed", ["closed_pick", "unread_pick"]),
    ],
    ids=["every-read-failed", "one-read-found-a-closed-posting"],
)
def test_picks_that_all_come_back_unreadable_warn_that_the_reader_failed(
    first_status: str, warned: list[str]
) -> None:
    reads = [
        _read(SRE_OPEN, "unreadable", detail=UNSAFE_READ, posting_status=first_status),
        _read(_job(1), "unreadable", detail=UNSAFE_READ, posting_status=""),
    ]

    score = _graded(reads)

    assert score["checks"] == {"failed": [], "warned": warned}


def test_an_output_recorded_before_picks_were_read_scores_only_the_attempt_checks() -> None:
    sample, request = _campaign_request("gc-two-titles")
    poll = _campaign_poll(request, _floor_queries(request), postings=[SRE_OPEN])
    output = web_stage_live.campaign_result(request, poll)

    score = campaign_attempt()(sample, output)

    assert score["value"] is True, score["checks"]
    assert score["metadata"]["pick_quality"] is None


def _open_posting(
    title: str, place: str, *, employer: str = "Acme", requirements: str = ""
) -> FetchedPosting:
    facts = StagedListingFacts(locations=PublishedFact(values=(place,), complete=True))
    return FetchedPosting(
        text=f"{employer} is hiring a {title} in {place}.{requirements}",
        posting_status="active",
        method="test",
        fetched_at=utc_now(),
        title=title,
        employer=employer,
        publisher_check=PostingCheck(
            status="readable", note="", title=title, employer=employer, facts=facts
        ),
        page_classification=PageClassification.POSTING,
    )


def test_the_solver_records_admissions_read_of_each_pick_a_search_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample, request = _campaign_request("gc-two-titles")
    home = "https://boards.greenhouse.io/acme/jobs/4200001"
    california = "https://boards.greenhouse.io/acme/jobs/4200002"
    remote_us = "https://boards.greenhouse.io/acme/jobs/4200003"
    closed = "https://boards.greenhouse.io/acme/jobs/4200004"
    unsearched = "https://boards.greenhouse.io/acme/jobs/4200005"
    title = "Payments Platform Engineer"
    postings = {
        home: _open_posting(title, "Calgary, Alberta, Canada"),
        california: _open_posting(title, "San Jose, CA"),
        remote_us: _open_posting(title, "Remote - US"),
        closed: FetchedPosting(
            text=None, posting_status="closed", method="closure_signal", fetched_at=utc_now()
        ),
    }
    poll = _campaign_poll(
        request,
        _floor_queries(request),
        postings=[home, california, remote_us, closed, unsearched],
        sources=(home, california, remote_us, closed),
    )

    score, _ = _campaign_score(monkeypatch, sample, poll, postings)

    outcomes = {
        grade["url"]: (grade["outcome"], grade["warned"])
        for grade in score["metadata"]["pick_quality"]["flagged"]
    }
    assert outcomes == {
        california: ("screened_out", ["place_misfit"]),
        remote_us: ("screened_out", ["place_misfit"]),
        closed: ("unreadable", ["closed_pick"]),
        unsearched: ("unresolved", ["ungrounded_pick"]),
    }
    assert score["metadata"]["pick_quality"]["rates"]["admitted"] == {"passed": 1, "total": 5}


def test_a_pick_read_that_raises_keeps_the_paid_attempt_and_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample, request = _campaign_request("gc-two-titles")
    unscripted = "https://boards.greenhouse.io/acme/jobs/4400001"
    poll = _campaign_poll(
        request, _floor_queries(request), postings=[unscripted], sources=(unscripted,)
    )

    score, _ = _campaign_score(monkeypatch, sample, poll)

    assert score["value"] is True, score["checks"]
    assert score["checks"]["warned"] == ["pick_reads_failed"]
    assert score["metadata"]["pick_quality"] is None


def test_a_pick_the_case_already_holds_is_read_as_held_and_counted_not_novel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample, request = _campaign_request("gc-two-titles")
    known = "https://boards.greenhouse.io/acme/jobs/4300001"
    on_registered_board = "https://jobs.ashbyhq.com/acme/00000000-0000-4000-8000-000000000031"
    planted = {
        **sample,
        "input": {
            **sample["input"],
            "known_urls": [f"{known}?gh_src=board"],
            "registered_boards": ["https://jobs.ashbyhq.com/acme"],
        },
    }
    poll = _campaign_poll(
        request,
        _floor_queries(request),
        postings=[known, on_registered_board],
        sources=(known, on_registered_board),
    )

    # No posting is scripted, so a pick admission tried to read would raise here.
    score, _ = _campaign_score(monkeypatch, planted, poll)

    assert score["checks"]["warned"] == []
    assert [grade["outcome"] for grade in score["metadata"]["pick_quality"]["flagged"]] == []
    assert score["metadata"]["pick_quality"]["rates"]["novel"] == {"passed": 0, "total": 2}


def test_the_solver_holds_out_a_pick_whose_must_haves_require_the_gated_certification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample, request = _campaign_request("gc-certification-gate")
    gated = "https://boards.greenhouse.io/acme/jobs/4600001"
    postings = {
        gated: _open_posting(
            "Cloud Security Engineer",
            "Denver, Colorado, United States",
            requirements="\nRequirements:\n- Active CISSP certification is required.",
        )
    }
    poll = _campaign_poll(request, _floor_queries(request), postings=[gated], sources=(gated,))

    score, _ = _campaign_score(monkeypatch, sample, poll, postings)

    [flagged] = score["metadata"]["pick_quality"]["flagged"]
    assert (flagged["outcome"], flagged["warned"]) == ("held_out", ["held_out_pick"])


def test_live_pick_reads_close_the_http_client_they_open(
    monkeypatch: pytest.MonkeyPatch, durable_database: Database
) -> None:
    closed: list[PoliteHttpClient] = []

    def close(client: PoliteHttpClient) -> None:
        closed.append(client)

    monkeypatch.setattr(PoliteHttpClient, "close", close)

    with web_stage_live.live_pick_scans(durable_database) as scans:
        assert isinstance(scans.postings.fetcher, web_stage_live.RecordedPostings)
        assert closed == []

    assert len(closed) == 1
