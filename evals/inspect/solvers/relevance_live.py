"""Live solver: run the shipped relevance-review stage against the real provider.

Only ``scripts/eval.sh --live`` should reach this solver, because it is the only
relevance mode that spends money. It refuses to launch unless the environment
carries the complete approved run identity, and it records the prompt snapshot
and dataset identity the run was bound to. Credentials load through
``clearscout.infrastructure.providers.credentials.find``; nothing here prompts.

Each repeat sends its rows in production-shaped batches: one task per search
context, split by the production batch limits, so one paid call decides every
row in its batch under the schema production closes over the task's CV and conditions. Every
trial sends the same batches, because rotating rows would put them in batches
production never sends.

A run may declare several prompt arms in ``CLEARSCOUT_EVAL_PROMPT_ARMS`` as
``name=directory`` entries separated by commas. Each directory holds one
``<prompt>.md`` file per name in ``RELEVANCE_PROMPTS``, the runner asks for one
arm per trial, and every call is checked against the bytes that arm declared.

``CLEARSCOUT_EVAL_CV_VERSION`` names a master-CV version in the billed workspace
to review against instead of the synthetic CV. ``--render-only`` writes each
batch request a trial would send, per arm, and calls no provider. It takes the
run's ``--limit``, ``--ids`` and ``--split`` and selects rows the way the runner
does.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import runpy
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from clearscout.application.config import ProviderName, Settings, Stage
from clearscout.application.discovery.relevance_review import review_plan
from clearscout.application.job_search.models import RelevanceReviewBatchResponse
from clearscout.application.master_cv.cv import CvInventory, parse_cv
from clearscout.application.master_cv.service import MasterCvService
from clearscout.application.prompts import overriding
from clearscout.application.tailoring.stages.context import record_block
from clearscout.infrastructure.artifacts.runtime import RuntimeStorage
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.providers.base import Finish, UnusableResponseError, wire_schema
from clearscout.infrastructure.providers.plan import RequestPlan, schema_digest
from clearscout.shared.costs import Ledger
from clearscout.shared.costs.prices import table as price_table
from clearscout.shared.errors import ClearscoutError
from evals.inspect.labeling import bytes_sha256
from evals.inspect.outcomes import BatchOutcome, FailedCall
from evals.inspect.runner import SPLITS, dataset_samples
from evals.inspect.solvers.clearscout_live import (
    PromptArm,
    arms_metadata,
    bill_sample,
    billing_target,
    credential_for,
    inside_repository,
    paid_stage_metadata,
    prompt_snapshot_fingerprint,
    requested_arm,
    run_settings,
    sample_provenance,
    stage_identity,
)
from evals.inspect.solvers.clearscout_live import declared_arms as shared_declared_arms
from evals.support.client import build_live_client
from evals.support.inventory import synthetic_inventory
from evals.support.relevance import (
    RelevanceCase,
    relevance_group,
    relevance_result,
    run_schema,
    sample_batches,
)

RELEVANCE_PROMPTS = (
    "relevance_review_system",
    "relevance_review_evidence",
    "relevance_review_user",
)
BILLING_ORIGIN = "eval.relevance_review_live"
CV_VERSION_ENV = "CLEARSCOUT_EVAL_CV_VERSION"


def declared_arms() -> tuple[PromptArm, ...]:
    """Return the relevance prompt versions this run declared, or the working tree alone."""
    return shared_declared_arms(RELEVANCE_PROMPTS)


def _dataset_rows(dataset: Path) -> list[dict[str, Any]]:
    lines = dataset.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _parsed_version(content: str) -> CvInventory:
    """Parse a stored version from a file, as search planning does, so line endings match."""
    with tempfile.TemporaryDirectory(prefix="clearscout-eval-cv-") as temporary:
        path = Path(temporary) / "master.md"
        path.write_text(content, encoding="utf-8")
        return parse_cv(path)


def run_inventory(
    database: Database,
    workspace_id: str,
    dataset: Path,
    rows: Sequence[Mapping[str, Any]],
) -> tuple[CvInventory, dict[str, Any]]:
    """Return the CV a run reviews against and the identity its metadata records.

    The synthetic CV, recording nothing, unless ``CLEARSCOUT_EVAL_CV_VERSION`` names a
    version the billed workspace holds.
    """
    version_id = os.environ.get(CV_VERSION_ENV, "").strip()
    if not version_id:
        return synthetic_inventory(), {}
    # Tracked labels were frozen against the synthetic CV, and real labels stay private.
    if inside_repository(dataset):
        raise RuntimeError(
            "Live evaluation refused: a CV version reviews a private dataset outside the "
            f"repository, and {dataset} is inside it."
        )
    storage = RuntimeStorage.resolve(database.url)
    try:
        version = MasterCvService(database, storage).read_version(workspace_id, version_id)
        inventory = _parsed_version(version.content)
    except ClearscoutError as error:
        raise RuntimeError(f"Live evaluation refused: CV version {version_id}: {error}") from error
    mislabelled = [
        str(row.get("id")) for row in rows if row.get("cv_hash") != inventory.content_hash
    ]
    if mislabelled:
        raise RuntimeError(
            f"Live evaluation refused: rows {', '.join(mislabelled)} do not name cv_hash "
            f"{inventory.content_hash}, which CV version {version_id} parses to."
        )
    return inventory, {
        "master_cv_version_id": version.id,
        "master_cv_content_sha256": version.content_sha256,
        "cv_hash": inventory.content_hash,
    }


def review_calls(samples: Sequence[Mapping[str, Any]], trials: int = 1, arms: int = 1) -> int:
    """Return the paid calls a run over ``samples`` makes: each trial's batches, once per arm."""
    return arms * trials * sum(len(task) for task in sample_batches(samples))


