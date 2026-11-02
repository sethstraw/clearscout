"""Validated master-CV writes and append-only discovery provenance."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from tailorcv.application.master_cv.cv import CvInventory, Entry, parse_cv
from tailorcv.infrastructure.artifacts.files import replace_private
from tailorcv.shared.errors import CvParseError, DiscoveryError

from .plan import Edit, plan
from .vocabulary import JOURNAL

if TYPE_CHECKING:
    from .conversation import Confirmed


@dataclass(frozen=True)
class Written:
    """What a confirmed write did: the file, the log, and the new identifiers."""

    master_cv: Path
    journal: Path
    entry_ids: tuple[str, ...]


def write(
    path: Path,
    confirmed: Sequence[Confirmed],
    *,
    inventory: CvInventory,
    at: datetime,
) -> Written:
    """Write confirmed material into the master CV, or refuse and change nothing.

    Everything is checked against a parse of the proposed document before a byte
    is written. The log is written first, because a provenance record for a line
    that is not there can be resolved by reading it and a line with no record
    cannot; the master CV is then replaced in one rename. The result has to
    parse, still carry every entry it carried before, and carry exactly the
    confirmed claims.
    """
    if not confirmed:
        raise DiscoveryError("Nothing was confirmed, so the master CV was not touched.")
    source = _read(path)
    edits = plan(source, inventory, confirmed)
    proposed = _public_rewritten(source, edits)
    after = _parses(path, proposed)
    added = _exactly_the_confirmed_claims(inventory, after, confirmed)
    journal_path = _log(path.parent, confirmed, added, at=at, master_cv=path)
    replace_private(path, proposed)
    return Written(
        master_cv=path, journal=journal_path, entry_ids=tuple(entry.id for entry in added)
    )


def _public_rewritten(source: str, edits: Sequence[Edit]) -> str:
    """Use the package rewrite boundary so write preflight remains replaceable."""
    from . import rewritten

    return rewritten(source, edits)


def _read(path: Path) -> str:
    """Return the master CV's current text, refusing what cannot be read."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise DiscoveryError(f"Cannot read the master CV {path}: {error}") from error


def _parses(path: Path, proposed: str) -> CvInventory:
    """Return the proposed document's inventory, refusing one that will not parse."""
    try:
        return parse_cv(path, text=proposed)
    except CvParseError as error:
        raise DiscoveryError(
            f"Adding that would leave a master CV this tool cannot read, so nothing was "
            f"written.\n  {error}"
        ) from error


def _exactly_the_confirmed_claims(
    before: CvInventory, after: CvInventory, confirmed: Sequence[Confirmed]
) -> tuple[Entry, ...]:
    """Return the new entries, refusing anything the change did not intend.

    Every identifier that existed still exists, so nothing was altered or dropped
    by an insertion landing in the wrong block, and the new identifiers are
    exactly the confirmed claims, each of the category and place confirmed for it.
    """
    lost = sorted(set(before.ids()) - set(after.ids()))
    if lost:
        raise DiscoveryError(
            f"That change would alter or remove {len(lost)} entry that is already in your "
            "master CV,\n"
            f"  starting with {lost[0]}. Nothing was written. This is a defect rather than "
            "something\n  to work around: report it with the master CV's shape around that "
            "entry."
        )
    added = tuple(entry for entry in after.entries if entry.id not in set(before.ids()))
    wanted = [item.material for item in confirmed]
    if len(added) != len(wanted):
        raise DiscoveryError(
            f"That change would add {len(added)} entry(s) and {len(wanted)} were confirmed.\n"
            "  Nothing was written."
        )
    for entry, material in zip(added, wanted, strict=True):
        if entry.text.strip() != material.text:
            raise DiscoveryError(
                f"The entry that would be added does not read as it was confirmed:\n"
                f"  confirmed: {material.text}\n  would add: {entry.text}"
            )
        if entry.kind is not material.category:
            raise DiscoveryError(
                f"{material.text!r} was confirmed as {material.category.value} and would be "
                f"read as {entry.kind.value}.\n  Nothing was written."
            )
        if material.placement.role_key and entry.role_key != material.placement.role_key:
            raise DiscoveryError(
                f"{material.text!r} was confirmed under {material.placement.role_key} and "
                f"would land under {entry.role_key or 'no role'}.\n  Nothing was written."
            )
    return added


def _log(
    directory: Path,
    confirmed: Sequence[Confirmed],
    added: Sequence[Entry],
    *,
    at: datetime,
    master_cv: Path,
) -> Path:
    """Append this session to the discovery log and return where it landed.

    Provenance the master CV cannot carry itself: an entry added here is
    indistinguishable in the CV from one that was always there.
    """
    path = directory / JOURNAL
    existing = path.read_text(encoding="utf-8") if path.is_file() else _log_header()
    body = [existing.rstrip(), "", f"## {at.isoformat(timespec='seconds')}", ""]
    body.append(f"Added to `{master_cv.name}` from confirmed discovery, not from the original")
    body.append("document. Every line below was typed by the person the CV is about.")
    for item, entry in zip(confirmed, added, strict=True):
        material = item.material
        body += [
            "",
            f"### `{entry.id}`  {material.subject}",
            "",
            f"- Wording: {material.text}",
            f"- Ownership: {material.level.value if material.level else 'none claimed'}"
            + (
                f", read from {', '.join(material.level_evidence)}"
                if material.level_evidence
                else ""
            ),
            f"- Metrics: {', '.join(material.metrics) or 'none stated'}",
            f"- Scope: {material.scope or 'not stated'}",
            f"- Category: {material.category.value}",
            f"- Placed under: {material.placement.named}",
            f"- Job gaps: {', '.join(material.gap_ids) or 'none named'}",
            f"- Postings: {', '.join(material.postings) or 'none named'}",
            f"- Ambiguous: {'; '.join(material.ambiguities) or 'nothing recorded'}",
            f"- Confirmed: {', '.join(point.value for point in item.points)}",
            "",
            "Questions and answers:",
            "",
        ]
        body += [f"    {reply.text}" for reply in material.replies]
    return replace_private(path, "\n".join(body).rstrip() + "\n")


def _log_header() -> str:
    """Return the log's opening explanation, written once."""
    return (
        "# Discovery log\n"
        "\n"
        "Every fact confirmed through `tailorcv discover` and added to the master CV,\n"
        "with the answers it was built from. Entries added this way are ordinary\n"
        "evidence once they are in the CV, so this file is the only record that they\n"
        "came from a conversation rather than from the original document.\n"
        "\n"
        "Nothing here was written by a model.\n"
    )
