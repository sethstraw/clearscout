"""Record each model stage's real HTTP traffic so the suite can replay it forever after.

Run this once, with a real API key. Each stage in ``STAGES`` drives the smallest
production entry point that sends its request, against the public synthetic
fixtures under ``tests/cassettes/``. Every request the vendor SDK makes and every
response it gets is written to that stage's cassette, with credentials stripped.
From then on the recorded replay test replays each file through the same SDK
and needs no key, no network, and no money.

    uv run python scripts/record_cassette.py --provider openai
    uv run python scripts/record_cassette.py --provider openai --stage vision --stage structure

A stage writes ``tests/cassettes/<stage>_<provider>.json``, so the tailoring run
is ``run_openai.json``. The replay test reads the stage and the provider back out
of the filename, so renaming a cassette breaks its replay.

Record OpenAI. It is the only provider clearscout has been validated against, and
a cassette is the record of that validation.

Review each cassette before committing it. It contains the model's actual output,
and a web-search stage's also carries the pages the search returned that day.
Never record from a private CV: response bodies can repeat their inputs.

What a recording is bound to, which is more than it looks
---------------------------------------------------------

A replay matches on the method, the URL, and the sha256 of the request body. A
body is built from the fixtures, the prompts, and the code that composes them,
so each of these invalidates the cassettes of the stages that read it:

- `record.toml`, the settings every stage records against;
- the synthetic candidate CV beside it (run, structure, relevance_review);
- `posting.txt` (run) and `posting.png` (vision);
- `stages.json` (relevance_review, search_vocabulary, posting_recovery,
  guided_campaign);
- the eval inputs two stages reuse: the platform engineer CV under `evals/cvs/`
  (search_vocabulary) and the eval support synthetic inventory (guided_campaign);
- any file under `prompts/`, whose text is sent verbatim;
- any change to the code that builds a request, a wire schema, or the cached
  prefix, including uncommitted work in the tree being recorded;
- for the run, the date, since evidence selection weighs how long ago each role ended.

A fixture or prompt change therefore has to be paid for again in real API calls.
Re-record deliberately rather than editing a cassette by hand. A web-search
stage's replay reads back what was recorded and never searches again.

These are public-repository files first. They describe an invented person, and a
recording made from anything else does not belong here whatever it replays.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tests.support.cassette import Cassette, recording_client

from clearscout.application.config import ProviderName, Settings, Stage, load_settings
from clearscout.delivery.cli.console import Terminal
from clearscout.infrastructure.providers.cache import ResponseCache
from clearscout.infrastructure.providers.client import Client
from clearscout.infrastructure.providers.credentials import resolve
from clearscout.shared.costs import Ledger
from clearscout.shared.costs.prices import ledger_for

CASSETTES = Path(__file__).resolve().parent.parent / "tests" / "cassettes"
CASSETTE_CONFIG = CASSETTES / "record.toml"
CASSETTE_CV = CASSETTES / "master_cv.md"
CASSETTE_POSTING = CASSETTES / "posting.txt"
CASSETTE_SCREENSHOT = CASSETTES / "posting.png"
STAGE_INPUTS = CASSETTES / "stages.json"
# The cover letter cites the posting by its origin, which for a file is a local path.
PUBLISHER_URL = "https://jobs.example.com/postings/reference-data-product-owner"
CAMPAIGN_ORIGIN = "cassette.guided_campaign"


def recording_provider(cassette: Cassette) -> Any:
    """Return a stand-in for ``provider_for`` that records what it sends.

    Module level and typed so a test can check its signature against the real
    function, rather than discovering a drift halfway through a paid recording.
    """

    # Recording uses the client's default timeout.
    def build(name: ProviderName, key: str, timeout: float | None = None) -> object:  # noqa: ARG001
        http = recording_client(cassette)
        if name is ProviderName.ANTHROPIC:
            from anthropic import Anthropic

            from clearscout.infrastructure.providers.anthropic import AnthropicProvider

            return AnthropicProvider(api_key=key, client=Anthropic(api_key=key, http_client=http))
        from openai import OpenAI

        from clearscout.infrastructure.providers.openai import OpenAIProvider

        return OpenAIProvider(api_key=key, client=OpenAI(api_key=key, http_client=http))

    return build


@dataclass
class StageRun:
    """What one stage's recording or replay drives: a provider, its settings, and scratch space."""

    provider: Any
    settings: Settings
    workdir: Path
    api_key: str
    sleeper: Callable[[float], None] = time.sleep
    ledger: Ledger = field(default_factory=Ledger)

    def client(self) -> Client:
        """Return an uncached standard-tier client, so a body never depends on a flex resend."""
        return Client(
            provider=self.provider,
            settings=self.settings,
            cache=ResponseCache(enabled=False),
            ledger=self.ledger,
            sleeper=self.sleeper,
        )


