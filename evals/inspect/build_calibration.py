"""Emit each workspace ruling paired with the current stored classification.

The output is calibration data, not a scoring dataset. A user's ruling is an
override on the *previous* prompt's judgment, so a downstream that treats the
ruling as an "expected" label trains itself to the last mistake
(bandit-feedback bias). Each row here carries the raw pair; converting it into
a scoring set requires separate blinded review and adjudication.

The tool is read-only against the workspace database. It requires an explicit
output path outside the public repository and skips a ruling whose
``requirement_key`` no longer has a ``posting_requirements`` row.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from clearscout.infrastructure.artifacts.runtime import RuntimeStorage
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models.discovery import PostingUrl
from clearscout.infrastructure.persistence.models.evaluations import (
    JobEvaluation,
    MatchEvidence,
    PostingRequirement,
)
from clearscout.infrastructure.persistence.models.workspaces import (
    MasterCvVersion,
    RequirementRuling,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

_SUPPORTING = frozenset({"DIRECT", "TAILORING_ONLY", "TRANSFERABLE", "ASSERTED"})


def _resolve_output(output: Path) -> Path:
    """Return an absolute output path outside the public repository."""
    resolved = output.expanduser().resolve()
    if resolved == REPO_ROOT or resolved.is_relative_to(REPO_ROOT):
        raise ValueError(f"Calibration output must stay outside the public repository: {resolved}")
    return resolved


def _cv_hash(session: Session, evaluation_id: str) -> str:
    """Return the CV content hash the evaluation was scored against, or ``''``."""
    row = session.execute(
        select(MasterCvVersion.content_sha256)
        .join(JobEvaluation, JobEvaluation.master_cv_version_id == MasterCvVersion.id)
        .where(JobEvaluation.id == evaluation_id)
    ).first()
    return row[0] if row else ""


def _newest_requirement_row(
    session: Session, requirement_key: str
) -> tuple[PostingRequirement, JobEvaluation] | None:
    """Return the newest posting_requirements row for the requirement, with its evaluation."""
    row = session.execute(
        select(PostingRequirement, JobEvaluation)
        .join(JobEvaluation, JobEvaluation.id == PostingRequirement.job_evaluation_id)
        .where(PostingRequirement.requirement_key == requirement_key)
        .order_by(JobEvaluation.version_number.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    return row[0], row[1]


def _current_classification(session: Session, *, evaluation_id: str, requirement_key: str) -> str:
    """Return the stored classification for this requirement in this evaluation."""
    citations = session.execute(
        select(MatchEvidence.classification)
        .where(MatchEvidence.evaluation_id == evaluation_id)
        .where(MatchEvidence.requirement_key == requirement_key)
    ).all()
    supporting = [row[0] for row in citations if row[0] in _SUPPORTING]
    if supporting:
        return str(supporting[0])
    return "GAP"


def _first_posting_url(session: Session, posting_id: str) -> str:
    """Return one URL associated with the posting, or ``''`` if none is recorded."""
    row = session.execute(
        select(PostingUrl.url).where(PostingUrl.job_posting_id == posting_id).limit(1)
    ).first()
    return row[0] if row else ""


def _records(session: Session, database_url: str) -> Iterable[dict[str, object]]:
    """Yield one raw pair per ruling that still has a matching requirement."""
    rulings = session.execute(
        select(
            RequirementRuling.requirement_key,
            RequirementRuling.ruling,
        )
    ).all()
    for requirement_key, ruling in rulings:
        pair = _newest_requirement_row(session, requirement_key)
        if pair is None:
            continue
        requirement, evaluation = pair
        current = _current_classification(
            session,
            evaluation_id=evaluation.id,
            requirement_key=requirement_key,
        )
        yield {
            "id": f"cal-{requirement.job_posting_id}-{requirement_key}",
            "requirement_key": requirement_key,
            "requirement_text": requirement.text,
            "posting_id": requirement.job_posting_id,
            "posting_url": _first_posting_url(session, requirement.job_posting_id),
            "ruling": ruling,
            "current_classification": current,
            "dimension": "hard",
            "cv_hash": _cv_hash(session, evaluation.id),
            "database_url": database_url,
        }


def build(data_root: Path, output: Path) -> int:
    """Write the calibration file for one workspace. Return the record count written."""
    output_path = _resolve_output(output)
    storage = RuntimeStorage.resolve(data_root=data_root)
    database = Database(storage.database_url, require_existing=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    try:
        with (
            database.session() as session,
            output_path.open("w", encoding="utf-8") as handle,
        ):
            for record in _records(session, storage.database_url):
                handle.write(json.dumps(record, ensure_ascii=False))
                handle.write("\n")
                count += 1
    finally:
        database.dispose()
    return count


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evals.inspect.build_calibration",
        description=(
            "Emit each workspace ruling paired with the current classification, "
            "as calibration data (not gold labels)."
        ),
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        required=True,
        help="Application data root containing the workspace database.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Private destination JSONL path outside the public repository.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    count = build(args.data_root, args.output)
    print(f"Wrote {count} records to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