@dataclass(frozen=True)
class BatchRequest:
    """One batch call a repeat sends, and the sample positions it decides."""

    arm: PromptArm
    index: int
    positions: tuple[int, ...]
    sample_ids: tuple[str, ...]
    case: RelevanceCase
    plan: RequestPlan
    schema: type[BaseModel]

    def provenance(self, ledger: Ledger) -> dict[str, Any]:
        """Return the call identity every member row records."""
        return {
            "index": self.index,
            "size": len(self.positions),
            "sample_ids": list(self.sample_ids),
            "call_dollars": ledger.total_dollars,
            "response_ids": [entry.response_id for entry in ledger.entries],
            "cache_keys": [entry.cache_key for entry in ledger.entries],
        }


@dataclass(frozen=True)
class _RelevanceLiveSolver:
    """One paid relevance-review run: production-shaped batches, shipped validation."""

    run_metadata: dict[str, Any]
    arms: tuple[PromptArm, ...]
    provider_name: ProviderName
    settings: Settings
    ledger: Ledger
    database: Database
    workspace_id: str
    inventory: CvInventory | None = None

    def _requested_arm(self, requested: object) -> PromptArm:
        """Return the declared arm this trial claims, refusing any other name."""
        return requested_arm(self.arms, requested)

    def requests(self, samples: Sequence[Mapping[str, Any]]) -> list[BatchRequest]:
        """Return every batch request one repeat sends, in the order a paid run sends them."""
        if not samples:
            return []
        named = {sample.get("arm") for sample in samples}
        if len(named) != 1:
            raise RuntimeError("Live evaluation refused: one repeat sends one prompt arm.")
        arm = self._requested_arm(named.pop())
        if prompt_snapshot_fingerprint(RELEVANCE_PROMPTS, arm.current()) != arm.fingerprint:
            suffix = "" if arm.name is None else f" (arm {arm.name})"
            raise RuntimeError(
                "Live evaluation refused: relevance_review prompt bytes changed after "
                f"run preflight{suffix}."
            )
        inventory = self.inventory if self.inventory is not None else synthetic_inventory()
        requests: list[BatchRequest] = []
        for task in sample_batches(samples):
            # Production closes one schema per task over the CV and the task's conditions.
            schema = run_schema(
                [samples[position] for batch in task for position in batch], inventory
            )
            for batch in task:
                positions = tuple(batch)
                members = [samples[position] for position in positions]
                case = relevance_group(f"batch {len(requests)}", members, inventory)
                with overriding(arm.snapshot):
                    plan = review_plan(
                        self.settings,
                        case.inventory,
                        case.search_context,
                        case.posting_block,
                        person_notes=case.person_notes,
                    )
                requests.append(
                    BatchRequest(
                        arm=arm,
                        index=len(requests),
                        positions=positions,
                        sample_ids=tuple(str(member.get("id")) for member in members),
                        case=case,
                        plan=plan,
                        schema=schema,
                    )
                )
        return requests

    def batch(self, samples: Sequence[Mapping[str, Any]]) -> Iterator[tuple[int, BatchOutcome]]:
        """Review one repeat's samples live, yielding each sample's output as its call returns."""
        for request in self.requests(samples):
            yield from self._reviewed(request)

    def _reviewed(self, request: BatchRequest) -> Iterator[tuple[int, BatchOutcome]]:
        """Send one batch, bill it once, and split the shipped validation over its members."""
        call_ledger = Ledger(prices=self.ledger.prices)
        client = build_live_client(
            self.provider_name,
            credential_for(self.provider_name),
            ledger=call_ledger,
            settings=self.settings,
        )
        truncated: UnusableResponseError | None = None
        try:
            with overriding(request.arm.snapshot):
                raw = client.complete(request.plan, request.schema)
            response = RelevanceReviewBatchResponse.model_validate(raw.model_dump(mode="json"))
            valid, failures = request.case.validate(response)
        except UnusableResponseError as error:
            if error.record.finish is not Finish.TRUNCATED:
                raise
            truncated = error
        finally:
            self.ledger.entries.extend(call_ledger.entries)
            billing_failure = bill_sample(
                self.database,
                self.workspace_id,
                ledger=call_ledger,
                provider=str(self.run_metadata["provider"]),
                origin=BILLING_ORIGIN,
            )
        call = request.provenance(call_ledger)
        if truncated is not None:
            failed = FailedCall(truncated, call)
            for position in request.positions:
                yield position, failed
            return
        share = None if call["call_dollars"] is None else call["call_dollars"] / call["size"]
        for member, (position, posting) in enumerate(
            zip(request.positions, request.case.pending, strict=True)
        ):
            provenance = sample_provenance(
                call_ledger, self.ledger, self.run_metadata, billing_failure
            )
            # Readers sum measurements per row, so only the first member carries the call's.
            if member:
                provenance["measurements"] = []
            provenance["actual_dollars"] = share
            provenance["batch"] = call
            if request.arm.name is not None:
                provenance["arm"] = request.arm.name
            result = relevance_result(
                [item for item in valid if item.pending_discovery_id == posting.id],
                [item for item in failures if item.pending_discovery_id == posting.id],
                response,
            )
            yield position, {**result, "metadata": provenance}

    def __call__(self, sample: dict[str, Any]) -> dict[str, Any]:
        """Review one posting live as a batch of one."""
        ((_, outcome),) = list(self.batch([sample]))
        if isinstance(outcome, FailedCall):
            raise outcome.error
        return outcome

    def rendered(self, samples: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Return what each batch of one repeat would send, with the digests a call records."""
        rendered: list[dict[str, Any]] = []
        for request in self.requests(samples):
            case, plan, evidence = request.case, request.plan, request.plan.evidence
            rendered.append(
                {
                    "batch_index": request.index,
                    "arm": request.arm.name,
                    "sample_ids": list(request.sample_ids),
                    "posting_ids": [posting.id for posting in case.pending],
                    "instructions": plan.instructions.text,
                    "instructions_digest": plan.instructions.digest,
                    "evidence": "" if evidence is None else evidence.text,
                    "evidence_digest": "" if evidence is None else evidence.digest,
                    "task": plan.task.text,
                    "task_digest": plan.task.digest,
                    "master_cv_evidence": record_block(case.inventory),
                    "search_context": case.search_context,
                    "posting_block": case.posting_block,
                    "person_notes": case.person_notes,
                    "posting_span_ids": {
                        posting.id: [span.id for span in case.spans[posting.id]]
                        for posting in case.pending
                    },
                    "cv_entry_ids": list(case.entry_ids),
                    "schema_digest": schema_digest(wire_schema(self.provider_name, request.schema)),
                }
            )
        return rendered


def _prepared(dataset: Path, run_metadata: dict[str, Any]) -> _RelevanceLiveSolver:
    """Bind a run to its arms, settings, workspace, and CV."""
    arms = declared_arms()
    declared = arms_metadata(arms)
    if declared:
        # Declared arms supply every call's bytes, so the working-tree fingerprint
        # would name prompts no row was produced from.
        run_metadata.pop("prompt_suite_fingerprint", None)
        run_metadata.update(declared)
    provider_name = ProviderName(run_metadata["provider"])
    settings = run_settings(provider_name, Stage.RELEVANCE_REVIEW)
    rows = _dataset_rows(dataset)
    database, workspace_id = billing_target()
    try:
        inventory, cv_identity = run_inventory(database, workspace_id, dataset, rows)
        # Refuse a row no schema can be closed over before any call is paid for.
        run_schema(rows, inventory)
    except BaseException:
        database.dispose()
        raise
    run_metadata.update(cv_identity)
    return _RelevanceLiveSolver(
        run_metadata=run_metadata,
        arms=arms,
        provider_name=provider_name,
        settings=settings,
        ledger=Ledger(prices=price_table(settings.prices)),
        database=database,
        workspace_id=workspace_id,
        inventory=inventory,
    )


def relevance_live_solver(dataset: Path) -> _RelevanceLiveSolver:
    """Return the prepared solver that runs the relevance-review stage live."""
    run_metadata = paid_stage_metadata(Stage.RELEVANCE_REVIEW, RELEVANCE_PROMPTS, dataset)
    return _prepared(dataset, run_metadata)


_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


def render_requests(
    dataset: Path,
    provider: ProviderName,
    output_dir: Path,
    samples: Sequence[Mapping[str, Any]],
) -> list[Path]:
    """Write each batch request a trial of these samples sends under every arm; call no provider."""
    output_dir = output_dir.expanduser().resolve()
    if inside_repository(output_dir):
        raise RuntimeError("Live evaluation refused: render output must be outside the repository.")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"Live evaluation refused: render output {output_dir} is not empty.")
    settings = run_settings(provider, Stage.RELEVANCE_REVIEW)
    solver = _prepared(
        dataset,
        {
            "provider": provider.value,
            **stage_identity(settings, Stage.RELEVANCE_REVIEW),
            "stage": Stage.RELEVANCE_REVIEW.value,
            "dataset_sha256": bytes_sha256(dataset.read_bytes()),
        },
    )
    try:
        # The runner hands a solver only these fields, so no label reaches a render.
        requests = [
            request
            for arm in solver.arms
            for request in solver.rendered(
                [
                    {"id": sample.get("id"), "input": sample.get("input"), "arm": arm.name}
                    for sample in samples
                ]
            )
        ]
    finally:
        solver.database.dispose()
    named = {
        _UNSAFE_NAME.sub(
            "_", f"batch-{request['batch_index']:03d}.{request['arm'] or 'working-tree'}"
        )
        + ".json": request
        for request in requests
    }
    if len(named) != len(requests):
        raise RuntimeError("Live evaluation refused: two rendered requests share one file name.")
    output_dir.mkdir(parents=True, exist_ok=True)
    run = {**solver.run_metadata, "prompt_arms": [arm.as_metadata() for arm in solver.arms]}
    written: list[Path] = []
    for name, content in {"run.json": run, **named}.items():
        path = output_dir / name
        with path.open("x", encoding="utf-8") as handle:
            # A request carries the real CV and posting bodies, as a paid run log does.
            path.chmod(0o600)
            handle.write(json.dumps(content, ensure_ascii=False, indent=2) + "\n")
        written.append(path)
    return written[1:]


def main(argv: list[str] | None = None) -> int:
    """Render the requests a relevance task's run would send, without a provider or a cost."""
    parser = argparse.ArgumentParser(
        prog="python -m evals.inspect.solvers.relevance_live",
        description="Write each request a live relevance run would send, calling no provider.",
    )
    parser.add_argument("task", type=Path, help="Task file whose DATASET_PATH the run reviews.")
    parser.add_argument("--render-only", type=Path, required=True, metavar="DIR")
    parser.add_argument("--provider", type=ProviderName, required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--split", choices=SPLITS)
    parser.add_argument("--ids", nargs="+")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        task = runpy.run_path(str(args.task))
        if task.get("STAGE") != Stage.RELEVANCE_REVIEW.value:
            raise RuntimeError(f"Live evaluation refused: {args.task} is not a relevance task.")
        dataset = Path(task["DATASET_PATH"])
        samples = dataset_samples(dataset, limit=args.limit, split=args.split, ids=args.ids)
        written = render_requests(dataset, args.provider, args.render_only, samples)
    except (ImportError, KeyError, OSError, ValueError, RuntimeError, ClearscoutError) as error:
        print(f"eval render error: {error}", file=sys.stderr)
        return 1
    print(f"rendered {len(written)} requests into {args.render_only}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
