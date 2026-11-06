"""Grade a screenshot transcript by how many of the source's words it kept.

Recall is over production's own comparable-word set, ``importer.tokens``, so a
fold or punctuation change the importer ignores is ignored here too, and the
source's ligatures are split as the shipped reader splits them. A row may also
set the fewest lines the reader must return.
"""

from __future__ import annotations

from typing import Any

from clearscout.application.master_cv.sources import LIGATURES
from clearscout.application.tailoring.pipeline.importer import tokens
from evals.support.vision_render import source_text


def _declared(sample: dict[str, Any]) -> tuple[float, int]:
    expect = sample.get("expect")
    recall = expect.get("min_recall") if isinstance(expect, dict) else None
    if not isinstance(recall, int | float) or not 0 < recall <= 1:
        raise ValueError("a vision sample must declare expect.min_recall in (0, 1]")
    lines = expect.get("min_lines", 0) if isinstance(expect, dict) else 0
    if not isinstance(lines, int) or lines < 0:
        raise ValueError("a vision sample's expect.min_lines must be a whole number")
    return float(recall), lines


def token_recall() -> Any:
    """Return the scorer for one transcribed screenshot."""

    def score(sample: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
        floor, fewest = _declared(sample)
        wanted = tokens(source_text(str(sample["input"]["source"])).translate(LIGATURES))
        transcript = output["transcript"]
        missing = sorted(wanted - tokens("\n".join(transcript)))
        recall = 1 - len(missing) / len(wanted)
        lines = int(output["line_count"])
        passed = recall >= floor and lines >= fewest
        return {
            "value": passed,
            "recall": round(recall, 4),
            "metadata": {
                "source_tokens": len(wanted),
                "missing": missing,
                "line_count": lines,
                "min_recall": floor,
                "min_lines": fewest,
            },
            "explanation": (
                f"recall {recall:.3f} (floor {floor}), {lines} line(s) (fewest {fewest})"
                + (f"; missing {', '.join(missing[:12])}" if missing else "")
            ),
        }

    # The runner checks every row's declarations with this before it calls a solver.
    score.validate_sample = _declared  # type: ignore[attr-defined]
    # The live smoke refuses an output lacking any of these fields.
    score.required_fields = ("transcript", "line_count")  # type: ignore[attr-defined]
    return score
