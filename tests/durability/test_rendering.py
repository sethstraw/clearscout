"""Handing the Markdown to a renderer, without depending on one.

tailorcv does not render. The renderer is found on PATH or it is not, it is
asked with a fixed argument list, and whatever it does the Markdown survives. A
stub executable stands in for pressume, so the suite needs neither it nor Typst.
"""

from __future__ import annotations

import json
import os
import stat
import tomllib
from pathlib import Path

import pytest

from tailorcv.application.tailoring import layout
from tailorcv.infrastructure.artifacts import rendering
from tests.support.suite import REPO_ROOT


def stub(directory: Path, script: str) -> None:
    """Put an executable named like the renderer at the front of PATH."""
    path = directory / rendering.RENDERER
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def test_nothing_is_rendered_when_the_renderer_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absent renderer is a one-line note, never a failed run."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    document = tmp_path / "Resume.md"
    document.write_text("# Resume\n", encoding="utf-8")

    outcome = rendering.render(document)
    assert not outcome.attempted
    assert not outcome.ok
    assert rendering.RENDERER in outcome.message
    assert "Markdown is written and unaffected" in outcome.message
    assert document.read_text(encoding="utf-8") == "# Resume\n"


def test_a_successful_render_reports_what_it_produced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The files it wrote are named back, because that is what the user wants to know."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    document = run_dir / "Resume.md"
    document.write_text("# Resume\n", encoding="utf-8")

    # The stub writes beside the Markdown, which is what --output asks for, and leaves
    # the manifest pressume keeps for itself.
    stub(
        binaries,
        f'#!/bin/sh\ntouch "{run_dir}/Resume.pdf" "{run_dir}/Resume.txt" '
        f'"{run_dir}/.pressume-manifest.json"\nexit 0\n',
    )
    monkeypatch.setenv("PATH", f"{binaries}{os.pathsep}{os.environ['PATH']}")

    outcome = rendering.render(document)
    assert outcome.attempted
    assert outcome.ok
    assert {path.name for path in outcome.produced} == {"Resume.pdf", "Resume.txt"}
    assert "Resume.pdf" in outcome.message
    assert "manifest" not in outcome.message


def test_the_renderer_is_asked_for_the_setting_the_line_model_was_measured_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Selection budgets pages for one theme and density, so no render may fall back to another."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    document = run_dir / "Resume.md"
    document.write_text("# Resume\n", encoding="utf-8")
    arguments = tmp_path / "arguments"

    stub(
        binaries,
        f'#!/bin/sh\nprintf "%s\\n" "$@" > "{arguments}"\ntouch "{run_dir}/Resume.pdf"\nexit 0\n',
    )
    monkeypatch.setenv("PATH", f"{binaries}{os.pathsep}{os.environ['PATH']}")

    assert rendering.render(document).ok
    asked = arguments.read_text(encoding="utf-8").splitlines()
    # A resume renders under pressume's own defaults, with its checks reported as JSON.
    assert asked[:2] == ["render", "Resume.md"]
    assert "--config" not in asked
    assert "--json" in asked
    assert asked[asked.index("--theme") + 1] == layout.THEME
    assert asked[asked.index("--density") + 1] == layout.DENSITY
    assert asked[asked.index("--formats") + 1] == "pdf,txt"


def test_a_letter_is_rendered_under_the_one_page_letter_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resume contract fails a letter, so a letter names pressume's letter profile."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    document = run_dir / "Cover_Letter.md"
    document.write_text("# Ana Ferreira\n\nLisbon\n\nDear Globex hiring team,\n", encoding="utf-8")
    arguments = tmp_path / "arguments"
    config = tmp_path / "config.toml"

    stub(
        binaries,
        f'#!/bin/sh\nprintf "%s\\n" "$@" > "{arguments}"\ncp "$2" "{config}"\n'
        f'touch "{run_dir}/Cover_Letter.pdf"\nexit 0\n',
    )
    monkeypatch.setenv("PATH", f"{binaries}{os.pathsep}{os.environ['PATH']}")

    assert rendering.render(document, letter=True).ok
    asked = arguments.read_text(encoding="utf-8").splitlines()
    assert asked[0] == "--config"
    assert asked[2:4] == ["render", "Cover_Letter.md"]
    assert "--json" in asked
    assert Path(asked[1]).parent != run_dir
    assert tomllib.loads(config.read_text(encoding="utf-8")) == {
        "documents": [{"file": "Cover_Letter.md", "profile": "letter", "pages": 1}],
        "profiles": {"letter": {"policy": "letter"}},
    }
    assert sorted(path.name for path in run_dir.iterdir()) == [
        "Cover_Letter.md",
        "Cover_Letter.pdf",
    ]


