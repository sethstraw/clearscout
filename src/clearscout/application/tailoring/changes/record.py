"""What a proposal is, and the digests that bind it to the run that made it."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from clearscout.application.tailoring.runs import RunIdentity

# Reject manifests from another contract version.
SCHEMA = 1

CHANGES_JSON = "changes.json"
CHANGES_MD = "changes.md"

ACCEPT = "accept"
REJECT = "reject"
# Written for a line a check named; an approval must replace it with accept or reject.
REVIEW = "review"

# Store human-readable reasons alongside hashes.
INPUT_NAMES = {
    "posting_id": "the posting",
    "cv_hash": "the master CV",
    "prompt_suite": "the prompts",
    "settings_hash": "the settings",
    "clearscout": "the clearscout version",
    "code_revision": "the source build",
    "cover_letter": "whether a cover letter was asked for",
    "trimmed": "how many items were trimmed",
}


class Kind(StrEnum):
    """What happened to a piece of the record on the way to the page."""

    NEW = "new"
    REFRAMED = "reframed"
    PRESERVED = "preserved"
    OMITTED = "omitted"


@dataclass(frozen=True)
class Excerpt:
    """One source and its exact text.

    ``source`` is an entry id, else a role key, the master CV's header, or the settings rules.
    """

    source: str
    text: str


@dataclass(frozen=True)
class Proposal:
    """One reviewable line: what it says, what it rests on, and why it is here."""

    id: str
    slot: str
    kind: Kind
    immutable: bool
    text: str
    evidence_ids: tuple[str, ...]
    evidence: tuple[Excerpt, ...]
    requirement_ids: tuple[str, ...]
    rationale: str
    digest: str = ""

    def content(self) -> dict[str, Any]:
        """Return everything the digest covers, in the order it is hashed."""
        return {
            "id": self.id,
            "slot": self.slot,
            "kind": self.kind.value,
            "immutable": self.immutable,
            "text": self.text,
            "evidence_ids": list(self.evidence_ids),
            "evidence": [[item.source, item.text] for item in self.evidence],
            "requirement_ids": list(self.requirement_ids),
            "rationale": self.rationale,
        }

    def payload(self) -> dict[str, Any]:
        """Return the proposal as it lands in ``changes.json``."""
        return {
            **self.content(),
            "evidence": [{"source": item.source, "text": item.text} for item in self.evidence],
            "digest": self.digest,
            "decision": ACCEPT,
        }


@dataclass(frozen=True)
class Review:
    """Every proposal from one draft, and the digest that binds the set."""

    run: dict[str, Any]
    proposals: tuple[Proposal, ...]
    digest: str

    def by_id(self) -> dict[str, Proposal]:
        """Return the proposals keyed by identifier."""
        return {proposal.id: proposal for proposal in self.proposals}

    def payload(self) -> dict[str, Any]:
        """Return the whole review as it lands in ``changes.json``."""
        return {
            "schema": SCHEMA,
            "run": self.run,
            "digest": self.digest,
            "proposals": [proposal.payload() for proposal in self.proposals],
        }


def _run_payload(identity: RunIdentity) -> dict[str, Any]:
    """Return what a proposal was derived from, as the digest sees it.

    ``RunIdentity.approval`` is deliberately absent: a digest that moved when an
    approval was applied could never be checked against the approval that moved
    it. Everything else in the identity is here.
    """
    return {
        "posting_id": identity.posting_id,
        "cv_hash": identity.cv_hash,
        "prompt_suite": identity.prompt_suite,
        "settings_hash": identity.settings_hash,
        "clearscout": identity.version,
        "code_revision": identity.code_revision,
        "cover_letter": identity.cover_letter,
        "trimmed": identity.trimmed,
    }


def digest_of(run: dict[str, Any], content: dict[str, Any]) -> str:
    """Return the digest binding one proposal to the run that produced it."""
    return digest({"schema": SCHEMA, "run": run, "proposal": content})


def set_digest(run: dict[str, Any], digests: tuple[str, ...]) -> str:
    """Return the digest binding the whole ordered set, which catches one added or dropped."""
    return digest({"schema": SCHEMA, "run": run, "proposals": list(digests)})


def digest(payload: dict[str, Any]) -> str:
    """Hash ``payload`` canonically, with sorted keys and fixed separators."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.blake2s(encoded.encode("utf-8"), digest_size=16).hexdigest()
