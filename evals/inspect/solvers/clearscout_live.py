"""Live solver: run the shipped match stage against the real provider.

Only ``scripts/eval.sh --live`` should reach this solver, because it is the
only mode that spends money. The solver refuses to launch unless the
environment carries the complete approved run identity. Credentials load through
``clearscout.infrastructure.providers.credentials.find`` (flag/env/keyring); this
solver never prompts.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Generic, TypeVar

from sqlalchemy import select

from clearscout.application.config import Effort, ProviderName, Settings, Stage
from clearscout.application.master_cv.cv import CvInventory
from clearscout.application.prompts import check_template, load, overriding, suite_fingerprint
from clearscout.application.tailoring.guardrails import Guardrails
from clearscout.application.tailoring.runs import settings_hash as run_settings_hash
from clearscout.application.tailoring.stages.match import Strength, match
from clearscout.application.workspaces.spend import record_calls
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import Workspace
from clearscout.infrastructure.providers.credentials import find as find_credential
from clearscout.shared.costs import Ledger
from clearscout.shared.costs.prices import table as price_table
from clearscout.shared.errors import AuthError, ClearscoutError
from evals.inspect.labeling import bytes_sha256
from evals.inspect.selection import SelectionManifest
from evals.semantic import StageMeasurement
from evals.support.client import build_live_client
from evals.support.inventory import synthetic_guardrails, synthetic_inventory
from evals.support.jobspec import (
    numbered_job_spec,
    positional_requirement_ids,
    requirement_outcomes,
    sample_job_spec,
)

_APPROVAL_ENV = "CLEARSCOUT_EVAL_LIVE_APPROVED_CENTS"
_ESTIMATE_ENV = "CLEARSCOUT_EVAL_ESTIMATED_CENTS"
_PROVIDER_ENV = "CLEARSCOUT_EVAL_LIVE_PROVIDER"
_MODEL_ENV = "CLEARSCOUT_EVAL_LIVE_MODEL"
_EFFORT_ENV = "CLEARSCOUT_EVAL_LIVE_EFFORT"
_SPLIT_ENV = "CLEARSCOUT_EVAL_DATASET_SPLIT"
_LIMIT_ENV = "CLEARSCOUT_EVAL_SAMPLE_LIMIT"
_SAMPLE_IDS_ENV = "CLEARSCOUT_EVAL_SAMPLE_IDS"
_TRIALS_ENV = "CLEARSCOUT_EVAL_TRIALS"
_OUTPUT_DIR_ENV = "CLEARSCOUT_EVAL_OUTPUT_DIR"
_SELECTION_MANIFEST_ENV = "CLEARSCOUT_EVAL_SELECTION_MANIFEST"
_SELECTION_MANIFEST_SHA_ENV = "CLEARSCOUT_EVAL_SELECTION_MANIFEST_SHA256"
_SELECTION_MANIFEST_BYTES_SHA_ENV = "CLEARSCOUT_EVAL_SELECTION_MANIFEST_BYTES_SHA256"
_DATABASE_URL_ENV = "CLEARSCOUT_EVAL_DATABASE_URL"
PROMPT_ARMS_ENV = "CLEARSCOUT_EVAL_PROMPT_ARMS"
_BILLING_ORIGIN = "eval.match_live"
_SPLITS = {"development", "regression", "holdout"}
_MATCH_PROMPTS = ("match_system", "match_user")
_REPO_ROOT = Path(__file__).resolve().parents[3]

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


def inside_repository(path: Path) -> bool:
    """Return whether ``path`` resolves into the public repository."""
    resolved = path.expanduser().resolve()
    return resolved == _REPO_ROOT or resolved.is_relative_to(_REPO_ROOT)


def _prompt_fingerprint(snapshot: Mapping[str, str]) -> str:
    """Return the suite fingerprint for one complete match-prompt snapshot."""
    return prompt_snapshot_fingerprint(_MATCH_PROMPTS, snapshot)


def _selected_provider() -> ProviderName:
    override = os.environ.get(_PROVIDER_ENV, "").strip().lower()
    if not override:
        raise RuntimeError("Live evaluation refused: an explicit provider is required.")
    return ProviderName(override)


def _named_effort(text: str) -> Effort | None:
    """Return the effort a run names, or None when it names none."""
    text = text.strip().lower()
    if not text:
        return None
    try:
        return Effort(text)
    except ValueError:
        allowed = ", ".join(effort.value for effort in Effort)
        raise RuntimeError(
            f"Live evaluation refused: no effort level called {text!r}; use one of {allowed}."
        ) from None


def live_settings(
    provider: ProviderName, stage: Stage, model: str = "", effort: Effort | None = None
) -> Settings:
    """Return the shipped settings with ``model`` and ``effort``, when named, on the paid stage."""
    settings = Settings(provider=provider)
    update = {name: value for name, value in (("model", model), ("effort", effort)) if value}
    if not update:
        return settings
    setting = settings.policy_for(stage).model_copy(update=update)
    return settings.model_copy(update={"stages": {**settings.stages, stage: setting}})


def run_settings(provider: ProviderName, stage: Stage) -> Settings:
    """Return the settings this approved run sends for its stage."""
    return live_settings(
        provider,
        stage,
        os.environ.get(_MODEL_ENV, "").strip(),
        _named_effort(os.environ.get(_EFFORT_ENV, "")),
    )


def _required_environment(name: str, description: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Live evaluation refused: {description} is required in {name}.")
    return value


def _selection_manifest_identity(*, split: str, limit: int) -> dict[str, Any]:
    """Strictly load the approved private selection manifest and bind run scope."""
    path = (
        Path(_required_environment(_SELECTION_MANIFEST_ENV, "a private selection manifest"))
        .expanduser()
        .resolve()
    )
    if inside_repository(path):
        raise RuntimeError(
            "Live evaluation refused: selection manifest must be outside the public repository."
        )
    expected = _required_environment(
        _SELECTION_MANIFEST_SHA_ENV, "an approved selection-manifest SHA-256"
    )
    if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
        raise RuntimeError(
            f"Live evaluation refused: {_SELECTION_MANIFEST_SHA_ENV} must be lowercase SHA-256."
        )
    expected_bytes = _required_environment(
        _SELECTION_MANIFEST_BYTES_SHA_ENV,
        "an approved exact-byte selection-manifest SHA-256",
    )
    if len(expected_bytes) != 64 or any(
        character not in "0123456789abcdef" for character in expected_bytes
    ):
        raise RuntimeError(
            f"Live evaluation refused: {_SELECTION_MANIFEST_BYTES_SHA_ENV} must be lowercase "
            "SHA-256."
        )
    try:
        content = path.read_bytes()
        manifest = SelectionManifest.model_validate_json(content)
    except (OSError, ValueError) as error:
        message = f"Live evaluation refused: invalid selection manifest: {error}"
        raise RuntimeError(message) from error
    if manifest.manifest_sha256 != expected:
        raise RuntimeError(
            "Live evaluation refused: selection manifest does not match the approved SHA-256."
        )
    actual_bytes = bytes_sha256(content)
    if actual_bytes != expected_bytes:
        raise RuntimeError(
            "Live evaluation refused: selection manifest bytes do not match the approved SHA-256."
        )
    if manifest.body.synthetic_inventory_hash != synthetic_inventory().content_hash:
        raise RuntimeError(
            "Live evaluation refused: selection manifest uses another synthetic inventory."
        )
    split_cases = {
        "development": manifest.body.ordered_cases,
        "holdout": manifest.body.held_out_cases,
    }
    if split not in split_cases:
        raise RuntimeError(
            "Live evaluation refused: the selection manifest holds development and holdout "
            "cases only."
        )
    if limit != len(split_cases[split]):
        raise RuntimeError(
            "Live evaluation refused: sample limit does not match the approved selection manifest."
        )
    return {
        "selection_manifest_path": str(path),
        "selection_manifest_sha256": manifest.manifest_sha256,
        "selection_manifest_bytes_sha256": actual_bytes,
        "dataset_sha256": manifest.body.dataset_sha256,
        "review_context_sha256": manifest.body.review_context_sha256,
        "synthetic_inventory_hash": manifest.body.synthetic_inventory_hash,
        "candidate_pool_sha256": manifest.body.candidate_pool_sha256,
        "extraction_qa_sha256": manifest.body.extraction_qa_sha256,
        "review_set_sha256": manifest.body.review_set_sha256,
        "resolution_set_sha256": manifest.body.resolution_set_sha256,
    }


def _approved_sample_ids(limit: int) -> tuple[str, ...] | None:
    """Return the sample ids an approval names, or None when it names a count alone."""
    text = os.environ.get(_SAMPLE_IDS_ENV, "").strip()
    if not text:
        return None
    ids = tuple(item.strip() for item in text.replace(",", " ").split())
    if not ids or len(set(ids)) != len(ids):
        raise RuntimeError(
            f"Live evaluation refused: {_SAMPLE_IDS_ENV} must name distinct sample ids."
        )
    if len(ids) != limit:
        raise RuntimeError(
            "Live evaluation refused: approved sample ids do not match the approved sample limit."
        )
    return ids


@dataclass(frozen=True)
class PaidRunScope:
    """The consent and scope controls every live evaluation carries."""

    provider: ProviderName
    split: str
    limit: int
    output_dir: Path
    approved_cents: int
    estimated_cents: int | None
    trials: int = 1
    sample_ids: tuple[str, ...] | None = None

    def as_metadata(self) -> dict[str, Any]:
        """Return the scope fields a run log records."""
        return {
            "provider": self.provider.value,
            "approved_cents": self.approved_cents,
            "estimated_cents": self.estimated_cents,
            "dataset_split": self.split,
            "sample_limit": self.limit,
            "sample_ids": list(self.sample_ids) if self.sample_ids is not None else None,
            "trials": self.trials,
            "output_dir": str(self.output_dir),
        }


def paid_run_scope() -> PaidRunScope:
    """Validate the environment one approved paid evaluation run must carry."""
    approved_text = _required_environment(
        _APPROVAL_ENV, "explicit paid-run consent and an advisory estimate"
    )
    if not approved_text.isdigit():
        raise RuntimeError(
            "Live evaluation refused: advisory cost estimate must be nonnegative integer cents."
        )
    approved_cents = int(approved_text)
    estimate_text = os.environ.get(_ESTIMATE_ENV, "").strip()
    if estimate_text and not estimate_text.isdigit():
        raise RuntimeError(
            f"Live evaluation refused: {_ESTIMATE_ENV} must be an integer cent count."
        )
    provider = _selected_provider()
    split = _required_environment(_SPLIT_ENV, "a dataset split")
    if split not in _SPLITS:
        allowed = ", ".join(sorted(_SPLITS))
        raise RuntimeError(f"Live evaluation refused: dataset split must be one of {allowed}.")
    limit_text = _required_environment(_LIMIT_ENV, "a sample limit")
    if not limit_text.isdigit() or int(limit_text) < 1:
        raise RuntimeError("Live evaluation refused: sample limit must be a positive integer.")
    sample_ids = _approved_sample_ids(int(limit_text))
    # A run that repeats each sample is approved for the repeats, not for a
    # larger sample: an absent variable is one trial per sample.
    trials_text = os.environ.get(_TRIALS_ENV, "").strip() or "1"
    if not trials_text.isdigit() or int(trials_text) < 1:
        raise RuntimeError("Live evaluation refused: trial count must be a positive integer.")
    output_dir = (
        Path(_required_environment(_OUTPUT_DIR_ENV, "a private output directory"))
        .expanduser()
        .resolve()
    )
    if inside_repository(output_dir):
        raise RuntimeError(
            "Live evaluation refused: output directory must be outside the public repository."
        )
    return PaidRunScope(
        provider=provider,
        split=split,
        limit=int(limit_text),
        trials=int(trials_text),
        output_dir=output_dir,
        approved_cents=approved_cents,
        estimated_cents=int(estimate_text) if estimate_text else None,
        sample_ids=sample_ids,
    )


def stage_identity(settings: Settings, stage: Stage) -> dict[str, Any]:
    """Return the model and settings identity one stage's live run is bound to."""
    choice = settings.choice_for_stage(stage, settings.unattended_tier())
    return {
        "requested_model": choice.model,
        "requested_effort": choice.effort.value if choice.effort is not None else None,
        "service_tier": choice.service_tier.value,
        "settings_hash": run_settings_hash(settings),
    }