def test_a_failed_check_is_reported_as_plain_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The task error names each failing check, not a coloured table cut off mid-sentence."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    document = run_dir / "Cover_Letter.md"
    document.write_text("Dear Globex hiring team,\n", encoding="utf-8")
    report = tmp_path / "report.json"
    rule = "MD041 the first line of the file must be the H1 name line"
    report.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "document": "Cover_Letter.md",
                        "check": "fonts",
                        "result": "pass",
                        "detail": "",
                    },
                    {
                        "document": "Cover_Letter.md",
                        "check": "document contract",
                        "result": "fail",
                        "detail": rule,
                    },
                ],
                "summary": {"passed": 1, "warnings": 0, "failed": 1},
            }
        ),
        encoding="utf-8",
    )
    stub(
        binaries,
        f'#!/bin/sh\ncat "{report}"\n'
        'printf "\\033[31m| FAIL | MD041 the first line of the|\\033[0m\\n" >&2\nexit 1\n',
    )
    monkeypatch.setenv("PATH", f"{binaries}{os.pathsep}{os.environ['PATH']}")

    outcome = rendering.render(document, letter=True)
    assert not outcome.ok
    assert f"document contract: {rule}" in outcome.message
    assert "\x1b" not in outcome.message
    assert "|" not in outcome.message
    assert "fonts" not in outcome.message


def test_a_successful_render_still_reports_the_checks_that_warned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A render that exits cleanly can still say the last page is nearly empty."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    document = run_dir / "Resume.md"
    document.write_text("# Resume\n", encoding="utf-8")
    report = tmp_path / "report.json"
    balance = "page balance: the final page is lightly filled"
    report.write_text(
        json.dumps(
            {
                "results": [
                    {"document": "Resume.md", "check": "fonts", "result": "pass", "detail": ""},
                    {
                        "document": "Resume.md",
                        "check": "readability",
                        "result": "warn",
                        "detail": balance,
                    },
                ],
                "summary": {"passed": 1, "warnings": 1, "failed": 0},
            }
        ),
        encoding="utf-8",
    )
    stub(binaries, f'#!/bin/sh\ncat "{report}"\ntouch "{run_dir}/Resume.pdf"\nexit 0\n')
    monkeypatch.setenv("PATH", f"{binaries}{os.pathsep}{os.environ['PATH']}")

    outcome = rendering.render(document)
    assert outcome.ok
    assert "Resume.pdf" in outcome.message
    assert len(outcome.warnings) == 1
    assert f"readability: {balance}" in outcome.warnings[0]
    assert "fonts" not in outcome.warnings[0]


def test_a_failing_renderer_never_costs_the_run_its_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deliverable is the Markdown. A renderer that falls over is an inconvenience."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    document = run_dir / "Resume.md"
    document.write_text("# Resume\n", encoding="utf-8")

    stub(binaries, "#!/bin/sh\necho 'typst is not installed' >&2\nexit 3\n")
    monkeypatch.setenv("PATH", f"{binaries}{os.pathsep}{os.environ['PATH']}")

    outcome = rendering.render(document)
    assert outcome.attempted
    assert not outcome.ok
    assert "typst is not installed" in outcome.message
    assert document.read_text(encoding="utf-8") == "# Resume\n"


def test_a_renderer_that_writes_nothing_is_not_reported_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit zero is the renderer's claim. The files are the evidence."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    document = run_dir / "Resume.md"
    document.write_text("# Resume\n", encoding="utf-8")

    stub(binaries, "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("PATH", f"{binaries}{os.pathsep}{os.environ['PATH']}")

    outcome = rendering.render(document)
    assert outcome.attempted
    assert not outcome.ok
    assert "wrote nothing" in outcome.message


def test_the_renderer_is_never_a_dependency() -> None:
    """It is a program on PATH, not an import and not a declared requirement."""
    root = REPO_ROOT
    manifest = (root / "pyproject.toml").read_text(encoding="utf-8")
    dependencies = manifest[
        manifest.index("dependencies = [") : manifest.index("[project.scripts]")
    ]
    assert rendering.RENDERER not in dependencies

    for path in (root / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert f"import {rendering.RENDERER}" not in text, f"{path.name} imports the renderer"
