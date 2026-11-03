"""Run identity, staging, and durable artifact paths."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clearscout import __version__
from clearscout.application.prompts import suite_fingerprint
from clearscout.application.tailoring.layout import shaping_fingerprint
from clearscout.infrastructure.artifacts.files import write_private
from clearscout.infrastructure.build import current_build

if TYPE_CHECKING:  # pragma: no cover - typing only
    from clearscout.application.config import Settings
    from clearscout.application.discovery.posting_identity import Posting
    from clearscout.application.master_cv.cv import CvInventory

MANIFEST = "run.json"


@dataclass(frozen=True)
class RunIdentity:
    """Everything that decides whether two runs would produce the same package."""

    posting_id: str
    cv_hash: str
    prompt_suite: str
    settings_hash: str
    version: str
    cover_letter: bool = False
    research_hash: str = ""
    trimmed: int = 0
    # Each approval creates a distinct run identity.
    approval: str = ""
    code_revision: str = ""

    @property
    def slug(self) -> str:
        """Return the directory name for this run, readable and sortable."""
        letter = "-letter" if self.cover_letter else ""
        research = f"-research{self.research_hash[:8]}" if self.research_hash else ""
        trim = f"-trim{self.trimmed}" if self.trimmed else ""
        approved = f"-appr{self.approval[:8]}" if self.approval else ""
        return (
            f"{self.cv_hash[:8]}-{self.prompt_suite[:8]}-{self.settings_hash[:8]}"
            f"{letter}{research}{trim}{approved}"
        )

    def inputs(self) -> dict[str, Any]:
        """Return the identity as the manifest's ``inputs`` block spells it."""
        return {
            "cv_hash": self.cv_hash,
            "prompt_suite": self.prompt_suite,
            "settings_hash": self.settings_hash,
            "cover_letter": self.cover_letter,
            "research_hash": self.research_hash,
            "trimmed": self.trimmed,
            "approval": self.approval,
            "clearscout": self.version,
            "code_revision": self.code_revision,
        }


def identity_for(
    posting: Posting,
    inventory: CvInventory,
    settings: Settings,
    *,
    cover_letter: bool = False,
    research_hash: str = "",
    trimmed: int = 0,
    approval: str = "",
) -> RunIdentity:
    """Return everything that decides what this run would produce."""
    return RunIdentity(
        posting_id=posting.identifier,
        cv_hash=inventory.content_hash,
        prompt_suite=suite_fingerprint(),
        settings_hash=settings_hash(settings),
        version=__version__,
        code_revision=current_build().revision,
        cover_letter=cover_letter,
        research_hash=research_hash,
        trimmed=trimmed,
        approval=approval,
    )


def settings_hash(settings: Settings) -> str:
    """Digest the settings that change the output, and none that do not.

    Paths and the credential source are excluded. The shaping fingerprint is
    joined on, because the length budget changes what a run produces while
    every declared setting stays the same.
    """
    payload = settings.model_dump(
        mode="json",
        exclude={"cv", "output_dir", "source", "cache", "request_timeout", "prices"},
    )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")) + shaping_fingerprint()
    return hashlib.blake2s(encoded.encode("utf-8"), digest_size=8).hexdigest()


def input_fingerprints(cv_hash: str, settings: Settings) -> dict[str, str]:
    """Return this run's input fingerprints, in the manifest's own vocabulary.

    Named to match the ``inputs`` block of ``run.json`` so that a comparison
    against an earlier run is a dictionary lookup rather than a translation.
    """
    return {
        "cv_hash": cv_hash,
        "prompt_suite": suite_fingerprint(),
        "settings_hash": settings_hash(settings),
    }


@dataclass
class Manifest:
    """The machine-readable record of one run, written beside its files."""

    identity: RunIdentity
    posting_url: str
    posting_origin: str
    retrieved: str
    jd_hash: str
    started: str
    coverage: float
    outcome: str = "incomplete"
    checks: int = 0
    findings: list[dict[str, Any]] = field(default_factory=list)
    # Provider aliases affect both pricing and output.
    served_models: dict[str, str] = field(default_factory=dict)
    # Named per stage so a run can be compared against the config that resolved it.
    resolved_efforts: dict[str, str] = field(default_factory=dict)
    # Keyed by document: each write-and-check pass, in order.
    attempts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # Keyed like attempts: the number of the attempt whose document was written.
    kept: dict[str, int] = field(default_factory=dict)
    # Keyed like attempts: every sampled critic draw's scores, in draw order.
    critic_draws: dict[str, list[dict[str, int]]] = field(default_factory=dict)
    cache_hits: list[dict[str, str]] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    # How the page was selected and fitted, serialized from its typed record.
    selection: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        """Return the manifest as the JSON that lands on disk."""
        return {
            "posting": {
                "id": self.identity.posting_id,
                "url": self.posting_url,
                "identity_from": self.posting_origin,
                "retrieved": self.retrieved,
                "jd_hash": self.jd_hash,
            },
            "inputs": self.identity.inputs(),
            "run": {
                "started": self.started,
                "outcome": self.outcome,
                "checks": self.checks,
                "findings": self.findings,
                "coverage": self.coverage,
                "served_models": self.served_models,
                "resolved_efforts": self.resolved_efforts,
                "attempts": self.attempts,
                "kept": self.kept,
                "critic_draws": self.critic_draws,
                "cache_hits": self.cache_hits,
                "notes": self.notes,
            },
            "selection": self.selection,
            "artifacts": self.artifacts,
        }

    def serialize(self, artifacts: Mapping[str, bytes]) -> tuple[bytes, dict[str, Any]]:
        """Serialize this manifest from the exact artifact bytes it records."""
        self.artifacts = {
            name: _digest_bytes(content) for name, content in sorted(artifacts.items())
        }
        payload = self.payload()
        content = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
        return content, payload


