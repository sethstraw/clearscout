"""Record one real run's HTTP traffic so the suite can replay it forever after.

Run this once, with a real API key, against the public synthetic fixtures used
by the replay test. Every request the vendor SDK makes and every response it
gets is written to a cassette, with credentials stripped. From then on the test
suite replays that file through the same SDK and needs no key, no network, and
no money.

    uv run python scripts/record_cassette.py tests/cassettes/posting.txt \
        --config tests/cassettes/record.toml \
        --out tests/cassettes/run_openai.json --provider openai

Record OpenAI. It is the only provider tailorcv has been validated against, and
a cassette is the record of that validation. The replay test reads the provider
back out of the filename, so renaming a cassette breaks its replay.

Review the cassette before committing it. It contains the model's actual output.
Never record it from a private CV: response bodies can repeat their inputs.

What a recording is bound to, which is more than it looks
---------------------------------------------------------

`tests/support/cassette.py` matches a replay on the method, the URL, and the
sha256 of the request body. The request body is built from the fixtures and the
prompts, so all of these invalidate every cassette recorded before them:

- `tests/cassettes/master_cv.md`, the synthetic candidate;
- `tests/cassettes/record.toml`, the rules and page budget recorded against;
- any file under `prompts/`, whose text is sent verbatim;
- anything that changes how the wire schema or the cached prefix is built.

A prompt or fixture change therefore has to be paid for again in real API calls.
Re-record deliberately rather than editing a cassette by hand.

These are public-repository files first. They describe an invented person, and a
recording made from anything else does not belong here whatever it replays.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tests.support.cassette import Cassette, recording_client

from tailorcv.application.config import ProviderName, load_settings
from tailorcv.delivery.cli.console import Terminal
from tailorcv.infrastructure.providers.credentials import resolve


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

            from tailorcv.infrastructure.providers.anthropic import AnthropicProvider

            return AnthropicProvider(api_key=key, client=Anthropic(api_key=key, http_client=http))
        from openai import OpenAI

        from tailorcv.infrastructure.providers.openai import OpenAIProvider

        return OpenAIProvider(api_key=key, client=OpenAI(api_key=key, http_client=http))

    return build


def main() -> int:
    """Record a full pipeline run and write the cassette."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("source", help="Posting URL, PDF, screenshot, or text file.")
    parser.add_argument("--out", type=Path, required=True, help="Cassette file to write.")
    parser.add_argument("--config", type=Path, default=None, help="Path to tailorcv.toml.")
    parser.add_argument(
        "--provider",
        type=ProviderName,
        default=None,
        help="Provider to record. Defaults to the configured one.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Where to write the run package. Defaults to a temporary directory.",
    )
    arguments = parser.parse_args()

    output_dir = arguments.output_dir or Path(tempfile.mkdtemp(prefix="tailorcv-cassette-"))
    settings = load_settings(
        arguments.config,
        overrides={"provider": arguments.provider, "output_dir": output_dir},
    )
    cassette = Cassette(path=arguments.out)
    credential = resolve(settings.provider, prompter=Terminal())

    from tailorcv.application.tailoring import pipeline
    from tailorcv.application.tailoring.pipeline import service as pipeline_service

    # Patch the service module; the pipeline package does not re-export provider_for.
    service_module: Any = pipeline_service
    service_module.provider_for = recording_provider(cassette)

    try:
        result = pipeline.run(
            pipeline.RunCommand(
                source=arguments.source,
                settings=settings,
                api_key=credential.key,
                cache_enabled=False,
                force=True,
            )
        )
    finally:
        cassette.save()
        print(f"Recorded {len(cassette.interactions)} interaction(s) to {arguments.out}")

    if result.materialized is None:
        raise RuntimeError("cassette recording requires a materialized run")
    print(f"Package: {result.materialized.directory}")
    print(f"Checks passed: {result.verification.passed}")
    for line in result.ledger.summary_lines():
        print(line)

    total_spend = result.ledger.total_dollars
    if not cassette.interactions:
        message = "cassette is empty: the recording client was not called"
        if total_spend is not None and total_spend > 0:
            message += f", but the run billed ${total_spend:.4f}"
        print(f"\nFAIL: {message}", file=sys.stderr)
        return 2

    print("\nReview the cassette before committing it; it contains real model output.")
    return 0 if result.verification.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
