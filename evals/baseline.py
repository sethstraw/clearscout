"""Read a baseline and say how a run differs from it. Nothing here writes.

A failing evaluation must never be able to rewrite the baseline it failed
against, so this module opens files for reading only and imports nothing that
writes. A run whose prompt suite or response schemas differ from the baseline's
is reported as a difference rather than absorbed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NamedTuple

BASELINES = Path(__file__).parent / "baselines"


class Difference(NamedTuple):
    """One place a run and its baseline disagree, named so it can be acted on."""

    case: str
    scorer: str
    expected: str
    actual: str

    def line(self) -> str:
        """Render the difference as one readable line."""
        return f"{self.case}: {self.scorer}: expected {self.expected}, got {self.actual}"


def path_for(case: str, directory: Path = BASELINES) -> Path:
    """Return where a case's baseline lives."""
    return directory / f"{case}.json"


def load(case: str, directory: Path = BASELINES) -> dict[str, Any] | None:
    """Return a recorded baseline, or ``None`` when the case has never been recorded."""
    path = path_for(case, directory)
    if not path.is_file():
        return None
    payload: Any = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None


def compare(case: str, baseline: dict[str, Any], scores: dict[str, Any]) -> list[Difference]:
    """Return every scorer whose value moved, plus any that appeared or vanished.

    A scorer the baseline does not know about is reported rather than ignored:
    a new measurement is a change to the instrument, and a reviewer should see
    it land rather than find it already there.
    """
    recorded = baseline.get("scores", {})
    recorded = recorded if isinstance(recorded, dict) else {}
    differences: list[Difference] = []
    for name in sorted(set(recorded) | set(scores)):
        if name not in recorded:
            differences.append(Difference(case, name, "not recorded", _show(scores[name])))
        elif name not in scores:
            differences.append(Difference(case, name, _show(recorded[name]), "not measured"))
        elif _show(recorded[name]) != _show(scores[name]):
            differences.append(Difference(case, name, _show(recorded[name]), _show(scores[name])))
    return differences


def provenance(baseline: dict[str, Any], current: dict[str, str]) -> list[tuple[str, str, str]]:
    """Return the provenance fields on which a baseline and a run disagree."""
    return [
        (field, str(baseline.get(field, "not recorded")), value)
        for field, value in sorted(current.items())
        if str(baseline.get(field, "not recorded")) != value
    ]


def _show(value: Any) -> str:
    """Render a scorer value the same way whether it came from JSON or from a run."""
    return json.dumps(value, sort_keys=True)