@dataclass(frozen=True)
class MaterializedRun:
    """CLI-visible paths for a serialized run written to disk."""

    directory: Path
    document: Path
    report: Path
    cover_letter: Path | None
    review: Path | None


def _posting_directory(root: Path, slug: str, posting_id: str) -> Path:
    """Return the run directory for one posting; the identifier keeps same-title ones apart."""
    return root / f"{slug}--{posting_id}"


def screening_directory(root: Path, slug: str, posting_id: str) -> Path:
    """Return a fresh directory for one screening of this posting.

    A screening is not a run: it drafts nothing and is worth repeating whenever
    a posting is reconsidered. It still belongs to the same posting.
    """
    directory = _posting_directory(root, slug, posting_id) / "screenings" / _timestamp()
    suffix = 1
    while directory.exists():
        directory = directory.with_name(f"{_timestamp()}-{suffix}")
        suffix += 1
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def existing_run(root: Path, slug: str, identity: RunIdentity) -> Path | None:
    """Return a completed run with this exact identity, if one is already here.

    Checked before spending anything. Every field of ``RunIdentity`` is
    compared, the package version included: a run made by a different version
    was made by different code.
    """
    runs = _posting_directory(root, slug, identity.posting_id) / "runs"
    if not runs.is_dir():
        return None
    for candidate in sorted(runs.iterdir(), reverse=True):
        manifest = candidate / MANIFEST
        if not manifest.is_file():
            continue
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        # A manifest holding valid JSON that is not an object has no fields to compare.
        if not isinstance(payload, dict):
            continue
        inputs = payload.get("inputs", {})
        if (
            payload.get("run", {}).get("outcome") != "incomplete"
            and inputs.get("cv_hash") == identity.cv_hash
            and inputs.get("prompt_suite") == identity.prompt_suite
            and inputs.get("settings_hash") == identity.settings_hash
            and bool(inputs.get("cover_letter")) == identity.cover_letter
            and str(inputs.get("research_hash", "")) == identity.research_hash
            and int(inputs.get("trimmed", 0)) == identity.trimmed
            # A manifest with no recorded approval matches only an unapproved identity.
            and str(inputs.get("approval", "")) == identity.approval
            # An unknown version cannot be treated as a match.
            and inputs.get("clearscout") == identity.version
            and bool(identity.code_revision)
            and inputs.get("code_revision") == identity.code_revision
        ):
            return candidate
    return None


def previous_run(root: Path, slug: str, posting_id: str) -> Path | None:
    """Return the most recent completed run for this posting, whatever its inputs."""
    runs = _posting_directory(root, slug, posting_id) / "runs"
    if not runs.is_dir():
        return None
    completed = [
        candidate
        for candidate in sorted(runs.iterdir(), reverse=True)
        if (candidate / MANIFEST).is_file()
    ]
    return completed[0] if completed else None


@contextmanager
def staged(root: Path, slug: str, identity: RunIdentity) -> Iterator[_Staging]:
    """Build a run somewhere private, then move it into place in one operation.

    The temporary directory is a sibling of the destination so that the move is
    a rename within one filesystem rather than a copy, which is what makes the
    promotion atomic.
    """
    runs = _posting_directory(root, slug, identity.posting_id) / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".staging-", dir=runs))
    staging = _Staging(directory=temporary, destination=runs / _stamp(identity))
    try:
        yield staging
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    staging.promote()


def _stamp(identity: RunIdentity) -> str:
    """Return the run directory name: when it ran, and what it ran on."""
    return f"{_timestamp()}-{identity.slug}"


def _timestamp() -> str:
    """Return the current instant in a form that sorts as it reads."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")


@dataclass
class _Staging:
    """A run under construction, and the place it will land when it is whole."""

    directory: Path
    destination: Path
    _artifacts: dict[str, bytes] = field(default_factory=dict, init=False, repr=False)

    def promote(self) -> Path:
        """Move the finished run into place, never over another run.

        Two runs starting inside the same second would otherwise share a name
        and the second would delete the first, so a taken name is answered by
        taking the next one rather than by clearing it.
        """
        destination = self.destination
        suffix = 1
        while destination.exists():
            destination = self.destination.with_name(f"{self.destination.name}-{suffix}")
            suffix += 1
        self.directory.replace(destination)
        self.destination = destination
        return destination

    def write(self, name: str, text: str) -> Path:
        """Write one artifact into the staging directory."""
        path = write_private(self.directory / name, text)
        self._artifacts[name] = text.encode("utf-8")
        return path

    def manifest(self, manifest: Manifest) -> Path:
        """Write the run manifest, hashing every artifact this run produced."""
        content, _payload = manifest.serialize(
            {name: body for name, body in self._artifacts.items() if name != MANIFEST}
        )
        return self.write(MANIFEST, content.decode("utf-8"))


def _digest_bytes(content: bytes) -> str:
    """Return the short content digest recorded in a run manifest."""
    return hashlib.blake2s(content, digest_size=8).hexdigest()