def stage_run(
    provider: Any,
    name: ProviderName,
    workdir: Path,
    *,
    api_key: str,
    sleeper: Callable[[float], None] = time.sleep,
) -> StageRun:
    """Return the run one stage drives, with the recording settings loaded."""
    settings = load_settings(CASSETTE_CONFIG, overrides={"provider": name, "output_dir": workdir})
    return StageRun(provider, settings, workdir, api_key, sleeper, ledger_for(settings.prices))


def cassette_path(stage: str, provider: ProviderName, directory: Path = CASSETTES) -> Path:
    """Return the file one stage's recording for one provider lives in."""
    return directory / f"{stage}_{provider.value}.json"


@contextlib.contextmanager
def serving(provider: Any) -> Iterator[None]:
    """Hand ``provider`` to the pipeline entry points that build their own client."""
    from clearscout.application.tailoring.pipeline import service

    # Same signature as provider_for; the pipeline package does not re-export it.
    def chosen(name: ProviderName, key: str, timeout: float | None = None) -> object:  # noqa: ARG001
        return provider

    original = service.provider_for
    service.provider_for = chosen  # type: ignore[assignment]
    try:
        yield
    finally:
        service.provider_for = original


def stage_inputs(stage: str) -> dict[str, Any]:
    """Return the synthetic inputs ``stages.json`` holds for one stage."""
    inputs: dict[str, Any] = json.loads(STAGE_INPUTS.read_text(encoding="utf-8"))[stage]
    return inputs


def run_stage(run: StageRun) -> Any:
    """Tailor the posting with the critic forced and a cover letter asked for."""
    from clearscout.application.tailoring import pipeline

    with serving(run.provider):
        return pipeline.run(
            pipeline.RunCommand(
                source=str(CASSETTE_POSTING),
                settings=run.settings,
                api_key=run.api_key,
                want_cover_letter=True,
                publisher_url=PUBLISHER_URL,
                cache_enabled=False,
                force=True,
                ledger=run.ledger,
            )
        )


def vision_stage(run: StageRun) -> Any:
    """Read the posting screenshot through the vision reader a run uses."""
    from clearscout.application.tailoring.pipeline.service import vision_reader
    from clearscout.infrastructure.web_fetch.ingest.image import from_image

    return from_image(CASSETTE_SCREENSHOT, vision_reader(run.client()))


def structure_stage(run: StageRun) -> Any:
    """Import the synthetic CV the way ``clearscout import`` does."""
    from clearscout.application.tailoring.pipeline import service

    with serving(run.provider):
        result = service.import_cv(
            CASSETTE_CV,
            run.settings,
            destination=run.workdir / "imported.md",
            api_key=run.api_key,
            cache_enabled=False,
        )
    run.ledger.entries.extend(result.ledger.entries)
    return result


def relevance_review_stage(run: StageRun) -> Any:
    """Review one batch of synthetic postings and validate it as a review task does."""
    from evals.support.relevance import relevance_batch

    from clearscout.application.discovery.relevance_review import review_plan
    from clearscout.application.discovery.search_parameter_summary import search_summary
    from clearscout.application.job_search.models import RelevanceReviewBatchResponse
    from clearscout.application.master_cv.cv import parse_cv
    from clearscout.domain.search_parameters import JobSearchParameters

    inputs = stage_inputs("relevance_review")
    parameters = JobSearchParameters.model_validate(inputs["parameters"])
    case = relevance_batch(
        "cassette", search_summary(parameters, ()), inputs["postings"], parse_cv(CASSETTE_CV)
    )
    plan = review_plan(
        run.settings,
        case.inventory,
        case.search_context,
        case.posting_block,
        person_notes=case.person_notes,
    )
    raw = run.client().complete(plan, case.schema)
    response = RelevanceReviewBatchResponse.model_validate(raw.model_dump(mode="json"))
    valid, failures = case.validate(response)
    return case, valid, failures


