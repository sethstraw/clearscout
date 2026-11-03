"""A pipeline runner that answers without a provider, a CV, or a paid call."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from clearscout.application.tailoring.pipeline import SerializedRunArtifacts, StoredMasterCv
from clearscout.application.tailoring.stages.jobspec import StoredPostingName
from clearscout.application.tailoring.workflows import BatchOutput, ScreenOutput, TailorOutput
from clearscout.domain import ArtifactLogicalType, TailoringOutcome
from clearscout.shared.costs import CallManifest, Ledger, Usage


def serialized_artifacts(
    files: Mapping[ArtifactLogicalType, tuple[str, bytes]],
    *,
    manifest: dict[str, Any],
    review: dict[str, Any],
) -> SerializedRunArtifacts:
    """Build the exact pipeline package used by durable workflow tests."""
    return SerializedRunArtifacts(
        document=files.get("resume.markdown", ("text/markdown", b""))[1],
        report=files.get("tailoring.report.markdown", ("text/markdown", b""))[1],
        manifest_json=files.get("tailoring.manifest.json", ("application/json", b"{}\n"))[1],
        manifest=manifest,
        cover_letter=(
            files["cover_letter.markdown"][1] if "cover_letter.markdown" in files else None
        ),
        review_json=(
            files["tailoring.review.json"][1] if "tailoring.review.json" in files else None
        ),
        review_markdown=(
            files["tailoring.review.markdown"][1] if "tailoring.review.markdown" in files else None
        ),
        review=review,
    )


class ScriptedPipelineRunner:
    """Return fixed screening, tailoring, and batch output, recording the approval."""

    def __init__(self) -> None:
        self.last_approval: dict[str, Any] | None = None
        self.last_research: tuple[Any, ...] = ()
        self.last_stored_name: StoredPostingName | None = None
        self.last_stored_cv: StoredMasterCv | None = None
        self.last_stored_names: tuple[StoredPostingName, ...] | None = None
        self.last_active_disputes: str | None = None

    def screen(self, *args: Any, **kwargs: Any) -> ScreenOutput:
        del args, kwargs
        ledger = Ledger()
        ledger.record(CallManifest("extract", "model-a", Usage(input_tokens=10, output_tokens=3)))
        return ScreenOutput(b"# Screening\n", "pursue", ledger)

    def tailor(self, *args: Any, **kwargs: Any) -> TailorOutput:
        del args
        self.last_approval = kwargs.get("approval")
        self.last_research = tuple(kwargs.get("research", ()))
        self.last_stored_name = kwargs.get("stored_name")
        self.last_stored_cv = kwargs.get("stored_cv")
        self.last_active_disputes = kwargs.get("active_disputes")
        ledger = Ledger()
        ledger.record(CallManifest("draft", "model-a", Usage(input_tokens=20, output_tokens=8)))
        review = {
            "proposals": [
                {
                    "id": "p01",
                    "slot": "summary",
                    "text": "Built systems.",
                    "evidence_ids": ["E1"],
                    "digest": "a" * 32,
                }
            ]
        }
        return TailorOutput(
            artifacts=serialized_artifacts(
                {
                    "resume.markdown": ("text/markdown", b"# Candidate\nBuilt systems.\n"),
                    "tailoring.review.json": (
                        "application/json",
                        (json.dumps(review) + "\n").encode("utf-8"),
                    ),
                },
                manifest={
                    "inputs": {
                        "prompt_suite": "prompt-a",
                        "cv_hash": "cv-a",
                        "settings_hash": "settings-a",
                    }
                },
                review=review,
            ),
            outcome=TailoringOutcome.PASSED,
            ledger=ledger,
        )

    def compare(self, *args: Any, **kwargs: Any) -> Any:
        self.last_stored_names = kwargs.get("stored_names")
        sources = args[0]
        return BatchOutput(
            report=b"# Batch comparison\n",
            record=b'{"schema_version": 1}\n',
            screenings=tuple(
                ScreenOutput(f"# Screening {position}\n".encode(), "pursue", Ledger())
                for position, _ in enumerate(sources, 1)
            ),
        )