def prompt_snapshot_fingerprint(names: Sequence[str], snapshot: Mapping[str, str]) -> str:
    """Return the suite fingerprint for one complete prompt snapshot."""
    with overriding(snapshot):
        return suite_fingerprint(tuple(names))


@dataclass(frozen=True)
class PromptArm:
    """One prompt version a run alternates onto, and the bytes it was declared as."""

    name: str | None
    source: Path | None
    prompt_names: tuple[str, ...]
    snapshot: Mapping[str, str]
    fingerprint: str

    def current(self) -> dict[str, str]:
        """Re-read this arm's prompt bytes from where it was declared."""
        if self.source is None:
            return {name: load(name) for name in self.prompt_names}
        return _arm_snapshot(self.prompt_names, self.source)

    def as_metadata(self) -> dict[str, Any]:
        """Return the bytes identity a reader checks this arm's rows against."""
        return {
            "arm": self.name,
            "source": str(self.source),
            "prompt_suite_fingerprint": self.fingerprint,
            "prompts": {
                name: bytes_sha256(text.encode("utf-8")) for name, text in self.snapshot.items()
            },
        }


def _arm_snapshot(prompt_names: Sequence[str], directory: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for name in prompt_names:
        path = directory / f"{name}.md"
        try:
            snapshot[name] = path.read_text(encoding="utf-8")
        except OSError as error:
            raise RuntimeError(
                f"Live evaluation refused: prompt arm cannot read {path}."
            ) from error
    return snapshot


def _arm(prompt_names: tuple[str, ...], name: str | None, directory: Path | None) -> PromptArm:
    snapshot = (
        {prompt: load(prompt) for prompt in prompt_names}
        if directory is None
        else _arm_snapshot(prompt_names, directory)
    )
    for prompt, text in snapshot.items():
        try:
            check_template(prompt, text)
        except ClearscoutError as error:
            named = "the working tree" if name is None else f"prompt arm {name}"
            raise RuntimeError(f"Live evaluation refused: {named}: {error}") from error
    return PromptArm(
        name=name,
        source=directory,
        prompt_names=prompt_names,
        snapshot=MappingProxyType(snapshot),
        fingerprint=prompt_snapshot_fingerprint(prompt_names, snapshot),
    )


def declared_arms(prompt_names: Sequence[str]) -> tuple[PromptArm, ...]:
    """Return the prompt versions this run declared, or the working tree alone."""
    names_tuple = tuple(prompt_names)
    declaration = os.environ.get(PROMPT_ARMS_ENV, "").strip()
    if not declaration:
        return (_arm(names_tuple, None, None),)
    arms: list[PromptArm] = []
    for entry in declaration.split(","):
        name, separator, directory = entry.partition("=")
        if not separator or not name.strip() or not directory.strip():
            raise RuntimeError(
                f"Live evaluation refused: prompt arm {entry!r} is not name=directory."
            )
        arms.append(_arm(names_tuple, name.strip(), Path(directory.strip()).expanduser().resolve()))
    names = [arm.name for arm in arms]
    if len(set(names)) != len(names):
        raise RuntimeError("Live evaluation refused: prompt arm names must be distinct.")
    return tuple(arms)


def arms_metadata(arms: Sequence[PromptArm]) -> dict[str, Any]:
    """Return the declared-arm identity a run records, empty when none was declared."""
    if all(arm.name is None for arm in arms):
        return {}
    return {"arms": len(arms), "prompt_arms": [arm.as_metadata() for arm in arms]}


def requested_arm(arms: Sequence[PromptArm], requested: object) -> PromptArm:
    """Return the declared arm a call claims, refusing any other name."""
    for arm in arms:
        if arm.name == requested:
            return arm
    raise RuntimeError(f"Live evaluation refused: undeclared prompt arm {requested!r}.")


def paid_stage_metadata(stage: Stage, prompt_names: Sequence[str], dataset: Path) -> dict[str, Any]:
    """Bind one paid run to its approved scope, stage, prompts, and dataset bytes."""
    scope = paid_run_scope()
    settings = run_settings(scope.provider, stage)
    snapshot = {name: load(name) for name in prompt_names}
    metadata: dict[str, Any] = {
        **scope.as_metadata(),
        **stage_identity(settings, stage),
        "stage": stage.value,
        "dataset_sha256": bytes_sha256(dataset.read_bytes()),
        "prompt_suite_fingerprint": prompt_snapshot_fingerprint(prompt_names, snapshot),
    }
    # The draw count changes both the critic's verdict and its cost.
    if stage is Stage.CRITIC:
        metadata["critic_trials"] = settings.critic_trials
    return metadata


def credential_for(provider_name: ProviderName) -> str:
    """Return the stored API key for one provider, or refuse without prompting."""
    credential = find_credential(provider_name)
    if credential is None:
        raise AuthError(
            f"No API key for {provider_name.value}. Set the environment "
            "variable or store one with 'clearscout auth'."
        )
    return credential.key


def sample_provenance(
    sample_ledger: Ledger,
    run_ledger: Ledger,
    run_metadata: Mapping[str, Any],
    billing_failure: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the spend and run identity one paid sample records."""
    provenance: dict[str, Any] = {
        "measurements": [
            StageMeasurement.from_manifest(entry).model_dump(mode="json")
            for entry in sample_ledger.entries
        ],
        "actual_dollars": sample_ledger.total_dollars,
        "cumulative_dollars": run_ledger.total_dollars,
        "run": dict(run_metadata),
    }
    if billing_failure is not None:
        provenance["billing_failure"] = dict(billing_failure)
    return provenance


def live_run_metadata(prompt_snapshot: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Validate and return the identity and scope controls for one live match run."""
    scope = paid_run_scope()
    selection_identity = _selection_manifest_identity(split=scope.split, limit=scope.limit)
    settings = run_settings(scope.provider, Stage.MATCH)
    snapshot = (
        dict(prompt_snapshot)
        if prompt_snapshot is not None
        else {name: load(name) for name in _MATCH_PROMPTS}
    )
    return {
        **selection_identity,
        **scope.as_metadata(),
        **stage_identity(settings, Stage.MATCH),
        "stage": Stage.MATCH.value,
        "prompt_suite_fingerprint": _prompt_fingerprint(snapshot),
    }


def billing_target() -> tuple[Database, str]:
    """Return the database and workspace a live run bills its calls to.

    A live evaluation spends real money against a real data root, so its calls
    belong in the same ledger every other paid path writes to. The run refuses
    rather than guess when the data root holds no single workspace to bill.
    """
    url = _required_environment(_DATABASE_URL_ENV, "the workspace database to bill")
    database = Database(url, require_existing=True)
    with database.session() as session:
        ids = list(session.scalars(select(Workspace.id).where(Workspace.archived_at.is_(None))))
    if len(ids) != 1:
        database.dispose()
        raise RuntimeError(
            f"Live evaluation refused: expected one active workspace to bill, found {len(ids)}."
        )
    return database, ids[0]


def bill(
    database: Database,
    workspace_id: str,
    *,
    ledger: Ledger,
    provider: str,
    origin: str,
) -> None:
    """File one sample's calls before the next sample starts.

    Billing per sample rather than per run means a run that dies partway still
    leaves the money it spent in the ledger.
    """
    if not ledger.entries:
        return
    with database.session() as session:
        record_calls(
            session,
            workspace_id=workspace_id,
            task_id=None,
            ledger=ledger,
            provider=provider,
            origin=origin,
        )


def bill_sample(
    database: Database,
    workspace_id: str,
    *,
    ledger: Ledger,
    provider: str,
    origin: str,
) -> dict[str, Any] | None:
    """Bill one sample, returning a marker rather than raising when the write fails.

    The provider was already paid by the time billing runs, so a ledger the run
    could not write is reported beside the answer instead of replacing it.
    Interrupts and cancellation derive from ``BaseException`` and still stop the run.
    """
    try:
        bill(database, workspace_id, ledger=ledger, provider=provider, origin=origin)
    except Exception as error:  # noqa: BLE001 - a ledger write fails in many ways
        failure = {
            "origin": origin,
            "unbilled_dollars": ledger.total_dollars,
            "error_type": type(error).__name__,
        }
        logger.warning("eval_sample_unbilled", extra=failure)
        return failure
    return None


@dataclass(frozen=True)
class PaidCall(Generic[_T]):
    """What one billed provider call returned, and the spend it recorded."""

    value: _T
    ledger: Ledger
    billing_failure: dict[str, Any] | None


def paid_call(
    call: Callable[[Any], _T],
    *,
    provider_name: ProviderName,
    settings: Settings,
    run_ledger: Ledger,
    run_metadata: Mapping[str, Any],
    prompt_snapshot: Mapping[str, str],
    database: Database,
    workspace_id: str,
    origin: str,
) -> PaidCall[_T]:
    """Run ``call`` on a fresh live client under ``prompt_snapshot``, then bill it once.

    The run ledger and the workspace ledger receive the call's entries even when
    ``call`` raises, because the provider may already have been paid.
    """
    call_ledger = Ledger(prices=run_ledger.prices)
    client = build_live_client(
        provider_name, credential_for(provider_name), ledger=call_ledger, settings=settings
    )
    try:
        with overriding(prompt_snapshot):
            value = call(client)
    finally:
        run_ledger.entries.extend(call_ledger.entries)
        billing_failure = bill_sample(
            database,
            workspace_id,
            ledger=call_ledger,
            provider=str(run_metadata["provider"]),
            origin=origin,
        )
    return PaidCall(value, call_ledger, billing_failure)


@dataclass(frozen=True)
class _ClearScoutLiveSolver:
    run_metadata: dict[str, Any]
    inventory: CvInventory
    guardrails: Guardrails
    provider_name: ProviderName
    settings: Settings
    prompt_snapshot: Mapping[str, str]
    ledger: Ledger
    database: Database
    workspace_id: str

    def __call__(self, sample: dict[str, Any]) -> dict[str, Any]:
        current_prompts = {name: load(name) for name in _MATCH_PROMPTS}
        if _prompt_fingerprint(current_prompts) != self.run_metadata["prompt_suite_fingerprint"]:
            raise RuntimeError(
                "Live evaluation refused: match prompt bytes changed after run preflight."
            )
        spec = sample_job_spec(sample, self.inventory)
        multi = "job_spec" in sample
        paid = paid_call(
            lambda client: match(client, self.inventory, numbered_job_spec(spec), self.guardrails),
            provider_name=self.provider_name,
            settings=self.settings,
            run_ledger=self.ledger,
            run_metadata=self.run_metadata,
            prompt_snapshot=self.prompt_snapshot,
            database=self.database,
            workspace_id=self.workspace_id,
            origin=_BILLING_ORIGIN,
        )
        evidence = paid.value
        provenance = sample_provenance(
            paid.ledger, self.ledger, self.run_metadata, paid.billing_failure
        )
        if multi:
            return {
                "per_requirement": requirement_outcomes(spec, evidence),
                "metadata": provenance,
            }
        result = evidence.for_requirement(positional_requirement_ids(1)[0])
        # A row the run could not establish is reported incomplete, so the
        # scorer cannot credit an unanswered requirement as a judged gap.
        detail: dict[str, Any] = {}
        if result is not None:
            detail["note"] = result.note
            if result.gap_class is not None:
                detail["gap_class"] = result.gap_class.value
        return {
            "completion": result.classification.value if result is not None else "GAP",
            "metadata": {
                "entry_keys": list(result.entry_ids) if result is not None else [],
                "named_tools": list(result.named_tools) if result is not None else [],
                "strength": result.strength.value if result is not None else Strength.NONE.value,
                "outcome_complete": result is not None and not result.needs_review,
                **detail,
                **provenance,
            },
        }


def clearscout_live_solver() -> _ClearScoutLiveSolver:
    """Return the prepared solver that runs the match stage live."""
    prompt_snapshot = MappingProxyType({name: load(name) for name in _MATCH_PROMPTS})
    run_metadata = live_run_metadata(prompt_snapshot)
    settings = run_settings(ProviderName(run_metadata["provider"]), Stage.MATCH)
    ledger = Ledger(prices=price_table(settings.prices))
    database, workspace_id = billing_target()
    return _ClearScoutLiveSolver(
        database=database,
        workspace_id=workspace_id,
        run_metadata=run_metadata,
        inventory=synthetic_inventory(),
        guardrails=synthetic_guardrails(),
        provider_name=ProviderName(run_metadata["provider"]),
        settings=settings,
        prompt_snapshot=prompt_snapshot,
        ledger=ledger,
    )