def search_vocabulary_stage(run: StageRun) -> Any:
    """Propose search vocabulary the way a fresh workspace's first run does."""
    from evals.inspect.solvers.search_vocabulary_live import propose

    return propose(run.client(), {"id": "cassette", "input": stage_inputs("search_vocabulary")})


def posting_recovery_stage(run: StageRun) -> Any:
    """Send one identity's cited search and settle it without reading a page."""
    from evals.inspect.solvers.web_stage_live import recovery_target

    from clearscout.application.discovery.assisted_content_resolution import (
        cited_recovery_search,
    )

    return cited_recovery_search(recovery_target(stage_inputs("posting_recovery")), run.client())


def guided_campaign_stage(run: StageRun) -> Any:
    """Dispatch one campaign attempt in the background and poll it to a terminal state."""
    from evals.inspect.solvers.web_stage_live import campaign_request

    from clearscout.application.discovery.guided_campaign_runner import poll_until_terminal

    request = campaign_request(stage_inputs("guided_campaign"), run.settings)
    client = run.client()
    choice = run.settings.choice_for_stage(Stage.GUIDED_CAMPAIGN, client.service_tier)
    dispatched = client.dispatch_background(
        request.plan, request.schema, choice, metadata={"origin": CAMPAIGN_ORIGIN}
    )
    started = time.monotonic()
    try:
        poll = poll_until_terminal(
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
    except BaseException:
        # An attempt left running may still bill, so a failed poll stops it.
        with contextlib.suppress(Exception):
            run.provider.cancel_background(dispatched.response_id)
        raise
    return request, poll


# What each recorded stage drives. The replay test declares its own checks per
# name, and a test holds the two lists equal.
STAGES: dict[str, Callable[[StageRun], Any]] = {
    "run": run_stage,
    "vision": vision_stage,
    "structure": structure_stage,
    "relevance_review": relevance_review_stage,
    "search_vocabulary": search_vocabulary_stage,
    "posting_recovery": posting_recovery_stage,
    "guided_campaign": guided_campaign_stage,
}


def _record(stage: str, name: ProviderName, key: str, directory: Path) -> bool:
    """Record one stage to its cassette and return whether it finished and recorded anything."""
    cassette = Cassette(path=cassette_path(stage, name, directory))
    provider = recording_provider(cassette)(name, key)
    finished = False
    with tempfile.TemporaryDirectory(prefix=f"clearscout-cassette-{stage}-") as workdir:
        run = stage_run(provider, name, Path(workdir), api_key=key)
        try:
            STAGES[stage](run)
            finished = True
        except Exception as error:  # noqa: BLE001 - one stage's failure must not stop the rest
            print(f"FAIL: {stage} raised {type(error).__name__}: {error}", file=sys.stderr)
        finally:
            cassette.save()
            spend = run.ledger.total_dollars
            billed = "unknown" if spend is None else f"${spend:.4f}"
            calls = Counter(entry.stage for entry in run.ledger.entries)
            print(f"{stage}: {len(cassette.interactions)} interaction(s), {billed}, {dict(calls)}")
    if not cassette.interactions:
        print(f"FAIL: {stage} recorded nothing; the recording client was not called.")
        return False
    return finished


def main() -> int:
    """Record the chosen stages, each to its own cassette."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--provider", type=ProviderName, default=ProviderName.OPENAI)
    parser.add_argument(
        "--stage",
        action="append",
        choices=sorted(STAGES),
        help="Stage to record; repeat for several. Defaults to every stage.",
    )
    parser.add_argument("--out-dir", type=Path, default=CASSETTES, help="Where cassettes go.")
    arguments = parser.parse_args()

    credential = resolve(arguments.provider, prompter=Terminal())
    recorded = [
        _record(stage, arguments.provider, credential.key, arguments.out_dir)
        for stage in arguments.stage or STAGES
    ]
    print("\nReplay offline before committing, and review every cassette: they hold real output.")
    print("  uv run --no-sync pytest tests/delivery/test_recorded.py -m recorded")
    return 0 if all(recorded) else 2


if __name__ == "__main__":
    raise SystemExit(main())
