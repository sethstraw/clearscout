"""Identity for a statement a versioned document makes.

A master-CV entry and a posting requirement are both statements read out of a
document that is written again over time. Each is addressed by its content, so
rewording one renames it and every citation to it stops resolving. Carrying an
identity forward is the same rule for both, and it lives here once.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

from tailorcv.domain.identity import normalize_text

# Below this similarity, two statements under one scope are separate claims.
SAME_STATEMENT = 0.8


@dataclass(frozen=True)
class Stated:
    """One statement a document makes, and the scope it makes it under."""

    id: str
    scope: str
    text: str


@dataclass(frozen=True)
class Known(Stated):
    """A statement a stored version holds, and the identity it carries."""

    key: str


def carried_identities(previous: Sequence[Known], current: Sequence[Stated]) -> dict[str, str]:
    """Return the identity each current statement inherits, keyed by its id.

    A statement keeps its identity through its content address while its wording
    and scope both hold, and through scope and similarity once it is reworded in
    place. A statement matching neither is absent from the result, so its caller
    starts a new identity rather than claiming evidence the document dropped.
    """
    inherited: dict[str, str] = {}
    by_id = {statement.id: statement for statement in previous}
    unclaimed = {statement.key for statement in previous}

    outstanding: list[Stated] = []
    for statement in current:
        known = by_id.get(statement.id)
        if known is not None and known.key in unclaimed:
            inherited[statement.id] = known.key
            unclaimed.discard(known.key)
        else:
            outstanding.append(statement)

    available = [statement for statement in previous if statement.key in unclaimed]
    inherited.update(_reworded_identities(outstanding, available, set(inherited)))
    return inherited


def _reworded_identities(
    outstanding: Sequence[Stated], available: Sequence[Known], settled: set[str]
) -> dict[str, str]:
    """Return the identity each reworded statement claims, closest wording first."""
    pairs = sorted(
        (-ratio, statement.id, known.key)
        for statement in outstanding
        for known in available
        if known.scope == statement.scope
        and (ratio := SequenceMatcher(None, known.text, statement.text).ratio()) >= SAME_STATEMENT
    )
    matched: dict[str, str] = {}
    claimed: set[str] = set()
    for _, statement_id, key in pairs:
        if statement_id in settled or statement_id in matched or key in claimed:
            continue
        matched[statement_id] = key
        claimed.add(key)
    return matched


def derived_proposal_identity(text: str) -> str:
    """Return a proposal identity that rows with matching normalized text share."""
    return hashlib.blake2s(normalize_text(text).encode("utf-8"), digest_size=8).hexdigest()
