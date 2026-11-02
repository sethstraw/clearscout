"""Hand the finished Markdown to a renderer, if the user has one and asks.

A separate program found on PATH, with no import and no declared dependency.
Rendering can never fail a run: the deliverable is the Markdown, so a missing
PDF costs one line of output.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from tailorcv.application.tailoring.layout import DENSITY, THEME

RENDERER = "pressume"
# Bound renderer time so a stalled process cannot block a completed run.
TIMEOUT = 180.0
# The render task indexes a PDF and a text copy; a pressume.toml may name others.
FORMATS = "pdf,txt"
# pressume's resume contract fails a letter, so a letter names its letter policy and length.
LETTER_PROFILE = "letter"
LETTER_PAGES = 1


@dataclass(frozen=True)
class Rendered:
    """What came of asking the renderer, in a form the CLI can print."""

    attempted: bool
    ok: bool
    message: str
    produced: tuple[Path, ...] = ()
    # One line per check the renderer warned on, reported even when it succeeded.
    warnings: tuple[str, ...] = ()


def available() -> str | None:
    """Return the renderer's path, or ``None`` when the user does not have one."""
    return shutil.which(RENDERER)


def render(document: Path, *, letter: bool = False) -> Rendered:
    """Ask the renderer to turn ``document`` into its other formats, beside it.

    A ``letter`` renders under pressume's one-page letter policy. Never raises: a
    run that produced good Markdown has succeeded, and this is the last thing it does.
    """
    executable = available()
    if executable is None:
        return Rendered(
            attempted=False,
            ok=False,
            message=(
                f"--render asked for a rendered copy, but {RENDERER} is not on your PATH.\n"
                "  The Markdown is written and unaffected. Install it with:\n"
                "  uv tool install git+https://github.com/sethstraw/pressume.git\n"
                "  Then restart TailorCV so the updated PATH is visible."
            ),
        )

    directory = document.parent
    before = _outputs(directory)
    try:
        # The config sits outside the output directory, so a render never lists it as produced.
        with tempfile.TemporaryDirectory(prefix="tailorcv-pressume-") as scratch:
            options: list[str] = []
            if letter:
                config = Path(scratch) / "pressume.toml"
                config.write_text(_letter_config(document.name), encoding="utf-8")
                options = ["--config", str(config)]
            # Use a fixed argument list without a shell for generated paths.
            finished = subprocess.run(  # noqa: S603
                [
                    executable,
                    *options,
                    "render",
                    document.name,
                    "--source",
                    str(directory),
                    "--output",
                    str(directory),
                    "--formats",
                    FORMATS,
                    "--theme",
                    THEME,
                    "--density",
                    DENSITY,
                    "--json",
                ],
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
                check=False,
            )
    except (OSError, subprocess.SubprocessError) as error:
        return Rendered(attempted=True, ok=False, message=f"{RENDERER} could not run: {error}")

    produced = tuple(sorted(_outputs(directory) - before))
    if finished.returncode != 0:
        failed = "\n".join(_checks(finished, "fail"))
        return Rendered(
            attempted=True,
            ok=False,
            message=(
                f"{RENDERER} did not finish:\n{failed or _why(finished)}\n"
                "  The Markdown is written and unaffected."
            ),
            produced=produced,
        )
    if not produced:
        return Rendered(
            attempted=True,
            ok=False,
            message=f"{RENDERER} reported success but wrote nothing next to the Markdown.",
        )
    names = ", ".join(path.name for path in produced)
    return Rendered(
        attempted=True,
        ok=True,
        message=f"Rendered {names}",
        produced=produced,
        warnings=tuple(f"{RENDERER} warned:\n{line}" for line in _checks(finished, "warn")),
    )


def _letter_config(name: str) -> str:
    """Return the pressume config that renders ``name`` as a one-page letter."""
    return (
        "[[documents]]\n"
        f"file = {json.dumps(name)}\n"
        f'profile = "{LETTER_PROFILE}"\n'
        f"pages = {LETTER_PAGES}\n"
        "\n"
        f"[profiles.{LETTER_PROFILE}]\n"
        f'policy = "{LETTER_PROFILE}"\n'
    )


def _checks(finished: subprocess.CompletedProcess[str], result: str) -> tuple[str, ...]:
    """Return each check ``--json`` reports with ``result``, one plain line apiece."""
    try:
        report = json.loads(finished.stdout or "")
    except ValueError:
        return ()
    results = report.get("results") if isinstance(report, dict) else None
    if not isinstance(results, list):
        return ()
    return tuple(
        f"    {item.get('check', 'check')}: {item['detail']}"
        if item.get("detail")
        else f"    {item.get('check', 'check')}"
        for item in results
        if isinstance(item, dict) and item.get("result") == result
    )


def _why(finished: subprocess.CompletedProcess[str]) -> str:
    """Return the lines that explain a failure, not the tally that counts them.

    A renderer ends with "3 passed, 0 warnings, 1 failed", so the last line of
    its output is the summary rather than the reason, and printing it leaves a
    person to re-run the renderer by hand to learn anything.
    """
    output = f"{finished.stdout or ''}\n{finished.stderr or ''}"
    lines = [line.rstrip() for line in output.splitlines() if line.strip()]
    blamed = [line for line in lines if "FAIL" in line or "Error" in line or "error" in line]
    chosen = blamed or lines[-3:]
    return "\n".join(f"    {line}" for line in chosen[:8]) or (
        f"    exit status {finished.returncode}"
    )


def _outputs(directory: Path) -> set[Path]:
    """Return the visible non-Markdown files in ``directory``, which is what a render adds.

    pressume keeps its own manifest there as a dotfile, which is no output of the render.
    """
    if not directory.is_dir():
        return set()
    return {
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix != ".md" and not path.name.startswith(".")
    }
