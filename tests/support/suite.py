"""Fixed facts every test can rely on: where the fixtures live, and one posting."""

from __future__ import annotations

from pathlib import Path

from tailorcv.application.config import ProviderName, Settings
from tailorcv.infrastructure.providers.credentials import ENVIRONMENT_VARIABLES

# Anchored once, because a test that counts its own parent directories breaks
# when it moves.
REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS = Path(__file__).resolve().parents[1]
FIXTURES = TESTS / "fixtures"
CASSETTES = TESTS / "cassettes"

# Spelled out rather than imported from `config`, so moving the default fails
# one named test instead of dragging the suite along silently.
SUITE_PROVIDER = ProviderName.OPENAI
SUITE_KEY_VARIABLE = ENVIRONMENT_VARIABLES[SUITE_PROVIDER]

# A fictional employer, long enough that ingest treats it as a job
# description rather than a fragment.
POSTING = (
    "Staff Data Platform Engineer at Globex. "
    "You will own streaming ingestion, schema governance, and data lineage. "
    "Required: Kafka, Terraform, PostgreSQL, and eight years of experience. "
    "Preferred: schema registry design and data quality tooling. "
    "Remote within the United States. No visa sponsorship is available. "
) * 4


def settings_for(master_cv: Path, tmp_path: Path, pages: int = 2) -> Settings:
    """Return settings for one run against a CV and a temporary output directory."""
    return Settings(cv=master_cv, output_dir=tmp_path / "runs", pages=pages)


__all__ = [
    "CASSETTES",
    "FIXTURES",
    "POSTING",
    "REPO_ROOT",
    "SUITE_KEY_VARIABLE",
    "SUITE_PROVIDER",
    "TESTS",
    "settings_for",
]
