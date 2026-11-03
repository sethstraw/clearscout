"""A run is filed under its posting, complete or absent, and never overwritten.

These are storage guarantees rather than content ones.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clearscout.application.tailoring.runs import (
    MANIFEST,
    Manifest,
    RunIdentity,
    existing_run,
    previous_run,
    screening_directory,
    staged,
)


def identity(**overrides: object) -> RunIdentity:
    """Build a run identity, with everything that decides the output set."""
    base: dict[str, object] = {
        "posting_id": "gh-123",
        "cv_hash": "c" * 16,
        "prompt_suite": "p" * 12,
        "settings_hash": "s" * 16,
        "version": "0.1.0",
        "code_revision": "sha256:synthetic-build",
    }
    return RunIdentity(**{**base, **overrides})  # type: ignore[arg-type]


def finish(root: Path, ident: RunIdentity, *, outcome: str = "passed") -> Path:
    """Write one complete run and return where it landed."""
    with staged(root, "Acme_Engineer", ident) as staging:
        staging.write("Acme_Engineer.md", "resume")
        staging.manifest(
            Manifest(
                identity=ident,
                posting_url="https://example.com/jobs/1",
                posting_origin="requisition",
                retrieved="2026-08-05",
                jd_hash="j" * 12,
                started="2026-08-05T12:00:00+00:00",
                coverage=0.0,
                outcome=outcome,
            )
        )
    return staging.destination


def test_every_run_lives_under_its_posting(tmp_path: Path) -> None:
    """Not a dated sibling. A second run is history, not a second application."""
    landed = finish(tmp_path, identity())
    assert landed.parent.name == "runs"
    assert landed.parent.parent.name == "Acme_Engineer--gh-123"


def test_an_identical_run_is_found_rather_than_repeated(tmp_path: Path) -> None:
    landed = finish(tmp_path, identity())
    assert existing_run(tmp_path, "Acme_Engineer", identity()) == landed


def test_a_changed_input_is_not_an_identical_run(tmp_path: Path) -> None:
    finish(tmp_path, identity())
    for changed in ("cv_hash", "prompt_suite", "settings_hash", "version"):
        assert existing_run(tmp_path, "Acme_Engineer", identity(**{changed: "different"})) is None


def test_a_manifest_written_before_the_version_was_recorded_is_not_reused(
    tmp_path: Path,
) -> None:
    """An unknown version is exactly the case where the code cannot be vouched for."""
    landed = finish(tmp_path, identity())
    payload = json.loads((landed / MANIFEST).read_text(encoding="utf-8"))
    del payload["inputs"]["clearscout"]
    (landed / MANIFEST).write_text(json.dumps(payload), encoding="utf-8")
    assert existing_run(tmp_path, "Acme_Engineer", identity()) is None


def test_a_manifest_holding_valid_json_that_is_not_an_object_is_skipped(
    tmp_path: Path,
) -> None:
    """A JSON array parses cleanly and then has no fields to compare."""
    first = finish(tmp_path, identity())
    latest = finish(tmp_path, identity())
    (latest / MANIFEST).write_text(json.dumps(["not", "a", "manifest"]), encoding="utf-8")
    assert existing_run(tmp_path, "Acme_Engineer", identity()) == first


def test_a_cover_letter_makes_it_a_different_run(tmp_path: Path) -> None:
    """Reuse must not hand back a package of the wrong shape."""
    finish(tmp_path, identity())
    assert existing_run(tmp_path, "Acme_Engineer", identity(cover_letter=True)) is None


def test_a_trimmed_run_is_a_different_run(tmp_path: Path) -> None:
    """A shorter resume is a different resume."""
    finish(tmp_path, identity())
    assert existing_run(tmp_path, "Acme_Engineer", identity(trimmed=2)) is None


def test_an_incomplete_run_is_never_reused(tmp_path: Path) -> None:
    finish(tmp_path, identity(), outcome="incomplete")
    assert existing_run(tmp_path, "Acme_Engineer", identity()) is None


def test_an_interrupted_run_leaves_no_run_directory_behind(tmp_path: Path) -> None:
    """A crash before promotion must not leave a partial package behind."""

    def interrupt_after_writing() -> None:
        with staged(tmp_path, "Acme_Engineer", identity()) as staging:
            staging.write("Acme_Engineer.md", "half a resume")
            raise RuntimeError("interrupted")

    with pytest.raises(RuntimeError):
        interrupt_after_writing()

    runs = tmp_path / "Acme_Engineer--gh-123" / "runs"
    assert list(runs.iterdir()) == [], "no staging directory and no partial run"


def test_promotion_never_lands_on_an_existing_run(tmp_path: Path) -> None:
    """Two runs starting in the same second must not have one delete the other."""
    first = finish(tmp_path, identity())
    (first / "my-edit.txt").write_text("mine", encoding="utf-8")
    second = finish(tmp_path, identity())

    assert second != first
    assert (first / "my-edit.txt").read_text(encoding="utf-8") == "mine"


def test_nothing_from_an_earlier_run_can_appear_inside_a_later_one(tmp_path: Path) -> None:
    """A run directory holds exactly the artifacts of the run that made it."""
    with staged(tmp_path, "Acme_Engineer", identity()) as staging:
        staging.write("Acme_Engineer.md", "resume")
        staging.write("Cover_Letter.md", "letter")
        staging.manifest(_manifest(identity()))
    withletter = staging.destination

    with staged(tmp_path, "Acme_Engineer", identity()) as staging:
        staging.write("Acme_Engineer.md", "resume")
        staging.manifest(_manifest(identity()))
    without = staging.destination

    assert (withletter / "Cover_Letter.md").is_file()
    assert not (without / "Cover_Letter.md").exists()


def test_the_manifest_hashes_every_artifact(tmp_path: Path) -> None:
    """A later reader can prove a file has not been edited since the run."""
    landed = finish(tmp_path, identity())
    payload = json.loads((landed / MANIFEST).read_text(encoding="utf-8"))
    assert set(payload["artifacts"]) == {"Acme_Engineer.md"}
    assert payload["posting"]["id"] == "gh-123"
    assert payload["inputs"]["clearscout"] == "0.1.0"
    assert payload["run"]["outcome"] == "passed"


def test_the_previous_run_is_found_whatever_its_inputs(tmp_path: Path) -> None:
    """Comparison needs the last run, not the last identical one."""
    finish(tmp_path, identity())
    later = finish(tmp_path, identity(cv_hash="edited"))
    assert previous_run(tmp_path, "Acme_Engineer", "gh-123") == later


def test_a_screening_is_filed_under_the_same_posting_as_its_runs(tmp_path: Path) -> None:
    """A screening belongs to the posting it screened, not to the day it ran."""
    landed = finish(tmp_path, identity())
    screening = screening_directory(tmp_path, "Acme_Engineer", "gh-123")
    posting = tmp_path / "Acme_Engineer--gh-123"
    assert screening.parent == posting / "screenings"
    assert landed.parent == posting / "runs"


def test_two_screenings_of_one_posting_do_not_collide(tmp_path: Path) -> None:
    """Re-screening is cheap and expected, so the second must not land on the first."""
    first = screening_directory(tmp_path, "Acme_Engineer", "gh-123")
    (first / "Screening_Report.md").write_text("first", encoding="utf-8")
    second = screening_directory(tmp_path, "Acme_Engineer", "gh-123")
    assert second != first
    assert (first / "Screening_Report.md").read_text(encoding="utf-8") == "first"


def _manifest(ident: RunIdentity) -> Manifest:
    return Manifest(
        identity=ident,
        posting_url="https://example.com/jobs/1",
        posting_origin="requisition",
        retrieved="2026-08-05",
        jd_hash="j" * 12,
        started="2026-08-05T12:00:00+00:00",
        coverage=0.0,
        outcome="passed",
    )


def test_editing_the_length_budget_is_not_a_reusable_run() -> None:
    """The run identity covers the length budget, not only the declared settings.

    Otherwise reuse would hand back a package built to the previous budget and
    report that nothing had changed.
    """
    from clearscout.application.tailoring.layout import SHAPING_CONSTANTS, shaping_fingerprint

    assert shaping_fingerprint() == shaping_fingerprint(), "a fingerprint must be stable"
    assert len(shaping_fingerprint()) == 12

    changed = dict(SHAPING_CONSTANTS)
    changed["LINES_PER_PAGE"] = changed["LINES_PER_PAGE"] + 1
    assert _fingerprint_of(changed) != shaping_fingerprint()


def test_the_fingerprint_covers_every_calibration_constant() -> None:
    """A constant left out is one the reuse check silently stops noticing."""
    import clearscout.application.tailoring.layout as layout_module
    from clearscout.application.tailoring.layout import SHAPING_CONSTANTS

    declared = {
        name
        for name, value in vars(layout_module).items()
        if name.isupper() and isinstance(value, int | float) and not isinstance(value, bool)
    }
    assert declared == set(SHAPING_CONSTANTS), (
        "layout constants and the shaping fingerprint have drifted apart: "
        f"{sorted(declared ^ set(SHAPING_CONSTANTS))}"
    )


def test_the_fingerprint_does_no_filesystem_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """It has to hold under a packaging that does not lay the source out as files."""
    from clearscout.application.tailoring.layout import shaping_fingerprint

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("shaping_fingerprint read a file")

    monkeypatch.setattr(Path, "read_bytes", refuse)
    monkeypatch.setattr(Path, "read_text", refuse)
    assert shaping_fingerprint()


def test_the_fingerprint_does_not_move_when_shaping_logic_does() -> None:
    """Rewriting the function that reads a constant leaves the fingerprint where it was.

    A pure fingerprint does no filesystem work; `--force` covers development and
    `RunIdentity.version` covers a release.
    """
    import clearscout.application.tailoring.layout as layout_module
    from clearscout.application.tailoring.layout import shaping_fingerprint

    before = shaping_fingerprint()
    original = layout_module.wrapped_lines
    try:
        layout_module.wrapped_lines = lambda text: 0
        assert shaping_fingerprint() == before
    finally:
        layout_module.wrapped_lines = original


def _fingerprint_of(constants: dict[str, float]) -> str:
    """Recompute a fingerprint from an altered constant table."""
    import hashlib
    import json

    payload = json.dumps(constants, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def test_the_settings_hash_moves_when_the_shaping_constants_do(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It is the settings hash that carries this into the run identity."""
    from clearscout.application.config import Settings
    from clearscout.application.tailoring.runs import settings_hash

    settings = Settings(pages=2)
    before = settings_hash(settings)
    monkeypatch.setattr(
        "clearscout.application.tailoring.runs.shaping_fingerprint", lambda: "different123"
    )
    assert settings_hash(settings) != before


def test_same_release_version_does_not_reuse_a_different_build(tmp_path):
    original = identity(code_revision="sha256:first")
    finish(tmp_path, original)
    assert existing_run(tmp_path, "Acme_Engineer", identity(code_revision="sha256:second")) is None


def test_a_legacy_unknown_build_is_not_invented_during_reuse(tmp_path):
    landed = finish(tmp_path, identity())
    path = landed / MANIFEST
    payload = json.loads(path.read_text())
    payload["inputs"].pop("code_revision", None)
    path.write_text(json.dumps(payload))
    assert existing_run(tmp_path, "Acme_Engineer", identity()) is None
