"""Finding the master CV without being told where it is."""

from __future__ import annotations

from pathlib import Path

import pytest

from clearscout.application.master_cv.discovery import candidates, reads_as_cv, resolve_master_cv
from clearscout.shared.errors import ConfigError


def place(directory: Path, name: str, source: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def test_a_conventionally_named_cv_is_found(tmp_path: Path, master_cv: Path) -> None:
    expected = place(tmp_path, "CV_Master.md", master_cv)
    assert resolve_master_cv(None, tmp_path) == expected


def test_a_cv_in_the_cv_folder_is_found(tmp_path: Path, master_cv: Path) -> None:
    expected = place(tmp_path / "cv", "CV_Master.md", master_cv)
    assert resolve_master_cv(None, tmp_path) == expected


def test_a_lone_markdown_file_in_a_cv_folder_is_found(tmp_path: Path, master_cv: Path) -> None:
    expected = place(tmp_path / "cv", "Whatever_I_Called_It.md", master_cv)
    assert resolve_master_cv(None, tmp_path) == expected


def test_a_readme_is_not_mistaken_for_a_cv(tmp_path: Path) -> None:
    """The parser is the test, not the filename."""
    (tmp_path / "cv").mkdir()
    (tmp_path / "cv" / "README.md").write_text("# Notes\n\nSome notes.\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="No master CV found"):
        resolve_master_cv(None, tmp_path)


def test_reads_as_cv_rejects_a_thin_document(tmp_path: Path) -> None:
    path = tmp_path / "thin.md"
    path.write_text("# A B\n\na@example.com\n\n## Summary\n\nShort.\n", encoding="utf-8")
    assert not reads_as_cv(path)


def test_ambiguity_asks_rather_than_guesses(tmp_path: Path, master_cv: Path) -> None:
    place(tmp_path, "CV_Master.md", master_cv)
    place(tmp_path / "cv", "CV.md", master_cv)
    with pytest.raises(ConfigError, match="Several files read as a master CV"):
        resolve_master_cv(None, tmp_path)


def test_nothing_found_says_exactly_where_it_looked(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        resolve_master_cv(None, tmp_path)
    message = str(caught.value)
    assert "CV_Master.md" in message
    assert "--cv PATH" in message
    assert "clearscout init" in message


def test_an_explicit_path_skips_discovery_entirely(tmp_path: Path, master_cv: Path) -> None:
    place(tmp_path, "CV_Master.md", master_cv)
    assert resolve_master_cv(master_cv, tmp_path) == master_cv.resolve()


def test_an_explicit_path_that_is_absent_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Master CV not found"):
        resolve_master_cv(tmp_path / "absent.md", tmp_path)


def test_candidates_are_deduplicated(tmp_path: Path, master_cv: Path) -> None:
    place(tmp_path / "cv", "CV_Master.md", master_cv)
    assert len(candidates(tmp_path)) == 1
